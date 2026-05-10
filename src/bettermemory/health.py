"""Aggregate health view of the memory store.

Reads from `events.iter_all_events` (active log + rotated archives) and
joins against `Store.load_all` to produce per-memory and per-marker
stats. Exposed two ways:

- as the `memory_health` MCP tool, so the model can self-curate during
  a conversation,
- as `bettermemory health` on the CLI, for offline audit.

The metrics are designed around the failure modes the rest of this
project is trying to detect:

- **dead_weight**: memories that have been retrieved but never `applied`
  (or never retrieved at all, despite existing for a while). Either the
  search ranking isn't surfacing them, or they're noise. Either way,
  prune candidates.
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
"""

from __future__ import annotations

import bisect
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .events import iter_all_events
from .models import Category, Memory, first_summary_line
from .origin import Origin, commit_author_timestamps, repos_match


# ---------------------------------------------------------------------------
# Per-memory and per-marker stats
# ---------------------------------------------------------------------------


@dataclass
class MemoryStats:
    """All the event-driven metrics for one memory.

    These are derived purely from the event log + the live memory record;
    nothing here is persisted on the memory itself. `last_verified_at` is
    the only field that comes off the memory record itself rather than
    the event stream — surfacing it here lets a curation pass treat
    "applied count" and "verification age" as orthogonal staleness axes
    without a second round-trip through the store.

    `category` mirrors the persisted memory field. Surfaced so the
    dead-weight / cold-memories filters can exclude ambient rows
    (their value is implicit and not visible in the use signal) and
    so the JSON consumer can spot ambient context at a glance.
    """

    id: str
    scopes: list[str]
    summary: str
    created: datetime
    updated: datetime
    retrieval_count: int = 0
    show_count: int = 0
    applied_count: int = 0
    ignored_count: int = 0
    contradicted_count: int = 0
    # `corrected` is the audit-only sibling of `contradicted`: the caller
    # noticed drift and fixed it inline (memory_update / memory_verify
    # already called) before logging the use event. Counted here so a
    # curation pass can see how often each memory has needed an inline
    # repair without conflating it with truly unresolved contradictions.
    corrected_count: int = 0
    last_used_at: datetime | None = None
    last_contradicted_at: datetime | None = None
    last_verified_at: datetime | None = None
    category: Category | None = None
    # Chronological list of resolution-relevant events for this memory:
    # each entry is `{kind: "update"|"verify"|"contradicted"|"corrected",
    # ts: "iso", note: str | None}`. Populated only for rows that land in
    # `HealthReport.contradicted` — most rows have nothing useful to say
    # and the field would just bloat the output. Lets the model see at a
    # glance whether a stuck flag is "out-of-order audit log" (resolution
    # events present but predate the contradicted event) or "genuinely
    # unresolved" (no resolution events after the contradiction).
    resolution_timeline: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_unresolved_contradiction(self) -> bool:
        """True if there's been a contradiction since the memory was
        last touched by either resolution path.

        Two ways to clear a contradiction:
        - **memory_update** bumps `updated` — the body has been refreshed
          in response to the contradiction.
        - **memory_verify** bumps `last_verified_at` — the body wasn't
          changed, but the caller spot-checked reality and confirmed it
          still matches despite the earlier contradiction event.

        Either action is a legitimate resolution, so the flag clears as
        soon as the later of the two timestamps surpasses the
        contradiction. This also gives the caller an out for the case
        where the `record_use(contradicted)` event is logged *after*
        the body was already corrected — re-running `memory_verify`
        slides the timestamp forward past the contradiction and the
        flag clears.
        """
        if self.last_contradicted_at is None:
            return False
        last_resolved_at = self.updated
        if (
            self.last_verified_at is not None
            and self.last_verified_at > last_resolved_at
        ):
            last_resolved_at = self.last_verified_at
        return self.last_contradicted_at > last_resolved_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopes": list(self.scopes),
            "summary": self.summary,
            "created": _iso(self.created),
            "updated": _iso(self.updated),
            "retrieval_count": self.retrieval_count,
            "show_count": self.show_count,
            "applied_count": self.applied_count,
            "ignored_count": self.ignored_count,
            "contradicted_count": self.contradicted_count,
            "corrected_count": self.corrected_count,
            "last_used_at": _iso(self.last_used_at) if self.last_used_at else None,
            "last_verified_at": (
                _iso(self.last_verified_at) if self.last_verified_at else None
            ),
            "category": self.category.value if self.category is not None else None,
            "has_unresolved_contradiction": self.has_unresolved_contradiction,
            "resolution_timeline": list(self.resolution_timeline),
        }


@dataclass
class MarkerStats:
    """Per-marker fire and override counts from `memory_write` events."""

    marker: str
    fire_count: int = 0
    override_count: int = 0

    @property
    def total(self) -> int:
        return self.fire_count + self.override_count

    @property
    def override_rate(self) -> float:
        """Fraction of fires the caller chose to override.

        High value = caller routinely rubber-stamps `acknowledge_transient`,
        which is the signal that the marker is producing too many false
        positives. Trim it.
        """
        return self.override_count / self.total if self.total > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "fire_count": self.fire_count,
            "override_count": self.override_count,
            "override_rate": round(self.override_rate, 3),
        }


@dataclass
class ScopeHealth:
    """Per-scope curation pivot.

    A flat dead_weight/heavily_used/contradicted view doesn't tell you
    whether the rot is concentrated in one scope. With per-scope counts
    you can drive a focused curation pass — "projects:foo has 4
    dead-weight memories out of 6 total, time to revisit" — without
    re-pivoting the flat lists by hand.

    Counts are over the same windowed event log as the flat view, so
    the numbers reconcile: sum of `active` across scopes >= total active
    (a memory tagged with N scopes is counted in each, by design).

    `cold` mirrors the new top-level cold_memories bucket: never
    retrieved within the window. Distinct from `dead` (which is now
    "retrieved but never applied"), so the two together tell the
    operator whether a scope's rot is "ranker not surfacing" (cold)
    or "model retrieving but not using" (dead).
    """

    scope: str
    active: int = 0
    dead: int = 0
    cold: int = 0
    contradicted: int = 0
    applied_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "active": self.active,
            "dead": self.dead,
            "cold": self.cold,
            "contradicted": self.contradicted,
            "applied_total": self.applied_total,
        }


@dataclass
class VerificationDebt:
    """Curation pivot for verification staleness.

    Mirrors the shape of `dead_weight` / `heavily_used`: capped row
    lists for inline display plus uncapped totals so the consumer can
    distinguish "5 never verified" from "500 never verified" without
    enumerating. The `fresh_count` is the residual — memories whose
    `last_verified_at` is within the staleness window — so
    `never_verified_total + stale_total + fresh_count` always equals
    the total number of active memories.
    """

    stale_after_days: int
    never_verified: list[MemoryStats] = field(default_factory=list)
    never_verified_total: int = 0
    stale: list[MemoryStats] = field(default_factory=list)
    stale_total: int = 0
    fresh_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stale_after_days": self.stale_after_days,
            "never_verified_total": self.never_verified_total,
            "stale_total": self.stale_total,
            "fresh_count": self.fresh_count,
            "never_verified": [s.to_dict() for s in self.never_verified],
            "stale": [s.to_dict() for s in self.stale],
        }


# Cap the inline row lists so the JSON stays bounded on big stores. The
# uncapped totals on VerificationDebt let a curation pass tell whether
# the bucket is small (handle now) or huge (schedule a session).
_VERIFICATION_DEBT_CAP = 20


@dataclass
class CommitDriftRow:
    """One memory whose verification anchor sits behind the current HEAD.

    Carries enough identity (`id`, `scopes`, `summary`) for a curation
    pass to act on the row without a follow-up `memory_show`. The
    `commits_since_verify` count is the actionable handle: high values
    (or values close to the total commit count of the repo) mean the
    memory has been verified for a snapshot the project has long since
    moved past.
    """

    id: str
    scopes: list[str]
    summary: str
    last_verified_at: datetime | None
    commits_since_verify: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopes": list(self.scopes),
            "summary": self.summary,
            "last_verified_at": (
                _iso(self.last_verified_at) if self.last_verified_at else None
            ),
            "commits_since_verify": self.commits_since_verify,
        }


@dataclass
class CommitDriftDebt:
    """Curation pivot for repo-aware staleness.

    Same shape philosophy as `VerificationDebt`: a capped `rows` list
    for inline display plus an uncapped `total_drifted`. Only meaningful
    when the health caller is currently inside a checkout of a repo
    matching at least one memory's origin — `current_repo` echoes back
    which repo this rollup is anchored to so a consumer doesn't have to
    guess. None on the `HealthReport` (rather than an empty `CommitDriftDebt`)
    when the caller wasn't in a repo, when git was unreachable, or when
    no memory's origin matched the current repo at all.

    Cwd-scoped by design: a health run from one repo answers a different
    question than the same run from another. Don't compare rows across
    runs from different cwds.
    """

    current_repo: str | None
    current_cwd: str | None
    rows: list[CommitDriftRow] = field(default_factory=list)
    total_drifted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_repo": self.current_repo,
            "current_cwd": self.current_cwd,
            "total_drifted": self.total_drifted,
            "rows": [r.to_dict() for r in self.rows],
        }


_COMMIT_DRIFT_DEBT_CAP = 20


@dataclass
class HealthReport:
    """The full aggregate view returned by `memory_health`."""

    generated_at: datetime
    window_days: int
    total_active_memories: int
    total_events: int
    distinct_sessions: int

    dead_weight: list[MemoryStats] = field(default_factory=list)
    # Memories created before the window that have NEVER been retrieved
    # (search hit count of zero in the window). Distinct from dead_weight,
    # which now requires `retrieval_count > 0 AND applied_count == 0` —
    # cold means "the ranker hasn't surfaced this for anyone to apply or
    # ignore in the window", which is a different curation question
    # ("does the trigger for this memory still exist?") than dead-weight's
    # ("is the model getting nothing from a memory it does retrieve?").
    # Ambient-category memories are excluded from both buckets — their
    # value is implicit and rarely shows up as a use event.
    cold_memories: list[MemoryStats] = field(default_factory=list)
    heavily_used: list[MemoryStats] = field(default_factory=list)
    contradicted: list[MemoryStats] = field(default_factory=list)
    marker_stats: list[MarkerStats] = field(default_factory=list)
    scope_distribution: dict[str, int] = field(default_factory=dict)
    # Per-scope curation pivot — "where is the rot concentrated?" Cheaper
    # than asking the model to fold scope_distribution and dead_weight
    # together every time.
    scope_health: list[ScopeHealth] = field(default_factory=list)
    # Singleton scopes that look like typos of another scope — flagged
    # only when there's a near neighbor (Levenshtein distance <= 2). A
    # singleton in isolation ("career", "personal-context") is usually
    # a legitimate narrow tag, not a misspell, so flagging every
    # singleton produced too many false positives in practice. The
    # neighbor check keeps the bucket actionable: if it fires, there's
    # almost always a real typo to fix.
    rare_scopes: list[str] = field(default_factory=list)
    # Use-events whose memory_id resolved to nothing (neither active nor
    # tombstoned). High counts hint at the model fabricating ULIDs in
    # `memory_record_use` — a quality signal worth surfacing.
    orphan_use_events: int = 0
    # Verification staleness rollup — never-verified vs stale vs fresh,
    # plus capped row lists for the rot. Unlike `dead_weight` and
    # `heavily_used`, this bucket is dominated by *young* memories on
    # any active store (every fresh write starts in `never_verified`
    # until something spot-checks it). The default field initializes
    # to an empty bucket; compute_health populates it during the run.
    verification_debt: VerificationDebt = field(
        default_factory=lambda: VerificationDebt(stale_after_days=30)
    )
    # Commit-drift rollup — memories whose verification anchor sits
    # behind the HEAD of the caller's current repo. Null when the caller
    # wasn't in a repo, when git was unreachable, or when no memory's
    # origin matched the current repo. Distinct from VerificationDebt:
    # that bucket asks "how long since I checked?", this one asks "did
    # the world I was checking against move?". A row can appear here
    # while still landing in `verification_debt.fresh_count` because the
    # calendar window hasn't elapsed.
    commit_drift_debt: CommitDriftDebt | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": _iso(self.generated_at),
            "window_days": self.window_days,
            "total_active_memories": self.total_active_memories,
            "total_events": self.total_events,
            "distinct_sessions": self.distinct_sessions,
            "dead_weight": [s.to_dict() for s in self.dead_weight],
            "cold_memories": [s.to_dict() for s in self.cold_memories],
            "heavily_used": [s.to_dict() for s in self.heavily_used],
            "contradicted": [s.to_dict() for s in self.contradicted],
            "marker_stats": [m.to_dict() for m in self.marker_stats],
            "scope_distribution": dict(self.scope_distribution),
            "scope_health": [s.to_dict() for s in self.scope_health],
            "rare_scopes": list(self.rare_scopes),
            "orphan_use_events": self.orphan_use_events,
            "verification_debt": self.verification_debt.to_dict(),
            "commit_drift_debt": (
                self.commit_drift_debt.to_dict()
                if self.commit_drift_debt is not None
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def compute_health(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    heavily_used_min_applied: int = 3,
    verification_stale_days: int = 30,
    caller_origin: Origin | None = None,
    now: datetime | None = None,
) -> HealthReport:
    """Build a `HealthReport` from active memories + the event stream.

    `events` is expected to be in chronological order — the function
    relies on that for "last_*" timestamps. `iter_all_events` returns
    archives + active log in chronological order; production callers
    should pass that directly.

    `window_days` controls the dead-weight cutoff: a memory is dead-weight
    if it was created more than `window_days` ago AND has no `applied`
    events. The window keeps recently-written memories from being flagged
    before they've had a chance to be retrieved.

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
    """
    if heavily_used_min_applied < 1:
        heavily_used_min_applied = 1
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    verification_cutoff = now - timedelta(days=verification_stale_days)

    by_id: dict[str, MemoryStats] = {}
    # Parallel mapping of memory id -> origin.repo, kept separately so we
    # don't have to add a field to MemoryStats just for the commit-drift
    # rollup. Captured during the same pass that builds `by_id` because
    # `memories` is an Iterable and we don't want to assume re-iterability.
    origin_repo_by_id: dict[str, str | None] = {}
    for m in memories:
        by_id[m.id] = MemoryStats(
            id=m.id,
            scopes=list(m.scopes),
            summary=first_summary_line(m.body),
            created=m.created,
            updated=m.updated,
            last_verified_at=m.last_verified_at,
            category=m.category,
        )
        origin_repo_by_id[m.id] = m.origin.repo if m.origin else None

    # Marker stats are accumulated by canonical marker name. Both
    # `markers` (transient_warning fires) and `markers_acknowledged`
    # (committed-with-override) feed in.
    marker_fires: Counter[str] = Counter()
    marker_overrides: Counter[str] = Counter()

    sessions: set[str] = set()
    total_events = 0
    orphan_use_events = 0

    # Per-id chronological log of resolution-relevant events
    # (update / verify / use[contradicted|corrected]). Accumulated for
    # every memory while we walk the event stream once; attached only to
    # rows that end up in the contradicted bucket (so we don't bloat the
    # output for rows that have nothing interesting to say). Cheaper than
    # re-iterating the events twice and bounded by the per-memory event
    # count, which is small in practice.
    resolution_events_by_id: dict[str, list[dict[str, Any]]] = {
        mid: [] for mid in by_id
    }

    def _append_resolution(mid: str, kind: str, ts_str: str | None, note: Any) -> None:
        # Defensive against malformed events: a missing or non-string
        # timestamp would still be useful in the timeline (the kind alone
        # tells you something happened), but we render it as None so the
        # consumer can skip unsorted entries cleanly.
        bucket = resolution_events_by_id.get(mid)
        if bucket is None:
            return
        bucket.append(
            {
                "kind": kind,
                "ts": ts_str if isinstance(ts_str, str) else None,
                "note": note if isinstance(note, str) else None,
            }
        )

    for ev in events:
        total_events += 1
        sess = ev.get("session")
        if sess:
            sessions.add(sess)

        kind = ev.get("kind")
        ts = _parse_ts(ev.get("ts"))

        if kind == "search":
            for mid in ev.get("returned", []) or []:
                stats = by_id.get(mid)
                if stats:
                    stats.retrieval_count += 1
        elif kind == "show":
            stats = by_id.get(ev.get("id", ""))
            if stats:
                stats.show_count += 1
        elif kind == "use":
            outcome = ev.get("outcome")
            for mid in ev.get("ids", []) or []:
                stats = by_id.get(mid)
                if stats is None:
                    # Memory may have been tombstoned after the use was
                    # recorded, or — more concerningly — the writer may
                    # have fabricated the ULID. We can't tell from the
                    # event alone (a tombstoned memory's id is a valid
                    # ULID just like any other), so we count both cases
                    # in `orphan_use_events`. A growing count is the
                    # "model is hallucinating ids" smoke test.
                    orphan_use_events += 1
                    continue
                if outcome == "applied":
                    stats.applied_count += 1
                elif outcome == "ignored":
                    stats.ignored_count += 1
                elif outcome == "contradicted":
                    stats.contradicted_count += 1
                    if ts is not None and (
                        stats.last_contradicted_at is None
                        or ts > stats.last_contradicted_at
                    ):
                        stats.last_contradicted_at = ts
                    _append_resolution(
                        mid, "contradicted", ev.get("ts"), ev.get("note")
                    )
                elif outcome == "corrected":
                    # Audit-only: the caller has already resolved via
                    # memory_update / memory_verify earlier in the turn.
                    # Increment the counter and bump last_used_at like
                    # any other use, but deliberately do NOT touch
                    # last_contradicted_at — that field is reserved for
                    # the unresolved-contradiction signal.
                    stats.corrected_count += 1
                    _append_resolution(mid, "corrected", ev.get("ts"), ev.get("note"))
                if ts is not None and (
                    stats.last_used_at is None or ts > stats.last_used_at
                ):
                    stats.last_used_at = ts
        elif kind == "update":
            mid = ev.get("id", "")
            if isinstance(mid, str) and mid:
                _append_resolution(mid, "update", ev.get("ts"), ev.get("note"))
        elif kind == "verify":
            mid = ev.get("id", "")
            if isinstance(mid, str) and mid:
                _append_resolution(mid, "verify", ev.get("ts"), ev.get("note"))
        elif kind == "write":
            for marker in ev.get("markers", []) or []:
                marker_fires[marker] += 1
            for marker in ev.get("markers_acknowledged", []) or []:
                marker_overrides[marker] += 1

    all_markers = sorted(set(marker_fires) | set(marker_overrides))
    marker_stats = [
        MarkerStats(
            marker=m,
            fire_count=marker_fires[m],
            override_count=marker_overrides[m],
        )
        for m in all_markers
    ]
    marker_stats.sort(key=lambda s: s.total, reverse=True)

    scope_distribution = Counter(
        scope for stats in by_id.values() for scope in stats.scopes
    )

    # Dead weight: the memory IS being retrieved within the window but
    # nothing the model produced ever called `record_use(applied)`. That's
    # the actionable signal — the ranker is surfacing the memory but the
    # model isn't getting value from it. Either the body is misleading,
    # the scopes are wrong, or the content is duplicate-noise. Either way,
    # a curation pass should look.
    #
    # Memories with `retrieval_count == 0` move into `cold_memories`
    # below. Ambient-category memories are excluded from both buckets:
    # their value is implicit (they shape responses without being cited),
    # so the use signal is structurally absent and a count of zero
    # there is not an indictment.
    dead_weight = [
        s
        for s in by_id.values()
        if s.category != Category.AMBIENT
        and s.created < cutoff
        and s.retrieval_count > 0
        and s.applied_count == 0
    ]
    dead_weight.sort(key=lambda s: s.created)

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
    # check (Levenshtein distance <= 2 against any other scope, including
    # other singletons) restricts the bucket to scopes that almost
    # certainly *are* typos: "projct:foo" against an existing
    # "projects:foo", "tool" against "tools", "bug"/"bugs" pairs.
    all_scopes = list(scope_distribution.keys())
    rare_scopes = sorted(
        scope
        for scope, count in scope_distribution.items()
        if count == 1
        and any(
            other != scope and _edit_distance_within(scope, other, 2)
            for other in all_scopes
        )
    )

    # Verification debt — partition active memories into never_verified /
    # stale / fresh against the staleness threshold. Sort each bucket by
    # the timestamp that's most actionable for a curation pass:
    # never_verified by `created` (oldest unverified first — those are
    # the highest-risk because they've had the most time to drift), and
    # stale by `last_verified_at` (oldest verification first — same
    # rationale, applied to memories that have at least been spot-checked
    # once). The capped `_VERIFICATION_DEBT_CAP` rows are inlined for
    # display; the totals are always uncapped so a downstream reader can
    # tell "5 stale" from "500 stale" without re-counting.
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
    never_verified_all.sort(key=lambda s: s.created)
    stale_all.sort(key=lambda s: s.last_verified_at or s.created)

    verification_debt = VerificationDebt(
        stale_after_days=verification_stale_days,
        never_verified=never_verified_all[:_VERIFICATION_DEBT_CAP],
        never_verified_total=len(never_verified_all),
        stale=stale_all[:_VERIFICATION_DEBT_CAP],
        stale_total=len(stale_all),
        fresh_count=fresh_count,
    )

    commit_drift_debt = _compute_commit_drift_debt(
        by_id=by_id,
        origin_repo_by_id=origin_repo_by_id,
        caller_origin=caller_origin,
    )

    return HealthReport(
        generated_at=now,
        window_days=window_days,
        total_active_memories=len(by_id),
        total_events=total_events,
        distinct_sessions=len(sessions),
        dead_weight=dead_weight,
        cold_memories=cold_memories,
        heavily_used=heavily_used,
        contradicted=contradicted,
        marker_stats=marker_stats,
        scope_distribution=dict(scope_distribution),
        scope_health=scope_health,
        rare_scopes=rare_scopes,
        orphan_use_events=orphan_use_events,
        verification_debt=verification_debt,
        commit_drift_debt=commit_drift_debt,
    )


def _compute_commit_drift_debt(
    *,
    by_id: dict[str, MemoryStats],
    origin_repo_by_id: dict[str, str | None],
    caller_origin: Origin | None,
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
    """
    if caller_origin is None:
        return None
    if not caller_origin.repo or not caller_origin.cwd:
        return None
    timestamps = commit_author_timestamps(Path(caller_origin.cwd))
    if timestamps is None:
        return None
    timestamps_sorted = sorted(timestamps)

    # Two-pass: filter by repo match first, then run the bisect. Lets us
    # short-circuit the "no matching memories" case before any per-row
    # work — keeps the rollup silent when it would have nothing to say.
    candidates: list[MemoryStats] = []
    for stats in by_id.values():
        origin_repo = origin_repo_by_id.get(stats.id)
        if origin_repo is None:
            continue
        if not repos_match(origin_repo, caller_origin.repo):
            continue
        if stats.last_verified_at is None:
            continue
        candidates.append(stats)
    if not candidates:
        return None

    rows: list[CommitDriftRow] = []
    for stats in candidates:
        since = stats.last_verified_at
        assert since is not None  # filtered above
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        # bisect_right gives us the first index strictly greater than
        # `since`; len - idx is then the count of timestamps after that
        # cut. Equal timestamps fall before the cut on bisect_right
        # semantics, which is what we want — a verify call that lands
        # at the same instant as a commit doesn't count as drift.
        idx = bisect.bisect_right(timestamps_sorted, since)
        count = len(timestamps_sorted) - idx
        if count > 0:
            rows.append(
                CommitDriftRow(
                    id=stats.id,
                    scopes=list(stats.scopes),
                    summary=stats.summary,
                    last_verified_at=stats.last_verified_at,
                    commits_since_verify=count,
                )
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
        )

    rows.sort(key=lambda r: r.commits_since_verify, reverse=True)
    return CommitDriftDebt(
        current_repo=caller_origin.repo,
        current_cwd=caller_origin.cwd,
        rows=rows[:_COMMIT_DRIFT_DEBT_CAP],
        total_drifted=len(rows),
    )


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------


def render_text(report: HealthReport) -> str:
    """Pretty-print the report for the CLI subcommand. Plain text — no
    colour codes — so it pipes cleanly to a file."""
    lines: list[str] = []
    lines.append(
        f"Memory health — generated {_iso(report.generated_at)}, "
        f"window {report.window_days} days"
    )
    lines.append("=" * 70)
    lines.append(f"Active memories: {report.total_active_memories}")
    lines.append(f"Events seen:     {report.total_events}")
    lines.append(f"Sessions:        {report.distinct_sessions}")

    lines.append("")
    lines.append(
        f"Dead weight ({len(report.dead_weight)}) — retrieved but never "
        f"`applied`, older than {report.window_days} days:"
    )
    if not report.dead_weight:
        lines.append("  (none)")
    for s in report.dead_weight[:20]:
        lines.append(
            f"  {s.id} [retrievals={s.retrieval_count}] {','.join(s.scopes)}: {s.summary}"
        )
    if len(report.dead_weight) > 20:
        lines.append(f"  ... and {len(report.dead_weight) - 20} more")

    lines.append("")
    lines.append(
        f"Cold memories ({len(report.cold_memories)}) — never retrieved, "
        f"older than {report.window_days} days:"
    )
    if not report.cold_memories:
        lines.append("  (none)")
    for s in report.cold_memories[:20]:
        lines.append(f"  {s.id} {','.join(s.scopes)}: {s.summary}")
    if len(report.cold_memories) > 20:
        lines.append(f"  ... and {len(report.cold_memories) - 20} more")

    lines.append("")
    lines.append(f"Heavily used ({len(report.heavily_used)}):")
    if not report.heavily_used:
        lines.append("  (none)")
    for s in report.heavily_used:
        lines.append(
            f"  {s.id} [applied={s.applied_count}] {','.join(s.scopes)}: {s.summary}"
        )

    lines.append("")
    lines.append(f"Contradicted ({len(report.contradicted)}):")
    if not report.contradicted:
        lines.append("  (none)")
    for s in report.contradicted:
        lines.append(
            f"  {s.id} [contradicted={s.contradicted_count}] {','.join(s.scopes)}: {s.summary}"
        )

    lines.append("")
    lines.append(f"Marker stats ({len(report.marker_stats)}):")
    if not report.marker_stats:
        lines.append("  (none)")
    for m in report.marker_stats:
        rate_pct = round(m.override_rate * 100, 1)
        lines.append(
            f"  {m.marker:<24} fires={m.fire_count}  "
            f"overrides={m.override_count}  rate={rate_pct}%"
        )

    lines.append("")
    lines.append(f"Scope health ({len(report.scope_health)}):")
    if not report.scope_health:
        lines.append("  (none)")
    for sh in report.scope_health:
        lines.append(
            f"  {sh.scope:<28} active={sh.active:<3} dead={sh.dead:<3} "
            f"cold={sh.cold:<3} contradicted={sh.contradicted:<3} "
            f"applied={sh.applied_total}"
        )

    lines.append("")
    lines.append(
        f"Rare scopes ({len(report.rare_scopes)}) — singletons within "
        "2 edits of another scope, likely typos:"
    )
    if not report.rare_scopes:
        lines.append("  (none)")
    for scope in report.rare_scopes:
        lines.append(f"  {scope}")

    if report.orphan_use_events:
        lines.append("")
        lines.append(
            f"Orphan use events: {report.orphan_use_events} — "
            "memory_record_use events whose ids resolved to neither active "
            "nor tombstoned memories. A growing count is the smoke test for "
            "fabricated ULIDs."
        )

    debt = report.verification_debt
    lines.append("")
    lines.append(
        f"Verification debt — never={debt.never_verified_total}  "
        f"stale={debt.stale_total}  fresh={debt.fresh_count}  "
        f"(stale after {debt.stale_after_days} days):"
    )
    if debt.never_verified_total == 0 and debt.stale_total == 0:
        lines.append("  (none)")
    if debt.never_verified:
        lines.append(f"  never-verified ({debt.never_verified_total}, oldest first):")
        for s in debt.never_verified:
            lines.append(f"    {s.id} {','.join(s.scopes)}: {s.summary}")
        if debt.never_verified_total > len(debt.never_verified):
            lines.append(
                f"    ... and {debt.never_verified_total - len(debt.never_verified)} more"
            )
    if debt.stale:
        lines.append(f"  stale ({debt.stale_total}, oldest verification first):")
        for s in debt.stale:
            verified = _iso(s.last_verified_at) or "?"
            lines.append(
                f"    {s.id} [verified={verified}] {','.join(s.scopes)}: {s.summary}"
            )
        if debt.stale_total > len(debt.stale):
            lines.append(f"    ... and {debt.stale_total - len(debt.stale)} more")

    cd = report.commit_drift_debt
    if cd is not None:
        lines.append("")
        lines.append(
            f"Commit drift — anchor={cd.current_repo or '?'}  "
            f"drifted={cd.total_drifted}:"
        )
        if not cd.rows:
            lines.append("  (none — anchored memories are caught up with HEAD)")
        else:
            lines.append(f"  drifted ({cd.total_drifted}, most commits-ahead first):")
            for row in cd.rows:
                verified = _iso(row.last_verified_at) or "?"
                lines.append(
                    f"    {row.id} [+{row.commits_since_verify} commits, "
                    f"verified={verified}] {','.join(row.scopes)}: {row.summary}"
                )
            if cd.total_drifted > len(cd.rows):
                lines.append(f"    ... and {cd.total_drifted - len(cd.rows)} more")

    return "\n".join(lines) + "\n"


def render_json(report: HealthReport) -> str:
    return json.dumps(report.to_dict(), indent=2) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _edit_distance_within(a: str, b: str, max_dist: int) -> bool:
    """True iff Levenshtein(a, b) <= max_dist.

    Standard Wagner-Fischer DP, two-row variant. We don't need the
    actual distance — only whether it falls within the threshold —
    but scope names are short enough (typically <30 chars) that the
    full table is cheap and the early-exit machinery isn't worth its
    complexity. The length-difference shortcut catches the obviously
    far cases without running the table at all.

    Used by the `rare_scopes` neighbor check; lifted out as a helper
    so it stays testable in isolation if we ever need to tune the
    threshold or swap algorithms.
    """
    if abs(len(a) - len(b)) > max_dist:
        return False
    if a == b:
        return True
    # Ensure |a| <= |b| so the inner row stays small.
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for i, cb in enumerate(b, 1):
        curr = [i] + [0] * len(a)
        for j, ca in enumerate(a, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,
                prev[j] + 1,
                prev[j - 1] + cost,
            )
        prev = curr
    return prev[-1] <= max_dist


# ---------------------------------------------------------------------------
# Public façade for callers that already have a memory directory
# ---------------------------------------------------------------------------


def curation_counts(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    window_days: int = 30,
    verification_stale_days: int = 30,
    caller_origin: Origin | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Cheap summary of curation pressure.

    Returns
    ``{"stale", "never_verified", "drifted", "cold", "dead"}`` —
    integer counts only, no row materialisation. Used by
    `memory_scope_overview` so the model can see at a glance whether
    the store has anything worth a curation pass without paying the
    full `compute_health` cost (which materialises and sorts every
    bucket and walks the event log to build resolution timelines).

    Numerical contract: each count must agree with the corresponding
    bucket size from `compute_health` over the same memories/events
    and same parameters. The tests in `tests/test_health.py` lock
    that in. We intentionally walk the event log here too — the
    "cheap" comes from skipping row construction, not from skipping
    the event walk (the walk is bounded and a session-start hint
    pays it once per session, which is the right cost).

    `caller_origin` drives the `drifted` count, mirroring
    `_compute_commit_drift_debt`. Pass None to skip the
    repo-aware portion (the count stays at zero).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    verification_cutoff = now - timedelta(days=verification_stale_days)

    # Re-iterate only once over `memories` — pull the slim bookkeeping
    # we need.
    mem_list: list[Memory] = list(memories)

    retrieval_counts: dict[str, int] = {m.id: 0 for m in mem_list}
    applied_counts: dict[str, int] = {m.id: 0 for m in mem_list}
    for ev in events:
        kind = ev.get("kind")
        if kind == "search":
            for mid in ev.get("returned", []) or []:
                if mid in retrieval_counts:
                    retrieval_counts[mid] += 1
        elif kind == "use" and ev.get("outcome") == "applied":
            for mid in ev.get("ids", []) or []:
                if mid in applied_counts:
                    applied_counts[mid] += 1

    never_verified = 0
    stale = 0
    cold = 0
    dead = 0
    for m in mem_list:
        is_ambient = m.category == Category.AMBIENT
        if m.last_verified_at is None:
            never_verified += 1
        elif m.last_verified_at < verification_cutoff:
            stale += 1
        if not is_ambient and m.created < cutoff:
            r = retrieval_counts.get(m.id, 0)
            a = applied_counts.get(m.id, 0)
            if r == 0:
                cold += 1
            elif a == 0:
                dead += 1

    drifted = 0
    if caller_origin is not None and caller_origin.repo and caller_origin.cwd:
        timestamps = commit_author_timestamps(Path(caller_origin.cwd))
        if timestamps is not None:
            timestamps_sorted = sorted(timestamps)
            for m in mem_list:
                if m.last_verified_at is None:
                    continue
                origin_repo = m.origin.repo if m.origin else None
                if origin_repo is None:
                    continue
                if not repos_match(origin_repo, caller_origin.repo):
                    continue
                since = m.last_verified_at
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                idx = bisect.bisect_right(timestamps_sorted, since)
                if len(timestamps_sorted) - idx > 0:
                    drifted += 1

    return {
        "stale": stale,
        "never_verified": never_verified,
        "drifted": drifted,
        "cold": cold,
        "dead": dead,
    }


def report_for_directory(
    root: Path,
    *,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    heavily_used_min_applied: int = 3,
    verification_stale_days: int = 30,
    caller_origin: Origin | None = None,
    now: datetime | None = None,
) -> HealthReport:
    """Convenience: load memories from `root`, walk the event log, return
    the report. Used by both the MCP tool and the CLI subcommand.

    `caller_origin`, if provided, drives the cwd-aware `commit_drift_debt`
    rollup. Production callers should pass `origin.capture()`'s result;
    leaving it None skips the rollup, which is appropriate for offline
    tooling that doesn't have a meaningful cwd to anchor against."""
    from .store import Store

    store = Store(root)
    return compute_health(
        store.load_all(),
        iter_all_events(root),
        window_days=window_days,
        heavily_used_top_k=heavily_used_top_k,
        heavily_used_min_applied=heavily_used_min_applied,
        verification_stale_days=verification_stale_days,
        caller_origin=caller_origin,
        now=now,
    )


__all__ = [
    "CommitDriftDebt",
    "CommitDriftRow",
    "MemoryStats",
    "MarkerStats",
    "ScopeHealth",
    "VerificationDebt",
    "HealthReport",
    "compute_health",
    "curation_counts",
    "render_text",
    "render_json",
    "report_for_directory",
]
