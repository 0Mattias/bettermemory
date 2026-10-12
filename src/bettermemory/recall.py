"""The recall surface: what bettermemory puts in front of a model without
a tool call, on two hook moments.

- The session brief (`session_brief`), on SessionStart inside a
  repository: at most eight pointers to the newest memories written in
  that repository whose staleness verdict is ``fresh`` exactly as
  `memory_show` computes it (the trust rule included), each an id and an
  80-character summary under a header that says they are pointers, not
  instructions. No body, no scope list, nothing outside a repository,
  nothing for an empty selection.
- The prompt recall (`prompt_recall`), on UserPromptSubmit: the prompt,
  pasted blocks removed and cut at 2,000 characters, probed through the
  audit's own probe (`hook._probe_message`: the production pool and
  ranker, top three), and up to three of those hits delivered as
  pointers when they are relevant (medium or high), written or imported
  here, in the caller's project (under the default `prompt_recall =
  "project"`), not contradicted, and not already shown, retrieved,
  written or updated in this session segment.

Both are pointers by construction. The read path's staleness machinery
lives on `memory_show`; a body delivered around it would be the one read
surface that skips verification, so the model is told to fetch, and the
fetch carries the signals.

Per-session state (`RecallState`) replaces the old 600-second
any-retrieval shield for delivery: a session segment (a SessionStart
with source ``compact`` or ``clear`` opens a new one) shows no id twice
and delivers at most twelve recalls. The state lives in the daemon,
lock-guarded, rebuilt from the log's events after a restart, evicted
after a day idle and bounded at a thousand sessions; the in-process CLI
path, which has no daemon, computes it from the log on every call. The
Stop audit keeps its own shield unchanged: a delivered `prompt_recall`
is still a retrieval kind there, so the turn it served is not a silent
miss.

Telemetry is the gate for both: each show is recorded (`session_brief`,
`prompt_recall`) at show time, and with `telemetry.enabled = false`
nothing is shown, since an unlogged injection could be neither measured
nor seen by the audit.

The brief's verdicts come from `verdict.py`. A warm call (the
repository's HEAD unchanged since the last brief for that repository and
directory in this process) reads `memoised_verdicts` and runs no git
process while the memo answers; when rows it cannot answer would leave
the brief short of eight, the first 24 of them are resolved on the
request path and the rest handed to a background fill (one in-flight
fill per repository and directory, `fill_verdicts` on a worker
thread, which stores row by row so one disturbance in the repository
loses one row and not the fill). A cold call resolves the 24 newest
eligible rows synchronously and hands the rest to the fill. The
in-process CLI path has no background: its process exits after
printing, so it resolves its 24 and stops.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import identity
from ._response import ResponseBuilder, isoformat_optional
from .audit import REAUDIT_DEDUP_WINDOW_SECONDS, MissHit, prompt_recall_fields
from .config import Config, load_config
from .events import HARNESS_SESSION_FIELD, Recorder, _event_id_items
from .models import Category, Memory, generate_ulid, utcnow
from .origin import (
    Origin,
    repos_match,
    toplevel_and_head_from_files,
    worktrees_match,
)
from .store import IMPORTED, LOCAL, STORE_FILENAME, Store, TrustRow
from .time_utils import ensure_utc, parse_event_ts
from .verdict import (
    FillReport,
    MemoryVerdict,
    UnresolvedVerdict,
    fill_verdicts,
    memoised_verdicts,
    resolve_verdicts,
)

log = logging.getLogger("bettermemory.recall")

__all__ = [
    "BRIEF_CAP_CHARS",
    "BRIEF_MAX_POINTERS",
    "BRIEF_SYNC_RESOLVE",
    "DEFAULT_HARNESS",
    "HookResult",
    "PROMPT_CUT_CHARS",
    "RECALL_CAP_CHARS",
    "RECALL_MAX_POINTERS",
    "REVIEW_UNANSWERED",
    "RULE",
    "RecallPointer",
    "RecallState",
    "SEGMENT_DELIVERY_CAP",
    "SURFACE_BRIEF",
    "SURFACE_RECALL",
    "brief_key",
    "clean_prompt",
    "disputed_marks",
    "in_project",
    "is_settlement",
    "previous_audit_at",
    "prompt_recall",
    "render_brief",
    "render_recall_block",
    "review_unanswered",
    "session_brief",
    "settle_shown",
    "summarize",
]


# The delivery rule's name, recorded on both events so a later rule can
# be told apart in the log.
RULE = "rs-v1"

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

# -- the brief ----------------------------------------------------------------

BRIEF_MAX_POINTERS = 8
# Rows a cold brief resolves on the request path; the rest go to the
# background fill.
BRIEF_SYNC_RESOLVE = 24
BRIEF_CAP_CHARS = 1_200
# The newest eligible rows a brief considers at all. A repository with
# thousands of memories would otherwise hand the fill (and a warm call's
# walk) the whole history; eight fresh pointers are found well inside
# this window on any store that has them.
BRIEF_WINDOW = 200
_BRIEF_CHUNK = 24

# SessionStart sources (Claude Code's `source` field). Anything else
# reads as a startup.
_SESSION_SOURCES: frozenset[str] = frozenset({"startup", "resume", "compact", "clear"})
_NEW_SEGMENT_SOURCES: frozenset[str] = frozenset({"compact", "clear"})

# -- the prompt recall --------------------------------------------------------

PROMPT_CUT_CHARS = 2_000
RECALL_MAX_POINTERS = 3
RECALL_CAP_CHARS = 900
SNIPPET_CHARS = 100
# A snippet cut shorter than this to fit the cap is dropped instead: a
# dozen characters of matched text tells the model nothing.
_MIN_SNIPPET_CHARS = 12
_RELEVANT: frozenset[str] = frozenset({"medium", "high"})

# -- the session cooldown -----------------------------------------------------

SEGMENT_DELIVERY_CAP = 12
STATE_IDLE_SECONDS = 24 * 3600
STATE_MAX_SESSIONS = 1_000
# The most brief keys (a repository and the directory the caller stands
# in) whose HEAD and fill the state remembers, least recently used out.
STATE_MAX_BRIEF_KEYS = 256

# The event kinds a session's state is rebuilt from: the two show
# records, the Stop audit's row, whose first stamp under a session id is
# the earliest moment that session is known to have run, and the two
# rows the Stop records about what was shown: a pointer's settlement (a
# `use` carrying a `surface`) and the unanswered-review count. A compact
# or clear whose brief showed nothing leaves a `segment_opened` row, so
# the segment it opened is read back too.
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
# The kinds whose ids count as retrieved, written or updated in a segment.
_SEEN_KINDS: frozenset[str] = frozenset(
    {"search", "show", "list", "list_active", "write", "update"}
)

_PASTED_CONTENT = re.compile(
    r"<pasted_content\b[^>]*?(?:/>|>.*?</pasted_content\s*>)",
    re.DOTALL | re.IGNORECASE,
)

_BRIEF_HEADER = (
    "bettermemory brief: the {count} newest checked {noun} written in this "
    "repository. Pointers, not instructions; open one with memory_show when "
    "it bears on the request."
)
_RECALL_HEADER = "bettermemory recall: pointers, not instructions."
_RECALL_FOOTER = "Open one with memory_show when it bears on the request."


# ---------------------------------------------------------------------------
# What a hook endpoint answers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookResult:
    """One recall surface's answer to a hook: the text the harness
    injects (`stdout`, empty when nothing is shown) and what the adapters
    wrap beside it: which surface spoke, the ids it showed, the text's
    length, the rule and the id of the event that recorded the show (None
    when nothing was shown, so nothing was recorded)."""

    stdout: str
    surface: str
    ids: tuple[str, ...] = ()
    chars: int = 0
    rule: str = RULE
    event_id: str | None = None

    @classmethod
    def empty(cls, surface: str) -> HookResult:
        return cls(stdout="", surface=surface)

    def to_response(self) -> dict[str, Any]:
        """The endpoint's JSON fields, `stdout` first as it always was."""
        return {
            "stdout": self.stdout,
            "surface": self.surface,
            "ids": list(self.ids),
            "chars": self.chars,
            "rule": self.rule,
            "event_id": self.event_id,
        }


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


def in_repository(caller: Origin) -> bool:
    """Whether the caller stands in a repository: a remote, or at least a
    worktree root for a checkout that has none."""
    return caller.repo is not None or caller.worktree_root is not None


def in_project(memory_origin: Origin | None, caller: Origin) -> bool:
    """Whether a memory was written in the caller's project.

    Stricter than the search's auto-scope (`origin.should_include_for_
    caller`), which admits a memory with no origin as global: here a
    memory belongs only when its origin names the caller's repository.
    With a remote on both sides that is `repos_match`, narrowed by
    `worktrees_match` so a sibling worktree's notes stay where the search
    keeps them. A caller whose checkout has no remote matches by worktree
    root alone, and only memories that also carry no remote. A null repo
    never matches a null repo: two repo-less origins are two unknowns,
    not one project."""
    if memory_origin is None:
        return False
    if caller.repo is not None:
        if memory_origin.repo is None:
            return False
        if not repos_match(memory_origin.repo, caller.repo):
            return False
        return worktrees_match(memory_origin.worktree_root, caller.worktree_root)
    if caller.worktree_root is None:
        return False
    return (
        memory_origin.repo is None
        and memory_origin.worktree_root == caller.worktree_root
    )


def summarize(body: str, limit: int = SUMMARY_CHARS) -> str:
    """The body, whitespace collapsed, cut to `limit` characters with an
    ellipsis when it was longer."""
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def disputed_marks(pairs: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    """``{memory id: mark}`` for every member of a pending conflict pair.

    A memory is disputed when it is either side of any conflicts row
    whose status is ``pending``, whatever the detector. The mark names one
    pair (the lowest id, so the mark is stable) and, when the memory sits
    in several, how many more: ``[disputed: cf-0123456789ab]`` or
    ``[disputed: cf-0123456789ab +2]``, never more than 31 characters.
    Disputed rows are delivered with the mark, not held back: the
    disagreement is itself worth knowing, and the queue is where it is
    settled. A dismissed pair a later scan resurrects is pending again,
    so it marks again."""
    by_member: dict[str, list[str]] = {}
    for a_id, b_id, pair_id in pairs:
        for member in (a_id, b_id):
            listed = by_member.setdefault(member, [])
            if pair_id not in listed:
                listed.append(pair_id)
    out: dict[str, str] = {}
    for member, pair_ids in by_member.items():
        pair_ids.sort()
        more = len(pair_ids) - 1
        suffix = f" +{min(more, 99)}" if more else ""
        out[member] = f"[disputed: {pair_ids[0][:15]}{suffix}]"
    return out


def _contradiction_open(memory: Memory, contradicted: Mapping[str, datetime]) -> bool:
    """Whether a `use` event with outcome ``contradicted`` names `memory`
    and no update or verify of it came later. An update bumps `updated`
    and a verify stamps `last_verified_at`, so the later of the two is
    when the record was last answered for (the rule `health` reads for
    its contradicted bucket)."""
    stamp = contradicted.get(memory.id)
    if stamp is None:
        return False
    answered = ensure_utc(memory.updated)
    verified = ensure_utc(memory.last_verified_at)
    if verified is not None and (answered is None or verified > answered):
        answered = verified
    return answered is None or stamp > answered


def _origin_for(cwd: Path | None) -> Origin:
    """The caller's origin, as the hooks capture it (`hook._origin_for`)."""
    from .hook import _origin_for as hook_origin_for

    return hook_origin_for(cwd)


def _open_store(cfg: Config) -> Store | None:
    """The store the config names, opened for the in-process hook path, or
    None when there is none (a hook never creates one)."""
    root = cfg.resolved_directory()
    if not (root / STORE_FILENAME).is_file():
        return None
    return Store.open_or_create(root / STORE_FILENAME)


def _str_ids(value: Any) -> list[str]:
    return [memory_id for _, memory_id in _event_id_items(value)]


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass
class _SessionRecall:
    """One (harness, session id)'s cooldown: the segment it is in, when
    that segment opened, the ids shown in it and how many recalls it has
    delivered, and when the session was last seen.

    For the Stop: `shown_by` names, per id shown in the segment, the
    surface that showed it and that show's event id; `settled` holds the
    ids whose show the Stop has already settled in the segment (at most
    one settlement per id per segment); `review_roots` the review chain
    roots already counted unanswered, for the whole session."""

    segment: int
    started_at: datetime
    shown: set[str]
    deliveries: int
    last_seen: datetime
    shown_by: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    settled: set[str] = field(default_factory=set)
    review_roots: set[str] = field(default_factory=set)


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


def _view(entry: _SessionRecall) -> SegmentView:
    return SegmentView(
        segment=entry.segment,
        started_at=entry.started_at,
        shown=frozenset(entry.shown),
        deliveries=entry.deliveries,
        shown_by=dict(entry.shown_by),
        settled=frozenset(entry.settled),
        review_roots=frozenset(entry.review_roots),
    )


def rebuild_session(
    store: Store,
    key: tuple[str, str],
    *,
    now: datetime,
    idle_seconds: int = STATE_IDLE_SECONDS,
) -> _SessionRecall:
    """A session's cooldown computed from the log: what a restarted
    daemon, or the in-process CLI path, knows about it.

    Reads the session's own `session_brief`, `prompt_recall` and
    `turn_audited` rows inside the idle window, through the kind index.
    Each show record carries the segment it was counted in, so the walk
    adopts the newest segment number and keeps only that segment's shown
    ids and delivery count. A `prompt_recall` written before the recall
    surface carries no `shown_ids`; its one delivered hit is the first of
    `top_hits`. A session the log does not know starts a segment now.

    The Stop's own rows are read too: a `use` with a `surface` is a
    settlement, and carries its segment like a show does; a
    `review_unanswered` names roots counted for the whole session. Any
    other `use` under the session (the hook's attribution of a real
    retrieval) is not the recall's and is skipped before it can stamp
    the session's start."""
    harness, session_id = key
    segment = 0
    started: datetime | None = None
    shown: set[str] = set()
    shown_by: dict[str, tuple[str, str | None]] = {}
    settled: set[str] = set()
    review_roots: set[str] = set()
    deliveries = 0
    for event in store.events_of_kinds(
        _STATE_KINDS, since=now - timedelta(seconds=idle_seconds)
    ):
        if (event.get("session") or event.get("session_id")) != session_id:
            continue
        named = event.get("harness")
        if isinstance(named, str) and named != harness:
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
        if started is None:
            started = stamp
        if kind == "turn_audited":
            continue
        number = event.get("segment")
        if isinstance(number, int) and not isinstance(number, bool):
            if number != segment:
                segment, shown, deliveries, started = number, set(), 0, stamp
                shown_by, settled = {}, set()
        if kind == "segment_opened":
            continue
        if kind == "use":
            settled.update(_str_ids(event.get("ids")))
            continue
        shown_event_id = event.get("event_id")
        shown_event = shown_event_id if isinstance(shown_event_id, str) else None
        surface = _SURFACE_OF_KIND.get(str(kind), SURFACE_RECALL)
        if kind == "session_brief":
            brief_ids = _str_ids(event.get("ids"))
            shown.update(brief_ids)
            for memory_id in brief_ids:
                shown_by.setdefault(memory_id, (surface, shown_event))
            continue
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
        shown.update(delivered)
        for memory_id in delivered:
            shown_by.setdefault(memory_id, (surface, shown_event))
        deliveries += 1
    return _SessionRecall(
        segment=segment,
        started_at=started if started is not None else now,
        shown=shown,
        deliveries=deliveries,
        last_seen=now,
        shown_by=shown_by,
        settled=settled,
        review_roots=review_roots,
    )


class RecallState:
    """The recall surface's process-wide state, lock-guarded.

    Two tables. Sessions: one `_SessionRecall` per (harness, session id),
    least recently seen first, evicted after `idle_seconds` without a hook
    and beyond `max_sessions`; a session not held is rebuilt from the log
    (`rebuild_session`) on first sight. Briefs: per repository, the HEAD
    the last brief there read (a brief is warm while it holds) and the one
    background fill in flight, and what each repository's last finished
    fill kept (`fill_report`).

    The daemon holds one for its lifetime and calls it from worker
    threads. The in-process CLI path builds a fresh one per call with
    `background=False`: it holds nothing between calls and runs no fill.
    The store each call passes is the caller's own connection; the state
    holds none."""

    def __init__(
        self,
        *,
        background: bool = True,
        max_sessions: int = STATE_MAX_SESSIONS,
        idle_seconds: int = STATE_IDLE_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[tuple[str, str], _SessionRecall] = OrderedDict()
        self._heads: OrderedDict[str, str] = OrderedDict()
        self._fills: OrderedDict[str, threading.Thread] = OrderedDict()
        self._reports: OrderedDict[str, FillReport] = OrderedDict()
        self._background = background
        self._max_sessions = max_sessions
        self._idle_seconds = idle_seconds

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
        rebuilt = rebuild_session(store, key, now=now, idle_seconds=self._idle_seconds)
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
            entry = self._sessions.get(key)
            if entry is None:  # evicted between the two locks: start afresh
                entry = _SessionRecall(0, now, set(), 0, now)
                self._sessions[key] = entry
            return _view(entry)

    def open_segment(
        self, store: Store, key: tuple[str, str], *, source: str, now: datetime
    ) -> SegmentView:
        """A SessionStart: ``compact`` and ``clear`` open a new segment
        (nothing shown, no deliveries); ``startup`` and ``resume`` keep
        the one the session is in."""
        self._entry(store, key, now)
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                entry = _SessionRecall(0, now, set(), 0, now)
                self._sessions[key] = entry
            if source in _NEW_SEGMENT_SOURCES:
                entry.segment += 1
                entry.started_at = now
                entry.shown = set()
                entry.shown_by = {}
                entry.settled = set()
                entry.deliveries = 0
            return _view(entry)

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
        segment they were counted in; no ids when the segment has had its
        `SEGMENT_DELIVERY_CAP`. What the caller read before (`segment`)
        may be stale by the time it delivers when two prompts of one
        session run at once; this is the step that decides, so no id is
        shown twice in a segment and no segment passes its cap."""
        with self._lock:
            entry = self._held(key, now)
            if entry.deliveries >= SEGMENT_DELIVERY_CAP:
                return [], entry.segment
            kept = [memory_id for memory_id in ids if memory_id not in entry.shown]
            if not kept:
                return [], entry.segment
            entry.shown.update(kept)
            for memory_id in kept:
                entry.shown_by.setdefault(memory_id, (SURFACE_RECALL, event_id))
            entry.deliveries += 1
            return kept, entry.segment

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

    # -- briefs ---------------------------------------------------------------

    def brief_is_warm(self, repo_key: str, head: str | None) -> bool:
        """Whether a brief in `repo_key` may read the memo alone: the
        files named the head, and it is the head the last brief there
        read."""
        if head is None:
            return False
        with self._lock:
            return self._heads.get(repo_key) == head

    def note_brief_head(self, repo_key: str, head: str | None) -> None:
        with self._lock:
            if head is None:
                self._heads.pop(repo_key, None)
                return
            self._heads[repo_key] = head
            self._heads.move_to_end(repo_key)
            while len(self._heads) > STATE_MAX_BRIEF_KEYS:
                self._heads.popitem(last=False)

    def _prune_fills(self) -> None:
        """Drop finished fills, then the oldest beyond the bound (lock
        held). A running fill dropped from the table runs on; it is only
        no longer the one a new fill for its key waits behind."""
        for key in [k for k, thread in self._fills.items() if not thread.is_alive()]:
            del self._fills[key]
        while len(self._fills) > STATE_MAX_BRIEF_KEYS:
            self._fills.popitem(last=False)

    def schedule_fill(
        self,
        repo_key: str,
        memories: Sequence[Memory],
        *,
        caller_origin: Origin,
    ) -> bool:
        """Resolve `memories`' commit legs into the memo on a worker thread
        (`fill_verdicts`), off the request path, so the next brief reads
        them warm; what it kept is kept for `fill_report`. One fill per
        repository at a time: while one runs, another is not started (the
        rows it would have filled are still unresolved next call and are
        scheduled then). Returns whether a fill started; never one without
        `background`."""
        if not self._background or not memories:
            return False
        rows = list(memories)
        with self._lock:
            running = self._fills.get(repo_key)
            if running is not None and running.is_alive():
                return False

            def fill() -> None:
                try:
                    report = fill_verdicts(rows, caller_origin=caller_origin)
                    with self._lock:
                        self._reports[repo_key] = report
                        self._reports.move_to_end(repo_key)
                        while len(self._reports) > STATE_MAX_BRIEF_KEYS:
                            self._reports.popitem(last=False)
                except Exception as exc:  # noqa: BLE001 - a fill is best effort
                    log.warning(
                        "brief fill for %s failed: %s: %s",
                        repo_key,
                        exc.__class__.__name__,
                        exc,
                    )

            thread = threading.Thread(
                target=fill, name=f"bettermemory-brief-fill:{repo_key}", daemon=True
            )
            self._prune_fills()
            self._fills[repo_key] = thread
            thread.start()
            return True

    def fill_report(self, repo_key: str) -> FillReport | None:
        """What the last finished fill for `repo_key` kept, or None when
        none has finished (or it failed): diagnostics for a bench."""
        with self._lock:
            return self._reports.get(repo_key)

    def wait_for_fills(self, timeout: float = 30.0) -> bool:
        """Join every fill in flight; True when none is left running."""
        with self._lock:
            threads = list(self._fills.values())
        for thread in threads:
            thread.join(timeout)
        return not any(thread.is_alive() for thread in threads)


# ---------------------------------------------------------------------------
# The session brief
# ---------------------------------------------------------------------------


def brief_key(caller: Origin) -> str:
    """The key the brief's state is held under for `caller`: its repository
    (the worktree root, else the remote) and the directory it stands in
    (`RecallState.brief_is_warm`, `schedule_fill`, `fill_report`)."""
    return f"{caller.worktree_root or caller.repo or ''}\0{caller.cwd or ''}"


def _head_of(caller: Origin) -> str | None:
    """The caller's HEAD from the repository's files, with no process;
    None where the files do not settle it (the brief is then never warm)."""
    where = caller.cwd or caller.worktree_root
    if where is None:
        return None
    found = toplevel_and_head_from_files(Path(where))
    return found[1] if found is not None else None


def _fresh(
    memory: Memory,
    verdict: MemoryVerdict,
    trust: TrustRow,
    responses: ResponseBuilder,
) -> bool:
    """Whether `memory_show` would read the row ``fresh``: the verdict,
    then the trust rule applied to it the way the show applies it to its
    assembled response (`ResponseBuilder.apply_trust`)."""
    row: dict[str, Any] = {
        "id": memory.id,
        "last_verified_at": isoformat_optional(memory.last_verified_at),
        "verification": verdict.verification.to_dict(),
        "staleness_verdict": verdict.staleness_verdict,
    }
    responses.apply_trust(
        row, provenance=trust.provenance, verified_locally_at=trust.verified_locally_at
    )
    return bool(row["staleness_verdict"] == "fresh")


@dataclass(frozen=True)
class _BriefSelection:
    memories: list[Memory]
    examined: int
    unresolved: int


def _select_brief(
    store: Store,
    caller: Origin,
    *,
    state: RecallState,
    stale_after_days: int,
    now: datetime,
) -> _BriefSelection:
    """The brief's rows, newest `updated` first.

    Eligible: written in the caller's project (`in_project`), stored and
    verified label local or imported, category not ambient, no open
    contradiction; within the newest `BRIEF_WINDOW`. Shown: the first
    `BRIEF_MAX_POINTERS` of those `memory_show` would read fresh. A store
    opened without its key verifies no label, so it shows nothing.

    Warmth is keyed on the repository AND the directory the caller stands
    in, since the drift memo keys on that directory too: a brief from a
    subdirectory is cold until a brief from there has run. A warm call
    whose memo cannot answer some rows, and would therefore show fewer
    than `BRIEF_MAX_POINTERS`, resolves the first `BRIEF_SYNC_RESOLVE` of
    them on the request path, as a cold call does, and hands the rest to
    the fill."""
    repo_key = brief_key(caller)
    ids = store.brief_rows(
        admit=lambda memory_origin: in_project(memory_origin, caller),
        provenance=_DELIVERED_PROVENANCE,
        exclude_category=Category.AMBIENT.value,
    )[:BRIEF_WINDOW]
    if not ids:
        return _BriefSelection([], 0, 0)
    trust = store.trust_rows(ids)
    if trust is None:
        return _BriefSelection([], 0, 0)
    ids = [
        memory_id
        for memory_id in ids
        if (row := trust.get(memory_id)) is not None
        and row.provenance in _DELIVERED_PROVENANCE
    ]
    contradicted = store.contradicted_at(ids)
    responses = ResponseBuilder(stale_after_days=stale_after_days)

    def eligible(chunk: Sequence[str]) -> list[Memory]:
        return [
            memory
            for memory in store.load_many(list(chunk))
            if not _contradiction_open(memory, contradicted)
        ]

    head = _head_of(caller)
    picked: list[Memory] = []
    pending: list[Memory] = []
    examined = 0
    if state.brief_is_warm(repo_key, head):
        # Each examined row in order, with whether it reads fresh (None
        # while the memo cannot say).
        ordered: list[tuple[Memory, bool | None]] = []
        fresh_count = 0
        for start in range(0, len(ids), _BRIEF_CHUNK):
            chunk = eligible(ids[start : start + _BRIEF_CHUNK])
            verdicts = memoised_verdicts(
                chunk,
                caller_origin=caller,
                now=now,
                stale_after_days=stale_after_days,
            )
            for memory, verdict in zip(chunk, verdicts):
                examined += 1
                if isinstance(verdict, UnresolvedVerdict):
                    pending.append(memory)
                    ordered.append((memory, None))
                    continue
                fresh = _fresh(memory, verdict, trust[memory.id], responses)
                ordered.append((memory, fresh))
                if fresh:
                    fresh_count += 1
                    if fresh_count >= BRIEF_MAX_POINTERS:
                        break
            if fresh_count >= BRIEF_MAX_POINTERS:
                break
        if pending and fresh_count < BRIEF_MAX_POINTERS:
            on_request = pending[:BRIEF_SYNC_RESOLVE]
            pending = pending[BRIEF_SYNC_RESOLVE:]
            settled = {
                memory.id: _fresh(memory, full, trust[memory.id], responses)
                for memory, full in zip(
                    on_request,
                    resolve_verdicts(
                        on_request,
                        caller_origin=caller,
                        now=now,
                        stale_after_days=stale_after_days,
                    ),
                )
            }
            ordered = [
                (memory, settled.get(memory.id) if fresh is None else fresh)
                for memory, fresh in ordered
            ]
        picked = [memory for memory, fresh in ordered if fresh][:BRIEF_MAX_POINTERS]
    else:
        synchronous: list[Memory] = []
        cursor = 0
        while len(synchronous) < BRIEF_SYNC_RESOLVE and cursor < len(ids):
            take = ids[cursor : cursor + BRIEF_SYNC_RESOLVE - len(synchronous)]
            cursor += len(take)
            synchronous.extend(eligible(take))
        resolved = resolve_verdicts(
            synchronous,
            caller_origin=caller,
            now=now,
            stale_after_days=stale_after_days,
        )
        for memory, full in zip(synchronous, resolved):
            examined += 1
            if len(picked) < BRIEF_MAX_POINTERS and _fresh(
                memory, full, trust[memory.id], responses
            ):
                picked.append(memory)
        pending = eligible(ids[cursor:])
        state.note_brief_head(repo_key, head)
    if pending:
        state.schedule_fill(
            repo_key,
            pending,
            caller_origin=caller,
        )
    return _BriefSelection(picked, examined, len(pending))


def _brief_line(memory: Memory, marks: Mapping[str, str]) -> str:
    line = f"- {memory.id} {summarize(memory.body)}"
    mark = marks.get(memory.id)
    return f"{line} {mark}" if mark else line


def render_brief(
    memories: Sequence[Memory], marks: Mapping[str, str] | None = None
) -> tuple[str, list[str]]:
    """The brief's text and the ids it shows: the header naming the count,
    then one ``- <id> <summary>`` line per memory, with its disputed mark.
    Trailing pointers are dropped until the whole fits `BRIEF_CAP_CHARS`
    (the header is recounted); ``("", [])`` when nothing fits or nothing
    was given."""
    shown_marks = marks or {}
    lines = [_brief_line(memory, shown_marks) for memory in memories]
    ids = [memory.id for memory in memories]
    while lines:
        count = len(lines)
        header = _BRIEF_HEADER.format(
            count=count, noun="memory" if count == 1 else "memories"
        )
        text = "\n".join([header, *lines])
        if len(text) <= BRIEF_CAP_CHARS:
            return text, ids[:count]
        lines.pop()
    return "", []


def session_brief(
    *,
    session_id: str | None,
    source: str | None = None,
    harness: str | None = None,
    transcript_path: str | None = None,
    cwd: Path | None = None,
    origin: Origin | None = None,
    config: Config | None = None,
    store: Store | None = None,
    state: RecallState | None = None,
    now: datetime | None = None,
) -> HookResult:
    """The SessionStart surface: open the session's segment (a new one on
    ``compact`` / ``clear``) and, inside a repository, return the brief.

    Nothing is shown without a session id (the show has no session to be
    recorded under or to count against), with `session_brief` or
    telemetry off, outside a repository or where git could not say
    whether the caller stands in one, on an empty store, or when no row
    passes (`_select_brief`). A shown brief is recorded as a
    `session_brief` event under the session id, stamped
    ``triggered_from="session_hook"`` so the hooks' server-session
    anchor (`hook._latest_in_process_session`) skips it like every other
    out-of-process row; it is not a retrieval kind, so the Stop audit's
    shield does not count it. Its ids join the segment's shown set, so the
    prompt recall does not point at them again.

    A ``compact`` or ``clear`` that shows no brief, with telemetry on,
    records a `segment_opened` row instead (the session id, harness,
    source and the new segment's number), so the segment is read back
    after a restart and on the in-process path, which rebuilds the state
    from the log on every call."""
    empty = HookResult.empty("session_brief")
    if not session_id:
        return empty
    cfg = config or load_config(None)
    if store is None:
        store = _open_store(cfg)
        if store is None:
            return empty
    state = state if state is not None else RecallState(background=False)
    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    opened_by = source if source in _SESSION_SOURCES else "startup"
    view = state.open_segment(store, key, source=opened_by, now=now)
    result = _brief_in_segment(
        key=key,
        view=view,
        opened_by=opened_by,
        transcript_path=transcript_path,
        cwd=cwd,
        origin=origin,
        cfg=cfg,
        store=store,
        state=state,
        now=now,
    )
    if (
        result.event_id is None
        and opened_by in _NEW_SEGMENT_SOURCES
        and cfg.telemetry.enabled
    ):
        # No brief carried the new segment's number to the log: record
        # it alone, or a restarted daemon and the in-process path (which
        # rebuilds on every call) would never see the compact.
        caller = origin if origin is not None else _origin_for(cwd)
        Recorder(
            store=store,
            session_id=key[1],
            enabled=cfg.telemetry.enabled,
            worktree_root=caller.worktree_root,
            triggered_from="session_hook",
        ).record(
            "segment_opened",
            session_id=key[1],
            harness=key[0],
            source=opened_by,
            segment=view.segment,
            rule=RULE,
        )
    return result


def _brief_in_segment(
    *,
    key: tuple[str, str],
    view: SegmentView,
    opened_by: str,
    transcript_path: str | None,
    cwd: Path | None,
    origin: Origin | None,
    cfg: Config,
    store: Store,
    state: RecallState,
    now: datetime,
) -> HookResult:
    """The brief for the segment `session_brief` just opened or resumed,
    recorded when shown; empty when nothing is."""
    empty = HookResult.empty("session_brief")
    if not cfg.behavior.session_brief or not cfg.telemetry.enabled:
        return empty
    caller = origin if origin is not None else _origin_for(cwd)
    if caller.git_indeterminate or not in_repository(caller):
        return empty
    if store.count_memories() == 0:
        return empty
    selection = _select_brief(
        store,
        caller,
        state=state,
        stale_after_days=cfg.behavior.verification_stale_days,
        now=now,
    )
    if not selection.memories:
        return empty
    text, ids = render_brief(
        selection.memories, disputed_marks(store.pending_conflict_pairs())
    )
    if not ids:
        return empty
    identity.bind_transcript(session_id=key[1], model=None, client=key[0])
    event_id = generate_ulid()
    recorder = Recorder(
        store=store,
        session_id=key[1],
        enabled=cfg.telemetry.enabled,
        worktree_root=caller.worktree_root,
        triggered_from="session_hook",
    )
    fields: dict[str, Any] = {
        "event_id": event_id,
        "session_id": key[1],
        "harness": key[0],
        "source": opened_by,
        "segment": view.segment,
        "ids": ids,
        "chars": len(text),
        "rows_examined": selection.examined,
        "unresolved": selection.unresolved,
        "rule": RULE,
    }
    if transcript_path:
        fields["transcript_path"] = transcript_path
    recorder.record("session_brief", **fields)
    state.note_shown(
        key, ids, delivery=False, now=now, surface=SURFACE_BRIEF, event_id=event_id
    )
    return HookResult(
        stdout=text,
        surface="session_brief",
        ids=tuple(ids),
        chars=len(text),
        rule=RULE,
        event_id=event_id,
    )


# ---------------------------------------------------------------------------
# The prompt recall
# ---------------------------------------------------------------------------


def clean_prompt(prompt: str) -> str:
    """The prompt as the probe reads it: every ``<pasted_content …>`` block
    removed (a paste is material the user handed over, not what they
    asked), then cut at `PROMPT_CUT_CHARS`."""
    return _PASTED_CONTENT.sub(" ", prompt)[:PROMPT_CUT_CHARS]


@dataclass(frozen=True)
class RecallPointer:
    """One delivered candidate as the block renders it.

    `provenance` is the verified label; it is named on the line only when
    it is not ``local``. `remote_stamp` and `trust_unavailable` keep the
    two annotations the one-hit block carried (a synced record's stamp
    this host never made; a store opened without its key): the rule's
    provenance gate admits local and imported records only, so neither is
    reached by a delivery today, and the renderer keeps them so a widened
    gate cannot deliver such a record unannounced."""

    id: str
    relevance: str
    summary: str
    snippet: str
    provenance: str | None = None
    remote_stamp: bool = False
    trust_unavailable: bool = False
    disputed: str | None = None


def _annotation(pointer: RecallPointer) -> str:
    if pointer.trust_unavailable:
        return "[provenance: unknown, key absent]"
    if pointer.provenance is not None and pointer.provenance != LOCAL:
        qualifier = ", unverified here" if pointer.remote_stamp else ""
        return f"[provenance: {pointer.provenance}{qualifier}]"
    return ""


def _recall_line(pointer: RecallPointer, snippet: str | None) -> str:
    parts = [f"- {pointer.id} [{pointer.relevance}] {pointer.summary}"]
    annotation = _annotation(pointer)
    if annotation:
        parts.append(annotation)
    if pointer.disputed:
        parts.append(pointer.disputed)
    line = " ".join(parts)
    return f"{line} — {snippet}" if snippet else line


def render_recall_block(pointers: Sequence[RecallPointer]) -> tuple[str, list[str]]:
    """The recall block and the ids it shows.

    The header line, one line per pointer (``- <id> [<relevance>]
    <summary>``, its annotations, then `` — `` and the query-biased
    snippet cut at `SNIPPET_CHARS`), and the footer. Over
    `RECALL_CAP_CHARS` the snippets are cut first, from the last pointer
    back (a snippet that would fall under a dozen characters is dropped),
    then trailing pointers go. ``("", [])`` when nothing fits."""
    kept = list(pointers)
    snippets: list[str | None] = [
        summarize(pointer.snippet, SNIPPET_CHARS) or None for pointer in kept
    ]

    def render() -> str:
        lines = [_recall_line(p, s) for p, s in zip(kept, snippets)]
        return "\n".join([_RECALL_HEADER, *lines, _RECALL_FOOTER])

    text = render()
    for index in range(len(kept) - 1, -1, -1):
        overflow = len(text) - RECALL_CAP_CHARS
        if overflow <= 0:
            break
        snippet = snippets[index]
        if snippet is None:
            continue
        target = len(snippet) - overflow
        snippets[index] = (
            summarize(snippet, target) if target >= _MIN_SNIPPET_CHARS else None
        )
        text = render()
    while kept and len(text) > RECALL_CAP_CHARS:
        kept.pop()
        snippets.pop()
        text = render()
    if not kept:
        return "", []
    return text, [pointer.id for pointer in kept]


def _seen_in_log(
    events: Iterable[dict[str, Any]],
    *,
    since: datetime,
    own_since: datetime,
    sessions: Collection[str],
    worktree_root: str | None,
    harness_session: str | None = None,
) -> set[str]:
    """The ids retrieved (a search, list or show hit), written or updated
    in the segment, from the log.

    Two halves, the union the audit's retrieval shield counts
    (`audit._count_recent_retrievals`): events recorded under one of
    `sessions` (the harness session and the bridged server session) at
    or after `own_since`, and events stamped with the caller's worktree
    under any session at or after `since`, the segment's start. The two
    cuts differ for a session's first segment, which began when the
    session did, a moment no record may carry (a brief is recorded only
    when one was shown): the caller passes the idle window for its own
    sessions there, and the first sight of the session for the worktree
    half, which would otherwise reach back into an earlier conversation
    in the same checkout. The worktree half errs toward holding a pointer
    back: a concurrent session's retrieval in the same checkout counts.

    An event stamped with a harness session (`events.HARNESS_SESSION_FIELD`)
    is this conversation's, under the own cut, when the stamp names
    `harness_session` or its session is one of `sessions` (a server whose
    stamp predates a /clear, which the bridge joined), and never by
    worktree. The two halves above are the bridge for events that carry
    no stamp."""
    seen: set[str] = set()
    for event in events:
        kind = event.get("kind")
        if kind not in _SEEN_KINDS:
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None:
            continue
        stamped = event.get(HARNESS_SESSION_FIELD)
        if isinstance(stamped, str) and stamped:
            # Another stamp joins through its session alone: a server
            # stamped with this window's id from before a /clear is in
            # `sessions` by the bridge, another window's never is.
            own = stamped == harness_session or (
                (event.get("session") or event.get("session_id")) in sessions
            )
            if not own or stamp < own_since:
                continue
        else:
            session = event.get("session") or event.get("session_id")
            own = session in sessions and stamp >= own_since
            here = (
                worktree_root is not None
                and event.get("worktree_root") == worktree_root
                and stamp >= since
            )
            if not (own or here):
                continue
        if kind in ("show", "write", "update"):
            # A refused write or update carries a status; a committed
            # update carries none, a committed write may say so.
            if kind != "show" and event.get("status", "committed") != "committed":
                continue
            memory_id = event.get("id")
            if isinstance(memory_id, str):
                seen.add(memory_id)
            continue
        seen.update(
            _str_ids(
                event.get("returned") or event.get("memory_ids") or event.get("hit_ids")
            )
        )
    return seen


def prompt_recall(
    *,
    prompt: str,
    session_id: str,
    harness: str | None = None,
    prompt_id: str | None = None,
    transcript_path: str | None = None,
    cwd: Path | None = None,
    origin: Origin | None = None,
    config: Config | None = None,
    store: Store | None = None,
    state: RecallState | None = None,
    now: datetime | None = None,
) -> HookResult:
    """The UserPromptSubmit surface: probe the prompt and return up to
    three pointers, or nothing.

    The probe is the audit's (`hook._probe_message`), unchanged: the
    production pool and ranker over the cleaned prompt (`clean_prompt`),
    the content-token floor and the acknowledgment gate, top three. A
    ``no_signal`` report delivers nothing. Each hit is then a candidate,
    held back with a reason when it is ``low_relevance`` (not medium or
    high), ``other_project`` (not written in the caller's project, under
    the default `prompt_recall = "project"`), ``provenance`` (not local or
    imported, or unverifiable), ``contradicted`` (an open contradiction),
    ``seen_in_segment`` (shown, retrieved, written or updated in this
    session segment) or ``segment_cap`` (the segment has had its twelve). The
    probe's verdict and its retrieval shield do not gate delivery; the
    segment does.

    Nothing outside a repository unless `prompt_recall = "everywhere"`;
    nothing where git could not say where the caller stands; nothing
    with telemetry off. A delivery is recorded as a `prompt_recall`
    event BEFORE the block is returned: it is a member of the audit's
    retrieval kinds, so the Stop audit of the same turn sees the
    delivery and reports ``ok`` rather than a silent miss."""
    empty = HookResult.empty("prompt_recall")
    cfg = config or load_config(None)
    mode = cfg.behavior.prompt_recall
    if mode not in ("project", "everywhere") or not cfg.telemetry.enabled:
        return empty
    if store is None:
        store = _open_store(cfg)
        if store is None:
            return empty
    query = clean_prompt(prompt)
    if not query.strip():
        return empty
    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    # Same caller the Stop hook publishes (`hook.run_audit`), minus the
    # model: this hook fires before the assistant has answered.
    identity.bind_transcript(session_id=key[1], model=None, client=key[0])
    caller = origin if origin is not None else _origin_for(cwd)
    if caller.git_indeterminate:
        # Delivery is scoped on the caller's repository and worktree, and
        # git could not say what they are. A null repo here would open
        # the probe's auto-scope to every project's memories on a prompt
        # that asked for none of them. No delivery is the only honest
        # answer; the Stop audit declines to call the turn a miss for the
        # same reason.
        return empty
    if mode == "project" and not in_repository(caller):
        return empty

    from .hook import (
        _disabled_scopes_from_events,
        _probe_message,
        bridged_session,
        joined_sessions,
    )

    state = state if state is not None else RecallState(background=False)
    view = state.segment(store, key, now=now)
    recent_cut = now - timedelta(seconds=REAUDIT_DEDUP_WINDOW_SECONDS)
    # A first segment began with the session; a later one when its
    # compact or clear opened it (`_seen_in_log` says why the cuts differ).
    own_since = (
        view.started_at
        if view.segment > 0
        else now - timedelta(seconds=STATE_IDLE_SECONDS)
    )
    recent = list(store.events_since(recent_cut))
    # The conversation's in-process rows: the harness join first (tool
    # events stamped with this session id), the worktree bridge only
    # when nothing is stamped with it.
    server_session = bridged_session(
        recent, harness_session=key[1], worktree_root=caller.worktree_root
    )
    excluded_scopes = _disabled_scopes_from_events(
        recent, worktree_root=caller.worktree_root, server_session=server_session
    )
    report = _probe_message(
        cfg=cfg,
        store=store,
        user_message=query,
        session_id=key[1],
        server_session=server_session,
        caller_origin=caller,
        excluded_scopes=excluded_scopes,
        recent=recent,
    )
    if report.verdict == "no_signal" or not report.top_hits:
        return empty

    sessions = {key[1]} | joined_sessions(
        recent, harness_session=key[1], worktree_root=caller.worktree_root
    )
    seen = set(view.shown) | _seen_in_log(
        store.events_of_kinds(_SEEN_KINDS, since=min(own_since, view.started_at)),
        since=view.started_at,
        own_since=own_since,
        sessions=sessions,
        worktree_root=caller.worktree_root,
        harness_session=key[1],
    )
    hit_ids = [hit.id for hit in report.top_hits]
    memories = {memory.id: memory for memory in store.load_many(hit_ids)}
    trust = store.trust_rows(hit_ids)
    contradicted = store.contradicted_at(hit_ids)
    chosen: list[tuple[MissHit, Memory, TrustRow]] = []
    suppressed: list[dict[str, str]] = []
    for hit in report.top_hits:
        memory = memories.get(hit.id)
        if memory is None:
            continue
        row = trust.get(hit.id) if trust is not None else None
        reason: str | None = None
        if hit.relevance not in _RELEVANT:
            reason = "low_relevance"
        elif mode == "project" and not in_project(memory.origin, caller):
            reason = "other_project"
        elif row is None or row.provenance not in _DELIVERED_PROVENANCE:
            reason = "provenance"
        elif _contradiction_open(memory, contradicted):
            reason = "contradicted"
        elif hit.id in seen:
            reason = "seen_in_segment"
        elif view.deliveries >= SEGMENT_DELIVERY_CAP:
            reason = "segment_cap"
        elif len(chosen) >= RECALL_MAX_POINTERS:
            continue
        if reason is not None:
            suppressed.append({"id": hit.id, "reason": reason})
            continue
        assert row is not None
        chosen.append((hit, memory, row))
    if not chosen:
        return empty

    marks = disputed_marks(store.pending_conflict_pairs())
    pointers = [
        RecallPointer(
            id=hit.id,
            relevance=hit.relevance,
            summary=summarize(memory.body),
            snippet=hit.snippet,
            provenance=row.provenance,
            disputed=marks.get(hit.id),
        )
        for hit, memory, row in chosen
    ]
    block, shown_ids = render_recall_block(pointers)
    if not shown_ids:
        return empty
    # The cooldown decides atomically: a concurrent prompt of the same
    # session may have shown some of these ids, or used the last of the
    # segment's deliveries, since `view` was read.
    event_id = generate_ulid()
    claimed, segment = state.claim_delivery(key, shown_ids, now=now, event_id=event_id)
    if not claimed:
        return empty
    if claimed != shown_ids:
        kept = set(claimed)
        block, shown_ids = render_recall_block([p for p in pointers if p.id in kept])
        if not shown_ids:
            return empty
    recorder = Recorder(
        store=store,
        session_id=key[1],
        enabled=cfg.telemetry.enabled,
        worktree_root=caller.worktree_root,
    )
    fields = prompt_recall_fields(
        report,
        session_id=key[1],
        probe_mode=cfg.behavior.search_mode or "hybrid",
        injected_chars=len(block),
        delivered_reason=mode,
        shown_ids=shown_ids,
        suppressed=suppressed,
        rule=RULE,
        segment=segment,
        prompt_id=prompt_id,
        transcript_path=transcript_path,
        harness=key[0],
    )
    fields["event_id"] = event_id
    recorder.record("prompt_recall", **fields)
    return HookResult(
        stdout=block,
        surface="prompt_recall",
        ids=tuple(shown_ids),
        chars=len(block),
        rule=RULE,
        event_id=str(fields["event_id"]),
    )


# ---------------------------------------------------------------------------
# At Stop: settling what was shown, and the unanswered reviews
# ---------------------------------------------------------------------------


def _used_in_segment(
    events: Iterable[dict[str, Any]],
    *,
    since: datetime,
    harness_session: str,
    sessions: Collection[str],
) -> set[str]:
    """The ids any `use` event of this conversation names at or after
    `since` (the segment's start): an explicit `memory_record_use`, the
    in-process fallback, or a Stop's attribution of a real retrieval.
    Joined by harness stamp first, else by session (the joined in-process
    sessions and the hook's own id)."""
    from .hook import event_joined

    used: set[str] = set()
    for event in events:
        if event.get("kind") != "use":
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None or stamp < since:
            continue
        if event_joined(event, harness_session=harness_session, sessions=sessions):
            used.update(_str_ids(event.get("ids") or event.get("memory_ids")))
    return used


def settle_shown(
    *,
    store: Store,
    recorder: Recorder,
    state: RecallState,
    session_id: str,
    assistant_response: str,
    sessions: Collection[str],
    harness: str | None = None,
    exclude: Collection[str] = (),
    prompt_id: str | None = None,
    client_model: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Settle the session segment's shown pointers against the turn's
    reply; the ids settled.

    Every id the brief or the prompt recall showed in the segment, not
    settled in it yet, runs through the containment matcher
    (`attribution.attribute_uses`) against `assistant_response`. A match
    records ONE `use` event: outcome ``applied``, ``attribution="hook"``
    (the matcher's tier), the matched sentence as its claim excerpt, the
    ``surface`` that showed it (``brief`` or ``recall``), that show's
    ``shown_event_id``, the segment, the harness, and the harness turn
    (``prompt_id``) when the Stop named one. An unmatched id records
    nothing: a pointer is not a retrieval, so the auto fallback never
    applies to it.

    Left out: ids in `exclude` (the real retrievals this Stop settled
    through `hook._emit_hook_attributions`), and ids any `use` of this
    conversation already named in the segment, joined by harness stamp
    first and by `sessions` otherwise (`_used_in_segment`), so one reply
    never credits a memory twice. With telemetry off the recorder writes
    nothing and the state is left as it was."""
    from .attribution import attribute_uses
    from .store import MemoryNotFoundError, TombstonedError

    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    view = state.segment(store, key, now=now)
    skip = set(view.settled) | set(exclude)
    pending = [memory_id for memory_id in view.shown_by if memory_id not in skip]
    if not pending or not assistant_response:
        return []
    used = _used_in_segment(
        store.events_of_kinds(("use",), since=view.started_at),
        since=view.started_at,
        harness_session=key[1],
        sessions=set(sessions) | {key[1]},
    )
    bodies: dict[str, str] = {}
    for memory_id in sorted(pending):
        if memory_id in used:
            continue
        try:
            bodies[memory_id] = store.load_one(memory_id).body
        except (MemoryNotFoundError, TombstonedError):
            continue
    if not bodies:
        return []
    settled: list[str] = []
    for match in attribute_uses(bodies, assistant_response):
        surface, shown_event_id = view.shown_by[match.memory_id]
        fields: dict[str, Any] = {
            "ids": [match.memory_id],
            "outcome": "applied",
            "auto": False,
            "attribution": "hook",
            "claim_excerpts": [match.claim_excerpt],
            "surface": surface,
            "shown_event_id": shown_event_id,
            "segment": view.segment,
            "harness": key[0],
            "triggered_from": "stop_hook",
        }
        if prompt_id:
            fields["prompt_id"] = prompt_id
        if client_model is not None:
            fields["client_model"] = client_model
        recorder.record("use", **fields)
        settled.append(match.memory_id)
    if settled and recorder.enabled:
        state.note_settled(key, settled, now=now)
    return settled


def previous_audit_at(
    store: Store, session_id: str, *, now: datetime
) -> datetime | None:
    """When the session's newest `turn_audited` was stamped, inside the
    idle window; None when it has none there. Read before the Stop
    records its own."""
    latest: datetime | None = None
    for event in store.events_of_kinds(
        ("turn_audited",), since=now - timedelta(seconds=STATE_IDLE_SECONDS)
    ):
        if (event.get("session") or event.get("session_id")) != session_id:
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    return latest


def review_unanswered(
    *,
    store: Store,
    recorder: Recorder,
    state: RecallState,
    session_id: str,
    sessions: Collection[str],
    since: datetime,
    harness: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Count the review chains the conversation left open since its
    previous turn; the roots counted.

    Reads the `review_issued` rows recorded under `sessions` (the joined
    in-process sessions: the harness join first, the bridge otherwise) at
    or after `since`, takes each row's chain root (its
    ``parent_review_id``, else its own ``review_id``), and counts each
    root `review.find_chain` reads open: no write row carries any of the
    chain's review ids. A re-send another gate refused before the review
    decided anything carries no review id on its write row, so its chain
    also reads open: the count means unanswered or refused. A root is
    counted once per session (`RecallState.note_review_roots`, rebuilt
    from these rows after a restart).

    A non-zero count records one `review_unanswered` event: ``count``,
    ``root_ids``, the hook's ``session_id``, the ``review_sessions`` read,
    the ``segment`` and the ``harness``. Nothing is recorded for zero,
    and nothing at all with telemetry off. No text is shown."""
    from .review import REVIEW_ISSUED, find_chain
    from .models import is_valid_ulid

    if not recorder.enabled or not sessions:
        return []
    now = now or utcnow()
    key = (harness or DEFAULT_HARNESS, str(session_id))
    view = state.segment(store, key, now=now)
    roots: list[str] = []
    for event in store.events_of_kinds((REVIEW_ISSUED,), since=since):
        if (event.get("session") or event.get("session_id")) not in sessions:
            continue
        stamp = parse_event_ts(event.get("ts"))
        if stamp is None or stamp < since:
            continue
        parent = event.get("parent_review_id")
        own = event.get("review_id")
        root = parent if isinstance(parent, str) and is_valid_ulid(parent) else own
        if not isinstance(root, str) or not is_valid_ulid(root):
            continue
        if root in view.review_roots or root in roots:
            continue
        roots.append(root)
    open_roots = [root for root in roots if find_chain(store, root).open]
    if not open_roots:
        return []
    recorder.record(
        "review_unanswered",
        count=len(open_roots),
        root_ids=open_roots,
        session_id=key[1],
        review_sessions=sorted(sessions),
        segment=view.segment,
        harness=key[0],
        triggered_from="stop_hook",
    )
    state.note_review_roots(key, open_roots, now=now)
    return open_roots
