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
  the same invalidation markers ``health.py``'s rollups honor: a
  ``silent_miss_cutoff`` event (written by ``bettermemory consolidate
  --acknowledge-misses-before``) drops every earlier ``turn_audited``
  / ``search_miss`` event from numerator, both denominator buckets,
  and the triage buffer alike (latest ``cutoff_ts`` wins), and a
  ``miss_ack`` event (written by ``memory_acknowledge_miss``) drops
  the one referenced miss from the numerator while the audited
  denominator keeps its turn. When the caller passes
  ``tombstoned_ids``, a miss whose canonical top-hit memory has been
  tombstoned drops from the numerator (and the triage buffer) the
  same way — ``health._silent_miss_stats``' filter #2 — while both
  denominator buckets keep their turns. The opt-in-retrieval
  contract's blind spot: turns where the model should have searched
  but didn't.

The methodology is described in ``docs/eval.md``. This module owns the
pure computation; the CLI wrapper lives in ``server.py``.

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
The tiering itself is ``health.applied_tier`` — one derivation, two
surfaces.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .health import applied_tier as _applied_tier
from .models import Memory, first_summary_line
from .time_utils import isoformat_utc, parse_event_ts


# ---------------------------------------------------------------------------
# Public defaults
# ---------------------------------------------------------------------------

# Default ``--since`` window. 30 days mirrors the default
# ``verification_stale_days`` so the eval window and the freshness
# threshold tell a consistent story by default. Override at the call
# site.
DEFAULT_SINCE_SPEC = "30d"

# Default floor for cold-endorsement row inclusion. Two design choices
# baked in here that look like sloppiness but aren't:
#
# 1. Value-duplication of the literal ``5``. The same integer lives at
#    ``health._COLD_ENDORSEMENT_MIN_RETRIEVALS``. We duplicate rather
#    than import so the eval module stays dependency-light (no reach
#    into health's privates), and the value is conservative enough
#    that drift between the two would be inert in practice.
#
# 2. Bare ``ENDORSEMENT`` prefix instead of ``COLD_ENDORSEMENT``. The
#    constant feeds ``endorsement_min_retrievals`` on ``compute_eval``
#    and ``EvalReport``, which serialises to wire-key ``min_retrievals``
#    nested under ``cold_endorsement_memories`` (see
#    ``EvalReport.to_dict``). The bucket scope is supplied by the
#    nesting, not by the identifier prefix — the parameter is
#    conceptually "threshold-for-the-bucket-named-at-the-call-site",
#    so prefixing it with ``cold_`` would be redundant once read in
#    context. Health's ``cold_endorsement_min_retrievals`` doesn't get
#    this nesting (it's a kwarg passed flat through several layers),
#    which is why the two modules diverge on the identifier even
#    though they share the literal.
DEFAULT_ENDORSEMENT_MIN_RETRIEVALS = 5

# Default cap for the inline silent-miss list. Recent enough to triage
# by eye; the full series lives in the event log for offline replay.
DEFAULT_SILENT_MISS_LIMIT = 20

# Wilson score z for 95% CI.
_WILSON_Z = 1.96

# Token shapes accepted by ``parse_since``: ``\\d+[smhd]`` or the literal
# ``"all"``. Bounded by the same arithmetic that bounds ``timedelta``,
# so we don't bother with an upper-bound check beyond rejecting zero.
_SINCE_PATTERN = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$")
_SINCE_UNIT_TO_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RateCI:
    """A rate with its raw numerator/denominator and 95% Wilson CI.

    The rate is ``None`` when the denominator is zero — distinct from
    ``0.0``, which is a real measurement that the denominator was
    populated and the numerator wasn't. Consumers branching on rate
    must handle ``None`` first.

    ``lower`` and ``upper`` are the Wilson interval bounds at z=1.96.
    On zero denominator both are ``None`` for the same reason.
    """

    numerator: int
    denominator: int
    rate: float | None
    lower: float | None
    upper: float | None
    # True when `numerator > denominator`. Two benign causes, NOT only a
    # corrupt log: (1) a torn read — the event log was read mid-rotation and
    # ordering anomalies leaked through; (2) a WINDOWING artifact — under a
    # `--since` window, memory_helped_rate's numerator (`use` events) and
    # denominator (retrieval events) are independent event streams, so a
    # use event can fall inside the window while its retrieval ages out,
    # legitimately pushing numerator past denominator with no corruption.
    # The Wilson helper clamps `k = n` so the interval stays well-defined;
    # the flag tells the consumer the rate was clamped to 1.0 and shouldn't
    # be read as a precise measurement — it does NOT by itself mean the data
    # is corrupt.
    torn_read: bool = False

    @classmethod
    def from_counts(cls, numerator: int, denominator: int) -> RateCI:
        if denominator <= 0:
            return cls(
                numerator=numerator,
                denominator=denominator,
                rate=None,
                lower=None,
                upper=None,
            )
        torn_read = numerator > denominator
        # Clamp the rate at 1.0 so the surfaced value matches the
        # clamped Wilson bounds. Without this the rate would read
        # >1.0 while the CI clamps at 1.0 — inconsistent and confusing.
        rate = 1.0 if torn_read else numerator / denominator
        lo, hi = _wilson_interval(numerator, denominator)
        return cls(
            numerator=numerator,
            denominator=denominator,
            rate=rate,
            lower=lo,
            upper=hi,
            torn_read=torn_read,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": self.rate,
            "ci95_lower": self.lower,
            "ci95_upper": self.upper,
            "torn_read": self.torn_read,
        }


@dataclass
class ColdEndorsementMemoriesRow:
    """One memory the ranker keeps surfacing that the model never
    deliberately endorses. Mirrors ``health.MemoryStats`` minimally —
    we only carry what the eval renderer needs."""

    id: str
    scopes: list[str]
    summary: str
    retrieval_count: int
    auto_applied_count: int
    explicit_applied_count: int  # always 0 for inclusion, kept for shape

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scopes": list(self.scopes),
            "summary": self.summary,
            "retrieval_count": self.retrieval_count,
            "auto_applied_count": self.auto_applied_count,
            "explicit_applied_count": self.explicit_applied_count,
        }


@dataclass
class SilentMissCandidate:
    """One ``search_miss`` event surfaced for the renderer. The full
    detail (top hits, full session id) stays in the event log; this
    is the triage shape."""

    ts: str  # ISO8601, as logged
    session_id: str
    top_missed_id: str | None
    top_missed_relevance: str | None
    threshold_rule: str | None
    recent_retrieval_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "session_id": self.session_id,
            "top_missed_id": self.top_missed_id,
            "top_missed_relevance": self.top_missed_relevance,
            "threshold_rule": self.threshold_rule,
            "recent_retrieval_count": self.recent_retrieval_count,
        }


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
    # Dataclasses in this module declaring `total_events_scanned`, all
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
    # AST-scans this module so a sixth surface cannot be added without
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

    silent_miss_recent: list[SilentMissCandidate] = field(default_factory=list)

    # Repeat audits excluded from the denominators (3.14+ dedup) and
    # per-model telemetry slices ({model: {audited, no_signal,
    # misses}}, from the Stop hook's transcript-derived `client_model`
    # stamp). Both additive: zero / empty on pre-3.14 logs.
    repeat_audits: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

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
    # measure honestly. Tiering is `health.applied_tier`, shared so the
    # two surfaces cannot derive three tiers two ways.
    applied_model: int = 0
    applied_hook: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
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
            },
            "silent_miss_recent": [c.to_dict() for c in self.silent_miss_recent],
        }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def parse_since(spec: str | None) -> timedelta | None:
    """Parse a ``--since`` argument into a timedelta, or ``None`` for "all".

    Accepts ``Ns`` / ``Nm`` / ``Nh`` / ``Nd`` (positive integer). The
    literal strings ``"all"`` and empty/None return ``None`` (no time
    filter). Anything else raises ``ValueError`` with a hint.

    An out-of-range value raises ``ValueError`` too: the regex's ``\\d+``
    accepts an arbitrarily long digit run (e.g.
    ``999999999999999999999d``), and the resulting ``timedelta``
    constructor overflows with ``OverflowError``. We catch that and
    re-raise as ``ValueError`` so every caller — including the CLI
    handler that surfaces ``ValueError`` via ``parser.error`` — sees one
    clean exception type instead of an uncaught ``OverflowError``
    traceback.
    """
    if spec is None or spec == "" or spec == "all":
        return None
    match = _SINCE_PATTERN.fullmatch(spec.strip())
    if not match:
        raise ValueError(
            f"--since: expected 'Ns'/'Nm'/'Nh'/'Nd' or 'all', got {spec!r}. "
            "Examples: '30d', '12h', '90m', 'all'."
        )
    value = int(match.group("value"))
    if value <= 0:
        raise ValueError(f"--since: value must be positive, got {value!r}")
    unit = match.group("unit")
    try:
        return timedelta(seconds=value * _SINCE_UNIT_TO_SECONDS[unit])
    except OverflowError as exc:
        raise ValueError(
            f"--since: value out of range: {spec!r}. "
            "Pick a smaller window (or 'all' for no time filter)."
        ) from exc


def compute_eval(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    since: timedelta | None = None,
    scope: str | None = None,
    endorsement_min_retrievals: int = DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
    silent_miss_limit: int = DEFAULT_SILENT_MISS_LIMIT,
    tombstoned_ids: set[str] | None = None,
) -> EvalReport:
    """Build an ``EvalReport`` from active memories + the event stream.

    ``events`` is expected in chronological order (``iter_all_events``
    produces this). ``now`` defaults to the current UTC instant.

    ``since`` filters events to those with ``ts >= now - since``;
    ``None`` admits all events. ``scope`` filters to events that
    reference at least one memory tagged with that scope — search /
    show / use events all carry a memory id and are mapped to scopes
    via the live store. ``turn_audited`` and ``search_miss`` are
    *not* filtered by scope (they're per-turn), so the silent-miss
    rate reflects the global cadence regardless of scope filter.

    Silent-miss telemetry honors the same two invalidation markers
    ``health.compute_health`` / ``health.curation_counts`` honor, so
    every surface reporting ``silent_miss_rate`` agrees over the same
    event stream: a ``silent_miss_cutoff`` event (written by
    ``bettermemory consolidate --acknowledge-misses-before``) drops
    ``turn_audited`` / ``search_miss`` events earlier than the latest
    ``cutoff_ts`` from the numerator, both denominator buckets, and
    the triage buffer — an earlier cutoff seen later in the log cannot
    shrink the window — and a ``miss_ack`` event (written by
    ``memory_acknowledge_miss``) drops the one referenced miss from
    the numerator while the denominator keeps its turn. Both markers
    are global: they apply even when their own ``ts`` falls outside
    the ``since`` window, mirroring ``curation_counts``' delta-mode
    exemption. Streams carrying no markers count exactly as before.

    ``tombstoned_ids`` mirrors ``health._silent_miss_stats``' filter
    #2: a ``search_miss`` whose canonical top-hit id
    (``top_hits[0].id``) is in the set drops from the numerator and
    the triage buffer — once the memory is gone the miss is no longer
    actionable — while both audited denominator buckets keep their
    turns (``turn_audited`` carries no per-memory payload). Default
    ``None`` preserves the tombstone-blind behavior byte-identically,
    so callers without a store handle (the comparative harness, the
    scripted driver) are untouched; the CLI wrapper enumerates the
    real set via ``store.load_tombstones()``, same as
    ``health.report_for_directory``. Legacy ``top_hit_ids``-shaped
    events carry no canonical top-hit id and fall through the filter
    (health's can't-prove-tombstoned conservative read).

    A memory that's been tombstoned still counts toward retrieval /
    use occurrences — we attribute via id, not via live status — but
    it cannot participate in cold-endorsement rows because we need
    its body/scopes for display.
    """
    now = now or datetime.now(timezone.utc)
    cutoff: datetime | None = (now - since) if since is not None else None
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

    # Counters.
    retrieval_occurrences = 0
    explicit_endorsements_with_excerpt = 0
    applied_total = 0
    applied_explicit = 0
    # `applied_explicit` split by producer (hook containment match vs
    # real `memory_record_use` call) — see the EvalReport fields.
    applied_model = 0
    applied_hook = 0
    turns_audited = 0
    turns_no_signal = 0
    silent_misses = 0
    total_events_scanned = 0
    events_in_window = 0

    # Per-memory counts for cold-endorsement rollup.
    retrieval_count: dict[str, int] = {}
    auto_applied_count: dict[str, int] = {}
    explicit_applied_count: dict[str, int] = {}

    # Rolling buffer of recent silent-miss events (last K, since
    # events come in chronological order — we just keep the tail).
    silent_miss_buffer: list[SilentMissCandidate] = []

    # Track the most recent threshold rule we've seen on any miss
    # event so the renderer can name the regime the numbers came
    # from. Empty string means no audits in the window.
    threshold_rule = ""

    # In-window ``turn_audited`` / ``search_miss`` events, buffered for
    # post-pass resolution against the invalidation markers below —
    # counting them inline would let a marker later in the log arrive
    # too late to retract an already-counted event. Same
    # buffer-then-resolve shape as ``health._StatsAccumulator``.
    audit_telemetry: list[dict[str, Any]] = []
    # The two escape hatches health.py's rollups honor, mirrored here
    # so the eval CLI, memory_health, and memory_scope_overview agree
    # over the same event stream: `silent_miss_cutoff` wipes all miss
    # telemetry before its `cutoff_ts`; `miss_ack` retracts one
    # `search_miss` by `event_id`.
    latest_miss_cutoff: datetime | None = None
    acknowledged_miss_event_ids: set[str] = set()
    # Repeat audits (the same session+message re-probed by a
    # multi-stop turn; `repeat=True` since 3.14) are excluded from
    # every denominator — their companion `search_miss` is never
    # emitted, so counting them would dilute the rate. Tallied
    # separately so the report can show how much re-audit noise the
    # dedup absorbed.
    repeat_audits = 0
    # Per-model slices of the audit telemetry, keyed by the
    # `client_model` the Stop hook stamps off the transcript (absent
    # on pre-3.14 events and on the MCP producer — those events simply
    # don't bucket; no "unknown" row is manufactured).
    by_model: dict[str, dict[str, int]] = {}

    for ev in events:
        if not isinstance(ev, dict):
            continue
        total_events_scanned += 1
        # Window membership is decided up front — BEFORE the marker
        # short-circuits below, which deliberately bypass the window
        # filter — so `events_in_window` covers exactly the population
        # `total_events_scanned` does, only window-scoped. An event with
        # a missing or unparseable `ts` counts as out-of-window, the
        # same predicate the filter further down applies.
        in_window = True
        if cutoff is not None:
            event_ts = _parse_ts(ev.get("ts"))
            in_window = event_ts is not None and event_ts >= cutoff
        if in_window:
            events_in_window += 1

        kind = ev.get("kind")
        # Invalidation markers — resolved BEFORE the `since` window
        # filter, mirroring `curation_counts`' delta-mode exemption:
        # both are global markers, so a cutoff/ack whose own ts falls
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
                latest_miss_cutoff is None or parsed_cutoff > latest_miss_cutoff
            ):
                latest_miss_cutoff = parsed_cutoff
            continue
        if kind == "miss_ack":
            # A missing / non-string / empty `event_id` is a malformed
            # admin event; ignore it rather than poisoning the set.
            target = ev.get("event_id")
            if isinstance(target, str) and target:
                acknowledged_miss_event_ids.add(target)
            continue

        if not in_window:
            continue

        if kind in ("search", "list"):
            # `list` is bundled with `search` because `audit.py` and the
            # tool-usage map both treat memory_list as a retrieval
            # surface (the model uses it to browse, the same way it
            # uses memory_search to query). Keeping these aligned
            # keeps the eval denominator consistent with audit cadence.
            # Legacy-name fallback (`memory_ids` / `hit_ids`) — same
            # discipline as the other event consumers (hook /
            # consolidate / _handlers). `list` events only ever wrote
            # `returned`, but reading through the same fallback chain
            # is harmless.
            returned = (
                ev.get("returned") or ev.get("memory_ids") or ev.get("hit_ids") or []
            )
            if not isinstance(returned, list):
                continue
            for mid in returned:
                if not isinstance(mid, str):
                    continue
                if not passes_scope(mid):
                    continue
                retrieval_occurrences += 1
                retrieval_count[mid] = retrieval_count.get(mid, 0) + 1
        elif kind == "show":
            mid = ev.get("id")
            if not isinstance(mid, str):
                continue
            if not passes_scope(mid):
                continue
            retrieval_occurrences += 1
            retrieval_count[mid] = retrieval_count.get(mid, 0) + 1
        elif kind == "use":
            outcome = ev.get("outcome")
            if outcome != "applied":
                continue
            ids = ev.get("ids") or ev.get("memory_ids") or []
            if not isinstance(ids, list):
                continue
            # Tiering via the shared `health.applied_tier` — auto /
            # hook / model, with the strict `auto is True` identity
            # comparison (a stray truthy `auto=1` / `auto="true"` reads
            # as NON-auto, same as missing/None, so we never silently
            # relabel borderline data as the server closing the loop).
            # Shared rather than re-derived here: this module and
            # `health._StatsAccumulator._handle_use` publish the same
            # split under different names, and the hand-mirrored version
            # of this comment used to cite a health.py line number that
            # had been wrong for several releases.
            tier = _applied_tier(ev)
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
                applied_total += 1
                if is_auto:
                    auto_applied_count[mid] = auto_applied_count.get(mid, 0) + 1
                else:
                    applied_explicit += 1
                    if tier == "hook":
                        applied_hook += 1
                    else:
                        applied_model += 1
                    explicit_applied_count[mid] = explicit_applied_count.get(mid, 0) + 1
                    if isinstance(excerpt, str) and excerpt.strip():
                        explicit_endorsements_with_excerpt += 1
        elif kind in ("turn_audited", "search_miss"):
            # Buffered rather than counted inline so a cutoff/ack
            # marker later in the log can retroactively invalidate
            # earlier telemetry. Resolution happens after the pass,
            # below — chronological order is preserved, so the
            # counting is identical to the pre-buffer inline loop
            # whenever no marker exists.
            audit_telemetry.append(ev)

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
    for ev in audit_telemetry:
        if latest_miss_cutoff is not None:
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts < latest_miss_cutoff:
                continue
        if ev.get("kind") == "turn_audited":
            if ev.get("repeat"):
                repeat_audits += 1
                continue
            # `no_signal` audits (empty store, gated probe, semantic model
            # unavailable) are not miss-capable turns; counting them dilutes
            # silent_miss_rate's denominator. Mirrors health.py's
            # `no_signal_total` split (round 88) so a config stuck at
            # permanent no_signal reads as "probe declined", not "healthy".
            if ev.get("verdict") == "no_signal":
                turns_no_signal += 1
            else:
                turns_audited += 1
            model = ev.get("client_model")
            if isinstance(model, str) and model:
                bucket = by_model.setdefault(
                    model, {"audited": 0, "no_signal": 0, "misses": 0}
                )
                if ev.get("verdict") == "no_signal":
                    bucket["no_signal"] += 1
                else:
                    bucket["audited"] += 1
            rule = ev.get("threshold_rule")
            if isinstance(rule, str) and rule:
                threshold_rule = rule
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
            if isinstance(event_id, str) and event_id in acknowledged_miss_event_ids:
                continue
            silent_misses += 1
            model = ev.get("client_model")
            if isinstance(model, str) and model:
                by_model.setdefault(model, {"audited": 0, "no_signal": 0, "misses": 0})[
                    "misses"
                ] += 1
            rule = ev.get("threshold_rule")
            if isinstance(rule, str) and rule:
                threshold_rule = rule
            candidate = _silent_miss_from_event(ev)
            if candidate is not None:
                silent_miss_buffer.append(candidate)
                if silent_miss_limit <= 0:
                    silent_miss_buffer = []
                elif len(silent_miss_buffer) > silent_miss_limit:
                    silent_miss_buffer = silent_miss_buffer[-silent_miss_limit:]

    # Cold-endorsement rows: retrieval_count >= floor AND at least one
    # apply happened AND explicit_applied_count == 0. Ambient memories
    # are excluded — same rationale as health's cold_endorsement_memories
    # bucket (their value is implicit; an explicit use event is
    # structurally rare).
    #
    # The "at least one apply" gate (auto + explicit > 0) keeps the
    # bucket as the COMPLEMENT of dead_weight, matching health's
    # `_is_weakly_endorsed`: cold-endorsement means "applies happened,
    # but every one was the auto fallback." A memory retrieved over the
    # floor with zero applies is dead_weight, not cold-endorsement, and
    # must not surface here.
    floor = max(1, int(endorsement_min_retrievals))
    cold_rows: list[ColdEndorsementMemoriesRow] = []
    cold_total = 0
    for mid, rcount in retrieval_count.items():
        if rcount < floor:
            continue
        if auto_applied_count.get(mid, 0) + explicit_applied_count.get(mid, 0) == 0:
            continue
        if explicit_applied_count.get(mid, 0) > 0:
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
                auto_applied_count=auto_applied_count.get(mid, 0),
                explicit_applied_count=0,
            )
        )
    # Sort by retrieval_count descending so the chattiest dead-letter
    # rows surface first; tie-break by id for determinism.
    cold_rows.sort(key=lambda r: (-r.retrieval_count, r.id))

    report = EvalReport(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        scope_filter=scope,
        threshold_rule=threshold_rule,
        total_events_scanned=total_events_scanned,
        events_in_window=events_in_window,
        retrieval_occurrences=retrieval_occurrences,
        explicit_endorsements_with_excerpt=explicit_endorsements_with_excerpt,
        applied_total=applied_total,
        applied_explicit=applied_explicit,
        applied_model=applied_model,
        applied_hook=applied_hook,
        turns_audited=turns_audited,
        turns_no_signal=turns_no_signal,
        silent_misses=silent_misses,
        cold_endorsement_memories_rows=cold_rows,
        cold_endorsement_memories_total=cold_total,
        endorsement_min_retrievals=floor,
        silent_miss_recent=silent_miss_buffer,
        repeat_audits=repeat_audits,
        by_model=by_model,
    )
    report.memory_helped_rate = RateCI.from_counts(
        explicit_endorsements_with_excerpt, retrieval_occurrences
    )
    report.endorsement_rate = RateCI.from_counts(applied_explicit, applied_total)
    report.silent_miss_rate = RateCI.from_counts(silent_misses, turns_audited)
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(report: EvalReport) -> str:
    """Plain-text rendering for the CLI. Stable across versions — tests
    will pin a representative shape so changes here surface in review."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    scope_part = f" · scope={report.scope_filter}" if report.scope_filter else ""
    lines.append(f"bettermemory eval — last {window}{scope_part}")
    lines.append("─" * 60)
    # Window-scoped, matching the "— last {window}" header above.
    # `total_events_scanned` counts the WHOLE log (marker resolution
    # runs ahead of the window filter), so printing it here published
    # an all-time figure under a windowed label.
    lines.append(f"Events scanned                      {report.events_in_window:>5d}")
    lines.append(
        f"Retrieval occurrences               {report.retrieval_occurrences:>5d}"
    )
    lines.append(f"Applied use events (auto+explicit)  {report.applied_total:>5d}")
    lines.append(f"Turns audited                       {report.turns_audited:>5d}")
    if report.turns_no_signal:
        lines.append(
            f"Turns no-signal (excluded)          {report.turns_no_signal:>5d}"
        )
    if report.repeat_audits:
        lines.append(f"Repeat audits (deduped, excluded)   {report.repeat_audits:>5d}")
    lines.append("")
    lines.append(_format_rate("memory_helped_rate", report.memory_helped_rate))
    lines.append(_format_rate("endorsement_rate ", report.endorsement_rate))
    lines.append(_format_rate("silent_miss_rate ", report.silent_miss_rate))

    if report.by_model:
        lines.append("")
        lines.append("Per-model audit telemetry (Stop-hook `client_model` stamp):")
        for model in sorted(report.by_model):
            counts = report.by_model[model]
            lines.append(
                f"  {model:<28s} audited={counts.get('audited', 0):<5d} "
                f"misses={counts.get('misses', 0):<4d} "
                f"no_signal={counts.get('no_signal', 0)}"
            )

    if report.cold_endorsement_memories_rows or report.cold_endorsement_memories_total:
        lines.append("")
        lines.append(
            f"Cold-endorsement memories "
            f"(retrievals ≥ {report.endorsement_min_retrievals}, 0 explicit applied): "
            f"{report.cold_endorsement_memories_total}"
        )
        for row in report.cold_endorsement_memories_rows[:10]:
            scopes = ",".join(row.scopes) or "—"
            lines.append(
                f"  {row.id}  {scopes:<18s}  "
                f'"{_truncate(row.summary, 50)}"  ({row.retrieval_count} retrievals)'
            )
        if report.cold_endorsement_memories_total > 10:
            lines.append(f"  … plus {report.cold_endorsement_memories_total - 10} more")

    if report.silent_miss_recent:
        lines.append("")
        lines.append(f"Silent-miss candidates (last {len(report.silent_miss_recent)}):")
        for c in report.silent_miss_recent:
            top = c.top_missed_id or "—"
            rel = c.top_missed_relevance or "—"
            lines.append(
                f"  {_short_ts(c.ts)}  session={c.session_id[:12]}…  "
                f"missed={top} relevance={rel}"
            )

    if report.threshold_rule:
        lines.append("")
        lines.append(f"Threshold rule: {report.threshold_rule}")
    if (
        report.memory_helped_rate.torn_read
        or report.endorsement_rate.torn_read
        or report.silent_miss_rate.torn_read
    ):
        lines.append("")
        lines.append(
            "NOTE: numerator > denominator (rate clamped to 1.0). Usually a "
            "windowing artifact under --since (a use event is in-window while "
            "its retrieval aged out), or — less often — a log read mid-rotation. "
            "Re-run without --since to rule out the window edge."
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _wilson_interval(k: int, n: int, z: float = _WILSON_Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion k / n.

    Returns ``(lower, upper)`` clamped to [0, 1]. Caller guarantees
    ``n > 0`` — this helper does not handle zero denominators, since
    the surrounding RateCI factory already returns ``(None, None)``
    in that case.
    """
    if n <= 0:
        return (0.0, 1.0)
    # ``k > n`` shouldn't happen on real-world counts, but the event
    # log is multiple files with partial rotation — a torn read could
    # surface a numerator larger than the denominator. Clamp rather
    # than raise math.sqrt(negative).
    if k > n:
        k = n
    if k < 0:
        k = 0
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


# `_parse_ts` is a thin module-local alias for the canonical
# `time_utils.parse_event_ts`. The eval module reads it as if it were
# local; the indirection centralises the parse semantics without
# routing every call site through the time_utils import path. Same
# tz-aware UTC contract — a naive ISO string comes back stamped as UTC
# so callers can compare against the tz-aware cutoff every rollup
# derives from `datetime.now(timezone.utc)` without raising.
_parse_ts = parse_event_ts


def _canonical_top_hit_id(ev: dict[str, Any]) -> str | None:
    """The tombstone-filter key for a ``search_miss`` event: the ``id``
    of the first entry in the canonical ``top_hits`` payload, or None
    when the shape is malformed or legacy.

    Mirrors the ``top_hit_id`` half of ``health._parse_silent_miss_event``
    exactly — canonical-only, NO ``top_hit_ids`` fallback. The legacy
    fallback in ``_silent_miss_from_event`` exists for the renderer's
    ``top_missed_id`` display; using it for the tombstone filter would
    drop legacy events health's filter #2 counts (its parser reads the
    legacy shape as None, which falls through on the
    can't-prove-tombstoned conservative read), diverging the two
    surfaces on pre-2.6.4 archives.
    """
    top_hits = ev.get("top_hits")
    if isinstance(top_hits, list) and top_hits:
        first = top_hits[0]
        if isinstance(first, dict):
            candidate = first.get("id")
            if isinstance(candidate, str):
                return candidate
    return None


def _silent_miss_from_event(ev: dict[str, Any]) -> SilentMissCandidate | None:
    """Pull the renderer-relevant fields out of a ``search_miss`` event.

    The full event carries the top-N probe hits; the renderer only
    needs the topmost (id + relevance) plus context. Returns ``None``
    when the event is malformed so the caller can ignore it without
    polluting the buffer.
    """
    ts = ev.get("ts")
    session_id = ev.get("session_id") or ev.get("session")
    if not isinstance(ts, str) or not isinstance(session_id, str):
        return None
    top_id: str | None = None
    top_relevance: str | None = None
    top_hits = ev.get("top_hits")
    if isinstance(top_hits, list) and top_hits:
        first = top_hits[0]
        if isinstance(first, dict):
            cand_id = first.get("id")
            cand_rel = first.get("relevance")
            if isinstance(cand_id, str):
                top_id = cand_id
            if isinstance(cand_rel, str):
                top_relevance = cand_rel
    else:
        # Legacy fallback for pre-2.6.4 hook-originated events: those
        # wrote `top_hit_ids=[strings]` instead of `top_hits=[dicts]`.
        # Match the discipline 70e41a4 established for the llm.py
        # field-name fix — read canonical, fall back to legacy.
        # Relevance isn't recoverable from the legacy shape (only ids
        # were stored); top_relevance stays None for old archives.
        legacy_ids = ev.get("top_hit_ids")
        if isinstance(legacy_ids, list) and legacy_ids:
            first_id = legacy_ids[0]
            if isinstance(first_id, str):
                top_id = first_id
    rule = ev.get("threshold_rule")
    rule_s = rule if isinstance(rule, str) else None
    recent = ev.get("recent_retrieval_count")
    # `bool` is a subclass of `int` in Python, so a stray `True` / `False`
    # in the field would slip past a naked `isinstance(_, int)` check and
    # count as 1 / 0. Exclude bools explicitly so a torn event reads as 0
    # rather than silently distorting the audit numerator.
    recent_i = recent if isinstance(recent, int) and not isinstance(recent, bool) else 0
    return SilentMissCandidate(
        ts=ts,
        session_id=session_id,
        top_missed_id=top_id,
        top_missed_relevance=top_relevance,
        threshold_rule=rule_s,
        recent_retrieval_count=recent_i,
    )


def _format_rate(label: str, rate: RateCI) -> str:
    if rate.rate is None or rate.lower is None or rate.upper is None:
        # ``n/a`` rather than 0 — denominator was empty, the rate is
        # undefined. Renderer surfaces the count so the consumer can
        # see *why* it's undefined.
        return f"{label:<20s} n/a    (k={rate.numerator}, n={rate.denominator})"
    # Render the actual asymmetric Wilson bounds, not `rate ± half`. The
    # Wilson interval is asymmetric and the point estimate k/n is NOT its
    # center, so `± half` reconstructed neither bound and printed impossible
    # probabilities at small n (e.g. 1/1 -> "1.00 ± 0.40", implying an upper
    # of 1.40). Brackets match the machine-readable ci95_lower/ci95_upper.
    bar = _bar(rate.rate)
    return (
        f"{label:<20s} {rate.rate:0.2f} [{rate.lower:0.2f}, {rate.upper:0.2f}]   "
        f"{bar}   (k={rate.numerator}, n={rate.denominator})"
    )


def _bar(rate: float, width: int = 10) -> str:
    """Render a rate ∈ [0,1] as a heavy-light unicode bar."""
    rate = max(0.0, min(1.0, rate))
    full = round(rate * width)
    return "▇" * full + "▁" * (width - full)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _humanize_seconds(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _short_ts(ts: str) -> str:
    # ``YYYY-MM-DDTHH:MM:SS…`` → ``YYYY-MM-DD HH:MM``.
    if len(ts) < 16 or "T" not in ts:
        return ts
    date_part, _, rest = ts.partition("T")
    time_part = rest[:5]
    return f"{date_part} {time_part}"


# ---------------------------------------------------------------------------
# Threshold-rule sweep — replay logged misses under alternative rules
# ---------------------------------------------------------------------------

# A threshold rule is a pure function that, given the top hits the
# probe returned and the recent retrieval count, decides whether the
# event should be flagged as a silent miss. The rule registry below
# carries every rule by name; the sweep walks logged `search_miss`
# events and asks each rule the counterfactual question: would *this*
# rule have flagged the same event? Because `search_miss` events only
# exist for turns the *current* rule (v1) flagged, the sweep can only
# meaningfully compare rules that are equally strict or stricter than
# v1 — a strictly looser rule would also fire on turns where v1
# didn't, and those turns aren't in the event log to replay (the
# `turn_audited` companion event doesn't carry `top_hits`). Stricter
# alternatives let the maintainer answer "is v1 over-firing? would
# raising the bar drop the miss count from N to N'?" — which is the
# calibration question audit.py's docstring flags as open.
#
# The rule signature stays narrow on purpose: `(top_hits,
# recent_retrieval_count) -> bool`. Anything more would require
# replaying historical user messages against the live ranker, which
# the log doesn't preserve (the message is the user's private query;
# logging it verbatim is precisely the surface 2.6.8 closed).


@dataclass(frozen=True)
class ThresholdRule:
    """Named decision rule for the silent-miss audit.

    `check(top_hits, recent_retrieval_count) -> bool` returns True when
    the event should be flagged as a miss. The top_hits list is the
    same shape that lands in the `search_miss` event under the
    `top_hits` key: a list of dicts with `id`, `score`, `relevance`,
    `scopes`, `snippet`.
    """

    name: str
    description: str
    check: Any  # Callable[[list[dict[str, Any]], int], bool]


def _rule_v1_top1_high(
    top_hits: list[dict[str, Any]], recent_retrieval_count: int
) -> bool:
    """Current default: top-1 relevance == "high" AND no recent retrieval.
    The relevance check is what `probe_for_miss` already runs; we
    re-derive it here so the rule is replayable from the logged event
    alone, without depending on the live ranker."""
    if recent_retrieval_count > 0:
        return False
    if not top_hits:
        return False
    return top_hits[0].get("relevance") == "high"


def _rule_v2_top1_high_score_50(
    top_hits: list[dict[str, Any]], recent_retrieval_count: int
) -> bool:
    """Tightening v1: also require the top-1 score >= 50. The keyword
    ranker emits scores roughly in [0, 200] for typical queries; 50
    is a conservative floor that filters single-token "high" hits that
    score in the 1-token-coverage low end. Replayable from the event
    alone because score lands on the same dict."""
    if not _rule_v1_top1_high(top_hits, recent_retrieval_count):
        return False
    top = top_hits[0]
    score = top.get("score")
    if not isinstance(score, (int, float)):
        return False
    return score >= 50.0


def _rule_v3_top1_high_dominant(
    top_hits: list[dict[str, Any]], recent_retrieval_count: int
) -> bool:
    """Tightening v1: require the top-1 hit to be at least 2x the score
    of the second hit (or be the only hit). The dominance signal
    distinguishes "one obvious match the model should have caught"
    from "a borderline ranker fluke where several hits tied."""
    if not _rule_v1_top1_high(top_hits, recent_retrieval_count):
        return False
    top = top_hits[0]
    top_score = top.get("score")
    if not isinstance(top_score, (int, float)):
        return False
    if len(top_hits) < 2:
        # Only one hit; dominance is trivially satisfied.
        return True
    second = top_hits[1]
    second_score = second.get("score")
    if not isinstance(second_score, (int, float)) or second_score <= 0:
        return True
    return top_score >= 2 * second_score


def _rule_v4_top1_high_strict_combined(
    top_hits: list[dict[str, Any]], recent_retrieval_count: int
) -> bool:
    """The intersection of v2 (score floor) and v3 (dominance). The
    most conservative of the bundled rules — if this fires, the event
    cleared both alternate strictness tests as well."""
    return _rule_v2_top1_high_score_50(
        top_hits, recent_retrieval_count
    ) and _rule_v3_top1_high_dominant(top_hits, recent_retrieval_count)


# Registry. Stable across the public API — adding new rules is
# additive; removing/renaming requires a deprecation cycle so callers
# of `compute_threshold_sweep` can pin a known-good set.
THRESHOLD_RULES: dict[str, ThresholdRule] = {
    "v1_top1_high": ThresholdRule(
        name="v1_top1_high",
        description="Current default: top-1 hit relevance == 'high' AND "
        "no retrieval (search/show/list) in the lookback window.",
        check=_rule_v1_top1_high,
    ),
    "v2_top1_high_score_50": ThresholdRule(
        name="v2_top1_high_score_50",
        description="v1 + top-1 score >= 50.0. Filters single-token "
        "high-relevance hits that score only on coverage.",
        check=_rule_v2_top1_high_score_50,
    ),
    "v3_top1_high_dominant": ThresholdRule(
        name="v3_top1_high_dominant",
        description="v1 + top-1 score >= 2x top-2 score. Distinguishes "
        "one-clear-match from borderline-ranker-noise.",
        check=_rule_v3_top1_high_dominant,
    ),
    "v4_top1_high_strict_combined": ThresholdRule(
        name="v4_top1_high_strict_combined",
        description="Intersection of v2 and v3: must clear both the "
        "score floor and the dominance test.",
        check=_rule_v4_top1_high_strict_combined,
    ),
}


@dataclass
class ThresholdSweepRow:
    rule: str
    description: str
    would_flag: int
    delta_from_v1: int  # negative means stricter than v1 (flags fewer)
    delta_pct: float | None  # share of v1 misses this rule would still flag

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "description": self.description,
            "would_flag": self.would_flag,
            "delta_from_v1": self.delta_from_v1,
            "delta_pct": self.delta_pct,
        }


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

    - `silent_miss_cutoff` (bulk, written by `bettermemory
      consolidate --acknowledge-misses-before`) IS honored: events
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
        # deliberately not resolved here — see the docstring.
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

    # Resolve against the cutoff: an invalidated event (ts strictly
    # before `cutoff_ts`; an unparseable ts drops too once a cutoff
    # exists — `health._count_post_cutoff`'s conservative read)
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


def render_threshold_sweep_text(report: ThresholdSweepReport) -> str:
    """Plain-text rendering: one row per rule, showing how many of the
    `replayable_misses` it would flag and the absolute / percentage
    delta vs. v1."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --threshold-sweep — last {window}")
    lines.append("─" * 60)
    # Window-scoped, matching the header — see `EvalReport.events_in_window`.
    lines.append(f"Events scanned         {report.events_in_window:>5d}")
    lines.append(f"Replayable misses      {report.replayable_misses:>5d}")
    if report.skipped_legacy_event_count > 0:
        lines.append(
            f"  (skipped {report.skipped_legacy_event_count} legacy events "
            "carrying top_hit_ids only — no relevance label to replay against)"
        )
    if report.v1_drift != 0:
        lines.append(
            f"  ⚠ v1 replay drift: {report.v1_drift:+d} (the in-process "
            "v1 rule disagrees with the production decision on that many "
            "events — check audit.py / eval.THRESHOLD_RULES for divergence)"
        )
    if report.replayable_misses == 0:
        lines.append("")
        lines.append(
            "No replayable misses in window. The sweep needs `search_miss` "
            "events with `top_hits` (2.6.4+) to produce a comparison; "
            "run with `--since all` if your recent window is empty, or "
            "wait until `memory_audit_turn` has fired a few times."
        )
        return "\n".join(lines) + "\n"
    lines.append("")
    lines.append(f"{'rule':<32s} {'flagged':>7s}  {'Δ v1':>7s}  {'% v1':>6s}")
    for row in report.rows:
        if row.delta_pct is None:
            pct = "—"
        else:
            pct = f"{row.delta_pct * 100:5.1f}%"
        delta = f"{row.delta_from_v1:+d}" if row.rule != "v1_top1_high" else "—"
        lines.append(f"  {row.rule:<30s} {row.would_flag:>7d}  {delta:>7s}  {pct:>6s}")
    lines.append("")
    lines.append("Caveat: this is a *relative* sweep over events the v1 rule")
    lines.append("already flagged. Strictly looser rules replay over the")
    lines.append("turn_audited stream instead — see --widening-preview.")
    return "\n".join(lines) + "\n"


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


def _rule_w1_top1_v2_high(
    top_hits: list[dict[str, Any]], recent_retrieval_count: int
) -> bool:
    """Widening candidate: top-1 SHADOW label == "high" and no recent
    retrieval. The shadow label (`search._relevance_label_v2`) keeps
    v1's coverage arm and adds an absolute matched-token floor, so this
    rule flags a strict superset of the v1 replay — the delta is
    exactly the cohort the coverage-fraction blind spot hides."""
    if recent_retrieval_count > 0:
        return False
    if not top_hits:
        return False
    return top_hits[0].get("relevance_v2") == "high"


def _rule_w2_top1_v2_high_from_medium(
    top_hits: list[dict[str, Any]], recent_retrieval_count: int
) -> bool:
    """Tightened widening candidate: v1's own high arm, plus the
    medium→high promotions of the absolute matched-token floor — but
    NOT the low→high ones.

    Calibrated on the first live labeling pass over the w1 cohort
    (2026-07-08; 103 replayable turns, 32 w1 flags, hand-labeled via
    `--widening-preview --detail`): v1-low turns promoted by the bare
    floor were almost pure noise — long pasted messages crossing
    matched_unique >= 4 against any domain-adjacent memory at
    coverage ~0.2 (charitable precision ~20%) — while v1-medium
    promotions read ~50% precision and contained every clearly-real
    catch. That is also the original blind-spot thesis: long
    natural-language queries landing at MEDIUM on strong matches, not
    at low. Keeping the replayed v1 arm preserves the strict-superset
    property that makes Δ v1 interpretable."""
    if _rule_v1_top1_high(top_hits, recent_retrieval_count):
        return True
    if recent_retrieval_count > 0:
        return False
    if not top_hits:
        return False
    top = top_hits[0]
    if not isinstance(top, dict):
        return False
    return top.get("relevance") == "medium" and top.get("relevance_v2") == "high"


# Registry of widening candidates, separate from THRESHOLD_RULES on
# purpose: mixing looser rules into the strict sweep would render
# meaningless rows there (over the v1-flagged replay set a widening
# always reads 100%). Additive like THRESHOLD_RULES.
WIDENING_RULES: dict[str, ThresholdRule] = {
    "w1_top1_v2_high": ThresholdRule(
        name="w1_top1_v2_high",
        description="Top-1 shadow relevance_v2 == 'high' (coverage >= 0.75 "
        "OR matched_unique >= 4) AND no retrieval in window.",
        check=_rule_w1_top1_v2_high,
    ),
    "w2_top1_v2_high_from_medium": ThresholdRule(
        name="w2_top1_v2_high_from_medium",
        description="v1 high, OR top-1 promoted medium→high by the shadow "
        "matched-token floor. Excludes w1's low→high promotions (measured "
        "~20% precision on the 2026-07-08 labeling pass vs ~50% for "
        "medium→high).",
        check=_rule_w2_top1_v2_high_from_medium,
    ),
}


@dataclass
class WideningPreviewReport:
    """Replay of `WIDENING_RULES` over miss-capable `turn_audited` events.

    `audits_with_features` is the denominator — turn_audited events
    carrying the 3.14+ `top_hits` payload. Older events (and producers
    that probed to a no-hit result) land in `audits_without_features`
    so the report is explicit about how much history was replayable.
    Repeat audits are excluded (`repeat_audits_skipped`), matching the
    rate surfaces. The bulk `silent_miss_cutoff` marker is deliberately
    NOT applied: it retracts pre-3.14 miss batches, and no event that
    predates the feature payload can enter this replay anyway.
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
            "v1_baseline_flagged": self.v1_baseline_flagged,
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass
class _ReplayableAudits:
    """Shared walk result for the widening lanes (counting + detail).

    One filter pipeline feeding both `compute_widening_preview` and
    `compute_widening_detail` keeps the two surfaces in lockstep — a
    turn the counting lane includes is exactly a turn the detail lane
    can show, so "N flagged" and the list of N can never disagree.
    """

    total_events_scanned: int
    # Window-scoped twin — the widening renderers label their counts
    # "— last {window}", so the preview report reads this, not the
    # all-time tally.
    events_in_window: int
    with_features: int
    without_features: int
    repeats_skipped: int
    # One (event, top_hits, recent_retrieval_count) triple per
    # replayable audit, in stream order.
    rows: list[tuple[dict[str, Any], list[dict[str, Any]], int]]


def _collect_replayable_audits(
    events: Iterable[dict[str, Any]],
    *,
    since: timedelta | None,
    now: datetime,
) -> _ReplayableAudits:
    """Filter the event stream down to replayable audited turns.

    Miss-capable (`verdict != "no_signal"`), non-repeat
    `turn_audited` events carrying a non-empty `top_hits` payload.
    Materialised (not a generator) because both consumers need the
    skip counters alongside the rows.
    """
    cutoff: datetime | None = (now - since) if since is not None else None
    total_events_scanned = 0
    events_in_window = 0
    without_features = 0
    repeats_skipped = 0
    rows: list[tuple[dict[str, Any], list[dict[str, Any]], int]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        total_events_scanned += 1
        # Window membership decided up front — BEFORE the kind dispatch
        # below `continue`s — so `events_in_window` covers exactly the
        # population `total_events_scanned` does, only window-scoped.
        # Same predicate as the per-event filter a few lines down.
        if cutoff is None:
            events_in_window += 1
        else:
            window_ts = _parse_ts(ev.get("ts"))
            if window_ts is not None and window_ts >= cutoff:
                events_in_window += 1
        if ev.get("kind") != "turn_audited":
            continue
        ts = _parse_ts(ev.get("ts"))
        if cutoff is not None and (ts is None or ts < cutoff):
            continue
        if ev.get("repeat"):
            repeats_skipped += 1
            continue
        if ev.get("verdict") == "no_signal":
            continue
        top_hits = ev.get("top_hits")
        if not isinstance(top_hits, list) or not top_hits:
            without_features += 1
            continue
        # Element guard, not just the container: the event log is
        # plaintext + git-synced + hand-editable, so a well-formed list
        # whose FIRST entry isn't a dict (`top_hits=["junk"]`) clears the
        # list check above but detonates at `top_hits[0].get(...)` — in
        # every replay rule (`_rule_v1_top1_high` / the `WIDENING_RULES`)
        # AND in the detail lane's evidence read. Validating the top hit
        # at this single choke point fixes preview, detail, and every
        # rule at once, mirroring `events._event_id_items`'
        # container-plus-element discipline. The top hit is the only
        # element any widening consumer reads, so guarding it suffices; a
        # non-dict top hit means the turn carries no usable features, so
        # it counts as feature-less alongside the pre-3.14 / no-hit rows.
        if not isinstance(top_hits[0], dict):
            without_features += 1
            continue
        recent = ev.get("recent_retrieval_count")
        # `bool` ⊂ `int` — same caveat as `_silent_miss_from_event`.
        if not isinstance(recent, int) or isinstance(recent, bool):
            recent = 0
        rows.append((ev, top_hits, recent))
    return _ReplayableAudits(
        total_events_scanned=total_events_scanned,
        events_in_window=events_in_window,
        with_features=len(rows),
        without_features=without_features,
        repeats_skipped=repeats_skipped,
        rows=rows,
    )


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
        v1_baseline_flagged=v1_count,
        rows=rows,
    )


def render_widening_preview_text(report: WideningPreviewReport) -> str:
    """Plain-text rendering, mirroring the strict sweep's shape."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --widening-preview — last {window}")
    lines.append("─" * 60)
    # Window-scoped, matching the header — see `EvalReport.events_in_window`.
    lines.append(f"Events scanned              {report.events_in_window:>5d}")
    lines.append(f"Replayable audited turns    {report.audits_with_features:>5d}")
    if report.audits_without_features:
        lines.append(
            f"  (skipped {report.audits_without_features} audits without "
            "top_hits — pre-3.14 events or no-hit probes)"
        )
    if report.repeat_audits_skipped:
        lines.append(
            f"  (skipped {report.repeat_audits_skipped} repeat audits — "
            "multi-stop re-probes of the same message)"
        )
    if report.audits_with_features == 0:
        lines.append("")
        lines.append(
            "No replayable audited turns yet. The preview needs "
            "`turn_audited` events carrying `top_hits` (3.14+); let the "
            "Stop hook run for a while and re-check."
        )
        return "\n".join(lines) + "\n"
    lines.append("")
    lines.append(f"v1 baseline (replayed)      {report.v1_baseline_flagged:>5d}")
    lines.append("")
    lines.append(f"{'rule':<32s} {'flagged':>7s}  {'Δ v1':>7s}  {'% v1':>6s}")
    for row in report.rows:
        if row.delta_pct is None:
            pct = "—"
        else:
            pct = f"{row.delta_pct * 100:5.1f}%"
        lines.append(
            f"  {row.rule:<30s} {row.would_flag:>7d}  "
            f"{row.delta_from_v1:+7d}  {pct:>6s}"
        )
    lines.append("")
    lines.append("Δ v1 counts turns the widened rule would flag that the")
    lines.append("replayed v1 baseline would not — the coverage-fraction")
    lines.append("blind-spot cohort. Both sides exclude shielded turns but")
    lines.append("not the project-suppression arm (see compute docstring),")
    lines.append("so absolute counts are upper bounds; the delta is the signal.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Widening detail — per-flagged-turn evidence for precision labeling
# ---------------------------------------------------------------------------
#
# The counting lane above answers "how MANY turns would a widened rule
# flag"; the flip decision the roadmap gates on needs "WHICH turns,
# against WHICH memories, on what evidence" — without that, a big
# delta is uninterpretable (it could be one over-matched memory or a
# genuinely wide blind spot). This lane dumps the flagged cohort so a
# human (or the model itself) can precision-label it.
#
# Evidence per turn is exactly what the `turn_audited` event already
# carries — no new logging: the redacted `probe_query`
# ({hash, preview, len} by default; the verbatim string only when
# `log_queries_verbatim` is on), the top hit's raw coverage pair, both
# relevance labels, and the hit's memory id joined against the active
# store + tombstone log for a summary. Rendering never widens
# exposure beyond what the log already holds.


def _probe_query_display(
    value: Any,
) -> tuple[str | None, int | None, str | None]:
    """Normalise the two on-disk `probe_query` shapes for display.

    Returns ``(preview, length, hash_prefix)``. The redacted shape
    (default since 2.6.8) is ``{hash, preview, len}``; the verbatim
    shape (opt-in `log_queries_verbatim`) is a plain string, whose
    "preview" is the string itself — the log already holds it, so the
    report reveals nothing new.
    """
    if isinstance(value, str):
        return value, len(value), None
    if isinstance(value, dict):
        preview = value.get("preview")
        length = value.get("len")
        digest = value.get("hash")
        return (
            preview if isinstance(preview, str) else None,
            length
            if isinstance(length, int) and not isinstance(length, bool)
            else None,
            digest if isinstance(digest, str) else None,
        )
    return None, None, None


@dataclass
class WideningFlaggedTurn:
    """One audited turn a widening rule would flag, with its evidence."""

    ts: str | None
    session_id: str | None
    client_model: str | None
    probe_query_preview: str | None
    probe_query_len: int | None
    probe_query_hash: str | None
    top_hit_id: str
    top_hit_score: float | None
    relevance_v1: str | None
    relevance_v2: str | None
    matched_unique: int | None
    query_unique: int | None
    v1_also_flagged: bool
    memory_status: str  # "active" | "tombstoned" | "unknown"
    memory_summary: str | None
    memory_scopes: list[str] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "session_id": self.session_id,
            "client_model": self.client_model,
            "probe_query_preview": self.probe_query_preview,
            "probe_query_len": self.probe_query_len,
            "probe_query_hash": self.probe_query_hash,
            "top_hit_id": self.top_hit_id,
            "top_hit_score": self.top_hit_score,
            "relevance_v1": self.relevance_v1,
            "relevance_v2": self.relevance_v2,
            "matched_unique": self.matched_unique,
            "query_unique": self.query_unique,
            "v1_also_flagged": self.v1_also_flagged,
            "memory_status": self.memory_status,
            "memory_summary": self.memory_summary,
            "memory_scopes": self.memory_scopes,
        }


@dataclass
class WideningMemoryRollup:
    """Aggregate of one memory's appearances as a flagged top hit.

    Flag concentration is the first diagnostic: N flags across two
    memories is a ranking problem with those two memories; N flags
    across N memories is a genuinely wide label change.
    """

    memory_id: str
    count: int
    distinct_sessions: int
    status: str
    summary: str | None
    scopes: list[str] | None
    score_min: float | None
    score_max: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "count": self.count,
            "distinct_sessions": self.distinct_sessions,
            "status": self.status,
            "summary": self.summary,
            "scopes": self.scopes,
            "score_min": self.score_min,
            "score_max": self.score_max,
        }


@dataclass
class WideningRuleDetail:
    """Flagged cohort for one widening rule."""

    rule: str
    description: str
    flagged_total: int
    beyond_v1: int
    turns: list[WideningFlaggedTurn] = field(default_factory=list)
    by_memory: list[WideningMemoryRollup] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "description": self.description,
            "flagged_total": self.flagged_total,
            "beyond_v1": self.beyond_v1,
            "turns": [t.to_dict() for t in self.turns],
            "by_memory": [m.to_dict() for m in self.by_memory],
        }


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
            "v1_baseline_flagged": self.v1_baseline_flagged,
            "rules": [r.to_dict() for r in self.rules],
        }


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
        status, summary, scopes = _resolve(memory_id)
        session = ev.get("session_id") or ev.get("session")
        turn = WideningFlaggedTurn(
            ts=ev.get("ts") if isinstance(ev.get("ts"), str) else None,
            session_id=session if isinstance(session, str) else None,
            client_model=(
                ev.get("client_model")
                if isinstance(ev.get("client_model"), str)
                else None
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
        for name in firing:
            flagged_by_rule[name].append(turn)
            sessions_by_rule_memory[name].setdefault(memory_id, set()).add(
                turn.session_id
            )

    rule_details: list[WideningRuleDetail] = []
    for name, rule in rules_in_use.items():
        turns = flagged_by_rule[name]
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
            row.distinct_sessions = len(
                sessions_by_rule_memory[name].get(memory_id, set())
            )
        by_memory = sorted(rollup.values(), key=lambda r: (-r.count, r.memory_id))
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
        v1_baseline_flagged=v1_count,
        rules=rule_details,
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_widening_detail_text(report: WideningDetailReport) -> str:
    """Plain-text rendering: per-memory concentration first (the fast
    diagnostic), then the flagged turns newest-first."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --widening-preview --detail — last {window}")
    lines.append("─" * 60)
    lines.append(
        f"Replayable audited turns {report.audits_with_features:>5d}    "
        f"v1 baseline (replayed) {report.v1_baseline_flagged:>4d}"
    )
    if report.audits_with_features == 0:
        lines.append("")
        lines.append(
            "No replayable audited turns yet. The detail lane needs "
            "`turn_audited` events carrying `top_hits` (3.14+); let the "
            "Stop hook run for a while and re-check."
        )
        return "\n".join(lines) + "\n"
    for detail in report.rules:
        lines.append("")
        lines.append(
            f"{detail.rule} — {detail.flagged_total} flagged, "
            f"{detail.beyond_v1} beyond v1"
        )
        lines.append(f"  {detail.description}")
        if not detail.turns:
            continue
        lines.append("")
        lines.append(f"  by top-hit memory ({len(detail.by_memory)} distinct):")
        for row in detail.by_memory:
            scores = (
                "score —"
                if row.score_min is None or row.score_max is None
                else f"score {row.score_min:.3f}–{row.score_max:.3f}"
            )
            scopes = ",".join(row.scopes or []) or "—"
            lines.append(
                f"  {row.count:>4d}×  {row.memory_id}  {row.status:<10s} "
                f"{scores}  sessions={row.distinct_sessions}"
            )
            lines.append(
                f"         [{_clip(scopes, 44)}] "
                f"{_clip(row.summary or '(no summary)', 60)}"
            )
        lines.append("")
        lines.append("  flagged turns (newest first):")
        for t in detail.turns:
            ts = (t.ts or "?")[:19]
            cov = (
                f"{t.matched_unique}/{t.query_unique}"
                if t.matched_unique is not None and t.query_unique is not None
                else "?/?"
            )
            marker = "  [v1 too]" if t.v1_also_flagged else ""
            preview = t.probe_query_preview or "(no probe_query logged)"
            suffix = (
                ""
                if t.probe_query_len is None
                or t.probe_query_preview is None
                or t.probe_query_len <= len(t.probe_query_preview)
                else f" (+{t.probe_query_len - len(t.probe_query_preview)} chars)"
            )
            lines.append(
                f"    {ts}  cov {cov:>6s}  v1={t.relevance_v1 or '?'}"
                f"{marker}  → {t.top_hit_id[:10]}…"
            )
            lines.append(f'      "{_clip(preview, 70)}"{suffix}')
    lines.append("")
    lines.append("Label each turn: would inlining the top hit's body have")
    lines.append("helped this message? Concentration on one memory means a")
    lines.append("ranking problem, not a label problem — fix the memory or")
    lines.append("the rule, not the formula.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Usage replay — the usage-signal flip bars' measurement surface
# ---------------------------------------------------------------------------
#
# The usage-aware ranking flags (`search.USAGE_FLAG_NAMES`) ship default-
# off with declared flip bars in docs/ROADMAP.md. The bars' replay clause
# reads exact per-turn toggle captures: `probe_for_miss` computes, inside
# the production ranker, what each flag-enabled probe's top-1 would have
# been with that one flag toggled off, and the capture lands additively
# on `turn_audited` / `prompt_recall` events (`usage_active` +
# `usage_toggles`). This section AGGREGATES those captures over a window
# and judges each changed top-1 under a pinned rule. It measures; it
# never decides — the bars are read against docs/ROADMAP.md by a human
# (or a session acting for one), and an unread bar is a hold.
#
# Why aggregation-only, and why no reconstruction lane for pre-capture
# history: the factors multiply per-LEG scores before RRF rank fusion,
# so no arithmetic on a logged fused score can reproduce the toggle —
# an "approximate" lane would put a number the mechanism can't back in
# front of a flip decision. Turns logged before the capture shipped are
# counted and labeled not-replayable instead.

# Pinned judgment rule for a changed top-1, versioned like the
# threshold rules so a future rule can be swept against the same log.
# v1: compare the shadow relevance labels (`relevance_v2`) of the
# production top-1 (flag ON) and the counterfactual top-1 (flag OFF)
# by tier; on a tier tie, more matched query tokens wins; still tied
# is "neutral". "improving" always means THE FLAG's pick is better.
USAGE_IMPROVEMENT_RULE = "v1_relevance_v2_tier_then_matched_unique"

# Pinned operationalization of the outcome_demotion bar's invariant
# ("zero demoted memories that were a later turn's explicitly-applied
# top-1"): a violation is a changed-turn's suppressed memory (the
# toggle-off winner the demotion kept out of the top slot) that shows
# up as a LATER replayable turn's production top-1 with an explicit
# non-auto `applied` use event for it within the attribution horizon
# (`audit.ATTRIBUTION_LOOKBACK_SECONDS`) either side of that later
# turn.
USAGE_DEMOTION_INVARIANT_RULE = "v1_later_top1_explicit_apply_within_600s"

_USAGE_V2_TIER = {"low": 0, "medium": 1, "high": 2}


def _judge_usage_change(
    on_v2: str,
    on_matched: int,
    off_v2: str,
    off_matched: int,
) -> str:
    """Apply `USAGE_IMPROVEMENT_RULE` to one changed top-1."""
    tier_delta = _USAGE_V2_TIER.get(on_v2, 0) - _USAGE_V2_TIER.get(off_v2, 0)
    if tier_delta > 0:
        return "improving"
    if tier_delta < 0:
        return "worsening"
    if on_matched > off_matched:
        return "improving"
    if on_matched < off_matched:
        return "worsening"
    return "neutral"


@dataclass
class UsageToggleChange:
    """One turn where a single-flag toggle changed the probe's top-1."""

    ts: str
    event_kind: str  # "turn_audited" | "prompt_recall"
    miss_labeled: bool
    on_top1_id: str
    on_relevance_v2: str
    on_matched_unique: int
    off_top1_id: str
    off_relevance_v2: str
    off_matched_unique: int
    query_unique: int
    judgment: str  # improving | worsening | neutral

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "event_kind": self.event_kind,
            "miss_labeled": self.miss_labeled,
            "on_top1_id": self.on_top1_id,
            "on_relevance_v2": self.on_relevance_v2,
            "on_matched_unique": self.on_matched_unique,
            "off_top1_id": self.off_top1_id,
            "off_relevance_v2": self.off_relevance_v2,
            "off_matched_unique": self.off_matched_unique,
            "query_unique": self.query_unique,
            "judgment": self.judgment,
        }


@dataclass
class UsageFlagReplay:
    """Replay rollup for one usage flag over the window."""

    flag: str
    active_turns: int
    changed_turns: int
    improving: int
    worsening: int
    neutral: int
    miss_labeled_worsening: int
    changes: list[UsageToggleChange] = field(default_factory=list)
    # outcome_demotion only; None on the other flags.
    invariant_rule: str | None = None
    invariant_violations: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "flag": self.flag,
            "active_turns": self.active_turns,
            "changed_turns": self.changed_turns,
            "improving": self.improving,
            "worsening": self.worsening,
            "neutral": self.neutral,
            "miss_labeled_worsening": self.miss_labeled_worsening,
            "changes": [c.to_dict() for c in self.changes],
        }
        if self.invariant_rule is not None:
            out["invariant_rule"] = self.invariant_rule
            out["invariant_violations"] = list(self.invariant_violations)
        return out


@dataclass
class UsageReplayReport:
    """Everything the usage-signal flip-bar read consumes, in one shape.

    Measurements only — the declared thresholds live in docs/ROADMAP.md
    and are deliberately NOT duplicated here, so the read compares one
    measured report against one declared entry and nothing in between
    can drift. `turns_without_capture` folds together pre-capture
    producers and no-live-signal turns (the event shape cannot split
    them — absence is absence); `first_capture_ts` bounds the ambiguity
    by naming when captures started appearing in this window.
    """

    generated_at: datetime
    window_seconds: int | None
    events_in_window: int
    replayable_turns: int
    turn_audited_turns: int
    prompt_recall_turns: int
    repeat_audits_skipped: int
    turns_without_capture: int
    first_capture_ts: str | None
    endorsed_distinct_in_window: int
    negative_distinct_in_window: int
    corroborated_memories: int
    corroborated_twice_memories: int
    improvement_rule: str = USAGE_IMPROVEMENT_RULE
    flags: list[UsageFlagReplay] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "events_in_window": self.events_in_window,
            "replayable_turns": self.replayable_turns,
            "turn_audited_turns": self.turn_audited_turns,
            "prompt_recall_turns": self.prompt_recall_turns,
            "repeat_audits_skipped": self.repeat_audits_skipped,
            "turns_without_capture": self.turns_without_capture,
            "first_capture_ts": self.first_capture_ts,
            "endorsed_distinct_in_window": self.endorsed_distinct_in_window,
            "negative_distinct_in_window": self.negative_distinct_in_window,
            "corroborated_memories": self.corroborated_memories,
            "corroborated_twice_memories": self.corroborated_twice_memories,
            "improvement_rule": self.improvement_rule,
            "flags": [f.to_dict() for f in self.flags],
        }


def compute_usage_replay(
    events: Iterable[dict[str, Any]],
    *,
    memories: Iterable[Memory] = (),
    since: timedelta | None = None,
    now: datetime | None = None,
) -> UsageReplayReport:
    """Aggregate the usage-toggle captures over the window.

    Walks non-repeat, miss-capable `turn_audited` events and
    `prompt_recall` events (a delivery IS a miss verdict, computed
    before the turn) that carry a well-formed `top_hits` payload —
    the same guards `_collect_replayable_audits` applies, extended to
    the delivery lane. The same single pass tallies the density
    preconditions the bars declare: distinct memories with explicit
    non-auto `applied` use events, and distinct memories with negative
    (`ignored` / `contradicted`) use events, both inside the window.
    `memories` feeds the corroboration-liveness counts (the persisted
    rollup is the signal `corroboration_boost` ranks on; the event log
    has nothing to say about it).
    """
    from .audit import ATTRIBUTION_LOOKBACK_SECONDS
    from .events import _event_id_list
    from .search import USAGE_FLAG_NAMES

    now = now or datetime.now(timezone.utc)
    cutoff: datetime | None = (now - since) if since is not None else None

    events_in_window = 0
    turn_audited_turns = 0
    prompt_recall_turns = 0
    repeats_skipped = 0
    turns_without_capture = 0
    first_capture_ts: str | None = None

    # One row per replayable turn, in stream order:
    # (ts, kind, miss_labeled, top1_dict, usage_active, usage_toggles)
    rows: list[
        tuple[datetime, str, bool, dict[str, Any], list[str], dict[str, Any]]
    ] = []
    # Explicit non-auto applied use events, for the invariant check and
    # the endorsement density: (ts, ids).
    explicit_applies: list[tuple[datetime, list[str]]] = []
    negative_ids: set[str] = set()
    endorsed_ids: set[str] = set()

    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = _parse_ts(ev.get("ts"))
        in_window = cutoff is None or (ts is not None and ts >= cutoff)
        if in_window:
            events_in_window += 1
        if not in_window or ts is None:
            continue
        kind = ev.get("kind")
        if kind == "use":
            outcome = ev.get("outcome")
            ids = _event_id_list(ev.get("ids") or ev.get("memory_ids"))
            if outcome == "applied" and ev.get("auto") is not True:
                explicit_applies.append((ts, ids))
                endorsed_ids.update(ids)
            elif outcome in ("ignored", "contradicted"):
                negative_ids.update(ids)
            continue
        if kind not in ("turn_audited", "prompt_recall"):
            continue
        if kind == "turn_audited":
            if ev.get("repeat"):
                repeats_skipped += 1
                continue
            if ev.get("verdict") == "no_signal":
                continue
        top_hits = ev.get("top_hits")
        if (
            not isinstance(top_hits, list)
            or not top_hits
            or not isinstance(top_hits[0], dict)
        ):
            continue
        if kind == "turn_audited":
            turn_audited_turns += 1
            miss_labeled = ev.get("verdict") == "miss"
        else:
            prompt_recall_turns += 1
            miss_labeled = True
        usage_active_raw = ev.get("usage_active")
        usage_active = (
            [f for f in usage_active_raw if isinstance(f, str)]
            if isinstance(usage_active_raw, list)
            else []
        )
        usage_toggles_raw = ev.get("usage_toggles")
        usage_toggles = (
            usage_toggles_raw if isinstance(usage_toggles_raw, dict) else {}
        )
        if usage_active or usage_toggles:
            ts_str = str(ev.get("ts"))
            if first_capture_ts is None or ts_str < first_capture_ts:
                first_capture_ts = ts_str
        else:
            turns_without_capture += 1
        rows.append(
            (ts, str(kind), miss_labeled, top_hits[0], usage_active, usage_toggles)
        )

    flags: list[UsageFlagReplay] = []
    for flag in USAGE_FLAG_NAMES:
        active_turns = 0
        changes: list[UsageToggleChange] = []
        suppressed: list[tuple[datetime, str]] = []
        for ts, kind, miss_labeled, top1, usage_active, usage_toggles in rows:
            if flag in usage_active:
                active_turns += 1
            toggle = usage_toggles.get(flag)
            if not isinstance(toggle, dict):
                continue
            off = toggle.get("top1")
            if not isinstance(off, dict):
                continue
            on_v2 = str(top1.get("relevance_v2") or "low")
            on_matched = int(top1.get("matched_unique") or 0)
            off_v2 = str(off.get("relevance_v2") or "low")
            off_matched = int(off.get("matched_unique") or 0)
            judgment = _judge_usage_change(on_v2, on_matched, off_v2, off_matched)
            changes.append(
                UsageToggleChange(
                    ts=isoformat_utc(ts),
                    event_kind=kind,
                    miss_labeled=miss_labeled,
                    on_top1_id=str(top1.get("id") or ""),
                    on_relevance_v2=on_v2,
                    on_matched_unique=on_matched,
                    off_top1_id=str(off.get("id") or ""),
                    off_relevance_v2=off_v2,
                    off_matched_unique=off_matched,
                    query_unique=int(
                        off.get("query_unique") or top1.get("query_unique") or 0
                    ),
                    judgment=judgment,
                )
            )
            if flag == "outcome_demotion":
                off_id = str(off.get("id") or "")
                if off_id:
                    suppressed.append((ts, off_id))

        invariant_rule: str | None = None
        violations: list[dict[str, str]] = []
        if flag == "outcome_demotion":
            invariant_rule = USAGE_DEMOTION_INVARIANT_RULE
            horizon = float(ATTRIBUTION_LOOKBACK_SECONDS)
            for supp_ts, supp_id in suppressed:
                for ts, _kind, _miss, top1, _active, _toggles in rows:
                    if ts <= supp_ts or str(top1.get("id") or "") != supp_id:
                        continue
                    applied_nearby = any(
                        supp_id in ids
                        and abs((apply_ts - ts).total_seconds()) <= horizon
                        for apply_ts, ids in explicit_applies
                    )
                    if applied_nearby:
                        violations.append(
                            {
                                "memory_id": supp_id,
                                "suppressed_at": isoformat_utc(supp_ts),
                                "applied_top1_at": isoformat_utc(ts),
                            }
                        )
                        break

        improving = sum(1 for c in changes if c.judgment == "improving")
        worsening = sum(1 for c in changes if c.judgment == "worsening")
        neutral = sum(1 for c in changes if c.judgment == "neutral")
        flags.append(
            UsageFlagReplay(
                flag=flag,
                active_turns=active_turns,
                changed_turns=len(changes),
                improving=improving,
                worsening=worsening,
                neutral=neutral,
                miss_labeled_worsening=sum(
                    1
                    for c in changes
                    if c.judgment == "worsening" and c.miss_labeled
                ),
                changes=changes,
                invariant_rule=invariant_rule,
                invariant_violations=violations,
            )
        )

    corroborated = 0
    corroborated_twice = 0
    for memory in memories:
        count = getattr(memory, "corroborations", 0) or 0
        if count >= 1:
            corroborated += 1
        if count >= 2:
            corroborated_twice += 1

    return UsageReplayReport(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        events_in_window=events_in_window,
        replayable_turns=len(rows),
        turn_audited_turns=turn_audited_turns,
        prompt_recall_turns=prompt_recall_turns,
        repeat_audits_skipped=repeats_skipped,
        turns_without_capture=turns_without_capture,
        first_capture_ts=first_capture_ts,
        endorsed_distinct_in_window=len(endorsed_ids),
        negative_distinct_in_window=len(negative_ids),
        corroborated_memories=corroborated,
        corroborated_twice_memories=corroborated_twice,
        flags=flags,
    )


def render_usage_replay_text(report: UsageReplayReport) -> str:
    """Plain-text rendering, mirroring the widening surfaces' shape."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --usage-replay — last {window}")
    lines.append("─" * 60)
    lines.append(f"Events scanned              {report.events_in_window:>5d}")
    lines.append(f"Replayable turns            {report.replayable_turns:>5d}")
    lines.append(
        f"  ({report.turn_audited_turns} audited, "
        f"{report.prompt_recall_turns} delivered recalls)"
    )
    if report.repeat_audits_skipped:
        lines.append(
            f"  (skipped {report.repeat_audits_skipped} repeat audits — "
            "multi-stop re-probes of the same message)"
        )
    if report.turns_without_capture:
        lines.append(
            f"  ({report.turns_without_capture} turns carry no usage capture — "
            "pre-capture producer or no live signal)"
        )
    if report.first_capture_ts:
        lines.append(f"  (first capture in window: {report.first_capture_ts})")
    lines.append("")
    lines.append("Density preconditions (this window)")
    lines.append(
        f"  explicit-endorsed distinct memories   "
        f"{report.endorsed_distinct_in_window:>5d}"
    )
    lines.append(
        f"  negative-outcome distinct memories    "
        f"{report.negative_distinct_in_window:>5d}"
    )
    lines.append(
        f"  corroborated memories (≥1 / ≥2)       "
        f"{report.corroborated_memories:>5d} / {report.corroborated_twice_memories}"
    )
    lines.append("")
    lines.append(f"Judgment rule: {report.improvement_rule}")
    for row in report.flags:
        lines.append("")
        lines.append(f"{row.flag}")
        lines.append(f"  turns with live signal      {row.active_turns:>5d}")
        lines.append(f"  changed top-1s              {row.changed_turns:>5d}")
        if row.changed_turns:
            lines.append(
                f"    improving / worsening / neutral   "
                f"{row.improving} / {row.worsening} / {row.neutral}"
            )
            lines.append(
                f"    miss-labeled worsening            {row.miss_labeled_worsening}"
            )
            for change in row.changes[-10:]:
                lines.append(
                    f"    {change.ts}  {change.judgment:<9s} "
                    f"on={change.on_top1_id[:10]}…({change.on_relevance_v2}) "
                    f"off={change.off_top1_id[:10]}…({change.off_relevance_v2})"
                )
            if len(row.changes) > 10:
                lines.append(f"    … and {len(row.changes) - 10} earlier changes")
        if row.invariant_rule is not None:
            lines.append(f"  invariant ({row.invariant_rule})")
            if row.invariant_violations:
                lines.append(
                    f"    VIOLATIONS: {len(row.invariant_violations)}"
                )
                for v in row.invariant_violations:
                    lines.append(
                        f"      {v['memory_id'][:10]}… suppressed "
                        f"{v['suppressed_at']} → applied top-1 "
                        f"{v['applied_top1_at']}"
                    )
            else:
                lines.append("    violations: 0")
    lines.append("")
    lines.append(
        "Read these numbers against the declared flip bars in "
        "docs/ROADMAP.md"
    )
    lines.append(
        "(the usage-signal flags entry). This surface measures; it "
        "never flips."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tool-usage rollup — per-MCP-tool call counts from the event log
# ---------------------------------------------------------------------------

# Map from event `kind` to the MCP tool that emits it. Used by
# `compute_tool_usage` so the rollup uses tool names rather than the
# wire-format event kinds the recorder writes. The exact set is the
# 22-tool memory_* + 5-tool episode_* surface listed in `server.py`'s
# module docstring, minus the ones without a dedicated event of their
# own, which appear in `TOOLS_WITHOUT_TELEMETRY` instead.
#
# Why an explicit map rather than counting raw `kind` values: some
# event kinds (`search_miss`, `pending_expired`) are side-effects of
# other tools, not tool calls in their own right. Counting raw kinds
# would double-count `memory_audit_turn` invocations that happen to
# detect a miss and under-count `memory_write` invocations that
# stage a pending confirmation (the `write` event has
# `status="pending"` but it's still one tool call). The map collapses
# both axes correctly.
_TOOL_EVENT_KIND_TO_TOOL: dict[str, str] = {
    "search": "memory_search",
    "show": "memory_show",
    "list": "memory_list",
    "scope_overview": "memory_scope_overview",
    "write": "memory_write",
    "write_confirm": "memory_write_confirm",
    "write_cancel": "memory_write_cancel",
    "update": "memory_update",
    "remove": "memory_remove",
    "restore": "memory_restore",
    "list_tombstones": "memory_list_tombstones",
    "verify": "memory_verify",
    "use": "memory_record_use",
    "rename_scope": "memory_rename_scope",
    "scope_disable": "memory_scope_disable",
    "scope_enable": "memory_scope_enable",
    "turn_audited": "memory_audit_turn",
    "miss_ack": "memory_acknowledge_miss",
    "curate": "memory_curate",
    "memory_proposals": "memory_proposals",
    "episode_write": "episode_write",
    "episode_handoff": "episode_handoff",
    "episode_search": "episode_search",
    "episode_promote": "episode_promote",
    # Corpus-inference pair (3.28.0). Both tools record only their
    # ACTING modes (scan / verdict / promote / dismiss) — a pure listing
    # call leaves no event, so their rows undercount reads. Same class
    # as `curate`, which records only on apply: the mapped kinds count
    # the calls that changed something, which is the half curation
    # telemetry cares about.
    "conflict_scan": "memory_conflicts",
    "conflict_verdict": "memory_conflicts",
    "episode_pattern": "episode_patterns",
}

# Tools that don't emit a dedicated event of their own; the rollup
# surfaces them with a 0 count and a note rather than silently dropping
# them, so a reader inspecting the report can tell "this tool is not
# counted" apart from "this tool was never called."
TOOLS_WITHOUT_TELEMETRY: tuple[str, ...] = ("memory_health",)

# Event kinds the recorder emits as side-effects of other tools, NOT as
# tool calls in their own right. ``search_miss`` is a sub-event of
# ``turn_audited`` (the audit detected a high-relevance hit the model
# would have missed); ``pending_expired`` fires when the TTL on a
# ``memory_write`` pending token elapses; ``silent_miss_cutoff`` is an
# additive admin event written by ``bettermemory consolidate
# --acknowledge-misses-before`` to invalidate a batch of pre-fix miss
# telemetry. None of these belong in the tool-usage rollup — they
# would inflate the parent tool's count (or, for the CLI event, count
# an admin operation as a tool invocation).
#
# The parity test in ``tests/test_eval.py`` asserts that every kind
# recorded anywhere in ``src/`` appears in either
# ``_TOOL_EVENT_KIND_TO_TOOL`` or this set, and that the two are
# mutually exclusive. Adding a new event kind without updating one of
# them is the bug class this guards against.
#
# ``proposals_enqueued`` is the Stop hook's write-reflex capture event —
# a side-effect of turn-end, not a tool call (the model never invokes it;
# accepting/dismissing the resulting proposals goes through the
# ``memory_proposals`` tool, which IS mapped above). The hook's
# ``auto_consolidate`` event is recorded via a module constant, not a
# string literal, so the AST parity scan never sees it — it is
# deliberately omitted here rather than tripping the "stale entry" half
# of the parity assertion.
#
# ``doctor_fix`` is `bettermemory doctor --fix`'s per-applied-fix audit
# record — an admin CLI operation like ``silent_miss_cutoff``, never a
# tool invocation, so counting it in the rollup would invent a tool.
#
# ``use_token_expired`` is the use-token counterpart of
# ``pending_expired``: one batched event per set of retrieval tokens
# that hit the 30-minute wall-clock eviction with nothing having
# settled them (no Stop-hook attribution, no explicit
# ``memory_record_use``, no in-process auto-commit). It is a
# consequence of ``memory_search`` / ``memory_show`` going unsettled,
# not a call — counting it would inflate whichever tool it was
# attributed to, and it has no tool of its own to be attributed to.
#
# ``prompt_recall`` is the UserPromptSubmit hook's delivery record
# (`hook.run_prompt_recall`): the probe's miss verdict computed before
# the turn and injected as context instead of flagged after it. Not a
# tool invocation — no model call happened — so it stays off the usage
# rollup; it is the delivery lane's OWN counter, read beside
# ``search_miss`` when tracing what the recall path did.
_KNOWN_SIDE_EFFECT_KINDS: frozenset[str] = frozenset(
    {
        "search_miss",
        "pending_expired",
        "silent_miss_cutoff",
        "proposals_enqueued",
        "doctor_fix",
        "use_token_expired",
        "prompt_recall",
    }
)

# The subset of the roster above recorded INSIDE a live client session,
# under that client's own session id. Verified at the call sites:
# ``search_miss`` / ``proposals_enqueued`` come off the Stop hook's
# recorder (hook.py) — the same recorder that writes that session's
# ``turn_audited`` rows — ``prompt_recall`` comes off the
# UserPromptSubmit hook's recorder (hook.py), stamped with the SAME
# Claude Code transcript session id the Stop hook's rows for that
# conversation carry — and ``pending_expired`` and
# ``use_token_expired`` are both drained handler-side through the live
# session's recorder (handlers/_shared.py), at the entry of the very
# tool call that noticed the eviction.
#
# MEMBERSHIP HERE IS NOT OPTIONAL AND NOT MECHANICALLY CHECKED. The
# roster below is DERIVED as ``_KNOWN − _IN_SESSION``, which makes
# ``ADMIN | IN_SESSION == KNOWN`` a tautology: a kind added to
# ``_KNOWN_SIDE_EFFECT_KINDS`` alone lands in the admin roster, and
# every partition assertion in the suite still passes. The visible
# damage is downstream — ``is_admin_recorded_event`` starts returning
# True, ``doctor._check_audit_turn_cadence`` drops the event AND its
# whole session from the census, and eval's tally treats a real client
# session as never having existed. The only guard is a hand-written,
# per-kind one — see
# ``tests/test_doctor.py::test_use_token_expired_is_classified_in_session``
# for the shape (membership assertion plus a behavioural census half).
# Write one alongside every new entry here.
_IN_SESSION_SIDE_EFFECT_KINDS: frozenset[str] = frozenset(
    {
        "search_miss",
        "pending_expired",
        "proposals_enqueued",
        "use_token_expired",
        "prompt_recall",
    }
)

# Event kinds recorded by an admin/CLI surface OUTSIDE any client
# session, under a fresh throwaway session id. Derived as the
# complement of the in-session subset rather than hand-listed, so a new
# entry on the roster above lands here automatically instead of
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
# ``bettermemory consolidate --acknowledge-debt`` is the live instance.
# It writes ``kind="use", outcome="applied", auto=False`` rows — the
# exact shape a model's ``memory_record_use`` call produces — under a
# fresh throwaway ``SessionState()`` id, so excluding by kind would
# have to exclude ``use`` wholesale and blind the tally to every real
# client session. What separates the two is ATTRIBUTION: every admin
# CLI writer stamps ``attribution="cli_<operation>"``
# (``cli_acknowledge_debt`` on the use rows,
# ``cli_acknowledge_misses`` on the cutoff marker), while every
# in-session producer stamps ``"model"``, ``"hook"``, or ``"auto"``
# (see the module docstring's attribution tier). A prefix rule rather
# than a hand-listed roster, deliberately: a new admin CLI operation
# lands on the correct side by construction instead of quietly
# inflating the count until someone notices.
#
# Scope of the exclusion is the SESSION TALLY ONLY. The acknowledge-debt
# rows are genuine endorsements — that is the whole point of the
# subcommand — so they keep counting toward ``applied_total`` and the
# endorsement rate. What they must not do is publish a session that
# never had a client attached to it.
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
class ToolUsageRow:
    """One row of the tool-usage rollup. ``count`` is the number of
    invocations attributed to this tool in the window; ``share`` is
    its fraction of the total non-zero rollup (``None`` when the
    rollup is entirely empty)."""

    tool: str
    count: int
    share: float | None
    has_telemetry: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "count": self.count,
            "share": self.share,
            "has_telemetry": self.has_telemetry,
        }


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


def render_tool_usage_text(report: ToolUsageReport) -> str:
    """Plain-text rendering for the CLI. One row per tool, sorted by
    descending call count; untelemetered tools surface with a footer
    so the reader sees "memory_health is not counted" rather than
    "memory_health was never called."""
    lines: list[str] = []
    window = (
        "all time"
        if report.window_seconds is None
        else _humanize_seconds(report.window_seconds)
    )
    lines.append(f"bettermemory eval --tool-usage — last {window}")
    lines.append("─" * 60)
    # Window-scoped, matching the header — see `EvalReport.events_in_window`.
    lines.append(f"Events scanned     {report.events_in_window:>5d}")
    lines.append(f"Tool calls         {report.total_tool_calls:>5d}")
    lines.append("")
    lines.append(f"{'tool':<32s} {'count':>7s}  share")
    for row in report.rows:
        if not row.has_telemetry:
            lines.append(f"  {row.tool:<30s} {row.count:>7d}  —  (no telemetry)")
            continue
        if row.share is None:
            share_str = "—"
        else:
            share_str = f"{row.share * 100:5.1f}%"
        bar = _bar(row.share or 0.0)
        lines.append(f"  {row.tool:<30s} {row.count:>7d}  {share_str}  {bar}")
    if report.unmapped_event_kinds:
        lines.append("")
        lines.append(
            "Unmapped event kinds (recorder emitted something the tool-usage "
            "map didn't know about — likely a new tool that needs to be "
            "added to _TOOL_EVENT_KIND_TO_TOOL, or a new side-effect kind "
            "that should be added to the side_effect_kinds set):"
        )
        for kind, count in sorted(
            report.unmapped_event_kinds.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            lines.append(f"  {kind:<30s} {count:>7d}")
    return "\n".join(lines) + "\n"


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


def _package_version() -> str:
    """Installed package version for the methodology footer.

    Same fallback contract as ``bettermemory.__version__`` (kept local
    so the eval module never imports the package root — that import
    direction is what caused the historical load-time cycle in the CLI
    package).
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("bettermemory")
    except PackageNotFoundError:  # pragma: no cover — dev checkouts only
        return "0+unknown"


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
            # session id — by kind (doctor --fix, the cutoff marker) or
            # by `cli_*` attribution (acknowledge-debt's `use` rows,
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


def _md_escape_cell(text: str) -> str:
    """Escape the one character that breaks a hand-rolled markdown
    table cell. Applied to the only non-static string the renderer
    emits (model names, which come off logged `client_model` stamps —
    the log is plaintext and hand-editable, so a stray ``|`` must not
    be able to shift columns)."""
    return text.replace("|", "\\|")


def _md_rate_cell(rate: RateCI, *, bold: bool = False) -> str:
    """One rate-trio table cell: ``k/n = 0.07 [0.05, 0.09]``.

    Mirrors docs/eval-results.md's published shape. ``n/a`` keeps the
    raw counts visible so the reader sees *why* the rate is undefined.
    """
    if rate.rate is None or rate.lower is None or rate.upper is None:
        return f"n/a (k={rate.numerator}, n={rate.denominator})"
    value = f"{rate.rate:0.2f}"
    if bold:
        value = f"**{value}**"
    return (
        f"{rate.numerator}/{rate.denominator} = {value} "
        f"[{rate.lower:0.2f}, {rate.upper:0.2f}]"
    )


def _count_phrase(n: int, singular: str, plural: str) -> str:
    """``1 turn audited`` / ``3 turns audited`` — number-agreement for
    the report's counted nouns. The plural is explicit at every call
    site (the phrases pluralise in different positions: "turns
    audited" vs "repeat audits deduped"), so a lazy ``+s`` rule can't
    quietly publish bad grammar."""
    return f"{n} {singular if n == 1 else plural}"


def _md_denominator_note(label: str, report: EvalReport) -> str:
    """One-line counts context under the rate table — window shape a
    reader needs to judge the CIs. Counts only.

    Every figure on the line is window-scoped, including the leading
    event count: it reads `events_in_window`, NOT the all-time
    `total_events_scanned` (which counts the whole log because the
    invalidation markers resolve ahead of the window filter). Mixing
    the two published an all-time figure under a `last Nd:` label.
    """
    parts = [
        _count_phrase(report.events_in_window, "event scanned", "events scanned"),
        _count_phrase(
            report.retrieval_occurrences,
            "retrieval occurrence",
            "retrieval occurrences",
        ),
        _count_phrase(report.applied_total, "applied use event", "applied use events"),
        _count_phrase(report.turns_audited, "turn audited", "turns audited"),
    ]
    if report.turns_no_signal:
        parts.append(
            _count_phrase(
                report.turns_no_signal,
                "no-signal turn excluded",
                "no-signal turns excluded",
            )
        )
    if report.repeat_audits:
        parts.append(
            _count_phrase(
                report.repeat_audits, "repeat audit deduped", "repeat audits deduped"
            )
        )
    return f"- {label}: " + " · ".join(parts) + "."


def render_report_markdown(doc: ReportDocument) -> str:
    """Render the composed document as publishable markdown.

    Content contract (the canary test pins it): rates, counts, CIs,
    model names, and static registry names only. Sections, top to
    bottom: header, rate trio (window vs all-time), reading guide,
    per-model table, threshold sweep, tool-usage top 10, methodology
    footer.
    """
    single_window = doc.window_seconds is None
    window_label = (
        "all time"
        if doc.window_seconds is None
        else f"last {_humanize_seconds(doc.window_seconds)}"
    )
    lines: list[str] = []

    # 1. Title + header — aggregate store shape, counts only.
    lines.append("# bettermemory eval report")
    lines.append("")
    if single_window:
        lines.append(f"Generated {doc.generated_at.isoformat()} · window: all time")
    else:
        lines.append(
            f"Generated {doc.generated_at.isoformat()} · "
            f"window: {window_label} vs all time"
        )
    lines.append("")
    active_noun = "active memory" if doc.active_memory_count == 1 else "active memories"
    event_noun = "logged event" if doc.total_events == 1 else "logged events"
    session_noun = (
        "distinct session" if doc.distinct_session_count == 1 else "distinct sessions"
    )
    lines.append(
        f"Store shape: **{doc.active_memory_count}** {active_noun} · "
        f"**{doc.total_events}** {event_noun} · "
        f"**{doc.distinct_session_count}** {session_noun}. "
        "Counts only — memory bodies, queries, scopes, paths, and session "
        "ids never appear in this report."
    )
    lines.append("")

    # 2. Rate trio, window vs all-time columns.
    lines.append("## Rates")
    lines.append("")
    if single_window:
        lines.append("| rate | all time |")
        lines.append("|---|---|")
        for name, rate in (
            ("memory_helped_rate", doc.alltime_eval.memory_helped_rate),
            ("endorsement_rate", doc.alltime_eval.endorsement_rate),
            ("silent_miss_rate", doc.alltime_eval.silent_miss_rate),
        ):
            lines.append(f"| `{name}` | {_md_rate_cell(rate, bold=True)} |")
    else:
        lines.append(f"| rate | {window_label} | all time |")
        lines.append("|---|---|---|")
        for name, window_rate, alltime_rate in (
            (
                "memory_helped_rate",
                doc.window_eval.memory_helped_rate,
                doc.alltime_eval.memory_helped_rate,
            ),
            (
                "endorsement_rate",
                doc.window_eval.endorsement_rate,
                doc.alltime_eval.endorsement_rate,
            ),
            (
                "silent_miss_rate",
                doc.window_eval.silent_miss_rate,
                doc.alltime_eval.silent_miss_rate,
            ),
        ):
            lines.append(
                f"| `{name}` | {_md_rate_cell(window_rate, bold=True)} | "
                f"{_md_rate_cell(alltime_rate)} |"
            )
    lines.append("")
    lines.append("Wilson 95% confidence intervals in brackets.")
    lines.append("")
    if not single_window:
        lines.append(_md_denominator_note(window_label, doc.window_eval))
    lines.append(_md_denominator_note("all time", doc.alltime_eval))
    torn = (
        doc.window_eval.memory_helped_rate.torn_read
        or doc.window_eval.endorsement_rate.torn_read
        or doc.window_eval.silent_miss_rate.torn_read
        or doc.alltime_eval.memory_helped_rate.torn_read
        or doc.alltime_eval.endorsement_rate.torn_read
        or doc.alltime_eval.silent_miss_rate.torn_read
    )
    if torn:
        lines.append("")
        lines.append(
            "> Note: a numerator exceeded its denominator (rate clamped to "
            "1.0). Usually a windowing artifact under a `--since` window — "
            "a use event lands in-window while its retrieval aged out — or, "
            "less often, a log read mid-rotation."
        )
    lines.append("")

    # 3. Reading guide — static prose adapted from docs/eval-results.md.
    lines.append("## Reading these numbers honestly")
    lines.append("")
    lines.append(
        "- `memory_helped_rate` is a deliberate floor: the numerator counts "
        "only explicit, claim-excerpt-backed endorsements, while the "
        "denominator counts every retrieval occurrence. Retrievals that "
        "quietly helped don't count."
    )
    if not single_window:
        lines.append(
            "- The window column vs the all-time column is the story: the "
            "attestation tooling matures over a store's history, so early "
            "events couldn't carry signals that now exist. Read the trend, "
            "not either column alone."
        )
    lines.append(
        "- A low or zero `silent_miss_rate` is a claim about the loosest "
        "evaluable rule (`v1_top1_high`), not about misses in general — "
        "strictly looser rules can't be replayed from the log alone. The "
        "threshold-sweep table below replays the flagged misses against "
        "stricter rules."
    )
    lines.append(
        "- n=1: this measures one deployment's store, workload, and "
        "retrieval discipline. Treat it as telemetry, not a benchmark."
    )
    lines.append("")

    # 4. Per-model audit telemetry.
    lines.append("## Per-model audit telemetry (all time)")
    lines.append("")
    by_model = doc.alltime_eval.by_model
    if by_model:
        lines.append("| model | audited | no_signal | misses |")
        lines.append("|---|---|---|---|")
        for model in sorted(by_model):
            counts = by_model[model]
            lines.append(
                f"| {_md_escape_cell(model)} | {counts.get('audited', 0)} | "
                f"{counts.get('no_signal', 0)} | {counts.get('misses', 0)} |"
            )
    else:
        lines.append(
            "No per-model telemetry in the log yet (the `client_model` "
            "stamp arrived with 3.14 events)."
        )
    lines.append("")

    # 5. Threshold-sweep counterfactual. Rule names/descriptions come
    # from the rows compute_threshold_sweep built off the static
    # registry — never from log-derived strings.
    lines.append("## Threshold sweep (counterfactual, all time)")
    lines.append("")
    if doc.sweep.replayable_misses == 0:
        lines.append(
            "No replayable misses in the log — the counterfactual needs "
            "`search_miss` events carrying `top_hits` (2.6.4+)."
        )
    else:
        lines.append("| rule | would flag | Δ v1 | % of v1 |")
        lines.append("|---|---|---|---|")
        for row in doc.sweep.rows:
            delta = f"{row.delta_from_v1:+d}" if row.rule != "v1_top1_high" else "—"
            pct = f"{row.delta_pct * 100:.1f}%" if row.delta_pct is not None else "—"
            lines.append(f"| `{row.rule}` | {row.would_flag} | {delta} | {pct} |")
        lines.append("")
        replayable = _count_phrase(
            doc.sweep.replayable_misses, "replayable miss", "replayable misses"
        )
        lines.append(
            f"{replayable}. Stricter rules replay over misses v1 already "
            'flagged, so this table answers "is v1 over-firing?" — not '
            '"what does v1 miss?".'
        )
        if doc.sweep.skipped_legacy_event_count:
            skipped = _count_phrase(
                doc.sweep.skipped_legacy_event_count, "legacy event", "legacy events"
            )
            lines.append(f"Skipped {skipped} carrying no replayable relevance label.")
    lines.append("")

    # 6. Tool-usage top 10.
    lines.append("## Tool usage (top 10, all time)")
    lines.append("")
    lines.append("| tool | calls | share |")
    lines.append("|---|---|---|")
    # The slice is top-10 by count, PLUS every untelemetered row pinned
    # in regardless of rank: those rows exist to say "not counted" (see
    # TOOLS_WITHOUT_TELEMETRY), and at 27 tools a structurally-zero row
    # can never crack a top-10 on count — sliced out, the note the row
    # carries would silently vanish from the published artifact.
    published = list(doc.tool_usage.rows[:10])
    published.extend(row for row in doc.tool_usage.rows[10:] if not row.has_telemetry)
    for usage_row in published:
        if not usage_row.has_telemetry:
            # Mirrors render_tool_usage_text: a tool that emits no
            # dedicated event of its own publishes as "not counted",
            # never as a bare zero a reader would take for "nobody ever
            # called it".
            lines.append(f"| `{usage_row.tool}` (no telemetry) | — | — |")
            continue
        share = f"{usage_row.share * 100:.1f}%" if usage_row.share is not None else "—"
        lines.append(f"| `{usage_row.tool}` | {usage_row.count} | {share} |")
    lines.append("")
    lines.append(
        f"{_count_phrase(doc.tool_usage.total_tool_calls, 'tool call', 'tool calls')} "
        f"total across {len(doc.tool_usage.rows)} known tools."
    )
    lines.append("")

    # 7. Methodology footer.
    lines.append("---")
    lines.append("")
    lines.append(
        f"Generated by `bettermemory eval --report` v{doc.version} "
        "(metric definitions: docs/eval.md)."
    )
    return "\n".join(lines) + "\n"


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
]
