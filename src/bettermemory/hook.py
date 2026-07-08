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

Session-disabled scopes: the hook reconstructs them from the event
log rather than the model's in-memory `SessionState` (which it can't
see across the process boundary). `memory_scope_disable` /
`memory_scope_enable` already append `scope_disable` / `scope_enable`
events stamped with the MCP server's stable per-process session id,
so the hook replays those toggles for the *current in-process server
session* and feeds the net set into the probe as `excluded_scopes` —
the same shield the in-process `memory_audit_turn` applies. A turn the
user framed as "unrelated to project X" is therefore no longer
false-flagged as a silent miss here. See `_disabled_scopes_from_events`.
Stop-hook events still carry `triggered_from="stop_hook"` so
downstream rollups can distinguish the two sources.

Reset-on-restart eventually holds, but not atomically. A restarted
server mints a fresh session id with no scope toggles under it yet —
but `_latest_in_process_session` anchors to the *most recent
non-stop-hook event*, which is still the PRIOR session until the new
server writes its first in-process event. During that gap window the
prior session's `scope_disable` events keep being replayed, so a stale
disable can shield a real miss until the new server's first in-process
tool call flips the anchor. The bias is conservative (over-suppress)
and self-correcting.

Residual divergence (two cases, opposite directions):
- Concurrent sessions: the anchor prefers the latest in-process event
  stamped with THIS hook's `worktree_root`, so a concurrent session in
  a DIFFERENT worktree no longer hijacks it — that hijack flipped the
  verdict in both directions (the foreign session's search *shielding*
  this window's real miss, and the foreign session's unrelated events
  *unshielding* a turn that searched correctly — anti-conservative
  over-flagging). The retrieval shield additionally counts any
  in-window retrieval stamped with this worktree REGARDLESS of session
  (`audit._count_recent_retrievals`), so a concurrent session in the
  SAME worktree — or a mid-conversation server restart flipping the
  anchor — can no longer orphan this window's own search and re-fire a
  false miss. What remains: the disabled-scope replay is still
  single-session-anchored (reset-on-restart is load-bearing there), so
  a same-worktree concurrent session can anchor the OTHER session's
  scope toggles; and logs with no stamped match for this worktree
  (legacy events, server outside a git checkout) fall back to
  latest-any session matching, where both directions are still
  possible.
- Rotation bound: the event read is window-aware (`iter_events_window`
  prepends the newest rotated segment when the active log doesn't cover
  the attribution window), so a single mid-window rotation no longer
  hides this turn's `search` / `scope_disable` events. The residual
  loss needs TWO rotations inside one window — a `scope_disable` that
  rotates beyond the newest segment while its session is still live is
  lost and the shielded miss RE-FIRES — biases toward *over-flagging*.
Both are narrow and match the single-active-server deployment.

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
from pathlib import Path
from typing import Any

from ._fsutil import bounded_stream_read, bounded_tail_read
from .attribution import attribute_uses
from .audit import (
    ATTRIBUTION_LOOKBACK_SECONDS,
    REAUDIT_DEDUP_WINDOW_SECONDS,
    is_duplicate_audit,
    probe_for_miss,
    search_miss_fields,
    turn_audited_fields,
)
from .config import Config, load_config
from .events import Recorder, redact_query
from .events import iter_events_window
from .models import utcnow
from .origin import capture as capture_origin
from .store import MemoryNotFoundError, Store, TombstonedError
from .time_utils import parse_event_ts


# Wall-clock window the hook attributes against. A retrieval older
# than this is considered settled — a prior turn's hook run settled
# it at that turn's end, or the in-process fallback (held behind the
# wall-clock floor mirroring this window) has fired, so attributing
# to a stale retrieval would risk double-counting. Wide enough to
# cover normal conversational pauses, narrow enough to focus on the
# current turn. The constant
# itself lives in `audit.py` (round 88) so the production search
# handler's endorsement tally can share the exact window without
# importing this module; the module-local alias keeps every existing
# in-file reference (and the historical name) intact.
_ATTRIBUTION_LOOKBACK_SECONDS = ATTRIBUTION_LOOKBACK_SECONDS

# Cap the transcript read to the trailing 1 MiB. The hook only needs the
# latest user + assistant message, which sit at the tail of an append-only
# JSONL log; older content is irrelevant for this turn. Reading the whole
# file was a real OOM vector on long Claude Code sessions (transcripts grow
# to hundreds of MB in extended pairing sessions). The cap mirrors the
# `_TRANSCRIPT_READ_CAP_BYTES` constant in consolidate.py and is enforced
# at byte granularity (not character) so multibyte UTF-8 can't bypass it.
_TRANSCRIPT_TAIL_READ_BYTES = 1_048_576

# `type="user"` transcript rows the human never typed. Claude Code records
# background task notifications, slash-command bookkeeping, harness stdout
# wrappers, and system reminders as user rows whose content opens with one
# of these envelope tags (task-notification rows carry NO metadata flag, so
# a content-prefix check is the only available discriminator). Skill and
# command expansions additionally carry `isMeta: true` at the row level —
# `_extract_last_exchange` checks that flag separately. On either signal
# the reverse walk skips the row and keeps walking: the human's message is
# typically a few rows earlier in the tail.
_SYNTHETIC_USER_PREFIXES = (
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<system-reminder>",
)

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
) -> tuple[str | None, str | None, str | None]:
    """Walk the transcript JSONL backwards to find the latest user
    message, the latest assistant response, and the model that
    produced it.

    Returns `(user_message, assistant_response, model)`. Any field is
    None when not found. Defensive against malformed lines — a
    single bad JSON line doesn't abort the whole parse.

    `model` is read off the newest assistant row's `message.model`
    (e.g. "claude-sonnet-5") — captured even when that row carries no
    text blocks (a tool-use-only stop), since any assistant row of the
    turn identifies the model. The MCP channel carries no model
    identity at all, so this transcript read is the ONLY place in the
    system that can attribute telemetry per-model; `run_audit` stamps
    it onto the events it emits as `client_model`.

    Format reference (Claude Code transcript schema, observed
    May 2026): one JSON object per line. User messages carry
    `{"type": "user", "message": {"content": "<string>"}}`; assistant
    messages carry `{"type": "assistant", "message": {"content":
    [<content blocks>]}}` where the content blocks each have a
    `type` field ("text" / "thinking" / "tool_use" / …). We
    concatenate text-block bodies for the response surface.

    Synthetic user rows (observed June 2026): transcripts also record
    payloads the human never typed as `type="user"` rows — background
    `<task-notification>` bodies, `<command-name>`/`<local-command-*>`
    bookkeeping, `<system-reminder>` injections, slash-command/skill
    expansions (those carry `isMeta: true`), and occasional empty
    strings. Accepting them verbatim made the audit probe (and the
    proposals extractor, whose contract is "only the user's own words
    are mined") run on harness text whenever such a row landed after
    the human's message. The walk skips those rows and keeps going —
    the human's message is usually a few rows earlier. If nothing
    human-looking is in the tail, `user` stays None and the hook
    no-ops, the same fail-quiet contract as before.
    """
    user: str | None = None
    assistant: str | None = None
    model: str | None = None
    # `bounded_tail_read` handles the seek-to-end + partial-line-discard +
    # unseekable-stream fallback. The latest user+assistant pair sits at
    # the tail of an append-only JSONL, so the head is uninteresting and
    # would risk loading hundreds of MB of session history into memory.
    try:
        chunk = bounded_tail_read(transcript_path, _TRANSCRIPT_TAIL_READ_BYTES)
    except OSError:
        return None, None, None
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
        if row_type == "assistant":
            if model is None:
                raw_model = message.get("model")
                if isinstance(raw_model, str) and raw_model:
                    model = raw_model
            if assistant is None:
                assistant = _flatten_assistant_content(message.get("content"))
        elif user is None and row_type == "user":
            # Harness-injected rows: skill/command expansions are stamped
            # `isMeta: true` at the row level. Keep walking — the human's
            # message sits below the expansion.
            if row.get("isMeta"):
                continue
            content = message.get("content")
            candidate: str | None = None
            if isinstance(content, str):
                candidate = content
            elif isinstance(content, list):
                candidate = _flatten_assistant_content(content)
            if candidate is None or not candidate.strip():
                # Empty/whitespace-only user rows occur in real
                # transcripts; capturing one used to silently drop the
                # whole audit at main()'s `if not user`. Keep walking.
                continue
            if candidate.lstrip().startswith(_SYNTHETIC_USER_PREFIXES):
                # Envelope-tagged synthetic payload (see the constant's
                # comment) — not the user's words. Keep walking.
                continue
            user = candidate
        if user is not None and assistant is not None:
            break
    return user, assistant, model


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
    client_model: str | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Pure-function entry point: given a user message and session
    id, run the probe and emit events. Returns a small dict suitable
    for JSON dumping. Side effects (event-log writes) only happen
    when there's something to report and the probe runs cleanly.

    `client_model` is the transcript-derived model id (see
    `_extract_last_exchange`); stamped as `client_model` on the
    `turn_audited` / `search_miss` / `use` events this hook emits so
    eval can slice telemetry per-model. None (unknown) omits the
    field."""
    cfg = config or load_config(None)
    root = cfg.resolved_directory()
    store = Store(root)
    memories = store.load_all()
    # Window-aware read: rotation archives the ENTIRE active log at a
    # moment independent of turn boundaries, so a turn that straddles a
    # rotation would lose its own `search` / `scope_disable` events
    # from a plain active-log read and re-fire as a false miss.
    # `iter_events_window` prepends the newest rotated segment whenever
    # the active log doesn't cover the requested window, so every
    # consumer of `recent` below (the retrieval shield, the disabled-
    # scope replay, the pending-retrieval attribution) sees the full
    # window across a single rotation. The request asks for the WIDEST
    # window any consumer needs — the re-audit dedup's
    # `REAUDIT_DEDUP_WINDOW_SECONDS` — which is a coverage request, not
    # a filter: the reader yields the whole active log either way, and
    # every time-scoped consumer (the retrieval shield, the attribution
    # pass) applies its own narrower cutoff internally.
    recent = list(iter_events_window(root, REAUDIT_DEDUP_WINDOW_SECONDS))
    # Capture once; reused for the probe's auto-scope and stamped on the
    # hook's events so episode_handoff can worktree-match this turn's
    # session (queue #28). The hook runs as a fresh process in the
    # turn's cwd, so this reflects the user's working repo.
    caller_origin = capture_origin()
    # Reconstruct the session-disabled scope set from the event log so the
    # probe shields the same scopes the in-process audit would. Without
    # this, a scope the user disabled via `memory_scope_disable` (e.g.
    # "this is unrelated to project X") would still produce silent-miss
    # flags here, since the hook can't read the server's in-memory state.
    excluded_scopes = _disabled_scopes_from_events(
        recent, worktree_root=caller_origin.worktree_root
    )
    # The probe's retrieval shield ("did the model already search this
    # turn?") must match the server's session id, not this hook's
    # `session_id` (which is Claude Code's transcript id — a different id
    # space from the server's `sess_<hex>`). Bridge to the live in-process
    # server session the same way `_disabled_scopes_from_events` and
    # `_emit_hook_attributions` do; the probe falls back to `session_id`
    # when no in-process session is on record. Without this, the shield was
    # structurally dead in the Stop hook and every searched-then-continued
    # turn could still emit a `search_miss`. Anchored to this hook's
    # worktree so a concurrent window's server can't hijack the bridge
    # (see `_latest_in_process_session`).
    server_session = _latest_in_process_session(
        recent, worktree_root=caller_origin.worktree_root
    )
    # Endorsement nudge: mirror the production search handler's opt-in
    # tally (`handlers/search.py::_explicit_applied_counts`) so the
    # probe ranks with the same usage signal the model's retrieval
    # would have seen. It MUST be counted over the same horizon
    # production uses — `ATTRIBUTION_LOOKBACK_SECONDS` (600s, what
    # `handlers/search.py` reads) — NOT the dedup-widened `recent`
    # above (`REAUDIT_DEDUP_WINDOW_SECONDS`, 3600s). `recent` is a
    # coverage read for the dedup / shield / attribution consumers,
    # each of which applies its own narrower cutoff; but
    # `_explicit_applied_counts` applies NO cutoff of its own, so
    # feeding it the 3600s read would count applies from up to an hour
    # ago that production's 600s ranker never saw, letting the probe
    # nudge a near-tie top-1 to "high" and flip a false `search_miss`.
    # Read a separately-scoped 600s window so the audit ranker matches
    # production (the same fix `handlers/audit_turn.py` already carries
    # for the in-process producer). Lazy import, gated on the flag —
    # default-config users pay nothing.
    applied_by_id: dict[str, int] | None = None
    if cfg.behavior.endorsement_boost and memories:
        from .handlers.search import _explicit_applied_counts

        endorsement_events = list(
            iter_events_window(root, ATTRIBUTION_LOOKBACK_SECONDS)
        )
        applied_by_id = _explicit_applied_counts(
            endorsement_events,
            {m.id for m in memories},
            now=utcnow(),
            lookback_seconds=ATTRIBUTION_LOOKBACK_SECONDS,
        )
    report = probe_for_miss(
        memories,
        user_message,
        recent_events=recent,
        session_id=session_id,
        retrieval_session_id=server_session,
        now=utcnow(),
        # The probe's "did the model already retrieve this turn?" shield
        # must use the same wall-clock definition of "this turn" as the
        # attribution pass below — the Stop hook fires at turn END, so a
        # tool-heavy turn easily outlives a short window. Pre-fix this
        # hardcoded 60s while attribution used 600s: any turn longer
        # than a minute aged its own search event out of the shield and
        # emitted a false `search_miss`. 600s is also the ceiling the
        # MCP handler clamps `lookback_seconds` to; the wider window's
        # bias is conservative (over-suppress), matching the project's
        # stance on miss-signal noise.
        lookback_seconds=_ATTRIBUTION_LOOKBACK_SECONDS,
        caller_origin=caller_origin,
        excluded_scopes=excluded_scopes,
        mode=cfg.behavior.search_mode or "hybrid",
        # Never load an embedding model here: the hook runs as a fresh
        # process on every Stop event, so a semantic-model load (1-10s)
        # per turn end would violate the must-never-block contract. For
        # `search_mode = "semantic"` the probe records an explicit
        # `no_signal` (`no_signal_reason="semantic_model_unavailable"`)
        # instead of crashing before `turn_audited` lands; `hybrid`
        # degrades to keyword+BM25 fusion as documented.
        semantic_model=None,
        half_life_days=cfg.behavior.recency_boost_half_life_days,
        applied_by_id=applied_by_id,
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
        worktree_root=caller_origin.worktree_root,
    )
    # `turn_audited` / `search_miss` field sets come from the shared
    # builders in `audit.py`, so the Stop hook and the in-process MCP
    # handler (`_handlers._advance_turn`) cannot drift — the 2.6.4
    # audit found them already diverged. `triggered_from="stop_hook"`
    # tags the source.
    #
    # Re-audit dedup: a long autonomous turn stops many times with the
    # same last user message, and each stop used to re-probe and
    # re-flag it (7 identical `search_miss` events from one ship-go
    # message on the 2026-07-03 dogfood log). A repeat still records
    # `turn_audited` — with `repeat=True`, so cadence stays observable
    # and eval/health can exclude it — but never a second
    # `search_miss`. The hash is computed with the production
    # `redact_query` so it compares equal to what the Recorder wrote.
    probe_mode = cfg.behavior.search_mode or "hybrid"
    repeat = is_duplicate_audit(
        recent,
        session_id=session_id,
        probe_query_hash=redact_query(user_message)["hash"],
        probe_query_text=user_message,
        now=utcnow(),
    )
    recorder.record(
        "turn_audited",
        **turn_audited_fields(
            report,
            session_id=session_id,
            probe_mode=probe_mode,
            assistant_present=assistant_response is not None,
            triggered_from="stop_hook",
            repeat=repeat,
            client_model=client_model,
        ),
    )
    if report.is_miss and not repeat:
        recorder.record(
            "search_miss",
            **search_miss_fields(
                report,
                session_id=session_id,
                triggered_from="stop_hook",
                client_model=client_model,
            ),
        )

    # Post-hoc use settlement — the turn's end is the semantically
    # correct place to decide what happened to this turn's retrievals.
    # Two tiers per pending retrieval:
    #
    # 1. ATTRIBUTION: phrase/containment-match recently-retrieved
    #    memories' bodies against the reply text; a match records
    #    `applied` with the matched sentence as the excerpt and
    #    `attribution="hook"` — the same shape an explicit model
    #    record_use would have produced.
    # 2. AUTO-FALLBACK: everything retrieved-but-unmatched records the
    #    plain `applied, auto=True, attribution="auto"` event HERE, at
    #    turn end. The in-process auto-commit used to own this and
    #    fired mid-turn (its clock is handler entries, and a tool-heavy
    #    turn advances it fast), which marked retrievals used before
    #    the reply even existed — starving tier 1 of everything it
    #    would have matched. `session.consume_old_tokens` now holds the
    #    in-process fallback behind a wall-clock floor
    #    (`AUTO_COMMIT_MIN_AGE_SECONDS`) so this hook settles first;
    #    the in-process path remains the fallback for hookless
    #    deployments. `_advance_turn` reads these events off the log
    #    and purges the matching in-memory tokens, so each retrieval
    #    generates one applied event total.
    if assistant_response:
        _emit_hook_attributions(
            store=store,
            recorder=recorder,
            recent_events=recent,
            session_id=session_id,
            assistant_response=assistant_response,
            worktree_root=caller_origin.worktree_root,
            client_model=client_model,
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


def _disabled_scopes_from_events(
    events: list[dict[str, Any]],
    *,
    worktree_root: str | None = None,
) -> set[str]:
    """Reconstruct the session-disabled scope set from the event log.

    `memory_scope_disable` / `memory_scope_enable` append `scope_disable`
    / `scope_enable` events stamped with the MCP server's stable
    per-process session id. The Stop hook runs in a separate process and
    can't read the server's in-memory `SessionState.disabled_scopes`, but
    it CAN replay those events off disk.

    The replay is anchored to the *current in-process server session* —
    the session id of the most recent event that did NOT originate from a
    Stop hook (see `_latest_in_process_session`; `worktree_root` is this
    hook's worktree, threaded through so a concurrent session in another
    worktree can't hijack the anchor and leak its scope toggles into this
    window's probe exclusions). Anchoring this way is what preserves
    reset-on-restart semantics: a restarted server mints a fresh session
    id and has emitted no scope toggles under it yet, so the
    reconstructed set is empty until the user disables a scope again —
    mirroring the in-memory state the server itself reset on startup.

    Returns the net disabled set: each `scope_disable` adds, each later
    `scope_enable` removes, walked in chronological (append) order. Empty
    when no in-process session is found or it toggled no scopes.
    """
    server_session = _latest_in_process_session(events, worktree_root=worktree_root)
    if server_session is None:
        return set()
    disabled: set[str] = set()
    for event in events:
        # Canonical-first session lookup with legacy fallback — same
        # discipline `_pending_retrievals` uses.
        if (event.get("session") or event.get("session_id")) != server_session:
            continue
        kind = event.get("kind")
        if kind not in ("scope_disable", "scope_enable"):
            continue
        scope = event.get("scope")
        if not isinstance(scope, str):
            continue
        if kind == "scope_disable":
            disabled.add(scope)
        else:
            disabled.discard(scope)
    return disabled


def _latest_in_process_session(
    events: list[dict[str, Any]],
    *,
    worktree_root: str | None = None,
) -> str | None:
    """Session id of the most recent non-Stop-hook event, or None.

    In-process MCP tool calls and the Stop hook write to the same event
    log under different session-id spaces (the server's `sess_<hex>` vs.
    Claude Code's transcript session id). Stop-hook events tag
    `triggered_from="stop_hook"`; everything else is in-process. The last
    such event identifies the live server session whose disabled-scope
    toggles the hook should honour. `events` is in chronological (append)
    order, so the latest match is found by walking in reverse.

    `worktree_root` makes the anchor concurrency-safe: the store and
    event log are shared across ALL projects and processes, so with two
    concurrent Claude Code windows the latest event frequently belongs
    to the OTHER window's server — which used to hijack the anchor and
    flip the audit verdict both ways (the foreign session's search
    shielding this window's miss; the foreign session's unrelated events
    unshielding a turn that searched correctly). The server-side
    Recorder already stamps `worktree_root` on every in-process event,
    so when the hook's own worktree is known we prefer the latest event
    whose stamp matches it. When no stamped match exists (legacy logs
    written before the stamp shipped, a server running outside any git
    checkout, or the restart gap before this worktree's server writes
    its first event) we fall back to the latest-any behaviour, which
    preserves the documented reset-on-restart semantics.
    """
    fallback: str | None = None
    for event in reversed(events):
        if event.get("triggered_from") == "stop_hook":
            continue
        session = event.get("session") or event.get("session_id")
        if not isinstance(session, str):
            continue
        if worktree_root is None or event.get("worktree_root") == worktree_root:
            return session
        if fallback is None:
            fallback = session
    return fallback


def _emit_hook_attributions(
    *,
    store: Store,
    recorder: Recorder,
    recent_events: list[dict[str, Any]],
    session_id: str,
    assistant_response: str,
    worktree_root: str | None = None,
    client_model: str | None = None,
) -> None:
    """Settle this turn's pending retrievals: emit attributed `applied`
    events for reply-matched memories and the plain auto-fallback
    `applied` for the rest.

    `pending` is the set of memory_ids retrieved (via `search`, `show`,
    or `list`/`list_active`) within the lookback window, MINUS ids that
    already have any
    `use` event in the same window — those have either been explicitly
    recorded by the model or auto-committed already, and re-attributing
    would double-count. The matcher's heuristics (≥6-token, ≥30-char,
    stopword-filtered candidate sentences) cap false positives; the
    "no-already-used" filter caps double-counting.

    Session-id bridge (load-bearing): `session_id` is the Claude Code
    transcript id, but the in-process MCP server wrote its `search`/`show`/
    `use` events under a DIFFERENT id space (`sess_<hex>`). Filtering
    retrievals by the transcript id — as this did before — never matched
    them, so `pending` was always empty and the hook NEVER attributed in
    production (only the single-id-space test fixtures passed). Bridge to
    the live in-process session the same way `_disabled_scopes_from_events`
    does. Retrievals are matched on that server session; the use-dedup set
    spans BOTH ids, because model/auto `use` events live under the server
    session while prior-turn hook attributions were recorded under the
    transcript id (this hook's own Recorder uses `session_id`).
    `worktree_root` is this hook's worktree, threaded into the anchor so
    a concurrent window's server can't claim the attribution session.
    """
    server_session = _latest_in_process_session(
        recent_events, worktree_root=worktree_root
    )
    retrieval_session = server_session or session_id
    pending = _pending_retrievals(
        recent_events,
        retrieval_session_id=retrieval_session,
        used_session_ids={retrieval_session, session_id},
        lookback_seconds=_ATTRIBUTION_LOOKBACK_SECONDS,
    )
    if not pending:
        return
    bodies: dict[str, str] = {}
    # Sorted iteration: `pending` is a set, and both event payloads
    # below inherit this order — deterministic ids keep the log
    # reproducible across runs (same discipline as consume_old_tokens).
    for memory_id in sorted(pending):
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
    if matches:
        fields: dict[str, Any] = {
            "ids": [m.memory_id for m in matches],
            "outcome": "applied",
            "auto": False,
            "attribution": "hook",
            "claim_excerpts": [m.claim_excerpt for m in matches],
            "triggered_from": "stop_hook",
        }
        if client_model is not None:
            fields["client_model"] = client_model
        recorder.record("use", **fields)
    # Auto-fallback for the retrieved-but-unmatched remainder — the
    # turn is over, the reply exists, and these memories demonstrably
    # did not shape it verbatim or by containment. Settling them NOW
    # (instead of the in-process ~2-turn TTL, which fired mid-turn and
    # starved the matcher) is the whole point of the wall-clock floor
    # in `session.consume_old_tokens`. Same event shape the in-process
    # fallback emits, so every downstream consumer (eval's
    # auto/explicit split, health's endorsement ratio, the
    # `_already_recorded_pending_ids` purge) reads it identically.
    matched_ids = {m.memory_id for m in matches}
    unmatched = [mid for mid in bodies if mid not in matched_ids]
    if unmatched:
        auto_fields: dict[str, Any] = {
            "ids": unmatched,
            "outcome": "applied",
            "auto": True,
            "attribution": "auto",
            "triggered_from": "stop_hook",
        }
        if client_model is not None:
            auto_fields["client_model"] = client_model
        recorder.record("use", **auto_fields)


def _pending_retrievals(
    events: list[dict[str, Any]],
    *,
    retrieval_session_id: str,
    used_session_ids: set[str],
    lookback_seconds: int,
) -> set[str]:
    """Memory_ids retrieved within the lookback window that have NOT yet
    been recorded via `record_use`.

    A retrieval is the `search`/`list`/`list_active` event's `returned`
    list or the `show` event's `id` — a `memory_list` surfaces ids the
    same way a search does, so listed ids are eligible for attribution
    too. A `use` event for the same id within the window counts as
    already-recorded and removes the id from the pending set. This
    approximates the in-process SessionState's `pending_use_tokens` from
    the event log alone, which is what the hook has access to.

    The session filter is SPLIT by event kind to bridge the two id
    spaces (see `_emit_hook_attributions`): `search`/`show`/`list`
    retrievals are matched on `retrieval_session_id` (the in-process
    server session that actually wrote them), while `use` dedup events
    are matched on `used_session_ids` (the server session AND the
    transcript id, since prior-turn hook attributions live under the
    latter).
    """
    cutoff_ts = utcnow().timestamp() - lookback_seconds
    retrieved: set[str] = set()
    used: set[str] = set()
    for event in events:
        ts = parse_event_ts(event.get("ts"))
        if ts is None or ts.timestamp() < cutoff_ts:
            continue
        # Canonical-first session lookup with legacy fallback — same
        # discipline 70e41a4 established for llm.py.
        session = event.get("session") or event.get("session_id")
        kind = event.get("kind")
        if kind in ("search", "list", "list_active"):
            # `list`/`list_active` (memory_list) surfaces memory ids exactly
            # like a `search` hit — the `_list_active` handler records the
            # ids under the same `returned` field name precisely so the
            # attribution pass and the probe's shield read one shape. Treat
            # them on the same branch so memories seen only via a listing are
            # eligible for hook attribution; the probe shield in audit.py
            # already groups these kinds the same way.
            if session != retrieval_session_id:
                continue
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
            if session != retrieval_session_id:
                continue
            mid = event.get("id")
            if isinstance(mid, str):
                retrieved.add(mid)
        elif kind == "use":
            if session not in used_session_ids:
                continue
            # Legacy fallback for `memory_ids` — same class as above.
            ids = event.get("ids") or event.get("memory_ids") or []
            if isinstance(ids, list):
                for mid in ids:
                    if isinstance(mid, str):
                        used.add(mid)
    return retrieved - used


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
        user, assistant, model = _extract_last_exchange(transcript_path)
        if not user:
            return 0

        result = run_audit(
            user_message=user,
            assistant_response=assistant,
            session_id=str(session_id),
            client_model=model,
        )
        if not args.quiet:
            print(json.dumps(result), file=sys.stdout)
    except Exception as exc:  # noqa: BLE001 — hook must never block turn end
        print(f"bettermemory audit-turn: {exc}", file=sys.stderr)
    return 0
