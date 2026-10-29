"""The eval family's renderers: the CLI text and the publishable report.

Every function here turns a report `eval.py` computed into text: the
plain-text rendering of each mode (the rate trio, the threshold sweep,
the widening preview and its detail, the tool-usage rollup) and the
markdown of `eval --report`, whose content contract the canary test in
tests/test_eval.py pins. The report classes stay in `eval.py` and are
imported here for type checking only, so the renderers' annotations do
not resolve at run time (`typing.get_type_hints` raises on them; nothing
reads them that way); `RateCI` and `EARLIER_RULES` come from
`eval_model.py`. `eval.py` re-exports every public renderer, so
`from bettermemory.eval import render_text` reads as before.

The text renderers print every string they take from the store or the log
(an id, a summary, a scope, a `client_model` stamp, a probe preview, a
rule, detector or reason a row carried) through `history.printable`, as
posture prints its fields, and `_text` passes every finished line through
it once more, as doctor ends its report, so an escape sequence or a line
break in one cannot reach the terminal as one, a field missed included.
The markdown report's one log-sourced cell goes through `_md_escape_cell`,
which escapes it for a table cell.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .eval_model import EARLIER_RULES, RateCI
from .health import _HOOKLESS_REASON
from .history import printable
from .time_utils import isoformat_utc_optional

if TYPE_CHECKING:
    from .eval import (
        EvalReport,
        ReportDocument,
        ThresholdSweepReport,
        ToolUsageReport,
        WideningDetailReport,
        WideningPreviewReport,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(report: EvalReport) -> str:
    """Plain-text rendering for the CLI. Stable across versions — tests
    will pin a representative shape so changes here surface in review."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    scope_part = (
        f" · scope={printable(report.scope_filter)}" if report.scope_filter else ""
    )
    if report.window_end is not None:
        # An absolute --until: name both ends, since "last N" would read
        # back from now.
        start = isoformat_utc_optional(report.window_start) or "the start"
        window = f"{start} to {isoformat_utc_optional(report.window_end)}"
        lines.append(f"bettermemory eval — {window}{scope_part}")
    else:
        lines.append(f"bettermemory eval — last {window}{scope_part}")
    lines.append("─" * 60)
    # Window-scoped, matching the "— last {window}" header above.
    # `total_events_scanned` counts the WHOLE log (marker resolution
    # runs ahead of the window filter), so printing it here published
    # an all-time figure under a windowed label.
    lines.append(f"Events scanned                      {report.events_in_window:>5d}")
    lines.append(
        f"Retrieval occurrences               {report.retrieval_occurrences:>5d}"
    )
    lines.append(f"Applied use events (auto+explicit)  {report.applied_total:>5d}")
    lines.append(f"Turns audited                       {report.turns_audited:>5d}")
    if report.turns_no_signal:
        lines.append(
            f"Turns no-signal (excluded)          {report.turns_no_signal:>5d}"
        )
    if report.repeat_audits:
        lines.append(f"Repeat audits (deduped, excluded)   {report.repeat_audits:>5d}")
    lines.append("")
    lines.append(_format_rate("memory_helped_rate", report.memory_helped_rate))
    lines.append(_format_rate("endorsement_rate ", report.endorsement_rate))
    lines.append(_format_rate("silent_miss_rate ", report.silent_miss_rate))

    if report.by_model:
        lines.append("")
        lines.append("Per-model audit telemetry (Stop-hook `client_model` stamp):")
        for model in sorted(report.by_model):
            counts = report.by_model[model]
            lines.append(
                f"  {printable(model):<28s} audited={counts.get('audited', 0):<5d} "
                f"misses={counts.get('misses', 0):<4d} "
                f"no_signal={counts.get('no_signal', 0)}"
            )

    if report.cold_endorsement_memories_rows or report.cold_endorsement_memories_total:
        lines.append("")
        lines.append(
            f"Cold-endorsement memories "
            f"(retrievals ≥ {report.endorsement_min_retrievals}, 0 explicit applied): "
            f"{report.cold_endorsement_memories_total}"
        )
        for row in report.cold_endorsement_memories_rows[:10]:
            scopes = printable(",".join(row.scopes)) or "—"
            lines.append(
                f"  {printable(row.id)}  {scopes:<18s}  "
                f'"{printable(_truncate(row.summary, 50))}"  '
                f"({row.retrieval_count} retrievals)"
            )
        if report.cold_endorsement_memories_total > 10:
            lines.append(f"  … plus {report.cold_endorsement_memories_total - 10} more")
    elif report.cold_endorsement_suppressed:
        # The one shared sentence (`health._HOOKLESS_REASON`) so this
        # renderer, memory_health, and the consolidate refusal cannot
        # drift into different explanations of the same silence.
        lines.append("")
        lines.append(f"Cold-endorsement memories: suppressed — {_HOOKLESS_REASON}")

    if report.silent_miss_recent:
        lines.append("")
        lines.append(f"Silent-miss candidates (last {len(report.silent_miss_recent)}):")
        for c in report.silent_miss_recent:
            top = printable(c.top_missed_id or "—")
            rel = printable(c.top_missed_relevance or "—")
            lines.append(
                f"  {printable(_short_ts(c.ts))}  "
                f"session={printable(c.session_id[:12])}…  "
                f"missed={top} relevance={rel}"
            )

    if report.threshold_rule:
        lines.append("")
        lines.append(f"Threshold rule: {printable(report.threshold_rule)}")
    lines.extend(_review_text_lines(report.review))
    lines.extend(_surface_text_lines(report.surfaces))
    if (
        report.memory_helped_rate.torn_read
        or report.endorsement_rate.torn_read
        or report.silent_miss_rate.torn_read
    ):
        lines.append("")
        lines.append(
            "NOTE: numerator > denominator (rate clamped to 1.0). Usually a "
            "windowing artifact under --since (a use event is in-window while "
            "its retrieval aged out), or — less often — a log read mid-rotation. "
            "Re-run without --since to rule out the window edge."
        )
    return _text(lines)


def _text(lines: list[str]) -> str:
    """The finished text: every line through `history.printable` once
    more, as doctor ends its report, so a field a renderer did not escape
    still cannot reach the terminal raw. `printable` writes ASCII escapes
    and leaves a backslash alone, so the second pass changes nothing the
    first one escaped."""
    return "\n".join(printable(line) for line in lines) + "\n"


def _review_text_lines(review: dict[str, Any] | None) -> list[str]:
    """The text form of the review section: totals and the per-client
    rows. Silent when no review chain was issued in the window. The
    per-model split is in the JSON."""
    if not review or not review["chains"]["issued"]:
        return []
    chains = review["chains"]

    def num(value: Any) -> str:
        return "n/a" if value is None else str(value)

    lines = [
        "",
        "Write review (chains issued in the window):",
        f"  issued={chains['issued']} answered={chains['answered']} "
        f"unanswered={chains['unanswered']} pending={chains['pending']} "
        f"closed_by_call={chains['closed_by_call']}",
        f"  pre_answered={review['pre_answered']} "
        f"pre_answered_zero={review['pre_answered_zero']['total']} "
        f"response_chars_p50={num(review['response_chars_p50'])} "
        f"calls_per_committed_write={num(review['calls_per_committed_write'])}",
        "  actions "
        + " ".join(
            f"{printable(str(name))}={count}"
            for name, count in review["actions"].items()
        ),
        "  filings_per_100_committed "
        + " ".join(
            f"{printable(str(name))}={num(value)}"
            for name, value in review["filings_per_100_committed"].items()
        ),
    ]
    for client, bucket in review["by_client"].items():
        lines.append(
            f"  client {printable(str(client))}: issued={bucket['issued']} "
            f"unanswered={bucket['unanswered']} "
            f"pre_answered_zero={bucket['pre_answered_zero']}"
        )
    grades = review["grades"]
    if grades["n"]:
        lines.append(
            f"  grades n={grades['n']} right_both={grades['right_both']} "
            f"pre_answered_n={grades['pre_answered_n']}"
        )
    return lines


def _rate_from_dict(rate: dict[str, Any]) -> RateCI:
    """A `RateCI` back from its `to_dict` form."""
    return RateCI(
        numerator=rate["numerator"],
        denominator=rate["denominator"],
        rate=rate["rate"],
        lower=rate["ci95_lower"],
        upper=rate["ci95_upper"],
        torn_read=rate["torn_read"],
    )


_SURFACE_RATES: tuple[str, ...] = (
    "shown_opened_rate",
    "shown_used_rate",
    "ignored_rate",
    "delivery_helped_rate",
)


def _surface_text_lines(surfaces: dict[str, Any] | None) -> list[str]:
    """The text form of the surfaces section; nothing when the window
    holds none of the recall surface's rows."""
    if surfaces is None:
        return []
    rules = printable(", ".join(surfaces["rules"])) or "none"
    lines = ["", f"Recall surfaces (rule {rules}):"]
    for surface in ("brief", "recall"):
        tally = surfaces[surface]
        lines.append(
            f"  {surface}: deliveries={tally['deliveries']} shown={tally['shown']} "
            f"opened={tally['opened']} used={tally['used']} "
            f"ignored={tally['ignored']} helped={tally['deliveries_helped']}"
        )
        for name in _SURFACE_RATES:
            lines.append("    " + _format_rate(name, _rate_from_dict(tally[name])))
    suppressed = surfaces["recall"]["suppressed"]
    if suppressed:
        lines.append(
            "  recall held back "
            + " ".join(
                f"{printable(str(reason))}={count}"
                for reason, count in suppressed.items()
            )
        )
    settled = surfaces["settlements"]
    segments = surfaces["segments"]
    unanswered = surfaces["review_unanswered"]
    lines.append(
        f"  settled brief={settled['brief']} recall={settled['recall']} · "
        f"sessions={segments['sessions']} segments_opened={segments['opened']} · "
        f"review_unanswered turns={unanswered['turns']} "
        f"chains={unanswered['chains']}"
    )
    for stratum, grade in surfaces["grades"].items():
        if grade["graded"]:
            lines.append(
                "  "
                + _format_rate(
                    f"graded {stratum}", _rate_from_dict(grade["relevant_rate"])
                )
            )
    lines.extend(_rule_split_text_lines(surfaces))
    lines.append(f"  {surfaces['note']}")
    return lines


def _rule_split_text_lines(surfaces: dict[str, Any]) -> list[str]:
    """The text form of the split on the show rows' rule (design 8.11):
    per rule group and surface the counts, with the four rates where the
    group delivered on that surface, then the membership delta. Nothing
    for a section without the split."""
    by_rule = surfaces.get("by_rule")
    if not isinstance(by_rule, dict):
        return []
    lines = ["  By rule:"]
    for group, split in by_rule.items():
        label = group
        if group == EARLIER_RULES:
            label = f"{group} ({printable(', '.join(split['rules'])) or 'none'})"
        for surface in ("brief", "recall"):
            tally = split[surface]
            lines.append(
                f"    {label} {surface}: deliveries={tally['deliveries']} "
                f"shown={tally['shown']} opened={tally['opened']} "
                f"used={tally['used']} ignored={tally['ignored']} "
                f"helped={tally['deliveries_helped']}"
            )
            if tally["deliveries"]:
                for name in _SURFACE_RATES:
                    lines.append(
                        "      " + _format_rate(name, _rate_from_dict(tally[name]))
                    )
    delta = surfaces.get("membership_delta")
    if isinstance(delta, dict):
        lines.append(
            f"  membership_delta rows={delta['rows']} "
            f"differing={delta['rows_differing']} "
            f"delivered_not_rc3={delta['delivered_not_rc3']} "
            f"rc3_not_delivered={delta['rc3_not_delivered']}"
        )
    return lines


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _format_rate(label: str, rate: RateCI) -> str:
    if rate.rate is None or rate.lower is None or rate.upper is None:
        # ``n/a`` rather than 0 — denominator was empty, the rate is
        # undefined. Renderer surfaces the count so the consumer can
        # see *why* it's undefined.
        return f"{label:<20s} n/a    (k={rate.numerator}, n={rate.denominator})"
    # Render the actual asymmetric Wilson bounds, not `rate ± half`. The
    # Wilson interval is asymmetric and the point estimate k/n is NOT its
    # center, so `± half` reconstructed neither bound and printed impossible
    # probabilities at small n (e.g. 1/1 -> "1.00 ± 0.40", implying an upper
    # of 1.40). Brackets match the machine-readable ci95_lower/ci95_upper.
    bar = _bar(rate.rate)
    return (
        f"{label:<20s} {rate.rate:0.2f} [{rate.lower:0.2f}, {rate.upper:0.2f}]   "
        f"{bar}   (k={rate.numerator}, n={rate.denominator})"
    )


def _bar(rate: float, width: int = 10) -> str:
    """Render a rate ∈ [0,1] as a heavy-light unicode bar."""
    rate = max(0.0, min(1.0, rate))
    full = round(rate * width)
    return "▇" * full + "▁" * (width - full)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _humanize_seconds(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _short_ts(ts: str) -> str:
    # ``YYYY-MM-DDTHH:MM:SS…`` → ``YYYY-MM-DD HH:MM``.
    if len(ts) < 16 or "T" not in ts:
        return ts
    date_part, _, rest = ts.partition("T")
    time_part = rest[:5]
    return f"{date_part} {time_part}"


# ---------------------------------------------------------------------------
# Threshold-rule sweep — replay logged misses under alternative rules
# ---------------------------------------------------------------------------


def render_threshold_sweep_text(report: ThresholdSweepReport) -> str:
    """Plain-text rendering: one row per rule, showing how many of the
    `replayable_misses` it would flag and the absolute / percentage
    delta vs. v1."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --threshold-sweep — last {window}")
    lines.append("─" * 60)
    # Window-scoped, matching the header — see `EvalReport.events_in_window`.
    lines.append(f"Events scanned         {report.events_in_window:>5d}")
    lines.append(f"Replayable misses      {report.replayable_misses:>5d}")
    if report.skipped_legacy_event_count > 0:
        lines.append(
            f"  (skipped {report.skipped_legacy_event_count} legacy events "
            "carrying top_hit_ids only — no relevance label to replay against)"
        )
    if report.v1_drift != 0:
        lines.append(
            f"  ⚠ v1 replay drift: {report.v1_drift:+d} (the in-process "
            "v1 rule disagrees with the production decision on that many "
            "events — check audit.py / eval.THRESHOLD_RULES for divergence)"
        )
    if report.replayable_misses == 0:
        lines.append("")
        lines.append(
            "No replayable misses in window. The sweep needs `search_miss` "
            "events with `top_hits` (2.6.4+) to produce a comparison; "
            "run with `--since all` if your recent window is empty, or "
            "wait until `memory_audit_turn` has fired a few times."
        )
        return _text(lines)
    lines.append("")
    lines.append(f"{'rule':<32s} {'flagged':>7s}  {'Δ v1':>7s}  {'% v1':>6s}")
    for row in report.rows:
        if row.delta_pct is None:
            pct = "—"
        else:
            pct = f"{row.delta_pct * 100:5.1f}%"
        delta = f"{row.delta_from_v1:+d}" if row.rule != "v1_top1_high" else "—"
        lines.append(f"  {row.rule:<30s} {row.would_flag:>7d}  {delta:>7s}  {pct:>6s}")
    lines.append("")
    lines.append("Caveat: this is a *relative* sweep over events the v1 rule")
    lines.append("already flagged. Strictly looser rules replay over the")
    lines.append("turn_audited stream instead — see --widening-preview.")
    return _text(lines)


# ---------------------------------------------------------------------------
# Widening preview — replay of candidate LOOSER rules over turn_audited
# ---------------------------------------------------------------------------


def render_widening_preview_text(report: WideningPreviewReport) -> str:
    """Plain-text rendering, mirroring the strict sweep's shape."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --widening-preview — last {window}")
    lines.append("─" * 60)
    # Window-scoped, matching the header — see `EvalReport.events_in_window`.
    lines.append(f"Events scanned              {report.events_in_window:>5d}")
    lines.append(f"Replayable audited turns    {report.audits_with_features:>5d}")
    if report.audits_without_features:
        lines.append(
            f"  (skipped {report.audits_without_features} audits without "
            "top_hits — pre-3.14 events or no-hit probes)"
        )
    if report.repeat_audits_skipped:
        lines.append(
            f"  (skipped {report.repeat_audits_skipped} repeat audits — "
            "multi-stop re-probes of the same message)"
        )
    if report.cutoff_retracted:
        lines.append(
            f"  (skipped {report.cutoff_retracted} audits retracted by a "
            "silent_miss_cutoff marker — miss telemetry invalidated by "
            "`consolidate --acknowledge-misses`)"
        )
    if report.audits_with_features == 0:
        lines.append("")
        lines.append(
            "No replayable audited turns yet. The preview needs "
            "`turn_audited` events carrying `top_hits` (3.14+); let the "
            "Stop hook run for a while and re-check."
        )
        return _text(lines)
    lines.append("")
    lines.append(f"v1 baseline (replayed)      {report.v1_baseline_flagged:>5d}")
    lines.append("")
    lines.append(f"{'rule':<32s} {'flagged':>7s}  {'Δ v1':>7s}  {'% v1':>6s}")
    for row in report.rows:
        if row.delta_pct is None:
            pct = "—"
        else:
            pct = f"{row.delta_pct * 100:5.1f}%"
        lines.append(
            f"  {row.rule:<30s} {row.would_flag:>7d}  "
            f"{row.delta_from_v1:+7d}  {pct:>6s}"
        )
    lines.append("")
    lines.append("Δ v1 counts turns the widened rule would flag that the")
    lines.append("replayed v1 baseline would not — the coverage-fraction")
    lines.append("blind-spot cohort. Both sides exclude shielded turns but")
    lines.append("not the project-suppression arm (see compute docstring),")
    lines.append("so absolute counts are upper bounds; the delta is the signal.")
    return _text(lines)


# ---------------------------------------------------------------------------
# Widening detail — per-flagged-turn evidence for precision labeling
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_widening_detail_text(report: WideningDetailReport) -> str:
    """Plain-text rendering: per-memory concentration first (the fast
    diagnostic), then the flagged turns newest-first."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --widening-preview --detail — last {window}")
    lines.append("─" * 60)
    lines.append(
        f"Replayable audited turns {report.audits_with_features:>5d}    "
        f"v1 baseline (replayed) {report.v1_baseline_flagged:>4d}"
    )
    if report.audits_with_features == 0:
        lines.append("")
        lines.append(
            "No replayable audited turns yet. The detail lane needs "
            "`turn_audited` events carrying `top_hits` (3.14+); let the "
            "Stop hook run for a while and re-check."
        )
        return _text(lines)
    for detail in report.rules:
        lines.append("")
        lines.append(
            f"{detail.rule} — {detail.flagged_total} flagged, "
            f"{detail.beyond_v1} beyond v1"
        )
        lines.append(f"  {detail.description}")
        if not detail.turns:
            continue
        lines.append("")
        lines.append(f"  by top-hit memory ({len(detail.by_memory)} distinct):")
        for row in detail.by_memory:
            scores = (
                "score —"
                if row.score_min is None or row.score_max is None
                else f"score {row.score_min:.3f}–{row.score_max:.3f}"
            )
            scopes = ",".join(row.scopes or []) or "—"
            lines.append(
                f"  {row.count:>4d}×  {printable(row.memory_id)}  "
                f"{printable(row.status):<10s} "
                f"{scores}  sessions={row.distinct_sessions}"
            )
            lines.append(
                f"         [{printable(_clip(scopes, 44))}] "
                f"{printable(_clip(row.summary or '(no summary)', 60))}"
            )
        lines.append("")
        lines.append("  flagged turns (newest first):")
        for t in detail.turns:
            ts = (t.ts or "?")[:19]
            cov = (
                f"{t.matched_unique}/{t.query_unique}"
                if t.matched_unique is not None and t.query_unique is not None
                else "?/?"
            )
            marker = "  [v1 too]" if t.v1_also_flagged else ""
            preview = t.probe_query_preview or "(no probe_query logged)"
            suffix = (
                ""
                if t.probe_query_len is None
                or t.probe_query_preview is None
                or t.probe_query_len <= len(t.probe_query_preview)
                else f" (+{t.probe_query_len - len(t.probe_query_preview)} chars)"
            )
            lines.append(
                f"    {printable(ts)}  cov {cov:>6s}  "
                f"v1={printable(t.relevance_v1 or '?')}"
                f"{marker}  → {printable(t.top_hit_id[:10])}…"
            )
            lines.append(f'      "{printable(_clip(preview, 70))}"{suffix}')
    lines.append("")
    lines.append("Label each turn: would inlining the top hit's body have")
    lines.append("helped this message? Concentration on one memory means a")
    lines.append("ranking problem, not a label problem — fix the memory or")
    lines.append("the rule, not the formula.")
    return _text(lines)


# ---------------------------------------------------------------------------
# Tool-usage rollup — per-MCP-tool call counts from the event log
# ---------------------------------------------------------------------------


def render_tool_usage_text(report: ToolUsageReport) -> str:
    """Plain-text rendering for the CLI. One row per tool, sorted by
    descending call count; untelemetered tools surface with a footer
    so the reader sees "memory_health is not counted" rather than
    "memory_health was never called."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --tool-usage — last {window}")
    lines.append("─" * 60)
    # Window-scoped, matching the header — see `EvalReport.events_in_window`.
    lines.append(f"Events scanned     {report.events_in_window:>5d}")
    lines.append(f"Tool calls         {report.total_tool_calls:>5d}")
    lines.append("")
    lines.append(f"{'tool':<32s} {'count':>7s}  share")
    for row in report.rows:
        if not row.has_telemetry:
            lines.append(f"  {row.tool:<30s} {row.count:>7d}  —  (no telemetry)")
            continue
        if row.share is None:
            share_str = "—"
        else:
            share_str = f"{row.share * 100:5.1f}%"
        bar = _bar(row.share or 0.0)
        lines.append(f"  {row.tool:<30s} {row.count:>7d}  {share_str}  {bar}")
    if report.unmapped_event_kinds:
        lines.append("")
        lines.append(
            "Unmapped event kinds (recorder emitted something the tool-usage "
            "map didn't know about — likely a new tool that needs to be "
            "added to _TOOL_EVENT_KIND_TO_TOOL, or a new side-effect kind "
            "that should be added to the side_effect_kinds set):"
        )
        for kind, count in sorted(
            report.unmapped_event_kinds.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            # An unmapped kind is whatever a log row named, so it is
            # escaped like the other renderers' log-sourced strings.
            lines.append(f"  {printable(kind):<30s} {count:>7d}")
    return _text(lines)


# ---------------------------------------------------------------------------
# Publishable markdown report — every rollup above, one self-contained doc
# ---------------------------------------------------------------------------


#: The characters that open markdown inline syntax or end a table cell:
#: a backslash (it escapes what follows), the cell separator, code spans,
#: emphasis and strikethrough, a link or an image, an autolink or inline
#: HTML, and an entity. Each is written backslash-escaped, which CommonMark
#: allows for every ASCII punctuation character.
_MD_CELL_SPECIALS = frozenset("\\|`*_~[]!<>&")


def _md_escape_cell(text: str) -> str:
    """`text` made inert in one markdown table cell. Applied to the only
    non-static string the report emits (model names, which come off
    logged `client_model` stamps; the log is plaintext and
    hand-editable). A character that opens inline syntax or ends the
    cell is backslash-escaped (`_MD_CELL_SPECIALS`), so a ``|`` cannot
    shift columns, and a link or an image written in markdown syntax,
    inline HTML or an entity cannot ride into the published report; a
    line break and every other control or format character is written as
    its Python escape (`history.printable`), so a newline cannot end the
    row and start markdown of its own, and an escape sequence cannot
    reach a terminal that prints the report. The backslashes are escaped
    first, so the ones `printable` writes read as text.

    Left as they are: a bare URL, a ``www.`` host and an email address,
    which GFM's autolink extension still links, and an ``@name``, which
    is a mention once the report is pasted into a GitHub issue. A
    model name is a client's own stamp, so those carry no markup this
    escaping would remove."""
    escaped = "".join(f"\\{ch}" if ch in _MD_CELL_SPECIALS else ch for ch in text)
    return printable(escaped)


def _md_rate_cell(rate: RateCI, *, bold: bool = False) -> str:
    """One rate-trio table cell: ``k/n = 0.07 [0.05, 0.09]``.

    Mirrors docs/eval-results.md's published shape. ``n/a`` keeps the
    raw counts visible so the reader sees *why* the rate is undefined.
    """
    if rate.rate is None or rate.lower is None or rate.upper is None:
        return f"n/a (k={rate.numerator}, n={rate.denominator})"
    value = f"{rate.rate:0.2f}"
    if bold:
        value = f"**{value}**"
    return (
        f"{rate.numerator}/{rate.denominator} = {value} "
        f"[{rate.lower:0.2f}, {rate.upper:0.2f}]"
    )


def _count_phrase(n: int, singular: str, plural: str) -> str:
    """``1 turn audited`` / ``3 turns audited`` — number-agreement for
    the report's counted nouns. The plural is explicit at every call
    site (the phrases pluralise in different positions: "turns
    audited" vs "repeat audits deduped"), so a lazy ``+s`` rule can't
    quietly publish bad grammar."""
    return f"{n} {singular if n == 1 else plural}"


def _md_denominator_note(label: str, report: EvalReport) -> str:
    """One-line counts context under the rate table — window shape a
    reader needs to judge the CIs. Counts only.

    Every figure on the line is window-scoped, including the leading
    event count: it reads `events_in_window`, NOT the all-time
    `total_events_scanned` (which counts the whole log because the
    invalidation markers resolve ahead of the window filter). Mixing
    the two published an all-time figure under a `last Nd:` label.
    """
    parts = [
        _count_phrase(report.events_in_window, "event scanned", "events scanned"),
        _count_phrase(
            report.retrieval_occurrences,
            "retrieval occurrence",
            "retrieval occurrences",
        ),
        _count_phrase(report.applied_total, "applied use event", "applied use events"),
        _count_phrase(report.turns_audited, "turn audited", "turns audited"),
    ]
    if report.turns_no_signal:
        parts.append(
            _count_phrase(
                report.turns_no_signal,
                "no-signal turn excluded",
                "no-signal turns excluded",
            )
        )
    if report.repeat_audits:
        parts.append(
            _count_phrase(
                report.repeat_audits, "repeat audit deduped", "repeat audits deduped"
            )
        )
    return f"- {label}: " + " · ".join(parts) + "."


def _surface_markdown_lines(
    window: dict[str, Any] | None,
    alltime: dict[str, Any] | None,
    *,
    window_label: str,
    single_window: bool,
) -> list[str]:
    """The report's recall surfaces section: each surface's four rates,
    the window beside all time, and the counts under them. Nothing when
    neither column holds the surface's rows."""
    if alltime is None:
        return []
    empty = RateCI(0, 0, None, None, None)
    lines = ["## Recall surfaces", ""]
    if single_window:
        lines.append("| surface | rate | all time |")
        lines.append("|---|---|---|")
    else:
        lines.append(f"| surface | rate | {window_label} | all time |")
        lines.append("|---|---|---|---|")
    for surface in ("brief", "recall"):
        for name in _SURFACE_RATES:
            whole = _rate_from_dict(alltime[surface][name])
            if single_window:
                lines.append(
                    f"| {surface} | `{name}` | {_md_rate_cell(whole, bold=True)} |"
                )
                continue
            part = (
                _rate_from_dict(window[surface][name]) if window is not None else empty
            )
            lines.append(
                f"| {surface} | `{name}` | {_md_rate_cell(part, bold=True)} | "
                f"{_md_rate_cell(whole)} |"
            )
    lines.append("")
    for label, surfaces in ([] if single_window else [(window_label, window)]) + [
        ("all time", alltime)
    ]:
        if surfaces is None:
            continue
        brief, recall = surfaces["brief"], surfaces["recall"]
        settled = surfaces["settlements"]
        lines.append(
            f"- {label}: "
            + " · ".join(
                [
                    _count_phrase(brief["deliveries"], "brief", "briefs"),
                    _count_phrase(
                        recall["deliveries"], "recall delivery", "recall deliveries"
                    ),
                    _count_phrase(
                        brief["shown"] + recall["shown"],
                        "pointer shown",
                        "pointers shown",
                    ),
                    _count_phrase(
                        settled["brief"] + settled["recall"],
                        "pointer settled",
                        "pointers settled",
                    ),
                    _count_phrase(
                        surfaces["segments"]["opened"],
                        "segment opened",
                        "segments opened",
                    ),
                    _count_phrase(
                        surfaces["review_unanswered"]["chains"],
                        "review chain unanswered",
                        "review chains unanswered",
                    ),
                ]
            )
            + "."
        )
    lines.append("")
    lines.append(
        "Use after a show, not because of it: an open is a `memory_show` of "
        "a shown id later in the same conversation, a use an attested "
        "applied `use` of it. " + alltime["note"]
    )
    lines.append("")
    lines.extend(
        _rule_split_markdown_lines(
            window,
            alltime,
            window_label=window_label,
            single_window=single_window,
        )
    )
    return lines


def _rule_split_markdown_lines(
    window: dict[str, Any] | None,
    alltime: dict[str, Any],
    *,
    window_label: str,
    single_window: bool,
) -> list[str]:
    """The report's split of the recall surfaces on the show rows' rule
    (design 8.11): each surface's four rates under the current rule and
    under the earlier rules together, the window beside all time, and the
    membership delta. Group names only (the current rule's name and
    `EARLIER_RULES`), never a rule string a row carried."""
    from .recall import RULE

    whole = alltime.get("by_rule")
    if not isinstance(whole, dict):
        return []
    part = window.get("by_rule") if window is not None else None
    empty = RateCI(0, 0, None, None, None)
    lines = ["### By rule", ""]
    if single_window:
        lines.append("| rule | surface | rate | all time |")
        lines.append("|---|---|---|---|")
    else:
        lines.append(f"| rule | surface | rate | {window_label} | all time |")
        lines.append("|---|---|---|---|---|")
    for group in whole:
        for surface in ("brief", "recall"):
            for name in _SURFACE_RATES:
                total = _rate_from_dict(whole[group][surface][name])
                if single_window:
                    lines.append(
                        f"| {group} | {surface} | `{name}` | "
                        f"{_md_rate_cell(total, bold=True)} |"
                    )
                    continue
                windowed = (
                    _rate_from_dict(part[group][surface][name])
                    if isinstance(part, dict) and group in part
                    else empty
                )
                lines.append(
                    f"| {group} | {surface} | `{name}` | "
                    f"{_md_rate_cell(windowed, bold=True)} | {_md_rate_cell(total)} |"
                )
    lines.append("")
    for label, surfaces in ([] if single_window else [(window_label, window)]) + [
        ("all time", alltime)
    ]:
        delta = surfaces.get("membership_delta") if surfaces is not None else None
        if not isinstance(delta, dict):
            continue
        lines.append(
            f"- membership_delta, {label}: "
            + " · ".join(
                [
                    _count_phrase(delta["rows"], "row compared", "rows compared"),
                    _count_phrase(
                        delta["rows_differing"], "row differing", "rows differing"
                    ),
                    _count_phrase(
                        delta["delivered_not_rc3"],
                        "pointer delivered and not rc3-admitted",
                        "pointers delivered and not rc3-admitted",
                    ),
                    _count_phrase(
                        delta["rc3_not_delivered"],
                        "pointer rc3-admitted and not delivered",
                        "pointers rc3-admitted and not delivered",
                    ),
                ]
            )
            + "."
        )
    lines.append("")
    lines.append(
        f"Split on each show row's rule, never on a clock: `{RULE}` "
        "delivers by membership, the earlier rules by the origin columns. "
        "The delta compares a row's delivered pointers with the ones rc3's "
        "rule would have delivered (`rc3_admitted`)."
    )
    lines.append("")
    return lines


def render_report_markdown(doc: ReportDocument) -> str:
    """Render the composed document as publishable markdown.

    Content contract (the canary test pins it): rates, counts, CIs,
    model names, and static registry names only. Sections, top to
    bottom: header, rate trio (window vs all-time), reading guide,
    per-model table, the recall surfaces (only when the log holds the
    surface's rows), threshold sweep, tool-usage top 10, methodology
    footer.
    """
    single_window = doc.window_seconds is None
    window_label = (
        "all time"
        if doc.window_seconds is None
        else f"last {_humanize_seconds(doc.window_seconds)}"
    )
    lines: list[str] = []

    # 1. Title + header — aggregate store shape, counts only.
    lines.append("# bettermemory eval report")
    lines.append("")
    if single_window:
        lines.append(f"Generated {doc.generated_at.isoformat()} · window: all time")
    else:
        lines.append(
            f"Generated {doc.generated_at.isoformat()} · "
            f"window: {window_label} vs all time"
        )
    lines.append("")
    active_noun = "active memory" if doc.active_memory_count == 1 else "active memories"
    event_noun = "logged event" if doc.total_events == 1 else "logged events"
    session_noun = (
        "distinct session" if doc.distinct_session_count == 1 else "distinct sessions"
    )
    lines.append(
        f"Store shape: **{doc.active_memory_count}** {active_noun} · "
        f"**{doc.total_events}** {event_noun} · "
        f"**{doc.distinct_session_count}** {session_noun}. "
        "Counts only — memory bodies, queries, scopes, paths, and session "
        "ids never appear in this report."
    )
    lines.append("")

    # 2. Rate trio, window vs all-time columns.
    lines.append("## Rates")
    lines.append("")
    if single_window:
        lines.append("| rate | all time |")
        lines.append("|---|---|")
        for name, rate in (
            ("memory_helped_rate", doc.alltime_eval.memory_helped_rate),
            ("endorsement_rate", doc.alltime_eval.endorsement_rate),
            ("silent_miss_rate", doc.alltime_eval.silent_miss_rate),
        ):
            lines.append(f"| `{name}` | {_md_rate_cell(rate, bold=True)} |")
    else:
        lines.append(f"| rate | {window_label} | all time |")
        lines.append("|---|---|---|")
        for name, window_rate, alltime_rate in (
            (
                "memory_helped_rate",
                doc.window_eval.memory_helped_rate,
                doc.alltime_eval.memory_helped_rate,
            ),
            (
                "endorsement_rate",
                doc.window_eval.endorsement_rate,
                doc.alltime_eval.endorsement_rate,
            ),
            (
                "silent_miss_rate",
                doc.window_eval.silent_miss_rate,
                doc.alltime_eval.silent_miss_rate,
            ),
        ):
            lines.append(
                f"| `{name}` | {_md_rate_cell(window_rate, bold=True)} | "
                f"{_md_rate_cell(alltime_rate)} |"
            )
    lines.append("")
    lines.append("Wilson 95% confidence intervals in brackets.")
    lines.append("")
    if not single_window:
        lines.append(_md_denominator_note(window_label, doc.window_eval))
    lines.append(_md_denominator_note("all time", doc.alltime_eval))
    torn = (
        doc.window_eval.memory_helped_rate.torn_read
        or doc.window_eval.endorsement_rate.torn_read
        or doc.window_eval.silent_miss_rate.torn_read
        or doc.alltime_eval.memory_helped_rate.torn_read
        or doc.alltime_eval.endorsement_rate.torn_read
        or doc.alltime_eval.silent_miss_rate.torn_read
    )
    if torn:
        lines.append("")
        lines.append(
            "> Note: a numerator exceeded its denominator (rate clamped to "
            "1.0). Usually a windowing artifact under a `--since` window — "
            "a use event lands in-window while its retrieval aged out — or, "
            "less often, a log read mid-rotation."
        )
    lines.append("")

    # 3. Reading guide — static prose adapted from docs/eval-results.md.
    lines.append("## Reading these numbers honestly")
    lines.append("")
    lines.append(
        "- `memory_helped_rate` is a deliberate floor: the numerator counts "
        "only explicit, claim-excerpt-backed endorsements, while the "
        "denominator counts every retrieval occurrence. Retrievals that "
        "quietly helped don't count."
    )
    if not single_window:
        lines.append(
            "- The window column vs the all-time column is the story: the "
            "attestation tooling matures over a store's history, so early "
            "events couldn't carry signals that now exist. Read the trend, "
            "not either column alone."
        )
    lines.append(
        "- A low or zero `silent_miss_rate` is a claim about the loosest "
        "evaluable rule (`v1_top1_high`), not about misses in general — "
        "strictly looser rules can't be replayed from the log alone. The "
        "threshold-sweep table below replays the flagged misses against "
        "stricter rules."
    )
    lines.append(
        "- n=1: this measures one deployment's store, workload, and "
        "retrieval discipline. Treat it as telemetry, not a benchmark."
    )
    lines.append("")

    # 4. Per-model audit telemetry.
    lines.append("## Per-model audit telemetry (all time)")
    lines.append("")
    by_model = doc.alltime_eval.by_model
    if by_model:
        lines.append("| model | audited | no_signal | misses |")
        lines.append("|---|---|---|---|")
        for model in sorted(by_model):
            counts = by_model[model]
            lines.append(
                f"| {_md_escape_cell(model)} | {counts.get('audited', 0)} | "
                f"{counts.get('no_signal', 0)} | {counts.get('misses', 0)} |"
            )
    else:
        lines.append(
            "No per-model telemetry in the log yet (the `client_model` "
            "stamp arrived with 3.14 events)."
        )
    lines.append("")

    # 4b. The recall surfaces, when either column holds the surface's
    # rows. Counts and rates only, like every section here.
    lines.extend(
        _surface_markdown_lines(
            doc.window_eval.surfaces,
            doc.alltime_eval.surfaces,
            window_label=window_label,
            single_window=single_window,
        )
    )

    # 5. Threshold-sweep counterfactual. Rule names/descriptions come
    # from the rows compute_threshold_sweep built off the static
    # registry — never from log-derived strings.
    lines.append("## Threshold sweep (counterfactual, all time)")
    lines.append("")
    if doc.sweep.replayable_misses == 0:
        lines.append(
            "No replayable misses in the log — the counterfactual needs "
            "`search_miss` events carrying `top_hits` (2.6.4+)."
        )
    else:
        lines.append("| rule | would flag | Δ v1 | % of v1 |")
        lines.append("|---|---|---|---|")
        for row in doc.sweep.rows:
            delta = f"{row.delta_from_v1:+d}" if row.rule != "v1_top1_high" else "—"
            pct = f"{row.delta_pct * 100:.1f}%" if row.delta_pct is not None else "—"
            lines.append(f"| `{row.rule}` | {row.would_flag} | {delta} | {pct} |")
        lines.append("")
        replayable = _count_phrase(
            doc.sweep.replayable_misses, "replayable miss", "replayable misses"
        )
        lines.append(
            f"{replayable}. Stricter rules replay over misses v1 already "
            'flagged, so this table answers "is v1 over-firing?" — not '
            '"what does v1 miss?".'
        )
        if doc.sweep.skipped_legacy_event_count:
            skipped = _count_phrase(
                doc.sweep.skipped_legacy_event_count, "legacy event", "legacy events"
            )
            lines.append(f"Skipped {skipped} carrying no replayable relevance label.")
    lines.append("")

    # 6. Tool-usage top 10.
    lines.append("## Tool usage (top 10, all time)")
    lines.append("")
    lines.append("| tool | calls | share |")
    lines.append("|---|---|---|")
    # The slice is top-10 by count, PLUS every untelemetered row pinned
    # in regardless of rank: those rows exist to say "not counted" (see
    # TOOLS_WITHOUT_TELEMETRY), and past ten tools a structurally-zero
    # row can never crack a top-10 on count; sliced out, the note the
    # row carries would silently vanish from the published artifact.
    published = list(doc.tool_usage.rows[:10])
    published.extend(row for row in doc.tool_usage.rows[10:] if not row.has_telemetry)
    for usage_row in published:
        if not usage_row.has_telemetry:
            # Mirrors render_tool_usage_text: a tool that emits no
            # dedicated event of its own publishes as "not counted",
            # never as a bare zero a reader would take for "nobody ever
            # called it".
            lines.append(f"| `{usage_row.tool}` (no telemetry) | — | — |")
            continue
        share = f"{usage_row.share * 100:.1f}%" if usage_row.share is not None else "—"
        lines.append(f"| `{usage_row.tool}` | {usage_row.count} | {share} |")
    lines.append("")
    lines.append(
        f"{_count_phrase(doc.tool_usage.total_tool_calls, 'tool call', 'tool calls')} "
        f"total across {len(doc.tool_usage.rows)} known tools."
    )
    lines.append("")

    # 7. Methodology footer.
    lines.append("---")
    lines.append("")
    lines.append(
        f"Generated by `bettermemory eval --report` v{doc.version} "
        "(metric definitions: docs/eval.md)."
    )
    return "\n".join(lines) + "\n"
