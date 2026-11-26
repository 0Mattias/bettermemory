"""Fixtures: the write review's counters, unit review_counters-1a
(``review_counters_rows.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/review_counters.rs``. This pins it
to `review_counters`' tables (`ANSWER_WINDOW`, `REVIEW_GRADE`,
`REFUSAL_ACTIONS`, `DETECTORS`, `_STATUS_ACTION`, and `review`'s
`REVIEW_ISSUED` and `UNDECLARED`), its field readers (`_str`, `_ids`,
`_session_of`, `_bucket_value`, `_score`), the three row readers
(`IssuedRow.from_event` with `chain_key`, `DecidedRow.from_event` with
`committed` and `zero_attestation`, `GradeRow.from_event`) and
`is_review_row`.

The first line is a header: the count of rows, the constants, the base
events (``bases``, each neutral, `_common.neutral`) and the sweep's
pools. Then one row per case, its kind under ``k``:

- ``ev``: an event and ``r``, what every reader answers of it (`_answer`).
  The event is ``ev`` (neutral), ``json`` (text `json.loads` reads: a key
  repeated in it keeps its first place and its last value), or ``base``
  (a name in ``bases``) with ``set``, ``[key, neutral]`` pairs set in turn
  (an existing key keeps its place, a new one goes last), and ``drop``,
  keys removed;
- ``str``, ``ids``, ``score``: ``v`` (neutral) and ``r``, what `_str`,
  `_ids` or `_score` answers of it.

An answer is null for None, a text is itself when ASCII and ``{"cps":
[...]}`` otherwise, a datetime is its `isoformat()`, a float its `repr`,
an `int` its decimal text, and an exception ``{"x": class}``: the class
only, since OverflowError's text follows the release.
A `DecidedRow`'s scores are under ``un`` and ``se``: a line holding
``"secret"`` before a long text is what the credential detectors take
for a key (`test_no_fixture_line_is_shaped_like_a_key`).

The last line is a sweep: ``count`` events drawn with `_Draw` from
``seed``: for each field of ``pools`` in order, ``pick(len(pool) + 1)``
chooses a value, or no field when it is the last; the SHA-256 of the
`_answer` of each, as `_common.dumps_line` and a newline.

Not gated: the `fromisoformat` forms used here (a trailing ``Z``, a
whole offset, a space separator, a date alone, three fractional digits)
read the same under 3.11 to 3.14 on every system, `float(int)` rounds
the same everywhere, and every text written is ASCII or code points.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from bettermemory import review as rv
from bettermemory import review_counters as rc

from ._common import dumps_line, jsonl, neutral
from .gen_eval_model import _Draw

SWEEP_SEED = 20261125
SWEEP_COUNT = 10000
TS = "2026-11-21T10:00:00.123Z"
BIG = 10**400
# 2**1024 - 2**970: the least magnitude `float(int)` overflows at.
EDGE = 2**1024 - 2**970


def _t(s: str) -> Any:
    return s if s.isascii() else {"cps": [ord(c) for c in s]}


def _ot(s: str | None) -> Any:
    return None if s is None else _t(s)


def _dt(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat()


def _f(x: float | None) -> str | None:
    return None if x is None else repr(x)


def _try(fn: Any, spell: Any) -> Any:
    try:
        return spell(fn())
    except Exception as exc:  # the class is the answer
        return {"x": type(exc).__name__}


def _issued(row: rc.IssuedRow | None) -> Any:
    if row is None:
        return None
    return {
        "id": _t(row.review_id),
        "parent": _ot(row.parent_review_id),
        "ts": _dt(row.ts),
        "session": _ot(row.session),
        "client": _t(row.client),
        "model": _t(row.model),
        "related": [_t(s) for s in row.related],
        "pre": row.pre_answered,
        "chars": None if row.response_chars is None else str(row.response_chars),
        "roster": [_t(s) for s in row.roster],
        "chain": _t(row.chain_key),
    }


def _decided(row: rc.DecidedRow | None) -> Any:
    if row is None:
        return None
    return {
        "id": _t(row.review_id),
        "ts": _dt(row.ts),
        "session": _ot(row.session),
        "client": _t(row.client),
        "status": _t(row.status),
        "action": _t(row.action),
        "relation": [[_t(k), _t(v)] for k, v in row.relation.items()],
        "un": _f(row.unsafe),
        "se": _f(row.secret),
        "mem": _ot(row.memory_id),
        "links": [_t(s) for s in row.links],
        "upd": [_t(s) for s in row.update_suggested],
        "filings": [[_t(a), _t(b)] for a, b in row.filings],
        "cred": [_t(s) for s in row.credential_kinds],
        "matches": [_t(s) for s in row.matches],
        "committed": row.committed,
        "zero": row.zero_attestation,
    }


def _grade(row: rc.GradeRow | None) -> Any:
    if row is None:
        return None
    return {
        "id": _t(row.review_id),
        "ts": _dt(row.ts),
        "sample": _dt(row.sample_ts),
        "ra": row.right_answers,
        "rx": row.right_action,
        "pre": row.pre_answered,
    }


def _answer(ev: Any) -> dict[str, Any]:
    return {
        "issued": _try(lambda: rc.IssuedRow.from_event(ev), _issued),
        "decided": _try(lambda: rc.DecidedRow.from_event(ev), _decided),
        "grade": _try(lambda: rc.GradeRow.from_event(ev), _grade),
        "is": _try(lambda: rc.is_review_row(ev), lambda b: b),
        "session": _try(lambda: rc._session_of(ev), _ot),
        "client": _try(lambda: rc._bucket_value(ev, "client"), _t),
        "model": _try(lambda: rc._bucket_value(ev, "model"), _t),
    }


BASES: dict[str, dict[str, Any]] = {
    "issued": {
        "ts": TS,
        "kind": "review_issued",
        "review_id": "r1",
        "parent_review_id": "r0",
        "session": "s-row",
        "client": "c-row",
        "model": "m-row",
        "actor": {"session": "s-act", "client": "c-act", "model": "m-act"},
        "related": ["a", "", 3, "b"],
        "pre_answered": True,
        "response_chars": 120,
        "roster": ["x", "y"],
    },
    "write": {
        "ts": TS,
        "kind": "write",
        "review_id": "w1",
        "status": "committed",
        "review_action": "committed",
        "id": "mem-1",
        "session": "s-row",
        "client": "c-row",
        "actor": {"session": "s-act", "client": "c-act"},
        "review_answers": {
            "relation": {"a": "unrelated", "b": "update"},
            "unsafe": 0.0,
            "secret": 0.1,
        },
        "review_filings": [{"id": "t1", "detector": "review"}],
        "supersedes_reviewed": ["l1"],
        "update_suggested": ["u1"],
        "credential_kinds": ["aws"],
        "matches": ["m1"],
    },
    "grade": {
        "ts": TS,
        "kind": "review_grade",
        "review_id": "g1",
        "sample_ts": "2026-11-20T09:00:00Z",
        "right_answers": True,
        "right_action": False,
        "pre_answered": True,
    },
}


def _cases() -> list[dict[str, Any]]:
    """Each case: ``(base, set pairs, dropped keys)``, an event, or JSON."""
    out: list[dict[str, Any]] = []

    def b(base: str, *pairs: tuple[str, Any], drop: tuple[str, ...] = ()) -> None:
        out.append({"base": base, "set": list(pairs), "drop": list(drop)})

    def ev(value: Any) -> None:
        out.append({"ev": value})

    # IssuedRow: every guard and field.
    b("issued")
    b("issued", drop=("review_id",))
    for rid in ["", 5, None, ["r1"], "r\u00e9", "r\ud800"]:
        b("issued", ("review_id", rid))
    b("issued", drop=("ts",))
    for ts in [
        "garbage",
        "",
        1700000000,
        None,
        "2026-11-21T10:00:00Z",
        "2026-11-21T10:00:00+00:00",
        "2026-11-21T12:30:00+05:30",
        "2026-11-21T10:00:00.250-03:00",
        "2026-11-21 10:00:00",
        "2026-11-21",
        "2026-02-30T10:00:00Z",
    ]:
        b("issued", ("ts", ts))
    doors: list[Any] = [None, False, 0, "", "memory_update", [], {}]
    for door in doors:
        b("issued", ("door", door))
    b("issued", drop=("parent_review_id",))
    for parent in ["", 7, "r\u00e9"]:
        b("issued", ("parent_review_id", parent))
    b("issued", drop=("actor",))
    b("issued", drop=("actor", "session"))
    b("issued", drop=("actor", "session", "client", "model"))
    for actor in [
        {},
        {"session": ""},
        {"session": 5, "client": "", "model": None},
        {"client": "c-act"},
        ["s-act", "c-act"],
        None,
        "s-act",
        {"actor": {"session": "deep", "client": "deep"}},
    ]:
        b("issued", ("actor", actor))
        b("issued", ("actor", actor), drop=("session", "client", "model"))
    for client in ["", 5, None, "none", ["c"]]:
        b("issued", ("client", client), ("model", client))
    b("issued", ("client", ""), ("actor", {"client": ""}))
    for related in [("a", "b"), {"a": 1}, "ab", [], [["a"], "a", "a"]]:
        b("issued", ("related", related), ("roster", related))
    b("issued", drop=("related", "roster"))
    for pre in [1, "true", False, None, 1.0]:
        b("issued", ("pre_answered", pre))
    b("issued", drop=("pre_answered",))
    for chars in [True, False, 0, -5, 10**30, -(10**30), 3.0, "12", None]:
        b("issued", ("response_chars", chars))
    b("issued", drop=("response_chars",))

    # DecidedRow: status and action.
    b("write")
    b("write", drop=("status",))
    for status in ["", 3, None]:
        b("write", ("status", status))
    for status in [*rc._STATUS_ACTION, "other", "refused_secret"]:
        b("write", ("status", status), drop=("review_action",))
        b("write", ("status", status), ("review_action", ""))
        b("write", ("status", status), ("review_action", 5))
    b("write", ("status", "duplicate"), ("review_action", "committed"))
    b("write", ("review_action", "custom"))
    b("write", drop=("review_id",))
    b("write", ("ts", "nope"))
    # The answers.
    answer_cases: list[Any] = [
        [],
        "x",
        None,
        {},
        {"relation": []},
        {"relation": {"a": 5, "b": None, "c": ["x"], "d": "update"}},
        {"relation": dict([(1, "unrelated"), ("a", "update"), (True, "x")])},
        {"relation": {"a": "unrelated", "b": "unrelated"}, "unsafe": 0, "secret": 0},
        {"relation": {}, "unsafe": 0.0, "secret": -0.0},
        {"relation": {"a": "unrelated"}, "unsafe": -0.0, "secret": 0},
        {"relation": {"a": "restates"}, "unsafe": 0, "secret": 0},
        # One label unrelated and one not: every label must be unrelated.
        {"relation": {"a": "update", "b": "unrelated"}, "unsafe": 0, "secret": 0},
        {"unsafe": 0},
        {"unsafe": float("nan"), "secret": 0},
        {"unsafe": 0, "secret": None},
    ]
    for answers in answer_cases:
        b("write", ("review_answers", answers))
    for value in [
        True,
        False,
        0,
        1,
        -1,
        2**53 + 1,
        EDGE - 1,
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.0,
        5e-324,
        "0.1",
        None,
        [0],
    ]:
        b("write", ("review_answers", {"unsafe": value, "secret": value}))
    for value in [BIG, -BIG, EDGE, -EDGE]:
        b("write", ("review_answers", {"unsafe": value}))
        b("write", ("review_answers", {"unsafe": 0.5, "secret": value}))
    b("write", ("review_answers", {"unsafe": BIG}), ("review_id", ""))
    b("write", ("review_answers", {"unsafe": BIG}), drop=("status",))
    # The filings, the id and the id lists.
    for filings in [
        {"id": "t1", "detector": "review"},
        [],
        [
            5,
            ["t", "review"],
            {"id": "t2"},
            {"id": "", "detector": "review"},
            {"id": "t3", "detector": 5},
            {"id": "t4", "detector": "review-disagrees"},
            {"detector": "review", "id": "t5", "x": 1},
        ],
        ("t", "review"),
    ]:
        b("write", ("review_filings", filings))
    for mem in ["", 5, None]:
        b("write", ("id", mem))
    b("write", drop=("id",))
    for lst in [("a",), ["", "a", 1, "b", "a"], "a", None]:
        b(
            "write",
            ("supersedes_reviewed", lst),
            ("update_suggested", lst),
            ("credential_kinds", lst),
            ("matches", lst),
        )
    b("write", drop=("actor", "client"))
    b("write", ("actor", {"client": "c-act", "session": ""}), ("client", ""))

    # GradeRow.
    b("grade")
    for value in [1, 0, "true", None, 1.0]:
        b("grade", ("right_answers", value))
        b("grade", ("right_action", value))
    b("grade", drop=("right_answers",))
    b("grade", drop=("right_action",))
    for sample in ["x", 5, None, "2026-11-20T09:00:00+09:00"]:
        b("grade", ("sample_ts", sample))
    b("grade", drop=("sample_ts",))
    b("grade", ("pre_answered", 1))
    b("grade", ("right_answers", False), ("right_action", True), drop=("pre_answered",))

    # is_review_row.
    for kind in [
        "review_issued",
        "review_grade",
        "write",
        "Write",
        "",
        1,
        None,
        ["review_issued"],
        {"review_issued": 1},
        ("review_issued",),
    ]:
        ev({"kind": kind})
        ev({"kind": kind, "review_id": "x"})
    for rid in ["", 5, None, ["x"]]:
        ev({"kind": "write", "review_id": rid})
    ev({})

    # Events that are not dicts.
    for value in [[], ["kind", "write"], None, "write", 5, ("kind",)]:
        ev(value)

    # JSON with a key held twice.
    for text in [
        '{"ts":"2026-11-21T10:00:00Z","kind":"write","review_id":"w","status":"x",'
        '"review_answers":{"relation":{"a":"update","b":"unrelated","a":"unrelated"},'
        '"unsafe":0,"secret":0}}',
        '{"ts":"2026-11-21T10:00:00Z","kind":"write","review_id":"w","status":"x",'
        '"review_answers":{"relation":{"a":"update","b":"unrelated","a":5}}}',
        '{"ts":"2026-11-21T10:00:00Z","kind":"write","review_id":"w","status":"x",'
        '"review_answers":{"relation":{"a":5,"b":"unrelated","a":"update"}}}',
        '{"ts":"2026-11-21T10:00:00Z","kind":"review_issued","review_id":"x",'
        '"review_id":5,"door":false,"door":null}',
        '{"ts":"bad","kind":"review_grade","review_id":"g","right_answers":true,'
        '"right_action":true,"ts":"2026-11-21T10:00:00Z"}',
        '{"kind":"write","actor":{"client":"a","client":""},"client":"",'
        '"actor":{"client":"b"}}',
    ]:
        out.append({"json": text})
    return out


def _build(case: dict[str, Any]) -> Any:
    if "ev" in case:
        return case["ev"]
    if "json" in case:
        return json.loads(case["json"])
    event = copy.deepcopy(BASES[case["base"]])
    for key, value in case["set"]:
        event[key] = copy.deepcopy(value)
    for key in case["drop"]:
        del event[key]
    return event


def _spell_case(case: dict[str, Any]) -> dict[str, Any]:
    if "ev" in case:
        return {"ev": neutral(case["ev"])}
    if "json" in case:
        return {"json": case["json"]}
    out: dict[str, Any] = {"base": case["base"]}
    if case["set"]:
        out["set"] = [[k, neutral(v)] for k, v in case["set"]]
    if case["drop"]:
        out["drop"] = case["drop"]
    return out


IDS_POOL: list[Any] = [["a"], ["a", "", 3, "b"], "a", None, ("a",)]
POOLS: list[tuple[str, list[Any]]] = [
    ("kind", ["review_issued", "write", "review_grade", "other", ["write"], None]),
    (
        "ts",
        [
            "2026-11-21T10:00:00Z",
            "2026-11-21T10:00:00.250+00:00",
            "2026-11-21T12:30:00+05:30",
            "2026-11-21 10:00:00",
            "bad",
            5,
            "",
        ],
    ),
    ("review_id", ["r1", "r2", "", 7, ["r1"], "r\u00e9"]),
    ("parent_review_id", ["r0", "", 3]),
    ("door", [None, False, 0, "memory_update"]),
    ("session", ["s1", "", 4]),
    (
        "actor",
        [
            {"session": "s2", "client": "c2", "model": "m2"},
            {"session": "", "client": ""},
            {"client": 5, "model": "m3"},
            ["s"],
            None,
            {"actor": {"session": "deep"}},
        ],
    ),
    ("client", ["c1", "", 9]),
    ("model", ["m1", "", None]),
    ("related", [["a", "b"], ["", "a", 1], "a", []]),
    ("pre_answered", [True, False, 1, "yes"]),
    ("response_chars", [10, 0, True, 10**25, 1.5, "3"]),
    ("roster", [["x"], [], ("x",)]),
    (
        "status",
        [
            "committed",
            "credential_warning",
            "instruction_warning",
            "duplicate",
            "refused_standing",
            "other",
            "",
            5,
        ],
    ),
    ("review_action", ["committed", "refused_secret", "", None, 3]),
    (
        "review_answers",
        [
            {"relation": {"a": "unrelated"}, "unsafe": 0, "secret": 0.0},
            {"relation": {"a": "update", "b": 5}, "unsafe": 0.5, "secret": True},
            {"relation": [], "unsafe": float("nan"), "secret": -0.0},
            {"unsafe": BIG},
            "x",
            {"relation": {}, "unsafe": 0, "secret": 0},
            {
                "relation": {"a": "unrelated", "b": "unrelated"},
                "unsafe": -0.0,
                "secret": 0,
            },
        ],
    ),
    (
        "review_filings",
        [
            [{"id": "t", "detector": "review"}],
            [{"id": "t"}, 5, {"id": "u", "detector": "review-disagrees"}],
            {"id": "t"},
            [],
        ],
    ),
    ("id", ["mem-1", "", 2]),
    ("supersedes_reviewed", IDS_POOL),
    ("update_suggested", IDS_POOL),
    ("credential_kinds", IDS_POOL),
    ("matches", IDS_POOL),
    ("sample_ts", ["2026-11-20T09:00:00Z", "x", None]),
    ("right_answers", [True, False, 1, None]),
    ("right_action", [True, False, 0]),
]


def _sweep_event(draw: _Draw) -> dict[str, Any]:
    event: dict[str, Any] = {}
    for key, pool in POOLS:
        i = draw.pick(len(pool) + 1)
        if i < len(pool):
            event[key] = copy.deepcopy(pool[i])
    return event


def _sweep() -> dict[str, Any]:
    draw = _Draw(SWEEP_SEED)
    h = hashlib.sha256()
    for _ in range(SWEEP_COUNT):
        h.update((dumps_line(_answer(_sweep_event(draw))) + "\n").encode("ascii"))
    return {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "sha256": h.hexdigest(),
    }


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for case in _cases():
        rows.append({"k": "ev", **_spell_case(case), "r": _answer(_build(case))})
    for value in ["a", "", 5, None, ["a"], "\u00e9", True]:
        rows.append({"k": "str", "v": neutral(value), "r": _ot(rc._str(value))})
    for value in IDS_POOL + [[], {"a": 1}, [["a"], "a", "\ud800"], "ab"]:
        rows.append(
            {"k": "ids", "v": neutral(value), "r": [_t(s) for s in rc._ids(value)]}
        )
    for value in [
        True,
        False,
        0,
        1,
        -7,
        2**53 + 1,
        2**63,
        EDGE - 1,
        EDGE,
        -EDGE,
        BIG,
        0.25,
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.0,
        "1",
        None,
        [1],
        {"v": 1},
    ]:
        rows.append(
            {
                "k": "score",
                "v": neutral(value),
                "r": _try(lambda v=value: rc._score(v), _f),
            }
        )
    rows.append(_sweep())
    header = {
        "k": "header",
        "count": len(rows),
        "answer_window_us": rc.ANSWER_WINDOW // timedelta(microseconds=1),
        "review_grade": rc.REVIEW_GRADE,
        "review_issued": rv.REVIEW_ISSUED,
        "undeclared": rv.UNDECLARED,
        "refusal_actions": list(rc.REFUSAL_ACTIONS),
        "detectors": list(rc.DETECTORS),
        "status_action": [[k, v] for k, v in rc._STATUS_ACTION.items()],
        "bases": {name: neutral(ev) for name, ev in BASES.items()},
        "pools": [[key, [neutral(v) for v in pool]] for key, pool in POOLS],
    }
    return {"review_counters_rows.jsonl": jsonl([header, *rows])}
