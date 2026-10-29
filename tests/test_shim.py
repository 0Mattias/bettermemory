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
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from bettermemory import __version__
from bettermemory._daemon_client import health, health_proof, read_state, write_state
from bettermemory.builder import INSTRUCTIONS
from bettermemory.config import Config, StorageConfig
from bettermemory.location import resolve_caller
from bettermemory.store import Store

from .conftest import child_tau_env
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
def daemon_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    # The review off in the daemon and shim children, as in-process.
    env = child_tau_env(_env(tmp_path), tmp_path / "site", None)
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
            # Checked while the shim serves. On Windows the SDK's stdio
            # client puts the shim in a job object that permits no breakaway
            # and kills its members when the session ends, so the daemon the
            # shim started goes with it; a detached daemon outliving the
            # process that started it is test_daemon_lifecycle's to check.
            state = _wait_running(tmp_path)
            assert health(state["port"]) is not None
    assert {tool.name for tool in listed.tools} == NINE
    # U5-P5: the cold path answers its first tools/list within 1.5 s;
    # MISSED-if over 3 s. The assertion holds the MISSED-if bound on a
    # developer machine, where bench/daemon/latency.py records the number
    # (0.8 s on the owner's). A shared CI runner's first import of the SDK
    # is not that measurement: the Windows runner read 6.9 s and 3.6 s on
    # two of three runs, so on CI the bound only catches a hang.
    bound = 30.0 if os.environ.get("CI") else 3.0
    assert first_answer < bound


async def test_two_shims_share_the_store_and_keep_separate_session_state(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    _wait_running(tmp_path)
    with Store(tmp_path / "store") as store:
        # Homed as the write handler homes it, by the caller resolved where
        # the shims stand (design 8.1): a record with no home row reaches
        # no caller (design 3.5).
        config = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
        caller = resolve_caller(store, config, Path.cwd())
        store.write(
            content="a fact filed under tools", scopes=["tools"], home=caller.home()
        )
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
    """A daemon from before 9.0.0rc12: /health reports the pid and store
    the test's state file names, with no proof."""

    shutdown_calls = 0
    posts = 0
    pid = 2**22 + 4321
    store = ""


class _FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, D401
        return

    def do_GET(self) -> None:  # noqa: N802
        fake = cast(_FakeDaemon, self.server)
        body = json.dumps(
            {"status": "ok", "version": "0.0.1", "store": fake.store, "pid": fake.pid}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        self.server.posts += 1  # type: ignore[attr-defined]
        if self.path == "/api/v1/admin/shutdown":
            self.server.shutdown_calls += 1  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


async def test_shim_starts_its_own_daemon_beside_a_pre_rc12_one(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """A daemon from before the proof (rc1 to rc11) at its state file's
    port, older than the shim. Before 9.0.0rc14 the shim took its /health,
    which repeats the state file's pid and store, for its own daemon's and
    posted it the shutdown with the token. It is another listener now: the
    shim posts it nothing, starts a daemon of its own beside it on another
    port, and serves through that one."""
    fake = _FakeDaemon(("127.0.0.1", 0), _FakeHandler)
    fake.store = str(tmp_path / "store" / "memory.sqlite")
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    state_dir = tmp_path / "state"
    try:
        write_state(
            state_dir,
            {
                "pid": fake.pid,
                "port": fake.server_address[1],
                "token": "t" * 64,
                "version": "0.0.1",
                "store": fake.store,
                "started": "2026-01-01T00:00:00Z",
            },
        )
        async with stdio_client(_shim_params(daemon_env)) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                assert init.server_info.version == __version__
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == NINE
                # While the shim serves, for the reason given in
                # test_shim_starts_a_daemon_when_none_answers.
                assert fake.shutdown_calls == 0 and fake.posts == 0
                state = _wait_running(tmp_path)
                assert state["version"] == __version__
                assert state["port"] != fake.server_address[1]
    finally:
        fake.shutdown()
        fake.server_close()


async def test_shim_falls_back_in_process_when_no_daemon_can_start(
    tmp_path: Path,
) -> None:
    env = child_tau_env(_env(tmp_path), tmp_path / "site", None)
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


# ---------------------------------------------------------------------------
# A daemon restart under a live shim (unit S-FIX)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _search(session: ClientSession) -> Any:
    return await session.call_tool("memory_search", {"query": "anything"})


async def test_shim_survives_a_restart_on_the_same_port(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    # The restart mints a fresh token; the shim's old one gets 401.
    port = _free_port()
    assert _cli(["up", "--port", str(port)], daemon_env).returncode == 0
    before = _wait_running(tmp_path)
    async with stdio_client(_shim_params(daemon_env)) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            assert not (await _search(session)).is_error
            assert _cli(["down"], daemon_env).returncode == 0
            assert _cli(["up", "--port", str(port)], daemon_env).returncode == 0
            after = _wait_running(tmp_path)
            assert after["token"] != before["token"]
            started = time.monotonic()
            assert not (await _search(session)).is_error
            first_call = time.monotonic() - started
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == NINE
    # S-FIX S2: one 401, one state read, one resend; MISSED-if over 2 s.
    assert first_call < (30.0 if os.environ.get("CI") else 2.0)


async def test_shim_follows_a_daemon_to_another_port(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", str(_free_port())], daemon_env).returncode == 0
    before = _wait_running(tmp_path)
    async with stdio_client(_shim_params(daemon_env)) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            assert not (await _search(session)).is_error
            assert _cli(["down"], daemon_env).returncode == 0
            assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
            assert _wait_running(tmp_path)["port"] != before["port"]
            assert not (await _search(session)).is_error


async def test_shim_starts_a_daemon_when_its_daemon_is_gone(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    before = _wait_running(tmp_path)
    async with stdio_client(_shim_params(daemon_env)) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            assert _cli(["down"], daemon_env).returncode == 0
            started = time.monotonic()
            assert not (await _search(session)).is_error
            elapsed = time.monotonic() - started
            # While the shim serves, as in test_shim_starts_a_daemon_when_none_answers.
            assert _wait_running(tmp_path)["pid"] != before["pid"]
    # S-FIX S3: within the 8 s start timeout plus 2 s; MISSED-if over 12 s.
    assert elapsed < (60.0 if os.environ.get("CI") else 12.0)


async def test_shim_answers_an_error_and_stays_up_when_no_daemon_can_start(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    _wait_running(tmp_path)
    store_dir = tmp_path / "store"
    parked = tmp_path / "store-parked"
    async with stdio_client(_shim_params(daemon_env)) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            assert _cli(["down"], daemon_env).returncode == 0
            # A file where the store's directory was: a daemon started now
            # cannot open the store and exits.
            store_dir.rename(parked)
            store_dir.write_text("not a directory")
            started = time.monotonic()
            with pytest.raises(MCPError, match="no bettermemory daemon"):
                await _search(session)
            elapsed = time.monotonic() - started
            store_dir.unlink()
            parked.rename(store_dir)
            assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
            _wait_running(tmp_path)
            assert not (await _search(session)).is_error
    # S-FIX S4: the error within 12 s; MISSED-if over 15 s or the shim exits.
    assert elapsed < (60.0 if os.environ.get("CI") else 15.0)


class _FakeMcpDaemon(http.server.ThreadingHTTPServer):
    """A daemon's HTTP surface, enough for a shim: /health, the shutdown
    endpoint and a stateless /mcp whose tools/call does what `mode` says.
    /health proves the token as a daemon does (`health_proof`), so the
    shim takes it for the daemon its state file names."""

    pid = 2**22 + 4322

    def __init__(self, *, version: str, token: str, mode: str = "answer") -> None:
        super().__init__(("127.0.0.1", 0), _FakeMcpHandler)
        self.version = version
        self.token = token
        self.mode = mode
        self.store = ""
        self.calls = 0
        self.shutdown_calls = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def state(self, store: Path) -> dict[str, Any]:
        self.store = str(store)
        return {
            "pid": self.pid,
            "port": self.port,
            "token": self.token,
            "version": self.version,
            "store": self.store,
            "started": "2026-01-01T00:00:00Z",
        }

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _FakeMcpHandler(http.server.BaseHTTPRequestHandler):
    @property
    def fake(self) -> _FakeMcpDaemon:
        return cast(_FakeMcpDaemon, self.server)

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
        path, _, query = self.path.partition("?")
        if path != "/health":
            self._json(405, {})
            return
        answer: dict[str, Any] = {
            "status": "ok",
            "version": self.fake.version,
            "store": self.fake.store,
            "pid": self.fake.pid,
        }
        nonce = urllib.parse.parse_qs(query).get("nonce")
        if nonce:
            answer["proof"] = health_proof(self.fake.token, nonce[0])
        self._json(200, answer)

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/v1/admin/shutdown":
            self.fake.shutdown_calls += 1
            self._json(200, {})
            return
        message = json.loads(body)
        method = message.get("method")
        if method == "tools/call":
            self.fake.calls += 1
        if self.headers.get("Authorization") != f"Bearer {self.fake.token}" or (
            method == "tools/call" and self.fake.mode == "unauthorized"
        ):
            self._json(401, {"error": "unauthorized"})
            return
        if "id" not in message:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": self.fake.version},
                "instructions": "fake",
            }
        elif method == "tools/list":
            result = {"tools": []}
        elif self.fake.mode == "drop":
            self.close_connection = True
            return
        else:
            result = {
                "content": [{"type": "text", "text": f"answered by {self.fake.port}"}],
                "isError": False,
            }
        self._json(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})


def _fake_shim_env(tmp_path: Path) -> dict[str, str]:
    return child_tau_env(_env(tmp_path), tmp_path / "site", None)


async def test_shim_never_resends_a_call_whose_reply_was_lost(tmp_path: Path) -> None:
    env = _fake_shim_env(tmp_path)
    fake = _FakeMcpDaemon(version=__version__, token="a" * 64, mode="drop")
    try:
        write_state(tmp_path / "state", fake.state(_store_path(tmp_path)))
        async with stdio_client(_shim_params(env)) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                with pytest.raises(MCPError, match="may have been applied"):
                    await _search(session)
                assert fake.calls == 1
                fake.mode = "answer"
                assert "answered by" in _text(await _search(session))
    finally:
        fake.stop()


async def test_shim_resends_once_on_401_then_answers_an_error(tmp_path: Path) -> None:
    env = _fake_shim_env(tmp_path)
    fake = _FakeMcpDaemon(version=__version__, token="a" * 64, mode="unauthorized")
    try:
        write_state(tmp_path / "state", fake.state(_store_path(tmp_path)))
        async with stdio_client(_shim_params(env)) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                with pytest.raises(MCPError, match="refused the shim's token"):
                    await _search(session)
                assert fake.calls == 2
    finally:
        fake.stop()


async def test_shim_reconnects_to_a_daemon_of_another_version_and_keeps_it(
    tmp_path: Path,
) -> None:
    env = _fake_shim_env(tmp_path)
    store = _store_path(tmp_path)
    first = _FakeMcpDaemon(version=__version__, token="a" * 64)
    second = _FakeMcpDaemon(version="0.0.1", token="b" * 64)
    try:
        write_state(tmp_path / "state", first.state(store))
        async with stdio_client(_shim_params(env)) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                assert f"answered by {first.port}" in _text(await _search(session))
                first.stop()
                write_state(tmp_path / "state", second.state(store))
                assert f"answered by {second.port}" in _text(await _search(session))
                assert second.calls == 1
                assert second.shutdown_calls == 0
    finally:
        first.stop()
        second.stop()


@pytest.mark.parametrize(
    "running_version", ["999.0.0", __version__], ids=["newer", "equal"]
)
async def test_shim_keeps_a_daemon_of_the_same_or_a_newer_version(
    tmp_path: Path, running_version: str
) -> None:
    """At start the shim uses a daemon of its own version or a newer one
    as it is, with one stderr line for a newer one: a shim from an older
    install beside a newer daemon serves through it instead of
    restarting it under the newer install's clients."""
    env = _fake_shim_env(tmp_path)
    store = _store_path(tmp_path)
    fake = _FakeMcpDaemon(version=running_version, token="a" * 64)
    stderr_path = tmp_path / "shim.stderr"
    try:
        write_state(tmp_path / "state", fake.state(store))
        with stderr_path.open("w") as stderr:
            async with stdio_client(_shim_params(env), errlog=stderr) as (r, w):
                async with ClientSession(r, w) as session:
                    init = await session.initialize()
                    assert init.server_info.version == __version__
                    assert f"answered by {fake.port}" in _text(await _search(session))
        assert fake.shutdown_calls == 0
        assert fake.calls == 1
        state = read_state(tmp_path / "state", store)
        assert state is not None and state["port"] == fake.port
    finally:
        fake.stop()
    kept = f"keeping the daemon of version {running_version} at 127.0.0.1:{fake.port}"
    assert (kept in stderr_path.read_text()) == (running_version != __version__)


@pytest.mark.parametrize("args", [[], ["serve"]], ids=["shim", "serve"])
def test_an_unmigrated_v8_directory_is_refused_in_one_line(
    tmp_path: Path, args: list[str]
) -> None:
    """An 8.x user sees the refusal, not daemon warnings and a traceback
    (ENG-32), and nothing is created on either path."""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / ".events.jsonl").write_text("")
    proc = _cli(args, _env(tmp_path))
    assert proc.returncode == 1
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, proc.stderr
    assert "bettermemory migrate v8" in lines[0]
    assert not _store_path(tmp_path).exists()
    assert not _state_path(tmp_path).exists()
