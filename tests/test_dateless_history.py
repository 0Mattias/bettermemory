"""Claim 42: a commit whose author line holds no date prints a blank
``%at`` line, which `origin_history.read_lines` skips, so
`commit_author_timestamps` returns one date fewer and no ERROR."""

from __future__ import annotations

import bisect
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import origin_dates, origin_history
from bettermemory.origin_history import HistoryUnread

from .test_multiline_literal_drift import neutral_git

__all__ = ["neutral_git"]

_ENV = {
    "GIT_AUTHOR_NAME": "A",
    "GIT_AUTHOR_EMAIL": "a@b.c",
    "GIT_COMMITTER_NAME": "A",
    "GIT_COMMITTER_EMAIL": "a@b.c",
}


def _git(repo: Path, *args: str, stdin: bytes | None = None, date: str = "") -> str:
    env = {**os.environ, **_ENV}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        env=env,
        timeout=30,
        check=True,
    )
    return done.stdout.decode().strip()


def _dateless(repo: Path) -> str:
    """A child of HEAD whose author line holds no date; HEAD moves to it."""
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    parent = _git(repo, "rev-parse", "HEAD")
    body = (
        f"tree {tree}\nparent {parent}\nauthor A <a@b.c>\n"
        "committer C <c@d.e> 1700000500 +0000\n\nno author date\n"
    ).encode()
    sha = _git(
        repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body
    )
    _git(repo, "update-ref", "HEAD", sha)
    return sha


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("a\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "one", date="1700000000 +0000")
    return repo


def _fresh() -> None:
    with origin_history._TIMESTAMPS_MEMO_LOCK:
        origin_history._TIMESTAMPS_MEMO.clear()
        origin_history._TIMESTAMPS_FAILED.clear()


def test_dateless_commit_between_two_declines_the_read(repo: Path) -> None:
    _dateless(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "three", date="1700001000 +0000")
    assert _git(repo, "rev-list", "--count", "HEAD") == "3"
    # git really prints a blank line for the commit.
    assert _git(repo, "log", "--format=%at").split("\n") == [
        "1700001000",
        "",
        "1700000000",
    ]
    _fresh()
    got = origin_dates.commit_author_timestamps(repo)
    # Promise: "printed a line its format does not print (one such line
    # declines the whole read)"; "never drops out of the count".
    assert got is HistoryUnread.ERROR, got


def test_dateless_head_after_verify_is_not_counted_zero(repo: Path) -> None:
    """The commit landed after the verify instant is the dateless one: the
    count since that instant must not read 0 (caught up)."""
    verified = datetime.fromtimestamp(1700000100, tz=timezone.utc)
    _dateless(repo)
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    _fresh()
    got = origin_dates.commit_author_timestamps(repo)
    if isinstance(got, list):
        since = len(got) - bisect.bisect_right(got, verified)
        assert len(got) == 2 and since >= 1, (got, since)
    else:
        assert got is HistoryUnread.ERROR, got


def test_plain_form_agrees_with_hashed_form(repo: Path) -> None:
    """The ``%H %at`` form read under a shallow cut declines this history;
    the plain form must not answer a list for the same commits."""
    _dateless(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "three", date="1700001000 +0000")
    head = _git(repo, "rev-parse", "HEAD")
    unrelated = frozenset({"0" * 40})
    hashed = origin_dates._read_author_timestamps(repo, head, unrelated)
    plain = origin_dates._read_author_timestamps(repo, head)
    assert hashed is HistoryUnread.ERROR
    assert plain is HistoryUnread.ERROR, plain


@pytest.mark.parametrize("raw", ["\n1\n", "1\n\n2\n", "1\n\n", " \n"])
def test_blank_author_records_are_not_silently_skipped(raw: str) -> None:
    assert origin_history.read_lines(raw, origin_history.seconds) is None


def test_an_empty_log_and_one_final_terminator_have_distinct_meanings() -> None:
    assert origin_history.read_lines("", origin_history.seconds) == []
    assert origin_history.read_lines("1\n2\n", origin_history.seconds) == [1, 2]
    assert origin_history.read_lines("\n", origin_history.seconds) is None


def test_a_dateless_commit_touching_the_anchor_declines_filtered_reads(
    repo: Path,
) -> None:
    parent = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("changed\n")
    _git(repo, "add", "f")
    tree = _git(repo, "write-tree")
    body = (
        f"tree {tree}\nparent {parent}\nauthor A <a@b.c>\n"
        "committer C <c@d.e> 1700000500 +0000\n\nchanged without author date\n"
    ).encode()
    sha = _git(
        repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body
    )
    _git(repo, "update-ref", "HEAD", sha)
    assert (
        origin_dates.commit_author_timestamps_touching_pathspecs(
            repo, ["f"], toplevel=repo
        )
        is None
    )
    assert (
        origin_dates.commit_author_sha_pairs_touching_pathspecs(
            repo, ["f"], toplevel=repo
        )
        is None
    )


@pytest.fixture(autouse=True)
def neutral_author_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in (
        ("GIT_AUTHOR_NAME", "Fixture"),
        ("GIT_AUTHOR_EMAIL", "fixture@example.invalid"),
        ("GIT_COMMITTER_NAME", "Fixture"),
        ("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
    ):
        monkeypatch.setenv(key, value)
