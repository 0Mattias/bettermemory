"""Tests for `bench/leg_census.py`, round 3's design instrument.

The threshold in the LongMemEval preregistration addendum 5 is read straight off
this census, so a census that rebuilt a DIFFERENT leg than the engine
scores would move that threshold with nothing noticing. The
reconstruction is therefore pinned against `search()`'s own observable
output rather than against literals: whatever the engine labels
`matched_leg="expansion"` has to appear in the leg this module rebuilds.

The engagement half is pinned the same way — the census must engage
exactly when the shipped coverage gate engages, because a census that
sampled a wider or narrower population than the mechanism would
calibrate the threshold against the wrong distribution.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import _RESCUE_COVERAGE_GATE, search

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "leg_census.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_leg_census", _BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_leg_census"] = module
    spec.loader.exec_module(module)
    return module


census = _load()


def _memory(body: str, *, created: datetime | None = None) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _corpus() -> list[Memory]:
    now = datetime.now(timezone.utc)
    return [
        _memory(
            "Feature flag removal has an owner and a deadline; stale flags "
            "are cleaned up in the monthly sweep.",
            created=now - timedelta(days=5),
        ),
        _memory(
            "Credential injection for containers uses mounted files.",
            created=now - timedelta(days=4),
        ),
        _memory(
            "Dependency bumps are batched weekly in the bot queue.",
            created=now - timedelta(days=3),
        ),
        _memory(
            "The escalation rotation pages the secondary after fifteen minutes.",
            created=now - timedelta(days=8),
        ),
    ]


# ---------------------------------------------------------------------------
# The reconstruction is the engine's leg
# ---------------------------------------------------------------------------


def test_every_expansion_labelled_hit_is_in_the_rebuilt_leg() -> None:
    """The load-bearing pin. A hit the engine attributes to the expansion
    leg must be a candidate the rebuilt leg ranked; if it is not, this
    module is measuring some other leg and every statistic it produces
    is about the wrong thing."""
    memories = _corpus()
    for query in ("creds", "do we ever rip the old toggles back out"):
        legs: dict[str, str] = {}
        hits = search(
            memories, query, max_results=4, matched_leg_out=legs, rescue_expansion=True
        )
        expansion_hits = {h.id for h in hits if legs.get(h.id) == "expansion"}
        if not expansion_hits:
            continue
        rebuilt = census.leg_for(memories, query)
        assert rebuilt is not None, f"engine ran a leg for {query!r}, census did not"
        assert expansion_hits <= set(rebuilt["ranked_ids"]), query


def test_engagement_matches_the_shipped_coverage_gate() -> None:
    """Engages exactly when the gate does. A confident query returns
    None; a paraphrase-only one returns a leg."""
    memories = _corpus()
    assert census.leg_for(memories, "feature flag removal owner deadline") is None
    leg = census.leg_for(memories, "creds")
    assert leg is not None
    assert leg["coverage"] < _RESCUE_COVERAGE_GATE


def test_no_leg_when_the_tables_know_no_vocabulary() -> None:
    """A low-coverage query the tables cannot expand has no leg to
    measure, and must not be recorded as an engaged zero."""
    assert census.leg_for(_corpus(), "kubernetes ingress controller topology") is None


def test_a_stopword_only_query_is_skipped_like_the_engine_skips_it() -> None:
    """`search()` runs the stopword fallback and skips the rescue
    entirely; the census must not invent a leg there."""
    assert census.leg_for(_corpus(), "the and of") is None


def test_the_probe_restores_the_gate_it_borrows() -> None:
    """The census silences the leg by moving the module-level gate to
    read the base fusion. A leaked mutation would disable the rescue for
    every later caller in the process, including the recall runs this
    census precedes."""
    census.leg_for(_corpus(), "creds")
    import bettermemory.search as engine

    assert engine._RESCUE_COVERAGE_GATE == _RESCUE_COVERAGE_GATE


# ---------------------------------------------------------------------------
# The recorded evidence
# ---------------------------------------------------------------------------


def test_margin_ratio_is_scale_free_and_bounded() -> None:
    """`margin_ratio` is the signal addendum 5 keys on, chosen because a
    raw score or margin is not comparable across corpora with different
    sizes and IDF scales. It must be a ratio in [0, 1]."""
    leg = census.leg_for(_corpus(), "creds")
    assert leg is not None
    assert 0.0 <= leg["margin_ratio"] <= 1.0
    expected = (leg["top_score"] - leg["runner_up_score"]) / leg["top_score"]
    assert leg["margin_ratio"] == pytest.approx(expected, abs=1e-4)


def test_a_single_candidate_leg_is_maximally_separated() -> None:
    """One candidate means nothing competes with it, so the runner-up is
    0 and the ratio is 1.0 — the cap must never fire on that shape."""
    memories = [_memory("Credential injection for containers uses mounted files.")]
    leg = census.leg_for(memories, "creds")
    assert leg is not None
    assert leg["leg_size"] == 1
    assert leg["runner_up_score"] == 0.0
    assert leg["margin_ratio"] == 1.0


# ---------------------------------------------------------------------------
# The summary split
# ---------------------------------------------------------------------------


def test_summary_splits_right_legs_from_wrong_ones() -> None:
    """The split is the whole design: a cap keyed on leg evidence is
    only derivable if the legs that voted wrong had visibly worse
    evidence than the legs that voted right."""
    records = [
        {
            "engaged": True,
            "leg_top_is_gold": True,
            "top_score": 10.0,
            "margin": 2.0,
            "margin_ratio": 0.20,
            "top_matched": 2,
            "leg_size": 30,
        },
        {
            "engaged": True,
            "leg_top_is_gold": False,
            "top_score": 6.0,
            "margin": 0.3,
            "margin_ratio": 0.05,
            "top_matched": 1,
            "leg_size": 40,
        },
        {"engaged": False},
    ]
    out = census.summarise(records)
    assert out["questions"] == 3
    assert out["engaged"] == 2
    assert out["leg_top_is_gold"] == 1
    assert out["leg_top_is_wrong"] == 1
    assert out["margin_ratio"]["right"]["p50"] == 0.20
    assert out["margin_ratio"]["wrong"]["p50"] == 0.05
    assert out["margin_ratio"]["all_engaged"]["n"] == 2


def test_summary_survives_a_population_with_no_engaged_legs() -> None:
    out = census.summarise([{"engaged": False}])
    assert out["engaged"] == 0
    assert out["margin_ratio"]["right"] is None


# ---------------------------------------------------------------------------
# The committed census the threshold is read off
# ---------------------------------------------------------------------------


def test_the_committed_census_supports_the_preregistered_threshold() -> None:
    """Addendum 5 fixes theta at 0.12 by a stated rule: the largest round
    value below the dev set's correct-leg margin_ratio p25, so the cap is
    non-binding for the upper three quarters of legs that vote right.

    Pinned because the derivation is the document's whole claim to not
    having fitted the number. If the census moves, the rule has to be
    re-applied in a new pre-registration rather than the constant being
    quietly kept.
    """
    import json

    path = _BENCH.parent / "retrieval" / "results" / "leg-census-2026-08-10.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    right = [r for r in payload["records"] if r["engaged"] and r["leg_top_is_gold"]]
    wrong = [r for r in payload["records"] if r["engaged"] and not r["leg_top_is_gold"]]
    assert len(right) == 14 and len(wrong) == 27

    ratios = sorted(r["margin_ratio"] for r in right)
    p25 = ratios[len(ratios) // 4]
    assert p25 == pytest.approx(0.1235, abs=1e-4)
    assert 0.12 < p25, "theta must sit below the correct legs' p25"

    kept_right = sum(1 for r in right if r["margin_ratio"] >= 0.12)
    kept_wrong = sum(1 for r in wrong if r["margin_ratio"] >= 0.12)
    assert kept_right == 12, "the cap must keep 12 of the 14 correct legs"
    assert kept_wrong == 4, "the cap must drop 23 of the 27 incorrect legs"
    precision = kept_right / (kept_right + kept_wrong)
    baseline = len(right) / (len(right) + len(wrong))
    assert precision == pytest.approx(0.75, abs=1e-3)
    assert precision / baseline >= 1.5
