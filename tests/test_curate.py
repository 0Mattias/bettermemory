"""Tests for the memory_curate MCP tool.

memory_curate wraps the (separately, thoroughly tested) consolidate engine
behind an in-session, dry-run-by-default tool. These tests pin what the
TOOL adds on top of the engine: the dry-run/apply contract, that dry-run is
side-effect-free, that apply records exactly one `curate` telemetry event,
that its actions are reversible, and input validation. The engine's
dedup/demotion/cold-scope/typo logic itself is covered by test_consolidate.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import iter_events
from bettermemory.models import Memory
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def curate_server(memory_dir: Path) -> Any:
    """A full-surface server — memory_curate is gated behind
    full_tool_surface (see test_tool_surface for the gating itself)."""
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True),
    )
    return build_server(config=cfg, store=Store(memory_dir), state=SessionState())


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _seed_near_duplicates(memory_dir: Path) -> tuple[Memory, Memory]:
    """Two near-duplicate memories (Jaccard > 0.75); `newer` is bumped so
    the keeper picker has a clean signal. Returns (older, newer). Seeded
    via the Store API directly so the write-dedup gate doesn't reject the
    second one — the point is to set up a store that already has a dup."""
    store = Store(memory_dir)
    older = store.write(
        content="the user prefers terse code-driven explanations over prose",
        scopes=["tools"],
    )
    newer = store.write(
        content="the user prefers terse code-driven explanations over long prose",
        scopes=["tools"],
    )
    newer = store.update(newer)
    return older, newer


def _curate_events(memory_dir: Path) -> list[dict[str, Any]]:
    return [e for e in iter_events(memory_dir) if e.get("kind") == "curate"]


async def test_dry_run_is_default_and_previews_without_mutation(
    curate_server: Any, memory_dir: Path
) -> None:
    older, newer = _seed_near_duplicates(memory_dir)

    res = await _call(curate_server, "memory_curate")  # dry_run defaults True

    assert res["dry_run"] is True
    assert res["applied"] is False
    assert res["dedup_candidates"], "the near-dup pair should surface as a candidate"
    assert res["actions_taken"] == []
    # The store is untouched — both memories still present, nothing tombstoned.
    remaining = {m.id for m in Store(memory_dir).load_all()}
    assert remaining == {older.id, newer.id}
    assert Store(memory_dir).load_tombstones() == []
    # A dry run records no telemetry event.
    assert _curate_events(memory_dir) == []


async def test_dry_run_returns_full_report_shape(
    curate_server: Any, memory_dir: Path
) -> None:
    """Even on an otherwise-clean store the preview carries every section
    the engine produces, so a caller can rely on the keys existing."""
    Store(memory_dir).write(content="a lone durable fact", scopes=["tools"])

    res = await _call(curate_server, "memory_curate")

    for key in (
        "dedup_candidates",
        "demotion_candidates",
        "cold_scope_suggestions",
        "scope_typo_pairs",
        "actions_taken",
        "failures",
        "dedup_method",
        "applied",
        "dry_run",
    ):
        assert key in res, f"report missing {key!r}"


async def test_apply_tombstones_duplicate_and_records_one_event(
    curate_server: Any, memory_dir: Path
) -> None:
    older, newer = _seed_near_duplicates(memory_dir)

    res = await _call(curate_server, "memory_curate", dry_run=False)

    assert res["dry_run"] is False
    assert res["applied"] is True
    assert any(a["kind"] == "tombstoned" for a in res["actions_taken"])
    # The older duplicate is tombstoned; the keeper survives.
    surviving = {m.id for m in Store(memory_dir).load_all()}
    assert newer.id in surviving
    assert older.id not in surviving
    # Exactly one rollup telemetry event, counting the tombstone. (The
    # event is recorded only on apply, so its existence already means a
    # non-dry-run pass.)
    events = _curate_events(memory_dir)
    assert len(events) == 1
    assert events[0]["tombstoned"] >= 1
    assert events[0]["dedup_method"] == "jaccard"


async def test_apply_action_is_reversible(curate_server: Any, memory_dir: Path) -> None:
    """memory_curate only tombstones (restorable) — never hard-deletes."""
    older, _ = _seed_near_duplicates(memory_dir)

    await _call(curate_server, "memory_curate", dry_run=False)
    store = Store(memory_dir)
    assert older.id not in {m.id for m in store.load_all()}

    # The tombstone is restorable, proving the action wasn't destructive.
    restored = store.restore(older.id)
    assert restored.id == older.id
    assert older.id in {m.id for m in store.load_all()}


async def test_rejects_nonpositive_window_days(curate_server: Any) -> None:
    with pytest.raises(Exception, match="window_days"):
        await _call(curate_server, "memory_curate", window_days=0)
