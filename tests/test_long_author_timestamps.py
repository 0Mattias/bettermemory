"""Claim 21: a commit whose author line carries a timestamp longer than
CPython's int-string limit (4300 digits) makes `origin_history.seconds`
raise ValueError, which propagates out of `commit_author_timestamps`."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bettermemory import origin_dates, origin_history
from bettermemory.origin_history import HistoryUnread

from .test_multiline_literal_drift import neutral_git
from .test_dateless_history import neutral_author_identity

__all__ = ["neutral_git", "neutral_author_identity"]


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return done.stdout.decode().strip()


def _repo_with_author_seconds(tmp_path: Path, digits: int) -> Path:
    repo = tmp_path / f"repo{digits}"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    parent = _git(repo, "rev-parse", "HEAD")
    body = (
        f"tree {tree}\nparent {parent}\n"
        f"author t <t@e> {'9' * digits} +0000\n"
        "committer t <t@e> 1700000000 +0000\n\nodd\n"
    ).encode()
    sha = _git(
        repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body
    )
    _git(repo, "update-ref", "HEAD", sha)
    # git prints the raw digits for %at
    assert _git(repo, "log", "-1", "--format=%at") == "9" * digits
    return repo


def test_seconds_never_raises_on_a_line_of_digits() -> None:
    # "The decimal seconds %at prints, or None for any other line."
    value = origin_history.seconds("9" * 5000)
    assert value is None or isinstance(value, int)


def test_long_zero_padding_keeps_a_representable_instant() -> None:
    assert origin_history.seconds("0" * 5000 + "42") == 42
    assert origin_history.seconds("0" * 5000) == 0


def test_datetime_range_is_the_saturation_boundary() -> None:
    latest = datetime.max.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    last = (latest - epoch) // timedelta(seconds=1)
    assert origin_history.author_dates([last]) == [latest.replace(microsecond=0)]
    for text in (str(last + 1), "9" * 5000):
        value = origin_history.seconds(text)
        assert value is not None
        assert origin_history.author_dates([value]) == [latest]


def test_4300_digit_author_date_reads_as_latest(tmp_path: Path) -> None:
    repo = _repo_with_author_seconds(tmp_path, 4300)
    dates = origin_dates.commit_author_timestamps(repo)
    assert isinstance(dates, list) and len(dates) == 2
    assert dates[-1] == datetime.max.replace(tzinfo=timezone.utc)


def test_5000_digit_author_date_does_not_raise(tmp_path: Path) -> None:
    repo = _repo_with_author_seconds(tmp_path, 5000)
    # Promise: a list of datetimes, None, or a HistoryUnread; never a raise.
    dates = origin_dates.commit_author_timestamps(repo)
    assert dates is None or isinstance(dates, (list, HistoryUnread))


def test_5000_digit_author_date_extension_does_not_raise(tmp_path: Path) -> None:
    """The `%m%H %at` reader (`_extended_author_timestamps`) shares `seconds`."""
    repo = tmp_path / "ext"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    base = origin_dates.commit_author_timestamps(repo)
    assert isinstance(base, list) and len(base) == 1
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    parent = _git(repo, "rev-parse", "HEAD")
    body = (
        f"tree {tree}\nparent {parent}\n"
        f"author t <t@e> {'9' * 5000} +0000\n"
        "committer t <t@e> 1700000000 +0000\n\nodd\n"
    ).encode()
    sha = _git(
        repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body
    )
    _git(repo, "update-ref", "refs/heads/main", sha)
    dates = origin_dates.commit_author_timestamps(repo)
    assert dates is None or isinstance(dates, (list, HistoryUnread))
