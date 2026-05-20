"""Silent retrieval-miss telemetry.

The opt-in retrieval contract is bettermemory's load-bearing wager against
mem0's 97.8% junk rate: the model searches when justified, never auto-loads.
That contract has a known dark side. False positives (junk hits) are
visible in `dead_weight` and `recent_negative_outcomes`. False negatives —
turns where memory *should* have been retrieved but wasn't — are
structurally invisible, because nothing in the event log records a search
that didn't happen.

This module closes that loop. `probe_for_miss` runs a cheap search
sweep over the active store using a completed turn's user message and
looks for a high-relevance hit. When a hit exists AND no retrieval
event (`search` or `show`) fired in the same session within a
configurable lookback window, the probe returns a `MissReport` — the
explicit signal that the retrieval contract slipped on this turn. The
probe uses the model's configured search mode by default so it measures
what the model would have done, not what a hypothetical scorer might
have found.

Design notes:

- **Probe matches the model's configured search mode.** The default is
  whatever `config.behavior.search_mode` resolves to (typically
  `"keyword"` in 1.6.0). Probing with a different scorer than the
  model would have used measures the wrong thing — a BM25 hit the
  model on keyword mode would never have seen is not a miss the model
  could have caught. Callers can override the `mode` parameter on the
  probe for offline curation passes that intentionally want a
  different lens, but the default is "what would the model have
  done."

- **`memory_show` counts as retrieval activity too.** The miss probe
  doesn't fire when a `search` OR `show` event landed in the
  session's lookback window — both are retrievals from the model's
  perspective. Counting only `search` would mis-flag the legitimate
  search-then-show flow.

- **No event emitted from this module.** Like `search.search` itself,
  the probe returns a structured verdict; the *handler* records the
  event. Keeps audit.py reusable from CLI / tests / offline tooling
  without dragging the recorder dependency in.

- **Threshold rule is a string in the report.** v1 is `top-1 hit
  relevance == "high"`. The relevance label comes from
  `_relevance_label(matched_unique, query_unique)` which classifies on
  coverage >= 0.75. **Calibration is unknown.** Single-token user
  messages structurally always score "high" if the token appears
  anywhere (1/1 = 1.0); multi-token natural language with stopwords
  often lands at 2/3 = "medium" and does not fire. Whether the v1
  rule under-fires, over-fires, or is roughly right depends on the
  real distribution of user messages, which we don't have data on
  yet. The rule version is recorded on every emitted event so a
  later calibration pass can replay historical logs under a new
  threshold without losing the audit trail.

- **Lookback is wall-clock, not turn-counter.** The audit fires from a
  client-side hook (Claude Code Stop hook, etc.), which doesn't carry
  the server's per-session turn counter. A 60s window comfortably
  covers a normal model turn while staying short enough that a search
  from two turns ago doesn't paper over a fresh miss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, cast

from .models import Memory, MemoryHit
from .origin import Origin
from .search import SearchMode, search as run_search


# Events that count as "the model retrieved memory in this turn."
# `search` is the obvious one; `show` is the equally-legitimate
# direct-by-id retrieval. Other event kinds (`use`, `verify`, etc.) are
# downstream of an earlier retrieval — counting them as retrieval would
# double-shield the audit. Kept as a module-level frozenset so a future
# event kind (e.g. a hypothetical `replay` mode) can be added in one
# place rather than scattered across the function body.
_RETRIEVAL_EVENT_KINDS: frozenset[str] = frozenset({"search", "show"})


# Threshold rule identifiers. Bumped when the criterion changes so a
# replay of older `search_miss` events can be re-evaluated against the
# new rule without losing the rule the original event was written under.
THRESHOLD_RULE_V1 = "v1_top1_high"

# Top-N hits to retain on every MissReport so a curation pass can replay
# the decision (e.g. "what was the system seeing when it flagged this?").
# Three is enough to triage by eye and small enough that the event log
# doesn't balloon with bodies.
_TOP_HITS_RETAINED = 3

# Default lookback for "was there a search event in this session". Long
# enough to cover a normal multi-tool model turn; short enough that a
# search from earlier in the session doesn't paper over a fresh miss.
DEFAULT_LOOKBACK_SECONDS = 60


# Verdict literals — surfaced both in the structured return and (verbatim)
# in the emitted event. Treat as a closed set; downstream consumers will
# branch on these.
Verdict = Literal["miss", "ok", "no_signal"]


@dataclass(frozen=True)
class MissHit:
    """Identity + ranking metadata for one probe hit.

    Bodies are deliberately not retained — the id + snippet is enough to
    triage offline, and full bodies in the event log would balloon disk
    usage on a busy store. Snippet uses the same `snippet_for` shape the
    search response builds, so a replay can look identical to a real
    search hit.
    """

    id: str
    score: float
    relevance: str
    scopes: tuple[str, ...]
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "score": self.score,
            "relevance": self.relevance,
            "scopes": list(self.scopes),
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class MissReport:
    """Structured verdict from `probe_for_miss`.

    `verdict` is the load-bearing field:

    - ``"miss"``: a high-relevance hit exists for this turn's query AND no
      `search` event fired in the lookback window. The retrieval contract
      slipped — the model should have searched.
    - ``"ok"``: either no hit cleared the threshold (genuine "nothing to
      retrieve here") OR a search already fired in the lookback window
      (the model did search; nothing for the audit to flag).
    - ``"no_signal"``: the probe couldn't run meaningfully — empty store,
      empty query, all-stopword query. Distinct from "ok" so the consumer
      can tell "audit ran and saw nothing relevant" apart from "audit
      had nothing to work with."

    `recent_retrieval_count` is the number of retrieval events (`search`
    or `show`) found in the session within `lookback_seconds`. Zero
    means no retrieval happened within the window; non-zero is the
    "model did retrieve" branch.

    `threshold_rule` records which decision rule was applied. Versioned
    so a future calibration pass can replay old reports under a new rule.
    """

    verdict: Verdict
    checked_at: datetime
    session_id: str
    lookback_seconds: int
    recent_retrieval_count: int
    threshold_rule: str
    top_hits: tuple[MissHit, ...] = field(default_factory=tuple)
    # Echoes back the raw user_message the probe ran against. None when
    # the probe was aborted before run_search executed at all (empty
    # store or empty/whitespace-only query); set to the input string in
    # every branch that reached the ranker — including the "no hits"
    # branch (e.g. an all-stopwords query that run_search filtered to
    # empty). Useful for offline triage: a non-None probe_query on a
    # `no_signal` report tells you the ranker actually ran and saw
    # nothing, distinct from "we never asked it."
    probe_query: str | None = None

    @property
    def is_miss(self) -> bool:
        return self.verdict == "miss"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "checked_at": self.checked_at.isoformat().replace("+00:00", "Z"),
            "session_id": self.session_id,
            "lookback_seconds": self.lookback_seconds,
            "recent_retrieval_count": self.recent_retrieval_count,
            "threshold_rule": self.threshold_rule,
            "top_hits": [h.to_dict() for h in self.top_hits],
            "probe_query": self.probe_query,
        }


def probe_for_miss(
    memories: list[Memory],
    user_message: str,
    *,
    recent_events: Iterable[dict[str, Any]],
    session_id: str,
    now: datetime | None = None,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    caller_origin: Origin | None = None,
    excluded_scopes: set[str] | None = None,
    mode: str = "keyword",
) -> MissReport:
    """Decide whether the just-completed turn was a silent retrieval miss.

    Runs a cheap search probe over `memories` using `user_message`, then
    asks: did the model retrieve memory this session within the last
    `lookback_seconds` (via `search` or `show`)? The cross of those two
    facts gives the verdict.

    `recent_events` is an iterable over the session's recent event log
    entries (any iterable — the function only walks it once). Callers
    typically pass `iter_events(store.root)` filtered to the current
    session id, or a slice of the in-memory recorder buffer. The function
    consumes the iterable defensively in a single pass; it does not
    materialise unbounded history.

    `caller_origin` mirrors the auto-scoping behaviour of `memory_search`
    so the probe matches the *model's* view of the store. Probing with a
    wider lens than the model uses would generate ghost misses for
    memories the model couldn't have retrieved in the first place. Pass
    None on intentionally cross-project audits.

    `mode` is forwarded to `search`. Production callers pass
    `config.behavior.search_mode` so the probe sees the same ranker
    the model would have used — probing with a different scorer
    measures "would a different ranker have hit" rather than "did the
    model miss what its ranker would have shown." Default falls to
    `"keyword"` (the package default) when the caller doesn't have a
    config to thread in. `"hybrid"` is available for offline curation
    passes that want paraphrase recall and have extras installed. The
    probe deliberately does NOT request `expand_top` or `path_drift` —
    those signals matter for *consuming* a hit, not for deciding
    whether a search should have happened.

    Returns a `MissReport`. The handler is responsible for emitting a
    `search_miss` event when `report.is_miss` — this function is
    side-effect-free so it can be reused from offline tooling.
    """
    now = now or datetime.now(timezone.utc)

    if not memories or not user_message.strip():
        return MissReport(
            verdict="no_signal",
            checked_at=now,
            session_id=session_id,
            lookback_seconds=lookback_seconds,
            recent_retrieval_count=0,
            threshold_rule=THRESHOLD_RULE_V1,
            top_hits=(),
            probe_query=None,
        )

    repo_filter = caller_origin.repo if caller_origin else None
    worktree_filter = caller_origin.worktree_root if caller_origin else None

    # `run_search` returns up to `max_results` hits already sorted by
    # relevance; we ask for `_TOP_HITS_RETAINED` so the report carries
    # enough context to triage without a second call. `mode` is cast to
    # the Literal `SearchMode` after validation — anything outside the
    # allowed set lets `run_search` raise the same ValueError the
    # memory_search handler would, so a bad config doesn't silently
    # degrade the audit.
    if mode not in ("keyword", "bm25", "semantic", "hybrid"):
        raise ValueError(
            f"unknown audit probe mode {mode!r}; "
            "must be one of: keyword, bm25, semantic, hybrid"
        )
    hits: list[MemoryHit] = run_search(
        memories,
        user_message,
        excluded_scopes=excluded_scopes,
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
        max_results=_TOP_HITS_RETAINED,
        now=now,
        mode=cast(SearchMode, mode),
    )
    if not hits:
        return MissReport(
            verdict="no_signal",
            checked_at=now,
            session_id=session_id,
            lookback_seconds=lookback_seconds,
            recent_retrieval_count=0,
            threshold_rule=THRESHOLD_RULE_V1,
            top_hits=(),
            probe_query=user_message,
        )

    top_hits = tuple(
        MissHit(
            id=h.id,
            score=h.score,
            relevance=h.relevance,
            scopes=tuple(h.scopes),
            snippet=h.snippet,
        )
        for h in hits
    )

    # v1 rule: any top-1-relevance="high" hit counts as "the probe found
    # something the model should have retrieved." Looser than requiring
    # high on all three; tighter than letting "medium" through (which
    # would fire on every borderline query and drown the bucket).
    top_hit = top_hits[0]
    clears_threshold = top_hit.relevance == "high"

    recent_retrieval_count = _count_recent_retrievals(
        recent_events,
        session_id=session_id,
        now=now,
        lookback_seconds=lookback_seconds,
    )

    if not clears_threshold:
        verdict: Verdict = "ok"
    elif recent_retrieval_count > 0:
        verdict = "ok"
    else:
        verdict = "miss"

    return MissReport(
        verdict=verdict,
        checked_at=now,
        session_id=session_id,
        lookback_seconds=lookback_seconds,
        recent_retrieval_count=recent_retrieval_count,
        threshold_rule=THRESHOLD_RULE_V1,
        top_hits=top_hits,
        probe_query=user_message,
    )


def _count_recent_retrievals(
    events: Iterable[dict[str, Any]],
    *,
    session_id: str,
    now: datetime,
    lookback_seconds: int,
) -> int:
    """Count retrieval events for `session_id` within the window.

    Retrieval = `kind in {"search", "show"}`. Both shield the audit
    from flagging a miss: a memory_show by id is an equally legitimate
    way for the model to pull content. Counting only `search` would
    mis-flag the common search-then-show round-trip, where the search
    happens early in the turn and the show happens later.

    Defensive against the same malformed-event cases the rest of the
    health pipeline handles: missing ts, non-string ts, non-session
    events. A single bad row never raises.
    """
    cutoff = now - timedelta(seconds=lookback_seconds)
    count = 0
    for ev in events:
        if ev.get("kind") not in _RETRIEVAL_EVENT_KINDS:
            continue
        if ev.get("session") != session_id:
            continue
        ts = _parse_ts(ev.get("ts"))
        if ts is None or ts < cutoff:
            continue
        count += 1
    return count


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


__all__ = [
    "DEFAULT_LOOKBACK_SECONDS",
    "THRESHOLD_RULE_V1",
    "MissHit",
    "MissReport",
    "Verdict",
    "probe_for_miss",
]
