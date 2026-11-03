"""Health and eval read cold endorsement under one definition (ENG-170, G5).

`health_model.endorsement_tier` decides which `use` events feed a memory's
per-memory applied tallies, and both `compute_health` and `compute_eval`
read it: a shown pointer's settlement (`recall_settle.settle_shown`, a `use`
naming the surface that showed it) is a recorded use of the memory, so a
memory retrieved, auto-applied and then settled is not cold-endorsed on
either report. Eval's rate trio still leaves the settlement out.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from bettermemory.eval import (
    DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
    compute_eval,
    compute_report,
)
from bettermemory.health import compute_health, endorsement_tier
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.recall import is_settlement

_CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW = datetime(2026, 5, 20, tzinfo=timezone.utc)
_TS = "2026-05-15T12:00:00+00:00"
_FLOOR = 3


def _memory() -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_CREATED,
        updated=_CREATED,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="The build runs from the repository root.\n",
    )


def _search(mid: str) -> dict[str, Any]:
    return {"ts": _TS, "session": "s", "kind": "search", "returned": [mid]}


def _auto(mid: str) -> dict[str, Any]:
    return {
        "ts": _TS,
        "session": "s",
        "kind": "use",
        "outcome": "applied",
        "ids": [mid],
        "auto": True,
        "triggered_from": "stop_hook",
    }


def _settlement(mid: str) -> dict[str, Any]:
    """A pointer's settlement as `recall_settle.settle_shown` records it."""
    return {
        "ts": _TS,
        "session": "s",
        "kind": "use",
        "outcome": "applied",
        "ids": [mid],
        "auto": False,
        "attribution": "hook",
        "surface": "recall",
        "claim_excerpts": ["The build runs from the repository root."],
    }


def _hooked_turn() -> dict[str, Any]:
    """Stop-hook telemetry, so neither report's honesty gate empties the
    bucket."""
    return {
        "ts": _TS,
        "session": "s",
        "kind": "turn_audited",
        "verdict": "clean",
        "triggered_from": "stop_hook",
    }


def _cold(memory: Memory, events: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    health = compute_health(
        [memory],
        events,
        now=_NOW,
        cold_endorsement_min_retrievals=_FLOOR,
        hook_telemetry_events=0,
    )
    evaluated = compute_eval(
        [memory], events, now=_NOW, since=None, endorsement_min_retrievals=_FLOOR
    )
    return (
        {row.id for row in health.cold_endorsement_memories.rows},
        {row.id for row in evaluated.cold_endorsement_memories_rows},
    )


@pytest.mark.parametrize(
    ("settled", "auto_applied", "cold"),
    [
        (True, True, False),
        (True, False, False),
        (False, True, True),
    ],
    ids=["auto-then-settled", "settled-only", "auto-only"],
)
def test_health_and_eval_agree_on_cold_endorsement(
    settled: bool, auto_applied: bool, cold: bool
) -> None:
    memory = _memory()
    events = [_hooked_turn(), *(_search(memory.id) for _ in range(_FLOOR))]
    if auto_applied:
        events.append(_auto(memory.id))
    if settled:
        events.append(_settlement(memory.id))
    health, evaluated = _cold(memory, events)
    assert health == evaluated
    assert health == ({memory.id} if cold else set())


def test_the_settlement_stays_out_of_the_trio() -> None:
    """The per-memory tallies count the settlement; the endorsement rate's
    counters do not, as before."""
    memory = _memory()
    events = [
        _hooked_turn(),
        *(_search(memory.id) for _ in range(_FLOOR)),
        _auto(memory.id),
        _settlement(memory.id),
    ]
    report = compute_eval(
        [memory], events, now=_NOW, since=None, endorsement_min_retrievals=_FLOOR
    )
    assert report.applied_total == 1
    assert report.applied_explicit == 0
    assert report.explicit_endorsements_with_excerpt == 0


def _model_apply(mid: str) -> dict[str, Any]:
    """A deliberate `memory_record_use(applied)`: the model tier."""
    return {
        "ts": _TS,
        "session": "s",
        "kind": "use",
        "outcome": "applied",
        "ids": [mid],
        "auto": False,
    }


@pytest.mark.parametrize(
    ("threshold", "cold"),
    [(0.0, False), (0.05, False), (0.1, False), (0.2, True), (1.0, True)],
)
def test_health_and_eval_agree_under_the_ratio_threshold(
    threshold: float, cold: bool
) -> None:
    """One explicit apply among ten (ENG-192): the ratio half of the
    predicate lists the memory once the threshold passes its share, 0.1,
    on both reports, and eval's row carries the share health's row
    carries. Eval read no threshold before, so its bucket was the strict
    one at every threshold."""
    memory = _memory()
    events = [
        _hooked_turn(),
        *(_search(memory.id) for _ in range(_FLOOR)),
        _model_apply(memory.id),
        *(_auto(memory.id) for _ in range(9)),
    ]
    health = compute_health(
        [memory],
        events,
        now=_NOW,
        cold_endorsement_min_retrievals=_FLOOR,
        cold_endorsement_ratio_threshold=threshold,
        hook_telemetry_events=0,
    )
    evaluated = compute_eval(
        [memory],
        events,
        now=_NOW,
        since=None,
        endorsement_min_retrievals=_FLOOR,
        cold_endorsement_ratio_threshold=threshold,
    )
    health_rows = health.cold_endorsement_memories.rows
    eval_rows = evaluated.cold_endorsement_memories_rows
    assert [r.id for r in eval_rows] == [r.id for r in health_rows]
    assert [r.id for r in eval_rows] == ([memory.id] if cold else [])
    bucket = evaluated.to_dict()["cold_endorsement_memories"]
    assert bucket["ratio_threshold"] == threshold
    if cold:
        assert eval_rows[0].endorsement_ratio == health_rows[0].endorsement_ratio
        assert bucket["rows"][0]["endorsement_ratio"] == pytest.approx(0.1)
        assert bucket["rows"][0]["explicit_applied_count"] == 1


def test_one_predicate_reads_both_buckets() -> None:
    """The bucket's rules are written once (`health_model.weakly_endorsed`)
    and both reports call that function, not a copy of it; so is the
    reading of the threshold (`health_model.clamp_ratio_threshold`). The
    bindings are read from each report module's namespace, where its code
    finds them; neither module exports them."""
    from bettermemory import eval as eval_module
    from bettermemory import eval_rates, health, health_model

    assert vars(eval_rates)["weakly_endorsed"] is health_model.weakly_endorsed
    clamp = health_model.clamp_ratio_threshold
    assert vars(eval_module)["clamp_ratio_threshold"] is clamp
    assert vars(health)["clamp_ratio_threshold"] is clamp
    assert health_model.weakly_endorsed(10, 1, 0.2) is True
    assert health_model.weakly_endorsed(10, 1, 0.0) is False
    assert health_model.weakly_endorsed(10, 0, 0.0) is True
    assert health_model.weakly_endorsed(0, 0, 0.5) is False


@pytest.mark.parametrize(
    ("configured", "read"),
    [
        (math.inf, 1.0),
        (1.5, 1.0),
        (-math.inf, 0.0),
        (math.nan, 0.0),
        (-0.5, 0.0),
        (0.25, 0.25),
    ],
    ids=["inf", "above-1", "-inf", "nan", "negative", "in-range"],
)
def test_both_reports_read_the_threshold_within_0_and_1(
    configured: float, read: float
) -> None:
    """`[behavior] cold_endorsement_ratio_threshold` takes any number TOML
    spells, ``inf`` and ``nan`` included (ENG-192, reviews F3 and R3).
    Both reports read it as the nearer end of 0 to 1, ``inf`` as 1 like
    any number above it, NaN as 0: a memory every apply of which was
    explicit is listed by neither, where above 1 both listed it; one
    explicit apply in ten is listed from ``inf`` down to 0.25 and not
    at 0; and eval's JSON stays strict JSON, where an infinite threshold
    was written as ``Infinity``."""
    from bettermemory.health_model import clamp_ratio_threshold

    wholly, partly = _memory(), _memory()
    events = [
        _hooked_turn(),
        *(_search(wholly.id) for _ in range(_FLOOR)),
        _model_apply(wholly.id),
        _model_apply(wholly.id),
        *(_search(partly.id) for _ in range(_FLOOR)),
        _model_apply(partly.id),
        *(_auto(partly.id) for _ in range(9)),
    ]
    health = compute_health(
        [wholly, partly],
        events,
        now=_NOW,
        cold_endorsement_min_retrievals=_FLOOR,
        cold_endorsement_ratio_threshold=configured,
        hook_telemetry_events=0,
    )
    evaluated = compute_eval(
        [wholly, partly],
        events,
        now=_NOW,
        since=None,
        endorsement_min_retrievals=_FLOOR,
        cold_endorsement_ratio_threshold=configured,
    )
    listed = [partly.id] if read > 0.1 else []
    assert [r.id for r in health.cold_endorsement_memories.rows] == listed
    assert [r.id for r in evaluated.cold_endorsement_memories_rows] == listed
    bucket = json.loads(json.dumps(evaluated.to_dict(), allow_nan=False))
    assert bucket["cold_endorsement_memories"]["ratio_threshold"] == read
    assert clamp_ratio_threshold(configured) == read


def test_the_report_reads_the_threshold_rate_mode_reads() -> None:
    """`compute_report` (`bettermemory eval --report`) replays the log for
    both columns under the threshold rate mode reads (ENG-192, review
    N5): the memory one explicit apply in ten leaves weakly endorsed is
    in both replays' buckets at 0.2, as in `compute_eval`'s."""
    memory = _memory()
    events = [
        _hooked_turn(),
        *(_search(memory.id) for _ in range(DEFAULT_ENDORSEMENT_MIN_RETRIEVALS)),
        _model_apply(memory.id),
        *(_auto(memory.id) for _ in range(9)),
    ]
    doc = compute_report(
        [memory],
        events,
        now=_NOW,
        since=timedelta(days=30),
        version="t",
        cold_endorsement_ratio_threshold=0.2,
    )
    assert doc.window_eval is not doc.alltime_eval
    for report in (doc.alltime_eval, doc.window_eval):
        assert report.cold_endorsement_ratio_threshold == 0.2
        assert [r.id for r in report.cold_endorsement_memories_rows] == [memory.id]


def test_endorsement_tier_counts_a_settlement() -> None:
    settlement = _settlement("m")
    assert is_settlement(settlement)
    assert endorsement_tier(settlement) == "hook"
    assert endorsement_tier(_auto("m")) == "auto"
    assert endorsement_tier({**_auto("m"), "outcome": "ignored"}) is None
    assert endorsement_tier({"kind": "search", "outcome": "applied"}) is None
