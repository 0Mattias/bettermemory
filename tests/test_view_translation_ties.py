"""Claim 40: translate() with two checkouts recording the same view path."""

from __future__ import annotations

from .test_membership_model import World, world

__all__ = ["world"]


def test_same_project_other_machine_registered_first(world: World) -> None:
    # M2's checkout of the project at /w/app is registered first.
    other = world.model_for("M2")
    theirs = world.repo("/w/app", remote="example.invalid/acme/app", machine="M2")
    r2 = world.see(theirs, model=other)
    # This machine's (M1) own live clone of the same project, also at /w/app.
    mine = world.clone(theirs, "/w/app", machine="M1")
    r1 = world.see(mine)
    assert r1.project == r2.project and r1.checkout != r2.checkout
    assert r1.checkout is not None
    assert world.model.live_view(world.model.checkout(r1.checkout)) == "/w/app"
    assert world.model.translate("/w/app/src/x.py") == ("/w/app/src/x.py", "checkout")


def test_other_project_other_machine_registered_first(world: World) -> None:
    other = world.model_for("M2")
    theirs = world.repo("/w/app", remote="example.invalid/acme/tool", machine="M2")
    r2 = world.see(theirs, model=other)
    mine = world.repo("/w/app", remote="example.invalid/acme/app")
    r1 = world.see(mine)
    assert r1.project != r2.project
    assert r1.checkout is not None
    assert world.model.live_view(world.model.checkout(r1.checkout)) == "/w/app"
    assert world.model.translate("/w/app/src/x.py", r1) == (
        "/w/app/src/x.py",
        "checkout",
    )


def test_control_local_registered_first(world: World) -> None:
    """The same world in the other registration order: answers 'checkout'."""
    mine = world.repo("/w/app", remote="example.invalid/acme/app")
    r1 = world.see(mine)
    other = world.model_for("M2")
    theirs = world.repo("/w/app", remote="example.invalid/acme/tool", machine="M2")
    world.see(theirs, model=other)
    assert world.model.translate("/w/app/src/x.py", r1) == (
        "/w/app/src/x.py",
        "checkout",
    )


def test_longest_recorded_view_still_precedes_a_shorter_live_one(world: World) -> None:
    foreign = world.model_for("M2")
    deeper = world.repo("/w/app/deep", remote="example.invalid/deep", machine="M2")
    world.see(deeper, model=foreign)
    mine = world.repo("/w/app", remote="example.invalid/app")
    caller = world.see(mine)
    path = "/w/app/deep/src.py"
    assert world.model.translate(path, caller) == (path, "unanchored")


def test_a_stale_local_tie_does_not_replace_a_foreign_projects_live_stand_in(
    world: World,
) -> None:
    foreign = world.model_for("M2")
    original = world.repo("/w/app", remote="example.invalid/tool", machine="M2")
    world.see(original, model=foreign)
    stand_in = world.clone(original, "/else/tool", machine="M1")
    world.see(stand_in)
    stale = world.repo("/w/app", remote="example.invalid/other")
    world.see(stale)
    world.delete(stale)
    assert world.model.translate("/w/app/src.py") == (
        "/else/tool/src.py",
        "project_checkout",
    )
