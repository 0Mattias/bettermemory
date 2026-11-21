"""Fixtures: the object questions of `gitobjects.ObjectReader`
(``gitobjects_reader.jsonl``, ``gitobjects_reader_zlib.jsonl``,
``gitobjects_reader_posix.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitobjects/files/reader.rs``. This
pins it to ``has``, ``has_many``, ``name_length``, ``resolve``,
``header``, ``read``, ``commit``, ``tree`` and ``fingerprint``, and
through them ``_raw``, ``_places``, ``_object``, ``_take_loose``,
``_loose`` and ``_packed``.

Each case builds a scratch tree under a root and makes
``ObjectReader(<root>/g)``:

- ``t``: the tree, each entry ``["d", rel]`` (a folder), ``["f", rel,
  bytes]`` (a file, bytes as `gen_gitobjects._sp` spells them), ``["p",
  rel]`` (a FIFO) or ``["m", rel, mode]`` (the permission bits of what is
  there). Every object store is written here by hand: loose objects and
  pack entries are zlib streams of stored blocks (no compressor's output,
  which differs between zlib builds), packs and version 2 indexes are
  laid out byte by byte, and object names are the content's SHA-1 or
  SHA-256;
- ``st``: ``os.stat`` claims as in ``gen_gitobjects_files`` (``f``:
  SF_DATALESS);
- ``q``: the questions in order: ``"n"`` (``name_length``), ``"c"``
  (``complete``), ``"e"`` (``evicted``), ``["h", sha]`` (``has``),
  ``["m", [sha, ...]]`` (``has_many``, sorted), ``["v", prefix]``
  (``resolve``), ``["H", sha]`` (``header``: the type and the size in
  hex), ``["R", sha, cap]``, ``["C", sha, cap]``, ``["T", sha, cap]``,
  ``["F", sha, cap]`` (``read``, ``commit``, ``tree``, ``fingerprint``;
  a cap of null is the default) or ``["w", rel, bytes]`` (a file written
  first).

Each answers ``a``, one per question: content as hex up to 64 bytes, else
``{"n": length, "sha256": digest}``; a commit as its fields (bytes in
hex); a tree as ``[mode, name hex, sha]`` triples; null for a write; or
``{"raise": class}``.

``gitobjects_reader.jsonl`` holds the cases every zlib inflates alike
(whole streams of stored blocks, or files cut where no stream is begun).
``gitobjects_reader_zlib.jsonl`` holds damaged streams, whose answer is
where the linked zlib's ``inflate`` stops (``ZLIB_GATED``).
``gitobjects_reader_posix.jsonl`` holds FIFOs, modes and alternates, POSIX
only.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import zlib
from typing import Any
from unittest import mock

from bettermemory import gitfile_rule, gitobjects

from ._common import jsonl
from .gen_gitobjects import _sp
from .gen_gitobjects_files import _abs, _claims, _rel, _spelled
from .gen_store_files import host_fact

_CODES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}


def _deflate(data: bytes, block: int = 65535) -> bytes:
    """A zlib stream of stored blocks of `data`."""
    out = bytearray(b"\x78\x01")
    pieces = [data[i : i + block] for i in range(0, len(data), block)] or [b""]
    for i, piece in enumerate(pieces):
        out.append(1 if i == len(pieces) - 1 else 0)
        out += len(piece).to_bytes(2, "little")
        out += (len(piece) ^ 0xFFFF).to_bytes(2, "little")
        out += piece
    out += zlib.adler32(data).to_bytes(4, "big")
    return bytes(out)


def _name(kind: str, body: bytes, wide: bool = False) -> bytes:
    algo = hashlib.sha256 if wide else hashlib.sha1
    return algo(f"{kind} {len(body)}\0".encode() + body).digest()


def _loose(kind: str, body: bytes, wide: bool = False) -> tuple[str, bytes]:
    """(rel path under objects, file bytes) of a loose object."""
    sha = _name(kind, body, wide).hex()
    return f"{sha[:2]}/{sha[2:]}", _deflate(f"{kind} {len(body)}\0".encode() + body)


def _size_varint(code: int, size: int) -> bytes:
    out = bytearray()
    c = (code << 4) | (size & 15)
    size >>= 4
    while size:
        out.append(c | 0x80)
        c = size & 0x7F
        size >>= 7
    out.append(c)
    return bytes(out)


def _ofs(distance: int) -> bytes:
    out = [distance & 0x7F]
    distance >>= 7
    while distance:
        distance -= 1
        out.append(0x80 | (distance & 0x7F))
        distance >>= 7
    return bytes(reversed(out))


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        c = n & 0x7F
        n >>= 7
        if n:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


def _delta(source: bytes, target: bytes) -> bytes:
    """A delta copying `source` whole where it fits, then inserting the
    rest of `target`."""
    out = bytearray(_varint(len(source)) + _varint(len(target)))
    done = 0
    if source and target.startswith(source) and len(source) < 0x10000:
        if len(source) < 256:
            out += bytes([0x90, len(source)])
        else:
            out += bytes([0xB0, len(source) & 0xFF, len(source) >> 8])
        done = len(source)
    rest = target[done:]
    for i in range(0, len(rest), 127):
        piece = rest[i : i + 127]
        out += bytes([len(piece)]) + piece
    return bytes(out)


class _Pack:
    """A pack laid out entry by entry; `idx` its version 2 index."""

    def __init__(self, wide: bool = False) -> None:
        self.data = bytearray(b"PACK\x00\x00\x00\x02\x00\x00\x00\x00")
        self.names: list[tuple[bytes, int]] = []
        self.wide = wide

    def raw(self, name: bytes, entry: bytes) -> int:
        at = len(self.data)
        self.data += entry
        self.names.append((name, at))
        return at

    def whole(self, kind: str, body: bytes) -> bytes:
        name = _name(kind, body, self.wide)
        self.raw(name, _size_varint(_CODES[kind], len(body)) + _deflate(body))
        return name

    def ofs(self, kind: str, base_at: int, delta: bytes, target: bytes) -> bytes:
        name = _name(kind, target, self.wide)
        at = len(self.data)
        head = _size_varint(6, len(delta)) + _ofs(at - base_at)
        self.raw(name, head + _deflate(delta))
        return name

    def ref(self, kind: str, base: bytes, delta: bytes, target: bytes) -> bytes:
        name = _name(kind, target, self.wide)
        self.raw(name, _size_varint(7, len(delta)) + base + _deflate(delta))
        return name

    def at(self, name: bytes) -> int:
        return dict(self.names)[name]

    def idx(self, large: dict[bytes, int] | None = None) -> bytes:
        large = large or {}
        hash_len = 32 if self.wide else 20
        entries = sorted(self.names)
        fanout = [sum(1 for n, _ in entries if n[0] <= b) for b in range(256)]
        out = bytearray(b"\xfftOc\x00\x00\x00\x02")
        for count in fanout:
            out += count.to_bytes(4, "big")
        for name, _ in entries:
            out += name
        out += b"\x00" * (4 * len(entries))
        table = bytearray()
        for name, at in entries:
            if name in large:
                out += (0x80000000 | (len(table) // 8)).to_bytes(4, "big")
                table += large[name].to_bytes(8, "big")
            else:
                out += at.to_bytes(4, "big")
        out += table
        out += b"\x00" * (2 * hash_len)
        return bytes(out)


def _f(rel: str, data: bytes) -> list[Any]:
    return ["f", rel, _sp(data)]


def _obj(rel: str, data: bytes, objects: str = "g/objects") -> list[Any]:
    return _f(f"{objects}/{rel}", data)


def _packed(
    p: _Pack, stem: str = "p", objects: str = "g/objects", **kw: Any
) -> list[Any]:
    return [
        _f(f"{objects}/pack/{stem}.pack", bytes(p.data)),
        _f(f"{objects}/pack/{stem}.idx", p.idx(**kw)),
    ]


BASE = [["d", "g/objects/pack"]]
SHA256 = (
    b"[core]\n\trepositoryformatversion = 1\n[extensions]\n\tobjectformat = sha256\n"
)

BLOB = b"hello\n"


def _tree_body(blob: bytes) -> bytes:
    return b"100644 a\x00" + blob


def _commit(tree: bytes, parents: list[bytes], message: bytes = b"m\n") -> bytes:
    out = b"tree " + tree.hex().encode() + b"\n"
    for p in parents:
        out += b"parent " + p.hex().encode() + b"\n"
    out += b"author A <a@b> 1700000000 +0000\ncommitter C <c@d> 1700000001 +0000\n\n"
    return out + message


def _case(name: str, t: list[Any], q: list[Any], **more: Any) -> dict[str, Any]:
    return {"name": name, "t": t, "q": q, **more}


def _all_of(sha: str, cap: int | None = None) -> list[Any]:
    return [
        ["h", sha],
        ["H", sha],
        ["R", sha, cap],
        ["C", sha, cap],
        ["T", sha, cap],
        ["F", sha, cap],
    ]


def _plain_cases() -> list[dict[str, Any]]:
    cs: list[dict[str, Any]] = []
    blob = _name("blob", BLOB)
    tree_body = _tree_body(blob)
    tree = _name("tree", tree_body)
    commit_body = _commit(tree, [])
    commit = _name("commit", commit_body)
    loose = [
        _obj(*_loose("blob", BLOB)),
        _obj(*_loose("tree", tree_body)),
        _obj(*_loose("commit", commit_body)),
    ]
    b, t, c = blob.hex(), tree.hex(), commit.hex()
    missing = "0" * 40
    cs.append(
        _case(
            "loose objects",
            [*BASE, *loose],
            [
                "n",
                *_all_of(b),
                *_all_of(t),
                *_all_of(c),
                ["h", b.upper()],
                ["h", missing],
                ["h", b[:39]],
                ["h", b + "0"],
                ["h", b[:39] + "g"],
                ["h", b[:38] + "éa"],
                ["m", [b, t.upper(), missing, b.upper(), "xyz", c]],
                ["m", []],
                ["v", b[:4]],
                ["v", c[:7].upper()],
                ["v", b[:3]],
                ["v", b + "0"],
                ["v", b[:5] + "é"],
                ["v", b[:4] + " "],
                ["v", "+" + b[:5]],
                ["v", b],
                ["v", missing],
                ["v", "ffff"],
                ["R", b, 5],
                ["R", b, 6],
                ["R", b, 0],
                ["C", c, 10],
                "c",
                "e",
            ],
        )
    )
    # Loose files that are not objects.
    bad_heads = [
        ("a size with a leading zero", b"blob 06\0hello\n"),
        ("no NUL in 32 bytes", b"blob 6" + b" " * 40 + b"\0hello\n"),
        ("an unknown type", b"blobs 6\0hello\n"),
        ("a body past its size", b"blob 5\0hello\n"),
        ("a body short of its size", b"blob 7\0hello\n"),
        ("a size of 2**64", b"blob 18446744073709551616\0hello\n"),
        ("an empty blob", b"blob 0\0"),
        ("a size sign", b"blob +6\0hello\n"),
        ("a head and NUL of 32 bytes", b"blob " + b"1" + b"0" * 25 + b"\0hello\n"),
        ("a head and NUL of 33 bytes", b"blob " + b"1" + b"0" * 26 + b"\0hello\n"),
    ]
    for label, data in bad_heads:
        cs.append(
            _case(
                f"loose {label}",
                [*BASE, _obj(f"{b[:2]}/{b[2:]}", _deflate(data))],
                [["h", b], ["H", b], ["R", b, None], ["R", b, 5]],
            )
        )
    posing = _name("blob", commit_body).hex()
    cs.append(
        _case(
            "a blob whose content is a commit",
            [*BASE, _obj(*_loose("blob", commit_body))],
            [["R", posing, None], ["C", posing, None], ["F", posing, None]],
        )
    )
    big = b"x" * 100000
    bigname = _name("blob", big).hex()
    cs.append(
        _case(
            "a loose blob of 100,000 bytes, in two blocks",
            [*BASE, _obj(*_loose("blob", big))],
            [
                ["H", bigname],
                ["R", bigname, 99999],
                ["R", bigname, 100000],
                ["R", bigname, None],
            ],
        )
    )
    whole = _loose("blob", BLOB)[1]
    for cut in (0, 1, 2, 10, len(whole) - 4, len(whole) - 1):
        cs.append(
            _case(
                f"a loose file cut to {cut} bytes",
                [*BASE, _obj(f"{b[:2]}/{b[2:]}", whole[:cut])],
                [["h", b], ["H", b], ["R", b, None]],
            )
        )
    cs.append(
        _case(
            "a loose path that is a folder",
            [*BASE, ["d", f"g/objects/{b[:2]}/{b[2:]}"]],
            [["h", b], ["H", b], ["m", [b]], ["v", b[:6]]],
        )
    )
    cs.append(
        _case(
            "an evicted loose file",
            [*BASE, *loose],
            [["h", b], ["H", b], "e", "c", ["R", b, None], "e"],
            st=[["f", f"g/objects/{b[:2]}/{b[2:]}"]],
        )
    )
    # Packs.
    p = _Pack()
    base_name = p.whole("blob", b"base content here\n")
    target1 = b"base content here\nmore\n"
    d1 = p.ofs(
        "blob", p.at(base_name), _delta(b"base content here\n", target1), target1
    )
    target2 = target1 + b"again\n"
    d2 = p.ref("blob", d1, _delta(target1, target2), target2)
    ptree_body = _tree_body(base_name) + b"40000 sub\x00" + d1
    ptree = p.whole("tree", ptree_body)
    pcommit = p.whole("commit", _commit(ptree, [commit, blob], b"packed\n"))
    names = [x.hex() for x in (base_name, d1, d2, ptree, pcommit)]
    cs.append(
        _case(
            "a pack of a blob, an OFS delta, a REF delta on it, a tree and a commit",
            [*BASE, *_packed(p)],
            [
                "n",
                *[q for n in names for q in _all_of(n)],
                ["m", [*names, missing]],
                ["v", names[0][:4]],
                ["R", names[2], len(target2) - 1],
                ["R", names[2], len(target2)],
                ["R", names[2], len(target1)],
                ["H", names[2]],
                "c",
            ],
        )
    )
    # A chain past the work factor: each delta adds a byte.
    p = _Pack()
    prev_at = p.raw(_name("blob", b"a"), _size_varint(3, 1) + _deflate(b"a"))
    text = b"a"
    tops = []
    for _ in range(20):
        nxt = text + b"b"
        name = p.ofs("blob", prev_at, _delta(text, nxt), nxt)
        prev_at, text = p.at(name), nxt
        tops.append(name.hex())
    top = tops[-1]
    cs.append(
        _case(
            "a chain of 20 deltas under caps",
            [*BASE, *_packed(p)],
            [
                ["H", top],
                ["R", top, None],
                ["R", top, 21],
                ["R", top, 20],
                ["R", tops[0], 1],
                ["R", tops[0], 2],
                ["R", tops[3], 5],
                ["R", tops[3], 6],
                ["R", top, 8],
                ["R", top, 9],
            ],
        )
    )
    # Damaged pack entries.
    p = _Pack()
    good = p.whole("blob", BLOB)
    bad_code = _name("blob", b"code 5")
    p.raw(bad_code, _size_varint(5, 6) + _deflate(b"code 5"))
    lost = _name("blob", b"lost base")
    p.ref("blob", b"\x11" * 20, _delta(b"", b"lost base"), b"lost base")
    far = _name("blob", b"far")
    p.raw(far, _size_varint(6, 3) + _ofs(10**6) + _deflate(b"\x00\x03far"))
    wrong = _name("blob", b"wrong source")
    p.ofs("blob", p.at(good), _delta(b"other", b"wrong source"), b"wrong source")
    cut_name = _name("blob", b"cut")
    p.raw(cut_name, _size_varint(3, 3))
    ns = [x.hex() for x in (good, bad_code, lost, far, wrong, cut_name)]
    cs.append(
        _case(
            "a pack of damaged entries",
            [*BASE, *_packed(p)],
            [q for n in ns for q in (["h", n], ["H", n], ["R", n, None])],
        )
    )
    # A large offset, at 2**63 and inside the pack.
    p = _Pack()
    one = p.whole("blob", b"one")
    two = p.whole("blob", b"two")
    cs.append(
        _case(
            "an index offset at 2**63",
            [*BASE, *_packed(p, large={two: 1 << 63})],
            [
                ["h", two.hex()],
                ["H", two.hex()],
                ["R", two.hex(), None],
                ["F", two.hex(), None],
                ["H", one.hex()],
                ["R", one.hex(), None],
                "c",
            ],
        )
    )
    # An evicted pack, then the same object loose.
    p = _Pack()
    p.whole("blob", BLOB)
    cs.append(
        _case(
            "an evicted pack, the object loose too",
            [*BASE, *_packed(p), loose[0]],
            [["h", b], ["H", b], "e", "c", ["R", b, None], "e"],
            st=[["f", "g/objects/pack/p.pack"]],
        )
    )
    cs.append(
        _case(
            "an evicted pack alone",
            [*BASE, *_packed(p)],
            [["H", b], ["h", b], "e", "c"],
            st=[["f", "g/objects/pack/p.pack"]],
        )
    )
    # Prefixes shared across packs and loose files.
    shared = []
    for i in range(4000):
        body = b"%d\n" % i
        shared.append((_name("blob", body), body))
    by4: dict[str, list[tuple[bytes, bytes]]] = {}
    for name, body in shared:
        by4.setdefault(name.hex()[:4], []).append((name, body))
    pair = next(v for k, v in sorted(by4.items()) if len(v) >= 2)
    (n1, b1), (n2, b2) = pair[0], pair[1]
    h1, h2 = n1.hex(), n2.hex()
    pre = h1[:4]
    long1 = next(k for k in range(5, 40) if h1[:k] != h2[:k])
    pa, pb = _Pack(), _Pack()
    pa.whole("blob", b1)
    pb.whole("blob", b2)
    pc = _Pack()
    pc.whole("blob", b1)
    cs += [
        _case(
            "two loose names sharing four digits",
            [*BASE, _obj(*_loose("blob", b1)), _obj(*_loose("blob", b2))],
            [
                ["v", pre],
                ["v", h1[:long1]],
                ["v", h2[:long1]],
                ["m", [h1, h2]],
                ["m", [h2, h1, h1]],
                ["v", h1[: long1 - 1]],
            ],
        ),
        _case(
            "two packs sharing four digits",
            [*BASE, *_packed(pa, "a"), *_packed(pb, "b")],
            [["v", pre], ["v", h1[:long1]], ["v", h2[:long1].upper()], ["m", [h1, h2]]],
        ),
        _case(
            "a pack and a loose file sharing four digits",
            [*BASE, *_packed(pa, "a"), _obj(*_loose("blob", b2))],
            [["v", pre], ["v", h2[:long1]], ["H", h2]],
        ),
        _case(
            "one object in two packs and loose",
            [*BASE, *_packed(pa, "a"), *_packed(pc, "c"), _obj(*_loose("blob", b1))],
            [["v", pre], ["v", h1[:-1]], ["H", h1], ["R", h1, None]],
        ),
        _case(
            "a fanout name not hex, of the width, or sharing the rest",
            [
                *BASE,
                _obj(*_loose("blob", b1)),
                _f(f"g/objects/{h1[:2]}/{h1[2:-1]}z", b""),
                _f(f"g/objects/{h1[:2]}/{h1[2:]}0", b""),
                _f(f"g/objects/{h1[:2]}/{h1[2:-1]}", b""),
            ],
            [["v", pre], ["v", h1[:10]], ["m", [h1, h1[:-1] + "z"]]],
        ),
        _case(
            "a fanout that is a file",
            [*BASE, _f(f"g/objects/{h1[:2]}", b"")],
            [["v", pre], ["m", [h1, h2]], ["h", h1]],
        ),
    ]
    # A pack appearing between questions.
    pn = _Pack()
    late = pn.whole("blob", b"late")
    ln = late.hex()
    cs.append(
        _case(
            "a pack appearing between questions",
            [*BASE, *loose],
            [
                ["h", ln],
                ["v", ln[:6]],
                ["H", ln],
                ["w", "g/objects/pack/n.pack", _sp(bytes(pn.data))],
                ["w", "g/objects/pack/n.idx", _sp(pn.idx())],
                ["v", ln[:6]],
                ["w", "g/objects/pack/m.pack", _sp(bytes(pn.data))],
                ["w", "g/objects/pack/m.idx", _sp(pn.idx())],
                ["H", ln],
                ["h", ln],
                ["m", [ln, b]],
                ["R", ln, None],
            ],
        )
    )
    pn2 = _Pack()
    l2 = pn2.whole("blob", b"later").hex()
    cs.append(
        _case(
            "a pack appearing before a read, a has, a resolve",
            [*BASE],
            [
                ["w", "g/objects/pack/n.pack", _sp(bytes(pn2.data))],
                ["w", "g/objects/pack/n.idx", _sp(pn2.idx())],
                ["R", l2, None],
                ["h", l2],
                ["v", l2[:4]],
            ],
        )
    )
    # Formats.
    wblob = _name("blob", BLOB, True)
    wtree_body = _tree_body(wblob)
    wtree = _name("tree", wtree_body, True)
    wcommit_body = _commit(wtree, [wblob])
    wp = _Pack(wide=True)
    wcommit = wp.whole("commit", wcommit_body)
    wp.whole("tree", wtree_body)
    wd_target = BLOB + b"x"
    wd = wp.ref("blob", wblob, _delta(BLOB, wd_target), wd_target)
    wp2 = _Pack(wide=True)
    wp2.whole("blob", BLOB)
    cs.append(
        _case(
            "sha256 objects loose and packed",
            [
                *BASE,
                _f("g/config", SHA256),
                _obj(*_loose("blob", BLOB, True)),
                *_packed(wp),
                *_packed(wp2, "q"),
            ],
            [
                "n",
                *_all_of(wblob.hex()),
                *_all_of(wtree.hex()),
                *_all_of(wcommit.hex()),
                *_all_of(wd.hex()),
                ["h", b],
                ["v", wblob.hex()[:4]],
                ["v", wblob.hex()[:63]],
                ["v", wblob.hex() + "0"],
                ["m", [wblob.hex(), b]],
            ],
        )
    )
    cs.append(
        _case(
            "an unknown object format",
            [*BASE, _f("g/config", b"[core]\n\trepositoryformatversion = 2\n"), *loose],
            ["n", ["h", b], ["v", b[:4]], ["H", b], ["C", c, None], ["m", [b]]],
        )
    )
    cs.append(
        _case(
            "no objects folder", [["d", "g"]], ["n", ["h", b], ["v", b[:4]], ["H", b]]
        )
    )
    return cs


def _zlib_cases() -> list[dict[str, Any]]:
    cs = []
    blob = _name("blob", BLOB).hex()
    whole = _deflate(b"blob 6\0hello\n")
    damaged = [
        ("a wrong check", whole[:-1] + bytes([whole[-1] ^ 1])),
        ("a block of type 3", b"\x78\x01\x07" + whole[3:]),
        ("a wrong header", b"\x78\x02" + whole[2:]),
        ("a stored length that disagrees", whole[:3] + b"\x0d\x00\x00\x00" + whole[7:]),
        ("bytes after the stream", whole + b"junk"),
    ]
    for label, data in damaged:
        cs.append(
            _case(
                f"loose {label}",
                [*BASE, _obj(f"{blob[:2]}/{blob[2:]}", data)],
                [["h", blob], ["H", blob], ["R", blob, None]],
            )
        )
    p = _Pack()
    base = p.whole("blob", b"0123456789")
    for n in (20, 21):
        target = b"0123456789" + b"y" * (n - 5)
        delta = _delta(b"0123456789", target)
        stream = bytearray(_deflate(delta))
        stream[-1] ^= 1
        name = _name("blob", target)
        head = _size_varint(6, len(delta))
        at = len(p.data)
        p.raw(name, head + _ofs(at - p.at(base)) + bytes(stream))
    names = [n.hex() for n, _ in p.names[1:]]
    cs.append(
        _case(
            "deltas of 20 and 21 bytes, their checks wrong",
            [*BASE, *_packed(p)],
            [q for n in names for q in (["H", n], ["R", n, None])],
        )
    )
    return cs


def _posix_cases() -> list[dict[str, Any]]:
    blob = _name("blob", BLOB)
    b = blob.hex()
    rel, data = _loose("blob", BLOB)
    p = _Pack()
    p.whole("blob", BLOB)
    alt = _Pack()
    other = alt.whole("blob", b"in the alternate")
    o = other.hex()
    return [
        _case(
            "a FIFO at a loose path",
            [*BASE, ["p", f"g/objects/{rel}"]],
            [["h", b], ["H", b], ["R", b, None], ["m", [b]]],
        ),
        _case(
            "an unreadable loose file",
            [*BASE, _obj(rel, data), ["m", f"g/objects/{rel}", 0]],
            [["h", b], ["H", b], ["R", b, None]],
        ),
        _case(
            "an unreadable pack, the object loose",
            [*BASE, *_packed(p), _obj(rel, data), ["m", "g/objects/pack/p.pack", 0]],
            [["H", b], ["R", b, None]],
        ),
        _case(
            "an unlistable fanout",
            [
                *BASE,
                _obj(rel, data),
                _obj(*_loose("blob", b"x")),
                ["m", f"g/objects/{b[:2]}", 0o300],
            ],
            [["h", b], ["v", b[:6]], ["m", [b, b[:-1] + "0"]]],
        ),
        _case(
            "objects in an alternate",
            [
                *BASE,
                _f("g/objects/info/alternates", b"../../x/objects\n"),
                ["d", "x/objects/pack"],
                *_packed(alt, objects="x/objects"),
                _obj(rel, data, objects="x/objects"),
                _obj(*_loose("blob", b"in the alternate"), objects="x/objects"),
            ],
            [
                ["h", b],
                ["h", o],
                ["v", o[:5]],
                ["v", b[:5]],
                ["H", o],
                ["R", b, None],
                ["m", [o, b]],
            ],
        ),
    ]


def _build(root: bytes, tree: list[Any]) -> None:
    modes = []
    for entry in tree:
        kind, rel = entry[0], entry[1]
        path = _abs(root, rel)
        if kind == "d":
            os.makedirs(path, exist_ok=True)
        elif kind == "f":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(_spelled(entry[2]))
        elif kind == "p":
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.mkfifo(path)  # type: ignore[attr-defined,unused-ignore]
        else:
            modes.append((path, entry[2]))
    for path, mode in modes:
        os.chmod(path, mode)


def _content(data: bytes) -> Any:
    if len(data) <= 64:
        return data.hex()
    return {"n": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _answer(reader: gitobjects.ObjectReader, root: bytes, q: Any) -> Any:
    if q == "n":
        return reader.name_length
    if q == "c":
        return reader.complete
    if q == "e":
        return [_rel(root, p) for p in reader.evicted]
    op = q[0]
    if op == "w":
        path = _abs(root, q[1])
        with open(path, "wb") as fh:
            fh.write(_spelled(q[2]))
        return None
    if op == "h":
        return reader.has(q[1])
    if op == "m":
        return sorted(reader.has_many(q[1]))
    if op == "v":
        return reader.resolve(q[1])
    if op == "H":
        found = reader.header(q[1])
        return None if found is None else [found[0], format(found[1], "x")]
    kw = {} if q[2] is None else {"max_size": q[2]}
    if op == "R":
        read = reader.read(q[1], **kw)
        return None if read is None else [read[0], _content(read[1])]
    if op == "C":
        commit = reader.commit(q[1], **kw)
        if commit is None:
            return None
        return {
            "tree": commit.tree,
            "parents": list(commit.parents),
            "author": commit.author.hex(),
            "committer": commit.committer.hex(),
            "message": commit.message.hex(),
        }
    if op == "T":
        entries = reader.tree(q[1], **kw)
        return (
            None
            if entries is None
            else [[e.mode, e.name.hex(), e.sha] for e in entries]
        )
    return reader.fingerprint(q[1], **kw)


def _run(case: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = os.fsencode(os.path.realpath(tmp))
        _build(root, case["t"])
        try:
            answers: list[Any] = []
            with (
                mock.patch.object(
                    gitfile_rule, "_git_is_at_least", lambda r: (2, 54) >= r
                ),
                mock.patch.object(gitfile_rule, "_reads_compat", lambda: True),
                _claims(root, case.get("st", [])),
            ):
                reader = gitobjects.ObjectReader(_abs(root, "g"))
                for q in case["q"]:
                    try:
                        answers.append(_answer(reader, root, q))
                    except (ValueError, OverflowError, KeyError) as exc:
                        answers.append({"raise": type(exc).__name__})
        finally:
            for entry in case["t"]:
                if entry[0] == "m":
                    os.chmod(_abs(root, entry[1]), 0o755)
    return {**case, "a": answers}


def _file(name: str, cases: list[dict[str, Any]]) -> dict[str, bytes]:
    rows = [_run(c) for c in cases]
    data = jsonl([{"fixture": name, "cases": len(rows)}, *rows])
    if leak := host_fact(data):
        raise AssertionError(f"{name} holds a host fact: {leak}")
    return {f"{name}.jsonl": data}


def generate() -> dict[str, bytes]:
    return _file("gitobjects_reader", _plain_cases())


def generate_zlib() -> dict[str, bytes]:
    return _file("gitobjects_reader_zlib", _zlib_cases())


def generate_posix() -> dict[str, bytes]:
    if sys.platform == "win32":
        return {}
    return _file("gitobjects_reader_posix", _posix_cases())
