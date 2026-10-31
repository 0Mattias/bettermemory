"""The standing roster on an update (ENG-112).

A standing statement is read against every standing ruling in force
before it is in force. `memory_write` shows a standing write the roster
on its review (`handlers.write`); this module shows it to a
`memory_update` that leaves its memory standing and in force with a
statement no roster has read (`standing.makes_standing`): its body edited
while it stays standing, a `standing` answer that makes it standing, or
its category made ambient. `memory_admin(action="answer_standing")`
sends an answer that would make a memory in force standing here, one
memory at a time (`handlers.standing`). A memory out of force is not
read here. One that comes back into force standing (the memory replacing
it removed or unlinked, or itself restored, ENG-200) is held out of force
(`standing.held_ids`) until a change through here reads it: any update
that leaves it standing gets the roster, and the commit releases it
(`StandingReview.releases`, `release`). So does an update that makes a
memory that came back while it was not standing standing: its read drops
the mark that would hold it. Each door that holds one names it in its
reply (`held_reply`).

The page, its lines and its question are the write's
(`write._issue_roster`, `review.roster_for`): every other admitted
standing record in force outside the session's disabled scopes, each line
windowed on the body the update leaves. The answers are the chain's keys
of the write's review (`review.parse_update_answers`): `named`, then
`relation` for each named record once it is shown in full, under the
chain's `review_id`, by the write's rules (`write._with_named`,
`review.check_named`). They are decided by `review.apply_review` as a
write's are, with `restates_refuses` off, since the memory is in the
store whatever the answer: a conflict with a standing record refuses; a
standing record the update replaces rests on the user's words (`said`,
`_guard.read_words`); an update or extends answer links from the memory
(`review_links`); a conflict with a record no longer standing files the
pair.

The chain is the write's (`review.find_chain`): `review_issued` rows with
``door`` `UPDATE_DOOR` and ``record`` the memory's id, closed by the
`update` row of its commit, the one that carries the review_id
(`StandingReview.fields`). A chain answers only an update of its own
memory, and a write's chain never answers an update.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import review as _review
from .. import standing as _standing
from .. import supersession as _supersession
from ..models import LinkType, Memory, MemoryLink, generate_ulid
from . import _guard
from ._shared import caller_anchoring
from .write import (
    _file_review_pairs,
    _issue_roster,
    _record_review_issued,
    _roster_due,
    _standing_rows,
    _with_named,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..admission import Admission
    from ..location import Caller
    from ..session import SessionState

__all__ = [
    "UPDATE_DOOR",
    "StandingReview",
    "after_commit",
    "early_response",
    "held_reply",
    "releasing",
    "release",
    "with_held",
    "review_change",
    "review_links",
]

#: The ``door`` an update's standing roster rows carry: the tool whose
#: call the chain answers.
UPDATE_DOOR = "memory_update"

#: The record's link cap (`models.Memory._check_links`), which the update's
#: `model_copy` skips: a record over it is written and then fails every
#: read (`update._new_links`), so links the review adds are held to it.
_LINK_CAP = 64


def _roster_hint(review_id: str, follow_up: bool, *, held: bool = False) -> str:
    """The roster page's hint on an update: what to send back. `held`
    leads a held memory's first page (ENG-200)."""
    if follow_up:
        lead = (
            "Not updated. Standing memories came into force since your last "
            "roster; they are listed below. Keep review.named and add any id "
            "below the change touches. "
        )
    elif held:
        lead = (
            "Not updated. This standing memory came back into force (the "
            "memory replacing it was removed or unlinked, or it was "
            "restored) and is held out of force until it is read against "
            "every other standing memory in force, one line each, under "
            "`roster`; any change that leaves it standing reads it. "
        )
    else:
        lead = (
            "Not updated. This change makes the memory standing, so it is "
            "shown every other standing memory in force, one line each, "
            "under `roster`. "
        )
    return (
        lead + "Answer `question` and re-send this same call, with the same "
        "arguments and acknowledge flags, adding "
        f'review={{"review_id": "{review_id}", "named": [<roster id>, ...]}} '
        "([] when the change touches none)."
    )


def _full_hint(review_id: str) -> str:
    """The hint of the page that shows named roster records in full."""
    return (
        "Not updated. Below in full are roster memories this review now "
        "holds: keep every answer, review.named included, and answer each id "
        "below. Re-send this same call, with the same arguments and "
        f'acknowledge flags: review={{"review_id": "{review_id}", "named": '
        f'[...], "relation": {{<id>: {_review.LABEL_CHOICES}}}}}.'
    )


_HELD_HINT = (
    "Each memory under `held` is standing and came back into force through "
    "this call. It is held out of force, not delivered and on no roster, "
    "until it is read against every other standing memory in force: give "
    "its standing answer, which shows it the roster, with "
)

_STANDING_HINT = (
    "Not updated. Each memory under `standing` directs work in its scope "
    "until it is replaced, and your answers conflict with it or replace it. "
    "Leave this memory as it is, or change it to comply; or, if the user "
    "changed the memory under `standing` in this conversation, answer update "
    "for it and add said=<the user's words as they typed them>."
)


@dataclass
class StandingReview:
    """What an update's standing roster leaves: a page to return instead
    of changing anything (a roster page, a full-text page or a refusal),
    or the decision with what its commit needs. `releases` says the
    memory carries a ``returned`` mark (ENG-200) and this read lets its
    commit drop it; a marked memory with no other standing memory in
    force to list has a review of that alone."""

    page: dict[str, Any] | None = None
    decision: _review.ReviewDecision | None = None
    answers: _review.ReviewAnswers | None = None
    related: _review.RelatedSet | None = None
    words: _guard.Words | None = None
    rests: list[str] = field(default_factory=list)
    releases: bool = False

    def fields(self) -> dict[str, Any]:
        """The update row's review fields: ids and labels, never a body.
        Empty when nothing was decided here. Only a commit carries the
        `review_id`, which closes the chain (`review.find_chain`): a
        refusal changes nothing, so the call re-sent with the user's words
        or other answers under the same review_id is decided on the pages
        the chain already showed."""
        answers, decision = self.answers, self.decision
        if answers is None or decision is None or answers.review_id is None:
            return {}
        out: dict[str, Any] = {
            "review_answers": {
                "relation": dict(answers.relation),
                "named": list(answers.named or ()),
            },
            "review_action": decision.action,
        }
        if decision.action == _review.ACTION_COMMIT:
            out["review_id"] = answers.review_id
        if decision.restated:
            out["review_restated"] = list(decision.restated)
        return out


def _chain(
    deps: ToolHandlers, answers: _review.ReviewAnswers | None, record_id: str
) -> _review.Chain | None:
    """The open chain of this memory's standing roster the answers name,
    or None: a review_id of another door, of another memory, or of a
    chain already decided starts a roster of its own."""
    if answers is None or answers.review_id is None:
        return None
    found = _review.find_chain(deps.store, answers.review_id)
    if found.open and found.door == UPDATE_DOOR and found.record == record_id:
        return found
    return None


def _issue_full(
    deps: ToolHandlers,
    related: _review.RelatedSet,
    *,
    chain: _review.Chain,
    record_id: str,
) -> dict[str, Any]:
    """Write nothing; return the named roster records in full and the
    relation question, a page of the chain."""
    review_id = generate_ulid()
    questions = {"relation": _review.questions_payload()["relation"]}
    response = deps.responses.review(
        review_id=review_id,
        related=[entry.to_dict() for entry in related.entries],
        questions=questions,
        hint=_full_hint(review_id),
    )
    _record_review_issued(
        deps,
        review_id=review_id,
        related_ids=related.ids,
        pre_answered=False,
        parent_review_id=chain.root,
        response_chars=len(json.dumps(response, ensure_ascii=False)),
        door=UPDATE_DOOR,
        record=record_id,
    )
    return response


def review_change(
    deps: ToolHandlers,
    state: SessionState,
    memory: Memory,
    *,
    body: str,
    category: str | None,
    probability: float | None,
    review: Mapping[str, Any] | None,
    said: str | None,
) -> StandingReview | None:
    """Take an update of `memory` that leaves `body`, `category` and the
    standing answer `probability`; return None when it makes no standing
    statement (`standing.makes_standing`), the review is off, or no other
    standing record is in force to list and the memory carries no
    ``returned`` mark; else the page it is shown, or the decision on its
    answers, which releases the memory once it commits: held, or marked
    while it was not standing and made standing here. Raises
    `review.ReviewAnswerError`, a teaching error, for answers it cannot
    read."""
    from ..admission import Admission
    from ..membership import REVIEW_STRATA

    answers = _review.parse_update_answers(review)
    tau = _review.current_tau()
    if tau is None or not _standing.makes_standing(
        deps.store,
        memory.id,
        category=category,
        probability=probability,
        body_changed=body != memory.body,
    ):
        return None
    held = memory.id in _standing.held_ids(deps.store, [memory.id])
    marked = bool(deps.store.returned_rows([memory.id]))
    _, caller = caller_anchoring(deps.store, deps.config)
    admission = Admission.of(REVIEW_STRATA, caller)
    chain = _chain(deps, answers, memory.id)
    if answers is None:
        answers = _review.ReviewAnswers(relation={}, unsafe=0.0, secret=0.0)
    related = _review.RelatedSet(entries=())
    unseen: list[str] = []
    if chain is not None:
        _review.check_answered_ids(answers, chain.shown | chain.roster)
        related, unseen = _with_named(
            answers,
            related,
            chain,
            standing_write=True,
            content=body,
            active=deps.store.load_all(),
        )
        missing = [i for i in related.ids if i not in answers.relation]
        if missing or unseen:
            full = _issue_full(
                deps,
                related.only([*missing, *unseen]),
                chain=chain,
                record_id=memory.id,
            )
            return StandingReview(page=full)
    page = _due_roster(deps, state, memory, body, related, admission, chain, held)
    if page is not None:
        return StandingReview(page=page)
    if chain is None:
        return StandingReview(releases=True) if marked else None
    decided = _decide(deps, caller, answers, related, body=body, tau=tau, said=said)
    decided.releases = marked
    return decided


def _due_roster(
    deps: ToolHandlers,
    state: SessionState,
    memory: Memory,
    body: str,
    related: _review.RelatedSet,
    admission: Admission,
    chain: _review.Chain | None,
    held: bool,
) -> dict[str, Any] | None:
    """The roster page due for this chain, or None: every other standing
    record in force on the first page, then only those that came into
    force since (`write._roster_due`). `held` leads the hint of a held
    memory's first page."""
    candidates, due = _roster_due(
        deps,
        state,
        related=related,
        declared_ids=[memory.id],
        admission=admission,
        chain=chain,
    )
    if not due:
        return None
    return _issue_roster(
        deps,
        state,
        content=body,
        candidates=candidates,
        due=due,
        admission=admission,
        parent_review_id=chain.root if chain is not None else None,
        follow_up=bool(chain is not None and chain.roster),
        hint=lambda review_id, follow_up: _roster_hint(review_id, follow_up, held=held),
        door=UPDATE_DOOR,
        record=memory.id,
    )


def _decide(
    deps: ToolHandlers,
    caller: Caller,
    answers: _review.ReviewAnswers,
    related: _review.RelatedSet,
    *,
    body: str,
    tau: _review.Tau,
    said: str | None,
) -> StandingReview:
    """The answers decided as a write's are (`review.apply_review`), with a
    restatement refusing nothing. A conflict with a standing record refuses
    whatever `said` holds, so the words are read only for a standing
    record the update replaces and nothing conflicts with."""
    standing_ids = _standing.guarded_ids(deps.store, related.ids)
    conflicted = any(
        answers.relation.get(i) == _review.LABEL_CONFLICT for i in standing_ids
    )
    rests = [i for i in _review.replaced_ids(answers, related) if i in standing_ids]
    words = _guard.read_words(deps, caller, said) if rests and not conflicted else None
    decision = _review.apply_review(
        answers,
        related,
        tau=tau,
        write_claim_sized=_supersession.claim_sized(body),
        write_supersession=deps.config.behavior.write_supersession,
        standing_ids=standing_ids,
        said=words is not None,
        restates_refuses=False,
    )
    return StandingReview(
        decision=decision, answers=answers, related=related, words=words, rests=rests
    )


def early_response(
    deps: ToolHandlers, record_id: str, review: StandingReview | None
) -> dict[str, Any] | None:
    """What the update returns instead of changing anything: the review's
    page, or the refusal its decision makes; None to go ahead."""
    from ..admission import Admission
    from ..membership import REVIEW_STRATA

    if review is None:
        return None
    if review.page is not None:
        deps.recorder.record("update", id=record_id, status="standing_roster")
        return review.page
    decision = review.decision
    if decision is None or decision.action != _review.ACTION_STANDING:
        return None
    deps.recorder.record(
        "update",
        id=record_id,
        status=_review.ACTION_STANDING,
        standing=list(decision.standing),
        **review.fields(),
    )
    _, caller = caller_anchoring(deps.store, deps.config)
    return {
        "status": _review.ACTION_STANDING,
        "standing": _standing_rows(
            deps,
            review.related or _review.RelatedSet(entries=()),
            decision.standing,
            Admission.of(REVIEW_STRATA, caller),
        ),
        "hint": _STANDING_HINT,
    }


def review_links(
    links: list[MemoryLink], review: StandingReview | None
) -> list[MemoryLink]:
    """`links` with the decision's supersedes and extends links added,
    each target once (note `review.REVIEW_LINK_NOTE`). Raises a teaching
    ValueError, before anything is written, when they would take the
    memory past the link cap."""
    decision = review.decision if review is not None else None
    if decision is None or decision.action != _review.ACTION_COMMIT:
        return links
    out = list(links)
    have = {(link.type, link.target_id) for link in out}
    added = [(LinkType.SUPERSEDES, t) for t in decision.links]
    added += [(LinkType.EXTENDS, t) for t in decision.extends]
    for link_type, target in added:
        if (link_type, target) not in have:
            out.append(
                MemoryLink(
                    type=link_type, target_id=target, note=_review.REVIEW_LINK_NOTE
                )
            )
            have.add((link_type, target))
    if len(out) > _LINK_CAP:
        raise ValueError(
            f"links list capped at {_LINK_CAP} entries: the review's answers "
            f"would leave this memory {len(out)}; nothing was written. Drop "
            "links it no longer needs, then re-send this call."
        )
    return out


def releasing(
    deps: ToolHandlers, review: StandingReview | None
) -> AbstractContextManager[object]:
    """The transaction an update's commit and its `release` share: the
    store's batch when the review releases a mark, else none, so an update
    with nothing to release takes the write lock at its commit as before."""
    if review is None or not review.releases:
        return nullcontext()
    return deps.store.batch()


def release(deps: ToolHandlers, record_id: str, review: StandingReview | None) -> None:
    """Release the marked memory a committed update read (ENG-200): drop
    its ``returned`` mark. Called inside the commit's transaction, so the read
    and the release land together."""
    if review is None or not review.releases:
        return
    decision = review.decision
    if decision is not None and decision.action != _review.ACTION_COMMIT:
        return
    deps.store.release_returned(record_id)


def held_reply(deps: ToolHandlers, ids: list[str]) -> dict[str, Any]:
    """The reply keys of a call that brought standing memories back into
    force (ENG-200): ``held``, each of `ids` now held, as a refusal names a
    standing memory (`standing.refusal_rows`), and the ``hint`` naming
    the update that reads each; {} when none is held."""
    from ..admission import Admission
    from ..membership import REVIEW_STRATA

    held = _standing.held_ids(deps.store, ids) if ids else set()
    ordered = [i for i in dict.fromkeys(ids) if i in held]
    if not ordered:
        return {}
    _, caller = caller_anchoring(deps.store, deps.config)
    calls = ", ".join(f'memory_update(id="{i}", standing=<0 to 1>)' for i in ordered)
    return {
        "held": _standing.refusal_rows(
            deps.store, ordered, Admission.of(REVIEW_STRATA, caller)
        ),
        "hint": _HELD_HINT + calls + ".",
    }


def with_held(response: dict[str, Any], reply: dict[str, Any]) -> dict[str, Any]:
    """`response` with `held_reply`'s keys, its own hint kept first."""
    if not reply:
        return response
    out = dict(response)
    out["held"] = reply["held"]
    out["hint"] = f"{out['hint']} {reply['hint']}" if out.get("hint") else reply["hint"]
    return out


def after_commit(
    deps: ToolHandlers, memory: Memory, review: StandingReview | None
) -> dict[str, Any]:
    """After the update committed: file the pairs the answers file, write
    the `standing_guard` row for the standing records it replaced on the
    user's words, and return the committed response's review keys
    (`supersedes`, `extends`, `update_suggested`, `conflicts_filed`)."""
    decision = review.decision if review is not None else None
    if review is None or decision is None or decision.action != _review.ACTION_COMMIT:
        return {}
    out: dict[str, Any] = {}
    linked = {link.target_id for link in memory.links}
    supersedes = [t for t in decision.links if t in linked]
    extends = [t for t in decision.extends if t in linked]
    if supersedes:
        out["supersedes"] = [{"id": t, "evidence": "review"} for t in supersedes]
    if extends:
        out["extends"] = [{"id": t, "evidence": "review"} for t in extends]
    if decision.update_suggested:
        out["update_suggested"] = list(decision.update_suggested)
    if decision.filings:
        filed = _file_review_pairs(
            deps, memory, decision.filings, deps.store.load_all(), cue_filed=set()
        )
        if filed:
            out["conflicts_filed"] = [
                {"pair_id": p, "id": t, "detector": d} for p, t, d in filed
            ]
    if review.words is not None and review.rests:
        _guard.record(deps, UPDATE_DOOR, review.rests, review.words)
    return out
