"""The live-store census's verdict read (`bench/rot/live_census.py`): the
commit leg counts in the memory's home checkout.

Since decision B23 `verify.compute_commit_drift` counts nothing without an
anchor, so the census passes the tree the read stands in as the memory's
home checkout, the way `bench/rot/run.py` does. A commit touching the
memory's attested path after its verify then moves the verdict off
``fresh``; with no such commit it stays ``fresh``.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from .test_membership_security import neutral_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_ROOT = Path(__file__).resolve().parents[1]
_CENSUS = _ROOT / "bench" / "rot" / "live_census.py"

#: A fixed instant the repository's commits and the verify are placed
#: around, as seconds since the epoch, so no clock is read.
_BASE = 1_700_000_000


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_rot_live_census", _CENSUS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_rot_live_census"] = module
    spec.loader.exec_module(module)
    return module


census = _load()


def _commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch, at: int, text: str, message: str
) -> None:
    """Write `text` to notes.md and commit it, authored and committed at
    `at` seconds since the epoch."""
    (repo / "notes.md").write_text(text, encoding="utf-8")
    stamp = f"@{at} +0000"
    monkeypatch.setenv("GIT_AUTHOR_DATE", stamp)
    monkeypatch.setenv("GIT_COMMITTER_DATE", stamp)
    for args in (("add", "notes.md"), ("commit", "-q", "-m", message)):
        done = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr


def _row(repo: Path) -> dict[str, Any]:
    """A census row for a memory written and verified in `repo`, an hour
    after the file it attests was committed."""
    return {
        "origin": {"cwd": str(repo), "worktree_root": str(repo)},
        "body": "The release checklist lives in notes.md.",
        "verified_paths": ["notes.md"],
        "absent_paths": [],
        "claims_raw": [],
        "last_verified_at": datetime.fromtimestamp(_BASE + 3_600, tz=timezone.utc),
    }


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    neutral_git(tmp_path, monkeypatch)
    path = tmp_path / "repo"
    path.mkdir()
    done = subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    _commit(path, monkeypatch, _BASE, "the checklist\n", "add the checklist")
    return path.resolve()


def test_the_commit_leg_counts_a_commit_after_the_verify(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.fromtimestamp(_BASE, tz=timezone.utc) + timedelta(days=1)
    row = _row(repo)
    assert census._verdict_from_own_worktree(row, 30, now) == "fresh"

    _commit(repo, monkeypatch, _BASE + 7_200, "the checklist, moved\n", "edit")

    assert census._verdict_from_own_worktree(row, 30, now) != "fresh"


def test_the_census_anchor_is_the_home_checkout(repo: Path) -> None:
    anchor = census._home_checkout(repo)
    assert anchor.commit_root(None) == (str(repo), "home_checkout")
