"""The bettermemory daemon: one process that owns the store, the recorder
and the session registry, serving the MCP surface over streamable HTTP at
``/mcp`` and the three hook bodies at ``/api/v1/hook/<event>``.

Binds 127.0.0.1 only. Every request except ``GET /health`` carries the
bearer token the state file holds (`_daemon_client.write_state`); a
process running as the user can read that file, which is the threat
model of a localhost daemon. The SDK's DNS-rebinding protection stays on
(Host and Origin restricted to localhost).

The MCP app is the one `builder.build_server` returns, served stateless
with JSON responses for 2026-07-28 clients; per-client session state is
keyed by `identity.registry_key` on the ``x-bettermemory-session`` header
the stdio shim sets per process (`shim.py`). The hook bodies are the
recall surface's (`recall.session_brief`, `recall.prompt_recall`) and the
Stop audit's (`hook.audit_transcript`), so a hook is one HTTP round trip
instead of a process that imports the SDK and opens the store.

The session-start and prompt bodies run on a worker thread
(`anyio.to_thread.run_sync`), so their store reads and their git work
never block the event loop the MCP surface is served from. SQLite binds a
connection to the thread that opened it, so each worker thread reads and
records through its own connection to the store (`_WorkerStores`). The
recall surface's per-session state (`recall.RecallState`) is the
daemon's, one for its lifetime, lock-guarded.

Every hook endpoint answers ``stdout`` (the text the harness injects,
unchanged) and beside it ``surface``, ``ids``, ``chars``, ``rule`` and
``event_id``: what an adapter for another harness wraps.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

import anyio.to_thread
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ._daemon_client import HOST, remove_state, write_state
from .builder import build_server
from .config import Config
from .store import Store

log = logging.getLogger("bettermemory.daemon")

OPEN_PATHS: frozenset[str] = frozenset({"/health"})


class BearerAuthMiddleware:
    """Pure ASGI: 401 on every HTTP request without the daemon's token,
    except the open paths."""

    def __init__(self, app: ASGIApp, *, token: str, open_paths: frozenset[str]) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")
        self._open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._open_paths:
            await self.app(scope, receive, send)
            return
        supplied = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                supplied = value
                break
        if not secrets.compare_digest(supplied, self._expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


Handler = Callable[[Request], Awaitable[Response]]


def _route(mcp: Any, path: str, methods: list[str]) -> Callable[[Handler], Handler]:
    """`MCPServer.custom_route` with a type: the SDK leaves the decorator
    untyped, and the daemon's routes are plain Starlette handlers."""
    return cast(Callable[[Handler], Handler], mcp.custom_route(path, methods=methods))


def _version() -> str:
    from . import __version__

    return __version__


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _cwd_of(payload: dict[str, Any]) -> Path | None:
    cwd = payload.get("cwd")
    return Path(str(cwd)) if isinstance(cwd, str) and cwd else None


def _text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


class _WorkerStores:
    """One connection to the daemon's store per worker thread.

    SQLite refuses a connection on any thread but the one that opened it,
    and the hook bodies run on `anyio`'s worker threads, so each thread
    opens its own (`Store.reopen`) on first use and keeps it for the
    thread's life. The connections share the file, its WAL and its key;
    every write is its own ``BEGIN IMMEDIATE`` transaction, the same
    discipline a CLI process writing beside the daemon already relies on."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._local = threading.local()

    def get(self) -> Store:
        held = getattr(self._local, "store", None)
        if held is None:
            held = self._store.reopen()
            self._local.store = held
        return cast(Store, held)


def _failed(surface: str, exc: Exception) -> Response:
    return JSONResponse(
        {
            "stdout": "",
            "surface": surface,
            "ids": [],
            "chars": 0,
            "rule": None,
            "event_id": None,
            "error": f"{exc.__class__.__name__}: {exc}",
        },
        status_code=500,
    )


def build_app(
    *,
    config: Config,
    store: Store,
    token: str,
    shutdown: Callable[[], None],
    version: str | None = None,
) -> Starlette:
    """The daemon's ASGI app: the MCP surface at ``/mcp``, the hook
    endpoints, ``/health`` and the shutdown endpoint, behind the bearer
    check. `shutdown` is called by ``POST /api/v1/admin/shutdown``."""
    from . import hook as _hook
    from . import recall as _recall

    served_version = version if version is not None else _version()
    mcp = build_server(config=config, store=store)
    recall_state = _recall.RecallState(background=True)
    worker_stores = _WorkerStores(store)

    @_route(mcp, "/health", ["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "version": served_version,
                "store": str(store.path),
                "pid": os.getpid(),
            }
        )

    @_route(mcp, "/api/v1/hook/session-start", ["POST"])
    async def hook_session_start(request: Request) -> Response:
        payload = await _json_body(request)

        def body() -> _recall.HookResult:
            return _recall.session_brief(
                session_id=_text(payload, "session_id"),
                source=_text(payload, "source"),
                harness=_text(payload, "harness"),
                transcript_path=_text(payload, "transcript_path"),
                cwd=_cwd_of(payload),
                config=config,
                store=worker_stores.get(),
                state=recall_state,
            )

        try:
            result = await anyio.to_thread.run_sync(body)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the hook
            log.warning(
                "hook session-start failed: %s: %s", exc.__class__.__name__, exc
            )
            return _failed("session_brief", exc)
        return JSONResponse(result.to_response())

    @_route(mcp, "/api/v1/hook/prompt", ["POST"])
    async def hook_prompt(request: Request) -> Response:
        payload = await _json_body(request)
        prompt = _text(payload, "prompt")
        session_id = payload.get("session_id")
        if prompt is None or not session_id:
            return JSONResponse(_recall.HookResult.empty("prompt_recall").to_response())

        def body() -> _recall.HookResult:
            return _recall.prompt_recall(
                prompt=prompt,
                session_id=str(session_id),
                harness=_text(payload, "harness"),
                prompt_id=_text(payload, "prompt_id"),
                transcript_path=_text(payload, "transcript_path"),
                cwd=_cwd_of(payload),
                config=config,
                store=worker_stores.get(),
                state=recall_state,
            )

        try:
            result = await anyio.to_thread.run_sync(body)
        except Exception as exc:  # noqa: BLE001
            log.warning("hook prompt failed: %s: %s", exc.__class__.__name__, exc)
            return _failed("prompt_recall", exc)
        return JSONResponse(result.to_response())

    @_route(mcp, "/api/v1/hook/stop", ["POST"])
    async def hook_stop(request: Request) -> Response:
        payload = await _json_body(request)
        transcript = payload.get("transcript_path")
        session_id = payload.get("session_id")
        quiet_fields: dict[str, Any] = {
            "stdout": "",
            "result": None,
            "surface": "turn_audit",
            "ids": [],
            "chars": 0,
            "rule": None,
            "event_id": None,
        }
        if not transcript or not session_id:
            return JSONResponse(quiet_fields)
        try:
            result = _hook.audit_transcript(
                transcript_path=Path(str(transcript)),
                session_id=str(session_id),
                config=config,
                store=store,
                cwd=_cwd_of(payload),
                dry_run=bool(payload.get("dry_run", False)),
                assistant_message=_text(payload, "last_assistant_message"),
                harness=_text(payload, "harness"),
                prompt_id=_text(payload, "prompt_id"),
                state=recall_state,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("hook stop failed: %s: %s", exc.__class__.__name__, exc)
            return JSONResponse(
                {**quiet_fields, "error": f"{exc.__class__.__name__}: {exc}"},
                status_code=500,
            )
        if result is None:
            return JSONResponse(quiet_fields)
        import json as _json

        stdout = (
            "" if payload.get("quiet", True) else _json.dumps(result, sort_keys=True)
        )
        return JSONResponse(
            {
                **quiet_fields,
                "stdout": stdout,
                "result": result,
                "chars": len(stdout),
                "rule": result.get("threshold_rule"),
            }
        )

    @_route(mcp, "/api/v1/admin/shutdown", ["POST"])
    async def admin_shutdown(request: Request) -> Response:
        shutdown()
        return JSONResponse({"status": "stopping"})

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=HOST,
    )
    app.add_middleware(BearerAuthMiddleware, token=token, open_paths=OPEN_PATHS)
    return app


def bind(host: str, port: int) -> socket.socket:
    """A bound, non-listening socket on `host`: `port` when it is free,
    else an ephemeral one. The caller reads the port it got."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        if port == 0:
            sock.close()
            raise
        sock.bind((host, 0))
    sock.setblocking(False)
    return sock


def serve(
    *,
    config: Config,
    store: Store,
    state_dir: Path,
    port: int | None = None,
    host: str = HOST,
) -> int:
    """Run the daemon in this process until the shutdown endpoint or a
    signal stops it. Writes the state file after the socket is bound and
    removes it on the way out. Returns the exit status."""
    import uvicorn

    requested = config.daemon.port if port is None else port
    sock = bind(host, requested)
    bound_port = int(sock.getsockname()[1])
    token = secrets.token_hex(32)
    holder: dict[str, Any] = {}

    def shutdown() -> None:
        server = holder.get("server")
        if server is not None:
            server.should_exit = True

    app = build_app(config=config, store=store, token=token, shutdown=shutdown)
    state = {
        "pid": os.getpid(),
        "port": bound_port,
        "token": token,
        "version": _version(),
        "store": str(store.path),
        "started": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    write_state(state_dir, state)
    log.info(
        "bettermemory daemon %s on %s:%d, store %s",
        state["version"],
        host,
        bound_port,
        store.path,
    )
    uv_config = uvicorn.Config(
        app,
        host=host,
        port=bound_port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)
    holder["server"] = server
    try:
        server.run(sockets=[sock])
    finally:
        remove_state(state_dir, store.path, pid=os.getpid())
        sock.close()
    return 0


__all__ = ["BearerAuthMiddleware", "OPEN_PATHS", "bind", "build_app", "serve"]
