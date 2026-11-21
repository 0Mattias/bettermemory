"""Fixtures: `membership_rows` (all of it): the membership table rows
read into records (`_checkout_from`, `_view_from`, `_project_from`,
`_proposal_from`) and written back (`_checkout_row`, `_project_row`,
`_proposal_row`), the stamp codecs (`_stamps_from`, `_ticks_from`,
`_stamps_json`), `_jsonable`, `_signature` (with `Location.root_file`)
and `_unwritten`.

Two files, one JSON object per line, keys sorted, ASCII:

- ``membership_rows.jsonl``, not gated. The first line is a header (the
  base rows, `TICKS_KEY`, `_TOKEN_NAME`, the count). Every other line is
  ``{"f": function, ...}``: a row case ``{"f", "d", "drop", "o"}`` is the
  function's base row with ``d`` put in place and ``drop`` left out; a
  checkout case adds ``views``. ``o`` is the record written back by the
  row writer (with what the writer leaves out beside it), ``{"raised":
  [class, text]}`` or ``{"refused": true}`` where the reference passes a
  value through that is neither str nor None (or a ``seen_order``
  outside a 64-bit int), which the port refuses. The text is kept only
  for a missing column's KeyError and two of CPython's conversion texts
  (`_KEPT`); else null, and the class alone is compared. The last line is
  a digest over 2,000 checkout rows drawn by `_Lcg` from `_POOLS`.
- ``membership_rows_python.jsonl``, gated on the Python release: `int()`
  of non-ASCII digits and whitespace, and lines past ASCII through
  `printable`.

A value is written as JSON where JSON holds it; else ``{"$d": [[k, v],
...]}`` (a dict, in order), ``{"$t": [...]}`` (a tuple), ``{"$i":
"digits"}`` (an int past 2**53), ``{"$f": "nan"}`` (a float JSON has no
spelling for), ``{"$h": [length, SHA-256 of its UTF-8]}`` (a str
past 256 code points, output only), ``{"$c": [code points]}`` (a str with a backslash or a
surrogate), ``{"$p": n}`` (10 to the power n, n at least 16) and ``{"$n":
[depth, leaf]}`` (that many lists around the leaf, input only).
Recorded paths and names are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from bettermemory import membership_records as mr
from bettermemory import membership_rows as rows

from ._common import jsonl


def enc(v: Any) -> Any:
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        if abs(v) < 2**53:
            return v
        if v > 0 and 10 ** round(math.log10(v)) == v:
            return {"$p": round(math.log10(v))}
        return {"$i": str(v)}
    if isinstance(v, float):
        return v if math.isfinite(v) else {"$f": repr(v)}
    if isinstance(v, str):
        if len(v) > 256:
            digest = hashlib.sha256(v.encode("utf-8", "surrogatepass")).hexdigest()
            return {"$h": [len(v), digest]}
        if "\\" in v or any(0xD800 <= ord(c) <= 0xDFFF for c in v):
            return {"$c": [ord(c) for c in v]}
        return v
    if isinstance(v, tuple):
        return {"$t": [enc(x) for x in v]}
    if isinstance(v, list):
        return [enc(x) for x in v]
    if isinstance(v, dict):
        return {"$d": [[enc(k), enc(x)] for k, x in v.items()]}
    raise TypeError(type(v).__name__)


def dec(v: Any) -> Any:
    if isinstance(v, list):
        return [dec(x) for x in v]
    if not isinstance(v, dict):
        return v
    ((tag, body),) = v.items()
    if tag == "$d":
        return {dec(k): dec(x) for k, x in body}
    if tag == "$t":
        return tuple(dec(x) for x in body)
    if tag == "$i":
        return int(body)
    if tag == "$f":
        return float(body)
    if tag == "$c":
        return "".join(chr(c) for c in body)
    if tag == "$p":
        return 10**body
    depth, leaf = body
    out = dec(leaf)
    for _ in range(depth):
        out = [out]
    return out


_KEPT = re.compile(
    r"'int' object is not iterable"
    r"|dictionary update sequence element #\d+ has length \d+; 2 is required"
)


def _raised(e: Exception) -> dict[str, Any]:
    kept = isinstance(e, KeyError) or _KEPT.fullmatch(str(e)) is not None
    return {"raised": [type(e).__name__, str(e) if kept else None]}


def _typed(*values: Any) -> bool:
    return all(v is None or isinstance(v, str) for v in values)


_CHECKOUT = {
    "id": "K1",
    "project": "P1",
    "machine": "M1",
    "kind": "primary",
    "object_device": "16777220",
    "object_inode": "123",
    "object_birth": "1700000000.5",
    "common_device": None,
    "common_inode": None,
    "common_birth": None,
    "git_dir": "/work/r/.git",
    "token": "tok",
    "token_status": "written",
    "stamps_json": [["description", 1, 2, 3, 4], {"ticks": True}],
    "last_head": "abc",
    "branch": "main",
    "remotes_json": ["origin"],
    "manifest_json": None,
    "legacy": 0,
    "retired": 0,
    "registered": "2026-01-01T00:00:00",
    "unresolved_json": None,
}
_VIEW = {
    "checkout": "K1",
    "machine": "M1",
    "path": "/work/r",
    "first_seen": "t1",
    "last_seen": "t2",
    "seen_order": 3,
}
_PROJECT = {
    "id": "P1",
    "name": "alpha",
    "merged_into": None,
    "provisional": None,
    "parent": None,
    "tenant": None,
    "folder": 0,
    "boundary": 0,
    "minted": "2026-01-01T00:00:00",
}
_PROPOSAL = {
    "id": "Q1",
    "checkout": "K1",
    "kind": "first_sight",
    "candidates_json": ["P1", "P2"],
    "evidence_json": {"dir": "/work/r", "n": 2},
    "reason": "two",
    "line": "a line",
    "state": "open",
    "answer": None,
    "answered_by": None,
    "signature_json": ["h", ["k"], None, "tok", False],
    "opened": "2026-01-01T00:00:00",
}
_BASES: dict[str, dict[str, Any]] = {
    "checkout": _CHECKOUT,
    "view": _VIEW,
    "project": _PROJECT,
    "proposal": _PROPOSAL,
}


def _view_tuple(v: mr.View) -> list[Any]:
    return [v.checkout, v.machine, v.path, v.first_seen, v.last_seen, v.order]


def _answer(f: str, row: dict[str, Any], views: list[mr.View]) -> Any:
    try:
        if f == "checkout":
            k = rows._checkout_from(row, views)
            if not _typed(k.git_dir, k.token, k.last_head, k.branch, k.replaced_by):
                return {"refused": True}
            return {
                "row": enc(rows._checkout_row(k)),
                "views": enc([_view_tuple(v) for v in k.views]),
                "sessions": enc(list(k.sessions)),
                "replaced_by": enc(k.replaced_by),
            }
        if f == "view":
            v = rows._view_from(row)
            if not -(2**63) <= v.order < 2**63:
                return {"refused": True}
            return enc(_view_tuple(v))
        if f == "project":
            p = rows._project_from(row)
            if not _typed(p.merged_into, p.provisional, p.parent, p.tenant):
                return {"refused": True}
            return enc(rows._project_row(p))
        pr = rows._proposal_from(row)
        if not _typed(pr.checkout, pr.answer, pr.answered_by):
            return {"refused": True}
    except Exception as e:  # noqa: BLE001 - every raise is the answer
        return _raised(e)
    try:
        written = enc(rows._proposal_row(pr))
    except Exception as e:  # noqa: BLE001
        written = _raised(e)
    return {"row": written, "ev": enc(pr.evidence), "sig": enc(pr.signature)}


def _case(
    f: str,
    d: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
    views: list[list[Any]] | None = None,
) -> dict[str, Any]:
    d = d or {}
    row = {k: v for k, v in _BASES[f].items() if k not in drop}
    row.update({k: dec(v) for k, v in d.items()})
    built = [mr.View(*v) for v in views or []]
    o = _short(f, _answer(f, row, built))
    out: dict[str, Any] = {"f": f, "d": d, "drop": list(drop), "o": o}
    if views is not None:
        out["views"] = views
    return out


def _full(f: str) -> Any:
    """The answer for the function's base row, in full."""
    return _answer(f, dict(_BASES[f]), [])


def _short(f: str, o: Any) -> Any:
    """An answer written against the base row's (`_full`): ``w`` the
    columns of the row written back that differ, then every other key
    that differs. A raise, a refusal and a view are written whole."""
    if f == "view" or "raised" in o or "refused" in o:
        return o
    base = _full(f)
    if f == "project":
        o, base = {"row": o}, {"row": base}
    out = {k: v for k, v in o.items() if k != "row" and base[k] != v}
    row = o["row"]
    if "$d" in row and "$d" in base["row"]:
        assert [c for c, _ in row["$d"]] == [c for c, _ in base["row"]["$d"]]
        was = dict(base["row"]["$d"])
        out["w"] = {c: v for c, v in row["$d"] if was[c] != v}
    else:
        out["row"] = row
    return out


_STR_ODD: list[Any] = [
    5,
    -0,
    1.5,
    {"$f": "nan"},
    True,
    None,
    [1, "a"],
    {"$t": ["a"]},
    {"$d": [["a", 1]]},
    "",
    {"$c": [0x5C, 0xD800]},
    {"$p": 4299},
    {"$p": 4300},
    {"$n": [50, 1]},
]
_TEXT_ODD: list[Any] = [5, 1.5, True, [], {"$d": []}, "", "x"]
_INT_ODD: list[Any] = [
    "12",
    " -7 ",
    "+0",
    "1_000",
    "1__0",
    "_1",
    "0x10",
    "",
    " ",
    "\t9\n",
    "1.5",
    1.9,
    -1.9,
    {"$f": "nan"},
    {"$f": "inf"},
    True,
    None,
    [1],
    {"$p": 30},
    {"$i": "-9223372036854775808"},
    {"$i": "9223372036854775807"},
    {"$i": "9223372036854775808"},
    {"$i": "-9223372036854775809"},
    "9" * 4300,
    "9" * 4301,
]


def _missing(f: str) -> list[dict[str, Any]]:
    cols = list(_BASES[f])
    out = [_case(f, drop=(c,)) for c in cols]
    # Neighbouring columns missing together: the KeyError names the one
    # read first, which pins the read order.
    out += [_case(f, drop=(a, b)) for a, b in zip(cols, cols[1:], strict=False)]
    return out


def _odd_columns(f: str, typed: set[str], ints: set[str], keep: set[str]) -> list[Any]:
    out = []
    for c in _BASES[f]:
        if c in keep:
            continue
        values = _TEXT_ODD if c in typed else _INT_ODD if c in ints else _STR_ODD
        out += [_case(f, {c: v}) for v in values]
    return out


_STAMPS: list[Any] = [
    None,
    [],
    "",
    0,
    5,
    1.5,
    "abc",
    {"$d": [["a", 1]]},
    [["n", 1, 2, 3]],
    [["n", 1, 2, 3, 4, 5]],
    [{"$t": ["n", "1", " 2 ", 3.9, True]}],
    [["n", 1, 2, 3, 4], ["m", 5, 6, 7, 8]],
    [[1, 2, 3, 4, 5]],
    [["n", "x", 2, 3, 4]],
    [["n", 1, 2, 3, None]],
    [["n", 1, 2, 3, {"$f": "inf"}]],
    [["n", {"$p": 40}, -1, 0, 1]],
    [[{"$p": 4300}, 1, 2, 3, 4]],
    ["abcde"],
    [{"$d": [["ticks", False]]}],
    [{"$d": [["ticks", 1]]}, {"$d": [["ticks", True]]}],
    [{"$d": [["ticks", None]]}, ["n", 1, 2, 3, 4], {"$d": [["ticks", False]]}],
    [{"$d": [["ticks", True], ["ticks", False]]}],
    [{"$d": [["tick", True]]}, ["ticks"]],
    [{"$t": ["ticks", True]}],
    [None, 1, "x", []],
]
_UNRESOLVED: list[Any] = [
    None,
    5,
    "ab",
    [],
    {"$d": []},
    {"$d": [["sessions", ["s1", "s2"]], ["replaced_by", "K2"]]},
    {"$d": [["sessions", "ab"]]},
    {"$d": [["sessions", 5]]},
    {"$d": [["sessions", [1, None, 2.5]]]},
    {"$d": [["sessions", {"$d": [["a", 1]]}]]},
    {"$d": [["sessions", {"$p": 4300}]]},
    {"$d": [["sessions", [{"$p": 4300}]]]},
    {"$d": [["replaced_by", 5]]},
    {"$d": [["replaced_by", ""]]},
    {"$d": [["sessions", None], ["replaced_by", None]]},
]
_EVIDENCE: list[Any] = [
    None,
    0,
    "",
    [],
    5,
    1.5,
    True,
    "ab",
    {"$d": [["b", 1], ["a", 2]]},
    {"$d": [[1, "x"], [1.0, "y"], [True, "z"]]},
    {"$d": [[{"$t": [1, "a"]}, 1]]},
    [["a", 1], ["b", 2], ["a", 3]],
    [[1, "x"], [True, "y"], [0, "z"], [False, "w"]],
    [["ab"]],
    ["ab", "cd"],
    ["abc"],
    [["a", 1], 5],
    [["a", 1], [1, 2, 3]],
    [[["l"], 1]],
    [[{"$d": [["a", 1]]}, 1]],
    [[{"$t": [1, [2]]}, 1]],
    [[{"$t": [1, 2]}, 1], [{"$t": [1.0, 2]}, 2]],
    [{"$t": ["a", 1]}, {"$d": [["k", 1], ["v", 2]]}],
    [[None, 1], [-0.0, 2], [0, 3]],
    {"$d": [[{"$p": 4300}, 1]]},
    {"$d": [["k", {"$d": [[2, [1, {"$t": [3]}]], ["2", "later"]]}]]},
]
_SIGNATURES: list[Any] = [
    None,
    "abc",
    "",
    [],
    {"$d": [["a", 1], ["b", 2]]},
    {"$t": ["h", None]},
    5,
    1.5,
    True,
    0,
]


def _rows_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [_case(f) for f in _BASES]
    out += _missing("checkout") + _missing("view") + _missing("project")
    out += _missing("proposal")
    out += _odd_columns(
        "checkout",
        {"git_dir", "token", "last_head", "branch"},
        set(),
        {"stamps_json", "unresolved_json", "manifest_json"},
    )
    out += _odd_columns("view", set(), {"seen_order"}, set())
    out += _odd_columns(
        "project", {"merged_into", "provisional", "parent", "tenant"}, set(), set()
    )
    out += _odd_columns(
        "proposal",
        {"checkout", "answer", "answered_by"},
        set(),
        {"evidence_json", "signature_json"},
    )
    out += [_case("checkout", {"stamps_json": v}) for v in _STAMPS]
    for kind in ("primary", "unresolved"):
        out += [
            _case("checkout", {"kind": kind, "unresolved_json": v}) for v in _UNRESOLVED
        ]
    out += [
        _case("checkout", {"manifest_json": v})
        for v in [
            {"$d": [["name", "n"], ["entries", [["p", "7"], ["q", 1.5]]]]},
            {"$d": [["name", "n"], ["entries", [["p"]]]]},
            {"$d": [["name", 5]]},
            {"$d": [["name", "n"], ["entries", 5]]},
            "x",
        ]
    ]
    out.append(_case("checkout", {"common_device": "1", "common_inode": 2}))
    out.append(_case("checkout", {"object_device": "1", "object_inode": None}))
    out.append(
        _case("checkout", {"common_device": 0, "common_inode": ""}, ("common_birth",))
    )
    out.append(_case("checkout", {"object_birth": 2.5}))
    out.append(_case("checkout", {"object_inode": None}, ("object_birth",)))
    out.append(_case("checkout", {"legacy": "", "retired": [0]}))
    views = [
        ["K1", "M1", "/a", "t", "t", 3],
        ["K1", "M2", "/b", "t", "t", -1],
        ["K1", "M3", "/c", "t", "t", 3],
        ["K1", "M4", "/d", "t", "t", 0],
    ]
    out.append(_case("checkout", views=views))
    out.append(_case("checkout", {"kind": "unresolved"}, views=views[::-1]))
    out.append(_case("checkout", {"id": 5}, ("project",), views=views))
    out += [_case("proposal", {"evidence_json": v}) for v in _EVIDENCE]
    out += [_case("proposal", {"signature_json": v}) for v in _SIGNATURES]
    out += [
        _case("proposal", {"candidates_json": v})
        for v in ["ab", {"$d": [["P1", 1]]}, [1, 2.5, None, [1]], 5, None, ""]
    ]
    out += [
        _case("proposal", {"line": v})
        for v in ["tab\there", "nl\nx", {"$c": [0x5C, 0x6E]}, "\x1b[31m", 5]
    ]
    out.append(_case("proposal", {"evidence_json": 5}, ("reason",)))
    out.append(_case("proposal", {"signature_json": 5}, ("opened",)))
    out.append(_case("proposal", {"signature_json": 5, "opened": {"$p": 4300}}))
    out.append(_case("proposal", {"checkout": 5, "opened": {"$p": 4300}}))
    out.append(_case("checkout", {"token": 5, "registered": {"$p": 4300}}))
    out.append(_case("checkout", {"token": 5}, ("stamps_json",)))
    return out


def _jsonable_cases() -> list[dict[str, Any]]:
    values: list[Any] = [
        None,
        5,
        "s",
        [1, {"$t": [2, 3]}],
        {"$t": []},
        {"$d": [[1, "a"], ["1", "b"], [2, "c"]]},
        {"$d": [["1", "a"], [1, "b"]]},
        {"$d": [[{"$t": [1, "a"]}, 1], [None, 2], [True, 3], [1.5, 4]]},
        {"$d": [["k", [{"$d": [[0, {"$t": [None]}]]}]]]},
        {"$d": [[{"$p": 4300}, 1]]},
        {"$n": [50, {"$t": [1]}]},
    ]
    out = []
    for v in values:
        try:
            o: Any = enc(rows._jsonable(dec(v)))
        except Exception as e:  # noqa: BLE001
            o = _raised(e)
        out.append({"f": "jsonable", "a": v, "o": o})
    return out


def _location(spec: dict[str, Any]) -> mr.Location:
    files = tuple(
        mr.ProjectFileRef(i, "n", d, d + "/bettermemory.toml") for i, d in spec["files"]
    )
    return mr.Location(
        machine="M1",
        cwd="/w",
        state="repo",
        root=spec["root"],
        kind=spec["kind"],
        git_dir=dec(spec["git_dir"]),
        head=spec["head"],
        remote_keys=frozenset(spec["remote_keys"]),
        shallow=spec["shallow"],
        token=spec["token"],
        token_written=spec["token_written"],
        token_status=spec["token_status"],
        files=files,
    )


def _location_cases() -> list[dict[str, Any]]:
    base: dict[str, Any] = {
        "root": "/w/r",
        "kind": "primary",
        "git_dir": "/w/r/.git",
        "head": "h1",
        "remote_keys": ["b", "a", "C"],
        "shallow": False,
        "token": "t1",
        "token_written": "t2",
        "token_status": "present",
        "files": [["F1", "/w/r/sub"], ["F2", "/w/r"], ["F3", "/w/r"]],
    }
    changes: list[dict[str, Any]] = [
        {},
        {"kind": "folder"},
        {"kind": "folder", "files": []},
        {"root": None},
        {"root": "/w/r/"},
        {"root": "/W/R"},
        {"files": [["F1", "/w/r/./"]]},
        {"files": []},
        {"kind": None},
        {"token": ""},
        {"token": None},
        {"token": None, "token_written": None},
        {"head": None, "remote_keys": [], "shallow": True},
        {"token_status": "failed:EACCES"},
        {"token_status": "skipped:policy"},
        {"token_status": "failed"},
        {"token_status": "fail"},
        {"token_status": " skipped"},
        {"token_status": "failed", "git_dir": None},
        {"token_status": "failed", "git_dir": {"$c": [67, 58, 92, 114, 92, 46, 103]}},
        {"token_status": "failed", "git_dir": '/w/a b"c'},
        {"token_status": "skipped:x", "git_dir": "C:"},
    ]
    out = []
    for change in changes:
        spec = {**base, **change}
        loc = _location(spec)
        out.append(
            {
                "f": "location",
                "a": change,
                "o": [enc(rows._signature(loc)), enc(rows._unwritten(loc))],
            }
        )
    return out


class _Lcg:
    """A 64-bit linear congruential generator, the same in the Rust test."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def next(self, n: int) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) % 2**64
        return (self.x >> 33) % n


_POOLS: dict[str, list[Any]] = {
    "id": ["K1", 5, 1.5, True, None, ["x"]],
    "project": ["P1", "", 7],
    "machine": ["M1", None],
    "kind": ["primary", "unresolved", 3],
    "object_device": ["1", None, 7],
    "object_inode": ["2", None],
    "object_birth": [None, "3", 2.5],
    "common_device": [None, "1"],
    "common_inode": [None, 4],
    "common_birth": [None, "b"],
    "git_dir": ["/g", None, "", 5],
    "token": ["t", None],
    "token_status": ["written", 0],
    "stamps_json": [
        None,
        [],
        [["n", 1, 2, 3, 4]],
        [["n", "1", 2, 3, 4], {"ticks": False}],
        [{"ticks": True}, ["n", 1, 2, 3]],
        [["n", "x", 2, 3, 4]],
        "ab",
        5,
    ],
    "last_head": ["h", None],
    "branch": ["b", None, 1],
    "remotes_json": [None, [], ["o", 1], "ab", 5],
    "manifest_json": [
        None,
        {"name": "n", "entries": [["p", 1]]},
        5,
        {"name": "n", "entries": 5},
    ],
    "legacy": [0, 1, "", None],
    "retired": [0, 1],
    "registered": ["r", 2],
    "unresolved_json": [
        None,
        {"sessions": ["s"], "replaced_by": "K2"},
        {"sessions": "ab"},
        {"replaced_by": 5},
        5,
    ],
}
_SEED = 20261121
_COUNT = 2000


def _digest_rows() -> list[dict[str, Any]]:
    rng = _Lcg(_SEED)
    out = []
    for _ in range(_COUNT):
        row = {k: pool[rng.next(len(pool))] for k, pool in _POOLS.items()}
        if rng.next(20) == 0:
            del row[list(_POOLS)[rng.next(len(_POOLS))]]
        out.append(row)
    return out


def _digest() -> dict[str, Any]:
    h = hashlib.sha256()
    for row in _digest_rows():
        o = _answer("checkout", row, [])
        h.update(json.dumps(o, sort_keys=True, separators=(",", ":")).encode("ascii"))
        h.update(b"\n")
    pools = [[k, [enc(v) for v in pool]] for k, pool in _POOLS.items()]
    return {
        "f": "digest",
        "seed": _SEED,
        "count": _COUNT,
        "pools": pools,
        "sha256": h.hexdigest(),
    }


def _python_cases() -> list[dict[str, Any]]:
    out = [
        _case("view", {"seen_order": v})
        for v in ["\u0661\u0662", "\u00a07\u3000", "\u0967", "1\u200b", "\uff11\uff10"]
    ]
    out += [
        _case("proposal", {"line": v})
        for v in [
            "caf\u00e9",
            "\u202eevil",
            "\u0378",
            "\U0001f600",
            "a\u0300",
            "\u2028",
        ]
    ]
    out.append(_case("checkout", {"stamps_json": [["n", "\u0661", "\u00a02", 3, 4]]}))
    return out


def generate() -> dict[str, bytes]:
    cases = _rows_cases() + _jsonable_cases() + _location_cases()
    header = {
        "k": "header",
        "bases": {k: enc(v) for k, v in _BASES.items()},
        "full": {k: _full(k) for k in _BASES},
        "ticks_key": rows.TICKS_KEY,
        "token_name": mr._TOKEN_NAME,
        "count": len(cases),
    }
    return {"membership_rows.jsonl": jsonl([header, *cases, _digest()])}


def generate_python() -> dict[str, bytes]:
    cases = _python_cases()
    header = {"k": "python", "count": len(cases)}
    return {"membership_rows_python.jsonl": jsonl([header, *cases])}
