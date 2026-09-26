"""memory_acknowledge_miss MCP tool — per-event silent-miss resolution.

The bulk `silent_miss_cutoff` hatch (written by `bettermemory
consolidate --acknowledge-misses-before <ts>`) wipes EVERY pre-cutoff
miss in one stroke — surgical when the operator wants to invalidate a
batch of false positives a fix has retroactively cleared, but blunt
when only one event in the window is the false positive. T4 closes
that gap: `memory_acknowledge_miss(event_id, reason)` emits one
`miss_ack` event referencing the original `search_miss`. The
`compute_health` / `curation_counts` rollups drop matching events
from both `miss_total` and `unique_miss_memories` (the ack-filter
sits alongside the tombstone filter; see `health.py:_silent_miss_stats`).

How event_ids reach the model: `compute_health` surfaces a bounded
`recent_silent_misses` list on every `memory_health` call, each entry
carrying the per-event ULID stamped at emission time by
`search_miss_fields`. The model triages the list, picks the false
positives, calls this tool with the id + a short reason. Legacy
`search_miss` events written before T4 added the field cannot be
acknowledged individually — the bulk cutoff remains the only escape
hatch for those.

Idempotent: a second ack for the same event_id returns the same
`{"status": "acknowledged"}` shape without emitting a duplicate
`miss_ack` event. The rollup tolerates duplicate acks defensively
(the set semantic collapses them) but the handler is the canonical
gate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


# Minimum length on the free-form `reason` field. Free enough for the
# model to write a one-liner ("stopword-heavy query; no real intent"),
# strict enough to prevent drive-by `ack("ok")` spam that would erode
# the audit trail's value. Eight chars is "false positive" minus three;
# shorter than the bulk cutoff command's reason-banner but enough to
# carry signal.
_MIN_REASON_LENGTH = 8

# Maximum length on the free-form `reason` field. The min floor keeps
# drive-by `ack("ok")` spam out of the audit trail; this ceiling keeps a
# runaway model (or a hostile client) from inflating the JSONL event log
# with a multi-megabyte reason. 500 chars covers any reasonable one-liner
# rationale, and deliberately does NOT follow `_NOTE_MAX_LEN`'s raise to
# 800 — an ack reason is a triage one-liner over a tiny population, not
# an evidence-carrying note (the T3 note-cap decision); pasting
# a whole transcript belongs in a memory body, not an ack reason.
_MAX_REASON_LENGTH = 500


DESC_MEMORY_ACKNOWLEDGE_MISS = (
    "Acknowledge ONE `search_miss` event as a false positive. "
    "Emits a `miss_ack` event referencing the original event by id; "
    "future `memory_health` and `memory_scope_overview` rollups "
    "exclude the acked miss from both `miss_total` and "
    "`unique_miss_memories`.\n\n"
    "When to use: `memory_health` surfaces a bounded "
    "`recent_silent_misses` list (each entry: `{event_id, top_hit_id, "
    "query_preview, ts}`). Scan it; if an entry is a false positive — "
    "stopword-heavy query, audit fired on a turn that didn't need "
    "retrieval, the top-hit memory is irrelevant to the actual user "
    "intent — feed its `event_id` here.\n\n"
    "How it differs from `bettermemory consolidate "
    "--acknowledge-misses-before <ts>`: that command writes ONE "
    "`silent_miss_cutoff` event that wipes EVERY pre-cutoff miss, "
    "legitimate or not. This tool surgically targets one event so "
    "legitimate misses keep counting.\n\n"
    "Parameters:\n"
    "- `event_id` (required): the per-event ULID stamped on the "
    "original `search_miss`. Must reference an existing search_miss "
    "in the event log. Legacy events written before this field "
    'existed return `{"status": "not_found", ...}` — use the '
    "bulk cutoff for those.\n"
    f"- `reason` (required, {_MIN_REASON_LENGTH}–{_MAX_REASON_LENGTH} "
    "chars): free-form "
    'explanation captured for audit purposes (e.g. "stopword query, '
    'no real intent", "top hit irrelevant to actual user turn"). '
    "The text persists in the event log and downstream miss-probe "
    "tuning can consume it.\n\n"
    'Returns `{"status": "acknowledged", "event_id", '
    '"reason"}` on success. Idempotent — a second ack for the '
    "same `event_id` returns the success shape without emitting a "
    'duplicate `miss_ack`. Returns `{"status": "not_found", '
    '"event_id", "hint"}` when no `search_miss` with the given '
    "id exists anywhere in the event log — the lookup covers "
    "rotated archives too, so a not_found id is mistyped, stale, "
    "or predates per-event ids; check "
    "`memory_health.recent_silent_misses` for live ids. "
    'Returns `{"status": "wrong_kind", ...}` when '
    "the id is found but the event is not a `search_miss`. The ack "
    "persists in the event log — once written, all future health "
    "rollups exclude the miss; the rollups read rotated archives "
    "too, so rotation does not undo an ack."
)


async def memory_acknowledge_miss(
    deps: ToolHandlers, event_id: str, reason: str, ctx: Context | None = None
) -> dict[str, Any]:
    """Acknowledge one `search_miss` event as a false positive.

    Validates that `event_id` references an existing `search_miss` in
    the event log; emits one `miss_ack` carrying the original id and
    the caller's `reason`; returns a structured status payload.
    Idempotent — a second call for the same event_id detects the
    existing `miss_ack` and short-circuits without re-emitting.
    """
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    stripped_reason = reason.strip()
    if len(stripped_reason) < _MIN_REASON_LENGTH:
        raise ValueError(
            f"reason must be at least {_MIN_REASON_LENGTH} characters "
            f"after stripping whitespace (got {len(stripped_reason)})"
        )
    if len(stripped_reason) > _MAX_REASON_LENGTH:
        raise ValueError(
            f"reason is {len(stripped_reason)} chars — cap is "
            f"{_MAX_REASON_LENGTH}. Write a one-line rationale; the ack "
            "reason lands in the event log, not a memory body."
        )
    target_event_id = event_id.strip()

    # Single pass over the event log. We need BOTH lookups (does the
    # search_miss exist? has it already been acked?) so we accumulate
    # in the same walk rather than iterating twice. The active + archive
    # log is bounded by `max_bytes`, so even on a chatty store this is
    # cheap — same shape `memory_health` walks once per call.
    found_search_miss = False
    already_acked = False
    wrong_kind: str | None = None
    for ev in deps.store.iter_events():
        kind = ev.get("kind")
        ev_event_id = ev.get("event_id")
        if not isinstance(ev_event_id, str) or ev_event_id != target_event_id:
            continue
        if kind == "search_miss":
            found_search_miss = True
        elif kind == "miss_ack":
            # Existing ack means this id has been acknowledged before.
            # We still want to confirm a matching search_miss exists
            # (so the result distinguishes "already acked" from
            # "ack written against a fabricated id"), but a second
            # walk would double the cost — and the only way a
            # `miss_ack` lands in the log is via this handler, which
            # validates the search_miss before emitting. Trust the
            # log: an existing ack means the search_miss existed at
            # ack time.
            already_acked = True
            found_search_miss = True
        elif wrong_kind is None:
            # Different event kind sharing this id. Surface the kind so
            # the caller can diagnose without re-grepping the log;
            # only retain the FIRST mismatched kind seen for a clean
            # error (later events of the same id with different kinds
            # would all be data-integrity bugs, not actionable).
            wrong_kind = kind if isinstance(kind, str) else None

    if found_search_miss and already_acked:
        # Idempotent success — return the same shape as a fresh ack so
        # the caller's branch on `status` collapses to one path.
        return {
            "status": "acknowledged",
            "event_id": target_event_id,
            "reason": stripped_reason,
        }
    if not found_search_miss:
        if wrong_kind is not None:
            return {
                "status": "wrong_kind",
                "event_id": target_event_id,
                "kind": wrong_kind,
                "hint": (
                    f"event_id {target_event_id!r} references a "
                    f"{wrong_kind!r} event, not a search_miss. Only "
                    "search_miss events can be acknowledged."
                ),
            }
        return {
            "status": "not_found",
            "event_id": target_event_id,
            "hint": (
                "No search_miss event with this id anywhere in the log. "
                "Either the id is mistyped or stale (check the health "
                "report's recent_silent_misses for live ids) or the event "
                "predates per-event ids and cannot be acknowledged "
                'individually — use memory_admin(action="acknowledge_miss", '
                "before=<ts>) for those."
            ),
        }

    # Found a fresh search_miss with no prior ack — emit the ack.
    deps.recorder.record(
        "miss_ack",
        event_id=target_event_id,
        reason=stripped_reason,
        session_id=state.session_id,
    )
    return {
        "status": "acknowledged",
        "event_id": target_event_id,
        "reason": stripped_reason,
    }


# How far ahead of now a bulk cutoff may sit. A cutoff in the future would
# silence misses that have not happened yet; a day of slack absorbs clock
# skew between the caller and this host.
_CUTOFF_FUTURE_SLACK = timedelta(days=1)


def canonical_cutoff(value: str) -> str:
    """Validate a bulk-acknowledgement cutoff and return it as the
    recorder's UTC spelling. The offset must be explicit (a trailing Z or
    a numeric offset): a naive local time from a caller in another zone
    would land the cutoff hours off and silence the wrong misses. Raises
    ValueError with the reason."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("`before` must be an ISO-8601 timestamp")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"`before` is not ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"`before` needs an explicit UTC offset or a trailing Z: {value!r}"
        )
    now = datetime.now(timezone.utc)
    if parsed > now + _CUTOFF_FUTURE_SLACK:
        raise ValueError(f"`before` lies in the future: {value!r}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def acknowledge_misses_before(
    deps: ToolHandlers,
    before: str,
    *,
    reason: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Acknowledge every `search_miss` earlier than `before` at once.

    Writes one additive `silent_miss_cutoff` event; the health and eval
    rollups honour the latest cutoff seen and drop `turn_audited` and
    `search_miss` events stamped earlier than it. Nothing is removed from
    the log. Refused when telemetry is off, because the cutoff is itself
    a telemetry event and a disabled recorder would drop it silently.
    """
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    cutoff = canonical_cutoff(before)
    if not deps.recorder.enabled:
        raise ValueError(
            "telemetry is disabled, so the cutoff event would be dropped; "
            "enable [telemetry] before acknowledging misses in bulk"
        )
    note = (reason or "").strip() or None
    if note is not None and len(note) > _MAX_REASON_LENGTH:
        raise ValueError(f"reason is over the {_MAX_REASON_LENGTH}-char cap")
    deps.recorder.record(
        "silent_miss_cutoff",
        cutoff_ts=cutoff,
        note=note,
        session_id=state.session_id,
    )
    return {"status": "cutoff_recorded", "cutoff_ts": cutoff, "reason": note}


__all__ = [
    "DESC_MEMORY_ACKNOWLEDGE_MISS",
    "acknowledge_misses_before",
    "canonical_cutoff",
    "memory_acknowledge_miss",
]
