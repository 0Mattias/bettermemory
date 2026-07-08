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
from ..credentials import find_credential_markers
from ..models import Confidence, Source
from ..proposals import ProposalQueue

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..config import Config
    from ..events import Recorder
    from ..store import Store


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
    "normal store path (source=inferred) and is indexed immediately. A "
    "proposal whose body contains a secret-shaped token is refused (the "
    "store is plain-text and `sync`'d across hosts); pass "
    "`acknowledge_credential=True` to accept anyway when the value is a "
    "documented public/example credential, mirroring the memory_write / "
    "memory_update escape hatch.\n"
    "- `dismiss`: drop the proposal from the queue without writing it. "
    "Requires `proposal_id`. Use for anything not worth remembering.\n\n"
    "Returns `{status, action, ...}`; `status` is one of `ok` (list), "
    "`accepted`, `dismissed`, or `not_found`."
)


def accept_proposal(
    *,
    store: "Store",
    config: "Config",
    recorder: "Recorder",
    proposal_id: str,
    scopes: list[str],
    category: str | None = None,
    acknowledge_credential: bool = False,
) -> dict[str, Any]:
    """Validate, atomically claim, and write one proposal as a durable memory.

    The shared core behind BOTH the ``memory_proposals(action="accept")`` MCP
    tool and the ``bettermemory proposals accept`` CLI, so the subtle
    write-policy + atomic-claim contract lives in exactly one place instead of
    drifting across two entry points. Steps:

    1. Resolve the proposal by id (clean ``not_found`` when the id is unknown).
    2. Validate the write payload through the SAME ``_validate_write_payload``
       every write entry point uses — content size, scope-count cap, AND the
       allowed-scopes whitelist — so an accepted proposal can't slip a scope
       past the policy other writes enforce. Validation runs BEFORE the claim
       so a bad scope/category raises with the proposal still in the queue
       (the caller fixes the inputs and retries); the only failure that loses
       the entry is an unexpected store error after a successful claim.
    3. Credential-scan the body that would be persisted with the SAME
       ``find_credential_markers`` the ``CredentialGate`` runs FIRST on the
       ``memory_write`` path — the write-reflex captures raw user text, so an
       accepted proposal is another door through which a secret-shaped token
       could reach the plain-text store WITHOUT ever passing the write-path
       gate. Runs BEFORE the claim, so a hit refuses with the proposal still
       queued (raises ``ValueError`` naming the detector kinds only — never
       the value, exactly as the write/update paths redact it). Passing
       ``acknowledge_credential=True`` bypasses this refusal, mirroring the
       identically-named escape hatch on ``memory_write`` / ``memory_update``
       for the rare legitimate case (a proposal that DESCRIBES a documented
       public/example credential PATTERN rather than leaking a live secret).
    4. Atomically CLAIM the proposal — ``ProposalQueue.remove`` re-checks it
       still exists under the queue's per-file flock and hands it to the single
       racer that wins, so a concurrent double-accept can't write twice.
    5. Write the durable memory through the normal store path.
    6. Record the accept event through ``recorder`` — HERE, at the single
       choke point, not per entry surface. Every caller (the
       ``memory_proposals`` MCP tool AND the ``bettermemory proposals
       accept`` CLI) therefore logs an accepted claim exactly once, and a
       forced ``acknowledge_credential`` override (detector kinds only,
       never the value) can't slip through an entry point that forgot to
       layer its own event on top — the CLI did exactly that before the
       recording moved here. Callers must NOT record a second accept event.

    Returns a result dict (``status`` in ``{"accepted", "not_found"}``). No
    event is recorded on either ``not_found`` path or on a refusal — only
    when the write actually lands. Raises ``ValueError`` on a bad payload (it
    bubbles to the caller and the proposal stays queued).
    """
    from .. import _handlers as _h

    queue = ProposalQueue(store.root)
    # Resolve the proposal BEFORE writing so a bad id fails cleanly.
    match = next((p for p in queue.load() if p.id == proposal_id), None)
    if match is None:
        return {"status": "not_found", "action": "accept", "proposal_id": proposal_id}
    cat_value = category or match.suggested_category
    payload = _validate_write_payload(
        content=match.body,
        scopes=list(scopes),
        confidence=Confidence.MEDIUM.value,
        source=Source.INFERRED.value,
        category=cat_value,
        allowed_scopes=config.scopes.allowed,
        max_content_bytes=config.behavior.max_content_bytes,
        max_scopes_per_write=config.behavior.max_scopes_per_write,
    )
    # Credential gate — mirror `CredentialGate`, which the memory_write path
    # runs FIRST, so a secret-shaped token captured by the write-reflex can't
    # slip onto the plain-text (sync'd) store by being ACCEPTED rather than
    # written. Scan the body that would be persisted; a hit refuses BEFORE the
    # claim so the proposal stays queued, and the error names the detector
    # `kind`s only — the value is never echoed, same as the write/update paths.
    credential_hits = find_credential_markers(payload["content"])
    if credential_hits and not acknowledge_credential:
        kinds = sorted({h.kind for h in credential_hits})
        raise ValueError(
            f"proposal {proposal_id} body contains a secret-shaped token "
            f"({', '.join(kinds)}) — this store is plain-text and `sync` "
            "pushes it across hosts via git, so the accept is refused. Edit "
            "the proposal to describe the secret without embedding it, dismiss "
            "it, or pass acknowledge_credential=True (the "
            "--acknowledge-credential flag on the CLI) if the value is a "
            "documented public/example credential (mirrors the memory_write / "
            "memory_update escape hatch). The value is redacted from this "
            "error regardless."
        )
    # Atomically CLAIM under the queue lock before the durable write — the
    # idempotency guard against a concurrent double-accept.
    claimed = queue.remove(proposal_id)
    if claimed is None:
        # Lost the race: another accept already claimed and wrote this
        # proposal. Do not write a duplicate.
        return {"status": "not_found", "action": "accept", "proposal_id": proposal_id}
    memory = store.write(**payload, origin=_h.capture_origin())
    cat_written = memory.category.value if memory.category is not None else cat_value
    # Kind only, never the value: the detector kinds a forced
    # `acknowledge_credential` override bypassed (empty when none) — a
    # too-loose detector / high override rate stays observable, mirroring
    # what `memory_write` records for the same escape hatch.
    credentials_acknowledged = (
        [h.kind for h in credential_hits]
        if credential_hits and acknowledge_credential
        else []
    )
    # Single choke point for the accept audit event (docstring step 6): the
    # write landed, so record it here for EVERY surface. Callers must not
    # record a second accept event — the MCP handler double-logged the
    # forced-override kinds when it layered its own record on top.
    recorder.record(
        "memory_proposals",
        action="accept",
        proposal_id=proposal_id,
        id=memory.id,
        scopes=memory.scopes,
        category=cat_written,
        credentials_acknowledged=credentials_acknowledged,
    )
    return {
        "status": "accepted",
        "action": "accept",
        "proposal_id": proposal_id,
        "id": memory.id,
        "scopes": memory.scopes,
        "category": cat_written,
        "credentials_acknowledged": credentials_acknowledged,
    }


async def memory_proposals(
    deps: "ToolHandlers",
    action: str = "list",
    proposal_id: str | None = None,
    scopes: list[str] | None = None,
    category: str | None = None,
    acknowledge_credential: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Handler body for the `memory_proposals` MCP tool."""
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
        # The validate -> atomic-claim -> write contract lives in
        # `accept_proposal` so this MCP tool and the `bettermemory proposals
        # accept` CLI share ONE implementation (no policy drift between
        # entry points). A bad scope/category raises ValueError from there
        # with the proposal still queued. The accept audit event — including
        # the forced `acknowledge_credential` override kinds — is recorded
        # INSIDE `accept_proposal` (its single choke point, step 6 of its
        # docstring), only when the write actually lands and never on the
        # not_found paths. Do NOT record another accept event here: that
        # double-logs the override on the MCP path while the CLI path
        # relies on the core's record being the only one.
        try:
            result = accept_proposal(
                store=deps.store,
                config=deps.config,
                recorder=deps.recorder,
                proposal_id=proposal_id,
                scopes=scopes,
                category=category,
                acknowledge_credential=acknowledge_credential,
            )
        except OSError as exc:
            # A disk-level failure (ENOSPC/EIO/EACCES). Translate to
            # ValueError so the MCP tool boundary returns a clean
            # structured error instead of letting the bare OSError leak
            # its absolute store path to the client — matching the sibling
            # lifecycle handlers (remove/restore/verify/rename_scope) and
            # the CLI twin `bettermemory proposals accept`. The OSError can
            # surface from EITHER the atomic queue claim (queue.remove,
            # which rewrites the queue file) OR the durable store.write
            # after it — so do NOT assert the entry is definitively gone;
            # tell the caller to re-check before retrying.
            raise ValueError(
                f"failed to accept proposal {proposal_id}: {exc} "
                "(it may have been removed from the queue — re-check with "
                'memory_proposals(action="list") before retrying)'
            ) from exc
        return result

    raise ValueError(
        f"unknown action {action!r}: expected 'list', 'accept', or 'dismiss'"
    )


__all__ = ["DESC_MEMORY_PROPOSALS", "accept_proposal", "memory_proposals"]
