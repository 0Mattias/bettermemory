"""Tests for the persistent embedding cache.

The cache is opt-in (`configure_persistent_cache(root, model)`) and
backed by `<root>/.embeddings.<safe_model>.npz`. Numpy is part of the
embeddings extra; tests that exercise the disk round-trip skip when it
isn't installed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


def test_persistent_cache_file_is_owner_only(tmp_path: Path) -> None:
    """`.npz` carries vector representations of memory bodies — same
    privacy bar as the source memories (0o600). The post-flush file
    must land owner-only, not at whatever the umask leaves behind."""
    import sys

    pytest.importorskip("numpy")
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits don't apply on Windows")

    configure_persistent_cache(tmp_path, "test-model")
    cached_embed(_FakeModel(), "01" + "A" * 24, "k", "body")
    flush_persistent_cache()
    target = tmp_path / ".embeddings.test-model.npz"
    assert target.exists()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600, (
        f"embedding cache mode is {oct(mode)}, expected 0o600 — "
        f"file is readable by group/world"
    )


def test_flush_chmods_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Privacy ordering pin: `os.fchmod` on the tmp fd must run BEFORE
    `Path.replace` moves the file into its visible path. The pre-fix
    shape called `os.chmod(target, 0o600)` AFTER the rename, opening a
    microsecond window where the `.npz` (which contains vector
    representations of memory bodies — same privacy bar as the source
    memories) was world-readable at the target path. This test pins
    the fchmod-before-rename discipline mirroring
    `store._atomic_write_post`.
    """
    import sys

    pytest.importorskip("numpy")
    if sys.platform == "win32":
        pytest.skip("fchmod is unavailable on Windows; ordering is moot")

    call_order: list[str] = []
    real_fchmod = os.fchmod
    real_replace = os.replace

    def spy_fchmod(fd: int, mode: int) -> None:
        call_order.append("fchmod")
        real_fchmod(fd, mode)

    def spy_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        call_order.append("replace")
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "fchmod", spy_fchmod)
    # Spy on `os.replace`, NOT `Path.replace`: the flush routes its
    # rename through `_fsutil.replace_atomic` (so it picks up the
    # Windows PermissionError retry), and that helper calls
    # `os.replace` directly. Spying on `Path.replace` silently stopped
    # firing when the call site moved, leaving both assertions below
    # unreachable — this test only runs where numpy is installed, so
    # the two embeddings CI legs were the only place that showed up.
    # `_fsutil.replace_atomic` is the single sanctioned rename site
    # (pinned by tests/test_fsutil.py::TestEveryRenameRoutesThrough-
    # ReplaceAtomic), so `os.replace` is the stable thing to watch.
    monkeypatch.setattr(os, "replace", spy_replace)

    configure_persistent_cache(tmp_path, "test-model")
    cached_embed(_FakeModel(), "01" + "B" * 24, "k", "body")
    flush_persistent_cache()

    # The fchmod call must precede the replace call. If `fchmod` is
    # missing entirely, the test catches a regression to the
    # pre-fix shape (chmod-after-rename only).
    assert "fchmod" in call_order, (
        "os.fchmod was never called during flush — chmod-on-the-fd-"
        "before-rename discipline is missing; the .npz is world-"
        "readable at the visible path for a microsecond window."
    )
    assert "replace" in call_order, "os.replace was never called"
    assert call_order.index("fchmod") < call_order.index("replace"), (
        f"fchmod must run before rename, got call order {call_order!r}"
    )


def test_flush_empty_but_dirty_cache_clears_flag(tmp_path: Path) -> None:
    """An empty-but-dirty cache must still resolve the dirty flag on
    flush.

    Regression: `_note_model_dimension` can purge every entry and set
    `_DIRTY=True`, leaving the cache empty-but-dirty. The old flush
    short-circuited on `if not _EMBEDDING_CACHE: return` *without*
    clearing `_DIRTY`, stranding the flag — a later genuine write that
    re-set `_DIRTY=True` then saw an already-set flag, and its data was
    never persisted. After this flush `_DIRTY` must be False, the
    now-stale on-disk file must be gone (so a restart can't re-hydrate
    the very entries the purge dropped), and a subsequent real write
    must persist.
    """
    pytest.importorskip("numpy")
    from bettermemory import semantic

    configure_persistent_cache(tmp_path, "test-model")
    path = semantic._PERSISTENT_PATH
    assert path is not None

    # Seed one entry (the fake model yields a fixed-length vector) and
    # persist it so there's a stale file on disk to clear later.
    cached_embed(_FakeModel(), "01" + "A" * 24, "k1", "hello world")
    flush_persistent_cache()
    assert path.exists()
    assert semantic._DIRTY is False

    # Purge every entry via a dimension that can't match the fake
    # model's output length. This drops the only entry and sets
    # _DIRTY=True, leaving the cache empty-but-dirty.
    semantic._note_model_dimension(999)
    assert semantic._EMBEDDING_CACHE == {}
    assert semantic._DIRTY is True

    # The flush must resolve the dirty state even with nothing to write.
    flush_persistent_cache()
    assert semantic._DIRTY is False, (
        "empty-but-dirty flush left _DIRTY set — a later real write "
        "would be skipped and lost"
    )
    # The stale file is removed so a restart can't re-hydrate the very
    # entries the purge dropped.
    assert not path.exists()

    # A later genuine write is now recognised as dirty and persists.
    cached_embed(_FakeModel(), "01" + "B" * 24, "k2", "fresh body")
    assert semantic._DIRTY is True
    flush_persistent_cache()
    assert semantic._DIRTY is False
    assert path.exists()
