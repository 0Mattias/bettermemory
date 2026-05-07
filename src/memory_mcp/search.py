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


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _scope_tokens(scope: str) -> list[str]:
    """Break `projects:foo-bar` into ['projects', 'foo', 'bar'] for matching."""
    return tokenize(scope)


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
) -> float:
    """Score a single memory against a tokenised query."""
    if not query_tokens:
        return 0.0

    body_tokens = tokenize(memory.body)
    body_count: dict[str, int] = {}
    for tok in body_tokens:
        body_count[tok] = body_count.get(tok, 0) + 1

    scope_tokens: list[str] = []
    for scope in memory.scopes:
        scope_tokens.extend(_scope_tokens(scope))
    scope_set = set(scope_tokens)

    raw = 0.0
    matched_unique = 0
    for tok in query_tokens:
        body_hits = body_count.get(tok, 0)
        scope_hit = 1 if tok in scope_set else 0
        contrib = body_hits + 2 * scope_hit  # scopes weighted 2x.
        if contrib > 0:
            matched_unique += 1
        raw += contrib

    if raw == 0.0:
        return 0.0

    # Mild boost for matching multiple distinct query terms — keeps "foo bar"
    # ranked above "foo foo foo" when the latter is just keyword spam.
    coverage = matched_unique / len(set(query_tokens))
    base = raw * (0.5 + 0.5 * coverage)

    return base * _recency_factor(memory.created, now, half_life_days)


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
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    scope_filter = set(scopes) if scopes else None
    excluded = excluded_scopes or set()

    hits: list[MemoryHit] = []
    for memory in memories:
        memory_scope_set = set(memory.scopes)
        if excluded and (memory_scope_set & excluded):
            continue
        if scope_filter is not None and not (memory_scope_set & scope_filter):
            continue

        score = score_memory(
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
                created=memory.created,
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
        "Install with `pip install memory-mcp[embeddings]` and a future "
        "release will enable this."
    )
