"""The daemon as a process: ``up``, ``status``, ``down`` and the state file.

Every daemon a test starts keeps its state file, its store and its key
under the test's own directory (``BETTERMEMORY_STATE_DIR``,
``BETTERMEMORY_DIR`` and ``BETTERMEMORY_KEYS_DIR``); the detach test
runs on every CI leg, which is how P9's Windows detach is graded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bettermemory import __version__
from bettermemory._daemon_client import (
    health,
    pid_alive,
    read_state,
    state_file_for,
)

from .conftest import shielded_child_env


def _env(tmp_path: Path) -> dict[str, str]:
    env = shielded_child_env()
    env["BETTERMEMORY_STATE_DIR"] = str(tmp_path / "state")
    env["BETTERMEMORY_DIR"] = str(tmp_path / "store")
    env["BETTERMEMORY_KEYS_DIR"] = str(tmp_path / "keys")
    return env


def _cli(
    args: list[str], env: dict[str, str], *, timeout: float = 30.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bettermemory", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _store_path(tmp_path: Path) -> Path:
    return tmp_path / "store" / "memory.sqlite"


def _state_path(tmp_path: Path) -> Path:
    return state_file_for(tmp_path / "state", _store_path(tmp_path))


def _wait_running(tmp_path: Path, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state(tmp_path / "state", _store_path(tmp_path))
        if state is not None and health(state["port"], timeout=0.5) is not None:
            return state
        time.sleep(0.05)
    raise AssertionError(
        f"no daemon answered within {timeout}s; state "
        f"{read_state(tmp_path / 'state', _store_path(tmp_path))!r}"
    )


@pytest.fixture
def daemon_env(tmp_path: Path) -> dict[str, str]:
    env = _env(tmp_path)
    yield env
    # Whatever the test left running goes down with it.
    _cli(["down"], env, timeout=30)


def test_up_writes_the_state_file_and_status_reads_running(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    up = _cli(["up", "--port", "0"], daemon_env)
    assert up.returncode == 0, up.stderr
    state = _wait_running(tmp_path)
    path = _state_path(tmp_path)
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert set(state) >= {"pid", "port", "token", "version", "store", "started"}
    assert state["version"] == __version__
    assert pid_alive(state["pid"])
    assert Path(state["store"]).name == "memory.sqlite"
    assert len(state["token"]) >= 32

    status = _cli(["status"], daemon_env)
    assert status.returncode == 0, status.stderr
    assert "running" in status.stdout
    assert str(state["port"]) in status.stdout

    # `up` twice is one daemon: the second call reports the first.
    again = _cli(["up", "--port", "0"], daemon_env)
    assert again.returncode == 0, again.stderr
    assert read_state(tmp_path / "state", _store_path(tmp_path))["pid"] == state["pid"]


def test_down_stops_the_daemon_and_removes_the_state_file(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    state = _wait_running(tmp_path)
    down = _cli(["down"], daemon_env)
    assert down.returncode == 0, down.stderr
    deadline = time.monotonic() + 10
    while pid_alive(state["pid"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pid_alive(state["pid"])
    assert not _state_path(tmp_path).exists()
    assert health(state["port"], timeout=0.5) is None
    status = _cli(["status"], daemon_env)
    assert status.returncode == 0
    assert "not running" in status.stdout


def test_a_stale_state_file_reads_not_running_and_is_removed(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _state_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 2**22 + 12345,
                "port": 1,
                "token": "x" * 64,
                "version": __version__,
                "store": str(tmp_path / "store" / "memory.sqlite"),
                "started": "2026-01-01T00:00:00Z",
            }
        )
    )
    status = _cli(["status"], daemon_env)
    assert status.returncode == 0, status.stderr
    assert "not running" in status.stdout
    assert not _state_path(tmp_path).exists()


def test_foreground_serves_until_shutdown(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "bettermemory", "up", "--foreground", "--port", "0"],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        state = _wait_running(tmp_path)
        assert state["pid"] == proc.pid
        assert _cli(["down"], daemon_env).returncode == 0
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
