"""Fixtures: rung 2 of the resolution ladder (``membership_rung.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/membership/rung.rs``. This pins
`_token_now`, `_by_object` and `_rung_two` of `membership_resolve` over
the real `Membership` on the recording store of
`gen_membership_shared_heal` (`_LogStub`), and `_token_read` over token
files the generator writes in a temporary directory.

A model case holds ``f``, ``tables``, ``log``, ``loc`` (a sighting: ``m``
machine, ``g`` git directory, ``o`` object key ``[device, inode,
birth]``, ``t`` token, ``w`` token written, ``s`` stamps, ``x`` ticks, ``st`` token status),
``remint`` (what the stub `_shared_remint` answers, each call logged as
``["remint", checkout, found]``), ``here`` (false: a client's liveness,
so `reads_here` is false) and ``disk`` (what the stub `_token_read`
answers per git directory, each call logged as ``["read", git_dir]``).
It records ``o``: for `_rung_two` the four checkouts and how (a checkout
as its id), for `_by_object` the two, for `_token_now` the location's
token, token written and token status; and ``calls``, the store calls.

A ``token_read`` case names what the token file holds (``file``: text,
``"dir"`` a directory, null none), whether its git directory exists and
whether `location._evicted` is made to answer true; ``o`` is the token
read. Not gated: no answer follows the Python release or the system (the
temporary directory's path is in no row).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from bettermemory import location
from bettermemory import membership as mm
from bettermemory import membership_resolve as mr
from bettermemory.membership_records import InitStamp, ObjectKey

from ._common import jsonl
from .gen_membership_core import _now, _raised, _spelled
from .gen_membership_rows import enc
from .gen_membership_shared_heal import (
    _A,
    _A_CT,
    _B,
    _JOIN,
    _KEPT,
    _LATER,
    _R,
    _T,
    _THERE,
    _k,
    _LogStub,
)

_D, _I = "16777220", "123"
_TOKEN = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_OTHER = "Z" * 26


def _loc(spec: dict[str, Any]) -> Any:
    o = spec.get("o", [_D, _I, None])
    return mm.Location(
        machine=spec.get("m", "M1"),
        cwd="/w/r",
        state="repo",
        root="/w/r",
        kind="primary",
        git_dir=spec.get("g", "/w/r/.git"),
        object_key=None if o is None else ObjectKey(*o),
        token=spec.get("t"),
        token_written=spec.get("w"),
        token_status=spec.get("st", "present"),
        stamps=tuple(InitStamp(*s) for s in spec.get("s", [])),
        ticks=spec.get("x"),
    )


def _id(k: Any) -> Any:
    return None if k is None else k.id


def _run(case: dict[str, Any]) -> dict[str, Any]:
    stub = _LogStub(case.get("tables", {}), case.get("log", []))
    kw: dict[str, Any] = {}
    if case.get("here") is False:
        kw["liveness"] = lambda k, v: "live"
    m = mm.Membership(
        stub,  # type: ignore[arg-type]
        "M1",
        now=lambda: _now(["2026-03-04T05:06:07", 0]),
        **kw,
    )

    def shared(k: Any, loc: Any, found: Any) -> bool:
        stub.log.append(["remint", k.id, found])
        return bool(case.get("remint", False))

    m._shared_remint = shared  # type: ignore[method-assign]
    disk = case.get("disk", {})

    def read(git_dir: str) -> Any:
        stub.log.append(["read", git_dir])
        return disk.get(git_dir)

    out = dict(case)
    out["tables"] = _spelled(case.get("tables", {}))
    f = case["f"]
    was = mr._token_read
    mr._token_read = read
    try:
        loc = _loc(case["loc"])
        if f == "rung_two":
            k, how, first, settled = mr._rung_two(m, loc)
            o: Any = [_id(k), how, _id(first), _id(settled)]
        elif f == "by_object":
            k, how = mr._by_object(m, loc)
            o = [_id(k), how]
        elif f == "token_now":
            got = mr._token_now(m, loc)
            o = [got.token, got.token_written, got.token_status, got is loc]
        else:
            raise AssertionError(f)
        out["o"] = enc(o)
    except Exception as e:  # noqa: BLE001
        out["o"] = _raised(e)
    finally:
        mr._token_read = was
    out["calls"] = enc(stub.log)
    return out


_OK = [_D, _I, None]


def _co(kid: str, **kw: Any) -> dict[str, Any]:
    return _k(kid, "P1", **kw)


def _rung(tables: list[Any], loc: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"f": "rung_two", "tables": {"checkouts": tables}, "loc": loc} | kw


def _rung_cases() -> list[dict[str, Any]]:
    lt = {"t": _T, "s": [_A], "x": False}
    lr = {"t": _R, "s": [_A], "x": False}
    born = [_D, _I, "1700000000.5"]
    k1 = _co("K1")
    k2 = _co("K2", token=_R)
    kb = _co("K1", object_birth="1700000000.5")
    kx = _co("K1", object_birth="1600000000.5")
    later = dict(_LATER, object_birth=None)
    there = dict(_THERE)
    heal_tabs = [_KEPT, later, there]
    out = [
        _rung([k1], dict(lt, o=None)),
        _rung([], lt),
        _rung([k1], lt),
        _rung([kb], dict(lt, o=born)),
        _rung([kx], dict(lt, o=born)),
        _rung([kb], lt),
        _rung([k1], dict(lt, o=born)),
        _rung([kx, k1], dict(lt, o=born)),
        _rung([_co("K1", object_device=None)], lt),
        _rung([_co("K1", object_inode=None)], lt),
        _rung([_co("K1", machine="M2")], dict(lt, m="M2")),
        _rung([_co("K1", retired=1)], lt, log=[["checkout_retired", '{"into": "K9"}']]),
        _rung([_co("K1", retired=1)], lt, log=[["checkout_retired", '{"into": ""}']]),
        _rung([_co("K1", retired=1)], lt, log=[]),
        _rung([k1], dict(lt, t=None)),
        _rung([k1], dict(lt, t="X" * 26)),
        _rung([k1], dict(lt, t="X" * 26), remint=True),
        _rung([k1], lr),
        _rung([k1], lr, remint=True),
        _rung([k1, k2], lr),
        _rung([k1, k2], lr, remint=True),
        _rung([k1, _co("K2")], lr, remint=True),
        _rung([k1, _co("K2", token="X" * 26)], lr, remint=True),
        _rung([k1, k2, _co("K3", token=_R)], lr, remint=True),
        _rung([k1, _co("K2", token=None)], dict(lr, t=None), remint=True),
        _rung([k1, kb, k2], dict(lr, o=born), remint=True),
        _rung([k1, kx, k2], dict(lr, o=born), remint=True),
        _rung(heal_tabs, lr, remint=True, log=[_JOIN]),
        _rung(heal_tabs, lr, remint=True, log=[_JOIN]),
        _rung(
            heal_tabs,
            lr,
            remint=True,
            log=[_JOIN],
        )
        | {
            "tables": {
                "checkouts": heal_tabs,
                "homes": [{"checkout": "K2", "project": "P2"}],
            }
        },
        _rung(heal_tabs, dict(lr, s=[_B]), remint=True, log=[_JOIN]),
        _rung(heal_tabs, dict(lr, s=[_A_CT]), remint=True, log=[_JOIN]),
        _rung(heal_tabs, lr, remint=True, log=[]),
        _rung([_KEPT, dict(later, token=None)], lr, remint=True, log=[_JOIN]),
        _rung([_co("K1", stamps_json="oops")], lt),
        _rung([_co("K1", retired=1), k2], lr, log=[["checkout_retired", "{bad"]]),
        # Once K1 is held as a re-mint, K2 carrying the token found is
        # continued by its token and ends the walk: K3, of the same
        # birth, is never reached.
        _rung(
            [k1, k2, _co("K3", object_birth="1700000000.5")],
            dict(lr, o=born),
            remint=True,
        ),
    ]
    out += [dict(c, f="by_object") for c in out[:3] + out[17:22] + out[27:30]]
    return out


def _now_cases() -> list[dict[str, Any]]:
    g = "/w/r/.git"
    base: dict[str, Any] = {
        "f": "token_now",
        "tables": {"checkouts": [_co("K1")]},
        "disk": {g: _OTHER},
    }
    lt = {"t": _R}
    out: list[dict[str, Any]] = [
        dict(base, loc=lt),
        dict(base, loc={"t": _T}),
        dict(base, loc={"t": None, "w": _R}),
        dict(base, loc={"t": None, "w": _R, "st": "absent"}),
        dict(base, loc={"t": _R, "st": "unreadable"}, disk={"/w/r/.git": _R}),
        dict(base, loc={"t": "", "w": _R}),
        dict(base, loc={"t": "", "w": ""}),
        dict(base, loc={"t": None, "w": None}),
        dict(base, loc=dict(lt, g=None)),
        dict(base, loc=dict(lt, g="")),
        dict(base, loc=dict(lt, o=None)),
        dict(base, loc=dict(lt, m="M2")),
        dict(base, loc=lt, here=False),
        dict(base, loc=lt, tables={"checkouts": []}),
        dict(base, loc=lt, tables={"checkouts": [_co("K1", machine="M2")]}),
        dict(base, loc=lt, tables={"checkouts": [_co("K1", object_inode="9")]}),
        dict(base, loc=lt, tables={"checkouts": [_co("K1"), _co("K2", token=_R)]}),
        dict(base, loc=lt, disk={}),
        dict(base, loc=lt, disk={g: _T}),
        dict(base, loc={"t": None, "w": _T}, disk={g: _T}),
        dict(base, loc={"t": None, "w": _T}),
        dict(base, loc=dict(lt, o=[_D, _I, "1.5"])),
        dict(
            base,
            loc=dict(lt, g="/w/r/.git/worktrees/x"),
            disk={"/w/r/.git/worktrees/x": "Y" * 26},
        ),
        dict(base, loc=lt, tables={"checkouts": [_co("K1", stamps_json="oops")]}),
    ]
    return out


def _token_read_cases(root: str) -> list[dict[str, Any]]:
    h = "bettermemory checkout token v1\n"
    specs: list[dict[str, Any]] = [
        {"file": h + _TOKEN + "\n"},
        {"file": h + _TOKEN},
        {"file": h + _TOKEN + "\n", "evicted": True},
        {"file": None},
        {"file": None, "git_dir": False},
        {"file": ""},
        {"file": h},
        {"file": _TOKEN + "\n"},
        {"file": h + _TOKEN.lower() + "\n"},
        {"file": "dir"},
        {"file": h + _TOKEN + "\n" + _TOKEN + "\n"},
        {"file": h + _TOKEN[:20]},
    ]
    out = []
    for i, spec in enumerate(specs):
        git_dir = os.path.join(root, f"g{i}", ".git")
        if spec.get("git_dir", True):
            os.makedirs(os.path.join(git_dir, "info"))
        f = spec["file"]
        path = os.path.join(git_dir, "info", "bettermemory")
        if f == "dir":
            os.makedirs(path)
        elif f is not None:
            with open(path, "w", encoding="ascii", newline="") as fh:
                fh.write(f)
        was = location._evicted
        if spec.get("evicted"):
            location._evicted = lambda st: True
        try:
            got = mr._token_read(git_dir)
        finally:
            location._evicted = was
        out.append({"f": "token_read", **spec, "o": got})
    return out


def generate() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as root:
        reads = _token_read_cases(root)
    rows = [_run(c) for c in [*_rung_cases(), *_now_cases()]]
    return {"membership_rung.jsonl": jsonl([*rows, *reads])}
