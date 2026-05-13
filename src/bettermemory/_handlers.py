"""MCP tool handlers as methods on `ToolHandlers`.

Each method is a thin handler: validate input via the Pydantic models,
call into `store` / `search`, emit one event to the `Recorder`, return a
JSON-serializable dict shaped by the `ResponseBuilder`. The recorder is
best-effort — telemetry failures are logged but never propagate up into
a tool call.

`server._register_tools` instantiates one `ToolHandlers` per server,
wires the per-tool descriptions (the `DESC_*` constants below), and
registers each method against the FastMCP instance. Tests reach handlers
via `mcp._tool_manager.get_tool(name).fn` — `fn` ends up being the bound
method, and `inspect.signature` strips `self`, so the JSON schema and
call surface are identical to the prior in-closure shape.
"""

from __future__ import annotations

from typing import Any, TypeAlias

from mcp.server.fastmcp import Context as _FastMCPContext

from ._response import ResponseBuilder, isoformat, isoformat_optional
from .config import Config
from .durability import find_transient_markers
from .events import Recorder, iter_all_events
from .health import curation_counts, report_for_directory
from .models import (
    Category,
    Confidence,
    SimilarHit,
    Source,
    is_valid_ulid,
    utcnow,
    validate_scope,
)
from .origin import Origin, capture as capture_origin, should_include_for_caller
from .scope_match import (
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)
from .search import find_similar, find_similar_tombstones, search as run_search
from .session import SessionSource, SessionState
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


# Local alias filling FastMCP's three generic params with Any — the handlers
# only ever read `ctx.client_id`, never the typed lifespan/request/session
# data, so unconstrained generics are the right shape. Aliasing once via
# `TypeAlias` (not a bare runtime assignment) keeps every handler
# signature readable AND keeps strict checkers happy — a plain
# `Context = X[Any, ...]` would type-check on mypy but trip
# "Variable not allowed in type expression" on Pyright/Pylance.
Context: TypeAlias = _FastMCPContext[Any, Any, Any]


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
# the memory makes. See `models.Category` for the persisted enum.
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
# Tool descriptions — model-facing strings. Kept as module-level constants
# (rather than docstrings or inline at the registration site) so the
# wiring layer in `server._register_tools` stays a short index and the
# strings live next to the handler bodies they describe.
# ---------------------------------------------------------------------------


DESC_MEMORY_SEARCH = (
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
    'when its relevance is "high" — collapses the common search-then-show '
    "round trip into one call, and surfaces the full `path_drift` "
    "report (with the actual missing paths) on the expanded hit. "
    "The expanded hit also carries a `commit_drift` block "
    "(`status: 'clean' | 'drift'` plus a `commits_since_verify` "
    "count) when the caller's current repo matches the memory's "
    "origin — non-zero is the cue to spot-check even when "
    "`verification.status` reads fresh, because the project has "
    "moved since the last memory_verify. Skip `expand_top` when "
    "you only need to triage. "
    "By default (`auto_scope=True`), results are filtered to "
    "memories written from the current repository — cross-project "
    "memories are excluded. Memories written outside any repo "
    "(`origin.repo is None`) pass through as global. Set "
    "`auto_scope=False` to explicitly include cross-project "
    "matches — say so in the reply so the user knows you reached "
    "outside the current scope. Honors session-disabled scopes "
    "set via memory_scope_disable."
)


DESC_MEMORY_SHOW = (
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
)


DESC_MEMORY_WRITE = (
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
)


DESC_MEMORY_WRITE_CONFIRM = (
    "Commit a memory_write that returned status='pending'. "
    "Pass the pending_id from that response."
)


DESC_MEMORY_WRITE_CANCEL = (
    "Drop a pending memory_write without committing. "
    "Pass the pending_id from the original write response."
)


DESC_MEMORY_UPDATE = (
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
)


DESC_MEMORY_LIST = (
    "List active memories. By default returns one-line summaries "
    "(IDs, scopes, summary, no body) — cheap triage. "
    "Pass `with_bodies=True` to inline full bodies in one call; "
    "useful for small stores where N round trips of "
    "`list -> show -> show` would be wasteful. Don't reach for "
    "`with_bodies` casually — it pulls every memory in scope into "
    "your context, which is the failure mode this project exists "
    "to avoid. Filter by `scopes` if you only care about a subset."
)


DESC_MEMORY_REMOVE = (
    "Tombstone a memory. The file is moved to .tombstones/ with a "
    "removal reason and the originating session id — never hard-"
    "deleted. Use when a stored fact is wrong or no longer relevant. "
    "Tombstones remain searchable via memory_list_tombstones and "
    "are surfaced as `removed_matches` on memory_write when a new "
    "body looks similar to a previously-removed fact, so the "
    "lesson encoded in the removal reason isn't lost. Use "
    "memory_restore(id) to undo an accidental removal."
)


DESC_MEMORY_LIST_TOMBSTONES = (
    "List removed (tombstoned) memories. One-line summaries plus "
    "removal metadata (`removed`, `removed_reason`, "
    "`removed_session`) — body stripped, like memory_list. Use "
    'for curation passes ("what did I clear out last month?") or '
    "to investigate when the user asks 'I think I had a memory "
    "about X — what happened?'. Pass `scopes` to filter, like "
    "memory_list. Tombstones are sorted by `removed` descending — "
    "most-recently-removed first."
)


DESC_MEMORY_RESTORE = (
    "Bring a tombstoned memory back to the active set. Strips the "
    "removal frontmatter, moves the file out of .tombstones/, and "
    "preserves the original `created`, `updated`, and "
    "`last_verified_at` timestamps — the body didn't change while "
    "it was tombstoned, so the recency boost stays honest. Raises "
    "if the id is active (use memory_update for edits) or unknown. "
    "The original removal reason and session live on in the event "
    "log even after restore."
)


DESC_MEMORY_HEALTH = (
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
)


DESC_MEMORY_RECORD_USE = (
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
)


DESC_MEMORY_VERIFY = (
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
)


DESC_MEMORY_SCOPE_OVERVIEW = (
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
)


DESC_MEMORY_SCOPE_DISABLE = (
    "Disable a scope for the rest of this session. Subsequent "
    "memory_search and memory_list calls will exclude memories "
    "tagged with this scope. Useful when the user says 'this is "
    "unrelated to project X'. Resets when the server restarts."
)


DESC_MEMORY_SCOPE_ENABLE = "Re-enable a previously disabled scope for this session."


DESC_MEMORY_RENAME_SCOPE = (
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
)


# ---------------------------------------------------------------------------
# Validation + per-handler bookkeeping helpers.
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


# ---------------------------------------------------------------------------
# ToolHandlers — one method per MCP tool. `_register_tools` instantiates
# this once per server and binds each method against the FastMCP instance.
# ---------------------------------------------------------------------------


class ToolHandlers:
    """One instance per server, captures the dependencies every handler
    needs. See module docstring for the wiring contract."""

    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        sessions: SessionSource,
        recorder: Recorder,
        responses: ResponseBuilder,
        semantic_model_factory: "SemanticModelFactory",
    ) -> None:
        self.config = config
        self.store = store
        self.sessions = sessions
        self.recorder = recorder
        self.responses = responses
        # Indirected so `_handlers.py` doesn't depend on the optional
        # `semantic` extra at import time. The factory takes `config` and
        # returns the model (or None for the Jaccard fallback).
        self._semantic_model_factory = semantic_model_factory

    # ---- memory_search ---------------------------------------------------

    async def memory_search(
        self,
        query: str,
        scopes: list[str] | None = None,
        max_results: int | None = None,
        expand_top: bool = False,
        auto_scope: bool = True,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        if max_results is None:
            max_results = self.config.behavior.default_max_results
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

        memories = self.store.load_all()
        hits = run_search(
            memories,
            query,
            scopes=scopes,
            excluded_scopes=set(state.disabled_scopes),
            repo_filter=repo_filter,
            max_results=max_results,
            half_life_days=self.config.behavior.recency_boost_half_life_days,
        )
        # Pin one `now` for the whole response so the verification verdict
        # is consistent across hits — the alternative (let each helper
        # call utcnow()) could land different status labels on adjacent
        # hits if we crossed a day boundary mid-loop.
        now = utcnow()
        out = [self.responses.hit_to_dict(h, now=now) for h in hits]

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
        self.responses.attach_commit_drift_counts(
            out, hits, memories, caller_origin=current_origin
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
                memory = self.store.load_one(hits[0].id)
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
                # `hit_to_dict` was based on `path_drift_missing` from
                # the search index (unloaded body) and may have skipped
                # claims surfaced by the actual body-level detection.
                top_verification = compute_verification_status(
                    memory.last_verified_at,
                    now=now,
                    stale_after_days=self.config.behavior.verification_stale_days,
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

        self.recorder.record(
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

    async def memory_show(self, id: str, ctx: Context | None = None) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        try:
            memory = self.store.load_one(id)
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
            stale_after_days=self.config.behavior.verification_stale_days,
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
        self.recorder.record(
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
            "created": isoformat(memory.created),
            "updated": isoformat(memory.updated),
            "last_verified_at": isoformat_optional(memory.last_verified_at),
            "verification": verification.to_dict(),
            "staleness_verdict": verdict,
            "body": memory.body,
            "origin": self.responses.origin_to_dict(memory.origin),
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

    async def memory_write(
        self,
        content: str,
        scopes: list[str],
        confidence: str = "medium",
        source: str = "explicit-statement",
        force: bool = False,
        acknowledge_transient: bool = False,
        acknowledge_scope_mismatch: bool = False,
        category: str = "fact",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        # `_advance_turn` keeps the per-session turn counter monotonic
        # for the auto-`record_use` flow even on calls that don't touch
        # search/show. We bump first so any pending use-tokens that
        # crossed their TTL get auto-committed before this write fires
        # its own event.
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        payload = _validate_write_payload(
            content=content,
            scopes=scopes,
            confidence=confidence,
            source=source,
            allowed_scopes=self.config.scopes.allowed,
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
            self.recorder.record(
                "write",
                status="transient_warning",
                scopes=payload["scopes"],
                forced=False,
                markers=[h.marker for h in transient_hits],
            )
            return {
                "status": "transient_warning",
                "markers": [
                    self.responses.transient_to_dict(h) for h in transient_hits
                ],
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
            existing_memories = self.store.load_all()
            mismatch = detect_scope_mismatch(
                body=payload["content"],
                declared_scopes=payload["scopes"],
                project_scopes=collect_project_scopes(existing_memories),
                project_roots=collect_project_roots(existing_memories),
            )
            if mismatch.has_mismatch:
                self.recorder.record(
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
            semantic_model = self._semantic_model_factory(self.config)
            high_threshold = (
                self.config.behavior.semantic_high_threshold
                if self.config.behavior.semantic_dedup
                else None
            )
            medium_threshold = (
                self.config.behavior.semantic_medium_threshold
                if self.config.behavior.semantic_dedup
                else None
            )
            similar = find_similar(
                payload["content"],
                self.store.load_all(),
                semantic_model=semantic_model,
                high_threshold=high_threshold,
                medium_threshold=medium_threshold,
            )
            high = [h for h in similar if h.relevance == "high"]
            if high:
                self.recorder.record(
                    "write",
                    status="duplicate",
                    scopes=payload["scopes"],
                    forced=False,
                    matches=[h.id for h in high],
                )
                return {
                    "status": "duplicate",
                    "matches": [self.responses.similar_to_dict(h) for h in high],
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
                self.store.load_tombstones(),
                semantic_model=semantic_model,
                high_threshold=high_threshold,
                medium_threshold=medium_threshold,
            )
            high_removed = [
                h for h in tombstone_similar if h.relevance == "high-removed"
            ]
            if high_removed:
                self.recorder.record(
                    "write",
                    status="previously_removed",
                    scopes=payload["scopes"],
                    forced=False,
                    removed_matches=[h.id for h in high_removed],
                )
                return {
                    "status": "previously_removed",
                    "removed_matches": [
                        self.responses.similar_to_dict(h) for h in high_removed
                    ],
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
        if self.config.behavior.require_write_confirmation:
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
                response["related"] = [
                    self.responses.similar_to_dict(h) for h in related
                ]
            if removed_related:
                response["removed_related"] = [
                    self.responses.similar_to_dict(h) for h in removed_related
                ]
            self.recorder.record(
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

        memory = self.store.write(**payload)
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
        self.recorder.record(
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
        return self.responses.committed(
            memory,
            related=related,
            removed_related=removed_related,
            warnings=warnings,
        )

    # ---- memory_write_confirm -------------------------------------------

    async def memory_write_confirm(
        self, pending_id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        pending = state.take_pending(pending_id)
        if pending is None:
            raise ValueError(
                f"no pending write with id {pending_id!r} (it may have "
                "expired or been already committed)"
            )
        memory = self.store.write(**pending.payload)
        self.recorder.record(
            "write_confirm",
            pending_id=pending_id,
            id=memory.id,
            scopes=memory.scopes,
        )
        return self.responses.committed(memory)

    # ---- memory_write_cancel --------------------------------------------

    async def memory_write_cancel(
        self, pending_id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        existed = state.cancel_pending(pending_id)
        self.recorder.record("write_cancel", pending_id=pending_id, existed=existed)
        return {"cancelled": pending_id, "existed": existed}

    # ---- memory_update ---------------------------------------------------

    async def memory_update(
        self,
        id: str,
        content: str | None = None,
        scopes: list[str] | None = None,
        confidence: str | None = None,
        category: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
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
            existing = self.store.load_one(id)
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc

        new_scopes = existing.scopes
        if scopes is not None:
            if not scopes:
                raise ValueError("scopes must contain at least one entry if provided")
            new_scopes = [validate_scope(s) for s in scopes]
            if self.config.scopes.allowed:
                allowed = set(self.config.scopes.allowed)
                unknown = [s for s in new_scopes if s not in allowed]
                if unknown:
                    raise ValueError(
                        f"scope(s) not in allowed list: {unknown}. "
                        f"Allowed: {sorted(self.config.scopes.allowed)}"
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
        updated = self.store.update(merged)
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
        self.recorder.record(
            "update",
            id=updated.id,
            fields=fields_changed,
            scopes=updated.scopes,
            confidence=updated.confidence.value,
            category=updated.category.value if updated.category is not None else None,
        )
        return self.responses.committed(updated)

    # ---- memory_list -----------------------------------------------------

    async def memory_list(
        self,
        scopes: list[str] | None = None,
        with_bodies: bool = False,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        if scopes:
            scopes = [validate_scope(s) for s in scopes]
        # Apply session-disabled scopes to listing too — consistency.
        excluded = set(state.disabled_scopes)
        # Single `now` for the whole listing — same reasoning as in
        # memory_search: consistent verification verdict across rows.
        now = utcnow()

        if with_bodies:
            out: list[dict[str, Any]] = []
            for memory in self.store.load_all():
                memory_scopes = set(memory.scopes)
                if excluded and (memory_scopes & excluded):
                    continue
                if scopes and not (memory_scopes & set(scopes)):
                    continue
                out.append(self.responses.memory_to_dict(memory, now=now))
            self.recorder.record(
                "list",
                scopes_filter=scopes,
                with_bodies=True,
                count=len(out),
                returned=[m["id"] for m in out],
            )
            return out

        out_summary: list[dict[str, Any]] = []
        for summary in self.store.list_summaries(scopes=scopes):
            if excluded and (set(summary.scopes) & excluded):
                continue
            out_summary.append(self.responses.summary_to_dict(summary, now=now))
        self.recorder.record(
            "list",
            scopes_filter=scopes,
            with_bodies=False,
            count=len(out_summary),
            returned=[s["id"] for s in out_summary],
        )
        return out_summary

    # ---- memory_remove ---------------------------------------------------

    async def memory_remove(
        self, id: str, reason: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        if not reason or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        try:
            tombstone_path = self.store.tombstone(
                id, reason, session_id=state.session_id
            )
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        self.recorder.record("remove", id=id, reason=reason)
        return {
            "removed": id,
            "tombstone_path": str(tombstone_path),
        }

    # ---- memory_list_tombstones -----------------------------------------

    async def memory_list_tombstones(
        self,
        scopes: list[str] | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        if scopes:
            scopes = [validate_scope(s) for s in scopes]
        excluded = set(state.disabled_scopes)
        out: list[dict[str, Any]] = []
        for summary in self.store.list_tombstones(scopes=scopes):
            if excluded and (set(summary.scopes) & excluded):
                continue
            out.append(self.responses.tombstone_summary_to_dict(summary))
        self.recorder.record(
            "list_tombstones",
            scopes_filter=scopes,
            count=len(out),
            returned=[s["id"] for s in out],
        )
        return out

    # ---- memory_restore --------------------------------------------------

    async def memory_restore(
        self, id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        try:
            memory = self.store.restore(id)
        except NotTombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        except ValueError:
            # _load_tombstone_path raises ValueError on a malformed file
            # (e.g. missing `created`). Surface verbatim — the message
            # tells the caller which field is missing.
            raise
        self.recorder.record(
            "restore",
            id=memory.id,
            scopes=memory.scopes,
        )
        return self.responses.committed(memory)

    # ---- memory_health ---------------------------------------------------

    async def memory_health(
        self,
        window_days: int = 30,
        heavily_used_top_k: int = 10,
        min_applied: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        # Falling through to the configured default lets the tool stay
        # ergonomic for the common case (don't pass anything, get the
        # tuned threshold) while still allowing a per-call override
        # ("show me everything that's been applied at least once on this
        # young store").
        threshold = (
            int(min_applied)
            if min_applied is not None
            else self.config.behavior.heavily_used_min_applied
        )
        report = report_for_directory(
            self.store.root,
            window_days=int(window_days),
            heavily_used_top_k=int(heavily_used_top_k),
            heavily_used_min_applied=threshold,
            verification_stale_days=self.config.behavior.verification_stale_days,
            # Pass caller_origin so the cwd-aware `commit_drift_debt`
            # rollup populates when the server is running inside a repo
            # whose memories live in this store.
            caller_origin=capture_origin(),
        )
        return report.to_dict()

    # ---- memory_record_use ----------------------------------------------

    async def memory_record_use(
        self,
        memory_ids: list[str],
        outcome: str,
        note: str | None = None,
        ctx: Context | None = None,
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
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder, override_ids=override_set)
        for mid in memory_ids:
            state.purge_use_token(mid)

        self.recorder.record(
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

    async def memory_verify(
        self,
        id: str,
        note: str | None = None,
        verified_paths: list[str] | None = None,
        verified_commits: list[str] | None = None,
        verified_versions: list[str] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
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
            memory = self.store.mark_verified(
                id,
                verified_paths=verified_paths,
                verified_commits=verified_commits,
                verified_versions=verified_versions,
            )
        except TombstonedError as exc:
            raise ValueError(str(exc)) from exc
        except MemoryNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        self.recorder.record(
            "verify",
            id=memory.id,
            last_verified_at=isoformat_optional(memory.last_verified_at),
            note=note,
            verified_paths=list(memory.verified_paths),
            verified_commits=list(memory.verified_commits),
            verified_versions=list(memory.verified_versions),
        )
        return {
            "verified": memory.id,
            "last_verified_at": isoformat_optional(memory.last_verified_at),
            "updated": isoformat(memory.updated),
            "verified_paths": list(memory.verified_paths),
            "verified_commits": list(memory.verified_commits),
            "verified_versions": list(memory.verified_versions),
        }

    # ---- memory_scope_overview ------------------------------------------

    async def memory_scope_overview(
        self,
        auto_scope: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        repo_filter: str | None = None
        current_origin: Origin | None = None
        if auto_scope:
            current_origin = capture_origin()
            repo_filter = current_origin.repo

        excluded = set(state.disabled_scopes)
        scope_counts: dict[str, int] = {}
        total = 0
        all_memories = self.store.load_all()
        for memory in all_memories:
            memory_scope_set = set(memory.scopes)
            if excluded and (memory_scope_set & excluded):
                continue
            if repo_filter is not None:
                # `should_include_for_caller` is the single definition of
                # "this memory belongs to this caller's project" — shared
                # with memory_search and the health rollups so the model
                # can't see "5 memories tagged projects:foo" here and
                # zero hits in search and have no way to reconcile that.
                if not should_include_for_caller(memory.origin, repo_filter):
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
            iter_all_events(self.store.root),
            window_days=30,
            verification_stale_days=self.config.behavior.verification_stale_days,
            caller_origin=current_origin,
        )

        self.recorder.record(
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

    # ---- memory_scope_disable -------------------------------------------

    async def memory_scope_disable(
        self, scope: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        clean = validate_scope(scope)
        state.disable(clean)
        self.recorder.record("scope_disable", scope=clean)
        return {"disabled_scopes": sorted(state.disabled_scopes)}

    # ---- memory_scope_enable --------------------------------------------

    async def memory_scope_enable(
        self, scope: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        clean = validate_scope(scope)
        state.enable(clean)
        self.recorder.record("scope_enable", scope=clean)
        return {"disabled_scopes": sorted(state.disabled_scopes)}

    # ---- memory_rename_scope --------------------------------------------

    async def memory_rename_scope(
        self,
        old_scope: str,
        new_scope: str,
        include_tombstones: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        clean_old = validate_scope(old_scope)
        clean_new = validate_scope(new_scope)
        if clean_old == clean_new:
            raise ValueError("old_scope and new_scope must differ")
        if self.config.scopes.allowed and clean_new not in set(
            self.config.scopes.allowed
        ):
            raise ValueError(
                f"new_scope {clean_new!r} is not in the allowed list: "
                f"{sorted(self.config.scopes.allowed)}"
            )
        result = self.store.rename_scope(
            clean_old, clean_new, include_tombstones=include_tombstones
        )
        self.recorder.record(
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


# A SemanticModelFactory is `(Config) -> Any | None` — the model object
# (when `semantic_dedup` is enabled and the extras are installed) or
# None for the Jaccard fallback. Kept as a callable rather than a hard
# import so `_handlers.py` doesn't pull in `semantic` (and through it
# `sentence-transformers`) at import time when semantic dedup is off.
from typing import Callable as _Callable  # noqa: E402

SemanticModelFactory: TypeAlias = _Callable[[Config], Any]
