"""Ranking memories against a query.

Four selectable rankers, dispatched by `search(mode=...)`:

- ``keyword`` (default in 1.6.0): the original TF + scope-weighted +
  coverage + recency scorer. Cheap, deterministic, good on
  identifier-heavy queries.
- ``bm25``: Okapi BM25 with IDF weighting, TF saturation, length
  normalisation, plus the same scope-bonus and recency multiplier as
  the keyword scorer. Better recall on rare-term queries.
- ``semantic``: sentence-transformers cosine over per-memory cached
  embeddings (extras-gated; raises a clear error when the embeddings
  extra isn't installed).
- ``hybrid``: reciprocal rank fusion (Cormack et al., SIGIR 2009)
  over keyword + BM25, plus semantic when a model is provided.
  Gracefully degrades to keyword+BM25 fusion when no model is
  available. The fused score lives in a different (much smaller)
  scale than the single-ranker scores — branch on `relevance`, not
  raw `score`, when comparing across modes.

`compute_idf` and `reciprocal_rank_fusion` are exported alongside
their per-mode scorers so callers can wire the rankers directly
without going through `search()`. The dedup path (`find_similar`)
is unchanged — it uses Jaccard or cosine over the existing
`_content_token_set` tokenizer.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Literal

from .models import Memory, MemoryHit, SimilarHit, TombstonedMemory, snippet_for
from .origin import should_include_for_caller
from .verify import detect_path_drift

# Search modes exposed via `search(mode=...)`. Default stays `keyword` in
# 1.6.0 to keep the existing behaviour byte-stable; the plan is to flip
# to `hybrid` once dogfooding has shaken out any ranking regressions.
SearchMode = Literal["keyword", "bm25", "semantic", "hybrid"]


# Strip punctuation, keep word characters (incl. unicode letters) and dashes
# inside tokens. Lowercase before tokenizing.
#
# `\w` (with `re.UNICODE`, which is the default in Python 3) is the right
# character class here: it covers ASCII alphanumerics plus the rest of
# Unicode's letters and digits, so a body like "Niño café Mañana" tokenizes
# correctly instead of fragmenting on each accented letter. The naive
# `[a-z0-9]` alternative — what this regex used to be — silently dropped
# every non-ASCII run after `.lower()` reduced the casing, which made
# non-English memories effectively unsearchable.
#
# `\w` also matches `_`, which we want to keep as token-internal anyway
# (it's how snake_case identifiers stay one token); the `[\w\-]` body just
# extends that with the literal hyphen so kebab-case stays whole too.
_TOKEN_RE = re.compile(r"\w[\w\-]*", re.UNICODE)

# Used by `_expand_kebab` to peel off sub-tokens from a kebab/snake compound.
_KEBAB_SPLIT_RE = re.compile(r"[-_]+")


# Short English stopword list. Stripped from the *query* only — bodies stay
# unfiltered so we don't lose information at index time. The point isn't NLP
# accuracy; it's stopping queries like "how to bake sourdough bread" from
# matching every memory on shared filler tokens ("how", "to"). We keep the
# list short and conservative — domain words ("get", "set", "run") stay in
# because they often *are* what the user is searching for.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


def tokenize(text: str) -> list[str]:
    """Pure regex tokenization. Whitespace and punctuation split; hyphens and
    underscores stay token-internal (so `python-frontmatter` is one token).
    Pair with `_expand_kebab` on indexed text if you also want to match by
    component.
    """
    return _TOKEN_RE.findall(text.lower())


def _expand_kebab(tokens: list[str]) -> list[str]:
    """Append the parts of any hyphen/underscore-joined token after the whole.

    `python-frontmatter` -> ['python-frontmatter', 'python', 'frontmatter'].

    Applied to indexed text (body, scope) only — never the query. The
    asymmetry is deliberate: a body containing `zephyr-quartz-9417` is
    *also* about `zephyr` and `quartz`, so a one-word query should hit it.
    But a query for `python-frontmatter` is a specific intent — we don't
    want it dragging in every body that happens to mention plain `python`.
    Index side widens; query side stays narrow.
    """
    out: list[str] = []
    for t in tokens:
        out.append(t)
        if "-" in t or "_" in t:
            for sub in _KEBAB_SPLIT_RE.split(t):
                if sub:
                    out.append(sub)
    return out


def _strip_stopwords(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t not in _STOPWORDS]


def _relevance_label(matched_unique: int, query_unique: int) -> str:
    """Map coverage (fraction of distinct query terms that hit) to a label.

    Calibrated for short queries: matching 1/1 or 2/2 is "high"; matching 1/3
    is "low". The thresholds are deliberately generous on the high side
    because a 1-word query with a strong match shouldn't be downgraded.
    """
    if query_unique <= 0:
        return "low"
    coverage = matched_unique / query_unique
    if coverage >= 0.75:
        return "high"
    if coverage >= 0.40:
        return "medium"
    return "low"


def _scope_tokens(scope: str) -> list[str]:
    """Break `projects:foo-bar` into ['projects', 'foo-bar', 'foo', 'bar']
    for matching — both the joined form and its components are emitted.
    """
    return _expand_kebab(tokenize(scope))


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


def compute_idf(memories: list[Memory]) -> tuple[dict[str, float], float]:
    """Build a per-term IDF map and the average doc length for BM25.

    `idf_map`: term -> log((N - df + 0.5) / (df + 0.5) + 1.0)`, the Okapi
    BM25 IDF variant that stays non-negative (so terms appearing in
    >half the corpus still contribute a tiny positive signal rather
    than pushing scores down).

    `avgdl`: average kebab-expanded stopword-stripped doc length across
    the corpus. Length normalisation in BM25 reads from this.

    Tokenisation here matches `_content_token_set` (the dedup path) on
    the body side — kebab expansion symmetric, stopwords stripped. The
    search-time query side strips stopwords too. Empty corpus returns
    `({}, 0.0)` so callers can short-circuit.
    """
    n = len(memories)
    if n == 0:
        return {}, 0.0

    df: dict[str, int] = {}
    total_len = 0
    for memory in memories:
        toks = _strip_stopwords(_expand_kebab(tokenize(memory.body)))
        total_len += len(toks)
        # Count each term once per doc — that's document-frequency, not
        # term-frequency. set() collapses repeats.
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    avgdl = total_len / n if n else 0.0
    idf_map: dict[str, float] = {
        term: math.log((n - dfi + 0.5) / (dfi + 0.5) + 1.0) for term, dfi in df.items()
    }
    return idf_map, avgdl


def score_memory_bm25(
    memory: Memory,
    query_tokens: list[str],
    *,
    idf_map: dict[str, float],
    avgdl: float,
    now: datetime,
    half_life_days: float = 30.0,
    k1: float = _BM25_K1_DEFAULT,
    b: float = _BM25_B_DEFAULT,
) -> tuple[float, list[str]]:
    """BM25 score for one memory against a tokenized query.

    Body terms scored via standard Okapi BM25: `idf * tf * (k1+1) /
    (tf + k1 * (1 - b + b*dl/avgdl))`. Scope matches add `2.0 * idf` as
    a fixed bonus, matching the keyword scorer's 2x scope weight so
    fusing the two rankers doesn't reweight scopes accidentally. The
    recency multiplier (`_recency_factor`) is applied at the end so a
    recently-edited memory climbs the same way it does in the keyword
    scorer.

    Returns `(score, matched_terms)`. `matched_terms` is the unique
    subset of `query_tokens` that hit body or scopes — used for the
    `match_terms` field on `MemoryHit` so the consumer sees which
    query words actually pulled the result up.

    Empty `query_tokens` or `avgdl <= 0` (empty corpus) returns
    `(0.0, [])`. Unknown terms (not in `idf_map`) contribute zero from
    the body but can still match a scope; scope-only matches default to
    `idf=1.0` since the term has no corpus statistics yet.
    """
    if not query_tokens or avgdl <= 0:
        return 0.0, []

    body_tokens = _strip_stopwords(_expand_kebab(tokenize(memory.body)))
    body_count: dict[str, int] = {}
    for tok in body_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1
    dl = len(body_tokens)

    scope_set: set[str] = set()
    for scope in memory.scopes:
        scope_set.update(_scope_tokens(scope))

    score = 0.0
    matched: list[str] = []
    seen: set[str] = set()
    length_norm = 1 - b + b * (dl / avgdl) if avgdl > 0 else 1.0
    for tok in query_tokens:
        contrib = 0.0

        tf = body_count.get(tok, 0)
        if tf > 0:
            idf = idf_map.get(tok, 0.0)
            denom = tf + k1 * length_norm
            contrib += idf * tf * (k1 + 1) / denom if denom > 0 else 0.0

        if tok in scope_set:
            # Floor IDF at 1.0 for scope-only hits so a brand-new scope
            # term (no body in the corpus yet) still contributes; the
            # 2x factor keeps it aligned with the keyword scorer.
            idf = idf_map.get(tok, 1.0)
            contrib += 2.0 * idf

        if contrib > 0 and tok not in seen:
            matched.append(tok)
            seen.add(tok)
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
) -> tuple[float, list[str]]:
    """Score a memory against a query. Return `(score, matched_terms)`.

    `matched_terms` is the de-duplicated subset of `query_tokens` that hit
    the body or scopes — surfaced in the result so the consumer can tell
    whether a partial match is meaningful or stopword-driven noise.
    """
    if not query_tokens:
        return 0.0, []

    body_tokens = _expand_kebab(tokenize(memory.body))
    body_count: dict[str, int] = {}
    for tok in body_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1

    scope_tokens: list[str] = []
    for scope in memory.scopes:
        scope_tokens.extend(_scope_tokens(scope))
    scope_set = set(scope_tokens)

    raw = 0.0
    matched: list[str] = []
    seen: set[str] = set()
    for tok in query_tokens:
        body_hits = body_count.get(tok, 0)
        scope_hit = 1 if tok in scope_set else 0
        contrib = body_hits + 2 * scope_hit  # scopes weighted 2x.
        if contrib > 0 and tok not in seen:
            matched.append(tok)
            seen.add(tok)
        raw += contrib

    if raw == 0.0:
        return 0.0, []

    # Mild boost for matching multiple distinct query terms — keeps "foo bar"
    # ranked above "foo foo foo" when the latter is just keyword spam.
    coverage = len(matched) / len(set(query_tokens))
    base = raw * (0.5 + 0.5 * coverage)

    # Recency boost reads from the freshness timestamp — `max(created, updated)`
    # — so an edited memory ranks like a new one. Without this, calling
    # memory_update on a year-old fact gives it the score of a year-old fact;
    # with it, refining a fact moves it up the list as you'd expect.
    freshness = max(memory.created, memory.updated)
    return base * _recency_factor(freshness, now, half_life_days), matched


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


def reciprocal_rank_fusion(
    ranking_lists: list[list[str]],
    *,
    k: int = _RRF_K_DEFAULT,
) -> dict[str, float]:
    """Fuse multiple ranked id-lists into one score-per-id map.

    Each `ranking_lists[i]` is a list of memory ids in best-first order
    for ranker i. The returned dict maps memory_id -> RRF score; sort
    descending to get the fused ranking. Ids that appear in no list are
    not present in the output. Duplicate ids within a single ranker's
    list are unusual but tolerated — the first (best-ranked) position
    wins for that ranker; later duplicates are ignored, matching the
    "one rank per (ranker, doc)" reading of the original paper.

    Empty `ranking_lists` returns an empty dict. `k` must be positive;
    the default (60) matches the Cormack et al. paper.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be positive, got {k}")
    if not ranking_lists:
        return {}

    fused: dict[str, float] = {}
    for ranking in ranking_lists:
        # Iterate with 1-indexed rank — the original formula assumes
        # rank starts at 1. `seen` guards the dedup contract above.
        seen: set[str] = set()
        for rank, memory_id in enumerate(ranking, start=1):
            if memory_id in seen:
                continue
            seen.add(memory_id)
            fused[memory_id] = fused.get(memory_id, 0.0) + 1.0 / (k + rank)
    return fused


def _filter_candidates(
    memories: list[Memory],
    *,
    scopes: list[str] | None,
    excluded_scopes: set[str] | None,
    repo_filter: str | None,
    worktree_filter: str | None,
) -> list[Memory]:
    """Apply scope / excluded-scope / repo / worktree filters.

    Extracted from `search()` so each search mode walks the same
    pre-filtered candidate list — fairness across rankers requires it,
    and it makes the per-mode scorers obviously equivalent on the
    filtering side. Order of `memories` is preserved.
    """
    scope_filter = set(scopes) if scopes else None
    excluded = excluded_scopes or set()
    out: list[Memory] = []
    for memory in memories:
        memory_scope_set = set(memory.scopes)
        if excluded and (memory_scope_set & excluded):
            continue
        if scope_filter is not None and not (memory_scope_set & scope_filter):
            continue
        if repo_filter is not None:
            if not should_include_for_caller(
                memory.origin,
                repo_filter,
                caller_worktree_root=worktree_filter,
            ):
                continue
        out.append(memory)
    return out


def _build_hit(
    memory: Memory,
    score: float,
    matched: list[str],
    *,
    query_unique: int,
) -> MemoryHit:
    """Construct a MemoryHit from a scored memory.

    `detect_path_drift` is the only call here that touches the
    filesystem — one regex pass + up to 8 stat() calls per hit. The
    body's already in memory at this point (load_all already ran), so
    the marginal cost is bounded by the cap inside `detect_path_drift`
    rather than corpus size.
    """
    drift = detect_path_drift(memory.body, verified_paths=memory.verified_paths)
    return MemoryHit(
        id=memory.id,
        scopes=memory.scopes,
        confidence=memory.confidence,
        category=memory.category,
        snippet=snippet_for(memory.body),
        score=round(score, 4),
        relevance=_relevance_label(len(matched), query_unique),
        match_terms=matched,
        created=memory.created,
        updated=memory.updated,
        last_verified_at=memory.last_verified_at,
        path_drift_checked=len(drift.checked),
        path_drift_missing=len(drift.missing),
        path_drift_checked_paths=list(drift.checked),
        path_drift_missing_paths=list(drift.missing),
        path_drift_verified_paths=list(drift.verified),
    )


def _score_keyword(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the original keyword scorer across all candidates. Returns
    `(memory, score, matched)` tuples for every candidate with `score > 0`.
    Order preserved from the input — sorting happens at the caller."""
    out: list[tuple[Memory, float, list[str]]] = []
    for memory in candidates:
        score, matched = score_memory(
            memory, query_tokens, now=now, half_life_days=half_life_days
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
) -> list[tuple[Memory, float, list[str]]]:
    """Run the BM25 scorer across all candidates. Returns
    `(memory, score, matched)` tuples for candidates with `score > 0`."""
    idf_map, avgdl = compute_idf(candidates)
    if avgdl <= 0:
        return []
    out: list[tuple[Memory, float, list[str]]] = []
    for memory in candidates:
        score, matched = score_memory_bm25(
            memory,
            query_tokens,
            idf_map=idf_map,
            avgdl=avgdl,
            now=now,
            half_life_days=half_life_days,
        )
        if score > 0:
            out.append((memory, score, matched))
    return out


def _score_semantic(
    candidates: list[Memory],
    query: str,
    semantic_model: Any,
    *,
    now: datetime,
    half_life_days: float,
    matched_terms_fallback: list[str],
) -> list[tuple[Memory, float, list[str]]]:
    """Cosine-similarity scoring over sentence-transformers embeddings.

    Reuses the per-memory cache from `bettermemory.semantic` so a search
    that runs alongside dedup shares vectors. `matched_terms_fallback`
    fills the `matched` slot for hits that came purely from semantic
    similarity — usually the stopword-stripped query tokens so the
    `match_terms` field on the resulting MemoryHit stays consistent
    with the keyword/BM25 paths.

    Threshold: hits with cosine < 0.3 are dropped. Below that, the
    similarity is noise — the model is matching style/structure rather
    than meaning. The threshold is conservative on purpose; we'd
    rather show fewer paraphrase hits than poison the result list
    with off-topic ones.
    """
    from .semantic import (
        cached_embed,
        cosine_similarity_normalized,
        flush_persistent_cache,
    )

    query_clean = query.strip()
    if not query_clean:
        return []
    query_vec = semantic_model.encode(query_clean, normalize_embeddings=True)

    threshold = 0.3
    out: list[tuple[Memory, float, list[str]]] = []
    for memory in candidates:
        body_clean = memory.body.strip()
        if not body_clean:
            continue
        body_vec = cached_embed(
            semantic_model,
            memory.id,
            memory.updated.isoformat(),
            body_clean,
        )
        sim = cosine_similarity_normalized(query_vec, body_vec)
        if sim < threshold:
            continue
        # Apply the same recency multiplier the other rankers use so a
        # stale paraphrase doesn't beat a fresh near-paraphrase.
        freshness = max(memory.created, memory.updated)
        score = sim * _recency_factor(freshness, now, half_life_days)
        out.append((memory, score, list(matched_terms_fallback)))
    flush_persistent_cache()
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


def _hybrid_fuse(
    rankings: list[list[tuple[Memory, float, list[str]]]],
    *,
    rrf_k: int,
) -> list[tuple[Memory, float, list[str]]]:
    """Fuse multiple ranker outputs into one ranked list via RRF.

    Each input is a per-ranker `[(memory, score, matched), ...]` list.
    Output is `[(memory, rrf_score, matched_union), ...]` ordered desc
    by RRF score. `matched_union` is the union of matched terms across
    rankers that surfaced the memory, sorted for stability.
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

    fused = reciprocal_rank_fusion(ranking_id_lists, k=rrf_k)
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


def search(
    memories: list[Memory],
    query: str,
    *,
    scopes: list[str] | None = None,
    excluded_scopes: set[str] | None = None,
    repo_filter: str | None = None,
    worktree_filter: str | None = None,
    max_results: int = 5,
    now: datetime | None = None,
    half_life_days: float = 30.0,
    mode: SearchMode = "keyword",
    semantic_model: Any | None = None,
    rrf_k: int = _RRF_K_DEFAULT,
) -> list[MemoryHit]:
    """Rank `memories` against `query` and return up to `max_results` hits.

    - `scopes`: if given, only consider memories tagged with at least one.
    - `excluded_scopes`: any memory tagged with one of these is dropped.
      (Used for session-disabled scopes.)
    - `repo_filter`: a remote URL. When provided, memories whose
      `origin.repo` doesn't match (compared via `origin.repos_match`) are
      dropped. Memories with no `origin.repo` (legacy or non-repo writes)
      pass through — they're treated as global.
    - `worktree_filter`: the caller's `git rev-parse --show-toplevel`
      path. Layered on top of `repo_filter` to catch worktree leakage:
      a memory written from one worktree of a repo shouldn't surface
      in a search run from a sibling worktree of the same repo.
      Memories with no `worktree_root` (legacy or non-repo writes)
      pass through. No-op without `repo_filter` — a worktree path
      without a repo identifier doesn't carry enough context to
      filter on.
    - `mode`: ranker selection. `"keyword"` (default, the original
      TF + coverage + recency scorer); `"bm25"` (Okapi BM25 with the
      same scope-bonus + recency boost); `"semantic"` (sentence-
      transformers cosine — requires `semantic_model`); `"hybrid"`
      (RRF fusion of keyword + BM25, plus semantic when a model is
      provided). The hybrid mode gracefully degrades when no
      semantic_model is given: it fuses keyword + BM25 only.
    - `semantic_model`: optional sentence-transformers model. Required
      for `mode="semantic"`; optional for `mode="hybrid"` (semantic is
      added to the fusion when present).
    - `rrf_k`: smoothing constant for hybrid fusion. Larger spreads
      weight further down the list; smaller makes top ranks dominate.
      60 is the canonical default and almost always correct.

    Score semantics vary by mode: keyword/BM25/semantic scores live on
    different scales and are not comparable across modes. Hybrid scores
    are RRF outputs (~0.01-0.05 range, summed `1/(k+rank)` over rankers).
    Use the `relevance` label, not the raw score, when comparing hits
    across modes.
    """
    if mode == "semantic" and semantic_model is None:
        raise ValueError("mode='semantic' requires semantic_model to be provided")

    now = now or datetime.now(timezone.utc)
    raw_tokens = tokenize(query)
    # Strip stopwords from the query — bodies stay unfiltered. If the query
    # was *only* stopwords ("what is the"), there's nothing meaningful left
    # to match on; return empty rather than serving every memory at score 0.
    query_tokens = _strip_stopwords(raw_tokens)
    if not query_tokens:
        return []

    query_unique = len(set(query_tokens))
    candidates = _filter_candidates(
        memories,
        scopes=scopes,
        excluded_scopes=excluded_scopes,
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
    )
    if not candidates:
        return []

    if mode == "keyword":
        scored = _score_keyword(
            candidates, query_tokens, now=now, half_life_days=half_life_days
        )
        # Sort by score, then created (newer wins on tie), then id as the
        # final discriminator. Without `id` the tiebreaker is undefined for
        # two memories that share both score and created timestamp — a real
        # case under microsecond-tied writes or under tests that mock the
        # clock. ULID-shaped ids are lexically time-ordered, so the final
        # tiebreaker also gives "newer wins" semantics.
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    elif mode == "bm25":
        scored = _score_bm25(
            candidates, query_tokens, now=now, half_life_days=half_life_days
        )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    elif mode == "semantic":
        # mypy: semantic_model is not None here (guarded above), but the
        # narrowing doesn't survive the assert-via-raise idiom across the
        # block boundary. Re-assert for the type checker.
        assert semantic_model is not None
        scored = _score_semantic(
            candidates,
            query,
            semantic_model,
            now=now,
            half_life_days=half_life_days,
            matched_terms_fallback=list(dict.fromkeys(query_tokens)),
        )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    else:  # mode == "hybrid"
        rankings: list[list[tuple[Memory, float, list[str]]]] = [
            _score_keyword(
                candidates, query_tokens, now=now, half_life_days=half_life_days
            ),
            _score_bm25(
                candidates, query_tokens, now=now, half_life_days=half_life_days
            ),
        ]
        if semantic_model is not None:
            rankings.append(
                _score_semantic(
                    candidates,
                    query,
                    semantic_model,
                    now=now,
                    half_life_days=half_life_days,
                    matched_terms_fallback=list(dict.fromkeys(query_tokens)),
                )
            )
        scored = _hybrid_fuse(rankings, rrf_k=rrf_k)

    return [
        _build_hit(memory, score, matched, query_unique=query_unique)
        for memory, score, matched in scored[:max_results]
    ]


# ---------------------------------------------------------------------------
# Dedup at write time
# ---------------------------------------------------------------------------


# Thresholds for find_similar. Calibrated against jaccard on stopword-stripped
# kebab-expanded token sets:
# - >= HIGH: block the write unless force=True. Two memories with this much
#   token overlap are very likely about the same fact; the right move is
#   memory_update on the existing entry.
# - >= MEDIUM: surface as `related` but do not block. The new memory may add
#   nuance worth keeping separate, but the writer should at least know the
#   adjacent memory exists.
# - <  MEDIUM: ignore.
HIGH_SIMILARITY = 0.75
MEDIUM_SIMILARITY = 0.40


def _content_token_set(text: str) -> set[str]:
    """Tokens used for similarity comparison: stopwords stripped, kebab/snake
    components included on both sides.

    Symmetric kebab expansion is the right move here (unlike search, where it
    is asymmetric). Two memories where one says `python-frontmatter` and the
    other says plain `python` are about overlapping topics; the dedup signal
    should fire. Inflating set size on the kebab side is the cost — accepted
    because the union grows in proportion and Jaccard stays well-behaved.
    """
    return set(_strip_stopwords(_expand_kebab(tokenize(text))))


def find_similar(
    new_body: str,
    existing: list[Memory],
    *,
    semantic_model: Any | None = None,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Find memories whose content overlaps `new_body` enough to flag.

    Default mode: Jaccard similarity on stopword-stripped, kebab-expanded
    token sets — symmetric and recency-free, unlike `score_memory`. Fast,
    deterministic, no extra deps.

    Semantic mode (when `semantic_model` is non-None): cosine similarity
    on sentence-transformers embeddings. Catches paraphrases that share
    no tokens — "the database" vs "Postgres", "shipped" vs "released".
    Pass a model object with an `encode(text, normalize_embeddings=True)`
    method (e.g. `sentence_transformers.SentenceTransformer`) — see
    `bettermemory.semantic.get_model()` for the loader.

    Thresholds default to the mode's natural range when None: 0.75/0.40
    for Jaccard, 0.85/0.65 for cosine. Pass explicit thresholds to tune.

    Returns hits with similarity >= medium_threshold, sorted descending
    by similarity. Hits below high_threshold are labeled `"medium"`; at
    or above, `"high"`. Empty when `new_body` has no content (or no
    tokens, in Jaccard mode).
    """
    if semantic_model is not None:
        return _find_similar_semantic(
            new_body,
            existing,
            semantic_model,
            high_threshold=(high_threshold if high_threshold is not None else 0.85),
            medium_threshold=(
                medium_threshold if medium_threshold is not None else 0.65
            ),
        )

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


def _find_similar_jaccard(
    new_body: str,
    existing: list[Memory],
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    new_tokens = _content_token_set(new_body)
    if not new_tokens:
        return []

    hits: list[SimilarHit] = []
    for memory in existing:
        existing_tokens = _content_token_set(memory.body)
        if not existing_tokens:
            continue

        intersection = new_tokens & existing_tokens
        if not intersection:
            continue

        union = new_tokens | existing_tokens
        similarity = len(intersection) / len(union)

        if similarity >= high_threshold:
            relevance = "high"
        elif similarity >= medium_threshold:
            relevance = "medium"
        else:
            continue

        hits.append(
            SimilarHit(
                id=memory.id,
                scopes=memory.scopes,
                confidence=memory.confidence,
                snippet=snippet_for(memory.body),
                similarity=round(similarity, 4),
                relevance=relevance,
                created=memory.created,
                updated=memory.updated,
            )
        )

    hits.sort(key=lambda h: (h.similarity, h.updated), reverse=True)
    return hits


def _find_similar_semantic(
    new_body: str,
    existing: list[Memory],
    model: Any,
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    """Cosine similarity over sentence-transformers embeddings.

    Imports `semantic` lazily so this module loads cleanly even when the
    embeddings extra isn't installed — a caller who never passes a
    `semantic_model` won't trigger the import path.
    """
    from .semantic import (
        cached_embed,
        cosine_similarity_normalized,
        flush_persistent_cache,
    )

    new_body_clean = new_body.strip()
    if not new_body_clean:
        return []

    new_vec = model.encode(new_body_clean, normalize_embeddings=True)

    hits: list[SimilarHit] = []
    for memory in existing:
        body_clean = memory.body.strip()
        if not body_clean:
            continue
        existing_vec = cached_embed(
            model,
            memory.id,
            memory.updated.isoformat(),
            body_clean,
        )
        similarity = cosine_similarity_normalized(new_vec, existing_vec)

        if similarity >= high_threshold:
            relevance = "high"
        elif similarity >= medium_threshold:
            relevance = "medium"
        else:
            continue

        hits.append(
            SimilarHit(
                id=memory.id,
                scopes=memory.scopes,
                confidence=memory.confidence,
                snippet=snippet_for(memory.body),
                similarity=round(similarity, 4),
                relevance=relevance,
                created=memory.created,
                updated=memory.updated,
            )
        )

    hits.sort(key=lambda h: (h.similarity, h.updated), reverse=True)
    # End-of-batch hook: persist any newly-computed embeddings as a
    # single atomic write. No-op when persistence isn't configured or
    # nothing changed since the last flush.
    flush_persistent_cache()
    return hits


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
    semantic_model: Any | None = None,
    high_threshold: float | None = None,
    medium_threshold: float | None = None,
) -> list[SimilarHit]:
    """Like `find_similar`, but scored against tombstoned memories and
    returning hits labeled with the `-removed` relevance suffix.

    Threshold defaults match the active path: 0.75/0.40 for Jaccard,
    0.85/0.65 for cosine. Empty input or empty body returns []. Hits
    are sorted descending by similarity, like `find_similar`.
    """
    if not tombstoned:
        return []

    if semantic_model is not None:
        return _find_similar_tombstones_semantic(
            new_body,
            tombstoned,
            semantic_model,
            high_threshold=(high_threshold if high_threshold is not None else 0.85),
            medium_threshold=(
                medium_threshold if medium_threshold is not None else 0.65
            ),
        )

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
    new_tokens = _content_token_set(new_body)
    if not new_tokens:
        return []

    hits: list[SimilarHit] = []
    for memory in tombstoned:
        existing_tokens = _content_token_set(memory.body)
        if not existing_tokens:
            continue

        intersection = new_tokens & existing_tokens
        if not intersection:
            continue

        union = new_tokens | existing_tokens
        similarity = len(intersection) / len(union)

        if similarity >= high_threshold:
            relevance = "high-removed"
        elif similarity >= medium_threshold:
            relevance = "medium-removed"
        else:
            continue

        hits.append(
            SimilarHit(
                id=memory.id,
                scopes=memory.scopes,
                confidence=memory.confidence,
                snippet=snippet_for(memory.body),
                similarity=round(similarity, 4),
                relevance=relevance,
                created=memory.created,
                updated=memory.updated,
                removed_at=memory.removed,
                removed_reason=memory.removed_reason,
            )
        )

    hits.sort(key=lambda h: (h.similarity, h.removed_at or h.updated), reverse=True)
    return hits


def _find_similar_tombstones_semantic(
    new_body: str,
    tombstoned: list[TombstonedMemory],
    model: Any,
    *,
    high_threshold: float,
    medium_threshold: float,
) -> list[SimilarHit]:
    """Cosine similarity over sentence-transformers embeddings, against
    tombstoned bodies. Mirrors `_find_similar_semantic` for the active path.

    Cache key uses `removed` rather than `updated` for tombstones: a
    tombstone's body is frozen post-removal (we don't bump `updated`
    on removal), so `removed` is the natural freshness handle and
    distinguishes the cache entry from any active-side cache that
    might exist for the same memory_id (e.g. immediately after a
    restore-then-tombstone cycle).
    """
    from .semantic import (
        cached_embed,
        cosine_similarity_normalized,
        flush_persistent_cache,
    )

    new_body_clean = new_body.strip()
    if not new_body_clean:
        return []

    new_vec = model.encode(new_body_clean, normalize_embeddings=True)

    hits: list[SimilarHit] = []
    for memory in tombstoned:
        body_clean = memory.body.strip()
        if not body_clean:
            continue
        existing_vec = cached_embed(
            model,
            f"tomb:{memory.id}",
            memory.removed.isoformat(),
            body_clean,
        )
        similarity = cosine_similarity_normalized(new_vec, existing_vec)

        if similarity >= high_threshold:
            relevance = "high-removed"
        elif similarity >= medium_threshold:
            relevance = "medium-removed"
        else:
            continue

        hits.append(
            SimilarHit(
                id=memory.id,
                scopes=memory.scopes,
                confidence=memory.confidence,
                snippet=snippet_for(memory.body),
                similarity=round(similarity, 4),
                relevance=relevance,
                created=memory.created,
                updated=memory.updated,
                removed_at=memory.removed,
                removed_reason=memory.removed_reason,
            )
        )

    hits.sort(key=lambda h: (h.similarity, h.removed_at or h.updated), reverse=True)
    flush_persistent_cache()
    return hits
