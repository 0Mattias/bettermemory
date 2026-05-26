"""`bettermemory audit-turn` — Claude Code Stop-hook silent-miss audit."""

from __future__ import annotations

import argparse


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``audit-turn`` subparser on the parent parser."""
    parser = sub.add_parser(
        "audit-turn",
        help=(
            "Run a silent-miss audit for the just-completed turn. "
            "Intended as a Claude Code Stop hook target: reads the "
            "hook's stdin JSON (`session_id`, `transcript_path`) and "
            "calls `audit.probe_for_miss` against the active store. "
            "Use --transcript-path + --session-id to invoke manually "
            "for debugging. Always exits 0 so a hook misfire never "
            "breaks the turn-end pipeline."
        ),
    )
    parser.add_argument(
        "--transcript-path",
        type=str,
        default=None,
        help="Override the transcript path from the Stop hook payload.",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Override the session id from the Stop hook payload.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Skip the JSON summary on stdout. Events still land in the log.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory audit-turn``."""
    from ..hook import main as _hook_main

    raise SystemExit(
        _hook_main(
            [
                *(
                    ["--transcript-path", args.transcript_path]
                    if args.transcript_path
                    else []
                ),
                *(["--session-id", args.session_id] if args.session_id else []),
                *(["--quiet"] if args.quiet else []),
            ]
        )
    )
