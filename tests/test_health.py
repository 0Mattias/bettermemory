"""Unit tests for health.py — aggregating events + memories into the
HealthReport."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from bettermemory.health import (
    MarkerStats,
    compute_health,
    render_json,
    render_text,
    report_for_directory,
)
from bettermemory.models import (
    Confidence,
    Memory,
    Source,
    generate_ulid,
)


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _memory(
    *,
    body: str = "x",
    scopes: list[str] | None = None,
    created: datetime | None = None,
    updated: datetime | None = None,
) -> Memory:
    """Build a Memory record for testing without going through the store."""
    now = created or _utc(2026, 1, 1)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=updated or now,
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
    )


def _event(
    kind: str,
    *,
    ts: datetime | None = None,
    session: str = "sess_test",
    **fields: Any,
) -> dict[str, Any]:
    return {
        "ts": (ts or _utc(2026, 1, 1)).isoformat().replace("+00:00", "Z"),
        "session": session,
        "kind": kind,
        **fields,
    }


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_store_and_events() -> None:
    report = compute_health([], [], now=_utc(2026, 5, 1))
    assert report.total_active_memories == 0
    assert report.total_events == 0
    assert report.distinct_sessions == 0
    assert report.dead_weight == []
    assert report.heavily_used == []
    assert report.contradicted == []
    assert report.marker_stats == []


# ---------------------------------------------------------------------------
# Dead weight
# ---------------------------------------------------------------------------


def test_old_memory_with_no_applied_is_dead_weight() -> None:
    old = _memory(created=_utc(2026, 1, 1))
    report = compute_health(
        [old], [], window_days=30, now=_utc(2026, 5, 1)
    )
    assert len(report.dead_weight) == 1
    assert report.dead_weight[0].id == old.id


def test_recent_memory_with_no_applied_is_NOT_dead_weight() -> None:
    """Within the window — not enough time to judge."""
    fresh = _memory(created=_utc(2026, 4, 25))
    report = compute_health(
        [fresh], [], window_days=30, now=_utc(2026, 5, 1)
    )
    assert report.dead_weight == []


def test_old_memory_with_applied_event_is_NOT_dead_weight() -> None:
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _event(
            "use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="applied"
        ),
    ]
    report = compute_health(
        [m], events, window_days=30, now=_utc(2026, 5, 1)
    )
    assert report.dead_weight == []
    assert len(report.heavily_used) == 1


def test_dead_weight_sorted_by_created_ascending() -> None:
    a = _memory(created=_utc(2026, 1, 5))
    b = _memory(created=_utc(2026, 1, 1))
    c = _memory(created=_utc(2026, 1, 10))
    report = compute_health(
        [a, b, c], [], window_days=30, now=_utc(2026, 5, 1)
    )
    assert [s.id for s in report.dead_weight] == [b.id, a.id, c.id]


# ---------------------------------------------------------------------------
# Heavily used — top-k by applied count
# ---------------------------------------------------------------------------


def test_heavily_used_orders_by_applied_count() -> None:
    a = _memory()
    b = _memory()
    c = _memory()
    events = [
        _event("use", ids=[a.id], outcome="applied"),
        _event("use", ids=[b.id], outcome="applied"),
        _event("use", ids=[b.id], outcome="applied"),
        _event("use", ids=[c.id], outcome="applied"),
        _event("use", ids=[c.id], outcome="applied"),
        _event("use", ids=[c.id], outcome="applied"),
    ]
    report = compute_health(
        [a, b, c], events, now=_utc(2026, 5, 1)
    )
    assert [s.id for s in report.heavily_used] == [c.id, b.id, a.id]


def test_heavily_used_top_k_truncates() -> None:
    memories = [_memory() for _ in range(15)]
    events = [
        _event("use", ids=[m.id], outcome="applied") for m in memories
    ]
    report = compute_health(
        memories, events, heavily_used_top_k=5, now=_utc(2026, 5, 1)
    )
    assert len(report.heavily_used) == 5


# ---------------------------------------------------------------------------
# Contradicted — only unresolved
# ---------------------------------------------------------------------------


def test_contradiction_after_update_is_unresolved() -> None:
    m = _memory(created=_utc(2026, 1, 1), updated=_utc(2026, 1, 1))
    events = [
        _event(
            "use",
            ts=_utc(2026, 4, 1),
            ids=[m.id],
            outcome="contradicted",
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert len(report.contradicted) == 1
    assert report.contradicted[0].id == m.id


def test_contradiction_before_last_update_is_resolved() -> None:
    """memory_update bumps `updated`. A contradiction predating the
    update has been addressed; don't flag it."""
    m = _memory(created=_utc(2026, 1, 1), updated=_utc(2026, 4, 15))
    events = [
        _event(
            "use",
            ts=_utc(2026, 4, 1),
            ids=[m.id],
            outcome="contradicted",
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert report.contradicted == []


def test_contradicted_sorted_most_recent_first() -> None:
    a = _memory(created=_utc(2026, 1, 1))
    b = _memory(created=_utc(2026, 1, 1))
    events = [
        _event("use", ts=_utc(2026, 4, 1), ids=[a.id], outcome="contradicted"),
        _event("use", ts=_utc(2026, 4, 20), ids=[b.id], outcome="contradicted"),
    ]
    report = compute_health([a, b], events, now=_utc(2026, 5, 1))
    assert [s.id for s in report.contradicted] == [b.id, a.id]


# ---------------------------------------------------------------------------
# Counters wired into MemoryStats
# ---------------------------------------------------------------------------


def test_retrieval_count_from_search_returned_field() -> None:
    m = _memory()
    events = [
        _event("search", returned=[m.id], relevance=["high"]),
        _event("search", returned=[m.id], relevance=["medium"]),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    stats = next(s for s in report.dead_weight if s.id == m.id)
    assert stats.retrieval_count == 2


def test_show_count_increments() -> None:
    m = _memory()
    events = [
        _event("show", id=m.id),
        _event("show", id=m.id),
        _event("show", id=m.id),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    stats = next(s for s in report.dead_weight if s.id == m.id)
    assert stats.show_count == 3


def test_use_outcome_counters() -> None:
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _event("use", ids=[m.id], outcome="applied"),
        _event("use", ids=[m.id], outcome="applied"),
        _event("use", ids=[m.id], outcome="ignored"),
        _event(
            "use",
            ts=_utc(2026, 5, 1),
            ids=[m.id],
            outcome="contradicted",
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 2))
    stats = next(
        s
        for s in (
            report.heavily_used
            + report.contradicted
            + report.dead_weight
        )
        if s.id == m.id
    )
    assert stats.applied_count == 2
    assert stats.ignored_count == 1
    assert stats.contradicted_count == 1


# ---------------------------------------------------------------------------
# Marker stats
# ---------------------------------------------------------------------------


def test_marker_fires_and_overrides_aggregate() -> None:
    events = [
        _event("write", status="transient_warning", markers=["currently"]),
        _event("write", status="transient_warning", markers=["currently"]),
        _event("write", status="transient_warning", markers=["today i"]),
        _event(
            "write",
            status="committed",
            id=generate_ulid(),
            markers_acknowledged=["currently"],
        ),
    ]
    report = compute_health([], events, now=_utc(2026, 5, 1))
    by_marker = {m.marker: m for m in report.marker_stats}
    assert by_marker["currently"].fire_count == 2
    assert by_marker["currently"].override_count == 1
    assert by_marker["today i"].fire_count == 1
    assert by_marker["today i"].override_count == 0


def test_marker_override_rate() -> None:
    m = MarkerStats(marker="x", fire_count=8, override_count=2)
    assert m.override_rate == 0.2
    assert MarkerStats(marker="y", fire_count=0, override_count=0).override_rate == 0.0


# ---------------------------------------------------------------------------
# Sessions, scope distribution, total counts
# ---------------------------------------------------------------------------


def test_distinct_sessions_counted() -> None:
    events = [
        _event("show", session="sess_a", id="x"),
        _event("show", session="sess_a", id="y"),
        _event("show", session="sess_b", id="z"),
    ]
    report = compute_health([], events, now=_utc(2026, 5, 1))
    assert report.distinct_sessions == 2


def test_scope_distribution_counts_each_scope() -> None:
    a = _memory(scopes=["tools", "infra"])
    b = _memory(scopes=["tools"])
    c = _memory(scopes=["learning-style"])
    report = compute_health([a, b, c], [], now=_utc(2026, 5, 1))
    assert report.scope_distribution["tools"] == 2
    assert report.scope_distribution["infra"] == 1
    assert report.scope_distribution["learning-style"] == 1


def test_total_events_includes_every_record() -> None:
    events = [
        _event("show", id="x"),
        _event("write", status="committed", id="y"),
        _event("search", query="q", returned=[]),
    ]
    report = compute_health([], events, now=_utc(2026, 5, 1))
    assert report.total_events == 3


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_text_does_not_raise_on_empty() -> None:
    report = compute_health([], [], now=_utc(2026, 5, 1))
    text = render_text(report)
    assert "Memory health" in text
    assert "Active memories: 0" in text


def test_render_json_round_trips() -> None:
    m = _memory(created=_utc(2026, 1, 1))
    report = compute_health(
        [m],
        [_event("use", ids=[m.id], outcome="applied")],
        now=_utc(2026, 5, 1),
    )
    parsed = json.loads(render_json(report))
    assert parsed["total_active_memories"] == 1
    assert parsed["heavily_used"][0]["id"] == m.id


# ---------------------------------------------------------------------------
# report_for_directory — end-to-end against a real Store + event log
# ---------------------------------------------------------------------------


def test_report_for_directory_loads_store_and_events(
    memory_dir: Path,
) -> None:
    """Plumb compute_health through a real on-disk store and event log."""
    from bettermemory.events import Recorder
    from bettermemory.store import Store

    store = Store(memory_dir)
    rec = Recorder(root=memory_dir, session_id="sess_test")
    m = store.write(content="durable fact", scopes=["tools"])
    rec.record("search", query="anything", returned=[m.id], relevance=["high"])
    rec.record("use", ids=[m.id], outcome="applied")

    report = report_for_directory(memory_dir)
    assert report.total_active_memories == 1
    assert len(report.heavily_used) == 1
    assert report.heavily_used[0].id == m.id
    assert report.heavily_used[0].applied_count == 1
