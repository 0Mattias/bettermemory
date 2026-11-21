"""Fixtures of the regular expression engine (`pyre`, native/README.md,
"Python's regular expressions").

The reference is CPython's ``re`` as the running interpreter has it: every
answer here is asked of ``re``, ``_sre``, ``re._casefix`` or ``re._parser``,
and no module of the product is imported. The engine is built in units; each
unit writes its own rows, and a row's ``part`` says what it pins. Unit
pyre-1 (the case folding) writes its rows in this module and marks each
with its name under ``unit``. Unit pyre-2 (the parser's tree, state and
tokenizer, and the atoms it reads) writes its rows in `gen_pyre_tree` and
`gen_pyre_atom`, under parts prefixed ``tree.``, ``token.`` and ``atom.``,
and spells their texts by `gen_pyre_tree.spell`. Unit pyre-3 (the repeats
and `getwidth`) writes its rows in `gen_pyre_parse`, under parts prefixed
``parse.``: in ``pyre.jsonl`` the answers that hold no tree text, in
``pyre_python.jsonl`` the tree texts. Unit pyre-4a (a class compiled as
``_optimize_charset`` compiles it) writes its rows in `gen_pyre_charset`,
under parts prefixed ``charset.``: in ``pyre_unicode.jsonl`` the classes
that fold and the sweeps that read a Unicode table, in
``pyre_python.jsonl`` the classes whose answer follows the release, the
rest in ``pyre.jsonl``. Unit pyre-4b (the lowering) writes
its rows in `gen_pyre_lower`, into ``pyre_lower.jsonl`` and, for a row
under IGNORECASE, under parts prefixed ``lower.`` in
``pyre_unicode.jsonl``. A file's first line is its
header: the fixture's name and, under ``rows``, how many rows each unit
wrote (the units of `_PARTS`). Three files, one JSON object per line, keys
sorted, ASCII:

- ``pyre.jsonl``, not gated: unit pyre-1's patterns and texts of ASCII
  only, whose answers read no Unicode table and are the same in every
  Python from 3.11 on, and unit pyre-2's parts, which read no Unicode table
  either (`gen_pyre_tree` and `gen_pyre_atom` name the releases they were
  run under). Its header also states, as fixed text, the interpreter the
  engine reproduces. The whole engine's rows are held to ``CAP_BYTES`` in
  this file.
- ``pyre_unicode.jsonl``, gated on the Unicode version: every code point
  through ``re``'s lower case, upper case, cased test and the three
  categories, by SHA-256 per block of 0x4000 code points; the extra cases
  in full; the exact set every cased literal matches under IGNORECASE, by
  SHA-256 per 64 patterns; and the cases past ASCII written out.
- ``pyre_python.jsonl``, gated on the Python release: the interpreter, its
  Unicode version, ``_sre.MAGIC``, ``_sre.CODESIZE`` and the SHA-256 of
  each of the five ``re`` sources, one row per fact, as the running
  interpreter reports them; and unit pyre-2's patterns that a release parses
  otherwise: an empty negative lookaround, ``\\z`` and a ``\\U`` escape of
  2**31 or more. Nothing that must hold everywhere is pinned only here.

A text of unit pyre-1 is a JSON string, or the list of its code points when
it holds a lone surrogate.

A ``kill`` row is one pattern under one flag set: ``pattern`` as written,
``flags`` as passed to ``re.compile``, ``sre_flags`` as ``_compile`` reads
them (``re.compile(...).flags``), the one node ``re._parser`` reads it
as (``literal`` and ``negated``), ``texts`` with ``fullmatch`` (whether
``fullmatch`` of each matches) and, in the Unicode file, ``set``: every code
point ``c`` for which ``findall`` over the string of all code points returns
``chr(c)`` (``unmatched``, the code points it does not return, for a negated
literal). ``kills`` names the wrong reading the row tells apart.

A ``sweep`` row holds one value per code point from U+0000 to U+10FFFF in
order, four bytes little-endian for a map and one byte (0 or 1) for a test:
``sha256`` over all of them and, under ``blocks``, the first 16 hex digits
of the SHA-256 of each block of 0x4000 code points (``[digest, n]`` stands
for ``n`` blocks in a row with that digest). A set of code points is hashed
as its pattern, its size and its members ascending, each four bytes
little-endian.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import inspect
import re
import struct
import sys
import unicodedata
from array import array
from collections.abc import Callable, Sequence
from typing import Any

from . import (
    gen_pyre_atom,
    gen_pyre_charset,
    gen_pyre_exec,
    gen_pyre_lower,
    gen_pyre_parse,
    gen_pyre_tree,
)
from ._common import jsonl

#: The cap on ``pyre.jsonl`` for the whole engine (every unit's rows); the
#: whole engine's fixture is planned at 134 KB.
CAP_BYTES = 150_000

#: The interpreter the engine reproduces, as fixed text for the header of
#: the file that is not gated. ``pyre_python.jsonl`` holds what the running
#: interpreter reports.
REPRODUCES = {"python": "3.13.13", "unicode": "15.1.0", "sre_magic": 20230612}

_N = 0x110000
_BLOCK = 0x4000
_I = int(re.IGNORECASE)


def _t(text: str) -> Any:
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return [ord(ch) for ch in text]
    return text


@functools.cache
def _every() -> str:
    """The string of every code point, U+0000 to U+10FFFF in order."""
    return "".join(map(chr, range(_N)))


def _sre() -> Any:
    return importlib.import_module("_sre")


def _matched(pattern: str, flags: int) -> list[int]:
    """Every code point ``pattern`` finds in the string of all of them."""
    return [ord(ch) for ch in re.compile(pattern, flags).findall(_every())]


def _digest16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _run_lengths(digests: list[str]) -> list[Any]:
    out: list[Any] = []
    for digest in digests:
        if out and isinstance(out[-1], list) and out[-1][0] == digest:
            out[-1][1] += 1
        elif out and out[-1] == digest:
            out[-1] = [digest, 2]
        else:
            out.append(digest)
    return out


def _set_bytes(pattern: int, members: list[int]) -> bytes:
    return struct.pack(f"<{2 + len(members)}I", pattern, len(members), *members)


# ---------------------------------------------------------------------------
# Unit pyre-1: the case folding sre uses, and the test of one literal.
# ---------------------------------------------------------------------------

_FOLD = "pyre-1"

#: Uncased literals swept under IGNORECASE: every one below this bound and
#: ``_FOLD_SEEDED`` more, drawn by ``_FOLD_RULE`` from ``_FOLD_SEED``.
_FOLD_BELOW = 0x530
_FOLD_SEEDED = 2000
_FOLD_SEED = 0x5EED_F01D
_FOLD_RULE = (
    "x = (x * 6364136223846793005 + 1442695040888963407) mod 2**64, from the "
    "seed; draw i (from 0) is (x >> 33) mod 0x10000 when i is even and mod "
    "0x110000 when i is odd; a draw is kept when it is uncased and new"
)


def _fold_kill(
    kills: str, pattern: str, flags: int, texts: list[str], *, every: bool
) -> dict[str, Any]:
    tree = list(importlib.import_module("re._parser").parse(pattern, flags))
    assert len(tree) == 1, (pattern, tree)
    op, literal = tree[0]
    assert str(op) in ("LITERAL", "NOT_LITERAL"), (pattern, tree)
    compiled = re.compile(pattern, flags)
    row: dict[str, Any] = {
        "part": "kill",
        "unit": _FOLD,
        "kills": kills,
        "pattern": _t(pattern),
        "flags": flags,
        "sre_flags": compiled.flags,
        "literal": int(literal),
        "negated": str(op) == "NOT_LITERAL",
        "texts": [_t(text) for text in texts],
        "fullmatch": [compiled.fullmatch(text) is not None for text in texts],
    }
    if every:
        found = _matched(pattern, flags)
        if row["negated"]:
            row["unmatched"] = sorted(set(range(_N)) - set(found))
        else:
            row["set"] = found
    return row


_ASCII_SHAPES = ["K", "k", "j", "kk", "", "Kk", "k\n"]

_FOLD_KILLS_ASCII: tuple[tuple[str, str, int, list[str]], ...] = (
    ("no fold at all", "k", _I, _ASCII_SHAPES),
    ("the pattern's own case kept", "K", _I, _ASCII_SHAPES),
    ("a fold without the flag", "k", 0, ["K", "k"]),
    ("a fold without the flag", "K", 0, ["K", "k"]),
    ("a fold by flipping bit 5", "@", _I, ["@", "`"]),
    ("a fold by flipping bit 5", "`", _I, ["@", "`"]),
    ("a fold by flipping bit 5", r"\[", _I, ["[", "{"]),
    ("a fold by flipping bit 5", r"\{", _I, ["[", "{"]),
    ("a fold by flipping bit 5", "_", _I, ["_", "\x7f"]),
    ("a fold by flipping bit 5", "\x7f", _I, ["_", "\x7f"]),
    ("a fold by flipping bit 5", "1", _I, ["1", "\x11"]),
    ("the edge of the letters", "z", _I, ["Z", "z", "{", "["]),
    ("the edge of the letters", "a", _I, ["A", "a", "!", "\x01"]),
    ("a control character folded", "\x00", _I, ["\x00", " "]),
    ("an escaped literal", "\\\n", _I, ["\n", "\\", "\\\n"]),
    ("NOT_LITERAL not the negation", "[^k]", _I, ["K", "k", "j", "", "jj"]),
    ("NOT_LITERAL not the negation", "[^K]", 0, ["K", "k", ""]),
    ("NOT_LITERAL not the negation", "[^1]", _I, ["1", "2", "\x11"]),
)

_FOLD_KILLS_UNICODE: tuple[tuple[str, str, int, list[str]], ...] = (
    ("a fold of ASCII letters only", "k", _I, ["\u212a", "kk"]),
    ("a fold of ASCII letters only", "s", _I, ["\u017f"]),
    ("no extra cases", "i", _I, ["\u0130", "\u0131", "i\u0307"]),
    ("the pattern's own case kept", "I", _I, ["\u0131", "\u0130"]),
    ("str.lower, two code points", "\u0130", _I, ["i", "I", "i\u0307"]),
    ("extra cases one way only", "\u0131", _I, ["i", "I", "\u0130"]),
    ("simple case folding", "\u212a", _I, ["k", "K"]),
    ("casefold, or a fold by upper case", "\u00df", _I, ["SS", "ss", "s", "S"]),
    ("casefold", "\u1e9e", _I, ["\u00df", "ss"]),
    ("title case read as uncased", "\u01c5", _I, ["\u01c4", "\u01c6"]),
    ("final sigma by context", "\u03c3", _I, ["\u03a3", "\u03c2"]),
    ("final sigma by context", "\u03a3", _I, ["\u03c3", "\u03c2"]),
    ("final sigma by context", "\u03c2", _I, ["\u03a3", "\u03c3"]),
    ("no extra cases", "\u00b5", _I, ["\u039c", "\u03bc"]),
    ("a mark read as uncased", "\u0345", _I, ["\u0399", "\u03b9", "\u1fbe"]),
    ("no extra cases", "\u1fbe", _I, ["\u0345", "\u0399"]),
    ("no extra cases", "\u03b8", _I, ["\u03d1", "\u03f4"]),
    ("no extra cases", "\u03f4", _I, ["\u03b8", "\u03d1"]),
    ("a table of the BMP only", "\U00010400", _I, ["\U00010428"]),
    ("a table of the BMP only", "\U00010428", _I, ["\U00010400"]),
    ("a surrogate folded", "\ud800", _I, ["\ud800", "\udc00"]),
    ("the property Cased", "\u00aa", _I, ["\u00aa", "A", "a"]),
    ("a fold by upper case", "\u0149", _I, ["\u02bc", "N"]),
    ("a fold by upper case", "\u01f0", _I, ["J", "j"]),
    ("a fold by upper case", "\u1f80", _I, ["\u1f88", "\u1f08", "\u1f00"]),
    ("no extra cases", "\ufb05", _I, ["\ufb06", "st"]),
    ("no extra cases", "\u1e61", _I, ["\u1e9b", "\u1e60"]),
    ("the direction of casefold", "\u13a0", _I, ["\uab70"]),
    ("the direction of casefold", "\uab70", _I, ["\u13a0"]),
    ("cased by its upper case only", "\u10d0", _I, ["\u1c90"]),
    ("cased by its upper case only", "\u10a0", _I, ["\u2d00", "\u10d0"]),
    ("no extra cases", "\u0442", _I, ["\u0422", "\u1c84", "\u1c85"]),
    ("no extra cases", "\ua64b", _I, ["\ua64a", "\u1c88"]),
    ("a compatibility letter kept", "\u2126", _I, ["\u03c9", "\u03a9"]),
    ("a compatibility letter kept", "\u212b", _I, ["\u00e5", "\u00c5"]),
    ("no extra cases", "\u0390", _I, ["\u1fd3"]),
    ("a table of Latin-1 only", "\u00ff", _I, ["\u0178"]),
    ("the last cased code point", "\U0001e943", _I, ["\U0001e921"]),
    ("a numeral read as uncased", "\u2167", _I, ["\u2177"]),
    ("a digit folded", "\u00b2", _I, ["2", "\u00b2"]),
    ("an upper case of two code points", "\u0587", _I, ["\u0535", "\u0565"]),
    ("a fold without the flag", "\u00e9", 0, ["\u00c9", "\u00e9"]),
    ("a fold without the flag", "\u212a", 0, ["k", "K", "\u212a"]),
    ("the last code point", "\U0010ffff", _I, ["\U0010ffff", "\U0010fffe"]),
    ("NOT_LITERAL not the negation", "[^k]", _I, ["K", "\u212a", "x", "", "xx"]),
    ("NOT_LITERAL not the negation", "[^\u03c3]", _I, ["\u03a3", "\u03c2", "s"]),
    ("NOT_LITERAL not the negation", "[^\u00aa]", _I, ["\u00aa", "a"]),
)


def _fold_ascii(flags: int) -> dict[str, Any]:
    """For each ASCII literal, the ASCII characters it matches."""
    text = "".join(map(chr, range(0x80)))
    compiled = [re.compile(re.escape(chr(p)), flags) for p in range(0x80)]
    (sre_flags,) = {pattern.flags for pattern in compiled}
    sets = [[ord(ch) for ch in pattern.findall(text)] for pattern in compiled]
    return {
        "part": "ascii",
        "unit": _FOLD,
        "flags": flags,
        "sre_flags": sre_flags,
        "sets": sets,
    }


def fold_rows() -> list[dict[str, Any]]:
    """Unit pyre-1's rows of ``pyre.jsonl``."""
    rows = [_fold_ascii(0), _fold_ascii(_I)]
    rows += [_fold_kill(*kill, every=False) for kill in _FOLD_KILLS_ASCII]
    return rows


@functools.cache
def _fold_values() -> dict[str, Sequence[int]]:
    """One value per code point: ``_sre.unicode_tolower``, the first code
    point of ``str.upper``, ``_sre.unicode_iscased``, and ``re``'s three
    categories on a ``str`` pattern. Kept for the life of the process (the
    golden test asks twice), so held compactly: 13 MB in all."""
    sre = _sre()

    def category(pattern: str) -> bytes:
        out = bytearray(_N)
        for cp in _matched(pattern, 0):
            out[cp] = 1
        return bytes(out)

    return {
        "lower": array("I", (sre.unicode_tolower(c) for c in range(_N))),
        "upper": array("I", (ord(chr(c).upper()[0]) for c in range(_N))),
        "iscased": bytes(sre.unicode_iscased(c) for c in range(_N)),
        "word": category(r"\w"),
        "digit": category(r"\d"),
        "space": category(r"\s"),
    }


_FOLD_SOURCES = {
    "lower": "_sre.unicode_tolower(c)",
    "upper": "ord(chr(c).upper()[0])",
    "iscased": "_sre.unicode_iscased(c)",
    "word": r"re.compile(r'\w').findall(every)",
    "digit": r"re.compile(r'\d').findall(every)",
    "space": r"re.compile(r'\s').findall(every)",
}


def _fold_sweep(what: str, values: Sequence[int]) -> dict[str, Any]:
    wide = what in ("lower", "upper")
    whole = hashlib.sha256()
    digests = []
    for lo in range(0, _N, _BLOCK):
        chunk = values[lo : lo + _BLOCK]
        data = struct.pack(f"<{_BLOCK}I", *chunk) if wide else bytes(chunk)
        whole.update(data)
        digests.append(_digest16(data))
    return {
        "part": "sweep",
        "unit": _FOLD,
        "what": what,
        "source": _FOLD_SOURCES[what],
        "bytes": 4 if wide else 1,
        "count": (
            sum(1 for c, v in enumerate(values) if v != c) if wide else sum(values)
        ),
        "blocks": _run_lengths(digests),
        "sha256": whole.hexdigest(),
    }


def _fold_extra_cases() -> dict[int, tuple[int, ...]]:
    table = importlib.import_module("re._casefix")._EXTRA_CASES
    return {int(key): tuple(int(c) for c in table[key]) for key in sorted(table)}


def _fold_facts() -> dict[str, Any]:
    """Counts the unit's notes state, each taken from the interpreter."""
    v = _fold_values()
    lower, upper, cased = v["lower"], v["upper"], v["iscased"]
    extra = _fold_extra_cases()
    lowers_of_another = {lower[c] for c in range(_N) if lower[c] != c}
    return {
        "part": "facts",
        "unit": _FOLD,
        "cased": sum(cased),
        "cased_past_bmp": sum(cased[0x10000:]),
        "cased_surrogates": sum(cased[0xD800:0xE000]),
        "cased_not_lower_or_upper_differs": sum(
            1 for c in range(_N) if bool(cased[c]) != (lower[c] != c or upper[c] != c)
        ),
        "lower_not_first_of_str_lower": sum(
            1 for c in range(_N) if lower[c] != ord(chr(c).lower()[0])
        ),
        "cased_lowering_to_uncased": sum(
            1 for c in range(_N) if cased[c] and not cased[lower[c]]
        ),
        "lower_is_upper_not_itself": sum(
            1 for c in range(_N) if lower[c] == upper[c] != c
        ),
        "uncased_lower_of_another": sum(1 for c in lowers_of_another if not cased[c]),
        "extra_keys": len(extra),
        "extra_partners": sum(len(others) for others in extra.values()),
        "extra_largest": max(max(key, *others) for key, others in extra.items()),
        "extra_not_symmetric": sum(
            1 for key, others in extra.items() for c in others if key not in extra[c]
        ),
        "extra_not_cased": sum(
            1 for key, others in extra.items() for c in (key, *others) if not cased[c]
        ),
        "extra_not_fixed_by_lower": sum(
            1 for key, others in extra.items() for c in (key, *others) if lower[c] != c
        ),
    }


@functools.cache
def _fold_cased_sets() -> tuple[tuple[int, tuple[int, ...]], ...]:
    """For every cased code point as a pattern under IGNORECASE, the exact
    set of code points it matches, asked of ``re`` over every code point."""
    cased = _fold_values()["iscased"]
    return tuple(
        (p, tuple(_matched(re.escape(chr(p)), _I))) for p in range(_N) if cased[p]
    )


def _fold_sets_row(
    part: str, sets: list[tuple[int, list[int]]], per: int
) -> dict[str, Any]:
    whole = hashlib.sha256()
    digests = []
    for start in range(0, len(sets), per):
        data = b"".join(
            _set_bytes(p, members) for p, members in sets[start : start + per]
        )
        whole.update(data)
        digests.append(_digest16(data))
    return {
        "part": part,
        "unit": _FOLD,
        "patterns": len(sets),
        "members": sum(len(members) for _, members in sets),
        "largest": max(len(members) for _, members in sets),
        "per": per,
        "blocks": digests,
        "sha256": whole.hexdigest(),
    }


def _fold_uncased_patterns() -> list[int]:
    cased = _fold_values()["iscased"]
    out = [p for p in range(_FOLD_BELOW) if not cased[p]]
    seen = set(out)
    x = _FOLD_SEED
    draw = kept = 0
    while kept < _FOLD_SEEDED:
        x = (x * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        p = (x >> 33) % (0x10000 if draw % 2 == 0 else _N)
        draw += 1
        if p not in seen and not cased[p]:
            seen.add(p)
            out.append(p)
            kept += 1
    return out


def fold_unicode_rows() -> list[dict[str, Any]]:
    """Unit pyre-1's rows of ``pyre_unicode.jsonl``."""
    values = _fold_values()
    rows: list[dict[str, Any]] = [
        _fold_sweep(what, values[what]) for what in _FOLD_SOURCES
    ]
    rows.append(
        {
            "part": "extra_cases",
            "unit": _FOLD,
            "source": "re._casefix._EXTRA_CASES",
            "table": [
                [key, list(others)] for key, others in _fold_extra_cases().items()
            ],
        }
    )
    rows.append(_fold_facts())
    cased = [(p, list(members)) for p, members in _fold_cased_sets()]
    rows.append(_fold_sets_row("cased_literals", cased, 64))
    uncased = [(p, _matched(re.escape(chr(p)), _I)) for p in _fold_uncased_patterns()]
    row = _fold_sets_row("uncased_literals", uncased, 256)
    row.update(below=_FOLD_BELOW, seeded=_FOLD_SEEDED, seed=_FOLD_SEED, rule=_FOLD_RULE)
    rows.append(row)
    rows += [_fold_kill(*kill, every=True) for kill in _FOLD_KILLS_UNICODE]
    return rows


#: The ``re`` sources the engine was read from, in the order of their rows.
_FOLD_RE_FILES = ("__init__", "_casefix", "_compiler", "_constants", "_parser")


def fold_python_rows() -> list[dict[str, Any]]:
    """Unit pyre-1's rows of ``pyre_python.jsonl``: what the engine was read
    from, as the running interpreter reports it, one row per fact so that
    under another release each is compared or set aside alone.

    A source is digested as the text Python reads (``inspect.getsource``,
    line ends read as ``\\n``) in UTF-8, so the digest is the release's
    whether its standard library is files or a zip archive. An installation
    without the sources raises."""
    sre = _sre()
    v = sys.version_info
    rows: list[dict[str, Any]] = [
        {"part": "python", "unit": _FOLD, "value": f"{v.major}.{v.minor}.{v.micro}"},
        {"part": "unicode", "unit": _FOLD, "value": unicodedata.unidata_version},
        {"part": "sre_magic", "unit": _FOLD, "value": int(sre.MAGIC)},
        {"part": "sre_codesize", "unit": _FOLD, "value": int(sre.CODESIZE)},
    ]
    for name in _FOLD_RE_FILES:
        module = importlib.import_module("re" if name == "__init__" else f"re.{name}")
        text = inspect.getsource(module)
        rows.append(
            {
                "part": "source",
                "unit": _FOLD,
                "file": f"{name}.py",
                "sha256": _digest16(text.encode("utf-8")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# The files. A unit adds its row functions to the file it writes into, each
# under the unit's name, which the header counts its rows by.
# ---------------------------------------------------------------------------

_PYRE_2 = "pyre-2"
_PYRE_3 = "pyre-3"
_PYRE_4A = "pyre-4a"
_PYRE_4B = "pyre-4b"
_PYRE_5A = "pyre-5a"

_PARTS: dict[str, tuple[tuple[str, Callable[[], list[dict[str, Any]]]], ...]] = {
    "pyre.jsonl": (
        (_FOLD, fold_rows),
        (_PYRE_2, gen_pyre_tree.rows),
        (_PYRE_2, gen_pyre_atom.rows),
        (_PYRE_3, gen_pyre_parse.rows),
        (_PYRE_4A, gen_pyre_charset.rows),
    ),
    "pyre_unicode.jsonl": (
        (_FOLD, fold_unicode_rows),
        (_PYRE_4A, gen_pyre_charset.rows_unicode),
        (_PYRE_4B, gen_pyre_lower.unicode_rows),
        (_PYRE_5A, gen_pyre_exec.unicode_rows),
    ),
    "pyre_python.jsonl": (
        (_FOLD, fold_python_rows),
        (_PYRE_2, gen_pyre_tree.rows_python),
        (_PYRE_2, gen_pyre_atom.rows_python),
        (_PYRE_3, gen_pyre_parse.rows_python),
        (_PYRE_4A, gen_pyre_charset.rows_python),
        (_PYRE_5A, gen_pyre_exec.python_rows),
    ),
}


def _file(name: str, **header: Any) -> dict[str, bytes]:
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for unit, part in _PARTS[name]:
        written = part()
        # A row that names its unit names the one it is listed under.
        assert all(row.get("unit", unit) == unit for row in written), (name, unit)
        counts[unit] = counts.get(unit, 0) + len(written)
        rows += written
    head = {
        "part": "header",
        "fixture": name.removesuffix(".jsonl"),
        "rows": counts,
        **header,
    }
    return {name: jsonl([head, *rows])}


def generate() -> dict[str, bytes]:
    out = _file("pyre.jsonl", cap_bytes=CAP_BYTES, reproduces=REPRODUCES)
    assert len(out["pyre.jsonl"]) <= CAP_BYTES, len(out["pyre.jsonl"])
    return out


def generate_unicode() -> dict[str, bytes]:
    return _file(
        "pyre_unicode.jsonl",
        unidata_version=unicodedata.unidata_version,
        code_points=_N,
        block=_BLOCK,
    )


def generate_python() -> dict[str, bytes]:
    return _file("pyre_python.jsonl")
