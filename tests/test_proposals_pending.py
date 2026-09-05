"""A `user-inference` proposal accepted through the MCP tool stages the
write instead of committing it.

The extractor stamps first-person preferences `user-inference` by
default, and `accept` ran `CONTENT_GATES`, which leaves `PendingGate`
out, so a claim about the user reached the store on the model's own
say-so — the one model-reachable route past the veto that category
exists for (the 2026-09-01 integrity recon's fifth weak point). The
shared core now stages such an accept when it is given a session, and
the CLI, with no session, still commits: the human typing it is the
confirmation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.handlers.proposals import accept_proposal
from bettermemory.proposals import Proposal, ProposalQueue
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PREFERENCE = "I prefer squash merges on every repository I own."


def _proposal(body: str, *, pid: str, cat: str) -> Proposal:
    return Proposal(
        id=pid,
        body=body,
        source_excerpt=body,
        suggested_category=cat,
        created=_NOW.isoformat(),
    )


def _build(root: Path) -> tuple[Any, Store]:
    cfg = Config(
        storage=StorageConfig(directory=str(root)),
        behavior=BehaviorConfig(full_tool_surface=True),
    )
    state = SessionState()
    store = Store(root)
    recorder = Recorder(root=root, session_id=state.session_id, enabled=True)
    return build_server(config=cfg, store=store, state=state, recorder=recorder), store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _accept(server: Any, pid: str, **kwargs: Any) -> Any:
    kwargs.setdefault("scopes", ["learning-style"])
    return await _call(
        server, "memory_proposals", action="accept", proposal_id=pid, **kwargs
    )


async def test_a_user_inference_accept_stages_and_confirm_commits(
    memory_dir: Path,
) -> None:
    queue = ProposalQueue(memory_dir)
    queue.append([_proposal(_PREFERENCE, pid="ui1", cat="user-inference")])
    server, store = _build(memory_dir)

    res = await _accept(server, "ui1")
    assert res["status"] == "pending"
    assert res["action"] == "accept" and res["proposal_id"] == "ui1"
    assert res["pending_reason"] == "user-inference"
    assert res["preview"]["category"] == "user-inference"
    assert "memory_write_confirm" in res["hint"]
    pending_id = res["pending_id"]
    # Claimed at staging, written nowhere.
    assert queue.load() == []
    assert store.load_all() == []
    staged = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "memory_proposals" and e.get("action") == "accept"
    ]
    assert [(e.get("status"), e.get("pending_id")) for e in staged] == [
        ("pending", pending_id)
    ]

    confirmed = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert confirmed["status"] == "committed"
    assert confirmed["category"] == "user-inference"
    stored = store.load_one(confirmed["id"])
    assert stored.category is not None and stored.category.value == "user-inference"
    assert stored.scopes == ["learning-style"]
    assert stored.source.value == "inferred"
    assert stored.body.strip() == _PREFERENCE
    shown = await _call(server, "memory_show", id=confirmed["id"])
    assert shown["provenance"] == "local"


async def test_cancelling_the_staged_accept_drops_the_claim(memory_dir: Path) -> None:
    queue = ProposalQueue(memory_dir)
    queue.append([_proposal(_PREFERENCE, pid="ui2", cat="user-inference")])
    server, store = _build(memory_dir)
    res = await _accept(server, "ui2")
    cancelled = await _call(server, "memory_write_cancel", pending_id=res["pending_id"])
    assert cancelled["existed"] is True
    assert store.load_all() == []
    assert queue.load() == []


async def test_a_fact_proposal_still_commits_on_accept(memory_dir: Path) -> None:
    queue = ProposalQueue(memory_dir)
    queue.append(
        [_proposal("The deploy job runs on runners-large.", pid="f1", cat="fact")]
    )
    server, store = _build(memory_dir)
    res = await _accept(server, "f1", scopes=["infrastructure"])
    assert res["status"] == "accepted"
    assert [m.id for m in store.load_all()] == [res["id"]]


async def test_an_explicit_user_inference_override_stages_too(memory_dir: Path) -> None:
    """The category the accept lands with is what the handshake reads,
    whether it came from the extractor's guess or the caller's override."""
    queue = ProposalQueue(memory_dir)
    queue.append([_proposal(_PREFERENCE, pid="ov1", cat="fact")])
    server, store = _build(memory_dir)
    res = await _accept(server, "ov1", category="user-inference")
    assert res["status"] == "pending"
    assert store.load_all() == []


def test_the_shared_core_without_a_session_commits_directly(tmp_path: Path) -> None:
    """The CLI's path: no session, no staging — the human accepting is the
    confirmation, and the recorded accept event names the memory."""
    queue = ProposalQueue(tmp_path)
    queue.append([_proposal(_PREFERENCE, pid="cli1", cat="user-inference")])
    config = Config(storage=StorageConfig(directory=str(tmp_path)))
    store = Store(tmp_path)
    recorder = Recorder(root=tmp_path, session_id="sess_test", enabled=True)
    res = accept_proposal(
        store=store,
        config=config,
        recorder=recorder,
        proposal_id="cli1",
        scopes=["learning-style"],
    )
    assert res["status"] == "accepted"
    assert res["category"] == "user-inference"
    assert [m.id for m in store.load_all()] == [res["id"]]
    assert queue.load() == []
