"""Tests for `bettermemory.hook` — the Claude Code Stop hook entry
point that fires the silent-miss audit at end-of-turn.

The MCP `memory_audit_turn` tool was originally the only invocation
surface; in practice models forget to call it and the silent-miss
telemetry stayed dormant for plugin users. This module is the
client-side hook that closes that loop. Tests here cover:

- transcript-JSONL parsing (`transcript.stop_turn`): the turn the Stop
  names, its owner prompt and its reply, defensive against malformed
  lines; the full battery is `test_transcript.py`.
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
from typing import Any

import pytest

from bettermemory.events import Recorder
from bettermemory.hook import (
    _flatten_assistant_content,
    _pending_retrievals,
    _read_payload,
    audit_transcript,
    human_prompt,
    is_synthetic_prompt,
    main as hook_main,
    prompt_main,
    run_audit,
    run_prompt_recall,
)
from bettermemory.membership import RecordHome
from bettermemory.models import Memory
from bettermemory.sessions import (
    _disabled_scopes_from_events,
    _latest_in_process_session,
)
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.transcript import stop_turn

from . import _transcript_rows as tr
from .test_membership_security import checkout

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


def _turn(
    path: Path, prompt_id: str | None = None, **limits: int
) -> tuple[str | None, str | None, str | None] | None:
    """`transcript.stop_turn` as (prompt, reply, model), or None."""
    turn = stop_turn(path, prompt_id, **limits)
    return None if turn is None else (turn.prompt, turn.reply, turn.model)


def _events(mem_dir: Path) -> list[dict[str, Any]]:
    """Every telemetry event the store under `mem_dir` holds, oldest first."""
    with Store.open(mem_dir) as store:
        return list(store.iter_events())


def _backdate(store: Store, memory: Memory, age: timedelta) -> None:
    """Re-put `memory` with `created` and `updated` moved `age` into the
    past. The put is logged like any write, so the row keeps a verified
    pointer into the log and still reads as local."""
    stamp = datetime.now(timezone.utc) - age
    store.put_memory(memory.model_copy(update={"created": stamp, "updated": stamp}))


def test_stop_turn_reads_the_newest_turn(tmp_path: Path) -> None:
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
    assert _turn(transcript) == ("latest ask", "latest reply", None)


def test_stop_turn_skips_thinking_and_tool_use(
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
    assert _turn(transcript) == ("ask", "user-visible reply", None)


def test_stop_turn_handles_missing_file(tmp_path: Path) -> None:
    """A transcript path that doesn't exist must return None rather than
    raising. The hook handles the None case by no-oping."""
    assert _turn(tmp_path / "missing.jsonl") is None


def test_stop_turn_tolerates_malformed_lines(tmp_path: Path) -> None:
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
    assert _turn(transcript) == ("first", "reply", None)


def test_stop_turn_survives_unicode_line_separators(
    tmp_path: Path,
) -> None:
    """Regression: the reverse walk split the tail with `splitlines()`,
    which also breaks on U+2028/U+2029/U+0085 — characters that are
    legal RAW inside JSON strings and that real serializers emit
    unescaped (Node's `JSON.stringify`, Python's
    `json.dumps(ensure_ascii=False)`). A transcript row whose content
    carried one shattered into fragments that all failed `json.loads`
    and were silently skipped, so the walk anchored on an OLDER
    exchange: the Stop audit probed a stale user message and
    attribution matched against a stale reply. The JSONL framing
    contract is bytes-level \\n; the parser must split exactly on
    that."""
    latest_ask = "why did the deploy\u2028script fail"
    latest_reply = "the runner's env file\u2029was missing"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in [
                {"type": "user", "message": {"content": "stale ask"}},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "stale reply"}]},
                },
                {"type": "user", "message": {"content": latest_ask}},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": latest_reply}]},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # The seed must hold the separators RAW in the FILE (the
    # ensure_ascii=False dump above writes them unescaped, as Node's
    # JSON.stringify does) — a backslash-escaped sequence in the file
    # would never trip splitlines() and the test would pin nothing.
    raw = transcript.read_bytes().decode("utf-8")
    assert "\u2028" in raw and "\u2029" in raw
    assert _turn(transcript) == (latest_ask, latest_reply, None)


def test_stop_turn_passes_a_task_notification_inside_the_turn(
    tmp_path: Path,
) -> None:
    """Regression: background-task completions inject a `type="user"` row
    whose content is a `<task-notification>` envelope AFTER the human's
    message. The reverse walk used to capture that synthetic payload as
    the user message, so the probe audited notification text and real
    silent misses on agentic turns were structurally suppressed. A
    notification delivered into the turn carries the turn's promptId,
    and the walk passes it to the owner's row of the same turn."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "promptId": "p1",
            "message": {"content": "how do we deploy myapp to staging again?"},
        },
        {
            "type": "user",
            "promptId": "p1",
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
    expected = ("how do we deploy myapp to staging again?", "deployed", None)
    assert _turn(transcript) == expected
    assert _turn(transcript, "p1") == expected


def test_a_notification_without_a_prompt_id_is_its_own_turn(tmp_path: Path) -> None:
    """RS-F5, changed deliberately from the walk that skipped the row: in a
    transcript whose rows carry no promptId, a submitted row is its own
    turn, so a notification after the owner's row is a newer turn the
    harness composed. The Stop reads it as a turn with no owner prompt
    and never anchors to the older row."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "how do we deploy myapp?"}},
        {
            "type": "user",
            "message": {"content": "<task-notification>done</task-notification>"},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "deployed"}]},
        },
    )
    assert _turn(transcript) == (None, "deployed", None)


def test_stop_turn_skips_meta_and_wrapper_rows(tmp_path: Path) -> None:
    """Regression: slash-command turns record the command bookkeeping
    (`<command-name>` wrapper rows) and the full skill expansion (an
    `isMeta: true` row) as `type="user"` rows ABOVE the assistant reply,
    under the turn's promptId. The walk used to return the expansion's
    documentation prose as "the user's own words" — violating the
    proposals extractor's stated precondition and shadowing whatever the
    human actually typed. Both shapes must be skipped so the genuine row
    wins."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "promptId": "p1",
            "message": {"content": "/plugin-marketplace docs"},
        },
        {
            "type": "user",
            "promptId": "p1",
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
            "promptId": "p1",
            "message": {"content": "<command-name>/plugin-marketplace</command-name>"},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "docs shown"}]},
        },
    )
    assert _turn(transcript) == ("/plugin-marketplace docs", "docs shown", None)


def test_stop_turn_skips_empty_user_rows(tmp_path: Path) -> None:
    """An empty-string user row of the turn (observed in real transcripts)
    used to be captured verbatim, and main()'s `if not user` then silently
    dropped the whole audit. The walk keeps going to the owner's row."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "promptId": "p1", "message": {"content": "real question"}},
        {"type": "user", "promptId": "p1", "message": {"content": ""}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "answer"}]},
        },
    )
    assert _turn(transcript) == ("real question", "answer", None)


def test_stop_turn_says_no_owner_prompt_when_only_synthetic_rows(
    tmp_path: Path,
) -> None:
    """RS-F5, changed deliberately: a turn whose rows are all composed by
    the harness is read as a turn with no owner prompt (the Stop records
    `no_owner_prompt`) instead of nothing; skipping must still not invent
    a prompt. A reminder-only row and a meta row are such rows."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "promptId": "p1",
            "message": {"content": "<system-reminder>reminder</system-reminder>"},
        },
        {
            "type": "user",
            "promptId": "p1",
            "isMeta": True,
            "message": {"content": "expanded skill markdown"},
        },
    )
    assert _turn(transcript) == (None, None, None)


def test_human_prompt_rejects_what_the_harness_composed() -> None:
    """Rows the human did not type, by flag or by opening: the wrapper the
    transcript keeps a message from another session behind, the tagged
    body the hook saw, the app's side-session note, a compaction summary.
    A row that names a tag after its first character is the human's."""

    def row(text: str, **flags: Any) -> dict[str, Any]:
        return {"type": "user", "message": {"content": text}, **flags}

    body = '<agent-message from="a" name="b">\nhello\n</agent-message>'
    assert human_prompt(row(f"Another Claude session sent a message:\n{body}")) is None
    assert human_prompt(row(body)) is None
    assert (
        human_prompt(row('<cross-session-message from="b">hi</cross-session-message>'))
        is None
    )
    assert (
        human_prompt(row("<side-sessions-event>\ndone\n</side-sessions-event>")) is None
    )
    assert human_prompt(row("  <scheduled-task>\nrun\n</scheduled-task>")) is None
    assert (
        human_prompt(
            row(
                "This session is being continued from a previous", isCompactSummary=True
            )
        )
        is None
    )
    typed = "why did the <task-notification> fire twice"
    assert human_prompt(row(typed)) == typed


def test_is_synthetic_prompt_is_the_one_prefix_test() -> None:
    """The same test the prompt recall applies to the UserPromptSubmit
    payload, which carries no flag at all."""
    assert is_synthetic_prompt("  <task-notification>\n<task-id>x</task-id>")
    assert is_synthetic_prompt('<agent-message from="a" name="b">')
    assert is_synthetic_prompt("Another Claude session sent a message while you were")
    assert not is_synthetic_prompt("ship the deploy")
    assert not is_synthetic_prompt("the <task-notification> fired twice")


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
    assert not (tmp_path / STORE_FILENAME).exists()


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
    # Oversized payload is treated as "nothing to audit": the audit
    # branch is never reached, so no store is opened and no event lands.
    assert not (tmp_path / STORE_FILENAME).exists()


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
    assert not (tmp_path / "mem" / STORE_FILENAME).exists()


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
    events = _events(mem_dir)
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
    events = _events(mem_dir)
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
    cleanly but record no event."""
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
    from bettermemory.config import Config, StorageConfig, TelemetryConfig
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
    assert _events(mem_dir) == [], (
        "telemetry-disabled config must suppress the Stop-hook events; got some anyway"
    )


def test_dry_run_reports_without_writing_any_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`audit-turn --dry-run` must compute the audit and write nothing.

    A normal run SETTLES the turn's pending retrievals, which is correct
    for the Stop hook and a trap for a human running the command to look
    at a store: inspection silently mutates usage telemetry. The manual
    path is worse still, because a caller-supplied `--session-id`
    defeats the settlement dedup — `_emit_hook_attributions` builds
    `used_session_ids` from the retrieval session and the supplied id,
    while prior hook attributions were recorded under the REAL
    transcript id — so a fabricated id hides them and already-settled
    retrievals settle again. That is not hypothetical: on 2026-09-16 a
    read-only review agent ran `--session-id doctor-probe-ro-audit`
    against this very store and re-settled 17 of 23 ids.

    Paired assertions, because "wrote nothing" is only meaningful beside
    "would have written something": the same transcript and store, run
    twice.

    Negative control: neuter the `if dry_run:` telemetry override in
    `run_audit` and the second half fails.
    """

    def _fixture(dirname: str) -> Path:
        mem_dir = tmp_path / dirname
        mem_dir.mkdir()
        Store(mem_dir).write(
            content="My postgres database is on port 5433, not the default 5432.",
            scopes=["infrastructure"],
        )
        return mem_dir

    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "what port is my postgres on?"}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Postgres is usually on 5432."}]
            },
        },
    )

    # Baseline: a normal run writes.
    wet = _fixture("wet")
    monkeypatch.setenv("BETTERMEMORY_DIR", str(wet))
    assert hook_main(["--transcript-path", str(transcript), "--session-id", "s1"]) == 0
    wet_events = _events(wet)
    assert wet_events, (
        "baseline run wrote nothing — fixture no longer exercises the path"
    )

    # The same inputs, dry: identical work, zero writes.
    dry = _fixture("dry")
    monkeypatch.setenv("BETTERMEMORY_DIR", str(dry))
    code = hook_main(
        ["--transcript-path", str(transcript), "--session-id", "s1", "--dry-run"]
    )
    assert code == 0
    assert _events(dry) == [], (
        "--dry-run wrote events; inspection must not mutate the store"
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
    assert not (mem_dir / STORE_FILENAME).exists()

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
    assert not (mem_dir / STORE_FILENAME).exists()


# ---------------------------------------------------------------------------
# Hook attribution — substring-matching retrieved memory bodies against the
# assistant reply and emitting `use` events with attribution="hook".
# ---------------------------------------------------------------------------


def _seed_search_event(mem_dir: Path, *, session_id: str, returned: list[str]) -> None:
    """Write a synthetic `search` event so the hook treats the listed
    memory_ids as recently retrieved in this session. Avoids spinning
    up the MCP server just to populate the precondition for an
    attribution test."""
    Recorder(store=Store(mem_dir), session_id=session_id).record(
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
    Recorder(store=Store(mem_dir), session_id=session_id).record(
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

    events = _events(mem_dir)
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

    events = _events(mem_dir)
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

    events = _events(mem_dir)
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
    Recorder(store=Store(mem_dir), session_id="sess-dup").record(
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

    events = _events(mem_dir)
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

    events = _events(mem_dir)
    use_events = [e for e in events if e["kind"] == "use"]
    assert len(use_events) == 1
    auto_event = use_events[0]
    assert auto_event["ids"] == [written.id]
    assert auto_event["outcome"] == "applied"
    assert auto_event["auto"] is True
    assert auto_event["attribution"] == "auto"
    assert auto_event["triggered_from"] == "stop_hook"


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
            "kind": "write",
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


def _miss_config(mem_dir: Path) -> object:
    from bettermemory.config import Config, StorageConfig, TelemetryConfig

    return Config(
        storage=StorageConfig(directory=str(mem_dir)),
        telemetry=TelemetryConfig(enabled=True),
    )


# A body + query pair that scores a deterministic high-relevance hit
# (mirrors the load-bearing case in test_audit.py).
_MISS_BODY = "backup strategy uses triangular restic replication"
_MISS_QUERY = "backup strategy"


def _global_home(store: Store, mem_dir: Path) -> RecordHome:
    """The home of a record written outside any project: the caller these
    audits run as stands in the plain test workspace, whose pool prefilter
    admits global records only (design 3.5)."""
    from bettermemory.location import resolve_caller

    return resolve_caller(store, _miss_config(mem_dir), str(mem_dir)).home()  # type: ignore[arg-type]


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
    written = store.write(
        content=_MISS_BODY,
        scopes=["infrastructure"],
        home=_global_home(store, mem_dir),
    )
    _backdate(store, written, timedelta(hours=1))
    return written.id


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
    Recorder(store=Store(mem_dir), session_id="sess_server").record("search")

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
        "bettermemory.origin.capture_caller",
        lambda *a, **k: Origin(worktree_root=wt_this),
    )

    # This window's server searched (stamped with this worktree)…
    Recorder(store=Store(mem_dir), session_id="sess_A", worktree_root=wt_this).record(
        "search"
    )
    # …then the other window's server wrote the LATEST event.
    Recorder(store=Store(mem_dir), session_id="sess_B", worktree_root=wt_other).record(
        "write"
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
        "bettermemory.origin.capture_caller",
        lambda *a, **k: Origin(worktree_root=wt_this),
    )

    # This window's server is alive (a non-retrieval write) but never
    # searched this turn…
    Recorder(store=Store(mem_dir), session_id="sess_A", worktree_root=wt_this).record(
        "write"
    )
    # …while the other window's server searched for its own topic.
    Recorder(store=Store(mem_dir), session_id="sess_B", worktree_root=wt_other).record(
        "search"
    )

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
        "bettermemory.origin.capture_caller",
        lambda *a, **k: Origin(worktree_root=wt_this),
    )

    # This worktree's first server session searched within the window…
    Recorder(store=Store(mem_dir), session_id="sess_A", worktree_root=wt_this).record(
        "search"
    )
    # …then a second SAME-worktree session wrote the latest in-process
    # event (a non-retrieval `write`, so only the orphaned search can
    # feed the shield), flipping the anchor to sess_B.
    Recorder(store=Store(mem_dir), session_id="sess_B", worktree_root=wt_this).record(
        "write"
    )

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
    written = store.write(
        content=_MISS_BODY,
        scopes=["infrastructure"],
        home=_global_home(store, mem_dir),
    )
    # Backdate to 300s — outside the 60s creation shield, INSIDE the
    # hook's 600s attribution lookback. (`_write_miss_memory`'s 1h
    # backdate clears both windows, so it cannot discriminate.)
    _backdate(store, written, timedelta(seconds=300))

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
    Recorder(store=Store(mem_dir), session_id="sess_server").record(
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

    rec = Recorder(store=Store(mem_dir), session_id="sess_server")
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
    Recorder(store=Store(mem_dir), session_id="sess_old").record(
        "scope_disable", scope="infrastructure"
    )
    Recorder(store=Store(mem_dir), session_id="sess_new").record("write")

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
    Recorder(store=Store(mem_dir), session_id="sess_old").record(
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

    # Backdate the server's search event 120s. The recorder always
    # stamps "now", so append the row directly in the recorder's shape
    # with the older `ts`, signed into the chain like any other row.
    ts = (utcnow() - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    store = Store(mem_dir)
    with store._transaction() as tx:
        store._append(tx, "search", {"returned": []}, session="sess_server", ts=ts)

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
    # as_posix(): a Windows tmp_path's backslashes would be (invalid)
    # escape sequences inside a TOML basic string; forward slashes are
    # valid TOML and a path Windows Python opens fine.
    config_path.write_text(
        f'[storage]\ndirectory = "{mem_dir.as_posix()}"\n\n'
        '[behavior]\nsearch_mode = "semantic"\n',
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
    audited = [e for e in _events(mem_dir) if e["kind"] == "turn_audited"]
    assert len(audited) == 1
    assert audited[0]["probe_mode"] == "hybrid"


def test_run_audit_threads_ranker_config_into_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Stop hook must hand the probe the configured ranker knobs —
    `recency_boost_half_life_days` (and `conversational`, pinned by its
    own test below) — and the shared attribution window as the probe's
    lookback, not the old hardcoded 60s. Pre-fix the probe ranked with
    hardwired defaults for every config. The usage multipliers left in
    9.0.0, so no tally reaches the probe."""
    from bettermemory.audit import probe_for_miss as real_probe
    from bettermemory.config import BehaviorConfig, Config, StorageConfig

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    captured: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_probe(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bettermemory.recall.probe_for_miss", spy)
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        behavior=BehaviorConfig(recency_boost_half_life_days=7.0),
    )
    run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-cfg",
        config=cfg,
    )
    assert captured["half_life_days"] == 7.0
    assert captured["lookback_seconds"] == 600
    assert "applied_by_id" not in captured
    assert "negative_by_id" not in captured
    assert "rescue_expansion" not in captured


def test_run_audit_threads_conversational_opt_out_into_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`[behavior] conversational = false` must reach the probe. The
    shared `_probe_message` body — one body serving BOTH hook producers
    (the Stop audit and the prompt-recall injection lane) precisely so
    they cannot drift — omitted `conversational` from its
    `probe_for_miss` call, so the hook ranked with the Lane L
    conversational rerank forced ON (the probe's default) under every
    config, while production `memory_search` and the MCP audit producer
    both threaded the knob. On an opted-out store with a temporal-phrase
    prompt the probe then scored with a different ranker than the
    model's actual retrieval — masked and phantom misses, and a recall
    injection production's own ranking would never have surfaced. The
    flag is read off the config beside the recency half-life."""
    from bettermemory.audit import probe_for_miss as real_probe
    from bettermemory.config import BehaviorConfig, Config, StorageConfig

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)

    captured: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_probe(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("bettermemory.recall.probe_for_miss", spy)
    cfg = Config(
        storage=StorageConfig(directory=str(mem_dir)),
        behavior=BehaviorConfig(conversational=False),
    )
    run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-conversational-off",
        config=cfg,
    )
    assert captured["conversational"] is False


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
    rank 1. It is also the only memory homed in the caller's project (the
    checkout at `worktree`; the decoys are global), so holding rank 1
    lets `_caller_in_top_hit_project` suppress the verdict to `ok`."""
    from bettermemory.location import resolve_caller
    from bettermemory.origin import Origin

    store = Store(mem_dir)
    plain = _global_home(store, mem_dir)
    dense = "alpha beta " * 6
    for i in range(60):
        store.write(
            content=f"{dense}filler-{i}",
            scopes=["infrastructure"],
            origin=None,
            home=plain,
        )
    padding = " ".join(f"pad{j}" for j in range(200))
    target = store.write(
        content=f"alpha beta alpha beta {padding} tail",
        scopes=["projects:repo-a"],
        origin=Origin(cwd=worktree, repo=_STARVED_REPO, worktree_root=worktree),
        home=resolve_caller(
            store,
            _miss_config(mem_dir),  # type: ignore[arg-type]
            worktree,
        ).home(),
    )
    for mem in store.load_all():
        age = timedelta(minutes=30) if mem.id == target.id else timedelta(days=30)
        _backdate(store, mem, age)
    top = {cid for cid, _ in store.query_candidates(_STARVED_QUERY, max_results=50)}
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
    worktree = str(checkout(tmp_path / "repo-a-wt"))
    target_id = _write_prefilter_starved_store(mem_dir, worktree)
    monkeypatch.setattr(
        "bettermemory.origin.capture_caller",
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
    misses = [e for e in _events(mem_dir) if e["kind"] == "search_miss"]
    assert len(misses) == 1


def test_run_audit_declines_a_miss_when_git_could_not_be_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When nothing could be read where the caller stands (git not
    answering: no binary, a timeout), the caller may well be inside the
    project of the top hit, and the turn used to be logged as a
    `search_miss`. Its resolution is INDETERMINATE, which every surface
    admits nothing (design 3.5): the probe ranks nothing and reports
    `no_signal`, never a miss. The measured outside (git said "not a
    repository") still misses."""
    from bettermemory import location
    from bettermemory.origin import Origin

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    _write_miss_memory(mem_dir)
    cfg = _miss_config(mem_dir)
    unread = location.resolve_caller(Store(mem_dir), cfg, None).resolution  # type: ignore[arg-type]
    assert unread.outcome == "indeterminate"

    nowhere = Origin(cwd=str(tmp_path))
    monkeypatch.setattr("bettermemory.origin.capture_caller", lambda *a, **k: nowhere)
    with monkeypatch.context() as patched:
        patched.setattr(location, "resolve_hook", lambda *a, **k: unread)
        result = run_audit(
            user_message=_MISS_QUERY,
            assistant_response=None,
            session_id="claude-origin-unknown",
            config=cfg,  # type: ignore[arg-type]
        )
    assert result["verdict"] == "no_signal"
    assert result["top_hits"] == []
    assert not [e for e in _events(mem_dir) if e["kind"] == "search_miss"]

    result = run_audit(
        user_message=_MISS_QUERY,
        assistant_response=None,
        session_id="claude-origin-nowhere",
        config=_miss_config(mem_dir),  # type: ignore[arg-type]
    )
    assert result["verdict"] == "miss"


# ---------------------------------------------------------------------------
# UserPromptSubmit — the CLI shim over the recall surface.
#
# The delivery rules live in `recall.prompt_recall` and are pinned in
# tests/test_recall_prompt.py. What this family pins is the hook's own
# contract: payload on stdin, block on stdout, exit 0 always, and the
# recall's event never disturbing the server-session anchor the Stop hook
# bridges through.
# ---------------------------------------------------------------------------

_HOOK_REPO = "git@github.com:owner/foo.git"


def _pin_caller_in_repo(monkeypatch: pytest.MonkeyPatch, worktree: Path) -> None:
    """Stand the hook's caller in the checkout at `worktree`, whatever
    directory the payload names: the recall delivers only memories homed
    in the caller's project (design 3.5)."""
    from bettermemory.origin import Origin

    caller = Origin(cwd=str(worktree), repo=_HOOK_REPO, worktree_root=str(worktree))
    monkeypatch.setattr("bettermemory.origin.capture_caller", lambda *a, **k: caller)


def _write_project_memory(mem_dir: Path, worktree: Path) -> str:
    """The high-relevance memory, written in the checkout at `worktree`
    (homed in its project) and backdated past the probe's creation
    shield."""
    from bettermemory.location import resolve_caller
    from bettermemory.origin import Origin

    store = Store(mem_dir)
    written = store.write(
        content=_MISS_BODY,
        scopes=["projects:foo"],
        origin=Origin(cwd=str(worktree), repo=_HOOK_REPO, worktree_root=str(worktree)),
        home=resolve_caller(
            store,
            _miss_config(mem_dir),  # type: ignore[arg-type]
            str(worktree),
        ).home(),
    )
    _backdate(store, written, timedelta(hours=1))
    return written.id


def test_stop_audit_reports_ok_after_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE honesty property the design leans on: a turn served by a
    delivery is not a SILENT miss. The Stop hook's audit of the same turn
    sees the `prompt_recall` event through the retrieval shield and
    reports `ok` with no `search_miss`."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    worktree = checkout(tmp_path / "foo")
    _write_project_memory(mem_dir, worktree)
    _pin_caller_in_repo(monkeypatch, worktree)
    cfg = _miss_config(mem_dir)

    block = run_prompt_recall(
        prompt=_MISS_QUERY,
        session_id="transcript-turn",
        cwd=worktree,
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
    kinds = [e["kind"] for e in _events(mem_dir)]
    assert "search_miss" not in kinds
    assert "turn_audited" in kinds


def test_recall_event_does_not_hijack_server_session_anchor() -> None:
    """`_latest_in_process_session` must skip `prompt_recall` and
    `session_brief` rows the way it skips Stop-hook rows: all three are
    stamped with members of `_OUT_OF_PROCESS_TRIGGERS` and carry Claude
    Code's transcript id, a different id space from the server's
    `sess_<hex>`. Admitting one as the anchor would hand the retrieval
    shield a session id that matches no server-emitted event."""
    events = [
        {"ts": "2026-08-05T10:00:00Z", "session": "sess_server", "kind": "search"},
        {
            "ts": "2026-08-05T10:00:05Z",
            "session": "transcript-abc",
            "kind": "prompt_recall",
            "triggered_from": "prompt_hook",
        },
        {
            "ts": "2026-08-05T10:00:06Z",
            "session": "transcript-abc",
            "kind": "session_brief",
            "triggered_from": "session_hook",
        },
    ]
    assert _latest_in_process_session(events) == "sess_server"


def test_prompt_main_injects_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end through the CLI shim: UserPromptSubmit payload on
    stdin, block on stdout (Claude Code injects stdout verbatim), exit
    0. The printed text is the rendered block plus print's newline —
    `injected_chars` on the event measures the block, not the pipe. The
    payload's `prompt_id` and `transcript_path` ride onto the event."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    worktree = checkout(tmp_path / "foo")
    memory_id = _write_project_memory(mem_dir, worktree)
    _pin_caller_in_repo(monkeypatch, worktree)

    payload = json.dumps(
        {
            "session_id": "transcript-cli",
            "transcript_path": str(tmp_path / "t.jsonl"),
            "cwd": str(worktree),
            "hook_event_name": "UserPromptSubmit",
            "prompt": _MISS_QUERY,
            "prompt_id": "turn-7",
        }
    )
    monkeypatch.setattr("sys.stdin", _StdinMock(payload.encode("utf-8")))
    code = prompt_main([])
    assert code == 0
    out = capsys.readouterr().out
    assert memory_id in out
    recalls = [e for e in _events(mem_dir) if e["kind"] == "prompt_recall"]
    assert len(recalls) == 1
    assert recalls[0]["injected_chars"] == len(out.rstrip("\n"))
    assert recalls[0]["prompt_id"] == "turn-7"
    assert recalls[0]["transcript_path"] == str(tmp_path / "t.jsonl")


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
    assert not (tmp_path / "mem" / STORE_FILENAME).exists()


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


# ---------------------------------------------------------------------------
# review_unanswered: the review chains the conversation left open (D19)
# ---------------------------------------------------------------------------

#: The Stop audit's two rows keep their field sets exactly: the
#: unanswered-review count is its own kind, never a field on these.
_TURN_AUDITED_KEYS = {
    "actor",
    "assistant_present",
    "kind",
    "lookback_seconds",
    "probe_mode",
    "probe_query",
    "recent_retrieval_count",
    "rule",
    "session",
    "session_id",
    "threshold_rule",
    "top_hits",
    "triggered_from",
    "ts",
    "verdict",
    "worktree_root",
}
_SEARCH_MISS_KEYS = {
    "actor",
    "event_id",
    "kind",
    "lookback_seconds",
    "probe_query",
    "recent_retrieval_count",
    "rule",
    "session",
    "session_id",
    "threshold_rule",
    "top_hits",
    "triggered_from",
    "ts",
    "worktree_root",
}


@pytest.fixture
def review_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from .test_recall_prompt import PromptWorld

    world = PromptWorld(tmp_path)
    monkeypatch.setattr(
        "bettermemory.origin.capture_caller", lambda *a, **k: world.caller
    )
    return world


def _review_row(
    store: Store, *, session: str = "sess_srv", parent: str | None = None
) -> str:
    from bettermemory.models import generate_ulid

    review_id = generate_ulid()
    Recorder(store=store, session_id=session).record(
        "review_issued",
        review_id=review_id,
        related=[],
        client="none",
        model="none",
        pre_answered=False,
        parent_review_id=parent,
    )
    return review_id


def _write_row(store: Store, *, session: str = "sess_srv", **fields: Any) -> None:
    Recorder(store=store, session_id=session).record("write", **fields)


def _stop(world: Any, **kw: Any) -> dict[str, Any]:
    from .test_recall_prompt import QUERY

    kw.setdefault("session_id", "transcript-review")
    kw.setdefault("config", world.config)
    kw.setdefault("store", world.store)
    return run_audit(user_message=QUERY, assistant_response=None, **kw)


def _unanswered(store: Store) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e["kind"] == "review_unanswered"]


def test_an_open_review_chain_is_counted(review_world: Any) -> None:
    root = _review_row(review_world.store)
    _stop(review_world)
    [row] = _unanswered(review_world.store)
    assert row["count"] == 1
    assert row["root_ids"] == [root]
    assert row["session_id"] == "transcript-review"
    assert row["session"] == "transcript-review"
    assert row["review_sessions"] == ["sess_srv"]
    assert row["segment"] == 0
    assert row["harness"] == "claude-code"
    # A hook row: the in-process anchor walk must skip it.
    assert row["triggered_from"] == "stop_hook"


def test_a_decided_chain_is_not_counted(review_world: Any) -> None:
    root = _review_row(review_world.store)
    _write_row(review_world.store, status="committed", review_id=root)
    child_root = _review_row(review_world.store)
    child = _review_row(review_world.store, parent=child_root)
    _write_row(review_world.store, status="committed", review_id=child)
    _stop(review_world)
    assert _unanswered(review_world.store) == []


def test_a_resend_refused_by_another_gate_reads_open(review_world: Any) -> None:
    """The dedup gate refused the re-send before the review decided: its
    write row carries no review id, so the chain is still open, and the
    count means unanswered or refused."""
    root = _review_row(review_world.store)
    _write_row(review_world.store, status="duplicate", scopes=["tools"])
    _stop(review_world)
    [row] = _unanswered(review_world.store)
    assert row["root_ids"] == [root]


def test_a_root_is_counted_once_per_session(review_world: Any) -> None:
    from bettermemory.recall import RecallState

    root = _review_row(review_world.store)
    state = RecallState()
    _stop(review_world, state=state)
    # The next turn re-sends and meets a follow-up review of the same
    # chain: same root, not counted again, with the daemon's state or
    # with the state rebuilt from the log.
    _review_row(review_world.store, parent=root)
    _stop(review_world, state=state)
    _review_row(review_world.store, parent=root)
    _stop(review_world)
    assert [r["root_ids"] for r in _unanswered(review_world.store)] == [[root]]


def test_the_window_opens_at_the_previous_turn(review_world: Any) -> None:
    _stop(review_world)
    assert _unanswered(review_world.store) == []
    root = _review_row(review_world.store)
    _stop(review_world)
    assert [r["root_ids"] for r in _unanswered(review_world.store)] == [[root]]


def test_nothing_is_counted_with_telemetry_off(review_world: Any) -> None:
    from .test_recall_prompt import config

    _review_row(review_world.store)
    _stop(review_world, config=config(review_world.store_dir, telemetry=False))
    assert _unanswered(review_world.store) == []


def test_the_harness_join_reads_this_conversations_server(review_world: Any) -> None:
    """The rows are read under the session the harness stamp names; a
    later server of another conversation (the one the worktree bridge
    would pick) and its open review are left alone."""
    Recorder(store=review_world.store, session_id="sess_ours").record(
        "show", id="m-1", harness_session="transcript-review"
    )
    ours = _review_row(review_world.store, session="sess_ours")
    _review_row(review_world.store, session="sess_theirs")
    _stop(review_world)
    [row] = _unanswered(review_world.store)
    assert row["root_ids"] == [ours]
    assert row["review_sessions"] == ["sess_ours"]


def test_the_stop_rows_keep_their_field_sets(review_world: Any) -> None:
    review_world.write(origin=None, scopes=["infrastructure"])
    _review_row(review_world.store)
    result = _stop(review_world)
    assert result["verdict"] == "miss"
    events = list(review_world.store.iter_events())
    [audited] = [e for e in events if e["kind"] == "turn_audited"]
    [miss] = [e for e in events if e["kind"] == "search_miss"]
    assert set(audited) == _TURN_AUDITED_KEYS
    assert set(miss) == _SEARCH_MISS_KEYS
    assert len(_unanswered(review_world.store)) == 1


# ---------------------------------------------------------------------------
# RS-F3: the Stop row's prompt_id and reply_source; the walk past the tail
# ---------------------------------------------------------------------------

_MIB = 1_048_576


def _tool_result_row(size: int) -> dict[str, Any]:
    """A user row carrying a tool result of about `size` bytes: never the
    human's text, and most of a real transcript's bytes."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "x" * size}
            ],
        },
    }


def _reply_row(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-test",
            "content": [{"type": "text", "text": text}],
        },
    }


def _long_turn(
    path: Path,
    *,
    after_prompt: int,
    human: bool = True,
    prompt_id: str | None = None,
) -> None:
    """A transcript whose newest turn has at least `after_prompt` bytes of
    tool results between the human's row and the reply: one row longer
    than a chunk, then rows of an odd size, so chunk boundaries fall
    inside rows. With `prompt_id` the turn's user rows carry it, as
    Claude Code stamps them, and the older turn carries another."""

    def stamped(row: dict[str, Any], turn: str | None) -> dict[str, Any]:
        return row if turn is None else {**row, "promptId": turn}

    older = None if prompt_id is None else f"{prompt_id}-older"
    rows: list[dict[str, Any]] = []
    if human:
        rows.append(
            stamped({"type": "user", "message": {"content": "stale ask"}}, older)
        )
        rows.append(_reply_row("stale reply"))
        rows.append(
            stamped({"type": "user", "message": {"content": "the long ask"}}, prompt_id)
        )
    rows.append(stamped(_tool_result_row(_MIB + _MIB // 2), prompt_id))
    written = _MIB + _MIB // 2
    while written < after_prompt:
        rows.append(stamped(_tool_result_row(300_007), prompt_id))
        written += 300_007
    rows.append(_reply_row("the long reply"))
    _write_transcript(path, *rows)


def _seed_store(mem_dir: Path) -> None:
    store = Store(mem_dir)
    store.write(content="alpha note about postgres", scopes=["infrastructure"])
    store.close()


def test_stop_turn_walks_back_past_the_tail(tmp_path: Path) -> None:
    """RS-F3: a turn whose tool results outgrew the 1 MiB tail. The tail
    holds the reply and tool results only; the walk continues toward the
    start of the file and finds the human's row about 3 MiB back. Every
    chunk boundary falls inside a row, and one row is longer than a
    chunk, so the carry between chunks is exercised."""
    transcript = tmp_path / "t.jsonl"
    _long_turn(transcript, after_prompt=3 * _MIB, prompt_id="p1")
    data = transcript.read_bytes()
    size = len(data)
    boundaries = [size - k * _MIB for k in range(1, size // _MIB + 1)]
    assert boundaries and all(data[b - 1 : b] != b"\n" for b in boundaries)
    expected = ("the long ask", "the long reply", "claude-test")
    assert _turn(transcript) == expected
    assert _turn(transcript, "p1") == expected


def test_stop_turn_none_when_no_human_row_in_a_long_file(
    tmp_path: Path,
) -> None:
    """The walk reaches the start of a file with no submitted row and
    gives nothing: no turn's prompt is known, and the Stop records
    nothing."""
    transcript = tmp_path / "t.jsonl"
    _long_turn(transcript, after_prompt=2 * _MIB, human=False)
    assert _turn(transcript) is None


def test_stop_turn_none_past_the_walk_back_bound(tmp_path: Path) -> None:
    """A human row further back than the bound is not read: the hook
    no-ops as before rather than reading the file without limit."""
    transcript = tmp_path / "t.jsonl"
    _long_turn(transcript, after_prompt=4 * _MIB, prompt_id="p1")
    assert _turn(transcript, "p1", budget=2 * _MIB) is None
    assert _turn(transcript, budget=2 * _MIB) is None
    assert _turn(transcript, "p1", budget=8 * _MIB) == (
        "the long ask",
        "the long reply",
        "claude-test",
    )


def test_main_stamps_prompt_id_and_reply_source_from_the_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RS-F3: the Stop row names the harness turn and the reply's source,
    "payload" when Claude Code sent `last_assistant_message`."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    _seed_store(mem_dir)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "promptId": "turn-7",
            "message": {"content": "what about postgres?"},
        },
        _reply_row("Postgres is on 5432."),
    )
    payload = {
        "session_id": "sess-rsf3",
        "transcript_path": str(transcript),
        "last_assistant_message": "Postgres is on 5432, as before.",
        "prompt_id": "turn-7",
    }
    monkeypatch.setattr("sys.stdin", _StdinMock(json.dumps(payload).encode("utf-8")))
    assert hook_main(["--quiet"]) == 0
    audited = next(e for e in _events(mem_dir) if e["kind"] == "turn_audited")
    assert audited["prompt_id"] == "turn-7"
    assert audited["reply_source"] == "payload"
    assert audited["assistant_present"] is True


def test_main_reads_the_tail_reply_when_the_payload_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    _seed_store(mem_dir)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {"type": "user", "message": {"content": "what about postgres?"}},
        _reply_row("Postgres is on 5432."),
    )
    payload = {"session_id": "sess-rsf3", "transcript_path": str(transcript)}
    monkeypatch.setattr("sys.stdin", _StdinMock(json.dumps(payload).encode("utf-8")))
    assert hook_main(["--quiet"]) == 0
    audited = next(e for e in _events(mem_dir) if e["kind"] == "turn_audited")
    assert audited["reply_source"] == "tail"
    assert "prompt_id" not in audited


def test_main_omits_reply_source_without_a_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No reply from either source: `assistant_present` False and no
    `reply_source` key, so the two fields agree by construction."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    _seed_store(mem_dir)
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        {
            "type": "user",
            "promptId": "turn-8",
            "message": {"content": "what about postgres?"},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "x", "input": {}}]},
        },
    )
    payload = {
        "session_id": "sess-rsf3",
        "transcript_path": str(transcript),
        "prompt_id": "turn-8",
    }
    monkeypatch.setattr("sys.stdin", _StdinMock(json.dumps(payload).encode("utf-8")))
    assert hook_main(["--quiet"]) == 0
    audited = next(e for e in _events(mem_dir) if e["kind"] == "turn_audited")
    assert audited["assistant_present"] is False
    assert "reply_source" not in audited
    assert audited["prompt_id"] == "turn-8"


def test_main_audits_a_turn_whose_tool_results_outgrew_the_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: the Stop row exists for a long turn and probes the
    turn's own prompt. At 9.0.0rc2 the hook wrote nothing for it."""
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(mem_dir))
    _seed_store(mem_dir)
    transcript = tmp_path / "t.jsonl"
    _long_turn(transcript, after_prompt=2 * _MIB, prompt_id="turn-9")
    payload = {
        "session_id": "sess-long",
        "transcript_path": str(transcript),
        "prompt_id": "turn-9",
    }
    monkeypatch.setattr("sys.stdin", _StdinMock(json.dumps(payload).encode("utf-8")))
    assert hook_main(["--quiet"]) == 0
    audited = next(e for e in _events(mem_dir) if e["kind"] == "turn_audited")
    assert audited["prompt_id"] == "turn-9"
    assert audited["reply_source"] == "tail"
    assert audited["probe_query"]["len"] == len("the long ask")


def test_the_stop_row_with_a_reply_and_a_prompt_id_adds_two_keys(
    review_world: Any,
) -> None:
    """RS-F3: beside the pinned set, a Stop that names its turn and had a
    reply carries exactly `prompt_id` and `reply_source` more; the
    search_miss row is unchanged."""
    from .test_recall_prompt import QUERY

    review_world.write(origin=None, scopes=["infrastructure"])
    result = run_audit(
        user_message=QUERY,
        assistant_response="a reply that cites nothing",
        session_id="transcript-review",
        config=review_world.config,
        store=review_world.store,
        prompt_id="turn-1",
        reply_source="payload",
    )
    assert result["verdict"] == "miss"
    events = list(review_world.store.iter_events())
    [audited] = [e for e in events if e["kind"] == "turn_audited"]
    [miss] = [e for e in events if e["kind"] == "search_miss"]
    assert set(audited) == _TURN_AUDITED_KEYS | {"prompt_id", "reply_source"}
    assert (audited["prompt_id"], audited["reply_source"]) == ("turn-1", "payload")
    assert set(miss) == _SEARCH_MISS_KEYS


# ---------------------------------------------------------------------------
# RS-F5: the Stop anchored to its turn
# ---------------------------------------------------------------------------

#: A Stop whose turn holds no owner prompt probes nothing (no query, no
#: hits) and says why; with a transcript read it also names the model.
_NO_OWNER_KEYS = (_TURN_AUDITED_KEYS - {"probe_query", "top_hits"}) | {
    "no_signal_reason",
    "client_model",
}


def _composed_after_owner(path: Path, reply: str = "the notification's reply") -> Path:
    """An owner turn ``p1`` asking the test query, then a turn ``p2`` a
    background task's notification submitted with the query in its
    text."""
    from .test_recall_prompt import QUERY

    return tr.write(
        path,
        tr.user(QUERY, "p1"),
        tr.assistant("the owner's answer"),
        tr.user(f"{QUERY} check finished", "p2", turnOrigin="task_notification"),
        tr.assistant(reply),
    )


def _stop_of(world: Any, transcript: Path, prompt_id: str) -> dict[str, Any] | None:
    return audit_transcript(
        transcript_path=transcript,
        session_id="transcript-review",
        config=world.config,
        store=world.store,
        prompt_id=prompt_id,
    )


def test_a_turn_without_an_owner_prompt_is_audited_as_no_signal(
    review_world: Any, tmp_path: Path
) -> None:
    """The turn the Stop names holds only a notification: its row says so
    (verdict no_signal, reason no_owner_prompt), nothing is probed, and
    the owner's prompt of the turn before is not probed in its place.
    Before RS-F5 the walk took that older prompt and flagged this miss."""
    review_world.write(origin=None, scopes=["infrastructure"])
    transcript = _composed_after_owner(tmp_path / "t.jsonl")
    result = _stop_of(review_world, transcript, "p2")
    assert result is not None
    assert (result["verdict"], result["no_signal_reason"], result["probe_query"]) == (
        "no_signal",
        "no_owner_prompt",
        None,
    )
    events = list(review_world.store.iter_events())
    [audited] = [e for e in events if e["kind"] == "turn_audited"]
    assert set(audited) == _NO_OWNER_KEYS | {"prompt_id", "reply_source"}
    assert (audited["verdict"], audited["no_signal_reason"]) == (
        "no_signal",
        "no_owner_prompt",
    )
    assert (audited["prompt_id"], audited["reply_source"]) == ("p2", "tail")
    assert (audited["assistant_present"], audited["recent_retrieval_count"]) == (
        True,
        0,
    )
    assert [e for e in events if e["kind"] == "search_miss"] == []
    # The owner's own turn is probed, and is the miss.
    owner = _stop_of(review_world, transcript, "p1")
    assert owner is not None and owner["verdict"] == "miss"


def test_a_turn_without_an_owner_prompt_settles_nothing(
    review_world: Any, tmp_path: Path
) -> None:
    """Its reply answered what the harness composed, so neither a pointer
    the recall showed nor a real retrieval is settled against it, though
    the reply quotes both; the owner's next turn settles them."""
    from .test_recall_prompt import BODY

    shown = review_world.write()
    assert review_world.recall(session_id="transcript-review").ids == (shown.id,)
    retrieved = review_world.store.write(
        content="The metrics dashboard runs at grafana.internal/d/api-latency.",
        scopes=["infrastructure"],
    )
    _seed_search_event(
        review_world.store_dir, session_id="transcript-review", returned=[retrieved.id]
    )
    reply = f"{BODY}. The metrics dashboard runs at grafana.internal/d/api-latency."
    transcript = _composed_after_owner(tmp_path / "t.jsonl", reply)
    _stop_of(review_world, transcript, "p2")
    assert [e for e in review_world.store.iter_events() if e["kind"] == "use"] == []
    tr.append(transcript, tr.user("and how is it set up", "p3"), tr.assistant(reply))
    _stop_of(review_world, transcript, "p3")
    uses = [e for e in review_world.store.iter_events() if e["kind"] == "use"]
    assert {memory_id for e in uses for memory_id in e["ids"]} == {
        shown.id,
        retrieved.id,
    }


def test_a_turn_without_an_owner_prompt_still_counts_open_reviews(
    review_world: Any, tmp_path: Path
) -> None:
    """The next turn's review window opens at this Stop's row, so the
    count is taken here as on any turn."""
    root = _review_row(review_world.store)
    _stop_of(review_world, _composed_after_owner(tmp_path / "t.jsonl"), "p2")
    [row] = _unanswered(review_world.store)
    assert row["root_ids"] == [root]


def test_a_turn_without_text_has_no_reply_though_the_turn_before_has_one(
    review_world: Any, tmp_path: Path
) -> None:
    """The reply fallback reads this turn only: no text in it is no reply
    (no `reply_source`, `assistant_present` false), never the previous
    turn's text."""
    from .test_recall_prompt import QUERY

    review_world.write(origin=None, scopes=["infrastructure"])
    transcript = tr.write(
        tmp_path / "t.jsonl",
        tr.user("an older ask", "p0"),
        tr.assistant("an older reply"),
        tr.user(QUERY, "p1"),
        tr.assistant(None),
        tr.tool_result(40, "p1"),
        tr.assistant(None),
    )
    _stop_of(review_world, transcript, "p1")
    [audited] = [
        e for e in review_world.store.iter_events() if e["kind"] == "turn_audited"
    ]
    assert audited["assistant_present"] is False
    assert "reply_source" not in audited
    assert audited["probe_query"]["len"] == len(QUERY)


def test_an_undecided_turn_records_nothing(review_world: Any, tmp_path: Path) -> None:
    """A Stop naming a turn the transcript does not hold has nothing to
    audit: no row, as for an unreadable file."""
    transcript = _composed_after_owner(tmp_path / "t.jsonl")
    assert _stop_of(review_world, transcript, "p9") is None
    assert [
        e for e in review_world.store.iter_events() if e["kind"] == "turn_audited"
    ] == []
