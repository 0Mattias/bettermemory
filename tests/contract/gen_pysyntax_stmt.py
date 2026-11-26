"""Fixtures of the simple statements (`pysyntax::module_expr::stmt`,
native/README.md, "Python source as CPython reads it").

The reference is ``ast.parse(source)`` of CPython 3.13.13, as
``claims.check_claim`` (``claims.py`` 672) calls it, over modules of
simple statements. Both files are gated on the Python release: under
any other interpreter each generator declines and writes the header
alone, naming the interpreter.

``pysyntax_stmt_posix.jsonl`` holds the rows whose answer follows
``Py_C_RECURSION_LIMIT``, the depth ``ast2obj`` converts: the sources
``ast.parse`` reads into trees deeper than the port answers under
Windows' limit (1,000 on arm64, 3,000 on x64; `_windows_differs`), which
a POSIX host, whose limit is 10,000, accepts and Windows refuses with
RecursionError. It is POSIX only, and its generator declines on a host
whose limit is not 10,000. ``pysyntax_stmt.jsonl`` holds every other
row, which every host answers alike: the parser's stack is ``MAXSTACK``
everywhere, and the trees past ``ast2obj``'s depth (10,500 deep) are
past every host's limit.

A case row holds ``t``, the source, spelled as `gen_pysyntax` spells
texts; ``parse``, ``ok`` or the class ``ast.parse`` raises; and for an
accepted source ``all`` and ``top``, the names ``symbols._all_names`` and
``symbols._top_level_names`` read, ``ids``, the identifiers and numbers
no shape holds (`_ids`), and ``shape``, the tree in preorder by class
(`gen_pysyntax_expr._shape`). The port declines a line that opens a
compound statement (`_compound`): ``want`` is ``D`` on such a row, where
every line before it parses. A row with ``band`` true lies where the
port's estimate of the parser's depth does not decide: the port answers
as the reference or declines.

The depth rows find, by bisection over the interpreter, where a `-`
chain in each statement's slot overflows the parser's stack (the slots
the port counts exactly), where the second pass overflows on a line the
first pass refuses (`_REFUSED`, the boundary pair ``band``, the source
60 or more pieces below it answered exactly), and the edge of 24 bracket
stacks drawn by a fixed LCG into statement slots (the boundary pair
``band``, 150 calls either side answered exactly).

A sweep row pins by count and SHA-256 the answers to 20,000 lines drawn
by a fixed LCG from statement templates (`sweep_cases`), one in three
with a token inserted or a character deleted (``E`` and the class
``ast.parse`` raises when refused, a subclass of SyntaxError as
SyntaxError; ``D`` declined; else the names, ids and shape joined by
``|``).
"""

from __future__ import annotations

import ast
import functools
import hashlib
import tokenize
import warnings
from typing import Any

from bettermemory import symbols

from ._common import jsonl
from .gen_pysyntax import _spelled
from .gen_pysyntax_expr import (
    _FORMS,
    _STATEMENTS,
    _WRAPPERS,
    _bound,
    _parse,
    _running,
    _runs,
    _shape,
    c_recursion_limit,
)
from .gen_pysyntax_fstring import _c_tokens, _Unjudged
from .gen_pysyntax_module import _compact

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"

_COMPOUND = {"if", "while", "for", "with", "try", "def", "class", "async"}
_KEYWORDS = {
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
    "continue", "def", "del", "elif", "else", "except", "finally", "for", "from",
    "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass",
    "raise", "return", "try", "while", "with", "yield",
}  # fmt: skip
_OPS = set(tokenize.EXACT_TOKEN_TYPES.values())

#: The singletons ``ast2obj`` converts without counting a level.
_FLAT = (ast.expr_context, ast.operator, ast.unaryop, ast.cmpop, ast.boolop)
#: The lowest ``Py_C_RECURSION_LIMIT`` of a host CI runs (Windows arm64),
#: the C depth ``ast.parse`` starts ``ast2obj`` at from a script and the
#: most further callers add (the port's ``C_RECURSION_LIMIT``,
#: ``START_DEPTH`` and ``START_BAND``).
_LOWEST_LIMIT, _START_DEPTH, _START_BAND = 1000, 3, 100


def _depth(tree: ast.AST) -> int:
    """The levels ``ast2obj`` counts converting ``tree``, the Module one."""
    deepest = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 1)]
    while stack:
        node, d = stack.pop()
        deepest = max(deepest, d)
        for k in ast.iter_child_nodes(node):
            stack.append((k, d if isinstance(k, _FLAT) else d + 1))
    return deepest


def _windows_differs(tree: ast.AST | None) -> bool:
    """Whether the port's answer to an accepted source may differ on a
    Windows host: its tree is deeper than the port reads without a
    decline where the C recursion limit is lowest."""
    return (
        tree is not None and _depth(tree) + _START_DEPTH + _START_BAND > _LOWEST_LIMIT
    )


def _ids(tree: ast.AST) -> str:
    """The identifiers and numbers of statements no shape holds, in the
    shape's preorder: ``AnnAssign.simple``, ``ImportFrom``'s module and
    level, an ``alias``'s name and asname, a ``Global``'s or
    ``Nonlocal``'s names."""
    out: list[str] = []
    stack: list[ast.AST] = [tree]
    while stack:
        x = stack.pop()
        if isinstance(x, ast.AnnAssign):
            out.append(f"AnnAssign {x.simple}")
        elif isinstance(x, ast.ImportFrom):
            out.append(f"ImportFrom {x.module or ''} {x.level}")
        elif isinstance(x, ast.alias):
            out.append(f"alias {x.name} {x.asname or ''}")
        elif isinstance(x, (ast.Global, ast.Nonlocal)):
            out.append(f"{type(x).__name__} {','.join(x.names)}")
        stack.extend(reversed(list(ast.iter_child_nodes(x))))
    return ";".join(out)


def _compound(toks: list[tuple[str, str]]) -> bool:
    """The port's ``compound`` over one logical line's tokens."""
    typ, s = toks[0]
    if (typ, s) == ("OP", "@") or (typ == "NAME" and s in _COMPOUND):
        return True
    return (typ, s) == ("NAME", "match") and toks[-1] == ("OP", ":")


def _type_alias(toks: list[tuple[str, str]]) -> bool:
    """The port's test for a type alias at a statement's first token."""
    return (
        toks[0] == ("NAME", "type")
        and len(toks) > 2
        and toks[1][0] == "NAME"
        and toks[1][1] not in _KEYWORDS
        and toks[2] in (("OP", "="), ("OP", "["))
    )


def _declined(translated: str) -> bool | None:
    """True when the port declines a compound line or a type alias before
    any statement it refuses, False when it refuses first, None when no
    statement is one. A type alias may follow `;`: what precedes it on its
    line must parse too."""
    try:
        toks = _c_tokens(translated)
    except _Unjudged:
        return None
    if toks is None:
        return None
    lines = translated.split("\n")

    def before(row: int, col: int) -> bool:
        return (
            _parse("\n".join(lines[: row - 1]) + "\n" + lines[row - 1][:col])[0] == "ok"
        )

    cur: list[tuple[str, str]] = []
    starts: list[tuple[int, int, int]] = []
    depth = 0
    for t in toks:
        name = "OP" if t.type in _OPS else tokenize.tok_name[t.type]
        if name in ("NEWLINE", "ENDMARKER"):
            if cur and _compound(cur):
                return before(starts[0][1], 0)
            for at, row, col in starts:
                if _type_alias(cur[at:]):
                    return before(row, col)
            cur, starts, depth = [], [], 0
        elif name in ("INDENT", "DEDENT"):
            if not cur:
                return None
        elif name not in ("NL", "COMMENT"):
            if not cur or (cur[-1] == ("OP", ";") and depth == 0):
                starts.append((len(cur), *t.start))
            if name == "OP" and t.string in ("(", "[", "{"):
                depth += 1
            elif name == "OP" and t.string in (")", "]", "}"):
                depth -= 1
            cur.append((name, t.string))
    return None


def _answer(text: str) -> tuple[str, str, Any]:
    """``(want, parse, tree)``: what the port answers (``ok``, ``E`` or
    ``D``), the class ``ast.parse`` raises or ``ok``, and its tree."""
    parse, tree = _parse(text)
    d = _declined(text.replace("\r\n", "\n").replace("\r", "\n"))
    if d is not None:
        return ("D" if d else "E"), parse, None
    return ("ok" if tree is not None else "E"), parse, tree


def _names(tree: ast.Module) -> list[list[str]]:
    return [sorted(symbols._all_names(tree)), sorted(symbols._top_level_names(tree))]


def _line(text: str) -> str:
    want, parse, tree = _answer(text)
    if want == "E":
        # The port reads every token first: a tokenizer refusal may stand
        # where the parser raises IndentationError sooner.
        family = "SyntaxError" if parse in ("IndentationError", "TabError") else parse
        return f"E {family}"
    if tree is None:
        return want
    return "|".join([*(",".join(n) for n in _names(tree)), _ids(tree), _shape(tree)])


def _row(text: str, spelled: Any, band: bool = False) -> dict[str, Any]:
    want, parse, tree = _answer(text)
    row: dict[str, Any] = {"t": spelled, "parse": parse}
    if band:
        row["band"] = True
    if want == "D":
        row["want"] = "D"
    if tree is not None:
        shape = _shape(tree)
        row["all"], row["top"] = _names(tree)
        row["ids"] = _ids(tree)
        row["shape"] = (
            shape
            if len(shape) <= 300
            else {"n": len(shape), "h": hashlib.sha256(shape.encode()).hexdigest()}
        )
    return row


def _text_row(text: str) -> dict[str, Any]:
    return _row(text, _spelled(text) if len(text) <= 400 else _compact(text))


#: Each statement and its near misses.
_FORMS_STMT = [
    "x = 1", "x = y = z", "x, y = z", "x, = y", "*a = b", "*a, *b = c", "x = *a",
    "x = *a, b", "[a, *b] = c", "(a, b), c = d", "() = x", "[] = x", "(a) = 1",
    "((a)) = 1", "(*a, b) = c", "*[*a] = b", "*(a) = b", "x[1:2] = 3", "x[a, b:c] = 1",
    "f().x = 1", "f()[0] = 1", "'a'.x = 1", "x.y.z = 1", "f() = 1", "a + 1 = 2",
    "-a = 1", "None = 1", "True = 1", "... = 1", "1 = x", "x = 1 = 2", "x == y = 1",
    "(x := 1) = 2", "await x = 1", "lambda: x = 1", "x if y else z = 1", "f'' = 1",
    "{a} = 1", "{a: b} = 1", "[x for x in y] = 1", "(yield) = 1", "x = (yield)",
    "x = yield", "x = yield y", "x = yield y, z", "x = yield *a", "x = yield *a, b",
    "x = yield from y", "x = yield from y, z", "x = yield from", "a = b = yield c",
    "a = yield = c", "x = yield )", "x.None = 1", "x =", "= 1", "x = = 1",
    "x: int", "x: int = 1", "x: int = 1, 2", "x: int = *a, b", "x: int = yield",
    "x: int = yield from y", "x: yield", "x: *a", "x: a := 1", "x: (a := 1)",
    "x: lambda: y", "(a): int", "((a)): int = 1", "(a.b): int", "a.b: int = 1",
    "a[0]: int", "(a[0]): int", "a, b: int", "(a, b): int", "[a]: int", "*a: int",
    "(*a): int", "f(): int", "True: int", "x:", "x: int =", "x: int = 1 = 2",
    "(a).b: c", "(a)[0]: c", "(a)(b).c: d", "((a)).b: c", "(a.b).c: d",
    "(a[0]).b: c", "(a).b: c = 1", "x; (a).b: int", "(a) .b: int", "(match).b: int",
    "((a).b).c: d", "(a.b)(c).d: e", "(a)[0].b: c", "(a)\\\n.b: c", "((a).b): int",
    "((a).b): c = 1", "(f()).b: c", "(a,).b: c", "[a].b: c", "(1).b: c", "('s').b: c",
    "(a).b = 1", "(a).b += 1", "del (a).b", "(a): c", "((a)): c", "(a.b): c",
    "x: int: str", "x: int; y: str", "a += 1", "a -= 1", "a *= 1", "a /= 1", "a //= 1",
    "a %= 1", "a @= 1", "a &= 1", "a |= 1", "a ^= 1", "a >>= 1", "a <<= 1",
    "a **= 1", "a += yield", "a += yield from b", "a += 1, 2", "a += *b, c",
    "(a, b) += 1", "[a] += 1", "a, b += 1", "(a) += 1", "a.b += 1", "a[0] += 1",
    "f() += 1", "a + 1 += 2", "*a += 1", "x += 1 = 2", "x += y += 1", "x +=",
    "x = y += 1", "return", "return x", "return x, y", "return *a, b", "return *a",
    "return x = 1", "return yield", "return (yield)", "return;", "raise", "raise a",
    "raise a from b", "raise from b", "raise a from", "raise a, b", "raise *a",
    "raise a from b from c", "raise a := 1", "assert a", "assert a, b", "assert",
    "assert a,", "assert (a, b)", "assert a, b, c", "assert *a", "pass", "pass x",
    "break", "continue", "break; continue", "del x", "del x, y", "del x,", "del",
    "del (a), [b], c.d, e[0]", "del ()", "del []", "del (a,)", "del [a, (b, c)]",
    "del *a", "del (*a,)", "del [*a]", "del f()", "del a + 1", "del (a := 1)",
    "del x = 1", "del x,,", "del x, y,", "del a.b.c, d[e][f]", "del (yield)",
    "global a", "global a, b", "global a,", "global", "global a b", "global a.b",
    "nonlocal x", "nonlocal x, y", "nonlocal", "global None", "yield", "yield x",
    "yield x, y", "yield *a, b", "yield *a", "yield from x", "yield from x, y",
    "yield from", "yield = 1", "yield x = 1", "import a", "import a.b.c",
    "import a.b.c as d", "import a . b", "import a, b as c, d.e", "import a as b.c",
    "import (a)", "import a,", "import", "import a.", "import a as", "import *",
    "import a as None", "import None", "import a.if", "from a import b",
    "from a import b, c", "from a import b as c, d as e", "from a import (b)",
    "from a import (b,)", "from a import (b, c,)", "from a import b,",
    "from a import ()", "from a import (*)", "from a import *", "from a import *, b",
    "from . import *", "from .. import x", "from ... import x", "from .... import x",
    "from . . . import x", "from .a.b import c", "from . a import b",
    "from ... a import (b as c, d,)", "from import x", "from a", "from a import",
    "from a. import b", "from a import b.c", "from a import (b", "from None import a",
    "from a import None", "from a import b as None", "from __future__ import braces",
    "from __future__ import annotations", "from __future__ import barry_as_FLUFL\n1 <> 2\n",
    "from __future__ import barry_as_FLUFL\n1 != 2\n", "1 <> 2\nfrom __future__ import barry_as_FLUFL\n",
    "from __future__ import barry_as_FLUFL; x = 1 <> 2 < 3\n",
    "from __future__ import (a, barry_as_FLUFL as b)\nf'{x <> y}'\n",
    "x = 1\nfrom __future__ import barry_as_FLUFL\nx <> y\n",
    "from .__future__ import barry_as_FLUFL\n1 <> 2\n",
    "from __future__.a import barry_as_FLUFL\n1 <> 2\n",
    "from __future__ import barry_as_flufl\n1 <> 2\n",
    "import __future__\n1 <> 2\n", "from __future__ import *\n1 <> 2\n",
    "from __future__ import \uff42arry_as_FLUFL\n1 <> 2\n",
    "from \uff3f_future__ import barry_as_FLUFL\n1 <> 2\n",
    "from __future__ import barry_as_FLUFL\n[a for a in b if a <> c]\n",
    "nonlocal x\nbreak\ncontinue\nreturn\n", "x;", "x; y", "x;;", ";x", "x; ;",
    "x = 1; y = 2;", "pass; pass", "return x; return", "import a; from b import c",
    "x; if y: pass", "x; def f(): pass", "x; type X = int", "x $; type X = int",
    "x; type X", "x = 1; match x:", "x\\\n; type X = int", "(x;\n type X = int)", "x; else: pass", "x = 1\ny = 2\n",
    "x = 1\n  y = 2\n", "x = 1\nif y:\n    z = 2\n", "if y:\n    z = 2\n",
    "x $\nif y: pass\n", "for x in y: pass", "while x: pass", "with x: pass",
    "try:\n    pass\nfinally:\n    pass\n", "def f(): pass", "class C: pass",
    "async def f(): pass", "@d\ndef f(): pass", "else: pass", "elif x: pass",
    "except: pass", "finally: pass", "type X = int", "type X[T] = list[T]",
    "type = 1", "type(x)", "type.x = 1", "type x", "match = 1", "match(x)",
    "match.x: int = 1", "match x:\n    case 1: pass\n", "match x: pass", "match x:",
    "case = 1", "case x: pass", "print >> f, x",
]  # fmt: skip

#: Names past ASCII in each slot: NFKC'd, a constant's NFKC a ValueError.
_NAMES = [
    "\uff58 = 1", "\uff58: int", "\uff58 += 1", "del \uff58", "global \uff58",
    "x.\uff59 = 1", "f(\uff4b=1).x = 1", "import \uff4d.\uff4e as \uff4f",
    "from \uff4d import \uff4e as \uff4f", "from .\uff4d import *",
    "\uff2e\uff4f\uff4e\uff45 = 1", "\uff2e\uff4f\uff4e\uff45: int",
    "\uff2e\uff4f\uff4e\uff45 += 1", "x = \uff2e\uff4f\uff4e\uff45 = 1",
    "del \uff2e\uff4f\uff4e\uff45", "global \uff34\uff52\uff55\uff45",
    "nonlocal a, \uff2e\uff4f\uff4e\uff45", "import \uff2e\uff4f\uff4e\uff45",
    "import a.\uff2e\uff4f\uff4e\uff45", "import a as \uff2e\uff4f\uff4e\uff45",
    "from \uff2e\uff4f\uff4e\uff45 import a", "from a import \uff2e\uff4f\uff4e\uff45",
    "from a import b as \uff2e\uff4f\uff4e\uff45", "from .\uff34\uff52\uff55\uff45 import a",
    "\uff49\uff4d\uff50\uff4f\uff52\uff54 a", "\uff49\uff4d\uff50\uff4f\uff52\uff54 = 1",
    "\uff44\uff45\uff4c x", "\uff50\uff41\uff53\uff53", "\uff52\uff45\uff54\uff55\uff52\uff4e",
    "\uff4d\uff41\uff54\uff43\uff48 x:\n    case 1: pass\n",
    "\uff54\uff59\uff50\uff45 X = int", "type \uff38 = int", "x.\uff2e\uff4f\uff4e\uff45 = 1",
    # What the second pass reads past a target the first pass refuses, and
    # the subject `match_stmt` reads first.
    "f()=\uff2e\uff4f\uff4e\uff45", "a+1+=\uff2e\uff4f\uff4e\uff45",
    "[a]:\uff2e\uff4f\uff4e\uff45", "del f(),\uff2e\uff4f\uff4e\uff45",
    "-x = \uff2e\uff4f\uff4e\uff45", "(a, b) += \uff2e\uff4f\uff4e\uff45",
    "match \uff2e\uff4f\uff4e\uff45: pass", "match \uff2e\uff4f\uff4e\uff45",
    "match x, *\uff2e\uff4f\uff4e\uff45", "match a := \uff2e\uff4f\uff4e\uff45",
    "match = \uff2e\uff4f\uff4e\uff45", "match.\uff2e\uff4f\uff4e\uff45 = 1",
    "f(): int = \uff2e\uff4f\uff4e\uff45", "x = f() = \uff2e\uff4f\uff4e\uff45",
    "del f(); \uff2e\uff4f\uff4e\uff45", "*a: \uff2e\uff4f\uff4e\uff45",
    "*a, b: \uff2e\uff4f\uff4e\uff45", "*a += \uff2e\uff4f\uff4e\uff45",
    "*f() = \uff2e\uff4f\uff4e\uff45", "f() = a if \uff2e\uff4f\uff4e\uff45 else b",
    "f() = a | \uff2e\uff4f\uff4e\uff45", "f() = a < \uff2e\uff4f\uff4e\uff45",
    "f() = yield \uff2e\uff4f\uff4e\uff45", "[a] + 1 = \uff2e\uff4f\uff4e\uff45",
    "(a) + 1 = \uff2e\uff4f\uff4e\uff45", "([a]) + 1 = \uff2e\uff4f\uff4e\uff45",
    "() + 1 = \uff2e\uff4f\uff4e\uff45", "(x for x in y) + 1 = \uff2e\uff4f\uff4e\uff45",
    "((x for x in y)) + 1 = \uff2e\uff4f\uff4e\uff45", "((a, b)) + 1 = \uff2e\uff4f\uff4e\uff45",
    "True + 1 = \uff2e\uff4f\uff4e\uff45", "[x for x in y] + 1 = \uff2e\uff4f\uff4e\uff45",
    "x == y = \uff2e\uff4f\uff4e\uff45", "not x = \uff2e\uff4f\uff4e\uff45",
    "await x = \uff2e\uff4f\uff4e\uff45", "lambda: x = \uff2e\uff4f\uff4e\uff45",
    "f(), x = \uff2e\uff4f\uff4e\uff45", "x, f() = \uff2e\uff4f\uff4e\uff45",
    "x, f(), = \uff2e\uff4f\uff4e\uff45", "f(), *x = \uff2e\uff4f\uff4e\uff45",
    "f(), None = \uff2e\uff4f\uff4e\uff45", "a, b += \uff2e\uff4f\uff4e\uff45",
    "del x, f(), \uff2e\uff4f\uff4e\uff45", "del f(), *\uff2e\uff4f\uff4e\uff45",
    "del *a, \uff2e\uff4f\uff4e\uff45", "(a).b: \uff2e\uff4f\uff4e\uff45",
    "(a).b: int = \uff2e\uff4f\uff4e\uff45", "f(): lambda: \uff2e\uff4f\uff4e\uff45",
    "f(): a := \uff2e\uff4f\uff4e\uff45", "x; f() = \uff2e\uff4f\uff4e\uff45",
    "f() = \uff2e\uff4f\uff4e\uff45 = 1", "f() += yield \uff2e\uff4f\uff4e\uff45",
    # A SyntaxError the second pass raises before it reaches the constant:
    # a line before whose `match_stmt` subject it refuses
    # (`invalid_match_stmt`, `invalid_named_expression`), and the lines
    # where it does not; a constant that rule reads past the subject.
    "match(x)\nf()=\uff2e\uff4f\uff4e\uff45", "match[0]\nf()=\uff2e\uff4f\uff4e\uff45",
    "match -1\nf()=\uff2e\uff4f\uff4e\uff45", "match(x)\ndel f(),\uff2e\uff4f\uff4e\uff45",
    "match(x)\n[a]:\uff2e\uff4f\uff4e\uff45", "match(x)\na+1+=\uff2e\uff4f\uff4e\uff45",
    "match(x)\n-x = \uff2e\uff4f\uff4e\uff45", "match (x),\nf()=\uff2e\uff4f\uff4e\uff45",
    "match -1, 2\nf()=\uff2e\uff4f\uff4e\uff45", "match *a, b\nf()=\uff2e\uff4f\uff4e\uff45",
    "match(x) if y else z\nf()=\uff2e\uff4f\uff4e\uff45",
    "match[0] = 1\nf()=\uff2e\uff4f\uff4e\uff45", "match *x\nf()=\uff2e\uff4f\uff4e\uff45",
    "match.x\nf()=\uff2e\uff4f\uff4e\uff45", "match(x); f()=\uff2e\uff4f\uff4e\uff45",
    "f()=\uff2e\uff4f\uff4e\uff45\nmatch(x)", "match(x)\nmatch \uff2e\uff4f\uff4e\uff45",
    "print(x)\nf()=\uff2e\uff4f\uff4e\uff45", "print.x\nf()=\uff2e\uff4f\uff4e\uff45",
    "f()=\uff2e\uff4f\uff4e\uff45\nprint -1", "match(x).y = 1\nf()=\uff2e\uff4f\uff4e\uff45",
    "match(x), y = 1, 2\nf()=\uff2e\uff4f\uff4e\uff45", "match[0].y = 1\nf()=\uff2e\uff4f\uff4e\uff45",
    "match(x).y = z = 1\nf()=\uff2e\uff4f\uff4e\uff45", "match(x).y += 1\nf()=\uff2e\uff4f\uff4e\uff45",
    "match f(x) = \uff2e\uff4f\uff4e\uff45", "match f(x) := \uff2e\uff4f\uff4e\uff45",
    "match f(x) = z = \uff2e\uff4f\uff4e\uff45", "match(x)\nmatch f(x) = \uff2e\uff4f\uff4e\uff45",
    # The parenthesis right after `match` the second pass refuses as the
    # subject's first atom (`invalid_group`, `invalid_named_expression`),
    # and the forms it does not.
    "match(*a) = \uff2e\uff4f\uff4e\uff45", "match (*a) = \uff2e\uff4f\uff4e\uff45",
    "match(**a) = \uff2e\uff4f\uff4e\uff45", "match(a=1) = \uff2e\uff4f\uff4e\uff45",
    "match(*a)\nf() = \uff2e\uff4f\uff4e\uff45", "match(*a)\ndel f(), \uff2e\uff4f\uff4e\uff45",
    "match(*a)\n[a]: \uff2e\uff4f\uff4e\uff45", "match(*a)\na + 1 += \uff2e\uff4f\uff4e\uff45",
    "y = 1\nmatch(*a)\nf() = \uff2e\uff4f\uff4e\uff45", "match(*a)\nf() = \uff2e\uff4f\uff4e\uff45; y",
    "match(**a)\nf() = \uff2e\uff4f\uff4e\uff45", "match(a, b=1)\nf() = \uff2e\uff4f\uff4e\uff45",
    "match(a=1)\nx = 1\nf() = \uff2e\uff4f\uff4e\uff45", "match(*a).y = 1\nf() = \uff2e\uff4f\uff4e\uff45",
    "match(*a); f() = \uff2e\uff4f\uff4e\uff45", "match(*a, b=1)\nf() = \uff2e\uff4f\uff4e\uff45",
    "match(*a if b else c)\nf() = \uff2e\uff4f\uff4e\uff45", "match(a := 1, b=2)\nf() = \uff2e\uff4f\uff4e\uff45",
    "match(*a)", "match(*a)[0] = \uff2e\uff4f\uff4e\uff45", "match(x, *a) = \uff2e\uff4f\uff4e\uff45",
    "match(x, **a)\nf() = \uff2e\uff4f\uff4e\uff45", "f(*a) = \uff2e\uff4f\uff4e\uff45",
    "match(*a, **b)\nf() = \uff2e\uff4f\uff4e\uff45", "match(**a, b=1)\nf() = \uff2e\uff4f\uff4e\uff45",
    "match(a=lambda: 1)\nf() = \uff2e\uff4f\uff4e\uff45", "match(a=not b)\nf() = \uff2e\uff4f\uff4e\uff45",
    "match(a=\uff2e\uff4f\uff4e\uff45)", "match(a=1, \uff2e\uff4f\uff4e\uff45)",
    # A constant's identifier in a call's argument after a keyword, which
    # only the second pass reads: a SyntaxError it raises first stands.
    "match(a=1, a := \uff2e\uff4f\uff4e\uff45)", "match(a=1, (a := \uff2e\uff4f\uff4e\uff45))",
    "match(*a)\nf(a=1, x + \uff2e\uff4f\uff4e\uff45)", "match(x)\nf(a=1, a := \uff2e\uff4f\uff4e\uff45)",
    "match(*a)\nf(**a, *\uff2e\uff4f\uff4e\uff45)", "match(*a)\nf(a=1, g(\uff2e\uff4f\uff4e\uff45))",
    "match(*a)\nf(a=1, \uff2e\uff4f\uff4e\uff45)", "match(*a)\nf(a=1, *\uff2e\uff4f\uff4e\uff45)",
    "match(*a)\nf(a=1, *(x + \uff2e\uff4f\uff4e\uff45))", "match(*a)\nf(a=1, b=\uff2e\uff4f\uff4e\uff45)",
    "match(*a)\nf(**a, \uff2e\uff4f\uff4e\uff45.x)", "f(a=1, x + \uff2e\uff4f\uff4e\uff45)",
    "f(a=1, a := \uff2e\uff4f\uff4e\uff45)", "f(**a, *\uff2e\uff4f\uff4e\uff45)",
    # A walrus no parenthesis holds ends the subject's last element:
    # `named_expression` takes it before its error rule reads on.
    "match a := 1 := \uff2e\uff4f\uff4e\uff45", "match a := 1 := \uff2e\uff4f\uff4e\uff45\ny = 1",
    "x\nmatch a := 1 := \uff2e\uff4f\uff4e\uff45", "match x, a := 1 := \uff2e\uff4f\uff4e\uff45",
    "match a, b := c := \uff2e\uff4f\uff4e\uff45", "match a := b := \uff2e\uff4f\uff4e\uff45",
    "match a := (1) := \uff2e\uff4f\uff4e\uff45", "match *a, b := 1 := \uff2e\uff4f\uff4e\uff45",
    "match (a := 1) := \uff2e\uff4f\uff4e\uff45", "match a := 1 = \uff2e\uff4f\uff4e\uff45",
    "match a := 1, := \uff2e\uff4f\uff4e\uff45", "match a := 1 := 2",
    "match x, (a := 1) := \uff2e\uff4f\uff4e\uff45", "match x, a := 1 = \uff2e\uff4f\uff4e\uff45",
]  # fmt: skip

#: Chains in each statement slot the port counts exactly: (head, piece,
#: tail).
_CHAINS = [
    ("x = ", "-", "x\n"), ("x = y = ", "-", "x\n"), ("x = y, ", "-", "x\n"),
    ("x = *", "-", "x,\n"), ("x = yield ", "-", "x\n"), ("x = yield y, ", "-", "x\n"),
    ("x = yield *", "-", "x\n"), ("x = yield from ", "-", "x\n"), ("x: ", "-", "x\n"),
    ("x.y: ", "-", "x\n"), ("(x): ", "-", "x\n"), ("x: int = ", "-", "x\n"),
    ("x: int = *", "-", "x,\n"), ("x: int = yield ", "-", "x\n"),
    ("x: int = yield from ", "-", "x\n"), ("x += ", "-", "x\n"), ("x += y, ", "-", "x\n"),
    ("x += yield ", "-", "x\n"), ("x += yield from ", "-", "x\n"), ("return ", "-", "x\n"),
    ("return y, ", "-", "x\n"), ("return *", "-", "x,\n"), ("raise ", "-", "x\n"),
    ("raise y from ", "-", "x\n"), ("assert ", "-", "x\n"), ("assert y, ", "-", "x\n"),
    ("yield ", "-", "x\n"), ("yield *", "-", "x\n"), ("yield from ", "-", "x\n"),
    ("x; ", "-", "x\n"), ("x; y; ", "-", "x\n"), ("x; x = ", "-", "x\n"),
    ("x; x: ", "-", "x\n"), ("x; return ", "-", "x\n"), ("x; raise y from ", "-", "x\n"),
    ("x = 1; ", "-", "x\n"), ("import a; ", "-", "x\n"), ("", "-", "x;\n"),
    ("x = ", "-", "\uff34rue\n"), ("match ", "-", "x\n"), ("match ", "-", "x, y\n"),
    ("match ", "-", "\uff2e\uff4f\uff4e\uff45\n"), ("match ", "-", "x, \uff2e\uff4f\uff4e\uff45\n"),
    ("match a, ", "-", "\uff2e\uff4f\uff4e\uff45\n"),
    ("match a, b, ", "-", "\uff2e\uff4f\uff4e\uff45\n"),
    ("match *", "-", "\uff2e\uff4f\uff4e\uff45, y\n"),
    ("match a, *", "-", "\uff2e\uff4f\uff4e\uff45\n"), ("match *a, ", "-", "x\n"),
    ("match ", "-", "x\ny = 1\n"), ("x\nmatch ", "-", "x\n"),
    # A `match` line whose subject's reading stops before the chain: the
    # line's own MemoryError stands.
    ("match = ", "-", "x\n"), ("match.y = ", "-", "x\n"), ("match += ", "-", "x\n"),
    ("match: int = ", "-", "x\n"), ("match[0][1] = ", "-", "x\n"), ("match, ", "-", "x\n"),
    ("match[0].y = ", "-", "x\n"),
]  # fmt: skip

#: Lines opening with `match` whose subject `match_stmt` reads where the
#: port does not decide: what that reading leaves in the parser's memo
#: (`match *` before a chain is read deeper than its own reading
#: overflows), a bracket around the subject's element, and the second
#: pass after it: (head, piece, tail, the pieces below the boundary
#: answered exactly).
_SUBJECTS = [
    ("match *", "-", "x, y\n", 10), ("match [", "-", "\uff2e\uff4f\uff4e\uff45]\n", 150),
    ("match(", "-", "x)\n", 150), ("match a, (", "-", "\uff2e\uff4f\uff4e\uff45)\n", 150),
    ("match ", "-", "x $\n", 60), ("match a, ", "-", "x\n", 60),
    ("match(", "-", "x)\nf() = 1\n", 150),
]  # fmt: skip

#: Lines the first pass refuses (or whose target the first pass does not
#: read), where the second pass overflows first: (head, piece, tail, the
#: pieces below the boundary answered exactly).
_REFUSED = [
    ("x = ", "-", "x $\n", 60), ("x: ", "-", "x $\n", 60), ("x += ", "-", "x $\n", 60),
    ("return ", "-", "x $\n", 60), ("yield ", "-", "x $\n", 60),
    ("x: int = ", "-", "x $\n", 60), ("x; ", "-", "x $\n", 60), ("del ", "-", "x\n", 60),
    ("del y, ", "-", "x\n", 60), ("x; del ", "-", "x\n", 60),
    ("x; del y, ", "-", "x $\n", 60), ("f() = ", "-", "x\n", 60), ("x = ", "-", "\n", 60),
    ("", "-", "x = 1\n", 60), ("x, y: ", "-", "x\n", 60), ("(yield ", "-", "x) = 1\n", 120),
]  # fmt: skip

#: Statement slots a bracket stack is drawn into.
_SLOTS = [
    "x = %s\n", "x: %s\n", "x: int = %s\n", "return %s\n", "x[%s] = 1\n", "del x[%s]\n",
    "x; %s\n", "y, x[%s] = 1\n", "x = y = %s\n", "raise %s\n", "yield %s\n", "x += %s\n",
]  # fmt: skip


class _Lcg:
    def __init__(self, state: int) -> None:
        self.state = state

    def draw(self) -> int:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) & (
            (1 << 64) - 1
        )
        return self.state >> 33


@functools.cache
def _depth_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The depth rows every host answers alike and those whose answer
    follows the C recursion limit (`_windows_differs`)."""
    rows: list[dict[str, Any]] = []
    posix: list[dict[str, Any]] = []

    def add(text: str, spelled: Any, band: bool = False) -> None:
        tree = _parse(text)[1]
        (posix if _windows_differs(tree) else rows).append(_row(text, spelled, band))

    for head, piece, tail in _CHAINS:

        def make(n: int, head: str = head, piece: str = piece, tail: str = tail) -> str:
            return head + piece * n + tail

        n = _bound(make, 0, 7000, "MemoryError", _parse(make(0))[0])
        assert n is not None, (head, piece, tail)
        for k in (n, n + 1):
            add(make(k), _runs((head, 1), (piece, k), (tail, 1)))
    for head, piece, tail, below in _REFUSED + _SUBJECTS:

        def refused(
            n: int, head: str = head, piece: str = piece, tail: str = tail
        ) -> str:
            return head + piece * n + tail

        n = _bound(refused, 0, 7000, "MemoryError", _parse(refused(0))[0])
        assert n is not None, (head, piece, tail)
        for k, band in ((n - below, False), (n, True), (n + 1, True)):
            add(refused(k), _runs((head, 1), (piece, k), (tail, 1)), band)
    g = _Lcg(0x6A09_E667_F3BC_C908)
    stacks = 0
    while stacks < 24:
        slot = _SLOTS[g.draw() % len(_SLOTS)]
        stack = [_WRAPPERS[g.draw() % len(_WRAPPERS)] for _ in range(1 + g.draw() % 4)]
        opens = "".join(o for o, _ in stack)
        closes = "".join(c for _, c in reversed(stack))
        head, tail = slot.split("%s")

        def wrapped(
            n: int, opens: str = opens, closes: str = closes, head: str = head,
            tail: str = tail,
        ) -> str:  # fmt: skip
            return head + opens + "-" * n + "x" + closes + tail

        n = _bound(wrapped, 0, 7000, "MemoryError")
        if n is None:
            continue
        stacks += 1
        for k in (n - 150, n, n + 1, n + 151):
            spelled = _runs((head + opens, 1), ("-", k), ("x" + closes + tail, 1))
            add(wrapped(k), spelled, k in (n, n + 1))
    # The second pass refuses at the literal target before it reads the
    # chain, which the port bounds: SyntaxError however deep.
    text = "x = 1 = " + "-" * 6000 + "x\n"
    add(text, _runs(("x = 1 = ", 1), ("-", 6000), ("x\n", 1)), True)
    # Deeper than any host's ``ast2obj`` converts: RecursionError
    # everywhere, the statement a level above its value.
    for head in ("x = ", "x: int = ", "x += ", "return ", "del x, "):
        text = head + "x" + "+x" * 10_500 + "\n"
        add(text, _runs((head + "x", 1), ("+x", 10_500), ("\n", 1)))
    # The module's depth is its deepest statement's, wherever it lies.
    for head, after in (
        ("x = ", "\ny = 1\n"),
        ("x = ", "; y = 1\n"),
        ("y = 1; x = ", "\n"),
    ):
        text = head + "x" + "+x" * 10_500 + after
        add(text, _runs((head + "x", 1), ("+x", 10_500), (after, 1)))
    return rows, posix


_LEAVES = ["x", "1", "'s'", "f'{x}'", "None", "...", "y.z", "a[0]", "f()"]
_EXPRS = [
    "-$", "not $", "$ + $", "$ if $ else $", "lambda: $", "f($, k=$)", "$.a", "$[$]",
    "($, $)", "[$, *$]", "{$: $}", "(a := $)", "(yield $)", "[$ for a in $]", "*$",
    "await $",
]  # fmt: skip
_TARGETS = ["a", "b.c", "d[$]", "e[$:$]", "f($).g", "(h)", "[i, j]", "(k, *l)", "*m"]
_STMTS = [
    "$", "T = $", "T = T = $", "T, T = $", "T: $", "T: $ = $", "T += $", "T -= $",
    "T **= $", "T = yield $", "T: $ = yield", "T += yield from $", "return",
    "return $", "return $, *$", "raise", "raise $", "raise $ from $", "assert $",
    "assert $, $", "del T", "del T, T", "del (T, [T])", "pass", "break", "continue",
    "global a, b", "nonlocal a", "import a.b as c", "import a, b.c", "from . import *",
    "from .a import (b as c, d,)", "from a import b, c as d", "yield", "yield $",
    "yield from $", "*T, T = $", "[T, *T] = $", "(T) = $", "(T): $", "$; $",
    "T = $; $", "$;", "if $: pass", "match $:", "type T = $",
]  # fmt: skip
_EDITS = [
    " = ", ":", ";", ",", "*", "del ", "yield ", "(", ")", "import ", " as ", ".",
    " += ", "return ", " from ", "lambda ", "x", "1", " ", "global ", "[", "]",
]  # fmt: skip


def sweep_cases(count: int) -> list[str]:
    """``count`` sources: one or two lines of statements drawn from
    `_STMTS`, their targets from `_TARGETS` (one in four an expression)
    and expressions from `_EXPRS` two deep, one in six with an edit
    inserted and one in six with a character deleted."""
    g = _Lcg(0xBB67_AE85_84CA_A73B)

    def expr(depth: int) -> str:
        if depth == 0 or g.draw() % 3 == 0:
            return _LEAVES[g.draw() % len(_LEAVES)]
        return fill(_EXPRS[g.draw() % len(_EXPRS)], depth - 1)

    def fill(template: str, depth: int) -> str:
        out = []
        for ch in template:
            if ch == "$":
                out.append(expr(depth))
            elif ch == "T":
                if g.draw() % 4 == 0:
                    out.append(expr(1))
                else:
                    out.append(fill(_TARGETS[g.draw() % len(_TARGETS)], 1))
            else:
                out.append(ch)
        return "".join(out)

    out = []
    for _ in range(count):
        lines = []
        for _ in range(1 + g.draw() % 2):
            body = fill(_STMTS[g.draw() % len(_STMTS)], 2)
            edit = g.draw() % 6
            if edit == 0:
                at = g.draw() % (len(body) + 1)
                body = body[:at] + _EDITS[g.draw() % len(_EDITS)] + body[at:]
            elif edit == 1:
                at = g.draw() % len(body)
                body = body[:at] + body[at + 1 :]
            lines.append(body + "\n")
        out.append("".join(lines))
    return out


def generate() -> dict[str, bytes]:
    """``pysyntax_stmt.jsonl``: rows ``{"t", "parse", ...}`` and the sweep
    ``stmt_templates``."""
    name = "pysyntax_stmt.jsonl"
    header: dict[str, Any] = {"fixture": "pysyntax_stmt", "python": REPRODUCES}
    if _running() != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = _running()
        return {name: jsonl([header])}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        texts = _FORMS_STMT + _NAMES + _STATEMENTS + _FORMS
        assert not any(_windows_differs(_parse(t)[1]) for t in texts)
        rows = [_text_row(t) for t in texts]
        rows += _depth_rows()[0]
        answers = [_line(t) for t in sweep_cases(20000)]
        rows.append(
            {
                "sweep": "stmt_templates",
                "count": len(answers),
                "sha256": hashlib.sha256("\n".join(answers).encode()).hexdigest(),
            }
        )
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}


def generate_posix() -> dict[str, bytes]:
    """``pysyntax_stmt_posix.jsonl``: the depth rows whose answer follows
    the host's C recursion limit, as a host whose limit is 10,000 gives
    them; elsewhere the header alone, naming the interpreter or limit."""
    name = "pysyntax_stmt_posix.jsonl"
    header: dict[str, Any] = {
        "fixture": "pysyntax_stmt_posix",
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
