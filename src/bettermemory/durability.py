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

That protocol has been executed once, and the retirement it produced is
the worked example: see the `SHA_MARKER` comment below for what the
commit-SHA detector's own event log said about it, and what had to be
kept afterwards so the evidence stays readable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


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
    # profile as "currently", except the habitual present-tense form
    # ("the rate limiter temporarily blocks an IP") describes designed
    # recurring behavior and is exempted via _PATTERN_OVERRIDES.
    # "interim" is deliberately absent ("interim report", "interim CTO"
    # are durable). The dated form "as of <date>" is handled by
    # _AS_OF_DATE_RE below; "as of today" / "as of yesterday" are
    # subsumed by the bare time-word markers.
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
    # In-flight work references. "wip" is deliberately absent and
    # "in progress" is copula-anchored for the same reason: kanban
    # column descriptions ("Todo, In Progress, and Done columns") and
    # WIP-limit conventions are durable, while the copula forms ("the
    # migration is in progress") carry the transience. Known residual:
    # hyphenated "in-progress" doesn't match the literal space. The two
    # idioms only fire with an in-flight-work object — see
    # _PATTERN_OVERRIDES; bare uses are dominated by durable
    # temporal-generic phrasing ("pinged in the middle of a focus
    # block", "reboots halfway through").
    "is in progress",
    "are in progress",
    "in the middle of",
    "halfway through",
    # Branch/repo state references. Bare "unpushed" catches every word
    # order ("is unpushed", "has unpushed commits") with one slot — the
    # word has essentially no durable usage. The copula forms for stash
    # keep durable policy facts ("prefers stashing WIP") silent. "dirty
    # working tree" / "not committed" are deliberately absent — both
    # appear in durable tool-behavior facts. Bare "uncommitted changes"
    # / "untracked files" share that dual-use profile (deploy guards
    # that "refuse to run when there are uncommitted changes", git-clean
    # caveats, stash policies), so both are copula-anchored — with an
    # asymmetry: "are uncommitted changes" is deliberately missing
    # because the existential form is how CI/deploy guards state their
    # trigger condition, while "are untracked files" stays because the
    # existential there-are form is the natural transient repo-state
    # report ("There are untracked files under scripts/").
    "unpushed",
    "commits ahead",
    "commits behind",
    "has uncommitted changes",
    "have uncommitted changes",
    "has untracked files",
    "have untracked files",
    "are untracked files",
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


# RETIRED marker name — read-side only. Nothing produces it any more.
#
# The commit-SHA detector was removed after its own telemetry condemned it,
# executing the tuning protocol this module's docstring has carried since it
# was written ("a high override rate is a signal that a marker is producing
# too many false positives and should be removed").
#
# The write-side record, closed: 47 fires, 45 overrides. Read that as 45 of
# 47 blocks overridden — `MarkerStats.override_rate` divides by fires PLUS
# overrides, so its 0.489 is 97.8% of the 0.500 that metric can reach when
# every block is answered. Pooled across the phrase markers the same metric
# is 0.161 (52 fires / 10 overrides), so this marker ran 3.0x the rest of
# the list. 36 of the 47 blocks were answered by an explicit override in the
# same session, median gap 25 seconds. A further 9 of the 45 overrides have
# no preceding block at all — the caller had begun passing
# `acknowledge_transient=True` pre-emptively, 7 of those in the final
# fortnight. That is the rubber-stamp the marker-list comment above calls
# worse than not having the marker.
#
# The corpus said the same thing from the other side. Of 210 accepted bodies
# in the dogfood store, 79 contained text this detector fired on: 66
# referential ("the fix landed in 68aff13"), 10 positional ("main is at
# 68aff13"), 3 incidental (a restic snapshot id, a Cloudflare build id, a
# container image tag). Only the positional class was ever the target, and 2
# of those 10 were already caught by another marker in the same body — so 8
# firings in 79 were catches nothing else would make. 64 of the 79 bodies
# carry `verified_commits` attestations: this project's own verification
# system treats commit identity as a durable anchor, and the write gate was
# arguing with it and losing.
#
# The name survives because `canonical_marker` folds 92 historical events
# (54 distinct raw names, 21 of them singletons) onto it. Do NOT wire it
# back into `find_transient_markers`: new fires would mix into that closed
# row and destroy the evidence above. A future SHA-shaped detector needs a
# new name and its own telemetry — and the better home for the one class
# this did catch is read-side, where a body-cited SHA is a resolvable commit
# rather than a regex judging English.
SHA_MARKER = "sha:<commit>"


# Events written before SHA_MARKER existed carry the hash in the name
# (`sha:874b0b0`) — seven chars of `sha[:7]`, all-digit prefixes included.
# The canonical name is not itself of this shape, so the fold below is
# idempotent.
_LEGACY_SHA_MARKER_RE = re.compile(r"^sha:[0-9a-f]{7}$")


def canonical_marker(marker: str) -> str:
    """Fold a stored event's marker name onto its current canonical form.

    Read-side only: the event log is append-only and is NOT rewritten.
    Aggregations key on the marker name, so without this fold every SHA
    event written before the bucketing fix stays its own row forever and
    the class the fix exists to make measurable reads as singletons.

    The detector that minted these names is gone, so this is now pure
    archive work: the row it produces is closed at 47 fires / 45 overrides
    and will never grow. That closed row is the evidence for the removal,
    which is exactly why the fold must not be simplified away as dead code
    — it has no producer left, and that is the point rather than a defect.
    """
    return SHA_MARKER if _LEGACY_SHA_MARKER_RE.match(marker) else marker


# Dated state snapshots ("as of 2026-06-09 the cluster is on k8s 1.29")
# are canonical transient content, bucketed under one marker rather than
# one per date. Bare "as of" deliberately stays unmatched: version-pinned
# forms ("as of 2.7.0", "as of Python 3.12") are durable.
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
# - "temporarily": the habitual present-tense third-person form
#   ("the rate limiter temporarily blocks an IP") describes designed
#   recurring system behavior — durable, and unlike "currently" the word
#   can't just be deleted in a rephrase without flipping the meaning
#   (temporary -> permanent). The \w+s lookahead skips exactly that
#   conjugation; past/progressive/imperative forms ("temporarily
#   disabled", "we're temporarily using") keep firing.
# - "in the middle of" / "halfway through": the bare idioms are dominated
#   by durable temporal-generic uses ("pinged in the middle of a focus
#   block", "the installer reboots halfway through"). They only fire with
#   an in-flight-work object: a bare gerund immediately after of/through
#   ("migrating the database" — morning/evening excluded, they share the
#   -ing shape) or an optionally articled work noun ("a migration",
#   "the rollout").

# In-flight-work object shape shared by the two idiom overrides.
_IN_FLIGHT_WORK_OBJECT = (
    r"(?:(?!(?:morning|evening)\b)\w+ing\b"
    r"|(?:(?:a|an|the)\s+)?(?:migration|refactor(?:ing)?|rewrite|upgrade"
    r"|rollout|deploy(?:ment)?|rebase|merge|release)\b)"
)

_PATTERN_OVERRIDES: dict[str, re.Pattern[str]] = {
    "today": re.compile(r"\btoday\b(?!['’]s|\.\w)", re.IGNORECASE),
    "the new": re.compile(r"\b[Tt]he new\b"),
    "at the moment": re.compile(
        r"\bat the moment\b(?!\s+(?:of|when|that)\b)", re.IGNORECASE
    ),
    "temporarily": re.compile(r"\btemporarily\b(?!\s+\w+s\b)", re.IGNORECASE),
    "in the middle of": re.compile(
        rf"\bin the middle of {_IN_FLIGHT_WORK_OBJECT}", re.IGNORECASE
    ),
    "halfway through": re.compile(
        rf"\bhalfway through {_IN_FLIGHT_WORK_OBJECT}", re.IGNORECASE
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

    `marker` is the canonical phrase from `TRANSIENT_PHRASE_MARKERS`, or
    `"as of <date>"` for dated state snapshots. `snippet` is up to ~40 chars
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
    if i < 0 or text[i] in _SENTENCE_BOUNDARY_CHARS:
        return True
    # Em/en dashes (Pd: — – ―) and emoji/symbol bullets (So: 🚀 ✅ ▪ ◦)
    # open a sentence the same way the ASCII hyphen/asterisk bullets in
    # the frozenset do. Sm (math symbols, e.g. →) is deliberately
    # excluded — narrow scope: an arrow points INTO a continuation of
    # the same clause, not at a fresh sentence.
    return unicodedata.category(text[i]) in ("Pd", "So")


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

    return hits


__all__ = [
    "SHA_MARKER",
    "TRANSIENT_PHRASE_MARKERS",
    "TransientMatch",
    "canonical_marker",
    "find_transient_markers",
]
