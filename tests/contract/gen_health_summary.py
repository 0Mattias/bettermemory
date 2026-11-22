"""Fixtures: the two health answers (``health_summary.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/health_summary.rs``. This pins it to
`health_summary.health_summary`, `health_section` and `section_names`
over hand-built reports.

The first line is a header with the count of rows. Then one row per case:
``report`` (the report in `_common.neutral` form, insertion order kept),
``op`` (``summary``, ``names`` or ``section`` with ``name``), and the
answer: ``out`` in neutral form, or ``error`` (the exception's class)
with ``text`` (``str(exc)``, through `text_field`) for a KeyError or a
ValueError.

Every value is one whose spelling is the same under every interpreter and
platform (a datetime's ``str`` included; non-ASCII text is printable in
every Unicode release the interpreters carry), so the file is not gated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bettermemory import health_summary as hs

from ._common import jsonl, neutral, text_field

_WHEN = datetime(2026, 11, 21, 8, 30, 5, 120, tzinfo=timezone.utc)

_FULL: dict[str, Any] = {
    "generated_at": "2026-11-21T08:30:05Z",
    "window_days": 30,
    "total_active_memories": 0,
    "total_events": 12,
    "distinct_sessions": 3,
    "recommendations": [
        {
            "kind": "stale",
            "count": 5,
            "action": "verify them",
            "memory_ids": ["a", "b", "c", "d"],
        },
        {
            "kind": "scoped",
            "count": 2,
            "action": "narrow",
            "scope": "p1, p2, p3, p4",
            "memory_ids": ["x"],
        },
        {"kind": "bare", "count": 0, "action": "nothing"},
        {"kind": "emptyids", "count": 1, "action": "a", "memory_ids": [], "scope": ""},
        {"kind": "intscope", "count": 1.5, "action": True, "scope": 7},
        {"kind": "commas", "count": 1, "action": "x", "scope": "a,b, c,,  d, "},
    ],
    "membership": {"projects": ["é", "日本"], "zero": 0},
    "empty_list": [],
    "empty_dict": {},
    "none": None,
    "zero": 0,
    "false": False,
    "blank": "",
    "when": _WHEN,
    "nested": {"when": [_WHEN], "f": 0.1, "big": 10**30},
}

_PARTIAL: dict[str, Any] = {
    "window_days": 7,
    "generated_at": "later",
    "recommendations": None,
    "only": [1, 2],
}

_ODD_RECS: list[Any] = [
    {"recommendations": [{"count": 1, "action": "x"}]},
    {"recommendations": [{"kind": "k", "action": "x"}]},
    {"recommendations": [{"kind": "k", "count": 1}]},
    {
        "recommendations": [
            {"kind": "k", "count": 1, "action": "x", "memory_ids": [1, 2]}
        ]
    },
    {
        "recommendations": [
            {"kind": "k", "count": 1, "action": "x", "memory_ids": [1], "scope": "s"}
        ]
    },
    {
        "recommendations": [
            {"kind": "k", "count": 1, "action": "x", "memory_ids": "abcd"}
        ]
    },
    {
        "recommendations": [
            {"kind": "k", "count": 1, "action": "x", "memory_ids": {"k1": 1, "k2": 2}}
        ]
    },
    {"recommendations": [{"kind": "k", "count": 1, "action": "x", "memory_ids": 5}]},
    {"recommendations": [{"kind": "k", "count": 1, "action": "x", "memory_ids": 0}]},
    {"recommendations": [{"kind": "k", "count": None, "action": "x", "scope": _WHEN}]},
    {"recommendations": [{"kind": "k", "count": 1, "action": "x", "scope": [1, 2]}]},
    {"recommendations": [{"kind": "k", "count": 1, "action": "x", "scope": ", , "}]},
    {"recommendations": ["not a dict"]},
    {"recommendations": "s"},
    {"recommendations": 3},
    {"recommendations": 0},
    {"recommendations": {}},
    {"recommendations": ({"kind": "t", "count": 1, "action": "x"},)},
    {"recommendations": []},
    {"generated_at": None, "window_days": [], "total_events": {}},
    {},
]

_SECTIONS = [
    "membership",
    "membership,when",
    " membership , zero ,, ",
    "when,membership,when",
    "　membership\x1c\x85",
    "​membership",
    "",
    " , ",
    "missing",
    "membership, missing, other",
    "generated_at",
    "é",
    "it's",
    "\ud800",
    "Membership",
    "empty_list,none,zero,false,blank",
]


def _case(report: dict[str, Any], op: str, name: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"report": neutral(report), "op": op}
    if name is not None:
        row.update(text_field("name", name))
    try:
        if op == "summary":
            out: Any = hs.health_summary(report)
        elif op == "names":
            out = hs.section_names(report)
        else:
            assert name is not None
            out = hs.health_section(report, name)
    except (KeyError, ValueError) as exc:
        text = repr(exc.args[0]) if isinstance(exc, KeyError) else str(exc)
        return {**row, "error": type(exc).__name__, **text_field("text", text)}
    except (TypeError, AttributeError) as exc:
        return {**row, "error": type(exc).__name__}
    return {**row, "out": neutral(out)}


def generate() -> dict[str, bytes]:
    rows = [
        _case(_FULL, "summary"),
        _case(_FULL, "names"),
        _case(_PARTIAL, "summary"),
        _case(_PARTIAL, "names"),
    ]
    rows += [_case(r, "summary") for r in _ODD_RECS]
    rows += [_case(_FULL, "section", n) for n in _SECTIONS]
    rows += [_case(_PARTIAL, "section", n) for n in ["only", "window_days", ""]]
    rows.append(_case({}, "section", "x"))
    header = {"fixture": "health_summary", "count": len(rows)}
    return {"health_summary.jsonl": jsonl([header, *rows])}
