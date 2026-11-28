"""Fixtures of definitions, decorators and `try`
(`pysyntax::module_expr::definition`, native/README.md, "The definitions
and `try`").

The reference is ``ast.parse(source)`` of CPython 3.13.13, as
``claims.check_claim`` (``claims.py`` 672) and ``symbols`` (``symbols.py``
444) call it, over modules of statements in blocks (`parse_module_blocks`):
`def` and `async def` with their parameters, annotations, defaults and
return annotations, `class` with its arguments, decorators, and `try` with
`except`, `except*`, `else` and `finally`. The file is gated on the Python
release: under any other interpreter the generator declines and writes the
header alone, naming the interpreter.

A case row holds what a `gen_pysyntax_compound` row holds (``t``,
``parse``, ``want``, ``band``, and for an accepted source ``all``, ``top``,
``ids`` and ``shape``), and for an accepted source ``marks``: each
statement, handler, ``arg`` and ``keyword`` in the shape's preorder with
its class, its identifier (a definition's or a handler's name, an arg's or
a keyword's) and its span, ``line:column-line:column`` with columns in code
points of the line (`_marks`). The port declines type parameters, `match`
and `type` statements as `gen_pysyntax_compound._declined` says.

The depth rows hold no tree a host converts differently: each slot's first
source the parser's stack refuses (by bisection over the interpreter,
`_CHAINS`; a class's arguments, which the port bounds, ``band``), modules
the first pass refuses near where its second pass overflows (`_REFUSED`:
the boundary pair ``band``, 60 pieces below it answered exactly, 100 in a
definition's or a class's parentheses), and sums deeper than every host's
``ast2obj`` converts.

A sweep row pins by count and SHA-256 the answers to 10,000 modules drawn
by a fixed LCG from definition templates (`sweep_cases`), one in three with
a token inserted, a character deleted or a line's indentation deepened or
shallowed (``E`` and the class ``ast.parse`` raises when refused, a
subclass of SyntaxError as SyntaxError where the tokenizer refuses; ``D``
declined; else the names, ids, shape and marks joined by ``|``).
"""

from __future__ import annotations

import ast
import hashlib
import warnings
from collections.abc import Callable
from typing import Any

from ._common import jsonl
from .gen_pysyntax import _spelled
from .gen_pysyntax_compound import _answer, _tokenizer_refuses
from .gen_pysyntax_expr import _bound, _parse, _running, _runs, _shape
from .gen_pysyntax_module import _compact
from .gen_pysyntax_stmt import _ids, _Lcg, _names, _windows_differs

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"

_NONE = "\uff2e\uff4f\uff4e\uff45"
#: The nodes `_marks` places.
_MARKED = (ast.stmt, ast.excepthandler, ast.arg, ast.keyword)


def _marks(tree: ast.AST, text: str) -> str:
    """Each statement, handler, arg and keyword in preorder: its class, its
    identifier and its span, columns in code points of the translated
    line (``col_offset`` counts UTF-8 bytes)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def at(line: int, col: int) -> str:
        return f"{line}:{len(lines[line - 1].encode()[:col].decode())}"

    out: list[str] = []
    stack: list[ast.AST] = [tree]
    while stack:
        x = stack.pop()
        if isinstance(x, _MARKED):
            name = getattr(x, "arg", None) or getattr(x, "name", None)
            ident = name if isinstance(name, str) else ""
            start = at(x.lineno, x.col_offset)
            end = at(x.end_lineno, x.end_col_offset)  # type: ignore[arg-type]
            out.append(f"{type(x).__name__} {ident} {start}-{end}")
        stack.extend(reversed(list(ast.iter_child_nodes(x))))
    return ";".join(out)


def _line(text: str) -> str:
    want, parse, tree = _answer(text)
    if want == "E":
        if _tokenizer_refuses(text) and parse in ("IndentationError", "TabError"):
            return "E SyntaxError"
        return f"E {parse}"
    if tree is None:
        return want
    names = [",".join(n) for n in _names(tree)]
    return "|".join([*names, _ids(tree), _shape(tree), _marks(tree, text)])


def _row(text: str, spelled: Any, band: bool = False) -> dict[str, Any]:
    want, parse, tree = _answer(text)
    row: dict[str, Any] = {"t": spelled, "parse": parse}
    if band:
        row["band"] = True
    if want == "D":
        row["want"] = "D"
    if tree is not None:
        assert not _windows_differs(tree), text[:80]
        shape = _shape(tree)
        row["all"], row["top"] = _names(tree)
        row["ids"] = _ids(tree)
        row["shape"] = (
            shape
            if len(shape) <= 300
            else {"n": len(shape), "h": hashlib.sha256(shape.encode()).hexdigest()}
        )
        row["marks"] = _marks(tree, text)
    return row


def _text_row(text: str) -> dict[str, Any]:
    return _row(text, _spelled(text) if len(text) <= 400 else _compact(text))


#: `def` and `async def`: every parameter form and each refused one.
_DEFS = [
    "def f(): pass", "def f():\n    pass\n", "def f(a): pass", "def f(a, b): pass",
    "def f(a,): pass", "def f(a=1): pass", "def f(a, b=1): pass", "def f(a=1, b=2): pass",
    "def f(a=1, b): pass", "def f(a, /): pass", "def f(a, /,): pass", "def f(a, /, b): pass",
    "def f(a=1, /, b=2): pass", "def f(a=1, /, b): pass", "def f(a, b=1, /, c=2): pass",
    "def f(/, a): pass", "def f(/): pass", "def f(a, /, /): pass", "def f(a, /, b, /): pass",
    "def f(*, a): pass", "def f(*, a=1, b): pass", "def f(*a): pass", "def f(*a, b, c=1): pass",
    "def f(*a,): pass", "def f(**k): pass", "def f(**k,): pass", "def f(*a, **k): pass",
    "def f(a, *, b, **k): pass", "def f(a, /, b, *c, d, e=1, **f): pass", "def f(*): pass",
    "def f(*,): pass", "def f(*, **k): pass", "def f(*a, *b): pass", "def f(*, *a): pass",
    "def f(**k, a): pass", "def f(**k, *a): pass", "def f(**k, **j): pass", "def f(**k, /): pass",
    "def f(*a, /): pass", "def f(*, a, /): pass", "def f(*a=1): pass", "def f(**k=1): pass",
    "def f(a=): pass", "def f(a:): pass", "def f(,): pass", "def f(a,,): pass", "def f(a b): pass",
    "def f(a / b): pass", "def f(a, a): pass", "def f((a)): pass", "def f(a.b): pass",
    "def f(1): pass", "def f(a: int): pass",
    "def f(a: int = 1, *b: str, c: bool = True, **d: dict) -> None: pass",
    "def f(*a: *Ts): pass", "def f(*a: *tuple[int, ...]): pass", "def f(a: *Ts): pass",
    "def f(**k: *Ts): pass", "def f(*a: *Ts = 1): pass", "def f(*, a: *Ts): pass",
    "def f(*a: *b, c): pass", "def f(*a: *b | c): pass", "def f(*a: *-b): pass",
    "def f(*a: *b, *c): pass", "def f(a: (yield)): pass", "def f(a: lambda: x): pass",
    "def f(a: x if y else z): pass", "def f(a: (b := c)): pass", "def f(a: b := c): pass",
    "def f(a=(b := c)): pass", "def f(a=b := c): pass", "def f(a=lambda x, y=1: x): pass",
    "def f(a=yield): pass", "def f(a=(yield)): pass", "def f(a=*b): pass",
    "def f(a=[x for x in y]): pass", "def f(a=f(b=1, **c), *, d=e.g): pass",
    "def f(a: int=1, /, b: int=2, *, c: int=3): pass", "def f() -> int: pass",
    "def f() -> (yield): pass", "def f() -> lambda: x: pass", "def f() -> *a: pass",
    "def f() -> a, b: pass", "def f() -> : pass", "def f() -> a := b: pass",
    "def f() -> (a := b): pass", "def f()->x:pass", "def f(): return", "def f(): x = 1; return x",
    "def f():\n    x = 1\n    return x\n", "def f(): pass\ndef g(): pass", "def f:\n", "def f: pass",
    "def f() pass", "def f()\n    pass\n", "def f():", "def f():\n", "def f():\npass\n",
    "def f():\n\n# c\n", "def (): pass", "def 1(): pass", "def if(): pass", "def f(a)(b): pass",
    "def f(a) -> b -> c: pass", "def f(): pass\n    x\n", "def f(): pass\nelse: pass",
    "def f(): pass\nelif x: pass", "def f():\n    pass\nelse:\n    pass\n",
    "def f(): pass\nexcept: pass", "def f(): pass\nfinally: pass", "def match(): pass",
    "def type(): pass", "def case(): pass", "def _(): pass",
    "def __init__(self, *args, **kwargs): pass", "def f(self, /, x): pass", "def f(): def g(): pass",
    "x; def f(): pass", "if x: def f(): pass", "def f(): if x: pass", "def f(): pass; x",
    "def f(): yield; yield from x; await y", "def f(\n    a,\n    b,\n): pass",
    "def f(a\n    pass\n", "def f(a: int\n", "def f() -> \n", "def f(*, a=1, **k):\n    return a\n",
    "async def f(): pass", "async def f(a=1, *b, **c) -> d: await x",
    "async def f():\n    async for x in y: pass\n    async with a as b: pass\n    await z\n",
    "async def f(): yield", "async f(): pass", "async def: pass", "async\ndef f(): pass",
    "async  def f(): pass", "async class C: pass", "async def f()\n", "async def f():\n",
]  # fmt: skip

#: `class`: arguments as a call's, near misses, methods and nested classes.
_CLASSES = [
    "class C: pass", "class C(): pass", "class C(a): pass", "class C(a, b): pass",
    "class C(a,): pass", "class C(a.b, c[d]): pass", "class C(*a): pass", "class C(**a): pass",
    "class C(a=1): pass", "class C(metaclass=M, x=1): pass", "class C(a, *b, c=1, **d): pass",
    "class C(a, b=1, *c, d=2, **e): pass", "class C(**a, *b): pass", "class C(a=1, b): pass",
    "class C(**a, b): pass", "class C(x for x in y): pass", "class C((x for x in y)): pass",
    "class C(a, (x for x in y)): pass", "class C(a, x for x in y): pass", "class C(a := 1): pass",
    "class C(a=b := 1): pass", "class C((a) = 1): pass", "class C(a=1, a=2): pass",
    "class C(f(x)(y)): pass", "class C(,): pass", "class C(a,,): pass", "class C(*): pass",
    "class C(**): pass", "class C(a=): pass", "class C(a)(b): pass", "class C(a).b: pass",
    "class C(a)[b]: pass", "class C(yield): pass", "class C((yield)): pass",
    "class C(lambda: x): pass", "class C(*a, *b, c=1, *d, **e, f=2): pass", "class C: x = 1",
    "class C:\n    x = 1\n    def f(self): pass\n",
    "class C:\n    class D:\n        def f(self, a=1): pass\n", "class C pass", "class C(a) pass",
    "class C:", "class C:\nx", "class: pass", "class 1: pass", "class C\n", "class C(:\n",
    "class C(a\n", "class C(a) -> b: pass", "class C: pass\nelse: pass", "class C: pass\n    x",
    "class C:\n    pass\nclass D(C): pass\n", "class C(\n    a,\n    b=1,\n): pass",
    "class C: def f(self): pass", "class C(object):\n    '''doc'''\n",
]  # fmt: skip

#: Decorators.
_DECORATORS = [
    "@d\ndef f(): pass", "@d\nclass C: pass", "@d\nasync def f(): pass", "@a\n@b\n@c\ndef f(): pass",
    "@a.b.c\ndef f(): pass", "@a(b, c=1)\ndef f(): pass", "@a[b]\ndef f(): pass", "@(a)\ndef f(): pass",
    "@a := b\ndef f(): pass", "@(a := b)\ndef f(): pass", "@lambda f: f\ndef g(): pass",
    "@a if b else c\ndef f(): pass", "@not a\ndef f(): pass", "@a + b\ndef f(): pass",
    "@-a\ndef f(): pass", "@(yield)\ndef f(): pass", "@yield\ndef f(): pass", "@*a\ndef f(): pass",
    "@a, b\ndef f(): pass", "@\ndef f(): pass", "@d\n", "@d", "@d\nx = 1", "@d\nif x: pass",
    "@d\ntry: pass\nfinally: pass", "@d\nasync for x in y: pass", "@d\n@\ndef f(): pass",
    "@d def f(): pass", "@d;\ndef f(): pass", "@d\n\n# c\ndef f(): pass", "@d  # c\ndef f(): pass",
    "@d\n    def f(): pass", "if x:\n    @d\ndef f(): pass", "if x:\n    @d\n    def f(): pass\n",
    "class C:\n    @property\n    def x(self): return 1\n    @x.setter\n    def x(self, v): pass\n",
    "@d\n\n\n\ndef f(): pass", "@a.b(c)(d)\ndef f(): pass", "@a\\\n.b\ndef f(): pass",
    "@a\n@b\nclass C(D): pass", "@d\nasync\ndef f(): pass", "@d\ndef f()\n", "@d\nclass C\n",
    "@d\n@e\n", "@d\ndef f():\nx\n", "@d\ndef f():\n",
]  # fmt: skip

#: `try` in each accepted and refused order.
_TRIES = [
    "try: pass\nexcept: pass", "try: pass\nfinally: pass", "try: pass\nexcept E: pass",
    "try: pass\nexcept E as e: pass", "try: pass\nexcept (A, B) as e: pass",
    "try: pass\nexcept E: pass\nexcept F: pass\nexcept: pass", "try: pass\nexcept: pass\nexcept E: pass",
    "try: pass\nexcept E: pass\nelse: pass", "try: pass\nexcept E: pass\nelse: pass\nfinally: pass",
    "try: pass\nexcept E: pass\nfinally: pass",
    "try:\n    a\nexcept E as e:\n    b\nelse:\n    c\nfinally:\n    d\n", "try: pass\nexcept* E: pass",
    "try: pass\nexcept* E as e: pass", "try: pass\nexcept *E: pass",
    "try: pass\nexcept* (A, B): pass\nexcept* C: pass\nelse: pass\nfinally: pass",
    "try: pass\nexcept*: pass", "try: pass\nexcept* : pass", "try: pass\nexcept E: pass\nexcept* F: pass",
    "try: pass\nexcept* E: pass\nexcept F: pass", "try: pass\nexcept* E: pass\nexcept: pass",
    "try: pass\nexcept: pass\nexcept* E: pass", "try: pass", "try:\n    pass\n", "try: pass\nelse: pass",
    "try: pass\nelse: pass\nfinally: pass", "try: pass\nfinally: pass\nexcept E: pass",
    "try: pass\nfinally: pass\nelse: pass", "try: pass\nfinally: pass\nfinally: pass",
    "try: pass\nexcept: pass\nelse: pass\nexcept E: pass", "try: pass\nexcept: pass\nelse: pass\nelse: pass",
    "try: pass\nexcept E, F: pass", "try: pass\nexcept E as e.f: pass", "try: pass\nexcept E as (e): pass",
    "try: pass\nexcept E as e, f: pass", "try: pass\nexcept E as: pass", "try: pass\nexcept yield: pass",
    "try: pass\nexcept (yield): pass", "try: pass\nexcept lambda: x: pass",
    "try: pass\nexcept a if b else c: pass", "try: pass\nexcept *a, b: pass",
    "try: pass\nexcept (a := b): pass", "try: pass\nexcept a := b: pass", "try: pass\nexcept *(a, b): pass",
    "try pass", "try\n    pass\n", "try:", "try:\n", "try:\npass\n", "try: pass\nexcept:",
    "try: pass\nexcept:\n", "try: pass\nexcept:\npass\n", "try: pass\nexcept E\n", "try: pass\nexcept E pass",
    "try: pass\nfinally pass", "try: pass\nfinally:\n", "try: pass\nexcept: pass\nelse:\n",
    "try: pass\nexcept: pass\nelse pass", "try: pass\nexcept* E:\n", "except E: pass", "except: pass",
    "finally: pass", "try: pass\n    except E: pass", "try:\n    pass\n  except E: pass",
    "try: pass\nexcept: pass\n    x", "try:\n    pass\nexcept:\n    pass\n    x\n  y\n",
    "if x:\n    try:\n        pass\n    except E:\n        pass\nelse:\n    pass\n",
    "try:\n    try: pass\n    finally: pass\nexcept: pass\n",
    "for x in y:\n    try: pass\n    except: break\n    else: continue\n    finally: pass\nelse:\n    pass\n",
    "try: pass\nexcept E: pass\nelif x: pass", "with a:\n    pass\nexcept: pass", "while x: pass\nexcept: pass",
    "try: a; b;\nexcept: c;\nelse: d;\nfinally: e;",
    "try:\n    pass\nexcept E as e: pass\nexcept F as e: pass\n", "try: try: pass", "x; try: pass",
    "try: pass\nexcept: pass; except: pass", "try: pass\nexcept E: x\nexcept F: y\nelse: z\nfinally: w\n",
    "try:\n    a\nexcept:\n    b\n\n\nfinally:\n    c\n", "try:\n    a\n# c\nexcept:\n    b\n",
]  # fmt: skip

#: Names past ASCII, and a constant's identifier in each slot, including
#: where only the second pass reads it and where a line before has the
#: second pass raise first.
_NAMES = [
    "def \uff46(\uff41, \uff42=1, *\uff43, \uff44, **\uff45): pass", "class \uff23(\uff42, \uff4d=1): pass",
    "try: pass\nexcept \uff25 as \uff45: pass", "@\uff44\ndef \uff46(): pass",
    "def f(\uff41: \uff49\uff4e\uff54) -> \uff52: pass", "def \ufb01(): pass", "class \u212b: pass",
    f"def {_NONE}(): pass", f"async def {_NONE}(): pass", f"class {_NONE}: pass", f"def f({_NONE}): pass",
    f"def f(a, /, {_NONE}): pass", f"def f({_NONE}, /): pass", f"def f(*{_NONE}): pass",
    f"def f(*, {_NONE}): pass", f"def f(**{_NONE}): pass", f"def f(a: {_NONE}): pass",
    f"def f(a={_NONE}): pass", f"def f(*a: *{_NONE}): pass", f"def f(*, a={_NONE}): pass",
    f"def f() -> {_NONE}: pass", f"@{_NONE}\ndef f(): pass", f"@d\nclass {_NONE}: pass",
    f"class C({_NONE}): pass", f"class C({_NONE}=1): pass", f"class C(a={_NONE}): pass",
    f"class C(**{_NONE}): pass", f"class C(*{_NONE}): pass", f"try: pass\nexcept {_NONE}: pass",
    f"try: pass\nexcept E as {_NONE}: pass", f"try: pass\nexcept* {_NONE}: pass",
    f"def f(): {_NONE} = 1", f"def f():\n    {_NONE} = 1\n", f"try: {_NONE} = 1\nfinally: pass",
    f"def f(**k, {_NONE}): pass", f"def f(**k, a={_NONE}): pass", f"def f(a=1, b, {_NONE}): pass",
    f"def f(a=1, {_NONE}): pass", f"class C(a=1, x + {_NONE}): pass", f"class C(a=1, {_NONE}): pass",
    f"class C(**a, *{_NONE}): pass", f"try: pass\nexcept E: pass\nexcept* {_NONE}: pass",
    f"try: pass\nexcept* E: pass\nexcept {_NONE}: pass", f"def f(a=1, b): pass\n{_NONE}",
    f"def f(): pass\nf() = {_NONE}\n", f"def f():\n    f() = {_NONE}\n",
    f"match(x)\ndef f(**k, {_NONE}): pass", f"match(x)\nclass C(a=1, x + {_NONE}): pass",
    f"match(x)\ntry: pass\nexcept E: pass\nexcept* {_NONE}: pass", f"match(x)\ndef f({_NONE}): pass",
    f"def f():\n    match(x)\nclass C(a=1, x + {_NONE}): pass",
    f"def f({_NONE}=1): pass\nx $", f"def f(a, {_NONE}): pass\nx $",
]  # fmt: skip

#: Lines and columns: CRLF and CR, tabs, backslash continuations, form
#: feeds, code points past ASCII before a name, `;` closing a block.
_PLACES = [
    "def f(a,\n      b): pass\n", "def f(a, \\\n b): pass", "if x:\n\tdef f(\n\t\ta): pass\n",
    "x = 1\r\ndef f():\r\n    return x\r\n", "x = 1\rdef f(): pass\r", "\u00e9 = 1; \u00fc = 2\ndef \u0192(\u00e4, \u00f6=\u00fc): pass\n",
    "def f(): pass  # c\n", "def f():\n    pass\n\n\n# c\n", "class C:\n    def f(self):\n        pass\n    x = 1\n",
    "\x0cdef f(): pass", "try:\n    a\nexcept E as e:\n    b;\n",
    "@a\n@b\nclass C(d, e=f):\n    def g(self, h: int = 1, *i, j, **k) -> l:\n        try:\n            m\n"
    "        except* N as o:\n            p\n        else:\n            q\n        finally:\n            r\n",
    "def f(): a;\ndef g(): b; c;", "def f(a = 1 , * , b : int = 2 ) -> x : pass",
    "class C(\u00e4, \u00f6=\u00fc): pass", "\u00e4 = [\n    1,\n]\ndef f(): pass\n",
    "def f():\n    '''\n    doc\n    '''\n    return 1\n", "x = '\u00e9'; def_ = 1\nclass C: pass\n",
    "if x:\n    def f(): pass\nelse:\n    class C: pass\n", "try:\n\tpass\nexcept:\n\tpass\n",
    "def f(a=lambda b, *c, d=1, **e: f(g=h, **i)): pass", "async def f(\\\n): pass",
    "class C(metaclass=M,\n        **kw): pass", "def f(\n    *,\n    a: int,\n) -> None:\n    ...\n",
]  # fmt: skip

#: Type parameters, `match` and `type` statements, declined after what the
#: port reads before them, inside a `try` block with no clause yet
#: included, or refused where a statement before them is.
_DECLINED = [
    "def f[T](): pass", "async def f[T](): pass", "class C[T]: pass", "class C[T](a): pass",
    "@d\ndef f[T](): pass", "if x:\n    def f[T](): pass\n", "def f(): pass\ndef g[T](): pass",
    f"def {_NONE}[T](): pass", f"@{_NONE}\ndef f[T](): pass", "def f(): x $\ndef g[T](): pass",
    "def f[](): pass", "class C[T]: type X = int", "def f(): type X = int",
    "def f():\n    match x:\n        case 1: pass\n", "class C:\n    type X = int\n",
    "try: pass\nexcept:\n    match x:\n        case 1: pass\n", "try: pass\nexcept: type X = int",
    "def f(): match x:", "@d\n@e\nclass C[T]: pass", "def f[T: int, *Ts, **P](a: T) -> T: pass",
    "try:\n    def f[T](): pass\nexcept: pass\n", "try:\n    match x:\n        case 1: pass\nexcept: pass\n",
    "try:\n    class C[T]: pass\nfinally: pass\n", "if x:\n    try:\n        type X = int\n    except: pass\n",
    "try: type X = int\nexcept: pass", "try:\n    if x:\n        match y:\n            case 1: pass\nexcept: pass\n",
    "try:\n    try:\n        def f[T](): pass\n    except: pass\nexcept* E: pass\n",
    "try:\n\t@d\n\tclass C[T]: pass\nfinally: pass\n", "try:\n    pass\nfinally:\n    def f[T](): pass\n",
    "try:\n    pass\nx = 1\ndef f[T](): pass\n", "try:\n    try:\n        pass\n    def f[T](): pass\nexcept: pass\n",
    "try:\n    x $\n    def f[T](): pass\nexcept: pass\n",
]  # fmt: skip

#: A line before that the second pass refuses (or does not), then a
#: definition's missing block or unexpected indent, or a `try` it refuses.
_SUBJECTS = ["match(x)", "match -x", "match(*a)", "match.x", "match = 1"]
_FOLLOWERS = [
    "def f():\n", "class C:\nx\n", "@d\n    def f(): pass\n", "try:\n", "try: pass\nexcept:\n",
    "try: pass\n", "def f(): pass\n    y\n", "try: pass\nfinally:\npass\n",
]  # fmt: skip


def _earlier() -> list[str]:
    return [s + "\n" + f for s in _SUBJECTS for f in _FOLLOWERS]


#: Slots whose offset the port holds exactly: (head, tail), a `-` chain
#: between; the first source the parser's stack refuses.
_CHAINS = [
    ("@", "x\ndef f(): pass\n"), ("@a\n@", "x\ndef f(): pass\n"), ("@", "x\nclass C: pass\n"),
    ("@", "x\nasync def f(): pass\n"), ("def f(a=", "x): pass\n"), ("def f(a, b=", "x): pass\n"),
    ("def f(a, /, b=", "x): pass\n"), ("def f(a=1, /, b=", "x): pass\n"), ("def f(*, a=", "x): pass\n"),
    ("def f(a, /, *, b=", "x): pass\n"), ("def f(a: ", "x): pass\n"), ("def f(a, /, b: ", "x): pass\n"),
    ("def f(*a: ", "x): pass\n"), ("def f(*a: *", "x): pass\n"), ("def f(**a: ", "x): pass\n"),
    ("def f(*, a: ", "x): pass\n"), ("def f() -> ", "x: pass\n"), ("async def f(a=", "x): pass\n"),
    ("def f(): ", "x\n"), ("def f():\n    ", "x\n"), ("class C: ", "x\n"), ("class C:\n    ", "x\n"),
    ("try: ", "x\nfinally: pass\n"), ("try:\n    ", "x\nexcept: pass\n"), ("try: pass\nexcept ", "x: pass\n"),
    ("try: pass\nexcept* ", "x: pass\n"),
    ("try: pass\nexcept: ", "x\n"), ("try: pass\nexcept E as e:\n    ", "x\n"),
    ("try: pass\nexcept: pass\nelse: ", "x\n"), ("try: pass\nexcept: pass\nexcept: pass\nelse: ", "x\n"),
    ("try: pass\nfinally: ", "x\n"), ("try: pass\nexcept: pass\nelse: pass\nfinally:\n    ", "x\n"),
    ("def f():\n def g():\n  ", "x\n"), ("class C:\n def f(a=", "x): pass\n"),
    ("if x:\n try:\n  pass\n except ", "x: pass\n"), ("x = ", "x\ntry pass\n"),
    ("try: ", "x\nfinally pass\n"), ("try: pass\nexcept: ", "x\nelse pass\n"),
]  # fmt: skip

#: A class's arguments, which the port bounds: the first refused source
#: and the last accepted one are ``band``.
_ARGUMENTS = [
    ("class C(", "x): pass\n"), ("class C(a, ", "x): pass\n"), ("class C(a=", "x): pass\n"),
    ("class C(a, b=1, *", "x): pass\n"), ("class C(a=1, **", "x): pass\n"), ("class C(**", "x): pass\n"),
]  # fmt: skip

#: Modules the first pass refuses, where the second pass overflows first
#: (a handler of the other kind's type is read by `invalid_try_stmt`
#: alone), and how far below that boundary the port answers exactly: 60
#: pieces, 100 inside a definition's or a class's parentheses, which
#: `Parser::reach` counts as a bracket's.
_REFUSED = [
    ("def f(a=", "x) pass\n", 100), ("def f(a=", "x): $\n", 100), ("@", "x\ndef f: pass\n", 60),
    ("def f(a=", "x, b): pass\n", 100), ("class C(", "x) pass\n", 100),
    ("try: ", "x\nexcept pass\n", 60), ("try: pass\nexcept ", "x pass\n", 60),
    ("x = ", "x\ndef f: pass\n", 60), ("def f(): ", "x $\n", 60), ("try: ", "x\n", 60),
    ("try: pass\nexcept E: pass\nexcept* ", "x: pass\n", 60),
    ("try: pass\nexcept* E: pass\nexcept ", "x: pass\n", 60),
]  # fmt: skip


def _depth_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(text: str, spelled: Any, band: bool = False) -> None:
        assert not _windows_differs(_parse(text)[1]), spelled
        rows.append(_row(text, spelled, band))

    def chain(head: str, tail: str) -> Callable[[int], str]:
        return lambda k: head + "-" * k + tail

    for head, tail in _CHAINS:
        n = _bound(chain(head, tail), 0, 7000, "MemoryError", _parse(head + tail)[0])
        assert n is not None, (head, tail)
        add(head + "-" * (n + 1) + tail, _runs((head, 1), ("-", n + 1), (tail, 1)))
    for head, tail in _ARGUMENTS:
        n = _bound(chain(head, tail), 0, 7000, "MemoryError")
        assert n is not None, (head, tail)
        add(
            head + "-" * (n + 1) + tail, _runs((head, 1), ("-", n + 1), (tail, 1)), True
        )
    for head, tail, below in _REFUSED:
        n = _bound(chain(head, tail), 0, 7000, "MemoryError", _parse(head + tail)[0])
        assert n is not None, (head, tail)
        for k, band in ((n - below, False), (n, True), (n + 1, True)):
            add(head + "-" * k + tail, _runs((head, 1), ("-", k), (tail, 1)), band)
    # Deeper than any host's ``ast2obj`` converts: RecursionError
    # everywhere.
    for head, tail in (
        ("def f(a=", "): pass\n"), ("@", "\ndef f(): pass\n"), ("class C(", "): pass\n"),
        ("try: pass\nexcept ", ": pass\n"), ("def f():\n    ", "\n"), ("def f() -> ", ": pass\n"),
    ):  # fmt: skip
        text = head + "x" + "+x" * 10_500 + tail
        add(text, _runs((head + "x", 1), ("+x", 10_500), (tail, 1)))
    return rows


_PARAMS = [
    "", "a", "a, b", "a=$", "a, b=$", "a: $", "a: $ = $", "a, /, b", "a=$, /, b=$", "*a", "*a: *$",
    "*, a", "*, a=$, b", "**k", "a, *b, c, **d", "*a, **k: $", "a=$, b", "*", "**k, a", "a, /",
]  # fmt: skip
_ARGLISTS = [
    "",
    "a",
    "a, b",
    "*a",
    "a=$",
    "a, b=$, **c",
    "**a",
    "a=$, b",
    "x for x in y",
    "a := $",
]
_LEAVES = ["x", "1", "'s'", "None", "y.z", "a[0]", "f()"]
_EXPRS = [
    "-$",
    "not $",
    "$ + $",
    "$ if $ else $",
    "lambda: $",
    "f($)",
    "($, $)",
    "(a := $)",
]
_SIMPLE = [
    "$",
    "x = $",
    "pass",
    "return $",
    "yield $",
    "raise $",
    "del x",
    "import a as b",
]
_EDITS = [
    ":", ";", ",", "*", "**", "/", "(", ")", "=", "->", "@", " as ", "def ", "class ", "try",
    "except", "except*", "else", "finally", "\n", "    ", "x", " ",
]  # fmt: skip


def sweep_cases(count: int) -> list[str]:
    """``count`` modules: one to three statements, each a definition
    (decorators, then `def`, `async def` or `class` with parameters or
    arguments from `_PARAMS` and `_ARGLISTS`), a `try` (one or two
    handlers of one kind, `else`, `finally`), or a simple statement, with
    blocks on the head's line or indented, nested three deep. One in
    twelve has an edit inserted (`_EDITS`), one in twelve a character
    deleted, one in twelve a line deepened by four spaces and one in
    twelve shallowed by four."""
    g = _Lcg(0x6A09_E667_F3BC_C908)

    def pick(items: list[str]) -> str:
        return items[g.draw() % len(items)]

    def expr(depth: int) -> str:
        if depth == 0 or g.draw() % 3 == 0:
            return pick(_LEAVES)
        return fill(pick(_EXPRS), depth - 1)

    def fill(template: str, depth: int) -> str:
        return "".join(expr(depth) if ch == "$" else ch for ch in template)

    def block(depth: int, indent: str) -> list[str]:
        lines: list[str] = []
        for _ in range(1 + g.draw() % 2):
            lines += statement(depth, indent)
        return lines

    def branch(head: str, depth: int, indent: str) -> list[str]:
        if depth == 3 or g.draw() % 3 == 0:
            return [indent + head + " " + fill(pick(_SIMPLE), 2)]
        return [indent + head, *block(depth + 1, indent + "    ")]

    def statement(depth: int, indent: str) -> list[str]:
        kind = g.draw() % 5
        if depth == 3 or kind == 0:
            return [indent + fill(pick(_SIMPLE), 2)]
        if kind == 4:
            star = "*" if g.draw() % 4 == 0 else ""
            lines = branch("try:", depth, indent)
            handlers = g.draw() % 3
            for _ in range(handlers):
                bare = g.draw() % 3 == 0
                typ = "" if bare and not star else " " + expr(1)
                alias = g.draw() % 2 == 0
                lines += branch(
                    f"except{star}{typ}{' as e' if typ and alias else ''}:",
                    depth,
                    indent,
                )
            els, fin = g.draw() % 2 == 0, g.draw() % 2 == 0
            if handlers and els:
                lines += branch("else:", depth, indent)
            if not handlers or fin:
                lines += branch("finally:", depth, indent)
            return lines
        decorated = g.draw() % 3 == 0
        lines = [indent + "@" + expr(2)] if decorated else []
        if kind == 3:
            args, bare = pick(_ARGLISTS), g.draw() % 2 == 0
            head = "class C:" if not args and bare else f"class C({fill(args, 1)}):"
        else:
            returns = f" -> {expr(1)}" if g.draw() % 3 == 0 else ""
            asynced = "async " if kind == 2 else ""
            head = f"{asynced}def f({fill(pick(_PARAMS), 1)}){returns}:"
        return lines + branch(head, depth, indent)

    out = []
    for _ in range(count):
        lines = block(0, "")
        edit = g.draw() % 12
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
            body = body[:at] + pick(_EDITS) + body[at:]
        elif edit == 1:
            at = g.draw() % len(body)
            body = body[:at] + body[at + 1 :]
        out.append(body)
    return out


def generate() -> dict[str, bytes]:
    """``pysyntax_definition.jsonl``: rows ``{"t", "parse", ...}`` and the
    sweep ``definition_templates``."""
    name = "pysyntax_definition.jsonl"
    header: dict[str, Any] = {"fixture": "pysyntax_definition", "python": REPRODUCES}
    if _running() != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = _running()
        return {name: jsonl([header])}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        texts = _DEFS + _CLASSES + _DECORATORS + _TRIES + _NAMES + _PLACES + _DECLINED
        rows = [_text_row(t) for t in texts + _earlier()]
        rows += _depth_rows()
        answers = [_line(t) for t in sweep_cases(10_000)]
        rows.append(
            {
                "sweep": "definition_templates",
                "count": len(answers),
                "sha256": hashlib.sha256("\n".join(answers).encode()).hexdigest(),
            }
        )
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}
