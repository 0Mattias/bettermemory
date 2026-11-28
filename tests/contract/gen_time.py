"""Fixture 5: datetime formatting and parsing.

Formatters take a neutral datetime (fields plus an offset in seconds, or
null for a naive value); parsers take text. Each result is the exact
output text, null, or the exception type raised. A cell whose result
differs by platform or Python version is marked and records each result.

``datetime_offsets.jsonl`` (`generate_offsets`) holds stated difference
D1: texts whose offset is zero hours, minutes and seconds with a non-zero
fraction after the seconds, read with the offset kept, as CPython 3.13.18
and later read them (3.11.15, 3.12.13 and 3.13.13 read UTC and drop the
fraction). A fraction after the offset's hour or minute or a fourth ``:``
is not D1: 3.14 refuses those texts, the port reads them as 3.13.13
does, and ``datetime_edges.jsonl`` holds them. The
readers are the reference's own (`PARSERS`, `eval_model`'s bound reader
and window test) with `d1_kept_offset` in place of
``datetime.fromisoformat``, so no answer asks the interpreter's parser,
whose answer here follows the release, and the file is the same under
every Python.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Self
from unittest.mock import patch

from pydantic import TypeAdapter

from bettermemory import eval_model, origin_history, store_rows, time_utils, v8
from bettermemory.review import ulid_time
from bettermemory.time_utils import isoformat_utc, parse_event_ts

from . import gen_eval_model
from ._common import jsonl, neutral

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


# ---------------------------------------------------------------------------
# datetime_offsets.jsonl: stated difference D1
# ---------------------------------------------------------------------------

D1 = (
    "an offset of zero hours, minutes and seconds with a non-zero fraction "
    "after the seconds is kept, as CPython 3.13.18 and later keep it"
)

#: The one shape `d1_kept_offset` reads: a date, a time with an optional
#: fraction, then an offset of zero hours, minutes and seconds with a
#: fraction after the seconds: ``.`` or ``,`` after ``00:00:00`` or after
#: the compact ``000000``, or two or more digits straight after the
#: compact seconds (the C parser refuses one).
_D1_SHAPE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?"
    r"([+-])(?:00:00:00[.,]|000000[.,]|000000(?=\d\d))(\d+)"
)


def _micro(digits: str) -> int:
    """A fraction's first six digits as microseconds, as the C parser
    reads them: fewer are scaled up, a seventh and later are dropped."""
    return int(digits[:6].ljust(6, "0"))


def d1_kept_offset(text: str) -> datetime:
    """`datetime.fromisoformat(text)` by D1's rule, for the one shape this
    file holds: the offset is the fraction's microseconds with its sign,
    and UTC only when they are zero (3.13.13 reads UTC whatever the
    fraction). Built from the text's fields, never by the interpreter's
    parser. Any other text is a fault of this generator."""
    m = _D1_SHAPE.fullmatch(text)
    assert m is not None, f"not a D1 text: {text!r}"
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    fraction = 0 if m.group(7) is None else _micro(m.group(7))
    sign = -1 if m.group(8) == "-" else 1
    offset = sign * _micro(m.group(9))
    tz = timezone.utc if offset == 0 else timezone(timedelta(microseconds=offset))
    return datetime(year, month, day, hour, minute, second, fraction, tzinfo=tz)


class _D1DateTime(datetime):
    """`datetime` whose `fromisoformat` is `d1_kept_offset`: the name
    ``datetime`` of each reader's module while a D1 row is answered."""

    @classmethod
    def fromisoformat(cls, date_string: str) -> Self:
        d = d1_kept_offset(date_string)
        return cls(*_fields_of(d), tzinfo=d.tzinfo)


def _fields_of(d: datetime) -> tuple[int, int, int, int, int, int, int]:
    return (d.year, d.month, d.day, d.hour, d.minute, d.second, d.microsecond)


#: The reference modules whose readers call ``datetime.fromisoformat``
#: through their module name ``datetime``.
_D1_READERS = (time_utils, store_rows, v8, origin_history, eval_model)


@contextmanager
def d1_readers() -> Iterator[None]:
    """The reference's readers reading by D1's rule."""
    with ExitStack() as stack:
        for module in _D1_READERS:
            stack.enter_context(patch.object(module, "datetime", _D1DateTime))
        yield


#: Texts through every reader of `PARSERS`: the three that left
#: ``datetime_edges.jsonl`` (its accidents), four more, and the comma and
#: compact spellings of a fraction after the seconds.
D1_TEXTS = [
    "2026-11-15T08:09:10+00:00:00.5",
    "2026-11-15T08:09:10+00:00:00.000001",
    "2026-11-15T08:09:10-00:00:00.000001",
    "2026-11-15T08:09:10-00:00:00.5",
    "2026-11-15T08:09:10+00:00:00.999999",
    "2026-11-15T08:09:10+00:00:00.1234567",
    "2026-11-15 08:09:10.123456-00:00:00.000001",
    "2026-11-15T08:09:10-00:00:00,25",
    "2026-11-15T08:09:10-000000.5",
    "2026-11-15T08:09:10+000000,5",
    "2026-11-15T08:09:10-00000085",
]

_NOW = gen_eval_model.NOW
#: `--since` and `--until` through `parse_window_bound`: the six that left
#: ``eval_model_python.jsonl`` (a bound moved by a microsecond, out of the
#: calendar, later than now), and the first three texts as ``--until``.
D1_BOUNDS: list[tuple[str | None, str, datetime]] = [
    ("2026-10-01T00:00:00-00:00:00.000001", "--since", _NOW),
    ("0001-01-01T00:00:00+00:00:00.000001", "--since", _NOW),
    ("0001-01-01T00:00:00.000001+00:00:00.000001", "--since", _NOW),
    ("9999-12-31T23:59:59.999999-00:00:00.000001", "--until", _NOW),
    ("2026-11-25T12:00:00-00:00:00.000001", "--since", _NOW),
    ("2026-11-25T12:00:00+00:00:00.000001", "--since", _NOW),
    *((text, "--until", _NOW) for text in D1_TEXTS[:3]),
]

_UTC = timezone.utc
#: `_show_in_window` where the kept offset moves a show across a bound.
D1_WINDOWS: list[tuple[dict[str, Any], datetime | None, datetime | None]] = [
    (
        {"ts": "2026-01-01T00:00:00+00:00:00.000001"},
        datetime(2026, 1, 1, tzinfo=_UTC),
        None,
    ),
    (
        {"ts": "2026-01-31T23:59:59.999999-00:00:00.000001"},
        None,
        datetime(2026, 2, 1, tzinfo=_UTC),
    ),
    (
        {"ts": "2025-12-31T23:59:59.5-00:00:00.5"},
        datetime(2026, 1, 1, tzinfo=_UTC),
        None,
    ),
]


def d1_rows() -> list[dict[str, Any]]:
    """Every D1 row, each answered by the reference's readers under
    `d1_readers`, in the keys of the file it came from or belongs with:
    ``datetime_edges.jsonl``'s ``{text, out}`` and
    ``eval_model_python.jsonl``'s bound and window rows."""
    for text in D1_TEXTS:
        assert _D1_SHAPE.fullmatch(text), text
    rows: list[dict[str, Any]] = []
    with d1_readers():
        for text in D1_TEXTS:
            rows.append(
                {
                    "text": text,
                    "out": {k: _run(fn, text) for k, (fn, _) in PARSERS.items()},
                }
            )
        rows += gen_eval_model._bound_rows(D1_BOUNDS)
        for show, start, end in D1_WINDOWS:
            rows.append(
                {
                    "k": "window",
                    "show": neutral(show),
                    "start": gen_eval_model._fields(start),
                    "end": gen_eval_model._fields(end),
                    **gen_eval_model._try(
                        lambda: eval_model._show_in_window(show, start, end),
                        lambda r: r,
                    ),
                }
            )
    for row in rows:
        assert "AssertionError" not in str(row), row
        row["difference"] = "D1"
    return rows


def generate_offsets() -> dict[str, bytes]:
    rows = d1_rows()
    header = {
        "fixture": "datetime_offsets",
        "cases": len(rows),
        "parsers": {k: v[1] for k, v in PARSERS.items()},
        "records": (
            "text: through each parser, as datetime_edges.jsonl; k bound and "
            "k window: eval_model's parse_window_bound and _show_in_window, "
            "as eval_model_python.jsonl"
        ),
        "stated_differences": {"D1": D1},
    }
    return {"datetime_offsets.jsonl": jsonl([header, *rows])}
