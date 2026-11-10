"""Nested reads share a budget; independent asynchronous calls do not."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import git_read_budget as budget
from bettermemory import _git


async def test_concurrent_calls_keep_independent_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr(budget, "_clock", lambda: now)
    release = asyncio.Event()

    @budget.async_call
    async def read(ready: asyncio.Event) -> float:
        with budget.reading("synthetic-root"):
            assert budget.allowance(5.0, "synthetic-root", None)[0] == 5.0
            ready.set()
            await release.wait()
            return budget.allowance(5.0, "synthetic-root", None)[0]

    first_ready, second_ready = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(read(first_ready))
    await first_ready.wait()
    now = 3.0
    second = asyncio.create_task(read(second_ready))
    await second_ready.wait()
    now = 4.0
    release.set()
    first_left, second_left = await asyncio.gather(first, second)
    assert (first_left, second_left) == (1.0, 4.0)


async def test_nested_call_does_not_restart_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr(budget, "_clock", lambda: now)

    @budget.async_call
    async def inner() -> float:
        with budget.reading("nested-root"):
            return budget.allowance(5.0, "nested-root", None)[0]

    @budget.async_call
    async def outer() -> float:
        nonlocal now
        with budget.reading("outer-root"):
            assert budget.allowance(5.0, "outer-root", None)[0] == 5.0
        now = 3.0
        return await inner()

    assert await outer() == 2.0
    # Only commit readers activate it; an identity or write outside a
    # reader keeps its own timeout even while a read owner exists.
    with budget.call_scope():
        with budget.reading("expired-root"):
            budget.allowance(5.0, "expired-root", None)
        now = 20.0
        assert budget.allowance(5.0, "identity-root", None) == (5.0, False)


@pytest.mark.parametrize("spends_deadline", [False, True])
def test_a_timeout_is_a_call_cutoff_only_when_the_deadline_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spends_deadline: bool
) -> None:
    now = 0.0
    monkeypatch.setattr(budget, "_clock", lambda: now)

    def timeout(argv: Any, **kwargs: Any) -> Any:
        nonlocal now
        if spends_deadline:
            now += float(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with budget.call_scope(), budget.reading(tmp_path):
        budget.allowance(5.0, tmp_path, None)
        now = 1.0
        if spends_deadline:
            with pytest.raises(budget.ReadDeadline):
                _git.run_git(("log",), cwd=tmp_path, timeout=5.0)
        else:
            assert _git.run_git(("log",), cwd=tmp_path, timeout=5.0) is None
