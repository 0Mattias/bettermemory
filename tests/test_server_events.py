"""Integration tests: each tool call emits the expected event.

These tests pin the contract between the server handlers and the event log.
If you change a handler's recorder.record(...) call, this is the file that
should fail loudly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path, SessionState]:
    """A live server whose recorder writes into `memory_dir/.events.jsonl`."""
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


# ---------------------------------------------------------------------------
# Per-handler events
# ---------------------------------------------------------------------------


async def test_write_emits_committed_event(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )

    events = _events(memory_dir)
    write_events = [e for e in events if e["kind"] == "write"]
    assert len(write_events) == 1
    e = write_events[0]
    assert e["status"] == "committed"
    assert e["id"] == written["id"]
    assert e["scopes"] == ["tools"]
    assert e["forced"] is False


async def test_write_duplicate_emits_event(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_write", content=body, scopes=["tools"])

    events = [e for e in _events(memory_dir) if e["kind"] == "write"]
    assert [e["status"] for e in events] == ["committed", "duplicate"]
    assert events[1]["matches"] == [events[0]["id"]]
    assert events[1]["forced"] is False


async def test_write_force_records_forced_true(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    body = "vendored python-frontmatter"
    await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_write", content=body, scopes=["tools"], force=True)

    forced_events = [
        e
        for e in _events(memory_dir)
        if e["kind"] == "write" and e.get("forced") is True
    ]
    assert len(forced_events) == 1
    assert forced_events[0]["status"] == "committed"


async def test_search_records_query_and_returned_ids(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking troubleshooting",
        scopes=["infrastructure"],
    )
    await _call(server, "memory_search", query="kubernetes networking")

    search_events = [e for e in _events(memory_dir) if e["kind"] == "search"]
    assert len(search_events) == 1
    e = search_events[0]
    # `telemetry.log_queries_verbatim` defaults to False since 2.6.8:
    # the query lands as `{"hash", "preview", "len"}`. The preview keeps
    # the first 32 chars (the full query here is shorter) so the field
    # is still triage-readable without storing the full text.
    assert isinstance(e["query"], dict)
    assert e["query"]["preview"] == "kubernetes networking"
    assert e["query"]["len"] == len("kubernetes networking")
    assert len(e["query"]["hash"]) == 16
    assert written["id"] in e["returned"]
    assert "high" in e["relevance"] or "medium" in e["relevance"]


async def test_search_expand_top_records_expanded_id(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    written = await _call(
        server,
        "memory_write",
        content="kubernetes networking troubleshooting",
        scopes=["infrastructure"],
    )
    await _call(
        server,
        "memory_search",
        query="kubernetes networking troubleshooting",
        expand_top=True,
    )

    search_events = [e for e in _events(memory_dir) if e["kind"] == "search"]
    assert search_events[-1]["expand_top"] is True
    assert search_events[-1]["expanded_id"] == written["id"]


async def test_show_emits_event(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_show", id=written["id"])

    show_events = [e for e in _events(memory_dir) if e["kind"] == "show"]
    assert len(show_events) == 1
    assert show_events[0]["id"] == written["id"]


async def test_update_records_fields_changed(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    written = await _call(server, "memory_write", content="initial", scopes=["tools"])
    await _call(
        server,
        "memory_update",
        id=written["id"],
        content="refined",
        confidence="high",
    )

    upd = [e for e in _events(memory_dir) if e["kind"] == "update"]
    assert len(upd) == 1
    assert upd[0]["id"] == written["id"]
    assert set(upd[0]["fields"]) == {"content", "confidence"}
    assert upd[0]["confidence"] == "high"


async def test_list_records_count_and_returned(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    a = await _call(server, "memory_write", content="alpha", scopes=["tools"])
    b = await _call(server, "memory_write", content="beta", scopes=["tools"])
    await _call(server, "memory_list")

    list_events = [e for e in _events(memory_dir) if e["kind"] == "list"]
    assert len(list_events) == 1
    assert list_events[0]["count"] == 2
    assert set(list_events[0]["returned"]) == {a["id"], b["id"]}
    assert list_events[0]["with_bodies"] is False


async def test_remove_records_reason(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="superseded")

    rm = [e for e in _events(memory_dir) if e["kind"] == "remove"]
    assert len(rm) == 1
    assert rm[0]["id"] == written["id"]
    assert rm[0]["reason"] == "superseded"


async def test_scope_disable_enable_record_events(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, _ = server_with_events
    await _call(server, "memory_scope_disable", scope="projects:foo")
    await _call(server, "memory_scope_enable", scope="projects:foo")

    kinds = [e["kind"] for e in _events(memory_dir)]
    assert "scope_disable" in kinds
    assert "scope_enable" in kinds


# ---------------------------------------------------------------------------
# Pending-write flow
# ---------------------------------------------------------------------------


async def test_pending_then_confirm_records_both(
    memory_dir: Path,
) -> None:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(require_write_confirmation=True),
    )
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )

    pending = await _call(
        server, "memory_write", content="pending fact", scopes=["tools"]
    )
    await _call(server, "memory_write_confirm", pending_id=pending["pending_id"])

    events = list(iter_events(memory_dir))
    kinds = [e["kind"] for e in events]
    assert kinds == ["write", "write_confirm"]
    assert events[0]["status"] == "pending"
    assert events[0]["pending_id"] == pending["pending_id"]
    assert events[1]["pending_id"] == pending["pending_id"]


async def test_pending_then_cancel_records_existed_true(
    memory_dir: Path,
) -> None:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(require_write_confirmation=True),
    )
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )

    pending = await _call(
        server, "memory_write", content="reconsider", scopes=["tools"]
    )
    await _call(server, "memory_write_cancel", pending_id=pending["pending_id"])

    cancel_events = [e for e in iter_events(memory_dir) if e["kind"] == "write_cancel"]
    assert len(cancel_events) == 1
    assert cancel_events[0]["existed"] is True


# ---------------------------------------------------------------------------
# Telemetry-disabled config
# ---------------------------------------------------------------------------


async def test_disabled_telemetry_writes_nothing(memory_dir: Path) -> None:
    from bettermemory.config import TelemetryConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        telemetry=TelemetryConfig(enabled=False),
    )
    state = SessionState()
    server = build_server(config=cfg, store=Store(memory_dir), state=state)

    await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_search", query="x")

    # No event log file at all.
    assert not (memory_dir / ".events.jsonl").exists()


# ---------------------------------------------------------------------------
# Session id consistency
# ---------------------------------------------------------------------------


async def test_all_events_carry_same_session_id(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, memory_dir, state = server_with_events
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_show", id=written["id"])
    await _call(server, "memory_search", query="x")

    events = list(iter_events(memory_dir))
    assert len(events) == 3
    sessions = {e["session"] for e in events}
    assert sessions == {state.session_id}
