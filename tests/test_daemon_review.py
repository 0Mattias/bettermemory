"""The write review on every MCP path: the daemon at /mcp, the stdio shim
in front of it, and the shim's in-process fallback.

Unit D1 (TESTS FIRST item 4). The review rides `memory_write`, so a host
with hooks and an MCP host without them run the same two-call round trip;
the hook endpoints carry nothing of it. Every test starts with the
thresholds None (conftest's `review_tau_unset`), so the in-process app gets
them from the `tau` fixture and a subprocess (the daemon the CLI starts,
the shim, the fallback) from a `sitecustomize` on its `PYTHONPATH` that
sets them at interpreter start (conftest's `child_tau_env`).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bettermemory import hook, identity
from bettermemory._daemon_client import STORE_FILENAME, spawn_lock_file_for
from bettermemory import review
from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.location import resolve_caller
from bettermemory.store import Store

from .conftest import child_tau_env
from .test_daemon import _auth, mcp_session, running_app
from .test_daemon_lifecycle import _cli, _env, _wait_running
from .test_membership_security import stand_in_checkout

TAU = 0.5

OLD = "Nightly database backups are handled by Restic."
NEW = "Backups moved to Borg for the archive volumes."


def _config(store_dir: Path) -> Config:
    return Config(
        storage=StorageConfig(directory=str(store_dir)),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )


@pytest.fixture
def daemon_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


@pytest.fixture
def daemon_config(tmp_path: Path) -> Config:
    return _config(tmp_path / "store")


@pytest.fixture
def tau(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(review, "TAU_UNSAFE", TAU)
    monkeypatch.setattr(review, "TAU_SECRET", TAU)
    return TAU


@pytest.fixture(autouse=True)
def _in_checkout(
    workspace: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Every path serves a caller standing in one checkout: the daemon
    resolves an undeclared caller at its process cwd, a child inherits
    this one, and the review strata admit the caller's own project and
    nothing outside any project (design 3.5)."""
    return stand_in_checkout(tmp_path_factory.mktemp("checkout"), monkeypatch)


def _seed(store: Store, config: Config, content: str, *, scopes: list[str]) -> str:
    """A record written where the test stands, homed in its project
    (design 2.1)."""
    home = resolve_caller(store, config, os.getcwd()).home()
    return store.write(content=content, scopes=scopes, home=home).id


@pytest.fixture(autouse=True)
def _no_declared_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(identity.ENV_CLIENT, raising=False)
    monkeypatch.delenv(identity.ENV_MODEL, raising=False)


def _tau_env(tmp_path: Path) -> dict[str, str]:
    """The lifecycle tests' child environment plus a `sitecustomize` that
    sets the review thresholds in every Python child at start, so a daemon
    the shim or the CLI spawns issues reviews too."""
    env = child_tau_env(_env(tmp_path), tmp_path / "site", TAU)
    env.pop(identity.ENV_CLIENT, None)
    env.pop(identity.ENV_MODEL, None)
    return env


@pytest.fixture
def tau_daemon_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    env = _tau_env(tmp_path)
    yield env
    _cli(["down"], env, timeout=30)


def _payload(result: Any) -> dict[str, Any]:
    assert not result.is_error, result
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return json.loads("".join(getattr(block, "text", "") for block in result.content))


async def _round_trip(session: ClientSession) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write NEW, answer its review with every id unrelated, re-send."""
    args = {"content": NEW, "scopes": ["tools"]}
    first = _payload(await session.call_tool("memory_write", args))
    assert first["status"] == "review"
    assert first["written"] is False
    assert first["hint"].startswith("Not written.")
    answers = {
        "review_id": first["review"]["review_id"],
        "relation": {e["id"]: "unrelated" for e in first["review"]["related"]},
        "unsafe": 0,
        "secret": 0,
    }
    second = _payload(
        await session.call_tool("memory_write", {**args, "review": answers})
    )
    return first, second


def _issued(store: Store) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e.get("kind") == review.REVIEW_ISSUED]


async def test_review_round_trip_over_streamable_http(
    daemon_config: Config,
    daemon_store: Store,
    tau: float,
) -> None:
    old = _seed(daemon_store, daemon_config, OLD, scopes=["tools"])
    headers = _auth(
        **{identity.HEADER_CLIENT: "http-host", identity.HEADER_MODEL: "writer-a"}
    )
    async with running_app(daemon_config, daemon_store) as client:
        async with mcp_session(client, headers=headers) as session:
            first, second = await _round_trip(session)
    assert [e["id"] for e in first["review"]["related"]] == [old]
    assert second["status"] == "committed"
    assert any(NEW in m.body for m in daemon_store.load_all())
    (issued,) = _issued(daemon_store)
    assert issued["client"] == "http-host"
    assert issued["model"] == "writer-a"


async def test_review_round_trip_through_stdio_shim(
    tmp_path: Path, tau_daemon_env: dict[str, str]
) -> None:
    with Store(tmp_path / "store") as store:
        old = _seed(store, _config(tmp_path / "store"), OLD, scopes=["tools"])
    assert _cli(["up", "--port", "0"], tau_daemon_env).returncode == 0
    _wait_running(tmp_path)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "bettermemory"], env=tau_daemon_env
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            first, second = await _round_trip(session)
    assert [e["id"] for e in first["review"]["related"]] == [old]
    assert second["status"] == "committed"
    with Store(tmp_path / "store") as store:
        assert any(NEW in m.body for m in store.load_all())
        (issued,) = _issued(store)
    assert issued["review_id"] == first["review"]["review_id"]


async def test_in_process_fallback_serves_the_same_review(
    tmp_path: Path, tau: float
) -> None:
    """No daemon can be started (its spawn lock cannot be opened), so the
    shim serves the store in-process, and the review it serves is the
    daemon's. The state directory itself stays usable: it holds this
    machine's identity, and a process without one is admitted nothing on
    any surface (decision B16, design 3.5), the review strata included."""
    env = _tau_env(tmp_path)
    store_path = Path(env["BETTERMEMORY_DIR"]).expanduser().resolve() / STORE_FILENAME
    spawn_lock_file_for(Path(env["BETTERMEMORY_STATE_DIR"]), store_path).mkdir(
        parents=True
    )
    with Store(tmp_path / "store") as store:
        old = _seed(store, _config(tmp_path / "store"), OLD, scopes=["tools"])
    stderr_path = tmp_path / "shim.stderr"
    with stderr_path.open("w") as stderr:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "bettermemory"], env=env
        )
        async with stdio_client(params, errlog=stderr) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                fallback = _payload(
                    await session.call_tool(
                        "memory_write", {"content": NEW, "scopes": ["tools"]}
                    )
                )
    assert "in-process" in stderr_path.read_text()
    assert fallback["status"] == "review"

    # The same write through the daemon's app, on the same store.
    with Store(tmp_path / "store") as daemon_store:
        async with running_app(_config(tmp_path / "store"), daemon_store) as client:
            async with mcp_session(client, headers=_auth()) as session:
                served = _payload(
                    await session.call_tool(
                        "memory_write", {"content": NEW, "scopes": ["tools"]}
                    )
                )
    assert set(fallback) == set(served) == {"status", "written", "review", "hint"}
    assert fallback["review"]["related"] == served["review"]["related"]
    assert [e["id"] for e in served["review"]["related"]] == [old]
    assert fallback["review"]["questions"] == served["review"]["questions"]
    assert fallback["review"]["questions"] == review.questions_payload()
    assert fallback["hint"].replace(fallback["review"]["review_id"], "<id>") == served[
        "hint"
    ].replace(served["review"]["review_id"], "<id>")


async def test_bare_http_client_without_identity_headers_gets_review_and_client_none(
    daemon_config: Config,
    daemon_store: Store,
    tau: float,
) -> None:
    _seed(daemon_store, daemon_config, OLD, scopes=["tools"])
    async with running_app(daemon_config, daemon_store) as client:
        async with mcp_session(client, headers=_auth()) as session:
            first, second = await _round_trip(session)
    assert first["status"] == "review"
    assert second["status"] == "committed"
    (issued,) = _issued(daemon_store)
    assert issued["client"] == review.UNDECLARED
    assert issued["model"] == review.UNDECLARED


async def test_two_shim_sessions_share_no_review_state(
    daemon_config: Config,
    daemon_store: Store,
    tau: float,
) -> None:
    """Two sessions against one daemon (the shim sets x-bettermemory-session
    per process) each get their own review, over their own related set.
    The session ids are this test's own: the daemon's session registry is
    process-wide, and test_daemon.py's sessions disable scopes in it."""
    visible = _seed(daemon_store, daemon_config, OLD, scopes=["tools"])
    hidden = _seed(
        daemon_store,
        daemon_config,
        "Restic backups of the archive volumes rotate monthly.",
        scopes=["ops"],
    )
    args = {"content": NEW, "scopes": ["tools"]}
    async with running_app(daemon_config, daemon_store) as client:
        async with mcp_session(
            client, headers=_auth(**{identity.HEADER_SESSION: "sess_review_a"})
        ) as a:
            disabled = await a.call_tool(
                "memory_admin", {"action": "disable_scope", "scope": "ops"}
            )
            assert not disabled.is_error
            review_a = _payload(await a.call_tool("memory_write", args))
        async with mcp_session(
            client, headers=_auth(**{identity.HEADER_SESSION: "sess_review_b"})
        ) as b:
            review_b = _payload(await b.call_tool("memory_write", args))
            related_b = [e["id"] for e in review_b["review"]["related"]]
            committed_b = _payload(
                await b.call_tool(
                    "memory_write",
                    {
                        **args,
                        "review": {
                            "review_id": review_b["review"]["review_id"],
                            "relation": {i: "unrelated" for i in related_b},
                            "unsafe": 0,
                            "secret": 0,
                        },
                    },
                )
            )
    related_a = [e["id"] for e in review_a["review"]["related"]]
    assert review_a["review"]["review_id"] != review_b["review"]["review_id"]
    assert visible in related_a and hidden not in related_a
    assert visible in related_b and hidden in related_b
    assert committed_b["status"] == "committed"
    rows = _issued(daemon_store)
    assert [row["actor"]["session"] for row in rows] == [
        "sess_review_a",
        "sess_review_b",
    ]
    assert all(row["parent_review_id"] is None for row in rows)


async def test_hook_endpoints_unchanged_by_review(
    daemon_config: Config,
    daemon_store: Store,
    tau: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stop audit stamps `checked_at` from the hook's clock; one fixed
    # instant makes the two passes comparable byte for byte.
    frozen = datetime.now(timezone.utc)
    monkeypatch.setattr(hook, "utcnow", lambda: frozen)
    _seed(daemon_store, daemon_config, OLD, scopes=["tools"])
    transcript = tmp_path / "transcript.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "how are backups run"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "I would have to check."}],
            },
        },
    ]
    transcript.write_bytes(
        ("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8")
    )
    requests = [
        ("/api/v1/hook/session-start", {"cwd": str(tmp_path)}),
        (
            "/api/v1/hook/prompt",
            {"cwd": str(tmp_path), "session_id": "sess_hook", "prompt": "hello there"},
        ),
        (
            "/api/v1/hook/stop",
            {
                "cwd": str(tmp_path),
                "session_id": "sess_hook",
                "transcript_path": str(transcript),
            },
        ),
    ]

    async def hooks(client: Any) -> list[bytes]:
        out: list[bytes] = []
        for path, body in requests:
            response = await client.post(path, json=body, headers=_auth())
            assert response.status_code == 200, path
            out.append(response.content)
        return out

    async with running_app(daemon_config, daemon_store) as client:
        before = await hooks(client)
        async with mcp_session(client, headers=_auth()) as session:
            issued = _payload(
                await session.call_tool(
                    "memory_write", {"content": NEW, "scopes": ["tools"]}
                )
            )
        assert issued["status"] == "review"
        after = await hooks(client)
    assert before == after
    assert len(_issued(daemon_store)) == 1
