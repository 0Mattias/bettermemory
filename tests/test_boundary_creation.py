"""Creating a workspace boundary preserves its known repositories' homes."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bettermemory.membership import (
    ASK,
    SAME_PROJECT,
    SURFACES,
    Declaration,
    Manifest,
    admitted,
    relation,
)

from .test_membership_cli import Scene, bm, file_ref, scene
from .test_membership_model import World, _client, _file, _folder, world

__all__ = ["scene", "world"]


@pytest.mark.parametrize("entry", ["flag", "first enclosing file", "first folder file"])
@pytest.mark.parametrize("enclosed", [False, True])
def test_a_workspace_boundary_takes_known_repositories_but_preserves_inner_tenants(
    world: World, entry: str, enclosed: bool
) -> None:
    outer = _client(world, "outer", boundary=True)
    ws = _client(world, "outer/ws", boundary=False)
    inner = _client(world, "outer/ws/secret", boundary=True)
    parents = (outer,) if enclosed else ()
    before = (ws, *parents) if entry == "flag" else parents
    repo = world.repo("/clients/outer/ws/app")
    repo.files = before
    first = world.see(repo)
    rid = world.write(first)
    assert first.project is not None
    old_tenant = first.tenant
    part = _file(repo.path + "/part", name="part")
    part_first = world.see(repo, cwd=part.dir, files=(part, *before))
    part_id = world.write(part_first)

    private = world.repo("/clients/outer/ws/secret/app")
    private.files = (inner, *before)
    private_first = world.see(private)
    private_id = world.write(private_first)
    outside = world.repo("/clients/outer/other")
    outside.files = parents
    outside_first = world.see(outside)
    outside_id = world.write(outside_first)

    boundary = replace(ws, boundary=True)
    if entry == "first folder file":
        loc = _folder(world, ws.dir, boundary, Manifest("ws", ()))
        world.model.resolve(replace(loc, files=(boundary, *parents)))
    repo.files = (boundary, *parents)
    again = world.see(repo)
    assert again.project == first.project
    assert again.tenant is not None and again.tenant != old_tenant
    assert world.model.project(first.project).tenant == again.tenant
    home = world.model.home(rid)
    assert home is not None and relation(home, again) == SAME_PROJECT
    assert all(admitted(surface, home, again) for surface in SURFACES)
    assert world.model.project(str(part_first.project)).tenant == again.tenant
    part_again = world.see(repo, cwd=part.dir, files=(part, boundary, *parents))
    part_home = world.model.home(part_id)
    assert part_home is not None and relation(part_home, part_again) == SAME_PROJECT
    assert all(admitted(surface, part_home, part_again) for surface in SURFACES)

    private.files = (inner, boundary, *parents)
    private_again = world.see(private)
    assert private_again.tenant == private_first.tenant
    private_home = world.model.home(private_id)
    assert private_home is not None
    assert relation(private_home, private_again) == SAME_PROJECT
    assert not any(admitted(surface, private_home, again) for surface in SURFACES)
    assert world.model.project(str(outside_first.project)).tenant == old_tenant
    outside_home = world.model.home(outside_id)
    assert outside_home is not None
    assert not any(admitted(surface, outside_home, again) for surface in SURFACES)


def test_init_new_answer_preserves_the_boundary_when_clearing_provisional_state(
    world: World,
) -> None:
    source = world.repo("/upstream/app", remote="example.invalid/app")
    world.see(source)
    app = world.clone(source, "/workspace/app")
    app.remotes.clear()
    asked = world.see(app)
    assert asked.outcome == ASK and asked.project is not None
    assert world.model.project(asked.project).provisional is not None
    boundary = _file(app.path, boundary=True)
    done = world.model.declare(
        Declaration(
            "init_new",
            "os:tester",
            checkout=asked.checkout,
            file_id=boundary.id,
            directory=app.path,
            boundary=True,
        )
    )
    assert done.project is not None
    project = world.model.project(done.project)
    assert project.provisional is None
    assert project.boundary and project.tenant == done.project


def test_a_known_checkout_outside_the_workspace_keeps_its_project_tenant(
    world: World,
) -> None:
    workspace = _client(world, "ws", boundary=False)
    app = world.repo("/clients/ws/app", remote="example.invalid/app")
    app.files = (workspace,)
    first = world.see(app)
    outside = world.clone(app, "/outside/app")
    old = world.see(outside)
    assert old.project == first.project
    rid = world.write(old)
    app.files = (replace(workspace, boundary=True),)
    inside = world.see(app)
    again = world.see(outside)
    assert again.project == first.project
    assert again.tenant == inside.tenant
    home = world.model.home(rid)
    assert home is not None and relation(home, again) == SAME_PROJECT
    assert all(admitted(surface, home, again) for surface in SURFACES)
    # An unknown clone in another declared tenant still cannot join it.
    other = world.clone(app, "/clients/other/app")
    other.files = (_client(world, "other", boundary=True),)
    separate = world.see(other)
    assert separate.project != first.project
    assert not any(admitted(surface, home, separate) for surface in SURFACES)


@pytest.mark.parametrize("entry", ["first", "rebind", "new"])
def test_project_init_boundary_takes_a_repository_known_before_the_workspace_file(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry: str,
) -> None:
    workspace = scene.root / "ws"
    workspace.mkdir()
    app = scene.repo("ws/app")
    first = scene.see(app)
    rid = scene.write(first)
    assert first.tenant is None
    scene.see(workspace)
    if entry != "first":
        bm(monkeypatch, "project", "init")
        capsys.readouterr()
    flags = [] if entry == "first" else [f"--{entry}"]
    bm(monkeypatch, "project", "init", "--boundary", *flags)
    assert "a boundary" in capsys.readouterr().out
    ref = file_ref(workspace)
    assert ref is not None and ref.boundary
    again = scene.model.resolve(app.location(scene.machine, files=(ref,)))
    assert again.project == first.project and again.tenant is not None
    home = scene.model.home(rid)
    assert home is not None and relation(home, again) == SAME_PROJECT
    assert all(admitted(surface, home, again) for surface in SURFACES)
