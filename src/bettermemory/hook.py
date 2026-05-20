"""Client-side hook entry point for end-of-turn audit telemetry.

The audit MCP tool (`memory_audit_turn`) needs to fire after every
assistant turn to populate silent-miss telemetry. The model is
supposed to invoke it via its tool surface, but in practice it
forgets — the audit's value is opt-in-on-top-of-opt-in. The natural
distribution point is a Claude Code Stop hook: the harness invokes
a CLI on the user's machine at the end of every turn, before
control returns to the user.

This module is the thin CLI surface that Stop hooks wire to. It
reads Claude Code's Stop hook payload from stdin
(`{session_id, transcript_path, cwd, hook_event_name}`), parses the
transcript JSONL to find the latest user message and assistant
response, and calls `audit.probe_for_miss` against the store
identified by `BETTERMEMORY_DIR` (or the usual resolution rules).

Cross-process limitation: the model and the hook run in different
processes, so the hook can't share the model's in-memory
SessionState. It CAN share the event log on disk — the audit
function only needs the session id and a list of recent events to
decide whether the model retrieved memory this turn. The session
id is carried in the Stop hook payload; recent events are read
from `.events.jsonl`.

Failure mode: the hook must never block the turn end. Every error
path is caught and exit code is forced to 0 so a parser hiccup or
a missing transcript doesn't surface as a Claude Code error banner.
The user can `bettermemory audit-turn --transcript-path ...` for a
loud version if they want to debug.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .audit import probe_for_miss
from .config import Config, load_config
from .events import Recorder
from .events import iter_events
from .models import utcnow
from .origin import capture as capture_origin
from .store import Store


def _read_payload(stdin_text: str) -> dict[str, Any]:
    """Parse the Stop hook stdin JSON. Tolerate whitespace and
    trailing newlines. Returns an empty dict if the payload is empty
    or malformed — the caller treats that as "nothing to audit"."""
    raw = stdin_text.strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _extract_last_exchange(
    transcript_path: Path,
) -> tuple[str | None, str | None]:
    """Walk the transcript JSONL backwards to find the latest user
    message and the latest assistant response.

    Returns `(user_message, assistant_response)`. Either field is
    None when not found. Defensive against malformed lines — a
    single bad JSON line doesn't abort the whole parse.

    Format reference (Claude Code transcript schema, observed
    May 2026): one JSON object per line. User messages carry
    `{"type": "user", "message": {"content": "<string>"}}`; assistant
    messages carry `{"type": "assistant", "message": {"content":
    [<content blocks>]}}` where the content blocks each have a
    `type` field ("text" / "thinking" / "tool_use" / …). We
    concatenate text-block bodies for the response surface.
    """
    user: str | None = None
    assistant: str | None = None
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    # Walk lines in reverse — the most recent user/assistant come
    # last. Stop once we have both.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        row_type = row.get("type")
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        if assistant is None and row_type == "assistant":
            assistant = _flatten_assistant_content(message.get("content"))
        elif user is None and row_type == "user":
            content = message.get("content")
            if isinstance(content, str):
                user = content
            elif isinstance(content, list):
                user = _flatten_assistant_content(content)
        if user is not None and assistant is not None:
            break
    return user, assistant


def _flatten_assistant_content(content: Any) -> str | None:
    """Pull text out of an assistant content list. Returns None if
    nothing useful is found. Skips thinking blocks and tool-use
    blocks — the audit cares about text the model showed the user."""
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


def run_audit(
    *,
    user_message: str,
    assistant_response: str | None,
    session_id: str,
    config: Config | None = None,
) -> dict[str, Any]:
    """Pure-function entry point: given a user message and session
    id, run the probe and emit events. Returns a small dict suitable
    for JSON dumping. Side effects (event-log writes) only happen
    when there's something to report and the probe runs cleanly."""
    cfg = config or load_config(None)
    root = cfg.resolved_directory()
    store = Store(root)
    memories = store.load_all()
    recent = list(iter_events(root))
    report = probe_for_miss(
        memories,
        user_message,
        recent_events=recent,
        session_id=session_id,
        now=utcnow(),
        lookback_seconds=60,
        caller_origin=capture_origin(),
        mode=cfg.behavior.search_mode or "keyword",
    )
    # Emit the audit event so cadence is visible even when there's
    # nothing to flag — matches the MCP handler's discipline.
    recorder = Recorder(root=root, session_id=session_id)
    recorder.record(
        "turn_audited",
        verdict=report.verdict,
        threshold_rule=report.threshold_rule,
        recent_retrieval_count=report.recent_retrieval_count,
        triggered_from="stop_hook",
    )
    if report.is_miss:
        recorder.record(
            "search_miss",
            session=session_id,
            probe_query=report.probe_query,
            top_hit_ids=[h.id for h in report.top_hits],
            triggered_from="stop_hook",
        )
    return report.to_dict()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point wired into `bettermemory audit-turn`.

    Defaults to reading the Claude Code Stop hook payload from
    stdin. Use `--transcript-path` + `--session-id` to drive the
    audit manually for debugging.

    Always exits 0 — a hook must never break the turn-end pipeline.
    Errors are logged to stderr but swallowed at the exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="bettermemory audit-turn",
        description="Run a silent-miss audit for the just-completed turn.",
    )
    parser.add_argument(
        "--transcript-path",
        type=str,
        default=None,
        help="Path to the Claude Code transcript JSONL. When omitted, "
        "read the path from the Stop hook stdin payload.",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Session id for event correlation. When omitted, read "
        "from the Stop hook stdin payload.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the JSON summary on stdout. The audit still "
        "writes its events to the on-disk log.",
    )
    args = parser.parse_args(argv)

    try:
        payload: dict[str, Any] = {}
        if args.transcript_path is None or args.session_id is None:
            payload = _read_payload(sys.stdin.read())

        transcript_raw = args.transcript_path or payload.get("transcript_path")
        session_id = args.session_id or payload.get("session_id")
        if not transcript_raw or not session_id:
            # Nothing to audit. Don't error — the hook fires for every
            # Stop event, and a malformed or missing payload should
            # be a silent no-op so the turn-end pipeline isn't
            # interrupted.
            return 0

        transcript_path = Path(str(transcript_raw)).expanduser()
        user, assistant = _extract_last_exchange(transcript_path)
        if not user:
            return 0

        result = run_audit(
            user_message=user,
            assistant_response=assistant,
            session_id=str(session_id),
        )
        if not args.quiet:
            print(json.dumps(result), file=sys.stdout)
    except Exception as exc:  # noqa: BLE001 — hook must never block turn end
        print(f"bettermemory audit-turn: {exc}", file=sys.stderr)
    return 0
