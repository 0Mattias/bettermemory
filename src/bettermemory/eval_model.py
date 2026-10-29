"""The eval family's model: rates, rows, rules and window parsing.

`eval.py` computes the effectiveness rates and composes the report.
This module holds the pieces those computations build on, none of
which needs them back: the Wilson interval and `RateCI`, the row
dataclasses the reports carry, the silent-miss readers of an event,
the threshold and widening rule registries, the widening lanes' shared
walk over the audited turns, the `--since` and `--until` parsing, the
recall surfaces' split on each show's rule, the tool-usage kind map
with its side-effect rosters, and the package version the report
carries. `eval.py` re-exports every name here that a caller imports
from it, so `from bettermemory.eval import ...` reads as before.
Nothing here imports `eval.py`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections.abc import Callable, Mapping
from typing import Any, Iterable

from .time_utils import parse_event_ts


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
# 1. Value-duplication of the literal ``30``. The same integer lives at
#    ``health_model._COLD_ENDORSEMENT_MIN_RETRIEVALS``, and that copy is the
#    canonical one — it carries the derivation (calibrated 2026-08-30:
#    (1-p)^N against the dogfood log's pooled explicit-endorse rate
#    *per search delivery*, picking the smallest N with
#    P(zero explicit | healthy) at or under 0.05; the old floor of 5
#    sat at ~coin-flip and flagged memories in active use). We still
#    duplicate rather than import so the eval module keeps out of
#    health's private constants — but the retired claim that drift
#    "would be inert in practice" is exactly what shipped: the 5 → 30
#    recalibration landed on the health side first and the two
#    surfaces published materially different buckets until this copy
#    caught up. Parity is now pinned mechanically
#    (``tests/test_eval.py::TestColdEndorsementMemories::
#    test_default_floor_matches_health_constant``), so the next
#    recalibration fails a test instead of drifting. Because the
#    derivation is per SEARCH delivery, the per-memory count this
#    floor is compared against is search-only too — see the
#    ``search_delivery_count`` comment in ``compute_eval``.
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
DEFAULT_ENDORSEMENT_MIN_RETRIEVALS = 30

# Default cap for the inline silent-miss list. Recent enough to triage
# by eye; the full series lives in the event log for offline replay.
DEFAULT_SILENT_MISS_LIMIT = 20

# Wilson score z for 95% CI.
_WILSON_Z = 1.96

# Token shapes accepted by ``parse_since``: ``\\d+[smhd]`` or the literal
# ``"all"``. ``parse_since`` refuses a span ``timedelta`` cannot hold;
# ``parse_window_bound`` refuses one that reaches back past
# ``_EARLIEST_START``.
_SINCE_PATTERN = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$")
_SINCE_UNIT_TO_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}

# The earliest start a relative bound may reach back to: the calendar's
# first day plus one, so a caller's own `now`, read a moment before the
# parse, still subtracts the span without leaving the calendar. The day
# is slack, not evidence: any margin longer than the gap between a
# caller's `now` and the parse would do.
_EARLIEST_START = datetime.min.replace(tzinfo=timezone.utc) + timedelta(days=1)


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
    deliberately endorses. Mirrors ``health_model.MemoryStats`` minimally —
    we only carry what the eval renderer needs. ``retrieval_count``
    is SEARCH deliveries only, the same basis as
    ``health_model.MemoryStats.retrieval_count`` (which only
    ``_handle_search`` bumps) — see ``search_delivery_count`` in
    ``compute_eval`` for why ``list`` / ``show`` don't count here."""

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


def parse_timestamp_utc(spec: str, *, flag: str) -> datetime:
    """Parse an absolute ISO-8601 timestamp with an explicit UTC offset
    (``Z`` or ``+HH:MM``) into an aware UTC datetime. A timestamp with no
    offset is refused: the live window is stated in UTC, and a local
    reading would shift it by the reader's offset. So is one the calendar
    cannot hold once read in UTC (``0001-01-01T00:00:00+02:00`` is the
    last two hours of the year 0, ``9999-12-31T23:00:00-02:00`` the first
    hour of the year 10000): `astimezone` raises `OverflowError` there,
    which is turned into the `ValueError` that names the flag."""
    raw = spec.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"{flag}: expected 'Ns'/'Nm'/'Nh'/'Nd', 'all', or an ISO-8601 UTC "
            f"timestamp such as 2026-10-01T00:00:00Z, got {spec!r}."
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{flag}: {spec!r} has no UTC offset; add 'Z' (for example "
            f"{spec.strip()}Z)."
        )
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(
            f"{flag}: {spec.strip()!r} lies outside the calendar once read in "
            "UTC; pick a time inside it."
        ) from exc


def parse_window_bound(
    spec: str | None, *, flag: str = "--since", now: datetime | None = None
) -> timedelta | datetime | None:
    """Parse a `--since` or `--until` value: a relative span
    (``Ns``/``Nm``/``Nh``/``Nd``, read back from now), ``all`` or empty
    for no bound (None), or an absolute ISO-8601 UTC timestamp. Raises
    ``ValueError`` with a hint for anything else.

    Every bound returned is one ``resolve_window`` can hold as of `now`
    (the current time when None). A relative span that reaches back past
    ``_EARLIEST_START`` is refused: ``timedelta`` holds it, but `now`
    minus it leaves the calendar, and the subtraction raised
    ``OverflowError`` outside the callers' error handling. An absolute
    ``--since`` later than `now` is refused too: it names a window that
    has not started, which read as an empty one in silence. A later
    ``--until`` stays accepted; it only says "up to now"."""
    if spec is None or spec.strip() in ("", "all"):
        return None
    current = now if now is not None else datetime.now(timezone.utc)
    if _SINCE_PATTERN.fullmatch(spec.strip()):
        span = parse_since(spec.strip())
        if span is not None and span > current - _EARLIEST_START:
            raise ValueError(
                f"{flag}: {spec.strip()!r} reaches back past the year 1. "
                "Pick a smaller window (or 'all' for no time filter)."
            )
        return span
    if not any(ch.isdigit() for ch in spec) or "-" not in spec:
        raise ValueError(
            f"{flag}: expected 'Ns'/'Nm'/'Nh'/'Nd', 'all', or an ISO-8601 UTC "
            f"timestamp such as 2026-10-01T00:00:00Z, got {spec!r}."
        )
    bound = parse_timestamp_utc(spec, flag=flag)
    if flag == "--since" and bound > current:
        raise ValueError(
            f"{flag}: {spec.strip()!r} is later than now, so the window would "
            "hold nothing yet. Pick a past time, a span such as '30d', or 'all'."
        )
    return bound


def resolve_window(
    since: timedelta | datetime | None,
    until: timedelta | datetime | None,
    *,
    now: datetime,
) -> tuple[datetime | None, datetime | None]:
    """The absolute ``[start, end)`` a pair of parsed bounds names as of
    `now`: a relative bound is `now` minus its span."""
    start = now - since if isinstance(since, timedelta) else since
    end = now - until if isinstance(until, timedelta) else until
    return start, end


#: The group the split reports every show under whose rule is not the
#: current one: the rules from before membership decided delivery (rs-v1
#: to rs-v4), read together (design 8.11).
EARLIER_RULES = "earlier"


def _with_rule_split(
    surfaces: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, Any] | None:
    """The surfaces section with its split on each show's rule (design
    8.11) added as ``by_rule`` and ``membership_delta``; None stays None,
    so a log without the surface's rows reports as it did."""
    if surfaces is None:
        return None
    by_rule, delta = surface_rule_split(rows, start=start, end=end)
    return {**surfaces, "by_rule": by_rule, "membership_delta": delta}


def _show_in_window(
    show: Mapping[str, Any], start: datetime | None, end: datetime | None
) -> bool:
    """Whether a show row falls in [start, end), as the surfaces section
    counts it: a row without a readable stamp only in an unbounded
    window."""
    stamp = parse_event_ts(show.get("ts"))
    if stamp is None:
        return start is None and end is None
    return (start is None or stamp >= start) and (end is None or stamp < end)


def surface_rule_split(
    rows: Iterable[dict[str, Any]],
    *,
    start: datetime | None,
    end: datetime | None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """The recall surfaces' tallies over [start, end), split on the rule
    each show row carries, never on a clock (design 8.11): the current
    rule (`recall.RULE`, rs-v5: delivery decided by membership) and the
    earlier rules together (`EARLIER_RULES`; a show with no rule is
    earlier). Each group carries the rules its shows named and, per
    surface (None for a window holding none of the surface's rows), the
    counts and the four rates of the section, computed by
    `recall_counters.compute_surface_report` over the group's shows: what
    the conversation did after a show is joined over every row, whatever
    its rule, and a show's follow-ups end at the id's next show by either
    rule, so the two groups add up to the section.

    `membership_delta` reads the show rows under the current rule that
    carry ``rc3_admitted`` (rows written before rs-v5 carry none and are
    not read): ``rows`` compared, ``rows_differing`` (the delivered ids
    and the rc3-admitted ids are not the same set), ``delivered_not_rc3``
    (ids delivered that rc3's rule would not have delivered) and
    ``rc3_not_delivered`` (ids rc3's rule would have delivered that were
    not)."""
    from .recall import RULE
    from .recall_counters import (
        SURFACES,
        compute_surface_report,
        is_surface_show,
        shown_ids,
    )

    def group_of(show: Mapping[str, Any]) -> str:
        return RULE if show.get("rule") == RULE else EARLIER_RULES

    def in_group(group: str) -> Callable[[Mapping[str, Any]], bool]:
        return lambda show: group_of(show) == group

    surface_rows: list[Mapping[str, Any]] = [
        row for row in rows if isinstance(row, Mapping)
    ]
    shows = [
        row
        for row in surface_rows
        if is_surface_show(row) and _show_in_window(row, start, end)
    ]
    by_rule: dict[str, Any] = {}
    for group in (RULE, EARLIER_RULES):
        report = compute_surface_report(
            surface_rows,
            start=start,
            end=end,
            pick=in_group(group),
        )
        named = {
            rule
            for show in shows
            if group_of(show) == group
            and isinstance(rule := show.get("rule"), str)
            and rule
        }
        by_rule[group] = {
            "rules": sorted(named),
            **{
                surface: report[surface] if report is not None else None
                for surface in SURFACES
            },
        }
    delta = {
        "rows": 0,
        "rows_differing": 0,
        "delivered_not_rc3": 0,
        "rc3_not_delivered": 0,
    }
    for show in shows:
        admitted = show.get("rc3_admitted")
        if group_of(show) != RULE or not isinstance(admitted, list):
            continue
        rc3 = {item for item in admitted if isinstance(item, str)}
        delivered = set(shown_ids(show))
        delta["rows"] += 1
        delta["rows_differing"] += int(delivered != rc3)
        delta["delivered_not_rc3"] += len(delivered - rc3)
        delta["rc3_not_delivered"] += len(rc3 - delivered)
    return by_rule, delta


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

    Mirrors the ``top_hit_id`` half of
    ``health_stats._parse_silent_miss_event``
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


# ---------------------------------------------------------------------------
# Widening preview — replay of candidate LOOSER rules over turn_audited
# ---------------------------------------------------------------------------


def _rule_w1_top1_v2_high(
    top_hits: list[dict[str, Any]], recent_retrieval_count: int
) -> bool:
    """Widening candidate: top-1 SHADOW label == "high" and no recent
    retrieval. The shadow label (`search_lane._relevance_label_v2`) keeps
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
    # Replayable audits dropped because their `ts` predates the latest
    # `silent_miss_cutoff` marker in the stream.
    cutoff_retracted: int
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
    `turn_audited` events carrying a non-empty `top_hits` payload,
    minus those retracted by the latest `silent_miss_cutoff` marker.
    Materialised (not a generator) because both consumers need the
    skip counters alongside the rows.

    The cutoff is a GLOBAL marker resolved after the walk, with the
    same max-semantics `compute_eval` and `health` apply: the latest
    `cutoff_ts` wins wherever it sits in the stream (an earlier cutoff
    seen later cannot undo a later one), it governs even when the
    marker event itself falls outside the `since` window, and once a
    cutoff exists an audit with an unparseable `ts` drops too — the
    same conservative read the rate surfaces take. Rows are therefore
    buffered with their timestamps and filtered once the stream ends.
    """
    cutoff: datetime | None = (now - since) if since is not None else None
    total_events_scanned = 0
    events_in_window = 0
    without_features = 0
    repeats_skipped = 0
    latest_miss_cutoff: datetime | None = None
    candidates: list[
        tuple[dict[str, Any], list[dict[str, Any]], int, datetime | None]
    ] = []
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
        kind = ev.get("kind")
        if kind == "silent_miss_cutoff":
            # Resolved regardless of the window — a bulk retraction
            # logged last week still invalidates the turns it names. A
            # malformed `cutoff_ts` parses to None and is ignored.
            parsed_cutoff = _parse_ts(ev.get("cutoff_ts"))
            if parsed_cutoff is not None and (
                latest_miss_cutoff is None or parsed_cutoff > latest_miss_cutoff
            ):
                latest_miss_cutoff = parsed_cutoff
            continue
        if kind != "turn_audited":
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
        candidates.append((ev, top_hits, recent, ts))
    cutoff_retracted = 0
    rows: list[tuple[dict[str, Any], list[dict[str, Any]], int]] = []
    for ev, top_hits, recent, row_ts in candidates:
        if latest_miss_cutoff is not None and (
            row_ts is None or row_ts < latest_miss_cutoff
        ):
            cutoff_retracted += 1
            continue
        rows.append((ev, top_hits, recent))
    return _ReplayableAudits(
        total_events_scanned=total_events_scanned,
        events_in_window=events_in_window,
        with_features=len(rows),
        without_features=without_features,
        repeats_skipped=repeats_skipped,
        cutoff_retracted=cutoff_retracted,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Widening detail — per-flagged-turn evidence for precision labeling
# ---------------------------------------------------------------------------


def _probe_query_display(
    value: Any,
) -> tuple[str | None, int | None, str | None]:
    """Normalise the two on-disk `probe_query` shapes for display.

    Returns ``(preview, length, hash_prefix)``. The redacted shape
    (the only one written since 9.0.0, the default since 2.6.8) is
    ``{hash, preview, len}``; the verbatim shape an older log can carry
    is a plain string, whose "preview" is the string itself — the log
    already holds it, so the report reveals nothing new.
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


# ---------------------------------------------------------------------------
# Tool-usage rollup — per-MCP-tool call counts from the event log
# ---------------------------------------------------------------------------

# Map from event `kind` to the MCP tool that emits it. Used by
# `compute_tool_usage` so the rollup uses tool names rather than the
# wire-format event kinds the recorder writes. The exact set is the
# nine-tool surface `builder._register_tools` registers: the seven
# memory_* record tools, `episode` and `memory_admin`. The last two
# fan out over actions, so several kinds map to one tool.
#
# Why an explicit map rather than counting raw `kind` values: some
# event kinds (`search_miss`, `use_token_expired`) are side-effects of
# other tools, not tool calls in their own right, and `turn_audited`
# is the Stop hook's audit record with no tool call behind it.
# Counting raw kinds would invent tools for them. The map collapses
# both axes correctly.
_TOOL_EVENT_KIND_TO_TOOL: dict[str, str] = {
    "search": "memory_search",
    "show": "memory_show",
    "write": "memory_write",
    "update": "memory_update",
    "remove": "memory_remove",
    "verify": "memory_verify",
    "use": "memory_record_use",
    "episode_write": "episode",
    "episode_handoff": "episode",
    # `memory_admin` records its acting modes and the tombstones
    # listing; `health` and a pure conflicts listing leave no event, so
    # its row undercounts reads.
    "restore": "memory_admin",
    "list_tombstones": "memory_admin",
    "rename_scope": "memory_admin",
    "scope_disable": "memory_admin",
    "scope_enable": "memory_admin",
    "miss_ack": "memory_admin",
    "conflict_scan": "memory_admin",
    "conflict_verdict": "memory_admin",
    "answer_standing": "memory_admin",
}

# Tools that don't emit a dedicated event of their own; the rollup
# surfaces them with a 0 count and a note rather than silently dropping
# them, so a reader inspecting the report can tell "this tool is not
# counted" apart from "this tool was never called." Empty on the
# nine-tool surface: every tool records at least one of its modes.
TOOLS_WITHOUT_TELEMETRY: tuple[str, ...] = ()

# Event kinds the recorder emits as side-effects of other tools or
# from the hooks, NOT as tool calls in their own right. ``turn_audited``
# is the Stop hook's audit record (`hook.run_audit`), written under the
# client's transcript session id with no tool call behind it, and
# ``search_miss`` is its sub-event (the audit detected a high-relevance
# hit the model would have missed). ``silent_miss_cutoff`` is the
# additive marker the bulk acknowledge
# (``memory_admin(action="acknowledge_miss", before=...)``) writes to
# invalidate a batch of pre-fix miss telemetry. None of these belong in
# the tool-usage rollup, where they would inflate a parent tool's count
# or invent a tool.
#
# The parity test in ``tests/test_eval.py`` asserts that every kind
# recorded anywhere in ``src/`` appears in either
# ``_TOOL_EVENT_KIND_TO_TOOL`` or this set, and that the two are
# mutually exclusive. Adding a new event kind without updating one of
# them is the bug class this guards against. A log carried over from
# v8 can still hold kinds no v9 writer emits (``pending_expired``,
# ``doctor_fix``, ``consolidate_write`` and the like); the rollup
# tallies an unmapped kind rather than dropping it.
#
# ``use_token_expired`` is one batched event per set of retrieval
# tokens that hit the 30-minute wall-clock eviction with nothing having
# settled them (no Stop-hook attribution, no explicit
# ``memory_record_use``, no in-process auto-commit). It is a
# consequence of ``memory_search`` / ``memory_show`` going unsettled,
# not a call, and it has no tool of its own to be attributed to.
#
# ``prompt_recall`` is the UserPromptSubmit hook's delivery record
# (`recall.prompt_recall`): pointers the probe ranked, delivered before
# the turn as context instead of flagged after it. Not a
# tool invocation, so it stays off the usage rollup; it is the
# delivery lane's OWN counter, read beside ``search_miss`` when tracing
# what the recall path did.
#
# ``session_brief`` is the SessionStart hook's record that it put the
# session brief (`recall.session_brief`) in front of the model: pointers
# to the repository's newest fresh memories. A hook record, not a tool
# call, so it stays off the usage rollup like ``prompt_recall``; and,
# unlike ``prompt_recall``, it is not a retrieval kind for the audit.
#
# ``review_grade`` is the owner's grade of one reviewed write, written by
# ``bettermemory review grade`` under a throwaway CLI session. No tool
# call is behind it, and it is admin-recorded by kind.
#
# ``recall_grade`` is the owner's grade of one recall sample's pointers,
# written by ``bettermemory recall grade`` under a throwaway CLI session
# and counted by the surfaces section. Admin-recorded by kind, like
# ``review_grade``.
#
# ``capture_grade`` is the owner's grade of one captured memory against
# the capture rules, written by ``bettermemory capture grade`` under a
# throwaway CLI session. Admin-recorded by kind, like ``review_grade``.
#
# ``review_unanswered`` is the Stop's count of the write-review chains
# the conversation left open since its previous turn
# (`recall.review_unanswered`). A hook record, not a tool call, and not a
# retrieval kind.
#
# ``segment_opened`` is a hook's record of what a compact or clear did to
# the session's recall segment when no brief row carried it: the
# SessionStart's, for a compact or clear whose brief showed nothing
# (`recall.session_brief`; a compact without its marker keeps its segment
# and says ``marker_absent``), and the later read's that found a
# compaction's marker (`recall.confirm_compaction`, with ``opened_at``).
# It carries the segment number a restarted daemon or the in-process path
# reads back. A hook record, not a tool call, and not a retrieval kind.
_KNOWN_SIDE_EFFECT_KINDS: frozenset[str] = frozenset(
    {
        "turn_audited",
        "search_miss",
        "silent_miss_cutoff",
        "use_token_expired",
        "prompt_recall",
        "recall_held",
        "session_brief",
        "review_grade",
        "recall_grade",
        "capture_grade",
        "review_unanswered",
        "segment_opened",
        "standing_guard",
    }
)

# The subset of the roster above recorded INSIDE a live client session,
# under that client's own session id. Verified at the call sites:
# ``turn_audited`` and ``search_miss`` come off the Stop hook's
# recorder (hook.py), ``prompt_recall`` and ``recall_held`` (RS-F7 part
# 6: the probed prompt the recall delivered nothing for) come off the
# UserPromptSubmit hook's recorder (recall.py), stamped with the SAME
# Claude Code transcript session id the Stop hook's rows for that
# conversation carry, ``session_brief`` comes off the SessionStart hook's recorder
# (recall.py) under that same transcript session id,
# ``review_unanswered`` comes off the Stop hook's recorder under that
# same transcript session id too, ``segment_opened`` off the SessionStart,
# UserPromptSubmit or Stop hook's recorder under it as well, and
# ``use_token_expired`` is drained handler-side through the
# live session's recorder (handlers/_shared.py), at the entry of the
# very tool call that noticed the eviction, and ``standing_guard`` is
# recorded handler-side too (handlers/_guard.py), inside the tool call
# whose change to a standing record the user's words carried.
# ``silent_miss_cutoff`` stays
# admin by kind, the classification its v8 CLI writer had: a cutoff on
# its own must never publish a session, and the session that issued
# one through ``memory_admin`` is counted through its other rows.
#
# MEMBERSHIP HERE IS NOT OPTIONAL AND NOT MECHANICALLY CHECKED. The
# admin roster in eval.py is DERIVED as ``_KNOWN − _IN_SESSION``, which makes
# ``ADMIN | IN_SESSION == KNOWN`` a tautology: a kind added to
# ``_KNOWN_SIDE_EFFECT_KINDS`` alone lands in the admin roster, and
# every partition assertion in the suite still passes. The visible
# damage is downstream: ``is_admin_recorded_event`` starts returning
# True, health's session census drops the event AND its whole session,
# and eval's tally treats a real client session as never having
# existed. The only guard is a hand-written, per-kind one: a membership
# assertion plus a behavioural census half, in the shape of
# ``tests/test_eval.py``'s in-session census tests. Write one alongside
# every new entry here.
_IN_SESSION_SIDE_EFFECT_KINDS: frozenset[str] = frozenset(
    {
        "turn_audited",
        "search_miss",
        "use_token_expired",
        "prompt_recall",
        "recall_held",
        "session_brief",
        "review_unanswered",
        "segment_opened",
        "standing_guard",
    }
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
