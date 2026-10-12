"""The daemon stops once the store file it opened is unlinked.

A daemon whose store was removed, or replaced by a rename over its path,
serves an inode nobody can open again, and before the watch it did so
forever (the toolcost bench removed its sandbox under one per run). The
watch reads the link count of a descriptor the daemon holds on the
store file: zero stops the daemon through the shutdown path, and a copy
over the path, which keeps the inode, does not.

The watch itself is tested in-process on a plain file; the daemon is
tested as a process, started by ``up`` under the test's own state
directory, store and keys, on an ephemeral port, with the check
interval lowered in the child by a ``sitecustomize``.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bettermemory import daemon as _daemon
from bettermemory._daemon_client import _stopped, health, pid_alive

from .test_daemon_lifecycle import (
    _cli,
    _env,
    _state_path,
    _store_path,
    _wait_running,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="an open store cannot be unlinked or renamed over on Windows; "
    "the daemon does not watch there",
)

#: The check interval the daemon children run at.
INTERVAL = 1.0
#: How long a daemon may take to exit once its store is unlinked: two
#: check intervals, the bound the watch promises. A shared CI runner
#: gets room for a slow shutdown; the bound there only catches a hang.
EXIT_BOUND = 15.0 if os.environ.get("CI") else 2 * INTERVAL


# ---------------------------------------------------------------------------
# The watch, in-process
# ---------------------------------------------------------------------------


def _watch(path: Path, interval: float) -> tuple[_daemon._StoreWatch, threading.Event]:
    fired = threading.Event()
    watch = _daemon._StoreWatch(path, fired.set, interval=interval)
    assert watch.start()
    return watch, fired


def test_the_watch_fires_when_the_file_is_unlinked(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    watch, fired = _watch(store, 0.05)
    try:
        store.unlink()
        assert fired.wait(2 * 0.05 + 1.0)
    finally:
        watch.stop()


def test_the_watch_fires_when_the_file_is_renamed_over(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    watch, fired = _watch(store, 0.05)
    try:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"another store")
        os.replace(replacement, store)
        assert fired.wait(2 * 0.05 + 1.0)
    finally:
        watch.stop()


def test_the_watch_ignores_a_copy_over_the_file(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    source = tmp_path / "source"
    source.write_bytes(b"copied over")
    watch, fired = _watch(store, 0.05)
    try:
        shutil.copyfile(source, store)
        assert not fired.wait(5 * 0.05)
    finally:
        watch.stop()


def test_a_failing_stat_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an observed link count of zero stops the daemon: a stat that
    fails is logged and checked again on the next tick."""
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    calls = {"n": 0}
    real_fstat = os.fstat

    def failing_fstat(fd: int) -> os.stat_result:
        calls["n"] += 1
        raise OSError("transient")

    watch, fired = _watch(store, 0.05)
    monkeypatch.setattr(_daemon.os, "fstat", failing_fstat)
    try:
        deadline = time.monotonic() + 2.0
        while calls["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls["n"] >= 3
        assert not fired.is_set()
    finally:
        monkeypatch.setattr(_daemon.os, "fstat", real_fstat)
        watch.stop()


def test_stopping_the_watch_does_not_wait_out_its_interval(tmp_path: Path) -> None:
    store = tmp_path / "memory.sqlite"
    store.write_bytes(b"store")
    watch, fired = _watch(store, 30.0)
    started = time.monotonic()
    watch.stop()
    assert time.monotonic() - started < 2.0
    assert not fired.is_set()


# ---------------------------------------------------------------------------
# The daemon, as a process
# ---------------------------------------------------------------------------


def _interval_env(env: dict[str, str], site: Path) -> dict[str, str]:
    """`env` with a ``sitecustomize`` that lowers the daemon's check
    interval in every Python child, the daemon `up` spawns among them."""
    site.mkdir(parents=True, exist_ok=True)
    (site / "sitecustomize.py").write_text(
        f"import bettermemory.daemon as _d\n_d.STORE_WATCH_SECONDS = {INTERVAL!r}\n",
        encoding="utf-8",
    )
    out = dict(env)
    out["PYTHONPATH"] = str(site) + os.pathsep + out.get("PYTHONPATH", "")
    return out


@pytest.fixture
def watched_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    env = _interval_env(_env(tmp_path), tmp_path / "site")
    yield env
    _cli(["down"], env, timeout=30)


def _start(tmp_path: Path, env: dict[str, str]) -> dict:
    up = _cli(["up", "--port", "0"], env)
    assert up.returncode == 0, up.stderr
    return _wait_running(tmp_path)


def _exited_within(state: dict, bound: float) -> float | None:
    """Seconds until the daemon in `state` was gone, None when it outlived
    `bound`. Gone is `_daemon_client._stopped`: the process gone, or its
    port closed, which covers a daemon the shim started and has not
    reaped yet (a zombie still answers ``kill(pid, 0)``)."""
    started = time.monotonic()
    while time.monotonic() - started < bound:
        if _stopped(int(state["pid"]), int(state["port"])):
            return time.monotonic() - started
        time.sleep(0.02)
    return None


def _process_gone(pid: int, bound: float = 10.0) -> bool:
    """Whether `pid` is gone within `bound`: the daemon's last act, after
    its port closed, is removing its state file on the way out. A daemon
    `up` started is reaped by init once it exits, never left a zombie."""
    deadline = time.monotonic() + bound
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def test_the_daemon_exits_when_its_store_is_unlinked(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    state = _start(tmp_path, watched_env)
    _store_path(tmp_path).unlink()
    took = _exited_within(state, EXIT_BOUND)
    assert took is not None, f"the daemon outlived its store by {EXIT_BOUND} s"
    # The normal shutdown ran: the state file is gone with the daemon.
    assert _process_gone(state["pid"])
    assert not _state_path(tmp_path).exists()
    log = _state_path(tmp_path).with_suffix(".log").read_text(encoding="utf-8")
    assert log.count("was removed or replaced") == 1


def test_the_daemon_exits_when_its_store_is_renamed_over(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    state = _start(tmp_path, watched_env)
    replacement = tmp_path / "replacement.sqlite"
    replacement.write_bytes(b"not the store the daemon opened")
    os.replace(replacement, _store_path(tmp_path))
    assert _exited_within(state, EXIT_BOUND) is not None
    assert _process_gone(state["pid"])
    assert not _state_path(tmp_path).exists()


def test_the_daemon_stays_up_through_a_copy_over_its_store(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    state = _start(tmp_path, watched_env)
    store = _store_path(tmp_path)
    backup = tmp_path / "backup.sqlite"
    shutil.copyfile(store, backup)
    shutil.copyfile(backup, store)
    # More than two check intervals.
    time.sleep(3 * INTERVAL)
    assert pid_alive(state["pid"])
    assert health(state["port"]) is not None
    # And the normal shutdown still works with the watch running.
    down = _cli(["down"], watched_env)
    assert down.returncode == 0, down.stderr
    assert _exited_within(state, 10.0) is not None
    assert _process_gone(state["pid"])
    assert not _state_path(tmp_path).exists()


def test_a_normal_shutdown_is_not_held_by_the_watch(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    """At the shipped 30 s interval, `down` must not wait out a tick."""
    env = _env(tmp_path)  # no sitecustomize: the shipped interval
    state = _start(tmp_path, env)
    started = time.monotonic()
    down = _cli(["down"], env)
    assert down.returncode == 0, down.stderr
    assert _exited_within(state, 10.0) is not None
    assert _process_gone(state["pid"])
    assert time.monotonic() - started < 10.0
    assert not _state_path(tmp_path).exists()


async def test_an_open_shim_restarts_a_daemon_that_stopped_this_way(
    tmp_path: Path, watched_env: dict[str, str]
) -> None:
    """The shim follows its daemon through a restart: once a daemon stops
    because its store was removed, the shim's next call finds no daemon
    listening, starts one and answers."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "bettermemory"], env=watched_env
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.list_tools()
            first = _wait_running(tmp_path)
            _store_path(tmp_path).unlink()
            assert _exited_within(first, EXIT_BOUND) is not None
            answered = await session.call_tool(
                "memory_search", {"query": "anything at all"}
            )
            assert not answered.is_error
            second = _wait_running(tmp_path)
            assert second["pid"] != first["pid"]
