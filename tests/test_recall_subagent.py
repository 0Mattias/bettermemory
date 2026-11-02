"""A subagent's SessionStart is the subagent's (ENG-150).

Claude Code raises a subagent's compaction as a SessionStart with source
``compact`` under the PARENT's session id and the parent's main transcript
path: read in the client it ships (2.1.296 and 2.1.309), the SessionStart
input is built without the tool-use context that fills the common
``agent_id`` field, and the subagent's ``compact_boundary`` lands in its
own transcript, ``<dir>/<session>/subagents/agent-<agent_id>.jsonl``,
after the hook has run. Before ENG-150 the hook answered it as the
parent's: the parent held a compact signal no marker of its own would
answer, and counted the brief printed into the subagent as shown in its
segment, so its prompt recall held those ids as seen.

A payload that names the subagent (``agent_id``, the hooks reference's
common field for the agent a hook fires in) is answered in the
subagent's own scope: the parent's segment, signal and shows are left as
they were, and the rows carry the subagent. A payload that names none,
which is what Claude Code sends, is the parent's until a later read of
its held signal finds no new marker in the parent's transcript, finds
the parent's conversation gone on past the window with no marker newer
than the one its segments opened on (a compaction of the parent's own
writes its marker before its conversation goes on), and finds the
subagent's marker in its own transcript: the signal is then taken back
for the subagent. The review's cases (D1, D2) are pinned here too, with
what the timing rule cannot tell.

From rs-v8 no compaction shows a brief, the session's or a subagent's
(ENG-115 Stage 2), so a take-back has no ids to withdraw, but for a
signal the log held from a brief before rs-v8 (pinned once). What is pinned
is the rest: the session's segment and shown set are left as they were,
read through its prompt recall; the subagent's segments open on its own
transcript's markers; the signals are held and answered as before; and
the `segment_opened` rows carry the subagent.

Every row here is stamped by one test clock, which the hooks read as
their `now` too, so a held segment and its rebuild compare whole.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import hook as hook_module
from bettermemory import log as bm_log
from bettermemory.config import BehaviorConfig, Config, StorageConfig, TelemetryConfig
from bettermemory.events import Recorder
from bettermemory.hook import audit_transcript
from bettermemory.recall import (
    HOOK_TIMEOUT_SECONDS,
    SUBAGENT_WAIT_SECONDS,
    HookResult,
    RecallState,
    SegmentView,
    prompt_recall,
    session_brief,
)
from bettermemory.recall_counters import compute_surface_report
from bettermemory.time_utils import isoformat_utc

from . import _transcript_rows as tr
from ._review_rows import Clock
from .test_recall_prompt import BODY, QUERY, PromptWorld

KEY = ("claude-code", "transcript-parent")
AGENT = "a1b2c3d4e5f6a7b8c"
OTHER_AGENT = "f0e1d2c3b4a596870"
OTHER_BODY = "the deploy window is tuesday mornings for the payments service"
OTHER_QUERY = "deploy window payments"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The time every row is stamped with and every hook reads as now."""
    clock = Clock(datetime.now(timezone.utc).replace(microsecond=0))
    monkeypatch.setattr(bm_log, "_utcnow_iso", clock.iso)
    monkeypatch.setattr(hook_module, "utcnow", lambda: clock.now)
    return clock


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PromptWorld:
    world = PromptWorld(tmp_path)
    monkeypatch.setattr(
        "bettermemory.origin.capture_caller", lambda *a, **k: world.caller
    )
    return world


class Parent:
    """One Claude Code session and its subagents' SessionStarts, over the
    session's own transcript, against one held state (the daemon,
    `held=True`) or a fresh state per call (the CLI path)."""

    def __init__(
        self, world: PromptWorld, clock: Clock, folder: Path, *, held: bool
    ) -> None:
        self.world = world
        self.clock = clock
        self.transcript = folder / f"{KEY[1]}.jsonl"
        self.subagents = folder / KEY[1] / "subagents"
        self.held = RecallState() if held else None
        self.config = Config(
            storage=StorageConfig(directory=str(world.store_dir)),
            telemetry=TelemetryConfig(enabled=True),
            behavior=BehaviorConfig(session_brief=True),
        )
        self.turns = 0
        folder.mkdir(parents=True, exist_ok=True)
        tr.write(self.transcript)

    def state(self) -> RecallState:
        return self.held if self.held is not None else RecallState()

    def start(self, source: str, **payload: Any) -> HookResult:
        """A SessionStart under the session's id and transcript path, as
        Claude Code raises it for the session and for its subagents
        alike; `payload` adds ``agent_id`` or ``agent_type``."""
        return session_brief(
            session_id=KEY[1],
            source=source,
            transcript_path=str(self.transcript),
            origin=self.world.caller,
            config=self.config,
            store=self.world.store,
            state=self.state(),
            now=self.clock.now,
            **payload,
        )

    def prompt(self, text: str = QUERY) -> HookResult:
        return prompt_recall(
            prompt=text,
            session_id=KEY[1],
            transcript_path=str(self.transcript),
            origin=self.world.caller,
            config=self.config,
            store=self.world.store,
            state=self.state(),
            now=self.clock.now,
        )

    def stop(self, text: str = "a question for the stop") -> None:
        self.turns += 1
        prompt_id = f"turn-{self.turns}"
        stamp = self.clock.iso()
        tr.append(
            self.transcript,
            {**tr.user(text, prompt_id), "timestamp": stamp},
            {**tr.assistant("noted"), "timestamp": stamp},
        )
        audit_transcript(
            transcript_path=self.transcript,
            session_id=KEY[1],
            config=self.config,
            store=self.world.store,
            prompt_id=prompt_id,
            state=self.state(),
        )

    def goes_on(self) -> None:
        """The session's conversation goes on: a subagent's result reaches
        it and it answers, both rows stamped now."""
        stamp = self.clock.iso()
        tr.append(
            self.transcript,
            {**tr.tool_result(64, "turn-subagent"), "timestamp": stamp},
            {**tr.assistant("carrying on"), "timestamp": stamp},
        )

    def subagent_boundary(
        self, agent: str, at: datetime, uuid: str = "sub-boundary-1"
    ) -> Path:
        """The subagent's compaction marker, in its own transcript, last
        written now by the test clock (the subagent scan reads only the
        transcripts written since the signal's window opened)."""
        self.subagents.mkdir(parents=True, exist_ok=True)
        path = self.subagents / f"agent-{agent}.jsonl"
        if not path.exists():
            tr.write(path, {**tr.user("the parent's task", "p0"), "agentId": agent})
        tr.append(path, {**tr.boundary(at, uuid), "agentId": agent})
        written = self.clock.now.timestamp()
        os.utime(path, (written, written))
        return path

    def segment(self) -> SegmentView:
        return self.state().segment(self.world.store, KEY, now=self.clock.now)

    def rebuilt(self) -> SegmentView:
        return RecallState().segment(self.world.store, KEY, now=self.clock.now)

    def pending(self) -> datetime | None:
        return self.state().pending_compaction(
            self.world.store, KEY, now=self.clock.now
        )

    def scope(self, agent: str) -> SegmentView:
        """The subagent's own scope of the session, from the log."""
        return RecallState(agent_id=agent).segment(
            self.world.store, KEY, now=self.clock.now
        )

    def scope_pending(self, agent: str) -> datetime | None:
        """When the subagent's own scope's held compact signal came."""
        return RecallState(agent_id=agent).pending_compaction(
            self.world.store, KEY, now=self.clock.now
        )

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.world.store.iter_events() if e["kind"] == kind]

    def taken(self) -> list[dict[str, Any]]:
        """The `segment_opened` rows that took a held signal of the
        session's back for a subagent."""
        return [r for r in self.rows("segment_opened") if "withdrawn" in r]


@pytest.fixture(params=["held", "cli"])
def parent(
    request: pytest.FixtureRequest, world: PromptWorld, clock: Clock, tmp_path: Path
) -> Iterator[Parent]:
    yield Parent(world, clock, tmp_path / "project", held=request.param == "held")


def _memory(world: PromptWorld, clock: Clock) -> str:
    """A memory the brief selects and `QUERY` reaches."""
    memory = world.write(BODY).id
    clock.advance(seconds=1)
    return memory


# ---------------------------------------------------------------------------
# The subagent named in the payload
# ---------------------------------------------------------------------------


def test_a_subagent_compaction_named_in_the_payload_leaves_the_parent_alone(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """The subagent's compact SessionStart shows no brief (rs-v8), and the
    parent's segment, signal and shows are what they were before it: the
    parent's next prompt still holds the id its startup showed as seen
    in segment 0."""
    first = _memory(world, clock)
    assert parent.start("startup").ids == (first,)
    clock.advance(seconds=30)
    before = parent.segment()
    assert (before.segment, before.shown, parent.pending()) == (
        0,
        frozenset({first}),
        None,
    )

    shown = parent.start("compact", agent_id=AGENT, agent_type="Explore")

    assert (shown.ids, shown.event_id) == ((), None)
    assert parent.segment() == before
    assert parent.rebuilt() == before
    assert parent.pending() is None
    clock.advance(seconds=30)
    assert parent.prompt().ids == ()
    [held] = parent.rows("recall_held")
    assert held["segment"] == 0
    assert held["held"] == [{"id": first, "reason": "seen_in_segment"}]


def test_the_subagents_compaction_records_its_segment_with_the_subagent(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """On a store the brief selects from, a subagent's compaction shows no
    brief, as the session's does (rs-v8), and records `segment_opened`:
    under the session's id, as every hook row of the conversation is,
    naming the subagent, where its identity was read, ``agent_type`` when
    the payload names one, and the segment of the subagent's own scope,
    which holds the compaction's signal. The parent's state reads none of
    it."""
    _memory(world, clock)
    signal = clock.now
    shown = parent.start("compact", agent_id=AGENT, agent_type="Explore")
    parent.start("compact", agent_id=OTHER_AGENT)

    assert (shown.ids, shown.event_id) == ((), None)
    assert parent.rows("session_brief") == []
    row, bare = parent.rows("segment_opened")
    assert (row["session"], row["session_id"], row["harness"]) == (
        KEY[1],
        KEY[1],
        KEY[0],
    )
    assert (row["agent_id"], row["agent_type"], row["agent_from"]) == (
        AGENT,
        "Explore",
        "payload",
    )
    assert (row["source"], row["segment"], row["marker_absent"]) == ("compact", 0, True)
    assert row["triggered_from"] == "session_hook"
    assert (bare["agent_id"], bare["agent_from"]) == (OTHER_AGENT, "payload")
    assert "agent_type" not in bare
    assert parent.scope(AGENT).segment == parent.scope(OTHER_AGENT).segment == 0
    assert parent.scope_pending(AGENT) == signal
    assert parent.pending() is None
    assert parent.rebuilt().segment == 0


def test_an_agent_type_alone_is_the_main_threads_agent(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """``agent_type`` without ``agent_id`` is the agent the session runs
    as (``claude --agent``): the SessionStart is the session's own."""
    memory = _memory(world, clock)
    assert parent.start("startup", agent_type="security-reviewer").ids == (memory,)
    [row] = parent.rows("session_brief")
    assert "agent_id" not in row and "agent_type" not in row
    assert parent.segment().shown == {memory}


def test_the_subagents_segments_open_on_its_own_transcripts_markers(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """A subagent compacted twice. Its second compact SessionStart reads
    the first one's marker in the subagent's own transcript and opens the
    subagent's next segment at the first compact, by the session's own
    rule (RS-F5); the second compact's marker is not written yet, so its
    signal is held in the subagent's scope. Neither shows a brief. The
    parent's transcript holds no marker, and its segment never moves."""
    _memory(world, clock)
    first = clock.now
    assert parent.start("compact", agent_id=AGENT).ids == ()
    parent.subagent_boundary(AGENT, first + timedelta(seconds=1))
    clock.advance(seconds=600)
    again = clock.now
    assert parent.start("compact", agent_id=AGENT).ids == ()
    opened = parent.rows("segment_opened")
    assert [
        (r["agent_id"], r["segment"], r.get("marker"), r.get("marker_absent"))
        for r in opened
    ] == [
        (AGENT, 0, None, True),
        (AGENT, 1, "sub-boundary-1", None),
        (AGENT, 1, None, True),
    ]
    assert opened[1]["opened_at"] == isoformat_utc(first)
    assert {r["triggered_from"] for r in opened} == {"session_hook"}
    assert parent.rows("session_brief") == []
    scope = parent.scope(AGENT)
    assert (scope.segment, scope.marker, scope.started_at) == (
        1,
        "sub-boundary-1",
        first,
    )
    assert parent.scope_pending(AGENT) == again
    assert parent.segment().segment == 0
    assert parent.pending() is None


def test_the_parents_own_compaction_is_unchanged(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """A compact SessionStart naming no subagent is the session's, as
    before: it shows no brief (rs-v8) and holds the signal, and the later
    read that finds the session's own marker opens the session's segment
    at the signal, where the prompt recall shows its id again."""
    memory = _memory(world, clock)
    assert parent.prompt().ids == (memory,)
    clock.advance(seconds=5)
    signal = clock.now
    assert parent.start("compact").ids == ()
    assert parent.pending() == signal
    tr.append(parent.transcript, tr.boundary(signal + timedelta(seconds=1)))
    clock.advance(seconds=300)
    parent.stop()
    view = parent.segment()
    assert (view.segment, view.marker, view.started_at) == (1, "boundary-1", signal)
    assert parent.rebuilt() == view
    assert parent.prompt().ids == (memory,)
    assert [r["segment"] for r in parent.rows("prompt_recall")] == [0, 1]


# ---------------------------------------------------------------------------
# The subagent named by its transcript (what Claude Code sends today)
# ---------------------------------------------------------------------------


def _subagent_compacts(
    parent: Parent, world: PromptWorld, clock: Clock
) -> tuple[str, str, datetime]:
    """The receipt's shape (D1, 2026-10-28): the session showed `first`;
    a subagent compacts, and Claude Code raises its SessionStart under the
    session's id and transcript path with no `agent_id`, so its signal is
    held as the session's (from rs-v8 it shows no brief); the subagent's
    marker lands in its own transcript, stamped half a second before the
    hook, and never in the session's. `second` is a memory no hook has
    shown."""
    first = _memory(world, clock)
    assert parent.start("startup").ids == (first,)
    second = world.write(OTHER_BODY).id
    clock.advance(seconds=30)
    signal = clock.now
    assert parent.start("compact").ids == ()
    assert parent.pending() == signal
    assert parent.segment().shown == {first}
    path = parent.subagent_boundary(AGENT, signal - timedelta(milliseconds=500))
    path.with_name(f"{path.stem}.meta.json").write_text('{"agentType": "Explore"}')
    return first, second, signal


@pytest.mark.parametrize(
    ("reading", "triggered_from"),
    [("stop", "stop_hook"), ("prompt", "prompt_hook"), ("resume", "session_hook")],
)
def test_a_held_signal_a_subagents_transcript_answers_is_the_subagents(
    parent: Parent,
    world: PromptWorld,
    clock: Clock,
    reading: str,
    triggered_from: str,
) -> None:
    """The next read of the session's held signal, once the session's
    conversation has gone on past the window (a Stop's own turn, or the
    subagent's result reaching the session before a prompt or a resume),
    finds no marker in the session's transcript and the subagent's marker
    in its own: the signal ends and its row opens the subagent's segment,
    withdrawing nothing (the compaction printed no brief). The session's
    segment neither opens nor resets: its prompt recall still holds the
    startup's id as seen and delivers the id no hook has shown in
    segment 0."""
    first, second, signal = _subagent_compacts(parent, world, clock)
    clock.advance(seconds=300)
    if reading == "stop":
        parent.stop()
    elif reading == "prompt":
        parent.goes_on()
        assert parent.prompt(OTHER_QUERY).ids == (second,)
    else:
        parent.goes_on()
        assert parent.start("resume").ids == ()

    assert parent.pending() is None
    view = parent.segment()
    assert parent.rebuilt() == view
    assert (view.segment, view.marker) == (0, None)
    assert parent.prompt().ids == ()
    if reading != "prompt":
        assert parent.prompt(OTHER_QUERY).ids == (second,)
    [delivery] = parent.rows("prompt_recall")
    assert (delivery["shown_ids"], delivery["segment"]) == ([second], 0)
    assert parent.segment().shown == {first, second}
    [taken] = parent.taken()
    assert (taken["agent_id"], taken["agent_type"], taken["agent_from"]) == (
        AGENT,
        "Explore",
        "transcript",
    )
    assert (taken["session"], taken["source"], taken["segment"]) == (
        KEY[1],
        "compact",
        1,
    )
    assert (taken["marker"], taken["opened_at"]) == (
        "sub-boundary-1",
        isoformat_utc(signal),
    )
    assert taken["withdrawn"] == [] and "brief_event_id" not in taken
    assert taken["triggered_from"] == triggered_from
    scope = parent.scope(AGENT)
    assert (scope.segment, scope.marker, scope.started_at) == (
        1,
        "sub-boundary-1",
        signal,
    )
    assert scope.shown == frozenset()


def test_a_signal_held_from_a_brief_before_rs_v8_takes_its_ids_back(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """A compact's brief recorded before rs-v8, whose signal no read had
    answered when the daemon restarted on a later build: the rebuild holds
    the signal with that brief's event, and the take-back for the
    subagent withdraws the ids the brief printed into it and names the
    brief, so the session's prompt recall shows them again. The live
    store held one such signal when the live path stopped naming a brief
    on the signal."""
    world.write(BODY)
    second = world.write(OTHER_BODY).id
    clock.advance(seconds=30)
    signal = clock.now
    Recorder(
        store=world.store,
        session_id=KEY[1],
        enabled=True,
        worktree_root=world.caller.worktree_root,
        triggered_from="session_hook",
    ).record(
        "session_brief",
        event_id="brief-before-rs-v8",
        session_id=KEY[1],
        harness=KEY[0],
        source="compact",
        segment=0,
        ids=[second],
        chars=64,
        marker_absent=True,
        rule="rs-v7",
    )
    assert parent.pending() == signal
    assert parent.segment().shown == {second}
    path = parent.subagent_boundary(AGENT, signal - timedelta(milliseconds=500))
    path.with_name(f"{path.stem}.meta.json").write_text('{"agentType": "Explore"}')
    clock.advance(seconds=300)
    parent.goes_on()

    assert parent.prompt(OTHER_QUERY).ids == (second,)
    assert parent.pending() is None
    assert parent.rebuilt() == parent.segment()
    [taken] = parent.taken()
    assert (taken["agent_id"], taken["withdrawn"], taken["brief_event_id"]) == (
        AGENT,
        [second],
        "brief-before-rs-v8",
    )
    assert parent.scope(AGENT).shown == {second}


def test_a_read_inside_the_wait_keeps_the_signal_and_the_sessions_marker_wins(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """A subagent's marker sits near the session's own compaction, whose
    marker has not landed yet: a read inside `SUBAGENT_WAIT_SECONDS` takes
    nothing back, and once the session's marker lands the compaction opens
    the session's segment as before."""
    memory = _memory(world, clock)
    assert parent.prompt().ids == (memory,)
    clock.advance(seconds=5)
    signal = clock.now
    assert parent.start("compact").ids == ()
    parent.subagent_boundary(AGENT, signal + timedelta(seconds=2))
    clock.advance(seconds=SUBAGENT_WAIT_SECONDS - 10)
    parent.stop()
    assert parent.pending() == signal
    assert parent.taken() == []
    tr.append(parent.transcript, tr.boundary(signal + timedelta(seconds=1)))
    clock.advance(seconds=60)
    parent.stop()
    view = parent.segment()
    assert (view.segment, view.marker, view.started_at) == (1, "boundary-1", signal)
    absent, opened = parent.rows("segment_opened")
    assert "agent_id" not in absent and absent["marker_absent"] is True
    assert "agent_id" not in opened and opened["marker"] == "boundary-1"
    assert parent.rebuilt() == view
    assert parent.prompt().ids == (memory,)


@pytest.mark.parametrize("offset", [-61, 61])
def test_a_subagent_marker_outside_the_window_takes_nothing(
    parent: Parent, world: PromptWorld, clock: Clock, offset: int
) -> None:
    memory = _memory(world, clock)
    assert parent.start("startup").ids == (memory,)
    clock.advance(seconds=30)
    signal = clock.now
    parent.start("compact")
    parent.subagent_boundary(AGENT, signal + timedelta(seconds=offset))
    clock.advance(seconds=600)
    parent.stop()
    assert parent.pending() == signal
    assert parent.taken() == []
    view = parent.segment()
    assert (view.segment, view.shown) == (0, frozenset({memory}))
    assert parent.rebuilt() == view


def test_a_subagent_compacting_after_the_sessions_own_compaction_is_taken_back(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """Review D1. The session compacted and a Stop opened its segment on
    its marker; a subagent compacts 30 s later. That marker sits within
    the window of the subagent's signal too, but a marker the segment
    already opened on answers no new compaction: the later read takes
    the signal back for the subagent, and the session's segment stays
    the one its own compaction opened, with what it has shown."""
    first = _memory(world, clock)
    assert parent.start("startup").ids == (first,)
    clock.advance(seconds=30)
    own = clock.now
    parent.start("compact")
    tr.append(parent.transcript, tr.boundary(own + timedelta(seconds=1)))
    clock.advance(seconds=5)
    parent.stop()
    assert (parent.segment().segment, parent.segment().marker) == (1, "boundary-1")
    assert parent.prompt().ids == (first,)
    second = world.write(OTHER_BODY).id
    clock.advance(seconds=25)
    signal = clock.now
    assert parent.start("compact").ids == ()
    assert parent.pending() == signal
    parent.subagent_boundary(AGENT, signal - timedelta(milliseconds=500))
    clock.advance(seconds=300)
    parent.stop()

    assert parent.pending() is None
    view = parent.segment()
    assert (view.segment, view.marker, view.shown) == (
        1,
        "boundary-1",
        frozenset({first}),
    )
    assert parent.rebuilt() == view
    taken = parent.taken()
    assert [(r["agent_id"], r["marker"], r["segment"]) for r in taken] == [
        (AGENT, "sub-boundary-1", 1)
    ]
    assert taken[0]["withdrawn"] == []
    clock.advance(seconds=30)
    assert parent.prompt().ids == ()
    assert parent.prompt(OTHER_QUERY).ids == (second,)


def test_a_late_marker_of_the_sessions_own_keeps_its_compaction(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """Review D2, the session's side. The session's own compaction, whose
    marker a slow SessionStart hook holds back, with a subagent's marker
    within the window of its signal: reads past the wait find no marker
    of the session's, but nor has the session's conversation gone on
    past the window, so nothing is taken back. Once the marker lands,
    the compaction opens the session's segment as before, holding what
    was shown since the signal."""
    first = _memory(world, clock)
    assert parent.start("startup").ids == (first,)
    second = world.write(OTHER_BODY).id
    clock.advance(seconds=30)
    signal = clock.now
    assert parent.start("compact").ids == ()
    parent.subagent_boundary(AGENT, signal - timedelta(seconds=30))
    clock.advance(seconds=90)
    assert parent.prompt(OTHER_QUERY).ids == (second,)
    clock.advance(seconds=210)
    assert parent.prompt().ids == ()
    assert parent.pending() == signal
    assert parent.taken() == []

    tr.append(
        parent.transcript,
        tr.boundary(signal + timedelta(seconds=1)),
        {
            **tr.user("the summary", isCompactSummary=True),
            "timestamp": isoformat_utc(signal + timedelta(seconds=25)),
        },
    )
    clock.advance(seconds=60)
    parent.stop()
    view = parent.segment()
    assert (view.segment, view.marker, view.started_at) == (1, "boundary-1", signal)
    assert view.shown == {second}
    assert parent.rebuilt() == view
    assert [r.get("agent_id") for r in parent.rows("segment_opened")] == [None, None]
    clock.advance(seconds=30)
    assert parent.prompt(OTHER_QUERY).ids == ()
    assert parent.prompt().ids == (first,)


def test_a_subagents_compaction_waits_for_the_sessions_conversation(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """Review D2, the subagent's side. The receipt's shape, read past the
    wait before the session's conversation has gone on: the signal stays
    the session's. Once a row of the session's conversation is stamped
    past the window, the next read takes the signal back."""
    _first, second, signal = _subagent_compacts(parent, world, clock)
    clock.advance(seconds=300)
    assert parent.prompt().ids == ()
    assert parent.pending() == signal
    assert parent.taken() == []
    assert parent.segment() == parent.rebuilt()

    parent.goes_on()
    clock.advance(seconds=30)
    assert parent.prompt(OTHER_QUERY).ids == (second,)
    assert parent.pending() is None
    [taken] = parent.taken()
    assert (taken["agent_id"], taken["withdrawn"], taken["triggered_from"]) == (
        AGENT,
        [],
        "prompt_hook",
    )
    assert parent.segment() == parent.rebuilt()


@pytest.mark.parametrize("offset", [90, -90])
def test_a_marker_of_the_sessions_own_outside_the_window_keeps_its_signal(
    parent: Parent, world: PromptWorld, clock: Clock, offset: int
) -> None:
    """Review D2, a marker stamped away from its hook. Claude Code stamps
    the marker of a full compaction after every SessionStart hook has
    returned, so a slow hook moves it past the window (+90 s), and a
    reactive compaction's before its hooks (-90 s). Neither answers the
    signal, as before ENG-150, but the session's conversation goes on
    after it and a subagent's marker sits within the window: the marker,
    newer than the one the segments opened on, keeps the signal the
    session's."""
    first = _memory(world, clock)
    assert parent.start("startup").ids == (first,)
    clock.advance(seconds=30)
    signal = clock.now
    assert parent.start("compact").ids == ()
    parent.subagent_boundary(AGENT, signal + timedelta(seconds=2))
    tr.append(parent.transcript, tr.boundary(signal + timedelta(seconds=offset)))
    clock.advance(seconds=300)
    parent.stop()

    assert parent.pending() == signal
    assert parent.taken() == []
    view = parent.segment()
    assert (view.segment, view.shown) == (0, frozenset({first}))
    assert parent.rebuilt() == view
    clock.advance(seconds=30)
    assert parent.prompt().ids == ()


def test_a_marker_no_segment_opened_on_long_before_keeps_nothing(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """A marker of the session's own that no segment opened on (a
    compaction the hook missed) keeps a later signal the session's only
    within a hook timeout after its stamp: one stamped long before the
    subagent's compaction takes nothing from the take-back."""
    tr.append(parent.transcript, tr.boundary(clock.now, uuid="missed"))
    clock.advance(seconds=HOOK_TIMEOUT_SECONDS + 60)
    first, _second, _signal = _subagent_compacts(parent, world, clock)
    clock.advance(seconds=300)
    parent.stop()

    assert parent.pending() is None
    [taken] = parent.taken()
    assert (taken["agent_id"], taken["marker"], taken["withdrawn"]) == (
        AGENT,
        "sub-boundary-1",
        [],
    )
    view = parent.segment()
    assert (view.segment, view.shown) == (0, frozenset({first}))
    assert parent.rebuilt() == view


def test_rows_of_the_session_before_its_marker_are_taken_for_a_subagent(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """What the timing rule cannot tell (rc14 known limitations), pinned
    so a change to it is deliberate: a compaction of the session's own
    whose conversation reaches the transcript before its marker (no such
    row in the 143 boundaries measured), with a subagent's marker within
    the window of its signal, is taken for the subagent once that
    conversation is past the window. The marker lands after the signal
    has ended, and the session's segment never opens on it: the prompt
    recall holds as seen the id the compaction would have shown again."""
    first = _memory(world, clock)
    assert parent.start("startup").ids == (first,)
    clock.advance(seconds=30)
    signal = clock.now
    assert parent.start("compact").ids == ()
    parent.subagent_boundary(AGENT, signal + timedelta(seconds=2))
    clock.advance(seconds=90)
    parent.stop()
    tr.append(parent.transcript, tr.boundary(signal + timedelta(seconds=1)))
    clock.advance(seconds=30)
    parent.stop()

    assert parent.pending() is None
    view = parent.segment()
    assert (view.segment, view.marker, view.shown) == (0, None, frozenset({first}))
    assert parent.rebuilt() == view
    [taken] = parent.taken()
    assert (taken["agent_id"], taken["withdrawn"]) == (AGENT, [])
    assert parent.prompt().ids == ()


def test_a_workflow_agents_transcript_names_it_too(
    parent: Parent, clock: Clock
) -> None:
    """A workflow's agents keep their transcripts one level down, in
    ``subagents/workflows/<run>/``."""
    signal = clock.now
    parent.start("compact")
    run = parent.subagents / "workflows" / "wf_run-001"
    run.mkdir(parents=True)
    tr.write(
        run / f"agent-{AGENT}.jsonl",
        {**tr.user("a workflow step", "p0"), "agentId": AGENT},
        {**tr.boundary(signal, "wf-boundary"), "agentId": AGENT},
    )
    clock.advance(seconds=300)
    parent.stop()
    assert parent.pending() is None
    [taken] = parent.taken()
    assert (taken["agent_id"], taken["marker"], taken["withdrawn"]) == (
        AGENT,
        "wf-boundary",
        [],
    )
    assert "agent_type" not in taken
    scope = parent.scope(AGENT)
    assert (scope.segment, scope.marker, scope.started_at) == (
        1,
        "wf-boundary",
        signal,
    )


def test_held_and_rebuilt_agree_through_a_subagents_compactions(
    world: PromptWorld, clock: Clock, tmp_path: Path
) -> None:
    """The daemon's held segment and the one rebuilt from the log are the
    same after each hook, through a subagent's compaction taken back, a
    repeat of it, the session's own compaction and a clear."""
    parent = Parent(world, clock, tmp_path / "project", held=True)
    first = _memory(world, clock)
    world.write(OTHER_BODY)
    signals: list[datetime] = []

    def agree() -> None:
        assert parent.segment() == parent.rebuilt()
        assert parent.pending() == RecallState().pending_compaction(
            world.store, KEY, now=clock.now
        )

    def subagent_marker() -> None:
        parent.subagent_boundary(AGENT, signals[-1], uuid=f"sub-{len(signals)}")

    def compact() -> None:
        signals.append(clock.now)
        parent.start("compact")

    steps = [
        lambda: parent.prompt(),
        compact,
        subagent_marker,
        lambda: parent.prompt(OTHER_QUERY),
        lambda: clock.advance(seconds=120),
        lambda: parent.stop(),
        lambda: parent.prompt(OTHER_QUERY),
        compact,
        lambda: clock.advance(seconds=120),
        lambda: parent.stop(),
        lambda: tr.append(parent.transcript, tr.boundary(signals[-1])),
        lambda: parent.stop(),
        lambda: parent.start("clear"),
        lambda: parent.prompt(),
    ]
    for step in steps:
        step()
        agree()
        clock.advance(seconds=10)
    opened = [(r.get("agent_id"), r["segment"]) for r in parent.rows("segment_opened")]
    assert opened == [(None, 0), (AGENT, 1), (None, 0), (None, 1)]
    assert parent.segment().segment == 2
    assert first in parent.segment().shown


# ---------------------------------------------------------------------------
# The counters
# ---------------------------------------------------------------------------


def test_the_surfaces_count_a_subagents_segment_apart_from_the_sessions(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """The subagent's compaction, taken back, opened the subagent's
    segment 1; the session's own compaction opened the session's segment
    1. Eval's surfaces count two segments opened, not one, and read the
    shows as before: the startup's brief, and a prompt recall in each of
    the session's segments (neither compaction shows a brief, rs-v8)."""
    first, second, _signal = _subagent_compacts(parent, world, clock)
    clock.advance(seconds=300)
    parent.stop()
    assert parent.prompt(OTHER_QUERY).ids == (second,)
    signal = clock.now
    parent.start("compact")
    tr.append(parent.transcript, tr.boundary(signal + timedelta(seconds=1)))
    clock.advance(seconds=120)
    parent.stop()
    assert parent.segment().segment == 1
    assert parent.prompt().ids == (first,)
    assert [
        (r.get("agent_id"), r["segment"]) for r in parent.rows("segment_opened")
    ] == [
        (None, 0),
        (AGENT, 1),
        (None, 0),
        (None, 1),
    ]
    surfaces = compute_surface_report(
        list(world.store.iter_events()), start=None, end=None
    )
    assert surfaces is not None
    assert surfaces["segments"] == {"sessions": 1, "opened": 2}
    assert surfaces["brief"]["deliveries"] == 1
    assert surfaces["recall"]["deliveries"] == 2


# ---------------------------------------------------------------------------
# The identity reaches the brief
# ---------------------------------------------------------------------------


async def test_the_daemon_passes_the_payloads_subagent_to_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook client forwards the SessionStart payload whole; the
    daemon's endpoint hands the brief the subagent it names."""
    from bettermemory import recall
    from bettermemory.store import Store

    from .test_daemon import _auth, running_app

    seen: list[dict[str, Any]] = []

    def brief(**kw: Any) -> HookResult:
        seen.append(kw)
        return HookResult.empty("session_brief")

    monkeypatch.setattr(recall, "session_brief", brief)
    store = Store(tmp_path / "store")
    config = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    payload = {
        "session_id": KEY[1],
        "source": "compact",
        "cwd": str(tmp_path),
        "transcript_path": str(tmp_path / f"{KEY[1]}.jsonl"),
        "agent_id": AGENT,
        "agent_type": "Explore",
    }
    async with running_app(config, store) as client:
        response = await client.post(
            "/api/v1/hook/session-start", json=payload, headers=_auth()
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/v1/hook/session-start",
            json={"session_id": KEY[1], "source": "compact", "cwd": str(tmp_path)},
            headers=_auth(),
        )
        assert response.status_code == 200, response.text
    named, unnamed = seen
    assert (named["agent_id"], named["agent_type"]) == (AGENT, "Explore")
    assert (unnamed["agent_id"], unnamed["agent_type"]) == (None, None)
