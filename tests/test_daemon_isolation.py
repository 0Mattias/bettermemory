"""One daemon, many conversations in many projects: the episode handoff
and the event log keep each conversation and each project apart.

The daemon serves every shim on the machine from one process, so the
session an event or an episode belongs to is the request's (the shim's
``x-bettermemory-session``, one per conversation), and the worktree it
was recorded in is the request's declared workspace, never the daemon's
own working directory. Before this, every client shared the daemon's one
session id: a new conversation's handoff saw nothing from the same
daemon lifetime, and after a restart every project's takeaways came back
in every project.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.identity import HEADER_SESSION, HEADER_WORKSPACE
from bettermemory.store import Store

from .test_daemon import _auth, mcp_session, running_app


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


def _checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def _payload(result: Any) -> dict[str, Any]:
    assert not result.is_error, result
    text = "".join(getattr(block, "text", "") for block in result.content)
    return json.loads(text)


async def _episode(
    client: Any, session: str, workspace: Path, **args: Any
) -> dict[str, Any]:
    headers = _auth(**{HEADER_SESSION: session, HEADER_WORKSPACE: str(workspace)})
    async with mcp_session(client, headers=headers) as s:
        return _payload(await s.call_tool("episode", args))


def _takeaways(handoff: dict[str, Any]) -> list[str]:
    return [e["takeaway"] for e in handoff["episodes"]]


@pytest.fixture
def projects(tmp_path: Path) -> tuple[Path, Path]:
    return _checkout(tmp_path / "proj-a"), _checkout(tmp_path / "proj-b")


async def test_handoff_sees_the_last_conversation_in_its_own_project(
    daemon_config: Config, daemon_store: Store, projects: tuple[Path, Path]
) -> None:
    a, b = projects
    async with running_app(daemon_config, daemon_store) as client:
        await _episode(
            client, "sess_a1", a, action="write", body="a", takeaway="from a"
        )
        await _episode(
            client, "sess_b1", b, action="write", body="b", takeaway="from b"
        )
        in_a = await _episode(client, "sess_a2", a, action="handoff")
        in_b = await _episode(client, "sess_b2", b, action="handoff")
    assert in_a["prior_session_id"] == "sess_a1"
    assert _takeaways(in_a) == ["from a"]
    assert in_b["prior_session_id"] == "sess_b1"
    assert _takeaways(in_b) == ["from b"]


async def test_a_restart_does_not_merge_projects(
    daemon_config: Config, daemon_store: Store, projects: tuple[Path, Path]
) -> None:
    a, b = projects
    async with running_app(daemon_config, daemon_store) as client:
        await _episode(
            client, "sess_a1", a, action="write", body="a", takeaway="from a"
        )
        await _episode(
            client, "sess_b1", b, action="write", body="b", takeaway="from b"
        )
    async with running_app(daemon_config, daemon_store) as client:
        in_a = await _episode(client, "sess_a2", a, action="handoff")
        in_b = await _episode(client, "sess_b2", b, action="handoff")
    assert _takeaways(in_a) == ["from a"]
    assert _takeaways(in_b) == ["from b"]


async def test_a_mixed_session_serves_only_the_callers_worktree(
    daemon_config: Config, daemon_store: Store, projects: tuple[Path, Path]
) -> None:
    """A session that wrote from two worktrees (every daemon lifetime in a
    store written before the fix looks like this) hands each project only
    its own episodes."""
    a, b = projects
    async with running_app(daemon_config, daemon_store) as client:
        await _episode(
            client, "sess_mixed", a, action="write", body="a", takeaway="from a"
        )
        await _episode(
            client, "sess_mixed", b, action="write", body="b", takeaway="from b"
        )
        in_a = await _episode(client, "sess_a2", a, action="handoff")
        in_b = await _episode(client, "sess_b2", b, action="handoff")
    assert _takeaways(in_a) == ["from a"]
    assert _takeaways(in_b) == ["from b"]


async def test_events_carry_the_requests_session_and_worktree(
    daemon_config: Config, daemon_store: Store, projects: tuple[Path, Path]
) -> None:
    _, b = projects
    async with running_app(daemon_config, daemon_store) as client:
        await _episode(
            client, "sess_b1", b, action="write", body="b", takeaway="from b"
        )
    writes = [e for e in daemon_store.iter_events() if e.get("kind") == "episode_write"]
    assert [(e.get("session"), e.get("worktree_root")) for e in writes] == [
        ("sess_b1", str(b))
    ]
