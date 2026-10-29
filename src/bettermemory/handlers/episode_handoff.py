"""episode_handoff MCP tool — handler implementation + DESC.

The read counterpart to `episode_write`. Surfaces the most-recent N
takeaways from the prior session in the same checkout, designed as
the FIRST MCP call at a /loop iteration entry. Closes the "no
forced-read at iteration entry" gap the audit identified: opt-in
`memory_search` works for stateless iterations, but iterations that
depend on prior-iteration state need a primitive that says "what did
the last session conclude here?" — that's this tool.

When `prior_session_id` is omitted, the handler resolves it from the
store's episodes of other sessions through `Membership.handoff` (design
3.6), which keys a session to a checkout by its episodes' homes: the
newest session that wrote in the caller's checkout; and for a primary
checkout with none of its own, the newest session of a primary checkout
of its project that is not live here, labelled (`inherited_from`).
Nothing takes a live sibling's run-state, and a caller outside any
checkout has none to hand off. A caller that knows the parent session
id (e.g., a /loop subagent that was passed its parent's session_id) can
pass it explicitly.

Returns `None`-rich shape so the caller can distinguish:

- "no prior session in this checkout": handoff returns
  `{"prior_session_id": None, "episodes": []}`. First-ever invocation
  in a checkout, or every prior session wrote its episodes in another
  checkout and left no row here.
- "immediately-prior session journaled nothing" (decision B22): the
  newest session that stood in this checkout wrote no episode at all, no
  floor and no takeaway. A session's episodes tie it to the checkout
  they are homed in; one with none is tied by its own rows: the
  `session_brief` and `prompt_recall` rows carry the caller's checkout
  from rule rs-v5 (design 8.11). A session with no such row here is not
  this checkout's. The walk rewinds past it like a floor-only session,
  and the `note` says it journaled nothing and left no floor; with no
  older takeaway it is surfaced itself, `episodes: []`, under the id
  its rows carry.
- "immediately-prior session recorded no takeaway" — the most-recent
  session of this checkout is floor-only. Because the floor is written
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
  most-recent session of this checkout has REAL takeaways that are
  all hidden by this session's `memory_scope_disable` set, and no
  older visible takeaway exists either. While a visible takeaway is
  still reachable, the walk rewinds past fully hidden sessions
  transparently (no note — the user asked for that scope to be
  suppressed); but when the walk exhausts with nothing visible it
  surfaces the hidden immediately-prior session as `prior_session_id`
  with `episodes: []` plus a note naming the scope-hide cause. It
  never collapses to the first-ever `{prior_session_id: None}` shape
  while sessions of this checkout demonstrably exist.
- "prior session has takeaways" — `{"prior_session_id": "sess_xxx",
  "episodes": [...]}` with the latest N entries (oldest first within
  the slice). No `note` unless the immediately-prior session (a newer
  one the walk rewound past) was floor-only.

Since 7.0.0 every emitted episode carries `provenance`, read from the
event log rather than the file (`provenance.EpisodeEvidence`): `local`
when an `episode_write` event names the id, `untracked` when the log
holds no in-process event that could have named it, `unaccounted` when
the file appeared under a session directory with no event, the planted
shape. Bodies are delivered on request only (`include_bodies`, default
False), and never for an unaccounted episode whatever the caller
passed: this is the reflexive first call of every loop iteration, so a
file dropped into a legitimate session's directory must not land whole
in context at entry. The takeaway and the scopes stay, with the label
beside them; `episode_search(ids=[...])` is the explicit read that
serves a body beside its label. The evidence is read for the emitted
rows only (`Store.episode_provenance`).

At handler entry the implementation writes a session-tag FLOOR
episode for the current session BEFORE doing anything else, homed to
the caller's checkout like any episode. The floor anchors the current
session in its checkout so a tick that crashes before `episode_write`
still leaves a journal entry the next tick's handoff finds there. The
write is idempotent — a session that already has any episode on
disk skips the floor write. The floor's `is_floor=True` flag is
how the emit step in this handler (and any future consumer that
distinguishes takeaway-bearing vs. anchor-only episodes) filters
them out of the takeaway summary surface.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .. import identity
from ..events import HARNESS_SESSION_FIELD
from ..store import UNACCOUNTED
from ..time_utils import parse_event_ts
from ._shared import Context, _advance_turn, caller_anchoring

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..location import Caller
    from ..store import Store

#: The rows that place a conversation in a checkout (decision B22): the
#: session brief's and the prompt recall's, which carry the caller's
#: checkout from rule rs-v5 (design 8.11).
_CHECKOUT_ROW_KINDS: tuple[str, ...] = ("session_brief", "prompt_recall")
#: The journal calls whose rows carry the conversation their server
#: session served (`events.HARNESS_SESSION_FIELD`): a conversation one of
#: them names journaled.
_JOURNAL_KINDS: tuple[str, ...] = ("episode_handoff", "episode_write")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


DESC_EPISODE_HANDOFF = (
    "Read the most-recent journal takeaways from a prior session in "
    "this checkout. Call this FIRST at a /loop iteration entry: it "
    "answers 'what did the last session conclude here?' without "
    "memory_search. Episodes are the sibling-to-memory primitive for "
    "journal-shaped writes (see episode_write).\n\n"
    "When `prior_session_id` is omitted the handler auto-resolves it "
    "from the episodes written in the caller's checkout (a primary "
    "checkout with none is offered a non-live sibling's, labelled in "
    "`inherited_from`) and the `disabled_scopes` cascade (a session "
    "whose only takeaways are scope-disabled is skipped; surviving "
    "episodes are scope-filtered). Pass it explicitly to override.\n\n"
    "Returns a dict:\n"
    "- `prior_session_id`: the resolved session id, or None when no "
    "prior session exists in the log.\n"
    "- `episodes`: list of {id, created, takeaway, scopes, provenance} "
    "dicts, oldest first, capped at `max_episodes` (default 5, cap "
    "50). `provenance` is read from the event log: `local` (an "
    "episode_write event names the id), `untracked` (no event could "
    "have) or `unaccounted` (the file appeared with no event: the "
    "planted shape). `body` is present only with `include_bodies=True` "
    "(default False) and never for an unaccounted episode; "
    "episode_search(ids=[...]) is the explicit read for one body.\n"
    "- `note` (optional `str`): set ONLY when the immediately-prior "
    "session of this checkout left nothing visible; names the cause: "
    "floor-only, zero-episode, promoted-out, or all-scope-hidden. The "
    "last two DID journal, so a `note` never means 'wrote no journal'. "
    "`episodes` MAY be non-empty: the walk rewinds to an older takeaway."
)


@dataclass
class _HandoffPick:
    """The session a handoff surfaces, the episodes it emits and its note flags."""

    session_id: str | None
    # Note flags: the immediately-prior session of the checkout was
    # floor-only (a crash-after-entry OR a clean read-only tick), or its
    # real takeaways are all hidden and nothing older is visible.
    note_floor_only: bool = False
    note_all_hidden: bool = False
    # The immediately-prior session of the checkout journaled nothing at
    # all (decision B22): no floor, no takeaway, only its rows here.
    note_zero_episode: bool = False
    inherited_from: str | None = None
    # The episodes of the adopted session the auto path emits (None: every
    # episode of the named session, the explicit path).
    chosen: set[str] | None = None


def _write_entry_floor(deps: ToolHandlers, caller: Caller) -> None:
    """Take the caller; return None, after writing the current session's floor if it has none."""
    from .. import _handlers as _h

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
    # timeout, OOM, Ctrl-C, power loss), T has an event recorded but
    # ZERO journal files, and nothing ties T to its checkout: T+1's
    # handoff would walk past T and adopt T-1, dropping T's history.
    #
    # The fix: at handoff ENTRY, write a tiny floor episode tagged
    # with the current session_id and homed to the caller's checkout.
    # Even if T crashes immediately after, T+1's handoff sees the floor
    # as an episode of its checkout and adopts T correctly.
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
    # bottom of `episode_handoff`). A crash between the two leaves a
    # floor and no event: harmless, TTL-pruned in 30 days, and the next
    # tick adopts it as the floor-only prior session it is.
    #
    # The floor write itself raises if it fails (disk full, etc.) —
    # propagate to the MCP caller rather than silently degrading
    # the durability contract.
    handoff_origin = _h.capture_origin()
    _maybe_write_session_floor(deps, handoff_origin, caller)


def _auto_pick(
    deps: ToolHandlers, caller: Caller, excluded_scopes: set[str]
) -> _HandoffPick:
    """Take the caller and its hidden scopes; return the prior session the walk resolves."""
    from ..membership import EpisodeRef

    pick = _HandoffPick(session_id=None)
    own_session = deps.recorder.session_for_request()
    candidates = {
        ep.id: ep for ep in deps.store.iter_episodes() if ep.session_id != own_session
    }

    def visible_real(ep: Any) -> bool:
        return not ep.is_floor and not (
            excluded_scopes and (set(ep.scopes) & excluded_scopes)
        )

    refs = [EpisodeRef(ep.id, ep.session_id, ep.created) for ep in candidates.values()]
    res = caller.resolution
    newest = caller.model.handoff(res, refs)
    # The newest prior session that stood in this checkout and
    # journaled nothing (decision B22), and whether it is newer than
    # the newest session that journaled here: then it is the
    # immediately-prior session the note names.
    quiet: tuple[str, datetime] | None = None
    if res.checkout is not None:
        homes = caller.model.homes(list(candidates))
        here = [
            candidates[i].created
            for i, home in homes.items()
            if home.checkout == res.checkout
        ]
        own = {own_session}
        own_harness = identity.current_harness_session()
        if own_harness is not None:
            own.add(own_harness)
        quiet = _quiet_session(
            deps.store,
            checkout=res.checkout,
            exclude=own,
            journaled={ep.session_id for ep in candidates.values()},
            episodes_here=here,
        )
    newest_at = max((candidates[i].created for i in newest.episodes), default=None)
    quiet_first = quiet is not None and (newest_at is None or quiet[1] > newest_at)
    if newest.rule == "own_checkout" and newest.session_id is not None:
        adopted = caller.model.handoff(
            res, [r for r in refs if visible_real(candidates[r.id])]
        )
        prior = [candidates[i] for i in newest.episodes]
        if adopted.rule == "own_checkout" and adopted.session_id is not None:
            # Rewound past newer sessions with nothing visible, or the
            # immediately-prior session itself.
            pick.session_id = adopted.session_id
            pick.chosen = set(adopted.episodes)
            if quiet_first:
                pick.note_zero_episode = True
            elif adopted.session_id != newest.session_id and all(
                ep.is_floor for ep in prior
            ):
                pick.note_floor_only = True
        elif quiet is not None and quiet_first:
            # Nothing visible anywhere, and the immediately-prior
            # session journaled nothing: surface it with its note.
            pick.session_id = quiet[0]
            pick.chosen = set()
            pick.note_zero_episode = True
        else:
            # Nothing visible anywhere: surface the immediately-prior
            # session with the note naming why it shows nothing.
            pick.session_id = newest.session_id
            pick.chosen = set(newest.episodes)
            if all(ep.is_floor for ep in prior):
                pick.note_floor_only = True
            else:
                pick.note_all_hidden = True
    elif newest.session_id is not None:
        # A primary checkout with no episode of its own: the newest
        # session of a non-live primary checkout of its project.
        pick.session_id = newest.session_id
        pick.chosen = set(newest.episodes)
        pick.inherited_from = newest.label
    elif quiet is not None:
        # No session journaled in this checkout and none is inherited;
        # the newest that stood here journaled nothing (decision B22).
        pick.session_id = quiet[0]
        pick.chosen = set()
        pick.note_zero_episode = True
    return pick


def _emit_episodes(
    deps: ToolHandlers,
    pick: _HandoffPick,
    *,
    prior_session_id: str | None,
    excluded_scopes: set[str],
    max_episodes: int,
    include_bodies: bool,
) -> list[dict[str, Any]]:
    """Take the pick; return the rows the handoff emits, setting its floor-only flag."""
    episodes: list[dict[str, Any]] = []
    resolved_session_id = pick.session_id
    chosen = pick.chosen
    # `note_floor_only` may already be True from the auto-resolution walk
    # in `_auto_pick` (the immediately-prior session of the checkout was
    # floor-only, whether or not we then rewound to an older real session).
    # The explicit-`prior_session_id` path bypasses that walk, so we derive
    # the floor-only determination for a named session below.
    if resolved_session_id is not None:
        # An explicit `prior_session_id` flows in verbatim (the
        # auto-resolution branch in `_auto_pick` only ever assigns a
        # session_id that already round-tripped through `list_by_session`).
        # A caller-supplied id — e.g. a child agent passed a mistyped or
        # path-shaped parent id — can fail `_session_dir`'s
        # `[A-Za-z0-9_-]` validator with a ValueError. On the /loop
        # iteration-entry hot path that should degrade to the graceful
        # `episodes: []` shape, not surface a raw ValueError, matching
        # how the auto-resolution walk (`_auto_pick`) and `episode_search`
        # handle an invalid session_id. The validator fails closed, so
        # this is not a traversal — just a loud-vs-quiet failure-mode
        # choice, and quiet is what every other episode read does.
        try:
            all_eps = deps.store.episodes_by_session(resolved_session_id)
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
                pick.note_floor_only = True
        # Filter floors from the emit stream — they carry no takeaway
        # and the marker body is a placeholder, not content the model
        # should reason over as "what the prior session concluded". The
        # auto path emits the adopted session's episodes of the checkout
        # `Membership.handoff` named.
        if chosen is not None:
            all_eps = [ep for ep in all_eps if ep.id in chosen]
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
        labels = deps.store.episode_provenance([ep.id for ep in recent])
        for ep in recent:
            label = labels.get(ep.id, UNACCOUNTED)
            # `body` is present only on request and never for an
            # unaccounted episode: the takeaway and the scopes travel
            # with the label beside them, the body stays on disk for the
            # explicit read (`episode_search(ids=[...])`). Omitted, not
            # emptied, so a caller branches on `"body" in row`.
            deliver_body = include_bodies and label != UNACCOUNTED
            episodes.append(
                {
                    "id": ep.id,
                    "created": ep.created.isoformat().replace("+00:00", "Z"),
                    "takeaway": ep.takeaway,
                    **({"body": ep.body.strip()} if deliver_body else {}),
                    "scopes": ep.scopes,
                    "provenance": label,
                }
            )
    return episodes


def _handoff_result(
    pick: _HandoffPick, episodes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Take the pick and its emitted rows; return the handoff's response with any note."""
    result: dict[str, Any] = {
        "prior_session_id": pick.session_id,
        "episodes": episodes,
    }
    if pick.inherited_from is not None:
        # Run-state taken from another checkout of the project (design
        # 3.6): said, never silent.
        result["inherited_from"] = pick.inherited_from
    # Additive surface key, only present when the immediately-prior
    # session left no takeaway. A caller that doesn't know about the field sees
    # the same shape as before. `episodes` may be NON-empty here: the walk
    # rewound past the empty session to an older real takeaway, and the note
    # flags that the most recent session left nothing. The empty shapes get
    # distinct text because their on-disk cause differs.
    if pick.note_floor_only:
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
            "the session-tag floor that anchored it in this checkout) "
            "but no episode_write followed — either it crashed before "
            "the takeaway, or it was a clean read-only tick with "
            "nothing to record. Any takeaways above (if present) come "
            "from an older session in this checkout that the handoff "
            "rewound to."
        )
    elif pick.note_zero_episode:
        # Zero-episode: no floor and no takeaway; the session is this
        # checkout's by its rows (decision B22), so the note claims no
        # floor and no episode_handoff call.
        result["note"] = (
            "The immediately-preceding session in this checkout recorded "
            "activity but journaled no takeaway and left no handoff floor: "
            "it may have been a tick that never called the handoff, or it "
            "crashed before journaling. Any takeaways above (if present) "
            "come from an older session in this checkout that the handoff "
            "rewound to."
        )
    elif pick.note_all_hidden:
        # Scope-hidden terminal shape: the immediately-prior session
        # HAS real takeaways, but every one of them is in a scope this
        # session disabled, and no older visible takeaway exists either. The
        # floor-only text would be a lie here (the
        # session journaled fine), and returning no note would be
        # indistinguishable from "prior session wrote nothing". `episodes` is
        # always [] on this branch — the emit-step scope filter hides the
        # same episodes the walk could not surface.
        result["note"] = (
            "The immediately-preceding session in this checkout recorded "
            "takeaways, but every one of them is in a scope this session "
            "has disabled (memory_admin disable_scope), so none can be shown. "
            "The prior session was not empty and this is not the first "
            "handoff in this checkout; re-enable the scope with "
            "memory_admin(action='enable_scope') to surface its takeaways."
        )
    return result


async def episode_handoff(
    deps: ToolHandlers,
    prior_session_id: str | None = None,
    max_episodes: int | None = None,
    include_bodies: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Handler body for the `episode_handoff` MCP tool.

    Auto-resolves the prior session by walking the event log when
    `prior_session_id` is None. Caps `max_episodes` at 50 to keep the
    response bounded; defaults to 5 to match the rest of the read
    surface (`default_max_results`). `include_bodies` (default False,
    also spelled out on the `_handlers.py` facade the served schema is
    built from) adds `body` to every row whose provenance is not
    `unaccounted`; see the module docstring for the rule.

    REWIND contract (episode-handoff-chain fix): the resolution does not
    stop at the most-recent session of the caller's checkout when that
    session is FLOOR-ONLY (an entry floor but no `episode_write`
    takeaway). A floor is written unconditionally at handoff ENTRY, so
    every clean read-only /loop tick leaves a floor-only session; adopting
    it would return `episodes: []`, severing the chain and hiding the real
    takeaways of the session before it. Instead the resolution REWINDS to
    the most-recent session of the checkout that carries a real (non-floor,
    not scope-hidden) takeaway and surfaces THAT. Independently, when the
    immediately-prior session was floor-only, an honest soft `note` is
    attached to the result. The note deliberately does NOT assert a
    crash: a floor-only session is byte-identical on disk whether the
    tick crashed after entry or was a clean read-only tick, so the note
    hedges both readings. When no older real session exists, the
    floor-only session itself is surfaced as `prior_session_id` with
    `episodes: []` plus the note.

    Auto-resolution keys run-state on the checkout (design 3.6): which
    episodes are this checkout's is `Membership.handoff`'s decision, read
    from each episode's home (the checkout it was written in). Two
    checkouts of one project share its memories but never each other's
    iteration state: a session of another checkout is offered only to a
    primary checkout with no episode of its own, and only when that
    checkout is not live here, labelled in `inherited_from`. A caller
    outside any checkout has no prior session. Asked twice, once over
    every episode of the other sessions and once over the visible real
    takeaways, `Membership.handoff` names the immediately-prior session
    (for the note) and the session the rewind adopts.
    A session of this checkout whose REAL episodes are all hidden by the
    caller's `disabled_scopes` is rewound past toward an older visible
    takeaway (silently: the transparent-rewind contract for
    scope_disable'd sessions), but when no visible takeaway exists it is
    surfaced as `prior_session_id` with `episodes: []` and a note naming
    the scope-hide cause, never the first-ever
    `{prior_session_id: None}` shape.
    A session that stood in this checkout and journaled nothing, newer
    than every session that journaled here, is the immediately-prior one
    (decision B22, `_quiet_session`): the zero-episode note names it, and
    with nothing visible to rewind to it is surfaced with `episodes: []`.
    An explicit `prior_session_id` is respected verbatim; the
    caller passing one in is explicit consent that they own the
    cross-checkout concern.
    """
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if max_episodes is None:
        max_episodes = 5
    max_episodes = max(1, min(int(max_episodes), 50))

    # The caller, resolved once (design 3.1): the floor is homed by it and
    # the handoff keys on its checkout.
    _anchoring, caller = caller_anchoring(deps.store, deps.config)
    _write_entry_floor(deps, caller)

    # Session-disabled scopes hide episodes uniformly across the read
    # surface, the same contract `memory_search` / `memory_list` honor. For
    # handoff the filter cascades into the session selection too: a
    # session whose episodes are ALL scope-suppressed behaves as if it
    # wrote nothing, so auto-resolution skips it and adopts the
    # next-most-recent session instead. An explicit `prior_session_id`
    # still respects the filter on the emit step.
    excluded_scopes: set[str] = set(state.disabled_scopes)

    pick = (
        _HandoffPick(session_id=prior_session_id)
        if prior_session_id is not None
        else _auto_pick(deps, caller, excluded_scopes)
    )
    episodes = _emit_episodes(
        deps,
        pick,
        prior_session_id=prior_session_id,
        excluded_scopes=excluded_scopes,
        max_episodes=max_episodes,
        include_bodies=include_bodies,
    )

    # The conversation this server session serves, when the request named
    # it: a later handoff reads it to know this conversation journaled
    # (decision B22, `_quiet_session`).
    harness = identity.current_harness_session()
    deps.recorder.record(
        "episode_handoff",
        prior_session_id=pick.session_id,
        max_episodes=max_episodes,
        include_bodies=include_bodies,
        returned=len(episodes),
        prior_crashed_pre_takeaway=pick.note_floor_only,
        **({HARNESS_SESSION_FIELD: harness} if harness is not None else {}),
    )
    return _handoff_result(pick, episodes)


def _quiet_session(
    store: Store,
    *,
    checkout: str,
    exclude: Collection[str],
    journaled: Collection[str],
    episodes_here: Iterable[datetime],
) -> tuple[str, datetime] | None:
    """The newest prior session that stood in `checkout` and journaled
    nothing, with the stamp of its last row there (decision B22); None
    when there is none.

    A session stood in the checkout when a `session_brief` or
    `prompt_recall` row it wrote names the checkout (rs-v5 rows carry
    it, design 8.11); a session with no such row is not this checkout's.
    Those rows are written under the conversation's own id, so a session
    journaled when it holds an episode (`journaled`: the sessions of the
    other sessions' episodes), when a journal call's row is stamped with
    it (`_JOURNAL_KINDS`, `events.HARNESS_SESSION_FIELD`), or when an
    episode of this checkout (`episodes_here`) was written between its
    first and last row there, which the log cannot tell from its own.
    `exclude` names the caller's own sessions."""
    spans: dict[str, tuple[datetime, datetime]] = {}
    for row in store.events_of_kinds(_CHECKOUT_ROW_KINDS, since=_EPOCH):
        if row.get("checkout") != checkout:
            continue
        session = row.get("session") or row.get("session_id")
        stamp = parse_event_ts(row.get("ts"))
        if not isinstance(session, str) or not session or stamp is None:
            continue
        if session in exclude:
            continue
        first, last = spans.get(session, (stamp, stamp))
        spans[session] = (min(first, stamp), max(last, stamp))
    if not spans:
        return None
    stamped = {
        row.get(HARNESS_SESSION_FIELD)
        for row in store.events_of_kinds(_JOURNAL_KINDS, since=_EPOCH)
    }
    written = sorted(episodes_here)
    quiet: list[tuple[datetime, str]] = []
    for session, (first, last) in spans.items():
        if session in journaled or session in stamped:
            continue
        if any(first <= created <= last for created in written):
            continue
        quiet.append((last, session))
    if not quiet:
        return None
    last, session = max(quiet)
    return session, last


def _maybe_write_session_floor(
    deps: ToolHandlers,
    handoff_origin: Any,
    caller: Caller,
) -> None:
    """Write a session-tag floor episode for the current session, if needed,
    homed to the caller's checkout (`Caller.home`, design 3.6) in the
    record's own transaction.

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
    session_id = deps.recorder.session_for_request()
    # Cheap existence check. `list_by_session` returns oldest-first
    # episodes; we only need to know whether any exists.
    try:
        existing = deps.store.episodes_by_session(session_id)
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
    with deps.store.batch():
        deps.store.write_floor(
            session_id=session_id,
            origin=handoff_origin,
            home=caller.home(),
        )


__all__ = ["DESC_EPISODE_HANDOFF", "episode_handoff"]
