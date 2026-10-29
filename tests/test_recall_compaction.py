"""The compaction marker (RS-F5): a compact SessionStart opens a session
segment only on the ``compact_boundary`` row its transcript holds, newer
than the one the session's segments last opened on.

Measured on 2026-10-19: one session sent 45 compact SessionStarts
against one compaction, and a boundary, stamped within 2.5 s of its
hook, reaches the file only after the hook has run. So a compact without
its marker is a resume and leaves a signal; the next hook that reads the
transcript (the prompt recall, the Stop, a SessionStart) and finds a
marker stamped within `COMPACT_MARKER_WINDOW_SECONDS` of the signal
opens the segment at the signal, keeping what was shown since, however
late that read comes. The signal ends only on a row the log holds, so
the held state and its rebuild from the log take the same transitions,
and the daemon and the CLI path, which rebuilds on every call, agree row
for row.

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
    COMPACT_MARKER_WINDOW_SECONDS,
    SURFACE_RECALL,
    HookResult,
    RecallState,
    SegmentView,
    confirm_compaction,
    prompt_recall,
    session_brief,
)
from bettermemory.time_utils import isoformat_utc

from . import _transcript_rows as tr
from ._review_rows import Clock
from .test_recall_prompt import BODY, QUERY, PromptWorld

KEY = ("claude-code", "transcript-compact")
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


class Session:
    """One Claude Code session's hooks over one transcript, against one
    held state (the daemon, `held=True`) or a fresh state per call (the
    CLI path)."""

    def __init__(
        self, world: PromptWorld, clock: Clock, transcript: Path, *, held: bool
    ) -> None:
        self.world = world
        self.clock = clock
        self.transcript = transcript
        self.held = RecallState() if held else None
        self.config = Config(
            storage=StorageConfig(directory=str(world.store_dir)),
            telemetry=TelemetryConfig(enabled=True),
            behavior=BehaviorConfig(session_brief=False),
        )
        self.turns = 0
        if not transcript.exists():
            tr.write(transcript)

    def state(self) -> RecallState:
        return self.held if self.held is not None else RecallState()

    def start(self, source: str, *, transcript: bool = True) -> HookResult:
        return session_brief(
            session_id=KEY[1],
            source=source,
            transcript_path=str(self.transcript) if transcript else None,
            origin=self.world.caller,
            config=self.config,
            store=self.world.store,
            state=self.state(),
            now=self.clock.now,
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

    def stop(self, text: str = "a question for the stop", reply: str = "noted") -> None:
        """A turn the owner typed, answered, and its Stop."""
        self.turns += 1
        prompt_id = f"turn-{self.turns}"
        tr.append(self.transcript, tr.user(text, prompt_id), tr.assistant(reply))
        audit_transcript(
            transcript_path=self.transcript,
            session_id=KEY[1],
            config=self.config,
            store=self.world.store,
            prompt_id=prompt_id,
            state=self.state(),
        )

    def boundary(self, at: datetime, uuid: str = "boundary-1") -> None:
        tr.append(self.transcript, tr.boundary(at, uuid))

    def segment(self) -> SegmentView:
        return self.state().segment(self.world.store, KEY, now=self.clock.now)

    def rebuilt(self) -> SegmentView:
        return RecallState().segment(self.world.store, KEY, now=self.clock.now)

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.world.store.iter_events() if e["kind"] == kind]


@pytest.fixture(params=["held", "cli"])
def session(
    request: pytest.FixtureRequest, world: PromptWorld, clock: Clock, tmp_path: Path
) -> Iterator[Session]:
    yield Session(world, clock, tmp_path / "t.jsonl", held=request.param == "held")


def _memories(world: PromptWorld, clock: Clock) -> tuple[str, str]:
    """Two memories the two queries each reach, written before the
    session's first hook."""
    first = world.write(BODY).id
    second = world.write(OTHER_BODY).id
    clock.advance(seconds=1)
    return first, second


# ---------------------------------------------------------------------------
# A compact SessionStart
# ---------------------------------------------------------------------------


def test_a_compact_on_its_marker_opens_a_segment_named_by_it(
    session: Session, clock: Clock
) -> None:
    session.boundary(clock.now - timedelta(seconds=1))
    session.start("compact")
    [row] = session.rows("segment_opened")
    assert (row["source"], row["segment"], row["marker"]) == (
        "compact",
        1,
        "boundary-1",
    )
    assert "marker_absent" not in row and "opened_at" not in row
    view = session.segment()
    assert (view.segment, view.marker, view.started_at) == (1, "boundary-1", clock.now)


def test_a_compact_without_its_marker_is_a_resume_and_says_so(
    session: Session, world: PromptWorld, clock: Clock
) -> None:
    first, _ = _memories(world, clock)
    assert session.prompt().ids == (first,)
    clock.advance(seconds=5)
    session.start("compact")
    [row] = session.rows("segment_opened")
    assert (row["source"], row["segment"], row["marker_absent"]) == ("compact", 0, True)
    assert "marker" not in row
    view = session.segment()
    assert (view.segment, set(view.shown), view.deliveries) == (0, {first}, 1)
    assert session.prompt().ids == ()


def test_a_compact_without_a_transcript_path_is_a_resume(
    session: Session, clock: Clock
) -> None:
    """No transcript to read the marker from: the conservative reading is
    a resume, and a later hook with the transcript may still confirm."""
    session.boundary(clock.now)
    session.start("compact", transcript=False)
    [row] = session.rows("segment_opened")
    assert (row["segment"], row["marker_absent"]) == (0, True)
    clock.advance(seconds=10)
    session.stop()
    assert session.segment().segment == 1


# ---------------------------------------------------------------------------
# The marker written after the hook
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confirming", "triggered_from"),
    [("prompt", "prompt_hook"), ("stop", "stop_hook"), ("resume", "session_hook")],
)
def test_a_later_read_confirms_a_marker_written_after_the_hook(
    session: Session,
    world: PromptWorld,
    clock: Clock,
    confirming: str,
    triggered_from: str,
) -> None:
    """The boundary lands in the file after the compact SessionStart ran;
    the next hook to read the transcript, minutes later as after a manual
    compact, opens the segment at the signal and records it with the
    marker and the moment it opened."""
    first, _ = _memories(world, clock)
    assert session.prompt().ids == (first,)
    clock.advance(seconds=5)
    signal = clock.now
    session.start("compact")
    session.boundary(signal + timedelta(seconds=1))
    clock.advance(seconds=300)
    if confirming == "prompt":
        assert session.prompt().ids == (first,)
    elif confirming == "stop":
        session.stop()
    else:
        session.start("resume")
    absent, confirmed = session.rows("segment_opened")
    assert absent["marker_absent"] is True
    assert (confirmed["source"], confirmed["segment"], confirmed["marker"]) == (
        "compact",
        1,
        "boundary-1",
    )
    assert confirmed["opened_at"] == isoformat_utc(signal)
    assert confirmed["triggered_from"] == triggered_from
    assert confirmed["rule"] == "rs-v6"
    view = session.segment()
    assert (view.segment, view.marker, view.started_at) == (1, "boundary-1", signal)
    if confirming != "prompt":
        assert session.prompt().ids == (first,)
    assert [e["segment"] for e in session.rows("prompt_recall")] == [0, 1]


def test_what_was_shown_since_the_signal_stays_in_the_new_segment(
    session: Session, world: PromptWorld, clock: Clock
) -> None:
    """A recall delivered between the compact SessionStart and the read
    that finds its marker was delivered after the compaction: it belongs
    to the new segment, and what was shown before the signal does not."""
    first, second = _memories(world, clock)
    assert session.prompt().ids == (first,)
    clock.advance(seconds=5)
    signal = clock.now
    session.start("compact")
    clock.advance(seconds=10)
    delivered = session.prompt(OTHER_QUERY)
    assert delivered.ids == (second,)
    session.boundary(signal + timedelta(seconds=1))
    clock.advance(seconds=10)
    session.stop()
    view = session.segment()
    assert (view.segment, set(view.shown), view.deliveries) == (1, {second}, 1)
    assert view.shown_by == {second: (SURFACE_RECALL, delivered.event_id)}
    assert view.started_at == signal
    clock.advance(seconds=10)
    assert session.prompt(OTHER_QUERY).ids == ()
    assert session.prompt().ids == (first,)


def test_a_settlement_before_the_signal_stays_in_the_old_segment(
    session: Session, world: PromptWorld, clock: Clock
) -> None:
    """The Stop settled a pointer shown before the compaction; the segment
    the marker opens shows it again, and settles it again."""
    first, _ = _memories(world, clock)
    assert session.prompt().ids == (first,)
    session.stop(reply=BODY)
    clock.advance(seconds=5)
    signal = clock.now
    session.start("compact")
    session.boundary(signal + timedelta(seconds=1))
    clock.advance(seconds=10)
    session.stop()
    assert session.segment().settled == frozenset()
    assert session.prompt().ids == (first,)
    session.stop(reply=BODY)
    settled = [e for e in session.rows("use") if e.get("surface") == SURFACE_RECALL]
    assert [(e["ids"], e["segment"]) for e in settled] == [([first], 0), ([first], 1)]


def test_held_and_rebuilt_agree_at_every_step(
    world: PromptWorld, clock: Clock, tmp_path: Path
) -> None:
    """The daemon's held segment and the one rebuilt from the log are the
    same after each hook: shows, the signal, its confirmation, a resume,
    a clear."""
    session = Session(world, clock, tmp_path / "t.jsonl", held=True)
    first, _ = _memories(world, clock)

    def agree() -> None:
        assert session.segment() == session.rebuilt()

    steps = [
        lambda: session.prompt(),
        lambda: session.start("compact"),
        lambda: session.prompt(OTHER_QUERY),
        lambda: session.boundary(clock.now - timedelta(seconds=15)),
        lambda: session.stop(),
        lambda: session.prompt(),
        lambda: session.start("resume"),
        lambda: session.start("compact"),
        lambda: session.stop(),
        lambda: session.start("clear"),
        lambda: session.prompt(),
    ]
    for step in steps:
        step()
        agree()
        clock.advance(seconds=10)
    assert session.segment().segment == 2
    assert first in session.segment().shown


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_the_window_is_sixty_seconds() -> None:
    """Measured: the 45 boundaries a compact SessionStart pairs with are
    stamped within 2.5 s of it, no compact row has one between 2.5 s and
    120 s from it, and the rows written with one land up to 29.2 s after
    the hook; any window from 2.5 s to 120 s parts the 92 measured rows
    alike."""
    assert COMPACT_MARKER_WINDOW_SECONDS == 60


def test_a_read_inside_the_window_keeps_waiting(session: Session, clock: Clock) -> None:
    signal = clock.now
    session.start("compact")
    clock.advance(seconds=10)
    session.stop()
    assert session.segment().segment == 0
    session.boundary(signal + timedelta(seconds=2))
    clock.advance(seconds=20)
    session.stop()
    assert session.segment().segment == 1


def test_a_marker_read_late_still_opens_the_segment(
    session: Session, world: PromptWorld, clock: Clock
) -> None:
    """The window bounds the marker's stamp, not the read: a Stop well
    after the window that finds no marker keeps the signal, and the next
    read that finds one stamped within the window of the signal opens
    the segment at the signal, with what was shown since."""
    first, _ = _memories(world, clock)
    signal = clock.now
    session.start("compact")
    clock.advance(seconds=90)
    session.stop()
    assert session.segment().segment == 0
    assert session.prompt().ids == (first,)
    session.boundary(signal + timedelta(seconds=5))
    clock.advance(seconds=600)
    session.stop()
    view = session.segment()
    assert (view.segment, view.started_at, view.shown) == (
        1,
        signal,
        frozenset({first}),
    )
    [row] = [r for r in session.rows("segment_opened") if "opened_at" in r]
    assert row["opened_at"] == isoformat_utc(signal)


def test_a_read_that_records_nothing_moves_neither_state(
    session: Session, clock: Clock
) -> None:
    """A prompt nothing is delivered for (an empty store here) reads the
    transcript and writes no row, so it changes nothing the log would not
    show: after it the daemon's state and the one rebuilt from the log
    both still wait, and both open the segment when a later read finds
    the marker."""
    signal = clock.now
    session.start("compact")
    clock.advance(seconds=90)
    assert session.prompt().ids == ()
    assert session.rows("prompt_recall") == []
    session.boundary(signal + timedelta(seconds=5))
    clock.advance(seconds=10)
    session.prompt()
    view = session.segment()
    assert (view.segment, view.marker) == (1, "boundary-1")


@pytest.mark.parametrize("offset", [-61, 61])
def test_a_marker_stamped_outside_the_window_opens_nothing(
    session: Session, clock: Clock, offset: int
) -> None:
    signal = clock.now
    session.boundary(signal + timedelta(seconds=offset))
    session.start("compact")
    clock.advance(seconds=100)
    session.stop()
    assert session.segment().segment == 0


# ---------------------------------------------------------------------------
# Many signals, many markers
# ---------------------------------------------------------------------------


def test_repeated_compacts_on_one_marker_open_one_segment(
    session: Session, clock: Clock
) -> None:
    """The measured shape: compact SessionStarts arrive again and again
    after one compaction. The marker already opened on opens nothing."""
    session.boundary(clock.now)
    for seconds in (0, 20, 20, 3600):
        clock.advance(seconds=seconds)
        session.start("compact")
        session.stop()
    rows = session.rows("segment_opened")
    assert [(r["segment"], r.get("marker"), r.get("marker_absent")) for r in rows] == [
        (1, "boundary-1", None),
        (1, None, True),
        (1, None, True),
        (1, None, True),
    ]
    assert session.segment().segment == 1


def test_each_new_marker_opens_its_segment(session: Session, clock: Clock) -> None:
    session.boundary(clock.now, "boundary-1")
    session.start("compact")
    clock.advance(seconds=3600)
    session.boundary(clock.now, "boundary-2")
    session.start("compact")
    view = session.segment()
    assert (view.segment, view.marker) == (2, "boundary-2")


def test_a_clear_drops_a_pending_signal(session: Session, clock: Clock) -> None:
    signal = clock.now
    session.start("compact")
    clock.advance(seconds=5)
    session.start("clear")
    session.boundary(signal + timedelta(seconds=1))
    clock.advance(seconds=10)
    session.stop()
    view = session.segment()
    assert (view.segment, view.marker) == (1, None)


def test_with_telemetry_off_the_compaction_is_confirmed(
    world: PromptWorld, clock: Clock, tmp_path: Path
) -> None:
    """The confirmation's row is product state (ENG-108): with telemetry
    off the segment opens on the marker and its row is written, as with
    telemetry on, so the state rebuilt from the log agrees."""
    session = Session(world, clock, tmp_path / "t.jsonl", held=True)
    signal = clock.now
    session.start("compact")
    session.boundary(signal + timedelta(seconds=1))
    clock.advance(seconds=10)
    off = Config(
        storage=StorageConfig(directory=str(world.store_dir)),
        telemetry=TelemetryConfig(enabled=False),
    )
    state = session.state()
    opened = confirm_compaction(
        store=world.store,
        state=state,
        key=KEY,
        transcript_path=str(session.transcript),
        cfg=off,
        triggered_from="prompt_hook",
        now=clock.now,
        origin=world.caller,
    )
    assert opened is not None and opened.segment == 1
    assert session.segment().segment == 1
    rows = session.rows("segment_opened")
    assert len(rows) == 2
    assert rows[-1]["opened_at"] is not None
