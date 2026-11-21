"""Fixtures: the repeats `re._parser._parse` reads and
`SubPattern.getwidth` (the ``parse.*`` parts of ``pyre.jsonl`` and
``pyre_python.jsonl``, unit pyre-3).

The native build reads them in
``native/crates/bettermemory-core/src/pyre/parse.rs`` (the repeat branch
of `_parse`, through `parse_flat`) and ``tree.rs`` (`getwidth`). The
reference here is the interpreter's own ``re`` package, run with the hooks
of `gen_pyre_atom._hooked`, which refuse a group, an alternative and a
possessive repeat where the port does.

An answer that holds no tree text is the same under every release and is
in ``pyre.jsonl``: the width of the tree as ``=lo,hi`` (decimal), or
``!Name@pos`` for an error of ``re``, ``?Feature@pos`` for a refusal, and
``%Overflow`` for the OverflowError "the repetition number is too large",
and ``%DigitLimit`` for the ValueError of ``int`` on a bound of more than
4,300 digits, neither of which is ``re.error`` or has a position
(`_outcome`). The tree's
``str(tree)`` names the opcodes of ``re._constants``, so it follows the
release and is in ``pyre_python.jsonl``.

Parts, by ``part``:

- ``parse.repeat`` (``pyre.jsonl``): ``pattern`` (a bytes pattern under
  ``bytes``) and its ``answer`` (`_outcome`).
- ``parse.tree`` (``pyre_python.jsonl``): each pattern of ``REPEATS`` that
  parses, and ``dump``, its ``str(tree)``.
- ``parse.width`` (``pyre.jsonl``): a tree built from ``tree`` as
  `gen_pyre_tree._built` builds one, for shapes no flat pattern parses to
  (a branch, a group, nested repeats, the cap at ``MAXWIDTH``), and
  ``width``, what `getwidth` returns, as two decimal texts.
- ``parse.sweep`` (``pyre.jsonl``): ``count`` patterns, each joined from
  up to seven of ``fragments`` by the draws of `gen_pyre_tree.Lcg` from
  ``seed`` + i; ``sha256`` is the digest of their `_outcome` lines. The
  same row in ``pyre_python.jsonl`` holds the digest of their
  `gen_pyre_atom._line` lines, the tree text included.
- ``parse.width_sweep`` (``pyre.jsonl``): ``count`` random trees drawn by
  `gen_pyre_tree._sub` from ``seed`` + i; ``sha256`` is the digest of the
  lines ``lo,hi``.
"""

from __future__ import annotations

import hashlib
import importlib
from typing import Any

from .gen_pyre_atom import _failed, _hooked, _line, _spelled
from .gen_pyre_tree import Lcg, _built, _sub, _tree, spell

P: Any = importlib.import_module("re._parser")
C: Any = importlib.import_module("re._constants")

SEED = 20261121
SWEEPS = 20000
WIDTHS = 4000
B = "\\"
TOP = 4294967294

#: Written patterns: every branch, bound and error of the repeat reader.
REPEATS: list[str | bytes] = [
    "a*",
    "a+",
    "a?",
    "a{2}",
    "a{2,}",
    "a{,3}",
    "a{2,3}",
    "a{,}",
    "a{0}",
    "a{0,0}",
    "a{00002}",
    "a{3,}",
    "a{,0}",
    "a{3,3}",
    # A `{` that is a literal.
    "a{}",
    "{}",
    "x{}}",
    "a{",
    "a{2",
    "a{2,",
    "a{,3",
    "a{x}",
    "a{2x}",
    "a{-1}",
    "a{ 2}",
    "a{2 }",
    "a{2,3,4}",
    "a{" + B + "2}",
    "a{2" + B + "}",
    "a" + B + "{2}",
    "a{١}",
    "a*{",
    "a*{x",
    "a*{}",
    "{",
    "{x",
    # The bounds at MAXREPEAT and past it.
    "a{4294967294}",
    "a{4294967294,4294967294}",
    "a{0004294967294}",
    "a{4294967295}",
    "a{4294967296}",
    "a{99999999999999999999}",
    "a{1,4294967295}",
    "a{,4294967295}",
    "a{4294967295,}",
    "a{4294967295,1}",
    "a{99999999999,5}",
    "a{5,99999999999}",
    # int's digit limit, leading zeros included, ahead of every check of
    # the bound and of what the repeat goes over.
    "a{" + "0" * 4299 + "1}",
    "a{" + "0" * 4300 + "1}",
    "{" + "0" * 4301 + "}",
    "a{" + "9" * 4301 + "}",
    "a{1," + "0" * 4301 + "}",
    "a{" + "9" * 4301 + ",1}",
    "a{3,2}",
    "a{1,0}",
    "a{4294967294,1}",
    "a{3,2",
    # Lazy and possessive.
    "a*?",
    "a+?",
    "a??",
    "a{2}?",
    "a{2,}?",
    "a{,3}?",
    "a*+",
    "a++",
    "a?+",
    "a{2}+",
    "a{2,3}+",
    "a{3,2}+",
    "a{3,2}?",
    "a*?+",
    "a*?" + B,
    "a*+" + B,
    "a*++",
    # Nothing to repeat and multiple repeat.
    "*",
    "+",
    "?",
    "{2}",
    "{2,3}",
    "^*",
    "$*",
    "a^*",
    "a$+",
    B + "A*",
    B + "Z+",
    B + "b?",
    B + "B{2}",
    "*a",
    "a**",
    "a*{2}",
    "a{2}*",
    "a{2}{3}",
    "a??*",
    "a*??",
    "a*?*",
    "^{4294967295}",
    "*{3,2}",
    "a**" + B,
    # What a repeat goes over.
    "ab*",
    "a*b*",
    "[ab]*",
    "[^a]+",
    ".*",
    B + "d*",
    B + "w{2}",
    B + "x41*",
    "[a-z]{1,3}?",
    "\xe9*",
    "\U0001f600+",
    "a" + B + "*",
    B + "**",
    "a*b",
    "a*|b",
    "a*(",
    "a*)",
    "(a)*",
    "a*" + B,
    "a{2}" + B,
    "a{" + B,
    "a{2," + B,
    "a" + B + "1*",
    # Bytes patterns.
    b"a*",
    b"\\xff{2}",
    b"a{4294967295}",
    b"*",
]

#: Trees built by hand, as `gen_pyre_tree._item` describes them.
WIDTH_TREES: list[list[Any]] = [
    [],
    [["lit", 97], ["at", "AT_END"], ["failure"]],
    [["max", 0, None, [["lit", 97]]]],
    [["max", 0, None, []]],
    [["max", 0, None, [["at", "AT_END"]]]],
    [["max", 1, None, [["max", 0, 1, [["lit", 97]]]]]],
    [["max", 0, None, [["lit", 97]]], ["lit", 98]],
    [["lit", 98], ["max", 0, None, [["lit", 97]]]],
    [["lit", 98], ["min", 2, None, [["lit", 97]]], ["lit", 99]],
    [["branch", []]],
    [["branch", []], ["lit", 97]],
    [["branch", [[["lit", 97]], [["lit", 97], ["lit", 98]], []]]],
    [["branch", [[["max", 1, None, [["lit", 97]]]], [["lit", 98]]]], ["lit", 99]],
    [["group", 1, 0, 0, [["lit", 97], ["any"]]]],
    [["group", None, 2, 0, [["max", 2, 3, [["lit", 97]]]]]],
    [["assert", 1, [["lit", 97]]], ["assert_not", -1, [["lit", 97]]]],
    [["in", [["negate"], ["range", 97, 122]]], ["not", 97]],
    [["min", 2, 5, [["lit", 97], ["lit", 98]]]],
    [["max", 3, 1, [["lit", 97]]]],
    [["max", TOP, TOP, [["lit", 97]]]],
    [["max", TOP, TOP, [["max", TOP, TOP, [["lit", 97]]]]]],
    [["max", TOP, TOP, [["max", TOP, TOP, [["max", TOP, TOP, [["lit", 97]]]]]]]],
    [["max", TOP, None, [["max", TOP, None, [["max", TOP, None, [["any"]]]]]]]],
    [["max", 2, 2, [["max", TOP, TOP, [["max", TOP, TOP, [["lit", 97]]]]]]]],
    [["max", 0, TOP, [["max", 0, None, [["lit", 97]]]]]],
    [["max", TOP, TOP, [["max", 0, 0, [["lit", 97]]]]]],
]

#: The fragments the sweep joins.
FRAGMENTS = [
    "a",
    "b",
    "[ab]",
    ".",
    "^",
    "$",
    B + "b",
    B + "d",
    B + "A",
    "*",
    "+",
    "?",
    "{",
    "}",
    ",",
    "0",
    "2",
    "{2}",
    "{,3}",
    "{2,}",
    "{1,2}",
    "{3,2}",
    "{0}",
    "{4294967295}",
    "{4294967294}",
    "*?",
    "+?",
    "??",
    "*+",
    "{2}?",
    "{2}+",
    "(",
    ")",
    "|",
    B,
    B + "{",
    B + "*",
]


def _outcome(pattern: str | bytes) -> str:
    """The answer for ``pattern`` without the tree's text: its width."""
    flags = 0
    try:
        with _hooked():
            tree = P.parse(pattern, flags)
    except Exception as exc:
        return _failed(exc)
    lo, hi = tree.getwidth()
    return f"={lo},{hi}"


def _sweep_text(seed: int) -> str:
    draw = Lcg(seed).draw
    return "".join(FRAGMENTS[draw() % len(FRAGMENTS)] for _ in range(draw() % 8))


def _sweep(answer: Any) -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(SWEEPS):
        digest.update((answer(_sweep_text(SEED + i)) + "\n").encode("ascii"))
    return {
        "part": "parse.sweep",
        "seed": SEED,
        "count": SWEEPS,
        "fragments": [spell(fragment) for fragment in FRAGMENTS],
        "sha256": digest.hexdigest(),
    }


def _widths() -> list[dict[str, Any]]:
    rows = []
    for items in WIDTH_TREES:
        tree = _built(items, P.State())
        assert _tree(tree) == items, items
        lo, hi = tree.getwidth()
        rows.append({"part": "parse.width", "tree": items, "width": [str(lo), str(hi)]})
    return rows


def _width_sweep() -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(WIDTHS):
        lo, hi = _sub(Lcg(SEED + i).draw, P.State(), 0).getwidth()
        digest.update(f"{lo},{hi}\n".encode("ascii"))
    return {
        "part": "parse.width_sweep",
        "seed": SEED,
        "count": WIDTHS,
        "sha256": digest.hexdigest(),
    }


def rows() -> list[dict[str, Any]]:
    repeats = [
        {"part": "parse.repeat", **_spelled(pattern), "answer": _outcome(pattern)}
        for pattern in REPEATS
    ]
    return [*repeats, *_widths(), _sweep(_outcome), _width_sweep()]


def rows_python() -> list[dict[str, Any]]:
    """The tree texts, which follow the release."""
    trees = []
    for pattern in REPEATS:
        line = _line(pattern)
        if line.startswith("["):
            trees.append({"part": "parse.tree", **_spelled(pattern), "dump": line})
    return [*trees, _sweep(_line)]
