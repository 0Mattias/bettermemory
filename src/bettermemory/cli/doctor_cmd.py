"""`bettermemory doctor`: the read-only install and runtime self-check.

The checks live in `bettermemory.doctor`; this module is the argparse
registration and the two renderings. Nothing here writes: the command
creates no config, no store and no daemon, so it is safe to run first
whenever memory "does nothing", and safe to run from a monitoring job.
The one exception is `--clear-orphan-keys --yes`, which moves the key
rings whose store is shown gone to the keys directory's quarantine and
deletes nothing (`doctor.check_orphan_keys`); without `--yes` it prints
the plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


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
            "Print the plan for the key rings whose store is shown gone (the "
            "orphan_keys check: no file at the bound path, and the nearest "
            "directory on the way up that is there readable and not empty; a "
            "disk unplugged reads the same); with --yes, move "
            "them to the keys directory's quarantine/<store_id>/, each read "
            "again under its lock first. Nothing is deleted, and `bettermemory "
            "key import` takes a key back. Every other ring is kept and named."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="With --clear-orphan-keys: move the rings the plan names.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Dispatch handler for ``bettermemory doctor``: print the report and
    exit with its status (0 ok, 1 fail, 2 warn)."""
    from ..doctor import exit_code, render_text, report, run_checks

    if args.yes and not args.clear_orphan_keys:
        sys.stderr.write("bettermemory doctor: --yes goes with --clear-orphan-keys\n")
        raise SystemExit(2)
    document = report(
        run_checks(clear_orphan_keys=args.clear_orphan_keys, yes=args.yes)
    )
    if args.json:
        sys.stdout.write(json.dumps(document, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(document))
        sys.stdout.write(_clear_text(document))
    raise SystemExit(exit_code(document["status"]))


def _clear_text(document: dict[str, Any]) -> str:
    """The `--clear-orphan-keys` lines under the report: the rings the
    plan would move and where, or what was moved, kept and not moved.
    Empty without the flag."""
    clear = next(
        (
            check["details"].get("clear")
            for check in document["checks"]
            if check["id"] == "orphan_keys" and isinstance(check.get("details"), dict)
        ),
        None,
    )
    if not clear:
        return ""
    lines = [""]
    if clear["status"] == "planned":
        planned = clear["planned"]
        if not planned:
            return "\nno key ring to move\n"
        lines.append(
            f"would move {len(planned)} key ring(s) to {clear['quarantine']} "
            "(nothing is deleted):"
        )
        lines += [f"  {ring['store_id']}  {ring['reason']}" for ring in planned]
        lines.append("run `bettermemory doctor --clear-orphan-keys --yes` to move them")
    else:
        for ring in clear["moved"]:
            lines.append(f"moved {ring['store_id']} to {ring['to']}")
        for ring in clear["kept"]:
            lines.append(f"kept {ring['store_id']}: {ring['reason']}")
        for ring in clear["failed"]:
            lines.append(f"not moved {ring['store_id']}: {ring['error']}")
        if len(lines) == 1:
            lines.append("no key ring to move")
    return "\n".join(lines) + "\n"


__all__ = ["add_subparser", "run"]
