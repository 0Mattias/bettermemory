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

Excluded surfaces: episode content still doesn't appear in
memory_search / memory_health / memory_list — this is a dedicated
episodic-tier read. memory_health carries the subtree's aggregate
volume (`episode_volume`) only.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ._shared import Context, _advance_turn
from ..time_utils import parse_event_ts

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..models import Episode


# Deliberately compact: this description is default-registered, so its
# full length is a per-turn context cost. Longhand rationale lives in
# docs/api.md; what stays here is every parameter, every return-shape key,
# and every cue tests/test_prompts.py pins ("most-recent", "PERMISSIVE",
# "LINKED", "gone from disk", "strict" — the worktree filter's permissive
# contract must stay spelled out, not deferred). Gating the tool behind
# `full_tool_surface` is not an option; see the episode block in builder.py.
DESC_EPISODE_SEARCH = (
    "Cross-session lookup for journal-shaped entries (episodes). NOT "
    "ranked — episodes are chronological and the filter set (scope / "
    "since / session_id) is the discovery surface. For the "
    "loop-iteration-entry case prefer `episode_handoff`, which "
    "auto-resolves the prior session and caps the surface.\n\n"
    "Returns `{id, session_id, created, takeaway, body, scopes, "
    "swarm_id}` per row, oldest-first inside the most-recent-"
    "`max_results` window: over the cap it keeps the MOST-RECENT N, so "
    "'what did I conclude lately?' reads the tail, not the head. "
    "`session_id` is present because this surface spans sessions "
    "(unlike episode_handoff); `swarm_id` (may be null) is the "
    "multi-agent cohort tag — pass a coordinator's session id to gather "
    "every sub-agent's takeaways in one read.\n\n"
    "WORKTREE SCOPING: by default (`auto_scope=True`) the bare "
    "discovery walk (no `swarm_id` / `parent_session_id`) drops "
    "episodes whose captured git worktree differs from yours. "
    "PERMISSIVE, not a boundary, and weaker than the strict equality "
    "episode_handoff applies — it passes an episode through when there "
    "is nothing to compare (none captured, or you outside any git "
    "checkout), when the recorded worktree is gone from disk, and when "
    "you are in a LINKED worktree of the checkout that wrote it, so "
    "under agent fan-out the primary checkout's episodes stay visible. "
    "An EXPLICIT `swarm_id` / `parent_session_id` / `ids` is never "
    "worktree-filtered: naming a cohort or session is deliberate "
    "cross-worktree intent.\n\n"
    "Parameters (full reference in docs/api.md):\n"
    "- `scopes` (optional): keep only episodes whose scope list "
    "intersects this filter.\n"
    "- `parent_session_id` (optional): restrict to one session's "
    "directory. Composes with `swarm_id` to narrow a fan-in.\n"
    "- `swarm_id` (optional): fan-in filter — episodes tagged with this "
    "cohort id, across all sessions.\n"
    "- `since` (optional ISO-8601): created at-or-after this instant.\n"
    "- `auto_scope` (default True): worktree-scope the bare walk (see "
    "WORKTREE SCOPING). False sweeps every worktree sharing the root.\n"
    "- `max_results` (default 20, cap 200): surfaces the most-recent N.\n"
    "- `ids` (optional): only these episode ULIDs — explicit selector, "
    "never worktree-filtered; unknown ids are absent, not an error.\n"
    "- `include_bodies` (default True): False OMITS `body` — "
    "takeaway-only rows. Scan, then re-read one via `ids`."
)


async def episode_search(
    deps: "ToolHandlers",
    scopes: list[str] | None = None,
    parent_session_id: str | None = None,
    swarm_id: str | None = None,
    since: str | None = None,
    max_results: int | None = None,
    auto_scope: bool = True,
    include_bodies: bool = True,
    ids: list[str] | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Handler body for the `episode_search` MCP tool.

    `include_bodies` / `ids` are also spelled out on the `_handlers.py`
    facade — FastMCP builds the served schema from THAT signature, and a
    parameter present only here is dropped by the argument model without
    an error (the call succeeds and ignores the flag). See
    `tests/test_episode_search_scan_and_fetch.py` for the wire proof.
    """
    # Route capture_origin through the parent ``_handlers`` module so the
    # test suite's monkey-patch propagates here too — the same shim
    # discipline `memory_search` / `episode_handoff` use.
    from .. import _handlers as _h
    from ..origin import worktrees_match

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
    # `ids=[]` is UNSET, not "select nothing" — the same reading `scopes`
    # gets on the line above. That matters beyond ergonomics: an empty
    # list read as an explicit selection would take the worktree carve-out
    # below and turn every client that defaults the argument to `[]` into
    # a silent cross-worktree sweep.
    id_filter: set[str] | None = set(ids) if ids else None
    # Session-disabled scopes are an opt-out hide; honored uniformly
    # across the read surface (memory_search, memory_list) — episodes
    # are the third leg, so we mirror the same `excluded & scopes`
    # short-circuit pattern from list_active.py:46 / search.py:226.
    excluded_scopes: set[str] = set(state.disabled_scopes)

    # Worktree isolation, opt-in by default (mirrors `memory_search`'s
    # `auto_scope`), applied ONLY to the bare discovery walk — the branch
    # below where the caller named NO explicit selector (`swarm_id`,
    # `parent_session_id`, `ids`). episode_search spans every session
    # directory under the shared memory root (BETTERMEMORY_DIR), so a sweep
    # across two worktrees of the same repository that share a root would
    # otherwise leak each other's journal bodies — the asymmetric
    # cross-worktree read `episode_handoff` guards against on the
    # iteration-entry path (`_worktrees_equal_strict`).
    #
    # An EXPLICIT `swarm_id`, `parent_session_id` or `ids` is exempt:
    # naming a cohort, a session or specific episodes IS the scoping
    # intent, and the cross-worktree read is deliberate, not a leak —
    # mirroring how `episode_handoff` respects an explicit
    # `prior_session_id` verbatim
    # ("explicit consent that they own the cross-tree concern"). The swarm
    # fan-in is the load-bearing case: a coordinator gathers sub-agents
    # that each ran in their OWN worktree, so filtering by the
    # coordinator's worktree would drop every sub-agent episode and
    # silently defeat `list_by_swarm`. So the filter guards only the
    # no-selector walk, the one path where an unintended cross-worktree
    # leak is the genuine concern.
    #
    # `ids` is the third member for the same reason and one more: it is
    # the fetch half of the scan-then-fetch pattern, and the scans that
    # produce the ids are very often the two cross-worktree reads above.
    # Filtering it would hand a coordinator ids its own next call cannot
    # resolve. `episode_promote` already resolves a caller-supplied ULID
    # across every session directory on precisely this argument — and it
    # DELETES on commit, so a read that refused what that write accepts
    # would be the stranger asymmetry. `ids=[]` is unset (above), so the
    # exemption needs the FILTER, not the raw argument.
    #
    # We use the permissive `worktrees_match` (either side None → True)
    # rather than the handoff's strict rule because the bare walk is a
    # discovery surface: legacy / pre-origin episodes (no worktree_root)
    # and callers outside any git checkout must still pass through, the
    # same trade `should_include_for_caller` makes for `memory_search`.
    # `auto_scope=False` is the explicit escape hatch for an intentional
    # cross-worktree sweep of the bare walk.
    apply_worktree_filter = (
        auto_scope
        and swarm_id is None
        and parent_session_id is None
        and id_filter is None
    )
    caller_worktree: str | None = None
    if apply_worktree_filter:
        current_origin = _h.capture_origin()
        caller_worktree = current_origin.worktree_root if current_origin else None

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

    matched: list[Episode] = []
    for ep in candidates:
        # The id filter first — it is the cheapest test, and on the
        # fetch-by-id path it rejects almost everything the walk loaded.
        #
        # Post-load membership, deliberately, rather than building
        # `<session_dir>/<id>.md` and stat-ing it: the ULID IS the
        # filename, so the fast path is tempting, but a caller-supplied
        # id used as a path component is a traversal surface and episode
        # ids have no charset guard (unlike session ids — see
        # `_session_dir`). `episode_promote` builds that path only AFTER
        # resolving the id through a walk, so its id is known-good by
        # then. This costs exactly what the walk already cost.
        if id_filter is not None and ep.id not in id_filter:
            continue
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
        # Naming a floor in `ids` does not reopen it: one rule, and the
        # DESC / docs/api.md both say floors are off this surface.
        if ep.is_floor:
            continue
        if since_dt is not None and ep.created < since_dt:
            continue
        if scope_filter is not None and not (scope_filter & set(ep.scopes)):
            continue
        if excluded_scopes and (set(ep.scopes) & excluded_scopes):
            continue
        # Worktree isolation — ONLY on the bare discovery walk (see the
        # `apply_worktree_filter` rationale above). Drop episodes from a
        # different worktree of the same repository; legacy / None-origin
        # episodes pass through (permissive `worktrees_match`). An explicit
        # swarm_id / parent_session_id leaves apply_worktree_filter False so
        # the swarm fan-in and single-session lookups read across worktrees
        # as documented; auto_scope=False disables it for the bare walk too.
        if apply_worktree_filter:
            ep_worktree = ep.origin.worktree_root if ep.origin else None
            if not worktrees_match(ep_worktree, caller_worktree):
                continue
        matched.append(ep)

    # Sort by the `created` DATETIME, not the rendered ISO string. The
    # string form is lossy for the sort: `datetime.isoformat()` omits the
    # fractional-seconds component when microsecond == 0 (a bare-date or
    # whole-second `created` lifts to e.g. `…T00:00:00Z`), and lexically
    # `"."` < `"Z"`, so a whole-second timestamp would sort AFTER a
    # same-second fractional one — mis-windowing the most-recent-N cap and
    # breaking the docstring's "oldest-first within most-recent-N" order.
    # Keying on the datetime mirrors what `list_by_session` /
    # `list_by_swarm` already do at the storage layer.
    matched.sort(key=lambda ep: ep.created)
    # Cap to the most-recent N (matches `episode_handoff`'s
    # `all_eps[-max_episodes:]` pattern and caller intuition for ad-hoc
    # journal lookup — "what did I conclude across the last few
    # sessions?" reads the tail, not the head). The slice keeps the
    # ascending order inside the recent-N window so output stays
    # oldest-first within the surfaced subset.
    matched = matched[-max_results:]

    # `include_bodies=False` OMITS the `body` key rather than emitting it
    # empty. Emitting `""` would save ~11 characters of a row whose body
    # averages ~3,000 — the entire point of the flag is the body — and it
    # would leave a key present whose value is now a lie, which a caller
    # can neither branch on nor distinguish from a genuinely empty entry.
    out: list[dict[str, Any]] = [
        {
            "id": ep.id,
            "session_id": ep.session_id,
            "created": ep.created.isoformat().replace("+00:00", "Z"),
            "takeaway": ep.takeaway,
            **({"body": ep.body.strip()} if include_bodies else {}),
            "scopes": ep.scopes,
            "swarm_id": ep.swarm_id,
        }
        for ep in matched
    ]

    deps.recorder.record(
        "episode_search",
        scopes_filter=list(scopes) if scopes else None,
        parent_session_id=parent_session_id,
        swarm_id=swarm_id,
        since=since,
        max_results=max_results,
        auto_scope=auto_scope,
        include_bodies=include_bodies,
        # The COUNT, not the ids: the payload is telemetry, and a batch of
        # ULIDs there would be bulk with no analysis reading it.
        ids=len(id_filter) if id_filter is not None else None,
        returned=len(out),
    )
    return out


__all__ = ["DESC_EPISODE_SEARCH", "episode_search"]
