"""`bettermemory capture review`, `capture undo`, `capture sample` and
`capture grade` (`bettermemory.capture_review`).

Capture itself happens in the session: the Stop hook asks the chat model
to save a stretch, and the model writes with `memory_write`. These read
the batches that leaves behind, take one back, and draw and grade its
memories against the bar. None calls a model.

Named `capture_cmd` rather than `capture` for the reason
`cli/__init__.py` gives for `health_cmd`: `bettermemory/capture.py` is a
module of its own, and two `capture.py` files would be the basename
collision that rename removed.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import Store


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``capture`` subparser and its four commands."""
    help_text = (
        "Session capture's batches. `capture review` lists each batch with "
        "its outcome, how many memories it saved, its refusals by rule and "
        "what was undone; `capture undo BATCH` tombstones a batch's "
        "memories (dry-run unless --apply); `capture sample` and `capture "
        "grade` draw captured memories and record the owner's grade of "
        "each against the ten rules. Calls no model."
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
        "linked to from outside the batch or filed in a conflict its own "
        "write did not file, unless --skip-changed, which leaves those and "
        "reports them."
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
    sample_text = (
        "Draw captured memories still active and not undone, each with its "
        "id and body, to grade with `capture grade`. --tally prints instead "
        "how many were graded and how many met all ten rules, per ISO week."
    )
    sample = capture_sub.add_parser("sample", help=sample_text, description=sample_text)
    sample.add_argument(
        "--n", type=int, default=20, help="How many to draw. Default: 20."
    )
    sample.add_argument(
        "--seed", type=int, default=0, help="The draw's random seed. Default: 0."
    )
    sample.add_argument(
        "--tally", action="store_true", help="Print the weekly grade tally."
    )
    sample.add_argument("--json", action="store_true", help="Emit JSON.")
    grade_text = (
        "Record the owner's grade of one captured memory as a capture_grade "
        "event: it meets all ten rules, or it fails, with the rules it "
        "breaks by number."
    )
    grade = capture_sub.add_parser("grade", help=grade_text, description=grade_text)
    grade.add_argument("memory_id", help="The memory id, as `capture sample` shows.")
    verdict = grade.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--meets", action="store_true", help="Meets all ten rules.")
    verdict.add_argument("--fails", action="store_true", help="Breaks a rule.")
    grade.add_argument(
        "--rule",
        type=int,
        action="append",
        choices=range(1, 11),
        metavar="RULE",
        help="A rule it breaks, 1 to 10 (with --fails; repeatable).",
    )
    grade.add_argument("--note", default=None, help="A note to keep with the grade.")
    grade.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory capture ...``."""
    from .. import capture_review as cr
    from ._common import cli_context

    if args.capture_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if args.capture_cmd == "grade":
        _cli_grade(args, parser=sub_parser)
        return
    if args.capture_cmd == "sample" and args.n < 1:
        sub_parser.error("--n must be at least 1")
    store = cli_context().store
    if args.capture_cmd == "review":
        doc = cr.review(store)
        sys.stdout.write(
            json.dumps(doc, indent=2) + "\n" if args.json else cr.render_review(doc)
        )
        return
    if args.capture_cmd == "sample":
        _cli_sample(args, store)
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


def _cli_sample(args: argparse.Namespace, store: Store) -> None:
    from .. import capture_review as cr

    if args.tally:
        tally = cr.grade_tally(store.iter_events())
        sys.stdout.write(
            json.dumps(tally, indent=2) + "\n" if args.json else cr.render_tally(tally)
        )
        return
    population = cr.live_members(store)
    drawn = cr.draw_members(population, n=args.n, seed=args.seed)
    doc = {
        "population": len(population),
        "requested": args.n,
        "seed": args.seed,
        "samples": [
            {"memory_id": m.memory_id, "batch": m.batch, "body": memory.body}
            for m, memory in drawn
        ],
    }
    if args.json:
        sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return
    lines = [f"Captured memories: {len(drawn)} of {len(population)}"]
    for sample in doc["samples"]:
        lines += [f"  {sample['memory_id']}  batch {sample['batch']}"]
        lines += [f"    {line}" for line in sample["body"].splitlines()]
    sys.stdout.write("\n".join(lines) + "\n")


def _cli_grade(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from .. import capture as cap
    from ._common import cli_context, cli_recorder

    if args.rule and not args.fails:
        parser.error("--rule goes with --fails")
    ctx = cli_context()
    if not ctx.config.telemetry.enabled:
        parser.error(
            "telemetry is off in this store's config, so a grade has nowhere to go"
        )
    member = next(
        (m for m in cap.members(ctx.store) if m.memory_id == args.memory_id), None
    )
    if member is None:
        sys.stderr.write(
            f"bettermemory capture grade: {args.memory_id!r} is not a captured memory\n"
        )
        raise SystemExit(1)
    fields: dict[str, Any] = {
        "memory_id": member.memory_id,
        "batch": member.batch,
        "meets": bool(args.meets),
        "rules": sorted(set(args.rule or [])),
        "note": args.note,
    }
    cli_recorder(ctx, attribution="cli_capture_grade").record("capture_grade", **fields)
    if args.json:
        sys.stdout.write(json.dumps(fields, indent=2) + "\n")
    else:
        verdict = "meets all ten rules" if args.meets else "fails"
        rules = (
            f" (rules {', '.join(map(str, fields['rules']))})"
            if fields["rules"]
            else ""
        )
        sys.stdout.write(f"graded {member.memory_id}: {verdict}{rules}\n")
