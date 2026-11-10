"""A cold show has one shared deadline across its failed history probes."""

from __future__ import annotations

import importlib
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches
from bettermemory import _git, _response, git_read_budget

from ._mcp import call_tool
from .test_commit_drift_cache import _git_args
from .test_home_commit_leg import World, _since, neutral_git, this_machine, world

__all__ = ["neutral_git", "this_machine", "world"]


async def test_show_does_not_restart_the_deadline_for_its_listing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = await world.homed("commit")
    _since(world.home, "commit")
    world.stand_at(world.other)
    real = subprocess.run
    elapsed = 0.0
    started = time.monotonic()
    timeouts: list[float] = []

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal elapsed
        cwd = kwargs.get("cwd")
        if (
            isinstance(argv, list)
            and argv
            and argv[0] == "git"
            and cwd is not None
            and Path(cwd) == world.home
            and _git_args(argv)[:1] == ("log",)
        ):
            timeout = float(kwargs["timeout"])
            timeouts.append(timeout)
            elapsed += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)

    def clock() -> float:
        return started + elapsed

    # The same probe runs against the recorded pre-fix snapshot, where
    # the shared-budget module does not exist yet.
    try:
        budget = importlib.import_module("bettermemory.git_read_budget")
    except ModuleNotFoundError as exc:
        if exc.name != "bettermemory.git_read_budget":
            raise
        monkeypatch.setattr(time, "monotonic", clock)
    else:
        monkeypatch.setattr(budget, "_clock", clock)
    _caches.clear_all()
    shown = await call_tool(world.server, "memory_show", {"id": rid})
    assert shown["commit_drift"] is None and shown["commit_drift_unread"] is True
    assert shown["staleness_verdict"] == "spot_check_required"
    assert timeouts, "the cold show never attempted its history"
    assert elapsed <= 5.0, ("show restarted a full timeout for a fallback", timeouts)


async def test_show_applicability_metadata_shares_the_deadline(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = await world.homed("commit")
    world.stand_at(world.other)
    elapsed = 0.0
    in_applicability = False
    real = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal elapsed
        if in_applicability:
            elapsed += min(1.0, float(kwargs["timeout"]))
        return real(argv, *args, **kwargs)

    def no_count(*args: Any, **kwargs: Any) -> tuple[bool, Any]:
        return True, None

    def history_applies(reader: Any) -> bool:
        nonlocal in_applicability
        in_applicability = True
        try:
            # A None leg still asks whether history applies at its root.
            # These metadata probes must remain inside the commit reader.
            for _ in range(6):
                result = _git.run_git(
                    ("rev-parse", "HEAD"), cwd=reader.cwd, timeout=1.0
                )
                assert result is not None and result.returncode == 0
            return True
        finally:
            in_applicability = False

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(git_read_budget, "_clock", lambda: elapsed)
    monkeypatch.setattr(_response._DriftPass, "lookup", no_count)
    monkeypatch.setattr(_response._DriftPass, "history_applies", history_applies)
    shown = await call_tool(world.server, "memory_show", {"id": rid})
    assert shown["commit_drift"] is None and shown["commit_drift_unread"] is True
    assert shown["staleness_verdict"] == "spot_check_required"
    assert elapsed == 5.0
