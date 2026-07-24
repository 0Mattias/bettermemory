"""Mechanical pin on *checkable* claims made by shipped prose.

Seventeen separate "falsified claim" defects have been found in this
repo — a CHANGELOG line, docstring, or doc paragraph asserting something
the code does not do. Several were written *by the repairs fixing
earlier ones*. Every fix so far closed an instance; nothing closed the
class. This module closes the mechanically-decidable slice of it, so the
next false claim fails CI instead of shipping.

Design bias: **a checker with false positives gets disabled, and a
disabled checker protects nothing.** Every rule below was run against
the real corpus at HEAD and tightened until its misfires were zero. Where
a claim shape is ambiguous in English, it is dropped rather than guessed.
Coverage is deliberately narrow; its value is what it catches tomorrow.

What is checked
---------------
1. ``path`` — a backtick-quoted token anchored at a repo-relative prefix
   (``src/`` ``docs/`` ``tests/`` ``bench/`` ``examples/`` ``plugin/``
   ``.github/``) with a source-file suffix must exist on disk.
2. ``symbol`` — ``` `name` in `module.py` ``` must resolve inside that
   module by AST (any binding: def, class, assignment, parameter,
   attribute, import).
3. ``test-count`` — "the N tests in ``tests/x.py``" / "``tests/x.py``
   contains N tests" must match the count of test functions found by
   AST. Digits and English number words ("Nine") both parse.
4. ``line-ref`` — a ``file.py:NNN`` citation must land in range, and the
   cited region must actually contain one of the code identifiers the
   surrounding paragraph attributes to it. Both halves apply to the
   markdown-linked and bare-backticked forms alike. When the paragraph
   names no identifier that exists in the target at all, there is no
   anchor to judge against and the rule stays quiet. A citation the
   nearby prose marks as non-resolving is quoted evidence rather than
   an assertion and is skipped — see the deliberately-not-checked list.
5. ``file-count`` — "N files are named ``x.py``" must match how many files
   the repo scan actually resolves for that name.

Sources are scoped by *rot rate*, which is the core design decision
-------------------------------------------------------------------
CHANGELOG.md is a frozen historical record. An entry that was accurate
when written is not a lie later, and rewriting shipped release notes to
appease a linter is worse than the drift. So sources are tiered by how
fast each claim shape decays:

* ``path`` and ``symbol`` claims are checked **everywhere**, changelog
  included. Both decay slowly enough to be worth pinning across history:
  measured at HEAD, the changelog carries 177 checked path tokens with 2
  stale (~1%) and 16 symbol claims with 1 stale (~6%). A handful of
  allowlist entries to cover the entire release history is a price worth
  paying for full-history coverage.
* ``test-count`` and ``line-ref`` claims are checked in **living
  documents only** (README.md, docs/*.md). Test counts and line numbers
  rot mechanically with every refactor — pinning them against frozen
  release notes would generate permanent allowlist churn and teach
  everyone to ignore this file.
* ``file-count`` claims are checked everywhere **except** the changelog,
  for the same reason: the count is a property of the tree as it stands
  now, so a frozen release note that was right when written would drift
  into permanent allowlist churn.

On src/ and tests/ docstrings: INCLUDED
---------------------------------------
Decided by measurement, not taste — but the measurement is deliberately
not written down here. A standing total ("across all N docstrings…") is
precisely the rot-prone shape this module exists to catch, and it lands
in this module's own blind spot: ``check_test_counts`` and
``check_line_refs`` never run against Python sources at all, and the one
counting shape that does, ``file-count``, matches phrasings about
*files*, not about docstrings. A docstring total is therefore
unfalsifiable here by construction. An earlier revision of this section
stated three of them. All three were wrong by HEAD, two were already
wrong on the day they were typed, and nothing in CI could notice.

The property that justified including them, and that survives any number
of docstrings being added:

* Only three shapes are run against Python sources at all — ``path``,
  ``symbol`` and ``file-count`` (see ``collect_failures``) — and
  docstrings are dense prose but sparse in even those, so extending the
  corpus this way is cheap.
* It costs nothing in exemptions. Every misfire found in a docstring —
  an illustrative ``docs/y.md``, builder.py's past-tense
  "``_register_tools`` lived in ``server.py``" — was answered with an
  extractor rule, never an allowlist entry. That is the load-bearing
  half, so it is derived rather than asserted from memory:
  ``test_no_allowlist_entry_covers_a_docstring_source`` fails the moment
  a docstring needs exempting.

Only docstrings are read, never statement bodies — the self-tests below are
built from deliberately invalid paths and symbols that exist precisely
to be rejected, so scanning bodies would misfire by construction.
**Corollary for anyone editing this file: keep synthetic examples in
code, not in docstrings.** Every rule here now applies to this file's
own prose, and the extractors do not know that a quoted counter-example
is only being discussed.

The mirror-image trap is the ``#`` comment. Only docstrings are read from
``.py`` sources, so a false example parked in a comment is invisible to
every rule here — and an example is exactly where a reader's scepticism
slides off. **Illustrative prose in this file is held to the same
standard as its assertions:** an example is either an obvious shape
(``N``, ``x.py``) or a fact checked against the tree, never a
plausible-looking number nobody counted.

Scanning ``tests/`` matters because this file is itself shipped prose,
and its first commit miscounted the files carrying the name
``verify.py`` — asserting three where the repo holds two. Excluding
``tests/`` had made the guard structurally unable to audit its own
docstrings.

The honest caveat, in two parts:

* Extending the corpus to ``tests/`` would *not* by itself have caught
  that defect — "N files are named X" was not a checked shape in any
  source. That is why the ``file-count`` rule exists; the corpus fix and
  the shape fix each close half of it. Verified: the rule fires on the
  original wording and passes the corrected wording.
* The docstring instances that actually shipped false elsewhere were
  *semantic* ("this returns X", "the lock is held here"). No regex
  decides those, so scanning docstrings would not have caught them.
  Those claims remain uncovered, here as everywhere.

What is deliberately NOT checked
--------------------------------
* **Semantic claims.** "This is O(1)", "the lock is held across the
  write". Not mechanically decidable; the honest answer is a human
  reviewer, not a fragile heuristic.
* **Past-tense relocation prose.** "``_register_tools`` lived in
  ``server.py``" is a true statement about history. Tense markers near a
  symbol claim suppress it (``_RELOCATION_PROSE``).
* **Citations a document quotes to say they do not resolve.** The swarm
  plan's errata quote their own rotten ``file.py:NNN`` forms as the
  evidence under analysis, pinning each resolution to a named commit;
  range- and anchor-checking such a quote against HEAD fails the prose
  precisely when it is right about the code. A citation with a
  non-resolving verdict nearby — ``_NONRESOLVING_PROSE`` within
  ``_NONRESOLVING_WINDOW`` characters, either side, both citation
  shapes — is quoted evidence, not an assertion. The window bound is
  what keeps this from becoming a paragraph-wide pass; the self-tests
  pin both directions.
* **Ambiguous module references, when any reading satisfies them.**
  Two files are named ``verify.py``. A claim is reported only when it
  fails against *every* candidate — see ``_resolve_modules``.
* **Bare "N tests in `x.py`" without a total-marking determiner.**
  English does not distinguish "the N tests in X" (total, checkable)
  from "N tests in X" (a subset, uncheckable). Real corpus instances of
  both exist. Only total-marked forms are accepted; see ``_TESTCOUNT_*``.
* **Counts against parametrized test files.** ``@pytest.mark.parametrize``
  makes function count and collected count differ, so "N tests" is
  ambiguous. Such files are skipped outright.
* **Commit messages.** Several real instances lived there, but they are
  immutable and not shipped prose. Out of scope by definition.
* **Planning documents' path claims.** ``docs/ROADMAP.md`` and the
  ``*-plan.md`` files propose files that do not exist yet — that is what
  a plan is. Their line-refs and symbol claims *are* checked, since those
  cite current code.
* **Placeholder paths.** ``src/mod.py``, ``docs/spec.md``, ``src/x.py``
  are syntax examples, not assertions of existence. Stems in
  ``_PLACEHOLDER_STEMS`` are skipped. This is an extractor rule, not an
  allowlist entry, because these are permanent by intent and an allowlist
  entry could never be retired.
* **Statement bodies in ``src/`` and ``tests/``.** Only docstrings are
  read from Python sources. Comments and string literals are not prose
  the project ships, and test bodies are synthetic by design.
* **Counting prose outside the pinned phrasings.** ``file-count`` matches
  "N files are named ``x.py``" and the elided "N are named ``x.py``".
  "``x.py`` names three files" says the same thing and is *not* matched.
  Rather than chase English, prefer the pinned phrasing when writing a
  count about this repo — an unmatched sentence is an unchecked claim.

How the ratchet works
---------------------
``_ALLOWLIST`` holds claims already false at HEAD which this test may not
fix (they live in files owned by other concurrent work, or in frozen
history). It is a ratchet, not a suppression, enforced by two paired
tests:

* ``test_no_unexpected_false_claims`` fails on any false claim **not**
  in the allowlist — the forward guard.
* ``test_allowlist_has_no_stale_entries`` fails on any allowlist entry
  that no longer corresponds to a real failure — the reverse guard. Fix
  a claim and this test tells you to delete its entry.

The reverse guard is the part that matters. Without it an allowlist
silently becomes permanent, which is how this kind of check normally
dies. Entries are keyed by (source, kind, subject) and never by line
number, so editing prose above a claim does not rot the list.

Self-tests at the bottom of this file feed synthetic prose through the
same extractors to prove each rule actually fires, and that each
precision guard actually suppresses. A checker whose rules are all
currently satisfied is indistinguishable from a checker that does
nothing, so those tests are load-bearing, not decorative.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Every filesystem access in this module resolves from `_REPO_ROOT`, never
# from the process CWD, so `pytest tests/` and `pytest` from a subdirectory
# see the same corpus.
_EVENTS_MODULE = "src/bettermemory/events.py"

_CHANGELOG = "CHANGELOG.md"
_PLAN_DOCS = frozenset(
    {
        "docs/ROADMAP.md",
        "docs/swarm-convergence-plan.md",
        "docs/v1.6-plan.md",
    }
)

# Conventional stand-in stems. `src/mod.py:42` in a sentence explaining
# citation syntax is not a claim that src/mod.py exists.
_PLACEHOLDER_STEMS = frozenset(
    {"x", "y", "z", "n", "mod", "spec", "foo", "bar", "baz", "qux", "example", "sample"}
)

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_NUM = "|".join([*_NUMBER_WORDS, r"\d{1,4}"])

# Qualifiers that turn a total into a delta: "9 *new* tests in X" says
# nothing about how many tests X holds.
_DELTA_QUALIFIER = re.compile(
    r"\b(new|additional|more|further|other|remaining|extra)\b", re.I
)

_PATH_PREFIXES = (
    "src/",
    "docs/",
    "tests/",
    "bench/",
    "examples/",
    "plugin/",
    ".github/",
)
_PATH_SUFFIX = re.compile(r"\.(py|md|toml|json|yml|yaml|txt|cfg|jsonl|sh)$")
_BACKTICK = re.compile(r"`{1,2}([^`\n]{1,160})`{1,2}")
_ILLUSTRATIVE_CUE = re.compile(
    r"\b(?:like|e\.g\.|eg\.|such as|for example|for instance)\W{0,3}$", re.I
)

# Prose that places a symbol somewhere it USED to be. "``_register_tools``
# lived in ``server.py``" is a true statement about history, not a claim
# that the symbol is there now. Checked against the match and a short
# lookback, since the tense marker often sits ahead of the symbol
# ("Pre-Round-3 `build_server` and `_register_tools` lived in ...").
_RELOCATION_PROSE = re.compile(
    r"\b(lived|moved|used to|previously|formerly|no longer|was|were|removed"
    r"|dropped|renamed|once|before|pre-\w+|until|old|former)\b",
    re.I,
)
_RELOCATION_LOOKBACK = 60

# Prose that quotes a `file.py:NNN` citation in order to say it does NOT
# resolve — an erratum analysing its own rotten line number. "…lands
# outside every function and class body, nowhere near the shard pick it
# was cited for" is evidence under discussion, not an assertion that the
# citation holds, and checking the quote against HEAD fails the prose
# precisely when it is right. Verdict phrasings only — words that pass
# judgement on a citation qua citation — so an ordinary wrong citation in
# live prose stays checked. Multi-word markers use `\s+` because markdown
# wraps mid-phrase. Proximity is bounded by `_NONRESOLVING_WINDOW` on
# both sides so a verdict on one citation cannot exempt a paragraph.
_NONRESOLVING_PROSE = re.compile(
    r"\b(?:do(?:es)?|did)\s+not\s+(?:point|resolve|land|sit)\b"
    r"|\bpoints?\s+at\s+prose\b"
    r"|\blands?\s+outside\b"
    r"|\bnowhere\s+near\b"
    r"|\bstraddles\b"
    r"|\bshort\s+of\s+the\b"
    r"|\bwrong\s+(?:when\s+written|on\s+arrival)\b"
    r"|\balready\s+(?:false|wrong|moved)\b"
    r"|\bnon-resolving\b"
    r"|\boriginally\s+shipped\b"
    r"|\bnarrowed\s+it\s+to\b"
    r"|\ba\s+different\s+(?:function|method|class)\b"
    r"|\brotted\b",
    re.I,
)
_NONRESOLVING_WINDOW = 120

# `symbol` [one or two plain words] in `module.py`. The interposed words
# may not contain punctuation — that keeps the match from stepping over a
# clause boundary and pairing an unrelated symbol with an unrelated file.
_SYMBOL_IN_MODULE = re.compile(
    r"`{1,2}(?P<sym>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`{1,2}"
    r"\*{0,2}(?:\s[A-Za-z][A-Za-z-]{1,14}){0,2}\s+in\s+"
    r"`{1,2}(?P<mod>(?:src/bettermemory/|tests/)?[a-z_][a-z0-9_/]*\.py)`{1,2}"
)

# "the/all/its N tests in `tests/x.py`" — a determiner marks the count as
# the file's total. Bare "N tests in X" is a subset reading; see docstring.
_TESTCOUNT_TOTAL = re.compile(
    rf"\b(?:all|the|its)\s+(?P<n>{_NUM})\s+(?:test|check)s?"
    rf"(?:\s+functions?|\s+cases?)?\s+in\s+`{{1,2}}(?P<path>tests/[\w/]+\.py)`{{1,2}}"
    rf"(?P<tail>[^.]{{0,12}})",
    re.I,
)
# "`tests/x.py` contains N test functions" — the file is the subject, so
# the count is unambiguously its total.
_TESTCOUNT_SUBJECT = re.compile(
    rf"`{{1,2}}(?P<path>tests/[\w/]+\.py)`{{1,2}}(?P<mid>[^`]{{0,120}}?)"
    rf"\b(?:contains|holds|has)\b\s+(?:only\s+|just\s+)?(?P<n>{_NUM})\s+"
    rf"(?:test|check)s?\b(?P<tail>[^.]{{0,12}})",
    re.I,
)
# A restrictive relative clause makes a count a subset rather than a total,
# and it does so in either phrasing — "has N tests that ..." and "the N tests
# in `X` that ..." are the same English. Both patterns capture a `tail` and
# both consult this, so the demotion cannot depend on which way round the
# sentence was written.
_RESTRICTIVE = re.compile(r"^\s*(that|which|covering|pinning|exercising)\b", re.I)

# Matches "N files are named `x.py`" and the elided "N are named `x.py`",
# where N is one to four digits or a word from `_NUMBER_WORDS`. Those are
# regex shapes, not counts about this repo — a comment is invisible to every
# rule in this file, so an example parked here must not read as a claim.
# Deliberately one phrasing (plus its elision) rather than a net for every
# English way of counting files — see the module docstring.
_FILECOUNT = re.compile(
    rf"\b(?P<n>{_NUM})\s+(?:files?\s+|modules?\s+)?are\s+named\s+"
    rf"`{{1,2}}(?P<name>[\w/]+\.py)`{{1,2}}",
    re.I,
)

_LINEREF_LINKED = re.compile(
    r"\[(?P<name>[\w./]+\.py):(?P<start>\d+)(?:-(?P<end>\d+))?\]\((?P<target>[^)]+)\)"
)
_LINEREF_BARE = re.compile(r"`{0,2}(?P<name>[\w/]+\.py):(?P<start>\d+)`{0,2}")
_CODE_IDENT = re.compile(r"`{1,2}([A-Za-z_][A-Za-z0-9_]*)(?:\(|`)")

# Slack for the anchor-proximity check. Generous on purpose: a citation
# that drifted a few lines during a refactor is still useful, while one
# pointing at an unrelated part of the file is the bug we want.
_ANCHOR_WINDOW = 15


@dataclass(frozen=True)
class Claim:
    """One extracted, mechanically-checkable assertion."""

    source: str
    line: int
    kind: str
    subject: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Allowlist identity — deliberately excludes ``line``."""
        return (self.source, self.kind, self.subject)


@dataclass(frozen=True)
class Failure:
    claim: Claim
    detail: str

    def __str__(self) -> str:
        return (
            f"{self.claim.source}:{self.claim.line} [{self.claim.kind}] "
            f"{self.claim.subject} — {self.detail}"
        )


# --------------------------------------------------------------------------
# Known-false claims at HEAD. Each entry says WHY it is exempt. The reverse
# guard deletes any entry that stops corresponding to a real failure.
# --------------------------------------------------------------------------
_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        _CHANGELOG,
        "path",
        "examples/memories/2025-04-15-projects-foo-stack.md",
    ): (
        "Frozen history. The example memory was later renamed "
        "foo-stack -> atlas-stack; the release note was accurate when "
        "written. Rewriting shipped release notes is worse than the drift."
    ),
    (
        _CHANGELOG,
        "path",
        "docs/blog/memory-is-rotting.md",
    ): (
        "Frozen history. A release note announced this draft post; the "
        "file was never committed (or was later removed). Genuinely "
        "false, but it is a historical entry, not a repair target."
    ),
    (
        _CHANGELOG,
        "symbol",
        "instructions in src/bettermemory/server.py",
    ): (
        "Frozen history. The MCP `instructions` block was in server.py "
        "when this entry shipped; the Round-3 wiring extraction moved it "
        "to builder.py. Accurate release note, since-refactored code."
    ),
}
# NOTE on two RETIRED entries — the swarm plan's line-ref pair,
# (docs/swarm-convergence-plan.md, line-ref, events.py:237) and
# (…, line-ref, events.py:235). Kept because the reverse guard's failure
# message points here, and because the 235 entry's history is the
# module's own cautionary tale: it was written twice. It sat here once
# before, was deleted as repaired, and was not repaired. Reconstructed
# from history rather than from the commit message that removed it:
#
#   * `fa45542` wrote the citation in markdown-LINKED form. That is the
#     form `check_line_refs` anchor-checked, and it genuinely failed —
#     on the anchor `crc32`, which the paragraph pinned to it then.
#   * `3f55d1b` rewrote the same citation into a BARE backticked
#     reference. `_LINEREF_BARE` only range-checked, and the line is
#     comfortably inside the file, so the extractor stopped matching —
#     while line 237 went on being cited for code that is not there.
#   * The checker was authored against the pre-rewrite tree and landed by
#     cherry-pick (`58b78dd`) onto the post-rewrite one, so its exemption
#     was already stale on arrival. Running that commit's checker against
#     that commit's tree reproduces it: no line-ref failure at all.
#   * `704da7c` then deleted the entry — correctly, per the reverse guard
#     — but recorded the cause as "the repair it was waiting on landed".
#     No such repair landed; the citation stayed in the doc.
#
# `test_allowlist_has_no_stale_entries` had already named both readings in
# its own failure message. The second was the true one and the first was
# written down as fact, so that message now spells out that the two causes
# need opposite responses instead of just saying "delete the entry".
#
# The retirement itself was the resolution the 235 entry queued, not a
# repair to the doc: `_NONRESOLVING_PROSE` landed, so a citation the
# surrounding prose marks as non-resolving is quoted evidence rather than
# an assertion. The doc's errata quote exactly such citations — pinned to
# named commits, with the verdict in the same sentence — so the failures
# stopped and the reverse guard forced both entries out. The 237 entry's
# reason had been written against an earlier revision of the paragraph
# (one that glossed the citation as `redact_query`'s docstring, a live
# claim); by retirement the prose marked that citation non-resolving too
# ("wrong when written", "already false of its own"), so it fell to the
# same rule.


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------
def _living_docs() -> list[tuple[str, str]]:
    """README + docs/*.md — documents expected to describe current state."""
    out: list[tuple[str, str]] = [
        ("README.md", (_REPO_ROOT / "README.md").read_text(encoding="utf-8"))
    ]
    for path in sorted(_REPO_ROOT.glob("docs/*.md")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        out.append((rel, path.read_text(encoding="utf-8")))
    return out


def _prose_sources() -> list[tuple[str, str]]:
    """Everything scanned for path/symbol claims: living docs + changelog."""
    out = _living_docs()
    out.append((_CHANGELOG, (_REPO_ROOT / _CHANGELOG).read_text(encoding="utf-8")))
    return out


def _docstrings_under(prefix: str) -> list[tuple[str, int, str]]:
    """``(relpath, first_line_of_literal, text)`` for each docstring under ``prefix``.

    Docstrings only — never statement bodies. Test modules are full of
    synthetic strings that exist precisely to be invalid, so scanning
    their bodies would misfire by construction.

    Files come from ``_all_py_files()`` rather than a glob, so this corpus
    inherits the same tracked-files discipline. A ``src/**/*.py`` glob
    would happily descend into a vendored tree parked under ``src/`` or
    ``tests/`` — and a docstring is prose, so a dependency's docstring
    could fail this repo's CI on a claim nobody here wrote.
    """
    out: list[tuple[str, int, str]] = []
    for rel in _all_py_files():
        if not rel.startswith(prefix):
            continue
        tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(
                node,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                continue
            text = ast.get_docstring(node, clean=False)
            if not text:
                continue
            first = node.body[0]
            out.append((rel, getattr(first, "lineno", 1), text))
    return out


def _code_docstrings() -> list[tuple[str, int, str]]:
    """Docstrings from both shipped source and the test suite.

    ``tests/`` is included so that this module — itself shipped prose,
    and the origin of a false claim on its first commit — falls inside
    the corpus it polices.
    """
    return _docstrings_under("src/") + _docstrings_under("tests/")


# --------------------------------------------------------------------------
# Repo introspection
# --------------------------------------------------------------------------
def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


_SKIP_DIR_NAMES = frozenset(
    {".git", ".claude", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
)


def _git_tracked_py_files() -> tuple[str, ...] | None:
    """Tracked ``*.py`` paths, or ``None`` when this is not a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        return None
    if proc.returncode != 0:  # pragma: no cover - not a checkout
        return None
    rels = [rel for rel in proc.stdout.decode("utf-8").split("\0") if rel]
    return tuple(sorted(rel for rel in rels if (_REPO_ROOT / rel).is_file()))


def _walk_py_files() -> tuple[str, ...]:
    """Corpus fallback for a tree with no git metadata.

    Prunes as it descends rather than filtering afterwards. A directory
    holding ``pyvenv.cfg`` is a virtualenv root per PEP 405 whatever it is
    named, so that marker catches environments a name list would miss.
    ``os.walk`` does not follow directory symlinks, so a link pointing into
    an environment is skipped too.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _SKIP_DIR_NAMES and not (here / d / "pyvenv.cfg").is_file()
        ]
        out.extend(
            (here / name).relative_to(_REPO_ROOT).as_posix()
            for name in filenames
            if name.endswith(".py")
        )
    return tuple(sorted(out))


@lru_cache(maxsize=None)
def _all_py_files() -> tuple[str, ...]:
    """Every Python file that belongs to this repo — and nothing vendored.

    Tracked files, so an untracked dependency tree cannot enter the corpus
    by any name. The previous rule was a directory-name skip list that
    happened to contain ``.venv`` but not ``venv``, which is where this
    repo's environment actually lives; the whole site-packages tree was
    being scanned. That was not merely slow. ``_resolve_modules`` matches
    on basename, so third-party modules became candidate readings of bare
    references meant for ``src/`` — a dependency shipping ``events.py`` or
    ``store.py`` could satisfy a claim about this project's module of that
    name, and ``file-count`` answers were inflated by files nobody here
    wrote.

    The tradeoff of keying on tracked-ness: a brand-new file joins the
    corpus when it is staged, not when it is created. That is the right
    way round for a CI gate, which always runs against a committed tree,
    and it is why the rule is tracked-ness rather than a smarter filter.
    """
    tracked = _git_tracked_py_files()
    return _walk_py_files() if tracked is None else tracked


@lru_cache(maxsize=None)
def _resolve_modules(name: str) -> tuple[str, ...]:
    """Every file a bare-ish module reference could plausibly mean.

    Bare references are genuinely ambiguous — two files are named
    ``verify.py`` and two are named ``init.py``. Rather than guess (a
    wrong guess is a false positive, the one outcome that gets this
    checker disabled) or skip (which would drop most of the corpus),
    callers verify against *all* candidates and report only when the
    claim fails against every one of them. A claim that holds for some
    plausible reading of the reference is not a false claim.
    """
    return tuple(
        rel for rel in _all_py_files() if rel == name or rel.endswith("/" + name)
    )


@lru_cache(maxsize=None)
def _bound_names(rel: str) -> frozenset[str]:
    """Every name bound anywhere in a module, by AST."""
    tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return frozenset(names)


@lru_cache(maxsize=None)
def _test_function_count(rel: str) -> int | None:
    """Count test functions by AST. None when the file is parametrized.

    ``@pytest.mark.parametrize`` decouples function count from collected
    count, which makes any "N tests" claim ambiguous — so we decline to
    check rather than risk a false positive.
    """
    tree = ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))

    def is_parametrize(node: ast.AST) -> bool:
        for deco in getattr(node, "decorator_list", []):
            if "parametrize" in ast.unparse(deco):
                return True
        return False

    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                if is_parametrize(node):
                    return None
                count += 1
    return count


# --------------------------------------------------------------------------
# Extraction + verification, per claim shape
# --------------------------------------------------------------------------
def _is_placeholder(token: str) -> bool:
    stem = token.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem.lower() in _PLACEHOLDER_STEMS


def check_paths(source: str, text: str, line_offset: int = 0) -> list[Failure]:
    """Anchored repo-relative path tokens must exist on disk."""
    out: list[Failure] = []
    if source in _PLAN_DOCS:
        return out
    for index, line in enumerate(text.splitlines(), 1):
        for match in _BACKTICK.finditer(line):
            token = match.group(1)
            if not token.startswith(_PATH_PREFIXES):
                continue
            if any(ch in token for ch in "*?<>%$ ") or not _PATH_SUFFIX.search(token):
                continue
            if _is_placeholder(token):
                continue
            if _ILLUSTRATIVE_CUE.search(line[: match.start()]):
                continue
            if (_REPO_ROOT / token).exists():
                continue
            claim = Claim(source, index + line_offset, "path", token)
            out.append(Failure(claim, "no such file in the repo"))
    return out


def check_symbols(source: str, text: str, line_offset: int = 0) -> list[Failure]:
    """``` `sym` in `module.py` ``` must resolve in that module by AST."""
    out: list[Failure] = []
    for match in _SYMBOL_IN_MODULE.finditer(text):
        sym, mod = match.group("sym"), match.group("mod")
        if sym.endswith("py") or _is_placeholder(mod):
            continue
        context = text[max(0, match.start() - _RELOCATION_LOOKBACK) : match.end()]
        if _RELOCATION_PROSE.search(context):
            continue
        candidates = _resolve_modules(mod)
        if not candidates:
            continue
        parts = sym.split(".")
        if any(
            parts[-1] in _bound_names(rel) or parts[0] in _bound_names(rel)
            for rel in candidates
        ):
            continue
        line = _line_of(text, match.start()) + line_offset
        claim = Claim(source, line, "symbol", f"{sym} in {mod}")
        out.append(Failure(claim, f"not bound anywhere in {mod} (checked by AST)"))
    return out


def check_file_counts(source: str, text: str, line_offset: int = 0) -> list[Failure]:
    """ "N files are named ``x.py``" must match what the repo scan resolves.

    This shape exists because the first version of this module miscounted
    the files carrying the name ``verify.py`` — asserting three where the
    repo holds two. That was a false claim in the very file built to stop
    false claims, in a shape no other rule covered.
    """
    out: list[Failure] = []
    for match in _FILECOUNT.finditer(text):
        name = match.group("name")
        if _is_placeholder(name):
            continue
        claimed = _parse_number(match.group("n"))
        if claimed < 0:
            continue
        actual = len(_resolve_modules(name))
        if actual == claimed:
            continue
        line = _line_of(text, match.start()) + line_offset
        claim = Claim(source, line, "file-count", name)
        out.append(
            Failure(
                claim, f"prose claims {claimed} file(s) so named; repo has {actual}"
            )
        )
    return out


def check_test_counts(source: str, text: str) -> list[Failure]:
    """Total-marked test counts must match the AST function count."""
    out: list[Failure] = []
    found: list[tuple[int, str, int]] = []

    for match in _TESTCOUNT_TOTAL.finditer(text):
        window = text[max(0, match.start() - 40) : match.start()]
        if _DELTA_QUALIFIER.search(window) or _DELTA_QUALIFIER.search(match.group(0)):
            continue
        if _RESTRICTIVE.match(match.group("tail")):
            continue
        found.append(
            (
                _line_of(text, match.start()),
                match.group("path"),
                _parse_number(match.group("n")),
            )
        )

    for match in _TESTCOUNT_SUBJECT.finditer(text):
        if _RESTRICTIVE.match(match.group("tail")):
            continue
        if _DELTA_QUALIFIER.search(match.group(0)):
            continue
        found.append(
            (
                _line_of(text, match.start()),
                match.group("path"),
                _parse_number(match.group("n")),
            )
        )

    for line, rel, claimed in found:
        if claimed < 0:
            continue
        if not (_REPO_ROOT / rel).is_file():
            claim = Claim(source, line, "test-count", rel)
            out.append(Failure(claim, "claims a test count for a file that is missing"))
            continue
        actual = _test_function_count(rel)
        if actual is None or actual == claimed:
            continue
        claim = Claim(source, line, "test-count", rel)
        out.append(Failure(claim, f"prose claims {claimed} tests; AST counts {actual}"))
    return out


def _parse_number(token: str) -> int:
    lowered = token.lower()
    if lowered.isdigit():
        return int(lowered)
    return _NUMBER_WORDS.get(lowered, -1)


def _paragraph_around(text: str, line: int) -> str:
    lines = text.splitlines()
    index = min(max(line - 1, 0), max(len(lines) - 1, 0))
    start = index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = index
    while end < len(lines) - 1 and lines[end + 1].strip():
        end += 1
    return "\n".join(lines[start : end + 1])


@lru_cache(maxsize=None)
def _module_lines(rel: str) -> tuple[str, ...]:
    """Cached line list for a repo-relative source file."""
    return tuple((_REPO_ROOT / rel).read_text(encoding="utf-8").splitlines())


def _anchor_miss(
    text: str, line: int, body: list[str], start: int, end: int
) -> set[str] | None:
    """Identifiers the paragraph pins to a citation but which are not near it.

    Returns ``None`` when there is nothing to decide: no identifier in the
    surrounding paragraph exists in the target file at all, so the citation
    has no anchor to be measured against and the rule stays quiet. Returns
    an empty set when at least one anchor lands within ``_ANCHOR_WINDOW``
    lines. Both of those are passes — callers test falsiness, not identity.
    """
    blob = "\n".join(body)
    anchors = {
        ident
        for ident in _CODE_IDENT.findall(_paragraph_around(text, line))
        if re.search(rf"\b{re.escape(ident)}\b", blob)
    }
    if not anchors:
        return None
    near = "\n".join(body[max(0, start - 1 - _ANCHOR_WINDOW) : end + _ANCHOR_WINDOW])
    if any(re.search(rf"\b{re.escape(a)}\b", near) for a in anchors):
        return set()
    return anchors


def _quoted_as_nonresolving(text: str, start: int, end: int) -> bool:
    """True when prose near a citation passes a non-resolving verdict on it.

    ``start``/``end`` are the citation match's span in ``text``. Such a
    citation is being discussed as evidence, not asserted, so callers skip
    both the range check and the anchor check for it.
    """
    window = text[max(0, start - _NONRESOLVING_WINDOW) : end + _NONRESOLVING_WINDOW]
    return bool(_NONRESOLVING_PROSE.search(window))


def _anchor_detail(name: str, start: int, missed: set[str]) -> str:
    return (
        f"none of the identifiers the paragraph attributes to this citation "
        f"({', '.join(sorted(missed))}) appear within {_ANCHOR_WINDOW} lines "
        f"of {name}:{start}"
    )


def check_line_refs(source: str, text: str) -> list[Failure]:
    """``file.py:NNN`` citations must be in range and land near their claim.

    Both halves apply to both citation shapes: a citation is a citation
    whether or not it is wrapped in a markdown link. The anchor half used
    to run on linked citations only, and that gap is not hypothetical — it
    is how a wrong citation shipped and how an allowlist entry covering it
    silently stopped matching (see the ``_ALLOWLIST`` note).

    Neither half runs on a citation the surrounding prose marks as
    non-resolving (``_quoted_as_nonresolving``): an erratum quoting its
    own rotten citation is not asserting it.
    """
    out: list[Failure] = []
    linked_spans: list[tuple[int, int]] = []

    for match in _LINEREF_LINKED.finditer(text):
        linked_spans.append(match.span())
        if _quoted_as_nonresolving(text, *match.span()):
            continue
        line = _line_of(text, match.start())
        name = match.group("name")
        subject = f"{name}:{match.group('start')}"
        target = ((_REPO_ROOT / source).parent / match.group("target")).resolve()
        claim = Claim(source, line, "line-ref", subject)
        if not target.is_file():
            out.append(Failure(claim, f"link target missing: {match.group('target')}"))
            continue
        body = target.read_text(encoding="utf-8").splitlines()
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end > len(body):
            out.append(
                Failure(claim, f"cites line {end}; {name} has {len(body)} lines")
            )
            continue
        missed = _anchor_miss(text, line, body, start, end)
        if not missed:
            continue
        out.append(Failure(claim, _anchor_detail(name, start, missed)))

    for match in _LINEREF_BARE.finditer(text):
        if any(s <= match.start() < e for s, e in linked_spans):
            continue
        name = match.group("name")
        if _is_placeholder(name):
            continue
        candidates = _resolve_modules(name)
        if not candidates:
            continue
        if _quoted_as_nonresolving(text, *match.span()):
            continue
        start = int(match.group("start"))
        line = _line_of(text, match.start())
        claim = Claim(source, line, "line-ref", f"{name}:{start}")

        lengths = {rel: len(_module_lines(rel)) for rel in candidates}
        in_range = sorted(rel for rel, n in lengths.items() if start <= n)
        if not in_range:
            sizes = ", ".join(f"{rel} has {n}" for rel, n in sorted(lengths.items()))
            out.append(Failure(claim, f"cites line {start}; {sizes}"))
            continue

        # Ambiguity is resolved the way `_resolve_modules` documents: a bare
        # reference may name several files, and a claim that holds for any
        # plausible reading is not false. So the anchor check reports only
        # when every in-range candidate misses.
        misses: dict[str, set[str]] = {}
        for rel in in_range:
            missed = _anchor_miss(text, line, list(_module_lines(rel)), start, start)
            if not missed:  # anchor landed, or there was no anchor to check
                break
            misses[rel] = missed
        else:
            named = {anchor for found in misses.values() for anchor in found}
            out.append(Failure(claim, _anchor_detail(name, start, named)))
    return out


def collect_failures() -> list[Failure]:
    """Run every checker over its in-scope corpus."""
    out: list[Failure] = []
    for source, text in _prose_sources():
        out.extend(check_paths(source, text))
        out.extend(check_symbols(source, text))
    for source, text in _living_docs():
        out.extend(check_test_counts(source, text))
        out.extend(check_line_refs(source, text))
        out.extend(check_file_counts(source, text))
    for rel, lineno, text in _code_docstrings():
        out.extend(check_paths(rel, text, line_offset=lineno - 1))
        out.extend(check_symbols(rel, text, line_offset=lineno - 1))
        out.extend(check_file_counts(rel, text, line_offset=lineno - 1))
    return out


# --------------------------------------------------------------------------
# The two paired ratchet tests
# --------------------------------------------------------------------------
def test_no_unexpected_false_claims() -> None:
    """Forward guard: no checkable claim in shipped prose may be false.

    If this fails, the prose is wrong — fix the prose (or the code it
    describes). Adding an ``_ALLOWLIST`` entry is for claims owned by
    other in-flight work, not for silencing your own.
    """
    unexpected = [f for f in collect_failures() if f.claim.key not in _ALLOWLIST]
    if unexpected:
        rendered = "\n".join(f"  - {f}" for f in unexpected)
        pytest.fail(
            f"{len(unexpected)} false claim(s) in shipped prose:\n{rendered}\n\n"
            f"Each is a statement the repo does not support. Fix the prose "
            f"rather than the checker unless the extraction itself misfired."
        )


def test_allowlist_has_no_stale_entries() -> None:
    """Reverse guard: the allowlist may not outlive the failures it covers.

    This is what makes the allowlist a ratchet. When someone repairs an
    exempted claim, its entry stops matching a real failure and this test
    fails, forcing the entry out. Without this, the list would silently
    calcify into permanent suppression.
    """
    live = {f.claim.key for f in collect_failures()}
    stale = sorted(key for key in _ALLOWLIST if key not in live)
    if stale:
        rendered = "\n".join(
            f"  - {key} (exempt because: {_ALLOWLIST[key]})" for key in stale
        )
        pytest.fail(
            f"{len(stale)} _ALLOWLIST entr(y/ies) no longer correspond to a real "
            f"failure:\n{rendered}\n\nTwo different things cause this and they "
            f"need opposite responses:\n"
            f"  (1) the claim was repaired — delete the entry, that is the "
            f"ratchet;\n"
            f"  (2) the extractor stopped matching a claim that is still "
            f"false — the prose was reworded, or a rule narrowed. Deleting "
            f"the entry then hides a live defect.\n"
            f"Check the claim against the source before deleting. Recording "
            f"(1) when it was really (2) has already happened once here; see "
            f"the _ALLOWLIST note."
        )


def test_allowlist_entries_carry_a_reason() -> None:
    """Every exemption must say why, so review can judge it."""
    for key, reason in _ALLOWLIST.items():
        assert len(reason.strip()) >= 40, f"{key} needs a substantive reason"


def test_no_allowlist_entry_covers_a_docstring_source() -> None:
    """Derives the 'costs nothing in exemptions' claim in the module docstring.

    Including ``src/`` and ``tests/`` docstrings in the corpus was
    justified on the grounds that their misfires get answered with
    extractor rules rather than allowlist entries. That is a standing
    claim about the allowlist, so it is asserted here instead of being
    written down as a count that would drift.
    """
    docstring_sources = {rel for rel, _, _ in _code_docstrings()}
    offenders = sorted(key for key in _ALLOWLIST if key[0] in docstring_sources)
    assert not offenders, (
        f"a docstring source now needs an exemption: {offenders}. Fix the "
        f"docstring — it is prose this repo owns and can simply correct."
    )


# --------------------------------------------------------------------------
# Self-tests. These prove the checkers can actually fail — a rule that is
# merely satisfied today is indistinguishable from a rule that does nothing.
# They assert against real repo files, so they stay honest as the repo moves.
# --------------------------------------------------------------------------
def test_detects_missing_path() -> None:
    fails = check_paths("docs/fake.md", "See `src/bettermemory/nope_xyz.py` for it.")
    assert [f.claim.subject for f in fails] == ["src/bettermemory/nope_xyz.py"]


def test_accepts_existing_path() -> None:
    assert check_paths("docs/fake.md", "See `src/bettermemory/store.py`.") == []


def test_placeholder_paths_are_not_claims() -> None:
    """`src/mod.py` in a syntax example asserts nothing about the repo."""
    text = "repo-relative citations (`src/mod.py`, `docs/spec.md`, `src/x.py`)"
    assert check_paths("docs/fake.md", text) == []


def test_illustrative_cue_suppresses_path_claim() -> None:
    assert (
        check_paths("docs/fake.md", "a repo-relative path like `src/nope_q.py`") == []
    )


def test_plan_docs_exempt_from_path_claims() -> None:
    """Plans propose files that do not exist yet; that is what a plan is."""
    text = "add a `tests/bench_not_real_xyz.py` harness"
    assert check_paths("docs/swarm-convergence-plan.md", text) == []
    assert len(check_paths("docs/clients.md", text)) == 1


def test_detects_unresolvable_symbol() -> None:
    fails = check_symbols("docs/fake.md", "`no_such_symbol_xyz` in `verify.py` does it")
    assert len(fails) == 1
    assert fails[0].claim.kind == "symbol"


def test_accepts_resolvable_symbol() -> None:
    assert (
        check_symbols("docs/fake.md", "`compute_staleness_verdict` in `verify.py`")
        == []
    )


def test_symbol_match_does_not_cross_a_clause_boundary() -> None:
    """The interposed-words rule must not pair unrelated halves.

    Real corpus text: "(`show`) both use it. Eight tests in
    `tests/test_indexed_lookup.py`" — `show` is not claimed to live in
    that file, and an over-greedy pattern would say it was.
    """
    text = "(`show`) both use it. Eight tests in `tests/test_indexed_lookup.py` pin it."
    assert check_symbols("docs/fake.md", text) == []


def test_past_tense_relocation_is_not_a_present_claim() -> None:
    """Real corpus text from builder.py's module docstring.

    ``_register_tools`` genuinely used to live in server.py and now lives
    in builder.py. The sentence is true; reading it as a present-tense
    claim was an extractor bug, not a false claim in the prose.
    """
    text = (
        "Pre-Round-3 ``build_server`` and ``_register_tools`` lived in ``server.py``."
    )
    assert check_symbols("src/bettermemory/builder.py", text) == []


def test_present_tense_claim_about_the_same_symbol_still_fires() -> None:
    """The relocation guard must not blanket-exempt the module."""
    text = "``_register_tools`` in ``server.py`` binds each handler."
    assert len(check_symbols("docs/fake.md", text)) == 1


def test_detects_wrong_test_count() -> None:
    """tests/test_indexed_lookup.py is unparametrized, so its count is exact."""
    real = _test_function_count("tests/test_indexed_lookup.py")
    assert real is not None
    text = f"The {real + 3} tests in `tests/test_indexed_lookup.py` pin the property."
    fails = check_test_counts("docs/fake.md", text)
    assert len(fails) == 1
    assert f"AST counts {real}" in fails[0].detail


def test_detects_wrong_test_count_spelled_as_a_word() -> None:
    """The real instances were spelled out ("Nine"), not written in digits."""
    text = "The nineteen tests in `tests/test_indexed_lookup.py` pin the property."
    fails = check_test_counts("docs/fake.md", text)
    assert len(fails) == 1


def test_accepts_correct_test_count() -> None:
    real = _test_function_count("tests/test_indexed_lookup.py")
    text = f"The {real} tests in `tests/test_indexed_lookup.py` pin the property."
    assert check_test_counts("docs/fake.md", text) == []


def test_subject_form_test_count_is_checked() -> None:
    text = "`tests/test_indexed_lookup.py` contains nineteen test functions."
    assert len(check_test_counts("docs/fake.md", text)) == 1


def test_bare_count_without_determiner_is_not_a_total_claim() -> None:
    """ "two checks in `X`" means two of them, not that X holds two."""
    text = "regression tests (`tests/test_version.py`, two checks in "
    text += "`tests/test_indexed_lookup.py`)."
    assert check_test_counts("docs/fake.md", text) == []


def test_delta_qualified_count_is_not_a_total_claim() -> None:
    text = "The 9 new tests in `tests/test_indexed_lookup.py` pin striping."
    assert check_test_counts("docs/fake.md", text) == []


def test_parametrized_files_are_skipped_for_counts() -> None:
    """Function count != collected count, so the claim is ambiguous."""
    parametrized = [
        rel
        for rel in _all_py_files()
        if rel.startswith("tests/") and _test_function_count(rel) is None
    ]
    assert parametrized, "expected at least one parametrized test module"
    text = f"The 99999 tests in `{parametrized[0]}` pin things."
    assert check_test_counts("docs/fake.md", text) == []


def test_detects_out_of_range_line_reference() -> None:
    text = "see [events.py:999999](../src/bettermemory/events.py) for the shard"
    fails = check_line_refs("docs/fake.md", text)
    assert len(fails) == 1
    assert "cites line 999999" in fails[0].detail


def _crc32_shard_line() -> int:
    """1-indexed line of the crc32 shard assignment in events.py.

    DERIVED, never hardcoded. These two self-tests originally cited
    `events.py:320` and `events.py:237` as literals, and the 320 one went
    red the moment unrelated work shifted the assignment down the file —
    a checker whose own fixtures rot on a line number, while the rule it
    enforces exists to catch exactly that. (The 237 case was no sounder;
    it passed only because that line happened to stay far from any
    `crc32`.) Resolving both at runtime keeps the tests exercising the
    real anchor-proximity logic against real source without inheriting
    the brittleness they are meant to police.
    """
    for i, line in enumerate(_module_lines(_EVENTS_MODULE), start=1):
        if "crc32(" in line and not line.lstrip().startswith("#"):
            return i
    raise AssertionError(
        "no crc32( call found in events.py — this fixture's anchor is gone; "
        "repoint it at whatever now identifies the shard assignment"
    )


def test_detects_line_reference_pointing_at_the_wrong_place() -> None:
    """A line-ref cited for `crc32` that lands nowhere near the assignment.

    A pure in-range line-count check passes this — only anchor proximity
    catches it. The decoy line is derived as "far from the real anchor"
    rather than hardcoded, so it cannot silently become a TRUE citation
    when the file shifts.
    """
    anchor = _crc32_shard_line()
    decoy = anchor + 200
    total = len(_module_lines(_EVENTS_MODULE))
    if decoy > total:  # pragma: no cover - only if events.py shrinks sharply
        decoy = max(1, anchor - 200)
    text = (
        "a recorder picks its shard by `crc32(session_id)` "
        f"([events.py:{decoy}](../src/bettermemory/events.py)), so writers differ"
    )
    fails = check_line_refs("docs/fake.md", text)
    assert len(fails) == 1
    assert "crc32" in fails[0].detail


def test_accepts_line_reference_that_lands_on_its_anchor() -> None:
    anchor = _crc32_shard_line()
    text = (
        "a recorder picks its shard by `crc32(session_id)` "
        f"([events.py:{anchor}](../src/bettermemory/events.py)), so writers differ"
    )
    assert check_line_refs("docs/fake.md", text) == []


def test_ambiguous_module_reference_accepts_any_plausible_reading() -> None:
    """Two files are named ``verify.py``; the claim holds if either satisfies it.

    Of those two candidates, only ``handlers/verify.py`` binds
    ``memory_verify``; the top-level ``verify.py`` does not bind the name
    at all. (It is bound in modules outside both candidates as well — the
    point here is just that one of the two resolves it.) Guessing
    "shallowest wins" would report this true statement as false — the
    exact false positive that gets a checker switched off.

    The count above is in the phrasing ``_FILECOUNT`` matches, so this
    docstring is itself checked by ``check_file_counts``.
    """
    assert len(_resolve_modules("verify.py")) > 1
    assert check_symbols("docs/fake.md", "`memory_verify` in `verify.py` runs it") == []


def test_ambiguous_module_reference_still_reports_a_claim_false_everywhere() -> None:
    """Ambiguity is not a free pass: absent from all candidates is false."""
    fails = check_symbols("docs/fake.md", "`absent_everywhere_xyz` in `verify.py`")
    assert len(fails) == 1


def test_detects_wrong_file_count() -> None:
    """The exact false claim this file shipped on its first commit.

    The original prose put the count at three; the repo holds two.
    The offending wording is kept in the body, not this docstring,
    because ``tests/`` docstrings are now part of the scanned corpus —
    quoting it here would make this docstring a false claim in its own
    right.
    """
    text = "three files are named `verify.py` so the reference is ambiguous"
    fails = check_file_counts("tests/fake.py", text)
    assert len(fails) == 1
    assert fails[0].claim.kind == "file-count"
    assert "claims 3 file(s) so named; repo has 2" in fails[0].detail


def test_accepts_correct_file_count() -> None:
    actual = len(_resolve_modules("verify.py"))
    text = f"{actual} files are named `verify.py` so the reference is ambiguous"
    assert check_file_counts("tests/fake.py", text) == []


def test_file_count_accepts_the_elided_form() -> None:
    """ "two are named `init.py`" elides the noun and is still a claim."""
    assert check_file_counts("tests/fake.py", "two are named `init.py`") == []
    assert len(check_file_counts("tests/fake.py", "nine are named `init.py`")) == 1


def test_file_count_ignores_placeholder_names() -> None:
    assert check_file_counts("tests/fake.py", "three files are named `mod.py`") == []


def test_this_module_is_inside_the_scanned_corpus() -> None:
    """The guard must be able to audit its own docstrings.

    Excluding ``tests/`` is what let the original miscount of
    ``verify.py`` ship: no rule could ever have read it.
    """
    scanned = {rel for rel, _, _ in _code_docstrings()}
    assert "tests/test_doc_claims.py" in scanned
    assert any(rel.startswith("src/") for rel in scanned)


def test_corpus_extension_alone_would_not_have_caught_the_defect() -> None:
    """Pins the honest limit claimed in the module docstring.

    Scanning ``tests/`` was necessary but not sufficient — the original
    wording is invisible to every rule that predates ``file-count``, so
    the corpus fix and the shape fix each close half of the hole.
    """
    text = "three files are named `verify.py` and two are named `init.py`"
    assert check_paths("tests/fake.py", text) == []
    assert check_symbols("tests/fake.py", text) == []
    assert len(check_file_counts("tests/fake.py", text)) == 1


def test_line_ref_uses_the_largest_plausible_candidate() -> None:
    """A line in range for any same-named file is not an out-of-range cite."""
    text = "see verify.py:1700 for the detector"
    assert check_line_refs("docs/fake.md", text) == []
    assert len(check_line_refs("docs/fake.md", "see verify.py:999999 there")) == 1


def test_bare_citation_is_anchor_checked_like_a_linked_one() -> None:
    """The gap that let a wrong citation ship: bare cites were range-only.

    Same sentence, same wrong line, only the markdown differs — so the
    two forms must reach the same verdict. Before this, the linked form
    failed and the bare form passed silently.
    """
    decoy = _crc32_shard_line() + 200
    bare = f"a recorder picks its shard by `crc32(session_id)` (`events.py:{decoy}`)"
    linked = (
        "a recorder picks its shard by `crc32(session_id)` "
        f"([events.py:{decoy}](../src/bettermemory/events.py))"
    )
    bare_fails = check_line_refs("docs/fake.md", bare)
    assert len(bare_fails) == 1
    assert "crc32" in bare_fails[0].detail
    assert len(check_line_refs("docs/fake.md", linked)) == 1


def test_bare_citation_that_lands_on_its_anchor_is_accepted() -> None:
    """The anchor extension must not fire on a correct bare citation."""
    anchor = _crc32_shard_line()
    text = f"a recorder picks its shard by `crc32(session_id)` (`events.py:{anchor}`)"
    assert check_line_refs("docs/fake.md", text) == []


def test_bare_citation_without_a_resolvable_anchor_stays_quiet() -> None:
    """No identifier from the paragraph exists in the target: nothing to judge.

    This is the precision guard that keeps the extension from firing on
    every incidental line number in the corpus.
    """
    text = "the shard rule is discussed at `events.py:10` in passing"
    assert check_line_refs("docs/fake.md", text) == []


def test_bare_citation_ambiguity_accepts_any_plausible_candidate() -> None:
    """Two files are named ``episodes.py``; one satisfying the anchor is enough.

    Real corpus shape. The anchor check must inherit ``_resolve_modules``'
    rule — report only when the citation fails against every candidate —
    or ambiguity becomes a false-positive engine.
    """
    assert len(_resolve_modules("episodes.py")) > 1
    src = _REPO_ROOT / "src/bettermemory/episodes.py"
    body = src.read_text(encoding="utf-8").splitlines()
    target = next(
        i for i, line in enumerate(body, start=1) if "def list_by_swarm" in line
    )
    text = f"`list_by_swarm(swarm_id)` walks the shard (`episodes.py:{target}`)"
    assert check_line_refs("docs/fake.md", text) == []


def test_citation_quoted_as_nonresolving_is_not_checked() -> None:
    """A quote of a rotten citation, verdict attached, is not an assertion.

    Real corpus shape: the swarm plan's errata quote their own shipped
    citations precisely to argue they miss. Checking the quote against
    HEAD failed the prose for being right — the false positive the
    allowlist carried until this rule landed (see the retired-entries
    note). Suppression must cover both citation shapes and both halves
    of the check, the range half included.
    """
    decoy = _crc32_shard_line() + 200
    bare = (
        f"the doc shipped `events.py:{decoy}` for the `crc32(session_id)` "
        "shard pick, but it lands outside every function and class body"
    )
    assert check_line_refs("docs/fake.md", bare) == []
    linked = (
        f"[events.py:{decoy}](../src/bettermemory/events.py) was cited for "
        "the `crc32(session_id)` shard pick and lands outside every "
        "function and class body"
    )
    assert check_line_refs("docs/fake.md", linked) == []
    out_of_range = (
        "the doc originally shipped `events.py:999999` for the "
        "`crc32(session_id)` shard pick"
    )
    assert check_line_refs("docs/fake.md", out_of_range) == []


def test_nonresolving_verdict_must_sit_near_the_citation() -> None:
    """The suppression is marker-plus-proximity, never a paragraph pass.

    The same wrong citation stays red with no verdict phrase in reach,
    and stays red with the verdict phrase pushed beyond
    ``_NONRESOLVING_WINDOW`` — the false-negative direction this rule
    must not have. The middle assertion pins that the far text really
    does contain a marker, so this test cannot rot into vacuity.
    """
    decoy = _crc32_shard_line() + 200
    plain = f"a recorder picks its shard by `crc32(session_id)` (`events.py:{decoy}`)"
    assert len(check_line_refs("docs/fake.md", plain)) == 1
    padding = "the surrounding discussion keeps going for a while. " * 4
    far = "an earlier citation straddles two functions. " + padding + plain
    assert _NONRESOLVING_PROSE.search(far) is not None
    assert len(check_line_refs("docs/fake.md", far)) == 1


def test_restrictive_clause_demotes_a_total_marked_count_too() -> None:
    """``_RESTRICTIVE`` must apply to both count phrasings, not just one.

    "the N tests in `X` that ..." is a subset in exactly the way
    "`X` has N tests that ..." is. The guard was wired to the subject
    form only, so the same English escaped it when written the other way
    round.
    """
    real = _test_function_count("tests/test_indexed_lookup.py")
    assert real is not None
    wrong = real + 3
    subset = f"The {wrong} tests in `tests/test_indexed_lookup.py` that pin striping"
    assert check_test_counts("docs/fake.md", subset) == []
    # The same wrong number, without the restrictive clause, is a total.
    total = f"The {wrong} tests in `tests/test_indexed_lookup.py` pin striping"
    assert len(check_test_counts("docs/fake.md", total)) == 1


def test_corpus_excludes_untracked_dependency_trees() -> None:
    """A virtualenv in the tree must not become part of the scanned corpus.

    The skip list this replaced named ``.venv`` but not ``venv``, so the
    site-packages tree was scanned. Beyond the cost, ``_resolve_modules``
    matches on basename, so a dependency's ``events.py`` became a
    candidate reading of a claim about this project's ``events.py``.
    """
    corpus = _all_py_files()
    assert corpus, "corpus is empty — the file discovery broke"
    assert not [rel for rel in corpus if "site-packages" in rel]
    assert all(rel.split("/")[0] not in _SKIP_DIR_NAMES for rel in corpus), (
        "corpus contains a path under a directory that should have been pruned"
    )
    # The docstring corpus must inherit the same discipline. It used a
    # `src/**/*.py` glob, which would descend into a vendored tree parked
    # under src/ or tests/ — and a dependency's docstring failing this
    # repo's CI is the worst version of this bug, not a milder one.
    scanned = {rel for rel, _, _ in _code_docstrings()}
    assert scanned <= set(corpus), (
        f"the docstring corpus reaches files the tracked listing excludes: "
        f"{sorted(scanned - set(corpus))[:10]}"
    )
    # Every module reference this file's own fixtures resolve must land in
    # first-party code, or the ambiguity rules are being fed foreign files.
    for name in ("events.py", "store.py", "verify.py", "episodes.py"):
        assert all(
            rel.startswith(("src/", "tests/", "bench/"))
            for rel in _resolve_modules(name)
        ), f"{name} resolves outside first-party code"


def test_walk_fallback_admits_nothing_the_git_listing_excludes() -> None:
    """The no-git fallback must not readmit what tracked-ness keeps out.

    ``git ls-files`` is the primary because tracked-ness is categorical;
    the walk runs only where there is no git metadata, and its pruning is
    heuristic. So the direction that matters is pinned here: the walk may
    miss a tracked file, but it may never admit an untracked one. The
    reverse containment is deliberately not asserted — the skip list can
    legitimately prune a directory someone has tracked a file inside.
    """
    tracked = _git_tracked_py_files()
    if tracked is None:  # pragma: no cover - only outside a git checkout
        pytest.skip("not a git checkout")
    walked = _walk_py_files()
    assert not set(walked) - set(tracked), (
        f"the walk admits untracked files the git listing excludes: "
        f"{sorted(set(walked) - set(tracked))[:10]}"
    )
