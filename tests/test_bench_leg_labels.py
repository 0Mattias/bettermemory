"""Tests for `bench/leg_labels.py`, round 5's design instrument.

Rounds 3 and 4 both derived a withholding rule from a PROXY — "the
leg's rank-1 is the gold document" — and both paid for it on the dev
set. This instrument replaces the proxy with the counterfactual it was
standing in for: the gold document's fused rank with the leg voting
against with it withheld.

The threshold in addendum 7 is read off its labels, so the two ways it
could lie are pinned here: the counterfactual has to be the engine's
own suppression (not a reconstruction), and the verdict arithmetic has
to be the metric the bench actually reports.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType


from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    _RESCUE_COVERAGE_GATE,
    _RESCUE_LEG_MIN_EVIDENCE,
    _leg_evidence_weight,
)

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "leg_labels.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_leg_labels", _BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_leg_labels"] = module
    spec.loader.exec_module(module)
    return module


labels = _load()


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
            "Credential injection for containers uses mounted files.",
            created=now - timedelta(days=5),
        ),
        _memory("The reconciliation job runs at 0300 UTC.", created=now),
        _memory("Dependency bumps are batched weekly.", created=now),
    ]


# ---------------------------------------------------------------------------
# The verdict arithmetic
# ---------------------------------------------------------------------------


def test_crossing_the_k_boundary_outranks_a_bare_rank_move() -> None:
    """The bench scores recall@5, so a leg that pulls the gold across
    that boundary is categorically different from one that shuffles it
    inside the head — the labels have to say which."""
    assert labels.K == 5


def test_a_leg_that_rescues_the_gold_into_the_head_is_labelled_helped() -> None:
    memories = _corpus()
    gold = memories[0].id
    got = labels.label_leg(memories, "creds", gold)
    assert got is not None
    assert got["helped"] is True
    assert got["verdict"] in ("rescued", "improved")
    assert got["rank_delta"] > 0


def test_a_confident_query_has_no_leg_to_label() -> None:
    """The gate never opens, so there is no counterfactual — and a
    record here would put an unengaged leg in the denominator every
    threshold below is derived against."""
    memories = _corpus()
    assert (
        labels.label_leg(memories, "reconciliation job 0300 UTC", memories[1].id)
        is None
    )


def test_a_stopword_only_query_is_skipped() -> None:
    memories = _corpus()
    assert labels.label_leg(memories, "the and of", memories[0].id) is None


# ---------------------------------------------------------------------------
# The counterfactual is the engine's own
# ---------------------------------------------------------------------------


def test_labelling_restores_every_constant_it_borrows() -> None:
    """Both arms are produced by moving module-level constants. A leaked
    mutation would silently change the ranking for every later caller —
    including the recall runs these labels precede."""
    memories = _corpus()
    labels.label_leg(memories, "creds", memories[0].id)
    import bettermemory.search as engine

    assert engine._RESCUE_LEG_MIN_EVIDENCE == _RESCUE_LEG_MIN_EVIDENCE
    assert engine._leg_evidence_weight is _leg_evidence_weight
    assert engine._RESCUE_COVERAGE_GATE == _RESCUE_COVERAGE_GATE


def test_an_absent_gold_is_worse_than_any_observed_rank() -> None:
    """A gold pushed out of the depth window must score as harmed, not
    as a tie — censoring it to `None` and comparing would read as
    neutral and hide the worst outcome the leg can produce."""
    assert labels.DEPTH > labels.K


# ---------------------------------------------------------------------------
# The summary the threshold is read off
# ---------------------------------------------------------------------------


def test_summary_splits_helped_from_hurt_and_keeps_neutral_apart() -> None:
    records = [
        {
            "verdict": "rescued",
            "helped": True,
            "top_matched": 3,
            "standout": 5.0,
            "margin_ratio": 0.4,
            "top_score": 9.0,
            "leg_size": 20,
        },
        {
            "verdict": "harmed",
            "helped": False,
            "top_matched": 1,
            "standout": 1.1,
            "margin_ratio": 0.05,
            "top_score": 4.0,
            "leg_size": 30,
        },
        {
            "verdict": "neutral",
            "helped": False,
            "top_matched": 2,
            "standout": 2.0,
            "margin_ratio": 0.2,
            "top_score": 6.0,
            "leg_size": 25,
        },
    ]
    out = labels.summarise(records)
    assert out["helped"] == 1 and out["hurt"] == 1 and out["neutral"] == 1
    assert out["verdicts"] == {"rescued": 1, "harmed": 1, "neutral": 1}
    assert out["top_matched"]["helped"]["p50"] == 3
    assert out["top_matched"]["hurt"]["p50"] == 1


def test_summary_survives_an_empty_population() -> None:
    out = labels.summarise([])
    assert out["labelled_legs"] == 0
    assert out["top_matched"]["helped"] is None


# ---------------------------------------------------------------------------
# The committed labels the threshold is read off
# ---------------------------------------------------------------------------


def test_the_committed_labels_support_the_preregistered_rule() -> None:
    """Addendum 7 requires the leg's rank-1 to match at least two
    synthesized terms. Pinned because that separation IS the document's
    argument: one matched term is a coincidence, two independent ones
    agreeing on the same document is evidence.

    If these counts move, the rule has to be re-derived in a new
    pre-registration rather than the constant quietly surviving.
    """
    import json

    path = _BENCH.parent / "retrieval" / "results" / "leg-labels-2026-08-10.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    recs = payload["records"]
    helped = [r for r in recs if r["helped"]]
    hurt = [r for r in recs if r["verdict"] in ("broke", "harmed")]
    assert len(helped) == 21 and len(hurt) == 3
    assert payload["summary"]["labelled_legs"] == 39

    # Perfect separation: every harmful leg matched exactly one term,
    # and no helpful leg did.
    assert all(r["top_matched"] <= 1 for r in hurt)
    assert all(r["top_matched"] >= 2 for r in helped)

    # …and the rules it replaces were paying helpful legs to catch them.
    assert sum(1 for r in helped if r["margin_ratio"] < 0.12) == 9
    assert sum(1 for r in helped if r["standout"] < 2.5) == 7
