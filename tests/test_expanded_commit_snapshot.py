"""An expanded hit's basis agrees with its later commit report."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from bettermemory._response import ResponseBuilder

from ._mcp import call_tool
from .test_home_commit_leg import World, _git, _since, neutral_git, this_machine, world

__all__ = ["neutral_git", "this_machine", "world"]


@pytest.mark.parametrize("stamp", ["unchanged", "current_head", "cleared"])
@pytest.mark.parametrize("leg", ["commit", "claim"])
async def test_expansion_keeps_its_count_and_basis_together(
    world: World, monkeypatch: pytest.MonkeyPatch, stamp: str, leg: str
) -> None:
    rid = await world.homed(leg)
    if leg == "claim":
        _since(world.home, leg)
    record = world.store.load_one(rid)
    world.store.update(
        record.model_copy(
            update={
                "verified_head": None,
                "last_verified_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
            }
        )
    )
    real = ResponseBuilder.attach_commit_drift_counts
    earlier: list[dict[str, Any]] = []
    current_head = _git(world.home, "rev-parse", "HEAD")

    def count(self: ResponseBuilder, out: Any, *args: Any, **kwargs: Any) -> None:
        real(self, out, *args, **kwargs)
        earlier.extend(dict(row) for row in out if row["id"] == rid)
        if stamp != "unchanged":
            latest = world.store.load_one(rid)
            update: dict[str, Any] = {"verified_head": current_head}
            if stamp == "cleared":
                update = {
                    "last_verified_at": None,
                    "verified_head": None,
                    "verified_paths": [],
                    "claims": [],
                }
            world.store.update(latest.model_copy(update=update))

    monkeypatch.setattr(ResponseBuilder, "attach_commit_drift_counts", count)
    result = await call_tool(
        world.server,
        "memory_search",
        {"query": "zebrafish loader", "auto_scope": False, "expand_top": True},
    )
    rows = result.get("result", result) if isinstance(result, dict) else result
    top = next(row for row in rows if row["id"] == rid)
    assert earlier and earlier[0]["commit_drift_basis"] == "author-date"
    assert earlier[0]["commit_drift_count"] > 0
    assert "body" in top and top["relevance"] == "high"
    if stamp == "cleared":
        assert top.get("commit_drift") is None
        assert top["staleness_verdict"] == "spot_check_required"
        assert "commit_drift_count" not in top
        assert "commit_drift_basis" not in top
        assert "claim_drift" not in top
        return
    expected_basis = "reachability" if stamp == "current_head" else "author-date"
    assert top["commit_drift"]["basis"] == expected_basis
    assert top["commit_drift_count"] == top["commit_drift"]["commits_since_verify"]
    if leg == "claim":
        assert earlier[0]["claim_drift"]["checked"] > 0
        assert top.get("claim_drift") == top["commit_drift"].get("claim_drift")
    assert top["commit_drift_basis"] == top["commit_drift"]["basis"]
