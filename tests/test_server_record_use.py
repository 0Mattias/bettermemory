"""Integration tests for memory_record_use — the model-driven feedback
signal that closes the retrieval loop."""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

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
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


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


async def test_record_corrected_emits_event(
    server_with_events: tuple[Any, Path],
) -> None:
    """`corrected` is the audit-after-fix outcome: the caller already
    resolved the drift via memory_update / memory_verify in the same
    turn, and this event is the audit-trail entry. The handler accepts
    it the same way as any other outcome — the behavioral difference
    (no contradiction-flag bump) lives in health.py, exercised by the
    test_health.py suite."""
    server, memory_dir = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    await _call(
        server,
        "memory_record_use",
        memory_ids=[written["id"]],
        outcome="corrected",
        note="Tool list was missing memory_restore; updated body and re-verified.",
    )

    e = [e for e in iter_events(memory_dir) if e["kind"] == "use"][-1]
    assert e["outcome"] == "corrected"
    assert "memory_restore" in e["note"]


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
    with pytest.raises(Exception, match="memory_ids must contain at least one"):
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
    with pytest.raises(Exception, match="outcome must be one of"):
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
    with pytest.raises(Exception, match="invalid memory id"):
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


# ---------------------------------------------------------------------------
# Note length cap — parity with the web /verify endpoint
# ---------------------------------------------------------------------------


async def test_record_use_rejects_oversized_note(
    server_with_events: tuple[Any, Path],
) -> None:
    """`note` caps at 800 chars so a hostile client (or a runaway
    model) can't inflate the JSONL event log with multi-megabyte
    notes. (The cap predates 5.0 at 500 chars, matching the
    since-removed web UI's /verify form; raised to 800 in 5.7.0 on
    the T1 live-store census — bench/rot/T3_NOTE_CAP_DECISION.md.)"""
    server, _ = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    with pytest.raises(Exception, match="cap is 800"):
        await _call(
            server,
            "memory_record_use",
            memory_ids=[written["id"]],
            outcome="ignored",
            note="x" * 801,
        )


async def test_record_use_accepts_max_length_note(
    server_with_events: tuple[Any, Path],
) -> None:
    """Sanity check: exactly 800 chars is accepted (the cap is
    inclusive)."""
    server, _ = server_with_events
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    res = await _call(
        server,
        "memory_record_use",
        memory_ids=[written["id"]],
        outcome="ignored",
        note="x" * 800,
    )
    assert res["outcome"] == "ignored"
