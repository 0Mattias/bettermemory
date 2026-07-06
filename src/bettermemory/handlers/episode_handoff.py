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

- "no prior session in this worktree" — handoff returns
  `{"prior_session_id": None, "episodes": []}`. First-ever invocation
  in a worktree, or all prior sessions in the event log belong to a
  different worktree.
- "prior session existed but wrote no episodes" — returns
  `{"prior_session_id": "sess_xxx", "episodes": []}`. The prior
  session did work but didn't journal a takeaway. Since queue #28,
  events carry a `worktree_root` origin, so a zero-episode session's
  worktree IS known when its events were stamped: a caller in a named
  worktree adopts such a candidate only when the worktrees match, and
  falls back to the strict None-only-matches-None rule for legacy
  (pre-#28) events that lack the field. A caller with no worktree only
  adopts a candidate that is also worktree-less.
- "prior session recorded no takeaway" — returns
  `{"prior_session_id": "sess_xxx", "episodes": [], "note": "..."}`.
  The prior tick called `episode_handoff` (which wrote a session-tag
  floor anchoring its worktree on disk) but no `episode_write`
  followed. Because the floor is written unconditionally at entry,
  this is ambiguous: the tick may have crashed before its takeaway,
  or it may have been a clean read-only tick with nothing to record.
  The `note` key surfaces both readings and distinguishes this
  floor-only shape from the "no prior session at all" case. E2 fix.
- "prior session has takeaways" — `{"prior_session_id": "sess_xxx",
  "episodes": [...]}` with the latest N entries (oldest first within
  the slice).

At handler entry the implementation writes a session-tag FLOOR
episode for the current session BEFORE doing anything else. The
floor anchors the current session's worktree on disk so a tick
that crashes before `episode_write` still leaves a journal entry
the next tick's handoff can resolve via the worktree filter. The
write is idempotent — a session that already has any episode on
disk skips the floor write. The floor's `is_floor=True` flag is
how the emit step in this handler (and any future consumer that
distinguishes takeaway-bearing vs. anchor-only episodes) filters
them out of the takeaway summary surface.
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
    "Auto-resolution applies two implicit filters when "
    "`prior_session_id` is omitted: (1) caller-worktree strict "
    "equality — a candidate session is adopted only when at least "
    "one of its episodes carries an `origin.worktree_root` equal to "
    "the caller's captured worktree (`None` matches only `None`, so "
    "two worktrees of the same repo never see each other's prior "
    "sessions); (2) `disabled_scopes` cascade — sessions whose only "
    "episodes overlap the current session's `memory_scope_disable` "
    "set are filtered out of candidate adoption, and any surviving "
    "candidate's emitted episodes are themselves scope-filtered "
    "before return. Mirrors the same opt-out cascade memory_search "
    "/ memory_list honor.\n\n"
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
    `_worktrees_equal_strict`. Zero-episode candidates (sessions
    that recorded events but never wrote a journal entry) are matched
    on their events' `worktree_root` origin: since queue #28 events
    carry that field, a caller in a named worktree CAN adopt such a
    candidate when the worktrees match, and skips it otherwise. Legacy
    (pre-#28) events lack the field and fall back to the conservative
    None-only-matches-None rule, so a named-worktree caller never
    adopts a worktree-less legacy candidate.
    An explicit `prior_session_id` is respected verbatim; the
    caller passing one in is explicit consent that they own the
    cross-tree concern.
    """
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if max_episodes is None:
        max_episodes = 5
    max_episodes = max(1, min(int(max_episodes), 50))

    # ---- E2: floor-write at handoff entry -------------------------------
    #
    # Write a session-tag floor episode for the CURRENT session BEFORE
    # recording the handoff event. The /loop pattern is:
    #
    #   tick T:   episode_handoff()  (reads T-1's journal, records event)
    #             ... model does work ...
    #             episode_write(takeaway=...)  (writes T's journal)
    #   tick T+1: episode_handoff()  (reads T's journal, records event)
    #
    # If tick T crashes between handoff and episode_write (model
    # timeout, OOM, Ctrl-C, power loss), T has an event recorded
    # but ZERO journal files. T+1's handoff finds T's session_id in
    # the event log, calls list_by_session(T) → empty, hits the
    # zero-episode branch above. That branch ONLY adopts when the
    # caller's worktree is None (strict-filter contract). In a real
    # worktree (the production case), T+1 walks past T and silently
    # adopts T-1, dropping T's full history.
    #
    # The fix: at handoff ENTRY, write a tiny floor episode tagged
    # with the current session_id and origin (worktree). Even if T
    # crashes immediately after, T+1's handoff sees the floor in
    # `list_by_session(T)`, applies the worktree-filter against the
    # floor's origin.worktree_root, and adopts T correctly.
    #
    # Idempotent: a session that calls episode_handoff twice in the
    # same process (e.g., a /loop subagent that re-handshakes) only
    # gets one floor — the second call sees a non-empty
    # `list_by_session` and skips the write. A session that wrote a
    # real takeaway via episode_write before the second handoff
    # also skips (real takeaway → list is non-empty → floor would
    # be redundant noise).
    #
    # Ordering invariant — the floor write MUST happen BEFORE the
    # handoff event is recorded (`deps.recorder.record(...)` at the
    # bottom of this function). Crash analysis:
    #
    #   - Crash between (1) floor and (2) event: floor exists, no
    #     event. T+1's handoff doesn't see T (no event matching T's
    #     session in the log), so the floor is harmless leftover
    #     (TTL-pruned in 30 days).
    #
    #   - Crash between (2) event and (3) return: floor exists,
    #     event recorded. T+1's handoff sees T's event, finds the
    #     floor, worktree filter matches. T's session_id IS T+1's
    #     prior-session — the fix's main path.
    #
    #   - Crash AFTER (3) return but BEFORE T's episode_write:
    #     same as (2) above. The floor anchors T on disk; T+1's
    #     handoff resolves T correctly.
    #
    # The floor write itself raises if it fails (disk full, etc.) —
    # propagate to the MCP caller rather than silently degrading
    # the durability contract.
    handoff_origin = _h.capture_origin()
    _maybe_write_session_floor(deps, handoff_origin)

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

        # Reuse the origin captured at handler entry above (used to
        # tag the session-tag floor episode). Same shim discipline
        # scope_overview / search / write use. The `worktree_root`
        # field is the discriminator the auto-scope filter on
        # `should_include_for_caller` uses for memories; we apply
        # the same key here for episodes so the two surfaces stay
        # in sync about what "this worktree's prior session" means.
        caller_worktree = handoff_origin.worktree_root if handoff_origin else None

        # Walk the event log to collect candidate session_ids with
        # their max event timestamp (descending order = most recent
        # first). Same `find_prior_session_boundary` discipline
        # `memory_scope_overview` uses — anchor on the recorder id
        # because that's the id every event in the log carries.
        # Events now stamp `worktree_root` (queue #28, events.py
        # Recorder), so we also collect a per-session worktree here.
        # That lets the zero-episode branch below worktree-match a
        # session that wrote events but no episodes (a search-only
        # tick, or a tick that crashed before episode_write). A
        # session's events all share its process worktree, so any
        # stamped value is representative; legacy events without the
        # field leave the session's worktree unknown (None), which
        # keeps the conservative pre-queue-#28 behavior.
        latest_ts_by_session: dict[str, str] = {}
        worktree_by_session: dict[str, str] = {}
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
            if sid not in worktree_by_session:
                wt = ev.get("worktree_root")
                if isinstance(wt, str):
                    worktree_by_session[sid] = wt

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
            #   2. It has zero episodes at all AND the caller has
            #      no worktree (caller_worktree is None). In that
            #      case we surface `{sid, episodes: []}` so the
            #      caller can still distinguish "no prior session"
            #      from "prior session existed but is empty" —
            #      matching the original docstring contract.
            #      There's no run-state leak in this branch
            #      because there are no episode bodies to surface;
            #      only the bare session_id is exposed, which is
            #      an opaque ULID. However, the session_id IS the
            #      handle a caller would use to look up the prior
            #      session's events / memories — surfacing the
            #      WRONG worktree's session_id as "this worktree's
            #      prior session" violates the "this worktree"
            #      contract tick-2 (2988fff) established for
            #      episode-bearing sessions, so we extend the same
            #      isolation to the zero-episode branch.
            #
            #      Recorder.record stamps `worktree_root` on events
            #      (queue item #28, now landed), so we read the
            #      candidate's worktree from the event log
            #      (`worktree_by_session`) and apply the strict
            #      equality rule against it. A same-worktree session
            #      that wrote events but no episodes (a search-only
            #      tick, or one that crashed before episode_write) is
            #      now correctly adopted as `prior_session_id`. When
            #      the candidate's events predate the stamp (legacy)
            #      or were written outside a git checkout, its
            #      worktree is unknown (None) and the rule falls back
            #      to the conservative None-only-matches-None
            #      behavior — a caller in a worktree never inherits an
            #      unknown-worktree session.
            # The discriminator under (1) is the worktree_root
            # itself, not the branch — one session can legitimately
            # span branches inside one worktree, so we don't
            # require ALL episodes to match.
            if not candidate_eps:
                # Worktree read from the session's events (queue #28);
                # None when the events predate the stamp or were
                # written outside a git checkout.
                candidate_worktree = worktree_by_session.get(sid)
                if _worktrees_equal_strict(candidate_worktree, caller_worktree):
                    resolved_session_id = sid
                    break
                # Zero-episode candidate whose worktree doesn't match
                # the caller (or is unknown while the caller is in a
                # worktree) — under the strict "this worktree" contract
                # we cannot adopt it, so walk past to the next-most-
                # recent candidate.
                continue
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
    # E2: when the resolved session has ONLY floor episodes (no real
    # takeaway-bearing entries), surface a marker note so the caller
    # can render "prior session recorded no takeaway" instead of
    # treating the empty episodes list as "no prior session at all".
    # The floor is written UNCONDITIONALLY at handoff entry, so a
    # floor-only session is ambiguous: it may have crashed before its
    # episode_write, OR it may have been a clean read-only tick that
    # ran episode_handoff and had nothing to journal. Both are
    # observably distinct from "no prior session" (empty list, no
    # note); the note itself acknowledges both readings rather than
    # asserting a crash the on-disk shape can't actually prove. The
    # variable name is historical (advisory-only; nothing branches
    # on it downstream).
    prior_crashed_pre_takeaway = False
    if resolved_session_id is not None:
        # An explicit `prior_session_id` flows in verbatim (the
        # auto-resolution branch above only ever assigns a session_id
        # that already round-tripped through `list_by_session`). A
        # caller-supplied id — e.g. a child agent passed a mistyped or
        # path-shaped parent id — can fail `_session_dir`'s
        # `[A-Za-z0-9_-]` validator with a ValueError. On the /loop
        # iteration-entry hot path that should degrade to the graceful
        # `episodes: []` shape, not surface a raw ValueError, matching
        # how the auto-resolution walk (above) and `episode_search`
        # handle an invalid session_id. The validator fails closed, so
        # this is not a traversal — just a loud-vs-quiet failure-mode
        # choice, and quiet is what every other episode read does.
        try:
            all_eps = deps.episode_store.list_by_session(resolved_session_id)
        except ValueError:
            all_eps = []
        # Track whether the session is floor-only BEFORE filtering, so
        # the scope-disable cascade can't mask a crash signal (a
        # floor's scopes are always [] so the scope filter never
        # touches it, but we record the determination here for
        # downstream clarity).
        any_real_takeaway = any(not ep.is_floor for ep in all_eps)
        if all_eps and not any_real_takeaway:
            prior_crashed_pre_takeaway = True
        # Filter floors from the emit stream — they carry no takeaway
        # and the marker body is a placeholder, not content the model
        # should reason over as "what the prior session concluded".
        all_eps = [ep for ep in all_eps if not ep.is_floor]
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
        prior_crashed_pre_takeaway=prior_crashed_pre_takeaway,
    )
    result: dict[str, Any] = {
        "prior_session_id": resolved_session_id,
        "episodes": episodes,
    }
    if prior_crashed_pre_takeaway:
        # Additive surface key — only present when the floor-only
        # shape is adopted. A caller that doesn't know about the
        # field sees the same shape as before; a caller that does
        # can render the note below rather than silently treating
        # the empty list as "nothing to surface".
        #
        # The note deliberately does NOT assert a crash: the floor
        # is written UNCONDITIONALLY at handoff entry, so a
        # floor-only prior session is genuinely ambiguous between
        # (a) a crash after entry but before episode_write and
        # (b) a clean read-only tick that ran episode_handoff and
        # simply had no takeaway to journal. The on-disk shape is
        # identical (a bare `is_floor` marker with no entry-vs-exit
        # field), so we surface both readings instead of the
        # misleading bare "crashed" claim.
        result["note"] = (
            "Prior session recorded no takeaway before it ended: it "
            "called episode_handoff (which wrote the session-tag "
            "floor that anchored the worktree match) but no "
            "episode_write followed — either it crashed before the "
            "takeaway, or it was a clean read-only tick with nothing "
            "to record."
        )
    return result


def _maybe_write_session_floor(
    deps: "ToolHandlers",
    handoff_origin: Any,
) -> None:
    """Write a session-tag floor episode for the current session, if needed.

    Idempotent: a session that already has at least one episode on
    disk (floor or real) skips the write. The check uses
    `list_by_session(recorder.session_id)` — a cheap directory walk
    bounded by the per-session episode count (usually 0-10). Multi-MCP
    racing on the same session_id is serialised by the per-session
    flock inside `EpisodeStore.write_floor` (delegated to
    `_persist_episode`); two handoffs in the same process landing
    concurrently both check empty, both attempt the write, but the
    flock serialises them — the second one's `list_by_session` under
    the flock would see the first's floor, BUT we don't re-check
    under the flock because the cost of a rare duplicate floor (one
    extra small file in the session_dir, 100+ bytes) is far less than
    the latency of holding the flock across a directory walk on
    every handoff. The duplicate is benign: `episode_handoff`'s emit
    step filters both out, and the TTL prune handles cleanup at the
    30-day horizon.

    The handoff handler at entry calls this UNCONDITIONALLY; the
    decision-to-write happens here so the call site stays a one-
    liner and the discipline (capture_origin once, route through
    the recorder's session_id) is enforced in one place.
    """
    session_id = deps.recorder.session_id
    # Cheap existence check. `list_by_session` returns oldest-first
    # episodes; we only need to know whether any exists.
    try:
        existing = deps.episode_store.list_by_session(session_id)
    except ValueError:
        # Hostile session_id (shouldn't happen — the recorder's
        # session_id is generated by us and validated at construction).
        # If it does, surfacing a ValueError at handoff entry is
        # better than silently skipping the floor and shipping a
        # half-broken handoff.
        raise
    if existing:
        # Session already has at least one episode — floor would
        # be redundant. This branch fires when:
        #   - The handler is called twice in the same process (e.g.
        #     a /loop subagent re-handshakes its parent's session).
        #   - The session called `episode_write` before its first
        #     `episode_handoff` (uncommon but valid — the protocol
        #     doesn't enforce order, and a session that started
        #     writing journal entries before calling handoff has
        #     no need for a floor anchor).
        return
    deps.episode_store.write_floor(
        session_id=session_id,
        origin=handoff_origin,
    )


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
