"""Tests for server.py — tool registration and end-to-end behavior.

We exercise the registered tools via FastMCP's `call_tool` rather than
spinning up the full stdio transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memory_mcp.config import Config, StorageConfig
from memory_mcp.server import build_server
from memory_mcp.session import SessionState
from memory_mcp.store import Store


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


@pytest.fixture
def confirming_server(memory_dir: Path) -> tuple[Any, SessionState]:
    """A server with require_write_confirmation=True."""
    from memory_mcp.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(require_write_confirmation=True),
    )
    state = SessionState()
    return build_server(config=cfg, store=Store(memory_dir), state=state), state


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return the parsed JSON payload.

    FastMCP returns a list[ContentBlock]; for our tools the structured
    result is what we care about, available via `call_tool`'s second value.
    """
    content, structured = await server.call_tool(name, kwargs)  # type: ignore[attr-defined]
    if structured is not None:
        return structured
    # Fallback: parse text content.
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------


async def test_six_tools_registered_plus_companion(server: Any) -> None:
    tools = await server.list_tools()
    names = {t.name for t in tools}
    expected = {
        "memory_search",
        "memory_show",
        "memory_write",
        "memory_list",
        "memory_remove",
        "memory_scope_disable",
        # Companion to scope_disable, mentioned in the spec.
        "memory_scope_enable",
    }
    assert expected <= names


async def test_each_tool_has_description_and_input_schema(server: Any) -> None:
    tools = await server.list_tools()
    for tool in tools:
        assert tool.description, f"{tool.name} missing description"
        assert tool.inputSchema, f"{tool.name} missing inputSchema"


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------


async def test_write_then_show_roundtrip(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="Prefer hands-on code tutorials.",
        scopes=["learning-style"],
    )
    assert written["scopes"] == ["learning-style"]

    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["id"] == written["id"]
    assert "code tutorials" in shown["body"]


async def test_search_finds_written_memory(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="The home lab is on subnet 10.42.",
        scopes=["infrastructure"],
    )
    hits = await _call(server, "memory_search", query="home lab subnet")
    # Structured returns under the "result" key for FastMCP — handle both.
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    assert len(hits) >= 1
    assert "home lab" in hits[0]["snippet"]


async def test_remove_excludes_from_list(server: Any) -> None:
    written = await _call(
        server,
        "memory_write",
        content="Temporary fact.",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_remove",
        id=written["id"],
        reason="not durable",
    )

    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert all(item["id"] != written["id"] for item in listing)


async def test_disabled_scope_hidden_from_search_and_list(server: Any) -> None:
    await _call(
        server,
        "memory_write",
        content="This is about project alpha.",
        scopes=["projects:alpha"],
    )
    await _call(
        server,
        "memory_write",
        content="Generic tooling preference.",
        scopes=["tools"],
    )

    state = await _call(
        server, "memory_scope_disable", scope="projects:alpha"
    )
    state = state.get("result", state) if isinstance(state, dict) and "result" in state else state
    assert "projects:alpha" in state["disabled_scopes"]

    hits = await _call(server, "memory_search", query="alpha")
    hits = hits.get("result", hits) if isinstance(hits, dict) else hits
    assert all("projects:alpha" not in h["scopes"] for h in hits)

    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert all("projects:alpha" not in item["scopes"] for item in listing)

    # Re-enabling brings them back.
    re = await _call(server, "memory_scope_enable", scope="projects:alpha")
    re = re.get("result", re) if isinstance(re, dict) and "result" in re else re
    assert "projects:alpha" not in re["disabled_scopes"]


# ---------------------------------------------------------------------------
# Validation surfaces
# ---------------------------------------------------------------------------


async def test_write_rejects_empty_scopes(server: Any) -> None:
    with pytest.raises(Exception):
        await _call(server, "memory_write", content="x", scopes=[])


async def test_write_rejects_invalid_scope(server: Any) -> None:
    with pytest.raises(Exception):
        await _call(server, "memory_write", content="x", scopes=["With Space"])


async def test_show_unknown_id_errors(server: Any) -> None:
    with pytest.raises(Exception):
        await _call(server, "memory_show", id="01HXYZNOTAREALIDOK000000ZZ")


# ---------------------------------------------------------------------------
# Pending-write flow (require_write_confirmation = true)
# ---------------------------------------------------------------------------


async def test_write_returns_pending_when_confirmation_required(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, state = confirming_server
    res = await _call(
        server,
        "memory_write",
        content="might want to remember this",
        scopes=["tools"],
    )
    assert res["status"] == "pending"
    assert res["pending_id"].startswith("pending_")
    assert res["preview"]["scopes"] == ["tools"]
    assert state.pending_writes  # parked, not committed


async def test_pending_write_commits_on_confirm(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _ = confirming_server
    pending = await _call(
        server,
        "memory_write",
        content="durable preference",
        scopes=["tools"],
    )
    committed = await _call(
        server, "memory_write_confirm", pending_id=pending["pending_id"]
    )
    assert committed["status"] == "committed"
    assert committed["id"]
    # Now visible via list
    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert any(item["id"] == committed["id"] for item in listing)


async def test_pending_write_does_not_persist_until_confirmed(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _ = confirming_server
    await _call(
        server,
        "memory_write",
        content="never to be committed",
        scopes=["tools"],
    )
    listing = await _call(server, "memory_list")
    listing = listing.get("result", listing) if isinstance(listing, dict) else listing
    assert listing == []


async def test_pending_write_can_be_cancelled(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, state = confirming_server
    pending = await _call(
        server,
        "memory_write",
        content="reconsider",
        scopes=["tools"],
    )
    pid = pending["pending_id"]
    res = await _call(server, "memory_write_cancel", pending_id=pid)
    assert res["existed"] is True
    assert pid not in state.pending_writes

    # Cannot confirm after cancel.
    with pytest.raises(Exception):
        await _call(server, "memory_write_confirm", pending_id=pid)


async def test_confirm_unknown_id_errors(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _ = confirming_server
    with pytest.raises(Exception):
        await _call(
            server, "memory_write_confirm", pending_id="pending_deadbeef0000"
        )


async def test_confirmation_disabled_writes_immediately(server: Any) -> None:
    """The default config commits immediately — no pending state."""
    res = await _call(
        server, "memory_write", content="immediate", scopes=["tools"]
    )
    assert res["status"] == "committed"
    assert "pending_id" not in res
