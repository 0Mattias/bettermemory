"""Tests for memory_rename_scope — the typo/deprecation fix-up tool."""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

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
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


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
    with pytest.raises(Exception, match="invalid scope"):
        await _call(
            server,
            "memory_rename_scope",
            old_scope="Tools",  # uppercase invalid
            new_scope="tooling",
        )


async def test_tool_rejects_old_equals_new(server: Any) -> None:
    with pytest.raises(Exception, match="must differ"):
        await _call(
            server,
            "memory_rename_scope",
            old_scope="tools",
            new_scope="tools",
        )


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
    with pytest.raises(Exception, match="not in the allowed list"):
        await _call(
            server,
            "memory_rename_scope",
            old_scope="tools",
            new_scope="career",
        )


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


# ---------------------------------------------------------------------------
# OSError regression — memory_rename_scope handler must catch a genuine
# disk-level OSError from `store.rename_scope` and re-raise as a structured
# ValueError, mirroring the OSError arms in remove/restore/update/verify.
# ---------------------------------------------------------------------------
#
# Store.rename_scope swallows per-file (ValueError, KeyError,
# FileNotFoundError) — race-losses and malformed files are skipped. A bare
# OSError from a genuine disk failure (EIO mid-write, ENOSPC during the
# atomic rename, EACCES on the unlink, …) inside `_write_path` still
# propagates out, through the previously guard-less handler, and would
# escape the MCP tool boundary as an unstructured error. The fix wraps the
# call in `except OSError -> raise ValueError(... ) from exc` so the
# boundary returns the same clean structured-error shape as every sibling
# lifecycle mutator.


async def test_tool_converts_oserror_to_value_error(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression at the handler boundary. Mock `Store.rename_scope` to
    raise an OSError that simulates a genuine disk failure (e.g. ENOSPC
    during the atomic rename of a touched memory). The handler must
    convert it to a ValueError — NOT leak the bare OSError past the MCP
    `call_tool` boundary.

    Parallel to ``test_remove_handler_converts_oserror_to_value_error``
    in tests/test_server_tombstones.py: we exercise the handler via the
    MCP `call_tool` boundary (which wraps the handler exception in
    `ToolError`) and assert two invariants:

      1. The error reaches the caller — i.e. no swallowing.
      2. Walking the `__cause__` chain, the handler's own exception is a
         `ValueError` whose cause is the original `OSError`. If the
         handler regresses (no `except OSError`), the direct cause of the
         wrapped ToolError would be an `OSError` with no intermediate
         `ValueError` — the assertion fails.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=cfg, store=store, state=SessionState())
    await _call(server, "memory_write", content="x", scopes=["old"])

    def raising_rename_scope(*args: Any, **kwargs: Any) -> Any:
        # Simulate a genuine disk-level failure (not a per-file race —
        # those are swallowed inside Store.rename_scope).
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "rename_scope", raising_rename_scope)

    with pytest.raises(Exception) as excinfo:
        await _call(
            server,
            "memory_rename_scope",
            old_scope="old",
            new_scope="new",
        )
    # Walk the cause chain: handler-emitted ValueError(failed to rename
    # scope ...) → cause = OSError(ENOSPC).
    chain: list[BaseException] = []
    cur: BaseException | None = excinfo.value
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    handler_value_errors = [
        e
        for e in chain
        if isinstance(e, ValueError) and "failed to rename scope" in str(e)
    ]
    assert handler_value_errors, (
        f"regression: handler did not wrap OSError into ValueError. "
        f"Cause chain: {[type(e).__name__ + ': ' + str(e) for e in chain]}"
    )
    underlying = [e for e in chain if isinstance(e, OSError) and e.errno == 28]
    assert underlying, (
        f"Original OSError must be preserved via `from exc` cause chain "
        f"for diagnostics. Cause chain: "
        f"{[type(e).__name__ + ': ' + str(e) for e in chain]}"
    )
