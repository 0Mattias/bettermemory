"""Fixtures: the per-turn use-token bookkeeping of `handlers/_shared.py`
(``use_tokens.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/use_tokens.rs``. This pins
`_drain_expired_use_tokens`, `_emit_expired_use_tokens`, `_advance_turn`,
`_event_ts_epoch`, `_scan_settled_use_events`,
`_already_recorded_pending_ids` (and its alias
`_hook_attributed_pending_ids`) and `_attach_use_tokens` over a
`SessionState`.

The effects are scripted as in ``gen_session``: ``time.time`` reads the
case's clock and ``secrets.token_hex(8)`` counts up from the case's seed.
The recorder is a stub: ``record`` keeps each row written (its kind and
the ``repr`` of its keywords), ``session_for_request`` answers the case's
``sid`` and counts its calls, and ``store.iter_events_backward`` yields
the case's scripted log rows (newest first), counting the calls and the
rows pulled.

A case is ``seed``, ``sid`` and the steps ``s``, each:

- ``t``: the clock from this step on (its ``repr``), when it changes;
- ``log``: the log's rows from this step on, when they change;
- ``do``: ``["iss", ids]`` (`issue_use_tokens` under the lock),
  ``["adv", ovr]`` (`_advance_turn`), ``["drain", ovr]``
  (`_drain_expired_use_tokens` under the lock), ``["emit", toks]``
  (`_emit_expired_use_tokens`, each token ``[id, token, repr(issued_at),
  turn]``), ``["arp", xs, xp]`` or ``["hap", xs, xp]``
  (`_already_recorded_pending_ids` or its alias, ``xp`` as ``[[id,
  repr(issued_at)], ...]``, null for absent), ``["att", hits]``
  (`_attach_use_tokens`, each hit a list of ``[key, value]`` pairs),
  ``["ets", raw]`` (`_event_ts_epoch`) or ``["turn", n]``;
- answers: ``r`` the return (``{"err": [type, message]}`` for an
  exception, the message of a `TypeError` null here and written in
  ``use_tokens_python.jsonl``), ``n`` the clock reads, ``k`` the draws, ``w`` the rows
  written, ``q`` the session lookups, ``it`` the log reads, ``pl`` the
  rows pulled and ``st`` the state after (``gen_session._snap``).

Two digest rows follow: random cases from splitmix64 ``seed``
(`_random_case`), each step's answers as compact sorted JSON, one per
line, hashed with SHA-256; and `_event_ts_epoch` of ``count`` random
instants across the datetime range (`_ets_digest`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterator
from typing import Any
from unittest import mock

from bettermemory.handlers import _shared
from bettermemory.session import PendingUseToken, SessionState

from ._common import jsonl
from .gen_location_stamps import splitmix
from .gen_session import _Fx, _snap, _tok


class _Rec:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.rows: list[dict[str, Any]] = []
        self.w: list[list[str]] = []
        self.q = self.it = self.pl = 0
        self.store = self

    def record(self, kind: str, **fields: Any) -> None:
        self.w.append([kind, repr(fields)])

    def session_for_request(self) -> str:
        self.q += 1
        return self.sid

    def iter_events_backward(self) -> Iterator[dict[str, Any]]:
        self.it += 1
        for row in self.rows:
            self.pl += 1
            yield row


def _ovr(v: Any) -> set[str] | None:
    return None if v is None else set(v)


def run(case: dict[str, Any], python: bool = False) -> list[dict[str, Any]]:
    """Each step's answers. A `TypeError`'s text follows the Python release
    (3.14 words the unhashable set element otherwise), so it is written
    only when ``python`` is set, for the Python-gated file."""
    fx = _Fx(case["seed"])
    rec: Any = _Rec(case["sid"])
    out = []
    with (
        mock.patch("bettermemory.session.time.time", fx.time),
        mock.patch("bettermemory.session.secrets.token_hex", fx.token_hex),
    ):
        st = SessionState()
        for step in case["s"]:
            if "t" in step:
                fx.now = float(step["t"])
            if "log" in step:
                rec.rows = step["log"]
            fx.reads = fx.draws = 0
            rec.w, rec.q, rec.it, rec.pl = [], 0, 0, 0
            op, *a = step["do"]
            r: Any = None
            try:
                if op == "iss":
                    r = list(st.issue_use_tokens(a[0]).values())
                elif op == "adv":
                    _shared._advance_turn(st, rec, override_ids=_ovr(a[0]))
                elif op == "drain":
                    with st.lock:
                        got = _shared._drain_expired_use_tokens(
                            st, rec, override_ids=_ovr(a[0])
                        )
                    r = [_tok(t.memory_id, t) for t in got]
                elif op == "emit":
                    toks = [
                        PendingUseToken(
                            token=t[1],
                            memory_id=t[0],
                            issued_at=float(t[2]),
                            issued_at_turn=t[3],
                        )
                        for t in a[0]
                    ]
                    _shared._emit_expired_use_tokens(st, rec, toks)
                elif op in ("arp", "hap"):
                    fn = (
                        _shared._already_recorded_pending_ids
                        if op == "arp"
                        else _shared._hook_attributed_pending_ids
                    )
                    xs = None if a[0] is None else set(a[0])
                    xp = None if a[1] is None else {m: float(v) for m, v in a[1]}
                    r = sorted(fn(st, rec, extra_session_ids=xs, extra_pending=xp))
                elif op == "att":
                    hits = [dict((k, v) for k, v in h) for h in a[0]]
                    _shared._attach_use_tokens(hits, st)
                    r = repr(hits)
                elif op == "ets":
                    r = repr(_shared._event_ts_epoch(a[0]))
                elif op == "turn":
                    st.turn_counter = a[0]
                else:
                    raise ValueError(op)
            except (TypeError, ValueError, OverflowError, KeyError) as e:
                text = str(e) if python or not isinstance(e, TypeError) else None
                r = {"err": [type(e).__name__, text]}
            out.append(
                {
                    "r": r,
                    "n": fx.reads,
                    "k": fx.draws,
                    "w": rec.w,
                    "q": rec.q,
                    "it": rec.it,
                    "pl": rec.pl,
                    "st": _snap(st),
                }
            )
    return out


def _row(ts: Any, session: Any = "S", ids: Any = None, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": kw.pop("kind", "use")}
    if ts is not None:
        row["ts"] = ts
    if session is not None:
        row["session"] = session
    if ids is not None:
        row["ids"] = ids
    row.update(kw)
    return row


# The clock's seconds as the recorder spells them.
T1000 = "1970-01-01T00:16:40Z"
T1000_5 = "1970-01-01T00:16:40.500Z"
T1600 = "1970-01-01T00:26:40Z"
T2801 = "1970-01-01T00:46:41Z"


def _st(*steps: Any) -> list[dict[str, Any]]:
    """Steps from ``(t, log, op...)`` tuples (None for no change)."""
    out = []
    for t, log, *do in steps:
        s: dict[str, Any] = {"do": list(do)}
        if t is not None:
            s["t"] = t
        if log is not None:
            s["log"] = log
        out.append(s)
    return out


def _unhashable_case() -> dict[str, Any]:
    """Unhashable sessions raise TypeError, even when the row would be
    skipped later; a later row is never reached."""
    return {
        "seed": 40,
        "sid": "S",
        "s": _st(
            ("1000.0", None, "iss", ["m1"]),
            (None, [_row(T1000, session=["S"], ids=["m1"])], "arp", None, None),
            (None, [_row(T1000, session={"a": 1}, ids=["m1"])], "arp", None, None),
            (None, [_row(T1000, kind="x", session=["S"])], "arp", None, None),
            (None, [_row(T1000, session=[["S"]], ids=[])], "arp", None, None),
            (
                None,
                [_row(T1000, session="", session_id=["S"]), _row(T1000, ids=["m1"])],
                "arp",
                None,
                None,
            ),
            ("2801.0", [_row(T1000, session={"x": []})], "adv", None),
        ),
    }


def _cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []

    def case(seed: int, *steps: Any) -> None:
        c.append({"seed": seed, "sid": "S", "s": _st(*steps)})

    # Nothing pending: no log read, no session lookup; the turn moves.
    case(
        0,
        ("1000.0", None, "adv", None),
        (None, [_row(T1000, ids=["m1"])], "arp", None, None),
        (None, None, "drain", None),
        (None, None, "emit", []),
        (None, None, "att", []),
    )
    # The auto-commit: two turns and ten minutes, overrides excluded.
    case(
        10,
        ("1000.0", None, "iss", ["m1", "m2", "m3"]),
        (None, None, "adv", None),
        (None, None, "adv", None),
        ("1599.5", None, "adv", None),
        ("1600.0", None, "adv", ["m2"]),
        (None, None, "adv", None),
        (None, None, "adv", []),
    )
    # A settled `use` row purges the live token before the auto-commit;
    # the scan stops at the first row older than the oldest token.
    old = "1970-01-01T00:16:39.999999Z"
    case(
        20,
        ("1000.0", None, "iss", ["m1", "m2"]),
        (
            "1000.5",
            [
                _row(T1000_5, kind="search", ids=["m2"]),
                _row(T1000_5, ids=["m1"]),
                _row(old, ids=["m2"]),
                _row(T1000_5, ids=["m2"]),
            ],
            "adv",
            None,
        ),
        ("1700.0", None, "adv", None),
        (None, None, "adv", None),
    )
    # The session match: ours, the bridge's tag, a foreign session, a
    # falsy session falling back to `session_id`, explicit extra ids.
    log = [
        _row(T1000, session="T", ids=["a"]),
        _row(T1000, session="T", ids=["b"], triggered_from="stop_hook"),
        _row(T1000, session="", ids=["c"], session_id="S"),
        _row(T1000, session=None, ids=["d"], session_id="S"),
        _row(T1000, session=None, ids=["e"], triggered_from="stop_hook"),
        _row(T1000, session=0, ids=["f"], session_id="X", triggered_from="stop_hook"),
        _row(T1000, session=[], ids=["g"], session_id="S"),
        _row(T1000, session="U", ids=["h"], triggered_from="other"),
        _row(T1000, session=5, ids=["i"], triggered_from="stop_hook"),
    ]
    ids = list("abcdefghi")
    case(
        30,
        ("1000.0", log, "iss", ids),
        (None, None, "arp", None, None),
        (None, None, "arp", ["T", "U"], None),
        (None, None, "hap", [], None),
        (None, None, "arp", ["X"], None),
    )
    c.append(_unhashable_case())
    # The timestamps: malformed or not a str is skipped without stopping
    # the scan; an offset; equal to the mint time counts; a naive time.
    case(
        50,
        ("1000.0", None, "iss", ["m1", "m2", "m3", "m4", "m5", "m6"]),
        (
            None,
            [
                _row("garbage", ids=["m1"]),
                _row(1000, ids=["m2"]),
                _row(None, ids=["m3"]),
                _row("1970-01-01T01:16:40+01:00", ids=["m4"]),
                _row("1970-01-01T00:16:40", ids=["m5"]),
                _row("1970-01-01T00:16:40ZZ", ids=["m6"]),
                _row("1970-01-01T00:16:39.9Z", ids=["m6"]),
                _row(T1000, ids=["m6"]),
            ],
            "arp",
            None,
            None,
        ),
        (None, [_row("1970-01-01T00:16:40-00:01", ids=["m6"])], "arp", None, None),
        (None, [_row("1970-01-01T00:16:41+00:00:01", ids=["m6"])], "arp", None, None),
    )
    # The ids: legacy `memory_ids`, falsy `ids`, not a list, non-str
    # members, unknown ids, repeats.
    case(
        60,
        ("1000.0", None, "iss", ["a", "b", "c", "d", "e"]),
        (
            None,
            [
                _row(T1000, ids=[], memory_ids=["a"]),
                _row(T1000, memory_ids=["b", 7, None]),
                _row(T1000, ids="c"),
                _row(T1000, ids={"d": 1}),
                _row(T1000, ids=[1, ["e"], "zz", "e", "e"]),
                _row(T1000, ids=0, memory_ids="d"),
                _row(T1000, ids=None, memory_ids=None),
            ],
            "arp",
            None,
            None,
        ),
    )
    # Extra pending tokens: an older mint time widens the scan and wins
    # over the live one; an id only there.
    case(
        70,
        ("1000.0", None, "iss", ["a"]),
        (
            "1000.0",
            [
                _row(T1000_5, ids=["b"]),
                _row(T1000, ids=["a"]),
                _row(old, ids=["a", "c"]),
            ],
            "arp",
            None,
            [["a", "999.0"], ["c", "999.0"]],
        ),
        (None, None, "arp", None, [["a", "1000.75"]]),
        (None, None, "arp", None, []),
        (None, None, "arp", None, [["c", "nan"]]),
        (None, None, "arp", None, [["c", "-inf"], ["a", "nan"]]),
    )
    # The expiry: drained after thirty minutes, settled ones dropped,
    # the lost ones emitted once with the oldest token's age and turns.
    case(
        80,
        ("1000.0", None, "iss", ["a", "b"]),
        ("1000.25", None, "adv", None),
        ("1500.0", None, "iss", ["c", "d"]),
        (
            "2801.0",
            [_row(T1000_5, session="T", ids=["b"], triggered_from="stop_hook")],
            "adv",
            None,
        ),
        ("3400.0", [], "adv", None),
        (None, None, "adv", None),
    )
    # Override ids drop expired tokens before the scan and keep live
    # ones out of the auto-commit.
    case(
        90,
        ("1000.0", None, "iss", ["a", "b", "c"]),
        ("2900.0", [], "adv", ["a"]),
        (None, None, "iss", ["d"]),
        ("5000.0", None, "adv", ["b", "c", "d"]),
        (None, None, "adv", ["b", "c"]),
    )
    # A drained stash with no live tokens still scans for the drained
    # ones; drain on its own under the lock.
    case(
        100,
        ("1000.0", None, "iss", ["a", "b"]),
        ("2900.0", [_row(T1600, ids=["b"])], "turn", 7),
        (None, None, "drain", None),
        (None, None, "iss", ["c"]),
        ("5000.0", [], "adv", None),
        (None, None, "drain", ["a"]),
    )
    # The expiry row's numbers: the oldest by `<` (ties keep the first,
    # NaN never replaces nor is replaced once first), truncation toward
    # zero, the turn difference past i64, NaN and the infinities.
    i64 = 2**63
    case(
        110,
        ("1000.0", None, "emit", [["a", "t1", "500.5", 3], ["b", "t2", "400.75", 9]]),
        (None, None, "emit", [["a", "t1", "400.0", 3], ["b", "t2", "400.0", 9]]),
        (None, None, "emit", [["a", "t1", "1000.9", 0]]),
        (None, None, "emit", [["a", "t1", "1001.9", 0]]),
        (None, None, "emit", [["a", "t1", "nan", 0], ["b", "t2", "1.0", 1]]),
        (None, None, "emit", [["b", "t2", "1.0", 1], ["a", "t1", "nan", 0]]),
        (None, None, "emit", [["a", "t1", "inf", 0]]),
        (None, None, "emit", [["a", "t1", "-inf", 0]]),
        (None, None, "emit", [["a", "t1", "-1e300", 0]]),
        (None, None, "turn", i64 - 1),
        (None, None, "emit", [["a", "t1", "0.0", -i64]]),
        ("nan", None, "emit", [["a", "t1", "0.0", 0]]),
    )
    # A NaN expiry age raises after the drain: the state keeps what the
    # drain did, and no auto-commit runs.
    case(
        120,
        ("-inf", None, "iss", ["a", "b"]),
        ("1e10", [], "adv", None),
        (None, None, "iss", ["c"]),
        ("inf", None, "adv", None),
    )
    # The hits' tokens: in order, a repeated id takes the last token,
    # an existing `use_token` is replaced in place, a hit with no id.
    case(
        130,
        (
            "1000.0",
            None,
            "att",
            [[["id", "a"], ["score", 1.5]], [["id", "b"]], [["id", "a"]]],
        ),
        (None, None, "att", [[["use_token", "old"], ["id", "c"], ["x", None]]]),
        (None, None, "att", [[["id", "d"]], [["title", "no id"]]]),
        (None, None, "att", [[["id", "é"], ["n", [1, {"k": True}]]]]),
    )
    # `_event_ts_epoch` alone: the spellings, rounding at microseconds,
    # the ends of the range, a value that is not a str.
    case(
        140,
        *[
            (None, None, "ets", raw)
            for raw in [
                T1000,
                T1000_5,
                "2026-11-21T10:00:00.123456Z",
                "2026-11-21T10:00:00.123456+05:30",
                "0001-01-01T00:00:00Z",
                "0001-01-01T00:00:00-23:59",
                "9999-12-31T23:59:59.999999Z",
                "9999-12-31T23:59:59.999999+23:59",
                "2286-11-20T17:46:39.999999Z",
                "2255-06-05T23:47:34.740992Z",
                "1969-12-31T23:59:59.999999Z",
                "1970-01-01",
                "2026-11-21 10:00Z",
                "",
                "Z",
                "2026-11-21T10:00:00z",
                1.5,
                None,
                ["2026-01-01"],
            ]
        ],
    )
    return c


IDS = ["m1", "m2", "m3", "m4"]
DELTAS = ["0.5", "1.0", "600.0", "1800.0", "1801.0"]
TS = [
    T1000,
    "1970-01-01T00:16:41Z",
    T1600,
    T2801,
    "1970-01-01T01:46:40+01:00",
    "bad",
    None,
    5,
]
SESSIONS: list[Any] = ["S", "S", "T", "", None, ["x"], {"a": 1}]
KINDS = ["use", "use", "use", "search"]
SEED = 0x7E5E7
COUNT = 2000


def _random_row(r: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": KINDS[r(4)]}
    ts = TS[r(8)]
    if ts is not None:
        row["ts"] = ts
    session = SESSIONS[r(7)]
    if session is not None:
        row["session"] = session
    if r(2):
        row["triggered_from"] = "stop_hook"
    k = r(6)
    if k == 0:
        row["ids"] = [IDS[r(4)] for _ in range(r(3) + 1)]
    elif k == 1:
        row["ids"] = []
    elif k == 2:
        row["ids"] = IDS[r(4)]
    elif k == 3:
        row["memory_ids"] = [IDS[r(4)]]
    elif k == 4:
        row["ids"] = [1, IDS[r(4)]]
    return row


def _random_case(gen: Iterator[int]) -> dict[str, Any]:
    """One digest case; the native test draws in the same order."""

    def r(n: int) -> int:
        return next(gen) % n

    case: dict[str, Any] = {"seed": r(1 << 32), "sid": "S"}
    steps: list[dict[str, Any]] = []
    t = 1000.0
    for _ in range(16):
        step: dict[str, Any] = {}
        if r(3) == 0:
            t += float(DELTAS[r(len(DELTAS))])
            step["t"] = repr(t)
        if r(4) == 0:
            step["log"] = [_random_row(r) for _ in range(r(5))]
        op = r(8)
        if op in (0, 1):
            do: list[Any] = ["adv", None if r(2) else [IDS[r(4)]]]
        elif op == 2:
            do = ["iss", [IDS[r(4)] for _ in range(r(3) + 1)]]
        elif op == 3:
            do = ["drain", None]
        elif op == 4:
            do = ["arp", None, None]
        elif op == 5:
            do = ["turn", r(5)]
        elif op == 6:
            do = ["att", [[["id", IDS[r(4)]]] for _ in range(r(3))]]
        else:
            do = ["adv", None]
        step["do"] = do
        steps.append(step)
    case["s"] = steps
    return case


def _digest() -> dict[str, Any]:
    gen = splitmix(SEED)
    h = hashlib.sha256()
    for _ in range(COUNT):
        for ans in run(_random_case(gen)):
            h.update(
                json.dumps(ans, sort_keys=True, separators=(",", ":")).encode("ascii")
            )
            h.update(b"\n")
    return {"digest": {"seed": SEED, "count": COUNT, "sha256": h.hexdigest()}}


ETS_SEED = 0xE7500
ETS_COUNT = 20000
#: Microseconds from the epoch to 0001-01-01 and to 9999-12-31T23:59:59.999999.
US_MIN = -62_135_596_800_000_000
US_SPAN = 253_402_300_800_000_000 - US_MIN


def _ets_digest() -> dict[str, Any]:
    """`_event_ts_epoch` of ``isoformat()`` of random UTC instants: the
    instant ``US_MIN + next % US_SPAN`` microseconds from the epoch."""
    gen = splitmix(ETS_SEED)
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    h = hashlib.sha256()
    for _ in range(ETS_COUNT):
        us = US_MIN + next(gen) % US_SPAN
        text = (epoch + dt.timedelta(microseconds=us)).isoformat()
        h.update(repr(_shared._event_ts_epoch(text)).encode("ascii"))
        h.update(b"\n")
    return {
        "ets_digest": {"seed": ETS_SEED, "count": ETS_COUNT, "sha256": h.hexdigest()}
    }


def _rows(cases: list[dict[str, Any]], python: bool) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        answers = run(case, python)
        rows.append(
            {**case, "s": [{**s, **a} for s, a in zip(case["s"], answers, strict=True)]}
        )
    return rows


def generate() -> dict[str, bytes]:
    rows = _rows(_cases(), False)
    header = {"file": "use_tokens", "cases": len(rows)}
    return {"use_tokens.jsonl": jsonl([header, *rows, _digest(), _ets_digest()])}


def generate_python() -> dict[str, bytes]:
    """The TypeError's text of an unhashable session, as CPython 3.13.13
    words it (``use_tokens_python.jsonl``, gated on the Python release)."""
    rows = _rows([_unhashable_case()], True)
    header = {"file": "use_tokens_python", "cases": len(rows)}
    return {"use_tokens_python.jsonl": jsonl([header, *rows])}
