"""Integration tests for the health report: `memory_admin(action="health")`
on the wire, and `health.report_for_store` where a test needs the knobs
the tool no longer exposes."""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.health import report_for_store
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server(memory_dir: Path, store: Store) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(store=store, session_id=state.session_id)
    return build_server(config=cfg, store=store, state=state, recorder=rec)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


async def _health(server: Any) -> dict[str, Any]:
    return await _call(server, "memory_admin", action="health")


async def test_memory_admin_is_registered(server: Any) -> None:
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_admin" in names


async def test_memory_health_returns_expected_keys(server: Any) -> None:
    res = await _health(server)
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


async def test_memory_health_reflects_recent_activity(
    server: Any, store: Store
) -> None:
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

    # `heavily_used_min_applied=1` lowers the heavily_used floor for this
    # test: the default threshold of 3 would correctly exclude a single
    # applied event, but we want to assert that the plumbing works
    # (write, search, use, health), not the threshold itself.
    res = report_for_store(
        store, window_days=0, heavily_used_top_k=10, heavily_used_min_applied=1
    ).to_dict()
    assert res["total_active_memories"] >= 1
    # The just-written memory should have applied=1.
    used_ids = [m["id"] for m in res["heavily_used"]]
    assert written["id"] in used_ids


async def test_memory_health_window_days_filters_dead_and_cold(
    server: Any, store: Store, tmp_path: Path
) -> None:
    """A freshly-written memory shouldn't appear in either curation bucket
    when the window is generous; with window=0 it's eligible. Under the
    new rule a memory with zero retrievals lands in `cold_memories`,
    not `dead_weight`, and a retrieval only counts against the memory
    once it is older than the endorsement grace (the auto-applied
    endorsement structurally lags retrieval, so a seconds-old search
    must NOT make the memory dead weight). Exercise all three."""
    written = await _call(
        server, "memory_write", content="freshly written body", scopes=["tools"]
    )
    big_window = report_for_store(store, window_days=30).to_dict()
    zero_window = report_for_store(store, window_days=0).to_dict()

    assert len(big_window["dead_weight"]) == 0
    assert len(big_window["cold_memories"]) == 0
    # Without a retrieval event, a "stale" memory is cold, not dead.
    assert len(zero_window["dead_weight"]) == 0
    assert len(zero_window["cold_memories"]) == 1
    assert zero_window["cold_memories"][0]["id"] == written["id"]

    # Retrieve the memory once (no record_use, so no applied). Inside the
    # endorsement grace the retrieval is evidence the ranker works, not
    # evidence against the memory: neither bucket may claim it.
    await _call(server, "memory_search", query="freshly written body")
    in_grace = report_for_store(store, window_days=0).to_dict()
    assert len(in_grace["dead_weight"]) == 0
    assert len(in_grace["cold_memories"]) == 0

    # A retrieval aged past the grace, never followed by an apply, crosses
    # into dead_weight. The recorder always stamps "now" and the log is
    # append-only, so the aged shape is built in a second store: the
    # memory, a `search` event three days old that returned it (landed
    # under its own `ts` through `import_event`), and one Stop-hook
    # `turn_audited` row, because the report refuses to call anything
    # dead weight on a store where nothing was ever in a position to
    # record an apply (`health.is_hook_telemetry_event`). The hook row
    # records no apply against any memory, so the memory under test
    # stays retrieved-never-applied.
    aged_store = Store(tmp_path / "aged")
    aged = aged_store.write(content="freshly written body", scopes=["tools"])
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    aged_store.import_event(
        {
            "ts": three_days_ago,
            "session": "sess-old",
            "kind": "search",
            "returned": [aged.id],
        }
    )
    aged_store.import_event(
        {
            "ts": three_days_ago,
            "session": "sess-stop-hook",
            "kind": "turn_audited",
            "triggered_from": "stop_hook",
            "verdict": "ok",
        }
    )
    after_grace = report_for_store(aged_store, window_days=0).to_dict()
    assert len(after_grace["dead_weight"]) == 1
    assert after_grace["dead_weight"][0]["id"] == aged.id
    assert len(after_grace["cold_memories"]) == 0


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

    res = await _health(server)
    by_marker = {m["marker"]: m for m in res["marker_stats"]}
    assert "currently" in by_marker
    stats = by_marker["currently"]
    assert stats["fire_count"] >= 1
    assert stats["override_count"] >= 1
