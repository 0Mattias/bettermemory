"""Absolute commit anchors keep their meaning through recorded checkout views."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from bettermemory import origin

from ._mcp import call_tool
from .test_home_commit_leg import (
    World,
    _agree,
    _estate,
    _git,
    _since,
    _surfaces,
    neutral_git,
    this_machine,
    world,
)
from .test_membership_anchoring import _remove

__all__ = ["neutral_git", "this_machine", "world"]

_PLACEMENTS = [
    (scenario, where)
    for scenario in ("rename", "reclone", "cross-volume")
    for where in ("home", "B", "plain")
] + [("linked", "B")]


async def _verified_absolute(world: World, basis: str) -> str:
    old = world.home
    absolute = str(old / "src/a.py")
    world.stand_at(old)
    written = world.store.write(
        content=f"The zebrafish loader is in `{absolute}`.",
        scopes=["tools"],
        origin=origin.capture(old),
    )
    await call_tool(
        world.server,
        "memory_verify",
        {"id": written.id, "verified_paths": [absolute]},
    )
    if basis == "author-date":
        record = world.store.load_one(written.id)
        world.store.update(record.model_copy(update={"verified_head": None}))
    world.monkeypatch.setenv("GIT_AUTHOR_DATE", "@4000000000 +0000")
    _since(old, "commit")
    # Prove the same record/history counts before changing a checkout view.
    before = await _surfaces(world, written.id, "home")
    _agree(written.id, before)
    assert before.show["commit_drift"]["commits_since_verify"] == 1
    assert before.show["commit_drift"]["basis"] == basis
    return written.id


@pytest.mark.parametrize("basis", ["reachability", "author-date"])
@pytest.mark.parametrize("scenario,where", _PLACEMENTS)
async def test_absolute_commit_anchors_survive_checkout_view_changes(
    world: World,
    scenario: str,
    where: str,
    basis: str,
    local_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if scenario == "cross-volume" and (
        sys.platform != "linux" or not Path("/dev/shm").is_dir()
    ):
        pytest.skip("the actual second-filesystem case runs on Linux /dev/shm")
    old = world.home
    rid = await _verified_absolute(world, basis)
    if scenario == "linked":
        sibling = old.parent / "sibling"
        _git(old, "worktree", "add", "-qb", "sibling", str(sibling))
        # B is the helper's caller-directory slot; real origin capture
        # identifies this as A's sibling, not as repository B.
        world.other = sibling
    else:
        if scenario == "cross-volume":
            parent = local_tmp_path
            if parent.stat().st_dev == old.stat().st_dev:
                pytest.skip("/dev/shm is not a distinct filesystem here")
            moved = parent / "alpha"
            shutil.move(str(old), str(moved))
            assert moved.stat().st_dev != old.parent.stat().st_dev
        else:
            moved = old.parent / (scenario + "-alpha")
            if scenario == "reclone":
                remote = _git(old, "remote", "get-url", "origin")
                _git(
                    old.parent,
                    "clone",
                    "-q",
                    "--no-hardlinks",
                    str(old),
                    str(moved),
                )
                _git(moved, "remote", "set-url", "origin", remote)
                _remove(old)
            else:
                old.rename(moved)
        world.home = moved
        # A session at the new location observes the view before an
        # unrelated repo/plain-folder caller reads the same record.
        await _surfaces(world, rid, "home")
    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["commit_drift"] is not None
    assert read.show["commit_drift"]["commits_since_verify"] == 1
    assert read.show["commit_drift"]["basis"] == basis
    assert read.show["staleness_verdict"] == "spot_check_recommended"
    assert rid in _estate(_cli_existing_views(world, where, capsys))["drifted"]


def _cli_existing_views(
    world: World, where: str, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    from bettermemory.cli import health_cmd
    from bettermemory.cli._common import CliContext

    world.monkeypatch.setattr(
        health_cmd,
        "cli_context",
        lambda: CliContext(world.config, world.store.path.parent, world.store),
    )
    world.monkeypatch.setattr(
        health_cmd, "capture_origin", lambda: origin.capture(world.place(where))
    )
    capsys.readouterr()
    before = world.store.conn.total_changes
    health_cmd._cli_health(json_out=True, days=30, top_k=10)
    report = json.loads(capsys.readouterr().out)
    assert world.store.conn.total_changes == before
    return report


@pytest.mark.parametrize("basis", ["reachability", "author-date"])
@pytest.mark.parametrize("where", ["home", "B", "plain"])
async def test_an_expected_home_anchor_without_a_recorded_view_is_not_checked(
    world: World, where: str, basis: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rid = await _verified_absolute(world, basis)
    record = world.store.load_one(rid)
    assert record.origin is not None
    former = world.home.parent / "former-machine" / "alpha"
    absolute = str(former / "src/a.py")
    # A historical/peer record can retain its verified origin and path
    # while this machine has no view of that original checkout spelling.
    world.store.update(
        record.model_copy(
            update={
                "body": f"The zebrafish loader is in `{absolute}`.\n",
                "verified_paths": [absolute],
                "verified_path_targets": {absolute: absolute},
                "origin": record.origin.model_copy(
                    update={"cwd": str(former), "worktree_root": str(former)}
                ),
            }
        )
    )
    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["commit_drift"] is None
    assert read.show["commit_drift_unchecked"] is True
    assert read.show["staleness_verdict"] == "spot_check_recommended"
    assert not (read.show.get("path_drift") or {}).get("missing")
    cli = _estate(_cli_existing_views(world, where, capsys))
    assert rid not in cli["drifted"]
    assert cli["skipped"] == ["a commit anchor has no recorded checkout view"]


def test_a_project_standin_requires_repository_evidence() -> None:
    from bettermemory.verify_anchoring import ClaimAnchor

    unproven = ClaimAnchor(
        "/other/repository", "project_checkout", holds=lambda *_: False
    )
    assert unproven.commit_root(None)[0] is None
    assert unproven.commit_root("a" * 40)[0] is None
    legacy = ClaimAnchor(
        "/cloned/repository",
        "project_checkout",
        holds=lambda *_: False,
        commit_standin=True,
    )
    assert legacy.commit_root(None) == ("/cloned/repository", "project_checkout")
    assert legacy.commit_root("a" * 40)[0] is None


@pytest.mark.parametrize(
    "path,recorded,relative",
    [
        ("/old/repo/src/a.py", "/old/repo", "src/a.py"),
        ("/old/repo/src\\a.py", "/old/repo", "src\\a.py"),
        (r"C:\old\repo\src\a.py", r"C:\old\repo", "src/a.py"),
        (r"\\server\share\repo\src\a.py", r"\\server\share\repo", "src/a.py"),
    ],
)
def test_recorded_commit_paths_keep_the_recording_machines_path_flavour(
    path: str, recorded: str, relative: str
) -> None:
    from bettermemory.commit_anchors import project_commit_anchors
    from bettermemory.verify_anchoring import ClaimAnchor

    anchor = ClaimAnchor("/new/repo", "home_checkout", recorded_roots=(recorded,))
    projected = project_commit_anchors((path,), anchor)
    assert projected.paths == (relative,)
    assert not projected.unmapped


@pytest.mark.parametrize(
    "path,expected_unmapped",
    [
        ("/old/repo/src/a.py", True),
        ("/old/repo/sub/../src/a.py", True),
        ("/old/repository/src/a.py", False),
    ],
)
def test_origin_spelling_without_a_recorded_view_is_not_a_translation(
    path: str, expected_unmapped: bool
) -> None:
    from bettermemory.commit_anchors import project_commit_anchors
    from bettermemory.verify_anchoring import ClaimAnchor

    anchor = ClaimAnchor("/new/repo", "home_checkout", origin_root="/old/repo")
    assert project_commit_anchors((path,), anchor).unmapped is expected_unmapped
