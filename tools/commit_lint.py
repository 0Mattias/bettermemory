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

The rules here are deliberately the MECHANICAL subset of the style. Most
of tone is a judgement a linter cannot make, so it lives in prose in
CONTRIBUTING.md and is enforced by review. What is encoded here is only
what can be decided without taste: the Conventional Commits envelope,
subject shape and length, the blank line before the body, body wrapping,
and the four categories of wording that are objectively out of place in a
project's permanent record — first-person narration, conversational
filler, references to the authoring session, and editorialising about the
project's own past mistakes.

Two escapes exist, both narrow. A commit git attributes to a bot is not
graded in ``--range`` mode at all (``_is_bot_author``): its subject and
body come from a template, so grading them fails a push on rules the
author cannot satisfy. And a message may waive one NAMED wording rule
with a ``Lint-skip:`` trailer (``_waived_rules``), which stays in the
permanent record where review sees it.

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
# version-bump commits (`release: 3.36.0`) predate this linter and are a
# real, recurring category, graded by `_RELEASE_DESCRIPTION_RE` below.
# `bench` likewise — the benchmark harnesses under bench/ are neither src
# nor tests.
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
#
# The three phrase rules below all bound with `(?<![\w\-])` / `(?![\w\-])`
# rather than a bare `(?<!\w)` / `(?!\w)`. A hyphen is not a word
# character, so the bare form matches inside a compound: `this round-trip`
# is a serialization property, `this window-local cache` and `the previous
# sweep-and-prune pass` name code, and `embarrassing-looking` is a
# hyphenated adjective — none of them is the phrase the rule bans. The
# apostrophe is deliberately NOT excluded here, unlike in
# `_FIRST_PERSON_RE`: `this session's episodes` is the possessive form of
# exactly the narration `_SESSION_RE` exists to catch.
_FILLER_RE = re.compile(
    r"(?<![\w\-])(?:"
    r"turns out|as it turns out|it turns out"
    r"|honestly|frankly|admittedly|obviously|clearly enough"
    r"|sadly|happily|thankfully|unfortunately for us"
    r"|oops|whoops|ugh|yay|hooray|alas|phew"
    r"|nobody reads|no one reads"
    r"|as promised|as threatened|finally!"
    r")(?![\w\-])",
    re.IGNORECASE,
)

# References to the authoring session. A commit is read years later by
# someone bisecting, who has no access to the sitting that produced it:
# "the last round", "this window", "an adversarial pass over the previous
# six commits" name a boundary that exists only in the author's calendar.
# The facts they gesture at — which commit, which repair, which claim —
# are all citable by sha, and the rule is that they must be. The
# determiners are enumerated rather than left open (`the current window`
# is not here) because this project has real time windows in its domain:
# demotion windows, verification windows, tag windows. The enumeration is
# not sufficient on its own — a body that closes a file-mode race writes
# `to close this window` about a window in the code — so `Lint-skip:
# session-narration` below is the escape for the residual ambiguity.
_SESSION_RE = re.compile(
    r"(?<![\w\-])(?:"
    r"(?:this|the last|the previous|the preceding)\s+(?:session|round|sweep|window)"
    r"|(?:last|previous)\s+(?:session|round)"
    r"|adversarial pass|audit pass"
    r"|earlier (?:today|in this session)|as of this writing"
    r")(?![\w\-])",
    re.IGNORECASE,
)

# Editorialising about the project's own record. Distinct from `_FILLER_RE`
# above, which covers conversational tics: these are phrases that grade a
# defect rather than describe it. A permanent record states what the code
# did and what it does now; that a maintainer found it embarrassing, or
# that it is the third occurrence, changes nothing a reader can act on and
# reads as apology rather than engineering. Anything a reader WOULD act on
# survives the rule — an incident path, a sha, a recurrence named as a
# defect class, a lesson cited from docs/incidents/ — because those are
# facts, not gradings.
_EDITORIAL_RE = re.compile(
    r"(?<![\w\-])(?:"
    r"to be honest|being honest|in all honesty|if we(?:'re| are) honest"
    r"|worth (?:stating|saying|admitting|confessing)(?:\s+(?:plainly|outright|here))?"
    r"|needless to say|it (?:should|must|has to) be said|let it be said"
    r"|mea culpa|embarrassing(?:ly)?|shameful(?:ly)?|humiliating"
    r"|cheerfully|blithely|merrily|gleefully|smugly"
    r"|for the (?:second|third|fourth|fifth|umpteenth) time"
    r"|which was the whole point|that is the whole point"
    r")(?![\w\-])",
    re.IGNORECASE,
)

# The four wording rules, in report order: `(rule name, pattern, advice)`.
_WORDING_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("first-person", _FIRST_PERSON_RE, "describe the change, not the author"),
    ("filler", _FILLER_RE, "carries no information about the change"),
    (
        "session-narration",
        _SESSION_RE,
        "the reader has no access to the authoring session; cite the commit by sha",
    ),
    (
        "editorial",
        _EDITORIAL_RE,
        "grade the defect by describing it, not by commenting on it",
    ),
)

# The wording rules are the judgement-adjacent half of this linter, and a
# judgement can be wrong: `this window` is session narration in one body
# and the name of a real race window in the next, and no pattern can tell
# those apart. A message may therefore waive a NAMED wording rule with a
# `Lint-skip:` trailer. Only the four rules above are waivable and only by
# name — there is no wildcard, and naming an envelope rule (subject shape,
# length, the blank line, body wrapping) is itself a violation, because
# those are mechanical and carry no ambiguity to escape. The waiver stays
# in the permanent record, where review sees it.
_LINT_SKIP_RE = re.compile(
    r"^Lint-skip:[ \t]*(?P<rules>.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_WAIVABLE_RULES = frozenset(rule for rule, _, _ in _WORDING_RULES)

# A `release:` subject carries the version and nothing else. The version
# is the index entry a reader scans for; a parenthetical thesis beside it
# ("release: 3.36.0 (a green light is only worth what it measured)") is a
# headline, and the release's argument belongs in the CHANGELOG entry the
# tag points at, where it has room to cite. Any trailing metadata a
# version legitimately carries — `3.36.0rc1`, `3.36.0+local` — is
# admitted; whitespace and punctuation are not.
_RELEASE_DESCRIPTION_RE = re.compile(r"^\d+\.\d+\.\d+[0-9A-Za-z.+-]*$")

# Emoji and other pictographs. The subject line is read in tooling that
# does not render them consistently.
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]"
)

# Trailers are exempt from the wording rules: a `Co-Authored-By:` line
# naming a person is not first-person narration, and its value is a name
# and an address rather than prose anyone chose. The keys are enumerated
# rather than matched by shape, because `<capitalised word>: <anything>`
# also describes an ordinary labelled body line — `Note:`, `Tests:`,
# `Gate:` — and exempting those turns the wording rules off for the rest
# of the line. `Lint-skip` is here so the waiver line is not itself graded.
# Keys are matched case-insensitively, as git matches them.
_TRAILER_KEYS = (
    "Acked-by",
    "Cc",
    "Closes",
    "Co-Authored-By",
    "Fixes",
    "Lint-skip",
    "Refs",
    "Reported-by",
    "Reviewed-by",
    "Signed-off-by",
    "Suggested-by",
    "Tested-by",
)
_TRAILER_RE = re.compile(
    r"^(?:" + "|".join(re.escape(key) for key in _TRAILER_KEYS) + r"): .+$",
    re.IGNORECASE,
)

# Quoted material is exempt from the wording rules. A commit that records
# what a user asked for, or the literal query a search was run with, has
# to be able to reproduce their words: `memory_search("how do I cut a
# release")` is a citation, not the author narrating. Backticks and
# double quotes both count; the span is blanked (not deleted) so the rest
# of the line keeps its word boundaries.
_QUOTED_RE = re.compile(r"`[^`]*`|\"[^\"]*\"|“[^”]*”")

# A comment line in a message file. `git commit` with an editor runs
# `--cleanup=default`, which strips these before the message is stored, so
# the hook must strip them too or it grades text that will never be
# committed. `git commit -m`/`-F` run `--cleanup=whitespace` instead and
# keep them, which is why `--range` mode grades a stored message as-is —
# see `lint_message`.
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
    """Drop what ``git commit`` drops from an editor buffer.

    Mirrors ``--cleanup=default`` so the ``commit-msg`` hook grades the
    text that will be recorded rather than the scaffolding around it.
    Without the scissors handling, ``git commit -v`` would feed the entire
    staged diff into the body rules and fail on every long source line in
    the patch. Applies to the hook only: ``--cleanup=whitespace``, which
    is what ``git commit -m`` and ``-F`` use, keeps ``#`` lines, so a
    stored message must be graded exactly as it was stored.
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

    if match.group("type") == "release" and not _RELEASE_DESCRIPTION_RE.match(
        description
    ):
        out.append(
            Violation(
                where,
                "release-subject",
                f"a release subject is the version alone — got {description!r}; "
                "the release's argument goes in its CHANGELOG entry",
            )
        )

    if _EMOJI_RE.search(subject):
        out.append(Violation(where, "subject-emoji", "subject contains an emoji"))

    return out


def _wording_paragraphs(text: str) -> list[str]:
    """The units the wording rules grade: runs of gradeable lines.

    A paragraph ends at a blank line or a trailer. Grading a paragraph
    rather than a line is what lets a banned phrase be caught when the
    author's wrap point falls inside it — ``the last\\nwindow`` is the
    same phrase as ``the last window``, and every rule but
    ``first-person`` carries multi-word phrases, so a gate that graded
    physical lines would let one through on where a line happened to
    break. A blank line is a real break rather than a wrap, so paragraphs
    are not joined across one.
    """
    paragraphs: list[str] = []
    run: list[str] = []
    for line in text.splitlines():
        if not line.strip() or _TRAILER_RE.match(line):
            if run:
                paragraphs.append("\n".join(run))
                run = []
            continue
        run.append(line)
    if run:
        paragraphs.append("\n".join(run))
    return paragraphs


def _check_wording(text: str, where: str, waived: frozenset[str]) -> list[Violation]:
    out: list[Violation] = []
    for paragraph in _wording_paragraphs(text):
        # Newline to space, and quoted spans blanked rather than dropped:
        # both substitutions preserve length, so every offset in `graded`
        # still indexes `paragraph` and a match can be attributed back to
        # the physical lines it spans.
        graded = _QUOTED_RE.sub(
            lambda m: " " * len(m.group(0)), paragraph.replace("\n", " ")
        )
        lines = paragraph.split("\n")
        for rule, pattern, advice in _WORDING_RULES:
            if rule in waived:
                continue
            match = pattern.search(graded)
            if match is None:
                continue
            first = paragraph.count("\n", 0, match.start())
            last = paragraph.count("\n", 0, match.end())
            source = " ".join(line.strip() for line in lines[first : last + 1])
            out.append(
                Violation(
                    where,
                    rule,
                    f"{match.group(0)!r} in {source!r} — {advice}",
                )
            )
    return out


def _waived_rules(text: str, where: str) -> tuple[frozenset[str], list[Violation]]:
    """Wording rules a ``Lint-skip:`` trailer waives, and its own faults.

    A name outside `_WAIVABLE_RULES` is reported rather than ignored, so a
    typo or an attempt to waive the envelope fails closed: the message
    still carries whatever the named rule would have caught, plus a
    violation for the waiver itself.
    """
    waived: set[str] = set()
    out: list[Violation] = []
    for match in _LINT_SKIP_RE.finditer(text):
        for name in re.split(r"[,\s]+", match.group("rules")):
            if not name:
                continue
            if name in _WAIVABLE_RULES:
                waived.add(name)
            else:
                out.append(
                    Violation(
                        where,
                        "lint-skip",
                        f"{name!r} is not a waivable rule — one of "
                        f"{', '.join(sorted(_WAIVABLE_RULES))}",
                    )
                )
    return frozenset(waived), out


def lint_message(raw: str, where: str, *, strip: bool = True) -> list[Violation]:
    """Grade one commit message. Returns every violation it carries.

    ``strip`` drops ``#`` comment lines and the ``git commit -v`` scissors
    block. That is right for the ``commit-msg`` hook, whose input is an
    editor buffer git has not cleaned yet, and wrong for ``--range``,
    whose input is a message git has already stored: ``git commit -m`` and
    ``-F`` run ``--cleanup=whitespace``, which keeps ``#`` lines, so
    stripping there would delete text the commit really carries and grade
    a message nobody wrote.
    """
    text = strip_comments(raw) if strip else raw.strip("\n")
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

    waived, waiver_faults = _waived_rules(text, where)
    out.extend(waiver_faults)
    out.extend(_check_wording(text, where, waived))
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


def _author_for(sha: str) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--format=%an", sha],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _is_bot_author(name: str) -> bool:
    """True for a commit GitHub attributes to an app rather than a person.

    GitHub names an app's commits ``<app>[bot]`` — ``dependabot[bot]``,
    ``github-actions[bot]``, ``renovate[bot]``. A bot composes its subject
    and body from a template, so grading them fails a push on rules the
    commit's author cannot satisfy by editing anything: Dependabot's
    generated ``Bumps the <group> group with N updates: [a](url), [b](url)
    …`` body line is past the wrap limit and is not configurable at all.

    The identity comes from git's author field, which only ``--range``
    mode has; the ``commit-msg`` hook grades a message with no author
    attached, so nothing written inside a message can reach this
    exemption. Reaching it as a person means setting ``user.name`` to
    ``…[bot]``, which mislabels the commit's authorship in permanent
    history rather than quietly bypassing a lint.
    """
    return name.strip().endswith("[bot]")


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
            if _is_bot_author(_author_for(sha)):
                continue
            violations.extend(lint_message(_message_for(sha), sha[:8], strip=False))

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
