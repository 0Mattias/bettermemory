"""The bettermemory daemon: one process that owns the store, the recorder
and the session registry, serving the MCP surface over streamable HTTP at
``/mcp`` and the three hook bodies at ``/api/v1/hook/<event>``.

Binds 127.0.0.1 only. Every request except ``GET /health`` carries the
bearer token the state file holds (`_daemon_client.write_state`); a
process running as the user can read that file, which is the threat
model of a localhost daemon. Every HTTP request, ``/health`` and the hook
and admin routes as well as ``/mcp``, must name a loopback host in its
Host header and, when it carries one, a loopback ``http://`` Origin
(`LocalHostMiddleware`): a page a browser loaded from an attacker's name
rebound to 127.0.0.1 is refused before any route runs, as the SDK's own
check (which stays on) refuses it at ``/mcp``. The hook and admin routes
read at most `MAX_BODY_BYTES` of body (`BodyLimitMiddleware`), the SDK's
cap at ``/mcp``. ``GET /health?nonce=`` answers a proof that the daemon
holds the state file's token (`_daemon_client.health_proof`), so a
client can tell its own daemon from another listener on the port.

The MCP app is the one `builder.build_server` returns, served stateless
with JSON responses for 2026-07-28 clients; per-client session state is
keyed by `identity.registry_key` on the ``x-bettermemory-session`` header
the stdio shim sets per process (`shim.py`). The hook bodies are the
recall surface's (`recall.session_brief`, `recall.prompt_recall`) and the
Stop audit's (`hook.audit_transcript`), so a hook is one HTTP round trip
instead of a process that imports the SDK and opens the store.

The three hook bodies run on the daemon's hook workers and the nine tool
bodies on its tool workers (`_Workers`, two pools): a few long-lived
threads each, so their store reads and their git work never block the
event loop the MCP surface is served from, and a tool call waiting on
git holds no hook up. SQLite binds a connection to the thread that
opened it, so each worker opens its own connection to the store once
(`Store.reopen`), keeps it for the daemon's life and closes it when the
app shuts down; a tool body runs over a copy of the handler bundle bound
to its worker's connection (`ToolHandlers.with_store`). The recall
surface's per-session state (`recall.RecallState`) is the daemon's, one
for its lifetime, lock-guarded, as is each MCP session's state
(`session.SessionState.lock`): two tool calls of one session may run on
two workers at once.

Every hook endpoint answers ``stdout`` (the text the harness injects,
unchanged) and beside it ``surface``, ``ids``, ``chars``, ``rule`` and
``event_id``: what an adapter for another harness wraps.

The session-start and prompt bodies resolve the caller's membership
first (`location.resolve_hook`, design 3.1 and 8.1) from what the hook
client posted: its ``location`` (a namespace probe, or the location
token), and in later rounds its ``machine`` and ``answers``. Resolutions
of one location run one at a time in this process (a per-location lock),
each inside the model's ``BEGIN IMMEDIATE`` transaction, which re-checks
the object key, so the hook workers and a CLI process beside the daemon
register a new checkout once (E28). A reply's ``membership`` field carries
the witness exchange's next question to a client whose namespace the
daemon cannot read, or the request to re-mint its token (design 8.7).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import inspect
import json
import logging
import os
import queue
import secrets
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Coroutine, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TypeVar, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from ._daemon_client import (
    DAEMON_HELD_STATUS,
    HEALTH_NONCE_PARAM,
    HOST,
    LOCK_WAIT_SECONDS,
    PROOF_STATE_KEY,
    acquire_lock,
    health_proof,
    lock_file_for,
    release_lock,
    remove_state,
    valid_nonce,
    write_state,
)
from ._fsutil import (
    ensure_owner_only_dir,
    fsync_dir,
    replace_atomic,
    sqlite_file_uri,
)
from .builder import build_server
from .config import Config
from .store import (
    CREATED_AFTER_ORPHANS,
    HOOK_BUSY_TIMEOUT_SECONDS,
    TOOL_BUSY_TIMEOUT_SECONDS,
    Store,
    StoreRefusedError,
    admit_store,
    note_residue,
    orphan_names,
    orphan_order,
)
from .time_utils import parse_event_ts

log = logging.getLogger("bettermemory.daemon")

OPEN_PATHS: frozenset[str] = frozenset({"/health"})

#: The host names a request to the daemon may name, in its Host header and
#: in an Origin: the loopback names a local client puts in a URL, the ones
#: the SDK allows at ``/mcp`` (`mcp.server.lowlevel.server`, its default
#: transport security for a localhost server).
LOCAL_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "[::1]"})

#: The largest body the hook and admin routes read: 4 MiB, the SDK's own
#: cap at ``/mcp`` (`mcp.server.transport_security.
#: DEFAULT_MAX_REQUEST_BODY_SIZE`), so one bound covers every route. A
#: hook's body is far under it: the client reads at most 1 MiB of the
#: harness's stdin (`_daemon_client._STDIN_CAP_BYTES`).
MAX_BODY_BYTES = 4 * 1024 * 1024
#: The routes `MAX_BODY_BYTES` covers: the hook routes and the shutdown
#: route. ``/mcp`` has the SDK's cap and ``/health`` reads no body.
CAPPED_PREFIX = "/api/v1/"


def _host_port(text: str) -> tuple[str, str | None] | None:
    """``host[:port]`` split into the host, lowercased, and the port text
    (None when there is none); None when `text` has no such shape. An IPv6
    literal keeps its brackets, the way a Host header and an Origin
    spell it."""
    text = text.lower()
    if text.startswith("["):
        close = text.find("]")
        if close < 0:
            return None
        host, rest = text[: close + 1], text[close + 1 :]
        if not rest:
            return host, None
        if not rest.startswith(":"):
            return None
        return host, rest[1:]
    host, colon, port = text.rpartition(":")
    if not colon:
        return text, None
    return host, port


class LocalHostMiddleware:
    """Pure ASGI, outermost: refuse every HTTP request that does not name
    this daemon on a loopback host.

    The Host header must be one of `LOCAL_HOSTS`, bare or with the
    daemon's port (any port when `port` is None, as an app built for a
    test is); otherwise 421, as the SDK answers at ``/mcp``. An Origin
    header, when present, must be ``http://`` and one of the same, bare or
    with the same port; otherwise 403. No Origin is fine: the CLI, the
    hook client and the shim send none.

    The SDK checks Host and Origin only inside its ``/mcp`` transport, so
    before this ``/health``, the hook routes and the shutdown route took
    any Host. A page a browser loaded from an attacker's name, rebound to
    127.0.0.1 (DNS rebinding), could then read ``/health``, whose store
    path carries the user's name, on a browser that does not block
    requests from public pages to private addresses. A browser sends the
    name the page was loaded from as the Host, so a Host that is not a
    loopback name is such a request, and nothing past this sees it. Every
    Host header and every Origin header a request carries is checked, so
    a second header cannot carry what the first was refused for.

    Other scope types pass: lifespan to the app, a websocket to
    `BearerAuthMiddleware`, which refuses it."""

    def __init__(self, app: ASGIApp, *, port: int | None = None) -> None:
        self.app = app
        self._port = None if port is None else str(port)

    def _named_here(self, text: str) -> bool:
        split = _host_port(text)
        if split is None or split[0] not in LOCAL_HOSTS:
            return False
        port = split[1]
        if port is None:
            return True
        if self._port is not None:
            return port == self._port
        return port.isascii() and port.isdigit()

    def _origin_named_here(self, text: str) -> bool:
        scheme = "http://"
        return text.lower().startswith(scheme) and self._named_here(text[len(scheme) :])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hosts: list[str] = []
        origins: list[str] = []
        for name, value in scope.get("headers", []):
            lowered = name.lower()
            if lowered == b"host":
                hosts.append(value.decode("latin-1"))
            elif lowered == b"origin":
                origins.append(value.decode("latin-1"))
        refusal: Response | None = None
        if not hosts or not all(self._named_here(host) for host in hosts):
            refusal = PlainTextResponse("Invalid Host header", status_code=421)
        elif not all(self._origin_named_here(origin) for origin in origins):
            refusal = PlainTextResponse("Invalid Origin header", status_code=403)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """Pure ASGI: 413 on a request under `prefix` whose body passes
    `max_bytes`, before any route reads it.

    A Content-Length over the cap is refused before a byte is read; a body
    that comes without one, or with one that understates it, is read
    chunk by chunk and refused as the chunk that passes the cap arrives.
    A body under the cap is handed on whole, replayed to the route as one
    message, so the route reads exactly what was sent. The SDK applies the
    same discipline inside its ``/mcp`` transport only; the hook routes
    read their bodies with `Request.json`, which reads to the end, so a
    client holding the token could make the daemon hold any size in
    memory."""

    def __init__(self, app: ASGIApp, *, max_bytes: int, prefix: str) -> None:
        self.app = app
        self._max = max_bytes
        self._prefix = prefix

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = PlainTextResponse("Request body too large", status_code=413)
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(
            self._prefix
        ):
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                continue
            if declared > self._max:
                await self._too_large(scope, receive, send)
                return
        body = bytearray()
        received = complete = False
        trailing: Message | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # The client went away (``http.disconnect``): the route sees
                # what came and then the disconnect, as it would have.
                trailing = message
                break
            received = True
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self._max:
                await self._too_large(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                complete = True
                break
        replay: list[Message] = []
        if received:
            replay.append(
                {"type": "http.request", "body": bytes(body), "more_body": not complete}
            )
        if trailing is not None:
            replay.append(trailing)

        async def replayed() -> Message:
            if replay:
                return replay.pop(0)
            return await receive()

        await self.app(scope, replayed, send)


class BearerAuthMiddleware:
    """Pure ASGI: 401 on every HTTP request without the daemon's token,
    except the open paths.

    Fails closed on every other scope type but lifespan: a websocket is
    closed before it is accepted (policy violation, 1008), and a scope of
    a type this does not know never reaches the app. The daemon serves no
    websocket, and a check that passed what it did not understand would
    let the first route that ever accepted one skip the token."""

    def __init__(self, app: ASGIApp, *, token: str, open_paths: frozenset[str]) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")
        self._open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        if scope.get("path") in self._open_paths:
            await self.app(scope, receive, send)
            return
        supplied = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                supplied = value
                break
        if not secrets.compare_digest(supplied, self._expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


Handler = Callable[[Request], Awaitable[Response]]


def _route(mcp: Any, path: str, methods: list[str]) -> Callable[[Handler], Handler]:
    """`MCPServer.custom_route` with a type: the SDK leaves the decorator
    untyped, and the daemon's routes are plain Starlette handlers."""
    return cast(Callable[[Handler], Handler], mcp.custom_route(path, methods=methods))


def _version() -> str:
    from . import __version__

    return __version__


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _cwd_of(payload: dict[str, Any]) -> Path | None:
    cwd = payload.get("cwd")
    return Path(str(cwd)) if isinstance(cwd, str) and cwd else None


def _posted(payload: dict[str, Any]) -> dict[str, Any]:
    """What a hook client posted about where it stands (design 8.1): its
    ``location`` (a namespace probe, or the location token), and in later
    rounds of the witness exchange its ``machine`` and ``answers``
    (`location.resolve_hook`)."""
    return {
        name: payload[name]
        for name in ("location", "machine", "answers")
        if isinstance(payload.get(name), dict)
    }


def _text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


_T = TypeVar("_T")

#: How many hook workers the daemon runs: enough that a cold brief's git
#: work does not hold up another session's prompt.
HOOK_WORKERS = 4
#: The threads the MCP tool bodies run on (`_Workers`, the second pool):
#: a tool body's git calls and sqlite reads hold no event loop, so a hook
#: of another session is answered while a tool call waits on git.
TOOL_WORKERS = 4


#: How long a stopping daemon gives the requests in flight before uvicorn
#: cancels them: the longest bound a handler runs under, the git runner's
#: `location._GIT_TIMEOUT_SECONDS` (20 s), so a request that would have
#: finished on its own does, and one that would not is cut where its own
#: timeout would have cut it. `_daemon_client.SHUTDOWN_TIMEOUT_SECONDS`
#: waits this plus `WORKERS_CLOSE_SECONDS` before its signal fallback;
#: tests/test_daemon_lifecycle.py pins both relations.
GRACEFUL_SHUTDOWN_SECONDS = 20
#: How long the daemon's stop waits for the workers' current jobs, both
#: pools under the one deadline (`_Workers.join`), before it interrupts the
#: jobs still running (`_finish_pools`). The interrupt ends a statement
#: inside SQLite; it does not reach a job waiting for another writer's lock
#: (that wait ends at the pool's busy timeout) or one outside SQLite (a git
#: call ends at `location._GIT_TIMEOUT_SECONDS`), and the stop waits for
#: those however long they take, since the store's lock is never given back
#: while a worker's connection is open. A stop's own length is therefore
#: `GRACEFUL_SHUTDOWN_SECONDS` plus this plus the step a job is in that the
#: interrupt does not reach (`_daemon_client.UNINTERRUPTED_STEP_SECONDS`)
#: and what the job runs after it; the supervisor's kill
#: (`service_units.STOP_TIMEOUT_SECONDS`) is the hard bound, after which
#: the process, its connections and its lock are gone and the next start
#: takes the store over.
WORKERS_CLOSE_SECONDS = 10.0
#: How often `_finish_pools` interrupts the jobs still running once
#: `WORKERS_CLOSE_SECONDS` has passed, in seconds. SQLite's interrupt ends
#: only a statement running when it lands, so it is repeated until the
#: worker ends. Measured in a controlled run on this tree (scratch probe,
#: SQLite 3.50.4): at a 0.05 s cadence, a statement begun after one
#: interrupt ran 0.023 s before the next ended it; the cadence bounds that
#: wait, and an interrupt is one call that returns at once.
INTERRUPT_EVERY_SECONDS = 0.05


async def _awaited(value: Awaitable[_T]) -> _T:
    return await value


class _Workers:
    """The threads the hook bodies run on and, in a second pool, the tool
    bodies, each thread with its own connection.

    SQLite refuses a connection on any thread but the one that opened
    it, and a pool that retires idle threads (anyio's, after ten seconds)
    would reopen the store on most hooks. These threads live as long as
    the app: each opens its connection (`Store.reopen`, at the pool's busy
    timeout) on its first job, keeps it, and closes it itself when `stop`
    asks every worker to stop. The connections share the file, its WAL
    and its key; every write is its own ``BEGIN IMMEDIATE`` transaction,
    the discipline a CLI process writing beside the daemon already relies
    on. A connection found inside a transaction at the start of a job,
    which no call can own then (a rollback that failed), is closed, which
    rolls the transaction back and gives up its lock, and a fresh one
    opened (`_connection`), before the job's first read rather than at its
    first write (`Store._transaction`).

    A job runs in a copy of its caller's context (`contextvars`): what
    the request's task bound on the loop, the identity middleware's
    binding and the SDK's request state among it, is seen in the body,
    and what the body binds stays in that copy, so one call's binding
    reaches no other. A pool runs synchronous bodies (`run`) and, for
    the tools, handler coroutines (`run_coroutine`): the bodies await
    nothing real, so a worker runs one to completion on a private event
    loop of its own. One pool per kind, so a hook never queues behind a
    tool and a tool never behind a hook; the daemon's stop stops both,
    joins both under one deadline (`WORKERS_CLOSE_SECONDS`), then
    interrupts what still runs in either and waits for every worker to end
    (`_close_pools`), so no worker's connection is open once it returns."""

    def __init__(
        self,
        store: Store,
        *,
        count: int = HOOK_WORKERS,
        name: str = "hook",
        busy_timeout: float = HOOK_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._count = count
        self._name = name
        self._busy_timeout = busy_timeout
        self._jobs: queue.Queue[
            tuple[
                Callable[[Store], Any],
                contextvars.Context,
                concurrent.futures.Future[Any],
            ]
            | None
        ] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False
        # The connection of each worker while its job runs, by thread:
        # what `finish` interrupts. Set and cleared under `_lock`, so an
        # interrupt never reaches a connection whose job has ended.
        self._running: dict[threading.Thread, Store] = {}

    def _connection(self, held: Store | None) -> Store:
        """The connection the worker's next job runs on: the one it holds,
        unless that one is inside a transaction. Between jobs no call owns
        one, so it is a residue (a rollback that failed) that holds the
        write lock against every other writer; the connection is closed,
        which rolls it back, and a fresh one opened. The first residue in
        the process is logged (`store.note_residue`)."""
        if held is not None and (held.in_batch or held.conn.in_transaction):
            note_residue(
                f"a {self._name} worker's connection to {held.path}",
                "it was closed and a new one opened",
            )
            held.close()
            held = None
        if held is None:
            return self._store.reopen(busy_timeout=self._busy_timeout)
        return held

    def _loop(self) -> None:
        held: Store | None = None
        me = threading.current_thread()
        # The worker's own loop, made before any body runs, so a coroutine
        # a body returns is never left un-awaited for want of one.
        loop = asyncio.new_event_loop()
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                body, context, future = job
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    current, held = held, None
                    held = self._connection(current)
                    with self._lock:
                        self._running[me] = held
                    try:
                        value = context.run(body, held)
                        if inspect.isawaitable(value):
                            value = loop.run_until_complete(
                                loop.create_task(_awaited(value), context=context)
                            )
                    finally:
                        with self._lock:
                            self._running.pop(me, None)
                    future.set_result(value)
                except BaseException as exc:  # noqa: BLE001 - handed to the caller
                    future.set_exception(exc)
        finally:
            loop.close()
            if held is not None:
                held.close()

    def _start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"the daemon's {self._name} workers are stopped")
            if self._threads:
                return
            for index in range(self._count):
                thread = threading.Thread(
                    target=self._loop,
                    name=f"bettermemory-{self._name}-{index}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    async def run(self, body: Callable[[Store], _T]) -> _T:
        """Run `body` on a worker, with that worker's store, in a copy of
        the caller's context."""
        self._start()
        future: concurrent.futures.Future[_T] = concurrent.futures.Future()
        self._jobs.put((body, contextvars.copy_context(), future))
        return await asyncio.wrap_future(future)

    async def run_coroutine(
        self, make: Callable[[Store], Coroutine[Any, Any, _T]]
    ) -> _T:
        """Run the coroutine `make(worker_store)` returns on a worker, to
        completion on the worker's own loop (`_loop`)."""
        return cast(_T, await self.run(make))

    def stop(self) -> None:
        """Ask every worker to stop after the job it is running; each
        closes its own connection on the way out. Asking again does
        nothing more."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = list(self._threads)
        for _ in threads:
            self._jobs.put(None)

    def join(self, deadline: float) -> None:
        """Wait for the stopped workers until `deadline` (a
        `time.monotonic` reading), the whole pool's bound, not each
        worker's."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def finish(self) -> None:
        """`_finish_pools` for this pool alone."""
        _finish_pools((self,))

    def _living(self) -> list[threading.Thread]:
        """This pool's workers that have not ended."""
        with self._lock:
            return [thread for thread in self._threads if thread.is_alive()]

    def _interrupt(self, thread: threading.Thread) -> None:
        """Interrupt `thread`'s connection while its job runs, under the
        lock its job's end takes, so the connection is still open."""
        with self._lock:
            running = self._running.get(thread)
            if running is not None:
                with contextlib.suppress(sqlite3.ProgrammingError):
                    running.conn.interrupt()

    def close(self, timeout: float = WORKERS_CLOSE_SECONDS) -> None:
        """`stop`, `join` within `timeout`, then `finish`: one pool on its
        own. The daemon's stop does the same for its two pools under one
        deadline (`_close_pools`), so a stop waits `WORKERS_CLOSE_SECONDS`
        once."""
        self.stop()
        self.join(time.monotonic() + timeout)
        self.finish()


def _finish_pools(pools: Sequence[_Workers]) -> None:
    """Wait for every stopped worker of `pools` to end, however long it
    takes: when this returns, every worker has closed its connection,
    which the daemon needs before it gives the store's lock back. Each
    round interrupts every living worker of every pool from this thread
    (`sqlite3.Connection.interrupt`, made to be called from another), so a
    worker waiting for a lock another worker's statement holds is freed by
    that statement's end in the same round, then waits
    `INTERRUPT_EVERY_SECONDS` for them to end. A statement running when an
    interrupt lands fails at once and its job's transaction rolls back. A
    statement waiting for another writer's lock first runs out the pool's
    busy timeout, and work outside SQLite (a git call, under its own
    timeout) is not interrupted (`WORKERS_CLOSE_SECONDS`)."""
    told = False
    while True:
        living = [(pool, thread) for pool in pools for thread in pool._living()]
        if not living:
            return
        if not told:
            told = True
            log.warning(
                "%d of the daemon's workers still run a job after the stop's "
                "%.0f s; interrupting them",
                len(living),
                WORKERS_CLOSE_SECONDS,
            )
        for pool, thread in living:
            pool._interrupt(thread)
        living[0][1].join(INTERRUPT_EVERY_SECONDS)


def _close_pools(pools: Sequence[_Workers]) -> None:
    """Stop the daemon's worker pools and wait until every worker has
    closed its connection: all joined under one deadline
    (`WORKERS_CLOSE_SECONDS`), then whatever still runs in any of them
    interrupted and waited for (`_finish_pools`). Calling it again once
    the pools are closed returns at once."""
    for pool in pools:
        pool.stop()
    deadline = time.monotonic() + WORKERS_CLOSE_SECONDS
    for pool in pools:
        pool.join(deadline)
    _finish_pools(pools)


def _wait_for_workers(finish: Callable[[], None]) -> None:
    """Call `finish` (the app's `_finish_pools` over its pools) until it
    returns. A KeyboardInterrupt during its wait (a Ctrl-C repeated while
    the stop waits) is logged and the wait goes on: the store's lock is
    given back only once no worker's connection is open, and the
    supervisor's kill is what ends a daemon that will not wait."""
    while True:
        try:
            finish()
            return
        except KeyboardInterrupt:
            log.warning(
                "interrupted while the daemon's workers end; the store's lock "
                "is kept until they have"
            )


def _failed(surface: str, exc: Exception) -> Response:
    return JSONResponse(
        {
            "stdout": "",
            "surface": surface,
            "ids": [],
            "chars": 0,
            "rule": None,
            "event_id": None,
            "error": f"{exc.__class__.__name__}: {exc}",
        },
        status_code=500,
    )


def build_app(
    *,
    config: Config,
    store: Store,
    token: str,
    shutdown: Callable[[], None],
    version: str | None = None,
    port: int | None = None,
) -> Starlette:
    """The daemon's ASGI app: the MCP surface at ``/mcp``, the hook
    endpoints, ``/health`` and the shutdown endpoint, behind the bearer
    check. `shutdown` is called by ``POST /api/v1/admin/shutdown``.

    Three checks wrap every route, outermost first: the Host and Origin
    check (`LocalHostMiddleware`, against `port`, the port the daemon
    bound; `serve` always passes it, and an app built without it takes
    any port on a loopback name), the bearer check (`BearerAuthMiddleware`)
    and the body cap on the hook and admin routes (`BodyLimitMiddleware`).
    A request with a rebinding Host is refused before the token is read,
    and a body is read only once the token has been checked, so a client
    without it cannot make the daemon hold a body at all."""
    from . import capture_hook as _capture_hook
    from . import hook as _hook
    from . import recall as _recall

    served_version = version if version is not None else _version()
    # Two pools over the daemon's store: the hook bodies' and the tool
    # bodies' (design: a tool body waiting on git holds no hook up).
    workers = _Workers(store, busy_timeout=HOOK_BUSY_TIMEOUT_SECONDS)
    tool_workers = _Workers(
        store, count=TOOL_WORKERS, name="tool", busy_timeout=TOOL_BUSY_TIMEOUT_SECONDS
    )
    mcp = build_server(config=config, store=store, offload=tool_workers.run_coroutine)
    recall_state = _recall.RecallState()

    @_route(mcp, "/health", ["GET"])
    async def health(request: Request) -> Response:
        try:
            orphans = [str(path) for path in store.store_orphans()]
        except sqlite3.Error:
            orphans = []
        answer: dict[str, Any] = {
            "status": "ok",
            "version": served_version,
            "store": str(store.path),
            "pid": os.getpid(),
            # The saved stores this one was created fresh after
            # (`Store.store_orphans`), while any is still on disk.
            "store_orphans": orphans,
        }
        # The identity proof (`_daemon_client.identify`): the token's MAC
        # over the client's nonce, which only the process holding the
        # state file's token can answer. A request without a nonce, or
        # with one of another shape, gets the reply it always got, byte
        # for byte; the proof discloses nothing about the token.
        nonce = request.query_params.get(HEALTH_NONCE_PARAM)
        if valid_nonce(nonce):
            answer["proof"] = health_proof(token, nonce)
        return JSONResponse(answer)

    @_route(mcp, "/api/v1/hook/session-start", ["POST"])
    async def hook_session_start(request: Request) -> Response:
        payload = await _json_body(request)

        def body(worker_store: Store) -> _recall.HookResult:
            return _recall.session_brief(
                session_id=_text(payload, "session_id"),
                source=_text(payload, "source"),
                harness=_text(payload, "harness"),
                transcript_path=_text(payload, "transcript_path"),
                # The subagent the hook fires in (ENG-150): Claude Code's
                # `agent_id` and `agent_type`, as the client forwarded them.
                agent_id=_text(payload, "agent_id"),
                agent_type=_text(payload, "agent_type"),
                cwd=_cwd_of(payload),
                config=config,
                store=worker_store,
                state=recall_state,
                posted=_posted(payload),
            )

        try:
            result = await workers.run(body)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the hook
            log.warning(
                "hook session-start failed: %s: %s", exc.__class__.__name__, exc
            )
            return _failed("session_brief", exc)
        return JSONResponse(result.to_response())

    @_route(mcp, "/api/v1/hook/prompt", ["POST"])
    async def hook_prompt(request: Request) -> Response:
        payload = await _json_body(request)
        prompt = _text(payload, "prompt")
        session_id = payload.get("session_id")
        if prompt is None or not session_id:
            return JSONResponse(_recall.HookResult.empty("prompt_recall").to_response())

        def body(worker_store: Store) -> _recall.HookResult:
            return _recall.prompt_recall(
                prompt=prompt,
                session_id=str(session_id),
                harness=_text(payload, "harness"),
                prompt_id=_text(payload, "prompt_id"),
                transcript_path=_text(payload, "transcript_path"),
                cwd=_cwd_of(payload),
                config=config,
                store=worker_store,
                state=recall_state,
                posted=_posted(payload),
            )

        try:
            result = await workers.run(body)
        except Exception as exc:  # noqa: BLE001
            log.warning("hook prompt failed: %s: %s", exc.__class__.__name__, exc)
            return _failed("prompt_recall", exc)
        return JSONResponse(result.to_response())

    @_route(mcp, "/api/v1/hook/stop", ["POST"])
    async def hook_stop(request: Request) -> Response:
        payload = await _json_body(request)
        transcript = payload.get("transcript_path")
        session_id = payload.get("session_id")
        quiet_fields: dict[str, Any] = {
            "stdout": "",
            "result": None,
            "surface": "turn_audit",
            "ids": [],
            "chars": 0,
            "rule": None,
            "event_id": None,
        }
        if not transcript or not session_id:
            return JSONResponse(quiet_fields)
        audit_error: list[str] = []

        def body(
            worker_store: Store,
        ) -> tuple[dict[str, Any] | None, _capture_hook.StopStep | None]:
            # The capture half runs first: a Stop that settles a capture
            # ended a turn the model spent saving memories, not one the
            # user asked for, so the audit leaves it out of turn_audited,
            # search_miss and the helped rate. A capture failure is logged
            # and never costs the audit, and an audit failure never costs an
            # opened capture its instruction: the reply still carries it.
            step: _capture_hook.StopStep | None = None
            try:
                step = _capture_hook.on_stop(config, worker_store, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "capture at stop failed: %s: %s", exc.__class__.__name__, exc
                )
            if step is not None and step.action == _capture_hook.SETTLED:
                return None, step
            try:
                return _hook.audit_transcript(
                    transcript_path=Path(str(transcript)),
                    session_id=str(session_id),
                    config=config,
                    store=worker_store,
                    cwd=_cwd_of(payload),
                    dry_run=bool(payload.get("dry_run", False)),
                    assistant_message=_text(payload, "last_assistant_message"),
                    harness=_text(payload, "harness"),
                    prompt_id=_text(payload, "prompt_id"),
                    state=recall_state,
                    posted=_posted(payload),
                ), step
            except Exception as exc:  # noqa: BLE001
                if step is None or step.instruction is None:
                    raise
                log.warning(
                    "hook stop audit failed: %s: %s", exc.__class__.__name__, exc
                )
                audit_error.append(f"{exc.__class__.__name__}: {exc}")
                return None, step

        try:
            result, step = await workers.run(body)
        except Exception as exc:  # noqa: BLE001
            log.warning("hook stop failed: %s: %s", exc.__class__.__name__, exc)
            return JSONResponse(
                {**quiet_fields, "error": f"{exc.__class__.__name__}: {exc}"},
                status_code=500,
            )
        # Only a Stop capture acted on carries these, so a reply with
        # capture off or unattended is the one it always was. The client
        # prints `hook_output` even under --quiet, for Claude Code to read.
        if step is not None:
            quiet_fields["capture"] = step.to_dict()
            if step.instruction is not None:
                try:
                    quiet_fields["hook_output"] = _capture_hook.hook_output(
                        step.instruction
                    )
                except Exception as exc:  # noqa: BLE001
                    # The model never sees this batch's instruction: closed
                    # as failed, the next Stop does not read it as an
                    # interrupt.
                    error = f"{exc.__class__.__name__}: {exc}"
                    log.warning("capture reply failed: %s", error)
                    batch_id = step.batch
                    with contextlib.suppress(Exception):
                        await workers.run(
                            lambda s: _capture_hook.close_failed(s, batch_id)
                        )
                    return JSONResponse(
                        {**quiet_fields, "error": error}, status_code=500
                    )
        if audit_error:
            quiet_fields["error"] = audit_error[0]
        if result is None:
            return JSONResponse(quiet_fields)
        import json as _json

        stdout = (
            "" if payload.get("quiet", True) else _json.dumps(result, sort_keys=True)
        )
        return JSONResponse(
            {
                **quiet_fields,
                "stdout": stdout,
                "result": result,
                "chars": len(stdout),
                "rule": result.get("threshold_rule"),
            }
        )

    @_route(mcp, "/api/v1/admin/shutdown", ["POST"])
    async def admin_shutdown(request: Request) -> Response:
        shutdown()
        return JSONResponse({"status": "stopping"})

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=HOST,
    )
    # Starlette runs the middleware added last outermost.
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES, prefix=CAPPED_PREFIX
    )
    app.add_middleware(BearerAuthMiddleware, token=token, open_paths=OPEN_PATHS)
    app.add_middleware(LocalHostMiddleware, port=port)
    served_lifespan = app.router.lifespan_context

    def close_workers() -> None:
        # Both pools stopped, then joined under one deadline: the client's
        # wait, `_daemon_client.SHUTDOWN_TIMEOUT_SECONDS`, is the graceful
        # bound plus WORKERS_CLOSE_SECONDS once. Then what still runs is
        # interrupted and waited for: no worker's connection is open when
        # this returns.
        _close_pools((workers, tool_workers))

    def finish_workers() -> None:
        # The wait alone, for a close something cut short: the pools are
        # stopped again (a no-op when the close got that far), so the
        # workers end once their jobs do.
        for pool in (workers, tool_workers):
            pool.stop()
        _finish_pools((workers, tool_workers))

    @asynccontextmanager
    async def lifespan(served: Any) -> AsyncIterator[Any]:
        try:
            async with served_lifespan(served) as state:
                yield state
        finally:
            close_workers()

    app.router.lifespan_context = lifespan
    # `serve` closes the pools again on its way out, before it closes the
    # store and gives the lock back: a loop closed without cancelling the
    # app's tasks never ran the lifespan's close. `finish_workers` is the
    # wait alone, for a close something cut short.
    app.state.close_workers = close_workers
    app.state.finish_workers = finish_workers
    return app


#: How often, in seconds, the daemon checks that its store path still
#: names the file it opened (`_StoreWatch`). Read when `serve` starts;
#: tests lower it.
STORE_WATCH_SECONDS = 30.0


#: The files SQLite keeps beside a store in WAL mode, by suffix.
_SIBLINGS = ("-wal", "-shm")


class _StoreWatch:
    """Fires once the store path no longer names the file the daemon
    opened.

    At `start` the watch records the device and inode the path names,
    following a symlink as SQLite does, and every `interval` seconds
    stats the path again. The path gone, or naming another device or
    inode, means the store was removed or replaced by a rename over its
    path, and a daemon left running would serve a store no other process
    can open again, under a state file that may be gone with it. A copy
    over the path writes into the same inode, as SQLite's own VACUUM,
    checkpoint and backup do, so none of them counts. A stat that fails
    any other way is logged and the next tick checks again.

    The watch holds no descriptor on the store. Closing any descriptor on
    a file drops every POSIX advisory lock the process holds on it, the
    daemon's SQLite connections' among them, and a connection whose locks
    are gone lets another process checkpoint and delete the WAL under it.

    Not on Windows, where SQLite's own handle already refuses an unlink
    or a rename over the open store, so there is nothing to watch for."""

    def __init__(
        self, path: Path, on_removed: Callable[[], None], *, interval: float
    ) -> None:
        # Absolute as SQLite makes it at open, against the same working
        # directory; symlinks are left for `os.stat` to follow.
        self._path = Path(os.path.abspath(path))
        self._on_removed = on_removed
        self._interval = interval
        self._stop = threading.Event()
        #: The device and inode the path named at `start`.
        self.identity: tuple[int, int] | None = None
        #: The same for the path and its ``-wal`` and ``-shm`` siblings,
        #: by suffix, those there at `start`: the daemon's own files.
        self.files: dict[str, tuple[int, int]] = {}
        #: When the watch started: a store at the path created later
        #: cannot be the one the daemon opened (`save_orphan`).
        self.started_at: datetime | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Record the file the path names and start the thread; False
        when the platform has nothing to watch or the path cannot be
        stat'ed."""
        if sys.platform == "win32":
            return False
        try:
            found = os.stat(self._path)
        except OSError as exc:
            log.warning(
                "cannot watch the store %s: %s: %s",
                self._path,
                exc.__class__.__name__,
                exc,
            )
            return False
        self.identity = (found.st_dev, found.st_ino)
        self.files = {"": self.identity}
        for suffix in _SIBLINGS:
            with contextlib.suppress(OSError):
                sibling = os.stat(f"{self._path}{suffix}")
                self.files[suffix] = (sibling.st_dev, sibling.st_ino)
        self.started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(
            target=self._run, name="bettermemory-store-watch", daemon=True
        )
        self._thread.start()
        return True

    def moved(self) -> bool:
        """Whether the path is gone or names another file than the one
        recorded at `start`; False for a watch that never started, and
        for a stat that failed any other way, logged."""
        if self.identity is None:
            return False
        try:
            found = os.stat(self._path)
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError as exc:
            log.warning(
                "could not stat the store %s: %s: %s",
                self._path,
                exc.__class__.__name__,
                exc,
            )
            return False
        return (found.st_dev, found.st_ino) != self.identity

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if not self.moved():
                continue
            log.warning(
                "the store %s was removed or replaced; the daemon stops",
                self._path,
            )
            self._on_removed()
            return

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the thread. It waits on an event, so this never waits out
        an interval."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


def _utc_stamp(now: datetime | None = None) -> str:
    """``20261012T053900Z``: the stamp an orphan's file name carries."""
    moment = now if now is not None else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _orphan_path(store_path: Path, stamp: str) -> Path:
    """``<store>.orphaned-<stamp>.sqlite`` beside the store, with a counter
    when an orphan from the same second is already there."""
    base = f"{store_path.name}.orphaned-{stamp}"
    candidate = store_path.with_name(f"{base}.sqlite")
    counter = 2
    while candidate.exists():
        candidate = store_path.with_name(f"{base}-{counter}.sqlite")
        counter += 1
    return candidate


def save_orphan(
    store: Store,
    *,
    now: datetime | None = None,
    into: Path | None = None,
    successor_after: datetime | None = None,
    own_files: Mapping[str, tuple[int, int]] | None = None,
) -> Path | None:
    """Back up the store the daemon still holds open, after its path was
    removed or replaced, to ``<store>.orphaned-<UTC stamp>.sqlite`` beside
    the path, or over `into`, an orphan saved earlier; the orphan's path,
    or None when it could not be written.

    The copy is SQLite's online backup through `store`'s own connection,
    the only way left to the inode, so it must run on the thread that
    opened that connection. It is written under a temporary name in the
    same directory and renamed into place, so no other process sees a
    partial file, and the directory is synced after the rename. A
    directory removed with the store is made again, owner-only. Every
    failure (the path's parent a file, a full disk) is logged at error
    level, never raised: the daemon stops either way.

    With `successor_after`, a store another process created at the path
    after that moment is told about the orphan (`_tell_successor`)."""
    store_path = Path(store.path)
    directory = store_path.parent
    temp: str | None = None
    try:
        ensure_owner_only_dir(directory, parents=True)
        handle, temp = tempfile.mkstemp(
            prefix=f".{store_path.name}.orphaned-", suffix=".tmp", dir=directory
        )
        os.close(handle)
        target = sqlite3.connect(temp)
        try:
            store.conn.backup(target)
        finally:
            target.close()
        orphan = into if into is not None else _orphan_path(store_path, _utc_stamp(now))
        replace_atomic(Path(temp), orphan)
        temp = None
        fsync_dir(directory)
    except (OSError, sqlite3.Error) as exc:
        log.error(
            "the store %s was removed or replaced and its open copy could not "
            "be saved: %s: %s",
            store_path,
            exc.__class__.__name__,
            exc,
        )
        return None
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)
    if into is None:
        log.error(
            "the store %s was removed or replaced; the store this daemon had "
            "open is saved at %s",
            store_path,
            orphan,
        )
    if successor_after is not None:
        _tell_successor(
            store_path,
            orphan,
            store_id=store.store_id,
            created_after=successor_after,
            own_files=own_files or {},
        )
    return orphan


def _tell_successor(
    store_path: Path,
    orphan: Path,
    *,
    store_id: str,
    created_after: datetime,
    own_files: Mapping[str, tuple[int, int]],
) -> None:
    """Name `orphan` in the ``created_after_orphans`` meta of the store now
    at `store_path`, when another process created it there after
    `created_after`: a store created before the orphan existed did not
    see it beside itself (`Store.create`) and would never say it started
    empty.

    A store whose ``created`` is earlier, an older store moved back over
    the path, is left alone, as is any file whose ``store_id`` is the
    orphan's.

    Nothing is opened while the path or its ``-wal`` or ``-shm`` is one
    of the daemon's own files (`own_files`, by suffix), or a ``-wal`` or
    ``-shm`` is there whose owner is unknown: SQLite opens the WAL and
    shared memory beside a store with the store, and closing them would
    drop the daemon connection's locks on its own, the WAL it still
    writes. Only another store's files are opened, by a short connection
    of their own that waits up to five seconds on a writer in another
    process. Failures are logged, never raised."""
    for suffix in ("", *_SIBLINGS):
        try:
            found = os.stat(f"{store_path}{suffix}")
        except FileNotFoundError:
            if suffix == "":
                return
            continue
        except OSError:
            return
        own = own_files.get(suffix)
        if (suffix and own is None) or (found.st_dev, found.st_ino) == own:
            log.warning(
                "the saved store %s is not named in the store now at %s: "
                "its %s may still be this daemon's",
                orphan,
                store_path,
                f"{store_path.name}{suffix}",
            )
            return
    uri = f"{sqlite_file_uri(Path(os.path.abspath(store_path)))}?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    except sqlite3.Error as exc:
        log.warning(
            "could not open the store now at %s: %s: %s",
            store_path,
            exc.__class__.__name__,
            exc,
        )
        return
    told = False
    try:
        # Admitted first, as every opener admits a store file it did not
        # write on its own connection (`store_schema.admit_store`): this
        # runs on the event loop, and a `meta` that is an endless view
        # would hold it, and every request, for good.
        admit_store(conn, store_path)
        # The write lock is taken first, so the busy timeout covers
        # waiting on a writer in another process: a read upgraded to a
        # write fails at once on a busy store, its busy handler never
        # called. The lock writes nothing; the reads below still come
        # before any write, so a file that is no database at all fails
        # them and is never stamped with a header.
        conn.execute("BEGIN IMMEDIATE")
        meta = dict(
            conn.execute(
                "SELECT key, value FROM meta WHERE key IN ('store_id', 'created', ?)",
                (CREATED_AFTER_ORPHANS,),
            ).fetchall()
        )
        created = parse_event_ts(meta.get("created"))
        names = orphan_names(meta.get(CREATED_AFTER_ORPHANS))
        if (
            meta.get("store_id") in (None, store_id)
            or created is None
            or created <= created_after
            or orphan.name in names
        ):
            conn.execute("ROLLBACK")
            return
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (
                CREATED_AFTER_ORPHANS,
                json.dumps(sorted([*names, orphan.name], key=orphan_order)),
            ),
        )
        conn.execute("COMMIT")
        told = True
    except (sqlite3.Error, StoreRefusedError) as exc:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()
        log.warning(
            "could not name the saved store %s in the store now at %s: %s: %s",
            orphan,
            store_path,
            exc.__class__.__name__,
            exc,
        )
    finally:
        conn.close()
    if told:
        log.warning(
            "the store now at %s was created after the one this daemon had "
            "open; it now names the saved store %s",
            store_path,
            orphan,
        )


def bind(host: str, port: int) -> socket.socket:
    """A bound, non-listening socket on `host`: `port` when it is free,
    else an ephemeral one. The caller reads the port it got.

    On POSIX, SO_REUSEADDR only lets the daemon rebind a port whose last
    connections sit in TIME_WAIT. On Windows it also lets a socket bind a
    port another socket holds, so a daemon started on the stale port of a
    state file shared it with whatever held it (and a client reached that
    listener instead), and another process could bind the daemon's own
    port beside it. SO_EXCLUSIVEADDRUSE, where the platform has it, refuses
    both: a held port fails to bind, and the daemon takes an ephemeral one
    and records it in its state file."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        if port == 0:
            sock.close()
            raise
        sock.bind((host, 0))
    sock.setblocking(False)
    return sock


def serve(
    *,
    config: Config,
    store: Store,
    state_dir: Path,
    port: int | None = None,
    host: str = HOST,
) -> int:
    """Run the daemon in this process until the shutdown endpoint, a
    signal, or the removal of its store file (`_StoreWatch`) stops it;
    on a removal it first saves the store it had open beside the path
    (`save_orphan`), and saves it again over that file once nothing
    else writes to it. A removal the watch had no tick to see before
    any other stop is caught as the app shuts down and saved the same
    way; a forced exit (a second signal) saves as the loop closes, or
    after the server returns when the loop left the app's tasks
    running. Writes the state file after the socket is bound; on the
    way out, on every stop but a kill, it closes `store`, removes the
    state file and then gives the lock back, in that order, so a free
    lock, which is what `_daemon_client.shutdown_daemon` waits for,
    means the store's files are released. A SIGTERM is such a stop (the
    daemon's own handler asks the server to exit). A forced exit (a
    second SIGINT) skips the app's shutdown event, but the loop's close
    cancels the lifespan task and the app's contexts unwind on that
    cancellation, so the workers are closed there as on any stop: joined
    under `WORKERS_CLOSE_SECONDS`, then a job still running interrupted
    and waited for (`_finish_pools`). The lock is never given back while
    a worker's connection is open: the workers are closed again (a no-op
    once done) before the store is, for a loop that closed without
    cancelling the app's tasks, and a close something cut short (a Ctrl-C
    repeated during its wait) is finished then (`_wait_for_workers`). A
    stop therefore lasts past its own bounds while a job is in a step the
    interrupt does not reach (`WORKERS_CLOSE_SECONDS`); the supervisor's
    kill is the hard bound. On Python 3.12 and later uvicorn's own
    stop first waits for the requests in flight under the same graceful
    bound, since `asyncio.Server.wait_closed` waits for their connections
    there, where 3.11's returned at once. A stop gives the requests in
    flight `GRACEFUL_SHUTDOWN_SECONDS`. Returns the exit status.

    One daemon per store: before anything else it takes the store's lock
    (`_daemon_client.lock_file_for`, beside the state file) and holds it
    until it returns, so the OS gives it back however the process ends.
    When another daemon holds it past `LOCK_WAIT_SECONDS` (two clients
    that each found no daemon and each started one), this one logs a line
    and returns `DAEMON_HELD_STATUS` without binding, serving or writing
    the state file, which stays the serving daemon's."""
    lock_path = lock_file_for(state_dir, store.path)
    lock = acquire_lock(lock_path, wait=LOCK_WAIT_SECONDS)
    if lock is None:
        log.error(
            "another bettermemory daemon already serves %s (it holds %s); "
            "this one exits",
            store.path,
            lock_path,
        )
        return DAEMON_HELD_STATUS
    try:
        return _serve_locked(
            config=config, store=store, state_dir=state_dir, port=port, host=host
        )
    finally:
        release_lock(lock)


def _migrate_at_start(store: Store, config: Config) -> None:
    """The migration's run at the daemon's open (design 8.6), under the
    store's lock and before the port is bound, so it runs once per start
    and before any request: when a record has no home (the first start of
    an upgraded store, or a re-upgrade after a rollback, design 8.12), every
    record is homed and one line logs the run's totals. Without a machine
    id (`location.process_machine` raises OSError) one warning is logged,
    the store is served as it is, and the next start tries again."""
    from . import location
    from .membership_migrate import ensure_migrated, settle_at_open, unhomed

    if not store.has_key:
        return
    try:
        identity = location.process_machine()
    except OSError as exc:
        log.warning(
            "membership migration not run for %s: no machine id (%s); the "
            "next start tries again",
            store.path,
            exc,
        )
        return
    settled = settle_at_open(store, identity)
    if settled:
        log.info(
            "membership: %d legacy questions closed as separate (nothing to "
            "decide): %s",
            len(settled),
            ", ".join(settled),
        )
    if not unhomed(store):
        return
    report = ensure_migrated(store, identity, config=config)
    if report is None:
        return
    totals = report.totals
    log.info(
        "membership migrated: %d records homed (%d memories, %d removed "
        "memories, %d episodes), %d projects, %d checkouts, %d legacy "
        "questions, %d log rows",
        report.homed,
        totals.memory_homes,
        totals.tombstone_homes,
        totals.episode_homes,
        totals.projects,
        totals.checkouts,
        totals.proposals,
        totals.log_rows,
    )


def _serve_locked(
    *,
    config: Config,
    store: Store,
    state_dir: Path,
    port: int | None,
    host: str,
) -> int:
    """`serve`, with the store's lock held."""
    import uvicorn

    _migrate_at_start(store, config)
    requested = config.daemon.port if port is None else port
    sock = bind(host, requested)
    bound_port = int(sock.getsockname()[1])
    token = secrets.token_hex(32)
    holder: dict[str, Any] = {}

    def shutdown() -> None:
        server = holder.get("server")
        if server is not None:
            server.should_exit = True

    def save(into: Path | None = None) -> Path | None:
        # On the event loop, the thread that opened `store` and the only
        # one its connection serves.
        return save_orphan(
            store,
            into=into,
            successor_after=watch.started_at,
            own_files=watch.files,
        )

    def orphaned() -> None:
        # The backup runs between two requests and before the port
        # closes, so a daemon started next for the path already finds the
        # orphan beside it. Once the app is shutting down, `settle` has
        # this covered.
        if holder.get("settled"):
            return
        holder["fired"] = True
        try:
            holder["orphan"] = save()
        except Exception:
            # The watch fires once: a daemon left serving here would
            # serve the lost store for good.
            log.exception("saving the removed store %s failed", store.path)
        finally:
            shutdown()

    def settle() -> None:
        # At the app's shutdown, on the event loop: the requests are
        # done, the hook workers have closed their connections and
        # `store`'s is still open. Writes that reached the orphaned inode
        # after the first save (a request in flight, a hook job) are
        # saved again over the same file; a removal no tick saw, before
        # `down` or a signal, is saved now.
        holder["settled"] = True
        orphan = holder.get("orphan")
        if orphan is not None:
            save(into=orphan)
        elif watch.moved():
            if not holder.get("fired"):
                log.warning(
                    "the store %s was removed or replaced; the daemon was "
                    "already stopping",
                    store.path,
                )
            holder["orphan"] = save()

    def store_gone() -> None:
        # From the watch thread, which starts after the loop is recorded.
        try:
            holder["loop"].call_soon_threadsafe(orphaned)
        except RuntimeError:
            # The loop is closed: `settle` has run.
            pass

    watch = _StoreWatch(Path(store.path), store_gone, interval=STORE_WATCH_SECONDS)
    app = build_app(
        config=config, store=store, token=token, shutdown=shutdown, port=bound_port
    )
    built_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(served: Any) -> AsyncIterator[Any]:
        # The watch runs while the loop does, so it always has one to
        # hand the backup to.
        holder["loop"] = asyncio.get_running_loop()
        watch.start()
        try:
            async with built_lifespan(served) as started:
                yield started
        finally:
            watch.stop()
            settle()

    app.router.lifespan_context = lifespan
    state = {
        "pid": os.getpid(),
        "port": bound_port,
        "token": token,
        "version": _version(),
        "store": str(store.path),
        "started": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        # This daemon answers ``/health?nonce=`` with a proof. A client of
        # 9.0.0rc14 on takes no reply without one for any daemon
        # (`_daemon_client.proves`) and reads no key here; the key tells
        # an rc12 or rc13 client, installed beside this one, to do the
        # same at this port, whatever pid and store a reply repeats.
        PROOF_STATE_KEY: 1,
    }
    write_state(state_dir, state)
    log.info(
        "bettermemory daemon %s on %s:%d, store %s",
        state["version"],
        host,
        bound_port,
        store.path,
    )
    uv_config = uvicorn.Config(
        app,
        host=host,
        port=bound_port,
        log_level="warning",
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(uv_config)
    holder["server"] = server
    if threading.current_thread() is threading.main_thread():
        # uvicorn captures SIGTERM while it serves and, once serving ends,
        # re-raises it against the handler it found; the default handler
        # would end the process there, before the ordered exit below. This
        # one asks the server to exit, so a SIGTERM (a supervisor's stop,
        # `_daemon_client.shutdown_daemon`'s fallback) stops the daemon the
        # way the shutdown endpoint does, and the re-raised signal is a
        # no-op. uvicorn installs its handlers on the main thread only,
        # and so does this.
        signal.signal(signal.SIGTERM, lambda signum, frame: shutdown())
    try:
        server.run(sockets=[sock])
    finally:
        # Every worker's connection closed before anything below: the
        # lifespan's close has done it when the app shut down or its task
        # was cancelled, and returns at once here; a loop closed without
        # cancelling the app's tasks never ran it. A close cut short (a
        # Ctrl-C repeated during its wait) is logged, the watch and the
        # settle below still run, and the wait is finished before the
        # store is closed and the lock given back.
        try:
            app.state.close_workers()
        except BaseException:
            log.exception("closing the daemon's workers was cut short")
        try:
            watch.stop()
            if not holder.get("settled"):
                # A loop closed without cancelling the app's tasks never
                # ran its shutdown, `settle` with it. This is the thread
                # the loop ran on, and `store`'s connection is still open.
                try:
                    settle()
                except Exception:
                    log.exception("saving the removed store %s failed", store.path)
        finally:
            try:
                # `down` waits for the store's lock, which `serve` gives
                # back once this returns: every worker has ended and the
                # store is closed here first, so a free lock means no
                # handle of this daemon is open on the store's files
                # (`_daemon_client._stopped`).
                _wait_for_workers(app.state.finish_workers)
                store.close()
            except Exception:
                log.exception("closing the store %s failed", store.path)
            finally:
                remove_state(state_dir, store.path, pid=os.getpid())
                sock.close()
    return 0


__all__ = [
    "BearerAuthMiddleware",
    "BodyLimitMiddleware",
    "CAPPED_PREFIX",
    "LOCAL_HOSTS",
    "LocalHostMiddleware",
    "MAX_BODY_BYTES",
    "OPEN_PATHS",
    "STORE_WATCH_SECONDS",
    "bind",
    "build_app",
    "save_orphan",
    "serve",
]
