"""Fixtures: a class compiled as ``re._compiler`` compiles it (the
``charset.*`` parts of ``pyre.jsonl`` and ``pyre_unicode.jsonl``, unit
pyre-4a).

The native build reads them in
``native/crates/bettermemory-core/src/pyre/charset.rs``. The reference is
the interpreter's own ``re._compiler._optimize_charset``, called with the
fold ``_compile`` passes for the flags (``_compiler.py`` 48-55), and the
opcode the ``IN`` branch of ``_compile`` chooses (85-97). A class is read
by ``re._parser.parse`` and written as its members (`gen_pyre_tree._member`)
so that the Rust test builds it without the parser.

An answer is one line: the members ``_optimize_charset`` returns, each
spelled by `_op`, then ``|`` and the opcode, then ``h1`` or ``h0`` for
``hascased``. A class that passes through the fold (any IGNORECASE row under
UNICODE) is in ``pyre_unicode.jsonl``; the ASCII fold and the classes without
IGNORECASE read no Unicode table and are in ``pyre.jsonl``. A class whose
answer follows the release (`RELEASE`) and the fuzz over every member are
in ``pyre_python.jsonl``.

Parts, by ``part``:

- ``charset.class``: ``pattern`` (a bytes pattern under ``bytes``, as a
  list of code points when it holds one past ASCII or a lone surrogate) or,
  for a shape the parser never writes, only ``members``; ``flags`` as
  ``_compile`` reads them; ``members``; ``answer``; ``refused``, the feature
  the engine refuses the class for (`_refusal`), or null.
- ``charset.sweep``: a class of a ``charset.class`` row (``pattern``,
  ``bytes`` and ``flags`` name it) the engine compiles, and ``sha256`` over one
  byte (1 when ``findall`` over the string of every code point returns it,
  else 0) per code point from U+0000 to U+10FFFF (``count``), or per byte
  for a bytes pattern. In ``pyre_unicode.jsonl`` when the class folds or
  holds a category.
- ``charset.fuzz``: ``count`` member lists drawn by `gen_pyre_tree.Lcg` from
  ``seed`` + i (`_drawn`, over every member under ``wide``), each through ``_optimize_charset`` under
  ``flags``; ``sha256`` over their answers, one line each, ``\\n`` ended.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import re
from typing import Any

from .gen_pyre_tree import Lcg, _built_member, _member

C = importlib.import_module("re._constants")
_compiler = importlib.import_module("re._compiler")
_parser = importlib.import_module("re._parser")
_sre = importlib.import_module("_sre")

_N = 0x110000
_U = int(re.UNICODE)
_I = int(re.IGNORECASE)
_L = int(re.LOCALE)
_A = int(re.ASCII)


@functools.cache
def _every() -> str:
    return "".join(map(chr, range(_N)))


def _words(words: list[int]) -> str:
    return "".join(f"{w:08x}" for w in words)


def _op(op: Any, av: Any) -> str:
    if op is C.NEGATE:
        return "N"
    if op is C.LITERAL:
        return f"L{av:x}"
    if op is C.RANGE:
        return f"R{av[0]:x}-{av[1]:x}"
    if op is C.RANGE_UNI_IGNORE:
        return f"U{av[0]:x}-{av[1]:x}"
    if op is C.CATEGORY:
        return "C" + str(av).removeprefix("CATEGORY_")
    if op is C.CHARSET:
        return "S" + _words(av)
    assert op is C.BIGCHARSET, op
    text = _words(av)
    if len(av) > 128:
        return f"B#{len(av)}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    return "B" + text


def _fold(flags: int) -> tuple[Any, Any, Any]:
    """The ``iscased``, ``tolower`` and ``fixes`` `_compile` passes."""
    if flags & _I and not flags & _L:
        if flags & _U:
            return _sre.unicode_iscased, _sre.unicode_tolower, _compiler._EXTRA_CASES
        return _sre.ascii_iscased, _sre.ascii_tolower, None
    return None, None, None


def _answer(members: list[Any], flags: int) -> str:
    iscased, tolower, fixes = _fold(flags)
    out, hascased = _compiler._optimize_charset(members, iscased, tolower, fixes)
    if flags & _I and flags & _L:
        opcode = "IN_LOC_IGNORE"
    elif not hascased:
        opcode = "IN"
    elif not fixes:
        opcode = "IN_IGNORE"
    else:
        opcode = "IN_UNI_IGNORE"
    spelled = " ".join(_op(op, av) for op, av in out)
    return f"{spelled} |{opcode} h{int(hascased)}"


def _refusal(members: list[Any], flags: int) -> str | None:
    """The feature the engine refuses a class for (`compile_class`), in its
    order: LOCALE; IGNORECASE without UNICODE; a category without UNICODE;
    a member past the BMP under IGNORECASE."""
    if flags & _L:
        return "Locale"
    if flags & _I and not flags & _U:
        return "Ascii" if flags & _A else "BytesIgnorecase"
    for op, av in members:
        if op is C.CATEGORY and not flags & _U:
            return "Ascii" if flags & _A else "BytesCategory"
        top = av if op is C.LITERAL else av[1] if op is C.RANGE else 0
        if flags & _I and top > 0xFFFF:
            return "ClassPastBmpIgnorecase"
    return None


def _spelled(pattern: str | bytes) -> Any:
    text = pattern.decode("latin-1") if isinstance(pattern, bytes) else pattern
    if text.isascii():
        return text
    return [ord(c) for c in text]


def _parsed(pattern: str | bytes, flags: int) -> tuple[list[Any], int]:
    tree = _parser.parse(pattern, flags)
    assert len(tree.data) == 1 and tree.data[0][0] is C.IN, (pattern, tree.data)
    return list(tree.data[0][1]), tree.state.flags


def _class_row(pattern: str | bytes, flags: int) -> dict[str, Any]:
    members, sre_flags = _parsed(pattern, flags)
    row: dict[str, Any] = {"part": "charset.class", "pattern": _spelled(pattern)}
    if isinstance(pattern, bytes):
        row["bytes"] = True
    row.update(
        flags=sre_flags,
        members=[_member(m) for m in members],
        answer=_answer(members, sre_flags),
        refused=_refusal(members, sre_flags),
    )
    return row


def _built_row(members: list[list[Any]], flags: int) -> dict[str, Any]:
    built = [_built_member(m) for m in members]
    return {
        "part": "charset.class",
        "flags": flags,
        "members": members,
        "answer": _answer(built, flags),
        "refused": _refusal(built, flags),
    }


def _sweep_row(pattern: str | bytes, flags: int) -> dict[str, Any]:
    members, sre_flags = _parsed(pattern, flags)
    assert _refusal(members, sre_flags) is None, pattern
    if isinstance(pattern, bytes):
        found = bytearray(256)
        for hit in re.compile(pattern, flags).findall(bytes(range(256))):
            found[hit[0]] = 1
    else:
        found = bytearray(_N)
        for char in re.compile(pattern, flags).findall(_every()):
            found[ord(char)] = 1
    row: dict[str, Any] = {"part": "charset.sweep", "pattern": _spelled(pattern)}
    if isinstance(pattern, bytes):
        row["bytes"] = True
    row.update(
        flags=sre_flags,
        count=len(found),
        sha256=hashlib.sha256(bytes(found)).hexdigest(),
    )
    return row


#: The class of 256 distinct chunks, the most BIGCHARSET holds (chunk i
#: holds U+0101 * i alone). It is not swept: its row is the largest, and
#: the sweeps of the other bitmaps read the same words.
_CHUNKS = "[" + "".join(f"\\u{i * 257:04x}" for i in range(256)) + "]"

# The classes. Without IGNORECASE: one run, two runs, three (a bitmap), the
# map widened past Latin-1 by a member that never lands in it, the chunks
# of BIGCHARSET, a range past the BMP, categories, a negation, bytes.
PLAIN: list[tuple[str | bytes, int]] = [
    ("[abc]", 0),
    ("[ac]", 0),
    ("[ab]", 0),
    ("[a-cx-z]", 0),
    ("[acx]", 0),
    ("[a-z0-9_]", 0),
    ("[^a-z]", 0),
    ("[^ac]", 0),
    ("[\\x00-\\xff]", 0),
    ("[\\xfe\\xff]", 0),
    ("[\\xff\\u0100]", 0),
    ("[a\\u0100]", 0),
    ("[ab\\u0100]", 0),
    ("[ace\\u0100]", 0),
    ("[\\x00-\\uffff]", 0),
    ("[\\uffff\\U00010000]", 0),
    ("[\\U00010000a]", 0),
    ("[\\U00010000ab]", 0),
    ("[ace\\U00010000]", 0),
    ("[ace\\U0010ffff]", 0),
    ("[\\U00010000-\\U0010ffff]", 0),
    ("[\\uff00-\\U00010010a]", 0),
    ("[\\x00-\\U0010ffff]", 0),
    ("[^\\x00-\\U0010ffff]", 0),
    ("[\\u0100-\\u01ff\\u0300-\\u03ff\\u0500]", 0),
    ("[\\u0000-\\u00ff\\u0200-\\u02ff\\u0400-\\u04ffxyz]", 0),
    (_CHUNKS, 0),
    ("[\\d]", 0),
    ("[\\da-f]", 0),
    ("[^\\w]", 0),
    ("[\\w-]", 0),
    ("[\\W\\d]", 0),
    ("[^\\D\\s]", 0),
    ("[a-z]", _A),
    ("[\\w]", _A),
    (b"[a-z\\xff]", 0),
    (b"[\\x80-\\xff]", 0),
    (b"[ace]", 0),
    (b"[\\d]", 0),
    (b"[A-Z]", _L),
    (b"[A-Zz]", _L | _I),
    # The ASCII fold: A to Z only, no extra cases, a range past the BMP
    # left a RANGE.
    ("[A-Z]", _I | _A),
    ("[0-9]", _I | _A),
    ("[\\xc0-\\xde]", _I | _A),
    (b"[A-Z\\xff]", _I),
    (b"[a-c]", _I),
]

# The classes under the Unicode fold.
FOLDED: list[tuple[str, int]] = [
    ("[a-z]", _I),
    ("[A-Z]", _I),
    ("[0-9]", _I),
    ("[0-9a]", _I),
    ("[k0]", _I),
    ("[s0]", _I),
    ("[i0]", _I),
    ("[\\u0130\\u0131]", _I),
    ("[\\u03c3\\u03c2]", _I),
    ("[\\u03a3x]", _I),
    ("[\\xb5!]", _I),
    ("[\\xc0-\\xff]", _I),
    ("[\\u0400-\\u04ff]", _I),
    ("[\\u1e9e1]", _I),
    ("[\\u01c5!]", _I),
    ("[\\u212a!]", _I),
    ("[\\ufb00-\\ufb06]", _I),
    ("[^a-z]", _I),
    ("[\\d\\w]", _I),
    ("[\\d!]", _I),
    ("[a-zA-Z0-9_.-]", _I),
    ("[\\x00-\\uffff]", _I),
]

# The classes whose answer follows the release: a category beside its
# complement (3.14 makes ``[\\s\\S]`` a class of everything) and, under a
# fold, a member past the BMP (3.11 keeps such a literal unlowered, makes
# every such range RANGE_UNI_IGNORE and sets ``hascased`` for either).
RELEASE: list[tuple[str, int]] = [
    ("[\\s\\S]", 0),
    ("[\\W\\wa]", 0),
    ("[K\\U00010000-\\U00010001]", _I | _A),
    ("[\\U00010400x]", _I),
    ("[\\U00010400-\\U0001044f]", _I),
    ("[\\uff00-\\U00010010]", _I),
]

# Shapes the parser never writes: a NEGATE after the first member, a
# duplicate, an empty class.
BUILT: list[tuple[list[list[Any]], int]] = [
    ([["lit", 97], ["negate"], ["lit", 98]], _U),
    ([["lit", 97], ["lit", 97]], _U),
    ([["range", 97, 99], ["range", 98, 100]], _U),
    ([], _U),
    ([["negate"]], _U),
    ([["cat", "CATEGORY_DIGIT"], ["negate"]], _U),
    ([["lit", 0x10000], ["lit", 0x10000], ["lit", 97]], _U),
]

FUZZ_POOL = [
    0,
    9,
    0x30,
    0x41,
    0x4B,
    0x5A,
    0x5F,
    0x61,
    0x6B,
    0x73,
    0x7A,
    0xB5,
    0xDF,
    0xFF,
    0x100,
    0x130,
    0x131,
    0x17F,
    0x1C5,
    0x3A3,
    0x3C2,
    0x3C3,
    0x1E9E,
    0x212A,
    0xFEFF,
    0xFFFF,
    0x10000,
    0x10400,
    0x10FFFF,
]
FUZZ_SPANS = [0, 1, 2, 25, 255, 256, 0x1000]
FUZZ_CATEGORIES = [
    "CATEGORY_DIGIT",
    "CATEGORY_NOT_DIGIT",
    "CATEGORY_SPACE",
    "CATEGORY_NOT_SPACE",
    "CATEGORY_WORD",
    "CATEGORY_NOT_WORD",
]
FUZZ_COUNT = 600
FUZZ_SEED = 0xC4A5_5E7


def _drawn(seed: int, wide: bool) -> list[list[Any]]:
    """One to six members: a draw of 10 picks the kind (0 a NEGATE, 1 a
    category, 2 to 5 a literal of the pool, 6 to 9 a range from a code
    point of the pool over a span, cut at U+10FFFF). Unless ``wide``, the
    pool stops at U+FFFF, a range is cut there, and the categories are
    the three that are not a complement (DIGIT, SPACE, WORD), so that no
    answer follows the release."""
    lcg = Lcg(seed)
    pool = FUZZ_POOL if wide else [c for c in FUZZ_POOL if c <= 0xFFFF]
    categories = FUZZ_CATEGORIES if wide else FUZZ_CATEGORIES[::2]
    top = 0x10FFFF if wide else 0xFFFF
    members: list[list[Any]] = []
    for _ in range(1 + lcg.draw() % 6):
        kind = lcg.draw() % 10
        if kind == 0:
            members.append(["negate"])
        elif kind == 1:
            members.append(["cat", categories[lcg.draw() % len(categories)]])
        elif kind < 6:
            members.append(["lit", pool[lcg.draw() % len(pool)]])
        else:
            lo = pool[lcg.draw() % len(pool)]
            span = FUZZ_SPANS[lcg.draw() % len(FUZZ_SPANS)]
            members.append(["range", lo, min(lo + span, top)])
    return members


def _fuzz_row(flags: int, wide: bool = False) -> dict[str, Any]:
    digest = hashlib.sha256()
    for i in range(FUZZ_COUNT):
        built = [_built_member(m) for m in _drawn(FUZZ_SEED + i, wide)]
        digest.update((_answer(built, flags) + "\n").encode())
    return {
        "part": "charset.fuzz",
        "flags": flags,
        "seed": FUZZ_SEED,
        "wide": wide,
        "count": FUZZ_COUNT,
        "sha256": digest.hexdigest(),
    }


def _swept(pattern: str | bytes, flags: int) -> bool:
    members, sre_flags = _parsed(pattern, flags)
    return _refusal(members, sre_flags) is None and pattern != _CHUNKS


def _gated(pattern: str | bytes, flags: int) -> bool:
    """Whether the membership reads a Unicode table."""
    members, sre_flags = _parsed(pattern, flags)
    folds = bool(sre_flags & _I and sre_flags & _U)
    return folds or any(op is C.CATEGORY for op, _ in members)


def rows() -> list[dict[str, Any]]:
    out = [_class_row(p, f) for p, f in PLAIN]
    out += [_built_row(m, f) for m, f in BUILT]
    out += [_sweep_row(p, f) for p, f in PLAIN if _swept(p, f) and not _gated(p, f)]
    out += [_fuzz_row(_U), _fuzz_row(_I)]
    return out


def rows_unicode() -> list[dict[str, Any]]:
    out = [_class_row(p, f) for p, f in FOLDED]
    swept = PLAIN + FOLDED + RELEASE
    out += [_sweep_row(p, f) for p, f in swept if _swept(p, f) and _gated(p, f)]
    out += [_fuzz_row(_I | _U)]
    return out


def rows_python() -> list[dict[str, Any]]:
    out = [_class_row(p, f) for p, f in RELEASE]
    return out + [_fuzz_row(f, wide=True) for f in (_U, _I, _I | _U)]
