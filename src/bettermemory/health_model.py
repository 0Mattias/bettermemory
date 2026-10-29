"""The report model of `health.py`: its dataclasses, caps and shared predicates.

`health.compute_health` builds a `HealthReport` from these types. The
shared predicates (`_is_dead_weight`, `is_hook_telemetry_event`,
`applied_tier`) are read by health's rollups, and the public ones by the
modules that import them through `health`. `health` re-exports every
public name here and every private one a caller outside the family
reads, so callers keep importing from `health`. Nothing here imports
`health`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import Category
from .store import EpisodeVolume
from .time_utils import isoformat_utc_optional
from .verify import BASIS_AUTHOR_DATE


# ---------------------------------------------------------------------------
# Shared dead-weight predicate
# ---------------------------------------------------------------------------
#
# One rule, three consumers: `compute_health`'s dead_weight bucket, the
# `curation_counts` "dead" rollup behind `memory_scope_overview`, and
# `consolidate.find_demotion_candidates` (the unattended fact->ambient
# retag). The demotion pass grew three conservative gates — the
# freshest-touch window, unresolved-contradiction parking, the
# endorsement grace — while the reporting rollups still keyed on
# `created` alone, so scope_overview kept reporting dead > 0 that the
# demotion pass (correctly) refused to drain. `_is_dead_weight` below
# is the single source of truth; the action side layers its fact/None
# category whitelist ON TOP (the report still surfaces e.g.
# user-inference dead rows — only the automated retag is
# category-restricted).

# Endorsement grace: the auto-applied endorsement structurally lags
# every retrieval by >= 2 memory-tool turns (session.py's use-token
# TTL), and dies with the server session — a retrieval in a session's
# final turns produces no `use(applied)` event at all until a later
# session re-retrieves. A memory's earliest timestamped retrieval must
# be at least this old before applied == 0 may count against it;
# otherwise the retrieval that proves the ranker works would be counted
# as evidence against the memory at the very Stop hook that fired it.
_ENDORSEMENT_GRACE_DAYS = 2


def _freshest_touch_ts(
    created: datetime,
    updated: datetime,
    last_verified_at: datetime | None,
    last_corroborated: datetime | None = None,
) -> float:
    """Epoch timestamp of the latest maintenance touch. A rewrite
    (`updated`), an attestation (`last_verified_at`), or a recurrence
    (`last_corroborated` — the claim re-entered a conversation and
    dedup credited it) is active life, not rot, so the dead-weight
    window keys on the most recent of the four rather than `created`
    alone."""
    ts = max(created.timestamp(), updated.timestamp())
    if last_verified_at is not None:
        ts = max(ts, last_verified_at.timestamp())
    if last_corroborated is not None:
        ts = max(ts, last_corroborated.timestamp())
    return ts


def _has_unresolved_contradiction(
    last_contradicted_at: datetime | None,
    updated: datetime,
    last_verified_at: datetime | None,
) -> bool:
    """True when the newest `use(contradicted)` event postdates both
    resolution paths (memory_update bumps `updated`; memory_verify
    bumps `last_verified_at`). The single implementation behind
    `MemoryStats.has_unresolved_contradiction`, `curation_counts`, and
    `consolidate.find_demotion_candidates` — see the property's
    docstring for the full resolution semantics."""
    if last_contradicted_at is None:
        return False
    last_resolved_at = updated
    if last_verified_at is not None and last_verified_at > last_resolved_at:
        last_resolved_at = last_verified_at
    return last_contradicted_at > last_resolved_at


def _is_dead_weight(
    *,
    category: Category | None,
    freshest_ts: float,
    retrieval_count: int,
    applied_count: int,
    has_unresolved_contradiction: bool,
    earliest_retrieval_ts: float | None,
    cutoff_ts: float,
    grace_cutoff_ts: float,
) -> bool:
    """The one dead-weight rule. All gates are conservative — each can
    only EXCLUDE a memory from the bucket:

    - ambient excluded (use signal structurally absent — implicit value);
    - latest maintenance touch (`_freshest_touch_ts`) before the window;
    - retrieved at least once (zero retrievals is `cold`, a different
      bucket asking a different curation question);
    - applied zero times;
    - no unresolved contradiction (parked for explicit resolution via
      memory_update/memory_verify, not lacking value);
    - earliest timestamped retrieval older than the endorsement grace
      (`earliest_retrieval_ts=None` — no timestamped retrieval —
      counts as old, so legacy logs stay eligible).
    """
    if category == Category.AMBIENT:
        return False
    if freshest_ts >= cutoff_ts:
        return False
    if retrieval_count == 0:
        return False
    if applied_count > 0:
        return False
    if has_unresolved_contradiction:
        return False
    if earliest_retrieval_ts is not None and earliest_retrieval_ts >= grace_cutoff_ts:
        # Every retrieval is younger than the endorsement grace — the
        # auto-applied commit window can't have elapsed yet, so
        # applied == 0 carries no signal.
        return False
    return True


# ---------------------------------------------------------------------------
# Shared telemetry-coverage predicate
# ---------------------------------------------------------------------------
#
# The dead-weight rule above reads `applied_count == 0` as evidence
# against a memory. That inference is only sound when something was in a
# position to record an apply. Settlement is overwhelmingly the Stop
# hook's job (`hook._emit_hook_attributions` — the containment pass plus
# its auto fallback); the in-process use-token path only settles a
# retrieval when the same session makes another `memory_*` call before
# the token expires. So on a store whose Stop hook was never wired,
# almost every retrieval ends with applied == 0 and the ENTIRE store
# reads as dead weight. That is a statement about the client's hook
# configuration, not about the memories — and on the unattended
# demotion path (`consolidate.find_demotion_candidates`) it retags them
# fact->ambient with nobody reviewing the diff.
#
# `is_hook_telemetry_event` is the one predicate answering "did
# hook-sourced settlement telemetry ever reach this event log?".
# `compute_health`, `curation_counts` and `find_demotion_candidates`
# each count it on their OWN existing event walk, so the three surfaces
# cannot disagree about coverage the way they once disagreed about dead
# weight — and the count costs no extra pass.
#
# Two deliberate exclusions:
#
# - `auto_consolidate` events (`consolidate.py`) also carry
#   `triggered_from="stop_hook"`, but that pass is opt-in-gated. Keying
#   coverage on them would make the signal depend on `[consolidate]`
#   config rather than on whether settlement runs. The `kind` gate below
#   already excludes them; the exclusion is named here so a future
#   widening to "anything stamped stop_hook" is a decision, not a slip.
# - `turn_audited` from `memory_audit_turn` carries
#   `triggered_from="mcp_tool"` (handlers/audit_turn.py), i.e. the model
#   calling the audit tool in-process. That is not evidence the hook is
#   wired, so the `stop_hook` check is load-bearing on that arm too.


def is_hook_telemetry_event(ev: dict[str, Any]) -> bool:
    """True when ``ev`` is Stop-hook-sourced settlement/audit telemetry.

    Two shapes, both written by `hook.run_audit`'s end-of-turn pass:

    - ``kind="use"`` carrying ``attribution="hook"`` (the containment
      matcher's explicit attribution) or ``triggered_from="stop_hook"``
      (which also covers the `attribution="auto"` fallback the same
      pass emits for the retrieved-but-unmatched remainder).
    - ``kind="turn_audited"`` with ``triggered_from="stop_hook"`` — the
      silent-miss probe. Counted because a wired hook that audits every
      turn IS covered telemetry even in a window where nothing was
      retrieved, so a quiet store doesn't read as unwired.

    A store with zero such events across its whole log has no settlement
    telemetry, which makes `applied_count == 0` uninformative — see the
    section comment above for what the callers do with that.
    """
    kind = ev.get("kind")
    if kind == "use":
        return (
            ev.get("attribution") == "hook" or ev.get("triggered_from") == "stop_hook"
        )
    if kind == "turn_audited":
        return ev.get("triggered_from") == "stop_hook"
    return False


def applied_tier(ev: dict[str, Any]) -> str:
    """Which surface settled this ``use(outcome="applied")`` event:
    ``"auto"``, ``"hook"`` or ``"model"``.

    THREE tiers, not two. The pre-3.32 split was binary — `auto is
    True` versus everything else — and the "everything else" bucket
    was named *explicit*, which read as "the model deliberately
    endorsed this". It isn't: the Stop hook's containment matcher
    emits `auto=False, attribution="hook"` on purpose (same shape an
    explicit call produces, so the pending-purge and the auto fallback
    treat them alike), so a phrase from the memory happening to appear
    in the reply landed in the same bucket as a real
    `memory_record_use(applied)` call.

    The classification, in order:

    - ``auto`` — strict ``auto is True``. Identity, not truthiness: a
      legacy ``auto=1`` / ``auto="true"`` reads as NON-auto so we never
      silently relabel borderline data as the server closing the loop.
      All four readers of this discriminator agree on the strict form.
    - ``hook`` — ``attribution == "hook"``, the containment matcher.
    - ``model`` — everything else, and deliberately a fall-through
      rather than an enumeration: a missing/None attribution (every
      pre-attribution event) and the CLI admin surfaces' own
      attribution values all land here. Admin-recorded rows ARE genuine
      endorsements (that is what `consolidate --acknowledge-debt` is
      for); what they must not do is masquerade as hook coverage. The
      fall-through is also the only shape this module is permitted:
      `eval.is_admin_recorded_event` owns the admin classification, and
      `tests/test_eval.py::TestAdminRecordedParity` fails any module in
      `src/` outside eval.py that re-spells half of it here.

    Shared with `eval.py` (which imports this) so the health rollup's
    per-memory counts and the published eval counters cannot derive the
    same three tiers two different ways — the drift class this repo
    keeps paying for.
    """
    if ev.get("auto") is True:
        return "auto"
    if ev.get("attribution") == "hook":
        return "hook"
    return "model"


def endorsement_tier(ev: dict[str, Any]) -> str | None:
    """The tier (`applied_tier`) a ``use`` event adds to its memories'
    per-memory applied tallies, or None when it adds none (another
    outcome, another kind).

    The one definition the cold-endorsement bucket is read under, in
    `health.compute_health` and `eval.compute_eval` alike: every applied
    use counts, and a shown pointer's settlement (`recall.is_settlement`,
    `recall.settle_shown`) is one. A settlement is the containment
    matcher finding the pointer's memory in the reply, the same ``hook``
    tier the matcher gives a retrieval it matched, so it is a recorded
    use of the memory and the memory is not "never endorsed". The two
    reports disagreed here: health counted the settlement and listed a
    memory retrieved, auto-applied and then settled 0 times, while eval
    skipped every settlement and listed it once. The eval's rate trio
    still leaves settlements out (its published series keeps one
    definition across the recall surface's arrival); that is a decision
    about the trio's counters, not about whether the memory was used."""
    if ev.get("kind") != "use" or ev.get("outcome") != "applied":
        return None
    return applied_tier(ev)


@dataclass
class TelemetryCoverage:
    """Whether the walked event log carries Stop-hook settlement telemetry.

    Attached to `HealthReport.telemetry_coverage` only when the caller
    asked for the honesty gate (see `compute_health`'s
    `hook_telemetry_events` parameter); `None` means "nobody measured",
    which is the pre-3.32 behaviour and what offline tooling and unit
    tests get by default.

    `dead_weight_suppressed` is the actionable bit: when it is True the
    `dead_weight` bucket is EMPTY BY CONSTRUCTION rather than because
    the store is clean, and `reason` says so in one line the model can
    surface verbatim. `cold_endorsement_suppressed` is the same
    statement about the `cold_endorsement_memories` bucket — both key
    on settlement counts an unwired hook cannot produce, so they gate
    together (the gate widened to the endorsement leg 2026-08-30).
    """

    hook_telemetry_events: int
    covered: bool
    dead_weight_suppressed: bool
    cold_endorsement_suppressed: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_telemetry_events": self.hook_telemetry_events,
            "covered": self.covered,
            "dead_weight_suppressed": self.dead_weight_suppressed,
            "cold_endorsement_suppressed": self.cold_endorsement_suppressed,
            "reason": self.reason,
        }


# The one sentence every surface uses when it suppresses a dead-weight
# verdict. Written once so the MCP report, the CLI rendering and the
# consolidate refusal cannot drift into three different explanations of
# the same silence.
#
# NOT "applied_count is structurally zero without the hook" — that
# overstates it, and this is a feature about honesty. The in-process
# auto-commit (`handlers/_shared._advance_turn`) does settle retrievals
# as `applied` on a hookless store, and `_is_dead_weight` keys on
# `applied_count`, which counts them. What it CANNOT do is settle a
# retrieval the session never comes back to — no later `memory_*` call,
# no settlement, ever. So the honest claim is the weaker one: with no
# hook the zero is uninformative, not guaranteed.
_HOOKLESS_REASON = (
    "no Stop-hook settlement telemetry in the event log — the only "
    "settlement left is the in-process auto-commit, which cannot settle "
    "a retrieval the session never returns to, so zero applied and zero "
    "explicit-applied counts do not distinguish an unhelpful memory "
    "from an unwired hook. Wire the Stop hook (the plugin's "
    "hooks.json carries the shape), then re-check."
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
    # this id — the count came entirely from automatic settlement (the
    # Stop hook's turn-end fallback or the in-process pass). A high
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
    # `explicit_applied_count` split by WHO produced the non-auto
    # apply. The two are disjoint and sum to it exactly (pinned by
    # test_health.py's conservation test), so every existing consumer
    # of `explicit_applied_count` keeps its exact meaning.
    #
    # Why the split: the Stop hook's containment matcher
    # (`attribution.attribute_uses`) emits `auto=False,
    # attribution="hook"` — deliberately the same shape an explicit
    # model call produces, so `_already_recorded_pending_ids` and the
    # auto fallback treat them alike. But a phrase from the memory
    # appearing in the reply is EVIDENCE the retrieval landed, not the
    # model deliberately endorsing the memory, and folding the two into
    # one number made `endorsement_ratio` unable to tell "the model
    # reached for this" from "some words overlapped".
    hook_applied_count: int = 0
    # The residual: an explicit apply that did NOT come from the hook's
    # containment pass — a real `memory_record_use(applied)` call.
    # `attribution=None` (every pre-attribution event) and the CLI
    # admin surfaces land here too; see `_applied_tier`.
    model_applied_count: int = 0
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
    # Recurrence rollup from the memory record (not the event log) —
    # feeds the freshest-touch window so a corroborated memory isn't
    # dead weight.
    last_corroborated: datetime | None = None
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
        return _has_unresolved_contradiction(
            self.last_contradicted_at, self.updated, self.last_verified_at
        )

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

    @property
    def model_endorsement_ratio(self) -> float | None:
        """`endorsement_ratio` with the hook's containment matches taken
        out of the numerator — the fraction of applies that came from
        the model actually calling `memory_record_use`.

        The published `endorsement_rate` (eval.py) and the
        `cold_endorsement_memories` bucket both key on the wider
        `explicit` numerator and keep doing so — they have recorded
        baselines, and the hook's attribution IS evidence the retrieval
        landed. This sibling exists because the wider ratio cannot
        answer "is the model deliberately reaching for this memory, or
        is a phrase merely overlapping?", and on a hook-wired store
        that is most of the endorsement signal. Same None-on-zero
        contract as `endorsement_ratio` so a consumer can hold the two
        side by side.
        """
        if self.applied_count == 0:
            return None
        return self.model_applied_count / self.applied_count

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
            # The explicit half, split by producer:
            # hook + model == explicit, always.
            "hook_applied_count": self.hook_applied_count,
            "model_applied_count": self.model_applied_count,
            "endorsement_ratio": self.endorsement_ratio,
            "model_endorsement_ratio": self.model_endorsement_ratio,
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
        """Overrides as a fraction of ALL events for this marker.

        Note the denominator: `total` is fires PLUS overrides, and a blocked
        write that the caller then re-issues with `acknowledge_transient`
        logs one of each. So a marker whose every block is answered scores
        0.500, not 1.000 — the practical ceiling is half, and a rate near it
        means near-total rubber-stamping rather than "about half the time".
        Read a headline figure against 0.500, not against 1.0; to recover
        the blocks-overridden fraction, compare `override_count` with
        `fire_count` directly.

        High value = caller routinely rubber-stamps `acknowledge_transient`,
        which is the signal that the marker is producing too many false
        positives. Trim it — `durability.SHA_MARKER` is the worked example
        of that trim, and of what its closed row looked like beforehand.
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
class ActorSlice:
    """One actor's footprint on the store — the 7.13.0-era read of the
    per-request caller `identity.py` has stamped on every record and
    event since 7.10.0.

    Keyed on `client`, because that is the axis a caller can act on:
    `memory_search(client=...)` / `memory_list(client=...)` select with
    it (7.12.0), and `identity.actor_matches` is the single definition
    both of those read. `client=None` is the `undeclared` bucket — see
    `ActorSlices.undeclared`, which is this same type.

    **Exactly one slice per memory.** Unlike `ScopeHealth`, whose counts
    deliberately over-sum (a memory carries many scopes and is counted
    under each), a memory carries exactly ONE actor, so the counts here
    reconcile exactly against the report's own totals. `ActorSlices`
    states both invariants and `compute_health` maintains them.

    `models` and `principals` are the CENSUS half: which spellings
    actually exist under this client, so a caller knows what it can
    pass to the `model` filter. Declared values only. An empty dict is
    a measurement — "this client declared none" — never a `<unset>`
    key, which would put a manufactured spelling into a census whose
    whole job is to report the real ones.
    """

    client: str | None
    # Store-wide, NOT windowed: this is the count that reconciles
    # against `total_active_memories`.
    memories: int = 0
    # The "what did this agent write this run" read. Memories whose
    # `created` falls inside `window_days`. Always <= `memories`.
    memories_in_window: int = 0
    events: int = 0
    searches: int = 0
    applies: int = 0
    models: dict[str, int] = field(default_factory=dict)
    principals: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "memories": self.memories,
            "memories_in_window": self.memories_in_window,
            "events": self.events,
            "searches": self.searches,
            "applies": self.applies,
            "models": dict(self.models),
            "principals": dict(self.principals),
        }


@dataclass
class ActorSlices:
    """Per-actor pivot on the store, plus the bucket for everything that
    declared no actor at all.

    `undeclared` is a FIRST-CLASS bucket and is always present, never an
    omission. On any store with history it is the majority: every record
    written before 7.10.0 carries no actor block, and a slice list that
    named only declared actors would describe a small fraction of the
    store while looking complete. That is the "could-not-ask
    manufactures a verdict" class this project publishes a third value
    for everywhere else, and the same rule applies to a census of who
    wrote what. Present at zero so "nobody undeclared" and "the field
    isn't there" cannot read the same.

    It is structurally the same `ActorSlice` type as a named actor, so
    the two invariants below have no special case:

    * `sum(s.memories for s in declared) + undeclared.memories ==
      total_active_memories`
    * `sum(s.events for s in declared) + undeclared.events ==
      total_events`

    A record whose actor block exists but names no `client` lands in
    `undeclared` too — it declared no value on the axis this pivot is
    keyed on — while its `model` / `principal` spellings still surface
    in that bucket's counters, so nothing it did declare is lost.
    """

    declared: list[ActorSlice] = field(default_factory=list)
    undeclared: ActorSlice = field(default_factory=lambda: ActorSlice(client=None))

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared": [s.to_dict() for s in self.declared],
            "undeclared": self.undeclared.to_dict(),
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
    # Checkability partition (2026-08-30). A debt row is *checkable*
    # when a verify pass has something mechanical to check: the memory
    # declares claims, or it carries drift anchors (body-cited paths
    # plus attested verified_paths — `commit_drift_anchor_paths`, the
    # same notion every drift surface reads). The remainder are
    # judgment records — preferences, directives, lessons — whose
    # verification is a re-read, not a tree check. Measured on the
    # dogfood store the day this landed, a large minority of debt was
    # structurally uncheckable, so an undivided total reads as backlog
    # a curate pass can never drain. The capped row lists sort
    # checkable-first for the same reason: the 20-row window should
    # show the rows a pass can actually act on.
    never_verified_checkable: int = 0
    stale_checkable: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stale_after_days": self.stale_after_days,
            "never_verified_total": self.never_verified_total,
            "never_verified_checkable": self.never_verified_checkable,
            "stale_total": self.stale_total,
            "stale_checkable": self.stale_checkable,
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
# rather than a real signal.
#
# Calibrated 2026-08-30 against the dogfood event log rather than by
# intuition. The pooled explicit-endorse rate there is p ≈ 0.130 (406
# non-auto applies over 3,119 search deliveries), so a perfectly
# healthy memory shows zero explicit applies after five retrievals
# with probability (1-p)^5 ≈ 0.50 — the previous floor of 5 flagged
# coin flips, and on the day it was measured every row the bucket held
# was demonstrably in active use. Thirty puts P(zero | healthy) at or
# under 0.05 for both that estimate (0.015) and the more conservative
# p ≈ 0.095 a same-week pass measured (0.048). The formula is the
# durable part — (1-p)^N against the store's own event log — and the
# constant is its snapshot; re-derive p before trusting the number on
# a store with a different settlement mix. Tunable inline on the
# compute_health call so tests can exercise the mechanics without
# forcing a config bump for the common case.
_COLD_ENDORSEMENT_MIN_RETRIEVALS = 30

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
    # The axis the count was measured on (`verify.CommitDriftStatus.basis`):
    # the reachable range from the stamp's recorded HEAD, or the
    # author-date count for a record with no usable anchor.
    basis: str = BASIS_AUTHOR_DATE

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopes": list(self.scopes),
            "summary": self.summary,
            "last_verified_at": (
                _iso(self.last_verified_at) if self.last_verified_at else None
            ),
            "commits_since_verify": self.commits_since_verify,
            "basis": self.basis,
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

    `unconfirmed` counts the homed candidates whose cited commit no live
    store here could resolve (design 3.6: not the home checkout's, which
    has no live view here, nor the caller's, which does not hold it), so
    their commits were not counted; present in the payload when non-zero.
    """

    current_repo: str | None
    current_cwd: str | None
    rows: list[CommitDriftRow] = field(default_factory=list)
    total_drifted: int = 0
    unconfirmed: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "current_repo": self.current_repo,
            "current_cwd": self.current_cwd,
            "total_drifted": self.total_drifted,
            "rows": [r.to_dict() for r in self.rows],
        }
        if self.unconfirmed:
            out["unconfirmed"] = self.unconfirmed
        return out


# Cap on distinct foreign worktrees a single health run walks git in.
# Each walked root costs one full `git log --format=%aI` plus the
# per-drifting-row narrowing calls, all under the drift legs' 5s
# ceiling — an unbounded estate would turn the deep report into a
# multi-second git crawl. Cheap skips (missing directory, moved repo)
# never consume the cap; groups past it are listed as skipped rather
# than silently dropped, so "covered everything" is never implied.
_CROSS_REPO_MAX_ROOTS = 8


@dataclass
class CrossRepoDriftGroup:
    """One foreign origin's drift rollup — `CommitDriftDebt`'s shape,
    anchored to the memory's recorded worktree instead of the caller's
    cwd. `candidates` counts the claim-anchored, verified memories the
    group was judged over, so "clean" is legible as "checked N, none
    drifted" rather than "nothing to check"."""

    repo: str
    worktree_root: str
    rows: list[CommitDriftRow] = field(default_factory=list)
    total_drifted: int = 0
    candidates: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "worktree_root": self.worktree_root,
            "total_drifted": self.total_drifted,
            "candidates": self.candidates,
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass
class CrossRepoDrift:
    """Commit drift for memories whose origin is NOT the caller's repo.

    Every other commit-drift surface is gated on the caller standing
    inside the memory's origin repo, so records anchored in other
    projects rot invisibly from here. This rollup resolves each foreign
    group's recorded `origin.worktree_root` on disk and runs the same
    claim-anchored legs there — see `_compute_cross_repo_drift` for the
    checks and the cap. Deep-report only by design: session-start's
    `curation_pending` stays caller-repo cheap.
    """

    groups: list[CrossRepoDriftGroup] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    total_drifted: int = 0
    # Verified memories this check could not judge because their origin
    # records no repo or no worktree_root, so there is nothing to resolve
    # on disk and no anchor to count commits against. They used to be
    # dropped by a bare `continue` BEFORE `skipped` was built, which made
    # a partial estate read as a complete one — the failure this class's
    # own docstring rules out ("checked N, clean" and "didn't check" must
    # not read the same). Not a per-group row: the whole point is that
    # they group into nothing. Origin-less records are a documented and
    # legitimate class (a write from outside any checkout), so this is a
    # coverage figure to state, not a defect to fix.
    unanchorable: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_drifted": self.total_drifted,
            "groups": [g.to_dict() for g in self.groups],
            "skipped": list(self.skipped),
            "unanchorable": self.unanchorable,
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
    the denominator — MISS-CAPABLE audits only, i.e. `turn_audited`
    events whose verdict is anything but ``"no_signal"`` (a missing or
    legacy verdict counts as miss-capable, the conservative read);
    `miss_total` is the numerator (audits that flagged a miss). A
    consumer can compute the miss *rate* with
    `miss_total / audited_total` when audited_total > 0; we don't ship
    the float here because rate-vs-count is a presentation choice and
    the raw counts are stable across consumers.

    `no_signal_total` (additive, round 88) counts the audits the probe
    declined to evaluate (`verdict == "no_signal"`). They used to land
    in `audited_total`, which made a deployment whose probe can NEVER
    measure a miss read as the healthy "audited heavily, model behaved"
    signature (audited climbing, misses pinned at 0). The worked
    example was a pre-4.0 `search_mode = "semantic"` install under the
    Stop hook, which hardcoded `semantic_model=None` and turned 100% of
    its audits into permanent no_signals; that configuration went out
    with the embedding lane, but the split it motivated stands — any
    future structurally-blind probe lands in the same bucket instead of
    diluting the rate denominator.

    Empty bucket (audited and miss both zero) means the audit hook
    either wasn't invoked in the window or every audit it fired stopped
    at the no-signal branch — `no_signal_total` distinguishes those two
    (zero vs. non-zero). The split-count shape is deliberate so a
    stalled hook ("nothing audited at all") and a structurally-blind
    probe ("every audit no_signal") don't look the same as a healthy
    run ("audited a lot, model behaved").

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
    no_signal_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "audited_total": self.audited_total,
            "miss_total": self.miss_total,
            "unique_miss_memories": self.unique_miss_memories,
            "no_signal_total": self.no_signal_total,
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
    "review_unaccounted",
    "fix_typo_scopes",
)


# Cap on the `unaccounted` rows `ProvenanceDebt` inlines. The total is
# uncapped; the rows are the triage window.
_PROVENANCE_ROW_CAP = 20


@dataclass
class ProvenanceRow:
    """One memory the provenance bucket surfaces: identity plus enough
    to decide without a `memory_show` whether the record is recognised."""

    id: str
    scopes: list[str]
    summary: str
    created: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopes": list(self.scopes),
            "summary": self.summary,
            "created": _iso(self.created),
        }


@dataclass
class ProvenanceDebt:
    """How the store's memories entered it, from the index's provenance
    column (schema v7; `provenance.py` carries the derivation).

    `counts` is the per-label census over every indexed row, with
    `unclassified` for rows a rebuild has not labelled yet.
    `unaccounted_total` is the one count that is a finding: the event
    log covers the memory's creation window, nothing wrote it, nothing
    pulled it. The `unaccounted` rows (capped at `_PROVENANCE_ROW_CAP`,
    newest first) are the triage window. Absent (None on the report)
    when the index is missing or unusable, so "no unaccounted memories"
    and "could not look" never read the same."""

    counts: dict[str, int]
    unaccounted_total: int
    unaccounted: list[ProvenanceRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "unaccounted_total": self.unaccounted_total,
            "unaccounted": [r.to_dict() for r in self.unaccounted],
        }


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
    # Per-actor pivot — "what did the fleet's seventh agent write this
    # run" — plus the census of which client / model / principal
    # spellings exist to pass to the 7.12.0 filters. Keyed on the
    # declared `client`, with an always-present `undeclared` bucket for
    # records and events whose writer named none (every memory written
    # before 7.10.0, and any client that names itself in no channel).
    # Counts reconcile EXACTLY against `total_active_memories` and
    # `total_events` — see `ActorSlices` for both invariants and why
    # this pivot can promise an exactness `scope_health` cannot.
    actor_slices: ActorSlices = field(default_factory=ActorSlices)
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
    cross_repo_drift: CrossRepoDrift | None = None
    # Provenance census plus the unaccounted rows, read off the index by
    # `report_for_directory` (the one entry point with a root; the pure
    # `compute_health` never sees the index). None when no index.
    provenance: ProvenanceDebt | None = None
    # Silent-miss telemetry — the false-negative half of opt-in
    # retrieval. `audited_total` and `miss_total` come from the
    # `turn_audited` and `search_miss` event kinds emitted by
    # memory_audit_turn. The pair is the denominator + numerator for the
    # miss rate; we keep them as raw counts so the consumer chooses how
    # to render. `audited_total` counts MISS-CAPABLE audits only;
    # `no_signal` verdicts land in the separate `no_signal_total` so a
    # probe that structurally can't measure (pre-4.0 semantic mode
    # under the Stop hook was the worked case) doesn't masquerade as
    # "audited heavily, no misses
    # found." Audited and miss both zero means the audit hook hasn't
    # fired in the window or every audit stopped at the no-signal
    # branch — `no_signal_total` tells those apart.
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
    # Honesty gate for `dead_weight` — see `TelemetryCoverage` and
    # `is_hook_telemetry_event`. Null when the caller didn't ask for the
    # measurement (`compute_health(hook_telemetry_events=None)`, the
    # default for offline tooling and unit fixtures); populated on every
    # production path. When `dead_weight_suppressed` is true the
    # `dead_weight` list above is empty BY CONSTRUCTION, and a consumer
    # that reports "no dead weight" without reading this field is
    # reporting a missing Stop hook as a clean store.
    telemetry_coverage: TelemetryCoverage | None = None
    # Episode-tier volume gauge — `{sessions, episodes, bytes,
    # prunable_sessions, ttl_days}` for `<root>/episodes/`. NOT a
    # curation bucket of memory rows and not a hole in the tier
    # separation: episode CONTENT still never reaches `memory_search`,
    # `memory_list` or any bucket above. This is the aggregate only.
    #
    # It is here because episode GC is write-triggered.
    # `EpisodeStore.prune_old_sessions` runs on `episode_write` and on
    # `bettermemory episodes prune`, nowhere else — so a read-only loop
    # (one that calls `episode_handoff` / `episode_search` and never
    # writes) never collects anything and the journal grows unbounded
    # with no surface reporting it. `prunable_sessions` is that missing
    # report; the rest is the denominator that makes it legible.
    #
    # Null when the report was built by `compute_health` directly — that
    # function takes memories + events and never sees `root`, so unit
    # fixtures and offline callers that hand in a list keep getting None.
    # Production paths all go through `report_for_directory`, which does
    # have a root; there it is null only when the episode subtree could
    # not be walked at all (see that function's OSError guard). Either
    # way None reads as "no measurement", never as "measured, empty" —
    # an empty subtree has its own reading, all zeroes.
    episode_volume: EpisodeVolume | None = None

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
            "actor_slices": self.actor_slices.to_dict(),
            "orphan_use_events": self.orphan_use_events,
            "verification_debt": self.verification_debt.to_dict(),
            "cross_repo_drift": (
                self.cross_repo_drift.to_dict()
                if self.cross_repo_drift is not None
                else None
            ),
            "commit_drift_debt": (
                self.commit_drift_debt.to_dict()
                if self.commit_drift_debt is not None
                else None
            ),
            "provenance": (
                self.provenance.to_dict() if self.provenance is not None else None
            ),
            "silent_misses": self.silent_misses.to_dict(),
            "recent_silent_misses": [m.to_dict() for m in self.recent_silent_misses],
            "cold_endorsement_memories": self.cold_endorsement_memories.to_dict(),
            "recommendations": [r.to_dict() for r in self.recommendations],
            "telemetry_coverage": (
                self.telemetry_coverage.to_dict()
                if self.telemetry_coverage is not None
                else None
            ),
            "episode_volume": (
                self.episode_volume.to_dict()
                if self.episode_volume is not None
                else None
            ),
        }


def _iso(dt: datetime | None) -> str | None:
    return isoformat_utc_optional(dt)
