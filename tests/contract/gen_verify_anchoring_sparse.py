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
import os
import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import verify_anchoring as va

from ._common import jsonl
from .gen_discovery import _clear_above


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


# --- unit 1b: the disk-reading half ------------------------------------------
#
# ``verify_anchoring_skip.jsonl`` (POSIX only: links on disk, paths spelled
# by ``posixpath.realpath``) pins `_sparse_enabled`, `_parts_below`,
# `_skip_index` with its memo and `_absent_by_sparsity` over small trees
# written to a scratch directory, and `gitfile_rule.object_format` over
# config texts with git read as 2.54. One JSON object per line: the
# header, then ``{"k": "o", "t": hex, "o": name or null}`` rows and
# ``{"k": "w", ...}`` worlds: ``f`` files (text, or ``{"hex"}``), ``d``
# folders, ``l`` links (``<root>`` the case's folder), ``home`` the
# folder ``~`` names, ``mv`` a file rewritten with ``{"hex"}`` just before
# its first ``read_bytes``, and ``q`` the queries in order, each
# ``{"p", "v"}`` over one memo:
#
# - ``S:<start>``: `_sparse_enabled(locate(start))`, null where nothing
#   is located;
# - ``B:<path>|<root>``: `_parts_below`, a list or null;
# - ``K:<start>``: `_skip_index(locate(start))` as ``{"i": null or
#   {"s", "k"}, "m": memo keys in order}``;
# - ``A:<path>``: `_absent_by_sparsity(path)`.

_REPO_FILES = {".git/HEAD": "ref: refs/heads/main\n"}
_REPO_DIRS = (".git/objects", ".git/refs")


def _skip_world(
    queries: list[str],
    *,
    files: dict[str, Any],
    dirs: tuple[str, ...] = (),
    links: dict[str, str] | None = None,
    mv: dict[str, bytes] | None = None,
    home: str | None = None,
) -> dict[str, Any]:
    return {
        "k": "w",
        "f": files,
        "d": list(dirs),
        "l": links or {},
        "mv": {k: {"hex": v.hex()} for k, v in (mv or {}).items()},
        "home": home,
        "q": queries,
    }


def _repo_at(name: str, config: str | None, index: bytes | None) -> dict[str, Any]:
    out: dict[str, Any] = {f"{name}/{k}": v for k, v in _REPO_FILES.items()}
    if config is not None:
        out[f"{name}/.git/config"] = config
    if index is not None:
        out[f"{name}/.git/index"] = {"hex": index.hex()}
    return out


def _repo_dirs(name: str) -> tuple[str, ...]:
    return tuple(f"{name}/{d}" for d in _REPO_DIRS)


def _bits(names: dict[bytes, bool], version: int = 2, h: int = 20) -> bytes:
    entries: list[dict[str, Any]] = []
    for name in sorted(names):
        e: dict[str, Any] = {"name": name}
        if names[name]:
            e["ext"] = 0x4000
        entries.append(e)
    return _index(3 if version == 2 else version, entries, hash_len=h)


_ON = "[core]\n\tsparseCheckout = true\n"
_OFF = "[core]\n\tsparseCheckout = false\n"
_BASE = _bits({b"a/x": True, b"a/y": True, b"b/z": False, b"m/k": False, b"m/s": True})


def _skip_worlds() -> list[dict[str, Any]]:
    worlds = []
    absent = [
        "A:<root>/r/a/x",
        "A:<root>/r/a",
        "A:<root>/r/a/x/deeper",
        "A:<root>/r/b/z",
        "A:<root>/r/m",
        "A:<root>/r/m/s",
        "A:<root>/r/c",
        "A:<root>/r",
        "A:<root>/r/b",
        "A:<root>/plain/q",
        "A:<root>/r//a/./x/",
        "A:~/r/a/x",
        "A:",
        "A:r/a/x",
    ]
    files = _repo_at("r", _ON, _BASE) | {"r/b/keep": "", "plain/f": ""}
    dirs = _repo_dirs("r")
    worlds.append(
        _skip_world(
            ["S:<root>/r", "K:<root>/r", "K:<root>/r/b", *absent],
            files=files,
            dirs=dirs,
            home="<root>",
        )
    )
    # The switch: every place and spelling.
    for config, worktree in [
        (_OFF, None),
        (None, None),
        (_ON, _OFF),
        (_OFF, _ON),
        (None, _ON),
        ("[core]\n\tsparseCheckout = true\n\tsparseCheckout = false\n", None),
        ("[core]\n\tsparseCheckout = false\n[core]\n\tsparsecheckout\n", None),
        ("[core]\r\n\tsparseCheckout = yes\r\n", None),
        ("[core]\r\tsparseCheckout = on\r", None),
        ("[core]\n\tsparseCheckout = false\r\tsparseCheckout = true\n", None),
        ("[core]\n\tsparseCheckout = maybe\n", None),
        (_ON, "[core]\n\tsparseCheckout = maybe\n"),
        (_ON, "[core\n"),
        ("[core\n", _ON),
        ("[core]\n\tsparseCheckout = 1\n\xff\n", None),
        ("[Core]\n\tSPARSECHECKOUT = true\n", None),
        ('[core "x"]\n\tsparseCheckout = true\n', None),
    ]:
        f = _repo_at("r", config, _BASE) | {"r/b/keep": ""}
        if worktree is not None:
            f["r/.git/config.worktree"] = worktree
        worlds.append(_skip_world(["S:<root>/r", "A:<root>/r/a/x"], files=f, dirs=dirs))
    # A config that is a folder; a config.worktree that is a folder.
    worlds.append(
        _skip_world(
            ["S:<root>/r", "K:<root>/r", "A:<root>/r/a/x"],
            files=_repo_at("r", None, _BASE) | {"r/.git/config.worktree": _ON},
            dirs=(*dirs, "r/.git/config"),
        )
    )
    # Parts below, through a symlinked root and in both directions.
    worlds.append(
        _skip_world(
            [
                "B:<root>/lr/a|<root>/r",
                "B:<root>/r/a/b|<root>/lr",
                "B:<root>/r|<root>/r",
                "B:<root>/plain|<root>/r",
                "B:<root>/rr|<root>/r",
                "B:<root>|<root>/r",
                "B:<root>/r/../r/b|<root>/r",
                "B:<root>/loop/x|<root>",
                "B:r/a|<root>/r",
                "A:<root>/lr/a/x",
                "A:<root>/lr/b/z",
                "A:<root>/lb/z",
                "S:<root>/lr",
            ],
            files=_repo_at("r", _ON, _BASE)
            | {"r/b/keep": "", "rr/f": "", "plain/f": ""},
            dirs=dirs,
            links={"lr": "<root>/r", "lb": "<root>/r/b", "loop": "<root>/loop"},
        )
    )
    # A sparse directory entry; a version-4 index; SHA-256.
    sparse_dir = _index(
        3,
        [
            {"name": b"d/", "ext": 0x4000},
            {"name": b"e/f", "ext": 0x4000},
            {"name": b"top"},
        ],
    )
    v4 = _bits({b"a/x": True, b"a/y": False}, version=4)
    s256 = _bits({b"a/x": True}, h=32)
    for config, index in [
        (_ON, sparse_dir),
        (_ON, v4),
        (_ON, s256),
        (_ON + "[extensions]\n\tobjectFormat = sha256\n", s256),
        (
            "[core]\n\trepositoryformatversion = 1\n\tsparseCheckout = true\n"
            "[extensions]\n\tobjectFormat = sha256\n",
            s256,
        ),
        (
            "[core]\n\trepositoryformatversion = 1\n\tsparseCheckout = true\n"
            "[extensions]\n\tobjectFormat = sha256\n",
            _BASE,
        ),
        (
            "[core]\n\trepositoryformatversion = 1\n\tsparseCheckout = true\n"
            "[extensions]\n\tobjectFormat = md5\n",
            _BASE,
        ),
        (_ON, None),
        (_ON, b"DIRC\0\0\0\x09"),
        (_ON, b""),
        (_ON, _index(2, [{"name": b"a/x"}], ext=b"link" + bytes(4))),
    ]:
        worlds.append(
            _skip_world(
                [
                    "K:<root>/r",
                    "A:<root>/r/d/e/f",
                    "A:<root>/r/e",
                    "A:<root>/r/a/x",
                    "A:<root>/r/a/y",
                    "A:<root>/r/top",
                ],
                files=_repo_at("r", config, index) | {"r/keep": ""},
                dirs=dirs,
            )
        )
    # An index that is a folder.
    worlds.append(
        _skip_world(
            ["K:<root>/r", "A:<root>/r/a/x"],
            files=_repo_at("r", _ON, None),
            dirs=(*dirs, "r/.git/index"),
        )
    )
    # Stamps that move during the read: the index, then the config.
    moved = _bits({b"a/x": False, b"a/q": True})
    for rel in ("r/.git/index", "r/.git/config"):
        new = moved if rel.endswith("index") else (_ON + "#moved\n").encode()
        worlds.append(
            _skip_world(
                ["K:<root>/r", "K:<root>/r", "K:<root>/r", "A:<root>/r/a/x"],
                files=_repo_at("r", _ON, _BASE),
                dirs=dirs,
                mv={rel: new},
            )
        )
    # A file moved during the read inside an A query: the switch is read
    # before the move (``read_text``), the index and the config after it.
    for rel in ("r/.git/index", "r/.git/config"):
        new = moved if rel.endswith("index") else b"[core]\n\tsparseCheckout = false\n"
        worlds.append(
            _skip_world(
                ["A:<root>/r/a/q", "K:<root>/r", "A:<root>/r/a/x", "K:<root>/r"],
                files=_repo_at("r", _ON, _BASE),
                dirs=dirs,
                mv={rel: new},
            )
        )
    # An index with no config in the common directory: the format reads
    # as sha1 and the index is parsed; the switch reads off.
    worlds.append(
        _skip_world(
            ["K:<root>/r", "A:<root>/r/a/x", "S:<root>/r"],
            files=_repo_at("r", None, _BASE),
            dirs=dirs,
        )
    )
    # The memo's bound and order: six repositories, one asked again.
    many: dict[str, Any] = {}
    d: tuple[str, ...] = ()
    for i in range(6):
        many |= _repo_at(f"r{i}", _ON, _bits({b"x": i % 2 == 0}))
        d += _repo_dirs(f"r{i}")
    worlds.append(
        _skip_world(
            [
                *(f"K:<root>/r{i}" for i in range(4)),
                "K:<root>/r0",
                "K:<root>/r4",
                "K:<root>/r5",
                "K:<root>/r0",
                "A:<root>/r1/x",
            ],
            files=many,
            dirs=d,
        )
    )
    # A linked worktree reads its own index and config.worktree, and the
    # common directory's config.
    wt = _repo_at("r", _ON, _BASE) | {
        "r/.git/worktrees/w/HEAD": "ref: refs/heads/w\n",
        "r/.git/worktrees/w/commondir": "../..\n",
        "r/.git/worktrees/w/gitdir": "<root>/w/.git\n",
        "r/.git/worktrees/w/config.worktree": _OFF,
        "r/.git/worktrees/w/index": {"hex": _bits({b"a/x": False}).hex()},
        "w/.git": "gitdir: <root>/r/.git/worktrees/w\n",
    }
    worlds.append(
        _skip_world(
            [
                "S:<root>/w",
                "S:<root>/r",
                "K:<root>/w",
                "A:<root>/w/a/x",
                "A:<root>/r/a/x",
            ],
            files=wt,
            dirs=dirs,
        )
    )
    return worlds


_FORMATS = [
    "",
    "[core]\n\trepositoryformatversion = 0\n",
    "[extensions]\n\tobjectFormat = sha256\n",
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat = sha256\n",
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat = SHA256\n",
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat = sha1\n",
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat = blake\n",
    "[core]\n\trepositoryformatversion = 2\n",
    "[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectFormat\n",
    "[core\n",
]


def _spell_path(path: str, real: str) -> str:
    return path.replace(real, "<root>")


def _skip_answer(world: dict[str, Any], base: Path) -> list[dict[str, Any]]:
    from bettermemory import githead
    from bettermemory import gitfile_rule as g

    base.mkdir(parents=True)
    saved = os.getcwd()
    os.chdir(base)
    try:
        real = os.getcwd()
        _clear_above(real)
        root = Path(real)
        for d in world["d"]:
            (root / d).mkdir(parents=True, exist_ok=True)
        for rel, value in world["f"].items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            data = (
                bytes.fromhex(value["hex"])
                if isinstance(value, dict)
                else value.replace("<root>", real).encode("utf-8", "surrogateescape")
            )
            path.write_bytes(data)
        for rel, target in world["l"].items():
            os.symlink(target.replace("<root>", real), root / rel)
        moves = {
            os.path.join(real, rel): bytes.fromhex(v["hex"])
            for rel, v in world["mv"].items()
        }
        original = Path.read_bytes

        def read_bytes(self: Path) -> bytes:
            new = moves.pop(os.fspath(self), None)
            if new is not None:
                original_write(self, new)
            return original(self)

        original_write = Path.write_bytes
        env = {"HOME": world["home"].replace("<root>", real)} if world["home"] else {}
        out = []
        va._clear_skip_memo()
        with (
            patch.dict(os.environ, env),
            patch.object(g, "_git_is_at_least", lambda want: (2, 54) >= want),
            patch.object(g, "_reads_compat", lambda: True),
            patch.object(Path, "read_bytes", read_bytes),
        ):
            for name in (
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_COMMON_DIR",
                "GIT_CEILING_DIRECTORIES",
                "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            ):
                os.environ.pop(name, None)
            for q in world["q"]:
                op, _, raw = q.partition(":")
                args = [a.replace("<root>", real) for a in raw.split("|")]
                v: Any
                if op == "S":
                    found = githead.locate(Path(args[0]))
                    v = None if found is None else va._sparse_enabled(found)
                elif op == "B":
                    got = va._parts_below(args[0], args[1])
                    v = None if got is None else [_spell_path(p, real) for p in got]
                elif op == "K":
                    found = githead.locate(Path(args[0]))
                    index = None if found is None else va._skip_index(found)
                    v = {
                        "i": None if index is None else _spell_index(index),
                        "m": [_spell_path(k, real) for k in va._SKIP_MEMO],
                    }
                else:
                    v = va._absent_by_sparsity(args[0])
                out.append({"p": q, "v": v})
        va._clear_skip_memo()
        return out
    finally:
        os.chdir(saved)


def generate_skip() -> dict[str, bytes]:
    """``verify_anchoring_skip.jsonl``: POSIX only."""
    from bettermemory import gitfile_rule as g

    from . import _sandbox
    from .gen_fs_realpath import refuse_root

    refuse_root()
    lines: list[dict[str, Any]] = []
    with patch.object(g, "_git_is_at_least", lambda want: (2, 54) >= want):
        for text in _FORMATS:
            got = g.object_format(text.encode())
            lines.append(
                {
                    "k": "o",
                    "t": text.encode().hex(),
                    "o": None if got is None else got.decode(),
                }
            )
    with _sandbox.world() as world:
        for i, w in enumerate(_skip_worlds()):
            lines.append({**w, "q": _skip_answer(w, world.root / "skip" / str(i))})
    header = {"k": "header", "count": len(lines), "cap": va._SKIP_MEMO_CAP}
    return {"verify_anchoring_skip.jsonl": jsonl([header, *lines])}
