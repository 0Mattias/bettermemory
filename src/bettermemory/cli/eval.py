"""`bettermemory eval` — compute and render the memory-effectiveness rates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ._common import cli_context


def add_subparser(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> argparse.ArgumentParser:
    """Register the ``eval`` subparser on the parent parser."""
    help_text = (
        "Compute the three memory-effectiveness rates "
        "(memory_helped_rate, endorsement_rate, silent_miss_rate) "
        "from the event log + active store. Methodology in "
        "docs/eval.md."
    )
    parser = sub.add_parser("eval", help=help_text, description=help_text)
    parser.add_argument(
        "--since",
        type=str,
        default="30d",
        help=(
            "Window for events to include. Accepts 'Ns'/'Nm'/'Nh'/'Nd' "
            "or 'all'. Default: 30d, mirroring the verification-staleness "
            "default so the eval window and freshness threshold tell a "
            "consistent story."
        ),
    )
    parser.add_argument(
        "--scope",
        type=str,
        default=None,
        help=(
            "Filter to events that reference memories tagged with this "
            "scope. The silent-miss rate is NOT filtered (it's per-turn, "
            "not per-memory) and stays global regardless."
        ),
    )
    parser.add_argument(
        "--min-retrievals",
        type=int,
        default=None,
        help=(
            "Floor for cold-endorsement row inclusion. Default 5; below "
            "this, the absence of explicit endorsement is treated as "
            "insufficient signal rather than debt."
        ),
    )
    parser.add_argument(
        "--silent-miss-limit",
        type=int,
        default=20,
        help=(
            "How many recent silent-miss events to surface inline. "
            "The full series stays in the event log. Default: 20."
        ),
    )
    parser.add_argument(
        "--tool-usage",
        action="store_true",
        help=(
            "Switch to the per-MCP-tool call-count rollup instead of the "
            "rate trio. One row per tool with absolute counts and share "
            "of total, plus a tally of any unmapped event kinds. Use to "
            "answer 'which tools is the model actually reaching for?' "
            "without running compute_health. Honours `--since` and "
            "`--json`; ignores `--scope`, `--min-retrievals`, and "
            "`--silent-miss-limit` (those are rate-mode knobs)."
        ),
    )
    parser.add_argument(
        "--threshold-sweep",
        action="store_true",
        help=(
            "Switch to a counterfactual replay of logged search_miss "
            "events against alternative threshold rules. Reports how "
            "many misses each rule (v1_top1_high, v2_top1_high_score_50, "
            "v3_top1_high_dominant, v4_top1_high_strict_combined) would "
            "have flagged. Honours `--since` and `--json`; ignores "
            "the rate-mode knobs. Useful for calibrating whether v1 is "
            "over-firing on borderline hits — see docs/eval.md for the "
            "caveat about absolute-vs-relative miss rates under "
            "differently-strict rules."
        ),
    )
    parser.add_argument(
        "--widening-preview",
        action="store_true",
        help=(
            "Switch to a replay of candidate LOOSER threshold rules over "
            "the turn_audited stream (3.14+ events carry per-turn "
            "top_hits with the raw coverage features). Reports how many "
            "audited turns each widening candidate (w1_top1_v2_high, "
            "w2_top1_v2_high_from_medium — the shadow relevance label) "
            "would flag beyond the replayed v1 baseline. The "
            "forward-looking counterpart to --threshold-sweep, which "
            "can only compare rules at least as strict as v1. Honours "
            "`--since` and `--json`; add --detail for the per-turn "
            "labeling surface."
        ),
    )
    parser.add_argument(
        "--usage-replay",
        action="store_true",
        help=(
            "Switch to the usage-signal flip-bar measurement surface: "
            "aggregate the per-turn usage-toggle captures "
            "(turn_audited/prompt_recall events carrying usage_active/"
            "usage_toggles) over the window, judge each changed top-1 "
            "under the pinned improvement rule, check the "
            "outcome_demotion invariant, and print the density "
            "preconditions. Measurements only — read the output against "
            "the declared bars in docs/ROADMAP.md. Honours `--since` "
            "and `--json`; ignores the rate-mode knobs."
        ),
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help=(
            "With --widening-preview: dump each flagged turn's evidence "
            "(redacted probe-query preview, top-hit coverage features, "
            "both relevance labels, the hit's memory summary) plus a "
            "per-memory concentration rollup, instead of just counts. "
            "This is the precision-labeling surface the relevance-v2 "
            "flip decision reads. Errors when used without "
            "--widening-preview."
        ),
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "Switch to a publishable, self-contained markdown report: the "
            "rate trio over the `--since` window AND all time side by side "
            "(Wilson 95%% CIs), per-model audit telemetry, the "
            "threshold-sweep counterfactual, and the tool-usage top 10, "
            "plus a reading guide and methodology footer. Aggregates only "
            "by tested contract — no memory bodies, queries, scopes, "
            "paths, or session ids ever land in the output, so it is safe "
            "to publish as-is. Honours `--since` (the window column); "
            "errors when combined with `--json` or any other mode flag. "
            "`--output FILE` writes it to a file instead of stdout."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "With --report: write the markdown to FILE instead of stdout. "
            "Errors when used without --report."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    return parser


def run(
    args: argparse.Namespace,
    *,
    sub_parser: argparse.ArgumentParser,
) -> None:
    """Dispatch handler for ``bettermemory eval``.

    ``sub_parser`` is forwarded into ``_cli_eval`` so ``parser.error(...)``
    on flag-validation failure points at the eval subparser — matches
    the pre-extraction ``parser=eval_parser`` argument.
    """
    _cli_eval(
        since_spec=args.since,
        scope=args.scope,
        endorsement_min_retrievals=args.min_retrievals,
        silent_miss_limit=args.silent_miss_limit,
        json_out=args.json,
        tool_usage=args.tool_usage,
        threshold_sweep=args.threshold_sweep,
        widening_preview=args.widening_preview,
        widening_detail=args.detail,
        usage_replay=args.usage_replay,
        report=args.report,
        output=args.output,
        parser=sub_parser,
    )


def _cli_eval(
    *,
    since_spec: str,
    scope: str | None,
    endorsement_min_retrievals: int | None,
    silent_miss_limit: int,
    json_out: bool,
    tool_usage: bool,
    threshold_sweep: bool,
    widening_preview: bool,
    widening_detail: bool = False,
    usage_replay: bool = False,
    report: bool = False,
    output: str | None = None,
    parser: Any,
) -> None:
    """`bettermemory eval` — compute and render the effectiveness report.

    Default mode reports the three effectiveness rates
    (memory_helped_rate, endorsement_rate, silent_miss_rate). Five
    alternative modes:

    - ``--tool-usage``: per-MCP-tool call-count rollup. Answers
      "which tools is the model actually reaching for?".
    - ``--threshold-sweep``: counterfactual replay of logged
      `search_miss` events against alternative STRICTER threshold
      rules. Answers "is the current v1_top1_high rule over-firing?".
    - ``--widening-preview``: replay of candidate LOOSER rules over
      the `turn_audited` stream (needs 3.14+ per-turn top_hits).
      Answers "what would a widened rule flag that v1 misses?".
    - ``--usage-replay``: aggregate the per-turn usage-toggle captures
      for the usage-signal flip bars (docs/ROADMAP.md). Answers "when
      a usage flag changed a top-1, was the flag's pick better?".
    - ``--report``: one publishable markdown document composing the
      rate trio (window vs all-time), per-model telemetry, the
      threshold sweep, and the tool-usage top 10. Aggregates only —
      the leak-free property is a tested contract. Markdown-only
      (``--json`` errors) and exclusive with every other mode flag;
      ``--output FILE`` redirects it to a file.

    The pure compute layer lives in ``bettermemory.eval`` so tests
    can drive every mode directly with synthetic events. The
    alternative modes are pairwise mutually exclusive; if more than
    one flag is set the parser exits with an error before this
    function runs.
    """
    import json as _json

    from ..eval import (
        DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
        compute_eval,
        compute_report,
        compute_threshold_sweep,
        compute_tool_usage,
        compute_usage_replay,
        compute_widening_detail,
        compute_widening_preview,
        parse_since,
        render_report_markdown,
        render_text,
        render_threshold_sweep_text,
        render_tool_usage_text,
        render_usage_replay_text,
        render_widening_detail_text,
        render_widening_preview_text,
    )
    from ..events import iter_all_events

    if report and (
        tool_usage
        or threshold_sweep
        or widening_preview
        or widening_detail
        or usage_replay
    ):
        # Same clean-exit style as the --detail guard below: message +
        # SystemExit(2) via parser.error. The report already composes
        # the rate trio, the threshold sweep, and the tool-usage rollup,
        # so combining it with a single-rollup mode flag is a conflict,
        # not a refinement.
        parser.error(
            "--report cannot be combined with --tool-usage, "
            "--threshold-sweep, --widening-preview, --usage-replay, or "
            "--detail (the report already composes the relevant rollups)"
        )
        return  # pragma: no cover — parser.error raises SystemExit

    if report and json_out:
        parser.error(
            "--report emits markdown, not JSON; --json only applies to the other modes"
        )
        return  # pragma: no cover — parser.error raises SystemExit

    if output is not None and not report:
        parser.error("--output only applies to --report")
        return  # pragma: no cover — parser.error raises SystemExit

    if sum((tool_usage, threshold_sweep, widening_preview, usage_replay)) > 1:
        parser.error(
            "--tool-usage, --threshold-sweep, --widening-preview, and "
            "--usage-replay are mutually exclusive"
        )
        return  # pragma: no cover — parser.error raises SystemExit

    if widening_detail and not widening_preview:
        parser.error("--detail only applies to --widening-preview")
        return  # pragma: no cover — parser.error raises SystemExit

    try:
        since = parse_since(since_spec)
    except (ValueError, OverflowError) as exc:
        # `parse_since` already maps an out-of-range value's OverflowError
        # to ValueError; the OverflowError arm here is defence-in-depth so
        # a future change to the parser can't leak a raw traceback past
        # this clean-error path.
        parser.error(str(exc))
        return  # pragma: no cover — parser.error raises SystemExit

    ctx = cli_context()
    directory = ctx.directory

    if report:
        # Report mode ignores the rate-mode knobs (`--scope`,
        # `--min-retrievals`, `--silent-miss-limit`) the same way the
        # other alternative modes do — no parser.error, so shell loops
        # don't have to strip them per invocation. Scope filtering in
        # particular is deliberately unsupported: the report never
        # prints scope names, and a scoped rate column would be
        # unlabelable without leaking the scope.
        report_store = ctx.store
        doc = compute_report(
            memories=report_store.load_all(),
            events=iter_all_events(directory),
            since=since,
            # Same tombstone enumeration rate-mode uses, so the report's
            # silent-miss numbers agree with `bettermemory eval` and
            # `memory_health` over the same log.
            tombstoned_ids={t.id for t in report_store.load_tombstones()},
        )
        markdown = render_report_markdown(doc)
        if output is not None:
            try:
                Path(output).write_text(markdown, encoding="utf-8")
            except OSError as exc:
                parser.error(f"--output: cannot write {output!r}: {exc}")
                return  # pragma: no cover — parser.error raises SystemExit
        else:
            sys.stdout.write(markdown)
        return

    if tool_usage:
        # Tool-usage mode ignores `--scope`, `--min-retrievals`, and
        # `--silent-miss-limit` — they're rate-mode knobs. We don't
        # call parser.error on them so a user piping the same args
        # into both modes (a reasonable shell loop) doesn't have to
        # strip the rate-mode flags before each invocation.
        usage_report = compute_tool_usage(
            events=iter_all_events(directory),
            since=since,
        )
        if json_out:
            sys.stdout.write(_json.dumps(usage_report.to_dict(), indent=2) + "\n")
        else:
            sys.stdout.write(render_tool_usage_text(usage_report))
        return

    if threshold_sweep:
        sweep_report = compute_threshold_sweep(
            events=iter_all_events(directory),
            since=since,
        )
        if json_out:
            sys.stdout.write(_json.dumps(sweep_report.to_dict(), indent=2) + "\n")
        else:
            sys.stdout.write(render_threshold_sweep_text(sweep_report))
        return

    if usage_replay:
        # Same knob policy as the sibling modes: the rate-mode knobs are
        # ignored, not rejected. The store join feeds only the
        # corroboration-liveness counts.
        replay_store = ctx.store
        replay_report = compute_usage_replay(
            events=iter_all_events(directory),
            memories=replay_store.load_all(),
            since=since,
        )
        if json_out:
            sys.stdout.write(_json.dumps(replay_report.to_dict(), indent=2) + "\n")
        else:
            sys.stdout.write(render_usage_replay_text(replay_report))
        return

    if widening_preview:
        if widening_detail:
            # The detail lane joins top-hit ids against the store for
            # summaries the same way rate-mode does (active memories +
            # tombstone log), so a flagged hit whose memory was since
            # removed reads "tombstoned" rather than resurfacing.
            detail_store = ctx.store
            detail_report = compute_widening_detail(
                events=iter_all_events(directory),
                since=since,
                memories=detail_store.load_all(),
                tombstoned_ids={t.id for t in detail_store.load_tombstones()},
            )
            if json_out:
                sys.stdout.write(_json.dumps(detail_report.to_dict(), indent=2) + "\n")
            else:
                sys.stdout.write(render_widening_detail_text(detail_report))
            return
        preview_report = compute_widening_preview(
            events=iter_all_events(directory),
            since=since,
        )
        if json_out:
            sys.stdout.write(_json.dumps(preview_report.to_dict(), indent=2) + "\n")
        else:
            sys.stdout.write(render_widening_preview_text(preview_report))
        return

    store = ctx.store

    floor = (
        endorsement_min_retrievals
        if endorsement_min_retrievals is not None
        else DEFAULT_ENDORSEMENT_MIN_RETRIEVALS
    )

    # `rate_report`, not `report` — that name is taken by the --report
    # mode flag in this scope since the report mode landed.
    rate_report = compute_eval(
        memories=store.load_all(),
        events=iter_all_events(directory),
        since=since,
        scope=scope,
        endorsement_min_retrievals=floor,
        silent_miss_limit=silent_miss_limit,
        # Same enumeration `health.report_for_directory` and the
        # scope-overview handler feed `compute_health`, so the eval
        # CLI's silent-miss numerator applies the identical tombstone
        # filter (health's `_silent_miss_stats` filter #2) — a miss
        # whose top-hit memory was since removed is no longer
        # actionable and must not keep the two surfaces disagreeing.
        tombstoned_ids={t.id for t in store.load_tombstones()},
    )
    if json_out:
        sys.stdout.write(_json.dumps(rate_report.to_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(render_text(rate_report))
