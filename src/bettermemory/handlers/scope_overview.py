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
from ..origin import Origin, should_include_for_caller
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
    "curation_pending_new_since_last_session}`. "
    "`curation_pending` is an integer-count rollup the model "
    "should branch on:\n"
    "  {stale, never_verified, drifted, cold, dead, "
    "silent_misses, endorsement_debt}\n"
    "Any non-zero `dead` or `drifted` is a cue to suggest a "
    "curation pass when the conversation has time. Non-zero "
    "`silent_misses` / `endorsement_debt` means the audit-turn "
    "telemetry has actionable backlog.\n\n"
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
    sorted_scopes = dict(
        sorted(scope_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )

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
    curation = curation_counts(
        all_memories,
        events_snapshot,
        window_days=30,
        verification_stale_days=deps.config.behavior.verification_stale_days,
        caller_origin=current_origin,
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
            caller_origin=current_origin,
            since=prior_boundary,
        )

    deps.recorder.record(
        "scope_overview",
        auto_scope=auto_scope,
        current_repo=repo_filter,
        total=total,
        scope_count=len(sorted_scopes),
        curation_pending=curation,
        curation_pending_new_since_last_session=curation_delta,
        prior_session_boundary=isoformat_optional(prior_boundary),
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
    }


__all__ = ["DESC_MEMORY_SCOPE_OVERVIEW", "memory_scope_overview"]
