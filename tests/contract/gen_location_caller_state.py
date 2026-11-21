"""Fixtures: the per-process state around a caller's resolution
(``location_caller_state.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_caller.rs``. This pins it
to `location`'s `process_machine`, `_recorded`, `_clear_machine`,
`_location_lock` (with `_Held` and `_LOCKS`), `_cwd_of`, `_keep_stand`,
`_stand_of`, `forget_session`, `_clear_stands`, `_same_namespace` and
`_with_file_errors`, each driven directly.

Nothing reads the machine: `open_machine`, `record_machine`,
``os.getcwd``, `object_key` and `_hostname` are the case's. A lock is the
reference's table with each `_Held`'s lock a lock that never blocks, so
one thread can take a location twice, as two threads would.

Each line holds ``k``, the function, its inputs and ``r``:

- ``name``: the name `_location_lock` gives ``loc`` (``m``, ``cwd``,
  ``root``, ``key`` as ``[device, inode, birth]`` or null);
- ``locks``: ``ops`` over an empty table, each ``["e", cwd]`` (enter the
  lock of a location of machine ``m`` at ``cwd``), ``["x", cwd]`` (leave
  the latest entered there) or ``["f", n, prefix]`` (enter and leave
  ``n`` locations ``prefix0`` up); ``r`` the table afterwards as
  `_table` writes it;
- ``stands``: ``ops`` over an empty table, each ``["k", store, session,
  cwd, checkout]`` (`_keep_stand`, machine ``m``), ``["f", n, prefix]``
  (``n`` stands in store ``S``, sessions ``prefix0`` up, checkout
  ``c``), ``["g", session]`` (`forget_session`), ``["c"]``
  (`_clear_stands`) or ``["o", store, session]`` (`_stand_of`, its
  ``[machine, path, checkout]`` or null appended to ``o``); ``r`` the
  table as `_table` writes it;
- ``machine``: ``ops``, each ``["p", id]`` (`process_machine` with
  `open_machine` answering a machine of that id, or raising where it is
  null), ``["r", store, id, fails]`` (`_recorded`, `record_machine`
  raising where ``fails``) or ``["c"]`` (`_clear_machine`); ``r`` one
  answer per op: the id, ``"raised"``, ``"opened"`` counts, or for
  ``r`` whether `record_machine` was called and whether it raised;
- ``cwd``: `_cwd_of(cwd)` with ``os.getcwd`` answering ``g`` (raising
  where null);
- ``ns``: `_same_namespace` with `object_key` answering ``here`` and
  `_hostname` answering ``local``; ``r`` the answer and which of the two
  were asked;
- ``errs_cp``: as ``errs``, each text a list of code points, for texts
  with lone surrogates, which a JSON reader of UTF-8 does not take.
- ``errs``: `_with_file_errors` over a resolution with ``lines``; ``r``
  its lines afterwards (the same object where ``errors`` is empty:
  ``same``).

`_table` writes a table as its length, its first and last three entries
and the SHA-256 of every entry's line, in order (a lock's ``name\\tusers``,
a stand's ``store\\tsession\\tmachine\\tpath\\tcheckout``).
"""

from __future__ import annotations

import contextlib
import hashlib
import types
from typing import Any
from unittest import mock

from bettermemory import location as L
from bettermemory import machine as M
from bettermemory.membership_records import Location, ObjectKey, Resolution

from ._common import jsonl

CAP = L.CACHE_CAP * 4


class _Free:
    """A lock that never blocks."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


def _table(entries: list[str]) -> dict[str, Any]:
    digest = hashlib.sha256("".join(e + "\n" for e in entries).encode()).hexdigest()
    return {"n": len(entries), "head": entries[:3], "tail": entries[-3:], "sha": digest}


def _loc(cwd: str, root: str | None = None, key: list[Any] | None = None) -> Location:
    ok = None if key is None else ObjectKey(key[0], key[1], key[2])
    return Location(machine="m", cwd=cwd, state="outside", root=root, object_key=ok)


def _name(m: str, cwd: str, root: str | None, key: list[Any] | None) -> dict[str, Any]:
    loc = _loc(cwd, root, key)
    loc = Location(
        machine=m, cwd=cwd, state="outside", root=root, object_key=loc.object_key
    )
    seen: list[str] = []
    real = L._LOCKS
    L._LOCKS = {}
    try:
        with L._location_lock(loc):
            seen = list(L._LOCKS)
    finally:
        L._LOCKS = real
    return {"k": "name", "m": m, "cwd": cwd, "root": root, "key": key, "r": seen[0]}


def _locks(ops: Any) -> dict[str, Any]:
    real = L._LOCKS
    L._LOCKS = {}
    open_: dict[str, list[contextlib.AbstractContextManager[None]]] = {}
    free = types.SimpleNamespace(Lock=_Free)
    try:
        with mock.patch.object(L, "threading", free):
            for op in ops:
                if op[0] == "e":
                    cm = L._location_lock(_loc(op[1]))
                    cm.__enter__()
                    open_.setdefault(op[1], []).append(cm)
                elif op[0] == "x":
                    open_[op[1]].pop().__exit__(None, None, None)
                else:
                    for i in range(op[1]):
                        with L._location_lock(_loc(f"{op[2]}{i}")):
                            pass
        table = _table([f"{k}\t{v.users}" for k, v in L._LOCKS.items()])
    finally:
        L._LOCKS = real
    return {"k": "locks", "ops": ops, "r": table}


def _res(checkout: str | None, lines: tuple[str, ...] = ()) -> Resolution:
    return Resolution(
        outcome="silent", project="p", checkout=checkout, rule="r", lines=lines
    )


def _stands(ops: Any) -> dict[str, Any]:
    L._clear_stands()
    out: list[Any] = []
    for op in ops:
        if op[0] == "k":
            store = types.SimpleNamespace(path=op[1])
            L._keep_stand(store, op[2], _loc(op[3]), _res(op[4]))  # type: ignore[arg-type]
        elif op[0] == "f":
            store = types.SimpleNamespace(path="S")
            for i in range(op[1]):
                L._keep_stand(store, f"{op[2]}{i}", _loc("w"), _res("c"))  # type: ignore[arg-type]
        elif op[0] == "g":
            L.forget_session(op[1])
        elif op[0] == "c":
            L._clear_stands()
        else:
            got = L._stand_of(types.SimpleNamespace(path=op[1]), op[2])  # type: ignore[arg-type]
            out.append(
                None
                if got is None
                else [got.machine, got.path, got.resolution.checkout]
            )
    entries = [
        f"{s}\t{k}\t{v.machine}\t{v.path}\t{v.resolution.checkout}"
        for (s, k), v in L._STANDS.items()
    ]
    L._clear_stands()
    return {"k": "stands", "ops": ops, "o": out, "r": _table(entries)}


def _machine(ops: Any) -> dict[str, Any]:
    L._clear_machine()
    out: list[Any] = []
    for op in ops:
        if op[0] == "p":
            opened: list[int] = []

            def open_machine(
                ident: str | None = op[1], opened: list[int] = opened
            ) -> Any:
                opened.append(1)
                if ident is None:
                    raise OSError("unopened")
                return types.SimpleNamespace(id=ident)

            with mock.patch.object(M, "open_machine", open_machine):
                try:
                    out.append([L.process_machine().id, len(opened)])
                except OSError:
                    out.append(["raised", len(opened)])
        elif op[0] == "r":
            calls: list[int] = []

            def record(
                identity: Any, fails: bool = op[3], calls: list[int] = calls
            ) -> None:
                calls.append(1)
                if fails:
                    raise OSError("unrecorded")

            model = types.SimpleNamespace(record_machine=record)
            store = types.SimpleNamespace(path=op[1])
            ident = types.SimpleNamespace(id=op[2])
            try:
                L._recorded(store, model, ident)  # type: ignore[arg-type]
                out.append([len(calls), False])
            except OSError:
                out.append([len(calls), True])
        else:
            L._clear_machine()
            out.append(None)
    L._clear_machine()
    return {"k": "machine", "ops": ops, "r": out}


def _cwd(cwd: str | None, g: str | None) -> dict[str, Any]:
    def getcwd() -> str:
        if g is None:
            raise FileNotFoundError(2, "gone")
        return g

    with mock.patch("os.getcwd", getcwd):
        r = L._cwd_of(cwd)
    return {"k": "cwd", "cwd": cwd, "g": g, "r": r}


def _ns(
    key: list[Any] | None, here: list[Any] | None, host: str | None, local: str | None
) -> dict[str, Any]:
    asked: list[str] = []

    def object_key(path: str) -> Any:
        asked.append("key")
        return None if here is None else tuple(here)

    def hostname() -> str | None:
        asked.append("host")
        return local

    with (
        mock.patch.object(L, "object_key", object_key),
        mock.patch.object(L, "_hostname", hostname),
    ):
        r = L._same_namespace("/c", None if key is None else tuple(key), host)
    return {
        "k": "ns",
        "key": key,
        "here": here,
        "host": host,
        "local": local,
        "r": [r, asked],
    }


def _cps(texts: list[str]) -> list[list[int]]:
    return [[ord(c) for c in t] for t in texts]


def _errs_cp(lines: list[str], errors: list[str]) -> dict[str, Any]:
    row = _errs(lines, errors)
    return {
        "k": "errs_cp",
        "lines": _cps(lines),
        "errors": _cps(errors),
        "r": _cps(row["r"]),
        "same": row["same"],
    }


def _errs(lines: list[str], errors: list[str]) -> dict[str, Any]:
    res = _res("c", tuple(lines))
    got = L._with_file_errors(res, errors)
    return {
        "k": "errs",
        "lines": lines,
        "errors": errors,
        "r": list(got.lines),
        "same": got is res,
    }


def _lines() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for m, cwd, root, key in [
        ("m", "/w", None, None),
        ("m", "/w", "/r", None),
        ("m", "/w", "", None),
        ("m", "", None, None),
        ("", "/w", "/r", ["1", "2", None]),
        ("m", "/w", "/r", ["1", "2", "3"]),
        ("m:x", "/w", None, ["", "", ""]),
        ("m", "/a:bé\n", None, None),
    ]:
        rows.append(_name(m, cwd, root, key))
    for ops in [
        [["e", "a"], ["e", "b"]],
        [["e", "a"], ["e", "a"], ["x", "a"]],
        [["e", "a"], ["x", "a"], ["e", "b"], ["e", "a"]],
        [["f", CAP - 1, "z"]],
        [["f", CAP, "z"]],
        [["f", CAP, "z"], ["e", "new"]],
        [["f", CAP, "z"], ["e", "z5"]],
        [["e", "held"], ["f", CAP - 1, "z"], ["e", "new"]],
        [["f", 10, "z"], ["e", "held"], ["f", CAP, "y"], ["e", "new"], ["x", "held"]],
        [["e", "a"], ["e", "b"], ["f", CAP - 2, "z"], ["x", "a"], ["e", "c"]],
        [["f", CAP * 2, "z"]],
    ]:
        rows.append(_locks(ops))
    for ops in [
        [
            ["k", "S", "s1", "/w", "c"],
            ["o", "S", "s1"],
            ["o", "T", "s1"],
            ["o", "S", None],
        ],
        [["k", "S", None, "/w", "c"], ["o", "S", None]],
        [["k", "S", "s1", "/w", "c"], ["k", "S", "s1", "/v", None], ["o", "S", "s1"]],
        [
            ["k", "S", "a", "/w", "c"],
            ["k", "S", "b", "/w", "c"],
            ["k", "S", "a", "/v", "d"],
        ],
        [
            ["k", "S", "a", "/w", "c"],
            ["k", "T", "a", "/w", "c"],
            ["k", "S", "b", "/w", "c"],
            ["g", "a"],
        ],
        [["k", "S", "a", "/w", ""], ["o", "S", "a"], ["g", "zz"]],
        [
            ["k", "S", "a", "/w", "c"],
            ["k", "T", "a", "/v", "c"],
            ["o", "S", "a"],
            ["o", "T", "a"],
        ],
        [["f", CAP, "s"]],
        [
            ["f", CAP, "s"],
            ["k", "T", "x", "/w", "c"],
            ["o", "S", "s0"],
            ["o", "S", "s1"],
        ],
        [["f", CAP, "s"], ["k", "S", "s0", "/w", "c"]],
        [["f", CAP, "s"], ["k", "S", "s3", "/w", None], ["k", "S", "n", "/w", "c"]],
        [["f", CAP + 5, "s"], ["c"], ["k", "S", "a", "/w", "c"]],
    ]:
        rows.append(_stands(ops))
    for ops in [
        [["p", "A"], ["p", "B"], ["c"], ["p", "B"]],
        [["p", None], ["p", "A"], ["p", None]],
        [
            ["r", "S", "A", False],
            ["r", "S", "A", False],
            ["r", "T", "A", False],
            ["r", "S", "B", False],
        ],
        [["r", "S", "A", True], ["r", "S", "A", False], ["r", "S", "A", False]],
        [["r", "S", "A", False], ["c"], ["r", "S", "A", False]],
    ]:
        rows.append(_machine(ops))
    for given, g in [
        ("/w", "/g"),
        ("/w", None),
        (None, "/g"),
        (None, None),
        ("", "/g"),
    ]:
        rows.append(_cwd(given, g))
    k12 = ["1", "2", None]
    for key, here, host, local in [
        (None, ["1", "2", None], None, None),
        (k12, None, None, None),
        (k12, ["1", "2", None], None, None),
        (k12, ["1", "3", None], None, None),
        (k12, ["9", "2", None], None, None),
        (["1", "2", "5"], ["1", "2", None], None, None),
        (["1", "2", "5"], ["1", "2", "5"], None, None),
        (["1", "2", "5"], ["1", "2", "6"], None, None),
        (k12, ["1", "2", "6"], None, None),
        (k12, k12, "h", "h"),
        (k12, k12, "h", "g"),
        (k12, k12, "h", None),
        (k12, k12, "", None),
        (k12, k12, "", ""),
        (k12, ["1", "3", None], "h", "h"),
    ]:
        rows.append(_ns(key, here, host, local))
    for lines, errors in [
        (["a"], []),
        ([], ["x"]),
        (["a"], ["a", "b"]),
        (["a"], ["b", "b"]),
        (["a\\nb"], ["a\nb"]),
        (["a\nb"], ["a\nb"]),
        ([], ["t\tr\r\x00\x7f\x85é ퟿﻿"]),
        (["q"], ["", ""]),
    ]:
        rows.append(_errs(lines, errors))
    rows.append(_errs_cp(["\ud800"], ["\ud800", "x\udfffy"]))
    return rows


def generate() -> dict[str, bytes]:
    lines = _lines()
    header = {"fixture": "location_caller_state", "cases": len(lines)}
    return {"location_caller_state.jsonl": jsonl([header, *lines])}
