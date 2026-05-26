"""episode_handoff MCP tool — handler implementation + DESC.

The read counterpart to `episode_write`. Surfaces the most-recent N
takeaways from the prior session in the same worktree, designed as
the FIRST MCP call at a /loop iteration entry. Closes the "no
forced-read at iteration entry" gap the audit identified: opt-in
`memory_search` works for stateless iterations, but iterations that
depend on prior-iteration state need a primitive that says "what did
the last session conclude here?" — that's this tool.

When `prior_session_id` is omitted, the handler resolves it
automatically via `find_prior_session_boundary` over the event log,
walking back to the most recent session_id other than the recorder's.
A caller that knows the parent session id (e.g., a /loop subagent
that was passed its parent's session_id) can pass it explicitly.

Returns `None`-rich shape so the caller can distinguish:

- "no prior session in this store" — handoff returns
  `{"prior_session_id": None, "episodes": []}`. First-ever invocation
  in a worktree.
- "prior session existed but wrote no episodes" — returns
  `{"prior_session_id": "sess_xxx", "episodes": []}`. The prior
  session did work but didn't journal a takeaway.
- "prior session has takeaways" — `{"prior_session_id": "sess_xxx",
  "episodes": [...]}` with the latest N entries (oldest first within
  the slice).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_EPISODE_HANDOFF = (
    "Read the most-recent journal takeaways from a prior session in "
    "this worktree. Call this FIRST at a /loop iteration entry — it "
    "answers 'what did the last session conclude here?' without the "
    "model needing to call memory_search.\n\n"
    "Episodes are the sibling-to-memory primitive for journal-shaped "
    "writes (see episode_write). When `prior_session_id` is omitted, "
    "the handler resolves it via the event log — the most recent "
    "session_id other than this process's own. Pass it explicitly "
    "when you know it (e.g., a child agent passed its parent's id).\n\n"
    "Returns a dict:\n"
    "- `prior_session_id`: the resolved session id, or None when no "
    "prior session exists in the log.\n"
    "- `episodes`: list of {id, created, takeaway, body, scopes} "
    "dicts, oldest first, capped at `max_episodes`. Each entry "
    "preferentially surfaces the writer's `takeaway`; the full "
    "`body` is included for the caller to inspect.\n\n"
    "Use this only at iteration entry. For ad-hoc lookup of an "
    "older session's journal, prefer `episode_search` with an "
    "explicit `parent_session_id`.\n\n"
    "Parameters:\n"
    "- `prior_session_id` (optional): override the auto-resolved id.\n"
    "- `max_episodes` (default 5, cap 50): how many takeaways to "
    "surface from the resolved session."
)


async def episode_handoff(
    deps: "ToolHandlers",
    prior_session_id: str | None = None,
    max_episodes: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Handler body for the `episode_handoff` MCP tool.

    Auto-resolves the prior session by walking the event log when
    `prior_session_id` is None. Caps `max_episodes` at 50 to keep the
    response bounded; defaults to 5 to match the rest of the read
    surface (`default_max_results`).
    """
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if max_episodes is None:
        max_episodes = 5
    max_episodes = max(1, min(int(max_episodes), 50))

    resolved_session_id: str | None = prior_session_id
    if resolved_session_id is None:
        from ..events import iter_all_events

        # Walk the event log to find the most-recent session_id that
        # isn't the recorder's. Same `find_prior_session_boundary`
        # discipline `memory_scope_overview` uses — anchor on the
        # recorder id because that's the id every event in the log
        # carries.
        latest_ts = None
        for ev in iter_all_events(deps.store.root):
            sid = ev.get("session") or ev.get("session_id")
            if not isinstance(sid, str) or sid == deps.recorder.session_id:
                continue
            ts = ev.get("ts")
            if latest_ts is None or (isinstance(ts, str) and ts > latest_ts):
                latest_ts = ts
                resolved_session_id = sid

    episodes: list[dict[str, Any]] = []
    if resolved_session_id is not None:
        all_eps = deps.episode_store.list_by_session(resolved_session_id)
        # Oldest first within the recent slice: take the LAST
        # `max_episodes`, which is the most recent chunk. This matches
        # the way a reader expects "the prior session's recent
        # takeaways" — chronological within the surfaced window.
        recent = all_eps[-max_episodes:]
        for ep in recent:
            episodes.append(
                {
                    "id": ep.id,
                    "created": ep.created.isoformat().replace("+00:00", "Z"),
                    "takeaway": ep.takeaway,
                    "body": ep.body.strip(),
                    "scopes": ep.scopes,
                }
            )

    deps.recorder.record(
        "episode_handoff",
        prior_session_id=resolved_session_id,
        max_episodes=max_episodes,
        returned=len(episodes),
    )
    return {
        "prior_session_id": resolved_session_id,
        "episodes": episodes,
    }


__all__ = ["DESC_EPISODE_HANDOFF", "episode_handoff"]
