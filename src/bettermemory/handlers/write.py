"""memory_write MCP tool — orchestrator + WriteGate strategy.

Pre-Round-2 ``memory_write`` was a 356-line method with six sequential
guard blocks inline (durability / scope-mismatch / groundedness /
dedup-active / dedup-tombstone / pending). Each block built a
short-circuit response dict and recorded its own event before
returning. The function read as a single long ladder; understanding any
one gate required scrolling past the others.

Round 2 extracts each gate into a small class (``WriteGate``
subclasses below) whose ``evaluate(payload, ...)`` method returns one
of:

- ``Reject`` — short-circuit with this response dict and recorder
  event. The orchestrator records the event and returns.
- ``Pending`` — stage the write through the SessionState and return a
  pending response.
- ``Continue`` — gate passed; move to the next.

The orchestrator (``memory_write`` below) holds the dependency
references, runs the gates in order, and falls through to the actual
``Store.write`` on the first gate that returns ``Continue`` through
the whole chain. Each gate stays under 40 lines and reads like a
self-contained policy decision; the orchestrator is the readable
sequence.

WriteGate decision: kept as a single file (this one) rather than
``handlers/write/<gate>.py``. The six gates are small (10-40 lines each),
share half a dozen helpers, and reading them one after the other in
declaration order matches how they fire at runtime — splitting them
across six files would hide that runtime order behind a directory
listing.

Includes ``memory_write_confirm`` / ``memory_write_cancel`` because
those tools complete the pending-write lifecycle this module owns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..durability import find_transient_markers
from ..models import Category, SimilarHit
from ..scope_match import (
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)
from ..search import find_similar, find_similar_tombstones
from ..session import SessionState
from ._shared import (
    Context,
    _AMBIENT_LONG_BODY_WORDS,
    _advance_turn,
    _maybe_attach_curation_hint,
    _validate_write_payload,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


# ---------------------------------------------------------------------------
# Description constants
# ---------------------------------------------------------------------------


DESC_MEMORY_WRITE = (
    "Create a new memory. Call PROACTIVELY when something durable "
    "enters the conversation — don't wait for 'remember that.' "
    "Triggers: user states a preference (→ "
    "category='user-inference'); a project decision the user "
    "concurred with (→ category='fact'); a tool / infrastructure / "
    "config fact; a unit of work finishes with a why git won't "
    "capture. The structural guardrails below catch bad writes; "
    "aggressive writing is safe.\n\n"
    "Parameters:\n"
    "- `content`: the memory body.\n"
    "- `scopes`: non-empty list. Avoid the catch-all 'general'; "
    "prefer narrow tags like `tools`, `infrastructure`, "
    "`projects:<name>`, `learning-style`.\n"
    "- `category` (default 'fact'): one of `fact`, "
    "`user-inference`, `ambient`.\n"
    "  - `fact`: project / infra / reference / tooling. Commits "
    "immediately (unless `require_write_confirmation`).\n"
    "  - `user-inference`: claims ABOUT THE USER. Always returns "
    "{status:'pending', pending_id} regardless of config — ask "
    "the user in plain language, then memory_write_confirm or "
    "memory_write_cancel. Misattribution sticks; user gets the "
    "veto.\n"
    "  - `ambient`: atmospheric context that shapes replies "
    "without being cited. Commits like fact but excluded from "
    "dead-weight curation; long bodies (>500 words) attach a "
    "non-blocking `ambient_body_long` warning.\n"
    "- `confidence` ('low' / 'medium' / 'high'), `source` "
    "('explicit-statement' / 'inferred').\n"
    "- `groundedness_check=True` + `source_transcript`: optional "
    "gate. Sentences with <30% token overlap to the transcript "
    "return {status:'ungrounded', claims:[…]}. Override via "
    "`acknowledge_ungrounded=True` when you have grounding sources "
    "outside the transcript (file reads, tool results). Off by "
    "default; opt in for a paper trail.\n\n"
    "Return statuses:\n"
    "- `committed` — write succeeded; payload carries the new id "
    "and `related` medium-overlap matches.\n"
    "- `transient_warning` — durability marker detected "
    "('currently', 'today I', 'we just', commit-SHA-like tokens, "
    "etc.). Extract the level-up durable form (the decision, the "
    "why) or pass `acknowledge_transient=True` (rare).\n"
    "- `duplicate` — content dedup fired. Prefer memory_update on "
    "the matched id; pass `force=True` only when the new memory "
    "is meaningfully different.\n"
    "- `previously_removed` — overlap with a tombstone; inspect "
    "`removed_reason`. If the rejection still applies, drop the "
    "write; if the fact is now correct, memory_restore the "
    "tombstone instead of a parallel entry.\n"
    "- `scope_mismatch` — body cites a project the declared "
    "scopes don't cover. Re-scope or pass "
    "`acknowledge_scope_mismatch=True`.\n"
    "- `pending` — `category='user-inference'` or "
    "`require_write_confirmation`. `pending_reason` distinguishes.\n"
    "- `ungrounded` — groundedness gate fired.\n\n"
    "A `committed` or `memory_write_confirm` response may inline a "
    "one-shot per-session `curation_hint` block when "
    "`dead_weight + drifted + endorsement_debt` pressure crosses "
    "the configured threshold. Shape: `{pressure, threshold, "
    "counts: {dead_weight, drifted, endorsement_debt}, message}`. "
    "Passive notification — call `memory_health` for full buckets, "
    "`memory_remove` / `memory_verify` to resolve."
)


DESC_MEMORY_WRITE_CONFIRM = (
    "Commit a memory_write that returned status='pending'. "
    "Pass the pending_id from that response. Pending writes expire "
    "after 1 hour; the confirm call will tell you which case fired "
    "(expired vs. never-existed)."
)


DESC_MEMORY_WRITE_CANCEL = (
    "Drop a pending memory_write without committing. "
    "Pass the pending_id from the original write response. "
    "Pending writes expire after 1 hour; if the TTL elapsed (or the "
    "id never existed) the call returns `existed=False`."
)


# ---------------------------------------------------------------------------
# WriteGate strategy
# ---------------------------------------------------------------------------


@dataclass
class GateContext:
    """Bundle of inputs every gate evaluates against.

    The orchestrator builds one of these per call (post-payload-
    validation, post-origin-capture) and threads it through the gate
    chain. Mutable state (`related`, `removed_related`,
    `transient_hits`) is set by earlier gates so later gates can
    surface their findings on the eventual response — `pending`
    needs the related lists, the commit path needs `transient_hits`
    so the override-rate event field can carry the acknowledged
    markers.
    """

    payload: dict[str, Any]
    force: bool
    acknowledge_transient: bool
    acknowledge_scope_mismatch: bool
    acknowledge_ungrounded: bool
    groundedness_check: bool
    source_transcript: str | None
    # Outputs the gates accumulate as they pass — read by later gates
    # or the final commit step.
    transient_hits: list[Any] = None  # type: ignore[assignment]
    related: list[SimilarHit] = None  # type: ignore[assignment]
    removed_related: list[SimilarHit] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.transient_hits is None:
            self.transient_hits = []
        if self.related is None:
            self.related = []
        if self.removed_related is None:
            self.removed_related = []


@dataclass
class Continue:
    """Gate passed — move to the next gate, or commit if last."""


@dataclass
class Reject:
    """Gate refused — short-circuit with this response.

    `event_kwargs` go straight into ``recorder.record("write", …)``
    so the audit log captures the rejection cause; `response` is the
    dict the handler returns to the caller.
    """

    response: dict[str, Any]
    event_kwargs: dict[str, Any]


@dataclass
class Pending:
    """Special-case ``Continue`` that stages the write through the
    SessionState rather than committing inline."""

    pending_reason: str


GateResult = Continue | Reject | Pending


class WriteGate:
    """Common base; subclasses override ``evaluate``."""

    def evaluate(self, deps: "ToolHandlers", gc: GateContext) -> GateResult:
        raise NotImplementedError


class TransientGate(WriteGate):
    """Durability check — reject bodies with transient-state markers
    unless `acknowledge_transient`.

    Runs FIRST: a transient body shouldn't become a duplicate of an
    existing transient memory, since the right move is to fix the
    body rather than route to memory_update on an unsalvageable
    parent. Catch transience before dedup so the rejection happens
    on the most actionable axis.
    """

    def evaluate(self, deps: "ToolHandlers", gc: GateContext) -> GateResult:
        gc.transient_hits = find_transient_markers(gc.payload["content"])
        if not gc.transient_hits or gc.acknowledge_transient:
            return Continue()
        return Reject(
            response={
                "status": "transient_warning",
                "markers": [
                    deps.responses.transient_to_dict(h) for h in gc.transient_hits
                ],
                "hint": (
                    "The body contains transient-state markers that won't "
                    "be true in a week. Either rephrase to the durable "
                    "level-up version (extract the architectural decision, "
                    "the why, what-was-built — discard the timestamp/state) "
                    "or pass acknowledge_transient=True if the marker is "
                    "genuinely durable in context."
                ),
            },
            event_kwargs={
                "status": "transient_warning",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "markers": [h.marker for h in gc.transient_hits],
            },
        )


class ScopeMismatchGate(WriteGate):
    """Reject bodies whose path / project-name citations don't match
    the declared scope list (unless `acknowledge_scope_mismatch`)."""

    def evaluate(self, deps: "ToolHandlers", gc: GateContext) -> GateResult:
        if gc.acknowledge_scope_mismatch:
            return Continue()
        existing_memories = deps.store.load_all()
        mismatch = detect_scope_mismatch(
            body=gc.payload["content"],
            declared_scopes=gc.payload["scopes"],
            project_scopes=collect_project_scopes(existing_memories),
            project_roots=collect_project_roots(existing_memories),
        )
        if not mismatch.has_mismatch:
            return Continue()
        return Reject(
            response={
                "status": "scope_mismatch",
                "matches": [m.to_dict() for m in mismatch.matches],
                "suggested_scopes": list(mismatch.suggested_scopes),
                "hint": (
                    "The body cites paths or project names that suggest "
                    "this memory belongs to a different scope. Either "
                    "add one of `suggested_scopes` to the declared "
                    "scope list, or pass acknowledge_scope_mismatch=True "
                    "if the cross-reference is intentional (e.g. an "
                    "infrastructure note that mentions multiple "
                    "projects by design)."
                ),
            },
            event_kwargs={
                "status": "scope_mismatch",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "suggested_scopes": list(mismatch.suggested_scopes),
                "mismatch_kinds": [m.kind for m in mismatch.matches],
            },
        )


class GroundednessGate(WriteGate):
    """Sentence-level overlap against `source_transcript` — fires only
    when `groundedness_check=True` and a transcript is provided.

    The HaluMem-style write-time grounding check: sentences that
    don't anchor to the conversation come back as "ungrounded". Closes
    the hallucinate-at-write-time failure mode common to systems that
    auto-extract memories from conversation.
    """

    def evaluate(self, deps: "ToolHandlers", gc: GateContext) -> GateResult:
        if not gc.groundedness_check:
            return Continue()
        if gc.source_transcript is None or gc.acknowledge_ungrounded:
            return Continue()
        from ..groundedness import check_groundedness

        ungrounded = check_groundedness(gc.payload["content"], gc.source_transcript)
        if not ungrounded:
            return Continue()
        return Reject(
            response={
                "status": "ungrounded",
                "claims": [c.to_dict() for c in ungrounded],
                "hint": (
                    "The body contains sentences that don't share enough "
                    "vocabulary with the source transcript to count as "
                    "grounded — the model may have hallucinated them, "
                    "or paraphrased so heavily that the audit trail is "
                    "lost. Either rephrase to keep the load-bearing "
                    "tokens close to the transcript, or pass "
                    "`acknowledge_ungrounded=True` if you have other "
                    "grounding sources (a file read, a tool result) "
                    "that aren't represented in this transcript."
                ),
            },
            event_kwargs={
                "status": "ungrounded",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "ungrounded_count": len(ungrounded),
            },
        )


class DedupActiveGate(WriteGate):
    """Content dedup against the active set. High overlap → reject as
    duplicate (the right move is memory_update on the matched id);
    medium overlap → record as `related` for the eventual response.

    Skipped when `force=True`.
    """

    def evaluate(self, deps: "ToolHandlers", gc: GateContext) -> GateResult:
        if gc.force:
            return Continue()
        semantic_model = deps._semantic_model_factory(deps.config)
        high_threshold = (
            deps.config.behavior.semantic_high_threshold
            if deps.config.behavior.semantic_dedup
            else None
        )
        medium_threshold = (
            deps.config.behavior.semantic_medium_threshold
            if deps.config.behavior.semantic_dedup
            else None
        )
        similar = find_similar(
            gc.payload["content"],
            deps.store.load_all(),
            semantic_model=semantic_model,
            high_threshold=high_threshold,
            medium_threshold=medium_threshold,
        )
        high = [h for h in similar if h.relevance == "high"]
        if high:
            return Reject(
                response={
                    "status": "duplicate",
                    "matches": [deps.responses.similar_to_dict(h) for h in high],
                    "hint": (
                        "An existing memory has high content overlap with "
                        "this write. Prefer memory_update on the matched "
                        "id over creating a parallel entry. Pass force=True "
                        "if the new memory is meaningfully different."
                    ),
                },
                event_kwargs={
                    "status": "duplicate",
                    "scopes": gc.payload["scopes"],
                    "forced": False,
                    "matches": [h.id for h in high],
                },
            )
        gc.related = [h for h in similar if h.relevance == "medium"]
        return Continue()


class DedupTombstoneGate(WriteGate):
    """Tombstone-aware dedup. High overlap with a removed memory →
    `previously_removed` (caller can memory_restore the tombstone
    rather than write a parallel entry); medium → `removed_related`
    for the response.

    Skipped when `force=True`.
    """

    def evaluate(self, deps: "ToolHandlers", gc: GateContext) -> GateResult:
        if gc.force:
            return Continue()
        semantic_model = deps._semantic_model_factory(deps.config)
        high_threshold = (
            deps.config.behavior.semantic_high_threshold
            if deps.config.behavior.semantic_dedup
            else None
        )
        medium_threshold = (
            deps.config.behavior.semantic_medium_threshold
            if deps.config.behavior.semantic_dedup
            else None
        )
        tombstone_similar = find_similar_tombstones(
            gc.payload["content"],
            deps.store.load_tombstones(),
            semantic_model=semantic_model,
            high_threshold=high_threshold,
            medium_threshold=medium_threshold,
        )
        high_removed = [h for h in tombstone_similar if h.relevance == "high-removed"]
        if high_removed:
            return Reject(
                response={
                    "status": "previously_removed",
                    "removed_matches": [
                        deps.responses.similar_to_dict(h) for h in high_removed
                    ],
                    "hint": (
                        "A previously-removed memory has high content overlap "
                        "with this write. Inspect each `removed_reason` — if "
                        "the rejection still applies, drop the write; if the "
                        "fact is now correct, call memory_restore(id) on the "
                        "tombstone instead of writing a parallel entry. Pass "
                        "force=True to bypass when the new memory is "
                        "meaningfully different from the removed one."
                    ),
                },
                event_kwargs={
                    "status": "previously_removed",
                    "scopes": gc.payload["scopes"],
                    "forced": False,
                    "removed_matches": [h.id for h in high_removed],
                },
            )
        gc.removed_related = [
            h for h in tombstone_similar if h.relevance == "medium-removed"
        ]
        return Continue()


class PendingGate(WriteGate):
    """Stage the write through the SessionState when either the global
    config flag (`require_write_confirmation`) OR
    `category=='user-inference'` requires it. User-inference is
    structurally enforced regardless of config: misattribution sticks
    and the user gets the veto."""

    def evaluate(self, deps: "ToolHandlers", gc: GateContext) -> GateResult:
        category_enum: Category = gc.payload["category"]
        if deps.config.behavior.require_write_confirmation:
            return Pending(pending_reason="config")
        if category_enum == Category.USER_INFERENCE:
            return Pending(pending_reason="user-inference")
        return Continue()


# Order matters: transient before dedup so the writer isn't routed to
# memory_update on a transient parent; scope-mismatch before dedup so
# the writer doesn't get a duplicate hit on a memory tagged for a
# different scope; groundedness before dedup because a hallucinated
# write being a "duplicate" of a real one is misleading; dedup before
# pending so the user-inference confirmation flow doesn't ask about
# a write we'd already reject. PendingGate is last because everything
# else either rejects or accepts.
_WRITE_GATES: tuple[WriteGate, ...] = (
    TransientGate(),
    ScopeMismatchGate(),
    GroundednessGate(),
    DedupActiveGate(),
    DedupTombstoneGate(),
    PendingGate(),
)


# ---------------------------------------------------------------------------
# Orchestrator: memory_write
# ---------------------------------------------------------------------------


async def memory_write(
    deps: "ToolHandlers",
    content: str,
    scopes: list[str],
    confidence: str = "medium",
    source: str = "explicit-statement",
    force: bool = False,
    acknowledge_transient: bool = False,
    acknowledge_scope_mismatch: bool = False,
    acknowledge_ungrounded: bool = False,
    category: str = "fact",
    groundedness_check: bool = False,
    source_transcript: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Validate the payload, run the gate chain, and either commit or
    short-circuit per the first gate that rejects."""
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    payload = _validate_write_payload(
        content=content,
        scopes=scopes,
        confidence=confidence,
        source=source,
        allowed_scopes=deps.config.scopes.allowed,
        category=category,
        max_content_bytes=deps.config.behavior.max_content_bytes,
        max_scopes_per_write=deps.config.behavior.max_scopes_per_write,
    )

    # Origin is captured before the gate chain so it's part of the
    # payload that flows into either staging or the direct write path.
    # We never persist origin for a rejection — the early return
    # below short-circuits before any disk I/O.
    payload["origin"] = _h.capture_origin()

    gc = GateContext(
        payload=payload,
        force=force,
        acknowledge_transient=acknowledge_transient,
        acknowledge_scope_mismatch=acknowledge_scope_mismatch,
        acknowledge_ungrounded=acknowledge_ungrounded,
        groundedness_check=groundedness_check,
        source_transcript=source_transcript,
    )

    pending_decision: Pending | None = None
    for gate in _WRITE_GATES:
        result = gate.evaluate(deps, gc)
        if isinstance(result, Reject):
            deps.recorder.record("write", **result.event_kwargs)
            return result.response
        if isinstance(result, Pending):
            pending_decision = result
            break

    # Capture which markers (if any) were overridden by
    # `acknowledge_transient` — feeds the override-rate signal in the
    # event log so we can tell whether a marker is producing too many
    # false positives.
    acknowledged = (
        [h.marker for h in gc.transient_hits]
        if gc.transient_hits and acknowledge_transient
        else []
    )

    if pending_decision is not None:
        return _stage_pending(
            deps,
            state,
            payload=payload,
            pending_reason=pending_decision.pending_reason,
            related=gc.related,
            removed_related=gc.removed_related,
            forced=force,
            acknowledged=acknowledged,
        )

    response = _commit_write(
        deps,
        payload=payload,
        related=gc.related,
        removed_related=gc.removed_related,
        forced=force,
        acknowledged=acknowledged,
    )
    _maybe_attach_curation_hint(response, deps, state)
    return response


def _stage_pending(
    deps: "ToolHandlers",
    state: SessionState,
    *,
    payload: dict[str, Any],
    pending_reason: str,
    related: list[SimilarHit],
    removed_related: list[SimilarHit],
    forced: bool,
    acknowledged: list[str],
) -> dict[str, Any]:
    """Stage the write through the SessionState, record the pending
    event, and return the pending response shape."""
    category_enum: Category = payload["category"]
    pending = state.stage_write(payload)
    hint = (
        "User-inference category — ask the user in plain "
        "language ('want me to remember that you prefer X?') "
        "and only then call memory_write_confirm(pending_id), "
        "or memory_write_cancel(pending_id) if they decline."
        if pending_reason == "user-inference"
        else (
            "Confirm with memory_write_confirm(pending_id) or "
            "drop with memory_write_cancel(pending_id)."
        )
    )
    response: dict[str, Any] = {
        "status": "pending",
        "pending_id": pending.pending_id,
        "pending_reason": pending_reason,
        "preview": {
            "content": payload["content"],
            "scopes": payload["scopes"],
            "confidence": payload["confidence"].value,
            "source": payload["source"].value,
            "category": category_enum.value,
        },
        "hint": hint,
    }
    if related:
        response["related"] = [deps.responses.similar_to_dict(h) for h in related]
    if removed_related:
        response["removed_related"] = [
            deps.responses.similar_to_dict(h) for h in removed_related
        ]
    deps.recorder.record(
        "write",
        status="pending",
        pending_id=pending.pending_id,
        pending_reason=pending_reason,
        category=category_enum.value,
        scopes=payload["scopes"],
        forced=forced,
        related=[h.id for h in related],
        removed_related=[h.id for h in removed_related],
        markers_acknowledged=acknowledged,
    )
    return response


def _commit_write(
    deps: "ToolHandlers",
    *,
    payload: dict[str, Any],
    related: list[SimilarHit],
    removed_related: list[SimilarHit],
    forced: bool,
    acknowledged: list[str],
) -> dict[str, Any]:
    """Persist the memory, record the commit event, return the
    committed response. Surfaces the ambient long-body warning as a
    non-blocking advisory when applicable."""
    category_enum: Category = payload["category"]
    memory = deps.store.write(**payload)
    warnings: list[str] = []
    if (
        category_enum == Category.AMBIENT
        and len(memory.body.split()) > _AMBIENT_LONG_BODY_WORDS
    ):
        warnings.append("ambient_body_long")
    deps.recorder.record(
        "write",
        status="committed",
        id=memory.id,
        category=category_enum.value,
        scopes=memory.scopes,
        confidence=memory.confidence.value,
        source=memory.source.value,
        forced=forced,
        related=[h.id for h in related],
        removed_related=[h.id for h in removed_related],
        markers_acknowledged=acknowledged,
        warnings=warnings,
    )
    return deps.responses.committed(
        memory,
        related=related,
        removed_related=removed_related,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# memory_write_confirm / memory_write_cancel
# ---------------------------------------------------------------------------


async def memory_write_confirm(
    deps: "ToolHandlers", pending_id: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    pending = state.take_pending(pending_id)
    if pending is None:
        # Distinguish "expired" from "never existed" so the model
        # can offer to re-stage the write rather than just retrying
        # with the same id. The 1h TTL is short enough that a long
        # human absence (lunch break, overnight think) is the most
        # common cause; before 2.6.8 the error was indistinguishable
        # from a typo, and the eviction was silent.
        if state.was_recently_expired(pending_id):
            raise ValueError(
                f"pending write {pending_id!r} expired before "
                "confirmation (the 1-hour TTL elapsed). The proposed "
                "memory was not saved. Re-stage with memory_write to "
                "create a fresh pending id."
            )
        raise ValueError(
            f"no pending write with id {pending_id!r} (it may have "
            "been already committed or never existed)"
        )
    memory = deps.store.write(**pending.payload)
    # If this pending write originated from `episode_promote`, delete
    # the source episode now — the durable memory is the authoritative
    # artifact and leaving the journal entry behind would survive past
    # confirmation as a duplicate. The link was stashed at staging
    # time by the promote handler; consume it (pop) so a redundant
    # later call doesn't try to delete twice.
    promo = state.take_promotion_episode(pending_id)
    if promo is not None:
        # Local import to break the cycle (episode_promote also imports
        # `memory_write` from this module).
        from .episode_promote import _delete_source_episode

        ep_session_id, ep_id = promo
        _delete_source_episode(deps, ep_session_id, ep_id)
    deps.recorder.record(
        "write_confirm",
        pending_id=pending_id,
        id=memory.id,
        scopes=memory.scopes,
    )
    response = deps.responses.committed(memory)
    _maybe_attach_curation_hint(response, deps, state)
    return response


async def memory_write_cancel(
    deps: "ToolHandlers", pending_id: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    existed = state.cancel_pending(pending_id)
    # Drop the promotion linkage if there is one, but DON'T delete the
    # source episode — cancel is the user saying "not yet", so the
    # caller should be able to fix the wording and re-promote from the
    # same journal entry.
    state.discard_promotion_episode(pending_id)
    deps.recorder.record("write_cancel", pending_id=pending_id, existed=existed)
    return {"cancelled": pending_id, "existed": existed}


__all__ = [
    "DESC_MEMORY_WRITE",
    "DESC_MEMORY_WRITE_CANCEL",
    "DESC_MEMORY_WRITE_CONFIRM",
    "memory_write",
    "memory_write_cancel",
    "memory_write_confirm",
]
