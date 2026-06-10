"""Ranking memories against a query.

Four selectable rankers, dispatched by `search(mode=...)`:

- ``hybrid`` (default since 2.6.8): reciprocal rank fusion (Cormack
  et al., SIGIR 2009) over keyword + BM25, plus semantic when a model
  is provided. Gracefully degrades to keyword+BM25 fusion when no
  model is available, so the flipped default doesn't add a dep
  requirement. The fused score lives in a different (much smaller)
  scale than the single-ranker scores — branch on `relevance`, not
  raw `score`, when comparing across modes.
- ``keyword`` (legacy default in 1.6.0): the original TF +
  scope-weighted + coverage + recency scorer. Cheap, deterministic,
  good on identifier-heavy queries but lacks IDF — underperforms on
  rare-term queries vs. BM25/hybrid.
- ``bm25``: Okapi BM25 with IDF weighting, TF saturation, length
  normalisation, plus the same scope-bonus and recency multiplier as
  the keyword scorer.
- ``semantic``: sentence-transformers cosine over per-memory cached
  embeddings (extras-gated; raises a clear error when the embeddings
  extra isn't installed).

`compute_idf` and `reciprocal_rank_fusion` are exported alongside
their per-mode scorers so callers can wire the rankers directly
without going through `search()`. The dedup path (`find_similar`)
is unchanged — it uses Jaccard or cosine over the existing
`_content_token_set` tokenizer.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from .models import Memory, MemoryHit, SimilarHit, TombstonedMemory, snippet_for
from .origin import should_include_for_caller
from .verify import detect_path_drift

log = logging.getLogger("bettermemory.search")

# Search modes exposed via `search(mode=...)`. Default is `hybrid` since
# 2.6.8 — the keyword scorer lacks IDF weighting and underperforms on
# rare-term queries, and hybrid degrades gracefully to keyword+BM25
# fusion when no embedding extra is installed (so flipping the default
# doesn't add a dep requirement).
SearchMode = Literal["keyword", "bm25", "semantic", "hybrid"]


# Strip punctuation, keep word characters (incl. unicode letters) and dashes
# inside tokens. Lowercase (and diacritic-fold — see `_fold_diacritics`)
# before tokenizing.
#
# `\w` (with `re.UNICODE`, which is the default in Python 3) is the right
# character class here: it covers ASCII alphanumerics plus the rest of
# Unicode's letters and digits, so a body like "Niño café Mañana" tokenizes
# correctly instead of fragmenting on each accented letter. The naive
# `[a-z0-9]` alternative — what this regex used to be — silently dropped
# every non-ASCII run after `.lower()` reduced the casing, which made
# non-English memories effectively unsearchable.
#
# `\w` also matches `_`, but `tokenize` canonicalizes `_` to `-` before this
# regex runs so snake_case and kebab-case spell the same token; the `[\w\-]`
# body keeps the hyphen token-internal so kebab-case stays whole.
#
# Two shape constraints beyond the original `\w[\w\-]*`:
# - the first alternative keeps dotted numeric literals whole ('16.3',
#   '3.12.1'), so a version pin survives as one token instead of
#   fragmenting into bare digits that match any enumeration digit;
# - a token must END on a word character, so suspended hyphenation
#   ("pre- and post-deploy") yields the matchable 'pre', not the dead
#   query token 'pre-'.
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)+|\w(?:[\w\-]*\w)?", re.UNICODE)

# Used by `_expand_kebab` to peel off sub-tokens from a kebab/snake compound.
_KEBAB_SPLIT_RE = re.compile(r"[-_]+")

# Possessive/contraction suffixes ("what's", "don't", "I'm" — straight or
# curly apostrophe) are stripped before tokenization. Without this the
# orphan fragment ('s', 't', 'm', ...) survives stopword stripping, deflates
# the relevance-coverage denominator, and gets reported in `match_terms`
# whenever a body happens to contain any possessive. The pattern is anchored
# to the apostrophe, so legitimate standalone tokens ("re", "d") and
# non-contraction apostrophes ("o'clock", trailing "users'") are untouched.
_CONTRACTION_RE = re.compile(r"(?<=\w)['’](?:s|t|d|m|ll|re|ve)\b")

# Fixed alias allowlist for the handful of symbol-bearing tech names that
# `_TOKEN_RE` would otherwise collapse to a bare letter ('C++' -> 'c',
# indistinguishable from a list-enumeration 'c.'). Applied symmetrically —
# `tokenize` serves query and indexed text alike — with word-ish boundaries
# so arithmetic, markdown headers, and substrings like 'asp.net' don't
# fire. Deliberately a tiny hard-coded list rather than widening _TOKEN_RE
# to accept '+'/'#', which would change tokens for every body. 'c++' maps
# to 'cpp ' (trailing space) so 'C++20' tokenizes as ['cpp', '20'] and a
# bare 'C++' query still hits it.
_SYMBOL_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\w)c\+\+"), "cpp "),
    (re.compile(r"(?<!\w)c#(?!\w)"), "csharp"),
    (re.compile(r"(?<!\w)f#(?!\w)"), "fsharp"),
    (re.compile(r"(?<!\w)\.net(?!\w)"), "dotnet"),
)


# Short English stopword list. Stripped from the *query* only — bodies stay
# unfiltered so we don't lose information at index time. The point isn't NLP
# accuracy; it's stopping queries like "how to bake sourdough bread" from
# matching every memory on shared filler tokens ("how", "to"). We keep the
# list short and conservative — domain words ("get", "set", "run") stay in
# because they often *are* what the user is searching for. 'about' and the
# indefinite pronouns ('anything', 'something', 'everything') are pure
# grammatical filler within word classes the list already covers; without
# them, natural retrieval phrasings ("anything stored about X") deflate the
# relevance-coverage denominator and label exact-topic hits 'low'.
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "anything",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "does",
        "everything",
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
        "something",
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


def _fold_diacritics(text: str) -> str:
    """NFD-decompose and drop combining marks, so 'Zürich' and 'zurich'
    share one token form.

    This mirrors the accent-insensitive matching of the FTS5 ``unicode61``
    tokenizer in index.py (whose ``remove_diacritics`` defaults on),
    closing the prefilter/ranker disagreement where the index returned a
    candidate that every Python ranker then scored 0. The NFD pass also
    normalises combining-mark *input*: a body pasted from macOS in
    decomposed form ('Tjörn' as 'o' + U+0308) previously SPLIT at the mark
    — ``\\w`` excludes category Mn — yielding ['tjo', 'rn']; now both the
    precomposed and decomposed spellings fold to 'tjorn'."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch)
    )


def tokenize(text: str) -> list[str]:
    """Regex tokenization behind a small symmetric normalisation pipeline.

    Whitespace and punctuation split; hyphens stay token-internal (so
    `python-frontmatter` is one token). Every normalisation applies to
    query and indexed text alike (tokenize serves both sides):

    - lowercase, then fold diacritics (see `_fold_diacritics`);
    - strip possessive/contraction suffixes ("what's" -> "what");
    - alias symbol-bearing tech names ("C++" -> "cpp", see
      `_SYMBOL_ALIASES`);
    - canonicalize '_' to '-' so `docker_compose` and `docker-compose`
      spell the same token;
    - keep dotted numerics whole ('16.3') and end tokens on a word
      character ('pre-' -> 'pre'), per `_TOKEN_RE`.

    Pair with `_expand_kebab` on indexed text if you also want to match by
    component.
    """
    text = _fold_diacritics(text.lower())
    text = _CONTRACTION_RE.sub("", text)
    for pattern, replacement in _SYMBOL_ALIASES:
        text = pattern.sub(replacement, text)
    return _TOKEN_RE.findall(text.replace("_", "-"))


def _expand_kebab(tokens: list[str]) -> list[str]:
    """Append the parts of any hyphen/underscore-joined token after the whole.

    `python-frontmatter` -> ['python-frontmatter', 'python', 'frontmatter'].

    Applied to indexed text (body, scope) only — never the query. The
    asymmetry is deliberate: a body containing `zephyr-quartz-9417` is
    *also* about `zephyr` and `quartz`, so a one-word query should hit it.
    But a query for `python-frontmatter` is a specific intent — we don't
    want it dragging in every body that happens to mention plain `python`.
    Index side widens; query side stays narrow.

    Dotted numeric tokens get the same index-side treatment: '16.3' also
    emits '16' and '3', so a query for 'postgres 16' still hits a body
    that says 'Postgres 16.3' — while the query token '16.3' can no
    longer be satisfied by a stray enumeration digit.
    """
    out: list[str] = []
    for t in tokens:
        out.append(t)
        if "-" in t or "_" in t:
            for sub in _KEBAB_SPLIT_RE.split(t):
                if sub:
                    out.append(sub)
        elif "." in t:
            # Only dotted numerics ('16.3') survive _TOKEN_RE with a '.'.
            for sub in t.split("."):
                if sub:
                    out.append(sub)
    return out


def _kebab_parts(tok: str) -> list[str]:
    """Components of a hyphen/underscore-joined token, or [] when the token
    isn't joined. Used by the conjunctive fallback in the scorers: a joined
    query token with no direct hit ('claude-code' against a body spelling
    it 'Claude Code') counts as matched iff ALL its components hit."""
    if "-" not in tok and "_" not in tok:
        return []
    parts = [p for p in _KEBAB_SPLIT_RE.split(tok) if p]
    return parts if len(parts) >= 2 else []


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


def _endorsement_factor(applied_count: int) -> float:
    """1 + 0.1 * (1 - exp(-applied_count / 3)). Mild usage bump, bounded to
    [1.0, 1.1) — exactly the ceiling `_recency_factor` uses.

    A memory the model has DELIBERATELY applied (an explicit
    `memory_record_use(applied)`, not the auto-fallback) climbs slightly, so
    a load-bearing fact wins a near-tie over a never-endorsed peer. The cap
    is the whole point: like recency, it can only break near-ties, never
    override the relevance signal — which keeps it from a rich-get-richer
    runaway. `applied_count == 0` returns exactly 1.0 (neutral), so the
    factor is a no-op unless real endorsement counts are supplied."""
    if applied_count <= 0:
        return 1.0
    return 1.0 + 0.1 * (1.0 - math.exp(-applied_count / 3.0))


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
    the corpus. Length normalisation in BM25 reads from this. Scope
    tokens do NOT count toward `avgdl` — it is a body-length statistic.

    Document frequency counts each memory's scope tokens alongside its
    body tokens (each term once per memory), so the `2.0 * idf` scope
    bonus in `score_memory_bm25` self-deflates for ubiquitous namespace
    tokens: 'projects' sits on every project-scoped memory, so its df
    approaches N and its Okapi IDF approaches 0, while a discriminating
    scope token ('homelab') keeps a high IDF. Body-only IDF priced that
    bonus off body rarity alone, letting the bare namespace prefix
    outrank genuine full-coverage body matches.

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
        # term-frequency. set() collapses repeats; scope tokens join the
        # per-doc set (see docstring) while avgdl stays body-only.
        doc_terms = set(toks)
        for scope in memory.scopes:
            doc_terms.update(_scope_tokens(scope))
        for term in doc_terms:
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
    length_norm = 1 - b + b * (dl / avgdl) if avgdl > 0 else 1.0
    # De-duplicate query tokens (insertion-ordered) before accumulating:
    # `matched` always used set semantics, but the raw loop re-added the
    # full saturated-TF contribution (and the scope bonus) per duplicate,
    # so a reduplicated phrase query ("end to end") silently doubled the
    # repeated word's weight. Byte-identical for non-duplicated queries.
    for tok in dict.fromkeys(query_tokens):
        contrib = 0.0

        tf = body_count.get(tok, 0)
        body_idf = idf_map.get(tok, 0.0)
        scope_hit = tok in scope_set
        # Floor IDF at 1.0 for scope-only hits so a brand-new scope
        # term (absent from every body AND scope in the idf corpus)
        # still contributes; the 2x factor keeps it aligned with the
        # keyword scorer. compute_idf counts scope tokens into df, so
        # known scope terms price off real corpus statistics instead
        # of this floor.
        scope_idf = idf_map.get(tok, 1.0)
        if tf == 0 and not scope_hit:
            # Conjunctive fallback for a joined query token with no
            # direct hit — see `_kebab_parts`. ALL components must hit
            # (preserving the 'python-frontmatter' must-not-match-plain-
            # 'python' precision guard); tf is the min component count
            # (the joined phrase can occur at most that often) and IDF
            # is the min across components — the weakest component
            # bounds how discriminating the joined phrase can be.
            parts = _kebab_parts(tok)
            if parts:
                component_hits = [body_count.get(p, 0) for p in parts]
                if min(component_hits) > 0:
                    tf = min(component_hits)
                    body_idf = min(idf_map.get(p, 0.0) for p in parts)
                if all(p in scope_set for p in parts):
                    scope_hit = True
                    scope_idf = min(idf_map.get(p, 1.0) for p in parts)

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
        contrib = min(body_hits, 2) + 2 * scope_hit
        if contrib > 0:
            matched.append(tok)
        raw += contrib

    if raw == 0.0:
        return 0.0, []

    # Mild boost for matching multiple distinct query terms — together with
    # the per-term TF cap above, this is what actually keeps "foo bar"
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
    drift = detect_path_drift(
        memory.body,
        verified_paths=memory.verified_paths,
        absent_paths=memory.verified_absent_paths,
    )
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
        path_drift_expected_absent_paths=list(drift.expected_absent),
    )


def _score_keyword(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    applied_by_id: dict[str, int] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the original keyword scorer across all candidates. Returns
    `(memory, score, matched)` tuples for every candidate with `score > 0`.
    Order preserved from the input — sorting happens at the caller.

    `applied_by_id` (optional) maps memory id → explicit-applied count; when
    given, a bounded `_endorsement_factor` nudges endorsed memories. None
    (the default) leaves scores untouched."""
    out: list[tuple[Memory, float, list[str]]] = []
    for memory in candidates:
        score, matched = score_memory(
            memory, query_tokens, now=now, half_life_days=half_life_days
        )
        if score > 0:
            if applied_by_id:
                score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
            out.append((memory, score, matched))
    return out


def _score_bm25(
    candidates: list[Memory],
    query_tokens: list[str],
    *,
    now: datetime,
    half_life_days: float,
    applied_by_id: dict[str, int] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Run the BM25 scorer across all candidates. Returns
    `(memory, score, matched)` tuples for candidates with `score > 0`.
    `applied_by_id`: see `_score_keyword`."""
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
            if applied_by_id:
                score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
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
    applied_by_id: dict[str, int] | None = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Cosine-similarity scoring over sentence-transformers embeddings.

    Reuses the per-memory cache from `bettermemory.semantic` so a search
    that runs alongside dedup shares vectors.

    `matched_terms_fallback` is the stopword-stripped query token list. We
    do NOT blindly stamp it onto a semantic hit: that would report query
    words that appear nowhere in the memory as "matched" and drive the
    coverage-based relevance label to a fabricated "high" for a pure
    paraphrase hit, violating the MemoryHit contract (match_terms = the
    query tokens that actually hit the body or scopes; relevance = the
    fraction that matched). Instead we intersect the fallback with the
    memory's literal body/scope tokens — the exact overlap `score_memory`
    computes — and report that, possibly empty. A paraphrase-only hit then
    honestly carries `match_terms=[]` / low relevance while still surfacing
    by score.

    Threshold: hits with cosine < 0.3 are dropped. Below that, the
    similarity is noise — the model is matching style/structure rather
    than meaning. The threshold is conservative on purpose; we'd
    rather show fewer paraphrase hits than poison the result list
    with off-topic ones.
    """
    from .semantic import (
        _note_model_dimension,
        cached_embed,
        cosine_similarity_normalized,
        flush_persistent_cache,
    )

    query_clean = query.strip()
    if not query_clean:
        return []
    query_vec = semantic_model.encode(query_clean, normalize_embeddings=True)
    # The query encode is the first fresh embedding this run does —
    # feed its dimension to the cache reconcile so any stale-dimension
    # hydrated entries are purged before the `cached_embed` hits below.
    _note_model_dimension(len(query_vec))

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
        if applied_by_id:
            score *= _endorsement_factor(applied_by_id.get(memory.id, 0))
        # Report only the query tokens that LITERALLY hit this memory's
        # body or scopes (same overlap `score_memory` computes), not the
        # whole query — so a paraphrase-only hit carries honest match_terms
        # and an honest (low) relevance label rather than a fabricated one.
        body_token_set = set(_expand_kebab(tokenize(memory.body)))
        scope_token_set: set[str] = set()
        for scope in memory.scopes:
            scope_token_set.update(_scope_tokens(scope))
        literal_matched = [
            tok
            for tok in matched_terms_fallback
            if tok in body_token_set or tok in scope_token_set
        ]
        out.append((memory, score, literal_matched))
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
    mode: SearchMode = "hybrid",
    semantic_model: Any | None = None,
    rrf_k: int = _RRF_K_DEFAULT,
    applied_by_id: dict[str, int] | None = None,
    allow_empty_query: bool = False,
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
    - `mode`: ranker selection. `"hybrid"` (default since 2.6.8: RRF
      fusion of keyword + BM25, plus semantic when a model is
      provided); `"keyword"` (legacy TF + coverage + recency scorer
      with no IDF weighting); `"bm25"` (Okapi BM25 with the same
      scope-bonus + recency boost); `"semantic"` (sentence-
      transformers cosine — requires `semantic_model`). The hybrid
      mode gracefully degrades when no `semantic_model` is given: it
      fuses keyword + BM25 only, so flipping the default doesn't
      require any embedding extra.
    - `semantic_model`: optional sentence-transformers model. Required
      for `mode="semantic"`; optional for `mode="hybrid"` (semantic is
      added to the fusion when present).
    - `rrf_k`: smoothing constant for hybrid fusion. Larger spreads
      weight further down the list; smaller makes top ranks dominate.
      60 is the canonical default and almost always correct.
    - `applied_by_id`: optional map of memory id → explicit-applied count.
      When given, a bounded `_endorsement_factor` (≤ +10%, same ceiling as
      recency) nudges endorsed memories up — a near-tie breaker, never a
      relevance override. `None` (the default) leaves scores untouched, so
      every existing caller and the package default are byte-stable.
    - `allow_empty_query`: when True, an empty or stopword-only query
      no longer short-circuits to `[]`. Instead the function runs the
      `_filter_candidates` pass (scope / repo / worktree / excluded)
      and returns the survivors sorted by `updated` desc — a browse
      mode. Hits get `score=0.0`, no `match_terms`, and the default
      "low" relevance label. Used by callers that already narrowed
      the candidate pool externally (e.g. `since_prior_session=True`)
      and want recency ordering rather than relevance ranking.

    Score semantics vary by mode: keyword/BM25/semantic scores live on
    different scales and are not comparable across modes. Hybrid scores
    are RRF outputs (~0.01-0.05 range, summed `1/(k+rank)` over rankers).
    Use the `relevance` label, not the raw score, when comparing hits
    across modes.
    """
    # Runtime guard against unknown modes. The `SearchMode` Literal pins
    # this at the type-checker layer, but the handler accepts an opaque
    # string from MCP and Python doesn't enforce Literals at call time;
    # without this check, a typo like `mode="emantic"` would fall through
    # the if/elif chain into the `else` branch and silently run hybrid.
    # Raising here makes the failure mode loud at the dispatch boundary
    # regardless of where the bad string came from (handler, CLI, future
    # programmatic client).
    if mode not in ("keyword", "bm25", "semantic", "hybrid"):
        raise ValueError(
            f"unknown search mode {mode!r}; "
            "must be one of: keyword, bm25, semantic, hybrid"
        )
    if mode == "semantic" and semantic_model is None:
        raise ValueError("mode='semantic' requires semantic_model to be provided")

    now = now or datetime.now(timezone.utc)
    raw_tokens = tokenize(query)
    # Strip stopwords from the query — bodies stay unfiltered. If the query
    # was *only* stopwords ("what is the"), there's nothing meaningful left
    # to match on; return empty rather than serving every memory at score 0
    # (unless the caller explicitly asked for browse mode — see
    # `allow_empty_query` above).
    query_tokens = _strip_stopwords(raw_tokens)
    if not query_tokens:
        if not allow_empty_query:
            return []
        # Browse mode: apply the same candidate filter the scored
        # path would, then sort by `updated` desc and emit zero-score
        # hits with no match terms. Mirrors the post-rank trim the
        # scored branches do at the end of `search()`.
        browse_candidates = _filter_candidates(
            memories,
            scopes=scopes,
            excluded_scopes=excluded_scopes,
            repo_filter=repo_filter,
            worktree_filter=worktree_filter,
        )
        browse_candidates.sort(key=lambda m: (m.updated, m.id), reverse=True)
        return [
            _build_hit(memory, score=0.0, matched=[], query_unique=0)
            for memory in browse_candidates[:max_results]
        ]

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
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            applied_by_id=applied_by_id,
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
            candidates,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
            applied_by_id=applied_by_id,
        )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    elif mode == "semantic":
        # mypy: semantic_model is not None here (guarded above), but the
        # narrowing doesn't survive the assert-via-raise idiom across the
        # block boundary. Re-assert for the type checker.
        assert semantic_model is not None
        try:
            scored = _score_semantic(
                candidates,
                query,
                semantic_model,
                now=now,
                half_life_days=half_life_days,
                matched_terms_fallback=list(dict.fromkeys(query_tokens)),
                applied_by_id=applied_by_id,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to keyword on encode failure.
            # A LOADED model can still raise at encode() time (device fault,
            # OOM on a large body, a tokenizer edge case). Explicit semantic
            # mode must not crash the search on that — fall back to the
            # keyword ranking so the caller still gets results.
            log.warning(
                "semantic search failed at encode time (%s); "
                "falling back to keyword ranking",
                exc,
            )
            scored = _score_keyword(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
            )
        scored.sort(key=lambda x: (x[1], x[0].created, x[0].id), reverse=True)
    else:  # mode == "hybrid"
        rankings: list[list[tuple[Memory, float, list[str]]]] = [
            _score_keyword(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
            ),
            _score_bm25(
                candidates,
                query_tokens,
                now=now,
                half_life_days=half_life_days,
                applied_by_id=applied_by_id,
            ),
        ]
        if semantic_model is not None:
            try:
                rankings.append(
                    _score_semantic(
                        candidates,
                        query,
                        semantic_model,
                        now=now,
                        half_life_days=half_life_days,
                        matched_terms_fallback=list(dict.fromkeys(query_tokens)),
                        applied_by_id=applied_by_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — degrade to lexical fusion.
                # The "hybrid gracefully degrades" guarantee must cover a
                # runtime encode() failure of a loaded model, not just the
                # model-is-None case: fuse the keyword+bm25 rankings already
                # computed instead of crashing the search.
                log.warning(
                    "semantic ranking failed at encode time (%s); "
                    "fusing keyword+bm25 only",
                    exc,
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
        try:
            return _find_similar_semantic(
                new_body,
                existing,
                semantic_model,
                high_threshold=(high_threshold if high_threshold is not None else 0.85),
                medium_threshold=(
                    medium_threshold if medium_threshold is not None else 0.65
                ),
            )
        except Exception as exc:  # noqa: BLE001 — degrade to Jaccard dedup.
            # A loaded model raising at encode() time must not crash the
            # write-dedup gate (memory_write calls find_similar BEFORE it
            # commits). Degrade to lexical Jaccard dedup so the write still
            # completes — but with the Jaccard-NATURAL thresholds, NOT the
            # ones the caller passed. Thresholds supplied alongside a
            # semantic_model are COSINE-calibrated (the write-dedup gate
            # passes semantic_high/medium_threshold = 0.85/0.65); forwarding
            # those to the Jaccard scorer — whose natural high/medium are
            # HIGH_SIMILARITY/MEDIUM_SIMILARITY (0.75/0.40) — would silently
            # neuter the gate, since Jaccard rarely reaches 0.85, letting a
            # near-duplicate the gate should BLOCK commit as a parallel
            # duplicate. Dedup at the lexical scorer's own calibration.
            log.warning(
                "semantic dedup failed at encode time (%s); falling back to Jaccard",
                exc,
            )
            return _find_similar_jaccard(
                new_body,
                existing,
                high_threshold=HIGH_SIMILARITY,
                medium_threshold=MEDIUM_SIMILARITY,
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


# ---------------------------------------------------------------------------
# Generic dedup engine
# ---------------------------------------------------------------------------
#
# Pre-Round-2 the active and tombstone passes were four separate functions
# (`_find_similar_jaccard`, `_find_similar_semantic`,
# `_find_similar_tombstones_jaccard`, `_find_similar_tombstones_semantic`)
# whose loop bodies were near-clones — same threshold dispatch, same
# tokenisation, same hit-construction shape with only the relevance label
# and the optional `removed_at` / `removed_reason` fields differing
# between active and tombstone passes. The four-way duplication meant
# bug fixes had to land four times. Consolidated below: one Jaccard
# scorer and one semantic scorer, each parameterised by a `build_hit`
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
    near-duplicate functions."""
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


def _score_similar_semantic(
    new_body: str,
    existing: list[Any],
    model: Any,
    *,
    high_threshold: float,
    medium_threshold: float,
    high_label: str,
    medium_label: str,
    build_hit: Callable[[Any, float, str], SimilarHit | None],
    sort_key: Callable[[SimilarHit], Any],
    cache_key_for: Callable[[Any], tuple[str, str]],
) -> list[SimilarHit]:
    """Cosine-similarity dedup over `existing`, building hits via
    `build_hit`.

    `cache_key_for(memory)` returns the `(id, freshness_key)` tuple
    used to address the embedding cache — the active pass uses
    `(memory.id, memory.updated.isoformat())`; the tombstone pass uses
    `(f"tomb:{memory.id}", memory.removed.isoformat())`. Keeping the
    key derivation outside this function is what lets active and
    tombstone caches coexist for the same memory id without colliding.

    Imports `semantic` lazily so this module loads cleanly even when
    the embeddings extra isn't installed.
    """
    from .semantic import (
        _note_model_dimension,
        cached_embed,
        cosine_similarity_normalized,
        flush_persistent_cache,
    )

    new_body_clean = new_body.strip()
    if not new_body_clean:
        return []

    new_vec = model.encode(new_body_clean, normalize_embeddings=True)
    # First fresh embedding of the run — prime the cache reconcile so a
    # stale-dimension hydrated entry can't reach `cosine` below. See
    # `semantic._note_model_dimension`.
    _note_model_dimension(len(new_vec))

    hits: list[SimilarHit] = []
    for memory in existing:
        body_clean = memory.body.strip()
        if not body_clean:
            continue
        cache_id, cache_freshness = cache_key_for(memory)
        existing_vec = cached_embed(model, cache_id, cache_freshness, body_clean)
        similarity = cosine_similarity_normalized(new_vec, existing_vec)

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
    # End-of-batch hook: persist any newly-computed embeddings as a
    # single atomic write. No-op when persistence isn't configured or
    # nothing changed since the last flush.
    flush_persistent_cache()
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
    return _score_similar_semantic(
        new_body,
        existing,
        model,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high",
        medium_label="medium",
        build_hit=_build_active_hit,
        sort_key=_active_sort_key,
        cache_key_for=lambda m: (m.id, m.updated.isoformat()),
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
        try:
            return _find_similar_tombstones_semantic(
                new_body,
                tombstoned,
                semantic_model,
                high_threshold=(high_threshold if high_threshold is not None else 0.85),
                medium_threshold=(
                    medium_threshold if medium_threshold is not None else 0.65
                ),
            )
        except Exception as exc:  # noqa: BLE001 — degrade to Jaccard dedup.
            # Same fail-soft as find_similar, with the same threshold care:
            # the caller's cosine thresholds (0.85/0.65) must NOT be applied
            # to the Jaccard scorer (natural 0.75/0.40), or a near-duplicate
            # tombstone would stop surfacing the previously_removed warning.
            # Use the Jaccard-natural defaults.
            log.warning(
                "semantic tombstone dedup failed at encode time (%s); "
                "falling back to Jaccard",
                exc,
            )
            return _find_similar_tombstones_jaccard(
                new_body,
                tombstoned,
                high_threshold=HIGH_SIMILARITY,
                medium_threshold=MEDIUM_SIMILARITY,
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
    restore-then-tombstone cycle). The `tomb:` prefix on the cache id
    is what keeps the active and tombstone caches from colliding for
    the same memory across a restore-then-tombstone cycle.
    """
    return _score_similar_semantic(
        new_body,
        tombstoned,
        model,
        high_threshold=high_threshold,
        medium_threshold=medium_threshold,
        high_label="high-removed",
        medium_label="medium-removed",
        build_hit=_build_tombstone_hit,
        sort_key=_tombstone_sort_key,
        cache_key_for=lambda m: (f"tomb:{m.id}", m.removed.isoformat()),
    )
