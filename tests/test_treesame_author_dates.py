"""Claim 50: the author-date touching log (`origin_dates._touching_log`)
runs with git's default history simplification, so a commit authored after
the stamp that changed an anchor is not listed when a merge is TREESAME to
its first parent, and the author-date count reads 0."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.claims import parse_claim
from bettermemory.origin import commits_since_anchor
from bettermemory.origin_dates import commit_author_timestamps_touching_pathspecs
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    ClaimAnchor,
    CommitDriftStatus,
    compute_commit_drift,
    resolve_commit_drift_count,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    set_git_discovery_ceiling(tmp_path, monkeypatch)


def _git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(repo: Path, rel: str, text: str, when: datetime) -> str:
    (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", rel, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _same_change_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Root: a.py VALUE = 1. `old` changes it to 2 before the stamp. main
    gets an unrelated commit (the verified head), the stamp, then the same
    change to a.py after the stamp. `old` merges main (--no-ff): the merge
    is TREESAME to its first parent on a.py. Root, verified head, and the
    post-stamp commit that changed a.py."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.py", "VALUE = 1\n", _T0)
    _git(repo, "checkout", "-q", "-b", "old")
    _commit(repo, "a.py", "VALUE = 2\n", _T0 + timedelta(minutes=30))
    _git(repo, "checkout", "-q", "main")
    verified_head = _commit(repo, "other.py", "x = 0\n", _T0 + timedelta(minutes=40))
    post = _commit(repo, "a.py", "VALUE = 2\n", _VERIFIED + timedelta(hours=1))
    _git(repo, "checkout", "-q", "old")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "merge main",
        "main",
        when=_VERIFIED + timedelta(hours=2),
    )
    return Path(os.path.realpath(repo)), verified_head, post


def _revert_repo(tmp_path: Path) -> tuple[Path, str]:
    """A side branch changes a.py and changes it back, both after the
    stamp, and is merged (--no-ff) into main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "a.py", "VALUE = 1\n", _T0)
    verified_head = _commit(repo, "other.py", "x = 0\n", _T0 + timedelta(minutes=40))
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "a.py", "VALUE = 2\n", _VERIFIED + timedelta(hours=1))
    _commit(repo, "a.py", "VALUE = 1\n", _VERIFIED + timedelta(hours=2))
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "other.py", "x = 1\n", _VERIFIED + timedelta(hours=3))
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "merge side",
        "side",
        when=_VERIFIED + timedelta(hours=4),
    )
    return Path(os.path.realpath(repo)), verified_head


def test_premise_post_stamp_commit_touched_the_anchor(tmp_path: Path) -> None:
    """The post-stamp commit is reachable from HEAD, authored after the
    stamp, and changed a.py; the reachable basis counts it."""
    repo, verified_head, post = _same_change_repo(tmp_path)
    assert _git(repo, "merge-base", "--is-ancestor", post, "HEAD") == ""
    assert _git(repo, "show", "--format=", "--name-only", post) == "a.py"
    assert (repo / "a.py").read_text() == "VALUE = 2\n"
    assert _git(repo, "show", f"{verified_head}:a.py") == "VALUE = 1"
    walk = commits_since_anchor(repo, verified_head, toplevel=repo)
    assert walk is not None
    assert post in walk.shas_touching(["a.py"])


def test_timestamps_touching_lists_the_post_stamp_commit(tmp_path: Path) -> None:
    """'Author timestamps of the commits touching any of `pathspecs`'."""
    repo, _, _ = _same_change_repo(tmp_path)
    touching = commit_author_timestamps_touching_pathspecs(
        repo, ["a.py"], toplevel=repo
    )
    assert touching is not None
    assert _VERIFIED + timedelta(hours=1) in touching


def test_author_date_count_holds_the_post_stamp_commit(tmp_path: Path) -> None:
    """'the EXACT count of commits authored after `since` that touched at
    least one anchor': one such commit, so 1."""
    repo, _, _ = _same_change_repo(tmp_path)
    count = resolve_commit_drift_count(
        cwd=repo, since=_VERIFIED, unfiltered=2, anchors=["a.py"], toplevel=repo
    )
    assert count == 1


def test_author_date_claim_count_holds_the_falsifying_commit(tmp_path: Path) -> None:
    """The claim leg: 'a.py::VALUE=1' is false at HEAD and the commit that
    falsified it was authored after the stamp."""
    repo, _, _ = _same_change_repo(tmp_path)
    count = resolve_commit_drift_count(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=2,
        anchors=(),
        claims=[parse_claim("a.py::VALUE=1")],
        toplevel=repo,
    )
    assert count == 1


def test_record_reads_drift_on_the_author_date_basis(tmp_path: Path) -> None:
    """The record as memory_show reads its commit leg, with no verified
    head: a.py differs from what was verified, by a post-stamp commit."""
    repo, _, _ = _same_change_repo(tmp_path)
    status = compute_commit_drift(
        _VERIFIED,
        None,
        caller_origin=None,
        body="The value in a.py is one.",
        claims=["a.py::VALUE=1"],
        verified_head=None,
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert isinstance(status, CommitDriftStatus), status
    assert status.basis == BASIS_AUTHOR_DATE
    assert status.commits_since_verify >= 1, status
    assert status.status == "drift"


def test_author_date_count_holds_a_merged_change_then_revert(tmp_path: Path) -> None:
    """Two commits authored after the stamp touched a.py on a merged side
    branch; the reachable basis counts 2, so must the dates."""
    repo, verified_head = _revert_repo(tmp_path)
    walk = commits_since_anchor(repo, verified_head, toplevel=repo)
    assert walk is not None and len(walk.shas_touching(["a.py"])) == 2
    count = resolve_commit_drift_count(
        cwd=repo, since=_VERIFIED, unfiltered=4, anchors=["a.py"], toplevel=repo
    )
    assert count == 2
