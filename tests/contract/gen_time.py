"""Fixture 5: datetime formatting and parsing.

Formatters take a neutral datetime (fields plus an offset in seconds, or
null for a naive value); parsers take text. Each result is the exact
output text, null, or the exception type raised. A cell whose result
differs by platform or Python version is marked and records each result.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import TypeAdapter

from bettermemory import origin_history, store_rows, v8
from bettermemory.review import ulid_time
from bettermemory.time_utils import isoformat_utc, parse_event_ts

from ._common import jsonl

_ADAPTER: TypeAdapter[datetime] = TypeAdapter(datetime)


def _pydantic_json(dt: datetime) -> str:
    out = _ADAPTER.dump_python(dt, mode="json")
    assert isinstance(out, str)
    return out


FORMATTERS: dict[str, tuple[Callable[[datetime], str], str]] = {
    "isoformat_utc": (isoformat_utc, "time_utils.isoformat_utc: log ts, events"),
    "isoformat": (
        lambda dt: dt.isoformat(),
        "datetime.isoformat(): store_rows._iso (record columns) and ~23 sites",
    ),
    "str": (str, "str(dt): what default=str spells in log payloads"),
    "compact_utc": (
        lambda dt: dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "backup.py and daemon.py stamp names",
    ),
    "date_only": (lambda dt: dt.strftime("%Y-%m-%d"), "models.py v8 file name"),
    "seconds_z": (
        lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "restore_lock started_at (from now in UTC)",
    ),
    "pydantic_json": (_pydantic_json, "pydantic datetime in mode='json' (responses)"),
}


def _dt_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


PARSERS: dict[str, tuple[Callable[[str], Any], str]] = {
    "parse_event_ts": (
        lambda s: _dt_or_none(parse_event_ts(s)),
        "time_utils.parse_event_ts, result as isoformat()",
    ),
    "store_rows_dt": (
        lambda s: store_rows._dt(s).isoformat(),
        "store_rows._dt (record columns), result as isoformat()",
    ),
    "v8_as_dt": (lambda s: v8._as_dt(s).isoformat(), "v8._as_dt (migration)"),
    "git_instant": (
        lambda s: _dt_or_none(origin_history.instant(s)),
        "origin_history.instant (git %aI)",
    ),
    "git_seconds": (origin_history.seconds, "origin_history.seconds (git %at)"),
    "ulid_time": (lambda s: _dt_or_none(ulid_time(s)), "review.ulid_time"),
}

_TZ = [None, 0, 7200, -18000, 19800, -1, 3600 * 23 + 59 * 60]


def _datetimes() -> list[datetime]:
    bases = [
        (2026, 11, 15, 8, 9, 10, 0),
        (2026, 11, 15, 8, 9, 10, 123000),
        (2026, 11, 15, 8, 9, 10, 123456),
        (2026, 11, 15, 8, 9, 10, 1),
        (1970, 1, 1, 0, 0, 0, 0),
        (2000, 2, 29, 23, 59, 59, 999999),
        (1, 1, 1, 0, 0, 0, 0),
        (9999, 12, 31, 23, 59, 59, 999999),
        (999, 3, 4, 5, 6, 7, 0),
    ]
    out = []
    for b in bases:
        for off in _TZ:
            tz = None if off is None else timezone(timedelta(seconds=off))
            out.append(datetime(*b, tzinfo=tz))
    return out


_TEXTS = [
    "2026-11-15T08:09:10.123Z",
    "2026-11-15T08:09:10Z",
    "2026-11-15T08:09:10.123456+00:00",
    "2026-11-15T08:09:10+02:00",
    "2026-11-15T08:09:10-05:30",
    "2026-11-15T08:09:10",
    "2026-11-15 08:09:10",
    "2026-11-15",
    "20261115T080910Z",
    "2026-11-15T08:09:10.1Z",
    "2026-11-15T08:09:10.1234567Z",
    "2026-11-15T08:09Z",
    "2026-11-15T08Z",
    "2026-11-15T08:09:10z",
    "2026-11-15t08:09:10Z",
    "2026-11-15T08:09:10+0200",
    "2026-11-15T08:09:10+02",
    "2026-11-15T08:09:10+02:00:30",
    "2026-W46-7",
    "2026-319",
    "2026-11-15T24:00:00Z",
    "2026-02-30T00:00:00Z",
    "0001-01-01T00:00:00+02:00",
    "9999-12-31T23:59:59.999999Z",
    " 2026-11-15T08:09:10Z ",
    "2026-11-15T08:09:10ZZ",
    "",
    "Z",
    "not a date",
    "1763194150",
    "0",
    "000000000000",
    "253402300800",
    "99999999999999999999999",
    "-1",
    "01J0000000000000000000000Z",
    "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
    "2026-11-15T08:09:10.123Z\n",
    "２０２６-11-15T08:09:10Z",
]


def _run(fn: Callable[[Any], Any], arg: Any) -> dict[str, Any]:
    try:
        return {"v": fn(arg)}
    except Exception as exc:  # noqa: BLE001 - the error type is the fixture
        return {"error": type(exc).__name__}


_MIDNIGHT_24 = "2026-11-15T24:00:00Z"
_NEXT_DAY = {"v": "2026-11-16T00:00:00+00:00"}

#: Parser cells whose result depends on the Python version, keyed by
#: (text, parser): the first version each result holds from, and the
#: result. ``datetime.fromisoformat`` reads the hour 24 as midnight of
#: the next day from 3.14 and rejects it before.
_BY_VERSION: dict[tuple[str, str], list[tuple[tuple[int, int], dict[str, Any]]]] = {
    (_MIDNIGHT_24, "parse_event_ts"): [((3, 11), {"v": None}), ((3, 14), _NEXT_DAY)],
    (_MIDNIGHT_24, "store_rows_dt"): [
        ((3, 11), {"error": "ValueError"}),
        ((3, 14), _NEXT_DAY),
    ],
    (_MIDNIGHT_24, "v8_as_dt"): [
        ((3, 11), {"error": "ValueError"}),
        ((3, 14), _NEXT_DAY),
    ],
    (_MIDNIGHT_24, "git_instant"): [((3, 11), {"v": None}), ((3, 14), _NEXT_DAY)],
}

#: Parser cells whose result depends on the platform, keyed by (text,
#: parser): the result on each ``sys.platform`` named, and on any other.
#: ``datetime.fromtimestamp`` past the year 9999 raises OSError on
#: Windows and ValueError elsewhere.
_BY_PLATFORM: dict[tuple[str, str], dict[str, dict[str, Any]]] = {
    ("7ZZZZZZZZZZZZZZZZZZZZZZZZZ", "ulid_time"): {
        "win32": {"error": "OSError"},
        "other": {"error": "ValueError"},
    },
}


def _parse(name: str, fn: Callable[[str], Any], text: str) -> dict[str, Any]:
    """The parser's result, or for a cell that differs by Python version
    or platform every recorded result, once this run's result is the one
    recorded for this version or platform. A result that is not records
    itself, so the file no longer matches."""
    out = _run(fn, text)
    by_version = _BY_VERSION.get((text, name))
    if by_version is not None:
        expected = [r for since, r in by_version if sys.version_info[:2] >= since][-1]
        if out != expected:
            return out
        return {
            "version_dependent": {
                f"{since[0]}.{since[1]}+": result for since, result in by_version
            }
        }
    by_platform = _BY_PLATFORM.get((text, name))
    if by_platform is not None:
        if out != by_platform.get(sys.platform, by_platform["other"]):
            return out
        return {"platform_dependent": by_platform}
    return out


_STRFTIME = frozenset({"compact_utc", "date_only", "seconds_z"})


def _format(name: str, fn: Callable[[datetime], str], dt: datetime) -> dict[str, Any]:
    """`strftime` pads ``%Y`` below the year 1000 per platform C library
    (glibc does not, macOS does), so those cells are marked, not pinned.
    `astimezone` reads a naive value as the machine's local time, so a
    naive value through ``compact_utc`` is marked too."""
    if name in _STRFTIME and dt.year < 1000:
        return {"platform_dependent": True}
    if name == "compact_utc" and dt.tzinfo is None:
        return {"local_time_dependent": True}
    return _run(fn, dt)


def generate() -> dict[str, bytes]:
    records: list[Any] = [
        {
            "fixture": "datetime",
            "formatters": {k: v[1] for k, v in FORMATTERS.items()},
            "parsers": {k: v[1] for k, v in PARSERS.items()},
        }
    ]
    for dt in _datetimes():
        offset = dt.utcoffset()
        records.append(
            {
                "datetime": {
                    "fields": [
                        dt.year,
                        dt.month,
                        dt.day,
                        dt.hour,
                        dt.minute,
                        dt.second,
                        dt.microsecond,
                    ],
                    "offset_seconds": None
                    if offset is None
                    else int(offset.total_seconds()),
                },
                "out": {k: _format(k, fn, dt) for k, (fn, _) in FORMATTERS.items()},
            }
        )
    for text in _TEXTS:
        records.append(
            {
                "text": text,
                "out": {k: _parse(k, fn, text) for k, (fn, _) in PARSERS.items()},
            }
        )
    return {"datetime.jsonl": jsonl(records)}
