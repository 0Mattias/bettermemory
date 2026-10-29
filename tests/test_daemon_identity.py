"""A client trusts only its own daemon: the ``/health`` proof, no reply
without one taken for the daemon (the pre-rc12 fallback is gone since
9.0.0rc14), the listener that is not ours, the signal that needs
evidence, and the owner-only state directory and log.

The state file outlives a daemon that was killed or crashed, and a
127.0.0.1 port is any account's to bind. Before 9.0.0rc12 a client took
anything answering ``{"status": "ok"}`` on the file's port for its
daemon: it posted the hook payloads (the prompt, the cwd, the transcript
path) there and printed the reply into the model's context, and `down`
signalled whatever process had inherited the file's pid.
"""

from __future__ import annotations

import http.server
import io
import json
import logging
import os
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from bettermemory import _daemon_client
from bettermemory._daemon_client import (
    HEALTH_NONCE_PARAM,
    PROOF_STATE_KEY,
    Identified,
    health,
    health_proof,
    identify,
    lock_file_for,
    package_version,
    post,
    proves,
    read_state,
    release_lock,
    shutdown_daemon,
    try_lock,
    valid_nonce,
    write_state,
)

from .test_daemon_lifecycle import _cli, _env, _store_path, _wait_running

TOKEN = "a" * 64
DEAD_PID = 2**22 + 4323


def _state(port: int, store: Path | str, *, pid: int = DEAD_PID) -> dict[str, Any]:
    return {
        "pid": pid,
        "port": port,
        "token": TOKEN,
        "version": package_version(),
        "store": str(store),
        "started": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# The proof, and no reply without one
# ---------------------------------------------------------------------------


NONCE = "0123456789abcdef" * 2
STATE = _state(1, "/stores/memory.sqlite", pid=4242)


def test_the_proof_verifies_and_a_wrong_token_or_nonce_fails_it() -> None:
    proof = health_proof(TOKEN, NONCE)
    assert len(proof) == 64
    assert proves(STATE, {"status": "ok", "proof": proof}, NONCE)
    assert not proves(
        STATE, {"status": "ok", "proof": health_proof("b" * 64, NONCE)}, NONCE
    )
    assert not proves(
        STATE, {"status": "ok", "proof": health_proof(TOKEN, "f" * 32)}, NONCE
    )
    assert not proves(STATE, {"status": "ok", "proof": proof.upper()}, NONCE)


@pytest.mark.parametrize("proof", [None, 1, ["x"], "", "é" * 64, "\x00" * 64])
def test_a_proof_of_another_shape_fails_without_raising(proof: object) -> None:
    assert not proves(STATE, {"status": "ok", "proof": proof}, NONCE)


def test_a_wrong_proof_decides_alone() -> None:
    """A listener that reports the state file's pid and store but cannot
    prove the token is not ours."""
    answer = {
        "status": "ok",
        "pid": STATE["pid"],
        "store": STATE["store"],
        "proof": health_proof("b" * 64, NONCE),
    }
    assert not proves(STATE, answer, NONCE)


@pytest.mark.parametrize(
    "answer",
    [
        {"pid": 4242, "store": "/stores/memory.sqlite"},
        {"pid": 4242, "store": "/stores/memory.sqlite", "version": "9.0.0rc11"},
        {"pid": 4243, "store": "/stores/memory.sqlite"},
        {"pid": 4242},
        {},
    ],
    ids=["pid-and-store", "rc11", "another-pid", "pid-only", "bare"],
)
@pytest.mark.parametrize("keyed", [False, True], ids=["pre-rc12-file", "proving-file"])
def test_a_reply_without_a_proof_is_never_ours(
    answer: dict[str, Any], keyed: bool
) -> None:
    """ENG-102. ``/health`` answers the pid and the store to any process on
    the host, so a reply without a proof is never the daemon's, whatever
    it repeats and whatever daemon wrote the state file. Before 9.0.0rc14
    a state file without `PROOF_STATE_KEY` (one an rc1 to rc11 daemon
    wrote) let a reply that repeated its pid and store pass for that
    daemon, so a listener that had read both off an open ``/health``
    before the crash was posted the hook payloads and the token. The same
    reply with the proof of the token is ours."""
    state = {**STATE, PROOF_STATE_KEY: 1} if keyed else STATE
    assert not proves(state, {"status": "ok", **answer}, NONCE)
    proven = {"status": "ok", **answer, "proof": health_proof(TOKEN, NONCE)}
    assert proves(state, proven, NONCE)


def test_valid_nonces() -> None:
    assert valid_nonce(NONCE)
    assert valid_nonce("A" * 128)
    for bad in (None, 7, "", "0" * 31, "0" * 129, "z" * 32, NONCE + "\n"):
        assert not valid_nonce(bad), bad


# ---------------------------------------------------------------------------
# Listeners: ours, and the ones that are not (a pre-rc12 daemon among them)
# ---------------------------------------------------------------------------


class _Listener(http.server.ThreadingHTTPServer):
    """Something answering on a port: /health answers `reply(nonce)`, and
    every request is recorded, its body with it."""

    def __init__(self, reply: Any) -> None:
        super().__init__(("127.0.0.1", 0), _ListenerHandler)
        self.reply = reply
        self.gets: list[str] = []
        self.posts: list[tuple[str, bytes]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _ListenerHandler(http.server.BaseHTTPRequestHandler):
    # Keep-alive, as uvicorn serves the daemon: `post` asks for the proof
    # on the connection its request then goes down.
    protocol_version = "HTTP/1.1"

    @property
    def listener(self) -> _Listener:
        return cast(_Listener, self.server)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _json(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.listener.gets.append(self.path)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        nonce = query.get(HEALTH_NONCE_PARAM, [None])[0]
        self._json(self.listener.reply(nonce))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.listener.posts.append((self.path, self.rfile.read(length)))
        self._json({"stdout": "INJECTED CONTEXT"})


@pytest.fixture
def listeners() -> Iterator[list[_Listener]]:
    made: list[_Listener] = []
    yield made
    for listener in made:
        listener.stop()


def _listen(made: list[_Listener], reply: Any) -> _Listener:
    listener = _Listener(reply)
    made.append(listener)
    return listener


def _another_store(version: str = "99.0.0") -> Any:
    """A listener that answers ok for another store, newer than us: the
    reply that kept it under the old rule."""
    return lambda nonce: {
        "status": "ok",
        "version": version,
        "store": "/elsewhere/memory.sqlite",
        "pid": 1,
    }


def _proving(token: str, store: Path, pid: int = DEAD_PID) -> Any:
    def reply(nonce: str | None) -> dict[str, Any]:
        answer: dict[str, Any] = {
            "status": "ok",
            "version": package_version(),
            "store": str(store),
            "pid": pid,
        }
        if nonce is not None:
            answer["proof"] = health_proof(token, nonce)
        return answer

    return reply


def _pre_rc12(store: Path, pid: int = DEAD_PID) -> Any:
    return lambda nonce: {
        "status": "ok",
        "version": "9.0.0rc11",
        "store": str(store),
        "pid": pid,
    }


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _BannerHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.recv(4096)
        self.request.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")


@pytest.fixture
def stranger() -> Iterator[int]:
    """The port of a listener that does not speak HTTP: it answers any
    request with another protocol's banner and closes. Its reply fails in
    `http.client` as a status line that is not one."""
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _BannerHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_a_listener_that_does_not_speak_http_answers_nothing(stranger: int) -> None:
    """Not a daemon's reply, and no exception out of discovery either: the
    client goes on to start a daemon beside it, as for a silent port."""
    assert health(stranger) is None
    assert identify(_state(stranger, "/x/memory.sqlite")) == Identified(None, False)


def test_identify_takes_a_daemon_that_proves_the_token(
    tmp_path: Path, listeners: list[_Listener]
) -> None:
    store = tmp_path / "memory.sqlite"
    listener = _listen(listeners, _proving(TOKEN, store))
    found = identify(_state(listener.port, store))
    assert found.ours and not found.foreign
    assert found.answer is not None and "proof" in found.answer
    second = identify(_state(listener.port, store))
    assert second.ours
    nonces = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)[HEALTH_NONCE_PARAM][0]
        for path in listener.gets
    ]
    assert len(nonces) == 2 and nonces[0] != nonces[1]
    assert all(valid_nonce(nonce) and len(nonce) == 32 for nonce in nonces)


def test_identify_refuses_a_daemon_that_holds_another_token(
    tmp_path: Path, listeners: list[_Listener]
) -> None:
    """Two stores' daemons after a crash: one came up on the port the
    other's state file names. It proves its own token, not this file's."""
    store = tmp_path / "memory.sqlite"
    listener = _listen(listeners, _proving("b" * 64, store))
    found = identify(_state(listener.port, store))
    assert found.foreign and not found.ours


def test_identify_with_nothing_listening() -> None:
    found = identify(_state(_closed_port(), "/x/memory.sqlite"))
    assert found == Identified(None, False)
    assert not found.foreign


def test_health_without_a_nonce_sends_none(listeners: list[_Listener]) -> None:
    listener = _listen(listeners, _another_store())
    assert health(listener.port) is not None
    assert listener.gets == ["/health"]


def test_a_pre_rc12_daemon_of_this_store_is_not_taken(
    tmp_path: Path, listeners: list[_Listener]
) -> None:
    """A reply in the shape of a daemon from before the proof, at the port
    of a state file without `PROOF_STATE_KEY`, repeating that file's pid
    and store: what the pre-rc12 fallback took for the daemon, and what
    any listener that read an open ``/health`` can send. Since 9.0.0rc14
    it is another listener."""
    store = tmp_path / "memory.sqlite"
    listener = _listen(listeners, _pre_rc12(store))
    write_state(tmp_path / "state", _state(listener.port, store))
    assert _daemon_client._answering(tmp_path / "state", store) is None
    assert identify(_state(listener.port, store)).foreign


def test_ensure_daemon_never_uses_a_listener_that_is_not_ours(
    tmp_path: Path, listeners: list[_Listener], caplog: pytest.LogCaptureFixture
) -> None:
    """The listener answers ok, for another store, at a newer version, the
    reply the old rule kept. It is neither kept, nor asked to stop, nor
    posted anything, and the warning names the state file's daemon."""
    store = tmp_path / "memory.sqlite"
    state_dir = tmp_path / "state"
    listener = _listen(listeners, _another_store())
    write_state(state_dir, _state(listener.port, store))
    caplog.set_level(logging.WARNING, logger="bettermemory._daemon_client")
    assert (
        _daemon_client.ensure_daemon(
            state_dir, store, version=package_version(), start=False
        )
        is None
    )
    assert listener.posts == []
    assert [record.getMessage() for record in caplog.records] == [
        f"the process answering at 127.0.0.1:{listener.port} is not the "
        f"bettermemory daemon its state file names (pid {DEAD_PID}, store "
        f"{store}): nothing is sent to it"
    ]
    assert _daemon_client._answering(state_dir, store) is None


def test_an_older_daemon_of_ours_is_still_replaced(
    tmp_path: Path, listeners: list[_Listener]
) -> None:
    """ENG-35's rule is unchanged for a daemon that is ours: an older one
    is asked to stop, with the token, and its state file goes."""
    store = tmp_path / "memory.sqlite"
    state_dir = tmp_path / "state"
    ours = _proving(TOKEN, store)

    def older(nonce: str | None) -> dict[str, Any]:
        return {**ours(nonce), "version": "0.0.1"}

    listener = _listen(listeners, older)
    write_state(state_dir, _state(listener.port, store))
    assert (
        _daemon_client.ensure_daemon(
            state_dir, store, version=package_version(), start=False
        )
        is None
    )
    assert [path for path, _ in listener.posts] == ["/api/v1/admin/shutdown"]
    assert read_state(state_dir, store) is None


def test_a_pre_rc12_daemon_is_neither_kept_nor_posted_a_shutdown(
    tmp_path: Path, listeners: list[_Listener]
) -> None:
    """A daemon from before the proof, older than this client, at the port
    of the state file it wrote: the pre-rc12 fallback took it for ours and
    posted it the shutdown with the token. Since 9.0.0rc14 it is another
    listener: nothing is posted to it, and the state file stays for the
    daemon started next to overwrite."""
    store = tmp_path / "memory.sqlite"
    state_dir = tmp_path / "state"
    listener = _listen(listeners, _pre_rc12(store))
    write_state(state_dir, _state(listener.port, store))
    assert (
        _daemon_client.ensure_daemon(
            state_dir, store, version=package_version(), start=False
        )
        is None
    )
    assert listener.posts == []
    assert read_state(state_dir, store) is not None


def _hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    payload: dict[str, Any],
) -> tuple[int, str, str]:
    """`hook_main` in process for `tmp_path`'s store and state directory,
    with no daemon start possible: what it returned and printed."""
    monkeypatch.setattr(
        _daemon_client, "resolved_store_path", lambda: tmp_path / "memory.sqlite"
    )
    monkeypatch.setattr(_daemon_client, "resolve_state_dir", lambda: tmp_path / "state")

    def no_spawn(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no daemon is started in this test")

    monkeypatch.setattr(_daemon_client, "_spawn", no_spawn)
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(sys, "stdin", stdin)
    code = _daemon_client.hook_main(["hook", "prompt"])
    out, err = capsys.readouterr()
    return code, out, err


SECRET_PROMPT = "the prompt a listener must never see"


@pytest.mark.parametrize(
    "reply",
    [
        _another_store(),
        _another_store(version="0.0.1"),
        lambda nonce: {"status": "ok"},
        lambda nonce: {"status": "ok", "proof": "0" * 64},
    ],
    ids=["another-store-newer", "another-store-older", "bare-ok", "forged-proof"],
)
def test_a_hook_posts_nothing_to_a_listener_that_is_not_ours(
    tmp_path: Path,
    listeners: list[_Listener],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reply: Any,
) -> None:
    """After a crash another account's listener holds the port the stale
    state file names. The hook sends it no payload and prints nothing it
    says: with no daemon of its own to start here, it reports that on
    stderr and exits 0, as for any unreachable daemon."""
    listener = _listen(listeners, reply)
    write_state(tmp_path / "state", _state(listener.port, tmp_path / "memory.sqlite"))
    code, out, err = _hook(
        monkeypatch,
        capsys,
        tmp_path,
        {"session_id": "s", "prompt": SECRET_PROMPT, "cwd": str(tmp_path)},
    )
    assert code == 0
    assert out == ""
    assert "INJECTED" not in out + err
    assert "no daemon could be reached or started" in err
    assert listener.posts == []


def test_a_hook_posts_nothing_to_a_listener_repeating_a_proving_daemons_health(
    tmp_path: Path,
    listeners: list[_Listener],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The listener answers what the crashed daemon's open ``/health``
    told it before the crash: this version, the state file's store and
    pid, no proof. The state file says its daemon proves, so the hook
    takes the listener for none and sends it nothing."""
    store = tmp_path / "memory.sqlite"
    listener = _listen(
        listeners,
        lambda nonce: {
            "status": "ok",
            "version": package_version(),
            "store": str(store),
            "pid": DEAD_PID,
        },
    )
    write_state(
        tmp_path / "state", {**_state(listener.port, store), PROOF_STATE_KEY: 1}
    )
    code, out, err = _hook(
        monkeypatch,
        capsys,
        tmp_path,
        {"session_id": "s", "prompt": SECRET_PROMPT, "cwd": str(tmp_path)},
    )
    assert code == 0
    assert out == ""
    assert "no daemon could be reached or started" in err
    assert listener.posts == []
    assert len(listener.gets) >= 1


def test_a_hook_starts_a_daemon_beside_a_listener_that_does_not_speak_http(
    tmp_path: Path,
    stranger: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The stale state file's port is held by another protocol. The hook
    reads that as no daemon and goes on to start one (refused here, so it
    reports the start), where its discovery once raised on the reply and
    no daemon was ever started while the listener stayed."""
    write_state(tmp_path / "state", _state(stranger, tmp_path / "memory.sqlite"))
    code, out, err = _hook(
        monkeypatch,
        capsys,
        tmp_path,
        {"session_id": "s", "prompt": "hello", "cwd": str(tmp_path)},
    )
    assert code == 0
    assert out == ""
    assert "no daemon could be reached or started" in err
    assert "BadStatusLine" not in err


@pytest.mark.parametrize("keyed", [False, True], ids=["pre-rc12-file", "keyed-file"])
def test_a_hook_beside_an_unproven_holder_of_the_store_gives_up_at_once(
    tmp_path: Path,
    listeners: list[_Listener],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    keyed: bool,
) -> None:
    """A daemon from before 9.0.0rc12 still running after the upgrade: its
    state file (without `PROOF_STATE_KEY`) names a live pid that holds the
    store's lock, and its port answers without a proof. No daemon of this
    version can start while it runs, so the hook neither starts one nor
    waits `START_TIMEOUT_SECONDS` for one: it posts nothing, warns once
    with the remedy, and gives up within the proof's own timeout. A state
    file with the key whose pid holds the lock while its port does not
    prove itself is answered the same way."""
    store = tmp_path / "memory.sqlite"
    state_dir = tmp_path / "state"
    listener = _listen(listeners, _pre_rc12(store, pid=os.getpid()))
    state = _state(listener.port, store, pid=os.getpid())
    if keyed:
        state[PROOF_STATE_KEY] = 1
    write_state(state_dir, state)
    held = try_lock(lock_file_for(state_dir, store))
    assert held is not None
    caplog.set_level(logging.WARNING, logger="bettermemory._daemon_client")
    try:
        started = time.monotonic()
        code, out, err = _hook(
            monkeypatch,
            capsys,
            tmp_path,
            {"session_id": "s", "prompt": SECRET_PROMPT, "cwd": str(tmp_path)},
        )
        elapsed = time.monotonic() - started
    finally:
        release_lock(held)
    assert code == 0
    assert out == ""
    assert listener.posts == []
    assert elapsed < 2.0, elapsed
    [warning] = [record.getMessage() for record in caplog.records]
    assert f"is held by pid {os.getpid()}" in warning
    assert f"Stop pid {os.getpid()}" in warning
    assert "bettermemory service install" in warning
    assert ("from before 9.0.0rc12" in warning) is not keyed
    assert "no daemon could be reached or started" in err


def test_a_hook_posts_nothing_to_a_pre_rc12_daemon_of_its_store(
    tmp_path: Path,
    listeners: list[_Listener],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reply in the shape of a daemon from before the proof, of this
    version and naming the pid and the store of a state file without
    `PROOF_STATE_KEY`: the pre-rc12 fallback posted it the prompt and
    printed its reply. Since 9.0.0rc14 the hook sends it nothing and
    prints nothing it says, as for any listener that is not ours."""
    store = tmp_path / "memory.sqlite"
    listener = _listen(
        listeners,
        lambda nonce: {
            "status": "ok",
            "version": package_version(),
            "store": str(store),
            "pid": DEAD_PID,
        },
    )
    write_state(tmp_path / "state", _state(listener.port, store))
    code, out, err = _hook(
        monkeypatch,
        capsys,
        tmp_path,
        {"session_id": "s", "prompt": SECRET_PROMPT, "cwd": str(tmp_path)},
    )
    assert code == 0
    assert out == ""
    assert "INJECTED" not in out + err
    assert "no daemon could be reached or started" in err
    assert listener.posts == []


def test_a_hook_to_its_own_daemon_asks_for_the_proof_again_on_its_post(
    tmp_path: Path,
    listeners: list[_Listener],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Discovery's health request carries the first proof; the post asks
    again, on the connection its request then goes down
    (`_daemon_client._proven_connection`, 9.0.0rc14): two health requests,
    each with its own nonce, and one post."""
    store = tmp_path / "memory.sqlite"
    listener = _listen(listeners, _proving(TOKEN, store))
    write_state(tmp_path / "state", _state(listener.port, store))
    code, out, err = _hook(
        monkeypatch,
        capsys,
        tmp_path,
        {"session_id": "s", "prompt": "hello", "cwd": str(tmp_path)},
    )
    assert code == 0, err
    assert out == "INJECTED CONTEXT\n"
    nonces = [
        urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)[HEALTH_NONCE_PARAM][0]
        for path in listener.gets
    ]
    assert len(nonces) == 2 and nonces[0] != nonces[1]
    assert len(listener.posts) == 1
    sent = json.loads(listener.posts[0][1])
    assert sent["prompt"] == "hello"


# ---------------------------------------------------------------------------
# shutdown_daemon: the token only to our daemon, a signal only on evidence
# ---------------------------------------------------------------------------


class _Process:
    """The state file's pid as `shutdown_daemon` sees it: alive until a
    SIGTERM reaches it, every signal recorded and none sent."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, dies: bool = True) -> None:
        self.alive = True
        self.dies = dies
        self.signals: list[tuple[int, int]] = []
        monkeypatch.setattr(_daemon_client, "pid_alive", lambda pid: self.alive)
        monkeypatch.setattr(os, "kill", self.kill)

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if self.dies:
            self.alive = False


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[Any]:
    """The store's lock, held by this process, as a serving daemon holds it."""
    handle = try_lock(lock_file_for(tmp_path / "state", tmp_path / "memory.sqlite"))
    assert handle is not None
    yield handle
    release_lock(handle)


def test_a_stale_state_files_pid_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon is gone and the OS has handed its pid to another process:
    nothing answers, the lock is free, so the daemon is stopped and the
    pid's new owner is left alone."""
    process = _Process(monkeypatch)
    state = _state(_closed_port(), tmp_path / "memory.sqlite")
    started = time.monotonic()
    assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=5.0)
    assert time.monotonic() - started < 3.0
    assert process.signals == []


def test_a_listener_that_is_not_ours_gets_no_shutdown_and_its_pid_no_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listeners: list[_Listener]
) -> None:
    """The old predicate read the held port as our daemon still running,
    waited out the timeout and signalled the pid. The port is someone
    else's; the free lock says our daemon is gone."""
    process = _Process(monkeypatch)
    listener = _listen(listeners, _another_store())
    state = _state(listener.port, tmp_path / "memory.sqlite")
    started = time.monotonic()
    assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=5.0)
    assert time.monotonic() - started < 3.0
    assert process.signals == []
    assert listener.posts == []


def test_down_reads_a_listener_that_does_not_speak_http_as_no_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stranger: int
) -> None:
    process = _Process(monkeypatch)
    state = _state(stranger, tmp_path / "memory.sqlite")
    assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=5.0)
    assert process.signals == []


def test_a_pid_that_holds_the_lock_is_signalled_without_a_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_lock: Any
) -> None:
    """A daemon that does not answer (wedged, or not listening yet) but is
    alive while the store's lock is held is the daemon: after the wait it
    gets the signal, as before."""
    process = _Process(monkeypatch)
    state = _state(_closed_port(), tmp_path / "memory.sqlite")
    assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=0.3)
    assert process.signals == [(DEAD_PID, signal.SIGTERM)]


def test_a_listener_that_is_not_ours_gets_nothing_while_the_lock_decides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listeners: list[_Listener],
    held_lock: Any,
) -> None:
    process = _Process(monkeypatch)
    listener = _listen(listeners, _another_store())
    state = _state(listener.port, tmp_path / "memory.sqlite")
    assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=0.3)
    assert listener.posts == []
    assert process.signals == [(DEAD_PID, signal.SIGTERM)]


def test_our_daemon_is_posted_the_shutdown_then_signalled_if_it_stays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listeners: list[_Listener],
    held_lock: Any,
) -> None:
    process = _Process(monkeypatch)
    store = tmp_path / "memory.sqlite"
    listener = _listen(listeners, _proving(TOKEN, store))
    state = _state(listener.port, store)
    assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=0.3)
    assert [path for path, _ in listener.posts] == ["/api/v1/admin/shutdown"]
    assert process.signals == [(DEAD_PID, signal.SIGTERM)]


def test_a_pid_that_survives_the_signal_reads_not_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_lock: Any
) -> None:
    process = _Process(monkeypatch, dies=False)
    state = _state(_closed_port(), tmp_path / "memory.sqlite")
    assert not shutdown_daemon(state, state_dir=tmp_path / "state", timeout=0.2)
    assert process.signals == [(DEAD_PID, signal.SIGTERM)]


def test_a_signal_that_fails_reads_the_daemon_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_lock: Any
) -> None:
    _Process(monkeypatch)

    def refused(pid: int, sig: int) -> None:
        raise PermissionError("not ours to signal")

    monkeypatch.setattr(os, "kill", refused)
    state = _state(_closed_port(), tmp_path / "memory.sqlite")
    assert not shutdown_daemon(state, state_dir=tmp_path / "state", timeout=0.2)


def test_a_lock_released_after_the_wait_is_not_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait ran out with the lock held, and the lock came free before
    the signal: the pid no longer holds it, so it is not signalled."""
    process = _Process(monkeypatch)
    monkeypatch.setattr(_daemon_client, "_wait_stopped", lambda *a, **k: False)
    state = _state(_closed_port(), tmp_path / "memory.sqlite")
    assert shutdown_daemon(state, state_dir=tmp_path / "state", timeout=0.2)
    assert process.signals == []


# ---------------------------------------------------------------------------
# The state directory and the daemon's log are owner-only from the start
# ---------------------------------------------------------------------------


@pytest.fixture
def open_umask() -> Iterator[None]:
    """The usual umask, 022, under which a plain mkdir or open makes a
    directory 0o755 and a file 0o644."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@pytest.mark.usefixtures("open_umask")
def test_spawn_makes_the_state_directory_and_the_log_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Child:
        pid = 4242

    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: _Child())
    state_dir = tmp_path / "a" / "b" / "state"
    store = tmp_path / "store" / "memory.sqlite"
    started = _daemon_client._spawn(state_dir, store, port=None, env={})
    assert _mode(tmp_path / "a") == 0o700
    assert _mode(tmp_path / "a" / "b") == 0o700
    assert _mode(state_dir) == 0o700
    assert started.log == _daemon_client.log_file_for(state_dir.resolve(), store)
    assert _mode(started.log) == 0o600
    assert started.log_offset == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@pytest.mark.usefixtures("open_umask")
def test_spawn_heals_an_open_state_directory_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What an earlier release left at the umask: the directory 0o755 and
    the log 0o644. The next start narrows both before the child writes,
    and keeps the log's lines (the child's own begin at the offset)."""

    class _Child:
        pid = 4242

    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: _Child())
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o755)
    os.chmod(state_dir, 0o755)
    store = tmp_path / "store" / "memory.sqlite"
    log = _daemon_client.log_file_for(state_dir.resolve(), store)
    log.write_text("an earlier start's line\n", encoding="utf-8")
    os.chmod(log, 0o644)
    started = _daemon_client._spawn(state_dir, store, port=None, env={})
    assert _mode(state_dir) == 0o700
    assert _mode(log) == 0o600
    assert started.log_offset == len("an earlier start's line\n")
    assert log.read_text(encoding="utf-8") == "an earlier start's line\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
@pytest.mark.usefixtures("open_umask")
def test_a_foreground_daemon_and_the_state_file_keep_the_directory_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`up --foreground` (a supervisor's start) makes the directory
    owner-only before it opens its lock there, and writing the state file
    heals a directory left open."""
    monkeypatch.chdir(tmp_path)
    for name in ("BETTERMEMORY_DIR", "BETTERMEMORY_KEYS_DIR", "BETTERMEMORY_STATE_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    entered = _daemon_client.enter_state_dir(
        tmp_path / "fg" / "state", tmp_path / "store" / "memory.sqlite"
    )
    assert _mode(tmp_path / "fg") == 0o700
    assert _mode(entered) == 0o700
    opened = tmp_path / "opened"
    opened.mkdir()
    os.chmod(opened, 0o755)
    written = write_state(opened, _state(1, tmp_path / "store" / "memory.sqlite"))
    assert _mode(opened) == 0o700
    assert _mode(written) == 0o600


# ---------------------------------------------------------------------------
# End to end, with a real daemon
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    env = _env(tmp_path)
    yield env
    _cli(["down"], env, timeout=30)


def test_a_real_daemon_proves_itself_and_takes_only_its_own_port(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    state = _wait_running(tmp_path)
    found = identify(state)
    assert found.ours and found.answer is not None
    assert found.answer["pid"] == state["pid"]
    plain = health(state["port"])
    assert plain is not None and "proof" not in plain
    assert identify({**state, "token": "b" * 64}).foreign
    request = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}/health", headers={"Host": "127.0.0.1:1"}
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)
    # The error holds the response and its socket until closed.
    refused.value.close()
    assert refused.value.code == 421
    assert _cli(["down"], daemon_env).returncode == 0


def test_a_listener_repeating_a_real_daemons_health_is_not_taken_for_it(
    tmp_path: Path, daemon_env: dict[str, str], listeners: list[_Listener]
) -> None:
    """What ``/health`` tells any process on the host without the token,
    the daemon's pid and store, is not enough to pass for the daemon once
    it is gone: its state file says it proves, and a listener that
    repeats its reply word for word offers no proof."""
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    state = _wait_running(tmp_path)
    assert state[PROOF_STATE_KEY] == 1
    seen = health(state["port"])
    assert seen is not None and seen["pid"] == state["pid"]
    listener = _listen(listeners, lambda nonce: seen)
    assert identify({**state, "port": listener.port}).foreign
    assert identify(state).ours
    assert _cli(["down"], daemon_env).returncode == 0


def test_bind_takes_another_port_when_a_listener_holds_the_one_asked_for(
    listeners: list[_Listener],
) -> None:
    """The listener sets SO_REUSEADDR, as http.server does. On Windows that
    let the daemon's bind share its port, where it must take another."""
    from bettermemory import daemon

    listener = _listen(listeners, _another_store())
    sock = daemon.bind("127.0.0.1", listener.port)
    try:
        assert sock.getsockname()[1] != listener.port
    finally:
        sock.close()


class _RecordingSocket(socket.socket):
    options: list[int] = []

    def setsockopt(self, level: int, optname: int, value: Any, *rest: Any) -> None:
        type(self).options.append(optname)
        if optname == _FAKE_EXCLUSIVE:
            return
        super().setsockopt(level, optname, value, *rest)


_FAKE_EXCLUSIVE = 0x7EEF


@pytest.mark.parametrize("platform_has_it", [True, False])
def test_bind_asks_for_exclusive_use_where_the_platform_offers_it(
    monkeypatch: pytest.MonkeyPatch, platform_has_it: bool
) -> None:
    from bettermemory import daemon

    if platform_has_it:
        monkeypatch.setattr(
            socket, "SO_EXCLUSIVEADDRUSE", _FAKE_EXCLUSIVE, raising=False
        )
    else:
        monkeypatch.delattr(socket, "SO_EXCLUSIVEADDRUSE", raising=False)
    monkeypatch.setattr(_RecordingSocket, "options", [])
    monkeypatch.setattr(socket, "socket", _RecordingSocket)
    sock = daemon.bind("127.0.0.1", 0)
    sock.close()
    if platform_has_it:
        assert _RecordingSocket.options == [_FAKE_EXCLUSIVE]
    else:
        assert _RecordingSocket.options == [socket.SO_REUSEADDR]


def test_a_daemon_starts_beside_a_listener_on_the_stale_port(
    tmp_path: Path,
    daemon_env: dict[str, str],
    listeners: list[_Listener],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole recovery: the state file names a crashed daemon's port,
    which another listener now holds. The client starts a daemon of its
    own, which finds the port held, binds another and rewrites the state
    file; the listener got health requests and nothing else."""
    store = _store_path(tmp_path)
    state_dir = tmp_path / "state"
    listener = _listen(listeners, _another_store())
    write_state(state_dir, _state(listener.port, store))
    spawned: list[_daemon_client.Started] = []
    spawn = _daemon_client._spawn

    def recording(*args: Any, **kwargs: Any) -> _daemon_client.Started:
        started = spawn(*args, **kwargs)
        spawned.append(started)
        return started

    monkeypatch.setattr(_daemon_client, "_spawn", recording)
    state = _daemon_client.ensure_daemon(
        state_dir,
        store,
        version=package_version(),
        port=listener.port,
        env=daemon_env,
        timeout=30.0,
    )
    assert state is not None
    assert state["port"] != listener.port
    assert identify(state).ours
    assert read_state(state_dir, store) == state
    assert listener.posts == []
    assert all(path.startswith("/health?") for path in listener.gets)
    # The daemon this process spawned is stopped and reaped here, so its
    # Popen is not collected while the child still runs.
    assert _cli(["down"], daemon_env).returncode == 0
    for started in spawned:
        started.child.wait(timeout=30)


def test_a_configured_proxy_never_sees_a_request(
    listeners: list[_Listener], monkeypatch: pytest.MonkeyPatch
) -> None:
    # urllib sends a request for 127.0.0.1 through http_proxy unless
    # no_proxy names it; the token and the hook payloads must not go there.
    proxy = _listen(listeners, _another_store())
    daemon = _listen(listeners, _proving(TOKEN, Path("/x/memory.sqlite")))
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
    assert health(daemon.port) is not None
    post(daemon.port, TOKEN, "/api/v1/hook/stop", {"session_id": "s"}, timeout=2.0)
    assert daemon.gets[0] == "/health" and len(daemon.gets) == 2
    assert daemon.gets[1].startswith(f"/health?{HEALTH_NONCE_PARAM}=")
    assert [path for path, _ in daemon.posts] == ["/api/v1/hook/stop"]
    assert proxy.gets == [] and proxy.posts == []


def test_status_takes_no_listener_repeating_a_proving_daemons_health(
    tmp_path: Path, listeners: list[_Listener]
) -> None:
    """`status` asks for the proof the hooks ask for: a listener that
    repeats the store and the pid an open ``/health`` gave it, without the
    proof the state file says its daemon gives, is not reported running."""
    from bettermemory.cli.daemon_cmd import NOT_OURS, RUNNING, daemon_report

    store = tmp_path / "memory.sqlite"
    listener = _listen(
        listeners,
        lambda nonce: {
            "status": "ok",
            "version": package_version(),
            "store": str(store),
            "pid": DEAD_PID,
        },
    )
    state_dir = tmp_path / "state"
    write_state(state_dir, {**_state(listener.port, store), PROOF_STATE_KEY: 1})
    report = daemon_report(state_dir, store, tidy=False)
    assert report["state"] == NOT_OURS
    assert report["running"] is False

    proving = _listen(listeners, _proving(TOKEN, store))
    write_state(state_dir, {**_state(proving.port, store), PROOF_STATE_KEY: 1})
    assert daemon_report(state_dir, store, tidy=False)["state"] == RUNNING
