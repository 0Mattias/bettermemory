"""Fixtures: the asking half of `membership_shared` (``membership_shared_ask.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/membership/shared_ask.rs``. This
pins `_ask_which`, `_retire_into`, `_asked_at`, `_asked_here`, `_unseen`
and `_asked_plan` over the real `Membership` on the recording store of
`gen_membership_shared_heal` (`_LogStub`), with a fixed clock and a
liveness that answers ``live`` for the case's paths and ``gone`` for any
other.

Each case holds ``f`` (the function), ``tables`` (the store's rows,
spelled by `enc`), ``log`` (``[kind, payload]`` pairs that
``membership_log`` answers), ``live`` (the live paths), ``clock`` (the
clock as ``[text, minutes]``, or ``[text, null]`` for a naive one), the
arguments as ids (``k``, ``first``, ``idle``, ``kept``, ``settled``,
``pr``), ``by`` and ``projects``, and what it gave: ``o`` the answer
(a list of checkout ids for `_asked_plan`) or ``{"raised": [class,
text]}``, and ``calls`` the store calls the function made, in order. The
checkouts and the question an argument names are read through
`Membership.checkout` and `Membership.proposal` before the function
runs, and those reads are not recorded. A checkout row in ``calls`` is
spelled as the header's row of its id with the columns that differ
(``$k``).

`_asked_plan` adds a heal's checkouts in the order of a dict built from a
set of project ids, which follows the string hash: every case gives the
heal one such project, so the answer's order is the reference's on every
run. Not gated: no answer follows the Python release or the system.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import membership as mm
from bettermemory import membership_shared as ms

from ._common import jsonl
from .gen_membership_core import _raised, _spelled
from .gen_membership_rows import _CHECKOUT, _PROJECT, _PROPOSAL, enc
from .gen_membership_shared_heal import _LogStub

_NOW = ["2026-03-04T05:06:07", 0]
#: A sighting inside `RETIRE_AFTER` of `_NOW`, one past it, and the bound.
_RECENT = "2026-01-01T00:00:00Z"
_OLD = "2025-01-01T00:00:00Z"
_EDGE = "2025-09-05T05:06:07Z"


def _k(kid: str, project: str, **kw: Any) -> dict[str, Any]:
    return dict(_CHECKOUT, id=kid, project=project, token=None) | kw


def _v(kid: str, path: str, machine: str = "M1", **kw: Any) -> dict[str, Any]:
    return {
        "checkout": kid,
        "machine": machine,
        "path": path,
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": _RECENT,
        "seen_order": 1,
    } | kw


def _p(pid: str, **kw: Any) -> dict[str, Any]:
    return dict(_PROJECT, id=pid, name=pid.lower()) | kw


def _q(
    qid: str, checkout: Any, kind: str, cands: list[str], **kw: Any
) -> dict[str, Any]:
    key = dict(ms._SUBJECT).get(kind, "x")
    ev = {key: cands[0]} if cands else {}
    return (
        dict(
            _PROPOSAL,
            id=qid,
            checkout=checkout,
            kind=kind,
            candidates_json=cands,
            evidence_json=ev,
            signature_json=None,
        )
        | kw
    )


_BASE = _k("K1", "P1")
_PROJECTS = [_p("P1"), _p("P2"), _p("P3"), _p("P4", merged_into="P2")]


def _bases() -> dict[str, Any]:
    return {"K1": enc(_BASE)}


def _z(v: Any) -> Any:
    """A checkout row spelled ``{"$k": [base id, {column: value}]}``."""
    if isinstance(v, list):
        return [_z(x) for x in v]
    if not isinstance(v, dict):
        return v
    pairs = v.get("$d")
    if isinstance(pairs, list) and len(v) == 1:
        base = _bases()["K1"]
        keys = [p[0] for p in pairs]
        if keys == [p[0] for p in base["$d"]]:
            was = dict((k, x) for k, x in base["$d"])
            diff = {k: x for k, x in pairs if was[k] != x}
            return {"$k": ["K1", diff]}
    return {k: _z(x) for k, x in v.items()}


def _clock(spec: list[Any]) -> datetime:
    text, minutes = spec
    dt = datetime.fromisoformat(text)
    if minutes is None:
        return dt
    return dt.replace(tzinfo=timezone(timedelta(minutes=minutes)))


class _Stub(_LogStub):
    """`_LogStub` that also puts questions and homes, keyed by their id."""

    def put_membership(self, kind: str, row: Any, *, session: Any = None) -> Any:
        if kind not in _MORE:
            return super().put_membership(kind, row, session=session)
        self._note("put", kind, dict(row), session)
        table, key = _MORE[kind]
        rows_ = self.tables.setdefault(table, [])
        for i, r in enumerate(rows_):
            if r.get(key) == row.get(key):
                rows_[i] = dict(row)
                break
        else:
            rows_.append(dict(row))
        return self._written()


_MORE = {"proposal": ("proposals", "id"), "home": ("homes", "record_id")}


def _run(case: dict[str, Any]) -> dict[str, Any]:
    tables = case.get("tables", {})
    if "$x" in tables:
        case = dict(case, x=tables["$x"])
        tables = _world_tables() | tables["$x"]
        del case["tables"]
    stub = _Stub(tables, case.get("log", []))
    live = set(case.get("live", []))
    clock = case.get("clock", _NOW)
    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        liveness=lambda k, v: "live" if v.path in live else "gone",
        now=lambda: _clock(clock),
    )
    f = case["f"]
    out = dict(case)
    if "tables" in case:
        out["tables"] = _spelled(case["tables"])
    else:
        out["x"] = _spelled(case["x"])
    args = {
        key: m.checkout(case[key])
        for key in ("k", "first", "idle", "kept", "settled")
        if case.get(key) is not None
    }
    pr = m.proposal(case["pr"]) if "pr" in case else None
    stub.log.clear()
    try:
        if f == "ask_which":
            ms._ask_which(m, args["k"], args["first"])
            o: Any = None
        elif f == "retire_into":
            ms._retire_into(m, args["idle"], args["kept"], case["by"])
            o = None
        elif f == "asked_at":
            o = ms._asked_at(m, pr, args["idle"], args["kept"])  # type: ignore[arg-type]
        elif f == "asked_here":
            ms._asked_here(m, args["k"])
            o = None
        elif f == "unseen":
            o = ms._unseen(m, args["k"])
        elif f == "asked_plan":
            got = ms._asked_plan(
                m, case["projects"], args.get("k"), args.get("settled")
            )
            o = [c.id for c in got]
        else:
            raise AssertionError(f)
        out["o"] = enc(o)
    except Exception as e:  # noqa: BLE001
        out["o"] = _raised(e)
    out["calls"] = enc(stub.log)
    return _z(out)


def _unseen_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(k: dict[str, Any], views: list[Any], **kw: Any) -> None:
        tables = {"checkouts": [k], "checkout_views": views}
        out.append({"f": "unseen", "k": k["id"], "tables": tables} | kw)

    here = _k("K2", "P1")
    there = _k("K2", "P1", machine="M2")
    add(_k("K2", "P1", retired=1), [_v("K2", "/a")], live=["/a"])
    add(here, [_v("K2", "/a")], live=["/a"])
    add(here, [_v("K2", "/a")])
    add(here, [])
    add(here, [_v("K2", "/a", machine="M2")], live=["/a"])
    add(here, [_v("K2", "/a"), _v("K2", "/b", seen_order=2)], live=["/a"])
    add(_k("K2", "P1", kind="unresolved"), [_v("K2", "/a")], live=["/a"])
    for seen in [
        [_RECENT],
        [_OLD],
        [_EDGE],
        ["2025-09-05T05:06:06Z"],
        ["2025-09-05T05:06:07.000001Z"],
        [_OLD, _RECENT],
        [_RECENT, _OLD],
        ["junk"],
        ["junk", _OLD],
        [""],
        ["2025-01-01T00:00:00"],
        ["2025-09-05T07:06:07+02:00"],
        ["2025-09-05T07:06:06+02:00"],
    ]:
        add(
            there,
            [_v("K2", f"/p{i}", machine="M2", last_seen=s) for i, s in enumerate(seen)],
        )
    add(there, [])
    add(
        there,
        [_v("K2", "/a", machine="M2", last_seen=_OLD)],
        clock=["2026-03-04T05:06:07", None],
    )
    add(
        there,
        [_v("K2", "/a", machine="M2", last_seen="junk")],
        clock=["2026-03-04T05:06:07", None],
    )
    add(
        there,
        [_v("K2", "/a", machine="M2", last_seen=_OLD)],
        clock=["0001-03-04T05:06:07", 0],
    )
    add(
        there,
        [_v("K2", "/a", machine="M2", last_seen=_EDGE)],
        clock=["2026-03-04T07:06:07", 120],
    )
    return out


def _ask_which_cases() -> list[dict[str, Any]]:
    checkouts = [_k("K1", "P1"), _k("K2", "P2"), _k("K3", "P4"), _k("K4", "P9")]
    base: dict[str, Any] = {
        "f": "ask_which",
        "k": "K2",
        "first": "K1",
        "tables": {"checkouts": checkouts, "projects": _PROJECTS},
    }
    dup = _q("Q007", "K2", "duplicate_checkout", ["P1", "P2"])
    return [
        base,
        dict(base, k="K3"),
        dict(base, k="K1", first="K3"),
        dict(base, k="K1", first="K1"),
        dict(base, k="K4"),
        dict(base, tables=dict(base["tables"], proposals=[dup])),
        dict(
            base, tables=dict(base["tables"], proposals=[dict(dup, state="answered")])
        ),
        dict(base, tables=dict(base["tables"], proposals=[dict(dup, checkout="K3")])),
        dict(
            base,
            tables=dict(
                base["tables"], projects=[_p("P1", name="a 'b'\n"), _p("P2", name="")]
            ),
        ),
    ]


def _world(**extra: Any) -> dict[str, Any]:
    """The header's tables (``world``) with `extra`'s in their place,
    spelled in a case as ``x``."""
    return {"$x": extra}


def _world_tables() -> dict[str, Any]:
    """K1 (P1, here) kept, K2 (P2, here) idle, K3 (P3, here), K4 (P3,
    M2), K5 (P3, here, retired), K6 (P2, M2)."""
    return {
        "projects": _PROJECTS,
        "checkouts": [
            _k("K1", "P1"),
            _k("K2", "P2"),
            _k("K3", "P3"),
            _k("K4", "P3", machine="M2"),
            _k("K5", "P3", retired=1),
            _k("K6", "P2", machine="M2"),
        ],
        "checkout_views": [
            _v("K1", "/w/r"),
            _v("K2", "/w/r/"),
            _v("K2", "/w/s"),
            _v("K2", "/w/r", machine="M2"),
            _v("K3", "/w/t"),
            _v("K4", "/x/t", machine="M2"),
            _v("K6", "/x/u", machine="M2"),
        ],
    }


def _retire_cases() -> list[dict[str, Any]]:
    base: dict[str, Any] = {
        "f": "retire_into",
        "idle": "K2",
        "kept": "K1",
        "by": "heal",
        "live": ["/w/t"],
    }
    imp = _q("Q001", "K2", "import", ["P3"])
    leg = _q("Q002", "K2", "legacy", ["P3", "P9"])
    first = _q("Q003", "K2", "first_sight", ["P1", "P2"])
    on3 = _q("Q010", "K3", "import", ["P3"])
    retired = ["checkout_retired", '{"checkout": "K2", "into": "K9"}']
    homes = [
        {"record_id": "R1", "checkout": "K2", "project": "P2", "rule": "r"},
        {"record_id": "R2", "rule": 5, "checkout": "K2"},
    ]
    return [
        dict(base, tables=_world()),
        dict(base, tables=_world(homes=homes)),
        dict(base, tables=_world(homes=[{"record_id": "R3", "checkout": "K2"}])),
        dict(base, tables=_world(proposals=[first, imp])),
        dict(base, tables=_world(proposals=[imp, leg])),
        dict(base, live=[], tables=_world(proposals=[imp])),
        dict(base, live=[], tables=_world(proposals=[imp, on3])),
        dict(base, tables=_world(proposals=[imp, on3])),
        dict(
            base,
            tables=_world(
                proposals=[imp, dict(on3, evidence_json={"imported_project": "PX"})]
            ),
        ),
        dict(base, tables=_world(proposals=[_q("Q004", "K2", "import", ["P1"])])),
        dict(base, tables=_world(proposals=[_q("Q004", "K2", "import", ["P4"])])),
        dict(base, tables=_world(proposals=[_q("Q004", "K2", "legacy", ["P9"])])),
        dict(base, tables=_world(proposals=[dict(imp, evidence_json=[1])])),
        dict(base, tables=_world(proposals=[imp, dict(on3, evidence_json=[1])])),
        dict(
            base,
            log=[retired],
            tables=_world(proposals=[imp]),
        ),
        dict(
            base,
            log=[["checkout_retired", '{"into": ""}']],
            tables=_world(proposals=[imp]),
        ),
        dict(base, idle="K5", tables=_world(proposals=[imp])),
        dict(
            base,
            log=[retired],
            idle="K5",
            tables=_world(proposals=[dict(imp, checkout="K5")]),
        ),
        dict(
            base,
            idle="K6",
            kept="K2",
            by="answer",
            tables=_world(proposals=[dict(imp, checkout="K6")]),
        ),
        dict(base, idle="K1", kept="K2", tables=_world()),
        # Two equal questions placed at one live checkout in one call: the
        # first is moved there, the second closed as settled into it.
        dict(base, tables=_world(proposals=[imp, dict(imp, id="Q011")])),
        # A live checkout whose id is empty asks the same already: the
        # question is closed as settled into `kept` (``where or kept.id``).
        dict(
            base,
            live=["/w/v"],
            tables=_world(
                proposals=[imp, _q("Q010", "", "import", ["P3"])],
                checkouts=[*_world_tables()["checkouts"], _k("", "P3")],
                checkout_views=[*_world_tables()["checkout_views"], _v("", "/w/v")],
            ),
        ),
    ]


def _asked_at_cases() -> list[dict[str, Any]]:
    base: dict[str, Any] = {
        "f": "asked_at",
        "idle": "K2",
        "kept": "K1",
        "pr": "Q001",
        "live": ["/w/t"],
    }
    rows = [
        ("Q001", "import", ["P3"]),
        ("Q001", "legacy", ["P3"]),
        ("Q001", "first_sight", ["P3"]),
        ("Q001", "import", ["P1"]),
        ("Q001", "import", ["P4"]),
        ("Q001", "import", []),
        ("Q001", "import", ["P9"]),
        ("Q001", "import", ["P2", "P3"]),
    ]
    out: list[dict[str, Any]] = [
        dict(base, tables=_world(proposals=[_q(q, "K2", kd, c)])) for q, kd, c in rows
    ]
    out.append(
        dict(
            base, live=[], tables=_world(proposals=[_q("Q001", "K2", "import", ["P3"])])
        )
    )
    out.append(
        dict(
            base,
            live=[],
            tables=_world(
                proposals=[_q("Q001", "K2", "import", ["P3"])],
                checkout_views=[_v("K4", "/x/t", machine="M2", last_seen=_OLD)],
            ),
        )
    )
    out.append(
        dict(
            base,
            idle="K3",
            tables=_world(proposals=[_q("Q001", "K3", "import", ["P3"])]),
        )
    )
    return out


def _asked_here_cases() -> list[dict[str, Any]]:
    base: dict[str, Any] = {"f": "asked_here", "k": "K3", "live": ["/w/t"]}
    qs = [
        _q("Q001", "K2", "import", ["P3"]),
        _q("Q002", "K3", "import", ["P3"]),
        _q("Q003", "K9", "legacy", ["P3"]),
        _q("Q004", None, "import", ["P3"]),
        _q("Q005", "K4", "import", ["P3"]),
        _q("Q006", "K5", "import", ["P3"]),
        _q("Q007", "K1", "import", ["P3"]),
        _q("Q008", "K1", "import", ["P2"]),
        _q("Q009", "", "legacy", ["P4"]),
    ]
    live_all = ["/w/t", "/w/r", "/w/r/", "/w/s"]
    return [
        dict(base, tables=_world(proposals=qs)),
        dict(base, live=live_all, tables=_world(proposals=qs)),
        dict(base, k="K6", tables=_world(proposals=qs)),
        dict(base, k="K2", tables=_world(proposals=qs)),
        dict(
            base,
            tables=_world(
                proposals=[
                    qs[0],
                    dict(qs[2], evidence_json={"unresolved_project": "P2"}),
                ]
            ),
        ),
        dict(
            base, tables=_world(proposals=[qs[0], _q("Q011", "K3", "import", ["P1"])])
        ),
        dict(base, tables=_world(proposals=[qs[0], dict(qs[1], evidence_json=[1])])),
        dict(base, tables=_world(proposals=[dict(qs[0], evidence_json=[1])])),
        dict(base, tables=_world(proposals=[dict(qs[0], state="answered")])),
        dict(
            base,
            tables=_world(
                proposals=[qs[4]],
                checkout_views=[_v("K4", "/x/t", machine="M2", last_seen=_OLD)],
            ),
        ),
        dict(
            base,
            tables=_world(proposals=[{"id": "Q1", "kind": "import", "state": "open"}]),
        ),
        # Two equal questions moved to `k` in one call: the first is moved,
        # the second closed as settled into `k`.
        dict(base, tables=_world(proposals=[qs[0], dict(qs[0], id="Q012")])),
    ]


def _plan_cases() -> list[dict[str, Any]]:
    base: dict[str, Any] = {"f": "asked_plan", "projects": ["P3"]}
    qs = [
        _q("Q001", "K2", "import", ["P3"]),
        _q("Q002", "K3", "legacy", ["P4"]),
        _q("Q003", "K4", "import", ["P3"]),
        _q("Q004", "K5", "import", ["P3"]),
        _q("Q005", None, "import", ["P3"]),
        _q("Q006", "", "import", ["P3"]),
        _q("Q007", "K9", "import", ["P3"]),
        _q("Q008", "K1", "first_sight", ["P3"]),
        _q("Q009", "K6", "import", ["P1"]),
    ]
    w = _world(proposals=qs)
    return [
        dict(base, tables=w),
        dict(base, projects=["P2"], tables=w),
        dict(base, projects=["P4", "P1"], tables=w),
        dict(base, projects=[], tables=w),
        dict(base, projects=["P9"], tables=w),
        dict(base, k="K3", tables=w),
        dict(base, k="K1", settled="K2", tables=w),
        dict(base, k="K1", settled="K2", projects=[], tables=w),
        dict(base, k="K3", settled="K2", projects=[], tables=w),
        dict(base, settled="K2", projects=[], tables=w),
        dict(
            base,
            k="K1",
            settled="K2",
            projects=[],
            tables=_world(proposals=[_q("Q001", "K2", "first_sight", ["P3"])]),
        ),
        dict(
            base,
            tables=_world(proposals=[{"id": "Q1", "kind": "import", "state": "open"}]),
        ),
        # Two equal questions moved to `k` in one call: the first is moved,
        # the second closed as settled into `k`.
        dict(base, tables=_world(proposals=[qs[0], dict(qs[0], id="Q012")])),
    ]


def _cases() -> list[dict[str, Any]]:
    return [
        *_unseen_cases(),
        *_ask_which_cases(),
        *_asked_at_cases(),
        *_asked_here_cases(),
        *_plan_cases(),
        *_retire_cases(),
    ]


def generate() -> dict[str, bytes]:
    header = {"f": "header", "base": _bases(), "world": _spelled(_world_tables())}
    return {
        "membership_shared_ask.jsonl": jsonl([header, *(_run(c) for c in _cases())])
    }
