"""Product state, with telemetry off and against a busy store (ENG-108).

The rows the product reads back (`events.STATE_KINDS`, a review's answer
and the audit rows, `events.is_state_row`) are recorded whatever
`[telemetry] enabled` says: with telemetry off the session brief and the
prompt recall show, a memory shown is not shown again as new, a
contradicted memory stays held back and a review chain is found. A state
row that meets another writer's lock is tried again and, when the lock
stays, raised: a tool answers ``store_busy``, a hook fails, and a show it
claimed goes with its session's held state. A dry run records nothing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import events, review, store_events
from bettermemory.builder import build_server
from bettermemory.config import Config, TelemetryConfig
from bettermemory.events import Recorder
from bettermemory.models import utcnow
from bettermemory.recall import HookResult, RecallState, session_brief
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool
from ._review_rows import (
    BACKUP_NEW,
    BACKUP_OLD,
    Harness,
    fix_origin_and_identity,
    set_tau,
)
from .test_recall_prompt import BODY, QUERY, PromptWorld, config

SESSION = "transcript-recall"


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


def _off(world: PromptWorld) -> Config:
    return config(world.store_dir, telemetry=False)


def _rows(store: Store, kind: str) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e["kind"] == kind]


def _brief(
    world: PromptWorld,
    cfg: Config,
    *,
    state: RecallState | None = None,
    source: str = "startup",
) -> HookResult:
    """A SessionStart of `SESSION`; a fresh state unless one is given,
    which is what the in-process path and a restarted daemon read."""
    return session_brief(
        session_id=SESSION,
        source=source,
        origin=world.caller,
        config=cfg,
        store=world.store,
        state=state if state is not None else RecallState(),
    )


@contextlib.contextmanager
def _locked(store: Store) -> Iterator[sqlite3.Connection]:
    """Another connection holds `store`'s write lock for the block; the
    test may roll it back early."""
    holder = sqlite3.connect(store.path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield holder
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()


# ---------------------------------------------------------------------------
# The rule the writers and a pruning read
# ---------------------------------------------------------------------------


def test_the_state_kinds_name_every_row_a_state_reader_reads() -> None:
    """A pruning (ENG-82) keeps what `is_state_row` answers true for; the
    list names each kind a reader of state reads back."""
    from bettermemory import recall_state

    assert set(recall_state._STATE_KINDS) <= events.STATE_KINDS
    # `Store.contradicted_at`, `review.find_chain`, the seen gate and the
    # disabled scopes.
    assert {"use", "review_issued", "show", "scope_disable", "scope_enable"} <= (
        events.STATE_KINDS
    )
    assert events.is_state_row("write", {"review_id": "01M5Z0000000000000000000AA"})
    assert events.is_state_row("standing_guard", {})
    assert not events.is_state_row("write", {"status": "committed", "forced": False})
    assert not events.is_state_row("search", {"query": "q"})


# ---------------------------------------------------------------------------
# Telemetry off: the brief, the recall, the contradiction and the review
# ---------------------------------------------------------------------------


def test_telemetry_off_the_brief_shows_and_the_recall_does_not_show_it_again(
    world: PromptWorld,
) -> None:
    memory = world.write()
    off = _off(world)
    assert _brief(world, off).ids == (memory.id,)
    (row,) = _rows(world.store, "session_brief")
    assert row["ids"] == [memory.id]
    # No prompt text with telemetry off: the hash and the length only.
    assert set(row["probe_query"]) == {"hash", "len"}
    # Read back from the log (the in-process path, a restarted daemon):
    # the brief's pointer is seen in the segment.
    assert world.recall(config=off, state=RecallState()).stdout == ""
    assert _rows(world.store, "prompt_recall") == []


def test_telemetry_off_a_resume_omits_what_the_segment_showed(
    world: PromptWorld,
) -> None:
    memory = world.write()
    off = _off(world)
    assert _brief(world, off).ids == (memory.id,)
    again = _brief(world, off, source="resume")
    assert again.ids == ()
    assert _rows(world.store, "session_brief")[-1]["omitted"] == [memory.id]


def test_telemetry_off_the_recall_delivers_once_in_a_segment(
    world: PromptWorld,
) -> None:
    memory = world.write()
    off = _off(world)
    assert world.recall(config=off, state=RecallState()).ids == (memory.id,)
    assert world.recall(config=off, state=RecallState()).stdout == ""


def test_telemetry_off_a_memory_opened_is_not_recalled(world: PromptWorld) -> None:
    opened = world.write()
    other = world.write(f"{BODY} again")
    Recorder(store=world.store, session_id="sess_server", enabled=False).record(
        "show", id=opened.id, harness_session=SESSION
    )
    delivered = world.recall(config=_off(world), state=RecallState())
    assert delivered.ids == (other.id,)
    (row,) = _rows(world.store, "prompt_recall")
    assert {"id": opened.id, "reason": "seen_in_segment"} in row["suppressed_ids"]


async def test_telemetry_off_memory_show_records_its_row(world: PromptWorld) -> None:
    memory = world.write()
    server = build_server(config=_off(world), store=world.store, state=SessionState())
    shown = await call_tool(server, "memory_show", {"id": memory.id})
    assert shown["id"] == memory.id
    assert [row["id"] for row in _rows(world.store, "show")] == [memory.id]


async def test_telemetry_off_a_contradicted_memory_stays_held_back(
    world: PromptWorld,
) -> None:
    contradicted = world.write()
    other = world.write(f"{BODY} again")
    off = _off(world)
    server = build_server(config=off, store=world.store, state=SessionState())
    await call_tool(
        server,
        "memory_record_use",
        {"memory_ids": [contradicted.id], "outcome": "contradicted"},
    )
    assert contradicted.id in world.store.contradicted_at([contradicted.id])
    delivered = world.recall(config=off, state=RecallState())
    assert delivered.ids == (other.id,)
    (row,) = _rows(world.store, "prompt_recall")
    assert {"id": contradicted.id, "reason": "contradicted"} in row["suppressed_ids"]
    assert contradicted.id not in _brief(world, off).ids


def test_telemetry_off_a_scope_toggle_is_recorded(world: PromptWorld) -> None:
    recorder = Recorder(store=world.store, session_id="sess_server", enabled=False)
    recorder.record("scope_disable", scope="projects:foo")
    recorder.record("scope_enable", scope="projects:foo")
    assert [e["kind"] for e in world.store.iter_events()] == [
        "scope_disable",
        "scope_enable",
    ]


@pytest.fixture
def quiet(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    """The review harness (`tests/_review_rows.py`) with the server rebuilt
    under `[telemetry] enabled = false`."""
    fix_origin_and_identity(monkeypatch, tmp_path_factory.mktemp("checkout"))
    set_tau(monkeypatch)
    h = Harness(tmp_path / "store", monkeypatch)
    h.config = dataclasses.replace(h.config, telemetry=TelemetryConfig(enabled=False))
    h.server = build_server(config=h.config, store=h.store, state=h.state)
    return h


async def test_telemetry_off_a_review_chain_is_found_and_answered(
    quiet: Harness,
) -> None:
    h = quiet
    h.seed(BACKUP_OLD)
    first = await h.write(BACKUP_NEW)
    assert first["status"] == "review", first
    review_id = first["review"]["review_id"]
    chain = review.find_chain(h.store, review_id)
    assert chain.found and chain.open
    done = await h.answer(first, BACKUP_NEW)
    assert done["status"] == "committed", done
    assert review.find_chain(h.store, review_id).decided
    (answer,) = h.events("write")
    assert answer["review_id"] == review_id


# ---------------------------------------------------------------------------
# A busy store: tried again, then raised
# ---------------------------------------------------------------------------


def test_a_state_row_that_meets_a_lock_lands_on_its_retry(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry on, as the issue found it: the first try meets the lock
    and the row used to be dropped with one log line."""
    store = world.store
    store.set_busy_timeout(0.05)
    pauses: list[float] = []
    with _locked(store) as holder:

        def pause(seconds: float) -> None:
            pauses.append(seconds)
            holder.execute("ROLLBACK")

        monkeypatch.setattr(store_events, "time", SimpleNamespace(sleep=pause))
        Recorder(store=store, session_id="sess_server").record("show", id="m-1")
    assert pauses == [store_events.EVENT_RETRY_PAUSE_SECONDS]
    assert [row["id"] for row in _rows(store, "show")] == ["m-1"]


def test_a_state_row_whose_lock_stays_is_raised(
    world: PromptWorld,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = world.store
    store.set_busy_timeout(0.05)
    pauses: list[float] = []
    monkeypatch.setattr(store_events, "time", SimpleNamespace(sleep=pauses.append))
    recorder = Recorder(store=store, session_id="sess_server")
    with _locked(store):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            recorder.record("show", id="m-1")
        # A measurement row under the same lock is tried once and dropped,
        # and the call it describes goes on.
        recorder.record("search", query="q", returned=[])
    assert len(pauses) == events.STATE_WRITE_ATTEMPTS - 1
    assert _rows(store, "show") == [] and _rows(store, "search") == []
    assert "state row show not recorded" in caplog.text


async def test_memory_show_whose_row_meets_a_held_lock_answers_store_busy(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer takes the lock just as the `show` row is written,
    and keeps it through both tries: the call answers ``store_busy``
    (retryable, nothing was kept) instead of a body whose show the log
    never holds."""
    memory = world.write()
    server = build_server(config=world.config, store=world.store, state=SessionState())
    store = world.store
    store.set_busy_timeout(0.05)
    monkeypatch.setattr(store_events, "time", SimpleNamespace(sleep=lambda _s: None))
    real = store.record_event

    def record_event(kind: str, **fields: Any) -> Any:
        if kind != "show":
            return real(kind, **fields)
        with _locked(store):
            return real(kind, **fields)

    with monkeypatch.context() as patched:
        patched.setattr(store, "record_event", record_event)
        with pytest.raises(ToolError, match=r"store_busy \(retryable\)"):
            await call_tool(server, "memory_show", {"id": memory.id})
    assert _rows(store, "show") == []
    shown = await call_tool(server, "memory_show", {"id": memory.id})
    assert shown["id"] == memory.id
    assert len(_rows(store, "show")) == 1


def _failing(store: Store, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """`store`'s `kind` rows fail as a held lock fails them."""
    real = store.record_event

    def record_event(event_kind: str, **fields: Any) -> Any:
        if event_kind == kind:
            raise sqlite3.OperationalError("database is locked")
        return real(event_kind, **fields)

    monkeypatch.setattr(store, "record_event", record_event)


def test_a_delivery_whose_row_cannot_land_is_neither_shown_nor_held(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = world.write()
    state = RecallState()
    with monkeypatch.context() as patched:
        _failing(world.store, patched, "prompt_recall")
        with pytest.raises(sqlite3.OperationalError):
            world.recall(state=state)
    # The claim went with the held state, so the next prompt delivers it.
    assert world.recall(state=state).ids == (memory.id,)
    assert len(_rows(world.store, "prompt_recall")) == 1


def test_a_brief_whose_row_cannot_land_is_neither_shown_nor_held(
    world: PromptWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = world.write()
    state = RecallState()
    with monkeypatch.context() as patched:
        _failing(world.store, patched, "session_brief")
        with pytest.raises(sqlite3.OperationalError):
            _brief(world, world.config, state=state)
    assert world.recall(state=state).ids == (memory.id,)


# ---------------------------------------------------------------------------
# A dry run records nothing
# ---------------------------------------------------------------------------


def test_a_dry_run_records_no_row_and_moves_no_held_state(world: PromptWorld) -> None:
    """Telemetry off no longer suppresses the state rows, so the dry run
    has its own switch (`events.suppressed`) and a throwaway state."""
    from bettermemory import hook as hook_module

    memory = world.write()
    state = RecallState()
    assert _brief(world, world.config, state=state).ids == (memory.id,)
    before = list(world.store.iter_events())
    hook_module.run_audit(
        user_message=QUERY,
        assistant_response=f"Done. {BODY}.",
        session_id=SESSION,
        config=_off(world),
        store=world.store,
        state=state,
        dry_run=True,
    )
    assert list(world.store.iter_events()) == before
    view = state.segment(world.store, ("claude-code", SESSION), now=utcnow())
    assert view.settled == frozenset()
