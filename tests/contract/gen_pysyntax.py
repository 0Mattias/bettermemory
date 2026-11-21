"""Fixtures of the eval-mode tokenizer (`pysyntax::lex`, native/README.md,
"Python source as CPython reads it").

The reference is CPython 3.13.13 as `ast.literal_eval` meets a text: the
text after ``lstrip(' \\t')``, parsed in eval mode. No module of the
product is imported. Two files:

- ``pysyntax_python.jsonl``, gated on the Python release as a whole: the
  tokenizer's tokens, its messages and the literal reader's answers as
  3.13.13 gives them. Under any other interpreter the generator declines:
  it writes the header alone, naming the interpreter.
- ``pysyntax.jsonl``, not gated: what `ast.literal_eval` returns or raises
  (``v`` alone, by exception type) for the escapes, strings, prefixes,
  numbers, line joining and the bounds, and the sweeps of escapes and of
  numbers by value, which 3.11, 3.12, 3.13 and 3.14 answer alike
  (`generate_stable`).

The first line of ``pysyntax_python.jsonl`` is the header. A case row
holds:

- ``t``: the text (a JSON string, the list of its code points when it holds
  a surrogate or two backslashes in a row, or ``{"r": [[piece, n], ...]}``, the pieces repeated and
  joined);
- ``k``: the tokens `tokenize.generate_tokens` reads from the text after
  ``lstrip(' \\t')`` with its line ends translated (``\\r\\n`` and ``\\r``
  to ``\\n``, as `_PyTokenizer_FromString` does), NAME, NUMBER, STRING and
  OP as ``[type, string]``, a string of more than 80 code points as
  ``{"n": length, "h": sha256}`` (of its UTF-32-LE), spelled as ``t`` is
  otherwise, up to and including
  the first f-string, ``["FSTRING", null]``; ``"raised"`` where
  `tokenize` raises first;
- ``p``: how ``ast.parse(text, mode="eval")`` of the stripped text ends:
  ``ok``, ``lex`` (a SyntaxError the tokenizer or the literal decoder
  raises, told by its message, `_LEX`), ``syntax`` (any other
  SyntaxError) or ``other`` (another exception);
- ``v``: what `ast.literal_eval` returns or raises (`_value`);
- ``d``, on a few rows: the table the port defers to (``ident``: a name
  past ASCII whose NFKC is a name `literal_eval` accepts; ``names``: a
  ``\\N{...}`` escape).

A sweep row pins many cases by a rule: ``count`` and the SHA-256 of their
answers, one line each, joined by newlines (`_sweep_escapes`,
`_sweep_numbers`).
"""

from __future__ import annotations

import ast
import hashlib
import io
import struct
import sys
import tokenize
import warnings
from typing import Any

from ._common import jsonl

#: The interpreter whose answers the file holds.
REPRODUCES = "3.13.13"
_INT_DIGITS = 4300

#: Messages of the SyntaxErrors the tokenizer and the literal decoder raise.
_LEX = (
    "invalid decimal literal",
    "invalid hexadecimal literal",
    "invalid octal literal",
    "invalid binary literal",
    "invalid digit",
    "invalid imaginary literal",
    "leading zeros",
    "unterminated string literal",
    "unterminated triple-quoted string literal",
    "too many nested parentheses",
    "unmatched",
    "does not match opening parenthesis",
    "was never closed",
    "unexpected EOF",
    "unexpected character after line continuation",
    "invalid non-printable character",
    "invalid character",
    "cannot contain null bytes",
    "(unicode error)",
    "(value error)",
    "bytes can only contain ASCII",
    "Exceeds the limit",
    "unexpected indent",
)


def _built(t: Any) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "".join(chr(x) for x in t)
    return "".join(_built(p) * n for p, n in t["r"])


def _spelled(text: str) -> Any:
    # Two backslashes in a row would read as a UNC path to the host-fact
    # guard (`gen_store_files.host_fact`): such a text is spelled as code
    # points too.
    if "\\\\" in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return [ord(ch) for ch in text]
    return text


def _digest(text: str) -> Any:
    if len(text) <= 80:
        return _spelled(text)
    data = text.encode("utf-32-le", "surrogatepass")
    return {"n": len(text), "h": hashlib.sha256(data).hexdigest()}


def _bits(x: float) -> str:
    return struct.pack(">d", x).hex()


def _stripped(text: str) -> str:
    return text.lstrip(" \t").replace("\r\n", "\n").replace("\r", "\n")


def _tokens(text: str) -> Any:
    out: list[Any] = []
    names = {tokenize.NAME, tokenize.NUMBER, tokenize.STRING, tokenize.OP}
    # FSTRING_START is 3.12's; the file is made under 3.13.13 alone.
    fstring_start = getattr(tokenize, "FSTRING_START", -1)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(_stripped(text)).readline):
            if tok.type == fstring_start:
                out.append(["FSTRING", None])
                return out
            if tok.type in names:
                out.append([tokenize.tok_name[tok.type], _digest(tok.string)])
    except Exception:
        return "raised"
    return out


def _parse(text: str) -> str:
    try:
        ast.parse(text.lstrip(" \t"), mode="eval")
    except SyntaxError as exc:
        return "lex" if any(m in str(exc.msg) for m in _LEX) else "syntax"
    except Exception:
        return "other"
    return "ok"


def _value(text: str) -> Any:
    try:
        v = ast.literal_eval(text)
    except Exception as exc:
        return {"raised": type(exc).__name__}
    if isinstance(v, bool) or v is None:
        return {"const": repr(v)}
    if isinstance(v, int):
        sys.set_int_max_str_digits(0)
        try:
            s = str(v)
        finally:
            sys.set_int_max_str_digits(_INT_DIGITS)
        if len(s) > 80:
            return {"int": {"n": len(s), "h": hashlib.sha256(s.encode()).hexdigest()}}
        return {"int": s}
    if isinstance(v, float):
        return {"float": _bits(v)}
    if isinstance(v, complex):
        return {"complex": [_bits(v.real), _bits(v.imag)]}
    if isinstance(v, str):
        return {"str": _digest(v)}
    if isinstance(v, bytes):
        if len(v) > 40:
            return {"bytes": {"n": len(v), "h": hashlib.sha256(v).hexdigest()}}
        return {"bytes": v.hex()}
    return {"type": type(v).__name__}


def _row(t: Any, defer: str | None = None) -> dict[str, Any]:
    text = _built(t)
    spelled = _spelled(t) if isinstance(t, str) else t
    row = {"t": spelled, "k": _tokens(text), "p": _parse(text), "v": _value(text)}
    if defer:
        row["d"] = defer
    return row


_ESC = "\\"

#: Escapes, each in a str and a bytes literal, raw and not.
_ESCAPES = [
    "\\\\",
    "\\'",
    '\\"',
    "\\a",
    "\\b",
    "\\f",
    "\\n",
    "\\r",
    "\\t",
    "\\v",
    "\\0",
    "\\7",
    "\\07",
    "\\101",
    "\\1011",
    "\\377",
    "\\400",
    "\\777",
    "\\8",
    "\\9",
    "\\x41",
    "\\x4",
    "\\x4g",
    "\\xff",
    "\\xFF",
    "\\x",
    "\\u00e9",
    "\\u00E9",
    "\\u12",
    "\\u1234x",
    "\\U0001F600",
    "\\U0010ffff",
    "\\U00110000",
    "\\Uffffffff",
    "\\U0001F60",
    "\\ud800",
    "\\udfff",
    "\\ud83d\\ude00",
    "\\q",
    "\\z",
    "\\ ",
    "\\é",
    "\\N",
    "\\N{",
    "\\N{}",
    "\\N}",
    "a\\\nb",
    "\\\n",
    "\\\r\n",
    "\\\r",
]

_STRINGS = [
    "''",
    '""',
    "''''''",
    '""""""',
    "'a'",
    '"a"',
    "'''a'''",
    '"""a"""',
    "'''a''b'''",
    "'''a'''''",
    "'a",
    "'a\nb'",
    "'''a\nb'''",
    "'''a\rb'''",
    "'''a\r\nb'''",
    "'''a",
    "'a\\'",
    "'\\",
    "'é'",
    "'€'",
    "'\U0001f600'",
    "'\t\x01\x7f'",
    "'\x0b'",
    "b'\x7f'",
    "b'é'",
    "rb'é'",
    "b'\\é'",
    "'a' 'b'",
    "'a'\\\n'b'",
    "'a' b'b'",
    "('a'\n'b')",
    "'a' # c",
    '"\'"',
    "'\"'",
    "'''\"'''",
    "'a''b'",
    "r'\\''",
    "r'\\'",
    "r'\\\\'",
    "r'a\\\nb'",
    "rb'\\x'",
    "r'\\N{DASH}'",
    "b'\\N{DASH}'",
    "b'\\u1234'",
]

_PREFIXES = [
    "",
    "r",
    "R",
    "u",
    "U",
    "b",
    "B",
    "br",
    "bR",
    "Br",
    "BR",
    "rb",
    "rB",
    "Rb",
    "RB",
    "f",
    "F",
    "fr",
    "rf",
    "Rf",
    "fR",
    "ur",
    "ru",
    "ub",
    "bu",
    "uf",
    "fu",
    "bf",
    "fb",
    "rr",
    "bb",
    "uu",
    "ff",
    "rbr",
    "x",
    "_",
]

_NUMBERS = [
    "0",
    "00",
    "000",
    "0_0",
    "0__0",
    "0_",
    "0_7",
    "07",
    "007",
    "08",
    "09",
    "09.5",
    "07e1",
    "07j",
    "0.",
    ".5",
    "0.5",
    "5.",
    "5.5",
    "1_000",
    "1__000",
    "1_",
    "_1",
    "1_000.000_1",
    "1e5",
    "1E5",
    "1e+5",
    "1e-5",
    "1e+",
    "1e-",
    "1e",
    "1E",
    "1e5_0",
    "1e_5",
    "1.e5",
    "1.5e",
    "1.5j",
    "1j",
    "1J",
    "0j",
    "1e5j",
    "1E-5j",
    "1jj",
    "1j_",
    "1.j",
    ".5j",
    "1_j",
    "0x0",
    "0x1F",
    "0XfF",
    "0x_1",
    "0x__1",
    "0x1_",
    "0x1__2",
    "0x",
    "0xg",
    "0x1g",
    "0o7",
    "0O17",
    "0o_7",
    "0o8",
    "0o78",
    "0o",
    "0b101",
    "0B1",
    "0b_1",
    "0b12",
    "0b1_",
    "0b",
    "0b1e",
    "0x1e",
    "0x1.5",
    "0o1.5",
    "1e309",
    "-1e309",
    "1e-400",
    "2.2250738585072014e-308",
    "4.9406564584124654e-324",
    "1.7976931348623157e308",
    "1.7976931348623159e308",
    "9007199254740993",
    "9007199254740993.0",
    "0.1",
    "1x",
    "1if 1 else 2",
    "1else",
    "1and 2",
    "1or 2",
    "1in x",
    "1is x",
    "1not in x",
    "1for",
    "1.real",
    "1 .real",
    "1..2",
    "1.__x",
    "5._",
    "._5",
    "x.1",
    "1_000j",
    "1__0j",
    "-1",
    "+1",
    "1+2j",
    "1-2j",
    "-0.0",
    "1e5_",
    "1.5_",
    "0e0",
    "0E0j",
    "1é",
    "1²",
    "123456789012345678901234567890",
    "0x123456789abcdef0123456789abcdef",
]

_LINES = [
    "",
    " ",
    "\t",
    "\n",
    "\n1",
    "\n 1",
    "\n\n1",
    " \n1",
    "#c\n1",
    "1#x",
    "1 #c",
    "1\n",
    "1\n\n",
    "1\r\n",
    "1\r",
    "1\n2",
    "1\n#c\n  \n",
    "1\n  ",
    "1\n  #c",
    "1\n\t\n",
    "1\n\f",
    "1\n\f#c",
    "\f1",
    "\f 1",
    "\f  \f1",
    "\t\f1",
    "\\\n1",
    " \\\n1",
    "\\\n 1",
    "\\\n\n1",
    "\n \\\n\f'a'",
    "\f \\\n\f1",
    "1\n \\\n\f",
    "\r\t\\\n\f1#c ",
    "\\\n \\\n\f1",
    "1\n\n2",
    "1\n#c\n2",
    "3or7",
    "11Jor81",
    "3or x",
    "3or",
    "1if7",
    "0x1or_",
    "\\\n \n1",
    "\\\n\\\n1",
    "1\\\n",
    "1 \\\n",
    "1 \\\n 2",
    "1\n\\\n",
    "1\n\\\n\n",
    "1\n\\\n#c",
    "1\n \\\n\n",
    "1 #c\\\n",
    "1\n#c\\",
    "1 \\\n#c",
    "\\",
    "\\ 1",
    "1\n\\",
    "1 \\x",
    "(1 \\\n,2)",
    "(\\\n1)",
    "(1,\n 2)\n",
    "(1\n  #c\n, 2)",
    "[1,\n\n]",
    "(\n1\n)\n2",
    "1\n)",
    "1\x00",
    "'\x00'",
    "#\x00",
    "1\x0b",
    "\x0b1",
    "1\x7f",
    "\xa01",
    "1;",
    "1\n\r\n",
    "1\r\r",
]

_OPS = [
    "$",
    "?",
    "`",
    "!",
    "!=",
    "1<>2",
    "a->b",
    "...",
    ". . .",
    "a:=1",
    "1**=2",
    "1//=2",
    "1<<=2",
    "1>>=2",
    "1**2",
    "1//2",
    "a@b",
    "a@=b",
    "~1",
    "1^2",
    "1|2",
    "1&2",
    "1%2",
    "{1:2}",
    "{1,2}",
    "[1,2]",
    "(1,)",
    "()",
    "[]",
    "{}",
    "(]",
    "[)",
    "{)",
    "(}",
    "1)",
    "(1",
    ")",
    "]",
    "}",
    "((1)",
    "(1))",
    "{'a':[(1,)]}",
    "€",
    "é",
    "set()",
    "True",
    "False",
    "None",
    "true",
    "x",
    "_",
    "__debug__",
    "print",
    "abc_123",
    "a b",
    "1 2 0x",
    "f'{1}' 0x",
    "'a' 0x",
    "\ud800",
    "'\udfff'",
    "1 \ud800",
]

_FSTRINGS = [
    "f'x'",
    "F'x'",
    "f''",
    "f'''x'''",
    "f'abc",
    "f'''abc",
    "f'{1}'",
    "f'{'a'}'",
    "f'x' 'y'",
    "'y' f'x'",
    "rf'\\x'",
    "fR'x'",
    "f'{x!r}'",
    "f'{\"a'",
    "f'\\''",
    "(f'x'",
]

_DEFERRED = [
    ("ｓｅｔ()", "ident"),
    ("Ｎｏｎｅ", "ident"),
    ("Ｔｒｕｅ", "ident"),
    ("Ｆａｌｓｅ", "ident"),
    ("sеt()", None),
    ("'\\N{EM DASH}'", "names"),
    ("'\\N{LATIN SMALL LETTER A}'", "names"),
    ("'\\N{NO SUCH NAME}'", "names"),
    ("u'\\N{EM DASH}'", "names"),
    ("'''\\N{EM DASH}'''", "names"),
]


#: The literals each prefix is put before.
_BODIES = ("'x'", '"x"', "'''x'''", "'\\x41\\n'")


def _explicit() -> list[dict[str, Any]]:
    rows = []
    for esc in _ESCAPES:
        for prefix in ("", "b", "r", "rb"):
            rows.append(_row(f"{prefix}'{esc}'"))
        rows.append(_row(f'"""{esc}"""'))
    rows.extend(_row(t) for t in _STRINGS)
    for prefix in _PREFIXES:
        rows.extend(_row(prefix + body) for body in _BODIES)
    rows.extend(_row(t) for t in _NUMBERS + _LINES + _OPS + _FSTRINGS)
    rows.extend(_row(t, d) for t, d in _DEFERRED)
    # Lines and spaces before the text, which literal_eval strips.
    rows.extend(_row(t) for t in (" 1", "\t1", " \t 'a'", "  \n1", "\t\\\n1"))
    return rows


#: Texts past the limits: the digit limit, nesting, long literals and lines.
_BOUNDS: list[Any] = [{"r": [["1", n]]} for n in (4299, 4300, 4301, 5000)] + [
    {"r": [["1_", 4299], ["1", 1]]},
    {"r": [["1_", 4300], ["1", 1]]},
    {"r": [["-", 1], ["1", 4301]]},
    {"r": [["1", 4301], ["j", 1]]},
    {"r": [["1", 4301], ["e0", 1]]},
    {"r": [["1", 4301], [".", 1]]},
    {"r": [["0", 10000]]},
    {"r": [["0_", 3000], ["0", 1]]},
    {"r": [["0", 4301], ["1", 1]]},
    {"r": [["0x", 1], ["f", 5000]]},
    {"r": [["0x", 1], ["_f", 3000]]},
    {"r": [["0o", 1], ["7", 6000]]},
    {"r": [["0b", 1], ["1", 9000]]},
    {"r": [["1.", 1], ["1", 5000]]},
    {"r": [["0.", 1], ["0", 400], ["1e-5", 1]]},
    {"r": [["(", 200], ["1", 1], [")", 200]]},
    {"r": [["(", 201], ["1", 1], [")", 201]]},
    {"r": [["[", 199], ["{", 1], ["}", 1], ["]", 199]]},
    {"r": [["[", 200], ["(", 1]]},
    {"r": [["'", 1], ["a", 5000], ["'", 1]]},
    {"r": [["b'", 1], ["\\x41", 2000], ["'", 1]]},
    {"r": [["'", 1], ["\\\n", 300], ["'", 1]]},
    {"r": [["'''", 1], ["\n", 300], ["'''", 1]]},
    {"r": [["(", 1], ["\n", 300], ["1)", 1]]},
    {"r": [["1", 1], ["\n", 300]]},
    {"r": [["\\\n", 300], ["1", 1]]},
]


def _bounds() -> list[dict[str, Any]]:
    return [_row(t) for t in _BOUNDS]


def escape_case(prefix: str, cp: int, suffix: str) -> str:
    return f"{prefix}'{_ESC}{chr(cp)}{suffix}'"


def _answer_literal(text: str) -> str:
    try:
        v = ast.literal_eval(text)
    except Exception:
        return "E"
    if isinstance(v, str):
        return "s:" + "-".join(f"{ord(c):x}" for c in v)
    if isinstance(v, bytes):
        return "b:" + v.hex()
    return "?"


def _sweep_escapes() -> dict[str, Any]:
    """Every code point below U+0250 after a backslash in a str and a bytes
    literal, raw and not, then nothing, ``7`` or ``41``: the value's code
    points (``s:``, hex, joined by ``-``), its bytes (``b:``, hex) or ``E``."""
    answers = []
    for prefix in ("", "b", "r", "rb"):
        for cp in range(0x250):
            for suffix in ("", "7", "41"):
                answers.append(_answer_literal(escape_case(prefix, cp, suffix)))
    return _sweep("escapes", answers)


_ALPHABET = "0123456789_.eEjJxXoObBaAfF+-"


def number_cases(count: int) -> list[str]:
    """``count`` texts of one to seven characters of `_ALPHABET`, drawn by a
    64-bit LCG (Knuth's MMIX constants) from a fixed seed, the high 31 bits
    of each state used."""
    state = 0x2545F4914F6CDD1D
    out = []

    def draw() -> int:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return state >> 33

    for _ in range(count):
        n = 1 + draw() % 7
        out.append("".join(_ALPHABET[draw() % len(_ALPHABET)] for _ in range(n)))
    return out


def _answer_number(text: str) -> str:
    if _parse(text) == "lex":
        return "E"
    toks = _tokens(text)
    try:
        v = ast.literal_eval(text)
    except Exception:
        return "M"
    if toks == "raised" or len(toks) != 1 or toks[0][0] != "NUMBER":
        return "M"
    if isinstance(v, int):
        return f"i:{v}"
    if isinstance(v, float):
        return f"f:{_bits(v)}"
    return f"j:{_bits(v.imag)}"


def _sweep_numbers() -> dict[str, Any]:
    """`number_cases(20000)`: ``i:`` and the decimal, ``f:`` and the bits,
    ``j:`` and the imaginary part's bits for one NUMBER token that reads;
    ``E`` for a tokenizer error; ``M`` otherwise."""
    return _sweep("numbers", [_answer_number(t) for t in number_cases(20000)])


def _sweep(name: str, answers: list[str]) -> dict[str, Any]:
    data = "\n".join(answers).encode()
    return {
        "sweep": name,
        "count": len(answers),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def generate() -> dict[str, bytes]:
    v = sys.version_info
    running = f"{v.major}.{v.minor}.{v.micro}"
    header: dict[str, Any] = {"fixture": "pysyntax_python", "python": REPRODUCES}
    if running != REPRODUCES:
        # Declined: the rows are 3.13.13's alone.
        header["declined"] = running
        return {"pysyntax_python.jsonl": jsonl([header])}
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(_INT_DIGITS)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rows = _explicit() + _bounds()
            rows += [_sweep_escapes(), _sweep_numbers()]
    finally:
        sys.set_int_max_str_digits(limit)
    header["rows"] = len(rows)
    return {"pysyntax_python.jsonl": jsonl([header, *rows])}


def _answer_value(text: str) -> str:
    """`ast.literal_eval` of a number text by value: ``i:``, ``f:`` or ``j:``
    as `_answer_number` spells them; ``E`` when it raises or returns
    anything but a number (``...`` is Ellipsis)."""
    try:
        v = ast.literal_eval(text)
    except Exception:
        return "E"
    if isinstance(v, bool):
        return "E"
    if isinstance(v, int):
        return f"i:{v}"
    if isinstance(v, float):
        return f"f:{_bits(v)}"
    if isinstance(v, complex) and v.real == 0.0:
        return f"j:{_bits(v.imag)}"
    return "E"


def _stable_texts() -> list[Any]:
    texts: list[Any] = []
    for esc in _ESCAPES:
        for prefix in ("", "b", "r", "rb"):
            texts.append(f"{prefix}'{esc}'")
        texts.append(f'"""{esc}"""')
    texts.extend(_STRINGS)
    for prefix in _PREFIXES:
        texts.extend(prefix + body for body in _BODIES)
    texts.extend(_NUMBERS + _LINES + _BOUNDS)
    return texts


def generate_stable() -> dict[str, bytes]:
    """``pysyntax.jsonl``: rows ``{"t", "v"}`` and the sweeps ``escapes``
    and ``values`` (`number_cases` without ``+`` and ``-``, by
    `_answer_value`), the same under every supported interpreter."""
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(_INT_DIGITS)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rows: list[dict[str, Any]] = []
            for t in _stable_texts():
                spelled = _spelled(t) if isinstance(t, str) else t
                rows.append({"t": spelled, "v": _value(_built(t))})
            rows.append(_sweep_escapes())
            cases = [t for t in number_cases(20000) if "+" not in t and "-" not in t]
            rows.append(_sweep("values", [_answer_value(t) for t in cases]))
    finally:
        sys.set_int_max_str_digits(limit)
    header = {"fixture": "pysyntax", "rows": len(rows)}
    return {"pysyntax.jsonl": jsonl([header, *rows])}
