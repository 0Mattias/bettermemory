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
  memories then surface the link on every retrieval, and resolution
  happens through the normal verbs at the model's leisure
  (memory_verify the right one, memory_update or memory_remove the
  wrong one).
- verdict="compatible": the detector misfired (incidental negator,
  added-detail number). The pair is dismissed and stays dismissed —
  UNLESS either member's content later changes, which resurrects it.

Scans are cheap to trigger here (`scan=True`) and also run
automatically on every APPLYING consolidate pass (memory_curate /
the Stop-hook auto path), so the queue fills itself during normal
curation; this tool is mainly the drain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..conflicts import ConflictQueue, scan_conflicts
from ..models import LinkType, MemoryLink
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
    "need it), then marks the candidate confirmed. Follow up with the "
    "normal verbs: memory_verify the correct side, memory_update / "
    "memory_remove the wrong one. The link surfaces on both memories "
    "at retrieval either way.\n"
    "  - `verdict='compatible'` — detector misfire (incidental "
    "negator, added-detail number). Dismissed and stays dismissed "
    "unless either body later changes, which re-queues the pair.\n\n"
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
            "resolve requires verdict='contradiction' or 'compatible', "
            f"got {verdict!r}"
        )
    candidate = next((c for c in queue.pending() if c.id == candidate_id), None)
    if candidate is None:
        raise ValueError(
            f"no pending conflict candidate with id {candidate_id!r} "
            "(already resolved, or GC'd because a member was removed — "
            "call memory_conflicts() to list what's live)"
        )

    if verdict == "compatible":
        resolved = queue.resolve(candidate_id, status="dismissed", note=note)
        return {
            "id": candidate_id,
            "verdict": "compatible",
            "status": "dismissed" if resolved else "already_resolved",
        }

    # contradiction: write the link FIRST (its `updated` bump must land
    # before the verdict timestamp — see conflicts.py), then stamp.
    try:
        source = deps.store.load_one(candidate.a_id)
    except (MemoryNotFoundError, TombstonedError) as exc:
        raise ValueError(
            f"conflict member {candidate.a_id} is no longer active ({exc}); "
            "re-scan to GC the candidate"
        ) from exc
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
