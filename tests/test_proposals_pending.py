"""A `user-inference` proposal accepted through the MCP tool commits; it
does not go pending.

The extractor stamps first-person preferences `user-inference` by
default. From 7.3.0 an accept of one from a session staged a pending
write, mirroring the pending gate `memory_write` then applied to that
category, so a captured preference waited on a confirmation round trip
before it landed. Both stagings are gone: every category commits on
accept, from the MCP tool and the CLI alike, and the label rides on the
record, which is what keeps the inference distinguishable and
correctable. The accept is still the confirmation step the queue's
contract rests on, so `require_write_confirmation` does not add a
second one here, for this category or any other.
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


def _build(root: Path, *, confirm: bool = False) -> tuple[Any, Store]:
    cfg = Config(
        storage=StorageConfig(directory=str(root)),
        behavior=BehaviorConfig(
            full_tool_surface=True, require_write_confirmation=confirm
        ),
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


async def test_a_user_inference_accept_commits_with_its_label(
    memory_dir: Path,
) -> None:
    queue = ProposalQueue(memory_dir)
    queue.append([_proposal(_PREFERENCE, pid="ui1", cat="user-inference")])
    server, store = _build(memory_dir)

    res = await _accept(server, "ui1")
    assert res["status"] == "accepted"
    assert res["action"] == "accept" and res["proposal_id"] == "ui1"
    assert res["category"] == "user-inference"
    assert "pending_id" not in res and "hint" not in res
    assert queue.load() == []
    stored = store.load_one(res["id"])
    assert stored.category is not None and stored.category.value == "user-inference"
    assert stored.scopes == ["learning-style"]
    assert stored.source.value == "inferred"
    assert stored.body.strip() == _PREFERENCE
    # One accept event, naming the memory it created — no staged row.
    accepts = [
        e
        for e in iter_events(memory_dir)
        if e["kind"] == "memory_proposals" and e.get("action") == "accept"
    ]
    assert [(e.get("status"), e.get("id")) for e in accepts] == [(None, res["id"])]
    shown = await _call(server, "memory_show", id=res["id"])
    assert shown["provenance"] == "local"


async def test_the_accept_stages_nothing_for_the_session(memory_dir: Path) -> None:
    """No staged row is left for `memory_scope_overview` to report as a
    dangling confirmation."""
    queue = ProposalQueue(memory_dir)
    queue.append([_proposal(_PREFERENCE, pid="ui2", cat="user-inference")])
    server, _ = _build(memory_dir)
    await _accept(server, "ui2")
    overview = await _call(server, "memory_scope_overview")
    assert overview["pending_writes"] == 0


async def test_a_fact_proposal_still_commits_on_accept(memory_dir: Path) -> None:
    queue = ProposalQueue(memory_dir)
    queue.append(
        [_proposal("The deploy job runs on runners-large.", pid="f1", cat="fact")]
    )
    server, store = _build(memory_dir)
    res = await _accept(server, "f1", scopes=["infrastructure"])
    assert res["status"] == "accepted"
    assert [m.id for m in store.load_all()] == [res["id"]]


async def test_an_explicit_user_inference_override_commits_too(
    memory_dir: Path,
) -> None:
    """The category the accept lands with is what the record carries,
    whether it came from the extractor's guess or the caller's override."""
    queue = ProposalQueue(memory_dir)
    queue.append([_proposal(_PREFERENCE, pid="ov1", cat="fact")])
    server, store = _build(memory_dir)
    res = await _accept(server, "ov1", category="user-inference")
    assert res["status"] == "accepted"
    [stored] = store.load_all()
    assert stored.category is not None and stored.category.value == "user-inference"


async def test_the_global_flag_does_not_stage_an_accept(memory_dir: Path) -> None:
    """Accepting IS the confirmation step this queue is built on, so the
    opt-in flag adds no second one — for `user-inference` exactly as for
    every other category."""
    queue = ProposalQueue(memory_dir)
    queue.append([_proposal(_PREFERENCE, pid="cf1", cat="user-inference")])
    server, store = _build(memory_dir, confirm=True)
    res = await _accept(server, "cf1")
    assert res["status"] == "accepted"
    assert [m.id for m in store.load_all()] == [res["id"]]


def test_the_shared_core_commits_directly(tmp_path: Path) -> None:
    """The CLI's path, which shares the core: the same commit, and the
    recorded accept event names the memory."""
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
