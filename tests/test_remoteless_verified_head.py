"""A verification cut is a commit even when its repository has no remote."""

from __future__ import annotations

import pytest

from bettermemory import location, verify
from bettermemory.attestation_paths import commit_paths
from bettermemory.membership import Membership
from bettermemory.verify_anchoring import Anchoring

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
from .test_moved_commit_anchors import _cli_existing_views

__all__ = ["neutral_git", "this_machine", "world"]


def _direct_drift(
    world: World, rid: str
) -> verify.CommitDriftStatus | verify.CommitDriftUnread | None:
    record = world.store.load_one(rid)
    assert record.origin is not None
    anchor = Anchoring(
        Membership(world.store, location.process_machine().id), None
    ).for_record(rid, origin_root=record.origin.worktree_root)
    return verify.compute_commit_drift(
        record.last_verified_at,
        record.origin.repo,
        caller_origin=None,
        verified_paths=commit_paths(record),
        body=record.body,
        claims=record.claims,
        verified_head=record.verified_head,
        anchor=anchor,
    )


async def test_a_remoteless_home_stamps_its_verified_head(world: World) -> None:
    _git(world.home, "remote", "remove", "origin")
    head = _git(world.home, "rev-parse", "HEAD")
    rid = await world.homed("commit")
    record = world.store.load_one(rid)
    assert record.origin is not None and record.origin.repo is None
    assert record.verified_head == head


async def _merge_older_feature(world: World, mode: str, legacy: bool) -> str:
    _git(world.home, "remote", "remove", "origin")
    _git(world.home, "checkout", "-qb", "feature")
    _since(world.home, "commit")
    feature = _git(world.home, "rev-parse", "HEAD")
    _git(world.home, "checkout", "-q", "main")
    rid = await world.homed("commit")
    if legacy:
        record = world.store.load_one(rid)
        world.store.update(record.model_copy(update={"verified_head": None}))
    # Author dates stay before the stamp for every integration mode.
    # Rewritten commits get a distinct object id after verification.
    world.monkeypatch.setenv("GIT_COMMITTER_DATE", "@4000000000 +0000")
    if mode == "cherry-pick":
        _git(world.home, "cherry-pick", feature)
        assert _git(world.home, "rev-parse", "HEAD") != feature
    else:
        if mode == "rebase":
            (world.home / "README.md").write_text("unrelated main update\n")
            _git(world.home, "add", "README.md")
            _git(world.home, "commit", "-qm", "unrelated main update")
            _git(world.home, "checkout", "-q", "feature")
            _git(world.home, "rebase", "main")
            assert _git(world.home, "rev-parse", "HEAD") != feature
            _git(world.home, "checkout", "-q", "main")
        _git(world.home, "merge", "--ff-only", "feature")
    return rid


@pytest.mark.parametrize("mode", ["fast-forward", "rebase", "cherry-pick"])
@pytest.mark.parametrize("where", ["home", "B", "plain"])
async def test_old_author_dates_do_not_hide_newly_reachable_commits_without_a_remote(
    world: World, mode: str, where: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rid = await _merge_older_feature(world, mode, legacy=False)
    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["commit_drift"] is not None
    assert read.show["commit_drift"]["commits_since_verify"] == 1
    assert read.show["commit_drift"]["basis"] == "reachability"
    assert read.show["staleness_verdict"] == "spot_check_recommended"
    assert rid in _estate(_cli_existing_views(world, where, capsys))["drifted"]
    direct = _direct_drift(world, rid)
    assert isinstance(direct, verify.CommitDriftStatus)
    assert direct.commits_since_verify == 1 and direct.basis == "reachability"


@pytest.mark.parametrize("mode", ["fast-forward", "rebase", "cherry-pick"])
@pytest.mark.parametrize("where", ["home", "B", "plain"])
async def test_a_legacy_stamp_cannot_prove_freshness_from_old_author_dates(
    world: World, mode: str, where: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rid = await _merge_older_feature(world, mode, legacy=True)
    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["staleness_verdict"] == "spot_check_required"
    assert read.show["commit_drift_unread"] is True
    assert "Author dates cannot rule out" in read.show["verification"]["recommendation"]
    assert rid in _estate(_cli_existing_views(world, where, capsys))["unread"]
    assert _direct_drift(world, rid) is verify.CommitDriftUnread.UNCONFIRMED_CUT


@pytest.mark.parametrize("where", ["home", "B", "plain"])
async def test_positive_legacy_author_dates_remain_drift_evidence(
    world: World, where: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(world.home, "remote", "remove", "origin")
    rid = await world.homed("commit")
    record = world.store.load_one(rid)
    world.store.update(record.model_copy(update={"verified_head": None}))
    world.monkeypatch.setenv("GIT_AUTHOR_DATE", "@4000000000 +0000")
    _since(world.home, "commit")
    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["commit_drift"]["commits_since_verify"] == 1
    assert read.show["commit_drift"]["basis"] == "author-date"
    assert not read.show.get("commit_drift_unread")
    assert rid in _estate(_cli_existing_views(world, where, capsys))["drifted"]


@pytest.mark.parametrize("where", ["home", "B", "plain"])
async def test_a_quiescent_remoteless_home_has_a_complete_clean_cut(
    world: World, where: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(world.home, "remote", "remove", "origin")
    rid = await world.homed("commit")
    read = await _surfaces(world, rid, where)
    _agree(rid, read)
    assert read.show["commit_drift"]["commits_since_verify"] == 0
    assert read.show["commit_drift"]["basis"] == "reachability"
    assert read.show["staleness_verdict"] == "fresh"
    assert rid not in _estate(_cli_existing_views(world, where, capsys))["unread"]
