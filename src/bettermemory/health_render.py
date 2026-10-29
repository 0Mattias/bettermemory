"""The renderers of `health.py`: a `HealthReport` as plain text or as JSON.

`bettermemory health` prints the report with `render_text`; `render_json`
writes the report's dict as indented JSON. Both read only the report and
the model's `_iso`. `health` re-exports both names, so callers keep
importing them from `health`. Nothing here imports `health`.

Every string the text form takes from the store or the log (an id, a
summary, a scope, a marker, an actor's spelling, a repo or work tree) is
printed through `history.printable`, as posture prints its fields, and
every finished line goes through it once more, as doctor ends its report:
the store is a file an editor can write and the log carries what a client
stamped, so an escape sequence or a line break in one must not reach the
terminal as one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .health_model import HealthReport, _iso
from .history import printable


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def _scopes(scopes: Iterable[str]) -> str:
    """A row's scopes, comma-joined and escaped."""
    return printable(",".join(scopes))


def render_text(report: HealthReport) -> str:
    """Pretty-print the report for the CLI subcommand. Plain text — no
    colour codes — so it pipes cleanly to a file. Store- and log-sourced
    strings are escaped (`history.printable`); `render_json` is not, since
    JSON escapes them itself."""
    lines: list[str] = []
    lines.append(
        f"Memory health — generated {_iso(report.generated_at)}, "
        f"window {report.window_days} days"
    )
    lines.append("=" * 70)
    lines.append(f"Active memories: {report.total_active_memories}")
    lines.append(f"Events seen:     {report.total_events}")
    lines.append(f"Sessions:        {report.distinct_sessions}")
    if report.episode_volume is not None:
        vol = report.episode_volume
        prunable = ""
        if vol.prunable_sessions:
            # Only surfaced when it is actionable. A read-only loop is the
            # case this line exists for: episode GC runs on `episode_write`
            # and `bettermemory episodes prune` only, so these directories
            # sit collectable until something writes.
            prunable = (
                f" — {vol.prunable_sessions} session"
                f"{'' if vol.prunable_sessions == 1 else 's'} past the "
                f"{vol.ttl_days}-day TTL, run `bettermemory episodes prune`"
            )
        lines.append(
            f"Episodes:        {vol.episodes} in {vol.sessions} session"
            f"{'' if vol.sessions == 1 else 's'}, "
            f"{vol.bytes:,} bytes{prunable}"
        )

    lines.append("")
    lines.append(
        f"Dead weight ({len(report.dead_weight)}) — retrieved but never "
        f"`applied`, older than {report.window_days} days:"
    )
    if not report.dead_weight:
        lines.append("  (none)")
        # An empty bucket has two very different causes, and the CLI
        # reader has no other way to tell them apart. Printed only when
        # suppressed, so the line never appears on a clean hooked store.
        coverage = report.telemetry_coverage
        if coverage is not None and coverage.dead_weight_suppressed:
            lines.append(f"  NOT MEASURED: {coverage.reason}")
    for s in report.dead_weight[:20]:
        lines.append(
            f"  {printable(s.id)} [retrievals={s.retrieval_count}] "
            f"{_scopes(s.scopes)}: {printable(s.summary)}"
        )
    if len(report.dead_weight) > 20:
        lines.append(f"  ... and {len(report.dead_weight) - 20} more")

    lines.append("")
    lines.append(
        f"Cold memories ({len(report.cold_memories)}) — never retrieved, "
        f"older than {report.window_days} days:"
    )
    if not report.cold_memories:
        lines.append("  (none)")
    for s in report.cold_memories[:20]:
        lines.append(f"  {printable(s.id)} {_scopes(s.scopes)}: {printable(s.summary)}")
    if len(report.cold_memories) > 20:
        lines.append(f"  ... and {len(report.cold_memories) - 20} more")

    lines.append("")
    lines.append(f"Heavily used ({len(report.heavily_used)}):")
    if not report.heavily_used:
        lines.append("  (none)")
    for s in report.heavily_used:
        # Surface the auto/explicit split so a curator can spot
        # weakly-endorsed memories at a glance: applied=N (auto=X exp=Y)
        # where exp=0 with non-zero auto is the "ranker keeps surfacing
        # this but the model never deliberately endorses it" pattern.
        lines.append(
            f"  {printable(s.id)} [applied={s.applied_count} "
            f"(auto={s.auto_applied_count} "
            f"exp={s.explicit_applied_count})] "
            f"{_scopes(s.scopes)}: {printable(s.summary)}"
        )

    lines.append("")
    lines.append(f"Contradicted ({len(report.contradicted)}):")
    if not report.contradicted:
        lines.append("  (none)")
    for s in report.contradicted:
        lines.append(
            f"  {printable(s.id)} [contradicted={s.contradicted_count}] "
            f"{_scopes(s.scopes)}: {printable(s.summary)}"
        )

    lines.append("")
    lines.append(f"Marker stats ({len(report.marker_stats)}):")
    if not report.marker_stats:
        lines.append("  (none)")
    else:
        # The rate's terms, once (`health_model.MarkerStats.override_rate`):
        # a refusal sent again before its override counts as a second fire.
        lines.append(
            "  rate = overrides / (fires + overrides); a refusal retried "
            "counts as two fires"
        )
    for m in report.marker_stats:
        rate_pct = round(m.override_rate * 100, 1)
        lines.append(
            f"  {printable(m.marker):<24} fires={m.fire_count}  "
            f"overrides={m.override_count}  rate={rate_pct}%"
        )

    lines.append("")
    lines.append(f"Scope health ({len(report.scope_health)}):")
    if not report.scope_health:
        lines.append("  (none)")
    for sh in report.scope_health:
        lines.append(
            f"  {printable(sh.scope):<28} active={sh.active:<3} dead={sh.dead:<3} "
            f"cold={sh.cold:<3} contradicted={sh.contradicted:<3} "
            f"applied={sh.applied_total}"
        )

    lines.append("")
    slices = report.actor_slices
    lines.append(f"Actors ({len(slices.declared)} declared):")
    if not slices.declared:
        lines.append("  (none declared)")
    for sl in slices.declared:
        spellings = ""
        if sl.models:
            spellings += f"  models={printable(','.join(sorted(sl.models)))}"
        if sl.principals:
            spellings += f"  principals={printable(','.join(sorted(sl.principals)))}"
        lines.append(
            f"  {printable(sl.client or ''):<20} memories={sl.memories:<5} "
            f"in-window={sl.memories_in_window:<5} events={sl.events:<6} "
            f"searches={sl.searches:<5} applies={sl.applies}{spellings}"
        )
    # Always printed, including at zero. On any store with history this
    # is the majority bucket (every record written before 7.10.0), and
    # a census that showed only declared actors would read as complete
    # while describing a fraction of the store.
    und = slices.undeclared
    lines.append(
        f"  {'(undeclared)':<20} memories={und.memories:<5} "
        f"in-window={und.memories_in_window:<5} events={und.events:<6} "
        f"searches={und.searches:<5} applies={und.applies}"
    )

    lines.append("")
    lines.append(
        f"Rare scopes ({len(report.rare_scopes)}) — singletons within "
        "2 edits of another scope, likely typos:"
    )
    if not report.rare_scopes:
        lines.append("  (none)")
    for scope in report.rare_scopes:
        lines.append(f"  {printable(scope)}")

    if report.orphan_use_events:
        lines.append("")
        lines.append(
            f"Orphan use events: {report.orphan_use_events} — "
            "memory_record_use events whose ids resolved to neither active "
            "nor tombstoned memories. A growing count is the smoke test for "
            "fabricated ULIDs."
        )

    debt = report.verification_debt
    lines.append("")
    lines.append(
        f"Verification debt — never={debt.never_verified_total} "
        f"({debt.never_verified_checkable} checkable)  "
        f"stale={debt.stale_total} ({debt.stale_checkable} checkable)  "
        f"fresh={debt.fresh_count}  "
        f"(stale after {debt.stale_after_days} days; checkable = "
        "declared claims or cited/attested paths a verify pass can "
        "check mechanically):"
    )
    if debt.never_verified_total == 0 and debt.stale_total == 0:
        lines.append("  (none)")
    if debt.never_verified:
        lines.append(
            f"  never-verified ({debt.never_verified_total}, "
            "checkable first, then oldest):"
        )
        for s in debt.never_verified:
            lines.append(
                f"    {printable(s.id)} {_scopes(s.scopes)}: {printable(s.summary)}"
            )
        if debt.never_verified_total > len(debt.never_verified):
            lines.append(
                f"    ... and {debt.never_verified_total - len(debt.never_verified)} more"
            )
    if debt.stale:
        lines.append(
            f"  stale ({debt.stale_total}, checkable first, then oldest verification):"
        )
        for s in debt.stale:
            verified = _iso(s.last_verified_at) or "?"
            lines.append(
                f"    {printable(s.id)} [verified={verified}] "
                f"{_scopes(s.scopes)}: {printable(s.summary)}"
            )
        if debt.stale_total > len(debt.stale):
            lines.append(f"    ... and {debt.stale_total - len(debt.stale)} more")

    ce = report.cold_endorsement_memories
    if ce.total > 0:
        lines.append("")
        lines.append(
            f"Cold-endorsement memories ({ce.total}) — retrieved >= "
            f"{ce.min_retrievals} times, never explicitly applied "
            "(model never deliberately endorsed the memory; every "
            "applied event came from the auto-commit pass):"
        )
        for s in ce.rows:
            lines.append(
                f"  {printable(s.id)} [retrievals={s.retrieval_count} "
                f"auto_applied={s.auto_applied_count}] "
                f"{_scopes(s.scopes)}: {printable(s.summary)}"
            )
        if ce.total > len(ce.rows):
            lines.append(f"  ... and {ce.total - len(ce.rows)} more")

    sm = report.silent_misses
    if sm.audited_total > 0 or sm.miss_total > 0 or sm.no_signal_total > 0:
        lines.append("")
        # `no_signal` rides the header so the two shapes the stats
        # docstring promises to distinguish actually render apart: a
        # store whose every audit stopped at the no-signal branch used
        # to print byte-identically to one whose audit hook never fired.
        lines.append(
            f"Silent misses — audited={sm.audited_total}  "
            f"miss={sm.miss_total}  "
            f"no_signal={sm.no_signal_total}  "
            f"unique_memories={sm.unique_miss_memories}  "
            f"(recorded by the Stop hook, `bettermemory hook stop`):"
        )
        if sm.audited_total == 0 and sm.no_signal_total > 0:
            lines.append(
                "  (no measurable audits — every audited turn stopped at "
                "the no-signal branch: empty store, empty query, or no "
                "hits at all)"
            )
        elif sm.miss_total == 0:
            lines.append("  (none — audit ran and found no misses)")
        else:
            miss_rate_pct: float | None = (
                round(sm.miss_total / sm.audited_total * 100, 1)
                if sm.audited_total > 0
                else None
            )
            rate_str = f"{miss_rate_pct}%" if miss_rate_pct is not None else "?"
            lines.append(
                f"  {sm.miss_total} of {sm.audited_total} audited turns "
                f"flagged a miss (rate={rate_str}) "
                f"across {sm.unique_miss_memories} distinct memor"
                f"{'y' if sm.unique_miss_memories == 1 else 'ies'}"
            )

    cd = report.commit_drift_debt
    if cd is not None:
        lines.append("")
        lines.append(
            f"Commit drift — anchor={printable(cd.current_repo or '?')}  "
            f"drifted={cd.total_drifted}:"
        )
        if not cd.rows:
            lines.append("  (none — anchored memories are caught up with HEAD)")
        else:
            lines.append(f"  drifted ({cd.total_drifted}, most commits-ahead first):")
            for row in cd.rows:
                verified = _iso(row.last_verified_at) or "?"
                lines.append(
                    f"    {printable(row.id)} [+{row.commits_since_verify} commits, "
                    f"verified={verified}] {_scopes(row.scopes)}: "
                    f"{printable(row.summary)}"
                )
            if cd.total_drifted > len(cd.rows):
                lines.append(f"    ... and {cd.total_drifted - len(cd.rows)} more")

    if report.cross_repo_drift is not None:
        xr = report.cross_repo_drift
        lines.append("")
        header = (
            f"Cross-repo drift — {xr.total_drifted} drifted across "
            f"{len(xr.groups)} checked foreign checkout(s)"
        )
        if xr.skipped:
            header += f", {len(xr.skipped)} skipped"
        if xr.unanchorable:
            header += f", {xr.unanchorable} unanchorable"
        lines.append(header + ":")
        for group in xr.groups:
            state = f"{group.total_drifted} drifted" if group.total_drifted else "clean"
            lines.append(
                f"  {printable(group.repo)} [{printable(group.worktree_root)}]: "
                f"{state} of {group.candidates} candidate(s)"
            )
            for row in group.rows:
                verified = _iso(row.last_verified_at) or "?"
                lines.append(
                    f"    {printable(row.id)} [+{row.commits_since_verify} commits, "
                    f"verified={verified}] {_scopes(row.scopes)}: "
                    f"{printable(row.summary)}"
                )
            if group.total_drifted > len(group.rows):
                lines.append(
                    f"    ... and {group.total_drifted - len(group.rows)} more"
                )
        for skip in xr.skipped:
            lines.append(
                f"  (skipped {printable(str(skip['repo']))} "
                f"[{printable(str(skip['worktree_root']))}]: "
                f"{printable(str(skip['reason']))})"
            )

    # Every line through `printable` once more, as doctor ends its report:
    # a field above that was not escaped still cannot reach the terminal
    # raw, and the fields that were come through unchanged.
    return "\n".join(printable(line) for line in lines) + "\n"


def render_json(report: HealthReport) -> str:
    return json.dumps(report.to_dict(), indent=2) + "\n"
