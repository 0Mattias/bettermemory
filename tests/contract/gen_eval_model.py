"""Fixtures: the eval family's model, unit eval_model-1
(``eval_model.jsonl``, ``eval_model_python.jsonl``) and unit
eval_model-2 (``eval_model_rules.jsonl``, written by `generate_rules`,
whose text says how).

The native build reads them in
``native/crates/bettermemory-core/src/eval_model.rs``. This pins it to
`eval_model`'s defaults, `_SINCE_PATTERN`, `_SINCE_UNIT_TO_SECONDS`,
`_EARLIEST_START`, `EARLIER_RULES`, `RateCI` (`from_counts`, `to_dict`),
`_wilson_interval`, `ColdEndorsementMemoriesRow` (`endorsement_ratio`,
`to_dict`), `SilentMissCandidate.to_dict`, `parse_since`,
`parse_timestamp_utc`, `parse_window_bound`, `resolve_window`,
`_show_in_window`, `_canonical_top_hit_id` and `_silent_miss_from_event`.

The first line is a header with the count of rows and the constants. Then
one row per case, its kind under ``k``; an exception is ``x`` (its class)
with ``msg`` (its text, `_common.text_field`), or ``msg_len`` and
``msg_sha256`` (of its UTF-8) for a text longer than 300 code points:

- ``rate``: ``n``, ``d`` and ``r``, the values of
  `RateCI.from_counts(n, d).to_dict()` (neutral) under the header's
  ``rate_keys``;
- ``wilson``: ``kk``, ``n``, ``z`` (neutral) and ``r``, the two bounds;
- ``cold``: ``in``, the row's fields, and ``r``, the values of `to_dict`
  (neutral) under ``cold_keys``;
- ``since``: ``spec`` and ``r``, the span in microseconds (decimal text)
  or null;
- ``ts``: ``spec``, ``flag`` and ``r``, `parse_timestamp_utc` (fields);
- ``bound``: ``spec``, ``flag``, ``now`` and ``r``: null, ``{"span":
  microseconds}`` or ``{"at": fields}``;
- ``resolve``: ``since``, ``until`` (null, ``{"span": ...}`` or ``{"at":
  ...}``), ``now`` and ``r``, the two ends (fields or null);
- ``window``: ``show`` (neutral), ``start``, ``end`` and ``r``;
- ``event``: ``ev`` (neutral), or ``ev_with``, the header's
  ``event_base`` with each ``[key, value]`` set in turn (an existing key
  keeps its place, a new one goes last; a null value drops the key);
  ``top``, `_canonical_top_hit_id`; and ``miss``,
  `_silent_miss_from_event` as the values of `to_dict` under
  ``miss_keys``, or null.

A spec is ``spec`` (text or null, `text_field`), or ``spec_runs``, a list
of ``[character, count]`` runs, for a long one. A datetime is its fields,
``[year, month, day, hour, minute, second, microsecond, offset]``, the
offset in microseconds or null when naive.

The last line of ``eval_model.jsonl`` is a sweep: ``count`` pairs drawn
with `_Draw` from ``seed`` through `RateCI.from_counts`, the SHA-256 of
the lines ``<n> <d> <rate> <lower> <upper> <torn>`` (floats as their bits
in hex, None as ``-``).

``eval_model.jsonl`` is not gated: integer division, the float steps of
the interval, ``repr`` of ASCII and of the BMP characters used here, the
decimal digits ``\\d`` reads here (Unicode 6 and older) and the
`fromisoformat` forms written here read the same under 3.11 to 3.14 on
every system. ``eval_model_python.jsonl`` holds what follows the release:
timestamps at `fromisoformat`'s edges (offsets of seconds and
microseconds, a lowercase ``z`` inside `parse_event_ts`, reduced and
compact forms, an hour of 24) and the text of `int()`'s digit limit,
CPython's own message. 3.11.15 and 3.12.13 write it byte for byte as
3.13.13 does. 3.14.10 does not: its `fromisoformat` keeps an offset's
microseconds, which 3.13.13 drops (``2026-10-01T00:00:00-00:00:00.000001``
is 00:00 UTC under 3.13.13 and 00:00:00.000001 under 3.14, and
``0001-01-01T00:00:00+00:00:00.000001`` leaves the calendar only under
3.14), and it reads an hour of 24 (``2026-10-01T24:00:00Z``, refused by
3.13.13, is the next midnight). CI's 3.13.18 reads `fromisoformat`
otherwise than 3.13.13 too (``PYTHON_VERSION`` in `generate.py`).
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import eval_model as em

from ._common import jsonl, neutral, text_field

_MASK = (1 << 64) - 1
I64_MAX = (1 << 63) - 1
I64_MIN = -(1 << 63)
SWEEP_SEED = 20261125
SWEEP_COUNT = 20000
UTC = timezone.utc


class _Draw:
    """`nxt` steps x = x * 6364136223846793005 + 1442695040888963407 mod
    2**64 and gives x >> 33; `pick(n)` is `nxt() % n`."""

    def __init__(self, seed: int) -> None:
        self.x = seed

    def nxt(self) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & _MASK
        return self.x >> 33

    def pick(self, n: int) -> int:
        return self.nxt() % n

    def wide(self) -> int:
        """A 93-bit draw: three steps."""
        return (self.nxt() << 62) ^ (self.nxt() << 31) ^ self.nxt()


def _bits(x: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', x))[0]:016x}"


def _fields(dt: datetime | None) -> list[int | None] | None:
    if dt is None:
        return None
    off = dt.utcoffset()
    return [
        dt.year,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
        dt.microsecond,
        None if off is None else off // timedelta(microseconds=1),
    ]


def _us(span: timedelta) -> str:
    return str(span // timedelta(microseconds=1))


def _err(exc: Exception) -> dict[str, Any]:
    text = str(exc)
    out: dict[str, Any] = {"x": type(exc).__name__}
    if len(text) > 300:
        out["msg_len"] = len(text)
        out["msg_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        out.update(text_field("msg", text))
    return out


def _try(fn: Callable[[], Any], spell: Callable[[Any], Any]) -> Any:
    try:
        return {"r": spell(fn())}
    except Exception as exc:  # the class and text are the answer
        return _err(exc)


def _spec(spec: str | None) -> dict[str, Any]:
    if spec is None or len(spec) <= 120:
        return {"spec": spec} if spec is None else text_field("spec", spec)
    runs: list[list[Any]] = []
    for ch in spec:
        if runs and runs[-1][0] == ch:
            runs[-1][1] += 1
        else:
            runs.append([ch, 1])
    return {"spec_runs": runs}


RATE_KEYS = [
    "numerator",
    "denominator",
    "rate",
    "ci95_lower",
    "ci95_upper",
    "torn_read",
]
COLD_KEYS = [
    "id",
    "scopes",
    "summary",
    "retrieval_count",
    "auto_applied_count",
    "explicit_applied_count",
    "endorsement_ratio",
]
MISS_KEYS = [
    "ts",
    "session_id",
    "top_missed_id",
    "top_missed_relevance",
    "threshold_rule",
    "recent_retrieval_count",
]


def _values(d: dict[str, Any], keys: list[str]) -> list[Any]:
    """`to_dict`'s values (neutral), its keys checked against ``keys``,
    which the header holds."""
    assert list(d) == keys, list(d)
    return [neutral(v) for v in d.values()]


def _bound(b: timedelta | datetime | None) -> Any:
    if b is None:
        return None
    if isinstance(b, timedelta):
        return {"span": _us(b)}
    return {"at": _fields(b)}


# ---------------------------------------------------------------------------
# RateCI and the interval
# ---------------------------------------------------------------------------

_RATES = [
    (0, 0),
    (0, -1),
    (5, -1),
    (5, I64_MIN),
    (0, 1),
    (1, 1),
    (2, 1),
    (1, 3),
    (2, 3),
    (3, 7),
    (-1, 3),
    (2**53 + 1, 2**53 + 1),
    (2**53 + 1, 2**53 + 2),
    (2**53 + 1, 2**53 + 3),
    (2**53 + 1, 2**53 + 4),
    (2**53 + 2, 2**53 + 5),
    (I64_MIN, 1),
    (I64_MIN, I64_MAX),
    (I64_MAX, I64_MAX),
    (I64_MAX - 1, I64_MAX),
    (1, I64_MAX),
    (I64_MAX, 1),
    (I64_MAX, 2),
    (1, 2),
    (49, 50),
]


def _rate_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "rate",
            "n": n,
            "d": d,
            "r": _values(em.RateCI.from_counts(n, d).to_dict(), RATE_KEYS),
        }
        for n, d in _RATES
    ]


_ZS = [
    em._WILSON_Z,
    float("nan"),
    float("inf"),
    float("-inf"),
    0.0,
    -0.0,
    -1.96,
    1e300,
    5e-324,
    1e-160,
]
_KNS = [
    (1, 3),
    (0, 1),
    (1, 1),
    (5, 2),
    (-3, 4),
    (0, 0),
    (1, -1),
    (2**53 + 1, 2**53 + 3),
]


#: Cases past the grid. At this ``z``, ``z * z`` is three times the least
#: subnormal: ``z * z / (2.0 * 3)`` rounds to 0.0, where ``z * z / 2.0 / 3``
#: would give the least subnormal as the upper bound.
_WILSON_MORE = [(0, 3, 3.8499310870763914e-162)]


def _wilson_rows() -> list[dict[str, Any]]:
    rows = []
    cases = [(k, n, z) for z in _ZS for k, n in _KNS] + _WILSON_MORE
    for k, n, z in cases:
        lo, hi = em._wilson_interval(k, n, z)
        rows.append(
            {
                "k": "wilson",
                "kk": k,
                "n": n,
                "z": neutral(z),
                "r": [neutral(lo), neutral(hi)],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# the cold-endorsement row
# ---------------------------------------------------------------------------

_COLD = [
    ("m1", ["global"], "a summary", 30, 0, 0),
    ("m2", [], "", 31, 3, 1),
    ("m3", ["a", "b\ud800"], "s\udcff", 0, 1, 2),
    ("m4", ["x"], "y", 5, -5, 2),
    ("m5", ["x"], "y", 5, 5, -2),
    ("m6", ["x"], "y", I64_MAX, I64_MAX, I64_MAX),
    ("m7", ["x"], "y", I64_MIN, I64_MIN, I64_MIN),
    ("m8", ["x"], "y", 1, I64_MAX, 1),
    ("m9", ["x"], "y", 1, 0, I64_MAX),
    ("m10", ["x"], "y", 1, -1, 1),
]


def _cold_rows() -> list[dict[str, Any]]:
    rows = []
    for id_, scopes, summary, rc, auto, explicit in _COLD:
        row = em.ColdEndorsementMemoriesRow(id_, scopes, summary, rc, auto, explicit)
        rows.append(
            {
                "k": "cold",
                "in": {
                    **text_field("id", id_),
                    "scopes": [text_field("v", s) for s in scopes],
                    **text_field("summary", summary),
                    "retrieval_count": rc,
                    "auto_applied_count": auto,
                    "explicit_applied_count": explicit,
                },
                "r": _values(row.to_dict(), COLD_KEYS),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# --since and --until
# ---------------------------------------------------------------------------

_SINCE: list[str | None] = [
    None,
    "",
    "all",
    " all",
    "all ",
    "All",
    "30d",
    "1s",
    "1m",
    "1h",
    "1d",
    "90m",
    "12h",
    "0d",
    "000s",
    "0007h",
    "  30d  ",
    "\t\n30d\x0b\x0c\r",
    "\x1c30d\x1f",
    "　30d ",
    "\x8530d\xa0",
    "​30d",
    "30 d",
    "30D",
    "30",
    "d",
    "-30d",
    "+30d",
    "3_0d",
    "30dd",
    "30d\n",
    "30d\nx",
    "٣٠d",
    "３０d",
    "१००m",
    "3٠d",
    "３０ｄ",
    "\xb2d",
    "1.5h",
    "1e3s",
    "86399999999999s",
    "86400000000000s",
    "1439999999999m",
    "1440000000000m",
    "23999999999h",
    "24000000000h",
    "999999999d",
    "1000000000d",
    "0" * 30 + "999999999d",
    "9" * 20 + "d",
    "9" * 40 + "s",
    "1" * 4300 + "d",
    "0" * 4300 + "d",
    "0" * 4299 + "1d",
    "\ud800",
    "30d\udcff",
    "'30d\"",
]


def _since_rows(specs: Sequence[str | None]) -> list[dict[str, Any]]:
    return [
        {
            "k": "since",
            **_spec(spec),
            **_try(
                lambda: em.parse_since(spec), lambda s: None if s is None else _us(s)
            ),
        }
        for spec in specs
    ]


NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
NOW_US = datetime(2026, 11, 25, 12, 0, 0, 1, tzinfo=UTC)
NOW_LATE = datetime(2026, 11, 25, 12, 0, 0, 999999, tzinfo=UTC)
NOW_IST = datetime(
    2026, 11, 25, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
NOW_NAIVE = datetime(2026, 11, 25, 12, 0)
NOW_Y1 = datetime(1, 1, 2, tzinfo=UTC)
NOW_Y1_EARLY = datetime(1, 1, 1, 12, 0, tzinfo=UTC)
NOW_Y9999 = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)


def _edge(now: datetime) -> int:
    """The whole seconds of `now - _EARLIEST_START`."""
    return (now - em._EARLIEST_START) // timedelta(seconds=1)


_EDGE = _edge(NOW)
_EDGE_DAYS = (NOW_Y9999 - em._EARLIEST_START).days

_BOUNDS: list[tuple[str | None, str, datetime]] = [
    (None, "--since", NOW),
    ("", "--since", NOW),
    ("  ", "--until", NOW),
    ("all", "--since", NOW),
    (" all ", "--until", NOW),
    ("　all\x1c", "--since", NOW),
    ("ALL", "--since", NOW),
    ("30d", "--since", NOW),
    (" 30d ", "--until", NOW),
    ("٣٠d", "--since", NOW),
    ("0d", "--since", NOW),
    ("0d", "--until", NOW),
    ("1000000000d", "--until", NOW),
    ("9" * 30 + "d", "--since", NOW),
    (f"{_EDGE}s", "--since", NOW),
    (f"{_EDGE + 1}s", "--since", NOW),
    (f"{_EDGE}s", "--until", NOW),
    (f"{_EDGE + 1}s", "--until", NOW),
    (f"{_EDGE}s", "--since", NOW_US),
    (f"{_EDGE + 1}s", "--since", NOW_US),
    (f"{_EDGE + 1}s", "--since", NOW_LATE),
    (f"{_EDGE}s", "--since", NOW_IST),
    (f"{_EDGE + 1}s", "--since", NOW_IST),
    (f"{_EDGE // 86400}d", "--since", NOW),
    (f"{_EDGE // 86400 + 1}d", "--since", NOW),
    ("30d", "--since", NOW_NAIVE),
    ("30d", "--until", NOW_NAIVE),
    ("1s", "--since", NOW_Y1),
    ("0s", "--since", NOW_Y1),
    ("1s", "--since", NOW_Y1_EARLY),
    (f"{_EDGE_DAYS}d", "--since", NOW_Y9999),
    (f"{_EDGE_DAYS + 1}d", "--since", NOW_Y9999),
    ("999999999d", "--since", NOW_Y9999),
    ("2026-10-01T00:00:00Z", "--since", NOW),
    ("2026-10-01T00:00:00z", "--since", NOW),
    ("2026-10-01T00:00:00+02:00", "--since", NOW),
    ("2026-10-01T00:00:00-05:00", "--until", NOW),
    ("2026-10-01T00:00:00.123456Z", "--since", NOW),
    ("  2026-10-01T00:00:00Z\n", "--since", NOW),
    ("2026-10-01T00:00:00", "--since", NOW),
    ("  2026-10-01T00:00:00 ", "--until", NOW),
    ("2026-10-01", "--since", NOW),
    ("0001-01-01T00:00:00+02:00", "--since", NOW),
    ("9999-12-31T23:00:00-02:00", "--until", NOW),
    ("0001-01-01T00:00:00Z", "--since", NOW),
    ("0001-01-01T01:59:59+02:00", "--since", NOW),
    ("9999-12-31T23:59:59Z", "--since", NOW),
    ("9999-12-31T23:59:59Z", "--until", NOW),
    ("2026-11-25T12:00:00Z", "--since", NOW),
    ("2026-11-25T12:00:00.000001Z", "--since", NOW),
    ("2026-11-25T12:00:00.000001Z", "--until", NOW),
    ("2026-11-25T13:00:00+01:00", "--since", NOW),
    ("2026-11-25T11:00:00-01:00", "--since", NOW),
    ("2026-11-25T11:00:01-01:00", "--since", NOW),
    ("2026-11-25T12:00:00.000001Z", "--since", NOW_IST),
    ("2026-11-25T12:00:00Z", "--since", NOW_US),
    ("2026-10-01T00:00:00Z", "--since", NOW_NAIVE),
    ("2026-10-01T00:00:00Z", "--until", NOW_NAIVE),
    ("2026-10-01T00:00:00Z", "--when", NOW),
    ("9999-12-31T23:59:59Z", "--when", NOW),
    ("yesterday", "--since", NOW),
    ("-", "--since", NOW),
    ("abc-def", "--until", NOW),
    ("2026", "--since", NOW),
    ("20261001T000000Z", "--since", NOW),
    ("\xb2-", "--since", NOW),
    ("２０２６-10-01T00:00:00Z", "--since", NOW),
    ("2026-13-01T00:00:00Z", "--since", NOW),
    ("2026-10-01T00:00:00+24:00", "--since", NOW),
    ("2026-10-01T00:00:00Z\ud800", "--since", NOW),
    ("30d-", "--since", NOW),
    ("-30d", "--until", NOW),
    # Each message that spells the stripped spec, from a padded one.
    (" 0001-01-01T00:00:00+02:00 ", "--since", NOW),
    (" 800000d ", "--since", NOW),
    (" 2027-01-01T00:00:00Z ", "--since", NOW),
    (" 1000000000d ", "--until", NOW),
]


def _bound_rows(cases: list[tuple[str | None, str, datetime]]) -> list[dict[str, Any]]:
    return [
        {
            "k": "bound",
            **_spec(spec),
            "flag": flag,
            "now": _fields(now),
            **_try(lambda: em.parse_window_bound(spec, flag=flag, now=now), _bound),
        }
        for spec, flag, now in cases
    ]


_TS = [
    ("2026-10-01T00:00:00Z", "--since"),
    (" 2026-10-01T00:00:00-01:30 ", "--until"),
    ("2026-10-01T00:00:00", "--since"),
    ("not a time", "--since"),
    ("9999-12-31T23:59:59-00:01", "--until"),
    ("Z", "--since"),
    ("", "--since"),
]


def _ts_rows(cases: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "k": "ts",
            **_spec(spec),
            "flag": flag,
            **_try(lambda: em.parse_timestamp_utc(spec, flag=flag), _fields),
        }
        for spec, flag in cases
    ]


_D = timedelta
_AT = datetime(2026, 10, 1, tzinfo=UTC)
_RESOLVE: list[tuple[Any, Any, datetime]] = [
    (None, None, NOW),
    (_D(days=30), None, NOW),
    (None, _D(hours=1), NOW),
    (_D(days=30), _D(hours=1), NOW_IST),
    (_AT, NOW, NOW),
    (_D(days=30), _AT, NOW_NAIVE),
    (_D(days=999999999), None, NOW),
    (None, _D(days=999999999), NOW),
    (_D(microseconds=1), None, datetime(1, 1, 1, tzinfo=UTC)),
    (_D(0), _D(0), datetime(1, 1, 1, tzinfo=UTC)),
    (_D(days=_EDGE_DAYS, seconds=86399, microseconds=999999), None, NOW_Y9999),
    (_D(days=_EDGE_DAYS + 1, seconds=86399, microseconds=999999), None, NOW_Y9999),
]


def _resolve_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "resolve",
            "since": _bound(since),
            "until": _bound(until),
            "now": _fields(now),
            **_try(
                lambda: em.resolve_window(since, until, now=now),
                lambda r: [_fields(r[0]), _fields(r[1])],
            ),
        }
        for since, until, now in _RESOLVE
    ]


# ---------------------------------------------------------------------------
# _show_in_window
# ---------------------------------------------------------------------------

_S = datetime(2026, 1, 1, tzinfo=UTC)
_E = datetime(2026, 2, 1, tzinfo=UTC)
_S_PLUS = datetime(2026, 1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
_NAIVE = datetime(2026, 1, 1)
_SHOWS: list[Any] = [
    {"ts": "2026-01-01T00:00:00Z"},
    {"ts": "2025-12-31T23:59:59.999999Z"},
    {"ts": "2026-02-01T00:00:00Z"},
    {"ts": "2026-01-31T23:59:59.999999+00:00"},
    {"ts": "2026-01-15T00:00:00"},
    {"ts": "2026-01-01T01:00:00+01:00"},
    {"ts": "2026-02-01T00:59:59+01:00"},
    {"ts": "2026-01-31T19:00:00-05:00"},
    {"ts": "2026-01-15"},
    {},
    {"ts": None},
    {"ts": 123},
    {"ts": "garbage"},
    {"ts": ["2026-01-15T00:00:00Z"]},
    {1: "2026-01-15T00:00:00Z", "kind": "show"},
    {"ts": "2026-01-15T00:00:00Z", "rule": "rs-v8"},
    [],
    "2026-01-15T00:00:00Z",
    None,
]
_WINDOWS: list[tuple[datetime | None, datetime | None]] = [
    (None, None),
    (_S, None),
    (None, _E),
    (_S, _E),
    (_S_PLUS, _E),
]


def _window_rows() -> list[dict[str, Any]]:
    rows = []
    for show in _SHOWS:
        for start, end in _WINDOWS:
            rows.append(
                {
                    "k": "window",
                    "show": neutral(show),
                    "start": _fields(start),
                    "end": _fields(end),
                    **_try(lambda: em._show_in_window(show, start, end), lambda r: r),
                }
            )
    for show, start, end in [
        ({"ts": "2026-01-15T00:00:00Z"}, _NAIVE, None),
        ({"ts": "2025-12-15T00:00:00Z"}, _S, _NAIVE),
        ({"ts": "2026-01-15T00:00:00Z"}, _S, _NAIVE),
        ({"ts": "2026-01-15T00:00:00Z"}, None, _NAIVE),
        ({"ts": "bad"}, _NAIVE, _NAIVE),
    ]:
        rows.append(
            {
                "k": "window",
                "show": neutral(show),
                "start": _fields(start),
                "end": _fields(end),
                **_try(lambda: em._show_in_window(show, start, end), lambda r: r),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# the silent-miss readers
# ---------------------------------------------------------------------------

_BASE: dict[str, Any] = {
    "kind": "search_miss",
    "ts": "2026-01-01T00:00:00Z",
    "session_id": "s1",
    "top_hits": [{"id": "m1", "relevance": "high"}, {"id": "m2"}],
    "threshold_rule": "rel>=0.5",
    "recent_retrieval_count": 3,
}


_DROP = object()


class _With:
    """`_BASE` with keys set (an existing key keeps its place, a new one
    goes last) or dropped, in the order given."""

    def __init__(self, **kw: Any) -> None:
        self.kw = kw

    def event(self) -> dict[str, Any]:
        ev = dict(_BASE)
        for key, value in self.kw.items():
            if value is _DROP:
                ev.pop(key, None)
            else:
                ev[key] = value
        return ev

    def spelled(self) -> dict[str, Any]:
        return {
            "ev_with": [
                [key, None if value is _DROP else neutral(value)]
                for key, value in self.kw.items()
            ]
        }


def _with(**kw: Any) -> _With:
    return _With(**kw)


_EVENTS: list[Any] = [
    _with(),
    _with(ts=_DROP),
    _with(ts=5),
    _with(ts=None),
    _with(ts=""),
    _with(ts="\ud800"),
    *[
        _with(session_id=v, session="s2")
        for v in [
            "",
            0,
            False,
            [],
            {},
            None,
            0.0,
            -0.0,
            (),
            "x",
            1,
            True,
            [0],
            float("nan"),
        ]
    ],
    _with(session_id=_DROP, session="s2"),
    _with(session_id=_DROP),
    _with(session_id="", session=5),
    _with(session_id="", session=""),
    _with(session_id=0, session=_DROP),
    _with(session_id=7),
    _with(top_hits=_DROP),
    _with(top_hits=None),
    _with(top_hits=[]),
    _with(top_hits=[{}]),
    _with(top_hits=[{"id": 5, "relevance": "r"}]),
    _with(top_hits=[{"id": "a"}]),
    _with(top_hits=[{"relevance": "low"}]),
    _with(top_hits=["a"]),
    _with(top_hits=[None, {"id": "b"}]),
    _with(top_hits=({"id": "t"},)),
    _with(top_hits="m1"),
    _with(top_hits={"id": "x"}),
    _with(top_hits=_DROP, top_hit_ids=["l1", "l2"]),
    _with(top_hits=[], top_hit_ids=["l1"]),
    _with(top_hits=[], top_hit_ids=[]),
    _with(top_hits=[], top_hit_ids=[5, "l2"]),
    _with(top_hits=[], top_hit_ids="l1"),
    _with(top_hits=[], top_hit_ids=("l1",)),
    _with(top_hits=None, top_hit_ids=["l1"]),
    _with(top_hits=["a"], top_hit_ids=["l1"]),
    _with(top_hits=[{"id": "c"}], top_hit_ids=["l1"]),
    _with(top_hits=[{}], top_hit_ids=["l1"]),
    _with(threshold_rule=_DROP),
    _with(threshold_rule=5),
    _with(threshold_rule=""),
    *[
        _with(recent_retrieval_count=v)
        for v in [
            _DROP,
            True,
            False,
            0,
            -1,
            2**63,
            2**70,
            -(2**80),
            1.5,
            "3",
            None,
            [3],
        ]
    ],
    {"ts": "t", "session": "s"},
    {},
    [],
    "search_miss",
    None,
    5,
]


def _event_rows() -> list[dict[str, Any]]:
    rows = []
    for case in _EVENTS:
        if isinstance(case, _With):
            ev = case.event()
            spelled = case.spelled()
        else:
            ev = case
            spelled = {"ev": neutral(case)}
        miss = _try(
            lambda: em._silent_miss_from_event(ev),
            lambda c: None if c is None else _values(c.to_dict(), MISS_KEYS),
        )
        top = _try(lambda: em._canonical_top_hit_id(ev), lambda r: r)
        if "r" in top and isinstance(top["r"], str):
            top = {"r": text_field("v", top["r"])}
        rows.append({"k": "event", **spelled, "top": top, "miss": miss})
    return rows


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------


def draw_count(d: _Draw) -> int:
    """A count of up to 63 bits, negative one time in eight."""
    width = d.pick(64)
    value = d.wide() & ((1 << width) - 1)
    return -value if d.pick(8) == 0 else value


def draw_pair(d: _Draw) -> tuple[int, int]:
    n = draw_count(d)
    mode = d.pick(4)
    if mode == 0:
        k = draw_count(d)
    elif mode == 1:
        k = d.wide() % (abs(n) + 1)
    elif mode == 2:
        k = n - d.pick(4)
    else:
        k = n + d.pick(4)
    return max(I64_MIN, min(I64_MAX, k)), n


def sweep_digest() -> str:
    d = _Draw(SWEEP_SEED)
    lines = []
    for _ in range(SWEEP_COUNT):
        k, n = draw_pair(d)
        r = em.RateCI.from_counts(k, n)
        parts = ["-" if v is None else _bits(v) for v in (r.rate, r.lower, r.upper)]
        lines.append(f"{k} {n} {' '.join(parts)} {int(r.torn_read)}")
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def generate() -> dict[str, bytes]:
    rows = [
        *_rate_rows(),
        *_wilson_rows(),
        *_cold_rows(),
        *_since_rows(_SINCE),
        *_ts_rows(_TS),
        *_bound_rows(_BOUNDS),
        *_resolve_rows(),
        *_window_rows(),
        *_event_rows(),
    ]
    header = {
        "fixture": "eval_model",
        "count": len(rows),
        "default_since": em.DEFAULT_SINCE_SPEC,
        "min_retrievals": em.DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
        "silent_miss_limit": em.DEFAULT_SILENT_MISS_LIMIT,
        "wilson_z": neutral(em._WILSON_Z),
        "since_pattern": em._SINCE_PATTERN.pattern,
        "since_flags": em._SINCE_PATTERN.flags,
        "units": [[u, s] for u, s in em._SINCE_UNIT_TO_SECONDS.items()],
        "earliest_start": _fields(em._EARLIEST_START),
        "earlier_rules": em.EARLIER_RULES,
        "rate_keys": RATE_KEYS,
        "cold_keys": COLD_KEYS,
        "miss_keys": MISS_KEYS,
        "event_base": neutral(_BASE),
    }
    sweep = {
        "k": "sweep",
        "seed": SWEEP_SEED,
        "count": SWEEP_COUNT,
        "sha256": sweep_digest(),
    }
    return {"eval_model.jsonl": jsonl([header, *rows, sweep])}


# ---------------------------------------------------------------------------
# what follows the release
# ---------------------------------------------------------------------------

_SINCE_PY = [
    "1" * 4301 + "d",
    "0" * 4301 + "d",
    "١" * 4301 + "s",
    "9" * 100000 + "h",
]

_BOUNDS_PY: list[tuple[str | None, str, datetime]] = [
    ("1" * 4301 + "d", "--until", NOW),
    ("2026-10-01T00:00:00+00:00:30", "--since", NOW),
    ("2026-10-01T00:00:00-00:00:00.000001", "--since", NOW),
    ("0001-01-01T00:00:00+00:00:00.000001", "--since", NOW),
    ("0001-01-01T00:00:00.000001+00:00:00.000001", "--since", NOW),
    ("9999-12-31T23:59:59.999999-00:00:00.000001", "--until", NOW),
    ("2026-11-25T12:00:00-00:00:00.000001", "--since", NOW),
    ("2026-11-25T12:00:00+00:00:00.000001", "--since", NOW),
    ("2026-10-01 00:00:00Z", "--since", NOW),
    ("2026-10-01T00:00Z", "--since", NOW),
    ("2026-10-01T00Z", "--since", NOW),
    ("2026-10-01T00:00:00.1Z", "--since", NOW),
    ("2026-10-01T00:00:00.123Z", "--since", NOW),
    ("2026-10-01T00:00:00,5Z", "--since", NOW),
    ("2026-W40-1T00:00:00Z", "--since", NOW),
    ("2026-10-01T000000Z", "--since", NOW),
    ("2026-10-01T00:00:00+0200", "--since", NOW),
    ("2026-10-01T24:00:00Z", "--since", NOW),
    ("2026-10-01T00:00:00 Z", "--since", NOW),
    ("2026-10-01T00:00:00ZZ", "--since", NOW),
    ("2026-10-01T00:00:00+02", "--since", NOW),
    ("2026-10-01T00:00:00\ud800Z", "--since", NOW),
    ("2026-10-01\ud800T00:00:00Z", "--since", NOW),
]

_TS_PY = [
    ("2026-10-01T00:00:00.000000001Z", "--since"),
    ("+002026-10-01T00:00:00Z", "--since"),
]

_SHOWS_PY: list[Any] = [
    {"ts": "2026-01-15T00:00:00z"},
    {"ts": "2026-01-15T00:00:00ZZ"},
    {"ts": "2026-01-15T00:00Z"},
    {"ts": "20260115T000000Z"},
    {"ts": "2026-01-15T00:00:00+00:00:00.000001"},
    {"ts": "2026-01-01T00:00:00.000001+00:00:00.000001"},
]


def generate_python() -> dict[str, bytes]:
    """What follows the release, as CPython 3.13.13 answers it."""
    rows = [*_since_rows(_SINCE_PY), *_bound_rows(_BOUNDS_PY), *_ts_rows(_TS_PY)]
    for show in _SHOWS_PY:
        for start, end in _WINDOWS[:4]:
            rows.append(
                {
                    "k": "window",
                    "show": neutral(show),
                    "start": _fields(start),
                    "end": _fields(end),
                    **_try(lambda: em._show_in_window(show, start, end), lambda r: r),
                }
            )
    header = {"fixture": "eval_model_python", "count": len(rows)}
    return {"eval_model_python.jsonl": jsonl([header, *rows])}


# ---------------------------------------------------------------------------
# unit eval_model-2: the rules, the replayable-audit walk, the widening types
# (``eval_model_rules.jsonl``)
# ---------------------------------------------------------------------------

RULES_SEED = 20261126
RULES_COUNT = 20000
_SINCE_30D = timedelta(days=30)
_RULES_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
_WALK_SINCES: list[timedelta | None] = [None, _SINCE_30D, timedelta(days=1)]


def _all_rules() -> list[em.ThresholdRule]:
    return [*em.THRESHOLD_RULES.values(), *em.WIDENING_RULES.values()]


def _rule_answer(rule: em.ThresholdRule, hits: list[Any], recent: int) -> Any:
    try:
        answer = rule.check(hits, recent)
    except Exception as exc:  # the class and text are the answer
        return _err(exc)
    assert isinstance(answer, bool)
    return answer


def _hit(rel: Any = "high", score: Any = _DROP, **kw: Any) -> dict[str, Any]:
    """A top hit: ``relevance`` and ``score`` (each dropped by `_DROP`),
    then ``kw``."""
    hit: dict[str, Any] = {"id": "m1"}
    for key, value in [("relevance", rel), ("score", score), *kw.items()]:
        if value is not _DROP:
            hit[key] = value
    return hit


_BIG = 2**1024
_SINGLE_SCORES: list[Any] = [
    True,
    False,
    0,
    49,
    50,
    51,
    -50,
    _BIG + 1,
    -_BIG,
    10**400,
    49.999999999999993,
    50.0,
    float("nan"),
    float("inf"),
    float("-inf"),
    -0.0,
    1e308,
    "60",
    None,
    [50],
]

_PAIR_SCORES: list[tuple[Any, Any]] = [
    (100, 50),
    (99, 50),
    (100, 50.0),
    (100.0, 50),
    (99.5, 49.75),
    (99.0, 49.75),
    (2 * _BIG, _BIG),
    (2 * _BIG - 1, _BIG),
    (2**53 + 3, float(2**52 + 2)),
    (2**54 + 1, 2**53 + 1),
    (2**54 + 2, 2**53 + 1),
    (float("inf"), 1e308),
    (1.7976931348623157e308, 1e308),
    (2**1030, 1e308),
    (float("nan"), 1),
    (1, float("nan")),
    (True, 0.5),
    (True, 1),
    (2, True),
    (1, True),
    (60, 0),
    (60, 0.0),
    (60, -0.0),
    (60, False),
    (60, -1),
    (60, -0.5),
    (60, float("-inf")),
    (60, "30"),
    (60, None),
    (60, _DROP),
    (60, [30]),
    (60, 10**400),
    (10**401, 10**400),
    (_DROP, 1),
    ("60", 1),
]

_RECENTS = [0, 1, -1, 10**30, -(10**30)]


def _rule_cases() -> list[tuple[list[Any], int]]:
    cases: list[tuple[list[Any], int]] = []
    for recent in _RECENTS:
        cases.append(([], recent))
        cases.append(([_hit(score=60)], recent))
        cases.append((["junk"], recent))
        cases.append(([_hit("medium", 60, relevance_v2="high")], recent))
    for rel in ["medium", "low", "HIGH", "high ", 5, None, _DROP]:
        cases.append(([_hit(rel, 60)], 0))
    firsts: list[Any] = [None, 5, [_hit(score=60)], ("t",), {}]
    for first in firsts:
        cases.append(([first], 0))
    for score in _SINGLE_SCORES:
        cases.append(([_hit(score=score)], 0))
    for top, second in _PAIR_SCORES:
        cases.append(([_hit(score=top), _hit("low", second)], 0))
    for second in ["junk", None, 7]:
        cases.append(([_hit(score=60), second], 0))
        cases.append(([_hit(score=40), second], 0))
        cases.append(([_hit("low", 60), second], 0))
    cases.append(([_hit(score=100), _hit(score=10), "junk"], 0))
    for rel, rel2 in [
        ("high", "high"),
        ("medium", "high"),
        ("low", "high"),
        ("medium", "medium"),
        ("medium", _DROP),
        (_DROP, "high"),
        ("medium", "HIGH"),
        ("medium", 5),
        ("Medium", "high"),
    ]:
        for recent in [0, 3]:
            cases.append(([_hit(rel, 60, relevance_v2=rel2)], recent))
    return cases


def _rule_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "rule",
            "hits": neutral(hits),
            "recent": str(recent),
            "r": [_rule_answer(rule, hits, recent) for rule in _all_rules()],
        }
        for hits, recent in _rule_cases()
    ]


_AUDIT_BASE: dict[str, Any] = {
    "kind": "turn_audited",
    "ts": "2026-11-20T00:00:00Z",
    "verdict": "miss",
    "top_hits": [{"id": "m1", "relevance": "high", "score": 60}],
    "recent_retrieval_count": 0,
}


class _Ev:
    """An event spelled compactly: `_AUDIT_BASE` with keys set or dropped
    (``w``), or any value (``n``)."""

    def __init__(self, value: Any, changes: dict[str, Any] | None) -> None:
        self.value = value
        self.changes = changes

    def spelled(self) -> dict[str, Any]:
        if self.changes is None:
            return {"n": neutral(self.value)}
        return {
            "w": [
                [key, None if value is _DROP else neutral(value)]
                for key, value in self.changes.items()
            ]
        }


def _a(**kw: Any) -> _Ev:
    ev = dict(_AUDIT_BASE)
    for key, value in kw.items():
        if value is _DROP:
            ev.pop(key, None)
        else:
            ev[key] = value
    return _Ev(ev, kw)


def _n(value: Any) -> _Ev:
    return _Ev(value, None)


def _cut(cutoff_ts: Any, ts: Any = "2026-11-21T00:00:00Z") -> _Ev:
    ev: dict[str, Any] = {"kind": "silent_miss_cutoff", "ts": ts}
    if cutoff_ts is not _DROP:
        ev["cutoff_ts"] = cutoff_ts
    return _n(ev)


_NOV1 = "2026-11-01T00:00:00Z"
_NOV10 = "2026-11-10T00:00:00Z"
_NOV15 = "2026-11-15T00:00:00Z"
_NOV20 = "2026-11-20T00:00:00Z"

_STREAMS: list[tuple[str, list[_Ev]]] = [
    ("empty", []),
    ("non_dicts", [_n(None), _n(5), _n("x"), _n([]), _n(["kind"]), _a(), _n((1,))]),
    (
        "window",
        [
            _a(ts="2026-11-25T00:00:00Z"),
            _a(ts=_NOV1),
            _a(ts="2026-10-26T12:00:00Z"),
            _a(ts="2026-10-26T11:59:59.999999Z"),
            _a(ts="2026-01-01T00:00:00Z"),
            _a(ts="2026-11-25T13:00:00+01:00"),
            _a(ts="2026-10-26T13:00:00+01:00"),
            _a(ts="2026-11-24T12:00:00"),
            _a(ts="not a time"),
            _a(ts=5),
            _a(ts=_DROP),
            _a(ts=None),
            _a(ts="2026-12-01T00:00:00Z"),
        ],
    ),
    (
        "repeat",
        [
            _a(repeat=v)
            for v in [True, 1, "yes", [0], 0.5, False, 0, "", None, [], 0.0, {}]
        ],
    ),
    (
        "verdict",
        [
            _a(verdict="no_signal"),
            _a(verdict="NO_SIGNAL"),
            _a(verdict=_DROP),
            _a(verdict=None),
            _a(verdict=["no_signal"]),
            _a(repeat=True, verdict="no_signal"),
            _a(verdict="no_signal", top_hits=[]),
        ],
    ),
    (
        "top_hits",
        [
            _a(top_hits=v)
            for v in [
                _DROP,
                None,
                [],
                "m1",
                {"id": "m1"},
                ["junk"],
                [None],
                [5],
                [[{}]],
                [{}],
                [{"id": "x"}, "junk"],
                [{"relevance": "low"}],
                ({"id": "t"},),
            ]
        ],
    ),
    (
        "recent",
        [
            _a(recent_retrieval_count=v)
            for v in [
                _DROP,
                True,
                False,
                0,
                3,
                -2,
                2**70,
                -(2**70),
                10**30,
                1.0,
                "3",
                None,
                [1],
            ]
        ],
    ),
    (
        "kinds",
        [
            _n({"kind": "search_miss", "ts": _NOV20}),
            _n({"ts": _NOV20}),
            _n({"kind": 5, "ts": "2025-01-01T00:00:00Z"}),
            _a(kind="TURN_AUDITED"),
            _a(kind=_DROP),
            _n({}),
            _a(),
        ],
    ),
    (
        "cutoff_before",
        [
            _cut("2026-01-01T00:00:00Z", ts="2026-01-01T00:00:00Z"),
            _a(ts=_NOV1),
            _a(ts=_NOV20),
        ],
    ),
    (
        "cutoff_inside",
        [
            _a(ts=_NOV1),
            _a(ts=_NOV10),
            _cut(_NOV10, ts=_NOV15),
            _a(ts=_NOV20),
            _a(ts="junk"),
        ],
    ),
    (
        "cutoff_after",
        [_a(ts=_NOV20), _cut("2026-12-01T00:00:00Z", ts="2025-01-01T00:00:00Z")],
    ),
    (
        "cutoffs_out_of_order",
        [_cut(_NOV15), _a(ts=_NOV10), _a(ts=_NOV20), _cut(_NOV1)],
    ),
    (
        "cutoff_unreadable",
        [
            _cut("junk"),
            _cut(5),
            _cut(_DROP),
            _cut(None),
            _cut([_NOV15]),
            _a(ts=_NOV20),
            _a(ts=5),
        ],
    ),
    (
        "cutoff_offset",
        [
            _cut("2026-11-15T01:00:00+01:00"),
            _a(ts=_NOV15),
            _a(ts="2026-11-14T23:59:59.999999Z"),
            _a(ts="2026-11-15T00:30:00+01:00"),
        ],
    ),
    (
        "cutoff_naive",
        [_cut("2026-11-15T00:00:00"), _a(ts="2026-11-14T23:00:00Z"), _a(ts=_NOV15)],
    ),
    (
        "cutoff_unreadable_rows",
        [
            _cut("2026-01-01T00:00:00Z"),
            _a(ts="junk"),
            _a(ts=_DROP),
            _a(ts=_NOV20),
        ],
    ),
    (
        "cutoff_after_skips",
        [
            _cut(_NOV15),
            _a(ts=_NOV1, repeat=True),
            _a(ts=_NOV1, top_hits=[]),
            _a(ts=_NOV1, verdict="no_signal"),
            _a(ts=_NOV1),
            _a(ts=_NOV1, top_hits=["junk"]),
        ],
    ),
    (
        "stream_order",
        [
            _a(ts=_NOV20, top_hits=[{"id": "a"}]),
            _n({"kind": "turn_audited", "ts": _NOV1, "top_hits": [{"id": "b"}]}),
            _a(ts=_NOV10, top_hits=[{"id": "c"}], recent_retrieval_count=7),
        ],
    ),
]


def _walk_answer(events: list[Any], since: timedelta | None, now: datetime) -> Any:
    def spell(w: Any) -> dict[str, Any]:
        rows = []
        for ev, top_hits, recent in w.rows:
            index = next(i for i, e in enumerate(events) if e is ev)
            assert top_hits is ev["top_hits"]
            rows.append([index, str(recent)])
        assert w.with_features == len(rows)
        return {
            "total": w.total_events_scanned,
            "in_window": w.events_in_window,
            "without": w.without_features,
            "repeats": w.repeats_skipped,
            "retracted": w.cutoff_retracted,
            "rows": rows,
        }

    return _try(
        lambda: em._collect_replayable_audits(events, since=since, now=now), spell
    )


def _span(since: timedelta | None) -> str | None:
    return None if since is None else _us(since)


def _walk_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, stream in _STREAMS:
        events = [ev.value for ev in stream]
        rows.append(
            {
                "k": "walk",
                "name": name,
                "events": [ev.spelled() for ev in stream],
                "r": [_walk_answer(events, s, _RULES_NOW) for s in _WALK_SINCES],
            }
        )
    edge = [_a(), _a(ts="0001-01-01T00:00:00Z"), _a(ts="junk")]
    unread = [_a(ts="junk"), _a(ts=_DROP), _n(5)]
    year1 = datetime.min.replace(tzinfo=UTC)
    late = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    ist = datetime(
        2026, 11, 25, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    naive = datetime(2026, 11, 25, 12, 0)
    one_us = timedelta(microseconds=1)
    cases: list[tuple[list[_Ev], timedelta | None, datetime]] = [
        (edge, _RULES_NOW - year1, _RULES_NOW),
        (edge, _RULES_NOW - year1 + one_us, _RULES_NOW),
        ([], _RULES_NOW - year1 + one_us, _RULES_NOW),
        (edge, timedelta.max, _RULES_NOW),
        (edge, timedelta.min, _RULES_NOW),
        (edge, timedelta(days=-1), _RULES_NOW),
        (edge, timedelta(0), _RULES_NOW),
        (edge, -one_us, late),
        (edge, timedelta(0), late),
        (edge, timedelta(days=1), year1),
        (edge, timedelta(days=1), ist),
        (edge, _SINCE_30D, naive),
        (unread, _SINCE_30D, naive),
        (edge, None, naive),
    ]
    for stream, since, now in cases:
        events = [ev.value for ev in stream]
        rows.append(
            {
                "k": "walk_at",
                "events": [ev.spelled() for ev in stream],
                "since": _span(since),
                "now": _fields(now),
                **_walk_answer(events, since, now),
            }
        )
    return rows


_PROBES: list[Any] = [
    _DROP,
    None,
    "",
    "abc",
    "é\U0001d11e",
    "\ud800x",
    {"preview": "p", "len": 5, "hash": "h"},
    {"preview": 5, "len": True, "hash": None},
    {"len": 2**70},
    {"len": -1},
    {"len": 1.0},
    {"len": "3"},
    {"preview": "\ud800", "hash": "\U0001d11e"},
    {},
    5,
    [],
    ["a"],
    True,
    1.5,
]


def _probe_rows() -> list[dict[str, Any]]:
    rows = []
    for value in _PROBES:
        given = None if value is _DROP else value
        spelled = {} if value is _DROP else {"v": neutral(value)}
        rows.append(
            {
                "k": "probe",
                **spelled,
                "r": [neutral(x) for x in em._probe_query_display(given)],
            }
        )
    return rows


_DESC_W2 = em.WIDENING_RULES["w2_top1_v2_high_from_medium"].description


def _turn(**kw: Any) -> em.WideningFlaggedTurn:
    fields: dict[str, Any] = {
        "ts": None,
        "session_id": None,
        "client_model": None,
        "probe_query_preview": None,
        "probe_query_len": None,
        "probe_query_hash": None,
        "top_hit_id": "?",
        "top_hit_score": None,
        "relevance_v1": None,
        "relevance_v2": None,
        "matched_unique": None,
        "query_unique": None,
        "v1_also_flagged": False,
        "memory_status": "unknown",
        "memory_summary": None,
        "memory_scopes": None,
    }
    fields.update(kw)
    return em.WideningFlaggedTurn(**fields)


_TURN_FULL = _turn(
    ts=_NOV20,
    session_id="s1",
    client_model="model-a",
    probe_query_preview="how do I →",
    probe_query_len=2**70,
    probe_query_hash="ab12",
    top_hit_id="m1",
    top_hit_score=1e308,
    relevance_v1="medium",
    relevance_v2="high",
    matched_unique=-1,
    query_unique=4,
    v1_also_flagged=True,
    memory_status="active",
    memory_summary="a summary",
    memory_scopes=["repo:a", "global"],
)
_TURN_EMPTY = _turn(top_hit_score=-0.0, memory_scopes=[])
_ROLLUPS = [
    em.WideningMemoryRollup("m1", 3, 2, "active", "s", ["a"], 0.5, 60.0),
    em.WideningMemoryRollup("?", 1, 0, "unknown", None, None, None, None),
]


def _fields_of(obj: Any) -> list[Any]:
    """A dataclass's field values in order."""
    return [getattr(obj, f) for f in obj.__dataclass_fields__]


def _dict_rows() -> list[dict[str, Any]]:
    objs: list[tuple[str, Any]] = [
        ("sweep_row", em.ThresholdSweepRow("v1_top1_high", "d", 3, 0, 1.0)),
        ("sweep_row", em.ThresholdSweepRow("w2", _DESC_W2, 0, -3, None)),
        ("sweep_row", em.ThresholdSweepRow("v3", "", 5, 2, 5 / 3)),
        ("turn", _TURN_FULL),
        ("turn", _TURN_EMPTY),
        *[("rollup", r) for r in _ROLLUPS],
        ("detail", em.WideningRuleDetail("w1", "d", 0, 0)),
        (
            "detail",
            em.WideningRuleDetail(
                "w2", _DESC_W2, 2, 1, [_TURN_FULL, _TURN_EMPTY], list(_ROLLUPS)
            ),
        ),
    ]
    rows = []
    for kind, obj in objs:
        values = _fields_of(obj)
        if kind == "detail":
            values[4] = [_fields_of(t) for t in obj.turns]
            values[5] = [_fields_of(m) for m in obj.by_memory]
        rows.append(
            {
                "k": "dict",
                "type": kind,
                "in": neutral(values),
                "r": neutral(obj.to_dict()),
            }
        )
    return rows


# The sweep's tables. A score, a relevance or a second-level value of
# `None` in a table means the key is left out (`_DROP`).
_SW_RELS: list[Any] = [
    _DROP,
    "high",
    "high",
    "high",
    "medium",
    "medium",
    "low",
    "HIGH",
    5,
    None,
]
_SW_SCORES: list[Any] = [
    _DROP,
    None,
    0,
    50,
    49,
    2**53 + 1,
    2**53 + 3,
    2**1030,
    -(2**1030),
    49.999999999999993,
    50.0,
    float("nan"),
    float("inf"),
    float("-inf"),
    -0.0,
    0.0,
    1e308,
    float(2**52 + 2),
    "60",
]
_SW_NON_DICTS: list[Any] = ["junk", None, 5, [], [{"relevance": "high"}]]
_SW_RECENTS: list[int] = [0, 0, 0, 0, 1, -1, 10**30, -(10**30)]


def _sw_spell(table: list[Any]) -> list[Any]:
    return [None if v is _DROP else neutral(v) for v in table]


def draw_score(d: _Draw) -> Any:
    mode = d.pick(4)
    if mode == 0:
        return d.pick(200) - 50
    if mode == 1:
        return (d.pick(1601) - 400) / 8
    if mode == 2:
        return d.pick(2) == 1
    return _SW_SCORES[d.pick(len(_SW_SCORES))]


def draw_hit(d: _Draw) -> Any:
    if d.pick(10) == 0:
        return _SW_NON_DICTS[d.pick(len(_SW_NON_DICTS))]
    hit: dict[str, Any] = {}
    for key in ("relevance", "relevance_v2"):
        value = _SW_RELS[d.pick(len(_SW_RELS))]
        if value is not _DROP:
            hit[key] = value
    score = draw_score(d)
    if score is not _DROP:
        hit["score"] = score
    return hit


def draw_case(d: _Draw) -> tuple[list[Any], int]:
    hits = [draw_hit(d) for _ in range(d.pick(4))]
    return hits, _SW_RECENTS[d.pick(len(_SW_RECENTS))]


def _sweep_word(answer: Any) -> str:
    if answer is True:
        return "1"
    if answer is False:
        return "0"
    return f"{answer['x']}:{answer['msg']}"


def rules_sweep_digest() -> str:
    d = _Draw(RULES_SEED)
    lines = []
    for _ in range(RULES_COUNT):
        hits, recent = draw_case(d)
        words = [_sweep_word(_rule_answer(r, hits, recent)) for r in _all_rules()]
        lines.append(" ".join(words))
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def generate_rules() -> dict[str, bytes]:
    """``eval_model_rules.jsonl``, read in
    ``native/crates/bettermemory-core/src/eval_model/rules.rs``.

    The header holds the two registries (``[key, name, description]``),
    ``audit_base`` (neutral), ``walk_sinces`` (microseconds as text, or
    null) and ``now`` (fields). Then one row per case under ``k``:

    - ``rule``: ``hits`` (neutral), ``recent`` (decimal) and ``r``, the
      answer of each rule in registry order (v1 to v4, w1, w2): a bool or
      the exception (``x``, ``msg``);
    - ``walk``: ``name``, ``events`` and ``r``, `_collect_replayable_audits`
      under each of ``walk_sinces`` at the header's ``now``; ``walk_at``:
      ``events``, ``since``, ``now`` and the answer (``r`` or the
      exception). An event is ``{"n": neutral}`` or ``{"w": [[key,
      neutral or null], ...]}``, ``audit_base`` with each key set in turn
      (an existing key keeps its place, a new one goes last, null drops
      it). An answer is the counters and ``rows``, ``[index, recent]``
      per row, the index of its event in the stream;
    - ``probe``: ``v`` (neutral; absent for a missing key) and ``r``, the
      three values of `_probe_query_display` (neutral);
    - ``dict``: ``type``, ``in`` (the fields in order, neutral; a
      detail's turns and rollups as their fields) and ``r``, `to_dict`
      (neutral).

    The last line is the sweep: ``count`` cases drawn with `_Draw` from
    ``seed`` by `draw_case` over the tables the line holds (null for a
    key left out), the SHA-256 of one line per case of the six answers
    (``1``, ``0`` or ``<class>:<message>``) joined by spaces.

    Not gated: the text is ASCII, the floats' ``repr`` is the same under
    every release, and the timestamps are forms `fromisoformat` reads
    alike under 3.11 to 3.14.
    """
    rows = [*_rule_rows(), *_walk_rows(), *_probe_rows(), *_dict_rows()]
    header = {
        "fixture": "eval_model_rules",
        "count": len(rows),
        "threshold_rules": [
            [k, r.name, r.description] for k, r in em.THRESHOLD_RULES.items()
        ],
        "widening_rules": [
            [k, r.name, r.description] for k, r in em.WIDENING_RULES.items()
        ],
        "audit_base": neutral(_AUDIT_BASE),
        "walk_sinces": [_span(s) for s in _WALK_SINCES],
        "now": _fields(_RULES_NOW),
    }
    sweep = {
        "k": "sweep",
        "seed": RULES_SEED,
        "count": RULES_COUNT,
        "rels": _sw_spell(_SW_RELS),
        "scores": _sw_spell(_SW_SCORES),
        "non_dicts": _sw_spell(_SW_NON_DICTS),
        "recents": [str(r) for r in _SW_RECENTS],
        "sha256": rules_sweep_digest(),
    }
    return {"eval_model_rules.jsonl": jsonl([header, *rows, sweep])}
