"""Claim 16: a relative attestation with a ``:line`` suffix (or trailing
punctuation) is accepted by the write gate and read by path drift as the
file, but reaches commit drift unnormalised and matches no commit."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    ClaimAnchor,
    CommitDriftStatus,
    commit_drift_anchor_paths,
    compute_commit_drift,
    detect_path_drift,
    unverifiable_attestations,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)
_BODY = "The loader reads its settings once at start."
_SPELLINGS = ["src/app.py", "src/app.py:2", "src/app.py:2-9", "src/app.py,"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    made = tmp_path / "home"
    made.mkdir()
    monkeypatch.setenv("HOME", str(made))
    monkeypatch.setenv("USERPROFILE", str(made))
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
    return made


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


def _repo(home: Path) -> tuple[Path, str]:
    repo = home / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "src/app.py", "a = 1\nb = 1\n", _T0)
    _commit(repo, "src/other.py", "y = 1\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "src/app.py", "a = 1\nb = 2\n", _VERIFIED + timedelta(hours=1))
    _commit(repo, "src/other.py", "y = 2\n", _VERIFIED + timedelta(hours=2))
    return Path(os.path.realpath(repo)), head


@pytest.mark.parametrize("spelled", _SPELLINGS)
def test_write_gate_and_path_drift_read_the_spelling_as_the_file(
    home: Path, spelled: str
) -> None:
    """Precondition (passes today): the other two readers take the
    spelling as ``src/app.py``."""
    root, _ = _repo(home)
    anchor = ClaimAnchor(root=str(root), rule="home_checkout")
    assert unverifiable_attestations([spelled], anchor=anchor) == []
    # a spelling of a file that is not there is refused: the gate reads it
    assert unverifiable_attestations(
        [spelled.replace("app", "gone")], anchor=anchor
    ) == [str(root / "src" / "gone.py")]
    report = detect_path_drift(
        "See src/app.py for the loader.", verified_paths=[spelled], anchor=anchor
    )
    assert [os.path.realpath(p) for p in report.verified] == [
        str(root / "src" / "app.py")
    ]


@pytest.mark.parametrize("spelled", _SPELLINGS)
def test_anchor_is_the_file(spelled: str) -> None:
    assert commit_drift_anchor_paths("", [spelled]) == ("src/app.py",)


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize("spelled", _SPELLINGS)
@pytest.mark.parametrize("body", ["", _BODY])
def test_relative_attestation_counts_its_commit(
    home: Path, basis: str, spelled: str, body: str
) -> None:
    root, head = _repo(home)
    status = compute_commit_drift(
        _VERIFIED,
        None,
        caller_origin=None,
        verified_paths=[spelled],
        body=body,
        verified_head=head if basis == BASIS_REACHABILITY else None,
        anchor=ClaimAnchor(root=str(root), rule="home_checkout"),
    )
    assert isinstance(status, CommitDriftStatus), (spelled, status)
    assert status.status == "drift", spelled
    assert status.commits_since_verify == 1, spelled
    assert status.basis == basis, spelled
