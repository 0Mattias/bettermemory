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

    # Every probe is audited, but the 3 distractor probes land
    # `no_signal` (nothing relevant stored) and are excluded from the
    # miss-capable denominator since the round-88 `turns_no_signal`
    # split — only the 7 signal-bearing turns count.
    assert ev.turns_audited + ev.turns_no_signal == len(wl.probes) == 10
    assert ev.turns_audited == 7
    assert ev.turns_no_signal == 3
    # Exactly the gold + not-searched probes register as silent misses.
    assert ev.silent_misses == len(wl.expected_miss_probes) == 5

    rate = ev.silent_miss_rate
    assert rate.numerator == 5
    assert rate.denominator == 7
    assert rate.rate == pytest.approx(5 / 7)
    # A real Wilson interval is produced (denominator > 0).
    assert rate.lower is not None and rate.upper is not None
    assert rate.lower < 5 / 7 < rate.upper


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
    assert bm["eval"]["silent_miss_rate"]["rate"] == pytest.approx(5 / 7)
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


# ---------------------------------------------------------------------------
# Agent driver — the machinery that computes the full trio. The scripted agent
# is a deterministic recorded transcript (authored citations) that proves the
# compute path end-to-end; a real measurement needs a real agent's decisions
# (production telemetry — the key-gated LiveAgent role-play was removed, see
# driver.py's module docstring).
# ---------------------------------------------------------------------------


def test_scripted_driver_computes_the_full_trio():
    """The headline: with a driver supplying citation events, ALL THREE rates
    compute (not None) — the gap that left helped/endorsement n/a offline is
    closed. Numbers are deterministic from the authored script."""
    from .driver import default_scripted_agent, run_driver

    wl = default_workload()
    ev = run_driver(wl, default_scripted_agent(wl))

    # The trio is computable — the whole point of the driver.
    assert ev.memory_helped_rate.rate is not None
    assert ev.endorsement_rate.rate is not None
    assert ev.silent_miss_rate.rate is not None

    # Endorsement: exactly one explicit citation, all explicit -> 1.0.
    assert ev.endorsement_rate.numerator == 1
    assert ev.endorsement_rate.denominator == 1
    assert ev.endorsement_rate.rate == pytest.approx(1.0)

    # Silent-miss lane is unchanged by the driver — still 5 misses over
    # the 7 miss-capable turns (the 3 distractor no_signal audits are
    # excluded from the denominator since the round-88 split).
    assert ev.silent_miss_rate.numerator == 5
    assert ev.silent_miss_rate.denominator == 7
    assert ev.silent_miss_rate.rate == pytest.approx(5 / 7)


def test_scripted_driver_helped_rate_is_below_recall():
    """helped_rate must NOT just relabel recall: the script retrieves two gold
    memories but cites only one, so the uncited-but-retrieved memory sits in
    the denominator and the rate is strictly below 1.0."""
    from .driver import default_scripted_agent, run_driver

    wl = default_workload()
    ev = run_driver(wl, default_scripted_agent(wl))
    assert ev.memory_helped_rate.numerator == 1
    assert ev.memory_helped_rate.denominator >= 2
    assert ev.memory_helped_rate.rate is not None
    assert ev.memory_helped_rate.rate < 1.0


def test_scripted_agent_excerpts_are_real_substrings():
    """default_scripted_agent's construction guard enforces that every cited
    excerpt actually appears in the cited memory's body (the honest
    claim-excerpt shape). Building it must not raise."""
    from .driver import default_scripted_agent

    wl = default_workload()
    agent = default_scripted_agent(wl)
    # One probe carries a citation; its excerpt resolved to a real memory.
    turns = [t for t in agent.script.values() if t.citations]
    assert turns and all(c.excerpt for t in turns for c in t.citations)


def test_offline_adapter_still_reports_na_with_driver_present():
    """Adding the driver must NOT change the offline adapter's honest n/a —
    the BetterMemoryAdapter lane stays a measurement-free zero-denominator."""
    from .adapters import BetterMemoryAdapter

    ev = BetterMemoryAdapter().run(default_workload(), k=5).eval_report
    assert ev is not None
    assert ev.memory_helped_rate.rate is None
    assert ev.endorsement_rate.rate is None


def test_cli_driver_scripted_emits_full_trio(capsys):
    """`--driver scripted --json` runs the driver and emits all three rates as
    real numbers (not n/a)."""
    from .comparative import main

    rc = main(["--driver", "scripted", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["memory_helped_rate"]["rate"] is not None
    assert data["endorsement_rate"]["rate"] is not None
    assert data["silent_miss_rate"]["rate"] is not None


def test_run_driver_silent_miss_follows_search_decision_not_hits():
    """End-to-end F1: the silent-miss lane is driven by the agent's search
    DECISION, decoupled from whether hits exist. An agent that never searches
    misses every gold probe (a high-relevance memory existed and it didn't
    look); one that always searches never misses — both see the same hits."""
    from .driver import AgentTurn, ScriptedAgent, run_driver

    wl = default_workload()
    never = ScriptedAgent(script={})  # every probe defaults to searched=False
    assert run_driver(wl, never).silent_miss_rate.numerator == len(wl.gold_probes) == 7

    always = ScriptedAgent(
        script={p.query: AgentTurn(searched=True) for p in wl.probes}
    )
    assert run_driver(wl, always).silent_miss_rate.numerator == 0


def test_run_driver_drops_citation_whose_excerpt_is_not_in_body():
    """F2: the citation honesty guard validates against the cited memory's BODY,
    not the truncated snippet. `snippet_for` truncates bodies >200 chars and
    appends a synthetic '...'; a model echoing that ellipsis would pass a
    snippet check but the phrase is absent from the body. run_driver must drop
    it so the published memory_helped numerator isn't inflated."""
    from bettermemory.models import snippet_for

    from .driver import AgentTurn, Citation, ScriptedAgent, run_driver
    from .workload import Workload, WorkloadFact, WorkloadProbe

    long_body = (
        "Production cutover is fully manual: a release captain must obtain a "
        "signed approval from the on-call lead, then run the promotion script "
        "by hand during the Tuesday window, never through the automated pipeline."
    )
    assert len(long_body) > 200
    snippet = snippet_for(long_body)
    # The synthetic-ellipsis tail is in the snippet but NOT the body.
    ellipsis_tail = snippet[-12:]
    assert ellipsis_tail in snippet and ellipsis_tail not in long_body

    query = "production cutover release captain signed approval"
    wl = Workload(
        name="long-body",
        facts=[WorkloadFact(key="cut", scopes=["projects:x"], body=long_body)],
        probes=[WorkloadProbe(query, "cut", agent_searched=True)],
    )
    mem_id = wl.gold_id(wl.probes[0])
    assert mem_id is not None

    # Cites the snippet ellipsis (absent from the body) -> must be dropped.
    bogus = ScriptedAgent(
        script={
            query: AgentTurn(
                searched=True, citations=(Citation(mem_id, ellipsis_tail),)
            )
        }
    )
    assert run_driver(wl, bogus).memory_helped_rate.numerator == 0

    # Control: a real body substring IS counted.
    real = ScriptedAgent(
        script={
            query: AgentTurn(
                searched=True, citations=(Citation(mem_id, "release captain"),)
            )
        }
    )
    assert run_driver(wl, real).memory_helped_rate.numerator == 1


def test_run_driver_searched_coherence_uses_validated_citations():
    """Cross-layer consistency (the self-audit's Bug B): a citation forces
    `searched`=True only if it SURVIVES the body guard. On a gold+not-searched
    probe, a body-VALID citation upgrades the turn to searched (no miss); a
    body-INVALID citation (dropped by the guard) must NOT flip the abstention —
    it stays a miss, consistent with the citation being excluded from the
    helped numerator. The two layers must never contradict each other."""
    from .driver import AgentTurn, Citation, ScriptedAgent, run_driver

    wl = default_workload()
    probe = wl.probes[0]  # the pytest gold + not-searched probe
    assert probe.gold_key is not None and not probe.agent_searched
    mem_id = wl.gold_id(probe)
    assert mem_id is not None

    # Everything else searched, so any miss isolates to this one probe.
    def agent_with(probe_turn: AgentTurn) -> ScriptedAgent:
        script = {p.query: AgentTurn(searched=True) for p in wl.probes}
        script[probe.query] = probe_turn
        return ScriptedAgent(script=script)

    # Abstained, but a BODY-VALID citation -> searched, no miss, helped +1.
    valid = agent_with(
        AgentTurn(searched=False, citations=(Citation(mem_id, "pytest over unittest"),))
    )
    ev_valid = run_driver(wl, valid)
    assert ev_valid.silent_miss_rate.numerator == 0
    assert ev_valid.memory_helped_rate.numerator == 1

    # Abstained, only a BODY-INVALID citation -> dropped; the abstention stands,
    # so it remains a miss and nothing counts toward helped.
    invalid = agent_with(
        AgentTurn(
            searched=False, citations=(Citation(mem_id, "phrase absent from body"),)
        )
    )
    ev_invalid = run_driver(wl, invalid)
    assert ev_invalid.silent_miss_rate.numerator == 1
    assert ev_invalid.memory_helped_rate.numerator == 0
