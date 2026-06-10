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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ..models import utcnow, validate_scope
from ..search import SearchMode, _filter_candidates, search as run_search
from ..store import MemoryNotFoundError, TombstonedError
from ..verify import (
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)
from ._shared import Context, _advance_turn, _attach_use_tokens

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


# Mirror of the candidate cap `_handlers._load_search_candidates` passes
# to `index.query` (its hardcoded `max_results=50`). A candidate slice of
# exactly this size means the FTS prefilter was saturated — see the
# cap-starvation guard below.
_PREFILTER_CAP = 50


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
    "- `query`: free text.\n"
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
    "in the log). The boundary IS the prior session's last-event "
    "ts, so a memory whose `updated` equals it belongs to that "
    "prior session — strict-`>` mirrors "
    "`curation_pending_new_since_last_session`'s exclusion so the "
    "two surfaces never double-count. The semantic is 'what has "
    "changed in the current session, since the last activity by "
    "other sessions' — i.e. this session's intra-session diff. A "
    "/loop iteration uses this to track what IT has "
    "written/updated; for what the prior iteration did, call "
    "episode_handoff instead. Returns empty when there's no prior "
    "session in the log; distinguish 'nothing new' (results=[]) "
    "from 'no baseline' by also calling memory_scope_overview and "
    "checking `curation_pending_new_since_last_session is None`.\n"
    "- `mode` (optional, default from config; package default `hybrid`): `keyword`, `bm25`, "
    "`semantic` (needs embeddings extra), or `hybrid` (RRF of the "
    "first three). `hybrid` for paraphrase recall; `keyword` for "
    "literal-token queries.\n\n"
    "Outcome is recorded automatically via the use_token within ~2 "
    "turns; only call memory_record_use to override "
    "(ignored / contradicted / corrected)."
)


def _explicit_applied_counts(
    events: list[dict[str, Any]], candidate_ids: set[str]
) -> dict[str, int]:
    """Tally explicit `memory_record_use(applied)` events per candidate id.

    Only DELIBERATE applies count: events with `auto is True` (the ~2-turn
    auto-fallback) are excluded, mirroring the auto/explicit split health.py
    and eval.py already use — auto-applies would otherwise inflate every
    retrieved memory and defeat the point. Restricted to `candidate_ids` so
    the tally is bounded by the result set, not the whole store."""
    counts: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") != "use" or ev.get("outcome") != "applied":
            continue
        if ev.get("auto") is True:
            continue
        for mid in ev.get("ids") or ev.get("memory_ids") or []:
            if mid in candidate_ids:
                counts[mid] = counts.get(mid, 0) + 1
    return counts


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
    if max_results is None:
        max_results = deps.config.behavior.default_max_results
    max_results = max(1, min(int(max_results), 50))

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
    # factory returns None when the embeddings extra isn't installed;
    # for `semantic` mode that's a hard error (the caller asked for
    # it specifically), for `hybrid` it's a graceful degrade to
    # keyword+bm25 fusion.
    semantic_model: Any | None = None
    if resolved_mode in ("semantic", "hybrid"):
        semantic_model = deps._semantic_model_factory(deps.config)
        if resolved_mode == "semantic" and semantic_model is None:
            raise ValueError(
                "mode='semantic' requires the embeddings extra. "
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
    # 2. Default: FTS5 candidate prefilter (T3.1 phase B). When the
    #    index exists and the store is large enough that load_all
    #    would dominate the budget, query the index for candidate
    #    ids and load just those. The candidate pool is generous
    #    (50 candidates for a 5-result return) so the downstream
    #    rankers see enough variety to score well.
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
        memories = deps._load_search_candidates(query, scopes=scopes)
        # Cap-starvation guard. The FTS prefilter threads only `scopes`
        # into SQL — the repo/worktree auto-scope filter and session-
        # disabled scopes apply post-cap inside run_search's
        # `_filter_candidates` pass. On a cap-saturated slice (exactly
        # `_PREFILTER_CAP` rows) those post-cap filters can strip every
        # candidate even though in-filter matches exist past the cap —
        # the failure mode the `_load_search_candidates` docstring
        # names: on a >50-memory store, in-repo matches ranked #51+
        # globally would return zero hits. Detect it with a dry-run of
        # the same authoritative filter; when fewer than `max_results`
        # candidates survive, reload the full corpus — the same cost as
        # the existing stale-index fallback, paid only on starved
        # searches. (Threading repo/worktree into SQL would need origin
        # columns in the index, i.e. a SCHEMA_VERSION bump — this guard
        # restores correctness without one.)
        post_cap_filter_active = (
            repo_filter is not None
            or worktree_filter is not None
            or bool(state.disabled_scopes)
        )
        if post_cap_filter_active and len(memories) == _PREFILTER_CAP:
            survivors = _filter_candidates(
                memories,
                scopes=scopes,
                excluded_scopes=set(state.disabled_scopes),
                repo_filter=repo_filter,
                worktree_filter=worktree_filter,
            )
            if len(survivors) < max_results:
                memories = deps.store.load_all()

    # Usage-aware ranking (opt-in via [behavior] endorsement_boost). Tally how
    # many times the model has EXPLICITLY applied each candidate and hand the
    # counts to the ranker, which applies a bounded endorsement nudge. The
    # event list is reused below for `recent_negative_outcomes`, so an enabled
    # boost adds no extra I/O on a hit-producing search. Stays None (ranker
    # neutral) when the flag is off — the shipped default is unchanged.
    recent_events: list[dict[str, Any]] | None = None
    applied_by_id: dict[str, int] | None = None
    if deps.config.behavior.endorsement_boost and memories:
        from ..events import iter_events

        recent_events = list(iter_events(deps.store.root))
        applied_by_id = _explicit_applied_counts(
            recent_events, {m.id for m in memories}
        )

    hits = run_search(
        memories,
        query,
        applied_by_id=applied_by_id,
        scopes=scopes,
        excluded_scopes=set(state.disabled_scopes),
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
        max_results=max_results,
        half_life_days=deps.config.behavior.recency_boost_half_life_days,
        mode=cast(SearchMode, resolved_mode),
        semantic_model=semantic_model,
        # Browse mode for the natural "what's new since last session"
        # usage: when the caller narrowed to the post-boundary slice
        # and didn't supply a meaningful query, treat all surviving
        # candidates as hits sorted by `updated` desc instead of
        # short-circuiting to an empty list on the stopword check.
        allow_empty_query=since_prior_session,
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
    # expand without a memory_show round-trip. One git call here
    # (`commit_author_timestamps`) + bisect per hit — the cost is
    # bounded regardless of result count. Omitted from the hit JSON
    # when the signal isn't applicable (caller not in a repo, hit's
    # memory from a different repo, hit's memory never verified)
    # rather than emitting a noisy "unknown" branch every consumer
    # would have to filter. The full `commit_drift` block (with
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
    # the cost off searches that produce no hits.
    if out:
        if recent_events is None:
            from ..events import iter_events

            recent_events = list(iter_events(deps.store.root))
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
        # from the links index; no-op when no index exists.
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

    deps.recorder.record(
        "search",
        query=query,
        scopes_filter=scopes,
        max_results=max_results,
        returned=[h["id"] for h in out],
        relevance=[h["relevance"] for h in out],
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


__all__ = ["DESC_MEMORY_SEARCH", "memory_search"]
