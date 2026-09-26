"""Tests for the silent-miss telemetry pipeline.

Unit tests for `bettermemory.audit`: `probe_for_miss` is a pure
function, so these pin the threshold rule, the lookback windowing, the
shields and the verdict matrix without a server. The event-field
builders (`turn_audited_fields`, `search_miss_fields`,
`prompt_recall_fields`) and the health rollup of the audit events are
pinned beside them.

The tests do not go through the event log; they hand `probe_for_miss` a
list of dicts directly so the verdict logic is decoupled from the stored
shape. The producer that records these events is the Stop hook
(`hook.run_audit`), exercised in tests/test_hook.py and
tests/test_telemetry_v2.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from bettermemory.audit import (
    DEFAULT_LOOKBACK_SECONDS,
    THRESHOLD_RULE_V1,
    _caller_in_top_hit_project,
    _RETRIEVAL_EVENT_KINDS,
    _VALID_TRIGGERED_FROM,
    MissReport,
    probe_for_miss,
    prompt_recall_fields,
)
from bettermemory.health import compute_health, curation_counts
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin


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


def _list_event(
    *,
    session: str,
    ts: datetime,
    returned: list[str] | None = None,
) -> dict[str, Any]:
    """A `list` event, the shape the retired `memory_list` tool recorded.

    The tool is gone from the surface, but `list` stays a member of
    `_RETRIEVAL_EVENT_KINDS`: stored logs carry these rows, and a replay
    of a turn that listed a scope (with bodies, the same effect as a
    search hit from the model's perspective) must still read as
    retrieved.
    """
    returned_ids = returned or []
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "session": session,
        "kind": "list",
        "count": len(returned_ids),
        "returned": returned_ids,
    }


def _write_event(
    *,
    session: str,
    ts: datetime,
    memory_id: str,
) -> dict[str, Any]:
    """A `memory_write` event. NOT a retrieval — `write` is deliberately
    absent from `_RETRIEVAL_EVENT_KINDS` (a write puts nothing stored in
    front of the model), so its presence in the lookback window must not
    shield the verdict. The proactive-capture tests below pass it to pin
    that the created-time filter, not a write shield, is what keeps a
    same-turn capture from self-flagging."""
    return {
        "ts": ts.isoformat().replace("+00:00", "Z"),
        "session": session,
        "kind": "write",
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


def test_single_content_token_query_returns_no_signal() -> None:
    """Bare continuations ("yes", "continue") and one-content-token
    fragments ("go for it" — "for"/"it" are stopwords) structurally
    always score "high" against any memory that mentions the token
    (1/1 = 1.0). Probe short-circuits to no_signal before the ranker
    runs so the entire single-content-token cohort drops out of the
    search_miss bucket. The gate counts UNIQUE content tokens — the v1
    coverage denominator is unique tokens, so a repeated-word
    continuation ("yes yes", "push it push it") is the same
    single-token class and must not slip past on list length (the body
    mentions both "yes" and "push", so an ungated probe would score
    1/1 = "high" and fire a false miss). probe_query is preserved so a
    `no_signal` report on this path is distinguishable from the
    empty-query branch (which sets probe_query=None)."""
    m = _memory("yes push the backup strategy uses triangular restic replication")
    for query in (
        "yes",
        "continue",
        "go for it",
        "push it",
        "yes yes",
        "push it push it",
    ):
        report = probe_for_miss(
            [m],
            query,
            recent_events=[],
            session_id="sess_x",
            now=_utc(2026, 5, 1),
        )
        assert report.verdict == "no_signal", query
        assert report.top_hits == ()
        assert report.probe_query == query


def test_all_acknowledgment_message_returns_no_signal() -> None:
    """Two-word acknowledgments built from non-stopword filler ("all
    done", "looks good", "sounds good", "thanks, done!") are bare
    continuations the gate documents itself as dropping — but the words
    aren't in search.py's deliberately-short stopword list, so they'd
    otherwise clear the two-token floor and score 2/2 = "high" against
    any ordinary body containing both words (the fixture body contains
    all of them). The audit-local `_ACK_TOKENS` set gates the
    all-acknowledgment cohort to no_signal before the ranker runs."""
    m = _memory(
        "thanks — once the migration is done it all looks good and the "
        "cutover sounds good"
    )
    for query in ("all done", "looks good", "sounds good", "thanks, done!"):
        report = probe_for_miss(
            [m],
            query,
            recent_events=[],
            session_id="sess_x",
            now=_utc(2026, 5, 1),
        )
        assert report.verdict == "no_signal", query
        assert report.top_hits == ()
        assert report.probe_query == query


def test_mixed_acknowledgment_message_passes_the_gate() -> None:
    """Control for `_ACK_TOKENS`: an acknowledgment followed by a real
    request must NOT be gated — the non-acknowledgment tokens
    ("update", "backup", "docs") fall outside the set, so the probe
    runs normally and the miss fires."""
    m = _memory("update the backup docs now it looks good")
    report = probe_for_miss(
        [m],
        "looks good, now update the backup docs",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert report.verdict == "miss"


def test_content_query_sharing_ack_stems_is_probed() -> None:
    """Control for the SURFACE-space comparison in the `_ACK_TOKENS`
    gate: content words whose stems collide with acknowledgment
    spellings ('sound'/'work' under 'sounds'/'works'; likewise 'don'
    under 'done', 'nic' under 'nice') must NOT be gated. Canonicalising
    `_ACK_SURFACE` through the stemming `tokenize` put those stems in
    the set, so "does the sound work" — content tokens
    {'sound', 'work'} — fell entirely inside it and was classified
    no_signal: the probe never ran and the retrieval miss below went
    uncounted. The gate compares unstemmed surfaces, so the query
    reaches the ranker and the miss fires."""
    m = _memory("living room tv: sound works only over the hdmi arc input")
    report = probe_for_miss(
        [m],
        "does the sound work",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert report.verdict == "miss"


def test_bare_numeric_continuation_returns_no_signal() -> None:
    """tokenize() splits dotted strings on ".", so a bare numeric
    continuation ("3.8.0" answering "which version should I pin?")
    fragments into digit pseudo-tokens that would otherwise clear the
    two-token floor and score "high" against any unrelated
    digit-bearing body (version pins, ports, cron specs). Pure-digit
    tokens don't count toward the gate, so the bare-numeric cohort is
    no_signal — at HEAD before the fix, "3.8.0" and "option 2" both
    flagged "miss" against the unrelated fixture bodies here."""
    memories = [
        _memory("web app toolchain: node 18.0.1 with pnpm 8.3.2"),
        _memory("backups: option b mirrors to s3 every 2 hours"),
    ]
    for query in ("3.8.0", "option 2", "3.12"):
        report = probe_for_miss(
            memories,
            query,
            recent_events=[],
            session_id="sess_x",
            now=_utc(2026, 5, 1),
        )
        assert report.verdict == "no_signal", query
        assert report.top_hits == ()
        assert report.probe_query == query


def test_substantive_query_with_version_number_passes_the_gate() -> None:
    """Control for the digit exclusion: it only drops digit fragments
    from the gate COUNT — a substantive query that happens to carry a
    version still passes on its word tokens ("pin", "python") and the
    ranker scores the digits normally."""
    m = _memory("pin python 3.12 for the data toolchain")
    report = probe_for_miss(
        [m],
        "pin python 3.12",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert report.verdict == "miss"


def test_two_content_token_query_passes_the_gate() -> None:
    """The MIN_PROBE_CONTENT_TOKENS floor lets two-content-token queries
    through. Pins the floor against an off-by-one that would also
    suppress legitimate short queries like "backup strategy"."""
    m = _memory("backup strategy uses triangular restic replication")
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    # Two content tokens → gate passes → ranker runs → miss verdict
    # because no search fired in the lookback window.
    assert report.verdict == "miss"


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


def test_injected_naive_now_is_coerced_against_tz_aware_event_ts() -> None:
    """An INJECTED naive `now` must not raise when the lookback walk
    compares it against a retrieval event's tz-aware `ts`.

    Regression for the miss-probe seam: `probe_for_miss` only coerced
    the *unset* (`None`) case (`now = now or datetime.now(...)`), so a
    caller passing a naive `datetime(...)` (no tzinfo) flowed uncoerced
    into `_count_recent_retrievals`, where `cutoff = now - timedelta(...)`
    stayed naive while the event `ts` parsed by `parse_event_ts` is
    always tz-aware. The `ts < cutoff` comparison then raised
    `TypeError: can't compare offset-naive and offset-aware datetimes`
    as soon as ANY retrieval event in the window was walked.

    The existing matrix builds `now` via `_utc(...)` (tz-aware), so the
    naive path was unexercised. This pins it: a naive `now` PLUS a
    matching `search` event (so the comparison at the seam actually
    runs) returns a normal `MissReport` instead of raising. The event is
    placed inside the lookback window, so the shield fires and the
    verdict is `ok` — proving the comparison executed and read correctly.
    """
    m = _memory("backup strategy uses triangular restic replication")
    # Naive — no tzinfo. This is the value that used to slip through.
    naive_now = datetime(2026, 5, 1, 12, 0, 0)
    assert naive_now.tzinfo is None
    # A matching `search` event 30s "before" now (built tz-aware by the
    # helper, the same shape the recorder writes) so the lookback walk
    # reaches the `ts < cutoff` comparison the bug crashed on.
    events = [
        _search_event(
            session="sess_x",
            ts=_utc(2026, 5, 1) - timedelta(seconds=30),
            returned=[m.id],
        ),
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=naive_now,
        lookback_seconds=60,
    )
    # Did not raise: the seam coerced the naive `now` to tz-aware UTC.
    assert isinstance(report, MissReport)
    # The in-window search shielded the miss — proves the comparison both
    # ran and read the event as recent (correct UTC windowing).
    assert report.verdict == "ok"
    assert report.recent_retrieval_count == 1
    # And `checked_at` is the now-coerced, tz-aware value.
    assert report.checked_at.tzinfo is not None


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


def test_memory_created_inside_lookback_window_does_not_flag_miss() -> None:
    """Proactive-capture turns must not self-flag: a memory written THIS
    turn (created inside the lookback window) did not exist when the
    user message arrived, so it cannot be evidence of a retrieval miss.
    Memory bodies routinely echo the user's phrasing, so before the
    created-time filter the just-written memory scored "high" against
    the very message that prompted it — every correct no-search
    proactive write emitted a `search_miss`. The `write` event in the
    window is included to pin that it does NOT shield (write isn't
    retrieval); the filter, not a shield, is what clears the turn. With
    the just-written memory as the only candidate, the probe falls
    through to the ran-and-saw-nothing no_signal branch (probe_query
    set)."""
    now = _utc(2026, 5, 1)
    fresh = _memory(
        "staging deploys switched to blue-green",
        scopes=["infrastructure"],
        created=now - timedelta(seconds=20),
    )
    events = [
        _write_event(
            session="sess_x",
            ts=now - timedelta(seconds=18),
            memory_id=fresh.id,
        ),
    ]
    report = probe_for_miss(
        [fresh],
        "we switched the staging deploys to blue-green",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "no_signal"
    assert report.top_hits == ()
    assert report.probe_query == "we switched the staging deploys to blue-green"


def test_old_memory_still_flags_miss_alongside_same_turn_write() -> None:
    """Control for the created-time filter: an OLD high-relevance memory
    still flags a miss even when the model wrote a different memory this
    turn. The filter drops only the hit that could not have been
    retrieved — it is not a write shield, so older unretrieved hits stay
    free to flag (adding `write` to `_RETRIEVAL_EVENT_KINDS` instead
    would have masked exactly this case)."""
    now = _utc(2026, 5, 1)
    old = _memory("backup strategy uses triangular restic replication")
    fresh = _memory(
        "staging deploys switched to blue-green",
        scopes=["infrastructure"],
        created=now - timedelta(seconds=20),
    )
    events = [
        _write_event(
            session="sess_x",
            ts=now - timedelta(seconds=18),
            memory_id=fresh.id,
        ),
    ]
    report = probe_for_miss(
        [old, fresh],
        "backup strategy",
        recent_events=events,
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "miss"
    assert report.top_hits[0].id == old.id


def test_creation_shield_decoupled_from_wide_lookback() -> None:
    """Round-88 regression: the created-time filter keys on the dedicated
    `creation_shield_seconds` window (default 60s, ~turn duration), NOT
    on `lookback_seconds`. Round 84 calibrated the filter when both
    windows shared 60s; round 85 widened the Stop hook's lookback to
    600s and the filter silently inherited the 10x window, so a memory
    created 1-10 minutes ago — well before this turn's user message,
    and exactly the freshest most-likely-relevant content — was
    structurally invisible to the primary producer's probe. A
    5-minute-old memory with a matching message and zero retrieval
    events must flag a miss at the hook's 600s lookback, exactly as it
    does at the in-process handler's 60s default (the two producers
    returned opposite verdicts for the identical turn pre-fix)."""
    now = _utc(2026, 5, 1)
    m = _memory(
        "backup strategy uses triangular restic replication",
        created=now - timedelta(seconds=300),
    )
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=now,
        lookback_seconds=600,
    )
    assert report.verdict == "miss", (
        "a 5-minute-old memory must be probe-visible at lookback=600; "
        "'no_signal' means the creation shield re-coupled to the "
        "retrieval-shield window"
    )
    assert report.top_hits[0].id == m.id


def test_creation_shield_still_drops_same_turn_write_at_wide_lookback() -> None:
    """Shield direction of the round-88 decoupling: a memory created
    INSIDE the creation-shield window (here 20s ago, the same-turn
    proactive-capture shape) stays filtered even when the caller's
    retrieval lookback is the wide 600s window — decoupling must not
    disable the self-flag protection the filter exists for."""
    now = _utc(2026, 5, 1)
    fresh = _memory(
        "staging deploys switched to blue-green",
        scopes=["infrastructure"],
        created=now - timedelta(seconds=20),
    )
    report = probe_for_miss(
        [fresh],
        "we switched the staging deploys to blue-green",
        recent_events=[],
        session_id="sess_x",
        now=now,
        lookback_seconds=600,
    )
    assert report.verdict == "no_signal"
    assert report.top_hits == ()


def test_same_worktree_retrieval_shields_under_any_session() -> None:
    """Round-88 regression (same-worktree anchor collision): the
    retrieval shield's question is "did the model retrieve in THIS
    worktree within the window", but it used to match a single anchored
    session id — so a concurrent same-worktree session (or a
    mid-conversation server restart) whose later event flipped the
    anchor orphaned every in-window retrieval the previous session made
    and re-fired a false miss. A `search` stamped with the caller's
    `worktree_root` must shield regardless of which session emitted it."""
    now = _utc(2026, 5, 1)
    m = _memory("backup strategy uses triangular restic replication")
    search_ev = _search_event(
        session="sess_A", ts=now - timedelta(seconds=300), returned=[m.id]
    )
    search_ev["worktree_root"] = "/wt/this"
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[search_ev],
        session_id="claude-x",
        # The anchor flipped to the OTHER same-worktree session — the
        # repro's `write under sess_B at T-60s` shape.
        retrieval_session_id="sess_B",
        now=now,
        lookback_seconds=600,
        caller_origin=Origin(worktree_root="/wt/this"),
    )
    assert report.verdict == "ok", (
        "a same-worktree in-window search must shield even when the "
        "session anchor points at the other session"
    )
    assert report.recent_retrieval_count == 1


def test_foreign_worktree_retrieval_does_not_shield_under_any_session() -> None:
    """Control for the worktree-wide shield: a retrieval stamped with a
    DIFFERENT worktree stays invisible to the shield unless its session
    matches the anchor — the any-session widening is scoped to the
    caller's own checkout, so the cross-worktree anti-hijack stance
    (foreign windows' searches must not shield this window's miss)
    is preserved."""
    now = _utc(2026, 5, 1)
    m = _memory("backup strategy uses triangular restic replication")
    search_ev = _search_event(
        session="sess_A", ts=now - timedelta(seconds=300), returned=[m.id]
    )
    search_ev["worktree_root"] = "/wt/other"
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[search_ev],
        session_id="claude-x",
        retrieval_session_id="sess_B",
        now=now,
        lookback_seconds=600,
        caller_origin=Origin(worktree_root="/wt/this"),
    )
    assert report.verdict == "miss"
    assert report.recent_retrieval_count == 0


def test_caller_in_project_suppresses_high_relevance_miss() -> None:
    """When the caller is in the same git project as the top-hit memory
    was written from, the probe returns ``"ok"`` instead of ``"miss"`` —
    the model has that project's source tree open, so the absence of a
    memory_search isn't a contract slip.

    Surfaced by 2.7.x dogfood: ~95% of ``search_miss`` events were of
    the form "update bettermemory" / "push it" asked from inside the
    bettermemory repo, where the model already had bettermemory's
    source open and didn't need a memory lookup."""
    repo = "git@github.com:owner/foo.git"
    mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="foo indexer notes about the ingestion pipeline",
        origin=Origin(cwd="/tmp/foo", repo=repo, worktree_root="/tmp/foo"),
    )
    caller = Origin(cwd="/tmp/foo", repo=repo, worktree_root="/tmp/foo")
    report = probe_for_miss(
        [mem],
        "foo indexer notes",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        caller_origin=caller,
    )
    # Sanity: without suppression this would be a miss — high
    # relevance hit, no recent retrieval.
    assert len(report.top_hits) >= 1
    assert report.top_hits[0].relevance == "high"
    assert report.recent_retrieval_count == 0
    # Suppression fires: caller is in foo's repo, top hit is project:foo.
    assert report.verdict == "ok"
    # And the report names WHICH shield spoke — the delivery lane
    # (`hook.run_prompt_recall` under `recall_in_project`) branches on
    # this to serve the cohort the audit deliberately declines to flag.
    assert report.suppressed_by == "project_cohort"


def test_suppressed_by_stamps_retrieval_shield_and_stays_none_elsewhere() -> None:
    """`suppressed_by` is the shield-attribution field: "retrieval" when
    an in-window retrieval event downgraded a clearing hit, and None on
    both a genuine miss and a below-threshold ok — the delivery lane
    must never mistake "nothing relevant" for "suppressed"."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    shielded = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[
            _search_event(
                session="sess_x",
                ts=now - timedelta(seconds=30),
                returned=[m.id],
            )
        ],
        session_id="sess_x",
        now=now,
        lookback_seconds=60,
    )
    assert shielded.verdict == "ok"
    assert shielded.suppressed_by == "retrieval"

    miss = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=now,
    )
    assert miss.verdict == "miss"
    assert miss.suppressed_by is None

    below_bar = probe_for_miss(
        [m],
        "kubernetes ingress annotations for the mail relay",
        recent_events=[],
        session_id="sess_x",
        now=now,
    )
    assert below_bar.verdict in ("ok", "no_signal")
    assert below_bar.suppressed_by is None


def test_prompt_recall_fields_carries_delivered_reason() -> None:
    """The recall event stamps which predicate lane fired: default
    "miss" keeps old call sites and replay readers unchanged; the
    cohort lane passes "project_cohort" explicitly."""
    m = _memory("backup strategy uses triangular restic replication")
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    default_fields = prompt_recall_fields(
        report, session_id="sess_x", probe_mode="hybrid", injected_chars=100
    )
    assert default_fields["delivered_reason"] == "miss"
    cohort_fields = prompt_recall_fields(
        report,
        session_id="sess_x",
        probe_mode="hybrid",
        injected_chars=100,
        delivered_reason="project_cohort",
    )
    assert cohort_fields["delivered_reason"] == "project_cohort"


def test_global_memory_top_hit_does_not_suppress() -> None:
    """A global (non-project-scoped) top hit doesn't trigger the
    project-cwd suppression even when the caller is inside a repo —
    cross-cutting notes (auth keys, home-dir scripts, etc.) should
    still surface as misses when the model didn't search."""
    repo = "git@github.com:owner/foo.git"
    mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="restic backup strategy for the home dir",
        origin=Origin(cwd="/tmp/foo", repo=repo, worktree_root="/tmp/foo"),
    )
    caller = Origin(cwd="/tmp/foo", repo=repo, worktree_root="/tmp/foo")
    report = probe_for_miss(
        [mem],
        "restic backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        caller_origin=caller,
    )
    # No project: scope on the hit → suppression doesn't apply → miss
    # fires normally.
    assert report.verdict == "miss"


def test_low_rank_project_hit_does_not_suppress_global_top_hit_miss() -> None:
    """Mixed-store regression: the verdict threshold reads only the top
    hit, so only that hit can explain away the missing search. A
    low-relevance ``projects:`` memory at rank 2 must not swallow a real
    miss on a global (cross-cutting) memory at rank 1 — before the gate
    was restricted to ``top_hits[:1]``, ANY same-repo project hit in the
    retained top 3 suppressed the verdict, so in a store dominated by
    project memories the global-miss cohort the helper's docstring
    carves out essentially never fired while working in a repo."""
    repo = "git@github.com:owner/foo.git"
    origin = Origin(cwd="/tmp/foo", repo=repo, worktree_root="/tmp/foo")
    global_mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["infrastructure"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="restic backup strategy for the home dir",
        origin=origin,
    )
    project_mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="foo deploy notes: backup the postgres db before each release",
        origin=origin,
    )
    report = probe_for_miss(
        [global_mem, project_mem],
        "restic backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        caller_origin=origin,
    )
    # Sanity: the global memory decides the threshold at rank 1 and the
    # same-repo project memory really is retained at a lower rank — the
    # exact mix that used to trip the suppression.
    assert report.top_hits[0].id == global_mem.id
    assert report.top_hits[0].relevance == "high"
    assert any("projects:foo" in h.scopes for h in report.top_hits[1:]), (
        "fixture broken: project memory not retained in top hits"
    )
    # The rank-2 project hit must not suppress the global top-1 miss.
    assert report.verdict == "miss"


def test_caller_outside_any_repo_does_not_suppress() -> None:
    """When the caller isn't inside a git checkout (caller_origin.repo
    is None), there's no project boundary to suppress against — every
    high-relevance hit fires as a normal miss."""
    repo = "git@github.com:owner/foo.git"
    mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="foo indexer notes",
        origin=Origin(cwd="/tmp/foo", repo=repo, worktree_root="/tmp/foo"),
    )
    # Caller has a cwd but no repo (e.g. running from a home dir).
    caller = Origin(cwd="/home/user")
    report = probe_for_miss(
        [mem],
        "foo indexer notes",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        caller_origin=caller,
    )
    assert report.verdict == "miss"


def test_legacy_memory_without_origin_does_not_suppress() -> None:
    """A memory written before the origin field shipped (origin=None)
    can't trigger suppression — we have no evidence to compare against
    the caller's repo. The miss surfaces as it would have pre-fix."""
    mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="foo indexer notes",
        origin=None,
    )
    caller = Origin(
        cwd="/tmp/foo",
        repo="git@github.com:owner/foo.git",
        worktree_root="/tmp/foo",
    )
    report = probe_for_miss(
        [mem],
        "foo indexer notes",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        caller_origin=caller,
    )
    assert report.verdict == "miss"


def test_caller_in_top_hit_project_helper_cross_repo() -> None:
    """Direct unit test for the helper: a project-tagged memory written
    from one repo doesn't suppress a caller in a different repo. The
    auto-scope filter usually keeps these out of the top hits, but the
    helper checks repos_match explicitly so offline callers that
    bypass auto-scope (eval replays, curation passes) don't lose the
    cross-project signal."""
    from bettermemory.audit import MissHit

    mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="foo indexer notes",
        origin=Origin(
            cwd="/tmp/foo",
            repo="git@github.com:owner/foo.git",
            worktree_root="/tmp/foo",
        ),
    )
    caller = Origin(
        cwd="/tmp/bar",
        repo="git@github.com:owner/bar.git",
        worktree_root="/tmp/bar",
    )
    hit = MissHit(
        id=mem.id,
        score=99.0,
        relevance="high",
        scopes=("projects:foo",),
        snippet="foo indexer notes",
    )
    assert _caller_in_top_hit_project((hit,), [mem], caller) is False


def test_caller_in_top_hit_project_helper_normalizes_remote_urls() -> None:
    """SSH and HTTPS forms of the same remote URL should match — relies
    on repos_match's URL normalisation. Without this, a memory written
    via ``git@github.com:owner/foo.git`` wouldn't suppress for a caller
    in the HTTPS-cloned ``https://github.com/owner/foo`` worktree of
    the same repo (and vice versa)."""
    from bettermemory.audit import MissHit

    mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="foo notes",
        origin=Origin(
            cwd="/tmp/foo",
            repo="git@github.com:owner/foo.git",
            worktree_root="/tmp/foo",
        ),
    )
    caller = Origin(
        cwd="/tmp/foo",
        repo="https://github.com/owner/foo",
        worktree_root="/tmp/foo",
    )
    hit = MissHit(
        id=mem.id,
        score=99.0,
        relevance="high",
        scopes=("projects:foo",),
        snippet="foo notes",
    )
    assert _caller_in_top_hit_project((hit,), [mem], caller) is True


# ---------------------------------------------------------------------------
# Closed-protocol pin for the `kind` whitelist consumed by
# `recent_retrieval_count` accumulator in `probe_for_miss`.
#
# `_RETRIEVAL_EVENT_KINDS` (`audit.py:96`) gates which event kinds count
# as "the model already retrieved" — `search`, `show`, `list`, plus the
# UserPromptSubmit hook's `prompt_recall` delivery (3.41.0: an injected
# pointer means the turn is not a SILENT miss, and membership is also
# the recall path's self-suppression bound). A silent
# addition to the source set (e.g. a hypothetical `replay` kind) would
# shield turns that shouldn't be shielded — under-counting fresh
# retrievals and inflating `search_miss` false-positives. A silent
# deletion would over-count misses (a retrieval that no longer counts
# triggers a false miss). The existing
# `test_search_show_and_list_all_count_toward_recent_retrieval` below
# pins the model-initiated three via `count == 3` against three events
# (`prompt_recall` has its own shielding test beside the builder tests)
# — catches a
# deletion (count drops to 2) but never imports `_RETRIEVAL_EVENT_KINDS`,
# so an addition slips through silently.
#
# The hardcoded tuple is alphabetised and NOT derived from the source
# set — derivation would silently shrink the expected list when the
# source shrinks, defeating the deletion guard. Mirrors the
# `_EXPECTED_USE_OUTCOMES` shape (db81630) on a different surface.
#
# Negative-control: adding `"bogus"` to `_RETRIEVAL_EVENT_KINDS` fails
# `test_retrieval_event_kinds_match_frozenset` (set inequality). Revert
# restores green.
_EXPECTED_RETRIEVAL_EVENT_KINDS: tuple[str, ...] = (
    "list",
    "prompt_recall",
    "search",
    "show",
)


def test_retrieval_event_kinds_match_frozenset() -> None:
    """Guard so additions to ``_RETRIEVAL_EVENT_KINDS`` (the closed-protocol
    whitelist consumed by the ``recent_retrieval_count`` accumulator in
    ``probe_for_miss``) are mirrored in the hardcoded
    ``_EXPECTED_RETRIEVAL_EVENT_KINDS`` tuple — otherwise a new retrieval
    kind could ship without a regression case, silently under-counting
    fresh retrievals and inflating ``search_miss`` false-positives.
    Mirrors ``test_use_outcomes_match_frozenset`` in
    ``tests/test_server_record_use_provenance.py`` — same closed-protocol
    addition-guard pattern on a different surface."""
    assert set(_EXPECTED_RETRIEVAL_EVENT_KINDS) == set(_RETRIEVAL_EVENT_KINDS)


def test_search_show_and_list_all_count_toward_recent_retrieval() -> None:
    """Mixed search, show, and list events accumulate. The count surfaces
    all three. Pins the `_RETRIEVAL_EVENT_KINDS` consumer clause for the
    `list` kind alongside `search` and `show` — a regression that dropped
    `"list"` from the frozenset would silently increase false-positive
    `search_miss` flags whenever the model used `memory_list` (e.g.
    session-start scope overview) as its retrieval primitive. Mirrors
    the per-kind shielding tests above; the load-bearing assertion is
    the count==3 — drop any one kind and the count drops to 2."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        _search_event(
            session="sess_x",
            ts=now - timedelta(seconds=30),
            returned=[m.id],
        ),
        _show_event(
            session="sess_x",
            ts=now - timedelta(seconds=20),
            memory_id=m.id,
        ),
        _list_event(
            session="sess_x",
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
    assert report.verdict == "ok"
    assert report.recent_retrieval_count == 3


def test_prompt_recall_event_shields_the_probe() -> None:
    """A `prompt_recall` event inside the lookback window shields the
    verdict exactly like a model-initiated retrieval — the fourth
    member of `_RETRIEVAL_EVENT_KINDS` (3.41.0). This is the property
    the recall path's honesty rests on twice over: the Stop hook's
    audit of an injection-served turn must report `ok` rather than
    re-flag a delivered pointer as a silent miss, and a second
    injection inside the window must self-suppress (the recall hook
    runs this same probe, so the shield IS its anti-spam bound).
    Deleting `"prompt_recall"` from the frozenset flips this verdict
    to `"miss"`; the count assertion pins the consumer clause the way
    the three-kind test above does for the model-initiated kinds."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        {
            "ts": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "session": "sess_x",
            "kind": "prompt_recall",
            "top_hits": [{"id": m.id}],
            "triggered_from": "prompt_hook",
        }
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


def test_prompt_recall_fields_shape_and_guard() -> None:
    """`prompt_recall_fields` is the canonical builder for the recall
    hook's event — same drift-prevention boundary as
    `turn_audited_fields` / `search_miss_fields`. Pins the field set
    (the `search_miss` replay shape plus `probe_mode` and
    `injected_chars`, minus nothing), the `"prompt_hook"` default, and
    the closed-set guard: a typo'd `triggered_from` raises at the
    builder rather than producing unsplittable eval rows."""
    now = _utc(2026, 5, 1)
    report = MissReport(
        verdict="miss",
        checked_at=now,
        session_id="transcript-abc",
        lookback_seconds=600,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=(),
        probe_query="backup strategy",
    )
    fields = prompt_recall_fields(
        report,
        session_id="transcript-abc",
        probe_mode="hybrid",
        injected_chars=712,
    )
    assert fields["triggered_from"] == "prompt_hook"
    assert fields["probe_mode"] == "hybrid"
    assert fields["injected_chars"] == 712
    assert fields["threshold_rule"] == THRESHOLD_RULE_V1
    assert fields["probe_query"] == "backup strategy"
    assert fields["session_id"] == "transcript-abc"
    assert fields["recent_retrieval_count"] == 0
    assert fields["top_hits"] == []
    assert fields["event_id"]
    with pytest.raises(ValueError, match="triggered_from"):
        prompt_recall_fields(
            report,
            session_id="transcript-abc",
            probe_mode="hybrid",
            injected_chars=712,
            triggered_from="prompt-hook",
        )


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


def test_probe_mode_default_is_hybrid() -> None:
    """Default falls to hybrid (the package default since 2.6.8) —
    matches what the model would do absent a config override. Pin the
    default so a future drift is deliberate, not silent."""
    import inspect

    sig = inspect.signature(probe_for_miss)
    assert sig.parameters["mode"].default == "hybrid"


def test_probe_mode_semantic_is_removed_and_no_signal_reason_stays_none() -> None:
    """`mode="semantic"` was removed with the embedding lane in 4.0.0.

    The probe forwards the mode to `search`, whose runtime guard raises
    on it like any unknown mode — config normalisation
    (`config._coerce_search_mode`) means no production Stop hook can
    reach this branch, so the raise only faces programmatic callers.
    `no_signal_reason` survives as a FIELD (recorded events carry its
    one historical value and replay must keep reading them), but every
    current no-signal branch leaves it None."""
    m = _memory("backup strategy uses triangular restic replication")
    with pytest.raises(ValueError, match="unknown audit probe mode"):
        probe_for_miss(
            [m],
            "backup strategy",
            recent_events=[],
            session_id="sess_x",
            now=_utc(2026, 5, 1),
            mode="semantic",
        )
    empty_store = probe_for_miss(
        [],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert empty_store.verdict == "no_signal"
    assert empty_store.no_signal_reason is None
    assert empty_store.to_dict()["no_signal_reason"] is None


def test_probe_half_life_days_matches_run_search_ranking() -> None:
    """Probe-matches-the-ranker for the recency knob: a non-default
    `half_life_days` must reorder the probe's hits exactly as it
    reorders `run_search` — pre-fix the probe hardwired the 30.0
    default, so any user with a configured
    `recency_boost_half_life_days` had the probe ranking with a
    different scorer than production retrieval.

    Fixture: six distinct query tokens; per-term body TF saturates at 2,
    so the OLD memory (every token twice) holds raw 12 while the NEW one
    (five tokens twice, one once) holds raw 11 — a ~9.1% gap, inside the
    recency boost's 10% ceiling. Under the default 30-day half-life
    the 1-day-old memory's ~+9.7% boost flips the order; under a
    0.5-day half-life its boost decays to ~+1.4% and the old memory's
    base score wins. The flip is the proof the parameter is
    load-bearing; the parity assertion is the proof the probe and the
    ranker read the same value."""
    from bettermemory.search import search as run_search

    now = _utc(2026, 5, 1)
    query = "backup strategy restic replication offsite archive"
    tokens = query.split()
    old_strong = _memory(
        " ".join(t for t in tokens for _ in range(2)),
        created=now - timedelta(days=300),
    )
    new_close = _memory(
        " ".join(t for t in tokens[:5] for _ in range(2)) + " archive",
        created=now - timedelta(days=1),
    )
    memories = [old_strong, new_close]

    default_report = probe_for_miss(
        memories,
        query,
        recent_events=[],
        session_id="sess_x",
        now=now,
        mode="keyword",
    )
    assert default_report.top_hits[0].id == new_close.id

    short_report = probe_for_miss(
        memories,
        query,
        recent_events=[],
        session_id="sess_x",
        now=now,
        mode="keyword",
        half_life_days=0.5,
    )
    assert short_report.top_hits[0].id == old_strong.id

    # Parity: the probe's ordering under the non-default half-life is
    # identical to run_search's under the same value.
    hits = run_search(
        memories,
        query,
        max_results=3,
        now=now,
        mode="keyword",
        half_life_days=0.5,
    )
    assert [h.id for h in hits] == [h.id for h in short_report.top_hits]


def test_probe_forwards_ranker_config_to_run_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring pin for the threaded ranker knobs: `half_life_days` and
    `conversational` must reach `run_search` verbatim — the
    probe-matches-the-ranker rule is only as good as the forwarding —
    and the expansion leg must stay off, because production never ranks
    with it."""
    from bettermemory import audit as audit_mod
    from bettermemory.search import search as real_run_search

    m = _memory("backup strategy uses triangular restic replication")
    captured: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_run_search(*args, **kwargs)

    monkeypatch.setattr(audit_mod, "run_search", spy)
    probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        mode="keyword",
        half_life_days=7.0,
        conversational=False,
    )
    assert captured["half_life_days"] == 7.0
    assert captured["conversational"] is False
    assert captured["rescue_expansion"] is False


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
    # `ts >= cutoff` semantics in `_count_recent_retrievals`.
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
    # exactly on the cutoff. _count_recent_retrievals uses `ts < cutoff`
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
    assert payload["silent_misses"] == {
        "audited_total": 0,
        "miss_total": 0,
        "unique_miss_memories": 0,
        "no_signal_total": 0,
    }


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


def test_turn_audited_fields_carries_no_signal_reason_when_set() -> None:
    """Round-88 regression: `no_signal_reason` never reached the wire —
    `turn_audited_fields` omitted it, so the only place
    `semantic_model_unavailable` existed was the tool-response dict and
    a STRUCTURAL no_signal (the Stop hook's permanent semantic-mode
    state) was event-identical to a benign bare-continuation one. The
    builder must forward the reason when the report carries one."""
    from bettermemory.audit import MissReport, turn_audited_fields

    report = MissReport(
        verdict="no_signal",
        checked_at=_utc(2026, 5, 22),
        session_id="s1",
        lookback_seconds=600,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
        probe_query="backup strategy",
        no_signal_reason="semantic_model_unavailable",
    )
    ta = turn_audited_fields(
        report,
        session_id="s1",
        probe_mode="semantic",
        assistant_present=False,
        triggered_from="stop_hook",
    )
    assert ta["no_signal_reason"] == "semantic_model_unavailable"


def test_turn_audited_fields_omits_no_signal_reason_when_none() -> None:
    """Omit-when-None direction of the round-88 additive field: the
    common non-no_signal event (and the legacy no-signal classes that
    set no reason) keeps its exact pre-existing shape, so the event log
    stays churn-free and shape-stable for existing consumers."""
    from bettermemory.audit import MissReport, turn_audited_fields

    report = MissReport(
        verdict="ok",
        checked_at=_utc(2026, 5, 22),
        session_id="s1",
        lookback_seconds=60,
        recent_retrieval_count=1,
        threshold_rule=THRESHOLD_RULE_V1,
        probe_query="backup strategy",
    )
    ta = turn_audited_fields(
        report,
        session_id="s1",
        probe_mode="hybrid",
        assistant_present=True,
        triggered_from="mcp_tool",
    )
    assert "no_signal_reason" not in ta


# ---------------------------------------------------------------------------
# Closed-protocol pin for the `triggered_from` discriminator consumed by
# `turn_audited_fields` and `search_miss_fields`.
#
# `_VALID_TRIGGERED_FROM` (`audit.py:134`) is the source discriminator
# that downstream eval rollups `groupby`-split on. A silent addition
# produces unsplittable eval rows (a new source emits a value the
# downstream consumer doesn't know how to bucket); a silent deletion
# means a legitimate source raises at the dispatch boundary. The
# existing for-loop in `test_turn_audited_fields_rejects_unknown_
# triggered_from` below covers deletions per-iteration (a dropped
# member fails the positive-case round-trip) but never imports
# `_VALID_TRIGGERED_FROM`, so an addition couldn't be caught.
#
# The hardcoded tuple is alphabetised and NOT derived from the source
# set — derivation would silently shrink the expected list when the
# source shrinks, defeating the deletion guard. Mirrors the
# `_EXPECTED_USE_OUTCOMES` shape (db81630) on a different surface.
#
# Negative-control: adding `"bogus"` to `_VALID_TRIGGERED_FROM` fails
# `test_valid_triggered_from_match_frozenset` (set inequality). Revert
# restores green.
_EXPECTED_VALID_TRIGGERED_FROM: tuple[str, ...] = (
    "mcp_tool",
    "prompt_hook",
    "session_capture",
    "stop_hook",
)


def test_valid_triggered_from_match_frozenset() -> None:
    """Guard so additions to ``_VALID_TRIGGERED_FROM`` (the closed-protocol
    discriminator consumed by ``turn_audited_fields`` /
    ``search_miss_fields``) are mirrored in the hardcoded
    ``_EXPECTED_VALID_TRIGGERED_FROM`` tuple — otherwise a new source
    could ship and downstream eval rollups would silently emit
    unsplittable rows (the ``groupby`` consumer has no bucket for it).
    Mirrors ``test_use_outcomes_match_frozenset`` in
    ``tests/test_server_record_use_provenance.py`` — same closed-protocol
    addition-guard pattern on a different surface."""
    assert set(_EXPECTED_VALID_TRIGGERED_FROM) == set(_VALID_TRIGGERED_FROM)


def test_turn_audited_fields_rejects_unknown_triggered_from() -> None:
    """`triggered_from` is a closed-set discriminator
    (`"stop_hook" | "mcp_tool"`) but Python doesn't enforce the
    Literal at call time. A typo elsewhere (`"stop-hook"`,
    `"mcptool"`) would silently produce unsplittable eval rows since
    downstream consumers `groupby`-split on this field. The builder
    raises at the dispatch boundary, mirroring the search-mode guard
    in `search.py:761`.

    Pinned against the hardcoded ``_EXPECTED_VALID_TRIGGERED_FROM``
    tuple so a deletion from ``_VALID_TRIGGERED_FROM`` fails the loop
    loudly rather than silently shrinking. The companion
    ``test_valid_triggered_from_match_frozenset`` catches the addition
    side.
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

    # Negative case: bogus value rejected by both builders.
    with pytest.raises(ValueError, match="triggered_from"):
        turn_audited_fields(
            report,
            session_id="s1",
            probe_mode="keyword",
            assistant_present=True,
            triggered_from="stop-hook",  # typo: hyphen instead of underscore
        )
    with pytest.raises(ValueError, match="triggered_from"):
        search_miss_fields(report, session_id="s1", triggered_from="mcptool")

    # Positive case: the two canonical values still flow through
    # unchanged. Keeps the closed set honest — a future broadening
    # would require adding the new value to `_VALID_TRIGGERED_FROM`
    # and updating `_EXPECTED_VALID_TRIGGERED_FROM` in one diff (the
    # companion `test_valid_triggered_from_match_frozenset` enforces
    # the latter).
    for value in _EXPECTED_VALID_TRIGGERED_FROM:
        ta = turn_audited_fields(
            report,
            session_id="s1",
            probe_mode="keyword",
            assistant_present=True,
            triggered_from=value,
        )
        assert ta["triggered_from"] == value
        sm = search_miss_fields(report, session_id="s1", triggered_from=value)
        assert sm["triggered_from"] == value


def test_prompt_recall_under_caller_id_shields_when_server_session_anchored() -> None:
    """The delivery lane's no-checkout shape: a `prompt_recall` event
    records under the CALLER's transcript id and — hooks outside a git
    checkout — carries no worktree stamp, while
    `_latest_in_process_session` anchors the server's `sess_<hex>`.
    The shield matches the UNION of the anchor and the caller's own id;
    an anchored-server-only match orphans the delivery, re-flags the
    served turn as a silent miss, and defeats the anti-spam bound (the
    recall hook runs this same probe as its suppression check).

    Mutation-soundness: restoring the pre-fix single-id anchor
    (`session_id=retrieval_session_id or session_id`) flips the verdict
    to "miss" and the count to 0."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        {
            "ts": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "session": "cc-transcript-1",
            "kind": "prompt_recall",
            "top_hits": [{"id": m.id}],
            "triggered_from": "prompt_hook",
        }
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="cc-transcript-1",
        retrieval_session_id="sess_anchor",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "ok"
    assert report.recent_retrieval_count == 1


def test_unrelated_session_still_does_not_shield_with_anchor_present() -> None:
    """The union is {anchor, caller} and nothing more — an event under a
    THIRD session id shields neither, keeping the per-session audit
    boundary that `test_show_in_different_session_does_not_shield` pins
    for the anchorless path."""
    m = _memory("backup strategy uses triangular restic replication")
    now = _utc(2026, 5, 1)
    events = [
        {
            "ts": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "session": "sess_SOMEONE_ELSE",
            "kind": "search",
            "triggered_from": "stop_hook",
        }
    ]
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=events,
        session_id="cc-transcript-1",
        retrieval_session_id="sess_anchor",
        now=now,
        lookback_seconds=60,
    )
    assert report.verdict == "miss"
    assert report.recent_retrieval_count == 0


def test_an_indeterminate_origin_declines_to_declare_a_miss() -> None:
    """`origin.capture()` returns a null `repo` both when git said "not a
    repository" and when git could not be asked at all — no binary, a
    timeout. The project-cohort shield reads the null as "the caller is
    nowhere" and stands down, so a turn asked from inside the matching
    repo while git was unreachable was published as a `miss` against
    the model. A miss is a verdict; could-not-ask never manufactures
    one. The measured null (git answered "no") still misses."""
    repo = "git@github.com:owner/foo.git"
    mem = Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 1),
        updated=_utc(2026, 1, 1),
        scopes=["projects:foo"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="foo indexer notes about the ingestion pipeline",
        origin=Origin(cwd="/tmp/foo", repo=repo, worktree_root="/tmp/foo"),
    )
    nowhere = Origin(cwd="/tmp/foo")
    assert nowhere.git_indeterminate is False
    answered = probe_for_miss(
        [mem],
        "foo indexer notes",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        caller_origin=nowhere,
    )
    assert answered.verdict == "miss"
    assert answered.suppressed_by is None

    unknown = Origin(cwd="/tmp/foo")
    unknown._git_indeterminate = True
    declined = probe_for_miss(
        [mem],
        "foo indexer notes",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        caller_origin=unknown,
    )
    assert declined.top_hits[0].relevance == "high", "the hit still clears"
    assert declined.verdict == "ok"
    assert declined.suppressed_by == "origin_indeterminate"
