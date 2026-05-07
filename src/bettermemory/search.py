"""Ranking memories against a query.

MVP scoring: keyword match + recency boost. Embeddings are an optional Phase 2
feature; the stub raises so callers know to install the extras.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .models import Memory, MemoryHit, snippet_for


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
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do",
        "does", "for", "from", "had", "has", "have", "how", "i", "if", "in",
        "into", "is", "it", "its", "me", "my", "no", "not", "of", "on", "or",
        "so", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "to", "too", "us", "was", "we", "were",
        "what", "when", "where", "which", "who", "why", "will", "with",
        "would", "you", "your",
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
    max_results: int = 5,
    now: datetime | None = None,
    half_life_days: float = 30.0,
) -> list[MemoryHit]:
    """Rank `memories` against `query` and return up to `max_results` hits.

    - `scopes`: if given, only consider memories tagged with at least one.
    - `excluded_scopes`: any memory tagged with one of these is dropped.
      (Used for session-disabled scopes.)
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
# Phase-2 stub
# ---------------------------------------------------------------------------


def embeddings_search(*_args, **_kwargs):  # noqa: D401
    """Reserved for embeddings-based search; install with extras."""
    raise NotImplementedError(
        "embeddings search not implemented in the MVP. "
        "Install with `pip install bettermemory[embeddings]` and a future "
        "release will enable this."
    )
