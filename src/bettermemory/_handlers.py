"""ToolHandlers facade: the dependency bundle and one method per served tool.

Every public tool method re-lists the kwargs the SDK will introspect,
because `mcp.tool(...)` builds the JSON schema from
`inspect.signature(method)`, and a `**kwargs` delegate would land in the
client manifest as a typeless catch-all. The body underneath forwards to
the per-tool module function under `handlers/` through `_run`: on a
worker of the daemon's tool pool, inline everywhere else.

Why this file carries names like `_already_recorded_pending_ids` and
`capture_origin`: the test suite monkey-patches both. Keeping them as
module-level bindings here, re-exported from their homes, preserves the
patch surface; the per-tool modules route their `capture_origin` calls
through `from .. import _handlers as _h; _h.capture_origin(...)` so the
patch propagates (see `handlers/_shared.py`).
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, ParamSpec, TypeVar, cast

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from pydantic.fields import FieldInfo

from . import handlers as _handlers_pkg
from ._response import ResponseBuilder
from .config import Config
from .events import Recorder
from .handlers.admin import AdminAction
from .handlers.episode import EpisodeAction
from .handlers._shared import (
    Context,
    _advance_turn,
    _already_recorded_pending_ids,
    _attach_use_tokens,
    _event_ts_epoch,
    _hook_attributed_pending_ids,
    _validate_content_size,
    _validate_write_payload,
)
from .origin import capture as capture_origin
from .session import SessionSource
from .store import (
    STORE_FULL,
    DefaultStoreSource,
    MemoryNotFoundError,
    NotFoundError,
    NotTombstonedError,
    Store,
    StoreSource,
    TombstonedError,
    retryable_code,
)

log = logging.getLogger("bettermemory._handlers")


# The description constants `builder._register_tools` reaches for, one
# per served tool, re-exported so the wiring layer has one import path.
DESC_EPISODE = _handlers_pkg.DESC_EPISODE
DESC_MEMORY_ADMIN = _handlers_pkg.DESC_MEMORY_ADMIN
DESC_MEMORY_RECORD_USE = _handlers_pkg.DESC_MEMORY_RECORD_USE
DESC_MEMORY_REMOVE = _handlers_pkg.DESC_MEMORY_REMOVE
DESC_MEMORY_SEARCH = _handlers_pkg.DESC_MEMORY_SEARCH
DESC_MEMORY_SHOW = _handlers_pkg.DESC_MEMORY_SHOW
DESC_MEMORY_UPDATE = _handlers_pkg.DESC_MEMORY_UPDATE
DESC_MEMORY_VERIFY = _handlers_pkg.DESC_MEMORY_VERIFY
DESC_MEMORY_WRITE = _handlers_pkg.DESC_MEMORY_WRITE


# ---------------------------------------------------------------------------
# FTS candidate prefilter
# ---------------------------------------------------------------------------
#
# Module-level rather than `ToolHandlers` methods: the out-of-process Stop
# hook (`hook.run_audit`) has only a `Store` — no dependency bundle — and
# has to build the SAME candidate pool `memory_search` ranks, or the
# silent-miss probe measures a retrieval production never performed. The
# one consumer, `handlers.search.resolve_search_pool`, takes that `Store`
# as its first argument and calls `load_search_candidates` with it, so
# nothing here is reached through the dependency bundle.


# The store size above which the FTS5 candidate pre-filter is
# used instead of a full load_all on every search. Calibrated so
# that small stores (the common case) keep the existing behaviour
# byte-stable — the candidate path adds a SQLite round-trip per
# search and a per-id load, which is net cheaper only once the
# alternative (load every file) dominates the budget. Tunable
# via the BETTERMEMORY_INDEX_THRESHOLD env var for testing.
_INDEX_THRESHOLD_DEFAULT = 500

# Candidate cap threaded into `index.query` by
# `load_search_candidates`. A full cap-sized row set from the
# index means the FTS prefilter was saturated — the loader reports
# that via its second return value so `handlers/search.py` can run
# its cap-starvation guard.
_PREFILTER_CAP = 50


def resolve_index_threshold() -> int:
    """Resolve the live threshold above which the FTS candidate
    pre-filter kicks in. Reads from BETTERMEMORY_INDEX_THRESHOLD
    on every search so tests can flip it without rebuilding the
    handler. Falls back to the module default."""
    import os

    raw = os.environ.get("BETTERMEMORY_INDEX_THRESHOLD")
    if raw is None:
        return _INDEX_THRESHOLD_DEFAULT
    try:
        value = int(raw)
        return value if value > 0 else _INDEX_THRESHOLD_DEFAULT
    except ValueError:
        return _INDEX_THRESHOLD_DEFAULT


def load_search_candidates(
    store: Store,
    query: str,
    scopes: list[str] | None = None,
    *,
    client: str | None = None,
    model: str | None = None,
) -> tuple[list[Any], bool, bool]:
    """Either load every active memory or pre-filter through the store's
    FTS table, depending on the store's size.

    Returns ``(candidates, prefilter_saturated, prefiltered)``.
    ``prefiltered`` says the FTS path served the candidates at all, which
    is what collapses pool-derived corpus statistics and makes the
    ranking read document frequencies off the store instead;
    ``prefilter_saturated`` says it served a full cap-sized slice, which
    is what the cap-starvation guard in `handlers/search.py` keys on.
    Every full-load branch reports False for both.

    `scopes`, `client` and `model` thread into the candidate query so the
    bounded slice is drawn from rows that pass the filters the ranker
    applies anyway: each is the same exact match in SQL as in Python, so
    the query never drops a row the ranker would have kept.

    Below the threshold, or on an empty query, or when the query matches
    nothing, the whole active set is loaded: small stores keep the
    behaviour they had, and a query the FTS table cannot answer is
    ranked over everything rather than over nothing.
    """
    if not query.strip():
        return store.load_all(), False, False
    if store.count_memories() < resolve_index_threshold():
        return store.load_all(), False, False
    candidate_pairs = store.query_candidates(
        query, scopes=scopes, client=client, model=model, max_results=_PREFILTER_CAP
    )
    if not candidate_pairs:
        return store.load_all(), False, False
    loaded: list[Any] = store.load_many([cid for cid, _ in candidate_pairs])
    if not loaded:
        return store.load_all(), False, False
    return loaded, len(candidate_pairs) == _PREFILTER_CAP, True


class RefusedError(ToolError, ValueError):
    """A refusal raised at the MCP boundary with the words the model is
    meant to read.

    The handlers refuse with ``ValueError`` (an invalid scope, a cap, a
    missing parameter, an unknown or tombstoned id), and docs/api.md names
    ``ValueError`` as a refusal's form at the MCP boundary. mcp 2.0.0
    forwarded the text of every exception a tool raised; from 2.1 the SDK
    treats anything but its own ``ToolError`` as a crash and sends the
    client only ``Error executing tool <name>``, keeping the text on the
    server. A refusal that derives from both keeps its words on the wire
    under every SDK version the declared bound admits, a direct caller
    that catches ``ValueError`` still catches it, and a genuine crash
    stays masked as the SDK intends.
    """


def _in_the_way(code: str) -> str:
    """What a store code says stood in a call's way."""
    if code == STORE_FULL:
        return "the disk or the database is full, so the store could not take a write"
    return (
        "another process or worker held the store's write lock longer than "
        "this call waits for it"
    )


class RetryableRefusal(RefusedError):
    """A refusal the same call can succeed after: the store was in the way,
    not the call (`store.retryable_code`), and the call had kept no write
    (`_kept_or_retryable`). Another process or worker held the store's
    write lock past this call's busy timeout (``store_busy``), or the disk
    or the database is full (``store_full``). The text carries the code
    followed by ``(retryable)``; on the wire the SDK puts ``Error executing
    tool <name>: `` before it, so a client looks for the code in the text
    rather than at its start. `code` and `retryable` carry the same for a
    direct caller."""

    retryable = True

    def __init__(self, code: str, detail: str) -> None:
        then = (
            "Free disk space, then retry the call."
            if code == STORE_FULL
            else "Retry the call."
        )
        super().__init__(
            f"{code} (retryable): {_in_the_way(code)} ({detail}). The write "
            f"this call was making was not kept. {then}"
        )
        self.code = code


class KeptWriteRefusal(RefusedError):
    """A store that was busy, locked or full, met by a call after it had
    already committed a write (`_kept_or_retryable`): a later step of the
    call (a filing, a prune, a second record) could not run, and what the
    call committed stays. Not retryable: the same call made again would
    make its write again. The text carries the code followed by ``(not
    retryable)``, found in the text as `RetryableRefusal`'s is."""

    retryable = False

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(
            f"{code} (not retryable): this call had committed its write to "
            f"the store when a later step of it found that "
            f"{_in_the_way(code)} ({detail}). What it committed stays. Do not "
            f"retry the call, which would make the write again; read the "
            f"store to see what it holds."
        )
        self.code = code


def store_refusal(exc: BaseException) -> RetryableRefusal | None:
    """The retryable refusal a store error is answered with at the tool
    boundary, or None for an error that is not the store being busy,
    locked or full (`store.retryable_code`). For an error met outside a
    tool body (`ToolHandlers._run`), where no write was made."""
    code = retryable_code(exc)
    if code is None:
        return None
    return RetryableRefusal(code, str(exc))


#: The store's answers that the record a call names is not there as the
#: call needs it: removed, never written, or active where a removed one was
#: meant. A call that read the record and lost a race to another writer
#: (an update or a verify racing a removal) gets one from the store's own
#: transaction; each is a refusal with its words, not a crash.
_ABSENT = (MemoryNotFoundError, TombstonedError, NotTombstonedError, NotFoundError)


def _absent_text(exc: KeyError) -> str:
    """The words of one of `_ABSENT`: its message, not the quoted repr a
    KeyError prints; a bare key (NotFoundError) is named as one."""
    said = exc.args[0] if len(exc.args) == 1 else exc.args
    if isinstance(exc, NotFoundError):
        return f"no record {said} in the store"
    return str(said)


_P = ParamSpec("_P")
_R = TypeVar("_R")


#: Where a server runs its tool bodies when it has a worker pool: given a
#: function of a worker's store that returns the body's coroutine, it runs
#: that coroutine on a worker and answers with its result
#: (`daemon._Workers.run_coroutine`). None means every body runs inline.
Offload = Callable[[Callable[[Store], Coroutine[Any, Any, Any]]], Awaitable[Any]]


async def _kept_or_retryable(
    make: Callable[[ToolHandlers], Coroutine[Any, Any, _R]], deps: ToolHandlers
) -> _R:
    """Run the tool body `make` builds over `deps`, answering a store that
    was busy, locked or full by what the body had kept. The body runs to
    its end on `deps.store`'s connection with nothing else on it (a
    worker's job, or inline, where the bodies await nothing that yields),
    so the record commits that connection counts (`Store.commits`) moved
    by this call's alone. Unmoved, nothing the call wrote was kept and the
    same call can succeed (`RetryableRefusal`); moved, its write was kept
    and a later step met the store (`KeptWriteRefusal`), which a retry
    would turn into a second write."""
    before = deps.store.commits
    try:
        return await make(deps)
    except sqlite3.OperationalError as exc:
        code = retryable_code(exc)
        if code is None:
            raise
        if deps.store.commits == before:
            raise RetryableRefusal(code, str(exc)) from exc
        raise KeptWriteRefusal(code, str(exc)) from exc


def _refusals_as_tool_errors(
    fn: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Re-raise a tool method's ``ValueError`` as a ``RefusedError`` with
    the same text, and the store's answer that the record is not there as
    the call needs it (`_ABSENT`) as one with its words, where the SDK
    would otherwise send the client only ``Error executing tool <name>``.
    A store that was busy, locked or full is answered inside the body's
    run (`_kept_or_retryable`), by what the call kept; one met outside it
    is a ``RetryableRefusal``. ``functools.wraps`` keeps the signature the
    SDK introspects for the served schema, and ``str(exc)`` is the text
    the SDK forwarded before 2.1, so the wire form does not change."""

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except ValueError as exc:
            raise RefusedError(str(exc)) from exc
        except _ABSENT as exc:
            raise RefusedError(_absent_text(exc)) from exc
        except sqlite3.OperationalError as exc:
            refusal = store_refusal(exc)
            if refusal is not None:
                raise refusal from exc
            if _read_only(exc):
                raise RefusedError(
                    "the store is open for reading only, so this call wrote "
                    f"nothing ({exc}): a later bettermemory raised the schema "
                    "revision a writer needs (meta's min_writer_revision), or "
                    "the store's file cannot be written; upgrade bettermemory, "
                    "or make the file writable"
                ) from exc
            raise

    return wrapper


def _read_only(exc: sqlite3.OperationalError) -> bool:
    """Whether `exc` is SQLite's refusal to write a database it opened for
    reading only (``SQLITE_READONLY`` and its extended codes)."""
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_READONLY


class ToolHandlers:
    """One instance per server, captures the dependencies every handler
    needs.

    The methods below all delegate to the corresponding ``handlers.*``
    module function, threading ``self`` as the dependency bundle. This
    keeps the wire surface byte-identical to the pre-Round-2 shape:
    The SDK introspects each method's signature and the JSON schema
    drops ``self``, so the call site that the model sees is unchanged.
    """

    def __init__(
        self,
        *,
        config: Config,
        # The protocol, not the concrete `Store`: this is the seam the
        # per-request resolution below is built on. `Store` satisfies it
        # structurally, and `builder.build_server` is the assignment that
        # makes mypy check that claim instead of leaving it prose.
        store: Store,
        sessions: SessionSource,
        recorder: Recorder,
        responses: ResponseBuilder,
        store_source: StoreSource | None = None,
        offload: Offload | None = None,
    ) -> None:
        self.config = config
        self.store = store
        # The tool bodies' worker pool, when the server has one (the
        # daemon); None runs every body on the caller's loop (the stdio
        # server, the in-process servers the tests build). `_run`.
        self.offload = offload
        self.sessions = sessions
        self.recorder = recorder
        self.responses = responses
        # The per-request store seam. Defaulting to a source that
        # returns `store` for every request is what keeps the whole
        # existing suite and every single-store caller on exactly the
        # path they were on: `for_request` then returns `self`, so
        # nothing is copied and no handler observes a different object.
        self.store_source: StoreSource = (
            store_source if store_source is not None else DefaultStoreSource(store)
        )

    # ---- per-request store resolution ------------------------------------

    def for_request(self, ctx: Context | None = None) -> ToolHandlers:
        """The bundle this request should run against: `self` when the
        resolved store is this bundle's store, which is every request the
        shipped source resolves; a copy bound to the resolved store
        otherwise, so a store source that resolves a different store per
        principal needs no change here. The recorder is process-wide on
        purpose: a per-tenant telemetry log is a policy decision this
        seam does not make."""
        resolved = self.store_source.for_request(ctx)
        if resolved is self.store:
            return self
        clone = copy.copy(self)
        clone.store = resolved
        # A worker's connection is a reopen of the process store, not of
        # the store this source resolved: the clone runs its body inline.
        clone.offload = None
        return clone

    def with_store(self, store: Store) -> ToolHandlers:
        """This bundle bound to `store`, a worker's connection to the same
        file: the store and the recorder, which writes through it; the
        sessions, the config and the response builder are shared."""
        clone = copy.copy(self)
        clone.store = store
        clone.recorder = dataclasses.replace(self.recorder, store=store)
        return clone

    async def _run(self, make: Callable[[ToolHandlers], Coroutine[Any, Any, _R]]) -> _R:
        """Run the tool body `make` builds over this bundle: on a worker
        of the server's pool, bound to that worker's connection, so the
        body's git calls and sqlite reads hold no event loop and a hook of
        another session is answered meanwhile; inline, on the caller's
        loop, when the server has no pool or this bundle is a per-request
        clone bound to another store. An exception travels back as it
        would from the inline call; a store that was busy, locked or full
        is answered by what the body kept (`_kept_or_retryable`)."""
        if self.offload is None:
            return await _kept_or_retryable(make, self)
        return cast(
            _R,
            await self.offload(
                lambda store: _kept_or_retryable(make, self.with_store(store))
            ),
        )

    # ---- delegations to per-tool modules --------------------------------
    #
    # Each method threads `self` (the dependency bundle) into the
    # corresponding `handlers.*` function. The signature mirrors the
    # function's minus its leading `deps` argument, so the SDK's
    # `inspect.signature` introspection produces the served schema.

    @_refusals_as_tool_errors
    async def memory_search(
        self,
        query: str,
        scopes: list[str] | None = None,
        exclude_scopes: list[str] | None = None,
        max_results: int | None = None,
        expand_top: bool = False,
        auto_scope: bool = True,
        since_prior_session: bool = False,
        mode: str | None = None,
        client: str | None = None,
        model: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        deps = self.for_request(ctx)
        return await deps._run(
            lambda d: _handlers_pkg.memory_search(
                d,
                query,
                scopes=scopes,
                exclude_scopes=exclude_scopes,
                max_results=max_results,
                expand_top=expand_top,
                auto_scope=auto_scope,
                since_prior_session=since_prior_session,
                mode=mode,
                client=client,
                model=model,
                ctx=ctx,
            )
        )

    @_refusals_as_tool_errors
    async def memory_show(self, id: str, ctx: Context | None = None) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await deps._run(lambda d: _handlers_pkg.memory_show(d, id, ctx=ctx))

    @_refusals_as_tool_errors
    async def memory_write(
        self,
        content: str,
        scopes: list[str],
        confidence: str = "medium",
        source: str = "explicit-statement",
        force: bool = False,
        acknowledge_transient: bool = False,
        acknowledge_scope_mismatch: bool = False,
        acknowledge_credential: bool = False,
        acknowledge_user_claim: bool = False,
        category: str = "fact",
        claims: list[str] | None = None,
        supersedes: list[str] | None = None,
        # The write review's answers. An object defaulting to an empty
        # dict, so it is served with no null branch; the answer format is
        # in the review response's hint, not here.
        review: dict[str, Any] = Field(default_factory=dict),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        # A direct Python call that omits `review` receives the FieldInfo
        # default itself; the SDK's validation fills in the empty dict.
        if isinstance(review, FieldInfo):
            review = {}
        return await deps._run(
            lambda d: _handlers_pkg.memory_write(
                d,
                content,
                scopes,
                confidence=confidence,
                source=source,
                force=force,
                acknowledge_transient=acknowledge_transient,
                acknowledge_scope_mismatch=acknowledge_scope_mismatch,
                acknowledge_credential=acknowledge_credential,
                acknowledge_user_claim=acknowledge_user_claim,
                category=category,
                claims=claims,
                supersedes=supersedes,
                review=review,
                ctx=ctx,
            )
        )

    @_refusals_as_tool_errors
    async def memory_update(
        self,
        id: str,
        content: str | None = None,
        scopes: list[str] | None = None,
        confidence: str | None = None,
        category: str | None = None,
        links: list[dict[str, Any]] | None = None,
        acknowledge_credential: bool = False,
        acknowledge_transient: bool = False,
        acknowledge_user_claim: bool = False,
        acknowledge_truncation: bool = False,
        standing: float | None = None,
        said: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await deps._run(
            lambda d: _handlers_pkg.memory_update(
                d,
                id,
                content=content,
                scopes=scopes,
                confidence=confidence,
                category=category,
                links=links,
                acknowledge_credential=acknowledge_credential,
                acknowledge_transient=acknowledge_transient,
                acknowledge_user_claim=acknowledge_user_claim,
                acknowledge_truncation=acknowledge_truncation,
                standing=standing,
                said=said,
                ctx=ctx,
            )
        )

    @_refusals_as_tool_errors
    async def memory_remove(
        self,
        id: str,
        reason: str,
        said: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await deps._run(
            lambda d: _handlers_pkg.memory_remove(d, id, reason, said=said, ctx=ctx)
        )

    @_refusals_as_tool_errors
    async def memory_verify(
        self,
        id: str,
        note: str | None = None,
        verified_paths: list[str] | None = None,
        verified_commits: list[str] | None = None,
        verified_versions: list[str] | None = None,
        verified_absent_paths: list[str] | None = None,
        claims: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await deps._run(
            lambda d: _handlers_pkg.memory_verify(
                d,
                id,
                note=note,
                verified_paths=verified_paths,
                verified_commits=verified_commits,
                verified_versions=verified_versions,
                verified_absent_paths=verified_absent_paths,
                claims=claims,
                ctx=ctx,
            )
        )

    @_refusals_as_tool_errors
    async def memory_record_use(
        self,
        memory_ids: list[str],
        outcome: str,
        note: str | None = None,
        claim_excerpts: list[str | None] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await deps._run(
            lambda d: _handlers_pkg.memory_record_use(
                d,
                memory_ids,
                outcome,
                note=note,
                claim_excerpts=claim_excerpts,
                ctx=ctx,
            )
        )

    @_refusals_as_tool_errors
    async def episode(
        self,
        action: EpisodeAction,
        body: str | None = None,
        takeaway: str | None = None,
        scopes: list[str] | None = None,
        swarm_id: str | None = None,
        prior_session_id: str | None = None,
        max_episodes: int | None = None,
        include_bodies: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await deps._run(
            lambda d: _handlers_pkg.episode(
                d,
                action,
                body=body,
                takeaway=takeaway,
                scopes=scopes,
                swarm_id=swarm_id,
                prior_session_id=prior_session_id,
                max_episodes=max_episodes,
                include_bodies=include_bodies,
                ctx=ctx,
            )
        )

    @_refusals_as_tool_errors
    async def memory_admin(
        self,
        action: AdminAction,
        id: str | None = None,
        scope: str | None = None,
        scopes: list[str] | None = None,
        old_scope: str | None = None,
        new_scope: str | None = None,
        scan: bool = False,
        verdict: str | None = None,
        note: str | None = None,
        reason: str | None = None,
        before: str | None = None,
        # The capture's closing call: member id -> {related id: label}.
        # Served as a plain object, as `review` is.
        relations: dict[str, Any] = Field(default_factory=dict),
        # The membership declarations (`handlers/questions.py`).
        answer: str | None = None,
        target: str | None = None,
        said: str | None = None,
        # answer_standing: record id -> probability. Served as `relations` is.
        standing: dict[str, Any] = Field(default_factory=dict),
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        if isinstance(relations, FieldInfo):
            relations = {}
        if isinstance(standing, FieldInfo):
            standing = {}
        return await deps._run(
            lambda d: _handlers_pkg.memory_admin(
                d,
                action,
                id=id,
                scope=scope,
                scopes=scopes,
                old_scope=old_scope,
                new_scope=new_scope,
                scan=scan,
                verdict=verdict,
                note=note,
                reason=reason,
                before=before,
                relations=relations,
                answer=answer,
                target=target,
                said=said,
                standing=standing,
                ctx=ctx,
            )
        )


__all__ = [
    "Context",
    "DESC_EPISODE",
    "DESC_MEMORY_ADMIN",
    "DESC_MEMORY_RECORD_USE",
    "DESC_MEMORY_REMOVE",
    "DESC_MEMORY_SEARCH",
    "DESC_MEMORY_SHOW",
    "DESC_MEMORY_UPDATE",
    "DESC_MEMORY_VERIFY",
    "DESC_MEMORY_WRITE",
    "ToolHandlers",
    "_advance_turn",
    "_already_recorded_pending_ids",
    "_attach_use_tokens",
    "_event_ts_epoch",
    "_hook_attributed_pending_ids",
    "_validate_content_size",
    "_validate_write_payload",
    "capture_origin",
]
