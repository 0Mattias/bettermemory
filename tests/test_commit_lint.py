"""Tests for `tools/commit_lint.py`, the commit-message gate.

A linter that blocks pushes is a liability without tests: a false
positive costs a developer a rebase, and a false negative is the reason
the linter exists. Both directions are pinned here.

The module lives outside `src/`, so it is loaded by path the same way
`tests/test_bench_claims.py` loads the bench harnesses.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_LINTER = Path(__file__).resolve().parents[1] / "tools" / "commit_lint.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("commit_lint", _LINTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["commit_lint"] = module
    spec.loader.exec_module(module)
    return module


commit_lint = _load()


def rules(message: str) -> set[str]:
    """The set of rule names `message` violates."""
    return {v.rule for v in commit_lint.lint_message(message, "test")}


# ---------------------------------------------------------------------------
# Messages that must pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "fix(store): release the lock before re-reading the index",
        "feat: add a truncation guard to memory_update",
        "docs(api): document the episode_search worktree filter",
        "test(concurrency): pin the CAS loser count",
        "perf(search): skip the snippet scan on short bodies",
        "release: 3.33.0",
        "refactor(handlers)!: fold the two scope-toggle tools into one",
        "ci: pin actions/checkout to v7",
        "fix: correct the CI matrix comment",
    ],
)
def test_conforming_subjects_pass(message: str) -> None:
    assert rules(message) == set()


def test_body_and_trailer_pass() -> None:
    message = (
        "fix(store): reject a body that shrinks to a prefix of the stored one\n"
        "\n"
        "A read-modify-write that loses its tail produces exactly this shape,\n"
        "and the store had no way to tell it from a deliberate trim.\n"
        "\n"
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
    )
    assert rules(message) == set()


def test_quoted_first_person_is_a_citation_not_narration() -> None:
    """A quoted user utterance is evidence; it is not the author narrating.

    Without this exemption the linter fires on every commit that records
    what was actually asked for, which is precisely the detail worth
    keeping.
    """
    message = (
        "fix(search): stop dropping the pronoun from a natural-language query\n"
        "\n"
        'On this store, `memory_search("how do I cut a release")` returned\n'
        "nothing because the tokenizer discarded the leading pronoun.\n"
    )
    assert rules(message) == set()


def test_long_unwrappable_body_lines_are_exempt() -> None:
    """URLs, paths and indented transcripts are not re-wrappable."""
    url = "https://example.com/" + "a" * 120
    message = (
        "docs: link the upstream migration guide\n"
        "\n"
        f"{url}\n"
        "\n"
        "    $ " + "uv run pytest " + "-k something_quite_long " * 5 + "\n"
        "\n"
        "| column | " + "value " * 30 + "|\n"
    )
    assert rules(message) == set()


def test_generated_merge_and_revert_subjects_are_exempt() -> None:
    assert rules('Revert "feat: add a thing"') == set()
    assert rules("Merge branch 'main' into topic") == set()


def test_comments_and_verbose_diff_are_stripped() -> None:
    """`git commit -v` appends the staged diff; git strips it, so must we."""
    message = (
        "fix(ci): raise the commit-lint checkout depth\n"
        "\n"
        "# Please enter the commit message for your changes.\n"
        "# On branch main\n"
        f"{commit_lint._SCISSORS}\n"
        "diff --git a/x b/x\n"
        "+" + "z" * 200 + "\n"
        "+I rewrote this line myself\n"
    )
    assert rules(message) == set()


# ---------------------------------------------------------------------------
# Messages that must fail
# ---------------------------------------------------------------------------


def test_missing_conventional_prefix_is_rejected() -> None:
    assert "subject-format" in rules("made the store faster")


def test_unknown_type_is_rejected() -> None:
    assert "subject-format" in rules("docs+desc: truth-sync the resident surfaces")


def test_overlong_subject_is_rejected() -> None:
    subject = "fix(store): " + "a" * 80
    assert "subject-length" in rules(subject)


def test_subject_length_boundary() -> None:
    """Exactly at the limit passes; one over fails."""
    at_limit = "fix: " + "a" * (commit_lint.MAX_SUBJECT - len("fix: "))
    assert len(at_limit) == commit_lint.MAX_SUBJECT
    assert rules(at_limit) == set()
    assert "subject-length" in rules(at_limit + "a")


def test_sentence_cased_description_is_rejected() -> None:
    assert "subject-case" in rules("fix: Windows has no POSIX mode bits")


def test_all_caps_token_may_open_the_description() -> None:
    """`CI`, `README`, `YAML` are names, not sentence casing."""
    assert rules("fix: CI could not resolve the push range") == set()


def test_trailing_period_is_rejected() -> None:
    assert "subject-period" in rules("fix(store): release the lock earlier.")


def test_first_person_narration_is_rejected() -> None:
    message = (
        "bench(longmemeval): their search returns zero\n"
        "\n"
        "I do not yet know why, and my first hypothesis was wrong.\n"
    )
    assert "first-person" in rules(message)


def test_first_person_in_the_subject_is_rejected() -> None:
    assert "first-person" in rules("fix: I finally tracked down the leak")


def test_io_is_not_first_person() -> None:
    """The `I` rule must not fire on `I/O`, `IOError`, or a bare `i`."""
    assert rules("perf(store): halve the I/O on a cold load") == set()
    assert rules("fix: translate IOError at the parser boundary") == set()


def test_filler_is_rejected() -> None:
    assert "filler" in rules("perf: stop paying for titles nobody reads")
    assert "filler" in rules(
        "fix(index): rebuild on a schema bump\n\nTurns out the version was stale.\n"
    )


def test_session_narration_is_rejected() -> None:
    """The sitting that produced a commit is not addressable by its reader."""
    assert "session-narration" in rules(
        "fix: repair four claims the last round overstated"
    )
    assert "session-narration" in rules(
        "docs(changelog): note the ingest allowlist\n"
        "\n"
        "Nearly every fix in this window is a check that reported on something\n"
        "other than the thing it claimed to report on.\n"
    )
    assert "session-narration" in rules(
        "fix(doctor): probe the resolved provider\n"
        "\n"
        "An adversarial pass over the previous six commits found the repair\n"
        "described itself more strongly than the code supported.\n"
    )


def test_a_domain_window_is_not_session_narration() -> None:
    """This project has real windows — demotion, verification, tag ranges.

    The determiners are enumerated rather than left open precisely so that
    a sentence about one of those does not have to be contorted around the
    rule. Only the deictic forms that point at the author's own calendar
    are matched.
    """
    message = (
        "fix(demotion): count an outcome once per window\n"
        "\n"
        "The 30-day demotion window and the newest tag window are both\n"
        "half-open, and the current window was double-counting its edge.\n"
    )
    assert rules(message) == set()


def test_editorial_grading_of_a_defect_is_rejected() -> None:
    """Describe the defect; do not comment on how bad it was."""
    assert "editorial" in rules(
        "fix(doctor): reconcile the index against disk\n"
        "\n"
        "`doctor` certified a stale index for the third time.\n"
    )
    assert "editorial" in rules(
        "docs(claims): describe the corpus each half covers\n"
        "\n"
        "The gap is worth stating plainly: the ratchet scanned 12 of 43 files.\n"
    )
    assert "editorial" in rules(
        "fix(ingest): enforce the scope allowlist\n"
        "\n"
        "`--dry-run` cheerfully reported writes that were about to be refused.\n"
    )


def test_naming_a_recurrence_as_a_fact_is_not_editorialising() -> None:
    """A cited incident and a named defect class are evidence, not grading.

    The rule has to leave room for the thing that makes a recurrence
    actionable — which incident, which lesson, which earlier commit — or
    it would push authors into vaguer prose than they started with.
    """
    message = (
        "fix(doctor): probe the resolved provider instead of inferring it\n"
        "\n"
        "This is the shape lesson 1 of\n"
        "docs/incidents/2026-07-25-doctor-false-green-on-importable-extra.md\n"
        "names: a check may skip only on the condition it measures. The repair\n"
        "in `0bf7a49` covered the sibling branch and not this one.\n"
    )
    assert rules(message) == set()


def test_a_release_subject_carries_the_version_alone() -> None:
    """The tag's argument belongs in the CHANGELOG entry, not the subject."""
    assert "release-subject" in rules(
        "release: 3.36.0 (a green light is only worth what it measured)"
    )
    assert "release-subject" in rules("release: the mcp 2.x port")
    assert rules("release: 3.36.0") == set()
    assert rules("release: 3.36.0rc1") == set()


def test_missing_blank_line_before_body_is_rejected() -> None:
    message = "fix(store): release the lock earlier\nThe body starts here.\n"
    assert "body-separator" in rules(message)


def test_overlong_wrappable_body_line_is_rejected() -> None:
    message = "docs: expand the rationale\n\n" + ("word " * 40) + "\n"
    assert "body-length" in rules(message)


def test_empty_message_is_rejected() -> None:
    assert "empty" in rules("   \n\n")
    assert "empty" in rules("# only a comment\n")


# ---------------------------------------------------------------------------
# The CLI surface the hook and CI depend on
# ---------------------------------------------------------------------------


def test_message_file_mode_returns_nonzero_on_a_violation(tmp_path: Path) -> None:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text("nope, no prefix here", encoding="utf-8")
    assert commit_lint.main(["--message-file", str(path)]) == 1

    path.write_text("fix(store): release the lock earlier", encoding="utf-8")
    assert commit_lint.main(["--message-file", str(path)]) == 0


def test_unresolvable_range_does_not_fail_the_build(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A force push or a first push leaves a range git cannot resolve.

    That must not read as a style failure — the author cannot fix it by
    editing a message, so blocking on it would just teach everyone to
    bypass the gate.
    """
    zeroes = "0" * 40
    assert commit_lint.main(["--range", f"{zeroes}..HEAD"]) == 0
    assert "cannot resolve range" in capsys.readouterr().err


def test_the_hook_script_is_executable_and_points_at_the_linter() -> None:
    """The hook is only useful if git can actually exec it."""
    hook = Path(__file__).resolve().parents[1] / ".githooks" / "commit-msg"
    assert hook.exists()
    text = hook.read_text(encoding="utf-8")
    assert "tools/commit_lint.py" in text
    assert "--message-file" in text
    if sys.platform != "win32":
        assert hook.stat().st_mode & 0o111, "commit-msg hook is not executable"
