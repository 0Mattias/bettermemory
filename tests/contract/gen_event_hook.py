"""Fixtures: `events`' suppression switch and a hook's deadline
(``event_hook.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/event_hook.rs``. This pins it to
`events.suppressed`, `deadline`, `within_hook_budget`, `hook_body`,
`_store_busy`, `_NOT_DONE` and `StoreBusy`'s text.

Rows:

- ``{"k": "busy", "c": class, "code": code, "r": bool}``: `_store_busy` of
  an exception of `class` whose ``sqlite_errorcode`` is `code` (absent
  where null; a text where it is not an ``int``);
- ``{"k": "msg", "e": event, "r": text}``: `StoreBusy`'s text;
- ``{"k": "run", "w": busy timeout or null, "f": index or null, "t":
  tree, "r": trace}``: a tree of nested blocks run over one fake store
  (none where ``w`` is null) whose ``set_busy_timeout`` call number ``f``
  (from 0) raises ``sqlite3.ProgrammingError`` as a closed connection's
  does, the clock stubbed by argument. A float that JSON cannot hold
  (``w`` or a node's clock) is its ``repr``, such as ``"nan"``. A node is ``["b", event, now,
  store?, children]`` (`within_hook_budget`; ``store?`` whether the store
  is passed), ``["h", event, now, store?, children]`` (a ``hook_body``
  decorated function called with ``store=`` the store or ``store=None``),
  ``["s", children]`` (`suppressed`) or ``["x", class, code]`` (raise).
  The trace holds ``["in", deadline, suppressed]`` as each block's body
  starts, ``["set", seconds]`` for each ``set_busy_timeout``, ``["out",
  deadline, suppressed]`` after each block, each deadline as ``repr``,
  then ``["ok"]`` or ``["raised", class, text or code]``, and last
  ``["end", deadline, suppressed]`` as the row's context holds them
  after it (a deadline the reference leaves set shows there).

The clock is `time.monotonic` (what `events` reads), replaced through ``mock.patch`` for
each node's single reading and restored on exit; every ContextVar is
reset by the reference's own ``finally`` or, where the reference leaves
one set, dropped with the copy of the context each row runs in.
"""

from __future__ import annotations

import contextvars
import math
import sqlite3
import time
from typing import Any
from unittest import mock

from bettermemory import events

from ._common import jsonl

EVENTS = ("session-start", "prompt", "stop", "other", "")
CLASSES = ("OperationalError", "IntegrityError", "DatabaseError", "ValueError")
CODES: tuple[Any, ...] = (
    None,
    0,
    1,
    5,
    6,
    8,
    261,
    262,
    517,
    5 + 0x100 * 255,
    0x105 + 0x10000,
    -1,
    -251,
    -250,
    2**62 + 5,
    -(2**63) + 6,
    True,
    "5",
)


def _exc(cls: str, code: Any) -> BaseException:
    kind: type[BaseException] = getattr(sqlite3, cls, None) or ValueError
    made: Any = kind("boom")
    if code is not None:
        made.sqlite_errorcode = code
    return made


def _spell_code(code: Any) -> Any:
    if isinstance(code, bool):
        return int(code)
    return code


class _Store:
    def __init__(self, waited: float, fail: int | None, trace: list[Any]) -> None:
        self.busy_timeout = waited
        self._fail = fail
        self._calls = 0
        self._trace = trace

    def set_busy_timeout(self, seconds: float) -> None:
        call = self._calls
        self._calls += 1
        if call == self._fail:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        self._trace.append(["set", repr(seconds)])
        self.busy_timeout = seconds


def _state() -> list[Any]:
    at = events.deadline()
    return [None if at is None else repr(at), events._SUPPRESSED.get()]


def _run(node: list[Any], store: _Store | None, trace: list[Any]) -> None:
    tag = node[0]
    if tag == "x":
        raise _exc(node[1], node[2])
    if tag == "s":
        try:
            with events.suppressed():
                trace.append(["in", *_state()])
                for child in node[1]:
                    _run(child, store, trace)
        finally:
            trace.append(["out", *_state()])
        return
    _, event, spelt, passed, children = node
    now = _float(spelt)
    given = store if passed else None

    def body() -> None:
        trace.append(["in", *_state()])
        for child in children:
            _run(child, store, trace)

    try:
        with mock.patch.object(time, "monotonic", return_value=now):
            if tag == "b":
                with events.within_hook_budget(event, given):  # type: ignore[arg-type]
                    body()
            else:

                @events.hook_body(event)
                def fn(store: Any = None) -> None:
                    body()

                fn(store=given)
    finally:
        trace.append(["out", *_state()])


def _float(spelt: Any) -> float:
    return float(spelt) if isinstance(spelt, str) else spelt


def _spell_float(value: float | None) -> Any:
    if value is None or math.isfinite(value):
        return value
    return repr(value)


def _run_row(
    waited: float | None, tree: list[Any], fail: int | None = None
) -> dict[str, Any]:
    return contextvars.copy_context().run(_run_row_here, waited, tree, fail)


def _run_row_here(
    waited: float | None, tree: list[Any], fail: int | None
) -> dict[str, Any]:
    trace: list[Any] = []
    store = None if waited is None else _Store(waited, fail, trace)
    try:
        _run(tree, store, trace)
        trace.append(["ok"])
    except events.StoreBusy as exc:
        cause = exc.__cause__
        trace.append(["raised", "StoreBusy", str(exc), type(cause).__name__])
    except Exception as exc:
        trace.append(
            [
                "raised",
                type(exc).__name__,
                _spell_code(getattr(exc, "sqlite_errorcode", None)),
            ]
        )
    trace.append(["end", *_state()])
    return {"k": "run", "w": _spell_float(waited), "f": fail, "t": tree, "r": trace}


def _trees() -> list[tuple[float | None, list[Any], int | None]]:
    out: list[tuple[float | None, list[Any], int | None]] = [
        (w, t, None) for w, t in _plain_trees()
    ]
    # set_busy_timeout raising: the lowering call before the try leaves the
    # new deadline set; the restoring call in finally replaces the body's
    # outcome and leaves the timeout at the budget.
    for fail in (0, 1):
        for children in (
            [],
            [["x", "OperationalError", 5]],
            [["x", "ValueError", None]],
        ):
            out.append((30.0, ["b", "prompt", 2.0, True, children], fail))
            out.append((30.0, ["h", "stop", 2.0, True, children], fail))
        out.append(
            (30.0, ["b", "stop", 1.0, False, [["b", "prompt", 2.0, True, []]]], fail)
        )
    out.append((5.0, ["b", "prompt", 2.0, True, []], 0))
    # NaN: a NaN busy timeout is not above the budget; a NaN clock makes a
    # NaN deadline, which a later min keeps and an earlier one does not
    # replace.
    nan = "nan"
    out.append((math.nan, ["b", "prompt", 2.0, True, []], None))
    out.append((math.inf, ["b", "prompt", 2.0, True, []], None))
    out.append((30.0, ["b", "prompt", nan, True, []], None))
    out.append(
        (None, ["b", "stop", nan, False, [["b", "prompt", 1.0, False, []]]], None)
    )
    out.append(
        (None, ["b", "stop", 1.0, False, [["b", "prompt", nan, False, []]]], None)
    )
    out.append(
        (None, ["b", "stop", "inf", False, [["b", "prompt", "-inf", False, []]]], None)
    )
    return out


def _plain_trees() -> list[tuple[float | None, list[Any]]]:
    out: list[tuple[float | None, list[Any]]] = []
    for event in EVENTS:
        for waited in (None, 5.0, 8.0, 13.0, 30.0, 0.0):
            out.append((waited, ["b", event, 100.0, True, []]))
        out.append((30.0, ["b", event, 100.0, False, []]))
        out.append((30.0, ["h", event, 0.5, True, []]))
        out.append((30.0, ["h", event, 0.5, False, []]))
    # Nested deadlines: the earlier one wins, and each comes back.
    for inner_now in (90.0, 100.0, 105.0, 200.0, 0.1):
        out.append(
            (
                30.0,
                [
                    "b",
                    "stop",
                    100.0,
                    True,
                    [["b", "prompt", inner_now, True, []], ["s", []]],
                ],
            )
        )
    out.append((None, ["b", "prompt", 100.0, True, [["b", "stop", 95.0, True, []]]]))
    out.append(
        (9.0, ["b", "prompt", 1.0, True, [["b", "session-start", 1.0, True, []]]])
    )
    # Errors: busy codes become StoreBusy, others pass through; the store
    # and the deadline come back either way.
    for event in EVENTS:
        for cls, code in (
            ("OperationalError", 5),
            ("OperationalError", 6),
            ("OperationalError", 517),
            ("OperationalError", 262),
            ("OperationalError", 8),
            ("OperationalError", None),
            ("IntegrityError", 5),
            ("ValueError", None),
        ):
            out.append((30.0, ["b", event, 7.0, True, [["x", cls, code]]]))
    out.append((30.0, ["h", "stop", 7.0, True, [["x", "OperationalError", 5]]]))
    # A nested hook's StoreBusy is a RuntimeError to the outer one.
    out.append(
        (
            30.0,
            [
                "b",
                "stop",
                7.0,
                True,
                [["b", "prompt", 8.0, True, [["x", "OperationalError", 6]]]],
            ],
        )
    )
    out.append(
        (30.0, ["s", [["b", "prompt", 3.0, True, [["x", "OperationalError", 261]]]]])
    )
    # Suppressed nesting.
    out.append((None, ["s", [["s", []], ["b", "prompt", 1.0, False, [["s", []]]]]]))
    out.append(
        (
            None,
            ["b", "prompt", 1.0, False, [["s", [["s", [["x", "ValueError", None]]]]]]],
        )
    )
    return out


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for cls in CLASSES:
        for code in CODES:
            row_code = _spell_code(code)
            rows.append(
                {
                    "k": "busy",
                    "c": cls,
                    "code": row_code,
                    "r": events._store_busy(_exc(cls, code)),
                }
            )
    rows += [{"k": "msg", "e": e, "r": str(events.StoreBusy(_msg(e)))} for e in EVENTS]
    rows += [_run_row(w, t, f) for w, t, f in _trees()]
    header = {"k": "header", "count": len(rows)}
    return {"event_hook.jsonl": jsonl([header, *rows])}


def _msg(event: str) -> str:
    """The text `within_hook_budget` raises for `event`, taken from a run."""
    row = _run_row(30.0, ["b", event, 0.0, True, [["x", "OperationalError", 5]]])
    return str(row["r"][-2][2])
