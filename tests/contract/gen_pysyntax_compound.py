"""Fixtures of blocks and the compound statements `if`, `while`, `for` and
`with` (`pysyntax::module_expr::compound`, native/README.md, "Python
source as CPython reads it").

The reference is ``ast.parse(source)`` of CPython 3.13.13, as
``claims.check_claim`` (``claims.py`` 672) calls it, over modules of
statements in blocks (`parse_module_blocks`). Both files are gated on the
Python release: under any other interpreter each generator declines and
writes the header alone, naming the interpreter.

``pysyntax_compound_posix.jsonl`` holds the rows whose answer follows
``Py_C_RECURSION_LIMIT``: the sources ``ast.parse`` reads into trees
deeper than the port answers under Windows' limit (`_windows_differs`). It
is POSIX only, and its generator declines on a host whose limit is not
10,000. ``pysyntax_compound.jsonl`` holds every other row.

A case row holds ``t``, the source, spelled as `gen_pysyntax` spells
texts; ``parse``, ``ok`` or the class ``ast.parse`` raises; and for an
accepted source ``all``, ``top``, ``ids`` and ``shape`` as
`gen_pysyntax_stmt` writes them. The port declines a `def`, `class`,
`try`, decorated, `match` or `type` statement (`_declined`): ``want`` is
``D`` on such a row, where every statement before it parses. A row with
``band`` true lies where the port's estimate of the parser's depth does
not decide: the port answers as the reference or declines.

The `match` rows (`_earlier`) put a line the first pass reads as an
expression statement, whose subject the second pass may refuse, before
a missing block or an unexpected indent: the second pass reads the
module from its start, so its SyntaxError there comes first.

The depth rows find, by bisection over the interpreter, where a `-` chain
in each slot of a compound statement overflows the parser's stack (the
slots the port counts exactly), where the second pass overflows on a
module the first pass refuses (`_REFUSED`: the boundary pair ``band``, 60
pieces below it answered exactly), where an `elif` chain overflows
(`_ELIFS`, likewise), and the edge of a bracket in a target or item
(`_BRACKETS`: the boundary pair ``band``, 150 calls either side answered
exactly).

A sweep row pins by count and SHA-256 the answers to 20,000 modules drawn
by a fixed LCG from block templates (`sweep_cases`), four in nine with
a token inserted, a character deleted, or an indentation step inserted or
deleted, one in nine each (``E`` and the class ``ast.parse`` raises when
refused; ``D`` declined; else the names, ids and shape joined by ``|``),
where the tokenizer refuses the module, a subclass of SyntaxError as
SyntaxError.
"""

from __future__ import annotations

import functools
import hashlib
import tokenize
import warnings
from typing import Any

from ._common import jsonl
from .gen_pysyntax import _spelled
from .gen_pysyntax_expr import (
    _bound,
    _parse,
    _running,
    _runs,
    _shape,
    c_recursion_limit,
)
from .gen_pysyntax_fstring import _c_tokens, _Unjudged
from .gen_pysyntax_module import _compact
from .gen_pysyntax_stmt import _ids, _Lcg, _names, _type_alias, _windows_differs

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"

_OPS = set(tokenize.EXACT_TOKEN_TYPES.values())
#: The keywords whose line holds a head's `:` before a block on its line.
_HEADS = {"if", "elif", "else", "while", "for", "with", "async"}
_NONE = "\uff2e\uff4f\uff4e\uff45"


def _definition(toks: list[tuple[str, str]]) -> bool:
    """Whether a logical line opens a statement the port declines: a
    definition, `try`, a decorator, or `match` with its `:` last."""
    head = toks[0]
    if head in (("NAME", "def"), ("NAME", "class"), ("NAME", "try"), ("OP", "@")):
        return True
    if head == ("NAME", "async") and len(toks) > 1 and toks[1] == ("NAME", "def"):
        return True
    return head == ("NAME", "match") and toks[-1] == ("OP", ":")


def _starts(toks: list[tuple[str, str]]) -> list[int]:
    """Where each simple statement of a logical line starts: its first
    token, past a head's `:` (one no `lambda` takes), and past each `;`,
    at bracket depth 0."""
    starts = [0]
    depth, lambdas = 0, 0
    head = toks[0][0] == "NAME" and toks[0][1] in _HEADS
    for i, (typ, s) in enumerate(toks):
        if typ == "OP" and s in ("(", "[", "{"):
            depth += 1
        elif typ == "OP" and s in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and (typ, s) == ("NAME", "lambda"):
            lambdas += 1
        elif depth == 0 and (typ, s) == ("OP", ":") and head:
            if lambdas:
                lambdas -= 1
            else:
                head = False
                starts.append(i + 1)
        elif depth == 0 and (typ, s) == ("OP", ";"):
            starts.append(i + 1)
    return [s for s in starts if s < len(toks)]


def _declined(translated: str) -> bool | None:
    """True when the port declines a statement (`_definition` at a line's
    start, a type alias at any statement's) before any statement it
    refuses, False when it refuses first, None when no statement is one.
    Where the declined statement starts, the module up to it with `pass`
    in its place must parse."""
    try:
        toks = _c_tokens(translated)
    except _Unjudged:
        return None
    if toks is None:
        return None
    lines = translated.split("\n")
    cur: list[tuple[str, str]] = []
    where: list[tuple[int, int]] = []
    for t in toks:
        name = "OP" if t.type in _OPS else tokenize.tok_name[t.type]
        if name in ("NEWLINE", "ENDMARKER"):
            if cur:
                found = 0 if _definition(cur) else None
                if found is None:
                    found = next(
                        (s for s in _starts(cur) if _type_alias(cur[s:])), None
                    )
                if found is not None:
                    row, col = where[found]
                    prefix = "\n".join(lines[: row - 1] + [lines[row - 1][:col]])
                    return _parse(prefix + "pass\n")[0] == "ok"
            cur, where = [], []
        elif name not in ("INDENT", "DEDENT", "NL", "COMMENT"):
            cur.append((name, t.string))
            where.append(t.start)
    return None


def _answer(text: str) -> tuple[str, str, Any]:
    """``(want, parse, tree)``: what the port answers (``ok``, ``E`` or
    ``D``), the class ``ast.parse`` raises or ``ok``, and its tree."""
    parse, tree = _parse(text)
    d = _declined(text.replace("\r\n", "\n").replace("\r", "\n"))
    if d is not None:
        return ("D" if d else "E"), parse, None
    return ("ok" if tree is not None else "E"), parse, tree


def _tokenizer_refuses(text: str) -> bool:
    try:
        return _c_tokens(text.replace("\r\n", "\n").replace("\r", "\n")) is None
    except _Unjudged:
        return True


def _line(text: str) -> str:
    want, parse, tree = _answer(text)
    if want == "E":
        # The port reads every token first: where the tokenizer refuses, a
        # parser's error the reference raises sooner is another subclass
        # of SyntaxError (a stated difference), so the family is compared.
        if _tokenizer_refuses(text) and parse in ("IndentationError", "TabError"):
            return "E SyntaxError"
        return f"E {parse}"
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


def _nested(levels: int, tail: str) -> str:
    """`if x:` nested ``levels`` deep, one space a level, then ``tail``
    at the innermost level."""
    return "".join(" " * k + "if x:\n" for k in range(levels)) + " " * levels + tail


#: Each form and its near misses.
_FORMS = [
    # `if`, `elif` and `else`.
    "if x: pass", "if x:\n    pass\n", "if x: pass\nelse: pass", "if x:\n    a\nelse:\n    b\n",
    "if x: a\nelif y: b\nelif z: c\nelse: d", "if x:\n    a\nelif y:\n    b\nelse:\n    c\n",
    "if x: a; b;", "if x: a; b", "if x: pass\ny = 1", "if x:\n    a\n    b\nc\n",
    "if x:\n    if y:\n        a\n    else:\n        b\nelse:\n    c\n",
    "if a:\n if b:\n  pass\n else: pass\nelse: pass\n", "if x:  # c\n    pass\n",
    "if x:\n\n    # c\n    pass\n\n", "if x:\n\tpass\n", "if x := 1: pass", "if (yield): pass",
    "if lambda: x: pass", "if x if y else z: pass", "if not x: pass", "if x\n    pass\n",
    "if x pass", "if x:", "if x:\n", "if x:\npass\n", "if x:\n\n# c\n", "if : pass",
    "if x:: pass", "if *a: pass", "if a, b: pass", "if yield: pass", "if x: ; pass",
    "if x: pass\nelse: pass\nelse: pass", "if x: pass\nelse: pass\nelif y: pass",
    "else: pass", "elif x: pass", "if x: pass\nelse pass", "if x: pass\nelse\n    pass",
    "if x: pass\nelse:\n", "if x: pass\nelse:\npass", "if x: pass\nelif y\n",
    "if x: pass\nelif y:\n", "if x: pass\nelif y:\npass", "if x: pass\n  elif y: pass",
    "if x:\n  pass\n elif y: pass", "if x:\n  pass\n    pass", "if x:\n    pass\n  pass",
    "if x:\n\tpass\n        pass", "if x:\n        pass\n\tpass", "if x:\n    pass\n\tpass",
    "if x: if y: pass", "if x: pass; if y: pass", "x; if y: pass", "if x: pass\n    y",
    "if x:\n    pass\nelse:\n    pass\n    y\nz", "if x: else: pass", "if x: elif y: pass",
    "if x:\n    pass\n  else: pass\n", "if x: pass\nelse: if y: pass",
    "if x: return\nelif y: yield z\nelse: del a", "if x: import a.b as c\nelse: from . import d",
    "if x: global a, b", "if x: a = b = c", "if x: a: int = 1", "if x: a += 1",
    "if x: f() = 1", "if x: x $", "if x:\n    x = 1\n  y\n", "if x:\n    pass\n",
    "if x: pass\n\n\nelse: pass\n", "if x: pass\n# c\nelse: pass\n",
    # `while`.
    "while x: pass", "while x:\n    a\nelse:\n    b\n", "while x: a\nelse: b", "while x := f(): pass",
    "while x\n", "while x:\n", "while x: pass\nelse: pass\nelse: pass",
    "while x: pass\nelif y: pass", "while : pass", "while *a: pass", "while x: break; continue",
    # `for`.
    "for x in y: pass", "for x in y:\n    a\nelse:\n    b\n", "for x, y in z: pass",
    "for x, in y: pass", "for (x, y), z in w: pass", "for [x, *y] in z: pass",
    "for *x, y in z: pass", "for *x in y: pass", "for *x, *y in z: pass", "for (x) in y: pass",
    "for ((x)) in y: pass", "for x.y.z in w: pass", "for 'a'.b in c: pass",
    "for x[0] in y: pass", "for x[a:b] in c: pass", "for x[*a] in b: pass", "for f().x in y: pass",
    "for (a)(b).c in d: pass", "for () in x: pass", "for [] in x: pass", "for *(a, b) in c: pass",
    "for *[a] in b: pass", "for (yield).a in b: pass", "for x in *a, b: pass", "for x in *a: pass",
    "for x in a, : pass", "for x in a, b, : pass", "for x in y if z else w: pass",
    "for x in lambda: y: pass", "for x in y in z: pass", "for x, y in z, w: pass",
    "for x in await y: pass", "for x in -a: pass", "for x in not a: pass", "for x in (yield): pass",
    "for x in y\n", "for x in y:\n", "for x in : pass", "for in y: pass", "for x y: pass",
    "for x: pass", "for x in y pass", "for f() in y: pass", "for x() in y: pass",
    "for x[0]() in y: pass", "for (*a) in b: pass", "for None in b: pass", "for x in yield: pass",
    "for x in y := 1: pass", "for (x in y) in z: pass", "for await x in y: pass",
    "for (x for x in y) in z: pass", "for [x for x in y] in z: pass", "for a + b in c: pass",
    "for -a in b: pass", "for a if b else c in d: pass", "for lambda: x in y: pass",
    "for (a := b) in c: pass", "for **a in b: pass", "for x,, in y: pass", "for x in y, *: pass",
    "for a in b: pass\nelif c: pass", "for x in y: pass\nelse: pass", "for x.y() .z in w: pass",
    "for 1 in x: pass", "for 'a' in x: pass", "for {a} in x: pass", "for a.b(c) in d: pass",
    "async for x in y: pass", "async for x in y:\n    a\nelse:\n    b\n", "async for f() in y: pass",
    "async for x in y\n", "async x", "async if x: pass", "async while x: pass", "async = 1",
    "async\nfor x in y: pass",
    # `with`.
    "with a: pass", "with a as b: pass", "with a as b, c as d: pass", "with a, b: pass",
    "with (a, b): pass", "with (a, b) as c: pass", "with (a as b, c as d,): pass",
    "with (yield): pass", "with (a): pass", "with (a,): pass", "with (): pass",
    "with a as b.c(): pass", "with a as (b, c): pass", "with (a as b): pass",
    "with (a as b) as c: pass", "with (a, b as c): pass", "with (a as b, c): pass",
    "with (a as b)\n: pass", "with a as *b: pass", "with a as [b]: pass", "with a as f(): pass",
    "with a as b c: pass", "with (a as b).c: pass", "with (a, b).c: pass",
    "with (a as b, c) + 1: pass", "with *a: pass", "with (*a, b): pass", "with a,: pass",
    "with (a, *b) as c: pass", "with a\n", "with a:\n", "with (a)\n", "with (a, b)\n",
    "with (a as b)\n", "with (a as b, c as d):\n", "with a as b:\n", "with : pass",
    "with a as : pass", "with a as b as c: pass", "with a, : pass", "with (a, ): pass",
    "with (a as b,): pass", "with ((a, b)): pass", "with (a), (b): pass", "with (a) as b, (c): pass",
    "with (a, b) as (c, d): pass", "with a as (b): pass", "with a as (b,): pass",
    "with a as b.c: pass", "with a as b[0]: pass", "with a as None: pass", "with a as b + c: pass",
    "with a as b if c else d: pass", "with (a as b + c): pass", "with (a as b.c()): pass",
    "with a as b.c(), d: pass", "with (a := 1): pass", "with a := 1: pass",
    "with (a := 1, b := 2): pass", "with (a, b := 2): pass", "with yield: pass",
    "with (yield x): pass", "with (yield x) as y: pass", "with (yield), a: pass",
    "with (a, yield): pass", "with lambda: x: pass", "with (lambda: x): pass", "with (*a): pass",
    "with (*a, b) as c: pass", "with (a, *b): pass", "with (a for a in b): pass",
    "with (a for a in b), c: pass", "with (a, b for b in c): pass", "with a, b as c, d: pass",
    "with (a), b: pass", "with (a): pass\nelse: pass", "with (a) (b): pass", "with (a)(b): pass",
    "with (a).b as c: pass", "with (a, b)[0]: pass", "with (a,) as b: pass",
    "with (a as b), c: pass", "with (a, b) as c, (d, e): pass", "with (a,\n b): pass",
    "with (\n a as b,\n c as d,\n): pass", "with a as *b, c: pass", "with (a as *b): pass",
    "with a as [*b]: pass", "with a as b, *c: pass", "with a as (*b): pass", "with a as b\n",
    "with a as b:\n    c\n", "with a as b.c[d]: pass", "with a as b[c].d(): pass",
    "with (a as b) (c): pass", "with ((a) as b): pass", "with (a as b c): pass",
    "with (a as b,) as c: pass", "with ((a), b): pass", "with ((a), b) as c: pass",
    "async with a: pass", "async with (a, b): pass", "async with a as b, c: pass",
    "async with a\n", "async with (a as b): pass\n",
    # Compound statements in blocks, and the statements this unit declines
    # there.
    "if x:\n    while y:\n        for z in w:\n            with v as u:\n                pass\n",
    "for x in y:\n    if x: break\n    else: continue\nelse:\n    pass\n",
    "while x:\n    with a as b, c:\n        d\n    e\n",
    "if x:\n    def f(): pass\n", "if x:\n    class C: pass\n", "if x:\n    try:\n        pass\n    finally:\n        pass\n",
    "if x:\n    @d\n    def f(): pass\n", "if x:\n    async def f(): pass\n",
    "if x:\n    match y:\n        case 1: pass\n", "if x:\n    type X = int\n",
    "if x: type X = int", "if x: y; type X = int", "if x: def f(): pass", "if x: class C: pass",
    "if x: match y:", "if x: try: pass", "for x in y:\n    pass\ndef f(): pass\n",
    "if x $:\n    def f(): pass\n", "if x:\n    y $\n    def f(): pass\n",
    "if x:\ndef f(): pass\n", "if x:\n    def f(): pass\n  y\n", "if x: pass\nelse:\n    @d\n    class C: pass\n",
    "with a:\n    match(x)\n    f() = 1\n", "if x: match(*a)", "if x:\n    match(*a)\n",
    "if x:\n    match = 1\n    match.y: int = 2\n", "if x: case = 1",
    # Ten levels, and a block's statements of every kind.
    _nested(10, "pass\n"), _nested(10, "x = 1\n" + " " * 9 + "y\n"),
    "if x:\n    a = 1\n    b: int\n    c += 1\n    del d\n    return e\n    raise f\n    assert g\n"
    "    global h\n    nonlocal i\n    import j\n    from k import l\n    yield m\n    pass\n",
]  # fmt: skip

#: Names past ASCII in each slot: NFKC'd, a constant's NFKC a ValueError,
#: including where only the second pass reads it.
_NAMES = [
    "if \uff58: \uff59 = 1", "while \uff58: pass", "for \uff58 in \uff59: pass",
    "for \uff58.\uff59 in z: pass", "with \uff58 as \uff59: pass", "with (\uff58 as \uff59): pass",
    "if x:\n    \uff58 = 1\nelif \uff59: pass\nelse: \uff5a = 2\n",
    f"if {_NONE}: pass", f"if x: pass\nelif {_NONE}: pass", f"while {_NONE}: pass",
    f"for {_NONE} in x: pass", f"for x in {_NONE}: pass", f"for x.{_NONE} in y: pass",
    f"for *{_NONE} in x: pass", f"for f({_NONE}) in x: pass", f"for -{_NONE} in x: pass",
    f"for f() in {_NONE}: pass", f"for x in -{_NONE}: pass", f"for x, -{_NONE} in y: pass",
    f"async for -{_NONE} in x: pass", f"with {_NONE}: pass", f"with a as {_NONE}: pass",
    f"with a as *{_NONE}: pass", f"with (a, {_NONE}): pass", f"with a as b + {_NONE}: pass",
    f"with (a as b + {_NONE}): pass", f"with a as b.c({_NONE}): pass",
    f"with (a as b + {_NONE}), c: pass", f"with (a) + {_NONE}: pass",
    f"with a as (b, -{_NONE}): pass", f"if x: {_NONE} = 1", f"if x:\n    {_NONE} = 1\n",
    f"if x: pass\nelse: {_NONE} = 1", f"if x\n{_NONE}\n", f"for x in y\nf() = {_NONE}\n",
    f"if x:\nf() = {_NONE}\n", f"with a as b c: {_NONE}\n", f"if x: f() = {_NONE}",
    f"if x:\n    f() = {_NONE}\n",
    # The second pass reads a block's `match` subject (an indented line's),
    # not a head's line's.
    f"if x: match(*a)\nf() = {_NONE}\n", f"if x:\n    match(*a)\nf() = {_NONE}\n",
    f"if x:\n    match(*a)\n    f() = {_NONE}\n", f"match(*a)\nif x: f() = {_NONE}\n",
    f"match(x)\nfor -{_NONE} in y: pass\n", f"match(x)\nwith a as b + {_NONE}: pass\n",
]  # fmt: skip

#: Slots of compound statements the port counts exactly: (head, tail),
#: the `-` chain between.
_CHAINS = [
    ("if ", "x: pass\n"), ("while ", "x: pass\n"), ("if x: pass\nelif ", "x: pass\n"),
    ("if x: pass\nelif y: pass\nelif ", "x: pass\n"),
    ("if x: pass\nelif y: pass\nelif y: pass\nelif ", "x: pass\n"),
    ("for x in ", "x: pass\n"), ("for x in y, ", "x: pass\n"), ("for x in *", "x: pass\n"),
    ("for x in y, *", "x: pass\n"), ("async for x in ", "x: pass\n"),
    ("if x: ", "x\n"), ("if x: y; ", "x\n"), ("if x:\n    ", "x\n"), ("if x:\n    y\n    ", "x\n"),
    ("if x:\n if x:\n  ", "x\n"), ("if x:\n if x:\n  if x:\n   ", "x\n"),
    ("if x:\n if ", "x: pass\n"), ("if x:\n while ", "x: pass\n"), ("if x:\n for y in ", "x: pass\n"),
    ("if x:\n if y: pass\n elif ", "x: pass\n"), ("if x:\n if y: ", "x\n"),
    ("if x: pass\nelse:\n if ", "x: pass\n"), ("if x: pass\nelse: ", "x\n"),
    ("if x: pass\nelse:\n    ", "x\n"), ("if x: pass\nelif y: ", "x\n"),
    ("if x: pass\nelif y: pass\nelif z: ", "x\n"), ("if x: pass\nelif y: pass\nelse: ", "x\n"),
    ("if x: pass\nelif y:\n    ", "x\n"), ("if x: pass\nelif y: pass\nelse:\n    ", "x\n"),
    ("while x: ", "x\n"), ("while x:\n    ", "x\n"), ("while x: pass\nelse: ", "x\n"),
    ("for x in y: ", "x\n"), ("for x in y:\n    ", "x\n"), ("for x in y: pass\nelse: ", "x\n"),
    ("async for x in y: ", "x\n"), ("with ", "x: pass\n"), ("with a, ", "x: pass\n"),
    ("with a, b, ", "x: pass\n"), ("with a as b, ", "x: pass\n"), ("with (", "x): pass\n"),
    ("with (a, ", "x): pass\n"), ("with (a, b, ", "x): pass\n"),
    ("with (", "x,): pass\n"), ("with (a as b, ", "x): pass\n"), ("with (a, b) as c, ", "x: pass\n"),
    ("with x: ", "x\n"), ("with x:\n    ", "x\n"), ("with (x): ", "x\n"), ("with (a, b): ", "x\n"),
    ("async with ", "x: pass\n"), ("async with (a, ", "x): pass\n"), ("async with x: ", "x\n"),
    ("if ", f"{_NONE}: pass\n"), ("if x:\n    x = ", f"{_NONE}\n"),
    ("with a as b, ", f"{_NONE}: pass\n"),
    # The `else` without its `:`: the first pass raises, with no second pass.
    ("if ", "x: pass\nelse pass\n"), ("while ", "x: pass\nelse\n"),
    # 98 and 99 levels.
    (_nested(98, ""), "x\n"), (_nested(99, ""), "x\n"), (_nested(98, "if "), "x: pass\n"),
]  # fmt: skip

#: Modules the first pass refuses, where the second pass overflows first:
#: (head, tail).
_REFUSED = [
    ("if ", "x\n"), ("if ", "x:\n"), ("if ", "x: $\n"), ("if ", "x $: pass\n"), ("if x: ", "x $\n"),
    ("if x:\n    ", "x $\n"), ("if x:\n if x:\n  ", "x $\n"), ("while ", "x\n"), ("while ", "x:\n"),
    ("if x: pass\nelif ", "x\n"), ("if x: pass\nelif ", "x:\n"), ("for x in ", "x\n"),
    ("for x in ", "x:\n"), ("for x in y, ", "x $: pass\n"), ("for ", "x in y\n"),
    ("for ", "x in y: pass\n"), ("for x, ", "x $ in y: pass\n"), ("with ", "x\n"), ("with ", "x:\n"),
    ("with a, ", "x $: pass\n"), ("with (", "x)\n"), ("with (a, ", "x)\n"),
    ("with a as ", "x + 1: pass\n"), ("with a as b, ", "x\n"), ("if x: pass\nelse: ", "x $\n"),
    ("if x: pass\nelse:\n    ", "x $\n"), (_nested(20, ""), "x $\n"),
]  # fmt: skip

#: `elif` chains, whose statements lie a call deeper at each: (head, the
#: repeated branch, tail).
_ELIFS = [
    ("if x: pass\n", "elif x: pass\n", ""), ("if x: pass\n", "elif x: x\n", ""),
    ("if x: pass\n", "elif x:\n pass\n", ""), ("if x: pass\n", "elif x: pass\n", "else: pass\n"),
    ("if x: pass\n", "elif x: return\n", ""), ("if x: pass\n", "elif x: x = 1\n", ""),
]  # fmt: skip

#: `match` lines the first pass reads as expression statements and whose
#: subject the second pass refuses (`invalid_match_stmt`, "expected ':'",
#: or the subject's own error rules), and controls it does not refuse.
_SUBJECTS = [
    "match(x)", "match -x", "match[x]", "match(x).y", "match(x) + 1", "match((x))",
    "match(x)(y)", "match [*a]", "match(*a)", "match(*a, b)", "match(x, *y)", "match(x); y",
    "match * x", "match.x", "match = 1",
]  # fmt: skip
#: What follows such a line: a missing block after each head, and an
#: unexpected indent, both raised only once the second pass raises nothing.
_FOLLOWERS = [
    "while y:\n", "    y\n", "if y: pass\n  z\n", "for a in b:\n", "with a as b:\n",
    "if a:\n    b\nelse:\nc\n", "async for a in b:\n", "if a:\n    b\n        c\n",
]  # fmt: skip


def _earlier() -> list[str]:
    """Each subject line before each follower: at the module's level, both
    inside a block, and the follower after the block closes."""
    out = []
    for subject in _SUBJECTS:
        for follower in _FOLLOWERS:
            module = subject + "\n" + follower
            inside = "".join("    " + line + "\n" for line in module.splitlines())
            out += [module, "if q:\n" + inside, "if q:\n    " + module]
    out += ["if a: match(x)\nwhile y:\n", "print(x)\nwhile y:\n", "print.x\n    y\n"]
    return out


#: Brackets in targets and items, which the port bounds.
_BRACKETS = [
    ("for a[", "x] in y: pass\n"), ("for a, b[", "x] in y: pass\n"), ("for [a[", "x]] in y: pass\n"),
    ("for *a[", "x] in y: pass\n"), ("with a as b[", "x]: pass\n"), ("with (", "x) + 1: pass\n"),
    ("with ([", "x]): pass\n"), ("with (", "x) as y: pass\n"),
    ("if x:\n    for a[", "x] in y: pass\n"),
]  # fmt: skip


@functools.cache
def _depth_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The depth rows every host answers alike and those whose answer
    follows the C recursion limit (`_windows_differs`)."""
    rows: list[dict[str, Any]] = []
    posix: list[dict[str, Any]] = []

    def add(text: str, spelled: Any, band: bool = False) -> None:
        tree = _parse(text)[1]
        (posix if _windows_differs(tree) else rows).append(_row(text, spelled, band))

    def spell(head: str, piece: str, k: int, tail: str) -> Any:
        if len(head) > 400:
            return {
                "r": [*_compact(head)["r"], [_spelled(piece), k], [_spelled(tail), 1]]
            }
        return _runs((head, 1), (piece, k), (tail, 1))

    for head, tail in _CHAINS:

        def make(n: int, head: str = head, tail: str = tail) -> str:
            return head + "-" * n + tail

        n = _bound(make, 0, 7000, "MemoryError", _parse(make(0))[0])
        assert n is not None, (head, tail)
        for k in (n, n + 1):
            add(make(k), spell(head, "-", k, tail))
    for head, tail in _REFUSED:

        def refused(n: int, head: str = head, tail: str = tail) -> str:
            return head + "-" * n + tail

        n = _bound(refused, 0, 7000, "MemoryError", _parse(refused(0))[0])
        assert n is not None, (head, tail)
        for k, band in ((n - 60, False), (n, True), (n + 1, True)):
            add(refused(k), spell(head, "-", k, tail), band)
    for head, piece, tail in _ELIFS:

        def chain(
            n: int, head: str = head, piece: str = piece, tail: str = tail
        ) -> str:
            return head + piece * n + tail

        n = _bound(chain, 0, 7000, "MemoryError")
        assert n is not None, (head, piece, tail)
        for k, band in ((n - 60, False), (n, True), (n + 1, True)):
            add(chain(k), _runs((head, 1), (piece, k), (tail, 1)), band)
    for head, tail in _BRACKETS:

        def wrapped(n: int, head: str = head, tail: str = tail) -> str:
            return head + "-" * n + tail

        n = _bound(wrapped, 0, 7000, "MemoryError")
        assert n is not None, (head, tail)
        for k in (n - 150, n, n + 1, n + 151):
            add(wrapped(k), spell(head, "-", k, tail), k in (n, n + 1))
    # 99 levels the tokenizer opens, and the 100th it refuses.
    for levels in (99, 100):
        text = _nested(levels, "pass\n")
        add(text, _compact(text))
    # Deeper than any host's ``ast2obj`` converts: RecursionError
    # everywhere, a block's statement a level below its head.
    for head, tail in (
        ("if ", ": pass\n"), ("if x:\n    ", "\n"), ("if x: pass\nelse: ", "\n"),
        ("for x in ", ": pass\n"), ("with ", ": pass\n"), ("with a as b[", "]: pass\n"),
    ):  # fmt: skip
        text = head + "x" + "+x" * 10_500 + tail
        add(text, _runs((head + "x", 1), ("+x", 10_500), (tail, 1)))
    return rows, posix


_LEAVES = ["x", "1", "'s'", "None", "y.z", "a[0]", "f()"]
_EXPRS = [
    "-$",
    "not $",
    "$ + $",
    "$ if $ else $",
    "lambda: $",
    "f($)",
    "($, $)",
    "[$, *$]",
    "(a := $)",
    "(yield $)",
]
_TARGETS = ["a", "b.c", "d[$]", "(e)", "[f, g]", "(h, *i)", "*j", "$"]
_BLOCK_HEADS = [
    "if $:", "while $:", "for T in $:", "for T, T in $, $:", "for *T, T in *$, $:", "with $:",
    "with $ as T:", "with $, $ as T:", "with ($, $ as T):", "with ($ as T,):", "async for T in $:",
    "async with $ as T, $:", "if $ if $ else $:", "while (a := $):",
]  # fmt: skip
_SIMPLE = [
    "$", "T = $", "T += $", "T: $", "pass", "break", "return $", "del T", "import a.b as c",
    "x; T = $", "global a", "yield $", "def f(): pass", "match $:", "type T = $",
]  # fmt: skip
_EDITS = [
    ":", ";", ",", "*", "(", ")", " as ", " in ", "for ", "if ", "else", "elif ", "with ",
    "while ", "\n", "    ", "x", " ",
]  # fmt: skip


def sweep_cases(count: int) -> list[str]:
    """``count`` modules: one to three statements, each a compound head
    (`_BLOCK_HEADS`) with a block on its line or indented, nested three deep,
    with `elif` and `else` after an `if`, `while` or `for`, else a simple
    statement (`_SIMPLE`); targets from `_TARGETS` and expressions from
    `_EXPRS` two deep. One in nine has an edit inserted, one in nine a
    character deleted, one in nine a line's indentation deepened by four
    spaces and one in nine shallowed by four."""
    g = _Lcg(0x3C6E_F372_FE94_F82B)

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
                out.append(fill(_TARGETS[g.draw() % len(_TARGETS)], 1))
            else:
                out.append(ch)
        return "".join(out)

    def block(depth: int, indent: str) -> list[str]:
        lines: list[str] = []
        for _ in range(1 + g.draw() % 2):
            lines += statement(depth, indent)
        return lines

    def statement(depth: int, indent: str) -> list[str]:
        if depth == 3 or g.draw() % 2 == 0:
            return [indent + fill(_SIMPLE[g.draw() % len(_SIMPLE)], 2)]
        head = fill(_BLOCK_HEADS[g.draw() % len(_BLOCK_HEADS)], 2)
        lines = branch(head, depth, indent)
        if head.startswith(("if", "while", "for", "async for")):
            follow = g.draw() % 4
            if follow == 0 and head.startswith("if"):
                lines += branch(f"elif {expr(2)}:", depth, indent)
            if follow in (0, 1):
                lines += branch("else:", depth, indent)
        return lines

    def branch(head: str, depth: int, indent: str) -> list[str]:
        if g.draw() % 3 == 0:
            return [indent + head + " " + fill(_SIMPLE[g.draw() % len(_SIMPLE)], 2)]
        return [indent + head, *block(depth + 1, indent + "    ")]

    out = []
    for _ in range(count):
        lines = block(0, "")
        edit = g.draw() % 9
        if edit == 2:
            at = g.draw() % len(lines)
            lines[at] = "    " + lines[at]
        elif edit == 3:
            at = g.draw() % len(lines)
            if lines[at].startswith("    "):
                lines[at] = lines[at][4:]
        body = "\n".join(lines) + "\n"
        if edit == 0:
            at = g.draw() % (len(body) + 1)
            body = body[:at] + _EDITS[g.draw() % len(_EDITS)] + body[at:]
        elif edit == 1:
            at = g.draw() % len(body)
            body = body[:at] + body[at + 1 :]
        out.append(body)
    return out


def generate() -> dict[str, bytes]:
    """``pysyntax_compound.jsonl``: rows ``{"t", "parse", ...}`` and the
    sweep ``block_templates``."""
    name = "pysyntax_compound.jsonl"
    header: dict[str, Any] = {"fixture": "pysyntax_compound", "python": REPRODUCES}
    if _running() != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = _running()
        return {name: jsonl([header])}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        texts = _FORMS + _NAMES + _earlier()
        assert not any(_windows_differs(_parse(t)[1]) for t in texts)
        rows = [_text_row(t) for t in texts]
        rows += _depth_rows()[0]
        answers = [_line(t) for t in sweep_cases(20000)]
        rows.append(
            {
                "sweep": "block_templates",
                "count": len(answers),
                "sha256": hashlib.sha256("\n".join(answers).encode()).hexdigest(),
            }
        )
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}


def generate_posix() -> dict[str, bytes]:
    """``pysyntax_compound_posix.jsonl``: the depth rows whose answer
    follows the host's C recursion limit, as a host whose limit is 10,000
    gives them; elsewhere the header alone, naming the interpreter or
    limit."""
    name = "pysyntax_compound_posix.jsonl"
    header: dict[str, Any] = {
        "fixture": "pysyntax_compound_posix",
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
