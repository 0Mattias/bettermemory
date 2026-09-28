"""The event window memo behind `Store.events_since(since, memoised=True)`.

`memory_search` reads the telemetry rows of the last
`ATTRIBUTION_LOOKBACK_SECONDS` on every search that returns a hit, and the
uncached read scans the whole log for them: the cut is on `substr(ts, 1,
19)`, which no index serves. The log only grows, one row at a time, each
row chained to the one before it, so the rows of a window whose cut only
moves forward can be kept and extended from the log's head: the memo keeps
the in-window rows up to the head it read, checks that the head row still
carries the MAC it read, and reads only the rows after it. These tests hold
it to the uncached read through appends from this connection and another,
mutation rows, imported rows stamped out of order, a cut moving forward
and back, and a head row rewritten outside the store.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches
from bettermemory import store as store_module
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

T = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _import(store: Store, at: datetime, kind: str = "search", **fields: Any) -> None:
    store.import_event(
        {"ts": _stamp(at), "session": "s1", "kind": kind, **fields},
        imported_from="test",
    )


def _window(store: Store, since: datetime, *, memoised: bool) -> list[dict[str, Any]]:
    return list(store.events_since(since, memoised=memoised))


def _same(store: Store, since: datetime) -> list[dict[str, Any]]:
    """The memoised read, asserted equal to the uncached one."""
    served = _window(store, since, memoised=True)
    assert served == _window(store, since, memoised=False)
    return served


def test_the_memoised_window_is_the_uncached_one_through_a_sequence(
    memory_dir: Path,
) -> None:
    store = Store(memory_dir)
    for minutes in (5, 120, 30, 1):
        _import(store, T - timedelta(minutes=minutes), query=f"q{minutes}")
    cut = T - timedelta(minutes=10)
    first = _same(store, cut)
    assert [e["query"]["preview"] for e in first] == ["q5", "q1"], (
        "premise: the window holds the rows stamped inside it, in log order"
    )

    # Rows after the head: in the window, before it, stamped out of order,
    # a mutation row between them, a use event.
    _import(store, T - timedelta(minutes=3), query="late")
    _import(store, T - timedelta(minutes=20), query="old")
    store.write(content="a memory written between two reads", scopes=["tools"])
    _import(store, T + timedelta(minutes=1), kind="use", ids=["x"], outcome="ignored")
    _same(store, cut)

    # The cut moves forward past rows the memo kept, then back before the
    # rows it dropped.
    _same(store, T - timedelta(minutes=4))
    _same(store, T - timedelta(seconds=30))
    _same(store, T - timedelta(hours=3))
    _same(store, T - timedelta(minutes=10))

    # Rows appended through another connection to the same file.
    other = Store(memory_dir)
    _import(other, T - timedelta(minutes=2), query="other connection")
    other.record_event("search", query="stamped now")
    _same(store, T - timedelta(minutes=10))
    _same(store, T - timedelta(days=400))


def test_a_head_row_rewritten_outside_the_store_starts_the_window_over(
    memory_dir: Path,
) -> None:
    """A tail removed and a row appended at its sequence number: the row
    the memo read as its head no longer carries the MAC it read, so the
    memo reads the log again rather than extend a history that changed."""
    store = Store(memory_dir)
    for minutes in (3, 2, 1):
        _import(store, T - timedelta(minutes=minutes), query=f"q{minutes}")
    cut = T - timedelta(minutes=10)
    _same(store, cut)
    raw = sqlite3.connect(memory_dir / "memory.sqlite")
    try:
        raw.execute("DELETE FROM log WHERE seq = (SELECT MAX(seq) FROM log)")
        raw.commit()
    finally:
        raw.close()
    _import(store, T - timedelta(minutes=1), query="rewritten")
    served = _same(store, cut)
    assert [e["query"]["preview"] for e in served] == ["q3", "q2", "rewritten"]


def test_a_warm_window_reads_only_the_rows_after_its_head(memory_dir: Path) -> None:
    store = Store(memory_dir)
    for minutes in range(200, 0, -1):
        _import(store, T - timedelta(minutes=minutes), query=f"q{minutes}")
    cut = T - timedelta(minutes=10)
    _same(store, cut)
    _import(store, T - timedelta(minutes=1), query="after the head")
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    try:
        served = _window(store, cut, memoised=True)
    finally:
        store._conn.set_trace_callback(None)
    assert served == _window(store, cut, memoised=False)
    assert statements, "premise: the memo reads the log"
    # The uncached read filters the whole table on the stamp; the memo reads
    # the head row by its sequence number and the rows past it.
    assert all("WHERE substr(ts" not in s for s in statements), statements
    assert all("seq" in s for s in statements), statements


def test_the_window_memo_is_bounded_and_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "EVENT_WINDOW_STORES", 2)
    stores = [Store(tmp_path / f"store{i}") for i in range(3)]
    for s in stores:
        _import(s, T - timedelta(minutes=1))
        _same(s, T - timedelta(minutes=10))
    assert [key[0] for key in store_module._EVENT_WINDOWS] == [
        str(s.path) for s in stores[1:]
    ]
    _caches.clear_all()
    assert not store_module._EVENT_WINDOWS


async def test_a_search_after_a_rejection_reads_it_from_the_memoised_window(
    memory_dir: Path,
) -> None:
    """The first search fills the memo; a use event recorded after it lies
    past the memo's head, and the next search annotates its hit with it as
    a search with every cache cleared does."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    store = Store(memory_dir)
    rec = Recorder(store=store, session_id=state.session_id, enabled=True)
    server = build_server(config=cfg, store=store, state=state, recorder=rec)
    written = _unwrap(
        await _mcp_call(
            server,
            "memory_write",
            {"content": "python list comprehension notes", "scopes": ["tools"]},
        )
    )
    before = _unwrap(await _mcp_call(server, "memory_search", {"query": "python"}))
    assert "recent_negative_outcomes" not in before[0]
    await _mcp_call(
        server,
        "memory_record_use",
        {"memory_ids": [written["id"]], "outcome": "ignored", "note": "not now"},
    )
    warm = _unwrap(await _mcp_call(server, "memory_search", {"query": "python"}))
    _caches.clear_all()
    cleared = _unwrap(await _mcp_call(server, "memory_search", {"query": "python"}))
    assert warm[0]["recent_negative_outcomes"] == cleared[0]["recent_negative_outcomes"]
    assert warm[0]["recent_negative_outcomes"][0]["outcome"] == "ignored"


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res
