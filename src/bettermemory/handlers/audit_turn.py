"""memory_audit_turn MCP tool — silent-retrieval-miss telemetry.

Description-edit history:

- H6 (Round 2): added a leading "Not for in-conversation use" stop
  line. The tool belongs to the client's end-of-turn Stop hook — the
  shipped plugin binds that hook to the `bettermemory audit-turn`
  CLI, and a client is free to wire it to this tool instead. Either
  way the model should never call it directly, and pre-Round-2 the
  description didn't make that loud enough to prevent occasional
  model calls.
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
from .search import (
    default_search_width,
    ranking_events_window_seconds,
    resolve_ranking_inputs,
    resolve_search_pool,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


# Deliberately compact: this description is default-registered, so its
# full length is a per-turn context cost paid by the ONLY reader that
# cannot act on it — the model, which the first sentence tells never to
# call this. The tool is dispatched by the client's Stop hook, and a hook
# author reads docs/api.md (`memory_audit_turn`), where the probe
# construction, the versioned threshold rule and the calibration payload
# live in full. What stays here is the never-call banner, every parameter,
# every return-shape key, the side-effects, and the retrieval-event set
# tests/test_prompts.py pins ("memory_list" — the predicate must stay
# spelled out, not deferred).
#
# The Stop hook this project ships does not reach the tool over the MCP
# channel: `plugin/hooks/hooks.json` dispatches `uvx bettermemory
# audit-turn --quiet` — the CLI, which writes `turn_audited` /
# `search_miss` to the event log itself and never opens an MCP session.
# That dispatch is pinned by `test_stop_hook_calls_audit_turn` in
# tests/test_plugin.py, and the maintainer's own event log agrees:
# every audited turn in it arrives `triggered_from="stop_hook"`.
#
# That is not a licence to gate the tool behind `full_tool_surface`. The
# evidence is n=1 — it shows THIS plugin path goes through the CLI, not
# that no client wires its Stop hook to the MCP tool, which the handler
# below still supports (`triggered_from="mcp_tool"`). Making registration
# conditional would withdraw the tool from every default install, and the
# compatibility contract does not allow removing a tool within a major
# version.
DESC_MEMORY_AUDIT_TURN = (
    "Not for in-conversation use. This tool is dispatched by the "
    "client's end-of-turn Stop hook; the model should never call "
    "this directly.\n\n"
    "Silent-miss telemetry (full reference in docs/api.md). Runs the "
    "search probe `memory_search` would have run for `user_message` "
    "(`assistant_response` optional), then checks whether a "
    "`memory_search`, `memory_show`, `memory_list`, or hook-injected "
    "`prompt_recall` event fired in the same session within "
    "`lookback_seconds` (default 60). A "
    "high-relevance probe hit with no retrieval in that window is a "
    "miss. Auto-scopes to the caller's repo so the probe matches the "
    "model's view; honours session-disabled scopes. Returns a "
    "`MissReport` with `verdict` in {'miss', 'ok', 'no_signal'} plus "
    "the top probe hits. Side-effects: emits `turn_audited` always, "
    "plus `search_miss` when the verdict is `miss`."
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
    the user's message. Runs a search probe over the pool and ranking
    inputs `memory_search` would have used for that message — same
    candidate set (`resolve_search_pool`), same `[behavior]` factors
    (`resolve_ranking_inputs`), same configured search mode; if a
    high-relevance hit exists AND no retrieval event (`search`,
    `show`, `list`, or `prompt_recall`) fired in the same session
    within the lookback window, emits `search_miss` so curation views
    can surface the rate.

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

    # Captured before the candidate pool because the pool's filters are
    # this origin's: the probe auto-scopes to the caller's repo, and the
    # BM25 corpus statistics must be priced over the same admitted
    # collection the ranker will score.
    current_origin = _h.capture_origin()
    # Candidate pool: production's, not an unconditional `load_all()`.
    # `resolve_search_pool` is the same helper `memory_search` builds its
    # pool with — the FTS prefilter above `_INDEX_THRESHOLD_DEFAULT`, the
    # cap-starvation guard, and the BM25 corpus-statistics provider that
    # capped slice needs. Probing the whole corpus instead ranked a
    # strict SUPERSET of what the model's retrieval could reach, and the
    # miss verdict reads only the rank-1 hit, so a memory production's
    # prefilter would have dropped could take that slot and decide the
    # verdict on its own. The filters handed here are the ones
    # `probe_for_miss` will re-apply inside `run_search`, so the document
    # frequencies price exactly the collection about to be ranked.
    # `min_survivors` is the width of a DEFAULT `memory_search`
    # (`default_search_width` — the config knob under the same clamp a
    # request goes through, NOT the raw knob, which `config.py` never
    # range-checks), not the probe's `_TOP_HITS_RETAINED`: the starvation
    # guard has to fire on the same slices a default-width retrieval's
    # would. It does NOT track a wider or narrower REQUEST — there is no
    # request on this path, and `resolve_search_pool` records what that
    # leaves open.
    probe_pool = resolve_search_pool(
        deps.store,
        user_message,
        excluded_scopes=set(state.disabled_scopes),
        repo_filter=current_origin.repo,
        worktree_filter=current_origin.worktree_root,
        min_survivors=default_search_width(deps.config.behavior),
    )

    # Window-aware event read. Rotation triggers on SIZE (`max_bytes`),
    # not time, at a moment independent of turn boundaries — so "the
    # rotation threshold is far larger than the window" (the comment
    # this replaces) conflated bytes with seconds: a turn straddling a
    # rotation lost its own search event from a plain active-log read
    # and emitted a false miss. `iter_events_window` prepends the
    # newest rotated segment when the active log doesn't cover the
    # clamped lookback window.
    #
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
    # the read can't leak stale events into them. The usage-aware ranking
    # tallies below don't read this list at all: they take production's
    # own separately-scoped read, which is NARROWER than this one for
    # endorsement (600s) and WIDER for demotion (the 30-day negative
    # window). Both tallies enforce their own cutoffs internally either
    # way, so the choice of feed is about matching production's
    # rotation-proofing, not about bounding a count.
    # Mirrors the Stop hook (`hook.run_audit`), which reads
    # `REAUDIT_DEDUP_WINDOW_SECONDS`.
    recent = list(
        iter_events_window(deps.store.root, max(window, REAUDIT_DEDUP_WINDOW_SECONDS))
    )

    # Probe uses the same search mode the model would have used —
    # otherwise we'd be measuring "would a different scorer have
    # hit" rather than "did the model miss what its ranker would
    # have shown." Falls through to `"hybrid"` (the package
    # default since 2.6.8) when the config doesn't carry an override.
    probe_mode = deps.config.behavior.search_mode or "hybrid"
    # Config-driven ranking inputs: the SAME `RankingInputs` the
    # production search handler threads
    # (`handlers.search.resolve_ranking_inputs`), so this probe cannot
    # rank on a different set of factors than `memory_search` would have.
    # That covers both usage-aware directions — `endorsement_boost`
    # nudges applied memories up, `outcome_demotion` slides recently
    # ignored/contradicted ones down — plus `corroboration_boost` and the
    # recency half-life. Threading only the endorsement half was a
    # telemetry-honesty bug in both directions, because the miss verdict
    # reads ONLY the rank-1 hit: a memory production had demoted out of
    # the top slot still held rank 1 here, and the hit production's
    # demotion promoted instead was never the one this probe judged.
    #
    # The event read is issued HERE, not inside the helper, and is
    # separately scoped — NOT the dedup-widened `recent` above.
    # `ranking_events_window_seconds` is production's own width (600s
    # with endorsement alone, the full 30-day negative window once
    # demotion is on), so the audit ranker's rotation-proofing matches
    # production's; it returns None on the default config, which pays no
    # read at all. Both tallies additionally enforce their own cutoffs
    # internally, so neither feed could have widened a count either way.
    tally_window = ranking_events_window_seconds(deps.config.behavior)
    tally_events: list[dict[str, Any]] | None = None
    if tally_window is not None and probe_pool.memories:
        tally_events = list(iter_events_window(deps.store.root, tally_window))
    ranking = resolve_ranking_inputs(
        deps.store.root,
        probe_pool.memories,
        deps.config.behavior,
        now=utcnow(),
        events=tally_events,
    )
    report = probe_for_miss(
        probe_pool.memories,
        user_message,
        recent_events=recent,
        session_id=state.session_id,
        now=utcnow(),
        lookback_seconds=window,
        caller_origin=current_origin,
        excluded_scopes=set(state.disabled_scopes),
        mode=probe_mode,
        half_life_days=ranking.half_life_days,
        applied_by_id=ranking.applied_by_id,
        negative_by_id=ranking.negative_by_id,
        corroboration_boost=ranking.corroboration_boost,
        corpus_stats_provider=probe_pool.corpus_stats_provider,
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
