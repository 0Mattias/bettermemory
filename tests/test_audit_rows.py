"""Audit rows for a `[policy]` refusal and an acknowledged scope mismatch
(ENG-108).

A call `[policy]` refuses for a forbidden flag left no row at all, and
`acknowledge_scope_mismatch` stepped the gate aside without looking, so
the commit row named nothing it waved through and, as a plain write,
was dropped with telemetry off. Both now leave an audit row
(`events.is_audit_row`), recorded whatever `[telemetry] enabled` says
and, like every state row, raised rather than dropped when it cannot
land.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import events, store_events
from bettermemory.config import Config, PolicyConfig, StorageConfig, TelemetryConfig
from bettermemory.events import Recorder
from bettermemory.policy import REFUSAL_STATUS
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool

_CLEAN = "The deploy pipeline pins its formatter version in CI."
_ALPHA = "The alpha service keeps its feature flags in a TOML file."
# Cites the `alpha` project from a memory scoped to `beta`.
_CROSS = "The beta release reuses the alpha flag loader unchanged."

TELEMETRY = pytest.mark.parametrize("telemetry", [True, False], ids=["on", "off"])


class World:
    """One in-process server over one scratch store."""

    def __init__(
        self,
        root: Path,
        *,
        telemetry: bool,
        forbid_force: bool = False,
        forbid_acknowledge: list[str] | None = None,
        managed: frozenset[str] = frozenset(),
    ) -> None:
        config = Config(
            storage=StorageConfig(directory=str(root)),
            policy=PolicyConfig(
                forbid_force=forbid_force,
                forbid_acknowledge=list(forbid_acknowledge or []),
            ),
            telemetry=TelemetryConfig(enabled=telemetry),
            managed_keys=managed,
        )
        self.store = Store(root)
        self.state = SessionState()
        self.server = build_server(
            config=config,
            store=self.store,
            state=self.state,
            recorder=Recorder(
                store=self.store, session_id=self.state.session_id, enabled=telemetry
            ),
        )

    async def call(self, tool: str, **kwargs: Any) -> Any:
        return await call_tool(self.server, tool, kwargs)

    def rows(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.store.iter_events() if e["kind"] == kind]


@contextlib.contextmanager
def _locked(store: Store) -> Iterator[None]:
    holder = sqlite3.connect(store.path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()


# ---------------------------------------------------------------------------
# A [policy] refusal
# ---------------------------------------------------------------------------


@TELEMETRY
async def test_a_policy_refusal_of_a_write_leaves_its_audit_row(
    tmp_path: Path, telemetry: bool
) -> None:
    world = World(
        tmp_path / "store",
        telemetry=telemetry,
        forbid_force=True,
        managed=frozenset({"policy.forbid_force"}),
    )
    with pytest.raises(ToolError, match=REFUSAL_STATUS):
        await world.call("memory_write", content=_CLEAN, scopes=["tools"], force=True)
    assert world.store.load_all() == []
    [row] = world.rows("write")
    assert row["status"] == REFUSAL_STATUS
    assert row["policy_flags"] == ["force"]
    assert row["policy_keys"] == ["policy.forbid_force"]
    assert row["policy_managed"] == ["policy.forbid_force"]
    # No content of the call rides on the trail.
    assert "content" not in row and _CLEAN not in str(row)
    assert events.is_audit_row("write", row)


@TELEMETRY
async def test_a_policy_refusal_of_an_update_leaves_its_audit_row(
    tmp_path: Path, telemetry: bool
) -> None:
    world = World(
        tmp_path / "store", telemetry=telemetry, forbid_acknowledge=["truncation"]
    )
    written = await world.call("memory_write", content=_CLEAN, scopes=["tools"])
    with pytest.raises(ToolError, match=REFUSAL_STATUS):
        await world.call(
            "memory_update",
            id=written["id"],
            content="The deploy pipeline",
            acknowledge_truncation=True,
        )
    [row] = world.rows("update")
    assert row["status"] == REFUSAL_STATUS
    assert row["id"] == written["id"]
    assert row["policy_flags"] == ["acknowledge_truncation"]
    assert row["policy_keys"] == ["policy.forbid_acknowledge"]
    assert row["policy_managed"] == []
    assert world.store.load_one(written["id"]).body.strip() == _CLEAN


async def test_a_policy_refusal_whose_row_meets_a_held_lock_answers_store_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal's row is tried twice against another writer's lock and
    the failure is the answer (retryable: nothing was kept), not a refusal
    whose trail the log never holds."""
    world = World(tmp_path / "store", telemetry=True, forbid_force=True)
    store = world.store
    store.set_busy_timeout(0.05)
    monkeypatch.setattr(store_events, "time", SimpleNamespace(sleep=lambda _s: None))
    real = store.record_event

    def record_event(kind: str, **fields: Any) -> Any:
        if fields.get("status") != REFUSAL_STATUS:
            return real(kind, **fields)
        with _locked(store):
            return real(kind, **fields)

    monkeypatch.setattr(store, "record_event", record_event)
    with pytest.raises(ToolError, match=r"store_busy \(retryable\)"):
        await world.call("memory_write", content=_CLEAN, scopes=["tools"], force=True)
    assert world.rows("write") == []


# ---------------------------------------------------------------------------
# acknowledge_scope_mismatch
# ---------------------------------------------------------------------------


@TELEMETRY
async def test_an_acknowledged_scope_mismatch_leaves_its_audit_row(
    tmp_path: Path, telemetry: bool
) -> None:
    world = World(tmp_path / "store", telemetry=telemetry)
    await world.call("memory_write", content=_ALPHA, scopes=["projects:alpha"])
    refused = await world.call("memory_write", content=_CROSS, scopes=["projects:beta"])
    assert refused["status"] == "scope_mismatch", refused
    committed = await world.call(
        "memory_write",
        content=_CROSS,
        scopes=["projects:beta"],
        acknowledge_scope_mismatch=True,
    )
    assert committed["status"] == "committed", committed
    [row] = [r for r in world.rows("write") if r.get("id") == committed["id"]]
    assert row["status"] == "committed"
    assert row["scope_mismatches_acknowledged"] == ["project_name"]
    assert events.is_audit_row("write", row)
    # The kind of each mismatch, never the text it cited.
    assert "alpha" not in str(row["scope_mismatches_acknowledged"])


@TELEMETRY
async def test_the_flag_on_a_write_with_no_mismatch_is_no_override(
    tmp_path: Path, telemetry: bool
) -> None:
    """A flag that waved nothing through is no override, as with the other
    acknowledge flags: the row is a plain write, state but no audit row,
    whatever telemetry says."""
    world = World(tmp_path / "store", telemetry=telemetry)
    committed = await world.call(
        "memory_write",
        content=_CLEAN,
        scopes=["tools"],
        acknowledge_scope_mismatch=True,
    )
    assert committed["status"] == "committed", committed
    [row] = world.rows("write")
    assert row["scope_mismatches_acknowledged"] == []
    assert not events.is_audit_row("write", row)
