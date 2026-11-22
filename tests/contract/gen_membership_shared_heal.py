"""Fixtures: the heal half of `membership_shared` (``membership_shared_heal.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/membership/shared_heal.rs``. This
pins `_carriers`, `_restamped`, `_healed`, `_chosen`, `retired_into` and
`_folded` over the real `Membership` on the recording store of
`gen_membership_core` (`_Stub`), given a ``membership_log`` that answers
the case's log rows whatever it is asked and records what it was asked.

Each case holds ``f`` (the function), ``tables`` (the store's rows,
spelled by `enc`), ``log`` (``[kind, payload]`` pairs), the arguments
(``k`` and ``later`` checkout rows, ``loc`` a sighting, ``how``,
``checkout``, ``tokens``), and what it gave: ``o`` the answer (a checkout
as its row) or ``{"raised": [class, text]}``, and ``calls`` the store
calls in order. A sighting is ``{"t": token, "w": token written, "s":
stamps, "x": ticks, "files": [[id, dir]]}`` at the root ``/w/r``; a stamp
is ``[name, inode, mtime, ctime, size]``. Not gated: no answer follows
the Python release or the system (no payload nests near the recursion
limit).
"""

from __future__ import annotations

from typing import Any

from bettermemory import checkout_token as ct
from bettermemory import membership as mm
from bettermemory import membership_shared as ms
from bettermemory.membership_records import InitStamp, ProjectFileRef
from bettermemory.log import LogRow
from bettermemory.membership_rows import _checkout_from, _checkout_row

from ._common import jsonl
from .gen_membership_core import _now, _raised, _spelled, _Stub
from .gen_membership_rows import _CHECKOUT, _PROJECT, enc

_NONCE = "0123456789ABC"
_T = "0123456789ABCDEFGHJKMNPQRS"
_S = 1_000_000_000


def _remint(token: str, device: str, inode: str) -> str:
    return _NONCE + ct._link(ct.Lineage(token, device, inode), _NONCE)


_R = _remint(_T, "40", "50")
_OWN = _remint(_T, "16777220", "123")
_A = ["description", 7, 5 * _S + 123, 6 * _S + 7, 10]
_A_THERE = ["description", 99, 5 * _S + 123, 6 * _S + 7, 10]
_A_CT = ["description", 7, 5 * _S + 123, 6 * _S + 9, 10]
_B = ["info/exclude", 8, 3 * _S + 1, 4 * _S + 1, 3]


class _LogStub(_Stub):
    def __init__(self, tables: Any, log: list[list[str]]) -> None:
        super().__init__(tables, None, False)
        self.log_rows = log

    def membership_log(
        self, kinds: Any = None, *, since_seq: int = 0, checkout: Any = None
    ) -> list[Any]:
        self._note("log", None if kinds is None else list(kinds), checkout)
        return [
            LogRow(i + 1, "t", None, k, p, "", "")
            for i, (k, p) in enumerate(self.log_rows)
        ]


def _k(kid: str, project: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        _CHECKOUT, id=kid, project=project, token=_T, object_birth=None
    )
    base["stamps_json"] = [_A]
    return base | kw


def _st(stamps: list[Any], ticks: Any = None) -> list[Any]:
    return [*stamps, {"ticks": ticks}] if ticks is not None else list(stamps)


def _loc(spec: dict[str, Any]) -> Any:
    return mm.Location(
        machine="M1",
        cwd="/w/r",
        state="repo",
        root="/w/r",
        kind="primary",
        token=spec.get("t"),
        token_written=spec.get("w"),
        token_status="present",
        stamps=tuple(InitStamp(*s) for s in spec.get("s", [])),
        ticks=spec.get("x"),
        files=tuple(
            ProjectFileRef(f[0], "n", f[1], f[1] + "/bettermemory.toml")
            for f in spec.get("files", [])
        ),
    )


def _run(case: dict[str, Any]) -> dict[str, Any]:
    stub = _LogStub(case.get("tables", {}), case.get("log", []))
    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        now=lambda: _now(["2026-03-04T05:06:07", 0]),
    )
    f = case["f"]
    out = dict(case)
    out["tables"] = _spelled(case.get("tables", {}))
    for key in ("k", "later"):
        if key in out:
            out[key] = enc(out[key])
    try:
        k = _checkout_from(case["k"], ()) if "k" in case else None
        later = _checkout_from(case["later"], ()) if "later" in case else None
        loc = _loc(case["loc"]) if "loc" in case else None
        if f == "carriers":
            o: Any = [_checkout_row(c) for c in ms._carriers(m, case["tokens"])]
        elif f == "restamped":
            o = _checkout_row(ms._restamped(m, k, loc, case["how"]))  # type: ignore[arg-type]
        elif f == "healed":
            o = ms._healed(m, k, later, loc)  # type: ignore[arg-type]
        elif f == "chosen":
            o = ms._chosen(m, k, later, loc)  # type: ignore[arg-type]
        elif f == "retired_into":
            o = ms.retired_into(m, k)  # type: ignore[arg-type]
        elif f == "folded":
            o = sorted(ms._folded(m, case["checkout"]))
        else:
            raise AssertionError(f)
        out["o"] = enc(o)
    except Exception as e:  # noqa: BLE001
        out["o"] = _raised(e)
    out["calls"] = enc(stub.log)
    return _z(out)


def _bases() -> dict[str, Any]:
    return {r["id"]: enc(r) for r in (_KEPT, _LATER, _THERE)}


def _z(v: Any) -> Any:
    """A checkout row spelled ``{"$k": [base id, {column: value}]}``: the
    header's row of that id (or ``K1``) with the columns that differ,
    where the row has the base's columns in its order."""
    if isinstance(v, list):
        return [_z(x) for x in v]
    if not isinstance(v, dict):
        return v
    pairs = v.get("$d")
    if isinstance(pairs, list) and len(v) == 1:
        bases = _bases()
        keys = [p[0] for p in pairs]
        ids = [p[1] for p in pairs if p[0] == "id"]
        base = bases.get(ids[0] if ids and isinstance(ids[0], str) else "", bases["K1"])
        if keys == [p[0] for p in base["$d"]]:
            was = dict((k, x) for k, x in base["$d"])
            diff = {k: x for k, x in pairs if was[k] != x}
            return {"$k": [base["$d"][0][1], diff]}
    return {k: _z(x) for k, x in v.items()}


_KEPT = _k("K1", "P1")
_LATER = _k("K2", "P2", token=_R)
_THERE = _k(
    "K3",
    "P2",
    machine="M2",
    token=_R,
    object_device="40",
    object_inode="50",
    stamps_json=_st([_A_THERE], False),
)
_JOIN = ["checkout_registered", '{"checkout": "K2", "rule": "join:T"}']
_LOC = {"t": _R, "s": [_A], "x": False}


def _heal(**change: Any) -> dict[str, Any]:
    tables: dict[str, Any] = {"checkouts": [_KEPT, _LATER, _THERE]}
    case: dict[str, Any] = {
        "f": "healed",
        "k": _KEPT,
        "later": _LATER,
        "loc": _LOC,
        "log": [_JOIN],
    }
    for key, v in change.items():
        if key in ("checkouts", "projects", "homes", "proposals", "project_files"):
            tables[key] = v
        else:
            case[key] = v
    case["tables"] = tables
    return case


def _p(pid: str, **kw: Any) -> dict[str, Any]:
    return dict(_PROJECT, id=pid, name=pid.lower()) | kw


def _heal_cases() -> list[dict[str, Any]]:
    cases = [
        _heal(),
        _heal(homes=[{"checkout": "K2", "project": "P2"}]),
        _heal(homes=[{"checkout": "K9", "project": "P2"}]),
        _heal(later=dict(_LATER, project="P1"), homes=[{"checkout": "K2"}]),
        _heal(
            later=dict(_LATER, project="P1"),
            checkouts=[_KEPT, _LATER, dict(_THERE, project="P1")],
            homes=[{"checkout": "K2"}],
        ),
        _heal(loc=dict(_LOC, t=None, w=_R)),
        _heal(loc=dict(_LOC, t="", w=_R)),
        _heal(loc=dict(_LOC, t=None, w=None)),
        _heal(
            later=dict(_LATER, token_status="superseded"), loc=dict(_LOC, t=None, w=_R)
        ),
        _heal(loc=dict(_LOC, t=_T)),
        _heal(later=dict(_LATER, token=None)),
        _heal(k=dict(_KEPT, token=None)),
        _heal(k=dict(_KEPT, token="")),
        _heal(k=dict(_KEPT, stamps_json=[])),
        _heal(later=dict(_LATER, token=_T), loc=dict(_LOC, t=_T)),
        _heal(later=dict(_LATER, token=_OWN), loc=dict(_LOC, t=_OWN)),
        _heal(loc=dict(_LOC, s=[_B])),
        _heal(loc=dict(_LOC, s=[_A_CT])),
        _heal(
            loc=dict(_LOC, s=[_A_CT]),
            checkouts=[_KEPT, _LATER, dict(_THERE, stamps_json=_st([_A_CT], False))],
        ),
        _heal(loc=dict(_LOC, x=True)),
        _heal(loc=dict(_LOC, x=None)),
        _heal(checkouts=[_KEPT, _LATER]),
        _heal(checkouts=[_KEPT, _LATER, dict(_THERE, machine="M1")]),
        _heal(checkouts=[_KEPT, _LATER, dict(_THERE, project="P3")]),
        _heal(
            checkouts=[_KEPT, _LATER, dict(_THERE, project="P3")],
            projects=[_p("P2"), _p("P3", merged_into="P2")],
        ),
        _heal(
            later=dict(_LATER, project="P4"),
            projects=[_p("P1"), _p("P2"), _p("P4", merged_into="P1")],
        ),
        _heal(
            loc=dict(_LOC, files=[["F1", "/w/r"]]),
            project_files=[{"file_id": "F1", "project": "P9"}],
        ),
        _heal(
            loc=dict(_LOC, files=[["F1", "/w/r"]]),
            project_files=[{"file_id": "F1", "project": "P1"}],
        ),
        _heal(
            loc=dict(_LOC, files=[["F1", "/w/r/sub"]]),
            project_files=[{"file_id": "F1", "project": "P9"}],
        ),
        _heal(proposals=[{"checkout": "K2", "kind": "first_sight", "state": "open"}]),
        _heal(proposals=[{"checkout": "K2", "state": "open"}]),
        _heal(log=[]),
        _heal(log=[["checkout_registered", '{"checkout": "K2", "rule": "join:R+T"}']]),
        _heal(log=[["checkout_registered", '{"checkout": "K2", "rule": "join:R"}']]),
        _heal(log=[_JOIN, ["project_joined", '{"checkout": "K2", "principal": "u"}']]),
        _heal(log=[["checkout_registered", "{bad"], _JOIN]),
        _heal(checkouts=[_KEPT, _LATER, dict(_THERE, stamps_json="oops")]),
    ]
    return cases


def _restamp_cases() -> list[dict[str, Any]]:
    k = _k("K1", "P1", stamps_json=_st([_A, _B], False))
    out: list[dict[str, Any]] = []
    for how in ["birth", "token", "remint", "view", "", "Birth"]:
        out.append(
            {"f": "restamped", "k": k, "loc": {"s": [_A_CT], "x": False}, "how": how}
        )
    for loc in [
        {"s": []},
        {"s": [_A, _B]},
        {"s": [_B, _A]},
        {"s": [_B, _A, _B]},
        {"s": [_A, _B], "x": False},
        {"s": [_A, _B], "x": True},
        {"s": [_A]},
        {"s": [_A], "x": True},
        {"s": [_A, _B, _A_CT], "x": None},
    ]:
        out.append({"f": "restamped", "k": k, "loc": loc, "how": "birth"})
    for kt in [None, True]:
        kk = dict(k, stamps_json=_st([_A, _B], kt))
        for x in [None, True, False]:
            out.append(
                {
                    "f": "restamped",
                    "k": kk,
                    "loc": {"s": [_A, _B], "x": x},
                    "how": "token",
                }
            )
    return out


def _retired_cases() -> list[dict[str, Any]]:
    k = _k("K2", "P2", retired=1)
    logs: list[list[str]] = [
        [],
        ['{"into": "K1"}'],
        ['{"into": "K1"}', '{"into": null}'],
        ['{"into": "K1"}', "{bad"],
        ['{"into": "K1"}', "[1]"],
        ['{"into": "K1"}', '"K5"'],
        ['{"into": "K1"}', '{"other": 1}'],
        ['{"into": ""}'],
        ['{"into": 5}'],
        ['{"into": 0}'],
        ['{"into": true}'],
        ['{"into": false}'],
        ['{"into": 1.5}'],
        ['{"into": 0.0}'],
        ['{"into": -0.0}'],
        ['{"into": [1, "a"]}'],
        ['{"into": {"a": null}}'],
        ['{"into": []}'],
        ['{"into": "K1"}', '{"into": "K3"}'],
        ['{"into": "K1", "into": "K4"}'],
        ['{"into": "\\u00e9t\\u00e9"}'],
        ["NaN", '{"into": NaN}'],
        ['{"into": 1e400}'],
        ['{"into": "K1"} x'],
        ["[" * 50 + "]" * 50, '{"into": ' + "[" * 40 + "]" * 40 + "}"],
        ['{"into": ' + "1" * 4301 + "}", '{"into": "K7"}'],
    ]
    out = [
        {
            "f": "retired_into",
            "k": dict(k, retired=0),
            "log": [["checkout_retired", logs[1][0]]],
        }
    ]
    for lg in logs:
        out.append(
            {"f": "retired_into", "k": k, "log": [["checkout_retired", p] for p in lg]}
        )
    return out


def _folded_cases() -> list[dict[str, Any]]:
    def r(c: Any, into: Any) -> list[str]:
        import json

        return ["checkout_retired", json.dumps({"checkout": c, "into": into})]

    sets: list[tuple[str, list[list[str]]]] = [
        ("K1", []),
        ("K1", [r("K2", "K1")]),
        ("K1", [r("K2", "K1"), r("K3", "K2"), r("K4", "K3")]),
        ("K1", [r("K4", "K3"), r("K3", "K2"), r("K2", "K1")]),
        ("K1", [r("K2", "K1"), r("K1", "K2")]),
        ("K1", [r("K1", "K1")]),
        ("K1", [r("K2", "K1"), r("K3", "K1"), r("K5", "K9")]),
        ("K2", [r("K2", "K1"), r("K3", "K2"), r("K1", "K3")]),
        ("K1", [r("K2", "K1"), r("K2", None), r("K6", "")]),
        ("K1", [r(None, "K1"), r(5, "K1"), r(True, "K1")]),
        ("5", [r("K2", 5)]),
        (
            "K1",
            [r("K2", "K1"), ["checkout_retired", "{bad"], ["checkout_retired", "[1]"]],
        ),
        ("K1", [r("K2", "K1"), ["checkout_retired", '{"into": "K1"}']]),
        ("K1", [["checkout_retired", '{"into": "K9"}'], r("K2", "K1")]),
        ("K1", [["checkout_retired", '{"into": 0}'], r("K2", "K1")]),
        ("K1", [r("K2", "K1"), r(["a"], "K1"), r({"b": 1}, "K1")]),
        ("", [r("K2", "")]),
    ]
    return [{"f": "folded", "checkout": c, "log": lg} for c, lg in sets]


def _chosen_cases() -> list[dict[str, Any]]:
    base = {"f": "chosen", "k": _KEPT, "later": _LATER, "loc": _LOC}
    q = {
        "checkout": "K2",
        "state": "answered",
        "answer": "new",
        "answered_by": "declaration:u",
    }
    return [
        dict(base, log=[_JOIN]),
        dict(base, log=[]),
        dict(
            base, log=[_JOIN], tables={"proposals": [dict(q, kind="remote_diverged")]}
        ),
        dict(base, log=[_JOIN], tables={"proposals": [dict(q, kind="legacy")]}),
        dict(
            base, log=[_JOIN], tables={"proposals": [{"checkout": "K2", "kind": ["x"]}]}
        ),
        dict(
            base, log=[_JOIN], tables={"proposals": [{"checkout": "K2", "kind": "x"}]}
        ),
        dict(
            base,
            loc=dict(_LOC, files=[["F1", "/w/r"]]),
            log=[_JOIN],
            tables={"project_files": [{"file_id": "F1", "project": "P1"}]},
        ),
        dict(
            base,
            loc=dict(_LOC, files=[["F1", "/w/r"]]),
            log=[],
            tables={"project_files": [{"file_id": "F1"}]},
        ),
    ]


def _carrier_cases() -> list[dict[str, Any]]:
    tables = {"checkouts": [_KEPT, _LATER, _THERE, _k("K4", "P1", token=None)]}
    return [
        {"f": "carriers", "tokens": t, "tables": tables}
        for t in [[_T, _R], [_R, _T, _R], [], ["none"], [_R]]
    ]


def _cases() -> list[dict[str, Any]]:
    return [
        *_carrier_cases(),
        *_restamp_cases(),
        *_heal_cases(),
        *_chosen_cases(),
        *_retired_cases(),
        *_folded_cases(),
    ]


def generate() -> dict[str, bytes]:
    header = {"f": "header", "base": _bases()}
    return {
        "membership_shared_heal.jsonl": jsonl([header, *(_run(c) for c in _cases())])
    }
