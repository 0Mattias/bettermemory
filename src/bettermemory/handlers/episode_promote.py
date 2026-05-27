"""episode_promote MCP tool — handler implementation + DESC.

Promote an episodic takeaway into a durable memory. Routes through
the standard `memory_write` path so the full durability gate fires:
TRANSIENT_PHRASE_MARKERS rejection, scope-mismatch detection,
groundedness, dedup, dedup-tombstone, the user-inference pending flow
— everything `memory_write` does. The promotion adds nothing to the
gate stack; it just supplies the body+scopes from an existing episode.

On successful commit, the source episode is deleted (its content has
been distilled into the durable memory). On a `pending` outcome
(category='user-inference' or the global confirm flag), the source
episode is left in place but a `pending_id → (session, episode_id)`
link is stashed on the session so `memory_write_confirm` can delete
the episode on user-confirm — without that linkage, the confirm path
would commit the durable memory and leave the journal entry as a
duplicate that survives until the 30-day TTL. `memory_write_cancel`
drops the link without acting on the episode so the caller can retry.

On any other non-committed status (duplicate, previously_removed,
transient_warning, scope_mismatch, ungrounded), the source episode
is left in place so the caller can adjust and re-promote.

Body default: when `use_body=False` (the default), the durable memory's
body is the episode's `takeaway`. When `use_body=True`, the full body
is used. The takeaway-only default matches the design intent — episodes
journal context, takeaways are the distilled fact worth keeping. An
episode without a takeaway requires `use_body=True` to promote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._fsutil import flock_excl
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


def _delete_source_episode(
    deps: "ToolHandlers", episode_session_id: str, episode_id: str
) -> None:
    """Remove an episode file from disk.

    Shared between the commit-immediately path (when `episode_promote`
    sees `status='committed'` synchronously) and the deferred path
    (when `memory_write_confirm` commits a previously-pending
    promotion).

    Holds the per-session flock anchored at
    `episodes_dir / .session-<id>` — the same anchor `EpisodeStore.write`
    and `EpisodeStore.prune_old_sessions` take. This is the third
    write-side path that touches a session_dir's contents (write + prune
    are the other two); leaving it unlocked drifts from the t13/t17
    contract that says BOTH directions of the session_dir lifecycle
    serialise on this anchor. The concrete race the lock prevents: a
    peer process's `prune_old_sessions` could `shutil.rmtree` the same
    session_dir between our `_session_dir` resolution and the `unlink`,
    and while the `FileNotFoundError` catch absorbs the visible failure
    today, the contract drift would trip future refactors (adding
    fsync_dir on the delete path, audit-telemetry, migrating to a
    different deletion primitive).

    The `FileNotFoundError` catch is preserved on purpose — a peer
    prune (or a duplicate confirm that landed first) deleting the
    source episode before us is a valid completion state: the
    post-condition this helper exists to establish (source episode is
    gone) holds either way.

    Lock ordering: `memory_write_confirm` calls into this helper AFTER
    `deps.store.write(...)` has returned, so the durable-store's
    per-memory-id flock (anchored under `<memory_id>.md.lock`) has
    already been released. The two flocks are anchored on different
    paths and never nested, so there's no deadlock risk between the
    confirm path and a concurrent peer prune."""
    session_dir = deps.episode_store._session_dir(episode_session_id)
    ep_path = session_dir / f"{episode_id}.md"
    lock_anchor = deps.episode_store.episodes_dir / f".session-{episode_session_id}"
    with flock_excl(lock_anchor):
        try:
            ep_path.unlink()
        except FileNotFoundError:
            pass


DESC_EPISODE_PROMOTE = (
    "Promote a journal entry (episode) into a durable memory. Routes "
    "through memory_write — the durability gate, scope-mismatch "
    "detection, dedup, and user-inference confirmation flow all "
    "apply. Nothing about promotion bypasses the standard write "
    "discipline.\n\n"
    "Use this when an iteration's takeaway turns out to be a fact "
    "worth keeping across sessions, not just a run-state note.\n\n"
    "On successful commit the source episode is deleted (its content "
    "has been distilled). On `pending` (user-inference category), the "
    "source episode is held for memory_write_confirm to delete — "
    "memory_write_cancel keeps the episode so you can retry. On any "
    "other non-committed status (duplicate, previously_removed, "
    "transient_warning, scope_mismatch, ungrounded) the source "
    "episode is left untouched so you can adjust and re-promote.\n\n"
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

    status = response.get("status")
    if status == "committed":
        # Distill: delete the source episode file. Other terminal
        # rejections (duplicate, previously_removed, transient_warning,
        # scope_mismatch, ungrounded) leave the episode alone so the
        # caller can retry with adjustments.
        _delete_source_episode(deps, episode_session_id, episode_id)
    elif status == "pending":
        # Stash the linkage so memory_write_confirm can delete the
        # source episode once the user confirms. memory_write_cancel
        # will discard this link (keeping the episode for a retry).
        # Without the stash, the confirm path commits the durable
        # memory but leaves the episode behind as a duplicate journal
        # entry surviving until the 30-day TTL.
        pending_id = response.get("pending_id")
        if pending_id is not None:
            state.stash_promotion_episode(
                str(pending_id), episode_session_id, episode_id
            )

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


__all__ = ["DESC_EPISODE_PROMOTE", "_delete_source_episode", "episode_promote"]
