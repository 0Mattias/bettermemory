"""Tests for the instrument panel (bench/panel).

The panel's numbers set the baselines the next units are judged by, so
each reading is pinned on input whose answer is known: the store side
over a store the product itself wrote (its own recorder, its own
schema), the transcript side over hand-made transcripts in Claude
Code's line format. And the receipt is held to its promise: aggregates
only, no record id in it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bettermemory import standing
from bettermemory.models import Category, Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.store_schema import StandingAnswer

from ._event_helpers import EventLog

_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / "bench" / "panel" / "run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_panel_run", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_panel_run"] = module
    spec.loader.exec_module(module)
    return module


run = _load()
store_side: Any = run.store_side
transcripts: Any = run.transcripts

_SINCE = datetime(2026, 11, 4, tzinfo=timezone.utc)


# -- the store side -------------------------------------------------------


def _memory(created: datetime, category: Category | None = None) -> Memory:
    return Memory(
        id=generate_ulid(),
        scopes=["tools"],
        confidence=Confidence.HIGH,
        source=Source.EXPLICIT,
        body="the bench reads its store through a read-only copy",
        created=created,
        updated=created,
        category=category,
    )


@pytest.fixture
def written(
    store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """A store with three records and the events a week of use leaves."""
    monkeypatch.setattr(standing, "TAU_STANDING", 0.5)
    now = datetime.now(timezone.utc)
    old = store.put_memory(_memory(now - timedelta(days=120)))
    new = store.put_memory(_memory(now - timedelta(days=2)))
    idle = store.put_memory(_memory(now - timedelta(days=40)))
    store.put_standing(old.id, StandingAnswer(0.9, "asked", "test/test"))
    log = EventLog(store)
    log.emit("search", query="q", returned=[old.id, new.id], relevance=["high", "high"])
    log.emit("show", id=old.id, staleness_verdict="spot_check_required")
    log.emit("verify", id=old.id, note="held")
    log.emit("show", id=new.id, staleness_verdict="fresh")
    log.emit("update", id=new.id)
    log.emit(
        "use",
        ids=[old.id],
        outcome="applied",
        attribution="model",
        claim_excerpts=["the bench reads its store"],
    )
    return {
        "path": memory_dir / STORE_FILENAME,
        "old": old.id,
        "new": new.id,
        "idle": idle.id,
        "now": now,
    }


def _read(written: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = written["path"]
    conn = store_side.open_copy(path)
    try:
        sections, rows = store_side.read(
            conn, since=None, until=None, now=written["now"]
        )
    finally:
        conn.close()
    return sections, rows


def test_the_snapshot_copies_through_a_read_only_connection(
    written: dict[str, Any], tmp_path: Path
) -> None:
    """The copy holds the log; the source is opened mode=ro, and a copy is
    never overwritten."""
    source = written["path"]
    copy = store_side.snapshot(source, tmp_path / "copy" / "store.sqlite")
    conn = store_side.open_copy(copy)
    try:
        kinds = {ev["kind"] for ev in store_side.events(conn)}
    finally:
        conn.close()
    assert {"search", "show", "verify", "update", "use"} <= kinds
    with pytest.raises(FileExistsError):
        store_side.snapshot(source, copy)


def test_the_records_reading_counts_each_use_by_age_and_standing(
    written: dict[str, Any],
) -> None:
    sections, rows = _read(written)
    by_id = {row["id"]: row for row in rows}
    old, new, idle = (
        by_id[written["old"]],
        by_id[written["new"]],
        by_id[written["idle"]],
    )
    assert old["standing"] and not new["standing"]
    assert (
        old["searched"],
        old["opened"],
        old["applied"],
        old["applied_explicit"],
    ) == (
        1,
        1,
        1,
        1,
    )
    assert (new["searched"], new["opened"], new["applied"]) == (1, 1, 0)
    assert idle["searched"] + idle["opened"] + idle["applied"] == 0
    records = sections["records"]
    assert records["records"] == 3
    groups = {(g["age"], g["standing"]): g for g in records["groups"]}
    assert groups[("30-90d", False)]["never_retrieved"]["numerator"] == 1
    assert groups[("90d+", True)]["never_applied_explicitly"]["numerator"] == 0
    assert groups[("0-7d", False)]["never_applied_explicitly"]["numerator"] == 1


def test_the_verdict_reading_sets_flagged_shows_against_fresh_ones(
    written: dict[str, Any],
) -> None:
    """A verify after a flagged show is a check that found it held; an
    update after a fresh show counts for the fresh group, so the lift
    reads the flag against shows that carried none."""
    sections, _ = _read(written)
    verdicts = sections["verdicts"]
    assert verdicts["shows"] == 2
    assert verdicts["flagged"]["outcomes"] == {"verified": 1}
    assert verdicts["fresh"]["outcomes"] == {"updated": 1}
    assert verdicts["update_lift_flagged_over_fresh"] == 0.0


def test_the_eval_reading_is_the_products_own(written: dict[str, Any]) -> None:
    sections, _ = _read(written)
    helped = sections["eval"]["memory_helped_rate"]
    # Two search deliveries and two shows retrieved; one applied with an excerpt.
    assert (helped["numerator"], helped["denominator"]) == (1, 4)


def test_the_pointer_reading_ranks_each_settlement_in_its_delivery() -> None:
    evs = [
        {
            "kind": "prompt_recall",
            "event_id": "e1",
            "shown_ids": ["A", "B"],
            "ts": "2026-11-05T00:00:00Z",
        },
        {
            "kind": "session_brief",
            "event_id": "e2",
            "ids": ["C"],
            "ts": "2026-11-05T00:00:00Z",
        },
        {
            "kind": "use",
            "surface": "recall",
            "shown_event_id": "e1",
            "ids": ["B"],
            "outcome": "applied",
            "ts": "2026-11-05T00:01:00Z",
        },
        {
            "kind": "use",
            "surface": "recall",
            "shown_event_id": "e1",
            "ids": ["A"],
            "outcome": "unused",
            "ts": "2026-11-05T00:01:00Z",
        },
        {
            "kind": "use",
            "surface": "brief",
            "shown_event_id": "e2",
            "ids": ["C"],
            "outcome": "unused",
            "ts": "2026-11-05T00:01:00Z",
        },
        {
            "kind": "use",
            "surface": "brief",
            "shown_event_id": "gone",
            "ids": ["C"],
            "outcome": "unused",
            "ts": "2026-11-05T00:01:00Z",
        },
    ]
    out = store_side.pointer_section(evs, since=_SINCE, until=None)
    assert out["recall"]["1"]["outcomes"] == {"unused": 1}
    assert out["recall"]["2"]["applied"]["rate"] == 1.0
    assert out["brief"]["1"]["settled"] == 1
    assert out["unmatched_settlements"] == 1


# -- the transcript side --------------------------------------------------


def _line(**entry: Any) -> str:
    return json.dumps(entry) + "\n"


def _use(use_id: str, name: str, ts: str = "2026-11-05T10:00:00Z") -> str:
    return _line(
        type="assistant",
        sessionId="s1",
        timestamp=ts,
        message={
            "content": [{"type": "tool_use", "id": use_id, "name": name, "input": {}}]
        },
    )


def _result(use_id: str, text: str, ts: str = "2026-11-05T10:00:01Z") -> str:
    return _line(
        type="user",
        sessionId="s1",
        timestamp=ts,
        message={
            "content": [{"type": "tool_result", "tool_use_id": use_id, "content": text}]
        },
    )


def _hook(uuid: str, event: str, command: str, content: str, ms: int) -> str:
    return _line(
        type="attachment",
        uuid=uuid,
        sessionId="s1",
        timestamp="2026-11-05T10:00:02Z",
        attachment={
            "type": "hook_success",
            "hookName": event,
            "hookEvent": event.split(":")[0],
            "command": command,
            "content": content,
            "durationMs": ms,
        },
    )


@pytest.fixture
def transcript_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    (root / "p").mkdir(parents=True)
    write = json.dumps({"status": "review"})
    done = json.dumps({"status": "committed", "id": "X"})
    lines = [
        # The result arrives before its use is read (a resumed session).
        _result("t0", "s" * 50),
        _use("t0", "mcp__bettermemory__memory_search"),
        _use("t1", "mcp__plugin_bettermemory_bettermemory__memory_search"),
        _result("t1", "s" * 150),
        _use("t2", "Read"),
        _result("t2", "r" * 1000),
        _use("w1", "mcp__bettermemory__memory_write"),
        _result("w1", write),
        _use("w2", "mcp__bettermemory__memory_write"),
        _result("w2", done),
        # Before the window: not counted.
        _use("old", "mcp__bettermemory__memory_show", ts="2026-11-01T00:00:00Z"),
        _result("old", "o" * 999, ts="2026-11-01T00:00:01Z"),
        _hook(
            "h1",
            "UserPromptSubmit",
            "x bettermemory hook prompt || true",
            "bettermemory recall: " + "p" * 79,
            300,
        ),
        # The desktop app records the command as a placeholder.
        _hook(
            "h2",
            "UserPromptSubmit",
            "command hook 1",
            "bettermemory recall: " + "p" * 19,
            100,
        ),
        _hook(
            "h3", "UserPromptSubmit", "command hook 1", "Session status updated.", 50
        ),
        _hook(
            "h4",
            "Stop",
            "uvx bettermemory hook stop --quiet; exit 0",
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": "bettermemory: 2 unanswered",
                    }
                }
            ),
            600,
        ),
    ]
    (root / "p" / "a.jsonl").write_text("".join(lines), encoding="utf-8")
    # A fork copies the same lines into a second file: counted once.
    (root / "p" / "fork.jsonl").write_text("".join(lines), encoding="utf-8")
    # A file last written before the window is not opened.
    stale = root / "p" / "stale.jsonl"
    stale.write_text(
        _use("z", "mcp__bettermemory__memory_show") + _result("z", "z" * 5),
        encoding="utf-8",
    )
    before = _SINCE.timestamp() - 86400
    os.utime(stale, (before, before))
    return root


def test_the_transcripts_count_each_call_once_in_characters(
    transcript_root: Path,
) -> None:
    out = transcripts.read(transcript_root, since=_SINCE)
    assert out["files"]["read"] == 2
    assert out["files"]["skipped_older_than_window"] == 1
    search = out["tool_responses"]["memory_search"]
    assert (search["n"], search["total"], search["p50"]) == (2, 200, 50)
    assert "memory_show" not in out["tool_responses"]
    writes = out["writes"]["memory_write"]
    assert (writes["calls"], writes["committed"], writes["calls_per_committed"]) == (
        2,
        1,
        2.0,
    )
    assert writes["statuses"] == {"review": 1, "committed": 1}
    assert "token" in out["unit"]


def test_the_transcripts_attribute_hooks_by_command_or_by_their_output(
    transcript_root: Path,
) -> None:
    out = transcripts.read(transcript_root, since=_SINCE)
    prompt = out["hooks"]["prompt (UserPromptSubmit)"]
    assert prompt["injected_chars"]["n"] == 2
    assert prompt["injected_chars"]["total"] == 100 + 40
    assert sorted([prompt["wall_ms"]["p50"], prompt["wall_ms"]["max"]]) == [
        100.0,
        300.0,
    ]
    stop = out["hooks"]["stop (Stop)"]
    assert stop["injected_chars"]["total"] == len("bettermemory: 2 unanswered")
    assert out["hook_launchers"] == {"installed": 1, "unknown": 1, "uvx": 1}
    assert out["other_hooks_wall_ms"]["UserPromptSubmit (other output)"]["n"] == 1


def test_the_share_is_of_the_context_tools_and_hooks_added(
    transcript_root: Path,
) -> None:
    out = transcripts.read(transcript_root, since=_SINCE)
    sessions = out["sessions"]
    bm_tools = (
        200
        + len(json.dumps({"status": "review"}))
        + len(json.dumps({"status": "committed", "id": "X"}))
    )
    bm_hooks = 100 + 40 + len("bettermemory: 2 unanswered")
    assert sessions["bettermemory_tool_result_chars"] == bm_tools
    assert sessions["bettermemory_hook_chars"] == bm_hooks
    assert sessions["tool_result_chars_all_tools"] == bm_tools + 1000
    assert sessions["bettermemory_share"] == round(
        (bm_tools + bm_hooks) / (bm_tools + 1000 + bm_hooks), 4
    )


# -- the receipt ----------------------------------------------------------


def test_the_receipt_names_no_record_and_the_rows_stay_out_of_the_repository(
    written: dict[str, Any], transcript_root: Path, tmp_path: Path
) -> None:
    path = written["path"]
    out = tmp_path / "receipt.json"
    private = tmp_path / "private"
    code = run.main(
        [
            "--store",
            str(path),
            "--transcripts",
            str(transcript_root),
            "--since",
            "2026-11-04",
            "--out",
            str(out),
            "--private-out",
            str(private),
        ]
    )
    assert code == 0
    text = out.read_text(encoding="utf-8")
    for key in ("old", "new", "idle"):
        assert written[key] not in text
    receipt = json.loads(text)
    assert receipt["kind"] == "instrument-panel"
    assert set(receipt["latency"]) >= {"x1", "x10", "x100"}
    rows = json.loads((private / "records.json").read_text(encoding="utf-8"))
    assert {row["id"] for row in rows} == {
        written["old"],
        written["new"],
        written["idle"],
    }
    with pytest.raises(SystemExit):
        run.main(["--store", str(path), "--no-transcripts", "--out", str(out)])
    with pytest.raises(SystemExit):
        run.main(
            [
                "--store",
                str(path),
                "--no-transcripts",
                "--private-out",
                str(_ROOT / "x"),
            ]
        )
