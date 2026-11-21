"""Fixtures: the leaves of the sparse-checkout reading
(``verify_anchoring_sparse.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/verify_anchoring/sparse.rs``. This
pins `verify_anchoring`'s `_config_switch`, `_index_varint`,
`_parse_skip_index` and `_SkipIndex.skips` (through it `_holds` and
`_any_below`), over hand-built config texts and index bytes.

One JSON object per line, keys sorted, ASCII. The first line is the header
``{"k": "header", "count": n, "flags": [...], "hash_len": {...}}`` (the
three flag constants and `_INDEX_HASH_LEN` with object format names as
text). Every other line is a case, ``f`` naming the function:

- ``"c"``: `_config_switch(t, s, k)`, each a list of code points; ``o`` is
  true, false, null or ``"UnicodeEncodeError"``;
- ``"v"``: `_index_varint(d, p)`; ``o`` is ``[value, pos]`` or
  ``"declined"``;
- ``"p"``: `_parse_skip_index(d, h)`; ``o`` is ``{"s": skipped, "k":
  kept}`` or ``"declined"``;
- ``"s"``: `_SkipIndex(s, k).skips(parts)`; ``o`` a boolean;
- ``"sweep"``: every truncation of each base index ``d`` and every one of
  its bytes set in turn to 0x00 and to 0xff (in that order: the
  truncations to lengths 0 up to the whole, then position by position
  0x00 then 0xff), each parsed with ``h``; ``n`` answers, ``sha256`` the
  digest of their spellings, each followed by a newline: ``D`` for a
  decline, else ``s`` and the skipped names in hex joined by ``,``, then
  ``|k`` and the kept names likewise.

Bytes are spelled ``d``: a list of pieces, a string of hex or ``[hex
byte, count]`` for a run. Names in answers are hex. Nothing here depends
on the Python release, the Unicode data or the platform: the config texts
and names are ASCII but for the encode errors (a lone surrogate, and a
name whose lower case is not ASCII, U+0130) and U+212A, whose lower case
is ASCII ``k`` under every Unicode release.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

from bettermemory import verify_anchoring as va

from ._common import jsonl


def _d(data: bytes) -> list[Any]:
    out: list[Any] = []
    plain = bytearray()
    i = 0
    while i < len(data):
        j = i
        while j < len(data) and data[j] == data[i]:
            j += 1
        if j - i >= 16:
            if plain:
                out.append(plain.hex())
                plain = bytearray()
            out.append([f"{data[i]:02x}", j - i])
        else:
            plain += data[i:j]
        i = j
    if plain:
        out.append(plain.hex())
    return out


def _cp(text: str) -> list[int]:
    return [ord(c) for c in text]


def _entry(
    name: bytes,
    *,
    version: int,
    hash_len: int,
    previous: bytes = b"",
    stage: int = 0,
    ext: int | None = None,
    length: int | None = None,
    strip: int | None = None,
) -> bytes:
    flags = (stage << 12) | (0x4000 if ext is not None else 0)
    flags |= min(len(name), 0xFFF) if length is None else length
    head = bytes(40) + bytes([0xAB]) * hash_len + struct.pack(">H", flags)
    if ext is not None:
        head += struct.pack(">H", ext)
    if version == 4:
        common = 0
        if strip is None:
            while (
                common < min(len(previous), len(name))
                and previous[common] == name[common]
            ):
                common += 1
            strip = len(previous) - common
        else:
            common = len(previous) - strip
        return head + _varint(strip) + name[common:] + b"\0"
    body = head + name
    pad = 8 - (len(body) % 8)
    return body + bytes(pad)


def _varint(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        value -= 1
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(out))


def _index(
    version: int,
    entries: list[dict[str, Any]],
    *,
    hash_len: int = 20,
    ext: bytes = b"",
    count: int | None = None,
) -> bytes:
    body = b"DIRC" + struct.pack(
        ">II", version, len(entries) if count is None else count
    )
    previous = b""
    for e in entries:
        e = dict(e)
        name = e.pop("name")
        body += _entry(name, version=version, hash_len=hash_len, previous=previous, **e)
        previous = name
    return body + ext + bytes([0xEE]) * hash_len


def _spell_index(got: Any) -> Any:
    return {"s": [n.hex() for n in got.skipped], "k": [n.hex() for n in got.kept]}


def _parse(data: bytes, h: int) -> Any:
    try:
        return _spell_index(va._parse_skip_index(data, h))
    except va._IndexDeclined:
        return "declined"


def _sweep_line(data: bytes, h: int) -> str:
    try:
        got = va._parse_skip_index(data, h)
    except va._IndexDeclined:
        return "D"
    return (
        "s"
        + ",".join(n.hex() for n in got.skipped)
        + "|k"
        + ",".join(n.hex() for n in got.kept)
    )


SKIP = 0x4000
ITA = 0x2000


def _indexes() -> list[tuple[bytes, int]]:
    out: list[tuple[bytes, int]] = []
    basic: list[dict[str, Any]] = [
        {"name": b"a.txt"},
        {"name": b"docs/guide.md", "ext": SKIP},
        {"name": b"docs/intro.md", "ext": SKIP},
        {"name": b"src/main.rs", "ext": 0},
        {"name": b"src/x", "ext": ITA},
        {"name": b"z", "stage": 1, "ext": SKIP},
        {"name": b"z", "stage": 2},
        {"name": b"z", "stage": 3},
    ]
    for v in (2, 3, 4):
        out.append((_index(v, basic), 20))
        out.append((_index(v, basic, hash_len=32), 32))
        # The same bytes read with the other hash length.
        out.append((_index(v, basic), 32))
        out.append((_index(v, basic, hash_len=32), 20))
    # A sparse index's directory entry, unsorted input, an empty name.
    sparse: list[dict[str, Any]] = [
        {"name": b"b/", "ext": SKIP},
        {"name": b"a", "ext": 0},
        {"name": b""},
        {"name": b"b/c", "ext": SKIP | ITA},
    ]
    for v in (2, 3, 4):
        out.append((_index(v, sparse), 20))
    # Unknown extended bits.
    for bit in (0x1000, 0x8000, 0x0001):
        out.append((_index(3, [{"name": b"a", "ext": bit}]), 20))
    # Long names: 4094, 4095 (found by NUL), 5000.
    for n in (4094, 4095, 5000):
        name = b"d/" + b"n" * (n - 2)
        for v in (2, 4):
            out.append((_index(v, [{"name": name, "ext": SKIP}, {"name": b"e"}]), 20))
    # A stated length that disagrees with the name.
    for v in (2, 4):
        out.append((_index(v, [{"name": b"abc", "length": 2}]), 20))
        out.append((_index(v, [{"name": b"abc", "length": 4}]), 20))
    out.append((_index(4, [{"name": b"abc", "length": 0xFFF}]), 20))
    out.append((_index(2, [{"name": b"ab\0c", "length": 0xFFF}]), 20))
    # Version 4: a strip past the previous name; a strip that empties it.
    out.append((_index(4, [{"name": b"ab"}, {"name": b"x", "strip": 3}]), 20))
    out.append((_index(4, [{"name": b"ab"}, {"name": b"ab", "strip": 2}]), 20))
    out.append((_index(4, [{"name": b"abc/d"}, {"name": b"abc/e", "ext": SKIP}]), 20))
    # Versions git refuses, a bad signature, counts that run past the end.
    for v in (0, 1, 5, 0xFFFFFFFF):
        out.append((_index(v, [{"name": b"a"}]), 20))
    out.append((b"DIRD" + _index(2, [{"name": b"a"}])[4:], 20))
    out.append((_index(2, [{"name": b"a"}], count=2), 20))
    out.append((_index(2, [{"name": b"a"}], count=0xFFFFFFFF), 20))
    out.append((_index(2, [], count=0), 20))
    out.append((_index(2, [{"name": b"a"}], count=0), 20))
    # Extensions: one read past, a link, one whose size runs past, a short tail.
    tree = b"TREE" + struct.pack(">I", 3) + b"xyz"
    link = b"link" + struct.pack(">I", 20) + bytes(20)
    out.append((_index(2, [{"name": b"a"}], ext=tree), 20))
    out.append((_index(2, [{"name": b"a"}], ext=link), 20))
    out.append((_index(2, [{"name": b"a"}], ext=tree + link), 20))
    out.append((_index(2, [{"name": b"a"}], ext=b"TREE" + struct.pack(">I", 999)), 20))
    out.append(
        (_index(2, [{"name": b"a"}], ext=b"TREE" + struct.pack(">I", 999) + link), 20)
    )
    out.append((_index(2, [{"name": b"a"}], ext=b"link123"), 20))
    out.append((_index(2, [{"name": b"a"}], ext=b"ab" + link), 20))
    # Too short for a header, and every short prefix of the signature.
    for data in (b"", b"DIRC", b"DIRC" + bytes(7), b"DIRC" + bytes(8)):
        out.append((data, 0))
        out.append((data + bytes(20), 20))
    out.append((b"DIRC\0\0\0\x02\0\0\0\0", 0))
    # Version 4 with a varint that runs into the trailer.
    v4 = _index(4, [{"name": b"a"}])
    out.append((v4[: 12 + 62] + b"\xff" * 2, 20))
    return out


def _bases() -> list[tuple[bytes, int]]:
    return [
        (
            _index(
                2, [{"name": b"a/b", "ext": SKIP}, {"name": b"c"}], ext=b"TREE\0\0\0\0"
            ),
            20,
        ),
        (_index(3, [{"name": b"a/", "ext": SKIP}, {"name": b"b", "ext": ITA}]), 20),
        (
            _index(
                4, [{"name": b"ab/c", "ext": SKIP}, {"name": b"ab/d"}, {"name": b"e"}]
            ),
            32,
        ),
    ]


def _sweep(data: bytes, h: int) -> dict[str, Any]:
    lines = []
    for n in range(len(data) + 1):
        lines.append(_sweep_line(data[:n], h))
    for i in range(len(data)):
        for b in (0x00, 0xFF):
            lines.append(_sweep_line(data[:i] + bytes([b]) + data[i + 1 :], h))
    digest = hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()
    return {"f": "sweep", "d": _d(data), "h": h, "n": len(lines), "sha256": digest}


_VARINTS = [
    b"",
    b"\x00",
    b"\x7f",
    b"\x80",
    b"\x80\x00",
    b"\x80\x7f",
    b"\xff\x7f",
    b"\x81\x00",
    b"\xff\xff\x7f",
    b"\x80" * 7 + b"\x00",
    b"\xff" * 7 + b"\x7f",
    b"\x80" * 8 + b"\x00",
    b"\xff" * 8 + b"\x7f",
    b"\xfe" + b"\xff" * 7 + b"\x7f",
    b"\x80" * 9 + b"\x00",
    b"\x80" * 20 + b"\x00",
]


def _varint_cases() -> list[dict[str, Any]]:
    out = []
    for data in _VARINTS:
        for pos in sorted({0, 1, len(data), len(data) + 1}):
            try:
                o: Any = list(va._index_varint(data, pos))
            except va._IndexDeclined:
                o = "declined"
            out.append({"f": "v", "d": _d(data), "p": pos, "o": o})
    for value in (0, 1, 127, 128, 16511, 16512, 2**56, 2**57 - 1, 2**63, 2**64 - 1):
        data = _varint(value) + b"!"
        try:
            o = list(va._index_varint(data, 0))
        except va._IndexDeclined:
            o = "declined"
        out.append({"f": "v", "d": _d(data), "p": 0, "o": o})
    return out


_CONFIGS = [
    "",
    "[core]\n\tsparseCheckout = true\n",
    "[core]\n\tsparsecheckout\n",
    "[core]\n\tsparseCheckout = false\n",
    "[core]\n\tsparseCheckout = true\n[core]\n\tsparseCheckout = no\n",
    "[core]\n\tsparseCheckout = no\n\tsparseCheckout = yes\n",
    "[core]\n\tsparseCheckout =\n",
    "[core]\n\tsparseCheckout = 2\n",
    "[core]\n\tsparseCheckout = 0x10\n",
    "[core]\n\tsparseCheckout = maybe\n",
    "[core]\n\tsparseCheckout = true\n\tsparseCheckout = maybe\n",
    "[core]\n\tsparseCheckout = maybe\n\tsparseCheckout = true\n",
    '[core "x"]\n\tsparseCheckout = true\n',
    "[Core]\n\tSPARSECHECKOUT = On\n",
    "[core.x]\n\tsparseCheckout = true\n",
    '[core]\n\tsparseCheckout = "tr\\\nue"\n',
    "[core\n\tsparseCheckout = true\n",
    "﻿[core]\n\tsparseCheckout = true\n",
    "[core]\r\n\tsparseCheckout = true\r\n",
    "[core]\n\tsparseCheckout = 1k\n",
    "[core]\n\tsparseCheckout = 99999999999999999999\n",
    "[core]\n\tsparseCheckout = true\n# [core] sparseCheckout = false\n",
    "[core] sparseCheckout = false ; true\n",
    "[core]\n\tsparseCheckout = \udcff\n",
    "[core]\n\tsparseCheckout = é\n",
    "[core]\n\tsparseCheckout = \ud800\n",
    "[\udc80]\n",
]


def _config_cases() -> list[dict[str, Any]]:
    out = []
    names = [
        ("core", "sparsecheckout"),
        ("core", "sparseCheckout"),
        ("CORE", "SPARSECHECKOUT"),
    ]
    for text in _CONFIGS:
        for section, key in names[:1] if text else names:
            out.append(_config(text, section, key))
    for section, key in [
        ("core", "Key"),
        ("core", "İ"),
        ("cöre", "x"),
        ("core", ""),
        ("", "sparsecheckout"),
        ("core.x", "sparsecheckout"),
        ("core", "sparse.checkout"),
    ]:
        out.append(
            _config("[core]\n\tkey = true\n\tsparseCheckout = off\n", section, key)
        )
        out.append(_config("[core", section, key))
    out.append(
        _config('[core "x"]\n\tsparseCheckout = true\n', "core.x", "sparsecheckout")
    )
    return out


def _config(text: str, section: str, key: str) -> dict[str, Any]:
    try:
        o: Any = va._config_switch(text, section, key)
    except UnicodeEncodeError:
        o = "UnicodeEncodeError"
    return {"f": "c", "t": _cp(text), "s": _cp(section), "k": _cp(key), "o": o}


def _skips_cases() -> list[dict[str, Any]]:
    worlds = [
        ([b"a/b/c", b"a/b/d"], [b"a/e"]),
        ([b"a/", b"x"], [b"a"]),
        ([b"a-b", b"a/b"], [b"a.b", b"a0"]),
        ([b"a/b/"], [b"a/b/c"]),
        ([], []),
        ([b""], [b"/"]),
        ([b"/x"], []),
        ([b"\xff/x"], [b"\xff/y"]),
    ]
    probes = [
        [],
        [b""],
        [b"a"],
        [b"a", b"b"],
        [b"a", b"b", b"c"],
        [b"a", b"b", b"c", b"q"],
        [b"a", b"e"],
        [b"a", b"z"],
        [b"x"],
        [b"a-b"],
        [b"", b"x"],
        [b"\xff"],
        [b"a/b"],
    ]
    out = []
    for skipped, kept in worlds:
        index = va._SkipIndex(tuple(sorted(skipped)), tuple(sorted(kept)))
        for parts in probes:
            out.append(
                {
                    "f": "s",
                    "skipped": [n.hex() for n in index.skipped],
                    "kept": [n.hex() for n in index.kept],
                    "parts": [p.hex() for p in parts],
                    "o": index.skips(parts),
                }
            )
    return out


def generate() -> dict[str, bytes]:
    """``verify_anchoring_sparse.jsonl``: not gated."""
    cases: list[dict[str, Any]] = []
    cases += _config_cases()
    cases += _varint_cases()
    for data, h in _indexes():
        cases.append({"f": "p", "d": _d(data), "h": h, "o": _parse(data, h)})
    cases += _skips_cases()
    for data, h in _bases():
        cases.append(_sweep(data, h))
    header = {
        "k": "header",
        "count": len(cases),
        "flags": [va._CE_EXTENDED, va._CE_SKIP_WORKTREE, va._CE_EXTENDED_FLAGS],
        "hash_len": {k.decode(): v for k, v in va._INDEX_HASH_LEN.items()},
    }
    return {"verify_anchoring_sparse.jsonl": jsonl([header, *cases])}
