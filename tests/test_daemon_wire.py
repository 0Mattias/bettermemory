"""ENG-102: the bearer token goes only down a connection whose listener has
just proven, on that same connection, that it holds it.

The state file outlives a daemon that crashed, and a 127.0.0.1 port is any
account's to bind. Before 9.0.0rc14 a client proved its daemon on one
connection and sent the token on another: the hook client right after its
discovery, and the long-running shim on every call to the port it knew. A
daemon that died in between and a listener that took its port received the
token, the payload, and the chance to answer them. Each test here crashes a
daemon (a stand-in, or a real one under SIGKILL), binds a squatter to its
port that records every byte it is sent and answers as the crashed daemon's
open ``/health`` did, and checks that the squatter got only the proof's
request: no token, no ``Authorization`` header (so no MAC or anything else
derived from the token either) and no payload.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpcore2
import httpx2
import pytest

from bettermemory import _daemon_client, shim
from bettermemory._daemon_client import (
    HEALTH_NONCE_PARAM,
    PROOF_STATE_KEY,
    health,
    health_proof,
    package_version,
    pid_alive,
    post,
    write_state,
)

from .test_daemon_lifecycle import _cli, _env, _wait_running

#: SIGKILL where the platform has it: the real daemon's crash is POSIX only.
_SIGKILL: int = getattr(signal, "SIGKILL", 9)

#: What a payload carries that a squatter must never see.
SECRET = "the payload a squatter must never see"


def _token() -> str:
    return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# A daemon that can crash, and what takes its port
# ---------------------------------------------------------------------------


class _Daemon(http.server.ThreadingHTTPServer):
    """A daemon's HTTP surface, HTTP/1.1 keep-alive as uvicorn serves it:
    ``/health`` proves `token`, a POST with the token is answered and
    recorded with the client port it came on, one without it gets 401.
    `crash` closes the listening socket and every connection, as the
    daemon's death does."""

    daemon_threads = True
    pid = 2**22 + 4325

    def __init__(self, token: str, store: Path) -> None:
        super().__init__(("127.0.0.1", 0), _DaemonHandler)
        self.token = token
        self.store = str(store)
        self.gets: list[tuple[int, str]] = []
        self.posts: list[tuple[int, str, bytes]] = []
        self.authorizations: list[str] = []
        self._held: list[socket.socket] = []
        self._crashed = False
        self._guard = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def state(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "port": self.port,
            "token": self.token,
            "version": package_version(),
            "store": self.store,
            "started": "2026-01-01T00:00:00Z",
            PROOF_STATE_KEY: 1,
        }

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._guard:
            self._held.append(request)
        super().process_request(request, client_address)

    def crash(self) -> None:
        with self._guard:
            if self._crashed:
                return
            self._crashed = True
            held = list(self._held)
        self.shutdown()
        self.server_close()
        for sock in held:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class _DaemonHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def daemon(self) -> _Daemon:
        return cast(_Daemon, self.server)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.daemon.gets.append((self.client_address[1], self.path))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        nonce = query.get(HEALTH_NONCE_PARAM)
        answer: dict[str, Any] = {
            "status": "ok",
            "version": package_version(),
            "store": self.daemon.store,
            "pid": self.daemon.pid,
        }
        if nonce:
            answer["proof"] = health_proof(self.daemon.token, nonce[0])
        self._json(200, answer)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.daemon.authorizations.append(self.headers.get("Authorization") or "")
        if self.headers.get("Authorization") != f"Bearer {self.daemon.token}":
            self._json(401, {"error": "unauthorized"})
            return
        self.daemon.posts.append((self.client_address[1], self.path, body))
        self._json(200, {"stdout": f"from the daemon at {self.daemon.port}"})


class _Squatter(socketserver.ThreadingTCPServer):
    """What takes a crashed daemon's port: it records every byte a client
    sends it, answers ``GET /health`` with `health` (the crashed daemon's
    open reply: its pid, store and version, no proof) and anything else
    with a reply a hook would print and a shim would forward. It keeps
    every connection open, so a client that read its ``/health`` could
    send the next request down the same one."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int, health: dict[str, Any]) -> None:
        self.health = health
        self.received = bytearray()
        self.requests: list[bytes] = []
        self.guard = threading.Lock()
        super().__init__(("127.0.0.1", port), _SquatHandler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def assert_got_nothing_of(self, token: str, *payloads: str) -> None:
        """Only proofs' health requests: not the token, no Authorization
        header of any scheme, none of `payloads`."""
        with self.guard:
            seen = bytes(self.received)
            requests = list(self.requests)
        assert token.encode() not in seen, "the squatter received the token"
        assert b"authorization" not in seen.lower(), "an Authorization header"
        for payload in payloads:
            assert payload.encode() not in seen, "the squatter received the payload"
        assert requests, "premise: the client reached the squatter"
        assert all(line.startswith(b"GET /health?nonce=") for line in requests), (
            requests
        )


class _SquatHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        squatter = cast(_Squatter, self.server)
        sock: socket.socket = self.request
        sock.settimeout(10.0)
        pending = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            with squatter.guard:
                squatter.received += chunk
            pending += chunk
            while b"\r\n\r\n" in pending:
                head, _, rest = pending.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                if len(rest) < length:
                    break
                pending = rest[length:]
                request_line = head.split(b"\r\n", 1)[0]
                with squatter.guard:
                    squatter.requests.append(request_line)
                reply: dict[str, Any]
                if request_line.startswith(b"GET /health"):
                    reply = squatter.health
                else:
                    reply = {"stdout": "INJECTED CONTEXT", "jsonrpc": "2.0", "id": 1}
                body = json.dumps(reply).encode()
                sock.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )


@pytest.fixture
def squatters() -> Iterator[list[_Squatter]]:
    made: list[_Squatter] = []
    yield made
    for squatter in made:
        squatter.stop()


@pytest.fixture
def daemons() -> Iterator[list[_Daemon]]:
    made: list[_Daemon] = []
    yield made
    for daemon in made:
        daemon.crash()


def _attempt(port: int, token: str, payload: dict[str, Any]) -> Exception | None:
    """`post` to `port`, and the URLError it raised, None when it did not."""
    try:
        post(port, token, "/api/v1/hook/prompt", payload, timeout=5.0)
    except urllib.error.URLError as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# The hook client: `post` and `hook_main`
# ---------------------------------------------------------------------------


def test_a_post_to_a_crashed_daemons_port_sends_its_squatter_no_token(
    tmp_path: Path, daemons: list[_Daemon], squatters: list[_Squatter]
) -> None:
    """A client that knows the daemon's port and token (the hook's
    membership rounds, `shutdown_daemon`, the bench's warm client) posts
    after the daemon died and another listener took the port. The
    squatter gets the proof's health request and nothing else, and the
    post fails as not proven."""
    token = _token()
    daemon = _Daemon(token, tmp_path / "memory.sqlite")
    daemons.append(daemon)
    assert post(
        daemon.port, token, "/api/v1/hook/prompt", {"prompt": "hi"}, timeout=5.0
    )
    port, open_health = daemon.port, health(daemon.port)
    assert open_health is not None
    daemon.crash()
    squatter = _Squatter(port, open_health)
    squatters.append(squatter)
    refused = _attempt(port, token, {"prompt": SECRET, "cwd": str(tmp_path)})
    squatter.assert_got_nothing_of(token, SECRET)
    assert isinstance(refused, _daemon_client.NotProven), refused
    assert "nothing was sent to it" in str(refused)


def test_a_hook_whose_daemon_dies_after_discovery_sends_the_squatter_nothing(
    tmp_path: Path,
    daemons: list[_Daemon],
    squatters: list[_Squatter],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The race every hook ran before 9.0.0rc14: discovery proves the
    daemon (`ensure_daemon`, `identify`), the daemon dies, and a listener
    takes its port before the post. The post proves its own connection,
    so the squatter gets the proof's request, the hook prints nothing it
    says, and its one stderr line says the listener did not prove
    itself."""
    token = _token()
    store = tmp_path / "memory.sqlite"
    daemon = _Daemon(token, store)
    daemons.append(daemon)
    write_state(tmp_path / "state", daemon.state())
    found = _daemon_client.ensure_daemon

    def found_then_crashed(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        state = found(*args, **kwargs)
        assert state is not None and state["port"] == daemon.port, "premise"
        open_health = health(daemon.port)
        assert open_health is not None
        daemon.crash()
        squatters.append(_Squatter(daemon.port, open_health))
        return state

    monkeypatch.setattr(_daemon_client, "ensure_daemon", found_then_crashed)
    monkeypatch.setattr(_daemon_client, "resolved_store_path", lambda: store)
    monkeypatch.setattr(_daemon_client, "resolve_state_dir", lambda: tmp_path / "state")
    payload = {"session_id": "s", "prompt": SECRET, "cwd": str(tmp_path)}
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
    )
    assert _daemon_client.hook_main(["hook", "prompt"]) == 0
    out, err = capsys.readouterr()
    squatters[0].assert_got_nothing_of(token, SECRET)
    assert out == ""
    assert "INJECTED" not in err
    assert "NotProven" in err and "did not prove it holds" in err


def test_a_hook_to_its_own_daemon_posts_down_the_connection_it_proved(
    tmp_path: Path, daemons: list[_Daemon]
) -> None:
    """`post` asks for the proof on the connection its request then goes
    down: one health request with a fresh nonce before every post, from
    the same client port as the post."""
    token = _token()
    daemon = _Daemon(token, tmp_path / "memory.sqlite")
    daemons.append(daemon)
    for _ in range(2):
        post(daemon.port, token, "/api/v1/hook/stop", {"session_id": "s"}, timeout=5.0)
    assert len(daemon.gets) == 2 and len(daemon.posts) == 2
    nonces = {
        urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)[HEALTH_NONCE_PARAM][0]
        for _, path in daemon.gets
    }
    assert len(nonces) == 2
    assert [port for port, _ in daemon.gets] == [port for port, _, _ in daemon.posts]


class _ClosingHandler(_DaemonHandler):
    """A daemon that proves its token over HTTP/1.0, closing every
    connection after its reply."""

    protocol_version = "HTTP/1.0"


def test_a_proof_on_a_connection_that_closes_carries_no_post(
    tmp_path: Path,
) -> None:
    """The proof holds for the connection it was made on. A reply that
    closes it cannot carry the request, and `http.client` would open a
    new, unproven connection for one: nothing is posted."""
    token = _token()
    daemon = _Daemon(token, tmp_path / "memory.sqlite")
    daemon.RequestHandlerClass = _ClosingHandler
    try:
        refused = _attempt(daemon.port, token, {"prompt": SECRET})
    finally:
        daemon.crash()
    assert isinstance(refused, _daemon_client.NotProven), refused
    assert "closes the connection" in str(refused)
    assert len(daemon.gets) == 1 and daemon.posts == []


async def test_a_shim_proof_on_a_connection_that_closes_gives_the_pool_nothing(
    tmp_path: Path,
) -> None:
    """The shim's twin of the test above: the pool's backend judges its
    proof by the same rule as a hook's post (`_daemon_client.proof_failure`),
    so a proof whose reply closes the connection fails the connect, and the
    pool is never handed a stream that cannot carry its request."""
    token = _token()
    daemon = _Daemon(token, tmp_path / "memory.sqlite")
    daemon.RequestHandlerClass = _ClosingHandler
    try:
        with pytest.raises(httpcore2.ConnectError, match="closes the connection"):
            await shim._ProvingBackend(daemon.port, token).connect_tcp(
                "127.0.0.1", daemon.port, timeout=5.0
            )
    finally:
        daemon.crash()
    assert len(daemon.gets) == 1 and daemon.posts == []


def test_a_proven_connection_never_opens_another(
    tmp_path: Path, daemons: list[_Daemon]
) -> None:
    """`http.client` reopens a dropped connection by itself unless told not
    to; the one a proof was made on is told not to, so whatever it carries
    next goes down the proven connection or nowhere."""
    daemon = _Daemon(_token(), tmp_path / "memory.sqlite")
    daemons.append(daemon)
    connection = _daemon_client._proven_connection(
        daemon.port, daemon.token, timeout=5.0
    )
    try:
        assert connection.auto_open == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "ok", "pid": _Daemon.pid},
        {"status": "ok", "pid": _Daemon.pid, "proof": "0" * 64},
        {"error": "not found"},
    ],
    ids=["no-proof", "forged-proof", "not-health"],
)
def test_a_listener_that_does_not_prove_itself_gets_no_post(
    tmp_path: Path, squatters: list[_Squatter], answer: dict[str, Any]
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    squatter = _Squatter(port, {**answer, "store": str(tmp_path / "memory.sqlite")})
    squatters.append(squatter)
    token = _token()
    refused = _attempt(port, token, {"prompt": SECRET})
    squatter.assert_got_nothing_of(token, SECRET)
    assert isinstance(refused, _daemon_client.NotProven), refused


# ---------------------------------------------------------------------------
# The shim: every connection its pool opens
# ---------------------------------------------------------------------------


def _shim_transport(upstream: shim._Upstream) -> shim._ForwardingTransport:
    """The transport `shim._serve_through_daemon` builds."""
    return shim._ForwardingTransport(shim._DaemonTransport(), upstream)


async def test_the_shims_next_call_after_a_crash_sends_the_squatter_nothing(
    tmp_path: Path, daemons: list[_Daemon], squatters: list[_Squatter]
) -> None:
    """The shim served a call through its daemon and keeps the port and
    the token. The daemon dies and a listener takes the port. The next
    call's connection fails its proof before the call goes out, the shim
    finds the daemon again (here on another port, with another token)
    and the call is answered there; the squatter got the proof's request
    and nothing else."""
    store = tmp_path / "memory.sqlite"
    first = _Daemon(_token(), store)
    second = _Daemon(_token(), store)
    daemons.extend([first, second])
    lookups: list[int] = []

    def find() -> tuple[dict[str, Any] | None, str | None]:
        lookups.append(second.port)
        return second.state(), None

    upstream = shim._Upstream(first.state(), find)
    url = f"http://127.0.0.1:{first.port}/api/v1/hook/prompt"
    async with httpx2.AsyncClient(
        transport=_shim_transport(upstream), timeout=httpx2.Timeout(10.0, connect=5.0)
    ) as client:
        served = await client.post(url, json={"prompt": "hi"})
        assert served.json() == {"stdout": f"from the daemon at {first.port}"}
        open_health = health(first.port)
        assert open_health is not None
        first.crash()
        squatter = _Squatter(first.port, open_health)
        squatters.append(squatter)
        resent = await client.post(url, json={"prompt": SECRET})
    squatter.assert_got_nothing_of(first.token, SECRET)
    assert resent.json() == {"stdout": f"from the daemon at {second.port}"}
    assert lookups == [second.port]
    assert [json.loads(body) for _, _, body in second.posts] == [{"prompt": SECRET}]


async def test_the_shim_answers_an_error_when_only_a_squatter_answers(
    tmp_path: Path, daemons: list[_Daemon], squatters: list[_Squatter]
) -> None:
    """With no daemon to find again, the call that met the squatter is
    answered with an error, and the squatter still got nothing but the
    proof's request."""
    daemon = _Daemon(_token(), tmp_path / "memory.sqlite")
    daemons.append(daemon)
    upstream = shim._Upstream(daemon.state(), lambda: (None, "none answered"))
    url = f"http://127.0.0.1:{daemon.port}/mcp"
    async with httpx2.AsyncClient(
        transport=_shim_transport(upstream), timeout=httpx2.Timeout(10.0, connect=5.0)
    ) as client:
        await client.post(url, json={"prompt": "hi"})
        open_health = health(daemon.port)
        assert open_health is not None
        daemon.crash()
        squatter = _Squatter(daemon.port, open_health)
        squatters.append(squatter)
        failed = await client.post(
            url, json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}
        )
    squatter.assert_got_nothing_of(daemon.token, "tools/call")
    error = failed.json()["error"]["message"]
    assert "no bettermemory daemon could be reached or started" in error
    assert "did not prove it is the bettermemory daemon" in error


async def test_the_shims_connections_are_proven_for_their_own_port(
    tmp_path: Path, daemons: list[_Daemon]
) -> None:
    """A connection to a port the backend holds no token for (another
    daemon, which would prove its own) fails before it is opened; one to
    its daemon's port is proven and handed to the pool."""
    daemon = _Daemon(_token(), tmp_path / "memory.sqlite")
    other = _Daemon(_token(), tmp_path / "memory.sqlite")
    daemons.extend([daemon, other])
    backend = shim._ProvingBackend(daemon.port, daemon.token)
    with pytest.raises(httpcore2.ConnectError, match="holds no token for"):
        await backend.connect_tcp("127.0.0.1", other.port, timeout=5.0)
    stream = await backend.connect_tcp("127.0.0.1", daemon.port, timeout=5.0)
    await stream.aclose()
    assert other.gets == [] and other.posts == []
    assert len(daemon.gets) == 1 and daemon.posts == []


async def test_a_connection_proven_for_one_token_never_carries_another(
    tmp_path: Path, daemons: list[_Daemon]
) -> None:
    """The shim's token changes while a connection it proved stays open (a
    refresh read a state file naming another token at the same port). The
    request with the new token goes down a connection of its own, which
    the daemon there, holding the old token, does not prove: the new token
    never reaches it, and the shim finds the daemon again and resends with
    the token the daemon proved."""
    daemon = _Daemon(_token(), tmp_path / "memory.sqlite")
    daemons.append(daemon)
    stale = _token()
    upstream = shim._Upstream(daemon.state(), lambda: (daemon.state(), None))
    url = f"http://127.0.0.1:{daemon.port}/api/v1/hook/prompt"
    async with httpx2.AsyncClient(
        transport=_shim_transport(upstream), timeout=httpx2.Timeout(10.0, connect=5.0)
    ) as client:
        assert (await client.post(url, json={"prompt": "hi"})).status_code == 200
        upstream.token = stale
        resent = await client.post(url, json={"prompt": SECRET})
    assert resent.json() == {"stdout": f"from the daemon at {daemon.port}"}
    assert upstream.token == daemon.token
    assert daemon.authorizations == [f"Bearer {daemon.token}"] * 2


# ---------------------------------------------------------------------------
# An httpx2 that keeps its pool elsewhere
# ---------------------------------------------------------------------------


def _pool_kept_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make httpx2 keep its transport's connection pool under another name
    than 2.16's `_pool`, as a later httpx2 might: `__init__` moves it to
    `_kept` and `handle_async_request` sends down what `_kept` holds. Both
    name the attributes only as strings, so neither method's code reads
    `_pool` by name."""
    cls = httpx2.AsyncHTTPTransport
    real_init, real_handle = cls.__init__, cls.handle_async_request

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        vars(self)["_kept"] = vars(self).pop("_pool")

    async def handle(self: Any, request: httpx2.Request) -> httpx2.Response:
        vars(self)["_pool"] = vars(self)["_kept"]
        return await real_handle(self, request)

    monkeypatch.setattr(cls, "__init__", init)
    monkeypatch.setattr(cls, "handle_async_request", handle)


def test_the_shims_pools_prove_their_connections_on_this_httpx2() -> None:
    """On the httpx2 the lock holds, the pool a request goes down is the
    shim's, every connection of which `_ProvingBackend` proves."""
    assert shim._unprovable_httpx2() is None
    transport = shim._ProvenTransport(1, _token())
    assert isinstance(transport._pool._network_backend, shim._ProvingBackend)


async def test_an_httpx2_that_keeps_its_pool_elsewhere_sends_the_squatter_nothing(
    tmp_path: Path,
    daemons: list[_Daemon],
    squatters: list[_Squatter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim's proof lives in the pool it gives httpx2's transport. With
    an httpx2 that reads its pool from elsewhere, an unguarded shim sent the
    call down httpx2's own, unproven pool: to a squatter on a crashed
    daemon's port, token and payload included. The shim refuses to build
    its transport there instead, and the squatter receives nothing."""
    daemon = _Daemon(_token(), tmp_path / "memory.sqlite")
    daemons.append(daemon)
    open_health = health(daemon.port)
    assert open_health is not None
    daemon.crash()
    squatter = _Squatter(daemon.port, open_health)
    squatters.append(squatter)
    _pool_kept_elsewhere(monkeypatch)
    upstream = shim._Upstream(daemon.state(), lambda: (None, "none answered"))
    url = f"http://127.0.0.1:{daemon.port}/api/v1/hook/prompt"
    refused: Exception | None = None
    try:
        async with httpx2.AsyncClient(
            transport=_shim_transport(upstream),
            timeout=httpx2.Timeout(10.0, connect=5.0),
        ) as client:
            await client.post(url, json={"prompt": SECRET})
    except RuntimeError as exc:
        refused = exc
    with squatter.guard:
        seen = bytes(squatter.received)
    assert daemon.token.encode() not in seen, "the squatter received the token"
    assert SECRET.encode() not in seen, "the squatter received the payload"
    assert refused is not None
    assert f"httpx2 {httpx2.__version__}" in str(refused)
    assert "nothing was sent" in str(refused)


def test_the_shim_does_not_start_on_an_httpx2_it_cannot_prove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_shim` asks before anything else: on such an httpx2 it ends in
    one line naming httpx2's version, before it looks for a daemon, and
    never falls back to an unproven pool."""
    _pool_kept_elsewhere(monkeypatch)

    def no_lookup(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the shim looked for a daemon")

    monkeypatch.setattr(_daemon_client, "ensure_daemon", no_lookup)
    with pytest.raises(SystemExit) as refused:
        shim.run_shim()
    message = str(refused.value.code)
    assert f"httpx2 {httpx2.__version__}" in message
    assert "the shim does not start, and nothing was sent to the daemon" in message
    with pytest.raises(RuntimeError, match="nothing was sent"):
        shim._ProvenTransport(1, _token())


# ---------------------------------------------------------------------------
# The real daemon, killed
# ---------------------------------------------------------------------------


def _wait_gone(pid: int, port: int, timeout: float = 15.0) -> None:
    """Until `pid` is gone and nothing listens on `port`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
            except OSError:
                return
        time.sleep(0.02)
    raise AssertionError(f"pid {pid} still alive or port {port} still listening")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX")
def test_a_real_daemons_post_is_proven_and_its_squatter_gets_nothing(
    tmp_path: Path, squatters: list[_Squatter]
) -> None:
    """Against the daemon itself (uvicorn, HTTP/1.1 keep-alive): a hook
    post goes down the connection its proof was made on, and after a
    SIGKILL a listener on the daemon's port gets the next post's proof
    request and nothing else."""
    env = _env(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    up = _cli(["up", "--port", "0"], env)
    assert up.returncode == 0, up.stderr
    state = _wait_running(tmp_path)
    pid, port, token = int(state["pid"]), int(state["port"]), str(state["token"])
    try:
        reply = post(
            port,
            token,
            "/api/v1/hook/session-start",
            {"session_id": "sess_wire", "cwd": str(tmp_path)},
            timeout=30.0,
        )
        assert isinstance(reply, dict) and "stdout" in reply
        open_health = health(port)
        assert open_health is not None and open_health["pid"] == pid
        os.kill(pid, _SIGKILL)
        _wait_gone(pid, port)
        squatter = _Squatter(port, open_health)
        squatters.append(squatter)
        refused = _attempt(port, token, {"prompt": SECRET, "cwd": str(tmp_path)})
        squatter.assert_got_nothing_of(token, SECRET)
        assert isinstance(refused, _daemon_client.NotProven), refused
    finally:
        if pid_alive(pid):
            os.kill(pid, _SIGKILL)
