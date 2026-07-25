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
4. ``line-ref`` — a ``file.py:NNN`` or ``file.py:NNN-MMM`` citation must
   land in range and the cited region must actually contain one of the
   code identifiers the surrounding paragraph attributes to it. A range
   must run forward: one whose start is past its end (the truncated-end
   typo) fails loudly as malformed rather than being silently reordered,
   and a forward range is then bounded by its end line — which bounds
   its start too. Both halves apply to the markdown-linked and
   bare-backticked forms alike, a range's end included. When the
   paragraph names no identifier that exists in the target at all, there
   is no anchor to judge against and the rule stays quiet. A citation
   the nearby prose marks as non-resolving, or whose paragraph pins its
   resolution to a named commit, is quoted evidence rather than an
   assertion and is skipped — see the deliberately-not-checked list.
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
* **Citations a paragraph resolves against a named commit.** The mirror
  image of the previous bullet, for the citations an erratum quotes as
  *landing*: those carry no non-resolving verdict — the verdict is that
  they hold — yet they describe the pinned tree, not HEAD, and
  HEAD-checking them fails the prose as soon as the cited file drifts.
  Before this rule, the swarm errata's two landing quotes survived only
  by accident: one sat inside the previous sentence's verdict window,
  the other passed because the cited region has not drifted yet. A
  commit-pin phrase (``_COMMIT_PINNED_PROSE``) anywhere in the
  citation's paragraph suppresses both halves of the check. Paragraph
  scope — where the verdict rule is window-bounded — is deliberate: a
  pin declares the reference frame for a whole analysis, not a
  judgement on one citation. It is also the honest new blind spot: a
  present-tense ``file.py:NNN`` assertion added to a pinned paragraph
  goes unchecked, so keep live claims out of pinned errata paragraphs.
  The self-tests pin both directions plus the paragraph boundary.
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
#
# One consumer of the pattern as a suppressor: `check_symbols` below, which
# searches the whole lookback for any alternative. That is affordable here
# because the claim it guards is already narrowed to the two-token `sym` in
# `mod.py` shape, which is what these alternatives were tuned for.
#
# The ratchet in `tests/test_symbol_citations.py` reads it for a second,
# weaker purpose. That module runs over a far wider surface — every
# backticked token in every docstring and comment — where these
# alternatives are ordinary English before they are tense markers: `was`,
# `were`, `before`, `until`, `once`, `old`, `moved`, `dropped`, `renamed`.
# It therefore keeps its OWN narrower list of attributive markers and
# imports this one only as an outer bound, asserting that every marker of
# its own is recognised here too. So a marker deleted from this pattern
# fails that module's subset guard if it is one the module also uses, and a
# marker widened here cannot widen that module's exemption at all.
# `_RELOCATION_LOOKBACK` below is a real shared input to it, though.
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
# precisely when it is right.
#
# What belongs here: a verdict on how a citation resolves, or a
# construction that *names what it judges* — the citation itself, or the
# code the citation missed or hit instead — which is why "originally
# shipped" carries a trailing `file.py:NNN` shape rather than sitting
# here as two bare words. What does not: vocabulary that merely turns up
# near a citation. A bare `\brotted\b` alternative
# used to sit here and was exactly that — `rotted` is this project's own
# word for a stale memory, live in ordinary living-document prose
# (README.md's opening paragraph, when this was written) — so a wrong
# citation within `_NONRESOLVING_WINDOW` of any such sentence was exempt
# with no CI signal. Dropping it changed no verdict in the corpus at the
# time: the single erratum citation its window reached keeps both the
# "originally shipped" construction and its paragraph's commit pin.
# `test_house_vocabulary_near_a_citation_does_not_suppress_it` pins the
# behaviour so the hole cannot be reopened silently.
#
# Three further alternatives have since had the same treatment.
# `nowhere near`, `short of the` and `narrowed it to` are ordinary
# English before they are verdicts — "nowhere near as fast", "stops
# short of the attestation block", "the triage narrowed it to the
# recorder" — so as bare phrases each exempted any citation that
# happened to sit within `_NONRESOLVING_WINDOW` of such a sentence.
# Each now has to name what the erratum is judging: the citation itself
# for `narrowed it to`, the code the citation missed for the other two.
# In the documents this rule actually runs against, all three occurred
# only inside the swarm-plan errata when this landed and every one of
# those constructions still matches, so the exposure closed was
# prospective and no suppression was orphaned.
# `test_bare_phrase_markers_must_name_what_they_judge` pins both
# directions for each.
#
# Two more have now followed: `straddles` and `a different
# function|method|class`. Neither is even verdict vocabulary — they are
# neutral description that only a citation turns into a judgement. This
# repo's own prose has turns that straddle a log rotation and citations
# that straddle the body-scan cap, and "we moved that to a different
# function" is unremarkable engineering English. `straddles` now takes
# a `file.py:NNN` as its grammatical *subject*, a citation being the
# only thing that straddles in the erratum sense; the appositive has to
# be predicated on the backticked identifier naming what the citation
# hit instead. In the documents this rule actually runs against, both
# occurred only inside the swarm-plan errata when this landed, on
# citations whose paragraphs are independently commit-pinned — deleting
# the two alternatives outright changed no verdict in that corpus — so
# no suppression was orphaned and the exposure closed was again
# prospective.
# `test_straddles_and_different_function_must_name_what_they_judge`
# pins both directions for each.
#
# The honest residual: this tightening is per-alternative, and not every
# alternative has had it. Which ones have is legible in the pattern
# itself — an alternative carrying a `file.py:NNN` shape or a backticked
# identifier next to the marker, before it or after it, names the
# citation or the code it judges; the rest match on wording alone and
# rely on that wording being rare outside a verdict, which is only ever
# as strong as the plainest English reading of the phrase. The guarantee
# against a paragraph-wide pass is the window bound below, not the
# phrasing. Multi-word markers use `\s+` because markdown wraps
# mid-phrase; proximity is bounded by `_NONRESOLVING_WINDOW` on both
# sides.
_NONRESOLVING_PROSE = re.compile(
    r"\b(?:do(?:es)?|did)\s+not\s+(?:point|resolve|land|sit)\b"
    r"|\bpoints?\s+at\s+prose\b"
    r"|\blands?\s+outside\b"
    # Landing verdicts, but only over the code the citation missed — or
    # hit instead: the marker must name it, as a backticked identifier.
    r"|\bnowhere\s+near\s+(?:the\s+)?`{1,2}[A-Za-z_][\w.]*`"
    r"|\bshort\s+of\s+the\s+`{1,2}[A-Za-z_][\w.]*`"
    r"|`{1,2}[A-Za-z_][\w.]*`{1,2},\s+a\s+different\s+(?:function|method|class)\b"
    r"|\bwrong\s+(?:when\s+written|on\s+arrival)\b"
    r"|\balready\s+(?:false|wrong|moved)\b"
    r"|\bnon-resolving\b"
    # Provenance and re-measurement, but only over a citation: the marker
    # must take a `file.py:NNN` (bare, backticked, or markdown-linked)
    # as its object.
    r"|\boriginally\s+shipped\s+\[?`{0,2}[\w/]+\.py:\d+"
    r"|\bnarrowed\s+it\s+to\s+\[?`{0,2}[\w/]+\.py:\d+"
    # Geometry verdict, over the citation as its subject — the same
    # `file.py:NNN` shapes, plus the tail of a markdown link.
    r"|[\w/]+\.py:\d+(?:-\d+)?`{0,2}(?:\]\([^)]*\))?\s+straddles\b",
    re.I,
)
_NONRESOLVING_WINDOW = 120

# Prose that pins a citation's RESOLUTION to a named commit — the mirror
# image of `_NONRESOLVING_PROSE`, covering the citations an erratum quotes
# as landing. Those carry no non-resolving verdict (the verdict is that
# they hold), yet they describe the pinned tree, not HEAD, so HEAD-checking
# them fails the prose whenever the cited file drifts — a false positive
# by construction, against exactly the prose that was being careful. Two
# phrasings, deliberately — "resolved against `<sha>`" and the errata's
# own "pinned to a named commit" — rather than a net for every English way
# of naming a commit; an unmatched phrasing leaves the citation checked,
# which fails loudly and teaches the canonical form. The sha must be
# backticked hex so casual prose ("resolved against the earlier tree")
# cannot pin anything. Multi-word, so `\s+` throughout: markdown wraps
# mid-phrase.
_COMMIT_PINNED_PROSE = re.compile(
    r"\bresolved\s+against\s+`[0-9a-f]{7,40}`"
    r"|\bpinned\s+to\s+a\s+named\s+commit\b",
    re.I,
)

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
_LINEREF_BARE = re.compile(
    r"`{0,2}(?P<name>[\w/]+\.py):(?P<start>\d+)(?:-(?P<end>\d+))?`{0,2}"
)
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


def _quoted_as_commit_pinned(text: str, line: int) -> bool:
    """True when the citation's paragraph pins its resolution to a commit.

    Paragraph-scoped where ``_quoted_as_nonresolving`` is window-scoped,
    deliberately: a non-resolving verdict judges one citation, but a pin
    ("resolved against ``60b7553``") declares the reference frame for
    every resolution in its analysis — the swarm errata pin a whole
    survey of citations with one phrase, sentences away from most of
    them. Callers skip both the range check and the anchor check for
    such a citation: it is a statement about the pinned tree, and HEAD
    is the wrong tree to judge it against. The cost of paragraph scope
    is disclosed in the module docstring's deliberately-NOT-checked list.
    """
    return bool(_COMMIT_PINNED_PROSE.search(_paragraph_around(text, line)))


def _anchor_detail(name: str, start: int, missed: set[str]) -> str:
    return (
        f"none of the identifiers the paragraph attributes to this citation "
        f"({', '.join(sorted(missed))}) appear within {_ANCHOR_WINDOW} lines "
        f"of {name}:{start}"
    )


def _malformed_range_detail(start: int, end: int) -> str:
    return (
        f"malformed range: start {start} is past end {end} — rejected as "
        f"written (a truncated end digit is the usual cause), never "
        f"silently reordered"
    )


def check_line_refs(source: str, text: str) -> list[Failure]:
    """``file.py:NNN`` citations must be in range and land near their claim.

    Both halves apply to both citation shapes: a citation is a citation
    whether or not it is wrapped in a markdown link. The anchor half used
    to run on linked citations only, and that gap is not hypothetical — it
    is how a wrong citation shipped and how an allowlist entry covering it
    silently stopped matching (see the ``_ALLOWLIST`` note).

    The same symmetry covers a range's end line. ``_LINEREF_BARE`` used
    to stop parsing at the start, so the end of a bare range was neither
    range-checked nor anchor-checked while the linked form checked both —
    a bare citation with a bogus end shipped silently where its linked
    twin failed. Both shapes now parse the end, range-check by it, and
    extend the anchor window to it.

    The start line closes the last gap of that family. Range-checking a
    range only by its end leaves a reversed one — start past end, the
    shape a truncated end digit produces — validating nothing but that
    (in-range) end, so an out-of-range start shipped silently in both
    shapes. The verdict is loud: a reversed range fails as malformed
    exactly as written, in both shapes, never silently reordered into
    the range the author probably meant. Rejecting reversal is also the
    whole start bound: a forward range's start cannot exceed its end, so
    the existing end check bounds the full span and a start past the
    file's last line cannot ship — there is no third, separate check to
    rot. Like the rest of the range half, the malformed verdict is
    suppressed for quoted-as-non-resolving and commit-pinned citations:
    quoted evidence keeps its exact shipped shape, malformed included.

    Neither half runs on a citation the surrounding prose marks as
    non-resolving (``_quoted_as_nonresolving``): an erratum quoting its
    own rotten citation is not asserting it. Nor on one whose paragraph
    pins its resolution to a named commit (``_quoted_as_commit_pinned``):
    an erratum resolving its quoted citations against a fixed tree is not
    asserting them against HEAD — that covers the citations an erratum
    quotes as *landing*, which no non-resolving verdict can.
    """
    out: list[Failure] = []
    linked_spans: list[tuple[int, int]] = []

    for match in _LINEREF_LINKED.finditer(text):
        linked_spans.append(match.span())
        if _quoted_as_nonresolving(text, *match.span()):
            continue
        line = _line_of(text, match.start())
        if _quoted_as_commit_pinned(text, line):
            continue
        name = match.group("name")
        subject = f"{name}:{match.group('start')}"
        target = ((_REPO_ROOT / source).parent / match.group("target")).resolve()
        claim = Claim(source, line, "line-ref", subject)
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start > end:
            # Tree-independent, so it precedes every filesystem check.
            out.append(Failure(claim, _malformed_range_detail(start, end)))
            continue
        if not target.is_file():
            out.append(Failure(claim, f"link target missing: {match.group('target')}"))
            continue
        body = target.read_text(encoding="utf-8").splitlines()
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
        end = int(match.group("end") or start)
        line = _line_of(text, match.start())
        if _quoted_as_commit_pinned(text, line):
            continue
        claim = Claim(source, line, "line-ref", f"{name}:{start}")

        if start > end:
            out.append(Failure(claim, _malformed_range_detail(start, end)))
            continue

        lengths = {rel: len(_module_lines(rel)) for rel in candidates}
        in_range = sorted(rel for rel, n in lengths.items() if end <= n)
        if not in_range:
            sizes = ", ".join(f"{rel} has {n}" for rel, n in sorted(lengths.items()))
            out.append(Failure(claim, f"cites line {end}; {sizes}"))
            continue

        # Ambiguity is resolved the way `_resolve_modules` documents: a bare
        # reference may name several files, and a claim that holds for any
        # plausible reading is not false. So the anchor check reports only
        # when every in-range candidate misses.
        misses: dict[str, set[str]] = {}
        for rel in in_range:
            missed = _anchor_miss(text, line, list(_module_lines(rel)), start, end)
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


def test_bare_range_end_is_range_checked_like_a_linked_one() -> None:
    """A bogus end in a bare range must fail exactly as it does linked.

    ``_LINEREF_BARE`` used to stop parsing at the start line, so the end
    of a bare range was never checked against the file at all — the
    bogus half shipped silently while the linked twin failed.
    """
    bare = "the recorder setup spans `events.py:5-999999` in full"
    bare_fails = check_line_refs("docs/fake.md", bare)
    assert len(bare_fails) == 1
    assert "cites line 999999" in bare_fails[0].detail
    linked = (
        "the recorder setup spans "
        "[events.py:5-999999](../src/bettermemory/events.py) in full"
    )
    linked_fails = check_line_refs("docs/fake.md", linked)
    assert len(linked_fails) == 1
    assert "cites line 999999" in linked_fails[0].detail


def test_bare_range_end_extends_the_anchor_window() -> None:
    """A valid bare range is judged by its whole span, not its start.

    The fixture puts the anchor inside the cited range but more than
    ``_ANCHOR_WINDOW`` lines past its start — the first assertion pins
    that geometry — so a parse that drops the end reports this correct
    citation as a miss: the false-positive direction, the one that gets
    a checker disabled.
    """
    anchor = _crc32_shard_line()
    start = max(1, anchor - _ANCHOR_WINDOW - 5)
    assert anchor - start > _ANCHOR_WINDOW, (
        "events.py no longer leaves room for this fixture ahead of the "
        "crc32 shard pick; repoint the range at whatever precedes it"
    )
    text = (
        "a recorder picks its shard by `crc32(session_id)` "
        f"(`events.py:{start}-{anchor}`)"
    )
    assert check_line_refs("docs/fake.md", text) == []


def test_reversed_range_is_rejected_as_malformed_in_both_shapes() -> None:
    """A reversed range must fail loud as written, never silently reorder.

    The main fixture is the truncated-end typo shape: the start far past
    the file's last line, the end comfortably inside it, and no
    identifier from the paragraph resolvable in the target — so nothing
    but the range half can fire, and range-checking only the end passes
    exactly this shape in both citation forms. That is the silent-ship
    direction this test pins closed. The last fixture drops the
    out-of-range start: reversal alone is the defect, wherever the
    endpoints land.
    """
    total = len(_module_lines(_EVENTS_MODULE))
    start, end = total + 200, 10
    bare = f"the recorder setup is discussed at `events.py:{start}-{end}` in passing"
    bare_fails = check_line_refs("docs/fake.md", bare)
    assert len(bare_fails) == 1
    assert "malformed range" in bare_fails[0].detail
    linked = (
        "the recorder setup is discussed at "
        f"[events.py:{start}-{end}](../src/bettermemory/events.py) in passing"
    )
    linked_fails = check_line_refs("docs/fake.md", linked)
    assert len(linked_fails) == 1
    assert "malformed range" in linked_fails[0].detail
    in_range = f"the recorder setup is discussed at `events.py:{end + 40}-{end}`"
    assert end + 40 < total, "events.py shrank under this fixture; re-derive it"
    in_range_fails = check_line_refs("docs/fake.md", in_range)
    assert len(in_range_fails) == 1
    assert "malformed range" in in_range_fails[0].detail


def test_forward_and_equal_endpoint_ranges_stay_quiet() -> None:
    """The malformed verdict is strict reversal, nothing wider.

    A forward range and the degenerate single-line range (start equal to
    end) are exactly as valid as before, in both shapes — reporting
    either would be the false-positive direction, the one that gets a
    checker disabled.
    """
    anchor = _crc32_shard_line()
    start = max(1, anchor - 3)
    forward = (
        "a recorder picks its shard by `crc32(session_id)` "
        f"(`events.py:{start}-{anchor}`)"
    )
    assert check_line_refs("docs/fake.md", forward) == []
    equal = (
        "a recorder picks its shard by `crc32(session_id)` "
        f"(`events.py:{anchor}-{anchor}`)"
    )
    assert check_line_refs("docs/fake.md", equal) == []
    linked = (
        "a recorder picks its shard by `crc32(session_id)` "
        f"([events.py:{start}-{anchor}](../src/bettermemory/events.py))"
    )
    assert check_line_refs("docs/fake.md", linked) == []


def test_reversed_range_quoted_as_evidence_is_not_checked() -> None:
    """Quoted evidence keeps its exact shipped shape, malformed included.

    An erratum quoting a truncated range in order to say it does not
    resolve is not asserting it, and neither is one resolving its quotes
    against a named commit — the malformed verdict inherits the same
    suppression as the rest of the range half, or the checker fails
    prose precisely when it is right about the defect. Both suppression
    rules are exercised.
    """
    total = len(_module_lines(_EVENTS_MODULE))
    quoted = (
        f"the doc shipped `events.py:{total + 200}-10` — a truncated end — "
        "and it does not resolve at HEAD"
    )
    assert check_line_refs("docs/fake.md", quoted) == []
    pinned = (
        "resolved against `0123abc`, "
        f"`events.py:{total + 200}-10` bracketed the recorder setup"
    )
    assert check_line_refs("docs/fake.md", pinned) == []


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
    far = (
        "an earlier citation was given as `Recorder.__post_init__`, a "
        "different function from the one it lands in. " + padding + plain
    )
    assert _NONRESOLVING_PROSE.search(far) is not None
    assert len(check_line_refs("docs/fake.md", far)) == 1


def test_house_vocabulary_near_a_citation_does_not_suppress_it() -> None:
    """A suppression marker must judge a citation, not merely sit beside one.

    ``rotted`` is this project's own word for a stale memory — README's
    opening paragraph uses it that way, of a memory and nowhere near a
    citation. While it sat in ``_NONRESOLVING_PROSE`` as a bare
    alternative, any wrong citation within ``_NONRESOLVING_WINDOW`` of
    such an ordinary sentence was exempt, and no CI signal said so. Same
    for the provenance marker, which now has to take the citation as its
    object: "originally shipped" about anything else leaves a nearby
    citation checked.

    Both fixtures are one clause away from a citation the checker
    otherwise reports, so each asserts the false-negative direction the
    suppression rules must not have.
    """
    decoy = _crc32_shard_line() + 200
    cited = f"a recorder picks its shard by `crc32(session_id)` (`events.py:{decoy}`)"
    assert len(check_line_refs("docs/fake.md", cited)) == 1
    rotted = "a memory that has rotted is flagged rather than quoted back; " + cited
    assert re.search(r"\brotted\b", rotted), "fixture lost the house word"
    assert len(check_line_refs("docs/fake.md", rotted)) == 1
    shipped = "the sharded recorder originally shipped in 3.24.0, and " + cited
    assert re.search(r"\boriginally\s+shipped\b", shipped), "fixture lost the marker"
    assert len(check_line_refs("docs/fake.md", shipped)) == 1
    # The construction the errata actually use — marker plus the citation
    # it is about — must still suppress, or the tightening has gone too far.
    quoted = (
        f"the doc originally shipped `events.py:{decoy}` for the "
        "`crc32(session_id)` shard pick"
    )
    assert check_line_refs("docs/fake.md", quoted) == []


def test_bare_phrase_markers_must_name_what_they_judge() -> None:
    """Three markers that used to match on wording alone, not on a verdict.

    ``nowhere near``, ``short of the`` and ``narrowed it to`` are
    ordinary English before they are verdicts — "nowhere near as fast",
    "stops short of the attestation block" (a real assertion message, in
    ``tests/test_prompts.py``), "the triage narrowed it to the
    recorder". While each sat in ``_NONRESOLVING_PROSE`` as a bare
    phrase, a wrong citation within ``_NONRESOLVING_WINDOW`` of any such
    sentence was exempt from both halves of the check, with no CI signal
    saying so.

    Each now has to name what the erratum is judging — the citation
    itself, or the code the citation missed. Both directions are pinned
    per marker: the ordinary sentence must leave the citation checked,
    and the erratum construction must still suppress it, because a
    tightening that only managed the first would have broken the prose
    these markers exist for.
    """
    decoy = _crc32_shard_line() + 200
    cited = f"a recorder picks its shard by `crc32(session_id)` (`events.py:{decoy}`)"
    assert len(check_line_refs("docs/fake.md", cited)) == 1

    loose_near = "the fallback is nowhere near as fast, and " + cited
    assert re.search(r"\bnowhere\s+near\b", loose_near), "fixture lost the phrase"
    assert len(check_line_refs("docs/fake.md", loose_near)) == 1
    tight_near = cited + ", nowhere near the `Recorder.__post_init__` it names"
    assert check_line_refs("docs/fake.md", tight_near) == []

    loose_short = "the documented shape stops short of the attestation block; " + cited
    assert re.search(r"\bshort\s+of\s+the\b", loose_short), "fixture lost the phrase"
    assert len(check_line_refs("docs/fake.md", loose_short)) == 1
    tight_short = cited + ", short of the `Recorder.__post_init__` it names"
    assert check_line_refs("docs/fake.md", tight_short) == []

    loose_narrowed = "the triage narrowed it to the recorder, and " + cited
    assert re.search(r"\bnarrowed\s+it\s+to\b", loose_narrowed), "fixture lost it"
    assert len(check_line_refs("docs/fake.md", loose_narrowed)) == 1
    tight_narrowed = (
        f"a later commit narrowed it to `events.py:{decoy}` for the "
        "`crc32(session_id)` shard pick"
    )
    assert check_line_refs("docs/fake.md", tight_narrowed) == []
    # Same sentence, marker removed: proves the marker does the
    # suppressing rather than the citation never having been checked.
    unmarked = tight_narrowed.replace("narrowed it to", "cites")
    assert len(check_line_refs("docs/fake.md", unmarked)) == 1


def test_straddles_and_different_function_must_name_what_they_judge() -> None:
    """Two markers that were neutral description before they were verdicts.

    ``straddles`` is house geometry vocabulary here — a turn straddles a
    log rotation, a citation straddles the body-scan cap — and "a
    different function" is unremarkable engineering English ("we moved
    that helper to a different function"). Neither reads as a judgement
    on a citation on its own, yet while each sat in
    ``_NONRESOLVING_PROSE`` as a bare phrase, a wrong citation within
    ``_NONRESOLVING_WINDOW`` of any such sentence was exempt from both
    halves of the check, with no CI signal saying so.

    ``straddles`` now takes the citation as its grammatical subject, and
    the appositive has to be predicated on the backticked identifier
    naming what the citation hit instead. Both directions are pinned per
    marker: the ordinary sentence must leave the citation checked, and
    the erratum construction — the shape the swarm plan's errata
    actually use — must still suppress it. Each erratum fixture is
    re-asserted with only its marker words removed, so a fixture that
    had quietly stopped being checkable at all could not pass for a
    working suppression.
    """
    decoy = _crc32_shard_line() + 200
    cited = f"a recorder picks its shard by `crc32(session_id)` (`events.py:{decoy}`)"
    assert len(check_line_refs("docs/fake.md", cited)) == 1

    loose_straddle = "a turn that straddles a rotation keeps its event; " + cited
    assert re.search(r"\bstraddles\b", loose_straddle), "fixture lost the phrase"
    assert len(check_line_refs("docs/fake.md", loose_straddle)) == 1
    tight_straddle = (
        f"`events.py:{decoy}-{decoy + 20}` straddles two functions, opening "
        "past the `crc32(session_id)` shard pick it was cited for"
    )
    assert check_line_refs("docs/fake.md", tight_straddle) == []
    unmarked_straddle = tight_straddle.replace(
        " straddles two functions, opening", " covers the region opening"
    )
    assert len(check_line_refs("docs/fake.md", unmarked_straddle)) == 1

    loose_different = "we moved that helper to a different function, and " + cited
    assert re.search(r"\ba\s+different\s+function\b", loose_different), "lost it"
    assert len(check_line_refs("docs/fake.md", loose_different)) == 1
    tight_different = (
        f"the doc cited `events.py:{decoy}` for the `crc32(session_id)` shard "
        "pick, but it lands in `Recorder._next_rotation_paths`, a different "
        "function"
    )
    assert check_line_refs("docs/fake.md", tight_different) == []
    unmarked_different = tight_different.replace(", a different function", " instead")
    assert len(check_line_refs("docs/fake.md", unmarked_different)) == 1


def test_citation_pinned_to_a_named_commit_is_not_checked() -> None:
    """A citation resolved against a named commit is not a HEAD claim.

    Real corpus shape: the swarm errata quote shipped citations as
    *landing* — resolved against a named commit, with no non-resolving
    verdict anywhere near, because the verdict is that they hold — and
    drift in the cited file would fail the check on prose that is true
    about the pinned tree. The unpinned mutation of each fixture must
    stay red: that is what proves the pin is doing the suppressing,
    rather than the citation never having been checked at all.
    """
    decoy = _crc32_shard_line() + 200
    pinned = (
        "resolved against `0123abc` with an AST walk, two land: "
        f"`events.py:{decoy}` is exactly the `crc32(session_id)` shard pick"
    )
    assert check_line_refs("docs/fake.md", pinned) == []
    unpinned = (
        f"two land: `events.py:{decoy}` is exactly the `crc32(session_id)` shard pick"
    )
    assert len(check_line_refs("docs/fake.md", unpinned)) == 1
    linked = (
        "resolved against `0123abc` with an AST walk, two land: "
        f"[events.py:{decoy}](../src/bettermemory/events.py) is exactly the "
        "`crc32(session_id)` shard pick"
    )
    assert check_line_refs("docs/fake.md", linked) == []
    linked_unpinned = (
        "two land: "
        f"[events.py:{decoy}](../src/bettermemory/events.py) is exactly the "
        "`crc32(session_id)` shard pick"
    )
    assert len(check_line_refs("docs/fake.md", linked_unpinned)) == 1
    # A file can shrink between the pinned commit and HEAD, so the range
    # half is suppressed too — same contract as the non-resolving rule.
    out_of_range = (
        "resolved against `0123abc`, `events.py:999999` was exactly the "
        "`crc32(session_id)` shard pick"
    )
    assert check_line_refs("docs/fake.md", out_of_range) == []
    # The errata's other phrasing must pin on its own as well.
    alt = (
        "every resolution above is pinned to a named commit; two land: "
        f"`events.py:{decoy}` is exactly the `crc32(session_id)` shard pick"
    )
    assert check_line_refs("docs/fake.md", alt) == []


def test_commit_pin_does_not_cross_a_paragraph_boundary() -> None:
    """The pin is paragraph-scoped, never a document-wide pass.

    A pin declares the reference frame for its own analysis, and a blank
    line ends that analysis: the same wrong citation one paragraph below
    the pin stays red — the false-negative direction this rule must not
    have. The middle assertion pins that the far text really does carry
    a pin phrase, so this test cannot rot into vacuity.
    """
    decoy = _crc32_shard_line() + 200
    plain = f"a recorder picks its shard by `crc32(session_id)` (`events.py:{decoy}`)"
    text = "the survey was resolved against `0123abc` in full.\n\n" + plain
    assert _COMMIT_PINNED_PROSE.search(text) is not None
    assert len(check_line_refs("docs/fake.md", text)) == 1


def test_commit_pin_requires_a_backticked_sha() -> None:
    """ "resolved against the earlier tree" pins nothing.

    The backticked-hex form is load-bearing: without it, any sentence
    about resolving one thing against another would quietly turn its
    whole paragraph into unchecked prose.
    """
    decoy = _crc32_shard_line() + 200
    text = (
        "resolved against the earlier tree, two land: "
        f"`events.py:{decoy}` is exactly the `crc32(session_id)` shard pick"
    )
    assert _COMMIT_PINNED_PROSE.search(text) is None
    assert len(check_line_refs("docs/fake.md", text)) == 1


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
