"""A hook from this conversation must not become its prior session."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory import log as log_module
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.identity import (
    HEADER_HARNESS_SESSION,
    HEADER_SESSION,
    HEADER_WORKSPACE,
)
from bettermemory.identity import ENV_HARNESS_SESSION
from bettermemory.recall import RecallState, session_brief
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool
from .test_daemon import _auth, mcp_session, running_app
from .test_server import _unwrap
from .test_verdict import _repo


@pytest.mark.parametrize("declare_harness", [True, False])
@pytest.mark.parametrize("same_conversation", [True, False])
async def test_same_conversation_hook_preserves_the_current_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declare_harness: bool,
    same_conversation: bool,
) -> None:
    repo = _repo(tmp_path)
    store = Store(tmp_path / "store")
    cfg = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(log_module, "_utcnow_iso", lambda: old)
    Recorder(store=store, session_id="sess_prior").record("search", query="prior")
    harness = "transcript_current"
    headers = {HEADER_SESSION: "sess_current", HEADER_WORKSPACE: str(repo)}
    if declare_harness:
        headers[HEADER_HARNESS_SESSION] = harness
    async with running_app(cfg, store) as client:
        async with mcp_session(client, headers=_auth(**headers)) as session:
            written = await session.call_tool(
                "memory_write",
                {"content": "session boundary hook proof", "scopes": ["tools"]},
            )
            assert not written.is_error
            memory_id = _unwrap(written.structured_content)["id"]
            before = await session.call_tool(
                "memory_search", {"query": "", "since_prior_session": True}
            )
            assert not before.is_error
            assert memory_id in {
                hit["id"] for hit in _unwrap(before.structured_content)
            }
            memory = store.load_one(memory_id)
            assert memory is not None
            later = (memory.updated + timedelta(hours=1)).isoformat()
            monkeypatch.setattr(log_module, "_utcnow_iso", lambda: later)
            hook_session = harness if same_conversation else "transcript_other"
            hook = await client.post(
                "/api/v1/hook/session-start",
                json={"cwd": str(repo), "session_id": hook_session, "source": "clear"},
                headers=_auth(),
            )
            assert hook.status_code == 200
            hook_rows = [
                row
                for row in store.iter_events()
                if row.get("triggered_from") == "session_hook"
                and row.get("session") == hook_session
            ]
            assert hook_rows and all(row["ts"] == later for row in hook_rows)
            after = await session.call_tool(
                "memory_search", {"query": "", "since_prior_session": True}
            )
            assert not after.is_error
            hits = _unwrap(after.structured_content)
            assert (memory_id in {hit["id"] for hit in hits}) == (
                declare_harness and same_conversation
            )


@pytest.mark.parametrize("same_conversation", [True, False])
async def test_in_process_search_joins_its_environment_to_hook_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_conversation: bool
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    harness = "transcript_current"
    monkeypatch.setenv(ENV_HARNESS_SESSION, harness)
    store = Store(tmp_path / "store")
    cfg = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(log_module, "_utcnow_iso", lambda: old)
    Recorder(store=store, session_id="sess_prior").record("search", query="prior")
    server = build_server(config=cfg, store=store, state=SessionState())
    written = await call_tool(
        server,
        "memory_write",
        {"content": "session hook environment", "scopes": ["tools"]},
    )
    memory_id = _unwrap(written)["id"]
    before = await call_tool(
        server, "memory_search", {"query": "", "since_prior_session": True}
    )
    assert memory_id in {hit["id"] for hit in _unwrap(before)}
    later = (store.load_one(memory_id).updated + timedelta(hours=1)).isoformat()
    monkeypatch.setattr(log_module, "_utcnow_iso", lambda: later)
    hook_session = harness if same_conversation else "transcript_other"
    session_brief(
        session_id=hook_session,
        source="clear",
        cwd=repo,
        config=cfg,
        store=store,
        state=RecallState(),
    )
    assert any(
        row.get("session") == hook_session
        and row.get("triggered_from") == "session_hook"
        for row in store.iter_events()
    )
    after = await call_tool(
        server, "memory_search", {"query": "", "since_prior_session": True}
    )
    assert (memory_id in {hit["id"] for hit in _unwrap(after)}) == same_conversation
