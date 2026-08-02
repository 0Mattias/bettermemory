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
from .write import (
    CONTENT_GATES,
    GateBundle,
    GateContext,
    Reject,
    apply_write_gates,
)
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
    "normal store path (source=inferred), is indexed immediately, and "
    "runs the SAME content gates memory_write runs: a secret-shaped "
    "token, a transient-state marker, a scope-mismatched citation, or "
    "high overlap with an existing or previously-removed memory refuses "
    "the accept with that gate's own status (`markers`/`matches` + "
    "`hint`) and leaves the proposal queued. Each refusal carries the "
    "memory_write escape hatch — `acknowledge_credential`, "
    "`acknowledge_transient`, `acknowledge_scope_mismatch`, or "
    "`force=True` for the two dedup refusals (prefer memory_update on "
    "the matched id, or memory_restore on the tombstone).\n"
    "- `dismiss`: drop the proposal from the queue without writing it. "
    "Requires `proposal_id`. Use for anything not worth remembering.\n\n"
    "Returns `{status, action, ...}`; `status` is one of `ok` (list), "
    "`accepted`, `dismissed`, `not_found`, or a gate refusal "
    "(`credential_warning`, `transient_warning`, `scope_mismatch`, "
    "`duplicate`, `previously_removed`)."
)


# The gate hints name the MCP parameter that overrides them. On this surface
# every override also has a CLI spelling, and the escape hatch shipped DEAD at
# that surface once — the refusal told the operator to pass a parameter
# `bettermemory proposals accept` could not express. Naming both keeps one
# refusal actionable from either entry point. A status absent from this map
# simply keeps the gate's hint verbatim.
_CLI_ESCAPE_FLAGS: dict[str, str] = {
    "credential_warning": "--acknowledge-credential",
    "transient_warning": "--acknowledge-transient",
    "scope_mismatch": "--acknowledge-scope-mismatch",
    "duplicate": "--force",
    "previously_removed": "--force",
}


def _gate_refusal(decision: Reject, proposal_id: str) -> dict[str, Any]:
    """Shape a gate `Reject` as a `memory_proposals` accept response.

    The gate's own body (status + markers/matches + hint) verbatim, so one
    refusal shape spans memory_write, memory_update and this surface, plus
    the `action`/`proposal_id` keys every result here carries — the caller
    needs to know WHICH still-queued entry was refused.

    `decision.event_kwargs` is deliberately dropped: no event is recorded on
    a refusal (the accept event fires only when the write lands).
    """
    response: dict[str, Any] = {
        "status": decision.response["status"],
        "action": "accept",
        "proposal_id": proposal_id,
    }
    response.update({k: v for k, v in decision.response.items() if k != "status"})
    flag = _CLI_ESCAPE_FLAGS.get(response["status"])
    hint = response.get("hint")
    if flag is not None and isinstance(hint, str):
        response["hint"] = (
            f"{hint} On `bettermemory proposals accept` the same override is {flag}."
        )
    return response


def accept_proposal(
    *,
    store: "Store",
    config: "Config",
    recorder: "Recorder",
    proposal_id: str,
    scopes: list[str],
    category: str | None = None,
    force: bool = False,
    acknowledge_credential: bool = False,
    acknowledge_transient: bool = False,
    acknowledge_scope_mismatch: bool = False,
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
    3. Run the SHARED content-gate chain (``CONTENT_GATES``) — the same
       policy object ``memory_write`` runs, not a private copy of one gate
       of it. The write-reflex captures raw user text, so an accepted
       proposal is another door onto the plain-text store; before this it
       was guarded by a hand-rolled credential scan and nothing else, so a
       transient body, a scope-mismatched citation, or a near-duplicate of
       an existing memory reached the store through the review surface
       while the same body was refused through ``memory_write``. The chain
       runs BEFORE the claim, so a hit refuses with the proposal still
       queued, returning the gate's own structured status (see
       ``_gate_refusal``). Every refusal keeps its ``memory_write`` escape
       hatch: ``acknowledge_credential`` (a proposal that DESCRIBES a
       documented public/example credential PATTERN rather than leaking a
       live secret), ``acknowledge_transient``, ``acknowledge_scope_mismatch``
       and ``force`` — which, as on ``memory_write``, bypasses BOTH dedup
       gates (the ``previously_removed`` hint offers it, so it has to work
       there too; ingest's narrower force, which keeps tombstone dedup on,
       answers to a different contract).

       ``UserClaimGate`` and ``PendingGate`` are the two gates
       ``CONTENT_GATES`` leaves out, and their exclusion is what makes this
       conversion safe: the extractor deliberately stamps explicit captures
       ("remember that I prefer X") as ``fact``, so proposal bodies match
       the user-claim shapes by construction and inheriting that gate would
       hard-refuse exactly the entries this queue exists to carry. Every
       gate that DOES run only READS the store (``load_all`` /
       ``load_tombstones``), so the module invariant — nothing writes until
       an accept lands — survives the addition.
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

    Returns a result dict (``status`` in ``{"accepted", "not_found"}`` or
    any gate refusal status: ``credential_warning``, ``transient_warning``,
    ``scope_mismatch``, ``duplicate``, ``previously_removed``). No event is
    recorded on the ``not_found`` paths or on a gate refusal — only when the
    write actually lands. Raises ``ValueError`` on a bad payload (it bubbles
    to the caller and the proposal stays queued).
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
        min_content_tokens=config.behavior.min_content_tokens,
        max_scopes_per_write=config.behavior.max_scopes_per_write,
    )
    # The content-gate chain (docstring step 3), scanned against the body
    # that would be persisted. Run BEFORE the claim so a refusal leaves the
    # proposal queued for the reviewer to edit, dismiss, or re-accept with
    # an override.
    gc = GateContext(
        payload=payload,
        force=force,
        acknowledge_transient=acknowledge_transient,
        acknowledge_scope_mismatch=acknowledge_scope_mismatch,
        # Groundedness is opt-in even on the MCP path and needs a transcript;
        # the captured `source_excerpt` is the user's own sentence, not a
        # conversation to anchor against, so the gate stays inert here.
        acknowledge_ungrounded=False,
        acknowledge_credential=acknowledge_credential,
        groundedness_check=False,
        source_transcript=None,
    )
    decision = apply_write_gates(
        GateBundle.for_store(store, config), gc, gates=CONTENT_GATES
    )
    if isinstance(decision, Reject):
        return _gate_refusal(decision, proposal_id)
    # `Pending` is unreachable: `PendingGate` is one of the two gates
    # `CONTENT_GATES` excludes by name, and accepting IS the confirmation
    # step this queue's contract is built on.
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
    # what `memory_write` records for the same escape hatch. The gate
    # populates `gc.credential_hits` even when the flag suppresses the
    # refusal, which is what makes the override countable at all.
    credentials_acknowledged = (
        [h.kind for h in gc.credential_hits]
        if gc.credential_hits and acknowledge_credential
        else []
    )
    # Same axis for the transient gate, now that it is reachable here —
    # with one asymmetry against `memory_write` worth naming, because the
    # obvious repair for it is wrong. There this field is a rollup input
    # as well as audit evidence: `health._StatsAccumulator` dispatches on
    # event KIND, and its `write` handler counts `markers` as fires and
    # `markers_acknowledged` as overrides into `MarkerStats`. This event's
    # kind is `memory_proposals`, so an accept-time override here is
    # grep-able in the log — the same standing this surface's
    # `credentials_acknowledged` has, which no rollup reads either — and
    # is absent from `MarkerStats.override_rate`.
    #
    # Keep it that way while the fires are missing. A refusal on this
    # surface records no event carrying its `markers` (`_gate_refusal`
    # drops the gate's `event_kwargs`; the accept event fires only when
    # the write lands), so the fire the write path logs when the gate
    # actually refuses has no counterpart here. Dispatching the kind for
    # its overrides alone would score a marker 1.000 where the same
    # block-then-acknowledge sequence through `memory_write` scores 0.500
    # — past the ceiling `MarkerStats.override_rate`'s own docstring
    # calibrates readers against, and reading as rubber-stamping on any
    # marker the two surfaces share. Recording the refusal's markers is
    # the prerequisite, not an optional companion.
    markers_acknowledged = (
        [h.marker for h in gc.transient_hits]
        if gc.transient_hits and acknowledge_transient
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
        forced=force,
        credentials_acknowledged=credentials_acknowledged,
        markers_acknowledged=markers_acknowledged,
    )
    return {
        "status": "accepted",
        "action": "accept",
        "proposal_id": proposal_id,
        "id": memory.id,
        "scopes": memory.scopes,
        "category": cat_written,
        "credentials_acknowledged": credentials_acknowledged,
        "markers_acknowledged": markers_acknowledged,
    }


async def memory_proposals(
    deps: "ToolHandlers",
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
        # The validate -> gate -> atomic-claim -> write contract lives in
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
                force=force,
                acknowledge_credential=acknowledge_credential,
                acknowledge_transient=acknowledge_transient,
                acknowledge_scope_mismatch=acknowledge_scope_mismatch,
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
