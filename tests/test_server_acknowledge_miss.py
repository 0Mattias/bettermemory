"""Integration tests for the `memory_acknowledge_miss` MCP tool — T4.

Three layers of behaviour:

- **Registration / discoverability.** The tool registers via
  `build_server`, the DESC enumerates `event_id`, `reason`, and the
  "ack persists" semantic.
- **Happy path.** A search_miss event lands in the log; the model
  reads the `event_id` off `memory_health.recent_silent_misses` and
  calls `memory_acknowledge_miss(event_id, reason)`. The rollup drops
  the miss; a second ack is idempotent.
- **Error shapes.** Unknown event_id, non-search_miss event_id, and
  short-reason rejection.

Each test goes through the public MCP surface (`server.call_tool`)
rather than poking the handler directly so the wire shape stays pinned.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path, SessionState]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=rec,
    )
    return server, memory_dir, state


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _events(memory_dir: Path) -> list[dict[str, Any]]:
    return list(iter_events(memory_dir))


# ---------------------------------------------------------------------------
# Registration + DESC enumeration
# ---------------------------------------------------------------------------


async def test_memory_acknowledge_miss_is_registered(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    server, _, _ = server_with_events
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "memory_acknowledge_miss" in names


async def test_memory_acknowledge_miss_desc_mentions_key_concepts(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """The DESC string carries `event_id`, `reason`, "ack persists",
    and `recent_silent_misses` so the model has everything it needs
    to discover ids and call the tool correctly. Pinned here so a
    future trim of the description doesn't silently drop the
    discovery cue.
    """
    server, _, _ = server_with_events
    tools = await server.list_tools()
    desc = next(t.description for t in tools if t.name == "memory_acknowledge_miss")
    assert desc is not None
    # Core inputs are named.
    assert "event_id" in desc
    assert "reason" in desc
    # The discovery path is described.
    assert "recent_silent_misses" in desc
    # The persistence semantic is described.
    assert "persists" in desc
    # The boundary to the bulk hatch is described.
    assert "silent_miss_cutoff" in desc or "acknowledge-misses-before" in desc


# ---------------------------------------------------------------------------
# Happy path — ack drops a miss from the rollup
# ---------------------------------------------------------------------------


async def _emit_search_miss(server: Any, memory_dir: Path) -> str:
    """Write a memory, audit a turn that misses retrieving it, return
    the resulting `search_miss` event's event_id. The fixture this
    function lives next to keeps the wire shape honest: the search_miss
    came from the real production path (memory_audit_turn → recorder),
    not a hand-crafted dict. The memory is backdated past probe_for_miss's
    created-time filter (a memory born inside the lookback window cannot
    be miss evidence), preserving the "memory existed before this turn"
    shape the write-then-audit sequence means to exercise — mirrors
    `_backdate_created` in test_audit.py."""
    await _call(
        server,
        "memory_write",
        content="backup strategy uses triangular restic replication",
        scopes=["infrastructure"],
    )
    store = Store(memory_dir)
    backdated = datetime.now(timezone.utc) - timedelta(hours=1)
    for path, mem in store.iter_active():
        store._write_path(
            path,
            mem.model_copy(update={"created": backdated, "updated": backdated}),
        )
    report = await _call(
        server,
        "memory_audit_turn",
        user_message="backup strategy",
    )
    assert report["verdict"] == "miss", "fixture broken: turn should have missed"
    return ""  # caller pulls the id from the event log


async def test_acknowledge_miss_happy_path_drops_miss_from_rollup(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """Emit a search_miss, ack it, recompute health — the miss is
    gone from `miss_total`. Same parity contract the rollup tests
    pin at the unit level, exercised end-to-end through the MCP
    handler."""
    server, memory_dir, _ = server_with_events
    await _emit_search_miss(server, memory_dir)

    miss_events = [e for e in _events(memory_dir) if e["kind"] == "search_miss"]
    assert len(miss_events) == 1
    event_id = miss_events[0]["event_id"]
    assert isinstance(event_id, str) and event_id

    # Pre-ack: the rollup sees the miss.
    pre = await _call(server, "memory_health")
    assert pre["silent_misses"]["miss_total"] == 1
    assert pre["silent_misses"]["unique_miss_memories"] == 1

    # Ack the event.
    res = await _call(
        server,
        "memory_acknowledge_miss",
        event_id=event_id,
        reason="stopword-heavy probe, no real intent",
    )
    assert res["status"] == "acknowledged"
    assert res["event_id"] == event_id
    assert res["reason"] == "stopword-heavy probe, no real intent"

    # Post-ack: the rollup drops the miss.
    post = await _call(server, "memory_health")
    assert post["silent_misses"]["miss_total"] == 0
    assert post["silent_misses"]["unique_miss_memories"] == 0


async def test_acknowledge_miss_emits_miss_ack_event(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """One `miss_ack` event lands in the log carrying event_id +
    reason."""
    server, memory_dir, _ = server_with_events
    await _emit_search_miss(server, memory_dir)
    miss_events = [e for e in _events(memory_dir) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    await _call(
        server,
        "memory_acknowledge_miss",
        event_id=event_id,
        reason="false positive against stopword query",
    )

    acks = [e for e in _events(memory_dir) if e["kind"] == "miss_ack"]
    assert len(acks) == 1
    assert acks[0]["event_id"] == event_id
    assert acks[0]["reason"] == "false positive against stopword query"


async def test_acknowledge_miss_idempotent_second_call(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """Calling ack twice on the same event_id returns success both
    times AND only emits ONE `miss_ack` event — the handler
    short-circuits on the second call by detecting the existing ack."""
    server, memory_dir, _ = server_with_events
    await _emit_search_miss(server, memory_dir)
    miss_events = [e for e in _events(memory_dir) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    first = await _call(
        server,
        "memory_acknowledge_miss",
        event_id=event_id,
        reason="stopword-heavy query",
    )
    assert first["status"] == "acknowledged"

    second = await _call(
        server,
        "memory_acknowledge_miss",
        event_id=event_id,
        reason="repeat ack",
    )
    assert second["status"] == "acknowledged"
    assert second["event_id"] == event_id

    # Only ONE miss_ack event lands — the second call detected the
    # existing ack and short-circuited.
    acks = [e for e in _events(memory_dir) if e["kind"] == "miss_ack"]
    assert len(acks) == 1


async def test_acknowledge_miss_surfaces_in_recent_silent_misses_pre_ack(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """Before the ack lands, `memory_health.recent_silent_misses`
    carries the event_id so the model can discover it. After the ack
    lands, the entry disappears from the list."""
    server, memory_dir, _ = server_with_events
    await _emit_search_miss(server, memory_dir)

    pre = await _call(server, "memory_health")
    assert len(pre["recent_silent_misses"]) == 1
    event_id = pre["recent_silent_misses"][0]["event_id"]
    assert isinstance(event_id, str) and event_id

    await _call(
        server,
        "memory_acknowledge_miss",
        event_id=event_id,
        reason="false positive on stopword",
    )

    post = await _call(server, "memory_health")
    assert post["recent_silent_misses"] == []


# ---------------------------------------------------------------------------
# Error shapes — unknown id, wrong kind, validation
# ---------------------------------------------------------------------------


async def test_acknowledge_miss_returns_not_found_for_unknown_event_id(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """An event_id that doesn't appear in the active log returns the
    structured `{"status": "not_found", ...}` shape. Distinguishable
    from validation errors (which raise ValueError) by the absence of
    an exception."""
    server, _, _ = server_with_events
    res = await _call(
        server,
        "memory_acknowledge_miss",
        event_id="01JFAKE_NEVER_EXISTED_XX",
        reason="testing not-found branch",
    )
    assert res["status"] == "not_found"
    assert res["event_id"] == "01JFAKE_NEVER_EXISTED_XX"
    assert "hint" in res


async def test_acknowledge_miss_wrong_kind_when_id_points_at_non_search_miss(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """An event_id that exists but on a non-search_miss event (e.g.
    a hand-injected `use` event) returns the `wrong_kind` status so
    the caller can diagnose the mismatch."""
    server, memory_dir, state = server_with_events
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    # Emit a non-search_miss event sharing the event_id field shape.
    rec.record("use", event_id="01JNON_SEARCH_MISS_XYZA", ids=["m"], outcome="applied")

    res = await _call(
        server,
        "memory_acknowledge_miss",
        event_id="01JNON_SEARCH_MISS_XYZA",
        reason="diagnose wrong_kind branch",
    )
    assert res["status"] == "wrong_kind"
    assert res["event_id"] == "01JNON_SEARCH_MISS_XYZA"
    assert res["kind"] == "use"


async def test_acknowledge_miss_rejects_short_reason(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """A reason shorter than the minimum length raises ValueError so
    the MCP boundary surfaces a structured error rather than emitting
    an audit-thin `miss_ack`."""
    server, memory_dir, _ = server_with_events
    # Pre-emit a search_miss so the failure is on the reason check,
    # not the not-found check.
    await _emit_search_miss(server, memory_dir)
    miss_events = [
        e for e in _events(server_with_events[1]) if e["kind"] == "search_miss"
    ]
    event_id = miss_events[0]["event_id"]

    with pytest.raises(Exception):  # FastMCP wraps ValueError
        await _call(
            server,
            "memory_acknowledge_miss",
            event_id=event_id,
            reason="ok",  # too short
        )


async def test_acknowledge_miss_rejects_empty_event_id(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """An empty event_id is a validation error — the handler must not
    silently turn it into a `not_found` lookup."""
    server, _, _ = server_with_events
    with pytest.raises(Exception):
        await _call(
            server,
            "memory_acknowledge_miss",
            event_id="",
            reason="long enough reason",
        )


async def test_acknowledge_miss_rejects_whitespace_only_reason(
    server_with_events: tuple[Any, Path, SessionState],
) -> None:
    """A whitespace-only reason fails the minimum-length check after
    stripping — protects against `ack(reason="        ")` drive-by."""
    server, memory_dir, _ = server_with_events
    await _emit_search_miss(server, memory_dir)
    miss_events = [
        e for e in _events(server_with_events[1]) if e["kind"] == "search_miss"
    ]
    event_id = miss_events[0]["event_id"]

    with pytest.raises(Exception):
        await _call(
            server,
            "memory_acknowledge_miss",
            event_id=event_id,
            reason="          ",
        )


async def test_acknowledge_miss_rejects_overlong_reason(
    server_with_events: tuple[Any, Path, SessionState],
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
    server, memory_dir, _ = server_with_events
    await _emit_search_miss(server, memory_dir)
    miss_events = [e for e in _events(memory_dir) if e["kind"] == "search_miss"]
    event_id = miss_events[0]["event_id"]

    with pytest.raises(Exception):  # FastMCP wraps the ValueError
        await _call(
            server,
            "memory_acknowledge_miss",
            event_id=event_id,
            reason="x" * 100_000,
        )
    acks = [e for e in _events(memory_dir) if e["kind"] == "miss_ack"]
    assert acks == [], "an over-cap reason must not emit a miss_ack"

    from bettermemory.handlers.acknowledge_miss import _MAX_REASON_LENGTH

    with pytest.raises(Exception):
        await _call(
            server,
            "memory_acknowledge_miss",
            event_id=event_id,
            reason="x" * (_MAX_REASON_LENGTH + 1),
        )
    assert [e for e in _events(memory_dir) if e["kind"] == "miss_ack"] == []

    res = await _call(
        server,
        "memory_acknowledge_miss",
        event_id=event_id,
        reason="y" * _MAX_REASON_LENGTH,
    )
    assert res["status"] == "acknowledged"
