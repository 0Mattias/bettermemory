"""memory_write MCP tool: the orchestrator of the gate chain in write_gates.py.

Pre-Round-2 ``memory_write`` was a 356-line method with sequential
guard blocks inline (durability / scope-mismatch / dedup-active /
dedup-tombstone). Each block built a
short-circuit response dict and recorded its own event before
returning. The function read as a single long ladder; understanding any
one gate required scrolling past the others.

Round 2 extracts each gate into a small class (``WriteGate``
subclasses, in ``write_gates.py``) whose ``evaluate(payload, ...)``
method returns one of:

- ``Reject`` — short-circuit with this response dict and recorder
  event. The orchestrator records the event and returns.
- ``Continue`` — gate passed; move to the next.

The orchestrator (``memory_write`` below) holds the dependency
references, runs the gates in order, and, once every gate has returned
``Continue``, hands the write to the review (``bettermemory.review``):
the first call returns the related statements and the questions and
writes nothing, and the re-sent call with answers is linked, filed,
refused or committed. While the review's thresholds are unset the write
commits straight after the gates, as it did before the review. Each gate
stays under 40 lines and reads like a self-contained policy decision; the
orchestrator is the readable sequence.

WriteGate decision: kept as a single file (``write_gates.py``) rather than
``handlers/write/<gate>.py``. The gates are small (10-40 lines each),
share half a dozen helpers, and reading them one after the other in
declaration order matches how they fire at runtime — splitting them
one-per-file would hide that runtime order behind a directory
listing. That file sits apart from this one to keep each module under
the package's 2,000-line bound (ENG-96); this module imports it, never
the reverse, and re-exports the names callers import from here.

"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..conflicts import ConflictCandidate, ConflictQueue, _pair_id
from ..models import (
    Category,
    Memory,
    SimilarHit,
    generate_ulid,
    is_valid_ulid,
    snippet_for,
    utcnow,
)
from ..search import _pairwise_content_jaccard, raw_content_tokens
from .. import capture as _capture
from .. import identity
from .. import policy as _policy
from .. import review as _review
from .. import standing as _standing
from .. import supersession as _supersession
from ..session import SessionState
from ..store import (
    MemoryNotFoundError,
    StandingAnswer,
    TombstonedError,
    error_code_name,
)
from ..supersession import SupersessionMatch, SupersessionReport
from . import _guard
from ._shared import (
    Context,
    _AMBIENT_LONG_BODY_WORDS,
    _advance_turn,
    _validate_declared_claims,
    _validate_write_payload,
)

# The gate chain lives in `write_gates`, which imports nothing from here.
# Every name a caller reads stays importable from here under the path it
# had before it moved. The plain import holds the names only this module
# reads; the `name as name` form marks a name kept for a caller outside
# this module (a public name `__all__` omits, or a private name another
# module or a test reads), which the type checkers would otherwise see as
# not exported.
from .write_gates import GateDeps, Reject
from .write_gates import (
    _WRITE_GATES as _WRITE_GATES,
    CaptureRulesGate as CaptureRulesGate,
    CredentialGate as CredentialGate,
    DedupActiveGate as DedupActiveGate,
    DedupTombstoneGate as DedupTombstoneGate,
    GateBundle as GateBundle,
    GateContext as GateContext,
    PreviouslyUndoneGate as PreviouslyUndoneGate,
    ScopeMismatchGate as ScopeMismatchGate,
    TransientGate as TransientGate,
    UserClaimGate as UserClaimGate,
    _find_user_claims as _find_user_claims,
    apply_write_gates as apply_write_gates,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..admission import Admission
    from ..location import Caller

log = logging.getLogger("bettermemory.handlers.write")


# ---------------------------------------------------------------------------
# Description constants
# ---------------------------------------------------------------------------


DESC_MEMORY_WRITE = (
    "Store a durable fact; call it proactively. `scopes`: narrow tags "
    "(projects:<name>, tools), never general. `category`: fact, "
    "user-inference (about the user) or ambient (context, never cited). "
    "Gates refuse transient state, secrets, control tokens, user claims "
    "filed as fact, scope mismatch and duplicates; each names its fix. "
    "`claims`: path or path::symbol bindings watched for drift. "
    "`supersedes`: ids this replaces."
)


# ---------------------------------------------------------------------------
# Orchestrator: memory_write
# ---------------------------------------------------------------------------


def _corroborate_duplicate(
    deps: ToolHandlers, state: SessionState, result: Reject
) -> None:
    """Record a recurrence on the memory a duplicate write matched.

    The counter measures INDEPENDENT re-entries of a claim, so the bump
    is once per (memory, session), and only the TOP high-overlap match is
    credited (a write that grazes three near-duplicates is one recurrence
    of one claim, not three). The store decides the once
    (`Store.record_corroboration` with the session): its check and its
    write are one transaction under the write lock, so two parallel
    duplicate writes of one session, on two tool workers, corroborate
    once, and the second answers `corroboration_recorded: false`.

    Best-effort by contract: the rejection response the model needs is
    already built, and a telemetry bump must never turn a clean
    "duplicate" answer into an error — store races (concurrent
    tombstone), a busy store and size-cap refusals log at WARNING and
    drop. On success the response gains `corroboration_recorded: true` +
    `corroborations` (the new total), so the model knows the recurrence
    was captured and doesn't force-write out of capture anxiety; the
    reject event gains `corroborated_id` for the audit trail.
    """
    matches = result.response.get("matches") or []
    if not matches or not isinstance(matches[0], dict):
        return
    top_id = matches[0].get("id")
    if not isinstance(top_id, str) or not top_id:
        return
    try:
        bumped = deps.store.record_corroboration(top_id, session=state.session_id)
    except Exception as exc:  # noqa: BLE001 — telemetry must not break the reject
        log.warning("corroboration bump for %s failed: %s", top_id, exc)
        return
    if bumped is None:
        result.response["corroboration_recorded"] = False
        return
    result.response["corroboration_recorded"] = True
    result.response["corroborations"] = bumped.corroborations
    result.event_kwargs["corroborated_id"] = top_id


@dataclass(frozen=True)
class _CaptureWrite:
    """A capture write admitted to its batch: the batch, the session
    evidence its member row records, and the deferred review it carries
    when it commits with relations pending."""

    batch: _capture.Batch
    harness_session: str | None
    server_session: str | None
    joined_by: str
    review: dict[str, Any] | None = None


# A capture's refusals, by status, answerable without the flags a capture
# ignores: the general hints offer `force` or an acknowledge flag, and a
# model that took one would be refused again. Each says what to do inside
# the capture instead, which for a body the store holds is to skip it
# (rule 9: a capture does not update). The review's own secret and
# restates refusals share the gates' statuses and take the same answers.
_CAPTURE_REFUSAL_HINTS: dict[str, str] = {
    "credential_warning": (
        "Not written. The body holds a secret-shaped value (an API key, "
        "private key, token or password). Leave the value out of the body "
        "and write it again with the same source, or skip it."
    ),
    "control_token_warning": (
        "Not written. The body holds a model's chat-template control token. "
        "Write the token's bare name without its delimiters, or leave it "
        "out; for a `Human:` or `Assistant:` line (kind `anthropic`), quote "
        "the line or name the speaker another way. Write it again with the "
        "same source, or skip it."
    ),
    "transient_warning": (
        "Not written. The body holds state that will not be true in a week. "
        "Write only the durable part, without the time-bound words, and with "
        "the same source, or skip it."
    ),
    "user_claim_warning": (
        "Not written. The body reads as a claim about the user. When it "
        "records a project or tool fact, rephrase it with that subject "
        'instead of the user ("the tool needs ...", not "the user '
        'wants ..."). When it is about the user, write it again with '
        'category="user-inference" (rule 8). Either way, keep the same source.'
    ),
    # The body names another project's name or path, so the memory takes
    # that project's scope too, or loses the reference when the claim does
    # not need it.
    "scope_mismatch": (
        "The body cites paths or project names that suggest this memory "
        "belongs to another scope as well. Add the scope of the project the "
        "body names from `suggested_scopes` to the declared list, or leave "
        "that reference out of the body when the claim does not need it, and "
        "write it again with the same source."
    ),
    "duplicate": (
        "Not written. The store already holds this memory. Skip it: a "
        "capture does not update stored memories."
    ),
    "previously_removed": (
        "Not written. A memory saying this was removed before. Skip it."
    ),
}


# Inside a capture a refusal ends the capture: a write re-sent without the
# tag would skip the capture rules, the undone gate and the membership an
# undo reaches. Only an unknown token may be a write that was never one.
_END_CAPTURE = (
    "Do not write it without the capture: source. End the capture: write "
    "nothing more, stop, and tell the user in one short line what you saved."
)
_CAPTURE_TOKEN_HINTS: dict[str, str] = {
    _capture.TOKEN_UNKNOWN: (
        "Not written. No capture batch has this token. Copy the token exactly "
        "from the capture instruction. If you were not asked to capture, drop "
        "the capture: source."
    ),
    _capture.BATCH_CLOSED: (
        "Not written. This capture batch has settled and takes no more writes. "
        + _END_CAPTURE
    ),
    _capture.BATCH_EXPIRED: (
        "Not written. This capture batch expired before it settled. " + _END_CAPTURE
    ),
    "capture_session_mismatch": (
        "Not written. This capture batch belongs to another conversation. "
        + _END_CAPTURE
    ),
}


def _admit_capture(
    deps: ToolHandlers, state: SessionState, token: str, scopes: list[str]
) -> tuple[dict[str, Any] | None, _CaptureWrite | None]:
    """Resolve a capture write's token: the refusal to return, or the
    batch it joins with the session evidence. The token is the binding;
    the harness session must also be the batch's Stop session or one the
    /clear bridge joins to it (`capture.joined_by`)."""
    batch, why = _capture.batch_for_token(deps.store, token)
    joined: str | None = None
    caller = identity.current()
    harness_session = caller.harness_session if caller is not None else None
    if batch is not None:
        joined = _capture.joined_by(
            deps.store,
            batch,
            harness_session=harness_session,
            server_session=state.session_id,
        )
        if joined is None:
            why = "capture_session_mismatch"
    if batch is None or joined is None:
        rule = why or _capture.TOKEN_UNKNOWN
        deps.recorder.record(
            "write",
            status="capture_refused",
            scopes=scopes,
            forced=False,
            capture_rule=rule,
            **({"capture_batch": batch.batch} if batch is not None else {}),
        )
        return {
            "status": "capture_refused",
            "rule": rule,
            "hint": _CAPTURE_TOKEN_HINTS[rule],
        }, None
    return None, _CaptureWrite(
        batch=batch,
        harness_session=harness_session,
        server_session=state.session_id,
        joined_by=joined,
    )


def _check_write_policy(
    deps: ToolHandlers,
    *,
    force: bool,
    acknowledge_transient: bool,
    acknowledge_scope_mismatch: bool,
    acknowledge_credential: bool,
    acknowledge_user_claim: bool,
) -> None:
    """Take the call's force and acknowledge flags; return None, raising a policy refusal."""
    # `[policy]` (policy.py): a flag the install forbids refuses the whole
    # call, before anything is validated or written. Checked after the
    # capture block, whose flags are already cleared, so a capture is
    # unchanged by any policy. A no-op with both switches off. The refusal
    # leaves its audit row (`events.AUDIT_STATUSES`) before it is raised.
    try:
        _policy.check_overrides(
            deps.config,
            "memory_write",
            force=force,
            acknowledge={
                "credential": acknowledge_credential,
                "transient": acknowledge_transient,
                "scope_mismatch": acknowledge_scope_mismatch,
                "user_claim": acknowledge_user_claim,
            },
        )
    except _policy.PolicyRefusal as refused:
        deps.recorder.record("write", **refused.event_fields())
        raise


def _write_payload(
    deps: ToolHandlers,
    *,
    content: str,
    scopes: list[str],
    confidence: str,
    source: str,
    category: str,
    captured: _CaptureWrite | None,
    claims: list[str] | None,
    supersedes: list[str] | None,
) -> dict[str, Any]:
    """Take the call's arguments; return the validated payload with origin, actor, claims, links."""
    from .. import _handlers as _h

    payload = _validate_write_payload(
        content=content,
        scopes=scopes,
        confidence=confidence,
        source=source,
        allowed_scopes=deps.config.scopes.allowed,
        category=category,
        max_content_bytes=deps.config.behavior.max_content_bytes,
        min_content_tokens=deps.config.behavior.min_content_tokens,
        max_scopes_per_write=deps.config.behavior.max_scopes_per_write,
        capture=captured is not None,
    )
    if captured is not None:
        payload["capture"] = captured

    # Origin is captured before the gate chain: it is part of the payload
    # the commit writes, and its cwd is where the writing caller is
    # resolved (`_payload_caller`), whose admission filters the review's
    # related set. A refusal or a review writes no record, so the origin
    # of a call that does not commit never reaches the disk.
    payload["origin"] = _h.capture_origin()
    # The actor rides beside the origin: who is writing, resolved at the
    # handler entry (`identity.bind` inside `sessions.for_request`) from
    # the request's headers, the server environment, the `initialize`
    # handshake's clientInfo and the attested principal. None when the
    # caller declared nothing, and then the record carries no block.
    payload["actor"] = identity.current_actor()

    # Declared claims are oracle-checked HERE, against the origin just
    # captured — before the gate chain, because a false claim has no
    # acknowledge_* escape by design (the entire value of the field is
    # that a stored claim was true at declaration; an override would
    # mint the one thing the read side is promised cannot exist). The
    # canonical rendered forms land in the payload, which is what the
    # commit writes. A reviewed write checks them again on the re-sent
    # call, since the review keeps no state between the two calls.
    if claims:
        origin = payload["origin"]
        payload["claims"] = _validate_declared_claims(
            claims,
            worktree_root=origin.worktree_root if origin else None,
            surface="memory_write",
            refusal=origin.git_refusal if origin else None,
        )

    # A declared `supersedes` list is checked here, ahead of the gates,
    # for the same reason claims are: a target that is not an active
    # memory is a defect the writer can only fix now, and the link dicts
    # ride the payload to the commit, which sets them unchanged.
    if supersedes:
        payload["links"] = _validate_declared_supersedes(deps, supersedes)
    return payload


def _refuse_gated_write(
    deps: ToolHandlers,
    state: SessionState,
    decision: Reject,
    captured: _CaptureWrite | None,
) -> dict[str, Any]:
    """Take a gate's refusal, amending its event and response in place; return the response."""
    # Recurrence-as-evidence: a duplicate rejection IS the stored
    # claim re-entering a conversation. Record the corroboration
    # on the matched memory (once per session per memory) before
    # the reject event goes out, so the event carries the id. Not
    # for a capture: the model restating its own context is not an
    # independent recurrence, and the bump would change a record an
    # undo of the batch could not restore.
    if decision.response.get("status") == "duplicate" and captured is None:
        _corroborate_duplicate(deps, state, decision)
    if captured is not None:
        decision.event_kwargs.setdefault("capture_batch", captured.batch.batch)
        _capture_refusal_hint(decision.response)
    deps.recorder.record("write", **decision.event_kwargs)
    return decision.response


def _commit_args(
    gc: GateContext,
    *,
    force: bool,
    acknowledge_transient: bool,
    acknowledge_scope_mismatch: bool,
    acknowledge_credential: bool,
    acknowledge_user_claim: bool,
) -> _CommitArgs:
    """Take the passed gates' context and the flags; return what the commit is handed."""
    # Capture which markers (if any) were overridden by
    # `acknowledge_transient` — feeds the override-rate signal in the
    # event log so we can tell whether a marker is producing too many
    # false positives.
    acknowledged = (
        [h.marker for h in gc.transient_hits]
        if gc.transient_hits and acknowledge_transient
        else []
    )
    # Parallel to `acknowledged`: which credential detectors were overridden
    # by `acknowledge_credential`, recorded (kind only) so a high override
    # rate flags a too-loose detector. Never the value.
    credentials_acknowledged = (
        [h.kind for h in gc.credential_hits]
        if gc.credential_hits and acknowledge_credential
        else []
    )
    # Same axis again for the user-claim gate: the phrases a caller
    # overrode. This gate's phrase list is the kind that only ever gets
    # revisited on override-rate evidence, so the evidence has to exist.
    user_claims_acknowledged = (
        [h.phrase for h in gc.user_claim_hits]
        if gc.user_claim_hits and acknowledge_user_claim
        else []
    )
    # And the scope-mismatch gate's: the mismatch kinds (`project_name`,
    # `project_root`) the flag waved through, never the cited text. Before
    # ENG-108 the gate stepped aside on the flag without looking, so an
    # acknowledged mismatch left no evidence and no audit row.
    scope_mismatches_acknowledged = (
        [m.kind for m in gc.scope_mismatch_hits]
        if gc.scope_mismatch_hits and acknowledge_scope_mismatch
        else []
    )

    return _CommitArgs(
        related=gc.related,
        removed_related=gc.removed_related,
        forced=force,
        acknowledged=acknowledged,
        credentials_acknowledged=credentials_acknowledged,
        user_claims_acknowledged=user_claims_acknowledged,
        scope_mismatches_acknowledged=scope_mismatches_acknowledged,
    )


async def memory_write(
    deps: ToolHandlers,
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
    review: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Validate the payload, run the gate chain, then the review: return
    the first gate's refusal, a review to answer, or the review's
    decision. With the review's thresholds unset, a write that passes the
    gates commits."""
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    # A capture write (`source="capture:<token>"`) joins the open batch its
    # token names, or is refused before anything else runs. On a capture
    # `force` and every acknowledge flag are ignored: no flag passes the
    # capture bar, and a captured memory never skips a gate.
    captured: _CaptureWrite | None = None
    token = _capture.token_of(source)
    if token is not None:
        refused, captured = _admit_capture(deps, state, token, scopes)
        if refused is not None:
            return refused
        force = acknowledge_transient = acknowledge_scope_mismatch = False
        acknowledge_credential = acknowledge_user_claim = False
    _check_write_policy(
        deps,
        force=force,
        acknowledge_transient=acknowledge_transient,
        acknowledge_scope_mismatch=acknowledge_scope_mismatch,
        acknowledge_credential=acknowledge_credential,
        acknowledge_user_claim=acknowledge_user_claim,
    )
    payload = _write_payload(
        deps,
        content=content,
        scopes=scopes,
        confidence=confidence,
        source=source,
        category=category,
        captured=captured,
        claims=claims,
        supersedes=supersedes,
    )

    gc = GateContext(
        payload=payload,
        force=force,
        acknowledge_transient=acknowledge_transient,
        acknowledge_scope_mismatch=acknowledge_scope_mismatch,
        acknowledge_credential=acknowledge_credential,
        acknowledge_user_claim=acknowledge_user_claim,
        capture=captured.batch if captured is not None else None,
    )

    decision = apply_write_gates(deps, gc)
    if decision is not None:
        return _refuse_gated_write(deps, state, decision, captured)

    commit = _commit_args(
        gc,
        force=force,
        acknowledge_transient=acknowledge_transient,
        acknowledge_scope_mismatch=acknowledge_scope_mismatch,
        acknowledge_credential=acknowledge_credential,
        acknowledge_user_claim=acknowledge_user_claim,
    )

    # The review runs after every gate, so a duplicate, a previously
    # removed body or a regex secret is refused before any review is built.
    # With the thresholds unset no review is issued and `review` is not
    # read: the write commits as it did before the review existed.
    tau = _review.current_tau()
    if tau is None:
        return _commit_write(
            deps, payload=payload, commit=commit, active_snapshot=gc.active_snapshot
        )
    response = _reviewed_write(
        deps,
        state,
        payload=payload,
        commit=commit,
        review=review,
        tau=tau,
        active_snapshot=gc.active_snapshot,
    )
    if captured is not None:
        _capture_refusal_hint(response)
    return response


def _capture_refusal_hint(response: dict[str, Any]) -> None:
    """Swap a capture refusal's hint for the one a capture can follow."""
    hint = _CAPTURE_REFUSAL_HINTS.get(response.get("status", ""))
    if hint is not None:
        response["hint"] = hint


# ---------------------------------------------------------------------------
# The write review
# ---------------------------------------------------------------------------
#
# `bettermemory.review` holds the questions, the related set and the pure
# decision; this section is the daemon's side of it: the review response,
# the review_issued row, the refusals and the commit. The review keeps no
# state between the two calls. The re-sent call is checked by coverage:
# its answers must name every related id recomputed for the content as
# re-sent, and a missing id gets a follow-up review for the missing ids
# only, chained to the first by `parent_review_id`.


@dataclass
class _CommitArgs:
    """What the gates hand the commit: the dedup gate's medium-overlap
    lists and the acknowledged markers for the commit event."""

    related: list[SimilarHit]
    removed_related: list[SimilarHit]
    forced: bool
    acknowledged: list[str]
    credentials_acknowledged: list[str]
    user_claims_acknowledged: list[str]
    scope_mismatches_acknowledged: list[str]


_REVIEW_SECRET_HINT = (
    "Not written. The review answered that the body carries a credential, "
    "key, token or password value, or part of one. Nothing of the body was "
    "kept, and acknowledge_credential does not apply to this answer."
)

_REVIEW_UNSAFE_HINT = (
    "Not written. The review answered that following this write could weaken "
    "security, skip a safety or review step, expose data or credentials, or "
    "mislead the user. Store it as a fact about its source (who said it, "
    "where), or drop it."
)

_RELATIONS_PENDING_HINT = (
    "Written. Once the capture is saved, answer relations_pending for every "
    'write in one call: memory_admin(action="relations", relations={<this '
    "id>: {<related id>: " + _review.LABEL_CHOICES + "}})."
)

_REVIEW_STANDING_HINT = (
    "Not written. Each memory under `standing` directs work in its scope "
    "until it is replaced, and your answers conflict with it or replace it. "
    "Rewrite the body to comply with it; or, if the user changed it in this "
    "conversation, answer update for it and add review.said, the user's "
    "words as they typed them."
)

_REVIEW_RESTATES_HINT = (
    "Not written. The review answered that a stored memory already says "
    "this; the recurrence is recorded on it. Use memory_update on the "
    "matched id to change what it says."
)


def _review_hint(review_id: str, *, follow_up: bool) -> str:
    """The review response's hint: what to send back, and only here, so
    the answer format is paid for per review and not per session."""
    lead = "Not written. "
    if follow_up:
        lead += (
            "The related set changed since your answers: keep them and add "
            "an answer for each id below. "
        )
    return (
        lead + "Answer in `review` and re-send this same call, with the same "
        "arguments and acknowledge flags: "
        f'review={{"review_id": "{review_id}", "relation": {{<id>: '
        f"{_review.LABEL_CHOICES}}} for every related id ({{}} when "
        'none), "unsafe": <0 to 1>, "secret": <0 to 1>, "standing": <0 to 1>}. '
        "unsafe, secret and standing are the probability that each question's "
        "statement holds."
    )


def _actor_fields() -> dict[str, str]:
    """The client and model a review row is counted under, `none` for an
    undeclared one."""
    actor = identity.current_actor()
    client = actor.client if actor is not None else None
    model = actor.model if actor is not None else None
    return {
        "client": client or _review.UNDECLARED,
        "model": model or _review.UNDECLARED,
    }


def _record_review_issued(
    deps: ToolHandlers,
    *,
    review_id: str,
    related_ids: list[str],
    pre_answered: bool,
    parent_review_id: str | None,
    response_chars: int | None,
    sent_review_id: str | None = None,
    deferred: bool = False,
) -> None:
    """The review_issued row: ids, the actor's client and model, and the
    chain fields. Never a body. `sent_review_id` is a review_id the call
    carried that the log holds no open chain for, kept for correlation.
    `deferred` marks a capture write that committed with relations pending
    for the batch's closing call."""
    fields: dict[str, Any] = {
        "review_id": review_id,
        "related": related_ids,
        **_actor_fields(),
        "pre_answered": pre_answered,
        "parent_review_id": parent_review_id,
    }
    if deferred:
        fields["deferred"] = True
    if response_chars is not None:
        fields["response_chars"] = response_chars
    if sent_review_id is not None:
        fields["sent_review_id"] = sent_review_id
    deps.recorder.record(_review.REVIEW_ISSUED, **fields)


def _issue_review(
    deps: ToolHandlers,
    related: _review.RelatedSet,
    *,
    parent_review_id: str | None,
    sent_review_id: str | None = None,
) -> dict[str, Any]:
    """Write nothing; return the related statements and the questions."""
    review_id = generate_ulid()
    response = deps.responses.review(
        review_id=review_id,
        related=[entry.to_dict() for entry in related.entries],
        questions=_review.questions_payload(),
        hint=_review_hint(
            review_id,
            follow_up=parent_review_id is not None or sent_review_id is not None,
        ),
    )
    _record_review_issued(
        deps,
        review_id=review_id,
        related_ids=related.ids,
        pre_answered=False,
        parent_review_id=parent_review_id,
        response_chars=len(json.dumps(response, ensure_ascii=False)),
        sent_review_id=sent_review_id,
    )
    return response


def _review_chain(
    deps: ToolHandlers,
    answers: _review.ReviewAnswers,
    related: _review.RelatedSet,
) -> tuple[_review.Chain | None, str | None]:
    """Take the answers and the related set; return the open chain and a stray review_id."""
    # Only a review_id whose chain the log holds open takes the call's
    # answer. One the log does not hold (fabricated, or from another
    # store) or one a write row already answered would leave a write no
    # chain counts, so the call is handled as if it carried none: a fresh
    # root review, or a pre-answered decision, with the sent id kept on
    # the review_issued row for correlation (DESIGN item 2).
    chain: _review.Chain | None = None
    stray_id: str | None = None
    allowed = set(related.ids)
    if answers.review_id is not None:
        found = _review.find_chain(deps.store, answers.review_id)
        allowed |= found.shown
        if found.open:
            chain = found
        else:
            stray_id = answers.review_id
    _review.check_answered_ids(answers, allowed)
    return chain, stray_id


def _decided_review_id(
    deps: ToolHandlers,
    answers: _review.ReviewAnswers,
    related: _review.RelatedSet,
    *,
    chain: _review.Chain | None,
    stray_id: str | None,
    deferred: bool,
) -> str:
    """Take the answers and their chain; return the review_id the decision is filed under."""
    if chain is not None and answers.review_id is not None:
        return answers.review_id
    # Pre-answered: decided without an open review. Recorded as a
    # review so the counters see it.
    review_id = generate_ulid()
    _record_review_issued(
        deps,
        review_id=review_id,
        related_ids=related.ids,
        pre_answered=True,
        parent_review_id=None,
        response_chars=None,
        sent_review_id=stray_id,
        deferred=deferred,
    )
    return review_id


def _standing_guard(
    deps: ToolHandlers,
    caller: Caller,
    answers: _review.ReviewAnswers,
    related: _review.RelatedSet,
    declared_ids: list[str],
    missing: list[str],
) -> tuple[list[str], list[str], _guard.Words | None]:
    """Take the review's answers and ids; return the standing ids, those replaced, the words."""
    # The standing records the write could conflict with or replace, of
    # every record it touches, admitted or not (unit RS-F7 part 5: the
    # guard reads every record, since admission decides delivery, not who
    # may change a record).
    candidates = list(
        dict.fromkeys([*related.ids, *declared_ids, *related.cue_supersedes])
    )
    standing_ids = _standing.guarded_ids(deps.store, candidates)
    # The standing records the write replaces rest on the user's words
    # (`_guard.read_words`). A capture write's pending ids may be answered
    # update at its closing call, so they are read now too. A conflict
    # with a standing record refuses whatever said holds, so said is then
    # not read.
    conflicted = any(
        answers.relation.get(i) == _review.LABEL_CONFLICT for i in standing_ids
    )
    potential = _review.replaced_ids(answers, related, declared_ids)
    potential += [i for i in missing if i not in potential]
    rests = [i for i in potential if i in standing_ids]
    words = (
        _guard.read_words(deps, caller, answers.said, name="review.said")
        if rests and not conflicted
        else None
    )
    return standing_ids, rests, words


def _review_fields(
    payload: dict[str, Any],
    answers: _review.ReviewAnswers,
    related: _review.RelatedSet,
    decision: _review.ReviewDecision,
    *,
    review_id: str,
    tau: _review.Tau,
    captured: _CaptureWrite | None,
    deferred: bool,
    missing: list[str],
    declared_ids: list[str],
    standing_ids: list[str],
    said: bool,
) -> tuple[
    dict[str, Any],
    list[_review.RelatedEntry],
    StandingAnswer | None,
    _CaptureWrite | None,
]:
    """Take the decision and the payload; return the review fields, pending, standing, capture."""
    standing: StandingAnswer | None = None
    capture: _CaptureWrite | None = None
    review_fields: dict[str, Any] = {
        "review_id": review_id,
        "review_answers": answers.to_event(),
        "review_action": decision.action,
    }
    if answers.standing is not None and decision.action == _review.ACTION_COMMIT:
        # The fourth answer is stored beside the record, in its own
        # transaction (unit RS-F7 part 1): the record is never written
        # without the answer its review gave. `_reviewed_write` puts it on
        # the payload.
        standing = _standing.answer_of(answers.standing, _standing.SOURCE_REVIEW)
    if decision.action == _review.ACTION_COMMIT and decision.restated:
        # A commit that also restates (unit D1b): the ids only, kept on the
        # log so these commits can be counted. They get no corroboration.
        review_fields["review_restated"] = list(decision.restated)
    pending: list[_review.RelatedEntry] = []
    if deferred and captured is not None and decision.action == _review.ACTION_COMMIT:
        pending = list(related.only(missing).entries)
        review_fields["relations_pending"] = missing
        capture = replace(
            captured,
            review=_capture.deferred_review(
                review_id=review_id,
                unsafe=answers.unsafe,
                secret=answers.secret,
                tau=tau,
                relation=answers.relation,
                related=[(e.id, e.claim_sized) for e in related.entries],
                cue_linked=[i for i in related.cue_supersedes if i in related.ids],
                claim_sized=_supersession.claim_sized(payload["content"]),
                declared=declared_ids,
                pending=missing,
                standing=standing_ids,
                said=said,
            ),
        )
    return review_fields, pending, standing, capture


def _review_refusal(
    deps: ToolHandlers,
    state: SessionState,
    *,
    payload: dict[str, Any],
    commit: _CommitArgs,
    related: _review.RelatedSet,
    decision: _review.ReviewDecision,
    review_fields: dict[str, Any],
    admission: Admission,
) -> dict[str, Any] | None:
    """Take the review's decision; return the refusal it calls for, or None for a commit."""
    if decision.action == _review.ACTION_SECRET:
        deps.recorder.record(
            "write",
            status="credential_warning",
            scopes=payload["scopes"],
            forced=False,
            credential_kinds=["review"],
            **review_fields,
        )
        return {
            "status": "credential_warning",
            "markers": [{"kind": "review", "snippet": None}],
            "hint": _REVIEW_SECRET_HINT,
        }

    if decision.action == _review.ACTION_UNSAFE:
        deps.recorder.record(
            "write",
            status="instruction_warning",
            scopes=payload["scopes"],
            forced=False,
            **review_fields,
        )
        return {"status": "instruction_warning", "hint": _REVIEW_UNSAFE_HINT}

    if decision.action == _review.ACTION_STANDING:
        deps.recorder.record(
            "write",
            status=_review.ACTION_STANDING,
            scopes=payload["scopes"],
            forced=False,
            standing=list(decision.standing),
            **review_fields,
        )
        return {
            "status": _review.ACTION_STANDING,
            "standing": _standing_rows(deps, related, decision.standing, admission),
            "hint": _REVIEW_STANDING_HINT,
        }

    if decision.action == _review.ACTION_RESTATES:
        by_id = {entry.id: entry for entry in related.entries}
        reject = Reject(
            response={
                "status": "duplicate",
                "evidence": "review",
                "matches": [
                    {
                        "id": memory_id,
                        "scopes": list(by_id[memory_id].scopes),
                        "updated": by_id[memory_id].updated,
                    }
                    for memory_id in decision.restated
                ],
                "hint": _REVIEW_RESTATES_HINT,
            },
            event_kwargs={
                "status": "duplicate",
                "evidence": "review",
                "scopes": payload["scopes"],
                "forced": commit.forced,
                "matches": list(decision.restated),
                **review_fields,
            },
        )
        # The dedup gate's recurrence-as-evidence, recorded the same way,
        # and skipped for a capture the same way.
        if payload.get("capture") is None:
            _corroborate_duplicate(deps, state, reject)
        deps.recorder.record("write", **reject.event_kwargs)
        return reject.response
    return None


def _reviewed_write(
    deps: ToolHandlers,
    state: SessionState,
    *,
    payload: dict[str, Any],
    commit: _CommitArgs,
    review: dict[str, Any] | None,
    tau: _review.Tau,
    active_snapshot: list[Memory] | None,
) -> dict[str, Any]:
    """The review after the gates: issue one, or decide on its answers.

    The cue code runs once, inside `related_for`, and its report is the
    one the commit uses. A teaching error raises `ValueError` and writes
    nothing.
    """
    from ..admission import Admission
    from ..membership import REVIEW_STRATA

    answers = _review.parse_answers(review)
    active = active_snapshot if active_snapshot is not None else deps.store.load_all()
    declared_ids = [str(link["target_id"]) for link in payload.get("links") or []]
    # The related set is the writing caller's REVIEW_STRATA admission
    # (design 3.5), resolved once here and kept in the payload for the
    # commit, which homes the record by the same resolution.
    caller = _payload_caller(deps, payload)
    admission = Admission.of(REVIEW_STRATA, caller)
    related = _review.related_for(
        deps.store,
        payload["content"],
        behavior=deps.config.behavior,
        excluded_scopes=frozenset(state.disabled_scopes),
        admission=admission,
        active=active,
        exclude_ids=declared_ids,
    )
    if answers is None:
        return _issue_review(deps, related, parent_review_id=None)

    chain, stray_id = _review_chain(deps, answers, related)

    # A score at or above its threshold refuses whatever the relation
    # answers say, so it needs no coverage; anything else is decided only
    # when every related id is answered. A capture write that carries its
    # scores without a review_id is the exception: it commits now, and the
    # ids it left unanswered wait for the batch's closing call.
    refused = answers.secret >= tau.secret or answers.unsafe >= tau.unsafe
    missing = [i for i in related.ids if i not in answers.relation]
    captured: _CaptureWrite | None = payload.get("capture")
    deferred = (
        bool(missing)
        and not refused
        and captured is not None
        and answers.review_id is None
    )
    if missing and not refused and not deferred:
        return _issue_review(
            deps,
            related.only(missing),
            parent_review_id=chain.root if chain is not None else None,
            sent_review_id=stray_id,
        )

    review_id = _decided_review_id(
        deps, answers, related, chain=chain, stray_id=stray_id, deferred=deferred
    )
    standing_ids, rests, words = _standing_guard(
        deps, caller, answers, related, declared_ids, missing
    )
    decision = _review.apply_review(
        answers,
        related,
        tau=tau,
        write_claim_sized=_supersession.claim_sized(payload["content"]),
        write_supersession=deps.config.behavior.write_supersession,
        declared_ids=declared_ids,
        standing_ids=standing_ids,
        said=words is not None,
    )
    review_fields, pending, standing, capture = _review_fields(
        payload,
        answers,
        related,
        decision,
        review_id=review_id,
        tau=tau,
        captured=captured,
        deferred=deferred,
        missing=missing,
        declared_ids=declared_ids,
        standing_ids=standing_ids,
        said=words is not None,
    )
    # The commit reads both from the payload: the review's standing answer,
    # stored beside the record, and the capture with its deferred review.
    if standing is not None:
        payload["standing"] = standing
    if capture is not None:
        payload["capture"] = capture
    refusal = _review_refusal(
        deps,
        state,
        payload=payload,
        commit=commit,
        related=related,
        decision=decision,
        review_fields=review_fields,
        admission=admission,
    )
    if refusal is not None:
        return refusal

    response = _commit_write(
        deps,
        payload=payload,
        commit=commit,
        active_snapshot=active,
        report=related.report,
        decision=decision,
        review_fields=review_fields,
        pending=pending,
    )
    if words is not None:
        # The standing memories the words were given for: the ones the
        # write replaces now, and a capture write's pending ones that its
        # closing call may replace on the same words.
        _guard.record(deps, "memory_write", rests, words)
        if not words.checked:
            response["said_checked"] = False
    return response


def _standing_rows(
    deps: ToolHandlers,
    related: _review.RelatedSet,
    ids: tuple[str, ...],
    admission: Admission,
) -> list[dict[str, Any]]:
    """Each standing record a refusal names, quoted: as the review showed
    it, or, for one it did not show (declared, or a cue target past the
    cap), as `standing.refusal_rows` does, which quotes no text of a record
    the caller is not admitted to."""
    by_id = {entry.id: entry for entry in related.entries}
    others = {
        row["id"]: row
        for row in _standing.refusal_rows(
            deps.store, [i for i in ids if i not in by_id], admission
        )
    }
    rows: list[dict[str, Any]] = []
    for memory_id in ids:
        entry = by_id.get(memory_id)
        if entry is not None:
            rows.append(
                {
                    "id": entry.id,
                    "scopes": list(entry.scopes),
                    "updated": entry.updated,
                    "text": entry.text,
                }
            )
        elif memory_id in others:
            rows.append(others[memory_id])
    return rows


# ---------------------------------------------------------------------------
# Write-time supersession
# ---------------------------------------------------------------------------


# A writer consolidating several stale notes into one may name them all;
# the detector's own cap (`supersession.MAX_LINKS_PER_WRITE`) is lower
# because it has no such intent to read.
MAX_DECLARED_SUPERSEDES = 16

_CONFLICTS_FILED_HINT = (
    "Each pair under `conflicts_filed` disagrees with this write on a value "
    "and nothing in the body says which side is current. "
    'memory_admin(action="conflicts") lists them; confirm or dismiss there.'
)


@dataclass
class SupersessionOutcome:
    """What the persist step did about supersession, for the event and
    the response. `declared` holds the targets the writer named in
    `supersedes=`; `detected` the matches `supersession.detect_supersession`
    linked on its own; `conflicts` the `(pair id, match)` rows filed for
    `memory_conflicts`. The review's share: `reviewed` the targets a
    review `update` answer linked, `extended` the targets its `extends`
    answers linked (unit RS-F7 part 5), `update_suggested` the update
    answers that did not link, `review_filed` the `(pair id, target,
    detector)` rows its conflict and disagreement answers filed, and
    `cue_dropped` the cue code's targets whose links the review's
    disagreement dropped (unit D1c)."""

    declared: list[str] = field(default_factory=list)
    detected: list[SupersessionMatch] = field(default_factory=list)
    conflicts: list[tuple[str, SupersessionMatch]] = field(default_factory=list)
    reviewed: list[str] = field(default_factory=list)
    extended: list[str] = field(default_factory=list)
    update_suggested: list[str] = field(default_factory=list)
    review_filed: list[tuple[str, str, str]] = field(default_factory=list)
    cue_dropped: list[str] = field(default_factory=list)

    def event_fields(self) -> dict[str, Any]:
        """Conditional, so a write that set nothing keeps the event's
        shape. `supersedes` is every target linked; `supersedes_detected`
        the subset the detector chose on its own, which is the telemetry
        a detector-set link removed by a later `memory_update(links=[])`
        is judged against."""
        out: dict[str, Any] = {}
        if self.declared or self.detected or self.reviewed:
            out["supersedes"] = (
                self.declared + [m.memory_id for m in self.detected] + self.reviewed
            )
        if self.detected:
            out["supersedes_detected"] = [m.memory_id for m in self.detected]
        if self.reviewed:
            out["supersedes_reviewed"] = list(self.reviewed)
        if self.extended:
            out["extends_reviewed"] = list(self.extended)
        if self.update_suggested:
            out["update_suggested"] = list(self.update_suggested)
        if self.conflicts:
            out["conflicts_filed"] = [pair_id for pair_id, _ in self.conflicts]
        if self.review_filed:
            out["review_filings"] = [
                {"pair_id": pair_id, "id": target, "detector": detector}
                for pair_id, target, detector in self.review_filed
            ]
        if self.cue_dropped:
            out["supersedes_cue_dropped"] = list(self.cue_dropped)
        return out

    def response_fields(self) -> dict[str, Any]:
        rows = [{"id": target, "evidence": "declared"} for target in self.declared]
        rows += [_match_row(m) for m in self.detected]
        rows += [{"id": target, "evidence": "review"} for target in self.reviewed]
        out: dict[str, Any] = {}
        if rows:
            out["supersedes"] = rows
        if self.extended:
            out["extends"] = [
                {"id": target, "evidence": "review"} for target in self.extended
            ]
        filed = [{"pair_id": pair_id, **_match_row(m)} for pair_id, m in self.conflicts]
        filed += [
            {"pair_id": pair_id, "id": target, "detector": detector}
            for pair_id, target, detector in self.review_filed
        ]
        if filed:
            out["conflicts_filed"] = filed
            out["hint"] = _CONFLICTS_FILED_HINT
        if self.update_suggested:
            out["update_suggested"] = list(self.update_suggested)
        return out


def _match_row(match: SupersessionMatch) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": match.memory_id,
        "summary": match.summary,
        "evidence": match.evidence,
        "new_value": match.new_value,
        "old_value": match.old_value,
    }
    if match.cue is not None:
        row["cue"] = match.cue
    return row


def _validate_declared_supersedes(deps: ToolHandlers, ids: Any) -> list[dict[str, Any]]:
    """The writer's `supersedes=` list as link dicts for the payload.

    Each id must be a ULID naming an ACTIVE memory: a declared edge to a
    tombstone or a typo would render nothing and sit in the frontmatter
    unread, so it is refused at the one moment the writer can fix it.
    Dicts rather than `MemoryLink`s because they ride the payload to the
    commit as plain data; `Store.write` validates them.
    """
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("supersedes must be a list of memory ids")
    unique: list[str] = []
    for memory_id in ids:
        if memory_id not in unique:
            unique.append(memory_id)
    if len(unique) > MAX_DECLARED_SUPERSEDES:
        raise ValueError(
            f"supersedes lists at most {MAX_DECLARED_SUPERSEDES} ids (got {len(unique)})"
        )
    links: list[dict[str, Any]] = []
    for memory_id in unique:
        if not is_valid_ulid(memory_id):
            raise ValueError(f"supersedes entry {memory_id!r} is not a memory id")
        try:
            deps.store.load_one(memory_id)
        except (MemoryNotFoundError, TombstonedError) as exc:
            raise ValueError(
                f"supersedes entry {memory_id!r} is not an active memory: {exc}"
            ) from exc
        links.append(
            {
                "type": "supersedes",
                "target_id": memory_id,
                "note": "declared at write time",
            }
        )
    return links


def _persist(
    deps: GateDeps,
    payload: dict[str, Any],
    *,
    active_snapshot: list[Memory] | None = None,
    report: SupersessionReport | None = None,
    decision: _review.ReviewDecision | None = None,
) -> tuple[Memory, SupersessionOutcome]:
    """Detect supersession, write the record with its links in one locked
    write, file the cue-less disagreements and the review's filings.
    Shared by the direct commit and the reviewed commit, and reachable
    through a `GateBundle` for a caller with a store and no server, so
    none of them can drift on what a write sets.

    `active_snapshot` is the `load_all` the dedup gate or the review
    already paid for; a caller with none loads. `report` is the cue
    code's report when the review already ran it on this call, so the cue
    code runs once per call; None runs it here (only when
    `behavior.write_supersession` is on). `decision` is the review's: its
    links are set beside the cue code's, less the cue targets it disputed
    (`cue_dropped`, unit D1c). A target the writer declared is excluded
    from detection. `OSError` from the
    write propagates so each caller can name its own remedy.
    """
    payload = dict(payload)
    captured: _CaptureWrite | None = payload.pop("capture", None)
    caller: Caller | None = payload.pop("caller", None)
    standing_answer: StandingAnswer | None = payload.pop("standing", None)
    declared_links = list(payload.pop("links", None) or [])
    declared_ids = [str(link["target_id"]) for link in declared_links]
    detected: list[SupersessionMatch] = []
    disagreements: list[SupersessionMatch] = []
    existing = active_snapshot
    if report is None and deps.config.behavior.write_supersession:
        if existing is None:
            existing = deps.store.load_all()
        report = _supersession.detect_supersession(
            payload["content"], existing, exclude_ids=declared_ids
        )
    if report is not None:
        detected, disagreements = report.supersedes, report.conflicts
    dropped = set(decision.cue_dropped) if decision is not None else set()
    cue_dropped = [m.memory_id for m in detected if m.memory_id in dropped]
    detected = [m for m in detected if m.memory_id not in dropped]
    linked = set(declared_ids) | {m.memory_id for m in detected}
    reviewed = [
        target
        for target in (decision.links if decision is not None else ())
        if target not in linked
    ]
    # A record the write replaces is not also disputed by it.
    disagreements = [
        m
        for m in disagreements
        if m.memory_id not in linked and m.memory_id not in reviewed
    ]
    extended = [
        target
        for target in (decision.extends if decision is not None else ())
        if target not in linked
    ]
    links = (
        declared_links
        + [
            {"type": "supersedes", "target_id": m.memory_id, "note": m.note()}
            for m in detected
        ]
        + [
            {
                "type": "supersedes",
                "target_id": target,
                "note": _review.REVIEW_LINK_NOTE,
            }
            for target in reviewed
        ]
        + [
            {
                "type": "extends",
                "target_id": target,
                "note": _review.REVIEW_LINK_NOTE,
            }
            for target in extended
        ]
    )

    def put() -> Memory:
        # The record, its home and HEAD as a witness of its project commit
        # together (design 8.1): a home is written once, with the record.
        if caller is None:
            return deps.store.write(**payload, links=links, standing=standing_answer)
        written = deps.store.write(
            **payload, links=links, home=caller.home(), standing=standing_answer
        )
        caller.add_head_witness("write")
        return written

    if captured is None:
        if caller is None:
            memory = put()
        else:
            with deps.store.batch():
                memory = put()
    else:
        # A capture write and its member row commit in one transaction, so
        # a batch interrupted at any point still names every memory it
        # wrote and an undo can find them all.
        with deps.store.batch():
            memory = put()
            _capture.record_member(
                deps.store,
                captured.batch,
                memory_id=memory.id,
                body=memory.body,
                harness_session=captured.harness_session,
                server_session=captured.server_session,
                joined_by=captured.joined_by,
                review=captured.review,
            )
    # The cue code files first: where it fires its row stands (DESIGN
    # item 6), so the review then skips the pairs it queued.
    conflicts = _file_conflicts(deps, memory, disagreements)
    review_filed: list[tuple[str, str, str]] = []
    if decision is not None and decision.filings:
        if existing is None:
            existing = deps.store.load_all()
        review_filed = _file_review_pairs(
            deps,
            memory,
            decision.filings,
            existing,
            cue_filed={pair_id for pair_id, _ in conflicts},
        )
    return memory, SupersessionOutcome(
        declared=declared_ids,
        detected=detected,
        conflicts=conflicts,
        reviewed=reviewed,
        extended=extended,
        update_suggested=list(decision.update_suggested) if decision else [],
        review_filed=review_filed,
        cue_dropped=cue_dropped,
    )


def _file_conflicts(
    deps: GateDeps, memory: Memory, matches: list[SupersessionMatch]
) -> list[tuple[str, SupersessionMatch]]:
    """Queue each cue-less disagreement for `memory_conflicts`.
    Best-effort by contract: the memory is already committed, and a filing
    that fails, on the disk or in the store (busy, locked, full), never
    turns a committed write into an error; it logs, and the response omits
    the pair."""
    if not matches:
        return []
    queue = ConflictQueue(deps.store)
    created = utcnow().isoformat()
    filed: list[tuple[str, SupersessionMatch]] = []
    for match in matches:
        candidate = ConflictCandidate(
            id=_pair_id(memory.id, match.memory_id),
            a_id=memory.id,
            b_id=match.memory_id,
            summary_a=snippet_for(memory.body, max_chars=100),
            summary_b=match.summary,
            similarity=match.similarity,
            method="jaccard",
            detector=match.detector(),
            created=created,
        )
        try:
            queue.file_pair(candidate)
        except (OSError, sqlite3.Error) as exc:
            log.warning(
                "conflict filing for %s / %s failed (%s: %s)",
                memory.id,
                match.memory_id,
                error_code_name(exc),
                exc,
            )
            continue
        filed.append((candidate.id, match))
    return filed


def _file_review_pairs(
    deps: GateDeps,
    memory: Memory,
    filings: tuple[tuple[str, str], ...],
    active: list[Memory],
    *,
    cue_filed: set[str],
    in_batch: bool = False,
) -> list[tuple[str, str, str]]:
    """Queue each pair the review's answers file, with detector
    ``review`` or ``review-disagrees``. Best-effort and a separate log
    transaction, as `_file_conflicts` is. A pair in `cue_filed` (the pair
    ids the cue code queued on this write) keeps the cue code's row and
    is neither filed again nor reported twice. `in_batch` puts each row
    straight into the caller's open `Store.batch`, for a caller whose
    `cue_filed` already holds every queued pair id."""
    queue = ConflictQueue(deps.store)
    created = utcnow().isoformat()
    by_id = {m.id: m for m in active}
    new_tokens = raw_content_tokens(memory.body)
    seen = set(cue_filed)
    filed: list[tuple[str, str, str]] = []
    for target, detector in filings:
        other = by_id.get(target)
        pair_id = _pair_id(memory.id, target)
        if other is None or pair_id in seen:
            continue
        seen.add(pair_id)
        candidate = ConflictCandidate(
            id=pair_id,
            a_id=memory.id,
            b_id=target,
            summary_a=snippet_for(memory.body, max_chars=100),
            summary_b=snippet_for(other.body, max_chars=100),
            similarity=round(
                _pairwise_content_jaccard(new_tokens, raw_content_tokens(other.body)),
                4,
            ),
            method="jaccard",
            detector=detector,
            created=created,
        )
        try:
            if in_batch:
                deps.store.put_conflict(candidate.to_dict())
            else:
                queue.file_pair(candidate)
        except (OSError, sqlite3.Error) as exc:
            log.warning(
                "review filing for %s / %s failed (%s: %s)",
                memory.id,
                target,
                error_code_name(exc),
                exc,
            )
            continue
        filed.append((candidate.id, target, detector))
    return filed


def _payload_caller(deps: GateDeps, payload: dict[str, Any]) -> Caller:
    """The writing caller, resolved once per request (design 3.1) and kept in
    the payload, which the commit reads it from."""
    caller: Caller | None = payload.get("caller")
    if caller is None:
        caller = _caller_at(deps, payload.get("origin"))
        payload["caller"] = caller
    return caller


def _caller_at(deps: GateDeps, origin: Any) -> Caller:
    """The writing caller resolved at its origin's cwd (design 8.1), the
    harness session it carries (decision B6). An origin with no cwd (a
    process whose working directory is gone) is a caller nothing could be
    read for: its write is homed by decision B6, never at another path."""
    from ..location import resolve_caller

    cwd = getattr(origin, "cwd", None) if origin is not None else None
    return resolve_caller(
        deps.store,
        deps.config,
        cwd if isinstance(cwd, str) and cwd else None,
        session=identity.current_harness_session(),
    )


def _commit_write(
    deps: ToolHandlers,
    *,
    payload: dict[str, Any],
    commit: _CommitArgs,
    active_snapshot: list[Memory] | None = None,
    report: SupersessionReport | None = None,
    decision: _review.ReviewDecision | None = None,
    review_fields: dict[str, Any] | None = None,
    pending: list[_review.RelatedEntry] | None = None,
) -> dict[str, Any]:
    """Persist the memory, record the commit event, return the
    committed response. Surfaces the ambient long-body warning as a
    non-blocking advisory when applicable. `review_fields` (the review id,
    the answers and the action, never a body) join the commit event of a
    reviewed write. `pending` are the related entries a capture write left
    for the closing call, shown as the review shows them."""
    category_enum: Category = payload["category"]
    captured: _CaptureWrite | None = payload.get("capture")
    # The caller's membership (design 3.2) at the write's own location,
    # its origin's cwd: resolved before the record's transaction, which
    # then writes the home (`Caller.home`) and the HEAD witness.
    payload = dict(payload)
    _payload_caller(deps, payload)
    try:
        memory, supersession = _persist(
            deps,
            payload,
            active_snapshot=active_snapshot,
            report=report,
            decision=decision,
        )
    except OSError as exc:
        # Disk-level failure (ENOSPC/EIO/EACCES) in the durable write.
        # Translate to ValueError so the MCP boundary returns a clean
        # structured error rather than leaking the bare OSError's absolute
        # path to the client — matching the sibling lifecycle handlers
        # (remove/restore/verify/rename_scope).
        raise ValueError(f"failed to write memory: {exc}") from exc
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
        forced=commit.forced,
        related=[h.id for h in commit.related],
        removed_related=[h.id for h in commit.removed_related],
        markers_acknowledged=commit.acknowledged,
        credentials_acknowledged=commit.credentials_acknowledged,
        user_claims_acknowledged=commit.user_claims_acknowledged,
        scope_mismatches_acknowledged=commit.scope_mismatches_acknowledged,
        warnings=warnings,
        # Conditional so the default write event keeps its exact shape —
        # a populated field in the log means claims were actually
        # declared, which is the only telemetry the backfill pass and a
        # future coverage measurement can be built from.
        **({"claims": list(memory.claims)} if memory.claims else {}),
        **({"capture_batch": captured.batch.batch} if captured is not None else {}),
        **supersession.event_fields(),
        **(review_fields or {}),
    )
    response = deps.responses.committed(
        memory,
        related=commit.related,
        removed_related=commit.removed_related,
        warnings=warnings,
        **supersession.response_fields(),
    )
    if pending:
        response["relations_pending"] = [entry.to_dict() for entry in pending]
        response["hint"] = " ".join(
            filter(None, [response.get("hint"), _RELATIONS_PENDING_HINT])
        )
    return response


__all__ = ["DESC_MEMORY_WRITE", "memory_write"]
