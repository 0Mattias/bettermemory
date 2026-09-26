"""``bettermemory`` with no arguments: a stdio MCP server in front of the
daemon.

A real stdio server, so every client config that names the ``bettermemory``
command keeps working: its ``initialize`` carries the daemon's
instructions and this package's version, its ``tools/list`` is the
daemon's, and its ``tools/call`` forwards over streamable HTTP with the
headers `identity` reads: ``x-bettermemory-session`` (this process's
session id, so two shims against one daemon keep separate session
state), the client's ``clientInfo`` as ``x-bettermemory-client`` and
``-client-version`` unless the environment declared them, and this
process's working directory as ``x-bettermemory-workspace`` unless one
was declared, because the daemon's own working directory says nothing
about the client.

When no daemon can be reached or started, the shim serves the store
in-process, the 8.x shape, and says so once on stderr: a broken daemon
degrades to slower memory, never to no memory.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from typing import Any

log = logging.getLogger("bettermemory.shim")


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


async def _serve_through_daemon(state: dict[str, Any], version: str) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp import types
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    from .identity import HEADER_SESSION

    session_id = "sess_" + secrets.token_hex(8)
    base_headers = {
        "Authorization": f"Bearer {state['token']}",
        HEADER_SESSION: session_id,
    }
    url = f"http://127.0.0.1:{state['port']}/mcp"
    async with httpx2.AsyncClient(
        headers=base_headers, timeout=httpx2.Timeout(60.0, connect=5.0)
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
                    forwarded = _forward_headers(ctx)
                    for key in list(http.headers):
                        if (
                            key.lower().startswith("x-bettermemory-")
                            and key.lower() != HEADER_SESSION
                        ):
                            del http.headers[key]
                    http.headers.update(forwarded)

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
    import anyio

    from ._daemon_client import (
        daemon_env_for,
        ensure_daemon,
        package_version,
        resolve_state_dir,
        resolved_store_path,
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
    try:
        state = ensure_daemon(
            resolve_state_dir(),
            store_path,
            version=version,
            port=config.daemon.port,
            env=daemon_env_for(store_path),
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
    anyio.run(_serve_through_daemon, state, version)


__all__ = ["run_shim"]
