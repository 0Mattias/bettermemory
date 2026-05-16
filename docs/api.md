# API surface (1.x)

This document is the contractual list of MCP tools bettermemory exposes. Signatures, defaults, and return shapes are stable within the 1.x line. See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the deprecation policy. The 1.x surface was frozen at 1.0; subsequent additions are permitted (and several have landed in 1.1, 1.2, and 1.3: the `commit_drift` block on retrieval hits, the `category` parameter on `memory_write` and `memory_update`, the `corrected` outcome on `memory_record_use`, the `verification_debt` and `commit_drift_debt` rollups on `memory_health`, the `staleness_verdict` on every retrieval, auto-`record_use` via `use_token`, and the `verified_paths` / `verified_commits` / `verified_versions` parameters on `memory_verify`). Renames, removals, and semantic redefinitions are forbidden until a 2.0 bump.

The 17 tools group naturally:

- **Retrieval**: `memory_search`, `memory_show`, `memory_list`, `memory_scope_overview`
- **Writing**: `memory_write` (plus `memory_write_confirm` and `memory_write_cancel` for the staged-write flow), `memory_update`
- **Lifecycle**: `memory_remove`, `memory_restore`, `memory_list_tombstones`
- **Verification**: `memory_verify`
- **Curation**: `memory_record_use`, `memory_health`, `memory_rename_scope`
- **Session-local**: `memory_scope_disable`, `memory_scope_enable`

## Retrieval

### `memory_search(query, scopes?, max_results?, expand_top?, auto_scope?, mode?)`

Rank stored memories against a free-text query.

- `query: str`. Required.
- `scopes: list[str] | None = None`. When set, only memories carrying at least one of these scopes are eligible.
- `max_results: int | None = None`. Falls through to `behavior.default_max_results` (config default `5`). Capped at 50.
- `expand_top: bool = False`. When the top hit's relevance is `"high"`, inline its full body and `path_drift` report so the caller can act without a `memory_show` round-trip. No-op for non-`"high"` top hits.
- `auto_scope: bool = True`. Filter by the caller's current git repo. Memories with no recorded `origin` (legacy entries or writes from outside any repo) are treated as global and always pass.
- `mode: str | None = None`. Ranker selection — one of `"keyword"` (TF + coverage + recency; the original scorer), `"bm25"` (Okapi BM25 with the same scope-bonus + recency), `"semantic"` (sentence-transformers cosine; requires the embeddings extra and raises if missing), or `"hybrid"` (RRF fusion of keyword + BM25, plus semantic when the extra is installed). Per-call override beats the config default `[behavior] search_mode`, which itself falls back to `"keyword"` in 1.6.0 for byte-stable ranking. Use `"hybrid"` when the query paraphrases what you expect the memory to say; stick with `"keyword"` for literal identifiers or file paths. The fused hybrid score lives in a smaller scale (~`0.01–0.05` from RRF) than single-ranker scores — compare across modes via `relevance`, not raw `score`.

Returns a list of hits. Each hit carries `id`, `scopes`, `relevance` (`"high"`, `"medium"`, or `"low"`), `match_terms`, `snippet`, `created`, `updated`, `last_verified_at`, `verification`, `path_drift_checked` and `path_drift_missing` integer counts, `staleness_verdict`, a `use_token`, and (when applicable) a `commit_drift_count` integer. The `commit_drift_count` field is OMITTED from the hit when the signal is not applicable: caller not in any repo, hit from a different repo, or hit has never been verified. A non-zero value is the cue to spot-check the memory even when `verification.status == "fresh"`, because the project has moved since the last `memory_verify`.

Negative-outcome annotations (T2.3): when a hit's memory has been `ignored` or `contradicted` within the last 30 days AND not since `applied`, the hit also carries a `recent_negative_outcomes` list. Each entry has shape `{outcome, most_recent_ts, count_in_window, session_id, note, claim_excerpt}` — at most one entry per outcome type, so two entries maximum (one for `ignored`, one for `contradicted`). The supersession rule is the load-bearing semantic: an `applied` event after a negative event clears the negative-bucket entries, because the user already validated the memory after the rejection. The `claim_excerpt` field (T1.1) is the load-bearing claim recorded at rejection time, when present, so the caller can rephrase or skip just the offending sentence rather than the whole body. The field is OMITTED from the hit (rather than emitted as null) when no qualifying negatives exist — absence is the default.

### Inter-memory links (T2.2)

Memories can carry typed links to other memories. The schema lives on the `Memory` model as `links: list[MemoryLink]` (persisted in YAML frontmatter; legacy memories load with `[]`).

Each `MemoryLink` is `{type, target_id, note?}`:

- `type`: one of `"supersedes"`, `"contradicts"`, `"extends"`, `"depends_on"`.
- `target_id`: a valid ULID — the other memory this one relates to.
- `note`: optional free-form string capturing *why* the link exists.

Set via `memory_update(id, links=[...])` — REPLACE semantics, pass the full new list. `links=[]` clears all links atomically. Self-links are rejected. Surface at retrieval is bidirectional: `memory_show` on the source memory carries the forward `links` list; `memory_show` on the target carries `reverse_links` (with `source_id` in place of `target_id`). Forward-compat: an unknown link type on disk loads as `[]` rather than failing the whole memory record.

### `memory_write(content, scopes, ...)`

Beyond the existing parameters (`confidence`, `source`, `force`, `acknowledge_transient`, `acknowledge_scope_mismatch`, `category`), `memory_write` accepts an opt-in groundedness check (T1.3):

- `groundedness_check: bool = False`. Opt-in. When True (and `source_transcript` is provided), the server walks the proposed body sentence-by-sentence, checking each sentence's content tokens against the transcript's token set. A sentence whose overlap ratio falls below 30% is flagged as ungrounded.
- `source_transcript: str | None = None`. The recent conversation turns that motivated this write — a free-form string concatenating whatever the caller considers the grounding source. Required for the gate to fire (otherwise the gate is a no-op).
- `acknowledge_ungrounded: bool = False`. Override the gate when the caller has other grounding sources (file reads, tool results) that aren't represented in the transcript. Same family as `acknowledge_transient` and `acknowledge_scope_mismatch`.

On failure: `{status: "ungrounded", claims: [{sentence, overlap_ratio}, ...], hint: "..."}` — the write does not commit. Each `claim` entry carries the verbatim flagged sentence and its `overlap_ratio` (the fraction of the sentence's content tokens that appeared in the transcript). Operationalises the HaluMem benchmark inline; no competitor in the May 2026 landscape runs a write-time groundedness gate.

### `memory_show(id)`

- `id: str`. Required, the memory's ULID.

Returns the full body plus `verification` (full block), `path_drift` (full report when there is drift; `null` otherwise), `staleness_verdict`, a `use_token`, and `commit_drift` (full block when applicable; `null` when the caller is not in the matching repo or the memory was never verified). The `commit_drift` block carries `status` (`"clean"` or `"drift"`), `commits_since_verify` (the integer count), and `recommendation` (actionable string on `"drift"`, null on `"clean"`).

### `memory_list(scopes?, with_bodies?)`

- `scopes: list[str] | None = None`. Filter, same shape as `memory_search`.
- `with_bodies: bool = False`. Opt in to inlining full bodies. Triage default returns body-stripped summaries.

Each row carries the same `staleness_verdict` rollup as the search and show surfaces.

### `memory_scope_overview(auto_scope?)`

Cheap session-start hint. Counts per scope without bodies or IDs.

- `auto_scope: bool = True`. Same semantics as `memory_search.auto_scope`.

Returns `{current_repo, scopes: {scope: count}, total, curation_pending}`. The `curation_pending` rollup is five integer counts (`stale`, `never_verified`, `drifted`, `cold`, `dead`) derived from the same logic as `memory_health` but without row materialisation. Lets the model spot pending curation at session start without paying the full health cost.

## Writing

### `memory_write(content, scopes, confidence?, source?, category?, force?, acknowledge_transient?, acknowledge_scope_mismatch?)`

- `content: str`. Required.
- `scopes: list[str]`. Required, non-empty.
- `confidence: str = "medium"`. One of `"low"`, `"medium"`, or `"high"`. Plain string in the JSON surface; validated against the `Confidence` enum at the handler.
- `source: str = "explicit-statement"`. One of `"explicit-statement"` or `"inferred"`. Same string-vs-enum note.
- `category: str = "fact"`. One of `"fact"`, `"user-inference"`, or `"ambient"`. `"fact"` (default) commits immediately. `"user-inference"` structurally goes pending and returns `{status: "pending", pending_id, pending_reason: "user-inference"}` regardless of the global `behavior.require_write_confirmation` config. Use `"user-inference"` for memories that capture claims about the user themselves (preferences, beliefs, working style) so the user always gets the conversational veto on misattribution. `"ambient"` commits like `"fact"` but is excluded from the dead-weight curation rule (its value is implicit, so a count of zero `applied` events is not an indictment); long bodies (over 500 words) attach a non-blocking `ambient_body_long` warning.
- `force: bool = False`. Bypass content dedup AND tombstone dedup. Reserve for the case where the new memory is genuinely adjacent to an existing match, not a duplicate of it.
- `acknowledge_transient: bool = False`. Bypass the durability marker check. Logged as an override.
- `acknowledge_scope_mismatch: bool = False`. Bypass the scope-mismatch warning when a cross-project reference is intentional.

Possible result statuses:

- `"ok"`: committed; the payload includes the new memory id and any `related` medium-overlap matches.
- `"transient_warning"`: durability gate fired; markers listed.
- `"duplicate"`: content dedup fired; `matches` listed.
- `"previously_removed"`: tombstone dedup fired; `removed_matches` listed with their original `removed_reason`.
- `"scope_mismatch"`: the body cites a known `projects:<name>` scope's name token (or a path under another project's tree) AND that scope is not in the declared scope list. Returns `suggested_scopes` and `matches`. Override via `acknowledge_scope_mismatch=True`.
- `"pending"`: when `category="user-inference"` is passed, OR when `require_write_confirmation = true` in config. `pending_reason` distinguishes the two; the caller must follow with `memory_write_confirm` either way.

### `memory_write_confirm(pending_id)` and `memory_write_cancel(pending_id)`

- `pending_id: str`. Returned by a `memory_write` whose result was `"pending"`. Pending entries expire after one hour.

### `memory_update(id, content?, scopes?, confidence?, category?)`

- `id: str`. Required.
- `content`, `scopes`, `confidence`, `category`. At least one must be provided. `scopes` has replace semantics (provide the full new list, not a delta). `category` accepts `"fact"` and `"ambient"`; `"user-inference"` is rejected because that category gates the pending-confirm WRITE flow and there is no equivalent gate on update.

Preserves `id`, `created`, and `source`. Bumps `updated`. Resets `last_verified_at` to `null` on content change (the old verification was for prose that no longer exists). Category, scope, and confidence-only edits preserve `last_verified_at`.

## Lifecycle

### `memory_remove(id, reason)`

- `id: str`. Required.
- `reason: str`. Required, non-empty. Captured into the tombstone's `removed_reason` and surfaced by future `memory_write` calls whose new body overlaps the removed body.

### `memory_restore(id)`

- `id: str`. Must reference a tombstone (active IDs raise).

Strips removal frontmatter; preserves `created`, `updated`, and `last_verified_at`.

### `memory_list_tombstones(scopes?)`

- `scopes: list[str] | None = None`. Filter as in `memory_list`.

## Verification

### `memory_verify(id, note?, verified_paths?, verified_commits?, verified_versions?)`

- `id: str`. Required.
- `note: str | None = None`. Free-form; recorded in the event log.
- `verified_paths: list[str] | None = None`. The actual filesystem paths the caller spot-checked.
- `verified_commits: list[str] | None = None`. The actual commit hashes the caller spot-checked.
- `verified_versions: list[str] | None = None`. The actual version strings the caller spot-checked.

Bumps `last_verified_at` to now without touching `updated`. Idempotent. The structured attestation lists are persisted on the memory record. The path-drift detector uses `verified_paths` to mark previously-attested paths that still exist as `verified`, downgrading the `staleness_verdict` for that hit. The commit-drift signal narrows the count to commits that actually touched any of `verified_paths`. Calling `memory_verify` with `verified_paths=None` preserves any prior attestation; an explicit empty list `[]` clears it.

## Curation

### `memory_record_use(memory_ids, outcome, note?, claim_excerpts?)`

- `memory_ids: list[str]`. IDs that shaped the consuming response. Always plural, even for a single memory.
- `outcome: str`. One of `"applied"`, `"ignored"`, `"contradicted"`, or `"corrected"`. `"corrected"` is the audit-only sibling of `"contradicted"` for the noticed-and-fixed-inline workflow where the caller has already run `memory_update` or `memory_verify` in the same turn. `"corrected"` increments a separate `corrected_count` on `MemoryStats` and never raises the `has_unresolved_contradiction` flag, so the previous foot-gun ("logged contradicted after the fix, flag stuck because event ts was greater than resolution ts") is gone structurally.
- `note: str | None = None`.
- `claim_excerpts: list[str | None] | None = None`. Optional provenance signal — a list parallel to `memory_ids` (same length) carrying the specific claim the caller applied / ignored / contradicted / corrected from each memory. Each entry is the load-bearing phrase quoted from the body (max 500 chars), or `None` for "no specific claim noted for this id". Surrounding whitespace is stripped; empty strings are rejected (pass `None` instead). Recorded in the event log so a later audit can trace any response back to the specific claim, not just the memory id. Recommended whenever the memory shaped a user-visible sentence; especially useful for `contradicted` and `corrected` outcomes so the audit log records which claim was wrong, not just that the memory had drift. Byte-stable on the wire: the event-log entry omits the field entirely (rather than emitting a null value) when `claim_excerpts` isn't passed, so existing log readers and health rollups keep working untouched.

Auto-commit semantics: every `memory_search` hit and `memory_show` response carries an opaque `use_token`. If `memory_record_use` is not called within roughly 2 turns, the server auto-commits the retrieval as `outcome="applied"` on the next `memory_*` call (logged with `auto=true`). Explicit calls win over the auto pass: the server purges the pending token before recording so the auto-commit cannot shadow the explicit outcome.

### `memory_health(window_days?, heavily_used_top_k?, min_applied?)`

- `window_days: int = 30`. Dead-weight cutoff window.
- `heavily_used_top_k: int = 10`.
- `min_applied: int | None = None`. Falls through to `behavior.heavily_used_min_applied` (config default `3`).

Returns the aggregate health rollup: `total_active_memories`, `total_events`, `distinct_sessions`, `dead_weight` (created before the window with retrieval count greater than zero and applied count of zero, ambient memories excluded), `cold_memories` (created before the window with zero retrievals; distinct from dead weight, see below), `heavily_used`, `contradicted` (rows in this bucket carry a `resolution_timeline` so a stuck flag can be self-diagnosed as out-of-order audit logging vs genuinely unresolved), `marker_stats`, `scope_distribution`, `scope_health`, `rare_scopes` (singletons within Levenshtein distance 2 of another scope, almost always real typos), `orphan_use_events` (a fabricated-id smoke test), `verification_debt` (partitions active memories into `never_verified`, `stale`, and `fresh` against `behavior.verification_stale_days`), and `commit_drift_debt` (populated when the server runs in a repo whose memories live in this store; surfaces rows whose verification anchor sits behind HEAD, sorted most-commits-ahead first; `null` otherwise).

The `dead_weight` bucket and the `cold_memories` bucket measure different failure modes: `dead_weight` is "the model retrieves this but it does not help", while `cold_memories` is "the ranker is not surfacing this at all". The two together separate ranker quality from body quality, so a curation pass can act on the right axis.

### `memory_rename_scope(old_scope, new_scope, include_tombstones?)`

- `old_scope: str` and `new_scope: str`. Required.
- `include_tombstones: bool = True`. By default also rewrites scopes on tombstoned memories.

Returns `{active: [ids], tombstoned: [ids]}` for the records that were actually touched.

## Session-local

### `memory_scope_disable(scope)` and `memory_scope_enable(scope)`

- `scope: str`. Singular. Multi-scope mute is intentionally one-tool-call-per-scope; the muted set is small in practice.

Resets when the server process restarts. Disabled scopes are filtered from `memory_search`, `memory_list`, and `memory_scope_overview`.

## Audit conclusions (recorded for the 1.0 freeze, still applicable in 1.x)

The 1.0 surface audit deliberately compared every signature against the patterns elsewhere in the API. The conclusions still hold for the 1.x line:

- **Naming consistency.** `id` is always the positional first argument when a tool acts on one memory. `scopes` is always the parameter name for a list filter; `scope` (singular) is always the parameter name for a single-scope action. No hidden synonyms.
- **Plural vs singular.** `memory_record_use(memory_ids: list)` is plural even for the single-id case so the JSON surface is consistent with batch use. The cost is one extra `[…]` in the most common call shape, which is mild.
- **Required vs optional.** Required arguments are always named in the description; defaults are sensible (configuration-driven where applicable, conservative otherwise: `force=False`, `acknowledge_transient=False`, `with_bodies=False`, `category="fact"`).
- **Enums-as-strings.** `confidence`, `source`, `category`, and `outcome` are typed as `str` in the JSON surface and validated against closed sets at the handler. JSON Schema cannot model Python enums in a way most MCP clients render well; the closed-set validation gives equivalent safety with a friendlier wire format.
- **Mutually-exclusive optionals.** `memory_update` requires at least one of `content`, `scopes`, `confidence`, or `category` at runtime. This is not expressible in the JSON Schema published to clients, but the handler returns a clear error message when no field is set.

The 1.x surface is the contract. Additions follow the permitted-within-1.x rules in `CONTRIBUTING.md`. Removals or renames wait for 2.0.
