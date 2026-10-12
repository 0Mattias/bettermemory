"""The recall surface's session cooldown (`recall.RecallState`).

Per (harness, session id): the segment, the ids shown in it and its
delivery count. A SessionStart with source ``compact`` or ``clear`` opens
a new segment; no id is shown twice in one; twelve deliveries at most;
the state is rebuilt from the log after a restart (and computed from it
on the in-process path), evicted after a day idle, bounded at a thousand
sessions, and safe to drive from many threads.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.recall import (
    SEGMENT_DELIVERY_CAP,
    STATE_MAX_SESSIONS,
    RecallState,
    rebuild_session,
)
from bettermemory.store import Store

from .test_recall_prompt import BODY, PromptWorld

KEY = ("claude-code", "transcript-recall")


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def test_compact_and_clear_open_a_segment(world: PromptWorld) -> None:
    state = RecallState(background=False)
    now = _now()
    assert state.open_segment(world.store, KEY, source="startup", now=now).segment == 0
    state.note_shown(KEY, ["a"], delivery=True, now=now)
    resumed = state.open_segment(world.store, KEY, source="resume", now=now)
    assert (resumed.segment, set(resumed.shown), resumed.deliveries) == (0, {"a"}, 1)
    compacted = state.open_segment(world.store, KEY, source="compact", now=now)
    assert (compacted.segment, set(compacted.shown), compacted.deliveries) == (
        1,
        set(),
        0,
    )
    assert state.open_segment(world.store, KEY, source="clear", now=now).segment == 2


def test_no_id_twice_in_a_segment_and_again_after_a_compact(
    world: PromptWorld,
) -> None:
    memory = world.write()
    state = RecallState(background=False)
    assert world.recall(state=state).ids == (memory.id,)
    assert world.recall(state=state).stdout == ""
    state.open_segment(world.store, KEY, source="compact", now=_now())
    assert world.recall(state=state).ids == (memory.id,)
    segments = [e["segment"] for e in world.recalls()]
    assert segments == [0, 1]


def test_a_brief_pointer_is_not_recalled_in_its_segment(world: PromptWorld) -> None:
    memory = world.write()
    state = RecallState(background=False)
    state.open_segment(world.store, KEY, source="startup", now=_now())
    state.note_shown(KEY, [memory.id], delivery=False, now=_now())
    assert world.recall(state=state).stdout == ""


def test_twelve_deliveries_per_segment(world: PromptWorld) -> None:
    memory = world.write()
    state = RecallState(background=False)
    for i in range(SEGMENT_DELIVERY_CAP):
        state.note_shown(KEY, [f"other-{i}"], delivery=True, now=_now())
    assert world.recall(state=state).stdout == ""
    assert world.recalls() == []
    state.open_segment(world.store, KEY, source="clear", now=_now())
    assert world.recall(state=state).ids == (memory.id,)


def test_the_cap_reason_is_recorded_beside_a_delivery(world: PromptWorld) -> None:
    """At the cap every candidate is held; a delivery that already sits at
    eleven goes out once more and the next is held."""
    first = world.write()
    state = RecallState(background=False)
    for i in range(SEGMENT_DELIVERY_CAP - 1):
        state.note_shown(KEY, [f"other-{i}"], delivery=True, now=_now())
    assert world.recall(state=state).ids == (first.id,)
    world.write(f"{BODY} again")
    assert world.recall(state=state).stdout == ""


# ---------------------------------------------------------------------------
# Rebuilt from the log
# ---------------------------------------------------------------------------


def test_a_restart_rebuilds_the_segment_from_the_log(world: PromptWorld) -> None:
    memory = world.write()
    before = RecallState(background=False)
    assert world.recall(state=before).ids == (memory.id,)

    after = RecallState(background=False)  # a restarted daemon
    view = after.segment(world.store, KEY, now=_now())
    assert (view.segment, set(view.shown), view.deliveries) == (0, {memory.id}, 1)
    assert world.recall(state=after).stdout == ""


def test_a_rebuild_keeps_only_the_newest_segment(world: PromptWorld) -> None:
    memory = world.write()
    assert world.recall().ids == (memory.id,)
    # A compact, recorded through its brief (the brief here is empty, so
    # the segment rides on the next recall's event instead).
    state = RecallState(background=False)
    state.open_segment(world.store, KEY, source="compact", now=_now())
    assert world.recall(state=state).ids == (memory.id,)
    rebuilt = rebuild_session(world.store, KEY, now=_now())
    assert (rebuilt.segment, rebuilt.shown, rebuilt.deliveries) == (1, {memory.id}, 1)
    # A brief event names its segment and source; the walk adopts it.
    world.store.record_event(
        "session_brief",
        session=KEY[1],
        harness=KEY[0],
        source="clear",
        segment=2,
        ids=["brief-id"],
        triggered_from="session_hook",
    )
    rebuilt = rebuild_session(world.store, KEY, now=_now())
    assert (rebuilt.segment, rebuilt.shown, rebuilt.deliveries) == (2, {"brief-id"}, 0)


def test_a_legacy_recall_event_counts_its_one_hit(world: PromptWorld) -> None:
    world.store.record_event(
        "prompt_recall",
        session=KEY[1],
        triggered_from="prompt_hook",
        top_hits=[{"id": "legacy-hit"}, {"id": "not-delivered"}],
        delivered_reason="miss",
    )
    rebuilt = rebuild_session(world.store, KEY, now=_now())
    assert rebuilt.shown == {"legacy-hit"} and rebuilt.deliveries == 1


def test_another_harness_or_session_is_not_rebuilt_in(world: PromptWorld) -> None:
    world.store.record_event(
        "prompt_recall", session=KEY[1], harness="codex", shown_ids=["x"], segment=0
    )
    world.store.record_event(
        "prompt_recall", session="other", harness=KEY[0], shown_ids=["y"], segment=0
    )
    rebuilt = rebuild_session(world.store, KEY, now=_now())
    assert rebuilt.shown == set() and rebuilt.deliveries == 0


def test_a_rebuild_reads_only_the_idle_window(world: PromptWorld) -> None:
    world.store.record_event(
        "prompt_recall",
        session=KEY[1],
        harness=KEY[0],
        shown_ids=["old"],
        segment=0,
    )
    assert rebuild_session(world.store, KEY, now=_now()).shown == {"old"}
    a_day_and_more_later = _now() + timedelta(hours=30)
    assert rebuild_session(world.store, KEY, now=a_day_and_more_later).shown == set()


# ---------------------------------------------------------------------------
# Eviction, the bound, threads
# ---------------------------------------------------------------------------


def test_a_session_idle_a_day_is_evicted(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    state = RecallState(background=False)
    start = _now()
    state.note_shown(KEY, ["a"], delivery=True, now=start)
    state.note_shown(("claude-code", "live"), [], delivery=False, now=start)
    later = start + timedelta(hours=23)
    state.segment(store, ("claude-code", "live"), now=later)
    assert len(state) == 2
    much_later = start + timedelta(hours=24, minutes=1)
    state.segment(store, ("claude-code", "third"), now=much_later)
    assert len(state) == 2  # KEY was idle past the day; "live" was seen at 23 h
    # Rebuilt from an empty log: nothing remembered.
    assert state.segment(store, KEY, now=much_later).shown == frozenset()


def test_the_table_holds_at_most_a_thousand_sessions(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    state = RecallState(background=False)
    now = _now()
    for i in range(STATE_MAX_SESSIONS + 5):
        state.note_shown(("claude-code", f"s{i}"), [f"id{i}"], delivery=True, now=now)
    assert len(state) == STATE_MAX_SESSIONS
    # The least recently seen went first; the newest is held.
    assert state.segment(store, ("claude-code", "s0"), now=now).shown == frozenset()
    newest = f"s{STATE_MAX_SESSIONS + 4}"
    assert state.segment(store, ("claude-code", newest), now=now).deliveries == 1


def test_the_state_is_thread_safe(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    state = RecallState(background=False)
    now = _now()
    workers, rounds = 8, 200
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def drive(worker: int) -> None:
        try:
            barrier.wait()
            for i in range(rounds):
                state.note_shown(KEY, [f"{worker}-{i}"], delivery=True, now=now)
                state.note_shown(
                    ("claude-code", f"w{worker}-{i % 7}"), [], delivery=False, now=now
                )
                if i % 50 == 0:
                    state.segment(store.reopen(), KEY, now=now)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=drive, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    view = state.segment(store, KEY, now=now)
    assert view.deliveries == workers * rounds
    assert len(view.shown) == workers * rounds
