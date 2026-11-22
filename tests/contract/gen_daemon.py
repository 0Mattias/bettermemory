"""Fixtures 7 and 8: the daemon's HTTP API and the Claude Code hooks.

- ``daemon_http.jsonl`` (`http_api`, ungated): each route of the daemon's app
  (`daemon.build_app`) over an empty store, driven in process (no socket,
  no port) with a fixed bearer token and a fixed served version: the
  request and the reply (status, the headers that matter, body). Normal
  calls and the main refusals, ``/health`` with and without a nonce, and
  an authorized ``/mcp`` exchange (initialize, then tools/list). Nothing
  here ranks or tokenizes.
- ``hooks.jsonl`` (`hooks`, gated): ``bettermemory hook <event>``
  (`_daemon_client.hook_main`) over a store of seeded records, its daemon
  the same app. The client's own `post` runs, proof of the token included;
  only the socket under it is a shim (`_Transport`) that hands each
  request to the app. Each record has the harness's stdin, every round
  trip the client made, and what it printed with its exit code.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import types
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import timedelta
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

from starlette.testclient import TestClient

from bettermemory import _daemon_client, identity, location
from bettermemory.config import (
    BehaviorConfig,
    CaptureConfig,
    Config,
    ScopesConfig,
    StorageConfig,
)
from bettermemory.daemon import MAX_BODY_BYTES, _migrate_at_start, build_app
from bettermemory.models import Confidence, Memory, Source
from bettermemory.store import Store

from ._common import jsonl
from ._sandbox import PLACEHOLDERS, Scrubber, World, world

TOKEN = "fixture-token-not-a-secret"
VERSION = "0.0.0+fixture"
PORT = 7397
BASE = f"http://127.0.0.1:{PORT}"
#: The nonce the client's proof asks with: its random source
#: (`_daemon_client.secrets.token_hex`) is fixed to it, so the proof the
#: daemon answers is pinned too.
NONCE = "00112233445566778899aabbccddeeff"
SESSION = "fixture-session-0001"
#: A session the brief never ran in, so no pointer is already seen there.
SESSION_2 = "fixture-session-0002"
#: The session the capture checkpoint opens in (its transcript is named
#: for it, as Claude Code names them).
SESSION_CAPTURE = "fixture-session-capture"

#: Reply headers worth pinning (the rest are the server stack's).
_HEADERS = ("content-type", "www-authenticate")

_SETUP = (
    "build_app(config, store, token, shutdown, version) with Config("
    "storage=StorageConfig(directory=<scratch>/mem), behavior=BehaviorConfig(), "
    "scopes=ScopesConfig()), every other setting at the package default, "
    "over Store.create(<scratch>/mem/store) migrated as a daemon start "
    "migrates it (daemon._migrate_at_start); no port is passed, so the Host "
    "check takes any port on a loopback name. Requests go to "
    f"{BASE} in process."
)


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def _config(w: World, **capture: Any) -> Config:
    cfg = Config(
        storage=StorageConfig(directory=str(w.memory_dir)),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )
    if capture:
        cfg.capture = CaptureConfig(**capture)
    return cfg


@contextmanager
def _app(w: World, cfg: Config) -> Iterator[tuple[TestClient, list[int], Store]]:
    store = Store.create(w.memory_dir / "store", keys_dir=w.keys_dir)
    # A request the app serves binds its caller (`identity.bind`,
    # `identity.bind_transcript`) and leaves it published with no token to
    # reset: the caller found here is restored on the way out, so no later
    # generator reads one of these.
    kept = identity._CURRENT.set(identity._CURRENT.get())
    try:
        _migrate_at_start(store, cfg)
        calls: list[int] = []
        app = build_app(
            config=cfg,
            store=store,
            token=TOKEN,
            shutdown=lambda: calls.append(1),
            version=VERSION,
        )
        with TestClient(app, base_url=BASE) as client:
            yield client, calls, store
    finally:
        store.close()
        identity._CURRENT.reset(kept)


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _reply(response: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "status": response.status_code,
        "headers": {h: response.headers[h] for h in _HEADERS if h in response.headers},
    }
    try:
        reply["body"] = response.json()
    except ValueError:
        reply["text"] = response.text
    return reply


def _exchange(
    client: TestClient,
    case: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    raw: bytes | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    sent = dict(headers or {})
    content: bytes | None = raw
    if body is not None:
        content = json.dumps(body).encode("utf-8")
        sent.setdefault("Content-Type", "application/json")
    response = client.request(
        method, path, headers=sent, content=content, params=params
    )
    request: dict[str, Any] = {"method": method, "path": path, "headers": sent}
    if params:
        request["query"] = params
    if body is not None:
        request["body"] = body
    elif raw is not None:
        request["body_bytes"] = (
            raw.decode("ascii") if len(raw) <= 64 else f"<{len(raw)} bytes>"
        )
    return {"case": case, "request": request, "reply": _reply(response)}


# ---------------------------------------------------------------------------
# daemon_http.jsonl
# ---------------------------------------------------------------------------

_MCP_ACCEPT = "application/json, text/event-stream"
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "fixture", "version": "0"},
    },
}


def _mcp(client: TestClient) -> list[dict[str, Any]]:
    """initialize, notifications/initialized and tools/list over /mcp; the
    session id the transport mints is written <mcp_session>."""
    headers = {**_auth(), "Accept": _MCP_ACCEPT}
    first = client.post("/mcp", headers=headers, json=_INITIALIZE)
    session = first.headers.get("mcp-session-id", "")
    out = []

    def record(case: str, body: Any, response: Any, sent: dict[str, str]) -> None:
        reply = _reply(response)
        if "mcp-session-id" in response.headers:
            reply["headers"]["mcp-session-id"] = "<mcp_session>"
        shown = {
            k: ("<mcp_session>" if k == "mcp-session-id" else v)
            for k, v in sent.items()
        }
        out.append(
            {
                "case": case,
                "request": {
                    "method": "POST",
                    "path": "/mcp",
                    "headers": shown,
                    "body": body,
                },
                "reply": reply,
            }
        )

    record(
        "mcp.initialize",
        _INITIALIZE,
        first,
        {**headers, "Content-Type": "application/json"},
    )
    later = {**headers, "Content-Type": "application/json", "mcp-session-id": session}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    record(
        "mcp.initialized", note, client.post("/mcp", headers=later, json=note), later
    )
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    record(
        "mcp.tools_list",
        listing,
        client.post("/mcp", headers=later, json=listing),
        later,
    )
    return out


def _http_cases(client: TestClient, calls: list[int]) -> list[dict[str, Any]]:
    ex = _exchange
    hook = "/api/v1/hook/session-start"
    json_auth = {**_auth(), "Content-Type": "application/json"}
    out = [
        ex(client, "health.plain", "GET", "/health"),
        ex(client, "health.nonce", "GET", "/health", params={"nonce": NONCE}),
        ex(client, "health.malformed_nonce", "GET", "/health", params={"nonce": "xyz"}),
        ex(client, "health.with_wrong_token", "GET", "/health", headers=_auth("nope")),
    ]
    for path in (
        "/api/v1/hook/session-start",
        "/api/v1/hook/prompt",
        "/api/v1/hook/stop",
        "/api/v1/admin/shutdown",
    ):
        out.append(ex(client, f"no_token {path}", "POST", path, body={}))
    out += [
        ex(client, "wrong_token", "POST", hook, headers=_auth("nope"), body={}),
        ex(
            client,
            "not_bearer",
            "POST",
            hook,
            headers={"Authorization": f"Basic {TOKEN}"},
            body={},
        ),
        ex(client, "malformed_body", "POST", hook, headers=json_auth, raw=b"{not json"),
        ex(
            client, "body_not_object", "POST", hook, headers=_auth(), body=["a", "list"]
        ),
        ex(
            client,
            "body_over_cap",
            "POST",
            hook,
            headers=json_auth,
            raw=b" " * (MAX_BODY_BYTES + 1),
        ),
        ex(client, "session_start.no_session", "POST", hook, headers=_auth(), body={}),
        ex(
            client,
            "prompt.missing_fields",
            "POST",
            "/api/v1/hook/prompt",
            headers=_auth(),
            body={},
        ),
        ex(
            client,
            "stop.missing_fields",
            "POST",
            "/api/v1/hook/stop",
            headers=_auth(),
            body={},
        ),
        ex(client, "wrong_method", "GET", hook, headers=_auth()),
        ex(
            client,
            "unknown_route",
            "POST",
            "/api/v1/hook/nope",
            headers=_auth(),
            body={},
        ),
        ex(
            client,
            "mcp.no_token",
            "POST",
            "/mcp",
            body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ),
        ex(
            client,
            "foreign_host",
            "GET",
            "/health",
            headers={"Host": "evil.example.invalid"},
        ),
    ]
    out += _mcp(client)
    out.append(
        ex(
            client,
            "shutdown",
            "POST",
            "/api/v1/admin/shutdown",
            headers=_auth(),
            body={},
        )
    )
    out[-1]["shutdown_called"] = len(calls)
    return out


def http_api() -> dict[str, bytes]:
    with world() as w:
        with _app(w, _config(w)) as (client, calls, _store):
            cases = _http_cases(client, calls)
        scrub = Scrubber(w.root, now=w.now)
        cases = scrub(cases)
    for record in cases:
        body = record["reply"].get("body")
        if isinstance(body, dict) and "pid" in body:
            body["pid"] = "<pid>"
    header = {
        "fixture": "daemon_http",
        "read": (
            "Each record is one request to the daemon's app over a new, empty "
            "store, in file order, with token "
            f"{TOKEN!r} and served version {VERSION!r}; no socket. request: "
            "method, path, the headers sent, query, and the JSON body (or "
            "body_bytes: the raw body, or its length when long); reply: "
            "status, the content-type and www-authenticate headers when "
            "present (and mcp-session-id), and the JSON body (or text: an "
            "event stream is kept as text). health.nonce's proof is the "
            "token's MAC over the nonce (`_daemon_client.health_proof`). "
            "Nothing here ranks or tokenizes."
        ),
        "setup": _SETUP,
        "max_body_bytes": MAX_BODY_BYTES,
        "placeholders": {
            **PLACEHOLDERS,
            "<pid>": "the daemon's process id",
            "<mcp_session>": "the session id the MCP transport mints (random)",
        },
    }
    return {"daemon_http.jsonl": jsonl([header, *cases])}


# ---------------------------------------------------------------------------
# hooks.jsonl
# ---------------------------------------------------------------------------

_SEED = (
    "The project zephyrcore uses ruff for linting and formatting, in pyproject.",
    "Builds in zephyrcore run with uv; the lockfile is uv.lock at the root.",
    "Release notes for zephyrcore are drafted in CHANGELOG.md before tagging.",
    "The zephyrcore test suite runs under pytest with four workers.",
    "Documentation pages for zephyrcore are built with mkdocs.",
    "The zephyrcore CI matrix covers Linux, macOS and Windows.",
)
SEED_IDS = tuple(f"01K{i + 1:023d}" for i in range(len(_SEED)))
#: Each record's age at the run, in days: well past every shield the hooks
#: apply to fresh records (`audit.DEFAULT_CREATION_SHIELD_SECONDS`, 60 s;
#: `capture_review.OWN_FILING_WINDOW`, one minute), however slow the run.
_AGES = tuple(len(_SEED) - i for i in range(len(_SEED)))


def _seed_records() -> list[dict[str, Any]]:
    return [
        {
            "id": SEED_IDS[i],
            "body": body,
            "scopes": ["projects:zephyrcore"],
            "category": "fact",
            "confidence": "medium",
            "source": Source.EXPLICIT.value,
            "age_days": _AGES[i],
        }
        for i, body in enumerate(_SEED)
    ]


def _seed(cfg: Config, store: Store, w: World) -> None:
    """The records, homed in the checkout's project, created and updated
    `age_days` before the run's clock (seconds dropped), with no standing
    answer, links, claims or verification."""
    home = location.resolve_caller(store, cfg, str(w.checkout), session=None).home()
    now = w.now.replace(microsecond=0)
    for row in _seed_records():
        created = now - timedelta(days=row["age_days"])
        memory = Memory(
            id=row["id"],
            created=created,
            updated=created,
            scopes=row["scopes"],
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body=row["body"],
        )
        store.put_memory(memory, home=home)


_TRANSCRIPT = [
    {
        "type": "user",
        "promptId": "prompt-0001",
        "message": {"role": "user", "content": "How do we lint zephyrcore?"},
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": "Run the linter it ships."}],
        },
    },
]
#: The capture session's transcript: one prompt and a reply long enough to
#: pass the checkpoint (`checkpoint_tokens` 500, four characters a token).
_CAPTURE_REPLY = "The build settled on uv and ruff for zephyrcore. " * 50
_CAPTURE_TRANSCRIPT = [
    {
        "type": "user",
        "promptId": "prompt-0002",
        "message": {"role": "user", "content": "Summarise the build decisions."},
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": _CAPTURE_REPLY}],
        },
    },
]


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> str:
    path.write_bytes(b"".join(json.dumps(r).encode("utf-8") + b"\n" for r in rows))
    return str(path)


class _Response:
    """What `http.client.HTTPResponse` gives `_daemon_client`: status,
    reason, headers, read, will_close."""

    def __init__(
        self, status: int, reason: str, headers: dict[str, str], body: bytes
    ) -> None:
        self.status = status
        self.reason = reason
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value
        self._body = io.BytesIO(body)
        self.will_close = False

    def read(self, amt: int | None = None) -> bytes:
        return self._body.read() if amt is None else self._body.read(amt)


@dataclass
class _Transport:
    """The socket under `_daemon_client`'s `http.client.HTTPConnection`:
    each request goes to the app through `client`, or is refused when the
    port is `refused`."""

    client: TestClient
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False

    def connection(self, host: str, port: int, timeout: float | None = None) -> Any:
        transport = self

        class _Connection:
            sock: object | None = object()
            auto_open = 1

            def __init__(self) -> None:
                self._response: _Response | None = None

            def request(
                self,
                method: str,
                url: str,
                body: bytes | None = None,
                headers: dict[str, str] | None = None,
            ) -> None:
                if transport.refused:
                    raise ConnectionRefusedError(111, "Connection refused")
                sent = dict(headers or {})
                response = transport.client.request(
                    method, url, headers=sent, content=body
                )
                transport.exchanges.append(
                    {
                        "method": method,
                        "url": url,
                        "headers": sent,
                        "body": json.loads(body) if body else None,
                        "status": response.status_code,
                        "reply": response.text,
                    }
                )
                self._response = _Response(
                    response.status_code,
                    response.reason_phrase,
                    dict(response.headers),
                    response.content,
                )

            def getresponse(self) -> _Response:
                assert self._response is not None
                return self._response

            def close(self) -> None:
                self.sock = None

        return _Connection()


def _client_run(
    client: TestClient,
    store: Store,
    event: str,
    stdin: dict[str, Any],
    *,
    argv: tuple[str, ...] = (),
    token: str = TOKEN,
    refused: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`hook_main` for `event` with `stdin` as the harness's payload and
    `argv` after the event: every round trip, what it printed, its exit
    code."""
    transport = _Transport(client, refused=refused)
    shim = types.SimpleNamespace(
        client=types.SimpleNamespace(
            HTTPConnection=transport.connection,
            HTTPException=http.client.HTTPException,
        )
    )
    fixed = types.SimpleNamespace(token_hex=lambda n=None: NONCE)
    out, err = io.StringIO(), io.StringIO()
    stdin_stream = io.TextIOWrapper(io.BytesIO(json.dumps(stdin).encode("utf-8")))
    saved = dict(os.environ)
    os.environ.update(env or {})
    try:
        with (
            patch.object(_daemon_client, "http", shim),
            patch.object(_daemon_client, "secrets", fixed),
            patch.object(
                _daemon_client,
                "ensure_daemon",
                lambda *a, **k: {"port": PORT, "token": token},
            ),
            patch.object(_daemon_client, "resolved_store_path", lambda: store.path),
            patch("sys.stdin", stdin_stream),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = _daemon_client.hook_main(["hook", event, *argv])
    finally:
        os.environ.clear()
        os.environ.update(saved)
    for exchange in transport.exchanges:
        body = exchange["body"]
        if isinstance(body, dict) and "location" in body:
            body["location"] = "<location_probe>"
        try:
            reply = json.loads(exchange["reply"])
        except ValueError:
            continue
        if isinstance(reply, dict) and "pid" in reply:
            reply["pid"] = "<pid>"
        exchange["reply"] = reply
    return {
        "event": event,
        "argv": list(argv),
        "env": env or {},
        "stdin": stdin,
        "exchanges": transport.exchanges,
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
        "exit_code": code,
    }


def _hook_cases(
    client: TestClient, store: Store, cfg: Config, w: World
) -> list[dict[str, Any]]:
    cwd = str(w.checkout)
    transcript = _write_rows(w.root / "transcript.jsonl", _TRANSCRIPT)
    capture = _write_rows(w.root / f"{SESSION_CAPTURE}.jsonl", _CAPTURE_TRANSCRIPT)
    start = {
        "session_id": SESSION,
        "source": "startup",
        "hook_event_name": "SessionStart",
        "transcript_path": transcript,
        "cwd": cwd,
    }
    prompt = {
        "session_id": SESSION_2,
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": transcript,
        "cwd": cwd,
        "prompt": "Which lockfile do zephyrcore builds with uv read at the root?",
    }
    stop = {
        "session_id": SESSION,
        "hook_event_name": "Stop",
        "transcript_path": transcript,
        "cwd": cwd,
    }
    run = _client_run
    cases: list[tuple[str, dict[str, Any]]] = []
    cases.append(
        ("session_start.empty_store", run(client, store, "session-start", start))
    )
    _seed(cfg, store, w)
    cases += [
        ("session_start.brief", run(client, store, "session-start", start)),
        (
            "session_start.resume",
            run(client, store, "session-start", {**start, "source": "resume"}),
        ),
        (
            "session_start.compact",
            run(client, store, "session-start", {**start, "source": "compact"}),
        ),
        (
            "session_start.clear",
            run(client, store, "session-start", {**start, "source": "clear"}),
        ),
        ("prompt.pointers", run(client, store, "prompt", prompt)),
        ("prompt.cooldown", run(client, store, "prompt", prompt)),
        (
            "prompt.empty",
            run(
                client,
                store,
                "prompt",
                {**prompt, "prompt": "Write a haiku about the sea."},
            ),
        ),
        (
            "prompt.seen_in_brief",
            run(client, store, "prompt", {**prompt, "session_id": SESSION}),
        ),
        ("stop.turn_audit", run(client, store, "stop", stop)),
        ("stop.turn_audit_quiet", run(client, store, "stop", stop, argv=("--quiet",))),
        (
            "stop.no_transcript",
            run(client, store, "stop", {"session_id": SESSION, "cwd": cwd}),
        ),
        (
            "stop.capture_checkpoint",
            run(
                client,
                store,
                "stop",
                {**stop, "session_id": SESSION_CAPTURE, "transcript_path": capture},
                argv=("--quiet",),
                env={_daemon_client.ATTENDED_ENV: "1"},
            ),
        ),
        (
            "client.wrong_token",
            run(client, store, "prompt", prompt, token="not-the-token"),
        ),
        (
            "client.daemon_unreachable",
            run(client, store, "prompt", prompt, refused=True),
        ),
        ("client.no_event", run(client, store, "", start)),
    ]
    return [{"case": case, **record} for case, record in cases]


def _restate_chars(reply: Any, scrub: Scrubber) -> None:
    """A reply's ``chars`` (the length of its ``stdout``) restated over the
    stdout as written here, the scratch path as <scratch>."""
    if isinstance(reply, dict) and isinstance(reply.get("stdout"), str):
        text = reply["stdout"]
        if isinstance(reply.get("chars"), int):
            reply["chars"] -= len(text) - len(scrub.paths(text))


def hooks() -> dict[str, bytes]:
    with world() as w:
        cfg = _config(w, stop_checkpoint=True, checkpoint_tokens=500)
        with _app(w, cfg) as (client, _calls, store):
            cases = _hook_cases(client, store, cfg, w)
        scrub = Scrubber(
            w.root, now=w.now, keep={NONCE, _daemon_client.health_proof(TOKEN, NONCE)}
        )
        for case in cases:
            for exchange in case["exchanges"]:
                _restate_chars(exchange["reply"], scrub)
        cases = scrub(cases)
    header = {
        "fixture": "hooks",
        "read": (
            "Each record runs `bettermemory hook <event> <argv>` in process "
            "(`_daemon_client.hook_main`) with stdin the harness's payload and "
            "env added to the environment; the client finds its daemon at "
            f"port {PORT} with token {TOKEN!r} (client.wrong_token: the client "
            "holds another token; client.daemon_unreachable: the connection is "
            "refused, errno 111). exchanges: each request the client sent "
            "(method, url, headers, JSON body) and the daemon's status and "
            "reply; the client's nonce is fixed (nonce). stdout, stderr, "
            "exit_code: what the client printed to Claude Code. A reply's chars "
            "is counted over its stdout as written here (a scratch path as "
            "<scratch>); the product counts the path as it is. Records run in "
            "file order on one app and store: session_start.empty_store runs "
            "before the seed records are put; the prompt recall's cooldown "
            "and the Stop audit read what earlier records wrote."
        ),
        "setup": _SETUP
        + " Here the capture checkpoint is on: Config.capture = CaptureConfig("
        "stop_checkpoint=True, checkpoint_tokens=500), its other fields at "
        "their defaults.",
        "nonce": NONCE,
        "seed": {
            "records": _seed_records(),
            "how": (
                "Store.put_memory of each record, homed in the checkout's project "
                "(location.resolve_caller(store, config, <scratch>/alpha).home()), "
                "created and updated age_days before the run's clock (seconds "
                "kept, microseconds dropped), after session_start.empty_store"
            ),
        },
        "transcripts": {
            "<scratch>/transcript.jsonl": _TRANSCRIPT,
            f"<scratch>/{SESSION_CAPTURE}.jsonl": _CAPTURE_TRANSCRIPT,
            "encoding": "one json.dumps line per row (default separators), UTF-8, \\n",
        },
        "placeholders": {
            **PLACEHOLDERS,
            "<pid>": "the daemon's process id",
            "<location_probe>": (
                "the client's namespace probe of its cwd (`_location_probe.probe`): "
                "host facts, never pinned"
            ),
        },
    }
    return {"hooks.jsonl": jsonl([header, *cases])}
