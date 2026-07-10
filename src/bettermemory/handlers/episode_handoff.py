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
  `{"prior_session_id": "sess_xxx", "episodes": []}` plus the honest
  hedged `note`. The prior session did work but didn't journal a
  takeaway. Since queue #28, events carry a `worktree_root` origin, so
  a zero-episode session's worktree IS known when its events were
  stamped: a caller in a named worktree considers such a candidate only
  when the worktrees match, and falls back to the strict
  None-only-matches-None rule for legacy (pre-#28) events that lack the
  field. A caller with no worktree only considers a candidate that is
  also worktree-less. Like a floor-only session, a worktree-matching
  zero-episode session does NOT stop the rewind: the walk keeps going
  to an older real takeaway when one exists (surfacing THAT with the
  note), and only surfaces the zero-episode session itself (episodes
  `[]` + note) when none does.
- "immediately-prior session recorded no takeaway" — the most-recent
  worktree-matching session is floor-only. Because the floor is written
  unconditionally at handoff entry, this is ambiguous: the tick may have
  crashed before its takeaway, or it may have been a clean read-only tick
  with nothing to record. Two things happen, independently:
  (1) REWIND — the walk does NOT stop at the floor-only session; it keeps
  walking back to the most-recent session that has a REAL (non-floor)
  takeaway and surfaces THAT, so a floor-only tick between two real
  sessions can no longer sever the handoff chain. If an older real
  session exists the result is `{"prior_session_id": "<older>",
  "episodes": [...], "note": "..."}`; if none does it is
  `{"prior_session_id": "<floor-only>", "episodes": [], "note": "..."}`.
  (2) NOTE — an honest soft `note` is attached either way, acknowledging
  that the immediately-preceding session recorded no takeaway (crash OR
  clean read-only tick — the on-disk shape can't prove which). The
  `note` also distinguishes this shape from the "no prior session at
  all" case. E2 fix + rewind (episode-handoff-chain).
- "immediately-prior session's takeaway was PROMOTED out" — a third
  cause of both empty shapes above: `episode_promote` DELETES the
  source episode when the durable write commits (immediately, or via
  `memory_write_confirm` on the deferred pending path), so a healthy
  handoff → episode_write → episode_promote session ends floor-only on
  disk, and a write → promote session that never called handoff ends
  zero-episode. Byte-identical on disk to the crash / clean-tick
  shapes, but the EVENT LOG distinguishes it: the session's
  `episode_write` event carries the episode id, and a matching
  COMMITTED `episode_promote` (or a `write_confirm` event carrying an
  `episode_id`, stamped by the deferred confirm path, any session's)
  proves the deletion path. A bare pending promote is not proof — it
  may have been cancelled (a `write_cancel` now stamps the kept episode's
  id, the negative-proof counterpart) or expired, leaving the episode for
  a later prune to remove. When the proving signal is present the note
  says the takeaway was promoted into a durable memory (find it via
  memory_search) instead of hedging crash-or-empty. When a bare pending
  trace exists with NEITHER a commit proof NOR a cancel proof — an old
  log written before those id stamps existed, or a still-open/expired
  pending — the outcome is genuinely unprovable from the log, and the
  note HEDGES ("a promotion was staged, its outcome can't be confirmed
  here") rather than asserting a promotion OR falsely claiming nothing
  was journaled.
- "prior sessions exist but every takeaway is scope-hidden" — the
  most-recent worktree-matching session has REAL takeaways that are
  all hidden by this session's `memory_scope_disable` set, and no
  older visible takeaway exists either. While a visible takeaway is
  still reachable, the walk rewinds past fully hidden sessions
  transparently (no note — the user asked for that scope to be
  suppressed); but when the walk exhausts with nothing visible it
  surfaces the hidden immediately-prior session as `prior_session_id`
  with `episodes: []` plus a note naming the scope-hide cause. It
  never collapses to the first-ever `{prior_session_id: None}` shape
  while worktree sessions demonstrably exist.
- "prior session has takeaways" — `{"prior_session_id": "sess_xxx",
  "episodes": [...]}` with the latest N entries (oldest first within
  the slice). No `note` unless the immediately-prior session (a newer
  one the walk rewound past) was floor-only.

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

from typing import TYPE_CHECKING, Any, Literal

from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from pathlib import Path

    from .._handlers import ToolHandlers


# Three-valued verdict from `_episode_promoted_out_of_session`. A plain
# bool collapsed the last two: "provably NOT promoted" and "cannot be
# proven either way" both read as False, and the handoff then emitted the
# same "journaled no takeaway" note for a real (but unprovable) promotion
# as for a genuine non-handoff tick.
#   - "promoted": the event log PROVES a takeaway this session wrote went
#     through the promotion delete path (committed promote, or a
#     write_confirm carrying its episode id).
#   - "staged-unresolved": a bare `pending` promote of one of this
#     session's episodes exists, but the log carries NO commit proof AND
#     NO cancel proof — the outcome is genuinely unprovable (an old log
#     written before the confirm/cancel carried the episode id, or a
#     still-open/expired pending). The note HEDGES rather than asserting
#     either a promotion or that nothing was journaled.
#   - "none": no pending/committed promote trace for this session's
#     episodes at all — a genuine crash / clean-tick / non-handoff shape.
_PromotionTrace = Literal["promoted", "staged-unresolved", "none"]


DESC_EPISODE_HANDOFF = (
    "Read the most-recent journal takeaways from a prior session in "
    "this worktree. Call this FIRST at a /loop iteration entry — it "
    "answers 'what did the last session conclude here?' without "
    "needing memory_search. Episodes are the sibling-to-memory "
    "primitive for journal-shaped writes (see episode_write).\n\n"
    "When `prior_session_id` is omitted the handler auto-resolves it — "
    "the most-recent event-log session_id other than this process's "
    "own — under two implicit filters that mirror the opt-out cascade "
    "memory_search / memory_list honor: caller-worktree strict "
    "equality (`None` matches only `None`, so sibling worktrees stay "
    "isolated) and the `disabled_scopes` cascade (a session whose only "
    "takeaways are scope-disabled is skipped; surviving episodes are "
    "scope-filtered). Pass it explicitly to override (e.g. a child "
    "agent's parent id).\n\n"
    "Returns a dict:\n"
    "- `prior_session_id`: the resolved session id, or None when no "
    "prior session exists in the log.\n"
    "- `episodes`: list of {id, created, takeaway, body, scopes} "
    "dicts, oldest first, capped at `max_episodes` (default 5, cap "
    "50); each surfaces the writer's `takeaway` plus the full "
    "`body`.\n"
    "- `note` (optional `str`): set ONLY when the immediately-prior "
    "worktree session left nothing visible — floor-only (ran "
    "episode_handoff, no episode_write: crash or clean read-only tick), "
    "zero-episode (activity but no journal, no floor), promoted-out "
    "(its takeaway was promoted into a durable memory and deleted — "
    "find it via memory_search), or all-scope-hidden (takeaways all in "
    "a scope this session disabled). The last two DID journal, so a "
    "`note` never means 'wrote no journal'. `episodes` MAY be "
    "non-empty here — the walk rewinds past the empty/hidden session "
    "to an older takeaway (all-scope-hidden: always []).\n\n"
    "For ad-hoc lookup of an older session's journal, prefer "
    "`episode_search` with an explicit `parent_session_id`."
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

    REWIND contract (episode-handoff-chain fix): the walk does not stop
    at the most-recent worktree-matching session when that session is
    FLOOR-ONLY (an entry floor but no `episode_write` takeaway). A
    floor is written unconditionally at handoff ENTRY, so every clean
    read-only /loop tick leaves a floor-only session on disk; a naive
    "adopt the first worktree match" walk would adopt that empty tick
    and return `episodes: []`, severing the chain and hiding the real
    takeaways of the session before it. Instead the walk REWINDS past
    floor-only ticks to the most-recent session that carries a real
    (non-floor) takeaway and surfaces THAT. Independently, when the
    immediately-prior session was floor-only, an honest soft `note` is
    attached to the result. The note deliberately does NOT assert a
    crash: a floor-only session is byte-identical on disk whether the
    tick crashed after entry or was a clean read-only tick, so the note
    hedges both readings. When no older real session exists, the
    floor-only session itself is surfaced as `prior_session_id` with
    `episodes: []` plus the note.

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
    carry that field, a caller in a named worktree considers such a
    candidate when the worktrees match, and skips it otherwise. Legacy
    (pre-#28) events lack the field and fall back to the conservative
    None-only-matches-None rule, so a named-worktree caller never
    adopts a worktree-less legacy candidate. A worktree-matching
    zero-episode candidate is treated exactly like a floor-only one for
    REWIND purposes: it is remembered as the fallback prior id and the
    walk keeps going toward an older real takeaway, so a zero-episode
    session between the caller and an older real-takeaway session no
    longer severs the chain. It is surfaced (with the honest hedged
    note) only when no older real session exists.
    A worktree-matching session whose REAL episodes are all hidden by
    the caller's `disabled_scopes` gets the same fallback treatment:
    the walk rewinds past it toward an older visible takeaway (and
    stays silent about it when one is found — the transparent-rewind
    contract for scope_disable'd sessions), but when the walk exhausts
    with no visible takeaway anywhere the fully hidden session is
    surfaced as `prior_session_id` with `episodes: []` and a note
    naming the scope-hide cause. Pre-fix it acted as a match-terminator
    that suppressed every older fallback while contributing none
    itself, collapsing the result to the first-ever
    `{prior_session_id: None}` shape even though worktree sessions
    demonstrably existed.
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

    # Note flag: set when the IMMEDIATELY-prior worktree session was
    # floor-only (a crash-after-entry OR a clean read-only tick — the
    # on-disk shape can't tell them apart). Drives the honest soft note
    # on the result; it does NOT gate the rewind (we still surface an
    # older real takeaway when one exists).
    note_floor_only = False
    # Distinct from floor-only: an immediately-prior ZERO-episode session
    # (events recorded but no episode/floor on disk — a search-only tick, or a
    # crash before the entry floor landed). It has NO floor, so its note must
    # NOT claim one was written or that episode_handoff was called.
    note_zero_episode = False
    # Third shape: the walk exhausted with NO visible takeaway anywhere and
    # the immediately-prior worktree session has real takeaways that are all
    # hidden by this session's disabled_scopes. Deliberately set only at
    # fallback-resolution time (for/else below), never eagerly in the walk,
    # so the transparent-rewind contract holds: while an older VISIBLE
    # takeaway is reachable, a fully hidden session is skipped silently.
    note_all_hidden = False
    # The session each note above describes (the immediately-prior worktree
    # session for the auto walk, the named session for the explicit path).
    # Drives the promotion-trace lookup below: a floor-only / zero-episode
    # shape can also mean "its takeaway was promoted out" (episode_promote
    # deletes the journal source on commit), and the event log can tell.
    note_subject_sid: str | None = None

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
        # Walk most-recent-first. The FIRST worktree-matching candidate
        # is the "immediately-prior" session — the one a naive "adopt
        # the first match" walk would return. Under the REWIND contract
        # we do NOT stop there when it has no visible takeaway (a
        # floor-only tick): we keep walking back to the most-recent
        # session that actually recorded a real takeaway and surface
        # THAT. A floor is written UNCONDITIONALLY at handoff entry, so
        # every clean read-only /loop tick leaves a floor-only session;
        # the pre-rewind walk adopted that empty tick and severed the
        # chain, hiding the real takeaways of the session before it.
        #
        # `seen_worktree_match` tracks whether we've already passed the
        # immediately-prior worktree session (so a still-older floor-only
        # session can't masquerade as "immediately prior" for the note).
        # `floor_only_fallback_sid` remembers the immediately-prior
        # session when it was floor-only, so if NO older real session
        # exists we still surface it (with the note) rather than
        # collapsing to "no prior session at all".
        # `scope_hidden_fallback_sid` is its sibling for the case where
        # the immediately-prior session HAS real takeaways but they are
        # all hidden by disabled_scopes: the walk rewinds past it
        # (transparently, no note) toward an older visible takeaway,
        # but if none exists anywhere it is surfaced as the prior id
        # with the honest scope-hide note — never the first-ever
        # `{prior_session_id: None}` shape while worktree sessions
        # demonstrably exist.
        seen_worktree_match = False
        floor_only_fallback_sid: str | None = None
        scope_hidden_fallback_sid: str | None = None
        for sid, _ts in ordered:
            try:
                candidate_eps = deps.episode_store.list_by_session(sid)
            except ValueError:
                # Hostile session_id surfaced in the event log;
                # `list_by_session` validates the on-disk path shape.
                # Skip rather than crash the handler.
                continue

            if not candidate_eps:
                # Zero-episode candidate: a session that recorded events
                # but never wrote any episode (a search-only tick, one that
                # crashed before the entry floor landed, a legacy pre-#28
                # session, or a session whose only real takeaway was later
                # promoted out). There's no run-state leak — no episode
                # bodies, only the bare opaque ULID — but the session_id IS
                # the handle a caller uses to look up the prior session's
                # events, so we still apply the strict "this worktree"
                # contract. Recorder.record stamps `worktree_root` on events
                # (queue #28), read here from `worktree_by_session`; a
                # legacy/no-checkout candidate has an unknown (None)
                # worktree and falls back to the conservative
                # None-only-matches-None rule, so a caller in a named
                # worktree never inherits it.
                candidate_worktree = worktree_by_session.get(sid)
                if _worktrees_equal_strict(candidate_worktree, caller_worktree):
                    # REWIND parity with the floor-only branch below. Like a
                    # floor-only tick, a zero-episode worktree session has
                    # NO visible takeaway to surface, so it must NOT
                    # adopt-and-break AHEAD of an older real takeaway — doing
                    # so would let a zero-episode session sitting between the
                    # caller and an older real-takeaway session sever the
                    # handoff chain (the exact bug the rewind fixed for
                    # floor-only sessions). Treat it identically: remember it
                    # as the fallback prior id and, when it is the
                    # immediately-prior worktree match, flag the honest soft
                    # note; then KEEP WALKING toward an older visible
                    # takeaway. It is surfaced (via the for/else fallback
                    # below) as `{sid, episodes: []}` + note only when no
                    # older real session exists.
                    if not seen_worktree_match:
                        note_zero_episode = True
                        floor_only_fallback_sid = sid
                        note_subject_sid = sid
                    seen_worktree_match = True
                    continue
                # Worktree mismatch (or unknown while caller is in a
                # worktree) — walk past to the next-most-recent candidate.
                continue

            # A candidate belongs to the caller's worktree when ANY of
            # its episodes (floor or real) carries a matching origin
            # under the strict None-only-matches-None rule. The
            # discriminator is the worktree_root itself, not the branch —
            # one session can legitimately span branches inside one
            # worktree, so we don't require ALL episodes to match.
            worktree_matches = any(
                _worktrees_equal_strict(
                    ep.origin.worktree_root if ep.origin else None,
                    caller_worktree,
                )
                for ep in candidate_eps
            )
            if not worktree_matches:
                continue

            # Visible, takeaway-bearing episodes. Floors carry no
            # takeaway; disabled scopes hide episodes uniformly across
            # the read surface (list_active.py:46, search.py:226), so a
            # scope the caller suppressed does not count as a takeaway to
            # rewind to.
            visible_real = [
                ep
                for ep in candidate_eps
                if not ep.is_floor
                and not (excluded_scopes and (set(ep.scopes) & excluded_scopes))
            ]
            if visible_real:
                # Most-recent worktree session that actually surfaces a
                # takeaway — adopt it. We may have rewound past newer
                # floor-only ticks to reach it; that's the whole point,
                # and `note_floor_only` (set below) records whether we
                # did so the honest soft note still fires.
                resolved_session_id = sid
                break

            # No visible takeaway in this worktree session. Two shapes:
            #   - floor-only (every episode is a floor): the ambiguous
            #     crash / clean-read-only-tick shape. If this is the
            #     MOST-recent worktree match it is the immediately-prior
            #     session → flag the honest soft note and remember it as
            #     the fallback prior id (used when no older real session
            #     turns up).
            #   - real episodes all hidden by disabled_scopes: the user
            #     explicitly suppressed that scope, so rewind past it
            #     toward an older VISIBLE takeaway without a note (the
            #     "rewind past the scope_disable'd session" contract).
            #     But it is a fallback CANDIDATE, not a match-terminator:
            #     if it is the immediately-prior worktree match, remember
            #     it so the for/else below can surface it — with the
            #     honest scope-hide note — when the walk exhausts with no
            #     visible takeaway anywhere. Pre-fix this branch set
            #     `seen_worktree_match` while remembering nothing, which
            #     both suppressed every OLDER floor-only / zero-episode
            #     fallback and contributed no fallback itself, collapsing
            #     the result to the first-ever `{prior_session_id: None}`
            #     shape despite worktree sessions demonstrably existing.
            has_real_episode = any(not ep.is_floor for ep in candidate_eps)
            if not seen_worktree_match:
                if not has_real_episode:
                    note_floor_only = True
                    floor_only_fallback_sid = sid
                    note_subject_sid = sid
                else:
                    scope_hidden_fallback_sid = sid
            seen_worktree_match = True
            # REWIND: keep walking toward an older visible takeaway.
            continue
        else:
            # Walk exhausted without a visible-takeaway session. If the
            # immediately-prior session was floor-only / zero-episode,
            # surface it (with the note) so the chain still reports
            # "prior existed, no takeaway" rather than "no prior session
            # at all". Failing that, if the immediately-prior session's
            # real takeaways were all scope-hidden, surface THAT with the
            # scope-hide note. The two fallbacks are mutually exclusive —
            # only the immediately-prior worktree match (gated on
            # `seen_worktree_match` above) can claim either slot — so the
            # elif is an either/or, not a priority order.
            if resolved_session_id is None:
                if floor_only_fallback_sid is not None:
                    resolved_session_id = floor_only_fallback_sid
                elif scope_hidden_fallback_sid is not None:
                    resolved_session_id = scope_hidden_fallback_sid
                    note_all_hidden = True

    episodes: list[dict[str, Any]] = []
    # `note_floor_only` may already be True from the auto-resolution walk
    # (the immediately-prior worktree session was floor-only, whether or
    # not we then rewound to an older real session). The explicit-
    # `prior_session_id` path bypasses that walk, so we derive the
    # floor-only determination for a named session below.
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
        # Explicit-`prior_session_id` path only: the walk never ran, so
        # if the caller named a floor-only session, surface the same
        # honest note. (The auto path already set `note_floor_only` when
        # the immediately-prior session was floor-only, and in the rewind
        # case `resolved_session_id` is the OLDER real session — deriving
        # from it here would wrongly clear the flag, so we gate on
        # `prior_session_id is not None`.)
        if prior_session_id is not None:
            any_real_takeaway = any(not ep.is_floor for ep in all_eps)
            if all_eps and not any_real_takeaway:
                note_floor_only = True
                note_subject_sid = resolved_session_id
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

    # Promotion trace: the floor-only / zero-episode shapes have a third
    # cause besides crash / clean-tick — `episode_promote` DELETES the
    # source episode when the durable write commits (immediately, or via
    # `memory_write_confirm` on the deferred pending path), so a healthy
    # handoff → episode_write → episode_promote session ends floor-only on
    # disk and a write → promote session with no handoff ends zero-episode.
    # The event log can tell those apart: `episode_write` events carry the
    # episode id + writer session, and a COMMITTED `episode_promote` (or a
    # `write_confirm` event carrying an `episode_id`, stamped by the
    # deferred confirm path) proves the delete ran — a bare pending promote
    # does not. But "not proven promoted" splits two ways: PROVABLY not
    # (a `write_cancel` stamped with the kept episode id) versus UNPROVABLE
    # (an old log, or a still-open/expired pending) — the verdict carries
    # that distinction so the note can hedge instead of falsely claiming
    # nothing was journaled. Only consulted when a note is about to fire
    # (the uncommon path), so the common no-note handoff pays nothing.
    promotion_trace: _PromotionTrace = "none"
    if (note_floor_only or note_zero_episode) and note_subject_sid is not None:
        promotion_trace = _episode_promoted_out_of_session(
            deps.store.root, note_subject_sid
        )

    deps.recorder.record(
        "episode_handoff",
        prior_session_id=resolved_session_id,
        max_episodes=max_episodes,
        returned=len(episodes),
        prior_crashed_pre_takeaway=note_floor_only,
    )
    result: dict[str, Any] = {
        "prior_session_id": resolved_session_id,
        "episodes": episodes,
    }
    # Additive surface key — only present when the immediately-prior worktree
    # session left no takeaway. A caller that doesn't know about the field sees
    # the same shape as before. `episodes` may be NON-empty here: the walk
    # rewound past the empty session to an older real takeaway, and the note
    # flags that the most recent session left nothing. The empty shapes get
    # distinct text because their on-disk cause differs.
    if note_floor_only:
        if promotion_trace == "promoted":
            # Not ambiguous after all: the event log shows this session's
            # episode_write followed by a matching episode_promote, and
            # promotion deletes the journal source on commit — that is why
            # only the floor remains. Say so instead of hedging
            # crash-or-empty, both of which would be false here.
            result["note"] = (
                "The immediately-preceding session recorded a takeaway, "
                "but it was promoted into a durable memory and the "
                "journal source deleted on commit (episode_promote) — "
                "only the session-tag floor remains on disk. The event "
                "log shows the session's episode_write followed by a "
                "matching episode_promote, so this is a promotion, not a "
                "crash or an empty tick; memory_search can surface the "
                "promoted content. Any takeaways above (if present) come "
                "from an older session in this worktree that the handoff "
                "rewound to."
            )
        elif promotion_trace == "staged-unresolved":
            # A takeaway WAS journaled and staged for promotion (so the
            # crash / clean-read-only-tick hedge below would be a lie — it
            # falsely implies no episode_write ran), but the event log
            # cannot confirm the promotion's outcome: a bare `pending`
            # promote with no committed/confirmed proof and no cancel
            # proof. Hedge honestly on the outcome rather than asserting a
            # promotion (it may never have committed) OR asserting nothing
            # was journaled (a takeaway demonstrably was).
            result["note"] = (
                "The immediately-preceding session called episode_handoff "
                "(which wrote the session-tag floor that anchored the "
                "worktree match) and staged a takeaway for promotion into "
                "a durable memory, but this event log cannot confirm the "
                "outcome: the promotion may have committed (for example, a "
                "log written before the confirm event recorded the "
                "source-episode id) or it may have been cancelled or "
                "expired. If it committed, memory_search can surface the "
                "promoted content; if not, no durable memory was written. "
                "Only the session-tag floor remains on disk. Any takeaways "
                "above (if present) come from an older session in this "
                "worktree that the handoff rewound to."
            )
        else:
            # Floor-only: a real `is_floor` marker exists on disk (the session
            # called episode_handoff, which writes the entry floor). Genuinely
            # ambiguous between (a) a crash after entry but before
            # episode_write and (b) a clean read-only tick that ran
            # episode_handoff with no takeaway — the on-disk shape is
            # identical, so surface both readings rather than the misleading
            # bare "crashed" claim.
            result["note"] = (
                "The immediately-preceding session recorded no takeaway "
                "before it ended: it called episode_handoff (which wrote "
                "the session-tag floor that anchored the worktree match) "
                "but no episode_write followed — either it crashed before "
                "the takeaway, or it was a clean read-only tick with "
                "nothing to record. Any takeaways above (if present) come "
                "from an older session in this worktree that the handoff "
                "rewound to."
            )
    elif note_zero_episode:
        if promotion_trace == "promoted":
            # Same promotion cause, zero-episode flavor: the session wrote a
            # takeaway WITHOUT ever calling episode_handoff (so no floor),
            # and the promotion deleted the journal source — nothing remains
            # on disk even though the session demonstrably journaled.
            result["note"] = (
                "The immediately-preceding session in this worktree "
                "journaled a takeaway, but it was promoted into a durable "
                "memory and the journal source deleted on commit "
                "(episode_promote); the session left no handoff floor, so "
                "nothing remains on disk. The event log shows its "
                "episode_write followed by a matching episode_promote — a "
                "promotion, not a crash or a journal-less tick; "
                "memory_search can surface the promoted content. Any "
                "takeaways above (if present) come from an older session "
                "in this worktree that the handoff rewound to."
            )
        elif promotion_trace == "staged-unresolved":
            # Zero-episode flavor of the unprovable-promotion hedge: a
            # takeaway was journaled and staged for promotion (so the
            # "journaled no takeaway / non-handoff tick" text below would be
            # an actively false claim), but the event log cannot confirm the
            # promotion's outcome — a bare `pending` promote with no
            # committed/confirmed proof and no cancel proof. State what is
            # known (a promotion was staged) and hedge the outcome; never
            # assert nothing was journaled. Still true, and kept: no floor
            # exists (the session never called episode_handoff).
            result["note"] = (
                "The immediately-preceding session in this worktree staged "
                "a takeaway for promotion into a durable memory (and left "
                "no handoff floor), but this event log cannot confirm the "
                "outcome: the promotion may have committed (for example, a "
                "log written before the confirm event recorded the "
                "source-episode id) or it may have been cancelled or "
                "expired. If it committed, memory_search can surface the "
                "promoted content; if not, no durable memory was written. "
                "Any takeaways above (if present) come from an older "
                "session in this worktree that the handoff rewound to."
            )
        else:
            # Zero-episode: NO floor on disk — the worktree match came from an
            # event's `worktree_root`, not a floor. So do NOT claim a floor
            # was written or that episode_handoff was called (it wasn't): the
            # session recorded activity (e.g. a search-only tick) but
            # journaled nothing, or crashed before its entry floor landed.
            result["note"] = (
                "The immediately-preceding session in this worktree recorded "
                "activity but journaled no takeaway (and left no handoff "
                "floor) — it may have been a non-handoff tick, or crashed "
                "before journaling. Any takeaways above (if present) come "
                "from an older session in this worktree that the handoff "
                "rewound to."
            )
    elif note_all_hidden:
        # Scope-hidden terminal shape: the immediately-prior worktree session
        # HAS real takeaways, but every one of them is in a scope this
        # session disabled, and no older visible takeaway exists either. The
        # floor-only / zero-episode texts would both be lies here (the
        # session journaled fine), and returning no note would be
        # indistinguishable from "prior session wrote nothing". `episodes` is
        # always [] on this branch — the emit-step scope filter hides the
        # same episodes the walk could not surface.
        result["note"] = (
            "The immediately-preceding session in this worktree recorded "
            "takeaways, but every one of them is in a scope this session "
            "has disabled (memory_scope_disable), so none can be shown. "
            "The prior session was not empty and this is not the first "
            "handoff in this worktree — re-enable the relevant scope via "
            "memory_scope_enable to surface its takeaways."
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


def _episode_promoted_out_of_session(root: "Path", session_id: str) -> _PromotionTrace:
    """Classify what the event log can prove about an episode WRITTEN BY
    `session_id` that is no longer on disk. Returns one of:

      - "promoted": the log PROVES a takeaway this session wrote went
        through the promotion delete path.
      - "staged-unresolved": a bare `pending` promote of one of this
        session's episodes exists, but the log carries no commit proof
        AND no cancel proof — the outcome is genuinely unprovable.
      - "none": no pending/committed promote trace for this session's
        episodes at all — a genuine crash / clean-tick / non-handoff shape.

    `episode_promote` deletes the source episode when the durable write
    commits — synchronously on `status="committed"`, or via
    `memory_write_confirm` when the write staged as `status="pending"`
    (the user-inference confirmation flow). Either way the session that
    wrote the episode ends up floor-only (if it had called
    episode_handoff) or zero-episode (if it hadn't) on disk,
    byte-identical to the crash / clean-tick shapes the handoff notes
    hedge about. The event log disambiguates:

      - `episode_write` events carry the episode's ULID (`id`) and the
        writer's session — collect the ids `session_id` wrote.
      - a SYNCHRONOUS `episode_promote` (`write_status="committed"`)
        deletes the source episode inline — POSITIVE proof; collect its
        `episode_id`. The promoter session is deliberately NOT filtered:
        a later session promoting an older session's takeaway (the
        documented /loop pattern) still deletes the OLDER session's entry.
      - a `write_confirm` event that carries an `episode_id` is the
        deferred confirm path's POSITIVE proof: `memory_write_confirm`
        stamps the deleted source-episode id onto that event only when
        the confirmed write was a promotion — collect those too.
      - a `write_cancel` event that carries an `episode_id` is the
        NEGATIVE-proof counterpart: `memory_write_cancel` stamps the KEPT
        source-episode id onto that event when a staged promotion is
        dropped, so the episode was demonstrably NOT promoted. A later
        prune can still rmtree the whole session dir, producing the same
        zero-episode absence a real promotion would — this stamp is what
        tells the two apart.

    Verdict logic (all sets are episode ids):
      - `written & promoted` non-empty → "promoted".
      - else a bare `pending` promote of a written episode that is
        neither in `promoted` nor in `cancelled` → "staged-unresolved".
      - else → "none".

    Why a bare `pending` promote is NOT read as proof on its own: it is
    recorded at STAGING time, before the outcome is known. The deferred
    delete happens later inside `memory_write_confirm` (stamping the
    confirm event); a cancel drops it (stamping the cancel event); an
    unconfirmed pending simply TTL-expires with no further event. A
    cancelled/expired pending leaves the episode ON disk, and
    `prune_old_sessions` then rmtrees the whole session directory 30 days
    on — producing the exact zero-episode absence a real promotion would.
    So on-disk absence + a bare pending cannot, by itself, prove a
    promotion; the committed/confirmed stamp proves it did, the cancel
    stamp proves it did not, and a pending with NEITHER is unprovable and
    hedged (never collapsed into the false "nothing was journaled" note).

    UNRECOVERABLE OLD LOGS: an event log written before these id stamps
    existed carries neither on its `write_confirm`/`write_cancel` — and
    the bare `episode_promote(pending)` never carried a linking key to
    the confirm/cancel either. So for a genuinely-committed pre-stamp
    promotion NO code change here can recover proof; it lands in
    "staged-unresolved" and honestly hedges. (A symmetric FORWARD
    hardening — a `pending_id` on the `episode_promote` event, joinable to
    the confirm/cancel's `pending_id` — would let a future promoter that
    somehow lost the episode-id stamp still be joined; it is recorded on
    the promote event, owned elsewhere, and would not help these old logs
    regardless, so it is intentionally out of scope here.)

    Cost: one pass over `iter_all_events` (active log + gz archives).
    Only invoked when a floor-only / zero-episode note is about to
    fire, which is the uncommon handoff outcome; the healthy adopt-a-
    takeaway path never pays it.
    """
    from ..events import iter_all_events

    written: set[str] = set()
    promoted: set[str] = set()
    cancelled: set[str] = set()
    staged_pending: set[str] = set()
    for ev in iter_all_events(root):
        kind = ev.get("kind")
        if kind == "episode_write":
            # Same session-field fallback discipline as the resolution
            # walk: current events stamp `session`, tolerate legacy
            # `session_id`.
            sid = ev.get("session") or ev.get("session_id")
            eid = ev.get("id")
            if sid == session_id and isinstance(eid, str):
                written.add(eid)
        elif kind == "episode_promote":
            eid = ev.get("episode_id")
            if not isinstance(eid, str):
                continue
            status = ev.get("write_status")
            if status == "committed":
                # Synchronous commit deleted the source episode inline —
                # positive proof.
                promoted.add(eid)
            elif status == "pending":
                # Staged, outcome not yet known at record time. Held aside;
                # only a later commit/confirm proof (→ promoted) or cancel
                # proof (→ cancelled) resolves it. A pending left in neither
                # is the unprovable case.
                staged_pending.add(eid)
        elif kind == "write_confirm":
            # A `write_confirm` carrying an `episode_id` is the durable,
            # confirm-TIME proof that a DEFERRED promotion's delete ran:
            # `memory_write_confirm` stamps the deleted source-episode id
            # onto this event only on the promotion path (a normal confirm
            # records episode_id=None, filtered out by the isinstance check).
            eid = ev.get("episode_id")
            if isinstance(eid, str):
                promoted.add(eid)
        elif kind == "write_cancel":
            # A `write_cancel` carrying an `episode_id` is the confirm-time
            # NEGATIVE proof: `memory_write_cancel` stamps the KEPT
            # source-episode id when a staged promotion is dropped, so this
            # episode was demonstrably not promoted. Separates a
            # provably-cancelled pending (→ honest "no takeaway" note) from
            # an unprovable one (→ hedged note). A normal cancel records
            # episode_id=None, filtered out by the isinstance check.
            eid = ev.get("episode_id")
            if isinstance(eid, str):
                cancelled.add(eid)
    if written & promoted:
        return "promoted"
    # A bare pending promote of one of THIS session's episodes, with no
    # positive commit proof and no negative cancel proof: unprovable.
    if (written & staged_pending) - promoted - cancelled:
        return "staged-unresolved"
    return "none"


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
