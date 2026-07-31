#!/usr/bin/env python3
"""Lint commit messages against the house style in CONTRIBUTING.md.

Two entry points, one rule set:

* ``--message-file <path>`` — lint a single message. This is the
  ``commit-msg`` hook's mode (``.githooks/commit-msg``).
* ``--range <A>..<B>`` — lint every non-merge commit in a git range. This
  is the CI mode; the ``commit-lint`` job in ``.github/workflows/ci.yml``
  passes the push or pull-request range so only *new* commits are graded.
  Pre-existing history is never re-linted: the commits written before this
  linter existed were written under the old, looser guidance, and grading
  them would fail every run forever over something nobody can fix.

The rules here are deliberately the MECHANICAL subset of the style. Tone —
"describe the change, do not narrate the session" — is a judgement a linter
cannot make, so it lives in prose in CONTRIBUTING.md and is enforced by
review. What is encoded here is only what can be decided without taste:
the Conventional Commits envelope, subject shape and length, the blank
line before the body, body wrapping, and the two categories of wording
that are objectively out of place in a project's permanent record —
first-person narration and conversational filler.

Exit status is 0 when every message passes and 1 otherwise, with each
violation printed as ``<sha-or-file>: <rule>: <detail>``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass


# Conventional Commits types this project uses. `release` is ours: the
# version-bump commits (`release: 3.32.0 (...)`) predate this linter and
# are a real, recurring category. `bench` likewise — the benchmark
# harnesses under bench/ are neither src nor tests.
TYPES = (
    "bench",
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "release",
    "revert",
    "style",
    "test",
)

# `type(scope)!: description`. The scope is optional; `!` marks a breaking
# change. The description is captured so the subject rules below can grade
# it without re-parsing.
_SUBJECT_RE = re.compile(
    r"^(?P<type>" + "|".join(TYPES) + r")"
    r"(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?"
    r"(?P<breaking>!)?"
    r": (?P<description>.+)$"
)

# 72 keeps `git log --oneline` readable in an 80-column terminal after the
# 8-char abbreviated sha and a space.
MAX_SUBJECT = 72

# Body wrapping. 100 rather than 72 because this project's bodies carry
# file paths, symbol names and small tables that read worse when hard-
# wrapped mid-token; see `_body_line_is_exempt` for what is excused.
MAX_BODY_LINE = 100

# First-person narration. The commit log is a record of what changed, not
# of who changed it or how they felt about it — "I do not yet know why"
# belongs in an issue, not in permanent history. `I` is matched
# case-sensitively and with `/` and `-` excluded on both sides, so `I/O`
# and `IOError` and a bare lowercase `i` are untouched; the possessives
# are matched case-insensitively. `we`/`our` are deliberately NOT here:
# "we" reads as the project rather than a person, and banning it produces
# contorted prose in rationale paragraphs.
_FIRST_PERSON_RE = re.compile(
    r"(?<![\w'/\-])(?:I|I'm|I'd|I've|I'll)(?![\w'/\-])"
    r"|(?<![\w'])(?:my|mine|myself)(?![\w'])",
)

# Conversational filler and editorialising. Narrow on purpose: every entry
# here is a phrase that carries no information about the change. Anything
# arguable (a metaphor, a wry aside) is left to review rather than
# encoded, because a false positive here blocks a push.
_FILLER_RE = re.compile(
    r"(?<!\w)(?:"
    r"turns out|as it turns out|it turns out"
    r"|honestly|frankly|admittedly|obviously|clearly enough"
    r"|sadly|happily|thankfully|unfortunately for us"
    r"|oops|whoops|ugh|yay|hooray|alas|phew"
    r"|nobody reads|no one reads"
    r"|as promised|as threatened|finally!"
    r")(?!\w)",
    re.IGNORECASE,
)

# Emoji and other pictographs. The subject line is read in tooling that
# does not render them consistently.
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]"
)

# Trailers and machine-read lines are exempt from the wording rules: a
# `Co-Authored-By:` line naming a person is not first-person narration,
# and a URL is not filler.
_TRAILER_RE = re.compile(r"^[A-Z][A-Za-z-]+: .+$")

# Quoted material is exempt from the wording rules. A commit that records
# what a user asked for, or the literal query a search was run with, has
# to be able to reproduce their words: `memory_search("how do I cut a
# release")` is a citation, not the author narrating. Backticks and
# double quotes both count; the span is blanked (not deleted) so the rest
# of the line keeps its word boundaries.
_QUOTED_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|“[^”]*”")

# A comment line in a message file. `git commit` strips these before the
# message is stored, so the hook must strip them too or it grades text
# that will never be committed.
_COMMENT_PREFIX = "#"

# `git commit -v` appends the staged diff below this marker. Everything
# from here down is stripped by git and must be stripped here as well.
_SCISSORS = "# ------------------------ >8 ------------------------"


@dataclass(frozen=True)
class Violation:
    """One broken rule, attributed to one commit."""

    where: str
    rule: str
    detail: str

    def render(self) -> str:
        return f"{self.where}: {self.rule}: {self.detail}"


def strip_comments(raw: str) -> str:
    """Drop what ``git commit`` drops: comments and the verbose diff.

    Mirrors git's own handling so the hook grades exactly the text that
    will be recorded. Without the scissors handling, ``git commit -v``
    would feed the entire staged diff into the body rules and fail on
    every long source line in the patch.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        if line.rstrip() == _SCISSORS:
            break
        if line.startswith(_COMMENT_PREFIX):
            continue
        lines.append(line)
    return "\n".join(lines).strip("\n")


def _body_line_is_exempt(line: str) -> bool:
    """True when a long body line is legitimately unwrappable.

    Three cases, all of which read worse when broken: a line with no
    whitespace at all past the limit (a URL, a long path, a dotted symbol
    name), a table row, and an indented block — code samples, tracebacks
    and command transcripts are pasted verbatim and re-wrapping them
    corrupts them.
    """
    if line.startswith((" ", "\t")):
        return True
    if line.lstrip().startswith("|") or line.lstrip().startswith("```"):
        return True
    return " " not in line[MAX_BODY_LINE:]


def _check_subject(subject: str, where: str) -> list[Violation]:
    out: list[Violation] = []

    if len(subject) > MAX_SUBJECT:
        out.append(
            Violation(
                where,
                "subject-length",
                f"{len(subject)} chars, limit {MAX_SUBJECT} — {subject[:MAX_SUBJECT]}…",
            )
        )

    match = _SUBJECT_RE.match(subject)
    if match is None:
        out.append(
            Violation(
                where,
                "subject-format",
                "expected `type(scope): description` with type one of "
                f"{', '.join(TYPES)} — got {subject!r}",
            )
        )
        # Everything below grades the description, which we could not
        # parse out. Stop here rather than emit cascading noise.
        return out

    description = match.group("description")

    if description[:1].isupper() and not description.split(" ", 1)[0].isupper():
        # Allowed: an all-caps token like `CI` or `README` opening the
        # description. Rejected: sentence-cased prose, which is
        # inconsistent with the rest of the log.
        out.append(
            Violation(
                where,
                "subject-case",
                f"description should start lowercase — got {description!r}",
            )
        )

    if description.endswith("."):
        out.append(Violation(where, "subject-period", "description ends with a period"))

    if _EMOJI_RE.search(subject):
        out.append(Violation(where, "subject-emoji", "subject contains an emoji"))

    return out


def _check_wording(text: str, where: str) -> list[Violation]:
    out: list[Violation] = []
    for line in text.splitlines():
        if _TRAILER_RE.match(line):
            continue
        # Blank out quoted spans rather than dropping them, so a word that
        # abuts a quote keeps its boundary and the reported line still
        # reads as the author wrote it.
        graded = _QUOTED_RE.sub(lambda m: " " * len(m.group(0)), line)
        first_person = _FIRST_PERSON_RE.search(graded)
        if first_person is not None:
            out.append(
                Violation(
                    where,
                    "first-person",
                    f"{first_person.group(0)!r} in {line.strip()!r} — describe "
                    "the change, not the author",
                )
            )
        filler = _FILLER_RE.search(graded)
        if filler is not None:
            out.append(
                Violation(
                    where,
                    "filler",
                    f"{filler.group(0)!r} in {line.strip()!r} — carries no "
                    "information about the change",
                )
            )
    return out


def lint_message(raw: str, where: str) -> list[Violation]:
    """Grade one commit message. Returns every violation it carries."""
    text = strip_comments(raw)
    if not text.strip():
        return [Violation(where, "empty", "commit message is empty")]

    # A revert or a merge produced by git itself is exempt: their subjects
    # are generated and reverting is more important than the envelope.
    if text.startswith("Revert ") or text.startswith("Merge "):
        return []

    lines = text.splitlines()
    out = _check_subject(lines[0], where)

    if len(lines) > 1 and lines[1].strip():
        out.append(
            Violation(
                where,
                "body-separator",
                "subject and body must be separated by a blank line",
            )
        )

    for i, line in enumerate(lines[1:], start=2):
        if len(line) > MAX_BODY_LINE and not _body_line_is_exempt(line):
            out.append(
                Violation(
                    where,
                    "body-length",
                    f"line {i} is {len(line)} chars, limit {MAX_BODY_LINE}",
                )
            )

    out.extend(_check_wording(text, where))
    return out


def _commits_in_range(rev_range: str) -> list[str]:
    """SHAs in ``rev_range``, merges excluded.

    Merge commits carry generated subjects and their parents are graded
    on their own commits, so grading the merge itself is noise.
    """
    result = subprocess.run(
        ["git", "rev-list", "--no-merges", rev_range],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _message_for(sha: str) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%B", sha],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--message-file",
        help="path to a file holding one commit message (commit-msg hook mode)",
    )
    group.add_argument(
        "--range",
        dest="rev_range",
        help="git revision range; every non-merge commit in it is linted",
    )
    args = parser.parse_args(argv)

    violations: list[Violation] = []
    if args.message_file:
        with open(args.message_file, encoding="utf-8") as handle:
            violations = lint_message(handle.read(), args.message_file)
    else:
        try:
            shas = _commits_in_range(args.rev_range)
        except subprocess.CalledProcessError as exc:
            # An unresolvable range (a force push, a first push where the
            # `before` sha is all zeroes, a shallow clone) must not be
            # reported as a style failure — that would block a push for a
            # reason the author cannot fix by editing their message.
            print(
                f"commit-lint: cannot resolve range {args.rev_range!r} "
                f"({exc.stderr.strip()}); nothing linted",
                file=sys.stderr,
            )
            return 0
        for sha in shas:
            violations.extend(lint_message(_message_for(sha), sha[:8]))

    for violation in violations:
        print(violation.render())

    if violations:
        print(
            f"\n{len(violations)} violation(s). "
            "See the 'Commit messages' section of CONTRIBUTING.md.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
