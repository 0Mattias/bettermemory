"""Fixture: the text of a record id as the reference mints and reads it
(``ulid.jsonl``).

`models.generate_ulid` takes ``int(time.time() * 1000)``, masks it to 48
bits, draws ten bytes from `secrets.token_bytes` and writes both in
Crockford base 32 (`_encode_crockford`, ten and sixteen characters).
`models.is_valid_ulid` reads a text as an id. Each case drives the
reference's own function with its clock and its random bytes replaced
(`unittest.mock.patch.object` on the module's ``time`` and ``secrets``;
no process environment is touched).

The first line is a header with the counts. Then:

- ``[ms, bytes, text]``: a millisecond value and ten random bytes (hex)
  and the text `generate_ulid` returns for them; pairs sharing their bytes
  as ``{"r": bytes, "m": [[ms, text], ...]}``, or sharing their
  milliseconds as ``{"ms": ms, "b": [[bytes, text], ...]}``. The clock is given the
  float ``ms / 1000`` (or a neighbour of it) for which
  ``int(t * 1000) == ms``, checked before the call.
- ``{"f": bits, "t": text}`` or ``{"f": bits, "x": name}``: ``time.time``
  returning the float with these IEEE bits (hex) and ten zero bytes, and
  the text or the exception's type: the float-to-milliseconds cases.
- ``{"v": text, "ok": bool}``: `is_valid_ulid` of a text, and
  ``{"at": i, "c": code point, "ok": bool}`` of 26 ``0`` with the
  character at ``i`` replaced.

Not gated: integers, bytes and an ASCII-class pattern, no Unicode data,
no interpreter wording (an exception is named by its type only) and IEEE
double multiplication, which is the same on every platform CI runs.
"""

from __future__ import annotations

import math
import struct
from types import SimpleNamespace
from unittest.mock import patch

from bettermemory import models

from ._common import dumps_line

_MASK = (1 << 48) - 1
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _bits(value: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', value))[0]:016x}"


def _minted(clock: float, random: bytes) -> str:
    with (
        patch.object(models, "time", SimpleNamespace(time=lambda: clock)),
        patch.object(
            models, "secrets", SimpleNamespace(token_bytes=lambda n: random[:n])
        ),
    ):
        return models.generate_ulid()


def _clock_for(ms: int) -> float:
    """A float the reference turns into exactly ``ms``."""
    t = ms / 1000
    for _ in range(64):
        got = int(t * 1000)
        if got == ms:
            return t
        t = math.nextafter(t, math.inf if got < ms else -math.inf)
    raise AssertionError(f"no clock for {ms}")


def _pairs() -> list[tuple[int, bytes]]:
    zero, ones = bytes(10), b"\xff" * 10
    out: list[tuple[int, bytes]] = []
    # Time 0, 1, the last 48-bit value and past it (masked by the reference).
    for ms in [0, 1, 2, 31, 32, _MASK - 1, _MASK, _MASK + 1, _MASK + 2]:
        out += [(ms, zero), (ms, ones)]
    out += [(1 << 49, zero), ((1 << 50) - 1, ones), ((1 << 52) + 12345, zero)]
    out += [((1 << 51) - 1, ones), (1_763_251_200_000, zero), (1_763_251_200_000, ones)]
    # Each single bit, of the time and of the bytes.
    for bit in range(48):
        out.append((1 << bit, zero))
    for bit in range(80):
        out.append((0, (1 << bit).to_bytes(10, "big")))
    # Each 5-bit group at every position taking every value, the rest all
    # zero and all ones. Time position 0 holds two bits of the 48: values
    # past 7 there are milliseconds past 48 bits.
    for pos in range(10):
        shift = 5 * (9 - pos)
        for value in range(32):
            for fill, rnd in ((0, zero), (_MASK, ones)):
                ms = (fill & ~(31 << shift)) | (value << shift)
                out.append((ms, rnd))
    for pos in range(16):
        shift = 5 * (15 - pos)
        for value in range(32):
            for fill, ms in ((0, 0), ((1 << 80) - 1, _MASK)):
                n = (fill & ~(31 << shift)) | (value << shift)
                out.append((ms, n.to_bytes(10, "big")))
    # Pairs from a fixed linear congruential rule, over the whole range.
    state = 0x2545F4914F6CDD1D
    for _ in range(400):
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        ms = state >> 16
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        hi = state
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        out.append((ms, ((hi << 16) | (state >> 48)).to_bytes(10, "big")))
    return list(dict.fromkeys(out))


def _clocks() -> list[float]:
    out: list[float] = [
        0.0,
        -0.0,
        5e-324,
        1e-3,
        0.0009999999999999998,
        0.001,
        0.0015,
        0.0019999999999999,
        -1e-4,
        -0.001,
        -0.5,
        -1.0,
        -1e9,
        1.0,
        1.5,
        0.1,
        0.3,
        0.7,
        2.675,
        1763251200.0,
        1763251200.123,
        1763251200.9995,
        1763251200.999,
        2.0**48 / 1000,
        (2.0**48 - 1) / 1000,
        2.0**53 / 1000,
        2.0**60,
        2.0**63 / 1000,
        2.0**63,
        2.0**64,
        -(2.0**63) / 1000,
        -(2.0**64),
        1e20,
        -1e20,
        1e300,
        -1e300,
        1.7976931348623157e308,
        math.inf,
        -math.inf,
        math.nan,
    ]
    # Each millisecond m near the edges and today, and the floats around
    # m / 1000 and (m + 0.5) / 1000, where truncation and rounding part.
    for m in [
        1,
        3,
        7,
        9,
        10,
        999,
        1001,
        123456789,
        1763251200001,
        1763251200999,
        _MASK,
    ]:
        for base in (m / 1000, (m + 0.5) / 1000, (m + 1) / 1000):
            t = base
            for _ in range(3):
                t = math.nextafter(t, -math.inf)
            for _ in range(7):
                out.append(t)
                t = math.nextafter(t, math.inf)
    return list({_bits(t): t for t in out}.values())


#: Each character tried at each position of a text otherwise all ``0``:
#: the look-alikes, lowercase, ASCII marks, controls and non-ASCII digits
#: and letters.
_BAD = "ILOUilou!/:@[`{-_ \x7f\x00\xe9\u0130\uff10\U0001d7ce"


def _texts() -> list[str]:
    return [
        "0" * 26,
        "7" + "Z" * 25,
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "Z" * 26,
        "8" + "0" * 25,
        "0" * 25,
        "0" * 27,
        "",
        "0" * 26 + "\n",
        "\n" + "0" * 26,
        " " + "0" * 25,
        "01arz3ndektsv4rrffq69g5fav",
        "0" * 25 + "a",
        *(ch + "0" * 25 for ch in _ALPHABET),
    ]


def _grouped(pairs: list[tuple[int, bytes, str]]) -> list[object]:
    """Pairs sharing their bytes (or else their milliseconds) with nine
    others or more, as one line; the rest one line each."""
    by_bytes: dict[bytes, list[tuple[int, str]]] = {}
    for ms, rnd, text in pairs:
        by_bytes.setdefault(rnd, []).append((ms, text))
    by_ms: dict[int, list[tuple[str, str]]] = {}
    out: list[object] = []
    for rnd, group in by_bytes.items():
        if len(group) >= 10:
            out.append({"r": rnd.hex(), "m": [list(p) for p in group]})
        else:
            for ms, text in group:
                by_ms.setdefault(ms, []).append((rnd.hex(), text))
    for ms, rows in by_ms.items():
        if len(rows) >= 10:
            out.append({"ms": ms, "b": [list(p) for p in rows]})
        else:
            out += [[ms, hexed, text] for hexed, text in rows]
    return out


def generate() -> dict[str, bytes]:
    pairs = [(ms, rnd, _minted(_clock_for(ms), rnd)) for ms, rnd in _pairs()]
    records = _grouped(pairs)
    floats = _clocks()
    for t in floats:
        try:
            records.append({"f": _bits(t), "t": _minted(t, bytes(10))})
        except (ValueError, OverflowError) as exc:
            records.append({"f": _bits(t), "x": type(exc).__name__})
    texts = _texts()
    for text in texts:
        records.append({"v": text, "ok": models.is_valid_ulid(text)})
    swept = 0
    for at in range(26):
        for ch in _BAD:
            text = "0" * at + ch + "0" * (25 - at)
            records.append({"at": at, "c": ord(ch), "ok": models.is_valid_ulid(text)})
            swept += 1
    header = {
        "fixture": "ulid",
        "pairs": len(pairs),
        "floats": len(floats),
        "texts": len(texts) + swept,
        "valid": sum(1 for r in records if isinstance(r, dict) and r.get("ok") is True),
    }
    body = "".join(dumps_line(r) + "\n" for r in [header, *records])
    return {"ulid.jsonl": body.encode("ascii")}
