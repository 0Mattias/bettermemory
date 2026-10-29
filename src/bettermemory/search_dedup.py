"""Write-time dedup for `search.py`: Jaccard over raw content token sets.

`find_similar` and `find_similar_tombstones` score a new body against
active and removed memories with `_pairwise_content_jaccard` and label
hits at `HIGH_SIMILARITY` and `MEDIUM_SIMILARITY`. Each side's token set
comes from `raw_content_tokens`, a bounded LRU over
`_raw_content_token_set` keyed on the text and registered with `_caches`.

`search.py` re-exports the names tests and other modules import from it.
A test that patches `_raw_content_token_set`, `raw_content_tokens` or
`RAW_TOKEN_SET_ENTRIES` patches this module, where their readers are.
Every tokenize call here goes through `search_lexicon` as
`_lexicon.tokenize`.
This module lives apart from `search.py` to keep each module under the
package's 2,000-line bound (ENG-96); from the family it imports
`search_lexicon` alone, and `search.py` imports it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Any, Callable

from . import _caches
from . import search_lexicon as _lexicon
from .models import Memory, SimilarHit, TombstonedMemory, snippet_for
from .search_lexicon import _STOPWORDS, _expand_kebab, _kebab_parts, _strip_stopwords


# ---------------------------------------------------------------------------
# Dedup at write time
# ---------------------------------------------------------------------------


# Thresholds for find_similar. Calibrated against jaccard on stopword-stripped
# token sets with pairwise-aware kebab expansion:
# - >= HIGH: block the write unless force=True. Two memories with this much
#   token overlap are very likely about the same fact; the right move is
#   memory_update on the existing entry.
# - >= MEDIUM: surface as `related` but do not block. The new memory may add
#   nuance worth keeping separate, but the writer should at least know the
#   adjacent memory exists.
# - <  MEDIUM: ignore.
HIGH_SIMILARITY = 0.75
MEDIUM_SIMILARITY = 0.40

# Minimum size of the SMALLER token set before the containment score in
# `_pairwise_content_jaccard` is allowed to fire. A 3-token fact whose
# handful of words all happen to appear somewhere in a long, topically
# unrelated note otherwise reaches containment ~1.0; requiring a real
# overlap keeps containment aimed at its actual target — a multi-token
# near-verbatim restatement of one sentence of a long body — rather than
# incidental common-vocabulary reuse.
_CONTAINMENT_MIN_TOKENS = 8
# Containment is a SOFT signal: it may raise a pair to 'related' but never
# to the 'high'/block bar. The ingest dedup gate (ingest.py) and the write
# gate skip/block only on a 'high' active hit, so an unbounded containment
# score would let a short vocabulary overlap SILENTLY drop a legitimately
# distinct write. Pin the containment contribution into the middle of the
# 'related' band so it always SURFACES (never silently blocks); a genuine
# verbatim duplicate still carries enough raw Jaccard to reach 'high' on
# its own merits.
_CONTAINMENT_CEILING = (HIGH_SIMILARITY + MEDIUM_SIMILARITY) / 2


def _content_token_set(text: str) -> set[str]:
    """Stopword-stripped token set with UNCONDITIONAL symmetric kebab/snake
    expansion. Retained as the reference for `compute_idf` (the BM25 corpus
    stats build the same shape inline); the Jaccard dedup scorers no longer
    call it.

    The dedup paths moved to `_raw_content_token_set` +
    `_pairwise_content_jaccard`: this function's old docstring claimed the
    union "grows in proportion and Jaccard stays well-behaved", which is
    mathematically false for a compound BOTH sides share — expanding it adds
    the same k part-tokens to intersection and union, and (i+k)/(u+k) > i/u
    whenever i < u, so expansion strictly inflates Jaccard for any
    non-identical pair. Two distinct per-environment facts sharing one
    compound identifier ("docker-compose ... prod" vs "docker-compose ...
    dev") crossed the 0.75 manual-apply threshold purely from that inflation.
    """
    return set(_strip_stopwords(_expand_kebab(_lexicon.tokenize(text))))


def _raw_content_token_set(text: str) -> set[str]:
    """Stopword-stripped tokens WITHOUT kebab expansion — the per-memory
    half of the pairwise dedup tokenisation. Compounds stay whole here;
    `_pairwise_content_jaccard` expands them per PAIR, only when the other
    side lacks the compound."""
    return set(_strip_stopwords(_lexicon.tokenize(text)))


# The raw token-set cache. `find_similar` (and the tombstone scan through
# the same scorer) and `supersession.detect_supersession` tokenise every
# active body on each write, the new body several times over; unit D1
# measured about nine tenths of a write on the owner's 662-memory copy in
# that tokenisation. `raw_content_tokens` memoises `_raw_content_token_set`
# on the one input it reads: the text. The tokenizer reads only module
# constants (the stopword lists, the stemmer's exceptions, the alias
# table), so the text is the whole key and a hit is the value a recompute
# builds. The key is the text and never an id or an `updated` stamp: a
# body rewritten in place is a different string and misses, so an edited
# record is tokenised again and its old entry ages out of the LRU. The
# U5b cache in `search.py` (`_memory_tokens`) keeps a different stream,
# the kebab-expanded one with stopwords, so it cannot serve this one. Values
# are frozensets, so no caller can change an entry another caller reads.
# The detector keys a body with its provenance stamp stripped, which is
# the body itself for an unstamped one, so one entry serves both callers
# for most records. The bound holds every active and removed body of a
# store of up to 5,000 unstamped records, the same bound as U5b's; past
# it the least recently used entry goes. The lock makes a look-up-and-touch
# and an insert-and-evict atomic; the tokenising runs outside it.
# Registered with `_caches`, which empties it before each test.
RAW_TOKEN_SET_ENTRIES = 5000

_RAW_TOKEN_SETS: OrderedDict[str, frozenset[str]] = OrderedDict()
_RAW_TOKEN_SETS_LOCK = threading.Lock()


def clear_raw_token_set_cache() -> None:
    """Drop every entry of `raw_content_tokens`' cache, so the next call
    for any text tokenises it again."""
    with _RAW_TOKEN_SETS_LOCK:
        _RAW_TOKEN_SETS.clear()


_caches.register(clear_raw_token_set_cache)


def raw_content_tokens(text: str) -> frozenset[str]:
    """`frozenset(_raw_content_token_set(text))`, or the entry an earlier
    call built for an equal text. The dedup scorer and the supersession
    detector read their token sets through this; see the comment on
    `RAW_TOKEN_SET_ENTRIES` for the key and the bound."""
    with _RAW_TOKEN_SETS_LOCK:
        cached = _RAW_TOKEN_SETS.get(text)
        if cached is not None:
            _RAW_TOKEN_SETS.move_to_end(text)
            return cached
    tokens = frozenset(_raw_content_token_set(text))
    with _RAW_TOKEN_SETS_LOCK:
        _RAW_TOKEN_SETS[text] = tokens
        while len(_RAW_TOKEN_SETS) > RAW_TOKEN_SET_ENTRIES:
            _RAW_TOKEN_SETS.popitem(last=False)
    return tokens


def _pairwise_content_jaccard(
    raw_a: AbstractSet[str], raw_b: AbstractSet[str]
) -> float:
    """Jaccard similarity with pairwise-aware kebab expansion.

    A kebab/snake compound is expanded into its parts (`_kebab_parts`,
    stopword parts stripped to match the old expand-then-strip order)
    only when the OTHER side's raw set lacks the compound. That keeps the
    cross-notation match (`python-frontmatter` vs `python frontmatter`
    still intersect on the parts) while a compound BOTH sides share
    contributes exactly one token to intersection and union — symmetric
    expansion of a shared compound added the same k part-tokens to both,
    which strictly inflates Jaccard whenever J < 1 (see
    `_content_token_set`) and pushed distinct per-environment facts over
    the dedup thresholds.
    """
    if not raw_a or not raw_b:
        return 0.0
    a = set(raw_a)
    for tok in raw_a:
        if tok not in raw_b:
            a.update(p for p in _kebab_parts(tok) if p not in _STOPWORDS)
    b = set(raw_b)
    for tok in raw_b:
        if tok not in raw_a:
            b.update(p for p in _kebab_parts(tok) if p not in _STOPWORDS)
    intersection = a & b
    if not intersection:
        return 0.0
    jaccard = len(intersection) / len(a | b)
    # Containment blind spot: a short near-verbatim restatement of one
    # sentence of a long body has |intersection| ~= the small set but a
    # union dominated by the long body, so Jaccard sinks below the
    # 'related' floor and the near-duplicate commits silently. Add a
    # containment score |intersection|/min — gated so it targets that case
    # and ONLY that case:
    #   - an absolute floor on the smaller set (`_CONTAINMENT_MIN_TOKENS`)
    #     so a 3-token fact sharing a few common words with a long
    #     unrelated note can't reach containment ~1.0;
    #   - it must itself clear MEDIUM_SIMILARITY (a weak overlap stays
    #     ignored); and
    #   - the result CAPPED into the 'related' band (`_CONTAINMENT_CEILING`,
    #     below HIGH_SIMILARITY) so containment can raise a pair to
    #     'related' but never to the 'high'/block bar — the ingest/write
    #     dedup gates skip only on a 'high' active hit, so an uncapped
    #     containment score could silently drop a legitimately distinct
    #     short write.
    #
    # There is NO size-ratio gate. The earlier `larger >= 3 * smaller`
    # cliff left a dead band: pure Jaccard covers full containment only up
    # to ratio 2.5 (1/r >= 0.40 = MEDIUM), so full containment at a ratio
    # in (2.5, 3.0) scored 0.333-0.400 — below MEDIUM AND below the gate,
    # hence silently ignored. `_CONTAINMENT_MIN_TOKENS` + the MEDIUM floor
    # + the ceiling already fence containment to its target case without
    # the discontinuity; `max(jaccard, containment)` makes containment a
    # no-op for near-identical pairs (jaccard already dominates there).
    # Honest scope note: dropping the gate DOES widen the firing set for
    # comparable-length pairs — two equal-size distinct writes sharing
    # 40-57% of the smaller side's tokens now land in the `related` band
    # via containment where raw Jaccard (intersection/union) kept them
    # below it. The widening is confined to the ADVISORY surface: the
    # ceiling below keeps containment under every high/block threshold,
    # so no write is ever refused by it — the accepted trade for closing
    # the (2.5, 3.0) full-containment dead band, pinned by
    # `test_find_similar_comparable_pair_widened_related_band_is_deliberate`.
    #
    # Ceiling guarantee: containment contributes at most _CONTAINMENT_CEILING
    # (< HIGH_SIMILARITY), so it can never raise a pair to the 'high'/block
    # band on its own — a genuine verbatim duplicate reaches 'high' via raw
    # Jaccard, which `max` leaves untouched. (Contested C3: this guarantee
    # is technically false only if a caller passes high_threshold <=
    # _CONTAINMENT_CEILING; no production caller does — the Jaccard-natural
    # high is HIGH_SIMILARITY.)
    smaller = a if len(a) <= len(b) else b
    if len(smaller) >= _CONTAINMENT_MIN_TOKENS:
        containment = len(intersection) / len(smaller)
        if containment >= MEDIUM_SIMILARITY:
            return max(jaccard, min(containment, _CONTAINMENT_CEILING))
    return jaccard


def find_similar(
    new_body: str,
    existing: list[Memory],
    *,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Find memories whose content overlaps `new_body` enough to flag.

    Default mode: Jaccard similarity on stopword-stripped token sets with
    pairwise-aware kebab expansion (`_pairwise_content_jaccard`) — symmetric
    and recency-free, unlike `score_memory`. Fast, deterministic, no extra
    deps.

    Thresholds default to HIGH_SIMILARITY / MEDIUM_SIMILARITY (0.75 /
    0.40) when None. Pass explicit thresholds to tune.

    Returns hits with similarity >= medium_threshold, sorted descending
    by similarity. Hits below high_threshold are labeled `"medium"`; at
    or above, `"high"`. Empty when `new_body` has no content (or no
    tokens, in Jaccard mode).
    """
    return _find_similar_jaccard(
        new_body,
        existing,
        high_threshold=(
            high_threshold if high_threshold is not None else HIGH_SIMILARITY
        ),
        medium_threshold=(
            medium_threshold if medium_threshold is not None else MEDIUM_SIMILARITY
        ),
    )


# ---------------------------------------------------------------------------
# Generic dedup engine
# ---------------------------------------------------------------------------
#
# Pre-Round-2 the active and tombstone passes were separate functions
# whose loop bodies were near-clones — same threshold dispatch, same
# tokenisation, same hit-construction shape with only the relevance label
# and the optional `removed_at` / `removed_reason` fields differing
# between active and tombstone passes. The four-way duplication meant
# bug fixes had to land repeatedly. Consolidated below: one Jaccard
# scorer parameterised by a `build_hit`
# callable that knows how to construct a SimilarHit for the
# active-vs-tombstone variant. The two public entry points
# (`find_similar`, `find_similar_tombstones`) keep their existing
# signatures so the call sites in `_handlers.py` don't move.
#
# The shape: scorers are pure — given a similarity, a Memory-ish, and
# the relevance label, build the SimilarHit. They return None to drop
# the row, which lets the build-hit callable handle the rare case
# where a candidate fails downstream validation. In practice every
# adopter returns a hit; the Optional shape exists for symmetry with
# the threshold check above it.


def _score_similar_jaccard(
    new_body: str,
    existing: list[Any],
    *,
    high_threshold: float,
    medium_threshold: float,
    high_label: str,
    medium_label: str,
    build_hit: Callable[[Any, float, str], SimilarHit | None],
    sort_key: Callable[[SimilarHit], Any],
) -> list[SimilarHit]:
    """Jaccard-similarity dedup over `existing`, building hits via
    `build_hit`. See module commentary at the section header for the
    role this plays — extracted from the pre-Round-2 quartet of
    near-duplicate functions.

    Token sets are RAW (no kebab expansion); `_pairwise_content_jaccard`
    expands compounds per pair so a compound both sides share can't
    inflate the score. Each set comes from `raw_content_tokens`' cache."""
    new_tokens = raw_content_tokens(new_body)
    if not new_tokens:
        return []

    hits: list[SimilarHit] = []
    for memory in existing:
        existing_tokens = raw_content_tokens(memory.body)
        if not existing_tokens:
            continue

        similarity = _pairwise_content_jaccard(new_tokens, existing_tokens)
        if similarity <= 0.0:
            continue

        if similarity >= high_threshold:
            relevance = high_label
        elif similarity >= medium_threshold:
            relevance = medium_label
        else:
            continue

        hit = build_hit(memory, round(similarity, 4), relevance)
        if hit is not None:
            hits.append(hit)

    hits.sort(key=sort_key, reverse=True)
    return hits


def _build_active_hit(memory: Memory, similarity: float, relevance: str) -> SimilarHit:
    """Construct a SimilarHit for the active-memory dedup path."""
    return SimilarHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        snippet=snippet_for(memory.body),
        similarity=similarity,
        relevance=relevance,
        created=memory.created,
        updated=memory.updated,
    )


def _build_tombstone_hit(
    memory: TombstonedMemory, similarity: float, relevance: str
) -> SimilarHit:
    """Construct a SimilarHit for the tombstone-aware dedup path. Carries
    the removal metadata the active variant doesn't have, so the
    write handler can render the `previously_removed` warning."""
    return SimilarHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        snippet=snippet_for(memory.body),
        similarity=similarity,
        relevance=relevance,
        created=memory.created,
        updated=memory.updated,
        removed_at=memory.removed,
        removed_reason=memory.removed_reason,
    )


def _active_sort_key(h: SimilarHit) -> tuple[float, datetime]:
    return (h.similarity, h.updated)


def _tombstone_sort_key(h: SimilarHit) -> tuple[float, datetime]:
    # Fall back to `updated` when `removed_at` is missing — defensive
    # against any TombstonedMemory whose removal time didn't make the
    # round trip (legacy fixtures). The active path uses `updated`
    # straight, so the fallback keeps the orderings comparable.
    return (h.similarity, h.removed_at or h.updated)


def _find_similar_jaccard(
    new_body: str,
    existing: list[Memory],
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    return _score_similar_jaccard(
        new_body,
        existing,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high",
        medium_label="medium",
        build_hit=_build_active_hit,
        sort_key=_active_sort_key,
    )


# ---------------------------------------------------------------------------
# Tombstone-aware dedup
# ---------------------------------------------------------------------------
#
# `find_similar` only walks the active set, which means the durability gate
# never fires when the writer is about to re-create a fact they previously
# removed. The lesson encoded in the tombstone's removal_reason is lost on
# the next write. `find_similar_tombstones` closes that loop: it scores
# the same body against tombstoned candidates and returns hits with
# `relevance="high-removed"` / `"medium-removed"` plus the removal
# metadata, so memory_write can warn ("you removed a 0.91-similar memory
# three weeks ago because 'turned out wrong'").
#
# Implementation note: we intentionally re-compute similarity here rather
# than calling find_similar(existing=tombstoned). The SimilarHit shape
# carries different metadata in each case (active hits have no removal
# fields; tombstone hits do), and TombstonedMemory is a distinct type
# from Memory so the type checker catches accidental mixing.


def find_similar_tombstones(
    new_body: str,
    tombstoned: list[TombstonedMemory],
    *,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Like `find_similar`, but scored against tombstoned memories and
    returning hits labeled with the `-removed` relevance suffix.

    Threshold defaults match the active path: 0.75/0.40. Empty input
    or empty body returns []. Hits
    are sorted descending by similarity, like `find_similar`.
    """
    if not tombstoned:
        return []

    return _find_similar_tombstones_jaccard(
        new_body,
        tombstoned,
        high_threshold=(
            high_threshold if high_threshold is not None else HIGH_SIMILARITY
        ),
        medium_threshold=(
            medium_threshold if medium_threshold is not None else MEDIUM_SIMILARITY
        ),
    )


def _find_similar_tombstones_jaccard(
    new_body: str,
    tombstoned: list[TombstonedMemory],
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    return _score_similar_jaccard(
        new_body,
        tombstoned,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high-removed",
        medium_label="medium-removed",
        build_hit=_build_tombstone_hit,
        sort_key=_tombstone_sort_key,
    )
