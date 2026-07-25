"""memory_scope_overview MCP tool — handler + DESC.

Cheap session-start hint: per-scope counts plus a curation_pending
rollup the model branches on at the start of every conversation.
Calling memory_search after this is opt-in; calling memory_health is
the deep view when this surface flags something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._response import isoformat_optional
from ..events import iter_all_events
from ..conflicts import ConflictQueue, split_judgeable
from ..health import curation_counts, find_prior_session_boundary
from ..time_utils import parse_event_ts
from ..models import utcnow
from ..origin import Origin, should_include_for_caller
from ..proposals import ProposalQueue
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_SCOPE_OVERVIEW = (
    "Cheap session-start hint: per-scope counts, no bodies / "
    "ids / summaries. Call once at the start of a conversation; "
    "if `total` is 0, skip memory_search for the rest of the "
    "session unless explicitly asked.\n\n"
    "Returns `{current_repo, current_cwd, auto_scope, scopes: "
    "{scope: count}, total, disabled_scopes, curation_pending, "
    "curation_pending_new_since_last_session, "
    "recently_removed_in_worktree, proposals_pending, "
    "pending_writes}`. "
    "`proposals_pending` is the count of write-reflex proposals the "
    "Stop hook has captured awaiting review via `memory_proposals` "
    "(0 unless the opt-in [proposals] auto_propose is on). "
    "`pending_writes` is this session's staged writes awaiting "
    "memory_write_confirm/cancel — a dangling confirmation "
    "(silent 1h expiry). "
    "`curation_pending` is an integer-count rollup the model "
    "should branch on:\n"
    "  {stale, never_verified, drifted, cold, dead, "
    "silent_misses, unique_silent_miss_memories, "
    "cold_endorsement_memories, conflicts}\n"
    # Deliberately unqualified: the count is now exactly "pairs
    # memory_conflicts can still rule on" (see the handler's
    # `split_judgeable` call), so this one line stayed true when the
    # counter was fixed and costs no extra per-turn chars. The
    # cross-surface contract is spelled out where it is free —
    # DESC_MEMORY_CONFLICTS (full-surface only) and api.md.
    "`conflicts` = contradiction pairs awaiting a "
    "memory_conflicts verdict. "
    "Any non-zero `dead` or `drifted` is a cue to suggest a "
    "curation pass when the conversation has time. Non-zero "
    "`silent_misses` / `cold_endorsement_memories` means the "
    "audit-turn telemetry has actionable backlog. `silent_misses` "
    "counts events; `unique_silent_miss_memories` counts the "
    "distinct memories those misses pointed at (dedup'd by top-hit "
    "id). Misses whose "
    "top-hit memory has been tombstoned are excluded from both "
    "counters. `cold_endorsement_memories` counts distinct "
    "memories (NOT turns) with `retrieval_count >= N` AND zero "
    "explicit applies — usually a sign the memory is over-surfaced "
    "or stale.\n\n"
    "`recently_removed_in_worktree` is the integer count of "
    "tombstones removed in the trailing 7 days; under "
    "`auto_scope=True` it's filtered to this worktree (tombstones "
    "with no recorded worktree are excluded), under `auto_scope=False` "
    "it covers every tombstone in the window. Non-zero is a 'where "
    "did X go?' signal — material was deliberately trimmed here "
    "recently; don't blindly re-suggest it.\n\n"
    "`curation_pending_new_since_last_session` is the same shape, "
    "filtered to events emitted and memories *created* since the "
    "previous session ended (not memories that aged into a bucket; "
    "an older record aging into `stale` between sessions stays "
    "visible only in the absolute `curation_pending` view — note "
    "this is distinct from the separate `drifted` bucket, which "
    "tracks working-tree drift). Branch on it when deciding "
    "whether to *prompt* about curation — non-zero means new rot "
    "since the last session, vs. the absolute view which persists "
    "until resolved. `null` on the very first session — fall back "
    "to `curation_pending`.\n\n"
    "Default-scoped to the caller's current repository; memories "
    "with no origin always pass as global. Set `auto_scope=False` "
    "for the cross-project view. Counts respect session-disabled "
    "scopes."
)


async def memory_scope_overview(
    deps: "ToolHandlers",
    auto_scope: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    repo_filter: str | None = None
    worktree_filter: str | None = None
    current_origin: Origin | None = None
    if auto_scope:
        current_origin = _h.capture_origin()
        repo_filter = current_origin.repo
        worktree_filter = current_origin.worktree_root

    excluded = set(state.disabled_scopes)
    scope_counts: dict[str, int] = {}
    total = 0
    all_memories = deps.store.load_all()
    for memory in all_memories:
        memory_scope_set = set(memory.scopes)
        if excluded and (memory_scope_set & excluded):
            continue
        if repo_filter is not None:
            # `should_include_for_caller` is the single definition of
            # "this memory belongs to this caller's project" — shared
            # with memory_search and the health rollups so the model
            # can't see "5 memories tagged projects:foo" here and
            # zero hits in search and have no way to reconcile that.
            # Worktree filter rides through the same helper so the
            # two surface filters stay in sync; without it, two
            # worktrees of one repo would disagree about scope counts
            # vs. search hits, exactly the symmetry this helper exists
            # to enforce.
            if not should_include_for_caller(
                memory.origin,
                repo_filter,
                caller_worktree_root=worktree_filter,
            ):
                continue
        total += 1
        for scope in memory.scopes:
            if scope in excluded:
                continue
            scope_counts[scope] = scope_counts.get(scope, 0) + 1

    # Sort scopes by count desc, then name for determinism. Important
    # for tests and for the model — a stable ordering means a "if the
    # top scope is X" branch behaves consistently across calls.
    sorted_scopes = dict(sorted(scope_counts.items(), key=lambda kv: (-kv[1], kv[0])))

    # Curation pending — seven integer counts that surface "is there
    # anything worth a curation pass right now?" without the full
    # `memory_health` cost. Walks the event log once (same shape
    # health.compute_health does) but skips row materialisation.
    # Globally scoped — `auto_scope=True` only filters the per-repo
    # totals above; curation is always cross-repo because rot in
    # another scope is still rot. The caller-origin we feed in
    # drives the `drifted` count when available.
    #
    # We materialise the event stream once and run `curation_counts`
    # twice: once unbounded for the absolute view (`curation_pending`)
    # and once bounded to events newer than the prior session
    # boundary for the delta view
    # (`curation_pending_new_since_last_session`). Materialisation
    # is necessary because `find_prior_session_boundary` walks the
    # same stream the rollups consume — running the iterator twice
    # would do twice the file I/O. The list is bounded by the
    # active log + rotated archives, which is the same scale
    # `compute_health` already pays at session-start once.
    events_snapshot = list(iter_all_events(deps.store.root))
    # Pass tombstoned ids so `curation_counts` can drop silent-miss
    # events whose top-hit memory has been removed — same filter
    # `compute_health` applies. Without it the scope-overview
    # `silent_misses` count and the `memory_health.silent_misses` count
    # would diverge on the same store: the latter excludes
    # tombstone-targeted misses, the former wouldn't. The two surfaces
    # are read together by the model branching on session-start hints
    # and following up with the deep view, so they must agree.
    tombstoned_ids = {t.id for t in deps.store.load_tombstones()}
    curation = curation_counts(
        all_memories,
        events_snapshot,
        window_days=30,
        verification_stale_days=deps.config.behavior.verification_stale_days,
        cold_endorsement_ratio_threshold=(
            deps.config.behavior.cold_endorsement_ratio_threshold
        ),
        caller_origin=current_origin,
        tombstoned_ids=tombstoned_ids,
    )
    # `conflicts` rides on the same rollup but comes from the verdict
    # queue, not the event stream — pending memory-vs-memory
    # contradiction candidates awaiting a memory_conflicts ruling. One
    # small-file read; empty list when the queue was never created. The
    # rows are loaded once here and reused for the delta view below (a
    # pending candidate `created` after the prior session boundary is
    # "new since last session"), keeping the two views' key sets equal —
    # the model must not need a different branch per view.
    #
    # Judgeable rows only, via the same `split_judgeable` the
    # `memory_conflicts` listing filters through. This count is the cue
    # that sends the model to that tool, so counting a row the tool
    # cannot list or rule on (a member was removed since the last scan;
    # `_load_active_member` refuses the verdict) would advertise
    # arbitration work that resolves to an empty response — and a cue
    # that keeps resolving to nothing is a cue the model learns to skip.
    # Liveness comes from the `load_all` snapshot already in hand, so
    # the filter costs a set build and no extra reads; `memory_conflicts`
    # answers the same question with per-row `load_one`, which is why
    # only the predicate is shared and not the authority behind it.
    #
    # Excluded, never GC'd: dropping the row would need the queue's
    # lock and a rewrite from a read path every session-start call makes,
    # racing the scan for no gain — and `load_all` skips unparseable or
    # momentarily unreadable files, so "absent from the snapshot" is not
    # proof a memory died. `upsert_scan` stays the only collector; every
    # applying curation pass runs it unconditionally, and it applies that
    # same caveat to its own snapshot before collecting anything.
    active_ids = {m.id for m in all_memories}
    judgeable_conflicts, _unjudgeable_conflicts = split_judgeable(
        ConflictQueue(deps.store.root).pending(),
        lambda member_id: member_id in active_ids,
    )
    curation["conflicts"] = len(judgeable_conflicts)
    # Use the recorder's session_id, not `state.session_id`.
    # Every event the recorder writes is tagged `session =
    # self.session_id` (events.py:159) — that's the single
    # process-lifetime id, and it's the only `session` value
    # the event log carries. In single-client stdio mode the
    # two ids are equal (`state` is the registry's default
    # state, built with the same id the recorder reads at
    # construction); in multi-client SessionRegistry mode each
    # request has its own `state.session_id` but the recorder
    # still stamps its own id onto every event, so passing
    # `state.session_id` would treat every recorded event as
    # "from another session" and collapse the delta. The
    # "session" the delta talks about is the recorder
    # lifetime / process run, which is what's actually visible
    # in the log.
    prior_boundary = find_prior_session_boundary(
        events_snapshot,
        deps.recorder.session_id,
    )
    if prior_boundary is None:
        # First session ever, or the event log was wiped. The delta
        # view is undefined, not zero — surface it as null so the
        # model branches on "no baseline" vs. "nothing new" rather
        # than collapsing the two cases.
        curation_delta: dict[str, int] | None = None
    else:
        curation_delta = curation_counts(
            all_memories,
            events_snapshot,
            window_days=30,
            verification_stale_days=deps.config.behavior.verification_stale_days,
            cold_endorsement_ratio_threshold=(
                deps.config.behavior.cold_endorsement_ratio_threshold
            ),
            caller_origin=current_origin,
            since=prior_boundary,
            tombstoned_ids=tombstoned_ids,
        )
        # Delta arm of the queue-derived `conflicts` key: candidates
        # whose detection time postdates the boundary. Walks the same
        # judgeable rows the absolute view counted — a row nobody can
        # rule on is not new work either, and a delta that outran its
        # own absolute count would be its own phantom. A candidate with
        # an unparseable `created` counts as new — same conservatism as
        # the annotation walk's unprovable-ts handling, erring toward
        # surfacing.
        new_conflicts = 0
        for cand in judgeable_conflicts:
            created_ts = parse_event_ts(cand.created)
            if created_ts is None or created_ts > prior_boundary:
                new_conflicts += 1
        curation_delta["conflicts"] = new_conflicts

    # Tombstone activity in the last 7 days. Helps the model spot
    # "you removed N memories about this area last week" before it
    # re-covers ground already explicitly trimmed. Filtered through
    # the SAME `should_include_for_caller` rule as the active count
    # above — see `_count_recent_tombstones` for the one extra
    # precondition this surface adds. The window mirrors the
    # curation_pending rollup's spirit (recent activity is what
    # matters, not the full lifetime of the store).
    recent_removed = _count_recent_tombstones(
        deps.store,
        caller_repo=repo_filter,
        worktree_root=(
            current_origin.worktree_root if auto_scope and current_origin else None
        ),
        now=utcnow(),
        window_days=7,
    )

    # Count of pending write-reflex proposals (opt-in [proposals]
    # auto_propose). Surfaced here so the session-start hint also tells the
    # model when the Stop hook has captured durable statements awaiting
    # review via `memory_proposals`. Zero (and cheap) when the feature is
    # off and the queue file doesn't exist.
    proposals_pending = len(ProposalQueue(deps.store.root).load())

    # Unresolved staged writes for THIS session (user-inference writes
    # awaiting memory_write_confirm / memory_write_cancel, or any write
    # under require_write_confirmation). `_advance_turn` above already
    # evicted TTL-expired entries, so this is the live count. Surfaced
    # because the dogfood event log shows staged writes silently
    # expiring — the model staged, the conversation moved on, and
    # nothing ever re-surfaced the dangling confirmation.
    pending_writes = len(state.pending_writes)

    deps.recorder.record(
        "scope_overview",
        auto_scope=auto_scope,
        current_repo=repo_filter,
        total=total,
        scope_count=len(sorted_scopes),
        curation_pending=curation,
        curation_pending_new_since_last_session=curation_delta,
        prior_session_boundary=isoformat_optional(prior_boundary),
        recently_removed_in_worktree=recent_removed,
        proposals_pending=proposals_pending,
        pending_writes=pending_writes,
    )
    return {
        "current_repo": repo_filter,
        "current_cwd": current_origin.cwd if current_origin else None,
        "auto_scope": auto_scope,
        "scopes": sorted_scopes,
        "total": total,
        "disabled_scopes": sorted(state.disabled_scopes),
        "curation_pending": curation,
        "curation_pending_new_since_last_session": curation_delta,
        "recently_removed_in_worktree": recent_removed,
        "proposals_pending": proposals_pending,
        "pending_writes": pending_writes,
    }


def _count_recent_tombstones(
    store: Any,
    *,
    caller_repo: str | None,
    worktree_root: str | None,
    now: Any,
    window_days: int,
) -> int:
    """Count tombstones removed within the trailing window.

    When `worktree_root` is None — `auto_scope=False`, or a caller
    outside any git checkout — every tombstone in the window counts.

    Otherwise ownership is decided by `should_include_for_caller`, the
    same rule the active per-scope counts above and `memory_search`'s
    auto-scope run on, applied to the tombstone's own `origin` (a
    tombstone carries the origin of the memory it retired). Read that
    function for the rule; what is worth stating HERE is the one extra
    precondition this surface adds and why:

    * a tombstone with no `origin`, or an `origin` carrying no
      `worktree_root`, is excluded rather than passed. The shared rule
      treats a null worktree as "no boundary to enforce" and falls back
      to repo-level matching, which is right for the retrieval surfaces
      — hiding a legacy memory is a real loss. Here the opposite is
      right: this count answers "was material trimmed in THIS
      workspace", it is not a retrieval, and nothing is lost by
      declining to attribute an unattributable removal. It is also the
      contract `DESC_MEMORY_SCOPE_OVERVIEW` and `docs/api.md` state.

    Routing the rest through the shared rule is what makes this surface
    agree with the memory counts sitting beside it in the same
    response: a checkout that moved, was re-cloned, or arrived over
    `sync` from another machine keeps its own tombstone count instead of
    reporting 0, and a linked worktree sees its primary's removals —
    the same relaxations `worktrees_match` already gave the active
    counts. It inherits the shared rule's tolerated false negatives
    too (a remote URL rewritten since the removal reads as another
    project) — the direction `origin.py` names as the tolerated one,
    since under-counting a removal costs a nudge and over-counting one
    attributes another project's curation to this workspace.

    Defensive: a tombstone with a missing/malformed `removed`
    timestamp is skipped silently (treated as outside the window)
    rather than crashing the overview path.
    """
    from datetime import timedelta

    cutoff = now - timedelta(days=window_days)
    count = 0
    for tombstone in store.load_tombstones():
        if tombstone.removed is None or tombstone.removed < cutoff:
            continue
        if worktree_root is not None:
            tomb_origin = tombstone.origin
            if tomb_origin is None or tomb_origin.worktree_root is None:
                continue
            if not should_include_for_caller(
                tomb_origin,
                caller_repo,
                caller_worktree_root=worktree_root,
            ):
                continue
        count += 1
    return count


__all__ = ["DESC_MEMORY_SCOPE_OVERVIEW", "memory_scope_overview"]
