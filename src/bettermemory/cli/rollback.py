"""`bettermemory rollback` — targeted, reversible removal by actor."""

from __future__ import annotations

import argparse
import sys

from ._common import cli_context, parse_iso_cutoff


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``rollback`` subparser on the parent parser."""
    help_text = (
        "Remove the memories one actor wrote, leaving every other "
        "actor's in place. Dry-run by default; --apply --yes commits. "
        "Every removal is a tombstone, so `tombstones restore` puts a "
        "record back."
    )
    parser = sub.add_parser("rollback", help=help_text, description=help_text)
    parser.add_argument(
        "--by-actor",
        metavar="CLIENT",
        required=True,
        help=(
            "Select records whose writer declared this client (exact, "
            "case-sensitive — the same rule search and memory_list "
            "filter on). Required: there is no default, because a "
            "rollback with no selector would mean the whole store. "
            "Records that declared NO client are never selected — an "
            "absent actor is not evidence of some other writer — and "
            "the report says how many were passed over for that reason."
        ),
    )
    parser.add_argument(
        "--since",
        metavar="ISO_TS",
        default=None,
        help=(
            "Only records CREATED at or after this timestamp, e.g. "
            "'2026-09-01T00:00:00Z'. Filters `created` rather than "
            "`updated`, because the actor and the creation timestamp "
            "are stamped by the same write event. Requires an explicit "
            "UTC offset or trailing Z. Omit to select everything the "
            "actor ever wrote."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually remove the selected records. Requires --yes as "
            "well; without it nothing is committed and the command "
            "exits non-zero."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Confirm a batch removal. Required alongside --apply, "
            "matching the accept-gate `consolidate --llm --apply` "
            "carries: a bulk removal must not commit silently."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    return parser


def run(args: argparse.Namespace) -> None:
    """Plan (and with --apply --yes, commit) a by-actor rollback."""
    from ..rollback import apply_rollback, plan_rollback, render_json, render_text
    from ..session import SessionState

    ctx = cli_context()
    store = ctx.store

    since = None
    if args.since is not None:
        # A far-future `--since` WARNS rather than refusing: unlike
        # `--acknowledge-misses-before`, where a typo'd century writes a
        # marker that hides the log indefinitely, a far-future window
        # here simply selects nothing — visible in the very next line of
        # output, and destructive of nothing.
        since, _ = parse_iso_cutoff(
            args.since, flag="rollback --since", far_future="warn"
        )

    report = plan_rollback(
        store.load_all(),
        client=args.by_actor,
        since=since,
    )
    report.applied = bool(args.apply)

    # The accept gate. `--apply` alone is deliberately not enough: this
    # is the widest-blast-radius command in the tool — one flag value
    # can select every record an agent ever wrote, with no per-record
    # justification beyond authorship — so it takes the strictest
    # posture already in the tree rather than a weaker one. Exiting
    # non-zero (where `consolidate --llm` merely warns) is the one place
    # this goes further: a script that asked to commit and did not must
    # not read as success.
    if args.apply and not args.yes:
        report.refusal_reason = (
            "--apply requires --yes as well. This would have removed "
            f"{len(report.candidates)} record(s) written by "
            f"client={report.client}. Re-run with --apply --yes to "
            "confirm."
        )
        sys.stdout.write(render_json(report) if args.json else render_text(report))
        raise SystemExit(1)

    if args.apply and args.yes:
        apply_rollback(
            store,
            report,
            session_id=SessionState().session_id,
        )

    sys.stdout.write(render_json(report) if args.json else render_text(report))
    if report.failures:
        raise SystemExit(1)
