"""Fixtures: the estate progress cursor (``health_roots.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/health_roots.rs``. This pins it to
`health_roots.EstateProgress` (`ordered`, `attempt`).

The first line is a header with the count of rows. Then one row per case:
a fresh progress gets ``cursor`` and ``retry_root`` (null or text), then
runs ``steps`` in order, each ``["o", roots]`` (`ordered`, answering the
list) or ``["a", root, iso]`` (`attempt` at that time) or ``["r", root]``
(sets ``retry_root``). ``out`` holds each `ordered` answer in order, and
``cursor``, ``retry_root`` and ``checked`` (``[[root, iso], ...]``, oldest
first) at the end. Text goes through `text_field` (a list of them for
roots), so a lone surrogate travels as code points.

The last line is the cap: ``count`` attempts on the roots ``r<i % mod>``
spelled by `_cap_root`, at times one second apart from `_BASE`, then the
digest of the checked roots in order with the cursor and the order of
the roots ``r0`` to ``r<mod - 1>``. Everything is code point order and
ASCII or code points, so the file is not gated.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory.health_roots import EstateProgress

from ._common import jsonl, text_field

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CAP_COUNT = 3000
CAP_MOD = 1500


def _t(i: int) -> datetime:
    return _BASE + timedelta(seconds=i)


def _texts(items: list[str]) -> list[dict[str, Any]]:
    return [text_field("v", s) for s in items]


def _opt(name: str, value: str | None) -> dict[str, Any]:
    return {name: None} if value is None else text_field(name, value)


_ROOTS = ["/b", "/a", "/c", "/a", "/é", "/Z", "/b/x", "/\ud800", "/\U0001f600", "/￿"]

CASES: list[tuple[str | None, str | None, list[Any]]] = [
    (None, None, [["o", []], ["o", _ROOTS]]),
    ("/b", None, [["o", _ROOTS]]),
    ("/bb", None, [["o", _ROOTS]]),
    ("/\U0010ffff", None, [["o", _ROOTS]]),
    ("", None, [["o", _ROOTS]]),
    (None, "/c", [["o", _ROOTS]]),
    ("/a", "/c", [["o", _ROOTS]]),
    ("/a", "/cc", [["o", _ROOTS]]),
    ("/a", "/a", [["o", _ROOTS]]),
    ("/\ud800", "/\ud800", [["o", _ROOTS]]),
    (None, None, [["a", "/b", 0], ["o", _ROOTS], ["a", "/c", 1], ["o", _ROOTS]]),
    (None, "/a", [["a", "/b", 0], ["o", _ROOTS], ["r", "/a"], ["o", _ROOTS]]),
    (None, None, [["a", "/x", 0], ["a", "/y", 1], ["a", "/x", 2], ["o", ["/x", "/y"]]]),
    (None, None, [["a", "/q", 0], ["a", "/q", 0], ["o", ["/q"]]]),
    ("/zzz", None, [["o", ["/a"]], ["o", ["/zzz"]], ["o", ["/zzz", "/a"]]]),
    (None, "/a", [["a", "/b", 3], ["o", ["/a", "/b"]]]),
]


def _run(cursor: str | None, retry: str | None, steps: list[Any]) -> dict[str, Any]:
    p = EstateProgress(cursor=cursor, retry_root=retry)
    outs: list[Any] = []
    for step in steps:
        if step[0] == "o":
            outs.append(_texts(p.ordered(step[1])))
        elif step[0] == "a":
            p.attempt(step[1], _t(step[2]))
        else:
            p.retry_root = step[1]
    wire: list[Any] = []
    for step in steps:
        if step[0] == "o":
            wire.append(["o", _texts(step[1])])
        elif step[0] == "a":
            wire.append(["a", text_field("v", step[1]), _t(step[2]).isoformat()])
        else:
            wire.append(["r", text_field("v", step[1])])
    return {
        **_opt("start_cursor", cursor),
        **_opt("start_retry", retry),
        "steps": wire,
        "out": outs,
        **_opt("cursor", p.cursor),
        **_opt("retry_root", p.retry_root),
        "checked": [[text_field("v", r), w.isoformat()] for r, w in p.checked.items()],
        "caller_first": p.caller_first,
    }


def _cap_root(i: int) -> str:
    return f"r{i % CAP_MOD}"


def cap_digest() -> str:
    p = EstateProgress()
    for i in range(CAP_COUNT):
        p.attempt(_cap_root(i), _t(i))
    lines = [json.dumps([r, w.isoformat()]) for r, w in p.checked.items()]
    lines.append(json.dumps(p.cursor))
    lines.append(json.dumps(p.ordered([_cap_root(i) for i in range(CAP_MOD)])))
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def generate() -> dict[str, bytes]:
    rows = [_run(c, r, s) for c, r, s in CASES]
    cap = {
        "cap": CAP_COUNT,
        "mod": CAP_MOD,
        "base": _BASE.isoformat(),
        "sha256": cap_digest(),
    }
    header = {"fixture": "health_roots", "count": len(rows)}
    return {"health_roots.jsonl": jsonl([header, *rows, cap])}
