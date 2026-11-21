"""Fixture: XID_Start, XID_Continue and `str.isidentifier`
(``unicode_xid.jsonl``).

The native build reads it in
``native/crates/bettermemory-core/src/unicode/xid.rs``. The reference
reads `str.isidentifier` (``doctor.py``, the ``NAME=value`` words before a
command). Over every code point U+0000 to U+10FFFF, surrogates included,
one answer character ``"0123"[a | b << 1]``, where ``a`` is
``chr(cp).isidentifier()`` and ``b`` is ``("a" + chr(cp)).isidentifier()``;
the header holds the count and the SHA-256 of all the answers in order and
of each 0x1000 block. The cases are whole strings with their answer.

Gated on Unicode 15.1.0 only: the answers are the interpreter's Unicode
data, and the `_` rule the cases include is the same in every CPython 3.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

from ._common import jsonl, text_field

BLOCK = 0x1000

_CASES = (
    "",
    "_",
    "__",
    "a",
    "A1",
    "_1",
    "1",
    "1a",
    "a b",
    "a-b",
    "é",
    "·",
    "a·",
    "℘",
    "℮",
    "゛",
    "·",
    "a·",
    "፩",
    "a፩",
    "٠",
    "a٠",
    "＿",
    "a＿",
    "Ⅰ",
    "ﬁ",
    "ʰ",
    "ͺ",
    "aͺ",
    "à",
    "̀",
    "a‌",
    "a‍",
    "ำ",
    "\U0001d400",
    "\U00020000",
    "\U000e0100",
    "a\U000e0100",
    "\ud800",
    "a\ud800",
    "a\udfff",
    "\U0010ffff",
    "\x00",
    "a\t",
)


def answer(cp: int) -> str:
    ch = chr(cp)
    return "0123"[int(ch.isidentifier()) | int(("a" + ch).isidentifier()) << 1]


def generate() -> dict[str, bytes]:
    whole = hashlib.sha256()
    blocks = []
    for start in range(0, 0x110000, BLOCK):
        text = "".join(answer(cp) for cp in range(start, start + BLOCK)).encode()
        whole.update(text)
        blocks.append(hashlib.sha256(text).hexdigest())
    records: list[Any] = [
        {
            "fixture": "unicode_xid",
            "unidata_version": unicodedata.unidata_version,
            "rule": 'every code point: "0123"[isidentifier(c) | isidentifier("a"+c) << 1]',
            "count": 0x110000,
            "block_size": BLOCK,
            "sha256": whole.hexdigest(),
            "block_sha256": blocks,
        }
    ]
    for case in _CASES:
        records.append(
            {**text_field("input", case), "isidentifier": case.isidentifier()}
        )
    return {"unicode_xid.jsonl": jsonl(records)}
