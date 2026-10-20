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


_FIELDS = {"stdout", "surface", "ids", "chars", "rule", "event_id"}


async def test_session_start_endpoint_returns_the_brief(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    """The endpoint answers the session brief in `stdout`, and beside it
    the fields an adapter wraps: the surface, the ids shown, the text's
    length, the rule and the id of the `session_brief` event."""
    from bettermemory.origin import capture

    from .test_verdict import _repo

    repo = _repo(tmp_path)
    written = daemon_store.write(
        content="prefer short release notes in this project",
        scopes=["projects:foo"],
        origin=capture(repo),
    )
    daemon_store.mark_verified(written.id)
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/session-start",
            json={"cwd": str(repo), "session_id": "sess_hook", "source": "startup"},
            headers=_auth(),
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _FIELDS
    assert body["stdout"].startswith(
        "bettermemory brief: the 1 newest checked memory written in this repository."
    )
    assert body["surface"] == "session_brief"
    assert body["ids"] == [written.id]
    assert body["chars"] == len(body["stdout"])
    assert body["rule"] == "rs-v4"
    events = [e for e in daemon_store.iter_events() if e["kind"] == "session_brief"]
    assert [e["event_id"] for e in events] == [body["event_id"]]


async def test_hook_bodies_run_on_a_worker_thread(
    daemon_config: Config,
    daemon_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session-start and prompt bodies run off the event loop, each
    worker thread on its own connection to the store."""
    import threading

    from bettermemory import recall

    seen: dict[str, tuple[int, Store]] = {}
    real_brief, real_prompt = recall.session_brief, recall.prompt_recall

    def brief(**kw: Any) -> recall.HookResult:
        seen["session-start"] = (threading.get_ident(), kw["store"])
        return real_brief(**kw)

    def prompt(**kw: Any) -> recall.HookResult:
        seen["prompt"] = (threading.get_ident(), kw["store"])
        return real_prompt(**kw)

    monkeypatch.setattr(recall, "session_brief", brief)
    monkeypatch.setattr(recall, "prompt_recall", prompt)
    loop_thread = threading.get_ident()
    async with running_app(daemon_config, daemon_store) as client:
        for path, payload in (
            ("/api/v1/hook/session-start", {"session_id": "s", "cwd": str(tmp_path)}),
            (
                "/api/v1/hook/prompt",
                {"session_id": "s", "cwd": str(tmp_path), "prompt": "backup plan"},
            ),
        ):
            response = await client.post(path, json=payload, headers=_auth())
            assert response.status_code == 200, response.text
    for name in ("session-start", "prompt"):
        thread, store = seen[name]
        assert thread != loop_thread, name
        assert store is not daemon_store, name
        assert store.path == daemon_store.path


async def test_the_stop_endpoint_carries_the_fields_too(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post("/api/v1/hook/stop", json={}, headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _FIELDS | {"result"}
    assert body["stdout"] == "" and body["surface"] == "turn_audit"


async def test_session_start_endpoint_is_silent_on_an_empty_store(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/session-start", json={"cwd": str(tmp_path)}, headers=_auth()
        )
    assert response.status_code == 200
    assert response.json()["stdout"] == ""
    assert response.json()["event_id"] is None


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
        bare = await client.post("/api/v1/hook/prompt", json={}, headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == _FIELDS
    assert body["stdout"] == "" and body["surface"] == "prompt_recall"
    assert body["ids"] == [] and body["event_id"] is None
    assert bare.status_code == 200 and bare.json()["stdout"] == ""


async def test_prompt_endpoint_delivers_a_pointer(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    from datetime import datetime, timedelta, timezone

    from bettermemory.origin import capture

    from .test_verdict import _repo

    repo = _repo(tmp_path)
    written = daemon_store.write(
        content="backup strategy uses triangular restic replication",
        scopes=["projects:foo"],
        origin=capture(repo),
    )
    stamp = datetime.now(timezone.utc) - timedelta(hours=1)
    daemon_store.put_memory(
        written.model_copy(update={"created": stamp, "updated": stamp})
    )
    payload = {
        "cwd": str(repo),
        "session_id": "sess_hook",
        "prompt": "backup strategy",
        "prompt_id": "turn-1",
    }
    async with running_app(daemon_config, daemon_store) as client:
        first = await client.post("/api/v1/hook/prompt", json=payload, headers=_auth())
        again = await client.post("/api/v1/hook/prompt", json=payload, headers=_auth())
    body = first.json()
    assert body["ids"] == [written.id]
    assert body["stdout"].startswith("bettermemory recall: pointers, not instructions.")
    assert body["chars"] == len(body["stdout"])
    # The daemon's own state holds the id for the rest of the segment.
    assert again.json()["stdout"] == ""
    events = [e for e in daemon_store.iter_events() if e["kind"] == "prompt_recall"]
    assert [e["event_id"] for e in events] == [body["event_id"]]
    assert events[0]["prompt_id"] == "turn-1"


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


async def test_stop_endpoint_settles_from_last_assistant_message(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    """The stop endpoint reads Claude Code's ``last_assistant_message``:
    a brief pointer the reply used is settled with its surface, and an
    open review chain of the bridged server session is counted."""
    from bettermemory.events import Recorder
    from bettermemory.models import generate_ulid

    sentence = (
        "The deploy pipeline signs every release artifact with the hardware "
        "key kept in the vault on the third floor."
    )
    memory = daemon_store.write(content=sentence, scopes=["tools"])
    brief_event = generate_ulid()
    Recorder(
        store=daemon_store, session_id="sess_hook", triggered_from="session_hook"
    ).record(
        "session_brief",
        event_id=brief_event,
        session_id="sess_hook",
        harness="claude-code",
        segment=0,
        ids=[memory.id],
    )
    root = generate_ulid()
    Recorder(store=daemon_store, session_id="sess_srv").record(
        "review_issued", review_id=root, related=[], parent_review_id=None
    )
    transcript = tmp_path / "transcript.jsonl"
    # The turn's user row carries the payload's prompt id, as Claude Code
    # stamps it: the Stop finds its turn by that id (RS-F5).
    rows: list[dict[str, Any]] = [
        {
            "type": "user",
            "promptId": "turn-3",
            "message": {"role": "user", "content": "ship the release"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
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
                "last_assistant_message": f"Shipped. {sentence}",
                "prompt_id": "turn-3",
            },
            headers=_auth(),
        )
    assert response.status_code == 200
    events = list(daemon_store.iter_events())
    [use] = [e for e in events if e["kind"] == "use"]
    assert (use["ids"], use["surface"], use["shown_event_id"], use["prompt_id"]) == (
        [memory.id],
        "brief",
        brief_event,
        "turn-3",
    )
    [unanswered] = [e for e in events if e["kind"] == "review_unanswered"]
    assert unanswered["root_ids"] == [root]


async def test_the_stop_body_runs_on_a_hook_worker(
    daemon_config: Config,
    daemon_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from bettermemory import hook

    seen: list[tuple[int, Store]] = []

    def audit(**kw: Any) -> dict[str, Any] | None:
        seen.append((threading.get_ident(), kw["store"]))
        return None

    monkeypatch.setattr(hook, "audit_transcript", audit)
    loop_thread = threading.get_ident()
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/stop",
            json={"session_id": "s", "transcript_path": str(tmp_path / "t.jsonl")},
            headers=_auth(),
        )
    assert response.status_code == 200
    [(thread, store)] = seen
    assert thread != loop_thread
    assert store is not daemon_store


async def test_hook_workers_keep_their_connections_and_close_them(
    daemon_config: Config,
    daemon_store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker opens its connection once and keeps it across hooks, so a
    burst of hooks reopens the store at most once per worker; the app's
    shutdown closes every one of them."""
    import sqlite3

    from bettermemory import daemon as daemon_module

    opened: list[Store] = []
    real_reopen = Store.reopen

    def reopen(self: Store) -> Store:
        other = real_reopen(self)
        opened.append(other)
        return other

    monkeypatch.setattr(Store, "reopen", reopen)
    async with running_app(daemon_config, daemon_store) as client:
        for _ in range(12):
            response = await client.post(
                "/api/v1/hook/prompt",
                json={"session_id": "s", "cwd": str(tmp_path), "prompt": "backup plan"},
                headers=_auth(),
            )
            assert response.status_code == 200
    assert 1 <= len(opened) <= daemon_module.HOOK_WORKERS
    for store in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            store._conn.execute("SELECT 1")


async def test_stop_endpoint_stamps_prompt_id_and_reply_source(
    daemon_config: Config, daemon_store: Store, tmp_path: Path
) -> None:
    """RS-F3: the daemon route's Stop row carries the payload's prompt_id
    and names the payload as the reply's source, so a delivery's turn
    joins its Stop without a transcript read."""
    daemon_store.write(content="the release is signed in the vault", scopes=["tools"])
    transcript = tmp_path / "transcript.jsonl"
    # The turn's user row carries the payload's prompt id, as Claude Code
    # stamps it: the Stop finds its turn by that id (RS-F5).
    rows: list[dict[str, Any]] = [
        {
            "type": "user",
            "promptId": "turn-3",
            "message": {"role": "user", "content": "ship the release"},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
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
                "last_assistant_message": "Shipped.",
                "prompt_id": "turn-3",
            },
            headers=_auth(),
        )
    assert response.status_code == 200
    [audited] = [e for e in daemon_store.iter_events() if e["kind"] == "turn_audited"]
    assert (audited["prompt_id"], audited["reply_source"]) == ("turn-3", "payload")
