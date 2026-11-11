"""Migrated project homes can lack a checkout without inventing one."""

from __future__ import annotations

import sys

import pytest

from bettermemory.cli import main as cli_main
from bettermemory.membership import GLOBAL
from bettermemory.membership_migrate import migrate

from .test_membership_migration import (
    World,
    _now,
    neutral_git as neutral_git,
    world as world,
)


def test_why_names_the_absent_checkout_of_a_migrated_project_home(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(world.store, world.identity, now=_now)
    why = world.model().why(world.labels["rem"])
    assert why.project not in (None, GLOBAL)
    assert why.checkout is None
    assert why.rule == "migration:remote_no_root"
    assert "written in checkout None" not in why.text
    assert "no checkout recorded" in why.text
    monkeypatch.setenv("BETTERMEMORY_DIR", str(world.tmp / "store"))
    monkeypatch.setattr(
        sys, "argv", ["bettermemory", "project", "why", world.labels["rem"]]
    )
    capsys.readouterr()
    cli_main()
    printed = capsys.readouterr().out
    assert "no checkout recorded" in printed.splitlines()[0]
    assert "written in checkout None" not in printed


def test_global_migration_keeps_its_existing_explanation(world: World) -> None:
    migrate(world.store, world.identity, now=_now)
    why = world.model().why(world.labels["void"])
    assert why.project == GLOBAL and why.checkout is None
    assert "global (written outside any repository and any project file)" in why.text
    assert "checkout None" not in why.text
