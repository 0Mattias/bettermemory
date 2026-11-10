"""Claim 20: `bettermemory project new` answering an unresolved-location
legacy question renames the records' project to the caller checkout's
folder name and reports the checkout as that project."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from bettermemory import membership as mb
from bettermemory.membership import ASK, Location

from .test_membership_cli import Scene, bm, scene, typed

__all__ = ["scene"]


def test_project_new_answering_unresolved_question_keeps_names_and_checkout(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = scene.repo("app")
    first = scene.see(app)
    assert first.project is not None and first.checkout is not None
    assert scene.model.project(first.project).name == "app"
    # Records written at app/x while it could not be read, by session s1.
    sub = app.path / "x"
    sub.mkdir()
    blind = Location(
        machine=scene.machine, cwd=str(sub), state=mb.UNREADABLE, session="s1"
    )
    scene.model.resolve(blind)
    home = scene.model.unresolved_home(blind, session="s1")
    rid = scene.put(home)
    unresolved = home.project
    assert unresolved != first.project
    assert scene.model.project(unresolved).name == "x"
    # Another sighting there (no session): the legacy question opens in app.
    asked = scene.see(replace(app, at=sub))
    assert asked.outcome == ASK and asked.checkout == first.checkout
    [pr] = scene.model.pending()
    assert (pr.kind, pr.reason) == (mb.LEGACY, mb.UNRESOLVED_LOCATION)
    assert pr.evidence["unresolved_project"] == unresolved
    capsys.readouterr()

    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "new")
    out = capsys.readouterr().out

    model = scene.model
    assert model.proposal(pr.id).answer == "new"
    k = model.checkout(first.checkout)
    # The checkout did not move: it is still the project it was.
    assert (model.canonical(k.project) or k.project) == first.project
    rec = model.home(rid)
    assert rec is not None and rec.project == unresolved
    # 'separate' renames nothing: the records' project keeps its name.
    assert model.project(unresolved).name == "x", (
        f"records' project {unresolved} renamed to {model.project(unresolved).name!r}"
    )
    # The final line names the checkout's real project.
    last = out.rstrip().splitlines()[-1]
    said = re.search(rf"checkout {k.id} is project .*?\((P\d+)\)", last)
    if said is not None:
        assert said.group(1) == first.project, last
