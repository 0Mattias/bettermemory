"""``bettermemory`` with no arguments: a stdio MCP server in front of the
daemon.

A real stdio server, so every client config that names the ``bettermemory``
command keeps working: its ``initialize`` carries the daemon's
instructions and this package's version, its ``tools/list`` is the
daemon's, and its ``tools/call`` forwards over streamable HTTP with the
headers `identity` reads: ``x-bettermemory-session`` (this process's
session id, so two shims against one daemon keep separate session
state), the harness's own session id (``CLAUDE_CODE_SESSION_ID`` in the
client's environment) as ``x-bettermemory-harness-session``, the client's ``clientInfo`` as ``x-bettermemory-client`` and
``-client-version`` unless the environment declared them, and this
process's working directory as ``x-bettermemory-workspace`` unless one
was declared, because the daemon's own working directory says nothing
about the client.

When no daemon can be reached or started, the shim serves the store
in-process, the 8.x shape, and says so once on stderr: a broken daemon
degrades to slower memory, never to no memory.

Once it serves, the shim follows its daemon through a restart. A
restart mints a fresh bearer token and may move the port, so a request
the daemon refuses with 401, or one that finds no daemon listening,
makes the shim read the state file again (starting a daemon only when
none answers, and never stopping one) and send the same request once
more. Both failures happen before the daemon runs anything, so the
resend cannot apply a call twice. The daemon found again must prove
itself the one its state file names (`_daemon_client.identify`, through
`_answering` and `wait_for_daemon`), at start as at a reconnect: a
listener another account left on a crashed daemon's port is not taken
for the daemon, and the resend goes to a daemon started beside it on
another port. Every connection the shim opens to the daemon is proven
on itself before a request goes down it (`_ProvingBackend`): a listener
that took a crashed daemon's port gets the proof's ``/health`` request
and nothing else, no token and no call, and the call that met it is
resent to the daemon found again, since nothing of it was sent. Before
9.0.0rc14 the proof was checked only where the shim found its daemon,
and a call sent to the remembered port after a crash reached whatever
answered there, token included. Any other failure between the shim
and the daemon answers the call that met it and leaves the shim up: the
SDK's transport would otherwise let the exception end its task group
and the process with it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from collections.abc import Callable, Iterable
from typing import Any, cast

import anyio
import anyio.to_thread
import h11
import httpcore2
import httpx2

log = logging.getLogger("bettermemory.shim")

#: JSON-RPC's internal error, the code a failed forward answers with.
_INTERNAL_ERROR = -32603


def _client_info_of(ctx: Any) -> tuple[str | None, str | None]:
    session = getattr(ctx, "session", None)
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None)
    if info is None:
        info = getattr(params, "clientInfo", None)
    if info is None:
        return None, None
    name = getattr(info, "name", None)
    version = getattr(info, "version", None)
    return (str(name) if name else None, str(version) if version else None)


def _forward_headers(ctx: Any) -> dict[str, str]:
    """The identity headers for one forwarded request."""
    from . import identity

    headers: dict[str, str] = {}
    env = os.environ
    for env_key, header in (
        (identity.ENV_CLIENT, identity.HEADER_CLIENT),
        (identity.ENV_CLIENT_VERSION, identity.HEADER_CLIENT_VERSION),
        (identity.ENV_MODEL, identity.HEADER_MODEL),
        (identity.ENV_WORKSPACE, identity.HEADER_WORKSPACE),
        (identity.ENV_HARNESS_SESSION, identity.HEADER_HARNESS_SESSION),
    ):
        value = env.get(env_key)
        if value:
            headers[header] = value.strip()
    if (
        identity.HEADER_CLIENT not in headers
        or identity.HEADER_CLIENT_VERSION not in headers
    ):
        name, version = _client_info_of(ctx)
        if name and identity.HEADER_CLIENT not in headers:
            headers[identity.HEADER_CLIENT] = name
        if version and identity.HEADER_CLIENT_VERSION not in headers:
            headers[identity.HEADER_CLIENT_VERSION] = version
    if identity.HEADER_WORKSPACE not in headers:
        try:
            headers[identity.HEADER_WORKSPACE] = os.getcwd()
        except OSError:
            pass
    return headers


def _rebind(headers: httpx2.Headers, forwarded: dict[str, str]) -> None:
    """Replace the client's identity headers with one call's: every
    ``x-bettermemory-*`` header but the per-process session goes, then
    the call's own (`_forward_headers`, the harness session among them)
    are set. A header the call does not carry is not left over from the
    previous one."""
    from .identity import HEADER_SESSION

    for key in list(headers):
        if key.lower().startswith("x-bettermemory-") and key.lower() != HEADER_SESSION:
            del headers[key]
    headers.update(forwarded)


def _error_reply(request: httpx2.Request, message: str) -> httpx2.Response:
    """A JSON-RPC error as the daemon's reply to `request`, which the SDK
    hands to the caller as an MCPError carrying `message`."""
    try:
        request_id = json.loads(request.content).get("id")
    except (ValueError, AttributeError, httpx2.RequestNotRead):
        request_id = None
    return httpx2.Response(
        503,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": _INTERNAL_ERROR, "message": message},
        },
        request=request,
    )


#: What `_Upstream.refresh` runs in a worker thread: the state of a
#: daemon that answers, or None and why none could be reached or started.
Finder = Callable[[], tuple[dict[str, Any] | None, str | None]]


class _Upstream:
    """The daemon the shim forwards to, as the state file last named it.
    `generation` counts the refreshes, so a request that failed against
    an old port or token can tell whether another request has already
    found the daemon again."""

    def __init__(self, state: dict[str, Any], find: Finder) -> None:
        self.port = int(state["port"])
        self.token = str(state["token"])
        self.generation = 0
        self._find = find
        self._lock = anyio.Lock()

    async def refresh(self, seen: int) -> str | None:
        """Find the daemon again after a request made at generation
        `seen` was refused. None once it is found, by this call or by one
        that refreshed first; else why it could not be."""
        async with self._lock:
            if self.generation != seen:
                return None
            state, why = await anyio.to_thread.run_sync(self._find)
            if state is None:
                return why or "no daemon answered"
            self.port = int(state["port"])
            self.token = str(state["token"])
            self.generation += 1
            log.info(
                "reconnected to the daemon at 127.0.0.1:%s (pid %s, version %s)",
                state["port"],
                state["pid"],
                state["version"],
            )
            return None


async def _prove(
    stream: httpcore2.AsyncNetworkStream,
    *,
    host: str,
    port: int,
    token: str,
    timeout: float | None,
) -> str | None:
    """None when the listener at the other end of `stream`, a connection
    just opened, proves on it that it holds `token`; else what it did
    instead.

    The exchange `_daemon_client._proven_connection` makes for a hook,
    spoken over the pool's own stream with `h11`, the HTTP/1.1 machine
    httpcore2 then runs on it: ``GET /health`` with a fresh nonce, the
    whole reply read, and judged by the rule a hook's proof is judged by
    (`_daemon_client.proof_failure`). A reply that leaves the connection
    closing (``Connection: close``, HTTP/1.0) fails it, since the request
    after it could not go down this connection."""
    from ._daemon_client import (
        HEALTH_NONCE_PARAM,
        HEALTH_PATH,
        PROOF_REPLY_CAP,
        proof_failure,
    )

    nonce = secrets.token_hex(16)
    machine = h11.Connection(h11.CLIENT)
    request = h11.Request(
        method="GET",
        target=f"{HEALTH_PATH}?{HEALTH_NONCE_PARAM}={nonce}",
        headers=[("Host", f"{host}:{port}"), ("Accept", "application/json")],
    )
    sent = (machine.send(request), machine.send(h11.EndOfMessage()))
    await stream.write(b"".join(part for part in sent if part), timeout=timeout)
    status = 0
    body = b""
    while True:
        event = machine.next_event()
        if event is h11.NEED_DATA:
            machine.receive_data(await stream.read(PROOF_REPLY_CAP, timeout=timeout))
        elif isinstance(event, h11.Response):
            status = event.status_code
        elif isinstance(event, h11.Data):
            body += bytes(event.data)
            if len(body) > PROOF_REPLY_CAP:
                break
        elif isinstance(event, h11.EndOfMessage):
            break
        else:
            return "no complete reply"
    closes = machine.their_state is not h11.DONE
    return proof_failure(status, closes, body, token, nonce)


class _ProvingBackend(httpcore2.AsyncNetworkBackend):
    """The network under one of the shim's connection pools
    (`_ProvenTransport`), bound to the one `port` and `token` that pool's
    requests carry. A TCP connection it opens goes to the pool only once
    the listener has proven on it that it holds `token` (`_prove`), so the
    token never goes down a connection to anything else, nor down one
    whose listener proved another token. A connection the pool keeps is
    the proven listener's for as long as it stays open; once it is lost
    (reset, timed out, closed by either side) the pool opens another,
    which is proven before its first request. A listener that does not
    prove itself fails the connect (`httpcore2.ConnectError`, nothing sent
    to it but the proof's request), which `_ForwardingTransport` reads as
    no daemon at the port: it finds the daemon again and resends."""

    def __init__(self, port: int | None, token: str) -> None:
        self._port = port
        self._token = token
        # httpcore2 declares a stand-in `AnyIOBackend` for when anyio is
        # missing, which the type checkers read; anyio comes with mcp.
        self._network = cast(httpcore2.AsyncNetworkBackend, httpcore2.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        if port != self._port:
            raise httpcore2.ConnectError(
                f"the shim holds no token for {host}:{port}; nothing was sent to it"
            )
        stream = await self._network.connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        try:
            why = await _prove(
                stream, host=host, port=port, token=self._token, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - every failure fails the connect
            why = f"the proof's exchange failed: {exc.__class__.__name__}"
        except BaseException:
            with anyio.CancelScope(shield=True):
                await stream.aclose()
            raise
        if why is not None:
            with anyio.CancelScope(shield=True):
                await stream.aclose()
            raise httpcore2.ConnectError(
                f"the listener at {host}:{port} did not prove it is the "
                f"bettermemory daemon its state file names ({why}); nothing "
                "was sent to it"
            )
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        raise httpcore2.ConnectError("the shim reaches its daemon over TCP only")

    async def sleep(self, seconds: float) -> None:
        await self._network.sleep(seconds)


def _unprovable_httpx2(
    transport: httpx2.AsyncHTTPTransport | None = None,
) -> str | None:
    """Why this httpx2 cannot carry the shim's proving pool; None when it
    can. `_ProvenTransport` puts its pool, the one whose every connection
    is proven (`_ProvingBackend`), where httpx2's `AsyncHTTPTransport`
    keeps its own: the private `_pool`, which `__init__` sets and
    `handle_async_request` reads (httpx2 2.16). An httpx2 that kept it
    under another name would send each request, token and all, down a
    pool the shim never made, its connections unproven, with nothing
    raised; mcp, which brings httpx2, sets it no upper bound. So both
    methods are read for the name, and a `transport` just made is read
    for the pool itself: any doubt is a refusal, never a fallback to an
    unproven pool."""
    cls = httpx2.AsyncHTTPTransport
    names = [
        getattr(getattr(method, "__code__", None), "co_names", ())
        for method in (cls.__init__, cls.handle_async_request)
    ]
    kept = transport is None or isinstance(
        vars(transport).get("_pool"), httpcore2.AsyncConnectionPool
    )
    if kept and all("_pool" in found for found in names):
        return None
    version = getattr(httpx2, "__version__", "of an unknown version")
    return (
        f"httpx2 {version} keeps its connection pool where the shim cannot prove "
        "its connections to the daemon"
    )


def refuse_unprovable_httpx2() -> None:
    """End the shim before it starts when this httpx2 cannot carry the
    proving pool (`_unprovable_httpx2`): one line naming httpx2's version,
    and nothing sent to any daemon. `run_shim` asks first, before it looks
    for a daemon, so the shim never runs on a pool it cannot prove."""
    why = _unprovable_httpx2()
    if why is not None:
        raise SystemExit(
            f"bettermemory: {why}; the shim does not start, and nothing was sent "
            "to the daemon. Install bettermemory with the httpx2 its release "
            "locks (docs/operations.md) and start it again."
        )


class _ProvenTransport(httpx2.AsyncHTTPTransport):
    """httpx2's own transport, its connection pool opening connections
    through a `_ProvingBackend` bound to `port` and `token`, with the
    limits httpx2 gives a transport by default. Made only where httpx2
    keeps its pool as 2.16 does (`_unprovable_httpx2`): elsewhere it
    raises, and no request is sent."""

    def __init__(self, port: int | None, token: str) -> None:
        super().__init__()
        why = _unprovable_httpx2(self)
        if why is not None:
            raise RuntimeError(f"{why}; nothing was sent")
        self._pool = httpcore2.AsyncConnectionPool(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=_ProvingBackend(port, token),
        )


class _DaemonTransport(httpx2.AsyncBaseTransport):
    """The transport under `_ForwardingTransport` (the `inner` the shim
    gives it): each request goes to the `_ProvenTransport` of the port
    and the token it carries, its URL's port and its bearer token, made
    the first time a request carries them. So a connection only ever
    carries the token its listener proved on it, whatever `_Upstream`
    holds by the time the connection is opened. The pool of a port and
    token the requests no longer carry (a refresh found the daemon again)
    takes no new request and is closed with the shim; there is one per
    daemon the shim has followed."""

    def __init__(self) -> None:
        why = _unprovable_httpx2()
        if why is not None:
            raise RuntimeError(f"{why}; nothing was sent")
        self._proven: dict[tuple[int | None, str], _ProvenTransport] = {}

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        _, _, token = request.headers.get("Authorization", "").partition(" ")
        key = (request.url.port, token)
        transport = self._proven.get(key)
        if transport is None:
            transport = self._proven[key] = _ProvenTransport(*key)
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in self._proven.values():
            await transport.aclose()


class _ForwardingTransport(httpx2.AsyncBaseTransport):
    """The shim's transport to the daemon. Every request goes to the
    port and with the token `upstream` holds, down a connection whose
    listener proved it holds that token when the connection was opened
    (`_DaemonTransport`, the `inner` the shim gives it). A request refused
    before the daemon ran it (no connection, a listener that does not
    prove itself, or 401) refreshes `upstream` and goes once more; every
    other failure comes back as an error reply to the request that met
    it, never as an exception."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, upstream: _Upstream) -> None:
        self._inner = inner
        self._upstream = upstream

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        seen = self._upstream.generation
        first = await self._attempt(request)
        if isinstance(first, httpx2.Response):
            return first
        why = await self._upstream.refresh(seen)
        if why is not None:
            return _error_reply(
                request,
                f"no bettermemory daemon could be reached or started ({why}); "
                f"the request met: {first}",
            )
        second = await self._attempt(request)
        if isinstance(second, httpx2.Response):
            return second
        return _error_reply(request, f"{second}, after one reconnect")

    async def _attempt(self, request: httpx2.Request) -> httpx2.Response | str:
        """The daemon's response, or why the daemon refused the request
        before running it."""
        upstream = self._upstream
        request.url = request.url.copy_with(port=upstream.port)
        request.headers["Host"] = request.url.netloc.decode("ascii")
        request.headers["Authorization"] = f"Bearer {upstream.token}"
        try:
            response = await self._inner.handle_async_request(request)
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
            return (
                f"no bettermemory daemon answered at {request.url.netloc.decode()}: "
                f"{exc.__class__.__name__}: {exc}"
            )
        except httpx2.TransportError as exc:
            return _error_reply(
                request,
                "the connection to the bettermemory daemon was lost before its "
                f"reply ({exc.__class__.__name__}: {exc}); the call may have been "
                "applied, so check before repeating it",
            )
        if response.status_code == 401:
            await response.aclose()
            return "the bettermemory daemon refused the shim's token"
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _serve_through_daemon(
    state: dict[str, Any], version: str, find: Finder
) -> None:
    from mcp import ClientSession
    from mcp import types
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    from .identity import HEADER_SESSION

    session_id = "sess_" + secrets.token_hex(8)
    url = f"http://127.0.0.1:{state['port']}/mcp"
    transport = _ForwardingTransport(_DaemonTransport(), _Upstream(state, find))
    async with httpx2.AsyncClient(
        headers={HEADER_SESSION: session_id},
        timeout=httpx2.Timeout(60.0, connect=5.0),
        transport=transport,
    ) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(
                read,
                write,
                client_info=types.Implementation(
                    name="bettermemory-shim", version=version
                ),
            ) as upstream:
                init = await upstream.initialize()

                def _bind(ctx: Any) -> None:
                    _rebind(http.headers, _forward_headers(ctx))

                async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
                    _bind(ctx)
                    return await upstream.list_tools()

                async def on_call_tool(ctx: Any, params: Any) -> Any:
                    _bind(ctx)
                    return await upstream.call_tool(params.name, params.arguments or {})

                server: Server[Any] = Server(
                    "bettermemory",
                    version=version,
                    instructions=init.instructions,
                    on_list_tools=on_list_tools,
                    on_call_tool=on_call_tool,
                )
                async with stdio_server() as (stdin, stdout):
                    await server.run(
                        stdin, stdout, server.create_initialization_options()
                    )


def run_shim() -> None:
    """The default entry point: find or start the daemon for the store
    this process would open, and serve stdio in front of it."""
    from ._daemon_client import (
        START_TIMEOUT_SECONDS,
        DaemonExited,
        _answering,
        daemon_env_for,
        ensure_daemon,
        held_lock_reason,
        package_version,
        resolve_state_dir,
        resolved_store_path,
        start_and_wait,
    )
    from .config import load_config

    refuse_unprovable_httpx2()
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    version = package_version()
    config = load_config()
    store_path = resolved_store_path()
    state_dir = resolve_state_dir()
    if not store_path.is_file():
        from .cli.serve import exit_if_unmigrated_v8

        exit_if_unmigrated_v8(store_path)
    from .cli._common import exit_if_bundle_file

    # A store directory that is a backup bundle: the daemon and the
    # in-process fallback both refuse it, so refuse it here, in one line,
    # before a daemon is spawned only to exit.
    exit_if_bundle_file(store_path)
    env = daemon_env_for(store_path)
    failed: Exception | None = None
    try:
        state = ensure_daemon(
            state_dir,
            store_path,
            version=version,
            port=config.daemon.port,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001 - the fallback below is the answer
        failed, state = exc, None
    if state is None:
        from .cli.serve import open_for_serve, run_serve

        def falling_back() -> None:
            if failed is not None:
                log.warning(
                    "daemon lookup failed: %s: %s", failed.__class__.__name__, failed
                )
            log.warning(
                "no daemon could be reached or started for %s; serving the store "
                "in-process",
                store_path,
            )

        # The fallback's store is opened before the shim says it falls
        # back: a store it cannot open either (a key file that is there
        # and cannot be read, which stopped the daemon too) ends the shim
        # in the opener's one line and exit 1 (a `SystemExit`), not in that
        # line after two warnings about an in-process server that never
        # starts. Any other failure of the open has no such line, and its
        # traceback alone would hide why the daemon was not there (its
        # error and its log, which `DaemonExited` names): the warnings
        # come first then, as they always did.
        try:
            opened = open_for_serve()
        except Exception:
            falling_back()
            raise
        falling_back()
        run_serve(opened)
        return
    log.info(
        "stdio shim for the daemon at 127.0.0.1:%s (pid %s)",
        state["port"],
        state["pid"],
    )

    def find() -> tuple[dict[str, Any] | None, str | None]:
        # Not ensure_daemon: a reconnect finds the daemon again, it does
        # not upgrade it, so an older daemon that came up meanwhile is not
        # replaced here either. Whatever daemon answers and proves itself
        # the state file's (`_answering`) serves; one is started only when
        # none does, and another listener on the port counts as none. None
        # is started while another process holds the store's lock: that
        # daemon is waited for instead (`start_and_wait`).
        found = _answering(state_dir, store_path)
        if found is not None:
            return found, None
        try:
            found = start_and_wait(
                state_dir,
                store_path,
                port=config.daemon.port,
                env=env,
                timeout=START_TIMEOUT_SECONDS,
            )
        except (DaemonExited, OSError) as exc:
            return None, f"{exc.__class__.__name__}: {exc}"
        if found is None:
            # With the store's lock still held the wait may have started
            # nothing (`start_and_wait` starts none beside a holder), so
            # the reason names the held lock rather than a start, in the
            # words `up` uses too (`held_lock_reason`).
            held = held_lock_reason(
                state_dir, store_path, timeout=START_TIMEOUT_SECONDS
            )
            if held is not None:
                return None, held
            return None, f"none answered within {START_TIMEOUT_SECONDS:g} s of a start"
        return found, None

    anyio.run(_serve_through_daemon, state, version, find)


__all__ = ["run_shim"]
