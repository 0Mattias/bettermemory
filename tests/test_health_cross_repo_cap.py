"""The estate check's per-run root cap bounds the roots git runs in.

`health._compute_cross_repo_drift` checked `_CROSS_REPO_MAX_ROOTS` at
the top of each group, but a group skipped after `origin.capture` (no
checkout identified, its repo unread or another repo's) did not consume
it, and `capture` runs several git processes. A store with records from
many foreign directories that no longer re-identify ran git in every one
of them to keep eight (ENG-169 G6). Every root git runs in now takes a
place; the stat-only skips (a worktree gone or unreadable) stay free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import health
from bettermemory.health_model import _CROSS_REPO_MAX_ROOTS
from bettermemory.health import compute_health
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin

_PAST_CAP = 4


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _foreign(root: Path, index: int) -> Memory:
    """A verified record citing a file, written in its own foreign
    checkout `root` of its own repository."""
    return Memory(
        id=generate_ulid(),
        created=_utc(2026, 1, 2),
        updated=_utc(2026, 1, 2),
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="cites `src/app.py`\n",
        last_verified_at=_utc(2026, 2, 1),
        origin=Origin(
            cwd=str(root),
            repo=f"https://github.com/example/foreign-{index}.git",
            worktree_root=str(root),
        ),
    )


def test_git_runs_in_at_most_the_capped_number_of_foreign_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = _CROSS_REPO_MAX_ROOTS
    memories = []
    for index in range(cap + _PAST_CAP):
        root = tmp_path / f"foreign-{index}"
        root.mkdir()
        memories.append(_foreign(root, index))
    captured: list[Path] = []

    def capture(root: Path) -> Origin:
        """What `capture` answers where git identifies no checkout."""
        captured.append(Path(root))
        return Origin(cwd=str(root), repo=None, worktree_root=None)

    monkeypatch.setattr(health, "capture", capture)
    caller = Origin(
        cwd=str(tmp_path),
        repo="https://github.com/example/caller.git",
        worktree_root=str(tmp_path),
    )
    report = compute_health(memories, [], caller_origin=caller, now=_utc(2026, 4, 1))

    assert len(captured) == cap
    xr = report.cross_repo_drift
    assert xr is not None
    reasons = sorted(s["reason"] for s in xr.skipped)
    assert reasons == sorted(
        ["no checkout identified in the recorded worktree"] * cap
        + ["past the per-run root cap"] * _PAST_CAP
    )


def test_a_root_gone_from_the_disk_takes_no_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded worktree positively absent is skipped on a stat alone,
    before git runs, so it leaves the places to the roots that exist."""
    cap = _CROSS_REPO_MAX_ROOTS
    memories = [
        _foreign(tmp_path / f"gone-{index}", index) for index in range(_PAST_CAP)
    ]
    for index in range(cap):
        root = tmp_path / f"foreign-{index}"
        root.mkdir()
        memories.append(_foreign(root, _PAST_CAP + index))
    captured: list[Path] = []

    def capture(root: Path) -> Origin:
        captured.append(Path(root))
        return Origin(cwd=str(root), repo=None, worktree_root=None)

    monkeypatch.setattr(health, "capture", capture)
    caller = Origin(
        cwd=str(tmp_path),
        repo="https://github.com/example/caller.git",
        worktree_root=str(tmp_path),
    )
    report = compute_health(memories, [], caller_origin=caller, now=_utc(2026, 4, 1))

    assert len(captured) == cap
    xr = report.cross_repo_drift
    assert xr is not None
    reasons = [s["reason"] for s in xr.skipped]
    assert reasons.count("worktree missing on disk") == _PAST_CAP
    assert "past the per-run root cap" not in reasons
