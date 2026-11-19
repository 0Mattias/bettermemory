"""Fixtures: the byte parsers of `gitobjects` (``gitobjects.jsonl``,
``gitobjects_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitobjects.rs``, unit 1 of the
module's port (native/README.md, "Git objects read from a repository's
files"). Every function pinned here is a function of its arguments alone:
no file is opened, nothing is inflated.

The first line is a header (the count); then one line per case, by
``"f"``, the bytes of every argument (``b``, and ``c`` for a second one)
as hex, or where a byte repeats 32 times or more as a list of pieces
(hex text, or ``[byte hex, count]`` for each run), integers as JSON
numbers, and the answer ``r``. An integer the
reference returns unbounded (a size, a distance, a varint) is its
``format(value, "x")``. Where the reference raises, ``r`` is
``{"raise": class}``.

- ``lh``: `_loose_header(b)`: ``[type, size, start]`` or null.
- ``vi``: `_varint(b, i)`: ``[value, next]`` or null.
- ``eh``: `_entry_header(b, h)`: ``[code, size, base, length]``, the base
  null, ``{"d": distance}`` or ``{"n": name hex}``; or null.
- ``ds``: `_delta_sizes(b)`: ``[source, target, first]`` or null.
- ``ad``: `_apply_delta(b, c)` (``b`` the base, ``c`` the delta): the
  result spelled as the arguments are, or null.
- ``hn``: `_hex_name(b, h)`. ``ha``: `_hex_at(b, i, h)`.
- ``pc``: `_parse_commit(b, h)`: ``[tree, parents, author, committer,
  message]`` (the last three hex) or null.
- ``pt``: `_parse_tree(b, h)`: ``[[mode, name, sha], ...]`` or null.
- ``ca``: `committed_at(b, h)` as decimal text. ``pl``: `parent_lines(b,
  h)`: ``[parents, at]`` or null. ``fp``: `commit_fingerprint(b)`.
- ``uq``: `_unquote_c(b, i)`: ``[text, end]`` or null. ``ae``:
  `_alternate_entries(b)`.
- ``ix``: `_parse_index(b, b"p.pack", h)`: null, or the answers of the
  index to ``q``, each ``["f", raw]`` (`find`), ``["o", position]``
  (`offset`) or ``["s", prefix, limit]`` (`starting`, the prefix as
  text).

``gitobjects_python.jsonl`` holds the loose headers whose size has more
digits than ``int()`` reads (CPython 3.11 and later raise ValueError;
3.10 read them), gated on the Python release. Every input is fixed text
or built here by fixed arithmetic; no line depends on the platform.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable, Iterator
from typing import Any

from bettermemory import gitobjects as g

from ._common import jsonl

Case = dict[str, Any]


def _x(value: int) -> str:
    return format(value, "x")


def _sp(data: bytes) -> Any:
    """Bytes as hex, or where a byte repeats 32 times or more, a list of
    pieces: hex text, or ``[byte hex, count]`` for each such run."""
    pieces: list[Any] = []
    plain = bytearray()
    i = 0
    while i < len(data):
        j = i
        while j < len(data) and data[j] == data[i]:
            j += 1
        if j - i >= 32:
            if plain:
                pieces.append(plain.hex())
                plain = bytearray()
            pieces.append([data[i : i + 1].hex(), j - i])
        else:
            plain += data[i:j]
        i = j
    if not pieces:
        return data.hex()
    if plain:
        pieces.append(plain.hex())
    return pieces


def _raised(call: Callable[[], Any], spell: Callable[[Any], Any]) -> Any:
    try:
        value = call()
    except (ValueError, IndexError, struct.error) as exc:
        return {"raise": type(exc).__name__}
    return spell(value)


def _bytes(seed: str, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}/{counter}".encode()).digest()
        counter += 1
    return out[:n]


def _lh(b: bytes) -> Case:
    return {
        "f": "lh",
        "b": _sp(b),
        "r": _raised(
            lambda: g._loose_header(b),
            lambda v: None if v is None else [v[0], _x(v[1]), v[2]],
        ),
    }


def _loose_cases() -> Iterator[Case]:
    heads = [
        b"",
        b"blob 0\x00",
        b"blob 12\x00hello",
        b"commit 250\x00tree",
        b"tree 1\x00",
        b"tag 9\x00",
        b"tree 0",
        b"blob\x00",
        b"blob \x00",
        b"blob 01\x00",
        b"blob 00\x00",
        b"blob -1\x00",
        b"blob +1\x00",
        b"blob 1 \x00",
        b"blob  1\x00",
        b"blob 1a\x00",
        b"Blob 1\x00",
        b"blobs 1\x00",
        b"ofs_delta 1\x00",
        b"blob 1\x00\x00",
        b"\x00blob 1\x00",
        b"blob \xd9\xa3\x00",
        b"blob 18446744073709551615\x00",
        b"blob 18446744073709551616\x00",
        b"blob 99999999999999999999999999\x00",
        b"blob " + b"7" * 4300 + b"\x00",
        b"blob 1\nx\x00",
    ]
    for head in heads:
        yield _lh(head)


def _vi(b: bytes, i: int) -> Case:
    v = g._varint(b, i)
    return {
        "f": "vi",
        "b": _sp(b),
        "i": i,
        "r": None if v is None else [_x(v[0]), v[1]],
    }


def _varint_cases() -> Iterator[Case]:
    for data in [
        b"",
        b"\x00",
        b"\x7f",
        b"\x80",
        b"\x80\x01",
        b"\xff\x7f",
        b"\xff\xff\xff\xff\x0f",
        b"\xff" * 9 + b"\x01",
        b"\xff" * 9 + b"\x7f",
        b"\xff" * 10 + b"\x01",
        b"\x80" * 40 + b"\x00",
        b"\x80" * 40 + b"\x01",
        b"\xff" * 40,
        b"\xff" * 40 + b"\x00",
    ]:
        for i in (0, 1, len(data), len(data) + 3):
            yield _vi(data, i)


def _eh(b: bytes, h: int) -> Case:
    def spell(v: Any) -> Any:
        if v is None:
            return None
        code, size, base, length = v
        if base is None:
            spelled = None
        elif isinstance(base, int):
            spelled = {"d": _x(base)}
        else:
            spelled = {"n": base.hex()}
        return [code, _x(size), spelled, length]

    return {"f": "eh", "b": _sp(b), "h": h, "r": spell(g._entry_header(b, h))}


def _entry_cases() -> Iterator[Case]:
    name = bytes(range(1, 21))
    bufs = [b""]
    for code in range(8):
        bufs += [
            bytes([code << 4 | 5]),
            bytes([0x80 | code << 4 | 15, 0x7F]),
            bytes([0x80 | code << 4, 0x80]),
            bytes(
                [
                    0x80 | code << 4 | 1,
                    0xFF,
                    0xFF,
                    0xFF,
                    0xFF,
                    0xFF,
                    0xFF,
                    0xFF,
                    0xFF,
                    0x01,
                ]
            ),
        ]
        bufs += [
            bytes([code << 4 | 3]) + b"\x05",
            bytes([code << 4 | 3]) + b"\x80\x00",
            bytes([code << 4 | 3]) + b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x7f",
            bytes([code << 4 | 3]) + b"\x81",
            bytes([code << 4 | 3]) + name,
            bytes([code << 4 | 3]) + name[:19],
        ]
    bufs.append(bytes([0x80 | 6 << 4]) + b"\xff" * 20 + b"\x7f")
    bufs.append(bytes([0x60]) + b"\xff" * 30 + b"\x00")
    for buf in bufs:
        for h in (20, 32, 0):
            if h != 20 and buf and (buf[0] >> 4) & 7 != 7:
                continue
            yield _eh(buf, h)


def _delta(source: int, target: int, ops: bytes) -> bytes:
    def enc(v: int) -> bytes:
        out = bytearray()
        while True:
            c = v & 0x7F
            v >>= 7
            if v:
                out.append(c | 0x80)
            else:
                out.append(c)
                return bytes(out)

    return enc(source) + enc(target) + ops


def _copy(offset: int, size: int, *, full: bool = False) -> bytes:
    op = 0x80
    args = bytearray()
    for bit in range(4):
        part = (offset >> (8 * bit)) & 0xFF
        if part or full:
            op |= 1 << bit
            args.append(part)
    for bit in range(3):
        part = (size >> (8 * bit)) & 0xFF
        if part or full:
            op |= 0x10 << bit
            args.append(part)
    return bytes([op]) + bytes(args)


def _delta_cases() -> Iterator[Case]:
    base = b"The quick brown fox jumps over the lazy dog.\n" * 3
    big = b"a" * 40 + b"b" * 0x10000
    n = len(base)
    pairs: list[tuple[bytes, bytes]] = [
        (base, _delta(n, 9, _copy(4, 5) + b"\x04 fox")),
        (base, _delta(n, 9, _copy(4, 5) + b"\x04 fox")[:-1]),
        (base, _delta(n, 5, _copy(4, 5))),
        (base, _delta(n, 4, _copy(4, 5))),
        (base, _delta(n, 6, _copy(4, 5))),
        (base, _delta(n + 1, 5, _copy(4, 5))),
        (base, _delta(n - 1, 5, _copy(4, 5))),
        (base, _delta(n, 0, b"")),
        (base, _delta(n, 1, b"")),
        (base, _delta(n, 1, b"\x00")),
        (base, _delta(n, 0, b"\x00")),
        (base, _delta(n, n, _copy(0, n))),
        (base, _delta(n, n, _copy(0, n, full=True))),
        (base, _delta(n, 1, _copy(n - 1, 1))),
        (base, _delta(n, 2, _copy(n - 1, 2))),
        (base, _delta(n, 1, _copy(n, 1))),
        (base, _delta(n, 3, b"\x05abc")),
        (base, _delta(n, 5, b"\x05abc")),
        (base, _delta(n, 5, b"\x05abcde\x01")),
        (base, _delta(n, 5, bytes([0x81]))),
        (base, _delta(n, 5, bytes([0x91, 0x01]))),
        (base, _delta(n, 5, bytes([0xFF, 0, 0, 0, 0, 0, 0]))),
        (base, _delta(n, 5, bytes([0xF0, 5, 0, 0]))),
        (b"", _delta(0, 0, b"")),
        (b"", _delta(0, 1, b"\x01z")),
        (b"", _delta(0, 1, _copy(0, 1))),
        (big, _delta(len(big), 0x10000, bytes([0x80]))),
        (big, _delta(len(big), 0x10000, bytes([0x81, 40]))),
        (big, _delta(len(big), 0x10000, bytes([0x81, 41]))),
        (big, _delta(len(big), 0x20000, bytes([0x80, 0x80]))),
        (base, b""),
        (base, b"\x80"),
        (base, bytes([n])),
        (base, bytes([n]) + b"\xff" * 12 + b"\x01"),
        (base, b"\x80" * 30 + bytes([n]) + b"\x00"),
        (base, bytes([n]) + b"\x80" * 30 + b"\x00"),
    ]
    for source, delta in pairs:
        sizes = g._delta_sizes(delta)
        yield {
            "f": "ds",
            "b": _sp(delta),
            "r": None if sizes is None else [_x(sizes[0]), _x(sizes[1]), sizes[2]],
        }
        out = g._apply_delta(source, delta)
        yield {
            "f": "ad",
            "b": _sp(source),
            "c": _sp(delta),
            "r": None if out is None else _sp(out),
        }


HEX40 = "0123456789abcdef0123456789ABCDEF01234567"
T = b"tree " + b"a" * 40 + b"\n"
P1 = b"parent " + b"b" * 40 + b"\n"
P2 = b"parent " + b"C" * 40 + b"\n"
AU = b"author A U Thor <a@example.com> 1700000000 +0100\n"
CO = b"committer C O Mitter <c@example.com> 1700000123 -0200\n"


def _commit_bodies() -> list[bytes]:
    bodies = [
        b"",
        T,
        T + AU + CO + b"\nmessage\n",
        T + P1 + AU + CO + b"\nmsg",
        T + P1 + P2 + AU + CO + b"\n",
        b"tree " + HEX40.encode() + b"\n" + AU + CO + b"\nx",
        b"tree " + b"a" * 39 + b"\n" + AU + CO,
        b"tree " + b"a" * 41 + b"\n" + AU + CO,
        b"tree " + b"g" * 40 + b"\n" + AU + CO,
        b"tree  " + b"a" * 40 + b"\n",
        b"Tree " + b"a" * 40 + b"\n",
        b"tree " + b"a" * 40,
        b"tree " + b"a" * 40 + b"\r\n" + AU + CO,
        T + b"parent " + b"b" * 39 + b"\n" + AU + CO,
        T + b"parent " + b"z" * 40 + b"\n" + AU + CO,
        T + P1,
        T + P1[:-1],
        T + AU + P1 + CO,
        T + AU + AU + CO + CO + b"\n\nm",
        T + CO + AU + b"\n",
        T + b"author \n" + b"committer \n",
        T + b"author\n" + b"committer\n",
        T + AU + b"\n" + CO,
        T + b"\n" + AU + CO,
        T + AU + CO.replace(b"1700000123", b"0001700000123"),
        T + AU + CO.replace(b"1700000123", b"-5"),
        T + AU + CO.replace(b"1700000123", b"-"),
        T + AU + CO.replace(b"1700000123", b"-0"),
        T + AU + CO.replace(b"1700000123", b"18446744073709551615"),
        T + AU + CO.replace(b"1700000123", b"18446744073709551616"),
        T + AU + CO.replace(b"1700000123", b"9" * 21),
        T + AU + CO.replace(b"1700000123", b"0" * 30 + b"42"),
        T + AU + CO.replace(b"1700000123", b"-18446744073709551615"),
        T + AU + CO.replace(b"1700000123", b"-18446744073709551616"),
        T + AU + CO.replace(b"> 1700000123", b">\t \r 77x"),
        T + AU + CO.replace(b"> 1700000123", b">  "),
        T + AU + CO.replace(b"> 1700000123", b"> +5"),
        T + AU + CO.replace(b"> 1700000123", b"> 5 > 6"),
        T + AU + b"committer no mark 5\n",
        T + AU + CO[:-1],
        T + AU + CO.replace(b"> 1700000123", b">"),
        T + AU + CO.replace(b"> 1700000123", b"> \xd9\xa3"),
        T + b"authorX\n" + CO,
        T + AU + b"committerX >7\n",
        T + AU,
        T + b"author no newline",
    ]
    return bodies


def _commit_cases() -> Iterator[Case]:
    for body in _commit_bodies():
        for h in (20, 32, 0):
            commit = g._parse_commit(body, h)
            yield {
                "f": "pc",
                "b": _sp(body),
                "h": h,
                "r": None
                if commit is None
                else [
                    commit.tree,
                    list(commit.parents),
                    commit.author.hex(),
                    commit.committer.hex(),
                    commit.message.hex(),
                ],
            }
        for hexsz in (40, 64, 39, 0, 200):
            yield {
                "f": "ca",
                "b": _sp(body),
                "h": hexsz,
                "r": str(g.committed_at(body, hexsz)),
            }
            lines = g.parent_lines(body, hexsz)
            yield {
                "f": "pl",
                "b": _sp(body),
                "h": hexsz,
                "r": None if lines is None else [list(lines[0]), lines[1]],
            }
        yield {"f": "fp", "b": _sp(body), "r": g.commit_fingerprint(body)}
    body256 = b"tree " + b"d" * 64 + b"\n" + b"parent " + b"E" * 64 + b"\n" + AU + CO
    for hexsz in (64, 40):
        yield {
            "f": "ca",
            "b": _sp(body256),
            "h": hexsz,
            "r": str(g.committed_at(body256, hexsz)),
        }
    for body, at, hexsz in [
        (b"abcdef", 0, 6),
        (b"abcdef", 0, 7),
        (b"abcdeg", 0, 6),
        (b"abc", 3, 0),
        (b"abc", 4, 0),
        (b"abc", 9, 2),
        (b"xxABcd", 2, 4),
        (b"", 0, 0),
    ]:
        yield {
            "f": "ha",
            "b": _sp(body),
            "i": at,
            "h": hexsz,
            "r": g._hex_at(body, at, hexsz),
        }
    for value, h in [
        (b"a" * 40, 20),
        (HEX40.encode(), 20),
        (b"a" * 39, 20),
        (b"a" * 41, 20),
        (b"g" * 40, 20),
        (b"", 0),
        (b"a" * 64, 32),
        (b"a" * 40 + b"\n", 20),
        (b"a" * 39 + b" ", 20),
    ]:
        yield {"f": "hn", "b": _sp(value), "h": h, "r": g._hex_name(value, h)}


def _tree_cases() -> Iterator[Case]:
    sha = bytes(range(20))
    bodies = [
        b"",
        b"100644 a.txt\x00" + sha,
        b"40000 dir\x00" + sha + b"100755 run me\x00" + sha,
        b"120000 link\x00" + sha + b"160000 sub\x00" + sha,
        b"100644 a\x00" + sha[:19],
        b"100644 \x00" + sha,
        b"100644a\x00" + sha,
        b" a\x00" + sha,
        b"1006448 a\x00" + sha,
        b"777777 a\x00" + sha,
        b"100648 a\x00" + sha,
        b"0 a\x00" + sha,
        b"100644 a b\x00" + sha,
        b"100644 a\x00\x00" + sha,
        b"100644 a\x00" + sha + b"x",
        b"100644 a\x00" + sha + b"100644",
        b"100644 \xff\xfe\x00" + sha,
        b"-1 a\x00" + sha,
        b"100644 a" + sha,
    ]
    for body in bodies:
        for h in (20, 32, 0):
            entries = g._parse_tree(body, h)
            yield {
                "f": "pt",
                "b": _sp(body),
                "h": h,
                "r": None
                if entries is None
                else [[e.mode, e.name.hex(), e.sha] for e in entries],
            }


def _quote_cases() -> Iterator[Case]:
    texts = [
        (b'"abc"', 0),
        (b'"a\\tb\\n\\\\\\"q"rest', 0),
        (b'"\\a\\b\\f\\v\\r"', 0),
        (b'"\\101\\377\\000"', 0),
        (b'"\\400"', 0),
        (b'"\\18"', 0),
        (b'"\\1"', 0),
        (b'"\\x41"', 0),
        (b'"\\', 0),
        (b'"open', 0),
        (b'x"in"', 1),
        (b'"a"', 3),
        (b'"a"', 9),
        (b'""', 0),
        (b'"\\777"', 0),
    ]
    for text, start in texts:
        found = g._unquote_c(text, start)
        yield {
            "f": "uq",
            "b": _sp(text),
            "i": start,
            "r": None if found is None else [found[0].hex(), found[1]],
        }
    files = [
        b"",
        b"/a/objects\n../b/objects\n",
        b"#comment\n/a\n",
        b'"/q u\\toted"\n/b',
        b'"/q"x/b\n',
        b'"/bad\\q"\n/b\n',
        b'"unclosed\n/b\n',
        b"\n\n/a\n\n",
        b'""\n/a',
        b"/a\r\n/b",
        b"#\n",
        b'"#x"\n',
        b'"a"',
        b'"a"\n',
    ]
    for text in files:
        yield {
            "f": "ae",
            "b": _sp(text),
            "r": [e.hex() for e in g._alternate_entries(text)],
        }


def _index(
    names: list[bytes],
    offsets: list[int],
    *,
    hash_len: int = 20,
    large: list[int] | None = None,
    tail: bytes = b"",
    fanout: list[int] | None = None,
    magic: bytes = b"\xfftOc",
    version: bytes = b"\x00\x00\x00\x02",
) -> bytes:
    if fanout is None:
        fanout = [sum(1 for n in names if n[0] <= i) for i in range(256)]
    out = magic + version + struct.pack(">256I", *fanout)
    out += b"".join(names)
    out += b"\x00\x00\x00\x00" * len(names)
    out += b"".join(struct.pack(">I", o) for o in offsets)
    out += b"".join(struct.pack(">Q", v) for v in large or [])
    out += b"\x11" * (2 * hash_len)
    return out + tail


def _names(count: int, hash_len: int = 20) -> list[bytes]:
    return sorted(_bytes(f"name{i}", hash_len) for i in range(count))


def _ask(index: Any, q: list[Any]) -> Any:
    def call() -> Any:
        if q[0] == "f":
            return index.find(bytes.fromhex(q[1]))
        if q[0] == "o":
            return index.offset(q[1])
        return [n.hex() for n in index.starting(q[1], limit=q[2])]

    return _raised(call, lambda v: v)


def _ix(data: bytes, h: int, queries: list[list[Any]]) -> Case:
    index = g._parse_index(data, b"p.pack", h)
    answers: list[Any] = []
    if index is not None:
        for q in queries:
            answers.append(_ask(index, q))
    return {
        "f": "ix",
        "b": _sp(data),
        "h": h,
        "q": queries,
        "r": None if index is None else answers,
    }


def _index_cases() -> Iterator[Case]:
    names = _names(6)
    offsets = [12, 400, 0x7FFFFFFF, 0x80000000, 0x80000001, 0x80000005]
    large = [1 << 40, (1 << 64) - 1]
    good = _index(names, offsets, large=large)
    queries: list[list[Any]] = []
    for i, n in enumerate(names):
        queries += [["f", n.hex()], ["o", i]]
    hx = [n.hex() for n in names]
    queries += [
        ["f", "00" * 20],
        ["f", "ff" * 20],
        ["f", hx[0][:-2]],
        ["f", hx[0] + "00"],
        ["f", ""],
        ["o", 6],
        ["o", 7],
        ["o", 9],
        ["o", 10],
        ["o", 12],
        ["o", 400],
    ]
    for prefix in [hx[0][:2], hx[0][:3], hx[0][:4], hx[1][:7], hx[2], hx[3][:39]]:
        for limit in (2, 1, 0, -1, 10):
            queries.append(["s", prefix, limit])
    for prefix in [
        "",
        "a",
        "0",
        hx[0][0],
        hx[0][:4].upper(),
        "g1",
        "1g",
        "abg",
        " ab cd",
        "ab cd e",
        "ab ",
        " a",
        "a ",
        "+a",
        "-a",
        "+",
        "0x",
        "\tab",
        "ab\x0bcd",
        "a_b",
        "\x1cab",
        "  ",
        "ab  cd",
        "abc d",
    ]:
        queries.append(["s", prefix, 2])
    yield _ix(good, 20, queries)
    same = [names[0], names[0][:19] + bytes([names[0][19] ^ 1])]
    same.sort()
    yield _ix(
        _index(same, [5, 6]),
        20,
        [["f", same[0].hex()], ["f", same[1].hex()], ["s", same[0].hex()[:4], 2]],
    )
    empty = _index([], [])
    yield _ix(empty, 20, [["f", "00" * 20], ["s", "ab", 2], ["o", 0], ["o", 1]])
    names32 = _names(3, 32)
    yield _ix(
        _index(names32, [1, 2, 3], hash_len=32),
        32,
        [["f", names32[1].hex()], ["o", 2], ["s", names32[2].hex()[:5], 2]],
    )
    zero = [b"\x00" * 20, b"\x00" * 19 + b"\x01", b"\xff" * 20]
    yield _ix(
        _index(zero, [1, 2, 3]),
        20,
        [
            ["f", "00" * 20],
            ["f", "ff" * 20],
            ["s", "0000", 5],
            ["s", "ff", 5],
            ["s", "00", 1],
        ],
    )
    bad = [
        good[:-1],
        good + b"\x00" * 8,
        _index(names, offsets, large=large, tail=b"\x00" * 7),
        _index(names, offsets, large=[*large, 1, 2, 3]),
        _index(names, offsets, large=[*large, 1, 2, 3, 4]),
        _index(names, offsets, magic=b"\xfftOC"),
        _index(names, offsets, version=b"\x00\x00\x00\x01"),
        _index(names, offsets, fanout=[0] * 255 + [7]),
        _index(names, offsets, fanout=[1, 0] + [6] * 254),
        _index(names, offsets, fanout=[0] * 128 + [6] * 127 + [5]),
        good[:1032],
        good[:1031],
        b"",
        _index([], [], large=[1]),
        _index([names[0]], [0x80000000], large=[9]),
    ]
    for data in bad:
        yield _ix(data, 20, [["o", 0], ["f", names[0].hex()]])
    one = _index([names[0]], [0x80000000])
    yield _ix(one, 20, [["o", 0], ["f", names[0].hex()], ["o", 1]])
    yield _ix(good, 32, [["o", 0]])
    yield _ix(_index([], [], hash_len=0), 0, [["o", 0], ["s", "00", 1]])


def generate() -> dict[str, bytes]:
    cases = [
        *_loose_cases(),
        *_varint_cases(),
        *_entry_cases(),
        *_delta_cases(),
        *_commit_cases(),
        *_tree_cases(),
        *_quote_cases(),
        *_index_cases(),
    ]
    header = {"fixture": "gitobjects", "cases": len(cases)}
    return {"gitobjects.jsonl": jsonl([header, *cases])}


def generate_python() -> dict[str, bytes]:
    cases = [
        _lh(b"blob " + b"7" * 4301 + b"\x00"),
        _lh(b"commit " + b"1" * 5000 + b"\x00"),
        _lh(b"blob 0" + b"7" * 4301 + b"\x00"),
    ]
    header = {"fixture": "gitobjects_python", "cases": len(cases)}
    return {"gitobjects_python.jsonl": jsonl([header, *cases])}
