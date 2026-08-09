"""The transient-marker gate exists on the update path too.

`memory_write` hard-refuses a body carrying transient-state markers
("currently", "as of <date>") — state that will not be true in a week
belongs in episodes, not memories. The update path mirrored the
credential, user-claim, and truncation gates but not this one, so the
laundering route was open: write a durable-looking body, then EDIT the
transient state into the committed record and the refusal never runs.

Same markers, same `acknowledge_transient` escape, same hint text as
the write side, so the two surfaces refuse and release identically.
The wire-schema test pins the failure mode that shipped once already
for `acknowledge_user_claim`: a handler-only parameter the `_handlers`
facade does not carry never reaches the served schema and is silently
dropped at call time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call, input_schema as _input_schema

_CLEAN = "the deploy script lives in bin/deploy.sh"
_TRANSIENT = "The migration is currently blocked on the schema review."


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=rec,
    )
    return server, memory_dir


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


async def _seed_fact(server: Any) -> str:
    res = await _call(server, "memory_write", content=_CLEAN, scopes=["tools"])
    assert res["status"] == "committed"
    return str(res["id"])


async def test_write_refuses_and_update_used_to_launder(
    server_with_events: tuple[Any, Path],
) -> None:
    """Both halves of the parity claim in one test: the write path
    refuses `_TRANSIENT`, and the update path now refuses the identical
    body instead of committing it into an existing record."""
    server, root = server_with_events
    written = await _call(server, "memory_write", content=_TRANSIENT, scopes=["tools"])
    assert written["status"] == "transient_warning"

    mid = await _seed_fact(server)
    edited = await _call(server, "memory_update", id=mid, content=_TRANSIENT)
    assert edited["status"] == "transient_warning"
    assert edited["markers"], "the refusal must name what tripped it"

    # The record is untouched and the refusal is on the event log.
    shown = await _call(server, "memory_show", id=mid)
    assert "currently" not in shown["body"]
    events = [
        e
        for e in iter_events(root)
        if e["kind"] == "update" and e.get("status") == "transient_warning"
    ]
    assert events and events[-1]["markers"] == ["currently"]


async def test_acknowledge_transient_commits_and_records_the_override(
    server_with_events: tuple[Any, Path],
) -> None:
    """The escape mirrors the write side, and the success event carries
    `markers_acknowledged` — the same field write.py records and
    health.py consumes, so one grep covers both surfaces' override
    rates."""
    server, root = server_with_events
    mid = await _seed_fact(server)
    edited = await _call(
        server,
        "memory_update",
        id=mid,
        content=_TRANSIENT,
        acknowledge_transient=True,
    )
    assert edited["status"] == "committed"
    commits = [
        e
        for e in iter_events(root)
        if e["kind"] == "update" and e.get("status") is None
    ]
    assert commits[-1]["markers_acknowledged"] == ["currently"]

    # A clean-body edit keeps the field present and empty.
    clean = await _call(
        server,
        "memory_update",
        id=mid,
        content=_CLEAN + " and the rollback plan sits beside it.",
    )
    assert clean["status"] == "committed"
    commits = [
        e
        for e in iter_events(root)
        if e["kind"] == "update" and e.get("status") is None
    ]
    assert commits[-1]["markers_acknowledged"] == []


async def test_the_override_is_served_on_the_wire_defaulting_to_off(
    server_with_events: tuple[Any, Path],
) -> None:
    """A handler-only parameter passes an `inspect.signature` check and
    still does nothing at call time — the served schema is built from
    the `_handlers.py` facade. Pin the schema, not the signature."""
    server, _ = server_with_events
    tools = {t.name: t for t in await server.list_tools()}
    props = _input_schema(tools["memory_update"])["properties"]
    assert "acknowledge_transient" in props, (
        "memory_update does not serve `acknowledge_transient`; a caller "
        f"passing it is silently ignored. Served: {sorted(props)}"
    )
    assert props["acknowledge_transient"].get("default") is False
