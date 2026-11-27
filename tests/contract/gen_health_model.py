"""Fixtures: the report model of health, unit health_model-1
(``health_model.jsonl``), unit health_model-2
(``health_model_debt.jsonl``, `generate_debt`) and unit health_model-3
(``health_model_report.jsonl``, `generate_report`), the last two described
where they begin below.

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
from bettermemory.origin_history import HistoryUnread
from bettermemory.store import EpisodeVolume

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


# ---------------------------------------------------------------------------
# unit health_model-2 (``health_model_debt.jsonl``)
# ---------------------------------------------------------------------------
#
# Each report type of the unit built from chosen values with its
# ``to_dict`` (``d``, neutral, or ``x``, the class raised),
# `clamp_ratio_threshold`, `weakly_endorsed` with `_is_weakly_endorsed`,
# `unread_fields` and `_unread_dict`; then two sweeps drawn with `_Draw`:
# ``rollup`` (``count`` report values, the kind by ``pick(9)``, the SHA-256
# of their ``json.dumps(to_dict())`` lines or the class raised) and
# ``pred`` (``count`` triples through `clamp_ratio_threshold` and
# `weakly_endorsed`). Not gated, for unit 1's reasons: no answer follows
# the release, the Unicode tables or the platform.

DEBT_ROLLUP_SEED = 20261125
DEBT_ROLLUP_COUNT = 20000
DEBT_PRED_SEED = 31
DEBT_PRED_COUNT = 20000
_CAUSE_VALUES = [
    "timeout",
    "over_budget",
    "shallow",
    "error",
    "unconfirmed_cut",
    "call_budget",
]


def _opt_text(name: str, text: str | None) -> dict[str, Any]:
    return {name: None} if text is None else text_field(name, text)


def _texts(items: list[str]) -> list[dict[str, Any]]:
    return [text_field("v", s) for s in items]


def _pairs(items: dict[str, int]) -> list[list[Any]]:
    return [[text_field("v", k), n] for k, n in items.items()]


def _clamp_rows() -> list[dict[str, Any]]:
    xs = [
        -math.inf,
        -1.0,
        -0.0,
        0.0,
        5e-324,
        -5e-324,
        0.5,
        _f(_bits(1.0) - 1),
        1.0,
        1.5,
        math.inf,
        math.nan,
        _f(0xFFF8000000000000),
        _f(0x7FF0000000000001),
    ]
    return [
        {"k": "clamp", "x": neutral(x), "r": neutral(hm.clamp_ratio_threshold(x))}
        for x in xs
    ]


_WEAK = [
    (0, 0, 0.0),
    (0, 0, 0.5),
    (0, 3, 0.5),
    (-1, 0, 0.5),
    (-5, -1, 0.5),
    (I64_MIN, 0, 0.5),
    (1, 0, 0.0),
    (1, 0, math.nan),
    (5, 0, -1.0),
    (I64_MAX, 0, -math.inf),
    (3, 1, 0.0),
    (3, 1, -0.0),
    (3, 1, math.nan),
    (3, 1, -math.inf),
    (3, 1, 5e-324),
    (3, 1, 0.5),
    (2, 1, 0.5),
    (4, 1, 0.25),
    (10, 1, 0.1),
    (3, 1, 1 / 3),
    (3, 1, math.nextafter(1 / 3, 1.0)),
    (3, 1, 1.0),
    (3, 3, 1.0),
    (3, 3, math.inf),
    (3, 4, 2.0),
    (1, -1, 0.5),
    (1, I64_MIN, 0.5),
    (I64_MAX, 1, 5e-324),
    (I64_MAX, 1, 1e-18),
    (I64_MAX, I64_MAX, 1.0),
    (I64_MAX, I64_MAX - 1, 1.0),
    (2**53 + 1, 2**53, 1.0),
]


def _weak_rows() -> list[dict[str, Any]]:
    rows = []
    for applied, explicit, t in _WEAK:
        stats = hm.MemoryStats(
            id="m",
            scopes=[],
            summary="",
            created=D1,
            updated=D1,
            applied_count=applied,
            explicit_applied_count=explicit,
        )
        rows.append(
            {
                "k": "weak",
                "applied": applied,
                "explicit": explicit,
                "t": neutral(t),
                "r": hm.weakly_endorsed(applied, explicit, t),
                "rs": hm._is_weakly_endorsed(stats, t),
            }
        )
    return rows


def _unread_map(n: int, causes: list[str]) -> dict[str, HistoryUnread]:
    """``n`` ids counting down (insertion order is not sorted order), the
    causes taken in turn from ``causes``."""
    return {f"id-{n - j:02d}": HistoryUnread(causes[j % len(causes)]) for j in range(n)}


def _unread_cases() -> list[dict[str, HistoryUnread]]:
    mixed = [
        "timeout",
        "error",
        "call_budget",
        "shallow",
        "unconfirmed_cut",
        "over_budget",
    ]
    odd = {
        "z\ud800": HistoryUnread("shallow"),
        "é": HistoryUnread("error"),
        "": HistoryUnread("shallow"),
        "a": HistoryUnread("call_budget"),
    }
    return [
        _unread_map(0, mixed),
        _unread_map(1, ["unconfirmed_cut"]),
        _unread_map(20, mixed),
        _unread_map(21, ["shallow", "error"]),
        _unread_map(25, mixed),
        _unread_map(25, ["timeout"]),
        odd,
    ]


def _unread_rows() -> list[dict[str, Any]]:
    rows = []
    for m in _unread_cases():
        rows.append(
            {
                "k": "unread",
                "in": [[text_field("v", k), why.value] for k, why in m.items()],
                "d": neutral(hm.unread_fields(m)),
            }
        )
    return rows


_IDS25 = [f"id-{25 - j}" for j in range(25)]
_UNREAD_DICT = [
    (0, {"timeout": 1}, ["x"]),
    (0, {}, []),
    (1, {}, []),
    (-1, {"z": 1, "a": 2}, ["b", "a"]),
    (3, {"timeout": 2, "error": 1}, _IDS25),
    (I64_MIN, {"é\ud800": I64_MAX}, ["\ud800"]),
    (I64_MAX, {"shallow": 0, "call_budget": -1}, []),
]


def _unread_dict_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "unread_dict",
            "unread": n,
            "causes": _pairs(causes),
            "ids": _texts(ids),
            "d": neutral(hm._unread_dict(n, causes, ids)),
        }
        for n, causes, ids in _UNREAD_DICT
    ]


def _actor_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled: dict[str, Any] = _opt_text("client", kw.get("client"))
    for k, v in kw.items():
        if k in ("models", "principals"):
            spelled[k] = _pairs(v)
        elif k != "client":
            spelled[k] = v
    return spelled


def _actors_rows() -> list[dict[str, Any]]:
    a1 = {"client": "claude-code", "memories": 3, "events": 9, "models": {"m": 2}}
    a2 = {"client": "c\ud800", "applies": I64_MIN, "principals": {"z": 1, "a": 2}}
    und = {"client": None, "memories": 40, "memories_in_window": 2, "searches": I64_MAX}
    cases: list[dict[str, Any]] = [
        {},
        {"declared": [a1]},
        {"declared": [a2, a1], "undeclared": und},
        {"undeclared": {"client": "named"}},
    ]
    rows = []
    for c in cases:
        kw: dict[str, Any] = {}
        spelled: dict[str, Any] = {}
        if "declared" in c:
            kw["declared"] = [hm.ActorSlice(**a) for a in c["declared"]]
            spelled["declared"] = [_actor_in(a) for a in c["declared"]]
        if "undeclared" in c:
            kw["undeclared"] = hm.ActorSlice(**c["undeclared"])
            spelled["undeclared"] = _actor_in(c["undeclared"])
        rows.append(
            {"k": "actors", "in": spelled, "d": neutral(hm.ActorSlices(**kw).to_dict())}
        )
    return rows


_RAISES = {**_M0, "id": "m-raise", "last_contradicted_at": D1, "last_verified_at": _N1}
_FULL = _MEMORIES[3]
_UNICODE = _MEMORIES[2]


def _stats_kw(rows: list[dict[str, Any]]) -> tuple[list[hm.MemoryStats], list[Any]]:
    return [_memory(kw) for kw in rows], [_memory_in(kw) for kw in rows]


def _vdebt_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"stale_after_days": 30},
        {
            "stale_after_days": 90,
            "never_verified": [_M0, _UNICODE],
            "never_verified_total": 500,
            "stale": [_FULL],
            "stale_total": 7,
            "fresh_count": 12,
            "never_verified_checkable": 300,
            "stale_checkable": 2,
        },
        {"stale_after_days": -1, "never_verified": [_M0, _RAISES], "stale": [_FULL]},
        {"stale_after_days": 0, "never_verified": [_M0], "stale": [_FULL, _RAISES]},
        {
            "stale_after_days": I64_MIN,
            "never_verified_total": I64_MAX,
            "stale_total": I64_MIN,
            "fresh_count": -1,
            "never_verified_checkable": I64_MAX,
            "stale_checkable": I64_MIN,
        },
    ]
    rows = []
    for c in cases:
        kw = dict(c)
        spelled = dict(c)
        for name in ("never_verified", "stale"):
            if name in c:
                kw[name], spelled[name] = _stats_kw(c[name])
        debt = hm.VerificationDebt(**kw)
        rows.append({"k": "vdebt", "in": spelled, "d": _try(debt.to_dict, neutral)})
    return rows


def _drow_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled = {
        **text_field("id", kw["id"]),
        "scopes": _texts(kw["scopes"]),
        **text_field("summary", kw["summary"]),
        "last_verified_at": _iso(kw["last_verified_at"]),
        "commits_since_verify": kw["commits_since_verify"],
    }
    if "basis" in kw:
        spelled.update(text_field("basis", kw["basis"]))
    return spelled


_R0: dict[str, Any] = {
    "id": "m-1",
    "scopes": ["projects:a"],
    "summary": "a drifted memory",
    "last_verified_at": D1,
    "commits_since_verify": 12,
}
_DROWS: list[dict[str, Any]] = [
    _R0,
    {**_R0, "last_verified_at": None, "commits_since_verify": 0},
    {**_R0, "basis": "reachability"},
    {
        **_R0,
        "id": "m-é\ud800",
        "scopes": [],
        "summary": "\ud83d",
        "last_verified_at": D1.astimezone(SEC30),
        "basis": "",
    },
    {**_R0, "last_verified_at": _N1, "commits_since_verify": -1, "basis": "b\ud800"},
    {**_R0, "last_verified_at": D1.astimezone(MICRO), "commits_since_verify": I64_MAX},
    {
        **_R0,
        "last_verified_at": datetime(1, 1, 1, tzinfo=P559),
        "commits_since_verify": I64_MIN,
    },
]


def _drow_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "drow",
            "in": _drow_in(kw),
            "d": neutral(hm.CommitDriftRow(**kw).to_dict()),
        }
        for kw in _DROWS
    ]


def _debt_in(c: dict[str, Any]) -> dict[str, Any]:
    spelled: dict[str, Any] = {}
    for k, v in c.items():
        if k in ("current_repo", "current_cwd", "repo", "worktree_root"):
            spelled.update(_opt_text(k, v))
        elif k == "rows":
            spelled[k] = [_drow_in(r) for r in v]
        elif k == "unread_causes":
            spelled[k] = _pairs(v)
        elif k == "unread_ids":
            spelled[k] = _texts(v)
        elif k == "last_checked_at":
            spelled[k] = _iso(v)
        else:
            spelled[k] = v
    return spelled


def _with_rows(c: dict[str, Any]) -> dict[str, Any]:
    kw = dict(c)
    if "rows" in kw:
        kw["rows"] = [hm.CommitDriftRow(**r) for r in kw["rows"]]
    return kw


def _ddebt_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"current_repo": None, "current_cwd": None},
        {
            "current_repo": "github.com/a/b",
            "current_cwd": "/w/b",
            "rows": [_R0, _DROWS[2]],
            "total_drifted": 40,
        },
        {"current_repo": "r", "current_cwd": None, "unconfirmed": 3},
        {"current_repo": "r", "current_cwd": "c", "unconfirmed": -1},
        {
            "current_repo": None,
            "current_cwd": "c\ud800",
            "unread": 0,
            "unread_causes": {"timeout": 2},
            "unread_ids": ["x"],
        },
        {
            "current_repo": "r",
            "current_cwd": "c",
            "unconfirmed": 2,
            "unread": 2,
            "unread_causes": {"timeout": 1, "error": 1},
            "unread_ids": ["b", "a"],
        },
        {"current_repo": "r", "current_cwd": "c", "unread": -2},
        {
            "current_repo": "r",
            "current_cwd": "c",
            "total_drifted": I64_MIN,
            "unconfirmed": I64_MAX,
            "unread": I64_MAX,
            "unread_causes": {"z": 1, "a": I64_MIN},
            "unread_ids": _IDS25,
        },
        {
            "current_repo": "r",
            "current_cwd": "c",
            **hm.unread_fields(_unread_map(25, ["error", "timeout"])),
        },
    ]
    return [
        {
            "k": "ddebt",
            "in": _debt_in(c),
            "d": neutral(hm.CommitDriftDebt(**_with_rows(c)).to_dict()),
        }
        for c in cases
    ]


def _group_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"repo": "github.com/a/b", "worktree_root": "/w/b"},
        {
            "repo": "r\ud800",
            "worktree_root": "",
            "rows": [_R0, _DROWS[3]],
            "total_drifted": 2,
            "candidates": 9,
            "last_checked_at": D1,
        },
        {
            "repo": "r",
            "worktree_root": "w",
            "candidates": -1,
            "unread": 1,
            "unread_causes": {"shallow": 1},
            "unread_ids": ["m-9"],
            "last_checked_at": _N1,
        },
        {
            "repo": "r",
            "worktree_root": "w",
            "unread": 0,
            "unread_causes": {"shallow": 1},
            "last_checked_at": D1.astimezone(SEC30),
        },
        {
            "repo": "r",
            "worktree_root": "w",
            "total_drifted": I64_MAX,
            "candidates": I64_MIN,
            "unread": -5,
            "last_checked_at": datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=M559),
        },
    ]
    return [
        {
            "k": "group",
            "in": _debt_in(c),
            "d": neutral(hm.CrossRepoDriftGroup(**_with_rows(c)).to_dict()),
        }
        for c in cases
    ]


def _cross_rows() -> list[dict[str, Any]]:
    g1 = {"repo": "a", "worktree_root": "/w/a", "rows": [_R0], "total_drifted": 1}
    g2 = {
        "repo": "b",
        "worktree_root": "/w/b",
        "unread": 1,
        "unread_causes": {"error": 1},
    }
    cases: list[dict[str, Any]] = [
        {},
        {"groups": [g1, g2], "total_drifted": 1, "unanchorable": 4},
        {
            "skipped": [
                {"repo": "c", "reason": "missing"},
                {"z": [1, 2.5, None], "a": {"n": True}},
                {},
            ],
            "total_drifted": I64_MIN,
            "unanchorable": I64_MAX,
        },
    ]
    rows = []
    for c in cases:
        kw = dict(c)
        spelled = dict(c)
        if "groups" in c:
            kw["groups"] = [
                hm.CrossRepoDriftGroup(**_with_rows(g)) for g in c["groups"]
            ]
            spelled["groups"] = [_debt_in(g) for g in c["groups"]]
        if "skipped" in c:
            spelled["skipped"] = neutral(c["skipped"])
        rows.append(
            {
                "k": "cross",
                "in": spelled,
                "d": neutral(hm.CrossRepoDrift(**kw).to_dict()),
            }
        )
    return rows


def _cold_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {"min_retrievals": hm._COLD_ENDORSEMENT_MIN_RETRIEVALS},
        {"min_retrievals": 5, "rows": [_FULL, _UNICODE], "total": 300},
        {"min_retrievals": 30, "rows": [_M0, _RAISES, _FULL], "total": 3},
        {"min_retrievals": I64_MIN, "total": I64_MAX},
    ]
    rows = []
    for c in cases:
        kw = dict(c)
        spelled = dict(c)
        if "rows" in c:
            kw["rows"], spelled["rows"] = _stats_kw(c["rows"])
        cold = hm.ColdEndorsementMemories(**kw)
        rows.append({"k": "cold", "in": spelled, "d": _try(cold.to_dict, neutral)})
    return rows


def _silent_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = [
        {},
        {
            "audited_total": 40,
            "miss_total": 9,
            "unique_miss_memories": 1,
            "no_signal_total": 3,
        },
        {
            "audited_total": I64_MIN,
            "miss_total": I64_MAX,
            "unique_miss_memories": -1,
            "no_signal_total": 0,
        },
    ]
    return [
        {"k": "silent", "in": c, "d": neutral(hm.SilentMissStats(**c).to_dict())}
        for c in cases
    ]


def _recent_rows() -> list[dict[str, Any]]:
    cases: list[dict[str, str | None]] = [
        {"event_id": None, "top_hit_id": None, "query_preview": None, "ts": None},
        {
            "event_id": "01J0000000000000000000000",
            "top_hit_id": "m-1",
            "query_preview": "how do I <redacted>",
            "ts": "2026-03-01T12:00:00Z",
        },
        {"event_id": "", "top_hit_id": "\ud800", "query_preview": "é—", "ts": None},
    ]
    rows = []
    for c in cases:
        spelled: dict[str, Any] = {}
        for k, v in c.items():
            spelled.update(_opt_text(k, v))
        rows.append(
            {
                "k": "recent",
                "in": spelled,
                "d": neutral(hm.RecentSilentMiss(**c).to_dict()),
            }
        )
    return rows


# The sweeps. Every draw below is made in the order the statements read.


def _draw_text(d: _Draw) -> str:
    return _TEXTS[d.pick(len(_TEXTS))]


def _draw_opt_text(d: _Draw) -> str | None:
    if d.pick(3) == 0:
        return None
    return _draw_text(d)


def _draw_census(d: _Draw, names: list[str]) -> dict[str, int]:
    """``pick(3)`` distinct keys of ``names`` from ``pick(len(names))`` on,
    each with a `draw_count`."""
    n = d.pick(3)
    start = d.pick(len(names))
    out = {}
    for j in range(n):
        out[names[(start + j) % len(names)]] = draw_count(d)
    return out


def _draw_actor(d: _Draw, undeclared: bool) -> hm.ActorSlice:
    client = None if undeclared else _draw_opt_text(d)
    memories = draw_count(d)
    in_window = draw_count(d)
    events = draw_count(d)
    searches = draw_count(d)
    applies = draw_count(d)
    models = _draw_census(d, _TEXTS)
    principals = _draw_census(d, _TEXTS)
    return hm.ActorSlice(
        client=client,
        memories=memories,
        memories_in_window=in_window,
        events=events,
        searches=searches,
        applies=applies,
        models=models,
        principals=principals,
    )


def _draw_drift_row(d: _Draw) -> hm.CommitDriftRow:
    ident = _draw_text(d)
    scopes = [_draw_text(d) for _ in range(d.pick(3))]
    summary = _draw_text(d)
    verified = draw_opt_dt(d, True)
    commits = draw_count(d)
    basis = d.pick(3)
    row = hm.CommitDriftRow(
        id=ident,
        scopes=scopes,
        summary=summary,
        last_verified_at=verified,
        commits_since_verify=commits,
    )
    if basis == 1:
        row.basis = "reachability"
    elif basis == 2:
        row.basis = _draw_text(d)
    return row


def _draw_unread(d: _Draw) -> dict[str, Any]:
    """By ``pick(2)``: 1 `unread_fields` of ``pick(26)`` ids counting down,
    each cause ``pick(6)``; 0 the three fields drawn apart (a count, a
    census of causes, ``pick(3)`` texts)."""
    if d.pick(2):
        n = d.pick(26)
        m = {}
        for j in range(n):
            m[f"u{n - j}"] = HistoryUnread(_CAUSE_VALUES[d.pick(6)])
        return hm.unread_fields(m)
    unread = draw_count(d)
    causes = _draw_census(d, _CAUSE_VALUES)
    ids = [_draw_text(d) for _ in range(d.pick(3))]
    return {"unread": unread, "unread_causes": causes, "unread_ids": ids}


def _draw_group(d: _Draw) -> hm.CrossRepoDriftGroup:
    repo = _draw_text(d)
    root = _draw_text(d)
    rows = [_draw_drift_row(d) for _ in range(d.pick(3))]
    total = draw_count(d)
    candidates = draw_count(d)
    unread = _draw_unread(d)
    checked = draw_opt_dt(d, True)
    return hm.CrossRepoDriftGroup(
        repo=repo,
        worktree_root=root,
        rows=rows,
        total_drifted=total,
        candidates=candidates,
        last_checked_at=checked,
        **unread,
    )


def draw_rollup(d: _Draw, i: int) -> Any:
    """One report value of the kind ``pick(9)``: 0 `ActorSlices`, 1
    `VerificationDebt`, 2 `CommitDriftRow`, 3 `CommitDriftDebt`, 4
    `CrossRepoDriftGroup`, 5 `CrossRepoDrift`, 6
    `ColdEndorsementMemories`, 7 `SilentMissStats`, 8 `RecentSilentMiss`.
    The `MemoryStats` rows are unit 1's `draw_memory`."""
    kind = d.pick(9)
    if kind == 0:
        declared = [_draw_actor(d, False) for _ in range(d.pick(3))]
        if d.pick(2):
            return hm.ActorSlices(declared=declared, undeclared=_draw_actor(d, True))
        return hm.ActorSlices(declared=declared)
    if kind == 1:
        days = draw_count(d)
        never = [draw_memory(d, i) for _ in range(d.pick(3))]
        never_total = draw_count(d)
        stale = [draw_memory(d, i) for _ in range(d.pick(3))]
        stale_total = draw_count(d)
        fresh = draw_count(d)
        never_checkable = draw_count(d)
        stale_checkable = draw_count(d)
        return hm.VerificationDebt(
            stale_after_days=days,
            never_verified=never,
            never_verified_total=never_total,
            stale=stale,
            stale_total=stale_total,
            fresh_count=fresh,
            never_verified_checkable=never_checkable,
            stale_checkable=stale_checkable,
        )
    if kind == 2:
        return _draw_drift_row(d)
    if kind == 3:
        repo = _draw_opt_text(d)
        cwd = _draw_opt_text(d)
        rows = [_draw_drift_row(d) for _ in range(d.pick(3))]
        total = draw_count(d)
        unconfirmed = draw_count(d)
        unread = _draw_unread(d)
        return hm.CommitDriftDebt(
            current_repo=repo,
            current_cwd=cwd,
            rows=rows,
            total_drifted=total,
            unconfirmed=unconfirmed,
            **unread,
        )
    if kind == 4:
        return _draw_group(d)
    if kind == 5:
        groups = [_draw_group(d) for _ in range(d.pick(3))]
        skipped = []
        for _ in range(d.pick(3)):
            repo = _draw_text(d)
            reason = _draw_text(d)
            skipped.append({"repo": repo, "reason": reason})
        total = draw_count(d)
        unanchorable = draw_count(d)
        return hm.CrossRepoDrift(
            groups=groups,
            skipped=skipped,
            total_drifted=total,
            unanchorable=unanchorable,
        )
    if kind == 6:
        floor = draw_count(d)
        rows_ = [draw_memory(d, i) for _ in range(d.pick(3))]
        total = draw_count(d)
        return hm.ColdEndorsementMemories(min_retrievals=floor, rows=rows_, total=total)
    if kind == 7:
        counts = [draw_count(d) for _ in range(4)]
        return hm.SilentMissStats(*counts)
    texts = [_draw_opt_text(d) for _ in range(4)]
    return hm.RecentSilentMiss(*texts)


def rollup_digest() -> str:
    d = _Draw(DEBT_ROLLUP_SEED)
    lines = []
    for i in range(DEBT_ROLLUP_COUNT):
        value = draw_rollup(d, i)
        try:
            lines.append(json.dumps(value.to_dict()))
        except TypeError:
            lines.append("TypeError")
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def pred_digest() -> str:
    """Each case: ``applied`` and ``explicit`` by `draw_count`, ``t`` by
    unit 1's `draw_float`; the line ``<applied> <explicit> <t bits> <clamp
    bits> <weak of t> <weak of the clamp>``, the answers 0 or 1."""
    d = _Draw(DEBT_PRED_SEED)
    lines = []
    for _ in range(DEBT_PRED_COUNT):
        applied = draw_count(d)
        explicit = draw_count(d)
        t = draw_float(d)
        c = hm.clamp_ratio_threshold(t)
        w = int(hm.weakly_endorsed(applied, explicit, t))
        wc = int(hm.weakly_endorsed(applied, explicit, c))
        lines.append(f"{applied} {explicit} {_bits(t):016x} {_bits(c):016x} {w} {wc}")
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def generate_debt() -> dict[str, bytes]:
    rows = [
        *_clamp_rows(),
        *_weak_rows(),
        *_unread_rows(),
        *_unread_dict_rows(),
        *_actors_rows(),
        *_vdebt_rows(),
        *_drow_rows(),
        *_ddebt_rows(),
        *_group_rows(),
        *_cross_rows(),
        *_cold_rows(),
        *_silent_rows(),
        *_recent_rows(),
    ]
    header = {
        "fixture": "health_model_debt",
        "count": len(rows),
        "caps": {
            "verification_debt": hm._VERIFICATION_DEBT_CAP,
            "cold_endorsement_min_retrievals": hm._COLD_ENDORSEMENT_MIN_RETRIEVALS,
            "cold_endorsement": hm._COLD_ENDORSEMENT_CAP,
            "cross_repo_max_roots": hm._CROSS_REPO_MAX_ROOTS,
            "commit_drift_debt": hm._COMMIT_DRIFT_DEBT_CAP,
            "recent_silent_misses": hm._RECENT_SILENT_MISSES_CAP,
        },
        "basis_default": hm.CommitDriftRow("i", [], "s", None, 0).basis,
    }
    sweeps = [
        {
            "k": "rollup",
            "seed": DEBT_ROLLUP_SEED,
            "count": DEBT_ROLLUP_COUNT,
            "sha256": rollup_digest(),
        },
        {
            "k": "pred",
            "seed": DEBT_PRED_SEED,
            "count": DEBT_PRED_COUNT,
            "sha256": pred_digest(),
        },
    ]
    return {"health_model_debt.jsonl": jsonl([header, *rows, *sweeps])}


# ---------------------------------------------------------------------------
# Unit health_model-3: ``health_model_report.jsonl`` (`generate_report`).
#
# It pins `Recommendation`, `ProvenanceRow`, `ProvenanceDebt` and
# `HealthReport`, each with its `to_dict` (and `EpisodeVolume.to_dict`
# through the report), and in the header `_RECOMMENDATION_ROW_CAP`,
# `_RECOMMENDATION_SIZE_FLOOR`, `RECOMMENDATION_KINDS` and
# `_PROVENANCE_ROW_CAP`. One row per case, its kind under ``k``: ``rec``,
# ``prow``, ``pdebt`` and ``report``, each with ``in``, the arguments
# given (a report's fields that are set, each spelled as the rows of units
# 1 and 2 spell its type; a field left out keeps its default), and ``d``,
# `to_dict` (neutral) or ``{"x": class}``. The last line is the sweep:
# ``count`` reports drawn with `_Draw` from ``seed`` by `draw_report`, the
# SHA-256 of their ``json.dumps(to_dict())`` lines or the class raised.
# Not gated, for the reasons the module text gives for unit 1's file.
# ---------------------------------------------------------------------------

REPORT_SEED = 20261127
REPORT_COUNT = 2000
_STATS_BUCKETS = ("dead_weight", "cold_memories", "heavily_used", "contradicted")


def _rec_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled: dict[str, Any] = {
        **text_field("kind", kw["kind"]),
        **text_field("summary", kw["summary"]),
        **text_field("action", kw["action"]),
        "count": kw["count"],
    }
    if "memory_ids" in kw:
        spelled["memory_ids"] = _texts(kw["memory_ids"])
    if "scope" in kw:
        spelled.update(_opt_text("scope", kw["scope"]))
    return spelled


def _prow_in(kw: dict[str, Any]) -> dict[str, Any]:
    return {
        **text_field("id", kw["id"]),
        "scopes": _texts(kw["scopes"]),
        **text_field("summary", kw["summary"]),
        "created": _iso(kw["created"]),
    }


def _pdebt_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled: dict[str, Any] = {
        "counts": _pairs(kw["counts"]),
        "unaccounted_total": kw["unaccounted_total"],
    }
    if "unaccounted" in kw:
        spelled["unaccounted"] = [_prow_in(r) for r in kw["unaccounted"]]
    return spelled


def _pdebt(kw: dict[str, Any]) -> hm.ProvenanceDebt:
    built = dict(kw)
    if "unaccounted" in kw:
        built["unaccounted"] = [hm.ProvenanceRow(**r) for r in kw["unaccounted"]]
    return hm.ProvenanceDebt(**built)


_REC0: dict[str, Any] = {
    "kind": "remove_dead_weight",
    "summary": "3 memories retrieved but never applied",
    "action": "remove or narrow them",
    "count": 3,
}
_RECS: list[dict[str, Any]] = [
    _REC0,
    {**_REC0, "memory_ids": ["m-1", "m-2"]},
    {**_REC0, "kind": "fix_typo_scopes", "count": 1, "scope": "projets:x"},
    {**_REC0, "memory_ids": [], "scope": None},
    {**_REC0, "kind": "", "summary": "é\ud800", "action": "", "scope": ""},
    {**_REC0, "kind": "not_a_kind", "count": I64_MIN, "memory_ids": ["\ud83d", "a"]},
    {**_REC0, "count": I64_MAX, "memory_ids": [f"m-{j}" for j in range(12)]},
]
_PROW0: dict[str, Any] = {
    "id": "m-7",
    "scopes": ["global", "projects:a"],
    "summary": "an unaccounted memory",
    "created": D1,
}
_PROWS: list[dict[str, Any]] = [
    _PROW0,
    {**_PROW0, "scopes": [], "summary": "", "created": _N1},
    {**_PROW0, "id": "m-é\ud800", "created": D1.astimezone(MICRO)},
    {**_PROW0, "summary": 'q"\\\n', "created": D1.astimezone(SEC30)},
    {**_PROW0, "created": datetime(1, 1, 1, tzinfo=P559)},
    {**_PROW0, "created": datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=M559)},
]
_PDEBTS: list[dict[str, Any]] = [
    {"counts": {}, "unaccounted_total": 0},
    {
        "counts": {"written": 40, "pulled": 3, "unaccounted": 2, "unclassified": 0},
        "unaccounted_total": 2,
        "unaccounted": [_PROW0, _PROWS[2]],
    },
    {
        "counts": {"z": 1, "a": 2, "é\ud800": 3},
        "unaccounted_total": 0,
        "unaccounted": [],
    },
    {"counts": {"": I64_MIN}, "unaccounted_total": I64_MAX},
    {"counts": {"unaccounted": 25}, "unaccounted_total": -1, "unaccounted": _PROWS},
]


def _rec_rows() -> list[dict[str, Any]]:
    return [
        {"k": "rec", "in": _rec_in(kw), "d": neutral(hm.Recommendation(**kw).to_dict())}
        for kw in _RECS
    ]


def _prow_rows() -> list[dict[str, Any]]:
    return [
        {
            "k": "prow",
            "in": _prow_in(kw),
            "d": neutral(hm.ProvenanceRow(**kw).to_dict()),
        }
        for kw in _PROWS
    ]


def _pdebt_rows() -> list[dict[str, Any]]:
    return [
        {"k": "pdebt", "in": _pdebt_in(kw), "d": neutral(_pdebt(kw).to_dict())}
        for kw in _PDEBTS
    ]


def _marker_in(kw: dict[str, Any]) -> dict[str, Any]:
    return {
        **text_field("marker", kw["marker"]),
        **{k: v for k, v in kw.items() if k != "marker"},
    }


def _scope_in(kw: dict[str, Any]) -> dict[str, Any]:
    return {
        **text_field("scope", kw["scope"]),
        **{k: v for k, v in kw.items() if k != "scope"},
    }


def _actors_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled: dict[str, Any] = {}
    if "declared" in kw:
        spelled["declared"] = [_actor_in(a) for a in kw["declared"]]
    if "undeclared" in kw:
        spelled["undeclared"] = _actor_in(kw["undeclared"])
    return spelled


def _actors(kw: dict[str, Any]) -> hm.ActorSlices:
    built: dict[str, Any] = {}
    if "declared" in kw:
        built["declared"] = [hm.ActorSlice(**a) for a in kw["declared"]]
    if "undeclared" in kw:
        built["undeclared"] = hm.ActorSlice(**kw["undeclared"])
    return hm.ActorSlices(**built)


def _stats_in(kw: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {k: [_memory_in(m) for m in v] if k in names else v for k, v in kw.items()}


def _stats_built(kw: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {k: [_memory(m) for m in v] if k in names else v for k, v in kw.items()}


def _cross_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled = dict(kw)
    if "groups" in kw:
        spelled["groups"] = [_debt_in(g) for g in kw["groups"]]
    if "skipped" in kw:
        spelled["skipped"] = neutral(kw["skipped"])
    return spelled


def _cross(kw: dict[str, Any]) -> hm.CrossRepoDrift:
    built = dict(kw)
    if "groups" in kw:
        built["groups"] = [
            hm.CrossRepoDriftGroup(**_with_rows(g)) for g in kw["groups"]
        ]
    return hm.CrossRepoDrift(**built)


def _coverage_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled = {k: v for k, v in kw.items() if k != "reason"}
    if "reason" in kw:
        spelled.update(_opt_text("reason", kw["reason"]))
    return spelled


def _recent_in(kw: dict[str, Any]) -> dict[str, Any]:
    spelled: dict[str, Any] = {}
    for k, v in kw.items():
        spelled.update(_opt_text(k, v))
    return spelled


def _optional(value: Any, spell: Callable[[Any], Any]) -> Any:
    return None if value is None else spell(value)


def _report_in(c: dict[str, Any]) -> dict[str, Any]:
    spellers: dict[str, Callable[[Any], Any]] = {
        "generated_at": _iso,
        "marker_stats": lambda v: [_marker_in(m) for m in v],
        "scope_distribution": _pairs,
        "scope_health": lambda v: [_scope_in(s) for s in v],
        "rare_scopes": _texts,
        "actor_slices": _actors_in,
        "verification_debt": lambda v: _stats_in(v, ("never_verified", "stale")),
        "commit_drift_debt": partial(_optional, spell=_debt_in),
        "cross_repo_drift": partial(_optional, spell=_cross_in),
        "provenance": partial(_optional, spell=_pdebt_in),
        "recent_silent_misses": lambda v: [_recent_in(r) for r in v],
        "cold_endorsement_memories": lambda v: _stats_in(v, ("rows",)),
        "recommendations": lambda v: [_rec_in(r) for r in v],
        "telemetry_coverage": partial(_optional, spell=_coverage_in),
    }
    spelled: dict[str, Any] = {}
    for k, v in c.items():
        if k in _STATS_BUCKETS:
            spelled[k] = [_memory_in(m) for m in v]
        elif k in spellers:
            spelled[k] = spellers[k](v)
        else:
            spelled[k] = v
    return spelled


def _report(c: dict[str, Any]) -> hm.HealthReport:
    builders: dict[str, Callable[[Any], Any]] = {
        "marker_stats": lambda v: [hm.MarkerStats(**m) for m in v],
        "scope_health": lambda v: [hm.ScopeHealth(**s) for s in v],
        "actor_slices": _actors,
        "verification_debt": lambda v: hm.VerificationDebt(
            **_stats_built(v, ("never_verified", "stale"))
        ),
        "commit_drift_debt": partial(
            _optional, spell=lambda v: hm.CommitDriftDebt(**_with_rows(v))
        ),
        "cross_repo_drift": partial(_optional, spell=_cross),
        "provenance": partial(_optional, spell=_pdebt),
        "silent_misses": lambda v: hm.SilentMissStats(**v),
        "recent_silent_misses": lambda v: [hm.RecentSilentMiss(**r) for r in v],
        "cold_endorsement_memories": lambda v: hm.ColdEndorsementMemories(
            **_stats_built(v, ("rows",))
        ),
        "recommendations": lambda v: [hm.Recommendation(**r) for r in v],
        "telemetry_coverage": partial(
            _optional, spell=lambda v: hm.TelemetryCoverage(**v)
        ),
        "episode_volume": partial(_optional, spell=lambda v: EpisodeVolume(**v)),
    }
    built: dict[str, Any] = {}
    for k, v in c.items():
        if k in _STATS_BUCKETS:
            built[k] = [_memory(m) for m in v]
        elif k in builders:
            built[k] = builders[k](v)
        else:
            built[k] = v
    return hm.HealthReport(**built)


_REPORT0: dict[str, Any] = {
    "generated_at": D1,
    "window_days": 30,
    "total_active_memories": 120,
    "total_events": 900,
    "distinct_sessions": 14,
}
_EPISODES = {
    "sessions": 4,
    "episodes": 31,
    "bytes": 20480,
    "prunable_sessions": 1,
    "ttl_days": 30,
}
_HOOKLESS = {
    "hook_telemetry_events": 0,
    "covered": False,
    "dead_weight_suppressed": True,
    "cold_endorsement_suppressed": True,
    "reason": hm._HOOKLESS_REASON,
}
_CROSS = {
    "groups": [
        {"repo": "a", "worktree_root": "/w/a", "rows": [_R0], "total_drifted": 1},
        {
            "repo": "b",
            "worktree_root": "/w/b",
            "unread": 1,
            "unread_causes": {"error": 1},
        },
    ],
    "skipped": [{"repo": "c", "reason": "missing"}],
    "total_drifted": 1,
    "unanchorable": 4,
}
_DDEBT = {
    "current_repo": "github.com/a/b",
    "current_cwd": "/w/b",
    "rows": [_R0, _DROWS[2]],
    "total_drifted": 40,
    "unconfirmed": 2,
    "unread": 2,
    "unread_causes": {"timeout": 1, "error": 1},
    "unread_ids": ["b", "a"],
}
_REPORT_FULL: dict[str, Any] = {
    **_REPORT0,
    "dead_weight": [_M0, _UNICODE],
    "cold_memories": [_FULL],
    "heavily_used": [_M0],
    "contradicted": [_FULL, _M0],
    "marker_stats": [
        {"marker": "m", "fire_count": 3, "override_count": 1},
        {"marker": "é\ud800", "fire_count": 0, "override_count": 0},
    ],
    "scope_distribution": {"z": 3, "a": 1, "é\ud800": 2, "global": 0},
    "scope_health": [
        {
            "scope": "z",
            "active": 3,
            "dead": 1,
            "cold": 0,
            "contradicted": 1,
            "applied_total": 9,
        },
        {"scope": "a"},
    ],
    "rare_scopes": ["projets:x", "\ud800"],
    "actor_slices": {
        "declared": [
            {"client": "claude-code", "memories": 3, "models": {"z": 2, "a": 1}}
        ],
        "undeclared": {"client": None, "memories": 117, "events": 900},
    },
    "orphan_use_events": 7,
    "verification_debt": {
        "stale_after_days": 90,
        "never_verified": [_M0],
        "never_verified_total": 80,
        "stale": [_UNICODE],
        "stale_total": 30,
        "fresh_count": 10,
        "never_verified_checkable": 60,
        "stale_checkable": 2,
    },
    "commit_drift_debt": _DDEBT,
    "cross_repo_drift": _CROSS,
    "provenance": _PDEBTS[1],
    "silent_misses": {
        "audited_total": 40,
        "miss_total": 9,
        "unique_miss_memories": 1,
        "no_signal_total": 3,
    },
    "recent_silent_misses": [
        {"event_id": "01J0", "top_hit_id": "m-1", "query_preview": "how", "ts": "t"},
        {"event_id": None, "top_hit_id": None, "query_preview": None, "ts": None},
    ],
    "cold_endorsement_memories": {"min_retrievals": 5, "rows": [_FULL], "total": 300},
    "recommendations": [_RECS[1], _RECS[2]],
    "telemetry_coverage": _HOOKLESS,
    "episode_volume": _EPISODES,
}
_EMPTY_LISTS: dict[str, Any] = {
    **_REPORT0,
    **{k: [] for k in _STATS_BUCKETS},
    "marker_stats": [],
    "scope_distribution": {},
    "scope_health": [],
    "rare_scopes": [],
    "recent_silent_misses": [],
    "recommendations": [],
}
_REPORTS: list[dict[str, Any]] = [
    _REPORT0,
    {**_REPORT0, "generated_at": _N1},
    {**_REPORT0, "generated_at": D1.astimezone(MICRO)},
    {**_REPORT0, "generated_at": D1.astimezone(SEC30)},
    {**_REPORT0, "generated_at": datetime(1, 1, 1, tzinfo=P559)},
    _EMPTY_LISTS,
    _REPORT_FULL,
    {**_REPORT0, "commit_drift_debt": {"current_repo": None, "current_cwd": None}},
    {**_REPORT0, "cross_repo_drift": {}},
    {**_REPORT0, "provenance": _PDEBTS[0]},
    {
        **_REPORT0,
        "telemetry_coverage": {
            "hook_telemetry_events": 12,
            "covered": True,
            "dead_weight_suppressed": False,
        },
    },
    {**_REPORT0, "episode_volume": dict.fromkeys(_EPISODES, 0)},
    {
        **_REPORT0,
        "commit_drift_debt": None,
        "cross_repo_drift": None,
        "provenance": None,
    },
    {**_REPORT0, "actor_slices": {}, "silent_misses": {}},
    {**_REPORT0, "verification_debt": {"stale_after_days": 30}},
    {**_REPORT0, "cold_endorsement_memories": {"min_retrievals": 30}},
    {
        "generated_at": D1,
        "window_days": I64_MIN,
        "total_active_memories": I64_MAX,
        "total_events": -1,
        "distinct_sessions": 0,
        "orphan_use_events": I64_MIN,
        "scope_distribution": {"a": I64_MAX, "b": I64_MIN},
        "episode_volume": {**_EPISODES, "bytes": I64_MAX, "ttl_days": I64_MIN},
    },
    *({**_REPORT0, k: [_M0, _RAISES]} for k in _STATS_BUCKETS),
    {**_REPORT0, "verification_debt": {"stale_after_days": 30, "stale": [_RAISES]}},
    {
        **_REPORT0,
        "cold_endorsement_memories": {"min_retrievals": 30, "rows": [_RAISES]},
    },
    {
        **_REPORT_FULL,
        "contradicted": [_RAISES],
        "cold_endorsement_memories": {"min_retrievals": 1, "rows": [_RAISES]},
    },
]


def _report_rows() -> list[dict[str, Any]]:
    return [
        {"k": "report", "in": _report_in(c), "d": _try(_report(c).to_dict, neutral)}
        for c in _REPORTS
    ]


def _draw_prov_row(d: _Draw) -> hm.ProvenanceRow:
    ident = _draw_text(d)
    scopes = [_draw_text(d) for _ in range(d.pick(3))]
    summary = _draw_text(d)
    created = draw_dt(d, True)
    return hm.ProvenanceRow(id=ident, scopes=scopes, summary=summary, created=created)


def _draw_rec(d: _Draw) -> hm.Recommendation:
    kind = hm.RECOMMENDATION_KINDS[d.pick(len(hm.RECOMMENDATION_KINDS))]
    summary = _draw_text(d)
    action = _draw_text(d)
    count = draw_count(d)
    ids = [_draw_text(d) for _ in range(d.pick(3))]
    scope = _draw_opt_text(d)
    return hm.Recommendation(kind, summary, action, count, ids, scope)


def draw_report(d: _Draw, i: int) -> hm.HealthReport:
    """A `HealthReport` drawn field by field in declaration order: the
    five required fields (``generated_at`` may be naive), each list of
    ``pick(3)`` entries (`MemoryStats` by unit 1's `draw_memory`), each
    dict a `_draw_census` of `_TEXTS`, and each defaulted or optional
    bucket left at its default (None) when ``pick(2)`` is 0, else drawn as
    unit 2's `draw_rollup` draws that kind."""
    r = hm.HealthReport(
        generated_at=draw_dt(d, True),
        window_days=draw_count(d),
        total_active_memories=draw_count(d),
        total_events=draw_count(d),
        distinct_sessions=draw_count(d),
    )
    for name in _STATS_BUCKETS:
        setattr(r, name, [draw_memory(d, i) for _ in range(d.pick(3))])
    r.marker_stats = [draw_marker(d) for _ in range(d.pick(3))]
    r.scope_distribution = _draw_census(d, _TEXTS)
    for _ in range(d.pick(3)):
        scope = _draw_text(d)
        counts = [draw_count(d) for _ in range(5)]
        r.scope_health.append(hm.ScopeHealth(scope, *counts))
    r.rare_scopes = [_draw_text(d) for _ in range(d.pick(3))]
    if d.pick(2):
        declared = [_draw_actor(d, False) for _ in range(d.pick(3))]
        r.actor_slices = hm.ActorSlices(
            declared=declared, undeclared=_draw_actor(d, True)
        )
    r.orphan_use_events = draw_count(d)
    if d.pick(2):
        v = r.verification_debt = hm.VerificationDebt(stale_after_days=draw_count(d))
        v.never_verified = [draw_memory(d, i) for _ in range(d.pick(3))]
        v.never_verified_total = draw_count(d)
        v.stale = [draw_memory(d, i) for _ in range(d.pick(3))]
        v.stale_total = draw_count(d)
        v.fresh_count = draw_count(d)
        v.never_verified_checkable = draw_count(d)
        v.stale_checkable = draw_count(d)
    if d.pick(2):
        c = hm.CommitDriftDebt(
            current_repo=_draw_opt_text(d), current_cwd=_draw_opt_text(d)
        )
        c.rows = [_draw_drift_row(d) for _ in range(d.pick(3))]
        c.total_drifted = draw_count(d)
        c.unconfirmed = draw_count(d)
        for k, val in _draw_unread(d).items():
            setattr(c, k, val)
        r.commit_drift_debt = c
    if d.pick(2):
        x = hm.CrossRepoDrift(groups=[_draw_group(d) for _ in range(d.pick(3))])
        for _ in range(d.pick(3)):
            repo = _draw_text(d)
            reason = _draw_text(d)
            x.skipped.append({"repo": repo, "reason": reason})
        x.total_drifted = draw_count(d)
        x.unanchorable = draw_count(d)
        r.cross_repo_drift = x
    if d.pick(2):
        p = hm.ProvenanceDebt(
            counts=_draw_census(d, _TEXTS), unaccounted_total=draw_count(d)
        )
        p.unaccounted = [_draw_prov_row(d) for _ in range(d.pick(3))]
        r.provenance = p
    if d.pick(2):
        r.silent_misses = hm.SilentMissStats(*[draw_count(d) for _ in range(4)])
    for _ in range(d.pick(3)):
        r.recent_silent_misses.append(
            hm.RecentSilentMiss(*[_draw_opt_text(d) for _ in range(4)])
        )
    if d.pick(2):
        cold = r.cold_endorsement_memories = hm.ColdEndorsementMemories(
            min_retrievals=draw_count(d)
        )
        cold.rows = [draw_memory(d, i) for _ in range(d.pick(3))]
        cold.total = draw_count(d)
    r.recommendations = [_draw_rec(d) for _ in range(d.pick(3))]
    if d.pick(2):
        events = draw_count(d)
        covered = bool(d.pick(2))
        dead = bool(d.pick(2))
        cold_s = bool(d.pick(2))
        r.telemetry_coverage = hm.TelemetryCoverage(
            events, covered, dead, cold_s, _draw_opt_text(d)
        )
    if d.pick(2):
        r.episode_volume = EpisodeVolume(*[draw_count(d) for _ in range(5)])
    return r


def report_digest() -> str:
    d = _Draw(REPORT_SEED)
    lines = []
    for i in range(REPORT_COUNT):
        report = draw_report(d, i)
        try:
            lines.append(json.dumps(report.to_dict()))
        except TypeError:
            lines.append("TypeError")
    return hashlib.sha256("\n".join(lines).encode("ascii")).hexdigest()


def generate_report() -> dict[str, bytes]:
    rows = [*_rec_rows(), *_prow_rows(), *_pdebt_rows(), *_report_rows()]
    header = {
        "fixture": "health_model_report",
        "count": len(rows),
        "caps": {
            "recommendation_row": hm._RECOMMENDATION_ROW_CAP,
            "recommendation_size_floor": hm._RECOMMENDATION_SIZE_FLOOR,
            "provenance_row": hm._PROVENANCE_ROW_CAP,
        },
        "kinds": list(hm.RECOMMENDATION_KINDS),
    }
    sweep = {
        "k": "report",
        "seed": REPORT_SEED,
        "count": REPORT_COUNT,
        "sha256": report_digest(),
    }
    return {"health_model_report.jsonl": jsonl([header, *rows, sweep])}
