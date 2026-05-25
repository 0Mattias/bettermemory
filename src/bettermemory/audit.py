"""Silent retrieval-miss telemetry.

The opt-in retrieval contract is bettermemory's load-bearing wager
against auto-injection failure modes: the model searches when
justified, never auto-loads. That contract has a known dark side.
False positives (junk hits) are visible in `dead_weight` and
`recent_negative_outcomes`. False negatives — turns where memory
*should* have been retrieved but wasn't — are structurally invisible,
because nothing in the event log records a search that didn't happen.

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
  `"hybrid"` since 2.6.8; was `"keyword"` in 1.6.0). Probing with a
  different scorer than the model would have used measures the wrong
  thing — a BM25 hit the model on keyword mode would never have seen
  is not a miss the model could have caught. Callers can override the `mode` parameter on the
  probe for offline curation passes that intentionally want a
  different lens, but the default is "what would the model have
  done."

- **`memory_show` and `memory_list` count as retrieval activity too.**
  The miss probe doesn't fire when a `search`, `show`, or `list`
  event landed in the session's lookback window — all three are
  retrievals from the model's perspective (`list` surfaces ids and,
  with `with_bodies=True`, full bodies — the model has the content
  it needed without a `search`). Counting only `search` would mis-flag
  the legitimate search-then-show and triage-via-list flows.

- **No event emitted from this module.** Like `search.search` itself,
  the probe returns a structured verdict; the *handler* records the
  event. Keeps audit.py reusable from CLI / tests / offline tooling
  without dragging the recorder dependency in.

- **Threshold rule is a string in the report.** v1 is `top-1 hit
  relevance == "high"`. The relevance label comes from
  `_relevance_label(matched_unique, query_unique)` which classifies on
  coverage >= 0.75. **Calibration is empirical**, and as of 2.7.0
  there's a tool for it: `bettermemory eval --threshold-sweep` runs
  a counterfactual replay of logged `search_miss` events under the
  bundled stricter rules (v2 score floor, v3 dominance, v4
  intersection) and reports `v1_drift` so a divergence between this
  in-process rule and what production actually flagged is visible.
  Single-token user messages structurally always score "high" if the
  token appears anywhere (1/1 = 1.0); multi-token natural language
  with stopwords often lands at 2/3 = "medium" and does not fire.
  Queries with fewer than `MIN_PROBE_CONTENT_TOKENS` content tokens
  short-circuit to ``no_signal`` before the rule runs — bare
  continuations like "yes" / "continue" / "go for it" structurally
  carry no signal, so dropping them from the audit corpus keeps the
  threshold rule honest. The rule version is recorded on every
  emitted event so a later calibration pass can replay historical
  logs under a new threshold without losing the audit trail.

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
from .origin import Origin, repos_match
from .search import SearchMode, _strip_stopwords, search as run_search, tokenize


# Events that count as "the model retrieved memory in this turn."
# `search` is the obvious one; `show` is the equally-legitimate
# direct-by-id retrieval; `list` is the same surface with a different
# entry point (scope filter, optionally with bodies). All three put
# memory content in front of the model, so a turn where any of them
# fired shouldn't trip the miss probe. Other event kinds (`use`,
# `verify`, etc.) are downstream of an earlier retrieval — counting
# them as retrieval would double-shield the audit. Kept as a
# module-level frozenset so a future event kind (e.g. a hypothetical
# `replay` mode) can be added in one place rather than scattered
# across the function body.
_RETRIEVAL_EVENT_KINDS: frozenset[str] = frozenset({"search", "show", "list"})


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

# Minimum content tokens (stopwords stripped) a probe query must carry
# before the threshold rule is even evaluated. The v1 rule labels any
# 1/1 coverage as "high", so a bare continuation like "yes", "continue",
# "go for it" (1 content token after stopword strip) structurally
# always fires "miss" if the token appears anywhere in any memory —
# the entire single-content-token cohort is a no-signal false-positive
# class. Surfaced on the 2.7.x dogfood log as ~26% of `search_miss`
# events. Two content tokens isn't a calibration claim; it's the
# floor below which coverage carries no information.
MIN_PROBE_CONTENT_TOKENS = 2

# Closed set of `triggered_from` discriminator values for `turn_audited`
# and `search_miss` events. The Stop hook emits `"stop_hook"`; the
# in-process MCP handler emits `"mcp_tool"`. Pinning the set at the
# builder boundary mirrors the search-mode runtime guard in
# `search.py:761` — without this check, a typo elsewhere silently
# produces unsplittable eval rows (downstream consumers `groupby`-split
# on this field). Mirrors the same Literal-at-types-only situation:
# Python doesn't enforce it at call time.
_VALID_TRIGGERED_FROM: frozenset[str] = frozenset({"stop_hook", "mcp_tool"})


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


def turn_audited_fields(
    report: MissReport,
    *,
    session_id: str,
    probe_mode: str,
    assistant_present: bool,
    triggered_from: str,
) -> dict[str, Any]:
    """Canonical field set for a ``turn_audited`` event.

    Single source of truth for the two producers — the Stop hook
    (``hook.run_audit``) and the in-process MCP handler
    (``_handlers._advance_turn``). The 2.6.4 audit found them already
    drifted (``triggered_from`` on the hook, absent on the handler);
    routing both through this builder makes that drift structurally
    impossible. ``triggered_from`` is the source discriminator —
    ``"stop_hook"`` or ``"mcp_tool"``, a closed set both producers
    populate so a consumer can split traffic without guessing.
    """
    if triggered_from not in _VALID_TRIGGERED_FROM:
        raise ValueError(
            f"triggered_from must be one of "
            f"{sorted(_VALID_TRIGGERED_FROM)!r}, got {triggered_from!r}"
        )
    return {
        "session_id": session_id,
        "verdict": report.verdict,
        "lookback_seconds": report.lookback_seconds,
        "recent_retrieval_count": report.recent_retrieval_count,
        "probe_mode": probe_mode,
        "threshold_rule": report.threshold_rule,
        "assistant_present": assistant_present,
        "triggered_from": triggered_from,
    }


def search_miss_fields(
    report: MissReport,
    *,
    session_id: str,
    triggered_from: str,
) -> dict[str, Any]:
    """Canonical field set for a ``search_miss`` event. Pairs with
    :func:`turn_audited_fields` — see that docstring for the
    drift-prevention rationale.

    Carries ``recent_retrieval_count`` so ``eval._silent_miss_from_event``
    can render it: the 2.6.4 audit found that consumer reading the
    field off the ``search_miss`` event while every producer emitted
    it on ``turn_audited`` only — the eval column was always blank.
    """
    if triggered_from not in _VALID_TRIGGERED_FROM:
        raise ValueError(
            f"triggered_from must be one of "
            f"{sorted(_VALID_TRIGGERED_FROM)!r}, got {triggered_from!r}"
        )
    return {
        "session_id": session_id,
        "threshold_rule": report.threshold_rule,
        "lookback_seconds": report.lookback_seconds,
        "recent_retrieval_count": report.recent_retrieval_count,
        "top_hits": [h.to_dict() for h in report.top_hits],
        "probe_query": report.probe_query,
        "triggered_from": triggered_from,
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
    mode: str = "hybrid",
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
    `"hybrid"` (the package default since 2.6.8) when the caller
    doesn't have a config to thread in. Hybrid gracefully degrades to
    keyword+BM25 fusion when no embedding extra is installed. The
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

    # Single-content-token cohort: structurally always scores "high" on
    # the v1 rule because 1/1 = 1.0 = "high" against any memory that
    # mentions the token. Bare continuations ("yes", "continue",
    # "go for it" — "for"/"it" are stopwords) fall in this bucket and
    # are pure noise. probe_query is set so a `no_signal` report on
    # this path is distinguishable from the empty-query branch above.
    if len(_strip_stopwords(tokenize(user_message))) < MIN_PROBE_CONTENT_TOKENS:
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
    elif _caller_in_top_hit_project(top_hits, memories, caller_origin):
        # Caller is working inside the same git project a top-hit memory
        # was written from. The model already has that project's source
        # tree open, so the absence of a memory_search isn't a contract
        # slip — the relevant context is reachable without one.
        # Dogfood evidence (2.7.x): ~95% of replayable misses came from
        # this cohort ("update bettermemory", "push it", "is X up to
        # date") asked from inside the matching repo.
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


def _caller_in_top_hit_project(
    top_hits: tuple[MissHit, ...],
    memories: list[Memory],
    caller_origin: Origin | None,
) -> bool:
    """True when the caller is in the same git project as a top-hit memory.

    Used to suppress the miss verdict when "the model has source open"
    explains the missing search. Both halves are load-bearing:

    * ``projects:`` scope on the hit — a global memory has no project
      boundary to suppress against. Surfacing one through this gate
      would swallow real misses on cross-cutting notes (auth tokens,
      home-dir scripts, etc.) that legitimately should have prompted
      a search even from inside a project repo.
    * ``repos_match`` between the caller's current repo and the
      memory's ``origin.repo`` — the memory was written from this
      same project, so the model is plausibly editing files it
      describes. A project-tagged memory written from a different
      repo (rare misconfiguration) doesn't qualify.

    The auto-scope filter on ``run_search`` already filters cross-repo
    memories out of the top hits when ``caller_origin.repo`` is set, so
    in practice the ``repos_match`` arm usually matches. Checking it
    explicitly keeps the suppression self-contained — the rule reads
    correctly without relying on transitive search behavior, and a
    future caller that bypasses auto-scope (offline curation, eval
    replays) doesn't accidentally suppress real cross-project misses.
    """
    if caller_origin is None or caller_origin.repo is None:
        return False
    memories_by_id = {m.id: m for m in memories}
    for hit in top_hits:
        if not any(s.startswith("projects:") for s in hit.scopes):
            continue
        mem = memories_by_id.get(hit.id)
        if mem is None or mem.origin is None or mem.origin.repo is None:
            continue
        if repos_match(mem.origin.repo, caller_origin.repo):
            return True
    return False


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
        # Canonical-first session read with the legacy fallback the
        # other event consumers use — see 70e41a4.
        if (ev.get("session") or ev.get("session_id")) != session_id:
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
    "MIN_PROBE_CONTENT_TOKENS",
    "THRESHOLD_RULE_V1",
    "MissHit",
    "MissReport",
    "Verdict",
    "probe_for_miss",
    "search_miss_fields",
    "turn_audited_fields",
]
