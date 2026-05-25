"""Tests for the `bettermemory eval` module + CLI subcommand.

Covers parse_since, compute_eval on each numerator/denominator path,
scope filtering, since-window filtering, Wilson intervals at the
endpoints, the silent-miss buffer cap, the endorsement-debt
exclusions (ambient, tombstoned, has-explicit-applied), and an
end-to-end CLI smoke through ``main()``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.eval import (
    DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
    THRESHOLD_RULES,
    TOOLS_WITHOUT_TELEMETRY,
    RateCI,
    _KNOWN_SIDE_EFFECT_KINDS,
    _TOOL_EVENT_KIND_TO_TOOL,
    _wilson_interval,
    compute_eval,
    compute_threshold_sweep,
    compute_tool_usage,
    parse_since,
    render_text,
    render_threshold_sweep_text,
    render_tool_usage_text,
)
from bettermemory.events import Recorder
from bettermemory.models import (
    Category,
    Confidence,
    Memory,
    Source,
    generate_ulid,
)
from bettermemory.store import Store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mem(
    *,
    body: str = "memory body",
    scopes: list[str] | None = None,
    category: Category | None = None,
    created: datetime | None = None,
) -> Memory:
    """Cheap Memory factory for compute_eval tests — no on-disk store needed."""
    now = created or datetime(2026, 5, 1, tzinfo=timezone.utc)
    return Memory(
        id=generate_ulid(),
        scopes=scopes or ["tools"],
        confidence=Confidence.HIGH,
        source=Source.EXPLICIT,
        body=body,
        created=now,
        updated=now,
        category=category,
    )


def _ev(
    kind: str, ts: datetime | str = "2026-05-15T12:00:00.000+00:00", **fields: Any
) -> dict[str, Any]:
    """Hand-built event dict for tests that don't need disk round-trip.

    Prefer the ``event_log`` pytest fixture (see ``tests/_event_helpers.py``)
    for new tests — it routes through the real ``Recorder`` and so the
    fixture's shape can never drift from production's. This helper
    remains for legacy tests; the ``session`` default is omitted when
    callers pass either ``session=`` or ``session_id=`` so the two
    fields never disagree in a way that's impossible in production.
    """
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    base: dict[str, Any] = {"ts": ts, "kind": kind}
    # Only default `session` when the caller didn't provide a session
    # field. Pre-2.6.4 the helper hardcoded `session: "sess-test"`
    # regardless of any `session_id=` the caller passed — the resulting
    # event had both fields disagreeing, which can't happen in
    # production (both are derived from the same SessionState).
    if "session" not in fields and "session_id" not in fields:
        base["session"] = "sess-test"
    base.update(fields)
    return base


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_days(self) -> None:
        assert parse_since("30d") == timedelta(days=30)

    def test_hours(self) -> None:
        assert parse_since("12h") == timedelta(hours=12)

    def test_minutes(self) -> None:
        assert parse_since("90m") == timedelta(minutes=90)

    def test_seconds(self) -> None:
        assert parse_since("45s") == timedelta(seconds=45)

    def test_all(self) -> None:
        assert parse_since("all") is None

    def test_none(self) -> None:
        assert parse_since(None) is None

    def test_empty(self) -> None:
        assert parse_since("") is None

    def test_whitespace_tolerant(self) -> None:
        assert parse_since("  30d  ") == timedelta(days=30)

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="--since"):
            parse_since("garbage")

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            parse_since("0d")

    def test_rejects_negative(self) -> None:
        # Negative is rejected by the regex (no leading sign accepted).
        with pytest.raises(ValueError, match="--since"):
            parse_since("-1d")

    def test_rejects_unknown_unit(self) -> None:
        with pytest.raises(ValueError, match="--since"):
            parse_since("30w")


# ---------------------------------------------------------------------------
# Wilson interval
# ---------------------------------------------------------------------------


class TestWilsonInterval:
    def test_returns_subset_of_unit_interval(self) -> None:
        lo, hi = _wilson_interval(5, 10)
        assert 0.0 <= lo <= 0.5 <= hi <= 1.0

    def test_zero_numerator(self) -> None:
        lo, hi = _wilson_interval(0, 100)
        assert lo == 0.0
        assert 0.0 < hi < 0.1  # tight upper bound; no observed positives

    def test_full_numerator(self) -> None:
        lo, hi = _wilson_interval(100, 100)
        # FP rounding can leave hi at 0.999…9 rather than exactly 1.0;
        # the clamp guarantees hi <= 1.0, ``approx`` covers the gap.
        assert hi == pytest.approx(1.0)
        assert 0.9 < lo < 1.0  # tight lower bound; saturated positives

    def test_zero_denominator_returns_full_interval(self) -> None:
        lo, hi = _wilson_interval(0, 0)
        assert (lo, hi) == (0.0, 1.0)

    def test_negative_denominator_returns_full_interval(self) -> None:
        # Guarded to avoid math errors on caller bugs.
        lo, hi = _wilson_interval(0, -5)
        assert (lo, hi) == (0.0, 1.0)

    def test_interval_narrows_with_n(self) -> None:
        _, hi_small = _wilson_interval(1, 5)
        _, hi_large = _wilson_interval(20, 100)
        assert hi_small > hi_large

    def test_wilson_interval_matches_known_reference(self) -> None:
        # Numerical-gold reference values at z=1.96. The other structural
        # tests in this class only check lo<=rate<=hi / clamps at the
        # endpoints — they'd pass even if the formula were swapped for
        # naive Wald, which clamps the same way. This pinning catches
        # an actual formula regression.
        #
        # Reference values computed via the Wilson formula
        # `(p̂ + z²/(2n) ± z·√((p̂(1-p̂) + z²/(4n))/n)) / (1 + z²/n)`
        # at z=1.96, matching scipy.stats.binomtest(k, n).proportion_ci(
        # method='wilson') to four decimal places.
        lo, hi = _wilson_interval(50, 100)
        assert lo == pytest.approx(0.4038, abs=5e-4)
        assert hi == pytest.approx(0.5962, abs=5e-4)
        lo, hi = _wilson_interval(1, 10)
        assert lo == pytest.approx(0.0179, abs=5e-4)
        assert hi == pytest.approx(0.4042, abs=5e-4)


# ---------------------------------------------------------------------------
# RateCI
# ---------------------------------------------------------------------------


class TestRateCI:
    def test_zero_denominator_yields_none_rate(self) -> None:
        rate = RateCI.from_counts(0, 0)
        assert rate.rate is None
        assert rate.lower is None
        assert rate.upper is None
        assert rate.numerator == 0
        assert rate.denominator == 0

    def test_typical_split(self) -> None:
        rate = RateCI.from_counts(3, 10)
        assert rate.rate == pytest.approx(0.3)
        assert rate.lower is not None and rate.lower < 0.3
        assert rate.upper is not None and rate.upper > 0.3
        assert rate.lower >= 0.0 and rate.upper <= 1.0

    def test_to_dict_carries_all_fields(self) -> None:
        d = RateCI.from_counts(2, 5).to_dict()
        assert d.keys() == {
            "numerator",
            "denominator",
            "rate",
            "ci95_lower",
            "ci95_upper",
            "torn_read",
        }

    def test_rate_ci_marks_torn_read_when_numerator_exceeds_denominator(self) -> None:
        # `k > n` is a torn-read scenario: the event log was read mid
        # rotation and ordering anomalies leaked through. The Wilson
        # helper clamps so the interval stays well-defined, but the
        # audit consumer needs the flag so CI consumers can branch on
        # "your numbers may be wrong" rather than silently trusting 1.0.
        torn = RateCI.from_counts(5, 3)
        assert torn.torn_read is True
        assert torn.rate == 1.0
        normal = RateCI.from_counts(3, 5)
        assert normal.torn_read is False


# ---------------------------------------------------------------------------
# compute_eval — empty + single-path cases
# ---------------------------------------------------------------------------


class TestComputeEvalEmpty:
    def test_zero_input(self) -> None:
        report = compute_eval(memories=[], events=[])
        assert report.total_events_scanned == 0
        assert report.memory_helped_rate.rate is None
        assert report.endorsement_rate.rate is None
        assert report.silent_miss_rate.rate is None
        assert report.endorsement_debt_rows == []
        assert report.silent_miss_recent == []

    def test_to_dict_round_trip(self) -> None:
        report = compute_eval(memories=[], events=[])
        d = report.to_dict()
        assert "memory_helped_rate" in d
        assert d["counts"]["retrieval_occurrences"] == 0
        # Stable JSON shape — every dataclass field maps to a key.
        assert json.dumps(d)  # must not raise


class TestRetrievalCounting:
    def test_search_returned_counts_each_id(self) -> None:
        mem = _mem()
        events = [_ev("search", returned=[mem.id, "other-id"])]
        report = compute_eval(memories=[mem], events=events)
        # Only the live memory counts under scope-less default — the
        # ``other-id`` falls into ``by_id`` lookups, but compute_eval
        # only filters by scope; with scope=None it admits all ids.
        assert report.retrieval_occurrences == 2

    def test_show_counts_one(self) -> None:
        mem = _mem()
        events = [_ev("show", id=mem.id)]
        report = compute_eval(memories=[mem], events=events)
        assert report.retrieval_occurrences == 1

    def test_malformed_search_returned_ignored(self) -> None:
        events = [_ev("search", returned="not-a-list")]
        report = compute_eval(memories=[], events=events)
        assert report.retrieval_occurrences == 0


class TestUseEvents:
    def test_explicit_applied_with_excerpt_increments_numerator(self) -> None:
        mem = _mem()
        events = [
            _ev("search", returned=[mem.id]),
            _ev(
                "use",
                ids=[mem.id],
                outcome="applied",
                claim_excerpts=["The auth middleware lives in src/auth/middleware.py."],
            ),
        ]
        report = compute_eval(memories=[mem], events=events)
        assert report.retrieval_occurrences == 1
        assert report.applied_total == 1
        assert report.applied_explicit == 1
        assert report.explicit_endorsements_with_excerpt == 1
        assert report.memory_helped_rate.rate == pytest.approx(1.0)
        assert report.endorsement_rate.rate == pytest.approx(1.0)

    def test_auto_applied_excluded_from_explicit_counters(self) -> None:
        mem = _mem()
        events = [
            _ev("search", returned=[mem.id]),
            _ev("use", ids=[mem.id], outcome="applied", auto=True),
        ]
        report = compute_eval(memories=[mem], events=events)
        assert report.applied_total == 1
        assert report.applied_explicit == 0
        assert report.explicit_endorsements_with_excerpt == 0
        assert report.endorsement_rate.rate == pytest.approx(0.0)
        assert report.memory_helped_rate.rate == pytest.approx(0.0)

    def test_endorsement_rate_split(self) -> None:
        mem = _mem()
        events = [
            _ev("use", ids=[mem.id], outcome="applied", auto=True),
            _ev("use", ids=[mem.id], outcome="applied", auto=True),
            _ev(
                "use",
                ids=[mem.id],
                outcome="applied",
                claim_excerpts=["load-bearing claim"],
            ),
        ]
        report = compute_eval(memories=[mem], events=events)
        # 1 explicit / 3 applied = 0.333…
        assert report.applied_total == 3
        assert report.applied_explicit == 1
        assert report.endorsement_rate.rate == pytest.approx(1 / 3)

    def test_explicit_applied_without_excerpt_does_not_help_rate(self) -> None:
        mem = _mem()
        events = [
            _ev("search", returned=[mem.id]),
            _ev("use", ids=[mem.id], outcome="applied"),  # no claim_excerpts
        ]
        report = compute_eval(memories=[mem], events=events)
        assert report.applied_explicit == 1
        assert report.explicit_endorsements_with_excerpt == 0
        # endorsement_rate counts explicit / applied regardless of excerpt
        assert report.endorsement_rate.rate == pytest.approx(1.0)
        # memory_helped_rate requires the excerpt — stays at 0
        assert report.memory_helped_rate.rate == pytest.approx(0.0)

    def test_excerpt_none_in_list_does_not_help(self) -> None:
        mem = _mem()
        events = [
            _ev("search", returned=[mem.id]),
            _ev(
                "use",
                ids=[mem.id],
                outcome="applied",
                claim_excerpts=[None],
            ),
        ]
        report = compute_eval(memories=[mem], events=events)
        assert report.explicit_endorsements_with_excerpt == 0

    def test_non_applied_outcomes_skipped(self) -> None:
        mem = _mem()
        events = [
            _ev("use", ids=[mem.id], outcome="ignored"),
            _ev("use", ids=[mem.id], outcome="contradicted"),
            _ev("use", ids=[mem.id], outcome="corrected"),
        ]
        report = compute_eval(memories=[mem], events=events)
        assert report.applied_total == 0
        assert report.applied_explicit == 0

    def test_compute_eval_dedupes_repeated_memory_ids_within_use_event(self) -> None:
        # A model that sends `record_use(memory_ids=["A", "A", "B"], ...)`
        # must not inflate `applied_total` to 3. eval.py is the citable
        # reference for the published metric, so the dedup happens here.
        mem_a = _mem(body="memory A")
        mem_b = _mem(body="memory B")
        events = [
            _ev(
                "use",
                ids=[mem_a.id, mem_a.id, mem_b.id],
                outcome="applied",
                claim_excerpts=["foo", "bar", "baz"],
            ),
        ]
        report = compute_eval(memories=[mem_a, mem_b], events=events)
        # A counted once + B counted once = 2, NOT 3.
        assert report.applied_total == 2
        assert report.applied_explicit == 2
        # Each unique id surfaced a non-empty excerpt, so both count.
        assert report.explicit_endorsements_with_excerpt == 2


# ---------------------------------------------------------------------------
# silent-miss rate
# ---------------------------------------------------------------------------


class TestSilentMissRate:
    def test_search_miss_increments_numerator(self) -> None:
        events = [
            _ev("turn_audited", verdict="ok", threshold_rule="v1_top1_high"),
            _ev("turn_audited", verdict="miss", threshold_rule="v1_top1_high"),
            _ev(
                "search_miss",
                session_id="sess-A",
                threshold_rule="v1_top1_high",
                top_hits=[{"id": "mem-A", "relevance": "high"}],
            ),
        ]
        report = compute_eval(memories=[], events=events)
        assert report.turns_audited == 2
        assert report.silent_misses == 1
        assert report.silent_miss_rate.rate == pytest.approx(0.5)
        assert report.threshold_rule == "v1_top1_high"
        assert len(report.silent_miss_recent) == 1
        cand = report.silent_miss_recent[0]
        assert cand.top_missed_id == "mem-A"
        assert cand.top_missed_relevance == "high"

    def test_silent_miss_buffer_truncates(self) -> None:
        # 20 audited turns, 15 of which were misses — well above the
        # default limit. Counts must still be exact; the buffer is
        # just for the renderer.
        events = [_ev("turn_audited", verdict="ok") for _ in range(20)]
        events += [
            _ev(
                "search_miss",
                ts=f"2026-05-15T12:00:{i:02d}.000+00:00",
                session_id=f"sess-{i}",
                top_hits=[{"id": f"mem-{i}", "relevance": "high"}],
            )
            for i in range(15)
        ]
        report = compute_eval(memories=[], events=events, silent_miss_limit=5)
        assert report.silent_misses == 15
        assert len(report.silent_miss_recent) == 5
        # Tail of the chronological stream.
        assert report.silent_miss_recent[-1].top_missed_id == "mem-14"

    def test_unparseable_miss_event_dropped_from_buffer_but_counted(self) -> None:
        # Event has no session_id AND no session field — the
        # candidate factory can't attribute it to a session, so it
        # falls out of the renderer-facing list. The numerator still
        # ticks because the event WAS recorded.
        events: list[dict[str, Any]] = [
            {"ts": "2026-05-15T12:00:00.000+00:00", "kind": "search_miss"}
        ]
        report = compute_eval(memories=[], events=events)
        assert report.silent_misses == 1
        assert report.silent_miss_recent == []

    def test_legacy_hook_top_hit_ids_shape_still_renders(self) -> None:
        """Regression for the 2.6.4 silent-miss fix.

        Pre-2.6.4 the Stop hook emitted `top_hit_ids=[strings]`
        instead of `top_hits=[dicts]`. Eval read canonical-only, so
        the renderer's `top_missed_id` came back None for every
        hook-originated miss — and the hook is the *primary*
        production source. Archived events on disk still carry the
        legacy shape, so the eval reader must tolerate both with the
        same discipline 70e41a4 established for llm.py.

        The synthesized fallback can't recover relevance (the legacy
        shape didn't store it), so the renderer shows None there —
        but the id surfaces, which is what triage actually uses.
        """
        events: list[dict[str, Any]] = [
            _ev(
                "search_miss",
                session_id="sess-legacy",
                top_hit_ids=["mem-legacy"],
                triggered_from="stop_hook",
            ),
        ]
        report = compute_eval(memories=[], events=events)
        assert report.silent_misses == 1
        cand = report.silent_miss_recent[0]
        assert cand.top_missed_id == "mem-legacy"
        # Relevance not recoverable from the legacy shape.
        assert cand.top_missed_relevance is None


# ---------------------------------------------------------------------------
# endorsement-debt rows
# ---------------------------------------------------------------------------


class TestEndorsementDebt:
    def _make_events(
        self, mem_id: str, retrievals: int, *, with_explicit: bool = False
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for _ in range(retrievals):
            events.append(_ev("search", returned=[mem_id]))
            events.append(_ev("use", ids=[mem_id], outcome="applied", auto=True))
        if with_explicit:
            events.append(
                _ev(
                    "use",
                    ids=[mem_id],
                    outcome="applied",
                    claim_excerpts=["load-bearing"],
                )
            )
        return events

    def test_row_included_when_floor_reached_and_no_explicit(self) -> None:
        mem = _mem()
        report = compute_eval(
            memories=[mem],
            events=self._make_events(mem.id, retrievals=5),
            endorsement_min_retrievals=5,
        )
        assert report.endorsement_debt_total == 1
        row = report.endorsement_debt_rows[0]
        assert row.id == mem.id
        assert row.retrieval_count == 5
        assert row.auto_applied_count == 5
        assert row.explicit_applied_count == 0

    def test_row_excluded_below_floor(self) -> None:
        mem = _mem()
        report = compute_eval(
            memories=[mem],
            events=self._make_events(mem.id, retrievals=4),
            endorsement_min_retrievals=5,
        )
        assert report.endorsement_debt_total == 0

    def test_row_excluded_when_explicit_applied_exists(self) -> None:
        mem = _mem()
        report = compute_eval(
            memories=[mem],
            events=self._make_events(mem.id, retrievals=5, with_explicit=True),
            endorsement_min_retrievals=5,
        )
        assert report.endorsement_debt_total == 0

    def test_ambient_memory_excluded(self) -> None:
        mem = _mem(category=Category.AMBIENT)
        report = compute_eval(
            memories=[mem],
            events=self._make_events(mem.id, retrievals=10),
            endorsement_min_retrievals=5,
        )
        assert report.endorsement_debt_total == 0

    def test_tombstoned_memory_excluded(self) -> None:
        # mem isn't in the memories list → can't be attributed to a row.
        ghost_id = generate_ulid()
        report = compute_eval(
            memories=[],
            events=self._make_events(ghost_id, retrievals=10),
            endorsement_min_retrievals=5,
        )
        assert report.endorsement_debt_total == 0

    def test_rows_sorted_by_retrieval_count_desc(self) -> None:
        m1 = _mem(body="m1")
        m2 = _mem(body="m2")
        events = self._make_events(m1.id, retrievals=6) + self._make_events(
            m2.id, retrievals=8
        )
        report = compute_eval(
            memories=[m1, m2],
            events=events,
            endorsement_min_retrievals=5,
        )
        assert [r.id for r in report.endorsement_debt_rows] == [m2.id, m1.id]


# ---------------------------------------------------------------------------
# scope filter + since window
# ---------------------------------------------------------------------------


class TestScopeFilter:
    def test_filter_admits_matching_scope(self) -> None:
        in_scope = _mem(scopes=["tools"])
        out_scope = _mem(scopes=["learning-style"])
        events = [
            _ev("search", returned=[in_scope.id, out_scope.id]),
            _ev("use", ids=[in_scope.id], outcome="applied", auto=True),
            _ev("use", ids=[out_scope.id], outcome="applied", auto=True),
        ]
        report = compute_eval(
            memories=[in_scope, out_scope],
            events=events,
            scope="tools",
        )
        # Only the in-scope memory's events are counted.
        assert report.retrieval_occurrences == 1
        assert report.applied_total == 1

    def test_silent_miss_unaffected_by_scope_filter(self) -> None:
        # turn_audited / search_miss are per-turn, not per-memory.
        events = [
            _ev("turn_audited", verdict="miss"),
            _ev("search_miss", session_id="sess-A"),
        ]
        report = compute_eval(memories=[], events=events, scope="tools")
        assert report.turns_audited == 1
        assert report.silent_misses == 1


class TestSinceWindow:
    def test_drops_events_before_cutoff(self) -> None:
        mem = _mem()
        now = datetime(2026, 5, 20, 12, tzinfo=timezone.utc)
        recent_ts = (now - timedelta(hours=1)).isoformat()
        old_ts = (now - timedelta(days=10)).isoformat()
        events = [
            _ev("search", ts=old_ts, returned=[mem.id]),
            _ev("search", ts=recent_ts, returned=[mem.id]),
        ]
        report = compute_eval(
            memories=[mem],
            events=events,
            now=now,
            since=timedelta(days=1),
        )
        # Only the recent event should land in retrieval_occurrences,
        # but total_events_scanned counts everything iter'd.
        assert report.total_events_scanned == 2
        assert report.retrieval_occurrences == 1

    def test_no_window_admits_all(self) -> None:
        mem = _mem()
        events = [
            _ev("search", ts="2000-01-01T00:00:00.000+00:00", returned=[mem.id]),
            _ev("search", ts="2026-05-20T00:00:00.000+00:00", returned=[mem.id]),
        ]
        report = compute_eval(memories=[mem], events=events, since=None)
        assert report.retrieval_occurrences == 2


# ---------------------------------------------------------------------------
# render_text + CLI smoke
# ---------------------------------------------------------------------------


class TestRenderText:
    def test_empty_renders_na(self) -> None:
        report = compute_eval(memories=[], events=[])
        text = render_text(report)
        assert "n/a" in text
        assert "bettermemory eval" in text

    def test_populated_renders_rates(self) -> None:
        mem = _mem()
        events = [
            _ev("search", returned=[mem.id]),
            _ev(
                "use",
                ids=[mem.id],
                outcome="applied",
                claim_excerpts=["the load-bearing sentence"],
            ),
        ]
        report = compute_eval(memories=[mem], events=events)
        text = render_text(report)
        assert "memory_helped_rate" in text
        assert "endorsement_rate" in text
        assert "1.00" in text  # rate at saturation

    def test_endorsement_debt_section(self) -> None:
        mem = _mem()
        events = []
        for _ in range(5):
            events.append(_ev("search", returned=[mem.id]))
            events.append(_ev("use", ids=[mem.id], outcome="applied", auto=True))
        report = compute_eval(
            memories=[mem], events=events, endorsement_min_retrievals=5
        )
        text = render_text(report)
        assert "Endorsement-debt memories" in text
        assert mem.id in text


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLI:
    def test_eval_subcommand_empty_store(self, tmp_path: Path, capsys: Any) -> None:
        """The CLI runs end-to-end against a fresh empty store."""
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "eval", "--since", "7d"]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        captured = capsys.readouterr()
        assert "bettermemory eval" in captured.out
        assert "n/a" in captured.out  # empty store → undefined rates

    def test_eval_subcommand_json_schema(self, tmp_path: Path, capsys: Any) -> None:
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "eval", "--json", "--since", "all"]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["counts"]["retrieval_occurrences"] == 0
        assert parsed["memory_helped_rate"]["rate"] is None
        assert (
            parsed["endorsement_debt"]["min_retrievals"]
            == DEFAULT_ENDORSEMENT_MIN_RETRIEVALS
        )
        assert parsed["window_seconds"] is None  # 'all'

    def test_eval_tool_usage_subcommand_text(self, tmp_path: Path, capsys: Any) -> None:
        """The --tool-usage CLI mode runs end-to-end and renders the
        per-tool rollup header so a downstream tail-the-output script
        can grep for it. Empty store gives every tool a zero count."""
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "eval", "--tool-usage", "--since", "all"]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        captured = capsys.readouterr()
        assert "bettermemory eval --tool-usage" in captured.out
        assert "memory_search" in captured.out
        # Empty store → zero tool calls total.
        assert "Tool calls             0" in captured.out

    def test_eval_tool_usage_subcommand_json(self, tmp_path: Path, capsys: Any) -> None:
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "eval", "--tool-usage", "--json", "--since", "all"]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["total_tool_calls"] == 0
        # Schema sanity: rows is a list, every entry has a tool name.
        assert isinstance(parsed["rows"], list)
        assert all("tool" in r and "count" in r for r in parsed["rows"])
        # The 18-tool surface lands in JSON too.
        tool_names = {r["tool"] for r in parsed["rows"]}
        assert "memory_search" in tool_names
        assert "memory_health" in tool_names

    def test_eval_threshold_sweep_subcommand_text(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """End-to-end CLI smoke. Empty store → "no replayable misses"
        message lands in stdout."""
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "eval", "--threshold-sweep", "--since", "all"]
        try:
            server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

        captured = capsys.readouterr()
        assert "bettermemory eval --threshold-sweep" in captured.out
        assert "No replayable misses" in captured.out

    def test_eval_threshold_sweep_and_tool_usage_mutually_exclusive(
        self, tmp_path: Path
    ) -> None:
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = [
            "bettermemory",
            "eval",
            "--tool-usage",
            "--threshold-sweep",
            "--since",
            "all",
        ]
        try:
            with pytest.raises(SystemExit):
                server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

    def test_eval_rejects_garbage_since(self, tmp_path: Path) -> None:
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "eval", "--since", "garbage"]
        try:
            with pytest.raises(SystemExit):
                server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save

    def test_eval_against_recorded_events(self, tmp_path: Path) -> None:
        """End-to-end: record real events via Recorder, write a memory
        via Store, run compute_eval over iter_all_events output."""
        from bettermemory.events import iter_all_events

        memdir = tmp_path / "memdir"
        store = Store(memdir)
        recorder = Recorder(root=memdir, session_id="sess-test")

        # Store.write builds and persists the Memory in one call.
        mem = store.write(
            content="The auth middleware lives in src/auth/middleware.py.",
            scopes=["tools"],
            confidence=Confidence.HIGH,
            source=Source.EXPLICIT,
        )

        recorder.record("search", returned=[mem.id], relevance=["high"])
        recorder.record(
            "use",
            ids=[mem.id],
            outcome="applied",
            claim_excerpts=["The body's first sentence."],
        )

        report = compute_eval(
            memories=store.load_all(),
            events=iter_all_events(memdir),
        )
        assert report.retrieval_occurrences == 1
        assert report.applied_total == 1
        assert report.applied_explicit == 1
        assert report.explicit_endorsements_with_excerpt == 1
        assert report.memory_helped_rate.rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# compute_tool_usage — per-MCP-tool call counts
# ---------------------------------------------------------------------------


class TestComputeToolUsage:
    def test_empty_events_returns_zero_rows_for_every_tool(self) -> None:
        """An empty event log still surfaces one row per known tool
        (all zero counts) so a consumer can branch on "tool never called"
        without a missing-key guard. Untelemetered tools surface too."""
        report = compute_tool_usage([])
        tool_names = {row.tool for row in report.rows}
        # The full 18 — explicit-mapped 17 plus the one in
        # TOOLS_WITHOUT_TELEMETRY (memory_health).
        assert len(report.rows) == 18
        assert "memory_search" in tool_names
        assert "memory_health" in tool_names
        for row in report.rows:
            assert row.count == 0
            assert row.share is None  # zero denominator
        assert report.total_tool_calls == 0
        assert report.unmapped_event_kinds == {}

    def test_counts_one_event_per_tool_call(self) -> None:
        events = [
            _ev("search", returned=["mid"]),
            _ev("search", returned=["mid"]),
            _ev("show", id="mid"),
            _ev("verify", id="mid"),
            _ev("verify", id="mid"),
            _ev("verify", id="mid"),
        ]
        report = compute_tool_usage(events)
        counts = {row.tool: row.count for row in report.rows}
        assert counts["memory_search"] == 2
        assert counts["memory_show"] == 1
        assert counts["memory_verify"] == 3
        assert report.total_tool_calls == 6

    def test_rows_sorted_by_count_descending(self) -> None:
        events = [_ev("search") for _ in range(5)] + [_ev("verify"), _ev("verify")]
        report = compute_tool_usage(events)
        # The first nonzero row should be memory_search (5); next memory_verify (2).
        nonzero = [r for r in report.rows if r.count > 0]
        assert [r.tool for r in nonzero] == ["memory_search", "memory_verify"]

    def test_side_effect_event_kinds_are_not_counted_as_tool_calls(self) -> None:
        """`search_miss`, `pending_expired`, and `silent_miss_cutoff`
        are side-effects of other tools (or CLI admin ops), not
        standalone tool calls. They must not inflate any tool's count
        and must not surface as unmapped either — a regression that
        moved any of these into `_TOOL_EVENT_KIND_TO_TOOL` would
        attribute admin operations to the wrong parent."""
        events = [
            _ev("search_miss"),
            _ev("search_miss"),
            _ev("pending_expired", pending_id="pending_x"),
            _ev("silent_miss_cutoff", cutoff_ts="2026-04-10T00:00:00Z"),
        ]
        report = compute_tool_usage(events)
        assert report.total_tool_calls == 0
        assert report.unmapped_event_kinds == {}

    def test_unmapped_event_kind_surfaces_in_report(self) -> None:
        """A new event kind that nobody updated the map for shows up
        in `unmapped_event_kinds` so the next contributor sees it
        rather than the count silently vanishing into thin air."""
        events = [
            _ev("brand_new_kind_no_one_mapped"),
            _ev("brand_new_kind_no_one_mapped"),
        ]
        report = compute_tool_usage(events)
        assert report.unmapped_event_kinds == {"brand_new_kind_no_one_mapped": 2}

    def test_since_filter_drops_old_events(self) -> None:
        old = _ev("search", ts="2026-01-01T00:00:00+00:00")
        recent = _ev("search", ts="2026-05-20T00:00:00+00:00")
        report = compute_tool_usage(
            [old, recent],
            since=timedelta(days=7),
            now=datetime(2026, 5, 22, tzinfo=timezone.utc),
        )
        # Only the recent event survives the window.
        counts = {r.tool: r.count for r in report.rows}
        assert counts["memory_search"] == 1

    def test_share_is_fraction_of_tool_calls(self) -> None:
        events = [_ev("search"), _ev("search"), _ev("show")]
        report = compute_tool_usage(events)
        for row in report.rows:
            if row.tool == "memory_search":
                assert row.share == pytest.approx(2 / 3)
            elif row.tool == "memory_show":
                assert row.share == pytest.approx(1 / 3)
            else:
                assert row.share == pytest.approx(0.0)

    def test_untelemetered_tool_marked(self) -> None:
        """memory_health doesn't emit a dedicated event, so the row exists
        with count=0 but flagged so the renderer can show "no telemetry"
        rather than "never called" — the two cases are different."""
        report = compute_tool_usage([])
        health_row = next(r for r in report.rows if r.tool == "memory_health")
        assert health_row.has_telemetry is False
        assert "memory_health" in TOOLS_WITHOUT_TELEMETRY

    def test_to_dict_is_self_describing(self) -> None:
        events = [_ev("search")]
        payload = compute_tool_usage(events).to_dict()
        assert payload["total_tool_calls"] == 1
        assert "rows" in payload
        assert any(r["tool"] == "memory_search" for r in payload["rows"])


# ---------------------------------------------------------------------------
# render_tool_usage_text — CLI rendering
# ---------------------------------------------------------------------------


class TestRenderToolUsageText:
    def test_text_renders_header_and_tool_rows(self) -> None:
        events = [_ev("search"), _ev("search"), _ev("show")]
        text = render_tool_usage_text(compute_tool_usage(events))
        assert "bettermemory eval --tool-usage" in text
        assert "memory_search" in text
        assert "memory_show" in text

    def test_text_marks_untelemetered_tools_distinctly(self) -> None:
        text = render_tool_usage_text(compute_tool_usage([]))
        # memory_health row carries the "no telemetry" caveat so the
        # zero count isn't misread as "never called".
        assert "memory_health" in text
        assert "no telemetry" in text

    def test_text_lists_unmapped_kinds_with_caveat(self) -> None:
        events = [_ev("freshly_added_kind")]
        text = render_tool_usage_text(compute_tool_usage(events))
        assert "Unmapped event kinds" in text
        assert "freshly_added_kind" in text

    def test_kind_map_parity_with_recorder_call_sites(self) -> None:
        """Every ``kind`` value passed to ``recorder.record()`` anywhere
        in ``src/bettermemory`` must appear in either
        ``_TOOL_EVENT_KIND_TO_TOOL`` (counted toward a tool's rollup) or
        ``_KNOWN_SIDE_EFFECT_KINDS`` (deliberately excluded — sub-events
        of other tools). A new event kind that's neither will silently
        show up in the ``unmapped_event_kinds`` footer rather than
        failing CI, which is exactly the slow-drift bug class this
        guards against.

        Implementation: AST-walk the source tree, extract every literal
        first-arg (positional) or ``kind=`` keyword passed to a method
        named ``record``, and assert set equality.

        Extraction is intentionally narrow — only literal-string ``kind``
        values are discovered. Patterns that would slip past:

        * ``recorder.record(KIND_CONST, ...)`` where ``KIND_CONST`` is a
          module-level ``Final[str]``.
        * ``recorder.record(kind=resolve_kind(x))`` or any non-literal
          expression for ``kind=``.
        * ``recorder.record(**payload)`` where ``kind`` is spread in.
        * ``kind`` passed as the second-or-later positional argument
          (only the first positional is read).

        Every call site in ``src/bettermemory`` today uses literal-string
        positional or ``kind=`` keyword form, so the assumption holds.
        If a future refactor switches to one of the patterns above,
        broaden the extractor here rather than letting drift sneak back
        in via the ``unmapped_event_kinds`` footer.
        """
        import ast

        src_root = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
        discovered: set[str] = set()
        for py_file in src_root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "record"):
                    continue
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    discovered.add(node.args[0].value)
                for kw in node.keywords:
                    if (
                        kw.arg == "kind"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        discovered.add(kw.value.value)

        mapped = set(_TOOL_EVENT_KIND_TO_TOOL.keys())
        side_effects = set(_KNOWN_SIDE_EFFECT_KINDS)

        overlap = mapped & side_effects
        assert not overlap, (
            f"kinds {sorted(overlap)} appear in BOTH _TOOL_EVENT_KIND_TO_TOOL "
            f"and _KNOWN_SIDE_EFFECT_KINDS. Move them to one place only."
        )

        expected = mapped | side_effects
        unmapped_in_src = discovered - expected
        assert not unmapped_in_src, (
            f"recorder.record() emits kind(s) {sorted(unmapped_in_src)} that "
            f"are neither in _TOOL_EVENT_KIND_TO_TOOL nor _KNOWN_SIDE_EFFECT_KINDS. "
            f"Add them to one or the other so the tool-usage rollup stays honest."
        )

        stale_in_map = expected - discovered
        assert not stale_in_map, (
            f"_TOOL_EVENT_KIND_TO_TOOL/_KNOWN_SIDE_EFFECT_KINDS contains "
            f"{sorted(stale_in_map)} but no recorder.record() call in src/ "
            f"emits those kinds. Stale entries — remove or fix the call site."
        )


# ---------------------------------------------------------------------------
# compute_threshold_sweep — counterfactual replay over logged misses
# ---------------------------------------------------------------------------


def _miss_event(
    *,
    top_hits: list[dict[str, Any]],
    recent_retrieval_count: int = 0,
    ts: str = "2026-05-15T12:00:00+00:00",
) -> dict[str, Any]:
    """Build a `search_miss` event in the canonical post-2.6.4 shape
    (with `top_hits` as list of dicts carrying `relevance` and
    `score`). Pre-2.6.4 hook events used `top_hit_ids` instead."""
    return _ev(
        "search_miss",
        ts=ts,
        top_hits=top_hits,
        recent_retrieval_count=recent_retrieval_count,
    )


def _hit(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "01XXXXXX",
        "score": 100.0,
        "relevance": "high",
        "scopes": ["tools"],
        "snippet": "snippet",
    }
    base.update(fields)
    return base


class TestComputeThresholdSweep:
    def test_v1_replay_matches_replayable_count(self) -> None:
        """v1 is the reference: replaying it over events the production
        rule already flagged must reproduce every flag (otherwise the
        helper's rule has drifted from production)."""
        events = [
            _miss_event(top_hits=[_hit(relevance="high", score=100.0)]),
            _miss_event(top_hits=[_hit(relevance="high", score=80.0)]),
            _miss_event(top_hits=[_hit(relevance="high", score=20.0)]),
        ]
        report = compute_threshold_sweep(events)
        v1_row = next(r for r in report.rows if r.rule == "v1_top1_high")
        assert v1_row.would_flag == report.replayable_misses == 3
        assert report.v1_drift == 0

    def test_v1_drift_surfaces_when_event_unreplayable_by_v1(self) -> None:
        """A logged miss whose top hit is `medium` (or otherwise can't
        be re-flagged by the in-process v1) increments `v1_drift`.
        This is the production-side guard the docstring promises:
        the helper diverging from the rule that originally fired the
        miss is observable rather than silent. The renderer surfaces
        a warning line when drift is non-zero."""
        events = [
            _miss_event(top_hits=[_hit(relevance="high", score=100.0)]),
            # This event was logged as a miss by production, but its
            # top hit is `medium` — the in-process v1 won't re-flag it.
            _miss_event(top_hits=[_hit(relevance="medium", score=100.0)]),
        ]
        report = compute_threshold_sweep(events)
        assert report.replayable_misses == 2
        assert report.v1_drift == 1
        text = render_threshold_sweep_text(report)
        assert "v1 replay drift" in text

    def test_recent_retrieval_count_true_does_not_count_as_one(self) -> None:
        """`isinstance(True, int)` is True in Python; a stray `True` in
        a torn `recent_retrieval_count` would slip past a naked int
        check and read as 1. Verify the bool guard zeroes it out."""
        events = [
            _miss_event(top_hits=[_hit()], recent_retrieval_count=True),
        ]
        report = compute_threshold_sweep(events)
        assert report.replayable_misses == 1
        # `recent` was coerced to 0 so v1 (which has no recent-retrieval
        # gate at top1=high) flags the miss; the meaningful assertion is
        # that this did not raise and did not silently read as 1.
        v1_row = next(r for r in report.rows if r.rule == "v1_top1_high")
        assert v1_row.would_flag == 1

    def test_v2_score_floor_drops_low_score_misses(self) -> None:
        events = [
            _miss_event(top_hits=[_hit(relevance="high", score=100.0)]),
            _miss_event(top_hits=[_hit(relevance="high", score=20.0)]),  # <50
            _miss_event(top_hits=[_hit(relevance="high", score=49.9)]),  # <50
        ]
        report = compute_threshold_sweep(events)
        v2 = next(r for r in report.rows if r.rule == "v2_top1_high_score_50")
        assert v2.would_flag == 1
        assert v2.delta_from_v1 == -2  # two misses fall below the floor

    def test_v3_dominance_drops_close_seconds(self) -> None:
        """v3 requires top-1 score >= 2 * top-2 score. An event whose
        top-1 narrowly beats top-2 doesn't clear the dominance bar."""
        dominated = _miss_event(
            top_hits=[
                _hit(relevance="high", score=100.0),
                _hit(relevance="medium", score=40.0),  # 100 >= 2*40, ok
            ]
        )
        not_dominant = _miss_event(
            top_hits=[
                _hit(relevance="high", score=100.0),
                _hit(relevance="medium", score=80.0),  # 100 < 2*80, drops
            ]
        )
        report = compute_threshold_sweep([dominated, not_dominant])
        v3 = next(r for r in report.rows if r.rule == "v3_top1_high_dominant")
        assert v3.would_flag == 1
        assert v3.delta_from_v1 == -1

    def test_v3_solo_hit_is_trivially_dominant(self) -> None:
        """A solo top-1 hit (no top-2 to compare against) cannot fail
        the dominance test — there's nothing for it to dominate against."""
        events = [_miss_event(top_hits=[_hit(relevance="high", score=10.0)])]
        report = compute_threshold_sweep(events)
        v3 = next(r for r in report.rows if r.rule == "v3_top1_high_dominant")
        assert v3.would_flag == 1

    def test_recent_retrieval_means_no_miss_under_any_rule(self) -> None:
        """The recent-retrieval shield is part of every rule's v1
        precondition; an event with non-zero recent_retrieval_count
        cannot flag under any of the bundled rules."""
        events = [
            _miss_event(
                top_hits=[_hit(relevance="high", score=200.0)],
                recent_retrieval_count=1,
            )
        ]
        report = compute_threshold_sweep(events)
        for row in report.rows:
            assert row.would_flag == 0

    def test_legacy_top_hit_ids_event_is_skipped_and_counted(self) -> None:
        """Pre-2.6.4 hook events wrote `top_hit_ids` (strings only,
        no relevance). The sweep can't replay them but counts them
        in `skipped_legacy_event_count` so the denominator is honest."""
        legacy = _ev("search_miss", top_hit_ids=["01XXXXXX"])
        modern = _miss_event(top_hits=[_hit(relevance="high")])
        report = compute_threshold_sweep([legacy, modern])
        assert report.skipped_legacy_event_count == 1
        assert report.replayable_misses == 1

    def test_non_miss_events_are_skipped(self) -> None:
        """The sweep only walks `search_miss` events; other kinds in
        the log must not pollute the count."""
        events = [_ev("search"), _ev("use"), _miss_event(top_hits=[_hit()])]
        report = compute_threshold_sweep(events)
        assert report.replayable_misses == 1
        assert report.total_events_scanned == 3

    def test_since_filter_drops_old_events(self) -> None:
        old = _miss_event(
            top_hits=[_hit()],
            ts="2026-01-01T00:00:00+00:00",
        )
        recent = _miss_event(
            top_hits=[_hit()],
            ts="2026-05-20T00:00:00+00:00",
        )
        report = compute_threshold_sweep(
            [old, recent],
            since=timedelta(days=7),
            now=datetime(2026, 5, 22, tzinfo=timezone.utc),
        )
        assert report.replayable_misses == 1

    def test_empty_events_emits_zero_replayable(self) -> None:
        report = compute_threshold_sweep([])
        assert report.replayable_misses == 0
        # Each known rule still appears as a row with would_flag=0 so a
        # downstream consumer doesn't need a missing-key guard.
        rule_names = {row.rule for row in report.rows}
        assert rule_names == set(THRESHOLD_RULES)

    def test_v1_first_in_row_order(self) -> None:
        """v1 is the reference rule and should land in position 0 of
        the rendered table regardless of its absolute flag count."""
        events = [_miss_event(top_hits=[_hit()])]
        report = compute_threshold_sweep(events)
        assert report.rows[0].rule == "v1_top1_high"

    def test_v2_halves_v1_on_synthetic_corpus(self) -> None:
        """The CHANGELOG calibration claim depends on this *mechanism*:
        replaying a corpus where half the v1-flagged misses fall below
        v2's score floor must show v2.would_flag == v1.would_flag / 2
        and delta_pct == 0.5. The maintainer's dogfood numbers are not
        in scope here (they live on a private log) — what's tested is
        the arithmetic that produces them. A bug in `delta_pct` or in
        the rule predicate would pass the per-row tests above but fail
        the aggregate shape this test pins down."""
        # 10 events: 5 above the score floor (would survive v2), 5 below.
        events = [
            _miss_event(top_hits=[_hit(relevance="high", score=60.0)]) for _ in range(5)
        ] + [
            _miss_event(top_hits=[_hit(relevance="high", score=10.0)]) for _ in range(5)
        ]
        report = compute_threshold_sweep(events)
        v1 = next(r for r in report.rows if r.rule == "v1_top1_high")
        v2 = next(r for r in report.rows if r.rule == "v2_top1_high_score_50")
        assert v1.would_flag == 10
        assert v2.would_flag == 5
        assert v2.delta_pct == 0.5
        assert v2.delta_from_v1 == -5


class TestComputeEvalListKind:
    def test_list_event_counts_as_retrieval(self) -> None:
        """`memory_list` is bundled with `memory_search` in audit.py's
        retrieval set; compute_eval must count it too so the eval
        denominator (`retrieval_occurrences`) stays aligned with the
        audit cadence. Without this, a workflow that leans on
        memory_list (browse-then-show) would distort the
        memory_helped_rate downward by underreporting the denominator."""
        events = [
            _ev("list", returned=["mem-A", "mem-B"]),
            _ev("use", ids=["mem-A"], outcome="applied", claim_excerpts=["x"]),
        ]
        report = compute_eval(memories=[], events=events)
        # Two ids returned by the list call = two retrieval occurrences.
        assert report.retrieval_occurrences == 2


class TestParseTsTzAware:
    def test_naive_iso_returns_utc(self) -> None:
        """A naive ISO timestamp (no `Z`, no `+00:00`) must be stamped
        as UTC, otherwise downstream comparison against the tz-aware
        cutoff would raise `TypeError` mid-iteration. The recorder
        always emits `Z`-suffixed timestamps, so this guards against
        external producers and older binaries."""
        from bettermemory.eval import _parse_ts

        parsed = _parse_ts("2026-05-15T12:00:00")
        assert parsed is not None
        assert parsed.tzinfo is timezone.utc

    def test_window_filter_does_not_crash_on_naive_ts(self) -> None:
        """End-to-end: a window filter over an event log containing a
        naive timestamp must not raise. Drops the event or includes it
        per the UTC interpretation, but does not propagate a tz
        comparison error to the caller."""
        events = [{"ts": "2026-05-15T12:00:00", "kind": "search_miss", "session": "s"}]
        # `now` and `since` cooperate to put the cutoff well in the past,
        # so the event survives the filter and contributes to silent_misses.
        report = compute_eval(
            memories=[],
            events=events,
            now=datetime(2026, 5, 20, tzinfo=timezone.utc),
            since=timedelta(days=30),
        )
        assert report.silent_misses == 1


class TestRenderThresholdSweepText:
    def test_renders_header_and_caveat(self) -> None:
        events = [_miss_event(top_hits=[_hit()])]
        text = render_threshold_sweep_text(compute_threshold_sweep(events))
        assert "bettermemory eval --threshold-sweep" in text
        # The caveat about strictly-looser rules being un-replayable
        # must surface so a reader doesn't misread the relative deltas
        # as absolute miss rates.
        assert "Caveat" in text
        assert "looser rules" in text

    def test_renders_legacy_skip_count(self) -> None:
        legacy = _ev("search_miss", top_hit_ids=["01XXXXXX"])
        modern = _miss_event(top_hits=[_hit()])
        text = render_threshold_sweep_text(compute_threshold_sweep([legacy, modern]))
        assert "skipped 1 legacy" in text

    def test_renders_empty_window_message(self) -> None:
        """An empty replayable bucket renders an explanatory message
        rather than a blank table — tells the user what to do next."""
        text = render_threshold_sweep_text(compute_threshold_sweep([]))
        assert "No replayable misses" in text
