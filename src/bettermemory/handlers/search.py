"""memory_search MCP tool: handler implementation and DESC.

The handler is the busiest of the nine tools: it issues use-tokens,
attaches per-hit drift signals, optionally expands the top hit, and
records its own event with a generous payload shape so the eval CLI
can rebuild what the model saw.

Description-edit history:

- H8 (Round 2): the "before you call this" guidance was buried mid-string.
  Hoisted to a two-line lead block so it's the first thing the model
  reads: opt-in retrieval and the transparency requirement land before
  any parameter detail.
- H9: `query` stopped being described as "free text". Every line above it
  told the caller how to READ a result; nothing told it how to WRITE the
  one input that decides whether a result exists. So the cue names the
  lever the measurement moved (vocabulary the memory would literally
  contain) and not the one it showed does nothing ("use keywords, not
  questions"), and it names re-querying, since a weak first hit is the
  caller's only signal that its wording missed.

  The measurement itself lives here rather than in the description: it is
  only honest with a caveat attached, and a per-turn surface cannot
  afford the caveat. Committed evidence is
  `bench/retrieval/results/v2-unpadded-2026-07-26.json`: a 180-document
  synthetic corpus, 20 blind-authored questions per probe. The lexical
  arm retrieves 35% recall@1 on questions as asked and 80% re-queried in
  nouns the documents actually contain; the control arm (question words
  stripped, content words kept) also scores 35%, identical to asking,
  because the ranker already strips stopwords. Only the within-corpus
  contrast transfers: the retrieval-bench notes establish that this
  corpus is easier than a real store, so these are not a store's rates.
  The 10%→65% pair this block used to cite had no committed artifact
  behind it and that bench retired it.

  Same pass retracted "`hybrid` for paraphrase recall" from `mode`:
  hybrid is RRF over keyword and BM25, both lexical, so that line
  promised the caller a capability the ranker does not have (and, since
  4.0.0, deliberately does not carry).

  Paid for in the same string rather than by raising the ratchet in
  `test_default_on_descriptions_fit_budget` (since replaced by
  `test_tool_surface.py::test_session_surface_under_budget`):
  `since_prior_session` gave back the sentence deriving why its boundary
  is strict-`>`. That is rationale for a decision the caller cannot
  influence, and it was resident on every turn. Its guard is
  `test_prompts.py::test_api_md_since_prior_session_strict_after`, which
  pins the wording in `docs/api.md`. The field's actionable semantics
  ("strictly after", the /loop guidance, empty-vs-no-baseline) stayed in
  the description until the 9.0 port cut it to "this session's changes",
  so the strict boundary is now stated in `docs/api.md` and not in the
  description.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, NamedTuple, cast

from ..models import utcnow, validate_scope
from ..search import (
    CorpusStats,
    SearchMode,
    candidate_admitted,
    _filter_candidates,
    _relevance_label_v2,
    search as run_search,
    top_hit_leads_runner_up,
)
from ..store import MemoryNotFoundError, Store, TombstonedError
from ..verdict import memory_verdict
from ._shared import Context, _advance_turn, _attach_use_tokens, caller_anchoring

if TYPE_CHECKING:
    from datetime import datetime

    from .._handlers import ToolHandlers
    from ..admission import Admission
    from ..config import BehaviorConfig
    from ..models import MemoryHit
    from ..origin import Origin
    from ..verify import Anchoring


DESC_MEMORY_SEARCH = (
    "Rank stored memories for `query`; hits carry a snippet, relevance, "
    "use_token and staleness_verdict. Auto-scoped to the caller's project "
    "(auto_scope=False: cross-project). `scopes` keeps tags, "
    "`exclude_scopes` drops them; `since_prior_session`: this session's "
    "changes; `expand_top`: the top hit's body and drift; `client`, "
    "`model`: one writer."
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


def default_search_width(behavior: BehaviorConfig) -> int:
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
    admission: Admission | None = None,
    client_filter: str | None = None,
    model_filter: str | None = None,
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
      The FTS prefilter threads only `scopes` into SQL; the membership
      admission and session-disabled scopes apply post-cap, so on
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

    Pass the same `scopes` / `excluded_scopes` / `admission` /
    `client_filter` / `model_filter` that will be handed to
    `search.search`: the corpus-statistics predicate binds them so
    document frequencies come from exactly the collection about to be
    ranked. `admission` is the request's (`admission.Admission`, built
    once from the caller's resolution): `memory_search` passes the
    SEARCH_AUTO_SCOPE or SEARCH_UNSCOPED surface, the hooks' probe the
    POOL_PREFILTER surface, the review's related set REVIEW_STRATA (design
    3.5); None only for offline callers that rank a whole store. The two
    audit producers leave the actor filters at None and must keep doing
    so: the probe measures what PRODUCTION retrieval would have surfaced,
    and production retrieval never carries one. Under auto-scope that
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
        store, query, scopes, client=client_filter, model=model_filter
    )
    memories, prefiltered = _guard_cap_starvation(
        store,
        memories,
        prefilter_saturated,
        prefiltered,
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client_filter=client_filter,
        model_filter=model_filter,
        min_survivors=min_survivors,
    )
    _corpus_stats = _corpus_stats_provider(
        store,
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client_filter=client_filter,
        model_filter=model_filter,
    )
    return SearchPool(
        memories=memories,
        corpus_stats_provider=_corpus_stats if prefiltered else None,
    )


def _guard_cap_starvation(
    store: Store,
    memories: list[Any],
    prefilter_saturated: bool,
    prefiltered: bool,
    *,
    scopes: list[str] | None,
    excluded: set[str],
    admission: Admission | None,
    client_filter: str | None,
    model_filter: str | None,
    min_survivors: int,
) -> tuple[list[Any], bool]:
    """Take the loaded pool and its flags; return it (the corpus if starved), prefiltered."""
    # The actor filters are absent here on purpose. They are the one
    # class of filter the prefilter SQL applies itself, so a saturated
    # slice is already 50 rows that PASS them and there is no post-cap
    # attrition for the guard to catch — counting them would arm a
    # starvation reload that can only ever reload the same set.
    post_cap_filter_active = admission is not None or bool(excluded)
    if post_cap_filter_active and prefilter_saturated:
        survivors = _filter_candidates(
            memories,
            scopes=scopes,
            excluded_scopes=excluded,
            admission=admission,
            client_filter=client_filter,
            model_filter=model_filter,
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
    return memories, prefiltered


def _corpus_stats_provider(
    store: Store,
    *,
    scopes: list[str] | None,
    excluded: set[str],
    admission: Admission | None,
    client_filter: str | None,
    model_filter: str | None,
) -> Callable[[list[str]], CorpusStats | None]:
    """Take the pool's filters; return the BM25 corpus-statistics lookup bound to them."""

    def _corpus_stats(terms: list[str]) -> CorpusStats | None:
        def _admit(memory_scopes: list[str], home: str | None, actor: Any) -> bool:
            return candidate_admitted(
                memory_scopes,
                home,
                actor,
                scope_filter=set(scopes) if scopes else None,
                excluded=excluded,
                admission=admission,
                client_filter=client_filter,
                model_filter=model_filter,
            )

        # Through the store's document frequency memo, which answers as
        # the uncached read does and reads again after a memory changes.
        resolved = store.document_frequencies(terms, admit=_admit, memoised=True)
        if resolved is None:
            return None
        size, body_df, scope_df = resolved
        return CorpusStats(size=size, body_df=body_df, scope_df=scope_df)

    return _corpus_stats


async def memory_search(
    deps: ToolHandlers,
    query: str,
    scopes: list[str] | None = None,
    exclude_scopes: list[str] | None = None,
    max_results: int | None = None,
    expand_top: bool = False,
    auto_scope: bool = True,
    since_prior_session: bool = False,
    mode: str | None = None,
    client: str | None = None,
    model: str | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Body of the ``memory_search`` MCP tool — pre-Round-2 was a method
    on ``ToolHandlers``. The signature mirrors the original (minus the
    leading ``self``) so the served JSON schema is unchanged."""
    # Route capture_origin through the parent ``_handlers`` module so
    # the test suite's monkey-patch (`tests/test_server_origin.py` /
    # `tests/test_server_commit_drift.py`) propagates here too.
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    max_results, resolved_mode = _request_width_and_mode(deps, max_results, mode)
    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    if exclude_scopes:
        exclude_scopes = [validate_scope(s) for s in exclude_scopes]
    # The scopes this search must not see: the session's disabled set
    # (`memory_scope_disable`) plus the caller's own `exclude_scopes`.
    # One set for every consumer below, so a memory the caller excluded
    # cannot come back through the pool, the ranking, or a link edge.
    excluded = set(state.disabled_scopes) | set(exclude_scopes or ())

    # The caller is resolved once (`_shared.caller_anchoring`, design 3.1)
    # and its admission built once (`admission.Admission`): the search's
    # own surface (design 3.5), SEARCH_AUTO_SCOPE, or SEARCH_UNSCOPED when
    # the caller turned auto-scope off (every project but across a
    # boundary, E34 d), decides the pool, the ranked set and the document
    # frequencies; the LINKS surface decides what a hit's links pull in and
    # whose commits its drift count reads. The origin is the caller's
    # directory, which the commit_drift signal counts commits in.
    from ..admission import Admission
    from ..membership import LINKS, SEARCH_AUTO_SCOPE, SEARCH_UNSCOPED

    current_origin = _h.capture_origin()
    anchoring, caller = caller_anchoring(deps.store, deps.config)
    surface = SEARCH_AUTO_SCOPE if auto_scope else SEARCH_UNSCOPED
    admission = Admission.of(surface, caller)
    links = admission.on(LINKS)

    prior_boundary = _prior_session_boundary(deps, since_prior_session)
    memories, corpus_stats_provider = _candidate_pool(
        deps,
        query,
        since_prior_session=since_prior_session,
        prior_boundary=prior_boundary,
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client=client,
        model=model,
        max_results=max_results,
    )
    hits, matched_leg = _rank_pool(
        deps,
        memories,
        query,
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client=client,
        model=model,
        max_results=max_results,
        resolved_mode=resolved_mode,
        since_prior_session=since_prior_session,
        corpus_stats_provider=corpus_stats_provider,
        anchoring=anchoring,
    )
    # Pin one `now` for the whole response so the verification verdict
    # is consistent across hits — the alternative (let each helper
    # call utcnow()) could land different status labels on adjacent
    # hits if we crossed a day boundary mid-loop.
    now = utcnow()
    out = [deps.responses.hit_to_dict(h, now=now) for h in hits]
    _attach_hit_signals(
        deps,
        out,
        hits,
        memories,
        matched_leg,
        current_origin=current_origin,
        links=links,
    )
    _attach_hit_relations(
        deps, out, hits, memories, links=links, excluded=excluded, now=now
    )
    expansion = _expand_top_hit(
        deps,
        out,
        hits,
        expand_top,
        current_origin=current_origin,
        now=now,
        anchoring=anchoring,
        links=links,
    )

    # Issue use-tokens after every other field is in place so the
    # bookkeeping reflects the canonical response shape the model
    # is about to act on.
    _attach_use_tokens(out, state)

    _record_search(
        deps,
        out,
        hits,
        query=query,
        scopes=scopes,
        exclude_scopes=exclude_scopes,
        client=client,
        model=model,
        max_results=max_results,
        expand_top=expand_top,
        expansion=expansion,
        auto_scope=auto_scope,
        surface=surface,
        admission=admission,
        since_prior_session=since_prior_session,
        prior_boundary=prior_boundary,
    )
    return out


class _TopExpansion(NamedTuple):
    """What the top hit's expansion produced, for the `search` event."""

    expanded_id: str | None
    drift_missing: int
    commit_drift_status: str | None
    commits_since_verify: int | None


def _request_width_and_mode(
    deps: ToolHandlers, max_results: int | None, mode: str | None
) -> tuple[int, str]:
    """Take the request's width and mode; return the clamped width and the mode."""
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
    if resolved_mode not in ("keyword", "bm25", "hybrid"):
        raise ValueError(
            f"unknown search mode {resolved_mode!r}; "
            "must be one of: keyword, bm25, hybrid"
        )
    return max_results, resolved_mode


def _prior_session_boundary(
    deps: ToolHandlers, since_prior_session: bool
) -> datetime | None:
    """Take the request's flag; return the prior session's boundary, or None."""
    # Prior-session boundary filter (loop-iteration entry path).
    # When set, narrow candidates to memories whose `updated` is
    # at/after the latest event-log timestamp from a session_id
    # other than the recorder's. We use the recorder's session
    # (not state.session_id) because the recorder is what stamps
    # events with `session`, so the boundary check has to compare
    # against the same id the events were tagged with. Surface as
    # empty when no prior session exists.
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
        from ..health import find_prior_session_boundary

        prior_boundary = find_prior_session_boundary(
            deps.store.iter_events(),
            deps.recorder.session_for_request(),
        )
    return prior_boundary


def _candidate_pool(
    deps: ToolHandlers,
    query: str,
    *,
    since_prior_session: bool,
    prior_boundary: datetime | None,
    scopes: list[str] | None,
    excluded: set[str],
    admission: Admission,
    client: str | None,
    model: str | None,
    max_results: int,
) -> tuple[list[Any], Callable[[list[str]], CorpusStats | None] | None]:
    """Take the request; return the candidate pool and its corpus-statistics lookup."""
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
            # Strict `>`: the boundary IS the prior session's last event
            # ts (per `find_prior_session_boundary`), so a memory whose
            # `updated` equals it was written by the prior session and
            # belongs to *that* session, not the current-session delta.
            # A naive `>=` would count the boundary memory in both
            # sessions' deltas.
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
            excluded_scopes=excluded,
            admission=admission,
            client_filter=client,
            model_filter=model,
            min_survivors=max_results,
        )
        memories = pool.memories
        corpus_stats_provider = pool.corpus_stats_provider
    return memories, corpus_stats_provider


def _rank_pool(
    deps: ToolHandlers,
    memories: list[Any],
    query: str,
    *,
    scopes: list[str] | None,
    excluded: set[str],
    admission: Admission,
    client: str | None,
    model: str | None,
    max_results: int,
    resolved_mode: str,
    since_prior_session: bool,
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None,
    anchoring: Anchoring,
) -> tuple[list[MemoryHit], dict[str, str]]:
    """Take the pool and the request; return the ranked hits and each hit's leg."""
    # Filled by `run_search` with `{memory_id: lexical|expansion}` for
    # the returned hits — see `search.search`'s `matched_leg_out` note for
    # why the leg travels as an out-parameter rather than a `MemoryHit`
    # field.
    matched_leg: dict[str, str] = {}
    hits = run_search(
        memories,
        query,
        scopes=scopes,
        excluded_scopes=excluded,
        admission=admission,
        client_filter=client,
        model_filter=model,
        max_results=max_results,
        half_life_days=deps.config.behavior.recency_boost_half_life_days,
        mode=cast(SearchMode, resolved_mode),
        # Browse mode for the natural "what's new since last session"
        # usage: when the caller narrowed to the post-boundary slice
        # and didn't supply a meaningful query, treat all surviving
        # candidates as hits sorted by `updated` desc instead of
        # short-circuiting to an empty list on the stopword check.
        allow_empty_query=since_prior_session,
        corpus_stats_provider=corpus_stats_provider,
        matched_leg_out=matched_leg,
        # The expansion leg is a bench instrument, not a shipped knob.
        rescue_expansion=False,
        conversational=deps.config.behavior.conversational,
        # Every hit's path drift reads a homed record where memory_show
        # reads it (design 3.6), so a non-top hit and memory_show agree.
        anchoring=anchoring,
    )
    return hits, matched_leg


def _attach_hit_signals(
    deps: ToolHandlers,
    out: list[dict[str, Any]],
    hits: list[MemoryHit],
    memories: list[Any],
    matched_leg: dict[str, str],
    *,
    current_origin: Origin,
    links: Admission,
) -> None:
    """Take the response rows; stamp each hit's leg, commit drift and provenance."""
    # `matched_leg`: which ranker surfaced each hit. Additive and
    # OMITTED when there is no leg (browse mode ranks nothing), so a
    # consumer pinned to the existing key set never meets a surprise
    # key with a meaningless value. This is the field that makes
    # `relevance` readable rather than misleading: `relevance` is
    # lexical coverage, which is 0 for a pure-paraphrase hit, so a
    # `semantic` leg reading "low" is the expected shape of a match by
    # meaning and not the noise the description used to call it.
    for row in out:
        leg = matched_leg.get(row["id"])
        if leg:
            row["matched_leg"] = leg

    # Per-hit `commit_drift_count`: cheap repo-aware staleness signal
    # surfaced on every hit (parallel to `path_drift_checked` /
    # `path_drift_missing`) so the model can self-triage which hit to
    # expand without a memory_show round-trip. Cold, one git call up
    # front (`commit_author_timestamps`; the repo root and the head come
    # from the repository's files) and one more (the path-filtered log
    # inside `resolve_commit_drift_count`) for each hit that has drift
    # to narrow, so the cold cost scales with `max_results`; warm, the
    # memoised resolutions answer with no process. The COST paragraph on
    # `attach_commit_drift_counts` carries the arithmetic. Omitted from
    # the hit JSON when the signal isn't applicable (caller not in a
    # repo, hit's memory from a different repo, hit's memory never
    # verified) rather than emitting a noisy "unknown" branch every
    # consumer would have to filter. The full `commit_drift` block (with
    # status / recommendation) is still attached to the expanded top
    # hit below; the count here is the lightweight triage signal.
    deps.responses.attach_commit_drift_counts(
        out, hits, memories, caller_origin=current_origin, admission=links
    )
    # Per-hit `provenance` (schema v7): how the record entered the store,
    # read from the index in one batched query. Omitted while the index
    # has not classified the row; never read from the file.
    deps.responses.attach_provenance(out, store=deps.store)


def _attach_hit_relations(
    deps: ToolHandlers,
    out: list[dict[str, Any]],
    hits: list[MemoryHit],
    memories: list[Any],
    *,
    links: Admission,
    excluded: set[str],
    now: datetime,
) -> None:
    """Take the response rows; attach recent negative outcomes and link relations."""
    # Per-hit `recent_negative_outcomes` (T2.3): walk the event log
    # once for the recent window and annotate any hit that was
    # ignored or contradicted AND not since validated. The lookup is
    # bounded — one event-log iteration filtered to the hit ids,
    # then per-id bucketing. The annotation tells the model "this
    # was rejected on date X" so it doesn't keep re-suggesting the
    # same junk; cheap to compute, high signal-to-noise. Skip when
    # the hit list is empty (nothing to annotate). Loading events
    # lazily here rather than at handler construction time keeps
    # the cost off searches that produce no hits. Window-aware read
    # (`iter_events_window`), so the annotation doesn't lose a
    # just-archived negative outcome to a rotation. This is the one
    # event read a search pays, and the store's event window memo serves
    # it: the lookback only moves forward, so a search reads the rows
    # appended since the last one rather than the whole log
    # (`Store.events_since(..., memoised=True)`).
    if out:
        from datetime import timedelta

        from ..audit import ATTRIBUTION_LOOKBACK_SECONDS

        recent_events = list(
            deps.store.events_since(
                now - timedelta(seconds=ATTRIBUTION_LOOKBACK_SECONDS), memoised=True
            )
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
            admission=links,
            excluded_scopes=excluded,
            # Pass the store so the helper can targeted-load
            # `depends_on` targets unrelated to the query. The
            # `memories` list is the FTS prefilter set (cap 50, ranked
            # by query relevance), so a depended-on target whose text
            # doesn't match the query is missing from the side-map —
            # the exact case the auto-pull feature exists to handle
            # (B depends_on A precisely because A provides context
            # B's query won't surface on its own). Filter discipline
            # for the targeted-load path is identical to the side-map
            # path: the LINKS admission + `excluded_scopes` re-applied
            # at load time to prevent cross-project / disabled-scope leak.
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
            admission=links,
            excluded_scopes=excluded,
        )


def _expand_top_hit(
    deps: ToolHandlers,
    out: list[dict[str, Any]],
    hits: list[MemoryHit],
    expand_top: bool,
    *,
    current_origin: Origin,
    now: datetime,
    anchoring: Anchoring,
    links: Admission,
) -> _TopExpansion:
    """Take the response rows; expand the top hit in place and return what it did."""
    # Optional auto-expansion of the top hit. Conservative: only fires
    # when the top hit clearly wins, so the model doesn't get hosed with
    # full bodies it didn't really need.
    # Path-drift runs against the expanded body — if we're already
    # paying the load cost, surfacing drift here saves a memory_show
    # round-trip when the model needs to act on it. Commit-drift is
    # bundled here too: same logic, same one-call-per-search budget,
    # only emitted when the caller's repo matches the memory's origin.
    #
    # "Clearly wins" used to mean `relevance == "high"` ALONE, and that
    # single string is why the semantic ranker could not reach this
    # affordance: `relevance` is lexical coverage and a pure-paraphrase
    # hit carries `match_terms=[]`, so it is "low" by construction no
    # matter how decisively it won the ranking. The gate now takes
    # either kind of evidence — the coverage label, or a rank-1 hit that
    # leads the runner-up by more than a rank slot's worth of score
    # (`top_hit_leads_runner_up`, derived from RRF's own spacing).
    #
    # It is a UNION, not a replacement, on purpose: dropping the label
    # arm would stop expanding some hits that expand today, and this
    # project subtracts on telemetry, never on taste — there is no
    # measurement saying those callers were over-served. Measured on the
    # dogfood store's logged searches, the label arm fires on 25% of
    # result sets and the union on 37%; the margin arm alone would have
    # fired on 16%, so replacing rather than widening would have been a
    # net narrowing dressed up as a fix.
    #
    # A sole hit does NOT qualify on the margin arm: with no runner-up
    # there is nothing to dominate, and "clear winner of a field of one"
    # is an absence of evidence, not evidence.
    expanded_id: str | None = None
    # A TELEMETRY field on the `search` event: the full-set count, so the
    # event log keeps measuring how much path evidence the detector
    # produced. The verdict escalates on the claim-anchored subset alone
    # (`verdict.memory_verdict`); collapsing the two would either
    # re-broaden the verdict or silently rewrite a logged series.
    expanded_drift_missing = 0
    expanded_commit_drift_status: str | None = None
    expanded_commits_since_verify: int | None = None
    expand_top_fires = bool(expand_top and out) and (
        out[0]["relevance"] == "high"
        or (len(out) > 1 and top_hit_leads_runner_up(out[0]["score"], out[1]["score"]))
    )
    if expand_top_fires:
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
            # The top hit's verdict is `memory_show`'s, through the one
            # verdict both surfaces read (`verdict.memory_verdict`): path
            # drift over the full body, the calendar status at this
            # search's `now`, and the commit leg read through the per-hit
            # memo `attach_commit_drift_counts` fills, so a top hit the
            # memo holds forks no git process here either. `claims` and
            # the body feed the leg exactly as on memory_show's call: a
            # count computed without the memory's claims is a different
            # policy (any-touch), and the loudest retrieval surface would
            # disagree with memory_show on the same memory in the same
            # turn — see `resolve_commit_drift_count`'s docstring.
            shown = memory_verdict(
                memory,
                caller_origin=(
                    current_origin if links.drift_counts(memory.id) else None
                ),
                now=now,
                stale_after_days=deps.config.behavior.verification_stale_days,
                anchoring=anchoring,
                admission=links,
            )
            drift = shown.path_drift
            if drift.has_drift or drift.verified or drift.expected_absent:
                out[0]["path_drift"] = drift.to_dict()
            expanded_drift_missing = len(drift.missing)
            commit_drift = shown.commit_drift
            if commit_drift is not None:
                out[0]["commit_drift"] = commit_drift.to_dict()
                expanded_commit_drift_status = commit_drift.status
                expanded_commits_since_verify = commit_drift.commits_since_verify
                # Overwrite the cheap per-hit `commit_drift_count` that
                # `attach_commit_drift_counts` stamped from the pre-expansion
                # bisect, so a single response never carries two
                # inconsistent counts on its top hit. Both read the same
                # memoised resolution, so they agree; pinning the field to
                # the block that also drives `commit_drift` /
                # `staleness_verdict` here makes the top-hit triple
                # provably consistent.
                out[0]["commit_drift_count"] = commit_drift.commits_since_verify
            # Re-derive the top hit's verdict from the just-computed
            # body-level signals — the verdict that landed via
            # `hit_to_dict` was based on `path_drift_missing` from
            # the search index (unloaded body) and may have skipped
            # claims surfaced by the actual body-level detection.
            #
            # Unless the trust rule already demoted the hit: a `synced`
            # record whose stamp this host never made reads "remote" and
            # spot_check_required (`ResponseBuilder.apply_trust`, applied
            # by `attach_provenance` above), and the file's stamp must
            # not put a verdict back that the rule took away.
            # Nor when the rule could not run at all (`trust_unavailable`,
            # an unreadable index): that demotion is for a stamp of
            # unknown origin, and the file's stamp is exactly what it
            # declines to trust.
            if out[0].get("verification", {}).get("status") != "remote" and not out[
                0
            ].get("trust_unavailable"):
                # Claim-anchored subset, matching every other verdict
                # site. The full report — prose misses included — is
                # already in `out[0]["path_drift"]` above, so nothing the
                # caller could see before disappeared; only the tier
                # narrowed. See `verdict_from_signals`.
                out[0]["staleness_verdict"] = shown.staleness_verdict
            expanded_id = memory.id
    return _TopExpansion(
        expanded_id=expanded_id,
        drift_missing=expanded_drift_missing,
        commit_drift_status=expanded_commit_drift_status,
        commits_since_verify=expanded_commits_since_verify,
    )


def _record_search(
    deps: ToolHandlers,
    out: list[dict[str, Any]],
    hits: list[MemoryHit],
    *,
    query: str,
    scopes: list[str] | None,
    exclude_scopes: list[str] | None,
    client: str | None,
    model: str | None,
    max_results: int,
    expand_top: bool,
    expansion: _TopExpansion,
    auto_scope: bool,
    surface: str,
    admission: Admission,
    since_prior_session: bool,
    prior_boundary: datetime | None,
) -> None:
    """Take the served rows and the request; record the `search` event row."""
    from .._response import isoformat_optional
    from ..admission import admitted_nothing

    # Shadow-label calibration features, event-log only (the response
    # dicts deliberately do NOT carry them — a surfaced v2 label would
    # nudge live model behavior before the calibration data justifies
    # a flip). `scores` / `match_counts` / `query_unique` are the RAW
    # features, so any future labeling formula — not just the bundled
    # v2 — can be replayed over this event history; that's the data-
    # layer fix for "the threshold sweep can narrow but never widen".
    # `query_unique` is per-search (every hit shares the denominator).
    _query_unique = hits[0].query_unique if hits else 0
    # The caller's own exclusions ride the event only when it passed any
    # (the recorder writes every field it is given, `None` included);
    # the session's disabled scopes are recorded by their own events.
    search_extra: dict[str, Any] = {}
    if exclude_scopes:
        search_extra["exclude_scopes"] = list(exclude_scopes)
    deps.recorder.record(
        "search",
        query=query,
        scopes_filter=scopes,
        # Recorded so a replay can tell an empty result that means
        # 'nothing matched' from one that means 'nothing this writer
        # wrote matched'. Omitted from the payload when unset, like
        # every other optional field the recorder takes.
        client_filter=client,
        model_filter=model,
        max_results=max_results,
        returned=[h["id"] for h in out],
        relevance=[h["relevance"] for h in out],
        relevance_v2=[
            _relevance_label_v2(len(h["match_terms"]), _query_unique) for h in out
        ],
        scores=[h["score"] for h in out],
        match_counts=[len(h["match_terms"]) for h in out],
        # Logged for the same reason as the raw features above, and for
        # one specific one: the 2026-07-30 attempt to re-cut the label
        # for pure-semantic hits could not be evaluated because nothing
        # in the event history recorded whether a hit had a semantic leg
        # at all (`search._relevance_label` carries the full negative
        # result). A replay can now separate the populations.
        matched_leg=[h.get("matched_leg") for h in out],
        query_unique=_query_unique,
        expand_top=expand_top,
        expanded_id=expansion.expanded_id,
        expanded_drift_missing=expansion.drift_missing,
        expanded_commit_drift_status=expansion.commit_drift_status,
        expanded_commits_since_verify=expansion.commits_since_verify,
        auto_scope=auto_scope,
        membership_surface=surface,
        **(
            {"membership": admitted_nothing(admission.nothing)}
            if admission.nothing is not None
            else {}
        ),
        since_prior_session=since_prior_session,
        prior_session_boundary=isoformat_optional(prior_boundary),
        **search_extra,
    )


__all__ = [
    "DESC_MEMORY_SEARCH",
    "MAX_SEARCH_RESULTS",
    "SearchPool",
    "clamp_search_width",
    "default_search_width",
    "memory_search",
    "resolve_search_pool",
]
