"""Fixtures: the recall surface's per-session state (``recall_state.jsonl``)
and the process-wide table of sessions (``recall_state_held.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/recall_state.rs`` and its child
``recall_state/held.rs``. This pins `recall_state`'s vocabulary
(`is_settlement`, `agent_of`, `taken_back`, `Subagent.from_payload` and
`fields`), `rebuild_session` over scripted rows, with `_replay`,
`_restart`, `_reopen`, `_hold_signal`, `_withdraw`, `_view` and
`_closing_view` through it, and `RecallState` over scripted calls.

Two files, neither gated: no row depends on the Python release or the
platform (no kind is nested deep enough for `str()` to raise, and every
clock is a fixed text, the sweep's moved by whole microseconds). One JSON
object per line, keys sorted, ASCII. ``recall_state.jsonl``'s first line is the header
(``{"k": "consts", ...}``: the constants and the count of the lines
after it). Every other line is one of:

- ``{"k": "settle" | "agent" | "back", "e": event, "o": answer}``:
  `is_settlement` (a bool or ``{"err": name}``), `agent_of` (a str or
  null) and `taken_back` of one event;
- ``{"k": "sub", "id": ..., "type": ..., "o": answer}``:
  `Subagent.from_payload(id, type)`, null or ``{"id", "type", "by",
  "fields"}`` (`fields()` as ``[key, value]`` pairs in order);
- ``{"k": "rebuild", "name": ..., "rows": events, "agent": ..., "fail":
  n, "o": answer}``: `rebuild_session` over a store whose
  `session_events_of_kinds` yields ``rows`` (and raises
  `sqlite3.OperationalError` after the first ``fail`` rows when ``fail``
  is present), under the key ``[HARNESS, SESSION]`` at ``NOW``; the
  answer is the entry's form or ``{"err": name}``;
- ``{"k": "sweep", "seed": n, "count": n, "sha256": hex}``: `rebuild_session`
  over ``count`` row sequences drawn by `_draw_rows` under splitmix64
  seeded with ``seed``; the SHA-256 of each answer's compact JSON form
  (or ``!`` and the exception's name), each followed by a newline.

``recall_state_held.jsonl`` (not gated) pins `RecallState`. Its first
line is the header (``{"k": "consts", ...}``: the clock's two bases, the
keys, the scripted store's rows and the draw tables of the sweep). Every
other line is one of:

- ``{"k": "held", "name": ..., "state": {"max", "idle", "agent"},
  "store": {"rows", "fail", "during"}, "steps": [...], "end": [...]}``:
  a state over a scripted store (`_HeldStore`: each session's rows as
  JSON text, a raise after ``fail[session]`` rows, and calls made on the
  state while a session's rows are first read), then the calls in order,
  each a step ``{"op", "key", "now", "at", "source", "marker", "ids",
  "work", "ev", "surface", "delivery"}`` (what the op takes) with ``r``:
  ``{"o": answer, "keys": [...], "reads": n}``, the call's answer
  (`_held_call`), the keys held after it in order, and the store reads it
  made; ``end`` is every held entry as ``[key, form]`` (`_held_form`);
- ``{"k": "sweep", "seed": n, "count": n, "sha256": hex}``: ``count``
  call sequences drawn by `_draw_calls` under splitmix64 seeded with
  ``seed``, run as above; the SHA-256 of each step's ``r`` and each
  sequence's ``end`` in compact JSON, each followed by a newline.

An event is its JSON text (``json.dumps``, so an int of any size keeps
its digits). The entry's form: ``seg`` (`str` of the segment), ``at``,
``seen`` (isoformat), ``shown``, ``settled``, ``roots`` (sorted),
``dl`` (the deliveries), ``by`` (``[id, surface, event_id]`` in the
dict's insertion order), ``marker``, ``sig`` (null or ``{"at", "shown",
"dl", "ev"}``), ``view`` (`_view`), ``closing`` (`_closing_view`) and
``kept`` (`_closing_view` with ``keep`` the signal's shown, null with no
signal). A view's form has ``seg``, ``at``, ``shown``, ``dl``, ``by``
(sorted by id), ``settled``, ``roots`` and ``marker``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from bettermemory import recall_state as rs
from bettermemory.transcript import CompactMarker

from ._common import dumps_line, jsonl

HARNESS = "claude-code"
SESSION = "S1"
NOW = "2026-03-01T09:30:00+00:00"
MASK = (1 << 64) - 1
SEED = 20261125
SWEEP = 5000


class _Rng:
    """splitmix64: the same draws in the generator and the native test."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return z ^ (z >> 31)

    def pick(self, options: list[Any]) -> Any:
        return options[self.next() % len(options)]


def _ts(minute: int) -> str:
    return f"2026-01-01T12:{minute:02d}:00Z"


def _iso(value: datetime) -> str:
    return value.isoformat()


def _view_form(view: rs.SegmentView) -> dict[str, Any]:
    return {
        "seg": str(view.segment),
        "at": _iso(view.started_at),
        "shown": sorted(view.shown),
        "dl": view.deliveries,
        "by": [[k, s, e] for k, (s, e) in sorted(view.shown_by.items())],
        "settled": sorted(view.settled),
        "roots": sorted(view.review_roots),
        "marker": view.marker,
    }


def _entry_form(entry: Any) -> dict[str, Any]:
    signal = entry.signal
    return {
        "seg": str(entry.segment),
        "at": _iso(entry.started_at),
        "seen": _iso(entry.last_seen),
        "shown": sorted(entry.shown),
        "dl": entry.deliveries,
        "by": [[k, s, e] for k, (s, e) in entry.shown_by.items()],
        "settled": sorted(entry.settled),
        "roots": sorted(entry.review_roots),
        "marker": entry.marker,
        "sig": None
        if signal is None
        else {
            "at": _iso(signal.at),
            "shown": sorted(signal.shown),
            "dl": signal.deliveries,
            "ev": signal.event_id,
        },
        "view": _view_form(rs._view(entry)),
        "closing": _view_form(rs._closing_view(entry)),
        "kept": None
        if signal is None
        else _view_form(rs._closing_view(entry, keep=signal.shown)),
    }


def _store(rows: list[dict[str, Any]], fail: int | None = None) -> Any:
    def session_events_of_kinds(session: str, kinds: Any) -> Any:
        assert session == SESSION and tuple(kinds) == rs._STATE_KINDS
        for i, row in enumerate(rows):
            if fail is not None and i == fail:
                raise sqlite3.OperationalError("disk I/O error")
            yield row
        if fail is not None and fail >= len(rows):
            raise sqlite3.OperationalError("disk I/O error")

    return SimpleNamespace(session_events_of_kinds=session_events_of_kinds)


def _rebuild(
    rows: list[dict[str, Any]], agent: str | None = None, fail: int | None = None
) -> Any:
    try:
        entry = rs.rebuild_session(
            _store(rows, fail),
            (HARNESS, SESSION),
            now=datetime.fromisoformat(NOW),
            agent_id=agent,
        )
    except (TypeError, RecursionError, sqlite3.OperationalError) as exc:
        return {"err": type(exc).__name__}
    return _entry_form(entry)


# -- the vocabulary -----------------------------------------------------------

_EVENTS: list[dict[str, Any]] = [
    {},
    {"kind": "use"},
    {"kind": "use", "surface": "brief"},
    {"kind": "use", "surface": "recall"},
    {"kind": "use", "surface": "session_brief"},
    {"kind": "use", "surface": None},
    {"kind": "use", "surface": 1},
    {"kind": "use", "surface": True},
    {"kind": "use", "surface": ["brief"]},
    {"kind": "use", "surface": {"brief": 1}},
    {"kind": "prompt_recall", "surface": ["brief"]},
    {"kind": "USE", "surface": "brief"},
    {"kind": ["use"], "surface": "brief"},
    {"kind": "use", "surface": "brief", "agent_id": "A"},
    {"agent_id": ""},
    {"agent_id": None},
    {"agent_id": 7},
    {"agent_id": ["A"]},
    {"agent_id": "A bé"},
    {"kind": "segment_opened", "agent_id": "A", "withdrawn": []},
    {"kind": "segment_opened", "agent_id": "A", "withdrawn": ["x", 3]},
    {"kind": "segment_opened", "agent_id": "A", "withdrawn": "x"},
    {"kind": "segment_opened", "agent_id": "A", "withdrawn": None},
    {"kind": "segment_opened", "agent_id": "A"},
    {"kind": "segment_opened", "agent_id": "", "withdrawn": ["x"]},
    {"kind": "segment_opened", "withdrawn": ["x"]},
    {"kind": "session_brief", "agent_id": "A", "withdrawn": ["x"]},
    {"kind": "segment_opened", "agent_id": 5, "withdrawn": ["x"]},
]

_SUBAGENTS: list[tuple[str | None, str | None]] = [
    (None, None),
    ("", "T"),
    (None, "T"),
    ("A", None),
    ("A", ""),
    ("A", "T"),
    ("aé", "Explore"),
]


def _vocabulary() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in _EVENTS:
        text = json.dumps(event)
        try:
            settle: Any = rs.is_settlement(event)
        except TypeError as exc:
            settle = {"err": type(exc).__name__}
        out.append({"k": "settle", "e": text, "o": settle})
        out.append({"k": "agent", "e": text, "o": rs.agent_of(event)})
        out.append({"k": "back", "e": text, "o": rs.taken_back(event)})
    for agent_id, agent_type in _SUBAGENTS:
        sub = rs.Subagent.from_payload(agent_id, agent_type)
        out.append(
            {
                "k": "sub",
                "id": agent_id,
                "type": agent_type,
                "o": None
                if sub is None
                else {
                    "id": sub.agent_id,
                    "type": sub.agent_type,
                    "by": sub.found_by,
                    "fields": [list(p) for p in sub.fields().items()],
                },
            }
        )
    return out


# -- rebuild_session over scripted rows ----------------------------------------


def _brief(minute: int, ids: Any, **extra: Any) -> dict[str, Any]:
    return {"kind": "session_brief", "ts": _ts(minute), "ids": ids, **extra}


def _recall(minute: int, **extra: Any) -> dict[str, Any]:
    return {"kind": "prompt_recall", "ts": _ts(minute), **extra}


def _use(minute: int, ids: Any, **extra: Any) -> dict[str, Any]:
    return {"kind": "use", "ts": _ts(minute), "ids": ids, **extra}


def _opened(minute: int, **extra: Any) -> dict[str, Any]:
    return {"kind": "segment_opened", "ts": _ts(minute), **extra}


def _audited(minute: int, **extra: Any) -> dict[str, Any]:
    return {"kind": "turn_audited", "ts": _ts(minute), **extra}


def _review(minute: int, roots: Any, **extra: Any) -> dict[str, Any]:
    return {"kind": "review_unanswered", "ts": _ts(minute), "root_ids": roots, **extra}


OPENED_AT = "2026-01-01T12:05:00+00:00"

_CASES: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = [
    ("empty log", [], {}),
    ("only review rows", [_review(1, ["r1"]), _review(2, ["r2", 3])], {}),
    (
        "a brief then recalls",
        [
            _audited(0),
            _brief(1, ["a", "b"], event_id="E1"),
            _recall(2, shown_ids=["c"], event_id="E2"),
            _recall(3, shown_ids=["a"], event_id="E3"),
        ],
        {},
    ),
    (
        "a pre-recall prompt_recall with top_hits only",
        [
            _recall(1, top_hits=[{"id": "h1"}, {"id": "h2"}]),
            _recall(2, top_hits=[{"id": 5}]),
            _recall(3, top_hits=[]),
            _recall(4, top_hits=["h3"]),
            _recall(5, top_hits={"id": "h4"}),
            _recall(6),
        ],
        {},
    ),
    (
        "a shown_ids of None is present",
        [_recall(1, shown_ids=None, top_hits=[{"id": "h1"}])],
        {},
    ),
    (
        "a shown_ids string is one id",
        [_recall(1, shown_ids="s1"), _recall(2, shown_ids=""), _recall(3, shown_ids=4)],
        {},
    ),
    (
        "settlements and plain uses",
        [
            _use(0, ["p"]),
            _brief(1, ["a", "b"]),
            _use(2, ["a"], surface="brief"),
            _use(3, ["b"]),
            _use(4, ["z"], surface="recall"),
            _use(5, ["b"], surface="other"),
        ],
        {},
    ),
    ("a plain use does not start the entry", [_use(0, ["p"]), _audited(9)], {}),
    ("a settlement starts the entry", [_use(3, ["a"], surface="recall")], {}),
    (
        "a list surface on a use raises",
        [_brief(1, ["a"]), _use(2, ["a"], surface=["brief"])],
        {},
    ),
    (
        "a list surface on a use without ts is skipped first",
        [_brief(1, ["a"]), {"kind": "use", "ids": ["a"], "surface": ["brief"]}],
        {},
    ),
    (
        "a list surface on another kind is read",
        [_recall(1, shown_ids=["a"], surface=["brief"])],
        {},
    ),
    (
        "segments restart",
        [
            _brief(1, ["a"], segment=0),
            _recall(2, shown_ids=["b"], segment=0),
            _use(3, ["a"], surface="brief", segment=0),
            _recall(4, shown_ids=["c"], segment=1, marker="m1"),
            _recall(5, shown_ids=["d"], segment=1),
            _opened(6, segment=2),
        ],
        {},
    ),
    (
        "a bool segment opens nothing",
        [
            _brief(1, ["a"]),
            _recall(2, shown_ids=["b"], segment=True),
            _recall(3, shown_ids=["c"], segment=False),
            _recall(4, shown_ids=["d"], segment=1.0),
            _recall(5, shown_ids=["e"], segment="1"),
        ],
        {},
    ),
    (
        "segment numbers past i64",
        [
            _recall(1, shown_ids=["a"], segment=2**70),
            _recall(2, shown_ids=["b"], segment=2**70),
            _recall(3, shown_ids=["c"], segment=-(2**80)),
            _recall(4, shown_ids=["d"], segment=-(2**80)),
        ],
        {},
    ),
    (
        "a compact held then reopened",
        [
            _brief(1, ["a"], event_id="E1"),
            _recall(2, shown_ids=["b"], event_id="E2"),
            _use(3, ["a", "b"], surface="brief"),
            _brief(4, ["c"], event_id="E4", marker_absent=True),
            _recall(6, shown_ids=["d"], event_id="E6"),
            _use(7, ["d", "a"], surface="recall"),
            _opened(8, segment=1, marker="m1", opened_at=OPENED_AT),
        ],
        {},
    ),
    (
        "a compact held, not reopened",
        [
            _brief(1, ["a"], event_id="E1"),
            _recall(2, shown_ids=["b"], event_id="E2", marker_absent=True),
            _recall(3, shown_ids=["c"]),
        ],
        {},
    ),
    (
        "marker_absent must be True",
        [
            _brief(1, ["a"], marker_absent=1),
            _brief(2, ["b"], marker_absent="true"),
        ],
        {},
    ),
    (
        "opened_at without a signal restarts at opened_at",
        [
            _brief(1, ["a"]),
            _opened(8, segment=1, opened_at=OPENED_AT),
            _recall(9, shown_ids=["b"]),
        ],
        {},
    ),
    (
        "an unreadable opened_at restarts at the row",
        [
            _brief(1, ["a"], marker_absent=True),
            _recall(2, shown_ids=["b"]),
            _opened(8, segment=1, opened_at="yesterday"),
        ],
        {},
    ),
    (
        "an opened_at in another offset",
        [
            _brief(1, ["a"], marker_absent=True),
            _opened(8, segment=1, opened_at="2026-01-01T17:35:00+05:30"),
        ],
        {},
    ),
    (
        "a marker that is not a str keeps the entry's",
        [
            _recall(1, shown_ids=["a"], segment=1, marker="m1"),
            _recall(2, shown_ids=["b"], segment=2, marker=7),
            _recall(3, shown_ids=["c"], segment=3),
        ],
        {},
    ),
    (
        "a subagent's withdrawn ids",
        [
            _brief(1, ["a"], event_id="E1"),
            _brief(2, ["b", "c"], event_id="E2", marker_absent=True),
            _use(3, ["b"], surface="brief"),
            _opened(4, agent_id="A", withdrawn=["b", "c"], brief_event_id="E2"),
            _recall(5, shown_ids=["d"]),
        ],
        {},
    ),
    (
        "the subagent's scope reads its own rows",
        [
            _brief(1, ["a"], event_id="E1"),
            _opened(
                4,
                agent_id="A",
                withdrawn=["b", "c", 9],
                brief_event_id="E2",
                segment=1,
            ),
            _recall(5, shown_ids=["b", "e"], agent_id="A", event_id="E5"),
            _recall(6, shown_ids=["f"], agent_id="B"),
            _use(7, ["c"], surface="brief", agent_id="A"),
        ],
        {"agent": "A"},
    ),
    (
        "a withdrawn brief_event_id that is not a str",
        [_opened(1, agent_id="A", withdrawn=["x"], brief_event_id=4)],
        {"agent": "A"},
    ),
    (
        "a session segment_opened with withdrawn shows them",
        [_opened(1, withdrawn=["w1", "w2"], brief_event_id="EB")],
        {},
    ),
    (
        "a withdraw before the entry starts does nothing",
        [
            _opened(1, agent_id="A", withdrawn=["a"]),
            _brief(2, ["a"]),
        ],
        {},
    ),
    (
        "a withdraw on a row without ts still applies",
        [
            _brief(1, ["a", "b"], marker_absent=True),
            {"kind": "segment_opened", "agent_id": "A", "withdrawn": ["a"]},
        ],
        {},
    ),
    (
        "a withdrawn that is not a list is not taken back",
        [
            _brief(1, ["a", "b"], marker_absent=True),
            _opened(2, agent_id="A", withdrawn="a"),
        ],
        {},
    ),
    (
        "rows of another harness",
        [
            _brief(1, ["a"], harness="codex"),
            _brief(2, ["b"], harness=HARNESS),
            _brief(3, ["c"], harness=5),
            _brief(4, ["d"], harness=None),
            _review(5, ["r1"], harness="codex"),
        ],
        {},
    ),
    (
        "rows with no ts",
        [
            {"kind": "session_brief", "ids": ["a"]},
            {"kind": "session_brief", "ids": ["b"], "ts": "not a time"},
            {"kind": "session_brief", "ids": ["c"], "ts": 1767268800},
            _brief(4, ["d"]),
            {"kind": "review_unanswered", "root_ids": ["r1"]},
        ],
        {},
    ),
    (
        "a naive ts and an offset ts",
        [
            {"kind": "turn_audited", "ts": "2026-01-01T11:00:00"},
            {
                "kind": "prompt_recall",
                "ts": "2026-01-01T13:00:00+02:00",
                "shown_ids": ["a"],
            },
        ],
        {},
    ),
    (
        "review roots across segments",
        [
            _review(1, ["r1"]),
            _brief(2, ["a"], segment=0),
            _review(3, ["r2", "r1"]),
            _recall(4, shown_ids=["b"], segment=1),
            _review(5, "r3"),
            _review(6, ["r4"], agent_id="A"),
        ],
        {},
    ),
    (
        "shown_by keeps the first show",
        [
            _recall(1, shown_ids=["a", "b"], event_id="E1"),
            _brief(2, ["b", "a", "c"], event_id="E2"),
            _opened(3, withdrawn=["c", "d"], brief_event_id="E3"),
            _recall(4, shown_ids=["d", "e"], event_id=5),
        ],
        {},
    ),
    (
        "a turn_audited only stamps the start",
        [_audited(4, segment=3, marker="m9"), _audited(5)],
        {},
    ),
    (
        "a use carries its segment",
        [
            _brief(1, ["a"]),
            _use(2, ["a"], surface="brief", segment=1),
        ],
        {},
    ),
    (
        "unknown kinds count as recalls",
        [
            {"kind": "other", "ts": _ts(1), "shown_ids": ["a"]},
            {"kind": None, "ts": _ts(2)},
        ],
        {},
    ),
    (
        "the store fails after two rows",
        [_brief(1, ["a"]), _recall(2, shown_ids=["b"]), _recall(3, shown_ids=["c"])],
        {"fail": 2},
    ),
    (
        "the store fails after every row",
        [_brief(1, ["a"])],
        {"fail": 1},
    ),
    (
        "a raising row before the store fails",
        [_use(1, ["a"], surface={"x": 1}), _brief(2, ["b"])],
        {"fail": 2},
    ),
    (
        "the empty agent scope reads no row",
        [_brief(1, ["a"]), _brief(2, ["b"], agent_id="")],
        {"agent": ""},
    ),
]


def _rebuilds() -> list[dict[str, Any]]:
    out = []
    for name, rows, opts in _CASES:
        row: dict[str, Any] = {
            "k": "rebuild",
            "name": name,
            "rows": json.dumps(rows),
            "agent": opts.get("agent"),
            "o": _rebuild(rows, opts.get("agent"), opts.get("fail")),
        }
        if "fail" in opts:
            row["fail"] = opts["fail"]
        out.append(row)
    return out


# -- the sweep ------------------------------------------------------------------

_ABSENT = object()

_DRAWS: list[tuple[str, list[Any]]] = [
    ("harness", [_ABSENT, _ABSENT, HARNESS, "codex", 7]),
    (rs.AGENT_FIELD, [_ABSENT, _ABSENT, "A", "", "B"]),
    ("segment", [_ABSENT, _ABSENT, 0, 1, 2, True, 2**70]),
    ("marker", [_ABSENT, "m1", "m2", 5]),
    ("opened_at", [_ABSENT, _ABSENT, "2026-01-01T11:00:00+00:00", "bad"]),
    ("marker_absent", [_ABSENT, _ABSENT, True, 1]),
    ("event_id", [_ABSENT, "E1", "E2", 3]),
    ("ids", [_ABSENT, ["a", "b"], ["c"], "d", ["a", 5]]),
    ("shown_ids", [_ABSENT, _ABSENT, None, ["b"], "c"]),
    ("top_hits", [_ABSENT, [{"id": "a"}], [], [{"id": 4}], [{"id": "e"}, {"id": "f"}]]),
    ("surface", [_ABSENT, "brief", "recall", "other", "brief", ["x"]]),
    ("withdrawn", [_ABSENT, ["a"], ["b", "c"], "x"]),
    ("brief_event_id", [_ABSENT, "B1", 2]),
    ("root_ids", [_ABSENT, ["r1"], ["r2", "r1"]]),
]


def _draw_rows(rng: _Rng) -> tuple[list[dict[str, Any]], str | None]:
    """One sequence: the scope, the row count, then per row its kind, its
    ts and each of `_DRAWS` in order."""
    agent = rng.pick([None, "A"])
    count = rng.next() % 9
    rows = []
    for i in range(count):
        row: dict[str, Any] = {"kind": rng.pick(list(rs._STATE_KINDS))}
        ts = rng.pick([_ts(i), _ts(i), _ts(i), _ABSENT, "bad"])
        if ts is not _ABSENT:
            row["ts"] = ts
        for name, options in _DRAWS:
            value = rng.pick(options)
            if value is not _ABSENT:
                row[name] = value
        rows.append(row)
    return rows, agent


def _sweep() -> dict[str, Any]:
    rng = _Rng(SEED)
    blob = []
    for _ in range(SWEEP):
        rows, agent = _draw_rows(rng)
        answer = _rebuild(rows, agent)
        if "err" in answer:
            blob.append("!" + answer["err"])
        else:
            blob.append(dumps_line(answer))
    text = "".join(line + "\n" for line in blob)
    return {
        "k": "sweep",
        "seed": SEED,
        "count": SWEEP,
        "sha256": hashlib.sha256(text.encode("ascii")).hexdigest(),
    }


def generate() -> dict[str, bytes]:
    rows = _vocabulary() + _rebuilds() + [_sweep()]
    header = {
        "k": "consts",
        "rule": rs.RULE,
        "harness": rs.DEFAULT_HARNESS,
        "idle": rs.STATE_IDLE_SECONDS,
        "max": rs.STATE_MAX_SESSIONS,
        "kinds": list(rs._STATE_KINDS),
        "surfaces": [list(p) for p in rs._SURFACE_OF_KIND.items()],
        "settlement": sorted(rs._SETTLEMENT_SURFACES),
        "brief": rs.SURFACE_BRIEF,
        "recall": rs.SURFACE_RECALL,
        "agent_field": rs.AGENT_FIELD,
        "from_payload": rs.AGENT_FROM_PAYLOAD,
        "from_transcript": rs.AGENT_FROM_TRANSCRIPT,
        "withdrawn": rs.WITHDRAWN_FIELD,
        "review": rs.REVIEW_UNANSWERED,
        "key": [HARNESS, SESSION],
        "now": NOW,
        "n": len(rows),
    }
    return {"recall_state.jsonl": jsonl([header, *rows])}


# -- RecallState: scripted call sequences (``recall_state_held.jsonl``) ----------

T0 = "2026-03-01T09:30:00+00:00"
T0_IST = "2026-03-01T15:00:00+05:30"
HELD_SEED = 20261126
HELD_SWEEP = 5000
_K1 = (HARNESS, "S1")
_K2 = (HARNESS, "S2")
_K3 = (HARNESS, "S3")
_K4 = ("codex", "S1")
_KEYS = [_K1, _K2, _K3, _K4]


def _at(clock: int, base: str = T0) -> datetime:
    """`base` plus `clock` microseconds."""
    return datetime.fromisoformat(base) + timedelta(microseconds=clock)


class _HeldStore:
    """A store whose session read yields each session's rows, raises
    `sqlite3.OperationalError` after the first ``fail[session]`` rows, and
    first runs the calls ``during[session]`` names on the state, once (a
    hook of another thread while the read runs outside the lock)."""

    def __init__(self, spec: dict[str, Any], state: Any) -> None:
        self.rows = {s: json.loads(t) for s, t in spec.get("rows", {}).items()}
        self.fail = dict(spec.get("fail", {}))
        self.during = {s: list(c) for s, c in spec.get("during", {}).items()}
        self.state = state
        self.reads = 0

    def session_events_of_kinds(self, session: str, kinds: Any) -> Any:
        assert tuple(kinds) == rs._STATE_KINDS
        self.reads += 1
        for step in self.during.pop(session, []):
            _held_call(self.state, self, step)
        rows = self.rows.get(session, [])
        fail = self.fail.get(session)
        for i, row in enumerate(rows):
            if fail is not None and i == fail:
                raise sqlite3.OperationalError("disk I/O error")
            yield row
        if fail is not None and fail >= len(rows):
            raise sqlite3.OperationalError("disk I/O error")


def _marker(step: dict[str, Any], when: datetime | None) -> CompactMarker | None:
    """The step's compaction marker, read at `when` (a step naming a marker
    names its time)."""
    named = step.get("marker")
    if named is None or when is None:
        return None
    return CompactMarker(key=named, at=when)


def _held_call(state: Any, store: Any, step: dict[str, Any]) -> Any:
    """One call on the state, answered as its form."""
    op = step["op"]
    key = tuple(step["key"]) if "key" in step else None
    now = datetime.fromisoformat(step["now"]) if "now" in step else None
    at = datetime.fromisoformat(step["at"]) if "at" in step else None
    try:
        if op == "seg":
            return _view_form(state.segment(store, key, now=now))
        if op == "open":
            view, opened = state.open_segment(
                store, key, source=step["source"], now=now, marker=_marker(step, now)
            )
            return [_view_form(view), opened]
        if op == "pend":
            pending = state.pending_compaction(store, key, now=now)
            return None if pending is None else _iso(pending)
    except (TypeError, OverflowError, RecursionError, sqlite3.OperationalError) as exc:
        return {"err": type(exc).__name__}
    if op == "resolve":
        got = state.resolve_compaction(key, at=at, marker=_marker(step, at))
        return None if got is None else _view_form(got)
    if op == "attr":
        got = state.attribute_compaction(key, at=at)
        return None if got is None else [got[0], got[1]]
    if op == "take":
        got = state.take_closed(key)
        return None if got is None else _view_form(got)
    if op == "shown":
        state.note_shown(
            key,
            step["ids"],
            delivery=step["delivery"],
            now=now,
            surface=step.get("surface"),
            event_id=step.get("ev"),
        )
        return None
    if op == "claim":
        kept, segment = state.claim_delivery(
            key, step["ids"], now=now, event_id=step.get("ev")
        )
        return [kept, str(segment)]
    if op == "brief":
        ks, kw, segment = state.claim_brief(
            key, step["ids"], step["work"], now=now, event_id=step.get("ev")
        )
        return [ks, kw, str(segment)]
    if op == "forget":
        state.forget(key)
        return None
    if op == "settled":
        state.note_settled(key, step["ids"], now=now)
        return None
    if op == "roots":
        state.note_review_roots(key, step["ids"], now=now)
        return None
    if op == "len":
        return len(state)
    raise AssertionError(op)


def _held_form(entry: Any) -> dict[str, Any]:
    """A held entry whole: the fields of `_entry_form` but its views, and
    the segment it last left (``closed``)."""
    form = _entry_form(entry)
    for name in ("view", "closing", "kept"):
        del form[name]
    form["closed"] = None if entry.closed is None else _view_form(entry.closed)
    return form


def _new_state(spec: dict[str, Any]) -> Any:
    return rs.RecallState(
        max_sessions=spec.get("max", rs.STATE_MAX_SESSIONS),
        idle_seconds=spec.get("idle", rs.STATE_IDLE_SECONDS),
        agent_id=spec.get("agent"),
    )


def _run(spec: dict[str, Any], steps: list[dict[str, Any]]) -> tuple[list[Any], Any]:
    """Each step's answer, the keys after it and the store reads it made;
    then every held entry."""
    state = _new_state(spec)
    store = _HeldStore(spec, state)
    out = []
    for step in steps:
        before = store.reads
        answer = _held_call(state, store, step)
        out.append(
            {
                "o": answer,
                "keys": [list(k) for k in state._sessions],
                "reads": store.reads - before,
            }
        )
    end = [[list(k), _held_form(e)] for k, e in state._sessions.items()]
    return out, end


def _rows_text(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows)


_S1_ROWS = [
    _brief(1, ["a", "b"], event_id="E0", segment=0),
    _recall(2, shown_ids=["c"], event_id="E1"),
    _use(3, ["a"], surface="brief"),
    _review(4, ["r1"]),
]
_S3_ROWS = [
    _brief(1, ["a"], event_id="E1"),
    _brief(4, ["b", "c"], event_id="E4", marker_absent=True),
    _recall(5, shown_ids=["d"], event_id="E5", agent_id="A"),
    _opened(6, segment=1, marker="m1", agent_id="A"),
    _recall(7, shown_ids=["e"], event_id="E7"),
]
_STORE = {"S1": _rows_text(_S1_ROWS), "S3": _rows_text(_S3_ROWS)}
SIGNAL_AT = "2026-01-01T12:04:00+00:00"


def _k(key: tuple[str, str]) -> list[str]:
    return list(key)


def _t(seconds: float, base: str = T0) -> str:
    return _iso(_at(round(seconds * 1_000_000), base))


def _s(op: str, key: tuple[str, str] = _K1, **args: Any) -> dict[str, Any]:
    return {"op": op, "key": _k(key), **args}


_HELD_CASES: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = [
    (
        "clear opens the next segment on the entry's own marker",
        {
            "rows": {
                "S1": _rows_text(
                    [*_S1_ROWS, _recall(5, shown_ids=["d"], segment=2, marker="m1")]
                )
            }
        },
        [
            _s("seg", now=_t(0)),
            _s("shown", ids=["x"], delivery=True, now=_t(1)),
            _s("settled", ids=["x", "q"], now=_t(2)),
            _s("open", source="clear", now=_t(3), marker="m9"),
            _s("take"),
            _s("take"),
            _s("seg", now=_t(4)),
        ],
    ),
    (
        "compact with a new marker opens, with the same marker holds",
        {"rows": _STORE},
        [
            _s("open", source="compact", now=_t(0), marker="m1"),
            _s("take"),
            _s("claim", ids=["p"], now=_t(1), ev="E2"),
            _s("open", source="compact", now=_t(2), marker="m1"),
            _s("pend", now=_t(3)),
            _s("take"),
        ],
    ),
    (
        "compact without a marker holds the signal, resolved later",
        {"rows": _STORE},
        [
            _s("claim", ids=["p"], now=_t(0), ev="E2"),
            _s("open", source="compact", now=_t(1)),
            _s("pend", now=_t(2)),
            _s("claim", ids=["q", "a"], now=_t(3), ev="E3"),
            _s("shown", ids=["s"], delivery=False, now=_t(4), ev="B4"),
            _s("settled", ids=["q", "p"], now=_t(5)),
            _s("resolve", at=_t(1), marker="m2"),
            _s("take"),
            _s("resolve", at=_t(1), marker="m3"),
            _s("pend", now=_t(6)),
        ],
    ),
    (
        "the signal is matched by instant, not by wall time",
        {"rows": _STORE},
        [
            _s("open", source="compact", now=_t(0)),
            _s("resolve", at=_t(0, "2026-03-01T09:30:00+05:30"), marker="m2"),
            _s("resolve", at=_t(0.000001), marker="m2"),
            _s("resolve", at="2026-03-01T09:30:00", marker="m2"),
            _s("resolve", at=_t(0, T0_IST), marker="m2"),
            _s("take"),
        ],
    ),
    (
        "a resolve with no marker or the entry's own keeps the signal",
        {
            "rows": {
                "S1": _rows_text([_recall(1, shown_ids=["a"], segment=1, marker="m1")])
            }
        },
        [
            _s("open", source="compact", now=_t(0)),
            _s("resolve", at=_t(0)),
            _s("resolve", at=_t(0), marker="m1"),
            _s("pend", now=_t(1)),
            _s("resolve", key=_K2, at=_t(0), marker="m2"),
            _s("take"),
        ],
    ),
    (
        "a rebuilt signal attributed to a subagent",
        {"rows": _STORE},
        [
            _s("pend", key=_K3, now=_t(0)),
            _s("attr", key=_K3, at="2026-01-01T12:04:00.000001+00:00"),
            _s("attr", key=_K3, at="2026-01-01T17:34:00+05:30"),
            _s("attr", key=_K3, at=SIGNAL_AT),
            _s("pend", key=_K3, now=_t(1)),
            _s("seg", key=_K3, now=_t(2)),
        ],
    ),
    (
        "an id another surface showed under the brief's event id stays",
        {"rows": _STORE},
        [
            _s("pend", key=_K3, now=_t(0)),
            _s("shown", key=_K3, ids=["x"], delivery=True, now=_t(1), ev="E4"),
            _s("claim", key=_K3, ids=["y"], now=_t(2), ev="E4"),
            _s("attr", key=_K3, at=SIGNAL_AT),
            _s("seg", key=_K3, now=_t(3)),
        ],
    ),
    (
        "a live signal names no brief and withdraws nothing",
        {"rows": _STORE},
        [
            _s("shown", ids=["b2"], delivery=False, now=_t(0), ev="EB"),
            _s("open", source="compact", now=_t(1)),
            _s("attr", at=_t(1)),
            _s("attr", at=_t(1)),
            _s("attr", key=_K2, at=_t(1)),
            _s("seg", now=_t(2)),
        ],
    ),
    (
        "startup, resume and any other source keep the segment",
        {"rows": _STORE},
        [
            _s("open", source="startup", now=_t(0), marker="m1"),
            _s("open", source="resume", now=_t(1), marker="m2"),
            _s("open", source="", now=_t(2)),
            _s("open", source="Clear", now=_t(3)),
            _s("take"),
            _s("pend", now=_t(4)),
        ],
    ),
    (
        "the sweep keeps the cutoff and drops a microsecond past it",
        {"rows": _STORE, "idle": 60},
        [
            _s("seg", key=_K1, now=_t(0)),
            _s("seg", key=_K2, now=_t(0.000001)),
            _s("seg", key=_K3, now=_t(60)),
            _s("seg", key=_K3, now=_t(60.000001)),
            _s("seg", key=_K3, now=_t(60.000002)),
            _s("len"),
        ],
    ),
    (
        "an out-of-order clock leaves stale entries behind a live one",
        {"rows": _STORE, "idle": 60},
        [
            _s("seg", key=_K1, now=_t(100)),
            _s("seg", key=_K2, now=_t(0)),
            _s("seg", key=_K3, now=_t(150)),
            _s("seg", key=_K1, now=_t(150)),
            _s("seg", key=_K3, now=_t(150)),
            _s("seg", key=_K4, now=_t(170)),
        ],
    ),
    (
        "the sweep drops idle sessions only on a call that rebuilds or reads",
        {"rows": _STORE, "idle": 60},
        [
            _s("seg", key=_K1, now=_t(0)),
            _s("shown", key=_K2, ids=["a"], delivery=True, now=_t(500)),
            _s("claim", key=_K3, ids=["a"], now=_t(500)),
            _s("pend", key=_K2, now=_t(500)),
        ],
    ),
    (
        "the cap evicts the least recently seen",
        {"rows": _STORE, "max": 2},
        [
            _s("seg", key=_K1, now=_t(0)),
            _s("seg", key=_K2, now=_t(1)),
            _s("seg", key=_K1, now=_t(2)),
            _s("seg", key=_K3, now=_t(3)),
            _s("shown", key=_K4, ids=["z"], delivery=True, now=_t(4)),
            _s("roots", key=_K3, ids=["r9"], now=_t(5)),
            _s("seg", key=_K1, now=_t(6)),
        ],
    ),
    (
        "a cap of zero drops every entry and still answers",
        {"rows": _STORE, "max": 0},
        [
            _s("seg", now=_t(0)),
            _s("claim", ids=["a", "b"], now=_t(1), ev="E"),
            _s("open", source="clear", now=_t(2)),
            _s("take"),
            _s("len"),
        ],
    ),
    (
        "a forget then a rebuild",
        {"rows": _STORE},
        [
            _s("seg", now=_t(0)),
            _s("shown", ids=["x"], delivery=True, now=_t(1), ev="E9"),
            _s("forget"),
            _s("forget", key=_K2),
            _s("seg", now=_t(2)),
        ],
    ),
    (
        "a held session is never rebuilt by the calls that take no store",
        {"rows": _STORE},
        [
            _s("shown", ids=["x"], delivery=False, now=_t(0)),
            _s("seg", now=_t(1)),
            _s("settled", key=_K3, ids=["a"], now=_t(2)),
            _s("pend", key=_K3, now=_t(3)),
        ],
    ),
    (
        "a session made while the rebuild reads is kept and marked seen",
        {
            "rows": _STORE,
            "during": {
                "S1": [_s("shown", ids=["z"], delivery=True, now=_t(0), ev="EZ")]
            },
        },
        [
            _s("seg", key=_K2, now=_t(1)),
            _s("seg", now=_t(5)),
            _s("seg", now=_t(6)),
        ],
    ),
    (
        "a session forgotten while the rebuild reads is inserted",
        {
            "rows": _STORE,
            "during": {"S1": [_s("forget")]},
        },
        [
            _s("shown", ids=["z"], delivery=True, now=_t(0)),
            _s("forget"),
            _s("seg", now=_t(1)),
        ],
    ),
    (
        "claim_delivery keeps repeats and drops ids already shown",
        {"rows": _STORE},
        [
            _s("claim", ids=["a", "x", "x", "c", "y"], now=_t(0), ev="E1"),
            _s("claim", ids=["a", "x"], now=_t(1), ev="E2"),
            _s("claim", ids=[], now=_t(2), ev="E3"),
            _s("seg", now=_t(3)),
        ],
    ),
    (
        "claim_brief filters both lists before either is noted",
        {"rows": _STORE},
        [
            _s("brief", ids=["s", "a", "s"], work=["s", "w", "b"], now=_t(0), ev="B1"),
            _s("brief", ids=["s"], work=["w", "v"], now=_t(1)),
            _s("claim", ids=["v", "u"], now=_t(2)),
            _s("seg", now=_t(3)),
        ],
    ),
    (
        "note_shown: an empty surface falls through and the first show is kept",
        {"rows": _STORE},
        [
            _s("shown", ids=["p", "q"], delivery=True, now=_t(0), surface="", ev="E1"),
            _s("shown", ids=["q", "r"], delivery=False, now=_t(1), surface="", ev="E2"),
            _s("shown", ids=["s", "p"], delivery=False, now=_t(2), surface="other"),
            _s(
                "shown",
                ids=["t", "t"],
                delivery=True,
                now=_t(3),
                surface="brief",
                ev="E4",
            ),
            _s("shown", ids=[], delivery=True, now=_t(4)),
            _s("seg", now=_t(5)),
        ],
    ),
    (
        "settled and review roots are held",
        {"rows": _STORE},
        [
            _s("settled", ids=["a", "zz"], now=_t(0)),
            _s("roots", ids=["r2", "r1"], now=_t(1)),
            _s("open", source="clear", now=_t(2)),
            _s("take"),
            _s("seg", now=_t(3)),
        ],
    ),
    (
        "a subagent-scoped state reads the subagent's rows",
        {"rows": _STORE, "agent": "A"},
        [
            _s("seg", key=_K3, now=_t(0)),
            _s("seg", key=_K1, now=_t(1)),
            _s("open", key=_K3, source="compact", now=_t(2), marker="m1"),
            _s("open", key=_K3, source="compact", now=_t(3), marker="m2"),
            _s("take", key=_K3),
        ],
    ),
    (
        "the harness is part of the key",
        {"rows": {"S1": _rows_text([*_S1_ROWS, _brief(5, ["h"], harness="codex")])}},
        [
            _s("seg", key=_K1, now=_t(0)),
            _s("seg", key=_K4, now=_t(1)),
        ],
    ),
    (
        "a rebuild that raises leaves the sweep done and inserts nothing",
        {
            "rows": {
                **_STORE,
                "S2": _rows_text([_brief(1, ["a"]), _use(2, ["a"], surface=["x"])]),
            },
            "fail": {"S3": 1},
            "idle": 60,
        },
        [
            _s("seg", key=_K1, now=_t(0)),
            _s("seg", key=_K3, now=_t(61)),
            _s("seg", key=_K1, now=_t(62)),
            _s("seg", key=_K2, now=_t(200)),
            _s("open", key=_K3, source="clear", now=_t(201)),
            _s("pend", key=_K3, now=_t(202)),
            _s("shown", key=_K3, ids=["n"], delivery=True, now=_t(203)),
            _s("seg", key=_K3, now=_t(204)),
        ],
    ),
    (
        "a naive clock against aware entries raises in the sweep",
        {"rows": _STORE},
        [
            _s("seg", now=_t(0)),
            _s("seg", key=_K2, now="2026-03-01T09:31:00"),
            _s("shown", key=_K2, ids=["n"], delivery=True, now="2026-03-01T09:31:00"),
            _s("seg", key=_K3, now=_t(2)),
        ],
    ),
    (
        "a naive clock throughout orders by wall time",
        {"rows": _STORE, "idle": 60},
        [
            _s("shown", ids=["n"], delivery=True, now="2026-03-01T09:30:00"),
            _s("seg", key=_K2, now="2026-03-01T09:31:00"),
            _s("seg", key=_K3, now="2026-03-01T09:31:00.000001"),
        ],
    ),
    (
        "a cutoff before year 1 raises OverflowError on an empty table",
        {"rows": _STORE},
        [
            _s("seg", now="0001-01-01T12:00:00+00:00"),
            _s("shown", ids=["n"], delivery=True, now="0001-01-01T12:00:00+00:00"),
            _s("seg", now="0001-01-02T00:00:00+00:00"),
            _s("seg", now="0001-01-02T00:00:00+05:00"),
        ],
    ),
    (
        "a negative idle drops every session not seen after the future cutoff",
        {"rows": _STORE, "idle": -60},
        [
            _s("seg", key=_K1, now=_t(0)),
            _s("seg", key=_K2, now=_t(0)),
            _s("seg", key=_K2, now=_t(0)),
        ],
    ),
    (
        "a segment past i64 opens the next as an int of any size",
        {"rows": {"S1": _rows_text([_recall(1, shown_ids=["a"], segment=2**70)])}},
        [
            _s("open", source="clear", now=_t(0)),
            _s("claim", ids=["a"], now=_t(1)),
            _s("brief", ids=["b"], work=[], now=_t(2)),
        ],
    ),
]


def _held_cases() -> list[dict[str, Any]]:
    out = []
    for name, spec, steps in _HELD_CASES:
        answers, end = _run(spec, steps)
        state_spec = {k: v for k, v in spec.items() if k in ("max", "idle", "agent")}
        store_spec = {k: v for k, v in spec.items() if k in ("rows", "fail", "during")}
        out.append(
            {
                "k": "held",
                "name": name,
                "state": state_spec,
                "store": store_spec,
                "steps": [{**s, "r": a} for s, a in zip(steps, answers, strict=True)],
                "end": end,
            }
        )
    return out


# -- the sweep of call sequences --------------------------------------------------

_OPS = [
    "seg",
    "open",
    "open",
    "pend",
    "resolve",
    "attr",
    "take",
    "shown",
    "claim",
    "brief",
    "forget",
    "settled",
    "roots",
]
_STEPS_US = [0, 1, 30_000_000, 60_000_000, 60_000_001, -30_000_000, 3_600_000_000]
_IDS: list[list[str]] = [[], ["a"], ["a", "b"], ["b", "a", "b"], ["c"], ["e", "d"]]


def _draw_calls(rng: _Rng) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One sequence: the state's bounds and scope, the failing session,
    the call count, then per call its draws in a fixed order."""
    spec: dict[str, Any] = {
        "max": rng.pick([0, 1, 2, 3, 1000]),
        "idle": rng.pick([0, 60, 3600]),
        "agent": rng.pick([None, None, "A"]),
        "rows": _STORE,
    }
    fail = rng.pick([None, None, "S2", "S3"])
    if fail is not None:
        spec["fail"] = {fail: 0 if fail == "S2" else 3}
    count = rng.next() % 12
    clock = 0
    clocks: list[int] = []
    steps = []
    for _ in range(count):
        op = rng.pick(_OPS)
        key = rng.pick(_KEYS)
        clock += rng.pick(_STEPS_US)
        clocks.append(clock)
        base = rng.pick([T0, T0, T0_IST])
        source = rng.pick(["clear", "compact", "compact", "startup", "resume", ""])
        marker = rng.pick([None, None, "m1", "m2"])
        ids = rng.pick(_IDS)
        work = rng.pick(_IDS)
        ev = rng.pick([None, "E1", "E2"])
        surface = rng.pick([None, "", "brief", "recall", "other"])
        delivery = rng.pick([False, True])
        at_clock = clocks[rng.next() % len(clocks)] + rng.pick([0, 0, 1])
        at_base = rng.pick([T0, T0_IST])
        step: dict[str, Any] = {
            "op": op,
            "key": list(key),
            "now": _iso(_at(clock, base)),
        }
        if op == "open":
            step["source"] = source
        if op in ("open", "resolve") and marker is not None:
            step["marker"] = marker
        if op in ("resolve", "attr"):
            step["at"] = _iso(_at(at_clock, at_base))
        if op in ("shown", "claim", "brief", "settled", "roots"):
            step["ids"] = ids
        if op == "brief":
            step["work"] = work
        if op in ("shown", "claim", "brief") and ev is not None:
            step["ev"] = ev
        if op == "shown":
            step["delivery"] = delivery
            if surface is not None:
                step["surface"] = surface
        steps.append(step)
    return spec, steps


def _held_sweep() -> dict[str, Any]:
    rng = _Rng(HELD_SEED)
    lines: list[str] = []
    for _ in range(HELD_SWEEP):
        spec, steps = _draw_calls(rng)
        answers, end = _run(spec, steps)
        lines.extend(dumps_line(a) for a in answers)
        lines.append(dumps_line(end))
    text = "".join(line + "\n" for line in lines)
    return {
        "k": "sweep",
        "seed": HELD_SEED,
        "count": HELD_SWEEP,
        "sha256": hashlib.sha256(text.encode("ascii")).hexdigest(),
    }


def generate_held() -> dict[str, bytes]:
    rows = [*_held_cases(), _held_sweep()]
    header = {
        "k": "consts",
        "t0": T0,
        "t0_ist": T0_IST,
        "keys": [list(k) for k in _KEYS],
        "store": _STORE,
        "ops": _OPS,
        "steps_us": _STEPS_US,
        "ids": _IDS,
        "n": len(rows),
    }
    return {"recall_state_held.jsonl": jsonl([header, *rows])}
