"""The daemon's HTTP surface beyond ``/mcp``: Host and Origin on every
route, the body cap on the hook and admin routes, the bearer check that
fails closed, and the identity proof ``/health`` answers.

The MCP SDK checks Host and Origin and caps the body only inside its
``/mcp`` transport. Before 9.0.0rc12 ``/health`` answered a rebinding Host
with the store path, pid and version, and a hook route read a body of any
size. Driven in-process through an ASGI transport, as tests/test_daemon.py
drives the app, and through the middlewares alone where a request needs a
shape no HTTP client sends (no Host, a body streamed past its length).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx2
import pytest
from starlette.types import Message, Receive, Scope, Send

from bettermemory._daemon_client import HEALTH_NONCE_PARAM, health_proof
from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.daemon import (
    CAPPED_PREFIX,
    MAX_BODY_BYTES,
    BearerAuthMiddleware,
    BodyLimitMiddleware,
    LocalHostMiddleware,
    build_app,
)
from bettermemory.store import Store

from .test_daemon import NINE, TOKEN, _auth, mcp_session, running_app

PORT = 7397
NONCE = "0123456789abcdef" * 2

#: Every route but /mcp, with the method it answers: what the SDK's own
#: check never covered.
ROUTES = (
    ("GET", "/health"),
    ("POST", "/api/v1/hook/session-start"),
    ("POST", "/api/v1/hook/prompt"),
    ("POST", "/api/v1/hook/stop"),
    ("POST", "/api/v1/admin/shutdown"),
)


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
async def bound_app(
    config: Config,
    store: Store,
    *,
    port: int | None = PORT,
    shutdown_calls: list[int] | None = None,
) -> AsyncIterator[httpx2.AsyncClient]:
    """The app as `daemon.serve` builds it, told the port it bound, inside
    its lifespan behind an ASGI transport."""
    calls = shutdown_calls if shutdown_calls is not None else []
    app = build_app(
        config=config,
        store=store,
        token=TOKEN,
        shutdown=lambda: calls.append(1),
        port=port,
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url=f"http://127.0.0.1:{PORT}"
        ) as client:
            yield client


async def _request(
    client: httpx2.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    content: bytes | None = None,
) -> httpx2.Response:
    if method == "GET":
        return await client.get(path, headers=headers)
    return await client.post(
        path,
        headers={"Content-Type": "application/json", **headers},
        content=b"{}" if content is None else content,
    )


# ---------------------------------------------------------------------------
# Host and Origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_a_rebinding_host_is_refused_on_every_route(
    daemon_config: Config, daemon_store: Store, method: str, path: str
) -> None:
    """The DNS-rebinding request: a page loaded from an attacker's name
    that now resolves to 127.0.0.1 sends that name as the Host. Every
    route answers 421, as the SDK does at /mcp, with or without the token,
    and the shutdown route never runs."""
    calls: list[int] = []
    async with bound_app(daemon_config, daemon_store, shutdown_calls=calls) as client:
        for headers in (
            {"Host": f"attacker.example:{PORT}"},
            {"Host": "attacker.example"},
            _auth(Host=f"attacker.example:{PORT}"),
            _auth(Host=f"attacker.example:{PORT}", Origin=f"http://127.0.0.1:{PORT}"),
        ):
            response = await _request(client, method, path, headers=headers)
            assert response.status_code == 421, (path, headers)
            assert "store" not in response.text
    assert calls == []


async def test_an_mcp_session_runs_through_the_checks(
    daemon_config: Config, daemon_store: Store
) -> None:
    """What the shim sends, Host from the URL and no Origin, passes every
    check on the app `serve` builds, port pinned."""
    async with bound_app(daemon_config, daemon_store) as client:
        async with mcp_session(client, headers=_auth()) as session:
            listed = await session.list_tools()
    assert {tool.name for tool in listed.tools} == NINE


async def test_mcp_refuses_a_rebinding_host_as_before(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with bound_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=_auth(Host=f"attacker.example:{PORT}"),
        )
    assert response.status_code == 421


@pytest.mark.parametrize(
    "host",
    [
        f"127.0.0.1:{PORT}",
        f"localhost:{PORT}",
        f"[::1]:{PORT}",
        f"LOCALHOST:{PORT}",
        "127.0.0.1",
        "localhost",
        "[::1]",
    ],
)
async def test_a_loopback_host_passes(
    daemon_config: Config, daemon_store: Store, host: str
) -> None:
    async with bound_app(daemon_config, daemon_store) as client:
        response = await client.get("/health", headers={"Host": host})
    assert response.status_code == 200, host
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8080",
        "localhost:1",
        "[::1]:8080",
        "127.0.0.1:",
        f"127.0.0.1:{PORT}:{PORT}",
        f"127.0.0.1.attacker.example:{PORT}",
        f"localhost.:{PORT}",
        f"::1:{PORT}",
        "[::1",
        f"[::1]x{PORT}",
        f"user@localhost:{PORT}",
        "0.0.0.0",
        "",
    ],
)
async def test_a_host_that_is_not_this_daemon_is_refused(
    daemon_config: Config, daemon_store: Store, host: str
) -> None:
    """Told its port, the daemon takes a loopback name bare or with that
    port only: another port is another listener's name, and a name that
    merely starts like a loopback one is not one."""
    async with bound_app(daemon_config, daemon_store) as client:
        response = await client.get("/health", headers={"Host": host})
    assert response.status_code == 421, host


async def test_an_app_built_without_its_port_takes_any_loopback_port(
    daemon_config: Config, daemon_store: Store
) -> None:
    """An app built for a test, told no port, takes a loopback name with
    any numeric port, as the SDK's own check does; a non-numeric port and
    a foreign name are still refused."""
    async with bound_app(daemon_config, daemon_store, port=None) as client:
        ok = await client.get("/health", headers={"Host": "localhost:8080"})
        bad_port = await client.get("/health", headers={"Host": "localhost:80a"})
        foreign = await client.get("/health", headers={"Host": "attacker.example:80"})
    assert ok.status_code == 200
    assert bad_port.status_code == 421
    assert foreign.status_code == 421


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_a_foreign_origin_is_refused(
    daemon_config: Config, daemon_store: Store, method: str, path: str
) -> None:
    async with bound_app(daemon_config, daemon_store) as client:
        for origin in (
            f"http://attacker.example:{PORT}",
            "http://attacker.example",
            f"https://localhost:{PORT}",
            "http://localhost:3000",
            f"http://localhost:{PORT}/",
            "null",
            "",
        ):
            response = await _request(
                client, method, path, headers=_auth(Origin=origin)
            )
            assert response.status_code == 403, (path, origin)


@pytest.mark.parametrize(
    "origin",
    [None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", "http://[::1]"],
)
async def test_no_origin_or_a_loopback_origin_passes(
    daemon_config: Config, daemon_store: Store, origin: str | None
) -> None:
    headers = _auth() if origin is None else _auth(Origin=origin)
    async with bound_app(daemon_config, daemon_store) as client:
        health = await client.get("/health", headers=headers)
        hook = await client.post(
            "/api/v1/hook/prompt", json={"session_id": "s"}, headers=headers
        )
    assert health.status_code == 200
    assert hook.status_code == 200


class _Inner:
    """An ASGI app that records whether a request reached it."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bodies: list[bytes] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls.append(scope["type"])
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        self.bodies.append(body)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _scope(
    headers: list[tuple[bytes, bytes]], *, path: str = "/health", kind: str = "http"
) -> dict[str, Any]:
    return {
        "type": kind,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
    }


async def _drive(
    app: Any, scope: dict[str, Any], chunks: list[bytes] | None = None
) -> list[Message]:
    """Run `app` on `scope`, its body sent as `chunks`; what it sent."""
    pending: list[Message] = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks or [b""]) - 1,
        }
        for index, chunk in enumerate(chunks or [b""])
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: list[Message]) -> int | None:
    for message in sent:
        if message["type"] == "http.response.start":
            return int(message["status"])
    return None


async def test_a_request_without_a_host_header_is_refused() -> None:
    inner = _Inner()
    sent = await _drive(LocalHostMiddleware(inner, port=PORT), _scope([]))
    assert _status(sent) == 421
    assert inner.calls == []


async def test_every_host_and_origin_header_is_checked() -> None:
    """A second Host or Origin cannot carry what the first was refused
    for, nor smuggle past one that passed."""
    inner = _Inner()
    middleware = LocalHostMiddleware(inner, port=PORT)
    good = (b"host", f"127.0.0.1:{PORT}".encode())
    assert _status(await _drive(middleware, _scope([good]))) == 204
    second_host = await _drive(middleware, _scope([good, (b"Host", b"evil.example")]))
    assert _status(second_host) == 421
    origins = [
        good,
        (b"origin", f"http://localhost:{PORT}".encode()),
        (b"Origin", b"http://evil.example"),
    ]
    assert _status(await _drive(middleware, _scope(origins))) == 403
    assert inner.calls == ["http"]


async def test_the_host_check_passes_scopes_that_are_not_http() -> None:
    """The lifespan reaches the app, and a websocket is left to the bearer
    check, which refuses it."""
    inner = _Inner()
    middleware = LocalHostMiddleware(inner, port=PORT)
    await _drive(middleware, {"type": "lifespan"})
    await _drive(middleware, _scope([], kind="websocket"))
    assert inner.calls == ["lifespan", "websocket"]


# ---------------------------------------------------------------------------
# The body cap
# ---------------------------------------------------------------------------


def test_the_cap_is_the_sdks_own() -> None:
    from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE

    assert MAX_BODY_BYTES == DEFAULT_MAX_REQUEST_BODY_SIZE == 4 * 1024 * 1024
    assert CAPPED_PREFIX == "/api/v1/"


def _oversized() -> bytes:
    """A JSON body one byte over the cap."""
    head = b'{"session_id": "s", "prompt": "'
    tail = b'"}'
    return head + b"x" * (MAX_BODY_BYTES + 1 - len(head) - len(tail)) + tail


@pytest.mark.parametrize(("method", "path"), [r for r in ROUTES if r[0] == "POST"])
async def test_an_oversized_body_is_refused_before_the_route_reads_it(
    daemon_config: Config, daemon_store: Store, method: str, path: str
) -> None:
    calls: list[int] = []
    body = _oversized()
    assert len(body) == MAX_BODY_BYTES + 1
    async with bound_app(daemon_config, daemon_store, shutdown_calls=calls) as client:
        response = await _request(client, method, path, headers=_auth(), content=body)
    assert response.status_code == 413, path
    assert calls == []


async def test_a_body_at_the_cap_reaches_the_hook(
    daemon_config: Config, daemon_store: Store
) -> None:
    head = b'{"session_id": "s", "prompt": "'
    tail = b'"}'
    body = head + b"x" * (MAX_BODY_BYTES - len(head) - len(tail)) + tail
    assert len(body) == MAX_BODY_BYTES
    async with bound_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/prompt",
            content=body,
            headers=_auth(**{"Content-Type": "application/json"}),
        )
    assert response.status_code == 200
    assert response.json()["surface"] == "prompt_recall"


async def test_the_token_is_checked_before_any_body_is_read(
    daemon_config: Config, daemon_store: Store
) -> None:
    """A client without the token is refused at 401 whatever the size of
    its body: it cannot make the daemon hold one."""
    async with bound_app(daemon_config, daemon_store) as client:
        response = await client.post(
            "/api/v1/hook/prompt",
            content=_oversized(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 401


async def test_a_declared_length_over_the_cap_is_refused_unread() -> None:
    inner = _Inner()
    middleware = BodyLimitMiddleware(inner, max_bytes=10, prefix=CAPPED_PREFIX)
    scope = _scope([(b"content-length", b"11")], path="/api/v1/hook/prompt")
    reads: list[int] = []

    async def receive() -> Message:
        reads.append(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    assert _status(sent) == 413
    assert reads == []
    assert inner.calls == []


async def test_a_streamed_body_past_the_cap_is_refused() -> None:
    """No Content-Length, or one that understates the body: the chunk that
    passes the cap is refused as it arrives."""
    inner = _Inner()
    middleware = BodyLimitMiddleware(inner, max_bytes=10, prefix=CAPPED_PREFIX)
    path = "/api/v1/hook/stop"
    unsized = await _drive(middleware, _scope([], path=path), [b"123456", b"78901"])
    assert _status(unsized) == 413
    understated = await _drive(
        middleware,
        _scope([(b"content-length", b"4")], path=path),
        [b"1234", b"5678", b"901"],
    )
    assert _status(understated) == 413
    assert inner.calls == []


async def test_a_body_under_the_cap_is_replayed_whole() -> None:
    inner = _Inner()
    middleware = BodyLimitMiddleware(inner, max_bytes=10, prefix=CAPPED_PREFIX)
    path = "/api/v1/hook/stop"
    sent = await _drive(
        middleware,
        _scope([(b"content-length", b"not a number")], path=path),
        [b"12345", b"67890"],
    )
    assert _status(sent) == 204
    assert inner.bodies == [b"1234567890"]


async def test_a_client_that_went_away_reaches_the_route_as_a_disconnect() -> None:
    seen: list[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(await receive())
        seen.append(await receive())

    middleware = BodyLimitMiddleware(inner, max_bytes=10, prefix=CAPPED_PREFIX)

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        return

    await middleware(_scope([], path="/api/v1/hook/stop"), receive, send)
    assert [message["type"] for message in seen] == ["http.disconnect"] * 2


async def test_the_cap_leaves_other_paths_alone() -> None:
    """/mcp has the SDK's own cap and /health reads no body."""
    inner = _Inner()
    middleware = BodyLimitMiddleware(inner, max_bytes=10, prefix=CAPPED_PREFIX)
    sent = await _drive(
        middleware,
        _scope([(b"content-length", b"20")], path="/mcp"),
        [b"x" * 20],
    )
    assert _status(sent) == 204
    assert inner.bodies == [b"x" * 20]
    await _drive(middleware, {"type": "lifespan"})
    assert inner.calls == ["http", "lifespan"]


# ---------------------------------------------------------------------------
# The bearer check fails closed
# ---------------------------------------------------------------------------


async def test_a_websocket_is_refused_by_the_bearer_check() -> None:
    inner = _Inner()
    middleware = BearerAuthMiddleware(inner, token=TOKEN, open_paths=frozenset())
    authorized = [(b"authorization", f"Bearer {TOKEN}".encode())]
    sent = await _drive(middleware, _scope(authorized, path="/mcp", kind="websocket"))
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]
    assert inner.calls == []


async def test_a_scope_of_an_unknown_type_never_reaches_the_app() -> None:
    inner = _Inner()
    middleware = BearerAuthMiddleware(inner, token=TOKEN, open_paths=frozenset())
    sent = await _drive(middleware, _scope([], kind="webtransport"))
    assert sent == []
    assert inner.calls == []
    await _drive(middleware, {"type": "lifespan"})
    assert inner.calls == ["lifespan"]


async def test_a_websocket_to_the_built_app_is_refused(
    daemon_config: Config, daemon_store: Store
) -> None:
    """Through the whole stack, an open path and the token included."""
    app = build_app(
        config=daemon_config,
        store=daemon_store,
        token=TOKEN,
        shutdown=lambda: None,
        port=PORT,
    )
    headers = [
        (b"host", f"127.0.0.1:{PORT}".encode()),
        (b"authorization", f"Bearer {TOKEN}".encode()),
    ]
    async with app.router.lifespan_context(app):
        for path in ("/health", "/mcp", "/api/v1/hook/prompt"):
            sent = await _drive(app, _scope(headers, path=path, kind="websocket"))
            assert [message["type"] for message in sent] == ["websocket.close"], path
            assert sent[0]["code"] == 1008


# ---------------------------------------------------------------------------
# The identity proof on /health
# ---------------------------------------------------------------------------


async def test_health_without_a_nonce_is_byte_for_byte_as_before(
    daemon_config: Config, daemon_store: Store
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.get("/health")
    body = response.json()
    assert set(body) == {"status", "version", "store", "pid", "store_orphans"}
    assert response.content == json.dumps(
        body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    assert list(body) == ["status", "version", "store", "pid", "store_orphans"]


@pytest.mark.parametrize("nonce", [NONCE, "AB" * 16, "f" * 128])
async def test_health_proves_the_token_over_a_nonce(
    daemon_config: Config, daemon_store: Store, nonce: str
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.get("/health", params={HEALTH_NONCE_PARAM: nonce})
    body = response.json()
    assert response.status_code == 200
    expected = hmac.new(
        TOKEN.encode(), b"bettermemory-health-v1:" + nonce.encode(), hashlib.sha256
    ).hexdigest()
    assert body["proof"] == expected == health_proof(TOKEN, nonce)
    assert body["proof"] != health_proof("another-token", nonce)
    assert list(body)[:-1] == ["status", "version", "store", "pid", "store_orphans"]


@pytest.mark.parametrize(
    "nonce", ["", "0" * 31, "0" * 129, "g" * 32, NONCE + " ", "é" * 32]
)
async def test_a_nonce_of_another_shape_gets_no_proof(
    daemon_config: Config, daemon_store: Store, nonce: str
) -> None:
    async with running_app(daemon_config, daemon_store) as client:
        response = await client.get("/health", params={HEALTH_NONCE_PARAM: nonce})
    assert response.status_code == 200
    assert "proof" not in response.json()
