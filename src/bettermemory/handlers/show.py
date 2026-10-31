"""memory_show MCP tool — handler implementation + DESC.

The handler is a focused single-id read: load the memory, surface every
drift signal we can in one call (so the caller doesn't bounce back for
a second tool round-trip), issue a use-token so the auto-`record_use`
flow has something to commit on the next turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import standing as _standing
from .._response import isoformat, isoformat_optional
from ..models import utcnow
from ..store import MemoryNotFoundError, TombstonedError
from ..verdict import memory_verdict
from ._shared import Context, _advance_turn, caller_anchoring

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..models import Memory
    from ..verdict import MemoryVerdict


DESC_MEMORY_SHOW = (
    "One memory in full: body, links both ways, verification, path and "
    "commit drift, staleness_verdict. Read it before relying on a hit whose "
    "verdict is not fresh."
)


async def memory_show(
    deps: ToolHandlers, id: str, ctx: Context | None = None
) -> dict[str, Any]:
    """Body of the ``memory_show`` MCP tool."""
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    try:
        memory = deps.store.load_one(id)
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    shown = _show_verdict(deps, memory)
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
    response = _show_response(deps, memory, shown, index_reads, token_map[memory.id])
    _apply_show_trust_and_standing(
        deps, memory, response, trust_unavailable, verified_locally_at
    )
    deps.recorder.record(
        "show",
        id=memory.id,
        path_drift_checked=len(drift.checked),
        path_drift_missing=len(drift.missing),
        verification_status=response["verification"]["status"],
        staleness_verdict=response["staleness_verdict"],
        commit_drift_status=(commit_drift.status if commit_drift is not None else None),
        commits_since_verify=(
            commit_drift.commits_since_verify if commit_drift is not None else None
        ),
    )
    return response


def _show_verdict(deps: ToolHandlers, memory: Memory) -> MemoryVerdict:
    """Take the shown memory; return its verdict for the caller's checkout."""
    from .. import _handlers as _h

    # One verdict for show and search (`verdict.memory_verdict`): the
    # calendar status, path drift and the commit leg, rolled up into
    # `staleness_verdict`, with the commit leg read through the search's
    # per-hit memo, so a show of a row nothing has moved under forks no
    # git process.
    #
    # Path drift runs against the full body. We surface `path_drift`
    # only when there's something actionable: a memory with no path
    # claims (or all-healthy paths) returns the field as null so the
    # consumer can branch on `if path_drift is not None`. Without
    # that, every memory_show would carry an empty `path_drift` dict
    # and the model would learn to ignore the field even when it
    # mattered. `verified_paths` is threaded in so a path the user
    # has previously attested gets surfaced in `path_drift.verified`
    # even when no other claims drift.
    #
    # Verification staleness is structurally always present — emitted
    # even for "fresh" memories — because consistent shape means the
    # consumer can branch on `verification.status` without an
    # existence check. The recommendation field is null on fresh,
    # populated otherwise; that's the actionable handle.
    #
    # Commit-drift is the cwd-aware sibling of verification: when the
    # caller's project is the memory's (its home, through the LINKS
    # admission: `Admission.drift_counts`, design 3.5), count commits
    # authored since the last verify in the caller's checkout.
    # Stays null when the memory is another project's or the
    # memory has no anchor to count from — emitting an "unknown"
    # branch every consumer would have to filter is worse than
    # silence, mirroring path_drift's null-when-clean contract.
    # The count is claim-anchored: narrowed to commits that touched
    # the memory's cited/attested paths (a memory anchored to
    # `[/etc/foo]` reads as `clean` when the project moved but
    # `/etc/foo` didn't), and not applicable at all — None — for a
    # memory whose body cites no paths this repo's commits could
    # invalidate (`body` feeds the anchor derivation).
    #
    # The verdict escalates on the claim-anchored path subset only:
    # attested paths that vanished, and citations resolved against this
    # memory's own recorded worktree. The full `missing` list is still
    # emitted in `_show_response`'s `path_drift` block — prose absences stay
    # visible as evidence, they just no longer raise the tier. Rationale
    # and the 15-vs-3 measurement live on `verdict_from_signals`.
    # The caller is resolved once (design 3.1): its anchoring reads the
    # record's claims where design 3.6 reads them, and its LINKS admission
    # decides whether the commit leg is counted here at all.
    from ..admission import Admission
    from ..membership import LINKS

    anchoring, caller = caller_anchoring(deps.store, deps.config)
    links = Admission.of(LINKS, caller)
    return memory_verdict(
        memory,
        caller_origin=_h.capture_origin() if links.drift_counts(memory.id) else None,
        now=utcnow(),
        stale_after_days=deps.config.behavior.verification_stale_days,
        anchoring=anchoring,
        admission=links,
    )


def _show_response(
    deps: ToolHandlers,
    memory: Memory,
    shown: MemoryVerdict,
    index_reads: dict[str, Any],
    use_token: str,
) -> dict[str, Any]:
    """Take the memory, verdict, token and index reads (popped in place); return the response."""
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
        # Who wrote it — the full shape, nulls included, so an absent
        # attestation reads as recorded rather than omitted. The key
        # itself is omitted when the writer declared nothing (identity).
        **(
            {"actor": actor_dict}
            if (actor_dict := deps.responses.actor_to_dict(memory.actor)) is not None
            else {}
        ),
        "path_drift": (
            drift.to_dict()
            if (drift.has_drift or drift.verified or drift.expected_absent)
            else None
        ),
        "commit_drift": (commit_drift.to_dict() if commit_drift is not None else None),
        "use_token": use_token,
        # Corroboration rollup — omitted while zero (absence-as-signal,
        # matching the frontmatter shape).
        **(
            {
                "corroborations": memory.corroborations,
                "last_corroborated": isoformat_optional(memory.last_corroborated),
            }
            if memory.corroborations
            else {}
        ),
        "verified_paths": list(memory.verified_paths),
        "verified_commits": list(memory.verified_commits),
        "verified_versions": list(memory.verified_versions),
        "verified_absent_paths": list(memory.verified_absent_paths),
        "claims": list(memory.claims),
        # The stamp's commit anchor, beside the attestations it was
        # stamped with; `null` on a record verified without a checkout
        # or before the field existed (the `commit_drift` block's
        # `basis` says which count that record gets).
        "verified_head": memory.verified_head,
        **index_reads,
    }
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

    Both lists are omitted when empty — absence-as-signal, so a
    consumer branches on key presence. That is the same contract the
    `corroborations` pair in the caller's return literal uses, and the
    OPPOSITE of the one `path_drift` / `commit_drift` take: those two
    keys are always emitted, with `null` as the no-signal value. Reverse
    links carry `source_id` so the consumer can navigate to the linking
    memory; forward links carry `target_id`.

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
