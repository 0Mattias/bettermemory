"""memory_conflicts MCP tool — handler implementation + DESC.

The corpus-inference arbitration surface. The server detects
memory-vs-memory disagreement candidates mechanically (high dedup-scan
similarity + a polarity flip or numeric divergence — see
`conflicts.py`); this tool is where the model LISTS them with both
bodies inline, and rules on each pair:

- verdict="contradiction": the pair genuinely disagrees. A
  `contradicts` link is written (a→b, with the note) BEFORE the verdict
  is stamped — ordering matters: the link-write bumps `updated`, and
  stamping the verdict afterwards keeps the resurrect rule quiet. Both
  members must still be ACTIVE for that to mean anything (see
  `_load_active_member`). Both memories then surface the link on every
  retrieval, and resolution happens through the normal verbs at the
  model's leisure (memory_verify the right one, memory_update or
  memory_remove the wrong one).
- verdict="compatible": the detector misfired (incidental negator,
  added-detail number). Any `contradicts` link between the pair is
  cleared first — the queue and the link layer are two authorities on
  the same question and a dismissal that left the edge standing would
  leave them permanently disagreeing — then the pair is dismissed and
  stays dismissed UNLESS either member's content later changes, which
  resurrects it.

Scans are cheap to trigger here (`scan=True`) and also run
automatically on every APPLYING consolidate pass (memory_curate /
the Stop-hook auto path), so the queue fills itself during normal
curation; this tool is mainly the drain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..conflicts import ConflictQueue, scan_conflicts
from ..models import LinkType, Memory, MemoryLink
from ..store import ConcurrentUpdateError, MemoryNotFoundError, TombstonedError
from ._shared import Context, _advance_turn
from .write import _resolve_dedup_thresholds

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_CONFLICTS = (
    "List and arbitrate memory-vs-memory contradiction candidates the "
    "corpus scan flagged (near-identical pairs with a negation flip or "
    "a numeric divergence — 'port 5432' vs 'port 5433'). The server "
    "detects mechanically; YOU judge.\n\n"
    "Modes (mutually exclusive):\n"
    "- default (no args): list pending candidates, both bodies "
    "inlined, strongest similarity first.\n"
    "- `scan=True`: run detection over the active set now and merge "
    "new candidates into the queue (also happens automatically on "
    "every applying memory_curate / auto-consolidate pass). Returns "
    "the merge counters plus the pending list.\n"
    "- `resolve=<candidate_id>` + `verdict`: rule on one pair.\n"
    "  - `verdict='contradiction'` — genuinely disagree. Writes a "
    "`contradicts` link (a→b; pass `note` with WHY — future curators "
    "need it), then marks the candidate confirmed. Refused when either "
    "member is no longer active (re-scan to GC the row). Follow up "
    "with the normal verbs: memory_verify the correct side, "
    "memory_update / memory_remove the wrong one. The link surfaces on "
    "both memories at retrieval either way.\n"
    "  - `verdict='compatible'` — detector misfire (incidental "
    "negator, added-detail number). Clears any `contradicts` link "
    "between the pair (echoed as `links_cleared`), then dismisses; "
    "stays dismissed unless either body later changes, which re-queues "
    "the pair.\n\n"
    "Judging tips: read both bodies, not the summaries. For numeric "
    "pairs, 'compatible' is right when the numbers describe different "
    "things (two ports of two services); 'contradiction' when they "
    "describe the same thing at different values (one service, two "
    "claimed ports — one memory is stale). Never resolve on similarity "
    "alone.\n\n"
    "Returns `{pending: [...], pending_total}` (+ `scan` counters or "
    "the `resolved` echo). Each pending row: `{id, a, b, similarity, "
    "method, detector}` where a/b are `{id, body, scopes, updated}`."
)


async def memory_conflicts(
    deps: "ToolHandlers",
    scan: bool = False,
    resolve: str | None = None,
    verdict: str | None = None,
    note: str | None = None,
    max_results: int = 10,
    ctx: Context | None = None,
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if resolve is not None and scan:
        raise ValueError("pass either scan=True or resolve=<id>, not both")
    if max_results < 1:
        raise ValueError("max_results must be a positive integer")

    queue = ConflictQueue(deps.store.root)
    out: dict[str, Any] = {}

    if resolve is not None:
        out["resolved"] = _resolve_verdict(
            deps, queue, candidate_id=resolve, verdict=verdict, note=note
        )
    elif scan:
        semantic_model, high_threshold, _medium = _resolve_dedup_thresholds(deps)
        counters = scan_conflicts(
            deps.store.root,
            deps.store.load_all(),
            semantic_model=semantic_model,
            threshold=high_threshold,
        )
        out["scan"] = counters
        deps.recorder.record("conflict_scan", **counters)

    pending = queue.pending()
    pending.sort(key=lambda c: c.similarity, reverse=True)
    rows: list[dict[str, Any]] = []
    for cand in pending[:max_results]:
        sides: dict[str, Any] = {}
        for key, mid in (("a", cand.a_id), ("b", cand.b_id)):
            try:
                m = deps.store.load_one(mid)
            except (MemoryNotFoundError, TombstonedError):
                sides = {}
                break
            sides[key] = {
                "id": m.id,
                "body": m.body,
                "scopes": m.scopes,
                "updated": m.updated.isoformat(),
            }
        if not sides:
            # A member vanished since the last scan; the next full scan
            # GCs the row. Skip rather than surface a one-sided pair.
            continue
        rows.append(
            {
                "id": cand.id,
                "similarity": cand.similarity,
                "method": cand.method,
                "detector": cand.detector,
                **sides,
            }
        )
    out["pending"] = rows
    out["pending_total"] = len(pending)
    if not rows and resolve is None and not scan:
        out["hint"] = (
            "No pending conflict candidates. Run memory_conflicts(scan=True) "
            "after bulk writes, or rely on the automatic scan every applying "
            "curation pass performs."
        )
    return out


def _load_active_member(deps: "ToolHandlers", memory_id: str) -> Memory:
    """Load one conflict member, refusing when it is no longer active.

    The refusal names the remedy (a re-scan GCs rows whose members died)
    because that is the only way out: the candidate stays pending until
    a full-corpus `upsert_scan` drops it, and re-issuing the verdict
    would keep failing the same way.
    """
    try:
        return deps.store.load_one(memory_id)
    except (MemoryNotFoundError, TombstonedError) as exc:
        raise ValueError(
            f"conflict member {memory_id} is no longer active ({exc}); "
            "re-scan (memory_conflicts(scan=True)) to GC the candidate"
        ) from exc


def _clear_contradicts_links(
    deps: "ToolHandlers", a_id: str, b_id: str
) -> list[dict[str, str]]:
    """Drop `contradicts` edges between the pair, in BOTH directions.

    The confirm path only ever writes a→b, but the relation is symmetric
    per the `LinkType` contract and retrieval annotates from either
    direction (`_response.attach_link_annotations`), so a hand-written
    b→a edge is just as live and has to go too. Mirrors the confirm-side
    write down to `preserve_verification=True`: dropping a link is a
    metadata edit, and clobbering a `mark_verified` that landed
    concurrently would cost an attestation the verdict never judged.

    A member that is already gone is skipped rather than refused — a
    compatible verdict on a moot pair is harmless and the row is GC'd by
    the next scan either way. Returns one `{source, target}` row per
    memory actually rewritten.
    """
    cleared: list[dict[str, str]] = []
    for source_id, target_id in ((a_id, b_id), (b_id, a_id)):
        try:
            source = deps.store.load_one(source_id)
        except (MemoryNotFoundError, TombstonedError):
            continue
        remaining = [
            link
            for link in source.links
            if not (link.type == LinkType.CONTRADICTS and link.target_id == target_id)
        ]
        if len(remaining) == len(source.links):
            continue
        try:
            deps.store.update(
                source.model_copy(update={"links": remaining}),
                preserve_verification=True,
            )
        except ConcurrentUpdateError as exc:
            raise ValueError(
                f"memory {source_id} changed concurrently; re-fetch via "
                f"memory_show and retry the verdict ({exc})"
            ) from exc
        cleared.append({"source": source_id, "target": target_id})
    return cleared


def _resolve_verdict(
    deps: "ToolHandlers",
    queue: ConflictQueue,
    *,
    candidate_id: str,
    verdict: str | None,
    note: str | None,
) -> dict[str, Any]:
    if verdict not in ("contradiction", "compatible"):
        raise ValueError(
            f"resolve requires verdict='contradiction' or 'compatible', got {verdict!r}"
        )
    candidate = next((c for c in queue.pending() if c.id == candidate_id), None)
    if candidate is None:
        raise ValueError(
            f"no pending conflict candidate with id {candidate_id!r} "
            "(already resolved, or GC'd because a member was removed — "
            "call memory_conflicts() to list what's live)"
        )

    if verdict == "compatible":
        # Two authorities rule on the same question — this queue and the
        # `contradicts` link layer retrieval annotates from. A dismissal
        # that left a standing edge in place (written by an earlier
        # `contradiction` verdict on the resurrected pair, or by hand via
        # memory_update) would leave them permanently disagreeing: the
        # queue calls the pair settled while every retrieval keeps
        # flagging it, and nothing ever re-raises it for arbitration.
        # Clear BEFORE stamping, for the same ordering reason the
        # confirm path writes before stamping: the clear bumps `updated`,
        # and a bump that postdates `verdict_ts` would resurrect the row
        # on the very next scan.
        cleared = _clear_contradicts_links(deps, candidate.a_id, candidate.b_id)
        resolved = queue.resolve(candidate_id, status="dismissed", note=note)
        out: dict[str, Any] = {
            "id": candidate_id,
            "verdict": "compatible",
            "status": "dismissed" if resolved else "already_resolved",
            "links_cleared": cleared,
        }
        if cleared:
            out["hint"] = (
                "A stale `contradicts` link between the pair was removed, so "
                "retrieval no longer annotates the two as disagreeing. If that "
                "was wrong, re-link explicitly via memory_update."
            )
        return out

    # contradiction: write the link FIRST (its `updated` bump must land
    # before the verdict timestamp — see conflicts.py), then stamp.
    # BOTH members must still be active, not just the link's source: a
    # link whose target is tombstoned resolves to nothing at annotation
    # time (`_response._resolve` skips missing/tombstoned targets) and
    # the next full scan GCs the queue row, so the verdict's only
    # durable artifact would be invisible from the moment it was made.
    source = _load_active_member(deps, candidate.a_id)
    _load_active_member(deps, candidate.b_id)
    already = any(
        link.type == LinkType.CONTRADICTS and link.target_id == candidate.b_id
        for link in source.links
    )
    if not already:
        new_link = MemoryLink(
            type=LinkType.CONTRADICTS,
            target_id=candidate.b_id,
            note=note or f"memory_conflicts verdict ({candidate.detector} detector)",
        )
        try:
            deps.store.update(
                source.model_copy(update={"links": [*source.links, new_link]}),
                preserve_verification=True,
            )
        except ConcurrentUpdateError as exc:
            raise ValueError(
                f"memory {candidate.a_id} changed concurrently; re-fetch via "
                f"memory_show and retry the verdict ({exc})"
            ) from exc
    resolved = queue.resolve(candidate_id, status="confirmed", note=note)
    deps.recorder.record(
        "conflict_verdict",
        candidate=candidate_id,
        verdict="contradiction",
        a=candidate.a_id,
        b=candidate.b_id,
        link_written=not already,
    )
    return {
        "id": candidate_id,
        "verdict": "contradiction",
        "status": "confirmed" if resolved else "already_resolved",
        "link_written": not already,
        "hint": (
            "The pair is now linked contradicts-wise and both sides "
            "surface it at retrieval. Resolve the substance next: "
            "memory_verify the side that matches reality, then "
            "memory_update or memory_remove the other."
        ),
    }
