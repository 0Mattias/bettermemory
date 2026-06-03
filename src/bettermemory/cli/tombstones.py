"""`bettermemory tombstones` — inspect and prune the tombstone audit log."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..models import utcnow, validate_scope
from .._response import isoformat
from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``tombstones`` subparser (with list/prune sub-subparsers)."""
    parser = sub.add_parser(
        "tombstones",
        help=(
            "Inspect, restore, and prune the tombstone (removed-memory) "
            "audit log. Subcommands: list, restore, prune."
        ),
    )
    tombstones_sub = parser.add_subparsers(dest="tombstones_cmd")

    tlist_parser = tombstones_sub.add_parser(
        "list", help="Print all tombstones with removal metadata."
    )
    tlist_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    tlist_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to tombstones tagged with at least one of the given "
            "scopes. Repeat to widen the filter."
        ),
    )

    trestore_parser = tombstones_sub.add_parser(
        "restore",
        help=(
            "Bring a tombstoned memory back to the active set by id. Strips "
            "removal frontmatter and preserves original timestamps. The CLI "
            "counterpart of the memory_restore tool, for the lean default "
            "surface where that tool isn't registered."
        ),
    )
    trestore_parser.add_argument(
        "id",
        metavar="ID",
        help="Id of the tombstoned memory to restore.",
    )
    trestore_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    tprune_parser = tombstones_sub.add_parser(
        "prune",
        help=(
            "Hard-delete tombstones older than --older-than days. "
            "Active memories are unaffected. Default value comes from "
            "config.toml `behavior.tombstone_retention_days`; if that's 0 "
            "(the default), --older-than is required."
        ),
    )
    tprune_parser.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "Cutoff in days. Tombstones whose `removed` timestamp is older "
            "than this are deleted. Required if no default is configured."
        ),
    )
    tprune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without touching disk.",
    )
    tprune_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    root_parser: argparse.ArgumentParser,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory tombstones``.

    ``root_parser`` is forwarded into ``_cli_tombstones_list`` /
    ``_cli_tombstones_restore`` / ``_cli_tombstones_prune`` so validation
    failures (a malformed ``--scope``, an unknown restore id, the missing
    ``--older-than`` default) surface through ``parser.error(...)`` with the
    root prog prefix; ``sub_parser`` is used for ``.print_help()`` when a bare
    ``bettermemory tombstones`` is invoked.
    """
    if args.tombstones_cmd == "list":
        _cli_tombstones_list(
            json_out=args.json, scopes=args.scope or None, parser=root_parser
        )
        return
    if args.tombstones_cmd == "restore":
        _cli_tombstones_restore(
            memory_id=args.id, json_out=args.json, parser=root_parser
        )
        return
    if args.tombstones_cmd == "prune":
        _cli_tombstones_prune(
            older_than_days=args.older_than,
            dry_run=args.dry_run,
            json_out=args.json,
            parser=root_parser,
        )
        return
    sub_parser.print_help()


def _cli_tombstones_list(
    *,
    json_out: bool,
    scopes: list[str] | None,
    parser: Any = None,
) -> None:
    """`bettermemory tombstones list` — print removed memories."""
    import json as _json

    ctx = cli_context()
    store = ctx.store
    if scopes:
        # `validate_scope` raises ValueError on a malformed --scope
        # (uppercase, spaces, illegal chars). Route it through
        # `parser.error(...)` for a clean `bettermemory: error: …` + exit
        # 2 instead of an uncaught traceback that leaks internal paths —
        # mirroring `_cli_export`. The `parser is None` fallback (direct
        # callers / tests) re-raises so programmatic callers still see the
        # exception.
        try:
            scopes = [validate_scope(s) for s in scopes]
        except ValueError as exc:
            if parser is not None:
                parser.error(str(exc))
            raise
    summaries = store.list_tombstones(scopes=scopes)

    if json_out:
        sys.stdout.write(
            _json.dumps(
                [
                    {
                        "id": s.id,
                        "scopes": s.scopes,
                        "summary": s.summary,
                        "created": isoformat(s.created),
                        "removed": isoformat(s.removed),
                        "removed_reason": s.removed_reason,
                        "removed_session": s.removed_session,
                    }
                    for s in summaries
                ],
                indent=2,
            )
            + "\n"
        )
        return

    if not summaries:
        sys.stdout.write("No tombstones.\n")
        return

    sys.stdout.write(f"Tombstones ({len(summaries)}):\n")
    for s in summaries:
        sess = s.removed_session or "<no session>"
        sys.stdout.write(
            f"  {s.id} [removed={isoformat(s.removed)}, "
            f"session={sess}] {','.join(s.scopes)}: {s.summary}\n"
            f"    reason: {s.removed_reason}\n"
        )


def _cli_tombstones_restore(
    *,
    memory_id: str,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory tombstones restore <id>` — un-tombstone a memory.

    The CLI counterpart of the `memory_restore` MCP tool, so a user on the
    lean default surface (`full_tool_surface = false`, where `memory_restore`
    isn't registered) can still recover a removed memory. Wraps
    `store.restore`, routing its KeyError-family failures (invalid/unknown id,
    or an id that's already active) and the malformed-frontmatter / disk-error
    cases through `parser.error` for a clean `bettermemory: error: …` + exit 2
    instead of an uncaught traceback — mirroring `_cli_tombstones_list`. The
    `parser is None` fallback (direct callers / tests) re-raises so
    programmatic callers still see the exception.
    """
    import json as _json

    from ..store import MemoryNotFoundError, NotTombstonedError

    ctx = cli_context()
    store = ctx.store
    try:
        memory = store.restore(memory_id)
    except (MemoryNotFoundError, NotTombstonedError, ValueError, OSError) as exc:
        if parser is not None:
            parser.error(str(exc))
        raise

    if json_out:
        sys.stdout.write(
            _json.dumps(
                {"id": memory.id, "scopes": memory.scopes},
                indent=2,
            )
            + "\n"
        )
        return
    sys.stdout.write(f"Restored {memory.id} [{','.join(memory.scopes)}]\n")


def _cli_tombstones_prune(
    *,
    older_than_days: int | None,
    dry_run: bool,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory tombstones prune` — hard-delete old tombstones."""
    import json as _json
    from datetime import timedelta

    ctx = cli_context()
    days = (
        older_than_days
        if older_than_days is not None
        else ctx.config.behavior.tombstone_retention_days
    )
    if days is None or days <= 0:
        # Hard refusal — pruning everything by accident would be a foot-gun.
        parser.error(
            "--older-than is required (no default configured). Pass an "
            "explicit cutoff in days, or set "
            "`behavior.tombstone_retention_days` in config.toml."
        )
    cutoff = timedelta(days=days)

    store = ctx.store

    if dry_run:
        # Use load_tombstones to inspect; don't call prune which deletes.
        now = utcnow()
        candidates = [t for t in store.load_tombstones() if t.removed < (now - cutoff)]
        ids = [t.id for t in candidates]
        if json_out:
            sys.stdout.write(
                _json.dumps({"would_delete": ids, "cutoff_days": days}, indent=2) + "\n"
            )
            return
        if not ids:
            sys.stdout.write(f"No tombstones older than {days} days.\n")
            return
        sys.stdout.write(
            f"Would delete {len(ids)} tombstone(s) older than {days} days:\n"
        )
        for t in candidates:
            sys.stdout.write(
                f"  {t.id} [removed={isoformat(t.removed)}]: {t.removed_reason}\n"
            )
        sys.stdout.write("(Dry run — re-run without --dry-run to apply.)\n")
        return

    pruned_ids = store.prune_tombstones(cutoff)
    if json_out:
        sys.stdout.write(
            _json.dumps({"deleted": pruned_ids, "cutoff_days": days}, indent=2) + "\n"
        )
        return
    if not pruned_ids:
        sys.stdout.write(f"No tombstones older than {days} days.\n")
        return
    sys.stdout.write(
        f"Deleted {len(pruned_ids)} tombstone(s) older than {days} days:\n"
    )
    for memory_id in pruned_ids:
        sys.stdout.write(f"  {memory_id}\n")
