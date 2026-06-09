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
from typing import Any, Callable, Iterable

from .events import iter_all_events
from .models import Category, Memory, first_summary_line
from .origin import (
    Origin,
    commit_author_timestamps,
    commits_since_touching_paths,
    repos_match,
)
from .time_utils import (
    ensure_utc,
    isoformat_utc_optional,
    parse_event_ts,
)


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
    # `applied_count` is the total of auto + explicit. Kept as a single
    # field so existing consumers and tests don't need to fold two
    # counts together; the split below tells you *how* the count was
    # reached.
    applied_count: int = 0
    # The model never called memory_record_use(applied) explicitly for
    # this id — the count came entirely from the server's auto-commit
    # pass that fires ~2 turns after a retrieval. A high
    # `auto_applied_count` with zero `explicit_applied_count` is the
    # "weakly endorsed" signal: the ranker keeps surfacing it, the auto
    # pass keeps logging it, but the model never deliberately reaches
    # for it. Pairs with the cold_endorsement_memories rollup.
    auto_applied_count: int = 0
    # The model called memory_record_use(applied) directly. The
    # deliberate-endorsement signal; a non-zero value means at least
    # once the model wrote a use event for this id rather than letting
    # the auto pass close the loop.
    explicit_applied_count: int = 0
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

    @property
    def endorsement_ratio(self) -> float | None:
        """Fraction of applies that were explicit, or None when there are
        no applies to ratio over.

        Closer to 1.0 means the model is deliberately endorsing this
        memory (calling memory_record_use directly). Closer to 0.0 means
        every applied event came from the server's auto-commit pass —
        the model retrieves the memory but never bothers to confirm it
        shaped the response. The latter is the "weakly endorsed"
        signal. Returns None on `applied_count == 0` so the consumer
        can distinguish "zero apply traffic" from "applied but all
        auto."
        """
        if self.applied_count == 0:
            return None
        return self.explicit_applied_count / self.applied_count

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
            "auto_applied_count": self.auto_applied_count,
            "explicit_applied_count": self.explicit_applied_count,
            "endorsement_ratio": self.endorsement_ratio,
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

# Minimum `retrieval_count` for a memory to be eligible for the
# `cold_endorsement_memories` bucket. Below this floor we treat the
# absence of explicit endorsement as "not enough traffic to judge"
# rather than a real signal. Five mirrors the same intuition behind
# `heavily_used_min_applied=3`: a handful of retrievals is enough to
# call a pattern, fewer is single-incident noise. Tunable inline on
# the compute_health call so tests can lower the floor without forcing
# a config bump for the common case.
_COLD_ENDORSEMENT_MIN_RETRIEVALS = 5

# Cap the inline row list. Same shape as the verification_debt and
# commit_drift_debt rollups — uncapped `total` for the bucket size,
# capped rows for inline display.
_COLD_ENDORSEMENT_CAP = 20


def _is_weakly_endorsed(stats: MemoryStats, ratio_threshold: float) -> bool:
    """Predicate for the cold_endorsement_memories bucket.

    Gated on "at least one apply happened" first: `applied_count == 0`
    returns False. The bucket is the COMPLEMENT of dead_weight (see the
    `ColdEndorsementMemories` docstring) — "applies happened, but every
    one was the auto fallback." A memory that was retrieved but never
    applied at all (auto included) belongs in dead_weight, not here;
    without this gate a pure dead-weight row (retrieval over the floor,
    zero applies) would satisfy `explicit_applied_count == 0` and land
    in BOTH buckets, double-counting it and mis-routing the
    never-applied memory to the acknowledge-debt path instead of removal.

    Past the gate, returns True when the memory looks weakly endorsed
    under either of two checks:

    - **Binary** (always on): `explicit_applied_count == 0`. The
      memory has been retrieved enough times to cross the floor and
      at least one applied event fired, but the model never
      deliberately called `memory_record_use(applied)` — every applied
      event came from the server's auto-fallback.

    - **Ratio** (off by default, on when `ratio_threshold > 0`):
      `explicit_applied_count / (auto + explicit) < ratio_threshold`.
      The model has reached for it occasionally, but the auto pass
      is doing most of the work — a "1 explicit out of 50 auto" case
      the binary check would miss.

    Default `ratio_threshold=0.0` preserves the original binary
    semantics exactly: the predicate reduces to the equality check
    because the ratio branch needs `ratio_threshold > 0` to fire.
    """
    if stats.applied_count == 0:
        return False
    if stats.explicit_applied_count == 0:
        return True
    if ratio_threshold <= 0.0:
        return False
    total_applied = stats.auto_applied_count + stats.explicit_applied_count
    if total_applied <= 0:
        return False
    ratio = stats.explicit_applied_count / total_applied
    return ratio < ratio_threshold


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


# Cap on the inline `recent_silent_misses` list — newest-first, bounded
# so the JSON stays compact on large stores. Ten is enough for a model
# to triage typical false-positive patterns at a glance; the full event
# log remains the source of truth for an exhaustive sweep.
_RECENT_SILENT_MISSES_CAP = 10


@dataclass
class ColdEndorsementMemories:
    """Curation pivot for retrieved-but-never-endorsed memories.

    Counts MEMORIES, not turns: every entry is one distinct memory
    that crossed the retrieval floor (`retrieval_count >=
    min_retrievals`) AND has `explicit_applied_count == 0` (or, when
    the ratio threshold is on, a ratio of explicit-to-total applies
    below the threshold). A single memory hit 50 times by the ranker
    contributes ONE row, not 50 — the bucket is "memories whose
    endorsement signal is cold despite heavy retrieval," not a count
    of cold-endorsement events.

    The "weakly endorsed" pattern: the server's auto-commit pass has
    been closing the loop on every retrieval, but no
    `memory_record_use(applied)` has ever fired explicitly. Either the
    memory IS useful and deserves a deliberate spot-check (verify + an
    explicit applied on the next hit), or the ranker is over-surfacing
    it and the right move is a narrower scope or a removal.

    Distinct from `dead_weight` (retrieved but never *applied* at all,
    auto included): dead_weight says the model doesn't even let the
    auto pass run on this — it must have called something that purged
    the use-token without recording. Cold-endorsement says the
    opposite: applies happened, but every single one was the auto
    fallback. The two together cover the spectrum of "applied signal
    is weak."

    Ambient memories are excluded — their value is implicit (they
    shape responses without being cited) and an explicit use event for
    them is structurally rare. Mirrors the exclusion in `dead_weight`
    / `cold_memories` for the same reason.

    Same shape as `VerificationDebt`: capped `rows` for inline display
    plus an uncapped `total` so a downstream reader can distinguish
    "3 weakly endorsed memories" from "300 weakly endorsed memories"
    without re-counting.
    """

    min_retrievals: int
    rows: list[MemoryStats] = field(default_factory=list)
    total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_retrievals": self.min_retrievals,
            "total": self.total,
            "rows": [s.to_dict() for s in self.rows],
        }


@dataclass
class SilentMissStats:
    """Rollup of `search_miss` / `turn_audited` events over the window.

    Surfaces the false-negative half of the retrieval contract: turns
    where the model should have searched but didn't. `audited_total` is
    the denominator (audits that ran at all); `miss_total` is the
    numerator (audits that flagged a miss). A consumer can compute the
    miss *rate* with `miss_total / audited_total` when audited_total > 0;
    we don't ship the float here because rate-vs-count is a presentation
    choice and the raw counts are stable across consumers.

    Empty bucket (both zero) means the audit hook either wasn't invoked
    in the window or invoked but never fired anything past the
    no-signal branch — distinct from "audited heavily, no misses,"
    which would have a non-zero `audited_total`. The two-count shape is
    deliberate so a stalled hook ("nothing audited at all") doesn't
    look the same as a healthy run ("audited a lot, model behaved").

    `miss_total` historically counted every `search_miss` event in the
    window. That conflates "9 turns hammering the same unretrieved
    memory" with "9 distinct unretrieved memories" — both look like 9.
    The rollup now also surfaces `unique_miss_memories`: the cardinality
    of the set of top-hit memory_ids on the in-window miss events. The
    pair lets a consumer read "9 events across 1 memory" (one mis-tagged
    memory the model keeps probing) vs. "9 events across 9 memories"
    (genuinely broad retrieval slippage). Misses whose top-hit memory
    has since been tombstoned are dropped from BOTH counters — once a
    memory is gone the miss is no longer actionable. `miss_total` retains
    its name for back-compat with existing consumers; the to_dict shape
    surfaces both keys.

    Silent-miss events acknowledged via `memory_acknowledge_miss` (T4)
    are also dropped from both counters — the per-event escape hatch
    for false positives the bulk `silent_miss_cutoff` would over-wipe.
    The ack-filter runs alongside the tombstone filter so the rollup
    reflects "outstanding actionable misses" rather than "every miss
    ever seen."
    """

    audited_total: int = 0
    miss_total: int = 0
    unique_miss_memories: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "audited_total": self.audited_total,
            "miss_total": self.miss_total,
            "unique_miss_memories": self.unique_miss_memories,
        }


@dataclass
class RecentSilentMiss:
    """One unacknowledged ``search_miss`` event surfaced for triage.

    Carried on ``HealthReport.recent_silent_misses`` so the model has
    something to feed into ``memory_acknowledge_miss(event_id, reason)``
    when it spots a false positive. The full event log is the source of
    truth; this list is a small, bounded inline subset designed for
    inline display — newest first, capped at
    ``_RECENT_SILENT_MISSES_CAP``.

    Fields:

    - ``event_id``: the per-event ULID stamped at emission time. Echoed
      back to ``memory_acknowledge_miss`` to scope an ack to one event.
      ``None`` only for legacy events written before T4 added the field;
      those rows surface for visibility but cannot be acknowledged.
    - ``top_hit_id``: the first id in the event's ``top_hits`` payload —
      the memory the probe found that the model should have retrieved.
    - ``query_preview``: short triage string (first 32 chars of the
      probe query, redacted shape under ``log_queries_verbatim=False``).
    - ``ts``: the event's ISO timestamp.
    """

    event_id: str | None
    top_hit_id: str | None
    query_preview: str | None
    ts: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "top_hit_id": self.top_hit_id,
            "query_preview": self.query_preview,
            "ts": self.ts,
        }


@dataclass
class Recommendation:
    """One actionable curation suggestion distilled from the bucket
    rollups.

    The buckets (dead_weight, contradicted, cold_endorsement_memories,
    rare_scopes, commit_drift_debt) carry the raw rows. A
    Recommendation collapses each one into "you have N memories of
    kind K, here's the one-line action that resolves them." Designed
    for proactive in-conversation surfacing where the full bucket
    detail would be too verbose.

    Pull-based discovery via the raw bucket fields remains the
    primary path. Recommendations are an additive convenience for the
    model / CLI that wants the digest.

    `kind` is the discriminator — closed set, listed in
    `RECOMMENDATION_KINDS`. `summary` describes the state, `action`
    names the fix. `memory_ids` carries up to
    `_RECOMMENDATION_ROW_CAP` ids (10 by default) so the model can
    drill in without an unbounded list. `scope` is populated only on
    scope-level recommendations (the typo-singleton case).
    """

    kind: str
    summary: str
    action: str
    count: int
    memory_ids: list[str] = field(default_factory=list)
    scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "action": self.action,
            "count": self.count,
            "memory_ids": list(self.memory_ids),
            "scope": self.scope,
        }


# Cap on memory_ids surfaced per Recommendation. Keeps the
# recommendations block bounded even on a large rotting store; the
# uncapped `count` field still tells the consumer the true size.
_RECOMMENDATION_ROW_CAP = 10

# Minimum bucket size that triggers a recommendation for size-driven
# kinds (dead_weight, cold_endorsement_memories, drifted). Below this
# floor the bucket is too small to warrant a proactive surface — the
# model doesn't need to be nudged to remove 1 dead-weight memory. The
# contradicted and rare_scopes recommendations use floor=1 because
# even a single instance is actionable (one stuck contradiction is
# still a stuck contradiction; one typo singleton is still a typo).
_RECOMMENDATION_SIZE_FLOOR = 3

# Closed set of recommendation kinds. Exhaustive enumeration so a
# consumer can switch over them without a missing-case branch. Adding
# a new kind requires extending this constant and `_compute_recommendations`.
RECOMMENDATION_KINDS: tuple[str, ...] = (
    "remove_dead_weight",
    "resolve_contradicted",
    "cleanup_cold_endorsements",
    "verify_drifted",
    "fix_typo_scopes",
)


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
    # Silent-miss telemetry — the false-negative half of opt-in
    # retrieval. `audited_total` and `miss_total` come from the
    # `turn_audited` and `search_miss` event kinds emitted by
    # memory_audit_turn. The pair is the denominator + numerator for the
    # miss rate; we keep them as raw counts so the consumer chooses how
    # to render. Both zero means the audit hook hasn't fired in the
    # window (or fired but only produced no_signal verdicts) — distinct
    # from "audited heavily, no misses found."
    silent_misses: SilentMissStats = field(default_factory=SilentMissStats)
    # Inline subset of unacknowledged `search_miss` events for triage.
    # Newest first, capped at `_RECENT_SILENT_MISSES_CAP`. Each entry
    # carries the per-event `event_id` the model feeds into
    # `memory_acknowledge_miss(event_id, reason)` when a miss turns out
    # to be a false positive (e.g. a stopword-heavy query). Tombstoned
    # and already-acked events are filtered out so the surface only
    # shows actionable misses. Empty when the audit hook hasn't been
    # firing or every flagged miss has been acked.
    recent_silent_misses: list[RecentSilentMiss] = field(default_factory=list)
    # Cold-endorsement-memories rollup — counts distinct memories the
    # ranker keeps surfacing (retrieval_count >= min) but the model
    # never explicitly endorses (explicit_applied_count == 0). The
    # "weakly endorsed" pattern; complement to dead_weight (which is
    # "never applied at all"). One memory hit 50 times contributes 1
    # to total, not 50 — this is a per-memory count, not a per-event
    # or per-turn count. Empty bucket = either no memory has crossed
    # the retrieval floor or every heavily-retrieved memory has at
    # least one explicit applied event.
    cold_endorsement_memories: ColdEndorsementMemories = field(
        default_factory=lambda: ColdEndorsementMemories(
            min_retrievals=_COLD_ENDORSEMENT_MIN_RETRIEVALS,
        )
    )
    # Proactive curation recommendations distilled from the buckets
    # above. Each entry collapses "N memories of kind K" into the
    # one-line action that resolves them. Empty list when no bucket
    # crosses the size floor — a healthy store surfaces nothing.
    # Populated by `_compute_recommendations` during `compute_health`;
    # consumers can ignore the field entirely and read the raw buckets
    # directly, which is what the existing CLI text rendering does.
    recommendations: list[Recommendation] = field(default_factory=list)

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
            "silent_misses": self.silent_misses.to_dict(),
            "recent_silent_misses": [m.to_dict() for m in self.recent_silent_misses],
            "cold_endorsement_memories": self.cold_endorsement_memories.to_dict(),
            "recommendations": [r.to_dict() for r in self.recommendations],
        }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


@dataclass
class _AccumulatorRollups:
    """The frozen output of ``_StatsAccumulator.rollups()``.

    A flat record so the caller (``compute_health``) can pluck out
    individual fields with a tuple-unpack feel rather than reading
    attributes off the live accumulator. Each field maps to one of
    the per-event-kind counters the pre-Round-2 inline loop maintained
    as local variables.
    """

    marker_fires: Counter[str]
    marker_overrides: Counter[str]
    sessions: set[str]
    total_events: int
    orphan_use_events: int
    silent_miss_audited_ts: list[datetime | None]
    # Per-miss-event records carrying everything downstream rollups
    # need. Each entry is `(ts, top_hit_id_or_None, event_id_or_None,
    # query_preview_or_None)`. The id is the first entry in the
    # event's `top_hits` payload (audit.py:418-427) — the memory the
    # probe found that the model should have retrieved. The event_id
    # is the per-event ULID stamped by `search_miss_fields` since T4;
    # legacy events written before that field existed degrade to
    # None and can't be ack-filtered (the bulk
    # `silent_miss_cutoff` hatch remains the only escape for those).
    # The query_preview is the short triage string consumers display
    # alongside the id in `recent_silent_misses`.
    silent_miss_events: list[tuple[datetime | None, str | None, str | None, str | None]]
    latest_miss_cutoff: datetime | None
    # Set of `event_id` values acknowledged via `memory_acknowledge_miss`
    # (the per-event escape hatch — T4). Silent-miss events with a
    # matching event_id drop out of both the rate and the unique-memory
    # rollup. Distinct from the bulk `silent_miss_cutoff` hatch: an
    # ack targets ONE event, the cutoff wipes everything before its
    # timestamp.
    acknowledged_miss_event_ids: set[str]
    resolution_events_by_id: dict[str, list[dict[str, Any]]]


def _parse_silent_miss_event(
    ev: dict[str, Any],
) -> tuple[datetime | None, str | None, str | None, str | None]:
    """Extract the (ts, top_hit_id, event_id, query_preview) tuple from a
    ``search_miss`` event, defensive against malformed shapes.

    Both ``_StatsAccumulator._handle_search_miss`` and ``curation_counts``
    build this exact tuple and MUST stay numerically in lockstep (pinned
    by ``test_curation_counts_matches_compute_health_buckets``). Sharing
    one parser makes that agreement structural rather than hand-mirrored.

    Malformed events (missing / non-list ``top_hits``, non-dict first
    entry, non-string ``id``) degrade ``top_hit_id`` to None — the event
    still counts toward the miss total, it just can't contribute to the
    unique-memory dedup or be tombstone-filtered. ``event_id`` is the
    per-event ULID (T4); legacy events without it read as None. The
    recorder redacts ``probe_query`` into a ``{hash, preview, len}`` dict
    (events.py ``_redact_event_fields``), so prefer the redacted preview,
    falling back to a raw string for tests / verbatim-mode events.
    """
    top_hit_id: str | None = None
    top_hits = ev.get("top_hits")
    if isinstance(top_hits, list) and top_hits:
        first = top_hits[0]
        if isinstance(first, dict):
            candidate = first.get("id")
            if isinstance(candidate, str):
                top_hit_id = candidate
    event_id_raw = ev.get("event_id")
    event_id = event_id_raw if isinstance(event_id_raw, str) else None
    query_preview: str | None = None
    probe_query = ev.get("probe_query")
    if isinstance(probe_query, dict):
        preview_raw = probe_query.get("preview")
        if isinstance(preview_raw, str):
            query_preview = preview_raw
    elif isinstance(probe_query, str):
        query_preview = probe_query[:32]
    return (
        ensure_utc(parse_event_ts(ev.get("ts"))),
        top_hit_id,
        event_id,
        query_preview,
    )


class _StatsAccumulator:
    """Walk an event stream once and accumulate every per-event-kind
    counter ``compute_health`` needs.

    Pre-Round-2 ``compute_health`` carried a 130-line ``for ev in
    events:`` loop with a long ``elif kind == "..."`` chain. The new
    shape: one method per event kind (``_handle_search`` /
    ``_handle_use`` / etc.) and a single dispatch method
    (``handle_event``) that routes by kind. The MemoryStats / Counter
    state is held on the accumulator; the post-stream rollup
    (``rollups()``) freezes it into an ``_AccumulatorRollups``
    dataclass so the orchestrator can read clean.

    Why a class rather than free functions: the per-handler state is
    shared (a `use` event mutates `MemoryStats`; a `silent_miss_cutoff`
    might invalidate buffered audit ts), so passing the dicts around
    as kwargs would just push the state into closures. The class is
    the cleaner pattern.

    Not exported. Tests still verify the rollups via the public
    ``compute_health`` surface.
    """

    def __init__(
        self,
        *,
        by_id: dict[str, MemoryStats],
        tombstoned_ids: set[str],
    ) -> None:
        self._by_id = by_id
        self._tombstoned_ids = tombstoned_ids
        # Marker stats are accumulated by canonical marker name. Both
        # `markers` (transient_warning fires) and `markers_acknowledged`
        # (committed-with-override) feed in.
        self._marker_fires: Counter[str] = Counter()
        self._marker_overrides: Counter[str] = Counter()
        self._sessions: set[str] = set()
        self._total_events = 0
        self._orphan_use_events = 0
        # Audit telemetry is buffered as timestamps and resolved after
        # the events pass so a `silent_miss_cutoff` event later in the
        # log can retroactively drop events before its `cutoff_ts` —
        # the post-fix rollup hatch documented at the `_handle_search_miss`
        # branch.
        self._silent_miss_audited_ts: list[datetime | None] = []
        # Each miss event contributes
        # `(ts, top_hit_id_or_None, event_id_or_None, query_preview_or_None)`.
        # `top_hit_id` is the first id in the event's `top_hits`
        # payload — present on every `search_miss` written via
        # `search_miss_fields`, defensively None on malformed legacy
        # events that lack the field entirely (the older `compute_health`
        # rollup didn't read top_hits, so those events shipped without
        # them; we accept the None and fall back to counting-only behavior
        # so the rollup degrades cleanly rather than crashing).
        # `event_id` is the per-event ULID stamped on every miss
        # written since T4 (Unreleased) — references the original
        # event from a `miss_ack` so a `memory_acknowledge_miss` call
        # can resolve one specific false positive without wiping the
        # whole pre-cutoff window. Legacy events lack it (None) and
        # cannot be ack-filtered.
        # `query_preview` is the redacted-shape preview string the
        # `recent_silent_misses` surface displays for triage.
        self._silent_miss_events: list[
            tuple[datetime | None, str | None, str | None, str | None]
        ] = []
        self._latest_miss_cutoff: datetime | None = None
        # `miss_ack` events captured during the same single pass over
        # the event stream. The set carries the original `event_id`
        # that each ack referenced. Resolved against
        # `_silent_miss_events` after the pass to drop acknowledged
        # misses from the rollup. Idempotent: duplicate acks for the
        # same `event_id` collapse to one set entry (the handler also
        # short-circuits a second ack, but the rollup tolerates the
        # legacy case where two ack events exist in the log).
        self._acknowledged_miss_event_ids: set[str] = set()
        # Per-id chronological log of resolution-relevant events
        # (update / verify / use[contradicted|corrected]). Accumulated
        # for every memory while we walk the event stream once;
        # attached only to rows that end up in the contradicted bucket
        # (so we don't bloat the output for rows that have nothing
        # interesting to say). Cheaper than re-iterating the events
        # twice and bounded by the per-memory event count, which is
        # small in practice.
        self._resolution_events_by_id: dict[str, list[dict[str, Any]]] = {
            mid: [] for mid in by_id
        }

    # ---- dispatch -------------------------------------------------------

    def handle_event(self, ev: dict[str, Any]) -> None:
        """Route one event to its per-kind handler. Always bumps the
        total-events counter and the per-session set, regardless of
        kind — those rollups are kind-agnostic."""
        self._total_events += 1
        # Canonical-first session read with the legacy fallback the
        # other event consumers use. The Recorder stamps `session` on
        # every canonical-emitted event, but `turn_audited` /
        # `search_miss` use `session_id` as their canonical field —
        # without the fallback, those event kinds were silently
        # dropped from the distinct-session rollup.
        sess = ev.get("session") or ev.get("session_id")
        if sess:
            self._sessions.add(sess)

        kind = ev.get("kind")
        handler = self._HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is not None:
            handler(self, ev)

    # ---- per-event handlers --------------------------------------------

    def _handle_search(self, ev: dict[str, Any]) -> None:
        # Canonical-first read with the legacy-name fallback the other
        # event consumers use (consolidate / hook / _handlers /
        # _response) — keeps the health rollups consistent if an
        # event carries the older `memory_ids` / `hit_ids` spelling.
        for mid in (
            ev.get("returned") or ev.get("memory_ids") or ev.get("hit_ids") or []
        ):
            stats = self._by_id.get(mid)
            if stats:
                stats.retrieval_count += 1

    def _handle_show(self, ev: dict[str, Any]) -> None:
        stats = self._by_id.get(ev.get("id", ""))
        if stats:
            stats.show_count += 1

    def _handle_use(self, ev: dict[str, Any]) -> None:
        outcome = ev.get("outcome")
        ts = _ensure_utc(parse_event_ts(ev.get("ts")))
        for mid in ev.get("ids") or ev.get("memory_ids") or []:
            stats = self._by_id.get(mid)
            if stats is None:
                # Memory may have been tombstoned after the use was
                # recorded (a benign lifecycle event — the memory
                # existed when used) or the writer may have fabricated
                # the ULID (the concerning case). We discriminate by
                # checking the tombstone set: tombstoned-id references
                # are filtered out so `orphan_use_events` is a clean
                # smoke test for "model is hallucinating ids". Older
                # callers that don't pass `tombstoned_ids` see the
                # legacy conflated count (every unknown id is an
                # orphan), which preserves backward compatibility.
                if mid not in self._tombstoned_ids:
                    self._orphan_use_events += 1
                continue
            if outcome == "applied":
                stats.applied_count += 1
                # Split on the `auto` discriminator the recorder
                # stamps in `_advance_turn`. A missing or non-True
                # value reads as explicit so legacy events written
                # before the auto-commit pass existed don't get
                # silently relabelled. The two-axis split is the
                # endorsement signal: high `auto_applied_count` with
                # zero explicit means the ranker keeps surfacing the
                # memory but the model never deliberately reaches.
                if ev.get("auto") is True:
                    stats.auto_applied_count += 1
                else:
                    stats.explicit_applied_count += 1
            elif outcome == "ignored":
                stats.ignored_count += 1
            elif outcome == "contradicted":
                stats.contradicted_count += 1
                if ts is not None and (
                    stats.last_contradicted_at is None
                    or ts > stats.last_contradicted_at
                ):
                    stats.last_contradicted_at = ts
                self._append_resolution(
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
                self._append_resolution(mid, "corrected", ev.get("ts"), ev.get("note"))
            if ts is not None and (
                stats.last_used_at is None or ts > stats.last_used_at
            ):
                stats.last_used_at = ts

    def _handle_update(self, ev: dict[str, Any]) -> None:
        mid = ev.get("id", "")
        if isinstance(mid, str) and mid:
            self._append_resolution(mid, "update", ev.get("ts"), ev.get("note"))

    def _handle_verify(self, ev: dict[str, Any]) -> None:
        mid = ev.get("id", "")
        if isinstance(mid, str) and mid:
            self._append_resolution(mid, "verify", ev.get("ts"), ev.get("note"))

    def _handle_write(self, ev: dict[str, Any]) -> None:
        for marker in ev.get("markers", []) or []:
            self._marker_fires[marker] += 1
        for marker in ev.get("markers_acknowledged", []) or []:
            self._marker_overrides[marker] += 1

    def _handle_turn_audited(self, ev: dict[str, Any]) -> None:
        # Denominator for the silent-miss rate. Buffered with the
        # event ts so a later `silent_miss_cutoff` can retroactively
        # drop pre-cutoff audits — keeping just the numerator filtered
        # would skew the rate (low miss / high audited).
        self._silent_miss_audited_ts.append(_ensure_utc(parse_event_ts(ev.get("ts"))))

    def _handle_search_miss(self, ev: dict[str, Any]) -> None:
        # Numerator. A separate kind from `turn_audited` (rather than
        # a field on it) so consumers that only care about misses can
        # filter the log on a single `kind=` value, and so the
        # `silent_miss_cutoff` hatch can target one kind cleanly
        # without rewriting the events log.
        #
        # The (ts, top_hit_id, event_id, query_preview) tuple is built by
        # the shared `_parse_silent_miss_event` so this accumulator and
        # `curation_counts` cannot drift apart (their agreement is pinned
        # by test_curation_counts_matches_compute_health_buckets).
        self._silent_miss_events.append(_parse_silent_miss_event(ev))

    def _handle_miss_ack(self, ev: dict[str, Any]) -> None:
        # Per-event escape hatch for silent_miss false positives — T4.
        # The handler `memory_acknowledge_miss` emits one `miss_ack`
        # event per acknowledgment; the rollup collects the referenced
        # `event_id` values and drops matching silent_miss events
        # from BOTH the count and the unique-memory dedup. Distinct
        # from the bulk `silent_miss_cutoff` hatch: an ack targets
        # ONE event, the cutoff wipes everything before its ts.
        target = ev.get("event_id")
        if isinstance(target, str) and target:
            self._acknowledged_miss_event_ids.add(target)

    def _handle_silent_miss_cutoff(self, ev: dict[str, Any]) -> None:
        # Additive escape hatch: when a fix lands that invalidates a
        # batch of historical misses (e.g. v2.7.3 cwd-suppression),
        # `bettermemory consolidate --acknowledge-misses-before <ts>`
        # writes one of these and the rollup honors the latest
        # `cutoff_ts` seen, dropping any earlier turn_audited /
        # search_miss events. Older `cutoff_ts` values are ignored so
        # a later cutoff can extend the window but not shrink it.
        # `_ensure_utc` after parsing so a naive cutoff_ts compares
        # cleanly against the aware event ts above (curation_counts
        # uses the same combination; keep them in sync so a naive
        # cutoff_ts can't produce divergent rollups across paths).
        parsed_cutoff = _ensure_utc(parse_event_ts(ev.get("cutoff_ts")))
        if parsed_cutoff is not None and (
            self._latest_miss_cutoff is None or parsed_cutoff > self._latest_miss_cutoff
        ):
            self._latest_miss_cutoff = parsed_cutoff

    # ---- helpers --------------------------------------------------------

    def _append_resolution(self, mid: str, kind: str, ts_str: Any, note: Any) -> None:
        # Defensive against malformed events: a missing or non-string
        # timestamp would still be useful in the timeline (the kind
        # alone tells you something happened), but we render it as
        # None so the consumer can skip unsorted entries cleanly.
        bucket = self._resolution_events_by_id.get(mid)
        if bucket is None:
            return
        bucket.append(
            {
                "kind": kind,
                "ts": ts_str if isinstance(ts_str, str) else None,
                "note": note if isinstance(note, str) else None,
            }
        )

    def rollups(self) -> _AccumulatorRollups:
        """Freeze the accumulated counters into a flat record."""
        return _AccumulatorRollups(
            marker_fires=self._marker_fires,
            marker_overrides=self._marker_overrides,
            sessions=self._sessions,
            total_events=self._total_events,
            orphan_use_events=self._orphan_use_events,
            silent_miss_audited_ts=self._silent_miss_audited_ts,
            silent_miss_events=self._silent_miss_events,
            latest_miss_cutoff=self._latest_miss_cutoff,
            acknowledged_miss_event_ids=self._acknowledged_miss_event_ids,
            resolution_events_by_id=self._resolution_events_by_id,
        )

    # Class-level dispatch table. Defined after the methods so the
    # references resolve; declared on the class so the lookup is
    # built once per process rather than per `handle_event` call.
    _HANDLERS: dict[str, Callable[["_StatsAccumulator", dict[str, Any]], None]] = {
        "search": _handle_search,
        "show": _handle_show,
        "use": _handle_use,
        "update": _handle_update,
        "verify": _handle_verify,
        "write": _handle_write,
        "turn_audited": _handle_turn_audited,
        "search_miss": _handle_search_miss,
        "silent_miss_cutoff": _handle_silent_miss_cutoff,
        "miss_ack": _handle_miss_ack,
    }


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
    tombstoned_ids = tombstoned_ids or set()

    by_id: dict[str, MemoryStats] = {}
    # Parallel mappings of memory id -> origin.repo and id -> verified_paths,
    # kept separately so we don't have to add fields to MemoryStats just for
    # the commit-drift rollup. Captured during the same pass that builds
    # `by_id` because `memories` is an Iterable and we don't want to assume
    # re-iterability. `verified_paths_by_id` lets the rollup narrow drift to
    # commits that touched attested paths, matching memory_show /
    # memory_search (see _compute_commit_drift_debt).
    origin_repo_by_id: dict[str, str | None] = {}
    verified_paths_by_id: dict[str, list[str]] = {}
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
        verified_paths_by_id[m.id] = list(m.verified_paths)

    accumulator = _StatsAccumulator(by_id=by_id, tombstoned_ids=tombstoned_ids)
    for ev in events:
        accumulator.handle_event(ev)
    rollups = accumulator.rollups()

    marker_stats = [
        MarkerStats(
            marker=m,
            fire_count=rollups.marker_fires[m],
            override_count=rollups.marker_overrides[m],
        )
        for m in sorted(set(rollups.marker_fires) | set(rollups.marker_overrides))
    ]
    marker_stats.sort(key=lambda s: s.total, reverse=True)

    # Re-bind the per-event accumulator-derived names back into the
    # local scope so the remaining rollup logic (dead_weight, cold,
    # contradicted, etc.) reads with the pre-refactor variable names.
    # No behavior change — every name maps 1:1 to the accumulator's
    # corresponding field.
    total_events = rollups.total_events
    orphan_use_events = rollups.orphan_use_events
    silent_miss_audited_ts = rollups.silent_miss_audited_ts
    silent_miss_events = rollups.silent_miss_events
    latest_miss_cutoff = rollups.latest_miss_cutoff
    acknowledged_miss_event_ids = rollups.acknowledged_miss_event_ids
    resolution_events_by_id = rollups.resolution_events_by_id
    sessions = rollups.sessions

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
        verified_paths_by_id=verified_paths_by_id,
        caller_origin=caller_origin,
    )

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
    endorsement_candidates = [
        s
        for s in by_id.values()
        if s.category != Category.AMBIENT
        and s.retrieval_count >= endorsement_floor
        and _is_weakly_endorsed(s, ratio_threshold)
    ]
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

    report = HealthReport(
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
        silent_misses=_silent_miss_stats(
            audited_ts=silent_miss_audited_ts,
            miss_events=silent_miss_events,
            cutoff=latest_miss_cutoff,
            tombstoned_ids=tombstoned_ids,
            acknowledged_event_ids=acknowledged_miss_event_ids,
        ),
        recent_silent_misses=_build_recent_silent_misses(
            silent_miss_events,
            cutoff=latest_miss_cutoff,
            tombstoned_ids=tombstoned_ids,
            acknowledged_event_ids=acknowledged_miss_event_ids,
        ),
        cold_endorsement_memories=cold_endorsement_memories,
    )
    report.recommendations = _compute_recommendations(report)
    return report


def _compute_recommendations(report: "HealthReport") -> list["Recommendation"]:
    """Distill the bucket rollups into proactive curation suggestions.

    Order is fixed (matches `RECOMMENDATION_KINDS`) so the first
    actionable item is the one that most likely warrants attention.
    Size-driven kinds (dead_weight, cold_endorsement_memories,
    drifted) only fire when the bucket crosses
    `_RECOMMENDATION_SIZE_FLOOR`; per-row kinds (contradicted,
    rare_scopes) fire on first occurrence because each instance is
    independently actionable.
    """
    out: list[Recommendation] = []

    if len(report.dead_weight) >= _RECOMMENDATION_SIZE_FLOOR:
        out.append(
            Recommendation(
                kind="remove_dead_weight",
                summary=(
                    f"{len(report.dead_weight)} memories are retrieved but "
                    "never applied — the ranker keeps surfacing them but "
                    "they don't shape replies."
                ),
                action=(
                    "memory_remove(id, reason=...) on the unhelpful ones, "
                    "or `bettermemory consolidate --acknowledge-debt` to "
                    "clear the signal without touching bodies if the "
                    "memories are still valuable."
                ),
                count=len(report.dead_weight),
                memory_ids=[s.id for s in report.dead_weight[:_RECOMMENDATION_ROW_CAP]],
            )
        )

    if report.contradicted:
        out.append(
            Recommendation(
                kind="resolve_contradicted",
                summary=(
                    f"{len(report.contradicted)} memories carry an "
                    "unresolved contradiction — recorded as `contradicted` "
                    "and not since updated or re-verified."
                ),
                action=(
                    "memory_update(id, content=...) with the corrected "
                    "fact, then memory_verify(id, verified_paths=...) to "
                    "clear the flag."
                ),
                count=len(report.contradicted),
                memory_ids=[
                    s.id for s in report.contradicted[:_RECOMMENDATION_ROW_CAP]
                ],
            )
        )

    if report.cold_endorsement_memories.total >= _RECOMMENDATION_SIZE_FLOOR:
        out.append(
            Recommendation(
                kind="cleanup_cold_endorsements",
                summary=(
                    f"{report.cold_endorsement_memories.total} memories "
                    f"crossed the retrieval floor "
                    f"({report.cold_endorsement_memories.min_retrievals}+ "
                    "retrievals) but were never explicitly endorsed — the "
                    "auto-applied pass has been closing the loop without "
                    "the model deliberately reaching for them."
                ),
                action=(
                    "`bettermemory consolidate --acknowledge-debt` to clear "
                    "the signal once you're sure the memories are useful; "
                    "memory_remove on the ones that aren't."
                ),
                count=report.cold_endorsement_memories.total,
                memory_ids=[
                    s.id
                    for s in report.cold_endorsement_memories.rows[
                        :_RECOMMENDATION_ROW_CAP
                    ]
                ],
            )
        )

    if (
        report.commit_drift_debt is not None
        and report.commit_drift_debt.total_drifted >= _RECOMMENDATION_SIZE_FLOOR
    ):
        out.append(
            Recommendation(
                kind="verify_drifted",
                summary=(
                    f"{report.commit_drift_debt.total_drifted} memories "
                    "anchored in this repo are behind HEAD — verified "
                    "before recent commits landed."
                ),
                action=(
                    "memory_verify(id, verified_commits=[...]) to re-anchor "
                    "if claims still hold; memory_update where the body "
                    "needs to track the new code."
                ),
                count=report.commit_drift_debt.total_drifted,
                memory_ids=[
                    r.id
                    for r in report.commit_drift_debt.rows[:_RECOMMENDATION_ROW_CAP]
                ],
            )
        )

    if report.rare_scopes:
        # One recommendation per typo singleton — each carries its own
        # candidate fix surfaced via the scope name. The model reads
        # the list and picks the rename target.
        out.append(
            Recommendation(
                kind="fix_typo_scopes",
                summary=(
                    f"{len(report.rare_scopes)} singleton scopes look like "
                    "typos of more common scopes."
                ),
                action=(
                    "memory_rename_scope(old, new) to fold each singleton "
                    "into the intended scope name."
                ),
                count=len(report.rare_scopes),
                memory_ids=[],
                scope=", ".join(report.rare_scopes[:_RECOMMENDATION_ROW_CAP]),
            )
        )

    return out


def _compute_commit_drift_debt(
    *,
    by_id: dict[str, MemoryStats],
    origin_repo_by_id: dict[str, str | None],
    verified_paths_by_id: dict[str, list[str]],
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
        # If the memory carries verified_paths, narrow to commits that
        # actually touched those paths — mirrors memory_show and the
        # memory_search top-hit surface (_response.py). Without this the
        # rollup nagged on memories the user deliberately attested as
        # stable and disagreed with the per-hit verdict. Falls back to the
        # unfiltered count when the filter can't run (git unreachable, all
        # paths outside the repo). Guarded on count > 0 so a caught-up
        # memory never pays the extra git call.
        vpaths = verified_paths_by_id.get(stats.id) or []
        if vpaths and count > 0:
            filtered = commits_since_touching_paths(
                Path(caller_origin.cwd), since, vpaths
            )
            if filtered is not None:
                count = filtered
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
        # Surface the auto/explicit split so a curator can spot
        # weakly-endorsed memories at a glance: applied=N (auto=X exp=Y)
        # where exp=0 with non-zero auto is the "ranker keeps surfacing
        # this but the model never deliberately endorses it" pattern.
        lines.append(
            f"  {s.id} [applied={s.applied_count} "
            f"(auto={s.auto_applied_count} "
            f"exp={s.explicit_applied_count})] "
            f"{','.join(s.scopes)}: {s.summary}"
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

    ce = report.cold_endorsement_memories
    if ce.total > 0:
        lines.append("")
        lines.append(
            f"Cold-endorsement memories ({ce.total}) — retrieved >= "
            f"{ce.min_retrievals} times, never explicitly applied "
            "(model never deliberately endorsed the memory; every "
            "applied event came from the auto-commit pass):"
        )
        for s in ce.rows:
            lines.append(
                f"  {s.id} [retrievals={s.retrieval_count} "
                f"auto_applied={s.auto_applied_count}] "
                f"{','.join(s.scopes)}: {s.summary}"
            )
        if ce.total > len(ce.rows):
            lines.append(f"  ... and {ce.total - len(ce.rows)} more")

    sm = report.silent_misses
    if sm.audited_total > 0 or sm.miss_total > 0:
        lines.append("")
        lines.append(
            f"Silent misses — audited={sm.audited_total}  "
            f"miss={sm.miss_total}  "
            f"unique_memories={sm.unique_miss_memories}  "
            f"(emit via memory_audit_turn from a client-side end-of-turn hook):"
        )
        if sm.miss_total == 0:
            lines.append("  (none — audit ran and found no misses)")
        else:
            miss_rate_pct: float | None = (
                round(sm.miss_total / sm.audited_total * 100, 1)
                if sm.audited_total > 0
                else None
            )
            rate_str = f"{miss_rate_pct}%" if miss_rate_pct is not None else "?"
            lines.append(
                f"  {sm.miss_total} of {sm.audited_total} audited turns "
                f"flagged a miss (rate={rate_str}) "
                f"across {sm.unique_miss_memories} distinct memor"
                f"{'y' if sm.unique_miss_memories == 1 else 'ies'}"
            )

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
    return isoformat_utc_optional(dt)


def _count_post_cutoff(
    timestamps: list[datetime | None], cutoff: datetime | None
) -> int:
    """Count timestamps that fall at or after a cutoff.

    When `cutoff` is None, returns the full count — preserving the
    pre-cutoff-event rollup behavior for stores that have never run
    `consolidate --acknowledge-misses-before`. When `cutoff` is set,
    events with a missing or unparseable timestamp are dropped on the
    conservative interpretation that we cannot prove they post-date
    the cutoff (a stamped Recorder always emits `ts`, so this only
    affects malformed legacy events).
    """
    if cutoff is None:
        return len(timestamps)
    return sum(1 for ts in timestamps if ts is not None and ts >= cutoff)


def _silent_miss_stats(
    *,
    audited_ts: list[datetime | None],
    miss_events: list[tuple[datetime | None, str | None, str | None, str | None]],
    cutoff: datetime | None,
    tombstoned_ids: set[str],
    acknowledged_event_ids: set[str] | None = None,
) -> SilentMissStats:
    """Fold the buffered audit telemetry into a `SilentMissStats`.

    Four filters compose in order:

    1. **Cutoff** — events whose ts predates the latest
       `silent_miss_cutoff` are dropped (the additive escape hatch
       documented at `_handle_silent_miss_cutoff`). Applied to both
       audited and miss events so the rate denominator stays consistent.
    2. **Tombstone** — miss events whose top-hit id is in
       `tombstoned_ids` are dropped: once a memory is gone, a miss
       against it is no longer actionable. Only applied to miss events
       — `turn_audited` carries no per-memory payload, and the
       denominator should reflect "audits the hook ran" regardless of
       whether their probe hits have since been tombstoned. Events with
       a None top-hit id (malformed legacy events without `top_hits`)
       fall through this filter on the conservative interpretation
       that we cannot prove the target was tombstoned.
    3. **Ack** — miss events whose `event_id` is in
       `acknowledged_event_ids` are dropped: the per-event escape
       hatch the T4 ``memory_acknowledge_miss`` handler writes. Like
       the tombstone filter this only applies to miss events; the
       denominator stays at "audits the hook ran" because the
       audit ITSELF wasn't a false positive — the audit ran, the
       probe found something, the model acknowledged the verdict.
       Events with a None event_id (legacy events written before T4
       added the field) cannot be acked and fall through this filter.
    4. **Dedup** — the survivors are folded into both `miss_total`
       (events count) and `unique_miss_memories` (set cardinality of
       top-hit ids; events with None ids contribute to the event
       count but not to the unique-memories count).
    """
    acknowledged_event_ids = acknowledged_event_ids or set()
    audited_total = _count_post_cutoff(audited_ts, cutoff)
    miss_total = 0
    unique_ids: set[str] = set()
    for ts, top_hit_id, event_id, _query_preview in miss_events:
        if cutoff is not None and (ts is None or ts < cutoff):
            continue
        if top_hit_id is not None and top_hit_id in tombstoned_ids:
            continue
        if event_id is not None and event_id in acknowledged_event_ids:
            continue
        miss_total += 1
        if top_hit_id is not None:
            unique_ids.add(top_hit_id)
    return SilentMissStats(
        audited_total=audited_total,
        miss_total=miss_total,
        unique_miss_memories=len(unique_ids),
    )


def _build_recent_silent_misses(
    miss_events: list[tuple[datetime | None, str | None, str | None, str | None]],
    *,
    cutoff: datetime | None,
    tombstoned_ids: set[str],
    acknowledged_event_ids: set[str],
    cap: int = _RECENT_SILENT_MISSES_CAP,
) -> list[RecentSilentMiss]:
    """Build the inline list of unacknowledged silent_miss events for
    triage in `HealthReport.recent_silent_misses`.

    Applies the same cutoff / tombstone / ack filters
    `_silent_miss_stats` uses so the inline list matches the rollup
    counts: a non-zero `miss_total` and an empty `recent_silent_misses`
    list shouldn't be possible unless every actionable miss is a
    legacy event without an `event_id` (i.e., un-ack-able). Sorted
    newest-first because the most recent events carry the most
    triage value; capped at `_RECENT_SILENT_MISSES_CAP` so the JSON
    stays compact. Events with None ts sort last (chronologically
    indeterminate) so they don't push genuine recent events out of
    the cap.
    """
    # Carry the parsed datetime alongside each row so we can sort on it
    # rather than on the rendered ISO string. `isoformat_utc` omits the
    # fractional-seconds component when microsecond == 0 ("…:09Z") but keeps
    # 6 digits otherwise ("…:09.500000Z"); a lexicographic string sort then
    # mis-orders two events in the SAME whole second when one has
    # microsecond 0 and the other doesn't (".'" < "Z"), and at the cap
    # boundary can evict the genuinely-newer event in favour of an older one.
    _floor = datetime.min.replace(tzinfo=timezone.utc)
    surviving: list[tuple[datetime, RecentSilentMiss]] = []
    for ts, top_hit_id, event_id, query_preview in miss_events:
        if cutoff is not None and (ts is None or ts < cutoff):
            continue
        if top_hit_id is not None and top_hit_id in tombstoned_ids:
            continue
        if event_id is not None and event_id in acknowledged_event_ids:
            continue
        surviving.append(
            (
                ts if ts is not None else _floor,
                RecentSilentMiss(
                    event_id=event_id,
                    top_hit_id=top_hit_id,
                    query_preview=query_preview,
                    ts=_iso(ts) if ts is not None else None,
                ),
            )
        )
    # Newest first by the underlying datetime; None-ts events sort to the
    # tail via the tz-aware floor (chronologically indeterminate).
    surviving.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _ts, row in surviving[:cap]]


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
    cold_endorsement_min_retrievals: int = _COLD_ENDORSEMENT_MIN_RETRIEVALS,
    cold_endorsement_ratio_threshold: float = 0.0,
    caller_origin: Origin | None = None,
    now: datetime | None = None,
    since: datetime | None = None,
    tombstoned_ids: set[str] | None = None,
) -> dict[str, int]:
    """Cheap summary of curation pressure.

    Returns
    ``{"stale", "never_verified", "drifted", "cold", "dead",
    "silent_misses", "unique_silent_miss_memories",
    "cold_endorsement_memories"}`` —
    integer counts only, no row materialisation. Used by
    `memory_scope_overview` so the model can see at a glance whether
    the store has anything worth a curation pass without paying the
    full `compute_health` cost (which materialises and sorts every
    bucket and walks the event log to build resolution timelines).
    `silent_misses` here is the *numerator* (miss_total) only — the
    rate denominator (audited_total) is available from
    `compute_health().silent_misses` when the consumer needs it.
    Session-start surfaces just the numerator because a non-zero
    count is the actionable signal; the audit-cadence denominator
    matters for tuning, not for "should I look at this now."
    `unique_silent_miss_memories` is the cardinality of the set of
    top-hit memory_ids on those events — distinguishes "9 events
    against 1 mis-tagged memory" from "9 events against 9 memories."
    `cold_endorsement_memories` is the count of distinct memories the
    ranker keeps surfacing (retrieval_count >= min) that the model
    never explicitly endorsed — per-memory, not per-turn or
    per-event. Same shape decision as silent_misses: surface the
    actionable count, defer the full bucket to compute_health.

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

    `tombstoned_ids`, when set, drops silent-miss events whose top-hit
    memory has been tombstoned. The miss is no longer actionable in
    that case (the memory can't be retrieved anymore), so leaving it
    in the rollup just inflates the count with stale signal. Both
    `silent_misses` and `unique_silent_miss_memories` honor the
    filter. The default (None / empty set) preserves the legacy
    "every miss counts" semantic for callers that haven't been
    updated.

    `since`, when set, switches the helper into *delta* mode:
    events older than `since` are skipped, and memories created
    before `since` are excluded from every state-derived bucket.
    The semantic shifts from "what's in the store today?" to "what
    has *newly* appeared since `since`?" — which is what
    `memory_scope_overview` uses to compute
    `curation_pending_new_since_last_session`. Retrieval counts in
    delta mode reflect only the post-`since` slice of the event log,
    so a memory written before `since` that has had no new
    retrievals will not light up `cold_endorsement_memories`. Drift
    detection follows the same "newly appeared" framing as the other
    state-derived buckets: the drift count is filtered to memories
    created after `since`, so an older row that drifted in the prior
    session won't double-surface in the next session's delta.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    verification_cutoff = now - timedelta(days=verification_stale_days)
    since_aware = _ensure_utc(since)
    tombstoned_ids_set = tombstoned_ids or set()

    # Re-iterate only once over `memories` — pull the slim bookkeeping
    # we need. In delta mode, `since` filters here so every downstream
    # rollup sees only the post-`since` slice of the store.
    mem_list: list[Memory] = []
    for m in memories:
        if since_aware is not None:
            created_aware = _ensure_utc(m.created)
            # Same `<=` boundary discipline as the event filter below:
            # a memory created at exactly `since` was created by the
            # prior session's last event (write events stamp creation
            # at the same ts they record), so it belongs to that
            # session, not the delta.
            if created_aware is None or created_aware <= since_aware:
                continue
        mem_list.append(m)

    retrieval_counts: dict[str, int] = {m.id: 0 for m in mem_list}
    applied_counts: dict[str, int] = {m.id: 0 for m in mem_list}
    # Tracks explicit-only applies for the cold_endorsement_memories
    # count. An auto-flagged applied event is the server closing the
    # loop, not the model endorsing — same discriminator
    # `_advance_turn`/`memory_record_use` use.
    explicit_applied_counts: dict[str, int] = {m.id: 0 for m in mem_list}
    # Per-miss-event tuples of
    # `(ts, top_hit_id, event_id, query_preview)`. The id is the first
    # entry in the event's `top_hits` payload (audit.py:418-427) —
    # needed for the tombstone filter and the unique-memories count.
    # The `event_id` was added in T4 so the ack-filter can drop
    # individually-acknowledged misses; legacy events without it read
    # as None and cannot be acked (the bulk `silent_miss_cutoff`
    # remains the only escape for those).
    # Mirrors the `compute_health` shape so the two paths stay in
    # numerical agreement.
    silent_miss_events_list: list[
        tuple[datetime | None, str | None, str | None, str | None]
    ] = []
    latest_miss_cutoff: datetime | None = None
    # Per-event acknowledgments — T4 escape hatch.
    # `miss_ack` events are global markers like `silent_miss_cutoff`:
    # an ack written long ago still applies to any matching miss
    # carrying its `event_id`, even in delta mode where the ack event
    # itself falls outside the `--since` window. Without the
    # delta-exemption a session-start scope_overview run could
    # over-count freshly-emitted misses against an ack the user
    # already recorded.
    acknowledged_event_ids: set[str] = set()
    for ev in events:
        kind = ev.get("kind")
        # `silent_miss_cutoff` is a global marker — once written it
        # applies to the entire silent_miss rollup regardless of
        # window. Resolve it BEFORE the `--since` filter so a cutoff
        # event whose own `ts` falls under the delta boundary still
        # masks pre-cutoff misses correctly. Without this exemption a
        # `--since` delta would silently drop the cutoff and the
        # numerator would over-count.
        if kind == "silent_miss_cutoff":
            parsed = _ensure_utc(_parse_event_ts(ev.get("cutoff_ts")))
            if parsed is not None and (
                latest_miss_cutoff is None or parsed > latest_miss_cutoff
            ):
                latest_miss_cutoff = parsed
            continue
        if kind == "miss_ack":
            # Same global-marker treatment as `silent_miss_cutoff`:
            # an ack remains valid regardless of when it was written.
            target = ev.get("event_id")
            if isinstance(target, str) and target:
                acknowledged_event_ids.add(target)
            continue
        if since_aware is not None:
            ev_ts = _ensure_utc(_parse_event_ts(ev.get("ts")))
            # Strict `<=` rather than `<`: when `since` is a session
            # boundary from `find_prior_session_boundary`, the boundary
            # value IS the prior session's last event timestamp, so
            # that event belongs to the *prior* session and must not
            # leak into the delta. The handler treats `since` as
            # exclusive ("events strictly after the prior session").
            if ev_ts is None or ev_ts <= since_aware:
                continue
        if kind == "search":
            # Legacy-name fallback — see the note in `compute_health`.
            for mid in (
                ev.get("returned") or ev.get("memory_ids") or ev.get("hit_ids") or []
            ):
                if mid in retrieval_counts:
                    retrieval_counts[mid] += 1
        elif kind == "use" and ev.get("outcome") == "applied":
            is_auto = ev.get("auto") is True
            for mid in ev.get("ids") or ev.get("memory_ids") or []:
                if mid in applied_counts:
                    applied_counts[mid] += 1
                    if not is_auto and mid in explicit_applied_counts:
                        explicit_applied_counts[mid] += 1
        elif kind == "search_miss":
            # Shared parser with `_StatsAccumulator._handle_search_miss`
            # so the two silent-miss readers stay numerically in lockstep.
            silent_miss_events_list.append(_parse_silent_miss_event(ev))

    silent_miss_stats = _silent_miss_stats(
        audited_ts=[],  # curation_counts only surfaces the numerator
        miss_events=silent_miss_events_list,
        cutoff=latest_miss_cutoff,
        tombstoned_ids=tombstoned_ids_set,
        acknowledged_event_ids=acknowledged_event_ids,
    )
    silent_misses = silent_miss_stats.miss_total
    unique_silent_miss_memories = silent_miss_stats.unique_miss_memories

    never_verified = 0
    stale = 0
    cold = 0
    dead = 0
    cold_endorsement_memories = 0
    endorsement_floor = max(1, int(cold_endorsement_min_retrievals))
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
        # Cold-endorsement-memories count: heavily retrieved (over the
        # floor) AND weakly endorsed under the same predicate
        # compute_health uses. Default ratio_threshold=0.0 reduces to
        # the original binary "no explicit applied event ever" check;
        # setting ratio_threshold > 0 catches the "1 explicit out of
        # 50 auto" case. We don't apply the `created < cutoff` window
        # here because the retrieval floor itself is the "has had
        # time to accumulate signal" guard. Per-memory count: one
        # memory contributes one to the total even if hit hundreds
        # of times by the ranker.
        if not is_ambient and retrieval_counts.get(m.id, 0) >= endorsement_floor:
            explicit = explicit_applied_counts.get(m.id, 0)
            total_applied = applied_counts.get(m.id, 0)
            ratio_threshold = max(0.0, float(cold_endorsement_ratio_threshold))
            # Gate on "at least one apply happened" — mirrors the
            # `applied_count == 0` guard in `_is_weakly_endorsed`. A
            # memory retrieved over the floor with zero applies is
            # dead_weight, not cold-endorsement (the bucket is the
            # complement of dead_weight: applies happened, but every
            # one was auto). Without this, a pure dead-weight row would
            # double-count here and in `dead`.
            if total_applied == 0:
                pass
            elif explicit == 0:
                cold_endorsement_memories += 1
            elif ratio_threshold > 0.0:
                ratio = explicit / total_applied
                if ratio < ratio_threshold:
                    cold_endorsement_memories += 1

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
                verified_at = _ensure_utc(m.last_verified_at)
                if verified_at is None:
                    continue
                idx = bisect.bisect_right(timestamps_sorted, verified_at)
                count = len(timestamps_sorted) - idx
                # Narrow to commits touching attested paths, matching
                # memory_show / memory_search / commit_drift_debt — a
                # memory whose verified_paths weren't touched isn't drifted.
                # Guarded on count > 0 so a caught-up memory pays no git call.
                vpaths = list(m.verified_paths)
                if vpaths and count > 0:
                    filtered = commits_since_touching_paths(
                        Path(caller_origin.cwd), verified_at, vpaths
                    )
                    if filtered is not None:
                        count = filtered
                if count > 0:
                    drifted += 1

    return {
        "stale": stale,
        "never_verified": never_verified,
        "drifted": drifted,
        "cold": cold,
        "dead": dead,
        "silent_misses": silent_misses,
        "unique_silent_miss_memories": unique_silent_miss_memories,
        "cold_endorsement_memories": cold_endorsement_memories,
    }


def find_prior_session_boundary(
    events: Iterable[dict[str, Any]],
    current_session_id: str | None,
) -> datetime | None:
    """Latest event timestamp belonging to a session other than the current one.

    Used by `memory_scope_overview` to compute the
    `curation_pending_new_since_last_session` delta. Returns
    ``None`` when the event log carries no events outside the current
    session — typical on a fresh install or the very first session
    after a memory directory was wiped. Callers treat ``None`` as
    "no prior session to delta against" and surface the delta dict
    as ``None`` rather than as the absolute counts, so the model can
    distinguish "nothing new" (delta is zero) from "no baseline
    available" (delta is None).

    Walks events forward and tracks the max ts among entries whose
    `session` (or legacy `session_id`) field differs from
    `current_session_id`. Both legacy and canonical event-shape
    field names are accepted to stay compatible with archives
    written before the field-name unification.
    """
    if not current_session_id:
        return None
    latest: datetime | None = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        session_id = ev.get("session") or ev.get("session_id")
        if not isinstance(session_id, str) or session_id == current_session_id:
            continue
        ts = _parse_event_ts(ev.get("ts"))
        if ts is None:
            continue
        ts = _ensure_utc(ts)
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


# `_parse_event_ts` and `_ensure_utc` are thin module-local aliases for
# the canonical helpers in `time_utils`. Kept as names because the rest
# of this module reads them as if they were local; the indirection
# centralises the parse / tz-stamp semantics without re-routing every
# call site through `time_utils.*`.
_parse_event_ts = parse_event_ts
_ensure_utc = ensure_utc


def report_for_directory(
    root: Path,
    *,
    window_days: int = 30,
    heavily_used_top_k: int = 10,
    heavily_used_min_applied: int = 3,
    verification_stale_days: int = 30,
    cold_endorsement_min_retrievals: int = _COLD_ENDORSEMENT_MIN_RETRIEVALS,
    cold_endorsement_ratio_threshold: float = 0.0,
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
    tombstoned_ids = {t.id for t in store.load_tombstones()}
    return compute_health(
        store.load_all(),
        iter_all_events(root),
        window_days=window_days,
        heavily_used_top_k=heavily_used_top_k,
        heavily_used_min_applied=heavily_used_min_applied,
        verification_stale_days=verification_stale_days,
        cold_endorsement_min_retrievals=cold_endorsement_min_retrievals,
        cold_endorsement_ratio_threshold=cold_endorsement_ratio_threshold,
        caller_origin=caller_origin,
        now=now,
        tombstoned_ids=tombstoned_ids,
    )


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
    "VerificationDebt",
    "HealthReport",
    "compute_health",
    "curation_counts",
    "render_text",
    "render_json",
    "report_for_directory",
]
