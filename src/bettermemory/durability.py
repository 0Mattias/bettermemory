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
    # Direct timestamp / state markers.
    "currently",
    "as of now",
    "right now",
    "for now",
    "at the moment",
    # Time-of-writing references.
    "today i",
    "today we",
    "this morning",
    "this afternoon",
    "yesterday",
    "tomorrow",
    "this week",
    "this month",
    "last week",
    # Recent-action references.
    "i just",
    "we just",
    "just shipped",
    "shipped today",
    "the latest",
    # Branch/repo state references.
    "is unpushed",
    "commits ahead",
    "commits behind",
    # New-thing references — these are the subtle ones.
    "the new",
    "now uses",
    "now using",
    "now relies",
)


# Bare hex strings that look like commit SHAs (7-40 hex chars at a word
# boundary). Conservative lower bound: 7 is the git short-SHA default. The
# upper bound stops it from matching long unrelated hex blobs (sha-512
# digests, hex-encoded keys). Lowercase only — git short SHAs are
# conventionally lowercase, and locking on case avoids false positives on
# uppercase identifiers like ULIDs (which use 0-9A-Z but not a-f).
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


# Pre-compile phrase regexes with word boundaries. Word boundaries stop
# "currently" from matching inside "concurrently" and "new" from matching
# inside "news"; case-insensitive matches "Currently", "CURRENTLY", etc.
_PHRASE_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (phrase, re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE))
    for phrase in TRANSIENT_PHRASE_MARKERS
)


@dataclass(frozen=True)
class TransientMatch:
    """One transient-marker hit against a candidate write.

    `marker` is the canonical phrase from `TRANSIENT_PHRASE_MARKERS`, or
    `"sha:<7-char prefix>"` for SHA matches. `snippet` is up to ~40 chars
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
        match = regex.search(content)
        if match is None or canonical in seen:
            continue
        hits.append(
            TransientMatch(
                marker=canonical,
                snippet=_snippet_around(content, match.start(), match.end()),
            )
        )
        seen.add(canonical)

    for match in _SHA_RE.finditer(content):
        sha = match.group()
        # Bucket all SHA hits under one canonical marker so a body listing
        # five commit SHAs doesn't produce five entries — one is enough to
        # tell the caller "you're putting branch state in memory".
        if "sha" in seen:
            break
        hits.append(
            TransientMatch(
                marker=f"sha:{sha[:7]}",
                snippet=_snippet_around(content, match.start(), match.end()),
            )
        )
        seen.add("sha")

    return hits


__all__ = [
    "TRANSIENT_PHRASE_MARKERS",
    "TransientMatch",
    "find_transient_markers",
]
