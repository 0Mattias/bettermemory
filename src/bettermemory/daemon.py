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
the stdio shim sets per process (`shim.py`). The hook bodies are
`hook.py`'s own functions on the daemon's store, so a hook is one HTTP
round trip instead of a process that imports the SDK and opens the store.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

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

    served_version = version if version is not None else _version()
    mcp = build_server(config=config, store=store)

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
        try:
            block, note = _hook.session_start_block(store, cwd=_cwd_of(payload))
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the hook
            log.warning(
                "hook session-start failed: %s: %s", exc.__class__.__name__, exc
            )
            return JSONResponse(
                {"stdout": "", "error": f"{exc.__class__.__name__}: {exc}"},
                status_code=500,
            )
        if note:
            log.info("%s", note)
        return JSONResponse({"stdout": block or ""})

    @_route(mcp, "/api/v1/hook/prompt", ["POST"])
    async def hook_prompt(request: Request) -> Response:
        payload = await _json_body(request)
        prompt = payload.get("prompt")
        session_id = payload.get("session_id")
        if not isinstance(prompt, str) or not prompt or not session_id:
            return JSONResponse({"stdout": ""})
        try:
            block = _hook.run_prompt_recall(
                prompt=prompt,
                session_id=str(session_id),
                config=config,
                store=store,
                cwd=_cwd_of(payload),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("hook prompt failed: %s: %s", exc.__class__.__name__, exc)
            return JSONResponse(
                {"stdout": "", "error": f"{exc.__class__.__name__}: {exc}"},
                status_code=500,
            )
        return JSONResponse({"stdout": block or ""})

    @_route(mcp, "/api/v1/hook/stop", ["POST"])
    async def hook_stop(request: Request) -> Response:
        payload = await _json_body(request)
        transcript = payload.get("transcript_path")
        session_id = payload.get("session_id")
        if not transcript or not session_id:
            return JSONResponse({"stdout": "", "result": None})
        try:
            result = _hook.audit_transcript(
                transcript_path=Path(str(transcript)),
                session_id=str(session_id),
                config=config,
                store=store,
                cwd=_cwd_of(payload),
                dry_run=bool(payload.get("dry_run", False)),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("hook stop failed: %s: %s", exc.__class__.__name__, exc)
            return JSONResponse(
                {
                    "stdout": "",
                    "result": None,
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
                status_code=500,
            )
        if result is None:
            return JSONResponse({"stdout": "", "result": None})
        import json as _json

        stdout = (
            "" if payload.get("quiet", True) else _json.dumps(result, sort_keys=True)
        )
        return JSONResponse({"stdout": stdout, "result": result})

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
