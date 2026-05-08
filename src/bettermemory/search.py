"""Ranking memories against a query.

MVP scoring: keyword match + recency boost. Embeddings are an optional Phase 2
feature; the stub raises so callers know to install the extras.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, NoReturn

from .models import Memory, MemoryHit, SimilarHit, snippet_for
from .origin import repos_match


# Strip punctuation, keep word characters (incl. unicode letters) and dashes
# inside tokens. Lowercase before tokenizing.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-_]*", re.UNICODE)

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


def search(
    memories: list[Memory],
    query: str,
    *,
    scopes: list[str] | None = None,
    excluded_scopes: set[str] | None = None,
    repo_filter: str | None = None,
    max_results: int = 5,
    now: datetime | None = None,
    half_life_days: float = 30.0,
) -> list[MemoryHit]:
    """Rank `memories` against `query` and return up to `max_results` hits.

    - `scopes`: if given, only consider memories tagged with at least one.
    - `excluded_scopes`: any memory tagged with one of these is dropped.
      (Used for session-disabled scopes.)
    - `repo_filter`: a remote URL. When provided, memories whose
      `origin.repo` doesn't match (compared via `origin.repos_match`) are
      dropped. Memories with no `origin.repo` (legacy or non-repo writes)
      pass through — they're treated as global.
    """
    now = now or datetime.now(timezone.utc)
    raw_tokens = tokenize(query)
    # Strip stopwords from the query — bodies stay unfiltered. If the query
    # was *only* stopwords ("what is the"), there's nothing meaningful left
    # to match on; return empty rather than serving every memory at score 0.
    query_tokens = _strip_stopwords(raw_tokens)
    if not query_tokens:
        return []

    query_unique = len(set(query_tokens))

    scope_filter = set(scopes) if scopes else None
    excluded = excluded_scopes or set()

    hits: list[MemoryHit] = []
    for memory in memories:
        memory_scope_set = set(memory.scopes)
        if excluded and (memory_scope_set & excluded):
            continue
        if scope_filter is not None and not (memory_scope_set & scope_filter):
            continue

        # Auto-scope filter: drop memories from a different repo. Memories
        # without an origin.repo (legacy writes, non-repo writes) pass —
        # they're "global" and always relevant to the current caller.
        if repo_filter is not None:
            memory_repo = memory.origin.repo if memory.origin else None
            if not repos_match(memory_repo, repo_filter):
                continue

        score, matched = score_memory(
            memory,
            query_tokens,
            now=now,
            half_life_days=half_life_days,
        )
        if score <= 0:
            continue

        hits.append(
            MemoryHit(
                id=memory.id,
                scopes=memory.scopes,
                confidence=memory.confidence,
                snippet=snippet_for(memory.body),
                score=round(score, 4),
                relevance=_relevance_label(len(matched), query_unique),
                match_terms=matched,
                created=memory.created,
                updated=memory.updated,
            )
        )

    hits.sort(key=lambda h: (h.score, h.created), reverse=True)
    return hits[:max_results]


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
    from .semantic import cached_embed, cosine_similarity_normalized

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
    return hits


# ---------------------------------------------------------------------------
# Phase-2 stub
# ---------------------------------------------------------------------------


def embeddings_search(*_args: Any, **_kwargs: Any) -> NoReturn:  # noqa: D401
    """Reserved for embeddings-based search; install with extras."""
    raise NotImplementedError(
        "embeddings search not implemented in the MVP. "
        "Install with `pip install bettermemory[embeddings]` and a future "
        "release will enable this."
    )
