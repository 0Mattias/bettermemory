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
    "CLI equivalent: `bettermemory health [--json]`."
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
