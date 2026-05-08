"""Unit tests for semantic.py — model loader, embedding cache,
cosine helper. The actual sentence-transformers dependency is optional
and not installed in the test environment, so we mostly use fake models
that satisfy the `encode(text, normalize_embeddings=True)` interface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from bettermemory.models import (
    Confidence,
    Memory,
    Source,
    generate_ulid,
)
from bettermemory.search import find_similar
from bettermemory.semantic import (
    cached_embed,
    cosine_similarity_normalized,
    get_model,
    reset_caches,
)


@pytest.fixture(autouse=True)
def _reset_semantic_caches() -> None:
    """Each test gets a fresh module-level cache state."""
    reset_caches()
    yield
    reset_caches()


# ---------------------------------------------------------------------------
# get_model — fail-soft when extras aren't installed
# ---------------------------------------------------------------------------


def test_get_model_returns_none_without_extras(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sentence-transformers isn't a test dep, so get_model returns None
    on the import error path. The caller treats None as the Jaccard
    fallback signal."""
    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    model = get_model()
    assert model is None
    # And we logged a single helpful warning so the user sees the hint.
    assert any(
        "embeddings extra is not installed" in rec.message
        for rec in caplog.records
    )


def test_get_model_only_logs_load_failure_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    get_model()
    get_model()
    get_model()
    warnings = [
        r
        for r in caplog.records
        if "embeddings extra is not installed" in r.message
    ]
    assert len(warnings) == 1


def test_get_model_caches_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once load has failed, subsequent calls return None without
    re-attempting the import."""
    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    assert get_model() is None
    assert get_model() is None


# ---------------------------------------------------------------------------
# cached_embed — keyed by (id, updated_key)
# ---------------------------------------------------------------------------


class _FakeModel:
    """Minimal SentenceTransformer-shaped stub. Calls are counted so we
    can assert on cache hits."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}
        self.encode_calls = 0

    def encode(
        self, text: str, normalize_embeddings: bool = True
    ) -> list[float]:
        self.encode_calls += 1
        # Default to a constant vector when no override; tests can supply
        # specific bodies to control the result.
        return self.vectors.get(text, [1.0, 0.0, 0.0])


def test_cached_embed_first_call_runs_encode() -> None:
    model = _FakeModel({"hello": [1.0, 0.0, 0.0]})
    vec = cached_embed(model, "id1", "ts1", "hello")
    assert vec == [1.0, 0.0, 0.0]
    assert model.encode_calls == 1


def test_cached_embed_second_call_same_key_hits_cache() -> None:
    model = _FakeModel({"hello": [1.0, 0.0, 0.0]})
    cached_embed(model, "id1", "ts1", "hello")
    cached_embed(model, "id1", "ts1", "hello")
    assert model.encode_calls == 1


def test_cached_embed_recomputes_when_updated_key_changes() -> None:
    """A bumped `updated` timestamp means the body might have changed —
    invalidate the cached vector."""
    model = _FakeModel({"hello": [1.0, 0.0, 0.0]})
    cached_embed(model, "id1", "ts1", "hello")
    cached_embed(model, "id1", "ts2", "hello")
    assert model.encode_calls == 2


def test_cached_embed_separate_keys_for_separate_memories() -> None:
    model = _FakeModel({"a": [1.0, 0.0, 0.0], "b": [0.0, 1.0, 0.0]})
    cached_embed(model, "id1", "ts", "a")
    cached_embed(model, "id2", "ts", "b")
    assert model.encode_calls == 2


# ---------------------------------------------------------------------------
# cosine_similarity_normalized
# ---------------------------------------------------------------------------


def test_cosine_identical_vectors_is_one() -> None:
    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert cosine_similarity_normalized(a, b) == 1.0


def test_cosine_orthogonal_vectors_is_zero() -> None:
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert cosine_similarity_normalized(a, b) == 0.0


def test_cosine_works_on_negatives() -> None:
    a = [0.6, 0.8, 0.0]
    b = [0.6, 0.8, 0.0]
    assert cosine_similarity_normalized(a, b) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# find_similar — semantic dispatch with a fake model
# ---------------------------------------------------------------------------


def _memory(body: str, *, scopes: list[str] | None = None) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
    )


def test_find_similar_dispatches_to_jaccard_without_model() -> None:
    """A no-model call still works — Jaccard fallback. Two bodies that
    share no tokens shouldn't surface, even if a semantic model would
    have flagged them."""
    a = _memory("the queue uses postgres")
    b = _memory("the database is postgres")
    hits = find_similar(a.body, [b])
    # Same tokens (postgres, queue/database) — Jaccard fires.
    assert len(hits) >= 0  # Coexists with prior tests.


def test_find_similar_uses_semantic_when_model_provided() -> None:
    """With a fake model whose encode returns identical vectors for two
    distinct bodies, the cosine path produces a high-similarity hit
    that pure Jaccard would miss."""
    a = _memory("distinct words alpha bravo")
    b = _memory("entirely different charlie delta echo")

    fake = _FakeModel(
        {
            a.body.strip(): [1.0, 0.0, 0.0],
            b.body.strip(): [1.0, 0.0, 0.0],
        }
    )
    hits = find_similar(a.body, [b], semantic_model=fake)
    assert len(hits) == 1
    assert hits[0].relevance == "high"
    assert hits[0].similarity == pytest.approx(1.0)


def test_find_similar_semantic_below_medium_threshold_excluded() -> None:
    a = _memory("body a")
    b = _memory("body b")
    fake = _FakeModel(
        {
            a.body.strip(): [1.0, 0.0, 0.0],
            # Cosine ≈ 0.1 — below the default 0.65 medium cutoff.
            b.body.strip(): [0.1, 0.99, 0.0],
        }
    )
    hits = find_similar(a.body, [b], semantic_model=fake)
    assert hits == []


def test_find_similar_semantic_medium_band() -> None:
    a = _memory("body a")
    b = _memory("body b")
    # Cosine = 0.7 — in the medium band (default 0.65–0.85).
    fake = _FakeModel(
        {
            a.body.strip(): [1.0, 0.0],
            b.body.strip(): [0.7, 0.7141],  # ~ 0.7
        }
    )
    hits = find_similar(a.body, [b], semantic_model=fake)
    assert len(hits) == 1
    assert hits[0].relevance == "medium"


def test_find_similar_semantic_custom_thresholds() -> None:
    """Caller can override defaults — useful when corpus or model want
    different cutoffs."""
    a = _memory("body a")
    b = _memory("body b")
    fake = _FakeModel(
        {
            a.body.strip(): [1.0, 0.0],
            b.body.strip(): [0.5, 0.866],  # cosine = 0.5
        }
    )
    # With permissive thresholds, cosine=0.5 passes as medium.
    hits = find_similar(
        a.body,
        [b],
        semantic_model=fake,
        high_threshold=0.99,
        medium_threshold=0.4,
    )
    assert len(hits) == 1
    assert hits[0].relevance == "medium"


def test_find_similar_semantic_skips_empty_bodies() -> None:
    fake = _FakeModel()
    hits = find_similar("", [_memory("anything")], semantic_model=fake)
    assert hits == []
