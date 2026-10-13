"""`bettermemory capture review` and `bettermemory capture undo`
(`bettermemory.capture_review`).

Capture itself happens in the session: the Stop hook asks the chat model
to save a stretch, and the model writes with `memory_write`. These read
the batches that leaves behind, and take one back. Neither calls a model.

Named `capture_cmd` rather than `capture` for the reason
`cli/__init__.py` gives for `health_cmd`: `bettermemory/capture.py` is a
module of its own, and two `capture.py` files would be the basename
collision that rename removed.
"""

from __future__ import annotations

import argparse
import json
import sys


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``capture`` subparser and its two commands."""
    help_text = (
        "Session capture's batches. `capture review` lists each batch with "
        "its outcome, how many memories it saved, its refusals by rule and "
        "what was undone; `capture undo BATCH` tombstones a batch's "
        "memories (dry-run unless --apply). Calls no model."
    )
    parser = sub.add_parser("capture", help=help_text, description=help_text)
    capture_sub = parser.add_subparsers(dest="capture_cmd")
    review = capture_sub.add_parser(
        "review", help="List capture batches.", description="List capture batches."
    )
    review.add_argument("--json", action="store_true", help="Emit JSON.")
    undo_text = (
        "Tombstone the memories one capture batch saved and record the undo, "
        "so the same body is not captured again; `tombstones restore` puts "
        "one back. Refuses while a member was since updated, verified, "
        "linked to from outside the batch or filed in a conflict, unless "
        "--skip-changed, which leaves those and reports them."
    )
    undo = capture_sub.add_parser("undo", help=undo_text, description=undo_text)
    undo.add_argument("batch", help="The batch id, as `capture review` lists it.")
    undo.add_argument("--apply", action="store_true", help="Commit the undo.")
    undo.add_argument(
        "--skip-changed",
        action="store_true",
        help="Undo the unchanged members and leave the changed ones.",
    )
    undo.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory capture ...``."""
    from .. import capture_review as cr
    from ._common import cli_context

    if args.capture_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    store = cli_context().store
    if args.capture_cmd == "review":
        doc = cr.review(store)
        sys.stdout.write(
            json.dumps(doc, indent=2) + "\n" if args.json else cr.render_review(doc)
        )
        return
    report = cr.plan_undo(store, args.batch, skip_changed=args.skip_changed)
    if args.apply and report.refusal_reason is None:
        cr.apply_undo(store, report)
    sys.stdout.write(
        json.dumps(report.to_dict(), indent=2) + "\n"
        if args.json
        else cr.render_undo(report)
    )
    if report.refusal_reason is not None:
        raise SystemExit(1)
