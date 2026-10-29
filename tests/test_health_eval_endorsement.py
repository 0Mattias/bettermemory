"""Health and eval read cold endorsement under one definition (ENG-170, G5).

`health.endorsement_tier` decides which `use` events feed a memory's
per-memory applied tallies, and both `compute_health` and `compute_eval`
read it: a shown pointer's settlement (`recall.settle_shown`, a `use`
naming the surface that showed it) is a recorded use of the memory, so a
memory retrieved, auto-applied and then settled is not cold-endorsed on
either report. Eval's rate trio still leaves the settlement out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from bettermemory.eval import compute_eval
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
    """A pointer's settlement as `recall.settle_shown` records it."""
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


def test_endorsement_tier_counts_a_settlement() -> None:
    settlement = _settlement("m")
    assert is_settlement(settlement)
    assert endorsement_tier(settlement) == "hook"
    assert endorsement_tier(_auto("m")) == "auto"
    assert endorsement_tier({**_auto("m"), "outcome": "ignored"}) is None
    assert endorsement_tier({"kind": "search", "outcome": "applied"}) is None
