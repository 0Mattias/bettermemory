"""Tests for the `mode` parameter on search() — dispatch across the
keyword, bm25, and hybrid rankers.

A fourth arm, `"semantic"`, was dispatched here until 4.0.0 removed the
embedding lane; its tests drove a stub model rather than a real one so
the file could run without the extra installed. Both are gone — `mode`
is now a three-value closed set and `search()` raises on anything else.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import search


def _memory(
    body: str,
    scopes: list[str] | None = None,
    *,
    created: datetime | None = None,
    confidence: Confidence = Confidence.MEDIUM,
) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes or ["tools"],
        confidence=confidence,
        source=Source.EXPLICIT,
        body=body,
    )


def test_mode_default_is_hybrid() -> None:
    """Calling search() with no `mode` should use the hybrid scorer
    (default since 2.6.8). Pin the default so a future flip is an
    obvious diff."""
    a = _memory("python list comprehension")
    b = _memory("kubernetes networking notes")
    default = search([a, b], "python list")
    explicit = search([a, b], "python list", mode="hybrid")
    assert [h.id for h in default] == [h.id for h in explicit]


def test_mode_bm25_returns_hits() -> None:
    """End-to-end: BM25 mode produces ranked MemoryHits the same shape as
    keyword mode. Ordering checked separately in test_search_bm25.py;
    here we only confirm the dispatch path is wired."""
    a = _memory("python list comprehension")
    b = _memory("python decorators and closures")
    hits = search([a, b], "python", mode="bm25")
    assert len(hits) == 2
    assert {h.id for h in hits} == {a.id, b.id}
    assert all(h.score > 0 for h in hits)


def test_mode_hybrid_fuses_the_two_lexical_legs() -> None:
    """Hybrid is the fusion of keyword + BM25 and nothing else.

    It was written as a degradation case — "the embeddings extra is not
    installed, so fuse what is left" — but 4.0.0 made two legs the whole
    definition, so the condition can no longer vary. What is still worth
    pinning is that the fusion beats neither leg alone into silence."""
    a = _memory("python list comprehension")
    b = _memory("rust borrow checker")
    hits = search([a, b], "python", mode="hybrid")
    assert hits
    assert hits[0].id == a.id


def test_mode_hybrid_fused_score_in_rrf_range() -> None:
    """Hybrid mode populates `score` with the RRF fused value, which lives
    in `[0, n_rankers / (k+1)]` — much smaller than raw keyword/BM25
    scores. Consumer code that compares scores across modes is wrong;
    pin the range so a future change can't silently introduce a
    fused-score-times-100 hack."""
    a = _memory("python list comprehension")
    b = _memory("rust borrow checker")
    hits = search([a, b], "python", mode="hybrid")
    # 2 rankers in keyword+BM25 hybrid; max possible is 2 * 1/(60+1) ≈ 0.033.
    # Apply a generous ceiling for any future ranker the fuse can add.
    for h in hits:
        assert 0 < h.score < 0.1


def test_mode_hybrid_consensus_top_beats_single_ranker_top() -> None:
    """Two rankers might pick different #1s; hybrid's job is to surface
    the doc both rankers agree is in the top tier. Set up a corpus
    where the keyword scorer favours one doc but BM25 (with IDF
    weighting) favours another, and verify the consensus pick wins."""
    now = datetime.now(timezone.utc)
    # `python` is common; `rust` appears once. A query for `python` alone
    # should favour 'common_a' under keyword (matches 'python' multiple
    # times in the body) but favour 'distinguished' under BM25 (because
    # 'python' has lower IDF in a corpus where it's common, while
    # 'python distinguished' is a focused short doc).
    common_a = _memory("python python python python python notes")
    common_b = _memory("python python notes more notes here")
    distinguished = _memory("python distinguished")
    rare = _memory("rust niche edge case")
    hits = search(
        [common_a, common_b, distinguished, rare],
        "python",
        mode="hybrid",
        now=now,
    )
    # Sanity: at minimum the python docs should outrank the rust doc,
    # and the distinguished short doc should land in the top tier
    # rather than being buried by `common_a`'s keyword spam.
    top_ids = [h.id for h in hits[:3]]
    assert rare.id not in top_ids
    assert distinguished.id in top_ids


def test_mode_hybrid_body_match_beats_scope_namespace_noise() -> None:
    """Hybrid leg of the scope-namespace regression (see
    test_bm25_scope_namespace_token_does_not_outrank_body_match): before
    scope tokens entered compute_idf's df map, the default hybrid mode
    put an unrelated project-scoped memory at rank 1 for 'side projects'
    because the BM25 ranker overpriced the ubiquitous 'projects' scope
    bonus. Both fused lexical rankers must agree on the genuine hit."""
    now = datetime.now(timezone.utc)
    noise_rows = [
        ("Prefers oat milk lattes from the corner shop", "projects:homelab"),
        ("Restic snapshots run nightly at three", "projects:homelab"),
        ("Uses ruff and mypy in CI", "projects:bettermemory"),
        ("Kuma monitors ping every minute", "projects:homelab"),
        ("Diun watches container image tags", "projects:homelab"),
        ("Tailscale subnet router runs on the NAS", "projects:homelab"),
    ]
    noise = [
        _memory(body, scopes=[scope], created=now - timedelta(days=2))
        for body, scope in noise_rows
    ]
    genuine = _memory(
        "Tracks side projects in a Notion board with a weekly review",
        scopes=["personal-context"],
        created=now - timedelta(days=2),
    )
    hits = search(noise + [genuine], "side projects", mode="hybrid", now=now)
    assert hits and hits[0].id == genuine.id


def test_mode_hybrid_body_idf_not_crushed_by_pool_ubiquitous_scope_token() -> None:
    """Hybrid leg of the round-88 body-IDF regression (see
    test_bm25_body_match_not_crushed_by_pool_ubiquitous_scope_token):
    with every candidate scoped projects:bettermemory, the shared df map
    priced a BODY mention of the project name near zero, so the bm25 leg
    lost its decisive margin for 'bettermemory crash' and the hybrid RRF
    tie's created-desc tiebreaker handed rank 1 to a fresher memory that
    never mentions the project. With body-only df the bm25 leg agrees
    with the keyword leg and the genuine hit wins the default mode."""
    now = datetime.now(timezone.utc)
    fillers = [
        "Uses ruff and mypy in CI for linting",
        "Restic snapshots run nightly at three",
        "Kuma monitors ping every minute",
        "Diun watches container image tags",
        "Tailscale subnet router runs on the NAS",
        "Prefers oat milk lattes from the corner shop",
        "Vendored frontmatter handling lives in the store module",
        "Episode handoffs summarize long loops",
        "Scope overview returns curation counts",
        "Tombstones are restorable for thirty days",
    ]
    corpus = [
        _memory(b, scopes=["projects:bettermemory"], created=now - timedelta(days=2))
        for b in fillers
    ]
    focal = _memory(
        "bettermemory crash on startup traced to a stale index file",
        scopes=["projects:bettermemory"],
        created=now - timedelta(days=30),
    )
    decoy = _memory(
        "MCP server crash loop traced to systemd restart limits",
        scopes=["projects:bettermemory"],
        created=now - timedelta(days=1),
    )
    hits = search(corpus + [focal, decoy], "bettermemory crash", mode="hybrid", now=now)
    assert hits and hits[0].id == focal.id


def test_mode_invalid_returns_typed_error() -> None:
    """An unknown mode raises ValueError at the dispatch boundary —
    the runtime guard above the if/elif chain catches typos like
    `mode="emantic"` that the Literal annotation can't enforce at
    call time. Without the guard, the chain falls through to the
    `else` branch and silently runs hybrid, masking a caller bug.
    Pin both the exception type AND a substring of the message so a
    refactor that drops the validation (or returns a generic error)
    fails here rather than slipping through."""
    a = _memory("anything")
    with pytest.raises(ValueError, match="unknown search mode"):
        search([a], "anything", mode="emantic")  # type: ignore[arg-type]
    # Also verify a syntactically-distinct invalid value — e.g.
    # an empty string — hits the same guard, so the validator is
    # genuinely a closed-set check, not a typo-specific reject.
    with pytest.raises(ValueError, match="unknown search mode"):
        search([a], "anything", mode="")  # type: ignore[arg-type]
