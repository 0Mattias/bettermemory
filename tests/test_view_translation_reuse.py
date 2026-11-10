from __future__ import annotations

from .test_membership_model import World, world

__all__ = ["world"]


def test_single_machine_reused_path(world: World) -> None:
    old = world.repo("/w/app", remote="example.invalid/acme/old")
    world.see(old)
    world.delete(old)
    new = world.repo("/w/app", remote="example.invalid/acme/new")
    r = world.see(new)
    assert r.checkout is not None
    assert world.model.live_view(world.model.checkout(r.checkout)) == "/w/app"
    assert world.model.translate("/w/app/src/x.py", r) == (
        "/w/app/src/x.py",
        "checkout",
    )
