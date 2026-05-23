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


# ---------------------------------------------------------------------------
# Public defaults
# ---------------------------------------------------------------------------

# Default ``--since`` window. 30 days mirrors the default
# ``verification_stale_days`` so the eval window and the freshness
# threshold tell a consistent story by default. Override at the call
# site.
DEFAULT_SINCE_SPEC = "30d"

# Default floor for ``endorsement_debt`` row inclusion. Shared with
# ``health._ENDORSEMENT_DEBT_MIN_RETRIEVALS`` — duplicating the literal
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
        rate = numerator / denominator
        lo, hi = _wilson_interval(numerator, denominator)
        return cls(
            numerator=numerator, denominator=denominator, rate=rate, lower=lo, upper=hi
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": self.rate,
            "ci95_lower": self.lower,
            "ci95_upper": self.upper,
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
            "endorsement_debt": {
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
    via live status — but it cannot participate in ``endorsement_debt``
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
        if kind == "search":
            # Legacy-name fallback — same discipline as the other
            # event consumers (hook / consolidate / _handlers).
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
            is_auto = bool(ev.get("auto"))
            raw_excerpts = ev.get("claim_excerpts")
            # claim_excerpts is parallel to ids; entries may be None.
            # An entry counts toward the helped-rate numerator only
            # when the corresponding excerpt is a non-empty string.
            excerpts: list[Any]
            if isinstance(raw_excerpts, list):
                excerpts = list(raw_excerpts)
            else:
                excerpts = [None] * len(ids)
            for i, mid in enumerate(ids):
                if not isinstance(mid, str):
                    continue
                if not passes_scope(mid):
                    continue
                applied_total += 1
                if is_auto:
                    auto_applied_count[mid] = auto_applied_count.get(mid, 0) + 1
                else:
                    applied_explicit += 1
                    explicit_applied_count[mid] = explicit_applied_count.get(mid, 0) + 1
                    excerpt = excerpts[i] if i < len(excerpts) else None
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
                if len(silent_miss_buffer) > silent_miss_limit:
                    silent_miss_buffer = silent_miss_buffer[-silent_miss_limit:]

    # Endorsement-debt rows: retrieval_count >= floor AND
    # explicit_applied_count == 0. Ambient memories are excluded — same
    # rationale as health's endorsement_debt bucket (their value is
    # implicit; an explicit use event is structurally rare).
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


def _parse_ts(raw: Any) -> datetime | None:
    """Parse the ISO-8601 timestamp the recorder writes. Returns
    ``None`` on any failure — the caller already knows to skip."""
    if not isinstance(raw, str):
        return None
    try:
        # The recorder writes ``YYYY-MM-DDTHH:MM:SS.fffZ``. ``fromisoformat``
        # in 3.11+ accepts that shape; older shapes (without microseconds,
        # or with explicit ``+00:00``) round-trip too.
        # The trailing ``Z`` is replaced with ``+00:00`` for
        # ``fromisoformat`` compatibility on 3.11 / 3.12, which don't
        # accept the bare ``Z`` literal.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
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
    recent_i = recent if isinstance(recent, int) else 0
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
        days = seconds // 86400
        return f"{days}d" if days != 1 else "1 day"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours}h"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}m"
    return f"{seconds}s"


def _short_ts(ts: str) -> str:
    # ``YYYY-MM-DDTHH:MM:SS…`` → ``YYYY-MM-DD HH:MM``.
    if len(ts) < 16 or "T" not in ts:
        return ts
    date_part, _, rest = ts.partition("T")
    time_part = rest[:5]
    return f"{date_part} {time_part}"


__all__ = [
    "EvalReport",
    "RateCI",
    "EndorsementDebtRow",
    "SilentMissCandidate",
    "DEFAULT_SINCE_SPEC",
    "DEFAULT_ENDORSEMENT_MIN_RETRIEVALS",
    "DEFAULT_SILENT_MISS_LIMIT",
    "compute_eval",
    "parse_since",
    "render_text",
]
