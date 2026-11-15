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


# ---------------------------------------------------------------------------
# unicode_sequences.jsonl: NFKD, and case mappings that read context or
# produce more than one code point, on sequences.
# ---------------------------------------------------------------------------

_SEQ_MAPS: dict[str, Callable[[str], str]] = {
    **_MAPS,
    "nfkd": lambda s: unicodedata.normalize("NFKD", s),
}

_SIGMA = "\u03a3"


def _sigma_contexts() -> list[str]:
    """``str.lower`` maps capital sigma to final sigma when a cased
    letter precedes it and none follows, skipping case-ignorable code
    points both ways (Unicode's Final_Sigma). Every way that reads."""
    alpha, latin = "\u0391", "a"
    ignorable = [
        "'",
        ".",
        ":",
        "\u00ad",
        "\u0301",
        "\u0345",
        "\u02b0",
        "\u2019",
        "\u200d",
    ]
    out = [
        _SIGMA,
        alpha + _SIGMA,
        _SIGMA + alpha,
        alpha + _SIGMA + alpha,
        latin + _SIGMA,
        "1" + _SIGMA,
        alpha + _SIGMA + "1",
        alpha + _SIGMA + " ",
        alpha + " " + _SIGMA,
        alpha + _SIGMA + _SIGMA,
        alpha + _SIGMA + _SIGMA + alpha,
        _SIGMA + _SIGMA,
        _SIGMA + " " + _SIGMA,
        "\u01c5" + _SIGMA,
        "\u1f88" + _SIGMA,
        "\u2160" + _SIGMA,
        "\u24b6" + _SIGMA,
        "\U0001d400" + _SIGMA,
        "\U00010400" + _SIGMA,
        "\u00aa" + _SIGMA,
        "\u02b0" + _SIGMA,
        alpha + "\U0001f600" + _SIGMA,
        alpha + "\udc80" + _SIGMA,
        alpha + _SIGMA + "\udc80",
        "\u03c3" + _SIGMA,
        "\u03c2" + _SIGMA + "\u03c2",
        alpha + _SIGMA + "\u0345" + alpha,
        alpha + "\u0345" + _SIGMA,
    ]
    for ig in ignorable:
        out += [
            alpha + ig + _SIGMA,
            alpha + _SIGMA + ig,
            alpha + _SIGMA + ig + alpha,
            alpha + ig + ig + _SIGMA + ig + ig,
            ig + _SIGMA,
        ]
    out = list(dict.fromkeys(out))
    words = [
        "\u039f\u0394\u03a5\u03a3\u03a3\u0395\u03a5\u03a3",
        "\u03a3\u0399\u03a3\u03a5\u03a6\u039f\u03a3.",
    ]
    return out + words


_MULTI = [
    "stra\u00dfe",
    "STRASSE \u1e9e",
    "\u0130STANBUL",
    "\u0130\u0307",
    "i\u0307\u0130",
    "\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06",
    "\u0149n",
    "\u01f0\u0323",
    "\u0390\u03b0",
    "\u1f80\u1f88\u1fb3\u1fbc\u1fc3\u1fcc\u1ff3\u1ffc",
    "\u1fb2\u1fb4\u1fb6\u1fb7",
    "\u0587\ufb13\ufb17",
    "\u1e96\u1e97\u1e98\u1e99\u1e9a",
    "\u0130\u03a3",
    "\u00df\u03a3",
    "A\u0130\u03a3",
    "\u03a3\u0345",
    "\u2126\u212a\u212b",
    "\u01c4\u01c5\u01c6\u01c7\u01c8\u01c9",
    "\u0131i\u0130I",
    "\U00010400\U00010428\U0001e900\U0001e922",
    "\u1e9b\u0323\u017f",
    "\u3392\u33a1\ufdfa",
    "\ufdfa\u03a3",
    "\u00bd\u2075\u2082\u2460",
    "\uac00\u11a8\u1100\u1161",
    "\u0f77\u0f79",
    "\u2fa1d\u2f800",
]


def sequences() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "fixture": "unicode_sequences",
            "unidata_version": unicodedata.unidata_version,
            "maps": sorted(_SEQ_MAPS),
            "map_table": (
                "nfkd over every scalar value, as _map_table in unicode.jsonl "
                "(Hangul syllables changed by the algorithm are left out of "
                "changed, kept in the hashes)"
            ),
            "sequences": (
                "input (code points) through every map: final sigma in every "
                "context str.lower distinguishes, multi-code-point case "
                "mappings next to each other and to sigma"
            ),
        },
        {"map": "nfkd", **_map_table(_SEQ_MAPS["nfkd"], hangul_exempt=True)},
    ]
    inputs = _sigma_contexts() + _MULTI
    assert len(set(inputs)) == len(inputs)
    for kind, group in (("sigma", _sigma_contexts()), ("multi", _MULTI)):
        for s in group:
            records.append(
                {
                    "kind": kind,
                    "input": [ord(c) for c in s],
                    **{name: [ord(c) for c in fn(s)] for name, fn in _SEQ_MAPS.items()},
                }
            )
    return {"unicode_sequences.jsonl": jsonl(records)}
