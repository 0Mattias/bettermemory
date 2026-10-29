"""`--since` and `--until` refuse a bound the window cannot hold.

ENG-171 H2. `parse_since` refused a span `timedelta` cannot hold, but a
span it can hold may still reach back past the calendar's first day:
`eval --since 1000000d` parsed, and `resolve_window`'s `now - span`
then raised `OverflowError` on a line outside the CLI's error handling,
so the user got a traceback instead of a usage error. An absolute
`--since` later than now parsed too, and named a window that had not
started, which every mode read as empty without a word. The parser now
refuses both, so every caller of `parse_window_bound` (`eval`, `review
samples` and `recall review`) hands `resolve_window` only bounds it can
subtract. A later `--until` stays accepted: it only says "up to now".
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.eval import parse_window_bound, resolve_window
from bettermemory.eval_model import _EARLIEST_START

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
FUTURE = "2999-01-01T00:00:00Z"


def _exit_code(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> int | str | None:
    from bettermemory.server import main as server_main

    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "memdir"))
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as excinfo:
        server_main()
    return excinfo.value.code


@pytest.mark.parametrize(
    "argv",
    [
        ["eval", "--since", "1000000d"],
        ["eval", "--until", "1000000d"],
        ["eval", "--since", FUTURE],
        ["eval", "--tool-usage", "--since", "1000000d"],
        ["eval", "--threshold-sweep", "--since", FUTURE],
        ["review", "samples", "--since", "1000000d"],
        ["recall", "review", "--since", "1000000d"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_a_bound_the_window_cannot_hold_is_a_usage_error(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _exit_code(argv, tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert code == 2
    flag = "--until" if "--until" in argv else "--since"
    assert flag in err
    assert "Traceback (most recent call last)" not in err


def test_the_longest_span_the_calendar_holds_resolves() -> None:
    days = (NOW - _EARLIEST_START).days
    span = parse_window_bound(f"{days}d", now=NOW)
    assert span == timedelta(days=days)
    start, end = resolve_window(span, None, now=NOW)
    assert start is not None and start >= _EARLIEST_START
    assert end is None
    with pytest.raises(ValueError, match="year 1"):
        parse_window_bound(f"{days + 1}d", now=NOW)


def test_a_caller_that_passes_no_now_is_held_to_the_current_time() -> None:
    with pytest.raises(ValueError, match="year 1"):
        parse_window_bound("1000000d")
    with pytest.raises(ValueError, match="later than now"):
        parse_window_bound(FUTURE)


def test_a_since_later_than_now_is_refused_and_an_until_is_not() -> None:
    with pytest.raises(ValueError, match="later than now"):
        parse_window_bound("2026-10-28T12:00:01Z", flag="--since", now=NOW)
    assert parse_window_bound("2026-10-28T12:00:00Z", flag="--since", now=NOW) == NOW
    later = parse_window_bound(FUTURE, flag="--until", now=NOW)
    assert later == datetime(2999, 1, 1, tzinfo=timezone.utc)
    start, end = resolve_window(timedelta(days=30), later, now=NOW)
    assert start is not None and end is not None and start < end


#: Bounds the calendar holds as written but not once read in UTC.
EDGES = [
    "0001-01-01T00:00:00+02:00",
    "0001-01-01T01:59:59+02:00",
    "9999-12-31T23:00:00-02:00",
]


@pytest.mark.parametrize("flag", ["--since", "--until"])
@pytest.mark.parametrize("spec", EDGES)
def test_a_bound_past_the_calendar_in_utc_names_its_flag(
    spec: str,
    flag: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`astimezone` raised `OverflowError` on these, which the CLI turned
    into "date value out of range" with no flag named (review D5)."""
    with pytest.raises(ValueError, match=f"^{flag}: .*outside the calendar"):
        parse_window_bound(spec, flag=flag, now=NOW)
    code = _exit_code(["eval", flag, spec], tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert code == 2
    assert f"{flag}: " in err
    assert "Traceback (most recent call last)" not in err
