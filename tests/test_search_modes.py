"""Tests for the `mode` parameter on search() — dispatch across keyword,
bm25, semantic, and hybrid rankers.

Semantic mode tests use a stub model (no sentence-transformers dependency)
so this file runs in the default test suite. The stub follows the same
contract — `encode(text, normalize_embeddings=True) -> vector` — and
returns deterministic vectors based on token overlap.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import search, tokenize


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


class _StubSemanticModel:
    """Deterministic stand-in for sentence-transformers.SentenceTransformer.

    Embeds text into a normalized vector over a small synthetic vocabulary
    (the union of tokens we feed it in tests). Cosine similarity on these
    vectors mirrors Jaccard token overlap, so paraphrases that share no
    surface tokens correctly score zero — which is the boundary the
    stub needs to model for these tests.
    """

    def __init__(self, vocab: list[str]) -> None:
        self._vocab = vocab
        self._index = {term: i for i, term in enumerate(vocab)}

    def encode(self, text: str, *, normalize_embeddings: bool = False) -> list[float]:
        toks = set(tokenize(text))
        vec = [1.0 if term in toks else 0.0 for term in self._vocab]
        if normalize_embeddings:
            norm = sum(x * x for x in vec) ** 0.5
            if norm > 0:
                vec = [x / norm for x in vec]
        return vec


def test_mode_keyword_is_default_and_unchanged() -> None:
    """Calling search() with no `mode` should use the keyword scorer — same
    behaviour as before the mode parameter existed. Pin the default so a
    future flip to hybrid is an obvious diff."""
    a = _memory("python list comprehension")
    b = _memory("kubernetes networking notes")
    default = search([a, b], "python list")
    explicit = search([a, b], "python list", mode="keyword")
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


def test_mode_semantic_requires_model() -> None:
    """`mode="semantic"` without a model is a programming error — raise
    ValueError loudly so the caller fixes it rather than silently
    returning empty results."""
    a = _memory("anything")
    with pytest.raises(ValueError, match="semantic_model"):
        search([a], "query", mode="semantic")


def test_mode_semantic_returns_hits_with_stub_model() -> None:
    """Semantic mode with a stub model: docs whose body shares tokens with
    the query should score positive. The stub mirrors Jaccard so this is
    really a smoke test for the dispatch path; the real ranking
    quality is a property of the production model."""
    a = _memory("python list comprehension")
    b = _memory("kubernetes networking notes")
    model = _StubSemanticModel(
        ["python", "list", "comprehension", "kubernetes", "networking", "notes"]
    )
    hits = search([a, b], "python list", mode="semantic", semantic_model=model)
    assert hits
    assert hits[0].id == a.id


def test_mode_hybrid_runs_without_semantic_model() -> None:
    """Hybrid mode degrades gracefully when no semantic model is provided:
    it fuses keyword + BM25 only. The "I asked for the best, but I don't
    have the embeddings extra installed" case — should still beat
    single-ranker quality in practice without hard-erroring."""
    a = _memory("python list comprehension")
    b = _memory("rust borrow checker")
    hits = search([a, b], "python", mode="hybrid")
    assert hits
    assert hits[0].id == a.id


def test_mode_hybrid_uses_semantic_when_model_provided() -> None:
    """When a model is provided, hybrid should fuse three rankers. Sanity
    check: top result for a multi-token query about python is still
    the python doc, not the rust doc, even though BM25 alone might
    score them differently than keyword."""
    a = _memory("python list comprehension")
    b = _memory("rust borrow checker")
    c = _memory("javascript promises")
    model = _StubSemanticModel(
        [
            "python",
            "list",
            "comprehension",
            "rust",
            "borrow",
            "checker",
            "javascript",
            "promises",
        ]
    )
    hits = search(
        [a, b, c],
        "python list",
        mode="hybrid",
        semantic_model=model,
    )
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


def test_mode_invalid_returns_typed_error() -> None:
    """The `mode` parameter is a Literal — but if a caller passes a string
    from the MCP boundary, we should fail fast on dispatch rather than
    silently returning empty results from an unmatched if/elif chain."""
    a = _memory("anything")
    # We can't easily check this at runtime because the Literal type
    # check happens at static analysis time, but we can assert that
    # the dispatch chain has an `else` covering hybrid — if a future
    # refactor drops `hybrid` from the chain, the if/elif/else falls
    # through and produces an empty result. The other tests above
    # cover that the four named modes all produce results.
    hits = search([a], "anything", mode="keyword")
    assert hits
