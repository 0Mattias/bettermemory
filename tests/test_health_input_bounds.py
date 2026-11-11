"""ENG-279: health handles large day windows and nonpositive list caps."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bettermemory import config as config_module
from bettermemory.cli import health_cmd
from bettermemory.config import Config
from bettermemory.events import Recorder
from bettermemory.health import compute_health
from bettermemory.store import Store

from .test_health import _event, _memory, _utc


@pytest.mark.parametrize("days", [-1, 0, 30, 800_000, 10**20, -(10**20)])
def test_a_large_dead_weight_window_preserves_its_meaning(days: int) -> None:
    memory = _memory(created=_utc(2020, 1, 1))
    events = [
        _event("search", ts=_utc(2026, 4, day), returned=[memory.id]) for day in (1, 5)
    ]
    report = compute_health([memory], events, window_days=days, now=_utc(2026, 5, 2))
    assert report.window_days == days
    assert [row.id for row in report.dead_weight] == ([memory.id] if days <= 30 else [])


@pytest.mark.parametrize("days", [-1, 0, 30, 800_000, 10**20, -(10**20)])
def test_a_large_verification_window_preserves_its_meaning(days: int) -> None:
    memory = _memory(created=_utc(2019, 1, 1), last_verified_at=_utc(2020, 1, 1))
    report = compute_health(
        [memory], [], verification_stale_days=days, now=_utc(2026, 5, 2)
    )
    assert report.verification_debt.stale_total == (1 if days <= 30 else 0)


@pytest.mark.parametrize(
    ("cap", "expected"), [(-2, 0), (-1, 0), (0, 0), (1, 1), (2, 2), (10, 3)]
)
def test_nonpositive_heavily_used_caps_select_no_records(
    cap: int, expected: int
) -> None:
    memories = [_memory(body=f"heavily used {i}") for i in range(3)]
    events = [
        _event("use", ids=[memory.id], outcome="applied")
        for memory, count in zip(memories, [3, 2, 1], strict=True)
        for _ in range(count)
    ]
    report = compute_health(
        memories,
        events,
        heavily_used_top_k=cap,
        heavily_used_min_applied=1,
        now=_utc(2026, 5, 2),
    )
    assert [row.id for row in report.heavily_used] == [
        memory.id for memory in memories[:expected]
    ]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [(["--days", "800000"], 3), (["--top-k", "-1"], 0), (["--top-k", "1"], 1)],
)
def test_the_cli_accepts_large_windows_and_obeys_its_list_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    expected: int,
) -> None:
    with Store(tmp_path / "store") as store:
        recorder = Recorder(store=store, session_id="synthetic_health", enabled=True)
        for i, count in enumerate([3, 2, 1]):
            memory = store.put_memory(_memory(body=f"CLI health record {i}"))
            for _ in range(count):
                recorder.record("use", ids=[memory.id], outcome="applied")
        config = Config()
        monkeypatch.setattr(
            health_cmd,
            "cli_context",
            lambda: SimpleNamespace(store=store, config=config),
        )
        monkeypatch.setattr(health_cmd, "capture_origin", lambda: None)
        monkeypatch.setattr(health_cmd, "_existing_anchoring", lambda _store: None)
        monkeypatch.setattr(health_cmd, "_membership_section", lambda _store: None)
        monkeypatch.setattr(config_module, "load_config", lambda: config)
        parser = argparse.ArgumentParser()
        health_cmd.add_subparser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["health", "--json", "--min-applied", "1", *extra])
        health_cmd.run(args)
    report = json.loads(capsys.readouterr().out)
    assert len(report["heavily_used"]) == expected
