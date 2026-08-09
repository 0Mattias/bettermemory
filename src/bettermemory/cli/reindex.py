"""`bettermemory reindex` — drop and rebuild the FTS5 index."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Any

from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``reindex`` subparser on the parent parser."""
    help_text = (
        "Rebuild the SQLite FTS5 index from the on-disk memories. "
        "The index is normally kept live by Store hooks on every "
        "write / update / tombstone; rerun this when the memory "
        "directory was edited outside the runtime (hand-edits, "
        "external sync, restored backup) so the index catches up. "
        "Safe to run anytime — atomic, transactional, leaves the "
        "prior index intact on partial failure."
    )
    parser = sub.add_parser("reindex", help=help_text, description=help_text)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
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
    _cli_reindex(json_out=args.json, parser=sub_parser)


def _cli_reindex(
    *,
    json_out: bool,
    parser: argparse.ArgumentParser | None = None,
) -> None:
    """`bettermemory reindex` — drop and rebuild the FTS5 index from
    the on-disk memories.

    Reports before/after counts so a partial corruption shows up as
    "indexed 234 of 250" instead of silently. The rebuild itself is
    transactional — if it fails partway, the prior index is intact
    and the caller sees the failure rather than a half-built index.
    """
    import json as _json

    from .. import index as _index

    ctx = cli_context()
    directory = ctx.directory
    store = ctx.store

    before = _index.status(directory)
    try:
        count = _index.rebuild(directory, store.iter_active())
        after = _index.status(directory)
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
        sys.stdout.write(_json.dumps(payload, indent=2) + "\n")
        return

    sys.stdout.write(
        f"Reindexed {count} memories from {directory}.\n"
        f"  before: {before.get('indexed_count', 0)} indexed, "
        f"{before.get('size_bytes', 0)} bytes\n"
        f"  after:  {after.get('indexed_count', 0)} indexed, "
        f"{after.get('size_bytes', 0)} bytes\n"
    )
