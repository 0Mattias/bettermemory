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
    _caller_in_top_hit_project,
    _RETRIEVAL_EVENT_KINDS,
    _VALID_TRIGGERED_FROM,
    MissReport,
    probe_for_miss,
)
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.health import compute_health, curation_counts
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
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


def _list_event(
    *,
    session: str,
    ts: datetime,
    returned: list[str] | None = None,
) -> dict[str, Any]:
    """A `memory_list` event — shape mirrors what the server emits at
    `test_server_events.py::test_list_records_count_and_returned`.

    `memory_list` is the third retrieval primitive (alongside search and
    show): surfaces a scope's ids and, with `with_bodies=True`, full
    bodies — same effect as a search hit from the model's perspective.
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
    done", "looks good", "sounds good") are bare continuations the gate
    documents itself as dropping — but the words aren't in search.py's
    deliberately-short stopword list, so they'd otherwise clear the
    two-token floor and score 2/2 = "high" against any ordinary body
    containing both words (the fixture body contains all of them). The
    audit-local `_ACK_TOKENS` set gates the all-acknowledgment cohort
    to no_signal before the ranker runs."""
    m = _memory(
        "once the migration is done it all looks good and the cutover sounds good"
    )
    for query in ("all done", "looks good", "sounds good"):
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
# as "the model already retrieved" — `search`, `show`, `list`. A silent
# addition to the source set (e.g. a hypothetical `replay` kind) would
# shield turns that shouldn't be shielded — under-counting fresh
# retrievals and inflating `search_miss` false-positives. A silent
# deletion would over-count misses (a retrieval that no longer counts
# triggers a false miss). The existing
# `test_search_show_and_list_all_count_toward_recent_retrieval` below
# pins all three via `count == 3` against three events — catches a
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
_EXPECTED_RETRIEVAL_EVENT_KINDS: tuple[str, ...] = ("list", "search", "show")


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


class _ProbeStubSemanticModel:
    """Deterministic sentence-transformers stand-in for the probe tests.

    Same contract and shape as `_StubSemanticModel` in
    test_search_modes.py — embeds over a tiny fixed vocabulary so cosine
    similarity mirrors token overlap. Local copy because the audit tests
    only need the dispatch smoke, not the ranking-quality cases."""

    def __init__(self, vocab: list[str]) -> None:
        self._vocab = vocab

    def encode(self, text: str, *, normalize_embeddings: bool = False) -> list[float]:
        from bettermemory.search import tokenize

        toks = set(tokenize(text))
        vec = [1.0 if term in toks else 0.0 for term in self._vocab]
        if normalize_embeddings:
            norm = sum(x * x for x in vec) ** 0.5
            if norm > 0:
                vec = [x / norm for x in vec]
        return vec


def test_probe_semantic_mode_without_model_returns_no_signal_with_reason() -> None:
    """Regression: `mode="semantic"` with no `semantic_model` used to fall
    through to `run_search`, whose ValueError aborted the entire audit —
    the Stop hook swallowed it before `turn_audited` was recorded, so
    semantic-mode users got zero audit telemetry ever. The probe now
    declines honestly: an explicit `no_signal` with
    `no_signal_reason="semantic_model_unavailable"`, never a silent
    degrade to a different scorer (the module documents against
    wrong-scorer probes)."""
    m = _memory("backup strategy uses triangular restic replication")
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        mode="semantic",
    )
    assert report.verdict == "no_signal"
    assert report.no_signal_reason == "semantic_model_unavailable"
    assert report.top_hits == ()
    # probe_query is preserved so triage can see what the audit declined
    # to probe; the reason field disambiguates from the no-hits branch.
    assert report.probe_query == "backup strategy"
    # The new field is additive on the wire shape; the legacy no-signal
    # branches keep it None.
    assert report.to_dict()["no_signal_reason"] == "semantic_model_unavailable"
    empty_store = probe_for_miss(
        [],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
    )
    assert empty_store.no_signal_reason is None


def test_probe_semantic_mode_with_model_runs_ranker() -> None:
    """With a model threaded through, semantic mode probes normally — the
    same body/query pair the keyword matrix uses scores a high-relevance
    hit and, with no retrieval in the window, flags a miss. Pre-fix this
    raised ValueError out of `run_search` regardless of the caller."""
    m = _memory("backup strategy uses triangular restic replication")
    model = _ProbeStubSemanticModel(
        ["backup", "strategy", "uses", "triangular", "restic", "replication"]
    )
    report = probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        mode="semantic",
        semantic_model=model,
    )
    assert report.verdict == "miss"
    assert report.top_hits[0].id == m.id
    assert report.no_signal_reason is None


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
    """Wiring pin for the three threaded ranker knobs: `half_life_days`,
    `applied_by_id`, and `semantic_model` must reach `run_search`
    verbatim — the probe-matches-the-ranker rule is only as good as
    the forwarding."""
    from bettermemory import audit as audit_mod
    from bettermemory.search import search as real_run_search

    m = _memory("backup strategy uses triangular restic replication")
    captured: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_run_search(*args, **kwargs)

    monkeypatch.setattr(audit_mod, "run_search", spy)
    sentinel_counts = {m.id: 2}
    probe_for_miss(
        [m],
        "backup strategy",
        recent_events=[],
        session_id="sess_x",
        now=_utc(2026, 5, 1),
        mode="keyword",
        half_life_days=7.0,
        applied_by_id=sentinel_counts,
    )
    assert captured["half_life_days"] == 7.0
    assert captured["applied_by_id"] is sentinel_counts
    assert captured["semantic_model"] is None


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


def _backdate_created(memory_dir: Path, memory_id: str) -> None:
    """Push a stored memory's `created`/`updated` an hour into the past.

    The created-time filter in `probe_for_miss` drops memories born
    inside the audit lookback window — they did not exist when the user
    message arrived, so they cannot be retrieval-miss evidence.
    Integration tests that write through the server and audit in the
    same breath would otherwise probe an empty candidate list;
    backdating restores the "memory existed before this turn" shape the
    tests mean to exercise. An hour comfortably predates the clamped
    maximum lookback (600s)."""
    store = Store(memory_dir)
    backdated = datetime.now(timezone.utc) - timedelta(hours=1)
    for path, mem in store.iter_active():
        if mem.id == memory_id:
            store._write_path(
                path,
                mem.model_copy(update={"created": backdated, "updated": backdated}),
            )
            return
    raise AssertionError(f"memory {memory_id!r} not found in store")


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
    written = await _call(
        server,
        "memory_write",
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )
    # The probe only considers memories that predate the lookback window
    # (a same-turn write can't be a retrieval miss) — backdate so the
    # just-written memory is a legitimate probe candidate.
    _backdate_created(memory_dir, written["id"])

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
    # `telemetry.log_queries_verbatim` defaults to False since 2.6.8:
    # `probe_query` lands as `{"hash", "preview", "len"}`. The preview
    # carries the first 32 chars so triage is still possible without
    # storing the full text.
    assert isinstance(miss["probe_query"], dict)
    assert miss["probe_query"]["preview"] == user_query[:32]
    assert miss["probe_query"]["len"] == len(user_query)
    assert len(miss["top_hits"]) >= 1


async def test_audit_turn_no_miss_after_recent_search(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """If the model *did* search before the audit fires, no miss event."""
    server, memory_dir, _ = server_with_events
    written = await _call(
        server,
        "memory_write",
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )
    # Backdate past the created-time filter so the verdict is decided by
    # the retrieval shield, not by an empty candidate list.
    _backdate_created(memory_dir, written["id"])
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
    # Default search_mode is "hybrid" (since 2.6.8) — probe matches.
    assert ev["probe_mode"] == "hybrid"


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
    # Backdate past the created-time filter so the verdict is decided by
    # the show-shield, not by an empty candidate list.
    _backdate_created(memory_dir, written["id"])
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


async def test_audit_turn_semantic_mode_without_extra_records_no_signal(
    memory_dir: Path,
) -> None:
    """Regression: with `search_mode = "semantic"` configured and no
    embeddings model resolvable, memory_audit_turn used to propagate
    `ValueError("mode=semantic requires semantic_model ...")` as a tool
    error on every call — and no `turn_audited` was ever recorded. The
    handler now resolves the model via the same factory production
    search uses; when it comes back None the probe records an explicit
    `no_signal` with a reason instead of crashing."""
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(search_mode="semantic"),
    )
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )
    Store(memory_dir).write(
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )

    report = await _call(server, "memory_audit_turn", user_message="backup strategy")
    assert report["verdict"] == "no_signal"
    assert report["no_signal_reason"] == "semantic_model_unavailable"

    audited = [e for e in _events(memory_dir) if e["kind"] == "turn_audited"]
    assert len(audited) == 1
    assert audited[0]["verdict"] == "no_signal"
    assert audited[0]["probe_mode"] == "semantic"


async def test_audit_turn_threads_ranker_config_into_probe(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process handler must hand the probe the same ranker
    configuration production search uses: the configured
    `recency_boost_half_life_days`, the endorsement tally (when
    `endorsement_boost` is on), and the factory-resolved semantic
    model for hybrid/semantic probe modes. Pre-fix the probe call
    passed none of these, so any non-default config probed with a
    different scorer than the model's retrieval."""
    from bettermemory import builder as builder_mod
    from bettermemory.audit import probe_for_miss as real_probe
    from bettermemory.config import BehaviorConfig

    captured: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_probe(*args, **kwargs)

    monkeypatch.setattr("bettermemory.handlers.audit_turn.probe_for_miss", spy)
    # The factory is consulted for hybrid (the configured mode below);
    # a sentinel that satisfies the model contract proves the resolved
    # object is threaded, not re-resolved or dropped.
    model_sentinel = _ProbeStubSemanticModel(["backup", "strategy"])
    monkeypatch.setattr(
        builder_mod, "_semantic_model_or_none", lambda _cfg: model_sentinel
    )

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(
            recency_boost_half_life_days=7.0, endorsement_boost=True
        ),
    )
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )
    written = Store(memory_dir).write(
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )
    # An explicit (non-auto) applied use — the only kind the
    # endorsement tally counts.
    rec.record("use", ids=[written.id], outcome="applied", auto=False)

    await _call(server, "memory_audit_turn", user_message="backup strategy")
    assert captured["half_life_days"] == 7.0
    assert captured["applied_by_id"] == {written.id: 1}
    assert captured["semantic_model"] is model_sentinel


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
_EXPECTED_VALID_TRIGGERED_FROM: tuple[str, ...] = ("mcp_tool", "stop_hook")


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
