"""Integration tests for the memory_health MCP tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    return build_server(config=cfg, store=Store(memory_dir), state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


async def test_memory_health_is_registered(server: Any) -> None:
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_health" in names


async def test_memory_health_returns_expected_keys(server: Any) -> None:
    res = await _call(server, "memory_health")
    expected = {
        "generated_at",
        "window_days",
        "total_active_memories",
        "total_events",
        "distinct_sessions",
        "dead_weight",
        "heavily_used",
        "contradicted",
        "marker_stats",
        "scope_distribution",
    }
    assert expected <= set(res.keys())


async def test_memory_health_reflects_recent_activity(server: Any) -> None:
    written = await _call(
        server, "memory_write", content="durable fact", scopes=["tools"]
    )
    await _call(server, "memory_search", query="durable")
    await _call(
        server,
        "memory_record_use",
        memory_ids=[written["id"]],
        outcome="applied",
    )

    # min_applied=1 lowers the heavily_used floor for this test — the
    # default threshold of 3 would correctly exclude a single applied
    # event, but we want to assert that the plumbing works (write →
    # search → use → health), not the threshold itself.
    res = await _call(
        server,
        "memory_health",
        window_days=0,
        heavily_used_top_k=10,
        min_applied=1,
    )
    assert res["total_active_memories"] >= 1
    # The just-written memory should have applied=1.
    used_ids = [m["id"] for m in res["heavily_used"]]
    assert written["id"] in used_ids


async def test_memory_health_window_days_filters_dead_and_cold(
    server: Any,
) -> None:
    """A freshly-written memory shouldn't appear in either curation bucket
    when the window is generous; with window=0 it's eligible. Under the
    new rule a memory with zero retrievals lands in `cold_memories`,
    not `dead_weight`. To land in dead_weight we need a retrieval
    event with no applied — exercise both."""
    written = await _call(
        server, "memory_write", content="freshly written body", scopes=["tools"]
    )
    big_window = await _call(server, "memory_health", window_days=30)
    zero_window = await _call(server, "memory_health", window_days=0)

    assert len(big_window["dead_weight"]) == 0
    assert len(big_window["cold_memories"]) == 0
    # Without a retrieval event, a "stale" memory is cold, not dead.
    assert len(zero_window["dead_weight"]) == 0
    assert len(zero_window["cold_memories"]) == 1
    assert zero_window["cold_memories"][0]["id"] == written["id"]

    # Now retrieve the memory once (no record_use → no applied) so it
    # crosses into the dead-weight bucket the next time we check.
    await _call(server, "memory_search", query="freshly written body")
    after_retrieval = await _call(server, "memory_health", window_days=0)
    assert len(after_retrieval["dead_weight"]) == 1
    assert after_retrieval["dead_weight"][0]["id"] == written["id"]
    assert len(after_retrieval["cold_memories"]) == 0


async def test_memory_health_surfaces_marker_overrides(server: Any) -> None:
    # Trip the durability gate.
    await _call(
        server,
        "memory_write",
        content="Currently uses Postgres for the metadata store.",
        scopes=["infrastructure"],
    )
    # Override.
    await _call(
        server,
        "memory_write",
        content="The 'currently' phrase is durable in this style guide.",
        scopes=["learning-style"],
        acknowledge_transient=True,
    )

    res = await _call(server, "memory_health")
    by_marker = {m["marker"]: m for m in res["marker_stats"]}
    assert "currently" in by_marker
    stats = by_marker["currently"]
    assert stats["fire_count"] >= 1
    assert stats["override_count"] >= 1
