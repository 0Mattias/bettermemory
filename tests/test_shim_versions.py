"""A shim and a daemon of different versions, across a candidate boundary
(ENG-107).

The plugin's unpinned `uvx bettermemory` moves to the next candidate while
a `uv tool install` or a venv beside it does not, so a shim from one
candidate meets the daemon of the next. This build's shim stands for the
earlier candidate. The daemon is this build's own, made to report the
next candidate by a `sitecustomize` in its environment that sets
`bettermemory.__version__`, the attribute the daemon's state file and
/health report (`daemon._version`) and a client compares
(`_daemon_client.package_version`).

At start a shim keeps a newer daemon and serves its tools; a shim whose
daemon comes back at another version while its session is open serves
through it and asks, once, for a restart of the MCP client, which still
holds the tools the session began with.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

import bettermemory
from bettermemory import _daemon_client
from bettermemory._daemon_client import health, write_state

from .conftest import child_tau_env
from .test_daemon_lifecycle import _cli, _env, _store_path, _wait_running
from .test_shim import (
    NINE,
    _FakeMcpDaemon,
    _free_port,
    _search,
    _shim_params,
    _text,
)

RESTART = "restart the MCP client"


def next_candidate(version: str) -> str:
    """The candidate after `version`, which `version_key` orders after it:
    `X.Y.ZrcN` gives `X.Y.Zrc(N+1)`, a final `X.Y.Z` gives `X.Y.(Z+1)`,
    and a dev release the release it leads to (`X.Y.Z.devN` gives
    `X.Y.Zrc1`, `X.Y.ZrcN.devM` gives `X.Y.ZrcN`). Empty for a version
    this cannot place (a `.postN`, a local `+...`), which skips the
    module rather than failing its collection."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?(?:\.dev(\d+))?", version)
    if match is None:
        return ""
    major, minor, patch, rc, dev = match.groups()
    base = f"{major}.{minor}.{patch}"
    if dev is not None:
        return f"{base}rc{rc if rc is not None else 1}"
    if rc is not None:
        return f"{base}rc{int(rc) + 1}"
    return f"{major}.{minor}.{int(patch) + 1}"


THIS = bettermemory.__version__
NEXT = next_candidate(THIS)
pytestmark = pytest.mark.skipif(
    not NEXT, reason=f"no candidate follows bettermemory {THIS} in version_key's order"
)


def _src_first(env: dict[str, str]) -> dict[str, str]:
    """`env` whose children import the package this test imported (a
    fail-before run points PYTHONPATH at another checkout's src)."""
    src = str(Path(bettermemory.__file__).resolve().parents[1])
    out = dict(env)
    out["PYTHONPATH"] = src + os.pathsep + out.get("PYTHONPATH", "")
    return out


def reporting(env: dict[str, str], site: Path, version: str) -> dict[str, str]:
    """`env` whose Python children report `version` as bettermemory's,
    with the review off as `child_tau_env` sets it (one `sitecustomize`
    runs, the first on the path)."""
    out = child_tau_env(env, site, None)
    with (site / "sitecustomize.py").open("a", encoding="utf-8") as fh:
        fh.write(f"import bettermemory as _bm\n_bm.__version__ = {version!r}\n")
    return out


@pytest.fixture
def daemon_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    env = child_tau_env(_src_first(_env(tmp_path)), tmp_path / "site", None)
    yield env
    _cli(["down"], env, timeout=30)


def test_the_next_candidate_is_newer() -> None:
    this, after = _daemon_client.version_key(THIS), _daemon_client.version_key(NEXT)
    assert this is not None and after is not None and after > this


async def test_a_shim_serves_through_the_daemon_of_the_next_candidate(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """At start the shim keeps the newer daemon, says so once, and serves
    that daemon's tools: no restart is asked, since the session loads the
    tools of the daemon it serves through."""
    newer = reporting(daemon_env, tmp_path / "site-next", NEXT)
    assert _cli(["up", "--port", "0"], newer).returncode == 0
    state = _wait_running(tmp_path)
    assert state["version"] == NEXT
    answer = health(int(state["port"]))
    assert answer is not None and answer["version"] == NEXT
    stderr_path = tmp_path / "shim.stderr"
    with stderr_path.open("w") as stderr:
        async with stdio_client(_shim_params(daemon_env), errlog=stderr) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                assert init.server_info.version == THIS
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == NINE
                written = await session.call_tool(
                    "memory_write",
                    {
                        "content": "written through an older shim to a newer daemon",
                        "scopes": ["tools"],
                    },
                )
                assert not written.is_error, _text(written)
                assert not (await _search(session)).is_error
    assert _wait_running(tmp_path)["pid"] == state["pid"], "kept, not replaced"
    told = stderr_path.read_text()
    assert f"keeping the daemon of version {NEXT} at 127.0.0.1:{state['port']}" in told
    assert RESTART not in told


async def test_a_shim_whose_daemon_returns_at_the_next_candidate_asks_for_a_restart(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """The daemon the session began with is replaced by one of the next
    candidate on the same port: the shim reconnects, forwards to it, and
    logs one line naming both versions and asking for a restart of the
    MCP client, however many calls follow."""
    port = _free_port()
    assert _cli(["up", "--port", str(port)], daemon_env).returncode == 0
    assert _wait_running(tmp_path)["version"] == THIS
    newer = reporting(daemon_env, tmp_path / "site-next", NEXT)
    stderr_path = tmp_path / "shim.stderr"
    with stderr_path.open("w") as stderr:
        async with stdio_client(_shim_params(daemon_env), errlog=stderr) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                assert not (await _search(session)).is_error
                assert _cli(["down"], daemon_env).returncode == 0
                assert _cli(["up", "--port", str(port)], newer).returncode == 0
                after = _wait_running(tmp_path)
                assert after["version"] == NEXT
                assert not (await _search(session)).is_error
                assert not (await _search(session)).is_error
                assert _wait_running(tmp_path)["pid"] == after["pid"]
    told = [line for line in stderr_path.read_text().splitlines() if RESTART in line]
    assert len(told) == 1, told
    assert f"runs version {NEXT}" in told[0]
    assert f"began with the daemon of version {THIS}" in told[0]
    assert f"to load version {NEXT}'s" in told[0]


async def test_a_shim_whose_daemon_returns_older_asks_for_a_restart_too(
    tmp_path: Path,
) -> None:
    """A reconnect does not replace the daemon it finds (a client's start
    does), so a shim that finds an older one serves through it and asks
    for the restart that replaces it."""
    env = child_tau_env(_src_first(_env(tmp_path)), tmp_path / "site", None)
    store = _store_path(tmp_path)
    first = _FakeMcpDaemon(version=THIS, token="a" * 64)
    older = _FakeMcpDaemon(version="9.0.0rc1", token="b" * 64)
    stderr_path = tmp_path / "shim.stderr"
    try:
        write_state(tmp_path / "state", first.state(store))
        with stderr_path.open("w") as stderr:
            async with stdio_client(_shim_params(env), errlog=stderr) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    assert f"answered by {first.port}" in _text(await _search(session))
                    first.stop()
                    write_state(tmp_path / "state", older.state(store))
                    assert f"answered by {older.port}" in _text(await _search(session))
                    assert older.shutdown_calls == 0
    finally:
        first.stop()
        older.stop()
    told = [line for line in stderr_path.read_text().splitlines() if RESTART in line]
    assert len(told) == 1, told
    assert "runs version 9.0.0rc1" in told[0]
    assert f"began with the daemon of version {THIS}" in told[0]


def test_a_process_tells_each_other_version_once_and_only_after_its_first(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The client side alone: the first daemon a process uses for a store
    names the version its session began with; a later one of another
    version is told once, the session's own version again not at all."""
    state_dir = tmp_path / "state"
    store = _store_path(tmp_path)
    daemons = [
        _FakeMcpDaemon(version=THIS, token="a" * 64),
        _FakeMcpDaemon(version=NEXT, token="b" * 64),
        _FakeMcpDaemon(version=THIS, token="c" * 64),
    ]
    caplog.set_level(logging.INFO, logger="bettermemory._daemon_client")
    try:
        write_state(state_dir, daemons[0].state(store))
        kept = _daemon_client.ensure_daemon(state_dir, store, version=THIS, start=False)
        assert kept is not None and kept["port"] == daemons[0].port
        found = []
        for daemon in (daemons[1], daemons[1], daemons[2], daemons[1]):
            write_state(state_dir, daemon.state(store))
            state = _daemon_client._answering(state_dir, store)
            assert state is not None
            found.append(state["port"])
    finally:
        for daemon in daemons:
            daemon.stop()
    ports = [daemons[i].port for i in (1, 1, 2, 1)]
    assert found == ports
    told = [r for r in caplog.records if RESTART in r.getMessage()]
    assert len(told) == 1
    assert told[0].levelno == logging.WARNING
    assert told[0].getMessage() == (
        f"the daemon at 127.0.0.1:{daemons[1].port} (pid {daemons[1].pid}) runs "
        f"version {NEXT}, and this session began with the daemon of version "
        f"{THIS}, whose tools and instructions the MCP client still holds: "
        f"restart the MCP client to load version {NEXT}'s"
    )
