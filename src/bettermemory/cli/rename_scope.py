"""`bettermemory rename-scope` — bulk-rename a scope tag across the store."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..models import validate_scope
from ._common import cli_context, cli_recorder


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``rename-scope`` subparser."""
    help_text = (
        "Replace OLD with NEW across every memory's scope list — the cheap "
        "fix for a typo'd or deprecated scope (e.g. `infra` -> "
        "`infrastructure`). The CLI counterpart of the memory_rename_scope "
        "tool. Bumps `updated` but preserves `last_verified_at`; tombstones "
        "are renamed too unless --no-tombstones."
    )
    parser = sub.add_parser("rename-scope", help=help_text, description=help_text)
    parser.add_argument("old", metavar="OLD", help="The scope to rename.")
    parser.add_argument("new", metavar="NEW", help="The replacement scope.")
    parser.add_argument(
        "--no-tombstones",
        action="store_true",
        help=(
            "Leave the tombstone (removed-memory) audit log untouched. By "
            "default tombstones are renamed too so the curation view stays "
            "consistent."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    return parser


def run(args: argparse.Namespace, *, root_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory rename-scope``."""
    _cli_rename_scope(
        old=args.old,
        new=args.new,
        include_tombstones=not args.no_tombstones,
        json_out=args.json,
        parser=root_parser,
    )


def _cli_rename_scope(
    *,
    old: str,
    new: str,
    include_tombstones: bool,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory rename-scope OLD NEW` — bulk scope rename.

    The CLI counterpart of the `memory_rename_scope` MCP tool, mirroring its
    validation (scope grammar, OLD != NEW, allowed-scopes whitelist) so the two
    entry points can't drift. Routes validation failures and genuine disk
    errors through `parser.error` for a clean `bettermemory: error: …` + exit 2
    instead of an uncaught traceback; the `parser is None` fallback (direct
    callers / tests) re-raises so programmatic callers still see the exception.
    """
    import json as _json

    ctx = cli_context()
    store = ctx.store
    config = ctx.config

    try:
        clean_old = validate_scope(old)
        clean_new = validate_scope(new)
        if clean_old == clean_new:
            raise ValueError("OLD and NEW must differ")
        if config.scopes.allowed and clean_new not in set(config.scopes.allowed):
            raise ValueError(
                f"new scope {clean_new!r} is not in the allowed list: "
                f"{sorted(config.scopes.allowed)}"
            )
    except ValueError as exc:
        if parser is not None:
            parser.error(str(exc))
        raise

    try:
        result = store.rename_scope(
            clean_old, clean_new, include_tombstones=include_tombstones
        )
    except OSError as exc:
        # Store.rename_scope swallows per-file races/malformed files; a genuine
        # disk-level failure still propagates. Surface it as a clean CLI error,
        # mirroring the memory_rename_scope MCP tool's OSError arm. The rename
        # is applied file-by-file and is idempotent, so a re-run safely
        # finishes any remaining files.
        msg = (
            f"failed to rename scope {clean_old!r} -> {clean_new!r}: {exc} "
            "(rename may be partially applied; safe to re-run)"
        )
        if parser is not None:
            parser.error(msg)
        raise

    active = result["active"]
    tombstoned = result["tombstoned"]
    # Mirrors the `memory_rename_scope` MCP tool's event, field for
    # field, so the audit log reads one shape whichever entry point ran
    # the rename; the recorder's attribution tells them apart.
    cli_recorder(ctx, attribution="cli_rename_scope").record(
        "rename_scope",
        old=clean_old,
        new=clean_new,
        include_tombstones=include_tombstones,
        active_count=len(active),
        tombstoned_count=len(tombstoned),
        failed_count=len(result.get("failed", [])),
    )
    # Item 6/6b: records whose per-record re-dump was skipped inside the rename
    # loop (e.g. the rename would push the file past the size cap). Surface them
    # so a partial run isn't silently reported as a clean one. `Store.rename_scope`
    # omits the key on a clean run, so normalise with `.get`.
    failed = result.get("failed", [])
    if json_out:
        sys.stdout.write(
            _json.dumps(
                {
                    "old_scope": clean_old,
                    "new_scope": clean_new,
                    "active": active,
                    "tombstoned": tombstoned,
                    "failed": failed,
                },
                indent=2,
            )
            + "\n"
        )
        return
    total = len(active) + len(tombstoned)
    noun = "memory" if total == 1 else "memories"
    sys.stdout.write(
        f"Renamed scope {clean_old!r} -> {clean_new!r}: "
        f"{len(active)} active + {len(tombstoned)} tombstoned {noun} updated.\n"
    )
    if failed:
        fnoun = "record" if len(failed) == 1 else "records"
        sys.stdout.write(
            f"WARNING: {len(failed)} {fnoun} could not be renamed and were "
            f"skipped (re-run after shrinking them):\n"
        )
        for entry in failed:
            sys.stdout.write(f"  - {entry['id']}: {entry['reason']}\n")
