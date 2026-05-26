"""`bettermemory health` — print the aggregate memory health report."""

from __future__ import annotations

import argparse
import sys

from ..health import report_for_directory
from ..origin import capture as capture_origin
from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``health`` subparser on the parent parser."""
    parser = sub.add_parser("health", help="Print the aggregate memory health report.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help=(
            "Window in days for the dead-weight cutoff. Memories created "
            "more than this many days ago with no `applied` events are "
            "flagged. Default: 30."
        ),
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="How many heavily-used memories to list. Default: 10.",
    )
    parser.add_argument(
        "--min-applied",
        type=int,
        default=None,
        help=(
            "Minimum applied_count for inclusion in heavily_used. Default "
            "comes from config.toml `behavior.heavily_used_min_applied` "
            "(typically 3). Lower to 1 on a fresh store to see anything "
            "that's been applied at least once."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory health``."""
    _cli_health(
        json_out=args.json,
        days=args.days,
        top_k=args.top_k,
        min_applied=args.min_applied,
    )


def _cli_health(
    *,
    json_out: bool,
    days: int,
    top_k: int,
    min_applied: int | None = None,
) -> None:
    """`bettermemory health` — print the aggregate report."""
    from ..health import render_json, render_text

    ctx = cli_context()
    # `--min-applied` overrides the config default; fall through to the
    # configured value when the flag wasn't passed. Avoids forcing the user
    # to pass the same number to every CLI invocation.
    threshold = (
        min_applied
        if min_applied is not None
        else ctx.config.behavior.heavily_used_min_applied
    )
    report = report_for_directory(
        ctx.directory,
        window_days=days,
        heavily_used_top_k=top_k,
        heavily_used_min_applied=threshold,
        verification_stale_days=ctx.config.behavior.verification_stale_days,
        # Capture caller origin so the CLI rendering picks up the
        # commit-drift bucket when run from inside a project whose
        # memories live in this store.
        caller_origin=capture_origin(),
    )
    sys.stdout.write(render_json(report) if json_out else render_text(report))
