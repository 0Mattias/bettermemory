"""episode_search MCP tool — handler implementation + DESC.

Cross-session lookup for journal-shaped entries. Unlike
`memory_search`, episode_search isn't ranked — episodes are
chronological by design and the model is usually filtering by
session/scope/time rather than asking "which one is most relevant".

Use cases:
- "what did I conclude about scope X across the last few sessions?"
- "what episodes did I write since timestamp T?"
- "list all takeaways from a specific session" (covered by
  episode_handoff with explicit `prior_session_id` for the common
  case, but episode_search is the no-cap form).

Excluded surfaces: episodes still don't appear in memory_search /
memory_health / memory_list — this is a dedicated episodic-tier read.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ._shared import Context, _advance_turn
from ..time_utils import parse_event_ts

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..models import Episode


DESC_EPISODE_SEARCH = (
    "Cross-session lookup for journal-shaped entries (episodes). "
    "Unlike memory_search, this is NOT ranked — episodes are "
    "chronological and the filter set (scope / since / session_id) "
    "is the discovery surface.\n\n"
    "Use this for ad-hoc journal lookup, e.g. 'what did I conclude "
    "about projects:auth across the last few sessions?'. For the "
    "loop-iteration-entry case prefer `episode_handoff`, which "
    "auto-resolves the prior session and caps the surface.\n\n"
    "Returns the matching episodes oldest first within the "
    "most-recent-`max_results` window. When the filter set "
    "produces more matches than the cap, the cap surfaces the "
    "MOST-RECENT N (the slice keeps oldest-first ordering inside "
    "that window — 'what did I conclude across the last few "
    "sessions?' reads the tail, not the head).\n\n"
    "Each row carries `{id, session_id, created, takeaway, body, "
    "scopes, swarm_id}`. `session_id` is included because "
    "episode_search spans sessions (unlike episode_handoff which "
    "scopes to one), so the caller can correlate a takeaway back to "
    "its originating session directory; `swarm_id` (may be null) is "
    "the cohort tag for multi-agent fan-in.\n\n"
    "MULTI-AGENT SWARM FAN-IN: when you fan out parallel sub-agents "
    "and pass each the coordinator's session id as `swarm_id` on "
    "`episode_write`, call `episode_search(swarm_id=<coordinator id>)` "
    "to gather EVERY sub-agent's takeaways across their individual "
    "session directories in one read — 'what did all my sub-agents "
    "conclude.' This is the N:1 cohort read, distinct from "
    "episode_handoff's 1:1 single-chain predecessor lookup.\n\n"
    "Parameters:\n"
    "- `scopes` (optional): if set, only episodes whose scope list "
    "intersects this filter are returned.\n"
    "- `parent_session_id` (optional): if set, restrict to that one "
    "session's directory (single-session journal lookup). Composes "
    "with `swarm_id` to narrow a fan-in to one sub-agent's session.\n"
    "- `swarm_id` (optional): fan-in filter — return only episodes "
    "tagged with this cohort id, gathered across all sessions. The "
    "swarm read surface.\n"
    "- `since` (optional ISO-8601): if set, only episodes created "
    "at-or-after this instant.\n"
    "- `max_results` (default 20, cap 200): cap on the returned "
    "list size; surfaces the most-recent N."
)


async def episode_search(
    deps: "ToolHandlers",
    scopes: list[str] | None = None,
    parent_session_id: str | None = None,
    swarm_id: str | None = None,
    since: str | None = None,
    max_results: int | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Handler body for the `episode_search` MCP tool."""
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if max_results is None:
        max_results = 20
    max_results = max(1, min(int(max_results), 200))

    since_dt: datetime | None = None
    if since is not None:
        since_dt = parse_event_ts(since)
        if since_dt is None:
            raise ValueError(f"since must be an ISO-8601 timestamp; got {since!r}")

    scope_filter: set[str] | None = set(scopes) if scopes else None
    # Session-disabled scopes are an opt-out hide; honored uniformly
    # across the read surface (memory_search, memory_list) — episodes
    # are the third leg, so we mirror the same `excluded & scopes`
    # short-circuit pattern from list_active.py:46 / search.py:226.
    excluded_scopes: set[str] = set(state.disabled_scopes)

    # Build the candidate episode pool. Three shapes, in precedence
    # order:
    #   - `swarm_id` set → multi-agent fan-in: the cohort across every
    #     session directory (`list_by_swarm`), optionally narrowed to a
    #     single session when `parent_session_id` is ALSO given. This is
    #     the swarm read — "what did all my sub-agents conclude."
    #   - `parent_session_id` only → restrict to that one session's
    #     directory (the original single-session journal lookup).
    #   - neither → every session directory, bounded by the prune TTL
    #     (default 30 days) so the walk stays cheap in long-running
    #     stores.
    # The fan-in / per-session split lives in the EpisodeStore so the
    # walk semantics have a single home, mirroring how the per-session
    # case already delegates to `list_by_session`.
    candidates: list[Episode]
    if swarm_id is not None:
        candidates = deps.episode_store.list_by_swarm(swarm_id)
        if parent_session_id is not None:
            candidates = [ep for ep in candidates if ep.session_id == parent_session_id]
    elif parent_session_id is not None:
        try:
            candidates = deps.episode_store.list_by_session(parent_session_id)
        except ValueError:
            # Invalid session_id (validation reject); empty result rather
            # than 500 the caller.
            candidates = []
    else:
        candidates = []
        for sid in deps.episode_store.iter_session_ids():
            try:
                candidates.extend(deps.episode_store.list_by_session(sid))
            except ValueError:
                # Invalid session_id (validation reject); skip rather than
                # 500 the caller.
                continue

    out: list[dict[str, Any]] = []
    for ep in candidates:
        # Skip session-tag floor episodes (E2 crash-recovery anchors).
        # They carry empty takeaways and a placeholder body; surfacing
        # them in a journal-summary surface like episode_search would
        # be noise indistinguishable from a takeaway from the model's
        # perspective ("what did I conclude" → "(session-tag floor —
        # no takeaway recorded)"). The candidate-walk side of
        # episode_handoff still sees floors via list_by_session, which
        # is what enables the worktree-filter match the floor was
        # written for in the first place. Both reads use
        # `list_by_session`, but only the summary surfaces filter the
        # flag; that asymmetry is the load-bearing piece of the fix.
        if ep.is_floor:
            continue
        if since_dt is not None and ep.created < since_dt:
            continue
        if scope_filter is not None and not (scope_filter & set(ep.scopes)):
            continue
        if excluded_scopes and (set(ep.scopes) & excluded_scopes):
            continue
        out.append(
            {
                "id": ep.id,
                "session_id": ep.session_id,
                "created": ep.created.isoformat().replace("+00:00", "Z"),
                "takeaway": ep.takeaway,
                "body": ep.body.strip(),
                "scopes": ep.scopes,
                "swarm_id": ep.swarm_id,
            }
        )

    out.sort(key=lambda e: e["created"])
    # Cap to the most-recent N (matches `episode_handoff`'s
    # `all_eps[-max_episodes:]` pattern and caller intuition for ad-hoc
    # journal lookup — "what did I conclude across the last few
    # sessions?" reads the tail, not the head). The slice keeps the
    # ascending order inside the recent-N window so output stays
    # oldest-first within the surfaced subset.
    out = out[-max_results:]

    deps.recorder.record(
        "episode_search",
        scopes_filter=list(scopes) if scopes else None,
        parent_session_id=parent_session_id,
        swarm_id=swarm_id,
        since=since,
        max_results=max_results,
        returned=len(out),
    )
    return out


__all__ = ["DESC_EPISODE_SEARCH", "episode_search"]
