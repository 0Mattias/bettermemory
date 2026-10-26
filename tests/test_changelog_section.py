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
