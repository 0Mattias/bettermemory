"""Fixtures: the recall surface's counters (``recall_counters.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/recall_counters.rs``. This pins
`recall_counters`' public names (`is_surface_row`, `is_surface_show`,
`surface_of`, `shown_ids`, `session_of`, `compute_surface_report` and the
constants) and the private `_ids`, `_in_window` and `_attested`.

One file, not gated: every id, stamp, rule and worktree is ASCII, every
stamp carries an offset (or reads as UTC), and float reprs, set and dict
order, `statistics.median_low` and `TypeError` on an unhashable member
test are the same under 3.11 to 3.14 on every system. One JSON object per
line, keys sorted. A value the log cannot spell in JSON is tagged: a
tuple is ``{"$t": [...]}`` and an int outside the signed 64-bit range is
``{"$i": "<decimal>"}``. The first line is the header (``k`` ``consts``):
the constants, ``kinds`` (`_ROW_KINDS` sorted) and ``count`` (the lines
after it). Every other line is one of:

- ``{"k": "read", "ev": event, ...}``: the readers over one event:
  ``row`` (`is_surface_row`), ``show``, ``surface``, ``shown`` and
  ``session``. An exception is ``{"x": class}``.
- ``{"k": "ids", "v": value, "out": [...]}``: `_ids`.
- ``{"k": "att", "ev": event, "id": id, "out": bool}``: `_attested`.
- ``{"k": "win", "ts": text, "start": text, "end": text, "out": bool}``:
  `_in_window` of `parse_event_ts(ts)`, each bound `fromisoformat` of
  its text or None.
- ``{"k": "report", "name": ..., "ev": [...], "start", "end", "pick",
  "out"}``: `compute_surface_report`. ``pick`` is null (no pick) or
  ``{"in": [...], "raise": rule}``: a show is picked when its
  ``rule`` (None when absent) is in ``in``, and the pick raises
  `ValueError` on a show whose ``rule`` equals ``raise`` (null: never).
  ``out`` is null for None, ``{"x": class}`` for an exception, or the
  section's compact text (`json.dumps` with separators ``,`` and ``:``,
  key order kept), with each rate dict spelled ``[numerator,
  denominator]`` (the generator checks it is `RateCI.from_counts` of
  them) and ``note`` left out (the header pins it).
- ``{"k": "sweep", ...}``: ``count`` logs drawn from ``seed`` by `_Draw`
  out of the pools the row holds (``"~"``: the key is absent), each
  through `compute_surface_report` in the window ``windows[w]`` without
  a pick and with ``picks[p]``; ``sha256`` is the digest of the lines
  `_sweep_lines` spells, joined by newlines.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from bettermemory import recall_counters as rc
from bettermemory.eval_model import RateCI
from bettermemory.time_utils import parse_event_ts

from ._common import jsonl

_MASK = (1 << 64) - 1
SWEEP_SEED = 20261126
SWEEP_COUNT = 5000
ABSENT = "~"
RATE_KEYS = [
    "numerator",
    "denominator",
    "rate",
    "ci95_lower",
    "ci95_upper",
    "torn_read",
]


def _ts(minute: int, offset: str = "+00:00", second: int = 0) -> str:
    """A stamp at 12:`minute`:`second` UTC, spelled in `offset`."""
    sign = 1 if offset[0] == "+" else -1
    shift = sign * (int(offset[1:3]) * 60 + int(offset[4:6]))
    total = 12 * 60 + minute + shift
    return f"2026-01-01T{total // 60:02d}:{total % 60:02d}:{second:02d}{offset}"


def _enc(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        return value if -(1 << 63) <= value < (1 << 63) else {"$i": str(value)}
    if isinstance(value, tuple):
        return {"$t": [_enc(v) for v in value]}
    if isinstance(value, list):
        return [_enc(v) for v in value]
    assert isinstance(value, dict)
    return {k: _enc(v) for k, v in value.items()}


def _x(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # the class is the answer
        return {"x": type(exc).__name__}


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        if list(value) == RATE_KEYS:
            n, d = value["numerator"], value["denominator"]
            assert value == RateCI.from_counts(n, d).to_dict()
            return [n, d]
        return {k: _compact(v) for k, v in value.items() if k != "note"}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _dt(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _pick(spec: dict[str, Any] | None) -> Callable[[Any], bool] | None:
    if spec is None:
        return None

    def pick(row: Any) -> bool:
        rule = row.get("rule")
        if spec["raise"] is not None and rule == spec["raise"]:
            raise ValueError(rule)
        return rule in spec["in"]

    return pick


def _report(events: list[Any], start: Any, end: Any, pick: Any) -> Any:
    out = rc.compute_surface_report(
        events, start=_dt(start), end=_dt(end), pick=_pick(pick)
    )
    if out is None:
        return None
    assert out["note"] == rc.SILENT_MISS_NOTE
    return json.dumps(_compact(out), separators=(",", ":"))


def _report_row(
    name: str,
    events: list[Any],
    start: str | None = None,
    end: str | None = None,
    pick: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "k": "report",
        "name": name,
        "ev": _enc(events),
        "start": start,
        "end": end,
        "pick": pick,
        "out": _x(lambda: _report(events, start, end, pick)),
    }


def _read_row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "k": "read",
        "ev": _enc(event),
        "row": _x(lambda: rc.is_surface_row(event)),
        "show": rc.is_surface_show(event),
        "surface": rc.surface_of(event),
        "shown": rc.shown_ids(event),
        "session": rc.session_of(event),
    }


def _reads() -> list[dict[str, Any]]:
    kinds: list[Any] = sorted(rc._ROW_KINDS) + ["search_x", "", None, 1, 1.0, True]
    kinds += [("use",), ["use"], {"use": 1}, ("use", ["x"]), []]
    rows = [_read_row({"kind": k}) for k in kinds]
    rows.append(_read_row({"ts": _ts(1)}))
    for ids in ["m1", "", ["m1", "m1", "", 5, "m2", "m1"], ("m1",), {"m1": 1}, [], 7]:
        rows.append(_read_row({"kind": "session_brief", "ids": ids, "shown_ids": "z"}))
        rows.append(_read_row({"kind": "prompt_recall", "rule": "r", "shown_ids": ids}))
    for rule in ["", "r1", None, 5, ["r"]]:
        rows.append(_read_row({"kind": "prompt_recall", "rule": rule, "ids": "m"}))
    rows.append(_read_row({"kind": "prompt_recall", "shown_ids": ["m1"]}))
    rows.append(_read_row({"kind": "show", "shown_ids": ["m1"], "rule": "r"}))
    for session in ["s1", "", 0, False, None, [], ["s"], 5]:
        rows.append(_read_row({"kind": "use", "session": session, "session_id": "sid"}))
        rows.append(_read_row({"kind": "use", "session_id": session}))
    return rows


def _ids_rows() -> list[dict[str, Any]]:
    values: list[Any] = [
        None, "", "m1", ["m1", "m2", "m1", "", None, 3, ["m3"], "m3"], [], ("m1",),
        {"m1": "m2"}, 5, True, 0.5,
    ]  # fmt: skip
    return [{"k": "ids", "v": _enc(v), "out": rc._ids(v)} for v in values]


def _att_rows() -> list[dict[str, Any]]:
    base = {"outcome": "applied", "ids": ["m1", "m2"], "claim_excerpts": ["a", "b"]}
    variants: list[dict[str, Any]] = [
        {},
        {"outcome": "ignored"},
        {"outcome": None},
        {"auto": True},
        {"auto": 1},
        {"auto": "yes"},
        {"auto": False},
        {"attribution": "auto"},
        {"attribution": "explicit"},
        {"ids": []},
        {"ids": [], "memory_ids": ["m1"]},
        {"ids": None, "memory_ids": ("m1",)},
        {"ids": "m1"},
        {"ids": ("m1", "m2")},
        {"claim_excerpts": ["  \t", "b"]},
        {"claim_excerpts": ["　\x85", "b"]},
        {"claim_excerpts": [" x ", "b"]},
        {"claim_excerpts": [5, "b"]},
        {"claim_excerpts": []},
        {"claim_excerpts": ("a", "b")},
        {"claim_excerpts": None},
        {"ids": ["m2", "m1", "m1"], "claim_excerpts": ["a", "", "c"]},
        {"ids": ["m2", "m1"], "claim_excerpts": ["a"]},
        {"ids": [1, "m1"], "claim_excerpts": ["a", ""]},
    ]
    rows = []
    for variant in variants:
        event = {**base, **variant}
        for memory_id in ["m1", "m2", "m3"]:
            rows.append(
                {
                    "k": "att",
                    "ev": _enc(event),
                    "id": memory_id,
                    "out": rc._attested(event, memory_id),
                }
            )
    return rows


def _win_rows() -> list[dict[str, Any]]:
    a, b = "2026-01-01T12:01:00+00:00", "2026-01-01T12:03:00+00:00"
    stamps: list[Any] = [
        None, "bad", _ts(0), _ts(1), _ts(1, "+02:00"), _ts(2), _ts(3), _ts(3, "-05:30"),
        "2026-01-01T12:02:59.999999Z", "2026-01-01T12:03:00", 5,
    ]  # fmt: skip
    rows = []
    for ts in stamps:
        for start, end in [(None, None), (a, None), (None, b), (a, b), (b, a)]:
            out = rc._in_window(parse_event_ts(ts), _dt(start), _dt(end))
            rows.append({"k": "win", "ts": ts, "start": start, "end": end, "out": out})
    return rows


# -- hand-built logs -----------------------------------------------------------


def _brief(session: Any, minute: int, ids: Any, **more: Any) -> dict[str, Any]:
    return {
        "kind": "session_brief",
        "session": session,
        "ids": ids,
        "triggered_from": "session_hook",
        "ts": _ts(minute),
        **more,
    }


def _recall(
    session: Any, minute: int, ids: Any, rule: Any = "r1", **more: Any
) -> dict[str, Any]:
    return {
        "kind": "prompt_recall",
        "session": session,
        "shown_ids": ids,
        "rule": rule,
        "triggered_from": "prompt_hook",
        "ts": _ts(minute),
        **more,
    }


def _stop(session: Any, minute: int, **more: Any) -> dict[str, Any]:
    return {
        "kind": "turn_audited",
        "session": session,
        "triggered_from": "stop_hook",
        "ts": _ts(minute),
        **more,
    }


def _open(minute: int, memory_id: Any, **more: Any) -> dict[str, Any]:
    """A server's `show` (an open), under the server's own session."""
    return {
        "kind": "show",
        "session": "srv",
        "id": memory_id,
        "ts": _ts(minute),
        **more,
    }


def _use(
    minute: int, ids: Any, outcome: str = "applied", **more: Any
) -> dict[str, Any]:
    excerpts = ["x"] * len(ids) if isinstance(ids, list) else ["x"]
    return {
        "kind": "use",
        "session": "srv",
        "ids": ids,
        "outcome": outcome,
        "claim_excerpts": excerpts,
        "ts": _ts(minute),
        **more,
    }


WA = {"worktree_root": "/wt/a"}
WB = {"worktree_root": "/wt/b"}


def _logs() -> list[dict[str, Any]]:
    r = _report_row
    out = [
        r("empty", []),
        r("no surface rows", [{"kind": "search", "ts": _ts(1)}, {"kind": "x"}]),
        r(
            "non-dict events skipped",
            ["x", 5, None, ["kind", "use"], ("a",), _brief("c1", 1, ["m1"])],
        ),
    ]
    # Kinds: an unhashable kind raises before anything is read.
    raising: list[tuple[str, Any]] = [
        ("list", ["use"]),
        ("dict", {"use": 1}),
        ("tuple holding a list", ("use", ["x"])),
        ("empty list", []),
    ]
    for name, kind in raising:
        out.append(r(f"kind {name} raises", [_brief("c1", 1, ["m1"]), {"kind": kind}]))
    absent: list[tuple[str, Any]] = [
        ("tuple", ("use",)),
        ("int", 1),
        ("bool", True),
        ("none", None),
    ]
    for name, kind in absent:
        out.append(r(f"kind {name} absent", [{"kind": kind, "ts": _ts(1)}]))
    # What alone makes a section.
    alone: list[tuple[str, dict[str, Any]]] = [
        ("segment_opened", {"kind": "segment_opened", "session": "c1", "segment": 1}),
        ("review_unanswered", {"kind": "review_unanswered", "count": 2}),
        ("recall_grade", {"kind": "recall_grade"}),
        ("settlement", {"kind": "use", "surface": "brief", "ids": ["m1", "m1"]}),
        ("use other surface", {"kind": "use", "surface": "other", "ids": ["m1"]}),
        ("turn_audited", {"kind": "turn_audited", "session": "c1"}),
        ("brief without ids", {"kind": "session_brief", "session": "c1", "ids": []}),
        ("recall without rule", {"kind": "prompt_recall", "shown_ids": ["m1"]}),
        ("settlement surface list", {"kind": "use", "surface": ["brief"]}),
        ("settlement surface tuple", {"kind": "use", "surface": ("brief",)}),
    ]
    for name, row in alone:
        out.append(r(f"alone {name}", [{**row, "ts": _ts(2)}]))
        out.append(r(f"alone {name} outside", [{**row, "ts": _ts(2)}], start=_ts(3)))
    # The window: a show at start counts, one at end does not; a show with
    # no stamp counts only in the unbounded window.
    window_log = [
        _brief("c1", 1, ["m1"]),
        _brief("c1", 3, ["m2"]),
        _open(3, "m2", **WA),
        _brief("c1", 4, ["m3"], ts="bad"),
        {**_recall("c1", 4, ["m4"]), "ts": None},
        _open(5, "m1"),
    ]
    for start, end in [(None, None), (_ts(1), None), (None, _ts(3)), (_ts(1), _ts(3))]:
        out.append(r(f"window {start} {end}", window_log, start=start, end=end))
    out.append(
        r(
            "window in other offsets",
            window_log,
            start=_ts(1, "+05:00"),
            end=_ts(3, "-02:00"),
        )
    )
    out.extend(_join_logs())
    out.extend(_tally_logs())
    return out


def _join_logs() -> list[dict[str, Any]]:
    r = _report_row
    out = []
    # The hooks' own rows: a row recorded under the conversation's id.
    out.append(
        r(
            "own session",
            [
                _brief("c1", 1, ["m1", "m2", "m3"]),
                {**_open(2, "m1"), "session": "c1"},
                {**_use(3, ["m2"]), "session": "c1"},
                {**_use(3, ["m3"], "ignored"), "session": "c1"},
                {**_open(4, "m3"), "session": "c2"},
            ],
        )
    )
    # A /clear: the server keeps its stamp h_old; the new conversation's
    # hooks began after h_old's last, so the stamp joins the bridge.
    clear = [
        _brief("h_old", 1, ["m1"], **WA),
        _open(2, "m1", harness_session="h_old", **WA),
        _brief("h_new", 5, ["m2"], **WA),
        _open(6, "m2", harness_session="h_old", **WA),
        _use(7, ["m2"], harness_session="h_old", **WA),
    ]
    out.append(r("clear with a stamped server", clear))
    out.append(r("clear, window after", clear, start=_ts(5)))
    # Another window's server still runs beside: its stamp stays its own.
    beside = [
        _brief("h_b", 1, ["m2"], **WB),
        _brief("h_new", 5, ["m2"], **WA),
        _stop("h_b", 7, **WB),
        _open(8, "m2", harness_session="h_b", **WA),
        _open(9, "m2", harness_session="h_b"),
    ]
    out.append(r("stamp running beside the bridge", beside))
    out.append(r("stamp beside, end before its last hook", beside, end=_ts(7)))
    # A stamp whose conversation has no hook row is never beside.
    out.append(
        r(
            "stamp without hooks",
            [
                _brief("h_new", 1, ["m1"], **WA),
                _open(2, "m1", harness_session="ghost", **WA),
            ],
        )
    )
    # Two worktrees: each unstamped row joins its own worktree's bridge.
    out.append(
        r(
            "two worktrees",
            [
                _brief("c_a", 1, ["m1"], **WA),
                _brief("c_b", 2, ["m1"], **WB),
                _open(3, "m1", **WA),
                _use(4, ["m1"], **WB),
                _open(5, "m1"),
                _use(6, ["m1"], worktree_root=""),
            ],
        )
    )
    # The bridge never answers a stamped conversation.
    out.append(
        r(
            "stamped bridge dropped",
            [
                _brief("c1", 1, ["m1"], **WA),
                _open(2, "m1", harness_session="c1", session="other", **WB),
                _open(3, "m1", **WA),
                _use(4, ["m1"], **WA),
            ],
        )
    )
    # A stamp wins over the row's own session, even a conversation's.
    out.append(
        r(
            "stamp before own session",
            [
                _brief("c1", 1, ["m1"]),
                _brief("c2", 1, ["m1"]),
                {**_open(2, "m1"), "session": "c1", "harness_session": "c2"},
                {**_use(3, ["m1"]), "session_id": "c1", "session": ""},
            ],
        )
    )
    # Rows at and after `end` neither join nor move the bridge.
    after_end = [
        _brief("c1", 1, ["m1"], **WA),
        _open(2, "m1", **WA),
        _brief("c2", 3, ["m9"], **WA),
        _open(3, "m1", **WA),
        _use(4, ["m1"], **WA),
    ]
    out.append(r("rows at and after end", after_end, end=_ts(3)))
    out.append(r("rows at and after end, unbounded", after_end))
    # A Stop's turn_audited and a brief that printed nothing anchor too.
    out.append(
        r(
            "anchors",
            [
                _brief("c1", 1, ["m1"], **WA),
                _stop("c2", 2, **WA),
                _open(3, "m1", **WA),
                _brief("c1", 4, [], **WA),
                _use(5, ["m1"], **WA),
                {**_recall("c3", 6, ["m5"], rule=None), **WA},
                _open(7, "m1", **WA),
            ],
        )
    )
    # A hook row with an unhashable trigger raises in hook_spans, but only
    # inside the bounds; and before a raising pick.
    bad = [_brief("c1", 1, ["m1"]), _stop("c1", 4, triggered_from=["stop_hook"])]
    out.append(r("unhashable trigger", bad))
    out.append(r("unhashable trigger after end", bad, end=_ts(4)))
    out.append(
        r("unhashable trigger before pick raise", bad, pick={"in": [], "raise": None})
    )
    return out


def _o(minute: int, memory_id: Any, **more: Any) -> dict[str, Any]:
    """An open stamped for conversation c1."""
    return _open(minute, memory_id, **{"harness_session": "c1", **more})


def _u(minute: int, ids: Any, outcome: str = "applied", **more: Any) -> dict[str, Any]:
    """A use stamped for conversation c1."""
    return _use(minute, ids, outcome, **{"harness_session": "c1", **more})


def _tally_logs() -> list[dict[str, Any]]:
    r = _report_row
    out = []
    # Re-shows of one id at the same instant across surfaces: the later
    # position ends the earlier show's follow-ups at that instant.
    same = [
        _brief("c1", 2, ["m1", "m2"]),
        _recall("c1", 2, ["m1"]),
        _o(2, "m1"),
        _o(2, "m2"),
        _u(3, ["m1"]),
    ]
    out.append(r("re-show at the same instant", same))
    out.append(r("re-show, pick the brief", same, pick={"in": [None], "raise": None}))
    out.append(r("re-show, pick the recall", same, pick={"in": ["r1"], "raise": None}))
    out.append(r("re-show, pick none", same, pick={"in": [], "raise": None}))
    # The same instant in two offsets, and the recall logged first.
    out.append(
        r(
            "re-show, recall first",
            [
                {**_recall("c1", 2, ["m1"]), "ts": _ts(2, "+03:00")},
                _brief("c1", 2, ["m1"]),
                _o(2, "m1", ts=_ts(2, "-01:00")),
            ],
        )
    )
    # Re-shows at later instants split the follow-ups; another
    # conversation's show of the id bounds nothing here.
    later = [
        _brief("c1", 1, ["m1", "m2"]),
        _o(2, "m1"),
        _recall("c1", 3, ["m1"], rule="r2"),
        _recall("c2", 3, ["m2"], rule="r2"),
        _o(4, "m1"),
        _u(4, ["m2"], outcome="ignored"),
        _u(5, ["m1", "m2"]),
        _recall("c1", 6, ["m1"], rule="r1"),
    ]
    out.append(r("re-show later", later))
    out.append(r("re-show later, end at the third show", later, end=_ts(6)))
    out.append(r("re-show later, pick r2", later, pick={"in": ["r2"], "raise": None}))
    out.append(r("re-show later, start after the first", later, start=_ts(3)))
    # chars: ints only (not bool or float), any size, median_low.
    chars_values: list[Any] = [
        30,
        10,
        True,
        2.0,
        40,
        20,
        1 << 70,
        -(1 << 80),
        0,
        None,
        "5",
    ]
    out.append(
        r(
            "chars",
            [_recall("c1", 1, ["m1"], chars=c) for c in chars_values]
            + [_brief("c1", 1, ["m1"], chars=7), _brief("c1", 1, ["m1"], chars=9)],
        )
    )
    out.append(
        r("chars even", [_recall("c1", 1, ["m1"], chars=c) for c in [4, 1, 3, 2]])
    )
    # suppressed_ids, on the recall surface only, sorted by reason.
    suppressed: list[Any] = [
        [
            {"reason": "b"},
            {"reason": "a"},
            {"reason": "b"},
            "x",
            {"reason": 5},
            {"why": "c"},
        ],
        ({"reason": "c"},),
        "ab",
        {"reason": "d"},
        [],
        0,
        False,
        "",
        None,
    ]
    out.append(
        r(
            "suppressed",
            [_recall("c1", 1, ["m1"], suppressed_ids=s) for s in suppressed]
            + [_brief("c1", 1, ["m1"], suppressed_ids=[{"reason": "z"}])],
        )
    )
    for name, value in [("int", 3), ("float", 0.5), ("true", True), ("big", 1 << 70)]:
        out.append(
            r(
                f"suppressed {name} raises",
                [_recall("c1", 1, ["m1"], suppressed_ids=value)],
            )
        )
    out.append(
        r(
            "pick raises before suppressed",
            [
                _recall("c1", 1, ["m1"], suppressed_ids=3),
                _recall("c1", 2, ["m1"], rule="r9"),
            ],
            pick={"in": ["r1"], "raise": "r9"},
        )
    )
    # Uses: attested, auto, attribution, ignored.
    out.append(
        r(
            "uses",
            [
                _brief("c1", 1, ["m1", "m2", "m3", "m4", "m5", "m6"]),
                _u(2, ["m1"], auto=1),
                _u(2, ["m2"], auto=True),
                _u(2, ["m3"], attribution="auto"),
                _u(2, ["m4"], claim_excerpts=[" "]),
                _u(2, [], memory_ids=["m5"], claim_excerpts=["y"]),
                _u(2, "m6", outcome="ignored"),
                _u(2, "m6", claim_excerpts=["y"]),
            ],
        )
    )
    # Segments and sessions, rules sorted.
    out.append(
        r(
            "segments",
            [
                _brief("c1", 1, ["m1"], segment=1),
                _brief("c1", 1, ["m1"], segment=1, agent_id="a1"),
                _recall("c1", 1, ["m1"], rule="zz", segment=1, agent_id=""),
                _recall("c2", 1, ["m1"], rule="aa", segment=True),
                _recall("c2", 1, ["m1"], rule="", segment=0),
                _recall("c3", 1, ["m1"], rule="mm", segment=1.0),
                _recall("c3", 1, ["m1"], rule="mm", segment=1 << 70),
                {
                    "kind": "segment_opened",
                    "session": "c3",
                    "segment": 1 << 70,
                    "ts": _ts(1),
                },
                {"kind": "segment_opened", "session": "c4", "segment": 2, "rule": "bb"},
                {
                    "kind": "segment_opened",
                    "session": "c4",
                    "segment": -2,
                    "agent_id": "a",
                },
                {
                    "kind": "segment_opened",
                    "session_id": "c5",
                    "rule": "cc",
                    "ts": _ts(1),
                },
                {"kind": "segment_opened", "segment": 3, "rule": "dd", "ts": _ts(1)},
            ],
        )
    )
    # Settlements of each surface.
    out.append(
        r(
            "settlements",
            [
                {
                    "kind": "use",
                    "surface": "brief",
                    "ids": ["m1", "m2", "m1"],
                    "ts": _ts(1),
                },
                {"kind": "use", "surface": "recall", "ids": "m3", "ts": _ts(1)},
                {"kind": "use", "surface": "recall", "ids": ("m3",), "ts": _ts(1)},
                {
                    "kind": "use",
                    "surface": "recall",
                    "memory_ids": ["m3"],
                    "ts": _ts(1),
                },
                {"kind": "use", "surface": "BRIEF", "ids": ["m4"], "ts": _ts(1)},
                {"kind": "use", "surface": 1, "ids": ["m4"], "ts": _ts(1)},
                {"kind": "show", "surface": "brief", "id": ["m4"], "ts": _ts(1)},
            ],
        )
    )
    # review_unanswered: an int that is not a bool, of any size.
    counts: list[Any] = [
        1,
        2,
        True,
        2.5,
        "3",
        None,
        1 << 70,
        -(1 << 66),
        -(10**40),
        10**40 + 7,
    ]
    unanswered = [
        {"kind": "review_unanswered", "count": c, "ts": _ts(1)} for c in counts
    ]
    out.append(r("review_unanswered", unanswered))
    out.append(r("review_unanswered negative", unanswered[7:9]))
    out.append(r("review_unanswered cancel", [*unanswered[8:10], unanswered[8]]))
    # Grades: the latest per (sample, id), irrelevant over relevant in a row.
    grade = {"kind": "recall_grade", "ts": _ts(1)}
    out.append(
        r(
            "grades",
            [
                {
                    **grade,
                    "sample_id": "s1",
                    "stratum": "delivery",
                    "relevant_ids": ["m1", "m2"],
                },
                {
                    **grade,
                    "sample_id": "s1",
                    "stratum": "brief",
                    "irrelevant_ids": ["m1"],
                },
                {
                    **grade,
                    "sample_id": "s2",
                    "stratum": "delivery",
                    "relevant_ids": "m1",
                },
                {
                    **grade,
                    "sample_id": "s3",
                    "stratum": "silent",
                    "relevant_ids": ["m1"],
                    "irrelevant_ids": ["m1"],
                },
                {**grade, "sample_id": "", "stratum": "silent", "relevant_ids": ["m7"]},
                {
                    **grade,
                    "sample_id": "s4",
                    "stratum": "other",
                    "relevant_ids": ["m7"],
                },
                {
                    **grade,
                    "sample_id": "s4",
                    "stratum": ["brief"],
                    "relevant_ids": ["m7"],
                },
                {**grade, "sample_id": 4, "stratum": "brief", "relevant_ids": ["m7"]},
                {
                    **grade,
                    "sample_id": "s5",
                    "stratum": "brief",
                    "ts": _ts(9),
                    "relevant_ids": ["m7"],
                },
            ],
            end=_ts(5),
        )
    )
    # Sessions: falsy fall back to session_id; a truthy non-str is none.
    out.append(
        r(
            "falsy sessions",
            [
                _brief("", 1, ["m1"], session_id="c1"),
                _brief(0, 1, ["m2"], session_id="c2"),
                _brief(["x"], 1, ["m3"], session_id="c3"),
                _brief(None, 1, ["m4"]),
                {**_open(2, "m1"), "session": "c1"},
                {**_open(2, "m3"), "session": "c3"},
                {**_open(2, "m2"), "session": None, "session_id": "c2"},
            ],
        )
    )
    # Ids of the show and use rows.
    out.append(
        r(
            "row ids",
            [
                _brief("c1", 1, ["m1", "m2", "m1", ""]),
                {**_open(2, ["m1"]), "session": "c1"},
                {**_open(2, ("m2",)), "session": "c1"},
                {**_use(2, {"m2": 1}), "session": "c1"},
                {**_use(2, ["m2", "m2"]), "session": "c1", "claim_excerpts": ["", "x"]},
            ],
        )
    )
    return out


# -- the sweep ---------------------------------------------------------------


class _Draw:
    """`nxt` steps x = x * 6364136223846793005 + 1442695040888963407 mod
    2**64 and gives x >> 33; `pick(n)` is `nxt() % n`."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def nxt(self) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & _MASK
        return self.x >> 33

    def pick(self, n: int) -> int:
        return self.nxt() % n


A = ABSENT
# Field order of a drawn event, and each field's pool.
POOLS: list[tuple[str, list[Any]]] = [
    (
        "kind",
        [
            "session_brief",
            "session_brief",
            "prompt_recall",
            "prompt_recall",
            "turn_audited",
            "show",
            "show",
            "show",
            "use",
            "use",
            "use",
            "segment_opened",
            "review_unanswered",
            "recall_grade",
            "search",
        ],
    ),  # fmt: skip
    ("session", ["c1", "c1", "c2", "srv", "", A]),
    ("session_id", [A, A, A, "c1"]),
    ("harness_session", [A, A, "c1", "c1", "c2", "x"]),
    ("worktree_root", ["/wt/a", "/wt/a", "/wt/b", A]),
    ("triggered_from", [A, A, "session_hook", "stop_hook", "prompt_hook"]),
    (
        "ts",
        [_ts(m) for m in range(6)]
        + [_ts(2, "+02:00"), _ts(3, "-01:00"), "2026-01-01T12:04:00", "bad", A],
    ),
    ("ids", [A, ["m1"], ["m1"], ["m1", "m2"], "m2", ["m2", "m1", "m2"], []]),
    ("shown_ids", [A, ["m1"], ["m2"], ["m1", "m2"]]),
    ("id", [A, "m1", "m1", "m2"]),
    ("memory_ids", [A, A, ["m1"]]),
    ("rule", [A, "r1", "r1", "r2", ""]),
    ("outcome", [A, "applied", "applied", "applied", "ignored"]),
    ("auto", [A, A, A, A, A, True, 1]),
    ("attribution", [A, A, A, A, A, "auto"]),
    ("claim_excerpts", [A, ["x", "y"], ["x", "y"], ["x"], [" ", "y"], ["", "y"]]),
    ("surface", [A, A, A, A, A, A, A, A, "brief", "recall", "other", ["brief"]]),
    ("segment", [A, 1, 2, 0, True]),
    ("agent_id", [A, A, A, "a1"]),
    ("chars", [A, 10, 20, 35, True]),
    (
        "suppressed_ids",
        [A, A, A, [{"reason": "cap"}], [{"reason": "dup"}, {"reason": "cap"}], "ab"],
    ),
    ("count", [A, 1, 3, True, 1 << 70]),
    ("sample_id", [A, "s1", "s2"]),
    ("stratum", [A, "delivery", "brief", "silent", "other"]),
    ("relevant_ids", [A, ["m1"], ["m2"]]),
    ("irrelevant_ids", [A, A, ["m1"]]),
]
WINDOWS: list[list[str | None]] = [
    [None, None],
    [_ts(1), None],
    [None, _ts(4)],
    [_ts(1), _ts(4)],
]
PICKS: list[dict[str, Any]] = [
    {"in": ["r1"], "raise": None},
    {"in": [None, "r2"], "raise": None},
    {"in": [], "raise": None},
]


def _draw_log(d: _Draw) -> list[dict[str, Any]]:
    events = []
    for _ in range(3 + d.pick(14)):
        event: dict[str, Any] = {}
        for key, pool in POOLS:
            value = pool[d.pick(len(pool))]
            if value != ABSENT:
                event[key] = value
        events.append(event)
    return events


def _line(fn: Callable[[], Any]) -> str:
    try:
        out = fn()
    except Exception as exc:  # the class is the answer
        return "!" + type(exc).__name__
    return "~" if out is None else json.dumps(out)


def _sweep_lines() -> list[str]:
    """Per log ``i``: the window ``w`` and pick ``p`` drawn after the log,
    then ``i w=<w> -=<answer>`` and ``i w=<w> p=<p>=<answer>``, each
    answer `json.dumps` of the section, ``~`` for None, ``!`` and the
    class for an exception."""
    d = _Draw(SWEEP_SEED)
    lines: list[str] = []
    for i in range(SWEEP_COUNT):
        events = _draw_log(d)
        w = d.pick(len(WINDOWS))
        p = d.pick(len(PICKS))
        start, end = (_dt(t) for t in WINDOWS[w])
        for name, pick in (("-", None), (f"p={p}", _pick(PICKS[p]))):
            answer = _line(
                lambda: rc.compute_surface_report(
                    events, start=start, end=end, pick=pick
                )
            )
            lines.append(f"{i} w={w} {name}={answer}")
    return lines


def _sweep_row() -> dict[str, Any]:
    lines = _sweep_lines()
    return {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "pools": [[key, _enc(pool)] for key, pool in POOLS],
        "windows": WINDOWS,
        "picks": PICKS,
        "lines": len(lines),
        "sha256": hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest(),
    }


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = [
        *_reads(),
        *_ids_rows(),
        *_att_rows(),
        *_win_rows(),
        *_logs(),
        _sweep_row(),
    ]
    header = {
        "k": "consts",
        "recall_grade": rc.RECALL_GRADE,
        "strata": list(rc.STRATA),
        "surfaces": list(rc.SURFACES),
        "note": rc.SILENT_MISS_NOTE,
        "kinds": sorted(rc._ROW_KINDS),
        "count": len(rows),
    }
    return {"recall_counters.jsonl": jsonl([header, *rows])}
