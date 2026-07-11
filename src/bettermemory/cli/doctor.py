"""`bettermemory doctor` — diagnose install state."""

from __future__ import annotations

import argparse


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``doctor`` subparser on the parent parser."""
    parser = sub.add_parser(
        "doctor",
        # Category summary, deliberately NOT a check-by-check list: the
        # old enumeration went stale every time the suite grew (it never
        # gained the sync-leak checks). Categories absorb new checks —
        # don't reintroduce a list or a hardcoded count here.
        help=(
            "Diagnose install state. Runs the full check suite — install "
            "wiring (binary path, config, MCP client configs), store "
            "integrity (parse, search index, storage), and sync-repo leak "
            "surfaces (tracked-despite-gitignore sidecars, parent repos "
            "tracking store files) — each failure prints a one-line fix "
            "hint. Exits 0/1/2 for ok/warn/fail."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (machine-readable) instead of human-readable text.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory doctor``."""
    from ..doctor import cli_doctor

    raise SystemExit(cli_doctor(json_out=args.json))
