"""One call shares its Git budget; a warm estate retains twenty roots."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, _response, origin_history
from bettermemory import git_read_budget

from ._mcp import call_tool
from .test_commit_drift_cache import _git_args, _whole_history_logs
from .test_home_commit_leg import World, _repo, neutral_git, this_machine, world

__all__ = ["neutral_git", "this_machine", "world"]


@pytest.mark.parametrize("basis", ["reachability", "author-date"])
async def test_a_warm_search_retains_twenty_home_histories(
    world: World, monkeypatch: pytest.MonkeyPatch, basis: str
) -> None:
    # Isolate call-count caching from wall-clock speed on native runners;
    # the separate fake-clock cases exercise the shared time budget.
    monkeypatch.setattr(git_read_budget, "_clock", lambda: 0.0)
    original = world.home
    ids = []
    try:
        for index in range(20):
            world.home = (
                original
                if index == 0
                else _repo(
                    original.parent / f"budget-{index:02}",
                    f"git@github.com:example/budget-{index}.git",
                )
            )
            ids.append(await world.homed("commit"))
            if basis == "author-date":
                record = world.store.load_one(ids[-1])
                world.store.update(record.model_copy(update={"verified_head": None}))
    finally:
        world.home = original
    world.stand_at(world.other)
    real = subprocess.run
    calls: list[tuple[Path | None, tuple[str, ...]]] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and argv and argv[0] == "git":
            cwd = kwargs.get("cwd")
            calls.append((Path(cwd) if cwd is not None else None, _git_args(argv)))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    _caches.clear_all()
    for turn in range(3):
        calls.clear()
        found = await call_tool(
            world.server,
            "memory_search",
            {
                "query": "zebrafish loader",
                "auto_scope": False,
                "max_results": 30,
            },
        )
        hits = found["result"] if isinstance(found, dict) else found
        assert {hit["id"] for hit in hits} == set(ids)
        if basis == "reachability":
            assert all(hit["commit_drift_count"] == 0 for hit in hits)
        else:
            assert all(hit["commit_drift_unread"] for hit in hits)
            assert all(
                hit["staleness_verdict"] == "spot_check_required" for hit in hits
            )
        whole = [
            (cwd, command) for cwd, command in calls if _whole_history_logs([command])
        ]
        if turn == 0:
            assert len(whole) == (20 if basis == "author-date" else 0)
        else:
            assert len(whole) <= 1 and all(cwd == world.other for cwd, _ in whole), (
                "a warm search re-read a home's whole history",
                whole,
            )


@pytest.mark.parametrize("cold_again", [False, True])
@pytest.mark.parametrize("count", [3, 4])
@pytest.mark.parametrize("bad_first", [False, True])
async def test_home_history_reads_share_one_call_deadline(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    cold_again: bool,
    count: int,
    bad_first: bool,
) -> None:
    original = world.home
    roots: set[Path] = set()
    ids = []
    try:
        for index in range(count):
            world.home = (
                original
                if index == 0
                else _repo(
                    original.parent / f"slow-{index}",
                    f"git@github.com:example/slow-{index}.git",
                )
            )
            roots.add(world.home)
            rid = await world.homed("commit")
            record = world.store.load_one(rid)
            world.store.update(record.model_copy(update={"verified_head": None}))
            ids.append(rid)
    finally:
        world.home = original
    world.stand_at(world.other)
    real = subprocess.run
    elapsed = 0.0
    timeouts: list[float] = []
    completed: set[Path] = set()
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
    # A zero origin keeps the integral fake costs exact across clock epochs.
    monkeypatch.setattr(git_read_budget, "_clock", lambda: elapsed)
    _caches.clear_all()
    first_order: list[str] | None = None
    for _ in range(count + 1):
        if cold_again:
            _caches.clear_all()
        elapsed = 0.0
        timeouts.clear()
        found = await call_tool(
            world.server,
            "memory_search",
            {
                "query": "zebrafish loader",
                "auto_scope": False,
                "max_results": 30,
                "expand_top": cold_again,
            },
        )
        hits = found["result"] if isinstance(found, dict) else found
        order = [hit["id"] for hit in hits]
        assert set(order) == set(ids)
        if first_order is None:
            first_order = order
        assert order == first_order, "read scheduling changed result ranking"
        assert all(hit["staleness_verdict"] != "fresh" for hit in hits)
        assert elapsed <= 5.0, ("home reads each received a fresh timeout", timeouts)
        cutoff = origin_history.HistoryUnread.CALL_BUDGET
        assert all(value is not cutoff for value in _response._DRIFT_MEMO.values())
        assert all(
            value[-1] is not cutoff for value in _response._DRIFT_TIMED_OUT.values()
        )
        assert all(
            value[-1] is not cutoff
            for value in origin_history._TIMESTAMPS_FAILED.values()
        )
    expected = roots - ({bad_root} if bad_root is not None else set())
    assert completed == expected, "a deferred home never received a complete read"
