"""A real membership estate can have two skip causes at one recorded root."""

from pathlib import Path

import pytest

import bettermemory._handlers as handlers
from bettermemory.machine import MachineIdentity
from bettermemory.membership import Membership
from bettermemory.origin import Origin
from bettermemory.store import Store

from ._mcp import call_tool
from .test_health_anchoring import (
    _git,
    _repo,
    _server_at,
    _write,
    neutral_git,
    this_machine,
)

__all__ = ["neutral_git", "this_machine"]


@pytest.mark.parametrize("legacy_first", [False, True])
async def test_health_reports_both_actual_membership_skip_causes(
    memory_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    this_machine: MachineIdentity,
    legacy_first: bool,
) -> None:
    repo = _repo(tmp_path / "work" / "service")
    with Store(memory_dir) as store:
        server = _server_at(store, memory_dir, repo, monkeypatch)
        if legacy_first:
            legacy = _write(store, repo, "src/b.py")
            homed = _write(store, repo, "src/a.py")
        else:
            homed = _write(store, repo, "src/a.py")
            legacy = _write(store, repo, "src/b.py")
        await call_tool(
            server, "memory_verify", {"id": homed, "verified_paths": ["src/a.py"]}
        )
        store.mark_verified(
            legacy,
            verified_paths=["src/b.py"],
            verified_head=_git(repo, "rev-parse", "HEAD"),
        )
        model = Membership(store, this_machine.id)
        assert model.home(homed) is not None and model.home(legacy) is None
        repo.rename(tmp_path / "unrecorded-move")
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setattr(handlers, "capture_origin", lambda: Origin(cwd=str(plain)))
        report = await call_tool(server, "memory_admin", {"action": "health"})
        estate = report["cross_repo_drift"]
        assert estate is not None and not estate["groups"]
        reasons = {
            "no_home"
            if row["reason"].startswith("no home:")
            else "unconfirmed"
            if row["reason"].startswith("unconfirmed:")
            else row["reason"]
            for row in estate["skipped"]
        }
        assert reasons == {"no_home", "unconfirmed"}
