"""Phrase-match attribution of retrieved memories to assistant reply text.

The MCP contract asks the model to attach `claim_excerpts` on
`memory_record_use` when a retrieved memory shaped a sentence in the
reply. In practice models skip this step — the explicit form costs
three steps (tool call + construct parallel list + remember), the
auto-commit covers the basic case, and the model defaults to the
free path. The result is `memory_helped_rate` reading 0% in dogfood
not because the metric is broken but because the contract puts the
load-bearing audit data on AI behavior the AI doesn't naturally
produce.

The Stop hook closes this gap post-hoc. After every turn, the hook
already has the assistant's reply text (it reads the transcript for
silent-miss detection); the retrieved memories are visible in the
event log. For each retrieved memory, extract distinctive phrases
from its body and substring-match against the reply. When a phrase
matches, the hook can emit a `memory_record_use` event with
`outcome="applied"`, `claim_excerpts=[matched_phrase]`,
`attribution="hook"` — the same shape an explicit model call would
have produced, plus the attribution flag so downstream eval can
distinguish heuristic attribution from model-explicit attribution.

This module is the matcher. Pure function, no I/O, no event recording
— that lives on the hook side. Returns one `AttributionMatch` per
(memory, matched-sentence) pair.

Heuristic precision over recall — better to miss a load-bearing
retrieval than to falsely attribute one. Thresholds tuned to keep
false positives low:

- Candidate sentences from the body must be ≥6 tokens AND ≥30 chars.
  Shorter or shallower sentences are statistically too likely to
  appear in arbitrary text.
- Substring match is case-insensitive and whitespace-normalised
  (collapse runs of whitespace to single space) so a paraphrased
  formatting difference doesn't break the match.
- Sentences where ≥80% of tokens are stopwords are filtered — a
  high-stopword sentence isn't distinctive enough to attribute on
  substring match alone.

One match per memory: the first matching sentence wins. The event log
records the matched sentence as the `claim_excerpt`, capped at 500
characters to match the `memory_record_use` contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


__all__ = ["AttributionMatch", "attribute_uses"]


@dataclass(frozen=True)
class AttributionMatch:
    """One phrase-match between a memory body and the assistant reply.

    `memory_id` is the matched memory. `claim_excerpt` is the
    body-sentence whose normalised form appeared in the reply text;
    capped at 500 chars to match the `memory_record_use` excerpt
    limit.
    """

    memory_id: str
    claim_excerpt: str


# Conservative stopword filter. Same shape as the search ranker's
# query-side filter — common English filler that doesn't carry
# attributive weight on its own. Kept frozen for cheap membership
# tests in the hot path.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "them",
        "us",
        "me",
        "my",
        "your",
        "his",
        "her",
        "their",
        "our",
        "not",
        "no",
        "yes",
        "can",
        "could",
        "would",
        "should",
        "may",
        "might",
        "will",
        "shall",
        "so",
        "than",
        "too",
        "very",
        "just",
        "also",
        "only",
        "any",
        "all",
    }
)

_MIN_TOKEN_COUNT = 6
_MIN_CHAR_COUNT = 30
_MAX_EXCERPT_CHARS = 500
# Sentences with >= this fraction stopwords are filtered out. 0.80
# allows a token-dense technical sentence ("the auth middleware lives
# in src/auth/middleware.py" — 8 tokens, 2 stopwords = 25% stopword)
# through while rejecting "this is the thing that we did and it was
# fine" (10 tokens, 9 stopwords = 90%).
_STOPWORD_RATIO_MAX = 0.80

# Sentence splitter. Splits on terminal punctuation followed by space,
# OR on one-or-more newlines. The trailing-space requirement preserves
# decimal numbers (1.5) and version strings (v2.6.0) — the dot there
# is followed by a digit, not whitespace. Abbreviations like "Dr.
# Smith" or "e.g. foo" DO split: that's accepted. Over-split is better
# than under-split for attribution because a longer sentence that
# fails the candidate filter at one boundary still has the same
# boundary tested from the other side, and memory bodies in this
# corpus rarely contain prose abbreviations of that shape.
_SENTENCE_SPLIT_RE = re.compile(r"(?:[.!?]\s+|\n+)")

# Strip trailing punctuation tokens for the stopword ratio check, so
# "config." reads as "config" before lookup.
_PUNCT_STRIP_RE = re.compile(r"^[^\w]+|[^\w]+$")


def _candidate_sentences(body: str) -> list[str]:
    """Pull distinctive candidate sentences from a memory body.

    Each candidate satisfies all of:
    - length ≥ ``_MIN_CHAR_COUNT``,
    - token count ≥ ``_MIN_TOKEN_COUNT``,
    - stopword ratio < ``_STOPWORD_RATIO_MAX``.

    Returns sentences in body order, capped at ``_MAX_EXCERPT_CHARS``
    each so the resulting events stay within the `claim_excerpts`
    limit. Returns an empty list for empty / pathological bodies.
    """
    if not body:
        return []
    out: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(body):
        sentence = raw.strip()
        if len(sentence) < _MIN_CHAR_COUNT:
            continue
        tokens = sentence.split()
        if len(tokens) < _MIN_TOKEN_COUNT:
            continue
        stopword_count = 0
        for token in tokens:
            cleaned = _PUNCT_STRIP_RE.sub("", token).lower()
            if cleaned in _STOPWORDS:
                stopword_count += 1
        if stopword_count / len(tokens) >= _STOPWORD_RATIO_MAX:
            continue
        # Strip terminal punctuation so a sentence ending in `.` still
        # matches against a reply that quoted the same content followed
        # by `—` or `,` or any other continuation. Mid-string `.` (paths
        # like `src/foo.py`, versions like `v1.5`) is preserved.
        cleaned = sentence.rstrip(".!?").strip()
        if len(cleaned) < _MIN_CHAR_COUNT:
            continue
        out.append(cleaned[:_MAX_EXCERPT_CHARS])
    return out


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for fuzzy substring comparison.

    Whitespace collapse means a sentence that was hard-wrapped in the
    memory body still matches the same sentence reflowed in the
    assistant reply. Lowercasing prevents an isolated capitalization
    difference from breaking the match.
    """
    return re.sub(r"\s+", " ", text.lower()).strip()


def attribute_uses(
    memories: dict[str, str],
    assistant_text: str,
) -> list[AttributionMatch]:
    """Find which retrieved memories' bodies overlap the assistant reply.

    ``memories`` maps memory_id → body. ``assistant_text`` is the
    flattened text of the assistant turn (no thinking / tool-use
    blocks). Returns one ``AttributionMatch`` per (memory, matched
    sentence) pair; a single memory produces at most one match (the
    first matching sentence wins) to keep the event log lean.

    Returns an empty list when ``assistant_text`` is empty, when
    ``memories`` is empty, or when no candidate sentence from any body
    appears in the reply.
    """
    if not assistant_text or not memories:
        return []
    haystack = _normalize(assistant_text)
    if not haystack:
        return []
    out: list[AttributionMatch] = []
    for memory_id, body in memories.items():
        if not body:
            continue
        for sentence in _candidate_sentences(body):
            needle = _normalize(sentence)
            if needle and needle in haystack:
                out.append(
                    AttributionMatch(memory_id=memory_id, claim_excerpt=sentence)
                )
                break  # one match per memory; rest stay potential noise
    return out
