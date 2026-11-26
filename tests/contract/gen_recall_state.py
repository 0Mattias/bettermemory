"""Fixtures: the recall surface's per-session state (``recall_state.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/recall_state.rs``. This pins
`recall_state`'s vocabulary (`is_settlement`, `agent_of`, `taken_back`,
`Subagent.from_payload` and `fields`), and `rebuild_session` over scripted
rows, with `_replay`, `_restart`, `_reopen`, `_hold_signal`, `_withdraw`,
`_view` and `_closing_view` through it.

One file, not gated: no row depends on the Python release or the
platform (no kind is nested deep enough for `str()` to raise). One JSON
object per line, keys sorted, ASCII. Its first line is the header
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
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from bettermemory import recall_state as rs

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
