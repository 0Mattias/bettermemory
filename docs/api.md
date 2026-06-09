# API surface (3.x)

The contractual list of MCP tools bettermemory exposes. Signatures, defaults, and return shapes are stable within the 3.x line per the rules in [`CONTRIBUTING.md`](../CONTRIBUTING.md). There are 25 tools, but only **18 register by default**: the seven curation / power-user tools (`memory_health`, `memory_curate`, `memory_acknowledge_miss`, `memory_rename_scope`, `memory_restore`, `memory_list_tombstones`, `memory_proposals`) register only when `full_tool_surface = true` under `[behavior]` (`memory_proposals` also surfaces when `[proposals]` is enabled); six have a direct `bettermemory` CLI counterpart (`health`, `tombstones list` / `tombstones restore`, `rename-scope`, `proposals`, and `consolidate` which `memory_curate` wraps), while `memory_acknowledge_miss`'s per-event ack stays MCP-only (the CLI offers the bulk `consolidate --acknowledge-misses-before` cutoff instead). The 25 group naturally:

- **Retrieval** — `memory_search` (now with `since_prior_session` filter), `memory_show`, `memory_list`, `memory_scope_overview`
- **Writing** — `memory_write` (plus `memory_write_confirm` / `memory_write_cancel` for the staged-write flow), `memory_update`
- **Lifecycle** — `memory_remove`, `memory_restore`, `memory_list_tombstones`
- **Verification** — `memory_verify`
- **Curation** — `memory_record_use`, `memory_health`, `memory_curate`, `memory_audit_turn`, `memory_acknowledge_miss`, `memory_rename_scope`, `memory_proposals`
- **Session-local** — `memory_scope_disable`, `memory_scope_enable`
- **Episodes** (sibling tier for journal-shaped run-state) — `episode_write`, `episode_handoff`, `episode_search`, `episode_promote`

## Retrieval

### `memory_search(query, scopes?, max_results?, expand_top?, auto_scope?, since_prior_session?, mode?)`

Rank stored memories against a free-text query.

- `query: str`. Required.
- `scopes: list[str] | None = None`. When set, only memories carrying at least one of these scopes are eligible.
- `max_results: int | None = None`. Falls through to `behavior.default_max_results` (config default `5`). Capped at 50.
- `expand_top: bool = False`. When the top hit's relevance is `"high"`, inline its full body and a freshly-computed `path_drift` + `commit_drift` report. No-op otherwise. (Per-hit `path_drift` already surfaces on any hit with drifted or attested paths, so `expand_top` is now mainly about the body inline, not the drift detail.)
- `auto_scope: bool = True`. Filter by the caller's current repo + worktree. Memories with no recorded `origin` are treated as global.
- `since_prior_session: bool = False`. When True, narrow candidates to memories whose `updated` is strictly after the latest event timestamp from any other `session_id` in the log — the current session's intra-session diff. The boundary IS the prior session's last-event ts, so a memory whose `updated` equals it belongs to that prior session, not the current-session delta. Mirrors `curation_pending_new_since_last_session`'s exclusion of boundary memory so the two surfaces in the "what's new since last session" workflow never double-count. Returns an empty list when no prior session boundary exists (first run, wiped log). Bypasses the FTS5 prefilter so newly-written rows outside the top-50 prefilter slice can't silently drop; the post-boundary set is bounded by session activity, so the linear scan is cheap regardless of corpus size. Pairs with `episode_handoff` (which surfaces what the prior iteration did). To distinguish "nothing new" (empty result) from "no baseline" (no prior session at all), also call `memory_scope_overview` and check `curation_pending_new_since_last_session is None`.
- `mode: str | None = None`. Ranker — `"hybrid"` (default since 2.6.8: RRF fusion of keyword + BM25 + semantic when the `[embeddings]` extra is installed; degrades gracefully to keyword+BM25 fusion without it), `"keyword"` (legacy TF + coverage + recency; no IDF, weaker on rare-term queries), `"bm25"` (Okapi BM25 with the same scope-bonus + recency), or `"semantic"` (sentence-transformers cosine; requires the `[embeddings]` extra). Per-call override beats `[behavior] search_mode`. Use `"keyword"` for literal identifiers / file paths if you need byte-stable 1.6.0 ranking; otherwise hybrid is a strict improvement. The fused hybrid score lives in a smaller scale (~0.01–0.05) than single-ranker scores — compare across modes via `relevance`, not raw `score`. When `[behavior] endorsement_boost` is on (off by default), a bounded usage-aware factor (≤ +10%, same ceiling as the recency boost) additionally nudges memories the model has *explicitly* applied up the ranking — a near-tie breaker that never overrides relevance.

Returns a list of hits. Each hit carries `id`, `scopes`, `relevance` (`"high"` / `"medium"` / `"low"`), `match_terms`, `snippet`, `created`, `updated`, `last_verified_at`, `verification`, `path_drift_checked` / `path_drift_missing` counts, `staleness_verdict`, a `use_token`, and a `commit_drift_count` when applicable (omitted when the caller isn't in the memory's repo, or the memory was never verified). When the body cites paths that no longer exist (or paths the user previously attested via `memory_verify`), the hit also carries `path_drift` with `{checked, missing, verified}` lists — the missing list is directly actionable, no `memory_show` round-trip needed.

Hits also carry `recent_negative_outcomes` when the memory was `ignored` or `contradicted` within the last 30 days AND not since `applied`. Each entry has shape `{outcome, most_recent_ts, count_in_window, session_id, note, claim_excerpt}` — at most one per outcome type. An `applied` event after a negative event clears the bucket. The field is omitted (not null) when no qualifying negatives exist.

Hits also carry `depends_on_resolved` when the hit's memory carries `depends_on`-typed links. Shape is a list of `{id, scopes, summary, link_note}` entries, where `summary` is the target's first-line summary and `link_note` is the link's optional free-form note. Bounded per call: at most 3 entries per hit, at most 10 entries across the full result set. Targets a hit depends on are auto-pulled even when the query wouldn't surface them on its own — the search layer issues targeted `store.load_one` calls for `depends_on` target ids missing from the FTS prefilter slice, then re-applies the same `auto_scope` + session-disabled-scope filter so a dependency edge can't leak cross-project or hidden-scope content into the response. Tombstoned or removed targets are skipped silently. The field is omitted (not null) when the hit has no `depends_on` links or all targets resolve out.

Hits also carry `superseded_by` and/or `contradicts` when the hit's memory participates in a `supersedes` / `contradicts` link edge — trust signals that activate those edge types at retrieval (purely additive; the annotation never reorders or drops a hit). `superseded_by` lists the ACTIVE memories that supersede this hit (inbound `supersedes` edges) — per the link contract the consumer should prefer them. `contradicts` lists memories in unresolved contradiction with this hit, in either direction (the relation is symmetric — both endpoints surface it; reconcile via `memory_verify` / `memory_update`). Each entry is the same `{id, scopes, summary, link_note}` shape as `depends_on_resolved`, with the same per-hit/per-call caps (3/10), targeted-load resolution, scope/origin re-filter, and tombstoned-target skip. Inbound edges are read from the links index; the annotation is a best-effort no-op when no index is present. Both keys are omitted (not null) when the hit has no such edges.

### `memory_show(id)`

Full body plus `verification` block, `path_drift` report (`null` when no drift), `staleness_verdict`, `use_token`, `commit_drift` block (`null` when not applicable; non-null shape is `{status, commits_since_verify, recommendation}` — `recommendation` is the actionable string to surface when `status == "drift"`, `null` on `"clean"`), and typed inter-memory edges as `links` (forward) and `reverse_links` (entries from the target side carry `source_id` instead of `target_id`).

Full return shape: `{id, scopes, confidence, source, category, created, updated, last_verified_at, verification, staleness_verdict, body, origin, path_drift, commit_drift, use_token, verified_paths, verified_commits, verified_versions}` plus `links` and `reverse_links` (each omitted entirely when the underlying list is empty — absence-as-signal, matching `path_drift` / `commit_drift`).

### `memory_list(scopes?, with_bodies?)`

- `scopes: list[str] | None = None`. Filter, same shape as `memory_search`.
- `with_bodies: bool = False`. Opt in to inlining full bodies; the triage default returns body-stripped summaries.

Each row carries the same `staleness_verdict` rollup as the search and show surfaces.

### `memory_scope_overview(auto_scope?)`

Cheap session-start hint. Counts per scope without bodies or IDs.

- `auto_scope: bool = True`. Same semantics as `memory_search.auto_scope`.

Returns `{current_repo, current_cwd, auto_scope, scopes: {scope: count}, total, disabled_scopes, curation_pending, curation_pending_new_since_last_session, recently_removed_in_worktree, proposals_pending}`. `proposals_pending` is the count of pending write-reflex proposals (0 unless the opt-in `[proposals] auto_propose` is on); see `memory_proposals`. The `curation_pending` rollup is integer counts (`stale`, `never_verified`, `drifted`, `cold`, `dead`, `silent_misses`, `unique_silent_miss_memories`, `cold_endorsement_memories`) derived from the same logic as `memory_health` but without row materialisation. `silent_misses` is the event count; `unique_silent_miss_memories` is the cardinality of the set of top-hit memory_ids on those events — the gap between the two distinguishes "9 events against 1 mis-tagged memory" from "9 events across 9 memories." Misses whose top-hit memory has been tombstoned are dropped from both (no longer actionable). `cold_endorsement_memories` counts distinct memories (NOT turns) with `retrieval_count >= N` AND zero explicit applies — one memory hit 50 times by the ranker contributes 1, not 50.

- `curation_pending_new_since_last_session: dict[str, int] | None`. Same shape as `curation_pending`, filtered to events emitted and memories *created* since the prior-session boundary. An older record aging into `stale` between sessions stays visible only in the absolute `curation_pending`. Branch on this dict when deciding whether to *prompt* the user about curation — non-zero values here mean new rot has accumulated since you were last around. `null` on the very first session (no prior boundary to delta against); fall back to `curation_pending` in that case. This is also the signal that disambiguates an empty `memory_search(since_prior_session=True)` between "nothing new" (key present, all zeros or non-null) and "no baseline" (`null`).
- `recently_removed_in_worktree: int`. Count of tombstones whose `removed` timestamp lands in the last 7 days. Under `auto_scope=True`, restricted to tombstones whose `origin.worktree_root` matches the caller's; tombstones with no recorded origin are excluded under that branch. Under `auto_scope=False`, every tombstone in the window counts. Non-zero is a cue that the model previously trimmed material in this area — useful before re-suggesting something that may have already been removed.

## Writing

### `memory_write(content, scopes, confidence?, source?, force?, acknowledge_transient?, acknowledge_scope_mismatch?, acknowledge_ungrounded?, category?, groundedness_check?, source_transcript?)`

Signature reflects the handler in `src/bettermemory/_handlers.py`. In MCP every argument is keyword-only at the wire, so positional order is only consequential for Python callers reading this as a spec.

- `content: str`. Required.
- `scopes: list[str]`. Required, non-empty.
- `confidence: str = "medium"`. One of `"low"`, `"medium"`, `"high"`.
- `source: str = "explicit-statement"`. One of `"explicit-statement"`, `"inferred"`, `"user-correction"`. `"user-correction"` is the post-hoc tag for memories created when the user contradicts an earlier inference — the body carries the corrected fact, the source records that the correction came from the user rather than from a fresh statement or model inference.
- `force: bool = False`. Bypass content dedup AND tombstone dedup.
- `acknowledge_transient: bool = False`. Bypass the durability marker check. Logged as an override.
- `acknowledge_scope_mismatch: bool = False`. Bypass the scope-mismatch warning when a cross-project reference is intentional.
- `acknowledge_ungrounded: bool = False`. Override the groundedness gate when grounding sources (file reads, tool results) aren't represented in the transcript.
- `category: str = "fact"`. One of `"fact"`, `"user-inference"`, `"ambient"`. `"fact"` commits immediately. `"user-inference"` structurally goes pending and returns `{status: "pending", pending_id, pending_reason: "user-inference"}` regardless of config — claims about the user always get the conversational veto. `"ambient"` commits like `"fact"` but is excluded from the dead-weight curation rule (long bodies over 500 words attach a non-blocking `ambient_body_long` warning).
- `groundedness_check: bool = False`. Opt-in. When True and `source_transcript` is provided, the server walks the proposed body sentence-by-sentence and flags any whose content tokens overlap the transcript by less than 30%.
- `source_transcript: str | None = None`. The conversation turns that motivated this write. Required for the gate to fire.

Result statuses:

- `"committed"` — write succeeded; payload includes the new id and `related` medium-overlap matches.
- `"transient_warning"` — durability gate fired; `markers` listed.
- `"duplicate"` — content dedup fired; `matches` listed. The right response is `memory_update` on the matched id.
- `"previously_removed"` — tombstone dedup fired; `removed_matches` listed with their original `removed_reason`. Either drop the write or `memory_restore` the tombstone.
- `"scope_mismatch"` — body cites a known `projects:<name>` scope's name (or a path under another project's tree) AND that scope isn't declared. `suggested_scopes` and `matches` returned.
- `"pending"` — when `category="user-inference"` OR `require_write_confirmation = true`. `pending_reason` distinguishes the two.
- `"ungrounded"` — groundedness gate fired. `claims: [{sentence, overlap_ratio}, ...]` returned. No commit.

A `committed` or pending-confirm response may carry an inline `curation_hint` block once per session. It fires on the first `memory_write` (or `memory_write_confirm`) whose call would otherwise return successfully AND whose `dead_weight + drifted + cold_endorsement_memories` pressure crosses `[behavior] curation_hint_threshold` (default `5`). Shape: `{pressure: int, threshold: int, counts: {dead_weight, drifted, cold_endorsement_memories}, message: str}`. One-shot per session — the check sets a session flag whether or not it crossed the threshold, so subsequent writes don't re-walk the event log. Disable structurally with `curation_hint_enabled = false` or `curation_hint_threshold = 0` in `[behavior]`. Pull-based discovery (calling `memory_scope_overview` / `memory_health`) remains the primary surface; this is a passive notification for a model that never asks.

### `memory_write_confirm(pending_id)` and `memory_write_cancel(pending_id)`

- `pending_id: str`. Returned by a `memory_write` whose result was `"pending"`. Pending entries expire after one hour.

### `memory_update(id, content?, scopes?, confidence?, category?, links?)`

At least one of `content`, `scopes`, `confidence`, `category`, `links` must be provided. `scopes` and `links` have REPLACE semantics (pass the full new list; `[]` clears).

`category` accepts `"fact"` and `"ambient"`; `"user-inference"` is rejected because that category gates the pending-confirm WRITE flow and there's no equivalent gate on update.

Preserves `id`, `created`, `source`. Bumps `updated`. Content changes reset `last_verified_at` to `null` AND clear the `verified_paths` / `verified_commits` / `verified_versions` attestation lists — the old attestation was for prose that no longer exists. Scope, confidence, category, and links edits preserve verification (they don't touch the body's claims).

Optimistic-concurrency CAS (W2, since 3.2.0). The handler snapshots the on-disk `updated` via `memory_show` (or an equivalent prior read) and the store's under-lock check refuses the write when another agent landed an update in between. Returns `{"status": "stale", "memory_id", "current_updated", "hint"}` — `current_updated` is the on-disk `updated` ISO timestamp at the moment the CAS failed; `hint` is the human-readable retry instruction. No partial write — the prior writer's change is intact. Re-fetch with `memory_show` and retry the edit on top of the current snapshot; do not auto-retry from the same caller stack, the conflict may need reconciliation (e.g. both edits touched the same sentence). Distinct from the genuine-not-found / tombstoned `ValueError` paths, which still surface as raised exceptions.

### Inter-memory links

`links: list[MemoryLink]` is persisted in YAML frontmatter. Each `MemoryLink` is `{type, target_id, note?}`:

- `type`: one of `"supersedes"`, `"contradicts"`, `"extends"`, `"depends_on"`.
- `target_id`: a valid ULID — the other memory.
- `note`: optional free-form string.

Self-links are rejected. `memory_show` surfaces forward `links` on the source and `reverse_links` on the target. Forward-compat: a link entry with an unknown type or invalid `target_id` is silently dropped — that entry only; the record's other valid links still load, rather than the whole list failing.

## Lifecycle

### `memory_remove(id, reason)`

- `id: str`. Required.
- `reason: str`. Required, non-empty. Captured into the tombstone's `removed_reason` and surfaced by future `memory_write` calls whose new body overlaps.

### `memory_restore(id)`

- `id: str`. Must reference a tombstone.

Strips removal frontmatter; preserves `created`, `updated`, `last_verified_at`.

Race-loss surface (W7, since 3.2.1). The handler translates `MemoryNotFoundError`, `NotTombstonedError`, and bare `OSError` from the store layer into `ValueError` at the MCP boundary — a structured callers-of-MCP boundary error rather than a leaked store-layer exception. Race-loss vs. genuine-not-found is disambiguated by the message: a `ValueError` whose message contains the substring `"raced with"` is the race-loss shape (a concurrent restore or prune completed between the find and the under-lock recheck — the id either already-active or already-gone), while a `ValueError` without that substring is a genuine not-found or already-active case (e.g. `"memory <id> is active; nothing to restore"` without the parenthetical hint). Callers that want to differentiate programmatically can match on the `"raced with"` substring; callers that just want to surface the message to the user can treat all `ValueError`s uniformly. No partial restore — either the active record exists with the full restored frontmatter, or the tombstone is untouched. Re-fetch via `memory_list` / `memory_list_tombstones` to determine which side of the race won and act accordingly.

### `memory_list_tombstones(scopes?)`

- `scopes: list[str] | None = None`. Filter as in `memory_list`.

## Verification

### `memory_verify(id, note?, verified_paths?, verified_commits?, verified_versions?)`

- `id: str`. Required.
- `note: str | None = None`. Free-form; recorded in the event log.
- `verified_paths: list[str] | None = None`. The actual filesystem paths spot-checked.
- `verified_commits: list[str] | None = None`. The actual commit hashes spot-checked.
- `verified_versions: list[str] | None = None`. The actual version strings spot-checked.

Bumps `last_verified_at` without touching `updated`. Idempotent. The structured attestation lists are persisted on the record. The path-drift detector uses `verified_paths` to mark previously-attested paths that still exist as `verified`, downgrading the verdict. The commit-drift signal narrows its count to commits that touched any `verified_paths`. Calling with `verified_paths=None` preserves any prior attestation; an explicit `[]` clears it.

Optimistic-concurrency CAS (W8, since 3.2.1). The handler snapshots the record via `memory_show` and the store's under-lock check refuses the write when another agent landed a verify in between. The fingerprint is `last_verified_at` (NOT `updated` — verify is orthogonal to content edits, so `updated` is the wrong axis to watch; snapshot `last_verified_at` instead). Returns the same shape as `memory_update`'s stale response: `{"status": "stale", "memory_id", "current_updated", "hint"}`. `current_updated` echoes the on-disk `updated` for uniformity with the W2 contract — the caller's rebase action is identical (`memory_show` re-fetch), so the field name stays stable across the two surfaces. No partial write — the prior verifier's `verified_*` lists are intact (REPLACE semantics on those lists make this race especially nasty: a silent merge would lose one agent's attestation, so the contract is reread + reattest). Re-fetch, reassess your attestation against the now-current `verified_*` lists, and retry.

## Curation

### `memory_record_use(memory_ids, outcome, note?, claim_excerpts?)`

- `memory_ids: list[str]`. Always plural, even for one memory.
- `outcome: str`. One of `"applied"`, `"ignored"`, `"contradicted"`, `"corrected"`. `"corrected"` is the audit-only sibling of `"contradicted"` for the noticed-and-fixed-inline workflow; it never raises the contradiction flag.
- `note: str | None = None`.
- `claim_excerpts: list[str | None] | None = None`. Parallel to `memory_ids` — one entry per id (max 500 chars), or `None` for "no specific claim". Recorded in the event log so an audit can trace any response back to the specific claim, not just the memory id. Empty strings are rejected (pass `None` instead).

Returns `{recorded: [<memory_id>...], outcome}` — `recorded` echoes the ids whose pending tokens were settled by this call — plus `claim_excerpts` when they were supplied.

Auto-commit: every `memory_search` hit and `memory_show` response carries an opaque `use_token`. If `memory_record_use` isn't called within ~2 turns, the server auto-commits as `outcome="applied"` on the next `memory_*` call (logged with `auto=true, attribution="auto"`). Explicit calls win — the server purges the pending token before recording and writes `attribution="model"`.

Hook attribution: the Stop hook (`bettermemory audit-turn`) also looks at the assistant's reply text against recently-retrieved memory bodies. When a candidate sentence from a body appears verbatim (case- and whitespace-normalised) in the reply, the hook emits its own `applied` event with `attribution="hook"`, `auto=false`, and the matched phrase as the `claim_excerpt`. The in-process auto-commit then reads the event log and purges any token whose memory_id was already hook-attributed, so each retrieval generates exactly one `applied` event (hook, model, or auto — not multiple). Older events without an `attribution` field fall back to `"model"` when `auto=false` and `"auto"` when `auto=true`, so back-compat with pre-attribution logs is implicit.

### `memory_health(window_days?, heavily_used_top_k?, min_applied?)`

- `window_days: int = 30`. Dead-weight cutoff window.
- `heavily_used_top_k: int = 10`.
- `min_applied: int | None = None`. Falls through to `behavior.heavily_used_min_applied` (config default `3`).

Returns the aggregate rollup: `generated_at` (ISO timestamp of when the report was computed — caller-side time-pinning so a stored report's recency is unambiguous), `window_days` (the analysis window actually used; echoes the `window_days` argument with its config-fallback default of 30 applied), `total_active_memories`, `total_events`, `distinct_sessions`, `dead_weight`, `cold_memories`, `heavily_used` (with per-row `applied=N (auto=X exp=Y)` split), `contradicted` (each row carries a `resolution_timeline`), `marker_stats`, `scope_distribution`, `scope_health`, `rare_scopes`, `orphan_use_events`, `verification_debt`, `commit_drift_debt` (null when the server isn't in a repo whose memories live in this store), `silent_misses`, `recent_silent_misses` (the bounded newest-first list of recent miss candidates, each `{event_id, top_hit_id, …}` — feed an `event_id` to `memory_acknowledge_miss`), `cold_endorsement_memories`, and `recommendations`.

`cold_endorsement_memories` is the per-memory count (NOT per-turn) of distinct memories with `retrieval_count >= N` AND zero explicit applies — the "weakly endorsed" pattern where the ranker keeps surfacing a memory but the model never deliberately calls `memory_record_use(applied)` on it. A single memory the ranker hits 50 times contributes 1, not 50.

`recommendations: list[Recommendation]` distills the bucket detail above into one-line actions. Each entry is `{kind, summary, action, count, memory_ids, scope}`, where `kind` is one of the closed set `"remove_dead_weight" | "resolve_contradicted" | "cleanup_cold_endorsements" | "verify_drifted" | "fix_typo_scopes"`, `memory_ids` is capped at 10 entries (the uncapped `count` still reports true size), and `scope` is populated only on scope-level recommendations (the typo-singleton case). Size-driven kinds (`remove_dead_weight`, `cleanup_cold_endorsements`, `verify_drifted`) require at least 3 rows in the underlying bucket before they fire; `resolve_contradicted` and `fix_typo_scopes` surface from a single row. Empty list means every bucket sits below its floor — pull-based reads of the raw buckets remain the primary path; `recommendations` is the additive digest for in-conversation surfacing.

The `silent_misses` rollup carries `{audited_total, miss_total, unique_miss_memories}`. `miss_total` is the event count (one per `search_miss` event); `unique_miss_memories` is the cardinality of the set of top-hit memory_ids on those events — distinguishes "9 events against 1 mis-tagged memory" from "9 events across 9 memories." Misses whose top-hit memory has been tombstoned are dropped from both `miss_total` and `unique_miss_memories` (the miss is no longer actionable once the memory is gone). The rollup also honors a `silent_miss_cutoff` event when present — written by `bettermemory consolidate --acknowledge-misses-before <ISO_TS>` to invalidate pre-fix `turn_audited` / `search_miss` events after a change that obsoletes them. CLI-only; no MCP surface.

`dead_weight` and `cold_memories` measure different failure modes: dead weight is *"retrieved but didn't help"*, cold is *"the ranker isn't surfacing this at all"*. Use them to act on the right axis.

### `memory_curate(dry_run?, window_days?)`

Execute the curation `memory_health` only describes — its `recommendations` point at the `bettermemory consolidate` CLI, which an in-session model can't run. Wraps the same `consolidate()` engine the Stop-hook `run_auto_consolidate` path uses, behind a dry-run-by-default safety contract.

- `dry_run: bool = True`. When `True` (the default) the call is a side-effect-free **preview**: it returns the full report with the store untouched and records no event. Re-call with `dry_run=False` to commit.
- `window_days: int = 30`. Dead-weight age cutoff; must be ≥ 1.

Returns the consolidate report dict plus `dry_run`: `dedup_candidates`, `demotion_candidates`, `cold_scope_suggestions`, `scope_typo_pairs`, `actions_taken`, `failures`, `dedup_method`, `applied`. On `dry_run=False` the two reversible actions are applied — near-duplicate memories are tombstoned (undo via `memory_restore`) and dead-weight facts (created before the window, retrieved at least once, never applied) are demoted to the `ambient` category (undo via `memory_update`); `actions_taken` lists each with a `.kind` of `"tombstoned"` or `"demoted_to_ambient"`. Cold-scope and scope-typo findings are **suggest-only** regardless of `dry_run` — act on them via `memory_rename_scope`. Dedup uses Jaccard overlap (no embedding model is loaded). Nothing is hard-deleted; an apply records one `curate` event for the tool-usage rollup.

### `memory_audit_turn(user_message, assistant_response?, lookback_seconds?)`

Silent-miss telemetry. Fires from a client-side end-of-turn hook with the user's message. Runs a search probe over the active store using the configured ranker and asks whether a `search`, `show`, or `list` event landed in the same session within `lookback_seconds` (default 60s, clamped to [1, 600]).

Always emits `turn_audited` so audit cadence stays visible. Emits `search_miss` additionally when a high-relevance probe hit exists AND no retrieval happened in the window. The threshold rule is versioned (`THRESHOLD_RULE_V1 = "v1_top1_high"`) and recorded on every event so a calibration pass can replay historical logs.

### `memory_acknowledge_miss(event_id, reason)`

- `event_id: str`. Required. The per-event ULID stamped on a `search_miss` event — surfaced in `memory_health`'s `recent_silent_misses` list.
- `reason: str`. Required, ≥ 8 characters. Free-form why this flagged miss is a false positive (the model already had the context open, the hit was off-topic, etc.).

Emits an `acknowledge_miss` event so the miss drops out of the actionable silent-miss counters (`memory_scope_overview` / `memory_health`). Idempotent: re-acking the same `event_id` returns the same result without writing a duplicate event.

### `memory_rename_scope(old_scope, new_scope, include_tombstones?)`

- `old_scope: str` and `new_scope: str`. Required.
- `include_tombstones: bool = True`.

Returns `{old_scope, new_scope, active: [ids], tombstoned: [ids]}` — the normalized scopes echoed back, plus the ids of the records actually touched.

### `memory_proposals(action?, proposal_id?, scopes?, category?)`

Review the write-reflex proposal queue — durable statements the Stop hook captured from the user's messages that were never written as memories (the capture half of the self-improving loop; opt-in via `[proposals] auto_propose`). Proposals are inert until accepted, so nothing is ever written without an explicit accept.

- `action: str = "list"`. One of `"list"`, `"accept"`, `"dismiss"`.
- `proposal_id: str | None = None`. Required for `accept` / `dismiss`; the `id` from a `list` row.
- `scopes: list[str] | None = None`. Required for `accept` — a memory needs at least one scope and the queue does not guess them.
- `category: str | None = None`. Optional override for `accept`; defaults to the proposal's `suggested_category` (`fact` / `user-inference` / `ambient`).

`list` returns `{status: "ok", action: "list", count, proposals: [{id, body, source_excerpt, suggested_category, created}]}`. `accept` writes the proposal as a normal memory (source=`inferred`), removes it from the queue, and returns `{status: "accepted", id, proposal_id, scopes, category}`. `dismiss` drops it and returns `{status: "dismissed", proposal_id}`. A missing id returns `{status: "not_found", ...}`. Surfaced for discovery via `memory_scope_overview`'s `proposals_pending` count.

## Session-local

### `memory_scope_disable(scope)` and `memory_scope_enable(scope)`

- `scope: str`. Singular.

Resets when the server process restarts. Disabled scopes are filtered from `memory_search`, `memory_list`, and `memory_scope_overview`.

## Episodes (sibling tier for journal-shaped run-state)

Episodes are NOT memories. They live in a sibling subtree (`<root>/episodes/<session_id>/<ulid>.md`), are excluded from `memory_search` / `memory_health` / `memory_list`, and have no durability gate. Use them for loop-iteration takeaways, "what we tried", and any content `memory_write` would (correctly) reject as transient. A 30-day TTL on session directories runs on each `episode_write` so the directory stays bounded.

The optional `swarm_id` on `episode_write` / `episode_search` is the multi-agent fan-in primitive (since 3.3.0): a coordinator fans out parallel sub-agents, each sub-agent tags its episodes with the coordinator's session id, and the coordinator gathers every sub-agent's takeaways with one `episode_search(swarm_id=…)`. It is a cross-cutting cohort label, orthogonal to the single-chain predecessor link `episode_handoff` resolves.

### `episode_write(body, takeaway?, scopes?, swarm_id?)`

Append a new episode for the current session.

- `body: str`. Required, non-empty. Free-form markdown.
- `takeaway: str | None`. One-sentence summary. Surfaced preferentially at `episode_handoff`; falls back to the first body line when absent.
- `scopes: list[str] | None`. Empty list is valid — episodes are keyed by `session_id`, scopes are tags for filtering.
- `swarm_id: str | None = None`. Optional cohort id for multi-agent swarm fan-in. When a coordinator fans out parallel sub-agents, each sub-agent passes the coordinator's session id here so the coordinator can later gather every sub-agent's takeaways via `episode_search(swarm_id=…)`. The episode still lives under this writer's own session directory; `swarm_id` is a cross-cutting label, distinct from `episode_handoff`'s single-chain predecessor link. Validated (charset + length) by the `Episode` model; an invalid value raises a `ValueError` that surfaces like the body / takeaway / scope caps.

Returns `{status: "committed", id, session_id, created, scopes, takeaway, swarm_id, pruned_sessions: [<sid>...]}`. `session_id` is auto-captured from the recorder; `origin` (cwd / repo / branch / worktree_root) is captured the same way as `memory_write`. `pruned_sessions` lists any session directories that hit the TTL on this write.

Size caps: `body` enforces `max_content_bytes` (default 1 MB, same cap `memory_write` / `memory_update` enforce); `takeaway` enforces `max_takeaway_bytes` (default 4 KB). The takeaway cap is separate because takeaways serialize into the YAML frontmatter region (64 KB ceiling) — an over-cap takeaway would silently corrupt the frontmatter, the loader would raise on every subsequent read, and the episode would vanish from `episode_search` / `episode_handoff` / `episode_promote` despite the write returning `status="committed"`.

### `episode_handoff(prior_session_id?, max_episodes?)`

Read recent takeaways from a prior session. Designed as the FIRST MCP call at `/loop` iteration entry.

- `prior_session_id: str | None`. When omitted, the handler resolves the most recent session in the event log whose id differs from the current recorder's. Pass explicitly when the caller already knows the parent session (e.g. subagent handoff).
- `max_episodes: int | None`. Default `5`, cap `50`.

Auto-resolution applies two implicit filters when `prior_session_id` is omitted:

- **Caller-worktree strict equality.** A candidate session is only adopted when at least one of its episodes carries an `origin.worktree_root` equal to the caller's captured worktree, OR (the zero-episode branch) when both are `None`. `None` matches only `None` — a caller in a named worktree never adopts a candidate with unknown / null worktree, and a caller with no worktree never adopts a candidate from a named one. Two worktrees of the same repo never see each other's prior sessions; cross-worktree session ids can't leak through this surface.
- **`disabled_scopes` cascade.** Sessions whose only episodes overlap the current session's `memory_scope_disable` set are filtered out of candidate adoption, AND any surviving candidate's emitted episodes are themselves scope-filtered against the disabled set before return. Mirrors the same opt-out cascade `memory_search` / `memory_list` honor.

Returns `{prior_session_id: str | None, episodes: [{id, created, takeaway, body, scopes}, ...]}`. `prior_session_id is None` AND `episodes == []` is the "no baseline" case; `prior_session_id != None` AND `episodes == []` is "baseline exists but no journal" — branch on both.

### `episode_search(scopes?, parent_session_id?, swarm_id?, since?, max_results?, auto_scope?)`

Cross-session lookup. Unlike `memory_search`, NOT ranked — episodes are chronological and the filter set is the discovery surface.

- `scopes: list[str] | None`. Intersection filter (a hit's scopes must include at least one).
- `parent_session_id: str | None`. Restrict to one session directory. Composes with `swarm_id` to narrow a fan-in to one sub-agent's session.
- `swarm_id: str | None = None`. Fan-in filter — return only episodes tagged with this cohort id, gathered across all session directories. This is the N:1 swarm read: pass the coordinator's session id (the same value each sub-agent passed to `episode_write`) to gather every sub-agent's takeaways in one call. Distinct from `episode_handoff`'s 1:1 single-chain predecessor lookup. When set, it takes precedence over the bare `parent_session_id`-only / no-filter walk.
- `since: str | None`. ISO-8601 timestamp; only episodes created at or after.
- `max_results: int | None`. Default `20`, cap `200`. The cap surfaces the **most-recent N** matches (the slice keeps oldest-first ordering inside that window — "what did I conclude across the last few sessions?" reads the tail, not the head).
- `auto_scope: bool = True`. Worktree isolation for the **bare discovery walk** (no `swarm_id` / `parent_session_id`): drops episodes whose `origin.worktree_root` doesn't match the caller's, mirroring `memory_search.auto_scope` and the isolation `episode_handoff` enforces. An explicit `swarm_id` or `parent_session_id` is a deliberate cross-tree read and is **never** worktree-filtered (the swarm fan-in gathers sub-agents that each ran in their own worktree). Legacy / no-origin episodes and callers outside any git checkout pass through. Set `False` to sweep the bare walk across every worktree sharing the memory root.

Returns a list of `{id, session_id, created, takeaway, body, scopes, swarm_id}` dicts oldest-first within the most-recent-`max_results` window. `session_id` is included because `episode_search` spans sessions (unlike `episode_handoff`, which scopes to one), so the caller can correlate a takeaway back to its originating session directory; `swarm_id` (may be `null`) is the cohort tag. Session-tag floor episodes (the crash-recovery anchors `episode_handoff` writes) are filtered out of this surface.

### `episode_promote(episode_id, scopes, category?, confidence?, source?, use_body?)`

Distill a journal takeaway into a durable memory. Routes through `memory_write` — the full durability gate fires.

- `episode_id: str`. Required.
- `scopes: list[str]`. Required (memory scopes are non-empty).
- `category: str = "fact"`, `confidence: str = "medium"`, `source: str = "explicit-statement"`. Standard `memory_write` fields.
- `use_body: bool = False`. When False (default), the episode's `takeaway` becomes the memory body; when True, the full body. An episode without a takeaway requires `use_body=True`.

Returns the `memory_write` response shape with one extra field: `promoted_from_episode_id: str` so the caller can correlate. On `status="committed"` the source episode is deleted (the durable memory is the authoritative artifact). On `status="pending"` (user-inference promotion or `require_write_confirmation`), the episode is held; `memory_write_confirm(pending_id)` deletes the source episode on commit, and `memory_write_cancel(pending_id)` preserves the episode so the caller can rephrase and re-promote. On any other non-committed status (duplicate, previously_removed, transient_warning, scope_mismatch, ungrounded) the episode is left intact.

## Naming conventions

These hold across the surface:

- `id` is the positional first argument when a tool acts on one memory.
- `scopes` (plural) is the list-filter parameter; `scope` (singular) is the single-scope action parameter.
- Enum-typed parameters are plain `str` in the JSON surface and validated against closed sets at the handler (`confidence`, `source`, `category`, `outcome`, `mode`, `link.type`).
- Required arguments are always named in the description; defaults are conservative (`force=False`, `acknowledge_transient=False`, `with_bodies=False`, `category="fact"`).
- `memory_update` requires at least one of `content`, `scopes`, `confidence`, `category`, `links` at runtime — not expressible in JSON Schema, but the handler returns a clear error.

The 3.x surface is the contract. Additions follow the rules in [`CONTRIBUTING.md`](../CONTRIBUTING.md). Removals or renames wait for 4.0.
