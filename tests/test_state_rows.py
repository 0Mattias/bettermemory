"""Product state, with telemetry off and against a busy store (ENG-108).

The rows the product reads back (`events.STATE_FIELDS`, the audit rows
among them) are recorded whatever `[telemetry] enabled` says: with
telemetry off the session brief and the prompt recall show, a memory
shown or searched is not shown again as new, the Stop settles what the
turn searched, a contradicted memory stays held back and a review chain
is found. With telemetry off each row keeps only the fields its readers
read, so no prompt text, body snippet, claim excerpt, quoted word or
transcript path lands. A state row that meets another writer's lock is
tried again and, when the lock stays, raised: a tool answers
``store_busy``, a hook fails, and a show it claimed goes with its
session's held state. A dry run records nothing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import events, review, store_events
from bettermemory import hook as hook_module
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
    """A pruning (ENG-82) keeps every state row; the table names each kind
    a reader of state reads back (tests/test_row_classes.py scans the
    readers for the rest)."""
    from bettermemory import recall_state

    assert set(recall_state._STATE_KINDS) <= events.STATE_KINDS
    # `Store.contradicted_at`, `review.find_chain`, the seen gate, the
    # Stop's settlement and the disabled scopes.
    assert {
        "use",
        "review_issued",
        "show",
        "search",
        "write",
        "update",
        "scope_disable",
        "scope_enable",
    } <= events.STATE_KINDS


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
    # No prompt text with telemetry off: the brief's probe is measurement.
    assert "probe_query" not in row
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
    # What the resume left out is measurement, not kept with telemetry off.
    assert "omitted" not in _rows(world.store, "session_brief")[-1]


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
    assert row["shown_ids"] == [other.id]
    # Why the opened one was held back is measurement.
    assert "suppressed_ids" not in row


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
    assert row["shown_ids"] == [other.id]
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
# Telemetry off: a state row keeps only what its readers read
# ---------------------------------------------------------------------------

# What `Store.iter_events` adds to a row's payload fields.
_ROW_META = {"ts", "session", "kind", "actor"}

# Free text measurement rows carry, each under the name its writers use:
# what no row may keep with telemetry off beside its readers' fields.
_NOISE: dict[str, Any] = {
    "query": "my private question",
    "probe_query": "my private prompt text",
    "top_hits": [{"id": "m-1", "snippet": "a private body sentence", "score": 0.9}],
    "said": "please note my private words",
    "claim_excerpts": ["a private body sentence"],
    "note": "a model's private note",
    "reason": "a private reason",
    "transcript_path": "/home/someone/.claude/projects/p/t.jsonl",
    "scopes": ["projects:foo"],
    "review_answers": {"said": "my private words"},
    "suppressed_ids": [{"id": "m-2", "reason": "seen_in_segment"}],
    "omitted": ["m-3"],
}


def _kept_only(rows: list[dict[str, Any]]) -> None:
    """Each of `rows`, recorded with telemetry off, is a state row that
    carries no field its kind's readers do not read (`events.STATE_FIELDS`,
    the stamps, a write's or an update's override evidence), none inside a
    field kept by its keys, and no preview of any text."""
    for row in rows:
        kind = row["kind"]
        assert kind in events.STATE_KINDS, kind
        kept = events.STATE_FIELDS[kind]
        allowed = set(kept) | events.STATE_STAMP_FIELDS | _ROW_META
        extra = {
            name
            for name in set(row) - allowed
            if not (
                kind in events.OVERRIDE_EVIDENCE_KINDS
                and name.endswith("_acknowledged")
            )
        }
        assert extra == set(), (kind, sorted(extra))
        for name, keys in kept.items():
            if keys is None or name not in row:
                continue
            value = row[name]
            for item in value if isinstance(value, list) else [value]:
                assert set(item) <= keys, (kind, name, item)
        assert '"preview"' not in json.dumps(row), kind


def test_no_kept_field_is_free_text() -> None:
    """The fields a state kind keeps are ids, statuses, counts, stamps and
    paths: none is a name the audit export redacts as free text
    (`audit_export.REDACTED_FIELDS`), but the Stop's probe, kept as its
    hash and length."""
    from bettermemory.audit_export import REDACTED_FIELDS

    for kind, kept in events.STATE_FIELDS.items():
        for name, keys in kept.items():
            if name == "probe_query":
                assert keys == frozenset({"hash", "len"}), kind
                continue
            assert name not in REDACTED_FIELDS, (kind, name)
            assert not (keys or frozenset()) & REDACTED_FIELDS, (kind, name)
    assert not events.STATE_STAMP_FIELDS & REDACTED_FIELDS


def test_telemetry_off_every_state_kind_keeps_only_its_readers_fields(
    store: Store,
) -> None:
    """Every state kind, recorded with each field its readers read and the
    free text measurement carries beside them: with telemetry off the
    listed fields and the stamps land, and nothing else (H2)."""
    recorder = Recorder(
        store=store,
        session_id="s1",
        enabled=False,
        worktree_root="/work/foo",
        triggered_from="stop_hook",
    )
    for kind in sorted(events.STATE_KINDS):
        listed = {
            name: _NOISE[name] if keys else f"{kind}.{name}"
            for name, keys in events.STATE_FIELDS[kind].items()
        }
        recorder.record(kind, **{**_NOISE, **listed})
    rows = list(store.iter_events())
    assert [row["kind"] for row in rows] == sorted(events.STATE_KINDS)
    _kept_only(rows)
    for row in rows:
        assert set(events.STATE_FIELDS[row["kind"]]) <= set(row), row["kind"]
        assert (row["worktree_root"], row["triggered_from"]) == (
            "/work/foo",
            "stop_hook",
        )
    by_kind = {row["kind"]: row for row in rows}
    assert by_kind["turn_audited"]["probe_query"]["len"] == len(_NOISE["probe_query"])
    assert by_kind["search_miss"]["top_hits"] == [{"id": "m-1"}]


def test_telemetry_on_a_state_row_keeps_its_whole_payload(store: Store) -> None:
    recorder = Recorder(store=store, session_id="s1", enabled=True)
    recorder.record(
        "standing_guard", door="memory_update", standing=["m-1"], said="yes"
    )
    recorder.record("use", ids=["m-1"], outcome="applied", claim_excerpts=["x"])
    guard, use = store.iter_events()
    assert guard["said"] == "yes" and use["claim_excerpts"] == ["x"]


def test_telemetry_off_an_interrupted_turn_keeps_no_preview(
    world: PromptWorld, tmp_path: Path
) -> None:
    """The next Stop and the next prompt's recall audit an interrupted
    turn; its recorder read no switch, so with telemetry off its row kept
    the first 32 characters of the owner's prompt (H1). Every row the two
    hooks write keeps only its readers' fields."""
    from datetime import datetime, timedelta, timezone

    from .test_recall_settlement import _audited, _interrupted, _stop

    world.config = _off(world)
    world.write()
    transcript = _interrupted(
        tmp_path / "t.jsonl", at=datetime.now(timezone.utc) - timedelta(minutes=10)
    )
    world.recall(
        config=world.config, state=RecallState(), transcript_path=str(transcript)
    )
    _stop(world, transcript, "p2")
    audited = _audited(world)
    assert set(audited) == {"p1", "p2"}
    for row in audited.values():
        assert set(row["probe_query"]) == {"hash", "len"}
    _kept_only(list(world.store.iter_events()))


def test_telemetry_off_the_brief_the_recall_and_the_stop_keep_no_text(
    world: PromptWorld, tmp_path: Path
) -> None:
    """S3 of the cold review: the recall's snippets and transcript path,
    and the settlements' claim excerpts, landed with telemetry off. Now the
    brief shows, the recall delivers, the Stop settles both, and no row
    keeps a field its readers do not read."""
    off = _off(world)
    world.config = off
    memory = world.write()
    state = RecallState()
    assert _brief(world, off, state=state).ids == (memory.id,)
    other = world.write(f"{BODY} for the archive volumes too")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    delivered = world.recall(config=off, state=state, transcript_path=str(transcript))
    assert delivered.ids == (other.id,)
    hook_module.run_audit(
        user_message=QUERY,
        assistant_response=f"The answer: {BODY}.",
        session_id=SESSION,
        config=off,
        store=world.store,
        state=state,
    )
    (audit,) = _rows(world.store, "turn_audited")
    assert audit["settled_count"] == 2
    assert {mid for row in _rows(world.store, "use") for mid in row["ids"]} == {
        memory.id,
        other.id,
    }
    _kept_only(list(world.store.iter_events()))


async def test_telemetry_off_a_use_keeps_no_note_and_no_excerpt(
    world: PromptWorld,
) -> None:
    memory = world.write()
    server = build_server(config=_off(world), store=world.store, state=SessionState())
    await call_tool(
        server,
        "memory_record_use",
        {
            "memory_ids": [memory.id],
            "outcome": "contradicted",
            "note": "a model's private note",
            "claim_excerpts": [BODY],
        },
    )
    (row,) = _rows(world.store, "use")
    assert (row["ids"], row["outcome"]) == ([memory.id], "contradicted")
    _kept_only([row])
    # The contradiction still holds the record back.
    assert memory.id in world.store.contradicted_at([memory.id])


async def test_telemetry_off_a_review_answer_keeps_no_quoted_words(
    quiet: Harness,
) -> None:
    """S2 of the cold review: every committed write under the sealed review
    carries a review id, and its row kept the user's typed words
    (`review_answers.said`) with telemetry off."""
    h = quiet
    h.seed(BACKUP_OLD)
    first = await h.write(BACKUP_NEW)
    shown = [entry["id"] for entry in first["review"]["related"]]
    done = await h.write(
        BACKUP_NEW,
        review={
            "review_id": first["review"]["review_id"],
            "relation": {i: "unrelated" for i in shown},
            "unsafe": 0.0,
            "secret": 0.0,
            "standing": 0.0,
            "said": "please note my private words here",
        },
    )
    assert done["status"] == "committed", done
    (answer,) = h.events("write")
    assert answer["review_id"] == first["review"]["review_id"]
    assert "review_answers" not in answer
    assert review.find_chain(h.store, answer["review_id"]).decided
    _kept_only(list(h.store.iter_events()))


# ---------------------------------------------------------------------------
# Telemetry off: what a turn searched is settled and not recalled again
# ---------------------------------------------------------------------------


def _searched(world: PromptWorld, memory_id: str, *, enabled: bool) -> None:
    """The server's `search` row for a search that returned `memory_id`, as
    the search handler records it."""
    Recorder(
        store=world.store,
        session_id="sess_server",
        enabled=enabled,
        worktree_root=world.caller.worktree_root,
    ).record("search", query=QUERY, returned=[memory_id], scores=[0.9])


@pytest.mark.parametrize("telemetry", [True, False], ids=["on", "off"])
def test_the_stop_settles_a_searched_memory_whatever_telemetry_says(
    world: PromptWorld, telemetry: bool
) -> None:
    """S6b of the cold review (M1): the Stop settles a turn's retrievals
    from its `search` rows, which were measurement, so with telemetry off a
    memory the model searched and quoted was never settled."""
    cfg = config(world.store_dir, telemetry=telemetry)
    memory = world.write()
    _searched(world, memory.id, enabled=telemetry)
    hook_module.run_audit(
        user_message=QUERY,
        assistant_response=f"Yes: {BODY}.",
        session_id=SESSION,
        config=cfg,
        store=world.store,
        state=RecallState(),
    )
    (use,) = _rows(world.store, "use")
    assert (use["ids"], use["attribution"]) == ([memory.id], "hook")
    (audit,) = _rows(world.store, "turn_audited")
    assert audit["settled_count"] == 1
    if not telemetry:
        _kept_only(list(world.store.iter_events()))


def test_telemetry_off_a_memory_searched_or_written_is_not_recalled(
    world: PromptWorld,
) -> None:
    """The seen gate reads the segment's `search`, `write` and `update`
    rows; with telemetry off none was written, so the recall delivered a
    memory the model had just searched or written as new."""
    searched = world.write()
    written = world.write(f"{BODY} again")
    third = world.write(f"{BODY} for the archive volumes too")
    _searched(world, searched.id, enabled=False)
    Recorder(
        store=world.store,
        session_id="sess_server",
        enabled=False,
        worktree_root=world.caller.worktree_root,
    ).record("write", status="committed", id=written.id, scopes=["projects:foo"])
    delivered = world.recall(config=_off(world), state=RecallState())
    assert delivered.ids == (third.id,)
    _kept_only(list(world.store.iter_events()))


async def test_telemetry_off_memory_search_records_its_ids_alone(
    world: PromptWorld,
) -> None:
    memory = world.write(origin=None, scopes=["tools"])
    server = build_server(config=_off(world), store=world.store, state=SessionState())
    await call_tool(server, "memory_search", {"query": QUERY})
    (row,) = _rows(world.store, "search")
    assert memory.id in row["returned"]
    assert "query" not in row
    _kept_only([row])


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
        recorder.record("recall_held", ids=[])
    assert len(pauses) == events.STATE_WRITE_ATTEMPTS - 1
    assert _rows(store, "show") == [] and _rows(store, "recall_held") == []
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
