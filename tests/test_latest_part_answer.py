"""A later answered part question supersedes the earlier directory binding."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bettermemory.membership import ASK, SILENT, Declaration

from .test_membership_model import World, _file, _monorepo, world

__all__ = ["world"]


@pytest.mark.parametrize("cross_thousand", [False, True])
def test_replacing_a_nested_project_file_uses_the_latest_answer(
    world: World, cross_thousand: bool
) -> None:
    vendors = []
    for name in ("other", "third"):
        file = _file(f"/w/{name}", name=name)
        repo = world.repo(f"/w/{name}")
        repo.files = (file,)
        result = world.see(repo)
        vendors.append((file, result.project))
    mono, root, _a, _b = _monorepo(world)
    world.see(mono)
    directory = "/m/vendor/pkg"
    for position, (file, project) in enumerate(vendors):
        nested = replace(file, dir=directory, path=f"{directory}/bettermemory.toml")
        asked = world.see(mono, cwd=directory, files=(nested, root))
        assert asked.outcome == ASK
        world.model.declare(
            Declaration(
                "join", "os:tester", checkout=asked.checkout, proposal=asked.proposal
            )
        )
        for _ in range(2):
            resolved = world.see(mono, cwd=directory, files=(nested, root))
            assert resolved.outcome == SILENT
            assert resolved.project == project
            assert world.model.pending() == []
        if position == 0 and cross_thousand:
            row = world.store.membership_rows(
                "proposals", where={"id": asked.proposal}
            )[0]
            row["id"] = "Q999"
            world.store.put_membership("proposal", row)
