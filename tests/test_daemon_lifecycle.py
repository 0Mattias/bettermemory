"""The daemon as a process: ``up``, ``status``, ``down`` and the state file.

Every daemon a test starts keeps its state file, its store and its key
under the test's own directory (``BETTERMEMORY_STATE_DIR``,
``BETTERMEMORY_DIR`` and ``BETTERMEMORY_KEYS_DIR``); the detach test
runs on every CI leg, which is how P9's Windows detach is graded.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
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
def daemon_env(tmp_path: Path) -> Iterator[dict[str, str]]:
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
    again_state = read_state(tmp_path / "state", _store_path(tmp_path))
    assert again_state is not None
    assert again_state["pid"] == state["pid"]


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


# A launcher in front of the interpreter, as a Windows venv's python.exe
# is: it runs the interpreter as its child, waits, and exits with its code.
_LAUNCHER = (
    "import subprocess, sys; sys.exit(subprocess.call([sys.executable, *sys.argv[1:]]))"
)


@pytest.mark.parametrize("launcher", [False, True], ids=["direct", "launcher"])
def test_foreground_serves_until_shutdown(
    tmp_path: Path, daemon_env: dict[str, str], launcher: bool
) -> None:
    """`up --foreground` serves until `down` stops it, and the process
    started exits 0 then. The state file names the process that serves,
    which /health confirms by its pid; with a launcher in front, as on the
    windows-latest leg, that is the launcher's child, not the process
    started."""
    command = ["-m", "bettermemory", "up", "--foreground", "--port", "0"]
    if launcher:
        command = ["-c", _LAUNCHER, *command]
    proc = subprocess.Popen(
        [sys.executable, *command],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        state = _wait_running(tmp_path)
        answer = health(state["port"])
        assert answer is not None and answer["pid"] == state["pid"]
        assert pid_alive(state["pid"])
        if launcher:
            assert state["pid"] != proc.pid
        assert proc.poll() is None
        assert _cli(["down"], daemon_env).returncode == 0
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert not pid_alive(state["pid"])
    assert not _state_path(tmp_path).exists()


# The line a daemon logs once it has bound its port and written its state
# file (`daemon.serve`).
_STARTED = re.compile(r"bettermemory daemon \S+ on ")


# A client of its own: waits for the go file, so the clients reach
# `ensure_daemon` together, then prints the pid and port it got.
_ENSURE_CLIENT = """
import json, sys, time
from pathlib import Path
from bettermemory._daemon_client import (
    daemon_env_for, ensure_daemon, package_version, resolve_state_dir,
    resolved_store_path,
)
go = Path(sys.argv[1])
while not go.exists():
    time.sleep(0.002)
store = resolved_store_path()
state = ensure_daemon(
    resolve_state_dir(), store, version=package_version(), port=0,
    env=daemon_env_for(store), timeout=30.0,
)
print(json.dumps(None if state is None else [state["pid"], state["port"]]))
"""


def test_concurrent_clients_on_a_fresh_store_start_one_daemon(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """Six processes that each find no daemon and call `ensure_daemon` at
    once end with one daemon for the store: every caller got the same pid
    and port, the state file names it, /health answers with it, and the
    log the clients' daemons share holds one daemon's start line once any
    daemon that lost the lock has had its grace to leave."""
    from bettermemory._daemon_client import LOCK_WAIT_SECONDS, log_file_for

    go = tmp_path / "go"
    clients = [
        subprocess.Popen(
            [sys.executable, "-c", _ENSURE_CLIENT, str(go)],
            env=daemon_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    pids: set[int] = set()
    try:
        time.sleep(1.0)
        go.write_text("go")
        answers = []
        for client in clients:
            out, err = client.communicate(timeout=60)
            assert client.returncode == 0, err
            answers.append(json.loads(out))
        pids = {answer[0] for answer in answers if answer is not None}
        assert None not in answers, answers
        assert len({tuple(answer) for answer in answers}) == 1, answers
        pid, port = answers[0]
        state = read_state(tmp_path / "state", _store_path(tmp_path))
        assert state is not None and (state["pid"], state["port"]) == (pid, port)
        answer = health(port)
        assert answer is not None and answer["pid"] == pid
        time.sleep(LOCK_WAIT_SECONDS + 1.0)
        log = log_file_for(tmp_path / "state", _store_path(tmp_path)).read_text(
            encoding="utf-8"
        )
        assert len(_STARTED.findall(log)) == 1, log
        assert read_state(tmp_path / "state", _store_path(tmp_path)) == state
    finally:
        for client in clients:
            if client.poll() is None:
                client.kill()
        # The fixture's `down` stops the daemon the state file names; a pid
        # a client reported that the file does not name is stopped here.
        state = read_state(tmp_path / "state", _store_path(tmp_path))
        for pid in pids - ({state["pid"]} if state else set()):
            if pid_alive(pid):
                os.kill(pid, signal.SIGTERM)


def test_a_second_foreground_daemon_on_a_served_store_exits(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """`up --foreground` on a store a daemon already serves takes no port
    and writes no state file: it waits out the lock's grace, logs one line
    naming the store, and exits with `DAEMON_HELD_STATUS`, while the first
    daemon keeps serving under its own state file."""
    from bettermemory._daemon_client import DAEMON_HELD_STATUS

    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    state = _wait_running(tmp_path)
    before = _state_path(tmp_path).read_bytes()
    second = _cli(["up", "--foreground", "--port", "0"], daemon_env)
    assert second.returncode == DAEMON_HELD_STATUS, second.stderr
    lines = [line for line in second.stderr.splitlines() if "already serves" in line]
    assert len(lines) == 1, second.stderr
    assert "memory.sqlite" in lines[0]
    assert not _STARTED.search(second.stderr), second.stderr
    assert _state_path(tmp_path).read_bytes() == before
    answer = health(state["port"])
    assert answer is not None and answer["pid"] == state["pid"]


def test_the_wait_goes_on_when_the_started_child_found_the_store_held(
    tmp_path: Path,
) -> None:
    """A child that exits with `DAEMON_HELD_STATUS` lost the store's lock to
    a daemon that is starting or busy: `wait_for_daemon` keeps polling for
    that daemon rather than raising `DaemonExited`, and returns None when
    none answers within the timeout."""
    from bettermemory._daemon_client import (
        DAEMON_HELD_STATUS,
        Started,
        log_file_for,
        wait_for_daemon,
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store_path = _store_path(tmp_path)
    log_path = log_file_for(state_dir, store_path)
    with log_path.open("ab") as log:
        child = subprocess.Popen(
            [sys.executable, "-c", f"import sys; sys.exit({DAEMON_HELD_STATUS})"],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=shielded_child_env(),
        )
    child.wait(timeout=30)
    started = time.monotonic()
    answered = wait_for_daemon(
        state_dir,
        store_path,
        timeout=0.5,
        started=Started(child=child, log=log_path, log_offset=0),
    )
    assert answered is None
    assert time.monotonic() - started >= 0.5
