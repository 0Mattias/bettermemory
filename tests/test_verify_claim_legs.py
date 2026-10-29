"""A claim the detector cannot address counts on its own path, on both bases.

`claims.governed_claim_paths` leaves out a claimed file whose claims the
`weak` tier cannot address (a container literal: its interior lines move
without a column-0 binding line). That file belonged to neither leg of the
commit-drift narrowing (ENG-161): the ungoverned leg was built from the
cited and attested anchors alone. So a claims-only memory (no path in its
body, its one claim a container literal) read not-applicable on the
reachable basis, fresh across the commit that falsified its claim, and the
repository-wide count on the author-date basis, since the governed log ran
over an empty pathspec list, which answers "git could not answer".

Both bases now build their legs in `verify._claim_legs`, where an
unaddressable claim's path is ungoverned and falls back to any-touch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.claims import governed_claim_paths, parse_claim
from bettermemory.origin import commits_since_anchor
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    ClaimAnchor,
    compute_commit_drift,
    resolve_commit_drift,
    resolve_commit_drift_count,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)

#: A container literal claim: unaddressable, so its file is ungoverned.
_CLAIM = 'src/limits.py::LIMITS={"soft": 1, "hard": 2}'
_MODULE = 'LIMITS = {\n    "soft": 1,\n    "hard": 2,\n}\n'
_BODY = "The limits table holds a soft and a hard bound."


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration and no inherited repository
    variables."""
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


def _repo(tmp_path: Path, *, falsify: bool) -> tuple[Path, str]:
    """A repository verified at `_VERIFIED` with HEAD at the first commit,
    then, with `falsify`, one commit editing the claimed dict's interior,
    and three commits to an unrelated file. Its root and the verified
    head."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "src/limits.py", _MODULE, _T0)
    _commit(repo, "src/other.py", "x = 0\n", _T0)
    head = _git(repo, "rev-parse", "HEAD")
    at = _VERIFIED + timedelta(hours=1)
    if falsify:
        _commit(repo, "src/limits.py", _MODULE.replace('"hard": 2', '"hard": 3'), at)
    for n in range(1, 4):
        _commit(repo, "src/other.py", f"x = {n}\n", at + timedelta(hours=n))
    return Path(os.path.realpath(repo)), head


def test_premise_the_container_claim_is_ungoverned() -> None:
    assert governed_claim_paths([parse_claim(_CLAIM)]) == []


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize(("falsify", "expected"), [(True, 1), (False, 0)])
def test_a_claims_only_container_claim_counts_its_own_path(
    tmp_path: Path, basis: str, falsify: bool, expected: int
) -> None:
    """One falsifying commit counts 1 (not None, not the repo-wide 4);
    with only unrelated commits the count is 0 (not the repo-wide 3)."""
    repo, head = _repo(tmp_path, falsify=falsify)
    unfiltered = 4 if falsify else 3
    walk = None
    if basis == BASIS_REACHABILITY:
        walk = commits_since_anchor(repo, head, toplevel=repo)
        assert walk is not None and len(walk.commits) == unfiltered
    claims = [parse_claim(_CLAIM)]
    resolved = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=unfiltered,
        anchors=(),
        claims=claims,
        toplevel=repo,
        walk=walk,
    )
    assert resolved is not None
    assert resolved.count == expected
    assert resolved.basis == basis
    count = resolve_commit_drift_count(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=unfiltered,
        anchors=(),
        claims=claims,
        toplevel=repo,
        walk=walk,
    )
    assert count == expected


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_the_record_reads_drifted_after_the_falsifying_commit(
    tmp_path: Path, basis: str
) -> None:
    """The record as `memory_show` reads its commit leg: a claims-only
    memory verified before the commit that falsified its container claim
    reads drift with one commit, on either basis."""
    repo, head = _repo(tmp_path, falsify=True)
    status = compute_commit_drift(
        _VERIFIED,
        None,
        caller_origin=None,
        body=_BODY,
        claims=[_CLAIM],
        verified_head=head if basis == BASIS_REACHABILITY else None,
        anchor=ClaimAnchor(root=str(repo), rule="home_checkout"),
    )
    assert status is not None
    assert status.status == "drift"
    assert status.commits_since_verify == 1
    assert status.basis == basis
