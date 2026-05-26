"""memory_audit_turn MCP tool — silent-retrieval-miss telemetry.

Description-edit history:

- H6 (Round 2): added a leading "Not for in-conversation use" stop
  line. The tool is dispatched by the client's end-of-turn Stop hook
  through the MCP channel; the model should never call it directly,
  and pre-Round-2 the description didn't make that loud enough to
  prevent occasional model calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..audit import (
    DEFAULT_LOOKBACK_SECONDS,
    probe_for_miss,
    search_miss_fields,
    turn_audited_fields,
)
from ..events import iter_events
from ..models import utcnow
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_AUDIT_TURN = (
    "Not for in-conversation use. This tool is dispatched by the "
    "client's end-of-turn Stop hook through the MCP channel; the "
    "model should never call this directly.\n\n"
    "Silent-miss telemetry. Call from a client-side end-of-turn hook "
    "with the user's message (and optionally the assistant's reply) to "
    "detect turns where memory *should* have been retrieved but wasn't. "
    "Runs a cheap search probe over the active store using the model's "
    "configured search mode (matches what the model would have done), "
    "then checks whether a `memory_search` OR `memory_show` event fired "
    "in the same session within `lookback_seconds` (default 60). When "
    "a high-relevance hit exists AND no retrieval happened in the "
    "window, emits a `search_miss` event so memory_health / "
    "memory_scope_overview can surface the rate. Returns a structured "
    "`MissReport` with `verdict` in {'miss', 'ok', 'no_signal'} plus "
    "the top probe hits for offline triage. This tool is the "
    "false-negative half of the retrieval contract — without it, the "
    "cost of opt-in retrieval (model didn't search when it should "
    "have) is structurally invisible. Auto-scopes to the caller's "
    "repo so the probe matches the model's view; honours "
    "session-disabled scopes. Side-effects: emits `turn_audited` "
    "always, plus `search_miss` when the verdict is `miss`. Safe to "
    "call after every turn; cost is one search sweep over the active "
    "store."
)


async def memory_audit_turn(
    deps: "ToolHandlers",
    user_message: str,
    assistant_response: str | None = None,
    lookback_seconds: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Detect silent retrieval misses for a just-completed turn.

    Fires from a client-side hook (Claude Code Stop hook, etc.) with
    the user's message. Runs a search probe (using the model's
    configured search mode) over the active store; if a
    high-relevance hit exists AND no retrieval event (`search` or
    `show`) fired in the same session within the lookback window,
    emits `search_miss` so curation views can surface the rate.

    `assistant_response` is accepted but currently used only to keep
    the API shape stable — a future probe will run against it too
    (the response text is where unsearched citations land).
    Validating now keeps the wire shape settled.

    Always emits `turn_audited` so audit cadence is visible in the
    log even when there's nothing to flag; emits `search_miss` only
    when `verdict == "miss"`.

    Known v1 limitations:

    - **Any retrieval shields**: the probe shields on ANY recent
      `search` or `show` in the window, even if that retrieval was
      for an unrelated query. A turn that searched for X but
      missed an unrelated B-relevant retrieval won't be flagged.
      Tightening this would require per-hit shielding — out of
      scope for v1.
    - **Cross-process audits**: the audit must share its
      SessionState with the model (same MCP `client_id`). A hook
      that opens its own MCP connection would get a fresh session
      and always see zero recent retrievals, false-flagging every
      turn. Production hooks must run in-process with the model.
    """
    from .. import _handlers as _h

    if not isinstance(user_message, str):
        raise ValueError("user_message must be a string")
    if assistant_response is not None and not isinstance(assistant_response, str):
        raise ValueError("assistant_response must be a string if provided")

    # Clamp lookback. Lower bound 1s (don't accept 0/negative — that
    # would always flag); upper bound 600s (10 minutes) so a misused
    # hook can't silence the audit by passing a huge window.
    if lookback_seconds is None:
        window = DEFAULT_LOOKBACK_SECONDS
    else:
        window = max(1, min(int(lookback_seconds), 600))

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)

    # Active-log iter is sufficient for a 60s lookback: rotation
    # thresholds are far larger than that window in normal use, so a
    # search event from this session within the window is still in
    # `.events.jsonl`. If a future deployment cranks the rotation to
    # something pathological, the right fix is to widen the iter
    # here, not to silently undercount misses.
    memories = deps.store.load_all()
    recent = list(iter_events(deps.store.root))

    current_origin = _h.capture_origin()
    # Probe uses the same search mode the model would have used —
    # otherwise we'd be measuring "would a different scorer have
    # hit" rather than "did the model miss what its ranker would
    # have shown." Falls through to `"hybrid"` (the package
    # default since 2.6.8) when the config doesn't carry an override.
    probe_mode = deps.config.behavior.search_mode or "hybrid"
    report = probe_for_miss(
        memories,
        user_message,
        recent_events=recent,
        session_id=state.session_id,
        now=utcnow(),
        lookback_seconds=window,
        caller_origin=current_origin,
        excluded_scopes=set(state.disabled_scopes),
        mode=probe_mode,
    )

    # `turn_audited` records that the audit ran at all — distinct
    # from `search_miss`, which only fires on a flagged turn. The
    # split lets `memory_health` derive a denominator (audits run)
    # for the silent-miss *rate* without conflating "audit didn't
    # run this turn" with "audit ran and found nothing."
    deps.recorder.record(
        "turn_audited",
        **turn_audited_fields(
            report,
            session_id=state.session_id,
            probe_mode=probe_mode,
            assistant_present=assistant_response is not None,
            triggered_from="mcp_tool",
        ),
    )
    if report.is_miss:
        deps.recorder.record(
            "search_miss",
            **search_miss_fields(
                report,
                session_id=state.session_id,
                triggered_from="mcp_tool",
            ),
        )
    return report.to_dict()


__all__ = ["DESC_MEMORY_AUDIT_TURN", "memory_audit_turn"]
