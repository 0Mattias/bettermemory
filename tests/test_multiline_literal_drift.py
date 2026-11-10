"""Claim 49: a scalar/str literal claim whose assignment spans physical
lines is put in the governed leg, and an edit to its continuation line
(no column-0 binding line, under the 12-character anchor floor) reads
count 0 / clean while `check_claim` calls the claim false at HEAD."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.claims import check_claim, parse_claim
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

# (claim, module at verify time, module after the falsifying commit)
_CASES = {
    "paren_int": (
        "mod.py::TIMEOUT=30",
        "TIMEOUT = (\n    30\n)\n",
        "TIMEOUT = (\n    300\n)\n",
    ),
    "backslash_int": (
        "mod.py::X=30",
        "X = \\\n    30\n",
        "X = \\\n    300\n",
    ),
    "implicit_concat_short": (
        "mod.py::NAME='abcd'",
        'NAME = (\n    "ab"\n    "cd"\n)\n',
        'NAME = (\n    "ab"\n    "ce"\n)\n',
    ),
    "triple_quoted_short": (
        "mod.py::HELP='\\nusage: x\\n-h help\\n'",
        'HELP = """\nusage: x\n-h help\n"""\n',
        'HELP = """\nusage: x\n-h  usage\n"""\n',
    ),
}


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


def _repo(tmp_path: Path, case: str) -> tuple[Path, str, str]:
    """A repo verified at `_VERIFIED` (HEAD then = the first two commits),
    then one commit editing the claimed assignment's continuation line.
    Root, verified head, claim."""
    claim, before, after = _CASES[case]
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "mod.py", before, _T0)
    _commit(repo, "other.py", "x = 0\n", _T0)
    repo = Path(os.path.realpath(repo))
    # Premise: the claim holds at the verified head (declare-time oracle).
    assert check_claim(parse_claim(claim), repo) is None
    head = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "mod.py", after, _VERIFIED + timedelta(hours=1))
    # Premise: the oracle calls the claim false at HEAD.
    reason = check_claim(parse_claim(claim), repo)
    assert reason is not None and "is currently" in reason
    return repo, head, claim


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_continuation_line_edit_counts(tmp_path: Path, case: str, basis: str) -> None:
    repo, head, claim = _repo(tmp_path, case)
    walk = None
    if basis == BASIS_REACHABILITY:
        walk = commits_since_anchor(repo, head, toplevel=repo)
        assert walk is not None and len(walk.commits) == 1
    resolved = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=1,
        anchors=(),
        claims=[parse_claim(claim)],
        toplevel=repo,
        walk=walk,
    )
    assert resolved is not None
    assert resolved.basis == basis
    # The commit falsified the claim; the file's any-touch rule (no claim
    # declared) would count it. A claim is "never worse than" that floor.
    assert resolved.count == 1, resolved

    # Control: the same memory citing the file without a claim counts it.
    unclaimed = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=1,
        anchors=(str(repo / "mod.py"),),
        claims=(),
        toplevel=repo,
        walk=walk,
    )
    assert unclaimed is not None and unclaimed.count == 1


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_record_reads_drift_after_the_falsifying_commit(
    tmp_path: Path, basis: str
) -> None:
    """The commit leg as `memory_show` reads it."""
    repo, head, claim = _repo(tmp_path, "paren_int")
    status = compute_commit_drift(
        _VERIFIED,
        None,
        caller_origin=None,
        body="The timeout is thirty seconds.",
        claims=[claim],
        verified_head=head if basis == BASIS_REACHABILITY else None,
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert isinstance(status, CommitDriftStatus)
    assert status.status == "drift", status
    assert status.commits_since_verify == 1


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("multiline", [True, False])
def test_dirty_worktree_layout_cannot_change_the_committed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, basis: str, multiline: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    binding = "X = (\n    30\n)\n" if multiline else "X = 30\n"
    _commit(repo, "mod.py", binding + "def f():\n    return 1\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    _commit(
        repo,
        "mod.py",
        binding + "def f():\n    return 2\n",
        _VERIFIED + timedelta(hours=1),
    )
    module = repo / "mod.py"
    read_text = Path.read_text

    def no_worktree_read(path: Path, *args, **kwargs):
        assert path != module, "the committed layout read the worktree"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_worktree_read)
    for dirty in ("X = 30\n", "X = (\n    30\n)\n"):
        module.write_text(dirty + "def f():\n    return 99\n")
        walk = (
            commits_since_anchor(repo, head, toplevel=repo)
            if basis == BASIS_REACHABILITY
            else None
        )
        result = resolve_commit_drift(
            cwd=repo,
            since=_VERIFIED,
            unfiltered=1,
            anchors=(),
            claims=[parse_claim("mod.py::X=30")],
            toplevel=repo,
            walk=walk,
        )
        assert result is not None and result.count == (1 if multiline else 0)
        assert result.claims_drifted == ()


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_a_multiline_window_still_counts_when_head_is_single_line(
    tmp_path: Path, basis: str
) -> None:
    repo, head, claim = _repo(tmp_path, "paren_int")
    _commit(repo, "mod.py", "TIMEOUT = 300\n", _VERIFIED + timedelta(hours=2))
    walk = (
        commits_since_anchor(repo, head, toplevel=repo)
        if basis == BASIS_REACHABILITY
        else None
    )
    result = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=2,
        anchors=(),
        claims=[parse_claim(claim)],
        toplevel=repo,
        walk=walk,
    )
    assert result is not None and result.count == 2
    # The collapse touches its binding line, so the patch can name that
    # claim independently of the additional file-touch floor.
    assert result.claims_drifted == (claim,)


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_the_literal_floor_does_not_count_another_claimed_files_body_churn(
    tmp_path: Path, basis: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "mod.py", "X = (\n    30\n)\n", _T0)
    _commit(repo, "other.py", "def f():\n    return 1\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "mod.py", "X = (\n    300\n)\n", _VERIFIED + timedelta(hours=1))
    _commit(
        repo, "other.py", "def f():\n    return 2\n", _VERIFIED + timedelta(hours=2)
    )
    walk = (
        commits_since_anchor(repo, head, toplevel=repo)
        if basis == BASIS_REACHABILITY
        else None
    )
    result = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=2,
        anchors=(),
        claims=[parse_claim("mod.py::X=30"), parse_claim("other.py::f")],
        toplevel=repo,
        walk=walk,
    )
    assert result is not None and result.count == 1
    assert result.claims_drifted == ()


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_a_literal_floor_cannot_suppress_a_bare_claims_whole_window(
    tmp_path: Path, basis: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "mod.py", "X = (\n    30\n)\n", _T0)
    _commit(repo, "x.py", "one\n", _T0)
    _commit(repo, "other.py", "def f():\n    return 1\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "mod.py", "X = (\n    300\n)\n", _VERIFIED + timedelta(hours=1))
    (repo / "x.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete x", when=_VERIFIED + timedelta(hours=2))
    _commit(
        repo, "other.py", "def f():\n    return 2\n", _VERIFIED + timedelta(hours=3)
    )
    walk = (
        commits_since_anchor(repo, head, toplevel=repo)
        if basis == BASIS_REACHABILITY
        else None
    )
    result = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=3,
        anchors=(),
        claims=[
            parse_claim("mod.py::X=30"),
            parse_claim("x.py"),
            parse_claim("other.py::f"),
        ],
        toplevel=repo,
        walk=walk,
    )
    assert result is not None and result.count == 3
    assert result.claims_drifted == ("x.py",)


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_a_literal_after_a_semicolon_keeps_its_file_floor(
    tmp_path: Path, basis: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "mod.py", "noise = 0; X = 30\n", _T0)
    claim = parse_claim("mod.py::X=30")
    assert check_claim(claim, repo) is None
    head = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "mod.py", "noise = 0; X = 300\n", _VERIFIED + timedelta(hours=1))
    walk = (
        commits_since_anchor(repo, head, toplevel=repo)
        if basis == BASIS_REACHABILITY
        else None
    )
    result = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=1,
        anchors=(),
        claims=[claim],
        toplevel=repo,
        walk=walk,
    )
    assert result is not None and result.count == 1
