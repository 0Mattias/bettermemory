"""The steps of ``eval.compute_eval``: the walk and the counts behind the rates.

``compute_eval`` walks the event log once (``_walk_eval_events``, with
``_count_search`` and ``_count_use`` for the retrieval and use rows),
resolves the buffered audit telemetry against the invalidation markers
(``_resolve_audit_telemetry``) and lists the cold-endorsement rows
(``_cold_endorsement_rows``); ``eval`` builds the report from what they
return. The semantics are documented on ``compute_eval``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from .eval_model import (
    ColdEndorsementMemoriesRow,
    SilentMissCandidate,
    _canonical_top_hit_id,
    _parse_ts,
    _silent_miss_from_event,
)
from .health import endorsement_tier as _endorsement_tier
from .health import is_hook_telemetry_event
from .models import Memory, first_summary_line


@dataclass
class _EvalWalk:
    """The counts and rows ``compute_eval``'s walk of the event log gathers."""

    # The review counters' rows, kept from the same walk. They are read
    # past the window's end (a chain is judged from rows up to 30
    # minutes after it), so they are collected before the window filter.
    review_rows: list[dict[str, Any]] = field(default_factory=list)
    # The recall surface's rows, kept the same way: what a conversation
    # did after a show is read from rows outside the window's start.
    surface_rows: list[dict[str, Any]] = field(default_factory=list)
    retrieval_occurrences: int = 0
    explicit_endorsements_with_excerpt: int = 0
    applied_total: int = 0
    applied_explicit: int = 0
    # `applied_explicit` split by producer (hook containment match vs
    # real `memory_record_use` call) — see the EvalReport fields.
    applied_model: int = 0
    applied_hook: int = 0
    total_events_scanned: int = 0
    events_in_window: int = 0
    # Per-memory counts for cold-endorsement rollup. The floor basis is
    # SEARCH deliveries only — `list` and `show` occurrences do NOT
    # count — mirroring health's `MemoryStats.retrieval_count`, which
    # only `_handle_search` bumps (`show` feeds a separate `show_count`;
    # there is no `list` handler at all). Two reasons the bases must
    # match: the recalibrated floor is DERIVED per search delivery
    # ((1-p)^N over search-delivered explicit-endorse odds), and one
    # `memory_list` call over an N-memory store would otherwise bump
    # every memory's floor count at once, so the two surfaces would
    # bucket different memories under the same nominal floor. The
    # `retrieval_occurrences` scalar deliberately keeps its wider
    # search+list+show basis — it is the helped-rate denominator with a
    # recorded baseline, not a floor input.
    search_delivery_count: dict[str, int] = field(default_factory=dict)
    auto_applied_count: dict[str, int] = field(default_factory=dict)
    explicit_applied_count: dict[str, int] = field(default_factory=dict)
    # Stop-hook settlement coverage for the cold-endorsement honesty
    # gate — counted over the WHOLE log (before the window filter),
    # matching `health.compute_health`, whose accumulator measures "did
    # settlement telemetry EVER reach this event log", not "recently".
    # Costs nothing: the walk below already visits every event.
    hook_telemetry_events: int = 0
    # In-window ``turn_audited`` / ``search_miss`` events, buffered for
    # post-pass resolution against the invalidation markers below —
    # counting them inline would let a marker later in the log arrive
    # too late to retract an already-counted event. Same
    # buffer-then-resolve shape as ``health._StatsAccumulator``.
    audit_telemetry: list[dict[str, Any]] = field(default_factory=list)
    # The two escape hatches health.py's rollups honor, mirrored here
    # so the eval CLI and memory_health agree
    # over the same event stream: `silent_miss_cutoff` wipes all miss
    # telemetry before its `cutoff_ts`; `miss_ack` retracts one
    # `search_miss` by `event_id`.
    latest_miss_cutoff: datetime | None = None
    acknowledged_miss_event_ids: set[str] = field(default_factory=set)


@dataclass
class _AuditTally:
    """The audit telemetry ``compute_eval`` counts once the markers are resolved."""

    turns_audited: int = 0
    turns_no_signal: int = 0
    silent_misses: int = 0
    # Rolling buffer of recent silent-miss events (last K, since
    # events come in chronological order — we just keep the tail).
    silent_miss_buffer: list[SilentMissCandidate] = field(default_factory=list)
    # Track the most recent threshold rule we've seen on any miss
    # event so the renderer can name the regime the numbers came
    # from. Empty string means no audits in the window.
    threshold_rule: str = ""
    # Repeat audits (the same session+message re-probed by a
    # multi-stop turn; `repeat=True` since 3.14) are excluded from
    # every denominator — their companion `search_miss` is never
    # emitted, so counting them would dilute the rate. Tallied
    # separately so the report can show how much re-audit noise the
    # dedup absorbed.
    repeat_audits: int = 0
    # Per-model slices of the audit telemetry, keyed by the
    # `client_model` the Stop hook stamps off the transcript (absent
    # on pre-3.14 events and on the MCP producer — those events simply
    # don't bucket; no "unknown" row is manufactured).
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)


def _count_search(
    walk: _EvalWalk,
    ev: dict[str, Any],
    kind: Any,
    passes_scope: Callable[[str | None], bool],
) -> None:
    """Count one in-window search or list event's deliveries into ``walk``."""
    # `list` is bundled with `search` because `audit.py` and the
    # tool-usage map both treat memory_list as a retrieval
    # surface (the model uses it to browse, the same way it
    # uses memory_search to query). Keeping these aligned
    # keeps the eval denominator consistent with audit cadence.
    # Legacy-name fallback (`memory_ids` / `hit_ids`) — same
    # discipline as the other event consumers (hook /
    # _handlers). `list` events only ever wrote
    # `returned`, but reading through the same fallback chain
    # is harmless.
    returned = ev.get("returned") or ev.get("memory_ids") or ev.get("hit_ids") or []
    if not isinstance(returned, list):
        return
    for mid in returned:
        if not isinstance(mid, str):
            continue
        if not passes_scope(mid):
            continue
        walk.retrieval_occurrences += 1
        # Floor basis: search deliveries only (see the
        # `search_delivery_count` comment above), so a `list`
        # browse widens the rate denominator without pushing
        # every browsed memory toward the cold bucket.
        if kind == "search":
            walk.search_delivery_count[mid] = walk.search_delivery_count.get(mid, 0) + 1


def _count_use(
    walk: _EvalWalk,
    ev: dict[str, Any],
    passes_scope: Callable[[str | None], bool],
    is_settlement: Callable[[dict[str, Any]], bool],
) -> None:
    """Count one in-window use event's applies into ``walk``."""
    # Tiering via the shared `health.endorsement_tier`: None for
    # an outcome other than applied, else `applied_tier`'s auto /
    # hook / model, with the strict `auto is True` identity
    # comparison (a stray truthy `auto=1` / `auto="true"` reads
    # as NON-auto, same as missing/None, so we never silently
    # relabel borderline data as the server closing the loop).
    # Shared rather than re-derived here: this module and
    # `health._StatsAccumulator._handle_use` publish the same
    # split under different names, and the per-memory tallies
    # below are the cold-endorsement bucket health lists too.
    tier = _endorsement_tier(ev)
    if tier is None:
        return
    # A pointer's settlement (`recall.settle_shown`) feeds the
    # per-memory tallies, as health's do (`endorsement_tier`),
    # and not the trio: the surfaces section counts it, so the
    # published series keeps one definition across the recall
    # surface's arrival.
    settlement = is_settlement(ev)
    ids = ev.get("ids") or ev.get("memory_ids") or []
    if not isinstance(ids, list):
        return
    is_auto = tier == "auto"
    raw_excerpts = ev.get("claim_excerpts")
    # claim_excerpts is parallel to ids; entries may be None.
    # An entry counts toward the helped-rate numerator only
    # when the corresponding excerpt is a non-empty string.
    excerpts: list[Any]
    if isinstance(raw_excerpts, list):
        excerpts = list(raw_excerpts)
    else:
        excerpts = [None] * len(ids)
    # Dedupe `ids` within a single use event before counting.
    # A model that sends `record_use(memory_ids=["A", "A"], ...)`
    # would otherwise inflate `applied_total` and the helped-rate
    # numerator. Since eval.py is the citable reference for the
    # published metric, the dedup must happen here — we can't
    # fix upstream without changing recorder semantics. Preserve
    # the first non-empty excerpt per id so the helped-rate
    # numerator stays accurate when "A" appears twice with
    # excerpts `["foo", "bar"]`.
    seen_ids: dict[str, Any] = {}
    for i, mid in enumerate(ids):
        if not isinstance(mid, str):
            continue
        excerpt = excerpts[i] if i < len(excerpts) else None
        if mid in seen_ids:
            existing = seen_ids[mid]
            # Upgrade to a non-empty excerpt if we don't have one yet.
            if not (isinstance(existing, str) and existing.strip()) and (
                isinstance(excerpt, str) and excerpt.strip()
            ):
                seen_ids[mid] = excerpt
        else:
            seen_ids[mid] = excerpt
    for mid, excerpt in seen_ids.items():
        if not passes_scope(mid):
            continue
        if is_auto:
            walk.auto_applied_count[mid] = walk.auto_applied_count.get(mid, 0) + 1
        else:
            walk.explicit_applied_count[mid] = (
                walk.explicit_applied_count.get(mid, 0) + 1
            )
        if settlement:
            continue
        walk.applied_total += 1
        if not is_auto:
            walk.applied_explicit += 1
            if tier == "hook":
                walk.applied_hook += 1
            else:
                walk.applied_model += 1
            if isinstance(excerpt, str) and excerpt.strip():
                walk.explicit_endorsements_with_excerpt += 1


def _walk_eval_events(
    events: Iterable[dict[str, Any]],
    cutoff: datetime | None,
    window_end: datetime | None,
    passes_scope: Callable[[str | None], bool],
) -> _EvalWalk:
    """Walk the event log once; returns the counts and rows ``compute_eval`` reads."""
    from .recall import is_settlement
    from .recall_counters import is_surface_row
    from .review_counters import is_review_row

    walk = _EvalWalk()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        walk.total_events_scanned += 1
        # Window membership is decided up front — BEFORE the marker
        # short-circuits below, which deliberately bypass the window
        # filter — so `events_in_window` covers exactly the population
        # `total_events_scanned` does, only window-scoped. An event with
        # a missing or unparseable `ts` counts as out-of-window, the
        # same predicate the filter further down applies.
        in_window = True
        if cutoff is not None or window_end is not None:
            event_ts = _parse_ts(ev.get("ts"))
            in_window = (
                event_ts is not None
                and (cutoff is None or event_ts >= cutoff)
                and (window_end is None or event_ts < window_end)
            )
        if in_window:
            walk.events_in_window += 1
        if is_review_row(ev):
            walk.review_rows.append(ev)
        if is_surface_row(ev):
            walk.surface_rows.append(ev)

        kind = ev.get("kind")
        # Coverage tally — like the markers below, deliberately ahead
        # of the window skip: the honesty gate asks about the whole log.
        if is_hook_telemetry_event(ev):
            walk.hook_telemetry_events += 1
        # Invalidation markers — resolved BEFORE the `since` window
        # filter: both are global markers, so a cutoff/ack whose own ts falls
        # outside the window still applies to in-window telemetry.
        # Without the exemption a windowed run would silently drop the
        # marker and over-count events health.py's rollups (which walk
        # the whole log) have already invalidated.
        if kind == "silent_miss_cutoff":
            # Latest `cutoff_ts` wins; an earlier cutoff seen later in
            # the log cannot shrink the invalidated window — same
            # max-semantics as `health._handle_silent_miss_cutoff`. A
            # malformed `cutoff_ts` parses to None and is ignored.
            parsed_cutoff = _parse_ts(ev.get("cutoff_ts"))
            if parsed_cutoff is not None and (
                walk.latest_miss_cutoff is None
                or parsed_cutoff > walk.latest_miss_cutoff
            ):
                walk.latest_miss_cutoff = parsed_cutoff
            continue
        if kind == "miss_ack":
            # A missing / non-string / empty `event_id` is a malformed
            # admin event; ignore it rather than poisoning the set.
            target = ev.get("event_id")
            if isinstance(target, str) and target:
                walk.acknowledged_miss_event_ids.add(target)
            continue

        if not in_window:
            continue

        if kind in ("search", "list"):
            _count_search(walk, ev, kind, passes_scope)
        elif kind == "show":
            mid = ev.get("id")
            if not isinstance(mid, str):
                continue
            if not passes_scope(mid):
                continue
            # A pinpoint read counts as a retrieval occurrence but not
            # toward the cold-endorsement floor — health keys `show`
            # into `show_count`, never `retrieval_count`.
            walk.retrieval_occurrences += 1
        elif kind == "use":
            _count_use(walk, ev, passes_scope, is_settlement)
        elif kind in ("turn_audited", "search_miss"):
            # Buffered rather than counted inline so a cutoff/ack
            # marker later in the log can retroactively invalidate
            # earlier telemetry. Resolution happens after the pass,
            # below — chronological order is preserved, so the
            # counting is identical to the pre-buffer inline loop
            # whenever no marker exists.
            walk.audit_telemetry.append(ev)
    return walk


def _resolve_audit_telemetry(
    walk: _EvalWalk,
    tombstoned_ids: set[str],
    silent_miss_limit: int,
) -> _AuditTally:
    """Resolve the walk's buffered audit telemetry; returns what it counts."""
    tally = _AuditTally()
    # Resolve the buffered audit telemetry. An event invalidated by the
    # latest cutoff (ts strictly before `cutoff_ts`; an unparseable ts
    # drops too once a cutoff exists — `health._count_post_cutoff`'s
    # conservative read) is dropped from EVERYTHING: the miss-capable
    # and no_signal denominators, the numerator, the threshold-rule
    # tracking, and the inline triage buffer — as if it were never
    # logged. A tombstoned-top-hit miss and an acked miss drop the
    # same way but from the numerator side only: audits carry neither
    # a per-memory payload nor an `event_id`, and the audit itself
    # wasn't the false positive (the audit ran, the probe found
    # something, the model acknowledged the verdict — or the memory
    # was later removed), so the denominator keeps its turn — filters
    # #2 and #3 of `health._silent_miss_stats`.
    for ev in walk.audit_telemetry:
        if walk.latest_miss_cutoff is not None:
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts < walk.latest_miss_cutoff:
                continue
        if ev.get("kind") == "turn_audited":
            if ev.get("repeat"):
                tally.repeat_audits += 1
                continue
            # `no_signal` audits (empty store, gated probe, semantic model
            # unavailable) are not miss-capable turns; counting them dilutes
            # silent_miss_rate's denominator. Mirrors health.py's
            # `no_signal_total` split (round 88) so a config stuck at
            # permanent no_signal reads as "probe declined", not "healthy".
            if ev.get("verdict") == "no_signal":
                tally.turns_no_signal += 1
            else:
                tally.turns_audited += 1
            model = ev.get("client_model")
            if isinstance(model, str) and model:
                bucket = tally.by_model.setdefault(
                    model, {"audited": 0, "no_signal": 0, "misses": 0}
                )
                if ev.get("verdict") == "no_signal":
                    bucket["no_signal"] += 1
                else:
                    bucket["audited"] += 1
            rule = ev.get("threshold_rule")
            if isinstance(rule, str) and rule:
                tally.threshold_rule = rule
        else:  # search_miss
            # Tombstone filter — health's filter #2, applied BEFORE the
            # ack filter to mirror `_silent_miss_stats`' documented
            # order (both `continue`, so the order is cosmetic). The
            # extraction is canonical-only on purpose: reading the
            # legacy `top_hit_ids` fallback here would drop events
            # health still counts (its parser degrades that shape to
            # None), splitting the two surfaces on old archives.
            if tombstoned_ids:
                top_hit_id = _canonical_top_hit_id(ev)
                if top_hit_id is not None and top_hit_id in tombstoned_ids:
                    continue
            event_id = ev.get("event_id")
            if (
                isinstance(event_id, str)
                and event_id in walk.acknowledged_miss_event_ids
            ):
                continue
            tally.silent_misses += 1
            model = ev.get("client_model")
            if isinstance(model, str) and model:
                tally.by_model.setdefault(
                    model, {"audited": 0, "no_signal": 0, "misses": 0}
                )["misses"] += 1
            rule = ev.get("threshold_rule")
            if isinstance(rule, str) and rule:
                tally.threshold_rule = rule
            candidate = _silent_miss_from_event(ev)
            if candidate is not None:
                tally.silent_miss_buffer.append(candidate)
                if silent_miss_limit <= 0:
                    tally.silent_miss_buffer = []
                elif len(tally.silent_miss_buffer) > silent_miss_limit:
                    tally.silent_miss_buffer = tally.silent_miss_buffer[
                        -silent_miss_limit:
                    ]
    return tally


def _cold_endorsement_rows(
    walk: _EvalWalk,
    by_id: dict[str, Memory],
    floor: int,
) -> tuple[list[ColdEndorsementMemoriesRow], int, bool]:
    """List the cold-endorsement rows; returns the rows, their total and the gate."""
    # Cold-endorsement rows: search deliveries >= floor AND at least
    # one apply happened AND explicit_applied_count == 0. Ambient
    # memories are excluded — same rationale as health's
    # cold_endorsement_memories bucket (their value is implicit; an
    # explicit use event is structurally rare).
    #
    # The "at least one apply" gate (auto + explicit > 0) keeps the
    # bucket as the COMPLEMENT of dead_weight, matching health's
    # `_is_weakly_endorsed`: cold-endorsement means "applies happened,
    # but every one was the auto fallback." A memory retrieved over the
    # floor with zero applies is dead_weight, not cold-endorsement, and
    # must not surface here.
    #
    # Honesty gate, mirroring health's `telemetry_coverage` (widened to
    # the endorsement leg 2026-08-30): with zero Stop-hook settlement
    # telemetry anywhere in the log, the loop is SKIPPED, not run and
    # filtered — the input signal (`explicit_applied_count`, produced
    # by the hook's containment matcher plus deliberate model calls) is
    # missing for every row at once, so listing rows would report an
    # unwired hook as acknowledge-debt. The report's
    # `cold_endorsement_suppressed` flag explains the empty bucket.
    cold_rows: list[ColdEndorsementMemoriesRow] = []
    cold_total = 0
    cold_suppressed = walk.hook_telemetry_events == 0
    if not cold_suppressed:
        for mid, rcount in walk.search_delivery_count.items():
            if rcount < floor:
                continue
            if (
                walk.auto_applied_count.get(mid, 0)
                + walk.explicit_applied_count.get(mid, 0)
                == 0
            ):
                continue
            if walk.explicit_applied_count.get(mid, 0) > 0:
                continue
            mem = by_id.get(mid)
            if mem is None:
                continue
            if mem.category == "ambient":
                continue
            cold_total += 1
            cold_rows.append(
                ColdEndorsementMemoriesRow(
                    id=mem.id,
                    scopes=list(mem.scopes),
                    summary=first_summary_line(mem.body),
                    retrieval_count=rcount,
                    auto_applied_count=walk.auto_applied_count.get(mid, 0),
                    explicit_applied_count=0,
                )
            )
        # Sort by retrieval_count descending so the chattiest
        # dead-letter rows surface first; tie-break by id for
        # determinism.
        cold_rows.sort(key=lambda r: (-r.retrieval_count, r.id))
    return cold_rows, cold_total, cold_suppressed
