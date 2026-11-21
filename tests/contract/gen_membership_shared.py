"""Fixtures: the pure rules of `membership_shared` (``membership_shared.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/membership_shared.rs``. This pins
`_same_file`, `_same_files`, `_ticked`, `_in_ticks`, `_carried_in_ticks`,
`_read_here`, `_moved_under`, `_read_as_now`, `_made_by`, `_continues`,
`_shared_remint`, `_view_made`, `_asked_where` (and through it `_decides`
and `_declared_answer`), `_joined_silently_by_token` (and
`_by_declaration`) and `_asks`.

`self` is a stub: `_carriers` answers the case's carriers and records the
tokens it was asked for, `membership_rows` the case's question rows and
`membership_log` the case's log rows. A checkout is spelled
``{"m": machine, "t": token, "s": stamps, "x": ticks, "o": [device,
inode] or null}``, a sighting ``{"s": stamps, "x": ticks}``, a stamp
``[name, inode, mtime, ctime, size]``. Each case names its function in
``f`` and holds its answer in ``r``, or ``e`` the class raised (a
KeyError with its key). ``asked`` is what `_carriers` was asked, null
where it was not called. A log row's payload is text or ``{"rep": [head,
unit, count, tail]}``.

The last line is a digest over `_SWEEP` pairs of stamp sets drawn by the
LCG `_Lcg` from `_SEED` (`_draw`): each pair's answers as a line of
``0``/``1`` (`_sweep_line`), and ``o`` the SHA-256 of the lines joined
by newlines. Not gated: no answer follows the Python release or the
system.

``membership_shared_python.jsonl`` holds the log cases whose payload
nests past the depth where the interpreter stops recursing
(`_deep_cases`): a header line and the cases, no digest. It is
Python-gated.
"""

from __future__ import annotations

import hashlib
import types
from typing import Any

from bettermemory import checkout_token as ct
from bettermemory import membership_shared as ms
from bettermemory.membership_records import ObjectKey, Proposal

from ._common import jsonl

_SEED = 20261121
_SWEEP = 3000
_NONCE = "0123456789ABC"
_TOKEN = "0123456789ABCDEFGHJKMNPQRS"
_OTHER = "ZZZZZZZZZZZZZZZZZZZZZZZZZZ"
_THIRD = "1111111111111111111111111A"
_S = 1_000_000_000
_HUGE = 10**40


def _remint(token: str, device: str, inode: str) -> str:
    return _NONCE + ct._link(ct.Lineage(token, device, inode), _NONCE)


def _stamp(s: list[Any]) -> tuple[Any, ...]:
    return tuple(s)


def _ck(c: dict[str, Any]) -> Any:
    o = c.get("o")
    return types.SimpleNamespace(
        machine=c.get("m", "m1"),
        token=c.get("t"),
        stamps=tuple(_stamp(s) for s in c.get("s", [])),
        ticks=c.get("x"),
        object_key=None if o is None else ObjectKey(o[0], o[1]),
    )


def _loc(c: dict[str, Any]) -> Any:
    return types.SimpleNamespace(
        stamps=tuple(_stamp(s) for s in c.get("s", [])), ticks=c.get("x")
    )


def _payload(p: Any) -> str:
    if isinstance(p, str):
        return p
    head, unit, count, tail = p["rep"]
    return head + unit * count + tail


class _Self:
    def __init__(self, case: dict[str, Any]) -> None:
        self.asked: list[str] | None = None
        carriers = [_ck(c) for c in case.get("carriers", [])]
        rows = case.get("rows", [])
        log = [
            types.SimpleNamespace(kind=k, payload=_payload(p))
            for k, p in case.get("log", [])
        ]
        outer = self

        def carriers_of(_self: Any, tokens: Any) -> list[Any]:
            outer.asked = sorted(set(tokens))
            return carriers

        self.carriers_of = carriers_of
        self._store = types.SimpleNamespace(
            membership_rows=lambda *a, **kw: list(rows),
            membership_log=lambda *a, **kw: list(log),
        )


def _run(case: dict[str, Any]) -> dict[str, Any]:
    f = case["f"]
    me = _Self(case)
    k: Any = _ck(case["k"]) if "k" in case else None
    loc: Any = _loc(case["loc"]) if "loc" in case else None
    carriers = [_ck(c) for c in case.get("carriers", [])]
    out = dict(case)
    try:
        if f == "same_file":
            r: Any = ms._same_file(_stamp(case["a"]), _stamp(case["b"]))
        elif f == "same_files":
            r = ms._same_files(
                [_stamp(s) for s in case["a"]], [_stamp(s) for s in case["b"]]
            )
        elif f == "ticked":
            r = ms._ticked([_stamp(s) for s in case["a"]])
        elif f == "in_ticks":
            r = ms._in_ticks(k, loc)
        elif f == "carried":
            r = ms._carried_in_ticks(_ck(case["c"]), k, loc)
        elif f == "read_here":
            r = ms._read_here(k, loc)
        elif f == "moved_under":
            r = ms._moved_under(k, loc)
        elif f == "read_as_now":
            r = ms._read_as_now(k, loc, case["found"], carriers)
        elif f == "made_by":
            r = ms._made_by(k, case["found"], carriers)
        elif f == "continues":
            r = ms._continues(k, loc, case["found"], carriers)
        elif f in ("shared_remint", "view_made"):
            saved = ms._carriers
            ms._carriers = me.carriers_of  # type: ignore[assignment]
            try:
                if f == "shared_remint":
                    r = ms._shared_remint(me, k, loc, case["found"])  # type: ignore[arg-type]
                else:
                    r = ms._view_made(me, k, case["found"])  # type: ignore[arg-type]
            finally:
                ms._carriers = saved
            out["asked"] = me.asked
        elif f == "asked_where":
            r = ms._asked_where(me, "k1")  # type: ignore[arg-type]
        elif f == "joined":
            r = ms._joined_silently_by_token(me, case["checkout"])  # type: ignore[arg-type]
        elif f == "asks":
            pr = Proposal(
                "p1",
                "k1",
                case["kind"],
                (),
                case["evidence"],
                "",
                "",
                "open",
                None,
                None,
                None,
                "t0",
            )
            r = list(ms._asks(pr))
        else:
            raise AssertionError(f)
        out["r"] = r
    except KeyError as e:
        out["e"] = f"KeyError:{e.args[0]}"
    except Exception as e:  # noqa: BLE001
        out["e"] = type(e).__name__
    return out


def _st(name: str, ino: int, m: int, c: int, size: int) -> list[Any]:
    return [name, ino, m, c, size]


_D = "description"
_X = "info/exclude"


def _stamp_cases() -> list[dict[str, Any]]:
    a = _st(_D, 1, 5 * _S + 123, 6 * _S + 7, 10)
    out: list[dict[str, Any]] = []
    pairs = [
        (a, a),
        (a, _st(_D, 9, 5 * _S + 123, 6 * _S + 7, 10)),
        (a, _st(_X, 1, 5 * _S + 123, 6 * _S + 7, 10)),
        (a, _st(_D, 1, 5 * _S + 123, 6 * _S + 7, 11)),
        (a, _st(_D, 1, 5 * _S + 124, 6 * _S + 7, 10)),
        (a, _st(_D, 1, 5 * _S + 123, 6 * _S + 8, 10)),
        (a, _st(_D, 1, 5 * _S, 6 * _S, 10)),
        (_st(_D, 1, 5 * _S, 6 * _S, 10), a),
        (a, _st(_D, 1, 5 * _S, 7 * _S, 10)),
        (a, _st(_D, 1, 6 * _S, 6 * _S, 10)),
        (_st(_D, 1, 6 * _S, 6 * _S, 10), _st(_D, 1, 5 * _S + 1, 5 * _S + 1, 10)),
        (_st(_D, 1, 6 * _S, 6 * _S, 10), _st(_D, 1, 7 * _S - 1, 7 * _S - 1, 10)),
        (_st(_D, 1, 6 * _S, 6 * _S, 10), _st(_D, 1, 7 * _S, 7 * _S, 10)),
        (_st(_D, 1, 6 * _S, 6 * _S + 1, 10), _st(_D, 1, 6 * _S + 5, 6 * _S + 5, 10)),
        (_st(_D, 1, 0, 0, 0), _st(_D, 1, _S - 1, -_S + 1, 0)),
        (_st(_D, 1, 0, 0, 0), _st(_D, 1, -_S, 0, 0)),
        (_st(_D, 1, -_S, -2 * _S, 0), _st(_D, 1, -_S - 999_999_999, -_S - 5, 0)),
        (_st(_D, 1, -1, -1, 0), _st(_D, 1, -_S, -_S, 0)),
        (_st(_D, 1, _HUGE, _HUGE, 1), _st(_D, 1, _HUGE + _S - 1, _HUGE + 3, 1)),
        (_st(_D, 1, _HUGE, _HUGE, 1), _st(_D, 1, _HUGE + _S, _HUGE, 1)),
        (_st(_D, 1, _HUGE, _HUGE, 1), _st(_D, 1, _HUGE - _S + 1, _HUGE - 1, 1)),
        (_st(_D, 1, -_HUGE, -_HUGE, 1), _st(_D, 1, -_HUGE + 5, -_HUGE - 5, 1)),
        (_st(_D, 1, -_HUGE, _HUGE, 1), _st(_D, 1, _HUGE, -_HUGE, 1)),
        (_st(_D, 1, -_HUGE - 7, 0, 1), _st(_D, 1, -_HUGE - 7, 0, 1)),
        (_st(_D, 1, 2**127, 2**127, 1), _st(_D, 1, 2**127 + 1, 2**127, 1)),
        (_st(_D, 1, 2**130 * _S, 0, 1), _st(_D, 1, 2**130 * _S + 999_999_999, 1, 1)),
        (_st(_D, 1, -(2**127), 0, 1), _st(_D, 1, 2**127 - 1, 0, 1)),
        (_st(_D, 1, -(2**127), 0, 1), _st(_D, 1, -(2**127) + 999_999_999, 0, 1)),
        (_st(_D, 1, 2**126, 0, 1), _st(_D, 1, -(2**126), 0, 1)),
        (_st(_D, 1, 0, 0, _HUGE), _st(_D, 1, 0, 0, _HUGE)),
    ]
    for x, y in pairs:
        out.append({"f": "same_file", "a": x, "b": y})
    out.append({"f": "same_files", "a": [], "b": [a]})
    out.append(
        {
            "f": "same_files",
            "a": [_st(_X, 1, 0, 0, 0), a],
            "b": [_st(_D, 7, 5 * _S, 6 * _S, 10)],
        }
    )
    out.append({"f": "same_files", "a": [_st(_X, 1, 0, 0, 0)], "b": [a]})
    ticked = [
        [],
        [_st(_D, 1, 100, 200, 0)],
        [_st(_D, 1, _S, 2 * _S, 0)],
        [_st(_D, 1, _S, 200, 0)],
        [_st(_D, 1, 150, 200, 0)],
        [_st(_D, 1, 100, 201, 0)],
        [_st(_D, 1, -100, -_S, 0)],
        [_st(_D, 1, -150, -_S, 0)],
        [_st(_D, 1, _HUGE + 100, _HUGE, 0)],
        [_st(_D, 1, _HUGE, _HUGE, 0)],
        [_st(_D, 1, _HUGE + 1, _HUGE, 0)],
        [_st(_D, 1, -_HUGE - 300, 0, 0)],
        [_st(_D, 1, -_HUGE - 301, 0, 0)],
        [_st(_D, 1, _S, _S, 0), _st(_X, 1, 100, 100, 0)],
        [_st(_D, 1, _S, _S, 0), _st(_X, 1, 10 * _S, 0, 0)],
        [_st(_D, 1, 0, 0, 0)],
    ]
    for s in ticked:
        out.append({"f": "ticked", "a": s})
    return out


def _tick_cases() -> list[dict[str, Any]]:
    fine = [_st(_D, 1, 5 * _S + 123, 6 * _S + 7, 10)]
    tick = [_st(_D, 1, 5 * _S + 100, 6 * _S + 200, 10)]
    tick2 = [_st(_D, 2, 5 * _S + 300, 6 * _S + 200, 10)]
    out: list[dict[str, Any]] = []
    for lx in (None, True, False):
        for kx in (None, True, False):
            for ks, ls in ((fine, fine), (tick, fine), (fine, tick), (tick, tick)):
                out.append(
                    {
                        "f": "in_ticks",
                        "k": {"s": ks, "x": kx},
                        "loc": {"s": ls, "x": lx},
                    }
                )
    for cx in (None, True, False):
        for cs, ks, ls in (
            (tick, tick, fine),
            (tick, fine, tick),
            (tick, fine, fine),
            (tick2, tick, tick),
            (fine, tick, tick),
            ([], [], []),
        ):
            out.append(
                {
                    "f": "carried",
                    "c": {"s": cs, "x": cx},
                    "k": {"s": ks},
                    "loc": {"s": ls},
                }
            )
    return out


def _view_cases() -> list[dict[str, Any]]:
    a = _st(_D, 1, 5 * _S + 123, 6 * _S + 7, 10)
    b = _st(_X, 2, 5 * _S + 500, 6 * _S + 9, 3)
    a_ct = _st(_D, 1, 5 * _S + 123, 9 * _S + 7, 10)
    a_ino = _st(_D, 77, 5 * _S + 123, 6 * _S + 7, 10)
    a_coarse = _st(_D, 77, 5 * _S, 6 * _S, 10)
    rem = _remint(_TOKEN, "40", "50")
    rem_other = _remint(_TOKEN, "41", "50")
    rem_own = _remint(_TOKEN, "10", "20")
    k = {"m": "m1", "t": _TOKEN, "s": [a, b], "o": ["10", "20"]}
    out: list[dict[str, Any]] = []
    for kk, ll in (
        ([a, b], [a, b]),
        ([a, b], [a]),
        ([a], [a, b]),
        ([a], [a_ino]),
        ([a], [a_ct]),
        ([a, b], [a_ct, b]),
        ([a, b], [a_ct]),
        ([], [a]),
        ([a], []),
        ([], []),
    ):
        kc = dict(k, s=kk)
        out.append({"f": "read_here", "k": kc, "loc": {"s": ll}})
        out.append({"f": "moved_under", "k": kc, "loc": {"s": ll}})
    carriers_sets: list[Any] = [
        [],
        [{"m": "m2", "t": rem, "s": [a_ino], "o": ["40", "50"]}],
        [{"m": "m1", "t": rem, "s": [a_ino], "o": ["40", "50"]}],
        [{"m": "m2", "t": rem, "s": [a_ino], "o": None}],
        [{"m": "m2", "t": rem, "s": [b], "o": ["40", "50"]}],
        [{"m": "m2", "t": _TOKEN, "s": [a_ino], "o": ["40", "50"]}],
        [{"m": "m2", "t": _TOKEN, "s": [a_ino], "o": ["41", "50"]}],
        [{"m": "m2", "t": _TOKEN, "s": [a_coarse], "o": ["40", "50"]}],
        [{"m": "m2", "t": _TOKEN, "s": [a_ino], "o": ["x", "50"]}],
        [{"m": "m2", "t": _OTHER, "s": [a_ino], "o": ["40", "50"]}],
        [{"m": "m2", "t": None, "s": [a_ino], "o": ["40", "50"]}],
        [
            {"m": "m2", "t": _OTHER, "s": [b], "o": ["40", "50"]},
            {"m": "m3", "t": rem, "s": [a], "o": ["41", "50"]},
        ],
        [{"m": "m2", "t": rem, "s": [a_ct], "o": ["40", "50"]}],
        [{"m": "m2", "t": rem, "s": [a_ct, a_ino], "o": ["40", "50"]}],
        [
            {
                "m": "m2",
                "t": rem,
                "s": [_st(_D, 9, 5 * _S + 100, 6 * _S + 200, 10)],
                "x": None,
                "o": ["40", "50"],
            }
        ],
        [{"m": "m2", "t": rem, "s": [a_ino], "x": True, "o": ["40", "50"]}],
    ]
    founds = [rem, rem_other, rem_own]
    for cs in carriers_sets:
        for found in founds:
            out.append({"f": "made_by", "k": k, "found": found, "carriers": cs})
    out.append(
        {
            "f": "made_by",
            "k": dict(k, t=None),
            "found": rem,
            "carriers": carriers_sets[5],
        }
    )
    out.append(
        {"f": "made_by", "k": dict(k, t=""), "found": rem, "carriers": carriers_sets[5]}
    )
    out.append(
        {
            "f": "made_by",
            "k": dict(k, t=_THIRD),
            "found": rem,
            "carriers": carriers_sets[5],
        }
    )
    locs = [
        {"s": [a, b]},
        {"s": [a_ct, b]},
        {"s": [a_ct]},
        {"s": [b]},
        {"s": [a_ino]},
        {"s": []},
        {"s": [a, b], "x": True},
        {"s": [a, b], "x": False},
    ]
    as_now: list[Any] = [
        [],
        [{"m": "m2", "t": rem, "s": [a_ct, b]}],
        [{"m": "m2", "t": rem, "s": [a_ct]}],
        [{"m": "m1", "t": rem, "s": [a_ct, b]}],
        [{"m": "m2", "t": _OTHER, "s": [a_ct, b]}],
        [{"m": "m2", "t": rem, "s": [_st(_D, 5, 5 * _S + 123, 9 * _S + 7, 10), b]}],
    ]
    for lc in locs[:5]:
        for cs in as_now[:5]:
            out.append(
                {"f": "read_as_now", "k": k, "loc": lc, "found": rem, "carriers": cs}
            )
    for lc in locs:
        for cs in as_now[:3] + carriers_sets[1:3] + carriers_sets[14:]:
            out.append(
                {"f": "continues", "k": k, "loc": lc, "found": rem, "carriers": cs}
            )
    ks: list[Any] = [
        k,
        dict(k, t=None),
        dict(k, t=""),
        dict(k, s=[]),
        dict(k, x=True),
        dict(k, t="not-a-token"),
    ]
    fs: list[Any] = [rem, None, "", _TOKEN, rem.lower(), 5]
    for kc in ks:
        for found in fs:
            for lc in (locs[0], locs[1]):
                out.append(
                    {
                        "f": "shared_remint",
                        "k": kc,
                        "loc": lc,
                        "found": found,
                        "carriers": carriers_sets[1] + as_now[1],
                    }
                )
            out.append(
                {
                    "f": "view_made",
                    "k": kc,
                    "found": found,
                    "carriers": carriers_sets[1],
                }
            )
    out.append(
        {
            "f": "shared_remint",
            "k": dict(k, t=_OTHER),
            "loc": locs[0],
            "found": _TOKEN,
            "carriers": [],
        }
    )
    out.append(
        {"f": "view_made", "k": dict(k, t=_OTHER), "found": _TOKEN, "carriers": []}
    )
    # Every stamp read here but its ctime, none exactly: the carriers are
    # reached through `_moved_under` alone.
    out.append(
        {
            "f": "shared_remint",
            "k": dict(k, s=[a]),
            "loc": {"s": [a_ct], "x": False},
            "found": rem,
            "carriers": [{"m": "m2", "t": rem, "s": [a_ct], "o": ["40", "50"]}],
        }
    )
    return out


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "kind": "remote_diverged",
        "state": "answered",
        "answer": "new",
        "answered_by": "declaration:os:u",
    }
    base.update(kw)
    return base


def _question_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    kinds: list[Any] = [
        "first_sight",
        "file_contradiction",
        "duplicate_checkout",
        "remote_diverged",
        "subproject_contradiction",
        "legacy",
        "import",
        "other",
        None,
        3,
        ["first_sight"],
        {"a": 1},
        "First_sight",
    ]
    for kind in kinds:
        for state in ("open", "answered"):
            out.append({"f": "asked_where", "rows": [_row(kind=kind, state=state)]})
    answers: list[Any] = [
        "new",
        "join",
        "rebind",
        "join:p2",
        "join:",
        "joi",
        "keep",
        "settled:k2",
        "",
        None,
        0,
        0.0,
        ["new"],
        "New",
        " new",
    ]
    for answer in answers:
        out.append({"f": "asked_where", "rows": [_row(answer=answer)]})
    bys: list[Any] = [
        "declaration:os:u",
        "declaration:",
        "declaration",
        "rule",
        None,
        "",
        0,
        0.0,
        ["declaration:"],
        "Declaration:x",
        " declaration:x",
    ]
    for by in bys:
        out.append({"f": "asked_where", "rows": [_row(answered_by=by)]})
    states: list[Any] = ["answered", "ANSWERED", None, 1, ["answered"]]
    for state in states:
        out.append({"f": "asked_where", "rows": [_row(state=state)]})
    out.append({"f": "asked_where", "rows": []})
    out.append({"f": "asked_where", "rows": [{"state": "open"}]})
    out.append({"f": "asked_where", "rows": [{"kind": "other"}]})
    out.append({"f": "asked_where", "rows": [{"kind": "first_sight"}]})
    out.append({"f": "asked_where", "rows": [{"kind": "other", "state": "open"}]})
    out.append({"f": "asked_where", "rows": [{"kind": "other", "state": "answered"}]})
    out.append(
        {
            "f": "asked_where",
            "rows": [{"kind": "other", "state": "answered", "answer": "keep"}],
        }
    )
    out.append(
        {
            "f": "asked_where",
            "rows": [{"kind": "other", "state": "answered", "answer": "new"}],
        }
    )
    out.append({"f": "asked_where", "rows": [_row(kind="first_sight"), {"x": 1}]})
    out.append({"f": "asked_where", "rows": [{"x": 1}, _row(kind="first_sight")]})
    out.append({"f": "asked_where", "rows": [_row(answer="keep"), _row(kind=["x"])]})
    out.append({"f": "asked_where", "rows": [_row(answer="keep"), _row()]})
    out.append(
        {
            "f": "asked_where",
            "rows": [_row(kind="legacy"), _row(answer=["x"]), _row(kind=[])],
        }
    )
    out.append(
        {
            "f": "asked_where",
            "rows": [_row(answer="new", answered_by="declaration:" + "x" * 50)],
        }
    )
    return out


def _log_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    reg = "checkout_registered"
    rules: list[Any] = [
        "join:T",
        "join:A+T",
        "join:T+B",
        "join:A+B",
        "join:",
        "join",
        "join:t",
        "join:AT",
        "new:T",
        "T",
        None,
        "",
        0,
        ["join:T"],
        "join:A:T",
        "join:B+T+C",
        "join:+T",
        "Join:T",
    ]
    import json

    for rule in rules:
        out.append(
            {
                "f": "joined",
                "checkout": "k1",
                "log": [[reg, json.dumps({"checkout": "k1", "rule": rule})]],
            }
        )
    payloads: list[Any] = [
        "{",
        "",
        "not json",
        "[1, 2]",
        "null",
        '"k1"',
        "1",
        '{"checkout": "k2", "rule": "join:T"}',
        '{"checkout": 1, "rule": "join:T"}',
        '{"rule": "join:T"}',
        '{"checkout": "k1", "checkout": "k2", "rule": "join:T"}',
        '{"checkout": "k2", "checkout": "k1", "rule": "join:T"}',
        '{"checkout": "k1", "rule": "join:T", "principal": "os:u"}',
        '{"checkout": "k1", "rule": "join:T", "principal": ""}',
        '{"checkout": "k1", "rule": "join:T", "principal": 0}',
        '{"checkout": "k1", "rule": "join:T", "principal": []}',
        '{"checkout": "k1", "rule": "join:T", "evidence": "declaration:os:u"}',
        '{"checkout": "k1", "rule": "join:T", "evidence": "declaration"}',
        '{"checkout": "k1", "rule": "join:T", "evidence": ["declaration:"]}',
        '{"checkout": "k1", "rule": "join:T", "witnesses": ["T", "declaration"]}',
        '{"checkout": "k1", "rule": "join:T", "witnesses": "declaration"}',
        '{"checkout": "k1", "rule": "join:T", "witnesses": [["declaration"]]}',
        '{"checkout": "k1", "rule": "join:T", "witnesses": {"declaration": 1}}',
        '{"checkout": "k1", "rule": "join:T", "x": NaN}',
        '{"checkout": "k1", "rule": "join:T", "x": 1e999}',
        '{"checkout": "k1", "rule": "join:T"} x',
        '{"checkout": "k\\u0031", "rule": "join:T"}',
        '{"checkout": "k1", "rule": "join:\\u0054"}',
    ]
    for p in payloads:
        out.append({"f": "joined", "checkout": "k1", "log": [[reg, p]]})
    good = '{"checkout": "k1", "rule": "join:T"}'
    bad = '{"checkout": "k1", "rule": "join:A"}'
    decl = '{"checkout": "k1", "principal": "os:u"}'
    seqs: list[list[Any]] = [
        [[reg, good], [reg, bad]],
        [[reg, bad], [reg, good]],
        [[reg, good], ["resolution_joined", decl]],
        [["resolution_joined", decl], [reg, good]],
        [["resolution_joined", good]],
        [[reg, good], ["project_absorbed", bad]],
        [[reg, good], ["other_kind", '{"checkout": "k1", "rule": "join:A"}']],
        [[reg, good], ["project_file_bound", "{"]],
        [[reg, {"rep": ['{"checkout": "k1", "x": ', "9", 5000, "}"]}], [reg, good]],
        [
            [
                reg,
                {"rep": ['{"checkout": "k1", "rule": "join:T", "x": ', "[", 500, ""]},
            ],
            [reg, good],
        ],
        [
            [reg, good],
            [
                reg,
                {"rep": ['{"checkout": "k1", "evidence": ', "[", 900, "]" * 900 + "}"]},
            ],
        ],
        [[reg, {"rep": ['{"checkout": "k1", "rule": "join:', "A+", 3000, 'T"}']}]],
    ]
    for s in seqs:
        out.append({"f": "joined", "checkout": "k1", "log": s})
    out.append(
        {
            "f": "joined",
            "checkout": "",
            "log": [[reg, '{"checkout": "", "rule": "join:T"}']],
        }
    )
    out.append({"f": "joined", "checkout": "k1", "log": []})
    out.append(
        {
            "f": "joined",
            "checkout": "k1",
            "log": [[reg, '{"checkout": "k1", "principal": 0.0, "rule": "join:T"}']],
        }
    )
    return out


def _deep_cases() -> list[dict[str, Any]]:
    """Log rows whose payload nests 200,000 arrays deep.

    `json.loads` stops where the interpreter stops recursing: 3.11, 3.12
    and 3.13 raise RecursionError, and so does 3.14 on macOS, where 3.14
    on Linux reads the whole text off its deeper main-thread stack.
    """
    reg = "checkout_registered"
    good = '{"checkout": "k1", "rule": "join:T"}'
    decl = '{"checkout": "k1", "principal": "os:u"}'
    deep = {"rep": ["[", "[", 200000, ""]}
    seqs: list[list[Any]] = [
        [[reg, good], [reg, deep]],
        [[reg, deep], ["resolution_joined", decl]],
    ]
    return [{"f": "joined", "checkout": "k1", "log": s} for s in seqs]


def _asks_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind in ("import", "legacy", "first_sight", "other"):
        for ev in (
            {},
            {"imported_project": "p2"},
            {"unresolved_project": "p3"},
            {"imported_project": None},
            {"imported_project": 0},
            {"imported_project": ["p"]},
            {"": "empty key"},
            {"imported_project": 1.5},
            {"imported_project": 0.0},
        ):
            out.append({"f": "asks", "kind": kind, "evidence": ev})
    out.append({"f": "asks", "kind": "import", "evidence": ["imported_project"]})
    return out


class _Lcg:
    def __init__(self, seed: int) -> None:
        self.x = seed

    def next(self, bound: int) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) % 2**64
        return (self.x >> 33) % bound


_TIMES = [
    0,
    100,
    _S,
    _S + 1,
    _S + 100,
    2 * _S,
    2 * _S - 1,
    3 * _S + 500,
    -_S,
    -100,
    _HUGE,
    _HUGE + 100,
    _HUGE + _S - 1,
    -_HUGE,
    2**127,
    2**127 + 100,
]


def _draw(rng: _Lcg) -> list[list[Any]]:
    out = []
    for _ in range(rng.next(3) + 1):
        out.append(
            [
                [_D, _X, "commondir"][rng.next(3)],
                rng.next(2) + 1,
                _TIMES[rng.next(len(_TIMES))],
                _TIMES[rng.next(len(_TIMES))],
                rng.next(2),
            ]
        )
    return out


def _sweep_line(a: list[list[Any]], b: list[list[Any]]) -> str:
    sa = [tuple(s) for s in a]
    sb = [tuple(s) for s in b]
    ka = types.SimpleNamespace(stamps=tuple(sa), ticks=None)
    lb = types.SimpleNamespace(stamps=tuple(sb), ticks=None)
    bits = [
        ms._same_files(sa, sb),
        ms._read_here(ka, lb),  # type: ignore[arg-type]
        ms._moved_under(ka, lb),  # type: ignore[arg-type]
        ms._ticked(sa),
        ms._in_ticks(ka, lb),  # type: ignore[arg-type]
        ms._carried_in_ticks(ka, lb, lb),  # type: ignore[arg-type]
        bool(ms._exact(sa) & ms._exact(sb)),
    ]
    return "".join("1" if x else "0" for x in bits)


def _digest() -> dict[str, Any]:
    rng = _Lcg(_SEED)
    lines = []
    for _ in range(_SWEEP):
        a = _draw(rng)
        b = _draw(rng)
        lines.append(_sweep_line(a, b))
    o = hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()
    return {"f": "sweep", "seed": _SEED, "n": _SWEEP, "o": o}


def generate() -> dict[str, bytes]:
    cases = [
        _run(c)
        for c in _stamp_cases()
        + _tick_cases()
        + _view_cases()
        + _question_cases()
        + _log_cases()
        + _asks_cases()
    ]
    header = {"f": "header", "cases": len(cases)}
    return {"membership_shared.jsonl": jsonl([header, *cases, _digest()])}


def generate_python() -> dict[str, bytes]:
    cases = [_run(c) for c in _deep_cases()]
    header = {"f": "header", "cases": len(cases)}
    return {"membership_shared_python.jsonl": jsonl([header, *cases])}
