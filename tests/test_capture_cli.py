"""`bettermemory capture review` and `bettermemory capture undo`, through
the CLI's own entry point over a scratch store."""

from __future__ import annotations

import json
import sys

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


def test_capture_alone_prints_its_help(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _cli(monkeypatch, capsys, world)
    assert code == 2 and "review" in out and "undo" in out
