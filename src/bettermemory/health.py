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
  (zero-retrieval rows land in `cold_memories` instead). Either the
  search ranking isn't surfacing them, or they're noise. Either way,
  prune candidates. The rule is the shared `_is_dead_weight` predicate
  — freshest-touch window, unresolved-contradiction parking, and the
  endorsement grace all exempt — which `curation_counts` and
  `consolidate.find_demotion_candidates` read too, so the reported
  signal and the unattended demotion action cannot diverge.
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
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .durability import canonical_marker
from .episodes import EpisodeStore, EpisodeVolume
from .events import _event_id_list, iter_all_events
from .models import Category, Memory, first_summary_line
from .origin import (
    Origin,
    capture,
    commit_author_timestamps,
    repo_toplevel,
    repos_match,
)
from .claims import load_claims
from .verify import (
    commit_drift_anchor_paths,
    resolve_commit_drift_count,
)
from .time_utils import (
    ensure_utc,
    isoformat_utc_optional,
    parse_event_ts,
)


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


# `_event_id_list` moved to `events.py` (imported above) so memory_health,
# memory_search's endorsement tally, and the negative-outcomes attach all
# share ONE normalizer for event id fields — the 3.15.0 audit found the
# health rollup hardened against poison id shapes while the search-path
# consumers of the very same events still iterated the raw field and took
# retrieval down. See `events._event_id_items` for the full rationale.


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
    "from an unwired hook. Run "
    "`bettermemory doctor` to wire the Stop hook, then re-check."
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_drifted": self.total_drifted,
            "groups": [g.to_dict() for g in self.groups],
            "skipped": list(self.skipped),
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
    # Per-audit `(ts, verdict_or_None)` records. The verdict rides
    # along so `_silent_miss_stats` can split miss-capable audits (the
    # rate denominator) from `no_signal` audits, which structurally
    # cannot flag a miss; a missing/legacy verdict reads as None and
    # counts as miss-capable (conservative).
    silent_miss_audited: list[tuple[datetime | None, str | None]]
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
    # Earliest timestamped retrieval per memory id — the endorsement-
    # grace input to the shared `_is_dead_weight` predicate. Ids with
    # no timestamped retrieval are simply absent (read as "old").
    earliest_retrieval_by_id: dict[str, datetime]
    # How many events on this walk satisfied `is_hook_telemetry_event`.
    # Zero means the store carries no Stop-hook settlement telemetry at
    # all, which makes every `applied_count == 0` uninformative — the
    # input to the dead-weight honesty gate. Defaulted so a caller
    # constructing this record positionally (tests do) stays valid.
    hook_telemetry_events: int = 0


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
        # Audit telemetry is buffered as `(ts, verdict)` pairs and
        # resolved after the events pass so a `silent_miss_cutoff`
        # event later in the log can retroactively drop events before
        # its `cutoff_ts` — the post-fix rollup hatch documented at the
        # `_handle_search_miss` branch. The verdict is carried so the
        # rollup can keep `no_signal` audits out of the miss-rate
        # denominator (see `SilentMissStats`).
        self._silent_miss_audited: list[tuple[datetime | None, str | None]] = []
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
        # Earliest timestamped retrieval per memory id — feeds the
        # endorsement-grace gate of the shared `_is_dead_weight`
        # predicate. Search events without a parseable ts simply don't
        # contribute (the predicate treats an absent earliest retrieval
        # as old, keeping legacy logs dead-weight-eligible).
        self._earliest_retrieval_by_id: dict[str, datetime] = {}
        # Stop-hook settlement telemetry seen on this walk — the
        # dead-weight honesty gate's input. Counted here rather than on
        # a second pass because `compute_health` already walks every
        # event exactly once and the predicate is two dict reads.
        self._hook_telemetry_events = 0
        # `eval.is_admin_recorded_event`, bound at construction through
        # a deferred import: eval.py imports `health.applied_tier` at
        # module scope, so a module-level import here would be a cycle.
        # By the time an accumulator is constructed both modules are
        # fully initialised, and binding once keeps `handle_event` —
        # which runs for every row in the log — free of repeated import
        # machinery. Calling the predicate, rather than re-spelling
        # either axis of the classification locally, is the one wiring
        # `tests/test_eval.py::TestAdminRecordedParity` permits.
        from .eval import is_admin_recorded_event

        self._is_admin_recorded_event: Callable[[dict[str, Any]], bool] = (
            is_admin_recorded_event
        )

    # ---- dispatch -------------------------------------------------------

    def handle_event(self, ev: dict[str, Any]) -> None:
        """Route one event to its per-kind handler. Always bumps the
        total-events counter, regardless of kind; the per-session set
        adds every event EXCEPT admin-recorded ones (the inline comment
        below has the why)."""
        self._total_events += 1
        # Canonical-first session read with the legacy fallback the
        # other event consumers use. The Recorder stamps `session` on
        # every canonical-emitted event, but `turn_audited` /
        # `search_miss` use `session_id` as their canonical field —
        # without the fallback, those event kinds were silently
        # dropped from the distinct-session rollup.
        # `isinstance(sess, str)` guard, not bare truthiness: a
        # list/dict-valued `session` is truthy but unhashable, so
        # `set.add()` would raise TypeError and blank the whole rollup
        # (memory_health / scope_overview / doctor). Only a non-empty
        # string can be a real session id anyway.
        # Admin/CLI writers (`consolidate --acknowledge-debt`'s use
        # rows and its `silent_miss_cutoff` marker, `doctor --fix`)
        # record under a fresh throwaway SessionState id — counting one
        # would publish a session no client ever attached to, so each
        # admin run would permanently inflate the Sessions line by one
        # and put `memory_health` in silent disagreement with
        # `eval.compute_report` and doctor's cadence census, which both
        # already exclude these. The exclusion gates the SESSION TALLY
        # ONLY: the event still dispatches to its handler below, so
        # acknowledge-debt's rows keep counting as the genuine
        # endorsements they are (eval.py's scope note on
        # `ADMIN_RECORDED_ATTRIBUTION_PREFIX`).
        sess = ev.get("session") or ev.get("session_id")
        if isinstance(sess, str) and sess and not self._is_admin_recorded_event(ev):
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
        ts = _ensure_utc(parse_event_ts(ev.get("ts")))
        for mid in _event_id_list(
            ev.get("returned") or ev.get("memory_ids") or ev.get("hit_ids")
        ):
            stats = self._by_id.get(mid)
            if stats:
                stats.retrieval_count += 1
                if ts is not None:
                    prev = self._earliest_retrieval_by_id.get(mid)
                    if prev is None or ts < prev:
                        self._earliest_retrieval_by_id[mid] = ts

    def _handle_show(self, ev: dict[str, Any]) -> None:
        # Guard the id exactly like `_handle_update` / `_handle_verify`:
        # a non-str (e.g. an unhashable list/dict) id would raise out of
        # `dict.get` and blank the whole rollup. Only a non-empty str can
        # match a real memory anyway.
        mid = ev.get("id", "")
        if isinstance(mid, str) and mid:
            stats = self._by_id.get(mid)
            if stats:
                stats.show_count += 1

    def _handle_use(self, ev: dict[str, Any]) -> None:
        # Coverage bookkeeping first: it is per-EVENT, not per-id, and
        # it must count even when every id in the event is unknown to
        # this store (a hook that settled a since-tombstoned memory
        # still proves the hook runs).
        self._note_hook_telemetry(ev)
        outcome = ev.get("outcome")
        ts = _ensure_utc(parse_event_ts(ev.get("ts")))
        # Dedupe ids WITHIN this one event before counting — mirroring
        # `eval.compute_eval`'s per-event dedup. The recorder stores
        # `memory_ids` verbatim (handlers/record_use.py), so a single
        # `memory_record_use(memory_ids=["A", "A"], ...)` call yields a
        # duplicate-carrying event; counting it raw settles two
        # applied/auto/explicit (and hook/model) increments for one
        # settlement, skews endorsement_ratio, lets two such events
        # cross the heavily_used floor (applied >= 3), and silently
        # disagrees with the published eval counters over the same log.
        # `dict.fromkeys` keeps first-seen order.
        for mid in dict.fromkeys(_event_id_list(ev.get("ids") or ev.get("memory_ids"))):
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
                # Tiering via the shared `applied_tier` (see its
                # docstring for the auto / hook / model rule and why
                # the third tier is a fall-through). `explicit` stays
                # exactly what it always was — everything not auto —
                # so `endorsement_ratio`, `_is_weakly_endorsed` and the
                # published eval rate all keep their meaning; the
                # hook/model pair below decomposes it.
                tier = applied_tier(ev)
                if tier == "auto":
                    stats.auto_applied_count += 1
                else:
                    stats.explicit_applied_count += 1
                    if tier == "hook":
                        stats.hook_applied_count += 1
                    else:
                        stats.model_applied_count += 1
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
        # Normalize through the shared id-list helper so a malformed
        # event can't take down the rollup: a numeric scalar under
        # `for marker in <scalar>` raises TypeError, and a bare string
        # would shred into per-character marker rows. `_event_id_list`
        # gives list-passthrough / lone-string -> [value] / else [].
        # `canonical_marker` folds pre-bucketing SHA names (`sha:874b0b0`)
        # onto the canonical one. Without it the fires and overrides this
        # rollup exists to compare stay split across a row per commit, and
        # the highest-volume marker class in the store reads as noise.
        for marker in _event_id_list(ev.get("markers")):
            self._marker_fires[canonical_marker(marker)] += 1
        for marker in _event_id_list(ev.get("markers_acknowledged")):
            self._marker_overrides[canonical_marker(marker)] += 1

    def _handle_turn_audited(self, ev: dict[str, Any]) -> None:
        # Denominator bookkeeping for the silent-miss rate. Buffered
        # with the event ts so a later `silent_miss_cutoff` can
        # retroactively drop pre-cutoff audits — keeping just the
        # numerator filtered would skew the rate (low miss / high
        # audited). The verdict rides along (round 88) so `no_signal`
        # audits — which structurally cannot flag a miss — land in
        # `no_signal_total` instead of inflating `audited_total`: a
        # pre-4.0 `search_mode="semantic"` deployment's Stop hook
        # no_signalled EVERY turn forever, and counting those as the
        # denominator produced a perpetual false-green 0% miss rate.
        # That config is gone; the split is not, because any
        # structurally-blind probe repeats the shape. Legacy verdicts
        # read as None and stay in the miss-capable denominator
        # (conservative).
        #
        # Coverage bookkeeping runs BEFORE the repeat early-out: a
        # deduped re-audit is still proof the Stop hook fired, and the
        # dead-weight gate asks "is the hook wired?", not "how many
        # distinct turns did it audit?".
        self._note_hook_telemetry(ev)
        if ev.get("repeat"):
            # Re-audit of the same (session, message) inside the dedup
            # window (3.14+; `audit.is_duplicate_audit`) — cadence
            # bookkeeping only. Producers never emit a companion
            # `search_miss` for a repeat, so counting it into either
            # denominator bucket would dilute a rate whose numerator
            # structurally can't include it.
            return
        verdict = ev.get("verdict")
        self._silent_miss_audited.append(
            (
                _ensure_utc(parse_event_ts(ev.get("ts"))),
                verdict if isinstance(verdict, str) else None,
            )
        )

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

    def _note_hook_telemetry(self, ev: dict[str, Any]) -> None:
        """Bump the Stop-hook coverage counter if `ev` qualifies.

        Deliberately not spelled with the _handle_ prefix: every method
        on this class carrying it must have a matching `_HANDLERS` key
        (pinned by `test_handlers_table_matches_handle_methods`), and
        this is a helper the real per-kind handlers call, not a
        dispatch target.
        """
        if is_hook_telemetry_event(ev):
            self._hook_telemetry_events += 1

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
            silent_miss_audited=self._silent_miss_audited,
            silent_miss_events=self._silent_miss_events,
            latest_miss_cutoff=self._latest_miss_cutoff,
            acknowledged_miss_event_ids=self._acknowledged_miss_event_ids,
            resolution_events_by_id=self._resolution_events_by_id,
            earliest_retrieval_by_id=self._earliest_retrieval_by_id,
            hook_telemetry_events=self._hook_telemetry_events,
        )

    # Class-level dispatch table. Defined after the methods so the
    # references resolve; declared on the class so the lookup is
    # built once per process rather than per `handle_event` call.
    #
    # `list` events are DELIBERATELY absent: `retrieval_count` is the
    # ranked-delivery basis the cold-endorsement floor and the
    # dead-weight rule read (a memory the RANKER keeps surfacing that
    # the model never applies), and a `memory_list` browse of an entire
    # scope would mark every row in it retrieved at once. `eval.py`
    # keys `list` into its `retrieval_occurrences` rate denominator by
    # design while keeping its own floor basis (`search_delivery_count`)
    # search-only — the two surfaces split the same way, on purpose;
    # `show` is likewise keyed into `show_count`, never here.
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
    hook_telemetry_events: int | None = None,
) -> HealthReport:
    """Build a `HealthReport` from active memories + the event stream.

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
        origin_repo_by_id[m.id] = m.origin.repo if m.origin else None
        origin_worktree_by_id[m.id] = m.origin.worktree_root if m.origin else None
        anchor_paths_by_id[m.id] = commit_drift_anchor_paths(m.body, m.verified_paths)
        claims_by_id[m.id] = tuple(m.claims)

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
    silent_miss_audited = rollups.silent_miss_audited
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
    # The rule is the shared `_is_dead_weight` predicate (see its
    # docstring for the full gate list) so this bucket, the
    # `curation_counts` "dead" rollup, and the consolidate demotion
    # pass cannot diverge — the rollup used to key on `created` alone
    # and reported dead rows the demotion pass refused to drain.
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
    all_scopes = list(scope_distribution.keys())
    rare_scopes = sorted(
        scope
        for scope, count in scope_distribution.items()
        if count == 1
        and any(
            other != scope and _scope_typo_neighbor(scope, other)
            for other in all_scopes
        )
    )

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

    commit_drift_debt = _compute_commit_drift_debt(
        by_id=by_id,
        origin_repo_by_id=origin_repo_by_id,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        caller_origin=caller_origin,
    )

    cross_repo_drift = _compute_cross_repo_drift(
        by_id=by_id,
        origin_repo_by_id=origin_repo_by_id,
        origin_worktree_by_id=origin_worktree_by_id,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
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
        cross_repo_drift=cross_repo_drift,
        silent_misses=_silent_miss_stats(
            audited=silent_miss_audited,
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
        telemetry_coverage=telemetry_coverage,
    )
    # Recommendations are distilled from the buckets, so the gate above
    # reaches them for free: an emptied `dead_weight` cannot cross the
    # size floor, and `memory_health` therefore never tells the model to
    # go remove rot it invented from a missing hook.
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
                    "memory_verify(id, verified_paths=[...]) to re-anchor "
                    "if claims still hold — paths are what this count is "
                    "narrowed by, and any verify slides the boundary "
                    "forward; memory_update where the body needs to track "
                    "the new code."
                ),
                count=report.commit_drift_debt.total_drifted,
                memory_ids=[
                    r.id
                    for r in report.commit_drift_debt.rows[:_RECOMMENDATION_ROW_CAP]
                ],
            )
        )

    if report.provenance is not None and report.provenance.unaccounted_total >= 1:
        # Floor of one, like `contradicted`: a single record that entered
        # the store outside every recorded path is independently
        # actionable, and the label exists so nobody waits for three.
        out.append(
            Recommendation(
                kind="review_unaccounted",
                summary=(
                    f"{report.provenance.unaccounted_total} memories entered "
                    "the store outside every recorded path: the event log "
                    "covers their creation, nothing wrote them, nothing "
                    "pulled them."
                ),
                action=(
                    "memory_show(id) each one. To keep a record you "
                    "recognise, memory_remove(id, reason=...) then "
                    "memory_restore(id): the restore re-admits it through "
                    "the store and it reads local from then on. Remove the "
                    "rest."
                ),
                count=report.provenance.unaccounted_total,
                memory_ids=[
                    r.id
                    for r in report.provenance.unaccounted[:_RECOMMENDATION_ROW_CAP]
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


def _drift_rows_for_candidates(
    candidates: list[MemoryStats],
    *,
    root: Path,
    timestamps: list[datetime],
    toplevel: Path | None,
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
) -> list[CommitDriftRow]:
    """The per-candidate bisect-and-narrow loop, shared between the
    caller-repo rollup and the cross-repo estate check — one loop so
    the two cannot compute different drift policies for the same
    memory. Returns drifting rows sorted heaviest-first; the caller
    owns capping and the empty-vs-None distinction.
    """
    rows: list[CommitDriftRow] = []
    for stats in candidates:
        since = stats.last_verified_at
        assert since is not None  # callers filter on this
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        # bisect_right gives us the first index strictly greater than
        # `since`; len - idx is then the count of timestamps after that
        # cut. Equal timestamps fall before the cut on bisect_right
        # semantics, which is what we want — a verify call that lands
        # at the same instant as a commit doesn't count as drift.
        idx = bisect.bisect_right(timestamps, since)
        count = len(timestamps) - idx
        # Narrow to commits that actually touched the memory's claim
        # anchors — mirrors memory_show and the memory_search top-hit
        # surface (_response.py). Without this the rollup nagged on
        # memories the user deliberately attested as stable and disagreed
        # with the per-hit verdict. None means every anchor escapes this
        # repo: the claims live elsewhere, drift is not applicable, drop
        # the row. Falls back to the unfiltered count only when git can't
        # answer the filtered query. Guarded on count > 0 so a caught-up
        # memory never pays the extra git call — and, unlike the
        # per-memory surfaces (memory_show, the search hit), this loop
        # needs no quiescent applicability classification: it emits
        # rows for `count > 0` only, so a caught-up memory contributes
        # nothing whether or not the signal applies to it, and there is
        # no affirmative 0 here for the escape/phantom rules to correct.
        if count > 0:
            stored_claims = claims_by_id.get(stats.id, ()) if claims_by_id else ()
            resolved = resolve_commit_drift_count(
                cwd=root,
                since=since,
                unfiltered=count,
                anchors=anchor_paths_by_id.get(stats.id, ()),
                claims=load_claims(list(stored_claims)) if stored_claims else (),
                toplevel=toplevel,
            )
            if resolved is None:
                continue
            count = resolved
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
    rows.sort(key=lambda r: r.commits_since_verify, reverse=True)
    return rows


def _compute_cross_repo_drift(
    *,
    by_id: dict[str, MemoryStats],
    origin_repo_by_id: dict[str, str | None],
    origin_worktree_by_id: dict[str, str | None],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    caller_origin: Origin | None,
) -> CrossRepoDrift | None:
    """The estate check: drift for memories anchored in repos the
    caller is NOT standing in.

    The blind spot this closes (2026-08-30): every commit-drift
    surface — show, search, `commit_drift_debt`, the curation counts —
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

    Bounds and silences: `_CROSS_REPO_MAX_ROOTS` caps the roots git is
    walked in per run (cheap skips don't consume it; over-cap groups
    are listed as skipped, never dropped). `None` when no foreign
    claim-anchored verified candidate exists at all — the same
    philosophy as `commit_drift_debt`'s None. A walked clean group IS
    emitted, with zero rows: "checked N, clean" and "didn't check"
    must not read the same.
    """
    groups: dict[tuple[str, str], list[MemoryStats]] = {}
    for stats in by_id.values():
        origin_repo = origin_repo_by_id.get(stats.id)
        worktree = origin_worktree_by_id.get(stats.id)
        if not origin_repo or not worktree:
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
    if not groups:
        return None

    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    out_groups: list[CrossRepoDriftGroup] = []
    skipped: list[dict[str, str]] = []
    walked = 0
    for (repo, worktree), candidates in ordered:
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
        if not root.exists():
            skipped.append(
                {
                    "repo": repo,
                    "worktree_root": worktree,
                    "reason": "worktree missing on disk",
                }
            )
            continue
        live = capture(root)
        if live.repo is None or not repos_match(repo, live.repo):
            skipped.append(
                {
                    "repo": repo,
                    "worktree_root": worktree,
                    "reason": "directory is no longer a checkout of the recorded repo",
                }
            )
            continue
        walked += 1
        timestamps = commit_author_timestamps(root)
        if timestamps is None:
            skipped.append(
                {
                    "repo": repo,
                    "worktree_root": worktree,
                    "reason": "git unreachable in the recorded worktree",
                }
            )
            continue
        rows = _drift_rows_for_candidates(
            candidates,
            root=root,
            timestamps=timestamps,
            toplevel=repo_toplevel(root),
            anchor_paths_by_id=anchor_paths_by_id,
            claims_by_id=claims_by_id,
        )
        out_groups.append(
            CrossRepoDriftGroup(
                repo=repo,
                worktree_root=worktree,
                rows=rows[:_COMMIT_DRIFT_DEBT_CAP],
                total_drifted=len(rows),
                candidates=len(candidates),
            )
        )
    if not out_groups and not skipped:
        return None
    return CrossRepoDrift(
        groups=out_groups,
        skipped=skipped,
        total_drifted=sum(g.total_drifted for g in out_groups),
    )


def _compute_commit_drift_debt(
    *,
    by_id: dict[str, MemoryStats],
    origin_repo_by_id: dict[str, str | None],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None = None,
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
    cwd_path = Path(caller_origin.cwd)
    timestamps = commit_author_timestamps(cwd_path)
    if timestamps is None:
        return None
    # Resolve the repo root once for the whole rollup — the per-memory
    # anchor resolution below would otherwise pay a `git rev-parse`
    # fork+exec per drifting memory.
    toplevel = repo_toplevel(cwd_path)

    # Two-pass: filter by repo match first, then run the bisect. Lets us
    # short-circuit the "no matching memories" case before any per-row
    # work — keeps the rollup silent when it would have nothing to say.
    # A memory with no claim anchors is dropped here too: repo commits
    # cannot invalidate a claim-less memory, so it can never be
    # "drifted" (the claim-anchored policy — see
    # `verify.resolve_commit_drift_count`; measured 100% false-positive
    # on the dogfood store before this gate).
    candidates: list[MemoryStats] = []
    for stats in by_id.values():
        origin_repo = origin_repo_by_id.get(stats.id)
        if origin_repo is None:
            continue
        if not repos_match(origin_repo, caller_origin.repo):
            continue
        if stats.last_verified_at is None:
            continue
        if not anchor_paths_by_id.get(stats.id) and not (
            claims_by_id and claims_by_id.get(stats.id)
        ):
            continue
        candidates.append(stats)
    if not candidates:
        return None

    rows = _drift_rows_for_candidates(
        candidates,
        root=cwd_path,
        timestamps=timestamps,
        toplevel=toplevel,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
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
    if report.episode_volume is not None:
        vol = report.episode_volume
        prunable = ""
        if vol.prunable_sessions:
            # Only surfaced when it is actionable. A read-only loop is the
            # case this line exists for: episode GC runs on `episode_write`
            # and `bettermemory episodes prune` only, so these directories
            # sit collectable until something writes.
            prunable = (
                f" — {vol.prunable_sessions} session"
                f"{'' if vol.prunable_sessions == 1 else 's'} past the "
                f"{vol.ttl_days}-day TTL, run `bettermemory episodes prune`"
            )
        lines.append(
            f"Episodes:        {vol.episodes} in {vol.sessions} session"
            f"{'' if vol.sessions == 1 else 's'}, "
            f"{vol.bytes:,} bytes{prunable}"
        )

    lines.append("")
    lines.append(
        f"Dead weight ({len(report.dead_weight)}) — retrieved but never "
        f"`applied`, older than {report.window_days} days:"
    )
    if not report.dead_weight:
        lines.append("  (none)")
        # An empty bucket has two very different causes, and the CLI
        # reader has no other way to tell them apart. Printed only when
        # suppressed, so the line never appears on a clean hooked store.
        coverage = report.telemetry_coverage
        if coverage is not None and coverage.dead_weight_suppressed:
            lines.append(f"  NOT MEASURED: {coverage.reason}")
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
        f"Verification debt — never={debt.never_verified_total} "
        f"({debt.never_verified_checkable} checkable)  "
        f"stale={debt.stale_total} ({debt.stale_checkable} checkable)  "
        f"fresh={debt.fresh_count}  "
        f"(stale after {debt.stale_after_days} days; checkable = "
        "declared claims or cited/attested paths a verify pass can "
        "check mechanically):"
    )
    if debt.never_verified_total == 0 and debt.stale_total == 0:
        lines.append("  (none)")
    if debt.never_verified:
        lines.append(
            f"  never-verified ({debt.never_verified_total}, "
            "checkable first, then oldest):"
        )
        for s in debt.never_verified:
            lines.append(f"    {s.id} {','.join(s.scopes)}: {s.summary}")
        if debt.never_verified_total > len(debt.never_verified):
            lines.append(
                f"    ... and {debt.never_verified_total - len(debt.never_verified)} more"
            )
    if debt.stale:
        lines.append(
            f"  stale ({debt.stale_total}, checkable first, then oldest verification):"
        )
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
    if sm.audited_total > 0 or sm.miss_total > 0 or sm.no_signal_total > 0:
        lines.append("")
        # `no_signal` rides the header so the two shapes the stats
        # docstring promises to distinguish actually render apart: a
        # store whose every audit stopped at the no-signal branch used
        # to print byte-identically to one whose audit hook never fired.
        lines.append(
            f"Silent misses — audited={sm.audited_total}  "
            f"miss={sm.miss_total}  "
            f"no_signal={sm.no_signal_total}  "
            f"unique_memories={sm.unique_miss_memories}  "
            f"(emit via memory_audit_turn from a client-side end-of-turn hook):"
        )
        if sm.audited_total == 0 and sm.no_signal_total > 0:
            lines.append(
                "  (no measurable audits — every audited turn stopped at "
                "the no-signal branch: empty store, empty query, or no "
                "hits at all)"
            )
        elif sm.miss_total == 0:
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

    if report.cross_repo_drift is not None:
        xr = report.cross_repo_drift
        lines.append("")
        header = (
            f"Cross-repo drift — {xr.total_drifted} drifted across "
            f"{len(xr.groups)} checked foreign checkout(s)"
        )
        if xr.skipped:
            header += f", {len(xr.skipped)} skipped"
        lines.append(header + ":")
        for group in xr.groups:
            state = f"{group.total_drifted} drifted" if group.total_drifted else "clean"
            lines.append(
                f"  {group.repo} [{group.worktree_root}]: {state} of "
                f"{group.candidates} candidate(s)"
            )
            for row in group.rows:
                verified = _iso(row.last_verified_at) or "?"
                lines.append(
                    f"    {row.id} [+{row.commits_since_verify} commits, "
                    f"verified={verified}] {','.join(row.scopes)}: {row.summary}"
                )
            if group.total_drifted > len(group.rows):
                lines.append(
                    f"    ... and {group.total_drifted - len(group.rows)} more"
                )
        for skip in xr.skipped:
            lines.append(
                f"  (skipped {skip['repo']} [{skip['worktree_root']}]: "
                f"{skip['reason']})"
            )

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
    audited: list[tuple[datetime | None, str | None]],
    miss_events: list[tuple[datetime | None, str | None, str | None, str | None]],
    cutoff: datetime | None,
    tombstoned_ids: set[str],
    acknowledged_event_ids: set[str] | None = None,
) -> SilentMissStats:
    """Fold the buffered audit telemetry into a `SilentMissStats`.

    `audited` is the list of `(ts, verdict_or_None)` pairs buffered
    from `turn_audited` events. Audits whose verdict is ``"no_signal"``
    are counted into `no_signal_total` rather than `audited_total` —
    they structurally cannot flag a miss, so leaving them in the rate
    denominator dilutes it (and for the pre-4.0 semantic-config Stop
    hook, whose audits were ALL permanent no_signals, manufactured a
    perpetual false-green 0% miss rate). A None verdict (missing/legacy field)
    counts as miss-capable, the conservative read. The cutoff filter
    below applies to BOTH buckets.

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
    # Verdict split BEFORE the shared cutoff count so both buckets get
    # the identical `_count_post_cutoff` semantics (None-ts handling
    # included) — filtering one bucket and not the other would let a
    # cutoff skew the no_signal/audited proportions.
    audited_total = _count_post_cutoff(
        [ts for ts, verdict in audited if verdict != "no_signal"], cutoff
    )
    no_signal_total = _count_post_cutoff(
        [ts for ts, verdict in audited if verdict == "no_signal"], cutoff
    )
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
        no_signal_total=no_signal_total,
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


# Trailing successor suffix on a scope's name part: a digit run,
# optionally preceded by '-'/'_'/'v' ("2024" in "aoc2024", "-v2" in
# "blog-v2", "2" in "foo2"). Two tails that are equal once this suffix
# is stripped are deliberate siblings, not typos of each other.
_SIBLING_SUFFIX_RE = re.compile(r"[-_]?v?\d+$")


def _scope_typo_neighbor(scope: str, other: str) -> bool:
    """True iff `scope` plausibly looks like a typo of `other`.

    Backs the `rare_scopes` neighbor check. A raw whole-string
    Levenshtein threshold misfires on real scope shapes in both
    directions — a shared "projects:" prefix contributes zero distance
    (so any two short project names collide), while namespace omission
    ("bettermemory" vs "projects:bettermemory") yields distance 9 and is
    never seen. The rules, in order:

    1. Exact name-part equality across a namespace boundary flags: the
       text after the first ':' (the whole string when bare) matching
       another scope's name part exactly is a stronger mis-tag signal
       than any distance-2 hit ("bettermemory" / "proj:bettermemory"
       against "projects:bettermemory").
    2. Equal leading ':'-segments are stripped and only the differing
       tails are compared, so a long shared namespace prefix can't lend
       distance slack to a short tail ("projects:vim" vs "projects:git"
       compares vim/git; "projct:foo" vs "projects:foo" differs in its
       first segment and still compares full strings).
    3. Tails equal after stripping a trailing successor suffix
       (`_SIBLING_SUFFIX_RE`) are exempt: aoc2023/aoc2024, blog-v2/
       blog-v3, foo/foo2 are deliberate sibling scopes.
    4. The distance threshold scales with tail length: 2 only when both
       tails are >= 6 chars, 1 below that, and for tails <= 3 chars only
       a length-changing edit counts — a substitution at that length
       rewrites a third or more of the tag (vim/git, gpu/cpu, api/aws)
       and isn't typo evidence, while insert/delete keeps the genuine
       bug/bugs catch. Residual accepted false positive: just/rust
       (distance-1 substitution at length 4) — clearing it would need
       threshold 0 for short names, dropping tool/tools and bug/bugs.
    """
    if scope.split(":", 1)[-1] == other.split(":", 1)[-1]:
        return True

    segs_a = scope.split(":")
    segs_b = other.split(":")
    common = 0
    for seg_a, seg_b in zip(segs_a, segs_b):
        if seg_a != seg_b:
            break
        common += 1
    tail_a = ":".join(segs_a[common:])
    tail_b = ":".join(segs_b[common:])

    if _SIBLING_SUFFIX_RE.sub("", tail_a) == _SIBLING_SUFFIX_RE.sub("", tail_b):
        return False

    shorter = min(len(tail_a), len(tail_b))
    if shorter >= 6:
        return _edit_distance_within(tail_a, tail_b, 2)
    if shorter <= 3 and len(tail_a) == len(tail_b):
        # Equal-length short tails: a distance-1 hit would have to be a
        # substitution, which rule 4 rejects below this length.
        return False
    return _edit_distance_within(tail_a, tail_b, 1)


def _edit_distance_within(a: str, b: str, max_dist: int) -> bool:
    """True iff Levenshtein(a, b) <= max_dist.

    Standard Wagner-Fischer DP, two-row variant. We don't need the
    actual distance — only whether it falls within the threshold —
    but scope names are short enough (typically <30 chars) that the
    full table is cheap and the early-exit machinery isn't worth its
    complexity. The length-difference shortcut catches the obviously
    far cases without running the table at all.

    Used by the `rare_scopes` neighbor check (via
    `_scope_typo_neighbor`, which owns the scope-shape rules); lifted
    out as a helper so it stays testable in isolation if we ever need
    to tune the threshold or swap algorithms.
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
    hook_telemetry_events: int | None = None,
    index_root: Path | None = None,
) -> dict[str, int]:
    """Cheap summary of curation pressure.

    Returns
    ``{"stale", "never_verified", "drifted", "cold", "dead",
    "silent_misses", "unique_silent_miss_memories",
    "cold_endorsement_memories", "unaccounted"}`` —
    integer counts only, no row materialisation. `unaccounted` is the
    one count that reads index state rather than event state: the
    memories the index labels as having entered the store outside every
    recorded path (`provenance.py`). It needs `index_root` and stays 0
    without one; in delta mode it counts only the unaccounted memories
    created after `since`. Used by
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

    `hook_telemetry_events` arms the same honesty gate
    `compute_health` carries, with identical semantics (`None` = caller
    did not measure, assume covered; an int = gate on, OR-ed with this
    walk's own observation). The `dead` AND `cold_endorsement_memories`
    counts are both gated — each shares its bucket with
    `compute_health`, and the numerical contract forces the two
    surfaces to zero together.

    The endorsement leg joined the gate 2026-08-30 (this paragraph
    used to defer it as a follow-up). `cold_endorsement_memories` keys
    on `explicit_applied_count == 0`, which is if anything MORE
    hook-dependent than `dead` — the Stop hook's containment matcher
    is the dominant producer of explicit applies — and the curation
    hint's pressure formula
    (`handlers/_shared._maybe_attach_curation_hint`) sums
    `dead + drifted + cold_endorsement_memories`, so a hookless store
    was nagged through a leg that was misleading for exactly the
    reason the gate exists. Of the two recorded shapes, the gate was
    widened rather than the leg dropped from the pressure sum alone:
    gating only the hint would have left this published count
    misleading on the one surface the model does not have to ask for,
    while gating the count corrects the hint's sum for free and keeps
    the compute_health agreement intact.

    The returned key set is deliberately UNCHANGED: it flows into
    `memory_scope_overview`'s `curation_pending` block, whose key set is
    enumerated in that tool's description and pinned on the wire. The
    explanation for a suppressed count lives on `memory_health`, which
    is where a model that wants to know WHY a count is zero is already
    being sent.
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
    # Earliest timestamped retrieval and newest contradicted ts per id —
    # the endorsement-grace and unresolved-contradiction inputs to the
    # shared `_is_dead_weight` predicate, mirroring what
    # `_StatsAccumulator` tracks for `compute_health`.
    earliest_retrieval: dict[str, datetime] = {}
    last_contradicted: dict[str, datetime] = {}
    observed_hook_telemetry = 0
    for ev in events:
        kind = ev.get("kind")
        # Coverage bookkeeping is exempt from the `--since` filter
        # below, exactly like the two global markers underneath it.
        # "Is the Stop hook wired for this store?" is a property of the
        # store, not of the delta window: a session whose hook events
        # all predate the boundary is still a hooked store, and gating
        # this behind the filter would make the delta arm manufacture a
        # "hookless" verdict every session-start and blank the `dead`
        # count that the absolute arm — same events, no filter — reports
        # normally. The two arms are read side by side.
        if is_hook_telemetry_event(ev):
            observed_hook_telemetry += 1
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
            search_ts = _ensure_utc(_parse_event_ts(ev.get("ts")))
            for mid in _event_id_list(
                ev.get("returned") or ev.get("memory_ids") or ev.get("hit_ids")
            ):
                if mid in retrieval_counts:
                    retrieval_counts[mid] += 1
                    if search_ts is not None:
                        prev = earliest_retrieval.get(mid)
                        if prev is None or search_ts < prev:
                            earliest_retrieval[mid] = search_ts
        elif kind == "use" and ev.get("outcome") == "applied":
            is_auto = ev.get("auto") is True
            # Same per-event id dedup as `_StatsAccumulator._handle_use`
            # and eval's counting loop. The numerical contract (each
            # count here must agree with its `compute_health` bucket)
            # breaks without it: a duplicate-carrying event raw-counted
            # here shifts the explicit/total ratio that feeds the
            # `cold_endorsement_memories` threshold below.
            for mid in dict.fromkeys(
                _event_id_list(ev.get("ids") or ev.get("memory_ids"))
            ):
                if mid in applied_counts:
                    applied_counts[mid] += 1
                    if not is_auto and mid in explicit_applied_counts:
                        explicit_applied_counts[mid] += 1
        elif kind == "use" and ev.get("outcome") == "contradicted":
            use_ts = _ensure_utc(_parse_event_ts(ev.get("ts")))
            if use_ts is not None:
                for mid in _event_id_list(ev.get("ids") or ev.get("memory_ids")):
                    if mid in retrieval_counts:
                        prev = last_contradicted.get(mid)
                        if prev is None or use_ts > prev:
                            last_contradicted[mid] = use_ts
        elif kind == "search_miss":
            # Shared parser with `_StatsAccumulator._handle_search_miss`
            # so the two silent-miss readers stay numerically in lockstep.
            silent_miss_events_list.append(_parse_silent_miss_event(ev))

    silent_miss_stats = _silent_miss_stats(
        audited=[],  # curation_counts only surfaces the numerator
        miss_events=silent_miss_events_list,
        cutoff=latest_miss_cutoff,
        tombstoned_ids=tombstoned_ids_set,
        acknowledged_event_ids=acknowledged_event_ids,
    )
    silent_misses = silent_miss_stats.miss_total
    unique_silent_miss_memories = silent_miss_stats.unique_miss_memories

    # Dead-weight honesty gate — see `compute_health` for the full
    # contract. `None` leaves the count exactly as it was pre-3.32.
    telemetry_covered = (
        hook_telemetry_events is None
        or (hook_telemetry_events + observed_hook_telemetry) > 0
    )

    never_verified = 0
    stale = 0
    cold = 0
    dead = 0
    cold_endorsement_memories = 0
    endorsement_floor = max(1, int(cold_endorsement_min_retrievals))
    grace_cutoff = now - timedelta(days=_ENDORSEMENT_GRACE_DAYS)
    for m in mem_list:
        is_ambient = m.category == Category.AMBIENT
        if m.last_verified_at is None:
            never_verified += 1
        elif m.last_verified_at < verification_cutoff:
            stale += 1
        if not is_ambient and m.created < cutoff:
            if retrieval_counts.get(m.id, 0) == 0:
                cold += 1
        # `dead` reads the shared `_is_dead_weight` predicate so the
        # count agrees with `compute_health`'s dead_weight bucket (the
        # numerical contract) — including the freshest-touch,
        # contradiction, and endorsement-grace gates the demotion pass
        # applies. Disjoint from `cold` by construction: dead requires
        # at least one retrieval. The telemetry gate rides in the same
        # condition for the same reason: `compute_health` empties its
        # bucket on a hookless store, so this count must go to zero
        # with it or the two surfaces disagree about the same store.
        first_seen = earliest_retrieval.get(m.id)
        if telemetry_covered and _is_dead_weight(
            category=m.category,
            freshest_ts=_freshest_touch_ts(
                m.created, m.updated, m.last_verified_at, m.last_corroborated
            ),
            retrieval_count=retrieval_counts.get(m.id, 0),
            applied_count=applied_counts.get(m.id, 0),
            has_unresolved_contradiction=_has_unresolved_contradiction(
                last_contradicted.get(m.id), m.updated, m.last_verified_at
            ),
            earliest_retrieval_ts=(
                first_seen.timestamp() if first_seen is not None else None
            ),
            cutoff_ts=cutoff.timestamp(),
            grace_cutoff_ts=grace_cutoff.timestamp(),
        ):
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
        # of times by the ranker. The telemetry gate rides in the same
        # condition for the same reason as `dead` above: explicit
        # applies come from the hook matcher and deliberate model
        # calls, so on a hookless store the zero is uninformative,
        # `compute_health` empties its bucket, and this count must
        # fall to zero with it or the two surfaces disagree.
        if (
            telemetry_covered
            and not is_ambient
            and retrieval_counts.get(m.id, 0) >= endorsement_floor
        ):
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
        cwd_path = Path(caller_origin.cwd)
        timestamps = commit_author_timestamps(cwd_path)
        if timestamps is not None:
            # One rev-parse for the whole pass; the per-memory anchor
            # resolution below reuses it.
            toplevel = repo_toplevel(cwd_path)
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
                # Claim-anchored gate, matching memory_show /
                # memory_search / commit_drift_debt: a memory citing no
                # paths at all is exempt (repo commits can't invalidate
                # a claim-less preference/lesson), and one whose anchors
                # all escape this repo is exempt too. Anchor derivation
                # is pure CPU; git work stays behind the count > 0 guard
                # so a caught-up memory pays no git call. No quiescent
                # applicability classification either: this is a
                # `drifted` COUNT, incremented on `count > 0` only, so a
                # caught-up memory reads the same zero contribution
                # whether the signal applies to it or not.
                anchors = commit_drift_anchor_paths(m.body, m.verified_paths)
                parsed_claims = load_claims(m.claims) if m.claims else []
                if not anchors and not parsed_claims:
                    continue
                idx = bisect.bisect_right(timestamps, verified_at)
                count = len(timestamps) - idx
                if count > 0:
                    resolved = resolve_commit_drift_count(
                        cwd=cwd_path,
                        since=verified_at,
                        unfiltered=count,
                        anchors=anchors,
                        claims=parsed_claims,
                        toplevel=toplevel,
                    )
                    if resolved is None:
                        continue
                    count = resolved
                if count > 0:
                    drifted += 1

    # Provenance is index state, not event state: read the `unaccounted`
    # ids off the index when a root is given, and in delta mode count
    # only the ones created after the boundary (`mem_list` is already
    # the post-`since` slice, so membership in it is the filter).
    unaccounted = 0
    if index_root is not None:
        from . import index as _index

        unaccounted_ids = _index.provenance_rows(index_root, label="unaccounted")
        if unaccounted_ids:
            if since_aware is None:
                unaccounted = len(unaccounted_ids)
            else:
                in_window = {m.id for m in mem_list}
                unaccounted = sum(1 for i in unaccounted_ids if i in in_window)

    return {
        "stale": stale,
        "never_verified": never_verified,
        "drifted": drifted,
        "cold": cold,
        "dead": dead,
        "silent_misses": silent_misses,
        "unique_silent_miss_memories": unique_silent_miss_memories,
        "cold_endorsement_memories": cold_endorsement_memories,
        "unaccounted": unaccounted,
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


def provenance_debt(root: Path, memories: Iterable[Memory]) -> ProvenanceDebt | None:
    """The provenance bucket for `root`'s index, joined against `memories`.

    Reads the index's per-label counts and its `unaccounted` ids, and
    fills the rows from the memories the caller already loaded (no
    second parse). None when the index is absent or unusable, which the
    report carries as null rather than as an empty bucket."""
    from . import index as _index

    counts = _index.provenance_counts(root)
    if counts is None:
        return None
    ids = _index.provenance_rows(root, label="unaccounted") or []
    by_id = {m.id: m for m in memories}
    rows: list[ProvenanceRow] = []
    for memory_id in ids:
        if len(rows) >= _PROVENANCE_ROW_CAP:
            break
        memory = by_id.get(memory_id)
        if memory is None:
            continue
        rows.append(
            ProvenanceRow(
                id=memory.id,
                scopes=list(memory.scopes),
                summary=first_summary_line(memory.body),
                created=memory.created,
            )
        )
    return ProvenanceDebt(counts=counts, unaccounted_total=len(ids), unaccounted=rows)


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
    tooling that doesn't have a meaningful cwd to anchor against.

    This is the entry point BOTH production surfaces use — the
    `memory_health` MCP tool (`handlers/health.py`) and `bettermemory
    health` (`cli/health_cmd.py`) — which is why the dead-weight honesty
    gate is armed here rather than in `compute_health`'s default: every
    caller looking at a REAL store goes through this function, and every
    caller passing a synthetic event list does not."""
    from .store import Store

    store = Store(root)
    tombstoned_ids = {t.id for t in store.load_tombstones()}
    memories = store.load_all()
    report = compute_health(
        memories,
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
        # `0`, not a pre-measured count: `iter_all_events` is a
        # generator we hand straight in, and pre-counting would mean a
        # second full walk of the log for two dict reads per event.
        # Zero arms the gate and delegates the measurement to the walk
        # `compute_health` is about to do anyway — see its docstring.
        hook_telemetry_events=0,
    )
    # Post-assigned for the reason the episode gauge below is: the
    # label lives in the index, and `compute_health` never sees a root.
    # Recommendations are recomputed so `review_unaccounted` can fire
    # on what the bucket found.
    report.provenance = provenance_debt(root, memories)
    report.recommendations = _compute_recommendations(report)
    # Post-assigned rather than threaded through `compute_health`, for
    # the same reason the honesty gate is armed here: `compute_health`
    # takes a memory list and an event iterable and NEVER sees `root`, so
    # there is no way for it to reach the episode subtree. Wiring the
    # gauge at the one entry point that has a root — the one both
    # production surfaces use — is what keeps `episode_volume` from
    # shipping as a permanent null while every hand-built unit fixture
    # passes.
    #
    # Stat-only (`EpisodeStore.volume` parses no frontmatter) and
    # deliberately confined to this function: `memory_health` and
    # `bettermemory health` are curation-pass surfaces, not per-turn
    # ones. `memory_scope_overview` — the session-start hot path — does
    # not call this function and must not grow an episode walk.
    #
    # Guarded because the tiers fail separately: this is the one line in
    # the function that touches `<root>/episodes`, and it reaches it
    # through the bare `iterdir` in `EpisodeStore.iter_session_ids`. A
    # regular FILE where that directory belongs (a bad export, a sync
    # conflict copy) raises NotADirectoryError; a directory this process
    # cannot read raises PermissionError. Both are OSError, both arrive
    # after every bucket above is already computed, and not one of those
    # buckets reads an episode — so letting the raise through would trade
    # the whole memory-health surface for its one episode gauge.
    #
    # Degrades to None rather than a zeroed `EpisodeVolume`, because an
    # unwalkable subtree is not an empty one and the readers act on that
    # distinction: on None `render_text` omits its "Episodes:" line and
    # `to_dict` carries a null, where zeroes would have both state an
    # empty subtree over episodes nobody could count. None already means
    # "no reading" for a `compute_health` caller, which never has a root
    # to measure — this widens that case to "no root, or a root whose
    # episode subtree could not be walked" rather than inventing a second
    # null meaning.
    try:
        report.episode_volume = EpisodeStore(root).volume()
    except OSError:
        report.episode_volume = None
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
    "curation_counts",
    "is_hook_telemetry_event",
    "render_text",
    "render_json",
    "report_for_directory",
]
