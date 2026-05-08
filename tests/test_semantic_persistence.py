"""Tests for the persistent embedding cache.

The cache is opt-in (`configure_persistent_cache(root, model)`) and
backed by `<root>/.embeddings.<safe_model>.npz`. Numpy is part of the
embeddings extra; tests that exercise the disk round-trip skip when it
isn't installed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory.semantic import (
    cached_embed,
    configure_persistent_cache,
    flush_persistent_cache,
    reset_caches,
)


@pytest.fixture(autouse=True)
def _reset_semantic_caches() -> Iterator[None]:
    reset_caches()
    yield
    reset_caches()


class _FakeModel:
    """SentenceTransformer-shaped stub. Returns deterministic small
    vectors so cache hits/misses are observable."""

    def __init__(self) -> None:
        self.encode_calls = 0

    def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
        self.encode_calls += 1
        # Deterministic shape so persistence round-trip is comparable.
        return [hash(text) % 7 / 7.0, 1.0, 0.0]


# ---------------------------------------------------------------------------
# Configuration semantics — no numpy needed
# ---------------------------------------------------------------------------


def test_configure_none_disables_persistence(tmp_path: Path) -> None:
    """`configure_persistent_cache(None, ...)` should leave persistence
    off — flush is a no-op and no file appears."""
    configure_persistent_cache(None, "any-model")
    model = _FakeModel()
    cached_embed(model, "01" + "0" * 24, "key", "body text")
    flush_persistent_cache()
    # Nothing written anywhere — there's no path to write to.
    assert not list(tmp_path.iterdir())


def test_configure_namespaces_model_in_filename(tmp_path: Path) -> None:
    """Different model names should produce different cache files so
    swapping models doesn't mix incompatible vectors."""
    pytest.importorskip("numpy")
    configure_persistent_cache(tmp_path, "model-a")
    cached_embed(_FakeModel(), "01" + "0" * 24, "k", "body")
    flush_persistent_cache()

    configure_persistent_cache(tmp_path, "model-b")
    cached_embed(_FakeModel(), "01" + "0" * 24, "k", "body")
    flush_persistent_cache()

    files = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".npz")
    assert files == [".embeddings.model-a.npz", ".embeddings.model-b.npz"]


def test_configure_sanitizes_unsafe_chars_in_model_name(tmp_path: Path) -> None:
    """Slashes (e.g. `org/model`) are normalised so the cache file
    lives at the configured root rather than implicitly creating
    subdirectories."""
    pytest.importorskip("numpy")
    configure_persistent_cache(tmp_path, "sentence-transformers/all-MiniLM-L6-v2")
    cached_embed(_FakeModel(), "01" + "0" * 24, "k", "body")
    flush_persistent_cache()
    files = [p.name for p in tmp_path.iterdir() if p.suffix == ".npz"]
    assert any("/" not in name for name in files)
    assert any("sentence-transformers_all-MiniLM-L6-v2" in name for name in files)


# ---------------------------------------------------------------------------
# Persistence round-trip — needs numpy
# ---------------------------------------------------------------------------


def test_persist_and_hydrate_round_trip(tmp_path: Path) -> None:
    """A fresh process with persistence configured should hydrate from
    the on-disk file and return cache hits without re-encoding."""
    pytest.importorskip("numpy")

    # Phase 1: populate + flush.
    configure_persistent_cache(tmp_path, "test-model")
    model_a = _FakeModel()
    memory_id = "01" + "B" * 24
    vec1 = cached_embed(model_a, memory_id, "key1", "alpha body")
    flush_persistent_cache()
    assert model_a.encode_calls == 1

    # Phase 2: simulate process restart.
    reset_caches()
    configure_persistent_cache(tmp_path, "test-model")
    model_b = _FakeModel()
    vec2 = cached_embed(model_b, memory_id, "key1", "alpha body")
    # Hit — second encode_calls didn't fire.
    assert model_b.encode_calls == 0
    # Vectors round-trip correctly.
    assert list(vec1) == list(vec2)


def test_stale_updated_key_recomputes(tmp_path: Path) -> None:
    """If the body changed (updated_key mismatch), the cache should
    miss and recompute even when the file has an old entry."""
    pytest.importorskip("numpy")
    configure_persistent_cache(tmp_path, "test-model")
    model = _FakeModel()
    memory_id = "01" + "C" * 24
    cached_embed(model, memory_id, "old-key", "v1 body")
    flush_persistent_cache()

    reset_caches()
    configure_persistent_cache(tmp_path, "test-model")
    model2 = _FakeModel()
    cached_embed(model2, memory_id, "new-key", "v2 body")
    # The updated_key changed, so we recompute.
    assert model2.encode_calls == 1


def test_flush_is_idempotent_when_no_changes(tmp_path: Path) -> None:
    """Calling flush twice in a row without intervening writes should
    not re-write the file (and should not crash)."""
    pytest.importorskip("numpy")
    configure_persistent_cache(tmp_path, "test-model")
    cached_embed(_FakeModel(), "01" + "D" * 24, "k", "body")
    flush_persistent_cache()
    cache_path = tmp_path / ".embeddings.test-model.npz"
    mtime_a = cache_path.stat().st_mtime_ns

    flush_persistent_cache()
    mtime_b = cache_path.stat().st_mtime_ns
    assert mtime_a == mtime_b


def test_corrupt_cache_file_falls_back_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A garbage-bytes cache file should log a WARNING and fall back
    to in-memory operation, not crash the dedup path."""
    pytest.importorskip("numpy")
    cache_path = tmp_path / ".embeddings.test-model.npz"
    cache_path.write_bytes(b"this is not a numpy archive")

    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    configure_persistent_cache(tmp_path, "test-model")
    model = _FakeModel()
    # Triggers _hydrate_persistent_cache; should log + fall back.
    vector = cached_embed(model, "01" + "E" * 24, "k", "body")
    assert list(vector) == [hash("body") % 7 / 7.0, 1.0, 0.0]
    assert any("unreadable" in rec.message for rec in caplog.records)


def test_atomic_write_uses_tmp_rename(tmp_path: Path) -> None:
    """The flush should write to `.tmp` and rename so a crash mid-write
    leaves the previous (possibly empty) file intact rather than a
    half-written corrupt file."""
    pytest.importorskip("numpy")
    configure_persistent_cache(tmp_path, "test-model")
    cached_embed(_FakeModel(), "01" + "F" * 24, "k", "body")
    flush_persistent_cache()

    # No `.tmp` left behind after a clean flush.
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []

    # The target file exists and is non-empty.
    target = tmp_path / ".embeddings.test-model.npz"
    assert target.exists()
    assert target.stat().st_size > 0
