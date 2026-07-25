"""memory_conflicts MCP tool — handler implementation + DESC.

The corpus-inference arbitration surface. The server detects
memory-vs-memory disagreement candidates mechanically (high dedup-scan
similarity + a polarity flip or numeric divergence — see
`conflicts.py`); this tool is where the model LISTS them with both
bodies inline, and rules on each pair:

- verdict="contradiction": the pair genuinely disagrees. A
  `contradicts` link is written (a→b, with the note) BEFORE the verdict
  is stamped. Both members must still be ACTIVE for that to mean
  anything (see `_load_active_member`). Both memories then surface the
  link on every retrieval, and resolution happens through the normal
  verbs at the model's leisure (memory_verify the right one,
  memory_update or memory_remove the wrong one).
- verdict="compatible": the detector misfired (incidental negator,
  added-detail number). Any `contradicts` link between the pair is
  cleared first — the queue and the link layer are two authorities on
  the same question and a dismissal that left the edge standing would
  leave them permanently disagreeing — then the pair is dismissed and
  stays dismissed UNLESS either member's BODY later changes, which
  resurrects it.

Both verdicts hand `queue.resolve` the member bodies they just read, so
the row records what it actually judged. That is what makes the
resurrect rule immune to this very surface: both branches REWRITE
memories (one adds a link, the other strips one) and both bumps land on
memories that sit in other queued pairs too, so a rule keyed on
`updated` had arbitrating one pair re-queue unrelated dismissed ones.
Both branches also record a `conflict_verdict` event — a verdict that
rewrites memories has to be as visible in the log as every other
mutating operation, and the compatible branch mutates just as surely as
the contradiction branch does.

Scans are cheap to trigger here (`scan=True`) and also run
automatically on every APPLYING consolidate pass (memory_curate /
the Stop-hook auto path), so the queue fills itself during normal
curation; this tool is mainly the drain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..conflicts import (
    ConflictCandidate,
    ConflictQueue,
    scan_conflicts,
    split_judgeable,
)
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
    "the merge counters plus the pending list. One counter there — "
    "`pending_rows_on_disk` — counts rows in the queue FILE, not "
    "judgeable work, so it can exceed the `pending_total` beside it; "
    "when a row's dead member is what put it above, `hint` says how "
    "many and why the scan left them there.\n"
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
    "the pair. Editing a memory's links or metadata is not a body "
    "change and will not re-queue anything.\n\n"
    "Judging tips: read both bodies, not the summaries. For numeric "
    "pairs, 'compatible' is right when the numbers describe different "
    "things (two ports of two services); 'contradiction' when they "
    "describe the same thing at different values (one service, two "
    "claimed ports — one memory is stale). Never resolve on similarity "
    "alone.\n\n"
    "Returns `{pending: [...], pending_total}` (+ `scan` counters or "
    "the `resolved` echo). Each pending row: `{id, a, b, similarity, "
    "method, detector}` where a/b are `{id, body, scopes, updated}`. "
    "`pending_total` counts the candidates that are judgeable right now "
    "(both members still active), so it exceeds the number of rows "
    "returned only when `max_results` truncated the list. A queued row "
    "whose member died since the last scan is in neither the list nor "
    "the total — nor in the `curation_pending.conflicts` count "
    "memory_scope_overview points here with — and `hint` says how many "
    "were left out."
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
    counters: dict[str, int] | None = None

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
    rows, listable, omitted = _render_pending(deps, pending, max_results=max_results)
    out["pending"] = rows
    out["pending_total"] = listable
    if resolve is None:
        hint = _pending_hint(omitted, listed=bool(rows), scan_counters=counters)
        if hint is not None:
            out["hint"] = hint
    return out


# The two numbers a `hint` has to hold apart, and the remedies for the
# gap between them. Assembled rather than inlined per branch: the
# omitted-row explanation is one fact and the mode only changes what to
# do about it, so the modes cannot drift into two accounts of the same
# state.
_UNJUDGEABLE_ROWS = (
    "{n} queued candidate(s) name a memory that is no longer active, so "
    "they are omitted from `pending` and `pending_total` — a one-sided pair "
    "cannot be judged. No count of arbitration work advertises them: "
    "memory_scope_overview's curation_pending.conflicts runs the same rows "
    "through the same filter. A scan's `pending_rows_on_disk` is the counter "
    "that does include them, because it counts the queue FILE rather than "
    "judgeable work."
)
_COLLECT_BY_SCANNING = (
    "The rows sit in the queue until a full scan collects them: "
    "memory_conflicts(scan=True), or the automatic scan every applying "
    "curation pass runs. A scan collects nothing and reports "
    "`gc_deferred=1` instead whenever it cannot prove its snapshot "
    "accounted for every `.md` file under the store root — a file it could "
    "not read must not look like a dead conflict member and take a settled "
    "verdict with it."
)
_GC_WAS_DEFERRED = (
    "The scan just run reported `gc_deferred=1`: it could not prove its "
    "snapshot accounted for every `.md` file under the store root, so it "
    "collected nothing rather than delete a settled verdict on the evidence "
    "of a bad read. These rows are what it left behind; the next pass that "
    "reads the store completely collects the ones whose member is really "
    "gone."
)
_GC_RAN_ANYWAY = (
    "The scan just run did collect (`gc_deferred=0`), so these rows turned "
    "one-sided only after its snapshot — a member removed, or a file that "
    "stopped reading, in the moment since. The next scan collects them or "
    "lists them again, depending on which it was."
)
_NOTHING_PENDING = (
    "No pending conflict candidates. Run memory_conflicts(scan=True) "
    "after bulk writes, or rely on the automatic scan every applying "
    "curation pass performs."
)


def _pending_hint(
    omitted: int,
    *,
    listed: bool,
    scan_counters: dict[str, int] | None,
) -> str | None:
    """The prose beside the counts — `None` when the numbers stand alone.

    Said once, in the one place that knows how many rows the listing had
    to leave out. `scan_counters` is the merge result when this call ran
    a scan and `None` when it only listed, which decides the remedy: a
    scan cannot be prescribed as the fix by the response of a scan that
    just ran.

    Scan mode gets this hint at all because it is the only mode whose
    payload carries the raw queue-file count (`pending_rows_on_disk`)
    beside the judgeable `pending_total`, and a scan that deferred
    collection is exactly the pass that leaves those two apart. Silence
    there left one payload holding two different numbers for two
    different questions with nothing naming which was which.

    The empty-queue nudge stays list-only: prescribing a scan in the
    response of the scan that just ran is noise.
    """
    if omitted:
        if scan_counters is None:
            remedy = _COLLECT_BY_SCANNING
        elif scan_counters.get("gc_deferred"):
            remedy = _GC_WAS_DEFERRED
        else:
            remedy = _GC_RAN_ANYWAY
        return f"{_UNJUDGEABLE_ROWS.format(n=omitted)} {remedy}"
    if not listed and scan_counters is None:
        return _NOTHING_PENDING
    return None


def _render_pending(
    deps: "ToolHandlers",
    pending: list[ConflictCandidate],
    *,
    max_results: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Render pending candidates into response rows.

    Returns `(rows, listable, omitted)`: the rows inside the
    `max_results` window, how many candidates could have been rendered
    in total, and how many were left out because a member no longer
    loads.

    `listable` — not `len(pending)` — is what the handler reports as
    `pending_total`, and the whole queue is walked to compute it rather
    than just the window. A row whose member vanished since the last
    scan cannot be judged (`_load_active_member` refuses the verdict), so
    counting it would put a nonzero total beside the very list it is
    missing from, next to a hint saying there is nothing to do. That
    number is what the model reads to decide whether arbitration work
    exists; a count that disagrees with its own list is worth more than
    the extra indexed loads walking the tail costs. Rows past the window
    still count — the total is deliberately allowed to exceed
    `len(rows)`, which is how a caller learns to raise `max_results`.

    The judgeable/omitted split itself is `conflicts.split_judgeable`,
    shared with `memory_scope_overview`'s `curation_pending.conflicts`
    counter — the session-start cue that sends the model *here*. Each
    surface supplies its own liveness authority (per-row `load_one`
    below; a full-corpus snapshot there) but neither owns the rule,
    because a cue that points at an empty list is the same phantom
    count in a second place.

    Dead rows are reported, never GC'd here. `upsert_scan` stays the
    queue's only garbage collector: it rules on liveness from one
    full-corpus snapshot, and only after checking that snapshot is
    COMPLETE against the root's file count — while this path has only
    per-row `load_one` misses, which a momentarily unparseable or
    unreadable file also produces. Deleting arbitration state from a
    read path on that evidence trades a phantom count (now fixed at the
    reporting layer) for lost judgment, and turns every list call into a
    locked rewrite racing the scan. The rows stay short-lived in the
    normal case: every applying curation pass calls the collector, which
    collects unless the store has a file it could not read.
    """
    live_sides: dict[str, dict[str, Any]] = {}
    dead_ids: set[str] = set()

    def is_active(memory_id: str) -> bool:
        """Liveness by per-row `load_one`, memoized in both directions —
        a memory in two candidate pairs is loaded once, and a dead id
        costs its full-directory `load_one` walk once. The rendered side
        falls out of the same load, so a judgeable row never re-reads to
        build its row."""
        if memory_id in live_sides:
            return True
        if memory_id in dead_ids:
            return False
        try:
            m = deps.store.load_one(memory_id)
        except (MemoryNotFoundError, TombstonedError):
            dead_ids.add(memory_id)
            return False
        live_sides[memory_id] = {
            "id": m.id,
            "body": m.body,
            "scopes": m.scopes,
            "updated": m.updated.isoformat(),
        }
        return True

    judgeable, omitted = split_judgeable(pending, is_active)
    rows = [
        {
            "id": cand.id,
            "similarity": cand.similarity,
            "method": cand.method,
            "detector": cand.detector,
            # Present in `live_sides` by construction: `split_judgeable`
            # only keeps a row once `is_active` has loaded both members.
            "a": dict(live_sides[cand.a_id]),
            "b": dict(live_sides[cand.b_id]),
        }
        for cand in judgeable[:max_results]
    ]
    return rows, len(judgeable), omitted


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


def _member_bodies(
    deps: "ToolHandlers", candidate: ConflictCandidate
) -> dict[str, str]:
    """`{memory_id: body}` for the pair, for the verdict's fingerprint.

    A member that no longer loads is simply ABSENT from the mapping
    rather than an error: a `compatible` verdict on a half-dead pair is
    harmless (`_clear_contradicts_links` takes the same line) and the row
    is GC'd by the next complete scan anyway. `ConflictQueue.resolve`
    then leaves that side hashless, on the `updated > verdict_ts`
    fallback — the honest answer when there is no body to fingerprint.

    Reads separately from `_clear_contradicts_links` rather than sharing
    its load: that one re-reads under its own CAS discipline, and a body
    is identical either side of a links-only rewrite, so the two extra
    indexed `load_one`s buy simplicity at no correctness cost.
    """
    bodies: dict[str, str] = {}
    for memory_id in (candidate.a_id, candidate.b_id):
        try:
            bodies[memory_id] = deps.store.load_one(memory_id).body
        except (MemoryNotFoundError, TombstonedError):
            continue
    return bodies


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
        # Snapshot the bodies BEFORE mutating anything: they are what
        # this verdict judged, and the fingerprint the row records is
        # what a later scan compares against to decide whether the pair
        # deserves re-arbitration. Clearing links below does not touch a
        # body, so before/after would hash the same — reading first just
        # keeps "what was judged" and "what was recorded" the same act.
        bodies = _member_bodies(deps, candidate)
        # Two authorities rule on the same question — this queue and the
        # `contradicts` link layer retrieval annotates from. A dismissal
        # that left a standing edge in place (written by an earlier
        # `contradiction` verdict on the resurrected pair, or by hand via
        # memory_update) would leave them permanently disagreeing: the
        # queue calls the pair settled while every retrieval keeps
        # flagging it, and nothing ever re-raises it for arbitration.
        # Clear BEFORE stamping, matching the confirm path's ordering:
        # with body fingerprints recorded the clear's `updated` bump can
        # no longer resurrect this row whichever way round it lands, but
        # a row that falls back to the timestamp rule still needs it.
        cleared = _clear_contradicts_links(deps, candidate.a_id, candidate.b_id)
        resolved = queue.resolve(
            candidate_id, status="dismissed", note=note, member_bodies=bodies
        )
        # Same event the contradiction branch records, and for the same
        # reason: this branch rewrote up to two memories and retired a
        # queue row, so leaving it out of the log made a mutating
        # operation the one invisible thing in an audit trail every other
        # mutator writes to.
        deps.recorder.record(
            "conflict_verdict",
            candidate=candidate_id,
            verdict="compatible",
            a=candidate.a_id,
            b=candidate.b_id,
            memories_rewritten=len(cleared),
        )
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
    target = _load_active_member(deps, candidate.b_id)
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
    # `source.body` is pre-link-write, which is the same body: the write
    # above only replaces `links`. Recorded for symmetry and forensics —
    # `confirmed` is terminal, so no scan ever consults these hashes.
    resolved = queue.resolve(
        candidate_id,
        status="confirmed",
        note=note,
        member_bodies={candidate.a_id: source.body, candidate.b_id: target.body},
    )
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
