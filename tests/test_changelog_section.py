"""`tools/changelog_section.py`: the CHANGELOG section a GitHub release
carries as its notes (`release.yml`, the github-release job)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "changelog_section", REPO / "tools" / "changelog_section.py"
)
assert _SPEC is not None and _SPEC.loader is not None
changelog_section = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(changelog_section)

TEXT = """\
# Changelog

## 9.0.0 - Unreleased

The stable summary.

### 9.0.0rc10

#### Fixed

- rc10's fix.

### 9.0.0rc9

- rc9's line.

## 8.0.0 - 2026-09-24

- 8.0.0's line.

## 7.0.0 - 2026-08-01

### 7.0.0rc1
"""


def test_a_candidates_section_runs_to_the_next_heading_of_its_level() -> None:
    """A deeper heading inside the section stays; the next candidate's or
    the next release's heading ends it."""
    assert changelog_section.section(TEXT, "9.0.0rc10") == (
        "#### Fixed\n\n- rc10's fix.\n"
    )
    assert changelog_section.section(TEXT, "9.0.0rc9") == "- rc9's line.\n"
    assert changelog_section.section(TEXT, "8.0.0") == "- 8.0.0's line.\n"


def test_a_fenced_code_blocks_comment_is_not_a_heading() -> None:
    """A ``# comment`` line inside a fenced block stays in the section it
    sits in; only a heading outside a fence ends one."""
    fenced = TEXT.replace(
        "- rc10's fix.\n",
        "- rc10's fix.\n\n```bash\n# a comment, not a heading\nbettermemory doctor\n```\n",
    )
    assert changelog_section.section(fenced, "9.0.0rc10") == (
        "#### Fixed\n\n- rc10's fix.\n\n```bash\n# a comment, not a heading\n"
        "bettermemory doctor\n```\n"
    )
    assert changelog_section.section(fenced, "9.0.0rc9") == "- rc9's line.\n"


def test_a_stable_section_is_the_summary_above_its_candidates() -> None:
    assert changelog_section.section(TEXT, "9.0.0") == "The stable summary.\n"


def test_a_missing_or_empty_section_is_none_and_fails_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tag whose version has no section, or an empty one (a stable heading
    with no summary before its first candidate), fails the release job
    instead of publishing an empty body."""
    assert changelog_section.section(TEXT, "9.0.0rc99") is None
    assert changelog_section.section(TEXT, "7.0.0") is None
    path = tmp_path / "CHANGELOG.md"
    path.write_text(TEXT, encoding="utf-8")
    assert changelog_section.main(["changelog_section.py", str(path), "9.0.0rc99"]) == 1
    assert "9.0.0rc99" in capsys.readouterr().err
    assert changelog_section.main(["changelog_section.py", str(path), "9.0.0rc9"]) == 0
    assert capsys.readouterr().out == "- rc9's line.\n"
    assert changelog_section.main(["changelog_section.py"]) == 2


def test_the_repositorys_rc9_section_reads() -> None:
    """The real file: rc9's section starts with its summary and ends before
    rc8's heading."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    body = changelog_section.section(text, "9.0.0rc9")
    assert body is not None and body.startswith("The ninth candidate")
    assert "### 9.0.0rc8" not in body
    assert "#### Fixed" in body


def test_a_section_over_the_cap_is_cut_at_a_paragraph_with_a_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """GitHub refuses a release body over 125,000 characters and the
    github-release job runs after the PyPI publish, so a section over the
    cap is cut at a paragraph boundary to fit it, ending in a note that says
    how long the section is and that CHANGELOG.md holds it whole; a section
    that fits is unchanged; a cap under the minimum is a usage error."""
    body = "".join(f"- bullet {i} of the section.\n\n" for i in range(100))
    body += "- the last bullet.\n"
    assert changelog_section.cut(body, len(body)) == body
    cut = changelog_section.cut(body, 1200)
    assert len(cut) <= 1200
    kept, _, note = cut.rpartition("\n\n")
    assert kept.endswith("of the section.") and body.startswith(kept + "\n\n")
    assert note.startswith("_The notes stop here: ")
    assert f"{len(body):,} characters" in note
    assert note.endswith("CHANGELOG.md at this tag._\n")
    with pytest.raises(ValueError):
        changelog_section.cut(body, changelog_section.MIN_MAX_CHARS - 1)
    # one-line bullets with no blank line between them: the cut falls before
    # a bullet, not back at the last blank line
    listed = "Intro.\n\n" + "".join(f"- bullet {i} of the list.\n" for i in range(300))
    cut_list = changelog_section.cut(listed, 1200)
    kept_list = cut_list.rpartition("\n\n")[0]
    assert len(cut_list) <= 1200 and len(kept_list) > 1200 - 400
    assert kept_list.endswith("of the list.") and listed.startswith(kept_list + "\n- ")
    path = tmp_path / "CHANGELOG.md"
    path.write_text(TEXT.replace("- rc9's line.\n", body), encoding="utf-8")
    argv = ["changelog_section.py", "--max-chars", "1200", str(path), "9.0.0rc9"]
    assert changelog_section.main(argv) == 0
    assert capsys.readouterr().out == cut
    assert changelog_section.main(argv[:1] + argv[3:]) == 0
    assert capsys.readouterr().out == body
    for bad in ("x", "10"):
        assert changelog_section.main(argv[:2] + [bad] + argv[3:]) == 2
        assert "max-chars" in capsys.readouterr().err


def test_the_repositorys_rc14_section_fits_the_release_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real file: the fourteenth candidate's section, the longest, comes
    out of the command within `RELEASE_BODY_CHARS`, cut with the note when
    the section is longer than that and whole otherwise."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    body = changelog_section.section(text, "9.0.0rc14")
    assert body is not None and body.startswith("The fourteenth candidate")
    argv = ["changelog_section.py", str(REPO / "CHANGELOG.md"), "9.0.0rc14"]
    assert changelog_section.main(argv) == 0
    out = capsys.readouterr().out
    assert len(out) <= changelog_section.RELEASE_BODY_CHARS
    if len(body) > changelog_section.RELEASE_BODY_CHARS:
        assert len(out) >= changelog_section.RELEASE_BODY_CHARS - 2_500
        assert out.endswith("CHANGELOG.md at this tag._\n")
        assert body.startswith(out.rpartition("\n\n")[0] + "\n")
    else:
        assert out == body
