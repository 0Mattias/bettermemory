"""Fixtures: the migration's report and its pure text helpers
(``membership_migrate_report.jsonl``, ``membership_migrate_report_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/membership_migrate``. This pins the
module's constants, `RootDecision`, `RootlessHome`, `LegacyQuestion`,
`MigrationTotals` and `MigrationReport` (``homed``, ``to_dict``,
``lines``), and `_basename`, `_origin`, `_text`, `_id_order`,
`_repository_name`, `_shas` and `_json_list`.

Every value is `_common.neutral`'s description. A line is ``{"f":
function, "a": [args], "o": answer}``; ``o`` is ``{"raised": class}``
where the reference raised. A report case is ``{"f": "report", "a":
report, "o": {"homed", "to_dict", "lines"}}``, the report given as its
fields (``roots``, ``rootless`` and ``proposals`` as lists of field
dicts). `_basename` reads ``os.path``, which is the host's; its rows
carry the answer with ``os.path`` set to `posixpath` (``posix``) and to
`ntpath` (``nt``), both pure Python on every host, so the file is the
same everywhere. A text with a backslash is written as code points.

``membership_migrate_report_python.jsonl`` is gated on the Python
release: `_origin` and `_json_list` of JSON nested past 1,000 levels,
where the decoder stops recursing as the release, the system and the
caller's own stack depth decide (3.14 on Linux reads 100,000 levels; under
3.13 a text nested 9,998 deep reads alone and raises under pytest, so no
case lies within a few hundred levels of the limit). The ungated file nests 900 levels
at most, which every release reads. No case holds malformed JSON within a
few levels of the decoder's limit: there CPython 3.13 raises RecursionError
for some errors and ValueError for others (the error's own construction
meets the limit), which `json.loads` of the crate does not follow (see the
native README). A deep answer is written as
``{"t": "chain"}`` (`_chain`).
"""

from __future__ import annotations

import dataclasses
import hashlib
import ntpath
import os
import posixpath
from typing import Any
from unittest import mock

from bettermemory import membership_migrate as mm

from ._common import jsonl, neutral

_FILE = "membership_migrate_report.jsonl"
_PYTHON = "membership_migrate_report_python.jsonl"


def _n(value: Any) -> Any:
    """`neutral`, with a text that holds a backslash as code points."""
    if isinstance(value, str) and "\\" in value:
        return {"t": "str", "v_cps": [ord(c) for c in value]}
    if isinstance(value, (list, tuple)):
        kind = "tuple" if isinstance(value, tuple) else "list"
        return {"t": kind, "v": [_n(v) for v in value]}
    if isinstance(value, dict):
        return {"t": "dict", "v": [[_n(k), _n(v)] for k, v in value.items()]}
    return neutral(value)


def _chain(value: Any) -> Any:
    """A value nested deeper than 16 levels, each level a list of one item
    or a dict of one entry, as ``{"t": "chain", "kinds": [...], "leaf":
    leaf}``, outermost first: each kind ``"l"`` or the dict's one key,
    written as runs ``[kind, count]``.
    Walked without recursion; anything else is `_n`'s."""
    kinds: list[str] = []
    v = value
    while True:
        if isinstance(v, list) and len(v) == 1:
            kinds.append("l")
            v = v[0]
        elif isinstance(v, dict) and len(v) == 1:
            ((k, v),) = v.items()
            kinds.append(k)
        else:
            break
    if len(kinds) <= 16:
        return _n(value)
    runs: list[list[Any]] = []
    for k in kinds:
        if runs and runs[-1][0] == k:
            runs[-1][1] += 1
        else:
            runs.append([k, 1])
    return {"t": "chain", "kinds": runs, "leaf": _n(v)}


def _call(f: Any, *args: Any) -> Any:
    try:
        answer = f(*args)
    except RecursionError:
        return {"raised": "RecursionError"}
    return _chain(answer)


def _constants() -> dict[str, Any]:
    return {
        "f": "constants",
        "o": _n(
            {
                "LIVE": mm.LIVE,
                "GONE": mm.GONE,
                "REPLACED": mm.REPLACED,
                "UNREACHABLE": mm.UNREACHABLE,
                "RULE_PREFIX": mm.RULE_PREFIX,
                "LEGACY_REASON": mm.LEGACY_REASON,
                "MIGRATED": mm.MIGRATED,
                "_RECORD_TABLES": [list(t) for t in mm._RECORD_TABLES],
                "_RECORD_KINDS": list(mm._RECORD_KINDS),
                "_PROBE_BUDGET": mm._PROBE_BUDGET,
                "_PROBE_WORKERS": mm._PROBE_WORKERS,
            }
        ),
    }


_BASENAMES = [
    "",
    "/",
    "//",
    "/a",
    "/a/b",
    "/a/b/",
    "/a/b//",
    "a",
    "a/",
    "a/b",
    "a\\b",
    "a\\b\\",
    "\\",
    "C:",
    "C:/",
    "C:\\",
    "C:\\x",
    "C:x",
    "C:x/y",
    "c:/Data/x",
    "1:x",
    "\u00e9:x",
    ":x",
    "//server",
    "//server/",
    "//server/share",
    "//server/share/",
    "//server/share/x",
    "//server/share/x/y",
    "//?/UNC/server/share/x",
    "//?/unc/server/share",
    "//?/UnC/s/sh/z",
    "//./device/x",
    "//?/C:/x",
    "///x",
    "/C:/x",
    "x:",
    "\u00e9",
    "/a/\ud800",
    "/a/b c/",
    "/.git",
]


def _basename_rows() -> list[dict[str, Any]]:
    rows = []
    for p in _BASENAMES:
        answers = {}
        for key, flavour in (("posix", posixpath), ("nt", ntpath)):
            with mock.patch.object(os, "path", flavour):
                answers[key] = _n(mm._basename(p))
        rows.append({"f": "basename", "a": [_n(p)], **answers})
    return rows


class Rep:
    """A long text as a recipe: the pieces repeated, joined, written
    ``{"t": "rep", "v": [[piece, count], ...]}``."""

    def __init__(self, *runs: tuple[str, int]) -> None:
        self.runs = runs
        self.text = "".join(piece * count for piece, count in runs)


def _arg(value: Any) -> Any:
    if isinstance(value, Rep):
        return {"t": "rep", "v": [list(r) for r in value.runs]}
    return _n(value)


def _raw(value: Any) -> Any:
    return value.text if isinstance(value, Rep) else value


_ORIGINS: list[Any] = [
    None,
    "",
    0,
    1,
    0.0,
    True,
    False,
    [],
    [1],
    {},
    {"a": 1},
    (),
    "{}",
    '{"a": 1}',
    '{"a": 1, "a": 2}',
    "[]",
    "[1]",
    "1",
    "null",
    '"x"',
    "{",
    "not json",
    " {} ",
    '{"x": "\\ud800"}',
    "NaN",
    '{"n": NaN}',
    Rep(("1", 4300)),
    Rep(("1", 4301)),
    Rep(("[", 900), ("]", 900)),
    Rep(('{"a":', 900), ("1", 1), ("}", 900)),
]


def _deep_texts() -> list[Rep]:
    return [
        Rep(("[", 1500), ("]", 1500)),
        Rep(('{"a":', 1500), ("1", 1), ("}", 1500)),
        Rep(("[", 100_000)),
        Rep(("[", 100_000), ("]", 100_000)),
    ]


def _helper_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in _ORIGINS:
        rows.append(
            {"f": "origin", "a": [_arg(raw)], "o": _call(mm._origin, _raw(raw))}
        )
        rows.append(
            {"f": "json_list", "a": [_arg(raw)], "o": _call(mm._json_list, _raw(raw))}
        )
    origins = [
        None,
        {},
        {"k": "v"},
        {"k": " v "},
        {"k": ""},
        {"k": "   "},
        {"k": "\u00a0\u3000"},
        {"k": "\u200b"},
        {"k": "\x1c"},
        {"k": 1},
        {"k": None},
        {"k": ["v"]},
        {"k": True},
        {"other": "v"},
        {"k": "\ud800"},
    ]
    for o in origins:
        for key in ("k", "K", ""):
            rows.append({"f": "text", "a": [_n(o), _n(key)], "o": _n(mm._text(o, key))})
    for i in ("", "P1", "P9", "P10", "P\ud800", "\u00e9\u00e9"):
        rows.append({"f": "id_order", "a": [_n(i)], "o": _n(list(mm._id_order(i)))})
    keys = [
        "",
        "github.com/acme/app",
        "github.com/acme/app.git",
        "github.com/acme/app.git/",
        "github.com/acme/app.git.git",
        "github.com/acme/.git",
        ".git",
        "local:",
        "local:/a/b",
        "local:/a/b.git",
        "local:local:x",
        "LOCAL:/a/b",
        "local:\\a\\b",
        "a\\b.git",
        "/",
        "///",
        "local:///",
        "x/",
        "app.GIT",
        "app.git ",
        "a/b/\ud800",
    ]
    for k in keys:
        rows.append(
            {"f": "repository_name", "a": [_n(k)], "o": _n(mm._repository_name(k))}
        )
    sha_sets: list[list[Any]] = [
        [],
        ["abcd", "abc", "ABCD", " abcd ", "abcg", "0123456789"],
        ["a" * 64, "a" * 65, "a" * 63, "a" * 4],
        [1, None, ("abcd",), ["abcd"], 0xABCD, 1.5, "dead\n", "\u00a0beef\u3000"],
        ["\u0430bcd", "\uff41bcd", "abcd\u200b", "\u212abcd", "ABCD\u0130"],
        ["", "    ", "abcd", "ABCD", "abcd"],
        ["12 34", "1234\x00", "\x1c1234\x1f", "deadbeef" * 8],
    ]
    for vs in sha_sets:
        rows.append({"f": "shas", "a": [_n(vs)], "o": _n(sorted(mm._shas(vs)))})
    return rows


_SWEEP_ALPHABET = "0123456789abcdefABCDEFgG \n\t\u00a0\u212a"
_SWEEP_COUNT = 20_000


def _sweep_texts() -> list[str]:
    """The seeded texts of the `_shas` sweep: a 64-bit LCG (multiplier
    6364136223846793005, increment 1442695040888963407, seed 1), each draw
    its high 31 bits; a length below 71, then each code point from
    `_SWEEP_ALPHABET`."""
    x = 1
    mask = (1 << 64) - 1

    def draw() -> int:
        nonlocal x
        x = (x * 6364136223846793005 + 1442695040888963407) & mask
        return x >> 33

    out = []
    for _ in range(_SWEEP_COUNT):
        n = draw() % 71
        out.append(
            "".join(_SWEEP_ALPHABET[draw() % len(_SWEEP_ALPHABET)] for _ in range(n))
        )
    return out


def _sweep_row() -> dict[str, Any]:
    """Each sweep text alone through `_shas`: the answer's one member or
    nothing, a line each, SHA-256 of the lines in order."""
    digest = hashlib.sha256()
    for t in _sweep_texts():
        got = sorted(mm._shas([t]))
        digest.update(((got[0] if got else "-") + "\n").encode())
    return {
        "f": "shas_sweep",
        "alphabet": _n(_SWEEP_ALPHABET),
        "n": _SWEEP_COUNT,
        "sha256": digest.hexdigest(),
    }


def _fields(obj: Any) -> dict[str, Any]:
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}


def _report_case(report: mm.MigrationReport) -> dict[str, Any]:
    args = {
        "dry_run": report.dry_run,
        "kept": report.kept,
        "machine": report.machine,
        "roots": [_fields(r) for r in report.roots],
        "rootless": [_fields(r) for r in report.rootless],
        "proposals": [_fields(p) for p in report.proposals],
        "totals": _fields(report.totals),
        "homes_by_rule": dict(report.homes_by_rule),
        "remint": list(report.remint),
    }
    return {
        "f": "report",
        "a": _n(args),
        "o": {
            "homed": report.homed,
            "to_dict": _n(report.to_dict()),
            "lines": _n(report.lines()),
        },
    }


def _reports() -> list[mm.MigrationReport]:
    root = mm.RootDecision(
        path="/w/app",
        state=mm.LIVE,
        project="P1",
        project_name="app",
        checkout="C1",
        rule="migration:root",
        memories=3,
        episodes=2,
        tombstones=1,
    )
    gone = mm.RootDecision(
        path='/w/"odd"\nname\u202e',
        state=mm.GONE,
        project="P10",
        project_name="",
        checkout=None,
        rule="migration:gone",
        memories=0,
        episodes=0,
        tombstones=0,
        recorded=7,
        held={"C9": 2, "C10": 5, "C2": 0, "\u00e9": 1},
    )
    empty_checkout = dataclasses.replace(
        root, checkout="", project_name="a \u201cq\u201d"
    )
    held_one = dataclasses.replace(root, held={"C1": 1}, recorded=1)
    rootless = mm.RootlessHome(
        record="M1",
        kind="memory",
        cwd="/w/app/sub",
        root="/w/app",
        project="P1",
        checkout="C1",
        rule="migration:cwd_in_root",
    )
    carried = mm.RootlessHome(
        record="E2",
        kind="episode",
        cwd="C:\\w\\x",
        root="github.com/acme/app",
        project="P3",
        checkout=None,
        rule="migration:remote_ambiguous",
        carriers=("P3", "P2", "P3"),
    )
    blank = dataclasses.replace(rootless, checkout="", cwd="", root="\ud800")
    question = mm.LegacyQuestion(
        proposal="Q1",
        checkout="C1",
        at="/w/app",
        legacy_project="L1",
        root="/w/old",
        line="the records may belong here",
    )
    odd_question = dataclasses.replace(question, at="\x00\x7f\u2028", line="a\nb")
    totals = mm.MigrationTotals(*range(1, 13))
    big = mm.MigrationTotals(
        memory_homes=2**40, tombstone_homes=-3, episode_homes=2**40
    )
    return [
        mm.MigrationReport(dry_run=False, kept="committed", machine="m1"),
        mm.MigrationReport(dry_run=True, kept="rolled back", machine=""),
        mm.MigrationReport(
            dry_run=True,
            kept="",
            machine="m\ud800",
            roots=(root, gone, empty_checkout, held_one),
            rootless=(rootless, carried, blank),
            proposals=(question, odd_question),
            totals=totals,
            homes_by_rule={"migration:root": 3, "migration:cwd": 1, "Z": 0, "a": -1},
            remint=("C2", "C1", ""),
        ),
        mm.MigrationReport(
            dry_run=False,
            kept="committed",
            machine="m1",
            roots=(gone,),
            totals=big,
            remint=("C\u00e9",),
        ),
    ]


def generate() -> dict[str, bytes]:
    """``membership_migrate_report.jsonl``: not gated."""
    rows: list[dict[str, Any]] = [_constants()]
    rows += _basename_rows()
    rows += _helper_rows()
    rows.append(_sweep_row())
    rows += [_report_case(r) for r in _reports()]
    return {_FILE: jsonl(rows)}


def generate_python() -> dict[str, bytes]:
    """``membership_migrate_report_python.jsonl``: gated on the Python
    release (where the JSON decoder stops recursing)."""
    rows: list[dict[str, Any]] = []
    for raw in _deep_texts():
        rows.append(
            {"f": "origin", "a": [_arg(raw)], "o": _call(mm._origin, _raw(raw))}
        )
        rows.append(
            {"f": "json_list", "a": [_arg(raw)], "o": _call(mm._json_list, _raw(raw))}
        )
    return {_PYTHON: jsonl(rows)}
