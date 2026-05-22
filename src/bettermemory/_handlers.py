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
from .audit import DEFAULT_LOOKBACK_SECONDS, probe_for_miss
from .config import Config
from .durability import find_transient_markers
from .events import Recorder, iter_all_events, iter_events
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


# Cap on free-text `note` strings recorded on `memory_verify` and
# `memory_record_use` events. The web UI already enforces 500 chars on
# the /verify POST — this is the matching cap for the MCP entry points,
# so a hostile client (or a runaway model) can't inflate the JSONL
# event log with multi-megabyte notes. 500 chars covers any reasonable
# rationale ("verified against commit abc123" sort of thing); pasting
# whole transcripts belongs in a memory body, not in an event note.
_NOTE_MAX_LEN = 500


# ---------------------------------------------------------------------------
# Tool descriptions — model-facing strings. Kept as module-level constants
# (rather than docstrings or inline at the registration site) so the
# wiring layer in `server._register_tools` stays a short index and the
# strings live next to the handler bodies they describe.
# ---------------------------------------------------------------------------


DESC_MEMORY_SEARCH = (
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
    "set False for explicit cross-project queries.\n"
    "- `mode` (optional, default from config): `keyword`, `bm25`, "
    "`semantic` (needs embeddings extra), or `hybrid` (RRF of the "
    "first three). `hybrid` for paraphrase recall; `keyword` for "
    "literal-token queries.\n\n"
    "When a hit shapes your reply, briefly say so ('Using your "
    "stored preference for…') — the transparency requirement. "
    "Outcome is recorded automatically via the use_token within ~2 "
    "turns; only call memory_record_use to override "
    "(ignored / contradicted / corrected)."
)


DESC_MEMORY_SHOW = (
    "Fetch a single memory's full content by id. Typically used "
    "after a memory_search snippet looks relevant. The response "
    "carries the same staleness signals as a search hit:\n"
    "- `verification.status` ('never' / 'stale' / 'fresh') with an "
    "actionable `recommendation` when not fresh.\n"
    "- `staleness_verdict` (fresh / spot_check_recommended / "
    "spot_check_required) — rolled-up signal across calendar, path "
    "and commit drift.\n"
    "- `path_drift` (the full report; missing-on-disk paths "
    "listed).\n"
    "- `commit_drift` (when caller is inside the memory's origin "
    "repo) — `status: 'clean' | 'drift'` + `commits_since_verify`.\n"
    "- Forward `links` and `reverse_links` for navigation.\n\n"
    "When the verdict isn't fresh, spot-check one claim before "
    "relying. memory_verify(id, …) if it holds; memory_update if "
    "drifted (content updates reset last_verified_at, so verify "
    "again after the fix)."
)


DESC_MEMORY_WRITE = (
    "Create a new memory. Call PROACTIVELY when something durable "
    "enters the conversation — don't wait for 'remember that.' "
    "Triggers: user states a preference (→ "
    "category='user-inference'); a project decision the user "
    "concurred with (→ category='fact'); a tool / infrastructure / "
    "config fact; a unit of work finishes with a why git won't "
    "capture. The structural guardrails below catch bad writes; "
    "aggressive writing is safe.\n\n"
    "Parameters:\n"
    "- `content`: the memory body.\n"
    "- `scopes`: non-empty list. Avoid the catch-all 'general'; "
    "prefer narrow tags like `tools`, `infrastructure`, "
    "`projects:<name>`, `learning-style`.\n"
    "- `category` (default 'fact'): one of `fact`, "
    "`user-inference`, `ambient`.\n"
    "  - `fact`: project / infra / reference / tooling. Commits "
    "immediately (unless `require_write_confirmation`).\n"
    "  - `user-inference`: claims ABOUT THE USER. Always returns "
    "{status:'pending', pending_id} regardless of config — ask "
    "the user in plain language, then memory_write_confirm or "
    "memory_write_cancel. Misattribution sticks; user gets the "
    "veto.\n"
    "  - `ambient`: atmospheric context that shapes replies "
    "without being cited. Commits like fact but excluded from "
    "dead-weight curation; long bodies (>500 words) attach a "
    "non-blocking `ambient_body_long` warning.\n"
    "- `confidence` ('low' / 'medium' / 'high'), `source` "
    "('explicit-statement' / 'inferred').\n"
    "- `groundedness_check=True` + `source_transcript`: optional "
    "gate. Sentences with <30% token overlap to the transcript "
    "return {status:'ungrounded', claims:[…]}. Override via "
    "`acknowledge_ungrounded=True` when you have grounding sources "
    "outside the transcript (file reads, tool results). Off by "
    "default; opt in for a paper trail.\n\n"
    "Return statuses:\n"
    "- `committed` — write succeeded; payload carries the new id "
    "and `related` medium-overlap matches.\n"
    "- `transient_warning` — durability marker detected "
    "('currently', 'today I', 'we just', commit-SHA-like tokens, "
    "etc.). Extract the level-up durable form (the decision, the "
    "why) or pass `acknowledge_transient=True` (rare).\n"
    "- `duplicate` — content dedup fired. Prefer memory_update on "
    "the matched id; pass `force=True` only when the new memory "
    "is meaningfully different.\n"
    "- `previously_removed` — overlap with a tombstone; inspect "
    "`removed_reason`. If the rejection still applies, drop the "
    "write; if the fact is now correct, memory_restore the "
    "tombstone instead of a parallel entry.\n"
    "- `scope_mismatch` — body cites a project the declared "
    "scopes don't cover. Re-scope or pass "
    "`acknowledge_scope_mismatch=True`.\n"
    "- `pending` — `category='user-inference'` or "
    "`require_write_confirmation`. `pending_reason` distinguishes.\n"
    "- `ungrounded` — groundedness gate fired."
)


DESC_MEMORY_WRITE_CONFIRM = (
    "Commit a memory_write that returned status='pending'. "
    "Pass the pending_id from that response."
)


# Append the links tail to the canonical memory_update description.
# (Done at module load time so the resulting constant is itself
# top-level and the FastMCP framework's description-binding picks
# up the full text.)


DESC_MEMORY_WRITE_CANCEL = (
    "Drop a pending memory_write without committing. "
    "Pass the pending_id from the original write response."
)


DESC_MEMORY_LINKS_TAIL = (
    " Optional `links` parameter sets the typed inter-memory edge "
    "list. Each entry is a dict with `type` (one of `supersedes`, "
    "`contradicts`, `extends`, `depends_on`), `target_id` (a valid "
    "ULID — the other memory this one relates to), and an optional "
    "`note` (free-form, why the link exists). REPLACE semantics: "
    "pass the full new list, not a delta; pass `links=[]` to clear "
    "all links atomically. Self-links are rejected. Links surface "
    "bidirectionally at retrieval: memory_show on the source "
    "carries `links`; memory_show on the target carries "
    "`reverse_links`. Use `supersedes` when this memory replaces "
    "the target (the retrieval consumer should prefer this one "
    "and demote the target); `contradicts` when both can't be "
    "true (consumer should reconcile via memory_verify); "
    "`extends` when this memory adds nuance to the target; "
    "`depends_on` when this memory only makes sense in the "
    "target's context."
)


DESC_MEMORY_UPDATE = (
    "Refine an existing memory in place. Preferred over "
    "memory_remove + memory_write when correcting a stored fact — "
    "preserves `id`, `created`, and `source`; bumps `updated`.\n\n"
    "Parameters (pass at least one):\n"
    "- `id`: required.\n"
    "- `content`: new body. Replacing the body clears "
    "`last_verified_at` and the verified-* attestations (the "
    "prior verification was for prose that no longer exists; "
    "call memory_verify again after).\n"
    "- `scopes` / `links`: REPLACE semantics — pass the full new "
    "list, or `[]` to clear. Scope-only edits preserve "
    "`last_verified_at`.\n"
    "- `confidence`: low / medium / high.\n"
    "- `category`: accepts `fact` and `ambient`. "
    "`user-inference` is REJECTED here — that category exists "
    "to gate WRITES through the pending-confirm flow; updates "
    "have no equivalent gate." + DESC_MEMORY_LINKS_TAIL
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
    "Aggregate health view for curation passes. Don't call on every "
    "turn — use `memory_scope_overview` for the session-start "
    "branch; this is the deep report.\n\n"
    "Returns buckets (capped row lists; bucket sizes are full "
    "counts):\n"
    "- `dead_weight` — created > `window_days` ago, never applied.\n"
    "- `heavily_used` — applied_count >= `min_applied` (default "
    "from config, typically 3).\n"
    "- `contradicted` — unresolved `record_use(contradicted)` "
    "events. Each row carries a `resolution_timeline` to debug "
    "stuck flags. Resolve via memory_update or memory_verify.\n"
    "- `verification_debt` — partition by never_verified / stale / "
    "fresh against `verification_stale_days`.\n"
    "- `commit_drift_debt` — when the server's in the memory's "
    "origin repo, memories with commits since last_verified_at.\n"
    "- `silent_misses` / `endorsement_debt` — populated when "
    "`memory_audit_turn` has been firing (see that tool).\n"
    "- `scope_distribution` + `scope_health` per-scope rollup; "
    "`rare_scopes` flags Levenshtein-near-others singletons "
    "(likely typos — fix with memory_rename_scope).\n"
    "- `orphan_use_events` — record_use calls against ids that "
    "don't exist (fabrication smoke test).\n"
    "- `marker_stats` — transient-marker fire/override rates.\n\n"
    "CLI equivalent: `bettermemory health [--json]`."
)


DESC_MEMORY_RECORD_USE = (
    "Override the auto-committed `applied` outcome for a "
    "retrieved memory. Default behavior: every memory_search hit "
    "auto-commits as `applied` ~2 turns later (logged with "
    "`auto=true`), so the common case handles itself — only call "
    "this tool to override.\n\n"
    "Outcomes (one per call):\n"
    "- `ignored`: retrieved but turned out off-topic.\n"
    "- `contradicted`: stored fact disagreed with user/reality AND "
    "you haven't fixed it yet. Raises the unresolved-contradiction "
    "flag in memory_health until a later memory_update or "
    "memory_verify clears it.\n"
    "- `corrected`: memory drifted and you fixed it inline "
    "(memory_update and/or memory_verify already called this "
    "turn). Audit-only; does NOT raise the contradiction flag. "
    "Use this instead of `contradicted` when the resolution is "
    "done — event timestamps decide flag state.\n"
    "- `applied` is also accepted explicitly (force-commit early).\n\n"
    "Parameters:\n"
    "- `memory_ids`: list (1+).\n"
    "- `outcome`: see above.\n"
    "- `note` (optional, ≤500 chars): free-form context.\n"
    "- `claim_excerpts` (optional): list parallel to `memory_ids` "
    "(same length, `None` slots OK) carrying the load-bearing "
    "phrase that shaped the response. ≤500 chars per excerpt. "
    "Especially useful on `contradicted` / `corrected` so the "
    "audit log records WHICH claim was wrong, not just that the "
    "memory drifted. Surfaces back in "
    "`recent_negative_outcomes` on later search hits."
)


DESC_MEMORY_VERIFY = (
    "Bump `last_verified_at` to now after spot-checking that a "
    "memory's claims still match reality (file paths exist, "
    "version still matches, configuration still what it says).\n\n"
    "Orthogonal to content edits: this tool does NOT bump "
    "`updated`; memory_update does NOT bump `last_verified_at`. A "
    "typo fix bumps `updated` only; a verify call bumps "
    "`last_verified_at` only. Idempotent — calling twice slides "
    "the timestamp forward.\n\n"
    "Parameters:\n"
    "- `id`: memory id.\n"
    "- `note` (optional, ≤500 chars): what was checked, for the "
    "event log.\n"
    "- `verified_paths` / `verified_commits` / `verified_versions` "
    "(optional lists of strings): structured attestations. The "
    "server uses these to short-circuit later drift signals — "
    "a future retrieval whose path_drift would have flagged a "
    "path still in `verified_paths` downgrades the verdict.\n\n"
    "After memory_update on a memory you later spot-check, verify "
    "again — memory_update clears `last_verified_at` because the "
    "prior verification was for prose that no longer exists.\n\n"
    "Also resolves an unresolved `record_use(contradicted)` flag "
    "in memory_health when the body still matches reality."
)


DESC_MEMORY_SCOPE_OVERVIEW = (
    "Cheap session-start hint: per-scope counts, no bodies / "
    "ids / summaries. Call once at the start of a conversation; "
    "if `total` is 0, skip memory_search for the rest of the "
    "session unless explicitly asked.\n\n"
    "Returns `{current_repo, current_cwd, auto_scope, scopes: "
    "{scope: count}, total, disabled_scopes, curation_pending}`. "
    "`curation_pending` is an integer-count rollup the model "
    "should branch on:\n"
    "  {stale, never_verified, drifted, cold, dead, "
    "silent_misses, endorsement_debt}\n"
    "Any non-zero `dead` or `drifted` is a cue to suggest a "
    "curation pass when the conversation has time. Non-zero "
    "`silent_misses` / `endorsement_debt` means the audit-turn "
    "telemetry has actionable backlog.\n\n"
    "Default-scoped to the caller's current repository; memories "
    "with no origin always pass as global. Set `auto_scope=False` "
    "for the cross-project view. Counts respect session-disabled "
    "scopes."
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


DESC_MEMORY_AUDIT_TURN = (
    "Silent-miss telemetry. Call from a client-side end-of-turn hook "
    "with the user's message (and optionally the assistant's reply) to "
    "detect turns where memory *should* have been retrieved but wasn't. "
    "Runs a cheap search probe over the active store using the model's "
    "configured search mode (matches what the model would have done), "
    "then checks whether a `memory_search` OR `memory_show` event fired "
    "in the same session within `lookback_seconds` (default 60). When "
    "a high-relevance hit exists AND no retrieval happened in the "
    "window, emits a `search_miss` event so memory_health / "
    "memory_scope_overview can surface the rate. Returns a structured "
    "`MissReport` with `verdict` in {'miss', 'ok', 'no_signal'} plus "
    "the top probe hits for offline triage. This tool is the "
    "false-negative half of the retrieval contract — without it, the "
    "cost of opt-in retrieval (model didn't search when it should "
    "have) is structurally invisible. Auto-scopes to the caller's "
    "repo so the probe matches the model's view; honours "
    "session-disabled scopes. Side-effects: emits `turn_audited` "
    "always, plus `search_miss` when the verdict is `miss`. Safe to "
    "call after every turn; cost is one search sweep over the active "
    "store."
)


# ---------------------------------------------------------------------------
# Validation + per-handler bookkeeping helpers.
# ---------------------------------------------------------------------------


def _validate_content_size(content: str, max_bytes: int) -> None:
    """Reject memory bodies whose UTF-8 byte length exceeds `max_bytes`.

    A no-op when `max_bytes <= 0` (cap disabled). Centralised so that
    `memory_write`, `memory_update`, and any future write entry point
    share the same bound. The check is on encoded byte length rather
    than character count because that's the unit that lands on disk
    and in the JSONL event log — a body of CJK or emoji characters
    expands meaningfully under UTF-8 encoding.
    """
    if max_bytes <= 0:
        return
    encoded_size = len(content.encode("utf-8"))
    if encoded_size > max_bytes:
        raise ValueError(
            f"content exceeds max_content_bytes "
            f"({encoded_size} bytes > {max_bytes} bytes). "
            f"Split into multiple memories or raise the "
            f"[behavior] max_content_bytes config setting."
        )


def _validate_write_payload(
    *,
    content: str,
    scopes: list[str],
    confidence: str,
    source: str,
    allowed_scopes: list[str],
    category: str = "fact",
    max_content_bytes: int = 0,
) -> dict[str, Any]:
    """Validate and normalise the kwargs for `Store.write`.

    Returns a dict suitable for `Store.write(**payload)`. Raises ValueError
    on any input problem so the model gets a clear error.
    """
    if not content or not content.strip():
        raise ValueError("content must be a non-empty string")
    if not scopes:
        raise ValueError("scopes must contain at least one entry")
    _validate_content_size(content, max_content_bytes)

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

    Hook-attributed ids (the Stop hook substring-matched a retrieved
    memory's body against the assistant turn and emitted a
    `record_use` event with `attribution="hook"`) are purged from
    the pending map before consume_old_tokens runs. The hook lives
    in a different process and can't touch this in-memory state, so
    its attribution is communicated through the event log. Without
    the purge, the auto-commit would fire a *second* `applied`
    event for the same retrieval — duplicating the audit signal and
    inflating the eval CLI's denominators.

    Auto-committed ids land in the event log under
    `kind="use", outcome="applied", auto=True, attribution="auto"`
    so the eval CLI can distinguish the three applied tiers (model
    explicit, hook attributed, auto fallback). Older events without
    `attribution` fall back to `model` when auto=false and `auto`
    when auto=true at read time.
    """
    state.advance_turn()
    if state.pending_use_tokens and recorder.enabled:
        hook_ids = _hook_attributed_pending_ids(state, recorder)
        for mid in hook_ids:
            state.purge_use_token(mid)
    auto_ids = state.consume_old_tokens(override_ids=override_ids)
    if auto_ids:
        recorder.record(
            "use",
            ids=list(auto_ids),
            outcome="applied",
            auto=True,
            attribution="auto",
        )


def _hook_attributed_pending_ids(
    state: SessionState,
    recorder: Recorder,
) -> set[str]:
    """Return the subset of pending-token memory_ids the Stop hook has
    already attributed for this session.

    Reads the active event log forward; bounded by the rotation cap
    (default 10 MB) and only invoked when there ARE pending tokens
    (the common between-batch case skips this entirely). Matches a
    hook event when its `attribution` is `"hook"`, its session
    matches the caller's, and at least one of its `ids` is currently
    pending. Older log entries are tolerated — they fall away once
    their wall-clock TTL evicts their token from `pending_use_tokens`.
    """
    if not state.pending_use_tokens:
        return set()
    pending = set(state.pending_use_tokens.keys())
    out: set[str] = set()
    for event in iter_events(recorder.root):
        if event.get("kind") != "use":
            continue
        # `session` / `session_id` both appear depending on producer
        # vintage: canonical handler writes both; pre-2.6.4 hook wrote
        # only `session`. Read either with the canonical-first
        # discipline 70e41a4 established for llm.py.
        if (event.get("session") or event.get("session_id")) != recorder.session_id:
            continue
        if event.get("attribution") != "hook":
            continue
        # Legacy fallback for `memory_ids` — same class as the 70e41a4
        # fix. Pre-2.6.3 `use` events landed with `memory_ids=[…]`
        # before the Recorder canonicalized to `ids=[…]`.
        ids = event.get("ids") or event.get("memory_ids") or []
        if not isinstance(ids, list):
            continue
        for mid in ids:
            if isinstance(mid, str) and mid in pending:
                out.add(mid)
    return out


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

    # The store size above which the FTS5 candidate pre-filter is
    # used instead of a full load_all on every search. Calibrated so
    # that small stores (the common case) keep the existing behaviour
    # byte-stable — the candidate path adds a SQLite round-trip per
    # search and a per-id load, which is net cheaper only once the
    # alternative (load every file) dominates the budget. Tunable
    # via the BETTERMEMORY_INDEX_THRESHOLD env var for testing.
    _INDEX_THRESHOLD_DEFAULT = 500

    def _index_threshold(self) -> int:
        """Resolve the live threshold above which the FTS candidate
        pre-filter kicks in. Reads from BETTERMEMORY_INDEX_THRESHOLD
        on every search so tests can flip it without rebuilding the
        handler. Falls back to the class default."""
        import os

        raw = os.environ.get("BETTERMEMORY_INDEX_THRESHOLD")
        if raw is None:
            return self._INDEX_THRESHOLD_DEFAULT
        try:
            value = int(raw)
            return value if value > 0 else self._INDEX_THRESHOLD_DEFAULT
        except ValueError:
            return self._INDEX_THRESHOLD_DEFAULT

    def _load_search_candidates(self, query: str) -> list[Any]:
        """Either load all active memories or pre-filter via the FTS5
        index, depending on store size and index health.

        The current heuristic: walk the index status once. If the
        on-disk index exists, has `indexed_count >= threshold`, and the
        query is non-empty, we query the index for up to 50 candidate
        ids and load just those by walking the file store for matches.
        Otherwise the full `load_all` runs (current behaviour, byte-
        stable result quality).

        Falls back to load_all when the index returns no candidates —
        a stale index missing recent writes shouldn't silently hide
        results. The recovery path is `bettermemory reindex`.
        """
        from . import index as _index

        if not query.strip():
            return self.store.load_all()
        status = _index.status(self.store.root)
        if not status.get("exists") or status.get("corrupt"):
            return self.store.load_all()
        indexed_count = int(status.get("indexed_count", 0) or 0)
        if indexed_count < self._index_threshold():
            return self.store.load_all()

        # Pre-filter via the index. 50 candidates is generous for a
        # default max_results of 5 — the downstream ranker reorders
        # within the candidate pool, so we want enough variety for
        # recency / scope-boost / coverage to find the best 5.
        candidate_pairs = _index.query(self.store.root, query, max_results=50)
        if not candidate_pairs:
            # Stale index or query that genuinely matches nothing —
            # fall back to load_all so we don't silently miss recent
            # writes that aren't in the index yet.
            return self.store.load_all()
        candidate_ids = {cid for cid, _ in candidate_pairs}

        # Load just the candidates via the index's id → filename
        # lookup — true O(k) on file IO. Candidates that aren't in
        # the lookup (a row written by a pre-v2 schema, an entry
        # that's been removed since the FTS pre-filter ran, etc.)
        # are skipped per-candidate. If every candidate misses we
        # fall back to `load_all` below — search must never silently
        # return empty when the FTS pre-filter actually matched.
        loaded: list[Any] = []
        ids = list(candidate_ids)
        filenames = _index.filenames_for_ids(self.store.root, ids)
        for cid in ids:
            filename = filenames.get(cid)
            if not filename:
                continue
            file_path = self.store.root / filename
            try:
                memory = self.store._load_path(file_path)
            except (ValueError, KeyError, OSError):
                # Stale filename (memory was moved / tombstoned
                # between the index lookup and the read) or a
                # malformed frontmatter row. Skip — the fallback
                # below covers the "every candidate failed" case.
                continue
            # Index-drift defense: `sync pull` rewrites files in
            # place, so the filename column can briefly point at a
            # path whose body now belongs to a different memory id.
            # Without this guard, the handler would score the
            # candidate's FTS hit against a body it isn't paired
            # with anymore. The post-pull `bettermemory reindex`
            # is the right long-term fix, but we don't trust the
            # index unconditionally between pull and reindex.
            if memory.id != cid:
                continue
            loaded.append(memory)
        if not loaded:
            # FTS matched, but every candidate's filename lookup
            # missed (pre-v2 schema rows, every match tombstoned
            # between pre-filter and read, etc.). Fall back to
            # `load_all` so the documented contract — "search keeps
            # working through schema upgrades and stale-index
            # windows" — actually holds. Hot path is the loaded
            # branch above; this is the safety net.
            return self.store.load_all()
        return loaded

    # ---- memory_search ---------------------------------------------------

    async def memory_search(
        self,
        query: str,
        scopes: list[str] | None = None,
        max_results: int | None = None,
        expand_top: bool = False,
        auto_scope: bool = True,
        mode: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        if max_results is None:
            max_results = self.config.behavior.default_max_results
        max_results = max(1, min(int(max_results), 50))

        # Resolve search mode: per-call override > config default > "keyword".
        # Validation happens via the Literal narrowing in search() — any
        # other value will raise ValueError at the dispatch boundary,
        # which the handler propagates to the caller as a tool error.
        resolved_mode = mode or self.config.behavior.search_mode or "keyword"
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
            semantic_model = self._semantic_model_factory(self.config)
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
        current_origin = capture_origin()
        repo_filter: str | None = current_origin.repo if auto_scope else None
        # Worktree filter rides along on the same auto_scope toggle as the
        # repo filter — both are pieces of the same "drop cross-context
        # memories" defaults pass. Disabling auto_scope drops both, so a
        # cross-project search keeps working without needing a second flag.
        worktree_filter: str | None = (
            current_origin.worktree_root if auto_scope else None
        )

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
        memories = self._load_search_candidates(query)
        # `cast` keeps mypy aware of the Literal narrowing — we already
        # validated `resolved_mode` against the four allowed values above,
        # but the local variable's type is `str` until we tell the checker
        # otherwise.
        from typing import cast

        from .search import SearchMode

        hits = run_search(
            memories,
            query,
            scopes=scopes,
            excluded_scopes=set(state.disabled_scopes),
            repo_filter=repo_filter,
            worktree_filter=worktree_filter,
            max_results=max_results,
            half_life_days=self.config.behavior.recency_boost_half_life_days,
            mode=cast(SearchMode, resolved_mode),
            semantic_model=semantic_model,
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
            from .events import iter_events

            recent_events = list(iter_events(self.store.root))
            self.responses.attach_recent_negative_outcomes(
                out, hits, recent_events, now=now
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
            **self._links_payload(memory),
        }

    def _links_payload(self, memory: Any) -> dict[str, Any]:
        """Build the `links` + `reverse_links` payload for memory_show.

        Forward `links` come from the memory's own frontmatter. Reverse
        `reverse_links` are computed by querying the FTS5 index's
        `memory_links` table — every memory that links AT this id is
        a row keyed on `target_id`, so the lookup is O(k) on the
        number of reverse links rather than O(N) on the store size.
        Surfaced so a retrieval consumer sees the relationship both
        ways (e.g. "this memory is superseded by X" alongside "X
        supersedes this").

        Both lists are omitted when empty (absence-as-signal contract,
        matches `path_drift` / `commit_drift`). Reverse links carry the
        source `memory_id` so the consumer can navigate to the linking
        memory; forward links carry the `target_id`.

        Fallback: if the index file doesn't exist (fresh install, just
        deleted), `links_for` returns empty lists and we walk the
        active set once. That matches the same fallback shape
        `_load_search_candidates` uses — search keeps working through
        a torn-down index, just slower.
        """
        from . import index as _index

        out: dict[str, Any] = {}
        if memory.links:
            out["links"] = [
                {
                    "type": link.type.value,
                    "target_id": link.target_id,
                    **({"note": link.note} if link.note is not None else {}),
                }
                for link in memory.links
            ]
        outbound, inbound = _index.links_for(self.store.root, memory.id)
        reverse: list[dict[str, Any]] = []
        if inbound:
            for ltype, source_id, note in inbound:
                if source_id == memory.id:
                    # Defensive: self-links shouldn't appear as reverse
                    # since they're already in `links`. Skip to keep
                    # the surface stable across index drift.
                    continue
                entry: dict[str, Any] = {"type": ltype, "source_id": source_id}
                if note is not None:
                    entry["note"] = note
                reverse.append(entry)
        elif not _index.index_path(self.store.root).exists():
            # No index — fall back to the old shape so a freshly-
            # initialised store still gets reverse links. After the
            # next write the index will repopulate.
            for other in self.store.load_all():
                if other.id == memory.id:
                    continue
                for link in other.links:
                    if link.target_id == memory.id:
                        entry = {
                            "type": link.type.value,
                            "source_id": other.id,
                        }
                        if link.note is not None:
                            entry["note"] = link.note
                        reverse.append(entry)
        if reverse:
            out["reverse_links"] = reverse
        return out

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
        acknowledge_ungrounded: bool = False,
        category: str = "fact",
        groundedness_check: bool = False,
        source_transcript: str | None = None,
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
            max_content_bytes=self.config.behavior.max_content_bytes,
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

        # Groundedness check runs after scope_mismatch and before dedup
        # (T1.3). Opt-in via `groundedness_check=True` plus a non-empty
        # `source_transcript`. Sentence-level overlap of the proposed
        # body against the transcript's token set — sentences that
        # don't anchor to the conversation come back as "ungrounded".
        # Returns `status: "ungrounded"` with the offending sentences
        # so the caller can rephrase or pass `acknowledge_ungrounded`.
        # Mirrors the transient_warning shape exactly. Closes the
        # hallucinate-at-write-time failure mode common to systems that
        # auto-extract memories from conversation. Implements the
        # HaluMem-style operation-level write-time grounding check
        # inline.
        if (
            groundedness_check
            and source_transcript is not None
            and not acknowledge_ungrounded
        ):
            from .groundedness import check_groundedness

            ungrounded = check_groundedness(payload["content"], source_transcript)
            if ungrounded:
                self.recorder.record(
                    "write",
                    status="ungrounded",
                    scopes=payload["scopes"],
                    forced=False,
                    ungrounded_count=len(ungrounded),
                )
                return {
                    "status": "ungrounded",
                    "claims": [c.to_dict() for c in ungrounded],
                    "hint": (
                        "The body contains sentences that don't share enough "
                        "vocabulary with the source transcript to count as "
                        "grounded — the model may have hallucinated them, "
                        "or paraphrased so heavily that the audit trail is "
                        "lost. Either rephrase to keep the load-bearing "
                        "tokens close to the transcript, or pass "
                        "`acknowledge_ungrounded=True` if you have other "
                        "grounding sources (a file read, a tool result) "
                        "that aren't represented in this transcript."
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
        links: list[dict[str, Any]] | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)
        if (
            content is None
            and scopes is None
            and confidence is None
            and category is None
            and links is None
        ):
            raise ValueError(
                "memory_update needs at least one of content, scopes, "
                "confidence, category, or links"
            )
        if content is not None and not content.strip():
            raise ValueError("content must be non-empty if provided")
        if content is not None:
            _validate_content_size(content, self.config.behavior.max_content_bytes)

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

        # `links` is REPLACE semantics — the caller passes the full new
        # list. Same shape as the `scopes` parameter: simpler than
        # diffing add/remove, and lets the caller atomically clear all
        # links with `links=[]`. None means "leave existing links
        # unchanged".
        new_links = existing.links
        if links is not None:
            from .models import MemoryLink as _MemoryLink

            parsed_links: list[_MemoryLink] = []
            for i, entry in enumerate(links):
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"links[{i}] must be a dict with 'type' and 'target_id'"
                    )
                try:
                    parsed_links.append(_MemoryLink.model_validate(entry))
                except (ValueError, KeyError) as exc:
                    raise ValueError(f"links[{i}] invalid: {exc}") from exc
                if parsed_links[-1].target_id == id:
                    raise ValueError(
                        f"links[{i}].target_id cannot equal the memory's own id "
                        f"(self-links are incoherent)"
                    )
            new_links = parsed_links

        # When `content` changes, the prior verification was for prose
        # that no longer exists — reset `last_verified_at` to None so the
        # caller has to re-confirm against the new body. The structured
        # attestation lists (`verified_paths`, `verified_commits`,
        # `verified_versions`) were also attached to the prior prose and
        # would lie about the new body — clear them in lockstep so the
        # staleness rollup doesn't read e.g. `verified_paths=["/etc/foo"]`
        # against text that no longer mentions `/etc/foo`. Scope/confidence/
        # category/links edits don't touch the body's claims, so the
        # verification stays intact for those. This matches the intuition
        # that verification is a property of body content, not of metadata.
        update_fields: dict[str, Any] = {
            "body": new_body,
            "scopes": new_scopes,
            "confidence": new_confidence,
            "category": new_category,
            "links": new_links,
        }
        if content is not None:
            update_fields["last_verified_at"] = None
            update_fields["verified_paths"] = []
            update_fields["verified_commits"] = []
            update_fields["verified_versions"] = []

        merged = existing.model_copy(update=update_fields)
        updated = self.store.update(merged)
        fields_changed = [
            name
            for name, value in (
                ("content", content),
                ("scopes", scopes),
                ("confidence", confidence),
                ("category", category),
                ("links", links),
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
        claim_excerpts: list[str | None] | None = None,
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
        if note is not None and len(note) > _NOTE_MAX_LEN:
            raise ValueError(
                f"note is {len(note)} chars — cap is {_NOTE_MAX_LEN}. "
                "The note is a short rationale for the outcome, not a "
                "place to paste prose; trim it before recording."
            )

        # `claim_excerpts` is the provenance signal (T1.1 of the 1.6 plan).
        # When provided, it's a list parallel to `memory_ids` with one
        # entry per id — the specific claim the model applied (or ignored
        # / contradicted / corrected) from that memory. `None` in a slot
        # means "no specific claim noted for this id, just the outcome".
        # Length must match exactly so the audit log can pair claims to
        # ids without ambiguity; the alternative (sparse dict keyed by id)
        # is harder for the model to assemble and clutters small calls.
        # Empty-string claims are rejected — pass `None` for "no claim".
        # Excerpts are capped at 500 chars to keep the event log small
        # and discourage dumping whole bodies (the body's already on disk;
        # the excerpt is supposed to be a quote, not a copy).
        recorded_excerpts: list[str | None] | None = None
        if claim_excerpts is not None:
            if not isinstance(claim_excerpts, list):
                raise ValueError("claim_excerpts must be a list of strings or None")
            if len(claim_excerpts) != len(memory_ids):
                raise ValueError(
                    f"claim_excerpts length {len(claim_excerpts)} does not "
                    f"match memory_ids length {len(memory_ids)}"
                )
            recorded_excerpts = []
            for i, excerpt in enumerate(claim_excerpts):
                if excerpt is None:
                    recorded_excerpts.append(None)
                    continue
                if not isinstance(excerpt, str):
                    raise ValueError(
                        f"claim_excerpts[{i}] must be a string or None, "
                        f"got {type(excerpt).__name__}"
                    )
                excerpt = excerpt.strip()
                if not excerpt:
                    raise ValueError(
                        f"claim_excerpts[{i}] is empty — pass None for "
                        "'no specific claim' instead of an empty string"
                    )
                if len(excerpt) > 500:
                    raise ValueError(
                        f"claim_excerpts[{i}] is {len(excerpt)} chars — "
                        "cap is 500. Quote the load-bearing phrase, not "
                        "the whole body."
                    )
                recorded_excerpts.append(excerpt)

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

        # Build the event payload conditionally so the on-disk shape is
        # byte-stable for calls that don't use the new field — existing
        # log parsers / tests that key off the kind="use" event keep
        # working without seeing a new claim_excerpts key with a null
        # value on every old event. `attribution="model"` distinguishes
        # the explicit-by-model path from the hook-attributed
        # (`attribution="hook"`) and auto-fallback (`attribution="auto"`)
        # paths in the eval CLI's rollups; older events without the
        # field fall back to `model` when auto=false, `auto` when
        # auto=true.
        event_fields: dict[str, Any] = {
            "ids": list(memory_ids),
            "outcome": outcome,
            "note": note,
            "attribution": "model",
        }
        if recorded_excerpts is not None:
            event_fields["claim_excerpts"] = recorded_excerpts
        self.recorder.record("use", **event_fields)

        result: dict[str, Any] = {
            "recorded": list(memory_ids),
            "outcome": outcome,
        }
        if recorded_excerpts is not None:
            result["claim_excerpts"] = recorded_excerpts
        return result

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
        if note is not None and len(note) > _NOTE_MAX_LEN:
            raise ValueError(
                f"note is {len(note)} chars — cap is {_NOTE_MAX_LEN}. "
                "The note is a short rationale for the verification, "
                "not a place to paste prose; trim it before recording."
            )
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
        worktree_filter: str | None = None
        current_origin: Origin | None = None
        if auto_scope:
            current_origin = capture_origin()
            repo_filter = current_origin.repo
            worktree_filter = current_origin.worktree_root

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
                # Worktree filter rides through the same helper so the
                # two surface filters stay in sync; without it, two
                # worktrees of one repo would disagree about scope counts
                # vs. search hits, exactly the symmetry this helper exists
                # to enforce.
                if not should_include_for_caller(
                    memory.origin,
                    repo_filter,
                    caller_worktree_root=worktree_filter,
                ):
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

    # ---- memory_audit_turn -----------------------------------------------

    async def memory_audit_turn(
        self,
        user_message: str,
        assistant_response: str | None = None,
        lookback_seconds: int | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Detect silent retrieval misses for a just-completed turn.

        Fires from a client-side hook (Claude Code Stop hook, etc.) with
        the user's message. Runs a search probe (using the model's
        configured search mode) over the active store; if a
        high-relevance hit exists AND no retrieval event (`search` or
        `show`) fired in the same session within the lookback window,
        emits `search_miss` so curation views can surface the rate.

        `assistant_response` is accepted but currently used only to keep
        the API shape stable — a future probe will run against it too
        (the response text is where unsearched citations land).
        Validating now keeps the wire shape settled.

        Always emits `turn_audited` so audit cadence is visible in the
        log even when there's nothing to flag; emits `search_miss` only
        when `verdict == "miss"`.

        Known v1 limitations:

        - **Any retrieval shields**: the probe shields on ANY recent
          `search` or `show` in the window, even if that retrieval was
          for an unrelated query. A turn that searched for X but
          missed an unrelated B-relevant retrieval won't be flagged.
          Tightening this would require per-hit shielding — out of
          scope for v1.
        - **Cross-process audits**: the audit must share its
          SessionState with the model (same MCP `client_id`). A hook
          that opens its own MCP connection would get a fresh session
          and always see zero recent retrievals, false-flagging every
          turn. Production hooks must run in-process with the model.
        """
        if not isinstance(user_message, str):
            raise ValueError("user_message must be a string")
        if assistant_response is not None and not isinstance(assistant_response, str):
            raise ValueError("assistant_response must be a string if provided")

        # Clamp lookback. Lower bound 1s (don't accept 0/negative — that
        # would always flag); upper bound 600s (10 minutes) so a misused
        # hook can't silence the audit by passing a huge window.
        if lookback_seconds is None:
            window = DEFAULT_LOOKBACK_SECONDS
        else:
            window = max(1, min(int(lookback_seconds), 600))

        state = self.sessions.for_request(ctx)
        _advance_turn(state, self.recorder)

        # Active-log iter is sufficient for a 60s lookback: rotation
        # thresholds are far larger than that window in normal use, so a
        # search event from this session within the window is still in
        # `.events.jsonl`. If a future deployment cranks the rotation to
        # something pathological, the right fix is to widen the iter
        # here, not to silently undercount misses.
        memories = self.store.load_all()
        recent = list(iter_events(self.store.root))

        current_origin = capture_origin()
        # Probe uses the same search mode the model would have used —
        # otherwise we'd be measuring "would a different scorer have
        # hit" rather than "did the model miss what its ranker would
        # have shown." Falls through to `"keyword"` (the package
        # default) when the config doesn't carry an override.
        probe_mode = self.config.behavior.search_mode or "keyword"
        report = probe_for_miss(
            memories,
            user_message,
            recent_events=recent,
            session_id=state.session_id,
            now=utcnow(),
            lookback_seconds=window,
            caller_origin=current_origin,
            excluded_scopes=set(state.disabled_scopes),
            mode=probe_mode,
        )

        # `turn_audited` records that the audit ran at all — distinct
        # from `search_miss`, which only fires on a flagged turn. The
        # split lets `memory_health` derive a denominator (audits run)
        # for the silent-miss *rate* without conflating "audit didn't
        # run this turn" with "audit ran and found nothing."
        self.recorder.record(
            "turn_audited",
            session_id=state.session_id,
            verdict=report.verdict,
            lookback_seconds=window,
            recent_retrieval_count=report.recent_retrieval_count,
            probe_mode=probe_mode,
            threshold_rule=report.threshold_rule,
            assistant_present=assistant_response is not None,
        )
        if report.is_miss:
            self.recorder.record(
                "search_miss",
                session_id=state.session_id,
                threshold_rule=report.threshold_rule,
                lookback_seconds=window,
                top_hits=[h.to_dict() for h in report.top_hits],
                probe_query=report.probe_query,
            )
        return report.to_dict()


# A SemanticModelFactory is `(Config) -> Any | None` — the model object
# (when `semantic_dedup` is enabled and the extras are installed) or
# None for the Jaccard fallback. Kept as a callable rather than a hard
# import so `_handlers.py` doesn't pull in `semantic` (and through it
# `sentence-transformers`) at import time when semantic dedup is off.
from typing import Callable as _Callable  # noqa: E402

SemanticModelFactory: TypeAlias = _Callable[[Config], Any]
