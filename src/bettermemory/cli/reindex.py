"""`bettermemory reindex` — drop and rebuild the FTS5 index."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any

from ..config import Config
from ..store import Store
from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``reindex`` subparser on the parent parser."""
    parser = sub.add_parser(
        "reindex",
        help=(
            "Rebuild the SQLite FTS5 index from the on-disk memories. "
            "The index is normally kept live by Store hooks on every "
            "write / update / tombstone; rerun this when the memory "
            "directory was edited outside the runtime (hand-edits, "
            "external sync, restored backup) so the index catches up. "
            "Safe to run anytime — atomic, transactional, leaves the "
            "prior index intact on partial failure."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--embeddings",
        action="store_true",
        help=(
            "After rebuilding the FTS5 index, also re-embed every "
            "active body into the persistent embedding cache. Useful "
            "after switching `semantic_provider` or "
            "`semantic_model_*` in config — the cache file is "
            "provider+model namespaced, so a fresh one needs warming. "
            "No-op when `semantic_dedup` is off in config. Requires "
            "one of the embedding extras: when neither is installed "
            "the rebuild logs a hint and exits cleanly."
        ),
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory reindex``.

    ``sub_parser`` is forwarded into ``_cli_reindex`` so a write failure in
    the rebuild (read-only memory dir, full disk, a SQLite I/O error)
    surfaces through ``parser.error(...)`` — a clean ``bettermemory
    reindex: error: …`` + exit 2 — instead of an uncaught traceback +
    exit 1, mirroring how ``export`` / ``rename-scope`` / ``proposals``
    thread their subparser through.
    """
    _cli_reindex(json_out=args.json, embeddings=args.embeddings, parser=sub_parser)


def _cli_reindex(
    *,
    json_out: bool,
    embeddings: bool = False,
    parser: argparse.ArgumentParser | None = None,
) -> None:
    """`bettermemory reindex` — drop and rebuild the FTS5 index from
    the on-disk memories.

    Reports before/after counts so a partial corruption shows up as
    "indexed 234 of 250" instead of silently. The rebuild itself is
    transactional — if it fails partway, the prior index is intact
    and the caller sees the failure rather than a half-built index.

    With `--embeddings` (`embeddings=True`), additionally re-embed
    every active body into the persistent embedding cache. The cache
    file is provider+model namespaced (see
    `semantic.configure_persistent_cache`), so a config swap from
    torch → fastembed (or any model change) leaves the old file as
    dead weight and needs the new file populated. This step is opt-in
    because torch loads can take 1-2s and the model download (when
    not cached) is several hundred MB; running it implicitly on every
    `reindex` would punish users who don't use semantic dedup at all.
    """
    import json as _json

    from .. import index as _index

    ctx = cli_context()
    config = ctx.config
    directory = ctx.directory
    store = ctx.store

    before = _index.status(directory)
    embeddings_report: dict[str, Any] | None = None
    try:
        count = _index.rebuild(directory, store.iter_active())
        after = _index.status(directory)
        if embeddings:
            embeddings_report = _reindex_embeddings(config, store)
    except (OSError, sqlite3.Error) as exc:
        # A genuine write failure during rebuild — read-only memory dir,
        # ENOSPC, EACCES on the `.index.db` path, or a SQLite I/O error
        # mid-transaction — raises OSError / sqlite3.Error (the latter is
        # NOT an OSError, so it would otherwise escape). Route it through
        # `parser.error(...)` for a clean `bettermemory reindex: error: …`
        # + exit 2, matching the sibling write commands (export /
        # rename-scope / proposals) instead of dumping a traceback and
        # exiting 1. `_index.rebuild` is transactional, so the prior index
        # is left intact. The `parser is None` fallback (direct
        # `_cli_reindex` callers / tests) re-raises so programmatic callers
        # still see the exception.
        if parser is not None:
            parser.error(str(exc))
        raise

    if json_out:
        payload: dict[str, Any] = {
            "indexed": count,
            "before": before,
            "after": after,
            "directory": str(directory),
        }
        if embeddings_report is not None:
            payload["embeddings"] = embeddings_report
        sys.stdout.write(_json.dumps(payload, indent=2) + "\n")
        return

    sys.stdout.write(
        f"Reindexed {count} memories from {directory}.\n"
        f"  before: {before.get('indexed_count', 0)} indexed, "
        f"{before.get('size_bytes', 0)} bytes\n"
        f"  after:  {after.get('indexed_count', 0)} indexed, "
        f"{after.get('size_bytes', 0)} bytes\n"
    )
    if embeddings_report is not None:
        status = embeddings_report.get("status", "?")
        if status == "ok":
            sys.stdout.write(
                "Re-embedded "
                f"{embeddings_report.get('embedded', 0)} memories with "
                f"provider {embeddings_report.get('provider')!r} "
                f"(model {embeddings_report.get('model')!r}); "
                f"cache flushed to {embeddings_report.get('cache_path')}.\n"
            )
        elif status == "disabled":
            sys.stdout.write(
                "Embedding re-build skipped: `[behavior] semantic_dedup` "
                "is off in config.\n"
            )
        elif status == "no_provider":
            sys.stdout.write(
                "Embedding re-build skipped: neither [embeddings] nor "
                "[embeddings-fast] is installed. Install one of them and "
                "rerun with `--embeddings`.\n"
            )
        elif status == "load_failed":
            sys.stdout.write(
                "Embedding re-build aborted: the configured provider "
                f"{embeddings_report.get('provider')!r} failed to load "
                "its model. See WARNING logs above for the underlying "
                "error.\n"
            )


def _reindex_embeddings(config: Config, store: Store) -> dict[str, Any]:
    """Re-embed every active body into the persistent cache.

    Helper for `bettermemory reindex --embeddings`. Returns a status
    dict so the caller can render text or JSON without re-deriving
    state. Stays close to the call site so the embedding work doesn't
    leak into the FTS5-index code path.
    """
    from ..semantic import _note_model_dimension, cached_embed, flush_persistent_cache
    from ..semantic_setup import (
        _configure_persistent_embeddings,
        _resolve_semantic_provider_and_model,
        _semantic_model_or_none,
    )

    if not config.behavior.semantic_dedup:
        return {"status": "disabled"}

    provider, model_name = _resolve_semantic_provider_and_model(config)
    if provider is None or model_name is None:
        return {"status": "no_provider"}

    _configure_persistent_embeddings(config, store)
    model = _semantic_model_or_none(config)
    if model is None:
        return {"status": "load_failed", "provider": provider, "model": model_name}

    embedded = 0
    primed = False
    for _path, memory in store.iter_active():
        # Embed the STRIPPED body, exactly as every reader does. The
        # persistent cache is keyed on `(memory.id, memory.updated)` —
        # NOT on the body text — so warming from the raw `memory.body`
        # would write an entry under the readers' own key but computed on
        # different text (any leading/trailing whitespace shifts the
        # vector). A subsequent search / dedup that strips first and looks
        # up the same key would read back this wrong-text vector. The read
        # sites this must match all do `memory.body.strip()` and skip
        # empty results: `search.py` (the paraphrase and similar-memory
        # loops) and `consolidate.py` (`_find_dedup_semantic`).
        body = memory.body.strip()
        if not body:
            # Readers `continue` past empty-after-strip bodies and never
            # cache them; do the same so we don't seed an entry the read
            # path would never create.
            continue
        if not primed:
            # Prime the live model dimension from one fresh encode BEFORE the
            # cached_embed hits. On an all-cache-hit run (every body unchanged
            # — the common case for `reindex --embeddings`) cached_embed never
            # encodes, so _MODEL_DIM would stay None and the stale-dimension
            # purge would never fire. A checkpoint that changed its output
            # dimension under the same model_name would then leave the old
            # wrong-dimension vectors in place while we report success.
            # Priming mirrors what the search paths do before their loops.
            _note_model_dimension(len(model.encode("dimension probe")))
            primed = True
        cached_embed(
            model,
            memory.id,
            memory.updated.isoformat(),
            body,
        )
        embedded += 1
    flush_persistent_cache()

    from ..semantic import _PERSISTENT_PATH

    return {
        "status": "ok",
        "provider": provider,
        "model": model_name,
        "embedded": embedded,
        "cache_path": str(_PERSISTENT_PATH) if _PERSISTENT_PATH else None,
    }
