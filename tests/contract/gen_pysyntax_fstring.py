"""Fixtures of the module tokenizer's f-strings (`pysyntax::fstring_lex`,
native/README.md, "Python source as CPython reads it").

The reference is CPython 3.13.13's tokenizer in module mode as
``ast.parse(source)`` in ``claims.check_claim`` (``claims.py`` 672) meets
it, read here in the parser's mode
(``tokenize._generate_tokens_from_c_tokenizer`` with ``extra_tokens``
false): the ``tokenize`` module's own mode moves the span of an
FSTRING_MIDDLE beside a doubled brace. The file ``pysyntax_fstring.jsonl``
is gated on the Python release (3.12 and 3.14 read f-strings differently
in places); under any other interpreter the generator declines and writes
the header alone, naming the interpreter.

A case row holds ``t``, the source, spelled as `gen_pysyntax` spells
texts; ``lex``, true when the tokenizer refuses it; ``parse``, the class
of what ``ast.parse`` raises or ``ok`` (recorded for the grammar units);
and ``tok`` when ``lex`` is false: ``[type, start, end]`` for a token with
text (an operator's type is ``OP``; offsets in code points of the source
with its line ends translated) and ``[type]`` for NEWLINE, INDENT, DEDENT
and ENDMARKER. The parser's mode reads no COMMENT and no NL. Past 400 code
points ``tok`` is ``{"n", "h"}``: the count of tokens and the SHA-256 of
their spelling by `_spelled_tokens`.

The tokenizer refuses a source when the C tokenizer in the parser's mode
raises, when one of its STRING or NUMBER tokens alone does not parse, or
when a literal part (FSTRING_MIDDLE) of a non-raw f-string does not decode
as the parser decodes it (`_part_decodes`). A SyntaxError of ``ast.parse``
whose message is a tokenizer's is asserted to be a refusal. A
UnicodeDecodeError of ``ast.parse`` is not: its error pass decodes a raw
f-string's format specification, so a raw triple-quoted f-string whose
specification is ``\\x4\\``, followed by an indented line, raises one
while the tokenizer accepts the source.

The C entry raises SystemError on some sources ``ast.parse`` accepts (a
triple-quoted f-string holding ``{x=:``, a newline and ``}``). Such a case
row holds ``lex`` false and ``tok`` the string ``"unjudged"``: the
tokenizer accepted (``ast.parse`` read it), and the tokens are not known.
A SystemError on a source ``ast.parse`` refuses, or on a sweep source,
stops the generator rather than write a row it cannot judge.

A sweep row pins 20,000 f-strings of one to eight pieces drawn by a fixed
LCG (`sweep_cases`) by count and the SHA-256 of their answers, one
line each (``E`` when refused, else the tokens as `_spelled_tokens`
spells them), joined by newlines.
"""

from __future__ import annotations

import ast
import codecs
import hashlib
import io
import sys
import tokenize
import warnings
from typing import Any

from ._common import jsonl
from .gen_pysyntax import _built, _spelled
from .gen_pysyntax_module import _compact

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"
_INT_DIGITS = 4300

#: Message prefixes of the tokenizer, the literal decoders and the
#: f-string modes.
_LEX_MESSAGES = (
    "unindent does not match",
    "inconsistent use of tabs",
    "too many levels of indentation",
    "source code string cannot contain null bytes",
    "unexpected EOF while parsing",
    "unexpected character after line continuation",
    "unterminated ",
    "invalid decimal literal",
    "invalid character",
    "invalid non-printable character",
    "unmatched ",
    "closing parenthesis",
    "too many nested parentheses",
    "(unicode error)",
    "(value error)",
    "f-string: single '}' is not allowed",
    "f-string: newlines are not allowed",
    "f-string: expressions nested too deeply",
    "too many nested f-strings",
)
_BARE = {"NEWLINE", "INDENT", "DEDENT", "ENDMARKER"}
_OPS = set(tokenize.EXACT_TOKEN_TYPES.values())


def _parse(text: str) -> tuple[str, bool]:
    """What ``ast.parse`` raises, and whether a tokenizer raised it."""
    try:
        ast.parse(text)
    except SyntaxError as e:
        msg = str(e.msg)
        lexer = msg.startswith(_LEX_MESSAGES) or "was never closed" in msg
        return type(e).__name__, lexer
    except Exception as e:
        # A UnicodeDecodeError is not the tokenizer's alone: the error pass
        # of ``ast.parse`` decodes a raw f-string's format specification,
        # so it can raise one for a source the tokenizer accepts.
        return type(e).__name__, False
    return "ok", False


def _part_decodes(part: str) -> bool:
    """``decode_unicode_with_escapes`` over a non-raw literal part: a
    backslash at the end or before a character past ASCII is a backslash,
    every character past ASCII is passed as its ``\\U`` escape, then the
    ``unicode_escape`` codec reads the ASCII result."""
    buf: list[str] = []
    k = 0
    while k < len(part):
        if part[k] == "\\":
            buf.append("\\")
            k += 1
            if k >= len(part) or ord(part[k]) >= 0x80:
                buf.append("u005c")
                if k >= len(part):
                    break
        if ord(part[k]) >= 0x80:
            buf.append(f"\\U{ord(part[k]):08x}")
        else:
            buf.append(part[k])
        k += 1
    try:
        codecs.unicode_escape_decode("".join(buf).encode("ascii"))
    except UnicodeDecodeError:
        return False
    return True


class _Unjudged(Exception):
    """The C entry raised SystemError: it cannot say what it read."""


def _c_tokens(text: str) -> list[Any] | None:
    """The parser's-mode tokens, or None when the tokenizer refuses.
    Raises `_Unjudged` when the C entry raises SystemError."""
    try:
        toks = list(
            tokenize._generate_tokens_from_c_tokenizer(  # type: ignore[attr-defined]
                io.StringIO(text).readline, extra_tokens=False
            )
        )
    except SystemError as e:
        raise _Unjudged(text) from e
    except Exception:
        return None
    raw: list[bool] = []
    for t in toks:
        if t.type in (tokenize.STRING, tokenize.NUMBER):
            try:
                ast.parse(t.string, mode="eval")
            except Exception:
                return None
        elif tokenize.tok_name[t.type] == "FSTRING_START":
            raw.append("r" in t.string.lower())
        elif tokenize.tok_name[t.type] == "FSTRING_END":
            raw.pop()
        elif tokenize.tok_name[t.type] == "FSTRING_MIDDLE" and not raw[-1]:
            if not _part_decodes(t.string):
                return None
    return toks


def _tokens(text: str, toks: list[Any]) -> list[list[Any]]:
    starts = [0]
    for line in text.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    out: list[list[Any]] = []
    for t in toks:
        name = "OP" if t.type in _OPS else tokenize.tok_name[t.type]
        if name in _BARE:
            out.append([name])
            continue
        a = starts[t.start[0] - 1] + t.start[1]
        b = starts[t.end[0] - 1] + t.end[1]
        if name == "FSTRING_MIDDLE":
            # The span spells the token: a doubled brace keeps one.
            assert text[a:b] == t.string, (text, t)
        out.append([name, a, b])
    return out


def _spelled_tokens(tok: list[list[Any]]) -> str:
    return " ".join(":".join(str(x) for x in t) for t in tok)


def _answer(text: str) -> dict[str, Any]:
    parse, lexer = _parse(text)
    translated = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        toks = _c_tokens(translated)
    except _Unjudged:
        # ``ast.parse`` read the source with the same tokenizer: when it
        # accepts, so did the tokenizer, and only the tokens are unknown.
        # Any other answer cannot be judged, and no row is written.
        if parse != "ok":
            raise
        return {"lex": False, "parse": parse, "tok": "unjudged"}
    lex = toks is None
    assert lex or not lexer, text
    tok: Any = None
    if toks is not None:
        tok = _tokens(translated, toks)
        if len(text) > 400:
            tok = {
                "n": len(tok),
                "h": hashlib.sha256(_spelled_tokens(tok).encode()).hexdigest(),
            }
    return {"lex": lex, "parse": parse, "tok": tok}


def _answer_line(text: str) -> str:
    a = _answer(text)
    if a["tok"] == "unjudged":
        raise _Unjudged(text)
    return "E" if a["lex"] else _spelled_tokens(a["tok"])


_PREFIXES = [
    "f'a'\n",
    "F'a'\n",
    "rf'a'\n",
    "Rf'a'\n",
    "fR'a'\n",
    "FR'a'\n",
    'f"a"\n',
    "f'''a\nb'''\n",
    'rF"""a\n"""\n',
    "f''\n",
    'f""\n',
    "f''''''\n",
    "f'''''''\n",
    "f'''a''''\n",
    "f'''a'' '''\n",
    "fb'a'\n",
    "bf'a'\n",
    "uf'a'\n",
    "fu'a'\n",
    "frb'a'\n",
    "f 'a'\n",
    "f'a' 'b' f'c'\n",
    "f'a'\"b\"\n",
]

_BRACES = [
    "f'{{'\n",
    "f'}}'\n",
    "f'{{}}'\n",
    "f'a{{b}}c'\n",
    "f'{{a'\n",
    "f'a}}'\n",
    "f'{{{x}}}'\n",
    "f'{{{{x'\n",
    "f'}'\n",
    "f'a}b'\n",
    "f'{'\n",
    "f'{x'\n",
    "f'{x}}'\n",
    "f'{x}}}'\n",
    "f'{}'\n",
    "f'{ }'\n",
    "f'{x}'\n",
    "f'{x}{y}'\n",
    "f'a{x}b{y}c'\n",
    "f'{x}{{'\n",
    "f'{{{x}'\n",
    "f'{x:'}\n",
    "f'{x:'}'\n",
    "f'{'a'}'\n",
]

_FIELDS = [
    "f'{(x)}'\n",
    "f'{[x]}'\n",
    "f'{{x: 1}}'\n",
    "f'{ {x: 1} }'\n",
    "f'{ {x: 1}[x] }'\n",
    "f'{(x}'\n",
    "f'{x)}'\n",
    "f'{x]}'\n",
    "f'{(x:y)}'\n",
    "f'{x[1:2]}'\n",
    "f'{x:y}'\n",
    "f'{x:=1}'\n",
    "f'{(x:=1)}'\n",
    "f'{lambda x: 1}'\n",
    "f'{(lambda x: 1)}'\n",
    "f'{x!r}'\n",
    "f'{x!s:>3}'\n",
    "f'{x!=y}'\n",
    "f'{x!}'\n",
    "f'{x !r}'\n",
    "f'{x!z}'\n",
    "f'{x=}'\n",
    "f'{x=!r}'\n",
    "f'{x=:>3}'\n",
    "f'{x = }'\n",
    "f'{x==y}'\n",
    "f'{x:{y}}'\n",
    "f'{x:{y}>{z}}'\n",
    "f'{x:a{y}b}'\n",
    "f'{x:{{y}}}'\n",
    "f'{x:{y}{{z}}}'\n",
    "f'{x:}}'\n",
    "f'{x:{y:{z}}}'\n",
    "f'{x:{y:{z:{w}}}}'\n",
    "f'{x:!r}'\n",
    "f'{x:{y!r}}'\n",
    "f'{x:a:b}'\n",
    "f'{x:a\nb}'\n",
    "f'''{x:a\nb}'''\n",
    "f'''{x:{y}\nb}'''\n",
    "f'{x:{y}\n}'\n",
    "f'{\"a\"}'\n",
    "f'{'a'}'\n",
    "f'{'''a'''}'\n",
    "f'{f'{x}'}'\n",
    "f'{f\"{x}\"}'\n",
    "f'{f'{f'{x}'}'}'\n",
    "f\"{'}'}\"\n",
    "f'{'}'}'\n",
    "f'{\"'\"}'\n",
    "f'{x\n}'\n",
    "f'{\nx}'\n",
    "f'''{\nx\n}'''\n",
    "f'{x # c}'\n",
    "f'''{x # c\n}'''\n",
    "f'''{x # c}\n}'''\n",
    "f'{x \\\n}'\n",
    "f'{x \\ }'\n",
    "f'{1}{1.5}{0x1}{1j}'\n",
    "f'{1_}'\n",
    "f'{'\\x'}'\n",
    "f'{x}' y\n",
    "f'{x}'\ny\n",
    "f'{x}' + f'{y}'\n",
    "(f'{x}',\n f'{y}')\n",
    "[f'{x\n}'\n]\n",
    "if x:\n    f'{y}'\n    z\n",
    "if x:\n    f'''{y\n}'''\nz\n",
    "if x:\n    f'''a\nb'''\n  z\n",
    "f'{x}'\n  y\n",
    "x = f'{y}' # c\n",
    "f'{x}' f'{y}' 'z'\n",
    "f'{x:{y}}' + 1\n",
    "f'{lambda: 1}'\n",
    "f'{yield}'\n",
    "f'{*x}'\n",
    "f'{x,y}'\n",
    "f'{x for x in y}'\n",
]

_ESCAPES = [
    "f'\\''\n",
    "f'\\\"'\n",
    "f'''\\''''\n",
    "f'a\\n'\n",
    "f'a\\{x}'\n",
    "f'a\\}}'\n",
    "f'a\\{{'\n",
    "f'\\{x}'\n",
    "rf'\\{x}'\n",
    "f'\\\\{x}'\n",
    "f'\\N{DASH}'\n",
    "f'\\N{DASH}{x}'\n",
    "rf'\\N{x}'\n",
    "f'\\N'\n",
    "f'\\N{'\n",
    "f'\\N{a}b}'\n",
    "f'\\x'\n",
    "f'\\x4'\n",
    "f'\\x41{x}'\n",
    "f'\\x4{x}'\n",
    "rf'\\x'\n",
    "f'\\u12'\n",
    "f'\\U00110000'\n",
    "f'\\U0010ffff'\n",
    "f'\\q'\n",
    "f'\\\xe9'\n",
    "f'\\\n'\n",
    "f'a\\\nb'\n",
    "f'\\101'\n",
    "f'{x:\\x}'\n",
    "rf'{x:\\x}'\n",
    "f'{x:\\x41}'\n",
    "f'{x:\\}'\n",
    "f'{x:\\{y}}'\n",
    "f'\\\n",
    "f'\\",
    "f'\xe9{x}\u20ac'\n",
]

_ENDS = [
    "f'a\n",
    "f'a",
    "f'{x}\n",
    "f'{x}",
    "f'{x",
    "f'{x\n",
    "f'''a\n",
    "f'''a",
    "f'''{x}\n",
    "f'''a''\n",
    "f'{x:a",
    "f'{x:a\n",
    "f'''{x:a",
    "x = (f'a\n)\n",
    "f'a\r\nb'\n",
    "f'''a\r\nb'''\r\n",
    "f'{x\r\n}'\r\n",
    # The C entry raises SystemError here; ast.parse accepts.
    'f"""{x=:\n}"""\n',
    # ast.parse raises UnicodeDecodeError; the tokenizer accepts.
    'Rf"""{x:\\x4\\}"""\n  x\n',
]


def _nested(n: int) -> str:
    return "f'{" * n + "x" + "}'" * n + "\n"


def _texts() -> list[str]:
    texts = [*_PREFIXES, *_BRACES, *_FIELDS, *_ESCAPES, *_ENDS]
    for n in (2, 3, 148, 149, 150):
        texts.append(_nested(n))
    # Nested f-strings alternating quotes, at the limit and past it.
    texts.append("".join("f'{f\"{" for _ in range(74)) + "x" + "}\"}'" * 74 + "\n")
    texts.append("".join("f'{f\"{" for _ in range(75)) + "x" + "}\"}'" * 75 + "\n")
    # The bracket limit shared with the field's brace.
    for n in (199, 200):
        texts.append("f'{" + "(" * n + "x" + ")" * n + "}'\n")
    texts.append("(" * 198 + "f'{(x)}'" + ")" * 198 + "\n")
    texts.append("(" * 199 + "f'{x}'" + ")" * 199 + "\n")
    texts.append("(" * 199 + "f'{(x)}'" + ")" * 199 + "\n")
    texts.append("f'" + "a" * 500 + "{x}" + "}}" * 100 + "'\n")
    texts.append("f'" + "{x}" * 200 + "'\n")
    return texts


#: The sweep's sources: an opener, one to eight pieces, the opener's
#: closing quote (or, one time in eight, a piece instead) and a tail.
#: The pieces hold braces single and doubled, `:`, `!` and `=` at field
#: depth, nested quotes and f-strings, backslashes, brackets, comments and
#: line ends. No piece puts an `N` after a backslash.
_OPENERS = ["f'", 'f"', "f'''", "rf'", 'F"""', "fr'"]
_PIECES = [
    "a",
    "a b",
    "{x}",
    "{x}",
    "{{",
    "}}",
    "{x!r}",
    "{x:>3}",
    "{x:{y}}",
    "{x=}",
    "{",
    "}",
    "{x",
    ":",
    "!",
    "!r",
    "=",
    "!=",
    ":=",
    "x",
    "(",
    ")",
    "[",
    "]",
    "'",
    '"',
    "\\",
    "\\x41",
    "\\x",
    "\\{",
    "\n",
    " ",
    "# c",
    "{'a'}",
    "{f'{x}'}",
    '{f"{x}"}',
    "{lambda: 1}",
    "\\\n",
]
_TAILS = ["\n", " y\n", "", "\nz\n", " + f'{x}'\n", ")\n"]


def sweep_cases(count: int) -> list[str]:
    state = 0x9E37_79B9_7F4A_7C15
    mask = (1 << 64) - 1

    def draw() -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) & mask
        return state >> 33

    out = []
    for _ in range(count):
        opener = _OPENERS[draw() % len(_OPENERS)]
        n = 1 + draw() % 8
        body = "".join(_PIECES[draw() % len(_PIECES)] for _ in range(n))
        close = opener.lstrip("fFrR") if draw() % 8 else _PIECES[draw() % len(_PIECES)]
        out.append(opener + body + close + _TAILS[draw() % len(_TAILS)])
    return out


def generate() -> dict[str, bytes]:
    """``pysyntax_fstring.jsonl``: rows ``{"t", "lex", "parse", "tok"}``
    and the sweep ``fstring_pieces``."""
    name = "pysyntax_fstring.jsonl"
    v = sys.version_info
    running = f"{v.major}.{v.minor}.{v.micro}"
    header: dict[str, Any] = {"fixture": "pysyntax_fstring", "python": REPRODUCES}
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
                    "sweep": "fstring_pieces",
                    "count": len(answers),
                    "sha256": hashlib.sha256("\n".join(answers).encode()).hexdigest(),
                }
            )
    finally:
        sys.set_int_max_str_digits(limit)
    header["rows"] = len(rows)
    return {name: jsonl([header, *rows])}
