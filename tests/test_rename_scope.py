"""Tests for memory_rename_scope — the typo/deprecation fix-up tool."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


# ---------------------------------------------------------------------------
# Store.rename_scope
# ---------------------------------------------------------------------------


def test_rename_scope_replaces_in_active_memories(store: Store) -> None:
    a = store.write(content="x", scopes=["projct:foo"])
    b = store.write(content="y", scopes=["projct:foo", "tools"])
    c = store.write(content="z", scopes=["tools"])

    result = store.rename_scope("projct:foo", "projects:foo")
    assert set(result["active"]) == {a.id, b.id}
    assert c.id not in result["active"]

    # Disk reflects the rename.
    assert store.load_one(a.id).scopes == ["projects:foo"]
    assert store.load_one(b.id).scopes == ["projects:foo", "tools"]
    assert store.load_one(c.id).scopes == ["tools"]


def test_rename_scope_handles_already_having_new(store: Store) -> None:
    """When a memory already carries the target scope, rename should
    drop the old without duplicating the new."""
    a = store.write(content="x", scopes=["infra", "infrastructure"])
    store.rename_scope("infra", "infrastructure")
    assert store.load_one(a.id).scopes == ["infrastructure"]


def test_rename_scope_preserves_order(store: Store) -> None:
    a = store.write(content="x", scopes=["tools", "old", "career"])
    store.rename_scope("old", "new")
    assert store.load_one(a.id).scopes == ["tools", "new", "career"]


def test_rename_scope_bumps_updated(store: Store) -> None:
    a = store.write(content="x", scopes=["old"])
    original = a.updated
    time.sleep(0.01)
    store.rename_scope("old", "new")
    reloaded = store.load_one(a.id)
    assert reloaded.updated > original


def test_rename_scope_preserves_last_verified_at(store: Store) -> None:
    """Renaming a scope is metadata-only; the body's claims didn't change,
    so the verification timestamp travels."""
    a = store.write(content="x", scopes=["old"])
    verified = store.mark_verified(a.id)
    assert verified.last_verified_at is not None

    store.rename_scope("old", "new")
    reloaded = store.load_one(a.id)
    assert reloaded.last_verified_at == verified.last_verified_at


def test_rename_scope_renames_in_tombstones_by_default(store: Store) -> None:
    a = store.write(content="x", scopes=["old"])
    store.tombstone(a.id, reason="r")

    result = store.rename_scope("old", "new")
    assert result["tombstoned"] == [a.id]
    tombstone = store.load_tombstone(a.id)
    assert tombstone.scopes == ["new"]


def test_rename_scope_skips_tombstones_when_disabled(store: Store) -> None:
    a = store.write(content="x", scopes=["old"])
    store.tombstone(a.id, reason="r")

    result = store.rename_scope("old", "new", include_tombstones=False)
    assert result["tombstoned"] == []
    tombstone = store.load_tombstone(a.id)
    assert tombstone.scopes == ["old"]


def test_rename_scope_no_op_when_scope_absent(store: Store) -> None:
    a = store.write(content="x", scopes=["tools"])
    result = store.rename_scope("nonexistent", "tools")
    assert result == {"active": [], "tombstoned": []}
    # No-op leaves the memory unchanged.
    assert store.load_one(a.id).scopes == ["tools"]


def test_rename_scope_old_equals_new_returns_empty(store: Store) -> None:
    store.write(content="x", scopes=["tools"])
    result = store.rename_scope("tools", "tools")
    assert result == {"active": [], "tombstoned": []}


def test_rename_scope_updates_fts5_index(store: Store) -> None:
    """Regression: a rename must propagate to the FTS5 index. The
    `scopes_text` column feeds BM25 ranking and the scope-LIKE
    pre-filter; without an upsert here the index drifts from disk
    until the next manual `bettermemory reindex`, and search-time
    scope ranking reads against the old name."""
    import sqlite3

    from bettermemory import index as _index

    memory = store.write(content="python tooling notes", scopes=["infra"])
    # Force the index to populate (covers the case where the store's
    # write path didn't upsert because the index file didn't yet exist
    # — `_index_upsert_quietly` does upsert, but this also locks in
    # the precondition explicitly).
    path = next(p for p in store._iter_active_paths() if p.is_file())
    _index.upsert(store.root, memory, filename=path.name)

    result = store.rename_scope("infra", "infrastructure")
    assert memory.id in result["active"]

    db_path = _index.index_path(store.root)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT scopes_text FROM memories WHERE id = ?",
            (memory.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "renamed memory missing from index"
    scopes_text = row[0]
    assert " infrastructure " in scopes_text, (
        f"new scope name not in index after rename: {scopes_text!r}"
    )
    assert " infra " not in scopes_text, (
        f"old scope name still in index after rename: {scopes_text!r}"
    )


def test_rename_scope_restored_memory_searchable_by_new_scope(
    store: Store,
) -> None:
    """Regression chain: restore writes back to the active set; the
    fix to `restore` also upserts the FTS5 index. Combined with the
    rename-updates-index fix above, a tombstone→restore→rename
    sequence ends with a fully-current index entry."""
    import sqlite3

    from bettermemory import index as _index

    memory = store.write(content="kept body", scopes=["alpha"])
    store.tombstone(memory.id, reason="oops")
    restored = store.restore(memory.id)
    assert restored.id == memory.id

    db_path = _index.index_path(store.root)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT scopes_text FROM memories WHERE id = ?",
            (memory.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, (
        "restored memory not re-added to the FTS5 index — search would silently miss it"
    )
    assert " alpha " in row[0]


# ---------------------------------------------------------------------------
# memory_rename_scope MCP tool
# ---------------------------------------------------------------------------


async def test_tool_renames_active(server: Any) -> None:
    a = await _call(server, "memory_write", content="x", scopes=["projct"])
    result = await _call(
        server,
        "memory_rename_scope",
        old_scope="projct",
        new_scope="projects",
    )
    assert result["old_scope"] == "projct"
    assert result["new_scope"] == "projects"
    assert result["active"] == [a["id"]]


async def test_tool_validates_scopes(server: Any) -> None:
    """Invalid scope strings (uppercase, whitespace) should raise."""
    await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_rename_scope",
            old_scope="Tools",  # uppercase invalid
            new_scope="tooling",
        )


async def test_tool_rejects_old_equals_new(server: Any) -> None:
    with pytest.raises(Exception) as excinfo:
        await _call(
            server,
            "memory_rename_scope",
            old_scope="tools",
            new_scope="tools",
        )
    assert "differ" in str(excinfo.value)


async def test_tool_rejects_new_outside_allowed_list(memory_dir: Path) -> None:
    """When `[scopes] allowed` is non-empty, renames into a non-allowed
    scope should fail just like writes do — otherwise rename is an
    end-run around the policy."""
    from bettermemory.config import BehaviorConfig, ScopesConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(allowed=["tools", "infrastructure"]),
    )
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionState())
    await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception) as excinfo:
        await _call(
            server,
            "memory_rename_scope",
            old_scope="tools",
            new_scope="career",
        )
    assert "allowed" in str(excinfo.value)


async def test_tool_records_event(server: Any, memory_dir: Path) -> None:
    from bettermemory.events import iter_events

    await _call(server, "memory_write", content="x", scopes=["old"])
    await _call(
        server,
        "memory_rename_scope",
        old_scope="old",
        new_scope="new",
    )
    events = list(iter_events(memory_dir))
    rename_events = [e for e in events if e["kind"] == "rename_scope"]
    assert len(rename_events) == 1
    assert rename_events[0]["old"] == "old"
    assert rename_events[0]["new"] == "new"
    assert rename_events[0]["active_count"] == 1
