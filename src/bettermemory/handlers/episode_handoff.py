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

    Auto-resolution honors caller worktree isolation: a candidate
    session_id is only adopted when the session has at least one
    episode whose `origin.worktree_root` matches the caller's
    captured worktree. Two worktrees of one repository that share a
    memory root (BETTERMEMORY_DIR) would otherwise see each other's
    iteration state through this handoff — `memory_search` and
    `memory_scope_overview` enforce the same isolation, and the
    handoff primitive has to mirror it or it becomes the cross-tree
    leak path. When the caller has no worktree (e.g., running
    outside any git checkout), symmetric isolation only accepts
    sessions whose episodes also have no worktree origin — see
    `_worktrees_equal_strict`. An explicit `prior_session_id` is
    respected verbatim; the caller passing one in is explicit
    consent that they own the cross-tree concern.
    """
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if max_episodes is None:
        max_episodes = 5
    max_episodes = max(1, min(int(max_episodes), 50))

    # Session-disabled scopes hide episodes uniformly across the read
    # surface — same contract `memory_search` / `memory_list` honor
    # (list_active.py:46, search.py:226). For handoff the filter
    # cascades into the candidate-selection walk too: a session whose
    # episodes are ALL scope-suppressed behaves as if it wrote nothing,
    # so auto-resolution skips it and adopts the next-most-recent
    # session instead. An explicit `prior_session_id` still respects
    # the filter on the emit step (the caller named a session, but the
    # episode bodies themselves are still gated through the same hide
    # rule).
    excluded_scopes: set[str] = set(state.disabled_scopes)

    resolved_session_id: str | None = prior_session_id
    if resolved_session_id is None:
        from ..events import iter_all_events

        # Capture caller origin at handler entry — same shim
        # discipline scope_overview / search / write use. The
        # worktree_root field is the discriminator the auto-scope
        # filter on `should_include_for_caller` uses for memories;
        # we apply the same key here for episodes so the two
        # surfaces stay in sync about what "this worktree's prior
        # session" means.
        caller_origin = _h.capture_origin()
        caller_worktree = caller_origin.worktree_root if caller_origin else None

        # Walk the event log to collect candidate session_ids with
        # their max event timestamp (descending order = most recent
        # first). Same `find_prior_session_boundary` discipline
        # `memory_scope_overview` uses — anchor on the recorder id
        # because that's the id every event in the log carries.
        # Events themselves don't stamp origin today, so the
        # per-candidate worktree check happens against the episode
        # files on disk (which DO carry origin via episode_write).
        latest_ts_by_session: dict[str, str] = {}
        for ev in iter_all_events(deps.store.root):
            sid = ev.get("session") or ev.get("session_id")
            if not isinstance(sid, str) or sid == deps.recorder.session_id:
                continue
            ts = ev.get("ts")
            if not isinstance(ts, str):
                continue
            prev = latest_ts_by_session.get(sid)
            if prev is None or ts > prev:
                latest_ts_by_session[sid] = ts

        # Most recent first. Tiebreak on session_id for determinism
        # in the (very unlikely) ts-collision case across different
        # sessions; without it the dict-iteration order would leak
        # into the result.
        ordered = sorted(
            latest_ts_by_session.items(),
            key=lambda kv: (kv[1], kv[0]),
            reverse=True,
        )
        for sid, _ts in ordered:
            try:
                candidate_eps = deps.episode_store.list_by_session(sid)
            except ValueError:
                # Hostile session_id surfaced in the event log;
                # `list_by_session` validates the on-disk path
                # shape. Skip rather than crash the handler.
                continue
            # A candidate matches when EITHER:
            #   1. It has at least one episode whose origin's
            #      worktree_root matches the caller's under the
            #      strict (None-only-matches-None) rule, OR
            #   2. It has zero episodes at all (the session
            #      existed but either never wrote a journal, or
            #      had all of its episodes promoted away). In
            #      that case we surface `{sid, episodes: []}` so
            #      the caller can still distinguish "no prior
            #      session" from "prior session existed but is
            #      empty" — matching the original docstring
            #      contract. There's no run-state leak in this
            #      branch because there are no episode bodies
            #      to surface; only the bare session_id is
            #      exposed, which is an opaque ULID.
            # The discriminator under (1) is the worktree_root
            # itself, not the branch — one session can legitimately
            # span branches inside one worktree, so we don't
            # require ALL episodes to match.
            if not candidate_eps:
                resolved_session_id = sid
                break
            # Apply session-disabled-scope filter BEFORE the worktree
            # match. If every episode in this candidate is in a
            # suppressed scope, treat the session as having nothing
            # to surface (per the read-surface contract: hidden ==
            # not there for this session). The handoff walk then
            # continues to the next-most-recent candidate, which is
            # exactly the user's expectation when they `scope_disable`
            # a project: "rewind past the last X-session and surface
            # what came before".
            visible_eps = (
                [ep for ep in candidate_eps if not (set(ep.scopes) & excluded_scopes)]
                if excluded_scopes
                else candidate_eps
            )
            if not visible_eps:
                # Had episodes, but all hidden by disabled_scopes.
                # Walk past; do NOT surface this as an "empty" prior
                # session (that branch is reserved for the genuine
                # zero-episode case caught above).
                continue
            if any(
                _worktrees_equal_strict(
                    ep.origin.worktree_root if ep.origin else None,
                    caller_worktree,
                )
                for ep in visible_eps
            ):
                resolved_session_id = sid
                break

    episodes: list[dict[str, Any]] = []
    if resolved_session_id is not None:
        all_eps = deps.episode_store.list_by_session(resolved_session_id)
        # Apply the same scope-hide filter to the emit stream. This
        # matters in two cases the auto-resolution walk doesn't reach:
        #  - Caller passed `prior_session_id` explicitly, bypassing
        #    the candidate-walk filter — the bodies themselves are
        #    still gated.
        #  - Auto-resolved session mixed visible and hidden episodes;
        #    only the visible ones should be surfaced.
        if excluded_scopes:
            all_eps = [ep for ep in all_eps if not (set(ep.scopes) & excluded_scopes)]
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


def _worktrees_equal_strict(
    candidate_worktree: str | None,
    caller_worktree: str | None,
) -> bool:
    """Strict worktree equality for the handoff isolation filter.

    Unlike `origin.worktrees_match` (which is permissive: either
    side None → True so legacy memories without a worktree field
    pass through), this is the stricter rule the handoff needs:

      None == None → True
      "A" == "A"   → True
      None == "A"  → False
      "A" == None  → False
      "A" == "B"   → False

    The asymmetry vs. `worktrees_match` matters because the
    handoff is the iteration-entry adoption point for run-state.
    A leak here surfaces the WRONG worktree's takeaways as "what
    the prior session concluded" — the most embarrassing failure
    mode the audit named. Symmetric None-only-matches-None
    isolation closes that hole: a caller running outside any git
    checkout never inherits a session whose episodes were
    captured from inside a worktree (and vice versa). Legacy
    episodes written before the worktree_root field shipped
    (origin=None or origin.worktree_root=None) are visible only
    to callers in the same all-null state — a strictly tighter
    rule than `should_include_for_caller`'s, which is the right
    call for isolation-vs-discovery surface trade.
    """
    return candidate_worktree == caller_worktree


__all__ = ["DESC_EPISODE_HANDOFF", "episode_handoff"]
