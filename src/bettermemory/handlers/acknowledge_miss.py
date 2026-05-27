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

from typing import TYPE_CHECKING, Any

from ..events import iter_all_events
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
    f"- `reason` (required, >= {_MIN_REASON_LENGTH} chars): free-form "
    'explanation captured for audit purposes (e.g. "stopword query, '
    'no real intent", "top hit irrelevant to actual user turn"). '
    "The text persists in the event log and downstream miss-probe "
    "tuning can consume it.\n\n"
    'Returns `{"status": "acknowledged", "event_id", '
    '"reason"}` on success. Idempotent — a second ack for the '
    "same `event_id` returns the success shape without emitting a "
    'duplicate `miss_ack`. Returns `{"status": "not_found", '
    '"event_id", "hint"}` when no `search_miss` with the given '
    "id is in the active event log (it may have rotated to archive "
    "or never existed); check `memory_health.recent_silent_misses` "
    'for live ids. Returns `{"status": "wrong_kind", ...}` when '
    "the id is found but the event is not a `search_miss`. The ack "
    "persists in the event log — once written, all future health "
    "rollups exclude the miss until the log is rotated past it."
)


async def memory_acknowledge_miss(
    deps: "ToolHandlers", event_id: str, reason: str, ctx: Context | None = None
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
    target_event_id = event_id.strip()

    # Single pass over the event log. We need BOTH lookups (does the
    # search_miss exist? has it already been acked?) so we accumulate
    # in the same walk rather than iterating twice. The active + archive
    # log is bounded by `max_bytes`, so even on a chatty store this is
    # cheap — same shape `memory_health` walks once per call.
    found_search_miss = False
    already_acked = False
    wrong_kind: str | None = None
    for ev in iter_all_events(deps.store.root):
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
                "No search_miss event with this id is in the active log. "
                "The event may have rotated to archive (the active log "
                "rotates at ~10MB by default); check "
                "memory_health.recent_silent_misses for live ids. Note "
                "that search_miss events written before T4 lack an "
                "event_id field and cannot be acknowledged individually "
                "— use `bettermemory consolidate "
                "--acknowledge-misses-before <ts>` for the bulk hatch."
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


__all__ = ["DESC_MEMORY_ACKNOWLEDGE_MISS", "memory_acknowledge_miss"]
