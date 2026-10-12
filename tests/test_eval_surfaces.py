"""The eval's recall surfaces section (D11).

`bettermemory eval` reads what the session brief and the prompt recall
showed, what the conversation then did with each pointer, the Stop's
settlements by surface, the cooldown's segments and the unanswered
reviews, all from the event log. The published trio keeps its
definitions: a `use` carrying a `surface` (a pointer's settlement) is
left out of it, and a log without the recall surface's events renders
every report body exactly as it did before the section existed (the
golden files under tests/fixtures/eval_surfaces/ were written by the
code before it).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bettermemory.models import Confidence, Memory, Source

FIXTURES = Path(__file__).parent / "fixtures" / "eval_surfaces"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SINCE = timedelta(days=30)

MEM_A = "01JAAAAAAAAAAAAAAAAAAAAAA1"
MEM_B = "01JAAAAAAAAAAAAAAAAAAAAAA2"
MEM_C = "01JAAAAAAAAAAAAAAAAAAAAAA3"


def _ts(days_ago: float, minutes: int = 0) -> str:
    return (NOW - timedelta(days=days_ago) + timedelta(minutes=minutes)).isoformat()


def _memory(memory_id: str, body: str, scopes: list[str]) -> Memory:
    stamp = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return Memory(
        id=memory_id,
        scopes=scopes,
        confidence=Confidence.HIGH,
        source=Source.EXPLICIT,
        body=body,
        created=stamp,
        updated=stamp,
    )


def legacy_memories() -> list[Memory]:
    return [
        _memory(MEM_A, "The deploy pipeline signs every release.", ["projects:foo"]),
        _memory(MEM_B, "Nightly builds publish coverage reports.", ["projects:foo"]),
        _memory(MEM_C, "The owner prefers short commit subjects.", ["workflow"]),
    ]


def legacy_log() -> list[dict[str, Any]]:
    """A log as a hook-wired 9.0 store wrote it before the recall surface:
    retrievals, every apply tier, audits, a miss and the old one-hit
    prompt recall's two lanes, in the window and before it."""
    events: list[dict[str, Any]] = []

    def ev(kind: str, ts: str, **fields: Any) -> None:
        events.append(
            {
                "ts": ts,
                "session": fields.pop("session", "sess_a"),
                "kind": kind,
                **fields,
            }
        )

    # Before the window: enough search deliveries of one memory to reach
    # the cold-endorsement floor, every apply auto.
    for i in range(31):
        ev("search", _ts(60, i), returned=[MEM_A], worktree_root="/w/foo")
    ev(
        "use",
        _ts(60, 40),
        ids=[MEM_A],
        outcome="applied",
        auto=True,
        attribution="auto",
        triggered_from="stop_hook",
        session="tr-old",
    )
    ev(
        "turn_audited",
        _ts(60, 41),
        session="tr-old",
        verdict="ok",
        threshold_rule="v1_top1_high",
        triggered_from="stop_hook",
        client_model="claude-test",
    )
    # In the window.
    ev("search", _ts(5), returned=[MEM_A, MEM_B], worktree_root="/w/foo")
    ev("list", _ts(5, 1), returned=[MEM_C])
    ev("show", _ts(5, 2), id=MEM_B, worktree_root="/w/foo")
    ev(
        "use",
        _ts(5, 3),
        ids=[MEM_B],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=["Nightly builds publish coverage reports."],
        triggered_from="stop_hook",
        session="tr-1",
    )
    ev(
        "use",
        _ts(5, 3),
        ids=[MEM_A],
        outcome="applied",
        auto=True,
        attribution="auto",
        triggered_from="stop_hook",
        session="tr-1",
    )
    ev(
        "use",
        _ts(5, 4),
        ids=[MEM_C],
        outcome="applied",
        auto=False,
        attribution="model",
        claim_excerpts=["short commit subjects"],
    )
    ev(
        "use",
        _ts(5, 5),
        ids=[MEM_A],
        outcome="ignored",
        auto=False,
        attribution="model",
    )
    ev(
        "prompt_recall",
        _ts(4),
        session="tr-2",
        event_id="01JBBBBBBBBBBBBBBBBBBBBBB1",
        session_id="tr-2",
        threshold_rule="v1_top1_high",
        top_hits=[{"id": MEM_A, "relevance": "high", "score": 3.1}],
        probe_mode="hybrid",
        injected_chars=210,
        triggered_from="prompt_hook",
        delivered_reason="miss",
    )
    ev(
        "prompt_recall",
        _ts(4, 30),
        session="tr-2",
        event_id="01JBBBBBBBBBBBBBBBBBBBBBB2",
        session_id="tr-2",
        threshold_rule="v1_top1_high",
        top_hits=[{"id": MEM_B, "relevance": "medium", "score": 2.0}],
        probe_mode="hybrid",
        injected_chars=190,
        triggered_from="prompt_hook",
        delivered_reason="project_cohort",
    )
    ev(
        "turn_audited",
        _ts(4, 1),
        session="tr-2",
        verdict="ok",
        threshold_rule="v1_top1_high",
        triggered_from="stop_hook",
        client_model="claude-test",
    )
    ev(
        "turn_audited",
        _ts(3),
        session="tr-2",
        verdict="no_signal",
        triggered_from="stop_hook",
        client_model="claude-test",
    )
    ev(
        "turn_audited",
        _ts(3, 1),
        session="tr-2",
        verdict="ok",
        repeat=True,
        triggered_from="stop_hook",
    )
    ev(
        "turn_audited",
        _ts(2),
        session="tr-3",
        verdict="miss",
        threshold_rule="v1_top1_high",
        triggered_from="stop_hook",
        client_model="claude-other",
    )
    ev(
        "search_miss",
        _ts(2),
        session="tr-3",
        session_id="tr-3",
        event_id="01JCCCCCCCCCCCCCCCCCCCCCC1",
        threshold_rule="v1_top1_high",
        top_hits=[{"id": MEM_C, "relevance": "high", "score": 4.0}],
        recent_retrieval_count=0,
        triggered_from="stop_hook",
        client_model="claude-other",
    )
    return events


def _render_all(memories: list[Memory], events: list[dict[str, Any]]) -> dict[str, str]:
    from bettermemory.eval import (
        compute_eval,
        compute_report,
        render_report_markdown,
        render_text,
    )

    window = compute_eval(memories, events, now=NOW, since=SINCE)
    alltime = compute_eval(memories, events, now=NOW, since=None)
    doc = compute_report(memories, events, now=NOW, since=SINCE, version="0-test")
    return {
        "window.txt": render_text(window),
        "alltime.txt": render_text(alltime),
        "window.json": json.dumps(window.to_dict(), indent=2, ensure_ascii=False)
        + "\n",
        "alltime.json": json.dumps(alltime.to_dict(), indent=2, ensure_ascii=False)
        + "\n",
        "report.md": render_report_markdown(doc),
    }


def _without_counts(report: Any) -> dict[str, Any]:
    out = dict(report.to_dict())
    for key in ("total_events_scanned", "events_in_window", "surfaces"):
        out.pop(key, None)
    return out


# ---------------------------------------------------------------------------
# Rows as the recall surface records them
# ---------------------------------------------------------------------------

CONV = "transcript-1"
SERVER = "sess_0123456789abcdef"
WORKTREE = "/w/foo"


def _brief(
    ts: str,
    ids: list[str],
    *,
    session: str = CONV,
    segment: int = 0,
    event_id: str = "01JDDDDDDDDDDDDDDDDDDDDDD1",
) -> dict[str, Any]:
    return {
        "ts": ts,
        "session": session,
        "kind": "session_brief",
        "event_id": event_id,
        "session_id": session,
        "harness": "claude-code",
        "source": "startup" if segment == 0 else "compact",
        "segment": segment,
        "ids": ids,
        "chars": 300,
        "rows_examined": 9,
        "unresolved": 0,
        "rule": "rs-v1",
        "triggered_from": "session_hook",
        "worktree_root": WORKTREE,
    }


def _recall(
    ts: str,
    ids: list[str],
    *,
    session: str = CONV,
    segment: int = 0,
    suppressed: list[dict[str, str]] | None = None,
    event_id: str = "01JDDDDDDDDDDDDDDDDDDDDDD2",
) -> dict[str, Any]:
    return {
        "ts": ts,
        "session": session,
        "kind": "prompt_recall",
        "event_id": event_id,
        "session_id": session,
        "top_hits": [{"id": i, "relevance": "high"} for i in ids],
        "probe_mode": "hybrid",
        "injected_chars": 400,
        "chars": 400,
        "triggered_from": "prompt_hook",
        "delivered_reason": "project",
        "shown_ids": ids,
        "suppressed_ids": suppressed or [],
        "rule": "rs-v1",
        "segment": segment,
        "harness": "claude-code",
        "worktree_root": WORKTREE,
    }


def _settle(ts: str, memory_id: str, surface: str, shown: str) -> dict[str, Any]:
    return {
        "ts": ts,
        "session": CONV,
        "kind": "use",
        "ids": [memory_id],
        "outcome": "applied",
        "auto": False,
        "attribution": "hook",
        "claim_excerpts": ["a sentence from the body"],
        "surface": surface,
        "shown_event_id": shown,
        "segment": 0,
        "harness": "claude-code",
        "triggered_from": "stop_hook",
        "worktree_root": WORKTREE,
    }


def _tool(
    kind: str,
    ts: str,
    *,
    session: str = SERVER,
    stamp: str | None = CONV,
    worktree: str | None = WORKTREE,
    **fields: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {"ts": ts, "session": session, "kind": kind, **fields}
    if stamp is not None:
        row["harness_session"] = stamp
    if worktree is not None:
        row["worktree_root"] = worktree
    return row


def _surfaces(events: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    from bettermemory.eval import compute_eval

    report = compute_eval(legacy_memories(), events, now=NOW, **kw)
    assert report.surfaces is not None
    return report.surfaces


# ---------------------------------------------------------------------------
# A log without the surface
# ---------------------------------------------------------------------------


def test_a_log_without_the_surface_renders_every_body_as_before() -> None:
    """The text, JSON and markdown bodies of a log holding no row of the
    recall surface (the old one-hit `prompt_recall` rows included) are
    the files the code before the section wrote."""
    rendered = _render_all(legacy_memories(), legacy_log())
    for name, text in rendered.items():
        assert text == (FIXTURES / name).read_text(), name
    assert "Recall surfaces" not in rendered["report.md"]


def test_the_old_one_hit_recall_is_not_a_show() -> None:
    from bettermemory.eval import compute_eval

    report = compute_eval(legacy_memories(), legacy_log(), now=NOW)
    assert report.surfaces is None
    assert "surfaces" not in report.to_dict()


def test_the_surface_leaves_the_trio_as_it_was() -> None:
    """Shows, settlements, segments, unanswered reviews and grades change
    no count of the trio: a settlement is a `use` carrying a `surface`,
    left out of applied_total, the helped numerator and the cold
    bucket, so the published series stays one."""
    from bettermemory.eval import compute_eval

    added = [
        _brief(_ts(1), [MEM_A, MEM_B]),
        _settle(_ts(1, 5), MEM_A, "brief", "01JDDDDDDDDDDDDDDDDDDDDDD1"),
        _recall(_ts(1, 10), [MEM_C]),
        _settle(_ts(1, 15), MEM_C, "recall", "01JDDDDDDDDDDDDDDDDDDDDDD2"),
        {
            "ts": _ts(1, 20),
            "session": CONV,
            "kind": "segment_opened",
            "segment": 1,
            "rule": "rs-v1",
        },
        {
            "ts": _ts(1, 21),
            "session": CONV,
            "kind": "review_unanswered",
            "count": 2,
            "root_ids": ["r1", "r2"],
        },
    ]
    base = legacy_log()
    for since in (SINCE, None):
        before = compute_eval(legacy_memories(), base, now=NOW, since=since)
        after = compute_eval(legacy_memories(), base + added, now=NOW, since=since)
        assert after.surfaces is not None
        assert _without_counts(after) == _without_counts(before)


def test_a_settlement_moves_no_rate_and_no_tool_call() -> None:
    """The slice 3 verifier's repro: one brief settlement used to move
    memory_helped_rate from 0/1 to 1/1, endorsement_rate from 0/1 to 1/2
    and the tool-call count from 2 to 3."""
    from bettermemory.eval import compute_report, compute_tool_usage

    base = [
        _tool("search", _ts(1), stamp=None, returned=["m1"]),
        _tool("use", _ts(1, 1), stamp=None, ids=["m1"], outcome="applied", auto=True),
    ]
    with_settlement = base + [_settle(_ts(1, 2), "m2", "brief", "e1")]
    for events in (base, with_settlement):
        alltime = compute_report([], events, version="t", now=NOW).alltime_eval
        assert (
            alltime.memory_helped_rate.numerator,
            alltime.memory_helped_rate.denominator,
        ) == (0, 1)
        assert (
            alltime.endorsement_rate.numerator,
            alltime.endorsement_rate.denominator,
        ) == (0, 1)
        assert (alltime.applied_hook, alltime.applied_total) == (0, 1)
        assert compute_tool_usage(events).total_tool_calls == 2
    report = compute_report([], with_settlement, version="t", now=NOW).alltime_eval
    assert report.surfaces is not None
    assert report.surfaces["settlements"] == {"brief": 1, "recall": 0}


# ---------------------------------------------------------------------------
# What the conversation did after a show
# ---------------------------------------------------------------------------


def test_opens_uses_and_ignores_after_a_show() -> None:
    events = [
        _brief(_ts(1), [MEM_A, MEM_B]),
        _tool("show", _ts(1, 2), id=MEM_A),
        _settle(_ts(1, 3), MEM_B, "brief", "01JDDDDDDDDDDDDDDDDDDDDDD1"),
        _recall(
            _ts(1, 10),
            [MEM_C, MEM_A],
            suppressed=[{"id": MEM_B, "reason": "seen_in_segment"}],
        ),
        _tool(
            "use",
            _ts(1, 11),
            ids=[MEM_C],
            outcome="ignored",
            auto=False,
            attribution="model",
        ),
        # The auto fallback and an excerpt-less apply are not uses.
        _tool(
            "use",
            _ts(1, 12),
            ids=[MEM_A],
            outcome="applied",
            auto=True,
            attribution="auto",
        ),
        _tool(
            "use",
            _ts(1, 13),
            ids=[MEM_A],
            outcome="applied",
            auto=False,
            attribution="model",
            claim_excerpts=[None],
        ),
    ]
    surfaces = _surfaces(events)
    brief = surfaces["brief"]
    assert (brief["deliveries"], brief["shown"], brief["opened"], brief["used"]) == (
        1,
        2,
        1,
        1,
    )
    assert brief["ignored"] == 0 and brief["deliveries_helped"] == 1
    assert brief["shown_opened_rate"]["rate"] == 0.5
    assert brief["delivery_helped_rate"]["rate"] == 1.0
    assert brief["chars_p50"] == 300
    assert "suppressed" not in brief
    recall = surfaces["recall"]
    # MEM_A's open came before the recall showed it: not after this show.
    assert (recall["shown"], recall["opened"], recall["used"], recall["ignored"]) == (
        2,
        0,
        0,
        1,
    )
    assert recall["deliveries_helped"] == 0
    assert recall["ignored_rate"]["rate"] == 0.5
    assert recall["suppressed"] == {"seen_in_segment": 1}
    assert surfaces["settlements"] == {"brief": 1, "recall": 0}
    assert surfaces["rules"] == ["rs-v1"]


def test_an_unstamped_row_joins_by_the_worktree_bridge() -> None:
    """With no harness stamp, a tool row joins the conversation whose hook
    row was the latest in its worktree before it; a conversation joined
    by stamps is never bridged, and a row in another worktree is not."""
    events = [
        _brief(_ts(1), [MEM_A, MEM_B, MEM_C]),
        _tool("show", _ts(1, 1), session="sess_bridge", stamp=None, id=MEM_A),
        _tool(
            "show",
            _ts(1, 2),
            session="sess_other",
            stamp=None,
            worktree="/w/bar",
            id=MEM_B,
        ),
    ]
    assert _surfaces(events)["brief"]["opened"] == 1
    stamped = events + [_tool("search", _ts(1, 3), returned=[MEM_C])]
    stamped.append(
        _tool("show", _ts(1, 4), session="sess_bridge", stamp=None, id=MEM_C)
    )
    # The search row stamps the conversation, so unstamped rows no longer
    # bridge to it, and the first open is lost with the second.
    assert _surfaces(stamped)["brief"]["opened"] == 0


def test_a_row_stamped_for_another_conversation_does_not_count() -> None:
    events = [
        _brief(_ts(1), [MEM_A]),
        _tool("show", _ts(1, 1), stamp="transcript-2", id=MEM_A),
    ]
    assert _surfaces(events)["brief"]["opened"] == 0


def test_rows_after_the_window_do_not_count() -> None:
    from bettermemory.eval import compute_eval

    events = [
        _brief(_ts(3), [MEM_A]),
        _tool("show", _ts(1), id=MEM_A),
    ]
    until = NOW - timedelta(days=2)
    report = compute_eval(legacy_memories(), events, now=NOW, since=SINCE, until=until)
    assert report.surfaces is not None
    assert report.surfaces["brief"]["shown"] == 1
    assert report.surfaces["brief"]["opened"] == 0
    assert _surfaces(events)["brief"]["opened"] == 1
    # A show before the window is not counted at all.
    late = compute_eval(legacy_memories(), events, now=NOW, since=timedelta(days=2))
    assert late.surfaces is None


def test_segments_unanswered_reviews_and_grades() -> None:
    events = [
        _brief(_ts(2), [MEM_A]),
        _recall(
            _ts(2, 5), [MEM_B], suppressed=[{"id": MEM_C, "reason": "segment_cap"}]
        ),
        _brief(_ts(1), [MEM_C], segment=1, event_id="01JDDDDDDDDDDDDDDDDDDDDDD3"),
        {
            "ts": _ts(1, 1),
            "session": "transcript-2",
            "kind": "segment_opened",
            "segment": 2,
            "rule": "rs-v1",
        },
        {
            "ts": _ts(1, 2),
            "session": CONV,
            "kind": "review_unanswered",
            "count": 2,
            "root_ids": ["r1", "r2"],
        },
        {
            "ts": _ts(1, 3),
            "session": CONV,
            "kind": "review_unanswered",
            "count": 1,
            "root_ids": ["r3"],
        },
        {
            "ts": _ts(0.5),
            "session": "cli",
            "kind": "recall_grade",
            "sample_id": "01JDDDDDDDDDDDDDDDDDDDDDD2",
            "stratum": "delivery",
            "relevant_ids": [MEM_B],
            "irrelevant_ids": [],
        },
        {
            "ts": _ts(0.4),
            "session": "cli",
            "kind": "recall_grade",
            "sample_id": "01JDDDDDDDDDDDDDDDDDDDDDD1",
            "stratum": "brief",
            "relevant_ids": [],
            "irrelevant_ids": [MEM_A],
        },
        # A later grade of the same id in the same sample replaces it.
        {
            "ts": _ts(0.3),
            "session": "cli",
            "kind": "recall_grade",
            "sample_id": "01JDDDDDDDDDDDDDDDDDDDDDD1",
            "stratum": "brief",
            "relevant_ids": [MEM_A],
            "irrelevant_ids": [],
        },
        {
            "ts": _ts(0.2),
            "session": "cli",
            "kind": "recall_grade",
            "sample_id": "silent-0123456789abcdef",
            "stratum": "silent",
            "relevant_ids": [MEM_C],
            "irrelevant_ids": [MEM_A],
        },
    ]
    surfaces = _surfaces(events)
    assert surfaces["segments"] == {"sessions": 2, "opened": 2}
    assert surfaces["recall"]["suppressed"] == {"segment_cap": 1}
    assert surfaces["review_unanswered"] == {"turns": 2, "chains": 3}
    grades = surfaces["grades"]
    assert (grades["delivery"]["graded"], grades["delivery"]["relevant"]) == (1, 1)
    assert (grades["brief"]["graded"], grades["brief"]["relevant"]) == (1, 1)
    assert (grades["silent"]["graded"], grades["silent"]["relevant"]) == (2, 1)
    assert grades["silent"]["relevant_rate"]["rate"] == 0.5


def test_review_unanswered_alone_opens_the_section() -> None:
    events = [
        {
            "ts": _ts(1),
            "session": CONV,
            "kind": "review_unanswered",
            "count": 1,
            "root_ids": ["r1"],
        }
    ]
    surfaces = _surfaces(events)
    assert surfaces["brief"]["deliveries"] == 0
    assert surfaces["brief"]["shown_opened_rate"]["rate"] is None
    assert surfaces["review_unanswered"] == {"turns": 1, "chains": 1}


def test_the_section_renders_in_text_json_and_markdown() -> None:
    from bettermemory.eval import (
        compute_eval,
        compute_report,
        render_report_markdown,
        render_text,
    )
    from bettermemory.recall_counters import SILENT_MISS_NOTE

    events = legacy_log() + [
        _brief(_ts(1), [MEM_A, MEM_B]),
        _tool("show", _ts(1, 2), id=MEM_A),
        _recall(_ts(1, 10), [MEM_C]),
        _settle(_ts(1, 11), MEM_C, "recall", "01JDDDDDDDDDDDDDDDDDDDDDD2"),
    ]
    report = compute_eval(legacy_memories(), events, now=NOW, since=SINCE)
    text = render_text(report)
    # The section follows every line the report printed before it.
    assert text.index("Threshold rule: v1_top1_high") < text.index("Recall surfaces")
    assert "Recall surfaces (rule rs-v1):" in text
    assert "  brief: deliveries=1 shown=2 opened=1 used=0 ignored=0 helped=1" in text
    assert "  recall: deliveries=1 shown=1 opened=0 used=1 ignored=0 helped=1" in text
    assert "    shown_opened_rate    0.50" in text
    assert "settled brief=0 recall=1" in text
    assert SILENT_MISS_NOTE in text
    assert json.loads(json.dumps(report.to_dict()))["surfaces"]["recall"]["used"] == 1

    doc = compute_report(
        legacy_memories(), events, now=NOW, since=SINCE, version="0-test"
    )
    markdown = render_report_markdown(doc)
    assert "## Recall surfaces" in markdown
    assert "| brief | `shown_opened_rate` | 1/2 = **0.50**" in markdown
    assert (
        "- last 30d: 1 brief · 1 recall delivery · 3 pointers shown · 1 pointer settled"
        in markdown
    )
    assert SILENT_MISS_NOTE in markdown
    assert markdown.index("## Recall surfaces") < markdown.index("## Threshold sweep")
    # Counts only: no memory id reaches the published report.
    for memory_id in (MEM_A, MEM_B, MEM_C):
        assert memory_id not in markdown


# ---------------------------------------------------------------------------
# Rows the real surface wrote
# ---------------------------------------------------------------------------


def test_a_real_delivery_and_its_settlement(tmp_path: Path, monkeypatch: Any) -> None:
    """The prompt recall delivers through the production path, the Stop
    settles the pointer against the reply, and the eval reads both."""
    from bettermemory import hook as hook_module
    from bettermemory.eval import compute_eval
    from bettermemory.hook import audit_transcript

    from .test_recall_prompt import PromptWorld

    world = PromptWorld(tmp_path)
    monkeypatch.setattr(hook_module, "capture_origin", lambda *a, **k: world.caller)
    sentence = (
        "The deploy pipeline signs every release artifact with the hardware "
        "key kept in the vault on the third floor."
    )
    memory = world.write(sentence)
    result = world.recall(
        "deploy pipeline release artifact hardware key", session_id=CONV
    )
    assert result.ids == (memory.id,)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": "ship"}}) + "\n"
    )
    audit_transcript(
        transcript_path=transcript,
        session_id=CONV,
        config=world.config,
        store=world.store,
        assistant_message=f"Done. {sentence}",
    )
    events = list(world.store.iter_events())
    report = compute_eval(world.store.load_all(), events)
    assert report.surfaces is not None
    recall = report.surfaces["recall"]
    assert (recall["deliveries"], recall["shown"], recall["used"]) == (1, 1, 1)
    assert report.surfaces["settlements"]["recall"] == 1
    assert report.applied_total == 0
