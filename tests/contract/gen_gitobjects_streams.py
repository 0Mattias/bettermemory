"""Fixtures: the zlib streams and the delta chain of `gitobjects`
(``gitobjects_streams.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/gitobjects/stream.rs``, unit 2 of
the module's port (native/README.md, "Git objects read from a
repository's files"): `_Stream.read`, `_Stream.read_to_end`, `_chain`,
`_inflate_at`, `_listed_in` and `_take`, over a file handle that is the
case's.

No file is opened. A handle is bytes ``d`` (or ``p``) held here, read
from its position; a read is logged and answers at most what was asked.
``s`` is the handle's script, one entry per read in turn, over and over:
a positive entry caps that read, 0 answers None (a non-blocking read
with nothing), -1 raises OSError and -2 PermissionError. A handle of a
walk also takes ``org`` (the file offset its bytes start at, default 0)
and ``ks`` (the number of the seek that raises OSError). A seek's
argument of 2**31 or more is given to a real `FileIO` first, so an
offset `FileIO.seek` refuses raises as it does there, and is not logged.

Bytes are spelled as `gen_gitobjects` spells them, with one more piece:
``[hex, count]`` repeats a unit of any length. No stream comes from
zlib's compressor, whose output is the zlib build's: every stream is
written here bit by bit, so every input is the same wherever the file is
made. What the file pins of each call is not: the handle's reads and
position, and when a `zlib.error` is raised, follow from where the
linked zlib's `inflate` stops and how much input it has consumed there.
macOS's libz 1.2.12, which made the file, and the native build's
`zlib-rs` agree on all of it; no other build has been compared, so the
file is gated on the zlib (`generate.ZLIB_GATED`). Nothing pinned is the
text of a `zlib.error`, which is the zlib build's too (macOS's libz says
"invalid literal/length/distance code" for what the native build's
inflate calls an invalid literal/length code or an invalid distance
code); its code is pinned.

The header holds the count, ``bases`` (valid streams, each ``[stream,
size inflated]``), ``packs`` (each ``{"p": bytes, "h": name length,
"ix": [[name, offset], ...]}``, the index built from ``ix`` in order:
the fanout, the names, zero CRCs, each offset in the 31-bit table or,
from 2**31, in the 64-bit table in order of appearance; ``["raw", n]``
writes ``n`` into the 31-bit table as it is) and ``swept`` (the packs
the chain sweep draws from, by number). Then one line per case, by
``f``:

- ``st``: `_Stream(handle, c)` and its calls ``o`` in turn, each
  ``["r", n]`` (`read`) or ``["e", n]`` (`read_to_end`); ``r`` holds for
  each ``[answer, reads so far, position]``. An answer is ``["b", hex]``
  (bytes), ``["B", length, sha256]`` (bytes past 48), ``["n"]`` (None),
  ``["z", code]`` (`zlib.error`) or ``["x", class]`` (another raise).
- ``ch``: `_chain(handle, index, at, cap=cap)` over pack ``pk`` of the
  header (or ``p``, ``h``, ``ix`` of the case): ``r`` is null,
  ``[code, [[start, size, target], ...]]`` (sizes as ``format(v, "x")``),
  past 8 links ``{"links": n, "sha": sha256}`` of `_links_text`, or a
  raise as above; ``log`` the handle's calls (``s<offset>``,
  ``r<count>``), or their count and sha256 past 400 characters.
- ``ia``: `_inflate_at(handle, _Link(start, size, None))`, ``l`` its
  ``[start, size]``; ``r`` an answer as ``st``'s, and ``log``.
- ``li``: `_listed_in(packs, raw)` over the indexes of the header's
  packs ``xs``: ``[[place in xs, offset], ...]`` or a raise.
- ``tk``: `_take(packs, want, found)` over the same: ``want`` as
  ``[[raw, sha], ...]`` and ``found`` afterwards (sorted), and ``x``
  where it raised.
- ``sw``: a sweep: ``n`` cases made from ``seed`` by ``rule`` (`_sweep`
  below, which the native test restates), pinned as the sha256 of their
  answers, one line each.
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib
from collections.abc import Callable, Iterator
from typing import Any

from bettermemory import gitobjects as g

from ._common import jsonl

Case = dict[str, Any]
_M64 = (1 << 64) - 1


# -- bytes, spelled -------------------------------------------------------


def _sp(data: bytes) -> Any:
    """`gen_gitobjects._sp`: hex, or pieces where a byte repeats 32 times."""
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


def _periodic(data: bytes, start: int, period: int) -> Any:
    """`data` spelled with its run of `period`-byte units from `start`."""
    unit = data[start : start + period]
    count = 0
    while data[start + count * period : start + (count + 1) * period] == unit:
        count += 1
    rest = data[start + count * period :]
    assert count > 4, count
    return [data[:start].hex(), [unit.hex(), count], rest.hex()]


def _unspell(spelled: Any) -> bytes:
    if isinstance(spelled, str):
        return bytes.fromhex(spelled)
    out = bytearray()
    for piece in spelled:
        if isinstance(piece, str):
            out += bytes.fromhex(piece)
        else:
            out += bytes.fromhex(piece[0]) * piece[1]
    return bytes(out)


def _bytes(seed: str, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}/{counter}".encode()).digest()
        counter += 1
    return out[:n]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- the random source of the sweeps (splitmix64) ---------------------------


class _Rng:
    def __init__(self, seed: int) -> None:
        self.state = seed & _M64

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _M64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        return self.next() % n

    def pick(self, seq: Any) -> Any:
        return seq[self.below(len(seq))]


# -- deflate, written bit by bit ---------------------------------------------

_LBASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59]
_LBASE += [67, 83, 99, 115, 131, 163, 195, 227, 258]
_LEXT = [0] * 8 + [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4 + [5] * 4 + [0]
_DBASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513]
_DBASE += [769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577]
_DEXT = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10]
_DEXT += [11, 11, 12, 12, 13, 13]
_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]
Codes = dict[int, tuple[int, int]]
Token = tuple[int, ...]


def _canon(lens: list[int]) -> Codes:
    """The canonical Huffman codes of a list of code lengths."""
    count = [0] * 16
    for length in lens:
        count[length] += 1
    count[0] = 0
    code = 0
    start = [0] * 16
    for b in range(1, 16):
        code = (code + count[b - 1]) << 1
        start[b] = code
    codes: Codes = {}
    for sym, length in enumerate(lens):
        if length:
            codes[sym] = (start[length], length)
            start[length] += 1
    return codes


_FIXED_LIT = _canon([8] * 144 + [9] * 112 + [7] * 24 + [8] * 8)
_FIXED_DIST = _canon([5] * 32)


def _length_symbol(length: int) -> tuple[int, int, int]:
    for i in range(28, -1, -1):
        if length >= _LBASE[i]:
            return 257 + i, length - _LBASE[i], _LEXT[i]
    raise AssertionError(length)


def _dist_symbol(dist: int) -> tuple[int, int, int]:
    for i in range(29, -1, -1):
        if dist >= _DBASE[i]:
            return i, dist - _DBASE[i], _DEXT[i]
    raise AssertionError(dist)


class _Bits:
    """A deflate bit stream: fields low bit first, codes high bit first."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def bits(self, value: int, count: int) -> _Bits:
        self.acc |= value << self.n
        self.n += count
        while self.n >= 8:
            self.out.append(self.acc & 0xFF)
            self.acc >>= 8
            self.n -= 8
        return self

    def code(self, pair: tuple[int, int]) -> _Bits:
        code, length = pair
        for i in range(length - 1, -1, -1):
            self.bits((code >> i) & 1, 1)
        return self

    def align(self) -> _Bits:
        if self.n:
            self.out.append(self.acc & 0xFF)
            self.acc = 0
            self.n = 0
        return self

    def raw(self, data: bytes) -> _Bits:
        assert self.n == 0
        self.out += data
        return self

    def block(self, final: int, kind: int) -> _Bits:
        return self.bits(final, 1).bits(kind, 2)

    def stored(self, final: int, data: bytes, nlen: int | None = None) -> _Bits:
        self.block(final, 0).align()
        n = len(data)
        other = n ^ 0xFFFF if nlen is None else nlen
        return self.raw(n.to_bytes(2, "little") + other.to_bytes(2, "little") + data)

    def tokens(self, toks: list[Token], lit: Codes, dist: Codes) -> _Bits:
        """Literals ``(0, byte)`` and matches ``(1, length, distance)``."""
        for tok in toks:
            if tok[0] == 0:
                self.code(lit[tok[1]])
            else:
                sym, extra, nbits = _length_symbol(tok[1])
                self.code(lit[sym]).bits(extra, nbits)
                dsym, dextra, dbits = _dist_symbol(tok[2])
                self.code(dist[dsym]).bits(dextra, dbits)
        return self

    def fixed(self, final: int, toks: list[Token]) -> _Bits:
        self.block(final, 1).tokens(toks, _FIXED_LIT, _FIXED_DIST)
        return self.code(_FIXED_LIT[256])

    def tables(
        self,
        ll: list[int],
        dl: list[int],
        cll: list[int],
        seq: list[tuple[int, int, int]] | None = None,
        ncode: int = 19,
    ) -> _Bits:
        """A dynamic block's header: the counts, the code length code
        `cll` (19 lengths by symbol) and the lengths of `ll` and `dl`,
        each sent as itself or as `seq` spells them (symbol, extra bits,
        their count)."""
        self.bits(len(ll) - 257, 5).bits(len(dl) - 1, 5).bits(ncode - 4, 4)
        for i in range(ncode):
            self.bits(cll[_ORDER[i]], 3)
        codes = _canon(cll)
        for sym, extra, nbits in seq or [(v, 0, 0) for v in ll + dl]:
            self.code(codes[sym]).bits(extra, nbits)
        return self


def _zs(
    body: Callable[[_Bits], Any],
    plain: bytes,
    *,
    header: bytes = b"\x78\x9c",
    check: bytes | None = None,
) -> bytes:
    """A zlib stream: the header, the blocks `body` writes, the Adler-32
    of `plain` (or `check`)."""
    w = _Bits()
    body(w)
    w.align()
    tail = zlib.adler32(plain).to_bytes(4, "big") if check is None else check
    return header + bytes(w.out) + tail


def _plain(toks: list[Token], before: bytes = b"") -> bytes:
    out = bytearray(before)
    for tok in toks:
        if tok[0] == 0:
            out.append(tok[1])
        else:
            for _ in range(tok[1]):
                out.append(out[-tok[2]])
    return bytes(out[len(before) :])


def _lits(data: bytes) -> list[Token]:
    return [(0, b) for b in data]


def _stored(data: bytes) -> bytes:
    return _zs(lambda w: w.stored(1, data), data, header=b"\x78\x01")


def _fixed(data: bytes) -> bytes:
    return _zs(lambda w: w.fixed(1, _lits(data)), data)


def _header(cmf: int, flags: int = 0) -> bytes:
    """A two-byte zlib header whose check holds."""
    flg = flags & 0xE0
    flg += (31 - (cmf * 256 + flg) % 31) % 31
    assert flg < 256 and (cmf * 256 + flg) % 31 == 0
    return bytes([cmf, flg])


# Code length codes: A sends lengths 0 to 15 as themselves; B has the
# three repeat codes and lengths 0 to 4.
_CLL_A = [4] * 16 + [0, 0, 0]
_CLL_B = [3, 3, 3, 3, 3] + [0] * 11 + [3, 3, 3]


def _lens(pairs: dict[int, int], n: int) -> list[int]:
    out = [0] * n
    for sym, length in pairs.items():
        out[sym] = length
    return out


# -- random valid streams (the bases) ---------------------------------------


def _random_lengths(rng: _Rng, k: int, maxlen: int) -> list[int]:
    """`k` code lengths of a complete code, none past `maxlen`."""
    leaves = [1, 1]
    deep = rng.below(10) < 3
    while len(leaves) < k:
        cands = [i for i, length in enumerate(leaves) if length < maxlen]
        if deep and rng.below(10) < 8:
            top = max(leaves[i] for i in cands)
            cands = [i for i in cands if leaves[i] == top]
        length = leaves.pop(rng.pick(cands))
        leaves += [length + 1, length + 1]
    for i in range(len(leaves) - 1, 0, -1):
        j = rng.below(i + 1)
        leaves[i], leaves[j] = leaves[j], leaves[i]
    return leaves


def _random_tokens(rng: _Rng, produced: bytearray, n: int) -> list[Token]:
    toks: list[Token] = []
    alphabet = rng.pick([256, 4, 26, 2])
    for _ in range(n):
        if produced and rng.below(100) < 45:
            limit = min(len(produced), 32768)
            pick = rng.below(10)
            dist = (
                1 + rng.below(min(limit, 8))
                if pick < 3
                else limit
                if pick == 3
                else 1 + rng.below(limit)
            )
            pick = rng.below(10)
            length = (
                3 + rng.below(10)
                if pick < 5
                else 258
                if pick == 5
                else 3 + rng.below(256)
            )
            toks.append((1, length, dist))
            for _ in range(length):
                produced.append(produced[-dist])
        else:
            b = rng.below(alphabet)
            toks.append((0, b))
            produced.append(b)
    return toks


def _random_dynamic(rng: _Rng, w: _Bits, toks: list[Token]) -> None:
    used = {256}
    dused: set[int] = set()
    for tok in toks:
        if tok[0] == 0:
            used.add(tok[1])
        else:
            used.add(_length_symbol(tok[1])[0])
            dused.add(_dist_symbol(tok[2])[0])
    for _ in range(rng.pick([0, 0, 3, 40])):
        used.add(rng.below(286))
    ll = [0] * 286
    syms = sorted(used)
    if len(syms) == 1:
        ll[256] = 1
        if rng.below(2):
            ll[rng.below(256)] = 1
    else:
        for sym, length in zip(syms, _random_lengths(rng, len(syms), 15), strict=True):
            ll[sym] = length
    nlen = max(257, max(i for i, v in enumerate(ll) if v) + 1)
    if rng.below(10) < 3:
        nlen += rng.below(286 - nlen + 1)
    dl = [0] * 30
    dsyms = sorted(dused)
    pick = rng.below(10)
    if not dsyms:
        if 4 <= pick < 7:
            dl[rng.below(30)] = 1
        elif pick >= 7:
            a = rng.below(30)
            dl[a] = dl[(a + 1 + rng.below(29)) % 30] = 1
    elif len(dsyms) == 1:
        dl[dsyms[0]] = 1
        if pick >= 5:
            dl[(dsyms[0] + 1 + rng.below(29)) % 30] = 1
    else:
        for _ in range(rng.pick([0, 0, 2, 10])):
            dused.add(rng.below(30))
        dsyms = sorted(dused)
        for sym, length in zip(
            dsyms, _random_lengths(rng, len(dsyms), 15), strict=True
        ):
            dl[sym] = length
    ndist = max(1, max([i for i, v in enumerate(dl) if v], default=0) + 1)
    if rng.below(10) < 3:
        ndist += rng.below(30 - ndist + 1)
    lens = ll[:nlen] + dl[:ndist]
    seq: list[tuple[int, int, int]] = []
    i = 0
    plain_only = rng.below(5) == 0
    while i < len(lens):
        v = lens[i]
        run = 1
        while i + run < len(lens) and lens[i + run] == v:
            run += 1
        take = 1
        if plain_only or rng.below(10) == 0:
            seq.append((v, 0, 0))
        elif v == 0 and run >= 11:
            take = (
                min(run, 138)
                if rng.below(10) < 7
                else 11 + rng.below(min(run, 138) - 10)
            )
            seq.append((18, take - 11, 7))
        elif v == 0 and run >= 3:
            take = (
                min(run, 10) if rng.below(10) < 7 else 3 + rng.below(min(run, 10) - 2)
            )
            seq.append((17, take - 3, 3))
        elif i > 0 and lens[i - 1] == v and run >= 3:
            take = min(run, 6)
            seq.append((16, take - 3, 2))
        else:
            seq.append((v, 0, 0))
        i += take
    clused = sorted({c[0] for c in seq})
    cll = [0] * 19
    if len(clused) == 1:
        cll[clused[0]] = cll[(clused[0] + 1 + rng.below(18)) % 19] = 1
    else:
        for sym, length in zip(
            clused, _random_lengths(rng, len(clused), 7), strict=True
        ):
            cll[sym] = length
    ncode = max([4] + [idx + 1 for idx in range(19) if cll[_ORDER[idx]]])
    if rng.below(5) == 0:
        ncode += rng.below(19 - ncode + 1)
    w.tables(ll[:nlen], dl[:ndist], cll, seq, ncode)
    w.tokens(toks, _canon(ll[:nlen]), _canon(dl[:ndist]))
    w.code(_canon(ll[:nlen])[256])


def _random_stream(rng: _Rng) -> tuple[bytes, bytes]:
    """A valid zlib stream of one to six blocks of every kind, and what
    it inflates to."""
    w = _Bits()
    produced = bytearray()
    blocks = rng.pick([1, 1, 2, 3, 6])
    for b in range(blocks):
        final = int(b == blocks - 1)
        kind = rng.pick([0, 1, 1, 2, 2, 2])
        if kind == 0:
            n = rng.pick([0, 0, 1, 5, 40, 150])
            data = bytes(rng.below(256) for _ in range(n))
            w.stored(final, data)
            produced += data
            continue
        toks = _random_tokens(rng, produced, rng.pick([0, 1, 3, 20, 20, 60, 150]))
        if kind == 1:
            w.fixed(final, toks)
        else:
            w.block(final, 2)
            _random_dynamic(rng, w, toks)
    w.align()
    header = _header(rng.pick([0x78, 0x78, 0x78, 0x08, 0x38]), rng.below(4) << 6)
    return header + bytes(w.out) + zlib.adler32(bytes(produced)).to_bytes(
        4, "big"
    ), bytes(produced)


def _run_lengths(first: int, units: int) -> tuple[bytes, int]:
    """One literal, then `units` times eight matches of 258 at distance
    1, in a fixed block: eight matches are 104 bits, so the stream
    repeats 13 bytes from its fifth byte on. Built from a stream of six
    units, whose bytes after the fifth whole repeat end every such
    stream."""

    def direct(count: int) -> bytes:
        toks: list[Token] = [(0, first)] + [(1, 258, 1)] * (8 * count)
        return _zs(lambda w: w.fixed(1, toks), b"")[:-4]

    short = direct(6)
    unit = short[4:17]
    assert short[4:69] == unit * 5
    size = 1 + 258 * 8 * units
    stream = short[:4] + unit * (units - 1) + short[69:]
    assert short[:4] + unit * 6 + short[69:] == direct(7)
    return stream + zlib.adler32(bytes([first]) * size).to_bytes(4, "big"), size


def _dense(literals: int, matches: int) -> tuple[bytes, int]:
    """`literals` bytes ``a``, then `matches` matches of 258 at distance
    1, in a dynamic block whose codes for symbol 285 and for the one
    distance are a single 0 bit each: two bits a match, so 16 MiB out is
    16 KB in. The end of block and the check are not sent: the stream is
    cut where its last match ends, at the end of a byte, so that no pad
    bit reads as a further match."""
    ll = _lens({0x61: 2, 256: 2, 285: 1}, 286)
    lit, dist = _canon(ll), _canon([1])
    assert lit[285] == dist[0] == (0, 1)

    def body(w: _Bits) -> None:
        w.block(1, 2).tables(ll, [1], _CLL_A)
        for _ in range(literals):
            w.code(lit[0x61])
        w.bits(0, 2 * matches)
        assert w.n == 0, w.n

    return _zs(body, b"", check=b""), literals + 258 * matches


def _bases() -> list[tuple[bytes, int]]:
    out: list[tuple[bytes, int]] = []
    seed = 0
    while len(out) < 60:
        stream, plain = _random_stream(_Rng(7000 + seed))
        seed += 1
        if len(stream) <= 420:
            assert zlib.decompress(stream) == plain
            out.append((stream, len(plain)))
    out.append((_stored(b""), 0))
    out.append((_fixed(b""), 0))
    out.append((_stored(b"0123456789abcdefghij" * 3), 60))
    out.append(_run_lengths(0x61, 38))
    return out


# -- the handles -------------------------------------------------------------


class _File:
    """The handle a case reads: bytes at `origin`, a script of reads."""

    def __init__(
        self,
        data: bytes,
        script: list[int] | None = None,
        origin: int = 0,
        bad_seek: int | None = None,
    ) -> None:
        self.data = data
        self.script = script or []
        self.origin = origin
        self.bad_seek = bad_seek
        self.pos = 0
        self.reads = 0
        self.seeks = 0
        self.log: list[str] = []

    def seek(self, offset: int) -> int:
        # A real FileIO converts the argument first; what it refuses
        # there (OverflowError) never reaches the file. No conversion
        # refuses an offset below 2**31.
        if offset >> 31:
            with open(os.devnull, "rb", buffering=0) as probe:
                try:
                    probe.seek(offset)
                except OSError:
                    pass
        self.log.append(f"s{offset}")
        self.seeks += 1
        if self.bad_seek == self.seeks - 1:
            raise OSError(5, "Input/output error")
        self.pos = offset
        return offset

    def read(self, n: int) -> bytes | None:
        self.log.append(f"r{n}")
        rule = self.script[self.reads % len(self.script)] if self.script else n
        self.reads += 1
        if rule == -1:
            raise OSError(5, "Input/output error")
        if rule == -2:
            raise PermissionError(13, "Permission denied")
        if rule == 0:
            return None
        at = self.pos - self.origin
        if not 0 <= at < len(self.data):
            return b""
        out = self.data[at : at + min(n, rule)]
        self.pos += len(out)
        return out


_RAISES = (zlib.error, OSError, OverflowError, IndexError, struct.error)


def _raise(exc: BaseException) -> list[Any]:
    if isinstance(exc, zlib.error):
        return ["z", int(str(exc).split()[1])]
    if isinstance(exc, struct.error):
        return ["x", "struct.error"]
    return ["x", type(exc).__name__]


def _data(value: bytes | None) -> list[Any]:
    if value is None:
        return ["n"]
    if len(value) <= 48:
        return ["b", value.hex()]
    return ["B", len(value), _sha(value)]


def _log(fh: _File) -> Any:
    text = " ".join(fh.log)
    if len(text) <= 400:
        return text
    return {"calls": len(fh.log), "sha": _sha(text.encode())}


# -- streams -----------------------------------------------------------------


def _ops(fh: _File, chunk: int, ops: list[list[Any]]) -> list[Any]:
    stream = g._Stream(fh, chunk)  # type: ignore[arg-type]
    out: list[Any] = []
    for kind, n in ops:
        try:
            got = stream.read(n) if kind == "r" else stream.read_to_end(n)
        except _RAISES as exc:
            answer = _raise(exc)
        else:
            answer = _data(got)
        out.append([answer, fh.reads, fh.pos])
    return out


def _st(
    why: str,
    data: bytes,
    ops: list[list[Any]],
    chunk: int = 1024,
    script: list[int] | None = None,
    spelled: Any = None,
) -> Case:
    case: Case = {"f": "st", "w": why, "d": _sp(data) if spelled is None else spelled}
    case["c"] = chunk
    if script:
        case["s"] = script
    case["o"] = ops
    case["r"] = _ops(_File(data, script), chunk, ops)
    return case


def _r(*sizes: int) -> list[list[Any]]:
    return [["r", n] for n in sizes]


def _header_cases() -> Iterator[Case]:
    good = _fixed(b"hello")
    yield _st("an empty file", b"", [["r", 1], ["e", 0], ["r", 0]])
    yield _st("one byte of the header", b"\x78", _r(1, 1))
    yield _st("the header alone", b"\x78\x9c", [["r", 1], ["e", 0]])
    yield _st("the header check fails", b"\x78\x9d" + good[2:], _r(1, 1, 1), chunk=2)
    yield _st("the header check fails, in one chunk", b"\x78\x9d" + good[2:], _r(1, 1))
    yield _st("a method other than deflate", _header(0x79) + good[2:], _r(1))
    yield _st("a window of 2**16", _header(0x88) + good[2:], _r(1))
    yield _st("a window of 2**8", _header(0x08) + good[2:], _r(9))
    preset = _header(0x78, 0x20)
    yield _st(
        "a preset dictionary is asked for",
        preset + b"\x01\x02\x03\x04" + good[2:],
        _r(1, 1),
    )
    yield _st("a preset dictionary, its id cut", preset + b"\x01\x02\x03", _r(1, 1))
    yield _st(
        "a preset dictionary, byte by byte", preset + b"\x00" * 9, _r(1, 1), chunk=1
    )
    far = _lits(bytes(i % 251 for i in range(301))) + [(1, 3, 300)]
    stream = _zs(lambda w: w.fixed(1, far), _plain(far), header=_header(0x08))
    yield _st("a distance past the window the header states", stream, [["e", 304]])


def _block_cases() -> Iterator[Case]:
    yield _st("a block of type 3", _zs(lambda w: w.block(1, 3), b""), _r(1, 1))
    yield _st(
        "a stored block whose lengths disagree",
        _zs(lambda w: w.stored(1, b"abc", 0), b"abc"),
        _r(3),
    )
    two = _zs(lambda w: w.stored(0, b"").stored(1, b"abc"), b"abc")
    yield _st("an empty stored block, then a last one", two, [["e", 3]])
    stored = _stored(b"0123456789")
    yield _st("a stored block cut in its data", stored[:12], [["r", 100], ["e", 0]])
    yield _st("a stored block cut in its lengths", stored[:5], _r(1))
    yield _st("the limit falls inside a stored block", stored, _r(3, 3, 100, 1))
    yield _st("a stored block, byte by byte", stored, [["e", 10]], chunk=1)
    yield _st("a fixed block", _fixed(b"hello"), [["r", 2], ["e", 3]])
    yield _st(
        "a fixed block, byte by byte", _fixed(b"hello"), _r(1, 1, 1, 1, 1, 1), chunk=1
    )
    rle: list[Token] = [(0, 0x61), (1, 258, 1), (1, 3, 259)]
    yield _st(
        "a match of 258 at distance 1",
        _zs(lambda w: w.fixed(1, rle), _plain(rle)),
        [["e", 262]],
    )
    yield _st(
        "the limit falls inside a match",
        _zs(lambda w: w.fixed(1, rle), _plain(rle)),
        _r(2, 255, 2, 3, 1),
    )

    def bad_symbol(w: _Bits) -> None:
        w.block(1, 1).code(_FIXED_LIT[0x61]).code(_FIXED_LIT[286])

    yield _st("literal/length symbol 286", _zs(bad_symbol, b"a"), _r(1, 1))
    yield _st(
        "literal/length symbol 286, the limit above it", _zs(bad_symbol, b"a"), _r(2)
    )

    def bad_distance(w: _Bits) -> None:
        w.block(1, 1).code(_FIXED_LIT[0x61]).code(_FIXED_LIT[257]).code(_FIXED_DIST[30])

    yield _st("distance symbol 30", _zs(bad_distance, b"a"), _r(1, 1))
    first: list[Token] = [(1, 3, 1)]
    yield _st("a match before any output", _zs(lambda w: w.fixed(1, first), b""), _r(1))
    past: list[Token] = [(0, 1), (0, 2), (1, 3, 3)]
    yield _st(
        "a distance one past what is out",
        _zs(lambda w: w.fixed(1, past), b""),
        _r(2, 1),
    )
    yield _st(
        "the same, the limit above it", _zs(lambda w: w.fixed(1, past), b""), _r(3)
    )


def _window_cases() -> Iterator[Case]:
    def run(matches: int, back: int) -> bytes:
        toks: list[Token] = [(0, 0x78), *[(1, 258, 1)] * matches, (1, 3, back)]
        plain = b"\x78" * (1 + 258 * matches + (3 if back <= 1 + 258 * matches else 0))
        return _zs(lambda w: w.fixed(1, toks), plain)

    stream = run(127, 32767)
    yield _st("a distance of all 32,767 bytes out", stream, [["e", 32770]])
    stream = run(127, 32768)
    yield _st("a distance of 32,768 with 32,767 out", stream, _r(32767, 1))
    stream = run(128, 32768)
    yield _st("a distance of 32,768 with 33,025 out", stream, [["r", 33000], ["e", 28]])
    yield _st(
        "the same, a byte at a time into the window",
        stream,
        _r(33024, 1, 1, 1, 1, 1),
        chunk=7,
    )


def _table_cases() -> Iterator[Case]:
    eob = _lens({256: 1}, 257)
    one = [0]

    def header(counts: tuple[int, int, int]) -> Callable[[_Bits], Any]:
        return lambda w: (
            w.block(1, 2).bits(counts[0], 5).bits(counts[1], 5).bits(counts[2], 4)
        )

    yield _st(
        "287 literal/length symbols", _zs(header((30, 0, 0)), b"") + b"\x00" * 8, _r(1)
    )
    yield _st("31 distance symbols", _zs(header((0, 30, 0)), b"") + b"\x00" * 8, _r(1))
    yield _st("the counts cut", _zs(header((0, 0, 0)), b"")[:3], _r(1))

    def cll_only(cll: list[int], ncode: int = 19) -> Callable[[_Bits], Any]:
        def body(w: _Bits) -> None:
            w.block(1, 2).bits(0, 5).bits(0, 5).bits(ncode - 4, 4)
            for i in range(ncode):
                w.bits(cll[_ORDER[i]], 3)

        return body

    over = _lens({0: 1, 1: 1, 2: 1}, 19)
    yield _st(
        "a code length code over-subscribed",
        _zs(cll_only(over), b"") + b"\x00" * 40,
        _r(1),
    )
    yield _st(
        "a code length code of one code",
        _zs(cll_only(_lens({0: 1}, 19)), b"") + b"\x00" * 40,
        _r(1),
    )
    none = _zs(cll_only([0] * 19, 4), b"", check=b"")
    yield _st(
        "a code length code of no code: a bit a length", none + b"\xff" * 33, _r(1)
    )
    yield _st("the same, its 258 bits cut", none + b"\xff" * 31, _r(1, 1))

    def dyn(
        ll: list[int],
        dl: list[int],
        body: Callable[[_Bits], Any],
        cll: list[int] = _CLL_A,
        seq: list[tuple[int, int, int]] | None = None,
    ) -> Callable[[_Bits], Any]:
        def write(w: _Bits) -> None:
            w.block(1, 2).tables(ll, dl, cll, seq)
            body(w)

        return write

    def nothing(w: _Bits) -> None:
        pass

    yield _st(
        "a repeat with no length before it",
        _zs(dyn(eob, one, nothing, _CLL_B, [(16, 0, 2)]), b""),
        _r(1),
    )
    long_run = [(18, 127, 7), (18, 127, 7)]
    yield _st(
        "a repeat past the count",
        _zs(dyn(eob, one, nothing, _CLL_B, long_run), b""),
        _r(1),
    )
    yield _st(
        "no end-of-block code",
        _zs(dyn(_lens({0: 1, 1: 1}, 257), one, nothing), b""),
        _r(1),
    )
    short = _lens({0: 2, 256: 2}, 257)
    yield _st(
        "literal/length codes that leave a gap",
        _zs(dyn(short, one, nothing), b""),
        _r(1),
    )
    many = _lens({0: 1, 1: 1, 256: 1}, 257)
    yield _st(
        "literal/length codes over-subscribed", _zs(dyn(many, one, nothing), b""), _r(1)
    )
    lit_a = _lens({0x61: 1, 256: 2, 257: 2}, 258)
    yield _st(
        "distance codes that leave a gap", _zs(dyn(lit_a, [2, 2], nothing), b""), _r(1)
    )
    codes = _canon(lit_a)

    def only(w: _Bits) -> None:
        w.code(codes[0x61]).code(codes[257]).bits(0, 1).code(codes[256])

    yield _st(
        "one distance code of one bit", _zs(dyn(lit_a, [1], only), b"aaaa"), [["e", 4]]
    )

    def other(w: _Bits) -> None:
        w.code(codes[0x61]).code(codes[257]).bits(1, 1).code(codes[256])

    yield _st(
        "the other half of a one-bit distance code",
        _zs(dyn(lit_a, [1], other), b"a"),
        _r(1, 1),
    )
    yield _st("the same, the limit above it", _zs(dyn(lit_a, [1], other), b"a"), _r(2))

    def lits(w: _Bits) -> None:
        w.code(codes[0x61]).code(codes[0x61]).code(codes[256])

    yield _st(
        "no distance code and no match", _zs(dyn(lit_a, [0], lits), b"aa"), [["e", 2]]
    )

    def match(w: _Bits) -> None:
        # Eight literals put the distance's one bit first in a byte.
        for _ in range(8):
            w.code(codes[0x61])
        w.code(codes[257]).bits(0, 1)

    unmatched = _zs(dyn(lit_a, [0], match), b"")
    yield _st("no distance code and a match", unmatched, _r(8))
    yield _st("the same, less asked", unmatched, _r(7, 1))
    yield _st("the same, the distance's bit not there", unmatched[:-5], _r(8, 1))
    yield _st(
        "end of block alone, in one bit",
        _zs(dyn(eob, one, lambda w: w.bits(0, 1)), b""),
        [["e", 0]],
    )
    yield _st(
        "the other half of that bit",
        _zs(dyn(eob, one, lambda w: w.bits(1, 1)), b""),
        _r(1),
    )
    by_runs = [3, 3, 3, 3] + [0] * 252 + [1] + [0, 0, 0]
    runs = [(3, 0, 0), (16, 0, 2), (18, 127, 7), (18, 103, 7), (1, 0, 0), (17, 4, 3)]
    run_codes = _canon(by_runs)

    def four(w: _Bits) -> None:
        for b in range(4):
            w.code(run_codes[b])
        w.code(run_codes[256])

    stream = _zs(dyn(by_runs, [0] * 4, four, _CLL_B, runs), bytes(range(4)))
    yield _st(
        "lengths by the three repeat codes, one run across both tables",
        stream,
        [["e", 4]],
    )


def _end_cases() -> Iterator[Case]:
    data = b"0123456789abcdefghij"
    good = _fixed(data)
    n = len(data)
    yield _st("the end seen with the last byte", good, [["e", n], ["r", 1]])
    yield _st("the end in the next chunk", good, [["e", n]], chunk=len(good) - 4)
    yield _st("byte by byte", good, [["e", n]], chunk=1)
    yield _st("longer than asked", good, [["e", n - 1]])
    yield _st("shorter than asked", good, [["e", n + 1]])
    yield _st("nothing asked of a stream that holds bytes", good, [["e", 0]])
    yield _st("nothing asked of an empty stream", _fixed(b""), [["e", 0]])
    yield _st("no byte asked: no read", good, _r(0, 0))
    for k in range(4):
        yield _st(
            f"the check cut to {k} bytes",
            good[: len(good) - 4 + k],
            [["r", n], ["e", 0]],
        )
    yield _st("the check cut, read to the end", good[:-1], [["e", n]])
    wrong = good[:-1] + bytes([good[-1] ^ 1])
    yield _st("the check differs", wrong, _r(n, 1))
    yield _st("the check differs, less asked", wrong, _r(n - 1, 1))
    yield _st(
        "the check differs, in the next chunk", wrong, _r(n, 1, 1), chunk=len(good) - 1
    )
    yield _st("bytes after the stream", good + b"\xff\x00garbage", [["e", n], ["r", 5]])
    yield _st("a second stream after the first", good + good, [["e", n], ["r", 5]])
    yield _st(
        "bytes after the stream, three at a time",
        good + b"tail",
        _r(7, 7, 7, 7),
        chunk=3,
    )
    then_bad = _zs(lambda w: w.stored(0, data).block(1, 3), data)
    yield _st("the limit met, then a block of type 3", then_bad, _r(n))
    yield _st(
        "the same, the bad block in the next chunk", then_bad, _r(n, 1, 1), chunk=27
    )
    yield _st("the same, a byte at a time: the tail is kept", then_bad, _r(*[1] * 22))
    far: list[Token] = [*_lits(b"01234"), (1, 3, 9)]
    then_far = _zs(lambda w: w.fixed(1, far), b"")
    yield _st(
        "the limit met before a match that reaches too far", then_far, _r(5, 1, 1)
    )
    yield _st(
        "an empty read in the middle", good, _r(n, 1), chunk=8, script=[8, 0, 8, 8]
    )
    yield _st(
        "an empty read, then the rest", good, _r(5, n), chunk=8, script=[8, 0, 8, 8]
    )
    yield _st(
        "a read raises OSError, then goes on",
        good,
        _r(n, n),
        chunk=8,
        script=[8, -1, 8, 8],
    )
    yield _st(
        "a read raises PermissionError",
        good,
        [["e", n], ["e", n]],
        chunk=8,
        script=[-2, 8],
    )
    yield _st(
        "reads that come back short", good, [["e", n]], chunk=1024, script=[1, 2, 3]
    )
    yield _st(
        "after a zlib.error each call reads on",
        b"\x78\x9d" + b"\x00" * 9,
        _r(1, 1, 1, 1),
        chunk=3,
    )


def _large_cases() -> Iterator[Case]:
    stream, size = _run_lengths(0x61, 38)
    spelled = _periodic(stream, 4, 13)
    assert _unspell(spelled) == stream
    blocks = [32768, 1, 65535, size]
    yield _st(
        "past one block of output, 78,433 bytes", stream, _r(*blocks), spelled=spelled
    )
    yield _st(
        "the same, read to the end", stream, [["e", size]], chunk=65536, spelled=spelled
    )
    yield _st(
        "the same, 64 bytes a read",
        stream,
        [["r", 32], ["e", size - 32]],
        chunk=64,
        spelled=spelled,
    )
    stream, size = _run_lengths(0x7A, 8130)
    spelled = _periodic(stream, 4, 13)
    assert _unspell(spelled) == stream and size > g._PIECE
    piece = [["r", g._PIECE], ["r", 1], ["e", size - g._PIECE - 1]]
    yield _st(
        "past the most asked of zlib at once, 16 MiB",
        stream,
        piece,
        chunk=65536,
        spelled=spelled,
    )
    yield _st(
        "the same in one call", stream, [["e", size]], chunk=65536, spelled=spelled
    )
    yield _st(
        "the same, a byte more asked",
        stream,
        [["e", size + 1]],
        chunk=65536,
        spelled=spelled,
    )
    # One call stops at 16 MiB inside the last match with all of its
    # input used, so the next turn reads the file (empty) and finishes the
    # match from zlib's state: one read more than a call without the cap.
    stream, size = _dense(1, 65028)
    assert size - 258 < g._PIECE < size
    yield _st(
        "16 MiB out inside the last match, the input used up",
        stream,
        _r(size, 1),
        chunk=65536,
    )
    yield _st("the same, read to the end", stream, [["e", size]], chunk=65536)
    # Below 16 MiB one call gives all that is asked: one read.
    stream, size = _dense(3, 32514)
    assert size - 258 < 1 << 23 < size
    yield _st("8 MiB and a match out of one call", stream, _r(size, 1), chunk=65536)


# -- packs -------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _entry(code: int, size: int, base: int | bytes | None = None) -> bytes:
    """A pack entry's header: the type and size, then an OFS_DELTA's
    distance or a REF_DELTA's base name."""
    first = (code << 4) | (size & 15)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    if isinstance(base, int):
        tail = [base & 0x7F]
        base >>= 7
        while base:
            base -= 1
            tail.append(0x80 | (base & 0x7F))
            base >>= 7
        out += bytes(reversed(tail))
    elif base is not None:
        out += base
    return bytes(out)


def _delta(source: int, target: int, rest: bytes = b"") -> bytes:
    return _varint(source) + _varint(target) + rest


class _Pack:
    """A pack written entry by entry, and the offsets of its names."""

    def __init__(self, hash_len: int = 20) -> None:
        self.data = bytearray(b"PACK\x00\x00\x00\x02\x00\x00\x00\x00")
        self.hash_len = hash_len
        self.at: dict[str, int] = {}
        self.ix: list[tuple[bytes, Any]] = []

    def name(self, label: str) -> bytes:
        return _bytes(f"object {label}", self.hash_len)

    def add(self, label: str, header: bytes, stream: bytes) -> int:
        offset = len(self.data)
        self.at[label] = offset
        self.ix.append((self.name(label), offset))
        self.data += header + stream
        return offset

    def back(self, label: str) -> int:
        return len(self.data) - self.at[label]

    def listed(self, label: str, offset: Any) -> None:
        self.ix.append((self.name(label), offset))

    def spec(self, spelled: Any = None) -> dict[str, Any]:
        ix = [[name.hex(), offset] for name, offset in sorted(self.ix)]
        data = _sp(bytes(self.data)) if spelled is None else spelled
        return {"p": data, "h": self.hash_len, "ix": ix}


def _index_of(spec: dict[str, Any]) -> Any:
    """The parsed index of a pack's ``ix``, by the header's rule."""
    hash_len = spec["h"]
    names = [bytes.fromhex(name) for name, _ in spec["ix"]]
    assert names == sorted(names)
    fanout = (
        [sum(1 for n in names if n[0] <= i) for i in range(256)]
        if hash_len
        else [0] * 256
    )
    small: list[int] = []
    large: list[int] = []
    for _, offset in spec["ix"]:
        if isinstance(offset, list):
            small.append(offset[1])
        elif offset < 0x80000000:
            small.append(offset)
        else:
            small.append(0x80000000 | len(large))
            large.append(offset)
    data = b"\xfftOc\x00\x00\x00\x02" + struct.pack(">256I", *fanout)
    data += b"".join(names) + b"\x00" * (4 * len(names))
    data += b"".join(struct.pack(">I", v) for v in small)
    data += b"".join(struct.pack(">Q", v) for v in large)
    data += b"\x00" * (2 * hash_len)
    index = g._parse_index(data, b"p.pack", hash_len)
    assert index is not None
    return index


def _main_pack() -> _Pack:
    p = _Pack()
    blob = b"0123456789"
    p.add("a", _entry(3, 10), _stored(blob))
    d1 = _delta(10, 12, b"\x90\x0a\x02zz")
    p.add("b", _entry(6, len(d1), p.back("a")), _fixed(d1))
    d2 = _delta(12, 12, b"\x90\x0c")
    p.add("c", _entry(6, len(d2), p.back("b")), _stored(d2))
    p.add("d", _entry(7, len(d1), p.name("a")), _stored(d1))
    p.add("e", _entry(7, len(d1), p.name("nowhere")), _stored(d1))
    p.add("f", _entry(7, len(d1), p.name("f")), _stored(d1))
    p.add("g", _entry(6, len(d1), 0), _stored(d1))
    at = len(p.data)
    p.add("h", _entry(6, len(d1), at), _stored(d1))
    p.add("i", _entry(6, len(d1), len(p.data) + 5), _stored(d1))
    p.add("commit", _entry(1, 7), _fixed(b"tree 00"))
    p.add("tree", _entry(2, 0), _stored(b""))
    p.add("tag", _entry(4, 300), _fixed(b"object "))
    p.add("zero", b"\x05", _stored(blob))
    p.add("five", b"\x55", _stored(blob))
    p.add("n", _entry(6, len(d1), p.back("a")), b"\x78\x9d" + _stored(d1)[2:])
    p.add("o", _entry(6, len(d2), p.back("n")), _stored(d2))
    p.add("p", _entry(6, 1, p.back("a")), _stored(b"\x80"))
    big = _delta(10, 5000)
    p.add("q", _entry(6, len(big), p.back("a")), _stored(big))
    p.add("r", _entry(3, 5000), _stored(blob))
    p.add("s", _entry(7, len(d1), p.name("t")), _stored(d1))
    p.add("t", _entry(6, len(d1), p.back("s")), _stored(d1))
    p.add("u", _entry(7, len(d1), p.name("past")), _stored(d1))
    p.listed("past", ["raw", 0x80000007])
    p.add("v", _entry(7, len(d1), p.name("far")), _stored(d1))
    p.listed("far", 1 << 63)
    p.add("w", _entry(7, len(d1), p.name("beyond")), _stored(d1))
    p.listed("beyond", 1_000_000)
    for label, filler in (("x", 19), ("x20", 18)):
        long = _delta(10, 12, b"\x00" * filler)
        stream = _stored(long)
        wrong = stream[:-1] + bytes([stream[-1] ^ 1])
        p.add(label, _entry(6, len(long), p.back("a")), wrong)
    p.add("y", _entry(6, 3, p.back("a")), _stored(_delta(10, 1 << 70)))
    wide = _entry(3, 1 << 70)
    p.add("z", wide, _stored(blob))
    p.add("cut", _entry(7, len(d1), p.name("a"))[:11], b"")
    return p


def _wide_pack() -> _Pack:
    p = _Pack(32)
    p.add("a", _entry(1, 4), _fixed(b"tree"))
    d = _delta(4, 4, b"\x90\x04")
    p.add("b", _entry(7, len(d), p.name("a")), _stored(d))
    p.add("c", _entry(7, len(d), p.name("b")), _fixed(d))
    p.add("d", _entry(7, len(d), p.name("b")[:20]), _stored(d))
    p.add("e", _entry(6, len(d), p.back("c")), _stored(d))
    return p


def _ladder(rungs: int, listed: bool) -> tuple[_Pack, Any]:
    """A base of 11 bytes and `rungs` deltas of 10 bytes, each naming
    the entry before it at distance 23, each stating a target of 10.
    Rung `k` is at `_rung(k)`; the index lists the rungs where `listed`."""
    p = _Pack()
    p.add("base", _entry(3, 11), _stored(b"0123456789a"))
    d = _delta(11, 10, b"\x90\x0a\x00\x00\x00\x00\x00\x00")
    unit = _entry(6, len(d), 23) + _stored(d)
    assert len(unit) == 23 and len(p.data) == _rung(0)
    head = bytes(p.data)
    for k in range(rungs):
        if listed:
            p.listed(f"rung {k}", _rung(k))
        p.data += unit
    return p, [head.hex(), [unit.hex(), rungs]]


def _rung(k: int) -> int:
    return 35 + 23 * k


def _walk(spec: dict[str, Any], fh: _File, at: int, cap: int | None) -> tuple[Any, Any]:
    index = _index_of(spec)
    try:
        walked = g._chain(fh, index, at, cap=cap)  # type: ignore[arg-type]
    except _RAISES as exc:
        return _raise(exc), None
    if walked is None:
        return None, None
    code, links = walked
    spelled = [
        [
            link.start,
            format(link.size, "x"),
            None if link.target is None else format(link.target, "x"),
        ]
        for link in links
    ]
    if len(links) > 8:
        return {
            "links": len(links),
            "sha": _sha(_links_text(code, links).encode()),
        }, links
    return [code, spelled], links


def _links_text(code: int, links: list[Any]) -> str:
    parts = [str(code)]
    for link in links:
        target = "-" if link.target is None else format(link.target, "x")
        parts.append(f"{link.start},{link.size:x},{target}")
    return ";".join(parts)


def _ch(
    why: str,
    pk: int,
    spec: dict[str, Any],
    at: int,
    cap: int | None = None,
    *,
    script: list[int] | None = None,
    origin: int = 0,
    bad_seek: int | None = None,
    own: bool = False,
) -> Case:
    case: Case = {"f": "ch", "w": why}
    if own:
        case.update(spec)
    else:
        case["pk"] = pk
    case["at"] = at
    case["cap"] = cap
    if script:
        case["s"] = script
    if origin:
        case["org"] = origin
    if bad_seek is not None:
        case["ks"] = bad_seek
    fh = _File(_unspell(spec["p"]), script, origin, bad_seek)
    case["r"], _ = _walk(spec, fh, at, cap)
    case["log"] = _log(fh)
    return case


def _chain_cases(packs: list[tuple[_Pack, dict[str, Any]]]) -> Iterator[Case]:
    main, spec = packs[0]
    at = main.at
    four = 4 << 20
    yield _ch("a base: no stream is read", 0, spec, at["a"])
    yield _ch("a base under a cap", 0, spec, at["a"], four)
    yield _ch("a delta by distance: its target is read", 0, spec, at["b"])
    yield _ch("the same under a cap", 0, spec, at["b"], four)
    yield _ch("two deltas: the second target is not read", 0, spec, at["c"])
    yield _ch("two deltas under a cap: both are", 0, spec, at["c"], four)
    yield _ch("a delta by name", 0, spec, at["d"])
    yield _ch("a base name the index lacks", 0, spec, at["e"])
    yield _ch("a delta that names itself", 0, spec, at["f"])
    yield _ch("a distance of zero", 0, spec, at["g"])
    yield _ch("a distance of the whole offset", 0, spec, at["h"])
    yield _ch("a distance past the offset", 0, spec, at["i"])
    yield _ch("a commit", 0, spec, at["commit"], four)
    yield _ch("a tree of no bytes", 0, spec, at["tree"], 0)
    yield _ch("a tag whose size its stream does not hold", 0, spec, at["tag"], 300)
    yield _ch("the same, a cap one below its size", 0, spec, at["tag"], 299)
    yield _ch("type code 0", 0, spec, at["zero"])
    yield _ch("type code 5", 0, spec, at["five"])
    yield _ch("a delta whose stream fails its header check", 0, spec, at["n"])
    yield _ch("a delta on it: its own head alone is read", 0, spec, at["o"])
    yield _ch("the same under a cap: the base's head raises", 0, spec, at["o"], four)
    yield _ch("a delta head that ends in a continuation", 0, spec, at["p"])
    yield _ch("a target of 5,000 under a cap of 5,000", 0, spec, at["q"], 5000)
    yield _ch("a target of 5,000 under a cap of 4,999", 0, spec, at["q"], 4999)
    yield _ch("a target of 5,000 and no cap", 0, spec, at["q"])
    yield _ch("a size of 5,000 under a cap of 5,000", 0, spec, at["r"], 5000)
    yield _ch("a size of 5,000 under a cap of 4,999", 0, spec, at["r"], 4999)
    yield _ch("the delta's own size above the cap", 0, spec, at["b"], 8)
    yield _ch("the delta's own size at the cap, its target above", 0, spec, at["b"], 9)
    yield _ch("a cap of 12 holds both", 0, spec, at["b"], 12)
    yield _ch("a cap of 2**64 - 1", 0, spec, at["c"], (1 << 64) - 1)
    yield _ch("two entries that name each other", 0, spec, at["s"])
    yield _ch("the same from the other", 0, spec, at["t"], four)
    yield _ch("a base listed past the index's 64-bit table", 0, spec, at["u"])
    yield _ch("a base listed at 2**63", 0, spec, at["v"])
    yield _ch("a base listed past the pack's end", 0, spec, at["w"])
    yield _ch(
        "a delta of 21 bytes, its check wrong: the head reads", 0, spec, at["x"], four
    )
    yield _ch(
        "a delta of 20 bytes, its check wrong: the head's read meets it",
        0,
        spec,
        at["x20"],
        four,
    )
    yield _ch("a target past 64 bits and no cap", 0, spec, at["y"])
    yield _ch(
        "a target past 64 bits under the largest cap", 0, spec, at["y"], (1 << 64) - 1
    )
    yield _ch("a size past 64 bits and no cap", 0, spec, at["z"])
    yield _ch(
        "a size past 64 bits under the largest cap", 0, spec, at["z"], (1 << 64) - 1
    )
    yield _ch("a header the pack's end cuts", 0, spec, at["cut"])
    yield _ch("an offset at the pack's end", 0, spec, len(main.data))
    yield _ch("offset 0: the pack's own header", 0, spec, 0)
    yield _ch("an offset of 2**63 - 1", 0, spec, (1 << 63) - 1)
    yield _ch("an offset of 2**63", 0, spec, 1 << 63)
    yield _ch("an offset of 2**64 - 1", 0, spec, (1 << 64) - 1, four)
    yield _ch("the first seek raises OSError", 0, spec, at["b"], bad_seek=0)
    yield _ch("the seek to the delta's stream raises", 0, spec, at["b"], bad_seek=1)
    yield _ch("the seek to the base raises", 0, spec, at["b"], bad_seek=2)
    yield _ch("the header's read raises OSError", 0, spec, at["b"], script=[-1])
    yield _ch(
        "the stream's read raises PermissionError", 0, spec, at["b"], script=[64, -2]
    )
    yield _ch("the header's read comes back empty", 0, spec, at["b"], script=[0, 64])
    yield _ch(
        "the header's read comes back short of the name",
        0,
        spec,
        at["d"],
        script=[9, 64],
    )
    yield _ch(
        "the stream's reads come back a byte at a time",
        0,
        spec,
        at["b"],
        four,
        script=[64, 1, 1, 1],
    )
    yield _ch("the stream's read comes back empty", 0, spec, at["b"], script=[64, 0])
    edge = (1 << 63) - at["b"] - 2
    yield _ch(
        "a delta whose stream starts at 2**63", 0, spec, (1 << 63) - 2, origin=edge
    )
    edge = (1 << 63) - at["a"] - 1
    yield _ch(
        "a base whose stream starts at 2**63", 0, spec, (1 << 63) - 1, origin=edge
    )
    wide, wspec = packs[1]
    yield _ch("names of 32 bytes", 1, wspec, wide.at["c"], four)
    yield _ch(
        "a name of 20 bytes where 32 are read: the stream is read from its middle",
        1,
        wspec,
        wide.at["d"],
    )
    yield _ch("a distance onto a chain by name", 1, wspec, wide.at["e"], 6)
    yield _ch(
        "the same under a cap of 1: its own size is past it", 1, wspec, wide.at["e"], 1
    )
    named = {
        "p": _sp(b"PACK" + bytes(8) + _entry(7, 2) + _stored(_delta(1, 1))),
        "h": 0,
        "ix": [],
    }
    yield _ch("names of no bytes", 0, named, 12, own=True)
    lspec = packs[2][1]
    yield _ch(
        "seven rungs of 20 and a base of 11 under a cap of 11: 151 of 176",
        2,
        lspec,
        _rung(6),
        11,
    )
    yield _ch(
        "eight rungs and the base under a cap of 11: 171 of 176",
        2,
        lspec,
        _rung(7),
        11,
    )
    yield _ch("nine rungs under a cap of 11: 180 at the ninth", 2, lspec, _rung(8), 11)
    yield _ch(
        "eight rungs under a cap of 10: 160 is 16 caps, the base is past one",
        2,
        lspec,
        _rung(7),
        10,
    )
    yield _ch("nine rungs under a cap of 10: 180 at the ninth", 2, lspec, _rung(8), 10)
    yield _ch("4,096 entries", 2, lspec, _rung(4094))
    yield _ch("4,097 entries", 2, lspec, _rung(4095))
    yield _ch("4,096 entries under a cap", 2, lspec, _rung(4094), four)
    yield _ch("4,097 entries under a cap", 2, lspec, _rung(4095), four)


def _ia(
    why: str,
    data: bytes,
    start: int,
    size: int,
    *,
    script: list[int] | None = None,
    origin: int = 0,
    spelled: Any = None,
) -> Case:
    case: Case = {"f": "ia", "w": why, "p": _sp(data) if spelled is None else spelled}
    case["l"] = [start, format(size, "x")]
    if script:
        case["s"] = script
    if origin:
        case["org"] = origin
    fh = _File(data, script, origin)
    try:
        got = g._inflate_at(fh, g._Link(start, size, None))  # type: ignore[arg-type]
    except _RAISES as exc:
        case["r"] = _raise(exc)
    else:
        case["r"] = _data(got)
    case["log"] = _log(fh)
    return case


def _inflate_cases() -> Iterator[Case]:
    blob = b"0123456789"
    pack = b"\x00" * 7 + _stored(blob) + b"next entry"
    yield _ia("the whole stream, of its size", pack, 7, 10)
    yield _ia("a size one below the stream's", pack, 7, 9)
    yield _ia("a size one above the stream's", pack, 7, 11)
    yield _ia("a size of zero where the stream holds bytes", pack, 7, 0)
    yield _ia("a size of zero and an empty stream", b"\x00" + _fixed(b""), 1, 0)
    yield _ia("a start past the file's end", pack, 500, 10)
    yield _ia("a stream cut before its check", pack[:20], 7, 10)
    yield _ia("a stream that fails its header check", b"\x78\x9d" + pack[9:], 0, 10)
    yield _ia("a read raises OSError", pack, 7, 10, script=[-1])
    yield _ia("a read comes back empty", pack, 7, 10, script=[0])
    yield _ia("a start of 2**63", pack, 1 << 63, 10)
    yield _ia("a start of 2**63 - 1", pack, (1 << 63) - 1, 10)
    yield _ia(
        "the stream ends at 2**63", pack, (1 << 63) - 21, 10, origin=(1 << 63) - 28
    )

    def slow(w: _Bits) -> None:
        for _ in range(40):
            w.stored(0, b"")
        w.stored(1, b"abcd")

    yield _ia(
        "forty empty blocks first: reads of the size and 64", _zs(slow, b"abcd"), 0, 4
    )
    stream, size = _run_lengths(0x62, 32)
    spelled = _periodic(stream, 4, 13)
    assert size == 66049 and _unspell(spelled) == stream
    yield _ia("a size of 66,049: reads of 65,536", stream, 0, size, spelled=spelled)
    yield _ia("a size of 65,472: reads of 65,536", stream, 0, 65472, spelled=spelled)
    yield _ia("a size of 65,471: reads of 65,535", stream, 0, 65471, spelled=spelled)
    stream, size = _dense(1, 65028)
    yield _ia(
        "16 MiB and 9 bytes, the stream cut after its last match", stream, 0, size
    )
    yield _ia("a size past 64 bits", pack, 7, (1 << 64) + 10)
    yield _ia("a size of 2**64 - 1", pack, 7, (1 << 64) - 1)


def _lookup_cases(packs: list[tuple[_Pack, dict[str, Any]]]) -> Iterator[Case]:
    """Over the indexes of packs 0, 4 and 5 of the header."""
    main = packs[0][0]
    a, b = main.name("a"), main.name("b")
    indexes = {i: _index_of(packs[i][1]) for i in (0, 4, 5)}

    def listed(why: str, xs: list[int], raw: bytes) -> Case:
        case: Case = {"f": "li", "w": why, "xs": xs, "raw": raw.hex()}
        chosen = [indexes[i] for i in xs]
        try:
            found = list(g._listed_in(chosen, raw))
        except _RAISES as exc:
            case["r"] = _raise(exc)
        else:
            case["r"] = [
                [next(n for n, x in enumerate(chosen) if x is pack), at]
                for pack, at in found
            ]
        return case

    yield listed("in two packs of three, in their order", [0, 4, 5], a)
    yield listed("in the first and the last", [5, 4, 0], b)
    yield listed("in no pack", [0, 4, 5], main.name("nowhere"))
    yield listed(
        "listed past the 64-bit table in both: left out", [0, 4], main.name("past")
    )
    yield listed("a 64-bit offset", [4, 0], main.name("far"))
    yield listed("no pack", [], a)
    yield listed("a name cut short", [0], a[:19])
    yield listed("an empty name", [0, 4], b"")
    yield listed("an empty name and no pack", [], b"")

    def take(why: str, xs: list[int], want: list[bytes], found: list[str]) -> Case:
        table = {raw: raw.hex() for raw in want}
        got = set(found)
        case: Case = {"f": "tk", "w": why, "xs": xs}
        case["want"] = [[raw.hex(), sha] for raw, sha in table.items()]
        case["found"] = sorted(found)
        answer: Case = {}
        try:
            g._take([indexes[i] for i in xs], table, got)
        except _RAISES as exc:
            answer["x"] = _raise(exc)
        answer["want"] = [[raw.hex(), sha] for raw, sha in table.items()]
        answer["found"] = sorted(got)
        case["r"] = answer
        return case

    nowhere = main.name("nowhere")
    yield take(
        "each name from the first pack that lists it",
        [4, 5, 0],
        [nowhere, b, a, main.name("c")],
        [],
    )
    yield take(
        "what is left keeps its order", [5], [nowhere, b, a, main.name("c")], ["kept"]
    )
    yield take("nothing wanted", [0, 4], [], ["kept"])
    yield take("no pack", [], [a], [])
    yield take(
        "every name in the first pack: the rest is not asked", [0, 4], [a, b], []
    )
    yield take(
        "a name listed past the 64-bit table is found", [4], [main.name("past")], []
    )
    yield take("an empty name raises before anything moves", [0], [a, b"", b], [])
    yield take(
        "an empty name: the first pack raises though it lists the other",
        [4, 0],
        [a, b""],
        [],
    )
    yield take("an empty name and no pack", [], [b""], [])


# -- the sweeps --------------------------------------------------------------

_CHUNKS = [1, 2, 3, 7, 13, 64, 65, 100, 257, 1024, 65536]
_SCRIPT = [0, 1, 2, 5, 6, 14, 15, 16, 100, -1]
_CAPS = [None, None, None, 0, 1, 12, 40, 4096, 4 << 20, (1 << 64) - 1]
_FAR = [(1 << 63) - 1, 1 << 63, (1 << 64) - 1, 0]
_PEEKS = [0, 1, 3, 20, 63, 64, -1]


def _mutate(rng: _Rng, data: bytes) -> bytes:
    b = bytearray(data)
    kind = rng.below(12)
    if kind == 0 or not b:
        return bytes(b)
    if kind == 1:
        del b[rng.below(len(b) + 1) :]
    elif kind == 2:
        i = rng.below(len(b))
        b[i] ^= 1 << rng.below(8)
    elif kind == 3:
        i = rng.below(len(b))
        b[i] = rng.below(256)
    elif kind == 4:
        for _ in range(rng.pick([1, 2, 5, 40])):
            b.append(rng.below(256))
    elif kind == 5:
        del b[rng.below(len(b))]
    elif kind == 6:
        i = rng.below(len(b) + 1)
        b.insert(i, rng.below(256))
    elif kind == 7:
        for _ in range(2 + rng.below(4)):
            i = rng.below(len(b))
            b[i] ^= 1 << rng.below(8)
    elif kind == 8:
        del b[rng.below(len(b) + 1) :]
        for _ in range(1 + rng.below(30)):
            b.append(rng.below(256))
    elif kind == 9:
        i = rng.below(min(len(b), 6))
        b[i] ^= 1 << rng.below(8)
    elif kind == 10:
        i = len(b) - 1 - rng.below(min(len(b), 6))
        b[i] ^= 1 << rng.below(8)
    else:
        b += data
    return bytes(b)


def _mutated(rng: _Rng, data: bytes, most: int) -> bytes:
    """`data` after none, one or two mutations."""
    times = [0, 1, 1, 2][rng.below(most)]
    for _ in range(times):
        data = _mutate(rng, data)
    return data


def _scripted(rng: _Rng, odds: int, entries: list[int], most: int) -> list[int]:
    if rng.below(odds):
        return []
    script = [rng.pick(entries) for _ in range(1 + rng.below(most))]
    if all(k <= 0 for k in script):
        script.append(7)
    return script


def _brief(answer: list[Any]) -> str:
    if answer[0] == "b":
        value = bytes.fromhex(answer[1])
        return f"b{len(value)}.{_sha(value)[:16]}"
    if answer[0] == "B":
        return f"b{answer[1]}.{answer[2][:16]}"
    return "".join(str(part) for part in answer)


def _sweep_stream(rng: _Rng, bases: list[tuple[bytes, int]]) -> str:
    stream, total = rng.pick(bases)
    data = _mutated(rng, stream, 4)
    chunk = rng.pick(_CHUNKS)
    script = _scripted(rng, 5, _SCRIPT, 4)
    sizes = [0, 1, 2, 20, 32, 100, 257, 258, 259, 1000, 32768, 32769, 65536]
    sizes += [total, max(total - 1, 0), total + 1, 10**9, max(total // 2, 1)]
    shape = rng.below(10)
    ops: list[list[Any]]
    if shape < 3:
        ops = [["r", 32], ["e", max(total - 32, 0)]]
    elif shape == 3:
        ops = [["e", total]]
    elif shape == 4:
        ops = [["r", 20]]
    elif shape == 5:
        ops = [["r", 1]] * min(total + 3, 12)
    else:
        ops = []
        for _ in range(1 + rng.below(4)):
            kind = "e" if rng.below(4) == 0 else "r"
            ops.append([kind, rng.pick(sizes)])
    answers = _ops(_File(data, script), chunk, ops)
    return ",".join(f"{_brief(a)}/{reads}/{pos}" for a, reads, pos in answers)


def _sweep_chain(rng: _Rng, packs: list[tuple[dict[str, Any], bytes, Any]]) -> str:
    spec, raw, index = rng.pick(packs)
    data = _mutated(rng, raw, 3)
    offsets = [o for _, o in spec["ix"] if not isinstance(o, list)]
    where = rng.below(8)
    if where < 5:
        at = rng.pick(offsets)
    elif where == 5:
        at = rng.below(len(data) + 2)
    elif where == 6:
        at = rng.pick(_FAR)
    else:
        at = rng.pick(offsets) + 1
    cap = rng.pick(_CAPS)
    fh = _File(data, _scripted(rng, 8, _PEEKS, 3))
    try:
        walked = g._chain(fh, index, at, cap=cap)  # type: ignore[arg-type]
    except _RAISES as exc:
        return f"{_brief(_raise(exc))}|{_sha(' '.join(fh.log).encode())[:16]}"
    if walked is None:
        return f"n|{_sha(' '.join(fh.log).encode())[:16]}"
    parts = [_links_text(walked[0], walked[1])]
    for link in walked[1][:6]:
        try:
            got = g._inflate_at(fh, link)  # type: ignore[arg-type]
        except _RAISES as exc:
            parts.append(_brief(_raise(exc)))
        else:
            parts.append(_brief(_data(got)))
    return f"{'&'.join(parts)}|{_sha(' '.join(fh.log).encode())[:16]}"


def _sweep(rule: str, seed: int, n: int, one: Callable[[_Rng], str]) -> Case:
    rng = _Rng(seed)
    digest = hashlib.sha256()
    for _ in range(n):
        digest.update(one(rng).encode() + b"\n")
    return {"f": "sw", "rule": rule, "seed": seed, "n": n, "sha": digest.hexdigest()}


def generate() -> dict[str, bytes]:
    bases = _bases()
    main, wide = _main_pack(), _wide_pack()
    ladder, spelled = _ladder(4096, False)
    small, small_spelled = _ladder(24, True)
    other, third = _Pack(), _Pack()
    other.data = third.data = bytearray()
    other.listed("a", 77)
    other.listed("far", 1 << 40)
    other.listed("past", ["raw", 0x80000009])
    third.listed("b", 5)
    packs = [
        (main, main.spec()),
        (wide, wide.spec()),
        (ladder, ladder.spec(spelled)),
        (small, small.spec(small_spelled)),
        (other, other.spec()),
        (third, third.spec()),
    ]
    swept = [
        (spec, bytes(pack.data), _index_of(spec))
        for pack, spec in (packs[0], packs[1], packs[3])
    ]
    cases = [
        *_header_cases(),
        *_block_cases(),
        *_window_cases(),
        *_table_cases(),
        *_end_cases(),
        *_large_cases(),
        *_chain_cases(packs),
        *_inflate_cases(),
        *_lookup_cases(packs),
    ]
    for seed in range(1, 5):
        cases.append(
            _sweep("stream", seed, 4000, lambda rng: _sweep_stream(rng, bases))
        )
    for seed in range(11, 15):
        cases.append(_sweep("chain", seed, 1500, lambda rng: _sweep_chain(rng, swept)))
    header = {
        "fixture": "gitobjects_streams",
        "cases": len(cases),
        "bases": [
            [stream.hex(), size]
            if len(stream) < 430
            else [_periodic(stream, 4, 13), size]
            for stream, size in bases
        ],
        "packs": [spec for _, spec in packs],
        "swept": [0, 1, 3],
    }
    return {"gitobjects_streams.jsonl": jsonl([header, *cases])}
