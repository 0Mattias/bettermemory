"""Fixtures of the module-mode reader (`pysyntax::module_lex`,
native/README.md, "Python source as CPython reads it").

The reference is CPython 3.13.13's tokenizer in module mode, as
``ast.parse(source)`` in ``claims.check_claim`` (``claims.py`` 672) meets
it. The file ``pysyntax_module.jsonl`` is gated on the Python release: the
tokenizer's rules and the messages a failure is classed by are 3.13.13's,
and under any other interpreter the generator declines and writes the
header alone, naming the interpreter.

The first line is the header. A case row holds ``t``, the source, spelled
as `gen_pysyntax` spells texts; ``lex``, true when the tokenizer refuses
it; ``parse``, the class of what ``ast.parse`` raises or ``ok``; and
``tok``, the tokens ``tokenize`` reads when ``lex`` is false:
``[type, start, end]`` for a token with text (offsets in code points of
the source with its line ends translated; an f-string's FSTRING_START to
FSTRING_END as one ``FSTRING``) and ``[type]`` for NEWLINE, NL, INDENT,
DEDENT and ENDMARKER; past 400 code points, ``{"n", "h"}``: the count of
tokens and the SHA-256 of their spelling by `_spelled_tokens`.

The tokenizer refuses a source when the C tokenizer in the parser's mode
(``tokenize._generate_tokens_from_c_tokenizer`` with ``extra_tokens``
false, over the source with ``\r\n`` and ``\r`` translated as
``ast.parse`` translates them) raises, or when one of its STRING or
NUMBER tokens alone does not parse (the literal decoders and the digit
limit, which the parser applies). ``ast.parse`` alone would not tell:
it stops at the first grammar error, before the tokenizer reaches a
later one. A SyntaxError of ``ast.parse`` whose message is one of
`_LEX_MESSAGES` is asserted to be a refusal.

A sweep row pins 20,000 sources of one to twelve pieces drawn by a fixed
LCG (`sweep_cases`) by count and the SHA-256 of their answers, one line
each (``E`` when refused, else the token types and offsets as
`_answer_line` spells them), joined by newlines.
"""

from __future__ import annotations

import ast
import hashlib
import io
import sys
import tokenize
import warnings
from typing import Any

from ._common import jsonl
from .gen_pysyntax import _built, _spelled

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"
_INT_DIGITS = 4300

#: Message prefixes of the tokenizer and the literal decoders.
_LEX_MESSAGES = (
    "unindent does not match",
    "inconsistent use of tabs",
    "too many levels of indentation",
    "source code string cannot contain null bytes",
    "unexpected EOF while parsing",
    "unexpected character after line continuation",
    "unterminated ",
    "invalid decimal literal",
    "invalid hexadecimal literal",
    "invalid octal literal",
    "invalid binary literal",
    "invalid digit",
    "invalid imaginary literal",
    "leading zeros",
    "invalid character",
    "invalid non-printable character",
    "unmatched ",
    "closing parenthesis",
    "too many nested parentheses",
    "Exceeds the limit",
    "(unicode error)",
    "(value error)",
    "bytes can only contain ASCII",
)
_BARE = {"NEWLINE", "NL", "INDENT", "DEDENT", "ENDMARKER"}


def _parse(text: str) -> tuple[str, bool]:
    """What ``ast.parse`` raises, and whether its message is a lexer's."""
    try:
        ast.parse(text)
    except SyntaxError as e:
        msg = str(e.msg)
        lexer = msg.startswith(_LEX_MESSAGES) or "was never closed" in msg
        return type(e).__name__, lexer
    except Exception as e:  # ValueError for a lone surrogate
        return type(e).__name__, True
    return "ok", False


def _refused(text: str) -> bool:
    """Whether the tokenizer in the parser's mode refuses ``text``."""
    try:
        toks = list(
            tokenize._generate_tokens_from_c_tokenizer(  # type: ignore[attr-defined]
                io.StringIO(text).readline, extra_tokens=False
            )
        )
    except Exception:
        return True
    for t in toks:
        if t.type in (tokenize.STRING, tokenize.NUMBER):
            try:
                ast.parse(t.string, mode="eval")
            except Exception:
                return True
    return False


def _tokens(text: str) -> list[list[Any]]:
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    out: list[list[Any]] = []
    fstart = 0
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        name = tokenize.tok_name[t.type]
        a = starts[t.start[0] - 1] + t.start[1]
        b = starts[t.end[0] - 1] + t.end[1]
        if name == "FSTRING_START":
            fstart = a
        elif name == "FSTRING_END":
            out.append(["FSTRING", fstart, b])
        elif name == "FSTRING_MIDDLE":
            pass
        elif name in _BARE:
            out.append([name])
        else:
            out.append([name, a, b])
    return out


def _answer(text: str) -> dict[str, Any]:
    parse, lexer = _parse(text)
    translated = text.replace("\r\n", "\n").replace("\r", "\n")
    lex = _refused(translated)
    assert lex or not lexer, text
    tok: Any = None
    if not lex:
        tok = _tokens(translated)
        if len(text) > 400:
            line = _spelled_tokens(tok)
            tok = {"n": len(tok), "h": hashlib.sha256(line.encode()).hexdigest()}
    return {"lex": lex, "parse": parse, "tok": tok}


def _spelled_tokens(tok: list[list[Any]]) -> str:
    return " ".join(":".join(str(x) for x in t) for t in tok)


def _answer_line(text: str) -> str:
    a = _answer(text)
    if a["lex"]:
        return "E"
    return _spelled_tokens(a["tok"])


_LINES = [
    "",
    "\n",
    "\n\n\n",
    "x",
    "x\n",
    "x = 1\ny = 2\n",
    "x = 1;y = 2",
    "# only a comment",
    "# c\n",
    "x # c\n",
    "x  # c",
    "   \n",
    "   ",
    "x\n   ",
    "x\n\t",
    "x\n  # c",
    "\n\nx\n\n",
    "x\r\ny\r\n",
    "x\ry\r",
    "x\r",
    "\r",
    "\r\n\r\n",
    "x\r\r\ny",
    "pass\n\x0c\nx\n",
    "\x0cx\n",
    "x\x0c= 1\n",
]

_INDENT = [
    "if x:\n    y\n",
    "if x:\n    y\nz\n",
    "if x:\n    y\n  z\n",
    "if x:\n  y\n    z\n",
    "if x:\n    if y:\n        z\n",
    "if x:\n    if y:\n        z\nw\n",
    "if x:\n    if y:\n        z\n    w\n",
    "if x:\n    if y:\n        z\n  w\n",
    "if x:\n    if y:\n        z\n      w\n",
    "  x\n",
    "x\n  y\n",
    "if x:\n    y\n\n    z\n",
    "if x:\n    y\n# c\n    z\n",
    "if x:\n    y\n        # c\n    z\n",
    "if x:\n    y\n  # c\nz\n",
    "if x:\n    y\n    ",
    "if x:\n    y\n  ",
    "if x:\n    y",
    "if x:\n    y\n    \n",
    "if x:\n    (y,\nz)\n",
    "if x:\n    (y,\n  z)\n  w\n",
    "if x:\n    [\n]\n",
    "if x:\n    {1:\n        2}\n",
    "def f():\n    return 1\n\nclass C:\n    pass\n",
    "if x:\n\ty\n",
    "if x:\n\ty\n\tz\n",
    "if x:\n\ty\n        z\n",
    "if x:\n        y\n\tz\n",
    "if x:\n  \ty\n",
    "if x:\n\t  y\n",
    "if x:\n  \ty\n\t  z\n",
    "if x:\n \ty\n\tz\n",
    "if x:\n\ty\n \tz\n",
    "if x:\n       \ty\n\tz\n",
    "if x:\n\t\ty\n\t\tz\n",
    "if x:\n\t\ty\n                z\n",
    "if x:\n    y\n\tz\n",
    "if x:\n\tif y:\n\t\tz\n\tw\n",
    "if x:\n\tif y:\n\t\tz\n        w\n",
    "if x:\n\x0c    y\n",
    "if x:\n    \x0cy\n",
    "if x:\n  \x0c  y\n",
    "if x:\n\t\x0c\ty\n",
    "if x:\n    y\n\x0cz\n",
    "if x:\n    y\n  \x0cz\n",
    "if x:\n    y\n\x0c    z\n",
    "if x:\n    y\n      \x0c    z\n",
    "if x:\n \x0c   y\n    z\n",
    "if x:\n    y\n\x0c\n    z\n",
    "if x:\n    y\n    \\\nz\n",
    "if x:\n    \\\n  y\n",
    "if x:\n  \\\n    y\n",
    "if x:\n    \\\n\\\ny\n",
    "\\\nx\n",
    "  \\\nx\n",
    "if x:\n    y\n  \\\n      z\n",
    "if x:\n    y\n\\\n    z\n",
    "if x:\n\t\\\n        y\n",
    "if x:\n\t\\\n\ty\n",
    "if x:\n    \\\n\n    y\n",
    "if x:\n    y\n    \\\n# c\n",
    "if x:\n    \\\n# c\n",
    "if x:\n    \\\n\x0cy\n",
]

_CONT = [
    "x = 1 + \\\n2\n",
    "x = 1 + \\\n    2\n",
    "x = \\\n\n",
    "x = 1 \\",
    "x = 1 \\\n",
    "x = 1 \\\n\n",
    "\\",
    "\\\n",
    "x \\ y\n",
    "x \\\ty\n",
    "x \\  \ny\n",
    "x \\\r\ny\n",
    "x \\\ry\n",
    "(1 +\n 2)\n",
    "(1 +\n\n 2)\n",
    "(1 +\n# c\n 2)\n",
    "(1 + \\\n 2)\n",
    "(\n",
    "(1\n",
    "[1,\n2\n",
    "{\n}",
    ")\n",
    "(]\n",
    "([)]\n",
    "x = (1,\n  2,\n    3)\ny\n",
    "def f(\n  a,\n):\n  pass\n",
    "x = '''a\nb'''\n",
    "x = '''a\n",
    "x = 'a\nb'\n",
    "x = 'a\\\nb'\n",
    "x = 'a",
    "x = '''",
    "x = ''''''\n",
    "x = '\\\n",
]

_TOKENS = [
    "x = 1.5e3 + 0x_ff - 0o7 * 0b1 / 1_000j\n",
    "x = 1__0\n",
    "x = 1_\n",
    "x = 0x\n",
    "x = 0b2\n",
    "x = 0o8\n",
    "x = 09\n",
    "x = 00\n",
    "x = 1e\n",
    "x = 1e+\n",
    "x = 1abc\n",
    "x = 1if y else 2\n",
    "x = 1 if y else 2\n",
    "x = 0x1for\n",
    "x = 1.__class__\n",
    "x = 1 .real\n",
    "x = .5\n",
    "x = ...\n",
    "x = 1" + "0" * 4299 + "\n",
    "x = 1" + "0" * 4300 + "\n",
    "x = 0x" + "f" * 5000 + "\n",
    "x = 1e309\n",
    "x = 'a' 'b' \"c\"\n",
    "x = b'a' 'b'\n",
    "x = rb'\\x' Rb'' bR'' BR\"\"\n",
    "x = u'a' U'b'\n",
    "x = ur'a'\n",
    "x = bu'a'\n",
    "x = '\\x4'\n",
    "x = '\\x41'\n",
    "x = b'\\x4'\n",
    "x = '\\u12'\n",
    "x = '\\U00110000'\n",
    "x = '\\q'\n",
    "x = b'\xe9'\n",
    "x = '\xe9'\n",
    "x = '\\N{DASH}'\n",
    "x = r'\\N{DASH}'\n",
    "x = f'a'\n",
    "x = f''\n",
    "x = F'''a\nb'''\n",
    "x = rf'a' fR\"b\" Rf'c'\n",
    "x = f'a\n",
    "x = f'''a\n",
    "x = f'{x}'\n",
    "x = f'{'a'}'\n",
    "x = f'a\\n'\n",
    "x = f'}}'\n",
    "x = fb'a'\n",
    "x = bf'a'\n",
    "x = uf'a'\n",
    "\xe9t\xe9 = 1\n",
    "\u212bx = 1\n",
    "\uff21 = 1\n",
    "x\u00b7 = 1\n",
    "\u00b7x = 1\n",
    "_\u0300 = 1\n",
    "\u0300 = 1\n",
    "x\u00a0= 1\n",
    "\u00a0x = 1\n",
    "x = 1 \u20ac\n",
    "\u2028x\n",
    "x\u2028y\n",
    "\ufeffx = 1\n",
    "x = \ufeff1\n",
    "\U0001d518 = 1\n",
    "\u2118 = 1\n",
    "\u309b = 1\n",
    "a\u2e2f = 1\n",
    "x = $\n",
    "x = ?\n",
    "x = `\n",
    "x = !\n",
    "x != y\n",
    "x <> y\n",
    "x -> y\n",
    "x := y\n",
    "x **= y //= z <<= w >>= v @= u\n",
    "x = \x0b1\n",
    "x = \x011\n",
    "x = \x7f\n",
    "x = \x85\n",
    "a\0",
    "\0",
    "# \0\n",
    "x = '\0'\n",
    "x = '\ud800'\n",
    "\ud800",
    "async def f():\n    await x\n",
    "match x:\n    case 1:\n        pass\n",
    "type X = int\n",
    "print 1\n",
    "x = = 1\n",
    "x =\n",
    "lambda: (yield)\n",
    "return\n",
    "x = (" * 200 + ")" * 200 + "\n",
    "x = " + "(" * 201 + ")" * 201 + "\n",
    "x = " + "[" * 200 + "]" * 200 + "\n",
]


def _levels(n: int, tab: bool = False) -> str:
    lines = []
    for k in range(n):
        pad = "\t" * k if tab else " " * k
        lines.append(f"{pad}if x:\n")
    lines.append(("\t" if tab else " ") * n + "pass\n")
    return "".join(lines)


def _texts() -> list[Any]:
    texts: list[Any] = [*_LINES, *_INDENT, *_CONT, *_TOKENS]
    for n in (98, 99, 100, 101):
        texts.append(_levels(n))
    texts.append(_levels(99, tab=True))
    texts.append(_levels(100, tab=True))
    # 99 levels then a dedent of every level at once, then one level more.
    texts.append(_levels(99) + "y\n" + _levels(99))
    return texts


#: Pieces of the sweep: indentation, line ends, continuations, comments,
#: names, numbers, strings, f-strings without fields and brackets.
_PIECES = [
    "\n",
    "\n",
    "\n",
    " ",
    "    ",
    "\t",
    "\x0c",
    "\\\n",
    "# c",
    "x",
    "if y:",
    "1",
    "0x1",
    "1.5",
    "'a'",
    "'''b\n'''",
    "f'c'",
    "(",
    ")",
    "[",
    "]",
    ":",
    "=",
    "+",
    "\n  ",
    "\n\t",
    "\n    ",
    "\n        ",
    "\n \t",
    "\n\t ",
]


def sweep_cases(count: int) -> list[str]:
    state = 0x2545_F491_4F6C_DD1D
    mask = (1 << 64) - 1

    def draw() -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        return state >> 33

    out = []
    for _ in range(count):
        n = 1 + draw() % 12
        out.append("".join(_PIECES[draw() % len(_PIECES)] for _ in range(n)))
    return out


def generate() -> dict[str, bytes]:
    """``pysyntax_module.jsonl``: rows ``{"t", "lex", "parse", "tok"}``
    and the sweep ``pieces``."""
    name = "pysyntax_module.jsonl"
    v = sys.version_info
    running = f"{v.major}.{v.minor}.{v.micro}"
    header: dict[str, Any] = {"fixture": "pysyntax_module", "python": REPRODUCES}
    if running != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = running
        return {name: jsonl([header])}
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(_INT_DIGITS)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rows: list[dict[str, Any]] = []
            for t in _texts():
                text = _built(t)
                spelled = _spelled(text) if len(text) <= 400 else _compact(text)
                rows.append({"t": spelled, **_answer(text)})
            answers = [_answer_line(t) for t in sweep_cases(20000)]
            rows.append(
                {
                    "sweep": "pieces",
                    "count": len(answers),
                    "sha256": hashlib.sha256("\n".join(answers).encode()).hexdigest(),
                }
            )
    finally:
        sys.set_int_max_str_digits(limit)
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}


def _compact(text: str) -> Any:
    """A long text as ``{"r": [[piece, n], ...]}``: runs of one repeated
    line or character."""
    runs: list[list[Any]] = []
    for line in text.splitlines(keepends=True):
        if runs and runs[-1][0] == line:
            runs[-1][1] += 1
        else:
            runs.append([line, 1])
    out: list[list[Any]] = []
    for piece, n in runs:
        if n == 1 and len(piece) > 40:
            # A long single line: its runs of one character.
            k = 0
            while k < len(piece):
                j = k
                while j < len(piece) and piece[j] == piece[k]:
                    j += 1
                out.append([_spelled(piece[k]), j - k])
                k = j
        else:
            out.append([_spelled(piece), n])
    assert _built({"r": out}) == text
    return {"r": out}
