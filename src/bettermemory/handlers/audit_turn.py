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
    REAUDIT_DEDUP_WINDOW_SECONDS,
    is_duplicate_audit,
    probe_for_miss,
    search_miss_fields,
    turn_audited_fields,
)
from ..events import iter_events_window, redact_query
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
    "then checks whether a `memory_search`, `memory_show`, or "
    "`memory_list` event fired in the same session within "
    "`lookback_seconds` (default 60). When "
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
    high-relevance hit exists AND no retrieval event (`search`,
    `show`, or `list`) fired in the same session within the
    lookback window, emits `search_miss` so curation views can
    surface the rate.

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
    - **Cross-process audits**: THIS handler's retrieval shield is
      bound to the caller's MCP session
      (`deps.sessions.for_request(ctx)`), so a hook that opens its
      own MCP connection to call this tool still gets a fresh
      session, sees zero recent retrievals, and false-flags every
      turn. That no longer means hooks have to run in-process: the
      out-of-process Stop hook (`hook.run_audit`) is the primary
      production producer — it bypasses the MCP channel and bridges
      the shield to the live server session by replaying the event
      log (`retrieval_session_id` resolved via
      `_latest_in_process_session`, anchored to the hook's
      worktree; the shield also counts any in-window retrieval
      stamped with that worktree regardless of session, so a
      same-worktree concurrent session or a mid-conversation
      restart can't orphan this turn's own search). The residual
      gap is the bridge's anchor, not the process boundary: when no
      worktree-stamped in-process event exists (legacy logs, a
      server outside a git checkout, the restart gap) it falls back
      to latest-any session matching and can mis-shield in either
      direction. See the `hook.py` module docstring for the full
      divergence analysis.
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

    # Window-aware event read. Rotation triggers on SIZE (`max_bytes`),
    # not time, at a moment independent of turn boundaries — so "the
    # rotation threshold is far larger than the window" (the comment
    # this replaces) conflated bytes with seconds: a turn straddling a
    # rotation lost its own search event from a plain active-log read
    # and emitted a false miss. `iter_events_window` prepends the
    # newest rotated segment when the active log doesn't cover the
    # clamped lookback window.
    memories = deps.store.load_all()
    # Read the WIDE dedup window, not the narrow probe `window`. Two
    # consumers walk `recent` with different horizons: `probe_for_miss`
    # applies its own `lookback_seconds=window` cutoff internally
    # (via `_count_recent_retrievals`), and `is_duplicate_audit`
    # applies `REAUDIT_DEDUP_WINDOW_SECONDS` (3600s). Clamping the read
    # to the 60s probe window meant a prior `turn_audited` older than
    # `window` but inside the dedup horizon fell off the read across a
    # log rotation — the dedup missed it and a duplicate `search_miss`
    # inflated the miss numerator. Read `max(...)` so the dedup sees its
    # full history. The two time-scoped consumers of `recent` re-derive
    # their own narrower cutoff internally — `_count_recent_retrievals`
    # (via `probe_for_miss`, `lookback_seconds=window`) and
    # `is_duplicate_audit` (`REAUDIT_DEDUP_WINDOW_SECONDS`) — so widening
    # the read can't leak stale events into them. The endorsement tally is
    # the exception: `_explicit_applied_counts` applies NO cutoff of its
    # own, so it is fed a separately-scoped read below, NOT this list.
    # Mirrors the Stop hook (`hook.run_audit`), which reads
    # `REAUDIT_DEDUP_WINDOW_SECONDS`.
    recent = list(
        iter_events_window(deps.store.root, max(window, REAUDIT_DEDUP_WINDOW_SECONDS))
    )

    current_origin = _h.capture_origin()
    # Probe uses the same search mode the model would have used —
    # otherwise we'd be measuring "would a different scorer have
    # hit" rather than "did the model miss what its ranker would
    # have shown." Falls through to `"hybrid"` (the package
    # default since 2.6.8) when the config doesn't carry an override.
    probe_mode = deps.config.behavior.search_mode or "hybrid"
    # Resolve the semantic model exactly as the production search
    # handler does (`handlers/search.py`): the factory caches per
    # process, so the in-process audit can afford the same scorer the
    # model's retrieval would have used. When `semantic` mode has no
    # model available the probe returns an explicit `no_signal`
    # (`no_signal_reason="semantic_model_unavailable"`) rather than
    # erroring the tool call.
    semantic_model: Any | None = None
    if probe_mode in ("semantic", "hybrid"):
        semantic_model = deps._semantic_model_factory(deps.config)
    # Endorsement nudge: same opt-in tally the production search handler
    # feeds the ranker. It MUST be counted over the same horizon production
    # uses — `ATTRIBUTION_LOOKBACK_SECONDS` (handlers/search.py) — not the
    # dedup-widened `recent` above. `_explicit_applied_counts` applies no
    # cutoff, so feeding it the 3600s read would count applies from up to an
    # hour ago that production's 600s ranker never saw, letting the audit
    # probe nudge a near-tie top-1 to "high" and flip a false `search_miss`.
    # Read a separately-scoped window so the audit ranker matches production.
    applied_by_id: dict[str, int] | None = None
    if deps.config.behavior.endorsement_boost and memories:
        from ..audit import ATTRIBUTION_LOOKBACK_SECONDS
        from .search import _explicit_applied_counts

        endorsement_events = list(
            iter_events_window(deps.store.root, ATTRIBUTION_LOOKBACK_SECONDS)
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
        session_id=state.session_id,
        now=utcnow(),
        lookback_seconds=window,
        caller_origin=current_origin,
        excluded_scopes=set(state.disabled_scopes),
        mode=probe_mode,
        semantic_model=semantic_model,
        half_life_days=deps.config.behavior.recency_boost_half_life_days,
        applied_by_id=applied_by_id,
    )

    # `turn_audited` records that the audit ran at all — distinct
    # from `search_miss`, which only fires on a flagged turn. The
    # split lets `memory_health` derive a denominator (audits run)
    # for the silent-miss *rate* without conflating "audit didn't
    # run this turn" with "audit ran and found nothing."
    #
    # Re-audit dedup, mirroring the Stop hook: a repeated audit of the
    # same (session, message) inside `REAUDIT_DEDUP_WINDOW_SECONDS`
    # records `turn_audited` with `repeat=True` and skips the
    # companion `search_miss` — multi-stop turns re-dispatch the same
    # last user message and used to multiply one decision point into
    # N miss events. `client_model` stays unset on this producer: the
    # MCP channel carries no model identity (the transcript-reading
    # Stop hook is the only source).
    repeat = is_duplicate_audit(
        recent,
        session_id=state.session_id,
        probe_query_hash=redact_query(user_message)["hash"],
        probe_query_text=user_message,
        now=utcnow(),
    )
    deps.recorder.record(
        "turn_audited",
        **turn_audited_fields(
            report,
            session_id=state.session_id,
            probe_mode=probe_mode,
            assistant_present=assistant_response is not None,
            triggered_from="mcp_tool",
            repeat=repeat,
        ),
    )
    if report.is_miss and not repeat:
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
