"""`bettermemory migrate` — one-shot data migrations."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``migrate`` subparser (with the ``v8`` sub-subparser)."""
    help_text = (
        "One-shot data migrations. `migrate v8` reads a v8 store directory "
        "into a bettermemory 9 store."
    )
    parser = sub.add_parser("migrate", help=help_text, description=help_text)
    migrate_sub = parser.add_subparsers(dest="migrate_cmd")
    v8_help = (
        "Read a v8 store directory (memories, tombstones, episodes, every "
        "event shard and archive, conflicts, the ingest watermark) into a "
        "bettermemory 9 store, one SQLite file. The directory is never "
        "written. Idempotent: a re-run imports what is new and reports the "
        "rest as present. Pending writes and proposals are dropped with a "
        "count; sidecars with no place in the store are left with a count; "
        "unknown files are named."
    )
    v8_parser = migrate_sub.add_parser("v8", help=v8_help, description=v8_help)
    v8_parser.add_argument(
        "--from",
        dest="source",
        type=str,
        default=None,
        metavar="DIR",
        help="The v8 store directory. Default: the resolved store directory.",
    )
    v8_parser.add_argument(
        "--to",
        dest="target",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "The store file to create or open. Default: memory.sqlite inside "
            "the v8 directory."
        ),
    )
    v8_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what a run would import and write nothing.",
    )
    v8_parser.add_argument(
        "--json", action="store_true", help="Print the report as JSON."
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    root_parser: argparse.ArgumentParser,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory migrate``.

    ``root_parser`` is used for ``.error(...)`` so the program-name
    prefix on validation failures matches the pre-extraction output;
    ``sub_parser`` is used for ``.print_help()`` so a bare
    ``bettermemory migrate`` invocation prints the migrate-scoped help
    instead of the root help.
    """
    if args.migrate_cmd == "v8":
        _cli_migrate_v8(
            source=args.source,
            target=args.target,
            dry_run=args.dry_run,
            json_out=args.json,
            parser=root_parser,
        )
        return
    sub_parser.print_help()


def _cli_migrate_v8(
    *,
    source: str | None,
    target: str | None,
    dry_run: bool,
    json_out: bool,
    parser: argparse.ArgumentParser,
) -> None:
    """`bettermemory migrate v8` — a v8 directory into a bettermemory 9
    store. Exit 0 with the report; 2 on a source that is not a directory;
    1 when the store cannot be written."""
    import json as _json

    from ..config import load_config
    from ..migrate_v8 import migrate
    from ..store import STORE_FILENAME

    root = Path(source).expanduser() if source else load_config().resolved_directory()
    if not root.is_dir():
        parser.error(f"--from: no v8 store directory at {root}")
    store_path = Path(target).expanduser() if target else root / STORE_FILENAME
    try:
        report = migrate(root, store_path, dry_run=dry_run)
    except (OSError, ValueError, sqlite3.Error) as exc:
        sys.stderr.write(f"migrate v8: {exc}\n")
        raise SystemExit(1) from None
    if json_out:
        sys.stdout.write(_json.dumps(report.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(report.render_text())
