"""Tests for the fastembed provider path in semantic.py.

The torch (`[embeddings]`) and fastembed (`[embeddings-fast]`) providers
expose the same `.encode(text, normalize_embeddings=True) -> ndarray`
surface so the dedup ranker doesn't need to know which one it's holding.
These tests cover the parts of the provider abstraction that don't
require either extra to be installed: the `resolve_provider` policy,
the per-provider warning, the (provider, model_name) cache key shape,
the persistent-cache file namespacing, and the `_FastembedAdapter`
shim's `.encode` contract via a hand-rolled fake.

CI matrix:
- `test`: neither extra installed — all tests here run.
- `test-embeddings`: only `[embeddings]` installed — `no_torch_embeddings`
  tests are deselected.
- `test-embeddings-fast`: only `[embeddings-fast]` installed —
  `no_fastembed` tests are deselected.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory import semantic
from bettermemory.semantic import (
    DEFAULT_FASTEMBED_MODEL_NAME,
    _FastembedAdapter,
    cached_embed,
    configure_persistent_cache,
    flush_persistent_cache,
    get_model,
    reset_caches,
    resolve_provider,
)


@pytest.fixture(autouse=True)
def _reset_semantic_caches() -> Iterator[None]:
    """Each test gets a fresh module-level cache + provider state."""
    reset_caches()
    yield
    reset_caches()


# ---------------------------------------------------------------------------
# resolve_provider — policy: explicit honoured, auto prefers torch
# ---------------------------------------------------------------------------


def test_resolve_provider_explicit_torch_honoured() -> None:
    """An explicit `"torch"` always returns `"torch"` — even when the
    extra isn't installed. The per-provider WARNING in get_model() is
    what surfaces the missing-extra hint; resolve_provider only picks."""
    assert resolve_provider("torch") == "torch"


def test_resolve_provider_explicit_fastembed_honoured() -> None:
    """Symmetric — explicit fastembed is honoured even without the extra."""
    assert resolve_provider("fastembed") == "fastembed"


def test_resolve_provider_unknown_warns_then_auto(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo in `semantic_provider` config shouldn't fail the server —
    log a clear warning and fall back to auto-detection."""
    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    # `auto` resolution is environment-dependent; we just assert it
    # didn't crash and produced *a* warning naming the input.
    resolve_provider("opossum")
    assert any("opossum" in rec.message for rec in caplog.records)


@pytest.mark.no_torch_embeddings
@pytest.mark.no_fastembed
def test_resolve_provider_auto_no_extras_returns_none() -> None:
    """When neither extra is installed, auto resolves to None and the
    caller falls back to Jaccard / keyword."""
    assert resolve_provider("auto") is None
    assert resolve_provider(None) is None


@pytest.mark.no_torch_embeddings
@pytest.mark.no_fastembed
def test_resolve_provider_empty_string_treated_as_auto() -> None:
    """An empty string in the config knob is treated as `auto` — the
    config-file parser passes the raw value through and we don't want
    a stray blank line to fail-closed."""
    assert resolve_provider("") is None


# ---------------------------------------------------------------------------
# get_model — fastembed branch fail-soft
# ---------------------------------------------------------------------------


@pytest.mark.no_fastembed
def test_get_model_fastembed_returns_none_without_extra(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`get_model(provider="fastembed")` returns None on the import-error
    path with a clear, actionable warning."""
    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    model = get_model(DEFAULT_FASTEMBED_MODEL_NAME, provider="fastembed")
    assert model is None
    assert any(
        "embeddings-fast extra is not installed" in rec.message
        for rec in caplog.records
    )


@pytest.mark.no_fastembed
def test_get_model_fastembed_only_logs_load_failure_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated calls cache the failure; only one WARNING fires per
    (process, provider, model). Symmetric with the torch path."""
    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    get_model(DEFAULT_FASTEMBED_MODEL_NAME, provider="fastembed")
    get_model(DEFAULT_FASTEMBED_MODEL_NAME, provider="fastembed")
    get_model(DEFAULT_FASTEMBED_MODEL_NAME, provider="fastembed")
    warnings = [
        r
        for r in caplog.records
        if "embeddings-fast extra is not installed" in r.message
    ]
    assert len(warnings) == 1


@pytest.mark.no_torch_embeddings
@pytest.mark.no_fastembed
def test_get_model_provider_failures_logged_independently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed torch load doesn't suppress the fastembed warning (and
    vice versa) — they're keyed independently in `_LOAD_FAILED_LOGGED`.
    """
    caplog.set_level(logging.WARNING, logger="bettermemory.semantic")
    get_model("any-torch-model", provider="torch")
    get_model("any-fastembed-model", provider="fastembed")
    torch_msgs = [
        r
        for r in caplog.records
        if "embeddings extra is not installed" in r.message
        and "embeddings-fast" not in r.message
    ]
    fast_msgs = [
        r
        for r in caplog.records
        if "embeddings-fast extra is not installed" in r.message
    ]
    assert len(torch_msgs) == 1
    assert len(fast_msgs) == 1


# ---------------------------------------------------------------------------
# Persistent cache namespacing per provider
# ---------------------------------------------------------------------------


def test_configure_torch_uses_legacy_filename(tmp_path: Path) -> None:
    """The default provider ("torch") preserves the pre-2.5.0 layout —
    `.embeddings.<model>.npz` with no provider segment — so existing
    caches on disk keep loading without a migration step."""
    configure_persistent_cache(tmp_path, "model-a")
    assert semantic._PERSISTENT_PATH is not None
    assert semantic._PERSISTENT_PATH.name == ".embeddings.model-a.npz"


def test_configure_explicit_torch_matches_default(tmp_path: Path) -> None:
    """Explicit `provider="torch"` and the default kwarg resolve to the
    same path. A user toggling the config knob from auto to torch
    shouldn't see a fresh cache file appear."""
    configure_persistent_cache(tmp_path, "model-a", provider="torch")
    assert semantic._PERSISTENT_PATH is not None
    assert semantic._PERSISTENT_PATH.name == ".embeddings.model-a.npz"


def test_configure_fastembed_adds_provider_segment(tmp_path: Path) -> None:
    """The fastembed provider gets a dedicated filename segment so its
    vectors can't be loaded into a torch run (different embedding
    spaces even at the same nominal dimensionality)."""
    configure_persistent_cache(tmp_path, "BAAI/bge-small-en-v1.5", provider="fastembed")
    assert semantic._PERSISTENT_PATH is not None
    # `/` in the model name is sanitised to `_` so the cache file lives
    # at the configured root rather than in an implicit subdir.
    assert (
        semantic._PERSISTENT_PATH.name
        == ".embeddings.fastembed.BAAI_bge-small-en-v1.5.npz"
    )


def test_configure_swap_provider_clears_in_memory_cache(tmp_path: Path) -> None:
    """Switching providers drops the in-memory cache so a stale torch
    vector can't shadow a fastembed lookup for the same (id,
    updated_key)."""

    class _FakeModel:
        def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
            return [1.0, 0.0]

    configure_persistent_cache(tmp_path, "model", provider="torch")
    cached_embed(_FakeModel(), "01" + "0" * 24, "k", "body")
    assert semantic._EMBEDDING_CACHE  # populated

    configure_persistent_cache(tmp_path, "model", provider="fastembed")
    assert not semantic._EMBEDDING_CACHE  # cleared on path change


def test_configure_torch_and_fastembed_files_coexist(tmp_path: Path) -> None:
    """A user who flips between providers (e.g. for benchmarking) ends
    up with one file per provider — neither overwrites the other."""
    pytest.importorskip("numpy")

    class _FakeModel:
        def encode(self, text: str, normalize_embeddings: bool = True) -> list[float]:
            return [1.0, 0.0]

    configure_persistent_cache(tmp_path, "model", provider="torch")
    cached_embed(_FakeModel(), "01" + "0" * 24, "k", "body")
    flush_persistent_cache()

    configure_persistent_cache(tmp_path, "model", provider="fastembed")
    cached_embed(_FakeModel(), "01" + "0" * 24, "k", "body")
    flush_persistent_cache()

    files = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".npz")
    assert files == [
        ".embeddings.fastembed.model.npz",
        ".embeddings.model.npz",
    ]


# ---------------------------------------------------------------------------
# _FastembedAdapter — encode contract
# ---------------------------------------------------------------------------


class _FakeFastembedModel:
    """Stub for `fastembed.TextEmbedding` — exposes `.embed([texts])`
    that yields the per-text vectors the adapter will unbox."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.embed_calls = 0

    def embed(self, texts: list[str]) -> Iterator[list[float]]:
        self.embed_calls += 1
        for _ in texts:
            yield list(self._vector)


def test_fastembed_adapter_returns_first_vector() -> None:
    """The adapter wraps a single-text encode into the list/generator
    dance fastembed expects and unboxes the first result so callers see
    a plain vector — matching the sentence-transformers `.encode`
    shape."""
    model = _FakeFastembedModel([0.6, 0.8, 0.0])
    adapter = _FastembedAdapter(model)
    vec = adapter.encode("hello", normalize_embeddings=True)
    assert list(vec) == [0.6, 0.8, 0.0]
    assert model.embed_calls == 1


def test_fastembed_adapter_normalize_kwarg_accepted_as_noop() -> None:
    """fastembed's BGE / E5 families ship L2-normalised by default; the
    `normalize_embeddings` kwarg exists for API parity with
    sentence-transformers and the adapter accepts it without
    re-normalising."""
    model = _FakeFastembedModel([1.0, 0.0])
    adapter = _FastembedAdapter(model)
    # Either value should produce the same vector.
    a = adapter.encode("x", normalize_embeddings=True)
    b = adapter.encode("x", normalize_embeddings=False)
    assert list(a) == list(b) == [1.0, 0.0]
