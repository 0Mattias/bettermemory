"""The capture's closing call: memory_admin(action="relations").

A capture write that carries its two scores commits in one call, and the
related statements it did not answer come back as `relations_pending`
(`handlers.write._reviewed_write`). Once the capture is saved the model
answers them all in one call, keyed member id -> {related id: label}.

The answers act as the write review's do (`review.apply_review`, on the
write's own scores and the related set its member row recorded at commit
time), applied after the commit: a conflict with a standing record, or a
standing record replaced without the user's words, tombstones the member
(refused_standing, unit RS-F7 part 5), and so does restates with nothing
updated, extended or declared; update links it (claim-sized pair,
write-time supersession on) or names the target in update_suggested,
extends sets an extends link, conflict files the pair as "review", and
conflict, unrelated or extends on a cue-linked target drops the cue link
and files "review-disagrees". A member's answers apply
once all its pending ids are answered; answers to part of them are kept
until the rest arrive. Repeat calls are allowed until the batch settles.
An answer for an id that is not pending for that member is a teaching error,
and nothing from that call is applied.

Each call is a `capture_relations` control row per batch, naming the
answers and everything they changed, so `capture undo` reverts it too.
A link is set without moving the member's `updated`: it is the write's own
review, as it would have been had the write waited for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .. import capture as _capture
from .. import identity
from .. import review as _review
from .. import standing as _standing
from ..models import LinkType, MemoryLink
from ..store import NotFoundError, TombstonedError
from ..supersession import SupersessionMatch, SupersessionReport
from . import standing_review as _standing_review
from ._shared import Context
from .write import _file_review_pairs

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..store import Store

_FORMAT = "relations={<member id>: {<related id>: " + _review.LABEL_CHOICES + "}}"

_DONE_HINT = "Every relation of this capture is answered."
_STANDING_HINT = (
    " A write under applied with refused_standing was removed: its answers "
    "conflict with or replace a standing memory. Write it again with "
    "memory_write, rewritten to comply, or answered update with review.said, "
    "the user's words, if the user changed it in this conversation."
)
_LEFT_HINT = (
    "Answer the ids under relations_pending in the same way, then stop. "
    f"Format: {_FORMAT}"
)


def _related_set(record: Mapping[str, Any]) -> _review.RelatedSet:
    """The related set a member row recorded, as `apply_review` reads it:
    ids with their claim_sized flags, and the cue code's links among them."""
    entries = tuple(
        _review.RelatedEntry(
            id=str(memory_id), scopes=(), updated="", claim_sized=bool(sized), text=""
        )
        for memory_id, sized in record.get("related") or []
    )
    report = SupersessionReport(
        eligible=True,
        supersedes=[
            SupersessionMatch(str(i), "supersedes", "", None, "", "", 0.0, "")
            for i in record.get("cue_linked") or []
        ],
        conflicts=[],
    )
    return _review.RelatedSet(entries=entries, report=report)


def _read(
    deps: ToolHandlers, relations: Any, *, server_session: str
) -> dict[str, tuple[_capture.Member, _capture.Batch, dict[str, str]]]:
    """Check the whole call before anything is applied: every key a member
    of an open batch whose write left relations pending, the caller's
    session joined to that batch, and every answer a label for an id
    pending for that member. Raises `ValueError` otherwise."""
    if not isinstance(relations, Mapping) or not relations:
        raise _review.ReviewAnswerError(
            f"relations maps each capture write's id to its answers. Format: {_FORMAT}"
        )
    store = deps.store
    by_id = {m.memory_id: m for m in _capture.members(store) if m.review is not None}
    batches = {b.batch: b for b in _capture.open_batches(store)}
    caller = identity.current()
    out: dict[str, tuple[_capture.Member, _capture.Batch, dict[str, str]]] = {}
    pending_of: dict[str, dict[str, tuple[str, ...]]] = {}
    for member_id, given in relations.items():
        member = by_id.get(member_id)
        if member is None:
            raise _review.ReviewAnswerError(
                f"{member_id!r} is not a capture write with relations_pending."
            )
        batch = batches.get(member.batch)
        if batch is None:
            raise ValueError(
                f"The capture batch of {member_id!r} has settled; its relations "
                "stay unanswered."
            )
        if batch.batch not in pending_of:
            joined = _capture.joined_by(
                store,
                batch,
                harness_session=caller.harness_session if caller else None,
                server_session=server_session,
            )
            if joined is None:
                raise ValueError("This capture batch belongs to another conversation.")
            pending_of[batch.batch] = _capture.relations_pending(store, batch.batch)
        pending = pending_of[batch.batch].get(member_id, ())
        if not isinstance(given, Mapping):
            raise _review.ReviewAnswerError(
                f"relations[{member_id!r}] must map related ids to labels. "
                f"Format: {_FORMAT}"
            )
        labels: dict[str, str] = {}
        for related_id, label in given.items():
            if label not in _review.LABELS:
                raise _review.ReviewAnswerError(
                    f"relations[{member_id!r}][{related_id!r}] is {label!r}, which "
                    f"is not a label; use one of {', '.join(_review.LABELS)}."
                )
            if related_id not in pending:
                raise _review.ReviewAnswerError(
                    f"relations[{member_id!r}] names {related_id!r}, which is not "
                    f"pending for that write (pending: {list(pending)}). Answer only "
                    "the ids its relations_pending listed."
                )
            labels[related_id] = label
        out[member_id] = (member, batch, labels)
    return out


def _apply(
    deps: ToolHandlers,
    member: _capture.Member,
    answered: Mapping[str, str],
    *,
    filed_before: set[str],
) -> dict[str, Any]:
    """Apply one fully answered member's closing answers. `answered` holds
    every answer the closing calls gave it; the write's own answers took
    effect at commit, so only the closing ids act here."""
    store = deps.store
    record = member.review or {}
    tau = record.get("tau") or [1.0, 1.0]
    relation = {**(record.get("relation") or {}), **answered}
    # What is standing is read again now, beside what the commit recorded:
    # a record can have become standing since.
    related_ids = [str(i) for i, _ in record.get("related") or []]
    related_ids += [str(i) for i in record.get("cue_linked") or []]
    guarded = set(record.get("standing") or []) | set(
        _standing.guarded_ids(store, related_ids)
    )
    decision = _review.apply_review(
        _review.ReviewAnswers(
            relation=relation,
            unsafe=float(record.get("unsafe", 0.0)),
            secret=float(record.get("secret", 0.0)),
        ),
        _related_set(record),
        tau=_review.Tau(unsafe=float(tau[0]), secret=float(tau[1])),
        write_claim_sized=bool(record.get("claim_sized")),
        write_supersession=deps.config.behavior.write_supersession,
        declared_ids=record.get("declared") or [],
        standing_ids=sorted(guarded),
        said=bool(record.get("said")),
    )
    out: dict[str, Any] = {"action": decision.action}
    try:
        memory = store.load_one(member.memory_id)
    except (NotFoundError, TombstonedError):
        out["action"] = "gone"
        return out
    if decision.action in (_review.ACTION_RESTATES, _review.ACTION_STANDING):
        # The write adds nothing, or may not stand: it goes, as the
        # two-call review would never have written it, and so do the pairs
        # its own commit filed.
        if decision.restated:
            out["restated"] = list(decision.restated)
        if decision.standing:
            out["standing"] = list(decision.standing)
        dropped = [
            str(c["id"])
            for c in store.list_conflicts()
            if c.get("a_id") == memory.id and c.get("status") == "pending"
        ]
        for conflict_id in dropped:
            store.delete_conflict(conflict_id)
        why = (
            "restates " + ", ".join(decision.restated)
            if decision.action == _review.ACTION_RESTATES
            else "refused_standing " + ", ".join(decision.standing)
        )
        store.tombstone(memory.id, reason="capture relations: " + why)
        out["tombstoned"] = True
        if dropped:
            out["conflicts_deleted"] = dropped
        return out
    if decision.action != _review.ACTION_COMMIT:
        return out
    links = [t for t in decision.links if t in answered]
    extends = [t for t in decision.extends if t in answered]
    cue_dropped = [t for t in decision.cue_dropped if t in answered]
    suggested = [t for t in decision.update_suggested if t in answered]
    filings = tuple((t, d) for t, d in decision.filings if t in answered)
    active = {m.id: m for m in store.load_all()}
    links = [t for t in links if t in active]
    extends = [t for t in extends if t in active]
    if links or extends or cue_dropped:
        kept = [
            link
            for link in memory.links
            if not (link.type == LinkType.SUPERSEDES and link.target_id in cue_dropped)
        ]
        have = {link.target_id for link in kept}
        added = [(LinkType.SUPERSEDES, t) for t in links]
        added += [(LinkType.EXTENDS, t) for t in extends]
        for link_type, target in added:
            if target not in have:
                kept.append(
                    MemoryLink(
                        type=link_type,
                        target_id=target,
                        note=_review.REVIEW_LINK_NOTE,
                    )
                )
                have.add(target)
        memory = store.put_memory(memory.model_copy(update={"links": kept}))
    filed = _file_review_pairs(
        deps,
        memory,
        filings,
        list(active.values()),
        cue_filed=filed_before,
        in_batch=True,
    )
    for key, value in (
        ("supersedes", links),
        ("extends", extends),
        ("supersedes_cue_dropped", cue_dropped),
        ("update_suggested", suggested),
        (
            "conflicts_filed",
            [{"pair_id": p, "id": t, "detector": d} for p, t, d in filed],
        ),
    ):
        if value:
            out[key] = value
    return out


def _supersedes_of(store: Store, memory_id: str) -> set[str]:
    """The ``supersedes`` targets of the active record, or none when it is
    not active."""
    try:
        memory = store.load_one(memory_id)
    except (NotFoundError, TombstonedError):
        return set()
    return {link.target_id for link in memory.links if link.type == LinkType.SUPERSEDES}


async def capture_relations(
    deps: ToolHandlers, relations: Any, *, ctx: Context | None = None
) -> dict[str, Any]:
    """Answer the relations a capture's writes left pending."""
    state = deps.sessions.for_request(ctx)
    checked = _read(deps, relations, server_session=state.session_id)
    store = deps.store
    applied: dict[str, dict[str, Any]] = {}
    batches: dict[str, _capture.Batch] = {}
    # The records a member's refusal or a dropped cue link stops replacing
    # come back into force, a standing one held until read (ENG-200).
    unlinked: list[str] = []
    with store.batch():
        filed_before = {str(c["id"]) for c in store.list_conflicts()}
        rows: dict[str, dict[str, Any]] = {}
        for member_id, (member, batch, given) in checked.items():
            batches[batch.batch] = batch
            row = rows.setdefault(batch.batch, {"answers": {}, "applied": {}})
            row["answers"][member_id] = given
            prior = _capture.relations_answered(store, batch.batch).get(member_id, {})
            pending = (member.review or {}).get("pending") or []
            answered = {**prior, **given}
            if any(i not in answered for i in pending):
                continue
            linked = _supersedes_of(store, member.memory_id)
            result = _apply(deps, member, answered, filed_before=filed_before)
            unlinked += sorted(linked - _supersedes_of(store, member.memory_id))
            row["applied"][member_id] = result
            applied[member_id] = result
        for batch_id, row in rows.items():
            _capture.record_relations(store, batch_id, row, session=state.session_id)
    left: dict[str, list[str]] = {}
    for batch_id in batches:
        for member_id, ids in _capture.relations_pending(store, batch_id).items():
            left[member_id] = list(ids)
    response: dict[str, Any] = {"status": "recorded", "applied": applied}
    if left:
        response["relations_pending"] = left
    response["hint"] = _LEFT_HINT if left else _DONE_HINT
    if any(r.get("action") == _review.ACTION_STANDING for r in applied.values()):
        response["hint"] += _STANDING_HINT
    return _standing_review.with_held(
        response, _standing_review.held_reply(deps, unlinked)
    )


__all__ = ["capture_relations"]
