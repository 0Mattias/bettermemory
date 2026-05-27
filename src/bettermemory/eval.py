"""Per-deployment memory effectiveness eval.

Three rates the existing event log makes computable today:

- ``memory_helped_rate`` — fraction of retrieval occurrences (search-hit
  positions + memory_show calls) that produced an attested
  ``memory_record_use(applied)`` event carrying a non-empty
  ``claim_excerpt``. Two attestation tiers feed in: model-explicit
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
  ``turn_audited`` events. The opt-in-retrieval contract's blind spot:
  turns where the model should have searched but didn't.

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
``auto`` is true. The eval rollups branch on ``auto`` directly so the
back-compat fall-through stays implicit; consumers wanting to split
model-explicit from hook-attributed reach into the raw events.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .models import Memory, first_summary_line
from .time_utils import parse_event_ts


# ---------------------------------------------------------------------------
# Public defaults
# ---------------------------------------------------------------------------

# Default ``--since`` window. 30 days mirrors the default
# ``verification_stale_days`` so the eval window and the freshness
# threshold tell a consistent story by default. Override at the call
# site.
DEFAULT_SINCE_SPEC = "30d"

# Default floor for endorsement-debt row inclusion. Shared with
# ``health._COLD_ENDORSEMENT_MIN_RETRIEVALS`` — duplicating the literal
# keeps the module dependency-light (we don't reach into health's
# privates), and the value is conservative enough that drift between
# the two would be inert in practice.
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
    # True when `numerator > denominator` — a torn read scenario where
    # the event log was read mid-rotation and ordering anomalies leaked
    # through. The Wilson helper clamps `k = n` in this case so the
    # interval stays well-defined, but the audit consumer needs the flag
    # so it knows "your numbers may be wrong" rather than silently
    # trusting a 1.0 rate.
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
class EndorsementDebtRow:
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

    retrieval_occurrences: int  # denominator for memory_helped_rate
    explicit_endorsements_with_excerpt: int  # numerator
    applied_total: int  # denominator for endorsement_rate
    applied_explicit: int  # numerator
    turns_audited: int  # denominator for silent_miss_rate
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

    endorsement_debt_rows: list[EndorsementDebtRow] = field(default_factory=list)
    endorsement_debt_total: int = 0
    endorsement_min_retrievals: int = DEFAULT_ENDORSEMENT_MIN_RETRIEVALS

    silent_miss_recent: list[SilentMissCandidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "scope_filter": self.scope_filter,
            "threshold_rule": self.threshold_rule,
            "total_events_scanned": self.total_events_scanned,
            "counts": {
                "retrieval_occurrences": self.retrieval_occurrences,
                "explicit_endorsements_with_excerpt": self.explicit_endorsements_with_excerpt,
                "applied_total": self.applied_total,
                "applied_explicit": self.applied_explicit,
                "turns_audited": self.turns_audited,
                "silent_misses": self.silent_misses,
            },
            "memory_helped_rate": self.memory_helped_rate.to_dict(),
            "endorsement_rate": self.endorsement_rate.to_dict(),
            "silent_miss_rate": self.silent_miss_rate.to_dict(),
            "cold_endorsement_memories": {
                "min_retrievals": self.endorsement_min_retrievals,
                "total": self.endorsement_debt_total,
                "rows": [r.to_dict() for r in self.endorsement_debt_rows],
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
    return timedelta(seconds=value * _SINCE_UNIT_TO_SECONDS[unit])


def compute_eval(
    memories: Iterable[Memory],
    events: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    since: timedelta | None = None,
    scope: str | None = None,
    endorsement_min_retrievals: int = DEFAULT_ENDORSEMENT_MIN_RETRIEVALS,
    silent_miss_limit: int = DEFAULT_SILENT_MISS_LIMIT,
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

    A memory that's been tombstoned (no longer in ``memories``) still
    counts toward retrieval / use occurrences — we attribute via id, not
    via live status — but it cannot participate in endorsement-debt
    rows because we need its body/scopes for display.
    """
    now = now or datetime.now(timezone.utc)
    cutoff: datetime | None = (now - since) if since is not None else None

    # Live store: id → memory, for endorsement-debt rows + scope filter.
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
    turns_audited = 0
    silent_misses = 0
    total_events_scanned = 0

    # Per-memory counts for endorsement-debt rollup.
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

    for ev in events:
        if not isinstance(ev, dict):
            continue
        total_events_scanned += 1

        if cutoff is not None:
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts < cutoff:
                continue

        kind = ev.get("kind")
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
            # Strict identity comparison matches health.py:736 — only
            # the literal `True` the Recorder stamps on auto-committed
            # use events qualifies as "auto". A stray truthy value
            # (legacy `auto=1`, `auto="true"`) reads as explicit, same
            # as missing/None, so we never silently relabel borderline
            # data as auto.
            is_auto = ev.get("auto") is True
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
                    explicit_applied_count[mid] = explicit_applied_count.get(mid, 0) + 1
                    if isinstance(excerpt, str) and excerpt.strip():
                        explicit_endorsements_with_excerpt += 1
        elif kind == "turn_audited":
            turns_audited += 1
            rule = ev.get("threshold_rule")
            if isinstance(rule, str) and rule:
                threshold_rule = rule
        elif kind == "search_miss":
            silent_misses += 1
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

    # Endorsement-debt rows: retrieval_count >= floor AND
    # explicit_applied_count == 0. Ambient memories are excluded — same
    # rationale as health's cold_endorsement_memories bucket (their
    # value is implicit; an explicit use event is structurally rare).
    floor = max(1, int(endorsement_min_retrievals))
    debt_rows: list[EndorsementDebtRow] = []
    debt_total = 0
    for mid, rcount in retrieval_count.items():
        if rcount < floor:
            continue
        if explicit_applied_count.get(mid, 0) > 0:
            continue
        mem = by_id.get(mid)
        if mem is None:
            continue
        if mem.category == "ambient":
            continue
        debt_total += 1
        debt_rows.append(
            EndorsementDebtRow(
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
    debt_rows.sort(key=lambda r: (-r.retrieval_count, r.id))

    report = EvalReport(
        generated_at=now,
        window_seconds=int(since.total_seconds()) if since is not None else None,
        scope_filter=scope,
        threshold_rule=threshold_rule,
        total_events_scanned=total_events_scanned,
        retrieval_occurrences=retrieval_occurrences,
        explicit_endorsements_with_excerpt=explicit_endorsements_with_excerpt,
        applied_total=applied_total,
        applied_explicit=applied_explicit,
        turns_audited=turns_audited,
        silent_misses=silent_misses,
        endorsement_debt_rows=debt_rows,
        endorsement_debt_total=debt_total,
        endorsement_min_retrievals=floor,
        silent_miss_recent=silent_miss_buffer,
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
    lines.append(
        f"Events scanned                      {report.total_events_scanned:>5d}"
    )
    lines.append(
        f"Retrieval occurrences               {report.retrieval_occurrences:>5d}"
    )
    lines.append(f"Applied use events (auto+explicit)  {report.applied_total:>5d}")
    lines.append(f"Turns audited                       {report.turns_audited:>5d}")
    lines.append("")
    lines.append(_format_rate("memory_helped_rate", report.memory_helped_rate))
    lines.append(_format_rate("endorsement_rate ", report.endorsement_rate))
    lines.append(_format_rate("silent_miss_rate ", report.silent_miss_rate))

    if report.endorsement_debt_rows or report.endorsement_debt_total:
        lines.append("")
        lines.append(
            f"Endorsement-debt memories "
            f"(retrievals ≥ {report.endorsement_min_retrievals}, 0 explicit applied): "
            f"{report.endorsement_debt_total}"
        )
        for row in report.endorsement_debt_rows[:10]:
            scopes = ",".join(row.scopes) or "—"
            lines.append(
                f"  {row.id}  {scopes:<18s}  "
                f'"{_truncate(row.summary, 50)}"  ({row.retrieval_count} retrievals)'
            )
        if report.endorsement_debt_total > 10:
            lines.append(f"  … plus {report.endorsement_debt_total - 10} more")

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
            "WARNING: torn-read detected (numerator > denominator) — "
            "log rotation may have raced; numbers may be wrong."
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
    half = (rate.upper - rate.lower) / 2.0
    bar = _bar(rate.rate)
    return (
        f"{label:<20s} {rate.rate:0.2f} ± {half:0.2f}   "
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
    replayable_misses: int
    skipped_legacy_event_count: int
    v1_drift: int = 0
    rows: list[ThresholdSweepRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "total_events_scanned": self.total_events_scanned,
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
    """
    now = now or datetime.now(timezone.utc)
    cutoff: datetime | None = (now - since) if since is not None else None
    rules_in_use = rules or THRESHOLD_RULES

    replayable: list[tuple[list[dict[str, Any]], int]] = []
    total_events_scanned = 0
    legacy_skipped = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        total_events_scanned += 1
        if cutoff is not None:
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts < cutoff:
                continue
        if ev.get("kind") != "search_miss":
            continue
        top_hits = ev.get("top_hits")
        if not isinstance(top_hits, list):
            # Legacy hook shape carried `top_hit_ids` only — no relevance
            # label, so no rule can re-evaluate. Skip but count for the
            # denominator-honesty footnote.
            if isinstance(ev.get("top_hit_ids"), list):
                legacy_skipped += 1
            continue
        recent = ev.get("recent_retrieval_count")
        # `bool` ⊂ `int` — same caveat as `_silent_miss_from_event`.
        if not isinstance(recent, int) or isinstance(recent, bool):
            recent = 0
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
    lines.append(f"Events scanned         {report.total_events_scanned:>5d}")
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
    lines.append("already flagged. Strictly looser rules cannot be evaluated")
    lines.append("from the log alone — turn_audited does not carry top_hits.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tool-usage rollup — per-MCP-tool call counts from the event log
# ---------------------------------------------------------------------------

# Map from event `kind` to the MCP tool that emits it. Used by
# `compute_tool_usage` so the rollup uses tool names rather than the
# wire-format event kinds the recorder writes. The exact set is the
# 18-tool surface listed in `server.py`'s module docstring; tools
# without a dedicated event of their own appear in
# `TOOLS_WITHOUT_TELEMETRY` instead.
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
    "episode_write": "episode_write",
    "episode_handoff": "episode_handoff",
    "episode_search": "episode_search",
    "episode_promote": "episode_promote",
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
_KNOWN_SIDE_EFFECT_KINDS: frozenset[str] = frozenset(
    {"search_miss", "pending_expired", "silent_miss_cutoff"}
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
    total_tool_calls: int
    rows: list[ToolUsageRow] = field(default_factory=list)
    unmapped_event_kinds: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_seconds": self.window_seconds,
            "total_events_scanned": self.total_events_scanned,
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
    total_tool_calls = 0

    for ev in events:
        if not isinstance(ev, dict):
            continue
        total_events_scanned += 1
        if cutoff is not None:
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts < cutoff:
                continue
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
    lines.append(f"Events scanned     {report.total_events_scanned:>5d}")
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


__all__ = [
    "EvalReport",
    "RateCI",
    "EndorsementDebtRow",
    "SilentMissCandidate",
    "ToolUsageReport",
    "ToolUsageRow",
    "ThresholdRule",
    "ThresholdSweepReport",
    "ThresholdSweepRow",
    "THRESHOLD_RULES",
    "TOOLS_WITHOUT_TELEMETRY",
    "DEFAULT_SINCE_SPEC",
    "DEFAULT_ENDORSEMENT_MIN_RETRIEVALS",
    "DEFAULT_SILENT_MISS_LIMIT",
    "compute_eval",
    "compute_tool_usage",
    "compute_threshold_sweep",
    "parse_since",
    "render_text",
    "render_tool_usage_text",
    "render_threshold_sweep_text",
]
