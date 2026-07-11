"""`bettermemory export` — dump active (and optionally tombstoned) memories
to a self-describing JSON document.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..models import utcnow, validate_scope
from .._fsutil import atomic_write_bytes
from .._response import isoformat
from ..store import Store


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``export`` subparser on the parent parser."""
    help_text = (
        "Dump all active memories (and tombstones, by default) to a "
        "self-describing JSON document. The format is round-trippable "
        "and intended for backup, migration between machines, or "
        "feeding an external indexer. Writes to stdout unless "
        "--output is given."
    )
    parser = sub.add_parser("export", help=help_text, description=help_text)
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Write the export to PATH instead of stdout. Use this for "
            "scripted backups (`bettermemory export -o backup.json`)."
        ),
    )
    parser.add_argument(
        "--no-tombstones",
        action="store_true",
        help=(
            "Skip tombstoned memories. By default the export includes "
            "them so a restored archive carries the same removal-reason "
            "audit trail; use this when you only want the live set."
        ),
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to memories tagged with at least one of the given "
            "scopes. Repeat to widen the filter. Applies to both active "
            "and tombstoned records."
        ),
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory export``.

    ``sub_parser`` is forwarded into ``_cli_export`` so a malformed
    ``--scope`` surfaces through ``parser.error(...)`` (a clean
    ``bettermemory export: error: …`` + exit 2) instead of an uncaught
    ``ValueError`` traceback — mirroring how ``eval`` / ``episodes``
    thread their subparser through.
    """
    _cli_export(
        output=args.output,
        include_tombstones=not args.no_tombstones,
        scopes=args.scope or None,
        parser=sub_parser,
    )


def _cli_export(
    *,
    output: str | None,
    include_tombstones: bool,
    scopes: list[str] | None,
    parser: argparse.ArgumentParser | None = None,
) -> None:
    """`bettermemory export` — dump active (and optionally tombstoned)
    memories to a self-describing JSON document.

    Format (`format_version: 1`):

        {
          "format_version": 1,
          "exported_at": "2026-05-09T12:34:56Z",
          "source_directory": "/Users/me/.claude-memory",
          "active_memories":     [<full Memory dict>, ...],
          "tombstoned_memories": [<full TombstonedMemory dict>, ...]
        }

    `tombstoned_memories` is omitted entirely when --no-tombstones is
    passed (vs. emitted as []) so a consumer can distinguish "not
    requested" from "no tombstones present". Each memory dict mirrors
    the Pydantic model — id, created, updated, scopes, confidence,
    source, body, origin, last_verified_at — and tombstones add
    removed / removed_reason / removed_session.

    The shape is intended to be round-trippable: a future
    `bettermemory import` can recreate active records and tombstones
    from this document with no loss. Bump format_version on any
    breaking change.
    """
    import json as _json
    from pathlib import Path as _Path

    # Lazy import keeps the module-import cost of ``bettermemory.cli``
    # off the hot path; ``load_config`` reads TOML and only matters when
    # ``export`` actually runs.
    from ..config import load_config as _load_config

    config = _load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    if scopes:
        # `validate_scope` raises ValueError on a malformed --scope
        # (uppercase, spaces, illegal chars). Route it through
        # `parser.error(...)` for a clean `bettermemory export: error: …`
        # + exit 2 instead of an uncaught traceback. The `parser is None`
        # fallback (direct `_cli_export` callers / tests) keeps the raw
        # ValueError so programmatic callers still see the exception.
        try:
            scopes = [validate_scope(s) for s in scopes]
        except ValueError as exc:
            if parser is not None:
                parser.error(str(exc))
            raise
    scope_set = set(scopes) if scopes else None

    active = store.load_all()
    if scope_set is not None:
        active = [m for m in active if scope_set.intersection(m.scopes)]

    payload: dict[str, Any] = {
        "format_version": 1,
        "exported_at": isoformat(utcnow()),
        "source_directory": str(directory),
        "active_memories": [m.model_dump(mode="json") for m in active],
    }
    tombstoned_count = 0
    if include_tombstones:
        tombstoned = store.load_tombstones()
        if scope_set is not None:
            tombstoned = [t for t in tombstoned if scope_set.intersection(t.scopes)]
        payload["tombstoned_memories"] = [t.model_dump(mode="json") for t in tombstoned]
        tombstoned_count = len(tombstoned)

    text = _json.dumps(payload, indent=2)

    if output:
        out_path = _Path(output)
        # Pre-check the parent is an existing directory. `atomic_write_bytes`
        # would otherwise silently create the parent tree via
        # `parent.mkdir(parents=True, exist_ok=True)` — that auto-mkdir
        # is intentional for fresh-install callers (init.py creating
        # ~/.claude.json under a missing ~/.config, sync.py creating a
        # .gitignore under a fresh sync root) but wrong here: a user
        # who typed `bettermemory export -o /typod/path/backup.json`
        # wants a loud error, not a silently-created
        # /typod/path/ tree with their backup buried inside. Pre-3.2.1
        # the bare `write_text` raised FileNotFoundError for missing
        # parents; this restores that contract while preserving the
        # atomic-write durability benefit. `out_path.parent` is
        # Path(".") when output is a bare filename, which always
        # exists as a directory — so this only fires on a genuinely
        # bad parent.
        #
        # `is_dir()` (not `exists()`): if the parent path is a regular
        # FILE, `exists()` returns True and the pre-check would pass,
        # but the helper's `mkdir(parents=True, exist_ok=True)` then
        # raises a confusing `FileExistsError` naming the internal
        # `.tmp` path. `is_dir()` catches both the missing-parent and
        # the parent-is-a-file cases here, so the export caller surfaces
        # one clean error pointing at the parent the user actually typed.
        parent = out_path.parent
        if not parent.is_dir():
            msg = (
                f"--output parent directory does not exist or is not a "
                f"directory: {parent}"
            )
            # Route through `parser.error` for a clean
            # `bettermemory export: error: …` + exit 2 instead of a raw
            # traceback / exit 1, mirroring the --scope ValueError arm
            # above and the sibling rename-scope / tombstones-restore
            # commands. `parser is None` (programmatic / test callers)
            # keeps the raw exception so they still see the exception type.
            if parser is not None:
                parser.error(msg)
            raise FileNotFoundError(msg)
        # Atomic + durable write via `_fsutil.atomic_write_bytes`: a plain
        # `out_path.write_text(...)` here would leave a truncated JSON on
        # power loss / process kill mid-write, defeating the point of a
        # backup. The helper writes to a tmp sibling, fsyncs, atomic-
        # renames into place, and fsyncs the parent directory. A genuine
        # filesystem failure here (read-only parent, ENOSPC, EACCES)
        # routes through `parser.error` for the same clean exit 2, rather
        # than leaking a raw OSError traceback.
        try:
            atomic_write_bytes(out_path, (text + "\n").encode("utf-8"))
        except OSError as exc:
            if parser is not None:
                parser.error(str(exc))
            raise
        summary = f"Exported {len(active)} active memories"
        if include_tombstones:
            summary += f" + {tombstoned_count} tombstones"
        summary += f" to {out_path}\n"
        # Status line goes to stderr so `-o` callers can still pipe
        # the file path on stdout if they want; consistent with how
        # most CLI tools split status from data.
        sys.stderr.write(summary)
        return

    sys.stdout.write(text + "\n")
