"""Tests for the silent-miss telemetry pipeline.

Two layers:

- Unit tests for `probe_for_miss` — pure function, no server. Pins the
  threshold rule, the lookback windowing, and the verdict matrix.
- Integration tests for the `memory_audit_turn` MCP tool — exercises
  the handler through `build_server` and asserts the events emitted
  match the verdict (turn_audited always; search_miss only on miss).

The unit tests don't go through the event log; they hand `probe_for_miss`
a list of dicts directly so the verdict logic is decoupled from the
on-disk shape. The integration tests do walk the on-disk log so the
end-to-end shape stays pinned to the recorder contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.audit import (
    DEFAULT_LOOKBACK_SECONDS,
    THRESHOLD_RULE_V1,
    probe_for_miss,
)
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.health import compute_health, curation_counts
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _memory(
    body: str,
    *,
    scopes: list[str] | None = None,
    created: datetime | None = None,
) -> Memory:
    now = created or _utc(2026, 1, 1)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _search_event(
    *,
    session: str,
    ts: datetime,
    returned: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "session": session,
        "kind": "search",
        "returned": returned or [],
    }


def _show_event(
    *,
    session: str,
    ts: datetime,
    memory_id: str,
) -> dict[str, Any]:
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "session": session,
        "kind": "show",
        "id": memory_id,
    }


# ---------------------------------------------------------------------------
# probe_for_miss — no-signal branches
# ---------------------------------------------------------------------------


def test_empty_store_returns_no_signal() -> None:
    report = probe_for_miss(
        [],
        "what's my backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert report.verdict == "no_signal"
    assert report.top_hits == ()
    assert report.probe_query is None
    assert report.threshold_rule == THRESHOLD_RULE_V1


def test_empty_query_returns_no_signal() -> None:
    m = _memory("backup strategy: triangular restic replication")
    report = probe_for_miss(
        [m],
        "   ",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert report.verdict == "no_signal"
    assert report.probe_query is None


def test_query_with_no_hits_returns_no_signal() -> None:
    """When the probe returns zero hits (rather than low-relevance hits),
    that's `no_signal` — there's nothing to score against, so the audit
    has no data point. Distinct from `ok` (probe ran, scored, threshold
    not cleared)."""
    m = _memory("unrelated content about widgets")
    report = probe_for_miss(
        [m],
        "querystring with no overlap whatsoever",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert report.verdict == "no_signal"
    assert report.top_hits == ()
    # Probe_query IS set in the no-hits branch (the ranker ran and
    # scored, just found nothing). Distinct from the empty-store /
    # empty-query branches where probe_query is None. This pin guards
    # against a future "always None on no_signal" simplification that
    # would lose the ran-vs-aborted distinction.
    assert report.probe_query == "querystring with no overlap whatsoever"


# ---------------------------------------------------------------------------
# probe_for_miss — miss vs ok verdict matrix
# ---------------------------------------------------------------------------


def test_high_relevance_hit_with_no_recent_search_is_miss() -> None:
    """The load-bearing case: probe finds a high-relevance hit, no search
    fired in the lookback window → miss."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "miss"
    assert report.is_miss
    assert report.recent_retrieval_count == 0
    assert len(report.top_hits) >= 1
    assert report.top_hits[0].id == m.id
    assert report.top_hits[0].relevance == "high"
    assert report.probe_query == "backup strategy"


def test_high_relevance_hit_with_recent_search_in_window_is_ok() -> None:
    """The model *did* search — the audit should not flag this as a miss
    even though the probe finds a strong hit."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        _search_event(
            session="sess_x",
            ts=now - timedelta(seconds=30),
            returned=[m.id],
        ),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "ok"
    assert report.recent_retrieval_count == 1


def test_recent_search_in_different_session_does_not_protect() -> None:
    """Another session's search doesn't count — the audit is per-session."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        _search_event(
            session="sess_OTHER",
            ts=now - timedelta(seconds=10),
            returned=[m.id],
        ),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "miss"
    assert report.recent_retrieval_count == 0


def test_search_outside_lookback_window_does_not_protect() -> None:
    """A search from 5 minutes ago can't shield a turn 60s after it."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        _search_event(
            session="sess_x",
            ts=now - timedelta(minutes=5),
            returned=[m.id],
        ),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "miss"
    assert report.recent_retrieval_count == 0


def test_recent_memory_show_shields_miss_detection() -> None:
    """memory_show is also retrieval — a model that read a memory by id
    in the lookback window shouldn't be flagged for a miss even if no
    search event landed."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        _show_event(
            session="sess_x",
            ts=now - timedelta(seconds=30),
            memory_id=m.id,
        ),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "ok"
    assert report.recent_retrieval_count == 1


def test_show_in_different_session_does_not_shield() -> None:
    """show events in another session don't shield this one — the audit
    is per-session, same as the search-shielding rule."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        _show_event(
            session="sess_OTHER",
            ts=now - timedelta(seconds=10),
            memory_id=m.id,
        ),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "miss"


def test_search_and_show_both_count_toward_recent_retrieval() -> None:
    """Mixed search and show events accumulate. The count surfaces both."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        _search_event(
            session="sess_x",
            ts=now - timedelta(seconds=20),
            returned=[m.id],
        ),
        _show_event(
            session="sess_x",
            ts=now - timedelta(seconds=10),
            memory_id=m.id,
        ),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "ok"
    assert report.recent_retrieval_count == 2


def test_probe_mode_rejects_unknown_value() -> None:
    """An invalid `mode` raises rather than silently degrading."""
    m = _memory("backup strategy")
    with pytest.raises(ValueError):
        probe_for_miss(
            [m],
            "backup strategy",
            recent_events=[],
            session_id="sess_x",
            now=_utc(2026, 5, 1),
            mode="garbage",
        )


def test_probe_mode_default_is_keyword() -> None:
    """Default falls to keyword (the package default) — matches what
    the model would do absent a config override. Pin the default so a
    future drift to `hybrid` is deliberate, not silent."""
    import inspect

    sig = inspect.signature(probe_for_miss)
    assert sig.parameters["mode"].default == "keyword"


def test_partial_coverage_query_does_not_clear_threshold() -> None:
    """v1 threshold rule requires `relevance == "high"` on the top hit.
    `_relevance_label` reads coverage as `matched_unique / query_unique`:
    1/2 = 0.5 → medium, which the v1 rule rejects.

    Note: a single-token query that matches at all is structurally "high"
    (1/1 = 1.0), so the v1 rule fires aggressively on terse user
    messages. That's intentional — a single load-bearing word that hits
    a memory IS a likely miss. The test pins the partial-coverage case
    so a future calibration of `_relevance_label` thresholds doesn't
    silently flip this branch."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    report = probe_for_miss(
        [m],
        # "backup" hits the body; "frobnicator" doesn't. 1/2 coverage →
        # medium. Stopword-free so the coverage math is unambiguous.
        "backup frobnicator",
        recent_events=[],
        session_id="sess_x",
        now=now,
    )
    assert report.verdict == "ok"
    # The hit IS retained for triage even though the verdict is ok —
    # the threshold rule decides the verdict, not whether to record
    # the top hit.
    assert len(report.top_hits) >= 1
    assert report.top_hits[0].relevance in ("medium", "low")


def test_lookback_zero_clamps_up() -> None:
    """A pathological lookback_seconds=0 would never see any search.
    The handler clamps to >=1; the probe itself accepts the value
    verbatim so the contract pins where clamping happens."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    # search at exactly `now` — lookback_seconds=0 would miss it under
    # strict-less cutoff. We still expect the probe to find it via the
    # `ts >= cutoff` semantics in `_count_recent_searches`.
    events = [
        _search_event(session="sess_x", ts=now, returned=[m.id]),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=0,
    )
    # With cutoff=now-0=now and the search ts=now, the search lands
    # exactly on the cutoff. _count_recent_searches uses `ts < cutoff`
    # for the negative case so ts == cutoff is included → recent count
    # is 1 and verdict is ok.
    assert report.recent_retrieval_count == 1


def test_to_dict_round_trips_through_json() -> None:
    """The on-wire shape must survive a JSON round-trip cleanly so the
    MCP layer can forward the report as a tool result."""
    m = _memory("backup strategy uses triangular restic replication")
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    serialised = json.dumps(report.to_dict())
    restored = json.loads(serialised)
    assert restored["verdict"] in ("miss", "ok", "no_signal")
    assert restored["threshold_rule"] == THRESHOLD_RULE_V1
    assert restored["session_id"] == "sess_x"


def test_threshold_rule_recorded_on_every_report() -> None:
    """Every report carries the rule version so a future calibration
    pass can replay old reports under a new threshold."""
    m = _memory("backup strategy uses triangular restic replication")
    for query in ("backup strategy", "completely unrelated query"):
        report = probe_for_miss(
            [m],
            query,
            recent_events=[],
            session_id="sess_x",
            now=_utc(2026, 5, 1),
        )
        assert report.threshold_rule == THRESHOLD_RULE_V1


def test_default_lookback_constant_is_60_seconds() -> None:
    """Pin the default so a future bump (or accidental shrink) is
    a deliberate decision."""
    assert DEFAULT_LOOKBACK_SECONDS == 60


# ---------------------------------------------------------------------------
# Integration: memory_audit_turn via build_server
# ---------------------------------------------------------------------------


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path, SessionState]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=rec,
    )
    return server, memory_dir, state


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _events(memory_dir: Path) -> list[dict[str, Any]]:
    return list(iter_events(memory_dir))


async def test_audit_turn_always_emits_turn_audited(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """`turn_audited` is the denominator for the miss rate; it must fire
    on every audit call regardless of verdict."""
    server, memory_dir, _ = server_with_events
    await _call(
        server,
        "memory_audit_turn",
        user_message="generic question with no memory context",
    )
    events = [e for e in _events(memory_dir) if e["kind"] == "turn_audited"]
    assert len(events) == 1
    assert events[0]["verdict"] in ("ok", "no_signal", "miss")
    assert events[0]["threshold_rule"] == THRESHOLD_RULE_V1


async def test_audit_turn_emits_search_miss_on_miss(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """Write a memory, then audit a turn whose user message would have
    hit it — no search occurred → miss event.

    Query is chosen so every token survives stopword stripping AND every
    surviving token hits the body, so the relevance label lands cleanly
    at "high" under the v1 rule. A natural user phrasing
    ("what's my backup strategy") would tokenize to ["s", "backup",
    "strategy"] after stopword strip — coverage 2/3 → medium — and the
    v1 rule would correctly return "ok". That conservatism is by
    design; the integration test is about wiring, not threshold tuning.
    """
    server, memory_dir, _ = server_with_events
    await _call(
        server,
        "memory_write",
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )

    user_query = "backup strategy"
    report = await _call(
        server,
        "memory_audit_turn",
        user_message=user_query,
    )
    assert report["verdict"] == "miss"
    miss_events = [e for e in _events(memory_dir) if e["kind"] == "search_miss"]
    assert len(miss_events) == 1
    miss = miss_events[0]
    assert miss["threshold_rule"] == THRESHOLD_RULE_V1
    assert miss["probe_query"] == user_query
    assert len(miss["top_hits"]) >= 1


async def test_audit_turn_no_miss_after_recent_search(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """If the model *did* search before the audit fires, no miss event."""
    server, memory_dir, _ = server_with_events
    await _call(
        server,
        "memory_write",
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )
    # Model searched — this should shield the audit.
    await _call(server, "memory_search", query="backup strategy")
    report = await _call(
        server,
        "memory_audit_turn",
        # Same shape as the miss test so the only differentiator between
        # the two cases is the prior memory_search.
        user_message="backup strategy",
    )
    assert report["verdict"] == "ok"
    miss_events = [e for e in _events(memory_dir) if e["kind"] == "search_miss"]
    assert miss_events == []


async def test_audit_turn_rejects_non_string_message(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, _, _ = server_with_events
    # Two layers can reject this — pydantic at the MCP arg-validation
    # boundary ("Input should be a valid string") OR the handler's own
    # `isinstance` guard ("user_message must be a string"). Either is
    # an acceptable surface — the regex covers both so a future
    # rearrangement of the validation order doesn't fail this test
    # for the wrong reason. The point is that non-string `user_message`
    # is loudly rejected, not which layer rejects first.
    with pytest.raises(Exception, match="user_message must be a string|valid string"):
        await _call(server, "memory_audit_turn", user_message=12345)


async def test_audit_turn_emits_probe_mode_and_retrieval_count(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """Pin that the recorded `turn_audited` event carries the renamed
    `recent_retrieval_count` field AND the `probe_mode` the audit ran
    under, so a future calibration pass can see which scorer produced
    each verdict."""
    server, memory_dir, _ = server_with_events
    await _call(server, "memory_audit_turn", user_message="generic question")
    audited = [e for e in _events(memory_dir) if e["kind"] == "turn_audited"]
    assert audited
    ev = audited[-1]
    assert "recent_retrieval_count" in ev
    assert isinstance(ev["recent_retrieval_count"], int)
    # Default search_mode is "keyword" — the probe should match.
    assert ev["probe_mode"] == "keyword"


async def test_audit_turn_show_shields_miss_via_handler(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """End-to-end: write a memory, memory_show it, then audit. The
    show should shield the audit from flagging a miss."""
    server, memory_dir, _ = server_with_events
    written = await _call(
        server,
        "memory_write",
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )
    await _call(server, "memory_show", id=written["id"])
    report = await _call(
        server,
        "memory_audit_turn",
        user_message="backup strategy",
    )
    assert report["verdict"] == "ok"
    miss_events = [e for e in _events(memory_dir) if e["kind"] == "search_miss"]
    assert miss_events == []


async def test_audit_turn_lookback_seconds_is_clamped(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """Out-of-range lookback gets clamped to the 1-600s band so a misused
    hook can't silence the audit by passing a huge window."""
    server, memory_dir, _ = server_with_events
    await _call(
        server,
        "memory_audit_turn",
        user_message="generic question",
        lookback_seconds=999_999,
    )
    audited = [e for e in _events(memory_dir) if e["kind"] == "turn_audited"]
    assert audited[-1]["lookback_seconds"] == 600
    await _call(
        server,
        "memory_audit_turn",
        user_message="generic question",
        lookback_seconds=-5,
    )
    audited = [e for e in _events(memory_dir) if e["kind"] == "turn_audited"]
    assert audited[-1]["lookback_seconds"] == 1


# ---------------------------------------------------------------------------
# Health rollup: silent_misses surfaces in compute_health + curation_counts
# ---------------------------------------------------------------------------


def test_compute_health_counts_silent_miss_and_turn_audited() -> None:
    """search_miss + turn_audited events feed into HealthReport.silent_misses
    as numerator + denominator."""
    events = [
        {
            "ts": _utc(2026, 5, 1).isoformat().replace("+00:00", "Z"),
            "session": "sess_x",
            "kind": "turn_audited",
            "verdict": "ok",
        },
        {
            "ts": _utc(2026, 5, 1, 13).isoformat().replace("+00:00", "Z"),
            "session": "sess_x",
            "kind": "turn_audited",
            "verdict": "miss",
        },
        {
            "ts": _utc(2026, 5, 1, 13).isoformat().replace("+00:00", "Z"),
            "session": "sess_x",
            "kind": "search_miss",
            "threshold_rule": THRESHOLD_RULE_V1,
        },
    ]
    report = compute_health([], events, window_days=30, now=_utc(2026, 5, 2))
    assert report.silent_misses.audited_total == 2
    assert report.silent_misses.miss_total == 1


def test_curation_counts_includes_silent_misses() -> None:
    """The session-start rollup surfaces silent_misses so a non-zero count
    triggers the curation cue without paying the full health cost."""
    events = [
        {
            "ts": _utc(2026, 5, 1).isoformat().replace("+00:00", "Z"),
            "session": "sess_x",
            "kind": "search_miss",
            "threshold_rule": THRESHOLD_RULE_V1,
        },
    ]
    counts = curation_counts([], events, window_days=30, now=_utc(2026, 5, 2))
    assert counts["silent_misses"] == 1


def test_compute_health_with_no_audit_events_reports_zeros() -> None:
    """A store that's never been audited has both counts at zero — the
    bucket distinguishes 'unaudited' from 'audited and clean'."""
    report = compute_health([], [], window_days=30, now=_utc(2026, 5, 1))
    assert report.silent_misses.audited_total == 0
    assert report.silent_misses.miss_total == 0


def test_health_to_dict_carries_silent_misses() -> None:
    """The serialised shape gains a `silent_misses` key so consumers can
    branch on it without back-compat shims."""
    report = compute_health([], [], window_days=30, now=_utc(2026, 5, 1))
    payload = report.to_dict()
    assert "silent_misses" in payload
    assert payload["silent_misses"] == {"audited_total": 0, "miss_total": 0}


def test_event_field_builders_pin_canonical_shape() -> None:
    """The `turn_audited` / `search_miss` field builders are the single
    source of truth for the two producers (the Stop hook and the
    in-process MCP handler), so the shapes can't drift. Pin the
    contract — and specifically the two gaps the 2.6.4 audit found:
    `search_miss` must carry `triggered_from` (the handler omitted it)
    and `recent_retrieval_count` (every producer omitted it, leaving
    `eval`'s silent-miss column permanently blank).
    """
    from bettermemory.audit import (
        MissHit,
        MissReport,
        search_miss_fields,
        turn_audited_fields,
    )

    report = MissReport(
        verdict="miss",
        checked_at=_utc(2026, 5, 22),
        session_id="s1",
        lookback_seconds=600,
        recent_retrieval_count=3,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=(
            MissHit(
                id="m1",
                score=0.9,
                relevance="high",
                scopes=("tools",),
                snippet="snip",
            ),
        ),
        probe_query="q",
    )
    ta = turn_audited_fields(
        report,
        session_id="s1",
        probe_mode="keyword",
        assistant_present=True,
        triggered_from="stop_hook",
    )
    sm = search_miss_fields(report, session_id="s1", triggered_from="mcp_tool")

    assert ta["triggered_from"] == "stop_hook"
    assert ta["recent_retrieval_count"] == 3
    assert ta["verdict"] == "miss"
    # The two 2.6.4-audit gaps: search_miss must carry both.
    assert sm["triggered_from"] == "mcp_tool"
    assert sm["recent_retrieval_count"] == 3
    # top_hits is the canonical list-of-dicts shape, not list-of-str.
    assert isinstance(sm["top_hits"][0], dict)
    assert sm["top_hits"][0]["id"] == "m1"
