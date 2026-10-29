"""The recall surface's session cooldown (`recall.RecallState`).

Per (harness, session id): the segment, the ids shown in it and its
delivery count. A SessionStart with source ``clear`` opens a new segment,
and one with source ``compact`` on its compaction marker; no id is shown
twice in one, and no count of deliveries caps one (rs-v6, RS-F7 part 0);
the state is rebuilt from the
session's rows in the log after a restart (and computed from them on the
in-process path), evicted after a day idle and rebuilt as it was,
bounded at a thousand sessions, and safe to drive from many threads.
The compaction marker's own battery is `test_recall_compaction.py`.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.recall import (
    STATE_MAX_SESSIONS,
    SURFACE_BRIEF,
    SURFACE_RECALL,
    RecallState,
    rebuild_session,
)
from bettermemory.store import Store
from bettermemory.transcript import CompactMarker

from . import _transcript_rows as tr

from .test_recall_prompt import BODY, PromptWorld

KEY = ("claude-code", "transcript-recall")


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compact(state: RecallState, store: Store, key: str = "boundary-1") -> None:
    """A compact SessionStart whose transcript holds a marker `key` the
    session has not opened a segment on: it opens one (RS-F5)."""
    now = _now()
    _, opened = state.open_segment(
        store, KEY, source="compact", now=now, marker=CompactMarker(key=key, at=now)
    )
    assert opened


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def test_a_compact_on_its_marker_and_a_clear_open_a_segment(
    world: PromptWorld,
) -> None:
    """RS-F5, changed deliberately: a compact opens a segment on a marker
    the segments have not opened on, and keeps the segment without one,
    as a resume does; a clear always opens one."""
    state = RecallState()
    now = _now()
    marker = CompactMarker(key="boundary-1", at=now)
    startup, opened = state.open_segment(world.store, KEY, source="startup", now=now)
    assert (startup.segment, opened) == (0, False)
    state.note_shown(KEY, ["a"], delivery=True, now=now)
    resumed, opened = state.open_segment(world.store, KEY, source="resume", now=now)
    assert (resumed.segment, set(resumed.shown), resumed.deliveries, opened) == (
        0,
        {"a"},
        1,
        False,
    )
    unmarked, opened = state.open_segment(world.store, KEY, source="compact", now=now)
    assert (unmarked.segment, set(unmarked.shown), unmarked.deliveries, opened) == (
        0,
        {"a"},
        1,
        False,
    )
    compacted, opened = state.open_segment(
        world.store, KEY, source="compact", now=now, marker=marker
    )
    assert (compacted.segment, set(compacted.shown), compacted.deliveries) == (
        1,
        set(),
        0,
    )
    assert (opened, compacted.marker) == (True, "boundary-1")
    again, opened = state.open_segment(
        world.store, KEY, source="compact", now=now, marker=marker
    )
    assert (again.segment, opened) == (1, False)
    cleared, opened = state.open_segment(world.store, KEY, source="clear", now=now)
    assert (cleared.segment, cleared.marker, opened) == (2, "boundary-1", True)


def test_no_id_twice_in_a_segment_and_again_after_a_compact(
    world: PromptWorld,
) -> None:
    memory = world.write()
    state = RecallState()
    assert world.recall(state=state).ids == (memory.id,)
    assert world.recall(state=state).stdout == ""
    _compact(state, world.store)
    assert world.recall(state=state).ids == (memory.id,)
    segments = [e["segment"] for e in world.recalls()]
    assert segments == [0, 1]


def test_an_update_holds_the_pointer_back_in_its_segment_only(
    world: PromptWorld,
) -> None:
    """An update the session made before a compact is the old segment's:
    the new segment may point at the memory again."""
    memory = world.write()
    state = RecallState()
    state.open_segment(world.store, KEY, source="startup", now=_now())
    world.store.record_event("update", session="transcript-recall", id=memory.id)
    assert world.recall(state=state).stdout == ""
    _compact(state, world.store)
    assert world.recall(state=state).ids == (memory.id,)


def test_a_brief_pointer_is_not_recalled_in_its_segment(world: PromptWorld) -> None:
    memory = world.write()
    state = RecallState()
    state.open_segment(world.store, KEY, source="startup", now=_now())
    state.note_shown(KEY, [memory.id], delivery=False, now=_now())
    assert world.recall(state=state).stdout == ""


def test_no_count_of_deliveries_holds_a_segment_back(world: PromptWorld) -> None:
    """rs-v6 (RS-F7 part 0): a segment that has had any number of
    deliveries still gets an unseen pointer, and no hold is named for the
    count; until rs-v6 twelve held every later one (``segment_cap``)."""
    first = world.write()
    state = RecallState()
    for i in range(40):
        state.note_shown(KEY, [f"other-{i}"], delivery=True, now=_now())
    assert world.recall(state=state).ids == (first.id,)
    second = world.write(f"{BODY} again")
    assert world.recall(state=state).ids == (second.id,)
    reasons = {
        e["reason"] for row in world.recalls() for e in row.get("suppressed_ids", [])
    }
    # The second recall holds the first record as seen, and nothing more.
    assert reasons == {"seen_in_segment"}


# ---------------------------------------------------------------------------
# Rebuilt from the log
# ---------------------------------------------------------------------------


def test_a_restart_rebuilds_the_segment_from_the_log(world: PromptWorld) -> None:
    memory = world.write()
    before = RecallState()
    assert world.recall(state=before).ids == (memory.id,)

    after = RecallState()  # a restarted daemon
    view = after.segment(world.store, KEY, now=_now())
    assert (view.segment, set(view.shown), view.deliveries) == (0, {memory.id}, 1)
    assert world.recall(state=after).stdout == ""


def test_a_rebuild_keeps_only_the_newest_segment(world: PromptWorld) -> None:
    memory = world.write()
    assert world.recall().ids == (memory.id,)
    # A compact, recorded through its brief (the brief here is empty, so
    # the segment rides on the next recall's event instead).
    state = RecallState()
    _compact(state, world.store)
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


def test_a_rebuild_reads_the_whole_session_past_the_idle_window(
    world: PromptWorld,
) -> None:
    """RS-F5, changed deliberately: the rebuild read the last day only, so
    a session evicted after a day idle came back with nothing shown and
    re-showed what its segment had. It reads the session's rows by
    session, whenever they were stamped."""
    world.store.record_event(
        "prompt_recall",
        session=KEY[1],
        harness=KEY[0],
        shown_ids=["old"],
        segment=0,
    )
    assert rebuild_session(world.store, KEY, now=_now()).shown == {"old"}
    a_day_and_more_later = _now() + timedelta(hours=30)
    assert rebuild_session(world.store, KEY, now=a_day_and_more_later).shown == {"old"}


def test_a_brief_row_without_ids_adds_nothing_and_keeps_its_segment(
    world: PromptWorld,
) -> None:
    """A resume whose brief omitted every candidate its segment had shown
    leaves a `session_brief` row with no ids: the rebuild reads the
    segment it carries and adds nothing to the shown set."""

    def brief(source: str, segment: int, ids: list[str], omitted: list[str]) -> None:
        world.store.record_event(
            "session_brief",
            session=KEY[1],
            harness=KEY[0],
            source=source,
            segment=segment,
            ids=ids,
            chars=300 if ids else 0,
            omitted=omitted,
            triggered_from="session_hook",
        )

    brief("startup", 0, ["a"], [])
    brief("resume", 0, [], ["a"])
    rebuilt = rebuild_session(world.store, KEY, now=_now())
    assert (rebuilt.segment, rebuilt.shown, rebuilt.deliveries) == (0, {"a"}, 0)
    brief("compact", 1, ["b"], [])
    brief("resume", 1, [], ["b"])
    rebuilt = rebuild_session(world.store, KEY, now=_now())
    assert (rebuilt.segment, rebuilt.shown, rebuilt.deliveries) == (1, {"b"}, 0)


# ---------------------------------------------------------------------------
# Eviction, the bound, threads
# ---------------------------------------------------------------------------


def test_a_session_idle_a_day_is_evicted(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    state = RecallState()
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
    state = RecallState()
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
    state = RecallState()
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
                    with store.reopen() as reopened:
                        state.segment(reopened, KEY, now=now)
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


# ---------------------------------------------------------------------------
# A compact with an empty brief still opens a segment the log remembers
# ---------------------------------------------------------------------------


def test_an_empty_compact_brief_opens_a_segment_on_the_cli_path(
    world: PromptWorld, tmp_path: Path
) -> None:
    """The CLI path holds no state between calls: the compact must be
    read back from the log even when its brief showed nothing, or the ids
    shown before it stay held and the cap carries over. The compact's
    transcript holds its marker (RS-F5), which the row names."""
    from bettermemory.config import BehaviorConfig, Config, StorageConfig
    from bettermemory.recall import session_brief

    memory = world.write()
    assert world.recall().ids == (memory.id,)
    assert world.recall().stdout == ""
    no_brief = Config(
        storage=StorageConfig(directory=str(world.store_dir)),
        behavior=BehaviorConfig(session_brief=False),
    )
    transcript = tr.write(tmp_path / "t.jsonl", tr.boundary(_now()))
    shown = session_brief(
        session_id=KEY[1],
        source="compact",
        transcript_path=str(transcript),
        origin=world.caller,
        config=no_brief,
        store=world.store,
    )
    assert shown.stdout == ""
    [opened] = [e for e in world.store.iter_events() if e["kind"] == "segment_opened"]
    assert (opened["segment"], opened["source"], opened["session_id"]) == (
        1,
        "compact",
        KEY[1],
    )
    assert (opened["marker"], "marker_absent" in opened) == ("boundary-1", False)
    assert opened["triggered_from"] == "session_hook"
    assert world.recall().ids == (memory.id,)
    assert [e["segment"] for e in world.recalls()] == [0, 1]


def test_a_restarted_daemon_reads_the_empty_compact(world: PromptWorld) -> None:
    from bettermemory.config import BehaviorConfig, Config, StorageConfig
    from bettermemory.recall import session_brief

    memory = world.write()
    before = RecallState()
    assert world.recall(state=before).ids == (memory.id,)
    session_brief(
        session_id=KEY[1],
        source="clear",
        origin=world.caller,
        config=Config(
            storage=StorageConfig(directory=str(world.store_dir)),
            behavior=BehaviorConfig(session_brief=False),
        ),
        store=world.store,
        state=before,
    )
    after = RecallState()
    view = after.segment(world.store, KEY, now=_now())
    assert (view.segment, set(view.shown), view.deliveries) == (1, set(), 0)


def test_a_segment_row_with_telemetry_off_and_none_on_startup(
    world: PromptWorld,
) -> None:
    """The compact's row is product state (ENG-108), recorded with
    telemetry off; a startup opens no segment and records none."""
    from bettermemory.config import Config, StorageConfig, TelemetryConfig
    from bettermemory.recall import session_brief

    off = Config(
        storage=StorageConfig(directory=str(world.store_dir)),
        telemetry=TelemetryConfig(enabled=False),
    )
    session_brief(session_id=KEY[1], source="compact", config=off, store=world.store)
    session_brief(
        session_id=KEY[1],
        source="startup",
        origin=world.caller,
        config=world.config,
        store=world.store,
    )
    rows = [e for e in world.store.iter_events() if e["kind"] == "segment_opened"]
    assert [(row["source"], row.get("marker_absent")) for row in rows] == [
        ("compact", True)
    ]


# ---------------------------------------------------------------------------
# The delivery is decided under one lock
# ---------------------------------------------------------------------------


def test_a_claim_keeps_unshown_ids_and_counts_without_a_cap(
    world: PromptWorld,
) -> None:
    state = RecallState()
    now = _now()
    assert state.claim_delivery(KEY, ["a", "b"], now=now) == (["a", "b"], 0)
    assert state.claim_delivery(KEY, ["a", "c"], now=now) == (["c"], 0)
    assert state.claim_delivery(KEY, ["a"], now=now) == ([], 0)
    for i in range(30):
        state.claim_delivery(KEY, [f"x{i}"], now=now)
    assert state.segment(world.store, KEY, now=now).deliveries == 32
    assert state.claim_delivery(KEY, ["fresh"], now=now) == (["fresh"], 0)


def test_a_brief_claim_keeps_each_lists_unshown_ids_and_counts_no_delivery(
    world: PromptWorld,
) -> None:
    """`claim_brief` mirrors `claim_delivery`: the ids not yet shown in the
    segment, per list and in order, noted as the brief's show; a brief is
    no delivery, and what a prompt recall showed first stays its show."""
    state = RecallState()
    now = _now()
    assert state.claim_delivery(KEY, ["a"], now=now, event_id="recall-1") == (["a"], 0)
    assert state.claim_brief(
        KEY, ["s1", "a"], ["w1", "w2"], now=now, event_id="b1"
    ) == (["s1"], ["w1", "w2"], 0)
    assert state.claim_brief(KEY, ["s1", "s2"], ["w2"], now=now, event_id="b2") == (
        ["s2"],
        [],
        0,
    )
    view = state.segment(world.store, KEY, now=now)
    assert view.shown == {"a", "s1", "s2", "w1", "w2"}
    assert view.deliveries == 1
    assert view.shown_by["a"] == (SURFACE_RECALL, "recall-1")
    assert view.shown_by["s1"] == (SURFACE_BRIEF, "b1")
    assert view.shown_by["s2"] == (SURFACE_BRIEF, "b2")
    assert state.claim_delivery(KEY, ["s1", "fresh"], now=now) == (["fresh"], 0)


def test_concurrent_prompts_deliver_an_id_once(world: PromptWorld) -> None:
    """Two prompts of one session at once: both may read the segment
    before either delivers, and the claim still lets one through."""
    memory = world.write()
    state = RecallState()
    barrier = threading.Barrier(6)
    results: list[tuple[str, ...]] = []
    original = state.claim_delivery

    def gated(*args: object, **kwargs: object) -> tuple[list[str], int]:
        barrier.wait(10)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    state.claim_delivery = gated  # type: ignore[method-assign]

    def run() -> None:
        with Store.open(world.store_dir) as store:
            results.append(world.recall(state=state, store=store).ids)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(results).count((memory.id,)) == 1
    assert len(world.recalls()) == 1
