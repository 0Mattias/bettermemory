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


DESC_EPISODE_SEARCH = (
    "Cross-session lookup for journal-shaped entries (episodes). "
    "Unlike memory_search, this is NOT ranked — episodes are "
    "chronological and the filter set (scope / since / session_id) "
    "is the discovery surface.\n\n"
    "Use this for ad-hoc journal lookup, e.g. 'what did I conclude "
    "about projects:auth across the last few sessions?'. For the "
    "loop-iteration-entry case prefer `episode_handoff`, which "
    "auto-resolves the prior session and caps the surface.\n\n"
    "Returns the matching episodes oldest first, capped at "
    "`max_results`.\n\n"
    "Parameters:\n"
    "- `scopes` (optional): if set, only episodes whose scope list "
    "intersects this filter are returned.\n"
    "- `parent_session_id` (optional): if set, restrict to that one "
    "session's directory.\n"
    "- `since` (optional ISO-8601): if set, only episodes created "
    "at-or-after this instant.\n"
    "- `max_results` (default 20, cap 200): hard cap on the returned "
    "list size."
)


async def episode_search(
    deps: "ToolHandlers",
    scopes: list[str] | None = None,
    parent_session_id: str | None = None,
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

    # Decide the source of session ids to walk: either one explicit id
    # or every session_id with a directory under episodes/. The latter
    # is bounded by the prune TTL (default 30 days) so the iteration is
    # cheap even in long-running stores.
    if parent_session_id is not None:
        candidate_sessions: list[str] = [parent_session_id]
    else:
        candidate_sessions = list(deps.episode_store.iter_session_ids())

    scope_filter: set[str] | None = set(scopes) if scopes else None

    out: list[dict[str, Any]] = []
    for sid in candidate_sessions:
        try:
            episodes = deps.episode_store.list_by_session(sid)
        except ValueError:
            # Invalid session_id (validation reject); skip rather than
            # 500 the caller.
            continue
        for ep in episodes:
            if since_dt is not None and ep.created < since_dt:
                continue
            if scope_filter is not None and not (scope_filter & set(ep.scopes)):
                continue
            out.append(
                {
                    "id": ep.id,
                    "session_id": ep.session_id,
                    "created": ep.created.isoformat().replace("+00:00", "Z"),
                    "takeaway": ep.takeaway,
                    "body": ep.body.strip(),
                    "scopes": ep.scopes,
                }
            )

    out.sort(key=lambda e: e["created"])
    out = out[:max_results]

    deps.recorder.record(
        "episode_search",
        scopes_filter=list(scopes) if scopes else None,
        parent_session_id=parent_session_id,
        since=since,
        max_results=max_results,
        returned=len(out),
    )
    return out


__all__ = ["DESC_EPISODE_SEARCH", "episode_search"]
