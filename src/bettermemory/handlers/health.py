"""memory_health MCP tool — aggregate curation view.

Description-edit history:

- M-Health (Round 2): clarified the trigger conditions. Was advertised
  as "for curation passes" without saying when a model should reach
  for it; added the explicit cue (user asks, or scope_overview
  surfaces non-zero `dead` / `drifted` / `silent_misses`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..health import report_for_directory
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_HEALTH = (
    "Aggregate health view for curation passes. Run on user request "
    "('how's the memory looking?') or when `memory_scope_overview` "
    "shows non-zero `dead` / `drifted` / `silent_misses`. Don't call "
    "on every turn — use `memory_scope_overview` for the session-start "
    "branch; this is the deep report.\n\n"
    "Returns buckets (capped row lists; bucket sizes are full "
    "counts):\n"
    "- `dead_weight` — last touched > `window_days` ago, retrieved at "
    "least once (earliest retrieval past the 2-day endorsement grace), "
    "never applied, no unresolved contradiction. The ranker keeps "
    "surfacing it but the model never reaches for it.\n"
    "- `cold_memories` — created > `window_days` ago AND never "
    "retrieved at all. The ranker isn't surfacing it; the model "
    "isn't asking. `dead_weight` and `cold_memories` measure "
    "different failure modes — act on the right axis.\n"
    "- `heavily_used` — applied_count >= `min_applied` (default "
    "from config, typically 3).\n"
    "- `contradicted` — unresolved `record_use(contradicted)` "
    "events. Each row carries a `resolution_timeline` to debug "
    "stuck flags. Resolve via memory_update or memory_verify.\n"
    "- `verification_debt` — partition by never_verified / stale / "
    "fresh against `verification_stale_days`.\n"
    "- `commit_drift_debt` — when the server's in the memory's "
    "origin repo, memories with commits since last_verified_at.\n"
    "- `silent_misses` / `cold_endorsement_memories` — populated "
    "when `memory_audit_turn` has been firing (see that tool). The "
    "`silent_misses` payload carries `{audited_total, miss_total, "
    "unique_miss_memories}`: `miss_total` counts events, "
    "`unique_miss_memories` counts the distinct memories those "
    "misses pointed at (dedup'd by top-hit id). Misses against "
    "tombstoned memories are dropped from both — no longer "
    "actionable. `cold_endorsement_memories` counts distinct "
    "memories (NOT turns) with `retrieval_count >= N` AND zero "
    "explicit applies — usually a sign the memory is over-surfaced "
    "by the ranker or stale.\n"
    "- `scope_distribution` + `scope_health` per-scope rollup; "
    "`rare_scopes` flags Levenshtein-near-others singletons "
    "(likely typos — fix with memory_rename_scope).\n"
    "- `orphan_use_events` — record_use calls against ids that "
    "don't exist (fabrication smoke test).\n"
    "- `marker_stats` — transient-marker fire/override rates.\n"
    "- `recommendations` — closed-set actionable digest naming the "
    "buckets above that crossed thresholds. Each entry: `{kind, "
    "summary, action, count, memory_ids, scope}` where `kind` is "
    "one of `remove_dead_weight` / `resolve_contradicted` / "
    "`cleanup_cold_endorsements` / `verify_drifted` / "
    "`fix_typo_scopes`; empty list means nothing crossed.\n\n"
    "CLI equivalent: `bettermemory health [--json]`.\n\n"
    # Documented AFTER the `CLI equivalent:` line on purpose. The bucket
    # region above is sliced and set-compared against the report's wire
    # shape by `test_desc_memory_health_enumerates_report_bucket_keys`,
    # and this key is not a bucket — it is the gate that says whether
    # `dead_weight` was measurable at all. Listing it up there would
    # both break that parity and invite the model to read it as another
    # pile of rows to act on.
    "`telemetry_coverage` is non-null whenever the coverage gate ran: "
    "`{hook_telemetry_events, covered, dead_weight_suppressed, reason}`. "
    "When `dead_weight_suppressed` is true, `dead_weight` is empty BY "
    "CONSTRUCTION — the event log carries no Stop-hook settlement "
    "telemetry, so 'never applied' says nothing about the memory — NOT "
    "because the store is clean. `memory_scope_overview`'s "
    "`curation_pending.dead` reads zero under the same gate. Report the "
    "`reason` verbatim rather than 'no dead weight found'.\n\n"
    # Also documented AFTER `CLI equivalent:`, and for the same reason as
    # `telemetry_coverage`: the sliced region above is set-compared
    # against `HealthReport.to_dict()` by
    # `test_desc_memory_health_enumerates_report_bucket_keys`, and this is
    # not a curation bucket of memory rows — it is the sibling tier's size
    # gauge. Listing it up there would invite the model to treat episodes
    # as another pile of rows to curate, which is exactly the tier
    # confusion the rest of the surface works to avoid.
    "`episode_volume` is the sibling episode tier's size gauge: "
    "`{sessions, episodes, bytes, prunable_sessions, ttl_days}`. Episode "
    "CONTENT is still absent from every bucket above — this is the "
    "aggregate only. `prunable_sessions` is the actionable one: episode "
    "GC runs on `episode_write` and `bettermemory episodes prune` and "
    "nowhere else, so a read-only loop never collects. Non-zero means "
    "that many session directories are already collectable."
)


async def memory_health(
    deps: "ToolHandlers",
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    min_applied: int | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    # Falling through to the configured default lets the tool stay
    # ergonomic for the common case (don't pass anything, get the
    # tuned threshold) while still allowing a per-call override
    # ("show me everything that's been applied at least once on this
    # young store").
    threshold = (
        int(min_applied)
        if min_applied is not None
        else deps.config.behavior.heavily_used_min_applied
    )
    report = report_for_directory(
        deps.store.root,
        window_days=int(window_days),
        heavily_used_top_k=int(heavily_used_top_k),
        heavily_used_min_applied=threshold,
        verification_stale_days=deps.config.behavior.verification_stale_days,
        cold_endorsement_ratio_threshold=(
            deps.config.behavior.cold_endorsement_ratio_threshold
        ),
        # Pass caller_origin so the cwd-aware `commit_drift_debt`
        # rollup populates when the server is running inside a repo
        # whose memories live in this store.
        caller_origin=_h.capture_origin(),
    )
    return report.to_dict()


__all__ = ["DESC_MEMORY_HEALTH", "memory_health"]
