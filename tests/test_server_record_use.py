"""Integration tests for memory_record_use — the model-driven feedback
signal that closes the retrieval loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=rec
    )
    return server, memory_dir


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


async def test_memory_record_use_is_registered(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_record_use" in names


# ---------------------------------------------------------------------------
# Happy path — each outcome
# ---------------------------------------------------------------------------


async def test_record_applied_emits_event(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )

    res = await _call(
        server,
        "memory_record_use",
        memory_ids=[written["id"]],
        outcome="applied",
    )
    assert res["recorded"] == [written["id"]]
    assert res["outcome"] == "applied"

    use_events = [e for e in iter_events(memory_dir) if e["kind"] == "use"]
    assert len(use_events) == 1
    e = use_events[0]
    assert e["ids"] == [written["id"]]
    assert e["outcome"] == "applied"
    assert e["note"] is None


async def test_record_ignored_emits_event(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    await _call(
        server,
        "memory_record_use",
        memory_ids=[written["id"]],
        outcome="ignored",
    )

    use_events = [e for e in iter_events(memory_dir) if e["kind"] == "use"]
    assert use_events[-1]["outcome"] == "ignored"


async def test_record_contradicted_with_note(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    await _call(
        server,
        "memory_record_use",
        memory_ids=[written["id"]],
        outcome="contradicted",
        note="user said the project switched from SQLite to Postgres",
    )

    e = [e for e in iter_events(memory_dir) if e["kind"] == "use"][-1]
    assert e["outcome"] == "contradicted"
    assert "Postgres" in e["note"]


async def test_record_use_multiple_ids_at_once(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    a = await _call(server, "memory_write", content="alpha fact", scopes=["tools"])
    b = await _call(server, "memory_write", content="beta fact", scopes=["tools"])
    await _call(
        server,
        "memory_record_use",
        memory_ids=[a["id"], b["id"]],
        outcome="applied",
    )

    e = [e for e in iter_events(memory_dir) if e["kind"] == "use"][-1]
    assert set(e["ids"]) == {a["id"], b["id"]}


# ---------------------------------------------------------------------------
# Validation — bad input is rejected
# ---------------------------------------------------------------------------


async def test_record_use_empty_ids_rejected(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_record_use",
            memory_ids=[],
            outcome="applied",
        )


async def test_record_use_invalid_outcome_rejected(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_record_use",
            memory_ids=[written["id"]],
            outcome="awesome",
        )


async def test_record_use_invalid_ulid_rejected(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_record_use",
            memory_ids=["not-a-ulid"],
            outcome="applied",
        )


async def test_record_use_accepts_valid_ulid_against_unknown_memory(
    server_with_events: tuple[Any, Path],
) -> None:
    """A well-formed ULID that doesn't correspond to a real memory is still
    accepted — the store isn't loaded on every record_use call. The event
    log captures what the caller said happened; analysis code can
    cross-reference."""
    server, memory_dir = server_with_events
    res = await _call(
        server,
        "memory_record_use",
        memory_ids=["01HXYZGACYJDKEGACY00000ABC"],
        outcome="applied",
    )
    assert res["outcome"] == "applied"


# ---------------------------------------------------------------------------
# Telemetry-disabled config
# ---------------------------------------------------------------------------


async def test_record_use_with_telemetry_disabled_is_noop(
    memory_dir: Path,
) -> None:
    from bettermemory.config import TelemetryConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        telemetry=TelemetryConfig(enabled=False),
    )
    state = SessionState()
    server = build_server(config=cfg, store=Store(memory_dir), state=state)

    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    res = await _call(
        server,
        "memory_record_use",
        memory_ids=[written["id"]],
        outcome="applied",
    )
    # The tool returns success — disabled telemetry means the side effect
    # is a no-op, not that the tool errors.
    assert res["outcome"] == "applied"
    # And no event log file is created.
    assert not (memory_dir / ".events.jsonl").exists()
