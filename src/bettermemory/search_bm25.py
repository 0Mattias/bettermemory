"""The keyword and BM25 scorers `search.py` fuses, and their corpus statistics.

`score_memory` (the keyword TF and coverage scorer), `score_memory_bm25`
and `compute_idf`, with `CorpusStats`, the `_MemoryTokens` streams the
scorers accept, `_scope_tokens` and `_recency_factor`. The rescue lane's
expansion tables sit here with the filler df-floor that reads them, which
reprices terms through `CorpusStats`.

`search.py` keeps the legs that call these scorers, the token cache that
builds `_MemoryTokens` and the fusion, and re-exports the names tests and
other modules import from it. Its `_score_bm25` calls `compute_idf` under
the `search` name, so a patch of `search.compute_idf` reaches it. Every
tokenize call here goes through `search_lexicon` as `_lexicon.tokenize`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import NamedTuple

from . import search_lexicon as _lexicon
from .expansion import ExpansionTables, build_tables as _build_expansion_tables
from .models import Memory
from .search_lexicon import (
    _STOPWORDS,
    _expand_kebab,
    _kebab_parts,
    _stem_token,
    _strip_stopwords,
)


# Tables built once through the live stemmer so lookups and emitted
# terms share the rankers' post-stem token space. A stemmer rule change
# re-stems the tables automatically at import — stemmed literals in the
# tables would drift silently instead.
_EXPANSION_TABLES: ExpansionTables = _build_expansion_tables(_stem_token)


# Document-frequency floor for the QUERY_FILLER_WORDS list, as a
# fraction of the priced collection. Memory bodies are technical prose,
# so conversational filler is corpus-RARE, and Okapi IDF prices it like
# a discriminating term — a distractor matching "supposed" + "remember"
# outranked the right memory matching "paged" + "wake". The floor says:
# a word this common in QUESTIONS is common, full stop. df >= half the
# collection puts its IDF at ~log(2), ~14% of a genuinely-rare term's
# weight — deflated, never deleted (hard-stripping was measured to
# delete the only hooks some queries have). A floor (max with the real
# df), never a ceiling: filler genuinely common in a store keeps its
# honest pricing.
_FILLER_DF_FLOOR_RATIO = 0.5


def _filler_floor_stats(
    base: CorpusStats | None, terms: list[str], pool_n: int
) -> CorpusStats | None:
    """CorpusStats with the filler df-floor applied for `terms`.

    Returns `base` untouched when no term is on the filler list — the
    common path stays allocation-free and byte-stable. Otherwise the
    floor entries OVERRIDE per term through `compute_idf`'s existing
    corpus-stats mechanism, which only ever re-prices terms the pool
    actually carries — filler absent from every candidate body matches
    nothing and needs no cap. `max(real df, floor)` keeps the honest
    direction: the floor can only make filler look common, never make
    a genuinely common word look rare.
    """
    filler_present = [t for t in terms if t in _EXPANSION_TABLES.filler_stems]
    if not filler_present:
        return base
    size = base.size if base is not None else pool_n
    if size <= 0:
        return base
    floor = max(1, int(size * _FILLER_DF_FLOOR_RATIO))
    body_df = dict(base.body_df) if base is not None else {}
    scope_df = dict(base.scope_df) if base is not None else {}
    for t in filler_present:
        body_df[t] = max(body_df.get(t, 0), floor)
        scope_df[t] = max(scope_df.get(t, 0), floor)
    return CorpusStats(size=size, body_df=body_df, scope_df=scope_df)


def _merge_corpus_stats(
    a: CorpusStats | None, b: CorpusStats | None
) -> CorpusStats | None:
    """Union of two stats over the SAME collection; `b` wins per term.

    Used to fold a second provider fetch (expansion terms) into the
    already-floored query-term stats. Sizes describe the same admitted
    collection by construction; `b.size` is preferred with `a.size` as
    the fallback so a defensive zero can't zero the Okapi denominator.
    """
    if a is None:
        return b
    if b is None:
        return a
    body_df = dict(a.body_df)
    body_df.update(b.body_df)
    scope_df = dict(a.scope_df)
    scope_df.update(b.scope_df)
    return CorpusStats(
        size=b.size if b.size > 0 else a.size, body_df=body_df, scope_df=scope_df
    )


def _scope_tokens(scope: str) -> list[str]:
    """Break `projects:foo-bar` into ['projects', 'foo-bar', 'foo', 'bar']
    for matching — both the joined form and its components are emitted.
    """
    return _expand_kebab(_lexicon.tokenize(scope))


class _MemoryTokens(NamedTuple):
    """Per-memory token streams, computed once per distinct body and scopes
    and served from `_memory_tokens`' cache to every later search.

    `tokenize()` is the hot spot of a search (NFKC + diacritic fold +
    stemmer + CJK segmentation), and before this existed each candidate's
    body and scopes were re-tokenized by every consumer — the keyword
    scorer, `compute_idf`, and `score_memory_bm25` — 6 tokenize calls per
    memory per hybrid search, ~88% of cumulative search time on a
    ~500-memory store. The scorers accept this as an optional `tokens`
    argument; passing it must be a pure perf change (the fields are
    exactly the expressions the recompute path evaluates), pinned by the
    precompute-equality test in test_search.py. Consumers only read,
    never mutate, the shared lists and set: a cached entry outlives the
    search that built it, so a mutation would reach every later search
    (test_token_cache.py runs every lane and checks no entry changes).
    """

    body: list[str]
    """`_expand_kebab(tokenize(memory.body))` — stopwords kept (the
    keyword scorer's stream; `set()` of it was the literal-match
    stream)."""

    content: list[str]
    """`_strip_stopwords(body)` — the BM25/IDF stream."""

    scope_set: set[str]
    """Union of `_scope_tokens(scope)` across `memory.scopes` — every
    consumer builds a set from the per-scope lists, so only the union is
    kept."""


def _recency_factor(created: datetime, now: datetime, half_life_days: float) -> float:
    """1 + 0.1 * exp(-days_old / half_life). Mild bump, not a takeover."""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - created).total_seconds())
    age_days = age_seconds / 86400.0
    return 1.0 + 0.1 * math.exp(-age_days / max(half_life_days, 0.001))


# ---------------------------------------------------------------------------
# BM25 scorer (Okapi variant)
# ---------------------------------------------------------------------------
#
# The Jaccard / TF-coverage scorer below (score_memory) treats every term
# equally and adds a coverage multiplier. It works well for short, content-
# rich queries but undervalues rare terms and overvalues repeated common
# ones. BM25 corrects both: IDF weights rare terms higher, TF saturation
# clips diminishing returns on repeats, and length normalisation gives a
# small edge to focused short bodies over long ones with the same hit
# count. We keep the scope-match bonus and recency factor on top so the
# bettermemory-specific signals still apply — BM25 isn't a religion, it's
# one of several signals fused by RRF in hybrid mode.
#
# `compute_idf` is a one-pass corpus walk run once per search() call; it's
# O(total_tokens) and shows up nowhere on profiles for corpora under ~50K
# memories. When we add an inverted index (T3.1), the same shape returns
# directly from the index.


_BM25_K1_DEFAULT = 1.2
_BM25_B_DEFAULT = 0.75


class CorpusStats(NamedTuple):
    """Document frequencies over the collection a search ranks, for when
    `memories` is a subset of it.

    `size` is the ranked collection's document count; `body_df` / `scope_df`
    map a term to how many documents in that collection carry it, in the
    body and in body-or-scopes respectively — the two denominators
    `compute_idf` otherwise derives from the list it was handed.

    "The collection" is deliberately NOT the whole store:
    `index.corpus_document_frequencies` builds these over the admitted set
    — scopes, excluded scopes, repo and worktree, the same rule
    `_filter_candidates` applies — so the denominator describes documents
    the caller could actually retrieve. A provider that fed whole-store
    frequencies here would re-create the auto-scope mispricing c58c836
    removed. Which see for why the subset case needs this at all.
    """

    size: int
    body_df: dict[str, int]
    scope_df: dict[str, int]


def compute_idf(
    memories: list[Memory],
    *,
    tokens: list[_MemoryTokens] | None = None,
    corpus_stats: CorpusStats | None = None,
) -> tuple[dict[str, float], dict[str, float], float]:
    """Build the per-term IDF maps and the average doc length for BM25.

    Returns ``(body_idf_map, scope_idf_map, avgdl)``. Both maps carry
    `term -> log((N - df + 0.5) / (df + 0.5) + 1.0)`, the Okapi BM25 IDF
    variant that stays non-negative (so terms appearing in >half the
    corpus still contribute a tiny positive signal rather than pushing
    scores down) — they differ only in what counts toward df:

    - ``body_idf_map``: df over BODY tokens only. Prices the tf > 0
      branch in `score_memory_bm25` off body rarity. Scope tokens are
      deliberately excluded: a single shared map fed body scoring too,
      which crushed the body-match weight of any term that rides every
      candidate's scope — under auto-scoping that is the project's own
      name, the most-queried term of all, so a body literally answering
      'bettermemory crash' lost to a fresher body that never mentioned
      the project (the term's df hit N via scopes and its IDF ~0).
    - ``scope_idf_map``: df over body AND scope tokens (each term once
      per memory). Prices the `2.0 * idf` scope bonus, which
      self-deflates for ubiquitous namespace tokens: 'projects' sits on
      every project-scoped memory, so its df approaches N and its Okapi
      IDF approaches 0, while a discriminating scope token ('homelab')
      keeps a high IDF. Body-only IDF priced that bonus off body rarity
      alone, letting the bare namespace prefix outrank genuine
      full-coverage body matches.

    ``avgdl``: average kebab-expanded stopword-stripped doc length
    across the corpus. Length normalisation in BM25 reads from this.
    Scope tokens do NOT count toward `avgdl` — it is a body-length
    statistic.

    Tokenisation here matches `_content_token_set` (the dedup path) on
    the body side — kebab expansion symmetric, stopwords stripped. The
    search-time query side strips stopwords too. Empty corpus returns
    `({}, {}, 0.0)` so callers can short-circuit.

    ``tokens``: optional precomputed `_MemoryTokens`, index-aligned with
    `memories` — `search()` tokenizes each candidate once and threads the
    streams here. None recomputes them; identical output either way.

    ``corpus_stats``: optional ranked-collection document frequencies —
    counted over the ADMITTED set, not the whole store; see `CorpusStats`.
    Supply it whenever `memories` is a query-biased SUBSET of that
    collection — which is what the FTS prefilter produces above
    `_INDEX_THRESHOLD_DEFAULT`. Without it, df is counted over candidates
    that are present precisely because they matched, so a discriminative
    query term's df approaches N and its Okapi IDF collapses toward zero:
    BM25 degenerates into length normalisation plus recency for exactly the
    term that should dominate. Measured at 74x on a 608-memory store.

    Supplied stats OVERRIDE per term rather than replace the maps
    wholesale: any term the corpus lookup does not carry (a stale index, a
    tokenizer edge) keeps its pool-derived value, so the degraded path is
    never worse than not passing stats at all.

    ``avgdl`` stays pool-derived even with `corpus_stats`. It is a scalar
    length normaliser, so a biased sample shifts scores by a roughly common
    factor instead of inverting their order the way collapsed IDF does, and
    a corpus-wide average would cost a full vocabulary scan per search.
    """
    n = len(memories)
    if n == 0:
        return {}, {}, 0.0

    body_df: dict[str, int] = {}
    scope_df: dict[str, int] = {}
    total_len = 0
    for i, memory in enumerate(memories):
        pre = tokens[i] if tokens is not None else None
        toks = (
            pre.content
            if pre is not None
            else _strip_stopwords(_expand_kebab(_lexicon.tokenize(memory.body)))
        )
        total_len += len(toks)
        # Count each term once per doc — that's document-frequency, not
        # term-frequency. set() collapses repeats; scope tokens join the
        # scope-side per-doc set only (see docstring) and avgdl stays
        # body-only.
        body_terms = set(toks)
        for term in body_terms:
            body_df[term] = body_df.get(term, 0) + 1
        doc_terms = set(body_terms)
        if pre is not None:
            doc_terms.update(pre.scope_set)
        else:
            for scope in memory.scopes:
                doc_terms.update(_scope_tokens(scope))
        for term in doc_terms:
            scope_df[term] = scope_df.get(term, 0) + 1

    avgdl = total_len / n if n else 0.0

    def _okapi(df: dict[str, int], total: int) -> dict[str, float]:
        return {
            term: math.log((total - dfi + 0.5) / (dfi + 0.5) + 1.0)
            for term, dfi in df.items()
        }

    body_idf = _okapi(body_df, n)
    scope_idf = _okapi(scope_df, n)

    if corpus_stats is not None and corpus_stats.size > 0:
        # Per-term override, not a wholesale swap — see the docstring. A
        # corpus df of 0 is dropped rather than trusted: it means the index
        # disagrees with a body we can see in front of us (mid-write, or a
        # stale row), and the pool-derived value is the better guess there.
        corpus_n = corpus_stats.size
        for term, dfi in corpus_stats.body_df.items():
            if term in body_idf and dfi > 0:
                body_idf[term] = math.log((corpus_n - dfi + 0.5) / (dfi + 0.5) + 1.0)
        for term, dfi in corpus_stats.scope_df.items():
            if term in scope_idf and dfi > 0:
                scope_idf[term] = math.log((corpus_n - dfi + 0.5) / (dfi + 0.5) + 1.0)

    return body_idf, scope_idf, avgdl


def score_memory_bm25(
    memory: Memory,
    query_tokens: list[str],
    *,
    body_idf_map: dict[str, float],
    scope_idf_map: dict[str, float],
    avgdl: float,
    now: datetime,
    half_life_days: float = 30.0,
    k1: float = _BM25_K1_DEFAULT,
    b: float = _BM25_B_DEFAULT,
    tokens: _MemoryTokens | None = None,
    stopword_fallback: bool = False,
) -> tuple[float, list[str]]:
    """BM25 score for one memory against a tokenized query.

    Body terms scored via standard Okapi BM25: `idf * tf * (k1+1) /
    (tf + k1 * (1 - b + b*dl/avgdl))`, priced off `body_idf_map`. Scope
    matches add `2.0 * idf` as a fixed bonus priced off the
    scope-inclusive `scope_idf_map`, matching the keyword scorer's 2x
    scope weight so fusing the two rankers doesn't reweight scopes
    accidentally — see `compute_idf` for why the two signals read
    different df statistics. The recency multiplier (`_recency_factor`)
    is applied at the end so a recently-edited memory climbs the same
    way it does in the keyword scorer.

    Returns `(score, matched_terms)`. `matched_terms` is the unique
    subset of `query_tokens` that hit body or scopes — used for the
    `match_terms` field on `MemoryHit` so the consumer sees which
    query words actually pulled the result up.

    Empty `query_tokens` or `avgdl <= 0` (empty corpus) returns
    `(0.0, [])`. Unknown terms (not in `body_idf_map`) contribute zero
    from the body but can still match a scope; scope-only matches
    default to `idf=1.0` since the term has no corpus statistics yet.

    ``tokens``: optional precomputed `_MemoryTokens` for this memory —
    `search()` tokenizes each candidate once and threads the streams
    here. None recomputes them; identical output either way.

    ``stopword_fallback``: set by `search()` when stopword stripping
    emptied the query and the unstripped-token fallback fired (every
    query token is a stopword). Body TF is then counted against the
    UNSTRIPPED body stream — the keyword scorer's stream — instead of
    the stripped content stream, with body IDF floored at 1.0 (fallback
    tokens are stopwords, absent from the content-stream `body_idf_map`
    by construction; the same no-corpus-statistics floor scope-only
    matches use). `dl` and the caller's `avgdl` stay on the content
    stream so length normalisation prices one consistent statistic in
    both calls. False (the default) is byte-identical to the scoring
    behaviour before the flag existed; without it a fallen-back token
    could match scopes but never a body — silent zero recall in
    mode="bm25" for exactly the stopword-collision queries the
    `search()` fallback guarantees answerable.
    """
    if not query_tokens or avgdl <= 0:
        return 0.0, []

    # Fallback calls count TF against the unstripped stream (stopwords
    # kept) so the fallen-back tokens are matchable at all; `dl` stays
    # on the content stream in BOTH calls — avgdl is a content-stream
    # statistic, and length normalisation must keep pricing the same
    # ratio whether or not the fallback fired.
    if tokens is not None:
        content_tokens = tokens.content
        body_stream = tokens.body
    else:
        body_stream = _expand_kebab(_lexicon.tokenize(memory.body))
        content_tokens = _strip_stopwords(body_stream)
    count_tokens = body_stream if stopword_fallback else content_tokens
    body_count: dict[str, int] = {}
    for tok in count_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1
    dl = len(content_tokens)
    # Component counter for the conjunctive kebab fallback below, built
    # lazily (only when a compound query token misses directly). It reads
    # the UNSTRIPPED `body_stream` so a stopword component ('to' in
    # 'end-to-end', both parts of 'to-do') is countable at all: in
    # non-fallback mode `body_count` above is built from the stopword-
    # STRIPPED content stream, where such a component has count 0 and would
    # zero the min() — silent zero recall in mode='bm25' for the whole
    # X-to-X / X-by-X compound family. Mirrors the keyword scorer, whose
    # `body_count` is unstripped by construction.
    comp_count: dict[str, int] | None = None

    if tokens is not None:
        scope_set = tokens.scope_set
    else:
        scope_set = set()
        for scope in memory.scopes:
            scope_set.update(_scope_tokens(scope))

    score = 0.0
    matched: list[str] = []
    length_norm = 1 - b + b * (dl / avgdl) if avgdl > 0 else 1.0
    # De-duplicate query tokens (insertion-ordered) before accumulating:
    # `matched` always used set semantics, but the raw loop re-added the
    # full saturated-TF contribution (and the scope bonus) per duplicate,
    # so a reduplicated phrase query ("end to end") silently doubled the
    # repeated word's weight. Byte-identical for non-duplicated queries.
    for tok in dict.fromkeys(query_tokens):
        contrib = 0.0

        tf = body_count.get(tok, 0)
        # Fallback tokens are stopwords — absent from the content-stream
        # `body_idf_map` by construction — so floor their body IDF at 1.0
        # (the scope bonus's no-corpus-statistics floor below), or the
        # occurrence just counted would be zeroed right back by a 0.0 IDF.
        body_idf = body_idf_map.get(tok, 1.0 if stopword_fallback else 0.0)
        scope_hit = tok in scope_set
        # Floor IDF at 1.0 for scope-only hits so a brand-new scope
        # term (absent from every body AND scope in the idf corpus)
        # still contributes; the 2x factor keeps it aligned with the
        # keyword scorer. compute_idf counts scope tokens into the
        # scope-inclusive map's df, so known scope terms price off real
        # corpus statistics instead of this floor.
        scope_idf = scope_idf_map.get(tok, 1.0)
        if tf == 0 and not scope_hit:
            # Conjunctive fallback for a joined query token with no
            # direct hit — see `_kebab_parts`. ALL components must hit
            # (preserving the 'python-frontmatter' must-not-match-plain-
            # 'python' precision guard); tf is the min component count
            # (the joined phrase can occur at most that often) and IDF
            # is the min across components — the weakest component
            # bounds how discriminating the joined phrase can be.
            #
            # Range the conjunction over the UNFILTERED parts and count
            # them against the unstripped `comp_count`. Filtering the
            # stopword parts out instead collapsed the conjunction: an
            # 'X-to-X' compound reduced to a single-word OR ('end-to-end'
            # -> match any 'end'), and a compound whose parts are ALL
            # stopwords ('to-do' -> ['to','do']) emptied `parts` and
            # skipped the fallback entirely — and 'to-do' survives
            # `_strip_stopwords` too, so it never reached the stopword
            # fallback either: silent zero recall in mode='bm25' for the
            # whole X-to-X / X-by-X family. This mirrors the keyword
            # scorer, whose fallback already ranges over unfiltered parts
            # on an unstripped body_count.
            parts = _kebab_parts(tok)
            if parts:
                if comp_count is None:
                    comp_count = {}
                    for t in body_stream:
                        comp_count[t] = comp_count.get(t, 0) + 1
                component_hits = [comp_count.get(p, 0) for p in parts]
                if min(component_hits) > 0:
                    tf = min(component_hits)
                    # Price body IDF off the parts that carry content-
                    # stream corpus statistics — the non-stopword ones.
                    # When EVERY part is a stopword ('to-do') there is no
                    # body IDF to read; floor at 1.0 (the same no-corpus-
                    # statistics floor scope-only and fallback matches use)
                    # rather than letting a defaulted 0.0 IDF zero the
                    # occurrence just counted.
                    content_parts = [p for p in parts if p not in _STOPWORDS]
                    body_idf = (
                        min(body_idf_map.get(p, 0.0) for p in content_parts)
                        if content_parts
                        else 1.0
                    )
                if all(p in scope_set for p in parts):
                    scope_hit = True
                    scope_idf = min(scope_idf_map.get(p, 1.0) for p in parts)

        if tf > 0:
            denom = tf + k1 * length_norm
            contrib += body_idf * tf * (k1 + 1) / denom if denom > 0 else 0.0

        if scope_hit:
            contrib += 2.0 * scope_idf

        if contrib > 0:
            matched.append(tok)
        score += contrib

    if score <= 0:
        return 0.0, []

    freshness = max(memory.created, memory.updated)
    return score * _recency_factor(freshness, now, half_life_days), matched


def score_memory(
    memory: Memory,
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float = 30.0,
    tokens: _MemoryTokens | None = None,
    scaffold_terms: frozenset[str] | None = None,
    scaffold_weight: float = 0.0,
) -> tuple[float, list[str]]:
    """Score a memory against a query. Return `(score, matched_terms)`.

    `matched_terms` is the de-duplicated subset of `query_tokens` that hit
    the body or scopes — surfaced in the result so the consumer can tell
    whether a partial match is meaningful or stopword-driven noise.

    ``tokens``: optional precomputed `_MemoryTokens` for this memory —
    `search()` tokenizes each candidate once and threads the streams
    here. None recomputes them; identical output either way.

    ``scaffold_terms`` / ``scaffold_weight`` (L2, lane-internal): when
    `scaffold_terms` is given — search() passes it only for a priced
    conversational query with at least one content term — each scaffold
    term's contribution is multiplied by `scaffold_weight`, and the
    coverage multiplier is computed over content terms alone, so
    scaffold can neither pay for rank nor dilute coverage. A scaffold
    hit that coexists with content still appends to `matched_terms`
    (display and evidence read as before); a candidate whose every
    match is scaffold scores `raw == 0` at weight zero and drops from
    the leg. None (the default, and every non-lane caller) is the
    stock scorer, byte-identical to before the parameters existed.
    """
    if not query_tokens:
        return 0.0, []

    body_tokens = (
        tokens.body
        if tokens is not None
        else _expand_kebab(_lexicon.tokenize(memory.body))
    )
    body_count: dict[str, int] = {}
    for tok in body_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1

    if tokens is not None:
        scope_set = tokens.scope_set
    else:
        scope_set = set()
        for scope in memory.scopes:
            scope_set.update(_scope_tokens(scope))

    raw = 0.0
    matched: list[str] = []
    content_matched = 0
    query_unique = len(set(query_tokens))
    # De-duplicate query tokens (insertion-ordered) before accumulating:
    # coverage and `matched` always used set semantics, but the raw loop
    # re-added the full contribution per duplicate, so a reduplicated
    # phrase query ("end to end") silently doubled the repeated word's
    # weight. Byte-identical for non-duplicated queries.
    for tok in dict.fromkeys(query_tokens):
        body_hits = body_count.get(tok, 0)
        scope_hit = 1 if tok in scope_set else 0
        if body_hits == 0 and scope_hit == 0:
            # Conjunctive fallback for a joined query token with no
            # direct hit: 'claude-code' should match a body that spells
            # it 'Claude Code'. ALL components must hit (preserving the
            # 'python-frontmatter' must-not-match-plain-'python'
            # precision guard); the contribution is the min component
            # count — the joined phrase can occur at most that often.
            parts = _kebab_parts(tok)
            if parts:
                component_hits = [body_count.get(p, 0) for p in parts]
                if min(component_hits) > 0:
                    body_hits = min(component_hits)
                if all(p in scope_set for p in parts):
                    scope_hit = 1
        # Per-term body TF saturates at 2 (scopes stay weighted 2x). The
        # coverage multiplier below spans only 2x, so an unbounded TF sum
        # would overrun it: a single-term spam body capped at 2 tops out
        # at 2 * (0.5 + 0.5/n) <= 1.5, strictly below any full-coverage
        # match (raw >= n, multiplier 1.0) for every query length n >= 2.
        #
        # n == 1 is a deliberate carve-out: the proof above only covers
        # n >= 2, and at n == 1 the coverage multiplier is CONSTANT
        # (every matching doc has coverage 1/1), so there is no
        # cross-term coverage race for repeated TF to overrun. The hard
        # cap there erased ALL term-frequency discrimination instead —
        # a focal six-mention body and an incidental two-mention body
        # tied at raw 2, recency decided, and the mirrored hybrid RRF
        # tie's created-desc tiebreaker handed rank 1 (and expand_top's
        # inlined body) to the newer wrong doc. A log1p residual above
        # the cap restores monotone TF ordering while staying
        # sub-linear: 1000 mentions earn ~2 + log1p(998) ≈ 8.9 raw, not
        # 1000, so repetition has steeply diminishing returns rather
        # than a linear takeover.
        contrib: float = min(body_hits, 2) + 2 * scope_hit
        if query_unique == 1 and body_hits > 2:
            contrib += math.log1p(body_hits - 2)
        if contrib > 0:
            matched.append(tok)
        if scaffold_terms is not None and tok in scaffold_terms:
            raw += contrib * scaffold_weight
        else:
            raw += contrib
            if contrib > 0:
                content_matched += 1

    if raw == 0.0:
        return 0.0, []

    # Mild boost for matching multiple distinct query terms — together with
    # the per-term TF cap above, this is what actually keeps "foo bar"
    # ranked above "foo foo foo" when the latter is just keyword spam.
    if scaffold_terms is None:
        coverage = len(matched) / query_unique
    else:
        # L2: content coverage. The caller guarantees at least one
        # content term in the query, so the denominator is never zero.
        content_unique = sum(
            1 for t in dict.fromkeys(query_tokens) if t not in scaffold_terms
        )
        coverage = content_matched / content_unique
    base = raw * (0.5 + 0.5 * coverage)

    # Recency boost reads from the freshness timestamp — `max(created, updated)`
    # — so an edited memory ranks like a new one. Without this, calling
    # memory_update on a year-old fact gives it the score of a year-old fact;
    # with it, refining a fact moves it up the list as you'd expect.
    freshness = max(memory.created, memory.updated)
    return base * _recency_factor(freshness, now, half_life_days), matched
