"""Structural durability check for memory_write.

The transient-marker rule used to live in `prompts.py` and the tool
description — it relied on the model spotting "currently", "today I", etc.
in a body it was about to write, and aborting on its own. That's
aspirational, not enforced; under task focus the reflective step gets
skipped and a transient body slips through.

This module moves the rule into code:

- `TRANSIENT_PHRASE_MARKERS` is the canonical list. The system prompt
  references the principle but doesn't enumerate phrases; the tool tells
  the caller which marker fired.
- `find_transient_markers(body)` returns the hits, or empty if the body
  is durable. Word-boundary regex per phrase, so "currently" doesn't fire
  inside "concurrently" and "new" doesn't fire inside "news".
- `memory_write` calls this before dedup. If anything fires and
  `acknowledge_transient` is not set, it returns
  `{status: "transient_warning", markers: [...]}` instead of committing.
  The caller either rephrases the body to extract the level-up durable
  form, or sets `acknowledge_transient=True` if the marker is genuinely
  durable in context (rare).

Telemetry: every fire AND every override is logged to `.events.jsonl`. A
high override rate is a signal that a marker is producing too many false
positives and should be removed; a low fire rate is a signal we should
expand the list. Tune against real traffic, not vibes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import chain


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
#
# Adding to this list: only do it for phrases whose meaning will drift in a
# week if nobody updates the memory. Each addition costs one false-positive
# slot — a phrase that's transient in some contexts and durable in others
# will trip writes that shouldn't be tripped, and the user will learn to
# rubber-stamp `acknowledge_transient=True`. That's worse than not having
# the marker. Watch the override rate in the event log; trim if it climbs.

TRANSIENT_PHRASE_MARKERS: tuple[str, ...] = (
    # Direct timestamp / state markers. "temporarily" / "for the time
    # being" are the author labeling the state transient — same FP
    # profile as "currently". "interim" is deliberately absent ("interim
    # report", "interim CTO" are durable). The dated form "as of <date>"
    # is handled by _AS_OF_DATE_RE below; "as of today" / "as of
    # yesterday" are subsumed by the bare time-word markers.
    "currently",
    "as of now",
    "as of this writing",
    "as of writing",
    "right now",
    "for now",
    "temporarily",
    "for the time being",
    "at the moment",
    # Time-of-writing references. "today" covers the whole adverb family
    # — fronted ("Today, I ..."), medial, and trailing ("merged ...
    # today") — via a pattern override below that excludes the
    # possessive ("today's date"), the dominant durable use. "next year"
    # is deliberately absent, mirroring the exclusion of "this year"
    # (year-granularity drift exceeds the one-week bar).
    "today",
    "this morning",
    "this afternoon",
    "tonight",
    "yesterday",
    "tomorrow",
    "this week",
    "this month",
    "last week",
    "next week",
    "next month",
    # Recent-action references. Bare "recently" is deliberately absent —
    # it is dual-use ("least-recently-used", a "Recently Viewed" panel)
    # — so only the aux+recently and recently+action-verb bigrams, which
    # are no more dual-use than "we just", are markers.
    "i just",
    "we just",
    "just shipped",
    "the latest",
    "was recently",
    "were recently",
    "has recently",
    "have recently",
    "recently switched",
    "recently migrated",
    "recently renamed",
    # In-flight work references. "wip" is deliberately absent: kanban
    # column descriptions and WIP-limit conventions are durable. Known
    # residual: hyphenated "in-progress" doesn't match the literal space.
    "in progress",
    "in the middle of",
    "halfway through",
    # Branch/repo state references. Bare "unpushed" catches every word
    # order ("is unpushed", "has unpushed commits") with one slot — the
    # word has essentially no durable usage. The copula forms for stash
    # keep durable policy facts ("prefers stashing WIP") silent. "dirty
    # working tree" / "not committed" are deliberately absent — both
    # appear in durable tool-behavior facts.
    "unpushed",
    "commits ahead",
    "commits behind",
    "uncommitted changes",
    "untracked files",
    "is stashed",
    "are stashed",
    # New-thing references — these are the subtle ones. Plural/first-
    # person-plural conjugations ("we now use") carry the same staleness
    # as the third-person-singular forms.
    "the new",
    "now uses",
    "now use",
    "now using",
    "now relies",
    "now rely",
    "now relying",
)


# Bare hex strings that look like commit SHAs (7-40 hex chars at a word
# boundary). Conservative lower bound: 7 is the git short-SHA default. The
# upper bound stops it from matching long unrelated hex blobs (sha-512
# digests, hex-encoded keys). Lowercase only — git short SHAs are
# conventionally lowercase, and locking on case avoids false positives on
# uppercase identifiers like ULIDs (which use 0-9A-Z but not a-f).
#
# The leading lookahead requires at least one a-f letter inside the run, so
# a purely-decimal token (a Unix epoch like 1700000000, a phone number, a
# large numeric id, an error code) can't masquerade as a SHA. Digits are a
# subset of the hex class, so without this guard those durable numbers would
# fail closed against exactly the content the gate is meant to admit.
#
# Two further exemptions, same rationale (durable identifiers must not fail
# closed): UUIDs are masked out before the scan (see _UUID_RE), and maximal
# exactly-32-hex runs are skipped in the scan loop — that's MD5 /
# machine-id / gist-id length, never a git ref (git emits 7-12 char short
# SHAs and 40-char full SHAs).
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


# `git describe` output (v3.7.1-5-g874b0b0) embeds the abbreviated hash
# behind a literal 'g'; 'g' is a word character outside [0-9a-f], so
# _SHA_RE's \b can never anchor there even though the string is the most
# common machine-generated spelling of pure branch state. The lookbehind
# keeps the bare hex as the match, so the sha:<prefix> marker format is
# unchanged.
_DESCRIBE_SHA_RE = re.compile(r"(?<=-g)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")


# UUIDs (8-4-4-4-12 hex) are permanent identifiers — KMS keys, tenant ids,
# content hashes — and structurally distinguishable from a commit SHA, but
# hyphens are word boundaries, so each >=7-char segment of a lowercase UUID
# would match _SHA_RE on its own. The SHA pass therefore runs over a copy
# with UUID spans blanked out (offset-preserving, so snippets still index
# the original text). Case-tolerant: mixed-case UUIDs still carry lowercase
# segments that would otherwise match.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


# Dated state snapshots ("as of 2026-06-09 the cluster is on k8s 1.29")
# are canonical transient content; bucketed under one marker like the SHA
# loop. Bare "as of" deliberately stays unmatched: version-pinned forms
# ("as of 2.7.0", "as of Python 3.12") are durable.
_AS_OF_DATE_RE = re.compile(r"\bas of \d{4}-\d{2}-\d{2}\b", re.IGNORECASE)
_AS_OF_DATE_MARKER = "as of <date>"


# Per-marker pattern overrides. Most phrases compile to the generic
# word-boundary pattern below; these need extra shape:
#
# - "today": the whole adverb family in any position ("Today, I ...",
#   "earlier today", trailing "merged ... today"), minus the possessive
#   ("today's metrics" — the dominant durable use) and domain names.
# - "the new": IGNORECASE exists for sentence-start capitalization, but
#   "the New <X>" with a capital N is a proper noun (the New York office,
#   The New Yorker), not a new-thing reference. Requiring lowercase "new"
#   keeps sentence-initial "The new schema" firing; accepted trade-off:
#   all-caps "THE NEW SCHEMA" no longer fires.
# - "at the moment": "at the moment of/when/that <event>" is an
#   event-trigger clause describing durable behavior, never the now-sense,
#   so suppressing those heads costs zero recall. The ambiguous a/an/the
#   heads ("At the moment the plan is ...") deliberately keep firing.
_PATTERN_OVERRIDES: dict[str, re.Pattern[str]] = {
    "today": re.compile(r"\btoday\b(?!['’]s|\.\w)", re.IGNORECASE),
    "the new": re.compile(r"\b[Tt]he new\b"),
    "at the moment": re.compile(
        r"\bat the moment\b(?!\s+(?:of|when|that)\b)", re.IGNORECASE
    ),
}


# Pre-compile phrase regexes with word boundaries. Word boundaries stop
# "currently" from matching inside "concurrently" and "new" from matching
# inside "news"; case-insensitive matches "Currently", "CURRENTLY", etc.
# The trailing (?!\.\w) lookahead keeps domain-name homonyms silent
# ("tomorrow.io", "today.dev") at zero recall cost — sentence-final
# "Deploy tomorrow." still fires because its period is followed by
# whitespace or end-of-text, not a word character.
_PHRASE_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        phrase,
        _PATTERN_OVERRIDES.get(phrase)
        or re.compile(rf"\b{re.escape(phrase)}\b(?!\.\w)", re.IGNORECASE),
    )
    for phrase in TRANSIENT_PHRASE_MARKERS
)


# Time-word markers additionally get a post-match title-case check: proper
# nouns built on time words ("Tomorrow Night", "This Week in Rust") are
# durable facts where the name itself is the content — structurally
# unfixable by rephrasing, so every fire would train an
# acknowledge_transient rubber-stamp, the failure mode the marker-list
# comment calls worse than no marker.
_TITLECASE_SKIP_MARKERS: frozenset[str] = frozenset(
    {
        "today",
        "tonight",
        "yesterday",
        "tomorrow",
        "this week",
        "this month",
        "last week",
        "next week",
        "next month",
    }
)


@dataclass(frozen=True)
class TransientMatch:
    """One transient-marker hit against a candidate write.

    `marker` is the canonical phrase from `TRANSIENT_PHRASE_MARKERS`,
    `"sha:<7-char prefix>"` for SHA matches, or `"as of <date>"` for dated
    state snapshots. `snippet` is up to ~40 chars
    of surrounding context — surfaced in the tool error so the caller can
    see exactly what tripped.
    """

    marker: str
    snippet: str


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


_CONTEXT_CHARS = 20


def _snippet_around(text: str, start: int, end: int) -> str:
    """Carve out a few words on either side of a match for the error message.

    Strips newlines and collapses whitespace so the snippet is one line in
    the tool response. The exact match is preserved verbatim (including
    case) — only the surrounding padding is normalized.
    """
    s = max(0, start - _CONTEXT_CHARS)
    e = min(len(text), end + _CONTEXT_CHARS)
    chunk = text[s:e].replace("\n", " ").strip()
    # Collapse runs of whitespace.
    chunk = re.sub(r"\s+", " ", chunk)
    prefix = "..." if s > 0 else ""
    suffix = "..." if e < len(text) else ""
    return f"{prefix}{chunk}{suffix}"


# Characters that can precede a sentence-initial word: terminators of the
# previous sentence, newlines, list bullets, and opening quotes/brackets.
_SENTENCE_BOUNDARY_CHARS = frozenset(".!?:;\n-*•\"'“([")

_NEXT_WORD_RE = re.compile(r"\s+([\w'’-]+)")


def _at_sentence_start(text: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i < 0 or text[i] in _SENTENCE_BOUNDARY_CHARS


def _is_titlecase_name(text: str, match: re.Match[str]) -> bool:
    """True when a time-word hit reads as a proper-noun name, not the time
    adverb: "Tomorrow Night", "This Week in Rust", "the tomorrow.io API"
    minus the domain part (that one is handled by the compile-time
    lookahead).

    Conservative on purpose: lowercase and ALL-CAPS matches never skip, and
    a capitalized match at sentence start only skips when the next word is
    itself title-case (and not the pronoun "I"), so "Tomorrow we ship" and
    "Yesterday I broke the build" keep firing.
    """
    words = match.group().split()
    if len(words) > 1:
        # The adverbial reading never capitalizes interior words
        # ("This week ..."); titles do ("This Week in Rust").
        return any(w.istitle() for w in words[1:])
    if not words[0].istitle():
        return False
    if not _at_sentence_start(text, match.start()):
        # Mid-sentence capitalization is a proper noun ("the Tomorrow
        # Night theme", "a USA Today column").
        return True
    follower = _NEXT_WORD_RE.match(text, match.end())
    if follower is None:
        return False
    nxt = follower.group(1)
    return nxt != "I" and nxt.istitle()


def find_transient_markers(content: str) -> list[TransientMatch]:
    """Scan `content` for transient-state markers.

    Returns a list of `TransientMatch`. Empty list means the body is
    durable enough to write — no markers fired. Hits are deduplicated by
    canonical `marker` value: if "currently" appears three times in one
    body, we report it once with the first snippet, not three times.
    """
    hits: list[TransientMatch] = []
    seen: set[str] = set()

    for canonical, regex in _PHRASE_REGEXES:
        if canonical in seen:
            continue
        for match in regex.finditer(content):
            if canonical in _TITLECASE_SKIP_MARKERS and _is_titlecase_name(
                content, match
            ):
                continue
            hits.append(
                TransientMatch(
                    marker=canonical,
                    snippet=_snippet_around(content, match.start(), match.end()),
                )
            )
            seen.add(canonical)
            break

    as_of = _AS_OF_DATE_RE.search(content)
    if as_of is not None:
        hits.append(
            TransientMatch(
                marker=_AS_OF_DATE_MARKER,
                snippet=_snippet_around(content, as_of.start(), as_of.end()),
            )
        )

    # The SHA pass runs over a UUID-masked copy: the substitution is
    # offset-preserving, so snippets still index the original text.
    masked = _UUID_RE.sub(lambda m: " " * len(m.group()), content)
    for match in chain(_SHA_RE.finditer(masked), _DESCRIBE_SHA_RE.finditer(masked)):
        sha = match.group()
        if len(sha) == 32:
            # A maximal exactly-32-hex run is MD5 / machine-id / gist-id
            # length — a durable artifact identifier, never a git ref.
            continue
        # Bucket all SHA hits under one canonical marker so a body listing
        # five commit SHAs doesn't produce five entries — one is enough to
        # tell the caller "you're putting branch state in memory".
        hits.append(
            TransientMatch(
                marker=f"sha:{sha[:7]}",
                snippet=_snippet_around(content, match.start(), match.end()),
            )
        )
        break

    return hits


__all__ = [
    "TRANSIENT_PHRASE_MARKERS",
    "TransientMatch",
    "find_transient_markers",
]
