"""The steps of `search.search` that read nothing `search.py` holds.

`_check_mode` refuses an unknown mode; `_record_matched_legs` fills the
caller's `matched_leg_out` for the trimmed hits; `_query_tokens`
tokenizes the query and applies the stopword fallback;
`_fetch_corpus_stats` asks the corpus-stats provider for the query's terms
and their kebab parts; `_hybrid_corpus_stats` applies the filler and
scaffold df-floors the hybrid legs price with; `_expansion_corpus_stats`
fetches the rescue leg's synthesized terms and merges their stats on.

The steps that read a name tests patch on `search` (`_hybrid_fuse`,
`_leg_top_evidence`, `_BASE_LEG_TRAILING_WITHHOLD`, the `_CONV_*`
constants) or that call the scorers `search.py` holds stay there, beside
`search`. This module lives apart from `search.py` to keep each module
under the 1,900-line watch line below the package's 2,000-line bound
(ENG-97); from the family it imports `search_lexicon`, `search_bm25` and
`search_lane`, never `search`, and `search.py` imports it.
"""

from __future__ import annotations

from typing import Callable

from . import search_lexicon as _lexicon
from .models import Memory
from .search_bm25 import CorpusStats, _filler_floor_stats, _merge_corpus_stats
from .search_lane import _matched_leg, _scaffold_floor_stats
from .search_lexicon import _kebab_parts, _strip_stopwords


def _check_mode(mode: str) -> None:
    """Refuse a `mode` other than keyword, bm25 or hybrid; return nothing."""
    # Runtime guard against unknown modes. The `SearchMode` Literal pins
    # this at the type-checker layer, but the handler accepts an opaque
    # string from MCP and Python doesn't enforce Literals at call time.
    # Without this check, a typo like `mode="emantic"` would only be caught
    # after the candidates were filtered and tokenized, when
    # `search._rank_candidates` hands every mode but hybrid to
    # `search._rank_single_leg`, which refuses anything but keyword and bm25.
    # Raising here makes the failure mode loud at the dispatch boundary,
    # before any candidate is read, regardless of where the bad string came
    # from (handler, CLI, future programmatic client).
    if mode not in ("keyword", "bm25", "hybrid"):
        raise ValueError(
            f"unknown search mode {mode!r}; must be one of: keyword, bm25, hybrid"
        )


def _record_matched_legs(
    trimmed: list[tuple[Memory, float, list[str]]],
    lexical_ids: set[str],
    expansion_ids: set[str],
    matched_leg_out: dict[str, str],
) -> dict[str, str]:
    """Name the leg that surfaced each trimmed hit; return `matched_leg_out`."""
    # `search`'s `matched_leg_out` is an out-parameter rather than a
    # `MemoryHit` field because the leg is a property of THIS CALL's ranker
    # configuration, not of the memory, and every other consumer of
    # `MemoryHit` (memory_show, memory_list, the detail surfaces) has no
    # legs to report; the MCP search handler is the one surface where it is
    # actionable.
    for memory, _, _ in trimmed:
        leg = _matched_leg(
            lexical=memory.id in lexical_ids,
            expansion=memory.id in expansion_ids,
        )
        if leg:
            matched_leg_out[memory.id] = leg
    return matched_leg_out


def _query_tokens(query: str, allow_empty_query: bool) -> tuple[list[str], bool]:
    """Tokenize `query` minus stopwords; return the tokens and the fallback flag."""
    raw_tokens = _lexicon.tokenize(query)
    # Strip stopwords from the query — bodies stay unfiltered. If stripping
    # EMPTIES a non-empty token list ("what is the" — or a lone term some
    # future stopword addition absorbs), fall back to the unstripped
    # tokens: stopword curation must never make a real query unanswerable,
    # so the worst case is filler-grade ranking, not silent zero recall.
    # The fallback flag threads into `search`'s BM25 legs: BM25 counts TF
    # against the stopword-STRIPPED body stream, so without the signal a
    # fallen-back token could match scopes but never a body — silent zero
    # recall in mode="bm25", exactly what this fallback exists to rule
    # out (the keyword scorer's stream keeps stopwords and needs no flag).
    # A truly empty query still returns empty rather than serving every
    # memory at score 0, and browse mode keeps its recency-ordered
    # semantics for both empty and stopword-only queries — see
    # `search`'s `allow_empty_query`.
    query_tokens = _strip_stopwords(raw_tokens)
    stopword_fallback = False
    if not query_tokens and raw_tokens and not allow_empty_query:
        query_tokens = raw_tokens
        stopword_fallback = True
    return query_tokens, stopword_fallback


def _fetch_corpus_stats(
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None,
    mode: str,
    query_tokens: list[str],
) -> CorpusStats | None:
    """Ask the provider for the query's terms and parts; return the corpus stats."""
    # Corpus-wide document frequencies for the BM25 rankers, resolved from
    # the SAME terms the scorer will look up: every query token plus the
    # `_kebab_parts` components of each joined one. The conjunctive
    # fallback prices a joined token with no direct hit off its parts (min
    # component IDF), and `compute_idf`'s override only re-prices fetched
    # terms — a whole-tokens-only fetch would leave those parts at the
    # pool-collapsed IDF the provider exists to correct. That parity is
    # the whole reason this is a provider rather than a precomputed value:
    # the caller knowing which terms to fetch would mean re-deriving this
    # tokenisation outside `search()`, and a hand-mirrored token pipeline is
    # exactly the drift schema v4 removed. Only the BM25 branches consume
    # it, so a keyword-only search never pays the lookup.
    corpus_stats: CorpusStats | None = None
    if corpus_stats_provider is not None and mode in ("bm25", "hybrid"):
        fetch_terms = list(query_tokens)
        for tok in query_tokens:
            fetch_terms.extend(_kebab_parts(tok))
        corpus_stats = corpus_stats_provider(list(dict.fromkeys(fetch_terms)))
    return corpus_stats


def _hybrid_corpus_stats(
    corpus_stats: CorpusStats | None,
    query_tokens: list[str],
    pool_n: int,
    *,
    rescue_expansion: bool,
    conv_pricing: bool,
) -> CorpusStats | None:
    """Apply the filler and scaffold df-floors; return the hybrid legs' stats."""
    # The filler df-floor applies to the BM25 legs of the default
    # hybrid path only — `bm25` and `keyword` modes are explicit
    # instrument choices and stay pure. With `rescue_expansion`
    # off, the raw stats pass through and the branch is
    # byte-identical to pre-5.1.
    hybrid_stats = (
        _filler_floor_stats(corpus_stats, query_tokens, pool_n)
        if rescue_expansion
        else corpus_stats
    )
    # Lane L's scaffold floor composes AFTER the filler floor: both
    # only ever raise a term's df, they touch disjoint classes, and
    # the pricing gate is required — no priced reading, no
    # repricing. (L1 keyed this on the temporal reading; L2's gate
    # subsumes it and is identical while the widening ships dark.)
    if conv_pricing:
        hybrid_stats = _scaffold_floor_stats(hybrid_stats, query_tokens, pool_n)
    return hybrid_stats


def _expansion_corpus_stats(
    hybrid_stats: CorpusStats | None,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None,
    exp_terms: list[str],
) -> CorpusStats | None:
    """Fetch the synthesized terms' stats; return them merged onto `hybrid_stats`."""
    exp_stats = hybrid_stats
    if corpus_stats_provider is not None:
        # Above the threshold the floored query-term
        # stats say nothing about the synthesized
        # terms; fetch those too so a store-rare
        # synonym prices off the store, not the slice.
        #
        # Same terms-to-fetch rule as the base fetch
        # above, and for the same reason: a synthesized
        # term can itself be joined — `morph_variants`
        # rewrites the tail of a kebab token, so
        # "split-testing" emits "split-test" — and
        # `_score_bm25`'s conjunctive fallback prices a
        # joined term with no direct hit off its parts.
        # Fetching whole terms only would leave those
        # parts at the pool-collapsed IDF the provider
        # exists to correct.
        exp_fetch = list(exp_terms)
        for term in exp_terms:
            exp_fetch.extend(_kebab_parts(term))
        exp_stats = _merge_corpus_stats(
            hybrid_stats,
            corpus_stats_provider(list(dict.fromkeys(exp_fetch))),
        )
    return exp_stats
