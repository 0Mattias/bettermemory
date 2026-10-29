"""The daemon's ``PRAGMA optimize`` (ENG-100): on its schedule, and once
more at shutdown, on the daemon's own connection to the store.

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


def test_the_daemon_optimizes_on_its_schedule_and_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run is on the loop's thread, the thread the daemon's store
    connection serves: the first as it starts serving, then one each
    `OPTIMIZE_EVERY_SECONDS`, and the last at shutdown, after the
    workers have stopped and while the connection is still open."""
    # Not raising: where the daemon has no schedule, the test says so by
    # the calls it never records.
    monkeypatch.setattr(_daemon, "OPTIMIZE_EVERY_SECONDS", 0.05, raising=False)
    calls: list[tuple[str, str, bool]] = []
    real = getattr(store_index, "optimize", None)

    def recording(store: Store, *, why: str) -> bool:
        assert real is not None
        ran = real(store, why=why)
        calls.append((why, threading.current_thread().name, ran))
        return ran

    monkeypatch.setattr(store_index, "optimize", recording, raising=False)
    directory = tmp_path / "store"
    out: dict[str, Any] = {}
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
            out["closed_after"] = len(calls)
            store.close()

    thread = threading.Thread(target=run, name="daemon-under-test", daemon=True)
    thread.start()
    assert opened.wait(30)
    deadline = time.monotonic() + 30
    state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        state = read_state(tmp_path / "state", out["store"].path)
        if state is not None and health(int(state["port"]), timeout=0.5):
            break
        time.sleep(0.02)
    assert state is not None
    serving = time.monotonic() + 10
    while time.monotonic() < serving and len(calls) < 3:
        time.sleep(0.02)
    scheduled = len(calls)
    post(
        int(state["port"]),
        str(state["token"]),
        "/api/v1/admin/shutdown",
        {},
        timeout=30,
    )
    thread.join(30)
    assert not thread.is_alive()
    assert out["code"] == 0
    assert scheduled >= 3, f"{scheduled} runs in 10 s of serving"
    whys = [why for why, _, _ in calls]
    assert whys[-1] == "shutdown"
    assert whys.count("shutdown") == 1
    assert set(whys[:-1]) == {"schedule"}
    assert {name for _, name, _ in calls} == {out["thread"]}
    assert all(ran for _, _, ran in calls)
    # The shutdown run came before `serve` returned and the store closed.
    assert out["closed_after"] == len(calls)
    assert _stat1(directory / STORE_FILENAME) == 1
