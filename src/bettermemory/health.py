"""Aggregate health view of the memory store.

Reads from `events.iter_all_events` (active log + rotated archives) and
joins against `Store.load_all` to produce per-memory and per-marker
stats. Exposed two ways:

- as `memory_admin(action="health")` on the MCP surface (the
  `memory_health` handler), so the model can self-curate during a
  conversation,
- as `bettermemory health` on the CLI, for offline audit.

The metrics are designed around the failure modes the rest of this
project is trying to detect:

- **dead_weight**: memories that have been retrieved but never `applied`
  (zero-retrieval rows land in `cold_memories` instead). Either the
  search ranking isn't surfacing them, or they're noise. Either way,
  prune candidates. The rule is the shared `_is_dead_weight` predicate
  — freshest-touch window, unresolved-contradiction parking, and the
  endorsement grace all exempt.
- **heavily_used**: memories with high applied-count. These are working;
  don't touch them.
- **contradicted**: memories with a `contradicted` use event newer than
  both the last `updated` (body refresh via `memory_update`) and the
  last `last_verified_at` (explicit re-check via `memory_verify`). Either
  resolution path clears the flag, so a sticky entry can be cleared by
  re-running the appropriate one. The sibling `corrected` outcome (for
  noticed-and-fixed-inline) is audit-only — it increments
  `corrected_count` but never raises this flag.
- **marker_stats**: the transient-marker override rate, per marker. A
  high override rate is the signal to remove the marker from the list,
  not vibes. A near-zero rate with non-zero fires is a healthy marker.

The report model (the dataclasses, caps and shared predicates) lives in
`health_model`, the event-log accumulator, the recommendations and the
rollup helpers in `health_stats`, and the text and JSON renderers in
`health_render`. This module re-exports every public name and every
private name a caller outside the family reads, so callers import them
from `health` as before; a private helper nothing outside the family
reads lives in its child alone.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .models import Category, Memory, first_summary_line
from . import location
from .origin import (
    Origin,
    capture,
    commit_author_timestamps,
    repo_toplevel_and_head,
    repos_match,
)
from .verify import (
    Anchoring,
    commit_drift_anchor_paths,
)
from .time_utils import (
    ensure_utc,
    parse_event_ts,
)
from .health_model import (
    RECOMMENDATION_KINDS,
    ActorSlice as ActorSlice,
    ActorSlices as ActorSlices,
    ColdEndorsementMemories,
    CommitDriftDebt,
    CommitDriftRow,
    CrossRepoDrift as CrossRepoDrift,
    CrossRepoDriftGroup as CrossRepoDriftGroup,
    HealthReport,
    MarkerStats,
    MemoryStats,
    ProvenanceDebt as ProvenanceDebt,
    ProvenanceRow as ProvenanceRow,
    RecentSilentMiss,
    Recommendation,
    ScopeHealth,
    SilentMissStats,
    TelemetryCoverage,
    VerificationDebt,
    _COLD_ENDORSEMENT_CAP,
    _COLD_ENDORSEMENT_MIN_RETRIEVALS as _COLD_ENDORSEMENT_MIN_RETRIEVALS,
    _COMMIT_DRIFT_DEBT_CAP,
    _CROSS_REPO_MAX_ROOTS,
    _ENDORSEMENT_GRACE_DAYS,
    _HOOKLESS_REASON as _HOOKLESS_REASON,
    _RECENT_SILENT_MISSES_CAP as _RECENT_SILENT_MISSES_CAP,
    _RECOMMENDATION_ROW_CAP as _RECOMMENDATION_ROW_CAP,
    _VERIFICATION_DEBT_CAP,
    _freshest_touch_ts as _freshest_touch_ts,
    _is_dead_weight,
    _is_weakly_endorsed,
    applied_tier,
    endorsement_tier as endorsement_tier,
    is_hook_telemetry_event,
)
from .health_stats import (
    _AccumulatorRollups,
    _StatsAccumulator as _StatsAccumulator,
    _build_actor_slices,
    _build_recent_silent_misses,
    _compute_recommendations,
    _drift_rows_for_candidates,
    _edit_distance_within as _edit_distance_within,
    _rare_scopes,
    _silent_miss_stats,
    find_prior_session_boundary as find_prior_session_boundary,
    provenance_debt as provenance_debt,
)
from .health_render import render_json, render_text

if TYPE_CHECKING:
    from .admission import DriftScope


# `_event_id_list` lives in `events.py` so memory_health,
# memory_search's endorsement tally, and the negative-outcomes attach all
# share ONE normalizer for event id fields — the 3.15.0 audit found the
# health rollup hardened against poison id shapes while the search-path
# consumers of the very same events still iterated the raw field and took
# retrieval down. See `events._event_id_items` for the full rationale.


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


@dataclass
class _HealthCensus:
    """What ``compute_health``'s walk of the memories records, keyed by memory id."""

    by_id: dict[str, MemoryStats]
    origin_repo_by_id: dict[str, str | None]
    anchor_paths_by_id: dict[str, tuple[str, ...]]
    origin_worktree_by_id: dict[str, str | None]
    claims_by_id: dict[str, tuple[str, ...]]
    verified_head_by_id: dict[str, str | None]
    actor_memories: Counter[str | None]
    actor_memories_in_window: Counter[str | None]
    actor_models: defaultdict[str | None, Counter[str]]
    actor_principals: defaultdict[str | None, Counter[str]]


def _census_memories(memories: Iterable[Memory], cutoff: datetime) -> _HealthCensus:
    """Walk the memories once; returns their stats and the per-id maps the buckets read."""
    by_id: dict[str, MemoryStats] = {}
    # Parallel mappings of memory id -> origin.repo and id -> claim anchors,
    # kept separately so we don't have to add fields to MemoryStats just for
    # the commit-drift rollup. Captured during the same pass that builds
    # `by_id` because `memories` is an Iterable and we don't want to assume
    # re-iterability. `anchor_paths_by_id` (attested verified_paths plus
    # body-cited paths, `verify.commit_drift_anchor_paths`) is what lets the
    # rollup narrow drift to commits touching a memory's actual claims —
    # and exempt claim-less memories entirely — matching memory_show /
    # memory_search (see _compute_commit_drift_debt).
    origin_repo_by_id: dict[str, str | None] = {}
    anchor_paths_by_id: dict[str, tuple[str, ...]] = {}
    origin_worktree_by_id: dict[str, str | None] = {}
    claims_by_id: dict[str, tuple[str, ...]] = {}
    verified_head_by_id: dict[str, str | None] = {}
    actor_memories: Counter[str | None] = Counter()
    actor_memories_in_window: Counter[str | None] = Counter()
    actor_models: defaultdict[str | None, Counter[str]] = defaultdict(Counter)
    actor_principals: defaultdict[str | None, Counter[str]] = defaultdict(Counter)

    for m in memories:
        by_id[m.id] = MemoryStats(
            id=m.id,
            scopes=list(m.scopes),
            summary=first_summary_line(m.body),
            created=m.created,
            updated=m.updated,
            last_verified_at=m.last_verified_at,
            last_corroborated=m.last_corroborated,
            category=m.category,
        )
        # Per-actor census, keyed on the declared `client` with None
        # for "declared nothing on this axis". `m.actor` is absent on
        # every memory written before 7.10.0, and an actor that
        # declared only a model has a None client — both land in the
        # undeclared bucket, whose model/principal counters still
        # record what they DID declare.
        actor = m.actor
        actor_client = actor.client if actor is not None else None
        actor_memories[actor_client] += 1
        created_at = _ensure_utc(m.created)
        if created_at is not None and created_at >= cutoff:
            actor_memories_in_window[actor_client] += 1
        if actor is not None:
            if actor.model:
                actor_models[actor_client][actor.model] += 1
            if actor.principal:
                actor_principals[actor_client][actor.principal] += 1
        origin_repo_by_id[m.id] = m.origin.repo if m.origin else None
        origin_worktree_by_id[m.id] = m.origin.worktree_root if m.origin else None
        anchor_paths_by_id[m.id] = commit_drift_anchor_paths(m.body, m.verified_paths)
        claims_by_id[m.id] = tuple(m.claims)
        verified_head_by_id[m.id] = m.verified_head
    return _HealthCensus(
        by_id=by_id,
        origin_repo_by_id=origin_repo_by_id,
        anchor_paths_by_id=anchor_paths_by_id,
        origin_worktree_by_id=origin_worktree_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        actor_memories=actor_memories,
        actor_memories_in_window=actor_memories_in_window,
        actor_models=actor_models,
        actor_principals=actor_principals,
    )


def _marker_stats(rollups: _AccumulatorRollups) -> list[MarkerStats]:
    """Count each marker's fires and overrides from the rollups; returns them, busiest first."""
    marker_stats = [
        MarkerStats(
            marker=m,
            fire_count=rollups.marker_fires[m],
            override_count=rollups.marker_overrides[m],
        )
        for m in sorted(set(rollups.marker_fires) | set(rollups.marker_overrides))
    ]
    marker_stats.sort(key=lambda s: s.total, reverse=True)
    return marker_stats


def _dead_weight_bucket(
    by_id: dict[str, MemoryStats],
    rollups: _AccumulatorRollups,
    hook_telemetry_events: int | None,
    now: datetime,
    cutoff: datetime,
) -> tuple[list[MemoryStats], TelemetryCoverage | None, bool]:
    """Gate on the hook's telemetry and list the dead weight; returns it, the coverage and the gate."""
    # Dead weight: the memory IS being retrieved within the window but
    # nothing the model produced ever called `record_use(applied)`. That's
    # the actionable signal — the ranker is surfacing the memory but the
    # model isn't getting value from it. Either the body is misleading,
    # the scopes are wrong, or the content is duplicate-noise. Either way,
    # a curation pass should look.
    #
    # The rule is the shared `_is_dead_weight` predicate (see its
    # docstring for the full gate list).
    #
    # Memories with `retrieval_count == 0` move into `cold_memories`
    # below. Ambient-category memories are excluded from both buckets:
    # their value is implicit (they shape responses without being cited),
    # so the use signal is structurally absent and a count of zero
    # there is not an indictment.
    #
    # ...and none of that reasoning holds on a store where nothing was
    # ever in a position to record an apply. The honesty gate empties
    # the bucket rather than reporting an unwired Stop hook as rot;
    # `telemetry_coverage` below carries the explanation so the empty
    # bucket is never mistaken for a clean store.
    grace_cutoff = now - timedelta(days=_ENDORSEMENT_GRACE_DAYS)
    observed_hook_telemetry = rollups.hook_telemetry_events
    if hook_telemetry_events is None:
        # Caller didn't measure — assume covered, pre-3.32 behaviour.
        telemetry_coverage: TelemetryCoverage | None = None
        telemetry_covered = True
    else:
        total_hook_telemetry = hook_telemetry_events + observed_hook_telemetry
        telemetry_covered = total_hook_telemetry > 0
        telemetry_coverage = TelemetryCoverage(
            hook_telemetry_events=total_hook_telemetry,
            covered=telemetry_covered,
            dead_weight_suppressed=not telemetry_covered,
            cold_endorsement_suppressed=not telemetry_covered,
            reason=None if telemetry_covered else _HOOKLESS_REASON,
        )
    dead_weight: list[MemoryStats] = []
    # The whole loop is skipped when coverage is absent, not run and
    # filtered afterwards: there is no per-memory judgement to make,
    # the input SIGNAL is missing for every row at once.
    if telemetry_covered:
        for s in by_id.values():
            first_seen = rollups.earliest_retrieval_by_id.get(s.id)
            if _is_dead_weight(
                category=s.category,
                freshest_ts=_freshest_touch_ts(
                    s.created, s.updated, s.last_verified_at, s.last_corroborated
                ),
                retrieval_count=s.retrieval_count,
                applied_count=s.applied_count,
                has_unresolved_contradiction=s.has_unresolved_contradiction,
                earliest_retrieval_ts=(
                    first_seen.timestamp() if first_seen is not None else None
                ),
                cutoff_ts=cutoff.timestamp(),
                grace_cutoff_ts=grace_cutoff.timestamp(),
            ):
                dead_weight.append(s)
        dead_weight.sort(key=lambda s: s.created)
    return dead_weight, telemetry_coverage, telemetry_covered


def _usage_buckets(
    by_id: dict[str, MemoryStats],
    cutoff: datetime,
    heavily_used_min_applied: int,
    heavily_used_top_k: int,
    resolution_events_by_id: dict[str, list[dict[str, Any]]],
) -> tuple[list[MemoryStats], list[MemoryStats], list[MemoryStats]]:
    """Sort out the cold, the heavily used and the contradicted memories; returns the three lists."""
    # Cold memories: never retrieved at all in the window. Either nobody is
    # asking the kind of question this memory answers, or the ranker isn't
    # surfacing it. Distinct from dead weight — a cold memory hasn't had
    # the chance to be "applied" or "ignored", so a curation pass should
    # ask "is the trigger for this memory still real?", not "is the body
    # misleading?". Sorted oldest-first like dead_weight; same ambient
    # exclusion.
    cold_memories = [
        s
        for s in by_id.values()
        if s.category != Category.AMBIENT
        and s.created < cutoff
        and s.retrieval_count == 0
    ]
    cold_memories.sort(key=lambda s: s.created)

    heavily_used = sorted(
        (s for s in by_id.values() if s.applied_count >= heavily_used_min_applied),
        key=lambda s: (s.applied_count, s.last_used_at or s.updated),
        reverse=True,
    )[:heavily_used_top_k]

    contradicted = [s for s in by_id.values() if s.has_unresolved_contradiction]
    contradicted.sort(key=lambda s: s.last_contradicted_at or s.updated, reverse=True)
    # Attach the resolution timeline to each contradicted row. Cheap because
    # the bucket is typically empty or small. We slice the per-id event list
    # rather than re-iterating the events stream — the accumulator was built
    # in the same pass that produced the counters above.
    for stats in contradicted:
        stats.resolution_timeline = list(resolution_events_by_id.get(stats.id, []))
    return cold_memories, heavily_used, contradicted


def _scope_buckets(
    by_id: dict[str, MemoryStats],
    dead_weight: list[MemoryStats],
    cold_memories: list[MemoryStats],
    contradicted: list[MemoryStats],
    scope_distribution: Counter[str],
    typo_scope_exceptions: Iterable[str] | None,
) -> tuple[list[ScopeHealth], list[str]]:
    """Roll the buckets up per scope and find the typo-like singletons; returns both."""
    # Per-scope rollup. A memory tagged with N scopes is counted once per
    # scope — `sum(scope.active for scope in scope_health)` will exceed
    # `total_active_memories` when scopes overlap, which is the right shape
    # for "where is the rot concentrated?". We sort by total count
    # descending so the heaviest-trafficked scopes lead.
    scope_health_map: dict[str, ScopeHealth] = {}
    dead_ids = {s.id for s in dead_weight}
    cold_ids = {s.id for s in cold_memories}
    contradicted_ids = {s.id for s in contradicted}
    for stats in by_id.values():
        for scope in stats.scopes:
            entry = scope_health_map.setdefault(scope, ScopeHealth(scope=scope))
            entry.active += 1
            entry.applied_total += stats.applied_count
            if stats.id in dead_ids:
                entry.dead += 1
            if stats.id in cold_ids:
                entry.cold += 1
            if stats.id in contradicted_ids:
                entry.contradicted += 1
    scope_health = sorted(
        scope_health_map.values(),
        key=lambda s: (-s.active, s.scope),
    )

    # Rare scopes — singletons that look like typos of another scope.
    # The heuristic used to flag every singleton, but most singletons
    # in practice are legitimate narrow tags ("career", "personal-context")
    # rather than misspells, and flagging them produced enough false
    # positives that the bucket stopped being actionable. The neighbor
    # check (`_scope_typo_neighbor`, against any other scope, including
    # other singletons) restricts the bucket to scopes that almost
    # certainly *are* typos: "projct:foo" against an existing
    # "projects:foo", "tool" against "tools", "bug"/"bugs" pairs, and
    # namespace omission/truncation ("bettermemory" or
    # "proj:bettermemory" against "projects:bettermemory" — exact name
    # part, wrong namespace). A raw whole-string Levenshtein threshold
    # was both too loose and too tight here, so the helper compares
    # namespace-stripped tails with a length-scaled threshold and
    # exempts deliberate sibling/successor scopes (aoc2023/aoc2024,
    # blog-v2/blog-v3, foo/foo2) — see its docstring for the rules.
    # `typo_scope_exceptions` is the off switch a confirmed false
    # positive needs. The neighbour heuristic cannot distinguish "typo of
    # a sibling scope" from "two projects that legitimately share a name
    # stem", and without suppression the recommendation re-fires forever:
    # every curation pass re-adjudicates it, and the failure is
    # asymmetric, because acting on it wrongly fuses two scopes and
    # acting on it rightly saves a tag. Its siblings already gate this
    # way (`dead_weight_suppressed`, `cold_endorsement_suppressed`).
    # One pass over the scopes (`_rare_scopes`), not one per singleton:
    # the pairwise form took 42 s on a store of 2,000 scopes.
    rare_scopes = _rare_scopes(
        scope_distribution, exempt=set(typo_scope_exceptions or ())
    )
    return scope_health, rare_scopes


def _verification_debt(
    by_id: dict[str, MemoryStats],
    claims_by_id: dict[str, tuple[str, ...]],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    verification_cutoff: datetime,
    verification_stale_days: int,
) -> VerificationDebt:
    """Partition the memories into never verified, stale and fresh; returns the debt."""

    # Verification debt — partition active memories into never_verified /
    # stale / fresh against the staleness threshold. Sort each bucket
    # checkable-first (see the predicate below and the field comment on
    # `VerificationDebt`), then by the timestamp that's most actionable
    # for a curation pass: never_verified by `created` (oldest
    # unverified first — those are the highest-risk because they've had
    # the most time to drift), and stale by `last_verified_at` (oldest
    # verification first — same rationale, applied to memories that
    # have at least been spot-checked once). The capped
    # `_VERIFICATION_DEBT_CAP` rows are inlined for display; the totals
    # are always uncapped so a downstream reader can tell "5 stale"
    # from "500 stale" without re-counting, and the `*_checkable`
    # splits say how much of each total a verify pass can mechanically
    # drain. The predicate reuses the maps the drift rollup already
    # built — declared claims, or drift anchors (body-cited paths plus
    # attested verified_paths) — so the partition costs no extraction.
    def _is_checkable(mid: str) -> bool:
        return bool(claims_by_id.get(mid)) or bool(anchor_paths_by_id.get(mid))

    never_verified_all: list[MemoryStats] = []
    stale_all: list[MemoryStats] = []
    fresh_count = 0
    for stats in by_id.values():
        if stats.last_verified_at is None:
            never_verified_all.append(stats)
        elif stats.last_verified_at < verification_cutoff:
            stale_all.append(stats)
        else:
            fresh_count += 1
    never_verified_all.sort(key=lambda s: (not _is_checkable(s.id), s.created))
    stale_all.sort(
        key=lambda s: (not _is_checkable(s.id), s.last_verified_at or s.created)
    )

    verification_debt = VerificationDebt(
        stale_after_days=verification_stale_days,
        never_verified=never_verified_all[:_VERIFICATION_DEBT_CAP],
        never_verified_total=len(never_verified_all),
        stale=stale_all[:_VERIFICATION_DEBT_CAP],
        stale_total=len(stale_all),
        fresh_count=fresh_count,
        never_verified_checkable=sum(
            1 for s in never_verified_all if _is_checkable(s.id)
        ),
        stale_checkable=sum(1 for s in stale_all if _is_checkable(s.id)),
    )
    return verification_debt


def _drift_rollups(
    by_id: dict[str, MemoryStats],
    *,
    origin_repo_by_id: dict[str, str | None],
    origin_worktree_by_id: dict[str, str | None],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]],
    verified_head_by_id: dict[str, str | None],
    caller_origin: Origin | None,
    anchoring: Anchoring | None,
    admission: DriftScope | None,
) -> tuple[CommitDriftDebt | None, CrossRepoDrift | None]:
    """Take the caller's-repo and the cross-repo drift rollups; returns both."""
    if anchoring is not None:
        anchoring.prime(list(by_id))
    commit_drift_debt = _compute_commit_drift_debt(
        by_id=by_id,
        origin_repo_by_id=origin_repo_by_id,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        caller_origin=caller_origin,
        admission=admission,
    )

    cross_repo_drift = _compute_cross_repo_drift(
        by_id=by_id,
        origin_repo_by_id=origin_repo_by_id,
        origin_worktree_by_id=origin_worktree_by_id,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        caller_origin=caller_origin,
        anchoring=anchoring,
    )
    return commit_drift_debt, cross_repo_drift


def _cold_endorsement_bucket(
    by_id: dict[str, MemoryStats],
    telemetry_covered: bool,
    cold_endorsement_min_retrievals: int,
    cold_endorsement_ratio_threshold: float,
) -> ColdEndorsementMemories:
    """List the memories surfaced past the floor and never endorsed; returns the bucket."""
    # Cold-endorsement memories — distinct memories the ranker keeps
    # surfacing (retrieval crossed the floor) that the model has never
    # explicitly endorsed (zero `explicit_applied_count`). Ambient
    # excluded by construction — their value is implicit and they're
    # structurally unlikely to carry explicit use events. Sort by
    # retrieval_count desc (most over-surfaced first), then by
    # last_used_at desc so the rows surface "actively over-surfaced"
    # before "historically over-surfaced." The bucket is uncapped in
    # `total`; rows are capped at `_COLD_ENDORSEMENT_CAP` for inline
    # display.
    endorsement_floor = max(1, int(cold_endorsement_min_retrievals))
    # Build the predicate once. `explicit_applied_count == 0` is the
    # binary "never deliberately endorsed" signal (the original
    # bucket semantic). When `cold_endorsement_ratio_threshold > 0`,
    # also flag memories whose explicit-to-total-applied ratio is
    # below the threshold — catches the "1 explicit out of 50 auto"
    # case the binary check misses. Default 0.0 preserves the
    # pre-existing behaviour exactly (the ratio branch never fires).
    ratio_threshold = max(0.0, float(cold_endorsement_ratio_threshold))
    # Rides the same honesty gate as dead_weight, and skipped the same
    # way — not filtered afterwards: `explicit_applied_count` is
    # produced by the Stop hook's containment matcher and deliberate
    # model calls, so on a store with no settlement telemetry the zero
    # is uninformative for every row at once. The empty bucket is
    # explained by `telemetry_coverage.cold_endorsement_suppressed`.
    endorsement_candidates = (
        [
            s
            for s in by_id.values()
            if s.category != Category.AMBIENT
            and s.retrieval_count >= endorsement_floor
            and _is_weakly_endorsed(s, ratio_threshold)
        ]
        if telemetry_covered
        else []
    )
    endorsement_candidates.sort(
        key=lambda s: (
            s.retrieval_count,
            s.last_used_at or s.updated,
        ),
        reverse=True,
    )
    cold_endorsement_memories = ColdEndorsementMemories(
        min_retrievals=endorsement_floor,
        rows=endorsement_candidates[:_COLD_ENDORSEMENT_CAP],
        total=len(endorsement_candidates),
    )
    return cold_endorsement_memories


def _health_report(
    census: _HealthCensus,
    rollups: _AccumulatorRollups,
    *,
    now: datetime,
    cutoff: datetime,
    verification_cutoff: datetime,
    window_days: int,
    heavily_used_top_k: int,
    heavily_used_min_applied: int,
    verification_stale_days: int,
    cold_endorsement_min_retrievals: int,
    cold_endorsement_ratio_threshold: float,
    caller_origin: Origin | None,
    tombstoned_ids: set[str],
    hook_telemetry_events: int | None,
    typo_scope_exceptions: Iterable[str] | None,
    anchoring: Anchoring | None,
    admission: DriftScope | None,
) -> HealthReport:
    """Build every bucket of the report from the census and the event rollups; returns the report."""
    by_id = census.by_id
    marker_stats = _marker_stats(rollups)

    scope_distribution = Counter(
        scope for stats in by_id.values() for scope in stats.scopes
    )

    # Per-actor pivot. Built from the memory walk above and the event
    # rollups together, in Python, from the arguments this function was
    # handed — deliberately NOT off the index's v11 actor columns.
    # `compute_health` is pure over memories + events and never sees an
    # index (`provenance` is the one index-backed field and it is
    # populated a layer up, in `report_for_store`); the events half
    # has no index at all; and an index can legitimately sit a row
    # behind disk between a write and a rebuild, which would put this
    # report's own reconciliation invariants in disagreement with
    # themselves. The v11 columns serve `memory_search`'s SQL
    # prefilter, which is what they were added for.
    actor_slices = _build_actor_slices(
        memories=census.actor_memories,
        memories_in_window=census.actor_memories_in_window,
        models=census.actor_models,
        principals=census.actor_principals,
        events=rollups.actor_events,
        searches=rollups.actor_searches,
        applies=rollups.actor_applies,
    )

    dead_weight, telemetry_coverage, telemetry_covered = _dead_weight_bucket(
        by_id, rollups, hook_telemetry_events, now, cutoff
    )

    cold_memories, heavily_used, contradicted = _usage_buckets(
        by_id,
        cutoff,
        heavily_used_min_applied,
        heavily_used_top_k,
        rollups.resolution_events_by_id,
    )

    scope_health, rare_scopes = _scope_buckets(
        by_id,
        dead_weight,
        cold_memories,
        contradicted,
        scope_distribution,
        typo_scope_exceptions,
    )

    verification_debt = _verification_debt(
        by_id,
        census.claims_by_id,
        census.anchor_paths_by_id,
        verification_cutoff,
        verification_stale_days,
    )

    commit_drift_debt, cross_repo_drift = _drift_rollups(
        by_id,
        origin_repo_by_id=census.origin_repo_by_id,
        origin_worktree_by_id=census.origin_worktree_by_id,
        anchor_paths_by_id=census.anchor_paths_by_id,
        claims_by_id=census.claims_by_id,
        verified_head_by_id=census.verified_head_by_id,
        caller_origin=caller_origin,
        anchoring=anchoring,
        admission=admission,
    )

    cold_endorsement_memories = _cold_endorsement_bucket(
        by_id,
        telemetry_covered,
        cold_endorsement_min_retrievals,
        cold_endorsement_ratio_threshold,
    )

    report = HealthReport(
        generated_at=now,
        window_days=window_days,
        total_active_memories=len(by_id),
        total_events=rollups.total_events,
        distinct_sessions=len(rollups.sessions),
        dead_weight=dead_weight,
        cold_memories=cold_memories,
        heavily_used=heavily_used,
        contradicted=contradicted,
        marker_stats=marker_stats,
        scope_distribution=dict(scope_distribution),
        scope_health=scope_health,
        rare_scopes=rare_scopes,
        actor_slices=actor_slices,
        orphan_use_events=rollups.orphan_use_events,
        verification_debt=verification_debt,
        commit_drift_debt=commit_drift_debt,
        cross_repo_drift=cross_repo_drift,
        silent_misses=_silent_miss_stats(
            audited=rollups.silent_miss_audited,
            miss_events=rollups.silent_miss_events,
            cutoff=rollups.latest_miss_cutoff,
            tombstoned_ids=tombstoned_ids,
            acknowledged_event_ids=rollups.acknowledged_miss_event_ids,
        ),
        recent_silent_misses=_build_recent_silent_misses(
            rollups.silent_miss_events,
            cutoff=rollups.latest_miss_cutoff,
            tombstoned_ids=tombstoned_ids,
            acknowledged_event_ids=rollups.acknowledged_miss_event_ids,
        ),
        cold_endorsement_memories=cold_endorsement_memories,
        telemetry_coverage=telemetry_coverage,
    )
    return report


def compute_health(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    heavily_used_min_applied: int = 3,
    verification_stale_days: int = 30,
    cold_endorsement_min_retrievals: int = _COLD_ENDORSEMENT_MIN_RETRIEVALS,
    cold_endorsement_ratio_threshold: float = 0.0,
    caller_origin: Origin | None = None,
    now: datetime | None = None,
    tombstoned_ids: set[str] | None = None,
    hook_telemetry_events: int | None = None,
    typo_scope_exceptions: Iterable[str] | None = None,
    anchoring: Anchoring | None = None,
    admission: DriftScope | None = None,
) -> HealthReport:
    """Build a `HealthReport` from active memories + the event stream.

    `admission` decides whose commit drift `commit_drift_debt` counts, as
    it decides it for `memory_show` and `memory_search`
    (`DriftScope.drift_counts`: a home the caller's LINKS admission
    admits, and not global), and the count is taken in the caller's
    checkout as `memory_show` takes it; without it every record whose
    origin repo matches the caller's is a candidate. `anchoring` reads
    `cross_repo_drift`'s homed groups where design 3.6 resolves their
    cited commit (`verify.ClaimAnchor.commit_root`); a record with no home
    row, and every record without it, is read as before.
    `memory_admin(health)` passes both, resolved for the request's
    caller; `bettermemory health` passes neither, since resolving the
    caller registers the checkout and the CLI report writes nothing.

    `events` is expected to be in chronological order — the function
    relies on that for "last_*" timestamps. `iter_all_events` returns
    archives + active log in chronological order; production callers
    should pass that directly. That ordering is a `heapq.merge` on
    event `ts` across the per-shard archive chains and the active
    segments, NOT "all archives, then the active log" — since 3.24.0
    sharded the active log a quiet shard's active segment can hold
    events older than a busy shard's freshly-cut archive, so the
    concatenated form this docstring used to describe would have fed
    `last_*` timestamps out of order.

    `window_days` controls the dead-weight cutoff: a memory is dead-weight
    when its latest maintenance touch (created / updated / last-verified)
    is more than `window_days` old, it has been retrieved but never
    `applied`, carries no unresolved contradiction, and its earliest
    retrieval has aged past the endorsement grace — the shared
    `_is_dead_weight` predicate. The window keeps recently-written (or
    recently-maintained) memories from being flagged before they've had
    a chance to accumulate use signal.

    `heavily_used_min_applied` is the floor on `applied_count` for inclusion
    in `heavily_used`. Default 3: a single acknowledgement is acknowledgement,
    not a usage pattern, and the bucket is meant to surface memories that are
    actively load-bearing. Lower to 1 on a fresh store if you want to see
    everything that's been touched at least once. Always >= 1 — a value of
    0 would dump every memory into the bucket and defeat the report.

    `verification_stale_days` controls the staleness threshold for the
    `verification_debt` bucket: a memory whose `last_verified_at` is older
    than this many days lands in the `stale` list; never-verified memories
    land in `never_verified` regardless of age. Should match the
    `behavior.verification_stale_days` config value the rest of the system
    uses for the per-row `verification.status` field, so a "stale" hit
    in search results and a "stale" entry in this bucket mean the same
    thing.

    `caller_origin`, when provided, drives the optional `commit_drift_debt`
    rollup: memories whose origin repo matches `caller_origin.repo` and
    whose `last_verified_at` precedes commits in the current HEAD are
    surfaced as drifted. The rollup is bounded to one git invocation
    (`commit_author_timestamps` + bisect) regardless of memory count,
    so calling it on a large store is cheap. Pass None (the default) to
    skip the rollup — production callers from the MCP tool / CLI thread
    in `capture()`'s output; tests and offline tooling can opt out.

    `hook_telemetry_events` arms the dead-weight honesty gate. Semantics:

    - `None` (default) — "the caller did not measure; assume covered".
      The gate is OFF and the bucket behaves exactly as it did before
      3.32. This is the right default for offline tooling and unit
      fixtures, which construct synthetic event lists that contain no
      Stop-hook rows and are nevertheless asserting on dead weight.
    - an int — the count of `is_hook_telemetry_event` rows the caller
      already observed over the SAME event stream. The gate is ON, and
      coverage is that count OR-ed with what this walk sees for itself,
      so a caller that cannot cheaply pre-measure (an `iter_all_events`
      generator it is handing straight in) passes `0` and delegates the
      measurement here. With coverage at zero the `dead_weight` bucket
      is emptied and `telemetry_coverage.dead_weight_suppressed` says
      why — see `is_hook_telemetry_event` for the full rationale.

    Deliberately NOT an MCP tool parameter: the gate belongs to every
    caller of this function equally, and a wire parameter would spend
    schema budget to let a client turn off an honesty check.
    """
    if heavily_used_min_applied < 1:
        heavily_used_min_applied = 1
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    verification_cutoff = now - timedelta(days=verification_stale_days)
    tombstoned_ids = tombstoned_ids or set()

    census = _census_memories(memories, cutoff)

    accumulator = _StatsAccumulator(by_id=census.by_id, tombstoned_ids=tombstoned_ids)
    for ev in events:
        accumulator.handle_event(ev)
    rollups = accumulator.rollups()

    report = _health_report(
        census,
        rollups,
        now=now,
        cutoff=cutoff,
        verification_cutoff=verification_cutoff,
        window_days=window_days,
        heavily_used_top_k=heavily_used_top_k,
        heavily_used_min_applied=heavily_used_min_applied,
        verification_stale_days=verification_stale_days,
        cold_endorsement_min_retrievals=cold_endorsement_min_retrievals,
        cold_endorsement_ratio_threshold=cold_endorsement_ratio_threshold,
        caller_origin=caller_origin,
        tombstoned_ids=tombstoned_ids,
        hook_telemetry_events=hook_telemetry_events,
        typo_scope_exceptions=typo_scope_exceptions,
        anchoring=anchoring,
        admission=admission,
    )
    # Recommendations are distilled from the buckets, so the gate above
    # reaches them for free: an emptied `dead_weight` cannot cross the
    # size floor, and `memory_health` therefore never tells the model to
    # go remove rot it invented from a missing hook.
    report.recommendations = _compute_recommendations(report)
    return report


def _group_cross_repo_candidates(
    by_id: dict[str, MemoryStats],
    *,
    origin_repo_by_id: dict[str, str | None],
    origin_worktree_by_id: dict[str, str | None],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    verified_head_by_id: dict[str, str | None] | None,
    caller_origin: Origin | None,
    anchoring: Anchoring | None,
) -> tuple[
    dict[tuple[str, str], list[MemoryStats]],
    dict[tuple[str, str], list[MemoryStats]],
    dict[tuple[str, str], str],
    int,
]:
    """Group the foreign candidates; returns both group maps, the unconfirmed and the unanchorable count."""
    groups: dict[tuple[str, str], list[MemoryStats]] = {}
    # Homed groups, keyed by (the repo or project they are listed under,
    # the live view they are read at); the membership model has already
    # read that view as the checkout itself.
    homed: dict[tuple[str, str], list[MemoryStats]] = {}
    unconfirmed: dict[tuple[str, str], str] = {}
    unanchorable = 0
    for stats in by_id.values():
        origin_repo = origin_repo_by_id.get(stats.id)
        worktree = origin_worktree_by_id.get(stats.id)
        anchor = anchoring.for_record(stats.id) if anchoring is not None else None
        if anchor is not None:
            if (
                origin_repo
                and caller_origin is not None
                and caller_origin.repo
                and repos_match(origin_repo, caller_origin.repo)
            ):
                continue
            if stats.last_verified_at is None:
                continue
            if not anchor_paths_by_id.get(stats.id) and not (
                claims_by_id and claims_by_id.get(stats.id)
            ):
                continue
            home = anchoring.home(stats.id) if anchoring is not None else None
            label = origin_repo or (home.project if home is not None else stats.id)
            cited = verified_head_by_id.get(stats.id) if verified_head_by_id else None
            commit_root, _rule = anchor.commit_root(cited)
            if commit_root is None:
                unconfirmed.setdefault((label, worktree or ""), anchor.rule)
                continue
            homed.setdefault((label, commit_root), []).append(stats)
            continue
        if not origin_repo or not worktree:
            # COUNTED, not dropped. With no recorded repo or worktree
            # there is nothing to resolve on disk and no anchor to count
            # commits against, so this check genuinely cannot judge the
            # record — which is exactly why its absence has to be
            # reported rather than absorbed. Gated on `last_verified_at`
            # so the figure counts records that could otherwise have been
            # judged, matching the candidate rule the groups below use;
            # an unverified record is out of scope for every drift leg.
            if stats.last_verified_at is not None:
                unanchorable += 1
            continue
        if (
            caller_origin is not None
            and caller_origin.repo
            and repos_match(origin_repo, caller_origin.repo)
        ):
            # The caller-repo rollup owns these.
            continue
        if stats.last_verified_at is None:
            continue
        if not anchor_paths_by_id.get(stats.id) and not (
            claims_by_id and claims_by_id.get(stats.id)
        ):
            continue
        groups.setdefault((origin_repo, worktree), []).append(stats)
    return groups, homed, unconfirmed, unanchorable


def _recorded_root_reachable(
    repo: str, worktree: str, root: Path, skipped: list[dict[str, str]]
) -> bool:
    """Stat a recorded worktree before git runs there, adding to ``skipped`` when it cannot; returns whether it can."""
    # Liveness in THREE states, not two, because the two-state read
    # asserted things it could not know — and crashed on one of them.
    #
    # `Path.exists()` was the original probe and it is the wrong
    # question twice over. It RE-RAISES every OSError outside its
    # own ignore set (ENOENT/ENOTDIR/EBADF/ELOOP), so a foreign
    # checkout behind a permission-denied parent, on a dropped
    # network mount, or on failing media propagated out of here,
    # out of `compute_health`, and took `memory_health` and
    # `bettermemory health` down with it — one unreadable directory
    # anywhere in the estate blanked the entire health surface. And
    # where it does answer `False` it folds "nothing is there"
    # together with a slice of "I could not find out" (Windows
    # ERROR_NOT_READY: a removable or disconnected volume), which then
    # got reported as the positive finding "worktree missing on disk".
    #
    # `location.view_state` is the package's settled answer to
    # exactly that question, the one the membership model reads a
    # view's liveness by (design 3.6): an errno taxonomy whose
    # unclassified default is "unreachable", so a new error class
    # holds the boundary instead of opening it. `verify` and
    # `symbols` already route their root probes through their own
    # guards for the same reason. Only a record with no home row
    # still reaches this recorded-root path.
    #
    # So: positively absent is a finding, unreadable is a separate
    # finding, and only a directory we could actually stat is
    # handed to git — asking git to work inside a directory we
    # could not stat is how the mislabel below happened.
    if location.view_state(worktree, location.PRIMARY, None, None) == "gone":
        skipped.append(
            {
                "repo": repo,
                "worktree_root": worktree,
                "reason": "worktree missing on disk",
            }
        )
        return False
    if not os.path.isdir(root):
        # Not positively gone, and still not stattable as a
        # directory: unreachable rather than absent. `os.path.isdir`
        # swallows the OSError that `Path.is_dir()` would re-raise
        # — the same choice, for the same reason, that
        # `verify._worktree_root_is_live` writes down.
        skipped.append(
            {
                "repo": repo,
                "worktree_root": worktree,
                "reason": "worktree unreadable",
            }
        )
        return False
    return True


def _recorded_root_group(
    repo: str,
    worktree: str,
    root: Path,
    candidates: list[MemoryStats],
    skipped: list[dict[str, str]],
    *,
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    verified_head_by_id: dict[str, str | None] | None,
) -> CrossRepoDriftGroup | None:
    """Judge one recorded worktree's candidates, adding to ``skipped`` when it is skipped; returns its group, or None when skipped."""
    live = capture(root)
    if live.repo is None or not repos_match(repo, live.repo):
        # `repo is None` is THREE outcomes wearing one value, and
        # only one of them is a finding about the directory. The
        # stat split above fixed the unreadable arm and it would be
        # easy to stop there and claim the rest — but `origin._git`
        # folds a missing binary, a timeout at its 1.0s ceiling and
        # a non-zero exit into the same `None`, and `capture` gates
        # `repo` on `worktree_root`, so "git never answered" arrives
        # here indistinguishable from "git answered: not a
        # checkout". A server spawned from a GUI with a minimal PATH
        # has no git at all, and would otherwise have every foreign
        # group in the estate libeled at once.
        #
        # `worktree_root` is the discriminator, because `capture`
        # sets it from the FIRST probe and only when that probe
        # succeeded. So:
        #   root is None                -> nothing identified a
        #                                  checkout here (git absent,
        #                                  git timed out, or the
        #                                  directory is not a repo)
        #   root set, repo is None      -> a checkout, but it names
        #                                  no remote we could read
        #   root set, repo mismatched   -> the finding, now earned
        if live.worktree_root is None:
            reason = "no checkout identified in the recorded worktree"
        elif live.repo is None:
            reason = "checkout found, but its repo could not be identified"
        else:
            reason = "directory is no longer a checkout of the recorded repo"
        skipped.append(
            {
                "repo": repo,
                "worktree_root": worktree,
                "reason": reason,
            }
        )
        return None
    timestamps = commit_author_timestamps(root)
    if timestamps is None:
        skipped.append(
            {
                "repo": repo,
                "worktree_root": worktree,
                "reason": "git unreachable in the recorded worktree",
            }
        )
        return None
    located = repo_toplevel_and_head(root)
    rows = _drift_rows_for_candidates(
        candidates,
        root=root,
        timestamps=timestamps,
        toplevel=located[0] if located is not None else None,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        head=located[1] if located is not None else None,
    )
    return CrossRepoDriftGroup(
        repo=repo,
        worktree_root=worktree,
        rows=rows[:_COMMIT_DRIFT_DEBT_CAP],
        total_drifted=len(rows),
        candidates=len(candidates),
    )


def _walk_cross_repo_roots(
    *,
    groups: dict[tuple[str, str], list[MemoryStats]],
    homed: dict[tuple[str, str], list[MemoryStats]],
    unconfirmed: dict[tuple[str, str], str],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    verified_head_by_id: dict[str, str | None] | None,
) -> tuple[list[CrossRepoDriftGroup], list[dict[str, str]]]:
    """Walk the grouped roots, largest first, within the per-run cap; returns the groups judged and the roots skipped, with why."""
    ordered = sorted(
        [(key, members, False) for key, members in groups.items()]
        + [(key, members, True) for key, members in homed.items()],
        key=lambda entry: len(entry[1]),
        reverse=True,
    )
    out_groups: list[CrossRepoDriftGroup] = []
    skipped: list[dict[str, str]] = [
        {
            "repo": repo,
            "worktree_root": worktree,
            "reason": f"unconfirmed: no live checkout holds its cited commit ({rule})",
        }
        for (repo, worktree), rule in unconfirmed.items()
    ]
    walked = 0
    for (repo, worktree), candidates, is_live_view in ordered:
        if walked >= _CROSS_REPO_MAX_ROOTS:
            skipped.append(
                {
                    "repo": repo,
                    "worktree_root": worktree,
                    "reason": "past the per-run root cap",
                }
            )
            continue
        root = Path(worktree)
        if is_live_view:
            walked += 1
            group = _cross_repo_group(
                repo,
                worktree,
                candidates,
                anchor_paths_by_id=anchor_paths_by_id,
                claims_by_id=claims_by_id,
                verified_head_by_id=verified_head_by_id,
            )
            if group is None:
                skipped.append(
                    {
                        "repo": repo,
                        "worktree_root": worktree,
                        "reason": "git unreachable in the home checkout",
                    }
                )
            else:
                out_groups.append(group)
            continue
        if not _recorded_root_reachable(repo, worktree, root, skipped):
            continue
        walked += 1
        group = _recorded_root_group(
            repo,
            worktree,
            root,
            candidates,
            skipped,
            anchor_paths_by_id=anchor_paths_by_id,
            claims_by_id=claims_by_id,
            verified_head_by_id=verified_head_by_id,
        )
        if group is not None:
            out_groups.append(group)
    return out_groups, skipped


def _compute_cross_repo_drift(
    *,
    by_id: dict[str, MemoryStats],
    origin_repo_by_id: dict[str, str | None],
    origin_worktree_by_id: dict[str, str | None],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    verified_head_by_id: dict[str, str | None] | None = None,
    caller_origin: Origin | None,
    anchoring: Anchoring | None = None,
) -> CrossRepoDrift | None:
    """The estate check: drift for memories anchored in repos the
    caller is NOT standing in.

    The blind spot this closes (2026-08-30): every commit-drift
    surface — show, search, `commit_drift_debt` —
    is gated on the caller being inside the memory's origin repo, so
    records anchored in other projects rot invisibly for as long as
    health runs from here; the motivating find was a foreign record
    pinning a HEAD twenty commits gone, on a branch that no longer
    existed, reading fresh for days. Candidates are grouped by
    (recorded repo, recorded `origin.worktree_root`); each group's
    directory is resolved on disk — read-only, never a checkout or
    fetch — re-identified as a checkout of the recorded repo
    (`origin.capture` + `repos_match`, so a moved or reused directory
    is skipped with its reason rather than misread), and judged by the
    same claim-anchored legs through `_drift_rows_for_candidates`.

    Bounds and silences: `_CROSS_REPO_MAX_ROOTS` caps the roots git runs
    in per run: a root takes a place when git first runs there, whether
    or not it re-identifies as a checkout of the recorded repo, since
    `capture` alone runs several git processes (ENG-169 G6). The skips
    decided on a stat alone (a worktree gone or unreadable) take none;
    over-cap groups are listed as skipped, never dropped. A verified
    record whose origin names no repo or no worktree_root cannot be
    grouped at all — there is nothing to resolve and no anchor to count
    against — so it is COUNTED as `unanchorable` rather than dropped. It
    used to fall out of a bare `continue` above the `skipped` list,
    which is how a partial estate came to read as a complete one: on the
    maintainer's own store that silence covered 107 verified records,
    22% of it. `None` only when there is nothing to report on either
    axis — no foreign claim-anchored verified candidate AND nothing
    unanchorable — the same philosophy as `commit_drift_debt`'s None. A
    walked clean group IS emitted, with zero rows: "checked N, clean" and
    "didn't check" must not read the same, and neither must "checked
    nothing".

    With `anchoring`, a homed record is grouped where design 3.6 resolves
    its cited commit (`verify.ClaimAnchor.commit_root`): its home
    checkout's live view, which the membership model recognised by its
    object, so no re-identification runs; else the caller's checkout when
    it holds the commit. With neither, the record is listed in `skipped`
    with the anchor's rule as the reason, unconfirmed, never counted.
    """
    groups, homed, unconfirmed, unanchorable = _group_cross_repo_candidates(
        by_id,
        origin_repo_by_id=origin_repo_by_id,
        origin_worktree_by_id=origin_worktree_by_id,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        caller_origin=caller_origin,
        anchoring=anchoring,
    )
    if not groups and not homed and not unconfirmed:
        # `unanchorable` alone still earns a payload. Returning None here
        # would say "no foreign records to check", when what happened is
        # "N foreign-or-unknown records could not be checked" — the same
        # silence this function was built to remove, one level up.
        if unanchorable:
            return CrossRepoDrift(unanchorable=unanchorable)
        return None

    out_groups, skipped = _walk_cross_repo_roots(
        groups=groups,
        homed=homed,
        unconfirmed=unconfirmed,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
    )
    if not out_groups and not skipped:
        return None
    return CrossRepoDrift(
        groups=out_groups,
        skipped=skipped,
        total_drifted=sum(g.total_drifted for g in out_groups),
        unanchorable=unanchorable,
    )


def _cross_repo_group(
    repo: str,
    worktree: str,
    candidates: list[MemoryStats],
    *,
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    verified_head_by_id: dict[str, str | None] | None,
) -> CrossRepoDriftGroup | None:
    """One group of the estate check judged at `worktree`, a live view the
    membership model read as the checkout itself (design 3.6), by the same
    claim-anchored legs `_compute_cross_repo_drift` runs. None when git
    cannot answer there."""
    root = Path(worktree)
    timestamps = commit_author_timestamps(root)
    if timestamps is None:
        return None
    located = repo_toplevel_and_head(root)
    rows = _drift_rows_for_candidates(
        candidates,
        root=root,
        timestamps=timestamps,
        toplevel=located[0] if located is not None else None,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        head=located[1] if located is not None else None,
    )
    return CrossRepoDriftGroup(
        repo=repo,
        worktree_root=worktree,
        rows=rows[:_COMMIT_DRIFT_DEBT_CAP],
        total_drifted=len(rows),
        candidates=len(candidates),
    )


def _compute_commit_drift_debt(
    *,
    by_id: dict[str, MemoryStats],
    origin_repo_by_id: dict[str, str | None],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None = None,
    verified_head_by_id: dict[str, str | None] | None = None,
    caller_origin: Origin | None,
    admission: DriftScope | None = None,
) -> CommitDriftDebt | None:
    """Build the optional commit-drift rollup, or None when not applicable.

    Emits None — rather than an empty `CommitDriftDebt` — when:
    - `caller_origin` was not provided,
    - the caller isn't currently inside a repo,
    - git was unreachable (`commit_author_timestamps` returned None),
    - or no memory's origin matches the caller's current repo.

    The "no matches" case is silenced because surfacing an empty bucket
    with a populated `current_repo` would be misleading for the model:
    "this report is anchored to repo X, which has no anchored memories"
    is technically true but invites the consumer to read meaning into a
    structurally empty result. The other rollups (`dead_weight`,
    `verification_debt`) always emit because their semantics are
    well-defined for an empty store; commit drift only has meaning when
    there's something to be drifted *from*.

    Counted via one `git log --format=%aI` call + bisect — independent
    of memory count.

    Every candidate is counted in the caller's checkout, where
    `memory_show` counts a record's commit leg (`_response._DriftPass.
    lookup`). `_drift_rows_by_root` can count a homed one at its home
    checkout's `commit_root` (design 3.6), but `memory_show` does not, so
    from a linked worktree the two would disagree both ways: a commit
    made in the home checkout only would be listed here and not shown
    there, and one made in the caller's only would be shown and not
    listed. It runs without the anchoring until the show path reads the
    commit leg at the home too.

    With `admission`, a record is a candidate only where the caller's
    admission counts its commit drift (`DriftScope.drift_counts`), the
    gate `memory_show` and `memory_search` read their commit leg under
    (`_response._DriftPass.lookup`): a record with no home row, a home
    the caller's surface does not admit, or a global one has no commit
    leg there, so it is not listed here as drifted either.
    """
    if caller_origin is None:
        return None
    if not caller_origin.repo or not caller_origin.cwd:
        return None
    cwd_path = Path(caller_origin.cwd)
    timestamps = commit_author_timestamps(cwd_path)
    if timestamps is None:
        return None
    # Resolve the repo root once for the whole rollup — the per-memory
    # anchor resolution below would otherwise pay a `git rev-parse`
    # fork+exec per drifting memory — and the head beside it, which the
    # reachable walks are keyed on.
    located = repo_toplevel_and_head(cwd_path)
    toplevel = located[0] if located is not None else None
    head = located[1] if located is not None else None

    # Two-pass: filter by repo match first, then run the bisect. Lets us
    # short-circuit the "no matching memories" case before any per-row
    # work — keeps the rollup silent when it would have nothing to say.
    # A memory with no claim anchors is dropped here too: repo commits
    # cannot invalidate a claim-less memory, so it can never be
    # "drifted" (the claim-anchored policy — see
    # `verify.resolve_commit_drift_count`; measured 100% false-positive
    # on the dogfood store before this gate).
    candidates: list[MemoryStats] = []
    if admission is not None:
        admission.prime(list(by_id))
    for stats in by_id.values():
        origin_repo = origin_repo_by_id.get(stats.id)
        if origin_repo is None:
            continue
        if not repos_match(origin_repo, caller_origin.repo):
            continue
        if stats.last_verified_at is None:
            continue
        if admission is not None and not admission.drift_counts(stats.id):
            continue
        if not anchor_paths_by_id.get(stats.id) and not (
            claims_by_id and claims_by_id.get(stats.id)
        ):
            continue
        candidates.append(stats)
    if not candidates:
        return None

    rows, unconfirmed = _drift_rows_by_root(
        candidates,
        cwd_path=cwd_path,
        timestamps=timestamps,
        toplevel=toplevel,
        head=head,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        # Counted where `memory_show` counts it: see the docstring.
        anchoring=None,
    )

    if not rows:
        # All matching memories are caught up — emit the bucket with an
        # empty rows list so the consumer can see we tried and the project
        # is clean, distinct from the "didn't try" None.
        return CommitDriftDebt(
            current_repo=caller_origin.repo,
            current_cwd=caller_origin.cwd,
            rows=[],
            total_drifted=0,
            unconfirmed=unconfirmed,
        )

    return CommitDriftDebt(
        current_repo=caller_origin.repo,
        current_cwd=caller_origin.cwd,
        rows=rows[:_COMMIT_DRIFT_DEBT_CAP],
        total_drifted=len(rows),
        unconfirmed=unconfirmed,
    )


def _drift_rows_by_root(
    candidates: list[MemoryStats],
    *,
    cwd_path: Path,
    timestamps: list[datetime],
    toplevel: Path | None,
    head: str | None,
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    verified_head_by_id: dict[str, str | None] | None,
    anchoring: Anchoring | None,
) -> tuple[list[CommitDriftRow], int]:
    """`_drift_rows_for_candidates` with each candidate counted at the root
    its cited commit resolves at (design 3.6, `verify.ClaimAnchor.
    commit_root`), and the count of candidates no live store here could
    resolve. A candidate with no home row, and every candidate without
    `anchoring`, is counted at the caller's directory as before; so is a
    homed one whose root is the caller's own work tree, with the history
    already read for it."""
    at_caller: list[MemoryStats] = []
    elsewhere: dict[str, list[MemoryStats]] = {}
    unconfirmed = 0
    caller_top = toplevel.resolve() if toplevel is not None else None
    for stats in candidates:
        anchor = anchoring.for_record(stats.id) if anchoring is not None else None
        if anchor is None:
            at_caller.append(stats)
            continue
        cited = verified_head_by_id.get(stats.id) if verified_head_by_id else None
        commit_root, _rule = anchor.commit_root(cited)
        if commit_root is None:
            unconfirmed += 1
        elif caller_top is not None and Path(commit_root).resolve() == caller_top:
            at_caller.append(stats)
        else:
            elsewhere.setdefault(commit_root, []).append(stats)
    rows: list[CommitDriftRow] = []
    if at_caller:
        rows.extend(
            _drift_rows_for_candidates(
                at_caller,
                root=cwd_path,
                timestamps=timestamps,
                toplevel=toplevel,
                anchor_paths_by_id=anchor_paths_by_id,
                claims_by_id=claims_by_id,
                verified_head_by_id=verified_head_by_id,
                head=head,
            )
        )
    for root_text, members in elsewhere.items():
        root = Path(root_text)
        root_timestamps = commit_author_timestamps(root)
        if root_timestamps is None:
            unconfirmed += len(members)
            continue
        located = repo_toplevel_and_head(root)
        rows.extend(
            _drift_rows_for_candidates(
                members,
                root=root,
                timestamps=root_timestamps,
                toplevel=located[0] if located is not None else None,
                anchor_paths_by_id=anchor_paths_by_id,
                claims_by_id=claims_by_id,
                verified_head_by_id=verified_head_by_id,
                head=located[1] if located is not None else None,
            )
        )
    rows.sort(key=lambda r: r.commits_since_verify, reverse=True)
    return rows, unconfirmed


# ---------------------------------------------------------------------------
# Public façade for callers that already have a memory directory
# ---------------------------------------------------------------------------


# `_parse_event_ts` and `_ensure_utc` are thin module-local aliases for
# the canonical helpers in `time_utils`. Kept as names because the rest
# of this module reads them as if they were local; the indirection
# centralises the parse / tz-stamp semantics without re-routing every
# call site through `time_utils.*`.
_parse_event_ts = parse_event_ts
_ensure_utc = ensure_utc


def report_for_store(
    store: Any,
    *,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    heavily_used_min_applied: int = 3,
    verification_stale_days: int = 30,
    cold_endorsement_min_retrievals: int = _COLD_ENDORSEMENT_MIN_RETRIEVALS,
    cold_endorsement_ratio_threshold: float = 0.0,
    caller_origin: Origin | None = None,
    now: datetime | None = None,
    typo_scope_exceptions: Iterable[str] | None = None,
    anchoring: Anchoring | None = None,
    admission: DriftScope | None = None,
) -> HealthReport:
    """Load the store's memories, walk its log, return the report. The
    entry point both production surfaces use: `memory_admin(health)` and
    `bettermemory health`.

    `typo_scope_exceptions` suppresses `fix_typo_scopes` for scopes the
    owner confirmed are not typos; left None it reads `[scopes]
    typo_exceptions` from the loaded config. `caller_origin`, if given,
    drives the cwd-aware `commit_drift_debt` rollup. `admission`, the
    request caller's LINKS admission (`handlers._shared.caller_anchoring`,
    as `memory_show` reads it), makes that rollup count a record's commit
    drift only where `memory_show` counts it; `anchoring`, the same
    caller's, reads `cross_repo_drift`'s homed groups where design 3.6
    reads their cited commit. `memory_admin(health)` passes both;
    `bettermemory health` passes neither, so it writes nothing.

    The dead-weight honesty gate is armed here rather than in
    `compute_health`'s default: every caller looking at a real store goes
    through this function, and a caller passing a synthetic event list
    does not."""
    if typo_scope_exceptions is None:
        try:
            from .config import load_config

            typo_scope_exceptions = load_config().scopes.typo_exceptions
        except Exception:  # noqa: BLE001 - a bad config must not block health
            typo_scope_exceptions = []
    tombstoned_ids = {t.id for t in store.load_tombstones()}
    memories = store.load_all()
    report = compute_health(
        memories,
        store.iter_events(),
        window_days=window_days,
        heavily_used_top_k=heavily_used_top_k,
        heavily_used_min_applied=heavily_used_min_applied,
        verification_stale_days=verification_stale_days,
        cold_endorsement_min_retrievals=cold_endorsement_min_retrievals,
        cold_endorsement_ratio_threshold=cold_endorsement_ratio_threshold,
        caller_origin=caller_origin,
        now=now,
        tombstoned_ids=tombstoned_ids,
        hook_telemetry_events=0,
        typo_scope_exceptions=typo_scope_exceptions,
        anchoring=anchoring,
        admission=admission,
    )
    report.provenance = provenance_debt(store, memories)
    report.recommendations = _compute_recommendations(report)
    report.episode_volume = store.episode_volume()
    return report


__all__ = [
    "ColdEndorsementMemories",
    "CommitDriftDebt",
    "CommitDriftRow",
    "MemoryStats",
    "MarkerStats",
    "RECOMMENDATION_KINDS",
    "RecentSilentMiss",
    "Recommendation",
    "ScopeHealth",
    "SilentMissStats",
    "TelemetryCoverage",
    "VerificationDebt",
    "HealthReport",
    "applied_tier",
    "compute_health",
    "is_hook_telemetry_event",
    "render_text",
    "render_json",
    "report_for_store",
]
