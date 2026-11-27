"""Fixtures: the write review's counters, unit review_counters-1a
(``review_counters_rows.jsonl``) and unit review_counters-1b
(``review_counters_chains.jsonl``, `generate_chains`, described above it).

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
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# review_counters-1b: the chains (``review_counters_chains.jsonl``)
# ---------------------------------------------------------------------------
#
# The header holds ``logs``: each a list of events, an event a JSON object,
# or a JSON text (`json.loads` reads it: the one way to hold an `int` past
# the float range). Then one row per case, its kind under ``k``:
#
# - ``chains``: `build_chains(logs[log], now=now, horizon=horizon)`;
# - ``window``: `chains_in_window(logs[log], start=, end=, now=)`;
# - ``tally``: a `_Tally` that adds every chain of that window, its
#   `response_chars` set to ``chars`` (`int` texts), and its `to_dict()`;
# - ``rate``: a `_Tally` with ``committed`` commits, ``filings`` counts and
#   ``calls`` committed calls: `filings_per_100()` and `to_dict()`'s
#   `calls_per_committed_write`;
# - ``share``: `_share(n, d)`; ``median``: `_median` of ``v`` (`int`
#   texts); ``week``: `_iso_week(datetime.fromisoformat(ts))`.
#
# A chain is spelled by `_chain`; a dict as ``[[key, value], ...]`` in its
# order; a float as its `repr`; an exception as ``{"x": class}``. Times
# are written as `isoformat()`. The last line is a sweep: ``count`` logs
# drawn by `_chain_log` from ``seed``, the SHA-256 of `_sweep_answer` of
# each as `_common.dumps_line` and a newline.
#
# Not gated: every text is ASCII, every time an aware `fromisoformat` form
# 3.11 to 3.14 read alike, the floats are correctly rounded quotients and
# `round` on them, alike everywhere, and the overflows are datetime's range
# and `float(int)`'s, not the interpreter's recursion.

CHAINS_SEED = 20261126
CHAINS_COUNT = 5000
_T0 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
_EDGE = 2**1024 - 2**970


def _at(minutes: float = 0, us: int = 0) -> str:
    return (_T0 + timedelta(minutes=minutes, microseconds=us)).isoformat()


def _iss(
    rid: str,
    ts: str,
    *,
    parent: str | None = None,
    session: str = "s1",
    client: str = "c1",
    related: tuple[str, ...] = (),
    pre: bool = False,
    roster: tuple[str, ...] = (),
) -> dict[str, Any]:
    ev: dict[str, Any] = {"kind": "review_issued", "review_id": rid, "ts": ts}
    if parent is not None:
        ev["parent_review_id"] = parent
    ev.update(session=session, client=client, model="m-" + client)
    if related:
        ev["related"] = list(related)
    if pre:
        ev["pre_answered"] = True
    if roster:
        ev["roster"] = list(roster)
    return ev


def _dec(
    rid: str,
    ts: str,
    action: str = "committed",
    *,
    relation: dict[str, str] | None = None,
    session: str = "s1",
    client: str = "c1",
    scores: tuple[int, int] | None = None,
    links: tuple[str, ...] = (),
    upd: tuple[str, ...] = (),
    filings: tuple[str, ...] = (),
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "kind": "write",
        "review_id": rid,
        "ts": ts,
        "status": "committed",
        "review_action": action,
        "session": session,
        "client": client,
    }
    answers: dict[str, Any] = {}
    if relation is not None:
        answers["relation"] = relation
    if scores is not None:
        answers["unsafe"], answers["secret"] = scores
    if answers:
        ev["review_answers"] = answers
    if links:
        ev["supersedes_reviewed"] = list(links)
    if upd:
        ev["update_suggested"] = list(upd)
    if filings:
        ev["review_filings"] = [{"id": "t", "detector": d} for d in filings]
    return ev


def _close_log(**over: Any) -> list[Any]:
    """An open chain `a` (related x, y) and a closing call `z`."""
    dec_at = over.pop("dec_at", _at(10))
    relation = over.pop("relation", {"x": "unrelated", "y": "update", "w": "a"})
    z = {"session": "s1", "client": "c1", **over}
    return [
        _iss("a", _at(0), related=("x", "y")),
        _iss("z", _at(1), pre=True, session=z["session"], client=z["client"]),
        _dec("z", dec_at, relation=relation, session="s9", client=z["client"]),
    ]


_LATE = "9999-12-31T23:45:00+00:00"

CHAIN_LOGS: dict[str, list[Any]] = {
    "empty": [],
    "window_edge": [
        _iss("a", _at(0)),
        _dec("a", _at(30)),
        _iss("b", _at(1)),
        _dec("b", _at(31, 1)),
    ],
    "orphan_parent": [
        _iss("f", _at(0), parent="p"),
        _dec("p", _at(5)),
        _iss("g", _at(1), parent="q"),
        _dec("g", _at(2), "refused_secret"),
        _dec("nobody", _at(3)),
    ],
    "standing_retry": [
        _iss("a", _at(0)),
        _dec("a", _at(5), "refused_standing"),
        _dec("a", _at(10)),
    ],
    "two_retries": [
        _iss("a", _at(0)),
        _dec("a", _at(5), "refused_standing"),
        _dec("a", _at(10), "refused_standing"),
        _dec("a", _at(15)),
        _dec("a", _at(20), "refused_unsafe"),
    ],
    "refusal_then_standing": [
        _iss("a", _at(0)),
        _dec("a", _at(5), "refused_secret"),
        _dec("a", _at(10), "refused_standing"),
    ],
    "standing_left": [
        _iss("a", _at(0)),
        _dec("a", _at(5), "refused_standing"),
        _dec("a", _at(40)),
    ],
    "standing_reissued": [
        _iss("a", _at(0)),
        _dec("a", _at(5), "refused_standing"),
        _iss("a2", _at(35), parent="a"),
        _dec("a2", _at(40)),
    ],
    "last_before": [
        _iss("r", _at(0)),
        _iss("r2", _at(20), parent="r"),
        _iss("r3", _at(60), parent="r"),
        _dec("r", _at(55)),
        _iss("q", _at(1)),
        _iss("q2", _at(20), parent="q"),
        _iss("q3", _at(70), parent="q"),
        _dec("q3", _at(45)),
    ],
    "close_match": _close_log(),
    "close_session": _close_log(session="s2"),
    "close_client": _close_log(client="c2"),
    "close_related": _close_log(relation={"x": "unrelated"}),
    "close_values": _close_log(relation={"k1": "x", "k2": "y"}),
    "close_at_window": _close_log(dec_at=_at(30)),
    "close_past_window": _close_log(dec_at=_at(30, 1)),
    "close_same_instant": _close_log(dec_at=_at(0)),
    "close_refused": [
        _iss("a", _at(0)),
        _iss("z", _at(1), pre=True),
        _dec("z", _at(2), "refused_secret"),
    ],
    "closer_order": [
        _iss("a", _at(0), related=("x",)),
        _iss("z2", _at(1), pre=True),
        _iss("z1", _at(2), pre=True),
        _dec("z1", _at(5), relation={"x": "unrelated"}),
        _dec("z2", _at(4), relation={}),
        _dec("z2", _at(8), relation={"x": "unrelated"}),
    ],
    "two_open": [
        _iss("a", _at(0)),
        _iss("b", _at(1)),
        _iss("z", _at(2), pre=True),
        _dec("z", _at(5)),
    ],
    "two_closers_two_open": [
        _iss("a", _at(0)),
        _iss("b", _at(1)),
        _iss("z1", _at(2), pre=True),
        _iss("z2", _at(3), pre=True),
        _dec("z1", _at(5)),
        _dec("z2", _at(6)),
    ],
    "roster": [
        _iss("a", _at(0), roster=("m1",)),
        _iss("z", _at(1), pre=True),
        _dec("z", _at(5)),
    ],
    "roster_follow_up": [
        _iss("a", _at(0), roster=("m1",)),
        _iss("a2", _at(2), parent="a"),
        _iss("z", _at(3), pre=True),
        _dec("z", _at(5)),
    ],
    "pending": [_iss("a", _at(0)), _iss("b", _at(10)), _iss("b2", _at(20), parent="b")],
    "horizon": [
        _iss("a", _at(0)),
        _dec("a", _at(20)),
        _iss("b", _at(15)),
        _dec("c", _at(5)),
        _iss("c", _at(1)),
    ],
    "equal_instants": [
        _iss("b", "2026-11-21T11:00:00+01:00"),
        _iss("a", "2026-11-21T10:00:00Z"),
        _iss("c", "2026-11-21T09:00:00-01:00"),
        _iss("d", "2026-11-21T10:05:00Z"),
        _dec("d", "2026-11-21T10:10:00+00:00", "refused_standing"),
        _dec("d", "2026-11-21T11:10:00+01:00"),
        _iss("e", "2026-11-21T10:06:00Z"),
        _dec("e", "2026-11-21T11:10:00+01:00"),
        _dec("e", "2026-11-21T10:10:00+00:00", "refused_standing"),
    ],
    "reroot": [
        _iss("a", _at(0)),
        _iss("b", _at(1), parent="a"),
        _iss("a", _at(2), parent="x"),
        _dec("a", _at(3)),
        _dec("b", _at(4), "refused_unsafe"),
    ],
    "window_roots": [
        _iss("a", _at(0)),
        _iss("b", _at(10)),
        _iss("c", _at(20)),
        _dec("c", _at(50)),
        _iss("d", _at(19)),
        _dec("d", _at(50, 1)),
        _iss("z", _at(15), pre=True),
        _dec("z", _at(16)),
    ],
    "late": [_iss("a", _LATE)],
    "late_offset": [_iss("a", "9999-12-31T23:45:00+05:00")],
    "late_answered": [_iss("a", _LATE), _dec("a", "9999-12-31T23:50:00Z")],
    "late_close": [
        _iss("a", "9999-12-31T23:40:00Z"),
        _iss("z", "9999-12-31T23:41:00Z", pre=True),
        _dec("z", "9999-12-31T23:50:00Z"),
    ],
    "late_close_same": [
        _iss("a", "9999-12-31T23:40:00Z"),
        _iss("z", "9999-12-31T23:41:00Z", pre=True),
        _dec("z", "9999-12-31T23:40:00Z"),
        _dec("a", "9999-12-31T23:41:00Z"),
    ],
    "early": [
        _iss("a", "0001-01-01T00:00:00+00:00"),
        _iss("b", "0001-01-01T00:30:00+01:00"),
        _dec("b", "0001-01-01T00:10:00Z"),
    ],
    "huge_score": [
        _iss("a", _at(0)),
        '{"kind": "write", "review_id": "a", "ts": "2027-01-01T00:00:00Z", '
        '"status": "committed", "review_answers": {"unsafe": ' + str(_EDGE) + "}}",
    ],
    "not_dicts": [
        [1],
        "a",
        None,
        _iss("a", _at(0)),
        {"kind": 5, "review_id": "a", "ts": _at(1)},
        {"kind": "write", "review_id": 5, "ts": _at(1), "status": "committed"},
        {"kind": "write", "review_id": "a", "ts": _at(1)},
        {"kind": "review_issued", "review_id": "b", "ts": _at(2), "door": False},
    ],
    "tally_mix": [
        _iss("c1", _at(0), related=("x",)),
        _dec(
            "c1",
            _at(1),
            links=("l1", "l2"),
            upd=("u",),
            filings=("review", "zz", "total", "zz"),
        ),
        _iss("c2", _at(1)),
        _dec("c2", _at(2), "refused_secret"),
        _iss("c3", _at(2)),
        _dec("c3", _at(3), "refused_standing"),
        _iss("c4", _at(3)),
        _dec("c4", _at(4), "odd"),
        _iss("c5", _at(4)),
        _dec("c5", _at(5), "refused_standing"),
        _dec("c5", _at(6), "restates"),
        _iss("c6", _at(5), pre=True),
        _dec(
            "c6",
            _at(6),
            relation={"q": "unrelated"},
            scores=(0, 0),
            filings=("review-disagrees",),
        ),
        _iss("c7", _at(6), pre=True),
        _dec("c7", _at(7), relation={"q": "update"}, scores=(0, 0)),
        _iss("c8", _at(7), session="s2"),
        _iss("c8b", _at(8), parent="c8", session="s2", related=("x",)),
        _iss("c9", _at(9), pre=True, session="s2"),
        _dec("c9", _at(10), relation={"x": "unrelated"}, scores=(0, 0)),
        _iss("c10", _at(10)),
        _iss("c10b", _at(11), parent="c10"),
        _iss("c11", _at(40)),
        _iss("c12", _at(41), pre=True),
    ],
}


def _cdt(dt: datetime) -> str:
    return dt.isoformat()


def _chain(c: rc.ReviewChain) -> dict[str, Any]:
    d = c.decided
    return {
        "key": c.key,
        "ids": c.review_ids,
        "rel": c.related,
        "rows": [[r.review_id, _cdt(r.ts)] for r in c.issued],
        "state": c.state,
        "closed": c.closed,
        "absorbed": c.absorbed,
        "retries": c.standing_retries,
        "calls": c.calls,
        "d": None if d is None else [d.review_id, _cdt(d.ts), d.action],
        "out": c.outcome,
        "session": c.session,
        "client": c.client,
        "model": c.model,
        "pre": c.pre_answered,
    }


def _spell(value: Any) -> Any:
    if isinstance(value, dict):
        return [[k, _spell(v)] for k, v in value.items()]
    if isinstance(value, float):
        return repr(value)
    return value


def _chains_of(fn: Any) -> Any:
    try:
        return [_chain(c) for c in fn()]
    except Exception as exc:  # the class is the answer
        return {"x": type(exc).__name__}


def _iso(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _events(log: list[Any]) -> list[Any]:
    return [
        json.loads(e) if isinstance(e, str) and e.startswith("{") else e for e in log
    ]


def _window_tally(
    events: list[Any], start: Any, end: Any, now: Any, chars: list[int]
) -> Any:
    def run() -> Any:
        tally = rc._Tally()
        for chain in rc.chains_in_window(events, start=start, end=end, now=now):
            tally.add(chain)
        tally.response_chars.extend(chars)
        return _spell(tally.to_dict())

    return _try(run, lambda v: v)


def _chain_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now = _at(60)
    plain = [
        "empty", "window_edge", "orphan_parent", "standing_retry", "two_retries",
        "refusal_then_standing", "standing_left", "standing_reissued", "last_before",
        "close_match", "close_session", "close_client", "close_related", "close_values",
        "close_at_window", "close_past_window", "close_same_instant", "close_refused",
        "closer_order", "two_open", "two_closers_two_open", "roster", "roster_follow_up",
        "equal_instants", "reroot", "window_roots", "late", "late_offset", "late_answered",
        "late_close", "late_close_same", "early", "huge_score", "not_dicts", "tally_mix",
    ]  # fmt: skip
    calls: list[tuple[str, str, str | None]] = [(name, now, None) for name in plain]
    calls += [
        ("window_edge", _at(29), None),
        ("pending", _at(30), None),
        ("pending", _at(30, -1), None),
        ("pending", _at(50), None),
        ("pending", _at(50, -1), None),
        ("horizon", now, _at(10)),
        ("horizon", now, _at(20)),
        ("horizon", now, _at(15, -1)),
        ("close_at_window", now, _at(29)),
        ("early", "0001-01-01T00:20:00+00:00", None),
        ("late", "9999-12-31T23:59:59.999999+00:00", "9999-12-31T00:00:00+00:00"),
    ]
    for name, at, horizon in calls:
        events = _events(CHAIN_LOGS[name])
        rows.append(
            {
                "k": "chains",
                "log": name,
                "now": at,
                "horizon": horizon,
                "r": _chains_of(
                    lambda e=events, n=at, h=horizon: rc.build_chains(
                        e, now=datetime.fromisoformat(n), horizon=_iso(h)
                    )
                ),
            }
        )
    windows: list[tuple[str, str | None, str | None, str]] = [
        ("window_roots", _at(10), _at(20), now),
        ("window_roots", _at(10), _at(20, -1), now),
        ("window_roots", _at(10, 1), None, now),
        ("window_roots", None, _at(10), now),
        ("window_roots", None, None, now),
        ("window_roots", _at(19), _at(20), _at(55)),
        ("window_roots", _at(19), _at(20, 1), _at(55)),
        ("window_roots", _at(15), _at(16), now),
        ("close_match", None, _at(5), now),
        ("close_match", None, _at(0, 1), now),
        ("horizon", _at(0), _at(1), now),
        ("empty", None, _LATE, now),
        ("empty", None, "9999-12-31T23:29:59.999999+00:00", now),
        ("empty", None, "9999-12-31T22:00:00-01:00", now),
        ("late", None, "9999-12-31T23:00:00+00:00", "9999-12-31T23:59:00+00:00"),
    ]
    for name, start, end, at in windows:
        events = _events(CHAIN_LOGS[name])
        rows.append(
            {
                "k": "window",
                "log": name,
                "start": start,
                "end": end,
                "now": at,
                "r": _chains_of(
                    lambda e=events, s=start, t=end, n=at: rc.chains_in_window(
                        e, start=_iso(s), end=_iso(t), now=datetime.fromisoformat(n)
                    )
                ),
            }
        )
    tallies: list[tuple[str, str | None, str | None, str, list[int]]] = [
        ("empty", None, None, now, []),
        ("tally_mix", None, None, _at(45), [5, 3, 9, 2**80]),
        ("tally_mix", _at(1), _at(9), _at(45), []),
        ("tally_mix", None, None, _at(80), [_EDGE]),
        ("tally_mix", None, None, _at(80), [_EDGE, _EDGE]),
        ("window_edge", None, None, now, [1]),
        ("two_retries", None, None, now, []),
        ("standing_left", None, None, now, []),
        ("close_match", None, None, now, []),
        ("orphan_parent", None, None, now, []),
        ("roster", None, None, now, []),
    ]
    for name, start, end, at, chars in tallies:
        events = _events(CHAIN_LOGS[name])
        rows.append(
            {
                "k": "tally",
                "log": name,
                "start": start,
                "end": end,
                "now": at,
                "chars": [str(c) for c in chars],
                "r": _window_tally(events, _iso(start), _iso(end), _iso(at), chars),
            }
        )
    rates: list[tuple[int, list[tuple[str, int]], int]] = [
        (0, [("zz", 3)], 5),
        (800, [], 100),
        (800, [("review", 1)], 1),
        (20000, [("review", 203), ("review-disagrees", 201)], 20001),
        (3, [("review", 1), ("zz", 2)], 10),
        (7, [("total", 1), ("zz", 2)], 10),
        (16, [], 1),
        (8, [], 1),
        (2000, [], 2001),
        (2000, [], 2003),
        (1000, [("review", 2675)], 2675),
    ]
    for committed, counts, committed_calls in rates:
        tally = rc._Tally()
        tally.actions[rv.ACTION_COMMIT] = committed
        for detector, n in counts:
            tally.filings[detector] = n
        tally.committed_calls = committed_calls
        rows.append(
            {
                "k": "rate",
                "committed": committed,
                "filings": [list(p) for p in counts],
                "calls": committed_calls,
                "r": [
                    _spell(tally.filings_per_100()),
                    _spell(tally.to_dict()["calls_per_committed_write"]),
                ],
            }
        )
    for n, d in [
        (0, 0), (1, 0), (0, 5), (1, 1), (7, 7), (2, 3), (5, 7), (1, 32), (3, 32),
        (3, 20000), (1, 20000), (5, 6), (1, 3), (12345, 99999),
    ]:  # fmt: skip
        rows.append({"k": "share", "n": n, "d": d, "r": _spell(rc._share(n, d))})
    medians: list[list[int]] = [
        [],
        [5],
        [3, 1],
        [3, 1, 2],
        [4, 1, 3, 2],
        [-3, 1],
        [-1, 0],
        [-1, 1],
        [-2, -1],
        [7, 7, 7, 8],
        [10**30, 10**30 + 1],
        [2**53, 2**53 + 1],
        [2**53, 2**53 + 2],
        [2**53 + 2, 2**53 + 4],
        [2**53 + 2, 2**53 + 5],
        [2**54 + 1, 2**54 + 2],
        [_EDGE],
        [_EDGE - 1],
        [_EDGE, _EDGE],
        [_EDGE - 1, _EDGE - 1],
        [_EDGE - 1, _EDGE],
        [_EDGE, -_EDGE],
        [2 * _EDGE, 0],
        [2**1024, 2**1023],
        [10**400, -(10**400), 5],
        [10**400, -(10**400), 5, 6],
        [-(10**400), -(10**400)],
    ]
    for values in medians:
        rows.append(
            {
                "k": "median",
                "v": [str(v) for v in values],
                "r": _try(lambda v=values: rc._median(v), _spell),
            }
        )
    for ts in [
        "2026-11-21T10:00:00+00:00",
        "2020-12-31T12:00:00+00:00",
        "2021-01-03T12:00:00+00:00",
        "2021-01-04T00:00:00+00:00",
        "2024-12-30T00:00:00+00:00",
        "2024-12-29T23:59:59.999999+00:00",
        "2004-12-31T00:00:00+00:00",
        "2008-12-29T00:00:00+00:00",
        "2010-01-03T00:00:00+00:00",
        "2015-12-31T00:00:00+00:00",
        "2021-01-04T00:30:00+01:00",
        "2021-01-03T23:30:00-01:00",
        "2026-11-23T05:00:00+06:00",
        "2026-11-22T20:00:00-05:00",
        "0001-01-01T00:00:00+00:00",
        "0001-01-07T23:59:59+00:00",
        "0001-01-08T00:00:00+00:00",
        "0001-01-01T00:30:00+01:00",
        "0001-01-01T23:00:00+23:59",
        "9999-12-31T23:59:59.999999+00:00",
        "9999-12-27T00:00:00+00:00",
        "9999-12-26T23:59:59+00:00",
        "9999-12-31T00:30:00+01:00",
        "9999-12-31T23:30:00-01:00",
        "9999-01-01T00:00:00+00:00",
    ]:
        rows.append(
            {
                "k": "week",
                "ts": ts,
                "r": _try(
                    lambda t=ts: rc._iso_week(datetime.fromisoformat(t)), lambda v: v
                ),
            }
        )
    return rows


# The sweep's pools, in the order `_chain_log` draws from them.
SW_IDS = ["a", "b", "c", "d"]
SW_PARENTS = [None, "a", "b"]
SW_MINUTES = [0, 5, 29, 30, 31, 45, 60, 61]
SW_OFFSETS = ["Z", "+01:00"]
SW_SESSIONS = ["s1", "s2"]
SW_ACTIONS = ["committed", "committed", "refused_standing", "refused_secret", "odd"]
SW_DETECTORS = [None, "review", "zz"]


def _sw_ts(draw: _Draw) -> str:
    minute = SW_MINUTES[draw.pick(len(SW_MINUTES))]
    us = draw.pick(2)
    offset = SW_OFFSETS[draw.pick(len(SW_OFFSETS))]
    hour = 10 + minute // 60 + (1 if offset == "+01:00" else 0)
    return f"2026-11-21T{hour:02d}:{minute % 60:02d}:00.{us:06d}{offset}"


def _chain_log(draw: _Draw) -> list[dict[str, Any]]:
    """1 to 8 events; each drawn field by field in this order."""
    events: list[dict[str, Any]] = []
    for _ in range(1 + draw.pick(8)):
        write = draw.pick(3) == 2
        rid = SW_IDS[draw.pick(len(SW_IDS))]
        ts = _sw_ts(draw)
        session = SW_SESSIONS[draw.pick(2)]
        ev: dict[str, Any] = {"ts": ts, "review_id": rid, "session": session}
        keys = [k for k in ("x", "y") if draw.pick(2)]
        if write:
            ev["kind"] = "write"
            ev["status"] = "committed"
            ev["review_action"] = SW_ACTIONS[draw.pick(len(SW_ACTIONS))]
            ev["review_answers"] = {
                "relation": {k: "unrelated" for k in keys},
                "unsafe": 0,
                "secret": draw.pick(2),
            }
            detector = SW_DETECTORS[draw.pick(len(SW_DETECTORS))]
            if detector is not None:
                ev["review_filings"] = [{"id": "t", "detector": detector}]
            if draw.pick(2):
                ev["supersedes_reviewed"] = ["l"]
        else:
            ev["kind"] = "review_issued"
            parent = SW_PARENTS[draw.pick(len(SW_PARENTS))]
            if parent is not None:
                ev["parent_review_id"] = parent
            ev["related"] = keys
            ev["pre_answered"] = draw.pick(3) == 0
            if draw.pick(6) == 0:
                ev["roster"] = ["m"]
        events.append(ev)
    return events


def _sweep_answer(events: list[Any]) -> Any:
    start, end, now = _T0, _T0 + timedelta(minutes=45), _T0 + timedelta(minutes=70)
    return {
        "b": _chains_of(
            lambda: rc.build_chains(events, now=_T0 + timedelta(minutes=50))
        ),
        "w": _chains_of(
            lambda: rc.chains_in_window(events, start=start, end=end, now=now)
        ),
        "t": _window_tally(events, start, end, now, []),
    }


def _chains_sweep() -> dict[str, Any]:
    draw = _Draw(CHAINS_SEED)
    h = hashlib.sha256()
    for _ in range(CHAINS_COUNT):
        line = dumps_line(_sweep_answer(_chain_log(draw)))
        h.update((line + "\n").encode("ascii"))
    return {
        "k": "sweep",
        "seed": CHAINS_SEED,
        "count": CHAINS_COUNT,
        "sha256": h.hexdigest(),
    }


def generate_chains() -> dict[str, bytes]:
    rows = _chain_rows()
    rows.append(_chains_sweep())
    header = {
        "k": "header",
        "count": len(rows),
        "answer_window_us": rc.ANSWER_WINDOW // timedelta(microseconds=1),
        "logs": CHAIN_LOGS,
    }
    return {"review_counters_chains.jsonl": jsonl([header, *rows])}


# ---------------------------------------------------------------------------
# review_counters-2: the report (``review_counters_report.jsonl``)
# ---------------------------------------------------------------------------
#
# The header holds ``logs`` as the chains file does: each a list of events,
# an event a JSON object or a JSON text (`json.loads` reads it: the way to
# hold an `int` past 2**64 or a text that is not ASCII). Then one row per
# case, kind ``report``: `compute_review_report(logs[log], start=, end=,
# now=).to_dict()` spelled by `_rspell` under ``r``. `_rspell` writes a
# dict as ``[[key, value], ...]`` in its order, a float as its `repr`, a
# text by `_t` (ASCII as itself, otherwise its code points), and an
# exception as ``{"x": class}``. Times are written as `isoformat()`.
#
# The last line is a sweep: ``count`` logs drawn by `_report_log` from
# ``seed`` (`_chain_log`'s events, each given a client and a model from
# ``pools``, each issued row a response size, then 0 to 2 grades), the
# SHA-256 of `_report_answer` of each as `_common.dumps_line` and a
# newline.
#
# Not gated: the texts that are not ASCII are written as code points or
# inside JSON texts `json.loads` reads alike on every release, every time
# is an aware `fromisoformat` form 3.11 to 3.14 read alike, and the floats
# are medians and correctly rounded quotients, alike everywhere.

REPORT_SEED = 20261127
REPORT_COUNT = 2000
RP_CLIENTS = ["c2", "c1", "c10"]
RP_MODELS = ["m2", "m1"]
RP_CHARS: list[Any] = [None, 5, 12, 40, True]
_D0, _D1 = rc.DETECTORS


def _cm(ev: dict[str, Any], client: str, model: str, **more: Any) -> dict[str, Any]:
    return {**ev, "client": client, "model": model, **more}


def _text(ev: dict[str, Any]) -> str:
    return json.dumps(ev, ensure_ascii=True)


def _week_log() -> list[Any]:
    """Committed chains decided in weeks met out of order."""
    stamps = [
        "2027-01-04T09:00:00+00:00",
        "0999-06-01T09:00:00+00:00",
        "2026-12-31T09:00:00+00:00",
        "2027-01-01T09:00:00+00:00",
        "2027-01-04T00:30:00+01:00",
        "2020-12-31T09:00:00+00:00",
        "2026-12-28T00:10:00+00:00",
    ]
    log: list[Any] = []
    for i, stamp in enumerate(stamps):
        dec = datetime.fromisoformat(stamp)
        issued = (dec - timedelta(minutes=5)).isoformat()
        rid = f"w{i}"
        filings = (_D1,) if i % 2 else ("zz", _D0)
        log.append(_iss(rid, issued))
        log.append(_dec(rid, stamp, filings=filings))
    return log


REPORT_LOGS: dict[str, list[Any]] = {
    "empty": [],
    "clients": [
        _cm(_iss("a", _at(0)), "c2", "m1", response_chars=10),
        _cm(_iss("b", _at(1)), "c1", "m3", response_chars=30),
        _cm(_iss("c", _at(2)), "c10", "m2", response_chars=20),
        _cm(_iss("d", _at(50)), "C1", "m1", response_chars=41),
        _cm(_iss("e", _at(3)), "c1", "m2"),
        _dec("a", _at(5), filings=(_D0, _D1, _D0), links=("l1",), upd=("u1",)),
        _dec("b", _at(6), "refused_secret", filings=(_D0,)),
        _dec("e", _at(7), "refused_unsafe"),
        ["not", "an", "event"],
        "plain text",
        5,
        {"kind": "write", "review_id": 7, "ts": _at(8)},
    ],
    "unicode_clients": [
        _text(_cm(_iss("u1", _at(0)), "Ａ", "\U0001f600", response_chars=3)),
        _text(_cm(_iss("u2", _at(1)), "\U0001f600", "Ａ", response_chars=4)),
        _text(_cm(_iss("u3", _at(2)), "\ud800", "a", response_chars=5)),
        _text(_cm(_iss("u4", _at(3)), "a", "\ud800", response_chars=6)),
        _text(_cm(_iss("u5", _at(4)), "é", "Z", response_chars=7)),
        _dec("u1", _at(5)),
    ],
    "detectors": [
        _iss("f1", _at(0)),
        _dec("f1", _at(2), filings=("zz", _D0, "aa", "zz")),
        _iss("f2", _at(1)),
        _dec("f2", _at(3), filings=("aa", _D1, "yy")),
        _iss("f3", _at(4)),
        _dec("f3", _at(6), "refused_secret", filings=("qq",)),
        _iss("f4", _at(5)),
        _dec("f4", _at(7), "refused_standing", filings=("pp",)),
        _iss("f5", _at(8)),
        _dec("f5", _at(9), "odd", filings=("oo",)),
    ],
    "weeks": _week_log(),
    "week_overflow": [
        _iss("o1", "0001-01-01T00:25:00+01:00"),
        _dec("o1", "0001-01-01T00:30:00+01:00", filings=(_D0,)),
    ],
    "week_overflow_unfiled": [
        _iss("o1", "0001-01-01T00:25:00+01:00"),
        _dec("o1", "0001-01-01T00:30:00+01:00"),
    ],
    "sizes": [
        _cm(_iss("r1", _at(0)), "c1", "m1", response_chars=100),
        _cm(_iss("r2", _at(1), roster=("m",)), "c1", "m1", response_chars=99999),
        _cm(_iss("r3", _at(2), parent="r2"), "c2", "m2", response_chars=1),
        _cm(_iss("r4", _at(3), pre=True), "c1", "m1", response_chars=7),
        _cm(_iss("r5", _at(4)), "c1", "m1", response_chars=True),
        _cm(_iss("r6", _at(5)), "c1", "m1", response_chars=False),
        _cm(_iss("r7", _at(6)), "c1", "m1", response_chars=2.5),
        _cm(_iss("r8", _at(7)), "c1", "m1", response_chars="12"),
        _cm(_iss("r9", _at(8)), "c1", "m1", response_chars=-3),
        _cm(_iss("r10", _at(9)), "c1", "m1", response_chars=0, door="x"),
        _cm(_iss("r11", _at(-5)), "c3", "m3", response_chars=60),
        _cm(_iss("r12", _at(4), parent="r11"), "c9", "m9", response_chars=70),
        _cm(_iss("r13", _at(46), parent="r1"), "c8", "m8", response_chars=80),
        '{"kind": "review_issued", "review_id": "r14", "ts": "'
        + _at(10)
        + '", "client": "c1", "model": "m1", "response_chars": 18446744073709551617}',
        '{"kind": "review_issued", "review_id": "r15", "ts": "'
        + _at(11)
        + '", "client": "c1", "model": "m1", "response_chars": 1, "response_chars": 4}',
    ],
    "size_overflow": [
        _cm(_iss("v1", _at(0)), "c1", "m1", response_chars=1),
        _cm(_iss("v2", _at(1)), "c1", "m1", response_chars=1),
        '{"kind": "review_issued", "review_id": "v3", "ts": "'
        + _at(2)
        + '", "client": "c2", "model": "m2", "response_chars": 1'
        + "0" * 400
        + "}",
    ],
    "size_overflow_model": [
        _cm(_iss("v1", _at(0)), "c1", "m1", response_chars=1),
        _cm(_iss("v2", _at(1)), "c1", "m1", response_chars=1),
        _cm(_iss("v3", _at(2)), "c1", "m1", response_chars=1),
        '{"kind": "review_issued", "review_id": "v4", "ts": "'
        + _at(3)
        + '", "client": "c1", "model": "m2", "response_chars": 1'
        + "0" * 400
        + "}",
    ],
    "grades": [
        {
            "kind": "review_grade",
            "review_id": "g1",
            "ts": _at(100),
            "sample_ts": _at(1),
            "right_answers": True,
            "right_action": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g1",
            "ts": _at(100),
            "sample_ts": _at(1),
            "right_answers": False,
            "right_action": True,
            "pre_answered": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g2",
            "ts": _at(200),
            "sample_ts": _at(2),
            "right_answers": True,
            "right_action": False,
        },
        {
            "kind": "review_grade",
            "review_id": "g2",
            "ts": _at(150),
            "sample_ts": _at(2),
            "right_answers": False,
            "right_action": False,
        },
        {
            "kind": "review_grade",
            "review_id": "g3",
            "ts": _at(300),
            "right_answers": True,
            "right_action": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g4",
            "ts": _at(10),
            "sample_ts": _at(-10),
            "right_answers": True,
            "right_action": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g5",
            "ts": _at(-10),
            "sample_ts": _at(10),
            "right_answers": True,
            "right_action": True,
            "pre_answered": 1,
        },
        {
            "kind": "review_grade",
            "review_id": "g6",
            "ts": _at(10),
            "sample_ts": _at(10),
            "right_answers": 1,
            "right_action": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g7",
            "ts": "2026-11-21T11:40:00+01:00",
            "sample_ts": _at(3),
            "right_answers": True,
            "right_action": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g7",
            "ts": _at(40),
            "sample_ts": _at(3),
            "right_answers": False,
            "right_action": False,
            "pre_answered": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g8",
            "ts": _at(40),
            "sample_ts": _at(45),
            "right_answers": True,
            "right_action": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g9",
            "ts": _at(40),
            "sample_ts": "2026-11-21T10:44:59.999999+00:00",
            "right_answers": True,
            "right_action": False,
            "pre_answered": True,
        },
        {
            "kind": "review_grade",
            "review_id": "g10",
            "ts": _at(40),
            "sample_ts": "2026-11-21T11:00:00+01:00",
            "right_answers": False,
            "right_action": True,
        },
    ],
    "late": [
        _iss("z1", "9999-12-31T22:00:00+00:00"),
        _dec("z1", "9999-12-31T22:10:00+00:00", filings=(_D0,)),
    ],
}

_REPORT_WINDOWS: list[tuple[str | None, str | None, str]] = [
    (None, None, _at(60)),
    (_at(0), _at(45), _at(120)),
    (_at(0), _at(45), _at(60)),
    (None, "2026-11-21T11:45:00+01:00", _at(120)),
    (None, "2026-11-21T11:00:00+01:00", _at(30)),
    (_at(-60), None, "2026-11-21T15:30:00+05:30"),
]

_REPORT_EXTRA: list[tuple[str, str | None, str | None, str]] = [
    ("weeks", None, None, "2027-02-01T00:00:00+00:00"),
    (
        "weeks",
        "2026-12-31T00:00:00+00:00",
        "2027-01-04T08:55:00+00:00",
        "2027-02-01T00:00:00+00:00",
    ),
    ("week_overflow", None, None, "0001-01-02T00:00:00+00:00"),
    ("week_overflow_unfiled", None, None, "0001-01-02T00:00:00+00:00"),
    ("late", None, "9999-12-31T23:45:00+00:00", "9999-12-31T23:50:00+00:00"),
    ("late", None, "9999-12-31T23:00:00+00:00", "9999-12-31T23:59:59.999999+00:00"),
    ("late", None, "9999-12-31T23:00:00+00:00", "9999-12-31T23:10:00+00:00"),
    ("late", "9999-12-31T21:00:00+00:00", None, "9999-12-31T23:00:00+00:00"),
    ("grades", _at(0), _at(45), _at(400)),
    ("grades", _at(5), None, _at(400)),
]


def _rspell(value: Any) -> Any:
    if isinstance(value, dict):
        return [[_rspell(k), _rspell(v)] for k, v in value.items()]
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _t(value)
    return value


def _report(events: list[Any], start: Any, end: Any, now: Any) -> Any:
    return _try(
        lambda: rc.compute_review_report(
            events, start=start, end=end, now=now
        ).to_dict(),
        _rspell,
    )


def _report_rows() -> list[dict[str, Any]]:
    cases: list[tuple[str, str | None, str | None, str]] = [
        (name, start, end, now)
        for name in REPORT_LOGS
        if name not in ("late", "week_overflow", "week_overflow_unfiled")
        for start, end, now in (
            _REPORT_WINDOWS if name == "clients" else _REPORT_WINDOWS[:2]
        )
    ]
    cases.extend(_REPORT_EXTRA)
    return [
        {
            "k": "report",
            "log": name,
            "start": start,
            "end": end,
            "now": now,
            "r": _report(_events(REPORT_LOGS[name]), _iso(start), _iso(end), _iso(now)),
        }
        for name, start, end, now in cases
    ]


def _report_log(draw: _Draw) -> list[dict[str, Any]]:
    """`_chain_log`'s events with a client, a model and a response size
    drawn for each in order, then 0 to 2 grades drawn field by field."""
    events = _chain_log(draw)
    for ev in events:
        ev["client"] = RP_CLIENTS[draw.pick(len(RP_CLIENTS))]
        ev["model"] = RP_MODELS[draw.pick(len(RP_MODELS))]
        if ev["kind"] == "review_issued":
            chars = RP_CHARS[draw.pick(len(RP_CHARS))]
            if chars is not None:
                ev["response_chars"] = chars
    for _ in range(draw.pick(3)):
        rid = SW_IDS[draw.pick(len(SW_IDS))]
        ts = _sw_ts(draw)
        sample_ts = _sw_ts(draw)
        events.append(
            {
                "kind": "review_grade",
                "review_id": rid,
                "ts": ts,
                "sample_ts": sample_ts,
                "right_answers": draw.pick(2) == 1,
                "right_action": draw.pick(2) == 1,
                "pre_answered": draw.pick(2) == 1,
            }
        )
    return events


def _report_answer(events: list[Any]) -> Any:
    start, end, now = _T0, _T0 + timedelta(minutes=45), _T0 + timedelta(minutes=70)
    return {
        "w": _report(events, start, end, now),
        "a": _report(events, None, None, _T0 + timedelta(minutes=50)),
    }


def _report_sweep() -> dict[str, Any]:
    draw = _Draw(REPORT_SEED)
    h = hashlib.sha256()
    for _ in range(REPORT_COUNT):
        line = dumps_line(_report_answer(_report_log(draw)))
        h.update((line + "\n").encode("ascii"))
    return {
        "k": "sweep",
        "seed": REPORT_SEED,
        "count": REPORT_COUNT,
        "sha256": h.hexdigest(),
    }


def generate_report() -> dict[str, bytes]:
    rows = _report_rows()
    rows.append(_report_sweep())
    header = {
        "k": "header",
        "count": len(rows),
        "logs": REPORT_LOGS,
        "pools": {"clients": RP_CLIENTS, "models": RP_MODELS, "chars": RP_CHARS},
    }
    return {"review_counters_report.jsonl": jsonl([header, *rows])}
