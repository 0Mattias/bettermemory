"""MCP server entry point and tool registration.

The full tool surface (mirrored in `prompts.SYSTEM_PROMPT_ADDENDUM` so
the consuming model sees an identical list):

- Retrieval: memory_search, memory_show, memory_list, memory_scope_overview
- Writing:   memory_write (+ _confirm / _cancel staged-write pair),
             memory_update
- Lifecycle: memory_remove, memory_restore, memory_list_tombstones
- Verification: memory_verify
- Curation:  memory_record_use, memory_health, memory_rename_scope
- Session:   memory_scope_disable / memory_scope_enable

Each handler is thin: validate the input via the Pydantic models, call
into `store` / `search`, emit one event to the `Recorder`, return a
JSON-serializable result. The recorder is best-effort — telemetry
failures are logged but never propagate up into a tool call.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config, load_config
from .durability import TransientMatch, find_transient_markers
from .events import Recorder
from .health import report_for_directory
from .origin import Origin, capture as capture_origin
from .models import (
    Confidence,
    MemoryHit,
    MemorySummary,
    SimilarHit,
    Source,
    TombstonedSummary,
    is_valid_ulid,
    validate_scope,
)
from .prompts import SYSTEM_PROMPT_ADDENDUM
from .search import find_similar, find_similar_tombstones, search as run_search
from .session import SessionState, get_state
from .store import (
    MemoryNotFoundError,
    NotTombstonedError,
    Store,
    TombstonedError,
)
from .verify import detect_path_drift


log = logging.getLogger("bettermemory")


# ---------------------------------------------------------------------------
# Use-recording outcomes — values land verbatim in the event log so the
# health view can aggregate them. Add new outcomes by extending this set;
# don't rename existing values without a migration story.
# ---------------------------------------------------------------------------


_USE_OUTCOMES: frozenset[str] = frozenset(
    {
        "applied",  # The retrieved memory shaped the response.
        "ignored",  # Retrieved but turned out off-topic.
        "contradicted",  # The user or current state contradicted the memory.
    }
)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_server(
    *,
    config: Config | None = None,
    store: Store | None = None,
    state: SessionState | None = None,
    recorder: Recorder | None = None,
) -> FastMCP:
    """Return a configured FastMCP instance.

    Tests pass in their own `store`, `state`, and `recorder` to keep things
    hermetic. The real entry point in `main()` lets `load_config` resolve
    everything. When `recorder` is None, one is constructed from `config` —
    `enabled=False` in the telemetry config makes every event a no-op.
    """
    config = config or load_config()
    store = store or Store(config.resolved_directory())
    state = state or get_state()
    if recorder is None:
        recorder = Recorder(
            root=config.resolved_directory(),
            session_id=state.session_id,
            enabled=config.telemetry.enabled,
            max_bytes=config.telemetry.max_bytes,
        )

    # Wire the persistent embedding cache to this store's directory. The
    # configure call doesn't load anything from disk yet; hydration is
    # lazy on the first cached_embed call so non-semantic-dedup sessions
    # never touch the file.
    _configure_persistent_embeddings(config, store)

    mcp = FastMCP(
        "bettermemory",
        instructions=(
            "Local file-backed memory. Memory is OPT-IN: call memory_search "
            "only when the user references shared context you don't have, or "
            "asks 'do you remember'. Default to not retrieving."
        ),
    )

    _register_tools(mcp, config=config, store=store, state=state, recorder=recorder)
    return mcp


def _semantic_model_or_none(config: Config) -> Any:
    """Lazy load the embedding model when `semantic_dedup = true` and the
    extras are installed. Returns None otherwise — callers treat None as
    the Jaccard fallback signal. The first call after `semantic_dedup`
    is enabled pays the model-load cost (~1-2s); subsequent calls hit
    `semantic.get_model`'s in-memory cache.
    """
    if not config.behavior.semantic_dedup:
        return None
    from .semantic import get_model

    return get_model(config.behavior.semantic_model_name)


def _configure_persistent_embeddings(config: Config, store: Store) -> None:
    """Hook the persistent embedding cache to the active store dir when
    semantic dedup is enabled. The cache file lives next to the events
    log and the memory bodies so it shares the same trust boundary —
    nothing new in the permissions story. No-op when semantic dedup is
    off; when off, the in-memory cache is unused too, so persistence
    would be a write-only cycle."""
    if not config.behavior.semantic_dedup:
        return
    from .semantic import configure_persistent_cache

    configure_persistent_cache(store.root, config.behavior.semantic_model_name)


def _register_tools(
    mcp: FastMCP,
    *,
    config: Config,
    store: Store,
    state: SessionState,
    recorder: Recorder,
) -> None:
    # ---- memory_search ---------------------------------------------------

    @mcp.tool(
        name="memory_search",
        description=(
            "Search stored memories. Call this only when you have reason to "
            "think the user is referencing context you don't have, or when "
            "the user explicitly asks. Default to not searching. Returns "
            "ranked hits with snippets — call memory_show for full content. "
            "Each hit includes `relevance` (high/medium/low) and "
            "`match_terms` (which query words actually hit). Branch on "
            '`relevance`, not the raw `score` — and treat "low" hits as '
            "probable noise unless you have a reason to use them. "
            "Each hit also carries `path_drift_checked` and "
            "`path_drift_missing` integer counts so you can self-triage "
            "stale hits without a memory_show round-trip — a hit with "
            "`path_drift_missing > 0` cites filesystem paths that no "
            "longer exist on disk, which is your cue to expand and "
            "consider memory_update or memory_verify. "
            "Pass `expand_top=True` to inline the full body of the top hit "
            'when its relevance is "high" — collapses the common '
            "search-then-show round trip into one call, and surfaces the "
            "full `path_drift` report (with the actual missing paths) on "
            "the expanded hit. Skip it when you only need to triage. "
            "By default (`auto_scope=True`), results are filtered to "
            "memories written from the current repository — cross-project "
            "memories are excluded. Memories written outside any repo "
            "(or before the auto-scope feature) are treated as global and "
            "always pass. Set `auto_scope=False` for cross-project queries "
            '("do you remember anything about X across all my projects").'
        ),
    )
    async def memory_search(
        query: str,
        scopes: list[str] | None = None,
        max_results: int | None = None,
        expand_top: bool = False,
        auto_scope: bool = True,
    ) -> list[dict[str, Any]]:
        if max_results is None:
            max_results = config.behavior.default_max_results
        max_results = max(1, min(int(max_results), 50))

        if scopes:
            scopes = [validate_scope(s) for s in scopes]

        # Auto-scope: capture the caller's current origin so we can drop
        # memories from a different repo. None when the caller isn't in a
        # repo (auto-scope is meaningless without a project boundary).
        repo_filter: str | None = None
        if auto_scope:
            current_origin = capture_origin()
            repo_filter = current_origin.repo

        memories = store.load_all()
        hits = run_search(
            memories,
            query,
            scopes=scopes,
            excluded_scopes=set(state.disabled_scopes),
            repo_filter=repo_filter,
            max_results=max_results,
            half_life_days=config.behavior.recency_boost_half_life_days,
        )
        out = [_hit_to_dict(h) for h in hits]

        # Optional auto-expansion of the top hit. Conservative: only fires
        # when the top hit clearly wins ("high" relevance) so the model
        # doesn't get hosed with full bodies it didn't really need.
        # Path-drift runs against the expanded body — if we're already
        # paying the load cost, surfacing drift here saves a memory_show
        # round-trip when the model needs to act on it.
        expanded_id: str | None = None
        expanded_drift_missing = 0
        if expand_top and out and out[0]["relevance"] == "high":
            try:
                memory = store.load_one(hits[0].id)
            except (MemoryNotFoundError, TombstonedError):
                # Race: memory was tombstoned between search and show.
                # Drop the body silently, the snippet still got returned.
                pass
            else:
                out[0]["body"] = memory.body
                drift = detect_path_drift(memory.body)
                if drift.has_drift:
                    out[0]["path_drift"] = drift.to_dict()
                    expanded_drift_missing = len(drift.missing)
                expanded_id = memory.id

        recorder.record(
            "search",
            query=query,
            scopes_filter=scopes,
            max_results=max_results,
            returned=[h["id"] for h in out],
            relevance=[h["relevance"] for h in out],
            expand_top=expand_top,
            expanded_id=expanded_id,
            expanded_drift_missing=expanded_drift_missing,
            auto_scope=auto_scope,
            repo_filter=repo_filter,
        )
        return out

    # ---- memory_show -----------------------------------------------------

    @mcp.tool(
        name="memory_show",
        description=(
            "Fetch a single memory's full content by ID. Use after "
            "memory_search when a snippet looks relevant and you want the "
            "full body. The response includes `last_verified_at` (null if "
            "the memory has never been spot-checked since write — call "
            "memory_verify after confirming the body still matches reality) "
            "and `path_drift` when the body cites filesystem paths that no "
            "longer exist on disk. Drift is advisory: it may indicate a "
            "stale memory, but it can also be a temporary mount or a path "
            "from a different machine. Treat it as a signal to spot-check, "
            "not a verdict."
        ),
    )
    async def memory_show(id: str) -> dict[str, Any]:
        try:
            memory = store.load_one(id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        # Path-drift runs against the full body. We surface `path_drift`
        # only when there's something actionable: a memory with no path
        # claims (or all-healthy paths) returns the field as null so the
        # consumer can branch on `if path_drift is not None`. Without
        # that, every memory_show would carry an empty `path_drift` dict
        # and the model would learn to ignore the field even when it
        # mattered.
        drift = detect_path_drift(memory.body)
        recorder.record(
            "show",
            id=memory.id,
            path_drift_checked=len(drift.checked),
            path_drift_missing=len(drift.missing),
        )
        return {
            "id": memory.id,
            "scopes": memory.scopes,
            "confidence": memory.confidence.value,
            "source": memory.source.value,
            "created": _isoformat(memory.created),
            "updated": _isoformat(memory.updated),
            "last_verified_at": _isoformat_optional(memory.last_verified_at),
            "body": memory.body,
            "origin": _origin_to_dict(memory.origin),
            "path_drift": drift.to_dict() if drift.has_drift else None,
        }

    # ---- memory_write ----------------------------------------------------

    @mcp.tool(
        name="memory_write",
        description=(
            "Create a new memory. Durable facts only. The tool runs a "
            "structural durability check on the body before writing: any "
            'transient-state marker ("currently", "today I", "we just", '
            '"the new", commit-SHA-like hex tokens, etc.) returns '
            "{status:'transient_warning', markers:[...]} instead of "
            "committing. Either rephrase the body to extract the level-up "
            "durable form (the architectural decision, the why, the "
            "what-was-built — discard the timestamp/state) or pass "
            "`acknowledge_transient=True` if the marker is genuinely "
            "durable in this case (rare — most fires are real). "
            "Confirm with the user only when the memory captures an "
            "inference about them (preferences, beliefs); for project / "
            "infra / reference / tooling memories, write directly and "
            "announce the save. Provide non-empty scopes (e.g. ['tools', "
            "'learning-style']). Content dedup runs after the durability "
            "check: if an existing memory has high overlap with the new "
            "body, this returns {status:'duplicate', matches:[...]} instead "
            "of creating a parallel entry — prefer memory_update on the "
            "matched id. Pass `force=True` to override when the new memory "
            "is meaningfully different (you have already inspected the "
            "matches and decided they are adjacent topics, not duplicates). "
            "Medium-overlap matches don't block; they're surfaced as "
            "`related` on the success response. If "
            "`require_write_confirmation` is true in config, a write that "
            "passes both checks returns {status:'pending', pending_id} and "
            "you must call memory_write_confirm(pending_id) to commit."
        ),
    )
    async def memory_write(
        content: str,
        scopes: list[str],
        confidence: str = "medium",
        source: str = "explicit-statement",
        force: bool = False,
        acknowledge_transient: bool = False,
    ) -> dict[str, Any]:
        payload = _validate_write_payload(
            content=content,
            scopes=scopes,
            confidence=confidence,
            source=source,
            allowed_scopes=config.scopes.allowed,
        )

        # Origin is captured before the durability check so it's always
        # part of the payload that flows into either staging or the direct
        # write path. We never persist origin for a transient_warning — the
        # write isn't happening — so the early return below short-circuits
        # before any disk I/O.
        payload["origin"] = capture_origin()

        # Durability check runs before dedup. A transient body shouldn't
        # become a duplicate of an existing transient memory — we'd just be
        # routing the caller toward memory_update on a fact that itself
        # shouldn't have been written. Catch transience first.
        transient_hits = find_transient_markers(payload["content"])
        if transient_hits and not acknowledge_transient:
            recorder.record(
                "write",
                status="transient_warning",
                scopes=payload["scopes"],
                forced=False,
                markers=[h.marker for h in transient_hits],
            )
            return {
                "status": "transient_warning",
                "markers": [_transient_to_dict(h) for h in transient_hits],
                "hint": (
                    "The body contains transient-state markers that won't "
                    "be true in a week. Either rephrase to the durable "
                    "level-up version (extract the architectural decision, "
                    "the why, what-was-built — discard the timestamp/state) "
                    "or pass acknowledge_transient=True if the marker is "
                    "genuinely durable in context."
                ),
            }

        # Dedup runs second — staging or writing happens only if the new body
        # isn't a high-overlap duplicate of an existing memory. `force=True`
        # is the override path: the caller has already looked at the matches
        # and decided this entry is meaningfully different.
        #
        # Two passes: active memories (returns "high"/"medium" relevance) and
        # tombstoned memories (returns "high-removed"/"medium-removed"). An
        # active high match is the strongest signal — there's a live record
        # to update — and short-circuits with status="duplicate". A
        # tombstone high match (without an active high) becomes
        # status="previously_removed", carrying the original removal_reason
        # so the writer can decide whether the rejection still applies.
        # Medium hits from either pass are surfaced as `related` /
        # `removed_related` and don't block.
        related: list[SimilarHit] = []
        removed_related: list[SimilarHit] = []
        if not force:
            semantic_model = _semantic_model_or_none(config)
            high_threshold = (
                config.behavior.semantic_high_threshold
                if config.behavior.semantic_dedup
                else None
            )
            medium_threshold = (
                config.behavior.semantic_medium_threshold
                if config.behavior.semantic_dedup
                else None
            )
            similar = find_similar(
                payload["content"],
                store.load_all(),
                semantic_model=semantic_model,
                high_threshold=high_threshold,
                medium_threshold=medium_threshold,
            )
            high = [h for h in similar if h.relevance == "high"]
            if high:
                recorder.record(
                    "write",
                    status="duplicate",
                    scopes=payload["scopes"],
                    forced=False,
                    matches=[h.id for h in high],
                )
                return {
                    "status": "duplicate",
                    "matches": [_similar_to_dict(h) for h in high],
                    "hint": (
                        "An existing memory has high content overlap with "
                        "this write. Prefer memory_update on the matched "
                        "id over creating a parallel entry. Pass force=True "
                        "if the new memory is meaningfully different."
                    ),
                }
            related = [h for h in similar if h.relevance == "medium"]

            # Tombstone-aware dedup. Only runs when no active high-overlap
            # match was found — otherwise the active path is the better
            # answer (update the live entry rather than discussing the
            # removed one).
            tombstone_similar = find_similar_tombstones(
                payload["content"],
                store.load_tombstones(),
                semantic_model=semantic_model,
                high_threshold=high_threshold,
                medium_threshold=medium_threshold,
            )
            high_removed = [
                h for h in tombstone_similar if h.relevance == "high-removed"
            ]
            if high_removed:
                recorder.record(
                    "write",
                    status="previously_removed",
                    scopes=payload["scopes"],
                    forced=False,
                    removed_matches=[h.id for h in high_removed],
                )
                return {
                    "status": "previously_removed",
                    "removed_matches": [_similar_to_dict(h) for h in high_removed],
                    "hint": (
                        "A previously-removed memory has high content overlap "
                        "with this write. Inspect each `removed_reason` — if "
                        "the rejection still applies, drop the write; if the "
                        "fact is now correct, call memory_restore(id) on the "
                        "tombstone instead of writing a parallel entry. Pass "
                        "force=True to bypass when the new memory is "
                        "meaningfully different from the removed one."
                    ),
                }
            removed_related = [
                h for h in tombstone_similar if h.relevance == "medium-removed"
            ]

        # Capture which markers (if any) were overridden by
        # acknowledge_transient — feeds the override-rate signal in the
        # event log so we can tell whether a marker is producing too many
        # false positives.
        acknowledged = (
            [h.marker for h in transient_hits]
            if transient_hits and acknowledge_transient
            else []
        )

        if config.behavior.require_write_confirmation:
            pending = state.stage_write(payload)
            response: dict[str, Any] = {
                "status": "pending",
                "pending_id": pending.pending_id,
                "preview": {
                    "content": payload["content"],
                    "scopes": payload["scopes"],
                    "confidence": payload["confidence"].value,
                    "source": payload["source"].value,
                },
                "hint": (
                    "Confirm with memory_write_confirm(pending_id) or "
                    "drop with memory_write_cancel(pending_id)."
                ),
            }
            if related:
                response["related"] = [_similar_to_dict(h) for h in related]
            if removed_related:
                response["removed_related"] = [
                    _similar_to_dict(h) for h in removed_related
                ]
            recorder.record(
                "write",
                status="pending",
                pending_id=pending.pending_id,
                scopes=payload["scopes"],
                forced=force,
                related=[h.id for h in related],
                removed_related=[h.id for h in removed_related],
                markers_acknowledged=acknowledged,
            )
            return response

        memory = store.write(**payload)
        recorder.record(
            "write",
            status="committed",
            id=memory.id,
            scopes=memory.scopes,
            confidence=memory.confidence.value,
            source=memory.source.value,
            forced=force,
            related=[h.id for h in related],
            removed_related=[h.id for h in removed_related],
            markers_acknowledged=acknowledged,
        )
        return _committed(memory, related=related, removed_related=removed_related)

    @mcp.tool(
        name="memory_write_confirm",
        description=(
            "Commit a memory_write that returned status='pending'. "
            "Pass the pending_id from that response."
        ),
    )
    async def memory_write_confirm(pending_id: str) -> dict[str, Any]:
        pending = state.take_pending(pending_id)
        if pending is None:
            raise ValueError(
                f"no pending write with id {pending_id!r} (it may have "
                "expired or been already committed)"
            )
        memory = store.write(**pending.payload)
        recorder.record(
            "write_confirm",
            pending_id=pending_id,
            id=memory.id,
            scopes=memory.scopes,
        )
        return _committed(memory)

    @mcp.tool(
        name="memory_write_cancel",
        description=(
            "Drop a pending memory_write without committing. "
            "Pass the pending_id from the original write response."
        ),
    )
    async def memory_write_cancel(pending_id: str) -> dict[str, Any]:
        existed = state.cancel_pending(pending_id)
        recorder.record("write_cancel", pending_id=pending_id, existed=existed)
        return {"cancelled": pending_id, "existed": existed}

    # ---- memory_update ---------------------------------------------------

    @mcp.tool(
        name="memory_update",
        description=(
            "Refine an existing memory in place. Pass the memory id and any "
            "of `content`, `scopes`, `confidence` to change. Preserves `id`, "
            "`created`, and `source`; bumps `updated`. Prefer this over "
            "memory_remove + memory_write when correcting or refining a "
            "stored fact — delete-and-recreate loses the original timestamp "
            "and litters .tombstones/ with what are really edits. Pass at "
            "least one field; replace semantics for `scopes` (provide the "
            "full new list, not a delta)."
        ),
    )
    async def memory_update(
        id: str,
        content: str | None = None,
        scopes: list[str] | None = None,
        confidence: str | None = None,
    ) -> dict[str, Any]:
        if content is None and scopes is None and confidence is None:
            raise ValueError(
                "memory_update needs at least one of content, scopes, or confidence"
            )
        if content is not None and not content.strip():
            raise ValueError("content must be non-empty if provided")

        try:
            existing = store.load_one(id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc

        new_scopes = existing.scopes
        if scopes is not None:
            if not scopes:
                raise ValueError("scopes must contain at least one entry if provided")
            new_scopes = [validate_scope(s) for s in scopes]
            if config.scopes.allowed:
                allowed = set(config.scopes.allowed)
                unknown = [s for s in new_scopes if s not in allowed]
                if unknown:
                    raise ValueError(
                        f"scope(s) not in allowed list: {unknown}. "
                        f"Allowed: {sorted(config.scopes.allowed)}"
                    )

        new_confidence = existing.confidence
        if confidence is not None:
            try:
                new_confidence = Confidence(confidence)
            except ValueError as exc:
                raise ValueError(
                    f"confidence must be one of {[c.value for c in Confidence]}"
                ) from exc

        new_body = existing.body
        if content is not None:
            new_body = content.strip() + "\n"

        # When `content` changes, the prior verification was for prose
        # that no longer exists — reset `last_verified_at` to None so the
        # caller has to re-confirm against the new body. Scope/confidence
        # edits don't touch the body's claims, so the verification stays
        # intact for those. This matches the intuition that verification
        # is a property of body content, not of metadata.
        update_fields: dict[str, Any] = {
            "body": new_body,
            "scopes": new_scopes,
            "confidence": new_confidence,
        }
        if content is not None:
            update_fields["last_verified_at"] = None

        merged = existing.model_copy(update=update_fields)
        updated = store.update(merged)
        fields_changed = [
            name
            for name, value in (
                ("content", content),
                ("scopes", scopes),
                ("confidence", confidence),
            )
            if value is not None
        ]
        recorder.record(
            "update",
            id=updated.id,
            fields=fields_changed,
            scopes=updated.scopes,
            confidence=updated.confidence.value,
        )
        return _committed(updated)

    # ---- memory_list -----------------------------------------------------

    @mcp.tool(
        name="memory_list",
        description=(
            "List active memories. By default returns one-line summaries "
            "(IDs, scopes, summary, no body) — cheap triage. "
            "Pass `with_bodies=True` to inline full bodies in one call; "
            "useful for small stores where N round trips of "
            "`list -> show -> show` would be wasteful. Don't reach for "
            "`with_bodies` casually — it pulls every memory in scope into "
            "your context, which is the failure mode this project exists "
            "to avoid. Filter by `scopes` if you only care about a subset."
        ),
    )
    async def memory_list(
        scopes: list[str] | None = None,
        with_bodies: bool = False,
    ) -> list[dict[str, Any]]:
        if scopes:
            scopes = [validate_scope(s) for s in scopes]
        # Apply session-disabled scopes to listing too — consistency.
        excluded = set(state.disabled_scopes)

        if with_bodies:
            out: list[dict[str, Any]] = []
            for memory in store.load_all():
                memory_scopes = set(memory.scopes)
                if excluded and (memory_scopes & excluded):
                    continue
                if scopes and not (memory_scopes & set(scopes)):
                    continue
                out.append(_memory_to_dict(memory))
            recorder.record(
                "list",
                scopes_filter=scopes,
                with_bodies=True,
                count=len(out),
                returned=[m["id"] for m in out],
            )
            return out

        out_summary: list[dict[str, Any]] = []
        for summary in store.list_summaries(scopes=scopes):
            if excluded and (set(summary.scopes) & excluded):
                continue
            out_summary.append(_summary_to_dict(summary))
        recorder.record(
            "list",
            scopes_filter=scopes,
            with_bodies=False,
            count=len(out_summary),
            returned=[s["id"] for s in out_summary],
        )
        return out_summary

    # ---- memory_remove ---------------------------------------------------

    @mcp.tool(
        name="memory_remove",
        description=(
            "Tombstone a memory. The file is moved to .tombstones/ with a "
            "removal reason and the originating session id — never hard-"
            "deleted. Use when a stored fact is wrong or no longer relevant. "
            "Tombstones remain searchable via memory_list_tombstones and "
            "are surfaced as `removed_matches` on memory_write when a new "
            "body looks similar to a previously-removed fact, so the "
            "lesson encoded in the removal reason isn't lost. Use "
            "memory_restore(id) to undo an accidental removal."
        ),
    )
    async def memory_remove(id: str, reason: str) -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        try:
            tombstone_path = store.tombstone(id, reason, session_id=state.session_id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        recorder.record("remove", id=id, reason=reason)
        return {
            "removed": id,
            "tombstone_path": str(tombstone_path),
        }

    # ---- memory_list_tombstones ------------------------------------------

    @mcp.tool(
        name="memory_list_tombstones",
        description=(
            "List removed (tombstoned) memories. One-line summaries plus "
            "removal metadata (`removed`, `removed_reason`, "
            "`removed_session`) — body stripped, like memory_list. Use "
            'for curation passes ("what did I clear out last month?") or '
            "to investigate when the user asks 'I think I had a memory "
            "about X — what happened?'. Pass `scopes` to filter, like "
            "memory_list. Tombstones are sorted by `removed` descending — "
            "most-recently-removed first."
        ),
    )
    async def memory_list_tombstones(
        scopes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if scopes:
            scopes = [validate_scope(s) for s in scopes]
        excluded = set(state.disabled_scopes)
        out: list[dict[str, Any]] = []
        for summary in store.list_tombstones(scopes=scopes):
            if excluded and (set(summary.scopes) & excluded):
                continue
            out.append(_tombstone_summary_to_dict(summary))
        recorder.record(
            "list_tombstones",
            scopes_filter=scopes,
            count=len(out),
            returned=[s["id"] for s in out],
        )
        return out

    # ---- memory_restore --------------------------------------------------

    @mcp.tool(
        name="memory_restore",
        description=(
            "Bring a tombstoned memory back to the active set. Strips the "
            "removal frontmatter, moves the file out of .tombstones/, and "
            "preserves the original `created`, `updated`, and "
            "`last_verified_at` timestamps — the body didn't change while "
            "it was tombstoned, so the recency boost stays honest. Raises "
            "if the id is active (use memory_update for edits) or unknown. "
            "The original removal reason and session live on in the event "
            "log even after restore."
        ),
    )
    async def memory_restore(id: str) -> dict[str, Any]:
        try:
            memory = store.restore(id)
        except NotTombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        except ValueError:
            # _load_tombstone_path raises ValueError on a malformed file
            # (e.g. missing `created`). Surface verbatim — the message
            # tells the caller which field is missing.
            raise
        recorder.record(
            "restore",
            id=memory.id,
            scopes=memory.scopes,
        )
        return _committed(memory)

    # ---- memory_scope_disable / enable -----------------------------------

    # ---- memory_health ---------------------------------------------------

    @mcp.tool(
        name="memory_health",
        description=(
            "Aggregate health view over the event log + active memories. "
            "Returns a structured report with dead-weight memories (created "
            "more than `window_days` ago, never `applied` according to "
            "memory_record_use), heavily-used memories, memories with "
            "unresolved contradictions, transient-marker fire/override "
            "rates, the scope distribution, a per-scope rollup "
            "(`scope_health`) showing where dead weight and contradictions "
            "concentrate, singleton scopes (`rare_scopes`, likely typos), "
            "and an `orphan_use_events` counter (memory_record_use calls "
            "whose ids resolved to no record — a fabrication smoke test). "
            "Use this to drive curation passes — prune dead weight, "
            "refresh contradicted memories via memory_update, trim "
            "transient markers whose override rate is high, fix typo "
            "scopes via memory_rename_scope. The corresponding CLI is "
            "`bettermemory health`. `min_applied` floors the heavily_used "
            "bucket on applied_count (default comes from config.toml — "
            "typically 3 — to keep the bucket out of one-off-"
            "acknowledgement noise). Per-row stats include "
            "`last_verified_at` so a curation pass can flag rows that "
            "haven't been spot-checked recently."
        ),
    )
    async def memory_health(
        window_days: int = 30,
        heavily_used_top_k: int = 10,
        min_applied: int | None = None,
    ) -> dict[str, Any]:
        # Falling through to the configured default lets the tool stay
        # ergonomic for the common case (don't pass anything, get the
        # tuned threshold) while still allowing a per-call override
        # ("show me everything that's been applied at least once on this
        # young store").
        threshold = (
            int(min_applied)
            if min_applied is not None
            else config.behavior.heavily_used_min_applied
        )
        report = report_for_directory(
            store.root,
            window_days=int(window_days),
            heavily_used_top_k=int(heavily_used_top_k),
            heavily_used_min_applied=threshold,
        )
        return report.to_dict()

    # ---- memory_record_use ----------------------------------------------

    @mcp.tool(
        name="memory_record_use",
        description=(
            "Record how a retrieved memory was used in your response. Call "
            "this once per response that consumed memory output, with the "
            'ids you actually relied on and an outcome of "applied" '
            '(the memory shaped the reply), "ignored" (you retrieved it '
            'but it turned out off-topic), or "contradicted" (the user '
            "or current state contradicted the stored fact). The event "
            "feeds the memory_health view so dead-weight memories can be "
            "pruned and stale ones can be flagged. `note` is an optional "
            "free-form string for context. Skip the call when no retrieved "
            "memory shaped your response — silence is also signal, as the "
            "absence of `applied` events for a recently-retrieved id is "
            "what tells us the memory wasn't useful."
        ),
    )
    async def memory_record_use(
        memory_ids: list[str],
        outcome: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        if not memory_ids:
            raise ValueError("memory_ids must contain at least one entry")
        if outcome not in _USE_OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(_USE_OUTCOMES)}")
        # ULID-format check only — we don't load the store to confirm the
        # id exists. Recording a use against a just-tombstoned memory is a
        # legitimate signal (the user contradicted it, we removed it),
        # and a load_all on every record_use call is wasteful.
        for mid in memory_ids:
            if not is_valid_ulid(mid):
                raise ValueError(f"invalid memory id: {mid!r}")
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string if provided")

        recorder.record(
            "use",
            ids=list(memory_ids),
            outcome=outcome,
            note=note,
        )
        return {
            "recorded": list(memory_ids),
            "outcome": outcome,
        }

    # ---- memory_verify ---------------------------------------------------

    @mcp.tool(
        name="memory_verify",
        description=(
            "Bump `last_verified_at` to now after spot-checking that a "
            "memory's claims still match reality. Call this when you have "
            "actively confirmed the body's verifiable content — file paths "
            "still exist, the version number still matches, the script is "
            "still where it says, the configuration is still what it says. "
            "Verification is the orthogonal axis to content edits: this "
            "tool does not touch `updated`, and `memory_update` does not "
            "touch `last_verified_at`. A typo fix bumps `updated` (the "
            "body changed) but not `last_verified_at` (no claim to have "
            "spot-checked the world); a verify call bumps "
            "`last_verified_at` (you confirmed reality) but not `updated` "
            "(the body is unchanged). `note` is an optional free-form "
            "string captured in the event log — use it to record what "
            "was checked ('confirmed `/usr/local/sbin/zb-backup.sh` "
            "exists on the homelab'). Idempotent: calling twice in a row "
            "just slides the timestamp forward."
        ),
    )
    async def memory_verify(
        id: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string if provided")
        try:
            memory = store.mark_verified(id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        recorder.record(
            "verify",
            id=memory.id,
            last_verified_at=_isoformat_optional(memory.last_verified_at),
            note=note,
        )
        return {
            "verified": memory.id,
            "last_verified_at": _isoformat_optional(memory.last_verified_at),
            "updated": _isoformat(memory.updated),
        }

    # ---- memory_scope_overview ------------------------------------------

    @mcp.tool(
        name="memory_scope_overview",
        description=(
            "Cheap session-start hint: counts of memories per scope, "
            "without bodies, IDs, or summaries. Default-scoped to the "
            "caller's current repository (memories with no origin pass as "
            "global). Returns `{current_repo, scopes: {scope: count}, "
            "total}`. Use this once at the start of a conversation to see "
            "whether stored memory exists for the current project — if "
            "`total` is 0, you can skip memory_search for the rest of the "
            "session unless the user explicitly asks. If the count is "
            "non-zero, memory_search remains the way to retrieve content; "
            "this tool only tells you whether searching is likely to be "
            "fruitful. Set `auto_scope=False` to count across all stored "
            "memory regardless of origin (the cross-project view). Counts "
            "respect session-disabled scopes."
        ),
    )
    async def memory_scope_overview(
        auto_scope: bool = True,
    ) -> dict[str, Any]:
        repo_filter: str | None = None
        current_origin: Origin | None = None
        if auto_scope:
            current_origin = capture_origin()
            repo_filter = current_origin.repo

        excluded = set(state.disabled_scopes)
        scope_counts: dict[str, int] = {}
        total = 0
        for memory in store.load_all():
            memory_scope_set = set(memory.scopes)
            if excluded and (memory_scope_set & excluded):
                continue
            if repo_filter is not None:
                memory_repo = memory.origin.repo if memory.origin else None
                # Reuse the same repos_match semantics as memory_search so
                # this tool's `current_repo` filter is bit-identical to
                # the search filter — otherwise the model would see
                # "5 memories tagged projects:foo" here and zero hits in
                # search and have no way to reconcile that.
                from .origin import repos_match

                if not repos_match(memory_repo, repo_filter):
                    continue
            total += 1
            for scope in memory.scopes:
                if scope in excluded:
                    continue
                scope_counts[scope] = scope_counts.get(scope, 0) + 1

        # Sort scopes by count desc, then name for determinism. Important
        # for tests and for the model — a stable ordering means a "if the
        # top scope is X" branch behaves consistently across calls.
        sorted_scopes = dict(
            sorted(scope_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        recorder.record(
            "scope_overview",
            auto_scope=auto_scope,
            current_repo=repo_filter,
            total=total,
            scope_count=len(sorted_scopes),
        )
        return {
            "current_repo": repo_filter,
            "current_cwd": current_origin.cwd if current_origin else None,
            "auto_scope": auto_scope,
            "scopes": sorted_scopes,
            "total": total,
            "disabled_scopes": sorted(state.disabled_scopes),
        }

    @mcp.tool(
        name="memory_scope_disable",
        description=(
            "Disable a scope for the rest of this session. Subsequent "
            "memory_search and memory_list calls will exclude memories "
            "tagged with this scope. Useful when the user says 'this is "
            "unrelated to project X'. Resets when the server restarts."
        ),
    )
    async def memory_scope_disable(scope: str) -> dict[str, Any]:
        clean = validate_scope(scope)
        state.disable(clean)
        recorder.record("scope_disable", scope=clean)
        return {"disabled_scopes": sorted(state.disabled_scopes)}

    @mcp.tool(
        name="memory_scope_enable",
        description=("Re-enable a previously disabled scope for this session."),
    )
    async def memory_scope_enable(scope: str) -> dict[str, Any]:
        clean = validate_scope(scope)
        state.enable(clean)
        recorder.record("scope_enable", scope=clean)
        return {"disabled_scopes": sorted(state.disabled_scopes)}

    # ---- memory_rename_scope ---------------------------------------------

    @mcp.tool(
        name="memory_rename_scope",
        description=(
            "Replace `old_scope` with `new_scope` across active memories "
            "(and tombstones, by default). The cheap fix for typo'd or "
            "deprecated scopes — e.g. `projct:foo` -> `projects:foo` "
            "after a misspell, or `infra` -> `infrastructure` after "
            "settling on the long form. Bumps `updated` on each touched "
            "memory; preserves `last_verified_at` (the body's claims "
            "didn't change, only the tag did). Memories that already "
            "carry `new_scope` get `old_scope` removed without "
            "duplicating `new_scope`. Returns "
            "`{active: [ids], tombstoned: [ids]}` for the records that "
            "were actually modified. Pass `include_tombstones=False` to "
            "leave the removal audit log untouched. Use after "
            "memory_health surfaces a typo in `rare_scopes`."
        ),
    )
    async def memory_rename_scope(
        old_scope: str,
        new_scope: str,
        include_tombstones: bool = True,
    ) -> dict[str, Any]:
        clean_old = validate_scope(old_scope)
        clean_new = validate_scope(new_scope)
        if clean_old == clean_new:
            raise ValueError("old_scope and new_scope must differ")
        if config.scopes.allowed and clean_new not in set(config.scopes.allowed):
            raise ValueError(
                f"new_scope {clean_new!r} is not in the allowed list: "
                f"{sorted(config.scopes.allowed)}"
            )
        result = store.rename_scope(
            clean_old, clean_new, include_tombstones=include_tombstones
        )
        recorder.record(
            "rename_scope",
            old=clean_old,
            new=clean_new,
            include_tombstones=include_tombstones,
            active_count=len(result["active"]),
            tombstoned_count=len(result["tombstoned"]),
        )
        return {
            "old_scope": clean_old,
            "new_scope": clean_new,
            "active": result["active"],
            "tombstoned": result["tombstoned"],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_write_payload(
    *,
    content: str,
    scopes: list[str],
    confidence: str,
    source: str,
    allowed_scopes: list[str],
) -> dict[str, Any]:
    """Validate and normalise the kwargs for `Store.write`.

    Returns a dict suitable for `Store.write(**payload)`. Raises ValueError
    on any input problem so the model gets a clear error.
    """
    if not content or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not scopes:
        raise ValueError("scopes must contain at least one entry")

    clean_scopes = [validate_scope(s) for s in scopes]

    if allowed_scopes:
        allowed_set = set(allowed_scopes)
        unknown = [s for s in clean_scopes if s not in allowed_set]
        if unknown:
            raise ValueError(
                f"scope(s) not in allowed list: {unknown}. "
                f"Allowed: {sorted(allowed_scopes)}"
            )

    try:
        conf_enum = Confidence(confidence)
    except ValueError as exc:
        raise ValueError(
            f"confidence must be one of {[c.value for c in Confidence]}"
        ) from exc
    try:
        src_enum = Source(source)
    except ValueError as exc:
        raise ValueError(f"source must be one of {[s.value for s in Source]}") from exc

    return {
        "content": content,
        "scopes": clean_scopes,
        "confidence": conf_enum,
        "source": src_enum,
    }


def _committed(  # type: ignore[no-untyped-def]
    memory,
    *,
    related: list[SimilarHit] | None = None,
    removed_related: list[SimilarHit] | None = None,
) -> dict[str, Any]:
    """Serialise a freshly-written Memory into the tool response shape.

    `related` carries any medium-overlap matches that didn't block the
    write — surfaced so the caller can still consider memory_update on a
    similar existing entry, just without a hard refusal.

    `removed_related` carries medium-overlap matches against tombstoned
    memories. Same shape as `related` plus the populated `removed_at` /
    `removed_reason` fields. Surfaced advisorily — the writer hasn't
    duplicated a previously-removed fact, but they're working in the
    same neighbourhood and may want to consult the removal reason.
    """
    out: dict[str, Any] = {
        "status": "committed",
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "created": _isoformat(memory.created),
        "updated": _isoformat(memory.updated),
        "last_verified_at": _isoformat_optional(memory.last_verified_at),
    }
    if related:
        out["related"] = [_similar_to_dict(h) for h in related]
    if removed_related:
        out["removed_related"] = [_similar_to_dict(h) for h in removed_related]
    return out


def _similar_to_dict(hit: SimilarHit) -> dict[str, Any]:
    """Serialise a SimilarHit to the tool response shape.

    `removed_at` / `removed_reason` are emitted only when populated —
    active hits keep the response shape lean by omitting the keys, while
    tombstone hits carry both. Consumers can branch on
    `"removed_reason" in hit` or on `relevance.endswith("-removed")`."""
    out: dict[str, Any] = {
        "id": hit.id,
        "scopes": hit.scopes,
        "confidence": hit.confidence.value,
        "snippet": hit.snippet,
        "similarity": hit.similarity,
        "relevance": hit.relevance,
        "created": _isoformat(hit.created),
        "updated": _isoformat(hit.updated),
    }
    if hit.removed_at is not None:
        out["removed_at"] = _isoformat(hit.removed_at)
    if hit.removed_reason is not None:
        out["removed_reason"] = hit.removed_reason
    return out


def _transient_to_dict(hit: TransientMatch) -> dict[str, Any]:
    """Serialize a transient-marker match for the tool response."""
    return {"marker": hit.marker, "snippet": hit.snippet}


def _isoformat(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _isoformat_optional(dt: datetime | None) -> str | None:
    """ISO-format `dt`, returning None when the input is None.

    Distinct from `_isoformat` because `None` is a meaningful response
    value for `last_verified_at` — "never verified" is a valid state, not
    an error. Returning the literal None lets JSON-serialisation produce
    `"last_verified_at": null` which the caller can branch on directly.
    """
    return None if dt is None else _isoformat(dt)


def _hit_to_dict(hit: MemoryHit) -> dict[str, Any]:
    return {
        "id": hit.id,
        "scopes": hit.scopes,
        "confidence": hit.confidence.value,
        "snippet": hit.snippet,
        "score": hit.score,
        "relevance": hit.relevance,
        "match_terms": hit.match_terms,
        "created": _isoformat(hit.created),
        "updated": _isoformat(hit.updated),
        "last_verified_at": _isoformat_optional(hit.last_verified_at),
        "path_drift_checked": hit.path_drift_checked,
        "path_drift_missing": hit.path_drift_missing,
    }


def _summary_to_dict(summary: MemorySummary) -> dict[str, Any]:
    return {
        "id": summary.id,
        "scopes": summary.scopes,
        "confidence": summary.confidence.value,
        "summary": summary.summary,
        "created": _isoformat(summary.created),
        "updated": _isoformat(summary.updated),
        "last_verified_at": _isoformat_optional(summary.last_verified_at),
    }


def _tombstone_summary_to_dict(summary: TombstonedSummary) -> dict[str, Any]:
    """Same shape as `_summary_to_dict` plus removal metadata.

    Mirroring the active shape lets a curator iterate uniformly: a row
    has `removed` set if and only if it's a tombstone. `removed_session`
    is `null` for legacy tombstones written before that field shipped.
    """
    return {
        "id": summary.id,
        "scopes": summary.scopes,
        "confidence": summary.confidence.value,
        "summary": summary.summary,
        "created": _isoformat(summary.created),
        "updated": _isoformat(summary.updated),
        "last_verified_at": _isoformat_optional(summary.last_verified_at),
        "removed": _isoformat(summary.removed),
        "removed_reason": summary.removed_reason,
        "removed_session": summary.removed_session,
    }


def _memory_to_dict(memory) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Full memory shape used by `memory_list(with_bodies=True)`.

    Same fields as `memory_show` plus the `summary` line so a consumer can
    treat the response uniformly with the body-less `memory_list` shape.
    """
    from .models import first_summary_line

    return {
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "summary": first_summary_line(memory.body),
        "body": memory.body,
        "created": _isoformat(memory.created),
        "updated": _isoformat(memory.updated),
        "last_verified_at": _isoformat_optional(memory.last_verified_at),
        "origin": _origin_to_dict(memory.origin),
    }


def _origin_to_dict(origin: Origin | None) -> dict[str, Any] | None:
    """Serialize an Origin for tool responses, or None if absent.

    Empty fields are stripped so the response carries only the data that
    was actually captured at write time. A memory written before this
    feature shipped returns None; a memory written outside any git repo
    returns `{"cwd": "..."}` without `repo` or `branch` keys.
    """
    if origin is None:
        return None
    payload = origin.model_dump(mode="json", exclude_none=True)
    return payload or None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point. By default runs the MCP server over stdio
    (`bettermemory`). Subcommands provide offline tooling: `bettermemory
    health` prints the aggregate report, mirroring the `memory_health`
    tool in human-readable form."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="bettermemory",
        description=(
            "Local file-backed memory MCP server with retrieval-on-demand. "
            "Run with no arguments to start the MCP server over stdio."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    health_parser = sub.add_parser(
        "health", help="Print the aggregate memory health report."
    )
    health_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    health_parser.add_argument(
        "--days",
        type=int,
        default=30,
        help=(
            "Window in days for the dead-weight cutoff. Memories created "
            "more than this many days ago with no `applied` events are "
            "flagged. Default: 30."
        ),
    )
    health_parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="How many heavily-used memories to list. Default: 10.",
    )
    health_parser.add_argument(
        "--min-applied",
        type=int,
        default=None,
        help=(
            "Minimum applied_count for inclusion in heavily_used. Default "
            "comes from config.toml `behavior.heavily_used_min_applied` "
            "(typically 3). Lower to 1 on a fresh store to see anything "
            "that's been applied at least once."
        ),
    )

    migrate_parser = sub.add_parser(
        "migrate",
        help=(
            "One-shot data migrations. Use `migrate origin` to backfill "
            "the origin field on memories written before that field "
            "existed."
        ),
    )
    migrate_sub = migrate_parser.add_subparsers(dest="migrate_cmd")
    origin_parser = migrate_sub.add_parser(
        "origin",
        help=(
            "Backfill origin frontmatter on legacy memories. Idempotent: "
            "memories that already have an origin field are skipped."
        ),
    )
    origin_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    origin_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help=(
            "Force-tag every legacy memory with this remote URL. Use "
            "when the auto-inference from the parent directory isn't "
            "right (e.g. global memory dir that you know belongs to one "
            "repo)."
        ),
    )
    origin_parser.add_argument(
        "--scope-repo",
        action="append",
        default=[],
        metavar="SCOPE=URL",
        help=(
            "Route memories by scope: tag any memory carrying SCOPE "
            "with the given remote URL. Repeat for multiple scopes "
            "(e.g. --scope-repo projects:foo=git@github.com:me/foo.git "
            "--scope-repo projects:bar=git@github.com:me/bar.git). "
            "Memories whose scopes match nothing in the map fall through "
            "to --repo (if given) or are left untagged. The right tool "
            "for a global memory dir whose memories already use "
            "projects:<name> tags."
        ),
    )

    tombstones_parser = sub.add_parser(
        "tombstones",
        help=(
            "Inspect and prune the tombstone (removed-memory) audit log. "
            "Subcommands: list, prune."
        ),
    )
    tombstones_sub = tombstones_parser.add_subparsers(dest="tombstones_cmd")

    tlist_parser = tombstones_sub.add_parser(
        "list", help="Print all tombstones with removal metadata."
    )
    tlist_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    tlist_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to tombstones tagged with at least one of the given "
            "scopes. Repeat to widen the filter."
        ),
    )

    tprune_parser = tombstones_sub.add_parser(
        "prune",
        help=(
            "Hard-delete tombstones older than --older-than days. "
            "Active memories are unaffected. Default value comes from "
            "config.toml `behavior.tombstone_retention_days`; if that's 0 "
            "(the default), --older-than is required."
        ),
    )
    tprune_parser.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "Cutoff in days. Tombstones whose `removed` timestamp is older "
            "than this are deleted. Required if no default is configured."
        ),
    )
    tprune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without touching disk.",
    )
    tprune_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    args = parser.parse_args()
    if args.cmd == "health":
        _cli_health(
            json_out=args.json,
            days=args.days,
            top_k=args.top_k,
            min_applied=args.min_applied,
        )
        return
    if args.cmd == "migrate":
        if args.migrate_cmd == "origin":
            scope_repo_map: dict[str, str] = {}
            for entry in args.scope_repo:
                if "=" not in entry:
                    parser.error(f"--scope-repo expects SCOPE=URL, got: {entry!r}")
                scope, url = entry.split("=", 1)
                scope = scope.strip()
                url = url.strip()
                if not scope or not url:
                    parser.error(
                        f"--scope-repo expects non-empty SCOPE and URL, got: {entry!r}"
                    )
                scope_repo_map[scope] = url
            _cli_migrate_origin(
                dry_run=args.dry_run,
                force_repo=args.repo,
                scope_repo_map=scope_repo_map,
            )
            return
        migrate_parser.print_help()
        return
    if args.cmd == "tombstones":
        if args.tombstones_cmd == "list":
            _cli_tombstones_list(json_out=args.json, scopes=args.scope or None)
            return
        if args.tombstones_cmd == "prune":
            _cli_tombstones_prune(
                older_than_days=args.older_than,
                dry_run=args.dry_run,
                json_out=args.json,
                parser=parser,
            )
            return
        tombstones_parser.print_help()
        return

    _cli_serve()


def _cli_serve() -> None:
    """The default no-arg behaviour: run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    log.info("memory directory: %s", directory)
    log.info(
        "telemetry: %s (event log at %s/.events.jsonl)",
        "on" if config.telemetry.enabled else "off",
        directory,
    )
    log.info(
        "reminder: include the SYSTEM_PROMPT_ADDENDUM in your client's "
        "system prompt — see docs/system_prompt.md"
    )

    mcp = build_server(config=config, store=store, state=get_state())
    mcp.run("stdio")


def _cli_health(
    *,
    json_out: bool,
    days: int,
    top_k: int,
    min_applied: int | None = None,
) -> None:
    """`bettermemory health` — print the aggregate report."""
    from .health import render_json, render_text

    config = load_config()
    directory = config.resolved_directory()
    # `--min-applied` overrides the config default; fall through to the
    # configured value when the flag wasn't passed. Avoids forcing the user
    # to pass the same number to every CLI invocation.
    threshold = (
        min_applied
        if min_applied is not None
        else config.behavior.heavily_used_min_applied
    )
    report = report_for_directory(
        directory,
        window_days=days,
        heavily_used_top_k=top_k,
        heavily_used_min_applied=threshold,
    )
    sys.stdout.write(render_json(report) if json_out else render_text(report))


def _cli_tombstones_list(*, json_out: bool, scopes: list[str] | None) -> None:
    """`bettermemory tombstones list` — print removed memories."""
    import json as _json

    config = load_config()
    store = Store(config.resolved_directory())
    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    summaries = store.list_tombstones(scopes=scopes)

    if json_out:
        sys.stdout.write(
            _json.dumps(
                [
                    {
                        "id": s.id,
                        "scopes": s.scopes,
                        "summary": s.summary,
                        "created": _isoformat(s.created),
                        "removed": _isoformat(s.removed),
                        "removed_reason": s.removed_reason,
                        "removed_session": s.removed_session,
                    }
                    for s in summaries
                ],
                indent=2,
            )
            + "\n"
        )
        return

    if not summaries:
        sys.stdout.write("No tombstones.\n")
        return

    sys.stdout.write(f"Tombstones ({len(summaries)}):\n")
    for s in summaries:
        sess = s.removed_session or "<no session>"
        sys.stdout.write(
            f"  {s.id} [removed={_isoformat(s.removed)}, "
            f"session={sess}] {','.join(s.scopes)}: {s.summary}\n"
            f"    reason: {s.removed_reason}\n"
        )


def _cli_tombstones_prune(
    *,
    older_than_days: int | None,
    dry_run: bool,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory tombstones prune` — hard-delete old tombstones."""
    import json as _json
    from datetime import timedelta

    config = load_config()
    days = (
        older_than_days
        if older_than_days is not None
        else config.behavior.tombstone_retention_days
    )
    if days is None or days <= 0:
        # Hard refusal — pruning everything by accident would be a foot-gun.
        parser.error(
            "--older-than is required (no default configured). Pass an "
            "explicit cutoff in days, or set "
            "`behavior.tombstone_retention_days` in config.toml."
        )
    cutoff = timedelta(days=days)

    store = Store(config.resolved_directory())

    if dry_run:
        # Use load_tombstones to inspect; don't call prune which deletes.
        from .models import utcnow

        now = utcnow()
        candidates = [t for t in store.load_tombstones() if t.removed < (now - cutoff)]
        ids = [t.id for t in candidates]
        if json_out:
            sys.stdout.write(
                _json.dumps({"would_delete": ids, "cutoff_days": days}, indent=2) + "\n"
            )
            return
        if not ids:
            sys.stdout.write(f"No tombstones older than {days} days.\n")
            return
        sys.stdout.write(
            f"Would delete {len(ids)} tombstone(s) older than {days} days:\n"
        )
        for t in candidates:
            sys.stdout.write(
                f"  {t.id} [removed={_isoformat(t.removed)}]: {t.removed_reason}\n"
            )
        sys.stdout.write("(Dry run — re-run without --dry-run to apply.)\n")
        return

    pruned_ids = store.prune_tombstones(cutoff)
    if json_out:
        sys.stdout.write(
            _json.dumps({"deleted": pruned_ids, "cutoff_days": days}, indent=2) + "\n"
        )
        return
    if not pruned_ids:
        sys.stdout.write(f"No tombstones older than {days} days.\n")
        return
    sys.stdout.write(
        f"Deleted {len(pruned_ids)} tombstone(s) older than {days} days:\n"
    )
    for memory_id in pruned_ids:
        sys.stdout.write(f"  {memory_id}\n")


def _cli_migrate_origin(
    *,
    dry_run: bool,
    force_repo: str | None,
    scope_repo_map: dict[str, str],
) -> None:
    """`bettermemory migrate origin` — backfill origin on legacy memories."""
    from .migrate import (
        infer_origin_for_memory_dir,
        migrate_origin_in_directory,
    )

    config = load_config()
    memory_dir = config.resolved_directory()

    print(f"Scanning {memory_dir}...")
    print()

    if scope_repo_map:
        print("Routing by scope:")
        for scope, url in scope_repo_map.items():
            print(f"  {scope:<32} -> {url}")
        print()

    if force_repo is not None:
        print(f"Fallback: untagged memories -> {force_repo!r}")
    else:
        inferred = infer_origin_for_memory_dir(memory_dir)
        if scope_repo_map and inferred is None:
            print(
                "Fallback: untagged memories left alone "
                "(no --repo and no auto-inference)."
            )
        elif scope_repo_map is None or not scope_repo_map:
            if inferred is None:
                print(
                    f"  Parent of memory dir: {memory_dir.parent}\n"
                    f"  No git remote detected.\n"
                    f"\n"
                    f"This appears to be a global memory directory — "
                    f"memories here probably came from many projects, "
                    f"and tagging them all with one repo would be "
                    f"misinformation. Nothing to do.\n"
                    f"\n"
                    f"Options:\n"
                    f"  --repo <url>                       "
                    f"force-tag every memory\n"
                    f"  --scope-repo projects:foo=<url>    "
                    f"route by scope (multi)"
                )
                return
            print(f"  Inferred repo:   {inferred.repo}")
            print(f"  cwd:             {inferred.cwd}")
            print("  branch:          (left null — original branch unknown)")

    print()
    report = migrate_origin_in_directory(
        memory_dir,
        force_repo=force_repo,
        scope_repo_map=scope_repo_map or None,
        dry_run=dry_run,
    )

    print("Results:")
    print(f"  Scanned:           {report.scanned}")
    print(f"  Already had origin: {report.already_had_origin}")
    print(f"  {'Would update' if dry_run else 'Updated':<18} {report.updated}")
    if report.malformed:
        print(f"  Malformed (skipped): {len(report.malformed)}")
        for path in report.malformed[:5]:
            print(f"    - {path}")
        if len(report.malformed) > 5:
            print(f"    ... and {len(report.malformed) - 5} more")

    if dry_run and report.updated:
        print()
        print("(Dry run — no changes written. Re-run without --dry-run to apply.)")


# Re-export the prompt for consumers who import the package.
__all__ = ["build_server", "main", "SYSTEM_PROMPT_ADDENDUM"]
