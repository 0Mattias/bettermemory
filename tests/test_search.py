"""Tests for search.py — keyword scoring and recency boost."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memory_mcp.models import Confidence, Memory, Source, generate_ulid
from memory_mcp.search import search, tokenize


def _memory(
    body: str,
    scopes: list[str] = ["tools"],
    *,
    created: datetime | None = None,
    confidence: Confidence = Confidence.MEDIUM,
) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes,
        confidence=confidence,
        source=Source.EXPLICIT,
        body=body,
    )


def test_tokenize_basic() -> None:
    assert tokenize("Hello, World! 123") == ["hello", "world", "123"]
    assert tokenize("python-frontmatter") == ["python-frontmatter"]


def test_exact_match_outranks_partial() -> None:
    a = _memory("python python python list comprehension")
    b = _memory("kubernetes networking notes")

    hits = search([a, b], "python list")
    assert hits[0].id == a.id


def test_scope_filter_excludes_non_matching() -> None:
    a = _memory("home lab routing", scopes=["infrastructure"])
    b = _memory("python tutorial style", scopes=["learning-style"])

    hits = search([a, b], "tutorial", scopes=["infrastructure"])
    # 'tutorial' is in b, but b doesn't have the 'infrastructure' scope.
    assert hits == []


def test_disabled_scope_excluded() -> None:
    a = _memory("python comprehension", scopes=["tools"])
    b = _memory("python comprehension", scopes=["projects:foo"])

    hits = search(
        [a, b],
        "python",
        excluded_scopes={"projects:foo"},
    )
    ids = [h.id for h in hits]
    assert a.id in ids
    assert b.id not in ids


def test_recency_boost_breaks_ties() -> None:
    now = datetime.now(timezone.utc)
    old = _memory("identical body words here", created=now - timedelta(days=180))
    new = _memory("identical body words here", created=now - timedelta(days=1))

    hits = search([old, new], "identical body", now=now)
    assert hits[0].id == new.id
    assert hits[0].score >= hits[1].score


def test_empty_query_returns_empty_list() -> None:
    a = _memory("anything")
    assert search([a], "") == []
    assert search([a], "   ") == []


def test_no_hits_is_empty_not_error() -> None:
    a = _memory("kubernetes networking")
    assert search([a], "totally unrelated") == []


def test_max_results_caps_output() -> None:
    memories = [_memory(f"python notes {i}") for i in range(10)]
    hits = search(memories, "python", max_results=3)
    assert len(hits) == 3


def test_scope_match_contributes_to_score() -> None:
    # 'projects' as a scope should match 'projects' as a query token.
    a = _memory("body without the keyword", scopes=["projects:alpha"])
    b = _memory("body without the keyword", scopes=["tools"])

    hits = search([a, b], "projects")
    assert any(h.id == a.id for h in hits)
    # b shouldn't surface — its scope tokens don't match.
    assert not any(h.id == b.id for h in hits)


def test_snippet_truncated_to_200_chars() -> None:
    long = "python " * 200
    a = _memory(long.strip())
    hits = search([a], "python")
    assert len(hits[0].snippet) <= 203  # 200 + "..."
