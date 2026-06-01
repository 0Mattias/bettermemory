"""Tests for `bettermemory reindex --embeddings` (the cache-warming path).

The behavioural contract under test: warming the persistent embedding
cache must embed the SAME text the readers (search / dedup / consolidate)
embed — `memory.body.strip()` — under the same `(memory_id, freshness)`
cache key. The cache is keyed on `(id, updated)`, NOT on the body text,
so warming from the unstripped body would write a wrong-text vector under
the readers' own key; a later strip-then-lookup would then read back a
vector computed on different text.

The semantic plumbing (`cached_embed`, the model loader, the cache flush)
is patched at its fully-qualified module path so these tests never touch a
real embedding model or the on-disk cache — they assert only on which
text reaches `cached_embed`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from bettermemory.cli import reindex as reindex_cmd
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.store import Store


class _FakeModel:
    def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
        return [0.0]


def _dedup_on_config(directory: Path) -> Config:
    """A real Config with semantic_dedup flipped on, pinned to `directory`.
    Using the real model (not a stand-in) keeps the type contract with
    `_reindex_embeddings(config: Config, ...)` honest under mypy/pyright."""
    return Config(
        storage=StorageConfig(directory=str(directory)),
        behavior=BehaviorConfig(semantic_dedup=True),
    )


@contextlib.contextmanager
def _patched_semantics(cached_embed: Any) -> Iterator[None]:
    """Patch the embedding plumbing so `_reindex_embeddings` runs without a
    real model or cache file. `cached_embed` is the fake the test observes;
    the rest are inert stubs."""
    with (
        mock.patch("bettermemory.semantic.cached_embed", cached_embed),
        mock.patch("bettermemory.semantic._note_model_dimension", lambda dim: None),
        mock.patch("bettermemory.semantic.flush_persistent_cache", lambda: None),
        mock.patch(
            "bettermemory.semantic_setup._resolve_semantic_provider_and_model",
            lambda config: ("torch", "fake-model"),
        ),
        mock.patch(
            "bettermemory.semantic_setup._configure_persistent_embeddings",
            lambda config, store: None,
        ),
        mock.patch(
            "bettermemory.semantic_setup._semantic_model_or_none",
            lambda config: _FakeModel(),
        ),
    ):
        yield


def test_reindex_embeddings_warms_cache_with_stripped_body(tmp_path: Path) -> None:
    mem_dir = tmp_path / "mem"
    store = Store(mem_dir)
    # Body has leading/trailing whitespace so stripped != raw observably.
    mem = store.write(content="  padded body needing strip  ", scopes=["s"])

    # Record every (id, freshness, text) tuple the warm loop hands to
    # cached_embed so we can assert on the exact text it embeds.
    seen: list[tuple[str, str, str]] = []

    def _fake_cached_embed(
        model: Any, memory_id: str, freshness: str, text: str
    ) -> list[float]:
        seen.append((memory_id, freshness, text))
        return [0.0]

    with _patched_semantics(_fake_cached_embed):
        report = reindex_cmd._reindex_embeddings(_dedup_on_config(mem_dir), store)

    assert report["status"] == "ok"
    assert report["embedded"] == 1
    assert len(seen) == 1
    memory_id, freshness, text = seen[0]
    assert memory_id == mem.id
    assert freshness == mem.updated.isoformat()
    # The load-bearing assertion: the warmed text is the STRIPPED body,
    # exactly what search / dedup / consolidate embed under this same key
    # — not the raw, whitespace-padded `memory.body`.
    assert text == mem.body.strip()
    assert text == "padded body needing strip"
    assert text != mem.body


def test_reindex_embeddings_skips_empty_after_strip(tmp_path: Path) -> None:
    # Readers `continue` past bodies that are empty after strip and never
    # cache them; the warm loop must do the same so it doesn't seed an
    # entry the read path would never create. Build a real (fully-valid)
    # Memory via Store.write, then mutate its body to whitespace-only to
    # model the "non-empty raw, empty stripped" boundary, and feed it via
    # a patched iter_active.
    mem_dir = tmp_path / "mem"
    store = Store(mem_dir)
    blank = store.write(content="placeholder", scopes=["s"])
    object.__setattr__(blank, "body", "   \n  ")

    seen: list[str] = []

    def _fake_cached_embed(
        model: Any, memory_id: str, freshness: str, text: str
    ) -> list[float]:
        seen.append(text)
        return [0.0]

    with _patched_semantics(_fake_cached_embed):
        with mock.patch.object(store, "iter_active", lambda: iter([(mem_dir, blank)])):
            report = reindex_cmd._reindex_embeddings(_dedup_on_config(mem_dir), store)

    assert report["status"] == "ok"
    assert report["embedded"] == 0
    assert seen == []
