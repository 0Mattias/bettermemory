"""The daemon's HTTP app, driven in-process through an ASGI transport.

One daemon owns the store, the recorder and the session registry; the
MCP surface is served at ``/mcp`` over streamable HTTP, the three hook
bodies at ``/api/v1/hook/<event>``, and ``/health`` is the only open
route. Everything else needs the bearer token the state file carries.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bettermemory import __version__
from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.daemon import build_app
from bettermemory.identity import HEADER_SESSION
from bettermemory.store import Store

TOKEN = "test-token-0123456789abcdef"
BASE = "http://127.0.0.1:7397"
NINE = {
    "memory_search",
    "memory_show",
    "memory_write",
    "memory_update",
    "memory_remove",
    "memory_verify",
    "memory_record_use",
    "episode",
    "memory_admin",
}


@pytest.fixture
def daemon_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


@pytest.fixture
def daemon_config(tmp_path: Path) -> Config:
    return Config(
        storage=StorageConfig(directory=str(tmp_path / "store")),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )


@asynccontextmanager
async def running_app(
    config: Config, store: Store, *, shutdown_calls: list[int] | None = None
) -> AsyncIterator[httpx2.AsyncClient]:
    """The app inside its lifespan (the SDK's session manager must be
    running before the first ``/mcp`` request), behind an ASGI transport."""
    calls = shutdown_calls if shutdown_calls is not None else []
    app = build_app(
        config=config,
        store=store,
        token=TOKEN,
        shutdown=lambda: calls.append(1),
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as client:
            yield client


def _auth(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", **extra}


@asynccontextmanager
async def mcp_session(
    client: httpx2.AsyncClient, *, headers: dict[str, str]
) -> AsyncIterator[ClientSession]:
    client.headers.update(headers)
    async with streamable_http_client(f"{BASE}/mcp", http_client=client) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            yield session


async def test_health_answers_without_a_token(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["store"] == str(daemon_store.path)
    assert isinstance(body["pid"], int)


async def test_every_other_route_needs_the_token(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        for path in (
            "/api/v1/hook/session-start",
            "/api/v1/hook/prompt",
            "/api/v1/hook/stop",
            "/api/v1/admin/shutdown",
        ):
            response = await client.post(path, json={})
            assert response.status_code == 401, path
        wrong = await client.post(
            "/api/v1/hook/session-start",
            json={},
            headers={"Authorization": "Bearer nope"},
        )
        assert wrong.status_code == 401
        mcp = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
        assert mcp.status_code == 401


async def test_tools_list_through_mcp_is_the_nine(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        async with mcp_session(client, headers=_auth()) as session:
            listed = await session.list_tools()
    assert {tool.name for tool in listed.tools} == NINE


async def test_write_then_search_round_trip(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        async with mcp_session(client, headers=_auth()) as session:
            written = await session.call_tool(
                "memory_write",
                {
                    "content": "the daemon serves the store over streamable http",
                    "scopes": ["tools"],
                },
            )
            assert not written.is_error
            found = await session.call_tool(
                "memory_search", {"query": "daemon streamable http", "max_results": 3}
            )
    assert not found.is_error
    text = "".join(getattr(block, "text", "") for block in found.content)
    assert "streamable http" in text


async def test_two_sessions_keep_separate_disabled_scopes(
    daemon_config: Config, daemon_store: Store
) -> None:
    """The shim sets x-bettermemory-session per process; two of them
    against one daemon must not share session state."""
    daemon_store.write(content="a fact filed under tools", scopes=["tools"])
    async with running_app(daemon_config, daemon_store) as client:
        async with mcp_session(
            client, headers=_auth(**{HEADER_SESSION: "sess_a"})
        ) as a:
            disabled = await a.call_tool(
                "memory_admin", {"action": "disable_scope", "scope": "tools"}
            )
            assert not disabled.is_error
            hidden = await a.call_tool("memory_search", {"query": "fact filed tools"})
            assert "a fact filed under tools" not in "".join(
                getattr(block, "text", "") for block in hidden.content
            )
        async with mcp_session(
            client, headers=_auth(**{HEADER_SESSION: "sess_b"})
        ) as b:
            visible = await b.call_tool("memory_search", {"query": "fact filed tools"})
    assert "a fact filed under tools" in "".join(
        getattr(block, "text", "") for block in visible.content
    )


async def test_session_start_endpoint_returns_the_block(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    daemon_store.write(content="a global preference", scopes=["personal-context"])
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/session-start", json={"cwd": str(tmp_path)}, headers=_auth()
        )
    assert response.status_code == 200
    body = response.json()
    assert body["stdout"].startswith(
        "bettermemory: 1 memory is in scope for this repository."
    )


async def test_session_start_endpoint_is_silent_on_an_empty_store(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/session-start", json={"cwd": str(tmp_path)}, headers=_auth()
        )
    assert response.status_code == 200
    assert response.json()["stdout"] == ""


async def test_prompt_endpoint_returns_nothing_without_a_miss(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/prompt",
            json={
                "cwd": str(tmp_path),
                "session_id": "sess_hook",
                "prompt": "hello there",
            },
            headers=_auth(),
        )
    assert response.status_code == 200
    assert response.json()["stdout"] == ""


async def test_stop_endpoint_audits_the_transcript(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    rows: list[dict[str, Any]] = [
        {
            "type": "user",
            "message": {"role": "user", "content": "what is our backup strategy"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "I do not know."}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/stop",
            json={
                "cwd": str(tmp_path),
                "session_id": "sess_hook",
                "transcript_path": str(transcript),
                "quiet": False,
            },
            headers=_auth(),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["verdict"] in {"ok", "miss", "no_signal", "suppressed"}
    assert json.loads(body["stdout"])["verdict"] == body["result"]["verdict"]
    kinds = [event["kind"] for event in daemon_store.iter_events()]
    assert "turn_audited" in kinds


async def test_shutdown_endpoint_calls_the_hook(
    daemon_config: Config, daemon_store: Store
) -> None:
    calls: list[int] = []
    async with running_app(daemon_config, daemon_store, shutdown_calls=calls) as client:
        response = await client.post("/api/v1/admin/shutdown", headers=_auth())
    assert response.status_code == 200
    assert calls == [1]


async def test_a_cli_write_is_visible_to_the_next_request(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    """The daemon holds no cache of rows: a second connection to the
    same file (what a CLI command opens) writes, and the daemon's next
    search finds it."""
    async with running_app(daemon_config, daemon_store) as client:
        with Store(tmp_path / "store") as other:
            other.write(content="written by another process entirely", scopes=["tools"])
        async with mcp_session(client, headers=_auth()) as session:
            found = await session.call_tool(
                "memory_search", {"query": "written by another process"}
            )
    assert "written by another process" in "".join(
        getattr(block, "text", "") for block in found.content
    )
