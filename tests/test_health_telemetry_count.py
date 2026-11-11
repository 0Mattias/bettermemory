"""ENG-279: measuring one event stream twice never doubles its row count."""

from __future__ import annotations

from typing import Any

import pytest

from bettermemory.health import compute_health
from bettermemory.models import Memory

from .test_health import _event, _memory, _utc


def _stream(kind: str, count: int) -> tuple[Memory, list[dict[str, Any]]]:
    memory = _memory()
    fields = (
        {"triggered_from": "stop_hook", "verdict": "ok"}
        if kind == "turn_audited"
        else {"ids": [memory.id], "outcome": "applied", "attribution": "hook"}
    )
    return memory, [_event(kind, ts=_utc(2026, 5, 1), **fields) for _ in range(count)]


@pytest.mark.parametrize("kind", ["turn_audited", "use"])
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("premeasured", [False, True])
def test_coverage_counts_each_telemetry_row_once(
    kind: str, count: int, premeasured: bool
) -> None:
    memory, events = _stream(kind, count)
    report = compute_health(
        [memory],
        iter(events),
        now=_utc(2026, 5, 2),
        hook_telemetry_events=count if premeasured else 0,
    )
    coverage = report.telemetry_coverage
    assert coverage is not None and coverage.covered is True
    assert coverage.hook_telemetry_events == count
    assert report.to_dict()["telemetry_coverage"]["hook_telemetry_events"] == count


@pytest.mark.parametrize("kind", ["turn_audited", "use"])
@pytest.mark.parametrize("count", [1, 3])
def test_a_caller_that_did_not_measure_keeps_the_gate_off(
    kind: str, count: int
) -> None:
    memory, events = _stream(kind, count)
    report = compute_health(
        [memory], iter(events), now=_utc(2026, 5, 2), hook_telemetry_events=None
    )
    assert report.telemetry_coverage is None
