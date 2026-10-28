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
listener another account left on a crashed daemon's port, which refuses
the token, is not taken for the daemon, and the resend goes to a daemon
started beside it on another port. The proof is checked where the shim
finds its daemon, not on every call; a call sent to the remembered port
in the moment between a crash and that port's capture still reaches
whatever answers there. Any other failure between the shim
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
from collections.abc import Callable
from typing import Any

import anyio
import anyio.to_thread
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


class _ForwardingTransport(httpx2.AsyncBaseTransport):
    """The shim's transport to the daemon. Every request goes to the
    port and with the token `upstream` holds. A request refused before
    the daemon ran it (no connection, or 401) refreshes `upstream` and
    goes once more; every other failure comes back as an error reply to
    the request that met it, never as an exception."""

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
    transport = _ForwardingTransport(
        httpx2.AsyncHTTPTransport(), _Upstream(state, find)
    )
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
        _spawn,
        daemon_env_for,
        ensure_daemon,
        package_version,
        resolve_state_dir,
        resolved_store_path,
        wait_for_daemon,
    )
    from .config import load_config

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
    env = daemon_env_for(store_path)
    try:
        state = ensure_daemon(
            state_dir,
            store_path,
            version=version,
            port=config.daemon.port,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001 - the fallback below is the answer
        log.warning("daemon lookup failed: %s: %s", exc.__class__.__name__, exc)
        state = None
    if state is None:
        log.warning(
            "no daemon could be reached or started for %s; serving the store in-process",
            store_path,
        )
        from .cli.serve import run_serve

        run_serve()
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
        # none does, and another listener on the port counts as none.
        found = _answering(state_dir, store_path)
        if found is not None:
            return found, None
        try:
            started = _spawn(state_dir, store_path, port=config.daemon.port, env=env)
            found = wait_for_daemon(
                state_dir, store_path, timeout=START_TIMEOUT_SECONDS, started=started
            )
        except (DaemonExited, OSError) as exc:
            return None, f"{exc.__class__.__name__}: {exc}"
        if found is None:
            return None, f"none answered within {START_TIMEOUT_SECONDS:g} s of a start"
        return found, None

    anyio.run(_serve_through_daemon, state, version, find)


__all__ = ["run_shim"]
