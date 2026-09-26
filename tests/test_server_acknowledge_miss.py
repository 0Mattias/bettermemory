"""Integration tests for `memory_admin(action="acknowledge_miss")`.

Three layers of behaviour:

- **Registration.** The action rides the `memory_admin` tool that
  `build_server` registers.
- **Happy path.** A search_miss event lands in the log; the model
  reads the `event_id` off the health report's `recent_silent_misses`
  and acknowledges it with a reason. The rollup drops the miss; a
  second ack is idempotent.
- **Error shapes.** Unknown event_id, non-search_miss event_id, and
  short-reason rejection.

Each test goes through the public MCP surface (`server.call_tool`)
rather than poking the handler directly so the wire shape stays pinned.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.models import generate_ulid
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Store, SessionState]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    store = Store(memory_dir)
    rec = Recorder(store=store, session_id=state.session_id, enabled=True)
    server = build_server(
        config=cfg,
        store=store,
        state=state,
        recorder=rec,
    )
    return server, store, state


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _events(store: Store) -> list[dict[str, Any]]:
    return list(store.iter_events())


async def _health(server: Any) -> dict[str, Any]:
    return await _call(server, "memory_admin", action="health")


async def _ack(server: Any, event_id: str, reason: str) -> dict[str, Any]:
    return await _call(
        server, "memory_admin", action="acknowledge_miss", id=event_id, reason=reason
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_memory_admin_is_registered(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    server, _, _ = server_with_events
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_admin" in names


# ---------------------------------------------------------------------------
# Happy path — ack drops a miss from the rollup
# ---------------------------------------------------------------------------


async def _emit_search_miss(server: Any, store: Store) -> str:
    """Write a memory, then land the `search_miss` the Stop hook's audit
    would have written for a turn that missed retrieving it, and return
    that event's event_id. The fields mirror `audit.search_miss_fields`,
    the one builder every producer of this event goes through: the
    memory the probe found sits first in `top_hits`, and the probe query
    is redacted by the store the way every recorded query is."""
    written = await _call(
        server,
        "memory_write",
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )
    event_id = generate_ulid()
    store.record_event(
        "search_miss",
        session="sess_hook",
        event_id=event_id,
        session_id="sess_hook",
        threshold_rule="top_score>=0.5",
        lookback_seconds=120,
        recent_retrieval_count=0,
        top_hits=[{"id": written["id"], "score": 0.9, "relevance": "high"}],
        probe_query="backup strategy",
        triggered_from="stop_hook",
    )
    return event_id


async def test_acknowledge_miss_happy_path_drops_miss_from_rollup(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """Emit a search_miss, ack it, recompute health — the miss is
    gone from `miss_total`. Same parity contract the rollup tests
    pin at the unit level, exercised end-to-end through the MCP
    handler."""
    server, store, _ = server_with_events
    await _emit_search_miss(server, store)

    miss_events = [e for e in _events(store) if e["kind"] == "search_miss"]
    assert len(miss_events) == 1
    event_id = miss_events[0]["event_id"]
    assert isinstance(event_id, str) and event_id

    # Pre-ack: the rollup sees the miss.
    pre = await _health(server)
    assert pre["silent_misses"]["miss_total"] == 1
    assert pre["silent_misses"]["unique_miss_memories"] == 1

    # Ack the event.
    res = await _ack(server, event_id, "stopword-heavy probe, no real intent")
    assert res["status"] == "acknowledged"
    assert res["event_id"] == event_id
    assert res["reason"] == "stopword-heavy probe, no real intent"

    # Post-ack: the rollup drops the miss.
    post = await _health(server)
    assert post["silent_misses"]["miss_total"] == 0
    assert post["silent_misses"]["unique_miss_memories"] == 0


async def test_acknowledge_miss_emits_miss_ack_event(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """One `miss_ack` event lands in the log carrying event_id +
    reason."""
    server, store, _ = server_with_events
    await _emit_search_miss(server, store)
    miss_events = [e for e in _events(store) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    await _ack(server, event_id, "false positive against stopword query")

    acks = [e for e in _events(store) if e["kind"] == "miss_ack"]
    assert len(acks) == 1
    assert acks[0]["event_id"] == event_id
    assert acks[0]["reason"] == "false positive against stopword query"


async def test_acknowledge_miss_idempotent_second_call(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """Calling ack twice on the same event_id returns success both
    times AND only emits ONE `miss_ack` event — the handler
    short-circuits on the second call by detecting the existing ack."""
    server, store, _ = server_with_events
    await _emit_search_miss(server, store)
    miss_events = [e for e in _events(store) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    first = await _ack(server, event_id, "stopword-heavy query")
    assert first["status"] == "acknowledged"

    second = await _ack(server, event_id, "repeat ack")
    assert second["status"] == "acknowledged"
    assert second["event_id"] == event_id

    # Only ONE miss_ack event lands — the second call detected the
    # existing ack and short-circuited.
    acks = [e for e in _events(store) if e["kind"] == "miss_ack"]
    assert len(acks) == 1


async def test_acknowledge_miss_surfaces_in_recent_silent_misses_pre_ack(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """Before the ack lands, the health report's `recent_silent_misses`
    carries the event_id so the model can discover it. After the ack
    lands, the entry disappears from the list."""
    server, store, _ = server_with_events
    await _emit_search_miss(server, store)

    pre = await _health(server)
    assert len(pre["recent_silent_misses"]) == 1
    event_id = pre["recent_silent_misses"][0]["event_id"]
    assert isinstance(event_id, str) and event_id

    await _ack(server, event_id, "false positive on stopword")

    post = await _health(server)
    assert post["recent_silent_misses"] == []


# ---------------------------------------------------------------------------
# Error shapes — unknown id, wrong kind, validation
# ---------------------------------------------------------------------------


async def test_acknowledge_miss_returns_not_found_for_unknown_event_id(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """An event_id that doesn't appear anywhere in the event log returns
    the structured `{"status": "not_found", ...}` shape. Distinguishable
    from validation errors (which raise ValueError) by the absence of
    an exception."""
    server, _, _ = server_with_events
    res = await _ack(server, "01JFAKE_NEVER_EXISTED_XX", "testing not-found branch")
    assert res["status"] == "not_found"
    assert res["event_id"] == "01JFAKE_NEVER_EXISTED_XX"
    assert "hint" in res


async def test_acknowledge_miss_wrong_kind_when_id_points_at_non_search_miss(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """An event_id that exists but on a non-search_miss event (e.g.
    a hand-injected `use` event) returns the `wrong_kind` status so
    the caller can diagnose the mismatch."""
    server, store, state = server_with_events
    rec = Recorder(store=store, session_id=state.session_id)
    # Emit a non-search_miss event sharing the event_id field shape.
    rec.record("use", event_id="01JNON_SEARCH_MISS_XYZA", ids=["m"], outcome="applied")

    res = await _ack(server, "01JNON_SEARCH_MISS_XYZA", "diagnose wrong_kind branch")
    assert res["status"] == "wrong_kind"
    assert res["event_id"] == "01JNON_SEARCH_MISS_XYZA"
    assert res["kind"] == "use"


async def test_acknowledge_miss_rejects_short_reason(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """A reason shorter than the minimum length raises ValueError so
    the MCP boundary surfaces a structured error rather than emitting
    an audit-thin `miss_ack`."""
    server, store, _ = server_with_events
    # Pre-emit a search_miss so the failure is on the reason check,
    # not the not-found check.
    await _emit_search_miss(server, store)
    miss_events = [e for e in _events(store) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    with pytest.raises(Exception):  # The SDK wraps ValueError
        await _ack(server, event_id, "ok")  # too short


async def test_acknowledge_miss_rejects_empty_event_id(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """An empty event_id is a validation error — the handler must not
    silently turn it into a `not_found` lookup."""
    server, _, _ = server_with_events
    with pytest.raises(Exception):
        await _ack(server, "", "long enough reason")


async def test_acknowledge_miss_rejects_whitespace_only_reason(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """A whitespace-only reason fails the minimum-length check after
    stripping — protects against `ack(reason="        ")` drive-by."""
    server, store, _ = server_with_events
    await _emit_search_miss(server, store)
    miss_events = [e for e in _events(store) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    with pytest.raises(Exception):
        await _ack(server, event_id, "          ")


async def test_acknowledge_miss_rejects_overlong_reason(
    server_with_events: tuple[Any, Store, SessionState],
) -> None:
    """A clearly-oversized reason is rejected (mirrors the MIN floor) so a
    runaway model or hostile client can't inflate the JSONL event log
    with a multi-megabyte ack reason. The rejection fires before any
    `miss_ack` is emitted — same audit-integrity contract as the
    short-reason check.

    The behavioral core (a 100k-char reason must be refused AND leave no
    miss_ack) is the load-bearing assertion: it fails against the pre-fix
    handler, which had no max bound and wrote the giant reason straight to
    the event log. The boundary block behind the `_MAX_REASON_LENGTH`
    import pins the exact cap (cap+1 rejected, cap accepted)."""
    server, store, _ = server_with_events
    await _emit_search_miss(server, store)
    miss_events = [e for e in _events(store) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    with pytest.raises(Exception):  # The SDK wraps the ValueError
        await _ack(server, event_id, "x" * 100_000)
    acks = [e for e in _events(store) if e["kind"] == "miss_ack"]
    assert acks == [], "an over-cap reason must not emit a miss_ack"

    from bettermemory.handlers.acknowledge_miss import _MAX_REASON_LENGTH

    with pytest.raises(Exception):
        await _ack(
            server,
            event_id,
            "x" * (_MAX_REASON_LENGTH + 1),
        )
    assert [e for e in _events(store) if e["kind"] == "miss_ack"] == []

    res = await _ack(server, event_id, "y" * _MAX_REASON_LENGTH)
    assert res["status"] == "acknowledged"
