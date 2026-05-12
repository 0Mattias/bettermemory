"""MCP server entry point and tool registration.

The full tool surface (mirrored in `prompts.SYSTEM_PROMPT_ADDENDUM` so
the consuming model sees an identical list):

- Retrieval: memory_search, memory_show, memory_list, memory_scope_overview
- Writing:   memory_write (+ _confirm / _cancel staged-write pair),
             memory_update
- Lifecycle: memory_remove, memory_restore, memory_list_tombstones
- Verification: memory_verify
- Curation:  memory_record_use, memory_health, memory_rename_scope
- Session:   memory_scope_disable / memory_scope_enable

Each handler is thin: validate the input via the Pydantic models, call
into `store` / `search`, emit one event to the `Recorder`, return a
JSON-serializable result. The recorder is best-effort — telemetry
failures are logged but never propagate up into a tool call.
"""

from __future__ import annotations

import bisect
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config, load_config
from .durability import TransientMatch, find_transient_markers
from .events import Recorder, iter_all_events
from .health import curation_counts, report_for_directory
from .origin import (
    Origin,
    capture as capture_origin,
    commit_author_timestamps,
    repos_match,
)
from .models import (
    Category,
    Confidence,
    MemoryHit,
    MemorySummary,
    SimilarHit,
    Source,
    TombstonedSummary,
    is_valid_ulid,
    utcnow,
    validate_scope,
)
from .prompts import SYSTEM_PROMPT_ADDENDUM
from .scope_match import (
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)
from .search import find_similar, find_similar_tombstones, search as run_search
from .session import SessionState, get_state
from .store import (
    MemoryNotFoundError,
    NotTombstonedError,
    Store,
    TombstonedError,
)
from .verify import (
    compute_commit_drift,
    compute_staleness_verdict,
    compute_verification_status,
    detect_path_drift,
)


log = logging.getLogger("bettermemory")


# ---------------------------------------------------------------------------
# Use-recording outcomes — values land verbatim in the event log so the
# health view can aggregate them. Add new outcomes by extending this set;
# don't rename existing values without a migration story.
# ---------------------------------------------------------------------------


_USE_OUTCOMES: frozenset[str] = frozenset(
    {
        "applied",  # The retrieved memory shaped the response.
        "ignored",  # Retrieved but turned out off-topic.
        "contradicted",  # The user or current state contradicted the memory.
        # The retrieved memory had drifted and was fixed in the same turn
        # (memory_update / memory_verify already called). Audit-only — does
        # not raise the unresolved-contradiction flag the way `contradicted`
        # does. Use this for the post-fix log entry; use `contradicted` when
        # you've noticed a conflict but haven't fixed it yet.
        "corrected",
    }
)


# ---------------------------------------------------------------------------
# memory_write categories. Orthogonal to `confidence` (how sure) and
# `source` (where the fact came from): `category` is what kind of claim
# the memory makes.
#
# - "fact" — the default; project / infrastructure / reference / tooling
#   facts about the world. Writes commit immediately (subject to the
#   global `require_write_confirmation` flag).
# - "user-inference" — a claim about the user themselves (preferences,
#   beliefs, working style). Always routed through the pending-write
#   flow regardless of the global flag, so the user gets to confirm
#   before a sticky misattribution lands. Structural enforcement of
#   the confirmation-tier policy: the model can't shortcut it by
#   omitting a confirm-first conversational turn.
# - "ambient" — atmospheric / response-shaping memories that don't make
#   crisp verifiable claims and aren't expected to be cited via
#   `record_use`. The user's identity, persistent environment quirks,
#   that sort of thing. Persisted on the memory record so the dead-weight
#   curation rule can exclude them: their value is implicit, so a count
#   of zero `applied` events is not an indictment. Long bodies (>500
#   words) emit a warning on write — ambient memories tend to drift
#   into catch-all dumps when they get too big, and a forced split is
#   the cheap fix.
#
# Persisted to frontmatter as the `category` field. Legacy memories
# without it load with `category=None`, which the runtime treats as the
# legacy "fact" default — same dead-weight semantics as before, no
# silent shape change for old stores.
# ---------------------------------------------------------------------------


_WRITE_CATEGORIES: frozenset[str] = frozenset({c.value for c in Category})


# Ambient memories that grow past this word count get a non-blocking
# warning attached to the committed response. We don't refuse the
# write — ambient is a soft category and a long body is sometimes
# correct (e.g. a curated user-context dump) — but the warning gives
# the writer a chance to decide whether to split. Mirrors the way
# `transient_warning` is firm but `ambient_body_long` is advisory.
_AMBIENT_LONG_BODY_WORDS = 500


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_server(
    *,
    config: Config | None = None,
    store: Store | None = None,
    state: SessionState | None = None,
    recorder: Recorder | None = None,
) -> FastMCP:
    """Return a configured FastMCP instance.

    Tests pass in their own `store`, `state`, and `recorder` to keep things
    hermetic. The real entry point in `main()` lets `load_config` resolve
    everything. When `recorder` is None, one is constructed from `config` —
    `enabled=False` in the telemetry config makes every event a no-op.
    """
    config = config or load_config()
    store = store or Store(config.resolved_directory())
    state = state or get_state()
    if recorder is None:
        recorder = Recorder(
            root=config.resolved_directory(),
            session_id=state.session_id,
            enabled=config.telemetry.enabled,
            max_bytes=config.telemetry.max_bytes,
        )

    # Wire the persistent embedding cache to this store's directory. The
    # configure call doesn't load anything from disk yet; hydration is
    # lazy on the first cached_embed call so non-semantic-dedup sessions
    # never touch the file.
    _configure_persistent_embeddings(config, store)

    mcp = FastMCP(
        "bettermemory",
        # The server-level instructions block is the canonical "what is
        # this server" message every MCP client surfaces at the
        # system-prompt level. Empirically validated on Claude Code
        # 2.1.x: the block lands in the "MCP Server Instructions"
        # section of the system prompt. Claude Code truncates the block
        # if it exceeds roughly 1.8KB. The cut is mid-sentence, with
        # an ellipsis. Keep this body comfortably under that ceiling
        # (~1500 chars is the working budget). Detail beyond what fits
        # belongs on the individual tool descriptions, which are not
        # subject to the same truncation. The optional system-prompt
        # addendum (`docs/system_prompt.md` /
        # `bettermemory.SYSTEM_PROMPT_ADDENDUM`) carries the long form
        # for clients that want it pasted into a project CLAUDE.md.
        # The instructions-length regression test in tests/test_server.py
        # guards the budget.
        instructions=(
            "Persistent memory between sessions lives in this server's "
            "MCP tools (listed below). Don't fragment memory across "
            "ad-hoc files alongside; future sessions only see what "
            "these tools surface.\n\n"
            "Memory is OPT-IN retrieval. Stored memories are NOT in "
            "your context unless you call memory_search. Default to "
            "NOT retrieving — false positives hurt more than false "
            "negatives. Call only when the user references shared "
            'context ("my project", "the script we wrote") or a '
            "request is ambiguous in a way stored preferences could "
            "resolve. Skip generic factual or self-contained "
            "technical questions.\n\n"
            "Writing is the OPPOSITE axis: PROACTIVE. memory_write is "
            "a routine reflex — reach for it whenever something "
            "durable enters the conversation. Triggers: user states a "
            "preference (→ category='user-inference', server stages "
            "pending); a project decision the user concurred with (→ "
            "category='fact', commits immediately, announce); a "
            "tool/infra/config fact becomes part of the work; a unit "
            "of work finishes with a why git won't capture. Don't wait "
            'for "remember that" — the user pays you to forget. '
            "Durability check, dedup, and pending tier are guardrails; "
            "your job is to capture.\n\n"
            "Session-start: memory_scope_overview returns counts plus "
            "curation_pending. If total=0, skip memory_search unless "
            "asked. memory_search auto-scopes to caller's repo.\n\n"
            "When a retrieved memory shapes your reply, say so briefly "
            '("Using your stored preference for…"). memory_record_use '
            "auto-commits as `applied` ~2 turns later; call explicitly "
            "to override.\n\n"
            "Verify before relying. When `staleness_verdict` isn't "
            "fresh, spot-check one claim; memory_verify(id, "
            "verified_paths=…) if it holds, memory_update first if "
            "drifted."
        ),
    )

    _register_tools(mcp, config=config, store=store, state=state, recorder=recorder)
    return mcp


def _semantic_model_or_none(config: Config) -> Any:
    """Lazy load the embedding model when `semantic_dedup = true` and the
    extras are installed. Returns None otherwise — callers treat None as
    the Jaccard fallback signal. The first call after `semantic_dedup`
    is enabled pays the model-load cost (~1-2s); subsequent calls hit
    `semantic.get_model`'s in-memory cache.
    """
    if not config.behavior.semantic_dedup:
        return None
    from .semantic import get_model

    return get_model(config.behavior.semantic_model_name)


def _configure_persistent_embeddings(config: Config, store: Store) -> None:
    """Hook the persistent embedding cache to the active store dir when
    semantic dedup is enabled. The cache file lives next to the events
    log and the memory bodies so it shares the same trust boundary —
    nothing new in the permissions story. No-op when semantic dedup is
    off; when off, the in-memory cache is unused too, so persistence
    would be a write-only cycle."""
    if not config.behavior.semantic_dedup:
        return
    from .semantic import configure_persistent_cache

    configure_persistent_cache(store.root, config.behavior.semantic_model_name)


def _register_tools(
    mcp: FastMCP,
    *,
    config: Config,
    store: Store,
    state: SessionState,
    recorder: Recorder,
) -> None:
    # ---- memory_search ---------------------------------------------------

    @mcp.tool(
        name="memory_search",
        description=(
            "Search stored memories. Call this only when you have reason to "
            "think the user is referencing context you don't have, or when "
            "the user explicitly asks. Default to not searching. Returns "
            "ranked hits with snippets — call memory_show for full content. "
            "Each hit includes `relevance` (high/medium/low) and "
            "`match_terms` (which query words actually hit). Branch on "
            '`relevance`, not the raw `score` — and treat "low" hits as '
            "probable noise unless you have a reason to use them. "
            "Each hit also carries `path_drift_checked` and "
            "`path_drift_missing` integer counts so you can self-triage "
            "stale hits without a memory_show round-trip — a hit with "
            "`path_drift_missing > 0` cites filesystem paths that no "
            "longer exist on disk, which is your cue to expand and "
            "consider memory_update or memory_verify. "
            "When the caller is currently inside a checkout of the "
            "memory's origin repo, hits whose memory has been verified "
            "at some point also carry a `commit_drift_count` integer — "
            "the count of commits authored after `last_verified_at`. "
            "Absent from the hit when the signal isn't applicable "
            "(caller not in a repo, hit from a different repo, hit "
            "never verified). A non-zero count is the cue to expand "
            "even when `verification.status` reads fresh: the calendar "
            "is fresh but the project has moved. "
            "Pass `expand_top=True` to inline the full body of the top hit "
            'when its relevance is "high" — collapses the common '
            "search-then-show round trip into one call, and surfaces the "
            "full `path_drift` report (with the actual missing paths) on "
            "the expanded hit. The expanded hit also carries a "
            "`commit_drift` block (`status: 'clean' | 'drift'` plus a "
            "`commits_since_verify` count) when the caller's current "
            "repo matches the memory's origin — non-zero is the cue to "
            "spot-check even when `verification.status` reads fresh, "
            "because the project has moved since the last memory_verify. "
            "Skip `expand_top` when you only need to triage. "
            "By default (`auto_scope=True`), results are filtered to "
            "memories written from the current repository — cross-project "
            "memories are excluded. Memories written outside any repo "
            "(or before the auto-scope feature) are treated as global and "
            "always pass. Set `auto_scope=False` for cross-project queries "
            '("do you remember anything about X across all my projects"). '
            "When you actually use a hit in your reply, briefly say so "
            '("Using your stored preference for…") and call '
            "memory_record_use(ids, outcome) once per response — outcome "
            'is "applied" / "ignored" / "contradicted" / "corrected" '
            "(the last is for noticed-and-fixed-inline; see the "
            "memory_record_use tool for the full distinction). Skip the "
            "call when no memory shaped the response. If a hit's "
            'verification.status is not "fresh", spot-check at least one '
            "verifiable claim before relying on it: call memory_verify(id) "
            "if it holds, or memory_update first if it has drifted."
        ),
    )
    async def memory_search(
        query: str,
        scopes: list[str] | None = None,
        max_results: int | None = None,
        expand_top: bool = False,
        auto_scope: bool = True,
    ) -> list[dict[str, Any]]:
        _advance_turn(state, recorder)
        if max_results is None:
            max_results = config.behavior.default_max_results
        max_results = max(1, min(int(max_results), 50))

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
        current_origin = capture_origin()
        repo_filter: str | None = current_origin.repo if auto_scope else None

        memories = store.load_all()
        hits = run_search(
            memories,
            query,
            scopes=scopes,
            excluded_scopes=set(state.disabled_scopes),
            repo_filter=repo_filter,
            max_results=max_results,
            half_life_days=config.behavior.recency_boost_half_life_days,
        )
        # Pin one `now` for the whole response so the verification verdict
        # is consistent across hits — the alternative (let each helper
        # call utcnow()) could land different status labels on adjacent
        # hits if we crossed a day boundary mid-loop.
        now = utcnow()
        stale_after_days = config.behavior.verification_stale_days
        out = [
            _hit_to_dict(h, now=now, stale_after_days=stale_after_days) for h in hits
        ]

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
        _attach_commit_drift_counts(out, hits, memories, caller_origin=current_origin)

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
                memory = store.load_one(hits[0].id)
            except (MemoryNotFoundError, TombstonedError):
                # Race: memory was tombstoned between search and show.
                # Drop the body silently, the snippet still got returned.
                pass
            else:
                out[0]["body"] = memory.body
                drift = detect_path_drift(
                    memory.body, verified_paths=memory.verified_paths
                )
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
                # `_hit_to_dict` was based on `path_drift_missing` from
                # the search index (unloaded body) and may have skipped
                # claims surfaced by the actual body-level detection.
                top_verification = compute_verification_status(
                    memory.last_verified_at,
                    now=now,
                    stale_after_days=stale_after_days,
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

        recorder.record(
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
        )
        return out

    # ---- memory_show -----------------------------------------------------

    @mcp.tool(
        name="memory_show",
        description=(
            "Fetch a single memory's full content by ID. Use after "
            "memory_search when a snippet looks relevant and you want the "
            "full body. The response includes a structured `verification` "
            "block (status: 'never' | 'stale' | 'fresh', plus an "
            "actionable `recommendation` when not fresh) — branch on "
            "`verification.status` to decide whether to spot-check before "
            "relying on the body. `last_verified_at` is preserved as a "
            "raw timestamp for back-compat. `path_drift` surfaces "
            "filesystem paths cited in the body that no longer exist on "
            "disk; like verification, it's advisory — drift can be a "
            "temporary mount or a path on a different machine. Treat both "
            "as signals to spot-check, not as verdicts. When "
            'verification.status is "never" or "stale", spot-check at '
            "least one verifiable claim from the body (file path, "
            "version, configuration) before basing a recommendation on "
            "it. If the check passes, call memory_verify(id, note=...) "
            "to record what you confirmed; if a claim has drifted, fix "
            "via memory_update first — memory_update resets "
            "last_verified_at to null because the prior verification "
            "was for prose that no longer exists, so call memory_verify "
            "again after the corrected version to close the loop. "
            "When the caller is currently inside a checkout of the "
            "same repo this memory was written from, the response also "
            "carries a `commit_drift` block: "
            "`status: 'clean' | 'drift'` plus a `commits_since_verify` "
            "count. `verification.status == 'fresh'` only proves the "
            "calendar is fresh; a non-zero commit_drift is the cue to "
            "spot-check anyway because the project has moved since the "
            "last memory_verify. Absent when the caller is not in the "
            "matching repo or the memory has never been verified."
        ),
    )
    async def memory_show(id: str) -> dict[str, Any]:
        _advance_turn(state, recorder)
        try:
            memory = store.load_one(id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        # Path-drift runs against the full body. We surface `path_drift`
        # only when there's something actionable: a memory with no path
        # claims (or all-healthy paths) returns the field as null so the
        # consumer can branch on `if path_drift is not None`. Without
        # that, every memory_show would carry an empty `path_drift` dict
        # and the model would learn to ignore the field even when it
        # mattered. `verified_paths` is threaded in so a path the user
        # has previously attested gets surfaced in `path_drift.verified`
        # even when no other claims drift.
        drift = detect_path_drift(memory.body, verified_paths=memory.verified_paths)
        # Verification staleness is structurally always present — emitted
        # even for "fresh" memories — because consistent shape means the
        # consumer can branch on `verification.status` without an
        # existence check. The recommendation field is null on fresh,
        # populated otherwise; that's the actionable handle.
        verification = compute_verification_status(
            memory.last_verified_at,
            now=utcnow(),
            stale_after_days=config.behavior.verification_stale_days,
        )
        # Commit-drift is the cwd-aware sibling of verification: when the
        # caller is currently inside a checkout of the same repo the
        # memory came from, count commits authored since the last verify.
        # Stays null when the caller isn't in the matching repo or the
        # memory has no anchor to count from — emitting an "unknown"
        # branch every consumer would have to filter is worse than
        # silence, mirroring path_drift's null-when-clean contract.
        # Verified paths narrow the count to commits that touched at
        # least one of those paths — a memory verified for `[/etc/foo]`
        # reads as `clean` when the project moved but `/etc/foo`
        # didn't.
        commit_drift = compute_commit_drift(
            memory.last_verified_at,
            memory.origin.repo if memory.origin else None,
            caller_origin=capture_origin(),
            verified_paths=memory.verified_paths,
        )
        commit_drift_count_for_verdict: int | None = (
            commit_drift.commits_since_verify if commit_drift is not None else None
        )
        verdict = compute_staleness_verdict(
            verification=verification,
            path_drift_missing=len(drift.missing),
            commit_drift_count=commit_drift_count_for_verdict,
        )
        # Issue a use-token for this show before returning so the
        # auto-`record_use` flow has something to commit on the next
        # turn if the model doesn't override.
        token_map = state.issue_use_tokens([memory.id])
        recorder.record(
            "show",
            id=memory.id,
            path_drift_checked=len(drift.checked),
            path_drift_missing=len(drift.missing),
            verification_status=verification.status,
            staleness_verdict=verdict,
            commit_drift_status=(
                commit_drift.status if commit_drift is not None else None
            ),
            commits_since_verify=(
                commit_drift.commits_since_verify if commit_drift is not None else None
            ),
        )
        return {
            "id": memory.id,
            "scopes": memory.scopes,
            "confidence": memory.confidence.value,
            "source": memory.source.value,
            "category": (
                memory.category.value if memory.category is not None else None
            ),
            "created": _isoformat(memory.created),
            "updated": _isoformat(memory.updated),
            "last_verified_at": _isoformat_optional(memory.last_verified_at),
            "verification": verification.to_dict(),
            "staleness_verdict": verdict,
            "body": memory.body,
            "origin": _origin_to_dict(memory.origin),
            "path_drift": (
                drift.to_dict() if (drift.has_drift or drift.verified) else None
            ),
            "commit_drift": (
                commit_drift.to_dict() if commit_drift is not None else None
            ),
            "use_token": token_map[memory.id],
            "verified_paths": list(memory.verified_paths),
            "verified_commits": list(memory.verified_commits),
            "verified_versions": list(memory.verified_versions),
        }

    # ---- memory_write ----------------------------------------------------

    @mcp.tool(
        name="memory_write",
        description=(
            "Create a new memory. Call this PROACTIVELY whenever "
            "something durable enters the conversation — don't wait "
            'for the user to say "remember that." Triggers: user '
            "states a preference or convention (→ "
            "category='user-inference', server stages pending for "
            "confirmation); a project decision the user concurred "
            "with (→ category='fact', commits immediately, announce "
            "the save in one line); a tool / infrastructure / config "
            "fact becomes part of the work (env vars, service ports, "
            "key file locations, dependency versions); a unit of work "
            "finishes with a why git or the CHANGELOG won't capture "
            "(architectural decisions, conventions established "
            "mid-session, why a refactor went one way and not the "
            "other). The tool isn't ceremonial — reach for it as "
            "routinely as you'd write a code comment; the guardrails "
            "below catch bad writes, your job is to capture. "
            "Durable facts only. The tool runs a "
            "structural durability check on the body before writing: any "
            'transient-state marker ("currently", "today I", "we just", '
            '"the new", commit-SHA-like hex tokens, etc.) returns '
            "{status:'transient_warning', markers:[...]} instead of "
            "committing. Either rephrase the body to extract the level-up "
            "durable form (the architectural decision, the why, the "
            "what-was-built — discard the timestamp/state) or pass "
            "`acknowledge_transient=True` if the marker is genuinely "
            "durable in this case (rare — most fires are real). "
            "Confirm with the user only when the memory captures an "
            "inference about them (preferences, beliefs); for project / "
            "infra / reference / tooling memories, write directly and "
            "announce the save. Provide non-empty scopes (e.g. ['tools', "
            "'learning-style']). Content dedup runs after the durability "
            "check: if an existing memory has high overlap with the new "
            "body, this returns {status:'duplicate', matches:[...]} instead "
            "of creating a parallel entry — prefer memory_update on the "
            "matched id. Pass `force=True` to override when the new memory "
            "is meaningfully different (you have already inspected the "
            "matches and decided they are adjacent topics, not duplicates). "
            "Medium-overlap matches don't block; they're surfaced as "
            "`related` on the success response. Tombstone-aware dedup "
            "also runs: high overlap with a previously-removed memory "
            "returns {status:'previously_removed', removed_matches:[...]} "
            "carrying the original removed_reason. Inspect the reason — "
            "if the rejection still applies, drop the write; if the "
            "fact is now correct, call memory_restore(id) on the "
            "tombstone rather than writing a parallel entry. "
            'Avoid the catch-all "general" scope; it defeats targeted '
            "retrieval — pick something narrower like `tools`, "
            "`learning-style`, `infrastructure`, or `projects:<name>`. "
            'Pass `category="user-inference"` when the memory captures '
            "a claim about the user themselves (preferences, beliefs, "
            "working style); that always returns {status:'pending', "
            "pending_id} regardless of config so the user gets to "
            "confirm before a sticky misattribution lands. Ask the user "
            "in plain language ('want me to remember that you prefer X?') "
            "and only then call memory_write_confirm(pending_id), or "
            "memory_write_cancel(pending_id) if they decline. The "
            'default category "fact" covers project / infrastructure / '
            "reference / tooling memories and commits immediately "
            "unless `require_write_confirmation` is true in config."
        ),
    )
    async def memory_write(
        content: str,
        scopes: list[str],
        confidence: str = "medium",
        source: str = "explicit-statement",
        force: bool = False,
        acknowledge_transient: bool = False,
        acknowledge_scope_mismatch: bool = False,
        category: str = "fact",
    ) -> dict[str, Any]:
        # `_advance_turn` keeps the per-session turn counter monotonic
        # for the auto-`record_use` flow even on calls that don't touch
        # search/show. We bump first so any pending use-tokens that
        # crossed their TTL get auto-committed before this write fires
        # its own event.
        _advance_turn(state, recorder)
        payload = _validate_write_payload(
            content=content,
            scopes=scopes,
            confidence=confidence,
            source=source,
            allowed_scopes=config.scopes.allowed,
            category=category,
        )

        # Origin is captured before the durability check so it's always
        # part of the payload that flows into either staging or the direct
        # write path. We never persist origin for a transient_warning — the
        # write isn't happening — so the early return below short-circuits
        # before any disk I/O.
        payload["origin"] = capture_origin()

        # Durability check runs before dedup. A transient body shouldn't
        # become a duplicate of an existing transient memory — we'd just be
        # routing the caller toward memory_update on a fact that itself
        # shouldn't have been written. Catch transience first.
        transient_hits = find_transient_markers(payload["content"])
        if transient_hits and not acknowledge_transient:
            recorder.record(
                "write",
                status="transient_warning",
                scopes=payload["scopes"],
                forced=False,
                markers=[h.marker for h in transient_hits],
            )
            return {
                "status": "transient_warning",
                "markers": [_transient_to_dict(h) for h in transient_hits],
                "hint": (
                    "The body contains transient-state markers that won't "
                    "be true in a week. Either rephrase to the durable "
                    "level-up version (extract the architectural decision, "
                    "the why, what-was-built — discard the timestamp/state) "
                    "or pass acknowledge_transient=True if the marker is "
                    "genuinely durable in context."
                ),
            }

        # Scope-mismatch check runs after transient. Cheap heuristic: if
        # the body cites a known `projects:<name>` scope's name token or
        # a path under another project's root and that scope isn't in the
        # declared scope list, surface the mismatch so the writer can
        # either retag or override. Mirrors the transient_warning shape.
        if not acknowledge_scope_mismatch:
            existing_memories = store.load_all()
            mismatch = detect_scope_mismatch(
                body=payload["content"],
                declared_scopes=payload["scopes"],
                project_scopes=collect_project_scopes(existing_memories),
                project_roots=collect_project_roots(existing_memories),
            )
            if mismatch.has_mismatch:
                recorder.record(
                    "write",
                    status="scope_mismatch",
                    scopes=payload["scopes"],
                    forced=False,
                    suggested_scopes=list(mismatch.suggested_scopes),
                    mismatch_kinds=[m.kind for m in mismatch.matches],
                )
                return {
                    "status": "scope_mismatch",
                    "matches": [m.to_dict() for m in mismatch.matches],
                    "suggested_scopes": list(mismatch.suggested_scopes),
                    "hint": (
                        "The body cites paths or project names that suggest "
                        "this memory belongs to a different scope. Either "
                        "add one of `suggested_scopes` to the declared "
                        "scope list, or pass acknowledge_scope_mismatch=True "
                        "if the cross-reference is intentional (e.g. an "
                        "infrastructure note that mentions multiple "
                        "projects by design)."
                    ),
                }

        # Dedup runs second — staging or writing happens only if the new body
        # isn't a high-overlap duplicate of an existing memory. `force=True`
        # is the override path: the caller has already looked at the matches
        # and decided this entry is meaningfully different.
        #
        # Two passes: active memories (returns "high"/"medium" relevance) and
        # tombstoned memories (returns "high-removed"/"medium-removed"). An
        # active high match is the strongest signal — there's a live record
        # to update — and short-circuits with status="duplicate". A
        # tombstone high match (without an active high) becomes
        # status="previously_removed", carrying the original removal_reason
        # so the writer can decide whether the rejection still applies.
        # Medium hits from either pass are surfaced as `related` /
        # `removed_related` and don't block.
        related: list[SimilarHit] = []
        removed_related: list[SimilarHit] = []
        if not force:
            semantic_model = _semantic_model_or_none(config)
            high_threshold = (
                config.behavior.semantic_high_threshold
                if config.behavior.semantic_dedup
                else None
            )
            medium_threshold = (
                config.behavior.semantic_medium_threshold
                if config.behavior.semantic_dedup
                else None
            )
            similar = find_similar(
                payload["content"],
                store.load_all(),
                semantic_model=semantic_model,
                high_threshold=high_threshold,
                medium_threshold=medium_threshold,
            )
            high = [h for h in similar if h.relevance == "high"]
            if high:
                recorder.record(
                    "write",
                    status="duplicate",
                    scopes=payload["scopes"],
                    forced=False,
                    matches=[h.id for h in high],
                )
                return {
                    "status": "duplicate",
                    "matches": [_similar_to_dict(h) for h in high],
                    "hint": (
                        "An existing memory has high content overlap with "
                        "this write. Prefer memory_update on the matched "
                        "id over creating a parallel entry. Pass force=True "
                        "if the new memory is meaningfully different."
                    ),
                }
            related = [h for h in similar if h.relevance == "medium"]

            # Tombstone-aware dedup. Only runs when no active high-overlap
            # match was found — otherwise the active path is the better
            # answer (update the live entry rather than discussing the
            # removed one).
            tombstone_similar = find_similar_tombstones(
                payload["content"],
                store.load_tombstones(),
                semantic_model=semantic_model,
                high_threshold=high_threshold,
                medium_threshold=medium_threshold,
            )
            high_removed = [
                h for h in tombstone_similar if h.relevance == "high-removed"
            ]
            if high_removed:
                recorder.record(
                    "write",
                    status="previously_removed",
                    scopes=payload["scopes"],
                    forced=False,
                    removed_matches=[h.id for h in high_removed],
                )
                return {
                    "status": "previously_removed",
                    "removed_matches": [_similar_to_dict(h) for h in high_removed],
                    "hint": (
                        "A previously-removed memory has high content overlap "
                        "with this write. Inspect each `removed_reason` — if "
                        "the rejection still applies, drop the write; if the "
                        "fact is now correct, call memory_restore(id) on the "
                        "tombstone instead of writing a parallel entry. Pass "
                        "force=True to bypass when the new memory is "
                        "meaningfully different from the removed one."
                    ),
                }
            removed_related = [
                h for h in tombstone_similar if h.relevance == "medium-removed"
            ]

        # Capture which markers (if any) were overridden by
        # acknowledge_transient — feeds the override-rate signal in the
        # event log so we can tell whether a marker is producing too many
        # false positives.
        acknowledged = (
            [h.marker for h in transient_hits]
            if transient_hits and acknowledge_transient
            else []
        )

        # Two independent triggers for the pending-write flow:
        #   1. Global config flag (`require_write_confirmation`) — opts the
        #      whole install into staged writes.
        #   2. Category == "user-inference" — structural enforcement of the
        #      confirmation-tier policy. A claim *about* the user is never
        #      a silent write, regardless of the global flag, because
        #      misattribution sticks.
        # `pending_reason` is recorded in the event log so health/analysis
        # can distinguish the two triggers later. Ambient memories take
        # the same fast path as fact (no pending gate), but they may
        # acquire a non-blocking long-body warning below.
        category_enum: Category = payload["category"]
        if config.behavior.require_write_confirmation:
            pending_reason = "config"
        elif category_enum == Category.USER_INFERENCE:
            pending_reason = "user-inference"
        else:
            pending_reason = None

        if pending_reason is not None:
            pending = state.stage_write(payload)
            hint = (
                "User-inference category — ask the user in plain "
                "language ('want me to remember that you prefer X?') "
                "and only then call memory_write_confirm(pending_id), "
                "or memory_write_cancel(pending_id) if they decline."
                if pending_reason == "user-inference"
                else (
                    "Confirm with memory_write_confirm(pending_id) or "
                    "drop with memory_write_cancel(pending_id)."
                )
            )
            response: dict[str, Any] = {
                "status": "pending",
                "pending_id": pending.pending_id,
                "pending_reason": pending_reason,
                "preview": {
                    "content": payload["content"],
                    "scopes": payload["scopes"],
                    "confidence": payload["confidence"].value,
                    "source": payload["source"].value,
                    "category": category_enum.value,
                },
                "hint": hint,
            }
            if related:
                response["related"] = [_similar_to_dict(h) for h in related]
            if removed_related:
                response["removed_related"] = [
                    _similar_to_dict(h) for h in removed_related
                ]
            recorder.record(
                "write",
                status="pending",
                pending_id=pending.pending_id,
                pending_reason=pending_reason,
                category=category_enum.value,
                scopes=payload["scopes"],
                forced=force,
                related=[h.id for h in related],
                removed_related=[h.id for h in removed_related],
                markers_acknowledged=acknowledged,
            )
            return response

        memory = store.write(**payload)
        # Ambient long-body advisory — non-blocking. Surfaced after the
        # commit so the caller still gets the id but sees a structured
        # warning they can act on (split, prune, leave). Fires on the
        # post-strip word count of the persisted body.
        warnings: list[str] = []
        if (
            category_enum == Category.AMBIENT
            and len(memory.body.split()) > _AMBIENT_LONG_BODY_WORDS
        ):
            warnings.append("ambient_body_long")
        recorder.record(
            "write",
            status="committed",
            id=memory.id,
            category=category_enum.value,
            scopes=memory.scopes,
            confidence=memory.confidence.value,
            source=memory.source.value,
            forced=force,
            related=[h.id for h in related],
            removed_related=[h.id for h in removed_related],
            markers_acknowledged=acknowledged,
            warnings=warnings,
        )
        return _committed(
            memory,
            related=related,
            removed_related=removed_related,
            warnings=warnings,
        )

    @mcp.tool(
        name="memory_write_confirm",
        description=(
            "Commit a memory_write that returned status='pending'. "
            "Pass the pending_id from that response."
        ),
    )
    async def memory_write_confirm(pending_id: str) -> dict[str, Any]:
        _advance_turn(state, recorder)
        pending = state.take_pending(pending_id)
        if pending is None:
            raise ValueError(
                f"no pending write with id {pending_id!r} (it may have "
                "expired or been already committed)"
            )
        memory = store.write(**pending.payload)
        recorder.record(
            "write_confirm",
            pending_id=pending_id,
            id=memory.id,
            scopes=memory.scopes,
        )
        return _committed(memory)

    @mcp.tool(
        name="memory_write_cancel",
        description=(
            "Drop a pending memory_write without committing. "
            "Pass the pending_id from the original write response."
        ),
    )
    async def memory_write_cancel(pending_id: str) -> dict[str, Any]:
        _advance_turn(state, recorder)
        existed = state.cancel_pending(pending_id)
        recorder.record("write_cancel", pending_id=pending_id, existed=existed)
        return {"cancelled": pending_id, "existed": existed}

    # ---- memory_update ---------------------------------------------------

    @mcp.tool(
        name="memory_update",
        description=(
            "Refine an existing memory in place. Pass the memory id and any "
            "of `content`, `scopes`, `confidence`, `category` to change. "
            "Preserves `id`, `created`, and `source`; bumps `updated`. "
            "Prefer this over memory_remove + memory_write when correcting "
            "or refining a stored fact — delete-and-recreate loses the "
            "original timestamp and litters .tombstones/ with what are "
            "really edits. Pass at least one field; replace semantics for "
            "`scopes` (provide the full new list, not a delta). `category` "
            "accepts the same values as `memory_write` (`fact`, `ambient`); "
            "use this to retag legacy memories written before the "
            "`ambient` tier existed without round-tripping through "
            "remove+rewrite. `user-inference` is rejected here — that "
            "category exists to gate WRITES through the pending-confirm "
            "flow, and there is no equivalent gate on update."
        ),
    )
    async def memory_update(
        id: str,
        content: str | None = None,
        scopes: list[str] | None = None,
        confidence: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        _advance_turn(state, recorder)
        if (
            content is None
            and scopes is None
            and confidence is None
            and category is None
        ):
            raise ValueError(
                "memory_update needs at least one of content, scopes, "
                "confidence, or category"
            )
        if content is not None and not content.strip():
            raise ValueError("content must be non-empty if provided")

        try:
            existing = store.load_one(id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc

        new_scopes = existing.scopes
        if scopes is not None:
            if not scopes:
                raise ValueError("scopes must contain at least one entry if provided")
            new_scopes = [validate_scope(s) for s in scopes]
            if config.scopes.allowed:
                allowed = set(config.scopes.allowed)
                unknown = [s for s in new_scopes if s not in allowed]
                if unknown:
                    raise ValueError(
                        f"scope(s) not in allowed list: {unknown}. "
                        f"Allowed: {sorted(config.scopes.allowed)}"
                    )

        new_confidence = existing.confidence
        if confidence is not None:
            try:
                new_confidence = Confidence(confidence)
            except ValueError as exc:
                raise ValueError(
                    f"confidence must be one of {[c.value for c in Confidence]}"
                ) from exc

        new_category = existing.category
        if category is not None:
            # `user-inference` is a write-time gate (pending-confirm flow);
            # there's no analogous gate on update, so allowing a retag
            # *into* `user-inference` would silently bypass that gate.
            # Allow `fact` and `ambient` only.
            allowed_update_categories = {Category.FACT.value, Category.AMBIENT.value}
            if category not in allowed_update_categories:
                raise ValueError(
                    "category must be one of "
                    f"{sorted(allowed_update_categories)} on update "
                    "(`user-inference` is write-only — it gates the "
                    "pending-confirm flow which has no equivalent here)"
                )
            new_category = Category(category)

        new_body = existing.body
        if content is not None:
            new_body = content.strip() + "\n"

        # When `content` changes, the prior verification was for prose
        # that no longer exists — reset `last_verified_at` to None so the
        # caller has to re-confirm against the new body. Scope/confidence/
        # category edits don't touch the body's claims, so the verification
        # stays intact for those. This matches the intuition that
        # verification is a property of body content, not of metadata.
        update_fields: dict[str, Any] = {
            "body": new_body,
            "scopes": new_scopes,
            "confidence": new_confidence,
            "category": new_category,
        }
        if content is not None:
            update_fields["last_verified_at"] = None

        merged = existing.model_copy(update=update_fields)
        updated = store.update(merged)
        fields_changed = [
            name
            for name, value in (
                ("content", content),
                ("scopes", scopes),
                ("confidence", confidence),
                ("category", category),
            )
            if value is not None
        ]
        recorder.record(
            "update",
            id=updated.id,
            fields=fields_changed,
            scopes=updated.scopes,
            confidence=updated.confidence.value,
            category=updated.category.value if updated.category is not None else None,
        )
        return _committed(updated)

    # ---- memory_list -----------------------------------------------------

    @mcp.tool(
        name="memory_list",
        description=(
            "List active memories. By default returns one-line summaries "
            "(IDs, scopes, summary, no body) — cheap triage. "
            "Pass `with_bodies=True` to inline full bodies in one call; "
            "useful for small stores where N round trips of "
            "`list -> show -> show` would be wasteful. Don't reach for "
            "`with_bodies` casually — it pulls every memory in scope into "
            "your context, which is the failure mode this project exists "
            "to avoid. Filter by `scopes` if you only care about a subset."
        ),
    )
    async def memory_list(
        scopes: list[str] | None = None,
        with_bodies: bool = False,
    ) -> list[dict[str, Any]]:
        _advance_turn(state, recorder)
        if scopes:
            scopes = [validate_scope(s) for s in scopes]
        # Apply session-disabled scopes to listing too — consistency.
        excluded = set(state.disabled_scopes)
        # Single `now` for the whole listing — same reasoning as in
        # memory_search: consistent verification verdict across rows.
        now = utcnow()
        stale_after_days = config.behavior.verification_stale_days

        if with_bodies:
            out: list[dict[str, Any]] = []
            for memory in store.load_all():
                memory_scopes = set(memory.scopes)
                if excluded and (memory_scopes & excluded):
                    continue
                if scopes and not (memory_scopes & set(scopes)):
                    continue
                out.append(
                    _memory_to_dict(memory, now=now, stale_after_days=stale_after_days)
                )
            recorder.record(
                "list",
                scopes_filter=scopes,
                with_bodies=True,
                count=len(out),
                returned=[m["id"] for m in out],
            )
            return out

        out_summary: list[dict[str, Any]] = []
        for summary in store.list_summaries(scopes=scopes):
            if excluded and (set(summary.scopes) & excluded):
                continue
            out_summary.append(
                _summary_to_dict(summary, now=now, stale_after_days=stale_after_days)
            )
        recorder.record(
            "list",
            scopes_filter=scopes,
            with_bodies=False,
            count=len(out_summary),
            returned=[s["id"] for s in out_summary],
        )
        return out_summary

    # ---- memory_remove ---------------------------------------------------

    @mcp.tool(
        name="memory_remove",
        description=(
            "Tombstone a memory. The file is moved to .tombstones/ with a "
            "removal reason and the originating session id — never hard-"
            "deleted. Use when a stored fact is wrong or no longer relevant. "
            "Tombstones remain searchable via memory_list_tombstones and "
            "are surfaced as `removed_matches` on memory_write when a new "
            "body looks similar to a previously-removed fact, so the "
            "lesson encoded in the removal reason isn't lost. Use "
            "memory_restore(id) to undo an accidental removal."
        ),
    )
    async def memory_remove(id: str, reason: str) -> dict[str, Any]:
        _advance_turn(state, recorder)
        if not reason or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        try:
            tombstone_path = store.tombstone(id, reason, session_id=state.session_id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        recorder.record("remove", id=id, reason=reason)
        return {
            "removed": id,
            "tombstone_path": str(tombstone_path),
        }

    # ---- memory_list_tombstones ------------------------------------------

    @mcp.tool(
        name="memory_list_tombstones",
        description=(
            "List removed (tombstoned) memories. One-line summaries plus "
            "removal metadata (`removed`, `removed_reason`, "
            "`removed_session`) — body stripped, like memory_list. Use "
            'for curation passes ("what did I clear out last month?") or '
            "to investigate when the user asks 'I think I had a memory "
            "about X — what happened?'. Pass `scopes` to filter, like "
            "memory_list. Tombstones are sorted by `removed` descending — "
            "most-recently-removed first."
        ),
    )
    async def memory_list_tombstones(
        scopes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        _advance_turn(state, recorder)
        if scopes:
            scopes = [validate_scope(s) for s in scopes]
        excluded = set(state.disabled_scopes)
        out: list[dict[str, Any]] = []
        for summary in store.list_tombstones(scopes=scopes):
            if excluded and (set(summary.scopes) & excluded):
                continue
            out.append(_tombstone_summary_to_dict(summary))
        recorder.record(
            "list_tombstones",
            scopes_filter=scopes,
            count=len(out),
            returned=[s["id"] for s in out],
        )
        return out

    # ---- memory_restore --------------------------------------------------

    @mcp.tool(
        name="memory_restore",
        description=(
            "Bring a tombstoned memory back to the active set. Strips the "
            "removal frontmatter, moves the file out of .tombstones/, and "
            "preserves the original `created`, `updated`, and "
            "`last_verified_at` timestamps — the body didn't change while "
            "it was tombstoned, so the recency boost stays honest. Raises "
            "if the id is active (use memory_update for edits) or unknown. "
            "The original removal reason and session live on in the event "
            "log even after restore."
        ),
    )
    async def memory_restore(id: str) -> dict[str, Any]:
        _advance_turn(state, recorder)
        try:
            memory = store.restore(id)
        except NotTombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        except ValueError:
            # _load_tombstone_path raises ValueError on a malformed file
            # (e.g. missing `created`). Surface verbatim — the message
            # tells the caller which field is missing.
            raise
        recorder.record(
            "restore",
            id=memory.id,
            scopes=memory.scopes,
        )
        return _committed(memory)

    # ---- memory_scope_disable / enable -----------------------------------

    # ---- memory_health ---------------------------------------------------

    @mcp.tool(
        name="memory_health",
        description=(
            "Aggregate health view over the event log + active memories. "
            "Returns a structured report with dead-weight memories (created "
            "more than `window_days` ago, never `applied` according to "
            "memory_record_use), heavily-used memories, memories with "
            "unresolved contradictions, transient-marker fire/override "
            "rates, the scope distribution, a per-scope rollup "
            "(`scope_health`) showing where dead weight and contradictions "
            "concentrate, singleton scopes that look like typos of an "
            "existing scope (`rare_scopes` — Levenshtein distance <= 2 from "
            "another scope; legitimate narrow singletons no longer trip "
            "the bucket), an `orphan_use_events` counter "
            "(memory_record_use calls whose ids resolved to no record — a "
            "fabrication smoke test), and a `verification_debt` rollup "
            "partitioning memories by verification status "
            "(never_verified / stale / fresh against the configured "
            "`verification_stale_days` threshold; capped row lists for "
            "inline display, uncapped totals for the bucket sizes). Use "
            "this to drive curation passes — prune dead weight, refresh "
            "contradicted memories via memory_update *or* re-confirm them "
            "via memory_verify (either resolution path clears the "
            "unresolved flag), spot-check the never_verified / stale "
            "buckets and call memory_verify on the ones whose claims "
            "still hold, trim transient markers whose override rate is "
            "high, fix typo scopes via memory_rename_scope. Each row in "
            "the contradicted bucket carries a `resolution_timeline` — "
            "the chronological log of update / verify / contradicted / "
            "corrected events for that memory — so a stuck flag can be "
            "self-diagnosed as out-of-order audit logging vs genuinely "
            "unresolved without grepping the event log by hand. The "
            "corresponding CLI is `bettermemory health`. `min_applied` "
            "floors the heavily_used bucket on applied_count (default "
            "comes from config.toml — typically 3 — to keep the bucket "
            "out of one-off-acknowledgement noise). Per-row stats "
            "include `last_verified_at` so a curation pass can flag "
            "rows that haven't been spot-checked recently. The cwd-aware "
            "`commit_drift_debt` rollup is populated when the server is "
            "running inside a repo whose memories live in this store: "
            "rows are memories whose origin matches the current repo "
            "and whose `last_verified_at` precedes commits in the "
            "current HEAD, sorted most-commits-ahead first. Null when "
            "the server isn't in a repo or no anchored memories exist."
        ),
    )
    async def memory_health(
        window_days: int = 30,
        heavily_used_top_k: int = 10,
        min_applied: int | None = None,
    ) -> dict[str, Any]:
        _advance_turn(state, recorder)
        # Falling through to the configured default lets the tool stay
        # ergonomic for the common case (don't pass anything, get the
        # tuned threshold) while still allowing a per-call override
        # ("show me everything that's been applied at least once on this
        # young store").
        threshold = (
            int(min_applied)
            if min_applied is not None
            else config.behavior.heavily_used_min_applied
        )
        report = report_for_directory(
            store.root,
            window_days=int(window_days),
            heavily_used_top_k=int(heavily_used_top_k),
            heavily_used_min_applied=threshold,
            verification_stale_days=config.behavior.verification_stale_days,
            # Pass caller_origin so the cwd-aware `commit_drift_debt`
            # rollup populates when the server is running inside a repo
            # whose memories live in this store.
            caller_origin=capture_origin(),
        )
        return report.to_dict()

    # ---- memory_record_use ----------------------------------------------

    @mcp.tool(
        name="memory_record_use",
        description=(
            "Record how a retrieved memory was used in your response. Call "
            "this once per response that consumed memory output, with the "
            "ids you actually relied on. Outcomes:\n"
            '- "applied" — the memory shaped the reply.\n'
            '- "ignored" — retrieved but turned out off-topic.\n'
            '- "contradicted" — the user or current state contradicted the '
            "stored fact AND you have not fixed it yet. Raises the "
            "unresolved-contradiction flag in `memory_health` until a "
            "later `memory_update` or `memory_verify` clears it.\n"
            '- "corrected" — the memory had drifted and you fixed it '
            "inline in the same turn (`memory_update` and/or "
            "`memory_verify` already called before this record_use call). "
            "Audit-only; does NOT raise the contradiction flag. Use this "
            "instead of `contradicted` when the resolution is already "
            "done — recording `contradicted` after the fix leaves the "
            "flag stuck because event timestamps decide resolution state.\n"
            "The event feeds `memory_health` so dead-weight memories can be "
            "pruned and stale ones flagged. `note` is an optional free-form "
            "string for context. Skip the call when no retrieved memory "
            "shaped your response — silence is also signal, as the absence "
            "of `applied` events for a recently-retrieved id is what tells "
            "us the memory wasn't useful."
        ),
    )
    async def memory_record_use(
        memory_ids: list[str],
        outcome: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        if not memory_ids:
            raise ValueError("memory_ids must contain at least one entry")
        if outcome not in _USE_OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(_USE_OUTCOMES)}")
        # ULID-format check only — we don't load the store to confirm the
        # id exists. Recording a use against a just-tombstoned memory is a
        # legitimate signal (the user contradicted it, we removed it),
        # and a load_all on every record_use call is wasteful.
        for mid in memory_ids:
            if not is_valid_ulid(mid):
                raise ValueError(f"invalid memory id: {mid!r}")
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string if provided")

        # The explicit outcome overrides any pending auto-commit. Pass
        # the ids through `_advance_turn` so the auto pass that would
        # otherwise have fired skips them, then purge their tokens so a
        # *future* auto-commit for the same id doesn't fire either —
        # the model has spoken, the auto-commit is settled.
        override_set = set(memory_ids)
        _advance_turn(state, recorder, override_ids=override_set)
        for mid in memory_ids:
            state.purge_use_token(mid)

        recorder.record(
            "use",
            ids=list(memory_ids),
            outcome=outcome,
            note=note,
        )
        return {
            "recorded": list(memory_ids),
            "outcome": outcome,
        }

    # ---- memory_verify ---------------------------------------------------

    @mcp.tool(
        name="memory_verify",
        description=(
            "Bump `last_verified_at` to now after spot-checking that a "
            "memory's claims still match reality. Call this when you have "
            "actively confirmed the body's verifiable content — file paths "
            "still exist, the version number still matches, the script is "
            "still where it says, the configuration is still what it says. "
            "Verification is the orthogonal axis to content edits: this "
            "tool does not touch `updated`, and `memory_update` does not "
            "touch `last_verified_at`. A typo fix bumps `updated` (the "
            "body changed) but not `last_verified_at` (no claim to have "
            "spot-checked the world); a verify call bumps "
            "`last_verified_at` (you confirmed reality) but not `updated` "
            "(the body is unchanged). `note` is an optional free-form "
            "string captured in the event log — use it to record what "
            "was checked ('confirmed `/usr/local/sbin/zb-backup.sh` "
            "exists on the homelab'). Idempotent: calling twice in a row "
            "just slides the timestamp forward. After memory_update on a "
            "memory whose claims you later spot-check, call memory_verify "
            "to close the loop — memory_update resets last_verified_at "
            "to null because the prior verification was for prose that "
            "no longer exists, so a verify after the corrected version "
            'is what restores the "checked against reality" state. '
            "Verify is also a resolution path for an unresolved "
            "contradiction in `memory_health`: if the body still matches "
            "reality despite an earlier `record_use(contradicted)` event, "
            "calling memory_verify after the contradiction clears the "
            "flag (the same way memory_update would)."
        ),
    )
    async def memory_verify(
        id: str,
        note: str | None = None,
        verified_paths: list[str] | None = None,
        verified_commits: list[str] | None = None,
        verified_versions: list[str] | None = None,
    ) -> dict[str, Any]:
        _advance_turn(state, recorder)
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string if provided")
        for label, value in (
            ("verified_paths", verified_paths),
            ("verified_commits", verified_commits),
            ("verified_versions", verified_versions),
        ):
            if value is None:
                continue
            if not isinstance(value, list) or not all(
                isinstance(s, str) for s in value
            ):
                raise ValueError(f"{label} must be a list of strings if provided")
        try:
            memory = store.mark_verified(
                id,
                verified_paths=verified_paths,
                verified_commits=verified_commits,
                verified_versions=verified_versions,
            )
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        recorder.record(
            "verify",
            id=memory.id,
            last_verified_at=_isoformat_optional(memory.last_verified_at),
            note=note,
            verified_paths=list(memory.verified_paths),
            verified_commits=list(memory.verified_commits),
            verified_versions=list(memory.verified_versions),
        )
        return {
            "verified": memory.id,
            "last_verified_at": _isoformat_optional(memory.last_verified_at),
            "updated": _isoformat(memory.updated),
            "verified_paths": list(memory.verified_paths),
            "verified_commits": list(memory.verified_commits),
            "verified_versions": list(memory.verified_versions),
        }

    # ---- memory_scope_overview ------------------------------------------

    @mcp.tool(
        name="memory_scope_overview",
        description=(
            "Cheap session-start hint: counts of memories per scope, "
            "without bodies, IDs, or summaries. Default-scoped to the "
            "caller's current repository (memories with no origin pass as "
            "global). Returns `{current_repo, scopes: {scope: count}, "
            "total}`. Use this once at the start of a conversation to see "
            "whether stored memory exists for the current project — if "
            "`total` is 0, you can skip memory_search for the rest of the "
            "session unless the user explicitly asks. If the count is "
            "non-zero, memory_search remains the way to retrieve content; "
            "this tool only tells you whether searching is likely to be "
            "fruitful. Set `auto_scope=False` to count across all stored "
            "memory regardless of origin (the cross-project view). Counts "
            "respect session-disabled scopes."
        ),
    )
    async def memory_scope_overview(
        auto_scope: bool = True,
    ) -> dict[str, Any]:
        _advance_turn(state, recorder)
        repo_filter: str | None = None
        current_origin: Origin | None = None
        if auto_scope:
            current_origin = capture_origin()
            repo_filter = current_origin.repo

        excluded = set(state.disabled_scopes)
        scope_counts: dict[str, int] = {}
        total = 0
        all_memories = store.load_all()
        for memory in all_memories:
            memory_scope_set = set(memory.scopes)
            if excluded and (memory_scope_set & excluded):
                continue
            if repo_filter is not None:
                memory_repo = memory.origin.repo if memory.origin else None
                # Reuse the same repos_match semantics as memory_search so
                # this tool's `current_repo` filter is bit-identical to
                # the search filter — otherwise the model would see
                # "5 memories tagged projects:foo" here and zero hits in
                # search and have no way to reconcile that.
                if not repos_match(memory_repo, repo_filter):
                    continue
            total += 1
            for scope in memory.scopes:
                if scope in excluded:
                    continue
                scope_counts[scope] = scope_counts.get(scope, 0) + 1

        # Sort scopes by count desc, then name for determinism. Important
        # for tests and for the model — a stable ordering means a "if the
        # top scope is X" branch behaves consistently across calls.
        sorted_scopes = dict(
            sorted(scope_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )

        # Curation pending — five integer counts that surface "is there
        # anything worth a curation pass right now?" without the full
        # `memory_health` cost. Walks the event log once (same shape
        # health.compute_health does) but skips row materialisation.
        # Globally scoped — `auto_scope=True` only filters the per-repo
        # totals above; curation is always cross-repo because rot in
        # another scope is still rot. The caller-origin we feed in
        # drives the `drifted` count when available.
        curation = curation_counts(
            all_memories,
            iter_all_events(store.root),
            window_days=30,
            verification_stale_days=config.behavior.verification_stale_days,
            caller_origin=current_origin,
        )

        recorder.record(
            "scope_overview",
            auto_scope=auto_scope,
            current_repo=repo_filter,
            total=total,
            scope_count=len(sorted_scopes),
            curation_pending=curation,
        )
        return {
            "current_repo": repo_filter,
            "current_cwd": current_origin.cwd if current_origin else None,
            "auto_scope": auto_scope,
            "scopes": sorted_scopes,
            "total": total,
            "disabled_scopes": sorted(state.disabled_scopes),
            "curation_pending": curation,
        }

    @mcp.tool(
        name="memory_scope_disable",
        description=(
            "Disable a scope for the rest of this session. Subsequent "
            "memory_search and memory_list calls will exclude memories "
            "tagged with this scope. Useful when the user says 'this is "
            "unrelated to project X'. Resets when the server restarts."
        ),
    )
    async def memory_scope_disable(scope: str) -> dict[str, Any]:
        _advance_turn(state, recorder)
        clean = validate_scope(scope)
        state.disable(clean)
        recorder.record("scope_disable", scope=clean)
        return {"disabled_scopes": sorted(state.disabled_scopes)}

    @mcp.tool(
        name="memory_scope_enable",
        description=("Re-enable a previously disabled scope for this session."),
    )
    async def memory_scope_enable(scope: str) -> dict[str, Any]:
        _advance_turn(state, recorder)
        clean = validate_scope(scope)
        state.enable(clean)
        recorder.record("scope_enable", scope=clean)
        return {"disabled_scopes": sorted(state.disabled_scopes)}

    # ---- memory_rename_scope ---------------------------------------------

    @mcp.tool(
        name="memory_rename_scope",
        description=(
            "Replace `old_scope` with `new_scope` across active memories "
            "(and tombstones, by default). The cheap fix for typo'd or "
            "deprecated scopes — e.g. `projct:foo` -> `projects:foo` "
            "after a misspell, or `infra` -> `infrastructure` after "
            "settling on the long form. Bumps `updated` on each touched "
            "memory; preserves `last_verified_at` (the body's claims "
            "didn't change, only the tag did). Memories that already "
            "carry `new_scope` get `old_scope` removed without "
            "duplicating `new_scope`. Returns "
            "`{active: [ids], tombstoned: [ids]}` for the records that "
            "were actually modified. Pass `include_tombstones=False` to "
            "leave the removal audit log untouched. Use after "
            "memory_health surfaces a typo in `rare_scopes`."
        ),
    )
    async def memory_rename_scope(
        old_scope: str,
        new_scope: str,
        include_tombstones: bool = True,
    ) -> dict[str, Any]:
        _advance_turn(state, recorder)
        clean_old = validate_scope(old_scope)
        clean_new = validate_scope(new_scope)
        if clean_old == clean_new:
            raise ValueError("old_scope and new_scope must differ")
        if config.scopes.allowed and clean_new not in set(config.scopes.allowed):
            raise ValueError(
                f"new_scope {clean_new!r} is not in the allowed list: "
                f"{sorted(config.scopes.allowed)}"
            )
        result = store.rename_scope(
            clean_old, clean_new, include_tombstones=include_tombstones
        )
        recorder.record(
            "rename_scope",
            old=clean_old,
            new=clean_new,
            include_tombstones=include_tombstones,
            active_count=len(result["active"]),
            tombstoned_count=len(result["tombstoned"]),
        )
        return {
            "old_scope": clean_old,
            "new_scope": clean_new,
            "active": result["active"],
            "tombstoned": result["tombstoned"],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_write_payload(
    *,
    content: str,
    scopes: list[str],
    confidence: str,
    source: str,
    allowed_scopes: list[str],
    category: str = "fact",
) -> dict[str, Any]:
    """Validate and normalise the kwargs for `Store.write`.

    Returns a dict suitable for `Store.write(**payload)`. Raises ValueError
    on any input problem so the model gets a clear error.
    """
    if not content or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not scopes:
        raise ValueError("scopes must contain at least one entry")

    clean_scopes = [validate_scope(s) for s in scopes]

    if allowed_scopes:
        allowed_set = set(allowed_scopes)
        unknown = [s for s in clean_scopes if s not in allowed_set]
        if unknown:
            raise ValueError(
                f"scope(s) not in allowed list: {unknown}. "
                f"Allowed: {sorted(allowed_scopes)}"
            )

    try:
        conf_enum = Confidence(confidence)
    except ValueError as exc:
        raise ValueError(
            f"confidence must be one of {[c.value for c in Confidence]}"
        ) from exc
    try:
        src_enum = Source(source)
    except ValueError as exc:
        raise ValueError(f"source must be one of {[s.value for s in Source]}") from exc

    if category not in _WRITE_CATEGORIES:
        raise ValueError(f"category must be one of {sorted(_WRITE_CATEGORIES)}")
    cat_enum = Category(category)

    return {
        "content": content,
        "scopes": clean_scopes,
        "confidence": conf_enum,
        "source": src_enum,
        "category": cat_enum,
    }


def _committed(  # type: ignore[no-untyped-def]
    memory,
    *,
    related: list[SimilarHit] | None = None,
    removed_related: list[SimilarHit] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Serialise a freshly-written Memory into the tool response shape.

    `related` carries any medium-overlap matches that didn't block the
    write — surfaced so the caller can still consider memory_update on a
    similar existing entry, just without a hard refusal.

    `removed_related` carries medium-overlap matches against tombstoned
    memories. Same shape as `related` plus the populated `removed_at` /
    `removed_reason` fields. Surfaced advisorily — the writer hasn't
    duplicated a previously-removed fact, but they're working in the
    same neighbourhood and may want to consult the removal reason.

    `warnings` is a list of canonical advisory codes — non-blocking
    flags surfaced on a successful commit. Currently:
    - ``"ambient_body_long"``: an ambient memory exceeded the
      `_AMBIENT_LONG_BODY_WORDS` threshold; consider splitting.
    Empty list omitted from the response so the shape stays minimal
    on the common no-warning case.
    """
    out: dict[str, Any] = {
        "status": "committed",
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "category": memory.category.value if memory.category is not None else None,
        "created": _isoformat(memory.created),
        "updated": _isoformat(memory.updated),
        "last_verified_at": _isoformat_optional(memory.last_verified_at),
    }
    if related:
        out["related"] = [_similar_to_dict(h) for h in related]
    if removed_related:
        out["removed_related"] = [_similar_to_dict(h) for h in removed_related]
    if warnings:
        out["warnings"] = list(warnings)
    return out


def _similar_to_dict(hit: SimilarHit) -> dict[str, Any]:
    """Serialise a SimilarHit to the tool response shape.

    `removed_at` / `removed_reason` are emitted only when populated —
    active hits keep the response shape lean by omitting the keys, while
    tombstone hits carry both. Consumers can branch on
    `"removed_reason" in hit` or on `relevance.endswith("-removed")`."""
    out: dict[str, Any] = {
        "id": hit.id,
        "scopes": hit.scopes,
        "confidence": hit.confidence.value,
        "snippet": hit.snippet,
        "similarity": hit.similarity,
        "relevance": hit.relevance,
        "created": _isoformat(hit.created),
        "updated": _isoformat(hit.updated),
    }
    if hit.removed_at is not None:
        out["removed_at"] = _isoformat(hit.removed_at)
    if hit.removed_reason is not None:
        out["removed_reason"] = hit.removed_reason
    return out


def _transient_to_dict(hit: TransientMatch) -> dict[str, Any]:
    """Serialize a transient-marker match for the tool response."""
    return {"marker": hit.marker, "snippet": hit.snippet}


def _isoformat(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _isoformat_optional(dt: datetime | None) -> str | None:
    """ISO-format `dt`, returning None when the input is None.

    Distinct from `_isoformat` because `None` is a meaningful response
    value for `last_verified_at` — "never verified" is a valid state, not
    an error. Returning the literal None lets JSON-serialisation produce
    `"last_verified_at": null` which the caller can branch on directly.
    """
    return None if dt is None else _isoformat(dt)


def _hit_to_dict(
    hit: MemoryHit,
    *,
    now: datetime,
    stale_after_days: int,
) -> dict[str, Any]:
    """Serialise a search hit, including the structured verification block.

    `now` and `stale_after_days` are threaded in (rather than being read
    from the clock here) so a multi-hit response uses one consistent
    "now" — preventing the awkward case where the first hit in a result
    set is judged fresh and the last is judged stale because we crossed
    a day boundary mid-loop. `last_verified_at` stays in the response
    as a raw timestamp for callers that already branch on it; the new
    `verification` field is the structured replacement.

    `staleness_verdict` is initialised here from verification +
    path_drift only; the commit-drift contribution is folded in by
    `_attach_commit_drift_counts` once the per-search timestamp list
    has been read. Initial verdict is correct for hits where commit
    drift isn't applicable (caller not in a repo, hit from a
    different repo, hit never verified) — those verdicts never get
    revisited.
    """
    verification = compute_verification_status(
        hit.last_verified_at, now=now, stale_after_days=stale_after_days
    )
    verdict = compute_staleness_verdict(
        verification=verification,
        path_drift_missing=hit.path_drift_missing,
        commit_drift_count=None,
    )
    return {
        "id": hit.id,
        "scopes": hit.scopes,
        "confidence": hit.confidence.value,
        "category": hit.category.value if hit.category is not None else None,
        "snippet": hit.snippet,
        "score": hit.score,
        "relevance": hit.relevance,
        "match_terms": hit.match_terms,
        "created": _isoformat(hit.created),
        "updated": _isoformat(hit.updated),
        "last_verified_at": _isoformat_optional(hit.last_verified_at),
        "verification": verification.to_dict(),
        "path_drift_checked": hit.path_drift_checked,
        "path_drift_missing": hit.path_drift_missing,
        "staleness_verdict": verdict,
    }


def _attach_commit_drift_counts(  # type: ignore[no-untyped-def]
    out: list[dict[str, Any]],
    hits: list[MemoryHit],
    memories,
    *,
    caller_origin: Origin,
) -> None:
    """Mutate `out` in-place, adding `commit_drift_count` to each hit
    whose memory is anchored to the caller's current repo and has been
    verified at some point.

    The signal mirrors `path_drift_checked` / `path_drift_missing`: a
    cheap integer surfaced on every hit so the model can self-triage
    which hit to expand without a memory_show round-trip. Cost is
    bounded — one `commit_author_timestamps` call for the whole search,
    then a per-hit `bisect_right` against the sorted timestamp list.
    Independent of result count.

    Omitted (key absent from the dict, not set to null) when:

    - caller is not currently in any repo (`caller_origin.repo` is None),
    - git was unreachable in the caller's cwd,
    - the hit's memory has no `origin.repo` (legacy / global memory),
    - the hit's memory's repo doesn't match the caller's,
    - the hit's memory has never been verified (no anchor to count from).

    Absence-as-signal mirrors `path_drift`'s null-when-clean contract
    and keeps the hit shape uniform: a consumer either sees the field
    or doesn't, no third "unknown" branch to filter.
    """
    if caller_origin.repo is None or caller_origin.cwd is None:
        return
    timestamps = commit_author_timestamps(Path(caller_origin.cwd))
    if timestamps is None:
        return
    timestamps_sorted = sorted(timestamps)
    # Build the id → origin.repo side-map from the in-memory `memories`
    # list (already loaded by the caller for the search itself), avoiding
    # a second store round-trip per hit.
    origin_repo_by_id: dict[str, str | None] = {
        m.id: (m.origin.repo if m.origin else None) for m in memories
    }
    for hit_dict, hit in zip(out, hits):
        if hit.last_verified_at is None:
            continue
        origin_repo = origin_repo_by_id.get(hit.id)
        if origin_repo is None:
            continue
        if not repos_match(origin_repo, caller_origin.repo):
            continue
        since = hit.last_verified_at
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        # bisect_right on the ascending list gives the first index
        # strictly greater than `since`; len - idx is the count of
        # commits strictly after the verify timestamp. Equal-timestamp
        # commits are not counted as drift, matching the health
        # rollup's semantics.
        idx = bisect.bisect_right(timestamps_sorted, since)
        count = len(timestamps_sorted) - idx
        hit_dict["commit_drift_count"] = count
        # Recompute the verdict now that we have the commit-drift
        # contribution. `_hit_to_dict` initialised it without that
        # input; the upgrade only fires for hits where the count was
        # actually applicable.
        verification_dict = hit_dict["verification"]
        verification_status = verification_dict["status"]
        verdict_required = verification_status in {"never", "stale"}
        if verdict_required:
            hit_dict["staleness_verdict"] = "spot_check_required"
        elif count > 0 or hit_dict["path_drift_missing"] > 0:
            hit_dict["staleness_verdict"] = "spot_check_recommended"
        else:
            hit_dict["staleness_verdict"] = "fresh"


def _summary_to_dict(
    summary: MemorySummary,
    *,
    now: datetime,
    stale_after_days: int,
) -> dict[str, Any]:
    """Serialise a memory_list summary with verification status attached.

    Same contract as `_hit_to_dict`: `now` injected for consistency,
    `verification` carries the actionable signal, raw `last_verified_at`
    kept for back-compat. Listing is the cheap-triage view, exactly the
    surface where staleness should be visible — a curator scrolling the
    list shouldn't have to call memory_show to see whether a row is
    fresh.

    `staleness_verdict` here intentionally only reflects verification
    + the commit-drift signal isn't computed at the list level (the
    list view never loads bodies for path_drift, never resolves the
    per-row repo for commit_drift). A list-row verdict of "fresh"
    therefore means "calendar-fresh"; a row landing in
    spot_check_recommended via the list view would imply the
    body-level drift signal was already known, which it isn't here.
    Effectively the list verdict collapses to fresh ↔ verification
    fresh, spot_check_required ↔ never|stale.
    """
    verification = compute_verification_status(
        summary.last_verified_at, now=now, stale_after_days=stale_after_days
    )
    verdict = compute_staleness_verdict(
        verification=verification,
        path_drift_missing=0,
        commit_drift_count=None,
    )
    return {
        "id": summary.id,
        "scopes": summary.scopes,
        "confidence": summary.confidence.value,
        "category": summary.category.value if summary.category is not None else None,
        "summary": summary.summary,
        "created": _isoformat(summary.created),
        "updated": _isoformat(summary.updated),
        "last_verified_at": _isoformat_optional(summary.last_verified_at),
        "verification": verification.to_dict(),
        "staleness_verdict": verdict,
    }


def _tombstone_summary_to_dict(summary: TombstonedSummary) -> dict[str, Any]:
    """Same shape as `_summary_to_dict` plus removal metadata.

    Mirroring the active shape lets a curator iterate uniformly: a row
    has `removed` set if and only if it's a tombstone. `removed_session`
    is `null` for legacy tombstones written before that field shipped.
    """
    return {
        "id": summary.id,
        "scopes": summary.scopes,
        "confidence": summary.confidence.value,
        "category": summary.category.value if summary.category is not None else None,
        "summary": summary.summary,
        "created": _isoformat(summary.created),
        "updated": _isoformat(summary.updated),
        "last_verified_at": _isoformat_optional(summary.last_verified_at),
        "removed": _isoformat(summary.removed),
        "removed_reason": summary.removed_reason,
        "removed_session": summary.removed_session,
    }


def _memory_to_dict(  # type: ignore[no-untyped-def]
    memory,
    *,
    now: datetime,
    stale_after_days: int,
) -> dict[str, Any]:
    """Full memory shape used by `memory_list(with_bodies=True)`.

    Same fields as `memory_show` plus the `summary` line so a consumer
    can treat the response uniformly with the body-less `memory_list`
    shape. Includes the `verification` block for parity with the rest
    of the retrieval surface — a `with_bodies=True` listing carries the
    same staleness signal a `memory_show` would.
    """
    from .models import first_summary_line

    verification = compute_verification_status(
        memory.last_verified_at, now=now, stale_after_days=stale_after_days
    )
    drift = detect_path_drift(memory.body, verified_paths=memory.verified_paths)
    verdict = compute_staleness_verdict(
        verification=verification,
        path_drift_missing=len(drift.missing),
        commit_drift_count=None,
    )
    return {
        "id": memory.id,
        "scopes": memory.scopes,
        "confidence": memory.confidence.value,
        "source": memory.source.value,
        "category": memory.category.value if memory.category is not None else None,
        "summary": first_summary_line(memory.body),
        "body": memory.body,
        "created": _isoformat(memory.created),
        "updated": _isoformat(memory.updated),
        "last_verified_at": _isoformat_optional(memory.last_verified_at),
        "verification": verification.to_dict(),
        "staleness_verdict": verdict,
        "origin": _origin_to_dict(memory.origin),
    }


def _advance_turn(
    state: SessionState,
    recorder: Recorder,
    *,
    override_ids: set[str] | None = None,
) -> None:
    """Bump the per-session turn counter and auto-commit any use-tokens
    that crossed their TTL.

    Called at the entry of every memory_* tool handler so the
    auto-`record_use` flow has a stable monotonic clock and the
    bookkeeping fires even on calls that don't issue new tokens
    (e.g. `memory_write`, `memory_health`). Telemetry-disabled
    recorders no-op, so this is safe to call unconditionally.

    `override_ids` is used by the `memory_record_use` path: ids the
    caller is explicitly recording for shouldn't be auto-committed
    as `applied` first — the explicit outcome wins. The session's
    `consume_old_tokens` accepts the same set so the exclusion is
    structural rather than racey.

    Auto-committed ids land in the event log under
    `kind="use", outcome="applied", auto=True` so health analysis
    can distinguish auto-applied from explicit-applied if a future
    rollup wants to. The current `compute_health` already counts them
    in the same `applied_count` slot — auto IS the signal that the
    model probably used the memory, the same as if it had called
    record_use itself.
    """
    state.advance_turn()
    auto_ids = state.consume_old_tokens(override_ids=override_ids)
    if auto_ids:
        recorder.record(
            "use",
            ids=list(auto_ids),
            outcome="applied",
            auto=True,
        )


def _attach_use_tokens(
    out: list[dict[str, Any]],
    state: SessionState,
) -> None:
    """Mint a `use_token` for each hit dict and inject it into the dict.

    Tokens are minted in bulk (`state.issue_use_tokens`) rather than
    per-hit to keep the secret-generation cost off the response's
    critical path on large result sets. Re-issuing for an id whose
    previous token is still pending is fine — the new token replaces
    the old, and the old one can never be exchanged.
    """
    if not out:
        return
    ids = [h["id"] for h in out]
    tokens = state.issue_use_tokens(ids)
    for h in out:
        h["use_token"] = tokens[h["id"]]


def _origin_to_dict(origin: Origin | None) -> dict[str, Any] | None:
    """Serialize an Origin for tool responses, or None if absent.

    Empty fields are stripped so the response carries only the data that
    was actually captured at write time. A memory written before this
    feature shipped returns None; a memory written outside any git repo
    returns `{"cwd": "..."}` without `repo` or `branch` keys.
    """
    if origin is None:
        return None
    payload = origin.model_dump(mode="json", exclude_none=True)
    return payload or None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point. By default runs the MCP server over stdio
    (`bettermemory`). Subcommands provide offline tooling: `bettermemory
    health` prints the aggregate report, mirroring the `memory_health`
    tool in human-readable form."""
    import argparse

    from . import __version__

    parser = argparse.ArgumentParser(
        prog="bettermemory",
        description=(
            "Local file-backed memory MCP server with retrieval-on-demand. "
            "Run with no arguments to start the MCP server over stdio."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bettermemory {__version__}",
    )
    sub = parser.add_subparsers(dest="cmd")

    health_parser = sub.add_parser(
        "health", help="Print the aggregate memory health report."
    )
    health_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    health_parser.add_argument(
        "--days",
        type=int,
        default=30,
        help=(
            "Window in days for the dead-weight cutoff. Memories created "
            "more than this many days ago with no `applied` events are "
            "flagged. Default: 30."
        ),
    )
    health_parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="How many heavily-used memories to list. Default: 10.",
    )
    health_parser.add_argument(
        "--min-applied",
        type=int,
        default=None,
        help=(
            "Minimum applied_count for inclusion in heavily_used. Default "
            "comes from config.toml `behavior.heavily_used_min_applied` "
            "(typically 3). Lower to 1 on a fresh store to see anything "
            "that's been applied at least once."
        ),
    )

    doctor_parser = sub.add_parser(
        "doctor",
        help=(
            "Diagnose install state. Runs a series of checks: binary on "
            "PATH, config loadable, storage dir writable, memories parse, "
            "event log writable, semantic-dedup extras present (when "
            "enabled), MCP client configs cross-checked against the "
            "currently-resolved binary path. Exits 0/1/2 for ok/warn/fail."
        ),
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (machine-readable) instead of human-readable text.",
    )

    init_parser = sub.add_parser(
        "init",
        help=(
            "Onboard a fresh install: print the MCP config snippet, or "
            "auto-patch a known client's config. Idempotent."
        ),
    )
    init_parser.add_argument(
        "--client",
        type=str,
        default=None,
        choices=["claude-code", "claude-desktop", "cursor", "continue", "cline"],
        help=(
            "Auto-patch the named client's MCP config. Without this "
            "flag, init runs in show-and-tell mode: prints the snippet "
            "and the common config locations so you can copy by hand."
        ),
    )
    init_parser.add_argument(
        "--print-only",
        action="store_true",
        help=(
            "Just print the JSON snippet (and target path, when --client "
            "is set) without writing anything. Useful for piping into "
            "jq or for review before applying."
        ),
    )
    init_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a structured JSON view (binary path, snippet, known clients).",
    )
    init_parser.add_argument(
        "--name",
        type=str,
        default=None,
        help=(
            "Server key under `mcpServers`. Default: `bettermemory` "
            "(specific enough to never collide with another MCP server). "
            "Override only if you have a strong reason — Claude Code's "
            "tool names are prefixed with this key."
        ),
    )
    init_parser.add_argument(
        "--with-addendum",
        action="store_true",
        help=(
            "Also print docs/system_prompt.md (the long-form policy). "
            "The MCP `instructions` block carries the core rules at "
            "the system-prompt level on every compliant client, but "
            "Claude Code truncates it at ~1.8KB. Print the addendum "
            "and paste into your CLAUDE.md to keep the writing-"
            "discipline / scope-hygiene / verification-ceremony "
            "detail in scope. The Claude Code plugin ships the same "
            "content as a SKILL.md — you don't need both."
        ),
    )
    init_parser.add_argument(
        "--config-path",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Override the default target file for --client. Use this "
            "to write into a project-scoped MCP config instead of the "
            "user-scoped default."
        ),
    )

    migrate_parser = sub.add_parser(
        "migrate",
        help=(
            "One-shot data migrations. Use `migrate origin` to backfill "
            "the origin field on memories written before that field "
            "existed."
        ),
    )
    migrate_sub = migrate_parser.add_subparsers(dest="migrate_cmd")
    origin_parser = migrate_sub.add_parser(
        "origin",
        help=(
            "Backfill origin frontmatter on legacy memories. Idempotent: "
            "memories that already have an origin field are skipped."
        ),
    )
    origin_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    origin_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help=(
            "Force-tag every legacy memory with this remote URL. Use "
            "when the auto-inference from the parent directory isn't "
            "right (e.g. global memory dir that you know belongs to one "
            "repo)."
        ),
    )
    origin_parser.add_argument(
        "--scope-repo",
        action="append",
        default=[],
        metavar="SCOPE=URL",
        help=(
            "Route memories by scope: tag any memory carrying SCOPE "
            "with the given remote URL. Repeat for multiple scopes "
            "(e.g. --scope-repo projects:foo=git@github.com:me/foo.git "
            "--scope-repo projects:bar=git@github.com:me/bar.git). "
            "Memories whose scopes match nothing in the map fall through "
            "to --repo (if given) or are left untagged. The right tool "
            "for a global memory dir whose memories already use "
            "projects:<name> tags."
        ),
    )

    export_parser = sub.add_parser(
        "export",
        help=(
            "Dump all active memories (and tombstones, by default) to a "
            "self-describing JSON document. The format is round-trippable "
            "and intended for backup, migration between machines, or "
            "feeding an external indexer. Writes to stdout unless "
            "--output is given."
        ),
    )
    export_parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Write the export to PATH instead of stdout. Use this for "
            "scripted backups (`bettermemory export -o backup.json`)."
        ),
    )
    export_parser.add_argument(
        "--no-tombstones",
        action="store_true",
        help=(
            "Skip tombstoned memories. By default the export includes "
            "them so a restored archive carries the same removal-reason "
            "audit trail; use this when you only want the live set."
        ),
    )
    export_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to memories tagged with at least one of the given "
            "scopes. Repeat to widen the filter. Applies to both active "
            "and tombstoned records."
        ),
    )

    tombstones_parser = sub.add_parser(
        "tombstones",
        help=(
            "Inspect and prune the tombstone (removed-memory) audit log. "
            "Subcommands: list, prune."
        ),
    )
    tombstones_sub = tombstones_parser.add_subparsers(dest="tombstones_cmd")

    tlist_parser = tombstones_sub.add_parser(
        "list", help="Print all tombstones with removal metadata."
    )
    tlist_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    tlist_parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="SCOPE",
        help=(
            "Filter to tombstones tagged with at least one of the given "
            "scopes. Repeat to widen the filter."
        ),
    )

    tprune_parser = tombstones_sub.add_parser(
        "prune",
        help=(
            "Hard-delete tombstones older than --older-than days. "
            "Active memories are unaffected. Default value comes from "
            "config.toml `behavior.tombstone_retention_days`; if that's 0 "
            "(the default), --older-than is required."
        ),
    )
    tprune_parser.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "Cutoff in days. Tombstones whose `removed` timestamp is older "
            "than this are deleted. Required if no default is configured."
        ),
    )
    tprune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without touching disk.",
    )
    tprune_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )

    args = parser.parse_args()
    if args.cmd == "health":
        _cli_health(
            json_out=args.json,
            days=args.days,
            top_k=args.top_k,
            min_applied=args.min_applied,
        )
        return
    if args.cmd == "doctor":
        from .doctor import cli_doctor

        raise SystemExit(cli_doctor(json_out=args.json))
    if args.cmd == "init":
        from pathlib import Path as _Path

        from .init import cli_init

        cli_init(
            client=args.client,
            print_only=args.print_only,
            json_out=args.json,
            name=args.name,
            with_addendum=args.with_addendum,
            config_path=_Path(args.config_path) if args.config_path else None,
        )
        return
    if args.cmd == "migrate":
        if args.migrate_cmd == "origin":
            scope_repo_map: dict[str, str] = {}
            for entry in args.scope_repo:
                if "=" not in entry:
                    parser.error(f"--scope-repo expects SCOPE=URL, got: {entry!r}")
                scope, url = entry.split("=", 1)
                scope = scope.strip()
                url = url.strip()
                if not scope or not url:
                    parser.error(
                        f"--scope-repo expects non-empty SCOPE and URL, got: {entry!r}"
                    )
                scope_repo_map[scope] = url
            _cli_migrate_origin(
                dry_run=args.dry_run,
                force_repo=args.repo,
                scope_repo_map=scope_repo_map,
            )
            return
        migrate_parser.print_help()
        return
    if args.cmd == "tombstones":
        if args.tombstones_cmd == "list":
            _cli_tombstones_list(json_out=args.json, scopes=args.scope or None)
            return
        if args.tombstones_cmd == "prune":
            _cli_tombstones_prune(
                older_than_days=args.older_than,
                dry_run=args.dry_run,
                json_out=args.json,
                parser=parser,
            )
            return
        tombstones_parser.print_help()
        return
    if args.cmd == "export":
        _cli_export(
            output=args.output,
            include_tombstones=not args.no_tombstones,
            scopes=args.scope or None,
        )
        return

    _cli_serve()


def _cli_serve() -> None:
    """The default no-arg behaviour: run the MCP server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    log.info("memory directory: %s", directory)
    log.info(
        "telemetry: %s (event log at %s/.events.jsonl)",
        "on" if config.telemetry.enabled else "off",
        directory,
    )
    log.info(
        "system prompt: server-level MCP `instructions` block carries "
        "the core policy; on Claude Code the block is truncated at "
        "~1.8KB so the long-form addendum (docs/system_prompt.md) or "
        "the plugin's SKILL.md carries the writing-discipline / "
        "scope-hygiene tail"
    )

    mcp = build_server(config=config, store=store, state=get_state())
    mcp.run("stdio")


def _cli_health(
    *,
    json_out: bool,
    days: int,
    top_k: int,
    min_applied: int | None = None,
) -> None:
    """`bettermemory health` — print the aggregate report."""
    from .health import render_json, render_text

    config = load_config()
    directory = config.resolved_directory()
    # `--min-applied` overrides the config default; fall through to the
    # configured value when the flag wasn't passed. Avoids forcing the user
    # to pass the same number to every CLI invocation.
    threshold = (
        min_applied
        if min_applied is not None
        else config.behavior.heavily_used_min_applied
    )
    report = report_for_directory(
        directory,
        window_days=days,
        heavily_used_top_k=top_k,
        heavily_used_min_applied=threshold,
        verification_stale_days=config.behavior.verification_stale_days,
        # Capture caller origin so the CLI rendering picks up the
        # commit-drift bucket when run from inside a project whose
        # memories live in this store.
        caller_origin=capture_origin(),
    )
    sys.stdout.write(render_json(report) if json_out else render_text(report))


def _cli_tombstones_list(*, json_out: bool, scopes: list[str] | None) -> None:
    """`bettermemory tombstones list` — print removed memories."""
    import json as _json

    config = load_config()
    store = Store(config.resolved_directory())
    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    summaries = store.list_tombstones(scopes=scopes)

    if json_out:
        sys.stdout.write(
            _json.dumps(
                [
                    {
                        "id": s.id,
                        "scopes": s.scopes,
                        "summary": s.summary,
                        "created": _isoformat(s.created),
                        "removed": _isoformat(s.removed),
                        "removed_reason": s.removed_reason,
                        "removed_session": s.removed_session,
                    }
                    for s in summaries
                ],
                indent=2,
            )
            + "\n"
        )
        return

    if not summaries:
        sys.stdout.write("No tombstones.\n")
        return

    sys.stdout.write(f"Tombstones ({len(summaries)}):\n")
    for s in summaries:
        sess = s.removed_session or "<no session>"
        sys.stdout.write(
            f"  {s.id} [removed={_isoformat(s.removed)}, "
            f"session={sess}] {','.join(s.scopes)}: {s.summary}\n"
            f"    reason: {s.removed_reason}\n"
        )


def _cli_tombstones_prune(
    *,
    older_than_days: int | None,
    dry_run: bool,
    json_out: bool,
    parser: Any,
) -> None:
    """`bettermemory tombstones prune` — hard-delete old tombstones."""
    import json as _json
    from datetime import timedelta

    config = load_config()
    days = (
        older_than_days
        if older_than_days is not None
        else config.behavior.tombstone_retention_days
    )
    if days is None or days <= 0:
        # Hard refusal — pruning everything by accident would be a foot-gun.
        parser.error(
            "--older-than is required (no default configured). Pass an "
            "explicit cutoff in days, or set "
            "`behavior.tombstone_retention_days` in config.toml."
        )
    cutoff = timedelta(days=days)

    store = Store(config.resolved_directory())

    if dry_run:
        # Use load_tombstones to inspect; don't call prune which deletes.
        from .models import utcnow

        now = utcnow()
        candidates = [t for t in store.load_tombstones() if t.removed < (now - cutoff)]
        ids = [t.id for t in candidates]
        if json_out:
            sys.stdout.write(
                _json.dumps({"would_delete": ids, "cutoff_days": days}, indent=2) + "\n"
            )
            return
        if not ids:
            sys.stdout.write(f"No tombstones older than {days} days.\n")
            return
        sys.stdout.write(
            f"Would delete {len(ids)} tombstone(s) older than {days} days:\n"
        )
        for t in candidates:
            sys.stdout.write(
                f"  {t.id} [removed={_isoformat(t.removed)}]: {t.removed_reason}\n"
            )
        sys.stdout.write("(Dry run — re-run without --dry-run to apply.)\n")
        return

    pruned_ids = store.prune_tombstones(cutoff)
    if json_out:
        sys.stdout.write(
            _json.dumps({"deleted": pruned_ids, "cutoff_days": days}, indent=2) + "\n"
        )
        return
    if not pruned_ids:
        sys.stdout.write(f"No tombstones older than {days} days.\n")
        return
    sys.stdout.write(
        f"Deleted {len(pruned_ids)} tombstone(s) older than {days} days:\n"
    )
    for memory_id in pruned_ids:
        sys.stdout.write(f"  {memory_id}\n")


def _cli_export(
    *,
    output: str | None,
    include_tombstones: bool,
    scopes: list[str] | None,
) -> None:
    """`bettermemory export` — dump active (and optionally tombstoned)
    memories to a self-describing JSON document.

    Format (`format_version: 1`):

        {
          "format_version": 1,
          "exported_at": "2026-05-09T12:34:56Z",
          "source_directory": "/Users/me/.claude-memory",
          "active_memories":     [<full Memory dict>, ...],
          "tombstoned_memories": [<full TombstonedMemory dict>, ...]
        }

    `tombstoned_memories` is omitted entirely when --no-tombstones is
    passed (vs. emitted as []) so a consumer can distinguish "not
    requested" from "no tombstones present". Each memory dict mirrors
    the Pydantic model — id, created, updated, scopes, confidence,
    source, body, origin, last_verified_at — and tombstones add
    removed / removed_reason / removed_session.

    The shape is intended to be round-trippable: a future
    `bettermemory import` can recreate active records and tombstones
    from this document with no loss. Bump format_version on any
    breaking change.
    """
    import json as _json
    from pathlib import Path as _Path

    config = load_config()
    directory = config.resolved_directory()
    store = Store(directory)

    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    scope_set = set(scopes) if scopes else None

    active = store.load_all()
    if scope_set is not None:
        active = [m for m in active if scope_set.intersection(m.scopes)]

    payload: dict[str, Any] = {
        "format_version": 1,
        "exported_at": _isoformat(utcnow()),
        "source_directory": str(directory),
        "active_memories": [m.model_dump(mode="json") for m in active],
    }
    tombstoned_count = 0
    if include_tombstones:
        tombstoned = store.load_tombstones()
        if scope_set is not None:
            tombstoned = [t for t in tombstoned if scope_set.intersection(t.scopes)]
        payload["tombstoned_memories"] = [t.model_dump(mode="json") for t in tombstoned]
        tombstoned_count = len(tombstoned)

    text = _json.dumps(payload, indent=2)

    if output:
        out_path = _Path(output)
        out_path.write_text(text + "\n", encoding="utf-8")
        summary = f"Exported {len(active)} active memories"
        if include_tombstones:
            summary += f" + {tombstoned_count} tombstones"
        summary += f" to {out_path}\n"
        # Status line goes to stderr so `-o` callers can still pipe
        # the file path on stdout if they want; consistent with how
        # most CLI tools split status from data.
        sys.stderr.write(summary)
        return

    sys.stdout.write(text + "\n")


def _cli_migrate_origin(
    *,
    dry_run: bool,
    force_repo: str | None,
    scope_repo_map: dict[str, str],
) -> None:
    """`bettermemory migrate origin` — backfill origin on legacy memories."""
    from .migrate import (
        infer_origin_for_memory_dir,
        migrate_origin_in_directory,
    )

    config = load_config()
    memory_dir = config.resolved_directory()

    print(f"Scanning {memory_dir}...")
    print()

    if scope_repo_map:
        print("Routing by scope:")
        for scope, url in scope_repo_map.items():
            print(f"  {scope:<32} -> {url}")
        print()

    if force_repo is not None:
        print(f"Fallback: untagged memories -> {force_repo!r}")
    else:
        inferred = infer_origin_for_memory_dir(memory_dir)
        if scope_repo_map and inferred is None:
            print(
                "Fallback: untagged memories left alone "
                "(no --repo and no auto-inference)."
            )
        elif scope_repo_map is None or not scope_repo_map:
            if inferred is None:
                print(
                    f"  Parent of memory dir: {memory_dir.parent}\n"
                    f"  No git remote detected.\n"
                    f"\n"
                    f"This appears to be a global memory directory — "
                    f"memories here probably came from many projects, "
                    f"and tagging them all with one repo would be "
                    f"misinformation. Nothing to do.\n"
                    f"\n"
                    f"Options:\n"
                    f"  --repo <url>                       "
                    f"force-tag every memory\n"
                    f"  --scope-repo projects:foo=<url>    "
                    f"route by scope (multi)"
                )
                return
            print(f"  Inferred repo:   {inferred.repo}")
            print(f"  cwd:             {inferred.cwd}")
            print("  branch:          (left null — original branch unknown)")

    print()
    report = migrate_origin_in_directory(
        memory_dir,
        force_repo=force_repo,
        scope_repo_map=scope_repo_map or None,
        dry_run=dry_run,
    )

    print("Results:")
    print(f"  Scanned:           {report.scanned}")
    print(f"  Already had origin: {report.already_had_origin}")
    print(f"  {'Would update' if dry_run else 'Updated':<18} {report.updated}")
    if report.malformed:
        print(f"  Malformed (skipped): {len(report.malformed)}")
        for path in report.malformed[:5]:
            print(f"    - {path}")
        if len(report.malformed) > 5:
            print(f"    ... and {len(report.malformed) - 5} more")

    if dry_run and report.updated:
        print()
        print("(Dry run — no changes written. Re-run without --dry-run to apply.)")


# Re-export the prompt for consumers who import the package.
__all__ = ["build_server", "main", "SYSTEM_PROMPT_ADDENDUM"]
