"""Fixtures: the membership model, unit membership-4a (`membership.py`):
the sighting of a known checkout (`_touch_view`, 1029-1078) and the
token re-mints (`_checkout_name` 1598-1601, `_give_token` 1602-1647,
`token_written` 1648-1674, `_foreign` 1835-1844).

The reference model runs over `gen_membership_core`'s recording store
`_Stub` with a fixed clock, a liveness that answers a state per view path
(``live`` where the sequence names none) and a fake token writer that
answers a fixed write per checkout id (None for an id it does not name;
no writer at all where the sequence's writer is null). Each step is one
call of the unit; its transcript is what it answered, the store calls in
order, the model's collected row numbers and its `_remint`.

One file, ``membership_touch.jsonl``, not gated: no row depends on the
Python release, the platform or a library version. The re-mint tokens are
made with `checkout_token._link` over a fixed nonce, so they are the same
bytes on every run.
"""

from __future__ import annotations

from typing import Any

from bettermemory import checkout_token as ct
from bettermemory import membership as mm
from bettermemory import membership_records as mr

from ._common import jsonl
from .gen_membership_core import _checkout, _k, _now, _raised, _sha, _spelled, _Stub, _v
from .gen_membership_rows import enc

_NOW = ["2026-03-04T05:06:07.123456", 60]
_TOK = "ABCDEFGHJKMNPQRSTVWXYZ0123"
_DEV, _INO = "16777220", "123"
# A re-mint of K's lineage (`succeeds` is true), and one of another inode.
_NEXT = "0123456789ABC" + ct._link(ct.Lineage(_TOK, _DEV, _INO), "0123456789ABC")
_ELSE = "0123456789ABC" + ct._link(ct.Lineage(_TOK, _DEV, "999"), "0123456789ABC")


def _view(kid: str, path: str, order: int, last: str, machine: str = "M1") -> Any:
    return {**_v(kid, path, order, machine), "last_seen": last}


_FRESH = "2026-03-04T04:00:00Z"
_TABLES: dict[str, list[Any]] = {
    "checkouts": [
        _k("K1", "P1"),
        _k("K2", "P1"),
        _k("K3", "P1", retired=1),
        _k("K4", "P1"),
        _k("K5", "P1"),
        _k("K6", "P1", token=_TOK, token_status="superseded"),
        _k("K7", "P1", token=_TOK),
        _k("K8", "P1", git_dir=None),
        _k("K9", "P1", kind="bare"),
        _k("K10", "P1", token=_TOK, object_device=None),
        _k("K11", "P1", token=_TOK, object_inode="x1"),
    ],
    "checkout_views": [
        _view("K1", "/w/a", 1, _FRESH),
        _view("K1", "/w/b", 2, _FRESH),
        _view("K1", "/m2/b", 9, "bad", "M2"),
        # Exactly one day old: stale (>=).
        _view("K2", "/w/c", 3, "2026-03-03T04:06:07.123456Z"),
        _view("K3", "/w/r", 4, "2026-03-03T04:06:07.123457Z"),
        _view("K4", "/w/g1", 5, _FRESH),
        _view("K4", "/w/g2", 6, _FRESH),
        _view("K5", "/w/e", 7, "2026-03-04T05:06:07+01:00"),
        _view("K5", "/w/e2", 7, "t2"),
        _view("K6", "/w/f", 8, "2026-03-03T04:06:08"),
        _view("K7", "/w/x/", 10, _FRESH),
    ],
}


def _loc(spec: dict[str, Any]) -> mr.Location:
    return mr.Location(**{"machine": "M1", "cwd": "/w", "state": "repo", **spec})


def _answer(m: mm.Membership, op: str, a: Any) -> Any:
    if op == "touch":
        return _checkout(m._touch_view(m.checkout(a[0]), _loc(a[1])))
    if op == "name":
        return m._checkout_name(m.checkout(a))
    if op == "give":
        m._give_token(m.checkout(a))
        return None
    if op == "written":
        m.token_written(a[0], a[1], a[2])
        return None
    if op == "foreign":
        return mm._foreign(m.checkout(a[0]), a[1], a[2])
    raise ValueError(op)


def _sequence(
    name: str,
    steps: list[list[Any]],
    *,
    live: dict[str, str] | None = None,
    writer: dict[str, Any] | None = None,
    fail: Any = None,
    tables: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    stub = _Stub(_TABLES if tables is None else tables, fail, False)
    states = live or {}
    writes = writer

    def liveness(k: Any, v: Any) -> str:
        return states.get(v.path, "live")

    def token_writer(k: Any) -> Any:
        assert writes is not None
        got = writes.get(k.id)
        return None if got is None else tuple(got)

    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        liveness=liveness,  # type: ignore[arg-type]
        token_writer=None if writes is None else token_writer,
        now=lambda: _now(_NOW),
        session="S1",
    )
    done = []
    for op, a in steps:
        start = len(stub.log)
        try:
            o = enc(_answer(m, op, a))
        except Exception as e:  # noqa: BLE001
            o = _raised(e)
        done.append(
            {
                "op": op,
                "a": a,
                "o": o,
                "log": enc(stub.log[start:]),
                "rows": list(m._rows),
                "remint": m._remint,
            }
        )
    return {
        "seq": name,
        "live": states,
        "writer": writes,
        "fail": fail,
        "tables": "base" if tables is None else _spelled(tables),
        "steps": done,
        "after": _sha(_spelled(stub.tables)),
    }


def _touches() -> list[list[Any]]:
    t = "touch"
    return [
        [t, ["K1", {"root": "/w/b"}]],
        [t, ["K1", {"root": "/w/b//"}]],
        [t, ["K1", {"root": "/w/a"}]],
        [t, ["K2", {"root": "/w/c"}]],
        [t, ["K3", {"root": "/w/r"}]],
        [t, ["K4", {"root": "/w/new"}]],
        [t, ["K5", {"root": "/w/e"}]],
        [t, ["K5", {"root": "/w/e2"}]],
        [t, ["K6", {"root": None, "cwd": "/w/f/"}]],
        [t, ["K6", {"root": "", "cwd": "/w/f"}]],
        [t, ["K7", {"root": "/w/x"}]],
        [t, ["K1", {"root": "/m2/b", "machine": "M2"}]],
        [t, ["K1", {"root": "/m3/z", "machine": "M3"}]],
        [t, ["KX", {"root": "/w"}]],
    ]


def _names() -> list[list[Any]]:
    return [
        ["name", "K1"],
        ["name", "K5"],
        ["name", "K7"],
        ["name", "K8"],
        ["touch", ["K8", {"root": "/"}]],
        ["name", "K8"],
    ]


def _gives() -> list[list[Any]]:
    return [["give", f"K{i}"] for i in range(1, 12)]


_WRITER: dict[str, Any] = {
    "K1": [_TOK, "written"],
    "K2": [None, "skipped:policy"],
    "K3": [_ELSE, "exists"],
    "K4": [_NEXT, "exists"],
    "K5": ["", "written"],
    "K6": [None, "skipped:read_only"],
    "K7": [_TOK, mr.OTHER_MACHINE_SKIP],
    "K8": [_TOK, "written"],
    "K10": [_NEXT, "exists"],
    "K11": [None, ct.CONTENDED],
}
_WRITER_SUPERSEDED: dict[str, Any] = {
    "K6": [_NEXT, "exists"],
    "K7": [None, ct.CONTENDED],
}


def _writtens() -> list[list[Any]]:
    w = "written"
    return [
        [w, ["K1", _TOK, "written"]],
        [w, ["K6", None, "skipped:policy"]],
        [w, ["K6", None, "superseded"]],
        [w, ["K6", _NEXT, "exists"]],
        [w, ["K6", _ELSE, "exists"]],
        [w, ["K6", _TOK, ct.CONTENDED]],
        [w, ["K6", "", "written"]],
        [w, ["K7", None, "skipped:policy"]],
        [w, ["K10", _NEXT, "exists"]],
        [w, ["KX", _TOK, "written"]],
    ]


def _foreigns() -> list[list[Any]]:
    f = "foreign"
    return [
        [f, ["K7", _NEXT, "exists"]],
        [f, ["K7", _ELSE, "exists"]],
        [f, ["K7", _TOK, "exists"]],
        [f, ["K7", None, "exists"]],
        [f, ["K7", "short", "exists"]],
        [f, ["K7", _ELSE, "written"]],
        [f, ["K7", _ELSE, "Exists"]],
        [f, ["K10", _NEXT, "exists"]],
        [f, ["K11", _NEXT, "exists"]],
        [f, ["K6", _NEXT, "exists"]],
    ]


# Two views of one order on M1, neither on the path touched, both gone.
_TIES: dict[str, list[Any]] = {
    "checkouts": [_k("K1", "P1")],
    "checkout_views": [
        _view("K1", "/t/a", 2, _FRESH),
        _view("K1", "/t/b", 2, _FRESH),
    ],
}


def _sequences() -> list[dict[str, Any]]:
    gone = {"/w/g1": "gone", "/w/g2": "replaced", "/w/a": "gone", "/w/b": "gone"}
    return [
        _sequence("touches", _touches()),
        _sequence("touches_gone", _touches(), live=gone),
        _sequence("touches_legacy", _touches()[5:6], live={"/w/g1": "live_legacy"}),
        _sequence("names", _names()),
        _sequence("gives_no_writer", _gives()),
        _sequence("gives", _gives(), writer=_WRITER),
        _sequence("gives_superseded", _gives(), writer=_WRITER_SUPERSEDED),
        _sequence("writtens", _writtens()),
        _sequence("foreigns", _foreigns()),
        _sequence(
            "ties",
            [["touch", ["K1", {"root": "/t/b"}]], ["touch", ["K1", {"root": "/t/c"}]]],
            live={"/t/a": "gone", "/t/b": "unreachable"},
            tables=_TIES,
        ),
        _sequence("fail_put_view", _touches()[2:3], fail=["put", 1]),
        _sequence("fail_number", _touches()[5:6], fail=["next_number", 1]),
        _sequence("fail_event", _touches()[5:6], fail=["record", 1]),
        _sequence("fail_retired", _touches()[4:5], fail=["put", 1]),
        _sequence("fail_give", [["give", "K1"]], writer=_WRITER, fail=["put", 1]),
        _sequence(
            "fail_written", [["written", ["K6", _NEXT, "exists"]]], fail=["record", 1]
        ),
    ]


def generate() -> dict[str, bytes]:
    header = {"seq": "header", "base": _spelled(_TABLES), "now": _NOW}
    return {"membership_touch.jsonl": jsonl([header, *_sequences()])}
