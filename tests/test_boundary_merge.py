"""A merge carries affiliated children without crossing private tenants."""

import pytest

from bettermemory import project_file
from bettermemory.membership import (
    OTHER_TENANT,
    SAME_PROJECT,
    SURFACES,
    admitted,
    relation,
)

from .test_membership_model import World, _client, _file, world
from .test_membership_cli import Scene, bm, scene

__all__ = ["scene", "world"]


def test_joining_into_a_boundary_keeps_subproject_homes_visible(world: World) -> None:
    mono = world.repo("/mono")
    first = world.see(mono)
    part = _file("/mono/packages/web")
    child = world.see(mono, cwd=part.dir, files=(part,))
    rid = world.write(child)
    client = world.repo("/client")
    client.files = (_file(client.path, boundary=True),)
    destination = world.see(client)
    assert first.project is not None and destination.project is not None
    world.model.merge(first.project, destination.project, evidence="fixture join")
    again = world.see(mono, cwd=part.dir, files=(part,))
    home = world.model.home(rid)
    assert home is not None
    assert relation(home, again) == SAME_PROJECT
    assert all(admitted(surface, home, again) for surface in SURFACES)


@pytest.mark.parametrize("source_boundary", [True, False])
@pytest.mark.parametrize("target_boundary", [True, False])
@pytest.mark.parametrize("outer_source", [True, False])
@pytest.mark.parametrize("outer_target", [True, False])
def test_merge_tenant_transitions_preserve_inner_and_unrelated_projects(
    world: World,
    source_boundary: bool,
    target_boundary: bool,
    outer_source: bool,
    outer_target: bool,
) -> None:
    source_outer = _client(world, "source", boundary=True)
    target_outer = _client(world, "target", boundary=True)
    source_file = _file("/clients/source/mono", boundary=source_boundary)
    target_file = _file("/clients/target/app", boundary=target_boundary)
    source_files = (source_file, *((source_outer,) if outer_source else ()))
    target_files = (target_file, *((target_outer,) if outer_target else ()))
    mono = world.repo(source_file.dir)
    mono.files = source_files
    original = world.see(mono)
    target = world.repo(target_file.dir)
    target.files = target_files
    destination = world.see(target)
    assert original.project is not None and destination.project is not None

    web = _file(mono.path + "/packages/web")
    part = world.see(mono, cwd=web.dir, files=(web, *source_files))
    rid = world.write(part)
    private = _file(mono.path + "/private", boundary=True)
    own = world.see(mono, cwd=private.dir, files=(private, *source_files))
    own_id = world.write(own)
    peer = world.repo(mono.path + "/peer")
    peer.files = source_files
    peer_first = world.see(peer)
    peer_id = world.write(peer_first)
    unrelated = world.repo("/clients/source/unrelated")
    unrelated.files = (source_outer,) if outer_source else ()
    unrelated_first = world.see(unrelated)
    unrelated_id = world.write(unrelated_first)
    raw_home = world.store.membership_rows("homes")

    world.model.merge(original.project, destination.project, evidence="fixture join")
    expected = world.model.project(destination.project).tenant
    assert expected == (
        destination.project
        if source_boundary or target_boundary
        else destination.tenant
    )
    after = world.see(mono, cwd=web.dir, files=(web, *source_files))
    # An outer file still declares the physical caller's tenant. A merge
    # of its non-boundary child cannot lift that boundary (decision D17).
    isolated = outer_source and not source_boundary and not target_boundary
    assert after.tenant == (original.tenant if isolated else expected)
    home = world.model.home(rid)
    assert home is not None
    assert relation(home, after) == (OTHER_TENANT if isolated else SAME_PROJECT)
    assert all(admitted(surface, home, after) == (not isolated) for surface in SURFACES)
    assert world.store.membership_rows("homes") == raw_home

    own_after = world.see(mono, cwd=private.dir, files=(private, *source_files))
    assert own_after.tenant == own.tenant
    own_home = world.model.home(own_id)
    assert own_home is not None and relation(own_home, own_after) == SAME_PROJECT
    assert not any(admitted(surface, own_home, after) for surface in SURFACES)
    peer_home = world.model.home(peer_id)
    assert peer_home is not None
    assert peer_home.tenant == (expected if source_boundary else peer_first.tenant)
    unrelated_home = world.model.home(unrelated_id)
    assert (
        unrelated_home is not None and unrelated_home.tenant == unrelated_first.tenant
    )
    before = len(world.store.log_rows())
    world.model.merge(original.project, destination.project, evidence="repeat join")
    assert len(world.store.log_rows()) == before
    assert world.store.refold()["status"] == "ok"


def test_cli_join_into_an_existing_boundary_keeps_the_parts_visible(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mono = scene.repo("mono")
    first = scene.see(mono)
    web = mono.path / "packages" / "web"
    web.mkdir(parents=True)
    project_file.write(web, name="web")
    mono.at = web
    part = scene.see(mono)
    rid = scene.write(part)
    client = scene.repo("client")
    project_file.write(client.path, name="client", boundary=True)
    destination = scene.see(client)
    mono.at = None
    scene.see(mono)
    capsys.readouterr()
    bm(monkeypatch, "project", "join", str(destination.project))
    assert scene.model.canonical(first.project) == destination.project
    mono.at = web
    after = scene.see(mono)
    home = scene.model.home(rid)
    assert home is not None and relation(home, after) == SAME_PROJECT
    assert after.tenant == destination.project
    assert all(admitted(surface, home, after) for surface in SURFACES)
    assert scene.store.refold()["status"] == "ok"
