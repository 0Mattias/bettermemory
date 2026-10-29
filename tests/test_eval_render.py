"""The eval renderers escape what the store and the log hand them (ENG-168).

`render_text`, `render_widening_detail_text` and `render_tool_usage_text`
print a summary, a scope, a `client_model` stamp, a probe preview, a
session id and the rules and reasons rows carried through
`history.printable`, so an escape sequence or a line break in one cannot
run in the terminal or split a line. `eval --report` writes the one
log-sourced string it prints, the model name, through `_md_escape_cell`,
so a pipe, a line break or inline markdown in it cannot leave its table
cell or ride into the published document.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from bettermemory.eval import (
    ColdEndorsementMemoriesRow,
    SilentMissCandidate,
    WideningDetailReport,
    WideningFlaggedTurn,
    WideningMemoryRollup,
    WideningRuleDetail,
    compute_eval,
    compute_report,
    compute_tool_usage,
    render_report_markdown,
    render_text,
    render_tool_usage_text,
    render_widening_detail_text,
)
from bettermemory.eval_render import _md_escape_cell

#: An OSC 52 clipboard write ended by BEL, a screen clear, a carriage
#: return, a line feed and a pipe.
HOSTILE = "\x1b]52;c;cHduZWQ=\x07|\x1b[2J\rover\nwritten"
ESCAPED = "\\x1b]52;c;cHduZWQ=\\x07|\\x1b[2J\\rover\\nwritten"
NOW = datetime(2026, 5, 20, tzinfo=timezone.utc)


def _control_chars(text: str) -> set[str]:
    """The C0 and DEL characters `text` holds, a line feed and a tab apart:
    the output's own line breaks and its one kept whitespace."""
    return {
        ch for ch in text if (ord(ch) < 0x20 or ord(ch) == 0x7F) and ch not in "\n\t"
    }


def _events(text: str) -> list[dict[str, Any]]:
    """A log whose audit rows carry `text` in every string a renderer
    prints: the model stamp, the rule, the session, the missed id and its
    relevance, plus a kind no tool maps, and a recall show whose rule and
    held-back reason carry it, so the surfaces section prints."""
    ts = "2026-05-15T12:00:00+00:00"
    return [
        {
            "ts": ts,
            "session": "s-recall",
            "kind": "prompt_recall",
            "ids": ["01AAAAAAAAAAAAAAAAAAAAAAAA"],
            "rule": f"rule{text}",
            "suppressed_ids": [
                {"id": "01AAAAAAAAAAAAAAAAAAAAAAAB", "reason": f"reason{text}"}
            ],
        },
        {
            "ts": ts,
            "session": f"sess{text}",
            "kind": "turn_audited",
            "verdict": "miss",
            "client_model": f"model{text}",
            "threshold_rule": f"rule{text}",
        },
        {
            "ts": ts,
            "session": f"sess{text}",
            "kind": "search_miss",
            "client_model": f"model{text}",
            "threshold_rule": f"rule{text}",
            "top_hits": [{"id": f"id{text}", "relevance": f"rel{text}"}],
        },
        {"ts": ts, "session": "s", "kind": f"kind{text}"},
    ]


def _rate_text(text: str) -> str:
    report = compute_eval([], _events(text), now=NOW, since=timedelta(days=30))
    assert report.surfaces is not None, "the recall show must open the section"
    # The review section prints once a chain was issued; its names are the
    # logged review actions, filing detectors and client stamps.
    review = report.review
    assert review is not None, "the review section must be present"
    review["chains"]["issued"] = 1
    review["actions"][f"act{text}"] = 1
    review["filings_per_100_committed"][f"det{text}"] = 50.0
    review["by_client"][f"client{text}"] = {
        "issued": 1,
        "unanswered": 0,
        "pre_answered_zero": 0,
    }
    report.scope_filter = f"scope{text}"
    report.cold_endorsement_memories_total = 1
    report.cold_endorsement_memories_rows = [
        ColdEndorsementMemoriesRow(
            id=f"id{text}",
            scopes=[f"scope{text}"],
            summary=f"sum{text}",
            retrieval_count=40,
            auto_applied_count=3,
            explicit_applied_count=0,
        )
    ]
    report.silent_miss_recent.append(
        SilentMissCandidate(
            ts=f"2026-05-15T12:00{text}",
            session_id=f"s{text}",
            top_missed_id=f"id{text}",
            top_missed_relevance=f"rel{text}",
            threshold_rule=None,
            recent_retrieval_count=0,
        )
    )
    return render_text(report)


def test_render_text_escapes_store_and_log_strings() -> None:
    text = _rate_text(HOSTILE)
    assert _control_chars(text) == set()
    for field in (
        f"model{ESCAPED}",
        f"rule{ESCAPED}",
        f"scope{ESCAPED}",
        f"id{ESCAPED}",
        f"act{ESCAPED}",
        f"det{ESCAPED}",
        f"client{ESCAPED}",
        f"reason{ESCAPED}",
    ):
        assert field in text, field
    assert "Write review" in text and "Recall surfaces" in text
    assert f"rel{ESCAPED}" in text
    assert len(text.splitlines()) == len(_rate_text("plain").splitlines())


def _detail_text(text: str) -> str:
    turn = WideningFlaggedTurn(
        ts=f"2026-05-15T12:00{text}",
        session_id=f"s{text}",
        client_model=f"model{text}",
        probe_query_preview=f"query{text}",
        probe_query_len=None,
        probe_query_hash=None,
        top_hit_id=f"i{text}",
        top_hit_score=0.5,
        relevance_v1=f"rel{text}",
        relevance_v2=None,
        matched_unique=1,
        query_unique=2,
        v1_also_flagged=False,
        memory_status="active",
        memory_summary=f"sum{text}",
        memory_scopes=[f"scope{text}"],
    )
    rollup = WideningMemoryRollup(
        memory_id=f"id{text}",
        count=1,
        distinct_sessions=1,
        status=f"st{text}",
        summary=f"sum{text}",
        scopes=[f"sc{text}"],
        score_min=0.5,
        score_max=0.5,
    )
    report = WideningDetailReport(
        generated_at=NOW,
        window_seconds=None,
        total_events_scanned=1,
        events_in_window=1,
        audits_with_features=1,
        audits_without_features=0,
        repeat_audits_skipped=0,
        v1_baseline_flagged=0,
        rules=[
            WideningRuleDetail(
                rule="w1_top1_v2_high",
                description="static",
                flagged_total=1,
                beyond_v1=1,
                turns=[turn],
                by_memory=[rollup],
            )
        ],
    )
    return render_widening_detail_text(report)


def test_render_widening_detail_text_escapes_store_and_log_strings() -> None:
    text = _detail_text(HOSTILE)
    assert _control_chars(text) == set()
    for field in (
        f"id{ESCAPED}",
        f"query{ESCAPED}"[:40],
        f"rel{ESCAPED}",
        f"sum{ESCAPED}"[:30],
        f"sc{ESCAPED}"[:30],
    ):
        assert field in text, field
    assert len(text.splitlines()) == len(_detail_text("plain").splitlines())


def test_render_tool_usage_text_escapes_an_unmapped_kind() -> None:
    report = compute_tool_usage(events=_events(HOSTILE), since=None, now=NOW)
    text = render_tool_usage_text(report)
    assert _control_chars(text) == set()
    assert f"kind{ESCAPED}" in text


def _table_rows(markdown: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith("|")]


def _cells(row: str) -> int:
    """The cell count of one table row: its separators not escaped."""
    return len(re.findall(r"(?<!\\)\|", row.replace("\\\\", ""))) - 1


def test_report_markdown_keeps_a_hostile_model_name_in_its_cell() -> None:
    """A `client_model` stamp holding a pipe, a line break, control
    characters and inline markdown: no control character reaches the
    document, every table row keeps its table's column count, and the
    stamp's markdown is escaped text."""
    model = f"m{HOSTILE}\r\n# heading `code` [link](http://x) ![i](http://x) <b>&amp; *e* _u_ ~s~ \\"
    events = _events("x")
    for ev in events[:2]:
        ev["client_model"] = model
    doc = compute_report([], events, since=None, now=NOW, version="0-test")
    markdown = render_report_markdown(doc)
    assert _control_chars(markdown) == set()
    assert "\n# heading" not in markdown
    model_rows = [row for row in _table_rows(markdown) if row.startswith("| m\\x1b")]
    assert len(model_rows) == 1
    header = next(r for r in _table_rows(markdown) if r.startswith("| model "))
    assert _cells(model_rows[0]) == _cells(header) == 4
    row = model_rows[0]
    for literal in ("\\`code\\`", "\\[link\\]", "\\!\\[i\\]", "\\<b\\>", "\\&amp;"):
        assert literal in row, literal
    assert "\\*e\\*" in row and "\\_u\\_" in row and "\\~s\\~" in row
    assert row.count("\\x1b") == 2 and "\\x07" in row and "\\r\\n" in row


def test_md_escape_cell_table() -> None:
    assert _md_escape_cell("claude-opus-4.5") == "claude-opus-4.5"
    assert _md_escape_cell("a|b") == "a\\|b"
    assert _md_escape_cell("a\\|b") == "a\\\\\\|b"
    assert _md_escape_cell("a\nb") == "a\\nb"
    assert _md_escape_cell("a\x1bb") == "a\\x1bb"
    assert _md_escape_cell("a\tb") == "a\tb"
