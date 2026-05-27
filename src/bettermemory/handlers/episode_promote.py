"""episode_promote MCP tool — handler implementation + DESC.

Promote an episodic takeaway into a durable memory. Routes through
the standard `memory_write` path so the full durability gate fires:
TRANSIENT_PHRASE_MARKERS rejection, scope-mismatch detection,
groundedness, dedup, dedup-tombstone, the user-inference pending flow
— everything `memory_write` does. The promotion adds nothing to the
gate stack; it just supplies the body+scopes from an existing episode.

On successful commit, the source episode is deleted (its content has
been distilled into the durable memory). On any non-committed status
(pending, duplicate, previously_removed, transient_warning,
scope_mismatch, ungrounded), the source episode is left in place so
the caller can adjust and re-promote.

Body default: when `use_body=False` (the default), the durable memory's
body is the episode's `takeaway`. When `use_body=True`, the full body
is used. The takeaway-only default matches the design intent — episodes
journal context, takeaways are the distilled fact worth keeping. An
episode without a takeaway requires `use_body=True` to promote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_EPISODE_PROMOTE = (
    "Promote a journal entry (episode) into a durable memory. Routes "
    "through memory_write — the durability gate, scope-mismatch "
    "detection, dedup, and user-inference confirmation flow all "
    "apply. Nothing about promotion bypasses the standard write "
    "discipline.\n\n"
    "Use this when an iteration's takeaway turns out to be a fact "
    "worth keeping across sessions, not just a run-state note.\n\n"
    "On successful commit the source episode is deleted (its content "
    "has been distilled). On any non-committed status (pending, "
    "duplicate, previously_removed, transient_warning, "
    "scope_mismatch, ungrounded) the source episode is left "
    "untouched so you can adjust and re-promote.\n\n"
    "Body default: when `use_body=False` (default), the durable "
    "memory's body is the episode's takeaway. Set `use_body=True` to "
    "use the full episode body. An episode with no takeaway requires "
    "use_body=True.\n\n"
    "Parameters:\n"
    "- `episode_id`: ULID of the source episode.\n"
    "- `scopes`: scopes for the durable memory. Required.\n"
    "- `category` (default 'fact'): memory category. user-inference "
    "still requires explicit user confirmation.\n"
    "- `confidence` (default 'medium'), `source` (default "
    "'explicit-statement'): standard memory_write fields.\n"
    "- `use_body=False`: when True, use the episode's body instead "
    "of its takeaway."
)


async def episode_promote(
    deps: "ToolHandlers",
    episode_id: str,
    scopes: list[str],
    category: str = "fact",
    confidence: str = "medium",
    source: str = "explicit-statement",
    use_body: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Handler body for the `episode_promote` MCP tool."""
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    # Find the episode across all known sessions. Episodes don't have
    # an O(1) id→path mapping (the ULID is the filename, but the
    # session_id directory is unknown to the caller), so we walk
    # session dirs. The walk is bounded by the prune TTL, same
    # rationale as episode_search's iteration cost.
    episode = None
    episode_session_id: str | None = None
    for sid in deps.episode_store.iter_session_ids():
        try:
            for ep in deps.episode_store.list_by_session(sid):
                if ep.id == episode_id:
                    episode = ep
                    episode_session_id = sid
                    break
        except ValueError:
            continue
        if episode is not None:
            break
    if episode is None or episode_session_id is None:
        raise ValueError(
            f"no episode with id {episode_id!r} (it may have been pruned "
            "past its TTL or never existed)"
        )

    # Pick the body for the durable memory.
    if use_body:
        body_for_memory = episode.body
    else:
        if episode.takeaway is None:
            raise ValueError(
                f"episode {episode_id} has no takeaway; pass use_body=True "
                "to promote the full body instead, or write a new episode "
                "with a takeaway."
            )
        body_for_memory = episode.takeaway

    # Route through the standard memory_write handler so every gate
    # fires (durability, scope-mismatch, groundedness, dedup, etc.).
    # We import lazily to avoid a circular import at module-load time.
    from . import write as _write_mod

    response = await _write_mod.memory_write(
        deps,
        content=body_for_memory,
        scopes=scopes,
        confidence=confidence,
        source=source,
        category=category,
        ctx=ctx,
    )

    # Annotate the response with the source episode id regardless of
    # outcome so the caller can correlate the promotion attempt.
    response = dict(response)
    response["promoted_from_episode_id"] = episode_id

    if response.get("status") == "committed":
        # Distill: delete the source episode file. Other statuses
        # (pending, duplicate, previously_removed, transient_warning,
        # scope_mismatch, ungrounded) leave the episode alone so the
        # caller can retry with adjustments.
        session_dir = deps.episode_store._session_dir(episode_session_id)
        ep_path = session_dir / f"{episode_id}.md"
        try:
            ep_path.unlink()
        except FileNotFoundError:
            # Race: episode was already deleted (concurrent promote?)
            # — fine, the durable memory is the authoritative artifact
            # now. Don't crash the response.
            pass

    deps.recorder.record(
        "episode_promote",
        episode_id=episode_id,
        scopes=list(scopes),
        category=category,
        use_body=use_body,
        write_status=response.get("status"),
        memory_id=response.get("id"),
    )
    return response


__all__ = ["DESC_EPISODE_PROMOTE", "episode_promote"]
