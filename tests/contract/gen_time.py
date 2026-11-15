"""Fixture 5: datetime formatting and parsing.

Formatters take a neutral datetime (fields plus an offset in seconds, or
null for a naive value); parsers take text. Each result is the exact
output text, null, or the exception type raised.
"""

from __future__ import annotations

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
            {"text": text, "out": {k: _run(fn, text) for k, (fn, _) in PARSERS.items()}}
        )
    return {"datetime.jsonl": jsonl(records)}
