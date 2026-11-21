"""Fixtures: the membership model, unit membership-2b (`membership.py`):
the machine rows (`record_machine`, `machines`) and the proposals
(`_open_proposals`, `proposal`, `_put_proposal`, `_open_proposal`,
`_close`, `open_proposal`, `restate_proposal`).

The reference model runs over `gen_membership_core`'s recording store,
with the ``machine`` and ``proposal`` kinds added. One file, not gated,
``membership_proposals.jsonl``: a sequence line ``{"seq", "init",
"tables", "fail", "steps", "after"}`` per scripted sequence, each step
``{"op", "a", "o", "log", "rows"}`` spelled as `gen_membership_core`
spells its steps.
"""

from __future__ import annotations

from typing import Any

from bettermemory import machine as mach
from bettermemory import membership as mm
from bettermemory import membership_rows as rows

from . import gen_membership_core as core
from ._common import jsonl
from .gen_membership_rows import _PROPOSAL, dec, enc

_MORE = {"machine": "machines", "proposal": "proposals"}


class _Stub(core._Stub):
    def put_membership(self, kind: str, row: Any, *, session: Any = None) -> Any:
        if kind not in _MORE:
            return super().put_membership(kind, row, session=session)
        self._note("put", kind, dict(row), session)
        rows_ = self.tables.setdefault(_MORE[kind], [])
        for i, r in enumerate(rows_):
            if r.get("id") == row["id"]:
                rows_[i] = dict(row)
                break
        else:
            rows_.append(dict(row))
        return self._written()


def _facts(a: Any) -> mach.MachineFacts | None:
    if a is None:
        return None
    idf = a["id_file"]
    return mach.MachineFacts(
        hardware=a["hardware"],
        hostname=a["hostname"],
        id_file=None if idf is None else tuple(idf),
    )


def _machine(x: mm.Machine) -> Any:
    return [x.id, x.facts, x.predecessor, x.minted]


def _line(a: Any) -> Any:
    if isinstance(a, dict) and "of" in a:
        prefix = a["of"]
        return lambda q: f"{prefix}{q}"
    return a


def _opening(a: Any) -> dict[str, Any]:
    return {
        "checkout": a["checkout"],
        "kind": a["kind"],
        "candidates": tuple(a["candidates"]),
        "evidence": dec(a["evidence"]),
        "reason": a["reason"],
        "line": _line(a["line"]),
        "distinct": a.get("distinct"),
    }


def _answer(m: mm.Membership, op: str, a: Any) -> Any:  # noqa: PLR0911
    if op == "record_machine":
        facts = _facts(a["facts"])
        assert facts is not None
        m.record_machine(
            mach.MachineIdentity(
                id=a["id"],
                facts=facts,
                predecessor=a["predecessor"],
                minted=True,
                cloned=False,
                facts_updated=False,
                recorded=_facts(a["recorded"]),
            )
        )
        return None
    if op == "machines":
        return [_machine(x) for x in m.machines()]
    if op == "open_proposals":
        return [rows._proposal_row(p) for p in m._open_proposals(a)]
    if op == "proposal":
        return rows._proposal_row(m.proposal(a))
    if op == "put_proposal":
        m._put_proposal(rows._proposal_from(dec(a)))
        return None
    if op == "open_proposal":
        return rows._proposal_row(m.open_proposal(**_opening(a)))
    if op == "open_signed":
        sig = a["signature"]
        got = m._open_proposal(
            **_opening(a), signature=None if sig is None else tuple(dec(sig))
        )
        return rows._proposal_row(got)
    if op == "close":
        pr = rows._proposal_from(dec(a["pr"]))
        m._close(pr, a["answer"], a["by"], said=a.get("said"))
        return None
    if op == "restate":
        got = m.restate_proposal(a["id"], line=a["line"], evidence=dec(a["evidence"]))
        return rows._proposal_row(got)
    raise ValueError(op)


def _step(m: mm.Membership, stub: _Stub, step: dict[str, Any]) -> dict[str, Any]:
    op: str = step["op"]
    a: Any = step.get("a")
    start = len(stub.log)
    out: dict[str, Any] = {"op": op, "a": a}
    try:
        out["o"] = enc(_answer(m, op, a))
    except Exception as e:  # noqa: BLE001
        out["o"] = core._raised(e)
    out["log"] = enc(stub.log[start:])
    out["rows"] = list(m._rows)
    return out


def _sequence(
    name: str,
    tables: dict[str, list[dict[str, Any]]],
    steps: list[dict[str, Any]],
    *,
    fail: Any = None,
    outer: bool = False,
    now: Any = None,
) -> dict[str, Any]:
    init: dict[str, Any] = {
        "now": now or ["2026-03-04T05:06:07.123456", 60],
        "session": "S1",
        "given": True,
    }
    stub = _Stub(tables, fail, outer)
    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        liveness=core._given,  # type: ignore[arg-type]
        now=lambda: core._now(init["now"]),
        session=init["session"],
    )
    return {
        "seq": name,
        "init": init,
        "tables": core._spelled(tables),
        "fail": fail,
        "outer": outer,
        "steps": [_step(m, stub, s) for s in steps],
        "after": core._sha(core._spelled(stub.tables)),
    }


_F1 = {"hardware": "HW1", "hostname": "host-a", "id_file": ["1", "2", None]}
_F2 = {"hardware": "HW1", "hostname": "host-b", "id_file": ["1", "2", "9.5"]}
_F0 = {"hardware": None, "hostname": None, "id_file": None}


def _mrow(mid: str, facts: Any, pred: Any = None) -> dict[str, Any]:
    return {
        "id": mid,
        "facts_json": facts,
        "predecessor": pred,
        "minted": "2026-01-01Z",
    }


def _rm(mid: str, facts: Any, pred: Any = None, recorded: Any = None) -> Any:
    return {
        "op": "record_machine",
        "a": {"id": mid, "facts": facts, "predecessor": pred, "recorded": recorded},
    }


def _machines() -> list[dict[str, Any]]:
    reordered = {"id_file": ["1", "2", None], "hostname": "host-a", "hardware": "HW1"}
    stored = [
        _mrow("M1", _F1),
        _mrow("M2", reordered),
        _mrow(
            "M3", {"hardware": "HW1", "hostname": "host-a", "id_file": ("1", "2", None)}
        ),
        {
            "minted": 5,
            "extra": 1,
            "facts_json": {"a": 1},
            "id": "M4",
            "predecessor": "M0",
        },
        _mrow("M5", None),
        _mrow("M6", [["k", "v"], "ab"]),
        _mrow("M7", {"hardware": True, "hostname": None, "id_file": None}),
    ]
    out = [
        _sequence(
            "machines_new",
            {},
            [
                _rm("M1", _F1),
                _rm("M1", _F1),
                _rm("M2", _F2, "M1", _F1),
                _rm("M3", _F0, "M1", None),
                _rm("M4", _F2, "M3", None),
                _rm("M3", _F1),
                {"op": "machines"},
            ],
        ),
        _sequence(
            "machines_stored",
            {"machines": stored},
            [
                _rm("M1", _F1),
                _rm("M2", _F1),
                _rm("M3", _F1),
                _rm("M4", _F1, "M9"),
                _rm("M5", _F0),
                _rm("M7", {"hardware": "1", "hostname": None, "id_file": None}),
                {"op": "machines"},
            ],
        ),
        _sequence(
            "machines_bad",
            {"machines": [{"id": "M1"}]},
            [_rm("M1", _F1), {"op": "machines"}],
        ),
        _sequence(
            "machines_pairs",
            {"machines": [_mrow("M1", [["k", "v"], ["k", "w"]]), _mrow("M2", 0)]},
            [{"op": "machines"}],
        ),
        _sequence(
            "machines_badpairs",
            {"machines": [_mrow("M6", ["ab", "c"])]},
            [{"op": "machines"}],
        ),
        _sequence(
            "machines_overflow", {}, [_rm("M1", _F1)], now=["0001-01-01T00:00:00", 60]
        ),
    ]
    for fail in [["rows", 1], ["put", 1], ["record", 1], ["put", 2]]:
        out.append(
            _sequence(
                f"machines_fail_{fail[0]}_{fail[1]}",
                {"machines": [_mrow("M1", _F2)]},
                [_rm("M2", _F1, "M1", _F2), _rm("M1", _F1), {"op": "machines"}],
                fail=fail,
            )
        )
    out.append(
        _sequence(
            "machines_outer", {}, [_rm("M1", _F1, "M0")], fail=["record", 1], outer=True
        )
    )
    return out


def _q(qid: str, **kw: Any) -> dict[str, Any]:
    return {**_PROPOSAL, "id": qid, **kw}


_QTABLE = [
    _q("Q001"),
    _q("Q002", kind="legacy", evidence_json={"project": "A", "n": 1}),
    _q("Q003", kind="legacy", evidence_json={"project": "B"}),
    _q("Q004", state="answered", answer="P1", answered_by="user"),
    _q("Q005", checkout=None),
    _q("Q006", checkout="K2", kind="legacy", evidence_json={}),
    _q("Q007", line="tab\there", signature_json=None),
    _q("Qx"),
]


def _op(**kw: Any) -> dict[str, Any]:
    base = {
        "checkout": "K1",
        "kind": "first_sight",
        "candidates": ["P1"],
        "evidence": enc({"dir": "/w", "n": (1, 2), 3: "three"}),
        "reason": "why",
        "line": "the line",
    }
    return base | kw


def _proposals() -> list[dict[str, Any]]:
    reads: list[dict[str, Any]] = [
        {"op": "open_proposals", "a": None},
        {"op": "open_proposals", "a": "K1"},
        {"op": "open_proposals", "a": "K9"},
        {"op": "proposal", "a": "Q004"},
        {"op": "proposal", "a": "Q404"},
        {"op": "proposal", "a": "Q007"},
    ]
    opens: list[dict[str, Any]] = [
        {"op": "open_proposal", "a": _op()},
        {"op": "open_proposal", "a": _op(kind="legacy")},
        {"op": "open_proposal", "a": _op(kind="legacy", distinct="project")},
        {
            "op": "open_proposal",
            "a": _op(
                kind="legacy",
                distinct="project",
                evidence=enc({"project": "B", "x": 1}),
            ),
        },
        {
            "op": "open_proposal",
            "a": _op(kind="legacy", distinct="project", evidence=enc({"project": "C"})),
        },
        {
            "op": "open_proposal",
            "a": _op(kind="legacy", distinct="n", evidence=enc({"n": 1.0})),
        },
        {
            "op": "open_proposal",
            "a": _op(kind="legacy", distinct="zz", evidence=enc({})),
        },
        {
            "op": "open_proposal",
            "a": _op(
                checkout="K2", kind="legacy", distinct="q", evidence=enc({"q": None})
            ),
        },
        {"op": "open_proposal", "a": _op(checkout=None)},
        {"op": "open_proposal", "a": _op(checkout="K3", line={"of": "answer with "})},
        {
            "op": "open_proposal",
            "a": _op(checkout="K3", kind="other", candidates=[], line={"of": ""}),
        },
        {
            "op": "open_signed",
            "a": _op(checkout="K4", signature=enc(("h", ["r"], None, "t", False))),
        },
        {"op": "open_signed", "a": _op(checkout="K4", signature=None)},
        {"op": "open_signed", "a": _op(checkout="K5", signature=None)},
        {"op": "open_signed", "a": _op(checkout="K6", signature=enc(["h", 1.0]))},
    ]
    q1 = rows._proposal_row(rows._proposal_from(_q("Q001")))
    closes: list[dict[str, Any]] = [
        {"op": "close", "a": {"pr": enc(q1), "answer": "P1", "by": "user"}},
        {
            "op": "close",
            "a": {"pr": enc(q1), "answer": "P2", "by": "rule", "said": "yes"},
        },
        {
            "op": "close",
            "a": {"pr": enc({**q1, "id": "Q404"}), "answer": "P1", "by": "user"},
        },
        {
            "op": "close",
            "a": {
                "pr": enc({**q1, "id": "Q005", "checkout": "K7"}),
                "answer": "P1",
                "by": "u",
            },
        },
        {
            "op": "close",
            "a": {"pr": enc({**q1, "state": "answered"}), "answer": "P1", "by": "u"},
        },
        {
            "op": "close",
            "a": {"pr": enc({**q1, "state": "dismissed"}), "answer": "P1", "by": "u"},
        },
        {"op": "put_proposal", "a": enc(_q("Q900", evidence_json={"k": (1, 2)}))},
        {"op": "proposal", "a": "Q900"},
    ]
    restates: list[dict[str, Any]] = [
        {
            "op": "restate",
            "a": {
                "id": "Q001",
                "line": "a line",
                "evidence": enc({"n": 2, "dir": "/work/r"}),
            },
        },
        {
            "op": "restate",
            "a": {
                "id": "Q001",
                "line": "a line",
                "evidence": enc({"dir": "/work/r", "n": 2.0}),
            },
        },
        {
            "op": "restate",
            "a": {
                "id": "Q001",
                "line": "a line",
                "evidence": enc({"dir": "/work/r", "n": True}),
            },
        },
        {
            "op": "restate",
            "a": {
                "id": "Q001",
                "line": "new",
                "evidence": enc({"dir": "/work/r", "n": 2}),
            },
        },
        {
            "op": "restate",
            "a": {
                "id": "Q001",
                "line": "a line",
                "evidence": enc({"dir": "/work/r", "n": 3}),
            },
        },
        {
            "op": "restate",
            "a": {
                "id": "Q002",
                "line": "a line",
                "evidence": enc({"project": "A", "n": [1]}),
            },
        },
        {
            "op": "restate",
            "a": {"id": "Q002", "line": "x", "evidence": enc({1: (2, 3)})},
        },
        {
            "op": "restate",
            "a": {
                "id": "Q007",
                "line": "tab\there",
                "evidence": enc({"dir": "/work/r", "n": 2}),
            },
        },
        {"op": "restate", "a": {"id": "Q004", "line": "zzz", "evidence": enc({})}},
        {"op": "restate", "a": {"id": "Q404", "line": "zzz", "evidence": enc({})}},
        {"op": "open_proposals", "a": "K1"},
    ]
    out = [
        _sequence("proposals_reads", {"proposals": _QTABLE}, reads),
        _sequence("proposals_open", {"proposals": _QTABLE}, opens),
        _sequence("proposals_open_empty", {}, [opens[0], opens[0], opens[8], opens[8]]),
        _sequence("proposals_close", {"proposals": _QTABLE}, closes),
        _sequence("proposals_restate", {"proposals": _QTABLE}, restates),
        _sequence(
            "proposals_overflow", {}, [opens[0]], now=["0001-01-01T00:00:00", 60]
        ),
        _sequence(
            "proposals_badrow",
            {"proposals": [_q("Q001", evidence_json="xy"), _q("Q002", state=None)]},
            [
                {"op": "open_proposals", "a": None},
                {"op": "proposal", "a": "Q002"},
                opens[0],
            ],
        ),
    ]
    for fail in [["rows", 1], ["next_id", 1], ["put", 1], ["record", 1]]:
        out.append(
            _sequence(
                f"proposals_open_fail_{fail[0]}",
                {"proposals": _QTABLE},
                [opens[4], {"op": "open_proposals", "a": "K1"}],
                fail=fail,
            )
        )
    for fail in [["rows", 1], ["put", 1], ["record", 1]]:
        out.append(
            _sequence(
                f"proposals_close_fail_{fail[0]}",
                {"proposals": _QTABLE},
                closes[1:2],
                fail=fail,
            )
        )
        out.append(
            _sequence(
                f"proposals_restate_fail_{fail[0]}",
                {"proposals": _QTABLE},
                restates[3:4],
                fail=fail,
            )
        )
    return out


def generate() -> dict[str, bytes]:
    return {"membership_proposals.jsonl": jsonl([*_machines(), *_proposals()])}
