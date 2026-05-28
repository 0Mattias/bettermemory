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
from ..health import curation_counts, find_prior_session_boundary
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
    "recently_removed_in_worktree, proposals_pending}`. "
    "`proposals_pending` is the count of write-reflex proposals the "
    "Stop hook has captured awaiting review via `memory_proposals` "
    "(0 unless the opt-in [proposals] auto_propose is on). "
    "`curation_pending` is an integer-count rollup the model "
    "should branch on:\n"
    "  {stale, never_verified, drifted, cold, dead, "
    "silent_misses, unique_silent_miss_memories, "
    "cold_endorsement_memories}\n"
    "Any non-zero `dead` or `drifted` is a cue to suggest a "
    "curation pass when the conversation has time. Non-zero "
    "`silent_misses` / `cold_endorsement_memories` means the "
    "audit-turn telemetry has actionable backlog. `silent_misses` "
    "counts events; `unique_silent_miss_memories` counts the "
    "distinct memories those misses pointed at (dedup'd by top-hit "
    "id) — the gap between the two flags `9 events against 1 "
    "memory` vs. `9 events across 9 memories`. Misses whose "
    "top-hit memory has been tombstoned are excluded from both "
    "counters. `cold_endorsement_memories` counts distinct "
    "memories (NOT turns) with `retrieval_count >= N` AND zero "
    "explicit applies — usually a sign the memory is over-surfaced "
    "or stale; one memory hit 50 times contributes 1, not 50.\n\n"
    "`recently_removed_in_worktree` is the integer count of "
    "tombstones removed in the trailing 7 days; under "
    "`auto_scope=True` it's filtered to this worktree (tombstones "
    "without an origin are excluded), under `auto_scope=False` it "
    "covers every tombstone in the window. Non-zero is a 'where "
    "did X go?' signal — material was deliberately trimmed here "
    "recently; don't blindly re-suggest it.\n\n"
    "`curation_pending_new_since_last_session` is the same shape, "
    "filtered to events emitted and memories *created* since the "
    "previous session ended (not memories that aged into a bucket; "
    "an older record aging into `stale` between sessions stays "
    "visible only in the absolute `curation_pending` view — note "
    "this is distinct from the separate `drifted` bucket, which "
    "tracks working-tree drift). Branch "
    "on this dict when deciding whether to *prompt* the user about "
    "curation — non-zero values here mean new rot has accumulated "
    "since you were last around, vs. the absolute `curation_pending` "
    "view which stays non-zero across sessions until each item is "
    "actually resolved. The field is `null` on the very first "
    "session (no prior boundary to delta against); fall back to "
    "`curation_pending` in that case.\n\n"
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

    # Tombstone activity in the last 7 days. Helps the model spot
    # "you removed N memories about this area last week" before it
    # re-covers ground already explicitly trimmed. Filtered by the
    # same auto_scope rule as the active count above: when
    # auto_scope=True and the caller is in a worktree, we restrict
    # the count to tombstones whose origin.worktree_root matches.
    # Tombstones without an origin (legacy or hand-edited) always
    # count under auto_scope=False; under auto_scope=True they are
    # excluded to keep the signal scoped tightly. The window mirrors
    # the curation_pending rollup's spirit (recent activity is what
    # matters, not the full lifetime of the store).
    recent_removed = _count_recent_tombstones(
        deps.store,
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
    }


def _count_recent_tombstones(
    store: Any,
    *,
    worktree_root: str | None,
    now: Any,
    window_days: int,
) -> int:
    """Count tombstones removed within the trailing window.

    When `worktree_root` is provided (auto_scope=True path), only
    tombstones whose `origin.worktree_root` matches are counted —
    tombstones without an origin are excluded under this branch
    because they can't be attributed to the current workspace. When
    `worktree_root` is None (auto_scope=False), every tombstone in
    the window counts.

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
            tomb_worktree = tomb_origin.worktree_root if tomb_origin else None
            if tomb_worktree != worktree_root:
                continue
        count += 1
    return count


__all__ = ["DESC_MEMORY_SCOPE_OVERVIEW", "memory_scope_overview"]
