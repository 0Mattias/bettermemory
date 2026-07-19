"""Tests for the `bettermemory eval` module + CLI subcommand.

Covers parse_since, compute_eval on each numerator/denominator path,
scope filtering, since-window filtering, Wilson intervals at the
endpoints, the silent-miss buffer cap, the cold-endorsement
exclusions (ambient, tombstoned, has-explicit-applied), and an
end-to-end CLI smoke through ``main()``.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.eval import (
    ADMIN_RECORDED_ATTRIBUTION_PREFIX,
    ADMIN_RECORDED_EVENT_KINDS,
    DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
    THRESHOLD_RULES,
    TOOLS_WITHOUT_TELEMETRY,
    RateCI,
    _IN_SESSION_SIDE_EFFECT_KINDS,
    _KNOWN_SIDE_EFFECT_KINDS,
    _TOOL_EVENT_KIND_TO_TOOL,
    _wilson_interval,
    compute_eval,
    compute_report,
    compute_threshold_sweep,
    compute_tool_usage,
    compute_widening_preview,
    is_admin_recorded_event,
    parse_since,
    render_report_markdown,
    render_text,
    render_threshold_sweep_text,
    render_tool_usage_text,
    render_widening_preview_text,
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

    def test_out_of_range_raises_valueerror_not_overflow(self) -> None:
        # The regex's `\d+` accepts an arbitrarily long digit run, so a
        # huge value overflows the `timedelta` constructor (raising
        # OverflowError internally). parse_since must surface that as a
        # ValueError so the single clean-error path — and the CLI's
        # parser.error handler — covers it; an uncaught OverflowError
        # would escape as a traceback.
        with pytest.raises(ValueError, match="--since"):
            parse_since("999999999999999999999d")
        with pytest.raises(ValueError, match="--since"):
            parse_since("9" * 400 + "d")


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
        assert report.cold_endorsement_memories_rows == []
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

    def test_no_signal_audits_excluded_from_denominator(self) -> None:
        """`no_signal` audits (probe declined: empty store, gated probe,
        semantic model unavailable) are not miss-capable turns — they land
        in `turns_no_signal`, not the silent_miss_rate denominator. Mirrors
        health.py's `no_signal_total` split: a config stuck at permanent
        no_signal must not read as a healthy 0% miss rate over a growing
        denominator."""
        events = [
            _ev("turn_audited", verdict="ok"),
            _ev("turn_audited", verdict="no_signal"),
            _ev("turn_audited", verdict="no_signal"),
            _ev("search_miss", session_id="sess-A"),
        ]
        report = compute_eval(memories=[], events=events)
        assert report.turns_audited == 1
        assert report.turns_no_signal == 2
        assert report.silent_misses == 1
        assert report.silent_miss_rate.rate == pytest.approx(1.0)
        assert report.to_dict()["counts"]["turns_no_signal"] == 2

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
# silent-miss invalidation markers — cutoff + per-event ack
# ---------------------------------------------------------------------------


class TestSilentMissInvalidation:
    """`compute_eval` honors the same two escape hatches health.py's
    rollups honor — the bulk `silent_miss_cutoff` (written by
    `bettermemory consolidate --acknowledge-misses-before`) and the
    per-event `miss_ack` (written by `memory_acknowledge_miss`).
    Before this suite existed, the eval CLI counted every in-window
    event, so after either hatch ran, `bettermemory eval`'s
    silent_miss_rate silently disagreed with `memory_health` /
    `memory_scope_overview` over the same event stream — and the eval
    CLI is the surface docs/eval.md tells people to compute the
    publishable trio from."""

    @staticmethod
    def _utc(year: int, month: int, day: int) -> datetime:
        return datetime(year, month, day, tzinfo=timezone.utc)

    @classmethod
    def _z(cls, year: int, month: int, day: int) -> str:
        """Canonical `Z`-suffixed cutoff_ts shape the consolidate CLI writes."""
        return cls._utc(year, month, day).isoformat().replace("+00:00", "Z")

    def test_cutoff_drops_pre_cutoff_events_from_both_sides(self) -> None:
        """`turn_audited` AND `search_miss` events before cutoff_ts drop
        from numerator, both denominator buckets, and the triage buffer.
        Filtering only the numerator would skew the rate (low miss /
        high audited) — the joint pin mirrors health's
        test_silent_miss_cutoff_drops_numerator_and_denominator_together."""
        events = [
            _ev("turn_audited", ts=self._utc(2026, 4, 1), verdict="ok"),
            _ev("turn_audited", ts=self._utc(2026, 4, 2), verdict="no_signal"),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 3),
                session_id="sess-pre",
                top_hits=[{"id": "mem-pre", "relevance": "high"}],
            ),
            _ev("turn_audited", ts=self._utc(2026, 4, 20), verdict="ok"),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 21),
                session_id="sess-post",
                top_hits=[{"id": "mem-post", "relevance": "high"}],
            ),
            _ev(
                "silent_miss_cutoff",
                ts=self._utc(2026, 4, 25),
                cutoff_ts=self._z(2026, 4, 10),
            ),
        ]
        report = compute_eval(memories=[], events=events)
        assert report.turns_audited == 1
        assert report.turns_no_signal == 0
        assert report.silent_misses == 1
        assert report.silent_miss_rate.rate == pytest.approx(1.0)
        # The invalidated miss vanishes from the triage buffer too —
        # surfacing known-bad telemetry there would contradict the hatch.
        assert [c.top_missed_id for c in report.silent_miss_recent] == ["mem-post"]

    def test_cutoff_keeps_events_at_exact_boundary(self) -> None:
        """`ts >= cutoff_ts` survives — same boundary as health's
        `_count_post_cutoff`. Flipping the guard to `>` would silently
        diverge the two surfaces on second-exact telemetry."""
        boundary = self._utc(2026, 4, 10)
        events = [
            _ev("turn_audited", ts=boundary - timedelta(seconds=1), verdict="ok"),
            _ev("search_miss", ts=boundary - timedelta(seconds=1), session_id="s-pre"),
            _ev("turn_audited", ts=boundary, verdict="ok"),
            _ev("search_miss", ts=boundary, session_id="s-at"),
            _ev("silent_miss_cutoff", ts=boundary, cutoff_ts=self._z(2026, 4, 10)),
        ]
        report = compute_eval(memories=[], events=events)
        assert report.turns_audited == 1
        assert report.silent_misses == 1

    def test_cutoff_latest_wins_and_earlier_cutoff_cannot_shrink(self) -> None:
        """With multiple cutoff events the max `cutoff_ts` wins — an
        earlier cutoff arriving LATER in the log cannot un-shrink the
        invalidated window. Both the miss side and the audited side
        resolve to the same cutoff value."""
        events = [
            _ev("turn_audited", ts=self._utc(2026, 4, 5), verdict="ok"),
            _ev("search_miss", ts=self._utc(2026, 4, 5), session_id="s-a"),
            _ev("turn_audited", ts=self._utc(2026, 4, 15), verdict="ok"),
            _ev("search_miss", ts=self._utc(2026, 4, 15), session_id="s-b"),
            _ev("turn_audited", ts=self._utc(2026, 4, 25), verdict="ok"),
            _ev("search_miss", ts=self._utc(2026, 4, 25), session_id="s-c"),
            # Newer cutoff written first…
            _ev(
                "silent_miss_cutoff",
                ts=self._utc(2026, 4, 26),
                cutoff_ts=self._z(2026, 4, 20),
            ),
            # …then a stale earlier cutoff lands later in the log.
            _ev(
                "silent_miss_cutoff",
                ts=self._utc(2026, 4, 27),
                cutoff_ts=self._z(2026, 4, 10),
            ),
        ]
        report = compute_eval(memories=[], events=events)
        # Max cutoff is 04-20: only the 04-25 audit + miss survive.
        assert report.turns_audited == 1
        assert report.silent_misses == 1

    def test_acked_miss_dropped_but_audited_denominator_keeps_turn(self) -> None:
        """A `miss_ack` retracts exactly the one referenced miss from
        the numerator (and the triage buffer). The denominator is NOT
        reduced — the audit itself wasn't the false positive (the audit
        ran, the probe found something, the model acknowledged the
        verdict): filter #3 of health's `_silent_miss_stats`. A dangling
        ack referencing a never-logged event_id degrades silently."""
        events = [
            _ev("turn_audited", ts=self._utc(2026, 4, 1), verdict="ok"),
            _ev("turn_audited", ts=self._utc(2026, 4, 2), verdict="ok"),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 5),
                session_id="s-a",
                event_id="EVID_A",
                top_hits=[{"id": "mem-A", "relevance": "high"}],
            ),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 6),
                session_id="s-b",
                event_id="EVID_B",
                top_hits=[{"id": "mem-B", "relevance": "high"}],
            ),
            _ev(
                "miss_ack",
                ts=self._utc(2026, 4, 10),
                event_id="EVID_A",
                reason="false positive",
            ),
            _ev("miss_ack", ts=self._utc(2026, 4, 11), event_id="EVID_DANGLING"),
        ]
        report = compute_eval(memories=[], events=events)
        assert report.silent_misses == 1
        assert report.turns_audited == 2
        assert report.silent_miss_rate.rate == pytest.approx(0.5)
        assert [c.top_missed_id for c in report.silent_miss_recent] == ["mem-B"]

    def test_markers_resolve_globally_under_since_window(self) -> None:
        """A cutoff event whose own ts falls OUTSIDE the `--since`
        window still applies to in-window telemetry — global-marker
        semantics, mirroring `curation_counts`' delta-mode exemption
        (health's test_silent_miss_cutoff_resolved_globally_under_since_delta).
        Without the exemption a windowed eval run would over-count
        events the health surfaces have already invalidated."""
        now = self._utc(2026, 5, 1)
        events = [
            # Cutoff written long before the window opens.
            _ev(
                "silent_miss_cutoff",
                ts=self._utc(2026, 1, 1),
                cutoff_ts=self._z(2026, 4, 20),
            ),
            # In-window (>= 04-10) but pre-cutoff — must drop.
            _ev("turn_audited", ts=self._utc(2026, 4, 15), verdict="ok"),
            _ev("search_miss", ts=self._utc(2026, 4, 15), session_id="s-pre"),
            # In-window and post-cutoff — survives.
            _ev("turn_audited", ts=self._utc(2026, 4, 25), verdict="ok"),
            _ev("search_miss", ts=self._utc(2026, 4, 25), session_id="s-post"),
        ]
        report = compute_eval(
            memories=[], events=events, now=now, since=timedelta(days=21)
        )
        assert report.turns_audited == 1
        assert report.silent_misses == 1

    def test_stream_without_markers_is_pinned_byte_identical(self) -> None:
        """No-regression pin: a stream carrying NO cutoff/ack events
        produces exactly the report the pre-invalidation implementation
        produced — the buffer-then-resolve restructure must be invisible
        unless a marker exists. Every count, both denominators, the
        threshold-rule tracking, the buffer contents, and the full
        serialised shape are pinned (values hand-computed against the
        pre-change event loop)."""
        now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        mem = _mem()
        events = [
            _ev("search", returned=[mem.id]),
            _ev("show", id=mem.id),
            _ev("use", ids=[mem.id], outcome="applied", claim_excerpts=["load"]),
            _ev("use", ids=[mem.id], outcome="applied", auto=True),
            _ev("turn_audited", verdict="ok", threshold_rule="v1_top1_high"),
            _ev("turn_audited", verdict="no_signal"),
            _ev(
                "search_miss",
                session_id="sess-A",
                threshold_rule="v1_top1_high",
                top_hits=[{"id": "mem-A", "relevance": "high"}],
            ),
            # Legacy audit without a verdict — stays miss-capable.
            _ev("turn_audited"),
        ]
        report = compute_eval(memories=[mem], events=events, now=now)
        assert report.to_dict() == {
            "generated_at": now.isoformat(),
            "window_seconds": None,
            "scope_filter": None,
            "threshold_rule": "v1_top1_high",
            "total_events_scanned": 8,
            # No `--since` window, so the window-scoped twin equals the
            # all-time count.
            "events_in_window": 8,
            "counts": {
                "retrieval_occurrences": 2,
                "explicit_endorsements_with_excerpt": 1,
                "applied_total": 2,
                "applied_explicit": 1,
                "turns_audited": 2,
                "turns_no_signal": 1,
                "silent_misses": 1,
                "repeat_audits": 0,
            },
            "by_model": {},
            "memory_helped_rate": RateCI.from_counts(1, 2).to_dict(),
            "endorsement_rate": RateCI.from_counts(1, 2).to_dict(),
            "silent_miss_rate": RateCI.from_counts(1, 2).to_dict(),
            "cold_endorsement_memories": {
                "min_retrievals": DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
                "total": 0,
                "rows": [],
            },
            "silent_miss_recent": [
                {
                    "ts": "2026-05-15T12:00:00.000+00:00",
                    "session_id": "sess-A",
                    "top_missed_id": "mem-A",
                    "top_missed_relevance": "high",
                    "threshold_rule": "v1_top1_high",
                    "recent_retrieval_count": 0,
                }
            ],
        }

    def test_parity_with_compute_health_over_same_stream(self) -> None:
        """compute_eval's audited / no_signal / miss counts must equal
        `health.compute_health`'s SilentMissStats over the same
        synthetic stream — the two surfaces report the same citable
        metric, and the whole point of mirroring the invalidation is
        that they cannot disagree after an escape hatch runs. The
        stream exercises every filter arm at once: pre-cutoff drops
        (both kinds + no_signal), the ack, a tombstoned top-hit (drops
        on both sides), a legacy `top_hit_ids` miss whose id is
        tombstoned (counts on both sides — the filter is
        canonical-only), a legacy un-ack-able miss, and a legacy
        verdict-less audit."""
        from bettermemory.health import compute_health

        now = self._utc(2026, 5, 1)
        tomb_id = generate_ulid()
        events = [
            # Pre-cutoff — all three drop.
            _ev("turn_audited", ts=self._utc(2026, 4, 1), verdict="ok"),
            _ev("turn_audited", ts=self._utc(2026, 4, 2), verdict="no_signal"),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 3),
                session_id="s-old",
                event_id="EVID_OLD",
                top_hits=[{"id": "mem-old", "relevance": "high"}],
            ),
            _ev(
                "silent_miss_cutoff",
                ts=self._utc(2026, 4, 10),
                cutoff_ts=self._z(2026, 4, 10),
            ),
            # Post-cutoff telemetry.
            _ev("turn_audited", ts=self._utc(2026, 4, 12), verdict="ok"),
            _ev("turn_audited", ts=self._utc(2026, 4, 13), verdict="no_signal"),
            _ev("turn_audited", ts=self._utc(2026, 4, 14)),  # legacy, miss-capable
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 15),
                session_id="s-a",
                event_id="EVID_A",
                top_hits=[{"id": "mem-A", "relevance": "high"}],
            ),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 16),
                session_id="s-b",
                event_id="EVID_B",
                top_hits=[{"id": "mem-B", "relevance": "high"}],
            ),
            # Legacy miss without an event_id — cannot be acked.
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 17),
                session_id="s-c",
                top_hits=[{"id": "mem-C", "relevance": "high"}],
            ),
            _ev(
                "miss_ack",
                ts=self._utc(2026, 4, 18),
                event_id="EVID_A",
                reason="false positive",
            ),
            # Tombstoned top-hit — drops from BOTH numerators (health's
            # `_silent_miss_stats` filter #2; audited denominators keep
            # their turns on both sides).
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 19),
                session_id="s-d",
                event_id="EVID_D",
                top_hits=[{"id": tomb_id, "relevance": "high"}],
            ),
            # Legacy `top_hit_ids` shape carrying the SAME tombstoned id
            # — counts on both sides: the filter reads the canonical
            # `top_hits[0].id` only, and the legacy shape degrades to
            # None (can't-prove-tombstoned conservative read).
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 20),
                session_id="s-e",
                top_hit_ids=[tomb_id],
            ),
        ]
        report = compute_eval(
            memories=[], events=events, now=now, tombstoned_ids={tomb_id}
        )
        health_stats = compute_health(
            [], events, now=now, tombstoned_ids={tomb_id}
        ).silent_misses
        assert report.turns_audited == health_stats.audited_total == 2
        assert report.turns_no_signal == health_stats.no_signal_total == 1
        # EVID_B + legacy s-c + legacy s-e survive; EVID_A acked,
        # EVID_D tombstone-filtered, everything pre-cutoff dropped.
        assert report.silent_misses == health_stats.miss_total == 3

    def test_tombstoned_top_hit_dropped_from_numerator_not_denominator(self) -> None:
        """A miss whose canonical top-hit memory is in `tombstoned_ids`
        drops from the numerator and the triage buffer — once the memory
        is gone the miss is no longer actionable — while BOTH audited
        denominator buckets keep their turns (audits carry no per-memory
        payload): health's `_silent_miss_stats` filter #2."""
        tomb = generate_ulid()
        events = [
            _ev("turn_audited", ts=self._utc(2026, 4, 1), verdict="ok"),
            _ev("turn_audited", ts=self._utc(2026, 4, 2), verdict="no_signal"),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 3),
                session_id="s-tomb",
                top_hits=[{"id": tomb, "relevance": "high"}],
            ),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 4),
                session_id="s-live",
                top_hits=[{"id": "mem-live", "relevance": "high"}],
            ),
        ]
        report = compute_eval(memories=[], events=events, tombstoned_ids={tomb})
        assert report.silent_misses == 1
        assert report.turns_audited == 1
        assert report.turns_no_signal == 1
        assert report.silent_miss_rate.rate == pytest.approx(1.0)
        assert [c.top_missed_id for c in report.silent_miss_recent] == ["mem-live"]

    def test_tombstone_filter_is_canonical_only_matching_health(self) -> None:
        """A legacy `top_hit_ids`-shaped miss whose id IS in the
        tombstone set still counts: health's `_parse_silent_miss_event`
        reads only the canonical `top_hits` payload, degrades the legacy
        shape to None, and falls through filter #2 on the
        can't-prove-tombstoned conservative read. Eval must extract the
        same way — reusing the renderer's legacy `top_hit_ids` fallback
        for the filter would drop events health counts, splitting the
        two surfaces on pre-2.6.4 archives. Pinned against
        compute_health directly."""
        from bettermemory.health import compute_health

        now = self._utc(2026, 5, 1)
        tomb = generate_ulid()
        events = [
            _ev("turn_audited", ts=self._utc(2026, 4, 1), verdict="ok"),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 2),
                session_id="s-legacy",
                top_hit_ids=[tomb],
            ),
        ]
        report = compute_eval(
            memories=[], events=events, now=now, tombstoned_ids={tomb}
        )
        health_stats = compute_health(
            [], events, now=now, tombstoned_ids={tomb}
        ).silent_misses
        assert report.silent_misses == health_stats.miss_total == 1

    def test_no_tombstones_is_pinned_byte_identical(self) -> None:
        """Default `tombstoned_ids=None`, an explicit empty set, and a
        set matching nothing all produce the identical serialised report
        over a miss-carrying stream — the parameter must be invisible
        unless a top-hit id actually matches, so `run_driver` / the
        comparative harness and every existing caller stay untouched."""
        events = [
            _ev("turn_audited", ts=self._utc(2026, 4, 1), verdict="ok"),
            _ev(
                "search_miss",
                ts=self._utc(2026, 4, 2),
                session_id="s-a",
                top_hits=[{"id": "mem-A", "relevance": "high"}],
            ),
        ]
        now = self._utc(2026, 5, 1)
        baseline = compute_eval(memories=[], events=events, now=now).to_dict()
        assert (
            compute_eval(
                memories=[], events=events, now=now, tombstoned_ids=set()
            ).to_dict()
            == baseline
        )
        assert (
            compute_eval(
                memories=[], events=events, now=now, tombstoned_ids={"mem-other"}
            ).to_dict()
            == baseline
        )
        assert baseline["counts"]["silent_misses"] == 1


# ---------------------------------------------------------------------------
# cold-endorsement rows
# ---------------------------------------------------------------------------


class TestColdEndorsementMemories:
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
        assert report.cold_endorsement_memories_total == 1
        row = report.cold_endorsement_memories_rows[0]
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
        assert report.cold_endorsement_memories_total == 0

    def test_row_excluded_when_explicit_applied_exists(self) -> None:
        mem = _mem()
        report = compute_eval(
            memories=[mem],
            events=self._make_events(mem.id, retrievals=5, with_explicit=True),
            endorsement_min_retrievals=5,
        )
        assert report.cold_endorsement_memories_total == 0

    def test_row_excluded_when_no_apply_happened_at_all(self) -> None:
        """A memory retrieved over the floor with ZERO applies (neither
        auto nor explicit) is dead_weight, NOT cold-endorsement.

        cold-endorsement is the COMPLEMENT of dead_weight — "applies
        happened, but every one was the auto fallback." Before the
        `auto + explicit > 0` gate, a never-applied memory satisfied the
        `explicit_applied_count == 0` test and surfaced here, mis-routing
        a removal candidate onto the acknowledge-debt path and inflating
        the rollup. Mirrors health's `_is_weakly_endorsed` /
        `curation_counts` `applied_count == 0` gate so the two surfaces
        agree."""
        mem = _mem()
        # 5 retrievals, NO use events — never applied at all.
        events = [_ev("search", returned=[mem.id]) for _ in range(5)]
        report = compute_eval(
            memories=[mem],
            events=events,
            endorsement_min_retrievals=5,
        )
        assert report.cold_endorsement_memories_total == 0
        assert report.cold_endorsement_memories_rows == []

    def test_ambient_memory_excluded(self) -> None:
        mem = _mem(category=Category.AMBIENT)
        report = compute_eval(
            memories=[mem],
            events=self._make_events(mem.id, retrievals=10),
            endorsement_min_retrievals=5,
        )
        assert report.cold_endorsement_memories_total == 0

    def test_tombstoned_memory_excluded(self) -> None:
        # mem isn't in the memories list → can't be attributed to a row.
        ghost_id = generate_ulid()
        report = compute_eval(
            memories=[],
            events=self._make_events(ghost_id, retrievals=10),
            endorsement_min_retrievals=5,
        )
        assert report.cold_endorsement_memories_total == 0

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
        assert [r.id for r in report.cold_endorsement_memories_rows] == [m2.id, m1.id]


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

    def test_cold_endorsement_memories_section(self) -> None:
        mem = _mem()
        events = []
        for _ in range(5):
            events.append(_ev("search", returned=[mem.id]))
            events.append(_ev("use", ids=[mem.id], outcome="applied", auto=True))
        report = compute_eval(
            memories=[mem], events=events, endorsement_min_retrievals=5
        )
        text = render_text(report)
        assert "Cold-endorsement memories" in text
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
            parsed["cold_endorsement_memories"]["min_retrievals"]
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

    def test_eval_rejects_out_of_range_since_cleanly(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        # An out-of-range --since (the regex accepts an arbitrarily long
        # digit run) must exit via parser.error (SystemExit, code 2) with
        # a clean message — NOT escape as an OverflowError traceback.
        from bettermemory.server import main as server_main

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(tmp_path / "memdir")
        argv_save = sys.argv[:]
        sys.argv = ["bettermemory", "eval", "--since", "999999999999999999999d"]
        try:
            with pytest.raises(SystemExit) as excinfo:
                server_main()
        finally:
            sys.argv = argv_save
            if env_save is None:
                os.environ.pop("BETTERMEMORY_DIR", None)
            else:
                os.environ["BETTERMEMORY_DIR"] = env_save
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--since" in err
        assert "Traceback (most recent call last)" not in err

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

    def test_eval_cli_applies_store_tombstone_filter(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The CLI wrapper enumerates the store's real tombstone set
        (`store.load_tombstones()`, same as `health.report_for_directory`)
        and passes it into compute_eval — after a miss's top-hit memory
        is tombstoned, `bettermemory eval` must agree with `memory_health`
        (numerator and triage list drop, audited denominator keeps its
        turn) rather than keep counting the unactionable miss."""
        from bettermemory.server import main as server_main

        memdir = tmp_path / "memdir"
        store = Store(memdir)
        recorder = Recorder(root=memdir, session_id="sess-test")
        mem = store.write(
            content="The auth middleware lives in src/auth/middleware.py.",
            scopes=["tools"],
            confidence=Confidence.HIGH,
            source=Source.EXPLICIT,
        )
        recorder.record("turn_audited", verdict="miss", session_id="sess-test")
        recorder.record(
            "search_miss",
            session_id="sess-test",
            top_hits=[{"id": mem.id, "score": 10.0, "relevance": "high"}],
        )
        store.tombstone(mem.id, "superseded")

        env_save = os.environ.get("BETTERMEMORY_DIR")
        os.environ["BETTERMEMORY_DIR"] = str(memdir)
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

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["counts"]["turns_audited"] == 1
        assert parsed["counts"]["silent_misses"] == 0
        assert parsed["silent_miss_recent"] == []


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
        # The full 25 — explicit-mapped 24 (memory_* + memory_curate +
        # memory_proposals + episode_write + episode_handoff +
        # episode_search + episode_promote + memory_acknowledge_miss) plus
        # the one in TOOLS_WITHOUT_TELEMETRY (memory_health).
        assert len(report.rows) == 25
        assert "memory_search" in tool_names
        assert "memory_health" in tool_names
        assert "episode_write" in tool_names
        assert "episode_handoff" in tool_names
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
# Tool-count parity — runtime registrations vs. eval-side enumeration
# ---------------------------------------------------------------------------


# The canonical tool count surfaced in prose ("25 MCP tools" — 21
# `memory_*` + 4 `episode_*` — in README / api.md / marketplace / plugin
# README). Pinned here as the single source of truth so a regression in
# either the runtime registrations or the eval-side enumeration trips
# one assertion instead of leaving the prose silently out of sync.
# Prose authors verify against this constant.
_EXPECTED_TOOL_COUNT = 25


async def test_tool_count_matches_registered_count(tmp_path: Path) -> None:
    """The eval-side tool enumeration agrees with the runtime registry.

    Two independent sources of truth name "the tools that exist":

    1. ``builder._register_tools`` — one ``mcp.tool(name=...)`` call per
       tool, surfaced at runtime via ``mcp.list_tools()``.
    2. ``eval._TOOL_EVENT_KIND_TO_TOOL`` values plus
       ``eval.TOOLS_WITHOUT_TELEMETRY`` — drives ``compute_tool_usage``'s
       per-tool row set so a consumer can iterate every tool without a
       missing-key guard.

    Both must enumerate the same set, or:

    - A new ``mcp.tool(...)`` registration that nobody added to the eval
      map produces a tool that ``compute_tool_usage`` silently ignores.
    - A new entry in the eval map without a runtime registration produces
      a "tool" the server can't actually serve, surfacing as a zero-count
      row that never moves.

    This test pins the set-equality, and pins the count to
    ``_EXPECTED_TOOL_COUNT`` so prose claims of "25 MCP tools" have
    something to track against.
    """
    from bettermemory.config import Config, StorageConfig
    from bettermemory.server import build_server
    from bettermemory.session import SessionState
    from bettermemory.store import Store

    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    registered = {tool.name for tool in await mcp.list_tools()}

    eval_side = set(_TOOL_EVENT_KIND_TO_TOOL.values()) | set(TOOLS_WITHOUT_TELEMETRY)

    assert eval_side == registered, (
        "Eval-side tool enumeration drifted from runtime-registered tools. "
        f"Only in eval map: {sorted(eval_side - registered)}; "
        f"only on server: {sorted(registered - eval_side)}. "
        "Update _TOOL_EVENT_KIND_TO_TOOL / TOOLS_WITHOUT_TELEMETRY in "
        "eval.py, or add the missing mcp.tool(...) registration in "
        "builder._register_tools, to bring the two surfaces back in sync."
    )
    assert len(registered) == _EXPECTED_TOOL_COUNT, (
        f"Runtime tool count is {len(registered)} but _EXPECTED_TOOL_COUNT "
        f"is {_EXPECTED_TOOL_COUNT}. Either a tool was added/removed and "
        "the constant + prose ('25 MCP tools' in docs/internals.md / "
        "api.md / marketplace / plugin README) needs to track it, or the "
        "registration list grew without the docs catching up."
    )


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

    def test_cutoff_invalidated_misses_dropped_from_replay_and_footnote(self) -> None:
        """Misses earlier than the latest `silent_miss_cutoff` were
        flagged by a since-fixed code bug, not by a genuine rule
        decision — they vanish from the replay set AND the legacy-skip
        footnote, as if never logged, so bug-invalidated telemetry can't
        pollute the "is v1 over-firing" calibration. Latest `cutoff_ts`
        wins: a stale earlier cutoff arriving later in the log cannot
        shrink the invalidated window (same max-semantics as
        `compute_eval` / health)."""
        events = [
            # Pre-cutoff modern miss — bug-flagged, drops.
            _miss_event(top_hits=[_hit()], ts="2026-04-01T00:00:00+00:00"),
            # Pre-cutoff legacy miss — drops from the footnote too.
            _ev(
                "search_miss",
                ts="2026-04-02T00:00:00+00:00",
                top_hit_ids=["01XXXXXX"],
            ),
            # Post-cutoff modern miss — survives.
            _miss_event(top_hits=[_hit()], ts="2026-04-20T00:00:00+00:00"),
            _ev(
                "silent_miss_cutoff",
                ts="2026-04-25T00:00:00+00:00",
                cutoff_ts="2026-04-10T00:00:00Z",
            ),
            # Stale earlier cutoff later in the log — ignored.
            _ev(
                "silent_miss_cutoff",
                ts="2026-04-26T00:00:00+00:00",
                cutoff_ts="2026-03-01T00:00:00Z",
            ),
        ]
        report = compute_threshold_sweep(events)
        assert report.replayable_misses == 1
        assert report.skipped_legacy_event_count == 0
        assert report.v1_drift == 0
        v1 = next(r for r in report.rows if r.rule == "v1_top1_high")
        assert v1.would_flag == 1

    def test_cutoff_resolves_globally_under_since_window(self) -> None:
        """A cutoff event whose own ts falls OUTSIDE `--since` still
        invalidates in-window telemetry — the same global-marker
        semantics `compute_eval` applies, so a windowed sweep can't
        replay events every rate surface has already dropped."""
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        events = [
            # Cutoff written long before the window opens.
            _ev(
                "silent_miss_cutoff",
                ts="2026-01-01T00:00:00+00:00",
                cutoff_ts="2026-04-20T00:00:00Z",
            ),
            # In-window but pre-cutoff — must drop.
            _miss_event(top_hits=[_hit()], ts="2026-04-15T00:00:00+00:00"),
            # In-window and post-cutoff — survives.
            _miss_event(top_hits=[_hit()], ts="2026-04-25T00:00:00+00:00"),
        ]
        report = compute_threshold_sweep(events, since=timedelta(days=21), now=now)
        assert report.replayable_misses == 1

    def test_acked_misses_retained_in_replay(self) -> None:
        """`miss_ack` retractions are deliberately NOT honored by the
        sweep: an acked miss is a confirmed false positive of the
        current rule — exactly the calibration signal a stricter
        candidate is judged against. The rate surfaces drop it (they
        report outstanding actionable misses); the sweep replays rule
        decisions, so it stays. The v2 pin below shows the payoff: the
        stricter rule demonstrably would have declined the miss a human
        had to retract."""
        events = [
            _ev(
                "search_miss",
                ts="2026-04-15T00:00:00+00:00",
                event_id="EVID_FP",
                # Below v2's score floor — v1 fired, v2 wouldn't have.
                top_hits=[_hit(relevance="high", score=20.0)],
                recent_retrieval_count=0,
            ),
            _ev(
                "miss_ack",
                ts="2026-04-16T00:00:00+00:00",
                event_id="EVID_FP",
                reason="false positive",
            ),
        ]
        report = compute_threshold_sweep(events)
        assert report.replayable_misses == 1
        v1 = next(r for r in report.rows if r.rule == "v1_top1_high")
        v2 = next(r for r in report.rows if r.rule == "v2_top1_high_score_50")
        assert v1.would_flag == 1
        assert v2.would_flag == 0


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


# ---------------------------------------------------------------------------
# compute_report / render_report_markdown — the --report artifact
# ---------------------------------------------------------------------------


def _run_eval_cli(memdir: Path, *argv: str) -> None:
    """Run ``bettermemory eval *argv`` end-to-end through ``server_main``
    with ``BETTERMEMORY_DIR`` pointed at ``memdir``, restoring env and
    ``sys.argv`` afterwards. Same save/restore ceremony the inline
    ``TestCLI`` blocks perform; factored because the report tests need
    it six times."""
    from bettermemory.server import main as server_main

    env_save = os.environ.get("BETTERMEMORY_DIR")
    os.environ["BETTERMEMORY_DIR"] = str(memdir)
    argv_save = sys.argv[:]
    sys.argv = ["bettermemory", "eval", *argv]
    try:
        server_main()
    finally:
        sys.argv = argv_save
        if env_save is None:
            os.environ.pop("BETTERMEMORY_DIR", None)
        else:
            os.environ["BETTERMEMORY_DIR"] = env_save


class TestComputeReport:
    def test_empty_inputs_with_window(self) -> None:
        doc = compute_report([], [], since=timedelta(days=30), version="1.2.3-test")
        assert doc.window_seconds == 30 * 86400
        assert doc.alltime_eval.window_seconds is None
        assert doc.window_eval is not doc.alltime_eval
        assert doc.active_memory_count == 0
        assert doc.total_events == 0
        assert doc.distinct_session_count == 0
        assert doc.version == "1.2.3-test"

    def test_since_all_reuses_the_alltime_report(self) -> None:
        """``--since all`` makes the window column the all-time column;
        compute_report reuses one object rather than recomputing an
        identical report, and the renderer collapses to one column."""
        doc = compute_report([], [], since=None, version="0-test")
        assert doc.window_eval is doc.alltime_eval
        assert doc.window_seconds is None

    def test_one_shot_iterator_feeds_all_four_computations(self) -> None:
        """``iter_all_events`` is a one-shot iterator; compute_report
        must materialise it once — if any of the four sub-computations
        saw an exhausted stream, its counts would silently read zero."""
        events = iter(
            [
                _ev("search", returned=["m1"]),
                _ev("show", id="m1"),
            ]
        )
        doc = compute_report(
            [],
            events,
            since=timedelta(days=30),
            now=datetime(2026, 5, 20, tzinfo=timezone.utc),
            version="0-test",
        )
        assert doc.window_eval.retrieval_occurrences == 2
        assert doc.alltime_eval.retrieval_occurrences == 2
        assert doc.tool_usage.total_tool_calls == 2
        assert doc.total_events == 2

    def test_distinct_sessions_counted_via_fallback_chain(self) -> None:
        """Sessions are counted through the same ``session_id`` /
        ``session`` fallback ``_silent_miss_from_event`` reads. Only the
        COUNT lands on the document — the ids themselves never do."""
        events = [
            _ev("search", session="s-A"),
            _ev("search", session_id="s-B"),
            _ev("search", session="s-A"),
        ]
        doc = compute_report([], events, version="0-test")
        assert doc.distinct_session_count == 2

    def test_admin_cli_events_do_not_invent_distinct_sessions(self) -> None:
        """Admin/CLI event kinds are recorded outside any client session
        under a fresh throwaway session id (see
        ``ADMIN_RECORDED_EVENT_KINDS``). Counting their ids publishes a
        "Store shape: N distinct sessions" figure inflated by sessions
        that never existed — one `doctor --fix` run would manufacture a
        second "session" on a single-session store."""
        events = [
            _ev("search", session="s-real"),
            _ev("doctor_fix", session="cli-doctor-run"),
            _ev("silent_miss_cutoff", session="cli-ack-run"),
        ]
        doc = compute_report([], events, version="0-test")
        assert doc.distinct_session_count == 1

    def test_in_session_side_effects_still_count_as_sessions(self) -> None:
        """The complement of the exclusion above: side-effect kinds the
        recorder writes INSIDE a live client session carry that client's
        own session id and must keep counting — over-excluding would
        under-report the store shape just as badly."""
        events = [_ev("search_miss", session="s-real")]
        doc = compute_report([], events, version="0-test")
        assert doc.distinct_session_count == 1

    def test_admin_recorded_kinds_derive_from_the_side_effect_roster(self) -> None:
        """``ADMIN_RECORDED_EVENT_KINDS`` is the ONE shared constant the
        session-counting surfaces read. Pin the derivation so a typo in
        the in-session list can't silently reclassify a kind: every name
        in the in-session subset must exist on the roster it subtracts
        from, and no in-session kind may leak into the admin set."""
        unknown = _IN_SESSION_SIDE_EFFECT_KINDS - _KNOWN_SIDE_EFFECT_KINDS
        assert not unknown, (
            f"_IN_SESSION_SIDE_EFFECT_KINDS names kind(s) {sorted(unknown)} "
            f"that _KNOWN_SIDE_EFFECT_KINDS does not carry — a typo here "
            f"silently leaves a real admin kind counted as a session."
        )
        assert not (ADMIN_RECORDED_EVENT_KINDS & _IN_SESSION_SIDE_EFFECT_KINDS)
        assert "doctor_fix" in ADMIN_RECORDED_EVENT_KINDS
        assert "silent_miss_cutoff" in ADMIN_RECORDED_EVENT_KINDS

    def test_version_defaults_to_installed_package_metadata(self) -> None:
        doc = compute_report([], [])
        assert isinstance(doc.version, str)
        assert doc.version


class TestRenderReportMarkdown:
    # The seven content-skeleton anchors, in the order the contract
    # specifies them. The footer anchor doubles as the methodology check.
    SECTION_ANCHORS = (
        "# bettermemory eval report",
        "## Rates",
        "## Reading these numbers honestly",
        "## Per-model audit telemetry (all time)",
        "## Threshold sweep (counterfactual, all time)",
        "## Tool usage (top 10, all time)",
        "Generated by `bettermemory eval --report` v",
    )

    def test_all_sections_present_and_ordered(self) -> None:
        doc = compute_report([], [], since=timedelta(days=30), version="0-test")
        md = render_report_markdown(doc)
        positions = [md.find(anchor) for anchor in self.SECTION_ANCHORS]
        assert all(p >= 0 for p in positions), positions
        assert positions == sorted(positions)

    def test_rate_cells_render_counts_rate_and_ci(self) -> None:
        mem = _mem()
        events = [
            # Aged out of the 30d window — in the all-time column only.
            _ev("search", ts="2026-01-01T00:00:00.000+00:00", returned=[mem.id]),
            _ev("search", returned=[mem.id]),
            _ev(
                "use",
                ids=[mem.id],
                outcome="applied",
                claim_excerpts=["a load-bearing claim"],
            ),
        ]
        doc = compute_report(
            [mem],
            events,
            since=timedelta(days=30),
            now=datetime(2026, 5, 20, tzinfo=timezone.utc),
            version="0-test",
        )
        md = render_report_markdown(doc)
        assert "| rate | last 30d | all time |" in md
        # Window column bold, all-time column plain — the same k/n =
        # rate [lo, hi] shape docs/eval-results.md publishes.
        assert "| `memory_helped_rate` | 1/1 = **1.00**" in md
        assert "Wilson 95%" in md
        # Every figure on a `last Nd:` denominator note is window-scoped,
        # the leading event count included. One of the three events aged
        # out, so the window figure must sit strictly BELOW the all-time
        # figure — publishing `total_events_scanned` (which counts the
        # whole log) under the window label read as an in-window count.
        assert "- last 30d: 2 events scanned ·" in md
        assert "- all time: 3 events scanned ·" in md

    def test_single_column_when_window_is_all_time(self) -> None:
        doc = compute_report([], [], since=None, version="0-test")
        md = render_report_markdown(doc)
        assert "window: all time" in md
        assert "| rate | all time |" in md
        assert "last 30d" not in md

    def test_torn_read_note_surfaces(self) -> None:
        """A use event in-window whose retrieval aged out clamps the
        helped rate to 1.0; the report must carry the same windowing-
        artifact honesty note the text renderer does."""
        mem = _mem()
        events = [
            # Aged-out retrieval — outside the 7d window.
            _ev("search", ts="2026-01-01T00:00:00+00:00", returned=[mem.id]),
            # In-window retrieval — denominator 1.
            _ev("search", ts="2026-05-18T00:00:00+00:00", returned=[mem.id]),
            # Two in-window attested uses — numerator 2 > denominator 1.
            _ev(
                "use",
                ts="2026-05-19T00:00:00+00:00",
                ids=[mem.id],
                outcome="applied",
                claim_excerpts=["claim"],
            ),
            _ev(
                "use",
                ts="2026-05-19T01:00:00+00:00",
                ids=[mem.id],
                outcome="applied",
                claim_excerpts=["claim"],
            ),
        ]
        doc = compute_report(
            [mem],
            events,
            since=timedelta(days=7),
            now=datetime(2026, 5, 20, tzinfo=timezone.utc),
            version="0-test",
        )
        assert doc.window_eval.memory_helped_rate.torn_read is True
        md = render_report_markdown(doc)
        assert "windowing artifact" in md

    def test_model_name_pipe_is_escaped(self) -> None:
        """`client_model` is the one log-derived string the renderer
        emits; a stray `|` in it must not shift table columns."""
        events = [_ev("turn_audited", verdict="clean", client_model="weird|model")]
        doc = compute_report([], events, version="0-test")
        md = render_report_markdown(doc)
        assert "weird\\|model" in md
        assert "| weird|model |" not in md

    def test_renderer_never_touches_leak_capable_fields(self) -> None:
        """Unit-level half of the canary contract: compute_report's
        parts DO carry leak-capable payloads (cold-endorsement rows
        with memory summaries + scope names, silent-miss candidates
        with session ids, the log-derived threshold_rule string) — and
        the renderer provably reads none of them."""
        # Lowercase because it doubles as a scope-name component (scopes
        # validate lowercase-alphanumeric with hyphens/colons); still
        # distinctive enough that an accidental match is impossible.
        canary = "leakcanary9q4"
        mem = _mem(body=f"Body {canary} first sentence.", scopes=[f"scope-{canary}"])
        events = [
            *[
                _ev("search", returned=[mem.id], query=f"find {canary}")
                for _ in range(5)
            ],
            _ev("use", ids=[mem.id], outcome="applied", auto=True),
            _ev(
                "turn_audited",
                verdict="miss",
                session_id=f"sess-{canary}",
                probe_query=f"{canary} probe",
            ),
            _ev(
                "search_miss",
                session_id=f"sess-{canary}",
                threshold_rule=f"rule-{canary}",
                top_hits=[
                    {
                        "id": mem.id,
                        "score": 60.0,
                        "relevance": "high",
                        "scopes": [f"scope-{canary}"],
                        "snippet": f"snippet {canary}",
                    }
                ],
                recent_retrieval_count=0,
            ),
        ]
        doc = compute_report([mem], events, version="0-test")
        # Prove the leak-capable fields are POPULATED on the parts —
        # otherwise this test would pass vacuously.
        assert doc.alltime_eval.cold_endorsement_memories_rows
        assert doc.alltime_eval.silent_miss_recent
        assert doc.alltime_eval.threshold_rule == f"rule-{canary}"
        md = render_report_markdown(doc)
        assert canary not in md
        # ...while the numbers those events feed still render.
        assert "0/5 = **0.00**" in md  # helped rate over 5 retrievals
        assert "1/1 = **1.00**" in md  # silent-miss rate 1/1

    def test_untelemetered_tool_row_is_marked_not_published_as_zero(self) -> None:
        """A tool in ``TOOLS_WITHOUT_TELEMETRY`` emits no dedicated event,
        so its rollup count is structurally 0. Published as a bare
        ``| memory_health | 0 | 0.0% |`` row it is indistinguishable from
        a tool nobody ever called — the artifact then implies nobody uses
        memory_health. Mirror the text renderer's "(no telemetry)"
        treatment instead."""
        events = [_ev("search"), _ev("show")]
        doc = compute_report([], events, version="0-test")
        # Non-vacuity: the untelemetered row is actually inside the
        # top-10 slice the renderer publishes.
        assert "memory_health" in [r.tool for r in doc.tool_usage.rows[:10]]
        md = render_report_markdown(doc)
        assert "| `memory_health` (no telemetry) | — | — |" in md
        assert "| `memory_health` | 0 |" not in md


class TestReportCLI:
    def test_report_canary_never_leaks_store_content(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """THE safety property, end to end: seed a real store + event
        log whose memory bodies, summaries, scope names, logged queries
        (verbatim mode — maximally adversarial), probe queries, session
        ids, snippets, a threshold_rule string, AND the store directory
        name all carry a distinctive canary token; run the real CLI;
        assert the token appears NOWHERE in the report while the seeded
        counts still show up as non-trivial numbers."""
        # Lowercase because the token doubles as a scope-name component
        # (scopes validate lowercase-alphanumeric with hyphens/colons).
        canary = "kanary7q3zx"
        memdir = tmp_path / f"store-{canary}"
        store = Store(memdir)
        recorder = Recorder(
            root=memdir,
            session_id=f"sess-{canary}-1",
            log_queries_verbatim=True,
        )
        mem_a = store.write(
            content=f"The {canary} deploy script lives in scripts/{canary}.sh.",
            scopes=[f"projects:{canary}-proj"],
            confidence=Confidence.HIGH,
            source=Source.EXPLICIT,
        )
        mem_b = store.write(
            content=f"Prefers {canary}-flavoured tutorials, code first.",
            scopes=[f"{canary}-learning"],
            confidence=Confidence.HIGH,
            source=Source.EXPLICIT,
        )
        for _ in range(5):
            recorder.record(
                "search",
                query=f"where is the {canary} deploy script",
                returned=[mem_a.id, mem_b.id],
            )
        for _ in range(2):
            recorder.record(
                "use",
                ids=[mem_a.id],
                outcome="applied",
                claim_excerpts=[f"The {canary} deploy script lives in scripts/."],
            )
        # mem_b: 5 retrievals, one auto apply, zero explicit — lands in
        # the cold-endorsement rows (canary summary + scopes) that the
        # interactive text mode prints and the report must not.
        recorder.record("use", ids=[mem_b.id], outcome="applied", auto=True)
        recorder.record(
            "turn_audited", verdict="clean", client_model="claude-test-model"
        )
        recorder.record(
            "turn_audited", verdict="clean", client_model="claude-test-model"
        )
        recorder.record(
            "turn_audited",
            verdict="miss",
            client_model="claude-test-model",
            probe_query=f"{canary} probe query",
        )
        recorder.record(
            "turn_audited", verdict="no_signal", client_model="claude-test-model"
        )
        recorder.record(
            "search_miss",
            client_model="claude-test-model",
            probe_query=f"find the {canary} script",
            threshold_rule=f"v1-{canary}-custom",
            top_hits=[
                {
                    "id": mem_a.id,
                    "score": 60.0,
                    "relevance": "high",
                    "scopes": [f"projects:{canary}-proj"],
                    "snippet": f"The {canary} deploy script",
                }
            ],
            recent_retrieval_count=0,
        )

        _run_eval_cli(memdir, "--report", "--since", "30d")
        out = capsys.readouterr().out

        # Zero leakage — the canary planted across every leak-capable
        # surface appears nowhere in the artifact.
        assert canary not in out
        # No substring of the store path leaks either (the directory
        # name itself carries the canary; these pin the full path and
        # the pytest tmp root too).
        assert str(memdir) not in out
        assert str(tmp_path) not in out
        assert memdir.name not in out

        # Non-vacuity guard: the seeded counts show up as numbers.
        assert "**2** active memories" in out
        assert "**13** logged events" in out
        assert "**1** distinct session." in out  # singular — one recorder
        assert "2/10 = **0.20**" in out  # memory_helped_rate, window column
        assert "2/3 = **0.67**" in out  # endorsement_rate
        assert "1/3 = **0.33**" in out  # silent_miss_rate
        assert "| claude-test-model | 3 | 1 | 1 |" in out  # by-model slice
        assert "| `v1_top1_high` | 1 | — | 100.0% |" in out  # sweep row
        assert "| `memory_search` | 5 | 41.7% |" in out  # tool-usage row

        # And the whole skeleton rendered.
        for anchor in TestRenderReportMarkdown.SECTION_ANCHORS:
            assert anchor in out

    def test_report_empty_store_renders_full_skeleton(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        _run_eval_cli(tmp_path / "memdir", "--report")
        out = capsys.readouterr().out
        assert "# bettermemory eval report" in out
        assert "n/a (k=0, n=0)" in out
        assert "No replayable misses" in out
        assert "No per-model telemetry" in out
        assert "Generated by `bettermemory eval --report` v" in out

    def test_report_output_writes_file_and_stdout_stays_silent(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        out_file = tmp_path / "report.md"
        _run_eval_cli(
            tmp_path / "memdir",
            "--report",
            "--since",
            "all",
            "--output",
            str(out_file),
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        text = out_file.read_text(encoding="utf-8")
        assert text.startswith("# bettermemory eval report")
        assert "Generated by `bettermemory eval --report` v" in text

    @pytest.mark.parametrize(
        "extra",
        [
            ["--json"],
            ["--tool-usage"],
            ["--threshold-sweep"],
            ["--widening-preview"],
            ["--detail"],
        ],
    )
    def test_report_conflicting_flags_hard_error(
        self, tmp_path: Path, capsys: Any, extra: list[str]
    ) -> None:
        """--report is a self-contained mode: combining it with --json
        or any other mode flag exits via parser.error (SystemExit 2,
        message names --report) — same clean-error style as the
        --detail-without---widening-preview guard."""
        with pytest.raises(SystemExit) as excinfo:
            _run_eval_cli(tmp_path / "memdir", "--report", *extra)
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--report" in err
        assert "Traceback (most recent call last)" not in err

    def test_output_without_report_hard_errors(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _run_eval_cli(tmp_path / "memdir", "--output", str(tmp_path / "x.md"))
        assert excinfo.value.code == 2
        assert "--output only applies to --report" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Windowed "Events scanned" denominators
# ---------------------------------------------------------------------------
#
# Every text renderer stamps a `— last {window}` header and then prints an
# "Events scanned" row underneath it. The rollups each keep TWO event
# tallies: `total_events_scanned` walks the WHOLE log (the invalidation
# markers are resolved ahead of the window filter, so the walk cannot stop
# at the window edge) and `events_in_window` is its window-scoped twin.
# Printing the all-time figure under a windowed header publishes a number
# that is simply not what the label says it is — the same defect
# `_md_denominator_note` was fixed for. These pin all four renderers.

_WINDOW_NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
_WINDOW_SINCE = timedelta(days=7)
# Aged out of the 7d window — counted all-time, never in-window.
_AGED_TS = "2026-01-01T00:00:00.000+00:00"
_FRESH_TS = "2026-05-19T00:00:00.000+00:00"


def _events_scanned_row(text: str) -> int:
    """The integer on the renderer's ``Events scanned`` row."""
    for line in text.splitlines():
        if line.startswith("Events scanned"):
            return int(line.split()[-1])
    raise AssertionError(f"no 'Events scanned' row in rendering:\n{text}")


def _audit_event(ts: str) -> dict[str, Any]:
    """A replayable (3.14+, miss-capable, non-repeat) ``turn_audited``."""
    return _ev(
        "turn_audited",
        ts=ts,
        verdict="ok",
        recent_retrieval_count=0,
        top_hits=[_hit(relevance="high", score=100.0)],
    )


class TestWindowedEventCounts:
    """One aged-out event + one in-window event through each renderer:
    the printed count must be 1 (the window), never 2 (the log)."""

    def test_render_text_events_scanned_is_window_scoped(self) -> None:
        mem = _mem()
        events = [
            _ev("search", ts=_AGED_TS, returned=[mem.id]),
            _ev("search", ts=_FRESH_TS, returned=[mem.id]),
        ]
        report = compute_eval(
            memories=[mem], events=events, now=_WINDOW_NOW, since=_WINDOW_SINCE
        )
        # Non-vacuity: the two tallies genuinely disagree on this stream,
        # so the assertion below can distinguish them.
        assert report.total_events_scanned == 2
        assert report.events_in_window == 1
        text = render_text(report)
        assert "— last 7d" in text
        assert _events_scanned_row(text) == 1

    def test_threshold_sweep_events_scanned_is_window_scoped(self) -> None:
        events = [
            _miss_event(top_hits=[_hit()], ts=_AGED_TS),
            _miss_event(top_hits=[_hit()], ts=_FRESH_TS),
        ]
        report = compute_threshold_sweep(events, now=_WINDOW_NOW, since=_WINDOW_SINCE)
        assert report.total_events_scanned == 2
        assert report.events_in_window == 1
        text = render_threshold_sweep_text(report)
        assert "— last 7d" in text
        assert _events_scanned_row(text) == 1

    def test_widening_preview_events_scanned_is_window_scoped(self) -> None:
        events = [_audit_event(_AGED_TS), _audit_event(_FRESH_TS)]
        report = compute_widening_preview(events, now=_WINDOW_NOW, since=_WINDOW_SINCE)
        assert report.total_events_scanned == 2
        assert report.events_in_window == 1
        # The aged-out audit is dropped by the window filter, not
        # miscounted as feature-less.
        assert report.audits_with_features == 1
        assert report.audits_without_features == 0
        text = render_widening_preview_text(report)
        assert "— last 7d" in text
        assert _events_scanned_row(text) == 1

    def test_tool_usage_events_scanned_is_window_scoped(self) -> None:
        events = [
            _ev("search", ts=_AGED_TS),
            _ev("search", ts=_FRESH_TS),
        ]
        report = compute_tool_usage(events, now=_WINDOW_NOW, since=_WINDOW_SINCE)
        assert report.total_events_scanned == 2
        assert report.events_in_window == 1
        assert report.total_tool_calls == 1
        text = render_tool_usage_text(report)
        assert "— last 7d" in text
        assert _events_scanned_row(text) == 1

    def test_all_time_window_collapses_the_two_tallies(self) -> None:
        """The complement: with no `--since`, the window IS the log, so
        every rollup's twin equals its all-time tally. Guards against a
        "fix" that makes the window count structurally smaller."""
        events = [_ev("search", ts=_AGED_TS), _ev("search", ts=_FRESH_TS)]
        for report in (
            compute_eval(memories=[], events=events, now=_WINDOW_NOW),
            compute_threshold_sweep(events, now=_WINDOW_NOW),
            compute_widening_preview(events, now=_WINDOW_NOW),
            compute_tool_usage(events, now=_WINDOW_NOW),
        ):
            assert report.events_in_window == report.total_events_scanned == 2

    def test_unparseable_ts_counts_as_out_of_window_everywhere(self) -> None:
        """An event with a broken `ts` is out-of-window under the same
        conservative read the per-event filters apply — the twin must not
        drift from the filter it mirrors."""
        events = [_ev("search", ts="not-a-timestamp"), _ev("search", ts=_FRESH_TS)]
        for report in (
            compute_eval(
                memories=[], events=events, now=_WINDOW_NOW, since=_WINDOW_SINCE
            ),
            compute_threshold_sweep(events, now=_WINDOW_NOW, since=_WINDOW_SINCE),
            compute_widening_preview(events, now=_WINDOW_NOW, since=_WINDOW_SINCE),
            compute_tool_usage(events, now=_WINDOW_NOW, since=_WINDOW_SINCE),
        ):
            assert report.total_events_scanned == 2
            assert report.events_in_window == 1


# ---------------------------------------------------------------------------
# ADMIN_RECORDED_EVENT_KINDS — parity pin against forked copies
# ---------------------------------------------------------------------------
#
# The constant's INVARIANT comment claims every session-counting consumer
# reads THAT constant and none keeps its own copy. A comment cannot
# enforce that, and the claim was false at its own introduction (doctor
# carried a hand-written frozenset of the same two names). These tests
# make the claim mechanical: any literal set of event kinds anywhere in
# `src/` or `tests/` that looks like a fork of the admin roster must equal
# it exactly, so a copy that drifts — or a new consumer that forks one —
# fails CI instead of silently disagreeing about which sessions are real.


def _literal_str_set(node: ast.AST) -> frozenset[str] | None:
    """The string members of a literal ``{...}`` / ``frozenset({...})`` /
    ``set([...])`` expression, or ``None`` when the node isn't one (or
    carries a non-literal member, which we can't evaluate statically)."""
    elts: list[ast.expr]
    if isinstance(node, ast.Set):
        elts = list(node.elts)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("frozenset", "set")
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))
    ):
        elts = list(node.args[0].elts)
    else:
        return None
    members: list[str] = []
    for elt in elts:
        if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
            return None
        members.append(elt.value)
    return frozenset(members)


def _admin_roster_forks(
    source: str, label: str
) -> list[tuple[str, str, frozenset[str]]]:
    """Every assignment in ``source`` whose value is a literal string set
    that reads as a copy of the admin roster.

    The predicate — names at least one admin-only kind AND no in-session
    kind — is what separates a fork from the two legitimate rosters in
    eval.py: `_KNOWN_SIDE_EFFECT_KINDS` is the superset (it carries
    `search_miss`, an in-session kind, so it fails the second clause) and
    `ADMIN_RECORDED_EVENT_KINDS` itself is a set-difference expression,
    not a literal, so it is never a candidate.
    """
    found: list[tuple[str, str, frozenset[str]]] = []
    for node in ast.walk(ast.parse(source)):
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        members = _literal_str_set(value)
        if members is None:
            continue
        if not (members & ADMIN_RECORDED_EVENT_KINDS):
            continue
        if members & _IN_SESSION_SIDE_EFFECT_KINDS:
            continue
        name = next(
            (t.id for t in targets if isinstance(t, ast.Name)),
            "<unnamed>",
        )
        found.append((label, name, members))
    return found


class TestAdminRecordedParity:
    def test_fork_detector_catches_a_drifted_copy(self) -> None:
        """Pin the pin. The scan below is only worth anything if its
        extractor actually fires; run it over a synthetic module that
        forks the roster and drops a kind, and confirm it is reported.
        Without this the real-tree scan could pass vacuously the day the
        last literal copy disappears."""
        drifted = sorted(ADMIN_RECORDED_EVENT_KINDS)[:1]
        synthetic = f"_MY_ADMIN_KINDS = frozenset({{{drifted[0]!r}}})\n"
        forks = _admin_roster_forks(synthetic, "<synthetic>")
        assert [(name, members) for _, name, members in forks] == [
            ("_MY_ADMIN_KINDS", frozenset(drifted))
        ]
        # ...and the drifted copy is exactly what the real assertion
        # rejects: it is not equal to the canonical roster.
        assert frozenset(drifted) != ADMIN_RECORDED_EVENT_KINDS

    def test_no_module_forks_the_admin_roster(self) -> None:
        """The invariant, mechanically. Every literal admin-kind set in
        the repo must equal ``ADMIN_RECORDED_EVENT_KINDS`` — a consumer
        may spell its own alias, but it may not disagree about the
        contents, and the day eval.py grows a third admin kind every
        surviving copy fails until it is reconciled (or, better, deleted
        in favour of importing the constant)."""
        repo_root = Path(__file__).resolve().parents[1]
        forks: list[tuple[str, str, frozenset[str]]] = []
        for tree_dir in (repo_root / "src", repo_root / "tests"):
            for py_file in sorted(tree_dir.rglob("*.py")):
                forks.extend(
                    _admin_roster_forks(
                        py_file.read_text(encoding="utf-8"),
                        str(py_file.relative_to(repo_root)),
                    )
                )
        drifted = [
            (label, name, sorted(members))
            for label, name, members in forks
            if members != ADMIN_RECORDED_EVENT_KINDS
        ]
        assert not drifted, (
            f"admin-kind roster forked and drifted: {drifted}. Every "
            f"session-counting consumer must read "
            f"eval.ADMIN_RECORDED_EVENT_KINDS "
            f"({sorted(ADMIN_RECORDED_EVENT_KINDS)}) rather than keeping a "
            f"hand-written copy — a copy that disagrees means two surfaces "
            f"disagree about which sessions ever existed."
        )

    def test_doctor_admin_kind_attributes_equal_the_canonical_roster(self) -> None:
        """The one consumer outside eval.py, checked by value rather than
        by name so it survives the constant being imported, aliased, or
        renamed on doctor's side. Any module attribute of doctor whose
        name mentions ADMIN and whose value is a set of strings must be
        this roster."""
        from bettermemory import doctor

        candidates = {
            name: value
            for name, value in vars(doctor).items()
            if "ADMIN" in name.upper()
            and isinstance(value, (set, frozenset))
            and all(isinstance(x, str) for x in value)
        }
        mismatched = {
            name: sorted(value)
            for name, value in candidates.items()
            if frozenset(value) != ADMIN_RECORDED_EVENT_KINDS
        }
        assert not mismatched, (
            f"doctor's admin-kind set(s) {mismatched} disagree with "
            f"eval.ADMIN_RECORDED_EVENT_KINDS "
            f"({sorted(ADMIN_RECORDED_EVENT_KINDS)}). The cadence census and "
            f"the published session tally would then exclude different "
            f"events from 'is this a real client session?'."
        )


# ---------------------------------------------------------------------------
# Attribution-based admin exclusion — the axis kinds cannot cover
# ---------------------------------------------------------------------------


class TestAdminRecordedAttribution:
    def test_acknowledge_debt_row_does_not_invent_a_session(self) -> None:
        """`bettermemory consolidate --acknowledge-debt` records
        ``kind="use"`` — a kind real client sessions also emit — under a
        fresh throwaway ``SessionState()`` id. Kind-based exclusion
        structurally cannot catch it without blinding the tally to every
        genuine session, so the exclusion runs off ``attribution``. Left
        unexcluded, one acknowledge-debt run publishes a phantom session
        in the report's store-shape line."""
        events = [
            _ev("search", session="s-real"),
            _ev(
                "use",
                session="01JCLI0000000000000000000A",
                ids=["mem-1"],
                outcome="applied",
                auto=False,
                attribution="cli_acknowledge_debt",
                note="bettermemory consolidate --acknowledge-debt",
            ),
        ]
        doc = compute_report([], events, version="0-test")
        assert doc.distinct_session_count == 1

    def test_in_session_use_events_still_count(self) -> None:
        """The complement — over-excluding would under-report the store
        shape just as badly. Every in-session attribution tier (and the
        pre-attribution back-compat shape) keeps its session."""
        events = [
            _ev("use", session="s-model", attribution="model"),
            _ev("use", session="s-hook", attribution="hook"),
            _ev("use", session="s-auto", auto=True, attribution="auto"),
            _ev("use", session="s-legacy"),  # pre-attribution event
        ]
        doc = compute_report([], events, version="0-test")
        assert doc.distinct_session_count == 4

    def test_exclusion_is_scoped_to_the_session_tally(self) -> None:
        """acknowledge-debt rows ARE genuine endorsements — recording
        them is the entire point of the subcommand — so they must keep
        counting toward the applied/endorsement denominators. Only the
        session tally rejects them."""
        mem = _mem()
        events = [
            _ev(
                "use",
                session="01JCLI0000000000000000000A",
                ids=[mem.id],
                outcome="applied",
                auto=False,
                attribution="cli_acknowledge_debt",
            )
        ]
        doc = compute_report([mem], events, version="0-test")
        assert doc.distinct_session_count == 0
        assert doc.alltime_eval.applied_total == 1
        assert doc.alltime_eval.applied_explicit == 1

    def test_predicate_reads_both_axes(self) -> None:
        events_and_expected: list[tuple[dict[str, Any], bool]] = [
            ({"kind": "doctor_fix"}, True),
            ({"kind": "silent_miss_cutoff"}, True),
            ({"kind": "use", "attribution": "cli_acknowledge_debt"}, True),
            (
                {"kind": "silent_miss_cutoff", "attribution": "cli_acknowledge_misses"},
                True,
            ),
            ({"kind": "search_miss"}, False),
            ({"kind": "use", "attribution": "hook"}, False),
            ({"kind": "search"}, False),
            # Non-string attribution can't be a prefix match and must not
            # raise — the log is plaintext and hand-editable.
            ({"kind": "use", "attribution": 7}, False),
        ]
        for ev, expected in events_and_expected:
            assert is_admin_recorded_event(ev) is expected, ev

    def test_recorded_events_actually_carry_the_attribution_field(
        self, tmp_path: Path
    ) -> None:
        """The exclusion is built on a field that must survive the real
        write path — `Recorder.record` merges arbitrary kwargs but also
        runs a redaction pass, and a redacted-away `attribution` would
        make the whole mechanism a no-op on production logs. Round-trip
        the acknowledge-debt shape through the real recorder."""
        from bettermemory.events import iter_all_events

        recorder = Recorder(root=tmp_path, session_id="01JCLI0000000000000000000A")
        recorder.record(
            "use",
            ids=["mem-1"],
            outcome="applied",
            auto=False,
            attribution="cli_acknowledge_debt",
            note="bettermemory consolidate --acknowledge-debt",
        )
        written = [e for e in iter_all_events(tmp_path) if e.get("kind") == "use"]
        assert len(written) == 1
        assert written[0]["attribution"] == "cli_acknowledge_debt"
        assert is_admin_recorded_event(written[0]) is True
        # And end to end: the real on-disk shape publishes no session.
        doc = compute_report([], written, version="0-test")
        assert doc.distinct_session_count == 0

    def test_every_literal_attribution_in_src_picks_a_side(self) -> None:
        """The prefix rule only works if admin CLI writers keep stamping
        ``cli_*``. AST-scan every literal ``attribution=`` keyword in
        ``src/`` and assert each is either an in-session tier value or
        prefixed — a new admin CLI op that invents
        ``attribution="acknowledge_foo"`` trips here rather than quietly
        inflating the published session count."""
        in_session_tiers = {"model", "hook", "auto"}
        src_root = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
        discovered: set[str] = set()
        for py_file in sorted(src_root.rglob("*.py")):
            for node in ast.walk(ast.parse(py_file.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if (
                        kw.arg == "attribution"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        discovered.add(kw.value.value)
        # Non-vacuity: the CLI writers this mechanism targets are present.
        assert "cli_acknowledge_debt" in discovered
        stray = {
            value
            for value in discovered
            if value not in in_session_tiers
            and not value.startswith(ADMIN_RECORDED_ATTRIBUTION_PREFIX)
        }
        assert not stray, (
            f"attribution value(s) {sorted(stray)} are neither an in-session "
            f"tier {sorted(in_session_tiers)} nor prefixed "
            f"{ADMIN_RECORDED_ATTRIBUTION_PREFIX!r}. If they come from an "
            f"admin/CLI surface running under a throwaway session id, "
            f"rename them to the prefix so `is_admin_recorded_event` "
            f"excludes them from the published session tally."
        )
