"""The two dispatch tools of the nine-tool surface: `episode` and
`memory_admin`. Each action is a former tool of its own; these tests pin
the dispatch (the action reaches its body, a missing argument is named)
and that each action returns the keys its former tool returned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig

from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call


@pytest.fixture
def served(memory_dir: Path) -> tuple[Any, SessionState, Store]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    store = Store(memory_dir)
    return build_server(config=cfg, store=store, state=state), state, store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


# ---------------------------------------------------------------------------
# episode
# ---------------------------------------------------------------------------


async def test_episode_write_then_handoff(served: Any) -> None:
    server, state, _ = served
    written = await _call(
        server,
        "episode",
        action="write",
        body="tried the port on the store; fell over at the fold",
        takeaway="the fold needs the log_mac column",
        scopes=["projects:bettermemory"],
    )
    assert written["status"] == "committed"
    assert written["session_id"] == state.session_id
    assert written["takeaway"] == "the fold needs the log_mac column"
    assert set(written) >= {"id", "created", "scopes", "swarm_id", "pruned_sessions"}

    # A second server in the same store is the next session; its handoff
    # resolves this one through the event log.
    other = build_server(
        config=Config(storage=StorageConfig(directory=str(served[2].path.parent))),
        store=served[2],
        state=SessionState(),
    )
    handoff = await _call(other, "episode", action="handoff")
    assert handoff["prior_session_id"] == state.session_id
    assert [row["takeaway"] for row in handoff["episodes"]] == [
        "the fold needs the log_mac column"
    ]
    assert "body" not in handoff["episodes"][0]
    with_bodies = await _call(
        other, "episode", action="handoff", include_bodies=True, max_episodes=1
    )
    assert with_bodies["episodes"][0]["body"].startswith("tried the port")


async def test_episode_write_needs_a_body(served: Any) -> None:
    server, _, _ = served
    with pytest.raises(Exception, match="needs `body`"):
        await _call(server, "episode", action="write")


async def test_episode_rejects_an_unknown_action(served: Any) -> None:
    server, _, _ = served
    with pytest.raises(Exception):
        await _call(server, "episode", action="promote", body="x")


# ---------------------------------------------------------------------------
# memory_admin
# ---------------------------------------------------------------------------


async def test_admin_disable_and_enable_scope(served: Any) -> None:
    server, state, _ = served
    out = await _call(server, "memory_admin", action="disable_scope", scope="tools")
    assert out == {"disabled_scopes": ["tools"]}
    assert state.disabled_scopes == {"tools"}
    out = await _call(server, "memory_admin", action="enable_scope", scope="tools")
    assert out == {"disabled_scopes": []}


async def test_admin_tombstones_and_restore(served: Any) -> None:
    server, _, store = served
    a = await _call(server, "memory_write", content="alpha fact", scopes=["tools"])
    b = await _call(
        server, "memory_write", content="beta fact", scopes=["infrastructure"]
    )
    removed = await _call(server, "memory_remove", id=a["id"], reason="wrong")
    assert removed == {"removed": a["id"]}
    await _call(server, "memory_remove", id=b["id"], reason="wrong")

    listed = await _call(server, "memory_admin", action="tombstones")
    assert {row["id"] for row in listed["tombstones"]} == {a["id"], b["id"]}
    scoped = await _call(server, "memory_admin", action="tombstones", scopes=["tools"])
    assert [row["id"] for row in scoped["tombstones"]] == [a["id"]]

    restored = await _call(server, "memory_admin", action="restore", id=a["id"])
    assert restored["status"] == "committed"
    assert restored["id"] == a["id"]
    assert store.load_one(a["id"]).body.strip() == "alpha fact"


async def test_admin_restore_needs_an_id(served: Any) -> None:
    server, _, _ = served
    with pytest.raises(Exception, match="needs `id`"):
        await _call(server, "memory_admin", action="restore")


async def test_admin_rename_scope(served: Any) -> None:
    server, _, store = served
    a = await _call(server, "memory_write", content="alpha fact", scopes=["tols"])
    out = await _call(
        server,
        "memory_admin",
        action="rename_scope",
        old_scope="tols",
        new_scope="tools",
    )
    assert out["old_scope"] == "tols" and out["new_scope"] == "tools"
    assert out["active"] == [a["id"]]
    assert out["tombstoned"] == [] and out["failed"] == []
    assert store.load_one(a["id"]).scopes == ["tools"]


async def test_admin_health_returns_the_report(served: Any) -> None:
    server, _, _ = served
    await _call(server, "memory_write", content="alpha fact", scopes=["tools"])
    report = await _call(server, "memory_admin", action="health")
    assert report["total_active_memories"] == 1
    assert "verification_debt" in report
    assert "recommendations" in report


async def test_admin_conflicts_lists_pending(served: Any) -> None:
    server, _, _ = served
    out = await _call(server, "memory_admin", action="conflicts")
    assert out["pending"] == []
    assert out["pending_total"] == 0
    scanned = await _call(server, "memory_admin", action="conflicts", scan=True)
    assert "scan" in scanned


async def test_admin_acknowledge_miss_needs_id_and_reason(served: Any) -> None:
    server, _, _ = served
    with pytest.raises(Exception, match="needs `id`"):
        await _call(server, "memory_admin", action="acknowledge_miss")
    with pytest.raises(Exception, match="needs `reason`"):
        await _call(server, "memory_admin", action="acknowledge_miss", id="evt_x")


async def test_admin_acknowledge_miss_by_id(served: Any) -> None:
    server, _, _ = served
    out = await _call(
        server,
        "memory_admin",
        action="acknowledge_miss",
        id="evt_missing",
        reason="no such miss, a probe",
    )
    assert out["status"] == "not_found"
    assert out["event_id"] == "evt_missing"


async def test_admin_acknowledge_misses_before_writes_a_cutoff(served: Any) -> None:
    server, state, store = served
    out = await _call(
        server,
        "memory_admin",
        action="acknowledge_miss",
        before="2026-05-25T05:25:35Z",
        reason="the cwd suppression fix landed",
    )
    assert out == {
        "status": "cutoff_recorded",
        "cutoff_ts": "2026-05-25T05:25:35Z",
        "reason": "the cwd suppression fix landed",
    }
    cutoffs = [
        ev for ev in store.iter_events() if ev.get("kind") == "silent_miss_cutoff"
    ]
    assert len(cutoffs) == 1
    assert cutoffs[0]["cutoff_ts"] == "2026-05-25T05:25:35Z"
    assert cutoffs[0]["session_id"] == state.session_id


@pytest.mark.parametrize(
    "before",
    ["2026-05-25T05:25:35", "not a time", "2099-01-01T00:00:00Z"],
)
async def test_admin_acknowledge_misses_before_refuses_bad_cutoffs(
    served: Any, before: str
) -> None:
    server, _, _ = served
    with pytest.raises(Exception, match="`before`"):
        await _call(server, "memory_admin", action="acknowledge_miss", before=before)


async def test_admin_rejects_an_unknown_action(served: Any) -> None:
    server, _, _ = served
    with pytest.raises(Exception):
        await _call(server, "memory_admin", action="curate")
