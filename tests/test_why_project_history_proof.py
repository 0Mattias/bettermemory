"""Project joins and their reversals stay visible in the home audit."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory import location, recall
from bettermemory.cli import main as cli_main
from bettermemory.location import resolve_caller

from .test_membership_questions import Bench


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bench]:
    recall.WORDS.clear()
    b = Bench(tmp_path, monkeypatch)
    try:
        yield b
    finally:
        b.close()
        recall.WORDS.clear()


@pytest.mark.parametrize("unjoined", [False, True])
@pytest.mark.parametrize("record_side", ["absorbed", "survivor"])
async def test_why_keeps_project_join_and_undo_rows_on_both_sides(
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unjoined: bool,
    record_side: str,
) -> None:
    fork, qid, _kid = bench.fork()
    theirs = (await bench.admin(action="answer_question", id=qid, answer="separate"))[
        "project"
    ]
    caller = resolve_caller(bench.store, bench.config, str(fork))
    written = bench.store.write(
        content="memory homed in the absorbed project",
        scopes=["tools"],
        home=caller.home(),
    )
    location.forget()
    monkeypatch.chdir(bench.app)
    await bench.admin(action="join_project", id=theirs, said="join the fork into app")
    if unjoined:
        await bench.admin(action="unjoin_project", id=theirs)
    record_id = written.id if record_side == "absorbed" else bench.memory
    why = bench.model().why(record_id)
    assert why.project == (
        theirs if unjoined and record_side == "absorbed" else bench.project
    )
    expected = {"project_merged", "project_absorbed"}
    if unjoined:
        expected.add("project_unmerged")
    actual = {row.kind for row in why.rows}
    assert expected <= actual, expected - actual
    selected = [row.seq for row in why.rows]
    assert selected == sorted(set(selected))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(bench.store_dir))
    monkeypatch.setattr(sys, "argv", ["bettermemory", "project", "why", record_id])
    capsys.readouterr()
    cli_main()
    printed = capsys.readouterr().out
    for kind in expected:
        assert kind in printed
