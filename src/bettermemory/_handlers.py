"""ToolHandlers facade: the dependency bundle and one method per served tool.

Every public tool method re-lists the kwargs the SDK will introspect,
because `mcp.tool(...)` builds the JSON schema from
`inspect.signature(method)`, and a `**kwargs` delegate would land in the
client manifest as a typeless catch-all. The body underneath is one line
forwarding to the per-tool module function under `handlers/`.

Why this file carries names like `_already_recorded_pending_ids` and
`capture_origin`: the test suite monkey-patches both. Keeping them as
module-level bindings here, re-exported from their homes, preserves the
patch surface; the per-tool modules route their `capture_origin` calls
through `from .. import _handlers as _h; _h.capture_origin(...)` so the
patch propagates (see `handlers/_shared.py`).
"""

from __future__ import annotations

import copy
import logging
from typing import Any

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
from .store import DefaultStoreSource, Store, StoreSource

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
    ) -> None:
        self.config = config
        self.store = store
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
        return clone

    # ---- delegations to per-tool modules --------------------------------
    #
    # Each method threads `self` (the dependency bundle) into the
    # corresponding `handlers.*` function. The signature mirrors the
    # function's minus its leading `deps` argument, so the SDK's
    # `inspect.signature` introspection produces the served schema.

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
        return await _handlers_pkg.memory_search(
            deps,
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

    async def memory_show(self, id: str, ctx: Context | None = None) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_show(deps, id, ctx=ctx)

    async def memory_write(
        self,
        content: str,
        scopes: list[str],
        confidence: str = "medium",
        source: str = "explicit-statement",
        force: bool = False,
        acknowledge_transient: bool = False,
        acknowledge_scope_mismatch: bool = False,
        acknowledge_ungrounded: bool = False,
        acknowledge_credential: bool = False,
        acknowledge_user_claim: bool = False,
        category: str = "fact",
        groundedness_check: bool = False,
        source_transcript: str | None = None,
        claims: list[str] | None = None,
        supersedes: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_write(
            deps,
            content,
            scopes,
            confidence=confidence,
            source=source,
            force=force,
            acknowledge_transient=acknowledge_transient,
            acknowledge_scope_mismatch=acknowledge_scope_mismatch,
            acknowledge_ungrounded=acknowledge_ungrounded,
            acknowledge_credential=acknowledge_credential,
            acknowledge_user_claim=acknowledge_user_claim,
            category=category,
            groundedness_check=groundedness_check,
            source_transcript=source_transcript,
            claims=claims,
            supersedes=supersedes,
            ctx=ctx,
        )

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
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_update(
            deps,
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
            ctx=ctx,
        )

    async def memory_remove(
        self, id: str, reason: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_remove(deps, id, reason, ctx=ctx)

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
        return await _handlers_pkg.memory_verify(
            deps,
            id,
            note=note,
            verified_paths=verified_paths,
            verified_commits=verified_commits,
            verified_versions=verified_versions,
            verified_absent_paths=verified_absent_paths,
            claims=claims,
            ctx=ctx,
        )

    async def memory_record_use(
        self,
        memory_ids: list[str],
        outcome: str,
        note: str | None = None,
        claim_excerpts: list[str | None] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_record_use(
            deps,
            memory_ids,
            outcome,
            note=note,
            claim_excerpts=claim_excerpts,
            ctx=ctx,
        )

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
        return await _handlers_pkg.episode(
            deps,
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
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_admin(
            deps,
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
            ctx=ctx,
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
