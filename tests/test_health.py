"""Unit tests for health.py — aggregating events + memories into the
HealthReport."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from bettermemory.health import (
    MarkerStats,
    _edit_distance_within,
    compute_health,
    curation_counts,
    render_json,
    render_text,
    report_for_directory,
)
from bettermemory.models import (
    Category,
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
    last_verified_at: datetime | None = None,
    category: Category | None = None,
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
        last_verified_at=last_verified_at,
        category=category,
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
# Dead weight (new definition: retrieved>0 AND applied=0) + Cold memories
# ---------------------------------------------------------------------------


def test_old_memory_with_retrievals_but_no_applied_is_dead_weight() -> None:
    """The new dead-weight rule: the memory IS being retrieved but the
    model is never recording `applied`. That's the actionable signal."""
    old = _memory(created=_utc(2026, 1, 1))
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[old.id]),
        _event("search", ts=_utc(2026, 4, 5), returned=[old.id]),
    ]
    report = compute_health([old], events, window_days=30, now=_utc(2026, 5, 1))
    assert len(report.dead_weight) == 1
    assert report.dead_weight[0].id == old.id
    assert report.cold_memories == []


def test_old_memory_never_retrieved_is_cold_not_dead() -> None:
    """Under the new rule, a memory with zero retrievals is cold, not
    dead — the ranker isn't surfacing it, which is a different
    curation question than dead-weight (which is "model retrieves but
    never applies")."""
    old = _memory(created=_utc(2026, 1, 1))
    report = compute_health([old], [], window_days=30, now=_utc(2026, 5, 1))
    assert report.dead_weight == []
    assert len(report.cold_memories) == 1
    assert report.cold_memories[0].id == old.id


def test_recent_memory_with_no_events_is_NOT_dead_or_cold() -> None:
    """Within the window — not enough time to judge."""
    fresh = _memory(created=_utc(2026, 4, 25))
    report = compute_health([fresh], [], window_days=30, now=_utc(2026, 5, 1))
    assert report.dead_weight == []
    assert report.cold_memories == []


def test_old_memory_with_applied_event_is_NOT_dead_or_cold() -> None:
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _event("search", ts=_utc(2026, 3, 1), returned=[m.id]),
        _event("use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="applied"),
    ]
    # Lower the threshold so a single application still surfaces — this
    # test is about the dead-weight rule, not the heavily_used one.
    report = compute_health(
        [m],
        events,
        window_days=30,
        heavily_used_min_applied=1,
        now=_utc(2026, 5, 1),
    )
    assert report.dead_weight == []
    assert report.cold_memories == []
    assert len(report.heavily_used) == 1


def test_dead_weight_sorted_by_created_ascending() -> None:
    a = _memory(created=_utc(2026, 1, 5))
    b = _memory(created=_utc(2026, 1, 1))
    c = _memory(created=_utc(2026, 1, 10))
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[a.id]),
        _event("search", ts=_utc(2026, 4, 1), returned=[b.id]),
        _event("search", ts=_utc(2026, 4, 1), returned=[c.id]),
    ]
    report = compute_health([a, b, c], events, window_days=30, now=_utc(2026, 5, 1))
    assert [s.id for s in report.dead_weight] == [b.id, a.id, c.id]


def test_cold_memories_sorted_by_created_ascending() -> None:
    a = _memory(created=_utc(2026, 1, 5))
    b = _memory(created=_utc(2026, 1, 1))
    c = _memory(created=_utc(2026, 1, 10))
    report = compute_health([a, b, c], [], window_days=30, now=_utc(2026, 5, 1))
    assert [s.id for s in report.cold_memories] == [b.id, a.id, c.id]


def test_ambient_excluded_from_dead_weight() -> None:
    """Ambient memories shape responses without being cited; the use
    signal is structurally absent there. They must NEVER land in
    dead_weight, regardless of retrieval/applied counts."""
    m = _memory(
        created=_utc(2026, 1, 1),
        category=Category.AMBIENT,
    )
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[m.id]),
        _event("search", ts=_utc(2026, 4, 5), returned=[m.id]),
    ]
    report = compute_health([m], events, window_days=30, now=_utc(2026, 5, 1))
    assert report.dead_weight == []


def test_ambient_excluded_from_cold_memories() -> None:
    """Mirror test for the cold bucket — same exclusion principle."""
    m = _memory(created=_utc(2026, 1, 1), category=Category.AMBIENT)
    report = compute_health([m], [], window_days=30, now=_utc(2026, 5, 1))
    assert report.cold_memories == []


def test_fact_category_treated_like_legacy_for_buckets() -> None:
    """A memory with category=FACT participates in dead/cold like a
    legacy memory (where category is None)."""
    legacy = _memory(created=_utc(2026, 1, 1))  # category is None
    fact = _memory(created=_utc(2026, 1, 1), category=Category.FACT)
    report = compute_health([legacy, fact], [], window_days=30, now=_utc(2026, 5, 1))
    assert {s.id for s in report.cold_memories} == {legacy.id, fact.id}


def test_scope_health_includes_cold_count() -> None:
    """The per-scope rollup gets a `cold` field paralleling `dead`."""
    a = _memory(created=_utc(2026, 1, 1), scopes=["tools"])
    b = _memory(created=_utc(2026, 1, 1), scopes=["tools"])
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[a.id]),
    ]
    report = compute_health([a, b], events, window_days=30, now=_utc(2026, 5, 1))
    sh = next(s for s in report.scope_health if s.scope == "tools")
    assert sh.dead == 1
    assert sh.cold == 1
    assert sh.active == 2


def test_health_to_dict_carries_cold_memories_key() -> None:
    """The serialised JSON shape must expose cold_memories so external
    consumers can read it without re-deriving."""
    m = _memory(created=_utc(2026, 1, 1))
    report = compute_health([m], [], window_days=30, now=_utc(2026, 5, 1))
    payload = report.to_dict()
    assert "cold_memories" in payload
    assert len(payload["cold_memories"]) == 1


def test_render_text_shows_cold_memories_section() -> None:
    """CLI rendering surfaces the new bucket."""
    m = _memory(created=_utc(2026, 1, 1))
    report = compute_health([m], [], window_days=30, now=_utc(2026, 5, 1))
    text = render_text(report)
    assert "Cold memories" in text


# ---------------------------------------------------------------------------
# curation_counts — fast helper used by memory_scope_overview
# ---------------------------------------------------------------------------


def test_curation_counts_zero_on_empty_store() -> None:
    out = curation_counts([], [], window_days=30, now=_utc(2026, 5, 1))
    assert out == {
        "stale": 0,
        "never_verified": 0,
        "drifted": 0,
        "cold": 0,
        "dead": 0,
        "silent_misses": 0,
        "endorsement_debt": 0,
    }


def test_curation_counts_matches_compute_health_buckets() -> None:
    """Numerical contract: counts agree with bucket sizes from
    compute_health over the same inputs."""
    cold = _memory(created=_utc(2026, 1, 1))
    dead = _memory(created=_utc(2026, 1, 1))
    fresh_verified = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 4, 25),
    )
    never = _memory(created=_utc(2026, 4, 1))  # never_verified, recent
    stale_v = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 1, 5),  # stale at threshold 30
    )
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[dead.id]),
    ]
    mems = [cold, dead, fresh_verified, never, stale_v]
    report = compute_health(
        mems,
        events,
        window_days=30,
        verification_stale_days=30,
        now=_utc(2026, 5, 1),
    )
    counts = curation_counts(
        mems,
        events,
        window_days=30,
        verification_stale_days=30,
        now=_utc(2026, 5, 1),
    )
    assert counts["dead"] == len(report.dead_weight)
    assert counts["cold"] == len(report.cold_memories)
    assert counts["never_verified"] == report.verification_debt.never_verified_total
    assert counts["stale"] == report.verification_debt.stale_total


def test_curation_counts_excludes_ambient_from_dead_and_cold() -> None:
    cold = _memory(created=_utc(2026, 1, 1), category=Category.AMBIENT)
    dead = _memory(created=_utc(2026, 1, 1), category=Category.AMBIENT)
    events = [_event("search", ts=_utc(2026, 4, 1), returned=[dead.id])]
    counts = curation_counts([cold, dead], events, window_days=30, now=_utc(2026, 5, 1))
    assert counts["dead"] == 0
    assert counts["cold"] == 0


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
    # Threshold lowered so all three rank — the test is about ordering.
    report = compute_health(
        [a, b, c],
        events,
        heavily_used_min_applied=1,
        now=_utc(2026, 5, 1),
    )
    assert [s.id for s in report.heavily_used] == [c.id, b.id, a.id]


def test_heavily_used_top_k_truncates() -> None:
    memories = [_memory() for _ in range(15)]
    events = [_event("use", ids=[m.id], outcome="applied") for m in memories]
    report = compute_health(
        memories,
        events,
        heavily_used_top_k=5,
        heavily_used_min_applied=1,
        now=_utc(2026, 5, 1),
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


def test_contradiction_resolved_by_later_verify() -> None:
    """memory_verify after a contradiction is the second resolution path:
    the body wasn't changed, but the user spot-checked reality and
    confirmed the body still matches despite the contradiction event.
    Treat as resolved — the contradicted bucket should not include it."""
    m = _memory(
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 4, 15),  # AFTER the contradiction below
    )
    events = [
        _event("use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="contradicted"),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert report.contradicted == []


def test_contradiction_after_verify_remains_unresolved() -> None:
    """A verify that *predates* the contradiction is not a resolution
    — the contradiction is the most recent signal and outranks an
    earlier spot-check. Without a *subsequent* update or verify, the
    flag stays set."""
    m = _memory(
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 3, 1),  # BEFORE the contradiction below
    )
    events = [
        _event("use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="contradicted"),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert len(report.contradicted) == 1
    assert report.contradicted[0].id == m.id


def test_contradiction_resolved_by_update_even_if_verify_predates_it() -> None:
    """The two resolution paths are independent: an `updated` newer
    than the contradiction clears the flag regardless of where
    `last_verified_at` sits."""
    m = _memory(
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 4, 15),  # AFTER the contradiction
        last_verified_at=_utc(2026, 3, 1),  # BEFORE the contradiction
    )
    events = [
        _event("use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="contradicted"),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert report.contradicted == []


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
    # show events alone don't bump retrieval_count, so under the new rule
    # this memory is `cold` (created old + zero retrievals), not dead.
    stats = next(s for s in report.cold_memories if s.id == m.id)
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
        _event("use", ids=[m.id], outcome="corrected"),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 2))
    stats = next(
        s
        for s in (
            report.heavily_used
            + report.contradicted
            + report.dead_weight
            + report.cold_memories
        )
        if s.id == m.id
    )
    assert stats.applied_count == 2
    assert stats.ignored_count == 1
    assert stats.contradicted_count == 1
    assert stats.corrected_count == 1


def test_corrected_does_not_raise_contradiction_flag() -> None:
    """`corrected` is the audit-only outcome for the
    noticed-and-fixed-inline workflow: the caller has already run
    memory_update / memory_verify before recording the use event.
    A `corrected` event must not push `last_contradicted_at` forward,
    because doing so would re-create the exact stuck-flag failure
    mode the new outcome was added to fix.
    """
    m = _memory(
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 4, 14),
        last_verified_at=_utc(2026, 4, 15),
    )
    events = [
        # The audit log entry lands AFTER the resolution events. With
        # the old `contradicted` outcome this would keep the flag set
        # because event ts > last_verified_at; with `corrected` it
        # must not.
        _event(
            "use",
            ts=_utc(2026, 4, 16),
            ids=[m.id],
            outcome="corrected",
            note="noticed drift mid-turn, ran memory_update + memory_verify",
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert report.contradicted == []
    # The counter still increments even though the flag stays clear —
    # otherwise a curation pass loses sight of how often a memory
    # has needed inline repair.
    stats = next(
        s
        for s in (
            report.heavily_used
            + report.contradicted
            + report.dead_weight
            + report.cold_memories
        )
        if s.id == m.id
    )
    assert stats.corrected_count == 1
    assert stats.contradicted_count == 0


def test_corrected_after_genuine_contradiction_clears_flag_only_via_update_or_verify() -> (
    None
):
    """A real contradicted event followed by a corrected event does
    NOT clear the unresolved flag — `corrected` is audit signal, not
    a resolution path. The actual resolution paths remain
    memory_update and memory_verify (whose timestamps live on the
    memory record, not in the event log). Recording `corrected`
    without a prior update/verify is a caller error; we don't try
    to silently paper over it.
    """
    m = _memory(
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),  # never updated since
        last_verified_at=None,  # never verified
    )
    events = [
        _event("use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="contradicted"),
        _event("use", ts=_utc(2026, 4, 2), ids=[m.id], outcome="corrected"),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert len(report.contradicted) == 1, (
        "corrected event without a real update/verify must not clear "
        "the flag — otherwise the outcome becomes a free pass."
    )


# ---------------------------------------------------------------------------
# Resolution timeline — chronological event log on contradicted rows
# ---------------------------------------------------------------------------


def test_resolution_timeline_attached_to_contradicted_rows() -> None:
    """A row in the contradicted bucket carries the chronological log of
    its resolution-relevant events (update / verify / contradicted /
    corrected). The model uses this to self-diagnose stuck-flag cases
    without grepping `.events.jsonl` by hand."""
    m = _memory(created=_utc(2026, 1, 1), updated=_utc(2026, 1, 1))
    events = [
        _event("update", ts=_utc(2026, 4, 1), id=m.id),
        _event(
            "verify",
            ts=_utc(2026, 4, 2),
            id=m.id,
            note="confirmed",
        ),
        _event(
            "use",
            ts=_utc(2026, 4, 3),
            ids=[m.id],
            outcome="contradicted",
            note="logged after the fix — this is the stuck-flag pattern",
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert len(report.contradicted) == 1
    timeline = report.contradicted[0].resolution_timeline
    kinds = [entry["kind"] for entry in timeline]
    assert kinds == ["update", "verify", "contradicted"]
    # Notes pass through; missing notes render as None rather than being
    # dropped (the kind alone is informative).
    assert timeline[1]["note"] == "confirmed"
    assert timeline[0]["note"] is None
    assert "stuck-flag" in timeline[2]["note"]


def test_resolution_timeline_empty_for_non_contradicted_rows() -> None:
    """The timeline is opt-in — only contradicted rows carry it. Other
    rows keep the field empty so the JSON output stays compact for the
    common case where the bucket is clean."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _event("use", ids=[m.id], outcome="applied"),
        _event("update", ts=_utc(2026, 2, 1), id=m.id),
    ]
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert report.contradicted == []
    assert len(report.heavily_used) == 1
    assert report.heavily_used[0].resolution_timeline == []


def test_resolution_timeline_includes_corrected_events() -> None:
    """A `corrected` event lives in the audit trail too, even though it
    doesn't drive the flag. If a memory ends up contradicted later via
    a different event, the timeline shows the full history."""
    m = _memory(
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        last_verified_at=None,
    )
    events = [
        _event(
            "use",
            ts=_utc(2026, 4, 1),
            ids=[m.id],
            outcome="corrected",
            note="early audit fix",
        ),
        _event(
            "use",
            ts=_utc(2026, 4, 20),
            ids=[m.id],
            outcome="contradicted",
            note="this one is real",
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert len(report.contradicted) == 1
    timeline = report.contradicted[0].resolution_timeline
    assert [e["kind"] for e in timeline] == ["corrected", "contradicted"]


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
# Verification debt — never / stale / fresh rollup
# ---------------------------------------------------------------------------


def test_verification_debt_partitions_active_memories() -> None:
    """Every active memory ends up in exactly one of the three buckets:
    never_verified (last_verified_at is None), stale (verified more
    than `verification_stale_days` ago), or fresh (verified within the
    window). The three counts must sum to total_active_memories — the
    invariant the curation pass relies on to read percentages without
    re-counting."""
    never = _memory(created=_utc(2026, 1, 1), last_verified_at=None)
    stale = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 2, 1),  # 90 days before now
    )
    fresh = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 4, 25),  # 6 days before now
    )
    report = compute_health(
        [never, stale, fresh],
        [],
        verification_stale_days=30,
        now=_utc(2026, 5, 1),
    )
    debt = report.verification_debt
    assert debt.never_verified_total == 1
    assert debt.stale_total == 1
    assert debt.fresh_count == 1
    assert (
        debt.never_verified_total + debt.stale_total + debt.fresh_count
        == report.total_active_memories
    )
    assert {s.id for s in debt.never_verified} == {never.id}
    assert {s.id for s in debt.stale} == {stale.id}


def test_verification_debt_sorts_oldest_first() -> None:
    """never_verified rows sort by `created` ascending (oldest first —
    that's the highest-risk because the body has had the most time to
    drift). stale rows sort by `last_verified_at` ascending for the
    same reason."""
    young_never = _memory(created=_utc(2026, 4, 1), last_verified_at=None)
    old_never = _memory(created=_utc(2026, 1, 1), last_verified_at=None)
    recent_stale = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 3, 1),
    )
    ancient_stale = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 1, 15),
    )
    report = compute_health(
        [young_never, old_never, recent_stale, ancient_stale],
        [],
        verification_stale_days=30,
        now=_utc(2026, 5, 1),
    )
    debt = report.verification_debt
    assert [s.id for s in debt.never_verified] == [old_never.id, young_never.id]
    assert [s.id for s in debt.stale] == [ancient_stale.id, recent_stale.id]


def test_verification_debt_threshold_respected() -> None:
    """The staleness boundary is exactly `verification_stale_days` —
    a memory verified at the boundary is fresh, one verified just
    before it is stale."""
    boundary = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 4, 1),  # exactly 30 days before now
    )
    just_past = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 3, 31),  # 31 days
    )
    report = compute_health(
        [boundary, just_past],
        [],
        verification_stale_days=30,
        now=_utc(2026, 5, 1),
    )
    # `last_verified_at < verification_cutoff` is the stale predicate;
    # the boundary case is on the fresh side.
    assert {s.id for s in report.verification_debt.stale} == {just_past.id}
    assert report.verification_debt.fresh_count == 1


def test_verification_debt_caps_row_lists_at_20() -> None:
    """When the buckets blow past the cap, the inline row lists are
    truncated to keep JSON output bounded, while the totals stay
    uncapped so a downstream reader can tell '5 stale' from '500 stale'
    without re-counting."""
    many = [
        _memory(created=_utc(2026, 1, i + 1), last_verified_at=None) for i in range(25)
    ]
    report = compute_health(many, [], now=_utc(2026, 5, 1))
    debt = report.verification_debt
    assert debt.never_verified_total == 25
    assert len(debt.never_verified) == 20  # capped


def test_verification_debt_to_dict_shape() -> None:
    """JSON shape is stable: `{stale_after_days, *_total, fresh_count,
    never_verified, stale}`. Asserting the shape so downstream consumers
    don't drift relative to it without us noticing."""
    m = _memory(created=_utc(2026, 1, 1), last_verified_at=None)
    report = compute_health([m], [], now=_utc(2026, 5, 1))
    payload = report.to_dict()["verification_debt"]
    assert set(payload) == {
        "stale_after_days",
        "never_verified_total",
        "stale_total",
        "fresh_count",
        "never_verified",
        "stale",
    }
    assert payload["never_verified_total"] == 1
    assert len(payload["never_verified"]) == 1


def test_verification_debt_render_text_section_present() -> None:
    """The CLI renderer surfaces the debt section. We don't pin exact
    formatting; just confirm the section appears with the relevant
    counts so a human running `bettermemory health` sees it."""
    m = _memory(created=_utc(2026, 1, 1), last_verified_at=None)
    report = compute_health([m], [], now=_utc(2026, 5, 1))
    text = render_text(report)
    assert "Verification debt" in text
    assert "never=1" in text


def test_verification_debt_empty_store() -> None:
    """An empty store returns a zeroed bucket — no exceptions, no
    div-by-zero, just the default-shape bucket so callers can render
    the section unconditionally."""
    report = compute_health([], [], now=_utc(2026, 5, 1))
    debt = report.verification_debt
    assert debt.never_verified_total == 0
    assert debt.stale_total == 0
    assert debt.fresh_count == 0
    assert debt.never_verified == []
    assert debt.stale == []


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
    # New sections render even when empty.
    assert "Scope health" in text
    assert "Rare scopes" in text


# ---------------------------------------------------------------------------
# Scope health pivot, rare scopes, orphan use events
# ---------------------------------------------------------------------------


def test_scope_health_counts_active_per_scope() -> None:
    a = _memory(scopes=["tools"])
    b = _memory(scopes=["tools", "infrastructure"])
    c = _memory(scopes=["infrastructure"])
    report = compute_health([a, b, c], [], now=_utc(2026, 5, 1))
    by_scope = {sh.scope: sh for sh in report.scope_health}
    assert by_scope["tools"].active == 2
    assert by_scope["infrastructure"].active == 2


def test_scope_health_counts_dead_per_scope() -> None:
    """A memory created beyond `window_days` ago with retrievals but no
    `applied` is dead in every scope it carries."""
    old_a = _memory(scopes=["tools"], created=_utc(2026, 1, 1))
    old_b = _memory(scopes=["tools"], created=_utc(2026, 1, 1))
    fresh = _memory(scopes=["tools"], created=_utc(2026, 4, 30))
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[old_a.id]),
        _event("search", ts=_utc(2026, 4, 1), returned=[old_b.id]),
    ]
    report = compute_health(
        [old_a, old_b, fresh], events, window_days=30, now=_utc(2026, 5, 1)
    )
    by_scope = {sh.scope: sh for sh in report.scope_health}
    assert by_scope["tools"].active == 3
    assert by_scope["tools"].dead == 2
    assert by_scope["tools"].cold == 0


def test_scope_health_counts_contradictions_per_scope() -> None:
    a = _memory(scopes=["projects:foo"], updated=_utc(2026, 1, 1))
    events = [
        _event(
            "use",
            ts=_utc(2026, 2, 1),
            ids=[a.id],
            outcome="contradicted",
        ),
    ]
    report = compute_health([a], events, now=_utc(2026, 5, 1))
    by_scope = {sh.scope: sh for sh in report.scope_health}
    assert by_scope["projects:foo"].contradicted == 1


def test_scope_health_sums_applied_per_scope() -> None:
    a = _memory(scopes=["tools"])
    b = _memory(scopes=["tools"])
    events = [
        _event("use", ids=[a.id], outcome="applied"),
        _event("use", ids=[a.id], outcome="applied"),
        _event("use", ids=[b.id], outcome="applied"),
    ]
    report = compute_health([a, b], events, now=_utc(2026, 5, 1))
    by_scope = {sh.scope: sh for sh in report.scope_health}
    assert by_scope["tools"].applied_total == 3


def test_scope_health_sorted_by_active_desc() -> None:
    """Heavier-trafficked scopes lead — easier visual scan during curation."""
    big_a = _memory(scopes=["tools"])
    big_b = _memory(scopes=["tools"])
    big_c = _memory(scopes=["tools"])
    small = _memory(scopes=["career"])
    report = compute_health([big_a, big_b, big_c, small], [], now=_utc(2026, 5, 1))
    scopes_in_order = [sh.scope for sh in report.scope_health]
    assert scopes_in_order[0] == "tools"


def test_rare_scopes_surfaces_singleton_with_near_neighbor() -> None:
    """A singleton at small edit distance (<= 2) from another scope is
    almost always a typo and gets flagged. `projct:foo` is two
    deletions away from `projects:foo` — the bucket's job is to surface
    exactly this case."""
    a = _memory(scopes=["projects:foo"])
    b = _memory(scopes=["projects:foo"])
    typo = _memory(scopes=["projct:foo"])  # two deletions from projects:foo
    report = compute_health([a, b, typo], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == ["projct:foo"]


def test_rare_scopes_excludes_repeated() -> None:
    a = _memory(scopes=["tools"])
    b = _memory(scopes=["tools"])
    report = compute_health([a, b], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == []


def test_rare_scopes_excludes_singleton_with_no_near_neighbor() -> None:
    """A legitimate narrow singleton — no scope within 2 edits — is
    not flagged. This is the false-positive fix: scopes like
    `personal-context` or `career` are intentionally narrow and should
    not be mistaken for typos just because they happen to be n=1."""
    a = _memory(scopes=["tools"])
    b = _memory(scopes=["tools"])
    standalone = _memory(scopes=["career"])  # far from "tools"
    report = compute_health([a, b, standalone], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == []


def test_rare_scopes_flags_two_close_singletons() -> None:
    """Two singletons at small edit distance flag each other. The
    curator decides which is canonical; the report's job is just to
    make the pair visible."""
    a = _memory(scopes=["bug"])
    b = _memory(scopes=["bugs"])  # distance 1 from "bug"
    report = compute_health([a, b], [], now=_utc(2026, 5, 1))
    assert sorted(report.rare_scopes) == ["bug", "bugs"]


def test_rare_scopes_distance_three_not_flagged() -> None:
    """Edit distance 3 isn't 'typo' territory anymore — flagging at
    distance 3+ would re-introduce the false-positive noise the
    neighbor check exists to suppress. `bug` -> `xyz` is 3 substitutions."""
    a = _memory(scopes=["bug"])
    b = _memory(scopes=["bug"])
    far = _memory(scopes=["xyz"])  # 3 substitutions away from "bug"
    report = compute_health([a, b, far], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == []


def test_edit_distance_within_threshold_cases() -> None:
    """Tight unit tests on the helper that backs the rare_scopes
    neighbor check, so a regression in the distance function shows up
    here rather than leaking through as a noisy rare_scopes report.
    Covers identical strings, the length-difference shortcut, distances
    1 and 2 (substitution / insertion / deletion / mixed), the at/just-
    over-threshold boundary, and an empty-string edge."""
    # Identical → distance 0, within any non-negative threshold.
    assert _edit_distance_within("tools", "tools", 0) is True
    assert _edit_distance_within("tools", "tools", 2) is True

    # Length-difference shortcut: |len(a) - len(b)| > max_dist → False
    # without running the table.
    assert _edit_distance_within("a", "abcdef", 2) is False

    # Distance 1: single substitution / insertion / deletion.
    assert _edit_distance_within("bag", "bug", 1) is True  # sub
    assert _edit_distance_within("bug", "bugs", 1) is True  # ins
    assert _edit_distance_within("bugs", "bug", 1) is True  # del

    # Distance 2: two edits, mixed kinds.
    assert _edit_distance_within("projects:foo", "projct:foo", 2) is True

    # At threshold: distance == max_dist returns True (inclusive bound).
    # `cat` -> `bag`: c→b, a→a, t→g — 2 substitutions, distance 2.
    assert _edit_distance_within("cat", "bag", 2) is True

    # Just over threshold: distance 3 against max_dist 2 returns False.
    # `bug` -> `xyz`: 3 substitutions, distance 3.
    assert _edit_distance_within("bug", "xyz", 2) is False

    # Empty string against an N-char string has distance N.
    assert _edit_distance_within("", "ab", 2) is True
    assert _edit_distance_within("", "abc", 2) is False
    assert _edit_distance_within("", "", 0) is True


def test_rare_scopes_neighbor_can_be_high_count_or_singleton() -> None:
    """The neighbor a singleton matches against can itself be either
    a multi-count scope (the typical typo-of-a-real-scope case) or
    another singleton (the typo-pair case). The fixture covers both
    in one shot — `tool` matches the high-count `tools`, `bug` and
    `bugs` match each other."""
    a = _memory(scopes=["tools"])
    b = _memory(scopes=["tools"])
    typo_of_high_count = _memory(scopes=["tool"])
    pair_a = _memory(scopes=["bug"])
    pair_b = _memory(scopes=["bugs"])
    standalone = _memory(scopes=["career"])
    report = compute_health(
        [a, b, typo_of_high_count, pair_a, pair_b, standalone],
        [],
        now=_utc(2026, 5, 1),
    )
    assert sorted(report.rare_scopes) == ["bug", "bugs", "tool"]


def test_orphan_use_events_count_unknown_ids() -> None:
    """A memory_record_use referencing a fabricated/unknown ULID gets
    counted in `orphan_use_events`. The count is the smoke test for
    model-side hallucination."""
    a = _memory()
    events = [
        _event("use", ids=[a.id], outcome="applied"),  # known
        _event("use", ids=[generate_ulid()], outcome="applied"),  # orphan
        _event(
            "use", ids=[generate_ulid(), generate_ulid()], outcome="ignored"
        ),  # 2 orphans
    ]
    report = compute_health([a], events, now=_utc(2026, 5, 1))
    assert report.orphan_use_events == 3


def test_orphan_use_events_zero_when_all_ids_known() -> None:
    a = _memory()
    events = [_event("use", ids=[a.id], outcome="applied")]
    report = compute_health([a], events, now=_utc(2026, 5, 1))
    assert report.orphan_use_events == 0


def test_orphan_use_events_excludes_tombstoned_ids() -> None:
    """Use events referencing tombstoned ids are NOT orphans — the memory
    existed when used; it was just removed later. Without this filter the
    "model is hallucinating ids" smoke test fires on every benign
    tombstone-after-use lifecycle, drowning out the real fabrication
    signal."""
    a = _memory()
    tombstoned_id = generate_ulid()
    fabricated_id = generate_ulid()
    events = [
        _event("use", ids=[a.id], outcome="applied"),  # known active
        _event("use", ids=[tombstoned_id], outcome="applied"),  # benign
        _event("use", ids=[fabricated_id], outcome="applied"),  # concerning
    ]
    report = compute_health(
        [a],
        events,
        now=_utc(2026, 5, 1),
        tombstoned_ids={tombstoned_id},
    )
    # Only the truly-unknown id counts.
    assert report.orphan_use_events == 1


def test_orphan_use_events_legacy_behavior_when_tombstones_unset() -> None:
    """Callers that don't pass `tombstoned_ids` get the old conflated
    behavior (every unknown id is an orphan) — backward compatibility
    for offline tooling that doesn't load the tombstone set."""
    a = _memory()
    tombstoned_id = generate_ulid()
    events = [_event("use", ids=[tombstoned_id], outcome="applied")]
    report = compute_health([a], events, now=_utc(2026, 5, 1))
    assert report.orphan_use_events == 1


def test_render_text_includes_orphan_section_when_nonzero() -> None:
    a = _memory()
    events = [_event("use", ids=[generate_ulid()], outcome="applied")]
    report = compute_health([a], events, now=_utc(2026, 5, 1))
    text = render_text(report)
    assert "Orphan use events: 1" in text


def test_render_text_omits_orphan_section_when_zero() -> None:
    """When the count is zero we don't print the section — keeps the
    happy-path report shorter and the smoke-test signal more salient
    when it does appear."""
    report = compute_health([], [], now=_utc(2026, 5, 1))
    text = render_text(report)
    assert "Orphan use events" not in text


def test_render_json_round_trips() -> None:
    m = _memory(created=_utc(2026, 1, 1))
    report = compute_health(
        [m],
        [_event("use", ids=[m.id], outcome="applied")],
        heavily_used_min_applied=1,
        now=_utc(2026, 5, 1),
    )
    parsed = json.loads(render_json(report))
    assert parsed["total_active_memories"] == 1
    assert parsed["heavily_used"][0]["id"] == m.id


# ---------------------------------------------------------------------------
# report_for_directory — end-to-end against a real Store + event log
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# heavily_used_min_applied threshold
# ---------------------------------------------------------------------------


def test_heavily_used_default_threshold_excludes_single_applies() -> None:
    """The default threshold is 3 — one acknowledgement is acknowledgement,
    not a usage pattern, and the heavily_used bucket is meant to surface
    repeat-use signal."""
    a = _memory()
    b = _memory()
    events = [
        _event("use", ids=[a.id], outcome="applied"),  # 1
        _event("use", ids=[b.id], outcome="applied"),  # 1
        _event("use", ids=[b.id], outcome="applied"),  # 2
    ]
    report = compute_health([a, b], events, now=_utc(2026, 5, 1))
    assert report.heavily_used == []


def test_heavily_used_default_threshold_includes_three_applies() -> None:
    a = _memory()
    events = [_event("use", ids=[a.id], outcome="applied") for _ in range(3)]
    report = compute_health([a], events, now=_utc(2026, 5, 1))
    assert len(report.heavily_used) == 1
    assert report.heavily_used[0].id == a.id
    assert report.heavily_used[0].applied_count == 3


def test_heavily_used_min_applied_one_includes_singletons() -> None:
    """A young store may want to see anything that's been applied at all
    — explicit min_applied=1 reproduces the pre-threshold behavior."""
    a = _memory()
    events = [_event("use", ids=[a.id], outcome="applied")]
    report = compute_health(
        [a], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert len(report.heavily_used) == 1


def test_heavily_used_min_applied_clamped_to_one() -> None:
    """A 0 threshold would dump every memory — clamp up to 1 so the
    bucket stays meaningful even on a misconfigured client."""
    a = _memory()  # never applied
    b = _memory()
    events = [_event("use", ids=[b.id], outcome="applied")]
    report = compute_health(
        [a, b], events, heavily_used_min_applied=0, now=_utc(2026, 5, 1)
    )
    assert {s.id for s in report.heavily_used} == {b.id}


def test_heavily_used_min_applied_high_filters_aggressively() -> None:
    a = _memory()
    b = _memory()
    events = [_event("use", ids=[a.id], outcome="applied") for _ in range(2)]
    events += [_event("use", ids=[b.id], outcome="applied") for _ in range(5)]
    report = compute_health(
        [a, b], events, heavily_used_min_applied=5, now=_utc(2026, 5, 1)
    )
    assert {s.id for s in report.heavily_used} == {b.id}


def test_min_applied_does_not_change_dead_weight_logic() -> None:
    """Raising the heavily_used floor must not promote a memory into
    dead_weight — dead_weight is purely "no applied events ever AND old"."""
    old_with_two_applies = _memory(created=_utc(2026, 1, 1))
    events = [
        _event(
            "use",
            ts=_utc(2026, 4, 1),
            ids=[old_with_two_applies.id],
            outcome="applied",
        ),
        _event(
            "use",
            ts=_utc(2026, 4, 2),
            ids=[old_with_two_applies.id],
            outcome="applied",
        ),
    ]
    report = compute_health(
        [old_with_two_applies],
        events,
        window_days=30,
        heavily_used_min_applied=10,  # excludes from heavily_used
        now=_utc(2026, 5, 1),
    )
    # Out of heavily_used (didn't clear the floor)…
    assert report.heavily_used == []
    # …but NOT dead-weight either, because applied_count > 0.
    assert report.dead_weight == []


# ---------------------------------------------------------------------------
# last_verified_at threaded through MemoryStats
# ---------------------------------------------------------------------------


def test_memory_stats_carries_last_verified_at() -> None:
    verified_at = _utc(2026, 4, 15)
    m = _memory(created=_utc(2026, 1, 1))
    m_with_verify = m.model_copy(update={"last_verified_at": verified_at})
    events = [_event("use", ids=[m.id], outcome="applied") for _ in range(3)]
    report = compute_health([m_with_verify], events, now=_utc(2026, 5, 1))
    assert len(report.heavily_used) == 1
    assert report.heavily_used[0].last_verified_at == verified_at
    # Surfaces in to_dict for the JSON view too.
    assert report.heavily_used[0].to_dict()["last_verified_at"] is not None


def test_memory_stats_last_verified_at_none_serialised_as_null() -> None:
    m = _memory()
    events = [_event("use", ids=[m.id], outcome="applied") for _ in range(3)]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert report.heavily_used[0].to_dict()["last_verified_at"] is None


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

    # Min applied at 1 so a single applied event still surfaces — this
    # test is about plumbing, not the threshold tuning.
    report = report_for_directory(memory_dir, heavily_used_min_applied=1)
    assert report.total_active_memories == 1
    assert len(report.heavily_used) == 1
    assert report.heavily_used[0].id == m.id
    assert report.heavily_used[0].applied_count == 1


# ---------------------------------------------------------------------------
# Change C — auto vs explicit applied count split
# ---------------------------------------------------------------------------


def test_auto_applied_event_lands_in_auto_count() -> None:
    """A use event with `auto=True` increments `auto_applied_count` and
    leaves `explicit_applied_count` at zero — the server's auto-commit
    pass shouldn't look like the model deliberately endorsed."""
    m = _memory()
    events = [_event("use", ids=[m.id], outcome="applied", auto=True)]
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    row = report.heavily_used[0]
    assert row.applied_count == 1
    assert row.auto_applied_count == 1
    assert row.explicit_applied_count == 0


def test_explicit_applied_event_lands_in_explicit_count() -> None:
    """A use event without `auto=True` (or with auto=False) counts as
    explicit — the model called memory_record_use directly."""
    m = _memory()
    events = [
        _event("use", ids=[m.id], outcome="applied"),  # no auto field
        _event("use", ids=[m.id], outcome="applied", auto=False),
    ]
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    row = report.heavily_used[0]
    assert row.applied_count == 2
    assert row.auto_applied_count == 0
    assert row.explicit_applied_count == 2


def test_mixed_auto_and_explicit_splits_correctly() -> None:
    """Total applied_count equals auto + explicit at every point."""
    m = _memory()
    events = [
        _event("use", ids=[m.id], outcome="applied", auto=True),
        _event("use", ids=[m.id], outcome="applied"),
        _event("use", ids=[m.id], outcome="applied", auto=True),
        _event("use", ids=[m.id], outcome="applied", auto=False),
    ]
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    row = report.heavily_used[0]
    assert row.applied_count == 4
    assert row.auto_applied_count == 2
    assert row.explicit_applied_count == 2
    assert row.applied_count == row.auto_applied_count + row.explicit_applied_count


def test_endorsement_ratio_none_when_zero_applies() -> None:
    """With no applied events at all, the ratio is None — distinct from
    'zero explicit out of N auto.' The memory isn't in heavily_used
    (zero applies), so the property is asserted directly on a hand-built
    MemoryStats instead of reaching through the report buckets."""
    from bettermemory.health import MemoryStats

    m = _memory()
    stats = MemoryStats(
        id=m.id,
        scopes=list(m.scopes),
        summary="x",
        created=m.created,
        updated=m.updated,
    )
    assert stats.endorsement_ratio is None


def test_endorsement_ratio_all_auto() -> None:
    """100% auto-applied → ratio 0.0. The weakly-endorsed signal."""
    m = _memory()
    events = [
        _event("use", ids=[m.id], outcome="applied", auto=True),
        _event("use", ids=[m.id], outcome="applied", auto=True),
        _event("use", ids=[m.id], outcome="applied", auto=True),
    ]
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert report.heavily_used[0].endorsement_ratio == 0.0


def test_to_dict_carries_split_counts_and_ratio() -> None:
    m = _memory()
    events = [
        _event("use", ids=[m.id], outcome="applied", auto=True),
        _event("use", ids=[m.id], outcome="applied"),
    ]
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    out = report.heavily_used[0].to_dict()
    assert out["auto_applied_count"] == 1
    assert out["explicit_applied_count"] == 1
    assert out["endorsement_ratio"] == 0.5


# ---------------------------------------------------------------------------
# Change D — endorsement_debt rollup
# ---------------------------------------------------------------------------


def test_endorsement_debt_picks_up_heavy_retrieval_with_zero_explicit() -> None:
    """The flagship case: a memory retrieved 5+ times, every applied
    event was auto, never explicitly endorsed → endorsement_debt."""
    m = _memory()
    events = []
    for _ in range(5):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert report.endorsement_debt.total == 1
    assert report.endorsement_debt.rows[0].id == m.id


def test_endorsement_debt_respects_min_retrievals_floor() -> None:
    """Below the floor (4 retrievals), the memory doesn't qualify —
    not enough traffic to call a pattern."""
    m = _memory()
    events = []
    for _ in range(4):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert report.endorsement_debt.total == 0


def test_endorsement_debt_excludes_explicitly_endorsed() -> None:
    """One explicit applied event lifts the memory out of debt — the
    model has reached for it deliberately at least once."""
    m = _memory()
    events = []
    for _ in range(10):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    # One explicit event lifts the memory out of the bucket.
    events.append(_event("use", ids=[m.id], outcome="applied"))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert report.endorsement_debt.total == 0


def test_endorsement_debt_excludes_ambient() -> None:
    """Ambient memories shape responses without being cited; explicit
    use events are structurally rare. They must not land here for the
    same reason they don't land in dead_weight or cold_memories."""
    m = _memory(category=Category.AMBIENT)
    events = []
    for _ in range(10):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert report.endorsement_debt.total == 0


def test_endorsement_debt_sorted_by_retrieval_count_desc() -> None:
    """Heaviest-trafficked first."""
    light = _memory()
    medium = _memory()
    heavy = _memory()
    events: list[dict[str, Any]] = []
    for _ in range(5):
        events.append(_event("search", returned=[light.id]))
        events.append(_event("use", ids=[light.id], outcome="applied", auto=True))
    for _ in range(10):
        events.append(_event("search", returned=[medium.id]))
        events.append(_event("use", ids=[medium.id], outcome="applied", auto=True))
    for _ in range(20):
        events.append(_event("search", returned=[heavy.id]))
        events.append(_event("use", ids=[heavy.id], outcome="applied", auto=True))
    report = compute_health(
        [light, medium, heavy],
        events,
        heavily_used_min_applied=1,
        now=_utc(2026, 5, 1),
    )
    assert [r.id for r in report.endorsement_debt.rows] == [
        heavy.id,
        medium.id,
        light.id,
    ]


def test_endorsement_debt_threshold_overridable() -> None:
    """Lower the floor so tests can exercise the bucket without 5+
    retrievals — and so a noisy store can tighten the criterion."""
    m = _memory()
    events = [
        _event("search", returned=[m.id]),
        _event("use", ids=[m.id], outcome="applied", auto=True),
    ]
    report = compute_health(
        [m],
        events,
        heavily_used_min_applied=1,
        endorsement_debt_min_retrievals=1,
        now=_utc(2026, 5, 1),
    )
    assert report.endorsement_debt.total == 1
    assert report.endorsement_debt.min_retrievals == 1


def test_endorsement_debt_min_retrievals_floor_clamped_above_zero() -> None:
    """A zero / negative threshold doesn't get interpreted literally
    (it would let zero-retrieval memories qualify) — clamped to 1."""
    m = _memory()
    report = compute_health(
        [m],
        [],
        heavily_used_min_applied=1,
        endorsement_debt_min_retrievals=0,
        now=_utc(2026, 5, 1),
    )
    assert report.endorsement_debt.min_retrievals == 1
    assert report.endorsement_debt.total == 0


def test_curation_counts_endorsement_debt_matches_health_bucket() -> None:
    """Numerical contract: curation_counts['endorsement_debt'] equals
    HealthReport.endorsement_debt.total over the same inputs."""
    m = _memory()
    events = []
    for _ in range(5):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    counts = curation_counts([m], events, window_days=30, now=_utc(2026, 5, 1))
    assert counts["endorsement_debt"] == report.endorsement_debt.total


def test_endorsement_debt_to_dict_shape() -> None:
    report = compute_health([], [], now=_utc(2026, 5, 1))
    payload = report.to_dict()
    assert "endorsement_debt" in payload
    assert payload["endorsement_debt"]["total"] == 0
    assert payload["endorsement_debt"]["rows"] == []
    assert payload["endorsement_debt"]["min_retrievals"] >= 1
