"""Every uncertain call leaves a row and is visible (RS-F6; design 8.10,
E13, E36 c3, E37, X13, X14): the rows, health's membership section in
`bettermemory health` and in the `memory_health` tool, `project pending`,
`project why`, the line of an open question in every session's first
output until it is answered, and `log verify` folding the membership
tables.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from typing import Any

import pytest

from bettermemory import checkout_token
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.membership import (
    ASK,
    DEFERRED,
    GLOBAL,
    INDETERMINATE,
    NEW,
    SESSION_BRIEF,
    SILENT,
    UNREADABLE,
    Location,
    Membership,
    admitted,
)
from bettermemory.models import utcnow
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import MEMBERSHIP_TABLES, RecordHome

from ._mcp import call_tool
from .test_membership_cli import Repo, Scene, bm, scene, typed
from .test_membership_portable import clone_of, make_repo

__all__ = ["scene"]

URL = "example.invalid/acme/game"


def _rows(scene: Scene, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in scene.store.membership_log([kind]):
        payload = json.loads(row.payload)
        assert isinstance(payload, dict)
        out.append(payload)
    return out


def _health(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[str, dict[str, Any]]:
    capsys.readouterr()
    bm(monkeypatch, "health")
    text = capsys.readouterr().out
    bm(monkeypatch, "health", "--json")
    section = json.loads(capsys.readouterr().out)["membership"]
    assert isinstance(section, dict)
    return text, section


def test_e13_x14_every_suppression_leaves_a_row_and_health_counts_it(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    game = scene.see(scene.repo("game"))
    p1, p2 = scene.write(game), scene.write(game)
    other = scene.see(scene.repo("other"))
    model = scene.model
    turn = model.admit_recall(other, [p1, p2])
    assert turn.delivered == ()
    rows = _rows(scene, "recall_suppressed")
    assert [{s["id"] for s in r["suppressed"]} for r in rows] == [{p1, p2}]
    assert {s["reason"] for s in rows[0]["suppressed"]} == {"other_project"}
    assert rows[0]["checkout"] == other.checkout
    # A caller nothing can be read for: a row, and the turn's suppression.
    blind = model.resolve(
        Location(
            machine=scene.machine,
            cwd=str(scene.root / "locked"),
            state=UNREADABLE,
            detail="EACCES",
        )
    )
    assert blind.outcome == INDETERMINATE
    assert blind.lines == (
        f"bettermemory: cannot tell what {scene.root / 'locked'} is (EACCES); nothing "
        "from any project is shown here",
    )
    model.admit_recall(blind, [p1])
    assert [r["suppressed"][0]["reason"] for r in _rows(scene, "recall_suppressed")][
        -1
    ] == "unknown"
    assert len(_rows(scene, "resolution_indeterminate")) == 1
    # Delivered in its own project: nothing suppressed, no row.
    model.admit_recall(scene.see(scene.repo("game2")), [])
    assert scene.model.admit_recall(game, [p1]).delivered == (p1,)
    assert len(_rows(scene, "recall_suppressed")) == 2
    text, section = _health(monkeypatch, capsys)
    assert section["suppressed_recall_turns"] == {str(other.checkout): 1, "none": 1}
    assert (section["indeterminate_captures"], section["deferred_captures"]) == (1, 0)
    assert "Membership: suppressed recall turns (2):" in text
    assert f"  checkout {other.checkout}: 1" in text
    assert "Membership: captures 1 indeterminate, 0 deferred" in text


def test_i21_e36_c3_an_evicted_head_defers_one_prompt_and_health_counts_it(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    synced = scene.repo("proj3")
    first = scene.see(synced)
    m3 = scene.write(first)
    # The same folder on a second machine: the token and the history came
    # along; its HEAD is still evicted by the file provider.
    there = clone_of(synced, scene.root / "mac-2" / "proj3")
    shutil.copy(checkout_token.token_path(synced.git_dir), there.git_dir / "info")
    second = Membership(scene.store, "mac-2")
    there.evicted = True
    deferred = second.resolve(there.location("mac-2"))
    assert deferred.outcome == DEFERRED and deferred.project is None
    assert any("evicted" in line and "HEAD" in line for line in deferred.lines)
    assert [r["detail"] for r in _rows(scene, "resolution_deferred")] == ["HEAD"]
    there.evicted = False
    arrived = Membership(scene.store, "mac-2").resolve(there.location("mac-2"))
    assert (arrived.outcome, arrived.project) == (SILENT, first.project)
    home = scene.model.home(m3)
    assert home is not None and admitted(SESSION_BRIEF, home, arrived)
    text, section = _health(monkeypatch, capsys)
    assert section["deferred_captures"] == 1
    assert "Membership: captures 0 indeterminate, 1 deferred" in text


def test_e37_x13_why_through_a_question_and_its_answer(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work = scene.repo("proj", remote=URL)
    project = scene.see(work)
    scene.write(project)
    tip = work.commit()
    shallow = make_repo(scene.root / "s" / "proj", commits=0)
    shallow.history.commits[tip] = ()
    shallow.head = tip
    shallow.remotes = {URL}
    shallow.shallow = True
    asked = scene.see(shallow)
    assert asked.outcome == ASK
    typed(monkeypatch, "proj")
    bm(monkeypatch, "project", "join", str(project.project))
    s1 = scene.write(scene.see(shallow))
    capsys.readouterr()
    bm(monkeypatch, "project", "why", s1)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(
        f'{s1}: project "proj" ({project.project}), written in checkout {asked.checkout}'
    )
    kinds = [line.split()[3] for line in out[1:]]
    for kind in (
        "checkout_registered",
        "proposal_opened",
        "proposal_answered",
        "home_put",
    ):
        assert kind in kinds, kind
    answered = [line for line in out if " proposal_answered " in line]
    assert '"by":"declaration:os:tester"' in answered[0]
    g1 = scene.put(RecordHome(GLOBAL, None, "outside_repository"))
    bm(monkeypatch, "project", "why", g1)
    assert capsys.readouterr().out.startswith(f"{g1}: global (written outside any")


def test_the_line_of_an_open_question_repeats_until_answered(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream = scene.repo("app", remote=URL)
    scene.see(upstream)
    fork = clone_of(upstream, scene.root / "fork", remote="example.invalid/me/game")
    first = scene.see(fork)
    assert first.outcome == ASK
    line = first.lines[-1]
    for _ in range(3):
        again = scene.see(fork)
        assert again.outcome == ASK and line in again.lines
        assert line in scene.model.first_output(again)
    assert [pr.line for pr in scene.model.pending()] == [line]
    typed(monkeypatch, "fork")
    bm(monkeypatch, "project", "new")
    settled = scene.see(fork)
    assert settled.outcome == SILENT and line not in settled.lines
    assert line not in scene.model.first_output(settled)
    assert scene.model.pending() == []


def _sections(scene: Scene) -> tuple[str, str, str, Repo]:
    """Open question and provisional project (a fork), an unresolved
    project (a write where nothing could be read), a checkout whose
    folder is gone, and a retired one whose folder is gone too."""
    old_repo = scene.repo("old")
    old = scene.see(old_repo)
    scene.monkeypatch.chdir(scene.root)
    shutil.rmtree(old_repo.path)
    Membership(
        scene.store, scene.machine, now=lambda: utcnow() + timedelta(days=400)
    ).retire_quiet()
    upstream = scene.repo("app", remote=URL)
    scene.see(upstream)
    fork = clone_of(upstream, scene.root / "fork", remote="example.invalid/me/game")
    asked = scene.see(fork)
    gone_repo = scene.repo("gone")
    gone = scene.see(gone_repo)
    scene.monkeypatch.chdir(scene.root)
    shutil.rmtree(gone_repo.path)
    blind = Location(machine=scene.machine, cwd=str(scene.root / "x"), state=UNREADABLE)
    scene.model.resolve(blind)
    scene.put(scene.model.unresolved_home(blind, session="s1"))
    assert asked.proposal is not None and old.checkout and gone.checkout
    return str(asked.proposal), str(gone.checkout), str(old.checkout), fork


def test_x14_health_lists_questions_provisional_unresolved_and_checkouts_without_a_live_view(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    proposal, gone, old, fork = _sections(scene)
    text, section = _health(monkeypatch, capsys)
    pr = scene.model.proposal(proposal)
    assert section["open_proposals"] == [
        {
            "id": proposal,
            "checkout": pr.checkout,
            "path": str(fork.path),
            "kind": "first_sight",
            "reason": "one_witness",
            "line": pr.line,
            "opened": pr.opened,
        }
    ]
    assert [(p["name"], p["reason"]) for p in section["provisional_projects"]] == [
        ("fork", "one_witness")
    ]
    assert [
        (u["name"], u["state"], u["memories"]) for u in section["unresolved_projects"]
    ] == [("x", INDETERMINATE, 1)]
    without = [k["checkout"] for k in section["checkouts_without_live_view"]]
    assert gone in without and old not in without
    assert (
        f"Membership: open questions (1):\n  {proposal} in checkout {pr.checkout} at {fork.path}: {pr.line}"
        in text
    )
    assert (
        "Membership: provisional projects (1):\n  " in text
        and '"fork": one_witness' in text
    )
    assert '"x" (indeterminate) at ' in text and ": 1 memories, 0 episodes" in text
    assert f"  {gone}, last seen at {scene.root / 'gone'}" in text
    assert "Memory health" in text.splitlines()[0]


def test_health_on_a_store_without_membership_rows(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text, section = _health(monkeypatch, capsys)
    assert (
        section["open_proposals"] == [] and section["checkouts_without_live_view"] == []
    )
    assert "Membership: open questions (0):\n  (none)" in text


async def test_the_memory_health_tool_returns_the_membership_section(
    scene: Scene,
) -> None:
    proposal, gone, _old, _fork = _sections(scene)
    state = SessionState()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(scene.root / "store"))),
        store=scene.store,
        state=state,
        recorder=Recorder(store=scene.store, session_id=state.session_id),
    )
    report = await call_tool(server, "memory_admin", {"action": "health"})
    section = report["membership"]
    assert [p["id"] for p in section["open_proposals"]] == [proposal]
    assert gone in [k["checkout"] for k in section["checkouts_without_live_view"]]


def test_log_verify_folds_the_membership_tables(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream = scene.repo("app", remote=URL)
    res = scene.see(upstream)
    assert res.outcome == NEW
    scene.write(res)
    rows = sum(
        len(scene.store.membership_rows(t))
        for t in (
            "machines",
            "projects",
            "project_witnesses",
            "project_remotes",
            "checkouts",
            "checkout_views",
            "homes",
        )
    )
    with pytest.raises(SystemExit) as exc:
        bm(monkeypatch, "log", "verify")
    out = capsys.readouterr().out
    assert exc.value.code == 0, out
    assert f"  membership {rows} rows in {len(MEMBERSHIP_TABLES)} tables" in out
    assert "  fold ok" in out
