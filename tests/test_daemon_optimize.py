"""The daemon's ``PRAGMA optimize`` (ENG-100): on its schedule, off the
event loop on a connection of its own, and once more at shutdown, on the
daemon's own connection to the store.

The daemon is served in-process on a thread of its own, which opens the
store as the CLI does, so the loop's thread is the connection's, as
`tests/test_daemon_store_watch.py` serves it; the schedule is lowered.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory import daemon as _daemon, store_index
from bettermemory._daemon_client import health, post, read_state
from bettermemory.store import STORE_FILENAME, Store


def _stat1(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_stat1'"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_optimize_analyses_the_store_and_leaves_it_one_every_opener_admits(
    tmp_path: Path, keys_dir: Path
) -> None:
    """The first run on a store with no statistics analyses its tables:
    SQLite writes ``sqlite_stat1``, whose text is SQLite's own, which the
    admission of a store file lets through (`store_schema.admit_store`)."""
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as store:
        for i in range(20):
            store.write(content=f"widget number {i} ships on fridays", scopes=["g"])
        assert store_index.optimize(store, why="test") is True
        assert store.busy_timeout == 5.0
    assert _stat1(path) == 1
    with Store.open(path, keys_dir=keys_dir, allow_rekey=True) as store:
        assert store.log_verify()["status"] == "ok"
    with Store.open(path, read_only=True) as reader:
        assert reader.has_key


def test_optimize_never_waits_for_another_writers_lock(
    tmp_path: Path, keys_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Analysing writes, so it needs the write lock; while another
    connection holds it the run is skipped at once, not after the busy
    timeout, and the connection's timeout is what it was."""
    caplog.set_level(logging.INFO, logger="bettermemory.store")
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as store:
        for i in range(20):
            store.write(content=f"gadget number {i} ships on mondays", scopes=["g"])
        holder = sqlite3.connect(path, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            assert store_index.optimize(store, why="test") is False
            assert time.monotonic() - started < 1.0
            holder.execute("ROLLBACK")
        finally:
            holder.close()
        assert store.busy_timeout == 5.0
        assert store_index.optimize(store, why="test") is True
    assert "PRAGMA optimize (test) skipped: database is locked" in caplog.text


def test_optimize_apart_runs_on_a_connection_of_its_own(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The daemon's scheduled run opens the store again, runs on that
    connection and closes it, leaving the daemon's connection as it was;
    a store that does not open again is skipped and said."""
    caplog.set_level(logging.INFO, logger="bettermemory.store")
    path = tmp_path / STORE_FILENAME
    real = store_index.optimize
    ran_on: list[Store] = []

    def recording(store: Store, *, why: str) -> bool:
        ran_on.append(store)
        return real(store, why=why)

    monkeypatch.setattr(store_index, "optimize", recording)
    with Store.create(path, keys_dir=keys_dir) as store:
        for i in range(20):
            store.write(content=f"sprocket number {i} ships on tuesdays", scopes=["g"])
        assert store_index.optimize_apart(store, "schedule") is True
        assert len(ran_on) == 1
        assert ran_on[0] is not store
        assert store.busy_timeout == 5.0
        assert _stat1(path) == 1
        path.rename(path.with_name("moved.sqlite"))
        assert store_index.optimize_apart(store, "schedule") is False
    assert len(ran_on) == 1
    assert "PRAGMA optimize (schedule) skipped" in caplog.text


def _serve(tmp_path: Path, out: dict[str, Any]) -> threading.Thread:
    """The daemon served on a thread of its own, its store written first;
    `out` gets the store, the thread's name, serve's code, and how many
    runs were recorded when serve returned."""
    directory = tmp_path / "store"
    opened = threading.Event()

    def run() -> None:
        from bettermemory.config import Config, StorageConfig

        config = Config(storage=StorageConfig(directory=str(directory)))
        store = Store(directory, keys_dir=tmp_path / "keys")
        store.write(content="the release runs from the tag", scopes=["global"])
        out["store"] = store
        out["thread"] = threading.current_thread().name
        opened.set()
        try:
            out["code"] = _daemon.serve(
                config=config, store=store, state_dir=tmp_path / "state", port=0
            )
        finally:
            out["closed_after"] = len(out.get("calls", []))
            store.close()

    thread = threading.Thread(target=run, name="daemon-under-test", daemon=True)
    thread.start()
    assert opened.wait(30)
    return thread


def _state(tmp_path: Path, store: Store, *, wait: float = 30) -> dict[str, Any] | None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        state = read_state(tmp_path / "state", store.path)
        if state is not None and health(int(state["port"]), timeout=0.5):
            return state
        time.sleep(0.02)
    return None


def _stop(state: dict[str, Any], thread: threading.Thread) -> None:
    post(
        int(state["port"]),
        str(state["token"]),
        "/api/v1/admin/shutdown",
        {},
        timeout=30,
    )
    thread.join(30)
    assert not thread.is_alive()


def test_the_daemon_optimizes_on_its_schedule_and_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run as the daemon starts serving, then one each
    `OPTIMIZE_EVERY_SECONDS`, each off the loop on a connection of its
    own; the last at shutdown, on the loop's thread and the daemon's own
    connection, after the workers have stopped and while it is open."""
    # Not raising: where the daemon has no schedule, the test says so by
    # the calls it never records.
    monkeypatch.setattr(_daemon, "OPTIMIZE_EVERY_SECONDS", 0.05, raising=False)
    out: dict[str, Any] = {"calls": []}
    calls: list[tuple[str, str, bool, bool]] = out["calls"]
    real = getattr(store_index, "optimize", None)

    def recording(store: Store, *, why: str) -> bool:
        assert real is not None
        ran = real(store, why=why)
        own = store is out.get("store")
        calls.append((why, threading.current_thread().name, ran, own))
        return ran

    monkeypatch.setattr(store_index, "optimize", recording, raising=False)
    thread = _serve(tmp_path, out)
    state = _state(tmp_path, out["store"])
    assert state is not None
    serving = time.monotonic() + 10
    while time.monotonic() < serving and len(calls) < 3:
        time.sleep(0.02)
    scheduled = len(calls)
    _stop(state, thread)
    assert out["code"] == 0
    assert scheduled >= 3, f"{scheduled} runs in 10 s of serving"
    whys = [why for why, _, _, _ in calls]
    assert whys[-1] == "shutdown"
    assert whys.count("shutdown") == 1
    assert set(whys[:-1]) == {"schedule"}
    for why, name, _, own in calls[:-1]:
        assert name != out["thread"], (why, name)
        assert own is False
    assert calls[-1][1] == out["thread"]
    assert calls[-1][3] is True
    assert all(ran for _, _, ran, _ in calls)
    # The shutdown run came before `serve` returned and the store closed.
    assert out["closed_after"] == len(calls)
    assert _stat1(out["store"].path) == 1


def test_the_loop_answers_while_a_scheduled_optimize_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run on a large store counts every table (1.8 s at 963 MB,
    made 2 s here); the loop serves ``/health`` while it runs."""
    monkeypatch.setattr(_daemon, "OPTIMIZE_EVERY_SECONDS", 3600.0, raising=False)
    window: dict[str, float] = {}
    real = store_index.optimize

    def slow(store: Store, *, why: str) -> bool:
        if why == "schedule" and "start" not in window:
            window["start"] = time.monotonic()
            time.sleep(2.0)
            window["end"] = time.monotonic()
        return real(store, why=why)

    monkeypatch.setattr(store_index, "optimize", slow)
    out: dict[str, Any] = {}
    thread = _serve(tmp_path, out)
    answered: list[float] = []
    state: dict[str, Any] | None = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and "end" not in window:
        state = state or read_state(tmp_path / "state", out["store"].path)
        if state is not None and health(int(state["port"]), timeout=0.2):
            answered.append(time.monotonic())
        time.sleep(0.05)
    state = state or _state(tmp_path, out["store"])
    assert state is not None
    _stop(state, thread)
    assert "end" in window
    during = [t for t in answered if window["start"] < t < window["end"]]
    assert len(during) >= 3, (len(answered), window)
