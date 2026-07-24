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

NO READ-SURFACE FILTERS — deliberate, and the reasoning is below
=================================================================
This handler applies NEITHER of the two hides its neighbours enforce:
not the session's `disabled_scopes`, not the caller's git worktree. The
episode lookup walks every session directory under the shared memory
root and will resolve — and on commit DELETE — an episode belonging to a
sibling worktree. That is the intended contract, not an oversight; it is
written out here because the shape ("unguarded walk that ends in a
delete") reads exactly like the bug `episode_patterns` was fixed for,
and has been re-flagged by review more than once.

Why this differs from `episode_patterns`. That surface DISCOVERS its own
delete set: a bare cross-session walk clusters server-chosen member
episodes, and the caller commits to the cluster sight-unseen. The read
filters are the only thing bounding what it can destroy, so it applies
both. `episode_promote` destroys exactly ONE episode, the one whose
26-character ULID the caller typed. The explicit id IS the bound.

This is the codebase's existing rule for explicit selection, not a new
exception. `episode_search` exempts an explicit `swarm_id` /
`parent_session_id` from the worktree filter ("naming a cohort or a
specific session IS the scoping intent"); `episode_handoff` respects an
explicit `prior_session_id` verbatim ("explicit consent that they own
the cross-tree concern"). One notch further: every single-id durable
surface — `memory_show`, `memory_update`, `memory_verify`,
`memory_restore`, and the destructive `memory_remove` — consults neither
filter. Filtering here would make `episode_promote` the only by-id tool
in the server that refuses to act on an entity the caller named.

The load-bearing case is swarm consolidation, and filtering would BREAK
it. `episode_search(swarm_id=…)` is documented and tested as a
cross-worktree read precisely because a coordinator fans out sub-agents
that each run in their own worktree. Promoting the good takeaways is the
endpoint of that fan-in — the whole point of gathering them. A worktree
filter here would fail every such promote with "no episode with id …"
while the id sits right there in the search result the server just
returned.

WHAT THE DELETE-ON-COMMIT CAN REACH. Be plain about it: this unlinks the
source episode file from ANY worktree's session directory and from any
scope, including one this session has `memory_scope_disable`d. Both
delete paths inherit that reach, since both act on the
`episode_session_id` resolved by the unfiltered walk: the synchronous
`status="committed"` branch below, and the deferred one where a
`status="pending"` promotion is stashed here and unlinked later by
`memory_write_confirm`. The reach is bounded by
unguessability rather than by a filter — a ULID cannot be arrived at by
walking, so holding a foreign one means some surface handed it over, and
the only surfaces that hand out a foreign episode id are the deliberate
cross-tree reads (`episode_search` with `swarm_id` /
`parent_session_id` / `auto_scope=False`, `episode_handoff` with an
explicit `prior_session_id`). Default-scoped `episode_search` and
`episode_patterns` never do.

One consequence worth naming, because it is the sharpest edge: the
durable memory is stamped with the PROMOTER's origin. Promoting across a
worktree boundary therefore RELOCATES the content — the source worktree
loses the journal entry, and the replacement memory does not come back
under that worktree's default auto-scoped `memory_search` (it is still
reachable by `memory_show(id)`, or `memory_search(auto_scope=False)`).
For the ephemeral sub-agent checkout this is the correct end state and
it self-heals: once that worktree is deleted, `worktrees_match`'s
dead-worktree degrade makes the memory globally visible again. For two
long-lived sibling worktrees it is a real relocation, so promote another
tree's episode only when you mean to take ownership of the fact.

Deliberately NOT mirrored into `DESC_EPISODE_PROMOTE`: it was drafted
there and pulled back out. `test_default_on_descriptions_fit_budget` is
a ratchet on the default-on descriptions, which are resident in context
on EVERY turn including the ~90% that never touch memory, and what slack
it carries is reserved for field-discoverability pins. This is a
behavioural caveat, not such a pin. The audience that needs the
reasoning — the next reader of this handler — is reading this docstring,
which costs nothing per turn. The DESC already states the delete
plainly; a caller only ever holds a foreign id by having opted into a
cross-tree read first. If you want it in the DESC anyway, buy the room
by trimming policy elsewhere and say so in the commit; raising the
ceiling to fit one more paragraph is the move that test's docstring
rules out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._fsutil import flock_excl, fsync_dir
from ._shared import Context

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
        # Durability gate (audit-3 A3-06): unlink() drops the dirent
        # from session_dir, but that metadata change lives in the
        # directory's page-cache until a dir-fsync hits disk. Without
        # this, a crash between the confirm returning "committed" and
        # the kernel flushing dirty pages can resurrect the episode
        # file on reboot — the durable memory exists, the journal
        # entry comes back as a duplicate that survives until the
        # 30-day TTL or the next prune pass. Symmetric to the
        # `fsync_dir` ceremony on the prune branches in
        # `EpisodeStore.prune_old_sessions`.
        #
        # Wrap in try/except OSError so a vanished session_dir (peer
        # prune raced past our FileNotFoundError catch above and
        # rmtree'd the parent too) doesn't crash this helper —
        # `fsync_dir` already swallows OSError internally, but a
        # narrow belt-and-suspenders here documents the intent.
        try:
            fsync_dir(session_dir)
        except OSError:
            pass


DESC_EPISODE_PROMOTE = (
    "Promote a journal entry (episode) into a durable memory. Routes "
    "through memory_write — the durability gate, scope-mismatch "
    "detection, dedup, and user-inference confirmation flow all "
    "apply.\n\n"
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
    "Returns the `memory_write` response shape with one extra "
    "field: `promoted_from_episode_id: str` so the caller can "
    "correlate the promotion attempt back to its source episode "
    "regardless of outcome (committed / pending / duplicate / "
    "scope_mismatch / etc.).\n\n"
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
    # NOTE: do NOT call `_advance_turn` here. This handler routes through
    # `memory_write` (below), which advances the turn counter and drains
    # expired pending-write / use-token TTLs at its own entry. Advancing
    # here too would double-count the turn on the promote path and
    # prematurely age the ~2-turn record_use / pending windows — the exact
    # "call it once and only once" contract `_advance_turn` documents. The
    # episode lookup / floor check / body selection below don't read any
    # turn-dependent state, so deferring the single advance to the nested
    # `memory_write` call is correct.
    # `state` is bound for the pending-promotion stash further down
    # (`stash_promotion_episode`) and for NOTHING else. In particular
    # `state.disabled_scopes` is deliberately NOT consulted — see the
    # module docstring's "NO READ-SURFACE FILTERS" section. A scope
    # hide is a retrieval-time hide for discovery surfaces; it is not
    # an access-control gate, and no by-id surface in this server
    # (`memory_show` / `memory_update` / `memory_verify` /
    # `memory_restore` / the destructive `memory_remove`) applies it to
    # an entity the caller named outright. Reading it here would mean
    # answering "no such episode" about an id the caller is holding.
    state = deps.sessions.for_request(ctx)

    # Find the episode across all known sessions. Episodes don't have
    # an O(1) id→path mapping (the ULID is the filename, but the
    # session_id directory is unknown to the caller), so we walk
    # session dirs. The walk is bounded by the prune TTL, same
    # rationale as episode_search's iteration cost.
    #
    # NO WORKTREE GUARD ON THIS WALK — deliberate. It resolves ids
    # belonging to sibling worktrees, and on commit the branch at the
    # bottom of this function DELETES what it resolved. Do not "fix"
    # this to match `episode_patterns` without reading the module
    # docstring first: that surface filters because it discovers its
    # own delete set from a bare walk; here the caller-supplied ULID is
    # the selector, which is the same explicit-selection carve-out
    # `episode_search` (swarm_id / parent_session_id) and
    # `episode_handoff` (prior_session_id) already make. Adding the
    # filter would break swarm consolidation outright — the coordinator
    # promoting a sub-agent takeaway it just gathered via
    # `episode_search(swarm_id=…)` is precisely a cross-worktree
    # promote, and it is a tested contract.
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

    # Floors are session-tag anchors, not content — they carry an
    # empty takeaway and a placeholder body. Promoting one would
    # either fail noisily through the durability gate (transient
    # marker rejection) or, worse, succeed and land a junk memory.
    # Reject explicitly at the promotion boundary so the error
    # message points to the actual reason rather than blaming the
    # caller for "transient phrase".
    if episode.is_floor:
        raise ValueError(
            f"episode {episode_id} is a session-tag floor (no takeaway). "
            "Floors anchor a session's worktree on disk so episode_handoff "
            "can resolve a tick that crashed before episode_write; they "
            "carry no journal content. Write a real takeaway via "
            "episode_write and promote that instead."
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
        #
        # REACH OF THIS DELETE: `episode_session_id` came from the
        # unfiltered walk above, so this unlinks the episode wherever it
        # lives — any worktree sharing the memory root, any scope,
        # including one this session has scope-disabled. That is the
        # documented contract (module docstring), bounded by the fact
        # that the caller had to name an unguessable ULID rather than by
        # any read filter. Note the content is distilled, not destroyed:
        # the durable memory now holds it — but stamped with THIS
        # caller's origin, so a cross-worktree promote relocates the
        # fact out of the source worktree's default auto-scoped
        # retrieval.
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
