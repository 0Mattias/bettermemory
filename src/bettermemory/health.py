"""Aggregate health view of the memory store.

Reads from `events.iter_all_events` (active log + rotated archives) and
joins against `Store.load_all` to produce per-memory and per-marker
stats. Exposed two ways:

- as the `memory_health` MCP tool, so the model can self-curate during
  a conversation,
- as `bettermemory health` on the CLI, for offline audit.

The metrics are designed around the failure modes the rest of this
project is trying to detect:

- **dead_weight**: memories that have been retrieved but never `applied`
  (or never retrieved at all, despite existing for a while). Either the
  search ranking isn't surfacing them, or they're noise. Either way,
  prune candidates.
- **heavily_used**: memories with high applied-count. These are working;
  don't touch them.
- **contradicted**: memories with a `contradicted` use event newer than
  both the last `updated` (body refresh via `memory_update`) and the
  last `last_verified_at` (explicit re-check via `memory_verify`). Either
  resolution path clears the flag, so a sticky entry can be cleared by
  re-running the appropriate one.
- **marker_stats**: the transient-marker override rate, per marker. A
  high override rate is the signal to remove the marker from the list,
  not vibes. A near-zero rate with non-zero fires is a healthy marker.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .events import iter_all_events
from .models import Memory, first_summary_line


# ---------------------------------------------------------------------------
# Per-memory and per-marker stats
# ---------------------------------------------------------------------------


@dataclass
class MemoryStats:
    """All the event-driven metrics for one memory.

    These are derived purely from the event log + the live memory record;
    nothing here is persisted on the memory itself. `last_verified_at` is
    the only field that comes off the memory record itself rather than
    the event stream — surfacing it here lets a curation pass treat
    "applied count" and "verification age" as orthogonal staleness axes
    without a second round-trip through the store.
    """

    id: str
    scopes: list[str]
    summary: str
    created: datetime
    updated: datetime
    retrieval_count: int = 0
    show_count: int = 0
    applied_count: int = 0
    ignored_count: int = 0
    contradicted_count: int = 0
    last_used_at: datetime | None = None
    last_contradicted_at: datetime | None = None
    last_verified_at: datetime | None = None

    @property
    def has_unresolved_contradiction(self) -> bool:
        """True if there's been a contradiction since the memory was
        last touched by either resolution path.

        Two ways to clear a contradiction:
        - **memory_update** bumps `updated` — the body has been refreshed
          in response to the contradiction.
        - **memory_verify** bumps `last_verified_at` — the body wasn't
          changed, but the caller spot-checked reality and confirmed it
          still matches despite the earlier contradiction event.

        Either action is a legitimate resolution, so the flag clears as
        soon as the later of the two timestamps surpasses the
        contradiction. This also gives the caller an out for the case
        where the `record_use(contradicted)` event is logged *after*
        the body was already corrected — re-running `memory_verify`
        slides the timestamp forward past the contradiction and the
        flag clears.
        """
        if self.last_contradicted_at is None:
            return False
        last_resolved_at = self.updated
        if (
            self.last_verified_at is not None
            and self.last_verified_at > last_resolved_at
        ):
            last_resolved_at = self.last_verified_at
        return self.last_contradicted_at > last_resolved_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopes": list(self.scopes),
            "summary": self.summary,
            "created": _iso(self.created),
            "updated": _iso(self.updated),
            "retrieval_count": self.retrieval_count,
            "show_count": self.show_count,
            "applied_count": self.applied_count,
            "ignored_count": self.ignored_count,
            "contradicted_count": self.contradicted_count,
            "last_used_at": _iso(self.last_used_at) if self.last_used_at else None,
            "last_verified_at": (
                _iso(self.last_verified_at) if self.last_verified_at else None
            ),
            "has_unresolved_contradiction": self.has_unresolved_contradiction,
        }


@dataclass
class MarkerStats:
    """Per-marker fire and override counts from `memory_write` events."""

    marker: str
    fire_count: int = 0
    override_count: int = 0

    @property
    def total(self) -> int:
        return self.fire_count + self.override_count

    @property
    def override_rate(self) -> float:
        """Fraction of fires the caller chose to override.

        High value = caller routinely rubber-stamps `acknowledge_transient`,
        which is the signal that the marker is producing too many false
        positives. Trim it.
        """
        return self.override_count / self.total if self.total > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "fire_count": self.fire_count,
            "override_count": self.override_count,
            "override_rate": round(self.override_rate, 3),
        }


@dataclass
class ScopeHealth:
    """Per-scope curation pivot.

    A flat dead_weight/heavily_used/contradicted view doesn't tell you
    whether the rot is concentrated in one scope. With per-scope counts
    you can drive a focused curation pass — "projects:foo has 4
    dead-weight memories out of 6 total, time to revisit" — without
    re-pivoting the flat lists by hand.

    Counts are over the same windowed event log as the flat view, so
    the numbers reconcile: sum of `active` across scopes >= total active
    (a memory tagged with N scopes is counted in each, by design).
    """

    scope: str
    active: int = 0
    dead: int = 0
    contradicted: int = 0
    applied_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "active": self.active,
            "dead": self.dead,
            "contradicted": self.contradicted,
            "applied_total": self.applied_total,
        }


@dataclass
class HealthReport:
    """The full aggregate view returned by `memory_health`."""

    generated_at: datetime
    window_days: int
    total_active_memories: int
    total_events: int
    distinct_sessions: int

    dead_weight: list[MemoryStats] = field(default_factory=list)
    heavily_used: list[MemoryStats] = field(default_factory=list)
    contradicted: list[MemoryStats] = field(default_factory=list)
    marker_stats: list[MarkerStats] = field(default_factory=list)
    scope_distribution: dict[str, int] = field(default_factory=dict)
    # Per-scope curation pivot — "where is the rot concentrated?" Cheaper
    # than asking the model to fold scope_distribution and dead_weight
    # together every time.
    scope_health: list[ScopeHealth] = field(default_factory=list)
    # Singleton scopes that look like typos of another scope — flagged
    # only when there's a near neighbor (Levenshtein distance <= 2). A
    # singleton in isolation ("career", "personal-context") is usually
    # a legitimate narrow tag, not a misspell, so flagging every
    # singleton produced too many false positives in practice. The
    # neighbor check keeps the bucket actionable: if it fires, there's
    # almost always a real typo to fix.
    rare_scopes: list[str] = field(default_factory=list)
    # Use-events whose memory_id resolved to nothing (neither active nor
    # tombstoned). High counts hint at the model fabricating ULIDs in
    # `memory_record_use` — a quality signal worth surfacing.
    orphan_use_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": _iso(self.generated_at),
            "window_days": self.window_days,
            "total_active_memories": self.total_active_memories,
            "total_events": self.total_events,
            "distinct_sessions": self.distinct_sessions,
            "dead_weight": [s.to_dict() for s in self.dead_weight],
            "heavily_used": [s.to_dict() for s in self.heavily_used],
            "contradicted": [s.to_dict() for s in self.contradicted],
            "marker_stats": [m.to_dict() for m in self.marker_stats],
            "scope_distribution": dict(self.scope_distribution),
            "scope_health": [s.to_dict() for s in self.scope_health],
            "rare_scopes": list(self.rare_scopes),
            "orphan_use_events": self.orphan_use_events,
        }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def compute_health(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    heavily_used_min_applied: int = 3,
    now: datetime | None = None,
) -> HealthReport:
    """Build a `HealthReport` from active memories + the event stream.

    `events` is expected to be in chronological order — the function
    relies on that for "last_*" timestamps. `iter_all_events` returns
    archives + active log in chronological order; production callers
    should pass that directly.

    `window_days` controls the dead-weight cutoff: a memory is dead-weight
    if it was created more than `window_days` ago AND has no `applied`
    events. The window keeps recently-written memories from being flagged
    before they've had a chance to be retrieved.

    `heavily_used_min_applied` is the floor on `applied_count` for inclusion
    in `heavily_used`. Default 3: a single acknowledgement is acknowledgement,
    not a usage pattern, and the bucket is meant to surface memories that are
    actively load-bearing. Lower to 1 on a fresh store if you want to see
    everything that's been touched at least once. Always >= 1 — a value of
    0 would dump every memory into the bucket and defeat the report.
    """
    if heavily_used_min_applied < 1:
        heavily_used_min_applied = 1
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    by_id: dict[str, MemoryStats] = {}
    for m in memories:
        by_id[m.id] = MemoryStats(
            id=m.id,
            scopes=list(m.scopes),
            summary=first_summary_line(m.body),
            created=m.created,
            updated=m.updated,
            last_verified_at=m.last_verified_at,
        )

    # Marker stats are accumulated by canonical marker name. Both
    # `markers` (transient_warning fires) and `markers_acknowledged`
    # (committed-with-override) feed in.
    marker_fires: Counter[str] = Counter()
    marker_overrides: Counter[str] = Counter()

    sessions: set[str] = set()
    total_events = 0
    orphan_use_events = 0

    for ev in events:
        total_events += 1
        sess = ev.get("session")
        if sess:
            sessions.add(sess)

        kind = ev.get("kind")
        ts = _parse_ts(ev.get("ts"))

        if kind == "search":
            for mid in ev.get("returned", []) or []:
                stats = by_id.get(mid)
                if stats:
                    stats.retrieval_count += 1
        elif kind == "show":
            stats = by_id.get(ev.get("id", ""))
            if stats:
                stats.show_count += 1
        elif kind == "use":
            outcome = ev.get("outcome")
            for mid in ev.get("ids", []) or []:
                stats = by_id.get(mid)
                if stats is None:
                    # Memory may have been tombstoned after the use was
                    # recorded, or — more concerningly — the writer may
                    # have fabricated the ULID. We can't tell from the
                    # event alone (a tombstoned memory's id is a valid
                    # ULID just like any other), so we count both cases
                    # in `orphan_use_events`. A growing count is the
                    # "model is hallucinating ids" smoke test.
                    orphan_use_events += 1
                    continue
                if outcome == "applied":
                    stats.applied_count += 1
                elif outcome == "ignored":
                    stats.ignored_count += 1
                elif outcome == "contradicted":
                    stats.contradicted_count += 1
                    if ts is not None and (
                        stats.last_contradicted_at is None
                        or ts > stats.last_contradicted_at
                    ):
                        stats.last_contradicted_at = ts
                if ts is not None and (
                    stats.last_used_at is None or ts > stats.last_used_at
                ):
                    stats.last_used_at = ts
        elif kind == "write":
            for marker in ev.get("markers", []) or []:
                marker_fires[marker] += 1
            for marker in ev.get("markers_acknowledged", []) or []:
                marker_overrides[marker] += 1

    all_markers = sorted(set(marker_fires) | set(marker_overrides))
    marker_stats = [
        MarkerStats(
            marker=m,
            fire_count=marker_fires[m],
            override_count=marker_overrides[m],
        )
        for m in all_markers
    ]
    marker_stats.sort(key=lambda s: s.total, reverse=True)

    scope_distribution = Counter(
        scope for stats in by_id.values() for scope in stats.scopes
    )

    dead_weight = [
        s for s in by_id.values() if s.created < cutoff and s.applied_count == 0
    ]
    dead_weight.sort(key=lambda s: s.created)

    heavily_used = sorted(
        (s for s in by_id.values() if s.applied_count >= heavily_used_min_applied),
        key=lambda s: (s.applied_count, s.last_used_at or s.updated),
        reverse=True,
    )[:heavily_used_top_k]

    contradicted = [s for s in by_id.values() if s.has_unresolved_contradiction]
    contradicted.sort(key=lambda s: s.last_contradicted_at or s.updated, reverse=True)

    # Per-scope rollup. A memory tagged with N scopes is counted once per
    # scope — `sum(scope.active for scope in scope_health)` will exceed
    # `total_active_memories` when scopes overlap, which is the right shape
    # for "where is the rot concentrated?". We sort by total count
    # descending so the heaviest-trafficked scopes lead.
    scope_health_map: dict[str, ScopeHealth] = {}
    dead_ids = {s.id for s in dead_weight}
    contradicted_ids = {s.id for s in contradicted}
    for stats in by_id.values():
        for scope in stats.scopes:
            entry = scope_health_map.setdefault(scope, ScopeHealth(scope=scope))
            entry.active += 1
            entry.applied_total += stats.applied_count
            if stats.id in dead_ids:
                entry.dead += 1
            if stats.id in contradicted_ids:
                entry.contradicted += 1
    scope_health = sorted(
        scope_health_map.values(),
        key=lambda s: (-s.active, s.scope),
    )

    # Rare scopes — singletons that look like typos of another scope.
    # The heuristic used to flag every singleton, but most singletons
    # in practice are legitimate narrow tags ("career", "personal-context")
    # rather than misspells, and flagging them produced enough false
    # positives that the bucket stopped being actionable. The neighbor
    # check (Levenshtein distance <= 2 against any other scope, including
    # other singletons) restricts the bucket to scopes that almost
    # certainly *are* typos: "projct:foo" against an existing
    # "projects:foo", "tool" against "tools", "bug"/"bugs" pairs.
    all_scopes = list(scope_distribution.keys())
    rare_scopes = sorted(
        scope
        for scope, count in scope_distribution.items()
        if count == 1
        and any(
            other != scope and _edit_distance_within(scope, other, 2)
            for other in all_scopes
        )
    )

    return HealthReport(
        generated_at=now,
        window_days=window_days,
        total_active_memories=len(by_id),
        total_events=total_events,
        distinct_sessions=len(sessions),
        dead_weight=dead_weight,
        heavily_used=heavily_used,
        contradicted=contradicted,
        marker_stats=marker_stats,
        scope_distribution=dict(scope_distribution),
        scope_health=scope_health,
        rare_scopes=rare_scopes,
        orphan_use_events=orphan_use_events,
    )


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def render_text(report: HealthReport) -> str:
    """Pretty-print the report for the CLI subcommand. Plain text — no
    colour codes — so it pipes cleanly to a file."""
    lines: list[str] = []
    lines.append(
        f"Memory health — generated {_iso(report.generated_at)}, "
        f"window {report.window_days} days"
    )
    lines.append("=" * 70)
    lines.append(f"Active memories: {report.total_active_memories}")
    lines.append(f"Events seen:     {report.total_events}")
    lines.append(f"Sessions:        {report.distinct_sessions}")

    lines.append("")
    lines.append(
        f"Dead weight ({len(report.dead_weight)}) — never `applied`, older than {report.window_days} days:"
    )
    if not report.dead_weight:
        lines.append("  (none)")
    for s in report.dead_weight[:20]:
        lines.append(
            f"  {s.id} [retrievals={s.retrieval_count}] {','.join(s.scopes)}: {s.summary}"
        )
    if len(report.dead_weight) > 20:
        lines.append(f"  ... and {len(report.dead_weight) - 20} more")

    lines.append("")
    lines.append(f"Heavily used ({len(report.heavily_used)}):")
    if not report.heavily_used:
        lines.append("  (none)")
    for s in report.heavily_used:
        lines.append(
            f"  {s.id} [applied={s.applied_count}] {','.join(s.scopes)}: {s.summary}"
        )

    lines.append("")
    lines.append(f"Contradicted ({len(report.contradicted)}):")
    if not report.contradicted:
        lines.append("  (none)")
    for s in report.contradicted:
        lines.append(
            f"  {s.id} [contradicted={s.contradicted_count}] {','.join(s.scopes)}: {s.summary}"
        )

    lines.append("")
    lines.append(f"Marker stats ({len(report.marker_stats)}):")
    if not report.marker_stats:
        lines.append("  (none)")
    for m in report.marker_stats:
        rate_pct = round(m.override_rate * 100, 1)
        lines.append(
            f"  {m.marker:<24} fires={m.fire_count}  "
            f"overrides={m.override_count}  rate={rate_pct}%"
        )

    lines.append("")
    lines.append(f"Scope health ({len(report.scope_health)}):")
    if not report.scope_health:
        lines.append("  (none)")
    for sh in report.scope_health:
        lines.append(
            f"  {sh.scope:<28} active={sh.active:<3} dead={sh.dead:<3} "
            f"contradicted={sh.contradicted:<3} applied={sh.applied_total}"
        )

    lines.append("")
    lines.append(
        f"Rare scopes ({len(report.rare_scopes)}) — singletons within "
        "2 edits of another scope, likely typos:"
    )
    if not report.rare_scopes:
        lines.append("  (none)")
    for scope in report.rare_scopes:
        lines.append(f"  {scope}")

    if report.orphan_use_events:
        lines.append("")
        lines.append(
            f"Orphan use events: {report.orphan_use_events} — "
            "memory_record_use events whose ids resolved to neither active "
            "nor tombstoned memories. A growing count is the smoke test for "
            "fabricated ULIDs."
        )

    return "\n".join(lines) + "\n"


def render_json(report: HealthReport) -> str:
    return json.dumps(report.to_dict(), indent=2) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _edit_distance_within(a: str, b: str, max_dist: int) -> bool:
    """True iff Levenshtein(a, b) <= max_dist.

    Standard Wagner-Fischer DP, two-row variant. We don't need the
    actual distance — only whether it falls within the threshold —
    but scope names are short enough (typically <30 chars) that the
    full table is cheap and the early-exit machinery isn't worth its
    complexity. The length-difference shortcut catches the obviously
    far cases without running the table at all.

    Used by the `rare_scopes` neighbor check; lifted out as a helper
    so it stays testable in isolation if we ever need to tune the
    threshold or swap algorithms.
    """
    if abs(len(a) - len(b)) > max_dist:
        return False
    if a == b:
        return True
    # Ensure |a| <= |b| so the inner row stays small.
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for i, cb in enumerate(b, 1):
        curr = [i] + [0] * len(a)
        for j, ca in enumerate(a, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,
                prev[j] + 1,
                prev[j - 1] + cost,
            )
        prev = curr
    return prev[-1] <= max_dist


# ---------------------------------------------------------------------------
# Public façade for callers that already have a memory directory
# ---------------------------------------------------------------------------


def report_for_directory(
    root: Path,
    *,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    heavily_used_min_applied: int = 3,
    now: datetime | None = None,
) -> HealthReport:
    """Convenience: load memories from `root`, walk the event log, return
    the report. Used by both the MCP tool and the CLI subcommand."""
    from .store import Store

    store = Store(root)
    return compute_health(
        store.load_all(),
        iter_all_events(root),
        window_days=window_days,
        heavily_used_top_k=heavily_used_top_k,
        heavily_used_min_applied=heavily_used_min_applied,
        now=now,
    )


__all__ = [
    "MemoryStats",
    "MarkerStats",
    "HealthReport",
    "compute_health",
    "render_text",
    "render_json",
    "report_for_directory",
]
