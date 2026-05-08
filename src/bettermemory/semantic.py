"""Optional embedding-based similarity for `memory_write` dedup.

The default `find_similar` is Jaccard on stopword-stripped, kebab-expanded
token sets — fast, deterministic, no extra deps. It catches lexical
overlap well but misses paraphrases ("the database" vs "Postgres",
"shipped" vs "released"). When the user enables `[behavior]
semantic_dedup = true` in config and has installed the `embeddings` extra,
we add a sentence-transformers cosine pass that catches those.

Imports are lazy — the module loads cleanly even when the extra isn't
installed. A failed `get_model()` returns None and the caller falls back
to Jaccard with a single WARNING log line, so a user who flipped the
config bit without installing the extras is told plainly that they
didn't get what they asked for.

Caching: an in-process LRU keyed by `(memory_id, updated_iso)` — when a
memory is updated, its `updated` timestamp moves and the old cache entry
is naturally invalidated. The cache is in-memory only; a server restart
re-embeds every memory at first use. That's the simple-correct tradeoff
for v1; persistent embedding cache is a future optimization.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("bettermemory.semantic")


# Default model. all-MiniLM-L6-v2 is the standard small choice — ~80 MB,
# fast on CPU, decent quality for English short-text similarity. Override
# via config.behavior.semantic_model_name when there's a reason.
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# Model loader (lazy, cached, fail-soft)
# ---------------------------------------------------------------------------


_MODEL_CACHE: dict[str, Any] = {}
_LOAD_FAILED: set[str] = set()
_LOAD_FAILED_LOGGED: set[str] = set()


def get_model(model_name: str = DEFAULT_MODEL_NAME) -> Any | None:
    """Return a cached `SentenceTransformer` instance, or None if the
    extra isn't installed / the model can't be loaded.

    None is the "fall back to Jaccard" signal. We log the failure once
    per (process, model_name) at WARNING so the user sees a clear hint
    on the first call but doesn't get spammed.
    """
    if model_name in _MODEL_CACHE:
        return _MODEL_CACHE[model_name]
    if model_name in _LOAD_FAILED:
        return None

    try:
        # Lazy import: the module loads cleanly without the extra. The
        # ImportError below is the user-friendly path.
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError:
        if model_name not in _LOAD_FAILED_LOGGED:
            log.warning(
                "semantic_dedup is enabled but the embeddings extra is "
                "not installed. Install with "
                "`pip install bettermemory[embeddings]` (or "
                "`uv pip install -e \".[embeddings]\"`). Falling back to "
                "Jaccard similarity."
            )
            _LOAD_FAILED_LOGGED.add(model_name)
        _LOAD_FAILED.add(model_name)
        return None

    try:
        model = SentenceTransformer(model_name)
    except Exception as exc:  # noqa: BLE001 — model load can fail many ways.
        if model_name not in _LOAD_FAILED_LOGGED:
            log.warning(
                "failed to load embedding model %r: %s. Falling back to "
                "Jaccard similarity.",
                model_name,
                exc,
            )
            _LOAD_FAILED_LOGGED.add(model_name)
        _LOAD_FAILED.add(model_name)
        return None

    _MODEL_CACHE[model_name] = model
    return model


# ---------------------------------------------------------------------------
# Embedding cache — keyed by (memory_id, updated)
# ---------------------------------------------------------------------------


@dataclass
class _CachedEmbedding:
    memory_id: str
    updated_key: str  # opaque key — typically isoformat(updated). Bumps on update.
    vector: Any


_EMBEDDING_CACHE: dict[str, _CachedEmbedding] = {}


def cached_embed(
    model: Any,
    memory_id: str,
    updated_key: str,
    body: str,
) -> Any:
    """Return an embedding for `body`, caching by `(memory_id,
    updated_key)`.

    `updated_key` is whatever the caller treats as a freshness handle —
    typically `isoformat(memory.updated)`. When the memory is updated,
    `updated_key` changes and we recompute. The cache survives across
    `find_similar` calls within one process.
    """
    cached = _EMBEDDING_CACHE.get(memory_id)
    if cached is not None and cached.updated_key == updated_key:
        return cached.vector
    vector = model.encode(body, normalize_embeddings=True)
    _EMBEDDING_CACHE[memory_id] = _CachedEmbedding(
        memory_id=memory_id, updated_key=updated_key, vector=vector
    )
    return vector


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def cosine_similarity_normalized(a: Any, b: Any) -> float:
    """Cosine similarity assuming both inputs are L2-normalized.

    For normalized vectors `cos(a, b) = a · b`. Works on numpy arrays,
    plain lists, or any iterable of floats — we don't import numpy
    explicitly, so the module remains usable when only sentence-transformers
    (which brings numpy) is installed but not used.
    """
    return float(sum(x * y for x, y in zip(a, b)))


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def reset_caches() -> None:
    """Clear all module-level caches. Tests use this to isolate cases."""
    _MODEL_CACHE.clear()
    _LOAD_FAILED.clear()
    _LOAD_FAILED_LOGGED.clear()
    _EMBEDDING_CACHE.clear()


__all__ = [
    "DEFAULT_MODEL_NAME",
    "get_model",
    "cached_embed",
    "cosine_similarity_normalized",
    "reset_caches",
]
