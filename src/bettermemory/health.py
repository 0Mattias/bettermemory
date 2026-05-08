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
- **contradicted**: memories with a `contradicted` use event whose
  timestamp is after the memory's `updated`. Either correct via
  `memory_update` or tombstone.
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
        """True if there's been a contradiction since the memory was last
        updated. memory_update bumps `updated`, so a refresh resolves the
        contradiction signal."""
        if self.last_contradicted_at is None:
            return False
        return self.last_contradicted_at > self.updated

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
                    # recorded — skip silently. The event is still in the
                    # log for the curious; we just can't attach it to an
                    # active record.
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
