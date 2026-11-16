"""Fixture: what ``tomllib.loads`` makes of TOML documents (``toml.jsonl``).

The native build reads config.toml and the managed file with a TOML reader
of its own; this pins it to CPython's. Each line is one document and what
``tomllib.loads`` returned for it, or the exception it raised:

- ``{"doc": text, "ok": value}``: the table, tagged so that nothing is
  lost: ``{"t": "table", "v": [[key, value], ...]}`` in insertion order,
  ``array``, ``str``, ``bool``, ``int`` (``v`` its decimal text, or
  ``hex`` its ``hex()`` where ``str()`` refuses one past 4300 digits), ``float`` (``bits``, the IEEE-754 bits in hex: inf, nan and
  its sign, and -0.0 kept), ``datetime`` (``v`` its isoformat, with the
  offset when it has one), ``date`` and ``time`` (their isoformat).
- ``{"doc": text, "err": class, "msg": message}``: ``TOMLDecodeError``
  with its whole message, location included; ``ValueError`` (an integer
  past ``int``'s 4300-digit limit, which ``tomllib`` does not wrap); or
  ``RecursionError`` (nesting past the interpreter's stack), whose message
  is not recorded.
- ``{"doc": text, "ok_sha256": hex}``: a value whose tagged form nests
  deeper than 100 JSON levels (JSON readers stop at some depth), by the
  SHA-256 of that form written as the file writes a line (sorted keys,
  compact, ASCII).

A document holding two backslashes in a row is written as ``doc_hex``,
its UTF-8 in hex, in place of ``doc``.

The documents are built by rule: each escape, each control character in
each kind of string, each number form with and without signs and
underscores, each date-time boundary, each redefinition rule, and every
refusal ``Lib/tomllib/_parser.py`` raises, each at least once. The first
line is a header naming the counts.

Not gated. ``tomllib`` reads ASCII syntax and no Unicode data; its
messages print keys through ``repr``, which consults ``str.isprintable``,
so the documents' non-ASCII keys are restricted to characters whose
printability no Unicode release since 6.0 changed (letters, CJK, an
emoji, and the format, space and private-use characters that are never
printable). Floats are read by CPython's own correctly rounded
conversion, the same on every platform, and ``tomllib``'s source is the
same in every 3.13 patch release. The deep-nesting documents sit far from
the interpreter's limit (50 and 150 levels, which load, and 400 to 1,000, which
do not), since where between them it stops depends on the caller's stack.
"""

from __future__ import annotations

import hashlib
import struct
import sys
import tomllib
from datetime import date, datetime, time
from typing import Any

from ._common import dumps_line


def tagged(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        try:
            return {"t": "int", "v": str(value)}
        except ValueError:
            # Past `str()`'s 4300-digit limit: its hex text instead.
            return {"t": "int", "hex": hex(value)}
    if isinstance(value, float):
        bits = struct.unpack(">Q", struct.pack(">d", value))[0]
        return {"t": "float", "bits": f"{bits:016x}"}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, datetime):
        return {"t": "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, time):
        return {"t": "time", "v": value.isoformat()}
    if isinstance(value, list):
        return {"t": "array", "v": [tagged(v) for v in value]}
    if isinstance(value, dict):
        return {"t": "table", "v": [[k, tagged(v)] for k, v in value.items()]}
    raise TypeError(type(value).__name__)


def outcome(doc: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(doc)
    except tomllib.TOMLDecodeError as exc:
        return {"err": "TOMLDecodeError", "msg": str(exc)}
    except RecursionError:
        return {"err": "RecursionError"}
    except ValueError as exc:
        return {"err": "ValueError", "msg": str(exc)}
    shown = tagged(value)
    if _depth(shown) > _MAX_JSON_DEPTH:
        text = dumps_line(shown)
        return {"ok_sha256": hashlib.sha256(text.encode("ascii")).hexdigest()}
    return {"ok": shown}


#: JSON readers stop at some depth (serde_json at 128); a tagged value
#: nested deeper is recorded by the SHA-256 of its `dumps_line` text.
_MAX_JSON_DEPTH = 100


def _depth(value: Any) -> int:
    deepest, stack = 0, [(value, 1)]
    while stack:
        item, depth = stack.pop()
        deepest = max(deepest, depth)
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return deepest


_CONTROLS = [chr(i) for i in range(32)] + ["\x7f"]

_ESCAPES = [
    "b",
    "t",
    "n",
    "f",
    "r",
    '"',
    "\\",
    "a",
    "x",
    "'",
    "0",
    "e",
    "/",
    " ",
    "U",
    "u",
]

_HEX4 = [
    "0000",
    "0041",
    "00e9",
    "00E9",
    "D7FF",
    "d800",
    "DBFF",
    "DC00",
    "DFFF",
    "E000",
    "FFFF",
]
_HEX8 = [
    "00000041",
    "0001F600",
    "0010FFFF",
    "00110000",
    "0000D800",
    "FFFFFFFF",
    "0001f600",
]

_INTS = [
    "0",
    "+0",
    "-0",
    "1",
    "+1",
    "-1",
    "42",
    "1_000",
    "1_2_3",
    "1__0",
    "1_",
    "_1",
    "01",
    "00",
    "-01",
    "+_1",
    "0_0",
    "9223372036854775807",
    "9223372036854775808",
    "-9223372036854775808",
    "-9223372036854775809",
    "1" + "0" * 40,
    "0x0",
    "0xDEAD_beef",
    "0x_1",
    "0x",
    "0X1F",
    "0xg",
    "-0x1",
    "+0x1",
    "0x1_",
    "0o0",
    "0o755",
    "0o8",
    "0O7",
    "0o_7",
    "-0o7",
    "0b0",
    "0b1010",
    "0b2",
    "0B1",
    "0b1__0",
    "+0b1",
    "0x" + "f" * 40,
    "0b" + "1" * 130,
    "0o" + "7" * 50,
    "-" + "1" * 4300,
    "1" * 4301,
    "1_" + "1" * 4300,
    "0x" + "f" * 3600,
]

_FLOATS = [
    "0.0",
    "+0.0",
    "-0.0",
    "1.5",
    "-1.5",
    "+1.5",
    "3.14159",
    "1e5",
    "1E5",
    "1e+5",
    "1e-5",
    "-1e-5",
    "1.5e10",
    "1.5E-10",
    "6.626e-34",
    "1_000.000_1",
    "1e1_0",
    "1.",
    ".5",
    "1.e5",
    "1e",
    "1e+",
    "1.5e",
    "1._5",
    "1_.5",
    "1.5_",
    "1e_5",
    "0.1",
    "0.30000000000000004",
    "1e308",
    "1.7976931348623157e308",
    "1e309",
    "-1e309",
    "5e-324",
    "2e-324",
    "1e-400",
    "-1e-400",
    "00.1",
    "-01.5",
    "0e0",
    "0.0e-0",
    "inf",
    "+inf",
    "-inf",
    "nan",
    "+nan",
    "-nan",
    "Inf",
    "NaN",
    "infinity",
    "9007199254740993",
    "9007199254740993.0",
    "0." + "1" * 400,
    "1" + "0" * 30 + ".0",
]

_DATETIMES = [
    "1979-05-27T07:32:00Z",
    "1979-05-27T07:32:00z",
    "1979-05-27t07:32:00Z",
    "1979-05-27 07:32:00Z",
    "1979-05-27T00:32:00-07:00",
    "1979-05-27T00:32:00+07:00",
    "1979-05-27T00:32:00.999999-07:00",
    "1979-05-27T00:32:00.9999999-07:00",
    "1979-05-27T00:32:00.1+00:00",
    "1979-05-27T00:32:00-00:00",
    "1979-05-27T00:32:00+23:59",
    "1979-05-27T00:32:00+24:00",
    "1979-05-27T00:32:00+1:00",
    "1979-05-27T00:32:00+01",
    "1979-05-27T00:32:00+01:60",
    "1979-05-27T07:32:00",
    "1979-05-27T07:32:00.123",
    "1979-05-27T07:32:00.",
    "1979-05-27T07:32",
    "1979-05-27T7:32:00",
    "1979-05-27T24:00:00",
    "1979-05-27T23:60:00",
    "1979-05-27T23:59:60",
    "1979-05-27",
    "1979-05-27T",
    "1979-05-27 ",
    "1979-5-27",
    "1979-05-7",
    "79-05-27",
    "0000-01-01",
    "0001-01-01",
    "9999-12-31",
    "9999-12-31T23:59:59.999999-23:59",
    "0001-01-01T00:00:00+23:59",
    "2000-02-29",
    "2001-02-29",
    "1900-02-29",
    "2004-02-30",
    "2021-04-31",
    "2021-06-31",
    "2021-13-01",
    "2021-00-01",
    "2021-01-00",
    "2021-01-32",
    "2021-12-31T23:59:59Zjunk",
    "07:32:00",
    "07:32:00.5",
    "00:00:00.000001",
    "07:32:00.1234567",
    "07:32",
    "24:00:00",
    "07:32:60",
    "7:32:00",
    "07:32:00Z",
    "07:32:00-07:00",
]


def _cases() -> list[str]:
    docs: list[str] = []
    add = docs.append

    # Each control character in each kind of string, a comment and a key.
    for ch in _CONTROLS:
        add(f'a = "x{ch}y"\n')
        add(f"a = 'x{ch}y'\n")
        add(f'a = """x{ch}y"""\n')
        add(f"a = '''x{ch}y'''\n")
        add(f"# c{ch}c\na = 1\n")
    for ch in ["\x00", "\x7f", "\r", "\x0b", "\x0c"]:
        add(f"a{ch} = 1\n")
        add(f"a = 1{ch}\n")
        add(f"a = [1,{ch}2]\n")
        add(f"{ch}a = 1\n")

    # Each escape, in a basic and a multi-line basic string.
    for esc in _ESCAPES:
        add(f'a = "x\\{esc}y"\n')
        add(f'a = """x\\{esc}y"""\n')
    for h in _HEX4:
        add(f'a = "\\u{h}"\n')
        add(f'a = """\\u{h}"""\n')
    for h in _HEX8:
        add(f'a = "\\U{h}"\n')
    for short in ["\\u", "\\u1", "\\u12", "\\u123", "\\u12G4", "\\U0001F60", "\\u00é9"]:
        add(f'a = "{short}"\n')
    add('a = "\\')
    add('a = "x\\')

    # Multi-line strings: the trimmed first newline, line-ending
    # backslashes, quotes at the end, CRLF.
    for body in [
        '"""\nabc"""',
        '"""\n\nabc"""',
        '"""abc\n"""',
        '"""a\\\n   b"""',
        '"""a\\  \n\n  b"""',
        '"""a\\ b"""',
        '"""a\\\t\n b"""',
        '"""a\\   """',
        '"""a""""',
        '"""a"""""',
        '"""a""""""',
        '"""""""',
        '""""""',
        '"""\\"""',
        '"""a"b""c"""',
        '"""',
        '"""abc',
        '"""a\\\n',
        "'''\nabc'''",
        "'''a''''",
        "'''a'''''",
        "'''a''''''",
        "''''''",
        "'''''''",
        "'''a\\nb'''",
        "'''abc",
        "'''a\nb'''",
        '"""a\r\nb"""',
        '"""a\rb"""',
        "'''a\rb'''",
        '"""\\\n"""',
        '"""\\\n  \n  x"""',
        '"""\\ \t \n  x"""',
        '"""x\\ \t x"""',
    ]:
        add(f"a = {body}\n")
        add(f"a = {body}")
    for body in [
        '""',
        "''",
        '"a\'b"',
        "'a\"b'",
        "'a\\b'",
        '"\\""',
        "'",
        '"',
        '"abc',
        "'abc",
        '"a\nb"',
        "'a\nb'",
        '"é😀"',
        "'é😀'",
        '"\\\\"',
        '"x" "y"',
    ]:
        add(f"a = {body}\n")

    # Numbers, alone, signed, in arrays, and with trailing junk.
    for text in _INTS + _FLOATS:
        add(f"a = {text}\n")
    for text in ["1", "1.5", "0x1", "inf", "nan", "1979-05-27", "07:32:00", "true"]:
        add(f"a = [{text},{text}]\n")
        add(f"a = {text}x\n")
        add(f"a = {text} # c\n")
        add(f"a = {{b = {text}}}\n")

    # Dates and times.
    for text in _DATETIMES:
        add(f"a = {text}\n")
        add(f"a = [{text}]\n")

    # Booleans and bad values.
    for text in [
        "true",
        "false",
        "True",
        "FALSE",
        "tru",
        "fals",
        "truex",
        "falsey",
        "t",
        "",
        " ",
        "#",
        "=",
        "+",
        "-",
        "++1",
        "--1",
        "+-1",
        ".",
        "x",
        "null",
        "[",
        "{",
        "]",
        "}",
        "@",
        "é",
        "1 2",
        "[1 2]",
        "{a=1 b=2}",
    ]:
        add(f"a = {text}\n")
        add(f"a = {text}")

    # Arrays.
    for text in [
        "[]",
        "[ ]",
        "[1]",
        "[1,]",
        "[1,2,]",
        "[,]",
        "[,1]",
        "[1,,2]",
        "[\n1,\n2\n]",
        "[ # c\n1 # d\n, # e\n2 # f\n]",
        "[[1],[2,[3]]]",
        "[1, 'a', 2.5, true, [], {}]",
        "[{a=1},{a=2}]",
        "[[]]",
        "[\n]",
        "[1\n,2]",
        "[1",
        "[1,",
        "[1,2",
        "[1 # c",
        "[1 # c\n",
        "[[1]",
        "[1]]",
        "[1}",
        "[{a=1}",
        "[\x00]",
        "[1,# c\x00\n2]",
        "[ \t1\t, \t2\t]",
        "[\r\n1\r\n]",
        "[1\r2]",
    ]:
        add(f"a = {text}\n")

    # Inline tables.
    for text in [
        "{}",
        "{ }",
        "{a = 1}",
        "{a=1,b=2}",
        "{a=1,}",
        "{,}",
        "{a=1,,b=2}",
        "{a.b = 1}",
        "{a.b = 1, a.c = 2}",
        "{a = 1, a = 2}",
        "{a = 1, a.b = 2}",
        "{a.b = 1, a = 2}",
        "{a = {b = 1}, a.c = 2}",
        "{a = {b = 1}, a.b = 2}",
        "{a = [1], a.b = 2}",
        "{a.b = 1, a.b.c = 2}",
        "{a = {}, b = {}}",
        "{\na = 1}",
        "{a = 1\n}",
        "{a = 1",
        "{a = 1,",
        "{a 1}",
        "{a = }",
        "{= 1}",
        "{'a' = 1, \"a\" = 2}",
        "{'a b' = 1}",
        '{"a.b" = 1, a.b = 2}',
        "{a = {b = {c = {d = 1}}}}",
        "{a.b.c.d = 1}",
        "{a = [{b = 1}]}",
        "{1 = 2}",
        "{a = 1 # c\n}",
        "{a=1}}",
        '{a = """x\ny"""}',
        "{a = 1, b.c = 2, b.d = 3, e = {f = 4}}",
        "{a.b = {c = 1}, a.b.d = 2}",
        "{a = [], a = []}",
        '{"" = 1}',
        "{'' = 1, \"\" = 2}",
    ]:
        add(f"t = {text}\n")

    # Keys.
    for text in [
        "a",
        "A-b_c",
        "1234",
        "-",
        "_",
        "a.b",
        "a . b",
        "a\t.\tb",
        '"a.b"',
        "'a.b'",
        '""',
        "''",
        '"é"',
        "'é'",
        '"\\u00e9"',
        "a.'b'.\"c\"",
        "a.",
        ".a",
        "a..b",
        "a b",
        "é",
        "3.14",
        "1.2.3",
        "true",
        "inf",
        "nan",
        "a.true",
        '"a\\nb"',
        "'a\\nb'",
        '"\\u200b"',
        '"\\u00a0"',
        '"\\ue000"',
        '"中"',
        '"😀"',
        '"\\u0000"',
        'a."".b',
        "'''a'''",
        '"""a"""',
        "a = 1",
        "[a]",
    ]:
        add(f"{text} = 1\n")
        add(f"[{text}]\n")
    add("a = 1\na = 2\n")
    add('a = 1\n"a" = 2\n')
    add("a = 1\n'a' = 2\n")
    add('"é" = 1\n"\\u00e9" = 2\n')
    add("é = 1\n")
    add("a\n")
    add("a =\n")
    add("a = 1 2\n")
    add("a = 1 = 2\n")
    add("= 1\n")
    add("a == 1\n")
    add("a = # c\n")

    # Tables, dotted keys and arrays of tables: every redefinition rule.
    for text in [
        "[a]\n[a]\n",
        "[a]\nb = 1\n[a]\n",
        "[a.b]\n[a]\n",
        "[a]\n[a.b]\n",
        "[a]\nb = 1\n[a.b]\n",
        "[a.b]\nc = 1\n[a]\nd = 2\n",
        "[a.b]\n[a]\nb = 1\n",
        "[a]\nb.c = 1\n[a.b]\n",
        "[a]\nb.c = 1\n[a.b.d]\n",
        "a.b = 1\n[a]\n",
        "a.b = 1\n[a.c]\n",
        "a = 1\n[a]\n",
        "a = 1\n[a.b]\n",
        "a = {}\n[a]\n",
        "a = {b = 1}\n[a.c]\n",
        "a = {}\na.b = 1\n",
        "a = {b = 1}\na.c = 1\n",
        "a = []\n[[a]]\n",
        "a = [{}]\n[[a]]\n",
        "a = []\n[a.b]\n",
        "[[a]]\n[a]\n",
        "[a]\n[[a]]\n",
        "[[a]]\n[[a]]\n",
        "[[a]]\nb = 1\n[[a]]\nb = 2\n",
        "[[a]]\n[a.b]\nc = 1\n[[a]]\n[a.b]\nc = 2\n",
        "[[a]]\n[[a.b]]\n[[a.b]]\n[[a]]\n",
        "[[a.b]]\n[a]\n",
        "[[a.b]]\n[[a]]\n",
        "[a]\n[[a.b]]\n[a.b.c]\n",
        "[[a]]\nb = [1]\n[a.b]\n",
        "[[a]]\n[a.b]\n[a.b]\n",
        "[a.b.c]\n[a.b]\n[a]\n",
        "[a.b.c]\n[a.b.c]\n",
        "[a]\nb = 1\nb.c = 1\n",
        "[a]\nb.c = 1\nb = 1\n",
        "a.b.c = 1\na.b.d = 2\na.e = 3\n",
        "a.b.c = 1\na.b = 2\n",
        "[a]\nb.c = 1\n[x]\n[a]\n",
        "[a]\nb.c=1\n[a.b.c]\n",
        "[a.b]\nc.d = 1\n[a.b.c]\n",
        "a.b = 1\n[a.b]\n",
        "[x]\na.b = 1\n[x.a]\n",
        "[x]\na.b = 1\n[x.a.c]\n",
        "[[x]]\na.b = 1\n[x.a]\n",
        "[ a ]\n",
        "[ a . b ]\n",
        "[\ta\t]\n",
        "[a]]\n",
        "[a\n",
        "[a",
        "[]\n",
        "[[]]\n",
        "[[a]\n",
        "[[a]]]\n",
        "[[a] ]\n",
        "[ [a]]\n",
        "[a] b = 1\n",
        "[a] # c\n",
        "[a]#c\n",
        "[a.b] x\n",
        "[a.'b c'.\"d\"]\n",
        '[""]\n[""]\n',
        '[a]\n"".b = 1\n',
        '[a.""]\n',
        "[[a]]\n[a.b]\n[a.b.c]\nd=1\n",
        "a = [{b = 1}]\n[a.c]\n",
        "a = [{b = 1}]\n[[a]]\n",
        "[[a]]\nb={}\n[a.b]\n",
        "[x.y]\n[x]\ny.z = 1\n",
        "[x]\ny.z = 1\n[x.y.w]\n",
        "[x]\ny = 1\n[x.y.z]\n",
        "a.b = 1\nb.a = 2\n[c]\na.b = 3\n",
        "[a]\n[b]\n[a.c]\n[b.c]\n",
        "[[fruit]]\nname = 'apple'\n[fruit.physical]\ncolor = 'red'\n[[fruit.variety]]\n"
        "name = 'red delicious'\n[[fruit]]\nname = 'banana'\n[[fruit.variety]]\n"
        "name = 'plantain'\n",
    ]:
        add(text)

    # Whitespace, comments, line endings, a BOM, NUL.
    for text in [
        "",
        " ",
        "\n",
        "\n\n\n",
        "\t\n",
        "# c",
        "# c\n",
        "#",
        "a = 1",
        "a = 1\n\n",
        "  a = 1  \n",
        "\ta\t=\t1\t\n",
        "a=1",
        "a = 1 # c",
        "a = 1 #",
        "a = 1#c\n",
        "a = 1\r\nb = 2\r\n",
        "a = 1\rb = 2\n",
        "a = 1\r",
        "\r\n",
        "\r",
        "a = 1\r\n# c\r\n",
        "\ufeffa = 1\n",
        "a = 1\n\ufeff",
        "\x00",
        "a = 1\n\x00",
        "a = '\x00'\n",
        "# é 😀 \u200b\na = 1\n",
        "a = 1 # \x7f\n",
        "a = 1 # \t\n",
        "[a]\r\nb = 1\r\n",
        "a = '''x\r\ny'''\r\n",
        'a = """x\\\r\n  y"""\r\n',
        "a = 1\n b = 2\n",
        "a = 1\n\n\n\nb = 2\nb = 3\n",
        "a = 1\nb = é\n",
        "x = 1\ny = 2\nz = 'abc\n",
    ]:
        add(text)

    # Nesting, far from the stack limit on either side.
    for depth in (50, 150):
        add("a = " + "[" * depth + "]" * depth + "\n")
        add("a = " + "{b = " * depth + "1" + "}" * depth + "\n")
    add("a = " + "[" * 1000 + "]" * 1000 + "\n")
    add("a = " + "{b = " * 700 + "1" + "}" * 700 + "\n")
    add("a = " + "[{b = " * 400 + "1" + "}]" * 400 + "\n")
    add("a = " + "[" * 1000 + "1,\n")

    # Long lines and many keys.
    add("a = '" + "x" * 2000 + "'\n")
    add('a = "' + "\\u00e9" * 500 + '"\n')
    add("".join(f"k{i} = {i}\n" for i in range(300)))
    add("".join(f"[t{i}]\nk = {i}\n" for i in range(100)))
    add("".join(f"a.b{i}.c = {i}\n" for i in range(50)) + "a.b0.c = 1\n")
    add("a = [" + ",".join(str(i) for i in range(500)) + "]\n")
    return docs


def _doc_field(doc: str) -> dict[str, str]:
    """The document as text, or as its UTF-8 in hex when it holds two
    backslashes in a row: escaped again in the file they would read as a
    UNC path to test_no_golden_file_carries_a_host_fact."""
    if "\\\\" in doc:
        return {"doc_hex": doc.encode("utf-8").hex()}
    return {"doc": doc}


def generate() -> dict[str, bytes]:
    # The rules overlap in places; each document is kept once, first seen.
    docs = list(dict.fromkeys(_cases()))
    records = [{**_doc_field(doc), **outcome(doc)} for doc in docs]
    counts: dict[str, int] = {}
    for record in records:
        kind = record.get("err", "ok")
        counts[kind] = counts.get(kind, 0) + 1
    header = {
        "fixture": "toml",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "documents": len(records),
        "outcomes": counts,
    }
    lines = [dumps_line(header)] + [dumps_line(r) for r in records]
    return {"toml.jsonl": ("\n".join(lines) + "\n").encode("ascii")}
