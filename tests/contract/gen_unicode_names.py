"""Fixture: the names ``\\N{...}`` reads in a str literal, and names past
ASCII (``unicode_names.jsonl``).

The native build reads it in
``native/crates/bettermemory-core/src/unicode/names.rs`` and
``src/pysyntax/lex.rs``. The reference reads ``ast.literal_eval`` in
``claims.py``; CPython's ``unicode_escape`` decoder resolves ``\\N{name}``
through ``unicodedata._ucnhash_CAPI``'s ``getcode`` (names and aliases,
never named sequences).

The header pins two sets by count and the SHA-256 of
``'<name>\\t<code point>'`` lines joined by ``\\n``:

- ``table``: every name ``unicodedata.name`` gives that no rule makes,
  and every alias, sorted by name (the native table's order);
- ``ruled``: every name made by a rule, in code point order: the Hangul
  syllables from the short names the header lists, and each prefix of
  ``ranges`` followed by the code point as ``%04X``.

Each of those names is checked through ``ast.literal_eval`` of
``'\\N{name}'``. The aliases are not listed by any public API; they are
read through ``getname`` of the capsule (3.13's
``Include/internal/pycore_ucnhash.h``: ``getname(code, buffer, buflen,
with_alias_and_seq)``, ``getcode(name, namelen, &code,
with_named_seq)``), where ``unicodedata`` keeps them at U+F0000 on and
the named sequences at U+F0200 on, and the rule ranges through
``getcode`` of each prefix.

Then rows of whole texts with what ``literal_eval`` returns (a str as
code points, another value by ``repr``) or raises: case variants, every
rule range at both ends and one past, each rule prefix at the first code
point of every range of another prefix, hex spellings (up to nine
digits), Hangul spellings, every alias, every named sequence, malformed
escapes, a name holding a code point past ASCII whose low byte would
spell a name, and names past ASCII (the tokenizer's identifier check over the raw run, then the
parser's NFKC, of which only ``set`` evaluates).

Gated on Unicode 15.1.0: under another version the generator writes the
header alone, naming the version.
"""

from __future__ import annotations

import ast
import ctypes
import hashlib
import unicodedata
import warnings
from collections.abc import Callable
from typing import Any

from ._common import jsonl, text_field

UNIDATA = "15.1.0"
HANGUL_FIRST, HANGUL_LAST = 0xAC00, 0xD7A3
HANGUL_PREFIX = "HANGUL SYLLABLE "
RULE_PREFIXES = (
    "CJK UNIFIED IDEOGRAPH-",
    "CJK COMPATIBILITY IDEOGRAPH-",
    "TANGUT IDEOGRAPH-",
    "KHITAN SMALL SCRIPT CHARACTER-",
    "NUSHU CHARACTER-",
    "EGYPTIAN HIEROGLYPH-",
)
ALIASES_START, NAMED_SEQUENCES_START = 0xF0000, 0xF0200


def _capi() -> tuple[Callable[[int, int], str | None], Callable[[str], int | None]]:
    get_pointer = ctypes.pythonapi.PyCapsule_GetPointer
    get_pointer.restype = ctypes.c_void_p
    get_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    capsule = getattr(unicodedata, "_ucnhash_CAPI")
    address = get_pointer(capsule, b"unicodedata._ucnhash_CAPI")
    getname_ptr, getcode_ptr = (ctypes.c_void_p * 2).from_address(address)
    assert getname_ptr is not None and getcode_ptr is not None
    getname_fn = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_int, ctypes.c_int
    )(getname_ptr)
    getcode_fn = ctypes.CFUNCTYPE(
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_int,
    )(getcode_ptr)

    def getname(code: int, with_alias: int) -> str | None:
        buffer = ctypes.create_string_buffer(256)
        if not getname_fn(code, buffer, 256, with_alias):
            return None
        return buffer.value.decode("ascii")

    def getcode(name: str) -> int | None:
        raw = name.encode("ascii")
        out = ctypes.c_uint32(0)
        if not getcode_fn(raw, len(raw), ctypes.byref(out), 0):
            return None
        return out.value

    return getname, getcode


def _spelled(name: str, text: str) -> dict[str, Any]:
    """``{name: text}``, or ``{name_cps: [...]}`` when ``text`` holds two
    backslashes in a row (which the golden files' host guard reads as the
    root of a Windows share) or a surrogate."""
    if "\\\\" in text:
        return {f"{name}_cps": [ord(c) for c in text]}
    return text_field(name, text)


def _evaluated(text: str) -> dict[str, Any]:
    try:
        with warnings.catch_warnings():
            # `b'\N'` is an unknown escape: a SyntaxWarning, not an error.
            warnings.simplefilter("ignore", SyntaxWarning)
            value = ast.literal_eval(text)
    except Exception as exc:  # every caller in claims.py catches Exception
        return {"raised": type(exc).__name__}
    if isinstance(value, str):
        return {"str": [ord(c) for c in value]}
    return _spelled("repr", repr(value))


def _named(name: str) -> int | None:
    answer = _evaluated("'\\N{" + name + "}'")
    cps = answer.get("str")
    return cps[0] if cps is not None and len(cps) == 1 else None


def _digest(pairs: list[tuple[str, int]]) -> dict[str, Any]:
    text = "\n".join(f"{name}\t{cp}" for name, cp in pairs)
    return {"count": len(pairs), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _scalars() -> range:
    return range(0x110000)


def _data() -> tuple[
    list[tuple[str, int]],
    list[tuple[str, int, int]],
    list[list[str]],
    list[str],
    list[str],
]:
    getname, getcode = _capi()
    ranges: list[tuple[str, int, int]] = []
    for prefix in RULE_PREFIXES:
        for cp in _scalars():
            if 0xD800 <= cp <= 0xDFFF or getcode(f"{prefix}{cp:04X}") != cp:
                continue
            if ranges and ranges[-1][0] == prefix and ranges[-1][2] == cp - 1:
                ranges[-1] = (prefix, ranges[-1][1], cp)
            else:
                ranges.append((prefix, cp, cp))
    ranges.sort(key=lambda r: r[1])
    syllable = [
        unicodedata.name(chr(cp))[len(HANGUL_PREFIX) :]
        for cp in range(HANGUL_FIRST, HANGUL_LAST + 1)
    ]
    jamo = [
        [syllable[i * 21 * 28][:-1] for i in range(19)],
        [syllable[11 * 21 * 28 + i * 28] for i in range(21)],
        [syllable[11 * 21 * 28 + i][1:] for i in range(28)],
    ]
    table = []
    for cp in _scalars():
        name = unicodedata.name(chr(cp), None)
        if name is not None and not name.startswith((HANGUL_PREFIX, *RULE_PREFIXES)):
            table.append((name, cp))
    aliases: list[str] = []
    while (alias := getname(ALIASES_START + len(aliases), 1)) is not None:
        aliases.append(alias)
        code = getcode(alias)
        assert code is not None
        table.append((alias, code))
    sequences: list[str] = []
    while (sequence := getname(NAMED_SEQUENCES_START + len(sequences), 1)) is not None:
        sequences.append(sequence)
    table.sort()
    for name, cp in table:
        assert _named(name) == cp, name
    return table, ranges, jamo, aliases, sequences


def _ruled(
    ranges: list[tuple[str, int, int]], jamo: list[list[str]]
) -> list[tuple[str, int]]:
    out = []
    for i in range(HANGUL_LAST - HANGUL_FIRST + 1):
        lead, vowel, tail = i // (21 * 28), i // 28 % 21, i % 28
        out.append(
            (
                HANGUL_PREFIX + jamo[0][lead] + jamo[1][vowel] + jamo[2][tail],
                HANGUL_FIRST + i,
            )
        )
    for prefix, first, last in ranges:
        out.extend((f"{prefix}{cp:04X}", cp) for cp in range(first, last + 1))
    out.sort(key=lambda e: e[1])
    for name, cp in out:
        assert _named(name) == cp, name
    return out


def _n(name: str) -> str:
    return "'\\N{" + name + "}'"


_FIXED = (
    _n("EM DASH"),
    _n("em dash"),
    _n("Em Dash"),
    _n("eM dAsH"),
    _n("LATIN SMALL LETTER A"),
    _n("latin small letter a"),
    _n("LATIN CAPITAL LETTER A"),
    _n("LATIN SMALL LETTER SHARP S"),
    _n("latin small letter sharp s"),
    _n("GREEK SMALL LETTER FINAL SIGMA"),
    _n("BULLET"),
    _n("bullet"),
    _n("SNOWMAN"),
    _n("DIGIT ZERO"),
    _n("HYPHEN-MINUS"),
    _n("hyphen-minus"),
    _n("HYPHEN MINUS"),
    _n("HYPHENMINUS"),
    _n("SPACE"),
    _n("NO-BREAK SPACE"),
    _n("<control>"),
    _n("TANGUT COMPONENT-001"),
    _n("tangut component-001"),
    _n("TANGUT COMPONENT-1"),
    _n("TANGUT COMPONENT-0001"),
    _n("CJK UNIFIED IDEOGRAPH-4E00"),
    _n("cjk unified ideograph-4e00"),
    _n("Cjk Unified Ideograph-4E00"),
    _n("CJK UNIFIED IDEOGRAPH-4e00"),
    _n("CJK UNIFIED IDEOGRAPH-4E0"),
    _n("CJK UNIFIED IDEOGRAPH-04E00"),
    _n("CJK UNIFIED IDEOGRAPH-004E00"),
    _n("CJK UNIFIED IDEOGRAPH-0004E00"),
    _n("CJK UNIFIED IDEOGRAPH-4E00 "),
    _n("CJK UNIFIED IDEOGRAPH- 4E00"),
    _n("CJK UNIFIED IDEOGRAPH-+4E00"),
    _n("CJK UNIFIED IDEOGRAPH-0x4E00"),
    _n("CJK UNIFIED IDEOGRAPH-4G00"),
    _n("CJK UNIFIED IDEOGRAPH-"),
    _n("CJK UNIFIED IDEOGRAPH 4E00"),
    _n("CJK UNIFIED IDEOGRAPH-2a6df"),
    _n("CJK UNIFIED IDEOGRAPH-02A6DF"),
    _n("CJK UNIFIED IDEOGRAPH-FFFFFFFF4E00"),
    # Seven to nine hex digits: past what `%04X` writes for a code point,
    # and nine overflow 32 bits.
    _n("CJK UNIFIED IDEOGRAPH-FFFFFFF"),
    _n("CJK UNIFIED IDEOGRAPH-FFFFFFFF"),
    _n("CJK UNIFIED IDEOGRAPH-FFFFFFFFF"),
    _n("CJK UNIFIED IDEOGRAPH-100004E00"),
    _n("CJK COMPATIBILITY IDEOGRAPH-f900"),
    _n("CJK COMPATIBILITY IDEOGRAPH-0F900"),
    _n("TANGUT IDEOGRAPH-17000"),
    _n("tangut ideograph-17000"),
    _n("TANGUT IDEOGRAPH-017000"),
    _n("TANGUT IDEOGRAPH-187f7"),
    _n("KHITAN SMALL SCRIPT CHARACTER-18b00"),
    _n("NUSHU CHARACTER-1b170"),
    _n("EGYPTIAN HIEROGLYPH-13460"),
    _n("HANGUL SYLLABLE GA"),
    _n("hangul syllable ga"),
    _n("Hangul Syllable Ga"),
    _n("HANGUL SYLLABLE ga"),
    _n("HANGUL SYLLABLE GAG"),
    _n("HANGUL SYLLABLE GAGG"),
    _n("HANGUL SYLLABLE GAGS"),
    _n("HANGUL SYLLABLE GGA"),
    _n("HANGUL SYLLABLE GGGA"),
    _n("HANGUL SYLLABLE A"),
    _n("HANGUL SYLLABLE YAE"),
    _n("HANGUL SYLLABLE WAELH"),
    _n("HANGUL SYLLABLE SSYEOLH"),
    _n("HANGUL SYLLABLE HIH"),
    _n("HANGUL SYLLABLE HIHH"),
    _n("HANGUL SYLLABLE G"),
    _n("HANGUL SYLLABLE GAX"),
    _n("HANGUL SYLLABLE GA "),
    _n("HANGUL SYLLABLE "),
    _n("HANGUL SYLLABLE"),
    _n("HANGUL SYLLABLEGA"),
    _n("HANGUL  SYLLABLE GA"),
    _n("HANGUL SYLLABLE \u00c0"),
    _n("KEYCAP NUMBER SIGN"),
    _n("NULL"),
    _n("null"),
    _n("NUL"),
    _n("LINE FEED"),
    _n("LF"),
    _n("lf"),
    _n("CHARACTER TABULATION"),
    _n("DELETE"),
    _n("BYTE ORDER MARK"),
    _n("BOM"),
    _n("ZWNBSP"),
    _n("ZERO WIDTH NO-BREAK SPACE"),
    _n("LATIN CAPITAL LETTER GHA"),
    _n("LATIN CAPITAL LETTER OI"),
    _n("VS256"),
    _n("vs256"),
    _n("VARIATION SELECTOR-256"),
    _n("A" * 300),
    _n("LATIN SMALL LETTER A" * 5),
    _n("LATIN SMALL LETTER A\u00e9"),
    # U+0141's low byte is `A`: a name past ASCII never matches by its
    # low bytes.
    _n("EM D\u0141SH"),
    _n("BULLE\u0154"),
    _n("\u00e9"),
    _n("EM\tDASH"),
    _n("EM \\\nDASH"),
    _n(""),
    _n("EM DASH\\N{BULLET"),
    "'\\N'",
    "'\\N '",
    "'\\Nx'",
    "'\\N{'",
    "'\\N{EM DASH'",
    "'\\N EM DASH}'",
    "'\\N{EM DASH}}'",
    "'\\N{{EM DASH}'",
    "'\\N{ EM DASH}'",
    "'\\N{EM DASH }'",
    "'\\N{EM  DASH}'",
    "'\\N{EMDASH}'",
    "'\\N{EM_DASH}'",
    "'\\N{EM-DASH}'",
    "'\\N{EM DASH}\\N{X}'",
    "'\\N{X}\\N{EM DASH}'",
    "'\\N{X}\\x4'",
    "'\\N{EM DASH}\\x4'",
    "'\\x4\\N{EM DASH}'",
    "'a\\N{EM DASH}b\\N{BULLET}c'",
    "'\\N{EM DASH}\\N{BULLET}'",
    "'\\N{EM DASH}' '\\N{BULLET}'",
    "'\\N{EM DASH}' + '\\N{BULLET}'",
    "'\\\\N{EM DASH}'",
    "'\\\\\\N{EM DASH}'",
    "r'\\N{EM DASH}'",
    "R'\\N{X}'",
    "u'\\N{EM DASH}'",
    "U'\\N{EM DASH}'",
    "b'\\N{EM DASH}'",
    "rb'\\N{X}'",
    "f'\\N{EM DASH}'",
    "'''\\N{EM DASH}'''",
    '"""\\N{EM\'DASH}"""',
    "'''\\N{EM\nDASH}'''",
    "('\\N{EM DASH}',)",
    "['\\N{EM DASH}', '\\N{X}']",
    "{'\\N{EM DASH}': '\\N{BULLET}'}",
    "'\\N{LATIN SMALL LETTER A}' '\\N{latin capital letter a}'",
    # Names past ASCII: the raw run must be an identifier; the parser
    # keeps its NFKC, and only `set` called with nothing evaluates.
    "\U0001d42c\U0001d41e\U0001d42d()",
    "\uff53\uff45\uff54()",
    "\uff53\uff45\uff54 ()",
    "(\uff53\uff45\uff54)()",
    "(\uff53\uff45\uff54())",
    "[\uff53\uff45\uff54()]",
    "{1: \uff53\uff45\uff54()}",
    "{\uff53\uff45\uff54()}",
    "\uff53\uff45\uff54(1)",
    "\uff53\uff45\uff54",
    "s\uff45t()",
    "\u017fet()",
    "\u02e2\u1d49\u1d57()",
    "\u24e2\u24d4\u24e3()",
    "set\u00b7()",
    "se\u0074\u0301()",
    "\uff33\uff25\uff34()",
    "\U0001d413\U0001d42b\U0001d42e\U0001d41e",
    "\uff34\uff52\uff55\uff45",
    "\uff26\uff41\uff4c\uff53\uff45",
    "\uff2e\uff4f\uff4e\uff45",
    "[\uff2e\uff4f\uff4e\uff45]",
    "\u00e9",
    "\u00e9()",
    "\u20ac",
    "\U0001d422\U0001d41f",
    "\ufb01",
    "\u2118",
    "\u309b",
    "_\uff53\uff45\uff54()",
    "1\u00e9",
    "\uff53\uff45\uff54()\u00e9",
    "\u00e9 + '\\N{EM DASH}'",
    "b\u00e9'x'",
)


def _cases(
    ranges: list[tuple[str, int, int]], aliases: list[str], sequences: list[str]
) -> list[str]:
    out = list(_FIXED)
    for prefix, first, last in ranges:
        for cp in (first - 1, first, last, last + 1):
            out.append(_n(f"{prefix}{cp:04X}"))
        out.append(_n(f"{prefix.lower()}{first:04x}"))
    # Each prefix with the first code point of every range of another
    # prefix: a prefix reads its own ranges only.
    for prefix in dict.fromkeys(r[0] for r in ranges):
        out.extend(_n(f"{prefix}{f:04X}") for p, f, _ in ranges if p != prefix)
    out.extend(_n(a) for a in aliases)
    out.extend(_n(s) for s in sequences)
    return out


def generate() -> dict[str, bytes]:
    header: dict[str, Any] = {
        "fixture": "unicode_names",
        "unidata_version": unicodedata.unidata_version,
    }
    if unicodedata.unidata_version != UNIDATA:
        header["declined"] = (
            f"Unicode {unicodedata.unidata_version}: the table is {UNIDATA}'s"
        )
        return {"unicode_names.jsonl": jsonl([header])}
    table, ranges, jamo, aliases, sequences = _data()
    cases = _cases(ranges, aliases, sequences)
    header.update(
        {
            "rows": len(cases),
            "table": {
                "rule": "every name and alias not made by a rule, sorted by name",
                **_digest(table),
            },
            "ruled": {
                "rule": "Hangul syllables by short names, then each range by %04X, "
                "in code point order",
                **_digest(_ruled(ranges, jamo)),
                "ranges": [list(r) for r in ranges],
                "jamo": jamo,
            },
        }
    )
    records: list[Any] = [header]
    for text in cases:
        records.append({**_spelled("t", text), "v": _evaluated(text)})
    return {"unicode_names.jsonl": jsonl(records)}
