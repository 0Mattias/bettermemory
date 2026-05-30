"""memory_proposals MCP tool — review the write-reflex proposal queue.

The Stop hook (opt-in `[proposals] auto_propose`) captures durable-looking
statements from the user's messages that were never written as memories
and queues them as inert `Proposal`s. This handler is the review surface:
the model lists them and either accepts one (→ a real memory write via the
normal store path) or dismisses it. Accepting is the confirmation step —
proposals never touch the store until then, so the "writes are confirmed,
never silent" contract holds even though capture is automatic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._shared import Context, _advance_turn, _validate_write_payload
from ..models import Confidence, Source
from ..proposals import ProposalQueue

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_PROPOSALS = (
    "Review the write-reflex proposal queue — durable statements the Stop "
    "hook captured from your messages that were never saved as memories. "
    "The capture half of the self-improving loop (opt-in via [proposals] "
    "auto_propose); proposals are INERT until you act on them, so nothing "
    "is ever written without your explicit accept.\n\n"
    "`memory_scope_overview` reports `proposals_pending` so you know when "
    "the queue is non-empty and worth a look.\n\n"
    "Actions (the `action` parameter):\n"
    "- `list` (default): return all queued proposals as `{id, body, "
    "source_excerpt, suggested_category, created}`. Check each against "
    "what's already in memory before accepting.\n"
    "- `accept`: write the proposal as a real memory and remove it from "
    "the queue. Requires `proposal_id` AND `scopes` (memories need at "
    "least one scope; the queue does not guess them). `category` defaults "
    "to the proposal's `suggested_category` — override if wrong "
    "(`fact` / `user-inference` / `ambient`). The write goes through the "
    "normal store path (source=inferred) and is indexed immediately.\n"
    "- `dismiss`: drop the proposal from the queue without writing it. "
    "Requires `proposal_id`. Use for anything not worth remembering.\n\n"
    "Returns `{status, action, ...}`; `status` is one of `ok` (list), "
    "`accepted`, `dismissed`, or `not_found`."
)


async def memory_proposals(
    deps: "ToolHandlers",
    action: str = "list",
    proposal_id: str | None = None,
    scopes: list[str] | None = None,
    category: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Handler body for the `memory_proposals` MCP tool."""
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    queue = ProposalQueue(deps.store.root)
    act = (action or "list").lower()

    if act == "list":
        proposals = [p.to_dict() for p in queue.load()]
        deps.recorder.record("memory_proposals", action="list", returned=len(proposals))
        return {
            "status": "ok",
            "action": "list",
            "count": len(proposals),
            "proposals": proposals,
        }

    if act == "dismiss":
        if not proposal_id:
            raise ValueError("proposal_id is required to dismiss a proposal")
        removed = queue.remove(proposal_id)
        deps.recorder.record(
            "memory_proposals",
            action="dismiss",
            proposal_id=proposal_id,
            found=removed is not None,
        )
        if removed is None:
            return {
                "status": "not_found",
                "action": "dismiss",
                "proposal_id": proposal_id,
            }
        return {"status": "dismissed", "action": "dismiss", "proposal_id": proposal_id}

    if act == "accept":
        if not proposal_id:
            raise ValueError("proposal_id is required to accept a proposal")
        if not scopes:
            raise ValueError(
                "scopes is required to accept a proposal — a memory needs at "
                "least one scope, and the proposal queue does not guess them"
            )
        # Resolve the proposal BEFORE writing so a bad id fails cleanly,
        # and remove it only AFTER the write commits — a write that raises
        # (invalid scope/category) leaves the proposal in the queue to retry.
        match = next((p for p in queue.load() if p.id == proposal_id), None)
        if match is None:
            return {
                "status": "not_found",
                "action": "accept",
                "proposal_id": proposal_id,
            }
        cat_value = category or match.suggested_category
        # Accept must enforce the SAME write policy as memory_write — not
        # just the content-size cap but the scope-count cap AND the
        # allowed-scopes whitelist. Previously this path validated only
        # content size, so an accepted proposal could write a scope outside
        # the configured whitelist or past max_scopes_per_write (fail-open).
        # Route through the shared validator so the policy surface cannot
        # drift between write entry points; it also gives a clean error for
        # a bad category or scope instead of a bare exception.
        payload = _validate_write_payload(
            content=match.body,
            scopes=list(scopes),
            confidence=Confidence.MEDIUM.value,
            source=Source.INFERRED.value,
            category=cat_value,
            allowed_scopes=deps.config.scopes.allowed,
            max_content_bytes=deps.config.behavior.max_content_bytes,
            max_scopes_per_write=deps.config.behavior.max_scopes_per_write,
        )
        memory = deps.store.write(**payload, origin=_h.capture_origin())
        queue.remove(proposal_id)
        cat_written = (
            memory.category.value if memory.category is not None else cat_value
        )
        deps.recorder.record(
            "memory_proposals",
            action="accept",
            proposal_id=proposal_id,
            id=memory.id,
            scopes=memory.scopes,
            category=cat_written,
        )
        return {
            "status": "accepted",
            "action": "accept",
            "proposal_id": proposal_id,
            "id": memory.id,
            "scopes": memory.scopes,
            "category": cat_written,
        }

    raise ValueError(
        f"unknown action {action!r}: expected 'list', 'accept', or 'dismiss'"
    )


__all__ = ["DESC_MEMORY_PROPOSALS", "memory_proposals"]
