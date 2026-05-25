"""`bettermemory` CLI entry point.

The original `bettermemory.server:main` argparse setup grew past a
thousand lines as subcommands accumulated. This package extracts every
subcommand into its own module so each owns its argparse builder and
handler. `main()` here orchestrates: build the root parser, ask each
module to register its subparser, dispatch on ``args.cmd``.

The exact order of ``sub.add_parser`` calls is load-bearing — it
determines the order subcommands appear in ``bettermemory --help``.
Preserve it when adding or removing modules.

MCP wiring (``build_server`` plus ``_register_tools`` and the
FastMCP ``instructions`` block, both private to the builder) lives
in ``bettermemory.builder``; ``server.py`` re-exports ``build_server``
for back-compat. The split keeps this package's top-level imports
from back-edging through ``server.py``, which was previously the
source of a load-time cycle in ``serve.py``.
"""

from __future__ import annotations

import argparse

from . import (
    audit_turn_cmd,
    consolidate,
    doctor,
    eval as eval_cmd,
    export,
    health_cmd,
    ingest,
    init as init_cmd,
    migrate,
    reindex,
    serve,
    sync,
    tombstones,
    ui,
)

# Round-3 audit fix: the CLI modules for the ``health`` and
# ``audit-turn`` subcommands were originally named ``cli/health.py`` and
# ``cli/audit_turn.py``, colliding with the MCP-tool-handler modules at
# ``handlers/health.py`` and ``handlers/audit_turn.py``. Grepping
# ``audit_turn`` from a fresh checkout returned both files with no hint
# at which was which. Renaming the CLI side (the handler side keeps the
# established "strip the memory_ prefix" convention) eliminates the
# basename collision while preserving every CLI surface — the
# subcommand strings ``bettermemory health`` and ``bettermemory
# audit-turn`` are unchanged.


def _build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    """Construct the root parser and every subparser.

    Returns ``(parser, subparsers)`` where ``subparsers`` is the dict of
    per-subcommand parsers keyed by their ``cmd`` name. The dict is
    needed by dispatchers that need to call ``.print_help()`` on a
    specific subparser when no sub-sub-command was given.
    """
    # Lazy version lookup: `cli/__init__.py` is imported by
    # `bettermemory/__init__.py` (via `from .cli import main`) before
    # `__version__` is bound there; a top-level `from .. import
    # __version__` would hit a partial-module AttributeError. Deferring
    # the lookup until call time avoids the race.
    from .. import __version__

    parser = argparse.ArgumentParser(
        prog="bettermemory",
        description=(
            "Persistent memory for Claude Code, retrieved on demand. "
            "Run with no arguments to start the MCP server over stdio."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bettermemory {__version__}",
    )
    sub = parser.add_subparsers(dest="cmd")

    # Order pinned by tests/test_cli_smoke.py
    # (`test_help_lists_all_subcommands`) and by users who memorise
    # the layout. Don't reorder casually.
    subparsers: dict[str, argparse.ArgumentParser] = {
        "health": health_cmd.add_subparser(sub),
        "doctor": doctor.add_subparser(sub),
        "init": init_cmd.add_subparser(sub),
        "migrate": migrate.add_subparser(sub),
        "export": export.add_subparser(sub),
        "tombstones": tombstones.add_subparser(sub),
        "ui": ui.add_subparser(sub),
        "sync": sync.add_subparser(sub),
        "reindex": reindex.add_subparser(sub),
        "audit-turn": audit_turn_cmd.add_subparser(sub),
        "consolidate": consolidate.add_subparser(sub),
        "ingest": ingest.add_subparser(sub),
        "eval": eval_cmd.add_subparser(sub),
    }
    return parser, subparsers


def main() -> None:
    """CLI entry point. By default runs the MCP server over stdio
    (``bettermemory``). Subcommands provide offline tooling: ``bettermemory
    health`` prints the aggregate report, mirroring the ``memory_health``
    tool in human-readable form."""
    parser, subparsers = _build_parser()
    args = parser.parse_args()

    cmd = args.cmd
    if cmd is None:
        serve.run_serve()
        return

    # Dispatch table. The two-parser convention (root + subparser) for
    # the multi-level commands mirrors the original server.main:
    # ``parser.error(…)`` should report the root prog name on validation
    # failure, while ``.print_help()`` on the subparser shows the right
    # help block when a sub-sub-command was missing.
    if cmd == "health":
        health_cmd.run(args)
        return
    if cmd == "doctor":
        doctor.run(args)
        return
    if cmd == "init":
        init_cmd.run(args)
        return
    if cmd == "migrate":
        migrate.run(args, root_parser=parser, sub_parser=subparsers["migrate"])
        return
    if cmd == "export":
        export.run(args)
        return
    if cmd == "tombstones":
        tombstones.run(args, root_parser=parser, sub_parser=subparsers["tombstones"])
        return
    if cmd == "ui":
        ui.run(args)
        return
    if cmd == "sync":
        sync.run(args, sub_parser=subparsers["sync"])
        return
    if cmd == "reindex":
        reindex.run(args)
        return
    if cmd == "audit-turn":
        audit_turn_cmd.run(args)
        return
    if cmd == "consolidate":
        consolidate.run(args)
        return
    if cmd == "ingest":
        ingest.run(args, sub_parser=subparsers["ingest"])
        return
    if cmd == "eval":
        eval_cmd.run(args, sub_parser=subparsers["eval"])
        return

    # argparse already rejects unknown subcommands; this is belt-and-
    # suspenders so a typo in a freshly-added subcommand surfaces as a
    # parser error, not a silent no-op falling through to serve.
    parser.error(f"unknown subcommand: {cmd!r}")


__all__ = ["main"]
