"""ToolHandlers facade — wires per-tool handler functions to a class.

Pre-Round-2 every MCP tool was a method on a 1700-line ``ToolHandlers``
god class. Round 2 moved each handler into its own module under
``handlers/`` (one module per tool, or one per symmetric pair). The
god class shrank but did NOT disappear — what's left here is a
~500-line dependency-bundle + per-tool delegation surface. It is
intentionally not "thin": every public tool method re-lists the same
kwargs the SDK will introspect, because the ``mcp.tool(...)``
decorator builds the JSON schema from ``inspect.signature(method)``
and a ``**kwargs``-only delegate would land in the client manifest
as a typeless catch-all. The signature has to be spelled out per
tool; the body underneath is one line forwarding to the per-tool
module function.

A ``__getattr__`` + registry would be ~20 lines instead of 220 but
would erase the schema (any client introspecting the tool list would
see ``**kwargs`` and no parameter docs). The per-tool methods are
the price of keeping the wire surface byte-identical to the
pre-refactor shape — see the docstring on each method for the call
it delegates to.

Why this file still has names like ``_already_recorded_pending_ids``
and ``capture_origin``: the test suite monkey-patches both. Keeping
them as module-level bindings here (re-exported from their new homes)
preserves the patch surface the suite documents. The new per-tool
modules in ``handlers/`` route their ``capture_origin`` calls through
``from .. import _handlers as _h; _h.capture_origin(...)`` so the
patch propagates — see ``handlers/_shared.py`` for the contract.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from . import handlers as _handlers_pkg
from ._response import ResponseBuilder
from .config import Config
from .events import Recorder
from .handlers._shared import (
    Context,
    _advance_turn,
    _already_recorded_pending_ids,
    _attach_use_tokens,
    _drain_pending_expired,
    _event_ts_epoch,
    _hook_attributed_pending_ids,
    _validate_content_size,
    _validate_write_payload,
)
from .origin import capture as capture_origin
from .session import SessionSource
from .store import DefaultStoreSource, MemoryStore, StoreSource

log = logging.getLogger("bettermemory._handlers")


# Re-export the description constants the server's `_register_tools`
# reaches for. Moving the strings into the per-tool modules let each
# DESC live next to the handler body it describes; re-exporting here
# preserves the historical `from ._handlers import DESC_*` import
# path the server uses. (Tests import handlers via the SDK's tool
# manager rather than these constants, so the re-export exists for
# server.py's benefit and back-compat with any out-of-tree caller.)
DESC_EPISODE_HANDOFF = _handlers_pkg.DESC_EPISODE_HANDOFF
DESC_EPISODE_PATTERNS = _handlers_pkg.DESC_EPISODE_PATTERNS
DESC_EPISODE_PROMOTE = _handlers_pkg.DESC_EPISODE_PROMOTE
DESC_EPISODE_SEARCH = _handlers_pkg.DESC_EPISODE_SEARCH
DESC_EPISODE_WRITE = _handlers_pkg.DESC_EPISODE_WRITE
DESC_MEMORY_ACKNOWLEDGE_MISS = _handlers_pkg.DESC_MEMORY_ACKNOWLEDGE_MISS
DESC_MEMORY_AUDIT_TURN = _handlers_pkg.DESC_MEMORY_AUDIT_TURN
DESC_MEMORY_CONFLICTS = _handlers_pkg.DESC_MEMORY_CONFLICTS
DESC_MEMORY_CURATE = _handlers_pkg.DESC_MEMORY_CURATE
DESC_MEMORY_HEALTH = _handlers_pkg.DESC_MEMORY_HEALTH
DESC_MEMORY_LINKS_TAIL = _handlers_pkg.DESC_MEMORY_LINKS_TAIL
DESC_MEMORY_LIST = _handlers_pkg.DESC_MEMORY_LIST
DESC_MEMORY_LIST_TOMBSTONES = _handlers_pkg.DESC_MEMORY_LIST_TOMBSTONES
DESC_MEMORY_PROPOSALS = _handlers_pkg.DESC_MEMORY_PROPOSALS
DESC_MEMORY_RECORD_USE = _handlers_pkg.DESC_MEMORY_RECORD_USE
DESC_MEMORY_REMOVE = _handlers_pkg.DESC_MEMORY_REMOVE
DESC_MEMORY_RENAME_SCOPE = _handlers_pkg.DESC_MEMORY_RENAME_SCOPE
DESC_MEMORY_RESTORE = _handlers_pkg.DESC_MEMORY_RESTORE
DESC_MEMORY_SCOPE_DISABLE = _handlers_pkg.DESC_MEMORY_SCOPE_DISABLE
DESC_MEMORY_SCOPE_ENABLE = _handlers_pkg.DESC_MEMORY_SCOPE_ENABLE
DESC_MEMORY_SCOPE_OVERVIEW = _handlers_pkg.DESC_MEMORY_SCOPE_OVERVIEW
DESC_MEMORY_SEARCH = _handlers_pkg.DESC_MEMORY_SEARCH
DESC_MEMORY_SHOW = _handlers_pkg.DESC_MEMORY_SHOW
DESC_MEMORY_UPDATE = _handlers_pkg.DESC_MEMORY_UPDATE
DESC_MEMORY_VERIFY = _handlers_pkg.DESC_MEMORY_VERIFY
DESC_MEMORY_WRITE = _handlers_pkg.DESC_MEMORY_WRITE
DESC_MEMORY_WRITE_CANCEL = _handlers_pkg.DESC_MEMORY_WRITE_CANCEL
DESC_MEMORY_WRITE_CONFIRM = _handlers_pkg.DESC_MEMORY_WRITE_CONFIRM


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
    store: MemoryStore,
    query: str,
    scopes: list[str] | None = None,
    *,
    client: str | None = None,
    model: str | None = None,
) -> tuple[list[Any], bool, bool]:
    """Either load all active memories or pre-filter via the FTS5
    index, depending on store size and index health.

    Returns ``(candidates, prefilter_saturated, prefiltered)``.

    ``prefilter_saturated`` is True only when the FTS prefilter
    path served the candidates AND the index returned a full
    cap-sized slice (`_PREFILTER_CAP` rows) — the signal the
    cap-starvation guard in `handlers/search.py` keys on. It is
    computed from the INDEX row count, not the loaded list: the
    per-candidate skips below (filename-lookup misses, id/body
    drift) can shrink the loaded list under the cap, which would
    mask saturation from a length check on the returned list.
    Every `load_all` branch reports False — the full corpus has
    no cap to be starved by.

    ``prefiltered`` is the weaker, separate claim that the FTS path
    served these candidates AT ALL, saturated or not. The two are not
    interchangeable and the difference is load-bearing: a prefilter
    that returns 30 rows is under the cap — so not saturated — but
    every one of those rows is present BECAUSE it matched the query,
    which is exactly the condition that collapses corpus statistics
    derived from the pool. `handlers/search.py` keys the BM25
    corpus-IDF lookup on this flag rather than on saturation; see
    `index.corpus_document_frequencies`.

    When `scopes` is given it is threaded into the FTS pre-filter so
    the bounded candidate slice is drawn from IN-SCOPE matches. Without
    it the index returns the 50 globally-highest-BM25 rows and the
    authoritative scope filter (`search.run_search`) narrows them — so
    on a large indexed store a scoped query whose in-scope matches all
    rank #51+ globally would come back empty even though matching
    memories exist. The index's `scopes_text LIKE '% scope %'` filter
    is the same exact, space-padded set-membership the authoritative
    `memory_scope_set & scope_filter` applies, so threading it never
    drops a candidate the authoritative pass would have kept.

    `client` / `model` thread into the prefilter for exactly the same
    reason and with the same guarantee: they are equality on the stored
    declared string, which is what the authoritative
    `search.candidate_admitted` applies, so the SQL cannot drop a row the
    ranker would have kept. Without them a store dominated by one
    writer's memories spends the whole 50-row cap on that writer and a
    filtered search for a rarer one comes back empty with matches on
    disk — the actor-filter form of the scope problem described above.

    The current heuristic: walk the index status once. If the
    on-disk index exists, is not flagged `needs_rebuild` (the
    post-schema-migration state where only touched memories are
    indexed), has `indexed_count >= threshold`, and the query is
    non-empty, we query the index for up to 50 candidate ids and
    load just those by walking the file store for matches.
    Otherwise the full `load_all` runs (current behaviour, byte-
    stable result quality).

    Falls back to load_all when the index returns no candidates —
    a stale index missing recent writes shouldn't silently hide
    results. The recovery path is `bettermemory reindex`.

    Also falls back (with a logged warning) when the index reads
    themselves raise: `status()` above inspects only the
    meta/sqlite_master pages, so page-level corruption in the
    data/FTS b-trees passes the gate and first surfaces out of
    `query()` / `load_many()`. Same routing as
    `needs_rebuild` — full scan, correct results, never a crashed
    search.

    NOT a substitute for `store.load_all()` in any caller that needs the
    store's SIZE rather than a query's candidates: every return except
    the fallbacks is capped and query-biased. `consolidate.
    run_auto_consolidate`'s bounded-store guard is the standing example
    — it reads `len()` as the active-set size, so handing it this list
    would silently disarm it.
    """
    import sqlite3

    from . import index as _index

    if not query.strip():
        return store.load_all(), False, False
    status = _index.status(store.root)
    # `needs_rebuild` means a schema-version migration dropped the
    # data tables and only incrementally-touched memories are back:
    # `indexed_count` can cross the threshold while every untouched
    # pre-upgrade memory is missing, so the count is not a coverage
    # signal until `rebuild()` clears the flag. Treat the index as
    # unusable outright — same routing as unreadable/absent.
    #
    # `index_unreadable` rather than `status.get("corrupt")`: a
    # version-skewed index reports `schema_skew` and no `corrupt` key
    # (7.13.0), and testing the old key alone would fall through to the
    # `indexed_count` read below against a None count.
    if (
        not status.get("exists")
        or _index.index_unreadable(status)
        or status.get("needs_rebuild")
    ):
        return store.load_all(), False, False
    indexed_count = int(status.get("indexed_count", 0) or 0)
    if indexed_count < resolve_index_threshold():
        return store.load_all(), False, False

    # Pre-filter via the index. 50 candidates is generous for a
    # default max_results of 5 — the downstream ranker reorders
    # within the candidate pool, so we want enough variety for
    # recency / scope-boost / coverage to find the best 5.
    #
    # Both index reads sit in ONE guard: the `status()` gate above
    # reads only the meta/sqlite_master pages, so an index whose
    # data/FTS b-tree pages are corrupt (torn WAL recovery, disk
    # fault) passes the gate and first fails HERE. The catch set
    # mirrors `status()`'s never-raises classification (ValueError:
    # unparseable meta IS corruption; IndexVersionError: a
    # concurrent migration can land a newer schema between the gate
    # and these reads). The index is a regenerable cache and the
    # canonical .md files are intact, so warn once and take the
    # same `load_all` routing as `needs_rebuild` — degrade, never
    # crash the tool call. (`Store.load_many` resolves its filenames
    # inside the guard because it walks the same data pages; its
    # empty-ids call is a free short-circuit.)
    try:
        candidate_pairs = _index.query(
            store.root,
            query,
            scopes=scopes,
            client=client,
            model=model,
            max_results=_PREFILTER_CAP,
        )
        candidate_ids = {cid for cid, _ in candidate_pairs}
        ids = list(candidate_ids)
        # The id -> record half is `Store.load_many`'s job now. It sits
        # INSIDE this guard on purpose: it resolves through the index
        # (`filenames_for_ids`) and then reads the files, so a
        # page-level index failure surfaces HERE, where the warning and
        # the `load_all` routing below can answer it. `load_many` does
        # not swallow index errors itself — a version that did would
        # return whichever candidates happened to resolve, which this
        # caller cannot tell apart from a complete pool.
        loaded: list[Any] = store.load_many(ids)
    except (
        OSError,
        ValueError,
        sqlite3.DatabaseError,
        _index.IndexVersionError,
    ) as exc:
        log.warning(
            "index candidate pre-filter failed: %s: %s. Search falls "
            "back to a full store scan. Run `bettermemory reindex` "
            "to repair.",
            type(exc).__name__,
            exc,
        )
        return store.load_all(), False, False
    if not candidate_pairs:
        # Stale index or query that genuinely matches nothing —
        # fall back to load_all so we don't silently miss recent
        # writes that aren't in the index yet.
        return store.load_all(), False, False
    # Pin the saturation signal HERE, before the per-candidate
    # loading step can drop rows — see the docstring.
    prefilter_saturated = len(candidate_pairs) == _PREFILTER_CAP

    # The id -> record half is `Store.load_many`'s job now, so the
    # quarantine predicate, the parse-failure width and the
    # index-drift guard live with the store rather than here. What
    # stays here is the FALLBACK decision, which is this function's:
    # candidates the lookup could not resolve (a pre-v2 schema row,
    # an entry removed since the FTS pre-filter ran) are skipped, and
    # if every candidate misses we fall back to `load_all` below —
    # search must never silently return empty when the FTS
    # pre-filter actually matched.
    if not loaded:
        # FTS matched, but every candidate's filename lookup
        # missed (pre-v2 schema rows, every match tombstoned
        # between pre-filter and read, etc.). Fall back to
        # `load_all` so the documented contract — "search keeps
        # working through schema upgrades and stale-index
        # windows" — actually holds. Hot path is the loaded
        # branch above; this is the safety net.
        return store.load_all(), False, False
    return loaded, prefilter_saturated, True


# ---------------------------------------------------------------------------
# ToolHandlers — the facade
# ---------------------------------------------------------------------------


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
        store: MemoryStore,
        sessions: SessionSource,
        recorder: Recorder,
        responses: ResponseBuilder,
        store_source: StoreSource | None = None,
    ) -> None:
        from .episodes import EpisodeStore

        self.config = config
        self.store = store
        # Episodes live in a sibling subtree of the memory root and
        # share the trust boundary. Construct lazily so legacy callers
        # that build a ToolHandlers without ever touching the episode
        # surface don't materialize the `episodes/` directory.
        self.episode_store = EpisodeStore(store.root)
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
    #
    # Teams Phase 1 / D2. The MCP SDK holds bound methods of ONE
    # `ToolHandlers` (constructed once in `builder.py`, its 27 bound
    # methods handed to `mcp.tool`), so typing the store anywhere up the
    # call chain still leaves `self` process-wide. This is the last point
    # where a request's `ctx` and the dependency bundle are both in hand,
    # so it is where a request can be asked what store it is for.
    #
    # What follows from resolving HERE rather than at the read sites:
    # all 108 request-path `deps.store` reads, the 18 `deps.episode_store`
    # reads and the `deps.store.root` fan-outs follow the rebound bundle
    # with no edit at any of them — including the reads inside the 12
    # ctx-less helpers (e.g. `conflicts._resolve_verdict`,
    # `show._links_payload`, `write._persist`), which hold no `ctx` and
    # so could not resolve a store themselves. Threading a resolved store
    # to those sites instead was priced and rejected: ~89 rewrites and
    # ~12 signature changes against 27 delegation lines.

    def for_request(self, ctx: Context | None = None) -> ToolHandlers:
        """The bundle this request should run against.

        Returns `self` when the resolved store IS this bundle's store —
        which is every request today, and every stdio request by
        construction, since a principal-less request shares one bucket.
        The copy branch exists so a store source that resolves a
        different store per principal needs no change here.

        Rebinds `store` and `episode_store` TOGETHER. Episodes are a
        sibling subtree of the memory root, so rebinding one and not the
        other would serve tenant A's memories beside tenant B's journal
        with nothing erroring — and it is not confined to the
        `episode_*` tools: `memory_write_confirm` reaches
        `deps.episode_store` privately to delete a promoted source
        episode, so a core memory tool would silently read the wrong
        tenant.

        Deliberately does NOT rebind `recorder`, and that is a stated
        limit rather than an oversight: the recorder's root comes from
        config (`builder.py`), never from the store, and it carries the
        event log the provenance tier is computed from. A recorder that
        followed a per-request store would be a per-tenant event log,
        which is a policy decision this unit does not make. Until the
        mount policy lands, the recorder stays process-wide — pinned by
        `test_for_request_leaves_the_recorder_on_the_process_root`, so
        the split is asserted rather than assumed.
        """
        resolved = self.store_source.for_request(ctx)
        if resolved is self.store:
            return self
        from .episodes import EpisodeStore

        clone = copy.copy(self)
        clone.store = resolved
        clone.episode_store = EpisodeStore(resolved.root)
        return clone

    # ---- FTS candidate prefilter ----------------------------------------
    #
    # Deliberately NOT a method. The prefilter is module-level
    # (`load_search_candidates`) so the out-of-process Stop hook can reach
    # it with only a `Store`, and the sole consumer
    # (`handlers.search.resolve_search_pool`) calls it directly. The bound
    # alias that survived the extraction is gone: it had no caller in src/,
    # tests/, bench/, examples/ or plugin/, and the prose that kept it
    # alive — a spread of citations naming it as the routing
    # implementation — now names the module function instead. Don't
    # re-add it: a caller holding this bundle is a caller that could take
    # the module function and its explicit `Store`, and a bound alias with
    # no caller is a name that reads live while reaching nothing.

    # ---- delegations to per-tool modules --------------------------------
    #
    # Each method below threads `self` (the dependency bundle) into the
    # corresponding `handlers.*` function. The method signature mirrors
    # the function signature minus its leading `deps` argument so
    # the SDK's `inspect.signature` introspection produces the same
    # JSON schema as the pre-Round-2 shape.

    async def memory_search(
        self,
        query: str,
        scopes: list[str] | None = None,
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

    async def memory_write_confirm(
        self, pending_id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_write_confirm(deps, pending_id, ctx=ctx)

    async def memory_write_cancel(
        self, pending_id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_write_cancel(deps, pending_id, ctx=ctx)

    async def episode_write(
        self,
        body: str,
        takeaway: str | None = None,
        scopes: list[str] | None = None,
        swarm_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.episode_write(
            deps,
            body,
            takeaway=takeaway,
            scopes=scopes,
            swarm_id=swarm_id,
            ctx=ctx,
        )

    async def episode_handoff(
        self,
        prior_session_id: str | None = None,
        max_episodes: int | None = None,
        include_bodies: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.episode_handoff(
            deps,
            prior_session_id=prior_session_id,
            max_episodes=max_episodes,
            include_bodies=include_bodies,
            ctx=ctx,
        )

    async def episode_search(
        self,
        scopes: list[str] | None = None,
        parent_session_id: str | None = None,
        swarm_id: str | None = None,
        since: str | None = None,
        max_results: int | None = None,
        auto_scope: bool = True,
        include_bodies: bool = True,
        ids: list[str] | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.episode_search(
            deps,
            scopes=scopes,
            parent_session_id=parent_session_id,
            swarm_id=swarm_id,
            since=since,
            max_results=max_results,
            auto_scope=auto_scope,
            include_bodies=include_bodies,
            ids=ids,
            ctx=ctx,
        )

    async def episode_promote(
        self,
        episode_id: str,
        scopes: list[str],
        category: str = "fact",
        confidence: str = "medium",
        source: str = "explicit-statement",
        use_body: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.episode_promote(
            deps,
            episode_id,
            scopes=scopes,
            category=category,
            confidence=confidence,
            source=source,
            use_body=use_body,
            ctx=ctx,
        )

    async def episode_patterns(
        self,
        promote: str | None = None,
        dismiss: str | None = None,
        body: str | None = None,
        scopes: list[str] | None = None,
        category: str = "fact",
        confidence: str = "medium",
        source: str = "inferred",
        min_sessions: int = 3,
        max_patterns: int = 5,
        auto_scope: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.episode_patterns(
            deps,
            promote=promote,
            dismiss=dismiss,
            body=body,
            scopes=scopes,
            category=category,
            confidence=confidence,
            source=source,
            min_sessions=min_sessions,
            max_patterns=max_patterns,
            auto_scope=auto_scope,
            ctx=ctx,
        )

    async def memory_conflicts(
        self,
        scan: bool = False,
        resolve: str | None = None,
        verdict: str | None = None,
        note: str | None = None,
        max_results: int = 10,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_conflicts(
            deps,
            scan=scan,
            resolve=resolve,
            verdict=verdict,
            note=note,
            max_results=max_results,
            ctx=ctx,
        )

    async def memory_proposals(
        self,
        action: str = "list",
        proposal_id: str | None = None,
        scopes: list[str] | None = None,
        category: str | None = None,
        force: bool = False,
        acknowledge_credential: bool = False,
        acknowledge_transient: bool = False,
        acknowledge_scope_mismatch: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_proposals(
            deps,
            action=action,
            proposal_id=proposal_id,
            scopes=scopes,
            category=category,
            force=force,
            acknowledge_credential=acknowledge_credential,
            acknowledge_transient=acknowledge_transient,
            acknowledge_scope_mismatch=acknowledge_scope_mismatch,
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

    async def memory_list(
        self,
        scopes: list[str] | None = None,
        with_bodies: bool = False,
        client: str | None = None,
        model: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_list(
            deps,
            scopes=scopes,
            with_bodies=with_bodies,
            client=client,
            model=model,
            ctx=ctx,
        )

    async def memory_remove(
        self, id: str, reason: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_remove(deps, id, reason, ctx=ctx)

    async def memory_list_tombstones(
        self,
        scopes: list[str] | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_list_tombstones(deps, scopes=scopes, ctx=ctx)

    async def memory_restore(
        self, id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_restore(deps, id, ctx=ctx)

    async def memory_health(
        self,
        window_days: int = 30,
        heavily_used_top_k: int = 10,
        min_applied: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_health(
            deps,
            window_days=window_days,
            heavily_used_top_k=heavily_used_top_k,
            min_applied=min_applied,
            ctx=ctx,
        )

    async def memory_curate(
        self,
        dry_run: bool = True,
        window_days: int = 30,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_curate(
            deps, dry_run=dry_run, window_days=window_days, ctx=ctx
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

    async def memory_scope_overview(
        self,
        auto_scope: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_scope_overview(
            deps, auto_scope=auto_scope, ctx=ctx
        )

    async def memory_scope_disable(
        self, scope: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_scope_disable(deps, scope, ctx=ctx)

    async def memory_scope_enable(
        self, scope: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_scope_enable(deps, scope, ctx=ctx)

    async def memory_rename_scope(
        self,
        old_scope: str,
        new_scope: str,
        include_tombstones: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_rename_scope(
            deps,
            old_scope,
            new_scope,
            include_tombstones=include_tombstones,
            ctx=ctx,
        )

    async def memory_audit_turn(
        self,
        user_message: str,
        assistant_response: str | None = None,
        lookback_seconds: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_audit_turn(
            deps,
            user_message,
            assistant_response=assistant_response,
            lookback_seconds=lookback_seconds,
            ctx=ctx,
        )

    async def memory_acknowledge_miss(
        self,
        event_id: str,
        reason: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        deps = self.for_request(ctx)
        return await _handlers_pkg.memory_acknowledge_miss(
            deps,
            event_id,
            reason,
            ctx=ctx,
        )


# Re-exports used by tests:
# - `_already_recorded_pending_ids` is imported directly by
#   tests/test_server.py.
# - `capture_origin` is monkey-patched by tests/test_server_origin.py
#   and tests/test_server_commit_drift.py to inject a fake
#   capture.
# - `_advance_turn`, `_drain_pending_expired`, `_event_ts_epoch`,
#   `_attach_use_tokens`, `_validate_content_size`,
#   `_validate_write_payload`, `_hook_attributed_pending_ids` are
#   carried for any out-of-tree caller that pulled them off this
#   module in the past. New handler bodies import them from
#   `handlers/_shared.py`.
__all__ = [
    "Context",
    "DESC_EPISODE_PATTERNS",
    "DESC_MEMORY_ACKNOWLEDGE_MISS",
    "DESC_MEMORY_AUDIT_TURN",
    "DESC_MEMORY_CONFLICTS",
    "DESC_MEMORY_CURATE",
    "DESC_MEMORY_HEALTH",
    "DESC_MEMORY_LINKS_TAIL",
    "DESC_MEMORY_LIST",
    "DESC_MEMORY_LIST_TOMBSTONES",
    "DESC_MEMORY_RECORD_USE",
    "DESC_MEMORY_REMOVE",
    "DESC_MEMORY_RENAME_SCOPE",
    "DESC_MEMORY_RESTORE",
    "DESC_MEMORY_SCOPE_DISABLE",
    "DESC_MEMORY_SCOPE_ENABLE",
    "DESC_MEMORY_SCOPE_OVERVIEW",
    "DESC_MEMORY_SEARCH",
    "DESC_MEMORY_SHOW",
    "DESC_MEMORY_UPDATE",
    "DESC_MEMORY_VERIFY",
    "DESC_MEMORY_WRITE",
    "DESC_MEMORY_WRITE_CANCEL",
    "DESC_MEMORY_WRITE_CONFIRM",
    "ToolHandlers",
    "_advance_turn",
    "_already_recorded_pending_ids",
    "_attach_use_tokens",
    "_drain_pending_expired",
    "_event_ts_epoch",
    "_hook_attributed_pending_ids",
    "_validate_content_size",
    "_validate_write_payload",
    "capture_origin",
]
