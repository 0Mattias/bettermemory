"""Claim 38: `project join <unrelated project>` at a checkout whose only
open question is a part's question (L7) must not answer that question
with a project it never offered, and its closing line must be true."""

from __future__ import annotations

import pytest

from bettermemory import project_file
from bettermemory.membership import ASK, Declaration

from .test_membership_cli import Scene, bm, scene, typed

__all__ = ["scene"]


@pytest.mark.parametrize("via_cli", [True, False])
def test_join_unrelated_project_does_not_answer_the_parts_question(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    via_cli: bool,
) -> None:
    mono = scene.repo("mono")
    project_file.write(mono.path, name="mono")
    root = scene.see(mono)
    other = scene.repo("other", remote="example.invalid/acme/other")
    project_file.write(other.path, name="other")
    theirs = scene.see(other)
    third = scene.see(scene.repo("third", remote="example.invalid/acme/third"))
    assert len({root.project, theirs.project, third.project}) == 3
    tools = mono.path / "packages" / "tools"
    tools.mkdir(parents=True)
    (tools / project_file.PROJECT_FILE_NAME).write_bytes(
        (other.path / project_file.PROJECT_FILE_NAME).read_bytes()
    )
    mono.at = tools
    asked = scene.see(mono)
    assert (asked.outcome, asked.project) == (ASK, root.project)
    qid = asked.proposal
    assert qid is not None
    q = scene.model.proposal(qid)
    assert q.kind == "subproject_contradiction"
    assert [scene.model.canonical(c) for c in q.candidates] == [theirs.project]

    # At mono's root: join the unrelated third project.
    mono.at = None
    scene.see(mono)
    capsys.readouterr()
    typed(monkeypatch, "third")
    code: int | str | None = 0
    try:
        if via_cli:
            bm(monkeypatch, "project", "join", str(third.project))
        else:
            assert root.checkout is not None and third.project is not None
            scene.model.declare(
                Declaration(
                    "join", "os:fixture", checkout=root.checkout, target=third.project
                )
            )
        code = 0
    except SystemExit as exc:  # a refusal is an honest outcome too
        code = exc.code
    except ValueError:
        code = 2
    out = capsys.readouterr().out
    model = scene.model
    k = model.checkout(str(root.checkout))
    now = model.canonical(k.project)
    said = f'checkout {k.id} is project "third" ({third.project})' in out
    after = model.proposal(qid)
    assert code in (0, 2)
    # The closing line is true of the store.
    assert not said or now == third.project, (
        f"printed that checkout {k.id} is {third.project} but it is {now}"
    )
    # The part's question is not answered with a project it never offered.
    assert after.answer != f"join:{third.project}", (
        f"question {qid} (offers {theirs.project}) closed with {after.answer}"
    )
    assert after.state == "open"
    # And the same question is not asked again at the next sighting.
    mono.at = tools
    again = scene.see(mono)
    assert again.proposal in (None, qid), (
        f"question reopened as {again.proposal} after {qid} was 'answered'"
    )


def test_joining_the_offered_part_reports_the_folder_and_checkout_separately(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mono = scene.repo("mono")
    project_file.write(mono.path, name="mono")
    root = scene.see(mono)
    other = scene.repo("other", remote="example.invalid/other")
    project_file.write(other.path, name="other")
    theirs = scene.see(other)
    part = mono.path / "part"
    part.mkdir()
    (part / project_file.PROJECT_FILE_NAME).write_bytes(
        (other.path / project_file.PROJECT_FILE_NAME).read_bytes()
    )
    mono.at = part
    asked = scene.see(mono)
    assert asked.outcome == ASK and asked.proposal is not None
    capsys.readouterr()
    typed(monkeypatch, "other")
    bm(monkeypatch, "project", "join", str(theirs.project))
    output = capsys.readouterr().out
    model = scene.model
    assert model.canonical(model.checkout(str(root.checkout)).project) == root.project
    assert model.proposal(asked.proposal).answer == f"join:{theirs.project}"
    assert "folder " in output and f"checkout {root.checkout} stays project" in output
