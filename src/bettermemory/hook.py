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

Known divergence from the in-process audit: this hook does NOT
see session-disabled scopes. The model-side `memory_audit_turn`
filters out scopes the user disabled via `memory_scope_disable`
(those live in `SessionState` and aren't persisted), so a turn the
user explicitly framed as "unrelated to project X" can be flagged
as a silent miss here even though the in-process audit would
shield it. Stop-hook events carry `triggered_from="stop_hook"` so
downstream rollups can distinguish the two sources; consumers
that want the strict count should prefer the model-side events.

Failure mode: the hook must never block the turn end. Every error
path is caught and exit code is forced to 0 so a parser hiccup or
a missing transcript doesn't surface as a Claude Code error banner.
The user can `bettermemory audit-turn --transcript-path ...` for a
loud version if they want to debug.

Trust boundary: `transcript_path` comes from Claude Code's Stop
hook payload, not from the model. We still defensively resolve
and `is_file()`-check the path before reading — the hook runs in
the user's process context, so a misconfigured upstream hook that
mutates the payload shouldn't be able to coax us into reading an
arbitrary file. The contents go nowhere observable even on
mis-feed (the JSONL parser drops anything that isn't a
user/assistant message), but the read itself is a surface worth
narrowing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ._fsutil import bounded_stream_read, bounded_tail_read
from .attribution import attribute_uses
from .audit import probe_for_miss, search_miss_fields, turn_audited_fields
from .config import Config, load_config
from .events import Recorder
from .events import iter_events
from .models import utcnow
from .origin import capture as capture_origin
from .store import MemoryNotFoundError, Store, TombstonedError


# Wall-clock window the hook attributes against. A retrieval older
# than this is considered settled — auto-commit will already have
# fired (the in-process TTL is two turns, typically seconds to
# minutes), so attributing to a stale retrieval would risk
# double-counting. Wide enough to cover normal conversational
# pauses, narrow enough to focus on the current turn.
_ATTRIBUTION_LOOKBACK_SECONDS = 600

# Cap the transcript read to the trailing 1 MiB. The hook only needs the
# latest user + assistant message, which sit at the tail of an append-only
# JSONL log; older content is irrelevant for this turn. Reading the whole
# file was a real OOM vector on long Claude Code sessions (transcripts grow
# to hundreds of MB in extended pairing sessions). The cap mirrors the
# `_TRANSCRIPT_READ_CAP_BYTES` constant in consolidate.py and is enforced
# at byte granularity (not character) so multibyte UTF-8 can't bypass it.
_TRANSCRIPT_TAIL_READ_BYTES = 1_048_576

# Cap the Stop-hook stdin payload. Claude Code emits a small JSON object
# (`session_id`, `transcript_path`, `cwd`, `hook_event_name`) — well under
# 1 KB in practice. 64 KB leaves five orders of magnitude of headroom for
# any sensible payload while bounding a misbehaving pipe writer's blast
# radius. Without this cap the hook process would buffer the entire pipe
# into memory before `json.loads` got a chance to reject. An oversized
# payload is treated as a malformed input — the hook silently no-ops,
# same contract as a bad JSON payload.
_STDIN_PAYLOAD_CAP_BYTES = 64 * 1024


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
    # `bounded_tail_read` handles the seek-to-end + partial-line-discard +
    # unseekable-stream fallback. The latest user+assistant pair sits at
    # the tail of an append-only JSONL, so the head is uninteresting and
    # would risk loading hundreds of MB of session history into memory.
    try:
        chunk = bounded_tail_read(transcript_path, _TRANSCRIPT_TAIL_READ_BYTES)
    except OSError:
        return None, None
    text = chunk.decode("utf-8", errors="replace")

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
        mode=cfg.behavior.search_mode or "hybrid",
    )
    # Emit the audit event so cadence is visible even when there's
    # nothing to flag — matches the MCP handler's discipline. Honour
    # the same `telemetry.enabled` / `telemetry.max_bytes` config
    # the server-side recorder reads, so a user who opted out of
    # event logging doesn't see the hook silently override that
    # choice on every Stop event.
    recorder = Recorder(
        root=root,
        session_id=session_id,
        enabled=cfg.telemetry.enabled,
        max_bytes=cfg.telemetry.max_bytes,
        log_queries_verbatim=cfg.telemetry.log_queries_verbatim,
    )
    # `turn_audited` / `search_miss` field sets come from the shared
    # builders in `audit.py`, so the Stop hook and the in-process MCP
    # handler (`_handlers._advance_turn`) cannot drift — the 2.6.4
    # audit found them already diverged. `triggered_from="stop_hook"`
    # tags the source.
    probe_mode = cfg.behavior.search_mode or "hybrid"
    recorder.record(
        "turn_audited",
        **turn_audited_fields(
            report,
            session_id=session_id,
            probe_mode=probe_mode,
            assistant_present=assistant_response is not None,
            triggered_from="stop_hook",
        ),
    )
    if report.is_miss:
        recorder.record(
            "search_miss",
            **search_miss_fields(
                report, session_id=session_id, triggered_from="stop_hook"
            ),
        )

    # Post-hoc claim_excerpt attribution. The MCP contract asks the
    # model to attach `claim_excerpts` on explicit memory_record_use
    # when a retrieved memory shaped a sentence in its reply; in
    # practice the model defaults to the free auto-commit path and
    # `memory_helped_rate` reads 0%. The hook closes the loop by
    # substring-matching recently-retrieved memories' bodies against
    # the assistant's reply text — when a body sentence appears
    # verbatim (case- and whitespace-normalised), record an
    # `applied` event with the matched phrase as the excerpt and
    # `attribution="hook"`. The in-process `_advance_turn` reads
    # these events and skips the redundant auto-commit so the
    # retrieval generates one applied event total, not two.
    if assistant_response:
        _emit_hook_attributions(
            store=store,
            recorder=recorder,
            recent_events=recent,
            session_id=session_id,
            assistant_response=assistant_response,
        )

    # Opt-in self-improving loop. Run the structurally-safe consolidation
    # subset (conservative dedup + non-destructive demote), debounced, at
    # turn end. Gated on `telemetry.enabled` because the event log is BOTH
    # the debounce clock AND the audit trail — no log means no reviewable
    # record, so we refuse to auto-mutate. Imported lazily so users who
    # haven't opted in never pay consolidate's (semantic/health/search)
    # import cost on every Stop event. Isolated in try/except so a
    # consolidate hiccup can neither block the turn end nor drop the audit
    # result above.
    if cfg.consolidate.auto_apply and cfg.telemetry.enabled:
        try:
            from .consolidate import run_auto_consolidate

            run_auto_consolidate(
                store,
                recorder=recorder,
                events=recent,
                session_id=session_id,
                interval_hours=cfg.consolidate.auto_apply_interval_hours,
                max_memories=cfg.consolidate.auto_apply_max_memories,
                memories=memories,
                now=utcnow(),
            )
        except Exception as exc:  # noqa: BLE001 — hook must never block turn end
            print(f"bettermemory auto-consolidate: {exc}", file=sys.stderr)

    # Opt-in write-reflex closure (the capture half of the self-improving
    # loop). Scan this turn's user message for durable-looking statements
    # the model didn't write and queue them as inert, review-gated
    # proposals for the `memory_proposals` tool. Imported lazily so users
    # who haven't opted in pay nothing per turn; best-effort so it can
    # never block the turn end. No telemetry gate — the proposal queue is
    # its own file and proposals are inert until the model reviews them;
    # the recorded event is best-effort observability only.
    if cfg.proposals.auto_propose:
        try:
            from .proposals import propose_from_exchange

            proposed = propose_from_exchange(
                root,
                user_text=user_message,
                max_pending=cfg.proposals.max_pending,
                now=utcnow(),
            )
            if proposed:
                recorder.record(
                    "proposals_enqueued",
                    count=len(proposed),
                    session_id=session_id,
                    triggered_from="stop_hook",
                )
        except Exception as exc:  # noqa: BLE001 — hook must never block turn end
            print(f"bettermemory proposals: {exc}", file=sys.stderr)

    return report.to_dict()


def _emit_hook_attributions(
    *,
    store: Store,
    recorder: Recorder,
    recent_events: list[dict[str, Any]],
    session_id: str,
    assistant_response: str,
) -> None:
    """Substring-match recently-retrieved memories against the reply
    text and emit `applied` events for matches.

    `pending` is the set of memory_ids retrieved (via `search` or
    `show`) in this session within the lookback window, MINUS ids
    that already have any `use` event in the same window — those
    have either been explicitly recorded by the model or
    auto-committed already, and re-attributing would double-count.
    The matcher's heuristics (≥6-token, ≥30-char, stopword-
    filtered candidate sentences) cap false positives; the
    "no-already-used" filter caps double-counting.
    """
    pending = _pending_retrievals(
        recent_events,
        session_id=session_id,
        lookback_seconds=_ATTRIBUTION_LOOKBACK_SECONDS,
    )
    if not pending:
        return
    bodies: dict[str, str] = {}
    for memory_id in pending:
        try:
            memory = store.load_one(memory_id)
        except (MemoryNotFoundError, TombstonedError):
            # Memory disappeared between retrieval and end-of-turn —
            # nothing to attribute against. Skip silently.
            continue
        bodies[memory_id] = memory.body
    if not bodies:
        return
    matches = attribute_uses(bodies, assistant_response)
    if not matches:
        return
    recorder.record(
        "use",
        ids=[m.memory_id for m in matches],
        outcome="applied",
        auto=False,
        attribution="hook",
        claim_excerpts=[m.claim_excerpt for m in matches],
        triggered_from="stop_hook",
    )


def _pending_retrievals(
    events: list[dict[str, Any]],
    *,
    session_id: str,
    lookback_seconds: int,
) -> set[str]:
    """Memory_ids retrieved in this session within the lookback window
    that have NOT yet been recorded via `record_use`.

    A retrieval is the `search` event's `returned` list or the `show`
    event's `id`. A `use` event for the same id within the window
    counts as already-recorded and removes the id from the pending
    set. This approximates the in-process SessionState's
    `pending_use_tokens` from the event log alone, which is what the
    hook has access to.
    """
    cutoff_ts = utcnow().timestamp() - lookback_seconds
    retrieved: set[str] = set()
    used: set[str] = set()
    for event in events:
        # Canonical-first session lookup with legacy fallback — same
        # discipline 70e41a4 established for llm.py.
        if (event.get("session") or event.get("session_id")) != session_id:
            continue
        ts_str = event.get("ts")
        if not isinstance(ts_str, str):
            continue
        ts = _parse_iso_ts(ts_str)
        if ts is None or ts.timestamp() < cutoff_ts:
            continue
        kind = event.get("kind")
        if kind == "search":
            # Legacy fallback: pre-2.6.3 search archives wrote
            # `memory_ids`, test fixtures used `hit_ids`. Read canonical,
            # fall back to either.
            returned = (
                event.get("returned")
                or event.get("memory_ids")
                or event.get("hit_ids")
                or []
            )
            if isinstance(returned, list):
                for mid in returned:
                    if isinstance(mid, str):
                        retrieved.add(mid)
        elif kind == "show":
            mid = event.get("id")
            if isinstance(mid, str):
                retrieved.add(mid)
        elif kind == "use":
            # Legacy fallback for `memory_ids` — same class as above.
            ids = event.get("ids") or event.get("memory_ids") or []
            if isinstance(ids, list):
                for mid in ids:
                    if isinstance(mid, str):
                        used.add(mid)
    return retrieved - used


def _parse_iso_ts(value: str) -> datetime | None:
    """Tolerant ISO-8601 parse — accepts the `Z` suffix used in event
    logs. Returns None on malformed input rather than raising so a
    single bad event doesn't break the whole attribution pass.
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


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
            # Bound the stdin read at byte granularity. A misbehaving
            # pipe writer streaming GB of garbage would otherwise OOM
            # the hook process before `json.loads` got a chance to
            # reject. Oversized payloads land in the same bucket as
            # malformed JSON: silent no-op, contract preserved.
            try:
                raw_payload = bounded_stream_read(
                    sys.stdin.buffer, _STDIN_PAYLOAD_CAP_BYTES
                )
            except ValueError:
                return 0
            payload = _read_payload(raw_payload.decode("utf-8", errors="replace"))

        transcript_raw = args.transcript_path or payload.get("transcript_path")
        session_id = args.session_id or payload.get("session_id")
        if not transcript_raw or not session_id:
            # Nothing to audit. Don't error — the hook fires for every
            # Stop event, and a malformed or missing payload should
            # be a silent no-op so the turn-end pipeline isn't
            # interrupted.
            return 0

        # Resolve + is_file() defense: collapses `..` segments, follows
        # symlinks to their target, and rejects fifos / devices / dirs /
        # missing files. A misconfigured Stop-hook payload (or a future
        # upstream hook that rewrites the field) can't coax the
        # transcript reader into opening `/etc/shadow` or a named pipe
        # this way. The contents go nowhere observable even without
        # this guard, but the read itself is the surface worth closing.
        transcript_path = Path(str(transcript_raw)).expanduser().resolve()
        if not transcript_path.is_file():
            return 0
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
