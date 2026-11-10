"""Claim 31: an absence claim on a directory path (`!legacy`) is not fired
by a commit that creates a file under it (`legacy/old.py`), though the
path is governed (taken out of any-touch) and `check_claim` now calls the
claim false."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.claims import (
    COMMIT_MARK,
    build_binding_index,
    check_claim,
    claim_level_drift,
    governed_claim_paths,
    parse_claim,
)
from bettermemory.origin import commits_since_anchor
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    ClaimAnchor,
    CommitDriftStatus,
    compute_commit_drift,
    resolve_commit_drift,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)
_BODY = "The legacy package was removed and must stay gone."


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


def _commit(repo: Path, rel: str, text: str, when: datetime) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", rel, when=when)


def _repo(tmp_path: Path, claim: str) -> tuple[Path, str]:
    """A repository whose claim held at the verify (nothing at `legacy`),
    then one commit that adds `legacy/old.py`. Root and verified head."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "src/other.py", "x = 0\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    # The declare-time oracle accepts the claim: nothing is there.
    assert check_claim(parse_claim(claim), repo) is None
    _commit(
        repo,
        "legacy/old.py",
        "def old():\n    return 1\n",
        _VERIFIED + timedelta(hours=1),
    )
    # ... and now calls it false.
    assert "exists in the worktree" in (check_claim(parse_claim(claim), repo) or "")
    return Path(os.path.realpath(repo)), head


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("claim", ["!legacy/old.py", "!legacy"])
def test_resolve_counts_the_commit_that_recreates_the_absent_path(
    tmp_path: Path, basis: str, claim: str
) -> None:
    repo, head = _repo(tmp_path, claim)
    parsed = [parse_claim(claim)]
    # The path is governed: any-touch no longer covers it.
    assert governed_claim_paths(parsed) == [parsed[0].rel_path]
    walk = None
    if basis == BASIS_REACHABILITY:
        walk = commits_since_anchor(repo, head, toplevel=repo)
        assert walk is not None and len(walk.commits) == 1
    resolved = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=1,
        anchors=(),
        claims=parsed,
        toplevel=repo,
        walk=walk,
    )
    assert resolved is not None
    assert resolved.basis == basis
    assert resolved.claims_checked == 1
    assert resolved.count == 1, resolved
    assert resolved.claims_drifted == (claim,)


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("claim", ["!legacy/old.py", "!legacy"])
def test_the_record_reads_drift_after_the_recreating_commit(
    tmp_path: Path, basis: str, claim: str
) -> None:
    repo, head = _repo(tmp_path, claim)
    status = compute_commit_drift(
        _VERIFIED,
        None,
        caller_origin=None,
        body=_BODY,
        claims=[claim],
        verified_head=head if basis == BASIS_REACHABILITY else None,
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert isinstance(status, CommitDriftStatus), status
    assert status.basis == basis
    assert status.status == "drift", status
    assert status.commits_since_verify == 1


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("kind", ["descendants", "header only", "sibling", "removed"])
def test_directory_absence_counts_real_descendant_commits_once(
    tmp_path: Path, basis: str, kind: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "other.py", "x = 0\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    names = (
        ["legacy2/file.py"]
        if kind == "sibling"
        else ["legacy/a.py", "legacy/deep/b.py"]
    )
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("" if kind == "header only" else "x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add files", when=_VERIFIED + timedelta(hours=1))
    if kind == "removed":
        for name in names:
            (repo / name).unlink()
        _git(repo, "add", "-A")
        _git(
            repo,
            "commit",
            "-q",
            "-m",
            "remove files",
            when=_VERIFIED + timedelta(hours=2),
        )
    claims = [parse_claim("!legacy"), parse_claim("!legacy/a.py")]
    walk = (
        commits_since_anchor(repo, head, toplevel=repo)
        if basis == BASIS_REACHABILITY
        else None
    )
    resolved = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=2 if kind == "removed" else 1,
        anchors=(),
        claims=claims,
        toplevel=repo,
        walk=walk,
    )
    if kind == "sibling":
        # Neither absent path has ever appeared in this history: the
        # sibling must not turn a phantom anchor into an applicable one.
        assert resolved is None
    else:
        assert resolved is not None
        assert resolved.count == (2 if kind == "removed" else 1)
        assert resolved.claims_drifted == ("!legacy", "!legacy/a.py")
    stream = _git(repo, "log", f"--format={COMMIT_MARK}%H", "-p", f"{head}..HEAD")
    result = claim_level_drift(claims[0], build_binding_index(stream))
    assert result["weak"] == (kind != "sibling")
    assert result["strict"] == (kind not in ("sibling", "removed"))
