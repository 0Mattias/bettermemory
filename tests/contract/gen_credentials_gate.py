"""Fixtures: the credential value gate (`credentials._is_placeholder`,
`_looks_like_secret`, `_NUMBER_SUFFIX_RE`, `_DATETIME_SHAPE_RE`, the two
placeholder tables).

Two files, one JSON object per line, keys sorted, ASCII:

- ``credentials_gate.jsonl``, not gated. Its first line pins the tables
  and the pattern texts: ``values`` and ``words`` (sorted), ``number_suffix``
  and ``datetime_shape`` (each compiled pattern's text) and ``inline`` (the
  patterns `_is_placeholder` and `_looks_like_secret` hand to ``re``
  inline, read from the two functions' source in source order).

  A case is ``{"t": text, "a": flags}``, with ``"n": [start, end]`` where
  `_NUMBER_SUFFIX_RE` matches in the text (it matches at most once; the
  generator checks that `sub` removes exactly that span). ``flags`` holds
  ``p`` where `_is_placeholder(text)`, ``d`` where
  `_DATETIME_SHAPE_RE.fullmatch(text)` and ``s`` where
  `_looks_like_secret(text)`, in that order. Every function is asked of
  every text. A text holding a surrogate is ``t_cps``; a long text is
  ``{"r": [[piece, count], ...]}`` under ``t`` (the pieces repeated,
  joined).

  A sweep is a digest of many cases in place of their rows; the reader
  makes the same texts by the same rule. Its ``sha256`` is of the lines
  ``flags + ":" + span + "\\n"`` in order, ``span`` being ``start,end`` or
  empty. Case ``i`` of a sweep draws bytes from the stream
  ``SHA-256("credentials-gate <name> <i> <block>")`` for block 0, 1, ...
  (the digests joined), each byte ``b`` choosing ``list[b % len(list)]``:

  - ``{"k": "join", ...}``: the first byte chooses a count from
    ``counts``; each next byte a piece from ``pieces``; the text is the
    pieces joined.
  - ``{"k": "mutate", ...}``: the first byte chooses a text from
    ``bases``; the second ``b % 4`` edits; each edit takes three bytes: the
    operation (``b % 3``: 0 replaces, 1 inserts, 2 deletes), the position
    (``b % (len + 1)``, in code points, of the text as edited so far; a
    replace or delete at ``len`` does nothing) and the character (from
    ``chars``; a delete reads and ignores it).

  Every character outside ASCII is one whose case, digit, space, word and
  letter properties are the same in every Unicode version from 6.3 on, so
  these bytes depend on no Unicode table and no Python release.
- ``credentials_gate_unicode.jsonl``, gated on the Unicode version. Its
  first line lists the templates, each ``[function, before, after]``: the
  text is ``before + chr(cp) + after`` and the function ``s``
  (`_looks_like_secret`), ``p`` (`_is_placeholder`), ``d`` (the datetime
  shape's `fullmatch`) or ``n`` (the number suffix's `search`). Each other
  line is a block of code points ``lo`` to ``hi`` (exclusive) with the
  templates asked (``t``, by index) and the SHA-256 of the lines
  ``hex(cp) + ":" + bits + "\\n"`` (lowercase hex, no ``0x``; one ``1`` or
  ``0`` per template asked). The templates read through `fullmatch` and
  `search` are asked of every code point; the others, which cost a whole
  call of the gate, of every code point below U+40000 (Unicode 15.1.0
  assigns no letter, digit, space or cased character above it).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from collections.abc import Callable, Iterator
from typing import Any

from bettermemory import credentials as cr

from ._common import has_surrogate, jsonl


def _inline_patterns() -> list[str]:
    """The pattern each ``re.<function>(pattern, ...)`` call of the two
    functions is given, in source order."""
    out: list[str] = []
    for fn in (cr._is_placeholder, cr._looks_like_secret):
        tree = ast.parse(inspect.getsource(fn))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
        ]
        calls.sort(key=lambda node: (node.lineno, node.col_offset))
        for call in calls:
            pattern = call.args[0]
            assert isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)
            out.append(pattern.value)
    return out


def _span(text: str) -> list[int] | None:
    found = [m.span() for m in cr._NUMBER_SUFFIX_RE.finditer(text)]
    assert len(found) <= 1, text
    if not found:
        assert cr._NUMBER_SUFFIX_RE.sub("", text) == text
        return None
    start, end = found[0]
    assert cr._NUMBER_SUFFIX_RE.sub("", text) == text[:start] + text[end:]
    return [start, end]


def _flags(text: str) -> str:
    return (
        ("p" if cr._is_placeholder(text) else "")
        + ("d" if cr._DATETIME_SHAPE_RE.fullmatch(text) else "")
        + ("s" if cr._looks_like_secret(text) else "")
    )


def _answers(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"a": _flags(text)}
    span = _span(text)
    if span is not None:
        out["n"] = span
    return out


_S = "hunter2Abc9XyZ12Q"  # a value the gate reads as a secret (17 characters)
_S12 = "Abcdefgh1234"  # one at the length floor
_S11 = "Abcdefg1234"  # one under it


def _strips() -> list[str]:
    spaces = [" ", "\t", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1f", "\x85", "\xa0"]
    spaces += ["\u1680", "\u2003", "\u2028", "\u2029", "\u202f", "\u205f", "\u3000"]
    not_spaces = ["\u200b", "\ufeff", "\x00", "\x1b", "\x7f", "\u180e"]
    out = [_S, _S12, _S11, _S + " ", " " + _S, "\t\n " + _S + " \r\n"]
    for c in spaces + not_spaces:
        out += [c + _S11, _S11 + c, c + _S11 + c]
    for tail in (".", ",", ";", ":", "!", "?", "?!.", "...", "-", "*", ")", "]"):
        out += [_S + tail, _S11 + tail, _S12 + tail]
    out += [
        f'"{_S}"',
        f"'{_S}'",
        f"'\"{_S}\"'",
        f" '{_S}' ",
        f"' {_S11} '",
        f"'\n{_S11}\n'",
        f'{_S}".',
        f'{_S}."',
        f'"{_S}",',
        f"{_S}.'",
        f".{_S}",
        f"'.{_S}'",
        f'"{_S11}"',
        f' "{_S11}" ',
        f'" {_S11}"',
        f"{_S11} x",
        f"{_S}\n{_S}",
        "'\"'\"",
        "",
        " ",
        "''",
        ".",
    ]
    return out


def _closers() -> list[str]:
    out = []
    for body in (_S, _S12, _S11):
        out += [
            body + "}",
            body + "}}",
            "{" + body + "}",
            body + "{}",
            body + ">",
            body + ">>",
            "<" + body + ">",
            body + "<>",
            body + "%",
            body + "%%",
            "%" + body + "%",
            body[:3] + "%" + body[3:] + "%",
            body[:3] + "{" + body[3:] + "}",
            body[:3] + "<" + body[3:] + ">",
            body + "}.",
            body + ".}",
            body + "}>",
            body + ">}",
            body + "%}",
            body + "}%",
            body + ">;",
            body + "};",
            body + "%!",
            body + ")}",
            body + "]>",
        ]
    for head in "$<{%":
        out += [head + _S, head + _S11, _S[:5] + head + _S[5:]]
    out += ["${DB_PASSWORD_VALUE}", "<your-key-here-1A>", "{{token_Value_1}}"]
    out += ["%SECRET_VALUE_1a%", "myapp-${VAR_Name1}", "$" + _S + "}", "}" + _S]
    out += [">" + _S, "100%Abcdefgh12", "Abcdefgh12100%"]
    return out


def _placeholders() -> list[str]:
    values = sorted(cr._PLACEHOLDER_VALUES)
    words = sorted(cr._PLACEHOLDER_WORDS)
    out = []
    for v in values:
        out += [v, v.upper(), v.title(), v + "1", v + "12345", v + "-12", v + "_7"]
        out += [v + "--12", v + "-", v + "1\n", v + "\n", v + "1a", v + "\u0661"]
        out += [v + "1\uff12", v + "\xb2", "1" + v, " " + v, v + "0000000000000"]
    for w in words:
        out += [w, w.upper(), w + "9", w + "_" + w, w + "-12-" + w, w + "x", "x" + w]
        out += [w + "_me_123456", "your_" + w + "_here", w + "\u0661", "9" + w]
    out += [
        "dummy_password_123",
        "test_api_key_12345",
        "replace-me-123",
        "your-api-key-1",
        "changeme12345",
        "CHANGEME12345",
        "Your_Token_Here",
        "dummy__password",
        "-dummy-",
        "_dummy_password_",
        "dummy_password_x",
        "dummy_password_x1",
        "dummypassword",
        "dummy password",
        "dummy.password",
        "1dummy",
        "123_dummy",
        "dummy_123_45_key",
        "dummy1_password2_3",
        "dummy_password_12\u0663",
        "dummy_password_\u0663",
        "12345",
        "123456789012",
        "-_-",
        "---",
        "_",
        "-1",
        "1-",
        "1-2_3",
        "xxxxxxxxxxx",
        "xxxxxxxxxxxxx",
        "XXXXXXXXXXXX",
        "***********",
        "*************",
        "############1",
        "\u212aey",
        "\u212aEY_12345678",
        "your_api_\u212aey",
        "your_api_\u212aey_1",
        "ap\u0130",
        "ap\u0130_key_123456",
        "T\u0130ken",
        "\xc9xample",
        "exampl\xc9",
        "DEMO_\u03a3",
        "test_\u01c5",
        "changeme_1_2",
        "changeme-12_",
        "changeme--12",
        "redacted--12",
        "redacted-_12",
        "redacted12\n",
        "redacted-12\n",
        "redacted12\n\n",
        "redacted\n12",
        "redacted12 ",
        "redacted 12",
        "redacted\u0661\u0662",
        "redacted-\u0661",
        "redacted\U0001d7ce",
        "redacted1\xb2",
        "redacted\xb21",
        "red12acted",
        "null0",
        "true1",
        "false-0",
        "none_1",
        "none12345678",
        "placeholder7",
        "example99",
        "your_api_key1",
        "your-api-key_1",
        "your_token_here-99",
    ]
    return out


def _structured() -> list[str]:
    return [
        # A backslash.
        "C:\\Work\\Abc123xyz",
        "Abc123\\xyzdef99",
        "\\Abc123xyzdef9",
        # The path start.
        "/etc/Abc123xyz99",
        "./Abc123xyz9999",
        "../Abc123xyz999",
        ".../Abc123xyz99",
        "..Abc/123xyz99",
        "~/Abc123xyz9999",
        "~Abc/123xyz9999",
        "C:/Abc123xyz999",
        "c:/abc123xyz999",
        "C:Abc/123xyz999",
        "CD:/Abc123xyz99",
        "9:/Abc123xyz999",
        "\xe9:/Abc123xyz999",
        # A scheme.
        "https://Abc123xyz",
        "Abc123://xyz999",
        "Abc123:/xyz9999",
        "Abc123:/x/yz999",
        "Abc123xyz999://",
        # The relative path.
        "abc123/def456ghi",
        "abc123/Def456ghi",
        "abc123//def456ghi",
        "abc123/def456ghi/",
        "abc.123/de-f_456",
        "abc123/def/456/g",
        "abc123/def456gh\xe9",
        "abc123/def456+gh",
        "abc123/def456gh\n",
        "Zm9vYmFy/MTIzNDU2",
        "Zm9vYmFy/MTIz+DU2==",
        "abc123def456ghi/",
        # The dotted reference.
        "config.SECRET_KEY_V2",
        "hunter2.Abc9XyZ12Q",
        "hunter2Abc9XyZ.12",
        "_private.Value123",
        "9config.Secret12",
        "config.S\xe9cret_12",
        "config.Secret-12",
        "config.Secret 12",
        "config..Secret12",
        "config.Secret12.",
        "config.Secret12..",
        ".config.Secret12",
        "\xe9config.Secret12",
        "A.bcdefgh1234\xb2",
        "A.bcdefgh1234\u0661",
        "A.bcdefgh1234\u4e00",
        "A.bcdefgh1234\u0345",
        "A.bcdefgh1234\u200b",
        "A.bcdefgh1234\n",
        "A.bcdefgh1234\nx",
        "Abcdefgh1234_9",
        "Abcdefgh12349",
        # The call expression.
        "secrets.token_hex(32)",
        "str(uuid4())12",
        "str(uuid4())",
        "f(Abcd1234efgh)",
        "f(Abcd1234\nefgh)",
        "f(Abcd1234\refgh)",
        "f(Abcd1234\u2028efgh)",
        "f-g(Abcd1234efg)",
        "f g(Abcd1234efg)",
        "f(Abcd1234efgh",
        "fAbcd1234efgh)",
        "Abcd1234efgh()",
        "Abcd1234efg()x",
        "Abcd1234efgh(",
        "Abcd1234efg)(",
        "9f(Abcd1234efgh)",
        "_f(Abcd1234efgh)",
        "f.g.h(Abc123)xy)",
        "f((((Abc12345)))",
        "f)Abcd1234efg()",
        "\xe9(Abcd1234efgh)",
        "f\xe9(Abcd1234efgh)",
        "f\xb2(Abcd1234efgh)",
        "f(Abcd1234efgh)\n",
        "f(Abcd1234efg\n)",
        "f(\nAbcd1234efg)",
        # The constant name.
        "DEFAULT_API_KEY_V2",
        "A1B2C3D4_E5F6G7H8",
        "A1B2C3D4_e5F6G7H8",
        "A1B2C3D4__E5F6G7",
        "A1B2C3D4E5F6_",
        "A1B2C3D4E5F6G_H",
        "_A1B2C3D4E5F6G7",
        "1A2B3C4D_E5F6G7H",
        "A1B2C3D4E5F6G7H8",
        "A1B2C3D4-E5F6G7H8",
        "A1B2C3D4_E5F6G7H\xc9",
        "A1B2C3D4_E5F6G7H\u0661",
        "A_1_2_3_4_5_6_B",
        "A1B2C3D4_E5F6G7H8\n",
    ]


def _datetimes() -> list[str]:
    ext = "2026-06-01T09:30"
    out = [
        "2026-01-15T00:30:00Z",
        "2026-06-01T09:30:00+02:00",
        "2026-06-01T09:30:00.123456-0200",
        ext,
        ext[:-1],
        ext + "z",
        ext + "Z",
        ext + "+0200",
        ext + "-02:00",
        ext + ":",
        ext + ":0",
        ext + ":00",
        ext + ":000",
        ext + ":00.",
        ext + ":00.5",
        ext + ":00.5Z",
        ext + ":00.123456789012345678901234567890",
        ext + ":00..5",
        ext + ".5",
        ext + ":00+",
        ext + ":00+0",
        ext + ":00+02",
        ext + ":00+02:",
        ext + ":00+02:0",
        ext + ":00+020",
        ext + ":00+0200",
        ext + ":00+02000",
        ext + ":00+02:00:",
        ext + ":00+02::00",
        ext + ":00+0200Z",
        ext + ":00Z+0200",
        ext + ":00ZZ",
        ext + ":00 Z",
        ext + ":00Z\n",
        ext + ":00\n",
        "\n" + ext + ":00",
        "2026-06-01 09:30:00Z",
        "2026-06-01_09:30:00Z",
        "2026-6-01T09:30:00Z",
        "2026-06-1T09:30:00Z",
        "026-06-01T09:30:00Z",
        "12026-06-01T09:30:00",
        "2026/06/01T09:30:00",
        "2026-06-01T09-30-00",
        "2026-06-01T0930",
        "2026-06-01T09:3",
        "2026-06-01TT09:30",
        "2026-06-01",
        "20260115T003000Z",
        "20260115t003000z",
        "20260115T0030",
        "20260115T00300",
        "20260115T003000",
        "20260115T0030000",
        "20260115T003",
        "20260115T",
        "20260115T0030+0200",
        "20260115T0030-02:00",
        "20260115T003000.5",
        "20260115T0030:00",
        "2026011T0030000Z",
        "202601150T003000",
        "20260115003000Z",
        "20260115T0030Z0",
        "\u0662\u0660\u0662\u0666-\u0660\u0661-\u0661\u0665T\u0660\u0660:\u0663\u0660:\u0660\u0660Z",
        "2026-01-15T00:30:00.\u0661\u0662+\u0660\u0662:\u0660\u0660",
        "2\uff10\uff12\uff16-01-15T00:30:00Z",
        "2026011\U0001d7d3T00\u0663\u0660\uff10\uff10",
        "2026-01-15T00:3\xb2:00Z",
        "2026-01-15T00:30:00.\xb2",
        "2026-01-15\ud800T00:30",
    ]
    return out


def _classes() -> list[str]:
    return [
        # Diversity.
        "aaaaaaa1Aaaa",
        "Abcdef1Abcdef",
        "Abcdefg1Abcdef",
        "A1A1A1A1A1A1A1A1",
        "Aa1Bb2Aa1Bb2Aa",
        "Aa1Bb2Cc3Aa1Bb",
        "\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9\xe9A1",
        "Abcdef\xe9\xc9bcdef1",
        # A separator and no uppercase letter.
        "sha256-hashed-x9",
        "sha256-Hashed-x9",
        "expire_after_30d",
        "bcrypt_hash_v2x1",
        "bcrypt_hash_v2X1",
        "abcdefg-1234\u2160",
        "abcdefg-1234\u24b6",
        "abcdefg-1234\u01c5",
        "abcdefg-1234\xc9",
        "abcdefg-1234\xe9",
        "abcdefg-1234\u212a",
        "abcdefg_1234\u03a3",
        "abcdefg_1234\u03c3",
        "abcdefg.1234+x9",
        "abcdefg+1234=x9",
        # A letter and a digit.
        "abcdefghijkl",
        "abcdefghijk1",
        "Abcdefghijk\xb2",
        "Abcdefghijk\u0661",
        "Abcdefghijk\xbd",
        "Abcdefghijk\u2160",
        "Abcdefghijk\uff11",
        "Abcdefghijk\U0001d7ce",
        "Abcdefghijk\u2082",
        "12345678901\xe9",
        "12345678901\u4e00",
        "12345678901\u2160",
        "12345678901\u0345",
        "12345678901\u24b6",
        "12345678901\ud800",
        "1234567890.5",
        "1234567890+5",
        "+*=&^#@!~|[]12",
        "+*=&^#@!~|[]ab",
        "+*=&^#@!~|[]a1",
        "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341A1",
        "\ud800\ud801\ud802\ud803\ud804\ud805\ud806\ud807\ud808\ud809a1",
        "p4ssw0rd!Hunter2",
        "correct-Horse-battery-9",
        "dGhpcyBpcyBhIHRlc3Q=",
    ]


def _hand() -> list[str]:
    return (
        _strips()
        + _closers()
        + _placeholders()
        + _structured()
        + _datetimes()
        + _classes()
    )


def _long() -> list[list[list[Any]]]:
    return [
        # The length ceiling, and one past it.
        [["aB3", 1], ["xyzwvuts", 127], ["q", 5]],
        [["aB3", 1], ["xyzwvuts", 127], ["q", 6]],
        [["aB3", 1], ["xyzwvuts", 127], ["q", 6], ["}", 1]],
        [["aB3", 1], ["xyzwvuts", 127], ["q", 6], [".", 400]],
        [[" ", 3000], ["aB3xyzwvuts9", 1], ["\n", 3000]],
        [['"', 2000], ["aB3xyzwvuts9", 1], ["'", 2000]],
        [["aB3xyzwvuts9", 400]],
        # A digit run as long as a value may be, and longer.
        [["x", 1], ["7", 1023]],
        [["changeme", 1], ["7", 1016]],
        [["changeme-", 1], ["7", 5000]],
        [["7", 5000], ["a", 1]],
        [["\u0661", 3000]],
        # Many tokens.
        [["dummy_", 170], ["key", 1]],
        [["dummy_", 170], ["kex", 1]],
        [["key-", 2000]],
        [["_", 4000]],
        # A long fraction, a long path, a long call.
        [["2026-06-01T09:30:00.", 1], ["5", 1000], ["Z", 1]],
        [["2026-06-01T09:30:00.", 1], ["5", 1000], ["ZZ", 1]],
        [["abc1/", 204], ["defg", 1]],
        [["abc1/", 204], ["defG", 1]],
        [["f(", 1], ["A1", 510], [")", 1]],
        [["A1_", 340], ["B2", 1]],
        [["a.", 1], ["B1", 500]],
    ]


def _case(text: str) -> dict[str, Any]:
    case = _answers(text)
    if has_surrogate(text):
        case["t_cps"] = [ord(c) for c in text]
    else:
        case["t"] = text
    return case


def _stream(name: str, i: int) -> Iterator[int]:
    block = 0
    while True:
        yield from hashlib.sha256(
            f"credentials-gate {name} {i} {block}".encode()
        ).digest()
        block += 1


def _joined(name: str, i: int, counts: list[int], pieces: list[str]) -> str:
    stream = _stream(name, i)
    count = counts[next(stream) % len(counts)]
    return "".join(pieces[next(stream) % len(pieces)] for _ in range(count))


def _mutated(name: str, i: int, bases: list[str], chars: list[str]) -> str:
    stream = _stream(name, i)
    text = list(bases[next(stream) % len(bases)])
    for _ in range(next(stream) % 4):
        op = next(stream) % 3
        at = next(stream) % (len(text) + 1)
        char = chars[next(stream) % len(chars)]
        if op == 1:
            text.insert(at, char)
        elif at < len(text):
            if op == 0:
                text[at] = char
            else:
                del text[at]
    return "".join(text)


def _digest(texts: Iterator[str]) -> str:
    h = hashlib.sha256()
    for text in texts:
        span = _span(text)
        spelled = "" if span is None else f"{span[0]},{span[1]}"
        h.update(f"{_flags(text)}:{spelled}\n".encode())
    return h.hexdigest()


_ASCII = list("abcxyzKEYQkey0189-_.:/\\~${}<>%()\"' \n\tTZtz+,;!?*#=")
_SECRETISH = list("abcdefghXYZKQ0123456789abcdefghXYZKQ0123456789") + list("-_./()}%")
_WORDISH = sorted(cr._PLACEHOLDER_VALUES | cr._PLACEHOLDER_WORDS) + [
    "-", "_", "--", "1", "12", "-1", "_9", "\n", "X", "x", "\u0661", " ", ".",
    "KEY", "Token", "\u212a",
]  # fmt: skip
_PATHISH = list("abc019_.-/~:ABC\\") + ["/", "/", "./", "../", "://"]
_CONSTISH = list("ABCZ019_a") + ["_", "__", "A1", "KEY"]
_CALLISH = ["f", "A", "_", ".", "(", ")", "x1", "\n", " ", "\xe9", ",", "9", "(", ")"]
_STABLE = [
    "a", "A", "1", "-", "_", ".", "b", "Z", "7", "x", "\xe9", "\xc9", "\u0661",
    "\xb2", "\u212a", "\u0130", "\xa0", "\u3000", "\x85", "\u2160", "\u01c5",
    "\u4e00", "\uff11", "\U0001d7ce", "\u24b6", "\u0345", "\u200b", "\u03a3",
    "\u03c3", "\u03c2", "\xbd", "\u2082", "\x1c",
]  # fmt: skip

#: The join sweeps: (name, counts, pieces, cases).
JOINS: list[tuple[str, list[int], list[str], int]] = [
    ("ascii", list(range(0, 31)), _ASCII, 9000),
    ("secretish", [11, 12, 12, 13, 14, 16, 20, 24], _SECRETISH, 9000),
    ("words", [1, 2, 3, 4, 5, 6], _WORDISH, 8000),
    ("paths", list(range(10, 21)), _PATHISH, 6000),
    ("constants", list(range(11, 19)), _CONSTISH, 5000),
    ("calls", list(range(10, 19)), _CALLISH, 5000),
    ("stable", list(range(0, 21)), _STABLE, 8000),
]

_BASES = [
    _S, _S12, _S11, _S + "}", "{" + _S + "}", _S + "%", "<" + _S + ">", "$" + _S,
    '"' + _S + '"', " " + _S + ".", "changeme12345", "your-api-key-1",
    "dummy_password_123", "your_token_here", "redacted-12", "xxxxxxxxxxxx",
    "/etc/Abc123xyz99", "../Abc123xyz999", "~/Abc123xyz9999", "C:/Abc123xyz999",
    "https://Abc123xyz", "abc123/def456ghi", "Zm9vYmFy/MTIzNDU2",
    "config.SECRET_KEY_V2", "hunter2.Abc9XyZ12Q", "secrets.token_hex(32)",
    "f(Abcd1234efgh)", "DEFAULT_API_KEY_V2", "A1B2C3D4_E5F6G7H8",
    "2026-01-15T00:30:00Z", "2026-06-01T09:30:00+02:00",
    "2026-06-01T09:30:00.123456-0200", "2026-06-01t09:30", "20260115T003000Z",
    "20260115T0030+0200", "20260115T00300", "Abcdef1Abcdef", "Abcdefg1Abcdef",
    "sha256-hashed-x9", "sha256-Hashed-x9", "abcdefghijk1", "Abcdefghijk\xb2",
    "12345678901\xe9", "C:\\Work\\Abc123xyz",
]  # fmt: skip
_EDITS = list("aZ09-_.:/\\~${}<>%()\"' \n\tTtz+,;!?") + [
    "\xe9", "\xc9", "\u0661", "\xb2", "\u2160", "\xa0", "\u212a",
]  # fmt: skip

#: The mutate sweeps: (name, bases, chars, cases).
MUTATIONS: list[tuple[str, list[str], list[str], int]] = [
    ("edits", _BASES, _EDITS, 24000),
]


def generate() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "k": "gate",
            "values": sorted(cr._PLACEHOLDER_VALUES),
            "words": sorted(cr._PLACEHOLDER_WORDS),
            "number_suffix": cr._NUMBER_SUFFIX_RE.pattern,
            "datetime_shape": cr._DATETIME_SHAPE_RE.pattern,
            "inline": _inline_patterns(),
        }
    ]
    seen: set[str] = set()
    for text in _hand():
        if text not in seen:
            seen.add(text)
            records.append(_case(text))
    for recipe in _long():
        text = "".join(piece * count for piece, count in recipe)
        records.append({"t": {"r": recipe}, **_answers(text)})
    for name, counts, pieces, n in JOINS:
        digest = _digest(_joined(name, i, counts, pieces) for i in range(n))
        records.append(
            {
                "k": "join",
                "name": name,
                "counts": counts,
                "pieces": pieces,
                "n": n,
                "sha256": digest,
            }
        )
    for name, bases, chars, n in MUTATIONS:
        digest = _digest(_mutated(name, i, bases, chars) for i in range(n))
        records.append(
            {
                "k": "mutate",
                "name": name,
                "bases": bases,
                "chars": chars,
                "n": n,
                "sha256": digest,
            }
        )
    return {"credentials_gate.jsonl": jsonl(records)}


def _searched(text: str) -> bool:
    return cr._NUMBER_SUFFIX_RE.search(text) is not None


def _shaped(text: str) -> bool:
    return cr._DATETIME_SHAPE_RE.fullmatch(text) is not None


_ASK: dict[str, Callable[[str], bool]] = {
    "s": cr._looks_like_secret,
    "p": cr._is_placeholder,
    "d": _shaped,
    "n": _searched,
}

#: The templates of the Unicode sweep: (function, before, after).
UNICODE_TEMPLATES: list[tuple[str, str, str]] = [
    ("d", "2026-01-15T00:3", ""),  # `\d` in the datetime shape
    ("n", "x", ""),  # `\d` in the number suffix
    ("s", "", _S11),  # `str.strip`
    ("p", "", "ey"),  # `str.lower`
    ("s", "A.bcdefgh1234", ""),  # `\w`
    ("s", "abcdefg-1234", ""),  # `str.isupper`
    ("s", "12345678901", ""),  # `str.isalpha`
    ("s", "Abcdefghijk", ""),  # `str.isdigit`
]
#: The templates asked of every code point; the rest stop at `_WHOLE_BELOW`.
_EVERYWHERE = (0, 1)
_WHOLE_BELOW = 0x40000
_BLOCK = 0x4000


def generate_unicode() -> dict[str, bytes]:
    records: list[Any] = [
        {"k": "unicode", "templates": [list(t) for t in UNICODE_TEMPLATES]}
    ]
    for lo in range(0, 0x110000, _BLOCK):
        asked = (
            tuple(range(len(UNICODE_TEMPLATES))) if lo < _WHOLE_BELOW else _EVERYWHERE
        )
        plan = [
            (
                _ASK[UNICODE_TEMPLATES[t][0]],
                UNICODE_TEMPLATES[t][1],
                UNICODE_TEMPLATES[t][2],
            )
            for t in asked
        ]
        h = hashlib.sha256()
        for cp in range(lo, lo + _BLOCK):
            c = chr(cp)
            bits = "".join(
                "1" if ask(before + c + after) else "0" for ask, before, after in plan
            )
            h.update(f"{cp:x}:{bits}\n".encode())
        records.append(
            {"lo": lo, "hi": lo + _BLOCK, "t": list(asked), "sha256": h.hexdigest()}
        )
    return {"credentials_gate_unicode.jsonl": jsonl(records)}
