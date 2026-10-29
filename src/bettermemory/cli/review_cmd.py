"""`bettermemory review samples` and `bettermemory review grade`: draw
reviewed writes for the owner to grade, and record the grades."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register the ``review`` subparser and its two commands."""
    help_text = (
        "The write review's grading surface. `review samples` draws "
        "reviewed writes from a window and shows each one's related set, "
        "answers and action; `review grade` records the owner's grade of "
        "one as a review_grade event, which `bettermemory eval` counts."
    )
    parser = sub.add_parser("review", help=help_text, description=help_text)
    review_sub = parser.add_subparsers(dest="review_cmd")

    samples_text = (
        "Draw reviewed writes to grade: every refusal, link and filing up to "
        "half the sample, then pre-answered writes up to 10 where that many "
        "exist, then random draws. A committed write shows its body from the "
        "store; a secret refusal shows kinds only; another refusal shows the "
        "body from the transcripts under ~/.claude/projects, found by "
        "review_id, and a refusal from any other host shows no body."
    )
    samples = review_sub.add_parser(
        "samples", help=samples_text, description=samples_text
    )
    samples.add_argument(
        "--since",
        default="30d",
        help=(
            "Window start: an ISO-8601 UTC timestamp (2026-10-01T00:00:00Z), "
            "a relative span ('Ns'/'Nm'/'Nh'/'Nd') or 'all'. Default: 30d."
        ),
    )
    samples.add_argument(
        "--until",
        default=None,
        help="Window end, in the same forms as --since. Default: now.",
    )
    samples.add_argument(
        "--n", type=int, default=40, help="How many samples to draw. Default: 40."
    )
    samples.add_argument(
        "--seed", type=int, default=0, help="The draw's random seed. Default: 0."
    )
    samples.add_argument(
        "--transcripts",
        default=None,
        metavar="DIR",
        help=(
            "The transcripts directory. Default: projects in Claude Code's"
            " config directory (~/.claude/projects)."
        ),
    )
    samples.add_argument("--json", action="store_true", help="Emit JSON.")

    grade_text = (
        "Record the grade of one sampled write as a review_grade event: "
        "whether the answers were right and whether the action was right."
    )
    grade = review_sub.add_parser("grade", help=grade_text, description=grade_text)
    grade.add_argument("review_id", help="The sample's review_id.")
    grade.add_argument("--answers", choices=["right", "wrong"], required=True)
    grade.add_argument("--action", choices=["right", "wrong"], required=True)
    grade.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory review ...``."""
    if args.review_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if args.review_cmd == "samples":
        _cli_samples(args, parser=sub_parser)
        return
    if args.review_cmd == "grade":
        _cli_grade(args, parser=sub_parser)
        return
    sub_parser.error(f"unknown review subcommand: {args.review_cmd!r}")


def _cli_samples(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from ..eval import parse_window_bound, resolve_window
    from ..review_samples import (
        answered_chains,
        build_samples,
        default_transcripts_dir,
        draw,
        render_samples_text,
    )
    from ..time_utils import isoformat_utc_optional
    from ._common import cli_context

    if args.n < 1:
        parser.error("--n must be at least 1")
    now = datetime.now(timezone.utc)
    try:
        since = parse_window_bound(args.since, flag="--since")
        until = parse_window_bound(args.until, flag="--until")
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
        return  # pragma: no cover - parser.error raises SystemExit
    start, end = resolve_window(since, until, now=now)
    if start is not None and end is not None and end <= start:
        parser.error("--until must be later than --since")

    ctx = cli_context()
    events = list(ctx.store.iter_events())
    population = answered_chains(events, start=start, end=end, now=now)
    drawn = draw(population, n=args.n, seed=args.seed)
    transcripts = (
        Path(args.transcripts).expanduser()
        if args.transcripts
        else default_transcripts_dir()
    )
    active = {m.id: m for m in ctx.store.load_all()}
    samples = build_samples(drawn, active=active, transcripts=transcripts)
    strata: dict[str, int] = {}
    for sample in samples:
        strata[sample.stratum] = strata.get(sample.stratum, 0) + 1
    doc: dict[str, Any] = {
        "window": {
            "since": isoformat_utc_optional(start),
            "until": isoformat_utc_optional(end),
        },
        "population": len(population),
        "requested": args.n,
        "seed": args.seed,
        "strata": strata,
        "samples": [s.to_dict() for s in samples],
    }
    if args.json:
        sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(render_samples_text(doc))


def _cli_grade(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from ..review_samples import find_answered_chain
    from ..time_utils import isoformat_utc
    from ._common import cli_context, cli_recorder

    ctx = cli_context()
    if not ctx.config.telemetry.enabled:
        parser.error(
            "telemetry is off in this store's config, so a grade has nowhere to go"
        )
    now = datetime.now(timezone.utc)
    chain = find_answered_chain(ctx.store.iter_events(), args.review_id, now=now)
    if chain is None or chain.decided is None:
        sys.stderr.write(
            f"bettermemory review grade: no answered review chain has review_id "
            f"{args.review_id!r}\n"
        )
        raise SystemExit(1)
    decided = chain.decided
    right_answers = args.answers == "right"
    right_action = args.action == "right"
    fields: dict[str, Any] = {
        "review_id": chain.key,
        "sample_ts": isoformat_utc(decided.ts),
        "client": chain.client,
        "pre_answered": chain.pre_answered,
        "zero_attestation": decided.zero_attestation,
        "action": decided.action,
        "right_answers": right_answers,
        "right_action": right_action,
        "right_both": right_answers and right_action,
    }
    recorder = cli_recorder(ctx, attribution="cli_review_grade")
    recorder.record("review_grade", **fields)
    if args.json:
        sys.stdout.write(json.dumps(fields, indent=2) + "\n")
    else:
        sys.stdout.write(
            f"graded {chain.key}: answers {args.answers}, action {args.action}\n"
        )
