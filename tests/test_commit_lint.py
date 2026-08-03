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


def test_a_hyphenated_compound_is_not_the_banned_phrase() -> None:
    """`round-trip`, `window-relative`, `session-scoped` name code.

    A hyphen is not a word character, so a bare `(?!\\w)` boundary matches
    inside the compound and turns this project's own vocabulary into a
    violation: `this round-trip` is a serialization property and
    `embarrassing-looking` is an adjective about column widths.
    `_FIRST_PERSON_RE` already excludes the hyphen so that `I/O` survives;
    the three phrase rules bound the same way.
    """
    for body in (
        "Without this round-trip, the audit cannot replay which claim applied.",
        "This round-trips one event through the recorder.",
        "The last round-trip through the provider was dropped.",
        "The counter is this session-scoped one, not the global one.",
        "A demotion window and this window-relative count disagree.",
        "The previous sweep-and-prune pass left the tombstone behind.",
        "The embarrassing-looking column widths come from the table writer.",
        "The cheerfully-named flag actually disables the cache.",
        "A not-obviously-wrong default kept the parser quiet.",
    ):
        assert rules(f"fix(store): rework the recorder\n\n{body}\n") == set(), body


def test_the_phrase_is_still_rejected_when_it_stands_alone() -> None:
    """Counterweight: the boundary excuses the compound, not the phrase."""
    assert "session-narration" in rules(
        "fix(store): rework the recorder\n\nThe last round overstated four claims.\n"
    )
    assert "session-narration" in rules(
        "fix(store): rework the recorder\n\nOne of this session's episodes was lost.\n"
    )
    assert "editorial" in rules(
        "fix(store): rework the recorder\n\nEmbarrassingly, the index was stale.\n"
    )


def test_a_banned_phrase_split_across_a_wrap_is_still_caught() -> None:
    """Three of the four rules are multi-word, and a body is hard-wrapped.

    A gate whose verdict depends on where a line happened to break is not
    measuring the thing it claims to: `worth stating plainly` is the same
    phrase whether or not the author's wrap point fell inside it.
    """
    message = (
        "docs(changelog): describe the recorder change\n"
        "\n"
        "The helper was rewritten during the\n"
        "last session, and the old behaviour was\n"
        "worth\n"
        "stating plainly, because it turns\n"
        "out the tuple carried no order.\n"
    )
    assert rules(message) == {"filler", "session-narration", "editorial"}


def test_a_blank_line_is_a_break_rather_than_a_wrap() -> None:
    """Joining is wrap repair, not flattening the whole message.

    Two paragraphs are two sentences by construction, so gluing the tail
    of one onto the head of the next would invent a phrase nobody wrote.
    """
    message = (
        "docs(changelog): describe the recorder change\n"
        "\n"
        "The behaviour is unchanged for the last\n"
        "\n"
        "session counts, which were already correct.\n"
    )
    assert rules(message) == set()


def test_a_labelled_body_line_is_not_a_trailer() -> None:
    """`Note:`, `Tests:`, `Gate:` are labels this project writes in bodies.

    Exempting everything shaped like `<capitalised word>: <text>` turns
    all four wording rules off for the rest of the line, so the exemption
    enumerates the trailer keys instead.
    """
    assert rules(
        "fix(store): release the lock before re-reading the index\n"
        "\n"
        "Note: obviously my first hypothesis was wrong.\n"
    ) == {"first-person", "filler"}
    assert "session-narration" in rules(
        "test(store): pin the CAS loser count\n"
        "\n"
        "Tests: re-ran the adversarial pass over the previous six commits.\n"
    )


def test_a_real_trailer_is_still_exempt() -> None:
    """A `Co-Authored-By:` value is a name, not prose anyone chose."""
    message = (
        "fix(store): release the lock before re-reading the index\n"
        "\n"
        "The read-modify-write lost its tail.\n"
        "\n"
        "Co-Authored-By: I. M. Author <author@example.com>\n"
    )
    assert rules(message) == set()


def test_a_named_wording_rule_can_be_waived() -> None:
    """`this window` is session narration in one body and a race window in
    the next, and no pattern separates them. The escape is explicit, per
    rule, and permanent — a reviewer reads the waiver in the log.
    """
    message = (
        "fix(store): set the mode on the fd, not after the rename\n"
        "\n"
        "The canonical write calls `os.fchmod` BEFORE the rename precisely\n"
        "to close this window (see store.py:1176-1185).\n"
        "\n"
        "Lint-skip: session-narration\n"
    )
    assert rules(message) == set()
    assert "session-narration" in rules(
        message.replace("\n\nLint-skip: session-narration\n", "")
    )


def test_a_waiver_silences_only_the_rule_it_names() -> None:
    message = (
        "fix(store): set the mode on the fd, not after the rename\n"
        "\n"
        "Honestly, the canonical write sets the mode before the rename to\n"
        "close this window (see store.py:1176-1185).\n"
        "\n"
        "Lint-skip: session-narration\n"
    )
    assert rules(message) == {"filler"}


def test_a_waiver_cannot_name_an_envelope_rule_or_a_wildcard() -> None:
    """The envelope is mechanical, so there is no judgement to escape.

    An unwaivable name fails closed: whatever the rules caught still
    stands, and the waiver itself is reported.
    """
    envelope = (
        "fix(store): set the mode on the fd, not after the rename\n"
        "\n"
        "The write closes this window (see store.py:1176-1185).\n"
        "\n"
        "Lint-skip: subject-length\n"
    )
    assert rules(envelope) == {"lint-skip", "session-narration"}

    wildcard = (
        "fix(store): set the mode on the fd, not after the rename\n"
        "\n"
        "Honestly, the write closes this window.\n"
        "\n"
        "Lint-skip: all\n"
    )
    assert rules(wildcard) == {"lint-skip", "filler", "session-narration"}


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


def test_a_stored_message_is_graded_with_the_lines_git_kept() -> None:
    """`--range` grades history, and history keeps `#` body lines.

    `git commit -m` and `-F` run `--cleanup=whitespace`, which strips
    trailing whitespace and collapses blank lines but does NOT remove
    comment lines; only an editor buffer gets `--cleanup=default`.
    Stripping a stored message before grading it therefore deletes text
    the commit really carries and reports green over prose nobody can see
    the linter skip.
    """
    stored = (
        "fix(store): release the lock earlier\n"
        "\n"
        "# I found this embarrassing bug in the last round, and honestly\n"
        "# it was worth stating plainly.\n"
    )
    assert {
        v.rule for v in commit_lint.lint_message(stored, "abc1234", strip=False)
    } == {"first-person", "filler", "session-narration", "editorial"}
    # Stripping is what an editor buffer needs, and only an editor buffer:
    # grading the scaffolding git wrote there would block a good commit.
    assert rules(stored) == set()


_COMMENT_ONLY_VIOLATION = (
    "fix(store): release the lock earlier\n"
    "\n"
    "# Honestly, the last round got this wrong.\n"
)


def test_the_hook_grades_a_comment_line_git_is_about_to_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hook's whole job is to fail here, one push before CI does.

    `git commit -m` never opens an editor, so git invokes the hook with
    `GIT_EDITOR=:` (githooks(5)) and resolves `--cleanup=default` to
    `whitespace`, which stores the `#` line verbatim. A hook that stripped
    it would pass a message `--range` then rejects — the message is already
    written by then, so the fix costs a rebase rather than an edit.
    """
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(_COMMENT_ONLY_VIOLATION, encoding="utf-8")
    monkeypatch.setenv("GIT_EDITOR", ":")

    assert commit_lint.main(["--message-file", str(path)]) == 1

    reported = capsys.readouterr().out
    graded_in_ci = {
        v.rule
        for v in commit_lint.lint_message(
            _COMMENT_ONLY_VIOLATION, "abc1234", strip=False
        )
    }
    assert graded_in_ci == {"filler", "session-narration"}
    for rule in graded_in_ci:
        assert f"{path}: {rule}:" in reported


def test_the_hook_ignores_the_scaffolding_git_is_about_to_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counterweight: an editor buffer is graded without git's own text.

    Git leaves `GIT_EDITOR` alone when it is about to launch an editor, and
    then resolves `--cleanup=default` to `strip`. Grading the template git
    wrote into the buffer, or the `git commit -v` diff below the scissors,
    would block a commit over lines that never enter history.
    """
    buffer = (
        "fix(store): release the lock earlier\n"
        "\n"
        "# Please enter the commit message for your changes. Lines starting\n"
        "# with '#' will be ignored, and an empty message aborts the commit.\n"
        "#\n"
        "# On branch main\n"
        "# Changes to be committed:\n"
        "#\tmodified:   tools/commit_lint.py\n"
        f"{commit_lint._SCISSORS}\n"
        "diff --git a/x b/x\n"
        "+I rewrote this line myself, honestly\n"
    )
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(buffer, encoding="utf-8")

    monkeypatch.delenv("GIT_EDITOR", raising=False)
    assert commit_lint.main(["--message-file", str(path)]) == 0
    monkeypatch.setenv("GIT_EDITOR", "vim")
    assert commit_lint.main(["--message-file", str(path)]) == 0


def test_only_gits_own_sentinel_means_no_editor() -> None:
    """`:` is the value githooks(5) documents, and nothing else means it.

    A no-op editor a user configured themselves — `true`, `/usr/bin/true` —
    is still an editor to `--cleanup=default`, which follows whether git was
    asked to edit rather than what the editor does.
    """
    assert commit_lint._editor_will_run({}) is True
    assert commit_lint._editor_will_run({"GIT_EDITOR": "vim"}) is True
    assert commit_lint._editor_will_run({"GIT_EDITOR": "true"}) is True
    assert commit_lint._editor_will_run({"GIT_EDITOR": ":"}) is False


def _stub_git(
    monkeypatch: pytest.MonkeyPatch, commits: dict[str, tuple[str, str]]
) -> list[list[str]]:
    """Serve `git rev-list` / `git log` for `--range` mode from a table.

    Range mode's whole job is to walk a range and grade what it finds, so
    the test has to enumerate that range itself: the ambient repository
    differs per clone, per CI leg and per push, and a test that read it
    would measure a different population every run. `commits` maps sha to
    `(author, message)` in range order; the returned list records every
    argv the linter handed to git.
    """
    calls: list[list[str]] = []

    class _Completed:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(argv: list[str], **kwargs: object) -> _Completed:
        calls.append(argv)
        assert kwargs["check"] is True
        if argv[1] == "rev-list":
            return _Completed("".join(f"{sha}\n" for sha in commits))
        assert argv[1] == "log" and argv[2] == "-1"
        author, message = commits[argv[4]]
        return _Completed(f"{author}\n" if argv[3] == "--format=%an" else message)

    monkeypatch.setattr(commit_lint.subprocess, "run", fake_run)
    return calls


def test_range_mode_reports_a_violating_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The only mode CI runs, on the only outcome that blocks a push."""
    calls = _stub_git(
        monkeypatch,
        {
            "1111111111111111": ("A Maintainer", "fix(store): release the lock\n"),
            "2222222222222222": ("A Maintainer", "made the store faster, honestly\n"),
            "3333333333333333": (
                "A Maintainer",
                "fix(store): drop the stale entry\n\n# I have not reproduced it.\n",
            ),
        },
    )
    assert commit_lint.main(["--range", "base..head"]) == 1

    out = capsys.readouterr().out
    assert "22222222: subject-format" in out
    assert "22222222: filler" in out
    # A stored `#` line is body text git kept, not editor scaffolding.
    assert "33333333: first-person" in out
    assert "11111111" not in out
    # Merges carry generated subjects and are graded through their parents.
    assert calls[0] == ["git", "rev-list", "--no-merges", "base..head"]
    assert ["git", "log", "-1", "--format=%B", "1111111111111111"] in calls


def test_range_mode_passes_a_conforming_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterweight: the range walk has to actually read every commit.

    Without this, a range mode that returned no shas at all would look
    identical to one that graded them and found nothing.
    """
    calls = _stub_git(
        monkeypatch,
        {
            "1111111111111111": ("A Maintainer", "fix(store): release the lock\n"),
            "2222222222222222": ("A Maintainer", "docs(api): document the filter\n"),
        },
    )
    assert commit_lint.main(["--range", "base..head"]) == 0
    assert [call[4] for call in calls if "--format=%B" in call] == [
        "1111111111111111",
        "2222222222222222",
    ]


# Verbatim from this repository's history. Dependabot composes both from a
# template; the second is the squash subject GitHub built from the PR
# title. Neither can be edited into shape by the account that authored it.
_DEPENDABOT_GROUPED = (
    "chore(deps): Bump the actions group with 3 updates\n"
    "\n"
    "Bumps the actions group with 3 updates in the / directory: "
    "[actions/checkout](https://github.com/actions/checkout), "
    "[astral-sh/setup-uv](https://github.com/astral-sh/setup-uv) and "
    "[actions/setup-python](https://github.com/actions/setup-python).\n"
)

_DEPENDABOT_SQUASH = "Bump the actions group across 1 directory with 2 updates (#2)\n"


def test_the_real_dependabot_messages_do_break_the_envelope() -> None:
    """The exemption exists because these fail, not because they pass."""
    assert "subject-case" in rules(_DEPENDABOT_GROUPED)
    assert "body-length" in rules(_DEPENDABOT_GROUPED)
    assert "subject-format" in rules(_DEPENDABOT_SQUASH)


def test_a_bot_authored_commit_is_not_graded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard must not judge input the author cannot control.

    Both messages sit inside the pushed range of a merged Dependabot PR,
    so grading them turns `commit-lint` red on every weekly bump —
    including security patches — over a subject and a body the bot wrote
    from a template.
    """
    _stub_git(
        monkeypatch,
        {
            "3f25dde5aaaaaaaa": ("dependabot[bot]", _DEPENDABOT_GROUPED),
            "82f4b931bbbbbbbb": ("dependabot[bot]", _DEPENDABOT_SQUASH),
        },
    )
    assert commit_lint.main(["--range", "base..head"]) == 0


def test_the_same_messages_from_a_person_are_graded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterweight: the exemption keys on authorship, not on the text."""
    _stub_git(
        monkeypatch,
        {
            "3f25dde5aaaaaaaa": ("A Maintainer", _DEPENDABOT_GROUPED),
            "82f4b931bbbbbbbb": ("A Maintainer", _DEPENDABOT_SQUASH),
        },
    )
    assert commit_lint.main(["--range", "base..head"]) == 1


def test_a_bot_name_written_into_a_message_waives_nothing(tmp_path: Path) -> None:
    """The hook has no author to read, so the message cannot claim one."""
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(f"{_DEPENDABOT_SQUASH}\ndependabot[bot]\n", encoding="utf-8")
    assert commit_lint.main(["--message-file", str(path)]) == 1


def test_dependabot_prefixes_conform_to_the_house_envelope() -> None:
    """Left to infer, Dependabot writes subjects this linter rejects.

    It reads the prefix and the capitalisation off recent history, which
    produced both forms above. Pinning `commit-message.prefix` is what
    keeps the bot inside the envelope; the author exemption is the
    backstop for the generated body it cannot reshape.
    """
    import yaml

    config_path = Path(__file__).resolve().parents[1] / ".github" / "dependabot.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    updaters = config["updates"]
    assert updaters, "dependabot.yml declares no updaters"
    for updater in updaters:
        ecosystem = updater["package-ecosystem"]
        message = updater.get("commit-message")
        assert message is not None, f"{ecosystem} updater pins no commit-message"
        prefixes = [message["prefix"]]
        if "prefix-development" in message:
            prefixes.append(message["prefix-development"])
        for prefix in prefixes:
            subject = f"{prefix}: bump the actions group with 2 updates"
            assert rules(subject) == set(), subject


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
