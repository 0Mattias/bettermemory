"""The recall surface's per-session state and the vocabulary its rows share.

`RecallState` holds one cooldown per (harness, session id): the segment
it is in, the ids shown and settled in it, and a compact signal still
pending. `rebuild_session` computes the same state from the session's
rows in the log through the transitions both share (`_restart`,
`_reopen`, `_hold_signal`, `_replay`). A subagent of the session has a
scope of its own (`Subagent`, ENG-150): its rows carry its ``agent_id``,
a state scoped to it reads only those, and the session's reads none of
them but the row that takes a held signal back from it (`_withdraw`).
The vocabulary the recall
modules read lives here too: the rule, the default harness, the
delivered provenance labels, the summary length, the state's bounds
and kinds, and the surfaces a settlement names. `recall` re-exports
every name here a caller imports from it. This module lives apart from
`recall.py` to keep each module under the package's 2,000-line bound
(ENG-96); it imports no recall module, and `recall` imports it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .events import _event_id_items
from .store import IMPORTED, LOCAL, Store
from .time_utils import parse_event_ts
from .transcript import CompactMarker


# The delivery rule's name, recorded on both events so a later rule can
# be told apart in the log. "rs-v2" (RS-F1, 2026-10-17) is "rs-v1" with
# the synthetic-prompt gate: a prompt the harness composed is not probed.
# "rs-v3" (RS-F2 and RS-F3, 2026-10-17) is "rs-v2" with the brief's
# omission on a resume (RS-F2) and the Stop row's prompt_id and
# reply_source (RS-F3).
# rs-v4: RS-F5's one owner-prompt definition, the Stop anchored to its turn, compact on the marker
# rs-v5: RS-F6's delivery decided by membership; the brief and recall rows carry the checkout, the membership rule and rc3_admitted
# Every row written after a landing carries its rule, so a read of the
# log splits on the row's rule and never on a clock.
RULE = "rs-v6"

# The harness a hook payload names when it names none: the one whose
# hook protocol the plugin implements.
DEFAULT_HARNESS = "claude-code"

# The provenance labels either surface delivers: records that entered
# through this store's own paths. A synced record carries a stamp this
# host may never have made and an unaccounted one a pointer that fails;
# neither is a pointer worth handing a model unasked.
_DELIVERED_PROVENANCE: frozenset[str] = frozenset({LOCAL, IMPORTED})

# The one summary length both surfaces print: the body, whitespace
# collapsed, cut with an ellipsis.
SUMMARY_CHARS = 80

# -- the session cooldown -----------------------------------------------------

STATE_IDLE_SECONDS = 24 * 3600
STATE_MAX_SESSIONS = 1_000

# The event kinds a session's state is rebuilt from: the two show
# records, the Stop audit's row, whose first stamp under a session id is
# the earliest moment that session is known to have run, and the two
# rows the Stop records about what was shown: a
# pointer's settlement (a `use` carrying a `surface`) and the
# unanswered-review count. A compact or clear whose brief showed nothing
# leaves a `segment_opened` row, as does the later read that confirms a
# compaction (with `opened_at`), so the segment, and a compact's pending
# signal, are read back too.
_STATE_KINDS: tuple[str, ...] = (
    "session_brief",
    "prompt_recall",
    "turn_audited",
    "use",
    "review_unanswered",
    "segment_opened",
)

# The surface a settled pointer was shown by, named on its `use` event.
SURFACE_BRIEF = "brief"
SURFACE_RECALL = "recall"
_SURFACE_OF_KIND: dict[str, str] = {
    "session_brief": SURFACE_BRIEF,
    "prompt_recall": SURFACE_RECALL,
}
_SETTLEMENT_SURFACES: frozenset[str] = frozenset(_SURFACE_OF_KIND.values())


def is_settlement(event: Mapping[str, Any]) -> bool:
    """True for a shown pointer's settlement: a `use` row naming the
    surface that showed it (`settle_shown`). The eval's trio and tool
    count leave exactly these out, and its surfaces section counts
    exactly these, so the two never disagree on a row."""
    return event.get("kind") == "use" and event.get("surface") in _SETTLEMENT_SURFACES


#: The event kind the Stop records when reviews in the session went
#: unanswered (`review_unanswered`).
REVIEW_UNANSWERED = "review_unanswered"


def _str_ids(value: Any) -> list[str]:
    return [memory_id for _, memory_id in _event_id_items(value)]


# -- a subagent's scope -------------------------------------------------------

#: The row field naming the subagent a hook row belongs to (Claude Code's
#: ``agent_id``); a row without it is the session's own.
AGENT_FIELD = "agent_id"
#: Where a row's subagent identity was read (`Subagent.found_by`): the hook
#: payload's ``agent_id``, or the subagent transcript that holds the
#: compaction's marker (``<dir>/<session>/subagents/agent-<agent_id>.jsonl``).
AGENT_FROM_PAYLOAD = "payload"
AGENT_FROM_TRANSCRIPT = "transcript"
#: The field of the `segment_opened` row that takes a held compact signal
#: back from the session for a subagent: the ids the compact's brief
#: printed, which leave the session's segment (none when it showed none).
WITHDRAWN_FIELD = "withdrawn"


@dataclass(frozen=True)
class Subagent:
    """A subagent of a session, as a hook row names it (ENG-150).

    Claude Code raises a subagent's compaction as a SessionStart under the
    parent's session id and main transcript path; `agent_id` is the id
    Claude Code gives the subagent (its transcript's name after
    ``agent-``), `agent_type` its agent name when known, `found_by` where
    the identity was read. Its rows are recorded under the session id
    with `fields`, and its shows and segments are its own scope's."""

    agent_id: str
    agent_type: str | None = None
    found_by: str = AGENT_FROM_PAYLOAD

    @classmethod
    def from_payload(
        cls, agent_id: str | None, agent_type: str | None = None
    ) -> Subagent | None:
        """The subagent a hook payload names, or None: an ``agent_type``
        alone names the main thread's agent (``claude --agent``), not a
        subagent."""
        if not agent_id:
            return None
        return cls(agent_id=agent_id, agent_type=agent_type or None)

    def fields(self) -> dict[str, str]:
        """What every row recorded for the subagent carries."""
        out = {AGENT_FIELD: self.agent_id, "agent_from": self.found_by}
        if self.agent_type:
            out["agent_type"] = self.agent_type
        return out


def agent_of(event: Mapping[str, Any]) -> str | None:
    """The subagent a row belongs to (its ``agent_id``), or None for the
    session's own row."""
    value = event.get(AGENT_FIELD)
    return value if isinstance(value, str) and value else None


def taken_back(event: Mapping[str, Any]) -> bool:
    """True for the row that took a held compact signal back from the
    session for a subagent (`RecallState.attribute_compaction`): a
    subagent's `segment_opened` carrying ``withdrawn``."""
    return (
        event.get("kind") == "segment_opened"
        and agent_of(event) is not None
        and isinstance(event.get(WITHDRAWN_FIELD), list)
    )


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Signal:
    """A compact SessionStart whose marker the transcript did not hold
    yet: when it came, and what the segment had shown and delivered by
    then. When a later read finds the marker, the segment it opens holds
    what was shown and delivered since (`_reopen`). `event_id` is the
    event of the brief the SessionStart showed, if any: when the
    compaction turns out to be a subagent's, its ids leave the segment
    (`_withdraw`)."""

    at: datetime
    shown: frozenset[str]
    deliveries: int
    event_id: str | None = None


@dataclass
class _SessionRecall:
    """One (harness, session id)'s cooldown: the segment it is in, when
    that segment opened, the ids shown in it and how many recalls it has
    delivered, and when the session was last seen.

    For the Stop: `shown_by` names, per id shown in the segment, the
    surface that showed it and that show's event id; `settled` holds the
    ids whose show the Stop has already settled in the segment (at most
    one settlement per id per segment); `review_roots` the review chain
    roots already counted unanswered, for the whole session.

    For compaction: `marker` is the compaction marker the session's
    segments last opened on, and `signal` a compact SessionStart not yet
    resolved against the transcript."""

    segment: int
    started_at: datetime
    shown: set[str]
    deliveries: int
    last_seen: datetime
    shown_by: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    settled: set[str] = field(default_factory=set)
    review_roots: set[str] = field(default_factory=set)
    marker: str | None = None
    signal: _Signal | None = None
    #: The segment the session last left (`_closing_view`), set and taken
    #: within one hook call: `open_segment` or `resolve_compaction` sets it
    #: under the lock and `settle_unused`, in the same call, takes it
    #: (`RecallState.take_closed`) to settle its unused pointers. A Stop
    #: never reads it; with telemetry off it holds an empty view until the
    #: next open replaces it.
    closed: SegmentView | None = None


@dataclass(frozen=True)
class SegmentView:
    """A snapshot of a session's current segment, safe to read outside
    the state's lock."""

    segment: int
    started_at: datetime
    shown: frozenset[str]
    deliveries: int
    shown_by: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)
    settled: frozenset[str] = frozenset()
    review_roots: frozenset[str] = frozenset()
    marker: str | None = None


def _view(entry: _SessionRecall) -> SegmentView:
    return SegmentView(
        segment=entry.segment,
        started_at=entry.started_at,
        shown=frozenset(entry.shown),
        deliveries=entry.deliveries,
        shown_by=dict(entry.shown_by),
        settled=frozenset(entry.settled),
        review_roots=frozenset(entry.review_roots),
        marker=entry.marker,
    )


# The transitions a session's cooldown takes, shared by the held state and
# its rebuild from the log, so the two cannot disagree on a segment.


def _closing_view(
    entry: _SessionRecall, keep: Collection[str] | None = None
) -> SegmentView:
    """The segment `entry` is about to leave, as its settlement reads it:
    everything it holds, or, when a compaction opens the next segment at
    an earlier signal (`_reopen`), only the ids shown before the signal
    (`keep`), the rest being the next segment's."""
    shown = set(entry.shown) if keep is None else set(entry.shown) & set(keep)
    return SegmentView(
        segment=entry.segment,
        started_at=entry.started_at,
        shown=frozenset(shown),
        deliveries=entry.deliveries,
        shown_by={k: v for k, v in entry.shown_by.items() if k in shown},
        settled=frozenset(entry.settled & shown),
        review_roots=frozenset(entry.review_roots),
        marker=entry.marker,
    )


def _restart(
    entry: _SessionRecall, *, segment: int, at: datetime, marker: str | None
) -> None:
    """Open `segment` at `at`, on `marker`: nothing shown, settled or
    delivered in it yet, and no compact signal pending."""
    entry.segment = segment
    entry.started_at = at
    entry.marker = marker
    entry.signal = None
    entry.shown = set()
    entry.shown_by = {}
    entry.settled = set()
    entry.deliveries = 0


def _reopen(
    entry: _SessionRecall,
    *,
    signal: _Signal,
    segment: int,
    at: datetime,
    marker: str | None,
) -> None:
    """Open `segment` at `at`, the moment of the compact `signal`, on
    `marker`: the ids shown and the recalls delivered since the signal
    are the new segment's, everything before it the old one's."""
    shown = entry.shown - signal.shown
    entry.segment = segment
    entry.started_at = at
    entry.marker = marker
    entry.signal = None
    entry.shown = shown
    entry.shown_by = {k: v for k, v in entry.shown_by.items() if k in shown}
    entry.settled &= shown
    entry.deliveries -= signal.deliveries


def _hold_signal(
    entry: _SessionRecall, *, at: datetime, event_id: str | None = None
) -> None:
    """Hold a compact SessionStart whose marker is not read yet, and the
    event of the brief it showed (`event_id`)."""
    entry.signal = _Signal(
        at=at,
        shown=frozenset(entry.shown),
        deliveries=entry.deliveries,
        event_id=event_id,
    )


def _withdraw(entry: _SessionRecall, ids: Iterable[str]) -> None:
    """The held compact signal was a subagent's (ENG-150): it ends, and
    `ids`, what that compact's brief printed into the subagent, leave the
    segment's shown and settled sets, as the session was never shown
    them."""
    gone = set(ids)
    entry.signal = None
    entry.shown -= gone
    entry.shown_by = {k: v for k, v in entry.shown_by.items() if k not in gone}
    entry.settled -= gone


def _replay(entry: _SessionRecall, event: Mapping[str, Any], stamp: datetime) -> None:
    """Apply one of the session's rows (other than a `review_unanswered`)
    to its rebuilt cooldown, in log order, by the transitions the held
    state takes.

    A row naming another segment opened it. One carrying ``opened_at`` (a
    compaction whose marker a later read found) opened it at that moment,
    with what was shown since the pending signal (`_reopen`); any other
    opened it at the row, with nothing shown (`_restart`). A compact row
    carrying ``marker_absent`` holds the signal (`_hold_signal`), a brief
    row naming its event. Then the row's shows or settlement join the
    segment; a `turn_audited` row has neither, nor has a `segment_opened`
    row but a subagent's that took a signal back from the session, whose
    ``withdrawn`` ids are the subagent's segment's shows (ENG-150)."""
    number = event.get("segment")
    if (
        isinstance(number, int)
        and not isinstance(number, bool)
        and number != entry.segment
    ):
        named = event.get("marker")
        marker = named if isinstance(named, str) else entry.marker
        opened_at = parse_event_ts(event.get("opened_at"))
        if opened_at is not None and entry.signal is not None:
            _reopen(
                entry,
                signal=entry.signal,
                segment=number,
                at=opened_at,
                marker=marker,
            )
        else:
            _restart(entry, segment=number, at=opened_at or stamp, marker=marker)
    kind = event.get("kind")
    shown_event_id = event.get("event_id")
    shown_event = shown_event_id if isinstance(shown_event_id, str) else None
    if event.get("marker_absent") is True:
        _hold_signal(
            entry, at=stamp, event_id=shown_event if kind == "session_brief" else None
        )
    if kind == "segment_opened":
        brief_event = event.get("brief_event_id")
        for memory_id in _str_ids(event.get(WITHDRAWN_FIELD)):
            entry.shown.add(memory_id)
            entry.shown_by.setdefault(
                memory_id,
                (SURFACE_BRIEF, brief_event if isinstance(brief_event, str) else None),
            )
        return
    if kind == "turn_audited":
        return
    if kind == "use":
        entry.settled.update(_str_ids(event.get("ids")))
        return
    surface = _SURFACE_OF_KIND.get(str(kind), SURFACE_RECALL)
    if kind == "session_brief":
        brief_ids = _str_ids(event.get("ids"))
        entry.shown.update(brief_ids)
        for memory_id in brief_ids:
            entry.shown_by.setdefault(memory_id, (surface, shown_event))
        return
    if "shown_ids" in event:
        delivered = _str_ids(event.get("shown_ids"))
    else:
        hits = event.get("top_hits")
        first = hits[0] if isinstance(hits, list) and hits else None
        delivered = (
            [first["id"]]
            if isinstance(first, dict) and isinstance(first.get("id"), str)
            else []
        )
    entry.shown.update(delivered)
    for memory_id in delivered:
        entry.shown_by.setdefault(memory_id, (surface, shown_event))
    entry.deliveries += 1


def rebuild_session(
    store: Store,
    key: tuple[str, str],
    *,
    now: datetime,
    agent_id: str | None = None,
) -> _SessionRecall:
    """A session's cooldown computed from the log: what a restarted
    daemon, the in-process CLI path, or the daemon after a day idle knows
    about it.

    Reads every one of the session's `session_brief`, `prompt_recall`,
    `turn_audited`, `use`, `review_unanswered` and `segment_opened` rows,
    by session and through the kind index, whenever they were stamped, so
    a rebuild agrees with the state that was held. Each show record
    carries the segment it was counted in, so the walk adopts the newest
    segment number and keeps only that segment's shown ids and delivery
    count (`_replay`). A `prompt_recall` written before the recall
    surface carries no `shown_ids`; its one delivered hit is the first of
    `top_hits`. A session the log does not know starts a segment now.

    The Stop's own rows are read too: a `use` with a `surface` is a
    settlement, and carries its segment like a show does; a
    `review_unanswered` names roots counted for the whole session; a
    `turn_audited` stamps the session's start. Any other `use` under the
    session (the hook's attribution of a real retrieval) is not the
    recall's and is skipped before it can stamp the session's start.

    The scope is the session's own, or with `agent_id` that subagent's
    (ENG-150): a row carrying an ``agent_id`` is read only by the scope of
    that subagent, and a row carrying none only by the session's, but
    one: the `segment_opened` row of a held compact signal a later read
    found to be a subagent's (it carries ``withdrawn``) ends the session's
    signal and takes those ids out of the session's segment
    (`_withdraw`), as the read did to the held state."""
    harness, session_id = key
    entry: _SessionRecall | None = None
    review_roots: set[str] = set()
    for event in store.session_events_of_kinds(session_id, _STATE_KINDS):
        named = event.get("harness")
        if isinstance(named, str) and named != harness:
            continue
        if agent_of(event) != agent_id:
            if agent_id is None and entry is not None and taken_back(event):
                _withdraw(entry, _str_ids(event.get(WITHDRAWN_FIELD)))
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None:
            continue
        kind = event.get("kind")
        if kind == "use" and not is_settlement(event):
            continue
        if kind == REVIEW_UNANSWERED:
            review_roots.update(_str_ids(event.get("root_ids")))
            continue
        if entry is None:
            entry = _SessionRecall(0, stamp, set(), 0, now)
        _replay(entry, event, stamp)
    if entry is None:
        entry = _SessionRecall(0, now, set(), 0, now)
    entry.review_roots = review_roots
    return entry


class RecallState:
    """The recall surface's process-wide state, lock-guarded.

    One table: one `_SessionRecall` per (harness, session id), least
    recently seen first, evicted after `idle_seconds` without a hook and
    beyond `max_sessions`; a session not held is rebuilt from the log
    (`rebuild_session`) on first sight, so an evicted session comes back
    as it was.

    The daemon holds one for its lifetime and calls it from worker
    threads. The in-process CLI path builds a fresh one per call and holds
    nothing between calls. The store each call passes is the caller's own
    connection; the state holds none.

    A state built with `agent_id` is that subagent's scope of each session
    it is asked about (ENG-150): rebuilt from the session's rows carrying
    the subagent's id and nothing else. A subagent raises no hook but its
    SessionStart, so nothing holds one: the SessionStart that names a
    subagent builds its scope fresh, as the CLI path builds a session's."""

    def __init__(
        self,
        *,
        max_sessions: int = STATE_MAX_SESSIONS,
        idle_seconds: int = STATE_IDLE_SECONDS,
        agent_id: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[tuple[str, str], _SessionRecall] = OrderedDict()
        self._max_sessions = max_sessions
        self._idle_seconds = idle_seconds
        self._agent_id = agent_id

    # -- sessions -------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _sweep(self, now: datetime) -> None:
        """Drop sessions idle past the window (lock held). The table is in
        last-seen order, so the walk stops at the first one still live."""
        cutoff = now - timedelta(seconds=self._idle_seconds)
        while self._sessions:
            key, entry = next(iter(self._sessions.items()))
            if entry.last_seen >= cutoff:
                break
            del self._sessions[key]

    def _entry(self, store: Store, key: tuple[str, str], now: datetime) -> None:
        """Make sure `key` is held (rebuilding it from the log outside the
        lock when it is not), mark it seen and move it to the back."""
        with self._lock:
            self._sweep(now)
            held = self._sessions.get(key)
            if held is not None:
                held.last_seen = now
                self._sessions.move_to_end(key)
                return
        rebuilt = rebuild_session(store, key, now=now, agent_id=self._agent_id)
        with self._lock:
            held = self._sessions.get(key)
            if held is None:
                self._sessions[key] = rebuilt
            else:
                held.last_seen = now
            self._sessions.move_to_end(key)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)

    def segment(
        self, store: Store, key: tuple[str, str], *, now: datetime
    ) -> SegmentView:
        """The session's current segment."""
        self._entry(store, key, now)
        with self._lock:
            return _view(self._held(key, now))

    def open_segment(
        self,
        store: Store,
        key: tuple[str, str],
        *,
        source: str,
        now: datetime,
        marker: CompactMarker | None = None,
        event_id: str | None = None,
    ) -> tuple[SegmentView, bool]:
        """A SessionStart: the segment it leaves the session in, and
        whether it opened that segment. `event_id` names the brief the
        SessionStart shows, held with a compact's signal.

        A ``clear`` opens a new segment (nothing shown, no deliveries).
        A ``compact`` opens one when `marker`, the compaction marker the
        transcript holds near `now`, is not the one the session's
        segments last opened on; without such a marker it keeps the
        segment, as a resume does, and holds the signal for a later read
        of the transcript (`resolve_compaction`). ``startup`` and
        ``resume`` keep the segment."""
        self._entry(store, key, now)
        with self._lock:
            entry = self._held(key, now)
            if source == "clear":
                entry.closed = _closing_view(entry)
                _restart(entry, segment=entry.segment + 1, at=now, marker=entry.marker)
                return _view(entry), True
            if source != "compact":
                return _view(entry), False
            if marker is not None and marker.key != entry.marker:
                entry.closed = _closing_view(entry)
                _restart(entry, segment=entry.segment + 1, at=now, marker=marker.key)
                return _view(entry), True
            _hold_signal(entry, at=now, event_id=event_id)
            return _view(entry), False

    def pending_compaction(
        self, store: Store, key: tuple[str, str], *, now: datetime
    ) -> datetime | None:
        """When the session's unresolved compact SessionStart came, or
        None when it holds none. A signal ends only on a row the log
        holds: its confirmation, a clear, another compact, or the
        `segment_opened` row that takes it back for a subagent
        (`attribute_compaction`)."""
        self._entry(store, key, now)
        with self._lock:
            signal = self._held(key, now).signal
            return None if signal is None else signal.at

    def resolve_compaction(
        self,
        key: tuple[str, str],
        *,
        at: datetime,
        marker: CompactMarker | None,
    ) -> SegmentView | None:
        """Resolve the compact signal held since `at` against `marker`, the
        marker the transcript holds near `at`: the segment it opens when
        the marker is not the one the segments last opened on (returned).
        Without such a marker the signal is kept for a later read; None
        then, and when the signal was resolved meanwhile."""
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None or entry.signal is None or entry.signal.at != at:
                return None
            if marker is None or marker.key == entry.marker:
                return None
            entry.closed = _closing_view(entry, keep=entry.signal.shown)
            _reopen(
                entry,
                signal=entry.signal,
                segment=entry.segment + 1,
                at=at,
                marker=marker.key,
            )
            return _view(entry)

    def attribute_compaction(
        self, key: tuple[str, str], *, at: datetime
    ) -> tuple[list[str], str | None] | None:
        """The compact signal held since `at` was a subagent's (ENG-150):
        end it and take the ids its SessionStart's brief printed out of
        the segment (`_withdraw`); those ids, sorted, and that brief's
        event id. None when the signal was resolved meanwhile."""
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None or entry.signal is None or entry.signal.at != at:
                return None
            signal = entry.signal
            shown_by = (SURFACE_BRIEF, signal.event_id)
            ids = (
                sorted(k for k, v in entry.shown_by.items() if v == shown_by)
                if signal.event_id is not None
                else []
            )
            _withdraw(entry, ids)
            return ids, signal.event_id

    def take_closed(self, key: tuple[str, str]) -> SegmentView | None:
        """The segment the session last left, once: what `open_segment` or
        `resolve_compaction` closed and no hook has settled yet
        (`settle_unused`); None when nothing waits."""
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None or entry.closed is None:
                return None
            closed, entry.closed = entry.closed, None
            return closed

    def _held(self, key: tuple[str, str], now: datetime) -> _SessionRecall:
        """The session's entry, created empty when it is not held (lock
        held), marked seen and moved to the back."""
        entry = self._sessions.get(key)
        if entry is None:
            entry = _SessionRecall(0, now, set(), 0, now)
            self._sessions[key] = entry
        entry.last_seen = now
        self._sessions.move_to_end(key)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return entry

    def note_shown(
        self,
        key: tuple[str, str],
        ids: Iterable[str],
        *,
        delivery: bool,
        now: datetime,
        surface: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """Record what a surface just showed in the session's segment; a
        prompt recall also counts as a delivery. `surface` and `event_id`
        name the show the Stop settles each id against."""
        with self._lock:
            entry = self._held(key, now)
            shown = list(ids)
            entry.shown.update(shown)
            named = surface or (SURFACE_RECALL if delivery else SURFACE_BRIEF)
            for memory_id in shown:
                entry.shown_by.setdefault(memory_id, (named, event_id))
            if delivery:
                entry.deliveries += 1

    def claim_delivery(
        self,
        key: tuple[str, str],
        ids: Sequence[str],
        *,
        now: datetime,
        event_id: str | None = None,
    ) -> tuple[list[str], int]:
        """Check and note one prompt recall's delivery under one lock: the
        ids not yet shown in the session's segment, in order, and the
        segment they were counted in. What the caller read before
        (`segment`) may be stale by the time it delivers when two prompts
        of one session run at once; this is the step that decides, so no
        id is shown twice in a segment."""
        with self._lock:
            entry = self._held(key, now)
            kept = [memory_id for memory_id in ids if memory_id not in entry.shown]
            if not kept:
                return [], entry.segment
            entry.shown.update(kept)
            for memory_id in kept:
                entry.shown_by.setdefault(memory_id, (SURFACE_RECALL, event_id))
            entry.deliveries += 1
            return kept, entry.segment

    def claim_brief(
        self,
        key: tuple[str, str],
        standing: Sequence[str],
        work: Sequence[str],
        *,
        now: datetime,
        event_id: str | None = None,
    ) -> tuple[list[str], list[str], int]:
        """Check and note one brief's show under one lock, as
        `claim_delivery` does a prompt recall's: each list's ids not yet
        shown in the session's segment, in order, noted as shown by the
        brief (`shown_by`, for the Stop), and the segment they were
        counted in. The brief read its segment (`segment`) before it
        selected, and a prompt recall of the same session may have shown
        some of these ids since, or a compaction opened the next segment;
        this is the step that decides, so no id is shown twice in a
        segment by either surface and the row names the segment the ids
        were claimed in. A brief counts as no delivery."""
        with self._lock:
            entry = self._held(key, now)
            kept_standing = [m for m in standing if m not in entry.shown]
            kept_work = [m for m in work if m not in entry.shown]
            for memory_id in [*kept_standing, *kept_work]:
                entry.shown.add(memory_id)
                entry.shown_by.setdefault(memory_id, (SURFACE_BRIEF, event_id))
            return kept_standing, kept_work, entry.segment

    def note_settled(
        self, key: tuple[str, str], ids: Iterable[str], *, now: datetime
    ) -> None:
        """Record ids whose show the Stop settled in the session's
        segment; none of them is settled again in it."""
        with self._lock:
            self._held(key, now).settled.update(ids)

    def note_review_roots(
        self, key: tuple[str, str], roots: Iterable[str], *, now: datetime
    ) -> None:
        """Record review chain roots counted unanswered in the session;
        none of them is counted again in it."""
        with self._lock:
            self._held(key, now).review_roots.update(roots)
