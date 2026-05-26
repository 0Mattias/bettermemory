"""memory_search MCP tool — handler implementation + DESC.

The handler is the busiest of the 22 tools: it issues use-tokens,
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
from ..search import SearchMode, search as run_search
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


DESC_MEMORY_SEARCH = (
    "Before you call this: (1) Don't call unless the user references "
    "shared context you don't have, or a request is ambiguous in a way "
    "stored preferences could resolve. (2) If a hit shapes your reply, "
    "announce it ('Using your stored preference for…') — non-negotiable.\n\n"
    "Search stored memories. Default: do NOT call. Reach for it "
    "only when the user references shared context you don't have "
    '("my project", "the script we wrote") or a request is '
    "ambiguous in a way stored preferences could resolve.\n\n"
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
    "to memories whose `updated` is at or after the prior session "
    "boundary (latest event from a different session_id in the "
    "log). The semantic is 'what has changed in the current "
    "session, since the last activity by other sessions' — i.e. "
    "this session's intra-session diff. A /loop iteration uses "
    "this to track what IT has written/updated; for what the "
    "prior iteration did, call episode_handoff instead. "
    "Returns empty when there's no prior session in the log; "
    "distinguish 'nothing new' (results=[]) from 'no baseline' "
    "by also calling memory_scope_overview and checking "
    "`curation_pending_new_since_last_session is None`.\n"
    "- `mode` (optional, default from config; package default `hybrid`): `keyword`, `bm25`, "
    "`semantic` (needs embeddings extra), or `hybrid` (RRF of the "
    "first three). `hybrid` for paraphrase recall; `keyword` for "
    "literal-token queries.\n\n"
    "When a hit shapes your reply, briefly say so ('Using your "
    "stored preference for…') — the transparency requirement. "
    "Outcome is recorded automatically via the use_token within ~2 "
    "turns; only call memory_record_use to override "
    "(ignored / contradicted / corrected)."
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

    # FTS5 candidate pre-filter (T3.1 phase B). When the index
    # exists and the store is large enough that load_all would
    # become the bottleneck, query the index for candidate ids
    # and load just those — sidesteps the linear scan that bites
    # at ~5K+ memories. The candidate pool is intentionally
    # generous (50 candidates for a 5-result return) so the
    # downstream rankers still see enough variety to do a good
    # job. For small stores, or when no candidates come back
    # (typical of stale index), we fall back to load_all so the
    # result quality stays identical to the pre-index path.
    memories = deps._load_search_candidates(query)

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
    prior_boundary = None
    if since_prior_session:
        from ..events import iter_all_events
        from ..health import find_prior_session_boundary

        prior_boundary = find_prior_session_boundary(
            iter_all_events(deps.store.root),
            deps.recorder.session_id,
        )
        if prior_boundary is None:
            memories = []
        else:
            memories = [m for m in memories if m.updated >= prior_boundary]

    hits = run_search(
        memories,
        query,
        scopes=scopes,
        excluded_scopes=set(state.disabled_scopes),
        repo_filter=repo_filter,
        worktree_filter=worktree_filter,
        max_results=max_results,
        half_life_days=deps.config.behavior.recency_boost_half_life_days,
        mode=cast(SearchMode, resolved_mode),
        semantic_model=semantic_model,
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
        deps.responses.attach_depends_on_resolved(out, hits, memories)

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
        else:
            out[0]["body"] = memory.body
            drift = detect_path_drift(memory.body, verified_paths=memory.verified_paths)
            if drift.has_drift or drift.verified:
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
