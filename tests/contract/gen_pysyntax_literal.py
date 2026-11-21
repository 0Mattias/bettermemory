"""Fixtures of the literal reader (`pysyntax::literal`, native/README.md,
"Python source as CPython reads it").

The reference is ``ast.literal_eval`` of CPython 3.13.13 followed by
``claims._canonical_repr`` (``claims.py`` 204-231). The values of mixed
real and complex arithmetic are the release's (3.14 changed them), so a
text that holds an imaginary part and a ``+`` or ``-`` (`_release_bound`)
goes into ``pysyntax_literal_python.jsonl``, gated on the Python release:
under any other interpreter its generator declines and writes the header
alone, naming the interpreter. Every other text goes into
``pysyntax_literal.jsonl``, which 3.11 to 3.14 answer alike and which is
not gated.

The first line of each file is the header. A case row
holds ``t``, the text, spelled as `gen_pysyntax` spells texts (a JSON
string, a list of code points, or ``{"r": [[piece, n], ...]}``), and
``v``: the canonical text spelled as `gen_pysyntax._digest` spells a
string (its code points' SHA-256 past 80), or null when
``literal_eval`` or ``_canonical_repr`` raises.

A sweep row pins 20,000 texts of one to eight pieces drawn by a fixed
LCG (`sweep_cases`) by count and the SHA-256 of their answers, one line
each (the canonical text, or ``E``), joined by newlines: the sweep
``pieces`` of the gated file draws from `_PIECES`, the sweep ``real`` of
the ungated file from `_PIECES` without ``1j``.
"""

from __future__ import annotations

import ast
import hashlib
import sys
import warnings
from typing import Any

from bettermemory.claims import _canonical_repr

from ._common import jsonl
from .gen_pysyntax import _built, _digest, _spelled

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"
_INT_DIGITS = 4300


def _answer(text: str) -> str | None:
    try:
        return _canonical_repr(ast.literal_eval(text))
    except Exception:
        return None


def _row(t: Any) -> dict[str, Any]:
    v = _answer(_built(t))
    return {
        "t": _spelled(t) if isinstance(t, str) else t,
        "v": None if v is None else _digest(v),
    }


_CONSTANTS = [
    "",
    " ",
    "1",
    "0",
    "-0",
    "+5",
    "007",
    "0x_ff",
    "1.5",
    "-0.0",
    "+0.0",
    "0.1",
    "1e16",
    "1e22",
    "1e-7",
    "-1e-320",
    "123456789012345678.0",
    "1e309",
    "-1e309",
    "1j",
    "-1j",
    "-0j",
    "+0j",
    "0j",
    "1.5j",
    "1e16j",
    "1e309j",
    "-1e309j",
    "True",
    "False",
    "None",
    "...",
    "Ellipsis",
    "__debug__",
    "x",
    "-True",
    "+False",
    "-None",
    "--1",
    "-+1",
    "- 1",
    "-(1)",
    "-((1))",
    "-(-1)",
    "+(1.5)",
    "-(1j)",
    "-(0j)",
    "+(-0.0)",
    "-'a'",
    "~1",
    "not 1",
    " \t1",
    "1\n",
    "1 # c",
    "\n1",
    "1;2",
    "1 2",
    "await x",
    "*a",
    "lambda: 1",
    "1 if 1 else 2",
    "1 < 2",
    "1 and 2",
    "a := 1",
    "(a := 1)",
    "print(1)",
    "1 .real",
    "1.real",
    "'a'.upper()",
    "'a'[0]",
    "[1][0]",
    "(1)(2)",
    "1**2",
    "-1**2",
    "2*3",
    "1/2",
    "1//2",
    "1%2",
    "1@2",
    "1<<2",
    "1|2",
    "(yield)",
    "x[0]",
    "f'a'",
    "f'{1}'",
    "'a' f'b'",
    "rb'\\x'",
]

_STRINGS = [
    "'a'",
    "'a' 'b'",
    "'a' \"b\"",
    "'a' b'b'",
    "b'a' 'b'",
    "b'a' b'b'",
    "u'x' 'y'",
    "('a'\n 'b')",
    "'a' ('b')",
    "r'\\d'",
    "'\\x00\\x1f\\x7f\\x80\\xa0'",
    "'é\\u2028'",
    "'\\ud800'",
    "'\\udc00\\ud800'",
    "'\\U0001f600'",
    "'a\\'b\"'",
    '"it\'s"',
    "'\\\\'",
    "'\\t\\n\\r\\x0b\\x0c'",
    "'\\u0378'",
    "'\\U000e0001'",
    "'\\xad'",
    "b''",
    "b'\\''",
    "b'\"'",
    "b'\\'\"'",
    "b'\\t\\n\\r\\x00\\x7f\\x80\\xff\\\\'",
    "b'\\x0b\\x0c\\x1f '",
    'b"a\'b"',
    "b'~'",
    "rb'\\n'",
    "b'\\777'",
]

_COMPLEX = [
    "1+2j",
    "1-2j",
    "-1+2j",
    "-1-2j",
    "+1+2j",
    "1.5+0j",
    "0+0j",
    "0-0j",
    "-0+0j",
    "-0.0+0j",
    "-0.0-0j",
    "0.0-0j",
    "-0.0+1j",
    "0-1e309j",
    "1+-2j",
    "1-+2j",
    "1+(2j)",
    "(1)+2j",
    "(-1)+2j",
    "-(1)+2j",
    "-(1+2j)",
    "+(1+2j)",
    "(1+2j)",
    "((1-2j))",
    "1+2j+3j",
    "1+2j-3",
    "1j+1j",
    "1j+1",
    "1j-1j",
    "1+2",
    "1-1",
    "1.5-0.5",
    "True+1j",
    "None+1j",
    "1+True",
    "1+1j*2",
    "2*1+1j",
    "1e309+1j",
    "-1e309-1e309j",
    "-1e309+0j",
    "1e309-1e309j",
    "0+1e309j",
    "1+1e16j",
    "1e16+1j",
    "1.5+2.5j",
    "1e-320+1j",
    "'a'+'b'",
    "[1]+[2]",
    "1 + 2j",
    "1 +\\\n2j",
    "(1 +\n2j)",
    "1+(2j)+3",
    "-(-1+2j)",
    "1+2J",
    "0b1+0o7j",
]

_SET_CALLS = [
    "set()",
    "set( )",
    "set (\n)",
    "(set)()",
    "((set))()",
    "set",
    "(set)",
    "set(())",
    "set([])",
    "set(x=1)",
    "set(*())",
    "set()()",
    "set()[0]",
    "set().x",
    "-set()",
    "{set()}",
    "[set()]",
    "(set(),)",
    "{1: set()}",
    "{set(): 1}",
    "set.x",
    "set(,)",
    "frozenset()",
    "dict()",
    "list()",
    "tuple()",
]

_SEQUENCES = [
    "()",
    "(1,)",
    "(1)",
    "1,",
    "1,2",
    "1,2,",
    "(1,2,)",
    "(,)",
    "(1,,)",
    ",",
    "1,,",
    "((1,),)",
    "(((1)))",
    "((),)",
    "( )",
    "(\n1\n,\n)",
    "[]",
    "[1,]",
    "[,]",
    "[1 2]",
    "[1,2][0]",
    "[*a]",
    "[x for x in y]",
    "(x for x in y)",
    "[[], [[]], ()]",
    "[1, 'a', b'a', None, ..., True, 1.0, 1j]",
    "(1, [2, (3,)], {4})",
    "[1,\n2]",
    "1, 2 if 3 else 4",
    "[1:2]",
    "(1:2)",
]

_SETS = [
    "{1}",
    "{1,}",
    "{1,2,1}",
    "{1, 1.0, True}",
    "{True, 1, 1.0}",
    "{1.0, 1}",
    "{0, 0.0, -0.0, 0j, False}",
    "{-0.0, 0}",
    "{False, 0}",
    "{1, 1+0j}",
    "{1+0j, 1}",
    "{1j, -1j}",
    "{-0.0+1j, 0.0+1j}",
    "{(1,2),(1,2)}",
    "{(1,[2])}",
    "{[1]}",
    "{{1}}",
    "{{}}",
    "{'a', b'a'}",
    "{1, '1'}",
    "{2, 1, 10, 'b', 'a'}",
    "{None, ...}",
    "{(), (1,), (1, 2)}",
    "{(1,), (1.0,), (True,)}",
    "{(1, 'a'), (1.0, 'a')}",
    "{1e400, 1e309}",
    "{-1e309, 1e309}",
    "{1, 1.0000000000000002}",
    "{0.5, 1/2}",
    "{'B', 'a', 'é', '\\U0001f600', 'z', ''}",
    "{-1, 1, 10, 2, -10}",
    "{b'b', b'a'}",
    "{1, *a}",
    "{1 for x in y}",
    "{,}",
    "{1,,}",
    "{1 2}",
    "{9007199254740993, 9007199254740992.0}",
    "{9007199254740992, 9007199254740992.0}",
    "{9007199254740992.0, 9007199254740992}",
    "{1000000000000000000000000000000, 1e30}",
    "{1000000000000000019884624838656, 1e30}",
    "{1e30, 1000000000000000019884624838656}",
    "{-1000000000000000019884624838656, -1e30}",
    "{18446744073709551616, 1.8446744073709552e19}",
    "{0x10000000000000000, 18446744073709551616.0}",
    "{2.5, 2, 3}",
    "{-1j, 0.0-1j}",
    "{9223372036854775807, 9.5e18}",
    "{9223372036854775807, 9.223372036854775e18}",
    "{9223372036854775808, 9.223372036854775807e18}",
    "{999999999999999999, 1e18}",
    "{1000000000000000000, 1e18}",
    "{1e-320, 0}",
]

_DICTS = [
    "{}",
    "{1: 2}",
    "{1: 'a', 1.0: 'b'}",
    "{True: 1, 1: 2}",
    "{True: 'a', 1: 'b', 1.0: 'c'}",
    "{1:2,}",
    "{1:2, 3}",
    "{1, 2: 3}",
    "{**a}",
    "{1: 2, **a}",
    "{[1]: 2}",
    "{(1,): [2]}",
    "{1: [2]}",
    "{'b': 1, 'a': 2, 1: 0}",
    "{1: 'x', '1': 'y'}",
    "{1: 1, 1: 1, 1: 3}",
    "{0.0: 'a', -0.0: 'b', 0: 'c'}",
    "{1: {2: 3}}",
    "{{}: 1}",
    "{(): ()}",
    "{1:}",
    "{:1}",
    "{1: 2 for x in y}",
    "{1::2}",
    "{1: 2, 3: 4,,}",
    "{'a': 1, 'a': 2}",
    "{0.0-1j: 1, -1j: 2}",
    "{1: 'b', 2: 'a'}",
    "{2: 'a', 10: 'b'}",
    "{(1, 2): 0, (1,): 0}",
    "{1: set(), 2: {}}",
    "{1j: 1, 1: 2}",
    "{0j: 'z', False: 'f'}",
    "{1: x}",
]

_BOUNDARY_INT = (1 << 1024) - (1 << 970)


def _big() -> list[Any]:
    top = 10**_INT_DIGITS
    hex_ok = hex(top - 1)
    hex_over = hex(top)
    return [
        {"r": [["9", _INT_DIGITS]]},
        {"r": [["-", 1], ["9", _INT_DIGITS]]},
        {"r": [["9", _INT_DIGITS + 1]]},
        hex_ok,
        hex_over,
        "-" + hex_ok,
        "-" + hex_over,
        "[" + hex_over + "]",
        "{" + hex_over + ": 1}",
        "{1: " + hex_over + "}",
        "(" + hex_ok + ",)",
        "1" + "0" * 308 + "+1j",
        "1" + "0" * 309 + "+1j",
        str(_BOUNDARY_INT) + "+0j",
        str(_BOUNDARY_INT - 1) + "+0j",
        "-" + str(_BOUNDARY_INT - 1) + "-0j",
        "9007199254740993+0j",
        "{" + str(int(1e300)) + ", 1e300}",
        "{1e300, " + str(int(1e300) + 1) + "}",
        "{" + str(int(1.7976931348623157e308)) + ", 1.7976931348623157e308}",
        "{" + str(1 << 1023) + ", 0x1p1023}",
        "{" + str(1 << 1023) + ", 8.98846567431158e307}",
    ]


def _deep() -> list[Any]:
    out: list[Any] = []
    for n in (50, 199, 200, 201):
        out.append({"r": [["[", n], ["]", n]]})
        out.append({"r": [["(", n], ["1", 1], [")", n]]})
        out.append({"r": [["(", n], ["1,", 1], [")", n]]})
        out.append({"r": [["{", n], ["1", 1], ["}", n]]})
        out.append({"r": [["{1: ", n], ["1", 1], ["}", n]]})
        out.append({"r": [["[(", n // 2], ["", 1], [")]", n // 2]]})
        out.append({"r": [["-(", n], ["1", 1], [")", n]]})
    out.append({"r": [["((", 99], ["1", 1], [",)", 99], [")", 99]]})
    out.append({"r": [["-", 3000], ["1", 1]]})
    out.append({"r": [["1+", 3000], ["1j", 1]]})
    out.append({"r": [["[", 1], ["1, ", 2000], ["]", 1]]})
    out.append({"r": [["{", 1], ["'k', ", 1000], ["}", 1]]})
    out.append({"r": [["'ab' ", 500]]})
    return out


#: The pieces the sweep draws from.
_PIECES = [
    "1",
    "0",
    "-",
    "+",
    "1.0",
    "-0.0",
    "1j",
    "True",
    "None",
    "'a'",
    "b'a'",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    ",",
    ":",
    "set",
    "()",
    " ",
    "...",
    "2",
    "0.5",
]


#: The pieces of the ungated sweep: no imaginary part.
_REAL_PIECES = [p for p in _PIECES if p != "1j"]


def _release_bound(text: str) -> bool:
    """Whether the text may hold mixed real and complex arithmetic."""
    return ("j" in text or "J" in text) and ("+" in text or "-" in text)


def sweep_cases(count: int, pieces: list[str] = _PIECES) -> list[str]:
    state = 0x2545F4914F6CDD1D
    out = []
    for _ in range(count):
        drawn = []
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        n = 1 + (state >> 33) % 8
        for _ in range(n):
            state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
            drawn.append(pieces[(state >> 33) % len(pieces)])
        out.append("".join(drawn))
    return out


def _sweep(name: str, pieces: list[str]) -> dict[str, Any]:
    cases = sweep_cases(20000, pieces)
    answers = []
    for text in cases:
        v = _answer(text)
        answers.append("E" if v is None else v)
    digest = hashlib.sha256(
        "\n".join(answers).encode("utf-8", "surrogatepass")
    ).hexdigest()
    return {"sweep": name, "count": len(cases), "sha256": digest}


def _texts() -> list[Any]:
    texts = _CONSTANTS + _STRINGS + _COMPLEX + _SET_CALLS + _SEQUENCES + _SETS
    return [*texts, *_DICTS, *_big(), *_deep()]


def _rows(gated: bool) -> list[dict[str, Any]]:
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(_INT_DIGITS)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rows = [_row(t) for t in _texts() if _release_bound(_built(t)) == gated]
            if gated:
                rows.append(_sweep("pieces", _PIECES))
            else:
                rows.append(_sweep("real", _REAL_PIECES))
    finally:
        sys.set_int_max_str_digits(limit)
    return rows


def generate_stable() -> dict[str, bytes]:
    """``pysyntax_literal.jsonl``: the texts no release changes."""
    rows = _rows(gated=False)
    header = {"fixture": "pysyntax_literal", "rows": len(rows)}
    return {"pysyntax_literal.jsonl": jsonl([header, *rows])}


def generate() -> dict[str, bytes]:
    """``pysyntax_literal_python.jsonl``: mixed real and complex texts."""
    name = "pysyntax_literal_python.jsonl"
    v = sys.version_info
    running = f"{v.major}.{v.minor}.{v.micro}"
    header: dict[str, Any] = {
        "fixture": "pysyntax_literal_python",
        "python": REPRODUCES,
    }
    if running != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = running
        return {name: jsonl([header])}
    rows = _rows(gated=True)
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}
