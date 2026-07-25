"""Tests for the 3.14 measurement layer.

Covers, in one place, the feature set that ships together:

- shadow relevance label v2 (`search._relevance_label_v2`) and the
  `query_unique` coverage denominator threaded onto `MemoryHit`;
- per-turn calibration features on `turn_audited` (probe_query /
  compact top_hits) and the enriched `MissHit`;
- re-audit dedup (`audit.is_duplicate_audit`, the `repeat` flag, and
  its exclusion from eval/health denominators);
- transcript-derived model attribution (`client_model`);
- end-of-turn use settlement: hook attribution + auto-fallback split,
  and the wall-clock floor that keeps the in-process auto-commit from
  racing the hook;
- the `pending_writes` count on memory_scope_overview;
- the eval `--widening-preview` replay lane and its `--detail`
  precision-labeling surface (`compute_widening_detail`).

Response-shape guards live here too: the shadow fields must NEVER
appear in an MCP response — they are event-log-only calibration data.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.audit import (
    ATTRIBUTION_LOOKBACK_SECONDS,
    MissHit,
    MissReport,
    THRESHOLD_RULE_V1,
    is_duplicate_audit,
    probe_for_miss,
    search_miss_fields,
    turn_audited_fields,
)
from bettermemory.config import Config, StorageConfig
from bettermemory.eval import (
    compute_eval,
    compute_threshold_sweep,
    compute_widening_detail,
    compute_widening_preview,
    render_widening_detail_text,
    render_widening_preview_text,
)
from bettermemory.events import Recorder, iter_events, redact_query
from bettermemory.health import compute_health
from bettermemory.hook import _extract_last_exchange, main as hook_main, run_audit
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.search import (
    _V2_HIGH_MATCHED_FLOOR,
    _relevance_label,
    _relevance_label_v2,
    search,
)
from bettermemory.server import build_server
from bettermemory.session import (
    AUTO_COMMIT_MIN_AGE_SECONDS,
    PendingUseToken,
    SessionState,
)
from bettermemory.store import Store


def _memory(
    body: str,
    scopes: list[str] | None = None,
    *,
    created: datetime | None = None,
) -> Memory:
    now = created or datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=scopes or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body,
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Shadow relevance label v2
# ---------------------------------------------------------------------------


def test_v2_floor_promotes_long_query_to_high() -> None:
    """The motivating case: a long natural-language query whose coverage
    fraction lands v1 at 'medium' even though it matched the absolute
    floor's worth of distinct content tokens."""
    assert _relevance_label(4, 9) == "medium"
    assert _relevance_label_v2(4, 9) == "high"
    assert _relevance_label(5, 8) == "medium"
    assert _relevance_label_v2(5, 8) == "high"


def test_v2_high_arm_degenerates_as_the_query_grows() -> None:
    """Why v2 stays shadow-only — the arithmetic behind the dogfood
    measurement in `_relevance_label_v2`'s docstring, pinned hermetically
    so the verdict survives without that private event log.

    Hold the EVIDENCE fixed at exactly the floor and let only the query
    grow. v1 decays the way a coverage fraction should: the same four
    matched tokens are worth steadily less as the question gets longer.
    v2 returns "high" at every length, including a 60-token query where
    four matches is 7% coverage — so past the floor its high arm no
    longer carries information about the query at all.

    That is the whole finding. On real turns the floor is not hard to
    reach (four distinct content tokens landing SOMEWHERE in a
    185-document store is near-certain for any long message), so a
    v2-driven miss rule fires on message length rather than on
    relevance: 100% of turns whose user message ran past 150 characters.
    The fix direction is the conjunction, asserted below — `and` makes
    the floor corroborate the fraction, where `or` lets it overrule it.
    """
    floor = _V2_HIGH_MATCHED_FLOOR
    for query_unique in (floor, 8, 16, 30, 60):
        assert _relevance_label_v2(floor, query_unique) == "high", query_unique
    assert _relevance_label(floor, floor) == "high"
    assert _relevance_label(floor, 8) == "medium"
    assert _relevance_label(floor, 60) == "low"

    # The conjunctive candidate keeps the floor's evidence requirement
    # without letting it manufacture a "high" out of a long query.
    def conjunctive(matched: int, query: int) -> bool:
        return bool(query) and matched / query >= 0.75 and matched >= floor

    assert conjunctive(floor, floor)
    assert not conjunctive(floor, 60)
    # It is strictly NARROWER than v1, not wider: a tiny fully-covered
    # query ("restic backup" matching 2/2) clears v1 on coverage alone
    # but carries less evidence than the floor asks for.
    assert _relevance_label(2, 2) == "high"
    assert not conjunctive(2, 2)


def test_v2_matches_v1_below_the_floor() -> None:
    """Below the matched-count floor the two formulas are identical —
    the v2 change is ONLY the absolute floor on the high arm."""
    for matched in range(0, _V2_HIGH_MATCHED_FLOOR):
        for query in range(0, 13):
            assert _relevance_label_v2(matched, query) == _relevance_label(
                matched, query
            ), (matched, query)


def test_v2_is_a_strict_widening_of_v1() -> None:
    """v1-high implies v2-high, and v2 never demotes: anything not
    promoted to high keeps its exact v1 label. This property is what
    makes the widening-preview delta interpretable."""
    for matched in range(0, 13):
        for query in range(0, 13):
            v1 = _relevance_label(matched, query)
            v2 = _relevance_label_v2(matched, query)
            if v1 == "high":
                assert v2 == "high", (matched, query)
            if v2 != "high":
                assert v2 == v1, (matched, query)


def test_v2_floor_cross_pinned_to_attribution_containment() -> None:
    """The v2 high-arm floor and the attribution containment floor
    answer the same question ("how many distinct content-token overlaps
    constitute a deliberate connection?") and are deliberately the same
    value. Not an import edge — a pin, so a future recalibration of one
    has to consciously decide about the other."""
    from bettermemory.attribution import _MIN_CONTAINMENT_TOKENS

    assert _V2_HIGH_MATCHED_FLOOR == _MIN_CONTAINMENT_TOKENS


def test_search_hits_carry_query_unique() -> None:
    mem = _memory("alpha beta gamma delta epsilon body text")
    hits = search([mem], "alpha beta gamma", max_results=5)
    assert hits
    assert all(h.query_unique == 3 for h in hits)


def test_browse_mode_hits_carry_zero_query_unique() -> None:
    mem = _memory("alpha beta gamma")
    hits = search([mem], "", max_results=5, allow_empty_query=True)
    assert hits
    assert all(h.query_unique == 0 for h in hits)


# ---------------------------------------------------------------------------
# MissHit / turn_audited calibration features
# ---------------------------------------------------------------------------


def _old_memory(body: str) -> Memory:
    """A memory old enough to clear the probe's creation shield."""
    return _memory(body, created=datetime.now(timezone.utc) - timedelta(hours=2))


def test_probe_populates_coverage_features() -> None:
    """The probe's MissHit carries the raw coverage pair and the shadow
    label; the long-query blind-spot case reads medium/v1, high/v2."""
    mem = _old_memory(
        "The kubernetes ingress lives on the staging cluster; "
        "deployment happens through the blue pipeline."
    )
    message = (
        "where does the kubernetes ingress for the staging cluster "
        "live in our deployment pipeline exactly, remind me please"
    )
    report = probe_for_miss(
        [mem],
        message,
        recent_events=[],
        session_id="sess-features",
    )
    assert report.top_hits, report
    top = report.top_hits[0]
    assert top.matched_unique >= _V2_HIGH_MATCHED_FLOOR
    assert top.query_unique > top.matched_unique
    assert top.relevance == "medium"
    assert top.relevance_v2 == "high"
    full = top.to_dict()
    for key in ("matched_unique", "query_unique", "relevance_v2", "snippet", "scopes"):
        assert key in full
    compact = top.to_compact_dict()
    assert set(compact) == {
        "id",
        "score",
        "relevance",
        "relevance_v2",
        "matched_unique",
        "query_unique",
    }


def _report_with_hits(now: datetime) -> MissReport:
    return MissReport(
        verdict="ok",
        checked_at=now,
        session_id="sess-1",
        lookback_seconds=60,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=(
            MissHit(
                id="mem-1",
                score=1.5,
                relevance="medium",
                scopes=("tools",),
                snippet="snippet text",
                matched_unique=4,
                query_unique=8,
                relevance_v2="high",
            ),
        ),
        probe_query="a long natural language question",
    )


def test_turn_audited_fields_carry_calibration_payload() -> None:
    now = datetime.now(timezone.utc)
    fields = turn_audited_fields(
        _report_with_hits(now),
        session_id="sess-1",
        probe_mode="hybrid",
        assistant_present=True,
        triggered_from="stop_hook",
        repeat=True,
        client_model="claude-sonnet-5",
    )
    assert fields["probe_query"] == "a long natural language question"
    assert fields["repeat"] is True
    assert fields["client_model"] == "claude-sonnet-5"
    (hit,) = fields["top_hits"]
    assert hit["relevance_v2"] == "high"
    assert hit["matched_unique"] == 4
    assert hit["query_unique"] == 8
    assert "snippet" not in hit and "scopes" not in hit


def test_turn_audited_fields_omit_additive_keys_when_absent() -> None:
    """Events from turns with no probe payload keep the exact legacy
    shape — the additive fields must not appear as None/False noise."""
    now = datetime.now(timezone.utc)
    bare = MissReport(
        verdict="no_signal",
        checked_at=now,
        session_id="sess-1",
        lookback_seconds=60,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
    )
    fields = turn_audited_fields(
        bare,
        session_id="sess-1",
        probe_mode="hybrid",
        assistant_present=False,
        triggered_from="mcp_tool",
    )
    for key in ("probe_query", "top_hits", "repeat", "client_model"):
        assert key not in fields


def test_search_miss_fields_carry_client_model_when_known() -> None:
    now = datetime.now(timezone.utc)
    with_model = search_miss_fields(
        _report_with_hits(now),
        session_id="sess-1",
        triggered_from="stop_hook",
        client_model="claude-fable-5",
    )
    assert with_model["client_model"] == "claude-fable-5"
    without = search_miss_fields(
        _report_with_hits(now), session_id="sess-1", triggered_from="stop_hook"
    )
    assert "client_model" not in without


# ---------------------------------------------------------------------------
# Re-audit dedup
# ---------------------------------------------------------------------------


def _audited(
    *,
    session: str,
    ts: datetime,
    probe_query: Any,
    kind: str = "turn_audited",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "session_id": session,
        "ts": _iso(ts),
        "probe_query": probe_query,
    }


def test_duplicate_detected_via_redacted_hash() -> None:
    now = datetime.now(timezone.utc)
    message = "nice, push and update, the works"
    events = [
        _audited(
            session="s1",
            ts=now - timedelta(minutes=10),
            probe_query=redact_query(message),
        )
    ]
    assert is_duplicate_audit(
        events,
        session_id="s1",
        probe_query_hash=redact_query(message)["hash"],
        probe_query_text=message,
        now=now,
    )


def test_duplicate_detected_via_verbatim_text() -> None:
    now = datetime.now(timezone.utc)
    message = "verbatim logged message"
    events = [
        _audited(session="s1", ts=now - timedelta(minutes=5), probe_query=message)
    ]
    assert is_duplicate_audit(
        events,
        session_id="s1",
        probe_query_hash=redact_query(message)["hash"],
        probe_query_text=message,
        now=now,
    )


def test_no_duplicate_across_sessions_windows_or_messages() -> None:
    now = datetime.now(timezone.utc)
    message = "same message text here"
    kwargs: dict[str, Any] = {
        "session_id": "s1",
        "probe_query_hash": redact_query(message)["hash"],
        "probe_query_text": message,
        "now": now,
    }
    other_session = [
        _audited(
            session="s2",
            ts=now - timedelta(minutes=5),
            probe_query=redact_query(message),
        )
    ]
    assert not is_duplicate_audit(other_session, **kwargs)
    outside_window = [
        _audited(
            session="s1", ts=now - timedelta(hours=2), probe_query=redact_query(message)
        )
    ]
    assert not is_duplicate_audit(outside_window, **kwargs)
    different_message = [
        _audited(
            session="s1",
            ts=now - timedelta(minutes=5),
            probe_query=redact_query("a different message entirely"),
        )
    ]
    assert not is_duplicate_audit(different_message, **kwargs)
    legacy_no_query = [
        {"kind": "turn_audited", "session_id": "s1", "ts": _iso(now)},
    ]
    assert not is_duplicate_audit(legacy_no_query, **kwargs)
    wrong_kind = [
        _audited(
            session="s1",
            ts=now - timedelta(minutes=5),
            probe_query=redact_query(message),
            kind="search",
        )
    ]
    assert not is_duplicate_audit(wrong_kind, **kwargs)


# ---------------------------------------------------------------------------
# Hook: model extraction, dedup wiring, settlement split
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, *rows: dict[str, object]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_extract_last_exchange_captures_model(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "the question"}},
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "the answer"}],
            },
        },
    )
    user, assistant, model = _extract_last_exchange(transcript)
    assert user == "the question"
    assert assistant == "the answer"
    assert model == "claude-sonnet-5"


def test_extract_model_from_tool_use_only_stop(tmp_path: Path) -> None:
    """A turn can stop on a tool-use-only assistant row (no text
    blocks). The model id still comes from the NEWEST assistant row;
    the response text falls back to the older text-bearing row."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "do the thing"}},
        {
            "type": "assistant",
            "message": {
                "model": "claude-old-model",
                "content": [{"type": "text", "text": "working on it"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-fable-5",
                "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
            },
        },
    )
    user, assistant, model = _extract_last_exchange(transcript)
    assert user == "do the thing"
    assert assistant == "working on it"
    assert model == "claude-fable-5"


def test_run_audit_dedups_repeats_and_stamps_model(tmp_path: Path) -> None:
    """Two audits of the same (session, message) inside the dedup
    window: the second records `repeat=True` and both carry the
    transcript-derived `client_model` stamp."""
    mem_dir = tmp_path / "mem"
    store = Store(mem_dir)
    store.write(
        content="kubernetes ingress staging cluster deployment pipeline notes",
        scopes=["infrastructure"],
    )
    cfg = Config(storage=StorageConfig(directory=str(mem_dir)))
    message = "where does the kubernetes ingress staging cluster live"
    for _ in range(2):
        run_audit(
            user_message=message,
            assistant_response="answered inline",
            session_id="cc-transcript-1",
            client_model="claude-sonnet-5",
            config=cfg,
        )
    audits = [e for e in iter_events(mem_dir) if e["kind"] == "turn_audited"]
    assert len(audits) == 2
    first, second = audits
    assert "repeat" not in first
    assert second["repeat"] is True
    assert first["client_model"] == "claude-sonnet-5"
    assert second["client_model"] == "claude-sonnet-5"
    # Redaction: the probe_query lands as {hash, preview, len}, never raw.
    assert isinstance(first["probe_query"], dict)
    assert first["probe_query"]["hash"] == redact_query(message)["hash"]
    # A miss (if flagged at all) is never emitted twice for a repeat.
    misses = [e for e in iter_events(mem_dir) if e["kind"] == "search_miss"]
    assert len(misses) <= 1


def test_hook_settlement_splits_attribution_and_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reply quotes memory A verbatim and never touches memory B: the
    hook emits one hook-attributed event for A and one auto-fallback
    event for B, both stamped with the transcript model."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    quoted = store.write(
        content="The staging ingress terminates TLS at the haproxy edge node.",
        scopes=["infrastructure"],
    )
    unquoted = store.write(
        content="Database migrations for the billing service run from the cron box.",
        scopes=["infrastructure"],
    )
    Recorder(root=mem_dir, session_id="sess-split").record(
        "search",
        query="seed",
        scopes_filter=None,
        max_results=5,
        returned=[quoted.id, unquoted.id],
    )

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "how is staging TLS set up?"}},
        {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-5",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Per your infra notes: the staging ingress "
                            "terminates TLS at the haproxy edge node."
                        ),
                    }
                ],
            },
        },
    )
    code = hook_main(
        ["--transcript-path", str(transcript), "--session-id", "sess-split", "--quiet"]
    )
    assert code == 0

    use_events = [e for e in iter_events(mem_dir) if e["kind"] == "use"]
    hook_events = [e for e in use_events if e.get("attribution") == "hook"]
    auto_events = [e for e in use_events if e.get("attribution") == "auto"]
    assert len(hook_events) == 1
    assert hook_events[0]["ids"] == [quoted.id]
    assert hook_events[0]["client_model"] == "claude-sonnet-5"
    assert len(auto_events) == 1
    assert auto_events[0]["ids"] == [unquoted.id]
    assert auto_events[0]["auto"] is True
    assert auto_events[0]["client_model"] == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# Session: wall-clock floor on the in-process auto-commit
# ---------------------------------------------------------------------------


def test_consume_holds_turn_old_but_wall_young_tokens() -> None:
    """The race fix itself: a token issued seconds ago is NOT consumed
    no matter how many handler entries have advanced the turn counter —
    the Stop hook gets first claim at turn end."""
    state = SessionState()
    state.issue_use_tokens(["m1"])
    for _ in range(5):
        state.advance_turn()
    assert state.consume_old_tokens() == []
    assert "m1" in state.pending_use_tokens


def test_consume_fires_when_both_axes_old() -> None:
    state = SessionState()
    state.pending_use_tokens["m1"] = PendingUseToken(
        token="use_x",
        memory_id="m1",
        issued_at=time.time() - AUTO_COMMIT_MIN_AGE_SECONDS - 60,
        issued_at_turn=0,
    )
    state.turn_counter = 5
    assert state.consume_old_tokens() == ["m1"]
    assert state.pending_use_tokens == {}


def test_consume_min_age_zero_restores_turn_only_behavior() -> None:
    state = SessionState()
    state.issue_use_tokens(["m1"])
    for _ in range(3):
        state.advance_turn()
    assert state.consume_old_tokens(min_age_seconds=0) == ["m1"]


def test_auto_commit_floor_cross_pinned_to_attribution_window() -> None:
    """session.py deliberately doesn't import the audit stack; the two
    constants describe the same turn-settlement window and are pinned
    here instead."""
    assert AUTO_COMMIT_MIN_AGE_SECONDS == float(ATTRIBUTION_LOOKBACK_SECONDS)


# ---------------------------------------------------------------------------
# Eval: repeat exclusion, per-model slices, widening preview
# ---------------------------------------------------------------------------


def _ev(
    kind: str, ts: str = "2026-05-15T12:00:00.000+00:00", **fields: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "ts": ts, "session": "sess-A"}
    out.update(fields)
    return out


def test_eval_excludes_repeat_audits_from_denominators() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev("turn_audited", verdict="ok"),
        _ev("turn_audited", verdict="ok", repeat=True),
        _ev("turn_audited", verdict="miss", repeat=True),
    ]
    report = compute_eval(memories=[], events=events, now=now)
    assert report.turns_audited == 1
    assert report.repeat_audits == 2


def test_eval_buckets_by_client_model() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev("turn_audited", verdict="ok", client_model="claude-sonnet-5"),
        _ev("turn_audited", verdict="no_signal", client_model="claude-sonnet-5"),
        _ev(
            "search_miss",
            client_model="claude-sonnet-5",
            top_hits=[{"id": "mem-A", "relevance": "high"}],
        ),
        _ev("turn_audited", verdict="ok"),  # no model — must not bucket
    ]
    report = compute_eval(memories=[], events=events, now=now)
    assert report.by_model == {
        "claude-sonnet-5": {"audited": 1, "no_signal": 1, "misses": 1}
    }
    assert report.turns_audited == 2


def _hit(relevance: str, relevance_v2: str) -> dict[str, Any]:
    return {
        "id": "mem-A",
        "score": 1.0,
        "relevance": relevance,
        "relevance_v2": relevance_v2,
        "matched_unique": 4,
        "query_unique": 8,
    }


def test_widening_preview_counts_and_deltas() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        # v1 and w1 both flag.
        _ev(
            "turn_audited",
            verdict="miss",
            recent_retrieval_count=0,
            top_hits=[_hit("high", "high")],
        ),
        # The blind-spot cohort: only the widened rule flags.
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit("medium", "high")],
        ),
        # Neither flags.
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit("medium", "medium")],
        ),
        # Shielded: a retrieval happened — neither flags.
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=2,
            top_hits=[_hit("high", "high")],
        ),
        # Excluded rows.
        _ev("turn_audited", verdict="ok", repeat=True, top_hits=[_hit("high", "high")]),
        _ev("turn_audited", verdict="no_signal"),
        _ev("turn_audited", verdict="ok"),  # pre-3.14: no top_hits
        _ev("search", returned=[]),
    ]
    report = compute_widening_preview(events, now=now)
    assert report.audits_with_features == 4
    assert report.audits_without_features == 1
    assert report.repeat_audits_skipped == 1
    assert report.v1_baseline_flagged == 1
    rows_by_rule = {r.rule: r for r in report.rows}
    assert set(rows_by_rule) == {"w1_top1_v2_high", "w2_top1_v2_high_from_medium"}
    w1 = rows_by_rule["w1_top1_v2_high"]
    assert w1.would_flag == 2
    assert w1.delta_from_v1 == 1
    # On this stream the medium→high promotion is the only widening,
    # so w2 (v1 arm + medium promotions) agrees with w1.
    w2 = rows_by_rule["w2_top1_v2_high_from_medium"]
    assert w2.would_flag == 2
    assert w2.delta_from_v1 == 1
    text = render_widening_preview_text(report)
    assert "w1_top1_v2_high" in text
    assert "w2_top1_v2_high_from_medium" in text
    assert "v1 baseline" in text


def test_widening_w2_excludes_low_promotions() -> None:
    """The four-quadrant contract from the 2026-07-08 labeling pass:
    w1 flags every v2-high top hit; w2 keeps the v1-high arm and the
    medium→high promotions but drops the low→high ones (the ~20%%-
    precision cohort: long messages crossing the matched floor at
    dilute coverage)."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        # v1-high: both rules flag (w2 via its replayed v1 arm).
        _ev(
            "turn_audited",
            verdict="miss",
            recent_retrieval_count=0,
            top_hits=[_hit("high", "high")],
        ),
        # medium→high promotion: both rules flag.
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit("medium", "high")],
        ),
        # low→high promotion: ONLY w1 flags — the cohort w2 exists to drop.
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit("low", "high")],
        ),
        # No promotion anywhere: neither flags.
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit("medium", "medium")],
        ),
    ]
    report = compute_widening_preview(events, now=now)
    rows_by_rule = {r.rule: r for r in report.rows}
    assert rows_by_rule["w1_top1_v2_high"].would_flag == 3
    assert rows_by_rule["w2_top1_v2_high_from_medium"].would_flag == 2
    assert report.v1_baseline_flagged == 1


def test_widening_preview_render_empty_state() -> None:
    report = compute_widening_preview([], now=datetime.now(timezone.utc))
    text = render_widening_preview_text(report)
    assert "No replayable audited turns yet" in text


def _hit_for(memory_id: str, relevance: str, relevance_v2: str) -> dict[str, Any]:
    return {
        "id": memory_id,
        "score": 0.25,
        "relevance": relevance,
        "relevance_v2": relevance_v2,
        "matched_unique": 5,
        "query_unique": 40,
    }


def test_widening_detail_rows_rollup_and_lockstep() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    mem_a = _memory("Alpha memory body line.\n\nMore prose.", ["projects:x"])
    tombstoned_id = generate_ulid()
    events = [
        # Flagged, redacted probe_query shape; v1 does NOT also flag.
        _ev(
            "turn_audited",
            ts="2026-05-15T12:00:00.000+00:00",
            session_id="s1",
            verdict="ok",
            recent_retrieval_count=0,
            probe_query={
                "hash": "abcd1234",
                "preview": "how do I frobnicate the",
                "len": 90,
            },
            top_hits=[_hit_for(mem_a.id, "medium", "high")],
        ),
        # Flagged, same memory, different session, verbatim probe_query;
        # v1 ALSO flags (relevance high).
        _ev(
            "turn_audited",
            ts="2026-05-16T12:00:00.000+00:00",
            session_id="s2",
            verdict="miss",
            recent_retrieval_count=0,
            probe_query="verbatim logged message",
            top_hits=[_hit_for(mem_a.id, "high", "high")],
        ),
        # Flagged against a tombstoned memory.
        _ev(
            "turn_audited",
            ts="2026-05-17T12:00:00.000+00:00",
            session_id="s3",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit_for(tombstoned_id, "medium", "high")],
        ),
        # Not flagged (v2 medium) — must not appear.
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit_for(mem_a.id, "medium", "medium")],
        ),
    ]
    report = compute_widening_detail(
        events,
        memories=[mem_a],
        tombstoned_ids={tombstoned_id},
        now=now,
    )
    preview = compute_widening_preview(events, now=now)

    # Lockstep: the detail lane's counters and flag totals must agree
    # with the counting lane over the same stream.
    assert report.audits_with_features == preview.audits_with_features == 4
    assert report.v1_baseline_flagged == preview.v1_baseline_flagged == 1
    detail = next(r for r in report.rules if r.rule == "w1_top1_v2_high")
    row = next(r for r in preview.rows if r.rule == "w1_top1_v2_high")
    assert detail.flagged_total == row.would_flag == 3
    assert detail.beyond_v1 == row.delta_from_v1 == 2

    # Turns are newest-first and carry the logged evidence.
    assert [t.session_id for t in detail.turns] == ["s3", "s2", "s1"]
    by_session = {t.session_id: t for t in detail.turns}
    redacted = by_session["s1"]
    assert redacted.probe_query_preview == "how do I frobnicate the"
    assert redacted.probe_query_len == 90
    assert redacted.probe_query_hash == "abcd1234"
    assert redacted.v1_also_flagged is False
    assert redacted.memory_status == "active"
    assert redacted.memory_summary == "Alpha memory body line"
    assert redacted.memory_scopes == ["projects:x"]
    assert redacted.matched_unique == 5 and redacted.query_unique == 40
    verbatim = by_session["s2"]
    assert verbatim.probe_query_preview == "verbatim logged message"
    assert verbatim.probe_query_len == len("verbatim logged message")
    assert verbatim.probe_query_hash is None
    assert verbatim.v1_also_flagged is True
    ghost = by_session["s3"]
    assert ghost.memory_status == "tombstoned"
    assert ghost.memory_summary is None

    # Rollup: concentration on mem_a (2 flags, 2 sessions) sorts first.
    assert [r.memory_id for r in detail.by_memory] == [mem_a.id, tombstoned_id]
    top_row = detail.by_memory[0]
    assert top_row.count == 2
    assert top_row.distinct_sessions == 2
    assert top_row.status == "active"
    assert top_row.summary == "Alpha memory body line"


def test_widening_detail_render_text() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    mem = _memory("Render target memory.", ["tools"])
    events = [
        _ev(
            "turn_audited",
            session_id="s1",
            verdict="ok",
            recent_retrieval_count=0,
            probe_query={
                "hash": "beef",
                "preview": "long query preview text",
                "len": 200,
            },
            top_hits=[_hit_for(mem.id, "medium", "high")],
        ),
    ]
    report = compute_widening_detail(events, memories=[mem], now=now)
    text = render_widening_detail_text(report)
    assert "w1_top1_v2_high — 1 flagged, 1 beyond v1" in text
    assert "by top-hit memory (1 distinct)" in text
    assert "Render target memory" in text
    assert "long query preview text" in text
    assert "(+177 chars)" in text  # 200 - len(preview)
    assert "cov   5/40" in text
    assert mem.id in text  # rollup shows the full id


def test_widening_detail_render_empty_state() -> None:
    report = compute_widening_detail([], now=datetime.now(timezone.utc))
    text = render_widening_detail_text(report)
    assert "No replayable audited turns yet" in text


def test_widening_detail_unknown_memory_without_resolver() -> None:
    """Omitting memories/tombstones degrades to status="unknown" —
    the compute layer must not require a store."""
    events = [
        _ev(
            "turn_audited",
            verdict="ok",
            recent_retrieval_count=0,
            top_hits=[_hit_for("01JUNKID000000000000000000", "medium", "high")],
        ),
    ]
    report = compute_widening_detail(events, now=datetime.now(timezone.utc))
    detail = next(r for r in report.rules if r.rule == "w1_top1_v2_high")
    assert detail.turns[0].memory_status == "unknown"
    assert detail.turns[0].probe_query_preview is None  # no probe_query logged


def test_widening_lanes_survive_poison_top_hit_element() -> None:
    """One hand-edited `top_hits=["junk"]` row — a non-dict ELEMENT in an
    otherwise well-formed list — must not take down either widening lane.

    Pre-fix, `_collect_replayable_audits` validated only that `top_hits`
    was a non-empty list, so the poison row reached `top_hits[0].get(...)`
    in `_rule_v1_top1_high` / every `WIDENING_RULES` check and in the
    detail lane's evidence read, killing both `compute_widening_preview`
    and `compute_widening_detail` with AttributeError even alongside good
    rows. The choke-point element guard buckets the poison row as
    feature-less while the good row still replays. The event log is
    plaintext + hand-editable — the same poison class events.py hardened
    the always-on paths against."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        # A good v1+w1-flagging audit that must still be replayed.
        _ev(
            "turn_audited",
            verdict="miss",
            recent_retrieval_count=0,
            top_hits=[_hit("high", "high")],
        ),
        # The poison: a non-empty top_hits list whose first (only) entry
        # is a bare string, not the expected hit dict.
        _ev("turn_audited", verdict="miss", top_hits=["junk"]),
    ]

    # Counting lane: survives, replays the good row, and buckets the
    # poison row as feature-less rather than crashing.
    preview = compute_widening_preview(events, now=now)
    assert preview.audits_with_features == 1
    assert preview.audits_without_features == 1
    assert preview.v1_baseline_flagged == 1
    w1_preview = next(r for r in preview.rows if r.rule == "w1_top1_v2_high")
    assert w1_preview.would_flag == 1

    # Detail lane: survives with header counters in lockstep with the
    # counting lane, and materialises exactly the one good flagged turn.
    detail = compute_widening_detail(events, now=now)
    assert detail.audits_with_features == 1
    assert detail.audits_without_features == 1
    assert detail.v1_baseline_flagged == 1
    w1_detail = next(r for r in detail.rules if r.rule == "w1_top1_v2_high")
    assert w1_detail.flagged_total == 1


# ---------------------------------------------------------------------------
# Threshold sweep: same poison-element threat model, separate collection walk
# ---------------------------------------------------------------------------
#
# `eval --threshold-sweep` runs through `compute_threshold_sweep`, a
# DIFFERENT function from the widening lanes' `_collect_replayable_audits`,
# with its own collection walk that validated only `isinstance(top_hits,
# list)`. The same hand-edited-log poison class therefore reached the rule
# predicates here too — and the v3 dominance rule reads the SECOND hit
# (`top_hits[1]`), so a single top-hit guard is insufficient for this lane.


def _sweep_hit(score: float = 100.0, relevance: str = "high") -> dict[str, Any]:
    """A canonical `search_miss` top-hit dict the strict-sweep rules read
    (`relevance` gates v1; `score` gates v2/v3)."""
    return {"id": "mem-A", "score": score, "relevance": relevance}


def test_threshold_sweep_survives_poison_top_hit_element() -> None:
    """One hand-edited `top_hits=["junk"]` search_miss — a non-dict FIRST
    element — must not take down `--threshold-sweep`.

    Pre-fix, `compute_threshold_sweep`'s walk validated only that
    `top_hits` was a list, so the poison row entered the replay set and
    `_rule_v1_top1_high` detonated at `top_hits[0].get("relevance")` with
    AttributeError — killing the whole sweep even alongside good rows. The
    choke-point element guard buckets the poison row into the observable
    `skipped_legacy_event_count` footnote (mirroring how the widening lane
    counts a non-dict top hit as feature-less) while the good row replays."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev("search_miss", recent_retrieval_count=0, top_hits=[_sweep_hit()]),
        # The poison: a non-empty top_hits list whose only entry is a bare
        # string, not the expected hit dict.
        _ev("search_miss", recent_retrieval_count=0, top_hits=["junk"]),
    ]

    report = compute_threshold_sweep(events, now=now)

    # Completed without raising; the good row still replays.
    assert report.replayable_misses == 1
    v1 = next(r for r in report.rows if r.rule == "v1_top1_high")
    assert v1.would_flag == 1
    # The poison row is accounted for, not silently dropped.
    assert report.skipped_legacy_event_count == 1
    assert report.total_events_scanned == 2


def test_threshold_sweep_survives_poison_second_top_hit_element() -> None:
    """A MIXED `top_hits=[{valid high hit}, "junk"]` search_miss — a
    perfectly valid top hit followed by a non-dict SECOND element — must
    not take down `--threshold-sweep`.

    This is the case a single `top_hits[0]` guard would MISS: v1/v2 read
    only the top hit and are fine, but `_rule_v3_top1_high_dominant`
    reaches `top_hits[1].get("score")` and detonated with AttributeError
    pre-fix — so the guard has to cover index 0 AND index 1. The good row
    (which every rule flags) still replays; the poison row is bucketed as
    a skipped row rather than crashing the run."""
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev("search_miss", recent_retrieval_count=0, top_hits=[_sweep_hit()]),
        # The poison: a valid high-scoring top hit, then a bare string as
        # the second element — only the v3 dominance rule reads it.
        _ev(
            "search_miss",
            recent_retrieval_count=0,
            top_hits=[_sweep_hit(), "junk"],
        ),
    ]

    report = compute_threshold_sweep(events, now=now)

    # Completed without raising; the good row still replays under every
    # rule, including the v3 lane whose second-hit read was the crash site.
    assert report.replayable_misses == 1
    v1 = next(r for r in report.rows if r.rule == "v1_top1_high")
    assert v1.would_flag == 1
    v3 = next(r for r in report.rows if r.rule == "v3_top1_high_dominant")
    assert v3.would_flag == 1
    # The poison row is accounted for, not silently dropped.
    assert report.skipped_legacy_event_count == 1
    assert report.total_events_scanned == 2


# ---------------------------------------------------------------------------
# Health: repeat exclusion
# ---------------------------------------------------------------------------


def test_health_excludes_repeat_audits_from_audited_total() -> None:
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    events = [
        _ev("turn_audited", verdict="ok"),
        _ev("turn_audited", verdict="ok", repeat=True),
        _ev("turn_audited", verdict="no_signal", repeat=True),
    ]
    report = compute_health([], events, now=now)
    assert report.silent_misses.audited_total == 1
    assert report.silent_misses.no_signal_total == 0


# ---------------------------------------------------------------------------
# Server-level: pending_writes surface, shadow-field leak guards, dedup
# ---------------------------------------------------------------------------


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


@pytest.fixture
def confirming_server(memory_dir: Path) -> tuple[Any, SessionState]:
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(require_write_confirmation=True),
    )
    state = SessionState()
    return build_server(config=cfg, store=Store(memory_dir), state=state), state


@pytest.fixture
def plain_server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=Store(memory_dir), state=SessionState())


async def test_scope_overview_counts_pending_writes(
    confirming_server: tuple[Any, SessionState],
) -> None:
    server, _state = confirming_server
    staged = await _call(
        server,
        "memory_write",
        content="a durable fact awaiting explicit confirmation",
        scopes=["tools"],
    )
    assert staged["status"] == "pending"
    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert overview["pending_writes"] == 1
    confirmed = await _call(
        server, "memory_write_confirm", pending_id=staged["pending_id"]
    )
    assert confirmed["status"] == "committed"
    overview = await _call(server, "memory_scope_overview", auto_scope=False)
    assert overview["pending_writes"] == 0


async def test_search_event_carries_shadow_features_response_does_not(
    plain_server: Any, memory_dir: Path
) -> None:
    """The calibration features land on the `search` EVENT; the MCP
    response stays shadow-free — surfacing relevance_v2 live would
    nudge model behavior before the calibration justifies a flip."""
    await _call(
        plain_server,
        "memory_write",
        content="alpha beta gamma delta epsilon reference body",
        scopes=["tools"],
    )
    hits = await _call(
        plain_server, "memory_search", query="alpha beta", auto_scope=False
    )
    hit_list = hits["result"] if isinstance(hits, dict) else hits
    assert hit_list
    for hit in hit_list:
        assert "relevance_v2" not in hit
        assert "query_unique" not in hit

    search_events = [e for e in iter_events(memory_dir) if e["kind"] == "search"]
    assert search_events
    ev = search_events[-1]
    assert ev["query_unique"] == 2
    assert ev["relevance_v2"] == [
        _relevance_label_v2(count, ev["query_unique"]) for count in ev["match_counts"]
    ]
    assert len(ev["scores"]) == len(ev["returned"])
    assert len(ev["match_counts"]) == len(ev["returned"])


async def test_audit_turn_handler_dedups_repeats(
    plain_server: Any, memory_dir: Path
) -> None:
    # A non-empty store is required for a dedup anchor: on an empty
    # store the probe aborts before `probe_query` is set, so the first
    # audit event carries nothing to match a repeat against.
    await _call(
        plain_server,
        "memory_write",
        content="kubernetes ingress staging cluster reference notes",
        scopes=["infrastructure"],
    )
    message = "kubernetes ingress staging cluster question"
    for _ in range(2):
        await _call(plain_server, "memory_audit_turn", user_message=message)
    audits = [e for e in iter_events(memory_dir) if e["kind"] == "turn_audited"]
    assert len(audits) == 2
    assert "repeat" not in audits[0]
    assert audits[1]["repeat"] is True
