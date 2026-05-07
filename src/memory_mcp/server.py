"""MCP server entry point and tool registration.

Six tools are exposed: memory_search, memory_show, memory_write, memory_list,
memory_remove, memory_scope_disable (plus a companion memory_scope_enable).

Each handler is thin: validate the input via the Pydantic models, call into
`store` / `search`, return a JSON-serializable result.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config, load_config
from .models import (
    Confidence,
    MemoryHit,
    MemorySummary,
    Source,
    validate_scope,
)
from .prompts import SYSTEM_PROMPT_ADDENDUM
from .search import search as run_search
from .session import SessionState, get_state
from .store import (
    MemoryNotFoundError,
    Store,
    TombstonedError,
)


log = logging.getLogger("memory_mcp")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_server(
    *,
    config: Config | None = None,
    store: Store | None = None,
    state: SessionState | None = None,
) -> FastMCP:
    """Return a configured FastMCP instance.

    Tests pass in their own `store` and `state` to keep things hermetic.
    The real entry point in `main()` lets `load_config` resolve everything.
    """
    config = config or load_config()
    store = store or Store(config.resolved_directory())
    state = state or get_state()

    mcp = FastMCP(
        "memory-mcp",
        instructions=(
            "Local file-backed memory. Memory is OPT-IN: call memory_search "
            "only when the user references shared context you don't have, or "
            "asks 'do you remember'. Default to not retrieving."
        ),
    )

    _register_tools(mcp, config=config, store=store, state=state)
    return mcp


def _register_tools(
    mcp: FastMCP,
    *,
    config: Config,
    store: Store,
    state: SessionState,
) -> None:
    # ---- memory_search ---------------------------------------------------

    @mcp.tool(
        name="memory_search",
        description=(
            "Search stored memories. Call this only when you have reason to "
            "think the user is referencing context you don't have, or when "
            "the user explicitly asks. Default to not searching. Returns "
            "ranked hits with snippets — call memory_show for full content."
        ),
    )
    async def memory_search(
        query: str,
        scopes: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        if max_results is None:
            max_results = config.behavior.default_max_results
        max_results = max(1, min(int(max_results), 50))

        if scopes:
            scopes = [validate_scope(s) for s in scopes]

        memories = store.load_all()
        hits = run_search(
            memories,
            query,
            scopes=scopes,
            excluded_scopes=set(state.disabled_scopes),
            max_results=max_results,
            half_life_days=config.behavior.recency_boost_half_life_days,
        )
        return [_hit_to_dict(h) for h in hits]

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
        return {
            "id": memory.id,
            "scopes": memory.scopes,
            "confidence": memory.confidence.value,
            "source": memory.source.value,
            "created": _isoformat(memory.created),
            "updated": _isoformat(memory.updated),
            "body": memory.body,
        }

    # ---- memory_write ----------------------------------------------------

    @mcp.tool(
        name="memory_write",
        description=(
            "Create a new memory. Only call this for durable preferences, "
            "not transient context, and confirm with the user first. "
            "Provide non-empty scopes (e.g. ['tools', 'learning-style']). "
            "If `require_write_confirmation` is true in config, this returns "
            "{status:'pending', pending_id} and you must call "
            "memory_write_confirm(pending_id) to commit."
        ),
    )
    async def memory_write(
        content: str,
        scopes: list[str],
        confidence: str = "medium",
        source: str = "explicit-statement",
    ) -> dict[str, Any]:
        payload = _validate_write_payload(
            content=content,
            scopes=scopes,
            confidence=confidence,
            source=source,
            allowed_scopes=config.scopes.allowed,
        )

        if config.behavior.require_write_confirmation:
            pending = state.stage_write(payload)
            return {
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

        memory = store.write(**payload)
        return _committed(memory)

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
        return {"cancelled": pending_id, "existed": existed}

    # ---- memory_list -----------------------------------------------------

    @mcp.tool(
        name="memory_list",
        description=(
            "List active memories (no body content) with one-line summaries. "
            "Use this to see what's available without retrieving everything. "
            "Filter by scopes if you only care about a subset."
        ),
    )
    async def memory_list(
        scopes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if scopes:
            scopes = [validate_scope(s) for s in scopes]
        # Apply session-disabled scopes to listing too — consistency.
        excluded = set(state.disabled_scopes)
        out: list[dict[str, Any]] = []
        for summary in store.list_summaries(scopes=scopes):
            if excluded and (set(summary.scopes) & excluded):
                continue
            out.append(_summary_to_dict(summary))
        return out

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
        return {
            "removed": id,
            "tombstone_path": str(tombstone_path),
        }

    # ---- memory_scope_disable / enable -----------------------------------

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


def _committed(memory) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Serialise a freshly-written Memory into the tool response shape."""
    return {
        "status": "committed",
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "created": _isoformat(memory.created),
        "updated": _isoformat(memory.updated),
    }


def _isoformat(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _hit_to_dict(hit: MemoryHit) -> dict[str, Any]:
    return {
        "id": hit.id,
        "scopes": hit.scopes,
        "confidence": hit.confidence.value,
        "snippet": hit.snippet,
        "score": hit.score,
        "created": _isoformat(hit.created),
    }


def _summary_to_dict(summary: MemorySummary) -> dict[str, Any]:
    return {
        "id": summary.id,
        "scopes": summary.scopes,
        "confidence": summary.confidence.value,
        "summary": summary.summary,
        "created": _isoformat(summary.created),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point — `memory-mcp` runs this. Stdio transport."""
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
        "reminder: include the SYSTEM_PROMPT_ADDENDUM in your client's "
        "system prompt — see docs/system_prompt.md"
    )

    mcp = build_server(config=config, store=store, state=get_state())
    mcp.run("stdio")


# Re-export the prompt for consumers who import the package.
__all__ = ["build_server", "main", "SYSTEM_PROMPT_ADDENDUM"]
