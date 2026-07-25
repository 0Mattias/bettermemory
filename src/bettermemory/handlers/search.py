"""memory_search MCP tool — handler implementation + DESC.

The handler is the busiest of the 25 tools: it issues use-tokens,
attaches per-hit drift signals, optionally expands the top hit, and
records its own event with a generous payload shape so the eval CLI
can rebuild what the model saw.

Description-edit history:

- H8 (Round 2): the "before you call this" guidance was buried mid-string.
  Hoisted to a two-line lead block so it's the first thing the model
  reads — opt-in retrieval + the transparency requirement land before
  any parameter detail.
- H9: `query` stopped being described as "free text". Every line above it
  told the caller how to READ a result; nothing told it how to WRITE the
  one input that decides whether a result exists. Measured on a 185-memory
  store over a 20-question gold set authored document-first in caller
  voice (so the ranker never picked the labels): questions as asked
  retrieve at 10% recall@1, the same questions re-queried in concrete
  nouns at 65%, and an insider-vocabulary ceiling arm at 90%. The control
  is what fixes the wording: stripping the question words and keeping the
  content words scores 10% — byte-identical to asking, because the ranker
  already strips stopwords. So the cue names the lever that moved
  (vocabulary the memory would literally contain) and not the one that
  did nothing ("use keywords, not questions"), and it names re-querying,
  since a weak first hit is the caller's only signal that it happened.
  Same pass retracted "`hybrid` for paraphrase recall" from `mode`: with
  no semantic leg configured — the package default — hybrid is RRF over
  keyword and BM25, both lexical, so that line promised the caller the
  exact capability whose absence this measurement is about.

  Paid for in the same string rather than by raising the ratchet in
  `test_default_on_descriptions_fit_budget`: `since_prior_session` gave
  back the sentence deriving why its boundary is strict-`>`. That is
  rationale for a decision the caller cannot influence, it was resident
  on every turn, and its actual guard is
  `test_prompts.py::test_api_md_since_prior_session_strict_after`, which
  pins the wording in `docs/api.md` — the field's actionable semantics
  ("strictly after", the /loop guidance, empty-vs-no-baseline) all stay.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, cast

from ..events import _event_id_list
from ..models import utcnow, validate_scope
from .._response import NEGATIVE_OUTCOME_WINDOW_DAYS
from ..time_utils import parse_event_ts
from ..search import (
    CorpusStats,
    SearchMode,
    candidate_admitted,
    _filter_candidates,
    _relevance_label_v2,
    search as run_search,
)
from ..store import MemoryNotFoundError, Store, TombstonedError
from ..verify import (
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)
from ._shared import Context, _advance_turn, _attach_use_tokens

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..config import BehaviorConfig


DESC_MEMORY_SEARCH = (
    "Search stored memories. Default: do NOT call — reach for it only "
    "when the user references shared context you lack "
    '("my project", "the script we wrote") or a request is ambiguous '
    "in a way stored preferences could resolve. When a hit shapes your "
    'reply, announce it ("Using your stored preference for…") — '
    "non-negotiable. (Full policy: the server `instructions` block.)\n\n"
    "Returns ranked hits with snippets. Per-hit fields the model "
    "should branch on:\n"
    "- `relevance` (high/medium/low) — use this, not the raw score; "
    'treat "low" as probable noise.\n'
    "- `staleness_verdict` (fresh / spot_check_recommended / "
    "spot_check_required) — rolled-up signal. When != fresh, "
    "the hit already carries the actionable detail (see "
    "`path_drift` below); memory_update what drifted, "
    "memory_verify the rest.\n"
    "- `match_terms` — which query words actually hit.\n"
    "- `path_drift_missing` (int) + `path_drift` ({checked, "
    "missing, verified} lists, when drift detected) — body-cited "
    "paths gone. Act on `path_drift.missing` directly; no "
    "memory_show round-trip needed.\n"
    "- `commit_drift_count` (int, when applicable) — commits since "
    "last_verified_at on the memory's origin repo. Non-zero means "
    "the project moved even if calendar-fresh.\n"
    "- `depends_on_resolved` (when present) — bounded auto-pull of "
    "`depends_on` link targets (max 3 per hit, max 10 per call). "
    "Each entry: `{id, scopes, summary, link_note}`. Surfaces "
    "context the query wouldn't on its own; saves a memory_show "
    "round-trip. OMITTED when the hit has no `depends_on` links.\n"
    "- `recent_negative_outcomes` (when present) — list of recent "
    "ignored/contradicted events for this memory (max two, one "
    "per outcome). The user already rejected this; don't re-surface "
    "unless you have new reason. OMITTED when none.\n\n"
    "Parameters:\n"
    "- `query`: nouns a memory would contain (tool, file, error names), "
    "not question phrasing — measured 10%→65% recall@1. Weak hits: "
    "re-query, different nouns.\n"
    "- `scopes` (optional): filter to scope union.\n"
    "- `max_results` (default 5, cap 50).\n"
    "- `expand_top=True`: inline the full body of the top hit when "
    'its relevance is "high" — saves a memory_show round trip and '
    "surfaces the full path_drift + commit_drift detail.\n"
    "- `auto_scope=True` (default): filter to current repo+worktree; "
    "memories with no recorded origin always pass as global. Set "
    "False for explicit cross-project queries.\n"
    "- `since_prior_session=False` (default): when True, filter "
    "to memories whose `updated` is strictly after the prior "
    "session boundary (latest event from a different session_id "
    "in the log). The semantic is 'what has "
    "changed in the current session, since the last activity by "
    "other sessions' — i.e. this session's intra-session diff. A "
    "/loop iteration uses this to track what IT has "
    "written/updated; for what the prior iteration did, call "
    "episode_handoff instead. Returns empty when there's no prior "
    "session in the log; distinguish 'nothing new' (results=[]) "
    "from 'no baseline' by also calling memory_scope_overview and "
    "checking `curation_pending_new_since_last_session is None`.\n"
    "- `mode` (optional, default from config; package default `hybrid`): `keyword`, `bm25`, "
    "`semantic` (needs embeddings extra + config opt-in), or `hybrid` "
    "(RRF of all three). Without the semantic leg every mode is "
    "lexical, so see `query`.\n\n"
    "Outcome is recorded automatically via the use_token within ~2 "
    "turns; only call memory_record_use to override "
    "(ignored / contradicted / corrected)."
)


def _explicit_applied_counts(
    events: list[dict[str, Any]],
    candidate_ids: set[str],
    *,
    now: datetime,
    lookback_seconds: int,
) -> dict[str, int]:
    """Tally explicit `memory_record_use(applied)` events per candidate id.

    Only DELIBERATE applies count: events with `auto is True` (the ~2-turn
    auto-fallback) are excluded, mirroring the auto/explicit split health.py
    and eval.py already use — auto-applies would otherwise inflate every
    retrieved memory and defeat the point. Restricted to `candidate_ids` so
    the tally is bounded by the result set, not the whole store.

    The attribution cutoff is MANDATORY and enforced HERE, not delegated to
    the caller. Every consumer must supply `now` + `lookback_seconds`; an
    event whose `ts` is older than `now - lookback_seconds` (or that carries
    no parseable `ts` at all — an unprovable event can't be shown in-window)
    is dropped internally. The old contract applied no cutoff of its own and
    trusted each caller to pre-window the event list; that mismatch let a
    caller feeding a wider coverage read (the dedup-widened 3600s `recent`
    the audit producers walk) silently over-count applies from up to an hour
    ago that production's 600s ranker never saw, nudging a near-tie top-1 and
    flipping a false `search_miss`. Making the window a required argument the
    function enforces closes that whole class of caller misuse: every site
    now passes the same 600s attribution horizon
    (`ATTRIBUTION_LOOKBACK_SECONDS`) and the tally can no longer be silently
    widened."""
    cutoff_ts = now.timestamp() - lookback_seconds
    counts: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") != "use" or ev.get("outcome") != "applied":
            continue
        if ev.get("auto") is True:
            continue
        ts = parse_event_ts(ev.get("ts"))
        if ts is None or ts.timestamp() < cutoff_ts:
            continue
        # Never iterate the raw id field: the event log is plaintext and
        # hand-editable, and a scalar / nested-list `ids` here failed EVERY
        # memory_search and memory_audit_turn call (TypeError / unhashable)
        # under endorsement_boost — the exact poison shapes health.py was
        # hardened against while this walk still read raw. One shared
        # normalizer (`events._event_id_list`) for all consumers.
        for mid in _event_id_list(ev.get("ids") or ev.get("memory_ids")):
            if mid in candidate_ids:
                counts[mid] = counts.get(mid, 0) + 1
    return counts


def _active_negative_counts(
    events: list[dict[str, Any]],
    candidate_ids: set[str],
    *,
    now: datetime,
    window_days: int,
    resolution_ts_by_id: dict[str, datetime],
) -> dict[str, tuple[int, int]]:
    """Tally ACTIVE negative use outcomes (ignored, contradicted) per
    candidate id, for the `[behavior] outcome_demotion` ranking factor.

    "Active" reuses the exact liveness rules the
    `recent_negative_outcomes` annotation applies, plus the resolution
    clearing `health._has_unresolved_contradiction` established — the
    ranker must never demote on evidence the other surfaces would call
    settled:

    - windowed: events older than `window_days` are dropped. The cutoff
      is enforced HERE, not delegated to the caller — same mandatory-
      window contract as `_explicit_applied_counts`, so a caller feeding
      a wider event read cannot silently widen the tally.
    - superseded: a later NON-AUTO `applied` clears every earlier
      negative (the model re-validated the memory). An auto-fallback
      apply carries no judgment and clears nothing — mirroring
      `attach_recent_negative_outcomes`.
    - resolved: a negative at or before the memory's resolution
      timestamp — `max(updated, last_verified_at)`, supplied via
      `resolution_ts_by_id` — judged a body that has since been
      rewritten or re-attested, so it no longer testifies. This is
      `health._has_unresolved_contradiction`'s rule applied per-event.
    - `corrected` is audit-only and `applied` is the positive case:
      neither counts negative.

    Returns a SPARSE dict — ids with no active negatives are absent — so
    the scorer's `.get(id, (0, 0))` default stays the common path."""
    cutoff_ts = now.timestamp() - window_days * 86400
    timelines: dict[str, list[tuple[datetime, str, bool]]] = {}
    for ev in events:
        if ev.get("kind") != "use":
            continue
        outcome = ev.get("outcome")
        if outcome not in ("ignored", "contradicted", "applied"):
            continue
        ts = parse_event_ts(ev.get("ts"))
        if ts is None or ts.timestamp() < cutoff_ts:
            continue
        auto = ev.get("auto") is True
        for mid in _event_id_list(ev.get("ids") or ev.get("memory_ids")):
            if mid in candidate_ids:
                timelines.setdefault(mid, []).append((ts, str(outcome), auto))

    counts: dict[str, tuple[int, int]] = {}
    for mid, timeline in timelines.items():
        timeline.sort(key=lambda entry: entry[0])
        resolution_ts = resolution_ts_by_id.get(mid)
        ignored = 0
        contradicted = 0
        for ts, outcome, auto in timeline:
            if outcome == "applied":
                if not auto:
                    ignored = 0
                    contradicted = 0
                continue
            if resolution_ts is not None and ts <= resolution_ts:
                continue
            if outcome == "ignored":
                ignored += 1
            else:
                contradicted += 1
        if ignored or contradicted:
            counts[mid] = (ignored, contradicted)
    return counts


class RankingInputs(NamedTuple):
    """Every `[behavior]`-driven input `search.search` takes beyond the
    query and the candidate list.

    One shape so the surfaces that rank memories cannot drift apart on
    THESE inputs: a knob lands in `resolve_ranking_inputs` once and every
    caller threads it. Three consume it — this handler, the web UI's
    `/memories` search, and (through both audit producers) the
    silent-miss probe. Before this existed the web ran the same ranker
    with the config inputs dropped, so `endorsement_boost` /
    `outcome_demotion` / `corroboration_boost` / a tuned
    `recency_boost_half_life_days` reordered results for the model and
    did nothing for the human reading the curation page.

    Scope note: this covers the `[behavior]` knobs only, NOT the
    candidate pool or its BM25 corpus statistics — those are a separate
    decision with a separate helper (`resolve_search_pool`) and a
    smaller reach. `web.py` deliberately ranks `store.load_all()` with
    no `corpus_stats_provider` (the full corpus IS its own statistics),
    so it threads this shape but not that one.

    `events` is the raw windowed event read the two tallies shared —
    `None` when neither tally ran. Exposed so a caller that needs the
    same window for a downstream annotation
    (`ResponseBuilder.attach_recent_negative_outcomes`) can reuse it
    instead of paying a second read.
    """

    applied_by_id: dict[str, int] | None
    negative_by_id: dict[str, tuple[int, int]] | None
    corroboration_boost: bool
    half_life_days: float
    events: list[dict[str, Any]] | None


def ranking_events_window_seconds(behavior: "BehaviorConfig") -> int | None:
    """How wide an event read `resolve_ranking_inputs` needs under
    `behavior` — or None when neither usage tally is enabled and no read
    is needed at all.

    The base is the attribution horizon `_explicit_applied_counts`
    enforces (`audit.ATTRIBUTION_LOOKBACK_SECONDS`). With
    `outcome_demotion` on it widens to the full negative window
    `_active_negative_counts` tallies over (`NEGATIVE_OUTCOME_WINDOW_DAYS`):
    a 600s request would only rotation-proof 600s of coverage, so older
    negatives would survive only by luck of the active log's size.

    Widening the READ can never widen a tally — both count-functions
    enforce their own cutoffs internally, which is exactly why their
    window arguments are mandatory.

    Split out of `resolve_ranking_inputs` so a caller that must issue the
    read itself can ask for the width the helper would have used instead
    of hardcoding one that drifts. Both audit producers do:
    `hook.run_audit` and `handlers.audit_turn.memory_audit_turn` each keep
    a module-local `iter_events_window` call — which is also the seam the
    suite's window-width spies patch — and hand the result back via
    `resolve_ranking_inputs(events=...)`."""
    if not (behavior.endorsement_boost or behavior.outcome_demotion):
        return None
    from ..audit import ATTRIBUTION_LOOKBACK_SECONDS

    window = ATTRIBUTION_LOOKBACK_SECONDS
    if behavior.outcome_demotion:
        window = max(window, NEGATIVE_OUTCOME_WINDOW_DAYS * 86400)
    return window


def resolve_ranking_inputs(
    root: Path,
    memories: Sequence[Any],
    behavior: "BehaviorConfig",
    *,
    now: datetime | None = None,
    events: list[dict[str, Any]] | None = None,
) -> RankingInputs:
    """Build the `RankingInputs` for one search over `memories`.

    Usage-aware ranking, both directions (each opt-in via `[behavior]`):
    `endorsement_boost` tallies how many times the model EXPLICITLY
    applied each candidate (bounded nudge up); `outcome_demotion`
    tallies still-active ignored/contradicted outcomes (bounded slide
    down — see `_active_negative_counts` for what "active" excludes).
    Both stay `None` (ranker neutral) when their flag is off, so the
    shipped default ranking is unchanged.

    Window-aware read (round 88): both audit producers
    (`hook.run_audit`, `memory_audit_turn`) tally over
    `iter_events_window`, so this reads the same substrate — reading the
    active log only meant the tally silently reset to `{}` the moment a
    rotation cut the applied events into an archive while the probes
    still saw the history, and a near-tie top-1 could rank differently
    in the audit than in the model's actual retrieval.

    One event read serves up to three consumers (endorsement tally,
    demotion tally, and the caller's `recent_negative_outcomes`
    annotation). `ranking_events_window_seconds` decides its width —
    with demotion on it widens from the 600s attribution horizon to the
    full negative window, which also upgrades the annotation's 30-day
    contract from best-effort to guaranteed.

    `events` lets a caller supply that read instead of having this helper
    issue it, for callers whose event access has to stay module-local
    (both audit producers — see `ranking_events_window_seconds`). Supply
    it at that function's width; a wider feed cannot widen either tally,
    since both count-functions re-derive their own cutoffs from `now`. It
    is ignored — and the returned `events` stays None — whenever no tally
    runs, so the field keeps meaning "the read the tallies shared".
    """
    applied_by_id: dict[str, int] | None = None
    negative_by_id: dict[str, tuple[int, int]] | None = None
    tally_events: list[dict[str, Any]] | None = None
    demotion_on = behavior.outcome_demotion
    window_seconds = ranking_events_window_seconds(behavior)
    if window_seconds is not None and memories:
        from ..audit import ATTRIBUTION_LOOKBACK_SECONDS
        from ..events import iter_events_window

        tally_events = (
            events
            if events is not None
            else list(iter_events_window(root, window_seconds))
        )
        tally_now = now if now is not None else utcnow()
        candidate_ids = {m.id for m in memories}
        if behavior.endorsement_boost:
            applied_by_id = _explicit_applied_counts(
                tally_events,
                candidate_ids,
                now=tally_now,
                lookback_seconds=ATTRIBUTION_LOOKBACK_SECONDS,
            )
        if demotion_on:
            negative_by_id = _active_negative_counts(
                tally_events,
                candidate_ids,
                now=tally_now,
                window_days=NEGATIVE_OUTCOME_WINDOW_DAYS,
                resolution_ts_by_id={
                    m.id: (
                        max(m.updated, m.last_verified_at)
                        if m.last_verified_at is not None
                        else m.updated
                    )
                    for m in memories
                },
            )
    return RankingInputs(
        applied_by_id=applied_by_id,
        negative_by_id=negative_by_id,
        corroboration_boost=behavior.corroboration_boost,
        half_life_days=behavior.recency_boost_half_life_days,
        events=tally_events,
    )


class SearchPool(NamedTuple):
    """The candidate list one search ranks, plus the BM25
    corpus-statistics wiring THAT pool needs.

    The two travel together because they are one decision: a pool served
    by the FTS prefilter is query-biased and needs corpus-derived
    document frequencies to keep its IDF honest, while a `load_all` pool
    IS the corpus and must not pay the lookup. Splitting them let a
    caller take one without the other — which is exactly how the
    silent-miss probe came to rank an unconditional `load_all()` with no
    provider while `memory_search` ranked a capped, corpus-corrected
    slice.

    `memories` is a SEARCH pool, never a census: above the index
    threshold it is capped and query-biased. Anything that wants the
    store's size must call `store.load_all()` itself.
    """

    memories: list[Any]
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None


# The ceiling on how many hits one search serves; with the floor of 1 it
# is the whole range a result width — request or config — can take.
# `memory_search` narrows a REQUEST to it and `default_search_width`
# narrows the CONFIG knob to it, both through `clamp_search_width`, so the
# request path and the audit path cannot disagree about the range.
#
# NOT `_handlers._PREFILTER_CAP`, which is also 50 today: that one bounds
# the INDEX ROWS the FTS prefilter returns, this one bounds the HITS a
# search serves. `resolve_search_pool`'s starvation guard compares a count
# bounded by the first against a width bounded by the second, so folding
# them into one constant would turn a measurement into an artifact of the
# spelling.
MAX_SEARCH_RESULTS = 50


def clamp_search_width(value: int) -> int:
    """Narrow a result width to the range a search can actually serve —
    `[1, MAX_SEARCH_RESULTS]`.

    The single arithmetic site for that range, read by all three widths
    that reach `resolve_search_pool`: `memory_search`'s request and both
    silent-miss producers' `min_survivors` (the latter two via
    `default_search_width`). Hoisted here because the producers used to
    read `behavior.default_max_results` RAW while `memory_search` clamped
    the same number before handing it to the same parameter — so an
    out-of-range knob desynchronised the audit's starvation guard from
    production's while a pin using an in-range value stayed green.
    """
    return max(1, min(int(value), MAX_SEARCH_RESULTS))


def default_search_width(behavior: "BehaviorConfig") -> int:
    """The width of a DEFAULT `memory_search` under `behavior` — what both
    silent-miss producers size their starvation guard for.

    `config.py` coerces `default_max_results` to an int at load and range-
    checks nothing, so the knob arrives here as any integer whatsoever.
    Clamping it through `clamp_search_width` is what keeps "the width of a
    default search" a width the request path can also produce, and outside
    that range the two ends of the guard come apart in opposite
    directions:

    - above `MAX_SEARCH_RESULTS` no survivor count can reach the threshold
      at all (the prefilter serves at most `_handlers._PREFILTER_CAP`
      rows), so `resolve_search_pool`'s `<` holds unconditionally on every
      saturated slice and the probe is handed the whole corpus with no
      corpus-statistics provider — the strict-superset-under-different-
      statistics pool that helper exists to eliminate;
    - at `<= 0` the comparison can never hold, so the probe keeps a
      starved slice that production (clamped to 1) reloads. That one
      reaches the verdict: measured on a fully starved slice, the probe
      returns `no_signal` at width 0 where width 1 returns `miss`.
    """
    return clamp_search_width(behavior.default_max_results)


def resolve_search_pool(
    store: Store,
    query: str,
    *,
    scopes: list[str] | None = None,
    excluded_scopes: set[str] | None = None,
    repo_filter: str | None = None,
    worktree_filter: str | None = None,
    min_survivors: int,
) -> SearchPool:
    """Build the candidate pool for `query` exactly as production
    retrieval does.

    Shared by `memory_search` and BOTH silent-miss audit producers
    (`hook.run_audit`, `handlers.audit_turn.memory_audit_turn`). The
    probe exists to measure what production retrieval would have
    surfaced, so it has to start from production's candidate set: an
    unconditional `load_all()` is a strict SUPERSET above
    `_INDEX_THRESHOLD_DEFAULT`, and since the miss verdict reads only the
    rank-1 hit, a memory the prefilter would have dropped can take that
    slot and rewrite the verdict.

    Three moving parts, all of them production's:

    - the FTS5 prefilter (`_handlers.load_search_candidates`), capped at
      `_PREFILTER_CAP` rows by query relevance and engaged only above
      `_INDEX_THRESHOLD_DEFAULT` indexed memories;
    - the cap-starvation guard, which dry-runs the authoritative post-cap
      filter (`_filter_candidates`) on a cap-SATURATED slice and reloads
      the full corpus when fewer than `min_survivors` candidates survive.
      The FTS prefilter threads only `scopes` into SQL; the repo/worktree
      auto-scope filter and session-disabled scopes apply post-cap, so on
      a saturated slice they can strip every candidate even though
      in-filter matches exist past the cap. The saturation signal comes
      from the loader (keyed on the INDEX row count) — NOT from
      `len(memories)`, which the loader's per-candidate skips can shrink
      below the cap, masking a saturated slice from the guard;
    - the BM25 corpus-statistics provider, returned non-None only when
      the prefilter actually served the pool. On a query-biased slice,
      document frequencies counted over the pool make the query's own
      discriminative terms look ubiquitous and collapse their IDF toward
      zero (measured at 74x on a 600-memory corpus). When the full corpus
      was loaded — including after a starvation reload — pool statistics
      ARE corpus statistics and the provider stays None so the shipped
      ranking is byte-stable.

    `min_survivors` is the starvation threshold, and the two callers pass
    DIFFERENT values on purpose — each the width of the search it is
    describing:

    - `memory_search` passes the REQUEST's `max_results`. That is the
      guard's whole purpose: a request for 50 results that finds 6
      in-filter survivors in the capped slice must reload, or it serves
      six hits while matching memories sit past the cap — the round-85
      failure this guard exists for. Pinning production to the config
      default instead would re-open that failure for every above-default
      request, so the divergence must not be closed from this end.
    - Both audit producers pass `default_search_width(behavior)`. The
      search they describe is the one the model did NOT make: a
      counterfactual carrying no `max_results` of its own, so its width is
      the config default — put through the SAME `clamp_search_width` a
      request goes through, which is what makes it a width production
      could actually have produced. Passing the raw knob instead put the
      guard outside the range production can reach, in either direction;
      `default_search_width` carries which failure lives at which end.
      There is no request to read on that path — the miss verdict's own
      precondition is that no retrieval happened.

    The widths are not interchangeable, and the difference reaches the
    verdict rather than stopping at pool size, so it is measured rather
    than asserted: `test_min_survivors_width_can_flip_the_probe_verdict`
    in `tests/test_search_prefilter.py` builds a store whose in-filter
    survivors sit between the two thresholds, and the same probe reports
    `ok` at the default width and `miss` at a request width of 25. The
    comparison is a strict `<`, so the asymmetry reverses for a request
    NARROWER than the default: there the audit reloads where production
    keeps the capped slice.

    What that leaves standing: a model habitually passing an
    above-default `max_results` is audited against a narrower
    counterfactual than its own habit. Nothing on the audit path can
    observe that habit, and deriving it from an earlier turn's search
    would make this turn's verdict depend on unrelated history — so it is
    left open rather than guessed.

    Pass the same `scopes` / `excluded_scopes` / `repo_filter` /
    `worktree_filter` that will be handed to `search.search`: the
    corpus-statistics predicate binds them so document frequencies come
    from exactly the collection about to be ranked. Under auto-scope that
    differs sharply from the whole store — a store spanning several
    projects would otherwise price term rarity against memories the
    caller cannot retrieve.

    Returns a pool, not a census — see `SearchPool`.
    """
    # Function-local to break the package cycle (`_handlers` imports the
    # `handlers` package, which imports this module), NOT to defer a
    # cost: importing any `bettermemory` submodule runs the package
    # `__init__`, which pulls `_handlers` in through `.builder` before
    # any caller — including the out-of-process Stop hook — reaches this
    # line, so the statement resolves out of `sys.modules`.
    from .._handlers import load_search_candidates

    excluded = set(excluded_scopes) if excluded_scopes else set()
    memories, prefilter_saturated, prefiltered = load_search_candidates(
        store, query, scopes
    )
    post_cap_filter_active = (
        repo_filter is not None or worktree_filter is not None or bool(excluded)
    )
    if post_cap_filter_active and prefilter_saturated:
        survivors = _filter_candidates(
            memories,
            scopes=scopes,
            excluded_scopes=excluded,
            repo_filter=repo_filter,
            worktree_filter=worktree_filter,
        )
        if len(survivors) < min_survivors:
            memories = store.load_all()
            # The starvation reload replaced the query-biased slice with
            # the whole corpus, so pool-derived statistics are corpus
            # statistics again and the BM25 corpus-IDF lookup must not
            # fire. Clearing the flag here rather than re-deriving it
            # later keeps the claim tied to the assignment that makes it
            # true.
            prefiltered = False

    def _corpus_stats(terms: list[str]) -> CorpusStats | None:
        from .. import index as _index

        def _admit(memory_scopes: list[str], origin: Any) -> bool:
            return candidate_admitted(
                memory_scopes,
                origin,
                scope_filter=set(scopes) if scopes else None,
                excluded=excluded,
                repo_filter=repo_filter,
                worktree_filter=worktree_filter,
            )

        resolved = _index.corpus_document_frequencies(store.root, terms, admit=_admit)
        if resolved is None:
            return None
        size, body_df, scope_df = resolved
        return CorpusStats(size=size, body_df=body_df, scope_df=scope_df)

    return SearchPool(
        memories=memories,
        corpus_stats_provider=_corpus_stats if prefiltered else None,
    )


async def memory_search(
    deps: "ToolHandlers",
    query: str,
    scopes: list[str] | None = None,
    max_results: int | None = None,
    expand_top: bool = False,
    auto_scope: bool = True,
    since_prior_session: bool = False,
    mode: str | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Body of the ``memory_search`` MCP tool — pre-Round-2 was a method
    on ``ToolHandlers``. The signature mirrors the original (minus the
    leading ``self``) so the FastMCP JSON schema is unchanged."""
    # Route capture_origin through the parent ``_handlers`` module so
    # the test suite's monkey-patch (`tests/test_server_origin.py` /
    # `tests/test_server_commit_drift.py`) propagates here too.
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    # The request clamp. Same helper — so the same range — the audit
    # producers reach through `default_search_width`; when the request
    # omits `max_results` the two paths are then reading one expression
    # over one value.
    if max_results is None:
        max_results = deps.config.behavior.default_max_results
    max_results = clamp_search_width(max_results)

    # Resolve search mode: per-call override > config default > "hybrid".
    # Validation happens via the Literal narrowing in search() — any
    # other value will raise ValueError at the dispatch boundary,
    # which the handler propagates to the caller as a tool error.
    resolved_mode = mode or deps.config.behavior.search_mode or "hybrid"
    if resolved_mode not in ("keyword", "bm25", "semantic", "hybrid"):
        raise ValueError(
            f"unknown search mode {resolved_mode!r}; "
            "must be one of: keyword, bm25, semantic, hybrid"
        )
    # Semantic model is resolved only when the mode needs it. The
    # factory returns None when no embedding extra is installed (or
    # when no configured consumer needs the model — see
    # `semantic_setup._semantic_model_or_none`); for `semantic` mode
    # that's a hard error (the caller asked for it specifically), for
    # `hybrid` it's a graceful degrade to keyword+bm25 fusion.
    semantic_model: Any | None = None
    if resolved_mode in ("semantic", "hybrid"):
        semantic_model = deps._semantic_model_factory(deps.config)
        if resolved_mode == "semantic" and semantic_model is None:
            raise ValueError(
                "mode='semantic' requires the embeddings extra and the "
                "config-level opt-in ([behavior] search_mode = 'semantic' "
                "or semantic_dedup = true). "
                "Install with `pip install bettermemory[embeddings]` "
                "or use mode='hybrid' for graceful keyword+bm25 fallback."
            )

    if scopes:
        scopes = [validate_scope(s) for s in scopes]

    # Capture caller origin once: it serves both the auto-scope filter
    # (drop memories from a different repo) and the commit_drift signal
    # on an expanded top hit (count repo-local commits since the last
    # verify of the matching memory). When the caller isn't in a repo,
    # `current_origin.repo` is None — auto-scope becomes a no-op and
    # commit_drift stays silent. Calling capture_origin once keeps the
    # subprocess cost paid in one place and makes the two consumers
    # agree on what "current repo" means for this request.
    current_origin = _h.capture_origin()
    repo_filter: str | None = current_origin.repo if auto_scope else None
    # Worktree filter rides along on the same auto_scope toggle as the
    # repo filter — both are pieces of the same "drop cross-context
    # memories" defaults pass. Disabling auto_scope drops both, so a
    # cross-project search keeps working without needing a second flag.
    worktree_filter: str | None = current_origin.worktree_root if auto_scope else None

    # Prior-session boundary filter (loop-iteration entry path).
    # When set, narrow candidates to memories whose `updated` is
    # at/after the latest event-log timestamp from a session_id
    # other than the recorder's. We use the recorder's session
    # (not state.session_id) for the same reason scope_overview
    # does — the recorder is what stamps events with `session`,
    # so the boundary check has to compare against the same id
    # the events were tagged with. Surface as empty when no prior
    # session exists; callers distinguish "nothing new" from "no
    # baseline" by also calling memory_scope_overview.
    #
    # Resolve the boundary *before* loading candidates so the
    # "no prior session" shortcut can skip the load entirely, and
    # so the `since_prior_session=True` branch below can take the
    # full-corpus `load_all` path (the FTS prefilter's top-50-by-
    # relevance cap silently hides newly-written memories that
    # rank outside the cap — the post-boundary set is bounded by
    # session activity, not corpus size, so the linear scan is
    # cheap regardless of store size).
    prior_boundary = None
    if since_prior_session:
        from ..events import iter_all_events
        from ..health import find_prior_session_boundary

        prior_boundary = find_prior_session_boundary(
            iter_all_events(deps.store.root),
            deps.recorder.session_id,
        )

    # Candidate pool. Two paths:
    #
    # 1. `since_prior_session=True`: bypass the FTS5 prefilter and
    #    take the full corpus via `load_all`, then narrow to the
    #    post-boundary slice. Required for correctness — the
    #    prefilter caps at 50 rows by query relevance, so a newly-
    #    written memory matching the query but ranked outside top-N
    #    would be dropped before the boundary filter ever sees it.
    #    The post-boundary slice is bounded by session activity, so
    #    even on a 10k-memory store only a handful of memories will
    #    pass the `updated > prior_boundary` check.
    # 2. Default: FTS5 candidate prefilter (T3.1 phase B), plus the
    #    cap-starvation guard and the BM25 corpus-statistics wiring that
    #    slice needs — all three resolved by `resolve_search_pool`, the
    #    one implementation the silent-miss audit producers call too, so
    #    the probe cannot rank a different candidate set than the model's
    #    actual retrieval did.
    # The provider stays None for every branch that does not go through
    # the FTS prefilter — including `since_prior_session`, which slices
    # `load_all()` by timestamp. That slice is narrower than the corpus
    # but it is not QUERY-biased, so pool-derived document frequencies
    # stay honest and the corpus lookup would be cost without benefit.
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None = None
    if since_prior_session:
        if prior_boundary is None:
            memories = []
        else:
            # Strict-`>` to match the `curation_counts` `<=` exclusion:
            # the boundary IS the prior session's last event ts (per
            # `find_prior_session_boundary`), so a memory whose `updated`
            # equals it was written by the prior session and belongs to
            # *that* session, not the current-session delta. A naive `>=`
            # double-counts the boundary memory across the two surfaces
            # (memory_search + memory_scope_overview/curation_counts) that
            # the api docs pair together as the "what's new since last
            # session" workflow.
            memories = [m for m in deps.store.load_all() if m.updated > prior_boundary]
    else:
        # The prefilter, the cap-starvation guard and the corpus-stats
        # wiring are one decision — see `resolve_search_pool`, which the
        # two audit producers share so the silent-miss probe ranks this
        # same pool. `min_survivors` is THIS request's `max_results`,
        # already clamped above — the audit producers pass
        # `default_search_width(behavior)` instead, which
        # `resolve_search_pool` records as a deliberate difference (each
        # caller sizes the guard for the search it describes), not a
        # drift to close. Deliberate about the VALUE, never about the
        # RANGE: both sides go through `clamp_search_width`.
        pool = resolve_search_pool(
            deps.store,
            query,
            scopes=scopes,
            excluded_scopes=set(state.disabled_scopes),
            repo_filter=repo_filter,
            worktree_filter=worktree_filter,
            min_survivors=max_results,
        )
        memories = pool.memories
        corpus_stats_provider = pool.corpus_stats_provider

    # Config-driven ranking inputs (usage tallies + the boost/half-life
    # knobs), resolved through the shared helper the web UI's /memories
    # search calls too — see `resolve_ranking_inputs` for what each one
    # does and why the event read is windowed. The event list it returns
    # is reused below for `recent_negative_outcomes`, so enabling either
    # tally adds no extra I/O on a hit-producing search.
    ranking = resolve_ranking_inputs(deps.store.root, memories, deps.config.behavior)
    recent_events: list[dict[str, Any]] | None = ranking.events
    applied_by_id = ranking.applied_by_id
    negative_by_id = ranking.negative_by_id

    hits = run_search(
        memories,
        query,
        applied_by_id=applied_by_id,
        negative_by_id=negative_by_id,
        corroboration_boost=ranking.corroboration_boost,
        scopes=scopes,
        excluded_scopes=set(state.disabled_scopes),
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
        max_results=max_results,
        half_life_days=ranking.half_life_days,
        mode=cast(SearchMode, resolved_mode),
        semantic_model=semantic_model,
        # Browse mode for the natural "what's new since last session"
        # usage: when the caller narrowed to the post-boundary slice
        # and didn't supply a meaningful query, treat all surviving
        # candidates as hits sorted by `updated` desc instead of
        # short-circuiting to an empty list on the stopword check.
        allow_empty_query=since_prior_session,
        corpus_stats_provider=corpus_stats_provider,
    )
    # Pin one `now` for the whole response so the verification verdict
    # is consistent across hits — the alternative (let each helper
    # call utcnow()) could land different status labels on adjacent
    # hits if we crossed a day boundary mid-loop.
    now = utcnow()
    out = [deps.responses.hit_to_dict(h, now=now) for h in hits]

    # Per-hit `commit_drift_count`: cheap repo-aware staleness signal
    # surfaced on every hit (parallel to `path_drift_checked` /
    # `path_drift_missing`) so the model can self-triage which hit to
    # expand without a memory_show round-trip. Two git calls up front
    # (`commit_author_timestamps` + `repo_toplevel`) and one more — the
    # path-filtered log inside `resolve_commit_drift_count` — for each
    # hit that has drift to narrow, so the cost scales with
    # `max_results` rather than being flat; the COST paragraph on
    # `attach_commit_drift_counts` carries the arithmetic. Omitted from
    # the hit JSON when the signal isn't applicable (caller not in a
    # repo, hit's memory from a different repo, hit's memory never
    # verified) rather than emitting a noisy "unknown" branch every
    # consumer would have to filter. The full `commit_drift` block (with
    # status / recommendation) is still attached to the expanded top
    # hit below; the count here is the lightweight triage signal.
    deps.responses.attach_commit_drift_counts(
        out, hits, memories, caller_origin=current_origin
    )

    # Per-hit `recent_negative_outcomes` (T2.3): walk the event log
    # once for the recent window and annotate any hit that was
    # ignored or contradicted AND not since validated. The lookup is
    # bounded — one event-log iteration filtered to the hit ids,
    # then per-id bucketing. The annotation tells the model "this
    # was rejected on date X" so it doesn't keep re-suggesting the
    # same junk; cheap to compute, high signal-to-noise. Skip when
    # the hit list is empty (nothing to annotate). Loading events
    # lazily here rather than at handler construction time keeps
    # the cost off searches that produce no hits. Same window-aware
    # substrate as the endorsement tally above, so the annotation
    # doesn't lose a just-archived negative outcome to a rotation.
    if out:
        if recent_events is None:
            from ..audit import ATTRIBUTION_LOOKBACK_SECONDS
            from ..events import iter_events_window

            recent_events = list(
                iter_events_window(deps.store.root, ATTRIBUTION_LOOKBACK_SECONDS)
            )
        deps.responses.attach_recent_negative_outcomes(
            out, hits, recent_events, now=now
        )

    # Per-hit `depends_on_resolved`: when a hit's memory carries
    # `depends_on`-typed links, inline summaries of the targets so
    # the model gets the dependency chain without a memory_show
    # round-trip. Bounded (max 3 per hit, max 10 total). The
    # MemoryLink type has existed in the schema since 2.x but
    # retrieval has never surfaced it automatically — this closes
    # that gap. Caller can disable via the response builder if a
    # noisy `depends_on` graph would dominate the response, but the
    # caps make the default safe.
    if out:
        # Re-apply the caller's scope filters to the dependency
        # auto-pull. The side-map inside `attach_depends_on_resolved`
        # is built from `memories` (the pre-filter loader output)
        # so cross-repo / session-disabled targets are still
        # resolvable by id — without re-checking here, a hit in a
        # caller-visible scope could pull in a target from a hidden
        # scope, undoing the deliberate scope filter via the
        # dependency edge.
        deps.responses.attach_depends_on_resolved(
            out,
            hits,
            memories,
            caller_origin=current_origin if auto_scope else None,
            excluded_scopes=set(state.disabled_scopes),
            # Pass the store so the helper can targeted-load
            # `depends_on` targets unrelated to the query. The
            # `memories` list is the FTS prefilter set (cap 50, ranked
            # by query relevance), so a depended-on target whose text
            # doesn't match the query is missing from the side-map —
            # the exact case the auto-pull feature exists to handle
            # (B depends_on A precisely because A provides context
            # B's query won't surface on its own). Filter discipline
            # for the targeted-load path is identical to the side-map
            # path: `caller_origin` + `excluded_scopes` re-applied at
            # load time to prevent cross-project / disabled-scope leak.
            store=deps.store,
        )

        # Per-hit `superseded_by` / `contradicts`: activate the
        # supersedes/contradicts MemoryLink edges as trust signals. Like
        # depends_on_resolved this is post-rank and additive (it never
        # reorders or drops a hit), with the same scope/origin re-filter
        # so a link can't leak a hidden-scope memory. Inbound edges come
        # from the links index, with a candidate-scan fallback whenever
        # the index can't answer (absent / empty / rebuild-pending /
        # unreadable — the states the candidate loader routed to
        # load_all).
        deps.responses.attach_link_annotations(
            out,
            hits,
            memories,
            store=deps.store,
            caller_origin=current_origin if auto_scope else None,
            excluded_scopes=set(state.disabled_scopes),
        )

    # Optional auto-expansion of the top hit. Conservative: only fires
    # when the top hit clearly wins ("high" relevance) so the model
    # doesn't get hosed with full bodies it didn't really need.
    # Path-drift runs against the expanded body — if we're already
    # paying the load cost, surfacing drift here saves a memory_show
    # round-trip when the model needs to act on it. Commit-drift is
    # bundled here too: same logic, same one-call-per-search budget,
    # only emitted when the caller's repo matches the memory's origin.
    expanded_id: str | None = None
    expanded_drift_missing = 0
    expanded_commit_drift_status: str | None = None
    expanded_commits_since_verify: int | None = None
    if expand_top and out and out[0]["relevance"] == "high":
        try:
            memory = deps.store.load_one(hits[0].id)
        except (MemoryNotFoundError, TombstonedError):
            # Race: memory was tombstoned between search and show.
            # Drop the body silently, the snippet still got returned.
            pass
        except OSError:
            # Transient IO error reading the top hit's body (e.g. the
            # backing file vanished mid-flight, a flaky network mount, or
            # a transient EIO). The body expansion is a best-effort
            # enrichment — skip the inline body but still return the
            # ranked hits the caller already has, rather than aborting the
            # whole search on one unreadable body.
            pass
        else:
            out[0]["body"] = memory.body
            drift = detect_path_drift(
                memory.body,
                verified_paths=memory.verified_paths,
                absent_paths=memory.verified_absent_paths,
            )
            if drift.has_drift or drift.verified or drift.expected_absent:
                out[0]["path_drift"] = drift.to_dict()
            expanded_drift_missing = len(drift.missing)
            commit_drift = compute_commit_drift(
                memory.last_verified_at,
                memory.origin.repo if memory.origin else None,
                caller_origin=current_origin,
                verified_paths=memory.verified_paths,
                body=memory.body,
            )
            commit_drift_count_for_verdict: int | None = None
            if commit_drift is not None:
                out[0]["commit_drift"] = commit_drift.to_dict()
                expanded_commit_drift_status = commit_drift.status
                expanded_commits_since_verify = commit_drift.commits_since_verify
                commit_drift_count_for_verdict = commit_drift.commits_since_verify
                # Overwrite the cheap per-hit `commit_drift_count` that
                # `attach_commit_drift_counts` stamped from the pre-expansion
                # bisect, so a single response never carries two
                # inconsistent counts on its top hit. Both paths now share
                # the author-timestamp + bisect_right source, so they agree
                # on the unfiltered count; this keeps them aligned on the
                # verified-paths-narrowed value too (compute_commit_drift
                # applies the path filter, which the per-hit pass also does
                # — but pinning the field to the block that also drives
                # `commit_drift`/`staleness_verdict` here makes the
                # top-hit triple provably consistent).
                out[0]["commit_drift_count"] = commit_drift.commits_since_verify
            # Re-derive the top hit's verdict from the just-computed
            # body-level signals — the verdict that landed via
            # `hit_to_dict` was based on `path_drift_missing` from
            # the search index (unloaded body) and may have skipped
            # claims surfaced by the actual body-level detection.
            top_verification = compute_verification_status(
                memory.last_verified_at,
                now=now,
                stale_after_days=deps.config.behavior.verification_stale_days,
            )
            out[0]["staleness_verdict"] = compute_staleness_verdict(
                verification=top_verification,
                path_drift_missing=expanded_drift_missing,
                commit_drift_count=commit_drift_count_for_verdict,
            )
            expanded_id = memory.id

    # Issue use-tokens after every other field is in place so the
    # bookkeeping reflects the canonical response shape the model
    # is about to act on.
    _attach_use_tokens(out, state)

    from .._response import isoformat_optional

    # Shadow-label calibration features, event-log only (the response
    # dicts deliberately do NOT carry them — a surfaced v2 label would
    # nudge live model behavior before the calibration data justifies
    # a flip). `scores` / `match_counts` / `query_unique` are the RAW
    # features, so any future labeling formula — not just the bundled
    # v2 — can be replayed over this event history; that's the data-
    # layer fix for "the threshold sweep can narrow but never widen".
    # `query_unique` is per-search (every hit shares the denominator).
    _query_unique = hits[0].query_unique if hits else 0
    deps.recorder.record(
        "search",
        query=query,
        scopes_filter=scopes,
        max_results=max_results,
        returned=[h["id"] for h in out],
        relevance=[h["relevance"] for h in out],
        relevance_v2=[
            _relevance_label_v2(len(h["match_terms"]), _query_unique) for h in out
        ],
        scores=[h["score"] for h in out],
        match_counts=[len(h["match_terms"]) for h in out],
        query_unique=_query_unique,
        expand_top=expand_top,
        expanded_id=expanded_id,
        expanded_drift_missing=expanded_drift_missing,
        expanded_commit_drift_status=expanded_commit_drift_status,
        expanded_commits_since_verify=expanded_commits_since_verify,
        auto_scope=auto_scope,
        repo_filter=repo_filter,
        since_prior_session=since_prior_session,
        prior_session_boundary=isoformat_optional(prior_boundary),
    )
    return out


__all__ = [
    "DESC_MEMORY_SEARCH",
    "MAX_SEARCH_RESULTS",
    "RankingInputs",
    "SearchPool",
    "clamp_search_width",
    "default_search_width",
    "memory_search",
    "ranking_events_window_seconds",
    "resolve_ranking_inputs",
    "resolve_search_pool",
]
