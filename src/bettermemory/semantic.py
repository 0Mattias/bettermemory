"""Optional embedding-based similarity for `memory_write` dedup.

The default `find_similar` is Jaccard on stopword-stripped, kebab-expanded
token sets — fast, deterministic, no extra deps. It catches lexical
overlap well but misses paraphrases ("the database" vs "Postgres",
"shipped" vs "released"). When the user enables `[behavior]
semantic_dedup = true` in config and has installed one of the embedding
extras, we add a cosine-similarity pass that catches those.

Two providers ship; both expose the same `.encode(body,
normalize_embeddings=True) -> numpy.ndarray` shape so the dedup path is
provider-agnostic:

- **torch** (`[embeddings]` extra): sentence-transformers + PyTorch.
  Default model `all-MiniLM-L6-v2` (~80 MB). Heavier disk + memory
  footprint, but it's the well-trodden path with the broadest model
  catalogue.
- **fastembed** (`[embeddings-fast]` extra): fastembed + ONNX Runtime.
  Default model `BAAI/bge-small-en-v1.5` (~33 MB ONNX, ~50 MB runtime
  total). Same retrieval surface; smaller install for users who can't
  afford ~500 MB of torch.

When both extras are installed `torch` wins by default — existing
`.embeddings.<model>.npz` caches stay byte-stable. Override the
auto-detection precedence via `[behavior] semantic_provider = "fastembed"`.

Imports are lazy — the module loads cleanly with neither extra installed.
A failed `get_model()` returns None and the caller falls back to Jaccard
with a single WARNING log line, so a user who flipped the config bit
without installing the deps is told plainly.

Caching: an in-process dict keyed by `memory_id` — when a memory is
updated, its `updated` timestamp moves, the cache key (`updated_key`)
mismatches, and we recompute. Optionally, a persistent layer flushes the
cache to disk so a fresh MCP server doesn't have to re-embed the whole
store on first use. The persistent layer is opt-in via
`configure_persistent_cache(root, model_name, provider=...)`:

- torch: `<root>/.embeddings.<safe_model>.npz` (legacy layout — keeps
  existing caches working without migration).
- fastembed: `<root>/.embeddings.fastembed.<safe_model>.npz`. Provider
  namespacing prevents a vector produced by one provider from leaking
  into the other's run — fastembed and torch vectors live in different
  embedding spaces even at the same nominal dimensionality.

Cache invalidation hierarchy (most-frequent first):
- Body unchanged, in-memory hit: returns instantly.
- Body changed (updated_key mismatch): recompute, replace entry, mark
  dirty for the next persist.
- Server restart with persistent cache: hydrate from disk; entries
  whose memory_id no longer exists stay in the file but are inert
  (nothing to match against); a future migration may prune them.
- Provider/model swap: file is namespaced by both, so flipping
  `semantic_provider` or `semantic_model_name` produces a fresh file
  at first use rather than mixing incompatible vectors.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("bettermemory.semantic")


# Provider identifier. `torch` = sentence-transformers ([embeddings]
# extra). `fastembed` = fastembed ([embeddings-fast] extra). Used by
# the persistent-cache path-namespacing and the auto-detection rule.
Provider = Literal["torch", "fastembed"]

# Default model per provider. Override via the matching config knob
# (`semantic_model_name` for torch, `semantic_model_fastembed` for
# fastembed). Different providers use different model catalogues — same
# nominal task, different identifiers.
#
# `DEFAULT_MODEL_NAME` is the torch default, kept under its historic
# name for backwards-compatible callers that still pass it explicitly
# (and the tests that reference the constant).
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_FASTEMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"


# Persistent-cache filename shape: `<root>/.embeddings.[<provider>.]<safe>.npz`
# (the provider segment is omitted for the legacy torch layout — see
# `configure_persistent_cache` below). Lifted to module-level constants so
# `sync.py`'s gitignore writer can build the matching glob by importing both
# halves rather than hardcoding a sibling `.embeddings.*.npz` literal that
# would drift silently if the on-disk shape ever moved.
EMBEDDING_FILENAME_PREFIX = ".embeddings."
EMBEDDING_FILENAME_SUFFIX = ".npz"


# ---------------------------------------------------------------------------
# Model loader (lazy, cached, fail-soft)
# ---------------------------------------------------------------------------


# Cache keys are `(provider, model_name)` tuples so a fastembed model
# and a torch model that happen to share a name (rare in practice but
# possible) don't collide. The legacy `_MODEL_CACHE: dict[str, Any]`
# would have failed silently in that case — the new key shape makes
# the provider distinction first-class.
_MODEL_CACHE: dict[tuple[Provider, str], Any] = {}
_LOAD_FAILED: set[tuple[Provider, str]] = set()
_LOAD_FAILED_LOGGED: set[tuple[Provider, str]] = set()


class _FastembedAdapter:
    """Wraps a `fastembed.TextEmbedding` to expose the same `.encode`
    surface that `sentence_transformers.SentenceTransformer` ships.

    The dedup path in `cached_embed` calls
    `model.encode(text, normalize_embeddings=True)` and expects a 1-D
    numpy array. fastembed returns a generator of vectors from
    `model.embed([texts])` — already L2-normalised by default for the
    BGE family — so the adapter wraps a single-text encode into the
    list/generator dance and unboxes the first result.
    """

    def __init__(self, model: Any) -> None:
        self._model = model

    def encode(self, text: str, normalize_embeddings: bool = True) -> Any:
        # fastembed returns a generator that yields numpy arrays. The
        # BGE / E5 / nomic-embed families ship normalised by default;
        # the kwarg is accepted for API parity with sentence-transformers
        # but does not need to re-normalise here.
        del normalize_embeddings  # API-parity placeholder; see above.
        vectors = list(self._model.embed([text]))
        return vectors[0]


def _load_torch_model(model_name: str) -> Any | None:
    """Best-effort load of a sentence-transformers model.

    Returns the model on success, None when the extra is missing or the
    model can't be loaded. Logs once per (process, "torch", model_name)
    at WARNING.
    """
    key: tuple[Provider, str] = ("torch", model_name)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    if key in _LOAD_FAILED:
        return None

    try:
        # Lazy import: the module loads cleanly without the extra. The
        # ImportError below is the user-friendly path.
        from sentence_transformers import (  # pyright: ignore[reportMissingImports]
            SentenceTransformer,
        )
    except ImportError:
        if key not in _LOAD_FAILED_LOGGED:
            log.warning(
                "semantic provider 'torch' requested but the "
                "embeddings extra is not installed. Install with "
                "`pip install bettermemory[embeddings]` (or "
                '`uv pip install -e ".[embeddings]"`). Falling back to '
                "Jaccard / keyword."
            )
            _LOAD_FAILED_LOGGED.add(key)
        _LOAD_FAILED.add(key)
        return None

    try:
        model = SentenceTransformer(model_name)
    except Exception as exc:  # noqa: BLE001 — model load can fail many ways.
        if key not in _LOAD_FAILED_LOGGED:
            log.warning(
                "failed to load sentence-transformers model %r: %s. "
                "Falling back to Jaccard / keyword.",
                model_name,
                exc,
            )
            _LOAD_FAILED_LOGGED.add(key)
        _LOAD_FAILED.add(key)
        return None

    _MODEL_CACHE[key] = model
    return model


def _load_fastembed_model(model_name: str) -> Any | None:
    """Best-effort load of a fastembed model wrapped in
    `_FastembedAdapter` so callers see the same `.encode` surface as the
    torch path.

    Returns the adapter on success, None when the extra is missing or
    the model can't be loaded. Logs once per (process, "fastembed",
    model_name).

    Network: fastembed downloads ONNX weights to its own cache on first
    use; air-gapped installs need to pre-stage the cache directory (see
    fastembed docs for `FASTEMBED_CACHE_DIR`). The runtime path doesn't
    gate on a network flag — that's left to the user's environment.
    """
    key: tuple[Provider, str] = ("fastembed", model_name)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    if key in _LOAD_FAILED:
        return None

    try:
        from fastembed import TextEmbedding  # pyright: ignore[reportMissingImports]
    except ImportError:
        if key not in _LOAD_FAILED_LOGGED:
            log.warning(
                "semantic provider 'fastembed' requested but the "
                "embeddings-fast extra is not installed. Install with "
                "`pip install bettermemory[embeddings-fast]` (or "
                '`uv pip install -e ".[embeddings-fast]"`). Falling '
                "back to Jaccard / keyword."
            )
            _LOAD_FAILED_LOGGED.add(key)
        _LOAD_FAILED.add(key)
        return None

    try:
        model: Any = TextEmbedding(model_name=model_name)
    except Exception as exc:  # noqa: BLE001 — model load can fail many ways.
        if key not in _LOAD_FAILED_LOGGED:
            log.warning(
                "failed to load fastembed model %r: %s. Falling back "
                "to Jaccard / keyword.",
                model_name,
                exc,
            )
            _LOAD_FAILED_LOGGED.add(key)
        _LOAD_FAILED.add(key)
        return None

    adapter = _FastembedAdapter(model)
    _MODEL_CACHE[key] = adapter
    return adapter


def _torch_extra_installed() -> bool:
    """Return True iff the sentence-transformers import resolves.

    Uses `importlib.util.find_spec` so we never actually import the
    module — checking the extra's presence shouldn't pay the import
    cost. Same idiom for `_fastembed_extra_installed`.
    """
    import importlib.util

    return importlib.util.find_spec("sentence_transformers") is not None


def _fastembed_extra_installed() -> bool:
    """Return True iff the fastembed import resolves. See
    `_torch_extra_installed` for the spec-check rationale."""
    import importlib.util

    return importlib.util.find_spec("fastembed") is not None


def resolve_provider(preference: str | None = None) -> Provider | None:
    """Pick a provider given a config preference.

    `preference` is the raw `[behavior] semantic_provider` value
    (typically "auto" / "torch" / "fastembed" / None). The resolution
    rule:

    - Explicit "torch" or "fastembed": honour it, even if the extra
      isn't installed. The caller then sees None from `get_model()`
      and the per-provider warning explains the missing extra.
    - "auto" or None: torch wins when installed (existing caches stay
      byte-stable), then fastembed, then None (no extra installed).

    Returns the chosen Provider, or None when no provider is available.
    """
    pref = (preference or "auto").strip().lower()
    if pref == "torch":
        return "torch"
    if pref == "fastembed":
        return "fastembed"
    if pref not in {"auto", ""}:
        log.warning(
            "unknown semantic_provider %r; falling back to auto-detect.",
            preference,
        )
    if _torch_extra_installed():
        return "torch"
    if _fastembed_extra_installed():
        return "fastembed"
    return None


def get_model(
    model_name: str = DEFAULT_MODEL_NAME,
    *,
    provider: Provider | None = None,
) -> Any | None:
    """Return a cached embedding model (or None for Jaccard fallback).

    By default this is the legacy torch path — passing only
    `model_name` keeps every existing call site (and every existing
    test) byte-stable. Pass `provider="fastembed"` to opt into the
    ONNX path explicitly, or `provider="torch"` to be explicit on the
    legacy path.

    The returned object exposes
    `.encode(text, normalize_embeddings=True) -> numpy.ndarray` for both
    providers. None is the "fall back to Jaccard" signal.
    """
    chosen: Provider = provider or "torch"
    if chosen == "torch":
        return _load_torch_model(model_name)
    # The Provider Literal narrows to "fastembed" in the only remaining
    # branch — no defensive else needed; mypy strict catches invalid
    # providers at type-check time.
    return _load_fastembed_model(model_name)


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

# `_MODEL_DIM` records the live embedding model's output dimension,
# learned from the first fresh encode this process does — None until
# then. Reset whenever the cache is cleared or the persistent path
# changes. See `_note_model_dimension`.
_MODEL_DIM: int | None = None


def configure_persistent_cache(
    root: Path | None,
    model_name: str,
    *,
    provider: Provider = "torch",
) -> None:
    """Enable (or disable) on-disk persistence of the embedding cache.

    `root` is the memory store directory; the cache file lives next to
    `.events.jsonl`. Pass None for `root` to disable persistence — the
    in-memory cache continues to work but a server restart recomputes
    everything.

    File layout (per provider):
    - torch: `<root>/.embeddings.<safe_model>.npz` (legacy — keeps
      pre-2.5.0 caches loadable without migration).
    - fastembed: `<root>/.embeddings.fastembed.<safe_model>.npz`. The
      provider segment prevents fastembed vectors and torch vectors
      (different embedding spaces) from being read into the same
      run.

    `model_name` is namespaced into the filename so swapping models
    produces a fresh file rather than mixing incompatible vectors.
    Characters not safe for filenames are replaced with underscore
    (e.g. `sentence-transformers/all-MiniLM-L6-v2` becomes
    `sentence-transformers_all-MiniLM-L6-v2`).

    Calling this doesn't trigger a load; the next `cached_embed` call
    hydrates lazily so we don't pay the disk hit when semantic dedup
    is never used in a session.

    When the resolved path differs from the previously-configured one
    (including the disable -> enable transition, OR a provider swap),
    the in-memory cache is cleared so a stale entry from a different
    provider/model can't hit on the next lookup. Without that,
    swapping providers would silently return the old provider's
    vector for the same `(memory_id, updated_key)` and leave
    `_DIRTY=False`, so the new file would never be written.
    """
    global _PERSISTENT_PATH, _HYDRATED, _DIRTY, _MODEL_DIM
    new_path: Path | None = None
    if root is not None:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", model_name)
        if provider == "torch":
            # Legacy layout — preserved verbatim so pre-2.5.0
            # `.embeddings.<model>.npz` files keep loading without a
            # migration step.
            new_path = (
                Path(root)
                / f"{EMBEDDING_FILENAME_PREFIX}{safe}{EMBEDDING_FILENAME_SUFFIX}"
            )
        else:
            new_path = (
                Path(root)
                / f"{EMBEDDING_FILENAME_PREFIX}{provider}.{safe}{EMBEDDING_FILENAME_SUFFIX}"
            )

    if new_path != _PERSISTENT_PATH:
        # Drop the in-memory cache; vectors keyed under the old model
        # name aren't valid lookup hits for the new one, and any
        # already-flushed entries can be re-hydrated from the new
        # file (or recomputed if no file exists yet).
        _EMBEDDING_CACHE.clear()

    _PERSISTENT_PATH = new_path
    _HYDRATED = False
    _DIRTY = False
    # New path -> next access re-hydrates; the dimension check must
    # re-run against whatever the new file holds.
    _MODEL_DIM = None


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
        import numpy as np  # pyright: ignore[reportMissingImports]
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
        import numpy as np  # pyright: ignore[reportMissingImports]
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
    # Process-unique tmp name so two concurrent flushes (e.g. two MCP
    # servers in the same memory dir) don't collide on the same
    # `.tmp` path. Worst case is still last-writer-wins on the
    # rename — that's OK because the cache is fully recomputable —
    # but with a shared `.tmp` they'd corrupt each other's writes.
    tmp = _PERSISTENT_PATH.with_suffix(f"{_PERSISTENT_PATH.suffix}.tmp.{os.getpid()}")
    from ._fsutil import flock_excl, fsync_dir, fsync_file

    try:
        # Serialize the rename against concurrent flushes. flock_excl
        # is per-inode exclusive; concurrent writers all serialise
        # through the same lockfile. The lock surface is small
        # (rename + chmod + fsync_dir); contention is negligible.
        with flock_excl(_PERSISTENT_PATH):
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
                # fchmod BEFORE the rename so the file is 0o600 the
                # moment it appears at `_PERSISTENT_PATH`. `.npz`
                # contains vector representations of memory bodies —
                # same privacy bar as the source memories, which use
                # 0o600. The pre-fix shape called `os.chmod(path,
                # 0o600)` AFTER the rename, opening a window where the
                # file was world-readable at the visible path (umask
                # is typically 0o644 on Linux/macOS, sometimes 0o664
                # on shared-user boxes). fchmod-before-rename closes
                # that window — see `store._atomic_write_post` for the
                # canonical write-up of this discipline. Suppressed —
                # Windows has no mode bits and some sandbox
                # filesystems reject fchmod; that's a permission-bit
                # loss, not a corruption risk. `sys.platform`
                # narrowing keeps mypy happy on Windows where
                # `os.fchmod` is absent from typeshed.
                if sys.platform != "win32":
                    with contextlib.suppress(OSError):
                        os.fchmod(f.fileno(), 0o600)
                f.flush()
                fsync_file(f.fileno())
            tmp.replace(_PERSISTENT_PATH)
            # Defensive post-rename chmod (belt-and-suspenders): if
            # the filesystem squashed the mode on rename (rare — most
            # POSIX filesystems preserve it) we can still recover.
            # This is a no-op when the fchmod above succeeded.
            with contextlib.suppress(OSError):
                os.chmod(_PERSISTENT_PATH, 0o600)
            # fsync the parent directory so the rename survives crash —
            # mirror `_atomic_write_post`.
            fsync_dir(_PERSISTENT_PATH.parent)
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


def _note_model_dimension(dim: int) -> None:
    """Record the live model's embedding dimension and, the first time
    it's seen, drop any cache entries that don't match it.

    A persistent cache written under one model checkpoint and hydrated
    under another (same `model_name`, different output dimension)
    would otherwise pair a stale-dimension cached vector against a
    freshly-computed one in `cosine_similarity_normalized`, whose
    `zip(strict=True)` raises `ValueError` — uncaught on the
    `memory_write` -> `find_similar` path, so the whole handler fails.

    Every caller that computes a *fresh* embedding feeds the dimension
    here: `cached_embed`'s own cache-miss branch, and the query encode
    in `find_similar` / `_search` / `find_similar_tombstones` (which
    runs before any `cached_embed`, so stale entries are purged before
    a cache hit can hand one back). The one-time purge forces every
    stale entry to recompute at the current dimension. No probe encode
    — the dimension is taken from work the caller already did.
    """
    global _MODEL_DIM, _DIRTY
    if _MODEL_DIM == dim:
        return
    _MODEL_DIM = dim
    stale = [mid for mid, c in _EMBEDDING_CACHE.items() if len(c.vector) != dim]
    if stale:
        for mid in stale:
            del _EMBEDDING_CACHE[mid]
        _DIRTY = True
        log.warning(
            "embedding cache: dropped %d stale-dimension entries — the "
            "persistent cache was written under a different model "
            "checkpoint; they will be recomputed at the current "
            "dimension.",
            len(stale),
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
    if (
        cached is not None
        and cached.updated_key == updated_key
        and (_MODEL_DIM is None or len(cached.vector) == _MODEL_DIM)
    ):
        return cached.vector
    vector = model.encode(body, normalize_embeddings=True)
    # A fresh encode — its length is the live model dimension. Feed it
    # to the reconcile pass so any stale-dimension hydrated entries are
    # purged before they can reach `cosine_similarity_normalized`.
    _note_model_dimension(len(vector))
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

    Raises ``ValueError`` on dimension mismatch. ``zip(strict=True)``
    catches the case where the persistent cache was written with one
    embedding model's output dimension and is being read against
    another — pre-2.6.4 ``zip(a, b)`` truncated to the shorter input
    and produced a meaningless similarity over the overlap, which
    still passed the threshold and silently misranked dedup.
    """
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


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
    global _PERSISTENT_PATH, _HYDRATED, _DIRTY, _MODEL_DIM
    _MODEL_CACHE.clear()
    _LOAD_FAILED.clear()
    _LOAD_FAILED_LOGGED.clear()
    _EMBEDDING_CACHE.clear()
    _PERSISTENT_PATH = None
    _HYDRATED = False
    _DIRTY = False
    _MODEL_DIM = None


__all__ = [
    "DEFAULT_MODEL_NAME",
    "DEFAULT_FASTEMBED_MODEL_NAME",
    "Provider",
    "configure_persistent_cache",
    "flush_persistent_cache",
    "get_model",
    "cached_embed",
    "cosine_similarity_normalized",
    "resolve_provider",
    "reset_caches",
]
