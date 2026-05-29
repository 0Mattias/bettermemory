"""Tests for the comparative-evaluation harness.

These pin the harness's honesty contract: bettermemory runs for real and
produces a genuine recall@k and silent_miss_rate; the live-agent metrics
read as n/a offline (not 0.0); competitors are recorded as unavailable
with a capability row, never with fabricated numbers.
"""

from __future__ import annotations

import json

import pytest

from .adapters import (
    BetterMemoryAdapter,
    SystemUnavailable,
    claude_mem_adapter,
    default_adapters,
    mem0_adapter,
    server_memory_adapter,
)
from .comparative import render_json, render_text, run_comparative
from .workload import default_workload


def test_default_workload_shape():
    wl = default_workload()
    assert len(wl.facts) == 10
    assert len(wl.probes) == 10
    # gold keys all resolve to a real fact id
    for probe in wl.gold_probes:
        assert wl.gold_id(probe) is not None
    assert len(wl.gold_probes) == 7
    assert len(wl.expected_miss_probes) == 5


def test_workload_rejects_dangling_gold_key():
    from .workload import Workload, WorkloadFact, WorkloadProbe

    with pytest.raises(ValueError):
        Workload(
            name="bad",
            facts=[WorkloadFact(key="a", scopes=["projects:x"], body="alpha beta")],
            probes=[WorkloadProbe("query terms", "missing", agent_searched=False)],
        )


def test_bettermemory_recall_is_perfect_on_crafted_workload():
    wl = default_workload()
    result = BetterMemoryAdapter().run(wl, k=5)
    assert result.ran is True
    assert result.gold_total == 7
    # The corpus is built so every gold probe has an unambiguous top hit.
    assert result.recalled == 7
    assert result.recall_at_k == pytest.approx(1.0)


def test_silent_miss_lane_matches_workload_intent():
    wl = default_workload()
    result = BetterMemoryAdapter().run(wl, k=5)
    assert result.eval_report is not None
    ev = result.eval_report

    # Every probe is an audited turn.
    assert ev.turns_audited == len(wl.probes) == 10
    # Exactly the gold + not-searched probes register as silent misses.
    assert ev.silent_misses == len(wl.expected_miss_probes) == 5

    rate = ev.silent_miss_rate
    assert rate.numerator == 5
    assert rate.denominator == 10
    assert rate.rate == pytest.approx(0.5)
    # A real Wilson interval is produced (denominator > 0).
    assert rate.lower is not None and rate.upper is not None
    assert rate.lower < 0.5 < rate.upper


def test_live_agent_metrics_are_na_offline_not_zero():
    wl = default_workload()
    result = BetterMemoryAdapter().run(wl, k=5)
    assert result.eval_report is not None
    ev = result.eval_report
    # No `use` / retrieval events are fed offline, so these have a zero
    # denominator and read as None — the honest "not measured" answer,
    # never a misleading 0.0.
    assert ev.memory_helped_rate.rate is None
    assert ev.endorsement_rate.rate is None
    assert ev.memory_helped_rate.denominator == 0
    assert ev.endorsement_rate.denominator == 0


def test_capability_matrix_only_bettermemory_computes_trio():
    assert BetterMemoryAdapter().capabilities().can_compute_trio is True
    for adapter in (mem0_adapter(), server_memory_adapter(), claude_mem_adapter()):
        caps = adapter.capabilities()
        assert caps.can_compute_trio is False
        # Each missing-signal row spells out which signal is absent.
        assert caps.notes


def test_competitor_adapters_raise_with_reason():
    wl = default_workload()
    for adapter in (mem0_adapter(), server_memory_adapter(), claude_mem_adapter()):
        with pytest.raises(SystemUnavailable) as exc:
            adapter.run(wl, k=5)
        assert exc.value.reason


def test_run_comparative_separates_ran_from_unavailable():
    report = run_comparative(default_adapters(), default_workload(), k=5)
    assert len(report.results) == 4
    ran = report.ran
    unavailable = report.unavailable
    assert [r.name for r in ran] == ["bettermemory"]
    assert {r.name for r in unavailable} == {"mem0", "server-memory", "claude-mem"}
    for r in unavailable:
        assert r.unavailable_reason
        assert r.recall_at_k is None
        assert r.eval_report is None


def test_render_text_has_expected_sections():
    report = run_comparative(default_adapters(), default_workload(), k=5)
    text = render_text(report)
    assert "Capability matrix" in text
    assert "silent_miss_rate" in text
    assert "Not run in this environment" in text
    # The live-agent metrics are surfaced as n/a, not a number.
    assert "n/a" in text


def test_render_json_roundtrips_and_carries_numbers():
    report = run_comparative(default_adapters(), default_workload(), k=5)
    data = json.loads(render_json(report))
    assert data["workload"] == "default-coding-agent"
    assert data["k"] == 5
    assert len(data["results"]) == 4

    by_name = {r["name"]: r for r in data["results"]}
    bm = by_name["bettermemory"]
    assert bm["ran"] is True
    assert bm["recall_at_k"] == pytest.approx(1.0)
    assert bm["eval"]["silent_miss_rate"]["rate"] == pytest.approx(0.5)
    assert bm["eval"]["memory_helped_rate"]["rate"] is None
    assert bm["capabilities"]["can_compute_trio"] is True

    for name in ("mem0", "server-memory", "claude-mem"):
        row = by_name[name]
        assert row["ran"] is False
        assert row["capabilities"]["can_compute_trio"] is False
        assert row["unavailable_reason"]


def test_main_json_flag_emits_machine_readable(capsys):
    """The CLI entry point (main + --json/--k argparse) was entirely
    uncovered. Drive it directly via the argv parameter."""
    from .comparative import main

    rc = main(["--json", "--k", "3"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["k"] == 3
    assert len(data["results"]) == 4


def test_main_default_text_output(capsys):
    """The default (text) render branch of main()."""
    from .comparative import main

    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Capability matrix" in out
    assert "(k=5)" in out


def test_workload_rejects_duplicate_fact_key():
    """The uniqueness guard in Workload._validate was uncovered (the only
    validation test triggers the dangling-gold_key branch instead). Use a
    probe whose gold_key matches the duplicated key so the dup-key check
    fires before the dangling-key check."""
    from .workload import Workload, WorkloadFact, WorkloadProbe

    with pytest.raises(ValueError):
        Workload(
            name="dup",
            facts=[
                WorkloadFact(key="a", scopes=["projects:x"], body="alpha beta"),
                WorkloadFact(key="a", scopes=["projects:y"], body="gamma delta"),
            ],
            probes=[WorkloadProbe("alpha beta", "a", agent_searched=False)],
        )


def test_gold_id_returns_none_for_distractor():
    """gold_id's documented None-for-distractor branch was never asserted —
    every caller only passes gold probes."""
    wl = default_workload()
    distractor = next(p for p in wl.probes if p.gold_key is None)
    assert wl.gold_id(distractor) is None


def test_render_text_omits_lanes_for_ran_result_without_eval_report():
    """render_text's `for r in report.ran` block conditionally formats the
    eval lanes (`if r.eval_report is not None`) and the recall label width at
    k != 5 — both untested because the only runnable adapter always supplies
    a full report at k=5. Exercise the False branch + k!=5 directly so a
    future runnable competitor that computes only recall doesn't hit
    unverified formatting."""
    from .adapters import Capabilities, RunResult
    from .comparative import ComparativeReport, render_text
    from .workload import BASE_NOW

    recall_only = RunResult(
        name="recall-only",
        capabilities=Capabilities(
            logs_retrieval=True, logs_endorsement=False, has_audit_hook=False
        ),
        ran=True,
        k=10,
        gold_total=5,
        recalled=4,
        recall_at_k=0.8,
        eval_report=None,
    )
    report = ComparativeReport(
        workload_name="w", k=10, generated_at=BASE_NOW, results=[recall_only]
    )
    out = render_text(report)  # must not raise
    assert "recall@" in out
    # The eval-lane block is omitted (no eval_report). Scope the negative
    # check to the INDENTED metric line so it doesn't match the capability-
    # matrix header prose, which also contains the substring "silent_miss_rate".
    assert "  silent_miss_rate" not in out
