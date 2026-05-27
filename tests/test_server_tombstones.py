"""Server tests for the tombstone lifecycle: memory_remove stamps the
session, memory_list_tombstones surfaces the removed records, memory_restore
brings them back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server_with_state(memory_dir: Path) -> tuple[Any, SessionState, Store]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    store = Store(memory_dir)
    return build_server(config=cfg, store=store, state=state), state, store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


async def test_tombstone_tools_registered(server_with_state: Any) -> None:
    server, _, _ = server_with_state
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_list_tombstones" in names
    assert "memory_restore" in names


# ---------------------------------------------------------------------------
# memory_remove now stamps the session
# ---------------------------------------------------------------------------


async def test_remove_writes_session_id_to_tombstone(
    server_with_state: Any,
) -> None:
    server, state, store = server_with_state
    written = await _call(server, "memory_write", content="bad fact", scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="turned out wrong")
    tombstone = store.load_tombstone(written["id"])
    assert tombstone.removed_session == state.session_id
    assert tombstone.removed_reason == "turned out wrong"


# ---------------------------------------------------------------------------
# memory_list_tombstones
# ---------------------------------------------------------------------------


async def test_list_tombstones_returns_removed_records(
    server_with_state: Any,
) -> None:
    server, _, _ = server_with_state
    a = await _call(server, "memory_write", content="alpha", scopes=["tools"])
    b = await _call(server, "memory_write", content="beta", scopes=["infrastructure"])
    await _call(server, "memory_remove", id=a["id"], reason="r")
    await _call(server, "memory_remove", id=b["id"], reason="r")

    raw = await _call(server, "memory_list_tombstones")
    rows = raw.get("result", raw) if isinstance(raw, dict) else raw
    ids = {row["id"] for row in rows}
    assert ids == {a["id"], b["id"]}
    for row in rows:
        assert "removed" in row
        assert "removed_reason" in row
        assert "removed_session" in row
        assert "summary" in row


async def test_list_tombstones_filters_by_scope(
    server_with_state: Any,
) -> None:
    server, _, _ = server_with_state
    a = await _call(server, "memory_write", content="alpha", scopes=["tools"])
    b = await _call(server, "memory_write", content="beta", scopes=["infrastructure"])
    await _call(server, "memory_remove", id=a["id"], reason="r")
    await _call(server, "memory_remove", id=b["id"], reason="r")

    raw = await _call(server, "memory_list_tombstones", scopes=["tools"])
    rows = raw.get("result", raw) if isinstance(raw, dict) else raw
    assert len(rows) == 1
    assert rows[0]["id"] == a["id"]


async def test_list_tombstones_respects_session_disabled_scope(
    server_with_state: Any,
) -> None:
    """A scope disabled via memory_scope_disable should be excluded from
    the tombstones view too — the session-disable signal applies broadly."""
    server, state, _ = server_with_state
    a = await _call(server, "memory_write", content="alpha", scopes=["tools"])
    await _call(server, "memory_remove", id=a["id"], reason="r")
    state.disable("tools")
    raw = await _call(server, "memory_list_tombstones")
    rows = raw.get("result", raw) if isinstance(raw, dict) else raw
    assert rows == []


# ---------------------------------------------------------------------------
# memory_restore
# ---------------------------------------------------------------------------


async def test_restore_brings_removed_memory_back(
    server_with_state: Any,
) -> None:
    server, _, _ = server_with_state
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="oops")
    restored = await _call(server, "memory_restore", id=written["id"])
    assert restored["status"] == "committed"
    assert restored["id"] == written["id"]

    # memory_show now succeeds again.
    shown = await _call(server, "memory_show", id=written["id"])
    assert shown["id"] == written["id"]


async def test_restore_active_id_raises_value_error(
    server_with_state: Any,
) -> None:
    """Active memories aren't tombstones — restore should refuse, with a
    clear error message routing the caller to memory_update if they
    actually wanted to edit."""
    server, _, _ = server_with_state
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    with pytest.raises(Exception, match="active"):
        await _call(server, "memory_restore", id=written["id"])


async def test_restore_unknown_id_raises_value_error(
    server_with_state: Any,
) -> None:
    """An id that's neither active nor tombstoned is a not-found, not a
    silent no-op."""
    from bettermemory.models import generate_ulid

    server, _, _ = server_with_state
    with pytest.raises(Exception, match="no tombstone|not found"):
        await _call(server, "memory_restore", id=generate_ulid())


async def test_restore_emits_event(server_with_state: Any, memory_dir: Path) -> None:
    server, _, _ = server_with_state
    written = await _call(server, "memory_write", content="x", scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="r")
    await _call(server, "memory_restore", id=written["id"])

    events = list(iter_events(memory_dir))
    kinds = [e["kind"] for e in events]
    assert "restore" in kinds
    restore_events = [e for e in events if e["kind"] == "restore"]
    assert restore_events[-1]["id"] == written["id"]


# ---------------------------------------------------------------------------
# W5 regression — memory_remove handler must catch OSError from
# `store.tombstone` and re-raise as a structured ValueError
# ---------------------------------------------------------------------------
#
# Pre-W5 the handler caught only `TombstonedError` / `MemoryNotFoundError`.
# A bare OSError from a genuine disk-level failure (EIO mid-write, ENOSPC
# during the atomic rename, EACCES on the unlink, …) would escape the
# handler and surface as a 500-shaped MCP error rather than the clean
# structured ValueError shape every other failure mode uses. W1 closes
# the most common race path (FileNotFoundError under concurrent
# tombstone) by raising `TombstonedError` inside `Store.tombstone`, but
# the broader gap remains. The fix adds an `except OSError` clause that
# converts the bare error to ValueError with a descriptive message.


async def test_remove_handler_converts_oserror_to_value_error(
    server_with_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W5 regression at the handler boundary. Mock `Store.tombstone`
    to raise an OSError that simulates a genuine disk failure (e.g.
    ENOSPC during the atomic rename). The handler must convert it to
    a ValueError — NOT leak the bare OSError.

    We exercise the handler via the MCP `call_tool` boundary (which
    wraps the handler exception in `ToolError`) and assert two
    invariants:

      1. The error reaches the caller — i.e. no swallowing.
      2. Walking the `__cause__` chain, the handler's own exception
         is a `ValueError` whose cause is the original `OSError`.
         If W5 regresses (handler doesn't catch `OSError`), the
         direct cause of the wrapped ToolError would be an `OSError`
         with no intermediate `ValueError` — the assertion fails.
    """
    server, _, store = server_with_state
    written = await _call(server, "memory_write", content="x", scopes=["tools"])

    def raising_tombstone(*args: Any, **kwargs: Any) -> Any:
        # Simulate a genuine disk-level failure (not a race — those are
        # converted to TombstonedError inside Store.tombstone by W1).
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "tombstone", raising_tombstone)

    with pytest.raises(Exception) as excinfo:
        await _call(server, "memory_remove", id=written["id"], reason="r")
    # Walk the cause chain: handler-emitted ValueError(failed to
    # tombstone ...) → cause = OSError(ENOSPC).
    chain: list[BaseException] = []
    cur: BaseException | None = excinfo.value
    while cur is not None and cur not in chain:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    handler_value_errors = [
        e
        for e in chain
        if isinstance(e, ValueError) and "failed to tombstone" in str(e)
    ]
    assert handler_value_errors, (
        f"W5 regression: handler did not wrap OSError into ValueError. "
        f"Cause chain: {[type(e).__name__ + ': ' + str(e) for e in chain]}"
    )
    underlying = [e for e in chain if isinstance(e, OSError) and e.errno == 28]
    assert underlying, (
        f"Original OSError must be preserved via `from exc` cause chain "
        f"for diagnostics. Cause chain: "
        f"{[type(e).__name__ + ': ' + str(e) for e in chain]}"
    )
