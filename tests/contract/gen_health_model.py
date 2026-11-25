"""Fixtures: the report model of health, unit health_model-1
(``health_model.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/health_model.rs``. This pins it to
`health_model`'s `_ENDORSEMENT_GRACE_DAYS`, `_HOOKLESS_REASON`,
`_freshest_touch_ts`, `_has_unresolved_contradiction`, `_is_dead_weight`,
`is_hook_telemetry_event`, `applied_tier`, `endorsement_tier`,
`explicit_ratio`, `_iso` and the types `TelemetryCoverage`, `MemoryStats`
(its three properties), `MarkerStats` (`total`, `override_rate`),
`ScopeHealth` and `ActorSlice`, each with its `to_dict`; and to Python's
``round(x, 3)``, which `MarkerStats.to_dict` calls.

The first line is a header with the count of rows and the two constants.
Then one row per case, its kind under ``k``:

- ``round``: ``x`` (a float, `_common.neutral`) and ``r``, ``round(x, 3)``;
- ``ts``: ``dts``, four datetimes (`_iso`'s fields) or nulls (`created`, `updated`,
  `last_verified_at`, `last_corroborated`), and ``r``,
  `_freshest_touch_ts` of them; every datetime is aware (a naive one's
  timestamp is the machine's local time, which the port declines);
- ``unres``: ``c``, ``u``, ``v`` (contradicted, updated, verified) and
  ``r``, `_has_unresolved_contradiction`, or ``x``, the class raised;
- ``ev``: an event (neutral) and ``hook``, ``tier`` and ``etier``, the
  three predicates' answers (``{"x": class}`` where one raises);
- ``dead``: the keyword arguments of `_is_dead_weight` and ``r``;
- ``ratio``: ``applied``, ``explicit`` and ``r``, `explicit_ratio`;
- ``memory``, ``marker``, ``scope``, ``actor``, ``coverage``: ``in``, the
  constructor's arguments, then ``d``, `to_dict` (neutral) or ``x``; a
  memory row also has ``props`` (the three properties), a marker row
  ``total`` (decimal text) and ``rate`` (`override_rate`).

The last three lines are sweeps, each drawn with `_Draw` from its
``seed``: ``stats`` (``count`` stats, three `MemoryStats` to each
`MarkerStats`, the SHA-256 of their ``json.dumps(to_dict())`` lines, or
the class raised), ``round`` (``count`` floats, each rounded to 3 and to 4
places, the SHA-256 of the lines ``<x bits> <n> <bits> <repr>``) and
``ts`` (``count`` sets of four aware datetimes, the SHA-256 of the
`_freshest_touch_ts` bits).

No answer here depends on the interpreter's release, its Unicode tables or
the platform: integer division, ``round``, ``float.__repr__``,
``datetime.isoformat`` and ``json.dumps`` of ASCII-escaped text spell the
same under 3.11 to 3.14 on every system, so the file is not gated.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from collections.abc import Callable
from functools import partial
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory import health_model as hm
from bettermemory.models import Category

from ._common import jsonl, neutral, text_field

_MASK = (1 << 64) - 1
I64_MAX = (1 << 63) - 1
I64_MIN = -(1 << 63)
STATS_SEED = 20261124
STATS_COUNT = 20000
ROUND_SEED = 7
ROUND_COUNT = 20000
TS_SEED = 99
TS_COUNT = 20000


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


def _f(bits: int) -> float:
    return float(struct.unpack(">d", struct.pack(">Q", bits))[0])


def _bits(x: float) -> int:
    return int(struct.unpack(">Q", struct.pack(">d", x))[0])


def _tz(**kw: Any) -> timezone:
    return timezone(timedelta(**kw))


def _iso(dt: datetime | None) -> list[int | None] | None:
    """A datetime by its fields, ``[year, month, day, hour, minute, second,
    microsecond, offset]``, the offset in microseconds or null when naive.
    Not its isoformat: ``fromisoformat`` reads an offset of minus one
    microsecond as zero."""
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


def _try(fn: Callable[[], Any], spell: Callable[[Any], Any]) -> Any:
    try:
        return spell(fn())
    except Exception as exc:  # the class is the answer
        return {"x": type(exc).__name__}


def _same(r: Any) -> Any:
    return r


def _as_r(r: Any) -> dict[str, Any]:
    return {"r": r}


UTC = timezone.utc
P559 = _tz(hours=23, minutes=59)
M559 = _tz(hours=-23, minutes=-59)
SEC30 = _tz(seconds=30)
MICRO = _tz(microseconds=-1)
D1 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# round(x, 3)
# ---------------------------------------------------------------------------

_ROUND_X = [
    0.0,
    -0.0,
    1 / 3,
    2 / 3,
    1 / 2000,
    3 / 2000,
    0.0005,
    -0.0005,
    0.0015,
    0.0025,
    0.0035,
    # Exact binary ties at three places: half to even.
    0.0625,
    0.1875,
    0.3125,
    2.0625,
    1.0005,
    2.675,
    0.1245,
    0.9995,
    0.5,
    1.0,
    -1.5,
    -0.0001,
    5e-324,
    -5e-324,
    1e-300,
    1e300,
    1.7976931348623157e308,
    123456789.0005,
    4503599627370495.5,
    float("inf"),
    float("-inf"),
    float("nan"),
    (2**53 + 1) / (2**63 - 1),
]
for _k in (1, 3, 5, 7, 999):
    _mid = _k / 2000
    _ROUND_X += [_mid, math.nextafter(_mid, 0.0), math.nextafter(_mid, 1.0)]


def _round_rows() -> list[dict[str, Any]]:
    return [
        {"k": "round", "x": neutral(x), "r": neutral(round(x, 3))} for x in _ROUND_X
    ]


# ---------------------------------------------------------------------------
# timestamps and contradiction
# ---------------------------------------------------------------------------

_TS = [
    (D1, D1, None, None),
    (D1, D1 + timedelta(seconds=1), None, None),
    (D1 + timedelta(seconds=1), D1, None, None),
    (D1, D1, D1 + timedelta(microseconds=1), None),
    (D1, D1, None, D1 + timedelta(days=3)),
    (D1, D1, D1 + timedelta(days=5), D1 + timedelta(days=3)),
    (D1.astimezone(P559), D1.astimezone(M559), None, None),
    (datetime(1, 1, 1, tzinfo=P559), datetime(1, 1, 1, tzinfo=UTC), None, None),
    (datetime(1, 1, 1, tzinfo=M559), datetime(1, 1, 1, tzinfo=P559), None, None),
    (
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=M559),
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=P559),
        None,
        None,
    ),
    (
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        None,
        None,
    ),
    (
        datetime(1970, 1, 1, tzinfo=SEC30),
        datetime(1970, 1, 1, tzinfo=MICRO),
        None,
        None,
    ),
    # Micros past 2**53, where float(us) / 1e6 rounds twice.
    (
        datetime(5530, 3, 3, 8, 26, 42, 691225, tzinfo=UTC),
        datetime(1, 1, 1, tzinfo=UTC),
        None,
        None,
    ),
    (
        datetime(9941, 7, 2, 8, 9, 54, 643946, tzinfo=UTC),
        datetime(1, 1, 1, tzinfo=UTC),
        None,
        None,
    ),
    (
        datetime(2729, 1, 14, 9, 59, 38, 293845, tzinfo=UTC),
        datetime(1, 1, 1, tzinfo=UTC),
        None,
        None,
    ),
    (
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(1, 1, 1, tzinfo=UTC),
        None,
        datetime(9342, 10, 7, 12, 47, 31, 388379, tzinfo=UTC),
    ),
]


def _ts_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "ts",
            "dts": [_iso(d) for d in dts],
            "r": neutral(hm._freshest_touch_ts(*dts)),
        }
        for dts in _TS
    ]


_N1 = datetime(2026, 3, 1, 12, 0)
_UNRES: list[tuple[datetime | None, datetime, datetime | None]] = [
    (None, D1, None),
    (None, _N1, D1),
    (D1 + timedelta(seconds=1), D1, None),
    (D1, D1, None),
    (D1 - timedelta(microseconds=1), D1, None),
    (D1 + timedelta(seconds=2), D1, D1 + timedelta(seconds=1)),
    (D1 + timedelta(seconds=2), D1, D1 + timedelta(seconds=2)),
    (D1 + timedelta(seconds=2), D1, D1 + timedelta(seconds=3)),
    (D1 + timedelta(seconds=2), D1 + timedelta(seconds=3), D1 - timedelta(days=1)),
    # One instant at two offsets is not later.
    (D1.astimezone(P559), D1.astimezone(M559), None),
    (D1.astimezone(M559) + timedelta(microseconds=1), D1.astimezone(P559), None),
    (D1.astimezone(SEC30), D1, D1.astimezone(MICRO)),
    (datetime(1, 1, 1, tzinfo=P559), datetime(1, 1, 1, tzinfo=UTC), None),
    (datetime(1, 1, 1, tzinfo=M559), datetime(1, 1, 1, tzinfo=UTC), None),
    # Naive against naive orders by the wall clock.
    (_N1 + timedelta(seconds=1), _N1, None),
    (_N1, _N1, _N1 - timedelta(seconds=1)),
    (_N1 + timedelta(seconds=1), _N1, _N1 + timedelta(seconds=2)),
    # Naive against aware raises at whichever comparison meets it.
    (D1 + timedelta(seconds=1), _N1, None),
    (_N1, D1, None),
    (D1, D1, _N1),
    (_N1, _N1, D1),
]


def _unres_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "unres",
            "c": _iso(c),
            "u": _iso(u),
            "v": _iso(v),
            **_try(partial(hm._has_unresolved_contradiction, c, u, v), _as_r),
        }
        for c, u, v in _UNRES
    ]


# ---------------------------------------------------------------------------
# event predicates
# ---------------------------------------------------------------------------

_EVENTS: list[Any] = [
    {},
    {"kind": "use"},
    {"kind": "use", "attribution": "hook"},
    {"kind": "use", "attribution": "auto"},
    {"kind": "use", "triggered_from": "stop_hook"},
    {
        "kind": "use",
        "attribution": "auto",
        "triggered_from": "stop_hook",
        "outcome": "applied",
    },
    {"kind": "use", "attribution": "hook", "outcome": "applied"},
    {"kind": "use", "attribution": "hook", "outcome": "applied", "auto": True},
    {"kind": "use", "outcome": "applied", "auto": True},
    {"kind": "use", "outcome": "applied", "auto": 1},
    {"kind": "use", "outcome": "applied", "auto": 1.0},
    {"kind": "use", "outcome": "applied", "auto": "true"},
    {"kind": "use", "outcome": "applied", "auto": "True"},
    {"kind": "use", "outcome": "applied", "auto": None},
    {"kind": "use", "outcome": "applied", "auto": False},
    {"kind": "use", "outcome": "applied", "auto": [True]},
    {"kind": "use", "outcome": "applied", "attribution": None},
    {"kind": "use", "outcome": "applied", "attribution": "model"},
    {"kind": "use", "outcome": "applied", "attribution": "admin"},
    {"kind": "use", "outcome": "applied", "attribution": "Hook"},
    {"kind": "use", "outcome": "applied", "attribution": "hook "},
    {"kind": "use", "outcome": "applied", "attribution": ["hook"]},
    {"kind": "use", "outcome": "ignored", "attribution": "hook"},
    {"kind": "use", "outcome": "contradicted", "auto": True},
    {"kind": "use", "outcome": None},
    {"kind": "use", "outcome": "APPLIED"},
    {"kind": "use", "outcome": 1},
    {"kind": "use", "triggered_from": "mcp_tool"},
    {"kind": "use", "triggered_from": "stop_hook\x00"},
    {"kind": "use", "triggered_from": 1},
    {"kind": "turn_audited"},
    {"kind": "turn_audited", "triggered_from": "stop_hook"},
    {"kind": "turn_audited", "triggered_from": "mcp_tool"},
    {"kind": "turn_audited", "attribution": "hook"},
    {"kind": "auto_consolidate", "triggered_from": "stop_hook"},
    {"kind": "search", "attribution": "hook", "outcome": "applied", "auto": True},
    {"kind": "Use", "attribution": "hook", "outcome": "applied"},
    {"kind": "usé", "attribution": "hook", "outcome": "applied"},
    {"kind": "\ud800", "attribution": "hook"},
    {"kind": None, "triggered_from": "stop_hook"},
    {"kind": 1, "triggered_from": "stop_hook"},
    {"kind": True, "attribution": "hook"},
    {"kind": ["use"], "attribution": "hook"},
    {"attribution": "hook", "outcome": "applied", "auto": True},
    {"auto": True},
    [["kind", "use"]],
    "use",
    None,
    1,
]


def _ev_rows() -> list[dict[str, Any]]:
    rows = []
    for ev in _EVENTS:
        rows.append(
            {
                "k": "ev",
                "ev": neutral(ev),
                "hook": _try(partial(hm.is_hook_telemetry_event, ev), _same),
                "tier": _try(partial(hm.applied_tier, ev), _same),
                "etier": _try(partial(hm.endorsement_tier, ev), _same),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# _is_dead_weight
# ---------------------------------------------------------------------------

_BASE_DEAD: dict[str, Any] = {
    "category": None,
    "freshest_ts": 100.0,
    "retrieval_count": 1,
    "applied_count": 0,
    "has_unresolved_contradiction": False,
    "earliest_retrieval_ts": None,
    "cutoff_ts": 200.0,
    "grace_cutoff_ts": 300.0,
}
_DEAD_CHANGES: list[dict[str, Any]] = [
    {},
    {"category": Category.FACT},
    {"category": Category.USER_INFERENCE},
    {"category": Category.AMBIENT},
    {"freshest_ts": 200.0},
    {"freshest_ts": math.nextafter(200.0, 0.0)},
    {"freshest_ts": 200.5},
    {"freshest_ts": float("nan")},
    {"freshest_ts": float("-inf")},
    {"cutoff_ts": float("nan")},
    {"cutoff_ts": float("inf")},
    {"retrieval_count": 0},
    {"retrieval_count": -1},
    {"retrieval_count": I64_MAX},
    {"applied_count": 1},
    {"applied_count": -1},
    {"applied_count": I64_MAX},
    {"applied_count": I64_MIN},
    {"has_unresolved_contradiction": True},
    {"earliest_retrieval_ts": 300.0},
    {"earliest_retrieval_ts": math.nextafter(300.0, 0.0)},
    {"earliest_retrieval_ts": 299.0},
    {"earliest_retrieval_ts": 301.0},
    {"earliest_retrieval_ts": float("nan")},
    {"earliest_retrieval_ts": -0.0, "grace_cutoff_ts": 0.0},
    {"grace_cutoff_ts": float("nan"), "earliest_retrieval_ts": 1.0},
    {"category": Category.AMBIENT, "retrieval_count": 0},
    {
        "category": Category.FACT,
        "freshest_ts": 199.0,
        "retrieval_count": 5,
        "earliest_retrieval_ts": 250.0,
    },
]


def _dead_rows() -> list[dict[str, Any]]:
    rows = []
    for change in _DEAD_CHANGES:
        kw = {**_BASE_DEAD, **change}
        spelled = {
            "category": kw["category"].value if kw["category"] is not None else None,
            "freshest_ts": neutral(kw["freshest_ts"]),
            "retrieval_count": kw["retrieval_count"],
            "applied_count": kw["applied_count"],
            "has_unresolved_contradiction": kw["has_unresolved_contradiction"],
            "earliest_retrieval_ts": (
                None
                if kw["earliest_retrieval_ts"] is None
                else neutral(kw["earliest_retrieval_ts"])
            ),
            "cutoff_ts": neutral(kw["cutoff_ts"]),
            "grace_cutoff_ts": neutral(kw["grace_cutoff_ts"]),
        }
        rows.append({"k": "dead", "a": spelled, "r": hm._is_dead_weight(**kw)})
    return rows


# ---------------------------------------------------------------------------
# ratios
# ---------------------------------------------------------------------------

_RATIOS = [
    (0, 0),
    (0, 5),
    (-1, 1),
    (I64_MIN, 1),
    (1, 0),
    (1, 1),
    (3, 1),
    (3, 2),
    (2000, 1),
    (2000, 3),
    (7, -1),
    (1, I64_MIN),
    (1, I64_MAX),
    (I64_MAX, 1),
    (I64_MAX, I64_MAX),
    (I64_MAX, I64_MAX - 1),
    (2**53 + 1, 1),
    (3, 2**53 + 1),
    (2**53 + 3, 2**53 + 1),
    (10**18 + 7, 10**18 + 3),
]


def _ratio_rows() -> list[dict[str, Any]]:
    out = []
    for applied, explicit in _RATIOS:
        r = hm.explicit_ratio(applied, explicit)
        out.append(
            {
                "k": "ratio",
                "applied": applied,
                "explicit": explicit,
                "r": None if r is None else neutral(r),
            }
        )
    return out


# ---------------------------------------------------------------------------
# the report types
# ---------------------------------------------------------------------------

_COUNT_FIELDS = [
    "retrieval_count",
    "show_count",
    "applied_count",
    "auto_applied_count",
    "explicit_applied_count",
    "hook_applied_count",
    "model_applied_count",
    "ignored_count",
    "contradicted_count",
    "corrected_count",
]
_DT_FIELDS = [
    "last_used_at",
    "last_contradicted_at",
    "last_verified_at",
    "last_corroborated",
]


def _memory(kw: dict[str, Any]) -> hm.MemoryStats:
    return hm.MemoryStats(**kw)


def _memory_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled: dict[str, Any] = {
        **text_field("id", kw["id"]),
        "scopes": [text_field("v", s) for s in kw["scopes"]],
        **text_field("summary", kw["summary"]),
        "created": _iso(kw["created"]),
        "updated": _iso(kw["updated"]),
    }
    for name in _COUNT_FIELDS:
        if name in kw:
            spelled[name] = kw[name]
    for name in _DT_FIELDS:
        if name in kw:
            spelled[name] = _iso(kw[name])
    if "category" in kw:
        spelled["category"] = None if kw["category"] is None else kw["category"].value
    if "resolution_timeline" in kw:
        spelled["resolution_timeline"] = neutral(kw["resolution_timeline"])
    return spelled


def _opt_float(x: float | None) -> Any:
    return None if x is None else neutral(x)


def _memory_row(kw: dict[str, Any]) -> dict[str, Any]:
    m = _memory(kw)
    return {
        "k": "memory",
        "in": _memory_in(kw),
        "props": {
            "unres": _try(lambda: m.has_unresolved_contradiction, lambda r: r),
            "er": _opt_float(m.endorsement_ratio),
            "mer": _opt_float(m.model_endorsement_ratio),
        },
        "d": _try(m.to_dict, neutral),
    }


_M0: dict[str, Any] = {
    "id": "m-1",
    "scopes": ["global"],
    "summary": "a memory",
    "created": D1,
    "updated": D1,
}
_MEMORIES: list[dict[str, Any]] = [
    _M0,
    {**_M0, "scopes": [], "summary": ""},
    {
        **_M0,
        "id": "m-é\U0001f600",
        "scopes": ["projects:a", "projects:é", "s\ud800"],
        "summary": "café — \ud83d",
        "created": datetime(1, 1, 1, tzinfo=P559),
        "updated": datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=M559),
    },
    {
        **_M0,
        "retrieval_count": 9,
        "show_count": 4,
        "applied_count": 3,
        "auto_applied_count": 1,
        "explicit_applied_count": 2,
        "hook_applied_count": 1,
        "model_applied_count": 1,
        "ignored_count": 2,
        "contradicted_count": 1,
        "corrected_count": 1,
        "last_used_at": D1 + timedelta(days=1),
        "last_contradicted_at": D1 + timedelta(days=2),
        "last_verified_at": D1 + timedelta(days=1),
        "last_corroborated": D1 + timedelta(days=4),
        "category": Category.FACT,
        "resolution_timeline": [
            {"kind": "update", "ts": "2026-03-01T12:00:00Z", "note": None},
            {"kind": "contradicted", "ts": "2026-03-03T12:00:00Z", "note": "stale é"},
        ],
    },
    {
        **_M0,
        "applied_count": 2000,
        "explicit_applied_count": 3,
        "model_applied_count": 1,
    },
    {**_M0, "applied_count": 3, "explicit_applied_count": 2, "model_applied_count": 2},
    {**_M0, "applied_count": -3, "explicit_applied_count": 1, "model_applied_count": 1},
    {**_M0, "applied_count": -3, "explicit_applied_count": 1, "model_applied_count": 0},
    {
        **_M0,
        "applied_count": I64_MAX,
        "explicit_applied_count": I64_MAX - 1,
        "model_applied_count": I64_MIN,
    },
    {
        **_M0,
        "applied_count": I64_MIN,
        "explicit_applied_count": I64_MAX,
        "model_applied_count": I64_MIN,
    },
    {
        **_M0,
        "applied_count": 2**53 + 1,
        "explicit_applied_count": 3,
        "model_applied_count": 2**53,
    },
    {**_M0, "category": Category.AMBIENT, "last_used_at": D1.astimezone(SEC30)},
    {
        **_M0,
        "category": Category.USER_INFERENCE,
        "last_verified_at": D1.astimezone(MICRO),
    },
    {
        **_M0,
        "created": D1.astimezone(SEC30),
        "updated": D1.astimezone(_tz(hours=5, minutes=30)),
    },
    {**_M0, "last_contradicted_at": D1 + timedelta(seconds=1)},
    {**_M0, "last_contradicted_at": D1, "last_verified_at": D1 - timedelta(days=1)},
    {**_M0, "last_contradicted_at": D1.astimezone(P559), "last_verified_at": None},
    {
        **_M0,
        "created": _N1,
        "updated": _N1,
        "last_used_at": _N1,
        "last_verified_at": _N1,
    },
    {**_M0, "updated": _N1, "last_contradicted_at": _N1 + timedelta(seconds=1)},
    {**_M0, "updated": _N1, "last_contradicted_at": D1},
    {**_M0, "last_contradicted_at": D1, "last_verified_at": _N1},
    {
        **_M0,
        "category": None,
        "resolution_timeline": [{"z": 1, "a": [1.5, None, True]}, "loose"],
    },
]


def _memory_rows() -> list[dict[str, Any]]:
    return [_memory_row(kw) for kw in _MEMORIES]


_MARKERS = [
    ("m", 0, 0),
    ("m", 1, 0),
    ("m", 0, 1),
    ("m", 1, 1),
    ("m", 2, 1),
    ("m", 1, 2),
    ("m", 1999, 1),
    ("m", 15, 1),
    ("m", 13, 3),
    ("m", 1997, 3),
    ("m", 1998, 2),
    ("m", 3, -1),
    ("m", -5, 1),
    ("m", -1, 1),
    ("m", 5, -6),
    ("sha é \ud800", 7, 9),
    ("m", I64_MAX, I64_MAX),
    ("m", I64_MAX, 1),
    ("m", 0, I64_MAX),
    ("m", I64_MIN, I64_MAX),
    ("m", 2**53 + 1, 2**53 - 1),
    ("m", 666, 1333),
]


def _marker_rows() -> list[dict[str, Any]]:
    rows = []
    for marker, fire, over in _MARKERS:
        s = hm.MarkerStats(marker=marker, fire_count=fire, override_count=over)
        rows.append(
            {
                "k": "marker",
                "in": {
                    **text_field("marker", marker),
                    "fire_count": fire,
                    "override_count": over,
                },
                "total": str(s.total),
                "rate": neutral(s.override_rate),
                "d": neutral(s.to_dict()),
            }
        )
    return rows


def _scope_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"scope": "global"},
        {
            "scope": "projects:é\ud800",
            "active": 6,
            "dead": 4,
            "cold": 1,
            "contradicted": 2,
            "applied_total": 30,
        },
        {
            "scope": "",
            "active": I64_MAX,
            "dead": I64_MIN,
            "cold": -1,
            "contradicted": 0,
            "applied_total": I64_MAX,
        },
    ]
    rows = []
    for kw in cases:
        ints = {k: v for k, v in kw.items() if k != "scope"}
        rows.append(
            {
                "k": "scope",
                "in": {**text_field("scope", kw["scope"]), **ints},
                "d": neutral(hm.ScopeHealth(**kw).to_dict()),
            }
        )
    return rows


def _actor_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"client": None},
        {"client": "claude-code"},
        {
            "client": "cé\ud800",
            "memories": 7,
            "memories_in_window": 2,
            "events": 40,
            "searches": 11,
            "applies": 5,
            "models": {"z-model": 3, "a-model": 1, "é": 2},
            "principals": {"p2": 1, "p1": I64_MAX},
        },
        {"client": "", "memories": I64_MIN, "models": {}, "principals": {"": 0}},
    ]
    rows = []
    for kw in cases:
        spelled: dict[str, Any] = (
            {"client": None}
            if kw["client"] is None
            else text_field("client", kw["client"])
        )
        for k, v in kw.items():
            if k in ("models", "principals"):
                spelled[k] = [[text_field("v", key), n] for key, n in v.items()]
            elif k != "client":
                spelled[k] = v
        rows.append(
            {"k": "actor", "in": spelled, "d": neutral(hm.ActorSlice(**kw).to_dict())}
        )
    return rows


def _coverage_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"hook_telemetry_events": 0, "covered": False, "dead_weight_suppressed": True},
        {"hook_telemetry_events": 12, "covered": True, "dead_weight_suppressed": False},
        {
            "hook_telemetry_events": 0,
            "covered": False,
            "dead_weight_suppressed": True,
            "cold_endorsement_suppressed": True,
            "reason": hm._HOOKLESS_REASON,
        },
        {
            "hook_telemetry_events": I64_MAX,
            "covered": True,
            "dead_weight_suppressed": True,
            "cold_endorsement_suppressed": False,
            "reason": "\ud800",
        },
    ]
    rows = []
    for kw in cases:
        spelled = {k: v for k, v in kw.items() if k != "reason"}
        if "reason" in kw:
            spelled.update(text_field("reason", kw["reason"]))
        rows.append(
            {
                "k": "coverage",
                "in": spelled,
                "d": neutral(hm.TelemetryCoverage(**kw).to_dict()),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# sweeps
# ---------------------------------------------------------------------------

_BIG = [2**53 + 1, I64_MAX, -1, I64_MIN, 2**53, 1999, 3]
_TEXTS = [
    "",
    "a",
    "global",
    "projects:x",
    "café",
    "—",
    "\U0001f600",
    "s\ud800",
    'q"\\\n',
]
_KINDS = ["update", "verify", "contradicted", "corrected"]


def draw_dt(d: _Draw, naive_ok: bool) -> datetime:
    """A datetime of years 1 to 9999: the fields in order, then its zone
    by ``pick(8)``: 0 naive (UTC where a naive one is not wanted), 1 UTC,
    2 to 5 whole minutes ``pick(2879) - 1439``, 6 seconds
    ``pick(172799) - 86399``, 7 microseconds ``pick(2**31) - 2**30``."""
    year = 1 + d.pick(9999)
    month = 1 + d.pick(12)
    day = 1 + d.pick(28)
    hour = d.pick(24)
    minute = d.pick(60)
    second = d.pick(60)
    micro = d.pick(1000000) if d.pick(2) else 0
    zone = d.pick(8)
    tz: timezone | None
    if zone == 0:
        tz = None if naive_ok else UTC
    elif zone == 1:
        tz = UTC
    elif zone <= 5:
        tz = _tz(minutes=d.pick(2879) - 1439)
    elif zone == 6:
        tz = _tz(seconds=d.pick(172799) - 86399)
    else:
        tz = _tz(microseconds=d.pick(2**31) - 2**30)
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)


def draw_count(d: _Draw) -> int:
    """``pick(6)`` when ``pick(4)`` is not zero, else one of `_BIG`."""
    if d.pick(4):
        return d.pick(6)
    return _BIG[d.pick(len(_BIG))]


def draw_opt_dt(d: _Draw, naive_ok: bool) -> datetime | None:
    if d.pick(3) == 0:
        return None
    return draw_dt(d, naive_ok)


def draw_memory(d: _Draw, i: int) -> hm.MemoryStats:
    """A `MemoryStats` drawn field by field in declaration order; naive
    datetimes in about one draw in sixteen, so some rows raise."""
    naive = d.pick(16) == 0
    ident = f"m{i}"
    scopes = [_TEXTS[d.pick(len(_TEXTS))] for _ in range(d.pick(4))]
    summary = _TEXTS[d.pick(len(_TEXTS))]
    created = draw_dt(d, naive)
    updated = draw_dt(d, naive)
    counts = [draw_count(d) for _ in _COUNT_FIELDS]
    dts = [draw_opt_dt(d, naive) for _ in _DT_FIELDS]
    cat_i = d.pick(4)
    category = None if cat_i == 0 else list(Category)[cat_i - 1]
    timeline = []
    for _ in range(d.pick(3)):
        kind = _KINDS[d.pick(4)]
        ts = draw_dt(d, False).isoformat()
        note = None if d.pick(2) else _TEXTS[d.pick(len(_TEXTS))]
        timeline.append({"kind": kind, "ts": ts, "note": note})
    kw: dict[str, Any] = {
        "id": ident,
        "scopes": scopes,
        "summary": summary,
        "created": created,
        "updated": updated,
        **dict(zip(_COUNT_FIELDS, counts, strict=True)),
        **dict(zip(_DT_FIELDS, dts, strict=True)),
        "category": category,
        "resolution_timeline": timeline,
    }
    return hm.MemoryStats(**kw)


def draw_marker(d: _Draw) -> hm.MarkerStats:
    marker = _TEXTS[d.pick(len(_TEXTS))]
    fire = draw_count(d)
    over = draw_count(d)
    return hm.MarkerStats(marker=marker, fire_count=fire, override_count=over)


def stats_digest() -> str:
    d = _Draw(STATS_SEED)
    lines = []
    for i in range(STATS_COUNT):
        stats: Any = draw_marker(d) if i % 4 == 3 else draw_memory(d, i)
        try:
            lines.append(json.dumps(stats.to_dict()))
        except TypeError:
            lines.append("TypeError")
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def draw_float(d: _Draw) -> float:
    """By ``pick(4)``: 0 any bit pattern (three draws, ``a << 33 | b << 2 |
    c & 3``); 1 a ratio ``pick(5000) / (1 + pick(5000))``; 2 a midpoint
    ``(2 * pick(1000) + 1) / 2000`` moved ``pick(3) - 1`` units in the last
    place; 3 ``(pick(2**31) - 2**30) / 2**pick(40)``."""
    kind = d.pick(4)
    if kind == 0:
        a = d.nxt()
        b = d.nxt()
        c = d.nxt()
        return _f(((a << 33) | (b << 2) | (c & 3)) & _MASK)
    if kind == 1:
        n = d.pick(5000)
        return n / (1 + d.pick(5000))
    if kind == 2:
        mid = (2 * d.pick(1000) + 1) / 2000
        step = d.pick(3) - 1
        return _f(_bits(mid) + step)
    n = d.pick(2**31) - 2**30
    return n / 2 ** d.pick(40)


def round_digest() -> str:
    d = _Draw(ROUND_SEED)
    lines = []
    for _ in range(ROUND_COUNT):
        x = draw_float(d)
        for n in (3, 4):
            r = round(x, n)
            lines.append(f"{_bits(x):016x} {n} {_bits(r):016x} {r!r}")
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def ts_digest() -> str:
    d = _Draw(TS_SEED)
    lines = []
    for _ in range(TS_COUNT):
        created = draw_dt(d, False)
        updated = draw_dt(d, False)
        verified = draw_opt_dt(d, False)
        corroborated = draw_opt_dt(d, False)
        ts = hm._freshest_touch_ts(created, updated, verified, corroborated)
        lines.append(f"{_bits(ts):016x}")
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def generate() -> dict[str, bytes]:
    rows = [
        *_round_rows(),
        *_ts_rows(),
        *_unres_rows(),
        *_ev_rows(),
        *_dead_rows(),
        *_ratio_rows(),
        *_memory_rows(),
        *_marker_rows(),
        *_scope_rows(),
        *_actor_rows(),
        *_coverage_rows(),
    ]
    header = {
        "fixture": "health_model",
        "count": len(rows),
        "grace_days": hm._ENDORSEMENT_GRACE_DAYS,
        **text_field("hookless_reason", hm._HOOKLESS_REASON),
    }
    sweeps = [
        {
            "k": "stats",
            "seed": STATS_SEED,
            "count": STATS_COUNT,
            "sha256": stats_digest(),
        },
        {
            "k": "round",
            "seed": ROUND_SEED,
            "count": ROUND_COUNT,
            "sha256": round_digest(),
        },
        {"k": "ts", "seed": TS_SEED, "count": TS_COUNT, "sha256": ts_digest()},
    ]
    return {"health_model.jsonl": jsonl([header, *rows, *sweeps])}
