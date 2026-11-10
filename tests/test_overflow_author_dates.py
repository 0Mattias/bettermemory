"""Claim 43: a commit whose author timestamp overflows git's timestamp type
is read at the far future by the unfiltered `%at` history but at the epoch by
the `%aI` path-filtered logs, so the two "same date space" counts disagree and
the anchored drift count narrows to 0 for the file that changed."""

from __future__ import annotations

import bisect
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bettermemory import origin_dates, verify

from .test_multiline_literal_drift import neutral_git
from .test_dateless_history import neutral_author_identity

__all__ = ["neutral_git", "neutral_author_identity"]

OVERFLOW = "99999999999999999999"


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin.encode() if stdin is not None else None,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return done.stdout.decode().strip()


def _repo(tmp_path: Path) -> tuple[Path, datetime]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.com")
    (repo / "g.txt").write_text("one\n")
    (repo / "other.txt").write_text("x\n")
    _git(repo, "add", ".")
    env_date = "2024-01-01T00:00:00+00:00"
    subprocess.run(
        ["git", "commit", "-q", "-m", "first", "--date", env_date],
        cwd=repo,
        check=True,
        timeout=30,
        env={**__import__("os").environ, "GIT_COMMITTER_DATE": env_date},
    )
    parent = _git(repo, "rev-parse", "HEAD")
    # second commit: changes g.txt, author timestamp past git's timestamp type
    (repo / "g.txt").write_text("two\n")
    _git(repo, "add", "g.txt")
    tree = _git(repo, "write-tree")
    body = (
        f"tree {tree}\nparent {parent}\n"
        f"author t <t@example.com> {OVERFLOW} +0000\n"
        f"committer t <t@example.com> 1717200000 +0000\n\nsecond\n"
    )
    sha = _git(
        repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", stdin=body
    )
    _git(repo, "update-ref", "refs/heads/main", sha)
    since = datetime(2024, 3, 1, tzinfo=timezone.utc)  # after first, before "now"
    return repo, since


def test_filtered_timestamps_share_the_unfiltered_date_space(tmp_path: Path) -> None:
    repo, since = _repo(tmp_path)
    whole = origin_dates.commit_author_timestamps(repo)
    assert isinstance(whole, list) and len(whole) == 2
    unfiltered = len(whole) - bisect.bisect_right(whole, since)
    assert unfiltered == 1  # the overflowing commit reads as the latest date

    touching = origin_dates.commit_author_timestamps_touching_pathspecs(repo, ["g.txt"])
    # Promise: same author dates, a count "in the SAME date space"; declining
    # (None) would also be honest. A list that places the commit before
    # `since` is the bug.
    assert touching is None or (
        len(touching) - bisect.bisect_right(touching, since) == unfiltered
    ), f"unfiltered={whole} filtered={touching}"


def test_sha_pairs_share_the_unfiltered_date_space(tmp_path: Path) -> None:
    repo, since = _repo(tmp_path)
    whole = origin_dates.commit_author_timestamps(repo)
    assert isinstance(whole, list)
    unfiltered = len(whole) - bisect.bisect_right(whole, since)
    pairs = origin_dates.commit_author_sha_pairs_touching_pathspecs(repo, ["g.txt"])
    assert pairs is None or (
        sum(1 for when, _ in pairs if when > since) == unfiltered
    ), f"unfiltered={whole} pairs={pairs}"


def test_drift_count_keeps_the_commit_that_changed_the_anchor(tmp_path: Path) -> None:
    repo, since = _repo(tmp_path)
    whole = origin_dates.commit_author_timestamps(repo)
    assert isinstance(whole, list)
    unfiltered = len(whole) - bisect.bisect_right(whole, since)
    assert unfiltered == 1
    count = verify.resolve_commit_drift_count(
        cwd=repo, since=since, unfiltered=unfiltered, anchors=["g.txt"]
    )
    assert count == 1, f"anchored count {count!r} for a file changed after since"
