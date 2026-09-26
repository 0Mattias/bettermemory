"""``bettermemory`` with no arguments: the stdio shim in front of the daemon.

The shim is a real stdio MCP server whose initialize carries the
daemon's instructions and this package's version, whose tools/list is
the daemon's, and whose tools/call forwards. It starts a daemon when
none answers, restarts one whose version differs, and serves the store
in-process when no daemon can be reached or started.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bettermemory import __version__
from bettermemory._daemon_client import health, read_state, write_state
from bettermemory.builder import INSTRUCTIONS
from bettermemory.store import Store

from .test_daemon_lifecycle import _cli, _env, _state_path, _store_path, _wait_running

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
def daemon_env(tmp_path: Path) -> dict[str, str]:
    env = _env(tmp_path)
    yield env
    _cli(["down"], env, timeout=30)


def _shim_params(env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "bettermemory"], env=env
    )


async def test_shim_serves_the_daemons_surface(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    state = _wait_running(tmp_path)
    async with stdio_client(_shim_params(daemon_env)) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            assert init.instructions == INSTRUCTIONS
            assert init.server_info.name == "bettermemory"
            assert init.server_info.version == __version__
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == NINE
            written = await session.call_tool(
                "memory_write",
                {
                    "content": "forwarded through the shim to the daemon",
                    "scopes": ["tools"],
                },
            )
            assert not written.is_error
    # The write landed in the daemon's store, not in a store of the shim's own.
    with Store(Path(state["store"]).parent) as store:
        assert any(
            "forwarded through the shim" in memory.body
            for memory in store.iter_active()
        )


async def test_shim_starts_a_daemon_when_none_answers(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert read_state(tmp_path / "state", _store_path(tmp_path)) is None
    started = time.monotonic()
    async with stdio_client(_shim_params(daemon_env)) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            listed = await session.list_tools()
            first_answer = time.monotonic() - started
    assert {tool.name for tool in listed.tools} == NINE
    state = _wait_running(tmp_path)
    assert health(state["port"]) is not None
    # U5-P5: the cold path answers its first tools/list within 1.5 s;
    # MISSED-if over 3 s. The assertion holds the MISSED-if bound.
    assert first_answer < 3.0


async def test_two_shims_share_the_store_and_keep_separate_session_state(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    _wait_running(tmp_path)
    with Store(tmp_path / "store") as store:
        store.write(content="a fact filed under tools", scopes=["tools"])
    async with stdio_client(_shim_params(daemon_env)) as (ra, wa):
        async with ClientSession(ra, wa) as a:
            await a.initialize()
            assert not (
                await a.call_tool(
                    "memory_admin", {"action": "disable_scope", "scope": "tools"}
                )
            ).is_error
            hidden = await a.call_tool("memory_search", {"query": "fact filed tools"})
            assert "a fact filed under tools" not in _text(hidden)
            async with stdio_client(_shim_params(daemon_env)) as (rb, wb):
                async with ClientSession(rb, wb) as b:
                    await b.initialize()
                    visible = await b.call_tool(
                        "memory_search", {"query": "fact filed tools"}
                    )
                    assert "a fact filed under tools" in _text(visible)
                    written = await b.call_tool(
                        "memory_write",
                        {"content": "written by the second shim", "scopes": ["tools"]},
                    )
                    assert not written.is_error
            found = await a.call_tool(
                "memory_admin", {"action": "enable_scope", "scope": "tools"}
            )
            assert not found.is_error
            seen = await a.call_tool("memory_search", {"query": "written second shim"})
            assert "written by the second shim" in _text(seen)


def _text(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


class _FakeDaemon(http.server.ThreadingHTTPServer):
    shutdown_calls = 0


class _FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D401
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {"status": "ok", "version": "0.0.1", "store": "x", "pid": 1}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/v1/admin/shutdown":
            self.server.shutdown_calls += 1  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


async def test_shim_restarts_a_daemon_of_another_version(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    fake = _FakeDaemon(("127.0.0.1", 0), _FakeHandler)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    state_dir = tmp_path / "state"
    try:
        write_state(
            state_dir,
            {
                "pid": 2**22 + 4321,
                "port": fake.server_address[1],
                "token": "t" * 64,
                "version": "0.0.1",
                "store": str(tmp_path / "store" / "memory.sqlite"),
                "started": "2026-01-01T00:00:00Z",
            },
        )
        async with stdio_client(_shim_params(daemon_env)) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                assert init.server_info.version == __version__
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == NINE
        assert fake.shutdown_calls == 1
        state = _wait_running(tmp_path)
        assert state["version"] == __version__
        assert state["port"] != fake.server_address[1]
    finally:
        fake.shutdown()
        fake.server_close()


async def test_shim_falls_back_in_process_when_no_daemon_can_start(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    env["BETTERMEMORY_STATE_DIR"] = str(blocker / "state")
    stderr_path = tmp_path / "shim.stderr"
    with stderr_path.open("w") as stderr:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "bettermemory"], env=env
        )
        async with stdio_client(params, errlog=stderr) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                assert init.server_info.version == __version__
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == NINE
                written = await session.call_tool(
                    "memory_write",
                    {"content": "served in-process by the shim", "scopes": ["tools"]},
                )
                assert not written.is_error
    assert "in-process" in stderr_path.read_text()
    with Store(tmp_path / "store") as store:
        assert any("served in-process" in m.body for m in store.iter_active())


def test_serve_runs_the_in_process_server_on_purpose(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "bettermemory", "serve"],
        env=env,
        input=(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                }
            )
            + "\n"
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert '"serverInfo"' in proc.stdout
    assert not _state_path(tmp_path).exists()
