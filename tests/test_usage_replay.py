"""Tests for the usage-toggle capture and the `--usage-replay` surface.

Three layers, matching the data's path from ranker to read:

1. `search(usage_toggles_out=...)` — the capture must reproduce, per
   flag, exactly what a real `search()` call without that flag's input
   would have ranked first. The ground-truth tests assert that
   equivalence directly by running the counterfactual search for real.
2. `probe_for_miss` / the event builders — the capture must ride
   `MissReport` onto `turn_audited` / `prompt_recall` events
   additively (absent on default-config turns).
3. `eval.compute_usage_replay` — the offline aggregation: counts,
   the pinned judgment rule, the demotion invariant, the density
   preconditions, and the window filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory.audit import (
    probe_for_miss,
    prompt_recall_fields,
    turn_audited_fields,
)
from bettermemory.eval import (
    USAGE_DEMOTION_INVARIANT_RULE,
    USAGE_IMPROVEMENT_RULE,
    _judge_usage_change,
    compute_usage_replay,
)
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import USAGE_FLAG_NAMES, search

_NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


def _memory(
    body: str,
    *,
    created: datetime,
    corroborations: int = 0,
) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=created,
        updated=created,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
        corroborations=corroborations,
    )


def _near_tie_pair() -> tuple[Memory, Memory]:
    """Two memories with identical bodies: identical leg scores, so the
    (created, id) tiebreaker decides — `newer` wins every leg until a
    usage factor separates them. The cleanest deterministic near-tie."""
    older = _memory(
        "postgres connection pooling uses pgbouncer transaction mode",
        created=_NOW - timedelta(days=10),
    )
    newer = _memory(
        "postgres connection pooling uses pgbouncer transaction mode",
        created=_NOW - timedelta(days=5),
    )
    return older, newer


QUERY = "how does postgres connection pooling work with pgbouncer"


class TestSearchToggleCapture:
    def test_endorsement_flip_captured_with_counterfactual_top1(self) -> None:
        older, newer = _near_tie_pair()
        capture: dict[str, Any] = {}
        hits = search(
            [older, newer],
            QUERY,
            now=_NOW,
            mode="hybrid",
            applied_by_id={older.id: 3},
            usage_toggles_out=capture,
        )
        # The endorsement factor lifts `older` past the tiebreak loss.
        assert hits[0].id == older.id
        assert capture["active"] == ["endorsement_boost"]
        toggle = capture["toggles"]["endorsement_boost"]["top1"]
        assert toggle["id"] == newer.id
        # Raw coverage features ride along for the offline judgment.
        assert toggle["query_unique"] > 0
        assert toggle["relevance_v2"] in ("low", "medium", "high")

    def test_active_without_change_reports_no_toggle(self) -> None:
        """Live signal that doesn't move the top-1 lands in `active`
        with an empty `toggles` — the common case."""
        strong = _memory(
            "postgres connection pooling uses pgbouncer transaction mode",
            created=_NOW - timedelta(days=5),
        )
        weak = _memory("unrelated kubernetes ingress note", created=_NOW)
        capture: dict[str, Any] = {}
        hits = search(
            [strong, weak],
            QUERY,
            now=_NOW,
            mode="hybrid",
            applied_by_id={strong.id: 3},
            usage_toggles_out=capture,
        )
        assert hits[0].id == strong.id
        assert capture["active"] == ["endorsement_boost"]
        assert capture["toggles"] == {}

    def test_no_usage_inputs_leaves_capture_empty(self) -> None:
        older, newer = _near_tie_pair()
        capture: dict[str, Any] = {}
        search([older, newer], QUERY, now=_NOW, usage_toggles_out=capture)
        assert capture == {}

    def test_out_param_does_not_change_results(self) -> None:
        older, newer = _near_tie_pair()
        with_capture = search(
            [older, newer],
            QUERY,
            now=_NOW,
            applied_by_id={older.id: 3},
            usage_toggles_out={},
        )
        without_capture = search(
            [older, newer],
            QUERY,
            now=_NOW,
            applied_by_id={older.id: 3},
        )
        assert [h.id for h in with_capture] == [h.id for h in without_capture]
        assert [h.score for h in with_capture] == [h.score for h in without_capture]

    def test_counterfactual_matches_real_off_search(self) -> None:
        """The load-bearing equivalence: for every flag with live
        signal, the captured (or implied-unchanged) counterfactual
        top-1 must equal what `search()` actually returns with that
        one flag's input removed. Run over a mixed fixture — an
        endorsed near-tie loser, a demoted near-tie winner, and a
        corroborated near-tie loser — in both hybrid and keyword
        modes, so all three factors and both fusion paths are
        exercised against ground truth."""
        older, newer = _near_tie_pair()
        demoted_pair_a = _memory(
            "redis cluster failover waits for quorum election",
            created=_NOW - timedelta(days=3),
        )
        demoted_pair_b = _memory(
            "redis cluster failover waits for quorum election",
            created=_NOW - timedelta(days=8),
        )
        corroborated = _memory(
            "nginx reload stays graceful without dropping live sessions",
            created=_NOW - timedelta(days=9, hours=1),
        )
        corroborated = corroborated.model_copy(update={"corroborations": 2})
        corroborated_rival = _memory(
            "nginx reload stays graceful without dropping live sessions",
            created=_NOW - timedelta(days=9),
        )
        pool = [
            older,
            newer,
            demoted_pair_a,
            demoted_pair_b,
            corroborated,
            corroborated_rival,
        ]
        # Each query hits exactly one designed near-tie pair, so exactly
        # one flag has live signal per query — "active" means a
        # non-neutral factor on a SCORED candidate, not a non-empty map.
        cases = [
            (
                "how does postgres connection pooling work with pgbouncer",
                {"endorsement_boost"},
            ),
            ("redis cluster failover quorum", {"outcome_demotion"}),
            ("is nginx reload graceful for live sessions", {"corroboration_boost"}),
        ]
        applied = {older.id: 3}
        # `demoted_pair_a` is the newer (winning) twin; demotion should
        # hand the slot to its older twin until toggled off.
        negatives = {demoted_pair_a.id: (2, 1)}
        for query, expected_active in cases:
            for mode in ("hybrid", "keyword"):
                capture: dict[str, Any] = {}
                on_hits = search(
                    pool,
                    query,
                    now=_NOW,
                    mode=mode,  # type: ignore[arg-type]
                    applied_by_id=applied,
                    negative_by_id=negatives,
                    corroboration_boost=True,
                    usage_toggles_out=capture,
                )
                assert on_hits, query
                assert set(capture["active"]) == expected_active, (query, mode)
                for flag in USAGE_FLAG_NAMES:
                    off_hits = search(
                        pool,
                        query,
                        now=_NOW,
                        mode=mode,  # type: ignore[arg-type]
                        applied_by_id=(
                            None if flag == "endorsement_boost" else applied
                        ),
                        negative_by_id=(
                            None if flag == "outcome_demotion" else negatives
                        ),
                        corroboration_boost=(flag != "corroboration_boost"),
                    )
                    toggle = capture["toggles"].get(flag)
                    expected = (
                        toggle["top1"]["id"]
                        if toggle is not None
                        else on_hits[0].id
                    )
                    assert off_hits[0].id == expected, (query, mode, flag)

    def test_counterfactual_flips_are_exercised(self) -> None:
        """Non-vacuity companion to the equivalence test: each factor's
        designed near-tie actually flips on its own query, so the
        ground-truth assertions above compare CHANGED outcomes, not
        just unchanged ones."""
        older, newer = _near_tie_pair()
        capture: dict[str, Any] = {}
        search(
            [older, newer],
            QUERY,
            now=_NOW,
            applied_by_id={older.id: 3},
            usage_toggles_out=capture,
        )
        assert "endorsement_boost" in capture["toggles"]

        pair_a = _memory("redis cluster failover", created=_NOW - timedelta(days=3))
        pair_b = _memory("redis cluster failover", created=_NOW - timedelta(days=8))
        capture = {}
        search(
            [pair_a, pair_b],
            "redis cluster failover",
            now=_NOW,
            negative_by_id={pair_a.id: (2, 1)},
            usage_toggles_out=capture,
        )
        assert "outcome_demotion" in capture["toggles"]


class TestProbeAndEventBuilders:
    def test_probe_report_and_builders_carry_capture(self) -> None:
        older, newer = _near_tie_pair()
        report = probe_for_miss(
            [older, newer],
            QUERY,
            recent_events=[],
            session_id="sess-test",
            now=_NOW,
            applied_by_id={older.id: 3},
        )
        assert report.usage_active == ("endorsement_boost",)
        assert report.usage_toggles is not None
        assert report.usage_toggles["endorsement_boost"]["top1"]["id"] == newer.id
        assert report.to_dict()["usage_active"] == ["endorsement_boost"]

        audited = turn_audited_fields(
            report,
            session_id="sess-test",
            probe_mode="hybrid",
            assistant_present=True,
            triggered_from="stop_hook",
        )
        assert audited["usage_active"] == ["endorsement_boost"]
        assert audited["usage_toggles"] == report.usage_toggles

        recall = prompt_recall_fields(
            report,
            session_id="sess-test",
            probe_mode="hybrid",
            injected_chars=100,
        )
        assert recall["usage_active"] == ["endorsement_boost"]
        assert recall["usage_toggles"] == report.usage_toggles

    def test_default_config_probe_emits_no_usage_fields(self) -> None:
        """A probe with no usage inputs — every default-config store —
        must keep both events' exact prior shape."""
        older, newer = _near_tie_pair()
        report = probe_for_miss(
            [older, newer],
            QUERY,
            recent_events=[],
            session_id="sess-test",
            now=_NOW,
        )
        assert report.usage_active == ()
        assert report.usage_toggles is None
        audited = turn_audited_fields(
            report,
            session_id="sess-test",
            probe_mode="hybrid",
            assistant_present=True,
            triggered_from="stop_hook",
        )
        assert "usage_active" not in audited
        assert "usage_toggles" not in audited
        recall = prompt_recall_fields(
            report,
            session_id="sess-test",
            probe_mode="hybrid",
            injected_chars=0,
        )
        assert "usage_active" not in recall
        assert "usage_toggles" not in recall


def _ev(
    kind: str,
    ts: datetime,
    **fields: Any,
) -> dict[str, Any]:
    """Hand-built event dict — controlled timestamps are the point here
    (the invariant and window tests need exact spacing the Recorder's
    now-stamping can't produce). The producer-side field shape is pinned
    by `TestProbeAndEventBuilders` against the real builders, so these
    literals can't silently drift from production."""
    base: dict[str, Any] = {"ts": ts.isoformat(), "kind": kind, "session": "s"}
    base.update(fields)
    return base


def _top1(mid: str, v2: str = "high", matched: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "id": mid,
            "score": 0.032,
            "relevance": v2,
            "relevance_v2": v2,
            "matched_unique": matched,
            "query_unique": 4,
        }
    ]


def _toggle(mid: str, v2: str, matched: int) -> dict[str, Any]:
    return {
        "top1": {
            "id": mid,
            "score": 0.031,
            "matched_unique": matched,
            "query_unique": 4,
            "relevance_v2": v2,
        }
    }


class TestJudgeUsageChange:
    def test_tier_dominates_then_matched_then_neutral(self) -> None:
        assert _judge_usage_change("high", 1, "medium", 3) == "improving"
        assert _judge_usage_change("low", 3, "medium", 1) == "worsening"
        assert _judge_usage_change("high", 3, "high", 2) == "improving"
        assert _judge_usage_change("high", 2, "high", 3) == "worsening"
        assert _judge_usage_change("medium", 2, "medium", 2) == "neutral"


class TestComputeUsageReplay:
    def test_counts_judgments_and_densities(self) -> None:
        t0 = _NOW - timedelta(hours=3)
        events = [
            # Enriched, changed, improving (flag's pick is the higher tier).
            _ev(
                "turn_audited",
                t0,
                verdict="ok",
                top_hits=_top1("M1", "high", 3),
                usage_active=["endorsement_boost"],
                usage_toggles={"endorsement_boost": _toggle("M2", "medium", 2)},
            ),
            # Enriched delivery, changed, worsening — and a delivery is
            # miss-labeled by definition.
            _ev(
                "prompt_recall",
                t0 + timedelta(minutes=10),
                top_hits=_top1("M3", "low", 1),
                usage_active=["endorsement_boost"],
                usage_toggles={"endorsement_boost": _toggle("M4", "high", 3)},
            ),
            # Enriched, live signal, no change.
            _ev(
                "turn_audited",
                t0 + timedelta(minutes=20),
                verdict="ok",
                top_hits=_top1("M1"),
                usage_active=["endorsement_boost", "outcome_demotion"],
            ),
            # Pre-capture producer (or no live signal) — counted, not judged.
            _ev(
                "turn_audited",
                t0 + timedelta(minutes=30),
                verdict="miss",
                top_hits=_top1("M5"),
            ),
            # Repeat — excluded everywhere.
            _ev(
                "turn_audited",
                t0 + timedelta(minutes=40),
                verdict="ok",
                repeat=True,
                top_hits=_top1("M1"),
                usage_active=["endorsement_boost"],
            ),
            # no_signal — not replayable.
            _ev("turn_audited", t0 + timedelta(minutes=50), verdict="no_signal"),
            # Density feeders.
            _ev("use", t0, outcome="applied", auto=False, ids=["Ma", "Mb"]),
            _ev("use", t0, outcome="applied", auto=True, ids=["Mauto"]),
            _ev("use", t0, outcome="ignored", ids=["Mc"]),
            _ev("use", t0, outcome="contradicted", ids=["Md"]),
        ]
        corroborated = _memory("corroborated fact", created=_NOW).model_copy(
            update={"corroborations": 2}
        )
        plain = _memory("plain fact", created=_NOW)
        report = compute_usage_replay(
            events,
            memories=[corroborated, plain],
            since=timedelta(days=1),
            now=_NOW,
        )
        assert report.replayable_turns == 4
        assert report.turn_audited_turns == 3
        assert report.prompt_recall_turns == 1
        assert report.repeat_audits_skipped == 1
        assert report.turns_without_capture == 1
        assert report.first_capture_ts == t0.isoformat()
        assert report.endorsed_distinct_in_window == 2
        assert report.negative_distinct_in_window == 2
        assert report.corroborated_memories == 1
        assert report.corroborated_twice_memories == 1
        assert report.improvement_rule == USAGE_IMPROVEMENT_RULE

        by_flag = {f.flag: f for f in report.flags}
        endorsement = by_flag["endorsement_boost"]
        assert endorsement.active_turns == 3
        assert endorsement.changed_turns == 2
        assert endorsement.improving == 1
        assert endorsement.worsening == 1
        assert endorsement.neutral == 0
        assert endorsement.miss_labeled_worsening == 1
        demotion = by_flag["outcome_demotion"]
        assert demotion.active_turns == 1
        assert demotion.changed_turns == 0
        assert demotion.invariant_rule == USAGE_DEMOTION_INVARIANT_RULE
        assert demotion.invariant_violations == []

    def test_demotion_invariant_violation_and_control(self) -> None:
        t0 = _NOW - timedelta(hours=2)
        later = t0 + timedelta(minutes=30)
        base = [
            # The demotion suppressed MX out of the top slot here.
            _ev(
                "turn_audited",
                t0,
                verdict="ok",
                top_hits=_top1("MY"),
                usage_active=["outcome_demotion"],
                usage_toggles={"outcome_demotion": _toggle("MX", "high", 3)},
            ),
            # Later, MX IS the production top-1.
            _ev(
                "turn_audited",
                later,
                verdict="ok",
                top_hits=_top1("MX"),
                usage_active=["outcome_demotion"],
            ),
        ]
        violation_events = base + [
            _ev(
                "use",
                later + timedelta(seconds=60),
                outcome="applied",
                auto=False,
                ids=["MX"],
            ),
        ]
        report = compute_usage_replay(violation_events, since=None, now=_NOW)
        demotion = {f.flag: f for f in report.flags}["outcome_demotion"]
        assert len(demotion.invariant_violations) == 1
        assert demotion.invariant_violations[0]["memory_id"] == "MX"

        # Control: the explicit apply lands outside the attribution
        # horizon of the later turn — no violation.
        control_events = base + [
            _ev(
                "use",
                later + timedelta(seconds=700),
                outcome="applied",
                auto=False,
                ids=["MX"],
            ),
        ]
        report = compute_usage_replay(control_events, since=None, now=_NOW)
        demotion = {f.flag: f for f in report.flags}["outcome_demotion"]
        assert demotion.invariant_violations == []

    def test_window_filter_excludes_aged_events(self) -> None:
        aged = _NOW - timedelta(days=40)
        events = [
            _ev(
                "turn_audited",
                aged,
                verdict="ok",
                top_hits=_top1("M1"),
                usage_active=["endorsement_boost"],
                usage_toggles={"endorsement_boost": _toggle("M2", "high", 3)},
            ),
            _ev("use", aged, outcome="applied", auto=False, ids=["Ma"]),
        ]
        report = compute_usage_replay(
            events, since=timedelta(days=30), now=_NOW
        )
        assert report.replayable_turns == 0
        assert report.endorsed_distinct_in_window == 0
        assert {f.flag: f for f in report.flags}[
            "endorsement_boost"
        ].changed_turns == 0
