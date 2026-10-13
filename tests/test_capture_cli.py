"""`bettermemory capture review` and `bettermemory capture undo`, through
the CLI's own entry point over a scratch store."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bettermemory import capture

from .test_capture_batch import World, _fixed_origin  # noqa: F401
from .test_capture_batch import world as world  # noqa: F401


def _cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    world: World,
    *argv: str,
) -> tuple[int, str]:
    from bettermemory.cli import main

    monkeypatch.setenv("BETTERMEMORY_DIR", str(world.root))
    monkeypatch.setattr(sys, "argv", ["bettermemory", "capture", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


async def test_review_then_undo_through_the_cli(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _cli(monkeypatch, capsys, world, "review")
    assert (code, out) == (0, "Capture batches: none yet\nUndos: 0\n")

    batch = world.open()
    result = await world.capture_write(batch)
    assert result["status"] == "committed"
    # Still open: listed as such, and not undoable yet.
    code, out = _cli(monkeypatch, capsys, world, "review", "--json")
    assert json.loads(out)["batches"][0]["outcome"] == "open"
    code, out = _cli(monkeypatch, capsys, world, "undo", batch.batch, "--apply")
    assert code == 1 and "still open" in out

    capture.close_batch(
        world.store,
        batch,
        outcome=capture.OUTCOME_SETTLED,
        watermark=1,
        transcript_bytes=1,
    )
    code, out = _cli(monkeypatch, capsys, world, "undo", batch.batch)
    assert code == 0 and "dry-run" in out and result["id"] in out
    assert world.store.count_memories() == 1
    code, out = _cli(
        monkeypatch, capsys, world, "undo", batch.batch, "--apply", "--json"
    )
    report = json.loads(out)
    assert code == 0 and report["committed"] and report["to_undo"] == [result["id"]]
    assert world.store.count_memories() == 0
    code, out = _cli(monkeypatch, capsys, world, "review")
    assert "1 settled" in out and "1 undone" in out and "Undos: 1" in out


BODIES = (
    "The bettermemory daemon binds 127.0.0.1 only and listens on port 7397.",
    "Ruff 0.17.0 under /opt/homebrew/bin shadows the project's pinned ruff.",
    "The v9 store keeps its log in SQLite with a keyed MAC chain per row.",
)


async def _members(world: World) -> list[str]:
    """Three captured memories over two settled batches; the second
    batch's one member is then undone."""
    ids = []
    for bodies in (BODIES[:2], BODIES[2:]):
        batch = world.open()
        for body in bodies:
            result = await world.capture_write(batch, body)
            assert result["status"] == "committed", result
            ids.append(result["id"])
        capture.close_batch(
            world.store,
            batch,
            outcome=capture.OUTCOME_SETTLED,
            watermark=1,
            transcript_bytes=1,
        )
    from bettermemory import capture_review

    capture_review.apply_undo(
        world.store, capture_review.plan_undo(world.store, batch.batch)
    )
    return ids


async def test_sample_draws_live_captured_members_with_their_bodies(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second, undone = await _members(world)
    world.store.write(content="An ordinary memory never sampled.", scopes=["x"])
    code, out = _cli(monkeypatch, capsys, world, "sample", "--json", "--seed", "3")
    doc = json.loads(out)
    assert code == 0 and (doc["population"], doc["requested"]) == (2, 20)
    assert {s["memory_id"] for s in doc["samples"]} == {first, second}
    assert {s["body"].strip() for s in doc["samples"]} == set(BODIES[:2])
    # The draw is seeded: the same seed draws the same sample.
    code, again = _cli(monkeypatch, capsys, world, "sample", "--json", "--seed", "3")
    assert again == out
    code, out = _cli(monkeypatch, capsys, world, "sample", "--n", "1")
    assert code == 0 and out.count(BODIES[0]) + out.count(BODIES[1]) == 1
    assert undone not in out
    code, _ = _cli(monkeypatch, capsys, world, "sample", "--n", "0")
    assert code == 2


async def test_grade_records_a_capture_grade_and_the_tally_reads_it(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from datetime import datetime, timezone

    first, second, _undone = await _members(world)
    code, out = _cli(monkeypatch, capsys, world, "grade", first, "--meets")
    assert code == 0 and first in out
    code, out = _cli(
        monkeypatch,
        capsys,
        world,
        "grade",
        second,
        "--fails",
        "--rule",
        "1",
        "--rule",
        "5",
        "--note",
        "a test total",
        "--json",
    )
    assert code == 0
    grades = [e for e in world.store.iter_events() if e["kind"] == "capture_grade"]
    assert [(g["memory_id"], g["meets"]) for g in grades] == [
        (first, True),
        (second, False),
    ]
    assert grades[1]["rules"] == [1, 5] and grades[1]["note"] == "a test total"
    assert grades[0]["attribution"] == "cli_capture_grade"
    # Graded again, the latest grade of a memory in a week is the one counted.
    _cli(monkeypatch, capsys, world, "grade", second, "--meets")
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    code, out = _cli(monkeypatch, capsys, world, "sample", "--tally", "--json")
    assert json.loads(out) == {f"{year}-W{week:02d}": {"graded": 2, "meets": 2}}
    code, out = _cli(monkeypatch, capsys, world, "sample", "--tally")
    assert f"{year}-W{week:02d}: 2 of 2 meet all ten rules" in out


async def test_grade_refuses_what_it_cannot_record(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    first, _second, _undone = await _members(world)
    ordinary = world.store.write(content="An ordinary memory.", scopes=["x"])
    code, _ = _cli(monkeypatch, capsys, world, "grade", ordinary.id, "--meets")
    assert code == 1
    code, _ = _cli(monkeypatch, capsys, world, "grade", first, "--meets", "--rule", "2")
    assert code == 2
    code, _ = _cli(
        monkeypatch, capsys, world, "grade", first, "--fails", "--rule", "11"
    )
    assert code == 2
    code, _ = _cli(monkeypatch, capsys, world, "grade", first)
    assert code == 2
    # Telemetry off: a grade has nowhere to go, as for `review grade`.
    from bettermemory import config as config_module

    off = tmp_path / "off.toml"
    off.write_text("[telemetry]\nenabled = false\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "default_config_path", lambda: off)
    code, _ = _cli(monkeypatch, capsys, world, "grade", first, "--meets")
    assert code == 2
    assert not [e for e in world.store.iter_events() if e["kind"] == "capture_grade"]


def test_capture_alone_prints_its_help(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _cli(monkeypatch, capsys, world)
    assert code == 2 and "review" in out and "undo" in out
    assert "sample" in out and "grade" in out
