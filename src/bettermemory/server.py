"""MCP server entry point and tool registration.

Six tools are exposed: memory_search, memory_show, memory_write, memory_list,
memory_remove, memory_scope_disable (plus a companion memory_scope_enable).

Each handler is thin: validate the input via the Pydantic models, call into
`store` / `search`, emit one event to the `Recorder`, return a
JSON-serializable result. The recorder is best-effort — telemetry failures
are logged but never propagate up into a tool call.
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
    is_valid_ulid,
    validate_scope,
)
from .prompts import SYSTEM_PROMPT_ADDENDUM
from .search import find_similar, search as run_search
from .session import SessionState, get_state
from .store import (
    MemoryNotFoundError,
    Store,
    TombstonedError,
)


log = logging.getLogger("bettermemory")


# ---------------------------------------------------------------------------
# Use-recording outcomes — values land verbatim in the event log so the
# health view can aggregate them. Add new outcomes by extending this set;
# don't rename existing values without a migration story.
# ---------------------------------------------------------------------------


_USE_OUTCOMES: frozenset[str] = frozenset(
    {
        "applied",       # The retrieved memory shaped the response.
        "ignored",       # Retrieved but turned out off-topic.
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

    mcp = FastMCP(
        "bettermemory",
        instructions=(
            "Local file-backed memory. Memory is OPT-IN: call memory_search "
            "only when the user references shared context you don't have, or "
            "asks 'do you remember'. Default to not retrieving."
        ),
    )

    _register_tools(
        mcp, config=config, store=store, state=state, recorder=recorder
    )
    return mcp


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
            "`relevance`, not the raw `score` — and treat \"low\" hits as "
            "probable noise unless you have a reason to use them. "
            "Pass `expand_top=True` to inline the full body of the top hit "
            "when its relevance is \"high\" — collapses the common "
            "search-then-show round trip into one call. Skip it when you "
            "only need to triage. "
            "By default (`auto_scope=True`), results are filtered to "
            "memories written from the current repository — cross-project "
            "memories are excluded. Memories written outside any repo "
            "(or before the auto-scope feature) are treated as global and "
            "always pass. Set `auto_scope=False` for cross-project queries "
            "(\"do you remember anything about X across all my projects\")."
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
        expanded_id: str | None = None
        if expand_top and out and out[0]["relevance"] == "high":
            try:
                memory = store.load_one(hits[0].id)
            except (MemoryNotFoundError, TombstonedError):
                # Race: memory was tombstoned between search and show.
                # Drop the body silently, the snippet still got returned.
                pass
            else:
                out[0]["body"] = memory.body
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
            "full body."
        ),
    )
    async def memory_show(id: str) -> dict[str, Any]:
        try:
            memory = store.load_one(id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        recorder.record("show", id=memory.id)
        return {
            "id": memory.id,
            "scopes": memory.scopes,
            "confidence": memory.confidence.value,
            "source": memory.source.value,
            "created": _isoformat(memory.created),
            "updated": _isoformat(memory.updated),
            "body": memory.body,
            "origin": _origin_to_dict(memory.origin),
        }

    # ---- memory_write ----------------------------------------------------

    @mcp.tool(
        name="memory_write",
        description=(
            "Create a new memory. Durable facts only. The tool runs a "
            "structural durability check on the body before writing: any "
            "transient-state marker (\"currently\", \"today I\", \"we just\", "
            "\"the new\", commit-SHA-like hex tokens, etc.) returns "
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
        related: list[SimilarHit] = []
        if not force:
            similar = find_similar(payload["content"], store.load_all())
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
            recorder.record(
                "write",
                status="pending",
                pending_id=pending.pending_id,
                scopes=payload["scopes"],
                forced=force,
                related=[h.id for h in related],
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
            markers_acknowledged=acknowledged,
        )
        return _committed(memory, related=related)

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
        recorder.record(
            "write_cancel", pending_id=pending_id, existed=existed
        )
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
                "memory_update needs at least one of content, scopes, "
                "or confidence"
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
                raise ValueError(
                    "scopes must contain at least one entry if provided"
                )
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
                    f"confidence must be one of "
                    f"{[c.value for c in Confidence]}"
                ) from exc

        new_body = existing.body
        if content is not None:
            new_body = content.strip() + "\n"

        merged = existing.model_copy(
            update={
                "body": new_body,
                "scopes": new_scopes,
                "confidence": new_confidence,
            }
        )
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
            "removal reason — never hard-deleted. Use when a stored fact "
            "is wrong or no longer relevant."
        ),
    )
    async def memory_remove(id: str, reason: str) -> dict[str, Any]:
        if not reason or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        try:
            tombstone_path = store.tombstone(id, reason)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        recorder.record("remove", id=id, reason=reason)
        return {
            "removed": id,
            "tombstone_path": str(tombstone_path),
        }

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
            "rates, and the scope distribution. Use this to drive curation "
            "passes — prune dead weight, refresh contradicted memories via "
            "memory_update, trim transient markers whose override rate is "
            "high. The corresponding CLI is `bettermemory health`."
        ),
    )
    async def memory_health(
        window_days: int = 30,
        heavily_used_top_k: int = 10,
    ) -> dict[str, Any]:
        report = report_for_directory(
            store.root,
            window_days=int(window_days),
            heavily_used_top_k=int(heavily_used_top_k),
        )
        return report.to_dict()

    # ---- memory_record_use ----------------------------------------------

    @mcp.tool(
        name="memory_record_use",
        description=(
            "Record how a retrieved memory was used in your response. Call "
            "this once per response that consumed memory output, with the "
            "ids you actually relied on and an outcome of \"applied\" "
            "(the memory shaped the reply), \"ignored\" (you retrieved it "
            "but it turned out off-topic), or \"contradicted\" (the user "
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
            raise ValueError(
                f"outcome must be one of {sorted(_USE_OUTCOMES)}"
            )
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
        description=(
            "Re-enable a previously disabled scope for this session."
        ),
    )
    async def memory_scope_enable(scope: str) -> dict[str, Any]:
        clean = validate_scope(scope)
        state.enable(clean)
        recorder.record("scope_enable", scope=clean)
        return {"disabled_scopes": sorted(state.disabled_scopes)}


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
        raise ValueError(
            f"source must be one of {[s.value for s in Source]}"
        ) from exc

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
) -> dict[str, Any]:
    """Serialise a freshly-written Memory into the tool response shape.

    `related` carries any medium-overlap matches that didn't block the write
    — surfaced so the caller can still consider memory_update on a similar
    existing entry, just without a hard refusal.
    """
    out: dict[str, Any] = {
        "status": "committed",
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "created": _isoformat(memory.created),
        "updated": _isoformat(memory.updated),
    }
    if related:
        out["related"] = [_similar_to_dict(h) for h in related]
    return out


def _similar_to_dict(hit: SimilarHit) -> dict[str, Any]:
    return {
        "id": hit.id,
        "scopes": hit.scopes,
        "confidence": hit.confidence.value,
        "snippet": hit.snippet,
        "similarity": hit.similarity,
        "relevance": hit.relevance,
        "created": _isoformat(hit.created),
        "updated": _isoformat(hit.updated),
    }


def _transient_to_dict(hit: TransientMatch) -> dict[str, Any]:
    """Serialize a transient-marker match for the tool response."""
    return {"marker": hit.marker, "snippet": hit.snippet}


def _isoformat(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


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
    }


def _summary_to_dict(summary: MemorySummary) -> dict[str, Any]:
    return {
        "id": summary.id,
        "scopes": summary.scopes,
        "confidence": summary.confidence.value,
        "summary": summary.summary,
        "created": _isoformat(summary.created),
        "updated": _isoformat(summary.updated),
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

    args = parser.parse_args()
    if args.cmd == "health":
        _cli_health(json_out=args.json, days=args.days, top_k=args.top_k)
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


def _cli_health(*, json_out: bool, days: int, top_k: int) -> None:
    """`bettermemory health` — print the aggregate report."""
    from .health import render_json, render_text

    config = load_config()
    directory = config.resolved_directory()
    report = report_for_directory(
        directory, window_days=days, heavily_used_top_k=top_k
    )
    sys.stdout.write(render_json(report) if json_out else render_text(report))


# Re-export the prompt for consumers who import the package.
__all__ = ["build_server", "main", "SYSTEM_PROMPT_ADDENDUM"]
