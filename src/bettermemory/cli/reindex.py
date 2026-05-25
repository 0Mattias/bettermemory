"""`bettermemory reindex` — drop and rebuild the FTS5 index."""

from __future__ import annotations

import argparse
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


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory reindex``."""
    _cli_reindex(json_out=args.json, embeddings=args.embeddings)


def _cli_reindex(*, json_out: bool, embeddings: bool = False) -> None:
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
    count = _index.rebuild(directory, store.iter_active())
    after = _index.status(directory)

    embeddings_report: dict[str, Any] | None = None
    if embeddings:
        embeddings_report = _reindex_embeddings(config, store)

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
    from ..semantic import cached_embed, flush_persistent_cache
    from ..server import (
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
    for _path, memory in store.iter_active():
        cached_embed(
            model,
            memory.id,
            memory.updated.isoformat(),
            memory.body,
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
