"""Fixtures: the membership model, unit membership-2 (`membership.py`):
the evidence writes (`add_witness`, `_add_witness`, `_remotes_of`,
`_add_remotes`), the boundary writes (`_bind_file`, `_file_flag`,
`_lift_boundary`, `_make_boundary`) and the checkout writes
(`_refresh_object`, `_put_view`, `_register`).

The reference model runs over `gen_membership_core`'s recording store
`_Stub`; each scripted call's transcript (what it answered, the store
calls it made in order, the event payloads through `_jsonable`, the
model's collected row numbers) is one step. A history reader is named
by a key of `_HISTORIES`, a location is a spec of `_loc`.

One file, ``membership_writes.jsonl``, not gated: no row depends on the
Python release, the platform or a library version. The first line is the
base tables; each other line a sequence as `gen_membership_core` spells
it.
"""

from __future__ import annotations

from typing import Any

from bettermemory import membership as mm
from bettermemory import membership_records as mr

from ._common import jsonl
from .gen_membership_core import (
    _Boom,
    _checkout,
    _k,
    _now,
    _p,
    _raised,
    _sha,
    _spelled,
    _Stub,
    _v,
    _w,
)
from .gen_membership_rows import enc


def _hex(i: int) -> str:
    return f"{i:040x}"


class _History:
    def __init__(self, fingerprint: str | None, blobs: dict[str, list[str]]) -> None:
        self._fp = fingerprint
        self._blobs = blobs

    def fingerprint(self, commit: str) -> str | None:
        return self._fp

    def blobs(self, commit: str) -> list[str]:
        return list(self._blobs.get(commit, []))


#: name -> (fingerprint, {commit: blobs}); the Rust test holds the same.
_HISTORY_SPECS: dict[str, Any] = {
    "plain": [None, {}],
    "rich": [
        "fp1",
        {
            _hex(1): ["b9", "b1", "b3", "b1", "b2", "b0", "b4", "b5", "b6", "b7", "b8"],
            _hex(2): ["b1", "bA", "b2"],
            _hex(500): ["z1", "z2"],
        },
    ],
}
_HISTORIES = {k: _History(v[0], v[1]) for k, v in _HISTORY_SPECS.items()}


def _key(spec: Any) -> mr.ObjectKey | None:
    return None if spec is None else mr.ObjectKey(*spec)


def _loc(spec: dict[str, Any]) -> mr.Location:
    kw: dict[str, Any] = {"machine": "M1", "cwd": "/w", "state": "repo", **spec}
    for k in ("object_key", "common_object_key"):
        if k in kw:
            kw[k] = _key(kw[k])
    if "remote_keys" in kw:
        kw["remote_keys"] = frozenset(kw["remote_keys"])
    if "ref_tips" in kw:
        kw["ref_tips"] = tuple(kw["ref_tips"])
    if "manifest" in kw:
        kw["manifest"] = mr.Manifest.from_json(kw["manifest"])
    return mr.Location(**kw)


def _file(spec: list[Any]) -> mr.ProjectFileRef:
    fid, name, d, boundary = spec
    return mr.ProjectFileRef(fid, name, d, d + "/bettermemory.toml", boundary)


def _answer(m: mm.Membership, op: str, a: Any) -> Any:  # noqa: PLR0911
    if op == "add_witness":
        hist = None if a[3] is None else _HISTORIES[a[3]]
        m.add_witness(a[0], a[1], source=a[2], history=hist)  # type: ignore[arg-type]
        return None
    if op == "add_witness_in":
        hist = None if a[3] is None else _HISTORIES[a[3]]
        m._add_witness(a[0], a[1], source=a[2], history=hist)  # type: ignore[arg-type]
        return None
    if op == "remotes_of":
        return sorted(m._remotes_of(a))
    if op == "add_remotes":
        m._add_remotes(a[0], a[1])
        return None
    if op == "bind_file":
        m._bind_file(
            a["file_id"],
            a["project"],
            evidence=a["evidence"],
            boundary=a.get("boundary", False),
            checkout=a.get("checkout"),
            directory=a.get("directory"),
        )
        return None
    if op == "file_flag":
        return m._file_flag(_file(a[0]), a[1])
    if op == "lift_boundary":
        m._lift_boundary(a[0], a[1])
        return None
    if op == "make_boundary":
        m._make_boundary(a[0], around=a[1])
        return None
    if op == "projects_all":
        return [[p.id, p.boundary, p.tenant] for p in m._all_projects().values()]
    if op == "refresh_object":
        return _checkout(m._refresh_object(m.checkout(a[0]), _loc(a[1])))
    if op == "put_view":
        m._put_view(mm.View(**a))
        return None
    if op == "register":
        return _checkout(m._register(_loc(a[0]), a[1], a[2]))
    raise ValueError(op)


def _step(m: mm.Membership, stub: _Stub, step: dict[str, Any]) -> dict[str, Any]:
    op: str = step["op"]
    a: Any = step.get("a")
    start = len(stub.log)
    out: dict[str, Any] = {"op": op, "a": a}
    if op == "call":
        inner: list[dict[str, Any]] = []
        out["inner"] = inner
        try:
            with m._call():
                for s in a["ops"]:
                    got = _step(m, stub, s)
                    inner.append(got)
                    if isinstance(got["o"], dict) and "raised" in got["o"]:
                        raise got.pop("_exc")
                if a.get("raise"):
                    raise _Boom("boom")
            out["o"] = None
        except Exception as e:  # noqa: BLE001
            out["o"] = _raised(e)
        for s in inner:
            s.pop("_exc", None)
    else:
        try:
            out["o"] = enc(_answer(m, op, a))
        except Exception as e:  # noqa: BLE001
            out["o"] = _raised(e)
            if isinstance(e, ValueError):
                out["o"]["raised"][1] = str(e)
            out["_exc"] = e
    out["log"] = enc(stub.log[start:])
    out["rows"] = list(m._rows)
    return out


_INIT: dict[str, Any] = {
    "now": ["2026-03-04T05:06:07.123456", 60],
    "session": "S1",
    "given": True,
}


def _sequence(
    name: str,
    tables: dict[str, list[dict[str, Any]]],
    steps: list[dict[str, Any]],
    *,
    fail: Any = None,
) -> dict[str, Any]:
    stub = _Stub(tables, fail, False)
    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        liveness=lambda k, v: "live",
        now=lambda: _now(_INIT["now"]),
        session=_INIT["session"],
    )
    done = []
    for s in steps:
        got = _step(m, stub, s)
        got.pop("_exc", None)
        done.append(got)
    return {
        "seq": name,
        "init": _INIT,
        "tables": "base" if tables is _TABLES else _spelled(tables),
        "fail": fail,
        "outer": False,
        "steps": done,
        "after": _sha(_spelled(stub.tables)),
    }


_KEY_A = ["16777220", "100", "1.5"]
_KEY_A2 = ["16777220", "100", "2.5"]
_KEY_B = ["16777220", "200", "1.5"]
_KEY_C = ["16777221", "300", None]

_TABLES: dict[str, list[Any]] = {
    "projects": [
        _p("P001", "root"),
        _p("P002", "mid", parent="P001"),
        _p("P003", "wall", parent="P002", boundary=1, tenant="P003"),
        _p("P004", "inner", parent="P003", tenant="P003"),
        _p("P005", "gone", merged_into="P001"),
        _p("P006", "ws"),
        _p("P007", "wschild", parent="P006"),
        _p("P008", "solo", boundary=1, tenant="P008"),
        _p("P009", "away", tenant="T9"),
        _p("P010", "full"),
        _p("P011", "under", parent="P008", tenant="P008"),
        _p("P012", "liftme", boundary=1, tenant="P012", parent="P009"),
        _p("P013", "inlift", parent="P012", tenant="P012"),
        _p("P014", "oldmerge", merged_into="P012", tenant="P012"),
    ],
    "checkouts": [
        _k("K1", "P006", object_device=None, object_inode=None, object_birth=None),
        _k("K2", "P009"),
        _k("K3", "P007", kind="unresolved"),
        _k("K4", "P001", machine="M2"),
        _k(
            "K5",
            "P001",
            object_device=_KEY_A[0],
            object_inode=_KEY_A[1],
            object_birth=_KEY_A[2],
            common_device=_KEY_A[0],
            common_inode=_KEY_A[1],
            common_birth=_KEY_A[2],
        ),
        _k(
            "K6",
            "P001",
            object_device=_KEY_A[0],
            object_inode=_KEY_A[1],
            object_birth=_KEY_A[2],
            common_device=_KEY_C[0],
            common_inode=_KEY_C[1],
            common_birth=None,
        ),
    ],
    "checkout_views": [
        _v("K1", "/ws/r1", 1),
        _v("K1", "/elsewhere", 0),
        _v("K2", "/ws/r2", 2),
        _v("K3", "/ws/r3", 3),
        _v("K4", "/ws/r4", 4, "M2"),
        _v("K5", "/w/k5", 5),
    ],
    "project_witnesses": [
        *[
            {**_w("P010", _hex(100 + i), i + 1), "source": "s", "fingerprint": None}
            for i in range(64)
        ],
        {**_w("P002", _hex(7), 70), "source": "s", "fingerprint": None},
        {**_w("P005", _hex(8), 71), "source": "s", "fingerprint": None},
    ],
    "project_blobs": [
        {"project": "P010", "blob": "c1", "witness": _hex(116)},
        {"project": "P010", "blob": "c2", "witness": _hex(116)},
        {"project": "P010", "blob": "c3", "witness": _hex(117)},
        {"project": "P001", "blob": "b0", "witness": _hex(9)},
        {"project": "P002", "blob": "b1", "witness": _hex(7)},
        {"project": "P005", "blob": "b2", "witness": _hex(8)},
    ],
    "project_remotes": [
        {"project": "P001", "remote_key": "r1"},
        {"project": "P005", "remote_key": "r2"},
        {"project": "P002", "remote_key": "r3"},
    ],
    "project_files": [{"file_id": "F1", "project": "P002", "evidence": "init"}],
}


def _all() -> dict[str, Any]:
    return {"op": "projects_all"}


def _witnesses() -> list[dict[str, Any]]:
    w = "add_witness"
    return [
        {"op": w, "a": ["P001", _hex(1), "head", "rich"]},
        {"op": w, "a": ["P005", _hex(10).upper(), "head", "rich"]},
        {"op": w, "a": ["P005", _hex(1), "head", "rich"]},
        {"op": w, "a": ["P001", _hex(2), "write", "rich"]},
        {"op": w, "a": ["P001", _hex(7), "write", None]},
        {"op": w, "a": ["P002", _hex(3), "verify", "plain"]},
        {"op": w, "a": ["P001", "abc", "head", None]},
        {"op": w, "a": ["P001", "x" * 100, "head", None]},
        {"op": w, "a": ["P001", "0" * 64, "head", "rich"]},
        {"op": w, "a": ["P001", "0" * 63 + "G", "head", None]},
        {"op": w, "a": ["P001", "'\"é " + "0" * 36, "head", None]},
        {"op": w, "a": ["PX", _hex(4), "head", None]},
        {"op": w, "a": ["P010", _hex(500), "head", "rich"]},
        {"op": w, "a": ["P010", _hex(501), "head", None]},
        {"op": w, "a": ["P010", _hex(100), "head", None]},
        {
            "op": "call",
            "a": {"ops": [{"op": "add_witness_in", "a": ["P001", _hex(5), "x", None]}]},
        },
    ]


def _remotes() -> list[dict[str, Any]]:
    return [
        {"op": "remotes_of", "a": "P001"},
        {"op": "remotes_of", "a": "P005"},
        {"op": "remotes_of", "a": "P002"},
        {"op": "remotes_of", "a": "PX"},
        {"op": "add_remotes", "a": ["P005", ["r9", "r1", "r2", "a0", "r9", "r3"]]},
        {"op": "add_remotes", "a": ["P002", []]},
        {"op": "add_remotes", "a": ["PX", ["k"]]},
        {"op": "remotes_of", "a": "P001"},
    ]


def _binds() -> list[dict[str, Any]]:
    b = "bind_file"
    base = {"file_id": "F2", "project": "P005", "evidence": "init"}
    return [
        {"op": b, "a": base},
        {"op": b, "a": {**base, "file_id": "F1", "checkout": "K1"}},
        {"op": b, "a": {**base, "file_id": "F3", "directory": "/ws/sub"}},
        {
            "op": b,
            "a": {**base, "file_id": "F4", "checkout": "K2", "directory": "/d"},
        },
        {"op": b, "a": {**base, "project": "P003", "boundary": True}},
        {
            "op": b,
            "a": {
                **base,
                "file_id": "F5",
                "project": "P006",
                "boundary": True,
                "directory": "/ws",
            },
        },
        _all(),
    ]


def _flags() -> list[dict[str, Any]]:
    f = "file_flag"
    return [
        {"op": f, "a": [["F1", "n", "/w/a", False], "P001"]},
        {"op": f, "a": [["F1", "n", "/w/a", True], "P003"]},
        {"op": f, "a": [["F1", "n", "/w/a b", False], "P003"]},
        {"op": f, "a": [["F1", "n", "/w/a", True], "P005"]},
        _all(),
        {"op": f, "a": [["F1", "n", "/w/a", True], "PX"]},
    ]


def _boundaries() -> list[dict[str, Any]]:
    mk = "make_boundary"
    lift = "lift_boundary"
    return [
        {"op": mk, "a": ["P003", None]},
        {"op": mk, "a": ["P002", None]},
        _all(),
        {"op": lift, "a": ["P002", "OUT"]},
        _all(),
        {"op": lift, "a": ["P012", "OUT"]},
        _all(),
        {"op": lift, "a": ["P008", "OUT"]},
        _all(),
        {"op": lift, "a": ["P001", "OUT"]},
        {"op": mk, "a": ["P006", "/ws"]},
        _all(),
        {"op": mk, "a": ["P009", "/ws/"]},
        _all(),
        {"op": mk, "a": ["P001", "/elsewhere"]},
        _all(),
        {"op": mk, "a": ["PX", None]},
        {"op": lift, "a": ["PX", None]},
    ]


def _boundaries2() -> list[dict[str, Any]]:
    return [
        {"op": "make_boundary", "a": ["P009", "/ws"]},
        _all(),
        {"op": "make_boundary", "a": ["P006", "/ws/r1"]},
        _all(),
        {"op": "lift_boundary", "a": ["P006", None]},
        _all(),
    ]


# An unresolved checkout is the only way into P002, a boundary R (P003)
# under `around` walls its child P004, and P005 already sits inside P001:
# making P001 a boundary around /ws moves none of them.
_WALLS: dict[str, list[dict[str, Any]]] = {
    "projects": [
        _p("P001", "outer"),
        _p("P002", "loose"),
        _p("P003", "repo", boundary=1),
        _p("P004", "repochild", parent="P003"),
        _p("P005", "inside", parent="P001", tenant="P001"),
    ],
    "checkouts": [
        _k("K1", "P002", kind="unresolved"),
        _k("K2", "P003"),
    ],
    "checkout_views": [_v("K1", "/ws/a", 1), _v("K2", "/ws/b", 2)],
}


# One short of the bound: the add that reaches it releases nothing.
_EDGE: dict[str, list[dict[str, Any]]] = {
    "projects": [_p("P001", "edge")],
    "project_witnesses": [
        {**_w("P001", _hex(100 + i), i + 1), "source": "s", "fingerprint": None}
        for i in range(63)
    ],
}


def _edge() -> list[dict[str, Any]]:
    w = "add_witness"
    return [
        {"op": w, "a": ["P001", _hex(900), "head", None]},
        {"op": w, "a": ["P001", _hex(901), "head", None]},
    ]


def _walls() -> list[dict[str, Any]]:
    return [{"op": "make_boundary", "a": ["P001", "/ws"]}, _all()]


def _refreshes() -> list[dict[str, Any]]:
    r = "refresh_object"
    return [
        {"op": r, "a": ["K5", {"object_key": _KEY_A2, "root": "/w/k5"}]},
        {"op": r, "a": ["K5", {"object_key": _KEY_A2}]},
        {
            "op": r,
            "a": ["K5", {"object_key": _KEY_B, "common_object_key": _KEY_C}],
        },
        {"op": r, "a": ["K6", {"object_key": _KEY_A2, "common_object_key": _KEY_B}]},
        {"op": r, "a": ["K5", {"object_key": None}]},
        {"op": r, "a": ["K1", {"object_key": _KEY_A}]},
        {"op": r, "a": ["K2", {"object_key": ["16777220", "123", "1700000000.5"]}]},
        {"op": r, "a": ["KX", {}]},
    ]


def _views() -> list[dict[str, Any]]:
    v = {
        "checkout": "K9",
        "machine": "M3",
        "path": "/p",
        "first_seen": "t1",
        "last_seen": "t2",
        "order": 9,
    }
    return [{"op": "put_view", "a": v}, {"op": "put_view", "a": {**v, "order": 2**40}}]


def _registers() -> list[dict[str, Any]]:
    full = {
        "root": "/w/r/",
        "kind": "linked",
        "git_dir": "/w/r/.git",
        "object_key": _KEY_A,
        "common_object_key": _KEY_C,
        "head": "h1",
        "branch": "main",
        "remote_keys": ["b", "a", "C"],
        "token": "t1",
        "token_written": "t2",
        "token_status": "present",
        "ticks": True,
        "manifest": {"name": "r", "entries": [["x", 1]]},
    }
    unborn = {
        "root": "/w/u",
        "manifest": {"name": "u", "entries": []},
        "token_status": "n/a",
    }
    r = "register"
    return [
        {"op": r, "a": [full, "P001", "file"]},
        {"op": r, "a": [{**full, "kind": "folder"}, "P001", "folder"]},
        {"op": r, "a": [unborn, "P002", "none"]},
        {"op": r, "a": [{**unborn, "token": "", "token_written": ""}, "P002", "x"]},
        {"op": r, "a": [{**unborn, "token": "", "token_written": None}, "PX", "x"]},
        {"op": r, "a": [{**unborn, "token": None, "token_written": "w"}, "P2", "x"]},
        {"op": r, "a": [{**unborn, "ref_tips": ["t"]}, "P002", "x"]},
        {"op": r, "a": [{"root": None, "cwd": "/c//"}, "P001", "none"]},
        {"op": r, "a": [{"root": "", "cwd": "/c", "machine": "M7"}, "P001", "y"]},
        {"op": r, "a": [{"root": "/", "kind": "bare"}, "P005", "y"]},
    ]


def _sequences() -> list[dict[str, Any]]:
    bad_blobs = {
        "projects": [_p("P001", "a")],
        "project_blobs": [{"project": "P001", "witness": "w"}],
    }
    ints = {
        "projects": [_p("P001", "a")],
        "project_remotes": [{"project": "P001", "remote_key": 7}],
        "project_blobs": [{"project": "P001", "blob": 5, "witness": "w"}],
    }
    return [
        _sequence("witnesses", _TABLES, _witnesses()),
        _sequence("remotes", _TABLES, _remotes()),
        _sequence("binds", _TABLES, _binds()),
        _sequence("flags", _TABLES, _flags()),
        _sequence("boundaries", _TABLES, _boundaries()),
        _sequence("boundaries2", _TABLES, _boundaries2()),
        _sequence("boundaries_walls", _WALLS, _walls()),
        _sequence("witness_edge", _EDGE, _edge()),
        _sequence("refreshes", _TABLES, _refreshes()),
        _sequence("views", _TABLES, _views()),
        _sequence("registers", _TABLES, _registers()),
        _sequence("registers_empty", {}, _registers()[:3]),
        _sequence("fail_blob", _TABLES, _witnesses()[:1], fail=["put", 3]),
        _sequence("fail_id", _TABLES, _registers()[:1], fail=["next_id", 1]),
        _sequence("fail_number", _TABLES, _registers()[:1], fail=["next_number", 1]),
        _sequence("fail_view", _TABLES, _registers()[:1], fail=["put", 2]),
        _sequence("bad_blobs", bad_blobs, _witnesses()[:1]),
        _sequence(
            "ints",
            ints,
            [
                {"op": "remotes_of", "a": "P001"},
                {"op": "add_remotes", "a": ["P001", ["7", "8"]]},
                {"op": "add_witness", "a": ["P001", _hex(2), "s", "rich"]},
            ],
        ),
    ]


def generate() -> dict[str, bytes]:
    header = {
        "seq": "header",
        "base": _spelled(_TABLES),
        "histories": _HISTORY_SPECS,
    }
    return {"membership_writes.jsonl": jsonl([header, *_sequences()])}
