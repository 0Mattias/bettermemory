"""Fixtures: the bounded reads of recorded paths (``location_reads.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_reads.rs``. This pins it to
`location_reads`' `bounded_read` and `bounded_reads`, and through them
`_board`, `_Slots`, `_stalled`, `_give_up`, `_fly` and `_Lanes`.

A case is a SEQUENCE of calls over one board (`_FLIGHTS` and `_SLOTS`
fresh for the case), with `READ_SLOTS` and `READ_HANDOFF` patched to the
case's ``slots`` and ``handoff``. A read is spelled ``v:x`` (answers
``x`` at once), ``e:x`` (raises with the text ``x``) or ``h:k`` (blocks
on the event ``k``, then answers ``late:k``). An event is set only after
the call that read it returned, where a call names it in ``rel``; the
next call starts once each read it released has landed, and every event
is set at the end of the case.

A call is ``one`` (`bounded_read` of ``[path, what, read]``) or ``many``
(`bounded_reads` of such jobs, with ``raises``), each with its budget
``b``. Each answers ``r``: ``["v", x]``, ``["u"]`` (`UNANSWERED`),
``["n"]`` (`NOT_READ`) or ``["e", x]`` (raised); a batch a list of them,
or ``["e", x]`` where it raised. ``ran`` is the reads the call started,
sorted. No row holds a time: budgets are far from what a read takes.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest import mock

from bettermemory import location_reads as lr

from ._common import jsonl


class Fail(Exception):
    pass


def S(path: str, what: str, read: str) -> list[str]:
    return [path, what, read]


def one(job: list[str], b: float, rel: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"one": job, "b": b, "rel": list(rel)}


def many(
    jobs: list[list[str]], b: float, raises: bool = False, rel: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {"many": jobs, "b": b, "raises": raises, "rel": list(rel)}


def _cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []

    def case(
        name: str, calls: list[dict[str, Any]], slots: int = 64, handoff: float = 0.01
    ) -> None:
        c.append({"name": name, "slots": slots, "handoff": handoff, "calls": calls})

    case("value", [one(S("/a", "x", "v:1"), 2.0)])
    case("raises", [one(S("/a", "x", "e:boom"), 2.0)])
    case("hung", [one(S("/a", "x", "h:k"), 0.2)])
    case(
        "hung then stalled answers at once",
        [
            one(S("/a", "x", "h:k"), 0.2),
            one(S("/a", "x", "v:2"), 0.2),
            one(S("/a", "y", "v:3"), 0.2),
        ],
    )
    case(
        "stalled other what, healthy other path",
        [
            one(S("/a", "x", "h:k"), 0.2),
            one(S("/a", "y", "v:3"), 0.2),
            one(S("/b", "x", "v:4"), 0.2),
        ],
    )
    case(
        "a larger budget waits the flight out",
        [
            one(S("/a", "x", "h:k"), 0.2),
            one(S("/a", "y", "v:3"), 0.9),
            one(S("/a", "x", "v:5"), 0.9),
        ],
    )
    case(
        "released then read afresh",
        [
            one(S("/a", "x", "h:k"), 0.2, rel=("k",)),
            one(S("/a", "x", "v:2"), 0.2),
            one(S("/a", "y", "e:no"), 0.2),
        ],
    )
    case(
        "batch healthy",
        [many([S("/a", "x", "v:1"), S("/b", "x", "v:2"), S("/c", "y", "v:3")], 2.0)],
    )
    case(
        "batch duplicates",
        [
            many(
                [
                    S("/a", "x", "v:1"),
                    S("/a", "x", "v:2"),
                    S("/a", "y", "v:3"),
                    S("/b", "x", "v:4"),
                    S("/a", "x", "e:e1"),
                ],
                2.0,
            )
        ],
    )
    case(
        "batch errors without raises",
        [
            many(
                [S("/a", "x", "e:one"), S("/b", "x", "v:2"), S("/c", "x", "e:two")], 2.0
            )
        ],
    )
    case(
        "batch errors with raises",
        [
            many(
                [S("/a", "x", "v:1"), S("/c", "x", "e:two"), S("/b", "x", "e:one")],
                2.0,
                raises=True,
            )
        ],
    )
    case(
        "batch raises by job order across paths",
        [
            many(
                [
                    S("/a", "x", "h:k"),
                    S("/a", "y", "e:late"),
                    S("/b", "x", "e:first"),
                    S("/c", "x", "e:third"),
                ],
                0.3,
                raises=True,
            )
        ],
    )
    case(
        "batch raises with no error",
        [many([S("/a", "x", "v:1"), S("/b", "x", "h:k")], 0.3, raises=True)],
    )
    case(
        "batch hung path then its other jobs",
        [
            many(
                [
                    S("/a", "x", "h:k"),
                    S("/a", "y", "v:2"),
                    S("/a", "x", "v:3"),
                    S("/b", "x", "v:4"),
                ],
                0.3,
            ),
            one(S("/a", "z", "v:5"), 0.3),
            many([S("/a", "x", "v:6"), S("/a", "q", "v:7")], 0.3),
        ],
    )
    case(
        "batch over a path stalled by an earlier call",
        [
            one(S("/a", "x", "h:k"), 0.2),
            many([S("/a", "x", "v:1"), S("/a", "y", "v:2"), S("/b", "x", "v:3")], 0.2),
        ],
    )
    case(
        "batch with a larger budget waits on an earlier read",
        [
            one(S("/a", "x", "h:k"), 0.2),
            many([S("/a", "x", "v:1"), S("/a", "y", "v:2"), S("/b", "x", "v:3")], 0.9),
        ],
    )
    case(
        "batch waits on a read released meanwhile",
        [
            one(S("/a", "x", "h:k"), 0.2, rel=("k",)),
            many([S("/a", "x", "v:1"), S("/a", "y", "v:2")], 0.2),
        ],
    )
    case(
        "more hung paths than slots",
        [
            many(
                [S(f"/h{i}", "x", f"h:k{i}") for i in range(5)]
                + [S("/ok", "x", "v:ok")],
                0.25,
            ),
            many(
                [S("/h0", "x", "v:a"), S("/h4", "y", "v:b"), S("/ok", "x", "v:c")], 0.25
            ),
        ],
        slots=2,
    )
    case(
        "a slot held by a single read delays a batch",
        [
            one(S("/a", "x", "h:k"), 0.1),
            many([S("/b", "x", "v:1"), S("/c", "x", "v:2")], 0.5),
        ],
        slots=1,
    )
    case(
        "a single read takes no slot",
        [many([S("/a", "x", "h:k")], 0.2), one(S("/b", "x", "v:1"), 0.5)],
        slots=1,
    )
    case(
        "hung reads past the lanes",
        [
            many(
                [S(f"/p{i}", "x", f"h:k{i}") for i in range(12)]
                + [S("/q", "x", "v:q")],
                0.3,
            )
        ],
    )
    case(
        "handoff at the budget gives up",
        [many([S("/a", "x", "h:k"), S("/b", "x", "v:2")], 0.05)],
        handoff=0.5,
    )
    case(
        "empty batch",
        [many([], 1.0), many([], 1.0, raises=True)],
    )
    return c


def _answer(value: Any) -> list[Any]:
    if value is lr.UNANSWERED:
        return ["u"]
    if value is lr.NOT_READ:
        return ["n"]
    return ["v", value]


def _run(case: dict[str, Any]) -> list[dict[str, Any]]:
    events: dict[str, threading.Event] = {}
    ran: list[str] = []
    ran_lock = threading.Lock()
    flights: dict[str, Any] = {}

    keys: dict[str, tuple[str, str]] = {}

    def make(path: str, what: str, spec: str) -> Any:
        kind, _, arg = spec.partition(":")

        def read() -> str:
            with ran_lock:
                ran.append(spec)
            if kind == "e":
                raise Fail(arg)
            if kind == "h":
                events.setdefault(arg, threading.Event()).wait()
                return f"late:{arg}"
            return arg

        if kind == "h":
            events.setdefault(arg, threading.Event())
            keys[arg] = (path, what)
        return read

    out = []
    with (
        mock.patch.object(lr, "READ_SLOTS", case["slots"]),
        mock.patch.object(lr, "READ_HANDOFF", case["handoff"]),
        mock.patch.object(lr, "_FLIGHTS", flights),
        mock.patch.object(lr, "_SLOTS", lr._Slots()),
    ):
        try:
            for call in case["calls"]:
                del ran[:]
                row: dict[str, Any] = {}
                try:
                    if "one" in call:
                        p, w, spec = call["one"]
                        row["r"] = _answer(
                            lr.bounded_read(p, w, make(p, w, spec), call["b"])
                        )
                    else:
                        jobs = [(p, w, make(p, w, spec)) for p, w, spec in call["many"]]
                        got = lr.bounded_reads(jobs, call["b"], raises=call["raises"])
                        row["r"] = [_answer(v) for v in got]
                except Fail as exc:
                    row["r"] = ["e", str(exc)]
                with ran_lock:
                    row["ran"] = sorted(ran)
                for k in call["rel"]:
                    events[k].set()
                _settle(flights, [keys[k] for k in call["rel"]])
                out.append(row)
        finally:
            for e in events.values():
                e.set()
            _settle(flights, None)
    return out


def _settle(flights: dict[str, Any], keys: list[tuple[str, str]] | None) -> None:
    """Wait until the released reads (`keys`; None for every read) are off
    the board."""
    end = time.monotonic() + 10
    while time.monotonic() < end:
        with lr._FLIGHTS_LOCK:
            if keys is None:
                left = any(flights.values())
            else:
                left = any(w in flights.get(p, {}) for p, w in keys)
        if not left:
            return
        time.sleep(0.002)
    raise RuntimeError("a released read did not land")


def generate() -> dict[str, bytes]:
    cases = _cases()
    rows: list[Any] = [
        {
            "cases": len(cases),
            "consts": [repr(lr.READ_HANDOFF), lr.READ_LANES, lr.READ_SLOTS],
        }
    ]
    for case in cases:
        rows.append({**case, "want": _run(case)})
    return {"location_reads.jsonl": jsonl(rows)}
