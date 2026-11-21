"""Fixtures: the history half of `location_witness`
(``location_witness.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/location_witness.rs``. This pins
`NeedWitnesses.joined`, `AnsweredHistory`, `AnsweredGit`,
`answered_history`, `_independent_answers` (through `AnsweredGit`),
`independent_answer` and the exchange keeping (`_NONCE`, `_Kept`,
`_EXCHANGES`, `_drop`, `ExchangeReads`, `carried`).

Every input the reference reads is JSON the client posts, so each row
holds JSON texts (``json.dumps``, ASCII) that both sides decode, and its
answer as one JSON text (``r``):

- ``k: "h"``: a reader built over ``a`` (``c``: ``"h"`` `AnsweredHistory`,
  ``"g"`` `AnsweredGit` with bound ``b`` and no exchange, ``"ah"``
  `answered_history`), then the calls of ``o`` (``[name, argument]``).
  ``r`` is ``{"err": class}`` or ``{"cls": class, "ops": [...]}``, each
  call ``{"ok": answer}`` or ``{"need": question}``; a set is sorted, a
  `Reach` is ``{"rel": [[commit, [from...]]...], "b": bounded}``.
- ``k: "j"``: `NeedWitnesses.joined` of the questions ``n``.
- ``k: "ia"``: `independent_answer` of ``q`` and ``a`` with ``h`` the
  history's answer (absent: no history); ``r`` holds the answer and the
  sets the history was asked.
- ``k: "x"``: steps over one empty `_EXCHANGES`, the clock and the nonce
  the step's (``now``, ``fresh`` as hex): ``new`` (an `ExchangeReads`),
  ``read`` (a read recorded), ``ind`` (its history's `independent`),
  ``carry`` and ``fill`` (``n`` exchanges made, read and carried, the
  i-th's nonce ``i`` as 16 big-endian bytes). Each step answers, and the
  kept exchanges after it, in full up to four, else their count, first
  two and last nonces.

Rows whose ids are floats read them through ``str`` of a float, whose
shortest-repr text is the same from Python 3.1 on and on every platform.
"""

from __future__ import annotations

import json
import types
from collections import OrderedDict
from typing import Any
from unittest import mock

from bettermemory import location_witness as lw
from bettermemory.location_git import Reach

from ._common import jsonl

H = [f"{i:x}" * 40 for i in range(1, 8)]
H64 = "e" * 64


def _t(v: Any) -> str:
    return json.dumps(v)


def _enc(v: Any) -> Any:
    if isinstance(v, Reach):
        return {"rel": [[c, sorted(v[c])] for c in sorted(v)], "b": v.bounded}
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    if isinstance(v, tuple):
        return [_enc(x) for x in v]
    return v


def _call(fn: Any, *args: Any) -> dict[str, Any]:
    try:
        return {"ok": _enc(fn(*args))}
    except lw.NeedWitnesses as e:
        return {"need": e.question}


def _history_row(c: str, a: Any, ops: list[Any], b: int = 256) -> dict[str, Any]:
    answers = json.loads(_t(a))
    row: dict[str, Any] = {"k": "h", "c": c, "a": _t(a), "o": _t(ops)}
    if b != 256:
        row["b"] = b
    try:
        with mock.patch.object(lw, "INDEPENDENT_MAX", b):
            if c == "h":
                r: Any = lw.AnsweredHistory(answers)
            elif c == "g":
                r = lw.AnsweredGit(answers)
            else:
                r = lw.answered_history(answers)
            out = []
            for name, arg in ops:
                with mock.patch.object(lw, "INDEPENDENT_MAX", b):
                    if name == "reach":
                        out.append(_call(r.reachable_among, arg))
                    else:
                        out.append(_call(getattr(r, name), arg))
    except Exception as e:  # noqa: BLE001 - the class is the answer
        row["r"] = _t({"err": type(e).__name__})
        return row
    row["r"] = _t({"cls": type(r).__name__, "ops": out})
    return row


def _history_cases() -> list[tuple[Any, ...]]:
    h1, h2, h3, h4, h5, h6, h7 = H
    no = ("holds", ["a"]), ("fingerprint", "X"), ("parents", "x"), ("blobs", "Q")
    asked_ops = [
        ("holds", ["abc", "DEF"]),
        ("holds", ["abc", "x", "Y"]),
        ("holds_objects", ["ABC"]),
        ("holds", []),
    ]
    walk = {
        "AA": ["fp", ["P1", "p2"]],
        "bb": [None, None],
        "cc": ["x"],
        "dd": "ab",
        "ee": [1, "pq"],
        "ff": [["l"], {"K": 1}],
        "GG": ["a", "b", "c"],
        "hh": ["FP", []],
        "ii": [True, [1, None]],
    }
    walk_ops = [
        (f, k)
        for k in ["aa", "BB", "cc", "dd", "ee", "FF", "gg", "hh", "ii"]
        for f in ("fingerprint", "parents")
    ]
    anc = {
        "ancestry_of": [h1, h2.upper()],
        "ancestry": [[h1, h2], [h2.upper(), h1], [h1, h3], [h2, h2]],
    }
    anc_ops = [
        ("reach", [h1, h2]),
        ("reach", [h2, h1.upper(), "zz", h1]),
        ("reach", [h1, h3]),
        ("reach", []),
        ("reach", ["NOTHEX"]),
        ("reach", [h1 + "0"]),
        ("reach", [h1]),
        ("reach", [H64]),
    ]
    return [
        ("empty", "h", {}, [*no, ("reach", [h1, h2]), ("reach", []), ("holds", [])]),
        ("empty lists", "h", {"asked": [], "held": []}, list(no)),
        (
            "mixed case ids",
            "h",
            {"asked": ["ABC", "def"], "held": ["Abc", "x"]},
            asked_ops,
        ),
        (
            "non-string ids",
            "h",
            {
                "asked": [1, True, None, {"a": 1}, [1, "B"], False, 0, 10**30],
                "held": [1, True, [1, "B"]],
            },
            [
                (
                    "holds",
                    [
                        "1",
                        "true",
                        "none",
                        "{'a': 1}",
                        "[1, 'b']",
                        "false",
                        "0",
                        str(10**30),
                    ],
                ),
                ("holds", ["True"]),
            ],
        ),
        (
            "asked a string: its characters",
            "h",
            {"asked": "aBc", "held": "b"},
            [("holds", ["a", "B", "c"]), ("holds", ["ab"])],
        ),
        (
            "asked a dict: its keys",
            "h",
            {"asked": {"X": 1, "y": 2}, "held": {"x": 0}},
            [("holds", ["x", "Y"])],
        ),
        ("asked an int", "h", {"asked": 5}, []),
        ("asked zero", "h", {"asked": 0, "held": 0}, [("holds", ["a"])]),
        ("asked true", "h", {"asked": True}, []),
        ("asked false", "h", {"asked": False}, [("holds", [])]),
        ("asked empty string", "h", {"asked": "", "held": None}, [("holds", ["a"])]),
        ("held an int", "h", {"asked": ["a"], "held": 3}, []),
        (
            "nested ids",
            "h",
            {"asked": [[[[1]]], {"k": [{"x": None}]}], "held": []},
            [("holds", ["[[[1]]]", "{'k': [{'x': none}]}"])],
        ),
        (
            "unicode lowering",
            "h",
            {"asked": ["ÄBC", "İ", "ΣΑΣ", "K"], "held": ["äbc"]},
            [("holds", ["äbc", "i̇", "σας", "k"]), ("holds", ["Äbc"])],
        ),
        (
            "lone surrogate id",
            "h",
            {"asked": ["\ud800A"], "held": ["\ud800a"]},
            [("holds", ["\ud800a"]), ("holds", ["\udfff"])],
        ),
        ("walk entries well and ill formed", "h", {"walk": walk}, walk_ops),
        ("walk a list", "h", {"walk": [["a", ["b"]]]}, [("parents", "a")]),
        ("walk parents an int", "h", {"walk": {"a": ["f", 3]}}, []),
        ("walk parents zero", "h", {"walk": {"a": ["f", 0]}}, [("parents", "A")]),
        (
            "walk keys equal once lowered",
            "h",
            {"walk": {"Ab": ["one", ["x"]], "aB": ["two", ["y"]]}},
            [("fingerprint", "AB"), ("parents", "ab")],
        ),
        ("ancestry asked and not", "h", anc, anc_ops),
        (
            "ancestry bounded false",
            "h",
            {**anc, "ancestry_bounded": False},
            [("reach", [h1, h2])],
        ),
        (
            "ancestry bounded zero",
            "h",
            {**anc, "ancestry_bounded": 0},
            [("reach", [h1, h2])],
        ),
        (
            "ancestry bounded null",
            "h",
            {**anc, "ancestry_bounded": None},
            [("reach", [h1])],
        ),
        (
            "ancestry bounded the string",
            "h",
            {**anc, "ancestry_bounded": "false"},
            [("reach", [h1])],
        ),
        (
            "ancestry 64 digits",
            "h",
            {"ancestry_of": [H64.upper(), h1], "ancestry": [[H64, h1]]},
            [("reach", [H64, h1]), ("reach", [h1])],
        ),
        (
            "ancestry item a two-character string",
            "h",
            {"ancestry_of": ["a"], "ancestry": ["Ab", {"x": 1, "Y": 2}]},
            [("reach", [h1])],
        ),
        ("ancestry item of one", "h", {"ancestry": [["a"]]}, []),
        ("ancestry item of three", "h", {"ancestry": [["a", "b", "c"]]}, []),
        ("ancestry item an int", "h", {"ancestry": [5]}, []),
        ("ancestry an int", "h", {"ancestry": 7}, []),
        (
            "ancestry of a string",
            "h",
            {"ancestry_of": h1, "ancestry": []},
            [("reach", [h1, h2])],
        ),
        (
            "blobs",
            "h",
            {
                "blobs": {
                    "C1": ["X", "y"],
                    "c2": [],
                    "c3": None,
                    "c4": "ab",
                    "c5": [1, None],
                    "C1 ": ["z"],
                }
            },
            [("blobs", c) for c in ["c1", "C2", "c3", "c4", "c5", "c6", "c1 "]],
        ),
        ("blobs ids an int", "h", {"blobs": {"a": 7}}, []),
        ("blobs a list", "h", {"blobs": [["a", ["b"]]]}, [("blobs", "a")]),
        (
            "error order: asked before walk",
            "h",
            {"walk": {"a": ["f", 3]}, "asked": 1},
            [],
        ),
        (
            "git: independent answers",
            "g",
            {
                "independent": [
                    {"of": [h2, h1], "top": [h1]},
                    {"of": [h1, h3], "top": []},
                    {"of": [h3, h4], "top": [h5]},
                    {"of": [h4, h5], "top": None},
                    {"of": [h5, h6], "top": "x"},
                    "junk",
                    {"of": "str"},
                    {"top": [h1]},
                    {"of": [h6.upper(), h7], "top": [h7.upper(), h7]},
                    {"of": [h1, h4], "top": [h1, h4, h1]},
                ]
            },
            [
                ("independent", x)
                for x in [
                    [h1, h2],
                    [h1, h3],
                    [h3, h4],
                    [h2, h1.upper()],
                    [h1],
                    [],
                    [h1, "zz"],
                    [h4, h5],
                    [h5, h6],
                    [h6, h7],
                    [h1, h4],
                    [h2, h3],
                    [h1, h1.upper()],
                ]
            ],
        ),
        (
            "git: a later entry replaces",
            "g",
            {
                "independent": [
                    {"of": [h1, h2], "top": [h1]},
                    {"of": [h2, h1, h2], "top": [h2]},
                    {"of": [h3, h4], "top": [h3]},
                    {"of": [h3, h4], "top": None},
                ]
            },
            [("independent", [h1, h2]), ("independent", [h3, h4])],
        ),
        (
            "git: non-string of",
            "g",
            {
                "independent": [
                    {"of": [1, 2], "top": [1]},
                    {"of": [True], "top": [True]},
                ]
            },
            [("independent", ["1", "2"])],
        ),
        (
            "git: independent not a list",
            "g",
            {"independent": {"of": [h1, h2]}},
            [("independent", [h1, h2])],
        ),
        (
            "git: of holding an unprintable depth",
            "g",
            {"independent": [{"of": [[[[h1]]]], "top": [[[[h1]]]]}]},
            [("independent", [h1])],
        ),
        (
            "git: bound 2",
            "g",
            {"independent": [{"of": [h1, h2, h3], "top": [h1]}]},
            [("independent", [h1, h2, h3]), ("independent", [h1, h2])],
            2,
        ),
        (
            "git: bound 3",
            "g",
            {"independent": [{"of": [h1, h2, h3], "top": [h1]}]},
            [("independent", [h1, h2, h3]), ("independent", [h1, h2, h3, h4])],
            3,
        ),
        ("git: walk errors first", "g", {"asked": 3, "independent": 3}, []),
        (
            "git: history readers",
            "g",
            {"asked": ["A"], "held": ["a"], "blobs": {"b": []}},
            [("holds", ["a"]), ("blobs", "B")],
        ),
        (
            "answered: git",
            "ah",
            {"independent_ok": True, "exchange": None},
            [("independent", [h1, h2])],
        ),
        ("answered: no exchange", "ah", {"independent_ok": True}, [("holds", [])]),
        (
            "answered: ok one",
            "ah",
            {"independent_ok": 1, "exchange": "x"},
            [("holds", [])],
        ),
        ("answered: ok string", "ah", {"independent_ok": "true", "exchange": "x"}, []),
        (
            "answered: error",
            "ah",
            {"independent_ok": True, "exchange": None, "held": 1},
            [],
        ),
    ]


def _joined_cases() -> list[tuple[str, list[Any]]]:
    return [
        ("none", []),
        ("one", [{"held": ["b", "a", "b"]}]),
        (
            "lists merged in order",
            [
                {"held": ["a", "b"]},
                {"held": ["c", "b"]},
                {"walk": True, "commits": ["x"]},
                {"walk": False, "commits": ["y", "x"]},
            ],
        ),
        (
            "flags first named",
            [
                {"remint": False},
                {"remint": True, "replacing": None},
                {"replacing": ["l"]},
            ],
        ),
        ("flag then list", [{"k": True}, {"k": ["a"]}]),
        ("list then flag", [{"k": ["a"]}, {"k": True}, {"k": ["b"]}]),
        ("empty list then items", [{"k": []}, {"k": ["a"]}]),
        (
            "views by path",
            [
                {"views": [{"path": "/a", "x": 1}, {"path": "/b"}]},
                {"views": [{"path": "/a", "x": 2}, {"name": "n"}, {"name": "n"}]},
            ],
        ),
        (
            "dicts by equality",
            [
                {"v": [{"a": 1}]},
                {
                    "v": [
                        {"a": True},
                        {"a": 1.0},
                        {"b": 1},
                        {"b": 1, "a": 2},
                        {"a": 2, "b": 1},
                        {"a": [1]},
                        {"a": [True]},
                    ]
                },
            ],
        ),
        (
            "items by repr",
            [
                {"v": [1, "1", True, 1.0, None, "None", [1, 2]]},
                {"v": [[1, 2], (1, 2), 1, "a'b", "a'b"]},
            ],
        ),
        (
            "paths by repr",
            [
                {"v": [{"path": 1}, {"path": "1"}, {"path": [1]}, {"path": True}]},
                {"v": [{"path": 1, "x": 9}, {"path": 1.0}]},
            ],
        ),
        (
            "a dict without path equal to one with",
            [
                {"v": [{"path": "a"}]},
                {"v": [{"path": "a"}, {"q": 1}]},
                {"v": [{"q": 1}]},
            ],
        ),
        (
            "keys in first order",
            [{"b": [1]}, {"a": [2], "b": [3]}, {"c": "s", "a": [2]}],
        ),
        ("a string is no list", [{"k": "ab"}, {"k": "cd"}]),
    ]


def _ia_cases() -> list[tuple[Any, ...]]:
    h1, h2, h3, h4 = H[:4]
    kept = [
        {"of": [h1, h2], "top": [h1]},
        {"of": [h3, h4], "top": None},
        "s",
        5,
        {"top": [h1]},
        {"of": [h2, h1], "top": [h2]},
    ]
    return [
        ("nothing asked", {}, {}, None),
        ("asked, no history", {"independent": [h2.upper(), h1]}, {}, None),
        ("asked, git answers", {"independent": [h2, h1, h2]}, {}, [h2, h1]),
        ("asked, git does not", {"independent": [h2, h1]}, {}, "none"),
        ("asked, git names none", {"independent": [h2, h1]}, {}, []),
        ("kept and replaced", {"independent": [h1, h2]}, {"independent": kept}, [h1]),
        ("kept, other asked", {"independent": [h3, h1]}, {"independent": kept}, None),
        ("kept, nothing asked", {}, {"independent": kept}, [h1]),
        ("asked empty", {"independent": []}, {"independent": kept[:1]}, [h1]),
        ("asked a string", {"independent": "ab"}, {}, [h1]),
        ("asked non-strings", {"independent": [1, "A", None]}, {}, None),
        ("kept a string", {}, {"independent": "abc"}, None),
        ("kept a dict", {}, {"independent": {"of": [h1]}}, None),
        ("kept an int", {}, {"independent": 5}, None),
        ("kept zero", {}, {"independent": 0}, None),
    ]


def _ia_row(note: str, q: Any, a: Any, h: Any) -> dict[str, Any]:
    calls: list[Any] = []

    class _H:
        def independent(self, of: Any) -> Any:
            calls.append(list(of))
            return None if h == "none" else frozenset(h)

    row: dict[str, Any] = {"k": "ia", "note": note, "q": _t(q), "a": _t(a)}
    if h is not None:
        row["h"] = h
    try:
        stub: Any = None if h is None else _H()
        got = lw.independent_answer(json.loads(_t(q)), json.loads(_t(a)), stub)
        row["r"] = _t({"ok": got, "calls": calls})
    except Exception as e:  # noqa: BLE001
        row["r"] = _t({"err": type(e).__name__, "calls": calls})
    return row


def _kept_state() -> Any:
    rows = [
        [
            n,
            list(k.owner),
            k.until,
            [[f, v] for f, v in k.reads.items()],
            sorted(list(x) for x in k.asked),
        ]
        for n, k in lw._EXCHANGES.items()
    ]
    if len(rows) <= 4:
        return rows
    names = list(lw._EXCHANGES)
    return {"n": len(rows), "first": names[:2], "last": names[-1]}


def _exchange_row(note: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    clock = {"now": 0.0}
    fresh = {"hex": "00" * 16}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["now"])
    fake_secrets = types.SimpleNamespace(token_hex=lambda n: fresh["hex"])
    out: list[Any] = []
    cur: Any = None
    with (
        mock.patch.object(lw, "_EXCHANGES", OrderedDict()),
        mock.patch.object(lw, "time", fake_time),
        mock.patch.object(lw, "secrets", fake_secrets),
    ):
        for st in steps:
            clock["now"] = st.get("now", clock["now"])
            fresh["hex"] = st.get("fresh", fresh["hex"])
            do = st["do"]
            if do == "new":
                try:
                    cur = lw.ExchangeReads(
                        json.loads(st["a"]), session=st.get("s"), machine=st["m"]
                    )
                    ans: Any = {
                        "nonce": cur.nonce,
                        "silent": cur.silent,
                        "reads": [[f, v] for f, v in cur.items()],
                        "asked": sorted(list(x) for x in cur.asked),
                        "hist": type(cur.history).__name__,
                    }
                except Exception as e:  # noqa: BLE001
                    cur = None
                    ans = {"err": type(e).__name__}
            elif do == "read":
                cur[st["f"]] = st["v"]
                ans = None
            elif do == "ind":
                if isinstance(cur.history, lw.AnsweredGit):
                    ans = _call(cur.history.independent, st["c"])
                else:
                    ans = "plain"
            elif do == "carry":
                ans = cur.carried().get("exchange")
            else:
                for i in range(st["n"]):
                    fresh["hex"] = i.to_bytes(16, "big").hex()
                    filled = lw.ExchangeReads(
                        {"exchange": None}, session="s", machine="m"
                    )
                    filled["f"] = None
                    filled.carried()
                ans = None
            out.append({"a": ans, "kept": _kept_state()})
    return {"k": "x", "note": note, "s": steps, "r": _t(out)}


def _new(
    a: Any, now: float, fresh: str, s: Any = "s1", m: str = "m1"
) -> dict[str, Any]:
    return {"do": "new", "a": _t(a), "now": now, "fresh": fresh * 16, "s": s, "m": m}


def _exchange_cases() -> list[tuple[str, list[dict[str, Any]]]]:
    h1, h2, h3, h4 = H[:4]
    n1, n2 = "11" * 16, "22" * 16
    git = {"exchange": None, "independent_ok": True}
    return [
        (
            "kept, taken up, kept again, expired at exactly its time",
            [
                _new(git, 100.0, "11"),
                {"do": "read", "f": "/f1", "v": "/F1"},
                {"do": "read", "f": "/f2", "v": None},
                {"do": "read", "f": "/f1", "v": "/G"},
                {"do": "carry", "now": 100.0},
                _new({**git, "exchange": n1}, 107.875, "22"),
                {"do": "carry"},
                _new({**git, "exchange": n1}, 107.875, "33", s="s2"),
                _new({**git, "exchange": n1}, 115.875, "44"),
            ],
        ),
        (
            "taken up just before its time",
            [
                _new(git, 1.0, "11"),
                {"do": "read", "f": "a", "v": "b"},
                {"do": "carry"},
                _new({"exchange": n1}, 8.999, "22"),
            ],
        ),
        (
            "owner mismatch by machine and by a null session",
            [
                _new(git, 5.0, "11", s=None),
                {"do": "read", "f": "a", "v": None},
                {"do": "carry"},
                _new({"exchange": n1}, 5.0, "22", s=""),
                _new({"exchange": n1}, 5.0, "33", s=None, m="m2"),
                _new({"exchange": n1}, 5.0, "44", s=None),
            ],
        ),
        (
            "a silent client drops its owner's exchanges",
            [
                _new(git, 10.0, "11"),
                {"do": "read", "f": "a", "v": "b"},
                {"do": "carry"},
                _new(git, 10.0, "22", s="s2"),
                {"do": "read", "f": "a", "v": "b"},
                {"do": "carry"},
                _new({"remint_ok": True}, 11.0, "33"),
                {"do": "read", "f": "c", "v": "d"},
                {"do": "carry"},
            ],
        ),
        (
            "empty answers are not silent and carry nothing unread",
            [
                _new({}, 1.0, "11"),
                {"do": "carry"},
                {"do": "read", "f": "x", "v": None},
                {"do": "carry"},
            ],
        ),
        (
            "other exchanges expire on a new round",
            [
                _new(git, 100.0, "11"),
                {"do": "read", "f": "a", "v": "b"},
                {"do": "carry"},
                _new(git, 104.0, "22", s="s9"),
                {"do": "read", "f": "a", "v": "b"},
                {"do": "carry"},
                _new(git, 108.0, "33", s="s8"),
                {"do": "carry", "now": 112.0},
            ],
        ),
        (
            "one tie asked an exchange, carried",
            [
                _new(git, 50.0, "11"),
                {"do": "ind", "c": [h1, h2]},
                {"do": "ind", "c": [h3, h4]},
                {"do": "ind", "c": [h1]},
                {"do": "carry"},
                _new(
                    {
                        **git,
                        "exchange": n1,
                        "independent": [{"of": [h1, h2], "top": [h2]}],
                    },
                    51.0,
                    "22",
                ),
                {"do": "ind", "c": [h1, h2]},
                {"do": "ind", "c": [h3, h4]},
                {"do": "carry"},
                _new({"exchange": n1}, 52.0, "33"),
                {"do": "ind", "c": [h1, h2]},
                {"do": "carry"},
            ],
        ),
        (
            "nonce forms",
            [
                _new(git, 1.0, "11"),
                {"do": "read", "f": "a", "v": "b"},
                {"do": "carry"},
                _new({"exchange": n1.upper()}, 1.0, "22"),
                _new({"exchange": n1[:31]}, 1.0, "33"),
                _new({"exchange": n1 + "\n"}, 1.0, "44"),
                _new({"exchange": 11}, 1.0, "55"),
                _new({"exchange": n2}, 1.0, "66"),
                _new({"exchange": n1}, 1.0, "77"),
            ],
        ),
        (
            "an error after the take-up loses the exchange",
            [
                _new(git, 1.0, "11"),
                {"do": "read", "f": "a", "v": "b"},
                {"do": "carry"},
                _new({"exchange": n1, "asked": 5}, 2.0, "22"),
                _new({"exchange": n1}, 2.0, "33"),
            ],
        ),
        (
            "the cap drops the oldest",
            [
                {"do": "fill", "n": 257, "now": 3.0},
                _new({"exchange": "00" * 15 + "05"}, 3.0, "aa", s="s", m="m"),
                {"do": "carry"},
                _new({"exchange": "00" * 15 + "00"}, 3.0, "bb", s="s", m="m"),
            ],
        ),
        (
            "carried twice moves to the end",
            [
                {"do": "fill", "n": 3, "now": 3.0},
                _new({"exchange": "00" * 16}, 3.0, "aa", s="s", m="m"),
                {"do": "carry"},
                {"do": "carry", "now": 4.0},
            ],
        ),
    ]


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    floats = {
        "asked": [1.5, 1e16, 1e-07, -0.0, 1e300, 2.0, 123456789.123],
        "held": [1e16, -0.0],
    }
    rows.append(
        {
            "note": "float ids",
            **_history_row(
                "h",
                floats,
                [
                    (
                        "holds",
                        [
                            "1.5",
                            "1e+16",
                            "1e-07",
                            "-0.0",
                            "1e+300",
                            "2.0",
                            "123456789.123",
                        ],
                    )
                ],
            ),
        }
    )
    rows.append(
        {
            "note": "float of",
            **_history_row(
                "g",
                {"independent": [{"of": [0.5, 1e22], "top": [1e22]}]},
                [("independent", ["0.5", "1e+22"])],
            ),
        }
    )
    rows.append(
        {
            "note": "float walk and blobs",
            **_history_row(
                "h",
                {"walk": {"a": [1.5, [2.5, 1e-5]]}, "blobs": {"b": [3.25]}},
                [("parents", "a"), ("fingerprint", "a"), ("blobs", "b")],
            ),
        }
    )
    rows.append(
        {
            "k": "ia",
            **_ia_row("float asked", {"independent": [1.5, 1e100]}, {}, None),
        }
    )
    for note, c, a, ops, *b in _history_cases():
        rows.append({"note": note, **_history_row(c, a, ops, *b)})
    for note, needs in _joined_cases():
        row: dict[str, Any] = {"k": "j", "note": note, "n": _t(needs)}
        try:
            got = lw.NeedWitnesses.joined(
                [lw.NeedWitnesses(q) for q in json.loads(_t(needs))]
            )
            row["r"] = _t({"ok": got.question})
        except Exception as e:  # noqa: BLE001
            row["r"] = _t({"err": type(e).__name__})
        rows.append(row)
    for note, q, a, h in _ia_cases():
        rows.append(_ia_row(note, q, a, h))
    for note, steps in _exchange_cases():
        rows.append(_exchange_row(note, steps))
    return rows


def generate() -> dict[str, bytes]:
    rows = _rows()
    return {
        "location_witness.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])
    }
