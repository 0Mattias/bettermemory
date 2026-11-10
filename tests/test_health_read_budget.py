"""Health shares its Git-read deadline and rotates roots deferred by time."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, git_read_budget
from bettermemory.health import compute_health
from bettermemory.health_roots import EstateProgress
from bettermemory.models import Memory
from bettermemory.origin import Origin

from .test_commit_drift_cache import _git_args, _whole_history_logs
from .test_health_root_rotation import _NOW, _caller, home, memories

__all__ = ["home", "memories"]


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("count", [8, 9])
@pytest.mark.parametrize("bad_first", [False, True])
def test_health_time_budget_preserves_eventual_coverage(
    home: Path,
    memories: list[Memory],
    monkeypatch: pytest.MonkeyPatch,
    offline: bool,
    count: int,
    bad_first: bool,
) -> None:
    memories = memories[:count]
    roots = {
        Path(m.origin.worktree_root)
        for m in memories
        if m.origin is not None and m.origin.worktree_root is not None
    }
    real = subprocess.run
    elapsed = 0.0
    completed: set[Path] = set()
    timeouts: list[float] = []
    bad_root: Path | None = None

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal elapsed, bad_root
        cwd = kwargs.get("cwd")
        if (
            isinstance(argv, list)
            and argv
            and argv[0] == "git"
            and cwd is not None
            and Path(cwd) in roots
            and _whole_history_logs([_git_args(argv)])
        ):
            timeout = float(kwargs["timeout"])
            if bad_first and bad_root is None:
                bad_root = Path(cwd)
            cost = 5.0 if Path(cwd) == bad_root else 3.0
            timeouts.append(timeout)
            elapsed += min(cost, timeout)
            if Path(cwd) == bad_root or timeout < cost:
                raise subprocess.TimeoutExpired(argv, timeout)
            completed.add(Path(cwd))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(git_read_budget, "_clock", lambda: elapsed)
    progress = EstateProgress()
    for _ in range(1 if offline else len(roots)):
        _caches.clear_all()
        elapsed = 0.0
        report = compute_health(
            memories,
            [],
            caller_origin=_caller(home),
            now=_NOW,
            estate_progress=progress,
            estate_root_limit=None if offline else 8,
        )
        estate = report.cross_repo_drift
        assert estate is not None
        if offline:
            assert len(estate.groups) == len(roots)
            assert elapsed == 3.0 * len(roots) + (2.0 if bad_first else 0.0)
        else:
            assert elapsed <= 5.0, timeouts
            assert len(estate.groups) in (1, 2)
            assert any(g.unread_causes == {"call_budget": 1} for g in estate.groups)
            assert all(
                skip["last_checked_at"] is None
                or skip["worktree_root"] in progress.checked
                for skip in estate.skipped
            )
    expected = roots - ({bad_root} if bad_root is not None else set())
    assert completed == expected, "a deferred root never received a complete read"


def test_health_progress_lock_wait_spends_the_call_deadline(
    home: Path, memories: list[Memory], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use a short real lock deadline, without a latency benchmark: a
    # worker must return an honest skipped report while another thread
    # owns this Store's cursor, rather than waiting indefinitely.
    monkeypatch.setattr(git_read_budget, "READ_SECONDS", 0.04)
    progress = EstateProgress()
    progress.lock.acquire()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        compute_health,
        memories,
        [],
        caller_origin=_caller(home),
        now=_NOW,
        estate_progress=progress,
    )
    try:
        report = future.result(timeout=2)
    finally:
        progress.lock.release()
        pool.shutdown(wait=True)
    estate = report.cross_repo_drift
    assert estate is not None
    assert not estate.groups and len(estate.skipped) == len(memories)
    assert all("waiting for health progress" in row["reason"] for row in estate.skipped)
    assert all(row["last_checked_at"] is None for row in estate.skipped)
    assert progress.cursor is None and not progress.checked


def test_a_slow_caller_cannot_starve_foreign_homes(
    home: Path, memories: list[Memory], monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = memories[0].origin
    assert caller is not None and caller.worktree_root is not None
    caller_root = Path(caller.worktree_root)
    foreign = {
        Path(m.origin.worktree_root)
        for m in memories[1:]
        if m.origin is not None and m.origin.worktree_root is not None
    }
    completed: set[Path] = set()
    elapsed = 0.0
    real = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal elapsed
        cwd = kwargs.get("cwd")
        if (
            isinstance(argv, list)
            and argv
            and argv[0] == "git"
            and cwd is not None
            and _whole_history_logs([_git_args(argv)])
        ):
            root = Path(cwd)
            cost = 5.0 if root == caller_root else 3.0
            timeout = float(kwargs["timeout"])
            elapsed += min(cost, timeout)
            if root == caller_root or timeout < cost:
                raise subprocess.TimeoutExpired(argv, timeout)
            completed.add(root)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(git_read_budget, "_clock", lambda: elapsed)
    progress = EstateProgress()
    for _ in range(2 * len(foreign)):
        _caches.clear_all()
        elapsed = 0.0
        report = compute_health(
            memories,
            [],
            caller_origin=Origin(**caller.model_dump()),
            now=_NOW,
            estate_progress=progress,
        )
        assert elapsed <= 5.0
        assert report.commit_drift_debt is not None
        assert report.commit_drift_debt.unread == 1
    assert completed == foreign, "the caller consumed every home's turn"


def test_an_inapplicable_caller_discloses_its_deferred_home(
    home: Path, memories: list[Memory], monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = memories[0].origin
    assert origin is not None
    caller = Origin(cwd=str(home), repo=origin.repo, worktree_root=str(home))
    monkeypatch.setattr(git_read_budget, "_clock", lambda: 0.0)
    progress = EstateProgress(caller_first=False)
    report = compute_health(
        memories,
        [],
        caller_origin=caller,
        now=_NOW,
        estate_progress=progress,
    )
    assert report.commit_drift_debt is None
    estate = report.cross_repo_drift
    assert estate is not None and estate.total_drifted == 8
    pending = [
        row for row in estate.skipped if row["worktree_root"] == origin.worktree_root
    ]
    assert len(pending) == 1 and "caller" in pending[0]["reason"]
    assert pending[0]["last_checked_at"] is None
    later = compute_health(
        memories,
        [],
        caller_origin=caller,
        now=_NOW,
        estate_progress=progress,
    ).cross_repo_drift
    assert later is not None
    assert any(group.worktree_root == origin.worktree_root for group in later.groups)


def test_offline_fallback_keeps_the_exhausted_root_budget(
    memories: list[Memory], monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = {
        Path(m.origin.worktree_root)
        for m in memories
        if m.origin is not None and m.origin.worktree_root is not None
    }
    costs: dict[Path, float] = {}
    elapsed = 0.0
    real = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal elapsed
        cwd = kwargs.get("cwd")
        if isinstance(argv, list) and argv and argv[0] == "git" and cwd is not None:
            root = Path(cwd)
            if root in roots:
                timeout = float(kwargs["timeout"])
                cost = min(1.0, timeout)
                costs[root] = costs.get(root, 0.0) + cost
                elapsed += cost
                if timeout < 1.0:
                    raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(git_read_budget, "_clock", lambda: elapsed)
    report = compute_health(memories, [], now=_NOW, estate_root_limit=None)
    assert report.cross_repo_drift is not None
    assert len(report.cross_repo_drift.groups) == len(roots)
    assert set(costs) == roots
    assert all(cost <= 5.0 for cost in costs.values()), costs
