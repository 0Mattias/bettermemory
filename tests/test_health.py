"""Unit tests for health.py — aggregating events + memories into the
HealthReport."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


from bettermemory.health import (
    MarkerStats,
    _edit_distance_within,
    compute_health,
    curation_counts,
    find_prior_session_boundary,
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
        "unique_silent_miss_memories": 0,
        "cold_endorsement_memories": 0,
    }


def test_curation_counts_matches_compute_health_buckets() -> None:
    """Numerical contract: counts agree with bucket sizes from
    compute_health over the same inputs — including the dead-weight
    gates both paths read from the shared `_is_dead_weight` predicate
    (freshest-touch window, unresolved contradiction, endorsement
    grace), exercised by the three `gated_*` rows below."""
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
    # Would-be dead rows that each trip one predicate gate instead:
    gated_maintained = _memory(
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 4, 28),  # rewritten inside the window
    )
    gated_contradicted = _memory(created=_utc(2026, 1, 1))
    gated_graced = _memory(created=_utc(2026, 1, 1))
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[dead.id]),
        _event("search", ts=_utc(2026, 4, 1), returned=[gated_maintained.id]),
        _event("search", ts=_utc(2026, 4, 1), returned=[gated_contradicted.id]),
        _event(
            "use",
            ts=_utc(2026, 4, 20),
            ids=[gated_contradicted.id],
            outcome="contradicted",
        ),
        # Only retrieval is one day before `now` — inside the
        # endorsement grace.
        _event("search", ts=_utc(2026, 4, 30), returned=[gated_graced.id]),
    ]
    mems = [
        cold,
        dead,
        fresh_verified,
        never,
        stale_v,
        gated_maintained,
        gated_contradicted,
        gated_graced,
    ]
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
    # The gated rows must not be counted dead on either path.
    assert [s.id for s in report.dead_weight] == [dead.id]
    assert counts["dead"] == 1


def test_dead_weight_parity_across_health_counts_and_demotion() -> None:
    """Regression (round 85): `find_demotion_candidates` gained the
    freshest-touch, unresolved-contradiction, and endorsement-grace
    gates in round 84 while `compute_health`'s dead_weight and
    `curation_counts`' dead still keyed on `created` alone — a fixture
    tripping all three gates reported dead_weight=3 / dead=3 against
    demotion candidates=0, so scope_overview kept advertising dead rot
    the unattended pass refused to drain. All three consumers now read
    the shared `_is_dead_weight` predicate: each gated memory appears
    in NONE of them, the control appears in ALL of them."""
    from bettermemory.consolidate import find_demotion_candidates

    now = _utc(2026, 6, 1)
    old = now - timedelta(days=90)
    maintained = _memory(created=old, updated=now - timedelta(days=1))
    contradicted = _memory(created=old)
    graced = _memory(created=old)
    control = _memory(created=old)
    events = [
        _event("search", ts=now - timedelta(days=20), returned=[maintained.id]),
        _event("search", ts=now - timedelta(days=20), returned=[contradicted.id]),
        _event(
            "use",
            ts=now - timedelta(days=5),
            ids=[contradicted.id],
            outcome="contradicted",
        ),
        # Only retrieval is six hours old — inside the endorsement grace.
        _event("search", ts=now - timedelta(hours=6), returned=[graced.id]),
        _event("search", ts=now - timedelta(days=20), returned=[control.id]),
    ]
    mems = [maintained, contradicted, graced, control]
    report = compute_health(mems, events, window_days=30, now=now)
    counts = curation_counts(mems, events, window_days=30, now=now)
    demotions = find_demotion_candidates(mems, events, window_days=30, now=now)
    assert [s.id for s in report.dead_weight] == [control.id]
    assert counts["dead"] == 1
    assert [d.memory_id for d in demotions] == [control.id]


def test_curation_counts_excludes_ambient_from_dead_and_cold() -> None:
    cold = _memory(created=_utc(2026, 1, 1), category=Category.AMBIENT)
    dead = _memory(created=_utc(2026, 1, 1), category=Category.AMBIENT)
    events = [_event("search", ts=_utc(2026, 4, 1), returned=[dead.id])]
    counts = curation_counts([cold, dead], events, window_days=30, now=_utc(2026, 5, 1))
    assert counts["dead"] == 0
    assert counts["cold"] == 0


# ---------------------------------------------------------------------------
# curation_counts — `since` (delta) filter
# ---------------------------------------------------------------------------


def test_curation_counts_since_drops_events_older_than_boundary() -> None:
    """Delta mode: a search_miss event before `since` does not contribute
    to `silent_misses`. The same event with `since=None` does."""
    old_event = _event("search_miss", ts=_utc(2026, 4, 1))
    new_event = _event("search_miss", ts=_utc(2026, 4, 20))
    events = [old_event, new_event]
    absolute = curation_counts([], events, now=_utc(2026, 5, 1))
    delta = curation_counts(
        [],
        events,
        now=_utc(2026, 5, 1),
        since=_utc(2026, 4, 10),
    )
    assert absolute["silent_misses"] == 2
    assert delta["silent_misses"] == 1


def test_curation_counts_since_excludes_memories_created_before_boundary() -> None:
    """Memories whose `created` predates `since` are filtered out of the
    delta view — `cold` / `dead` / `stale` / `never_verified` rollups
    only see post-`since` memories."""
    old = _memory(created=_utc(2026, 1, 1))  # would normally count cold
    fresh = _memory(created=_utc(2026, 4, 20))  # post-boundary, recent
    absolute = curation_counts([old, fresh], [], window_days=30, now=_utc(2026, 5, 1))
    delta = curation_counts(
        [old, fresh],
        [],
        window_days=30,
        now=_utc(2026, 5, 1),
        since=_utc(2026, 4, 10),
    )
    # Both rows are never_verified absolute; only the post-boundary row
    # is in the delta view.
    assert absolute["never_verified"] == 2
    assert delta["never_verified"] == 1
    # Cold only triggers on >30d-old memories; the absolute counts the
    # old row, the delta excludes it because old < since.
    assert absolute["cold"] == 1
    assert delta["cold"] == 0


def test_curation_counts_since_zero_when_nothing_new() -> None:
    """An event log fully behind `since` produces an all-zero delta —
    distinct from None (no baseline), which is the handler's
    responsibility, not the helper's."""
    events = [_event("search_miss", ts=_utc(2026, 4, 1))]
    delta = curation_counts(
        [_memory(created=_utc(2026, 1, 1))],
        events,
        now=_utc(2026, 5, 1),
        since=_utc(2026, 4, 30),
    )
    assert delta == {
        "stale": 0,
        "never_verified": 0,
        "drifted": 0,
        "cold": 0,
        "dead": 0,
        "silent_misses": 0,
        "unique_silent_miss_memories": 0,
        "cold_endorsement_memories": 0,
    }


def test_curation_counts_since_excludes_old_memory_aging_into_stale() -> None:
    """The headline `curation_pending_new_since_last_session` claim:
    a memory created BEFORE `since` that has since aged into the
    `stale` bucket (last_verified_at older than the staleness cutoff)
    surfaces in the absolute view but NOT the delta. The point of
    the delta is "new rot since last session"; an old row crossing
    a threshold isn't new — the row itself predates `since`."""
    # Verified the day after it was created, both far in the past.
    # `stale` triggers when last_verified_at < (now - 30d), i.e.
    # last_verified_at < 2026-04-01. The 2026-01-02 verification
    # is well past that cutoff, so the row is `stale`.
    old = _memory(
        created=_utc(2026, 1, 1),
        last_verified_at=_utc(2026, 1, 2),
    )
    absolute = curation_counts(
        [old],
        [],
        window_days=30,
        verification_stale_days=30,
        now=_utc(2026, 5, 1),
    )
    delta = curation_counts(
        [old],
        [],
        window_days=30,
        verification_stale_days=30,
        now=_utc(2026, 5, 1),
        since=_utc(2026, 4, 10),
    )
    assert absolute["stale"] == 1
    assert delta["stale"] == 0


def test_curation_counts_since_filter_is_exclusive_at_boundary() -> None:
    """`since` is *exclusive*: an event whose ts equals the boundary
    value belongs to the prior session (the boundary IS that session's
    last event ts, per `find_prior_session_boundary`) and must not
    leak into the delta. Same applies to memory `created`. A naive
    strict-`<` filter would double-count the boundary event."""
    boundary = _utc(2026, 4, 10)
    boundary_event = _event("search_miss", ts=boundary)
    boundary_memory = _memory(created=boundary)
    delta = curation_counts(
        [boundary_memory],
        [boundary_event],
        window_days=30,
        now=_utc(2026, 5, 1),
        since=boundary,
    )
    assert delta["silent_misses"] == 0
    assert delta["never_verified"] == 0


def test_curation_counts_since_filters_cold_endorsement_to_post_boundary() -> None:
    """`cold_endorsement_memories` rides the same `mem_list` filter as
    stale / cold / dead, so a heavily-retrieved memory created before
    `since` must not surface in the delta even if its post-`since`
    retrievals push it over the floor."""
    old = _memory(created=_utc(2026, 1, 1))
    # 10 retrievals all after `since`, each closed by an auto-applied
    # use event — a genuine cold-endorsement shape (applies happened,
    # every one auto, zero explicit). Would normally flag the row; the
    # row itself predates `since` so the delta must exclude it. The
    # auto-applied events matter: without an apply the memory is
    # dead_weight, not cold-endorsement (see
    # test_zero_apply_memory_is_dead_weight_not_cold_endorsement), and
    # this test is about the `since` filter, not the apply-gate.
    search_events: list[dict[str, Any]] = []
    for _ in range(10):
        search_events.append(_event("search", ts=_utc(2026, 4, 20), returned=[old.id]))
        search_events.append(
            _event(
                "use", ts=_utc(2026, 4, 20), ids=[old.id], outcome="applied", auto=True
            )
        )
    absolute = curation_counts(
        [old],
        search_events,
        now=_utc(2026, 5, 1),
        cold_endorsement_min_retrievals=5,
    )
    delta = curation_counts(
        [old],
        search_events,
        now=_utc(2026, 5, 1),
        since=_utc(2026, 4, 10),
        cold_endorsement_min_retrievals=5,
    )
    assert absolute["cold_endorsement_memories"] == 1
    assert delta["cold_endorsement_memories"] == 0


# ---------------------------------------------------------------------------
# silent_miss_cutoff — additive escape hatch for pre-fix telemetry
# ---------------------------------------------------------------------------


def test_silent_miss_cutoff_drops_pre_cutoff_misses_in_compute_health() -> None:
    """A `silent_miss_cutoff` event with cutoff_ts T drops `search_miss`
    events at ts<T from both numerator and denominator. Post-T events
    survive."""
    pre = _event("search_miss", ts=_utc(2026, 4, 1))
    post = _event("search_miss", ts=_utc(2026, 4, 20))
    cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    report = compute_health([], [pre, post, cutoff], now=_utc(2026, 5, 1))
    assert report.silent_misses.miss_total == 1


def test_silent_miss_cutoff_drops_pre_cutoff_audited_in_compute_health() -> None:
    """The denominator (`turn_audited`) is filtered too — filtering only
    the numerator would skew the rate (low miss / high audited)."""
    pre_audit = _event("turn_audited", ts=_utc(2026, 4, 1))
    post_audit = _event("turn_audited", ts=_utc(2026, 4, 20))
    cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    report = compute_health([], [pre_audit, post_audit, cutoff], now=_utc(2026, 5, 1))
    assert report.silent_misses.audited_total == 1


def test_silent_miss_cutoff_latest_wins() -> None:
    """When multiple cutoff events exist the rollup honors the newest
    `cutoff_ts`, not the first or the last in log order. Older cutoffs
    cannot un-shrink the window an earlier extension established."""
    miss_a = _event("search_miss", ts=_utc(2026, 4, 5))
    miss_b = _event("search_miss", ts=_utc(2026, 4, 15))
    miss_c = _event("search_miss", ts=_utc(2026, 4, 25))
    early_cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    later_cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 20),
        cutoff_ts=_utc(2026, 4, 20).isoformat().replace("+00:00", "Z"),
    )
    report = compute_health(
        [],
        [miss_a, early_cutoff, miss_b, later_cutoff, miss_c],
        now=_utc(2026, 5, 1),
    )
    assert report.silent_misses.miss_total == 1


def test_silent_miss_cutoff_ignores_older_value_after_newer_seen() -> None:
    """A cutoff event written after a newer cutoff event cannot shrink
    the window — the rollup keeps the max `cutoff_ts` it has ever
    observed in the log, regardless of arrival order."""
    miss_a = _event("search_miss", ts=_utc(2026, 4, 5))
    miss_b = _event("search_miss", ts=_utc(2026, 4, 15))
    newer_first = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 18),
        cutoff_ts=_utc(2026, 4, 20).isoformat().replace("+00:00", "Z"),
    )
    older_later = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 21),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    report = compute_health(
        [],
        [miss_a, newer_first, miss_b, older_later],
        now=_utc(2026, 5, 1),
    )
    # max cutoff is 2026-04-20, so both miss_a (04-05) and miss_b
    # (04-15) are filtered out.
    assert report.silent_misses.miss_total == 0


def test_silent_miss_cutoff_ignored_when_malformed() -> None:
    """A cutoff event with a non-parseable `cutoff_ts` is silently
    dropped — the rollup falls through to the pre-cutoff counting
    behavior rather than failing the whole health report."""
    miss = _event("search_miss", ts=_utc(2026, 4, 1))
    junk_cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts="not-a-timestamp",
    )
    no_cutoff_field = _event("silent_miss_cutoff", ts=_utc(2026, 4, 10))
    report = compute_health(
        [], [miss, junk_cutoff, no_cutoff_field], now=_utc(2026, 5, 1)
    )
    assert report.silent_misses.miss_total == 1


def test_silent_miss_cutoff_no_op_without_cutoff_event() -> None:
    """Backwards-compat: stores with no `silent_miss_cutoff` events in
    their log behave exactly as before — every `search_miss` and
    `turn_audited` event counts."""
    misses = [
        _event("search_miss", ts=_utc(2026, 4, 1)),
        _event("search_miss", ts=_utc(2026, 4, 20)),
    ]
    audits = [
        _event("turn_audited", ts=_utc(2026, 4, 1)),
        _event("turn_audited", ts=_utc(2026, 4, 20)),
    ]
    report = compute_health([], misses + audits, now=_utc(2026, 5, 1))
    assert report.silent_misses.miss_total == 2
    assert report.silent_misses.audited_total == 2


def test_silent_miss_cutoff_filters_curation_counts_too() -> None:
    """The scope-overview fast helper honors the same cutoff as
    `compute_health`. Without this, the session-start
    `curation_pending.silent_misses` count and the deep health report
    would disagree on the same store."""
    pre = _event("search_miss", ts=_utc(2026, 4, 1))
    post = _event("search_miss", ts=_utc(2026, 4, 20))
    cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    counts = curation_counts([], [pre, post, cutoff], now=_utc(2026, 5, 1))
    assert counts["silent_misses"] == 1


def test_silent_miss_cutoff_drops_numerator_and_denominator_together() -> None:
    """Both `search_miss` and `turn_audited` are filtered from the SAME
    event log so the miss-rate metric doesn't skew. The two single-axis
    tests above pin each kind in isolation; this one pins the joint
    behavior — a regression that filtered only one side would still
    pass the per-kind tests but fail this one."""
    pre_audit = _event("turn_audited", ts=_utc(2026, 4, 1))
    pre_miss = _event("search_miss", ts=_utc(2026, 4, 2))
    post_audit = _event("turn_audited", ts=_utc(2026, 4, 20))
    post_miss = _event("search_miss", ts=_utc(2026, 4, 21))
    cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    report = compute_health(
        [],
        [pre_audit, pre_miss, post_audit, post_miss, cutoff],
        now=_utc(2026, 5, 1),
    )
    # Both buckets must drop the pre-cutoff event; the rate stays at
    # 1/1 instead of skewing to 1/2 (miss kept, audit dropped) or
    # 2/1 (audit kept, miss dropped).
    assert report.silent_misses.miss_total == 1
    assert report.silent_misses.audited_total == 1


def test_silent_miss_cutoff_keeps_events_at_exact_boundary() -> None:
    """`_count_post_cutoff` uses `ts >= cutoff` — an event whose ts is
    exactly the cutoff is kept. Flipping the inequality to `>` would
    silently change semantics without surfacing in the other tests
    (they all use strictly-pre or strictly-post timestamps)."""
    cutoff_at = _utc(2026, 4, 10)
    one_second_before = cutoff_at - timedelta(seconds=1)
    miss_at_boundary = _event("search_miss", ts=cutoff_at)
    audit_at_boundary = _event("turn_audited", ts=cutoff_at)
    miss_before = _event("search_miss", ts=one_second_before)
    audit_before = _event("turn_audited", ts=one_second_before)
    cutoff = _event(
        "silent_miss_cutoff",
        ts=cutoff_at,
        cutoff_ts=cutoff_at.isoformat().replace("+00:00", "Z"),
    )
    report = compute_health(
        [],
        [miss_at_boundary, audit_at_boundary, miss_before, audit_before, cutoff],
        now=_utc(2026, 5, 1),
    )
    # The boundary events are kept; the strictly-pre ones are dropped.
    assert report.silent_misses.miss_total == 1
    assert report.silent_misses.audited_total == 1


def test_silent_miss_cutoff_latest_wins_filters_audited_side_too() -> None:
    """The latest-cutoff-wins tests above only assert on the miss side.
    A bug where `compute_health` picked the max cutoff for `search_miss`
    but the first-seen cutoff for `turn_audited` would slip through —
    this test pins that both sides resolve to the SAME cutoff value."""
    audit_a = _event("turn_audited", ts=_utc(2026, 4, 5))
    audit_b = _event("turn_audited", ts=_utc(2026, 4, 15))
    audit_c = _event("turn_audited", ts=_utc(2026, 4, 25))
    early_cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    later_cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 20),
        cutoff_ts=_utc(2026, 4, 20).isoformat().replace("+00:00", "Z"),
    )
    report = compute_health(
        [],
        [audit_a, early_cutoff, audit_b, later_cutoff, audit_c],
        now=_utc(2026, 5, 1),
    )
    # Only audit_c (04-25) survives the 04-20 cutoff. Pinning audited
    # specifically — the miss-side equivalent test already exists.
    assert report.silent_misses.audited_total == 1


def test_no_signal_audits_split_out_of_audited_total() -> None:
    """Round-88 regression: `verdict == "no_signal"` audits structurally
    cannot flag a miss, so counting them in `audited_total` diluted the
    miss rate — and for a `search_mode="semantic"` deployment, whose
    Stop hook hardcodes `semantic_model=None` and therefore no_signals
    on EVERY turn forever, it manufactured the exact "audited heavily,
    model behaved" false-green signature the two-count shape exists to
    rule out (the module's own docstrings described no_signal-only runs
    as both-zero in two places, contradicted by the behavior). The
    no_signal audits move to the additive `no_signal_total`; a missing
    or legacy verdict stays in the miss-capable denominator, the
    conservative read."""
    events = [
        _event("turn_audited", ts=_utc(2026, 4, 1), verdict="no_signal"),
        _event("turn_audited", ts=_utc(2026, 4, 2), verdict="no_signal"),
        _event("turn_audited", ts=_utc(2026, 4, 3), verdict="no_signal"),
        _event("turn_audited", ts=_utc(2026, 4, 4), verdict="ok"),
        # Legacy event written before producers stamped a verdict —
        # treated as miss-capable so old logs keep their denominator.
        _event("turn_audited", ts=_utc(2026, 4, 5)),
    ]
    report = compute_health([], events, now=_utc(2026, 5, 1))
    assert report.silent_misses.audited_total == 2
    assert report.silent_misses.no_signal_total == 3
    assert report.silent_misses.miss_total == 0


def test_no_signal_only_deployment_reads_as_unmeasured_not_green() -> None:
    """The semantic-config signature end-to-end at the rollup level:
    audits firing every turn but ALL no_signal must NOT present as a
    healthy non-zero denominator with zero misses. Post-fix the
    denominator stays at zero (nothing miss-capable ran) and the
    no_signal bucket carries the cadence, so a consumer can tell
    "probe can't measure" apart from both "stalled hook" (all three
    zero) and "healthy run" (non-zero audited)."""
    events = [
        _event("turn_audited", ts=_utc(2026, 4, d), verdict="no_signal")
        for d in range(1, 6)
    ]
    report = compute_health([], events, now=_utc(2026, 5, 1))
    assert report.silent_misses.audited_total == 0
    assert report.silent_misses.no_signal_total == 5


def test_silent_miss_cutoff_filters_no_signal_bucket_too() -> None:
    """The bulk `silent_miss_cutoff` hatch applies the same ts filter to
    BOTH audit buckets — dropping only the miss-capable side would skew
    the no_signal/audited proportions a calibration pass reads."""
    pre_ns = _event("turn_audited", ts=_utc(2026, 4, 1), verdict="no_signal")
    post_ns = _event("turn_audited", ts=_utc(2026, 4, 20), verdict="no_signal")
    pre_ok = _event("turn_audited", ts=_utc(2026, 4, 2), verdict="ok")
    post_ok = _event("turn_audited", ts=_utc(2026, 4, 21), verdict="ok")
    cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 10),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    report = compute_health(
        [],
        [pre_ns, pre_ok, post_ns, post_ok, cutoff],
        now=_utc(2026, 5, 1),
    )
    assert report.silent_misses.audited_total == 1
    assert report.silent_misses.no_signal_total == 1


def test_silent_miss_cutoff_resolved_globally_under_since_delta() -> None:
    """`curation_counts(since=...)` filters event walk by `--since`, but
    `silent_miss_cutoff` events are global markers — their effect must
    apply even if the cutoff event itself falls below the delta window.
    Without this exemption, a delta run would drop the cutoff and the
    rollup would over-count pre-cutoff misses."""
    # Cutoff written long ago, well before the `since` boundary.
    cutoff = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 1, 1),
        cutoff_ts=_utc(2026, 4, 10).isoformat().replace("+00:00", "Z"),
    )
    # Two misses in the delta window — one pre-cutoff, one post-cutoff.
    pre = _event("search_miss", ts=_utc(2026, 4, 5))
    post = _event("search_miss", ts=_utc(2026, 4, 20))

    counts = curation_counts(
        [],
        [cutoff, pre, post],
        now=_utc(2026, 5, 1),
        since=_utc(2026, 4, 1),
    )
    # The cutoff must apply — only the post-cutoff miss counts.
    # Without the exemption, the cutoff would be silently dropped and
    # both misses (pre and post) would count as 2.
    assert counts["silent_misses"] == 1


# ---------------------------------------------------------------------------
# silent_misses dedup + tombstone filter (T2 / T3)
# ---------------------------------------------------------------------------
#
# `miss_total` (the raw event count) historically conflated "9 turns
# hammering the same mis-tagged memory" with "9 distinct memories the
# model failed to retrieve" — both surfaced as 9. The
# `unique_miss_memories` counter dedups by top-hit memory_id so a
# consumer can tell the two cases apart. Separately, the rollup now
# drops events whose top-hit memory has been tombstoned: once the
# memory is gone the miss is no longer actionable, and other rollups
# (`dead_weight`, `heavily_used`, `orphan_use_events`) already
# cross-reference against the tombstone set. These tests pin both
# behaviors against the public `compute_health` / `curation_counts`
# surfaces — they're naturally coupled because the same accumulator
# code path implements both.


def _search_miss_with_top_hit(memory_id: str, ts: datetime) -> dict[str, Any]:
    """A `search_miss` event carrying the canonical `top_hits` payload.

    Mirrors the shape `search_miss_fields` produces: `top_hits` is a
    list of dicts, each carrying at minimum an `id`. The other fields
    (`score`, `relevance`, `scopes`, `snippet`) are present on real
    events but irrelevant to the rollup so we keep the fixture
    minimal — the rollup only reads `top_hits[0]["id"]`.
    """
    return _event(
        "search_miss",
        ts=ts,
        top_hits=[{"id": memory_id, "score": 1.0, "relevance": "high"}],
    )


def test_silent_misses_dedup_by_top_hit_in_compute_health() -> None:
    """Five `search_miss` events all pointing at the same memory: the
    event count stays 5 (`miss_total` is unchanged shape), but the
    distinct-memory count is 1. Without the dedup the rollup over-states
    breadth — 5 events look like 5 mis-tagged memories rather than one
    mis-tag the model kept probing."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _search_miss_with_top_hit(m.id, ts=_utc(2026, 4, day))
        for day in (1, 2, 3, 4, 5)
    ]
    report = compute_health([m], events, window_days=30, now=_utc(2026, 5, 1))
    assert report.silent_misses.miss_total == 5
    assert report.silent_misses.unique_miss_memories == 1


def test_silent_misses_dedup_by_top_hit_in_curation_counts() -> None:
    """Same dedup contract applies to the fast `curation_counts` helper
    so the session-start rollup and the deep `memory_health` view agree
    on the same store — otherwise the model would see 9 in scope-overview
    and 1 in health and have no way to reconcile."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _search_miss_with_top_hit(m.id, ts=_utc(2026, 4, day))
        for day in (1, 2, 3, 4, 5)
    ]
    counts = curation_counts([m], events, window_days=30, now=_utc(2026, 5, 1))
    assert counts["silent_misses"] == 5
    assert counts["unique_silent_miss_memories"] == 1


def test_silent_misses_unique_count_matches_event_count_when_distinct() -> None:
    """When every miss targets a different memory the two counters
    agree. Pins the floor of the dedup contract — `unique_miss_memories`
    is never less than the count of distinct ids in the event stream."""
    memories = [_memory(created=_utc(2026, 1, 1)) for _ in range(3)]
    events = [
        _search_miss_with_top_hit(m.id, ts=_utc(2026, 4, 1 + i))
        for i, m in enumerate(memories)
    ]
    report = compute_health(memories, events, window_days=30, now=_utc(2026, 5, 1))
    assert report.silent_misses.miss_total == 3
    assert report.silent_misses.unique_miss_memories == 3


def test_silent_misses_tombstone_filter_in_compute_health() -> None:
    """Tombstoning the targeted memory drops its existing miss events
    from BOTH the event count and the unique-memories count. The miss
    is no longer actionable once the memory is gone — the rollup tracks
    other code paths (dead_weight, heavily_used, orphan_use_events) that
    already cross-reference against the tombstone set."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _search_miss_with_top_hit(m.id, ts=_utc(2026, 4, day)) for day in (1, 2, 3, 4)
    ]
    # Without the filter both counters would carry the misses.
    pre_tombstone = compute_health([m], events, window_days=30, now=_utc(2026, 5, 1))
    assert pre_tombstone.silent_misses.miss_total == 4
    assert pre_tombstone.silent_misses.unique_miss_memories == 1
    # With m tombstoned the misses against it drop out.
    post_tombstone = compute_health(
        [m],
        events,
        window_days=30,
        now=_utc(2026, 5, 1),
        tombstoned_ids={m.id},
    )
    assert post_tombstone.silent_misses.miss_total == 0
    assert post_tombstone.silent_misses.unique_miss_memories == 0


def test_silent_misses_tombstone_filter_in_curation_counts() -> None:
    """`curation_counts` honors the same filter — the scope-overview
    fast path can't disagree with the deep health view on the same
    store."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _search_miss_with_top_hit(m.id, ts=_utc(2026, 4, day)) for day in (1, 2, 3, 4)
    ]
    counts = curation_counts(
        [m],
        events,
        window_days=30,
        now=_utc(2026, 5, 1),
        tombstoned_ids={m.id},
    )
    assert counts["silent_misses"] == 0
    assert counts["unique_silent_miss_memories"] == 0


def test_silent_misses_mixed_live_and_tombstoned_targets() -> None:
    """The combined T2+T3 case: misses against a live memory M1 (3
    events) and a later-tombstoned memory M2 (4 events). After tombstone
    filtering, `miss_total == 3` (M1 only) and
    `unique_miss_memories == 1` (M1 only). The filters compose: the
    tombstone filter runs before the dedup, so M2's contribution drops
    from both counters."""
    m1 = _memory(created=_utc(2026, 1, 1))
    m2 = _memory(created=_utc(2026, 1, 1))
    events = [
        # 3 misses against the live memory
        _search_miss_with_top_hit(m1.id, ts=_utc(2026, 4, 1)),
        _search_miss_with_top_hit(m1.id, ts=_utc(2026, 4, 2)),
        _search_miss_with_top_hit(m1.id, ts=_utc(2026, 4, 3)),
        # 4 misses against the to-be-tombstoned memory
        _search_miss_with_top_hit(m2.id, ts=_utc(2026, 4, 4)),
        _search_miss_with_top_hit(m2.id, ts=_utc(2026, 4, 5)),
        _search_miss_with_top_hit(m2.id, ts=_utc(2026, 4, 6)),
        _search_miss_with_top_hit(m2.id, ts=_utc(2026, 4, 7)),
    ]
    report = compute_health(
        [m1, m2],
        events,
        window_days=30,
        now=_utc(2026, 5, 1),
        tombstoned_ids={m2.id},
    )
    # M1's 3 misses survive both filters; M2's 4 drop out via tombstone.
    assert report.silent_misses.miss_total == 3
    assert report.silent_misses.unique_miss_memories == 1
    # Same contract via the fast helper.
    counts = curation_counts(
        [m1, m2],
        events,
        window_days=30,
        now=_utc(2026, 5, 1),
        tombstoned_ids={m2.id},
    )
    assert counts["silent_misses"] == 3
    assert counts["unique_silent_miss_memories"] == 1


def test_silent_misses_malformed_top_hits_degrade_to_event_count_only() -> None:
    """A `search_miss` event with no / non-list / non-dict `top_hits`
    still counts toward `miss_total` (the legacy "count every event"
    semantic) but cannot contribute to `unique_miss_memories` and
    cannot be tombstone-filtered. Backward-compat shield for legacy
    events written before this rollup read `top_hits` at all — the
    rollup degrades cleanly rather than crashing on missing fields."""
    no_top_hits = _event("search_miss", ts=_utc(2026, 4, 1))
    empty_list = _event("search_miss", ts=_utc(2026, 4, 2), top_hits=[])
    non_dict = _event("search_miss", ts=_utc(2026, 4, 3), top_hits=["not-a-dict"])
    no_id = _event("search_miss", ts=_utc(2026, 4, 4), top_hits=[{"score": 0.9}])
    report = compute_health(
        [], [no_top_hits, empty_list, non_dict, no_id], now=_utc(2026, 5, 1)
    )
    # All four count as events (legacy semantic preserved) but none
    # produces a usable id for dedup.
    assert report.silent_misses.miss_total == 4
    assert report.silent_misses.unique_miss_memories == 0


def test_silent_misses_to_dict_carries_unique_miss_memories() -> None:
    """The serialized shape exposes `unique_miss_memories` alongside
    `miss_total` and `audited_total` — consumers reading the JSON
    payload (the CLI, downstream eval tools) need the new key without
    re-deriving it."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _search_miss_with_top_hit(m.id, ts=_utc(2026, 4, 1)),
        _search_miss_with_top_hit(m.id, ts=_utc(2026, 4, 2)),
    ]
    report = compute_health([m], events, window_days=30, now=_utc(2026, 5, 1))
    payload = report.to_dict()
    assert payload["silent_misses"] == {
        "audited_total": 0,
        "miss_total": 2,
        "unique_miss_memories": 1,
        "no_signal_total": 0,
    }


# ---------------------------------------------------------------------------
# find_prior_session_boundary — locates the previous session's tail
# ---------------------------------------------------------------------------


def test_find_prior_session_boundary_returns_none_when_only_current_session() -> None:
    events = [
        _event("search", ts=_utc(2026, 4, 1), session="sess_current"),
        _event("show", ts=_utc(2026, 4, 2), session="sess_current"),
    ]
    assert find_prior_session_boundary(events, "sess_current") is None


def test_find_prior_session_boundary_returns_none_when_current_session_id_missing() -> (
    None
):
    """An empty / None current session id has no baseline to delta against."""
    events = [_event("search", ts=_utc(2026, 4, 1), session="sess_a")]
    assert find_prior_session_boundary(events, None) is None
    assert find_prior_session_boundary(events, "") is None


def test_find_prior_session_boundary_returns_latest_other_session_ts() -> None:
    """The boundary is the max ts of any event NOT in current_session."""
    events = [
        _event("search", ts=_utc(2026, 4, 1), session="sess_old"),
        _event("show", ts=_utc(2026, 4, 5), session="sess_older_still"),
        _event("show", ts=_utc(2026, 4, 3), session="sess_old"),
        _event("search", ts=_utc(2026, 4, 10), session="sess_current"),
    ]
    boundary = find_prior_session_boundary(events, "sess_current")
    assert boundary == _utc(2026, 4, 5)


def test_find_prior_session_boundary_accepts_legacy_session_id_field() -> None:
    """Pre-unification archives wrote `session_id` instead of `session`.
    The boundary helper has to accept both, otherwise old archives
    would invisibly hide the prior session boundary."""
    legacy = {
        "ts": _utc(2026, 4, 1).isoformat().replace("+00:00", "Z"),
        "session_id": "sess_old",  # legacy field name
        "kind": "search",
    }
    current = _event("search", ts=_utc(2026, 4, 10), session="sess_current")
    assert find_prior_session_boundary([legacy, current], "sess_current") == _utc(
        2026, 4, 1
    )


def test_find_prior_session_boundary_skips_malformed_events() -> None:
    """Garbage entries don't poison the walk — the helper treats them
    as "no info" and keeps going."""
    events = [
        {"ts": "not-a-timestamp", "session": "sess_old", "kind": "x"},
        {"session": "sess_old", "kind": "x"},  # no ts
        _event("search", ts=_utc(2026, 4, 5), session="sess_old"),
        _event("search", ts=_utc(2026, 4, 10), session="sess_current"),
    ]
    assert find_prior_session_boundary(events, "sess_current") == _utc(2026, 4, 5)


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


def test_rare_scopes_excludes_year_suffixed_sibling_project() -> None:
    """A year-suffixed successor of an existing project scope is a
    deliberate new sibling, not a typo — and count==1 is the natural
    state for a just-started project, so without the suffix exemption
    the flag would fire exactly when a legitimate new scope appears.
    The shared 'projects:' prefix must not lend distance slack either."""
    a = _memory(scopes=["projects:aoc2023"])
    b = _memory(scopes=["projects:aoc2023"])
    sibling = _memory(scopes=["projects:aoc2024"])  # new year, new repo
    report = compute_health([a, b, sibling], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == []


def test_rare_scopes_excludes_version_and_numeric_suffix_siblings() -> None:
    """Version-suffixed ('blog-v2'/'blog-v3') and bare-numeric
    ('foo'/'foo2') successors are equal once the trailing digit run
    (optionally preceded by '-'/'v') is stripped — deliberate siblings,
    exempt from the typo flag even when both sides are singletons."""
    v2 = _memory(scopes=["projects:blog-v2"])
    v3 = _memory(scopes=["projects:blog-v3"])
    base_a = _memory(scopes=["projects:foo"])
    base_b = _memory(scopes=["projects:foo"])
    successor = _memory(scopes=["projects:foo2"])
    report = compute_health(
        [v2, v3, base_a, base_b, successor], [], now=_utc(2026, 5, 1)
    )
    assert report.rare_scopes == []


def test_rare_scopes_flags_bare_name_missing_namespace() -> None:
    """Dropping the namespace — tagging 'bettermemory' instead of
    'projects:bettermemory' — is the most common scope mis-tag, but the
    whole-string distance is the full prefix length (9), so the old
    check could never see it. Exact name-part equality across a
    namespace boundary flags it."""
    a = _memory(scopes=["projects:bettermemory"])
    b = _memory(scopes=["projects:bettermemory"])
    bare = _memory(scopes=["bettermemory"])  # name part matches exactly
    report = compute_health([a, b, bare], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == ["bettermemory"]


def test_rare_scopes_flags_truncated_namespace() -> None:
    """Same namespace-omission rule covers a truncated namespace:
    'proj:bettermemory' carries the exact name part of an existing
    'projects:bettermemory' (whole-string distance 4, invisible to the
    old check) and gets flagged."""
    a = _memory(scopes=["projects:bettermemory"])
    b = _memory(scopes=["projects:bettermemory"])
    truncated = _memory(scopes=["proj:bettermemory"])
    report = compute_health([a, b, truncated], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == ["proj:bettermemory"]


def test_rare_scopes_excludes_unrelated_short_tags() -> None:
    """Short topic tags land within flat distance 2 of each other while
    being completely unrelated — vim/git, gpu/cpu, api/aws are
    substitution-distance hits that rewrite most of the tag, and
    ml/sql is distance 2 on a 2-char name. The length-scaled threshold
    (substitutions don't count at <= 3 chars; distance 1 only below
    6 chars) keeps every one of these singletons out of the bucket."""
    gits = [_memory(scopes=["git"]) for _ in range(3)]
    cpu_a = _memory(scopes=["cpu"])
    cpu_b = _memory(scopes=["cpu"])
    aws_a = _memory(scopes=["aws"])
    aws_b = _memory(scopes=["aws"])
    sql_a = _memory(scopes=["sql"])
    sql_b = _memory(scopes=["sql"])
    vim = _memory(scopes=["vim"])
    gpu = _memory(scopes=["gpu"])
    api = _memory(scopes=["api"])
    ml = _memory(scopes=["ml"])
    report = compute_health(
        [*gits, cpu_a, cpu_b, aws_a, aws_b, sql_a, sql_b, vim, gpu, api, ml],
        [],
        now=_utc(2026, 5, 1),
    )
    assert report.rare_scopes == []


def test_rare_scopes_excludes_short_tails_under_shared_namespace() -> None:
    """The shared 'projects:' prefix contributes zero distance, so
    'projects:vim' vs 'projects:git' degenerated to vim/git under the
    flat threshold. Stripping the equal leading segment and comparing
    only the 3-char tails (where substitutions don't count) clears it."""
    a = _memory(scopes=["projects:git"])
    b = _memory(scopes=["projects:git"])
    vim = _memory(scopes=["projects:vim"])
    report = compute_health([a, b, vim], [], now=_utc(2026, 5, 1))
    assert report.rare_scopes == []


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
# Change D — cold_endorsement_memories rollup
# ---------------------------------------------------------------------------


def test_cold_endorsement_picks_up_heavy_retrieval_with_zero_explicit() -> None:
    """The flagship case: a memory retrieved 5+ times, every applied
    event was auto, never explicitly endorsed → counts as one
    cold-endorsement memory.

    Per-memory semantic: the rollup counts MEMORIES, not turns. One
    memory retrieved 5 times here contributes 1 to total — not 5."""
    m = _memory()
    events = []
    for _ in range(5):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    assert report.cold_endorsement_memories.total == 1
    assert report.cold_endorsement_memories.rows[0].id == m.id


def test_cold_endorsement_counts_memories_not_turns() -> None:
    """Pin the per-memory semantic that motivated the rename: a single
    memory retrieved many times across many turns contributes ONE to
    `cold_endorsement_memories.total`, not one per turn or per
    retrieval event. The old `endorsement_debt` label suggested per-
    turn counting; this test locks the actual per-memory contract."""
    m = _memory()
    events = []
    # 5 turns, each turn retrieves and auto-applies the same memory.
    for _ in range(5):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    # ONE memory, regardless of how many turns hit it.
    assert report.cold_endorsement_memories.total == 1
    # The same value surfaces through curation_counts.
    counts = curation_counts([m], events, window_days=30, now=_utc(2026, 5, 1))
    assert counts["cold_endorsement_memories"] == 1


def test_cold_endorsement_respects_min_retrievals_floor() -> None:
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
    assert report.cold_endorsement_memories.total == 0


def test_cold_endorsement_excludes_explicitly_endorsed() -> None:
    """One explicit applied event lifts the memory out of the bucket
    — the model has reached for it deliberately at least once."""
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
    assert report.cold_endorsement_memories.total == 0


def test_cold_endorsement_excludes_ambient() -> None:
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
    assert report.cold_endorsement_memories.total == 0


def test_cold_endorsement_sorted_by_retrieval_count_desc() -> None:
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
    assert [r.id for r in report.cold_endorsement_memories.rows] == [
        heavy.id,
        medium.id,
        light.id,
    ]


def test_cold_endorsement_threshold_overridable() -> None:
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
        cold_endorsement_min_retrievals=1,
        now=_utc(2026, 5, 1),
    )
    assert report.cold_endorsement_memories.total == 1
    assert report.cold_endorsement_memories.min_retrievals == 1


def test_cold_endorsement_min_retrievals_floor_clamped_above_zero() -> None:
    """A zero / negative threshold doesn't get interpreted literally
    (it would let zero-retrieval memories qualify) — clamped to 1."""
    m = _memory()
    report = compute_health(
        [m],
        [],
        heavily_used_min_applied=1,
        cold_endorsement_min_retrievals=0,
        now=_utc(2026, 5, 1),
    )
    assert report.cold_endorsement_memories.min_retrievals == 1
    assert report.cold_endorsement_memories.total == 0


def test_curation_counts_cold_endorsement_matches_health_bucket() -> None:
    """Numerical contract: curation_counts['cold_endorsement_memories']
    equals HealthReport.cold_endorsement_memories.total over the same
    inputs."""
    m = _memory()
    events = []
    for _ in range(5):
        events.append(_event("search", returned=[m.id]))
        events.append(_event("use", ids=[m.id], outcome="applied", auto=True))
    report = compute_health(
        [m], events, heavily_used_min_applied=1, now=_utc(2026, 5, 1)
    )
    counts = curation_counts([m], events, window_days=30, now=_utc(2026, 5, 1))
    assert counts["cold_endorsement_memories"] == report.cold_endorsement_memories.total


def test_zero_apply_memory_is_dead_weight_not_cold_endorsement() -> None:
    """A memory created before the window, retrieved over the floor, but
    NEVER applied (auto or explicit) is dead_weight — NOT cold-endorsement.

    cold_endorsement is the COMPLEMENT of dead_weight: "applies happened,
    but every one was the auto fallback." A pure dead-weight row
    (applied_count == 0) must land ONLY in dead_weight. Before the
    `applied_count == 0` gate it satisfied `explicit_applied_count == 0`
    and double-counted into both buckets, inflating the cold-endorsement
    rollup and mis-routing a never-applied memory to acknowledge-debt
    instead of removal. Pinned across compute_health AND curation_counts
    so the two surfaces stay in numerical agreement."""
    m = _memory(created=_utc(2026, 1, 1))
    # 5 retrievals (>= floor), ZERO use events — never applied at all.
    events = [_event("search", ts=_utc(2026, 4, 1), returned=[m.id]) for _ in range(5)]
    report = compute_health(
        [m],
        events,
        window_days=30,
        cold_endorsement_min_retrievals=5,
        now=_utc(2026, 5, 1),
    )
    # In dead_weight (retrieved, never applied) ...
    assert [s.id for s in report.dead_weight] == [m.id]
    # ... and ABSENT from cold_endorsement (no apply ever happened).
    assert report.cold_endorsement_memories.total == 0
    assert report.cold_endorsement_memories.rows == []

    # The fast helper agrees: dead counted, cold_endorsement not.
    counts = curation_counts(
        [m],
        events,
        window_days=30,
        cold_endorsement_min_retrievals=5,
        now=_utc(2026, 5, 1),
    )
    assert counts["dead"] == 1
    assert counts["cold_endorsement_memories"] == 0


def test_cold_endorsement_to_dict_shape() -> None:
    report = compute_health([], [], now=_utc(2026, 5, 1))
    payload = report.to_dict()
    assert "cold_endorsement_memories" in payload
    assert payload["cold_endorsement_memories"]["total"] == 0
    assert payload["cold_endorsement_memories"]["rows"] == []
    assert payload["cold_endorsement_memories"]["min_retrievals"] >= 1


# ---------------------------------------------------------------------------
# Registry-vs-handler-methods parity for `_StatsAccumulator` (Class 7 —
# closed by this commit).
#
# `_StatsAccumulator._HANDLERS` (`health.py:833`) is a class-level
# `dict[str, Callable]` that routes each event `kind` to a sibling
# `_handle_<kind>` instance method. `handle_event` (`:651`) consumes the
# table by `_HANDLERS.get(kind)` and `handler is not None` — events
# whose kind is missing from the table are silently dropped (the
# fallback is `if handler is not None`, NOT `else: raise`).
#
# The two enumerations MUST agree:
#
#   - Registry-only (key without method): would crash on first event of
#     that kind with `AttributeError` when the stored callable is
#     dereferenced — loud, but later than necessary.
#   - Method-only (sibling `_handle_*` method without registry entry):
#     SILENT — `handle_event` no-ops on the kind, the health rollup
#     drops the metric, and the contributor's "I added the handler"
#     belief survives until someone manually validates the rollup. This
#     is the bad direction: tracked-metric drift with no test failure.
#
# Hazard tier: medium (silent metric degradation on the dispatch-arm
# side; loud-but-late on the registry-only side). The mapping is
# 1:1 with no renaming (key `"foo"` maps to method `_handle_foo`) so
# the `name[len('_handle_'):]` strip is faithful.
#
# Note: `_HANDLERS` lives INSIDE the `_StatsAccumulator` class (not as
# a module-level constant) — the table is built once per class rather
# than per `handle_event` call. The methods are bound to `self` via the
# `handler(self, ev)` call site in `handle_event`. Importing via the
# class attribute (`_StatsAccumulator._HANDLERS`) rather than module
# scope is the correct discovery path.
#
# Negative-control verified at commit time (see commit message for
# detail).
# ---------------------------------------------------------------------------


def test_handlers_table_matches_handle_methods() -> None:
    """Every `_handle_<kind>` method on `_StatsAccumulator` MUST be
    wired into `_StatsAccumulator._HANDLERS`, and vice versa.

    Drift on the method-only side is the silent-bad direction: a
    contributor adds `_handle_remove` for a new event kind and forgets
    the `"remove": _handle_remove` table entry — `handle_event` silently
    drops the event (`if handler is not None:`), the health rollup loses
    the metric, and no test fails. Drift on the registry-only side
    crashes loudly on first event of that kind (`AttributeError` on the
    stored callable) but later than necessary.

    Closes Class 7 (same-file string-key registry-dict vs sequential
    dispatch-arm parity) from the tick-25 Branch B audit."""
    from bettermemory.health import _StatsAccumulator

    handler_methods = {
        name[len("_handle_") :]
        for name in dir(_StatsAccumulator)
        if name.startswith("_handle_")
    }
    registry_keys = set(_StatsAccumulator._HANDLERS)
    assert registry_keys == handler_methods, (
        "_StatsAccumulator._HANDLERS keys / _handle_* methods drifted; "
        "see health.py:_StatsAccumulator._HANDLERS (the dispatch table) "
        "and the sibling _handle_* methods on the same class. "
        f"registry-only={registry_keys - handler_methods} "
        "(would AttributeError on first event of that kind); "
        f"methods-only={handler_methods - registry_keys} "
        "(SILENTLY no-ops in handle_event — health rollup loses the metric)."
    )


# ---------------------------------------------------------------------------
# Proactive recommendations
# ---------------------------------------------------------------------------


def test_recommendations_empty_on_healthy_store() -> None:
    """A clean store with no bucket pressure surfaces no recommendations.
    The field must be present (so consumers don't need a missing-key
    guard) but empty."""
    report = compute_health([], [], now=_utc(2026, 5, 1))
    assert report.recommendations == []
    # The to_dict roundtrip carries the field as well.
    assert report.to_dict()["recommendations"] == []


def test_recommendations_surfaces_dead_weight_when_above_floor() -> None:
    """3+ dead-weight memories trips the remove_dead_weight
    recommendation. 2 or fewer stays silent — the model doesn't need
    a nudge to remove a single memory."""
    memories = [_memory(created=_utc(2026, 1, i + 1)) for i in range(3)]
    events = [_event("search", ts=_utc(2026, 4, 1), returned=[m.id]) for m in memories]
    report = compute_health(memories, events, window_days=30, now=_utc(2026, 5, 1))
    kinds = [r.kind for r in report.recommendations]
    assert "remove_dead_weight" in kinds
    dead_rec = next(r for r in report.recommendations if r.kind == "remove_dead_weight")
    assert dead_rec.count == 3
    assert len(dead_rec.memory_ids) == 3
    assert "memory_remove" in dead_rec.action


def test_recommendations_dead_weight_silent_below_floor() -> None:
    """2 dead-weight memories is below the floor of 3 — stays quiet."""
    memories = [_memory(created=_utc(2026, 1, i + 1)) for i in range(2)]
    events = [_event("search", ts=_utc(2026, 4, 1), returned=[m.id]) for m in memories]
    report = compute_health(memories, events, window_days=30, now=_utc(2026, 5, 1))
    assert not any(r.kind == "remove_dead_weight" for r in report.recommendations)


def test_recommendations_surfaces_contradicted_on_first_occurrence() -> None:
    """`resolve_contradicted` fires at count=1 because each instance is
    independently actionable — one stuck contradiction is still a stuck
    contradiction."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _event("search", ts=_utc(2026, 4, 1), returned=[m.id]),
        _event(
            "use",
            ts=_utc(2026, 4, 2),
            ids=[m.id],
            outcome="contradicted",
        ),
    ]
    report = compute_health([m], events, window_days=30, now=_utc(2026, 5, 1))
    kinds = [r.kind for r in report.recommendations]
    assert "resolve_contradicted" in kinds
    rec = next(r for r in report.recommendations if r.kind == "resolve_contradicted")
    assert rec.count == 1
    assert "memory_update" in rec.action


def test_recommendations_memory_ids_capped_at_row_cap() -> None:
    """The `memory_ids` field is bounded for inline display — uncapped
    `count` still tells the consumer the true bucket size."""
    from bettermemory.health import _RECOMMENDATION_ROW_CAP

    # Build more memories than the cap.
    memories = [
        _memory(created=_utc(2026, 1, 1)) for _ in range(_RECOMMENDATION_ROW_CAP + 5)
    ]
    events = [_event("search", ts=_utc(2026, 4, 1), returned=[m.id]) for m in memories]
    report = compute_health(memories, events, window_days=30, now=_utc(2026, 5, 1))
    dead_rec = next(r for r in report.recommendations if r.kind == "remove_dead_weight")
    assert dead_rec.count == _RECOMMENDATION_ROW_CAP + 5  # uncapped
    assert len(dead_rec.memory_ids) == _RECOMMENDATION_ROW_CAP  # capped


def test_recommendations_to_dict_shape_is_stable() -> None:
    """Recommendation.to_dict carries the closed set of fields the
    consumer reads. Keep this pinned so a future field addition doesn't
    silently rename existing keys."""
    memories = [_memory(created=_utc(2026, 1, i + 1)) for i in range(3)]
    events = [_event("search", ts=_utc(2026, 4, 1), returned=[m.id]) for m in memories]
    report = compute_health(memories, events, window_days=30, now=_utc(2026, 5, 1))
    raw = report.recommendations[0].to_dict()
    assert set(raw.keys()) == {
        "kind",
        "summary",
        "action",
        "count",
        "memory_ids",
        "scope",
    }


def test_cold_endorsement_ratio_threshold_off_by_default() -> None:
    """Default `cold_endorsement_ratio_threshold=0.0` preserves the
    original semantic: only memories with ZERO explicit_applied land
    in the bucket. A memory with even one explicit endorsement stays
    out, regardless of how lopsided the auto-vs-explicit ratio is."""
    m = _memory(created=_utc(2026, 1, 1))
    # 9 retrievals → above the floor of 5.
    events: list[dict[str, Any]] = [
        _event("search", ts=_utc(2026, 2, 1), returned=[m.id]) for _ in range(9)
    ]
    # Make most of those count as auto-applied + one explicit.
    events.extend(
        _event("use", ts=_utc(2026, 3, i + 1), ids=[m.id], outcome="applied", auto=True)
        for i in range(8)
    )
    events.append(_event("use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="applied"))

    report = compute_health(
        [m],
        events,
        window_days=30,
        now=_utc(2026, 5, 1),
    )
    # Binary check: one explicit endorsement is enough to stay out.
    assert report.cold_endorsement_memories.total == 0


def test_cold_endorsement_ratio_threshold_surfaces_lopsided_memory() -> None:
    """With `ratio_threshold=0.2`, a memory whose explicit-applied
    ratio is below 20% lands in the bucket even when the binary
    check would skip it."""
    m = _memory(created=_utc(2026, 1, 1))
    events: list[dict[str, Any]] = [
        _event("search", ts=_utc(2026, 2, 1), returned=[m.id]) for _ in range(9)
    ]
    # 8 auto + 1 explicit → ratio 1/9 ≈ 0.11, below the 0.2 threshold.
    events.extend(
        _event("use", ts=_utc(2026, 3, i + 1), ids=[m.id], outcome="applied", auto=True)
        for i in range(8)
    )
    events.append(_event("use", ts=_utc(2026, 4, 1), ids=[m.id], outcome="applied"))

    report = compute_health(
        [m],
        events,
        window_days=30,
        cold_endorsement_ratio_threshold=0.2,
        now=_utc(2026, 5, 1),
    )
    assert report.cold_endorsement_memories.total == 1
    assert report.cold_endorsement_memories.rows[0].id == m.id


def test_cold_endorsement_ratio_threshold_skips_high_ratio_memory() -> None:
    """A memory with a healthy explicit ratio stays out of the bucket
    even when the threshold is set."""
    m = _memory(created=_utc(2026, 1, 1))
    events: list[dict[str, Any]] = [
        _event("search", ts=_utc(2026, 2, 1), returned=[m.id]) for _ in range(9)
    ]
    # 1 auto + 8 explicit → ratio 8/9 ≈ 0.89, well above 0.2 threshold.
    events.append(
        _event("use", ts=_utc(2026, 3, 1), ids=[m.id], outcome="applied", auto=True)
    )
    events.extend(
        _event("use", ts=_utc(2026, 3, i + 2), ids=[m.id], outcome="applied")
        for i in range(8)
    )

    report = compute_health(
        [m],
        events,
        window_days=30,
        cold_endorsement_ratio_threshold=0.2,
        now=_utc(2026, 5, 1),
    )
    assert report.cold_endorsement_memories.total == 0


def test_recommendation_kinds_constant_matches_compute_output() -> None:
    """Every kind `_compute_recommendations` can emit must appear in the
    public `RECOMMENDATION_KINDS` enumeration — a consumer that
    switches over the constant won't hit an unknown kind."""
    from bettermemory.health import RECOMMENDATION_KINDS

    # The kinds we know exist today. Update this set alongside any new
    # recommendation added to `_compute_recommendations`.
    expected = {
        "remove_dead_weight",
        "resolve_contradicted",
        "cleanup_cold_endorsements",
        "verify_drifted",
        "fix_typo_scopes",
    }
    assert set(RECOMMENDATION_KINDS) == expected


# ---------------------------------------------------------------------------
# miss_ack — per-event silent-miss escape hatch (T4)
# ---------------------------------------------------------------------------
#
# The bulk `silent_miss_cutoff` hatch wipes EVERY pre-cutoff miss
# in one stroke. T4 closes the granularity gap with
# `memory_acknowledge_miss(event_id, reason)`, which emits one
# `miss_ack` event referencing a single `search_miss` by id. The
# rollup drops the matching miss from BOTH `miss_total` and
# `unique_miss_memories`, just like the tombstone filter. Same
# defensive shape: a `miss_ack` referencing a search_miss that never
# existed degrades silently (it's idempotent — repeated acks are a
# no-op via the set semantic the rollup uses).


def _search_miss_with_event_id(
    memory_id: str,
    event_id: str,
    *,
    ts: datetime,
    query_preview: str | None = None,
) -> dict[str, Any]:
    """A `search_miss` event carrying the T4 `event_id` + redacted
    probe_query shape the real recorder produces.

    Mirrors `search_miss_fields` output: `event_id` is a top-level
    string (the per-event ULID); `top_hits` carries the targeted
    memory id; `probe_query` is the `{hash, preview, len}` dict shape
    when `log_queries_verbatim=False` (the default).
    """
    fields: dict[str, Any] = {
        "event_id": event_id,
        "top_hits": [{"id": memory_id, "score": 1.0, "relevance": "high"}],
    }
    if query_preview is not None:
        fields["probe_query"] = {
            "hash": "00000000000000ff",
            "preview": query_preview,
            "len": len(query_preview),
        }
    return _event("search_miss", ts=ts, **fields)


def test_miss_ack_drops_acked_event_from_compute_health_rollup() -> None:
    """A `miss_ack` event with event_id X drops the matching search_miss
    from both `miss_total` and `unique_miss_memories`. Distinct from the
    bulk `silent_miss_cutoff` hatch — only the one referenced event
    drops, not every pre-cutoff event."""
    m = _memory(created=_utc(2026, 1, 1))
    a = _search_miss_with_event_id(m.id, "EVID_A", ts=_utc(2026, 4, 5))
    b = _search_miss_with_event_id(m.id, "EVID_B", ts=_utc(2026, 4, 10))
    ack = _event(
        "miss_ack", ts=_utc(2026, 4, 15), event_id="EVID_A", reason="false positive"
    )

    pre_ack = compute_health([m], [a, b], now=_utc(2026, 5, 1))
    assert pre_ack.silent_misses.miss_total == 2
    assert pre_ack.silent_misses.unique_miss_memories == 1

    post_ack = compute_health([m], [a, b, ack], now=_utc(2026, 5, 1))
    # EVID_A drops out; EVID_B still counts. Unique-memory count stays
    # at 1 because EVID_B still points at memory `m`.
    assert post_ack.silent_misses.miss_total == 1
    assert post_ack.silent_misses.unique_miss_memories == 1


def test_miss_ack_drops_acked_event_from_curation_counts() -> None:
    """The fast `curation_counts` helper honors the same ack filter
    so the session-start view and the deep health view agree on the
    same store — same parity contract as the tombstone filter (T3)."""
    m = _memory(created=_utc(2026, 1, 1))
    a = _search_miss_with_event_id(m.id, "EVID_A", ts=_utc(2026, 4, 5))
    b = _search_miss_with_event_id(m.id, "EVID_B", ts=_utc(2026, 4, 10))
    ack = _event(
        "miss_ack", ts=_utc(2026, 4, 15), event_id="EVID_A", reason="false positive"
    )

    counts = curation_counts([m], [a, b, ack], now=_utc(2026, 5, 1))
    assert counts["silent_misses"] == 1
    assert counts["unique_silent_miss_memories"] == 1


def test_miss_ack_unique_memories_drops_to_zero_when_all_acked() -> None:
    """Acknowledging both misses against the only memory should drop
    `unique_miss_memories` to zero — the set has no surviving id."""
    m = _memory(created=_utc(2026, 1, 1))
    a = _search_miss_with_event_id(m.id, "EVID_A", ts=_utc(2026, 4, 5))
    b = _search_miss_with_event_id(m.id, "EVID_B", ts=_utc(2026, 4, 10))
    ack_a = _event(
        "miss_ack", ts=_utc(2026, 4, 15), event_id="EVID_A", reason="false positive 1"
    )
    ack_b = _event(
        "miss_ack", ts=_utc(2026, 4, 16), event_id="EVID_B", reason="false positive 2"
    )
    report = compute_health([m], [a, b, ack_a, ack_b], now=_utc(2026, 5, 1))
    assert report.silent_misses.miss_total == 0
    assert report.silent_misses.unique_miss_memories == 0


def test_miss_ack_legacy_events_without_event_id_cannot_be_acked() -> None:
    """Search_miss events written before T4 lack the `event_id` field;
    no ack event can reference them. They count toward `miss_total`
    forever (until the bulk `silent_miss_cutoff` hatch wipes them).
    This is the documented limit of the per-event hatch."""
    m = _memory(created=_utc(2026, 1, 1))
    # Legacy event — no event_id field.
    legacy = _event(
        "search_miss",
        ts=_utc(2026, 4, 5),
        top_hits=[{"id": m.id, "score": 1.0, "relevance": "high"}],
    )
    ack = _event("miss_ack", ts=_utc(2026, 4, 15), event_id="EVID_LEGACY")
    report = compute_health([m], [legacy, ack], now=_utc(2026, 5, 1))
    # The ack can't bind to the legacy event — it counts normally.
    assert report.silent_misses.miss_total == 1


def test_miss_ack_duplicate_acks_idempotent_rollup() -> None:
    """Two `miss_ack` events for the same event_id collapse via the set
    semantic — the rollup tolerates duplicate acks defensively."""
    m = _memory(created=_utc(2026, 1, 1))
    miss = _search_miss_with_event_id(m.id, "EVID_A", ts=_utc(2026, 4, 5))
    ack1 = _event("miss_ack", ts=_utc(2026, 4, 10), event_id="EVID_A", reason="ack one")
    ack2 = _event("miss_ack", ts=_utc(2026, 4, 11), event_id="EVID_A", reason="ack two")
    report = compute_health([m], [miss, ack1, ack2], now=_utc(2026, 5, 1))
    assert report.silent_misses.miss_total == 0


def test_miss_ack_with_malformed_event_id_is_ignored() -> None:
    """A `miss_ack` whose `event_id` field is missing / non-string is
    silently dropped — the rollup falls through to counting every miss
    normally rather than crashing on a malformed admin event."""
    m = _memory(created=_utc(2026, 1, 1))
    miss = _search_miss_with_event_id(m.id, "EVID_A", ts=_utc(2026, 4, 5))
    bad_acks = [
        _event("miss_ack", ts=_utc(2026, 4, 10)),  # no event_id
        _event("miss_ack", ts=_utc(2026, 4, 11), event_id=12345),  # non-string
        _event("miss_ack", ts=_utc(2026, 4, 12), event_id=""),  # empty string
    ]
    report = compute_health([m], [miss, *bad_acks], now=_utc(2026, 5, 1))
    # None of the bad acks bind; the miss still counts.
    assert report.silent_misses.miss_total == 1


def test_combined_cutoff_tombstone_ack_filters_compose() -> None:
    """The combined T2/T3/T4 test: one cutoff-dropped event, one
    tombstone-dropped event, one acked event, one survivor. The
    4-stage filter (cutoff + tombstone + ack + dedup) drops exactly
    three of the four; the survivor counts."""
    live = _memory(created=_utc(2026, 1, 1))
    tombstoned = _memory(created=_utc(2026, 1, 1))

    cutoff_event = _event(
        "silent_miss_cutoff",
        ts=_utc(2026, 4, 1),
        cutoff_ts=_utc(2026, 4, 1).isoformat().replace("+00:00", "Z"),
    )
    # Drops via cutoff (pre-2026-04-01)
    pre_cutoff = _search_miss_with_event_id(live.id, "EVID_PRE", ts=_utc(2026, 3, 20))
    # Drops via tombstone filter
    against_tombstone = _search_miss_with_event_id(
        tombstoned.id, "EVID_TOMB", ts=_utc(2026, 4, 10)
    )
    # Drops via ack
    acked = _search_miss_with_event_id(live.id, "EVID_ACK", ts=_utc(2026, 4, 11))
    ack = _event(
        "miss_ack", ts=_utc(2026, 4, 12), event_id="EVID_ACK", reason="false positive"
    )
    # Survivor — passes all four filters
    survivor = _search_miss_with_event_id(live.id, "EVID_LIVE", ts=_utc(2026, 4, 20))

    events = [cutoff_event, pre_cutoff, against_tombstone, acked, ack, survivor]
    report = compute_health(
        [live, tombstoned],
        events,
        now=_utc(2026, 5, 1),
        tombstoned_ids={tombstoned.id},
    )
    assert report.silent_misses.miss_total == 1
    assert report.silent_misses.unique_miss_memories == 1

    counts = curation_counts(
        [live, tombstoned],
        events,
        now=_utc(2026, 5, 1),
        tombstoned_ids={tombstoned.id},
    )
    assert counts["silent_misses"] == 1
    assert counts["unique_silent_miss_memories"] == 1


def test_recent_silent_misses_surfaced_on_health_report() -> None:
    """`compute_health` populates `recent_silent_misses` with the
    triage shape (event_id + top_hit_id + query_preview + ts) so the
    model has something to feed into `memory_acknowledge_miss`. Newest
    first; capped."""
    m = _memory(created=_utc(2026, 1, 1))
    events = [
        _search_miss_with_event_id(
            m.id, "EVID_A", ts=_utc(2026, 4, 5), query_preview="why backup strategy"
        ),
        _search_miss_with_event_id(
            m.id, "EVID_B", ts=_utc(2026, 4, 10), query_preview="restic config"
        ),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert len(report.recent_silent_misses) == 2
    # Newest first — EVID_B (04-10) should precede EVID_A (04-05).
    assert report.recent_silent_misses[0].event_id == "EVID_B"
    assert report.recent_silent_misses[0].top_hit_id == m.id
    assert report.recent_silent_misses[0].query_preview == "restic config"
    assert report.recent_silent_misses[1].event_id == "EVID_A"


def test_recent_silent_misses_ordering_is_microsecond_correct() -> None:
    """Regression: recent_silent_misses sorted on the rendered ISO STRING,
    but isoformat_utc omits the fractional-seconds part when microsecond==0
    ("…:09Z") and keeps it otherwise ("…:09.500000Z"). For two events in the
    SAME whole second a lexicographic sort mis-orders them ('.' < 'Z'), so the
    genuinely-newer sub-second event was demoted below (and at the cap, evicted
    in favour of) an older round-second one. Sorting on the datetime fixes it.
    """
    from datetime import datetime, timezone

    m = _memory(created=_utc(2026, 1, 1))
    older_round = datetime(2026, 4, 10, 12, 0, 9, tzinfo=timezone.utc)  # …:09Z
    newer_sub = datetime(2026, 4, 10, 12, 0, 9, 500000, tzinfo=timezone.utc)  # …:09.5Z
    events = [
        _search_miss_with_event_id(m.id, "ROUND", ts=older_round, query_preview="a"),
        _search_miss_with_event_id(m.id, "SUB", ts=newer_sub, query_preview="b"),
    ]
    report = compute_health([m], events, now=_utc(2026, 5, 1))
    assert len(report.recent_silent_misses) == 2
    # The sub-second event is 0.5s newer and must lead, despite sorting AFTER
    # the round-second event lexicographically.
    assert report.recent_silent_misses[0].event_id == "SUB"
    assert report.recent_silent_misses[1].event_id == "ROUND"


def test_recent_silent_misses_filters_acked_and_tombstoned() -> None:
    """The inline list matches the rollup's filter set — acked /
    tombstoned events don't leak into the triage surface even though
    they exist in the event log."""
    live = _memory(created=_utc(2026, 1, 1))
    tombstoned = _memory(created=_utc(2026, 1, 1))

    events = [
        _search_miss_with_event_id(live.id, "EVID_LIVE", ts=_utc(2026, 4, 5)),
        _search_miss_with_event_id(tombstoned.id, "EVID_TOMB", ts=_utc(2026, 4, 6)),
        _search_miss_with_event_id(live.id, "EVID_ACKED", ts=_utc(2026, 4, 7)),
        _event(
            "miss_ack",
            ts=_utc(2026, 4, 8),
            event_id="EVID_ACKED",
            reason="false positive",
        ),
    ]
    report = compute_health(
        [live, tombstoned],
        events,
        now=_utc(2026, 5, 1),
        tombstoned_ids={tombstoned.id},
    )
    surfaced_ids = {m.event_id for m in report.recent_silent_misses}
    assert surfaced_ids == {"EVID_LIVE"}


def test_recent_silent_misses_carries_legacy_events_with_none_event_id() -> None:
    """Legacy search_miss events without event_id should still surface
    in the triage list — the model needs to see them even though it
    can't ack them (event_id=None tells it so)."""
    m = _memory(created=_utc(2026, 1, 1))
    legacy = _event(
        "search_miss",
        ts=_utc(2026, 4, 5),
        top_hits=[{"id": m.id, "score": 1.0, "relevance": "high"}],
    )
    report = compute_health([m], [legacy], now=_utc(2026, 5, 1))
    assert len(report.recent_silent_misses) == 1
    assert report.recent_silent_misses[0].event_id is None
    assert report.recent_silent_misses[0].top_hit_id == m.id


def test_recent_silent_misses_cap_bounds_inline_list() -> None:
    """The inline list is capped — newest entries win. Don't bloat
    the JSON when 200 misses are in-window."""
    from bettermemory.health import _RECENT_SILENT_MISSES_CAP

    m = _memory(created=_utc(2026, 1, 1))
    # Many misses, deterministic event_ids and ascending ts.
    events = [
        _search_miss_with_event_id(m.id, f"EVID_{i:03d}", ts=_utc(2026, 4, 1 + i))
        for i in range(_RECENT_SILENT_MISSES_CAP * 3)
    ]
    report = compute_health([m], events, now=_utc(2026, 6, 1), window_days=120)
    assert len(report.recent_silent_misses) == _RECENT_SILENT_MISSES_CAP
    # Newest first — the highest-numbered event_id should lead.
    assert report.recent_silent_misses[0].event_id == (
        f"EVID_{(_RECENT_SILENT_MISSES_CAP * 3) - 1:03d}"
    )


def test_recent_silent_misses_serialised_in_to_dict() -> None:
    """The serialised report payload exposes `recent_silent_misses` —
    consumers reading the JSON (CLI, downstream tooling) need the
    inline list without re-deriving."""
    m = _memory(created=_utc(2026, 1, 1))
    miss = _search_miss_with_event_id(m.id, "EVID_X", ts=_utc(2026, 4, 5))
    report = compute_health([m], [miss], now=_utc(2026, 5, 1))
    payload = report.to_dict()
    assert "recent_silent_misses" in payload
    assert len(payload["recent_silent_misses"]) == 1
    entry = payload["recent_silent_misses"][0]
    assert entry == {
        "event_id": "EVID_X",
        "top_hit_id": m.id,
        "query_preview": None,
        "ts": entry["ts"],  # ts shape varies — just pin the key exists
    }
    assert entry["ts"].startswith("2026-04-05")
