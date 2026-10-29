"""Per-deployment memory effectiveness eval.

Three rates the existing event log makes computable today:

- ``memory_helped_rate`` — fraction of retrieval occurrences that
  produced an attested ``memory_record_use(applied)`` event carrying a
  non-empty ``claim_excerpt``. The denominator is per-event retrieval
  occurrences across ``memory_search`` / ``memory_list`` / ``memory_show``
  in the window, counted by memory id — a memory surfaced N times counts
  N (there is no per-turn dedup; the event schema carries no turn id).
  Two attestation tiers feed the numerator: model-explicit
  (the model called ``memory_record_use`` with excerpts) and
  hook-attributed (the Stop hook substring-matched a body sentence
  against the assistant reply and emitted an excerpt automatically).
  Both are evidence the retrieval was load-bearing; both count toward
  the numerator. The ``auto`` fallback (no excerpt, ``attribution="auto"``)
  is the bare-minimum signal and is excluded.
- ``endorsement_rate`` — among ``use`` events with ``outcome="applied"``,
  the fraction that were non-auto. A low rate means every applied is
  the server's auto-commit fallback; nothing — neither the model
  reaching for ``memory_record_use`` nor the Stop hook's heuristic
  attribution — produced evidence the retrieval shaped a reply.
- ``silent_miss_rate`` — ``search_miss`` events divided by
  miss-capable ``turn_audited`` events (``verdict != "no_signal"``).
  Audits the probe declined are excluded from the denominator and
  reported separately as ``turns_no_signal``, so a probe stuck at
  "declined" can't read as a healthy 0% miss rate. Both sides honor
  the same invalidation markers ``health_stats``' rollups honor: a
  ``silent_miss_cutoff`` event (a bulk acknowledgement, written by
  ``memory_admin(action="acknowledge_miss", before=...)``; 8.x wrote it
  with ``consolidate --acknowledge-misses-before``) drops every earlier
  ``turn_audited`` / ``search_miss`` event from numerator, both
  denominator buckets, and the triage buffer alike (latest
  ``cutoff_ts`` wins), and a
  ``miss_ack`` event (written by ``memory_acknowledge_miss``) drops
  the one referenced miss from the numerator while the audited
  denominator keeps its turn. When the caller passes
  ``tombstoned_ids``, a miss whose canonical top-hit memory has been
  tombstoned drops from the numerator (and the triage buffer) the
  same way — ``health_stats._silent_miss_stats``' filter #2 — while both
  denominator buckets keep their turns. The opt-in-retrieval
  contract's blind spot: turns where the model should have searched
  but didn't.

The methodology is described in ``docs/eval.md``. This module computes
the rates and composes the reports; its siblings, kept apart for the
package's 2,000-line bound (ENG-96), hold the rest: ``eval_model`` the
model the computations build on, ``eval_rates`` the walk and the counts
behind ``compute_eval`` (apart since ENG-97 split that function), and
``eval_render`` the renderers. None of them imports this module at run
time. The CLI wrapper is ``cli/eval.py``.

All numerator/denominator counts are stored on the report so consumers
can recompute (or weight) the rates differently. Confidence intervals
use the Wilson score interval at 95% (z=1.96), which behaves well at
small n and at the rate endpoints.

Attribution tier: events carry an ``attribution`` field with values
``"model"`` (explicit by AI), ``"hook"`` (Stop-hook substring match),
or ``"auto"`` (the auto-fallback). Older events without the field
fall back to ``"model"`` when ``auto`` is false and ``"auto"`` when
``auto`` is true. ``applied_total`` / ``applied_explicit`` and the
``endorsement_rate`` built on them keep the two-way auto/explicit
split they have always had (published, with recorded baselines);
``applied_model`` / ``applied_hook`` decompose the explicit half so a
consumer can tell a deliberate ``memory_record_use`` call from the
Stop hook's containment match without reaching into the raw events.
The tiering itself is ``health_model.applied_tier`` — one derivation, two
surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .eval_model import (
    DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
    DEFAULT_SILENT_MISS_LIMIT,
    DEFAULT_SINCE_SPEC,
    EARLIER_RULES,
    THRESHOLD_RULES,
    TOOLS_WITHOUT_TELEMETRY,
    WIDENING_RULES,
    _IN_SESSION_SIDE_EFFECT_KINDS as _IN_SESSION_SIDE_EFFECT_KINDS,
    _KNOWN_SIDE_EFFECT_KINDS as _KNOWN_SIDE_EFFECT_KINDS,
    _TOOL_EVENT_KIND_TO_TOOL as _TOOL_EVENT_KIND_TO_TOOL,
    ColdEndorsementMemoriesRow,
    RateCI,
    SilentMissCandidate,
    ThresholdRule,
    ThresholdSweepRow,
    ToolUsageRow,
    WideningFlaggedTurn,
    WideningMemoryRollup,
    WideningRuleDetail,
    parse_since,
    parse_timestamp_utc as parse_timestamp_utc,
    parse_window_bound as parse_window_bound,
    resolve_window as resolve_window,
    surface_rule_split,
    _collect_replayable_audits,
    _package_version,
    _parse_ts as _parse_ts,
    _probe_query_display,
    _rule_v1_top1_high,
    _wilson_interval as _wilson_interval,
    _with_rule_split,
)
from .eval_rates import (
    _AuditTally,
    _EvalWalk,
    _cold_endorsement_rows,
    _resolve_audit_telemetry,
    _walk_eval_events,
)
from .eval_render import (
    render_report_markdown,
    render_text,
    render_threshold_sweep_text,
    render_tool_usage_text,
    render_widening_detail_text,
    render_widening_preview_text,
)
from .health import _HOOKLESS_REASON  # one sentence, shared — see its comment
from .models import Memory, first_summary_line
from .time_utils import isoformat_utc_optional


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EvalReport:
    """Output of ``compute_eval``. Self-describing — every count, every
    row, every CI is on the dataclass so a JSON dump round-trips."""

    generated_at: datetime
    window_seconds: int | None  # None = "all"
    scope_filter: str | None
    threshold_rule: (
        str  # currently the rule the most-recent miss event used, or "" if none
    )
    total_events_scanned: int
    # Events whose own `ts` falls inside the `--since` window — the
    # window-scoped twin of `total_events_scanned` (which counts the
    # WHOLE log, because the invalidation markers are resolved before
    # the window filter). Equal to `total_events_scanned` when the
    # window is all-time.
    #
    # Scope of the claim, from an enumeration of the module rather than
    # from memory (an earlier version of this comment guessed, and was
    # wrong about which surfaces publish a count).
    #
    # Dataclasses in the eval*.py family declaring `total_events_scanned`, all
    # six of which now also declare `events_in_window`: `EvalReport`,
    # `ThresholdSweepReport`, `WideningPreviewReport`,
    # `WideningDetailReport`, `ToolUsageReport`, and the internal
    # `_ReplayableAudits` walk result. They are separate dataclasses, so
    # THIS field governs only this report; the twins move together by
    # convention, pinned below.
    #
    # Surfaces that publish an event count, and which tally each reads:
    #   - the four "Events scanned" text rows, each under a
    #     "— last {window}" header — `render_text`,
    #     `render_threshold_sweep_text`, `render_widening_preview_text`,
    #     `render_tool_usage_text` — all read `events_in_window`.
    #   - `_md_denominator_note`'s per-column line — `events_in_window`.
    #   - the five published `to_dict`s (the four above plus
    #     `WideningDetailReport`, which has no text event-count row but
    #     is dumped verbatim by `--widening-preview --detail --json`) —
    #     each emits BOTH keys, so a JSON consumer picks the one it
    #     means instead of being handed an all-time figure next to
    #     `window_seconds`.
    #   - `ReportDocument.total_events` is deliberately NOT windowed: it
    #     is fed from the all-time sub-report and printed on the
    #     markdown "Store shape" line, which is an all-time statement.
    #
    # `tests/test_eval.py::TestWindowedEventCounts` pins the four text
    # renderers and the detail JSON; `TestEventCountTwinEnumeration`
    # AST-scans eval*.py so a sixth surface cannot be added without
    # its twin.
    events_in_window: int

    retrieval_occurrences: int  # denominator for memory_helped_rate
    explicit_endorsements_with_excerpt: int  # numerator
    applied_total: int  # denominator for endorsement_rate
    applied_explicit: int  # numerator
    turns_audited: int  # denominator for silent_miss_rate (miss-capable only)
    turns_no_signal: int  # audits the probe declined (excluded from the rate)
    silent_misses: int  # numerator

    memory_helped_rate: RateCI = field(
        default_factory=lambda: RateCI(0, 0, None, None, None)
    )
    endorsement_rate: RateCI = field(
        default_factory=lambda: RateCI(0, 0, None, None, None)
    )
    silent_miss_rate: RateCI = field(
        default_factory=lambda: RateCI(0, 0, None, None, None)
    )

    cold_endorsement_memories_rows: list[ColdEndorsementMemoriesRow] = field(
        default_factory=list
    )
    cold_endorsement_memories_total: int = 0
    endorsement_min_retrievals: int = DEFAULT_ENDORSEMENT_MIN_RETRIEVALS
    # Honesty gate on the bucket above (2026-08-30, parity with
    # health's `telemetry_coverage.cold_endorsement_suppressed`): True
    # when the walked log carries zero Stop-hook settlement telemetry
    # (`health_model.is_hook_telemetry_event`, counted over the WHOLE log —
    # not the window — matching health's "did settlement
    # telemetry EVER reach this log" semantics), in which case the rows
    # and total above are empty BY CONSTRUCTION, not because the store
    # is clean. Serialised under `cold_endorsement_memories` with the
    # shared `health_model._HOOKLESS_REASON` sentence so the two surfaces
    # cannot explain the same silence two different ways.
    cold_endorsement_suppressed: bool = False

    silent_miss_recent: list[SilentMissCandidate] = field(default_factory=list)

    # Repeat audits excluded from the denominators (3.14+ dedup) and
    # per-model telemetry slices ({model: {audited, no_signal,
    # misses}}, from the Stop hook's transcript-derived `client_model`
    # stamp). Both additive: zero / empty on pre-3.14 logs.
    repeat_audits: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    # The window's absolute bounds: `window_start` is `--since` (given as
    # a timestamp, or `now` minus a relative span), `window_end` is
    # `--until`. None is open on that side.
    window_start: datetime | None = None
    window_end: datetime | None = None
    # The write review's counters over the same window (D1, DESIGN items
    # 3, 12 and 14): `review_counters.ReviewReport.to_dict()`. None for a
    # report built without them.
    review: dict[str, Any] | None = None

    # `applied_explicit` split by producer — `applied_model +
    # applied_hook == applied_explicit`, always. Additive: the
    # `endorsement_rate` numerator stays `applied_explicit`, because
    # that rate has a recorded baseline and the hook's attribution IS
    # evidence the retrieval landed.
    #
    # What the pair adds is the ability to ask a question the single
    # number cannot answer. The Stop hook's containment matcher emits
    # `auto=False, attribution="hook"` — the same shape an explicit
    # model call produces — so on a hook-wired store `applied_explicit`
    # is dominated by "a phrase from the memory appeared in the reply",
    # not by "the model called memory_record_use". Reading the former
    # as deliberate endorsement overstates the model's engagement with
    # memory, which is precisely the metric this module exists to
    # measure honestly. Tiering is `health_model.applied_tier`, shared so the
    # two surfaces cannot derive three tiers two ways.
    applied_model: int = 0
    applied_hook: int = 0
    # The recall surface's section over the same window (D11):
    # `recall_counters.compute_surface_report`, with its split on the show
    # rows' rule and the membership delta (`surface_rule_split`, design
    # 8.11). None when the window holds none of the surface's rows, and
    # then the key is left out of `to_dict` and no line of it is rendered,
    # so a log written before the surface reports byte for byte as it did.
    surfaces: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "window": {
                "since": isoformat_utc_optional(self.window_start),
                "until": isoformat_utc_optional(self.window_end),
            },
            "scope_filter": self.scope_filter,
            "threshold_rule": self.threshold_rule,
            "total_events_scanned": self.total_events_scanned,
            "events_in_window": self.events_in_window,
            "counts": {
                "retrieval_occurrences": self.retrieval_occurrences,
                "explicit_endorsements_with_excerpt": self.explicit_endorsements_with_excerpt,
                "applied_total": self.applied_total,
                "applied_explicit": self.applied_explicit,
                # The explicit half, split by producer. Sums to
                # `applied_explicit` exactly.
                "applied_model": self.applied_model,
                "applied_hook": self.applied_hook,
                "turns_audited": self.turns_audited,
                "turns_no_signal": self.turns_no_signal,
                "silent_misses": self.silent_misses,
                "repeat_audits": self.repeat_audits,
            },
            "by_model": self.by_model,
            "memory_helped_rate": self.memory_helped_rate.to_dict(),
            "endorsement_rate": self.endorsement_rate.to_dict(),
            "silent_miss_rate": self.silent_miss_rate.to_dict(),
            "cold_endorsement_memories": {
                "min_retrievals": self.endorsement_min_retrievals,
                "total": self.cold_endorsement_memories_total,
                "rows": [r.to_dict() for r in self.cold_endorsement_memories_rows],
                "suppressed": self.cold_endorsement_suppressed,
                "suppressed_reason": (
                    _HOOKLESS_REASON if self.cold_endorsement_suppressed else None
                ),
            },
            "silent_miss_recent": [c.to_dict() for c in self.silent_miss_recent],
            "review": self.review,
        }
        if self.surfaces is not None:
            out["surfaces"] = self.surfaces
        return out


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _eval_report(
    *,
    now: datetime,
    cutoff: datetime | None,
    window_end: datetime | None,
    scope: str | None,
    floor: int,
    walk: _EvalWalk,
    tally: _AuditTally,
    cold_rows: list[ColdEndorsementMemoriesRow],
    cold_total: int,
    cold_suppressed: bool,
) -> EvalReport:
    """Build the ``EvalReport`` from the walk, the audit tally and the cold rows."""
    from .recall_counters import compute_surface_report
    from .review_counters import compute_review_report

    if cutoff is None:
        window_seconds = None
    else:
        window_seconds = int(((window_end or now) - cutoff).total_seconds())
    report = EvalReport(
        generated_at=now,
        window_seconds=window_seconds,
        window_start=cutoff,
        window_end=window_end,
        review=compute_review_report(
            walk.review_rows, start=cutoff, end=window_end, now=now
        ).to_dict(),
        surfaces=_with_rule_split(
            compute_surface_report(walk.surface_rows, start=cutoff, end=window_end),
            walk.surface_rows,
            start=cutoff,
            end=window_end,
        ),
        scope_filter=scope,
        threshold_rule=tally.threshold_rule,
        total_events_scanned=walk.total_events_scanned,
        events_in_window=walk.events_in_window,
        retrieval_occurrences=walk.retrieval_occurrences,
        explicit_endorsements_with_excerpt=walk.explicit_endorsements_with_excerpt,
        applied_total=walk.applied_total,
        applied_explicit=walk.applied_explicit,
        applied_model=walk.applied_model,
        applied_hook=walk.applied_hook,
        turns_audited=tally.turns_audited,
        turns_no_signal=tally.turns_no_signal,
        silent_misses=tally.silent_misses,
        cold_endorsement_memories_rows=cold_rows,
        cold_endorsement_memories_total=cold_total,
        endorsement_min_retrievals=floor,
        cold_endorsement_suppressed=cold_suppressed,
        silent_miss_recent=tally.silent_miss_buffer,
        repeat_audits=tally.repeat_audits,
        by_model=tally.by_model,
    )
    report.memory_helped_rate = RateCI.from_counts(
        walk.explicit_endorsements_with_excerpt, walk.retrieval_occurrences
    )
    report.endorsement_rate = RateCI.from_counts(
        walk.applied_explicit, walk.applied_total
    )
    report.silent_miss_rate = RateCI.from_counts(
        tally.silent_misses, tally.turns_audited
    )
    return report


def compute_eval(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    since: timedelta | datetime | None = None,
    scope: str | None = None,
    endorsement_min_retrievals: int = DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
    silent_miss_limit: int = DEFAULT_SILENT_MISS_LIMIT,
    tombstoned_ids: set[str] | None = None,
    until: timedelta | datetime | None = None,
) -> EvalReport:
    """Build an ``EvalReport`` from active memories + the event stream.

    ``events`` is expected in chronological order (``iter_all_events``
    produces this). ``now`` defaults to the current UTC instant.

    ``since`` filters events to those with ``ts >= now - since``;
    ``None`` admits all events. An aware ``datetime`` is an absolute
    start instead. ``until`` (a datetime, or a span read back from
    ``now``) closes the window: events with ``ts >= until`` are left
    out. The ``review`` section counts the review chains issued in the
    same window, each judged from rows up to 30 minutes after ``until``
    (``review_counters``); ``scope`` does not filter it. ``scope`` filters to events that
    reference at least one memory tagged with that scope — search /
    show / use events all carry a memory id and are mapped to scopes
    via the live store. ``turn_audited`` and ``search_miss`` are
    *not* filtered by scope (they're per-turn), so the silent-miss
    rate reflects the global cadence regardless of scope filter.

    Silent-miss telemetry honors the same two invalidation markers
    ``health.compute_health`` honors, so
    every surface reporting ``silent_miss_rate`` agrees over the same
    event stream: a ``silent_miss_cutoff`` event (written by
    ``memory_admin(action="acknowledge_miss", before=...)``; 8.x wrote it
    with ``consolidate --acknowledge-misses-before``) drops
    ``turn_audited`` / ``search_miss`` events earlier than the latest
    ``cutoff_ts`` from the numerator, both denominator buckets, and
    the triage buffer — an earlier cutoff seen later in the log cannot
    shrink the window — and a ``miss_ack`` event (written by
    ``memory_acknowledge_miss``) drops the one referenced miss from
    the numerator while the denominator keeps its turn. Both markers
    are global: they apply even when their own ``ts`` falls outside
    the ``since`` window, as health reads them over the whole log.
    Streams carrying no markers count exactly as before.

    ``tombstoned_ids`` mirrors ``health_stats._silent_miss_stats``' filter
    #2: a ``search_miss`` whose canonical top-hit id
    (``top_hits[0].id``) is in the set drops from the numerator and
    the triage buffer — once the memory is gone the miss is no longer
    actionable — while both audited denominator buckets keep their
    turns (``turn_audited`` carries no per-memory payload). Default
    ``None`` preserves the tombstone-blind behavior byte-identically,
    so callers without a store handle (the comparative harness, the
    scripted driver) are untouched; the CLI wrapper enumerates the
    real set via ``store.load_tombstones()``, same as
    ``health.report_for_store``. Legacy ``top_hit_ids``-shaped
    events carry no canonical top-hit id and fall through the filter
    (health's can't-prove-tombstoned conservative read).

    A memory that's been tombstoned still counts toward retrieval /
    use occurrences — we attribute via id, not via live status — but
    it cannot participate in cold-endorsement rows because we need
    its body/scopes for display.

    The ``cold_endorsement_memories`` bucket mirrors health's on both
    axes the 2026-08-30 recalibration touched: the per-memory floor
    count is SEARCH deliveries only (``memory_list`` browses and
    ``memory_show`` pinpoint reads keep feeding
    ``retrieval_occurrences`` but not the floor — see the
    ``search_delivery_count`` comment in ``eval_rates._EvalWalk``),
    and the bucket rides the same Stop-hook telemetry honesty gate: on
    a log with zero ``health_model.is_hook_telemetry_event`` rows — measured
    over the whole log, not the window — the rows are skipped entirely
    and ``cold_endorsement_suppressed`` says so, because
    ``explicit_applied_count == 0`` is uninformative when nothing was
    ever in a position to attribute an apply.
    """
    now = now or datetime.now(timezone.utc)
    cutoff, window_end = resolve_window(since, until, now=now)
    tombstoned_ids = tombstoned_ids or set()

    # Live store: id → memory, for cold-endorsement rows + scope filter.
    by_id: dict[str, Memory] = {m.id: m for m in memories}

    # Scope filter: a memory passes if `scope` is in its scopes.
    def passes_scope(memory_id: str | None) -> bool:
        if scope is None:
            return True
        if memory_id is None:
            return False
        mem = by_id.get(memory_id)
        if mem is None:
            # Tombstoned or fabricated id — scope-filtered queries can't
            # attribute it, so it doesn't count.
            return False
        return scope in mem.scopes

    walk = _walk_eval_events(events, cutoff, window_end, passes_scope)
    tally = _resolve_audit_telemetry(walk, tombstoned_ids, silent_miss_limit)
    floor = max(1, int(endorsement_min_retrievals))
    cold_rows, cold_total, cold_suppressed = _cold_endorsement_rows(walk, by_id, floor)
    return _eval_report(
        now=now,
        cutoff=cutoff,
        window_end=window_end,
        scope=scope,
        floor=floor,
        walk=walk,
        tally=tally,
        cold_rows=cold_rows,
        cold_total=cold_total,
        cold_suppressed=cold_suppressed,
    )


# ---------------------------------------------------------------------------
# Threshold-rule sweep — replay logged misses under alternative rules
# ---------------------------------------------------------------------------


@dataclass
class ThresholdSweepReport:
    """Counterfactual rollup of how many logged search_miss events each
    rule would have flagged.

    `replayable_misses` is the denominator — `search_miss` events that
    carry the `top_hits` array. Pre-2.6.4 hook-originated events
    wrote `top_hit_ids` instead and can't be replayed; those land in
    `skipped_legacy_event_count` so the report stays honest about how
    much of the history was actually replayed.

    The `v1_top1_high` row is always present (computing its replay
    over the same events is the validation check — it must equal
    `replayable_misses` exactly, otherwise the helper has drifted
    from the production rule). `v1_drift` carries the difference
    (`replayable_misses - v1_would_flag`); a non-zero value means
    real log data contains events that *should* have tripped v1 but
    don't under the in-process rule — typically a sign the rule
    diverged from production or a future log shape introduced an
    event that the production rule no longer matches. The renderer
    surfaces a warning line when this drifts.
    """

    generated_at: datetime
    window_seconds: int | None
    total_events_scanned: int
    # Window-scoped twin of `total_events_scanned` — see the identically
    # named field on `EvalReport`. `total_events_scanned` counts the
    # whole log here too (the `silent_miss_cutoff` marker is resolved
    # ahead of the window filter), so the renderer's "Events scanned"
    # row under a "— last {window}" header must read THIS one.
    events_in_window: int
    replayable_misses: int
    skipped_legacy_event_count: int
    v1_drift: int = 0
    rows: list[ThresholdSweepRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "total_events_scanned": self.total_events_scanned,
            "events_in_window": self.events_in_window,
            "replayable_misses": self.replayable_misses,
            "skipped_legacy_event_count": self.skipped_legacy_event_count,
            "v1_drift": self.v1_drift,
            "rows": [r.to_dict() for r in self.rows],
        }


def _buffer_sweep_misses(
    events: Iterable[dict[str, Any]], cutoff: datetime | None
) -> tuple[
    list[tuple[datetime | None, list[dict[str, Any]] | None, int]],
    int,
    int,
    datetime | None,
]:
    """Walk the log for in-window search_miss rows; returns them, the two event counts and the latest cutoff."""
    # In-window `search_miss` rows buffered as (ts, top_hits-or-None,
    # recent) for post-pass resolution against the invalidation cutoff —
    # same buffer-then-resolve shape as `compute_eval`, because a
    # `silent_miss_cutoff` later in the log must be able to retract
    # earlier telemetry. `top_hits is None` marks the legacy
    # `top_hit_ids`-only shape, buffered rather than counted inline so a
    # cutoff-invalidated legacy event doesn't inflate the honesty
    # footnote with a row that was never valid telemetry.
    buffered: list[tuple[datetime | None, list[dict[str, Any]] | None, int]] = []
    total_events_scanned = 0
    events_in_window = 0
    latest_miss_cutoff: datetime | None = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        total_events_scanned += 1
        # Window membership decided up front — BEFORE the marker
        # short-circuit and the kind dispatch below, both of which
        # `continue` — so `events_in_window` covers exactly the
        # population `total_events_scanned` does, only window-scoped.
        # Same predicate (and same missing/unparseable-ts-is-out-of-
        # window read) as the per-event filter further down.
        if cutoff is None:
            events_in_window += 1
        else:
            ev_ts = _parse_ts(ev.get("ts"))
            if ev_ts is not None and ev_ts >= cutoff:
                events_in_window += 1
        kind = ev.get("kind")
        # Marker resolution BEFORE the `since` window filter — global
        # semantics, mirroring `compute_eval`: a cutoff whose own ts
        # falls outside the window still applies, so a windowed sweep
        # can't replay events every rate surface has already
        # invalidated. Latest `cutoff_ts` wins; a malformed value
        # parses to None and is ignored. `miss_ack` events are
        # deliberately not resolved here — see the docstring of
        # `compute_threshold_sweep`.
        if kind == "silent_miss_cutoff":
            parsed_cutoff = _parse_ts(ev.get("cutoff_ts"))
            if parsed_cutoff is not None and (
                latest_miss_cutoff is None or parsed_cutoff > latest_miss_cutoff
            ):
                latest_miss_cutoff = parsed_cutoff
            continue
        if kind != "search_miss":
            continue
        ts = _parse_ts(ev.get("ts"))
        if cutoff is not None and (ts is None or ts < cutoff):
            continue
        top_hits = ev.get("top_hits")
        if not isinstance(top_hits, list):
            # Legacy hook shape carried `top_hit_ids` only — no relevance
            # label, so no rule can re-evaluate. Buffer as legacy so a
            # cutoff-surviving row still lands in the honesty footnote.
            if isinstance(ev.get("top_hit_ids"), list):
                buffered.append((ts, None, 0))
            continue
        # Element guard, not just the container: the event log is
        # plaintext + git-synced + hand-editable, so a well-formed list
        # whose hit at a rule-read position isn't a dict clears the list
        # check above but detonates at `top_hits[0].get(...)` in
        # `_rule_v1_top1_high` (`top_hits=["junk"]`) or, for a MIXED
        # `[{good high hit}, "junk"]`, at `top_hits[1].get(...)` in
        # `_rule_v3_top1_high_dominant` — which reads the SECOND hit, so
        # guarding index 0 alone (as the widening lane does, reading only
        # its top hit) is insufficient here; both index 0 AND index 1 are
        # checked. A non-dict at a read position leaves no replayable
        # decision, so the row buckets as legacy — same None sentinel,
        # same cutoff resolution — the way the widening lane counts a
        # non-dict top hit as feature-less.
        if (top_hits and not isinstance(top_hits[0], dict)) or (
            len(top_hits) >= 2 and not isinstance(top_hits[1], dict)
        ):
            buffered.append((ts, None, 0))
            continue
        recent = ev.get("recent_retrieval_count")
        # `bool` ⊂ `int` — same caveat as `_silent_miss_from_event`.
        if not isinstance(recent, int) or isinstance(recent, bool):
            recent = 0
        buffered.append((ts, top_hits, recent))
    return buffered, total_events_scanned, events_in_window, latest_miss_cutoff


def _resolve_sweep_buffer(
    buffered: list[tuple[datetime | None, list[dict[str, Any]] | None, int]],
    latest_miss_cutoff: datetime | None,
) -> tuple[list[tuple[list[dict[str, Any]], int]], int]:
    """Drop the rows the latest cutoff retracts; returns the replayable rows and the legacy count."""
    # Resolve against the cutoff: an invalidated event (ts strictly
    # before `cutoff_ts`; an unparseable ts drops too once a cutoff
    # exists — `health_stats._count_post_cutoff`'s conservative read)
    # vanishes from the replay set AND the legacy footnote, as if it
    # were never logged. With no cutoff in the stream this loop is the
    # identity split the pre-buffer inline code performed.
    replayable: list[tuple[list[dict[str, Any]], int]] = []
    legacy_skipped = 0
    for ts, top_hits, recent in buffered:
        if latest_miss_cutoff is not None and (ts is None or ts < latest_miss_cutoff):
            continue
        if top_hits is None:
            legacy_skipped += 1
        else:
            replayable.append((top_hits, recent))
    return replayable, legacy_skipped


def _sweep_rows(
    rules_in_use: dict[str, ThresholdRule],
    replayable: list[tuple[list[dict[str, Any]], int]],
) -> tuple[int, list[ThresholdSweepRow]]:
    """Replay each rule over the replayable misses; returns v1's count and the sorted rows."""
    # Compute v1's flag count first — it acts as the reference for
    # `delta_from_v1` on every other row.
    v1_rule = rules_in_use.get("v1_top1_high")
    if v1_rule is not None:
        v1_count = sum(
            1 for top_hits, recent in replayable if v1_rule.check(top_hits, recent)
        )
    else:
        v1_count = 0

    rows: list[ThresholdSweepRow] = []
    for rule_name, rule in rules_in_use.items():
        would_flag = sum(
            1 for top_hits, recent in replayable if rule.check(top_hits, recent)
        )
        delta = would_flag - v1_count
        if v1_count > 0:
            delta_pct: float | None = would_flag / v1_count
        else:
            delta_pct = None
        rows.append(
            ThresholdSweepRow(
                rule=rule_name,
                description=rule.description,
                would_flag=would_flag,
                delta_from_v1=delta,
                delta_pct=delta_pct,
            )
        )

    # Stable ordering: v1 first (the reference), then by would_flag
    # descending, then by name.
    def sort_key(row: ThresholdSweepRow) -> tuple[int, int, str]:
        v1_first = 0 if row.rule == "v1_top1_high" else 1
        return (v1_first, -row.would_flag, row.rule)

    rows.sort(key=sort_key)
    return v1_count, rows


def compute_threshold_sweep(
    events: Iterable[dict[str, Any]],
    *,
    rules: dict[str, ThresholdRule] | None = None,
    since: timedelta | None = None,
    now: datetime | None = None,
) -> ThresholdSweepReport:
    """Replay logged search_miss events against each named rule.

    Only `search_miss` events with a `top_hits` list participate;
    legacy `top_hit_ids` (pre-2.6.4 hook shape) lack the relevance
    label every rule needs and are counted in
    `skipped_legacy_event_count` so the denominator stays explicit.
    The sweep is a *relative* comparison among rules at least as
    strict as v1; a strictly looser rule would also flag turns
    where v1 didn't, which we can't replay from the log alone
    because the companion `turn_audited` event doesn't carry
    `top_hits`. That limitation is the calibration question
    `audit.py`'s docstring flags as open.

    The two invalidation markers the rate surfaces
    (`compute_eval` / `health.compute_health`) honor apply
    ASYMMETRICALLY here, and the asymmetry is deliberate:

    - `silent_miss_cutoff` (bulk, written by
      `memory_admin(action="acknowledge_miss", before=...)`; 8.x wrote
      it with `consolidate --acknowledge-misses-before`) IS honored: events
      earlier than the latest `cutoff_ts` drop from the replay set
      and the legacy footnote alike, as if never logged. The cutoff
      exists to retract batches flagged by a since-fixed code bug
      (e.g. the v2.7.3 cwd-suppression batch) — those events were
      never genuine rule decisions, so replaying them pollutes the
      "is v1 over-firing" calibration with noise no rule change can
      address. Resolution is global, like `compute_eval`'s: a cutoff
      whose own ts falls outside `since` still applies, and the
      latest `cutoff_ts` wins.
    - `miss_ack` (per-event, written by `memory_acknowledge_miss`)
      is deliberately NOT honored: an acked miss is a *confirmed
      false positive of the current rule* — exactly the calibration
      signal a stricter candidate is judged against ("would v2/v3/v4
      have declined to flag the miss a human had to retract?").
      Dropping acks would blind the sweep to precisely the events it
      exists to learn from. The rate surfaces drop them because they
      report *outstanding actionable misses*; the sweep replays
      *rule decisions*.
    """
    now = now or datetime.now(timezone.utc)
    cutoff: datetime | None = (now - since) if since is not None else None
    rules_in_use = rules or THRESHOLD_RULES

    buffered, total_events_scanned, events_in_window, latest_miss_cutoff = (
        _buffer_sweep_misses(events, cutoff)
    )

    replayable, legacy_skipped = _resolve_sweep_buffer(buffered, latest_miss_cutoff)

    v1_count, rows = _sweep_rows(rules_in_use, replayable)

    return ThresholdSweepReport(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        total_events_scanned=total_events_scanned,
        events_in_window=events_in_window,
        replayable_misses=len(replayable),
        skipped_legacy_event_count=legacy_skipped,
        v1_drift=len(replayable) - v1_count,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Widening preview — replay of candidate LOOSER rules over turn_audited
# ---------------------------------------------------------------------------
#
# The strict sweep above can only compare rules at least as strict as
# v1, because historically only `search_miss` events (v1 already fired)
# carried `top_hits`. Since 3.14 every miss-capable `turn_audited`
# event carries a compact `top_hits` payload with the RAW coverage
# features (matched_unique / query_unique / score) plus the shadow
# `relevance_v2` label — so a candidate rule that fires where v1
# didn't finally has a denominator to replay against. This lane is the
# calibration surface for the relevance-label widening (the
# "long natural-language queries land at medium" blind spot audit.py's
# docstring names).
#
# Honesty note on the baseline: the v1 count here is REPLAYED from the
# logged features (`_rule_v1_top1_high`), not read off the logged
# verdict. The production pipeline applies one suppression the event
# payload can't reproduce (the caller-in-top-hit-project arm), so both
# the v1 replay and every widening row overcount what production would
# flag — symmetrically. The delta between rows is the honest signal;
# the absolute counts are upper bounds.


@dataclass
class WideningPreviewReport:
    """Replay of `WIDENING_RULES` over miss-capable `turn_audited` events.

    `audits_with_features` is the denominator — turn_audited events
    carrying the 3.14+ `top_hits` payload. Older events (and producers
    that probed to a no-hit result) land in `audits_without_features`
    so the report is explicit about how much history was replayable.
    Repeat audits are excluded (`repeat_audits_skipped`), matching the
    rate surfaces. The bulk `silent_miss_cutoff` marker IS applied
    (`cutoff_retracted`): a replayable audit whose `ts` falls before the
    latest logged `cutoff_ts` was retracted by a bulk acknowledgement
    (`memory_admin(action="acknowledge_miss", before=...)`; 8.x wrote it
    with `consolidate --acknowledge-misses-before`) and is dropped from
    the replay, the same way
    `compute_eval` drops it from the rate surfaces. The marker used to
    be skipped on the reasoning that it only ever retracted pre-3.14
    batches no replay could see; cutoffs have long postdated the
    feature payload, so an unapplied one let retracted turns keep
    voting on the widening deltas.
    """

    generated_at: datetime
    window_seconds: int | None
    total_events_scanned: int
    # Window-scoped twin of `total_events_scanned` — see the identically
    # named field on `EvalReport`. The renderer's "Events scanned" row
    # sits under a "— last {window}" header and must read THIS one.
    events_in_window: int
    audits_with_features: int
    audits_without_features: int
    repeat_audits_skipped: int
    v1_baseline_flagged: int
    # Replayable audits retracted by the latest `silent_miss_cutoff`
    # marker — counted, not silently dropped, so the report stays
    # explicit about how much history it replayed. Defaulted so a
    # caller constructing the report without the walk still gets a
    # well-formed record.
    cutoff_retracted: int = 0
    rows: list[ThresholdSweepRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "total_events_scanned": self.total_events_scanned,
            "events_in_window": self.events_in_window,
            "audits_with_features": self.audits_with_features,
            "audits_without_features": self.audits_without_features,
            "repeat_audits_skipped": self.repeat_audits_skipped,
            "cutoff_retracted": self.cutoff_retracted,
            "v1_baseline_flagged": self.v1_baseline_flagged,
            "rows": [r.to_dict() for r in self.rows],
        }


def compute_widening_preview(
    events: Iterable[dict[str, Any]],
    *,
    rules: dict[str, ThresholdRule] | None = None,
    since: timedelta | None = None,
    now: datetime | None = None,
) -> WideningPreviewReport:
    """Replay candidate looser rules over the turn_audited stream.

    Walks miss-capable (`verdict != "no_signal"`, non-repeat)
    `turn_audited` events carrying `top_hits` and counts, per rule in
    `rules` (default `WIDENING_RULES`), how many turns it would have
    flagged — alongside the replayed v1 baseline the deltas are
    measured against. See the section comment above for why the
    baseline is replayed rather than read off the logged verdict.
    """
    now = now or datetime.now(timezone.utc)
    rules_in_use = rules or WIDENING_RULES

    walk = _collect_replayable_audits(events, since=since, now=now)
    total_events_scanned = walk.total_events_scanned
    with_features = walk.with_features
    without_features = walk.without_features
    repeats_skipped = walk.repeats_skipped
    v1_count = 0
    counts: dict[str, int] = {name: 0 for name in rules_in_use}
    for _, top_hits, recent in walk.rows:
        if _rule_v1_top1_high(top_hits, recent):
            v1_count += 1
        for name, rule in rules_in_use.items():
            if rule.check(top_hits, recent):
                counts[name] += 1

    rows = [
        ThresholdSweepRow(
            rule=name,
            description=rules_in_use[name].description,
            would_flag=counts[name],
            delta_from_v1=counts[name] - v1_count,
            delta_pct=(counts[name] / v1_count) if v1_count > 0 else None,
        )
        for name in rules_in_use
    ]
    rows.sort(key=lambda r: (-r.would_flag, r.rule))

    return WideningPreviewReport(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        total_events_scanned=total_events_scanned,
        events_in_window=walk.events_in_window,
        audits_with_features=with_features,
        audits_without_features=without_features,
        repeat_audits_skipped=repeats_skipped,
        cutoff_retracted=walk.cutoff_retracted,
        v1_baseline_flagged=v1_count,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Widening detail — per-flagged-turn evidence for precision labeling
# ---------------------------------------------------------------------------
#
# The counting lane above answers "how MANY turns would a widened rule
# flag"; the flip decision the declared bars gate on needs "WHICH
# turns, against WHICH memories, on what evidence" — without that, a
# big delta is uninterpretable (it could be one over-matched memory or
# a genuinely wide blind spot). This lane dumps the flagged cohort so
# a human (or the model itself) can precision-label it.
#
# Evidence per turn is exactly what the `turn_audited` event already
# carries — no new logging: the redacted `probe_query`
# ({hash, preview, len}; a log written before 9.0.0 can carry the
# verbatim string), the top hit's raw coverage pair, both
# relevance labels, and the hit's memory id joined against the active
# store + tombstone log for a summary. Rendering never widens
# exposure beyond what the log already holds.


@dataclass
class WideningDetailReport:
    """Per-turn evidence behind `WideningPreviewReport`'s counts.

    Header counters are computed from the same
    `_collect_replayable_audits` walk as the counting lane, so the two
    reports over the same stream always agree.
    """

    generated_at: datetime
    window_seconds: int | None
    total_events_scanned: int
    # Window-scoped twin of `total_events_scanned` — see the identically
    # named field on `EvalReport`. This report has no "Events scanned"
    # text row, but `to_dict` publishes the count next to
    # `window_seconds` and is dumped verbatim by
    # `eval --widening-preview --detail --json`, so the JSON consumer
    # needs the window-scoped figure just as much as a text reader does.
    events_in_window: int
    audits_with_features: int
    audits_without_features: int
    repeat_audits_skipped: int
    v1_baseline_flagged: int
    # Same counter as `WideningPreviewReport.cutoff_retracted`, from the
    # same walk, so the two lanes agree on what was replayable.
    cutoff_retracted: int = 0
    rules: list[WideningRuleDetail] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "total_events_scanned": self.total_events_scanned,
            "events_in_window": self.events_in_window,
            "audits_with_features": self.audits_with_features,
            "audits_without_features": self.audits_without_features,
            "repeat_audits_skipped": self.repeat_audits_skipped,
            "cutoff_retracted": self.cutoff_retracted,
            "v1_baseline_flagged": self.v1_baseline_flagged,
            "rules": [r.to_dict() for r in self.rules],
        }


def _flagged_turn(
    ev: dict[str, Any],
    top_hits: list[dict[str, Any]],
    v1_flags: bool,
    resolve: Callable[[str], tuple[str, str | None, list[str] | None]],
) -> WideningFlaggedTurn:
    """Build one flagged turn's evidence from its audit event; returns the turn."""
    top = top_hits[0] if isinstance(top_hits[0], dict) else {}
    preview, length, digest = _probe_query_display(ev.get("probe_query"))
    raw_score = top.get("score")
    score = (
        float(raw_score)
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool)
        else None
    )
    matched = top.get("matched_unique")
    q_unique = top.get("query_unique")
    memory_id = str(top.get("id") or "?")
    status, summary, scopes = resolve(memory_id)
    session = ev.get("session_id") or ev.get("session")
    return WideningFlaggedTurn(
        ts=ev.get("ts") if isinstance(ev.get("ts"), str) else None,
        session_id=session if isinstance(session, str) else None,
        client_model=(
            ev.get("client_model") if isinstance(ev.get("client_model"), str) else None
        ),
        probe_query_preview=preview,
        probe_query_len=length,
        probe_query_hash=digest,
        top_hit_id=memory_id,
        top_hit_score=score,
        relevance_v1=(
            top.get("relevance") if isinstance(top.get("relevance"), str) else None
        ),
        relevance_v2=(
            top.get("relevance_v2")
            if isinstance(top.get("relevance_v2"), str)
            else None
        ),
        matched_unique=(
            matched
            if isinstance(matched, int) and not isinstance(matched, bool)
            else None
        ),
        query_unique=(
            q_unique
            if isinstance(q_unique, int) and not isinstance(q_unique, bool)
            else None
        ),
        v1_also_flagged=v1_flags,
        memory_status=status,
        memory_summary=summary,
        memory_scopes=scopes,
    )


def _rule_turns_and_rollup(
    turns: list[WideningFlaggedTurn],
    sessions_by_memory: dict[str, set[str | None]],
) -> tuple[list[WideningFlaggedTurn], list[WideningMemoryRollup]]:
    """Order one rule's flagged turns and roll them up by memory; returns both lists."""
    # Newest first; undated rows sink to the end in stream order.
    # Under reverse=True the LOWEST key lands last, so undated rows
    # take rank 0 and dated ones rank 1 — the (0, ...) spelling put
    # undated rows at the TOP of a "newest first" list.
    turns_sorted = sorted(
        turns,
        key=lambda t: (1, t.ts) if t.ts is not None else (0, ""),
        reverse=True,
    )
    rollup: dict[str, WideningMemoryRollup] = {}
    for t in turns:
        row = rollup.get(t.top_hit_id)
        if row is None:
            rollup[t.top_hit_id] = WideningMemoryRollup(
                memory_id=t.top_hit_id,
                count=1,
                distinct_sessions=0,  # filled after the loop
                status=t.memory_status,
                summary=t.memory_summary,
                scopes=t.memory_scopes,
                score_min=t.top_hit_score,
                score_max=t.top_hit_score,
            )
            continue
        row.count += 1
        if t.top_hit_score is not None:
            row.score_min = (
                t.top_hit_score
                if row.score_min is None
                else min(row.score_min, t.top_hit_score)
            )
            row.score_max = (
                t.top_hit_score
                if row.score_max is None
                else max(row.score_max, t.top_hit_score)
            )
    for memory_id, row in rollup.items():
        row.distinct_sessions = len(sessions_by_memory.get(memory_id, set()))
    by_memory = sorted(rollup.values(), key=lambda r: (-r.count, r.memory_id))
    return turns_sorted, by_memory


def compute_widening_detail(
    events: Iterable[dict[str, Any]],
    *,
    memories: list[Memory] | None = None,
    tombstoned_ids: set[str] | None = None,
    rules: dict[str, ThresholdRule] | None = None,
    since: timedelta | None = None,
    now: datetime | None = None,
) -> WideningDetailReport:
    """Collect per-turn evidence for every widening-rule flag.

    Same replay semantics as `compute_widening_preview` (shared walk;
    see `_ReplayableAudits`), but instead of counting it materialises
    each flagged turn with its logged evidence, plus a per-memory
    rollup of where the flags concentrate. `memories` /
    `tombstoned_ids` resolve top-hit ids to summaries the way
    `compute_eval` does; omitted, every hit reports
    ``memory_status="unknown"``.
    """
    now = now or datetime.now(timezone.utc)
    rules_in_use = rules or WIDENING_RULES
    by_id = {m.id: m for m in (memories or [])}
    tombstones = tombstoned_ids or set()

    def _resolve(memory_id: str) -> tuple[str, str | None, list[str] | None]:
        mem = by_id.get(memory_id)
        if mem is not None:
            return "active", first_summary_line(mem.body), list(mem.scopes)
        if memory_id in tombstones:
            return "tombstoned", None, None
        return "unknown", None, None

    walk = _collect_replayable_audits(events, since=since, now=now)
    v1_count = 0
    flagged_by_rule: dict[str, list[WideningFlaggedTurn]] = {
        name: [] for name in rules_in_use
    }
    sessions_by_rule_memory: dict[str, dict[str, set[str | None]]] = {
        name: {} for name in rules_in_use
    }
    for ev, top_hits, recent in walk.rows:
        v1_flags = _rule_v1_top1_high(top_hits, recent)
        if v1_flags:
            v1_count += 1
        firing = [
            name for name, rule in rules_in_use.items() if rule.check(top_hits, recent)
        ]
        if not firing:
            continue
        turn = _flagged_turn(ev, top_hits, v1_flags, _resolve)
        for name in firing:
            flagged_by_rule[name].append(turn)
            sessions_by_rule_memory[name].setdefault(turn.top_hit_id, set()).add(
                turn.session_id
            )

    rule_details: list[WideningRuleDetail] = []
    for name, rule in rules_in_use.items():
        turns = flagged_by_rule[name]
        turns_sorted, by_memory = _rule_turns_and_rollup(
            turns, sessions_by_rule_memory[name]
        )
        rule_details.append(
            WideningRuleDetail(
                rule=name,
                description=rule.description,
                flagged_total=len(turns),
                beyond_v1=sum(1 for t in turns if not t.v1_also_flagged),
                turns=turns_sorted,
                by_memory=by_memory,
            )
        )
    rule_details.sort(key=lambda r: (-r.flagged_total, r.rule))

    return WideningDetailReport(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        total_events_scanned=walk.total_events_scanned,
        events_in_window=walk.events_in_window,
        audits_with_features=walk.with_features,
        audits_without_features=walk.without_features,
        repeat_audits_skipped=walk.repeats_skipped,
        cutoff_retracted=walk.cutoff_retracted,
        v1_baseline_flagged=v1_count,
        rules=rule_details,
    )


# ---------------------------------------------------------------------------
# Tool-usage rollup — per-MCP-tool call counts from the event log
# ---------------------------------------------------------------------------

# Event kinds recorded by an admin/CLI surface OUTSIDE any client
# session, under a fresh throwaway session id. Derived as the
# complement of the in-session subset rather than hand-listed, so a new
# entry on the roster in eval_model.py lands here automatically instead of
# quietly falling behind.
#
# INVARIANT: a "session" observed only through these kinds never
# existed as a client session.
#
# This roster is ONE OF TWO axes and is not usable on its own — see
# ``ADMIN_RECORDED_ATTRIBUTION_PREFIX`` below for the admin rows it
# structurally cannot catch. Consumers therefore call
# ``is_admin_recorded_event``; no other module in ``src/`` names this
# constant in code (doctor.py mentions it in prose, explaining why it
# calls the predicate instead).
# ``tests/test_eval.py::TestAdminRecordedParity`` enforces both halves
# mechanically: it AST-scans ``src/`` and ``tests/`` for any literal
# set of event kinds that looks like a fork of this roster and asserts
# each one equals this constant exactly, and it AST-scans ``src/`` for
# any module other than this one that names either axis constant —
# which is how a consumer wires up half the classification. Both scans
# self-test against a synthetic offender so neither can pass vacuously.
ADMIN_RECORDED_EVENT_KINDS: frozenset[str] = (
    _KNOWN_SIDE_EFFECT_KINDS - _IN_SESSION_SIDE_EFFECT_KINDS
)

# The SECOND exclusion axis, and the one kind-based exclusion
# structurally cannot cover: an admin CLI operation that records under
# a kind which is ALSO a legitimate in-session kind.
#
# ``bettermemory tombstones restore`` and ``bettermemory rename-scope``
# are the live instances. They write ``restore`` and ``rename_scope``
# rows, the exact shapes ``memory_admin`` produces in session, under a
# fresh throwaway ``SessionState()`` id, so excluding by kind would
# have to exclude those kinds wholesale and blind the tally to every
# real client session that used them. What separates the two is
# ATTRIBUTION: every admin CLI writer stamps
# ``attribution="cli_<operation>"`` (``cli_tombstones_restore``,
# ``cli_rename_scope``), while every in-session producer stamps
# ``"model"``, ``"hook"``, or ``"auto"`` (see the module docstring's
# attribution tier). A prefix rule rather than a hand-listed roster,
# deliberately: a new admin CLI operation lands on the correct side by
# construction instead of quietly inflating the count until someone
# notices.
#
# Scope of the exclusion is the SESSION TALLY ONLY. What the rows must
# not do is publish a session that never had a client attached to it.
ADMIN_RECORDED_ATTRIBUTION_PREFIX = "cli_"


def is_admin_recorded_event(ev: dict[str, Any]) -> bool:
    """True when ``ev`` was written by an admin/CLI surface rather than
    from inside a live client session.

    The single DEFINITION of that classification. Kind-based
    (``ADMIN_RECORDED_EVENT_KINDS``) catches the kinds only an admin
    surface ever emits; attribution-based
    (``ADMIN_RECORDED_ATTRIBUTION_PREFIX``) catches admin operations
    riding a kind that is also legitimately in-session. A
    non-string/absent ``attribution`` reads as in-session, which is the
    correct back-compat fall-through: every pre-attribution event came
    off a real client.

    Scope, stated precisely because the loose version of this sentence
    has been wrong twice: every caller that excludes admin-recorded
    events calls THIS, rather than testing one axis itself — that is
    the invariant, and ``TestAdminRecordedParity`` enforces it from the
    one side a static check can see: it fails any module in ``src/``
    outside this one that names either axis constant in code, and any
    literal set anywhere that forks the kind roster. It is NOT a claim
    that every session tally in the
    codebase excludes admin-recorded events; a tally that doesn't is a
    gap in that tally, and the fix is to call this, never to re-derive
    half the classification locally.
    """
    if ev.get("kind") in ADMIN_RECORDED_EVENT_KINDS:
        return True
    attribution = ev.get("attribution")
    return isinstance(attribution, str) and attribution.startswith(
        ADMIN_RECORDED_ATTRIBUTION_PREFIX
    )


@dataclass
class ToolUsageReport:
    """Output of ``compute_tool_usage``. Carries one row per known MCP
    tool — even the zero-count ones — so a downstream consumer can
    branch on "tool was never called" without a missing-key guard.

    ``unmapped_event_kinds`` carries the count of event-kind values
    that didn't map to any tool. A non-zero value here indicates the
    map drifted out of sync with the recorder — useful as a guardrail
    so an unmapped new event kind doesn't silently vanish from the
    rollup.
    """

    generated_at: datetime
    window_seconds: int | None
    total_events_scanned: int
    # Window-scoped twin of `total_events_scanned` — see the identically
    # named field on `EvalReport`. The renderer's "Events scanned" row
    # sits under a "— last {window}" header and must read THIS one.
    events_in_window: int
    total_tool_calls: int
    rows: list[ToolUsageRow] = field(default_factory=list)
    unmapped_event_kinds: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "total_events_scanned": self.total_events_scanned,
            "events_in_window": self.events_in_window,
            "total_tool_calls": self.total_tool_calls,
            "rows": [r.to_dict() for r in self.rows],
            "unmapped_event_kinds": dict(self.unmapped_event_kinds),
        }


def compute_tool_usage(
    events: Iterable[dict[str, Any]],
    *,
    since: timedelta | None = None,
    now: datetime | None = None,
) -> ToolUsageReport:
    """Roll up event log into per-MCP-tool call counts.

    ``since`` applies the same window semantics as ``compute_eval`` —
    events with ``ts < now - since`` are skipped. Pass ``None`` for
    all-time. Returns one row per known MCP tool (with zero-count
    rows preserved) plus a ``rows`` field sorted by descending count
    and a tally of unmapped event kinds so the map can be audited
    against the live recorder.
    """
    from .recall import is_settlement

    now = now or datetime.now(timezone.utc)
    cutoff: datetime | None = (now - since) if since is not None else None

    per_tool: dict[str, int] = {tool: 0 for tool in _TOOL_EVENT_KIND_TO_TOOL.values()}
    for tool in TOOLS_WITHOUT_TELEMETRY:
        per_tool[tool] = 0
    unmapped: dict[str, int] = {}
    total_events_scanned = 0
    events_in_window = 0
    total_tool_calls = 0

    for ev in events:
        if not isinstance(ev, dict):
            continue
        total_events_scanned += 1
        if cutoff is not None:
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts < cutoff:
                continue
        events_in_window += 1
        kind = ev.get("kind")
        if not isinstance(kind, str):
            continue
        if is_settlement(ev):
            # A pointer's settlement, recorded by the Stop
            # (`recall_settle.settle_shown`): no tool was called.
            continue
        mapped_tool = _TOOL_EVENT_KIND_TO_TOOL.get(kind)
        if mapped_tool is not None:
            per_tool[mapped_tool] += 1
            total_tool_calls += 1
        elif kind in _KNOWN_SIDE_EFFECT_KINDS:
            continue
        else:
            unmapped[kind] = unmapped.get(kind, 0) + 1

    rows: list[ToolUsageRow] = []
    for tool, count in per_tool.items():
        share = (count / total_tool_calls) if total_tool_calls > 0 else None
        has_telemetry = tool not in TOOLS_WITHOUT_TELEMETRY
        rows.append(
            ToolUsageRow(
                tool=tool, count=count, share=share, has_telemetry=has_telemetry
            )
        )
    # Descending by count, then by tool name for determinism. Untelemetered
    # rows always sort to the bottom (count is 0 and share is the same as
    # any other zero-count row, so the tie-break by name puts them where
    # they belong without a special case).
    rows.sort(key=lambda r: (-r.count, r.tool))

    return ToolUsageReport(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        total_events_scanned=total_events_scanned,
        events_in_window=events_in_window,
        total_tool_calls=total_tool_calls,
        rows=rows,
        unmapped_event_kinds=unmapped,
    )


# ---------------------------------------------------------------------------
# Publishable markdown report — every rollup above, one self-contained doc
# ---------------------------------------------------------------------------
#
# `bettermemory eval --report` renders the store's dogfood telemetry as
# ONE markdown artifact a maintainer can paste into a README, a blog
# post, or docs/eval-results.md without a redaction pass. That safety
# property is the feature, and it is a *tested contract*, not a hope
# (see the canary test in tests/test_eval.py): the renderer emits
# rates, counts, CIs, model names, and the static tool/rule registry
# names ONLY. It never touches the leak-capable fields the sub-reports
# carry for the interactive modes — `cold_endorsement_memories_rows`
# (memory summaries + scope names), `silent_miss_recent` (session ids
# + memory ids), `scope_filter`, or the log-derived `threshold_rule`
# string (rule names in the report come from the static
# `THRESHOLD_RULES` registry, so a hand-edited event can't inject
# text). No memory bodies, no probe queries (not even the redacted
# previews), no filesystem paths, no scope names, no session ids.
#
# The composition is deliberately NOT new measurement: `compute_report`
# only re-runs the existing computations (`compute_eval` twice — the
# `--since` window and all-time side by side, because the trend between
# the two windows is the story eval-results.md tells — plus
# `compute_threshold_sweep` and `compute_tool_usage` over the full
# log) and counts distinct sessions. The renderer is the only new
# logic.


@dataclass
class ReportDocument:
    """Output of ``compute_report`` — the sub-reports the markdown
    renderer composes, so tests can drive the renderer with synthetic
    parts.

    No ``to_dict``: report mode is markdown-only by contract (the CLI
    hard-errors on ``--report --json``), and each part is individually
    serialisable already.

    ``window_eval`` and ``alltime_eval`` are the SAME object when the
    caller asked for ``--since all`` — the renderer collapses to a
    single column rather than printing two identical ones.
    """

    generated_at: datetime
    window_seconds: int | None  # None = the window IS all-time
    version: str
    active_memory_count: int
    total_events: int
    distinct_session_count: int
    window_eval: EvalReport
    alltime_eval: EvalReport
    sweep: ThresholdSweepReport
    tool_usage: ToolUsageReport


def compute_report(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    since: timedelta | None = None,
    tombstoned_ids: set[str] | None = None,
    version: str | None = None,
) -> ReportDocument:
    """Compose the existing rollups into one report document.

    ``events`` may be a one-shot iterator (``iter_all_events`` is); it
    is materialised once here because four computations re-walk it.
    ``since`` is the window column of the rate trio; the all-time
    column, the threshold sweep, the tool-usage rollup, and the
    aggregate store-shape counters always cover the full log — the
    report is a distribution artifact, and all-time is the canonical
    denominator for everything except the trend comparison. ``None``
    means the window is all-time and the renderer shows one column.

    Distinct sessions are COUNTED via the same ``session_id`` /
    ``session`` fallback chain ``_silent_miss_from_event`` reads,
    minus every event ``is_admin_recorded_event`` rejects (admin/CLI
    writers run under a throwaway session id and would each publish a
    phantom session); the ids themselves never land on the document.
    """
    now = now or datetime.now(timezone.utc)
    memory_list = list(memories)
    event_list = list(events)

    alltime_eval = compute_eval(
        memory_list,
        event_list,
        now=now,
        since=None,
        tombstoned_ids=tombstoned_ids,
    )
    if since is None:
        window_eval = alltime_eval
    else:
        window_eval = compute_eval(
            memory_list,
            event_list,
            now=now,
            since=since,
            tombstoned_ids=tombstoned_ids,
        )
    sweep = compute_threshold_sweep(event_list, since=None, now=now)
    usage = compute_tool_usage(event_list, since=None, now=now)

    sessions: set[str] = set()
    for ev in event_list:
        if not isinstance(ev, dict):
            continue
        if is_admin_recorded_event(ev):
            # Recorded outside any client session under a throwaway
            # session id, by kind (the cutoff marker) or by `cli_*`
            # attribution (the CLI's `restore` and `rename_scope` rows,
            # which wear a kind real sessions also use). Counting one
            # would publish a session that never existed.
            continue
        sid = ev.get("session_id") or ev.get("session")
        if isinstance(sid, str) and sid:
            sessions.add(sid)

    return ReportDocument(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        version=version if version is not None else _package_version(),
        active_memory_count=len(memory_list),
        total_events=alltime_eval.total_events_scanned,
        distinct_session_count=len(sessions),
        window_eval=window_eval,
        alltime_eval=alltime_eval,
        sweep=sweep,
        tool_usage=usage,
    )


__all__ = [
    "EvalReport",
    "RateCI",
    "ColdEndorsementMemoriesRow",
    "ReportDocument",
    "SilentMissCandidate",
    "ToolUsageReport",
    "ToolUsageRow",
    "ThresholdRule",
    "ThresholdSweepReport",
    "ThresholdSweepRow",
    "WideningDetailReport",
    "WideningFlaggedTurn",
    "WideningMemoryRollup",
    "WideningPreviewReport",
    "WideningRuleDetail",
    "THRESHOLD_RULES",
    "WIDENING_RULES",
    "TOOLS_WITHOUT_TELEMETRY",
    "ADMIN_RECORDED_EVENT_KINDS",
    "ADMIN_RECORDED_ATTRIBUTION_PREFIX",
    "is_admin_recorded_event",
    "DEFAULT_SINCE_SPEC",
    "DEFAULT_ENDORSEMENT_MIN_RETRIEVALS",
    "DEFAULT_SILENT_MISS_LIMIT",
    "EARLIER_RULES",
    "compute_eval",
    "compute_report",
    "compute_tool_usage",
    "compute_threshold_sweep",
    "compute_widening_detail",
    "compute_widening_preview",
    "parse_since",
    "render_report_markdown",
    "render_text",
    "render_tool_usage_text",
    "render_threshold_sweep_text",
    "render_widening_detail_text",
    "render_widening_preview_text",
    "surface_rule_split",
]
