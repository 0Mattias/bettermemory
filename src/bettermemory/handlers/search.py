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
from .. import git_read_budget, identity

import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, cast

from ..models import utcnow, validate_scope
from ..search import (
    CorpusStats,
    SearchMode,
    candidate_admitted,
    _relevance_label_v2,
    search as run_search,
    top_hit_leads_runner_up,
)
from ..store import MemoryNotFoundError, Store, TombstonedError
from ..verdict import memory_verdict
from ..commit_legs import UNCHECKED_RECOMMENDATIONS
from ..verify_basis import UNREAD_RECOMMENDATIONS
from ._shared import Context, _advance_turn, _attach_use_tokens, caller_anchoring

if TYPE_CHECKING:
    from datetime import datetime

    from .._handlers import ToolHandlers
    from ..admission import Admission
    from ..models import MemoryHit
    from ..origin import Origin
    from ..verdict import MemoryVerdict
    from ..verify import Anchoring


DESC_MEMORY_SEARCH = (
    "Rank stored memories for `query`; hits carry a snippet, relevance, "
    "use_token and staleness_verdict. Auto-scoped to the caller's project "
    "(auto_scope=False: cross-project). `scopes` keeps tags, "
    "`exclude_scopes` drops them; `since_prior_session`: this session's "
    "changes; `expand_top`: the top hit's body and drift; `client`, "
    "`model`: one writer."
)


class Hydrator:
    """Reads the records a search returns in full, once each.

    A pool's candidates are read as a ranking uses them (`Store.light_many`:
    one statement, no signed-row fields). A hit's drift report reads the
    signed row's path targets and its origin the native origin, so the
    few records a search returns are loaded again whole (`Store.load_many`)
    before their hits are built (`search.search`'s `hydrate`). `full`
    keeps what was loaded, by id."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.full: dict[str, Any] = {}

    def __call__(self, memories: Sequence[Any]) -> list[Any]:
        missing = [m.id for m in memories if m.id not in self.full]
        if missing:
            for whole in self._store.load_many(missing):
                self.full[whole.id] = whole
        return [self.full.get(m.id, m) for m in memories]

    def replaced(self, memories: Sequence[Any]) -> list[Any]:
        """`memories` with each record this read in full in its place."""
        return [self.full.get(m.id, m) for m in memories]


class SearchPool(NamedTuple):
    """The candidates one search ranks, the BM25 statistics they are
    priced with, and the loader of the hits it returns.

    The three travel together because they are one decision. The pool
    holds the records the full-text index ranks best for the query, so it
    is biased toward the query: its own document frequencies and its own
    length average describe the matches, not the collection, and a ranking
    over it reads both from the store (`corpus_stats_provider`). Its
    records carry what a ranking reads and no more, so the hits a search
    returns are read in full first (`hydrate`).

    `memories` is a search pool, never a census: at most `POOL_SIZE`
    admitted records. Anything that wants the store's size counts it.
    """

    memories: list[Any]
    corpus_stats_provider: Callable[[list[str]], CorpusStats | None] | None
    hydrate: Hydrator | None = None


# The ceiling on how many hits one search serves; with the floor of 1 it
# is the whole range a result width, request or config, can take
# (`clamp_search_width`).
MAX_SEARCH_RESULTS = 50

#: How many admitted candidates one search ranks: the records the
#: full-text index ranks best for the query, among those the request
#: admits. Measured on 1,266 distinct searches the owner's sessions made,
#: replayed over a copy of his store (1,369 records) against a ranking of
#: every record (2026-11-12, `bench/parity/private.py`'s replay): how
#: often the pool's top hit and its whole list are the whole store's, and
#: the median time to build and rank the pool.
#:
#:     pool   top hit   whole list   median
#:       50    96.4%      52.1%       8.9 ms
#:      100    98.5%      70.1%      11.3 ms
#:      200    99.2%      84.0%      16.2 ms
#:      400    99.4%      91.7%      26.2 ms
#:
#: 200 is the last size whose step buys more agreement than time: 400
#: adds 0.2 points of top hit for 10 ms. It is four times the widest
#: result a search serves (`MAX_SEARCH_RESULTS`). The pool this replaced
#: (50 rows by the index's rank, admitted afterwards, priced with the
#: pool's own length average) read 91.9% and 31.5% in 21.2 ms.
POOL_SIZE = 200

#: Row numbers asked of the index before the pool reads every match: four
#: pools' worth, so a request that admits a quarter of what matches fills
#: its pool from the one bounded read.
_FIRST_READ = 4 * POOL_SIZE


def clamp_search_width(value: int) -> int:
    """Narrow a result width to the range a search can actually serve:
    `[1, MAX_SEARCH_RESULTS]`.

    The single arithmetic site for that range: `memory_search`'s request
    and `config set`'s check of `default_max_results` both read it.
    """
    return max(1, min(int(value), MAX_SEARCH_RESULTS))


def resolve_search_pool(
    store: Store,
    query: str,
    *,
    scopes: list[str] | None = None,
    excluded_scopes: set[str] | None = None,
    admission: Admission | None = None,
    client_filter: str | None = None,
    model_filter: str | None = None,
    size: int = POOL_SIZE,
) -> SearchPool:
    """Build the candidate pool for `query` exactly as production
    retrieval does.

    Shared by `memory_search`, the session brief, the write review's
    related list and both silent-miss producers (`recall_settle`,
    `handlers.audit_turn`): a probe measures what production retrieval
    would have surfaced, so it starts from production's candidates.

    One path, whatever the store's size:

    - The full-text index ranks every record the query matches, from the
      index alone (`Store.ranked_rows`). The match is built from the
      tokens the rankers score (`search_lexicon.fts_match_query`), so a
      record no leg can score is not a candidate, and a query that
      matches nothing has no candidates: nothing is loaded for it.
    - The rows are put through the request's own filter in that order
      (`Store.admitted_rows` with `search.candidate_admitted`: scopes,
      excluded scopes, the membership admission, the writer filters),
      from the admission inputs the store keeps for its statistics, so a
      row the request does not admit is never read. The pool is the first
      `size` that pass, read as a ranking reads them
      (`Store.light_many`). A request that admits little of what matches
      admits further down the order instead of loading the store, and
      the pool is short only when the matches run out.
    - An empty query has no match to rank by. Its pool is the newest
      `size` admitted records (`Store.newest_rows`), which is the order a
      browse returns them in.
    - The statistics a ranking prices the pool with come from the store,
      over the collection the same filter admits
      (`Store.ranking_statistics`): its size, each term's document
      frequency, and the average BM25 length. The pool's own would
      describe the matches.

    Pass the same `scopes` / `excluded_scopes` / `admission` /
    `client_filter` / `model_filter` that will be handed to
    `search.search`: the statistics' predicate binds them, so the
    denominator is the collection about to be ranked. `admission` is the
    request's (`admission.Admission`, built once from the caller's
    resolution): `memory_search` passes the SEARCH_AUTO_SCOPE or
    SEARCH_UNSCOPED surface, the hooks' probe the POOL_PREFILTER surface,
    the review's related set REVIEW_STRATA (design 3.5); None only for
    offline callers that rank a whole store. The two audit producers
    leave the writer filters at None and must keep doing so: the probe
    measures what production retrieval would have surfaced, and
    production retrieval never carries one.

    Returns a pool, not a census (`SearchPool`). Its `hydrate` is passed
    to `search.search` by a caller that returns hits.
    """
    excluded = set(excluded_scopes) if excluded_scopes else set()
    memories = _admitted_candidates(
        store,
        query,
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client_filter=client_filter,
        model_filter=model_filter,
        size=size,
    )
    return SearchPool(
        memories=memories,
        corpus_stats_provider=_corpus_stats_provider(
            store,
            scopes=scopes,
            excluded=excluded,
            admission=admission,
            client_filter=client_filter,
            model_filter=model_filter,
        ),
        hydrate=Hydrator(store),
    )


def _admitted_candidates(
    store: Store,
    query: str,
    *,
    scopes: list[str] | None,
    excluded: set[str],
    admission: Admission | None,
    client_filter: str | None,
    model_filter: str | None,
    size: int,
) -> list[Any]:
    """The first `size` records the request admits, in the index's order
    for `query`, or newest first for an empty one. The rows are admitted
    before they are read (`Store.admitted_rows`), so only the pool's own
    records are loaded."""
    admit = _admit_predicate(
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client_filter=client_filter,
        model_filter=model_filter,
    )
    rows: list[int] = []
    if not query.strip():
        offset = 0
        while len(rows) < size:
            newest = store.newest_rows(limit=_FIRST_READ, offset=offset)
            rows.extend(store.admitted_rows(newest, admit))
            if len(newest) < _FIRST_READ:
                break
            offset += len(newest)
    else:
        ranked = store.ranked_rows(query, limit=_FIRST_READ)
        rows = store.admitted_rows(ranked, admit)
        if len(rows) < size and len(ranked) == _FIRST_READ:
            # The request admits less than a quarter of the best matches:
            # admit among every match instead.
            rows = store.admitted_rows(store.ranked_rows(query), admit)
    return store.light_many(rows[:size], seed_tokens=store.index_spelled_here())


def _admit_predicate(
    *,
    scopes: list[str] | None,
    excluded: set[str],
    admission: Admission | None,
    client_filter: str | None,
    model_filter: str | None,
) -> Callable[[list[str], str | None, Any], bool]:
    """The request's filter as the store's row readers take it: over a
    record's scopes, the project its ``homes`` row names and its writer
    (`search.candidate_admitted`, the predicate `_filter_candidates` runs)."""
    scope_filter = set(scopes) if scopes else None

    def admit(memory_scopes: list[str], home: str | None, actor: Any) -> bool:
        return candidate_admitted(
            memory_scopes,
            home,
            actor,
            scope_filter=scope_filter,
            excluded=excluded,
            admission=admission,
            client_filter=client_filter,
            model_filter=model_filter,
        )

    return admit


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
    admit = _admit_predicate(
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client_filter=client_filter,
        model_filter=model_filter,
    )

    def _corpus_stats(terms: list[str]) -> CorpusStats | None:
        # Through the store's document frequency memo, which answers as
        # the uncached read does and reads again after a memory changes.
        resolved = store.ranking_statistics(terms, admit=admit)
        if resolved is None:
            return None
        size, body_df, scope_df, avgdl = resolved
        return CorpusStats(size=size, body_df=body_df, scope_df=scope_df, avgdl=avgdl)

    return _corpus_stats


@git_read_budget.async_call
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
    pool = _candidate_pool(
        deps,
        query,
        since_prior_session=since_prior_session,
        prior_boundary=prior_boundary,
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client=client,
        model=model,
    )
    hits, matched_leg = _rank_pool(
        deps,
        pool,
        query,
        scopes=scopes,
        excluded=excluded,
        admission=admission,
        client=client,
        model=model,
        max_results=max_results,
        resolved_mode=resolved_mode,
        since_prior_session=since_prior_session,
        anchoring=anchoring,
    )
    # The response's side map of records by id: the pool, with each hit's
    # record as it was read in full.
    memories = (
        pool.hydrate.replaced(pool.memories)
        if pool.hydrate is not None
        else pool.memories
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
        anchoring=anchoring,
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
    # The request clamp: the one range a result width can take, whether
    # the request names it or the config default stands in.
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
    # strictly after the latest event-log timestamp from a session_id
    # other than the recorder's or this conversation's declared harness
    # id (which its hooks use). We use the recorder's request session
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
            current_harness_session_id=identity.current_harness_session(),
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
) -> SearchPool:
    """Take the request; return its candidate pool."""
    # Two paths:
    #
    # 1. `since_prior_session=True`: the records changed after the prior
    #    session's last event, whatever they say. They are read through
    #    the index on `updated` (`Store.rows_updated_since`), not through
    #    the query: a record written since and ranked low for the query is
    #    still part of the answer. The slice is bounded by one session's
    #    activity. It is not biased toward the query, so it is priced with
    #    its own statistics and carries no provider.
    # 2. Default: `resolve_search_pool`, the one implementation the
    #    silent-miss audit producers call too, so the probe cannot rank a
    #    different candidate set than the model's actual retrieval did.
    if not since_prior_session:
        return resolve_search_pool(
            deps.store,
            query,
            scopes=scopes,
            excluded_scopes=excluded,
            admission=admission,
            client_filter=client,
            model_filter=model,
        )
    memories: list[Any] = []
    if prior_boundary is not None:
        # Strict `>`: the boundary IS the prior session's last event ts
        # (per `find_prior_session_boundary`), so a memory whose `updated`
        # equals it was written by the prior session and belongs to that
        # session, not the current-session delta.
        store = deps.store
        memories = [
            m
            for m in store.light_many(
                store.rows_updated_since(prior_boundary),
                seed_tokens=store.index_spelled_here(),
            )
            if m.updated > prior_boundary
        ]
    return SearchPool(
        memories=memories, corpus_stats_provider=None, hydrate=Hydrator(deps.store)
    )


def _rank_pool(
    deps: ToolHandlers,
    pool: SearchPool,
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
    anchoring: Anchoring,
) -> tuple[list[MemoryHit], dict[str, str]]:
    """Take the pool and the request; return the ranked hits and each hit's leg."""
    # Filled by `run_search` with `{memory_id: lexical|expansion}` for
    # the returned hits — see the note in `search_steps._record_matched_legs`
    # for why the leg travels as an out-parameter rather than a `MemoryHit`
    # field.
    matched_leg: dict[str, str] = {}
    hits = run_search(
        pool.memories,
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
        corpus_stats_provider=pool.corpus_stats_provider,
        matched_leg_out=matched_leg,
        # The expansion leg is a bench instrument, not a shipped knob.
        rescue_expansion=False,
        conversational=deps.config.behavior.conversational,
        # Every hit's path drift reads a homed record where memory_show
        # reads it (design 3.6), so a non-top hit and memory_show agree.
        anchoring=anchoring,
        # The hits are built from the whole record; the pool's records
        # carry what the ranking read.
        hydrate=pool.hydrate,
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
    anchoring: Anchoring,
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
    # from the repository's files), one per other root a hit is counted
    # at (its home's live view, `commit_legs`), and one more (the
    # path-filtered log inside `resolve_commit_drift_count`) for each hit
    # that has drift to narrow, so the cold cost scales with
    # `max_results`; warm, the memoised resolutions answer with no
    # process. The COST paragraph on `attach_commit_drift_counts` carries
    # the arithmetic. Omitted from the hit JSON when the signal isn't
    # applicable (hit's memory never verified, naming no repository, or
    # citing nothing a commit could move) rather than emitting a noisy
    # "unknown" branch every consumer would have to filter; a hit whose
    # leg nothing here read says so (`commit_drift_unchecked`). The full
    # `commit_drift` block (with status / recommendation) is still
    # attached to the expanded top hit below; the count here is the
    # lightweight triage signal.
    deps.responses.attach_commit_drift_counts(
        out,
        hits,
        memories,
        caller_origin=current_origin,
        admission=links,
        anchoring=anchoring,
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
    # Per-hit `recent_negative_outcomes`: a hit that was ignored or
    # contradicted inside the window and not validated since says so, so
    # the model does not keep re-suggesting what was rejected. The window
    # is thirty days and nearly every ``use`` row in it is an applied
    # one, so the read is in two steps: the window's negative rows, which
    # the store keeps as it reads them (`Store.negative_uses`), and, only
    # when one of them names a hit, the window's ``use`` rows that name
    # that hit (`Store.events_of_kinds`'s `naming`), which say whether it
    # was applied since. A search none of whose hits was rejected reads
    # no ``use`` row.
    if out:
        from datetime import timedelta

        from .._response import NEGATIVE_OUTCOME_WINDOW_DAYS
        from ..events import _event_id_items

        since = now - timedelta(days=NEGATIVE_OUTCOME_WINDOW_DAYS)
        hit_ids = {hit.id for hit in hits}
        rejected: set[str] = set()
        for event in deps.store.negative_uses(since):
            named = event.get("ids") or event.get("memory_ids")
            rejected.update(mid for _, mid in _event_id_items(named) if mid in hit_ids)
        if rejected:
            recent_events = list(
                deps.store.events_of_kinds(
                    ("use",), since=since, naming=sorted(rejected)
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
        # from the links table; when that read fails, the links the
        # search's own candidates carry are read instead.
        deps.responses.attach_link_annotations(
            out,
            hits,
            memories,
            store=deps.store,
            admission=links,
            excluded_scopes=excluded,
        )
        # A standing hit held out of force (ENG-200) is flagged, so a
        # search is not read as putting it back in force.
        from ..standing import held_ids

        held = held_ids(deps.store, [str(row["id"]) for row in out])
        for row in out:
            if row["id"] in held:
                row["held"] = True


def _expand_unread(
    deps: ToolHandlers, row: dict[str, Any], shown: MemoryVerdict
) -> None:
    """Take the top hit and its expanded verdict; make the hit's
    `commit_drift_unread` and `commit_drift_unchecked` the verdict's. The
    hit's pass and the expansion read one memo, so they agree save where
    the expansion resolved the leg again (a key the files do not settle, a
    head moved in between); the later read is the one the response keeps,
    as for the count."""
    flagged = (
        row.pop("commit_drift_unread", None),
        row.pop("commit_drift_unchecked", None),
    )
    if (
        shown.commit_drift_unread is not None
        or shown.commit_drift_unchecked is not None
    ):
        for key in ("commit_drift_count", "commit_drift_basis", "claim_drift"):
            row.pop(key, None)
    if shown.commit_drift_unread is not None:
        deps.responses.apply_commit_drift_unread(row, shown.commit_drift_unread)
    elif shown.commit_drift_unchecked is not None:
        deps.responses.apply_commit_drift_unchecked(row, shown.commit_drift_unchecked)
    elif any(flagged):
        verification = row.get("verification")
        if isinstance(verification, dict) and (
            verification.get("recommendation") in UNREAD_RECOMMENDATIONS
            or verification.get("recommendation") in UNCHECKED_RECOMMENDATIONS
        ):
            verification["recommendation"] = shown.verification.recommendation


def _apply_expanded_drift(
    deps: ToolHandlers, row: dict[str, Any], shown: MemoryVerdict
) -> None:
    """Replace the hit pass's drift fields with one expansion verdict."""
    drift = shown.path_drift
    row["path_drift_checked"] = len(drift.checked)
    row["path_drift_missing"] = len(drift.missing)
    if drift.should_report:
        row["path_drift"] = drift.to_dict()
    else:
        row.pop("path_drift", None)

    # An inapplicable or unread later leg must not retain an earlier
    # count. A changed basis also replaces its associated claim detail.
    for key in (
        "commit_drift",
        "commit_drift_count",
        "commit_drift_basis",
        "claim_drift",
    ):
        row.pop(key, None)
    _expand_unread(deps, row, shown)
    commit = shown.commit_drift
    if commit is not None:
        block = commit.to_dict()
        row["commit_drift"] = block
        row["commit_drift_count"] = commit.commits_since_verify
        row["commit_drift_basis"] = commit.basis
        if "claim_drift" in block:
            row["claim_drift"] = block["claim_drift"]


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
        except (OSError, sqlite3.Error):
            # Body expansion is best effort. An unreadable optional body
            # leaves the already-ranked hits available without inline text.
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
                caller_origin=current_origin,
                now=now,
                stale_after_days=deps.config.behavior.verification_stale_days,
                anchoring=anchoring,
                admission=links,
            )
            drift = shown.path_drift
            _apply_expanded_drift(deps, out[0], shown)
            expanded_drift_missing = len(drift.missing)
            commit_drift = shown.commit_drift
            if commit_drift is not None:
                expanded_commit_drift_status = commit_drift.status
                expanded_commits_since_verify = commit_drift.commits_since_verify
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
        # at all (`search_lane._relevance_label` carries the full negative
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
    "POOL_SIZE",
    "Hydrator",
    "SearchPool",
    "clamp_search_width",
    "memory_search",
    "resolve_search_pool",
]
