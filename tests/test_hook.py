"""Tests for `bettermemory.hook` — the Claude Code Stop hook entry
point that fires the silent-miss audit at end-of-turn.

The MCP `memory_audit_turn` tool was originally the only invocation
surface; in practice models forget to call it and the silent-miss
telemetry stayed dormant for plugin users. This module is the
client-side hook that closes that loop. Tests here cover:

- transcript-JSONL parsing: walking the file in reverse to find the
  latest user message and the latest assistant response, defensive
  against malformed lines.
- the hook's contract that errors must NEVER surface as non-zero
  exit codes (Claude Code's Stop hook would render that as an error
  banner; we'd rather lose telemetry than break the user's flow).
- end-to-end: write a memory, fake a transcript, run the hook,
  assert events landed in the log.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.events import Recorder, iter_events
from bettermemory.hook import (
    _disabled_scopes_from_events,
    _extract_last_exchange,
    _flatten_assistant_content,
    _latest_in_process_session,
    _pending_retrievals,
    _read_payload,
    _render_recall_block,
    main as hook_main,
    prompt_main,
    run_audit,
    run_prompt_recall,
)
from bettermemory.store import Store


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def test_read_payload_parses_well_formed_json() -> None:
    payload = _read_payload('{"session_id": "abc", "transcript_path": "/x"}')
    assert payload == {"session_id": "abc", "transcript_path": "/x"}


def test_read_payload_tolerates_whitespace_and_newlines() -> None:
    payload = _read_payload('  {"a": 1}\n\n')
    assert payload == {"a": 1}


def test_read_payload_returns_empty_on_invalid_json() -> None:
    """A malformed payload mustn't crash the hook. The dispatcher
    treats an empty dict as 'nothing to audit' and exits 0."""
    assert _read_payload("not json") == {}
    assert _read_payload("") == {}
    assert _read_payload("   ") == {}


def test_read_payload_returns_empty_on_non_dict_root() -> None:
    """A JSON list at the root isn't a Stop hook payload — treat the
    same as malformed."""
    assert _read_payload("[1, 2, 3]") == {}
    assert _read_payload('"just a string"') == {}


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, *rows: dict[str, object]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_extract_last_exchange_finds_last_pair(tmp_path: Path) -> None:
    """The standard case: a user message followed by an assistant
    response. Should pull both out cleanly."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "first ask"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "first reply"}]},
        },
        {"type": "user", "message": {"content": "latest ask"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "latest reply"}]},
        },
    )
    user, assistant, _ = _extract_last_exchange(transcript)
    assert user == "latest ask"
    assert assistant == "latest reply"


def test_extract_last_exchange_skips_thinking_and_tool_use(
    tmp_path: Path,
) -> None:
    """Assistant `content` is a list of blocks. Only `text` blocks
    contribute to the surface the audit looks at — thinking and
    tool_use are internal."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "ask"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                    {"type": "text", "text": "user-visible reply"},
                ]
            },
        },
    )
    _, assistant, _ = _extract_last_exchange(transcript)
    assert assistant == "user-visible reply"


def test_extract_last_exchange_handles_missing_file(tmp_path: Path) -> None:
    """A transcript path that doesn't exist must return (None, None)
    rather than raising. The hook handles the None case by no-oping."""
    user, assistant, _ = _extract_last_exchange(tmp_path / "missing.jsonl")
    assert user is None and assistant is None


def test_extract_last_exchange_tolerates_malformed_lines(tmp_path: Path) -> None:
    """A single bad JSON line in the middle of the transcript must
    not abort the whole parse. The hook fires for every turn — one
    corrupted log can't shadow every subsequent audit."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "first"}}),
                "this line is not valid json",
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "reply"}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    user, assistant, _ = _extract_last_exchange(transcript)
    assert user == "first"
    assert assistant == "reply"


def test_extract_last_exchange_skips_task_notification_row(tmp_path: Path) -> None:
    """Regression: background-task completions inject a `type="user"` row
    whose content is a `<task-notification>` envelope AFTER the human's
    message. The reverse walk used to capture that synthetic payload as
    the user message, so the probe audited notification text and real
    silent misses on agentic turns were structurally suppressed. The
    walk must skip it and keep going to the human row."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "message": {"content": "how do we deploy myapp to staging again?"},
        },
        {
            "type": "user",
            "message": {
                "content": (
                    "<task-notification>task wf_1 finished"
                    "<status>completed</status></task-notification>"
                )
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "deployed"}]},
        },
    )
    user, assistant, _ = _extract_last_exchange(transcript)
    assert user == "how do we deploy myapp to staging again?"
    assert assistant == "deployed"


def test_extract_last_exchange_skips_meta_and_wrapper_rows(tmp_path: Path) -> None:
    """Regression: slash-command turns record the command bookkeeping
    (`<command-name>` wrapper rows) and the full skill expansion (an
    `isMeta: true` row) as `type="user"` rows ABOVE the assistant reply.
    The walk used to return the expansion's documentation prose as "the
    user's own words" — violating the proposals extractor's stated
    precondition and shadowing whatever the human actually typed. Both
    shapes must be skipped so the genuine row wins."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "/plugin-marketplace docs"}},
        {
            "type": "user",
            "isMeta": True,
            "message": {
                "content": (
                    "Useful for enterprise administrators to add "
                    'organization-specific context. User: "Format my code '
                    'after Claude writes it".'
                )
            },
        },
        {
            "type": "user",
            "message": {"content": "<command-name>/plugin-marketplace</command-name>"},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "docs shown"}]},
        },
    )
    user, assistant, _ = _extract_last_exchange(transcript)
    assert user == "/plugin-marketplace docs"
    assert assistant == "docs shown"


def test_extract_last_exchange_skips_empty_user_rows(tmp_path: Path) -> None:
    """An empty-string user row (observed in real transcripts) used to be
    captured verbatim, and main()'s `if not user` then silently dropped
    the whole audit. The walk must keep going to the human row instead."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "real question"}},
        {"type": "user", "message": {"content": ""}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "answer"}]},
        },
    )
    user, _, _ = _extract_last_exchange(transcript)
    assert user == "real question"


def test_extract_last_exchange_none_when_only_synthetic_rows(tmp_path: Path) -> None:
    """Fail-quiet contract preserved: when NOTHING human-looking is in the
    tail, the user surface stays None (the hook then no-ops exactly as it
    does for a missing user message) — skipping must not invent one."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "message": {"content": "<system-reminder>reminder</system-reminder>"},
        },
        {
            "type": "user",
            "isMeta": True,
            "message": {"content": "expanded skill markdown"},
        },
    )
    user, assistant, _ = _extract_last_exchange(transcript)
    assert user is None
    assert assistant is None


def test_flatten_assistant_content_returns_none_on_no_text() -> None:
    """An assistant turn with only thinking + tool_use returns None
    for the assistant surface — the audit can still proceed on user
    message alone."""
    assert (
        _flatten_assistant_content(
            [
                {"type": "thinking", "thinking": "..."},
                {"type": "tool_use", "name": "Bash", "input": {}},
            ]
        )
        is None
    )


# ---------------------------------------------------------------------------
# CLI entry point — end-to-end
# ---------------------------------------------------------------------------


class _StdinMock:
    """Minimal stdin double exposing a `.buffer` for binary reads.

    The hook now reads via `sys.stdin.buffer` (binary) so the
    2.6.4 byte-cap can apply; tests need to swap that out without
    importing the full real-stdin machinery.
    """

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def test_main_no_op_when_payload_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty stdin payload: nothing to do. Must exit 0 and write
    nothing to the event log."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    monkeypatch.setattr("sys.stdin", _StdinMock(b""))
    code = hook_main(["--quiet"])
    assert code == 0
    assert not (tmp_path / ".events.jsonl").exists()


def test_main_no_op_when_stdin_oversized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression for the 2.6.4 stdin byte-cap fix.

    Pre-2.6.4 the hook called `sys.stdin.read()` with no cap; a
    misbehaving upstream piping GB of bytes would buffer the whole
    thing into memory before `json.loads` could reject. The fix
    caps via `bounded_stream_read` and treats oversized payloads as
    "malformed input — silent no-op" so the contract that the hook
    never breaks the turn end stays intact.
    """
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    # 128 KiB of garbage — 2× the 64 KiB cap.
    monkeypatch.setattr("sys.stdin", _StdinMock(b"x" * (128 * 1024)))
    code = hook_main(["--quiet"])
    # Must NOT raise; must exit 0 quietly.
    assert code == 0
    # Oversized payload is treated as "nothing to audit" — no events
    # land in the log because we didn't reach the audit branch.
    assert not (tmp_path / ".events.jsonl").exists()


def test_main_no_op_when_user_message_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Transcript exists but has no user message — exit 0 silently.
    Forcing the audit anyway would produce a "no_signal" verdict
    that's noise in the log."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "alone"}]},
        },
    )
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "mem"))
    code = hook_main(
        [
            "--transcript-path",
            str(transcript),
            "--session-id",
            "sess",
            "--quiet",
        ]
    )
    assert code == 0
    assert not (tmp_path / "mem" / ".events.jsonl").exists()


def test_main_runs_audit_and_logs_turn_audited(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end happy path: write a memory whose body lines up with
    the user message, fake a transcript carrying that question, run
    the hook, assert a `turn_audited` event landed (and probably a
    `search_miss` since no search/show events were recorded for the
    session yet)."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    store.write(
        content="My postgres database is on port 5433, not the default 5432.",
        scopes=["infrastructure"],
    )

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "message": {"content": "what port is my postgres on?"},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": "Postgres is usually on 5432.",
                    }
                ]
            },
        },
    )

    code = hook_main(
        [
            "--transcript-path",
            str(transcript),
            "--session-id",
            "sess-xyz",
        ]
    )
    assert code == 0
    events = list(iter_events(mem_dir))
    kinds = [e["kind"] for e in events]
    assert "turn_audited" in kinds, f"no turn_audited event in {kinds}"
    audited = next(e for e in events if e["kind"] == "turn_audited")
    assert audited["triggered_from"] == "stop_hook"
    # The hook mirrors the in-process handler's `assistant_present`
    # signal — the transcript above carries an assistant text block,
    # so the field must be True. Pre-fix the hook accepted
    # `assistant_response` as a parameter and silently dropped it,
    # leaving downstream rollups joining stop-hook and model-side
    # events with an inconsistent field shape.
    assert audited["assistant_present"] is True
    # Canonical-shape regression for the 2.6.4 silent-miss fix.
    # Pre-2.6.4 the hook emitted `turn_audited` without `session_id` /
    # `lookback_seconds` / `probe_mode`, and `search_miss` with
    # `top_hit_ids=[strings]` instead of `top_hits=[dicts]`. That
    # silently broke `eval.py:_silent_miss_from_event` for the
    # primary production source. The fields below MUST exist with
    # the canonical shape the in-process handler also writes — if
    # any disappears, the silent-miss eval renderer regresses.
    assert isinstance(audited.get("session_id"), str)
    assert isinstance(audited.get("lookback_seconds"), int)
    assert isinstance(audited.get("probe_mode"), str)
    assert isinstance(audited.get("threshold_rule"), str)
    if "search_miss" in kinds:
        miss = next(e for e in events if e["kind"] == "search_miss")
        # top_hits must be a list of dicts with id+relevance, not a
        # bare list of id strings.
        top_hits = miss.get("top_hits")
        assert isinstance(top_hits, list)
        if top_hits:
            first = top_hits[0]
            assert isinstance(first, dict)
            assert "id" in first
            assert "relevance" in first
        # threshold_rule and lookback_seconds are required for eval
        # to render the row meaningfully.
        assert isinstance(miss.get("threshold_rule"), str)
        assert isinstance(miss.get("lookback_seconds"), int)
        # The hook keeps its source marker.
        assert miss.get("triggered_from") == "stop_hook"
    # The output (when --quiet wasn't passed) is a JSON summary.
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["verdict"] in ("miss", "ok", "no_signal")


def test_main_records_assistant_present_false_when_no_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Symmetric check: a transcript whose latest assistant turn has
    only non-text blocks (thinking, tool_use) flattens to None — the
    event must record `assistant_present=False` so the rollup can
    distinguish 'no model response surfaced to user' from
    'response present but empty'."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    store.write(content="alpha note about postgres", scopes=["infrastructure"])

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "what about postgres?"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "internal-only"},
                    {"type": "tool_use", "name": "x", "input": {}},
                ]
            },
        },
    )

    code = hook_main(
        ["--transcript-path", str(transcript), "--session-id", "sess-q", "--quiet"]
    )
    assert code == 0
    events = list(iter_events(mem_dir))
    audited = next(e for e in events if e["kind"] == "turn_audited")
    assert audited["assistant_present"] is False


def test_main_swallows_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Contract: the hook must never propagate a non-zero exit.
    Force a downstream raise via a deliberately-broken
    BETTERMEMORY_DIR and confirm we still return 0."""
    import sys

    if sys.platform == "win32":
        # /dev/null is POSIX-specific; the broken-dir simulation
        # would need a different path on Windows.
        pytest.skip("broken-dir simulation is POSIX-only")
    monkeypatch.setenv("BETTERMEMORY_DIR", "/dev/null/not-a-dir")
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "question"}},
    )
    code = hook_main(
        [
            "--transcript-path",
            str(transcript),
            "--session-id",
            "sess",
            "--quiet",
        ]
    )
    assert code == 0


# ---------------------------------------------------------------------------
# Telemetry config + path defense (review pass — H2, M9)
# ---------------------------------------------------------------------------


def test_main_respects_telemetry_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: hook.run_audit was constructing a Recorder without
    threading `cfg.telemetry.enabled` through, so a user who
    explicitly opted out of event logging would still see
    `turn_audited` / `search_miss` events written on every Stop hook
    invocation. The recorder now honours the same config the server
    side reads.

    Fixture writes a config.toml with `[telemetry] enabled = false`
    and points BETTERMEMORY_CONFIG_PATH at it; the hook should run
    cleanly but produce no .events.jsonl."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    # Disable telemetry via a tmp config file.
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "[telemetry]\nenabled = false\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Some platforms read platformdirs from different env vars;
    # belt-and-suspenders via the documented override.
    monkeypatch.setenv("BETTERMEMORY_CONFIG", str(config_dir / "config.toml"))

    from bettermemory.store import Store

    Store(mem_dir).write(content="kept body", scopes=["tools"])
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "kept body question"}},
    )

    # Run with a config that disables telemetry explicitly by passing
    # it through the function entry point — the CLI-level override via
    # env-var is tested above; here we lock the behaviour at the
    # public surface.
    from bettermemory.config import Config, TelemetryConfig, StorageConfig
    from bettermemory.hook import run_audit

    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=False),
    )
    run_audit(
        user_message="kept body question",
        assistant_response=None,
        session_id="sess",
        config=cfg,
    )
    assert not (mem_dir / ".events.jsonl").exists(), (
        "telemetry-disabled config must suppress the Stop-hook "
        "event log; got an events.jsonl anyway"
    )


def test_main_rejects_non_file_transcript_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for the M9 review finding: a Stop-hook payload
    that delivers `transcript_path=<a directory>` or `<a fifo>` or
    `<a missing file>` must not coax the hook into reading
    something it shouldn't. The contract: resolve() + is_file()
    rejection short-circuits the hook to exit 0 without opening
    anything."""
    import sys

    if sys.platform == "win32":
        pytest.skip("file-mode checks are POSIX-shaped; Windows differs")
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    # Point at the memory directory itself (a directory, not a file).
    code = hook_main(
        [
            "--transcript-path",
            str(mem_dir),
            "--session-id",
            "sess",
            "--quiet",
        ]
    )
    assert code == 0
    assert not (mem_dir / ".events.jsonl").exists()

    # Point at a missing path.
    code = hook_main(
        [
            "--transcript-path",
            str(tmp_path / "definitely-missing.jsonl"),
            "--session-id",
            "sess",
            "--quiet",
        ]
    )
    assert code == 0
    assert not (mem_dir / ".events.jsonl").exists()


# ---------------------------------------------------------------------------
# Hook attribution — substring-matching retrieved memory bodies against the
# assistant reply and emitting `use` events with attribution="hook".
# ---------------------------------------------------------------------------


def _seed_search_event(mem_dir: Path, *, session_id: str, returned: list[str]) -> None:
    """Write a synthetic `search` event so the hook treats the listed
    memory_ids as recently retrieved in this session. Avoids spinning
    up the MCP server just to populate the precondition for an
    attribution test."""
    from bettermemory.events import Recorder

    Recorder(root=mem_dir, session_id=session_id).record(
        "search",
        query="seed",
        scopes_filter=None,
        max_results=5,
        returned=returned,
        relevance=["high"] * len(returned),
        expand_top=False,
        expanded_id=None,
        expanded_drift_missing=0,
        expanded_commit_drift_status=None,
        expanded_commits_since_verify=None,
        auto_scope=True,
        repo_filter=None,
    )


def _seed_list_event(mem_dir: Path, *, session_id: str, returned: list[str]) -> None:
    """Write a synthetic `list` event (as the `memory_list` handler does)
    so the hook treats the listed memory_ids as retrieved this turn. The
    handler records the listed ids under the same `returned` field name a
    `search` uses, so attribution can read one shape across both kinds."""
    from bettermemory.events import Recorder

    Recorder(root=mem_dir, session_id=session_id).record(
        "list",
        returned=returned,
        scope=None,
    )


def test_pending_retrievals_treats_naive_ts_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the naive-timestamp parse (fix g).

    The bespoke `_parse_iso_ts` returned a NAIVE datetime when an event's
    `ts` lacked a UTC offset, and the caller's `ts.timestamp()` then
    interpreted that wall-clock in the runner's LOCAL zone — silently
    shifting the event by the local UTC offset and pushing recent
    retrievals out of the lookback window in any non-UTC zone.
    `parse_event_ts` stamps naive values as UTC so the lookback math is
    correct everywhere.

    Pinned deterministically: force a +05:30 local zone and a fixed
    `utcnow`, then seed a `search` event whose naive `ts` is 60s before
    `now` in UTC — well inside the 600s window. Under the buggy local
    parse the same wall-clock reads as 05:30 in the future of UTC, i.e.
    ~5.5h "old" once compared against the UTC cutoff, so the id would be
    dropped. Under the fix it stays retrieved.
    """
    import sys
    import time
    from datetime import datetime, timezone

    if sys.platform == "win32":
        pytest.skip("time.tzset() / TZ override is POSIX-only")

    monkeypatch.setenv("TZ", "Asia/Kolkata")  # UTC+05:30, no DST
    time.tzset()
    try:
        now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("bettermemory.hook.utcnow", lambda: now)

        # Naive ISO string (no offset) for an instant 60s before `now`.
        naive_ts = "2026-05-31T11:59:00"
        event = {
            "ts": naive_ts,
            "session": "sess_server",
            "kind": "search",
            "returned": ["mem_naive"],
        }
        pending = _pending_retrievals(
            [event],
            retrieval_session_id="sess_server",
            used_session_ids={"sess_server"},
            lookback_seconds=600,
        )
        assert pending == {"mem_naive"}, (
            "a naive event ts 60s old must be treated as UTC and stay "
            "inside the lookback window; got it dropped (local-time parse "
            "regression)"
        )
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_pending_retrievals_includes_list_event_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit-level pin for fix h: a `list` (memory_list) event's `returned`
    ids join the pending-retrieval set on the same branch as `search`,
    matched on the retrieval session id. `list_active` is accepted as an
    alias for the same kind."""
    from datetime import timezone

    from bettermemory.models import utcnow

    now = utcnow().astimezone(timezone.utc)
    monkeypatch.setattr("bettermemory.hook.utcnow", lambda: now)
    ts = now.isoformat().replace("+00:00", "Z")

    for kind in ("list", "list_active"):
        event = {
            "ts": ts,
            "session": "sess_server",
            "kind": kind,
            "returned": ["mem_listed"],
        }
        pending = _pending_retrievals(
            [event],
            retrieval_session_id="sess_server",
            used_session_ids={"sess_server"},
            lookback_seconds=600,
        )
        assert pending == {"mem_listed"}, f"{kind} event ids should be pending"

    # And a `use` event for the same id still dedups it back out, just
    # like the search path.
    used_event = {
        "ts": ts,
        "session": "sess_server",
        "kind": "use",
        "ids": ["mem_listed"],
    }
    list_event = {
        "ts": ts,
        "session": "sess_server",
        "kind": "list",
        "returned": ["mem_listed"],
    }
    pending = _pending_retrievals(
        [list_event, used_event],
        retrieval_session_id="sess_server",
        used_session_ids={"sess_server"},
        lookback_seconds=600,
    )
    assert pending == set(), "a recorded use must dedup a listed id out"


def test_hook_attributes_use_when_listed_body_appears_in_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for list-retrieval attribution (fix h).

    A `memory_list` surfaces memory ids exactly like a `search` hit, and
    `memory_list` records them under a `list` event so the hook can
    attribute them. Before the fix, `_pending_retrievals` dispatched on
    `search`/`show`/`use` only, so a memory seen ONLY via a listing was
    never eligible for hook attribution — undercounting
    memory_helped_rate. This mirrors the search-based attribution test
    but seeds a `list` event instead, and asserts the `use` event fires.
    """
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    written = store.write(
        content=(
            "The metrics dashboard runs at grafana.internal/d/api-latency "
            "for the oncall watch — pages on p99 over 800ms."
        ),
        scopes=["infrastructure"],
    )
    # Seed a `list` event (NOT a search) under the server session.
    _seed_list_event(mem_dir, session_id="sess-listattr", returned=[written.id])

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "where is the latency dashboard?"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "The metrics dashboard runs at "
                            "grafana.internal/d/api-latency for the oncall "
                            "watch — pages on p99 over 800ms. I'll add a panel."
                        ),
                    }
                ]
            },
        },
    )

    code = hook_main(
        [
            "--transcript-path",
            str(transcript),
            "--session-id",
            "sess-listattr",
            "--quiet",
        ]
    )
    assert code == 0

    events = list(iter_events(mem_dir))
    use_events = [e for e in events if e["kind"] == "use"]
    assert len(use_events) == 1, (
        f"expected one hook attribution from a list-retrieval; got: {use_events}. "
        "Before the fix _pending_retrievals ignored `list` events, so listed "
        "ids were never eligible for attribution."
    )
    ev = use_events[0]
    assert ev["attribution"] == "hook"
    assert ev["outcome"] == "applied"
    assert ev["ids"] == [written.id]


def test_hook_attributes_use_when_body_appears_in_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end attribution: write a memory whose body has a
    distinctive sentence, seed a `search` event so the hook treats
    the memory as retrieved, transcribe an assistant reply quoting
    the sentence, run the hook, assert the `use` event landed with
    `attribution="hook"` and the matched excerpt.
    """
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    written = store.write(
        content=(
            "The metrics dashboard runs at grafana.internal/d/api-latency "
            "for the oncall watch — pages on p99 over 800ms."
        ),
        scopes=["infrastructure"],
    )
    _seed_search_event(mem_dir, session_id="sess-attr", returned=[written.id])

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "where is the latency dashboard?"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "The metrics dashboard runs at "
                            "grafana.internal/d/api-latency for the oncall "
                            "watch — pages on p99 over 800ms. I'll add a "
                            "panel."
                        ),
                    }
                ]
            },
        },
    )

    code = hook_main(
        ["--transcript-path", str(transcript), "--session-id", "sess-attr", "--quiet"]
    )
    assert code == 0

    events = list(iter_events(mem_dir))
    use_events = [e for e in events if e["kind"] == "use"]
    assert len(use_events) == 1, f"expected one use event; got: {use_events}"
    ev = use_events[0]
    assert ev["attribution"] == "hook"
    assert ev["outcome"] == "applied"
    assert ev["auto"] is False
    assert ev["ids"] == [written.id]
    assert isinstance(ev["claim_excerpts"], list)
    # Producer-side pin, load-bearing since the 3.41.0 dedup rewrite:
    # `_already_recorded_pending_ids` derives the session bridge
    # PER-EVENT from this tag (a `use` row under a transcript id is
    # accepted because the row itself says a hook wrote it — the old
    # whole-log stop-hook-session pre-pass is gone). An attributed use
    # event that dropped the stamp would stop deduping against the
    # in-process auto-fallback and double-count the retrieval. The
    # AUTO-fallback shape has the same pin in its own test below.
    assert ev["triggered_from"] == "stop_hook"
    assert "grafana.internal/d/api-latency" in ev["claim_excerpts"][0]


def test_hook_attributes_across_session_id_spaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: the Stop hook and the in-process MCP server write events
    under DIFFERENT session-id spaces — the server's `sess_<hex>` recorder
    id vs Claude Code's transcript id. The attribution lookup used to filter
    retrievals by the transcript id, so it never matched the server-written
    `search`/`show` events: `pending` was always empty and the hook NEVER
    attributed a use in production. Only the single-id-space test fixtures
    (which reused one id for both roles) passed. The fix bridges retrieval
    lookup to the in-process server session via `_latest_in_process_session`.

    Here the search event is seeded under a server-style id and the hook is
    invoked with a DIFFERENT transcript-style id — the attribution must
    still fire (it returned zero use events before the fix).
    """
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    written = store.write(
        content=(
            "The metrics dashboard runs at grafana.internal/d/api-latency "
            "for the oncall watch — pages on p99 over 800ms."
        ),
        scopes=["infrastructure"],
    )
    # Server recorder id space (sess_<hex>) — what the in-process server
    # actually stamps on search/show events. Deliberately distinct from the
    # transcript id passed to the hook below.
    _seed_search_event(
        mem_dir, session_id="sess_deadbeefcafef00d", returned=[written.id]
    )

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "where is the latency dashboard?"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "The metrics dashboard runs at "
                            "grafana.internal/d/api-latency for the oncall "
                            "watch — pages on p99 over 800ms. I'll add a panel."
                        ),
                    }
                ]
            },
        },
    )

    # A Claude-transcript-style UUID — NOT the search event's session id.
    code = hook_main(
        [
            "--transcript-path",
            str(transcript),
            "--session-id",
            "9f8e7d6c-1234-4abc-9def-0123456789ab",
            "--quiet",
        ]
    )
    assert code == 0

    events = list(iter_events(mem_dir))
    use_events = [e for e in events if e["kind"] == "use"]
    assert len(use_events) == 1, (
        f"expected one cross-id-space hook attribution; got: {use_events}. "
        "Before the fix this was empty — retrievals were filtered by the "
        "transcript id, never matching the server's sess_<hex> events."
    )
    ev = use_events[0]
    assert ev["attribution"] == "hook"
    assert ev["outcome"] == "applied"
    assert ev["ids"] == [written.id]


def test_hook_skips_attribution_when_already_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If a use event for the memory already exists this session
    (e.g. the model explicitly recorded use, or an earlier hook turn
    attributed it), the hook must NOT emit a second use event.
    Approximates the in-process SessionState's pending-token check
    from the event log alone — the only signal the cross-process
    hook has access to.
    """
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    written = store.write(
        content=(
            "Sessions are stored in Redis with a 24-hour TTL and "
            "evicted on graceful logout via the destroy_session helper."
        ),
        scopes=["infrastructure"],
    )
    _seed_search_event(mem_dir, session_id="sess-dup", returned=[written.id])

    # Pre-record a model-explicit use for the same memory.
    from bettermemory.events import Recorder

    Recorder(root=mem_dir, session_id="sess-dup").record(
        "use",
        ids=[written.id],
        outcome="applied",
        note=None,
        attribution="model",
    )

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "session storage details?"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Sessions are stored in Redis with a 24-hour TTL "
                            "and evicted on graceful logout via the "
                            "destroy_session helper. Anything else?"
                        ),
                    }
                ]
            },
        },
    )

    code = hook_main(
        ["--transcript-path", str(transcript), "--session-id", "sess-dup", "--quiet"]
    )
    assert code == 0

    events = list(iter_events(mem_dir))
    use_events = [e for e in events if e["kind"] == "use"]
    # Only the pre-seeded model use stays — no hook duplicate.
    assert len(use_events) == 1
    assert use_events[0]["attribution"] == "model"


def test_hook_emits_auto_fallback_when_reply_doesnt_quote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If no candidate sentence from any retrieved memory's body
    appears in the reply, the hook settles the pending retrieval with
    the plain auto-fallback event (`auto=True, attribution="auto"`) —
    the turn is over and the memory demonstrably didn't shape the
    reply, so this is the end-of-turn home of the commit that used to
    fire mid-turn from the in-process ~2-turn TTL. No hook-attributed
    (`attribution="hook"`) event may be emitted for a no-match turn."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))

    store = Store(mem_dir)
    written = store.write(
        content="The kubernetes ingress is on the staging cluster only for now.",
        scopes=["infrastructure"],
    )
    _seed_search_event(mem_dir, session_id="sess-miss", returned=[written.id])

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "what's for dinner?"}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Probably pasta. I'm flexible."}]
            },
        },
    )

    code = hook_main(
        ["--transcript-path", str(transcript), "--session-id", "sess-miss", "--quiet"]
    )
    assert code == 0

    events = list(iter_events(mem_dir))
    use_events = [e for e in events if e["kind"] == "use"]
    assert len(use_events) == 1
    auto_event = use_events[0]
    assert auto_event["ids"] == [written.id]
    assert auto_event["outcome"] == "applied"
    assert auto_event["auto"] is True
    assert auto_event["attribution"] == "auto"
    assert auto_event["triggered_from"] == "stop_hook"


# ---------------------------------------------------------------------------
# Opt-in self-improving loop — auto-consolidate fired from the Stop hook
# ---------------------------------------------------------------------------


def test_run_audit_auto_consolidates_when_opted_in(tmp_path: Path) -> None:
    """With [consolidate] auto_apply on (and telemetry on), run_audit fires
    the structurally-safe consolidation subset at turn end and records a
    reviewable auto_consolidate event."""
    from bettermemory.config import Config, ConsolidateConfig, StorageConfig
    from bettermemory.hook import run_audit

    mem_dir = tmp_path / "mem"
    store = Store(mem_dir)
    store.write(content="alpha beta gamma delta epsilon zeta", scopes=["tools"])
    store.write(content="alpha beta gamma delta epsilon zeta", scopes=["tools"])

    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        consolidate=ConsolidateConfig(auto_apply=True),
    )
    run_audit(
        user_message="hello",
        assistant_response="hi",
        session_id="sess-auto",
        config=cfg,
    )
    auto_events = [e for e in iter_events(mem_dir) if e["kind"] == "auto_consolidate"]
    assert len(auto_events) == 1
    assert auto_events[0]["status"] == "ran"
    assert len(store.load_all()) == 1  # duplicate consolidated away


def test_run_audit_no_consolidate_when_disabled(tmp_path: Path) -> None:
    """Default config (auto_apply off) never auto-mutates the store."""
    from bettermemory.config import Config, StorageConfig
    from bettermemory.hook import run_audit

    mem_dir = tmp_path / "mem"
    store = Store(mem_dir)
    store.write(content="alpha beta gamma delta", scopes=["tools"])
    store.write(content="alpha beta gamma delta", scopes=["tools"])

    cfg = Config(storage=StorageConfig(directory=str(mem_dir)))
    run_audit(
        user_message="hello",
        assistant_response="hi",
        session_id="sess-noop",
        config=cfg,
    )
    assert [e for e in iter_events(mem_dir) if e["kind"] == "auto_consolidate"] == []
    assert len(store.load_all()) == 2  # untouched


def test_run_audit_no_consolidate_when_telemetry_off(tmp_path: Path) -> None:
    """Auto-consolidate refuses to run without the event log — its debounce
    clock AND audit trail — even when auto_apply is on."""
    from bettermemory.config import (
        Config,
        ConsolidateConfig,
        StorageConfig,
        TelemetryConfig,
    )
    from bettermemory.hook import run_audit

    mem_dir = tmp_path / "mem"
    store = Store(mem_dir)
    store.write(content="alpha beta gamma delta", scopes=["tools"])
    store.write(content="alpha beta gamma delta", scopes=["tools"])

    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        consolidate=ConsolidateConfig(auto_apply=True),
        telemetry=TelemetryConfig(enabled=False),
    )
    run_audit(
        user_message="hello",
        assistant_response="hi",
        session_id="sess-telemoff",
        config=cfg,
    )
    assert len(store.load_all()) == 2  # not mutated
    assert not (mem_dir / ".events.jsonl").exists()  # telemetry off → no log


def test_run_audit_size_guard_sees_the_store_not_the_probe_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hook→`run_auto_consolidate` wiring: the bounded-store guard
    must measure the STORE, never the audit probe's candidate pool.

    `run_auto_consolidate`'s `memories` argument feeds `active =
    memories if memories is not None else store.load_all()`, and the
    Bounded safety contract skips the unattended O(N²) dedup when
    `len(active) > max_memories`. Once the probe started ranking
    production's pool instead of a full `load_all()`, the only list this
    hook holds is a `_PREFILTER_CAP`-capped, query-biased slice — so
    forwarding it would cap the measured size at 50 and run the pass on
    exactly the oversized stores the guard defers. Shipped defaults make
    that collision exact: `_INDEX_THRESHOLD_DEFAULT` and
    `auto_apply_max_memories` are both 500.

    Engage the prefilter for real and assert the guard still fires with
    the TRUE active count. The two duplicate bodies are the teeth: with
    the guard defeated, the dedup pass tombstones one and the store
    shrinks."""
    from bettermemory import index
    from bettermemory.config import (
        Config,
        ConsolidateConfig,
        StorageConfig,
        TelemetryConfig,
    )
    from bettermemory.consolidate import AUTO_CONSOLIDATE_EVENT

    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    mem_dir = tmp_path / "mem"
    store = Store(mem_dir)
    for i in range(58):
        store.write(
            content=f"alpha beta gamma delta epsilon zeta filler-{i}",
            scopes=["tools"],
        )
    for _ in range(2):
        store.write(
            content="alpha beta gamma delta epsilon zeta duplicate marker",
            scopes=["tools"],
        )
    total = len(store.load_all())
    assert total == 60
    index.rebuild(mem_dir, store.iter_active())

    pool_sizes: list[int] = []
    import bettermemory.handlers.search as search_mod

    real_pool = search_mod.resolve_search_pool

    def pool_spy(*args: object, **kwargs: object) -> object:
        pool = real_pool(*args, **kwargs)  # type: ignore[arg-type]
        pool_sizes.append(len(pool.memories))
        return pool

    monkeypatch.setattr(search_mod, "resolve_search_pool", pool_spy)

    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=True),
        # Between the prefilter cap (50) and the store (60): a pool-sized
        # count passes the guard, a store-sized count trips it.
        consolidate=ConsolidateConfig(auto_apply=True, auto_apply_max_memories=55),
    )
    run_audit(
        user_message="alpha beta",
        assistant_response="hi",
        session_id="sess-guard",
        config=cfg,
    )

    assert pool_sizes and pool_sizes[0] <= 50 < total, (
        "sanity: the FTS prefilter must actually have capped the probe's "
        "pool, or this test is not exercising the divergence"
    )
    auto_events = [
        e for e in iter_events(mem_dir) if e["kind"] == AUTO_CONSOLIDATE_EVENT
    ]
    assert len(auto_events) == 1
    assert auto_events[0]["status"] == "skipped_store_too_large"
    assert auto_events[0]["active_count"] == total, (
        "the guard measured the probe's capped pool instead of the store"
    )
    assert len(store.load_all()) == total  # nothing tombstoned


# ---------------------------------------------------------------------------
# Write-reflex closure — proposal capture fired from the Stop hook
# ---------------------------------------------------------------------------


def test_run_audit_proposes_writes_when_opted_in(tmp_path: Path) -> None:
    """With [proposals] auto_propose on, run_audit captures a durable
    statement from the user message into the (inert) proposal queue."""
    from bettermemory.config import Config, ProposalsConfig, StorageConfig
    from bettermemory.hook import run_audit
    from bettermemory.proposals import ProposalQueue

    mem_dir = tmp_path / "mem"
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        proposals=ProposalsConfig(auto_propose=True),
    )
    run_audit(
        user_message="I prefer hands-on tutorials with runnable code, not screenshots.",
        assistant_response="sure",
        session_id="sess-prop",
        config=cfg,
    )
    pending = ProposalQueue(mem_dir).load()
    assert len(pending) == 1
    assert "runnable code" in pending[0].body


def test_run_audit_no_proposals_when_disabled(tmp_path: Path) -> None:
    """Default config (auto_propose off) captures nothing."""
    from bettermemory.config import Config, StorageConfig
    from bettermemory.hook import run_audit
    from bettermemory.proposals import ProposalQueue

    mem_dir = tmp_path / "mem"
    cfg = Config(storage=StorageConfig(directory=str(mem_dir)))
    run_audit(
        user_message="I prefer hands-on tutorials with runnable code, not screenshots.",
        assistant_response="sure",
        session_id="sess-noprop",
        config=cfg,
    )
    assert ProposalQueue(mem_dir).load() == []


# ---------------------------------------------------------------------------
# Failure isolation — a consolidate / proposals hiccup must neither block the
# turn end nor drop the audit result. run_audit's comments assert this; these
# pin it so a regression that lets the exception escape would fail CI.
# ---------------------------------------------------------------------------


def test_run_audit_isolates_consolidate_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raising auto-consolidate is swallowed: run_audit still returns the
    report and the already-recorded turn_audited event survives."""
    from bettermemory.config import (
        Config,
        ConsolidateConfig,
        StorageConfig,
        TelemetryConfig,
    )
    from bettermemory.hook import run_audit

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("consolidate exploded")

    monkeypatch.setattr("bettermemory.consolidate.run_auto_consolidate", boom)
    mem_dir = tmp_path / "mem"
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        consolidate=ConsolidateConfig(auto_apply=True),
        telemetry=TelemetryConfig(enabled=True),
    )
    result = run_audit(
        user_message="some durable turn content here",
        assistant_response=None,
        session_id="sess-iso-c",
        config=cfg,
    )
    assert isinstance(result, dict)  # returned, did not propagate
    assert "consolidate exploded" in capsys.readouterr().err  # caught + logged
    kinds = [e.get("kind") for e in iter_events(mem_dir)]
    assert "turn_audited" in kinds  # audit result was NOT dropped


def test_run_audit_isolates_proposals_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same isolation guarantee for the write-reflex capture half."""
    from bettermemory.config import (
        Config,
        ProposalsConfig,
        StorageConfig,
        TelemetryConfig,
    )
    from bettermemory.hook import run_audit

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("proposals exploded")

    monkeypatch.setattr("bettermemory.proposals.propose_from_exchange", boom)
    mem_dir = tmp_path / "mem"
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        proposals=ProposalsConfig(auto_propose=True),
        telemetry=TelemetryConfig(enabled=True),
    )
    result = run_audit(
        user_message="I prefer hands-on tutorials with runnable code.",
        assistant_response=None,
        session_id="sess-iso-p",
        config=cfg,
    )
    assert isinstance(result, dict)
    assert "proposals exploded" in capsys.readouterr().err
    kinds = [e.get("kind") for e in iter_events(mem_dir)]
    assert "turn_audited" in kinds


# ---------------------------------------------------------------------------
# Session-disabled scopes reconstructed from the event log (C3)
# ---------------------------------------------------------------------------


def _scope_event(*, session: str, kind: str, scope: str) -> dict[str, object]:
    """An in-process scope_disable / scope_enable event as the server
    writes it: stamped with the server session, no `triggered_from`."""
    return {"session": session, "kind": kind, "scope": scope}


def test_latest_in_process_session_skips_stop_hook_events() -> None:
    """The anchor must ignore the hook's own events (which carry
    `triggered_from="stop_hook"`) and return the most recent in-process
    server session id."""
    events = [
        {"session": "sess_server", "kind": "search"},
        {
            "session": "claude-uuid",
            "kind": "turn_audited",
            "triggered_from": "stop_hook",
        },
    ]
    assert _latest_in_process_session(events) == "sess_server"


def test_latest_in_process_session_none_when_only_stop_hook() -> None:
    """No in-process session in the log → None (nothing to anchor to)."""
    events = [
        {
            "session": "claude-uuid",
            "kind": "turn_audited",
            "triggered_from": "stop_hook",
        },
    ]
    assert _latest_in_process_session(events) is None


def test_latest_in_process_session_prefers_matching_worktree_stamp() -> None:
    """Regression (concurrent-session hijack): the event log is shared
    across ALL processes, so the latest event frequently belongs to a
    second Claude Code window's server. When the hook knows its own
    worktree, the anchor must prefer the latest event STAMPED with it,
    not whichever process wrote last."""
    events = [
        {"session": "sess_A", "kind": "search", "worktree_root": "/wt/this"},
        # Second window's server starting up — wrote last, different worktree.
        {
            "session": "sess_B",
            "kind": "scope_overview",
            "worktree_root": "/wt/other",
        },
    ]
    assert _latest_in_process_session(events, worktree_root="/wt/this") == "sess_A"


def test_latest_in_process_session_falls_back_to_latest_any() -> None:
    """No stamped match for this worktree (legacy unstamped logs, or the
    restart gap before this worktree's server writes its first event) →
    fall back to the latest-any behaviour, preserving reset-on-restart
    semantics for pre-stamp logs."""
    events = [
        {"session": "sess_old", "kind": "search"},  # legacy, no stamp
        {"session": "sess_new", "kind": "write"},  # legacy, no stamp
    ]
    assert _latest_in_process_session(events, worktree_root="/wt/this") == "sess_new"


def test_disabled_scopes_reconstructs_net_set() -> None:
    """Replay disable/enable for the current server session: a scope
    disabled then re-enabled drops out; one left disabled stays."""
    events = [
        _scope_event(session="sess_server", kind="scope_disable", scope="projects:a"),
        _scope_event(session="sess_server", kind="scope_disable", scope="projects:b"),
        _scope_event(session="sess_server", kind="scope_enable", scope="projects:a"),
    ]
    assert _disabled_scopes_from_events(events) == {"projects:b"}


def test_disabled_scopes_resets_on_new_server_session() -> None:
    """Reset-on-restart: a scope disabled under an OLD server session is
    NOT honoured once a fresh in-process session (the restarted server)
    appears in the log — the anchor moves to the new session, which has
    toggled nothing."""
    events: list[dict[str, object]] = [
        _scope_event(session="sess_old", kind="scope_disable", scope="projects:a"),
        # The restarted server's first in-process activity, new session id.
        {"session": "sess_new", "kind": "search"},
    ]
    assert _disabled_scopes_from_events(events) == set()


def test_disabled_scopes_empty_when_no_in_process_session() -> None:
    """Only stop-hook events in the log → no anchor → empty set, even if
    a stray scope event somehow carried a stop-hook session."""
    events = [
        {
            "session": "claude-uuid",
            "kind": "turn_audited",
            "triggered_from": "stop_hook",
        },
    ]
    assert _disabled_scopes_from_events(events) == set()


def _miss_config(mem_dir: Path) -> "object":
    from bettermemory.config import Config, StorageConfig, TelemetryConfig

    return Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=True),
    )


# A body + query pair that scores a deterministic high-relevance hit
# (mirrors the load-bearing case in test_audit.py).
_MISS_BODY = "backup strategy uses triangular restic replication"
_MISS_QUERY = "backup strategy"


def _write_miss_memory(mem_dir: Path) -> str:
    """Seed the high-relevance memory, backdated past the created-time
    filter in `probe_for_miss` (a memory born inside the lookback window
    cannot be retrieval-miss evidence, so a same-breath write-then-audit
    would probe an empty candidate list). Backdating keeps every test in
    this family exercising the mechanism it names — the disabled-scope
    suppressions in particular must come from scope logic, not from the
    filter emptying the store. Mirrors `_backdate_created` in
    test_audit.py; an hour comfortably predates the clamped maximum
    lookback (600s). Returns the memory id."""
    store = Store(mem_dir)
    written = store.write(content=_MISS_BODY, scopes=["infrastructure"])
    backdated = datetime.now(timezone.utc) - timedelta(hours=1)
    for path, mem in store.iter_active():
        if mem.id == written.id:
            store._write_path(
                path,
                mem.model_copy(update={"created": backdated, "updated": backdated}),
            )
            return written.id
    raise AssertionError(f"memory {written.id!r} not found in store")


def test_run_audit_baseline_flags_miss(tmp_path: Path) -> None:
    """Control: with no disabled scopes, a high-relevance hit and no
    recent retrieval is a silent miss."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-baseline",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "miss"


def test_run_audit_recent_retrieval_under_server_session_suppresses_miss(
    tmp_path: Path,
) -> None:
    """Regression (whole-tree sweep, HIGH): the Stop hook's retrieval
    shield must bridge to the *server* session id. A `search` the server
    emitted under its `sess_<hex>` id within the lookback window proves the
    model retrieved this turn, so the same high-relevance hit that is a
    miss in the baseline must NOT be flagged. Before the fix, run_audit
    compared retrievals against Claude's transcript session id — a
    different id space from the server's `sess_<hex>` — so the shield was
    structurally dead and this returned "miss". The only difference from
    the baseline test is the seeded retrieval, so a non-miss verdict
    isolates the shield."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    # The server emits a retrieval event under its own session id, exactly
    # as memory_search does. The hook reads it back cross-process.
    Recorder(root=mem_dir, session_id="sess_server").record("search")

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        # Claude Code's transcript id — deliberately NOT the server session.
        session_id="claude-retrieved",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "ok"


def test_run_audit_foreign_worktree_event_does_not_unshield(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent-session hijack, over-flagging direction: THIS worktree's
    server searched 10s ago, then a second Claude Code window's server
    (different worktree) wrote any event. The anchor used to flip to the
    foreign session, making the in-window search invisible to the shield
    — a spurious `search_miss` for a turn that searched correctly. With
    the worktree-stamped anchor the verdict must stay "ok"."""
    from bettermemory.origin import Origin

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    wt_this = str(tmp_path / "wt-this")
    wt_other = str(tmp_path / "wt-other")
    monkeypatch.setattr(
        "bettermemory.hook.capture_origin",
        lambda *a, **k: Origin(worktree_root=wt_this),
    )

    # This window's server searched (stamped with this worktree)…
    Recorder(root=mem_dir, session_id="sess_A", worktree_root=wt_this).record("search")
    # …then the other window's server wrote the LATEST event.
    Recorder(root=mem_dir, session_id="sess_B", worktree_root=wt_other).record(
        "scope_overview"
    )

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-concurrent-a",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "ok", (
        "the foreign session's later event hijacked the anchor and made "
        "this worktree's own search invisible to the retrieval shield"
    )


def test_run_audit_foreign_worktree_search_does_not_shield(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent-session hijack, mirrored (suppression) direction: only
    the OTHER window's server searched (an unrelated query, different
    worktree); this worktree's server has session-start activity but no
    search. The foreign search used to win the anchor and shield this
    window's genuine miss. With the worktree-stamped anchor the miss
    must fire."""
    from bettermemory.origin import Origin

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    wt_this = str(tmp_path / "wt-this")
    wt_other = str(tmp_path / "wt-other")
    monkeypatch.setattr(
        "bettermemory.hook.capture_origin",
        lambda *a, **k: Origin(worktree_root=wt_this),
    )

    # This window's server is alive (session-start overview) but never
    # searched this turn…
    Recorder(root=mem_dir, session_id="sess_A", worktree_root=wt_this).record(
        "scope_overview"
    )
    # …while the other window's server searched for its own topic.
    Recorder(root=mem_dir, session_id="sess_B", worktree_root=wt_other).record("search")

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-concurrent-b",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "miss", (
        "the foreign session's search shielded this worktree's genuine "
        "miss via the hijacked anchor"
    )


def test_run_audit_same_worktree_concurrent_session_retrieval_shields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Round-88 regression (same-worktree anchor collision): THIS
    worktree's server searched within the window under sess_A, then a
    SECOND same-worktree session (a concurrent Claude window, or the
    restarted server mid-conversation) wrote the latest in-process
    event under sess_B. `_latest_in_process_session` correctly anchors
    to sess_B (latest worktree-stamped event), but the retrieval shield
    used to match that single session id only — orphaning sess_A's
    in-window search and re-firing a false `search_miss` for a turn
    that retrieved correctly. The shield now counts retrievals stamped
    with this worktree under ANY session, so the verdict stays "ok".
    (Round 85's 60→600s widening scaled this collision window ~10x in
    the over-flag direction; the cross-worktree variants above pin the
    foreign-worktree directions.)"""
    from bettermemory.origin import Origin

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    wt_this = str(tmp_path / "wt-this")
    monkeypatch.setattr(
        "bettermemory.hook.capture_origin",
        lambda *a, **k: Origin(worktree_root=wt_this),
    )

    # This worktree's first server session searched within the window…
    Recorder(root=mem_dir, session_id="sess_A", worktree_root=wt_this).record("search")
    # …then a second SAME-worktree session wrote the latest in-process
    # event (a non-retrieval `write`, so only the orphaned search can
    # feed the shield), flipping the anchor to sess_B.
    Recorder(root=mem_dir, session_id="sess_B", worktree_root=wt_this).record("write")

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-concurrent-same-wt",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "ok", (
        "the same-worktree session's later event flipped the anchor and "
        "orphaned this worktree's own in-window search — the shield is "
        "matching a single session id again"
    )


def test_run_audit_flags_miss_on_memory_minutes_old(tmp_path: Path) -> None:
    """Round-88 regression (creation shield re-coupled to the lookback):
    the hook probes with `lookback_seconds=600`, and the created-time
    filter used to reuse that window — so a memory created 1-10 minutes
    ago (existing well before this turn's message; the freshest,
    most-likely-relevant content) was structurally invisible to the
    primary production producer, while the in-process handler flagged
    the identical turn. A 5-minute-old memory with a matching message
    and zero retrieval events must flag a miss through the hook."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    store = Store(mem_dir)
    written = store.write(content=_MISS_BODY, scopes=["infrastructure"])
    # Backdate to 300s — outside the 60s creation shield, INSIDE the
    # hook's 600s attribution lookback. (`_write_miss_memory`'s 1h
    # backdate clears both windows, so it cannot discriminate.)
    backdated = datetime.now(timezone.utc) - timedelta(seconds=300)
    for path, mem in store.iter_active():
        if mem.id == written.id:
            store._write_path(
                path,
                mem.model_copy(update={"created": backdated, "updated": backdated}),
            )
            break

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-fresh-memory",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "miss", (
        "a 5-minute-old memory must be probe-visible at the hook's 600s "
        "lookback; 'no_signal' means the creation shield re-coupled to "
        "the retrieval-shield window"
    )


def test_run_audit_disabled_scope_suppresses_stop_hook_miss(tmp_path: Path) -> None:
    """C3 core: a scope the user disabled in-session (recorded as a
    `scope_disable` event) is excluded from the hook's probe, so the
    same turn that would otherwise be flagged is no longer a miss."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    # Seed the in-process server's disable event onto disk, exactly as
    # memory_scope_disable would. The hook reads it back cross-process.
    Recorder(root=mem_dir, session_id="sess_server").record(
        "scope_disable", scope="infrastructure"
    )

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-disabled",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    # Pin the exact suppressed verdict: with the only matching memory's
    # scope excluded, the probe sees no candidate at all -> "no_signal".
    # (`!= "miss"` would also pass if the store were empty or the probe
    # silently broke; this pins the real shield path.)
    assert result["verdict"] == "no_signal"


def test_run_audit_reenabled_scope_reflags_miss(tmp_path: Path) -> None:
    """The disable correctly resets: a scope disabled then re-enabled in
    the same server session is back in scope, so the miss fires again."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    rec = Recorder(root=mem_dir, session_id="sess_server")
    rec.record("scope_disable", scope="infrastructure")
    rec.record("scope_enable", scope="infrastructure")

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-reenabled",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "miss"


def test_run_audit_disable_resets_after_server_restart(tmp_path: Path) -> None:
    """Reset-on-restart end-to-end: a scope disabled under a prior server
    session does NOT shield the audit once a fresh server session has
    written in-process activity — the miss fires again."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    # Old server disabled the scope, then restarted; the new server's first
    # in-process activity lands under a fresh session id, which re-anchors
    # the disabled-scope reconstruction (the prior session's disable no
    # longer applies), so the miss must reappear. The anchor is a
    # non-retrieval `write` event on purpose: a `search`/`show`/`list` here
    # would (correctly) trip the retrieval shield and mask the reset behind
    # an unrelated mechanism — the shield itself is covered by
    # test_run_audit_recent_retrieval_under_server_session_suppresses_miss.
    Recorder(root=mem_dir, session_id="sess_old").record(
        "scope_disable", scope="infrastructure"
    )
    Recorder(root=mem_dir, session_id="sess_new").record("write")

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-restart",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "miss"


def test_run_audit_stale_disable_shields_during_restart_gap(tmp_path: Path) -> None:
    """Restart gap window (documented conservative bias): reset-on-restart
    is NOT atomic. Until a restarted server writes its first in-process
    event, `_latest_in_process_session` still anchors to the OLD session,
    so the old session's `scope_disable` keeps shielding the hook. With
    only the prior session's disable on disk (no new in-process activity),
    the miss stays suppressed. This pins the gap window the module
    docstring describes — biased toward over-suppression, self-correcting
    once the new server records any in-process tool call (covered by
    test_run_audit_disable_resets_after_server_restart)."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    # Prior server disabled the scope, then restarted. The new server has
    # not yet written ANY in-process event, so the latest non-stop-hook
    # event is still sess_old's disable.
    Recorder(root=mem_dir, session_id="sess_old").record(
        "scope_disable", scope="infrastructure"
    )

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-restart-gap",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "no_signal"


def test_run_audit_shields_search_older_than_sixty_seconds(tmp_path: Path) -> None:
    """Regression: the hook hardcoded `lookback_seconds=60` at the probe
    call while using the 600s `_ATTRIBUTION_LOOKBACK_SECONDS` window for
    everything else — two definitions of "this turn" in one function.
    The Stop hook fires at turn END, so on any tool-heavy turn longer
    than a minute the server's `search` event aged out of the shield
    window and a searched-then-continued turn emitted a false
    `search_miss`. A server search ~120s old (well past 60s, well
    inside the attribution window) must still shield."""
    from bettermemory.models import utcnow

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    # Backdate the server's search event 120s — the recorder always
    # stamps "now", so write the line directly in the recorder's shape.
    ts = (utcnow() - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    (mem_dir / ".events.jsonl").write_text(
        json.dumps(
            {"ts": ts, "session": "sess_server", "kind": "search", "returned": []}
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-slow-turn",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "ok", (
        "a server search 120s old must shield a tool-heavy turn; 'miss' "
        "means the probe lookback regressed below the attribution window"
    )


def test_run_audit_shield_survives_event_log_rotation(tmp_path: Path) -> None:
    """End-to-end pin for the rotation false-miss: rotation archives the
    ENTIRE active log when it crosses max_bytes, at a moment independent
    of turn boundaries. Pre-fix the hook read the active log only, so a
    turn straddling a rotation lost its own `search` event and re-fired
    as a miss. Force a rotation AFTER the server's search; the
    window-aware read must still see it and return "ok"."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    # The server searches this turn...
    Recorder(root=mem_dir, session_id="sess_server").record("search")
    # ...then a mid-turn write trips rotation (max_bytes=1: any non-empty
    # active log rotates before the append), archiving the search event.
    # The new event is deliberately a non-retrieval `write` so the shield
    # can only be fed by the ARCHIVED search.
    Recorder(root=mem_dir, session_id="sess_server", max_bytes=1).record("write")
    assert list(mem_dir.glob(".events-*.jsonl.gz")), "rotation did not fire"

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-rotated",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "ok", (
        "the archived search must still shield the turn; 'miss' means the "
        "hook is reading the active log only again"
    )


def test_run_audit_endorsement_tally_uses_production_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 7 regression: the endorsement tally in `hook.run_audit` (the
    PRIMARY production audit producer) must be counted over the SAME window
    production search uses (`ATTRIBUTION_LOOKBACK_SECONDS`, 600s) — NOT the
    dedup-widened `recent` read (`REAUDIT_DEDUP_WINDOW_SECONDS`, 3600s).

    The F7 hardening fixed only the sibling (`handlers/audit_turn.py`); the
    shipped hook still fed `recent` (the 3600s coverage list) straight into
    `_explicit_applied_counts`, which applies no cutoff of its own.
    `iter_events_window` differs between the two windows only in whether it
    prepends the newest rotated archive (it does when the active log's oldest
    event is younger than `now - window`), so the 3600s read counts applies
    from an archive that production's 600s ranker would never have prepended
    — an endorsement nudge the model's real retrieval never saw, enough to
    flip a near-tie top-1 into a false `search_miss`.

    Assert the hook issues an `iter_events_window` read at the 600s
    attribution window when `endorsement_boost` is on (pre-fix it reused the
    3600s `recent` list and never read the narrower window). Reverting the
    fix drops the 600s call, so the final assertion fails."""
    import bettermemory.hook as hook_mod
    from bettermemory.audit import (
        ATTRIBUTION_LOOKBACK_SECONDS,
        REAUDIT_DEDUP_WINDOW_SECONDS,
    )
    from bettermemory.config import (
        BehaviorConfig,
        Config,
        StorageConfig,
        TelemetryConfig,
    )
    from bettermemory.events import iter_events_window as real_iew
    from bettermemory.models import utcnow

    # The two constants must not be accidentally equal — the whole fix rests
    # on the dedup window being strictly wider than the attribution window.
    assert ATTRIBUTION_LOOKBACK_SECONDS == 600
    assert REAUDIT_DEDUP_WINDOW_SECONDS == 3600
    assert ATTRIBUTION_LOOKBACK_SECONDS != REAUDIT_DEDUP_WINDOW_SECONDS

    windows: list[int] = []

    def spy(root: object, window_seconds: int, **kw: object) -> object:
        windows.append(window_seconds)
        return real_iew(root, window_seconds, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(hook_mod, "iter_events_window", spy)

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    mem_id = _write_miss_memory(mem_dir)

    # Seed the endorsement signal the tally reads: an explicit (non-auto)
    # applied `use` in-window, plus a second one backdated past the 600s
    # attribution window but inside the 3600s dedup window. Under the buggy
    # 3600s tally both would be counted; the fix scopes the read to 600s.
    Recorder(root=mem_dir, session_id="sess_server").record(
        "use", ids=[mem_id], outcome="applied", auto=False
    )
    stale_ts = (utcnow() - timedelta(seconds=1800)).isoformat().replace("+00:00", "Z")
    with (mem_dir / ".events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "ts": stale_ts,
                    "session": "sess_server",
                    "kind": "use",
                    "ids": [mem_id],
                    "outcome": "applied",
                    "auto": False,
                }
            )
            + "\n"
        )

    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=True),
        behavior=BehaviorConfig(endorsement_boost=True),
    )
    run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-endorsement",
        config=cfg,
    )

    # The dedup / shield / attribution consumers still get the full 3600s
    # coverage read...
    assert REAUDIT_DEDUP_WINDOW_SECONDS in windows
    # ...but the endorsement tally is scoped to production's 600s window, so
    # the audit ranker matches what the model's retrieval actually saw. This
    # 600s read is absent pre-fix (the tally reused `recent`).
    assert ATTRIBUTION_LOOKBACK_SECONDS in windows


def test_run_audit_legacy_semantic_config_file_audits_as_hybrid(
    tmp_path: Path,
) -> None:
    """A config FILE still saying `search_mode = "semantic"` must audit.

    Pre-4.0 that deployment was the permanently-unmeasured cohort: the
    probe declined every turn with `no_signal_reason =
    "semantic_model_unavailable"`. The 4.0.0 strip removed the mode
    outright and `config._coerce_search_mode` normalises the stale file
    value to `hybrid` (loudly) — so the hook, which always goes through
    `load_config` in its fresh process, probes hybrid and produces REAL
    telemetry. Pin the end-to-end path so the unmeasured cohort cannot
    recur under a leftover config line."""
    from bettermemory.config import load_config

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[storage]\ndirectory = "{mem_dir}"\n\n[behavior]\nsearch_mode = "semantic"\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.behavior.search_mode == "hybrid"

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-legacy-semantic",
        config=cfg,
    )
    # Pre-4.0 this was a structural no_signal; now the probe really ran.
    assert result["verdict"] != "no_signal"
    assert result.get("no_signal_reason") is None
    audited = [e for e in iter_events(mem_dir) if e["kind"] == "turn_audited"]
    assert len(audited) == 1
    assert audited[0]["probe_mode"] == "hybrid"


def test_run_audit_threads_ranker_config_into_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Stop hook must hand the probe the configured ranker knobs —
    `recency_boost_half_life_days` and (when `endorsement_boost` is on)
    the explicit-applied tally. Pre-fix the probe ranked with hardwired
    defaults for every config."""
    from bettermemory.audit import probe_for_miss as real_probe
    from bettermemory.config import BehaviorConfig, Config, StorageConfig

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    memory_id = _write_miss_memory(mem_dir)
    # An explicit (non-auto) applied use — the only kind the endorsement
    # tally counts.
    Recorder(root=mem_dir, session_id="sess_server").record(
        "use", ids=[memory_id], outcome="applied", auto=False
    )

    captured: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_probe(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bettermemory.hook.probe_for_miss", spy)
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        behavior=BehaviorConfig(
            recency_boost_half_life_days=7.0, endorsement_boost=True
        ),
    )
    run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-cfg",
        config=cfg,
    )
    assert captured["half_life_days"] == 7.0
    assert captured["applied_by_id"] == {memory_id: 1}


def test_run_audit_endorsement_tally_drops_out_of_window_applies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation-sound: `_explicit_applied_counts` now enforces its OWN 600s
    cutoff, so an explicit apply backdated past `ATTRIBUTION_LOOKBACK_SECONDS`
    can no longer reach `applied_by_id` — even though it rides in on the
    active-log read the hook feeds the tally. The prior contract applied no
    cutoff of its own and trusted the caller to pre-window, so a stale apply
    (t-1800s: inside the 3600s dedup horizon, outside the 600s attribution
    window) was counted, nudging the probe's near-tie ranker. Seed one apply
    at t-100s (in-window) and one at t-1800s (out-of-window); the probe must
    see only the in-window count. Reverting the internal `ts` drop re-counts
    the stale apply as ``{id: 2}`` and this fails."""
    from bettermemory.audit import probe_for_miss as real_probe
    from bettermemory.config import BehaviorConfig, Config, StorageConfig
    from bettermemory.models import utcnow

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    memory_id = _write_miss_memory(mem_dir)

    now = utcnow()
    fresh_ts = (now - timedelta(seconds=100)).isoformat().replace("+00:00", "Z")
    stale_ts = (now - timedelta(seconds=1800)).isoformat().replace("+00:00", "Z")
    with (mem_dir / ".events.jsonl").open("a", encoding="utf-8") as fh:
        for ts in (fresh_ts, stale_ts):
            fh.write(
                json.dumps(
                    {
                        "ts": ts,
                        "session": "sess_server",
                        "kind": "use",
                        "ids": [memory_id],
                        "outcome": "applied",
                        "auto": False,
                    }
                )
                + "\n"
            )

    captured: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_probe(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bettermemory.hook.probe_for_miss", spy)
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        behavior=BehaviorConfig(endorsement_boost=True),
    )
    run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-window",
        config=cfg,
    )
    # Only the t-100s apply is in-window; the t-1800s apply is dropped by the
    # tally's internal cutoff even though both sit in the read it was fed.
    assert captured["applied_by_id"] == {memory_id: 1}
    # And the probe window is the shared attribution window, not the
    # old hardcoded 60.
    assert captured["lookback_seconds"] == 600


# A two-memory near-tie the bounded `search._demotion_factor` can
# re-rank. Both memories carry the SAME body, so both clear the v1
# "high" threshold and only recency separates them before any demotion.
# What differs is suppression eligibility: the project-scoped memory was
# written from the caller's repo, so `_caller_in_top_hit_project`
# explains away the missing search while it holds rank 1; the global one
# cannot. Mirrors `_demotion_pair` in test_audit.py.
_DEMOTION_QUERY = "restic replication"
_DEMOTION_BODY = "restic replication runbook lives in the homelab tree"
_DEMOTION_REPO = "git@github.com:owner/homelab.git"


def _write_demotion_pair(mem_dir: Path, worktree: str) -> tuple[str, str]:
    """Seed `(project_memory_id, global_memory_id)`.

    The project memory is a day fresher — a real, deterministic score
    lead under the default half-life, far inside the demotion factor's
    reach. Both are backdated well past the probe's creation shield, and
    past the point where a negative outcome recorded "now" would be
    treated as resolved by a newer `updated`."""
    from bettermemory.origin import Origin

    store = Store(mem_dir)
    project = store.write(
        content=_DEMOTION_BODY,
        scopes=["projects:homelab"],
        origin=Origin(cwd=worktree, repo=_DEMOTION_REPO, worktree_root=worktree),
    )
    global_memory = store.write(content=_DEMOTION_BODY, scopes=["infrastructure"])
    now = datetime.now(timezone.utc)
    ages = {
        project.id: timedelta(hours=1),
        global_memory.id: timedelta(days=1, hours=1),
    }
    for path, mem in store.iter_active():
        age = ages.get(mem.id)
        if age is None:
            continue
        stamp = now - age
        store._write_path(
            path, mem.model_copy(update={"created": stamp, "updated": stamp})
        )
    return project.id, global_memory.id


def test_run_audit_demotion_changes_the_probe_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Stop hook — the PRIMARY production audit producer — must tally
    active negative outcomes and feed them to the probe whenever
    `[behavior] outcome_demotion` is on.

    Pre-fix it tallied only explicit applies, so a memory production
    retrieval had demoted out of the top slot still held rank 1 in the
    probe. Since the miss verdict reads ONLY the rank-1 hit, the audit
    then reported on a ranking the model never performed — in both
    directions (the demoted memory's suppression masking a real miss
    here; elsewhere a demotion-promoted top hit the probe never saw).

    Same store, same message, same flag: the only difference between the
    two runs is one recorded rejection."""
    from bettermemory.config import (
        BehaviorConfig,
        Config,
        StorageConfig,
        TelemetryConfig,
    )
    from bettermemory.origin import Origin as _Origin

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    worktree = str(tmp_path / "homelab-wt")
    project_id, global_id = _write_demotion_pair(mem_dir, worktree)
    monkeypatch.setattr(
        "bettermemory.hook.capture_origin",
        lambda *a, **k: _Origin(
            cwd=worktree, repo=_DEMOTION_REPO, worktree_root=worktree
        ),
    )

    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=True),
        # Keyword mode ranks on the raw scorer, where the bounded factor
        # is directly visible; hybrid's RRF would only show it once a
        # per-ranker rank actually swapped.
        behavior=BehaviorConfig(search_mode="keyword", outcome_demotion=True),
    )

    neutral = run_audit(
        user_message=_DEMOTION_QUERY,
        assistant_response=None,
        session_id="claude-demotion-neutral",
        config=cfg,
    )
    assert neutral["top_hits"][0]["id"] == project_id
    assert neutral["top_hits"][0]["relevance"] == "high"
    assert neutral["recent_retrieval_count"] == 0
    # Rank 1 is the caller's own project memory → "the model has this
    # repo open" explains the missing search.
    assert neutral["verdict"] == "ok"

    # One explicit rejection, postdating the memory's `updated` so it is
    # unresolved and still testifies.
    Recorder(root=mem_dir, session_id="sess_server").record(
        "use", ids=[project_id], outcome="contradicted", auto=False
    )

    demoted = run_audit(
        user_message=_DEMOTION_QUERY,
        assistant_response=None,
        # A fresh session id: the re-audit dedup matches on (session,
        # message) and would mark the second run a repeat, which
        # suppresses the companion `search_miss` event.
        session_id="claude-demotion-rejected",
        config=cfg,
    )
    assert demoted["top_hits"][0]["id"] == global_id
    assert demoted["top_hits"][0]["relevance"] == "high"
    assert demoted["verdict"] == "miss"
    misses = [e for e in iter_events(mem_dir) if e["kind"] == "search_miss"]
    assert len(misses) == 1, "the flipped verdict must reach the event log"


# A store where the FTS5 candidate prefilter is SATURATED and the memory
# that would win a full-corpus ranking sits past the cap. Above
# `_INDEX_THRESHOLD_DEFAULT` production ranks that capped slice; a probe
# ranking an unconditional `store.load_all()` ranks a strict superset.
# Mirrors `_write_prefilter_starved_store` in test_audit.py.
_STARVED_QUERY = "alpha beta"
_STARVED_REPO = "git@github.com:example/repo-a.git"


def _write_prefilter_starved_store(mem_dir: Path, worktree: str) -> str:
    """Seed the starved store and return the past-the-cap target's id.

    60 short bodies repeat both query terms and win the FTS5 BM25
    ordering, monopolising the `_PREFILTER_CAP` slice. The target
    mentions each term twice inside a long body — length normalisation
    drops it past the cap — but the keyword scorer caps per-term TF at
    2, so over the FULL corpus it ties the decoys and its freshness wins
    rank 1. It is also the only project-scoped memory written from the
    caller's repo, so holding rank 1 lets `_caller_in_top_hit_project`
    suppress the verdict to `ok`."""
    from bettermemory import index
    from bettermemory.origin import Origin

    store = Store(mem_dir)
    dense = "alpha beta " * 6
    for i in range(60):
        store.write(
            content=f"{dense}filler-{i}", scopes=["infrastructure"], origin=None
        )
    padding = " ".join(f"pad{j}" for j in range(200))
    target = store.write(
        content=f"alpha beta alpha beta {padding} tail",
        scopes=["projects:repo-a"],
        origin=Origin(cwd=worktree, repo=_STARVED_REPO, worktree_root=worktree),
    )
    now = datetime.now(timezone.utc)
    for path, mem in store.iter_active():
        age = timedelta(minutes=30) if mem.id == target.id else timedelta(days=30)
        stamp = now - age
        store._write_path(
            path, mem.model_copy(update={"created": stamp, "updated": stamp})
        )
    index.rebuild(mem_dir, store.iter_active())
    top = {cid for cid, _ in index.query(mem_dir, _STARVED_QUERY, max_results=50)}
    assert len(top) == 50, "prefilter slice is not saturated — densify the decoys"
    assert target.id not in top, (
        "precondition drift: the target landed inside the FTS top-50, so "
        "production and a load_all probe would see the same pool"
    )
    return target.id


def test_run_audit_ranks_productions_candidate_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Stop hook — the PRIMARY production audit producer — must rank
    production's candidate pool, not an unconditional `load_all()`.

    Pre-fix it loaded the whole store while `memory_search` ranked the
    `_PREFILTER_CAP`-capped FTS slice. The probe's pool was a strict
    superset, and the miss verdict reads ONLY the rank-1 hit, so a
    memory the prefilter would have dropped could take that slot: here
    the caller's own project memory, which `_caller_in_top_hit_project`
    suppresses to `ok` while production's real rank-1 is a global memory
    worth flagging. Asserting the verdict alone would be weak — the
    rank-1 identity is what the pool decides, so pin both."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    from bettermemory.config import (
        BehaviorConfig,
        Config,
        StorageConfig,
        TelemetryConfig,
    )
    from bettermemory.origin import Origin as _Origin

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    worktree = str(tmp_path / "repo-a-wt")
    target_id = _write_prefilter_starved_store(mem_dir, worktree)
    monkeypatch.setattr(
        "bettermemory.hook.capture_origin",
        lambda *a, **k: _Origin(
            cwd=worktree, repo=_STARVED_REPO, worktree_root=worktree
        ),
    )

    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=True),
        behavior=BehaviorConfig(search_mode="keyword"),
    )
    result = run_audit(
        user_message=_STARVED_QUERY,
        assistant_response=None,
        session_id="claude-starved",
        config=cfg,
    )
    assert result["recent_retrieval_count"] == 0
    assert result["top_hits"][0]["id"] != target_id, (
        "the probe ranked the past-the-cap memory production's prefilter "
        "would never have surfaced"
    )
    assert result["top_hits"][0]["scopes"] == ["infrastructure"]
    assert result["verdict"] == "miss"
    misses = [e for e in iter_events(mem_dir) if e["kind"] == "search_miss"]
    assert len(misses) == 1


# ---------------------------------------------------------------------------
# UserPromptSubmit recall — the probe's verdict delivered before the turn.
#
# `run_prompt_recall` is `run_audit`'s predicate re-aimed (shared
# `_probe_message`), so this family does not re-test the shields the
# run_audit family above already pins. What it pins is the DELIVERY
# contract: inject exactly on a would-be miss, record the injection as a
# retrieval-kind event, refuse to fire off the books, and never disturb
# the server-session anchor the Stop hook bridges through.
# ---------------------------------------------------------------------------


def test_run_prompt_recall_injects_on_would_be_miss(tmp_path: Path) -> None:
    """Happy path: high-relevance hit, no recent retrieval — the recall
    fires. The block carries the id (the pointer), the scopes, and the
    verify-first instruction; the `prompt_recall` event carries
    `injected_chars == len(block)` so the per-turn context cost the
    resident-footprint suite deliberately does not measure stays
    measurable from the log."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    memory_id = _write_miss_memory(mem_dir)

    block = run_prompt_recall(
        prompt=_MISS_QUERY,
        session_id="transcript-recall",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert block is not None
    assert memory_id in block
    assert "memory_show" in block
    assert "infrastructure" in block

    events = list(iter_events(mem_dir))
    recalls = [e for e in events if e["kind"] == "prompt_recall"]
    assert len(recalls) == 1
    event = recalls[0]
    assert event["triggered_from"] == "prompt_hook"
    assert event["injected_chars"] == len(block)
    assert event["top_hits"][0]["id"] == memory_id
    assert event["session"] == "transcript-recall"


def test_run_prompt_recall_silent_when_probe_says_ok(tmp_path: Path) -> None:
    """A recent retrieval in the window means the probe reports `ok`,
    and `ok` means NOTHING is injected and NOTHING is recorded — the
    common case must leave the log byte-identical, because the recall
    hook runs on every single prompt submission."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    memory_id = _write_miss_memory(mem_dir)

    recorder = Recorder(root=mem_dir, session_id="transcript-recall", enabled=True)
    recorder.record("search", query=_MISS_QUERY, returned=[memory_id])
    before = list(iter_events(mem_dir))

    block = run_prompt_recall(
        prompt=_MISS_QUERY,
        session_id="transcript-recall",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert block is None
    assert list(iter_events(mem_dir)) == before


def test_run_prompt_recall_self_suppresses_within_window(tmp_path: Path) -> None:
    """The delivered recall is itself a retrieval-kind event, so an
    immediate second high-scoring prompt probes `ok` — the anti-spam
    bound is `_RETRIEVAL_EVENT_KINDS` membership plus the attribution
    window, not a separate knob. One injection, then silence."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)
    cfg = _miss_config(mem_dir)

    first = run_prompt_recall(
        prompt=_MISS_QUERY,
        session_id="transcript-recall",
        config=cfg,  # type: ignore[arg-type]
    )
    second = run_prompt_recall(
        prompt=_MISS_QUERY,
        session_id="transcript-recall",
        config=cfg,  # type: ignore[arg-type]
    )
    assert first is not None
    assert second is None
    events = list(iter_events(mem_dir))
    assert len([e for e in events if e["kind"] == "prompt_recall"]) == 1


def test_stop_audit_reports_ok_after_recall(tmp_path: Path) -> None:
    """THE honesty property the whole design leans on: a turn served by
    an injection is not a SILENT miss. The Stop hook's audit of the
    same turn must see the `prompt_recall` event through the retrieval
    shield and report `ok` with no `search_miss` — otherwise every
    delivery would be double-counted as a failure of the contract it
    just fulfilled."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)
    cfg = _miss_config(mem_dir)

    block = run_prompt_recall(
        prompt=_MISS_QUERY,
        session_id="transcript-turn",
        config=cfg,  # type: ignore[arg-type]
    )
    assert block is not None

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response="using the stored strategy",
        session_id="transcript-turn",
        config=cfg,  # type: ignore[arg-type]
    )
    assert result["verdict"] == "ok"
    assert result["recent_retrieval_count"] >= 1
    kinds = [e["kind"] for e in iter_events(mem_dir)]
    assert "search_miss" not in kinds
    assert "turn_audited" in kinds


def test_run_prompt_recall_respects_knob_off(tmp_path: Path) -> None:
    """`[behavior] prompt_recall = false` restores purely opt-in
    retrieval: no injection, no probe side effects, no event log."""
    from bettermemory.config import (
        BehaviorConfig,
        Config,
        StorageConfig,
        TelemetryConfig,
    )

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=True),
        behavior=BehaviorConfig(prompt_recall=False),
    )
    block = run_prompt_recall(
        prompt=_MISS_QUERY, session_id="transcript-off", config=cfg
    )
    assert block is None
    assert list(iter_events(mem_dir)) == []


def test_run_prompt_recall_refuses_without_telemetry(tmp_path: Path) -> None:
    """With telemetry off the Recorder would drop the `prompt_recall`
    event, the Stop-hook shield would never see the delivery, and the
    same turn would re-flag as a miss — an unlogged injection is worse
    than none, so the recall path refuses to fire at all."""
    from bettermemory.config import Config, StorageConfig, TelemetryConfig

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=False),
    )
    block = run_prompt_recall(
        prompt=_MISS_QUERY, session_id="transcript-quiet", config=cfg
    )
    assert block is None
    assert list(iter_events(mem_dir)) == []


def test_recall_event_does_not_hijack_server_session_anchor() -> None:
    """`_latest_in_process_session` must skip `prompt_recall` rows the
    way it skips Stop-hook rows: both are stamped with members of
    `_OUT_OF_PROCESS_TRIGGERS`, and both carry Claude Code's transcript
    id — a different id space from the server's `sess_<hex>`. Admitting
    one as the anchor would hand the retrieval shield a session id that
    matches no server-emitted event, structurally killing it (the
    pre-2.6.x dead-shield failure, reintroduced through the new
    writer)."""
    events = [
        {"ts": "2026-08-05T10:00:00Z", "session": "sess_server", "kind": "search"},
        {
            "ts": "2026-08-05T10:00:05Z",
            "session": "transcript-abc",
            "kind": "prompt_recall",
            "triggered_from": "prompt_hook",
        },
    ]
    assert _latest_in_process_session(events) == "sess_server"


def test_render_recall_block_caps_snippet_never_instructions() -> None:
    """The cap truncates the SNIPPET, not the frame: a recall whose
    verify-first instruction got cut would deliver a pointer without
    the discipline that makes pointers safe. Pathological snippet in,
    bounded block out, instructions intact."""
    from bettermemory.audit import MissHit, MissReport, THRESHOLD_RULE_V1
    from bettermemory.hook import _RECALL_BLOCK_CAP_CHARS
    from bettermemory.models import utcnow

    hit = MissHit(
        id="01TESTRECALLCAP0000000000",
        score=1.0,
        relevance="high",
        scopes=("infrastructure",),
        snippet="x" * (3 * _RECALL_BLOCK_CAP_CHARS),
        matched_unique=2,
        query_unique=2,
        relevance_v2="high",
    )
    report = MissReport(
        verdict="miss",
        checked_at=utcnow(),
        session_id="transcript-cap",
        lookback_seconds=600,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=(hit,),
        probe_query="cap probe",
    )
    block = _render_recall_block(report)
    assert len(block) <= _RECALL_BLOCK_CAP_CHARS
    assert "…" in block
    assert "memory_show" in block
    assert 'outcome="ignored"' in block
    assert hit.id in block


def test_render_recall_block_cap_holds_against_pathological_scope_list() -> None:
    """The cap guards the WHOLE block, scope list included. Scopes are
    caller data living in the FRAME the snippet math measures around,
    so pre-fix a pathological list pushed the render past the ceiling
    no matter how hard the snippet was cut — the exact hole the cap's
    comment claimed to cover. Post-fix the list is bounded to
    `_RECALL_SCOPES_CAP_CHARS` (whole names, ellipsis tail) and the
    verify-first instructions survive verbatim.

    Mutation-soundness: reverting the scope bound makes the length
    assertion fail (~4KB render); routing the bound through name
    truncation instead of whole-name drops breaks the whole-name
    assertion."""
    from bettermemory.audit import MissHit, MissReport, THRESHOLD_RULE_V1
    from bettermemory.hook import _RECALL_BLOCK_CAP_CHARS, _RECALL_SCOPES_CAP_CHARS
    from bettermemory.models import utcnow

    scopes = tuple(f"projects:very-long-scope-name-{i:04d}" for i in range(120))
    hit = MissHit(
        id="01TESTRECALLSCOPECAP00000",
        score=1.0,
        relevance="high",
        scopes=scopes,
        snippet="y" * 400,
        matched_unique=2,
        query_unique=2,
        relevance_v2="high",
    )
    report = MissReport(
        verdict="miss",
        checked_at=utcnow(),
        session_id="transcript-scope-cap",
        lookback_seconds=600,
        recent_retrieval_count=0,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=(hit,),
        probe_query="scope cap probe",
    )
    block = _render_recall_block(report)
    assert len(block) <= _RECALL_BLOCK_CAP_CHARS, len(block)
    # The rendered scope run is bounded and marked, with only WHOLE
    # scope names kept ahead of the ellipsis.
    assert ", \u2026" in block or ", …" in block
    rendered_scopes = block.split("[", 1)[1].split("]", 1)[0]
    assert len(rendered_scopes) <= _RECALL_SCOPES_CAP_CHARS + len(", …")
    for name in rendered_scopes.split(", ")[:-1]:
        assert name in scopes, name
    assert "memory_show" in block
    assert 'outcome="ignored"' in block
    assert hit.id in block


def test_prompt_main_injects_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end through the CLI shim: UserPromptSubmit payload on
    stdin, block on stdout (Claude Code injects stdout verbatim), exit
    0. The printed text is the rendered block plus print's newline —
    `injected_chars` on the event measures the block, not the pipe."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    memory_id = _write_miss_memory(mem_dir)

    payload = json.dumps(
        {
            "session_id": "transcript-cli",
            "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
            "prompt": _MISS_QUERY,
        }
    )
    monkeypatch.setattr("sys.stdin", _StdinMock(payload.encode("utf-8")))
    code = prompt_main([])
    assert code == 0
    out = capsys.readouterr().out
    assert memory_id in out
    recalls = [e for e in iter_events(mem_dir) if e["kind"] == "prompt_recall"]
    assert len(recalls) == 1
    assert recalls[0]["injected_chars"] == len(out.rstrip("\n"))


def test_prompt_main_no_op_when_payload_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty stdin — silent no-op, exit 0, no store side effects. Same
    contract as the Stop hook's `main`, and it matters more here: this
    hook fires on every prompt submission."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr("sys.stdin", _StdinMock(b""))
    code = prompt_main([])
    assert code == 0
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "mem" / ".events.jsonl").exists()


def test_prompt_main_no_op_when_stdin_oversized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Over the 64 KiB payload cap — same bucket as malformed JSON:
    silent no-op. A pathological pipe writer must not hold the prompt
    hostage while the process buffers garbage."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr("sys.stdin", _StdinMock(b"x" * (128 * 1024)))
    code = prompt_main([])
    assert code == 0
    assert capsys.readouterr().out == ""


def test_prompt_main_no_op_without_prompt_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A payload with a session id but no usable `prompt` (absent, or a
    non-string shape a misbehaving upstream hook rewrote) is a silent
    no-op — never a crash, never an injection built from `str(None)`."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "mem"))
    for payload in (
        {"session_id": "s"},
        {"session_id": "s", "prompt": 42},
        {"session_id": "s", "prompt": ""},
    ):
        monkeypatch.setattr(
            "sys.stdin", _StdinMock(json.dumps(payload).encode("utf-8"))
        )
        code = prompt_main([])
        assert code == 0
        assert capsys.readouterr().out == ""


def test_prompt_main_swallows_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blown-up store must land on stderr and exit 0 — this hook sits
    between the user and the model on every prompt, so the failure
    contract is stricter than anywhere else in the product."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    _write_miss_memory(mem_dir)

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("synthetic recall failure")

    monkeypatch.setattr("bettermemory.hook.run_prompt_recall", _boom)
    monkeypatch.setattr(
        "sys.stdin",
        _StdinMock(
            json.dumps({"session_id": "s", "prompt": _MISS_QUERY}).encode("utf-8")
        ),
    )
    code = prompt_main([])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "synthetic recall failure" in captured.err
