"""episode_patterns MCP tool — handler implementation + DESC.

The cross-session half of consolidation. `episode_promote` distills ONE
journal entry; this tool surfaces themes recurring across MANY sessions'
episodes (mechanical detection — see `patterns.py`) and lets the model
either promote a pattern into a durable memory or dismiss it.

Promotion deliberately requires the model to AUTHOR the body: the
server can prove that four sessions kept circling the same terms, but
only the model can write the sentence that is true across all four.
The write routes through the full `memory_write` gate stack (dedup,
durability, scope-mismatch, user-inference confirmation), so a pattern
whose fact is already stored dedup-rejects — and that rejection records
a corroboration on the existing memory, which is the recurrence signal
landing where it belongs.

On a committed promote the member episodes are deleted (their content
is distilled — same lifecycle as `episode_promote`). On `pending`
(user-inference) the episodes are LEFT IN PLACE: the multi-episode
confirm-time cleanup isn't wired, and the worst case — journal entries
surviving until their ~30-day TTL — is the pre-existing behavior for
every unpromoted episode. On any other non-committed status the
episodes are untouched so the caller can adjust and retry.

READ-SURFACE FILTERS: the listing walk applies the same two hides
`episode_search` documents as the uniformity contract ("episodes are
the third leg") — the session's `disabled_scopes` and, by default, the
caller's git worktree. This surface is a bare cross-session discovery
walk with no explicit selector (no `swarm_id` / `parent_session_id`
carve-out to honor), so both filters apply unconditionally to it. The
stakes are higher here than on a pure read: the filtered pool is also
what `promote` bulk-DELETES from on commit, so an unguarded walk would
destroy a sibling worktree's journal entries — a cross-boundary
destructive act, not just a leak.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import Episode
from ..patterns import PatternDismissals, clusterable_episodes, find_episode_patterns
from . import write as _write_mod
from ._shared import Context, _advance_turn
from .episode_promote import _delete_source_episode

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_EPISODE_PATTERNS = (
    "Surface themes recurring across MULTIPLE sessions' episodes — the "
    "consolidation no single session can see (four different days each "
    "journaling the same flaky proxy). The server clusters episodes "
    "sharing distinctive terms across >= 3 distinct sessions; YOU judge "
    "each candidate.\n\n"
    "Modes (mutually exclusive):\n"
    "- default (no args): list current candidates — `{id, terms, "
    "episode_ids, distinct_sessions, snippets}` per pattern, one "
    "snippet per member episode so it's judgeable in place.\n"
    "- `promote=<pattern_id>` + `body` + `scopes`: write the durable "
    "memory. AUTHOR the body yourself — state the fact that is true "
    "across the member episodes (the terms are evidence pointers, not "
    "a synthesis). Routes through the full memory_write gate stack; "
    "category/confidence/source accepted as on memory_write. On "
    "commit, member episodes are deleted (distilled). A `duplicate` "
    "rejection is still a WIN: it records a corroboration on the "
    "existing memory. On `pending` (user-inference) episodes stay "
    "until their TTL.\n"
    "- `dismiss=<pattern_id>`: not worth consolidating (incidental "
    "vocabulary overlap). Sticky for that exact episode set; a NEW "
    "episode joining the theme legitimately reopens it under a fresh "
    "id.\n\n"
    "Candidates recompute on every call (episodes churn constantly); "
    "ids are content-stable hashes of the member set, so a listed id "
    "stays valid while the members live. Detection is conservative "
    "(>= 3 episodes, >= 3 sessions, ubiquitous project vocabulary "
    "excluded) — an empty list usually just means the journal is "
    "young or already consolidated.\n\n"
    "READ FILTERS (same hides episode_search / episode_handoff "
    "enforce): episodes whose scopes are in this session's "
    "`disabled_scopes` are dropped before detection, and by default "
    "(`auto_scope=True`) so are episodes written from a DIFFERENT git "
    "worktree of the same repository sharing one memory root. Both "
    "apply to promote/dismiss too — a pattern you cannot see is "
    "neither promotable nor dismissible, which is what keeps the "
    "commit-time member DELETION inside your own worktree. Legacy "
    "episodes with no captured worktree, and callers outside any git "
    "checkout, pass through. Set `auto_scope=False` to sweep every "
    "worktree sharing the root (scope-disable still applies)."
)


def _all_episodes(deps: "ToolHandlers") -> list[Episode]:
    """Every episode on disk, unfiltered.

    Deliberately UNfiltered: this pool is the liveness authority for the
    dismissal GC (`PatternDismissals.dismissed_ids`), which drops rows
    whose member episodes have all aged out. Feeding it the caller-
    filtered pool would make "invisible from here" look like "aged out"
    and silently delete a dismissal recorded in another worktree (or
    against a scope this session happens to have disabled). Detection
    and the promote-time deletion run over `_visible_episodes(...)`
    instead.
    """
    out: list[Episode] = []
    for sid in deps.episode_store.iter_session_ids():
        try:
            out.extend(deps.episode_store.list_by_session(sid))
        except ValueError:
            continue
    return out


def _visible_episodes(
    episodes: list[Episode],
    *,
    excluded_scopes: set[str],
    apply_worktree_filter: bool,
    caller_worktree: str | None,
) -> list[Episode]:
    """The subset of `episodes` this caller may see — and therefore the
    only ones detection may cluster and `promote` may delete.

    Mirrors `episode_search`'s two filters verbatim:

    - Session-disabled scopes are an opt-out hide honored uniformly
      across the read surface (memory_search, memory_list,
      episode_search, episode_handoff); the same `excluded & scopes`
      short-circuit lands here.
    - Worktree isolation via the permissive `worktrees_match` (either
      side None → True), so legacy / pre-origin episodes and callers
      outside any git checkout still pass through — the same trade
      `should_include_for_caller` makes for `memory_search`.

    Unlike `episode_search` there is no explicit-selector carve-out to
    make: `episode_patterns` has no `swarm_id` / `parent_session_id`
    equivalent, so its walk is *always* the bare discovery walk that
    filter guards. `auto_scope=False` remains the explicit escape hatch
    for a deliberate cross-worktree sweep.
    """
    from ..origin import worktrees_match

    out: list[Episode] = []
    for ep in episodes:
        if excluded_scopes and (set(ep.scopes) & excluded_scopes):
            continue
        if apply_worktree_filter:
            ep_worktree = ep.origin.worktree_root if ep.origin else None
            if not worktrees_match(ep_worktree, caller_worktree):
                continue
        out.append(ep)
    return out


async def episode_patterns(
    deps: "ToolHandlers",
    promote: str | None = None,
    dismiss: str | None = None,
    body: str | None = None,
    scopes: list[str] | None = None,
    category: str = "fact",
    confidence: str = "medium",
    source: str = "inferred",
    min_sessions: int = 3,
    max_patterns: int = 5,
    auto_scope: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if promote is not None and dismiss is not None:
        raise ValueError("pass either promote=<id> or dismiss=<id>, not both")
    if min_sessions < 2:
        raise ValueError("min_sessions must be >= 2")
    if max_patterns < 1:
        raise ValueError("max_patterns must be a positive integer")

    # Route capture_origin through the parent ``_handlers`` module so the
    # test suite's monkey-patch propagates here too — the same shim
    # discipline `memory_search` / `episode_search` / `episode_handoff` use.
    from .. import _handlers as _h

    # `for_request` is a pure lookup (creates the state on first touch),
    # so calling it on BOTH paths is free. The turn advance is what stays
    # conditional: the promote path routes through memory_write, which
    # advances the turn itself — same single-advance contract
    # episode_promote documents. We need `state` unconditionally now
    # because `disabled_scopes` gates the candidate pool that promote
    # resolves its target from.
    state = deps.sessions.for_request(ctx)
    if promote is None:
        _advance_turn(state, deps.recorder)

    caller_worktree: str | None = None
    if auto_scope:
        current_origin = _h.capture_origin()
        caller_worktree = current_origin.worktree_root if current_origin else None

    all_episodes = _all_episodes(deps)
    episodes = _visible_episodes(
        all_episodes,
        excluded_scopes=set(state.disabled_scopes),
        apply_worktree_filter=auto_scope,
        caller_worktree=caller_worktree,
    )
    candidates = find_episode_patterns(
        episodes, min_sessions=min_sessions, max_patterns=max_patterns
    )
    dismissals = PatternDismissals(deps.store.root)
    # Liveness for the dismissal GC comes from the UNFILTERED pool: a
    # row is dead only when its members are gone from DISK, not when
    # they're merely hidden from this caller (see `_all_episodes`).
    live_ids = {ep.id for ep in all_episodes}
    dismissed = dismissals.dismissed_ids(live_ids)
    candidates = [c for c in candidates if c.id not in dismissed]

    if dismiss is not None:
        target = next((c for c in candidates if c.id == dismiss), None)
        if target is None:
            raise ValueError(
                f"no live pattern candidate with id {dismiss!r} — list first; "
                "candidates recompute as episodes churn"
            )
        dismissals.dismiss(target.id, target.episode_ids)
        deps.recorder.record(
            "episode_pattern",
            action="dismissed",
            pattern=target.id,
            episodes=len(target.episode_ids),
        )
        return {"dismissed": target.id, "remaining": len(candidates) - 1}

    if promote is not None:
        target = next((c for c in candidates if c.id == promote), None)
        if target is None:
            raise ValueError(
                f"no live pattern candidate with id {promote!r} — list first; "
                "candidates recompute as episodes churn"
            )
        if not body or not body.strip():
            raise ValueError(
                "promote requires `body`: author the durable fact that is "
                "true across the member episodes (see their snippets)"
            )
        if not scopes:
            raise ValueError("promote requires `scopes` for the durable memory")

        response = await _write_mod.memory_write(
            deps,
            content=body,
            scopes=scopes,
            confidence=confidence,
            source=source,
            category=category,
            ctx=ctx,
        )
        response["promoted_from_pattern"] = target.id
        response["pattern_episode_ids"] = target.episode_ids
        if response.get("status") == "committed":
            deleted = 0
            # Keyed off the VISIBLE pool, not `all_episodes`: the delete
            # set can never reach past the read filters that produced the
            # candidate. `target.episode_ids` already comes from a
            # candidate detected over `episodes`, so this is belt-and-
            # braces — but it is the line that makes "the delete set
            # never reaches past the read filters" true by construction
            # rather than by derivation. Note what that does and does
            # not buy: it is exactly as strong as the filters, and the
            # worktree one is the permissive `origin.worktrees_match`,
            # so an episode the filter had no boundary to enforce on
            # (no captured worktree, dead recorded worktree, caller
            # outside any git checkout) is inside the delete set.
            by_id = {ep.id: ep for ep in episodes}
            for eid in target.episode_ids:
                ep = by_id.get(eid)
                if ep is None:
                    continue
                try:
                    _delete_source_episode(deps, ep.session_id, eid)
                    deleted += 1
                except OSError:  # pragma: no cover - crash-safe best effort
                    continue
            response["episodes_deleted"] = deleted
            deps.recorder.record(
                "episode_pattern",
                action="promoted",
                pattern=target.id,
                memory_id=response.get("id"),
                episodes_deleted=deleted,
            )
        return response

    # TWO counts, deliberately, because they answer different questions
    # and neither one alone is honest:
    #
    # - `episodes_scanned` is the VISIBLE pool — every episode this call
    #   read after the two read-surface hides. Reporting the on-disk
    #   total here instead would make a scope-hidden or cross-worktree
    #   journal look like it was considered and found unpatterned.
    # - `episodes_clustered` is detection's actual input, i.e. the
    #   evidence base behind `patterns` (and behind an EMPTY `patterns`).
    #   It comes from `clusterable_episodes`, the same predicate
    #   `find_episode_patterns` filters with, so the reported number
    #   cannot drift from what was clustered.
    #
    # The visible pool is a strict superset: floors (session-tag anchors
    # `episode_handoff` writes) and empty-body episodes are read and
    # counted as scanned, then dropped before clustering. Publishing the
    # visible pool under the label "what detection clustered over" — the
    # shape this response shipped with — overstates the evidence base in
    # any store where a handoff has ever written a floor, which is the
    # exact overstatement that label was reaching for. Pinned by
    # `test_patterns_listing_separates_scanned_from_clustered`.
    clustered = len(clusterable_episodes(episodes))
    return {
        "patterns": [c.to_dict() for c in candidates],
        "episodes_scanned": len(episodes),
        "episodes_clustered": clustered,
        **(
            {}
            if candidates
            else {
                "hint": (
                    "No recurring cross-session themes right now. Detection "
                    "needs >= 3 episodes across >= 3 distinct sessions "
                    "sharing distinctive terms — a young or freshly-"
                    "consolidated journal legitimately lists none."
                )
            }
        ),
    }
