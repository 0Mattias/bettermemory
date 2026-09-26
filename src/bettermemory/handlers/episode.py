"""The `episode` MCP tool: the journal tier's write and handoff in one call.

Episodes are the sibling-to-memory primitive for journal-shaped writes the
durability gate rejects on `memory_write`: loop-iteration state, what was
tried, run-local takeaways that need to survive one context reset but are
not durable facts. `action="write"` appends one for the current session;
`action="handoff"` reads the prior session's takeaways in this worktree.
The two bodies live in `episode_write.py` and `episode_handoff.py`; this
module is the dispatch the served tool is built from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ._shared import Context
from .episode_handoff import episode_handoff
from .episode_write import episode_write

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


EpisodeAction = Literal["write", "handoff"]

DESC_EPISODE = (
    "The journal beside memory: run-state and takeaways, no durability "
    "gate, pruned after 30 days. write: `body`, one-line `takeaway`, "
    "`scopes`, `swarm_id` for fan-in. handoff: the prior session's "
    "takeaways in this worktree (`prior_session_id` names one; "
    "`max_episodes` up to 50; `include_bodies`, never for an unaccounted "
    "episode). Call handoff first at a loop iteration's entry."
)


async def episode(
    deps: ToolHandlers,
    action: EpisodeAction,
    body: str | None = None,
    takeaway: str | None = None,
    scopes: list[str] | None = None,
    swarm_id: str | None = None,
    prior_session_id: str | None = None,
    max_episodes: int | None = None,
    include_bodies: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if action == "write":
        if body is None:
            raise ValueError("action='write' needs `body`")
        return await episode_write(
            deps,
            body,
            takeaway=takeaway,
            scopes=scopes,
            swarm_id=swarm_id,
            ctx=ctx,
        )
    if action == "handoff":
        return await episode_handoff(
            deps,
            prior_session_id=prior_session_id,
            max_episodes=max_episodes,
            include_bodies=include_bodies,
            ctx=ctx,
        )
    raise ValueError(f"unknown episode action {action!r}; expected write or handoff")


__all__ = ["DESC_EPISODE", "EpisodeAction", "episode"]
