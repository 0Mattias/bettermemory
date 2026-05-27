"""episode_write MCP tool — handler implementation + DESC.

Episodes are the sibling-to-memory primitive for journal-shaped writes
the durability gate (`durability.TRANSIENT_PHRASE_MARKERS`) explicitly
rejects on `memory_write`: loop-iteration state, "what we tried",
run-local takeaways that need to survive one context reset but aren't
durable facts. Stored at `<root>/episodes/<session_id>/<ulid>.md`,
TTL-pruned (default 30 days) on each write so the directory stays
bounded without a separate cleanup pass.

Excluded from `memory_search`, `memory_health`, `memory_list` — the
write here lands in a sibling subtree the memory iterators never see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._shared import Context, _advance_turn, _validate_content_size

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_EPISODE_WRITE = (
    "Append a journal-shaped entry for the current session — for "
    "run-state, iteration takeaways, 'what we tried'. Episodes are "
    "NOT durable facts; the durability gate that rejects transient "
    "memory_write content (TRANSIENT_PHRASE_MARKERS) does not apply "
    "here. Stored at <root>/episodes/<session_id>/<ulid>.md with a "
    "default 30-day TTL.\n\n"
    "Use this for content memory_write would reject as transient:\n"
    "- 'iteration N tried X, fell over at step 3'\n"
    "- 'currently blocked on Y; next step is Z'\n"
    "- 'this branch's release plan' (state that changes weekly)\n\n"
    "Episodes are invisible to memory_search / memory_health / "
    "memory_list — they are a sibling tier, not a memory category. "
    "Surface them via episode_handoff at iteration entry or "
    "episode_search for cross-session lookup. Promote a takeaway to "
    "durable memory via episode_promote (routes through memory_write, "
    "durability gate fires as normal).\n\n"
    "Parameters:\n"
    "- `body`: free-form markdown. Required, non-empty.\n"
    "- `takeaway` (optional): one-sentence summary. Surfaced "
    "preferentially at episode_handoff; when None, handoff falls "
    "back to the first line of body.\n"
    "- `scopes` (optional): list of scope tags. Empty list is "
    "valid (handoff keys on session_id, not scope)."
)


async def episode_write(
    deps: "ToolHandlers",
    body: str,
    takeaway: str | None = None,
    scopes: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Handler body for the `episode_write` MCP tool.

    Captures `session_id` from the recorder (the process-wide id every
    event in the log is tagged with), captures origin via the same
    shim the other handlers use, prunes old session dirs as a cheap
    side-effect of the write path, and returns the committed episode
    summary.
    """
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if not body or not body.strip():
        raise ValueError("episode body must be a non-empty string")
    # Mirror the size cap memory_write / memory_update enforce so a
    # multi-MB episode body can't slip past the write surface and land
    # on disk uncapped. Episodes share the same fsynced-file storage
    # path memories use; the DoS/disk-fill exposure is identical.
    # Raises ValueError with the same message shape as the memory_write
    # path, so the MCP error surface is uniform across both tiers.
    _validate_content_size(body, deps.config.behavior.max_content_bytes)

    origin = _h.capture_origin()
    # The recorder's session_id is the canonical per-process id that's
    # tagged on every event. Same discipline scope_overview uses for
    # the prior-session boundary — keying episodes on the same id keeps
    # the episode_handoff handler aligned with what the event log
    # actually carries.
    session_id = deps.recorder.session_id

    episode = deps.episode_store.write(
        session_id=session_id,
        body=body,
        takeaway=takeaway,
        scopes=list(scopes or []),
        origin=origin,
    )
    # Prune old session dirs as a cheap side effect of the write path.
    # Exempt the active session so a long-paused worktree's history
    # survives across the pause. The prune walks one level (session
    # dirs only) and stats one file per session — bounded by the
    # number of sessions in the worktree's lifetime, which is small.
    pruned = deps.episode_store.prune_old_sessions(keep_session_id=session_id)

    deps.recorder.record(
        "episode_write",
        id=episode.id,
        session=session_id,
        scopes=episode.scopes,
        has_takeaway=episode.takeaway is not None,
        pruned_sessions=pruned,
    )
    return {
        "status": "committed",
        "id": episode.id,
        "session_id": session_id,
        "created": episode.created.isoformat().replace("+00:00", "Z"),
        "scopes": episode.scopes,
        "takeaway": episode.takeaway,
        "pruned_sessions": pruned,
    }


__all__ = ["DESC_EPISODE_WRITE", "episode_write"]
