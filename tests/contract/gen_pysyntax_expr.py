"""Fixtures of the expression grammar (`pysyntax::module_expr`,
native/README.md, "Python source as CPython reads it").

The reference is ``ast.parse(source)`` of CPython 3.13.13, as
``claims.check_claim`` (``claims.py`` 672) calls it, over modules of
expression statements. Both files are gated on the Python release: the
grammar, its actions and its limits (the parser's ``MAXSTACK``, the C
recursion limit ``ast2obj`` holds a tree to) are 3.13.13's; under any
other interpreter each generator declines and writes the header alone,
naming the interpreter.

``pysyntax_expr_posix.jsonl`` holds the rows whose answer follows
``Py_C_RECURSION_LIMIT``, the depth ``ast2obj`` converts: every depth row
the parser reads (accepted on a POSIX host, where the limit is 10,000;
RecursionError on Windows, where it is 3,000 on x64 and 1,000 on arm64)
and the rows past ``ast2obj``'s depth. It is POSIX only, and its
generator declines on a host whose limit is not 10,000.
``pysyntax_expr.jsonl`` holds every other row, which every host answers
alike: the parser's stack is ``MAXSTACK`` everywhere.

A case row holds ``t``, the source, spelled as `gen_pysyntax` spells
texts (``{"r": [[piece, n], ...]}`` for a long one); ``parse``, ``ok`` or
the class ``ast.parse`` raises; and for an accepted source ``facts``, the
names ``symbols._all_names`` reads from expressions (sorted Store-context
Names, ``arg`` names, Attribute names, keyword names), and ``shape``, the
tree in preorder by class with each node's children in field order
(`_shape`; ``{"n", "h"}``, its length and SHA-256, past 300 characters).
The shape holds every class ``ast.walk`` counts, operators and contexts
included.

The port declines a logical line that is a statement other than an
expression (`_statement_like`, the port's ``statement_line``): ``want`` is
``D`` on such a row, where every line before it parses. A row with
``band`` true lies where the port's estimate of the parser's depth, or of
the C stack ``ast.parse``'s caller already holds, does not decide: the
port answers as the reference or declines.

The depth rows find, by bisection over the interpreter, the deepest
nesting of each construct the parser reads and the first it refuses
(MemoryError, "Parser stack overflowed"), for chains the port counts
exactly and for brackets drawn by a fixed LCG, which it bounds (there
the boundary pair is ``band`` and the sources 150 calls either side are
not); chains the first pass refuses near the stack (`_REFUSED`), where
the parser's second pass, which reads the source again with its error
rules, overflows sooner than the first; and chains about as deep as
``ast2obj`` converts before RecursionError, at depths whose answer the
caller's own C depth does not change.

Two sweep rows pin answers by count and SHA-256, one line each (``E``
refused, ``D`` declined, else the facts and shape joined by ``|``): the
sources of ``pysyntax_fstring.jsonl``'s sweep, and 20,000 expressions
drawn by a fixed LCG from templates (`sweep_cases`), each in parentheses,
a third of them with one character inserted or deleted.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import platform
import sys
import tokenize
import warnings
from collections.abc import Callable
from typing import Any

from ._common import jsonl
from .gen_pysyntax import _built, _spelled
from .gen_pysyntax_fstring import _c_tokens, _texts, _Unjudged
from .gen_pysyntax_fstring import sweep_cases as fstring_sweep_cases
from .gen_pysyntax_module import _compact

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"

_STATEMENT = {
    "assert", "async", "break", "class", "continue", "def", "del", "elif", "else",
    "except", "finally", "for", "from", "global", "if", "import", "nonlocal", "pass",
    "raise", "return", "try", "while", "with", "yield",
}  # fmt: skip
_MARKERS = {
    "=", ";", "+=", "-=", "*=", "/=", "//=", "%=", "@=", "&=", "|=", "^=", ">>=",
    "<<=", "**=",
}  # fmt: skip
_OPS = set(tokenize.EXACT_TOKEN_TYPES.values())


def _parse(text: str) -> tuple[str, ast.Module | None]:
    try:
        tree = ast.parse(text)
    except Exception as e:
        return type(e).__name__, None
    return "ok", tree


def _shape(tree: ast.AST) -> str:
    out: list[str] = []
    stack: list[Any] = [tree]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            out.append(x)
            continue
        out.append(type(x).__name__)
        kids = list(ast.iter_child_nodes(x))
        if kids:
            stack.append(")")
            for i, k in enumerate(reversed(kids)):
                if i:
                    stack.append(",")
                stack.append(k)
            stack.append("(")
    return "".join(out)


def _facts(tree: ast.AST) -> list[list[str]]:
    store: set[str] = set()
    args: set[str] = set()
    attrs: set[str] = set()
    kws: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            store.add(n.id)
        elif isinstance(n, ast.arg):
            args.add(n.arg)
        elif isinstance(n, ast.Attribute):
            attrs.add(n.attr)
        elif isinstance(n, ast.keyword) and n.arg is not None:
            kws.add(n.arg)
    return [sorted(store), sorted(args), sorted(attrs), sorted(kws)]


def _statement_like(toks: list[tuple[str, str]]) -> bool:
    """The port's ``statement_line`` over one logical line's tokens."""
    if toks[0] == ("OP", "@") or (toks[0][0] == "NAME" and toks[0][1] in _STATEMENT):
        return True
    depth = lambdas = 0
    for typ, s in toks:
        if typ == "OP" and s in ("(", "[", "{"):
            depth += 1
        elif typ == "OP" and s in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and typ == "NAME" and s == "lambda":
            lambdas += 1
        elif depth == 0 and typ == "OP" and s == ":":
            if lambdas == 0:
                return True
            lambdas -= 1
        elif (
            depth == 0 and typ == "OP" and s in _MARKERS and (lambdas == 0 or s != "=")
        ):
            return True
    return False


def _declined(translated: str) -> bool | None:
    """True when the port declines a statement line before any line it
    refuses, False when it refuses first, None when no line is one."""
    try:
        toks = _c_tokens(translated)
    except _Unjudged:
        return None
    if toks is None:
        return None
    lines = translated.split("\n")
    cur: list[tuple[str, str]] = []
    row = 0
    for t in toks:
        name = "OP" if t.type in _OPS else tokenize.tok_name[t.type]
        if name in ("NEWLINE", "ENDMARKER"):
            if cur and _statement_like(cur):
                return _parse("\n".join(lines[: row - 1]) + "\n")[0] == "ok"
            cur = []
        elif name in ("INDENT", "DEDENT"):
            if not cur:
                return None
        else:
            if not cur:
                row = t.start[0]
            cur.append((name, t.string))
    return None


def _answer(text: str) -> tuple[str, str, Any]:
    """``(want, parse, tree)``: what the port answers (``ok``, ``E`` or
    ``D``), the class ``ast.parse`` raises or ``ok``, and its tree."""
    parse, tree = _parse(text)
    translated = text.replace("\r\n", "\n").replace("\r", "\n")
    d = _declined(translated)
    if d is not None:
        return ("D" if d else "E"), parse, None
    return ("ok" if tree is not None else "E"), parse, tree


def _line(text: str) -> str:
    want, _, tree = _answer(text)
    if tree is None:
        return want
    return "|".join([*(",".join(f) for f in _facts(tree)), _shape(tree)])


def _row(text: str, spelled: Any, band: bool = False) -> dict[str, Any]:
    want, parse, tree = _answer(text)
    row: dict[str, Any] = {"t": spelled, "parse": parse}
    if band:
        row["band"] = True
    if want == "D":
        row["want"] = "D"
    if tree is not None:
        shape = _shape(tree)
        row["facts"] = _facts(tree)
        row["shape"] = (
            shape
            if len(shape) <= 300
            else {"n": len(shape), "h": hashlib.sha256(shape.encode()).hexdigest()}
        )
    return row


def _text_row(text: str, band: bool = False) -> dict[str, Any]:
    return _row(text, _spelled(text) if len(text) <= 400 else _compact(text), band)


_FORMS = [
    "",
    "x",
    "x\n",
    "x\ny\n",
    "x  # c\n",
    "(x,\n y)\n",
    "x \\\n + y\n",
    "  x\n",
    "x\n  y\n",
    "1",
    "1.5",
    "1j",
    "0x1f",
    "'s'",
    "b'b'",
    "'a' 'b'",
    "u'a' 'b'",
    "b'a' b'b'",
    "b'a' 'b'",
    "'a' b'b'",
    "b'a' f'{x}'",
    "'a' f'{x}' b'c'",
    "f'{x}' b''",
    "rb'a' Rb'b'",
    "...",
    "....__class__",
    "True",
    "False",
    "None",
    "True.x",
    "None()",
    "x.y",
    "x.y.z",
    "x.True",
    "x.match",
    "x .y",
    "x.\ny",
    "f()",
    "f(x)",
    "f(x,)",
    "f(,)",
    "f(x,,)",
    "f(x)(y)",
    "f(x)[y]",
    "x[y]",
    "x[y, z]",
    "x[y,]",
    "x[]",
    "x[:]",
    "x[::]",
    "x[1:2]",
    "x[1:2:3]",
    "x[1:2:3:4]",
    "x[:, :]",
    "x[1:2, 3]",
    "x[*a]",
    "x[*a, b]",
    "x[a:b, *c]",
    "x[*a:b]",
    "x[a:=1]",
    "x[a:b:=1]",
    "x[a, b:=1]",
    "x[(a:=1):2]",
    "x[a:=1:2]",
    "x[...]",
    "x[lambda: 1]",
    "x[a:lambda: b]",
    "x[*a or b]",
    "f(a=1)",
    "f(a=1, a=2)",
    "f(a, b=1)",
    "f(a=1, b)",
    "f(*a)",
    "f(**a)",
    "f(*a, b)",
    "f(a, *b, c)",
    "f(a=1, *b)",
    "f(a=1, *b, c)",
    "f(**a, b=1)",
    "f(**a, *b)",
    "f(**a, b)",
    "f(**a, **b)",
    "f(a, *b, **c, d=1)",
    "f(*a or b)",
    "f(a:=1)",
    "f(a=b:=1)",
    "f(a, b:=1)",
    "f(True=1)",
    "f(x.y=1)",
    "f((a)=1)",
    "f(a==1)",
    "f(lambda: x)",
    "f(*)",
    "f(**)",
    "f(a=)",
    "f(x for x in y)",
    "f(x for x in y, 1)",
    "f(x for x in y,)",
    "f(1, x for x in y)",
    "f((x for x in y), 1)",
    "f(*x for x in y)",
    "f(a=1 for x in y)",
    "f(x for x in y)(z)",
    "f(x := 1 for y in z)",
    "-x",
    "+x",
    "~x",
    "not x",
    "not not x",
    "--x",
    "-not x",
    "not -x",
    "- await x",
    "await x",
    "await -x",
    "await await x",
    "await x ** 2",
    "await x.y()",
    "x ** y ** z",
    "-x ** -y",
    "x ** -y",
    "x ** not y",
    "x + y * z",
    "x * y + z",
    "(x + y) * z",
    "x - y - z",
    "x - (y - z)",
    "x | y ^ z & w << v >> u + t - s * r / q // p % o @ n",
    "x // y",
    "x @ y",
    "x < y",
    "x < y < z",
    "x < y == z > w",
    "x not in y",
    "x is not y",
    "x is y",
    "x in y",
    "x not y",
    "x is not not y",
    "x in not y",
    "x < not y",
    "x <> y",
    "x != y",
    "not x == y",
    "not x in y",
    "x and y",
    "x and y and z",
    "x or y and z",
    "x and y or z",
    "x and (y and z)",
    "x and not y",
    "x or not y",
    "x if y else z",
    "x if y else z if w else v",
    "(x if y else z) if w else v",
    "x if y if z else w else v",
    "x if y",
    "x if lambda: y else z",
    "x if y else lambda: z",
    "x if (y := 1) else z",
    "lambda: x",
    "lambda: x if y else z",
    "lambda x: x",
    "lambda x, y: x",
    "lambda x=1: x",
    "lambda x, y=1: x",
    "lambda x=1, y: x",
    "lambda *a: a",
    "lambda **k: k",
    "lambda *a, **k: 0",
    "lambda *, a: a",
    "lambda *: 0",
    "lambda *,: 0",
    "lambda a, /: 0",
    "lambda /: 0",
    "lambda a, /, b: 0",
    "lambda a=1, /, b: 0",
    "lambda a=1, /, b=2: 0",
    "lambda *a=1: 0",
    "lambda **k=1: 0",
    "lambda **k, a: 0",
    "lambda **k,: 0",
    "lambda a, /, b, *, c: 0",
    "lambda a, *, /: 0",
    "lambda a, /, /: 0",
    "lambda *a, *b: 0",
    "lambda *a, **b, : 0",
    "lambda a, *b, c=1, d: 0",
    "lambda a=1, *b, c, d=2, **e: (a, b)",
    "lambda a, a: 0",
    "lambda ,: 0",
    "lambda a,: 0",
    "lambda a b: 0",
    "lambda (a): 0",
    "lambda a=: 0",
    "lambda *, **k: 0",
    "lambda: (yield)",
    "lambda: yield",
    "lambda x=yield: 0",
    "lambda: x := 1",
    "lambda: lambda: x",
    "lambda a=lambda b=1: b: a",
    "(x)",
    "(x,)",
    "(x, y)",
    "()",
    "(,)",
    "(*x)",
    "(*x,)",
    "(*x, y)",
    "((x))",
    "(x := 1)",
    "(x.y := 1)",
    "((x) := 1)",
    "(x := y := 1)",
    "(x := (y := 1))",
    "(x := 1, y)",
    "x := 1",
    "(x := 1 if y else z)",
    "(lambda: 1)",
    "(yield)",
    "(yield x)",
    "(yield x, y)",
    "(yield *x, y)",
    "(yield *x)",
    "(yield from x)",
    "(yield from x, y)",
    "(yield from)",
    "(yield, 1)",
    "(yield yield)",
    "(yield x for x in y)",
    "((yield), 1)",
    "[yield]",
    "f(yield)",
    "(yield).x",
    "(yield) + 1",
    "[x]",
    "[]",
    "[x, y,]",
    "[*x, *y, z]",
    "[*x or y]",
    "[x := 1]",
    "[,]",
    "{}",
    "{x}",
    "{x, y}",
    "{*x, y}",
    "{x: y}",
    "{x: y, z: w}",
    "{**x}",
    "{**x, y: z}",
    "{y: z, **x}",
    "{x, y: z}",
    "{x: y, z}",
    "{**x or y}",
    "{x := 1}",
    "{x: y := 1}",
    "{x := 1: y}",
    "{*x: y}",
    "{x: *y}",
    "{lambda: 1}",
    "{lambda: 1: 2}",
    "{x: lambda: y}",
    "{**x, y}",
    "{x:}",
    "{:x}",
    "{,}",
    "{x,}",
    "{**}",
    "[x for x in y]",
    "[x for x in y if z]",
    "[x for x in y if z if w]",
    "[x for x in y for z in w]",
    "[x for x in y if z for w in v if u]",
    "[x async for x in y]",
    "[x for x in y if await z]",
    "[x async x in y]",
    "[x for x, in y]",
    "[x for x, y in z]",
    "[x for *x, y in z]",
    "[x for *x in y]",
    "[x for (x) in y]",
    "[x for (x, y) in z]",
    "[x for [x, y] in z]",
    "[x for () in y]",
    "[x for [] in y]",
    "[x for x.y in z]",
    "[x for x[0] in z]",
    "[x for f() in z]",
    "[x for 1 .real in z]",
    "[x for (x.y) in z]",
    "[x for x + y in z]",
    "[x for -x in y]",
    "[x for x in *y]",
    "[x for x in y, z]",
    "[x for x in lambda: y]",
    "[x for x in y if lambda: z]",
    "[x for x in y or z]",
    "[x for x in y if z else w]",
    "[x for x in y if z or w]",
    "[x := 1 for y in z]",
    "[x for y in (z := w)]",
    "[x for y in z := w]",
    "[x for y in z if w := v]",
    "[*x for x in y]",
    "[x, y for x in z]",
    "[x for x in y,]",
    "[x for x in]",
    "[x for in y]",
    "[x for]",
    "[x if y for z in w]",
    "(x for x in y)",
    "(x for x in y if z)",
    "(x, y for x in z)",
    "(*x for x in y)",
    "{x for x in y}",
    "{x: y for x in z}",
    "{**x for x in y}",
    "{x: y for x, y in z}",
    "{x, y for x in z}",
    "{x: y, z: w for x in v}",
    "f'a'",
    "f''",
    "f'{x}'",
    "f'{x!r}'",
    "f'{x!s:>3}'",
    "f'{x!a}'",
    "f'{x!z}'",
    "f'{x!A}'",
    "f'{x!rr}'",
    "f'{x! r}'",
    "f'{x !r}'",
    "f'{x!r }'",
    "f'{x!}'",
    "f'{x!r!s}'",
    "f'{x!r=}'",
    "f'{x=!r}'",
    "f'{x=}'",
    "f'{x = }'",
    "f'{x=:>3}'",
    "f'{x=!s:{y}}'",
    "f'{x:}'",
    "f'{x=:}'",
    "f'{x!r:}'",
    "f'{x:{y}}'",
    "f'{x:a{y}b}'",
    "f'{x:{y=}}'",
    "f'{x:{y!r:{z}}}'",
    "f'{x:{y}!r}'",
    "f'{x!r:{y}=}'",
    "f'{x:{{}}}'",
    "f'{x:{y}{{z}}}'",
    "f'{x:=1}'",
    "f'{(x:=1)}'",
    "f'{x,}'",
    "f'{x, }'",
    "f'{x,,}'",
    "f'{*x,,}'",
    "f'{x, ,}'",
    "f'{x,, y}'",
    "f'{x:{y,,}}'",
    "f'{x, y}'",
    "f'{*x}'",
    "f'{*x,}'",
    "f'{*x, y}'",
    "f'{yield}'",
    "f'{yield x}'",
    "f'{yield from x}'",
    "f'{lambda: 1}'",
    "f'{(lambda: 1)}'",
    "f'{x for x in y}'",
    "f'{(x for x in y)}'",
    "f'{}'",
    "f'{ }'",
    "f'{=}'",
    "f'{!r}'",
    "f'{x}' 'a' f'b{y}'",
    "'a' f'{x}'",
    "f'' 'a'",
    "f'' ''",
    "f'a{{b}}c'",
    "f'{x}{{'",
    "f'{{'",
    "f'\\\n'",
    "f'{x}\\\n{y}'",
    "f'{x:\\x41}'",
    "rf'{x:\\x41}'",
    "f'{x:{y}\\x41}'",
    "f'{x=}' f'{y=}'",
    "f'a{x=}'",
    "f'{x}' f'{y}'",
    "f'{f'{x}'}'",
    "f'{f'{f'{x}'}'}'",
    "f'{x!ｒ}'",
    "f'{x!Ｎｏｎｅ}'",
    "f'{x!if}'",
    "f'{x:{y:{z}}}'",
    "ｘ",
    "ｉｆ",
    "ｉｆ x",
    "Ｔｒｕｅ",
    "ＴＲＵＥ",
    "Ｎｏｎｅ.a",
    "x.ｉｆ",
    "x.Ｔｒｕｅ",
    "f(Ｔｒｕｅ=1)",
    "lambda Ｔｒｕｅ: 0",
    "(Ｎｏｎｅ := 1)",
    "[x for Ｆａｌｓｅ in y]",
    "f'{x!Ｔｒｕｅ}'",
    "(__debug__ := 1)",
    "x y",
    "x,",
    "x, y",
    "x, y,",
    "*x",
    "*x,",
    "*x, *y",
    "**x",
    "*x or y,",
    "*x | y,",
    "*not x,",
    "*-x,",
    "*x if y else z,",
    "1 if 2 else *x",
    "x, *y",
    "*x, y",
    "(*x, y)",
    "x $ y",
    "x ? y",
    "x ` y",
    "x;",
    "match(x)",
    "type(x)",
    "case(x)",
    "match x",
    "_",
    "print >> f, x",
    "x\n\ny\n",
    "x\n#c\ny\n",
    "'\\N{EM DASH}'",
    "x $\n'\\N{EM DASH}'",
    "fR'''\\\n'''",
    "Rf'\\\n{x}'",
    "RF'\\\n'",
    "FR'{x:\\\n}'",
]

#: Statement lines the port declines (``want`` is ``D`` where the lines
#: before parse) and near misses it reads.
_STATEMENTS = [
    "x = 1",
    "x: int",
    "x: int = 1",
    "x += 1",
    "x **= 2",
    "x; y",
    "x;",
    "del x",
    "pass",
    "yield x",
    "yield",
    "if x: pass",
    "if x:\n    y\n",
    "for x in y: pass",
    "@d\ndef f(): pass",
    "import x",
    "from x import y",
    "async def f(): pass",
    "match x:\n    case 1: pass\n",
    "type X = int",
    "x = lambda a=1: a",
    "lambda a=1: a",
    "lambda a, b=lambda c=1: c: b",
    "x\ny = 1\n",
    "x +\ny = 1\n",
    "x = 1\nx +\n",
    "(x) = 1",
    "x[y] = z",
    "f(x) = 1",
    "x + 1 = 2",
    "x := 1\ny = 2\n",
    "print(x) # c\nreturn\n",
    "x if y else z = w",
    "f'{x:y}' = 1",
    "{x: y}",
    "x[a:b]",
    "lambda: x, y = 1",
    "lambda: (x := 1)",
]

#: Chains the port counts exactly: (head, repeated piece, tail).
_CHAINS = [
    ("", "-", "x\n"), ("", "+", "x\n"), ("", "~", "x\n"), ("", "not ", "x\n"),
    ("", "lambda: ", "x\n"), ("", "x if y else ", "x\n"), ("x", "**x", "\n"),
    ("*", "-", "x,\n"), ("x, ", "-", "x\n"), ("x < ", "-", "x\n"), ("x or ", "-", "x\n"),
    ("", "-", "1\n"), ("", "-", "'s'\n"), ("", "-", "f'a'\n"), ("lambda: ", "-", "x\n"),
    ("x if y else ", "-", "x\n"), ("not ", "-", "x\n"), ("x\n", "-", "x\n"),
    ("x and ", "not ", "x\n"), ("", "-x ** ", "x\n"),
]  # fmt: skip

#: Bracket wrappers the port bounds: drawn in stacks around a `-` chain.
_WRAPPERS = [
    ("(", ")"), ("[", "]"), ("{", "}"), ("f(", ")"), ("f(a, ", ")"), ("f(a=", ")"),
    ("f(*", ")"), ("x[", "]"), ("x[a, ", "]"), ("x[:", "]"), ("(a, ", ")"), ("[a, ", "]"),
    ("{a: ", "}"), ("{**", "}"), ("[", " for a in b]"), ("[a for a in ", "]"),
    ("[a for a in b if ", "]"), ("(a := ", ")"), ("(yield ", ")"), ("f'{", "}'"),
    ('f"{a:{', '}}"'), ("lambda a=", ": b"), ("-(", ")"), ("not (", ")"),
    ("a if b else (", ")"), ("lambda: (", ")"), ("[a for x[", "] in b]"), ("x.y(", ")"),
    ("(", ").z"), ("await (", ")"),
]  # fmt: skip


def _bound(
    make: Callable[[int], str], lo: int, hi: int, cls: str, first: str = "ok"
) -> int | None:
    """The largest ``n`` in ``[lo, hi)`` whose source does not raise
    ``cls``, when ``make(lo)`` answers ``first`` and ``make(hi)`` raises
    ``cls``. A deep source the parser reads may raise RecursionError in
    ``ast2obj`` on a host whose C recursion limit is lower, so the search
    asks only whether ``cls`` is raised: its answer is every host's."""
    if _parse(make(lo))[0] != first or _parse(make(hi))[0] != cls:
        return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _parse(make(mid))[0] != cls:
            lo = mid
        else:
            hi = mid
    return lo if _parse(make(lo + 1))[0] == cls else None


def c_recursion_limit() -> int:
    """``Py_C_RECURSION_LIMIT`` of this host's CPython 3.13 build
    (``Include/cpython/pystate.h``): 1,000 on Windows arm64, 3,000 on
    other Windows, 10,000 on the POSIX hosts CI runs."""
    if sys.platform == "win32":
        return 1000 if platform.machine().lower() in ("arm64", "aarch64") else 3000
    return 10_000


#: Chains the first pass refuses before its leaf or at it: (head, piece,
#: tail, the piece's chain in `_CHAINS`, whether the port may decline
#: at the boundary, the count below the boundary answered exactly). The
#: first pass raises SyntaxError (or ValueError, which ends the parse)
#: and the second pass, deeper by its error rules and reading past the
#: first pass's failure, overflows from the bisected count; the first
#: pass itself overflows where the chain's leaf would. In the last two the
#: error rules nest per piece on a flat line: `invalid_legacy_expression`
#: reads the rest of the line after each element's NAME, and
#: `invalid_arithmetic` the inversion after each ``+ not``.
_REFUSED = [
    ("", "-", "\n", 0, True, 60), ("", "-", "x $\n", 0, True, 60),
    ("", "-", "x x\n", 0, True, 60), ("", "-", "1 if\n", 0, True, 60),
    ("", "-", "x\n  y\n", 0, True, 60), ("", "-", "x <> y\n", 0, True, 60),
    ("", "-", "\uff34rue\n", 0, False, 60), ("", "-", "b'' 'a'\n", 12, True, 60),
    ("", "not ", "\n", 3, True, 60), ("", "x if y else ", "\n", 5, True, 60),
    ("", "lambda: ", "\n", 4, True, 2400), ("x", "**x", "**\n", 6, True, 30),
    ("(a.b := ", "-", "1)\n", None, True, 100), ("f(a=1, ", "-", "b c)\n", None, True, 100),
    ("[*", "lambda: ", "1]\n", None, True, 2400),
    ("", "x if y else ", "lambda \uff34rue: 1\n", 5, False, 60),
    ("", "x + x, ", "x $\n", None, True, 60), ("x", " + not x", "\n", None, True, 60),
]  # fmt: skip


def _runs(*pieces: tuple[str, int]) -> Any:
    return {"r": [[_spelled(p), n] for p, n in pieces if n]}


@functools.cache
def _depth_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The depth rows every host answers alike (the parser's refusals and
    the tokenizer's bracket limit) and those whose answer follows the C
    recursion limit (a deep source the parser reads, accepted where the
    limit is 10,000 and RecursionError where it is lower)."""
    rows: list[dict[str, Any]] = []
    posix: list[dict[str, Any]] = []

    def add(row: dict[str, Any]) -> None:
        deep = row["parse"] in ("ok", "RecursionError")
        (posix if deep else rows).append(row)

    edges = []
    for head, piece, tail in _CHAINS:

        def make(n: int, head: str = head, piece: str = piece, tail: str = tail) -> str:
            return head + piece * n + tail

        n = _bound(make, 0, 7000, "MemoryError")
        assert n is not None, (head, piece, tail)
        edges.append(n)
        for k in (n, n + 1):
            add(_row(make(k), _runs((head, 1), (piece, k), (tail, 1))))
    for head, piece, tail, chain, band, below in _REFUSED:

        def refused(
            n: int, head: str = head, piece: str = piece, tail: str = tail
        ) -> str:
            return head + piece * n + tail

        n = _bound(refused, 0, 7000, "MemoryError", _parse(refused(0))[0])
        assert n is not None, (head, piece, tail)
        counts = {n - below: False, n: band, n + 1: band}
        if chain is not None:
            # Where the first pass meets the stack, as the chain's leaf:
            # answered exactly.
            counts[edges[chain] + 1] = False
        for k, b in counts.items():
            add(_row(refused(k), _runs((head, 1), (piece, k), (tail, 1)), b))
    state = 0x5851_F42D_4C95_7F2D
    mask = (1 << 64) - 1

    def draw() -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        return state >> 33

    stacks = 0
    while stacks < 30:
        stack = [_WRAPPERS[draw() % len(_WRAPPERS)] for _ in range(1 + draw() % 4)]
        opens = "".join(o for o, _ in stack)
        closes = "".join(c for _, c in reversed(stack))

        def wrapped(n: int, opens: str = opens, closes: str = closes) -> str:
            return opens + "-" * n + "x" + closes + "\n"

        n = _bound(wrapped, 0, 7000, "MemoryError")
        if n is None:
            continue
        stacks += 1
        # The boundary pair, which the port may decline, and sources 150
        # calls either side, which it answers.
        for k in (n - 150, n, n + 1, n + 151):
            spelled = _runs((opens, 1), ("-", k), ("x" + closes + "\n", 1))
            add(_row(wrapped(k), spelled, k in (n, n + 1)))
    # Brackets to the tokenizer's limit, which the stack outlasts; their
    # trees are at most 203 deep, which every host converts.
    for o, c in [("(", ")"), ("[", "]"), ("f(", ")"), ("(a, ", ")"), ("x[", "]")]:
        for k in (199, 200):
            text = o * k + "x" + c * k + "\n"
            rows.append(
                _row(text, _runs((o, k), ("x", 1), (c, k), ("\n", 1)), o == "(a, ")
            )
    # The depth ``ast2obj`` converts. Where the interpreter stops depends on
    # the C depth its caller already holds (3 from a script, 30 under
    # pytest), so the rows hold depths whose answer no caller changes: the
    # port's last accepted chain and its first declined one (accepted from
    # a caller up to 103 deep), and the first it refuses, past the limit
    # from a caller 3 deep.
    for piece in ("+x", ".a"):
        for k in (9894, 9895, 9995, 10_000):
            text = "x" + piece * k + "\n"
            posix.append(_row(text, _runs(("x", 1), (piece, k), ("\n", 1)), k == 9895))
    return rows, posix


_LEAVES = [
    "x", "y", "1", "2.5", "3j", "'s'", "b'b'", "True", "None", "...", "f'a{x}'",
    "f'{x!r:>{y}}'", "f'{x=}'", "u's' 't'",
]  # fmt: skip
_TEMPLATES = [
    "-$", "not $", "~$", "await $", "$ + $", "$ * $", "$ ** $", "$ // $", "$ @ $", "$ | $",
    "$ << $", "$ and $", "$ or $", "$ < $ <= $", "$ not in $", "$ is not $", "$ == $",
    "$ if $ else $", "lambda: $", "lambda a, *b, c=$, **d: $", "lambda a=$, /, b=$: $",
    "f($)", "f($, *$, k=$, **$)", "f($ for a in $)", "$.a", "$($)", "$[$]", "$[$:$:$]",
    "$[$, *$]", "($, $)", "($,)", "[$, *$]", "{$: $, **$}", "{$, $}", "[$ for a in $ if $]",
    "($ for a, *b in $)", "{a: $ for a in $}", "{$ async for a in $}", "(a := $)",
    "(yield $)", "(yield from $)", "(yield)", "f'{$}'", "f'{$:{$}}'", "f'{$!s}'", "($)",
    "[$]", "$ if $ else lambda: $", "b'x' $", "'a' $", "[a for a.b[$] in $]",
]  # fmt: skip
_EDITS = [
    ",", ":", "*", "**", "=", ":=", "(", ")", "[", "]", " for ", " in ", " if ", "not ",
    "lambda ", "yield ", "<>", ".", "!r", "{", "}", " ", "await ", "x", "1", "-", "else ",
]  # fmt: skip


def sweep_cases(count: int) -> list[str]:
    """``count`` sources: a template expansion three deep in parentheses,
    one in six with an edit inserted and one in six with a character
    deleted."""
    state = 0x2545_F491_4F6C_DD1D
    mask = (1 << 64) - 1

    def draw() -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        return state >> 33

    def expr(depth: int) -> str:
        if depth == 0 or draw() % 4 == 0:
            return _LEAVES[draw() % len(_LEAVES)]
        template = _TEMPLATES[draw() % len(_TEMPLATES)]
        return "".join(expr(depth - 1) if ch == "$" else ch for ch in template)

    out = []
    for _ in range(count):
        body = expr(3)
        edit = draw() % 6
        if edit == 0:
            at = draw() % (len(body) + 1)
            body = body[:at] + _EDITS[draw() % len(_EDITS)] + body[at:]
        elif edit == 1:
            at = draw() % len(body)
            body = body[:at] + body[at + 1 :]
        out.append("(" + body + ")\n")
    return out


def _sweep(name: str, sources: list[str]) -> dict[str, Any]:
    answers = [_line(t) for t in sources]
    digest = hashlib.sha256("\n".join(answers).encode()).hexdigest()
    return {"sweep": name, "count": len(answers), "sha256": digest}


def _running() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def generate() -> dict[str, bytes]:
    """``pysyntax_expr.jsonl``: rows ``{"t", "parse", ...}`` and the sweeps
    ``fstring_pieces`` and ``expr_templates``."""
    name = "pysyntax_expr.jsonl"
    header: dict[str, Any] = {"fixture": "pysyntax_expr", "python": REPRODUCES}
    if _running() != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = _running()
        return {name: jsonl([header])}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rows = [_text_row(t) for t in _FORMS + _STATEMENTS]
        rows += [_text_row(_built(t)) for t in _texts()]
        rows += _depth_rows()[0]
        rows.append(_sweep("fstring_pieces", fstring_sweep_cases(20000)))
        rows.append(_sweep("expr_templates", sweep_cases(20000)))
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}


def generate_posix() -> dict[str, bytes]:
    """``pysyntax_expr_posix.jsonl``: the depth rows whose answer follows
    the host's C recursion limit, as a host whose limit is 10,000 gives
    them; elsewhere the header alone, naming the interpreter or limit."""
    name = "pysyntax_expr_posix.jsonl"
    header: dict[str, Any] = {
        "fixture": "pysyntax_expr_posix",
        "python": REPRODUCES,
        "c_recursion_limit": 10_000,
    }
    if _running() != REPRODUCES:
        header["declined"] = _running()
        return {name: jsonl([header])}
    if c_recursion_limit() != 10_000:
        header["declined"] = f"C recursion limit {c_recursion_limit()}"
        return {name: jsonl([header])}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rows = _depth_rows()[1]
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}
