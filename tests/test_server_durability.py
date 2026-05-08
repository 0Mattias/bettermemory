"""Integration tests for the durability gate in memory_write.

These tests pin the contract end-to-end through the MCP tool surface: the
durability check fires before dedup, returns a `transient_warning` status
that doesn't persist anything, and `acknowledge_transient=True` overrides
with the override recorded in the event log.
"""

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
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=rec,
    )
    return server, memory_dir


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)  # type: ignore[attr-defined]
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


# ---------------------------------------------------------------------------
# Transient bodies are blocked by default
# ---------------------------------------------------------------------------


async def test_transient_body_returns_warning(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="The auth service currently uses JWT for sessions.",
        scopes=["projects:auth"],
    )
    assert res["status"] == "transient_warning"
    markers = [m["marker"] for m in res["markers"]]
    assert "currently" in markers


async def test_transient_warning_does_not_persist(
    server_with_events: tuple[Any, Path],
) -> None:
    """A transient_warning response must not write to disk."""
    server, _ = server_with_events
    await _call(
        server,
        "memory_write",
        content="Today I refactored the auth flow.",
        scopes=["projects:auth"],
    )
    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) and "result" in listing else listing
    assert listing == []


async def test_multiple_markers_all_reported(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Today I shipped the auth refactor; we just merged commit a1b2c3d.",
        scopes=["projects:auth"],
    )
    assert res["status"] == "transient_warning"
    markers = {m["marker"] for m in res["markers"]}
    # At least three distinct markers should fire.
    assert "today i" in markers
    assert "we just" in markers
    assert any(m.startswith("sha:") for m in markers)


async def test_marker_response_includes_snippet(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content=(
            "The deployment pipeline is currently using GitHub Actions "
            "for the runner pool."
        ),
        scopes=["infrastructure"],
    )
    assert res["status"] == "transient_warning"
    snippet = res["markers"][0]["snippet"]
    assert "currently" in snippet.lower()


# ---------------------------------------------------------------------------
# Override path
# ---------------------------------------------------------------------------


async def test_acknowledge_transient_commits(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content="Conference talk Tuesday — slides currently in /tmp/.",
        scopes=["projects:talk"],
        acknowledge_transient=True,
    )
    assert res["status"] == "committed"


async def test_acknowledge_transient_records_overridden_markers(
    server_with_events: tuple[Any, Path],
) -> None:
    """The override is logged so we can compute the override rate per
    marker in the health view."""
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="The 'today I' phrase is durable in this style guide.",
        scopes=["learning-style"],
        acknowledge_transient=True,
    )
    write_events = [
        e for e in iter_events(memory_dir) if e["kind"] == "write"
    ]
    assert write_events
    e = write_events[-1]
    assert e["status"] == "committed"
    assert "today i" in e.get("markers_acknowledged", [])


async def test_clean_body_records_empty_acknowledged_list(
    server_with_events: tuple[Any, Path],
) -> None:
    """Normal writes carry markers_acknowledged=[] so analytics can count
    explicit overrides without filtering out clean writes by absence."""
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="The auth service uses JWT with rotating refresh tokens.",
        scopes=["projects:auth"],
    )
    write_events = [
        e for e in iter_events(memory_dir) if e["kind"] == "write"
    ]
    assert write_events[-1]["status"] == "committed"
    assert write_events[-1]["markers_acknowledged"] == []


# ---------------------------------------------------------------------------
# Ordering with dedup: durability fires first
# ---------------------------------------------------------------------------


async def test_durability_fires_before_dedup(
    server_with_events: tuple[Any, Path],
) -> None:
    """Two transient bodies that overlap heavily: the second write should
    return transient_warning, not duplicate. Catching transience first
    avoids routing the caller toward memory_update on a fact that itself
    shouldn't have been written."""
    server, _ = server_with_events
    body = "Currently the queue depth is around 200 messages."
    # First write: transient_warning, nothing persisted.
    first = await _call(
        server, "memory_write", content=body, scopes=["projects:queue"]
    )
    assert first["status"] == "transient_warning"

    # Second write: still transient_warning, not duplicate (since the first
    # didn't write anything).
    second = await _call(
        server, "memory_write", content=body, scopes=["projects:queue"]
    )
    assert second["status"] == "transient_warning"


async def test_acknowledged_transient_body_can_still_be_blocked_by_dedup(
    server_with_events: tuple[Any, Path],
) -> None:
    """Once acknowledge_transient passes the durability gate, dedup runs
    normally — a second acknowledged write of the same body returns
    duplicate, not committed."""
    server, _ = server_with_events
    body = "Currently the queue depth is around 200 messages."
    first = await _call(
        server,
        "memory_write",
        content=body,
        scopes=["projects:queue"],
        acknowledge_transient=True,
    )
    assert first["status"] == "committed"

    second = await _call(
        server,
        "memory_write",
        content=body,
        scopes=["projects:queue"],
        acknowledge_transient=True,
    )
    assert second["status"] == "duplicate"


# ---------------------------------------------------------------------------
# Telemetry of the warning itself
# ---------------------------------------------------------------------------


async def test_transient_warning_logs_event_with_markers(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="Currently shipping a fix for the login bug.",
        scopes=["projects:auth"],
    )
    write_events = [
        e for e in iter_events(memory_dir) if e["kind"] == "write"
    ]
    assert len(write_events) == 1
    e = write_events[0]
    assert e["status"] == "transient_warning"
    assert "currently" in e["markers"]
