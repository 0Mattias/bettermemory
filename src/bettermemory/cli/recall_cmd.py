"""`bettermemory recall review` and `bettermemory recall grade`: draw what
the recall surface showed for the owner to grade, and record the grades."""

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
    """Register the ``recall`` subparser and its two commands."""
    help_text = (
        "The recall surface's grading surface. `recall review` draws prompt "
        "recall deliveries, session briefs and silent prompts from a window "
        "and shows each one's pointers and the prompt it answered; `recall "
        "grade` records the owner's grade of a sample's pointers as a "
        "recall_grade event, which `bettermemory eval` counts."
    )
    parser = sub.add_parser("recall", help=help_text, description=help_text)
    recall_sub = parser.add_subparsers(dest="recall_cmd")

    review_text = (
        "Draw samples to grade: prompt recall deliveries (12), session "
        "briefs (4), and silent prompts (4), turns the recall delivered "
        "nothing on before the model retrieved a project memory itself. "
        "Samples already graded are not drawn again. Each prompt is read "
        "from the session's transcript under ~/.claude/projects at review "
        "time and never stored."
    )
    review = recall_sub.add_parser("review", help=review_text, description=review_text)
    review.add_argument(
        "--since",
        default="7d",
        help=(
            "Window start: an ISO-8601 UTC timestamp (2026-10-01T00:00:00Z), "
            "a relative span ('Ns'/'Nm'/'Nh'/'Nd') or 'all'. Default: 7d."
        ),
    )
    review.add_argument(
        "--until",
        default=None,
        help="Window end, in the same forms as --since. Default: now.",
    )
    review.add_argument(
        "--deliveries", type=int, default=12, help="Deliveries to draw. Default: 12."
    )
    review.add_argument(
        "--briefs", type=int, default=4, help="Briefs to draw. Default: 4."
    )
    review.add_argument(
        "--silent", type=int, default=4, help="Silent prompts to draw. Default: 4."
    )
    review.add_argument(
        "--seed", type=int, default=0, help="The draw's random seed. Default: 0."
    )
    review.add_argument(
        "--transcripts",
        default=None,
        metavar="DIR",
        help="The transcripts directory. Default: ~/.claude/projects.",
    )
    review.add_argument("--json", action="store_true", help="Emit JSON.")

    grade_text = (
        "Record the grade of one sample's pointers as a recall_grade event: "
        "each id relevant or not. For a silent prompt, relevant means the "
        "recall should have pointed at it. A later grade of an id replaces "
        "an earlier one."
    )
    grade = recall_sub.add_parser("grade", help=grade_text, description=grade_text)
    grade.add_argument("sample_id", help="The sample's id, as the review printed it.")
    grade.add_argument(
        "--relevant", nargs="+", default=[], metavar="ID", help="Relevant ids."
    )
    grade.add_argument(
        "--irrelevant", nargs="+", default=[], metavar="ID", help="Irrelevant ids."
    )
    grade.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory recall ...``."""
    if args.recall_cmd is None:
        sub_parser.print_help()
        raise SystemExit(2)
    if args.recall_cmd == "review":
        _cli_review(args, parser=sub_parser)
        return
    if args.recall_cmd == "grade":
        _cli_grade(args, parser=sub_parser)
        return
    sub_parser.error(f"unknown recall subcommand: {args.recall_cmd!r}")


def _cli_review(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from ..eval import parse_window_bound, resolve_window
    from ..recall_counters import STRATUM_BRIEF, STRATUM_DELIVERY, STRATUM_SILENT
    from ..recall_review import (
        Transcripts,
        build_samples,
        candidates,
        default_transcripts_dir,
        draw,
        graded_samples,
        render_samples_text,
    )
    from ..time_utils import isoformat_utc_optional
    from ._common import cli_context

    sizes = {
        STRATUM_DELIVERY: args.deliveries,
        STRATUM_BRIEF: args.briefs,
        STRATUM_SILENT: args.silent,
    }
    if any(size < 0 for size in sizes.values()):
        parser.error("--deliveries, --briefs and --silent must not be negative")
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
    memories = {m.id: m for m in ctx.store.load_all()}
    population = candidates(events, start=start, end=end, memories=memories)
    graded = graded_samples(events)
    drawn = draw(population, sizes=sizes, seed=args.seed, exclude=graded)
    root = (
        Path(args.transcripts).expanduser()
        if args.transcripts
        else default_transcripts_dir()
    )
    samples = build_samples(drawn, memories=memories, transcripts=Transcripts(root))
    doc: dict[str, Any] = {
        "window": {
            "since": isoformat_utc_optional(start),
            "until": isoformat_utc_optional(end),
        },
        "population": {
            stratum: len([c for c in pool if c.sample_id not in graded])
            for stratum, pool in population.items()
        },
        "requested": sizes,
        "seed": args.seed,
        "samples": samples,
    }
    if args.json:
        sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(render_samples_text(doc))


def _cli_grade(args: argparse.Namespace, *, parser: argparse.ArgumentParser) -> None:
    from ..recall import RULE
    from ..recall_review import find_candidate
    from ..time_utils import isoformat_utc
    from ._common import cli_context, cli_recorder

    relevant = list(dict.fromkeys(args.relevant))
    irrelevant = list(dict.fromkeys(args.irrelevant))
    if not relevant and not irrelevant:
        parser.error("name at least one id with --relevant or --irrelevant")
    both = sorted(set(relevant) & set(irrelevant))
    if both:
        parser.error(f"graded both relevant and irrelevant: {', '.join(both)}")
    ctx = cli_context()
    if not ctx.config.telemetry.enabled:
        parser.error(
            "telemetry is off in this store's config, so a grade has nowhere to go"
        )
    memories = {m.id: m for m in ctx.store.load_all()}
    candidate = find_candidate(
        ctx.store.iter_events(), args.sample_id, memories=memories
    )
    if candidate is None:
        sys.stderr.write(
            f"bettermemory recall grade: no recall sample has id {args.sample_id!r}\n"
        )
        raise SystemExit(1)
    unknown = [i for i in relevant + irrelevant if i not in candidate.ids]
    if unknown:
        sys.stderr.write(
            f"bettermemory recall grade: sample {candidate.sample_id} did not "
            f"show {', '.join(unknown)}\n"
        )
        raise SystemExit(1)
    fields: dict[str, Any] = {
        "sample_id": candidate.sample_id,
        "stratum": candidate.stratum,
        "sample_ts": isoformat_utc(candidate.ts),
        "harness": candidate.harness,
        "segment": candidate.segment,
        "relevant_ids": relevant,
        "irrelevant_ids": irrelevant,
        "rule": RULE,
    }
    recorder = cli_recorder(ctx, attribution="cli_recall_grade")
    recorder.record("recall_grade", **fields)
    if args.json:
        sys.stdout.write(json.dumps(fields, indent=2) + "\n")
    else:
        sys.stdout.write(
            f"graded {candidate.sample_id}: {len(relevant)} relevant, "
            f"{len(irrelevant)} not\n"
        )
