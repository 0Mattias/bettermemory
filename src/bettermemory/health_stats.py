"""The rollup arithmetic of `health.py`: the event-log accumulator and its helpers.

`health.compute_health` walks the event log through `_StatsAccumulator`,
builds the actor-slice, silent-miss and recent-miss rollups with the
helpers here, and distills the finished report with
`_compute_recommendations`. The claim-anchored commit-drift rows
(`_drift_rows_for_candidates`), the scope-typo check,
`find_prior_session_boundary` and `provenance_debt` live here too.
`health` re-exports every public name here and every private one a
caller outside the family reads, so callers keep importing from
`health`. Nothing here imports `health`.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .durability import canonical_marker
from .events import _event_id_list
from .models import Memory, first_summary_line
from .origin import ReachableWalk, commits_since_anchor
from .claims import load_claims
from .verify import BASIS_AUTHOR_DATE, BASIS_REACHABILITY, resolve_commit_drift
from .time_utils import ensure_utc, parse_event_ts
from .health_model import (
    ActorSlice,
    ActorSlices,
    CommitDriftRow,
    HealthReport,
    MemoryStats,
    ProvenanceDebt,
    ProvenanceRow,
    RecentSilentMiss,
    Recommendation,
    SilentMissStats,
    _PROVENANCE_ROW_CAP,
    _RECENT_SILENT_MISSES_CAP,
    _RECOMMENDATION_ROW_CAP,
    _RECOMMENDATION_SIZE_FLOOR,
    _iso,
    endorsement_tier,
    is_hook_telemetry_event,
)

# health.py reads these two helpers under module-local aliases (the
# comment beside them there says why); the bodies moved here read both
# spellings, so this module binds both.
from .time_utils import ensure_utc as _ensure_utc
from .time_utils import parse_event_ts as _parse_event_ts


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
    # Per-actor event footprint keyed by declared `client`, `None` for
    # undeclared. Counts EVERY event, so it reconciles against
    # `total_events` below.
    actor_events: Counter[str | None]
    actor_searches: Counter[str | None]
    actor_applies: Counter[str | None]
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

    ``_StatsAccumulator._handle_search_miss`` builds this tuple.
    ``eval_model._canonical_top_hit_id`` mirrors its ``top_hit_id`` half
    by hand (canonical ``top_hits`` only, no legacy fallback), so the
    tombstone filter reads a miss the same way on both surfaces; change
    the two together.

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
        # Per-actor event footprint, keyed by declared `client` with
        # `None` for "this event's writer declared no client". Read in
        # ONE place (`_note_actor`, from `handle_event`) rather than
        # per-kind, so the pivot counts every event the report counts
        # and the `events` invariant in `ActorSlices` holds by
        # construction rather than by keeping N handlers in step.
        self._actor_events: Counter[str | None] = Counter()
        self._actor_searches: Counter[str | None] = Counter()
        self._actor_applies: Counter[str | None] = Counter()
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
        # (memory_health / doctor). Only a non-empty
        # string can be a real session id anyway.
        # Admin/CLI writers (the commands recording through
        # `cli._common.cli_recorder`, `rollback --apply`, and in a log
        # carried over from 8.x `consolidate --acknowledge-debt` and
        # `doctor --fix`) record under a fresh throwaway SessionState id —
        # counting one would publish a session no client ever attached to,
        # so each admin run would permanently inflate the Sessions line by
        # one and put `memory_health` in silent disagreement with
        # `eval.compute_report` and doctor's cadence census, which both
        # already exclude these. The exclusion gates the SESSION TALLY
        # ONLY: the event still dispatches to its handler below, so
        # admin-recorded use rows keep counting as the genuine
        # endorsements they are (eval.py's scope note on
        # `ADMIN_RECORDED_ATTRIBUTION_PREFIX`).
        sess = ev.get("session") or ev.get("session_id")
        if isinstance(sess, str) and sess and not self._is_admin_recorded_event(ev):
            self._sessions.add(sess)

        self._note_actor(ev)

        kind = ev.get("kind")
        handler = self._HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is not None:
            handler(self, ev)

    # ---- per-event handlers --------------------------------------------

    def _handle_search(self, ev: dict[str, Any]) -> None:
        # Canonical-first read with the legacy-name fallback the other
        # event consumers use (hook / _handlers / _response) — keeps
        # the health rollups consistent if an event carries the older
        # `memory_ids` / `hit_ids` spelling.
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
            tier = endorsement_tier(ev)
            if tier is not None:
                stats.applied_count += 1
                # Tiering via the shared `endorsement_tier`, the one
                # definition eval's cold-endorsement rows read too (a
                # pointer's settlement counts; see its docstring), and
                # `applied_tier`'s auto / hook / model rule. `explicit`
                # stays exactly what it always was — everything not auto
                # — so `endorsement_ratio`, `_is_weakly_endorsed` and the
                # published eval rate all keep their meaning; the
                # hook/model pair below decomposes it.
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
        # the shared `_parse_silent_miss_event`.
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
        # batch of historical misses (e.g. v2.7.3 cwd-suppression), a
        # bulk acknowledgement (`memory_admin(action="acknowledge_miss",
        # before=<ts>)`; 8.x wrote it with `consolidate
        # --acknowledge-misses-before`) writes one of these and the
        # rollup honors the latest `cutoff_ts` seen, dropping any
        # earlier turn_audited / search_miss events. Older `cutoff_ts`
        # values are ignored so a later cutoff can extend the window but
        # not shrink it.
        # `_ensure_utc` after parsing so a naive cutoff_ts compares
        # cleanly against the aware event ts above.
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

    def _note_actor(self, ev: dict[str, Any]) -> None:
        """Attribute one event to its writer's declared `client`.

        Deliberately NOT a `_handle_<kind>` method: those are dispatch
        targets and `test_handlers_table_matches_handle_methods` pins
        the table against them. This runs for EVERY event from
        `handle_event`, which is the point — the per-actor `events`
        count has to reconcile against `total_events`, and a per-kind
        hook would only ever see the kinds that have handlers.

        A malformed `actor` (anything but a dict) and a non-string
        `client` both read as undeclared rather than raising: this is a
        census over an append-only log that older writers and other
        clients also append to, and one bad line must not blank the
        whole pivot. Same defensive posture as the `session` read
        above.
        """
        actor = ev.get("actor")
        client: str | None = None
        if isinstance(actor, dict):
            raw = actor.get("client")
            if isinstance(raw, str) and raw:
                client = raw
        self._actor_events[client] += 1
        kind = ev.get("kind")
        if kind == "search":
            self._actor_searches[client] += 1
        elif kind == "use" and ev.get("outcome") == "applied":
            # Per EVENT, not per id: this pivot answers "how much did
            # this actor settle", and one `memory_record_use` call
            # carrying five ids is one act of settlement. The per-id
            # counts stay on `MemoryStats.applied_count`, which is the
            # basis the dead-weight and endorsement rules read.
            self._actor_applies[client] += 1

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
            actor_events=self._actor_events,
            actor_searches=self._actor_searches,
            actor_applies=self._actor_applies,
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
    _HANDLERS: dict[str, Callable[[_StatsAccumulator, dict[str, Any]], None]] = {
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


def _build_actor_slices(
    *,
    memories: Counter[str | None],
    memories_in_window: Counter[str | None],
    models: defaultdict[str | None, Counter[str]],
    principals: defaultdict[str | None, Counter[str]],
    events: Counter[str | None],
    searches: Counter[str | None],
    applies: Counter[str | None],
) -> ActorSlices:
    """Fold the memory-side and event-side per-actor counters into one
    pivot, with the undeclared bucket split out.

    The key set is the UNION of every counter's keys, not the memory
    counter's alone. An actor that only searched this window wrote no
    memory, and dropping it would silently lose the events it is
    accountable for — and with them the `events` invariant. The reverse
    (a client that wrote before 7.10.0 and has never searched since)
    is just as real.

    `None` is pulled out into `undeclared` rather than sorted among the
    named clients: it is the answer "nobody declared", and leaving it
    in a list of names would invite a consumer to render it as a client
    called None. It is emitted even when every counter is empty, so a
    fully-attributed store still publishes the bucket at zero.

    Declared slices sort by memory count descending, then by client
    name, so the render order is stable across runs for a consumer
    diffing two reports.
    """
    keys: set[str | None] = (
        set(memories)
        | set(memories_in_window)
        | set(models)
        | set(principals)
        | set(events)
        | set(searches)
        | set(applies)
    )

    def slice_for(key: str | None) -> ActorSlice:
        return ActorSlice(
            client=key,
            memories=memories.get(key, 0),
            memories_in_window=memories_in_window.get(key, 0),
            events=events.get(key, 0),
            searches=searches.get(key, 0),
            applies=applies.get(key, 0),
            models=dict(models[key]) if key in models else {},
            principals=dict(principals[key]) if key in principals else {},
        )

    declared = sorted(
        (slice_for(k) for k in keys if k is not None),
        key=lambda s: (-s.memories, s.client or ""),
    )
    return ActorSlices(declared=declared, undeclared=slice_for(None))


def _compute_recommendations(report: HealthReport) -> list[Recommendation]:
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
                    "memory_remove(id, reason=...) on the unhelpful ones; "
                    "memory_record_use(applied) on the ones that are still "
                    "valuable clears the signal without touching bodies."
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
                    "memory_record_use(applied) on the ones you are sure are "
                    "useful clears the signal; memory_remove on the ones "
                    "that aren't."
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
                    'memory_admin(action="restore", id=...): the restore '
                    "re-admits it through the store and it reads local from "
                    "then on. Remove the rest."
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
                    'memory_admin(action="rename_scope", old_scope=..., '
                    "new_scope=...) to fold each singleton into the intended "
                    "scope name."
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
    verified_head_by_id: dict[str, str | None] | None = None,
    head: str | None = None,
) -> list[CommitDriftRow]:
    """The per-candidate count-and-narrow loop, shared between the
    caller-repo rollup and the cross-repo estate check — one loop so
    the two cannot compute different drift policies for the same
    memory. Returns drifting rows sorted heaviest-first; the caller
    owns capping and the empty-vs-None distinction.

    `verified_head_by_id` and `head` (the commit `root`'s HEAD names,
    resolved once by the caller) select the basis per candidate, the
    same way `verify.compute_commit_drift` does: a candidate whose stamp
    recorded an anchor HEAD still descends from is counted over the
    reachable walk from it, the rest over the author-date bisect.
    """
    rows: list[CommitDriftRow] = []
    # The walks of this pass no memo keeps (`commits_since_anchor`): a store
    # verified at one dead anchor forks one walk for it, and so does one
    # anchor where the walk memo cannot key the walk.
    walked: dict[tuple[str, str, str], ReachableWalk | bool] = {}
    for stats in candidates:
        since = stats.last_verified_at
        assert since is not None  # callers filter on this
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        stored_claims = claims_by_id.get(stats.id, ()) if claims_by_id else ()
        parsed_claims = load_claims(list(stored_claims)) if stored_claims else []
        # The basis, per memory: the reachable walk from the stamp's
        # recorded HEAD when the record carries one and the anchor is
        # still an ancestor of this root's HEAD (memoised per anchor,
        # so a store verified at one commit walks once for the whole
        # rollup); the author-date bisect otherwise — the rule
        # `verify.compute_commit_drift` applies on memory_show.
        walk = None
        anchor = verified_head_by_id.get(stats.id) if verified_head_by_id else None
        if anchor is not None and head is not None:
            walk = commits_since_anchor(
                root, anchor, toplevel=toplevel, head=head, walked=walked
            )
        if walk is not None:
            count = len(walk.commits)
            basis = BASIS_REACHABILITY
        else:
            # bisect_right gives us the first index strictly greater than
            # `since`; len - idx is then the count of timestamps after that
            # cut. Equal timestamps fall before the cut on bisect_right
            # semantics, which is what we want — a verify call that lands
            # at the same instant as a commit doesn't count as drift.
            idx = bisect.bisect_right(timestamps, since)
            count = len(timestamps) - idx
            basis = BASIS_AUTHOR_DATE
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
        # The richer entry point over the same core as
        # `resolve_commit_drift_count`, so the row can carry the basis
        # the narrowing settled on.
        if count > 0:
            resolved = resolve_commit_drift(
                cwd=root,
                since=since,
                unfiltered=count,
                anchors=anchor_paths_by_id.get(stats.id, ()),
                claims=parsed_claims,
                toplevel=toplevel,
                walk=walk,
            )
            if resolved is None:
                continue
            count = resolved.count
            basis = resolved.basis
        if count > 0:
            rows.append(
                CommitDriftRow(
                    id=stats.id,
                    scopes=list(stats.scopes),
                    summary=stats.summary,
                    last_verified_at=stats.last_verified_at,
                    commits_since_verify=count,
                    basis=basis,
                )
            )
    rows.sort(key=lambda r: r.commits_since_verify, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_post_cutoff(
    timestamps: list[datetime | None], cutoff: datetime | None
) -> int:
    """Count timestamps that fall at or after a cutoff.

    When `cutoff` is None, returns the full count — preserving the
    pre-cutoff-event rollup behavior for stores that never logged a bulk
    acknowledgement (`memory_admin(action="acknowledge_miss", before=...)`).
    When `cutoff` is set, events with a missing or unparseable timestamp
    are dropped on the conservative interpretation that we cannot prove
    they post-date the cutoff (a stamped Recorder always emits `ts`, so
    this only affects malformed legacy events).
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


#: The longest scope the deletion index takes. A scope's 1- and
#: 2-deletion variants number about L²/2 strings of about L characters, so
#: the index costs the cube of a scope's length: one 1,009-character scope,
#: which `memory_write` accepts, took the report from 0.07 s and 3 MB to
#: 2.63 s and 567 MB (review D4). A longer scope is compared with every
#: scope whose length is within 2 of its own instead (`_rare_scopes`).
#: The value is a choice, not a measurement: it covers the namespaced
#: scopes stores hold (``projects:bettermemory`` is 21) and keeps a
#: scope's variants near 500.
_DELETION_INDEX_MAX_LEN = 32


def _deletion_variants(text: str) -> set[str]:
    """`text` and every string one or two deleted characters leave of it:
    the keys of the symmetric-deletion index (`_rare_scopes`). Two strings
    within edit distance 2 share a key, since the edits of an alignment
    undone by deletions on each side leave one string."""
    out = {text}
    size = len(text)
    for i in range(size):
        once = text[:i] + text[i + 1 :]
        out.add(once)
        for j in range(i, size - 1):
            out.add(once[:j] + once[j + 1 :])
    return out


def _rare_scopes(
    scope_counts: Mapping[str, int], exempt: Collection[str] = ()
) -> list[str]:
    """The singleton scopes (count 1, not in `exempt`) that have a typo
    neighbour among all the scopes (`_scope_typo_neighbor`), sorted: the
    `rare_scopes` bucket.

    One pass over the scopes, not a pass per singleton: every scope the
    predicate can call a neighbour of another is within edit distance 2
    of it as a whole string, or shares its name part (rule 1). Rule 4
    compares the tails left after the leading segments the two share,
    and a shared prefix adds no distance, so a tail pair within 2 is a
    whole-string pair within 2; where one scope is all shared segments
    its tail is empty, and the rule then holds only for a one-character
    tail on the other, two edits away (``a:b`` and ``a:b:c``). So the
    candidates are the scopes sharing a deletion variant
    (`_deletion_variants`, keyed by hash: a collision only adds a
    candidate) or a name part, and the predicate decides each, so the
    bucket is the pairwise check's to the scope. Measured on synthetic
    stores of 500, 1,000 and 2,000 scopes (70% namespaced, 5% planted
    one-edit typos): the pairwise check took 2.1 s, 8.1 s and 32.2 s
    (42 s on the review's shapes), this pass 0.013 s, 0.027 s and
    0.056 s, with a 22 MB peak at 2,000.

    A scope longer than `_DELETION_INDEX_MAX_LEN` stays out of the
    deletion index, whose cost grows with the cube of a scope's length.
    The lengths of a pair the predicate accepts by distance differ by 2
    at most, so a long singleton's candidates are every scope whose
    length is within 2 of its own, and a short singleton's include the
    long scopes within 2 of its length; the name-part index still holds
    every scope. The bucket stays exact.

    The worst case is still near the square of the scope count: short
    names that share most of their deletion variants (2,000 scopes of
    three letters out of thirteen behind one namespace) make the
    candidates approach all pairs.
    """
    scopes = list(scope_counts)
    wanted = {
        n
        for n, scope in enumerate(scopes)
        if scope_counts[scope] == 1 and scope not in exempt
    }
    if not wanted:
        return []
    first: dict[int, int] = {}
    shared: dict[int, list[int]] = {}
    by_name: dict[str, list[int]] = {}
    long_by_length: dict[int, list[int]] = {}
    for n, scope in enumerate(scopes):
        by_name.setdefault(scope.split(":", 1)[-1], []).append(n)
        if len(scope) > _DELETION_INDEX_MAX_LEN:
            long_by_length.setdefault(len(scope), []).append(n)
            continue
        for key in map(hash, _deletion_variants(scope)):
            seen = first.setdefault(key, n)
            if seen != n:
                group = shared.get(key)
                if group is None:
                    shared[key] = [seen, n]
                else:
                    group.append(n)
    del first
    candidates: dict[int, set[int]] = {}
    for group in (*shared.values(), *by_name.values()):
        if len(group) < 2:
            continue
        for n in group:
            if n in wanted:
                candidates.setdefault(n, set()).update(group)
    if long_by_length:
        by_length: dict[int, list[int]] = {}
        for n, scope in enumerate(scopes):
            by_length.setdefault(len(scope), []).append(n)
        for n in wanted:
            size = len(scopes[n])
            # A long singleton against every scope of a near length; a
            # short one against the long scopes of a near length, which
            # the deletion index does not hold.
            near = by_length if size > _DELETION_INDEX_MAX_LEN else long_by_length
            for length in range(size - 2, size + 3):
                group = near.get(length)
                if group:
                    candidates.setdefault(n, set()).update(group)
    return sorted(
        scopes[n]
        for n, others in candidates.items()
        if any(m != n and _scope_typo_neighbor(scopes[n], scopes[m]) for m in others)
    )


def _edit_distance_within(a: str, b: str, max_dist: int) -> bool:
    """True iff Levenshtein(a, b) <= max_dist.

    Wagner-Fischer, banded: a cell more than `max_dist` off the diagonal
    holds a distance above `max_dist`, so each row keeps only the
    ``2 * max_dist + 1`` cells within `max_dist` of it and the rest read
    as `max_dist + 1`, which keeps every answer to "within `max_dist`?"
    exact. The work is the longer string's length times that band, not
    the product of the two lengths: a 4,000-character scope and its
    one-edit typo took the full table about 16 million cells (review D4).
    The length-difference shortcut catches the obviously far cases
    without running the table at all, and a row whose every cell is past
    the threshold ends it, since no later row falls below it.

    Used by the `rare_scopes` neighbor check (via
    `_scope_typo_neighbor`, which owns the scope-shape rules); lifted
    out as a helper so it stays testable in isolation if we ever need
    to tune the threshold or swap algorithms.
    """
    if abs(len(a) - len(b)) > max_dist:
        return False
    if a == b:
        return True
    # Ensure |a| <= |b|: the columns are `a`'s.
    if len(a) > len(b):
        a, b = b, a
    columns = len(a)
    width = 2 * max_dist + 1
    over = max_dist + 1
    # Row i keeps column j at index j - i + max_dist; row 0 holds j itself.
    prev = [over] * width
    for d in range(max_dist, min(width, columns + max_dist + 1)):
        prev[d] = d - max_dist
    for i, cb in enumerate(b, 1):
        curr = [over] * width
        best = over
        for d in range(width):
            j = i - max_dist + d
            if j < 0 or j > columns:
                continue
            if j == 0:
                value = i if i <= max_dist else over
            else:
                value = prev[d] + (0 if a[j - 1] == cb else 1)
                if d + 1 < width:
                    value = min(value, prev[d + 1] + 1)
                if d >= 1:
                    value = min(value, curr[d - 1] + 1)
            curr[d] = value
            best = min(best, value)
        if best > max_dist:
            return False
        prev = curr
    return prev[columns - len(b) + max_dist] <= max_dist


def find_prior_session_boundary(
    events: Iterable[dict[str, Any]],
    current_session_id: str | None,
) -> datetime | None:
    """Latest event timestamp belonging to a session other than the current one.

    Used by `memory_search(since_prior_session=True)` to narrow the
    candidates to memories written after the prior session. Returns
    ``None`` when the event log carries no events outside the current
    session — typical on a fresh install or the very first session
    after a memory directory was wiped; the search then returns no
    rows.

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


def provenance_debt(store: Any, memories: Iterable[Memory]) -> ProvenanceDebt | None:
    """The provenance bucket of the store, joined against `memories`.
    Reads the per-label counts and the ids whose pointer into the log
    fails, and fills the rows from the memories the caller already
    loaded. None when the store cannot verify (no key on this machine),
    which the report carries as null rather than as an empty bucket."""
    if not store.has_key:
        return None
    counts = store.provenance_counts()
    ids = store.unaccounted_ids()
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
