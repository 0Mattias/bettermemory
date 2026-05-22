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
    RateCI,
    _wilson_interval,
    compute_eval,
    parse_since,
    render_text,
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
        }


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
