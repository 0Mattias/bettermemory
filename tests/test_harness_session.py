"""The harness-session join (D10).

The harness's own session id (``CLAUDE_CODE_SESSION_ID`` in the client's
environment) is the id the hooks name in their payloads. The stdio shim
forwards it as ``x-bettermemory-harness-session``; the daemon stamps it on
the tool kinds the recall surface joins (show, search, use, write, update,
verify); an in-process server or CLI with no daemon reads the environment
directly. The hooks then join tool events to their own rows by that stamp
first, the 9.0.0 worktree bridge only when nothing carries it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest

from bettermemory import identity
from bettermemory import shim as shim_module
from bettermemory._daemon_client import HARNESS_SESSION_ENV, daemon_env_for
from bettermemory.config import Config
from bettermemory.events import HARNESS_STAMPED_KINDS, Recorder
from bettermemory.hook import bridged_session, joined_sessions
from bettermemory.identity import (
    ENV_HARNESS_SESSION,
    HEADER_HARNESS_SESSION,
    HEADER_SESSION,
)
from bettermemory.recall import RecallState
from bettermemory.store import Store

from .test_daemon import _auth, mcp_session, running_app
from .test_recall_prompt import PromptWorld

HARNESS = "0b9e6f6a-7d0c-4a58-9b8e-harness00001"


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _HttpContext:
    """A request context that came over HTTP."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.request = _Request(headers)


# ---------------------------------------------------------------------------
# The shim produces the header, and it survives the rebind and a resend
# ---------------------------------------------------------------------------


def test_the_shim_forwards_the_harness_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_HARNESS_SESSION, HARNESS)
    headers = shim_module._forward_headers(None)
    assert headers[HEADER_HARNESS_SESSION] == HARNESS


def test_no_header_without_the_variable() -> None:
    assert HEADER_HARNESS_SESSION not in shim_module._forward_headers(None)


def test_the_header_survives_the_rebind(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rebind drops every x-bettermemory-* header but the per-process
    session before setting the call's own: the harness header is among
    the call's own, so it is set again on every call."""
    monkeypatch.setenv(ENV_HARNESS_SESSION, HARNESS)
    headers = httpx2.Headers(
        {
            HEADER_SESSION: "sess_shim",
            HEADER_HARNESS_SESSION: "stale",
            identity.HEADER_MODEL: "left-over",
        }
    )
    shim_module._rebind(headers, shim_module._forward_headers(None))
    assert headers[HEADER_SESSION] == "sess_shim"
    assert headers[HEADER_HARNESS_SESSION] == HARNESS
    assert identity.HEADER_MODEL not in headers


class _RefusingOnce(httpx2.AsyncBaseTransport):
    """A daemon that refuses the first request with 401 (a restart minted a
    new token) and answers the next; it keeps every request's headers."""

    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(dict(request.headers))
        status = 401 if len(self.seen) == 1 else 200
        return httpx2.Response(status, json={"ok": True}, request=request)


async def test_the_header_survives_a_401_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_HARNESS_SESSION, HARNESS)
    inner = _RefusingOnce()

    def find() -> tuple[dict[str, Any] | None, str | None]:
        return {"port": 7398, "token": "b" * 64, "pid": 1, "version": "x"}, None

    upstream = shim_module._Upstream({"port": 7397, "token": "a" * 64}, find)
    transport = shim_module._ForwardingTransport(inner, upstream)
    async with httpx2.AsyncClient(
        transport=transport, headers={HEADER_SESSION: "sess_shim"}
    ) as client:
        shim_module._rebind(client.headers, shim_module._forward_headers(None))
        response = await client.post("http://127.0.0.1:7397/mcp", json={})
    assert response.status_code == 200
    assert len(inner.seen) == 2
    assert [seen[HEADER_HARNESS_SESSION] for seen in inner.seen] == [HARNESS, HARNESS]
    assert inner.seen[1]["authorization"] == f"Bearer {'b' * 64}"


# ---------------------------------------------------------------------------
# Resolution: the header over HTTP, the environment off it, never the
# daemon's environment
# ---------------------------------------------------------------------------


def test_the_header_names_the_harness_session() -> None:
    caller = identity.resolve(
        _HttpContext({HEADER_HARNESS_SESSION: HARNESS}),
        environ={ENV_HARNESS_SESSION: "from-env"},
    )
    assert caller.harness_session == HARNESS


def test_the_daemon_never_reads_its_own_environment() -> None:
    """Over HTTP the environment belongs to whichever client started the
    daemon; a request without the header declares no harness session."""
    caller = identity.resolve(
        _HttpContext({}), environ={ENV_HARNESS_SESSION: "the-first-client"}
    )
    assert caller.harness_session is None


def test_the_in_process_path_reads_the_environment() -> None:
    caller = identity.resolve(None, environ={ENV_HARNESS_SESSION: HARNESS})
    assert caller.harness_session == HARNESS


def test_a_daemon_is_started_without_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_HARNESS_SESSION, HARNESS)
    env = daemon_env_for(tmp_path / "store" / "bettermemory.db")
    assert ENV_HARNESS_SESSION not in env
    assert HARNESS_SESSION_ENV == ENV_HARNESS_SESSION


def test_the_transcript_caller_declares_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hooks name their session in the payload and record under it."""
    monkeypatch.setenv(ENV_HARNESS_SESSION, HARNESS)
    caller = identity.bind_transcript(session_id="t", model=None)
    assert caller.harness_session is None


# ---------------------------------------------------------------------------
# The stamp: the six tool kinds only
# ---------------------------------------------------------------------------


def test_the_recorder_stamps_the_tool_kinds_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI path: no request, the environment answers."""
    monkeypatch.setenv(ENV_HARNESS_SESSION, HARNESS)
    store = Store(tmp_path / "store")
    token = identity.publish(identity.resolve(None))
    try:
        recorder = Recorder(store=store, session_id="sess_cli")
        kinds = sorted(HARNESS_STAMPED_KINDS) + [
            "turn_audited",
            "search_miss",
            "review_issued",
            "list",
        ]
        for kind in kinds:
            recorder.record(kind, n=1)
    finally:
        identity.reset(token)
    stamped = {e["kind"]: e.get("harness_session") for e in store.iter_events()}
    assert HARNESS_STAMPED_KINDS == {
        "show",
        "search",
        "use",
        "write",
        "update",
        "verify",
    }
    for kind in HARNESS_STAMPED_KINDS:
        assert stamped[kind] == HARNESS, kind
    for kind in ("turn_audited", "search_miss", "review_issued", "list"):
        assert stamped[kind] is None, kind


async def test_the_daemon_stamps_tool_events_from_the_header(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        headers = _auth(
            **{HEADER_SESSION: "sess_shim", HEADER_HARNESS_SESSION: HARNESS}
        )
        async with mcp_session(client, headers=headers) as session:
            written = await session.call_tool(
                "memory_write",
                {
                    "content": "the harness header joins tool rows to hook rows",
                    "scopes": ["tools"],
                },
            )
            assert not written.is_error
            await session.call_tool("memory_search", {"query": "harness header"})
    events = list(daemon_store.iter_events())
    tool_rows = [e for e in events if e["kind"] in ("write", "search")]
    assert {e["kind"] for e in tool_rows} == {"write", "search"}
    assert all(e.get("harness_session") == HARNESS for e in tool_rows)


async def test_no_stamp_without_the_header(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        async with mcp_session(client, headers=_auth()) as session:
            await session.call_tool("memory_search", {"query": "anything at all"})
    searches = [e for e in daemon_store.iter_events() if e["kind"] == "search"]
    assert searches and all("harness_session" not in e for e in searches)


@pytest.fixture
def daemon_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


@pytest.fixture
def daemon_config(tmp_path: Path) -> Config:
    from bettermemory.config import BehaviorConfig, ScopesConfig, StorageConfig

    return Config(
        storage=StorageConfig(directory=str(tmp_path / "store")),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )


# ---------------------------------------------------------------------------
# The join: the harness stamp first, the bridge only when absent
# ---------------------------------------------------------------------------


def _ev(kind: str, session: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "session": session, "ts": "2026-10-11T10:00:00Z", **fields}


def test_the_harness_stamp_beats_the_bridge() -> None:
    events = [
        _ev("show", "sess_ours", harness_session=HARNESS, worktree_root="/a"),
        _ev("scope_disable", "sess_newer", worktree_root="/a"),
    ]
    assert bridged_session(events, harness_session=HARNESS, worktree_root="/a") == (
        "sess_ours"
    )
    assert joined_sessions(events, harness_session=HARNESS, worktree_root="/a") == {
        "sess_ours"
    }


def test_the_bridge_answers_when_nothing_is_stamped() -> None:
    events = [_ev("show", "sess_old"), _ev("search", "sess_new")]
    assert bridged_session(events, harness_session=HARNESS) == "sess_new"


def test_a_session_stamped_for_another_conversation_is_never_the_bridge() -> None:
    events = [
        _ev("search", "sess_mine"),
        _ev("show", "sess_theirs", harness_session="another"),
        _ev("scope_disable", "sess_theirs"),
    ]
    assert bridged_session(events, harness_session=HARNESS) == "sess_mine"


def test_hook_rows_are_not_joined() -> None:
    events = [
        _ev("use", "sess_hook", harness_session=HARNESS, triggered_from="stop_hook")
    ]
    assert joined_sessions(events, harness_session=HARNESS) == set()


# ---------------------------------------------------------------------------
# The recall's "retrieved in this segment" gate joins by the stamp
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> PromptWorld:
    return PromptWorld(tmp_path)


def test_a_stamped_retrieval_anywhere_holds_the_pointer_back(
    world: PromptWorld,
) -> None:
    """A show by this conversation's server, recorded from another
    worktree and under a session the bridge would not pick, still counts:
    the stamp names the conversation."""
    memory = world.write()
    Recorder(store=world.store, session_id="sess_ours", worktree_root="/else").record(
        "show", id=memory.id, harness_session="transcript-recall"
    )
    Recorder(
        store=world.store, session_id="sess_newer", worktree_root=str(world.worktree)
    ).record("scope_enable", scope="nothing")
    assert world.recall(state=RecallState(background=False)).stdout == ""


def test_another_conversations_stamped_retrieval_does_not(world: PromptWorld) -> None:
    """The worktree half no longer reaches a stamped row: a concurrent
    conversation's show in the same checkout says nothing about this one."""
    memory = world.write()
    Recorder(
        store=world.store, session_id="sess_theirs", worktree_root=str(world.worktree)
    ).record("show", id=memory.id, harness_session="another-conversation")
    assert world.recall(state=RecallState(background=False)).ids == (memory.id,)


def test_an_unstamped_retrieval_in_the_worktree_still_holds_it_back(
    world: PromptWorld,
) -> None:
    memory = world.write()
    Recorder(
        store=world.store, session_id="sess_old", worktree_root=str(world.worktree)
    ).record("show", id=memory.id)
    assert world.recall(state=RecallState(background=False)).stdout == ""
