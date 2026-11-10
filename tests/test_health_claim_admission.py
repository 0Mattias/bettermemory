"""Health and per-memory reads admit the same parseable stored claims."""

from __future__ import annotations

from bettermemory import location, origin
from bettermemory.claims import load_claims
from bettermemory.membership import Membership

from .test_home_commit_leg import (
    _BODIES,
    World,
    _estate,
    _git,
    _since,
    _surfaces,
    neutral_git,
    this_machine,
    world,
)

__all__ = ["neutral_git", "this_machine", "world"]

_BAD = ["src\\a.py::load"]


async def test_no_home_record_with_unparseable_claims_is_not_listed_skipped(
    world: World,
) -> None:
    assert load_claims(_BAD) == []
    world.stand_at(world.home)
    written = world.store.write(
        content=_BODIES["claim"], scopes=["tools"], origin=origin.capture(world.home)
    )
    world.store.mark_verified(
        written.id, claims=_BAD, verified_head=_git(world.home, "rev-parse", "HEAD")
    )
    assert (
        Membership(world.store, location.process_machine().id).home(written.id) is None
    )
    _since(world.home, "claim")
    for where in ("home", "B"):
        read = await _surfaces(world, written.id, where)
        assert read.show["commit_drift"] is None
        assert "commit_drift_unchecked" not in read.show
        assert read.show["staleness_verdict"] == "fresh"
        assert _estate(read.health)["skipped"] == []


async def test_homed_record_with_unparseable_claims_is_no_candidate(
    world: World,
) -> None:
    rid = await world.homed("claim")
    assert Membership(world.store, location.process_machine().id).home(rid) is not None
    world.store.mark_verified(
        rid, claims=_BAD, verified_head=_git(world.home, "rev-parse", "HEAD")
    )
    _since(world.home, "claim")
    for where in ("home", "B"):
        read = await _surfaces(world, rid, where)
        assert read.show["commit_drift"] is None
        assert "commit_drift_unchecked" not in read.show
        assert read.show["staleness_verdict"] == "fresh"
        estate = read.health.get("cross_repo_drift")
        candidates = sum(g["candidates"] for g in estate["groups"]) if estate else 0
        assert candidates == 0
        assert read.health.get("commit_drift_debt") is None


async def test_a_valid_claim_beside_an_invalid_one_still_counts(world: World) -> None:
    rid = await world.homed("claim")
    world.store.mark_verified(
        rid,
        claims=[*_BAD, "src/a.py::load"],
        verified_head=_git(world.home, "rev-parse", "HEAD"),
    )
    _since(world.home, "claim")
    read = await _surfaces(world, rid, "B")
    assert read.show["commit_drift"]["commits_since_verify"] == 1
    estate = read.health["cross_repo_drift"]
    assert sum(group["candidates"] for group in estate["groups"]) == 1
    assert _estate(read.health)["skipped"] == []
