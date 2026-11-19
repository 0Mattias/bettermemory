"""Fixtures: verification staleness (``verify_status.jsonl``, not gated).

The native build reads it in
``native/crates/bettermemory-core/src/verify_status.rs``. This pins it to
`verify_status`'s `compute_verification_status`,
`remote_verification_status` and `VerificationStatus.to_dict`, and through
them `_stale_recommendation`, `_remote_recommendation` and
`_NEVER_RECOMMENDATION`.

One JSON object per line, keys sorted, ASCII. The first line is the header
``{"k": "header", "default": DEFAULT_VERIFICATION_STALE_DAYS, "count": n,
...}``.
Every other line is a case:

- ``f``: ``"c"`` (`compute_verification_status`) or ``"r"``
  (`remote_verification_status`);
- ``lv``: the stamp, null (``"c"`` only) or a datetime;
- ``now``: the clock (``"c"`` only), a datetime;
- ``s``: ``stale_after_days``, absent for the default (always an `int`
  within 64 bits);
- ``o``: ``to_dict()`` of the answer, or ``{"raised": class}``. Its
  ``recommendation`` is null, ``{"never": true}`` (the header's
  ``never``), ``{"stale": n}`` (`_stale_recommendation(n)`) or
  ``{"remote": stamp}`` (`_remote_recommendation` of the block's stamp,
  ``stamp`` being its spelling in the block); the generator asserts each.

The header also holds ``never`` (`_NEVER_RECOMMENDATION`), ``stale``
(``[n, text]`` pairs of `_stale_recommendation`) and ``remote`` (``[stamp,
text]`` pairs of `_remote_recommendation`, the stamp a datetime as below,
made UTC where naive).

A datetime is ``[year, month, day, hour, minute, second, microsecond,
offset]``, the offset in microseconds east of UTC (a `timezone` of that
`timedelta`) or null for a naive value. Nothing here depends on the Python
release, the Unicode data or the platform: the arithmetic is integer
`timedelta` arithmetic, one correctly rounded `int / int` and a float floor
division, all IEEE double.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import verify_status as vs

from ._common import jsonl

_DT = list[Any]

_UTC = 0
_H = 3_600_000_000


def _make(spec: _DT) -> datetime:
    y, mo, d, h, mi, s, us, offset = spec
    tz = None if offset is None else timezone(timedelta(microseconds=offset))
    return datetime(y, mo, d, h, mi, s, us, tzinfo=tz)


def _case(f: str, lv: _DT | None, now: _DT | None, s: int | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {} if s is None else {"stale_after_days": s}
    record: dict[str, Any] = {"f": f, "lv": lv}
    if s is not None:
        record["s"] = s
    try:
        if f == "c":
            assert now is not None
            record["now"] = now
            got = vs.compute_verification_status(
                None if lv is None else _make(lv), now=_make(now), **kwargs
            )
        else:
            assert lv is not None
            got = vs.remote_verification_status(_make(lv), **kwargs)
        record["o"] = _spelled(got)
    except TypeError as exc:
        record["o"] = {"raised": type(exc).__name__}
    return record


def _spelled(got: vs.VerificationStatus) -> dict[str, Any]:
    """`to_dict()`, its recommendation by reference to the header's texts."""
    out = got.to_dict()
    rec = out["recommendation"]
    if rec is None:
        return out
    if rec == vs._NEVER_RECOMMENDATION:
        out["recommendation"] = {"never": True}
    elif got.age_days is not None and rec == vs._stale_recommendation(got.age_days):
        out["recommendation"] = {"stale": got.age_days}
    else:
        assert got.last_verified_at is not None
        assert rec == vs._remote_recommendation(got.last_verified_at)
        out["recommendation"] = {"remote": out["last_verified_at"]}
    return out


def _shift(spec: _DT, us: int) -> _DT:
    moved = _make(spec[:7] + [None]) + timedelta(microseconds=us)
    return [
        moved.year,
        moved.month,
        moved.day,
        moved.hour,
        moved.minute,
        moved.second,
        moved.microsecond,
        spec[7],
    ]


_BASE = [2026, 3, 1, 12, 0, 0, 0, _UTC]
_DAY = 86_400_000_000
_OFFSETS: list[int | None] = [
    None,
    0,
    2 * _H,
    -(5 * _H + 30 * 60_000_000),
    30_000_000,
    500_000,
    -1_000_000,
    -1,
    86_399_999_999,
    -86_399_999_999,
    23 * _H + 59 * 60_000_000,
]
_STALE: list[int | None] = [
    None,
    0,
    -1,
    1,
    29,
    30,
    31,
    365,
    -(2**63),
    2**63 - 1,
    2**53 // 86_400,
    2**53 // 86_400 + 1,
    106_751_991_167_300,
]


def _cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    now = _BASE
    # Never verified, over every threshold, a naive and an aware clock.
    for s in _STALE:
        out.append(_case("c", None, now, s))
    out.append(_case("c", None, now[:7] + [None], None))
    # Around the 30-day bound, in microseconds.
    for delta in (
        0,
        1,
        -1,
        _DAY - 1,
        _DAY,
        _DAY + 1,
        30 * _DAY - 1,
        30 * _DAY,
        30 * _DAY + 1,
        31 * _DAY,
        31 * _DAY - 1,
        -30 * _DAY,
        1000 * _DAY + 999_999,
    ):
        for s in (None, 0, -5, 1, 30, 31):
            out.append(_case("c", _shift(now, -delta), now, s))
    # Every offset on the stamp and on the clock; naive stamps read as UTC.
    for lo in _OFFSETS:
        for no in _OFFSETS:
            lv = [2026, 1, 30, 11, 59, 59, 999_999, lo]
            out.append(_case("c", lv, _BASE[:7] + [no], None))
        out.append(_case("r", [2025, 7, 4, 0, 0, 0, 0, lo], None, None))
        out.append(_case("r", [2025, 7, 4, 0, 0, 0, 123_456, lo], None, -3))
    for s in _STALE:
        out.append(_case("r", [1, 1, 1, 0, 0, 0, 0, None], None, s))
    # The extremes of the range: the microsecond count passes 2^53, so
    # total_seconds rounds, and the floor division follows the rounding.
    first = [1, 1, 1, 0, 0, 0, 0, None]
    last = [9999, 12, 31, 23, 59, 59, 999_999, None]
    out.append(_case("c", first, last[:7] + [0], None))
    out.append(_case("c", last, first[:7] + [0], None))
    out.append(_case("c", [1, 1, 2, 0, 0, 0, 0, -86_399_999_999], last[:7] + [0], 0))
    out.append(_case("c", first, [9999, 12, 31, 0, 0, 0, 0, 86_399_999_999], 0))
    for days in (2_000_000, 2_500_000, 3_000_000, 3_652_058):
        for us in (-1, 0, 1, 2, 3, 499_999, 500_000, 500_001):
            lv = _shift(last, -(days * _DAY + us))
            out.append(_case("c", lv[:7] + [None], last[:7] + [0], days))
            out.append(_case("c", lv[:7] + [None], last[:7] + [0], days - 1))
    # Spread: a fixed seed, so the same bytes on every run.
    rng = random.Random(20261119)
    for _ in range(120):
        ordinal = rng.randrange(1, 3_652_059)
        d = datetime.fromordinal(ordinal)
        lv = [
            d.year,
            d.month,
            d.day,
            rng.randrange(24),
            rng.randrange(60),
            rng.randrange(60),
            rng.choice((0, rng.randrange(1_000_000))),
            rng.choice(_OFFSETS),
        ]
        try:
            later = _shift(lv, rng.choice((1, -1)) * rng.randrange(400 * _DAY))
        except OverflowError:
            later = lv
        no = rng.choice(_OFFSETS[1:])
        s = rng.choice(_STALE)
        out.append(_case("c", lv, later[:7] + [no], s))
    return out


def generate() -> dict[str, bytes]:
    """``verify_status.jsonl``: not gated."""
    cases = _cases()
    header = {
        "k": "header",
        "default": vs.DEFAULT_VERIFICATION_STALE_DAYS,
        "count": len(cases),
        "never": vs._NEVER_RECOMMENDATION,
        "stale": [[n, vs._stale_recommendation(n)] for n in (0, 1, 31, 3_652_058)],
        "remote": [
            [spec, vs._remote_recommendation(_make(spec).replace(tzinfo=timezone.utc))]
            for spec in ([2025, 7, 4, 0, 0, 0, 0, None], [1, 1, 1, 0, 0, 0, 1, None])
        ],
    }
    return {"verify_status.jsonl": jsonl([header, *cases])}
