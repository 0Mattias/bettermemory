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
    _PATH_ENVS,
    _spawn,
    health,
    lock_file_for,
    pid_alive,
    read_state,
    release_lock,
    shutdown_daemon,
    state_file_for,
    try_lock,
)
from bettermemory.config import ENV_DIR_OVERRIDE, KEYS_DIR_ENV

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


def test_a_spawned_daemon_runs_in_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_spawn` gives the child the state directory as its working
    directory, so a daemon pins no project checkout (Windows holds a
    process's working directory open against a rename or a delete), and
    hands it the path variables it resolves at startup absolute, since a
    relative one would otherwise resolve against that directory. An
    environment that names no store directory gets the store's, so the
    child never resolves a store of its own where it runs."""
    captured: dict = {}

    class _Child:
        pid = 4242

    def fake_popen(command: list[str], **kwargs: object) -> _Child:
        captured.update(kwargs, command=command)
        return _Child()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)
    env = {"BETTERMEMORY_DIR": "store", "BETTERMEMORY_KEYS_DIR": "keys"}
    started = _spawn(
        Path("state"), tmp_path / "store" / "memory.sqlite", port=None, env=env
    )
    assert started.child.pid == 4242
    state_dir = (tmp_path / "state").resolve()
    assert captured["cwd"] == str(state_dir)
    assert captured["env"]["BETTERMEMORY_STATE_DIR"] == str(state_dir)
    assert captured["env"]["BETTERMEMORY_DIR"] == str((tmp_path / "store").resolve())
    assert captured["env"]["BETTERMEMORY_KEYS_DIR"] == str(
        (tmp_path / "keys").resolve()
    )
    assert "--foreground" in captured["command"]
    assert _PATH_ENVS == (ENV_DIR_OVERRIDE, KEYS_DIR_ENV)
    bare = _spawn(Path("state"), Path("store") / "memory.sqlite", port=None, env={})
    assert bare.child.pid == 4242
    assert captured["env"]["BETTERMEMORY_DIR"] == str((tmp_path / "store").resolve())
    assert "BETTERMEMORY_KEYS_DIR" not in captured["env"]


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


# ---------------------------------------------------------------------------
# `down` returns once the store is released, not when the port closes
# ---------------------------------------------------------------------------

# A daemon's shape as `shutdown_daemon` sees it, in a process of its own:
# it answers /health and the shutdown endpoint, and on the shutdown it
# closes its port first and gives the store's lock back HOLD seconds
# later, the order the real daemon stops in (the server stops accepting,
# the lifespan shutdown closes the store, `serve` gives the lock back).
# argv: the state directory, the store path, HOLD, "lock" or "nolock".
_STOPPING_DAEMON = r"""
import http.server, json, os, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
from bettermemory._daemon_client import lock_file_for, release_lock, try_lock, write_state

state_dir, store_path = Path(sys.argv[1]), sys.argv[2]
hold, mode = float(sys.argv[3]), sys.argv[4]
lock = try_lock(lock_file_for(state_dir, store_path)) if mode == "lock" else None
assert lock is not None or mode == "nolock"
stop = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json(200, {"status": "ok", "version": "0", "store": store_path, "pid": os.getpid()})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._json(200, {})
        stop.set()


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
write_state(state_dir, {
    "pid": os.getpid(), "port": server.server_address[1], "token": "t" * 64,
    "version": "0", "store": store_path,
    "started": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
})
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
stop.wait()
server.shutdown()
server.server_close()  # the port is closed; the lock is still held
time.sleep(hold)
if lock is not None:
    release_lock(lock)
"""


def _stopping_daemon(
    tmp_path: Path, *, hold: float, lock: bool
) -> tuple[subprocess.Popen[str], dict]:
    """The stand-in daemon, answering; its state as the client reads it."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    store_path = _store_path(tmp_path)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _STOPPING_DAEMON,
            str(state_dir),
            str(store_path),
            str(hold),
            "lock" if lock else "nolock",
        ],
        env=shielded_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = read_state(state_dir, store_path)
        if state is not None and health(state["port"], timeout=0.5) is not None:
            return child, state
        if child.poll() is not None:
            _, err = child.communicate(timeout=15)
            raise AssertionError(f"the stand-in exited {child.returncode}: {err}")
        time.sleep(0.02)
    _ended(child)
    raise AssertionError("the stand-in daemon never answered")


def _ended(child: subprocess.Popen[str]) -> None:
    """The stand-in gone and its pipes closed."""
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=15)


def test_down_waits_for_the_stores_lock_not_for_the_port(tmp_path: Path) -> None:
    """`shutdown_daemon` returns once the daemon has given the store's
    lock back, not when its port closes. The real daemon closes its port
    first and its store after, so a `down` that returned on the port left
    the store's files open for a moment, and a rename of the store right
    after it failed on Windows, where an open handle refuses the rename
    (the 9.0.0rc8 release run). The stand-in stops in that order and
    holds the lock for a second after its port has closed: on the old
    predicate `down` returns at once and finds the lock held."""
    child, state = _stopping_daemon(tmp_path, hold=1.0, lock=True)
    try:
        started = time.monotonic()
        assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=15.0)
        elapsed = time.monotonic() - started
        handle = try_lock(lock_file_for(tmp_path / "state", state["store"]))
        assert handle is not None, (
            "down returned while the daemon held the store's lock"
        )
        release_lock(handle)
        assert elapsed >= 0.9
    finally:
        _ended(child)


def test_down_takes_a_daemon_without_a_lock_as_stopped_at_its_port(
    tmp_path: Path,
) -> None:
    """A daemon that holds no lock (one from before the lock, or a zombie
    whose handles the OS dropped) is stopped when its port is closed, as
    before: `down` waits for no lock such a daemon will never give back."""
    child, state = _stopping_daemon(tmp_path, hold=5.0, lock=False)
    try:
        started = time.monotonic()
        assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=15.0)
        assert time.monotonic() - started < 4.0
        assert pid_alive(state["pid"])
    finally:
        _ended(child)


def test_down_returns_with_the_store_released(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """When `down` returns, the daemon's lock is free, its state file is
    gone, and the store's directory can be renamed at once, on Windows
    too, where an open handle in it would refuse the rename."""
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    state = _wait_running(tmp_path)
    assert _cli(["down"], daemon_env).returncode == 0
    handle = try_lock(lock_file_for(tmp_path / "state", state["store"]))
    assert handle is not None, "down returned while the daemon held the store's lock"
    release_lock(handle)
    assert not _state_path(tmp_path).exists()
    store_dir = tmp_path / "store"
    parked = tmp_path / "store-parked"
    store_dir.rename(parked)
    parked.rename(store_dir)


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM is TerminateProcess on Windows"
)
def test_a_sigterm_stops_the_daemon_with_the_store_released(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """A SIGTERM stops a foreground daemon the way `down` does: the
    process exits 0 after closing its store, removing its state file and
    giving the lock back. uvicorn re-raises a captured SIGTERM once it
    has served; without a handler of the daemon's own the process died
    there, its state file left behind and its exit the signal's."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "bettermemory", "up", "--foreground", "--port", "0"],
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        state = _wait_running(tmp_path)
        os.kill(int(state["pid"]), signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=15)
    assert proc.returncode == 0, err
    assert not _state_path(tmp_path).exists()
    handle = try_lock(lock_file_for(tmp_path / "state", state["store"]))
    assert handle is not None, "the daemon exited with the store's lock held"
    release_lock(handle)


def test_the_clients_wait_is_the_daemons_own_bounds() -> None:
    """`down` waits for what the daemon itself allows a stop to take: the
    requests in flight, bounded by the longest bound a handler runs under
    (the git runner's), then the hook workers' join; past that, the
    signal."""
    from bettermemory import daemon, location
    from bettermemory._daemon_client import SHUTDOWN_TIMEOUT_SECONDS

    assert daemon.GRACEFUL_SHUTDOWN_SECONDS == location._GIT_TIMEOUT_SECONDS
    assert SHUTDOWN_TIMEOUT_SECONDS == (
        daemon.GRACEFUL_SHUTDOWN_SECONDS + daemon.WORKERS_CLOSE_SECONDS
    )
