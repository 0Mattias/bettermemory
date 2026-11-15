"""memory_show MCP tool — handler implementation + DESC.

The handler is a focused single-id read: load the memory, surface every
drift signal we can in one call (so the caller doesn't bounce back for
a second tool round-trip), issue a use-token so the auto-`record_use`
flow has something to commit on the next turn.
"""

from __future__ import annotations
from .. import git_read_budget

from typing import TYPE_CHECKING, Any

from .. import standing as _standing
from .._response import isoformat, isoformat_optional, verdict_and_reason
from ..models import utcnow
from ..verdict import memory_verdict
from ._shared import Context, _advance_turn, caller_anchoring

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..admission import Admission
    from ..models import Memory
    from ..verdict import MemoryVerdict


DESC_MEMORY_SHOW = (
    "One memory in full: body, links both ways, verification and drift. A "
    "staleness_verdict other than fresh: act on what `reason` names first."
)


@git_read_budget.async_call
async def memory_show(
    deps: ToolHandlers, id: str, ctx: Context | None = None
) -> dict[str, Any]:
    """Body of the ``memory_show`` MCP tool."""
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    # A removed or unknown id raises the store's own error, which the tool
    # boundary refuses with its words (`_refusals_as_tool_errors`).
    memory = deps.store.load_one(id)
    shown, links = _show_verdict(deps, memory)
    drift = shown.path_drift
    commit_drift = shown.commit_drift
    # The links payload carries `provenance` too: how the record entered
    # the store, from the index (schema v7) and never from the file,
    # since a hand-written frontmatter field cannot supply it. Null
    # until a rebuild has classified the row, the same null-when-no-
    # signal contract `path_drift` and `commit_drift` keep on this
    # surface. Read on the index open the links query already holds, so
    # the no-inbound show stays at two connections.
    index_reads = _links_payload(deps, memory)
    verified_locally_at = index_reads.pop("_verified_locally_at")
    trust_unavailable = index_reads.pop("_trust_unavailable")
    # Issue a use-token for this show before returning so the
    # auto-`record_use` flow has something to commit on the next
    # turn if the model doesn't override.
    token_map = state.issue_use_tokens([memory.id])
    shown_state = _show_response(deps, memory, shown, index_reads, token_map[memory.id])
    _apply_show_trust_and_standing(
        deps, memory, shown_state, trust_unavailable, verified_locally_at
    )
    _apply_objection(deps, shown_state, links, set(state.disabled_scopes))
    deps.recorder.record(
        "show",
        id=memory.id,
        path_drift_checked=len(drift.checked),
        path_drift_missing=len(drift.missing),
        verification_status=shown_state["verification"]["status"],
        staleness_verdict=verdict_and_reason(shown_state)[0],
        commit_drift_status=(commit_drift.status if commit_drift is not None else None),
        commits_since_verify=(
            commit_drift.commits_since_verify if commit_drift is not None else None
        ),
    )
    return deps.responses.render_show(shown_state, memory, drift)


def _apply_objection(
    deps: ToolHandlers,
    shown_state: dict[str, Any],
    links: Admission,
    excluded: set[str],
) -> None:
    """Take the show's state; keep on it what on record objects to the
    record, as the search keeps it on a hit: the active, admitted records
    a `contradicts` link names in either direction
    (`_response.contradicting_ids`) and the recent uses that rejected it
    (`_response.recent_rejections`). `_response.verdict_and_reason` reads
    both."""
    from .._response import contradicting_ids, recent_rejections

    record_id = shown_state["id"]
    contradicted = contradicting_ids(
        deps.store, [record_id], admission=links, excluded_scopes=excluded
    )
    shown_state["_contradicted_by"] = contradicted.get(record_id, [])
    rejections = recent_rejections(deps.store, [record_id], now=utcnow())
    if rejections:
        shown_state["recent_negative_outcomes"] = rejections[record_id]


def _show_verdict(
    deps: ToolHandlers, memory: Memory
) -> tuple[MemoryVerdict, Admission]:
    """Take the shown memory; return its verdict for the caller's checkout,
    and the caller's LINKS admission."""
    from .. import _handlers as _h

    # One verdict for show and search (`verdict.memory_verdict`): the
    # calendar status, path drift over the full body (with the record's
    # attested paths) and the commit leg, read through the search's
    # per-hit memo, so a show of a row nothing has moved under forks no
    # git process. The commit leg is counted in the caller's checkout when
    # the caller's admission counts the record's home there (design 3.5),
    # else where health's estate check counts it (`commit_legs`, ENG-229),
    # and a leg that applies and that nothing here reads is flagged and
    # never fresh. It is claim-anchored: narrowed to commits that touch
    # what the record cites, attests or claims, and None for a record
    # that cites nothing a commit could move. The rollup is
    # `verify.verdict_from_signals`.
    #
    # The caller is resolved once (design 3.1): its anchoring reads the
    # record's claims where design 3.6 reads them, and its LINKS admission
    # decides whether the commit leg is counted in the caller's checkout
    # or where the anchoring reads it.
    from ..admission import Admission as _Admission
    from ..membership import LINKS

    anchoring, caller = caller_anchoring(deps.store, deps.config)
    links = _Admission.of(LINKS, caller)
    verdict = memory_verdict(
        memory,
        caller_origin=_h.capture_origin(),
        now=utcnow(),
        stale_after_days=deps.config.behavior.verification_stale_days,
        anchoring=anchoring,
        admission=links,
    )
    return verdict, links


def _show_response(
    deps: ToolHandlers,
    memory: Memory,
    shown: MemoryVerdict,
    index_reads: dict[str, Any],
    use_token: str,
) -> dict[str, Any]:
    """Take the memory, verdict, token and index reads (popped in place);
    return the show's state, which the trust rule, the standing question
    and the event read, and `ResponseBuilder.render_show` turns into the
    response."""
    drift = shown.path_drift
    verification = shown.verification
    commit_drift = shown.commit_drift
    verdict = shown.staleness_verdict
    response: dict[str, Any] = {
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "category": (memory.category.value if memory.category is not None else None),
        "created": isoformat(memory.created),
        "updated": isoformat(memory.updated),
        "last_verified_at": isoformat_optional(memory.last_verified_at),
        "verification": verification.to_dict(),
        "staleness_verdict": verdict,
        "provenance": index_reads.pop("provenance"),
        "body": memory.body,
        "origin": deps.responses.origin_to_dict(memory.origin),
        "actor": deps.responses.actor_to_dict(memory.actor),
        "path_drift": drift.to_dict() if drift.should_report else None,
        "commit_drift": (commit_drift.to_dict() if commit_drift is not None else None),
        "use_token": use_token,
        **index_reads,
    }
    if shown.commit_drift_unread is not None:
        # The commit leg applies and its history was not read: `commit_drift`
        # stays null, the row says why and is not fresh.
        deps.responses.apply_commit_drift_unread(response, shown.commit_drift_unread)
    elif shown.commit_drift_unchecked is not None:
        # The commit leg applies and nothing here read it (ENG-229).
        deps.responses.apply_commit_drift_unchecked(
            response, shown.commit_drift_unchecked
        )
    return response


def _apply_show_trust_and_standing(
    deps: ToolHandlers,
    memory: Memory,
    response: dict[str, Any],
    trust_unavailable: bool,
    verified_locally_at: str | None,
) -> None:
    """Take the assembled response; apply the trust rule and standing question."""
    # The trust rule (6.6.0), applied to the assembled response and
    # before the event so the log records what the caller was told: a
    # synced record whose stamp this host never made reads
    # `verification.status: "remote"` and spot_check_required.
    if trust_unavailable:
        # The store was opened without its key, so the rule below has
        # nothing to read: neither the label nor whether this host made
        # the stamp.
        # `provenance: null` alone said "not classified yet", which is a
        # different and benign state; this says the question could not
        # be asked, and demotes a stamp of unknown origin.
        deps.responses.apply_trust_unavailable(response)
    else:
        deps.responses.apply_trust(
            response,
            provenance=response["provenance"],
            verified_locally_at=verified_locally_at,
        )
    # The standing question (unit RS-F7 part 1) rides the response of a
    # record with no answer for its current body while the question is
    # asked: the agent has it in hand, and memory_update(standing=...)
    # takes the answer, through the standing roster when it makes the
    # record standing (ENG-112).
    if (
        _standing.asking()
        and _standing.answers(deps.store, [memory.id]).get(memory.id) is None
    ):
        response["standing_question"] = _standing.question(memory.id)
    # A standing record held out of force (ENG-200) says so, with the
    # call that reads it against the standing roster.
    if _standing.held_ids(deps.store, [memory.id]):
        response["held"] = True
        response["held_hint"] = (
            "This standing memory came back into force and is held out of "
            "force, not delivered and on no roster, until it is read against "
            f'the standing roster: memory_update(id="{memory.id}", '
            "standing=<0 to 1>) shows it the roster."
        )


def _links_payload(deps: ToolHandlers, memory: Any) -> dict[str, Any]:
    """Build the `links` + `reverse_links` payload for memory_show.

    Forward `links` come from the record itself. Reverse
    `reverse_links` come from the store's `memory_links` table
    (`Store.links_with_status`): every memory that links AT this id is
    a row keyed on `target_id`, so the lookup is O(k) on the
    number of reverse links rather than O(N) on the store size.
    Surfaced so a retrieval consumer sees the relationship both
    ways (e.g. "this memory is superseded by X" alongside "X
    supersedes this").

    Both lists are omitted when empty. Reverse links carry `source_id`,
    forward links `target_id`; the response groups each list by type and
    note (`_response._grouped_links`).

    The same read returns the record's trust row, so the dict also
    carries `provenance` (always present; None when the store holds no
    trust row for the id or was opened without its key), the private
    `_verified_locally_at` (this host's verification stamp) and
    `_trust_unavailable` (the store has no key, `Store.has_key`).
    `memory_show` lifts them into its own slots and applies the trust
    rule, or `apply_trust_unavailable` in its place.
    """

    out: dict[str, Any] = {}
    if memory.links:
        out["links"] = [
            {
                "type": link.type.value,
                "target_id": link.target_id,
                **({"note": link.note} if link.note is not None else {}),
            }
            for link in memory.links
        ]
    _outbound, inbound, trust = deps.store.links_with_status(memory.id)
    trust_unavailable = not deps.store.has_key
    out["provenance"] = trust.provenance if trust is not None else None
    out["_trust_unavailable"] = trust_unavailable
    out["_verified_locally_at"] = (
        trust.verified_locally_at if trust is not None else None
    )
    reverse: list[dict[str, Any]] = []
    for ltype, source_id, note in inbound:
        if source_id == memory.id:
            continue
        entry: dict[str, Any] = {"type": ltype, "source_id": source_id}
        if note is not None:
            entry["note"] = note
        reverse.append(entry)
    if reverse:
        out["reverse_links"] = reverse
    return out


__all__ = ["DESC_MEMORY_SHOW", "memory_show"]
