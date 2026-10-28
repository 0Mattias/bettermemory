"""`bettermemory` CLI entry point.

The original `bettermemory.server:main` argparse setup grew past a
thousand lines as subcommands accumulated. This package extracts every
subcommand into its own module so each owns its argparse builder and
handler. `main()` here orchestrates: build the root parser, ask each
module to register its subparser, dispatch on ``args.cmd``.

The exact order of ``sub.add_parser`` calls is load-bearing — it
determines the order subcommands appear in ``bettermemory --help``.
Preserve it when adding or removing modules.

Every ``add_parser`` call passes the same string as both ``help=``
and ``description=``: argparse renders ``help=`` only in the parent
parser's subcommand listing, while ``description=`` is the only
statement of what the command does that ``bettermemory <cmd>
--help`` itself prints. Bind the string to one variable and pass it
twice so the two surfaces can't drift.

MCP wiring (``build_server`` plus ``_register_tools`` and the
server-level ``instructions`` block, neither re-exported through
``server.py``) lives in ``bettermemory.builder``; ``server.py``
re-exports ``build_server`` for back-compat. The split keeps this package's top-level imports
from back-edging through ``server.py``, which was previously the
source of a load-time cycle in ``serve.py``.
"""

from __future__ import annotations

import argparse

from . import (
    audit_turn_cmd,
    backup_cmd,
    capture_cmd,
    daemon_cmd,
    doctor_cmd,
    episodes,
    eval as eval_cmd,
    export,
    health_cmd,
    init as init_cmd,
    log_cmd,
    membership_cmd,
    memory_move_cmd,
    migrate,
    project_cmd,
    prompt_recall_cmd,
    recall_cmd,
    rename_scope,
    review_cmd,
    rollback,
    security_cmd,
    serve,
    service_cmd,
    session_start_cmd,
    stats_cmd,
    tombstones,
    try_cmd,
)

# Several CLI modules carry a ``_cmd`` suffix (``cli/health_cmd.py``,
# ``cli/audit_turn_cmd.py``, ``cli/session_start_cmd.py`` and their
# kin). The handler side keeps the established "strip the memory_
# prefix" convention (``handlers/health.py``), and a bare
# ``cli/health.py`` beside it made a grep for the name return both
# files with no hint at which was which. The suffix keeps the basenames
# apart; the subcommand strings (``bettermemory health``,
# ``bettermemory audit-turn`` and the rest) are unchanged.


def _build_parser() -> tuple[
    argparse.ArgumentParser, dict[str, argparse.ArgumentParser]
]:
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
            "MCP memory for coding agents, checked against your files and "
            "git history on every read. Run with no arguments to start the "
            "MCP server over stdio."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bettermemory {__version__}",
    )
    sub = parser.add_subparsers(dest="cmd")

    # Order is user-facing — it is exactly the order `bettermemory
    # --help` lists subcommands in, and people memorise the layout. Don't
    # reorder casually.
    #
    # NOTHING PINS IT, though: `test_help_lists_all_subcommands` (which
    # this comment used to credit) asserts substring membership over a
    # hand-picked 8-name subset, not order and not completeness — see the
    # note at `tests/test_cli_smoke.py`'s registry/dispatch parity test,
    # which says the same thing from the other side. What IS mechanically
    # enforced is that every key here has a matching `if cmd == "<key>"`
    # arm in `main()` below (`test_subparser_registry_matches_main_dispatch`).
    subparsers: dict[str, argparse.ArgumentParser] = {
        "health": health_cmd.add_subparser(sub),
        "init": init_cmd.add_subparser(sub),
        "migrate": migrate.add_subparser(sub),
        "export": export.add_subparser(sub),
        "tombstones": tombstones.add_subparser(sub),
        "rollback": rollback.add_subparser(sub),
        "episodes": episodes.add_subparser(sub),
        "log": log_cmd.add_subparser(sub),
        "project": project_cmd.add_subparser(sub),
        "memory": memory_move_cmd.add_subparser(sub),
        "membership": membership_cmd.add_subparser(sub),
        "audit-turn": audit_turn_cmd.add_subparser(sub),
        "session-start": session_start_cmd.add_subparser(sub),
        "prompt-recall": prompt_recall_cmd.add_subparser(sub),
        "eval": eval_cmd.add_subparser(sub),
        "review": review_cmd.add_subparser(sub),
        "capture": capture_cmd.add_subparser(sub),
        "recall": recall_cmd.add_subparser(sub),
        "rename-scope": rename_scope.add_subparser(sub),
        "try": try_cmd.add_subparser(sub),
        "backup": backup_cmd.add_subparser(sub),
        "service": service_cmd.add_subparser(sub),
        "doctor": doctor_cmd.add_subparser(sub),
        "security": security_cmd.add_subparser(sub),
        "stats": stats_cmd.add_subparser(sub),
        "up": daemon_cmd.add_up(sub),
        "down": daemon_cmd.add_down(sub),
        "status": daemon_cmd.add_status(sub),
        "serve": serve.add_subparser(sub),
        "hook": _add_hook_subparser(sub),
    }
    return parser, subparsers


def _add_hook_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """`bettermemory hook <event>`, listed here for `--help`. The process
    entry point (`_entry.main`) dispatches the hook words to
    `_daemon_client.hook_main` before this package is imported, so the
    parser arm below is reached only by an in-process caller."""
    parser = sub.add_parser(
        "hook",
        help=(
            "Run a harness hook through the daemon: session-start, stop or "
            "prompt. Reads the hook's stdin JSON, prints the daemon's answer, "
            "always exits 0."
        ),
    )
    parser.add_argument("event", choices=["session-start", "stop", "prompt"])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--transcript-path", default=None)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--prompt", default=None)
    return parser


def main() -> None:
    """CLI entry point. By default runs the stdio shim in front of the
    local daemon (``bettermemory``); ``bettermemory serve`` runs the
    in-process stdio server. Subcommands provide offline tooling:
    ``bettermemory health`` prints the aggregate report, mirroring the
    ``memory_admin`` health action in human-readable form."""
    parser, subparsers = _build_parser()
    args = parser.parse_args()

    cmd = args.cmd
    if cmd is None:
        from ..shim import run_shim

        run_shim()
        return
    if cmd == "up":
        daemon_cmd.run_up(args)
        return
    if cmd == "down":
        daemon_cmd.run_down(args)
        return
    if cmd == "status":
        daemon_cmd.run_status(args)
        return
    if cmd == "serve":
        serve.run_serve()
        return
    if cmd == "hook":
        from .._daemon_client import hook_main

        words = ["hook", args.event]
        if args.quiet:
            words.append("--quiet")
        if args.dry_run:
            words.append("--dry-run")
        for flag in ("transcript_path", "session_id", "prompt"):
            value = getattr(args, flag)
            if value is not None:
                words += ["--" + flag.replace("_", "-"), value]
        raise SystemExit(hook_main(words))

    # Dispatch table. The two-parser convention (root + subparser) for
    # the multi-level commands mirrors the original server.main:
    # ``parser.error(…)`` should report the root prog name on validation
    # failure, while ``.print_help()`` on the subparser shows the right
    # help block when a sub-sub-command was missing.
    if cmd == "health":
        health_cmd.run(args)
        return
    if cmd == "init":
        init_cmd.run(args)
        return
    if cmd == "migrate":
        migrate.run(args, root_parser=parser, sub_parser=subparsers["migrate"])
        return
    if cmd == "export":
        export.run(args, sub_parser=subparsers["export"])
        return
    if cmd == "tombstones":
        tombstones.run(args, root_parser=parser, sub_parser=subparsers["tombstones"])
        return
    if cmd == "rollback":
        rollback.run(args)
        return
    if cmd == "episodes":
        episodes.run(args, sub_parser=subparsers["episodes"])
        return
    if cmd == "log":
        log_cmd.run(args, sub_parser=subparsers["log"])
        return
    if cmd == "project":
        project_cmd.run(args, sub_parser=subparsers["project"])
        return
    if cmd == "memory":
        memory_move_cmd.run(args, sub_parser=subparsers["memory"])
        return
    if cmd == "membership":
        membership_cmd.run(args, sub_parser=subparsers["membership"])
        return
    if cmd == "audit-turn":
        audit_turn_cmd.run(args)
        return
    if cmd == "session-start":
        session_start_cmd.run(args)
        return
    if cmd == "prompt-recall":
        prompt_recall_cmd.run(args)
        return
    if cmd == "eval":
        eval_cmd.run(args, sub_parser=subparsers["eval"])
        return
    if cmd == "review":
        review_cmd.run(args, sub_parser=subparsers["review"])
        return
    if cmd == "capture":
        capture_cmd.run(args, sub_parser=subparsers["capture"])
        return
    if cmd == "recall":
        recall_cmd.run(args, sub_parser=subparsers["recall"])
        return
    if cmd == "rename-scope":
        rename_scope.run(args, root_parser=parser)
        return
    if cmd == "try":
        try_cmd.run(args)
        return
    if cmd == "backup":
        backup_cmd.run(args, sub_parser=subparsers["backup"])
        return
    if cmd == "service":
        service_cmd.run(args, sub_parser=subparsers["service"])
        return
    if cmd == "doctor":
        doctor_cmd.run(args)
        return
    if cmd == "security":
        security_cmd.run(args, sub_parser=subparsers["security"])
        return
    if cmd == "stats":
        stats_cmd.run(args, sub_parser=subparsers["stats"])
        return

    # argparse already rejects unknown subcommands; this is belt-and-
    # suspenders so a typo in a freshly-added subcommand surfaces as a
    # parser error, not a silent no-op falling through to serve.
    parser.error(f"unknown subcommand: {cmd!r}")


__all__ = ["main"]
