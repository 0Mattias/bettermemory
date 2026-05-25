"""`bettermemory export` — dump active (and optionally tombstoned) memories
to a self-describing JSON document.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..models import utcnow, validate_scope
from .._response import isoformat
from ..store import Store


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``export`` subparser on the parent parser."""
    parser = sub.add_parser(
        "export",
        help=(
            "Dump all active memories (and tombstones, by default) to a "
            "self-describing JSON document. The format is round-trippable "
            "and intended for backup, migration between machines, or "
            "feeding an external indexer. Writes to stdout unless "
            "--output is given."
        ),
    )
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


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory export``."""
    _cli_export(
        output=args.output,
        include_tombstones=not args.no_tombstones,
        scopes=args.scope or None,
    )


def _cli_export(
    *,
    output: str | None,
    include_tombstones: bool,
    scopes: list[str] | None,
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
        scopes = [validate_scope(s) for s in scopes]
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
        out_path.write_text(text + "\n", encoding="utf-8")
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
