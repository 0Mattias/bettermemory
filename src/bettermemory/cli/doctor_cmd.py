"""`bettermemory doctor`: the read-only install and runtime self-check.

The checks live in `bettermemory.doctor`; this module is the argparse
registration and the two renderings. Nothing here writes: the command
creates no config, no store and no daemon, so it is safe to run first
whenever memory "does nothing", and safe to run from a monitoring job.
The one exception is `--clear-orphan-keys`, which deletes the key rings
whose store is gone (`doctor.check_orphan_keys`).
"""

from __future__ import annotations

import argparse
import json
import sys


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``doctor`` subparser on the parent parser."""
    help_text = (
        "Check the install, the config, the store and its key, the daemon, "
        "the client wiring, the disk and the key rings whose store is gone, "
        "read-only, each ok, warn or fail with a one-line fix. Exit 0 when every "
        "check is ok, 1 on any failure, 2 on warnings only. Posture (file modes, "
        "the sandbox) is `bettermemory security audit`'s."
    )
    parser = sub.add_parser("doctor", help=help_text, description=help_text)
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit {checks: [{id, status, summary, remedy, details}], status} "
            "as JSON instead of text."
        ),
    )
    parser.add_argument(
        "--clear-orphan-keys",
        action="store_true",
        help=(
            "Delete the key rings whose store is gone (the orphan_keys check), "
            "each read again under its lock first. A ring whose store cannot be "
            "reached now (a path this user cannot read, a volume not mounted) or "
            "that names no store file is kept."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory doctor``: print the report and
    exit with its status (0 ok, 1 fail, 2 warn)."""
    from ..doctor import exit_code, render_text, report, run_checks

    document = report(run_checks(clear_orphan_keys=args.clear_orphan_keys))
    if args.json:
        sys.stdout.write(json.dumps(document, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(document))
    raise SystemExit(exit_code(document["status"]))


__all__ = ["add_subparser", "run"]
