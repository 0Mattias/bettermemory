"""An attested file counts the commits that changed it, however the
attestation spells the home directory.

`commit_drift_anchor_paths` handed the attestations to the pathspec
resolver as they were spelled. ``~/proj/src/x.py`` resolved, but
``$HOME/proj/src/x.py``, which the body scan and the write gate both read
as the same path, reached the resolver as a relative path, was joined
onto the repository root, and became a pathspec no commit touches: the
count was None, which never demotes and never escalates, so a record
whose attested file changed read fresh (ENG-167). Attestations now pass
the validator the body scan applies (`verify._attested_anchor`).
"""

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
    commit_drift_anchor_paths,
    compute_commit_drift,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)
_BODY = "The loader reads its settings once at start."


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory under `tmp_path`, ``~`` expanding to it, no user
    or system git configuration and no inherited repository variables."""
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
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env
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
    """``~/proj``, verified at `_VERIFIED` with HEAD at its first commit,
    then one commit to the attested file and one to another file."""
    repo = home / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "src/x.py", "x = 1\n", _T0)
    _commit(repo, "src/other.py", "y = 1\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "src/x.py", "x = 2\n", _VERIFIED + timedelta(hours=1))
    _commit(repo, "src/other.py", "y = 2\n", _VERIFIED + timedelta(hours=2))
    return repo, head


def _spellings(home: Path) -> list[str]:
    return [
        "$HOME/proj/src/x.py",
        "${HOME}/proj/src/x.py",
        "~/proj/src/x.py",
        str(home / "proj" / "src" / "x.py"),
    ]


def test_every_spelling_of_the_home_is_one_anchor(home: Path) -> None:
    for spelled in _spellings(home):
        anchors = commit_drift_anchor_paths(_BODY, [spelled])
        assert len(anchors) == 1, spelled
        expanded = os.path.normpath(os.path.expanduser(anchors[0]))
        assert expanded == os.path.normpath(home / "proj" / "src" / "x.py"), spelled


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_an_attested_file_counts_its_commit_under_every_spelling(
    home: Path, basis: str
) -> None:
    repo, head = _repo(home)
    root = Path(os.path.realpath(repo))
    for spelled in _spellings(home):
        status = compute_commit_drift(
            _VERIFIED,
            None,
            caller_origin=None,
            verified_paths=[spelled],
            body=_BODY,
            verified_head=head if basis == BASIS_REACHABILITY else None,
            anchor=ClaimAnchor(root=str(root), rule="home_checkout"),
        )
        assert status is not None, spelled
        assert status.status == "drift", spelled
        assert status.commits_since_verify == 1, spelled
        assert status.basis == basis, spelled
