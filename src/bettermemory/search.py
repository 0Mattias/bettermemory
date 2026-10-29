"""Ranking memories against a query.

Three selectable rankers, dispatched by `search(mode=...)` — all of
them deterministic code; nothing here loads a model, and by project
direction nothing may (the code is the model):

- ``hybrid`` (default since 2.6.8): reciprocal rank fusion (Cormack
  et al., SIGIR 2009) over keyword + BM25. The fused score lives in a
  different (much smaller) scale than the single-ranker scores, so raw
  `score` is not comparable across modes — read `relevance` together
  with `matched_leg` instead.
- ``keyword`` (legacy default in 1.6.0): the original TF +
  scope-weighted + coverage + recency scorer. Cheap, deterministic,
  good on identifier-heavy queries but lacks IDF — underperforms on
  rare-term queries vs. BM25/hybrid.
- ``bm25``: Okapi BM25 with IDF weighting, TF saturation, length
  normalisation, plus the same scope-bonus and recency multiplier as
  the keyword scorer.

`compute_idf` and `reciprocal_rank_fusion` are exported alongside
their per-mode scorers so callers can wire the rankers directly
without going through `search()`. The dedup path (`find_similar`)
uses Jaccard over `_raw_content_token_set` token sets with
pairwise-aware kebab expansion (`_pairwise_content_jaccard`), each set
served from a bounded body-keyed cache (`raw_content_tokens`).

Four modules beside this one hold the parts, and this module re-exports
the names callers import from it: `search_lexicon` (the tokenizer and the
FTS index text), `search_bm25` (the keyword and BM25 scorers),
`search_lane` (the conversational lane's temporal reading and the per-hit
labels) and `search_dedup` (the write-time dedup). A fifth,
`search_steps`, holds the query-side steps of `search` that read nothing
this module holds.
"""

from __future__ import annotations

import logging
import threading
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal, NamedTuple

from . import _caches
from . import search_lexicon as _lexicon
from .expansion import expansion_terms as _expansion_terms_impl
from .models import Memory, MemoryHit, snippet_for, snippet_window
from .identity import Actor, actor_matches
from .search_bm25 import (
    _EXPANSION_TABLES as _EXPANSION_TABLES,
    CorpusStats as CorpusStats,
    _MemoryTokens as _MemoryTokens,
    _filler_floor_stats as _filler_floor_stats,
    _merge_corpus_stats as _merge_corpus_stats,
    _scope_tokens as _scope_tokens,
    compute_idf as compute_idf,
    score_memory as score_memory,
    score_memory_bm25 as score_memory_bm25,
)
from .search_dedup import (
    HIGH_SIMILARITY as HIGH_SIMILARITY,
    MEDIUM_SIMILARITY as MEDIUM_SIMILARITY,
    RAW_TOKEN_SET_ENTRIES as RAW_TOKEN_SET_ENTRIES,
    _RAW_TOKEN_SETS as _RAW_TOKEN_SETS,
    _pairwise_content_jaccard as _pairwise_content_jaccard,
    _raw_content_token_set as _raw_content_token_set,
    clear_raw_token_set_cache as clear_raw_token_set_cache,
    find_similar as find_similar,
    find_similar_tombstones as find_similar_tombstones,
    raw_content_tokens as raw_content_tokens,
)
from .search_lane import (
    LEG_EXPANSION as LEG_EXPANSION,
    LEG_LEXICAL as LEG_LEXICAL,
    _CONV_SCAFFOLD_FLOOR_RATIO as _CONV_SCAFFOLD_FLOOR_RATIO,
    _CONV_SCAFFOLD_STEMS as _CONV_SCAFFOLD_STEMS,
    _V2_HIGH_MATCHED_FLOOR as _V2_HIGH_MATCHED_FLOOR,
    _TemporalReading as _TemporalReading,
    _conv_scaffold_terms as _conv_scaffold_terms,
    _matched_leg as _matched_leg,
    _memory_anchor_day as _memory_anchor_day,
    _relevance_label as _relevance_label,
    _relevance_label_v2 as _relevance_label_v2,
    _scaffold_floor_stats as _scaffold_floor_stats,
    _temporal_reading as _temporal_reading,
)
from .search_lexicon import (
    _ALIAS_ANCHOR_PATTERNS as _ALIAS_ANCHOR_PATTERNS,
    _FOLD_FALLBACK_RE as _FOLD_FALLBACK_RE,
    _STOPWORDS as _STOPWORDS,
    _TOKEN_RE as _TOKEN_RE,
    _expand_kebab as _expand_kebab,
    _fold_diacritics as _fold_diacritics,
    _kebab_parts as _kebab_parts,
    _stem_token as _stem_token,
    _strip_stopwords as _strip_stopwords,
    _tokenize_impl as _tokenize_impl,
    _tokenize_unstemmed as _tokenize_unstemmed,
    fts_index_text as fts_index_text,
    fts_match_query as fts_match_query,
    tokenize as tokenize,
    tokenizer_fingerprint as tokenizer_fingerprint,
)
from .search_steps import (
    _check_mode,
    _expansion_corpus_stats,
    _fetch_corpus_stats,
    _hybrid_corpus_stats,
    _query_tokens,
    _record_matched_legs,
)
from .verify import PathDriftReport, detect_path_drift

if TYPE_CHECKING:
    from .admission import Admission
    from .verify import Anchoring, ClaimAnchor

log = logging.getLogger("bettermemory.search")

# Search modes exposed via `search(mode=...)`. Default is `hybrid` since
# 2.6.8 — the keyword scorer lacks IDF weighting and underperforms on
# rare-term queries, and hybrid degrades gracefully to keyword+BM25
# fusion when no embedding extra is installed (so flipping the default
# doesn't add a dep requirement).
SearchMode = Literal["keyword", "bm25", "hybrid"]


# ---------------------------------------------------------------------------
# Query-time rescue expansion (retrieval campaign, Phase 1)
# ---------------------------------------------------------------------------
#
# The measured enemy is vocabulary: the query says "toggles", the memory
# says "feature flags", and on bench/retrieval that class of miss is the
# whole 25-point recall@1 gap the removed embedding arm used to cover.
# The rescue is three committed word tables (expansion.py) feeding one
# extra, down-weighted BM25 leg — engaged only when the base ranking is
# not confident. Every constant below is measured on the gold set
# (the retrieval-bench notes, 2026-08-09 grid), not chosen by taste.
#
# `search(rescue_expansion=True)`, hybrid-mode only and DEFAULT OFF,
# runs two query-time repairs from the retrieval campaign:
# (a) listed discourse-filler words (`expansion.QUERY_FILLER_WORDS`)
# get a document-frequency FLOOR in the BM25 legs, so corpus-rare
# conversational filler can't outprice real content terms; and
# (b) when the fused base ranking's top hit covers less than
# `_RESCUE_COVERAGE_GATE` of the query's unique tokens, one extra
# BM25 leg over SYNTHESIZED vocabulary (inflection variants,
# clipping full-forms, synonym group mates — `expansion.py`) joins
# the fusion at `_RESCUE_LEG_WEIGHT`.
#
# Why the default is off: the lane's own preregistered held-out
# check killed default-on. On the technical-prose gold set the
# repairs are worth +15/+30 recall@1/@5 as-asked
# (bench/retrieval, 2026-08-09 artifacts); on LongMemEval's
# conversational stores the same mechanisms cost 1.65 macro@5
# points — inflection variants of common chat verbs are
# promiscuous matchers there, the inverse of the technical corpus
# where expansion vocabulary is rare and discriminating. Kill
# criterion and ablations: the LongMemEval preregistration
# addendum 3 and the bench README. Flipping the default back on is
# earned by a fresh preregistration on both instruments, not by an
# operator's hunch. Since 9.0.0 there is no config key for it: the
# product ranks with the leg off, and bench/retrieval's requery arm
# is the one caller that turns the parameter on.

# The rescue leg engages only when the fused base ranking is NOT
# confident: top-hit coverage below this bar. Measured, not derived: on
# the gold set every requery-probe top hit covers >= 0.60 of its query
# (the leg never engages; requery stays byte-stable at 80%/100%) while
# every rescued asked-probe case covers below it. At 0.65 requery gives
# back a question; at 0.75 two. The value sits between the relevance
# bands' 0.40/0.75 because it is the same signal those bands read —
# coverage as ranking confidence.
_RESCUE_COVERAGE_GATE = 0.60

# Weight of the expansion leg in weighted RRF; base legs stay at 1.0.
# Expansion is a rescue, never a peer: at weight 1.0 the leg overrules
# confident base-leg agreement (measured: two rank-0 hits lost to
# variant noise), at 0.5 two rescued cases stall short of the top-5. At
# 0.7 every measured rescue lands and no base-leg agreement is
# overturned.
_RESCUE_LEG_WEIGHT = 0.7

# How many synthesized terms the rescue leg's top candidate must match
# before the leg's vote counts. Below this the leg is withheld
# entirely, leaving the base fusion exactly as it stood. (The lane's
# other mechanism, the filler df-floor, is keyed on `rescue_expansion`
# rather than on the leg, so it still applies — a withheld leg
# reproduces a lane-on query whose leg found nothing, not a
# `rescue_expansion=False` query.)
#
# Why the leg needs a confidence test at all: `_hybrid_fuse` fuses by
# RANK, so a leg contributes `_RESCUE_LEG_WEIGHT / (rrf_k + rank)`
# whether its rank-1 was found by a discriminating synonym or by a
# single coincidental token. IDF only reorders WITHIN the leg; it
# cannot reduce the leg's influence.
#
# Why a COUNT and not a threshold on a score distribution. Two earlier
# rules were measured and retired: a fixed margin level (round 3) and a
# self-calibrating gap ratio (round 4). Both separated on the dev set,
# both caught the same three harmful legs, and both also withheld nine
# and seven HELPFUL legs — because both were fitted to a proxy ("the
# leg's rank-1 is the gold document") rather than to whether the leg's
# vote actually moved the gold. Labelling that directly
# (bench/leg_labels.py) reframes the problem: of 39 engaged dev legs,
# 21 help, 3 hurt, 15 are neutral, and every harmful one had a rank-1
# matching exactly ONE synthesized term while no helpful one did.
#
# One matched term is a coincidence; two independent synthesized terms
# agreeing on the same document is evidence. 2 is the minimum
# non-trivial count — a bar stated independently of any measurement,
# which the labels confirm rather than select. It is also the first
# rule here that is not a threshold on a distribution, so there is no
# spread to shift between corpora: rounds 3 and 4 failed on exactly
# that, and a count of agreeing terms has no such spread.
#
# Preregistered in the LongMemEval preregistration addendum 7
# before this code existed.
_RESCUE_LEG_MIN_EVIDENCE = 2

# The evidence count at which the rescue leg earns its FULL weight.
# Below it the leg still votes, but at the fraction of that bar its own
# evidence reaches:
#
#     scale(m) = min(1.0, m / _EVIDENCE_FULL_AT)
#
# so a leg at the floor (2 matched terms) votes at two thirds strength
# and a leg with 3 or more votes in full.
#
# Why the vote should scale at all: `_hybrid_fuse` fuses by RANK, so
# before this the leg contributed `_RESCUE_LEG_WEIGHT / (rrf_k + rank)`
# whether its rank-1 rested on a discriminating synonym or on a single
# coincidental token. Three rounds of choosing WHICH legs vote plateaued
# within 0.004 of each other, and one round of choosing WHAT they
# contain could not reach the incumbent's precision; the vote itself was
# the one variable that had never moved.
#
# 3 is read off the dev labels by a stated rule, not tuned: legs
# stratify monotonically by evidence — 0% helpful at one matched term,
# 68.2% at two, 100% at three or more — and the leg earns its full
# weight at the count where the labels first read 100%.
# `bench/leg_labels.py` is the instrument; addendum 9 is the
# preregistration, committed before this constant existed.
#
# The scale is bounded in [0, 1], so this can only ever REDUCE the
# leg's influence relative to the flat weight, never amplify it. An
# amplifying change would need a different safety argument.
_EVIDENCE_FULL_AT = 3

# Whether the leg's weight is SCALED by its evidence, or flat above the
# floor. **Default off: the shipped lane uses the flat weight.**
#
# Both forms were preregistered, implemented and measured (addenda 9 and
# 10). The scaled forms win on conversational stores and lose on
# technical ones, and the decision follows from who actually turns this
# lane on. Measured on the gold set, by the preregistered dev gates'
# own verdicts:
#
#     form                        asked r@1/r@5   control r@1/r@5
#     flat (this default)         0.55 / 0.90     0.50 / 0.85
#     scaled (m-1)/(F-1)          0.55 / 0.80     0.50 / 0.80
#     scaled m/F                  0.55 / 0.80     0.50 / 0.85
#
# The scaled forms' compensating gains land on LongMemEval's
# conversational stores (macro@5 0.8823 flat -> 0.8926 / 0.8901
# scaled) — and every one of those figures is BELOW that corpus's
# lane-off baseline of 0.8935, so a conversational store is better
# served leaving `rescue_expansion` off entirely than by any form of it.
# The audience that turns the lane on is a store whose owner knows it
# holds technical prose, and for that audience flat is strictly better.
#
# Round 8 is why this is a knob rather than an inference: no cheap store
# statistic separates the two corpora by anything near the twofold the
# weight would have to move (best 1.70x, and filler-token share — the
# quantity the lane's own premise names — only 1.13x). The engine cannot
# tell which corpus it is holding, so the owner does.
#
# The scaled form stays committed and exercisable rather than deleted:
# it is the campaign's best held-out configuration and the record for
# it is published. `bench/*/run.py --evidence-scaling on` drives it.
_RESCUE_LEG_EVIDENCE_SCALING = False

# Whether a BASE leg whose rank-1 evidence trails its peer's is withheld
# from the hybrid fusion. **Default off until round 9's gates pass: the
# shipped base fusion is unchanged.**
#
# Why the base pair should be weighted at all: `_hybrid_fuse` reads
# rank, not evidence, so a keyword or BM25 leg whose rank-1 rests on one
# coincidental token votes exactly as hard as one whose top candidate
# matched four query terms. That is the same defect the rescue leg's
# machinery above corrects — addendum 9 named the generalisation as a
# future hypothesis, and the base-leg census measured it
# (bench/base_leg_census.py, artifact
# bench/retrieval/results/base-leg-labels-2026-08-12.json).
#
# Why RELATIVE evidence and not the rescue leg's absolute count: at a
# base leg's rank-1 the matched-term count mostly measures query length,
# and the census found it does not stratify helpfulness (BM25 runs
# backwards under it). The leg's evidence relative to its peer's does
# stratify, sharply: leading legs helped 20/29 labelled cases, trailing
# legs 2/29, zero of twelve at a deficit of two or more. With exactly
# two base legs only the weight RATIO matters, so the whole design
# space is what happens to the trailing leg.
#
# Why withholding rather than a graded weight: rounds 6-7 measured that
# a graded constant between 0 and 1 has monotone OPPOSITE optima on a
# technical and a conversational corpus, and round 8 measured that the
# store cannot supply the constant. This rule declines to own such a
# scalar. Withholding is already the lane's grammar for thin evidence
# ("a leg with one word of evidence does not get to vote"); this is the
# same sentence with "fewer words than its peer" as the predicate. Ties
# — 80% of dev probes — return None and fuse byte-identically.
#
# Preregistered in the LongMemEval preregistration addendum 12
# (round 9) before this code existed. `bench/*/run.py --base-withhold
# on` drives the mechanism arm.
_BASE_LEG_TRAILING_WITHHOLD = False


# ---------------------------------------------------------------------------
# The conversational lane (Lane L unit 1, the L1 declaration)
# ---------------------------------------------------------------------------
#
# Two deterministic repairs for conversation-shaped stores, behind
# `search(conversational=...)`, hybrid-mode only. Shipped DEFAULT ON at
# 6.1.0 on the L1 gate read (`[behavior] conversational` opts out) —
# unlike `rescue_expansion`, whose held-out check kept it opt-in — and
# byte-stable for every query with no temporal reading:
#
# - L1-S: when the query has a temporal reading, its temporal-SCAFFOLD
#   tokens (day/week/ago/last/many and kin — the question's syntax, not
#   its content) get a document-frequency floor in the BM25 legs so they
#   cannot outprice content terms. The L1 miss anatomy found this the
#   dominant LongMemEval failure: for "how many days ago did I buy a
#   smoker?" the sessions outranking the gold matched `ago`/`day`/`many`
#   while the gold matched `smoker`. Exactly the filler df-floor's
#   pathology in temporal dress, repaired through the same stats seam.
# - L1-T: within the near-tie band of the fused ranking, date anchors
#   break lookalike ties — an explicit query window (a named month, a
#   date, "last month") boosts in-window anchors and demotes out-of-
#   window ones; an elapsed/order-shaped ask ("how many weeks ago did
#   I…", "which happened first…") boosts the FIRST narration (earliest
#   anchor), or the latest when the ask says "the last time I".
#
# Constants below are the declaration's declared defaults; the caps and
# the tuning protocol live in the declaration, and the finals a gate
# read used are recorded in its artifact.
#
# What `search(conversational=True)` runs, default on (the L1 declaration): when
# the query has a temporal reading, (a) temporal-SCAFFOLD tokens
# (day/week/ago/last/many and kin — the question's syntax, not
# its content) get a document-frequency floor in the BM25 legs
# through the same stats seam as the filler floor, so they cannot
# outprice content terms; and (b) date anchors break lookalike
# ties inside the fused ranking's near-tie band — an explicit
# window (a named month, a date, "last month" resolved against
# `now`) boosts in-window anchors, and boosts only: the demote
# and the earliest/latest selector were measured net-negative at
# tuning and are dead (the record has the join).
#
# Why the default is on: the unit's gate read
# (the L1 record, artifacts beside it) measured +1.27
# LongMemEval macro-recall@5 points and +0.93 at @1 on the full
# 500 against the paired off arm, with the dev instrument
# byte-identical, no question type regressed, and the untouched
# holdout half generalizing stronger than the tuning half. The
# reference line (0.916) is NOT met — the shipped reading is
# 0.9062, stated plainly in the record. `[behavior]
# conversational = false` is the supported opt-out; the module
# constants carry the tuned finals the gate ranked with.

# Near-tie band for L1-T: items whose fused score is at least this
# fraction of the top hit's participate in anchor selection. RRF scores
# decay slowly (1/(k+rank)), so 0.5 reaches roughly the top fifty of a
# two-leg ranking — the L1 anatomy put 81% of missed evidence at
# distinct-session ranks 5-19.
_CONV_BAND_TAU = 0.50

# L1-T adjustment magnitudes, multiplicative on the fused score.
#
# The tuning frontier's path (artifacts in bench/l/results/): read 1
# measured the declared defaults net-negative at @1 (0.5301 -> 0.5038
# on the tuning half), the damage concentrated in earliest-selector
# displacement of already-correct rank-1 golds — the anatomy's
# gold-is-earliest evidence holds among MISSES and inverts among
# tops, so the SELECTOR IS DROPPED (declaration §5's drop rule;
# 0.0 is the drop, not a tuned value). Read 2 isolated the floor
# (clean +0.54 @5, @1 exactly preserved). Read 4 re-enters the
# window arm boost-only: read 1's window losses came through the
# demote side, its gains through the boost, so the demote stays 0.
_CONV_WINDOW_BOOST = 0.30
_CONV_WINDOW_DEMOTE = 0.0
_CONV_SELECTOR_BOOST = 0.0
_CONV_SELECTOR_DECAY = 0.7

# L2 (the L2 declaration): the pricing gate's
# widening and the keyword leg's scaffold weight. Both ship None —
# dark: with both None the engine is behaviorally identical to 6.1.0,
# and only a tuning-read config commit under the declaration's
# protocol may set an arm.
#
# _CONV_SCAFFOLD_MIN_STEMS — a query with NO temporal reading still
# enters the PRICING gate (the BM25 df-floor and the keyword
# repricing, never the window rerank) when it carries at least this
# many distinct scaffold tokens plus at least one content token. The
# count asks that dominate the multi-session residual ("how many
# projects have I led…") carry the scaffold class densely but parse
# no window and no selector; co-occurrence plus content is the
# structural signal. None removes the widening — the pricing gate is
# L1's temporal reading alone.
#
# None is the L2-PARK reversion (the L2 record): the widening
# died in tuning — in count asks the bodies' own scaffold (amounts,
# "total", "this year") is evidence, and the widened floor strips the
# gold's edge over content lookalikes — and the keyword weight died
# at the gate, so the constant pair ships dark and the engine is
# behaviorally 6.1.0.
_CONV_SCAFFOLD_MIN_STEMS: int | None = None
# _CONV_KEYWORD_SCAFFOLD_WEIGHT — a priced query's scaffold terms
# contribute this multiple of their standard keyword-scorer
# contribution, and the coverage multiplier is computed over content
# terms alone (see score_memory). None leaves the keyword leg stock:
# L1 repriced only the BM25 half of the fusion's vote, and this
# constant is the other half. None is the L2-PARK reversion
# (the L2 record): the tuning half's weight curve rose
# monotonically into the declared cap, and the holdout half inverted
# it — the gate read moved the untouched half down and the
# multi-session type below its guard, the exact overfit G1h exists
# to catch. A temporal gold's scaffold co-match is load-bearing
# separation in this leg; repricing it trades rescues for losses at
# every weight the unit was allowed to price.
_CONV_KEYWORD_SCAFFOLD_WEIGHT: float | None = None


def _conv_scaffold_shaped(query_tokens: list[str]) -> bool:
    """The widened pricing-gate predicate (L2): at least
    `_CONV_SCAFFOLD_MIN_STEMS` distinct scaffold tokens AND at least
    one content token. Count asks carry the scaffold class without
    parsing a window or a selector; requiring co-occurrence plus
    content is what keeps single common stems ("long", "time") from
    firing on non-count discourse — the declaration's rejected head
    trigger. None disables the widening entirely.
    """
    if _CONV_SCAFFOLD_MIN_STEMS is None:
        return False
    distinct = list(dict.fromkeys(query_tokens))
    # Read the ordered stream, then dedup: `_conv_scaffold_terms` licenses
    # a bare numeral off the token that follows it, and deduplicating
    # first can move that neighbour.
    scaffold = list(dict.fromkeys(_conv_scaffold_terms(query_tokens)))
    return len(scaffold) >= _CONV_SCAFFOLD_MIN_STEMS and len(scaffold) < len(distinct)


def _conversational_rerank(
    scored: list[tuple[Memory, float, list[str]]],
    *,
    reading: _TemporalReading,
) -> list[tuple[Memory, float, list[str]]]:
    """L1-T: date-anchor selection inside the near-tie band.

    Multiplicative, bounded adjustments on the fused RRF score, then a
    re-sort under the standard `(score, created, id)` key — determinism
    is structural. Items below the band, and every item when the query
    has no temporal reading, keep their score bit-for-bit.
    """
    if not scored:
        return scored
    top_score = scored[0][1]
    if top_score <= 0.0:
        return scored
    threshold = _CONV_BAND_TAU * top_score
    banded = [i for i, (_, s, _m) in enumerate(scored) if s >= threshold]
    if len(banded) <= 1:
        return scored
    anchors = {i: _memory_anchor_day(scored[i][0]) for i in banded}

    factors: dict[int, float] = {}
    if reading.window is not None:
        lo, hi = reading.window
        for i in banded:
            in_window = lo <= anchors[i] <= hi
            factors[i] = (
                1.0 + _CONV_WINDOW_BOOST if in_window else 1.0 - _CONV_WINDOW_DEMOTE
            )
    elif reading.selector is not None:
        distinct = sorted(set(anchors.values()), reverse=(reading.selector == "latest"))
        ordinal = {a: k for k, a in enumerate(distinct)}
        for i in banded:
            factors[i] = 1.0 + _CONV_SELECTOR_BOOST * (
                _CONV_SELECTOR_DECAY ** ordinal[anchors[i]]
            )
    else:
        return scored

    adjusted = [
        (memory, score * factors.get(i, 1.0), matched)
        for i, (memory, score, matched) in enumerate(scored)
    ]
    adjusted.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    return adjusted


# The token cache. `_memory_tokens` is a pure function of `memory.body`
# and `memory.scopes` (the tokenizer reads only module constants), so it
# is memoised on exactly those inputs and a hit is the value a recompute
# builds. The key is the inputs rather than `(id, updated)`: a record is
# rewritten without `updated` moving (`Store.mark_verified`), and benches
# and tests build Memory objects in one process that share an id and an
# `updated` stamp with different bodies (tests/test_server_commit_drift.py
# does so on purpose). The key costs one string hash per candidate, about
# 1.9 µs on a body of the owner's store against about 0.82 ms to tokenise
# it. The bound holds every record of a store of up to 5,000; past it the
# least recently used entry goes. Callers on several threads share the
# cache (bench/aml/server.py runs searches in a thread pool), so the lock
# makes each look-up-and-touch and each insert-and-evict atomic;
# tokenising runs outside it.
TOKEN_CACHE_ENTRIES = 5000

_TOKEN_CACHE: OrderedDict[tuple[str, tuple[str, ...]], _MemoryTokens] = OrderedDict()
_TOKEN_CACHE_LOCK = threading.Lock()


def clear_token_cache() -> None:
    """Drop every entry of `_memory_tokens`' cache, so the next call for
    any body and scopes tokenises them again."""
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.clear()


_caches.register(clear_token_cache)


def _memory_tokens(memory: Memory) -> _MemoryTokens:
    """Build the `_MemoryTokens` for one candidate, or return the entry an
    earlier call built for an equal body and scopes tuple. Field
    expressions mirror the scorers' recompute paths token for token; see
    `_MemoryTokens` for why equality is load-bearing, and the comment on
    `TOKEN_CACHE_ENTRIES` for the cache."""
    key = (memory.body, tuple(memory.scopes))
    with _TOKEN_CACHE_LOCK:
        cached = _TOKEN_CACHE.get(key)
        if cached is not None:
            _TOKEN_CACHE.move_to_end(key)
            return cached
    body = _expand_kebab(_lexicon.tokenize(memory.body))
    scope_set: set[str] = set()
    for scope in memory.scopes:
        scope_set.update(_scope_tokens(scope))
    tokens = _MemoryTokens(
        body=body, content=_strip_stopwords(body), scope_set=scope_set
    )
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE[key] = tokens
        while len(_TOKEN_CACHE) > TOKEN_CACHE_ENTRIES:
            _TOKEN_CACHE.popitem(last=False)
    return tokens


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------
#
# RRF (Cormack, Clarke, Büttcher, SIGIR 2009) fuses multiple ranked lists
# into one without needing the underlying scores to be on the same scale.
# Each doc's fused score is the sum, over rankers, of `1 / (k + rank)`.
# Docs absent from a ranker contribute nothing from that ranker. k=60 is
# the original paper's recommendation and is the de-facto default across
# implementations.
#
# Why RRF and not weighted score fusion: BM25 scores, Jaccard-style
# coverage scores, and cosine scores live on different scales (BM25 is
# unbounded, cosine is 0..1, the keyword scorer here mixes raw counts
# with multiplicative coefficients). Adding them directly biases the
# fused result toward whichever scale happens to be largest. Rank-only
# fusion sidesteps the calibration problem entirely — only positions
# matter, so a ranker can swap its scoring function without changing
# the fused output as long as the order stays the same.
#
# Practical note: when only one ranker is provided, RRF degenerates to
# `1 / (k + rank)` over that ranker — order is preserved, scores are
# rescaled. Callers can use that as a sanity check.


_RRF_K_DEFAULT = 60

# Relative score lead the top hit must hold over the runner-up before a
# caller's `expand_top` inlines its body (`handlers/search.py`). Lives
# here because it is DERIVED from RRF's spacing rather than chosen: a
# fused score is a sum of `1/(k + rank)` terms, so a top hit that leads
# by exactly ONE rank slot in every ranker scores `(k+2)/(k+1)` of the
# runner-up — 1.6% at the canonical k — which is the smallest possible
# non-tie and carries no information beyond "not tied". A TWO-slot lead,
# `(k+3)/(k+1)`, is the first margin a single position disagreement
# cannot manufacture, so that is the bar. Measured on the dogfood store's
# logged searches, the median top/runner-up ratio is 1.016 — the one-slot
# case, i.e. most hybrid results genuinely are near-ties and the
# derivation is sitting right on top of the mass of the distribution.
#
# Raw-score modes (keyword / bm25) separate far more widely
# than fused ones, so the same constant is loose there by construction.
# That is the correct direction: it is calibrated on the tightest scale
# in use, and a mode whose scores actually spread has already made the
# caller's case for it.
EXPAND_TOP_SCORE_MARGIN = (_RRF_K_DEFAULT + 3) / (_RRF_K_DEFAULT + 1) - 1.0


def top_hit_leads_runner_up(top_score: float, next_score: float) -> bool:
    """Does the top hit lead the runner-up by more than a rank slot?

    The leg-agnostic half of the `expand_top` gate. The lexical half —
    a "high" coverage label — could not fire for a pure-paraphrase hit
    (`match_terms` is empty by construction), which is how the 4x-cost
    semantic capability (removed in 4.0.0) ended up unable to trigger the one affordance
    that shows the caller a full body.

    Zero or negative runner-up scores mean nothing to compare against on
    a ratio, so any positive top score leads. An all-zero result set
    (browse mode) leads nothing.
    """
    if next_score <= 0.0:
        return top_score > 0.0
    return top_score >= next_score * (1.0 + EXPAND_TOP_SCORE_MARGIN)


def reciprocal_rank_fusion(
    ranking_lists: list[list[str]],
    *,
    k: int = _RRF_K_DEFAULT,
    weights: list[float] | None = None,
) -> dict[str, float]:
    """Fuse multiple ranked id-lists into one score-per-id map.

    Each `ranking_lists[i]` is a list of memory ids in best-first order
    for ranker i. The returned dict maps memory_id -> RRF score; sort
    descending to get the fused ranking. Ids that appear in no list are
    not present in the output. Duplicate ids within a single ranker's
    list are unusual but tolerated — the first (best-ranked) position
    wins for that ranker; later duplicates are ignored, matching the
    "one rank per (ranker, doc)" reading of the original paper.

    `weights` (optional) is index-aligned with `ranking_lists`: ranker
    i contributes `weights[i] / (k + rank)` instead of `1 / (k + rank)`.
    None means all-1.0 and is byte-identical to the pre-weights output
    — the rescue-expansion leg is the reason this exists (a rescue
    contributes at reduced strength; base legs stay at 1.0). Length
    mismatch raises: a silently-recycled weight would misweight a leg
    without anything looking wrong.

    Empty `ranking_lists` returns an empty dict. `k` must be positive;
    the default (60) matches the Cormack et al. paper.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    if weights is not None and len(weights) != len(ranking_lists):
        raise ValueError(
            f"weights length {len(weights)} != rankings length {len(ranking_lists)}"
        )
    if not ranking_lists:
        return {}

    fused: dict[str, float] = {}
    for i, ranking in enumerate(ranking_lists):
        w = weights[i] if weights is not None else 1.0
        # Iterate with 1-indexed rank — the original formula assumes
        # rank starts at 1. `seen` guards the dedup contract above.
        seen: set[str] = set()
        for rank, memory_id in enumerate(ranking, start=1):
            if memory_id in seen:
                continue
            seen.add(memory_id)
            fused[memory_id] = fused.get(memory_id, 0.0) + w / (k + rank)
    return fused


def _filter_candidates(
    memories: list[Memory],
    *,
    scopes: list[str] | None,
    excluded_scopes: set[str] | None,
    admission: Admission | None,
    client_filter: str | None = None,
    model_filter: str | None = None,
) -> list[Memory]:
    """Apply scope / excluded-scope / membership / actor filters.

    Extracted from `search()` so each search mode walks the same
    pre-filtered candidate list — fairness across rankers requires it,
    and it makes the per-mode scorers obviously equivalent on the
    filtering side. Order of `memories` is preserved. The candidates'
    home rows are read in one read (`Admission.prime`).
    """
    scope_filter = set(scopes) if scopes else None
    excluded = excluded_scopes or set()
    if admission is not None:
        admission.prime(memory.id for memory in memories)
    out: list[Memory] = []
    for memory in memories:
        home = admission.home(memory.id) if admission is not None else None
        if candidate_admitted(
            memory.scopes,
            home.recorded_project if home is not None else None,
            memory.actor,
            scope_filter=scope_filter,
            excluded=excluded,
            admission=admission,
            client_filter=client_filter,
            model_filter=model_filter,
        ):
            out.append(memory)
    return out


def candidate_admitted(
    memory_scopes: list[str],
    memory_home: str | None,
    memory_actor: Actor | None,
    *,
    scope_filter: set[str] | None,
    excluded: set[str],
    admission: Admission | None,
    client_filter: str | None = None,
    model_filter: str | None = None,
) -> bool:
    """Does one memory survive the search filters?

    Split out of `_filter_candidates` so the BM25 corpus-statistics path
    can decide admission from a store row (id, scopes, the project its
    ``homes`` row names, actor) without parsing a body. Both callers
    therefore run the identical predicate: the IDF denominator is
    provably the same collection that gets ranked.

    `memory_home` is the project the record's ``homes`` row names (None
    for a record with none) and `admission` the request's
    (`admission.Admission`, built once per request from the caller's
    resolution): `membership.admitted` on the search's surface decides
    (design 3.5; a global record with the projects `memory_scopes` name,
    `Admission.admits_row`), and a record with no home is admitted nowhere. None
    admits every record: offline callers (the benches, an eval replay)
    that rank a store as a whole, never a caller's request.

    `client_filter` / `model_filter` (7.12.0) select on the writing
    request's DECLARED identity, and differ from the membership rule in
    two ways worth stating where the rule lives:

    * They are exact and case-sensitive, with no normalisation on either
      side — which is what lets `index.query` ALSO spell them as a SQL
      `WHERE` (to spend its candidate cap on eligible rows) without the
      two spellings being able to disagree.
    * A memory with no actor matches NEITHER. This is a selection: 'what
      did this client write' must not answer with everything written
      before 7.10.0, when nothing recorded a writer at all.

    The filters read only the two DECLARED fields. `principal` — the
    attested one — is not selectable, and the omission is deliberate:
    it is None on every unauthenticated transport, so a filter on it
    could not be measured on any transport this ships with.
    """
    memory_scope_set = set(memory_scopes)
    if excluded and (memory_scope_set & excluded):
        return False
    if scope_filter is not None and not (memory_scope_set & scope_filter):
        return False
    if admission is not None and not admission.admits_row(memory_scopes, memory_home):
        return False
    if not actor_matches(memory_actor, client=client_filter, model=model_filter):
        return False
    return True


# How much left context a mid-body snippet window keeps in front of its
# anchor, and — the same number, deliberately — the offset at or under
# which we serve the plain head instead. A match that close to the top is
# already inside the head window, so a leading "..." over the first few
# words would be pure noise.
_SNIPPET_LEAD_CHARS = 40

# Hard bound on how far into a body the anchor scan walks. The scan is one
# `_TOKEN_RE` pass plus one `tokenize` per distinct raw token — cheap per
# token, but linear in body length and paid the first time a body is a
# RETURNED hit, so a pathological body degrades to head-of-body past this
# point rather than to a stall.
_SNIPPET_SCAN_CHARS = 8000


class _SnippetTokens(NamedTuple):
    """The anchor scan of one body, the part of `_query_biased_snippet`
    that depends on the body alone: where each `_TOKEN_RE` match in its
    first `_SNIPPET_SCAN_CHARS` stripped characters starts, and the
    normalised surfaces of each, `frozenset(_expand_kebab(tokenize(raw)))`,
    index-aligned."""

    starts: tuple[int, ...]
    surfaces: tuple[frozenset[str], ...]


# The snippet path's two memos. The scan of a body is memoised on the slice
# of it the scan reads, its first `_SNIPPET_SCAN_CHARS` stripped characters
# (`_snippet_tokens`), so a hit shown again tokenizes nothing and a body of
# any length keeps a key of at most that many characters; and the surfaces
# of a raw token are interned across every body the path scans
# (`_token_surfaces`), so a body shown for the first time tokenizes only the
# raw tokens no earlier scan met. Both memoise pure functions of their keys
# (the tokenizer reads only module constants), so what they return is what
# the uncached scan computes and a snippet served from them is the uncached
# one, character for character. Each is an LRU bounded in entries, not in
# bytes. Measured with tracemalloc on Python 3.13 (the bytes each memo
# releases when cleared): the scan memo holds about 8.6 KB an entry for the
# public corpus's bodies (bench/retrieval/corpus.jsonl) and the interned
# surfaces about 390 bytes a raw token, 13.6 MB together for those 1,080
# bodies and 52 MB at the 5,000-entry bound for bodies of that shape. The
# densest scan, a slice of one-letter tokens, holds about 186 KB an entry
# (about 930 MB at the bound), and a raw token as long as the scan about
# 25 KB with its body's entry (about 490 MB for 20,000 such tokens, the
# interning bound). Their lock makes a look-up-and-touch and an
# insert-and-evict atomic; the tokenizing runs outside it. The values are
# tuples and frozensets that no reader can change. Registered with
# `_caches`, which empties both before each test.
SNIPPET_TOKEN_ENTRIES = 5000
TOKEN_SURFACE_ENTRIES = 20_000

_SNIPPET_TOKENS: OrderedDict[str, _SnippetTokens] = OrderedDict()
_SNIPPET_TOKENS_LOCK = threading.Lock()
_TOKEN_SURFACES: OrderedDict[str, frozenset[str]] = OrderedDict()
_TOKEN_SURFACES_LOCK = threading.Lock()


def clear_snippet_caches() -> None:
    """Drop every scanned body and every interned raw token."""
    with _SNIPPET_TOKENS_LOCK:
        _SNIPPET_TOKENS.clear()
    with _TOKEN_SURFACES_LOCK:
        _TOKEN_SURFACES.clear()


_caches.register(clear_snippet_caches)


def _token_surfaces(raw: str) -> frozenset[str]:
    """`frozenset(_expand_kebab(tokenize(raw)))` for one raw token, interned
    across the bodies the snippet path scans."""
    with _TOKEN_SURFACES_LOCK:
        surfaces = _TOKEN_SURFACES.get(raw)
        if surfaces is not None:
            _TOKEN_SURFACES.move_to_end(raw)
            return surfaces
    surfaces = frozenset(_expand_kebab(_lexicon.tokenize(raw)))
    with _TOKEN_SURFACES_LOCK:
        _TOKEN_SURFACES[raw] = surfaces
        while len(_TOKEN_SURFACES) > TOKEN_SURFACE_ENTRIES:
            _TOKEN_SURFACES.popitem(last=False)
    return surfaces


def _snippet_tokens(scan: str) -> _SnippetTokens:
    """The anchor scan of `scan`, the first `_SNIPPET_SCAN_CHARS` stripped
    characters of a body (the caller slices it, and the slice is the memo's
    key), or the one an earlier call made of an equal slice."""
    with _SNIPPET_TOKENS_LOCK:
        cached = _SNIPPET_TOKENS.get(scan)
        if cached is not None:
            _SNIPPET_TOKENS.move_to_end(scan)
            return cached
    starts: list[int] = []
    surfaces: list[frozenset[str]] = []
    for m in _TOKEN_RE.finditer(scan):
        starts.append(m.start())
        surfaces.append(_token_surfaces(m.group()))
    tokens = _SnippetTokens(starts=tuple(starts), surfaces=tuple(surfaces))
    with _SNIPPET_TOKENS_LOCK:
        _SNIPPET_TOKENS[scan] = tokens
        while len(_SNIPPET_TOKENS) > SNIPPET_TOKEN_ENTRIES:
            _SNIPPET_TOKENS.popitem(last=False)
    return tokens


def _query_biased_snippet(body: str, matched: list[str], max_chars: int = 200) -> str:
    """`snippet_for`, but windowed on where the query actually hit.

    Head-of-body truncation answers "what does this memory start with",
    not "why did this memory come back": on a long body the matched terms
    routinely sit past character 200 and the caller is shown an unrelated
    opening paragraph. This walks the RAW body with `_TOKEN_RE` and
    normalises each raw token INDIVIDUALLY — `_expand_kebab(tokenize(tok))`,
    exactly the per-token slice of the `_MemoryTokens.body` stream the
    scorers count against, so membership in it is the same predicate that
    put the term in `matched`. Per-token and not whole-body because the
    normalised stream's offsets do not map back: NFKC, `.lower()` and
    `_fold_diacritics` are all length-changing, which is why the folded
    text cannot be used to locate anything in the raw one.

    Only `_build_hit` calls this. `snippet_for` keeps its head-of-body
    contract for every other consumer — write-time dedup's `SimilarHit`,
    consolidate's summaries — none of which have a query to bias toward.

    Falls back to `snippet_for` by DELEGATING to it, never by re-deriving
    it, whenever biasing is impossible or pointless: short body, no
    matched terms, no anchor found in the body, or an anchor already
    inside the head window.

    The scan of the body is the query-independent half and comes from
    `_snippet_tokens`, memoised per body with each raw token's surfaces
    interned across bodies; only the membership tests against this
    query's terms run per call.
    """
    text = body.strip()
    # This must stay the FIRST statement. The scan below costs one
    # `_tokenize_impl` call per raw body token the snippet memos have not
    # met, and `test_search_tokenizes_each_candidate_once` pins the
    # per-search call count. Its fixtures are 46-char bodies, so they exit
    # here and the count is unchanged; hoisting the scan above this line
    # breaks that test — and, more to the point, would spend the calls on
    # every hit whose body already fits whole.
    #
    # The `not matched` half covers the two populations that legitimately
    # carry no literal terms: browse mode (`_build_hit(..., matched=[])`)
    # and (historically) paraphrase-only semantic hits, whose `literal_matched` is empty
    # by design.
    if len(text) <= max_chars or not matched:
        return snippet_for(text, max_chars)

    # Tier 1 is the matched term itself. Tier 2 is its kebab components:
    # the scorers' conjunctive fallback marks 'claude-code' matched when
    # the body spells it 'Claude Code', and no raw token there normalises
    # to the compound. Tier 2 is consulted only when tier 1 found nothing,
    # so a compound that DID hit literally is never dragged off to a bare
    # 'python' mention elsewhere — the precision `_expand_kebab`'s
    # one-directional widening exists to protect.
    primary_terms = set(matched)
    part_terms = {p for tok in matched for p in _kebab_parts(tok)} - primary_terms

    scan = text[:_SNIPPET_SCAN_CHARS]
    scanned = _snippet_tokens(scan)
    starts: list[int] = list(scanned.starts)
    primary: list[int] = []
    secondary: list[int] = []
    for start, surfaces in zip(scanned.starts, scanned.surfaces):
        if not surfaces.isdisjoint(primary_terms):
            primary.append(start)
        elif not surfaces.isdisjoint(part_terms):
            secondary.append(start)

    # Symbol-aliased terms are invisible to the token scan above and can
    # only be found by re-running their own patterns — see
    # `_ALIAS_ANCHOR_PATTERNS`.
    for pattern, alias in _ALIAS_ANCHOR_PATTERNS:
        if alias in primary_terms:
            bucket = primary
        elif alias in part_terms:
            bucket = secondary
        else:
            continue
        for m in pattern.finditer(scan):
            bucket.append(m.start())
            starts.append(m.start())

    anchors = sorted(primary) or sorted(secondary)
    if not anchors:
        # Every matched term hit a SCOPE rather than the body (the
        # scorers' `scope_hit` term), or the body carries decomposed
        # combining marks that split a raw token where the folded stream
        # does not, or every occurrence lies past `_SNIPPET_SCAN_CHARS`.
        return snippet_for(text, max_chars)

    # Densest window wins. The rendered window opens `_SNIPPET_LEAD_CHARS`
    # BEFORE its anchor, so counting against `a + budget` would promise
    # anchors the window never reaches — count against
    # `a + budget - lead`, the span actually shown. `>` (not `>=`) keeps
    # the EARLIEST window on a tie, so a body of uniform density reads
    # exactly as it did before this existed.
    budget = max_chars - 3
    reach = budget - _SNIPPET_LEAD_CHARS
    starts = sorted(set(starts))
    best_at, best_cover = anchors[0], -1
    for i, anchor in enumerate(anchors):
        cover = bisect_left(anchors, anchor + reach) - i
        if cover > best_cover:
            best_cover, best_at = cover, anchor

    if best_at <= _SNIPPET_LEAD_CHARS:
        # Already inside the head window — serve it unchanged, with no
        # leading ellipsis. This is what keeps every head-anchored hit
        # byte-identical to what it was before.
        return snippet_for(text, max_chars)

    # Snap the window start to a token start so it never opens mid-word;
    # `_truncate_at_word` only ever guarded the trailing edge. The lookup
    # always lands, so there is no "nothing to snap to" case to guard:
    # `best_at` is itself in `starts` — the token loop and the alias loop
    # both feed every anchor position into it — and `lead <= best_at`, so
    # the first start at or after `lead` is at or before `best_at`.
    lead = best_at - _SNIPPET_LEAD_CHARS
    start = starts[bisect_left(starts, lead)]
    return snippet_window(text, start, max_chars)


def _build_hit(
    memory: Memory,
    score: float,
    matched: list[str],
    *,
    query_unique: int,
    anchor: ClaimAnchor | None = None,
    path_drift: bool = True,
) -> MemoryHit:
    """Construct a MemoryHit from a scored memory.

    `anchor` is where the record's claims are read (`verify.ClaimAnchor`,
    design 3.6): a homed record's at its anchor, as `memory_show` reads
    them; with None (a record with no home row, or a search with no
    anchoring) at the worktree its origin recorded.

    `detect_path_drift` is the only call here that touches the
    filesystem — one regex pass + up to 8 stat() calls per hit — and
    with `path_drift` False it is not made: the hit carries an empty
    drift report, for a caller that reads only the ranking. The
    body's already in memory at this point (load_all already ran), so
    the marginal cost is bounded by the cap inside `detect_path_drift`
    rather than corpus size.

    `_query_biased_snippet` adds a second regex pass over the body plus
    one `tokenize` per raw token no earlier scan met — but only for
    bodies longer than the snippet budget, bounded by
    `_SNIPPET_SCAN_CHARS`, and once per body while the snippet memos hold
    it. Both passes are per-HIT, not per-candidate: `_build_hit` runs at
    most `max_results` times (default 5), after the ranking has been
    trimmed.
    """
    drift = (
        detect_path_drift(
            memory.body,
            verified_paths=memory.verified_paths,
            absent_paths=memory.verified_absent_paths,
            worktree_root=(
                memory.origin.worktree_root
                if anchor is None and memory.origin is not None
                else None
            ),
            anchor=anchor,
        )
        if path_drift
        else PathDriftReport(checked=(), missing=())
    )
    return MemoryHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        category=memory.category,
        snippet=_query_biased_snippet(memory.body, matched),
        score=round(score, 4),
        relevance=_relevance_label(len(matched), query_unique),
        match_terms=matched,
        query_unique=query_unique,
        created=memory.created,
        updated=memory.updated,
        last_verified_at=memory.last_verified_at,
        path_drift_checked=len(drift.checked),
        path_drift_missing=len(drift.missing),
        path_drift_checked_paths=list(drift.checked),
        path_drift_missing_paths=list(drift.missing),
        path_drift_verified_paths=list(drift.verified),
        path_drift_expected_absent_paths=list(drift.expected_absent),
        path_drift_dropped_as_route_paths=list(drift.dropped_as_route),
        # The counts and the full path list stay full-set — every
        # absence the caller could see before is still on the hit. This
        # is the provenance subset the verdict escalates on, and it has
        # to ride the hit because the response builder is where the
        # verdict is computed and a bare path string carries no trace of
        # where it came from.
        path_drift_claim_anchored_missing_paths=list(drift.claim_anchored_missing),
    )


def _score_keyword(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    candidate_tokens: list[_MemoryTokens] | None = None,
    scaffold_terms: frozenset[str] | None = None,
    scaffold_weight: float = 0.0,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the original keyword scorer across all candidates. Returns
    `(memory, score, matched)` tuples for every candidate with `score > 0`.
    Order preserved from the input — sorting happens at the caller.

    `scaffold_terms` / `scaffold_weight` thread through to
    `score_memory` (L2, lane-internal — see there). None is the stock
    scorer.

    `candidate_tokens` (optional): precomputed `_MemoryTokens`,
    index-aligned with `candidates` — see `search()`."""
    out: list[tuple[Memory, float, list[str]]] = []
    for i, memory in enumerate(candidates):
        score, matched = score_memory(
            memory,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            tokens=candidate_tokens[i] if candidate_tokens is not None else None,
            scaffold_terms=scaffold_terms,
            scaffold_weight=scaffold_weight,
        )
        if score > 0:
            out.append((memory, score, matched))
    return out


def _score_bm25(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    candidate_tokens: list[_MemoryTokens] | None = None,
    stopword_fallback: bool = False,
    corpus_stats: CorpusStats | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the BM25 scorer across all candidates. Returns
    `(memory, score, matched)` tuples for candidates with `score > 0`.
    `candidate_tokens`: see `_score_keyword`; `stopword_fallback`: see
    `score_memory_bm25`;
    `corpus_stats`: see `compute_idf` — required for a correct ranking
    whenever `candidates` is a query-filtered subset rather than the
    whole corpus."""
    body_idf_map, scope_idf_map, avgdl = compute_idf(
        candidates, tokens=candidate_tokens, corpus_stats=corpus_stats
    )
    if avgdl <= 0:
        return []
    out: list[tuple[Memory, float, list[str]]] = []
    for i, memory in enumerate(candidates):
        score, matched = score_memory_bm25(
            memory,
            query_tokens,
            body_idf_map=body_idf_map,
            scope_idf_map=scope_idf_map,
            avgdl=avgdl,
            now=now,
            half_life_days=half_life_days,
            tokens=candidate_tokens[i] if candidate_tokens is not None else None,
            stopword_fallback=stopword_fallback,
        )
        if score > 0:
            out.append((memory, score, matched))
    return out


def _id_order(
    scored: list[tuple[Memory, float, list[str]]],
) -> list[str]:
    """Return memory ids sorted desc by score, with (created, id) tiebreakers.
    Matches the existing search() sort key so single-mode RRF degenerates
    to the same order as direct scoring would produce."""
    scored_sorted = sorted(
        scored,
        key=lambda x: (x[1], x[0].created, x[0].id),
        reverse=True,
    )
    return [memory.id for memory, _, _ in scored_sorted]


def _leg_top_evidence(scored: list[tuple[Memory, float, list[str]]]) -> int:
    """How many synthesized terms the leg's rank-1 candidate matched.

    Read off the leg's own fusion ordering — the same
    `(score, created, id)` ordering `_id_order` applies — so the
    candidate judged is the one that would have voted.
    """
    if not scored:
        return 0
    top = max(scored, key=lambda x: (x[1], x[0].created, x[0].id))
    return len(top[2])


def _leg_evidence_weight(evidence: int) -> float:
    """The rescue leg's RRF weight, scaled by its own evidence.

    Returns 0.0 below `_RESCUE_LEG_MIN_EVIDENCE` — the round-5 rule,
    unchanged: a leg resting on one matched term is a coincidence and
    votes nothing.

    Above the floor the SHIPPED form is flat, the full
    `_RESCUE_LEG_WEIGHT`, because that is what measures best for the
    audience that turns this lane on — see `_RESCUE_LEG_EVIDENCE_SCALING`
    for the arms and the reasoning. With scaling enabled the weight is
    instead the fraction of the full-evidence bar the leg's own evidence
    reaches, `evidence / _EVIDENCE_FULL_AT`, capped at 1. Either way the
    result is within [0, `_RESCUE_LEG_WEIGHT`].

    Round 6 used `(evidence - 1) / (_EVIDENCE_FULL_AT - 1)` instead. The
    offset existed only to map the floor to exactly half weight, which
    was a choice rather than a derivation, and it is the choice that
    cost the dev set two questions at recall@5 while the mechanism
    itself was working. The plain ratio has no offset to justify and
    introduces no constant.

    The forms differ only at the floor: 2/3 of full weight here against
    1/2 there. The round-5 labels independently measure that stratum at
    68.2% helpful, which the structural 66.7% corroborates to 1.5
    points — two numbers from different places, neither derived from
    the other. See addendum 10.
    """
    if evidence < _RESCUE_LEG_MIN_EVIDENCE:
        return 0.0
    if not _RESCUE_LEG_EVIDENCE_SCALING:
        return _RESCUE_LEG_WEIGHT
    scale = min(1.0, evidence / max(1, _EVIDENCE_FULL_AT))
    return _RESCUE_LEG_WEIGHT * scale


def _base_leg_weights(
    rankings: list[list[tuple[Memory, float, list[str]]]],
) -> list[float] | None:
    """Fusion weights for the base pair — None for the shipped flat 1.0s.

    With `_BASE_LEG_TRAILING_WITHHOLD` on and one base leg's rank-1
    matching strictly fewer query terms than the other's, the trailing
    leg is withheld (weight 0.0) and the leading leg keeps its full
    vote. Ties — the overwhelmingly common case — return None, so the
    fusion is byte-identical to the pre-round-9 engine.

    The withheld leg's ranking stays IN the fusion at weight zero (its
    unique candidates keep tail positions rather than vanishing) — the
    exact counterfactual the base-leg census labelled, so its labels
    transfer 1:1. See `_BASE_LEG_TRAILING_WITHHOLD` for the derivation
    and addendum 12 for the preregistration.
    """
    if not _BASE_LEG_TRAILING_WITHHOLD or len(rankings) != 2:
        return None
    m_a = _leg_top_evidence(rankings[0])
    m_b = _leg_top_evidence(rankings[1])
    if m_a == m_b:
        return None
    return [1.0, 0.0] if m_a > m_b else [0.0, 1.0]


def _hybrid_fuse(
    rankings: list[list[tuple[Memory, float, list[str]]]],
    *,
    rrf_k: int,
    weights: list[float] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Fuse multiple ranker outputs into one ranked list via RRF.

    Each input is a per-ranker `[(memory, score, matched), ...]` list.
    Output is `[(memory, rrf_score, matched_union), ...]` ordered desc
    by RRF score. `matched_union` is the union of matched terms across
    rankers that surfaced the memory, sorted for stability. `weights`
    (optional, index-aligned) passes through to
    `reciprocal_rank_fusion`; None is byte-identical to before the
    parameter existed.
    """
    if not rankings:
        return []

    by_id: dict[str, Memory] = {}
    matched_by_id: dict[str, set[str]] = {}
    ranking_id_lists: list[list[str]] = []
    for scored in rankings:
        ranking_id_lists.append(_id_order(scored))
        for memory, _, matched in scored:
            by_id.setdefault(memory.id, memory)
            matched_by_id.setdefault(memory.id, set()).update(matched)

    fused = reciprocal_rank_fusion(ranking_id_lists, k=rrf_k, weights=weights)
    if not fused:
        return []

    # Tiebreaker: equal RRF scores fall back to (created, id) desc, same
    # as single-mode search — preserves deterministic ordering under
    # microsecond-tied writes / mocked clocks.
    ordered_ids = sorted(
        fused.keys(),
        key=lambda mid: (fused[mid], by_id[mid].created, mid),
        reverse=True,
    )
    return [(by_id[mid], fused[mid], sorted(matched_by_id[mid])) for mid in ordered_ids]


def _browse_hits(
    browse_candidates: list[Memory],
    max_results: int,
    anchoring: Anchoring | None,
    path_drift: bool,
) -> list[MemoryHit]:
    """Sort the filtered candidates newest first; return them as zero-score hits."""
    # Browse mode: the candidates passed the same filter the scored
    # path applies; sort by `updated` desc and emit zero-score
    # hits with no match terms. Mirrors the post-rank trim the
    # scored branches do at the end of `search()`.
    browse_candidates.sort(key=lambda m: (m.updated, m.id), reverse=True)
    browsed = browse_candidates[:max_results]
    return [
        _build_hit(
            memory,
            score=0.0,
            matched=[],
            query_unique=0,
            anchor=anchor,
            path_drift=path_drift,
        )
        for memory, anchor in zip(
            browsed, _claim_anchors(anchoring, browsed), strict=True
        )
    ]


def _conversational_gates(
    query: str,
    query_tokens: list[str],
    now: datetime,
    *,
    mode: SearchMode,
    conversational: bool,
    stopword_fallback: bool,
) -> tuple[_TemporalReading | None, bool]:
    """Read the query's temporal shape; return the reading and the pricing gate."""
    # Lane L: parse the query's temporal shape once. Hybrid-only, like
    # the rescue lane, and skipped on the stopword fallback for the same
    # reason (the fallback's TF stream has different matched semantics).
    # A non-temporal query leaves `conv_reading` None and every path
    # below byte-identical to the flag being off.
    conv_reading: _TemporalReading | None = None
    if conversational and mode == "hybrid" and not stopword_fallback:
        reading = _temporal_reading(query, now)
        if reading.is_temporal:
            conv_reading = reading
    # L2: the PRICING gate — the repricing mechanisms' key, wider than
    # the temporal reading when the widening constant is set. The
    # window/selector rerank below stays keyed on `conv_reading` alone:
    # a scaffold-shaped query has no window to boost. With
    # `_CONV_SCAFFOLD_MIN_STEMS` None the predicate is constant-False
    # and `conv_pricing == (conv_reading is not None)` — L1's key.
    conv_pricing = conv_reading is not None
    if (
        not conv_pricing
        and conversational
        and mode == "hybrid"
        and not stopword_fallback
    ):
        conv_pricing = _conv_scaffold_shaped(query_tokens)
    return conv_reading, conv_pricing


def _rank_single_leg(
    mode: SearchMode,
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    candidate_tokens: list[_MemoryTokens],
    stopword_fallback: bool,
    corpus_stats: CorpusStats | None,
    track_legs: bool,
) -> tuple[list[tuple[Memory, float, list[str]]], set[str]]:
    """Rank by the keyword or BM25 leg alone; return the ranking and its ids."""
    lexical_ids: set[str] = set()
    if mode == "keyword":
        scored = _score_keyword(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            candidate_tokens=candidate_tokens,
        )
        # Sort by score, then created (newer wins on tie), then id as the
        # final discriminator. Without `id` the tiebreaker is undefined for
        # two memories that share both score and created timestamp — a real
        # case under microsecond-tied writes or under tests that mock the
        # clock. ULID-shaped ids are lexically time-ordered, so the final
        # tiebreaker also gives "newer wins" semantics.
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
        if track_legs:
            lexical_ids = {memory.id for memory, _, _ in scored}
    else:
        scored = _score_bm25(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            candidate_tokens=candidate_tokens,
            stopword_fallback=stopword_fallback,
            corpus_stats=corpus_stats,
        )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
        if track_legs:
            lexical_ids = {memory.id for memory, _, _ in scored}
    return scored, lexical_ids


def _keyword_scaffold(
    query_tokens: list[str], conv_pricing: bool
) -> frozenset[str] | None:
    """Pick the scaffold terms the keyword leg reprices; return them, or None."""
    # L2: the keyword leg's scaffold repricing, the fusion's other
    # half. Engaged only for a priced query with at least one
    # content term (an all-scaffold temporal query leaves the leg
    # stock — the shipped behavior), and only when the weight
    # constant is set; None is byte-identical to 6.1.0.
    conv_scaffold: frozenset[str] | None = None
    if conv_pricing and _CONV_KEYWORD_SCAFFOLD_WEIGHT is not None:
        distinct_q = list(dict.fromkeys(query_tokens))
        # Ordered stream in, dedup after — see `_conv_scaffold_shaped`.
        scaffold_q = list(dict.fromkeys(_conv_scaffold_terms(query_tokens)))
        if scaffold_q and len(scaffold_q) < len(distinct_q):
            conv_scaffold = frozenset(scaffold_q)
    return conv_scaffold


def _rescue_fuse(
    scored: list[tuple[Memory, float, list[str]]],
    rankings: list[list[tuple[Memory, float, list[str]]]],
    base_weights: list[float] | None,
    query_tokens: list[str],
    query_unique: int,
    *,
    candidates: list[Memory],
    candidate_tokens: list[_MemoryTokens],
    now: datetime,
    half_life_days: float,
    hybrid_stats: CorpusStats | None,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None,
    rrf_k: int,
) -> tuple[list[tuple[Memory, float, list[str]]], set[str]]:
    """Fuse the rescue leg in when the base top hit is unsure; return ranking, ids."""
    expansion_ids: set[str] = set()
    if scored:
        top_matched = set(scored[0][2]) & set(query_tokens)
        coverage = len(top_matched) / query_unique if query_unique else 0.0
    else:
        coverage = 0.0
    if coverage < _RESCUE_COVERAGE_GATE:
        exp_terms = _expansion_terms_impl(
            list(dict.fromkeys(query_tokens)),
            _EXPANSION_TABLES,
            _stem_token,
        )
        if exp_terms:
            exp_stats = _expansion_corpus_stats(
                hybrid_stats, corpus_stats_provider, exp_terms
            )
            exp_leg = _score_bm25(
                candidates,
                exp_terms,
                now=now,
                half_life_days=half_life_days,
                candidate_tokens=candidate_tokens,
                corpus_stats=exp_stats,
            )
            # A leg with one word of evidence does not get to
            # vote. RRF reads rank, not score, so a leg resting
            # on a single coincidental match contributes exactly
            # as much as one three synonyms agree on;
            # withholding it is the only way the fusion can tell
            # them apart. A withheld leg leaves `scored` as
            # the base fusion, so the result reproduces
            # a lane-on query whose leg found nothing — the
            # same shape the lane already has when `exp_terms`
            # comes back empty. The filler floor above is keyed
            # on `rescue_expansion`, not on the leg, so it
            # stays. See `_RESCUE_LEG_MIN_EVIDENCE`.
            leg_weight = (
                _leg_evidence_weight(_leg_top_evidence(exp_leg)) if exp_leg else 0.0
            )
            if leg_weight <= 0.0:
                exp_leg = []
            if exp_leg:
                expansion_ids = {m.id for m, _, _ in exp_leg}
                # The base pair keeps the SAME weights it fused
                # with above — one code path, no fork (addendum
                # 12's scope): a lane-on query must not restore
                # a vote the base fusion just withheld.
                scored = _hybrid_fuse(
                    rankings + [exp_leg],
                    rrf_k=rrf_k,
                    weights=[*(base_weights or (1.0, 1.0)), leg_weight],
                )
                # `match_terms` stays a subset of the CALLER's
                # tokens — synthesized terms explain the leg,
                # not the caller's query, and letting them into
                # the matched list would inflate the coverage
                # `relevance` is computed from. An
                # expansion-only hit therefore reads
                # relevance="low", match_terms=[] with
                # matched_leg="expansion" — the exact shape
                # pure-paraphrase hits have always had.
                qset = set(query_tokens)
                scored = [
                    (m, s, [t for t in matched if t in qset])
                    for m, s, matched in scored
                ]
    return scored, expansion_ids


def _rank_hybrid(
    candidates: list[Memory],
    query_tokens: list[str],
    query_unique: int,
    *,
    now: datetime,
    half_life_days: float,
    candidate_tokens: list[_MemoryTokens],
    stopword_fallback: bool,
    corpus_stats: CorpusStats | None,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None,
    rescue_expansion: bool,
    conv_pricing: bool,
    rrf_k: int,
    track_legs: bool,
) -> tuple[list[tuple[Memory, float, list[str]]], set[str], set[str]]:
    """Fuse the keyword and BM25 legs and any rescue; return ranking and leg ids."""
    lexical_ids: set[str] = set()
    expansion_ids: set[str] = set()
    hybrid_stats = _hybrid_corpus_stats(
        corpus_stats,
        query_tokens,
        len(candidates),
        rescue_expansion=rescue_expansion,
        conv_pricing=conv_pricing,
    )
    conv_scaffold = _keyword_scaffold(query_tokens, conv_pricing)
    rankings: list[list[tuple[Memory, float, list[str]]]] = [
        _score_keyword(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            candidate_tokens=candidate_tokens,
            scaffold_terms=conv_scaffold,
            scaffold_weight=_CONV_KEYWORD_SCAFFOLD_WEIGHT or 0.0,
        ),
        _score_bm25(
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            candidate_tokens=candidate_tokens,
            stopword_fallback=stopword_fallback,
            corpus_stats=hybrid_stats,
        ),
    ]
    # Snapshot the leg membership from the working list by name,
    # not position, so a future ranker joining `rankings` can't
    # silently change what "lexical" means here — the rescue leg
    # below joins the FUSION but deliberately never this snapshot.
    if track_legs:
        lexical_ids = {memory.id for ranking in rankings for memory, _, _ in ranking}
    # Round 9: a base leg whose rank-1 evidence trails its peer's is
    # withheld. Skipped on the stopword fallback exactly as the
    # rescue leg is — the fallback's TF stream has different matched
    # semantics. None (every tie, and the shipped default) fuses
    # byte-identically.
    base_weights = None if stopword_fallback else _base_leg_weights(rankings)
    scored = _hybrid_fuse(rankings, rrf_k=rrf_k, weights=base_weights)

    # Rescue expansion: one extra, down-weighted BM25 leg over
    # synthesized vocabulary, engaged only when the base fusion is
    # not confident about its own top hit. Skipped on the stopword
    # fallback (an all-filler query has no content tokens worth
    # expanding, and the fallback already runs a special TF stream
    # the expansion leg does not share).
    if rescue_expansion and not stopword_fallback:
        scored, expansion_ids = _rescue_fuse(
            scored,
            rankings,
            base_weights,
            query_tokens,
            query_unique,
            candidates=candidates,
            candidate_tokens=candidate_tokens,
            now=now,
            half_life_days=half_life_days,
            hybrid_stats=hybrid_stats,
            corpus_stats_provider=corpus_stats_provider,
            rrf_k=rrf_k,
        )
    return scored, lexical_ids, expansion_ids


def _rank_candidates(
    candidates: list[Memory],
    query: str,
    query_tokens: list[str],
    query_unique: int,
    *,
    now: datetime,
    mode: SearchMode,
    half_life_days: float,
    rrf_k: int,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None,
    stopword_fallback: bool,
    rescue_expansion: bool,
    conversational: bool,
    track_legs: bool,
) -> tuple[list[tuple[Memory, float, list[str]]], set[str], set[str]]:
    """Rank the admitted `candidates` by `mode`; return the ranking and leg ids."""
    # `matched_leg` bookkeeping. Populated only when the caller asked for
    # it, so the default path pays nothing; the sets are the ids each leg
    # actually SCORED, which is what makes the reported leg a statement
    # about the run rather than about `mode`. `expansion_ids` is the one
    # exception to the only-when-asked rule: the rescue block fills it
    # while it has the leg in hand (a set build over an already-scored
    # list), because re-deriving it at the trim would mean re-running
    # the leg.
    lexical_ids: set[str] = set()
    expansion_ids: set[str] = set()

    # Tokenize each candidate at most once per call and thread the streams
    # through every consumer below — the keyword scorer, compute_idf, BM25,
    # blocks otherwise re-tokenize the same
    # bodies and scopes (6 tokenize calls per memory per hybrid search,
    # ~88% of cumulative search time). Pure perf: see `_MemoryTokens`.
    # A candidate whose body and scopes an earlier call tokenised is
    # served from `_memory_tokens`' cache and not tokenised again.
    candidate_tokens = [_memory_tokens(m) for m in candidates]
    conv_reading, conv_pricing = _conversational_gates(
        query,
        query_tokens,
        now,
        mode=mode,
        conversational=conversational,
        stopword_fallback=stopword_fallback,
    )
    corpus_stats = _fetch_corpus_stats(corpus_stats_provider, mode, query_tokens)

    if mode == "hybrid":
        scored, lexical_ids, expansion_ids = _rank_hybrid(
            candidates,
            query_tokens,
            query_unique,
            now=now,
            half_life_days=half_life_days,
            candidate_tokens=candidate_tokens,
            stopword_fallback=stopword_fallback,
            corpus_stats=corpus_stats,
            corpus_stats_provider=corpus_stats_provider,
            rescue_expansion=rescue_expansion,
            conv_pricing=conv_pricing,
            rrf_k=rrf_k,
            track_legs=track_legs,
        )
    else:
        scored, lexical_ids = _rank_single_leg(
            mode,
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            candidate_tokens=candidate_tokens,
            stopword_fallback=stopword_fallback,
            corpus_stats=corpus_stats,
            track_legs=track_legs,
        )

    # Lane L's anchor selection runs last — after the rescue leg has
    # joined or declined the fusion — so a lane-on query reorders the
    # ranking the caller would otherwise have received, before the trim.
    if conv_reading is not None:
        scored = _conversational_rerank(scored, reading=conv_reading)

    return scored, lexical_ids, expansion_ids


def _ranked_hits(
    trimmed: list[tuple[Memory, float, list[str]]],
    anchoring: Anchoring | None,
    query_unique: int,
    path_drift: bool,
) -> list[MemoryHit]:
    """Anchor each trimmed hit's claims; return the hits `search` serves."""
    anchors = _claim_anchors(anchoring, [memory for memory, _, _ in trimmed])
    return [
        _build_hit(
            memory,
            score,
            matched,
            query_unique=query_unique,
            anchor=anchor,
            path_drift=path_drift,
        )
        for (memory, score, matched), anchor in zip(trimmed, anchors, strict=True)
    ]


def search(
    memories: list[Memory],
    query: str,
    *,
    scopes: list[str] | None = None,
    excluded_scopes: set[str] | None = None,
    admission: Admission | None = None,
    client_filter: str | None = None,
    model_filter: str | None = None,
    max_results: int = 5,
    now: datetime | None = None,
    half_life_days: float = 30.0,
    mode: SearchMode = "hybrid",
    rrf_k: int = _RRF_K_DEFAULT,
    allow_empty_query: bool = False,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None = None,
    matched_leg_out: dict[str, str] | None = None,
    rescue_expansion: bool = False,
    conversational: bool = True,
    anchoring: Anchoring | None = None,
    path_drift: bool = True,
) -> list[MemoryHit]:
    """Rank `memories` against `query` and return up to `max_results` hits.

    - `scopes`: if given, only consider memories tagged with at least one.
    - `excluded_scopes`: any memory tagged with one of these is dropped.
      (Used for session-disabled scopes.)
    - `admission`: the request's membership admission
      (`admission.Admission`): a memory whose home the search's surface
      does not admit for the caller is dropped (design 3.5), and so is a
      memory with no home. None ranks every memory (offline callers).
    - `client_filter` / `model_filter`: the writing request's DECLARED
      identity (`Memory.actor.client` / `.model`), matched exactly and
      case-sensitively. Unlike the admission, a memory carrying
      NO actor is dropped rather than passed: these select a writer
      rather than admitting a caller, and every memory written before
      7.10.0 has no actor to select on. Declared means forgeable —
      evidence for attribution and rollback, never a permission
      boundary.
    - `mode`: ranker selection. `"hybrid"` (default since 2.6.8: RRF
      fusion of keyword + BM25); `"keyword"` (legacy TF + coverage +
      recency scorer with no IDF weighting); `"bm25"` (Okapi BM25 with
      the same scope-bonus + recency boost).
    - `rrf_k`: smoothing constant for hybrid fusion. Larger spreads
      weight further down the list; smaller makes top ranks dominate.
      60 is the canonical default and almost always correct.
    - `allow_empty_query`: when True, an empty or stopword-only query
      no longer short-circuits to `[]`. Instead the function runs the
      `_filter_candidates` pass (scope / membership / excluded)
      and returns the survivors sorted by `updated` desc — a browse
      mode. Hits get `score=0.0`, no `match_terms`, and the default
      "low" relevance label. Used by callers that already narrowed
      the candidate pool externally (e.g. `since_prior_session=True`)
      and want recency ordering rather than relevance ranking. When
      False (the default), only a truly EMPTY query returns `[]`; a
      stopword-only query is ranked on its unstripped tokens instead
      (see `_query_tokens`'s fallback note), so stopword curation can
      never make a non-empty query unanswerable.
    - `matched_leg_out`: optional dict the function fills with
      `{memory_id: "lexical" | "expansion"}` for the hits it
      returns — WHICH RANKER surfaced each one (why an out-parameter:
      `search_steps._record_matched_legs`). `None` (the default) skips
      the bookkeeping entirely, so every existing caller is byte-stable
      in both output and cost. Browse-mode hits get no entry — nothing
      ranked them.
    - `rescue_expansion`: hybrid-mode only, DEFAULT OFF. When True, the
      two query-time repairs described above `_RESCUE_COVERAGE_GATE` run;
      the notes there also say why the default is off. Hits surfaced only
      by the expansion leg report `matched_leg="expansion"` with
      `match_terms` still an honest subset of the caller's own tokens
      (possibly empty — the same shape pure-paraphrase hits always had).
      False (the default) is the pre-5.1 two-leg behavior byte for byte.

      `keyword` and `bm25` modes are explicit instrument choices and
      are never touched. Above the index threshold the FTS prefilter
      nominates the pool from the CALLER's tokens, so there the rescue
      re-ranks the nominated pool rather than widening it — a
      documented limit, not a silent one.
    - `conversational`: hybrid-mode only, DEFAULT ON since 6.1.0 (the
      L1 ship, owner decision 2026-08-16). The Lane L repairs for
      conversation-shaped stores (the L1 declaration) run as described
      above `_CONV_BAND_TAU`; the notes there also say why the default
      is on. A query with no temporal reading is untouched byte for
      byte, as is every call with the flag off, so `conversational=False`
      reproduces the pre-6.1.0 ranking exactly.

    - `anchoring`: the request's claim anchoring (`verify.Anchoring`,
      design 3.6). Each hit's path drift reads a homed record's claims at
      its anchor, the reading `memory_show` and the expanded top hit
      take, so one memory reads one verdict on every hit of a search and
      on `memory_show` (ENG-165). A record with no home row, and every
      record when None (the offline callers), is read at the worktree
      its origin recorded, as `verdict.resolve_verdicts` reads it.

    - `path_drift`: False reads no hit's path claims, so the search
      stats nothing on the disk and every hit carries an empty drift
      report. For a caller that reads only the ranking (the session
      brief, `recall_brief._ranked`): a path on a mount that stopped
      answering would block it in stat() for a result it discards
      (ENG-169 G2).

    Score semantics vary by mode: keyword/BM25 scores live on
    different scales and are not comparable across modes. Hybrid scores
    are RRF outputs (~0.01-0.05 range, summed `1/(k+rank)` over rankers).
    Comparing hits across modes on the raw score is meaningless; compare
    on `relevance` plus `matched_leg`, which together say how much of the
    query the hit literally contains AND which ranker found it —
    `relevance` alone answers only the first, and answers it "low" for
    every pure-paraphrase hit.
    """
    _check_mode(mode)

    now = now or datetime.now(timezone.utc)
    query_tokens, stopword_fallback = _query_tokens(query, allow_empty_query)
    if not query_tokens and not allow_empty_query:
        return []
    candidates = _filter_candidates(
        memories,
        scopes=scopes,
        excluded_scopes=excluded_scopes,
        admission=admission,
        client_filter=client_filter,
        model_filter=model_filter,
    )
    if not query_tokens:
        return _browse_hits(candidates, max_results, anchoring, path_drift)
    if not candidates:
        return []

    query_unique = len(set(query_tokens))
    scored, lexical_ids, expansion_ids = _rank_candidates(
        candidates,
        query,
        query_tokens,
        query_unique,
        now=now,
        mode=mode,
        half_life_days=half_life_days,
        rrf_k=rrf_k,
        corpus_stats_provider=corpus_stats_provider,
        stopword_fallback=stopword_fallback,
        rescue_expansion=rescue_expansion,
        conversational=conversational,
        track_legs=matched_leg_out is not None,
    )
    trimmed = scored[:max_results]
    if matched_leg_out is not None:
        _record_matched_legs(trimmed, lexical_ids, expansion_ids, matched_leg_out)
    return _ranked_hits(trimmed, anchoring, query_unique, path_drift)


def _claim_anchors(
    anchoring: Anchoring | None, memories: Sequence[Memory]
) -> list[ClaimAnchor | None]:
    """Each memory's claim anchor under `anchoring`, its homes read in one
    query; None for a record with no home row and for every record when
    there is no anchoring."""
    if anchoring is None:
        return [None] * len(memories)
    anchoring.prime([memory.id for memory in memories])
    return [anchoring.for_record(memory.id) for memory in memories]
