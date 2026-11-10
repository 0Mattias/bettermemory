"""Repeated estate reads eventually check every real home root (ENG-238)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory import health_roots
from bettermemory.health import compute_health, report_for_store
from bettermemory.health_model import _CROSS_REPO_MAX_ROOTS
from bettermemory.health_roots import EstateProgress, progress_for_store
from bettermemory.models import Memory, generate_ulid
from bettermemory.origin import Origin
from bettermemory.store import Store

from ._mcp import call_tool
from .test_health_cross_repo_cap import _foreign
from .test_home_commit_leg import World, _repo, _since, neutral_git, this_machine, world
from .test_moved_commit_anchors import _cli_existing_views
from .test_relative_attestation_suffixes import _commit, _git, home

__all__ = ["home", "neutral_git", "this_machine", "world"]

_NOW = datetime(2026, 4, 1, tzinfo=timezone.utc)


@pytest.fixture
def memories(home: Path) -> list[Memory]:
    made = []
    for index in range(9):
        root = home / f"foreign-{index}"
        root.mkdir()
        _git(root, "init", "-q", "-b", "main")
        _git(
            root,
            "remote",
            "add",
            "origin",
            f"https://github.com/example/foreign-{index}.git",
        )
        _commit(
            root, "src/app.py", "LIMIT = 1\n", datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        cut = _git(root, "rev-parse", "HEAD")
        _commit(
            root, "src/app.py", "LIMIT = 2\n", datetime(2026, 3, 1, tzinfo=timezone.utc)
        )
        made.append(_foreign(root, index).model_copy(update={"verified_head": cut}))
    return made


def _caller(home: Path) -> Origin:
    return Origin(
        cwd=str(home),
        repo="https://github.com/example/caller.git",
        worktree_root=str(home),
    )


def test_repeated_health_reads_cover_nine_drifting_roots(
    home: Path, memories: list[Memory]
) -> None:
    progress = EstateProgress()
    expected = {str(m.origin.worktree_root) for m in memories if m.origin is not None}
    checked: set[str] = set()
    reports = []
    for step in range(math.ceil(len(expected) / _CROSS_REPO_MAX_ROOTS)):
        when = _NOW + timedelta(minutes=step)
        report = compute_health(
            memories,
            [],
            caller_origin=_caller(home),
            now=when,
            estate_progress=progress,
        )
        estate = report.cross_repo_drift
        assert estate is not None
        assert len(estate.groups) == _CROSS_REPO_MAX_ROOTS
        assert estate.total_drifted == len(estate.groups)
        assert all(group.rows[0].commits_since_verify == 1 for group in estate.groups)
        assert all(group.last_checked_at == when for group in estate.groups)
        checked.update(group.worktree_root for group in estate.groups)
        reports.append(estate.to_dict())
    assert checked == expected, "a root is starved by the same cap on every health read"
    assert reports[0]["skipped"][0]["last_checked_at"] is None
    assert reports[1]["skipped"][0]["last_checked_at"] == "2026-04-01T00:00:00Z"


def test_store_reports_share_rotation_without_writing_the_store(
    home: Path, memories: list[Memory], memory_dir: Path
) -> None:
    with Store(memory_dir) as store:
        for memory in memories:
            row = store.write(
                content=memory.body, scopes=["tools"], origin=memory.origin
            )
            store.mark_verified(row.id, verified_head=memory.verified_head)
        before = store.conn.total_changes
        checked: set[str] = set()
        for _ in range(2):
            report = report_for_store(
                store, caller_origin=_caller(home), typo_scope_exceptions=[]
            )
            assert report.cross_repo_drift is not None
            checked.update(
                group.worktree_root for group in report.cross_repo_drift.groups
            )
        assert len(checked) == 9
        assert store.conn.total_changes == before


@pytest.mark.parametrize("count", [9, 17, 41])
def test_timestamp_eviction_does_not_reset_the_rotation(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_roots, "_LAST_CHECKED_CAP", 2)
    progress = EstateProgress()
    roots = [f"root-{i:03}" for i in range(count)]
    seen: set[str] = set()
    for _ in range(math.ceil(count / 8)):
        with progress.lock:
            for root in progress.ordered(roots)[:8]:
                progress.attempt(root, _NOW)
                seen.add(root)
    assert seen == set(roots)
    assert len(progress.checked) == 2


def test_progress_does_not_retain_its_store_handle() -> None:
    import gc
    import weakref

    # The suite retains real Stores until teardown so it can close them.
    # A weak-key handle proves the progress registry owns no handle.
    class StoreHandle:
        pass

    store = StoreHandle()
    state = progress_for_store(store)
    assert progress_for_store(store) is state
    reference = weakref.ref(store)
    del store
    gc.collect()
    assert reference() is None


async def test_daemon_rotation_and_offline_cli_cover_nine_homed_roots(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    original_home = world.home
    ids = []
    try:
        for index in range(9):
            world.home = (
                original_home
                if index == 0
                else _repo(
                    original_home.parent / f"root-{index}",
                    f"git@github.com:example/health-{index}.git",
                )
            )
            ids.append(await world.homed("commit"))
            _since(world.home, "commit")
    finally:
        world.home = original_home
    world.stand_at(world.plain)
    checked: set[str] = set()
    for _ in range(2):
        report = await call_tool(world.server, "memory_admin", {"action": "health"})
        estate = report["cross_repo_drift"]
        assert len(estate["groups"]) == 8 and len(estate["skipped"]) == 1
        assert estate["total_drifted"] == 8
        for group in estate["groups"]:
            assert group["last_checked_at"] is not None
            for row in group["rows"]:
                checked.add(row["id"])
                assert row["commits_since_verify"] == 1
    assert checked == set(ids)
    for rid in ids:
        shown = await call_tool(world.server, "memory_show", {"id": rid})
        assert shown["commit_drift"]["commits_since_verify"] == 1
    # Each new CLI invocation scans all homes; no process-local cursor or
    # store write is needed to reach the root skipped by the daemon's cap.
    for _ in range(2):
        estate = _cli_existing_views(world, "plain", capsys)["cross_repo_drift"]
        assert len(estate["groups"]) == 9 and estate["skipped"] == []
        assert estate["total_drifted"] == 9


def test_failed_identification_still_rotates(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bettermemory import health

    memories = []
    roots = set()
    for index in range(9):
        root = home / f"failed-{index}"
        root.mkdir()
        roots.add(str(root))
        memories.append(_foreign(root, index))
    captured: list[str] = []

    def capture(root: Path) -> Origin:
        captured.append(str(root))
        return Origin(cwd=str(root), repo=None, worktree_root=None)

    monkeypatch.setattr(health, "capture", capture)
    progress = EstateProgress()
    for _ in range(2):
        report = compute_health(memories, [], now=_NOW, estate_progress=progress)
        estate = report.cross_repo_drift
        assert estate is not None and estate.groups == []
        attempted = [
            s for s in estate.skipped if s["reason"] != "past the per-run root cap"
        ]
        assert len(attempted) == 8
        assert all(s["last_checked_at"] == "2026-04-01T00:00:00Z" for s in attempted)
    assert len(captured) == 16 and set(captured) == roots


def test_two_repository_labels_for_one_root_share_a_place(
    memories: list[Memory], home: Path
) -> None:
    first = memories[0]
    assert first.origin is not None and first.origin.repo is not None
    alias = first.model_copy(
        update={
            "id": generate_ulid(),
            "origin": first.origin.model_copy(
                update={"repo": first.origin.repo.removesuffix(".git")}
            ),
        }
    )
    report = compute_health(memories[:8] + [alias], [], caller_origin=_caller(home))
    estate = report.cross_repo_drift
    assert estate is not None and estate.skipped == []
    assert len({g.worktree_root for g in estate.groups}) == 8
    assert estate.total_drifted == 9
