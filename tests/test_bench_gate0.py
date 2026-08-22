"""Tests for `bench/longmemeval/gate0.py`, round 2's pre-run kill.

This scorer retired a preregistered experiment. That is the highest-cost
verdict any code in `bench/` produces — it is the difference between "we
did not find a design" and "there is not one to find" — so the judge is
tested the way `tests/test_changelog.py` tests its own: with fixtures
that fire, fixtures that stay quiet, and a check that each tier can tell
a passing world from a failing one.

A green run against today's artifacts says nothing on its own. It is
green whenever the committed census happens to fail the gate, and
equally green against a judge hardwired to return KILL. The synthetic
fixtures below are what make it evidence.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_BENCH = Path(__file__).resolve().parents[1] / "bench" / "longmemeval" / "gate0.py"
_RESULTS = _BENCH.parent / "results"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_gate0", _BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_gate0"] = module
    spec.loader.exec_module(module)
    return module


gate0 = _load()


def _terms(*ratios: float, dead: int = 0) -> list[dict[str, object]]:
    """Emitted terms at the given df ratios, plus `dead` zero-df ones."""
    out: list[dict[str, object]] = [
        {"term": f"t{i}", "df": 1, "df_ratio": r} for i, r in enumerate(ratios)
    ]
    out += [{"term": f"z{i}", "df": 0, "df_ratio": 0.0} for i in range(dead)]
    return out


def _dev(probe: str, engaged: bool, *ratios: float) -> dict[str, object]:
    return {"probe": probe, "engaged": engaged, "terms": _terms(*ratios)}


# ---------------------------------------------------------------------------
# The population the gate is judged over
# ---------------------------------------------------------------------------


def test_recall_at_counts_ranks_below_k() -> None:
    rec = {"n_evidence": 2, "evidence_ranks": [0, 7]}
    assert gate0.recall_at(rec, 5) == 0.5
    assert gate0.recall_at(rec, 10) == 1.0
    assert gate0.recall_at({"n_evidence": 0, "evidence_ranks": []}, 5) == 0.0


def test_unretrieved_evidence_is_a_miss_not_a_crash() -> None:
    """`evidence_ranks` carries None for evidence the search never
    surfaced — the sidecar's own encoding for a total miss."""
    assert gate0.recall_at({"n_evidence": 2, "evidence_ranks": [None, None]}, 5) == 0.0
    assert gate0.recall_at({"n_evidence": 2, "evidence_ranks": [1, None]}, 5) == 0.5


def test_regressed_population_is_strictly_worse_at_k5() -> None:
    """Only questions the lane moved DOWN count. Unchanged and improved
    questions are not what a repair is aimed at, and sweeping them in
    would dilute the median the separability check reads."""
    base = {
        "down": {"n_evidence": 2, "evidence_ranks": [0, 1]},
        "same": {"n_evidence": 1, "evidence_ranks": [0]},
        "up": {"n_evidence": 2, "evidence_ranks": [0, 9]},
    }
    lane = {
        "down": {"n_evidence": 2, "evidence_ranks": [0, 9]},
        "same": {"n_evidence": 1, "evidence_ranks": [0]},
        "up": {"n_evidence": 2, "evidence_ranks": [0, 1]},
    }
    assert gate0.regressed_qids(base, lane) == ["down"]


def test_zero_df_terms_are_excluded_from_the_ratio_pool() -> None:
    """`morph_variants` emits non-words that match nothing. Letting them
    into the pool would drag every median toward zero and make the gate
    look better separated than it is."""
    records = {"q": {"terms": _terms(0.2, 0.4, dead=6)}}
    assert gate0.live_ratios(records, ["q"]) == [0.2, 0.4]


# ---------------------------------------------------------------------------
# Gate 0a — separability
# ---------------------------------------------------------------------------


def test_separability_passes_when_the_classes_are_far_apart() -> None:
    """The world the hypothesis predicted: the regressed questions'
    emitted terms are an order of magnitude more common."""
    held = {"q": {"terms": _terms(0.50, 0.50)}}
    dev = [_dev("asked", True, 0.05, 0.05)]
    out = gate0.score(held, dev, ["q"], tau=0.05)
    a = out["gate_0a_separability"]
    assert a["passes"] is True
    assert a["worst_case_multiple"] == 10.0


def test_separability_fails_when_the_classes_overlap() -> None:
    """The world measured: the two populations sit in the same band."""
    held = {"q": {"terms": _terms(0.03, 0.03)}}
    dev = [_dev("asked", True, 0.036, 0.036)]
    out = gate0.score(held, dev, ["q"], tau=0.05)
    assert out["gate_0a_separability"]["passes"] is False
    assert out["verdict"] == "KILL"


def test_separability_verdict_is_the_worst_reading_not_the_kindest() -> None:
    """Four readings of "the dev set's rescued questions" are computed,
    and one of them passing is not enough. Choosing the flattering
    reading after seeing the answer is the move a pre-registration
    exists to prevent."""
    held = {"q": {"terms": _terms(0.50)}}
    # Engaged asked probes are rare (passes at 10x); the whole pool is
    # common enough to fail, so the conjunction must fail.
    dev = [_dev("asked", True, 0.05), _dev("control", False, 0.40)]
    out = gate0.score(held, dev, ["q"], tau=0.05)
    readings = out["gate_0a_separability"]["readings"]
    assert readings["asked probe, leg engaged"]["passes"] is True
    assert readings["all probes"]["passes"] is False
    assert out["gate_0a_separability"]["passes"] is False


# ---------------------------------------------------------------------------
# Gate 0b — reachability
# ---------------------------------------------------------------------------


def test_reachability_needs_the_population_not_just_the_poster_child() -> None:
    """`a89d7624` being altered is necessary and NOT sufficient: a gate
    that repairs the one question the README quotes and nothing else
    cannot repair the class."""
    held = {gate0.CLEANEST_PROOF_QID: {"terms": _terms(0.9)}}
    held.update({f"q{i}": {"terms": _terms(0.01)} for i in range(24)})
    qids = list(held)
    out = gate0.score(held, [_dev("asked", True, 0.05)], qids, tau=0.05)
    b = out["gate_0b_reachability"]
    assert b["cleanest_proof_altered"] is True
    assert b["regressed_questions_altered"] == 1
    assert b["passes"] is False


def test_reachability_needs_the_poster_child_too() -> None:
    """The population bar alone is not sufficient either."""
    held = {f"q{i}": {"terms": _terms(0.9)} for i in range(25)}
    held[gate0.CLEANEST_PROOF_QID] = {"terms": _terms(0.001)}
    out = gate0.score(held, [_dev("asked", True, 0.05)], list(held), tau=0.05)
    b = out["gate_0b_reachability"]
    assert b["regressed_questions_altered"] >= gate0.REACHABILITY_MIN_QUESTIONS
    assert b["cleanest_proof_altered"] is False
    assert b["passes"] is False


def test_both_gates_passing_is_the_only_way_to_a_pass_verdict() -> None:
    held = {f"q{i}": {"terms": _terms(0.9)} for i in range(24)}
    held[gate0.CLEANEST_PROOF_QID] = {"terms": _terms(0.9)}
    out = gate0.score(held, [_dev("asked", True, 0.05)], list(held), tau=0.05)
    assert out["gate_0a_separability"]["passes"] is True
    assert out["gate_0b_reachability"]["passes"] is True
    assert out["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# The committed verdict
# ---------------------------------------------------------------------------


def test_thresholds_match_the_preregistration() -> None:
    """Addendum 4 fixed these before the census existed. A later edit to
    any of them re-opens a retired experiment on changed rules. The
    preregistration prose moved to the owner-side archive with the rest
    of the bench documents (2026-08-21); these literals are the pinned
    thresholds it fixed."""
    assert gate0.TAU == 0.05
    assert gate0.SEPARABILITY_MULTIPLE == 5.0
    assert gate0.REACHABILITY_MIN_QUESTIONS == 20


def test_the_published_verdict_is_the_kill() -> None:
    """The committed artifact, so the README's claim and the file agree.

    If this ever flips to PASS the experiment is un-retired, and that
    has to be a deliberate, reviewed change with a new pre-registration
    — not a quiet consequence of an artifact being regenerated.
    """
    import json

    verdict = json.loads(
        (_RESULTS / "gate0-2026-08-10.json").read_text(encoding="utf-8")
    )
    assert verdict["verdict"] == "KILL"
    assert verdict["gate_0a_separability"]["worst_case_multiple"] < 1.0
    assert verdict["gate_0b_reachability"]["regressed_questions_altered"] < 20


@pytest.mark.parametrize("path", ["df-census-2026-08-10.json", "gate0-2026-08-10.json"])
def test_the_verdicts_inputs_are_committed(path: str) -> None:
    """The kill is reproducible from the repository, not from a re-run."""
    assert (_RESULTS / path).exists()
