"""`bettermemory doctor` — diagnose install state."""

from __future__ import annotations

import argparse


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``doctor`` subparser on the parent parser."""
    parser = sub.add_parser(
        "doctor",
        help=(
            "Diagnose install state. Runs a series of checks: binary on "
            "PATH, config loadable, storage dir writable, memories parse, "
            "event log writable, semantic-dedup extras present (when "
            "enabled), MCP client configs cross-checked against the "
            "currently-resolved binary path. Exits 0/1/2 for ok/warn/fail."
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
