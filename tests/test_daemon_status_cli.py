"""``status --json/--check``, ``up --json`` and ``down --json``, and the rule
that `status` and `down` keep a state file whose daemon may be alive.

Most cases stage the state file by hand (`write_state`) beside a port
that is closed or answered by a stand-in listener, with the store's lock
held by this process or free, and run the commands in-process; one test
drives a real daemon through the subprocess CLI. Every state file, store
and key stays under the test's own directory.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import __version__, _daemon_client
from bettermemory._daemon_client import (
    PROOF_STATE_KEY,
    DaemonExited,
    health_proof,
    lock_file_for,
    read_state,
    release_lock,
    resolved_store_path,
    state_file_for,
    try_lock,
    write_state,
)
from bettermemory.cli import daemon_cmd
from bettermemory.cli.daemon_cmd import (
    CHECK_NOT_ANSWERING,
    CHECK_NOT_OURS,
    CHECK_NOT_RUNNING,
    CHECK_RUNNING,
    daemon_report,
    is_this_daemon,
    lock_held,
    probe,
    uptime_seconds,
)
from bettermemory.server import main as cli_main

from .conftest import shielded_child_env

#: A pid no process has: above every platform's default pid range.
_DEAD_PID = 2**22 + 12345


@pytest.fixture
def where(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(state dir, store path) for the in-process commands."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "store"))
    return state_dir, resolved_store_path()


def _main(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """`bettermemory <argv>` in-process; its exit code (0 when it returned)."""
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        cli_main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stage(state_dir: Path, store: Path, *, pid: int, port: int) -> Path:
    write_state(
        state_dir,
        {
            "pid": pid,
            "port": port,
            "token": "t" * 64,
            "version": __version__,
            "store": str(store),
            "started": "2026-01-01T00:00:00Z",
        },
    )
    return state_file_for(state_dir, store)


@contextmanager
def _held(state_dir: Path, store: Path) -> Iterator[None]:
    """The store's lock, held by this process as a serving daemon holds it."""
    handle = try_lock(lock_file_for(state_dir, store))
    assert handle is not None
    try:
        yield
    finally:
        release_lock(handle)


@contextmanager
def _listener(
    answer: dict[str, Any], *, token: str | None = None
) -> Iterator[tuple[int, list[str]]]:
    """A local HTTP server answering /health with `answer`; the paths of
    the POSTs it received. With `token`, a /health that asks for a proof
    gets the proof of `token` beside `answer`, as a daemon answers it."""
    posts: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _reply(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            nonce = query.get(_daemon_client.HEALTH_NONCE_PARAM)
            if token is not None and nonce:
                self._reply({**answer, "proof": health_proof(token, nonce[0])})
                return
            self._reply(answer)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            posts.append(self.path)
            self._reply({})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), posts
    finally:
        server.shutdown()
        server.server_close()


#: Replies of listeners that are no daemon's /health, each with what
#: `probe` names it.
_NOT_HEALTH = {
    "http-404": (
        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        "HTTP 404",
    ),
    # Not followed off the port.
    "redirect": (
        b"HTTP/1.1 302 Found\r\nLocation: http://192.0.2.1/health\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n",
        "HTTP 302",
    ),
    "not-json": (
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello",
        "a reply that is not JSON",
    ),
    "not-ok": (
        b"HTTP/1.1 200 OK\r\nContent-Length: 22\r\nConnection: close\r\n\r\n"
        b'{"status": "starting"}',
        "a reply that is not a daemon's /health",
    ),
    "not-http": (
        b"SSH-2.0-OpenSSH_9.9\r\n",
        "a reply that is not HTTP (BadStatusLine)",
    ),
}


@contextmanager
def _replying(reply: bytes) -> Iterator[tuple[int, list[bytes]]]:
    """A local TCP listener that answers every connection with `reply`,
    whatever was asked; the first line of each request it received."""
    asked: list[bytes] = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.1)
    done = threading.Event()

    def serve() -> None:
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2.0)
                try:
                    asked.append(conn.recv(65536).split(b"\r\n", 1)[0])
                    conn.sendall(reply)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(server.getsockname()[1]), asked
    finally:
        done.set()
        thread.join(timeout=5)
        server.close()


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def test_this_daemon_is_the_one_that_proves_the_state_files_token() -> None:
    """The proof decides, for a state file written before the proof (no
    `PROOF_STATE_KEY`) as for one written after: a reply that repeats the
    state file's store and pid without the token's MAC over the nonce is
    not this store's daemon (9.0.0rc14; before, a state file without the
    key let the store and the pid decide)."""
    nonce = "0" * 32
    state = {"store": "/s/memory.sqlite", "pid": 7, "token": "t"}
    for written in (state, {**state, PROOF_STATE_KEY: 1}):
        repeated = {"store": "/s/memory.sqlite", "pid": 7}
        assert not is_this_daemon(repeated, written, nonce)
        assert not is_this_daemon({"status": "ok"}, written, nonce)
        answer = {**repeated, "proof": health_proof("t", nonce)}
        assert is_this_daemon(answer, written, nonce)
        assert not is_this_daemon(
            {**repeated, "proof": health_proof("u", nonce)}, written, nonce
        )


def test_uptime_reads_the_state_files_timestamp() -> None:
    now = datetime(2026, 1, 1, 0, 1, 30, tzinfo=timezone.utc)
    assert uptime_seconds("2026-01-01T00:00:00Z", now=now) == 90
    assert uptime_seconds("2026-01-01T00:00:00.5+00:00", now=now) == 89
    # A timestamp without a zone is UTC, as the daemon writes it.
    assert uptime_seconds("2026-01-01T00:00:00", now=now) == 90
    assert uptime_seconds("2026-01-01T00:05:00Z", now=now) == 0
    assert uptime_seconds("yesterday", now=now) is None
    assert uptime_seconds(None, now=now) is None
    assert uptime_seconds("2026-01-01T00:00:00Z") is not None


def test_a_lock_file_that_does_not_exist_reads_free_and_is_not_created(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "daemon-x.lock"
    assert lock_held(path) is False
    assert not path.parent.exists()


def test_a_lock_that_cannot_be_opened_reads_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "daemon-x.lock"
    path.write_bytes(b"")

    def refused(path: Path) -> None:
        raise PermissionError(path)

    monkeypatch.setattr(_daemon_client, "try_lock", refused)
    assert lock_held(path) is None


def test_the_probe_reads_a_daemon_a_stranger_and_silence_apart() -> None:
    """A daemon's /health is the reply; any other answer is named; a port
    that refuses, or accepts and stays silent, is nothing. `health`, which
    the hooks use, reads the last two alike and lets a reply that is not
    HTTP escape as an exception, which `status --check` would have turned
    into exit 1."""
    answer = {"status": "ok", "store": "/s/memory.sqlite", "pid": 7}
    with _listener(answer) as (port, _):
        assert probe(port) == (answer, None)
    for reply, named in _NOT_HEALTH.values():
        with _replying(reply) as (port, asked):
            assert probe(port) == (None, named)
        assert asked == [b"GET /health HTTP/1.1"]
    assert probe(_closed_port()) == (None, None)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as silent:
        # Listening, never accepting: the connection waits in the backlog
        # with no reply, as it does for a daemon too busy to answer.
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        assert probe(int(silent.getsockname()[1]), timeout=0.2) == (None, None)


def test_the_probe_asks_the_port_itself_whatever_proxy_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """urllib sends a request for 127.0.0.1 through ``http_proxy`` when
    ``no_proxy`` does not name it; the probe connects to the port."""
    answer = {"status": "ok", "store": "/s/memory.sqlite", "pid": 7}
    proxy_reply = _NOT_HEALTH["http-404"][0]
    with _replying(proxy_reply) as (proxy, proxied), _listener(answer) as (port, _):
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        for name in ("http_proxy", "HTTP_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy}")
        assert probe(port) == (answer, None)
    assert proxied == []


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_without_a_state_file(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, store = where
    assert _main(["status"], monkeypatch) == 0
    assert capsys.readouterr().out == f"bettermemory daemon: not running for {store}\n"
    assert _main(["status", "--check"], monkeypatch) == CHECK_NOT_RUNNING
    capsys.readouterr()
    assert _main(["status", "--json"], monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "stopped"
    assert report["check"] == CHECK_NOT_RUNNING
    assert report["running"] is False
    assert report["pid"] is None
    assert report["store"] == str(store)
    assert report["client_version"] == __version__


def test_a_stale_state_file_is_removed_and_reads_not_running(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A state file whose process is gone is removed, as `status` always
    removed it, with the same line."""
    state_dir, store = where
    path = _stage(state_dir, store, pid=_DEAD_PID, port=_closed_port())
    assert _main(["status", "--json", "--check"], monkeypatch) == CHECK_NOT_RUNNING
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "stale"
    assert report["state_file_removed"] is True
    assert report["pid_alive"] is False
    assert not path.exists()
    _stage(state_dir, store, pid=_DEAD_PID, port=_closed_port())
    assert _main(["status"], monkeypatch) == 0
    assert capsys.readouterr().out == (
        f"bettermemory daemon: not running for {store} "
        f"(stale state file for pid {_DEAD_PID} removed)\n"
    )


def test_a_silent_daemon_that_holds_the_lock_keeps_its_state_file(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A daemon that is starting, busy or hung does not answer /health but
    holds the store's lock. Removing its state file would leave it serving
    the store unseen, so `status` reports it and keeps the file; the
    default exit stays 0 and --check answers 2."""
    state_dir, store = where
    pid = os.getpid()
    path = _stage(state_dir, store, pid=pid, port=_closed_port())
    before = path.read_bytes()
    with _held(state_dir, store):
        assert _main(["status"], monkeypatch) == 0
        out = capsys.readouterr().out
        assert f"state present, daemon not answering (pid {pid} holds the lock)" in out
        assert _main(["status", "--check"], monkeypatch) == CHECK_NOT_ANSWERING
        capsys.readouterr()
        assert _main(["status", "--json"], monkeypatch) == 0
        report = json.loads(capsys.readouterr().out)
    assert path.read_bytes() == before
    assert report["state"] == "not_answering"
    assert report["check"] == CHECK_NOT_ANSWERING
    assert report["lock_held"] is True
    assert report["pid_alive"] is True
    assert report["state_file_removed"] is False


def test_a_live_pid_whose_lock_cannot_be_checked_keeps_its_state_file(
    where: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir, store = where
    pid = os.getpid()
    path = _stage(state_dir, store, pid=pid, port=_closed_port())
    lock_file_for(state_dir, store).write_bytes(b"")

    def refused(path: Path) -> None:
        raise PermissionError(path)

    monkeypatch.setattr(_daemon_client, "try_lock", refused)
    report = daemon_report(state_dir, store)
    assert report["state"] == "not_answering"
    assert report["lock_held"] is None
    assert "its lock could not be checked" in report["message"]
    assert path.exists()


def test_a_live_pid_that_does_not_hold_the_lock_is_stale(
    where: tuple[Path, Path],
) -> None:
    """A pid that exists but holds no lock is not the daemon: the OS gives
    a dead daemon's lock back, and its pid can be reused. The file goes."""
    state_dir, store = where
    path = _stage(state_dir, store, pid=os.getpid(), port=_closed_port())
    report = daemon_report(state_dir, store)
    assert report["state"] == "stale"
    assert report["check"] == CHECK_NOT_RUNNING
    assert report["lock_held"] is False
    assert not path.exists()
    # The lock file a daemon leaves behind (it is never removed), free:
    # taken for an instant, given back, and the stale file still goes.
    lock = lock_file_for(state_dir, store)
    with _held(state_dir, store):
        pass
    assert lock.exists()
    path = _stage(state_dir, store, pid=os.getpid(), port=_closed_port())
    assert daemon_report(state_dir, store)["lock_held"] is False
    assert not path.exists()
    assert lock_held(lock) is False
    with _held(state_dir, store):
        assert lock_held(lock) is True


def test_a_report_without_tidy_leaves_a_stale_file(where: tuple[Path, Path]) -> None:
    state_dir, store = where
    path = _stage(state_dir, store, pid=_DEAD_PID, port=_closed_port())
    report = daemon_report(state_dir, store, tidy=False)
    assert report["state"] == "stale"
    assert report["state_file_removed"] is False
    assert "left in place" in report["message"]
    assert path.exists()


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "ok", "store": "/elsewhere/memory.sqlite", "pid": 1},
        {"status": "ok"},
    ],
    ids=["another-store", "no-identity"],
)
def test_a_listener_that_is_not_this_daemon_reads_not_ours(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: dict[str, Any],
) -> None:
    """Whatever answers on the state file's port as another store's
    daemon, or as no daemon at all, is not this store's: --check answers
    3. The daemon the file named is gone (its pid is), so the file goes,
    and the hooks will no longer find the listener through it."""
    state_dir, store = where
    with _listener(answer) as (port, _):
        path = _stage(state_dir, store, pid=_DEAD_PID, port=port)
        assert _main(["status", "--json", "--check"], monkeypatch) == CHECK_NOT_OURS
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "not_ours"
    assert report["health_ok"] is False
    assert report["answered"]["store"] == answer.get("store")
    assert report["state_file_removed"] is True
    assert not path.exists()


def test_a_listener_beside_a_daemon_that_holds_the_lock_keeps_the_file(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, store = where
    answer = {"status": "ok", "store": str(store), "pid": _DEAD_PID}
    with _listener(answer) as (port, _), _held(state_dir, store):
        path = _stage(state_dir, store, pid=os.getpid(), port=port)
        assert _main(["status", "--check"], monkeypatch) == CHECK_NOT_OURS
    out = capsys.readouterr().out
    assert "answers for another process" in out
    assert f"its state file is kept (pid {os.getpid()} holds the lock)" in out
    assert path.exists()


def test_a_listener_reply_reaches_the_terminal_without_its_escapes(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whatever listens on the state file's port writes the store and the
    pid its /health reply names. `status` and `down` quote them, and an
    OSC 52 clipboard write or a clear-screen sequence among them must not
    reach the terminal as one."""
    state_dir, store = where
    answer = {
        "status": "ok",
        "store": "/x\x1b]52;c;cHduZWQ=\x07\x1b[2J",
        "pid": "1\x1b[8m",
    }
    with _listener(answer) as (port, _), _held(state_dir, store):
        _stage(state_dir, store, pid=os.getpid(), port=port)
        assert _main(["status"], monkeypatch) == 0
        assert _main(["down"], monkeypatch) == 1
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        assert "answers for another process" in text
        assert "\x1b" not in text and "\x07" not in text


def test_a_listener_that_repeats_the_state_file_says_it_has_no_proof(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A listener that repeats the store and the pid of a state file whose
    daemon offers the proof reads not ours, and the message says why: the
    same store and pid, without the token's proof, not "another process"
    whose identity reads the state file's own."""
    state_dir, store = where
    answer = {"status": "ok", "store": str(store), "pid": os.getpid()}
    with _listener(answer) as (port, _), _held(state_dir, store):
        write_state(
            state_dir,
            {
                "pid": os.getpid(),
                "port": port,
                "token": "t" * 64,
                "version": __version__,
                "store": str(store),
                "started": "2026-01-01T00:00:00Z",
                "health_proof": 1,
            },
        )
        assert _main(["status", "--check"], monkeypatch) == CHECK_NOT_OURS
    out = capsys.readouterr().out
    assert "without the token's proof" in out
    assert "another process" not in out


@pytest.mark.parametrize("kind", sorted(_NOT_HEALTH))
def test_a_listener_that_is_no_daemon_reads_not_ours_without_a_traceback(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """An HTTP error, a body that is not a daemon's /health, or a protocol
    that is not HTTP on the state file's port answers, but not as this
    store's daemon: --check answers 3, not the 1 ("not running") that the
    traceback of a reply that is not HTTP used to exit with. The file
    follows the rule: kept while a live pid holds the lock, removed once
    the named daemon is gone."""
    state_dir, store = where
    reply, named = _NOT_HEALTH[kind]
    with _replying(reply) as (port, _):
        path = _stage(state_dir, store, pid=os.getpid(), port=port)
        with _held(state_dir, store):
            assert _main(["status", "--json", "--check"], monkeypatch) == 3
            report = json.loads(capsys.readouterr().out)
            assert path.exists()
            assert _main(["status"], monkeypatch) == 0
            out = capsys.readouterr().out
        assert report["state"] == "not_ours"
        assert report["check"] == CHECK_NOT_OURS
        assert report["answered"] == {
            "store": None,
            "pid": None,
            "version": None,
            "reply": named,
        }
        assert report["state_file_removed"] is False
        assert f"answers for something that is not a bettermemory daemon ({named})" in (
            out
        )
        assert f"its state file is kept (pid {os.getpid()} holds the lock)" in out
        _stage(state_dir, store, pid=_DEAD_PID, port=port)
        assert _main(["status", "--check"], monkeypatch) == CHECK_NOT_OURS
        # The line a stale state file always printed comes first.
        assert capsys.readouterr().out == (
            f"bettermemory daemon: not running for {store} (stale state file for "
            f"pid {_DEAD_PID} removed); port {port} answers for something that "
            f"is not a bettermemory daemon ({named})\n"
        )
        assert not path.exists()


def test_a_daemon_that_answers_as_itself_reads_running(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, store = where
    pid = os.getpid()
    answer = {
        "status": "ok",
        "store": str(store),
        "pid": pid,
        "version": "9.9.9",
        "store_orphans": ["/s/memory.orphaned-x.sqlite"],
    }
    with _listener(answer, token="t" * 64) as (port, _):
        _stage(state_dir, store, pid=pid, port=port)
        assert _main(["status", "--json", "--check"], monkeypatch) == CHECK_RUNNING
        report = json.loads(capsys.readouterr().out)
        assert _main(["status"], monkeypatch) == 0
        text = capsys.readouterr().out
    assert report["state"] == "running"
    assert report["health_ok"] is True
    assert report["version"] == "9.9.9"
    assert report["port"] == port
    assert report["store_orphans"] == answer["store_orphans"]
    assert report["uptime_seconds"] > 0
    assert "t" * 64 not in json.dumps(report)
    assert text == (
        f"bettermemory daemon 9.9.9 running: pid {pid}, port {port}, store "
        f"{store}, since 2026-01-01T00:00:00Z\n"
    )


# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------


def test_down_without_a_state_file(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, store = where
    assert _main(["down"], monkeypatch) == 0
    assert capsys.readouterr().out == f"bettermemory daemon: not running for {store}\n"
    assert _main(["down", "--json"], monkeypatch) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["result"] == "not_running"
    assert outcome["stopped"] is False


def _no_stop(state: dict[str, Any], *, state_dir: Path, **kw: Any) -> bool:
    return False


def test_down_keeps_the_state_file_of_a_daemon_that_did_not_stop(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A daemon that did not stop and still holds the store's lock keeps
    its state file: without it, every hook would start a daemon that
    exits on the held lock."""
    state_dir, store = where
    pid = os.getpid()
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", _no_stop)
    path = _stage(state_dir, store, pid=pid, port=_closed_port())
    with _held(state_dir, store):
        assert _main(["down"], monkeypatch) == 1
        err = capsys.readouterr().err
        assert _main(["down", "--json"], monkeypatch) == 1
        outcome = json.loads(capsys.readouterr().out)
    assert err == (
        f"bettermemory daemon: pid {pid} did not stop; the state file is kept "
        f"(pid {pid} holds the lock)\n"
    )
    assert path.exists()
    assert outcome["result"] == "did_not_stop"
    assert outcome["state_file_kept"] is True
    assert outcome["lock_held"] is True
    assert outcome["was_running"] is True


def test_down_removes_the_state_file_when_nothing_holds_the_lock(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, store = where
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", _no_stop)
    path = _stage(state_dir, store, pid=os.getpid(), port=_closed_port())
    assert _main(["down"], monkeypatch) == 1
    assert "the state file is removed" in capsys.readouterr().err
    assert not path.exists()


def test_down_reports_a_lock_it_cannot_check(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, store = where
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", _no_stop)
    monkeypatch.setattr(daemon_cmd, "lock_held", lambda path: None)
    path = _stage(state_dir, store, pid=os.getpid(), port=_closed_port())
    assert _main(["down"], monkeypatch) == 1
    assert "its lock could not be checked" in capsys.readouterr().err
    assert path.exists()


def test_down_stops_and_removes_the_state_file(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir, store = where
    port = _closed_port()
    asked: list[int] = []

    def stopped(state: dict[str, Any], *, state_dir: Path, **kw: Any) -> bool:
        asked.append(int(state["pid"]))
        return True

    monkeypatch.setattr(_daemon_client, "shutdown_daemon", stopped)
    path = _stage(state_dir, store, pid=_DEAD_PID, port=port)
    assert _main(["down", "--json"], monkeypatch) == 0
    outcome = json.loads(capsys.readouterr().out)
    assert asked == [_DEAD_PID]
    assert outcome["result"] == "stopped"
    assert outcome["message"] == (
        f"bettermemory daemon: stopped pid {_DEAD_PID} (port {port})"
    )
    assert not path.exists()


def test_down_leaves_a_listener_that_is_not_this_daemon_alone(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`down` posts the token to nothing that does not answer as the
    daemon the state file names, and signals no pid on its say-so. The
    named daemon is gone, so its file is removed and `down` succeeds."""
    state_dir, store = where

    def never(state: dict[str, Any], **kw: Any) -> bool:
        raise AssertionError("shutdown_daemon reached a listener that is not ours")

    monkeypatch.setattr(_daemon_client, "shutdown_daemon", never)
    answer = {"status": "ok", "store": "/elsewhere/memory.sqlite", "pid": 1}
    with _listener(answer) as (port, posts):
        path = _stage(state_dir, store, pid=_DEAD_PID, port=port)
        assert _main(["down", "--json"], monkeypatch) == 0
        outcome = json.loads(capsys.readouterr().out)
        assert posts == []
        assert outcome["result"] == "not_ours"
        assert outcome["state_file_kept"] is False
        assert not path.exists()
        # The same listener beside a live pid that holds the lock: nothing
        # is stopped, the file stays, and `down` fails.
        path = _stage(state_dir, store, pid=os.getpid(), port=port)
        with _held(state_dir, store):
            assert _main(["down"], monkeypatch) == 1
        assert "nothing was stopped" in capsys.readouterr().err
        assert posts == []
        assert path.exists()


def test_down_posts_nothing_to_a_listener_that_is_no_daemon(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reply that is no daemon's /health does not come from the daemon
    the state file names either, so the token is not posted there and no
    pid is signalled: the stale file of a gone daemon is removed, and the
    file of a live pid whose lock cannot be checked is kept, which the
    message says rather than claiming the lock is held."""
    state_dir, store = where

    def never(state: dict[str, Any], **kw: Any) -> bool:
        raise AssertionError("shutdown_daemon reached a listener that is not ours")

    monkeypatch.setattr(_daemon_client, "shutdown_daemon", never)
    reply, named = _NOT_HEALTH["http-404"]
    with _replying(reply) as (port, asked):
        path = _stage(state_dir, store, pid=_DEAD_PID, port=port)
        assert _main(["down", "--json"], monkeypatch) == 0
        outcome = json.loads(capsys.readouterr().out)
        assert outcome["result"] == "not_ours"
        assert outcome["answered"]["reply"] == named
        assert not path.exists()
        path = _stage(state_dir, store, pid=os.getpid(), port=port)
        monkeypatch.setattr(daemon_cmd, "lock_held", lambda path: None)
        assert _main(["down"], monkeypatch) == 1
        err = capsys.readouterr().err
    assert f"answers for something that is not a bettermemory daemon ({named})" in err
    assert (
        "nothing was stopped and the state file is kept "
        f"(pid {os.getpid()} is alive; its lock could not be checked)"
    ) in err
    assert path.exists()
    assert asked and all(
        line.split(b" ")[1].split(b"?")[0] == b"/health" for line in asked
    )


def test_down_says_why_it_kept_the_file_of_a_daemon_that_is_gone(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A gone daemon's file that could not be removed (or names a daemon
    started since) is kept, and the message does not claim a live pid."""
    state_dir, store = where
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", _no_stop)
    monkeypatch.setattr(daemon_cmd, "_remove_stale", lambda *a: False)
    path = _stage(state_dir, store, pid=_DEAD_PID, port=_closed_port())
    assert _main(["down"], monkeypatch) == 1
    assert capsys.readouterr().err == (
        f"bettermemory daemon: pid {_DEAD_PID} did not stop; the state file is "
        f"kept (the daemon of pid {_DEAD_PID} is gone, but the file could not "
        "be removed or names a daemon started since)\n"
    )
    assert path.exists()


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------


def test_up_json_reports_a_daemon_that_exited(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _, store = where
    log = tmp_path / "daemon.log"

    def exited(*args: object, **kw: object) -> None:
        raise DaemonExited(store, 3, "config does not parse", log)

    monkeypatch.setattr(_daemon_client, "ensure_daemon", exited)
    assert _main(["up", "--json"], monkeypatch) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["state"] == "failed"
    assert failure["exit_status"] == 3
    assert failure["log_file"] == str(log)
    assert "config does not parse" in failure["error"]
    assert _main(["up"], monkeypatch) == 1
    assert "config does not parse" in capsys.readouterr().err


def test_up_json_reports_no_daemon(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, store = where
    monkeypatch.setattr(_daemon_client, "ensure_daemon", lambda *a, **kw: None)
    assert _main(["up", "--json"], monkeypatch) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["error"] == f"bettermemory: no daemon could be started for {store}"
    assert failure["exit_status"] is None


def test_up_json_does_not_apply_to_foreground(
    where: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _main(["up", "--foreground", "--json"], monkeypatch) == 2
    assert "--foreground" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# A real daemon, through the CLI
# ---------------------------------------------------------------------------


def _cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bettermemory", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_real_daemon_through_up_status_and_down_json(tmp_path: Path) -> None:
    env = shielded_child_env()
    env["BETTERMEMORY_STATE_DIR"] = str(tmp_path / "state")
    env["BETTERMEMORY_DIR"] = str(tmp_path / "store")
    env["BETTERMEMORY_KEYS_DIR"] = str(tmp_path / "keys")
    try:
        up = _cli(["up", "--port", "0", "--json"], env)
        assert up.returncode == 0, up.stderr
        started = json.loads(up.stdout)
        assert started["state"] == "running"
        assert started["newly_started"] is True
        state = read_state(tmp_path / "state", Path(started["store"]))
        assert state is not None
        assert state["token"] not in up.stdout

        status = _cli(["status", "--json", "--check"], env)
        assert status.returncode == CHECK_RUNNING, status.stderr
        report = json.loads(status.stdout)
        assert state["token"] not in status.stdout
        assert {
            "state",
            "check",
            "running",
            "health_ok",
            "store",
            "state_file",
            "lock_file",
            "log_file",
            "pid",
            "port",
            "version",
            "client_version",
            "started",
            "uptime_seconds",
        } <= set(report)
        assert (report["pid"], report["port"]) == (state["pid"], state["port"])
        assert report["version"] == __version__
        assert report["health_ok"] is True
        assert isinstance(report["uptime_seconds"], int)

        again = _cli(["up", "--port", "0", "--json"], env)
        assert again.returncode == 0, again.stderr
        assert json.loads(again.stdout)["newly_started"] is False

        down = _cli(["down", "--json"], env)
        assert down.returncode == 0, down.stderr
        stopped = json.loads(down.stdout)
        assert stopped["result"] == "stopped"
        assert stopped["pid"] == state["pid"]

        after = _cli(["status", "--check"], env)
        assert after.returncode == CHECK_NOT_RUNNING
        assert "not running" in after.stdout
    finally:
        _cli(["down"], env)


def test_the_default_status_still_exits_zero_whatever_it_finds(
    where: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --check, `status` exits 0 in every state, as it always did
    (tests/test_daemon_lifecycle.py pins the running and stale cases)."""
    state_dir, store = where
    with _listener({"status": "ok"}) as (port, _):
        _stage(state_dir, store, pid=_DEAD_PID, port=port)
        assert _main(["status"], monkeypatch) == 0
    _stage(state_dir, store, pid=os.getpid(), port=_closed_port())
    with _held(state_dir, store):
        assert _main(["status"], monkeypatch) == 0
