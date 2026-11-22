"""Fixtures: the refusals of `_handlers.py` (``tool_handlers.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/tool_handlers.rs``. This pins
`_refusals_as_tool_errors` (and through it `store_refusal`, `_read_only`,
`RetryableRefusal`, `_in_the_way` and `absent_text`) and
`_kept_or_retryable` (and `KeptWriteRefusal`) over tool bodies that answer
or raise a scripted exception.

No SQLite runs: each ``sqlite3.OperationalError`` is built with a fixed
message and its ``sqlite_errorcode`` set by hand, so no row holds the C
library's text. The store's classification is `store.retryable_code`.

A row's ``k`` is ``w`` (the wrapper) or ``kr`` (`_kept_or_retryable`,
``moved`` saying whether the body committed before it raised). ``b`` is
what the body does:

- ``{"ok": v}``: answers ``v`` (a str, or a list or dict of them);
- ``{"value": t}``: raises ``ValueError(t)``;
- ``{"absent": kind, "args": [...]}``: raises the store's error of that
  kind (``NotFound``, ``MemoryNotFound``, ``Tombstoned``,
  ``NotTombstoned``) with those args;
- ``{"sqlite": t, "code": c}``: raises ``OperationalError(t)`` with
  ``sqlite_errorcode`` ``c`` (absent where ``c`` is missing, a str where
  ``c`` is one);
- ``{"sqlite": t, "wide": d}``: the same with ``sqlite_errorcode``
  ``int(d)``, a decimal past 64 bits that JSON readers would round;
- ``{"tool": t}``, ``{"refused": t}``, ``{"retry": [code, t]}``,
  ``{"kept": [code, t]}``: raises that `ToolError`;
- ``{"other": t}``: raises ``RuntimeError(t)``.

``o`` is ``{"r": v}`` (the answer), ``{"cls", "text", "code",
"retryable"}`` (a `ToolError` raised) or ``{"raised": cls}`` (another
exception, as it is).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from bettermemory import _handlers as h
from bettermemory import store as st

TOKEN = "<|im_start|>"


def _raise(body: dict[str, Any]) -> Any:
    if "ok" in body:
        return body["ok"]
    if "value" in body:
        raise ValueError(body["value"])
    if "absent" in body:
        cls = {
            "NotFound": st.NotFoundError,
            "MemoryNotFound": st.MemoryNotFoundError,
            "Tombstoned": st.TombstonedError,
            "NotTombstoned": st.NotTombstonedError,
        }[body["absent"]]
        raise cls(*body["args"])
    if "sqlite" in body:
        exc = sqlite3.OperationalError(body["sqlite"])
        if "code" in body:
            exc.sqlite_errorcode = body["code"]
        if "wide" in body:
            exc.sqlite_errorcode = int(body["wide"])
        raise exc
    if "tool" in body:
        raise ToolError(body["tool"])
    if "refused" in body:
        raise h.RefusedError(body["refused"])
    if "retry" in body:
        raise h.RetryableRefusal(*body["retry"])
    if "kept" in body:
        raise h.KeptWriteRefusal(*body["kept"])
    raise RuntimeError(body["other"])


def _outcome(run: Any) -> dict[str, Any]:
    try:
        return {"r": asyncio.run(run())}
    except ToolError as exc:
        return {
            "cls": type(exc).__name__,
            "text": str(exc),
            "code": getattr(exc, "code", None),
            "retryable": getattr(exc, "retryable", None),
        }
    except Exception as exc:  # noqa: BLE001 - the class is the answer
        return {"raised": type(exc).__name__}


class _Store:
    def __init__(self) -> None:
        self.commits = 7


class _Deps:
    def __init__(self) -> None:
        self.store = _Store()


def _wrapped(body: dict[str, Any]) -> dict[str, Any]:
    @h._refusals_as_tool_errors
    async def tool() -> Any:
        return _raise(body)

    return _outcome(tool)


def _kept(body: dict[str, Any], moved: bool) -> dict[str, Any]:
    async def make(deps: Any) -> Any:
        if moved:
            deps.store.commits += 1
        return _raise(body)

    async def run() -> Any:
        return await h._kept_or_retryable(make, _Deps())  # type: ignore[arg-type]

    return _outcome(run)


def _bodies() -> list[dict[str, Any]]:
    s: list[dict[str, Any]] = [
        {"sqlite": "database is locked", "code": 5},
        {"sqlite": "database table is locked", "code": 6},
        {"sqlite": "database or disk is full", "code": 13},
        {"sqlite": "busy recovery", "code": 5 | (1 << 8)},
        {"sqlite": "busy snapshot", "code": 5 | (2 << 8)},
        {"sqlite": "locked shared cache", "code": 6 | (1 << 8)},
        {"sqlite": "readonly", "code": 8},
        {"sqlite": "readonly rollback", "code": 8 | (3 << 8)},
        {"sqlite": "readonly dbmoved", "code": 8 | (4 << 8)},
        {"sqlite": "negative", "code": -248},
        {"sqlite": "negative busy", "code": -251},
        {"sqlite": "low byte only", "code": 0x7FFFFFFFFFFFFF05},
        {"sqlite": "readonly bit 7", "code": 0x88},
        {"sqlite": "wide busy", "wide": str(2**64 + 5)},
        {"sqlite": "wide readonly", "wide": str(-(2**70) + 8)},
        {"sqlite": "an error", "code": 1},
        {"sqlite": "no code"},
        {"sqlite": "none code", "code": None},
        {"sqlite": "str code", "code": "5"},
        {"sqlite": "zero", "code": 0},
        {"sqlite": "corrupt", "code": 11},
        {"sqlite": "", "code": 5},
        {"sqlite": f"token {TOKEN} in a lock", "code": 5},
        {"sqlite": f"token {TOKEN} read only", "code": 8},
        {"sqlite": f"token {TOKEN} plain", "code": 1},
        {"sqlite": "eé\U0001f600", "code": 13},
    ]
    return [
        {"ok": "plain"},
        {"ok": f"a {TOKEN} b"},
        {"ok": [f"{TOKEN}", "x", {"k": f"[INST] {TOKEN}"}]},
        {"ok": {f"key {TOKEN}": f"v {TOKEN}"}},
        {"value": "an invalid scope"},
        {"value": f"quoted {TOKEN} id"},
        {"value": ""},
        {"absent": "NotFound", "args": ["abc"]},
        {"absent": "NotFound", "args": [f"id {TOKEN}"]},
        {"absent": "NotFound", "args": ["a", "b"]},
        {"absent": "NotFound", "args": []},
        {"absent": "NotFound", "args": [7]},
        {"absent": "MemoryNotFound", "args": ["memory x was not found"]},
        {"absent": "Tombstoned", "args": [f"removed {TOKEN}"]},
        {"absent": "NotTombstoned", "args": ["still active"]},
        {"absent": "MemoryNotFound", "args": ["a", 1]},
        {"tool": f"a tool error {TOKEN}"},
        {"refused": f"already refused {TOKEN}"},
        {"retry": ["store_busy", f"held {TOKEN}"]},
        {"retry": ["store_full", "full"]},
        {"kept": ["store_busy", "late"]},
        {"kept": ["store_full", "late full"]},
        {"other": "boom"},
        *s,
    ]


def generate() -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    for body in _bodies():
        rows.append({"k": "w", "b": body, "o": _wrapped(body)})
        for moved in (False, True):
            rows.append({"k": "kr", "moved": moved, "b": body, "o": _kept(body, moved)})
    header = {"fixture": "tool_handlers", "count": len(rows)}
    lines = [json.dumps(r, sort_keys=True) for r in [header, *rows]]
    return {"tool_handlers.jsonl": ("\n".join(lines) + "\n").encode("ascii")}
