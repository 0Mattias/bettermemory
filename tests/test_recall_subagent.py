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
they were, and the rows carry the subagent.

Every row here is stamped by one test clock, which the hooks read as
their `now` too, so a held segment and its rebuild compare whole.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import hook as hook_module
from bettermemory import log as bm_log
from bettermemory.config import BehaviorConfig, Config, StorageConfig, TelemetryConfig
from bettermemory.hook import audit_transcript
from bettermemory.recall import (
    SURFACE_BRIEF,
    HookResult,
    RecallState,
    SegmentView,
    prompt_recall,
    session_brief,
)

from . import _transcript_rows as tr
from ._review_rows import Clock
from .test_recall_prompt import BODY, QUERY, PromptWorld

KEY = ("claude-code", "transcript-parent")
AGENT = "a1b2c3d4e5f6a7b8c"
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
        tr.append(self.transcript, tr.user(text, prompt_id), tr.assistant("noted"))
        audit_transcript(
            transcript_path=self.transcript,
            session_id=KEY[1],
            config=self.config,
            store=self.world.store,
            prompt_id=prompt_id,
            state=self.state(),
        )

    def subagent_boundary(
        self, agent: str, at: datetime, uuid: str = "sub-boundary-1"
    ) -> Path:
        """The subagent's compaction marker, in its own transcript."""
        self.subagents.mkdir(parents=True, exist_ok=True)
        path = self.subagents / f"agent-{agent}.jsonl"
        if not path.exists():
            tr.write(path, {**tr.user("the parent's task", "p0"), "agentId": agent})
        tr.append(path, {**tr.boundary(at, uuid), "agentId": agent})
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

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.world.store.iter_events() if e["kind"] == kind]


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
    """The subagent's compact SessionStart prints its brief, and the
    parent's segment, signal and shows are what they were before it: the
    parent's next prompt still delivers the id only the subagent was
    shown."""
    first = _memory(world, clock)
    assert parent.start("startup").ids == (first,)
    second = world.write(OTHER_BODY).id
    clock.advance(seconds=30)
    before = parent.segment()
    assert (before.segment, before.shown, parent.pending()) == (
        0,
        frozenset({first}),
        None,
    )

    shown = parent.start("compact", agent_id=AGENT, agent_type="Explore")

    assert set(shown.ids) == {first, second}
    assert parent.segment() == before
    assert parent.rebuilt() == before
    assert parent.pending() is None
    clock.advance(seconds=30)
    assert parent.prompt(OTHER_QUERY).ids == (second,)
    [delivery] = parent.rows("prompt_recall")
    assert delivery["segment"] == 0
    assert delivery["shown_ids"] == [second]


def test_the_subagents_brief_row_carries_the_subagent(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """The row is recorded under the session's id, as every hook row of
    the conversation is, and names the subagent, where its identity was
    read, and the segment of the subagent's own scope."""
    memory = _memory(world, clock)
    shown = parent.start("compact", agent_id=AGENT, agent_type="Explore")
    [row] = parent.rows("session_brief")
    assert row["event_id"] == shown.event_id
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
    assert row["ids"] == [memory]
    assert row["transcript_path"] == str(parent.transcript)
    scope = parent.scope(AGENT)
    assert scope.shown == {memory}
    assert scope.shown_by[memory] == (SURFACE_BRIEF, shown.event_id)


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


def test_a_subagent_compaction_that_shows_nothing_records_its_own_segment(
    parent: Parent, clock: Clock
) -> None:
    """With nothing to brief, the compact still records `segment_opened`
    for the subagent's scope, and the parent's state reads none of it."""
    parent.start("compact", agent_id=AGENT)
    [row] = parent.rows("segment_opened")
    assert (row["agent_id"], row["agent_from"], row["marker_absent"]) == (
        AGENT,
        "payload",
        True,
    )
    assert "agent_type" not in row
    assert parent.pending() is None
    assert parent.rebuilt().segment == 0
    assert parent.scope(AGENT).segment == 0


def test_the_subagents_segments_open_on_its_own_transcripts_markers(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """A subagent compacted twice. Its second compact SessionStart reads
    the first one's marker in the subagent's own transcript and opens the
    subagent's next segment at the first compact, holding what that
    compact's brief showed, by the session's own rule (RS-F5); the second
    compact's marker is not written yet, so its brief is a resume's. The
    parent's transcript holds no marker, and its segment never moves."""
    memory = _memory(world, clock)
    first = clock.now
    assert parent.start("compact", agent_id=AGENT).ids == (memory,)
    parent.subagent_boundary(AGENT, first + timedelta(seconds=1))
    clock.advance(seconds=600)
    again = parent.start("compact", agent_id=AGENT)
    assert again.ids == ()
    opened, brief = parent.rows("segment_opened"), parent.rows("session_brief")[-1]
    assert [(r["agent_id"], r["segment"], r["marker"]) for r in opened] == [
        (AGENT, 1, "sub-boundary-1")
    ]
    assert opened[0]["triggered_from"] == "session_hook"
    assert (brief["agent_id"], brief["segment"], brief["omitted"]) == (
        AGENT,
        1,
        [memory],
    )
    assert brief["marker_absent"] is True
    scope = parent.scope(AGENT)
    assert (scope.segment, scope.marker, scope.started_at) == (
        1,
        "sub-boundary-1",
        first,
    )
    assert parent.segment().segment == 0
    assert parent.pending() is None


def test_the_parents_own_compaction_is_unchanged(
    parent: Parent, world: PromptWorld, clock: Clock
) -> None:
    """A compact SessionStart naming no subagent is the session's, as
    before: it holds the signal, and the later read that finds the
    session's own marker opens the session's segment."""
    memory = _memory(world, clock)
    signal = clock.now
    assert parent.start("compact").ids == (memory,)
    assert parent.pending() == signal
    tr.append(parent.transcript, tr.boundary(signal + timedelta(seconds=1)))
    clock.advance(seconds=300)
    parent.stop()
    view = parent.segment()
    assert (view.segment, view.marker, view.shown) == (
        1,
        "boundary-1",
        frozenset({memory}),
    )
    assert parent.rebuilt() == view


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


def test_the_cli_path_passes_the_payloads_subagent_to_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bettermemory session-start` in process reads the same two fields
    off the piped payload."""
    from bettermemory import recall
    from bettermemory.cli import session_start_cmd
    from bettermemory.store import Store

    Store(tmp_path / "store").close()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "store"))
    seen: list[dict[str, Any]] = []

    def brief(**kw: Any) -> HookResult:
        seen.append(kw)
        return HookResult.empty("session_brief")

    monkeypatch.setattr(recall, "session_brief", brief)
    monkeypatch.setattr(
        session_start_cmd,
        "_read_payload",
        lambda: {
            "session_id": KEY[1],
            "source": "compact",
            "agent_id": AGENT,
            "agent_type": "Explore",
        },
    )
    assert session_start_cmd._build_context_block() is None
    [kw] = seen
    assert (kw["session_id"], kw["agent_id"], kw["agent_type"]) == (
        KEY[1],
        AGENT,
        "Explore",
    )
