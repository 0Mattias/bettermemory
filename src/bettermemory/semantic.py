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

Caching: an in-process dict keyed by `memory_id` — when a memory is
updated, its `updated` timestamp moves, the cache key (`updated_key`)
mismatches, and we recompute. Optionally, a persistent layer flushes the
cache to `<root>/.embeddings.<model>.npz` so a fresh MCP server doesn't
have to re-embed the whole store on first use. The persistent layer is
opt-in via `configure_persistent_cache(root, model_name)` and is a
transparent layer on top of the in-memory cache: hydration happens
lazily on first read, persistence happens at flush points (end of
`find_similar` calls). No-op when the embeddings extra isn't installed
— numpy is a transitive dep through sentence-transformers.

Cache invalidation hierarchy (most-frequent first):
- Body unchanged, in-memory hit: returns instantly.
- Body changed (updated_key mismatch): recompute, replace entry, mark
  dirty for the next persist.
- Server restart with persistent cache: hydrate from disk; entries
  whose memory_id no longer exists stay in the file but are inert
  (nothing to match against); a future migration may prune them.
- Model swap: persistent file is namespaced by model name, so flipping
  `semantic_model_name` in config produces a new file at first use
  rather than mixing vectors from different models.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
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
        from sentence_transformers import SentenceTransformer
    except ImportError:
        if model_name not in _LOAD_FAILED_LOGGED:
            log.warning(
                "semantic_dedup is enabled but the embeddings extra is "
                "not installed. Install with "
                "`pip install bettermemory[embeddings]` (or "
                '`uv pip install -e ".[embeddings]"`). Falling back to '
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

# Persistent cache layer — opt-in via `configure_persistent_cache`. None
# means "in-memory only" (the default and only behaviour pre-this-change).
_PERSISTENT_PATH: Path | None = None
# `_HYDRATED` is per-(_PERSISTENT_PATH) — flips back to False whenever the
# path changes so the next read pulls in the new file. Lazy-loaded on
# first cache access (not at configure time) to avoid touching the disk
# during MCP server startup before we know whether semantic dedup is
# actually used in this session.
_HYDRATED: bool = False
# `_DIRTY` flags whether the in-memory cache has changes not yet flushed
# to the persistent file. `flush_persistent_cache()` is a no-op when
# False — saves a useless file write on every find_similar call that
# happened to hit cache.
_DIRTY: bool = False


def configure_persistent_cache(root: Path | None, model_name: str) -> None:
    """Enable (or disable) on-disk persistence of the embedding cache.

    `root` is the memory store directory; the cache file lives next to
    `.events.jsonl` at `<root>/.embeddings.<safe_model>.npz`. Pass None
    for `root` to disable persistence — the in-memory cache continues
    to work but a server restart recomputes everything.

    `model_name` is namespaced into the filename so swapping models in
    config produces a fresh file rather than mixing incompatible
    vectors. Characters not safe for filenames are replaced with
    underscore (e.g. `sentence-transformers/all-MiniLM-L6-v2` becomes
    `sentence-transformers_all-MiniLM-L6-v2`).

    Calling this doesn't trigger a load; the next `cached_embed` call
    hydrates lazily so we don't pay the disk hit when semantic dedup
    is never used in a session.

    When the resolved path differs from the previously-configured one
    (including the disable -> enable transition), the in-memory cache
    is cleared so a stale entry from a different model can't hit on
    the next lookup. Without that, swapping models would silently
    return the old model's vector for the same `(memory_id,
    updated_key)` and leave `_DIRTY=False`, so the new model's
    persistent file would never be written.
    """
    global _PERSISTENT_PATH, _HYDRATED, _DIRTY
    new_path: Path | None = None
    if root is not None:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", model_name)
        new_path = Path(root) / f".embeddings.{safe}.npz"

    if new_path != _PERSISTENT_PATH:
        # Drop the in-memory cache; vectors keyed under the old model
        # name aren't valid lookup hits for the new one, and any
        # already-flushed entries can be re-hydrated from the new
        # model's file (or recomputed if no file exists yet).
        _EMBEDDING_CACHE.clear()

    _PERSISTENT_PATH = new_path
    _HYDRATED = False
    _DIRTY = False


def _hydrate_persistent_cache() -> None:
    """Load the on-disk cache into `_EMBEDDING_CACHE` on first use.

    Idempotent: subsequent calls after a successful hydration are no-ops.
    Failures are logged at WARNING and silently fall back to an empty
    in-memory cache — a corrupt or partial cache file should never
    block the dedup path.
    """
    global _HYDRATED
    if _HYDRATED or _PERSISTENT_PATH is None:
        return
    if not _PERSISTENT_PATH.exists():
        _HYDRATED = True
        return
    try:
        # numpy import is lazy — only the persistent path needs it.
        # The semantic extra brings sentence-transformers which depends
        # on numpy, so when this code runs numpy is available; we still
        # guard so a misconfiguration (persistent path set without the
        # extra) degrades gracefully.
        import numpy as np
    except ImportError:
        log.warning(
            "persistent embedding cache configured but numpy is not "
            "available — falling back to in-memory cache only."
        )
        _HYDRATED = True
        return

    try:
        with np.load(_PERSISTENT_PATH, allow_pickle=False) as data:
            ids = list(data["ids"])
            keys = list(data["keys"])
            vectors = data["vectors"]
        for i, (memory_id, key) in enumerate(zip(ids, keys)):
            _EMBEDDING_CACHE[str(memory_id)] = _CachedEmbedding(
                memory_id=str(memory_id),
                updated_key=str(key),
                vector=vectors[i],
            )
    except Exception as exc:  # noqa: BLE001 — corrupt cache must never crash dedup
        log.warning(
            "persistent embedding cache at %s is unreadable (%s); "
            "falling back to in-memory cache.",
            _PERSISTENT_PATH,
            exc,
        )
    _HYDRATED = True


def flush_persistent_cache() -> None:
    """Write the in-memory cache to disk if persistence is configured
    and the cache has unsaved changes. Atomic via `.tmp` + rename so a
    crash mid-write leaves the previous cache file intact.

    Callers should invoke this at natural batch boundaries — typically
    end of a `find_similar` call. A no-op when persistence is disabled
    or no entry has changed since the last flush.
    """
    global _DIRTY
    if _PERSISTENT_PATH is None or not _DIRTY:
        return
    try:
        import numpy as np
    except ImportError:
        # Same as hydrate — silently fall back. This branch is unusual
        # because semantic dedup is what populates the cache in the
        # first place; if numpy is genuinely missing, _EMBEDDING_CACHE
        # is empty too.
        _DIRTY = False
        return

    if not _EMBEDDING_CACHE:
        return
    ids = []
    keys = []
    vectors = []
    for memory_id, cached in _EMBEDDING_CACHE.items():
        ids.append(memory_id)
        keys.append(cached.updated_key)
        vectors.append(cached.vector)
    # Stack into a 2D array for compact storage; np.savez_compressed
    # handles the rest. Object arrays for ids/keys are fine —
    # they're short ULID-shaped strings.
    _PERSISTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PERSISTENT_PATH.with_suffix(_PERSISTENT_PATH.suffix + ".tmp")
    try:
        # Pass an open file handle rather than the path. np.savez_compressed
        # auto-appends `.npz` to a path string that doesn't already end in
        # it — which would turn our `.npz.tmp` into `.npz.tmp.npz` and
        # break the atomic rename below. Writing to a file object bypasses
        # that suffix-mangling. fsync the file before closing so the bytes
        # backing the rename are durable, mirroring `_atomic_write_post`'s
        # discipline in the main store.
        with open(tmp, "wb") as f:
            np.savez_compressed(
                f,
                ids=np.array(ids),
                keys=np.array(keys),
                vectors=np.stack(vectors),
            )
            f.flush()
            from ._fsutil import fsync_file

            fsync_file(f.fileno())
        tmp.replace(_PERSISTENT_PATH)
        _DIRTY = False
    except Exception as exc:  # noqa: BLE001 — never break the dedup path
        # Clean up the orphaned tmp so a half-written cache doesn't sit
        # in the persistence directory until the next successful flush
        # rolls over it. `missing_ok` covers the case where the failure
        # happened before the file was created (or after the rename).
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        log.warning(
            "failed to persist embedding cache to %s: %s",
            _PERSISTENT_PATH,
            exc,
        )


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
    `find_similar` calls within one process. When persistent caching
    is configured, hits also survive across process restarts; cache
    misses mark the cache dirty for the next `flush_persistent_cache`.
    """
    global _DIRTY
    _hydrate_persistent_cache()
    cached = _EMBEDDING_CACHE.get(memory_id)
    if cached is not None and cached.updated_key == updated_key:
        return cached.vector
    vector = model.encode(body, normalize_embeddings=True)
    _EMBEDDING_CACHE[memory_id] = _CachedEmbedding(
        memory_id=memory_id, updated_key=updated_key, vector=vector
    )
    _DIRTY = True
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
    """Clear all module-level caches. Tests use this to isolate cases.

    Resets the persistent-cache configuration too — any in-progress
    `_HYDRATED` / `_DIRTY` state is cleared, and a subsequent call
    needs to reconfigure persistence explicitly. Doesn't touch the
    on-disk file; that's a deliberate filesystem effect that survives.
    """
    global _PERSISTENT_PATH, _HYDRATED, _DIRTY
    _MODEL_CACHE.clear()
    _LOAD_FAILED.clear()
    _LOAD_FAILED_LOGGED.clear()
    _EMBEDDING_CACHE.clear()
    _PERSISTENT_PATH = None
    _HYDRATED = False
    _DIRTY = False


__all__ = [
    "DEFAULT_MODEL_NAME",
    "configure_persistent_cache",
    "flush_persistent_cache",
    "get_model",
    "cached_embed",
    "cosine_similarity_normalized",
    "reset_caches",
]
