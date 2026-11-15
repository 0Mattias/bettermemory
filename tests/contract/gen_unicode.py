"""Fixture 6: the per-code-point Unicode operations the product reads.

For every scalar value (U+0000 to U+10FFFF without the surrogates):

- maps (`unicodedata.normalize` NFC, NFD, NFKC; `str.casefold`,
  `str.lower`, `str.upper`): every code point whose result is not itself,
  with the result, except the Hangul syllables U+AC00 to U+D7A3 under NFD,
  which decompose algorithmically (Unicode 3.12) and are covered by the
  table hash only;
- properties (`unicodedata.category`, `unicodedata.combining`): runs of
  equal value;
- predicates (`str.isalpha`, `isalnum`, `isdigit`, `isspace`, and the `re`
  classes `\\w` and `\\d` the tokenizer uses): ranges where true.

Each map also has a SHA-256 over its full table, one line per code point,
``"%X\\t%s\\n" % (cp, " ".join("%X" % ord(c) for c in result))``, and
per 0x1000 block, so a native build can find where it diverges.
A single code point's normalisation does not exercise composition across
code points; `sequences` holds a set of multi-code-point inputs for that.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from ._common import jsonl

_MAPS: dict[str, Callable[[str], str]] = {
    "nfc": lambda s: unicodedata.normalize("NFC", s),
    "nfd": lambda s: unicodedata.normalize("NFD", s),
    "nfkc": lambda s: unicodedata.normalize("NFKC", s),
    "casefold": str.casefold,
    "lower": str.lower,
    "upper": str.upper,
}

_WORD = re.compile(r"\w")
_DIGIT = re.compile(r"\d")

_PREDICATES: dict[str, Callable[[str], bool]] = {
    "isalpha": str.isalpha,
    "isalnum": str.isalnum,
    "isdigit": str.isdigit,
    "isspace": str.isspace,
    "re_word": lambda c: _WORD.match(c) is not None,
    "re_digit": lambda c: _DIGIT.match(c) is not None,
}

_SEQUENCES = (
    "é",
    "Ẹ́",
    "ậ",
    "가",
    "각",
    "각",
    "ȫ",
    "Å",
    "Å",
    "q̣̇",
    "ẛ̣",
    "ﬁ",
    "ｶﾞ",
    "آ",
    "क़",
    "אַ",
    "ཀྵ",
    "\U0001d15e\U0001d165",
    "é́",
    "́e",
    "İ",
    "ß",
    "ﬃ",
    "ΐ",
)


def _scalars() -> range:
    return range(0x110000)


def _hex(text: str) -> str:
    return " ".join(f"{ord(c):X}" for c in text)


def _map_table(fn: Callable[[str], str], hangul_exempt: bool) -> dict[str, Any]:
    whole = hashlib.sha256()
    blocks: list[str] = []
    block = hashlib.sha256()
    diffs: list[list[Any]] = []
    for cp in _scalars():
        if cp and cp % 0x1000 == 0:
            blocks.append(block.hexdigest())
            block = hashlib.sha256()
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        result = fn(ch)
        line = f"{cp:X}\t{_hex(result)}\n".encode("ascii")
        whole.update(line)
        block.update(line)
        if result != ch and not (hangul_exempt and 0xAC00 <= cp <= 0xD7A3):
            diffs.append([cp, [ord(c) for c in result]])
    blocks.append(block.hexdigest())
    return {"sha256": whole.hexdigest(), "block_sha256": blocks, "changed": diffs}


def _runs(fn: Callable[[str], Any]) -> list[list[Any]]:
    runs: list[list[Any]] = []
    for cp in _scalars():
        if 0xD800 <= cp <= 0xDFFF:
            continue
        value = fn(chr(cp))
        if not runs or runs[-1][1] != value:
            runs.append([cp, value])
    return runs


def _ranges(fn: Callable[[str], bool]) -> list[list[int]]:
    out: list[list[int]] = []
    for cp in _scalars():
        if 0xD800 <= cp <= 0xDFFF or not fn(chr(cp)):
            continue
        if out and out[-1][1] == cp - 1:
            out[-1][1] = cp
        else:
            out.append([cp, cp])
    return out


def generate() -> dict[str, bytes]:
    doc: dict[str, Any] = {
        "fixture": "unicode",
        "unidata_version": unicodedata.unidata_version,
        "scope": "U+0000..U+10FFFF without U+D800..U+DFFF",
        "line_format": "%X<TAB>space-separated %X of the result<LF>, ASCII",
        "block_size": 0x1000,
        "maps": {
            name: _map_table(fn, hangul_exempt=name == "nfd")
            for name, fn in _MAPS.items()
        },
        "category_runs": _runs(unicodedata.category),
        "combining_runs": _runs(unicodedata.combining),
        "predicates": {name: _ranges(fn) for name, fn in _PREDICATES.items()},
        "sequences": [
            {
                "input": [ord(c) for c in s],
                **{name: [ord(c) for c in fn(s)] for name, fn in _MAPS.items()},
            }
            for s in _SEQUENCES
        ],
    }
    return {"unicode.jsonl": jsonl([doc])}
