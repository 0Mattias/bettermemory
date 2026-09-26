# API surface (9.x)

The contractual list of MCP tools bettermemory exposes. Signatures, defaults and return shapes are stable within the 9.x line per the rules in [`CONTRIBUTING.md`](../CONTRIBUTING.md). There are nine tools, and all nine register on every start; there is no configuration that adds or removes one. Two of them multiplex what used to be separate tools behind an `action` parameter, so the surface stays small without losing the work: `memory_admin` carries the curation actions, `episode` the journal. The served size of the surface (every description, every input schema and the server `instructions` block, as `tools/list` and `initialize` deliver them) is measured by `bench/toolcost` and recorded in its dated artifacts.

- **Retrieval**: `memory_search`, `memory_show`
- **Writing**: `memory_write`, `memory_update`
- **Lifecycle**: `memory_remove`, `memory_admin(action="restore" | "tombstones")`
- **Verification**: `memory_verify`
- **Curation**: `memory_record_use`, `memory_admin(action="health" | "conflicts" | "acknowledge_miss" | "rename_scope")`
- **Session-local**: `memory_admin(action="disable_scope" | "enable_scope")`
- **Episodes** (the journal beside memory): `episode(action="write" | "handoff")`

The `bettermemory` CLI carries the rest: `up`, `down` and `status` for the local daemon, `serve`, `health`, `tombstones`, `rollback`, `rename-scope`, `episodes`, `export`, `migrate v8`, `log verify`, `eval`, `init`, `try`, and the hook command (`hook session-start|stop|prompt`; `session-start`, `audit-turn` and `prompt-recall` are its 8.x aliases). The [CLI](#cli) section lists them.

What 9.0 removed from the MCP surface, and where the work went: `memory_list` and `memory_scope_overview` (the session-start hook prints the per-scope counts; `bettermemory export` lists the store), `memory_write_confirm` and `memory_write_cancel` with the staged-write flow, `memory_audit_turn` (the Stop hook runs the audit out of process), `memory_curate` and `memory_proposals` with their modules, `episode_search`, `episode_promote` (a promotion is a `memory_write` the model makes from a handoff it has read) and `episode_patterns`, and the eight tools that became `memory_admin` actions. The [CHANGELOG](../CHANGELOG.md) entry for 9.0.0 has the full list.

## Retrieval

### `memory_search(query, scopes?, exclude_scopes?, max_results?, expand_top?, auto_scope?, since_prior_session?, mode?, client?, model?)`

Rank stored memories against a free-text query.

- `query: str`. Required.
- `scopes: list[str] | None = None`. When set, only memories carrying at least one of these scopes are eligible.
- `exclude_scopes: list[str] | None = None` (9.0.0). Memories carrying any of these scopes are dropped, before ranking, as the session's disabled scopes are. A scope in both lists is excluded.
- `max_results: int | None = None`. Falls through to `behavior.default_max_results` (config default `5`). Capped at 50.
- `expand_top: bool = False`. When the top hit's relevance is `"high"`, inline its full body and a freshly computed `path_drift` plus `commit_drift` report. No-op otherwise.
- `auto_scope: bool = True`. Filter by the caller's current repo and worktree, via `origin.should_include_for_caller`. The worktree half is **permissive by design and is not an isolation boundary**; four cases pass through: the memory records no `origin` (treated as global), the caller is outside any git checkout, the caller sits in a **linked worktree** of the checkout that wrote the memory (so a `git worktree`-based agent run still sees the primary checkout's knowledge), and the recorded worktree is **positively gone from disk**. What stays isolated is the live-sibling case: memories written in one live worktree do not surface in another. `origin.worktrees_match` is the rule; do not infer a stronger guarantee than it implements.
- `since_prior_session: bool = False`. When True, narrow candidates to memories whose `updated` is strictly after the latest event timestamp from any other `session_id` in the log: the current session's own changes. The boundary is the prior session's last event, so a memory whose `updated` equals it belongs to that prior session. Returns an empty list when no prior session exists (first run, wiped log). Bypasses the FTS5 prefilter so newly written rows outside the top-50 prefilter slice cannot drop; the post-boundary set is bounded by session activity. Pairs with `episode(action="handoff")`, which surfaces what the prior iteration did.
- `client: str | None = None`, `model: str | None = None`. Select on the writing request's **declared** identity, `actor.client` / `actor.model` as recorded on the memory (see [Identity](#identity-who-is-calling-and-from-where)). Matched exactly and case-sensitively, as a SQL `WHERE` on the candidate query, so the filter narrows the FTS slice instead of being applied after the cap. **A record whose writer declared nothing matches neither value**: every memory written before 7.10.0, and any written since by a client that names itself in no channel. A filtered result is a listing of labelled memories and never a census of who wrote what. And the values are declared, therefore forgeable: evidence for attribution, per-model telemetry and targeted rollback, never a permission boundary. `principal` is the attested field and is deliberately not filterable.
- `mode: str | None = None`. Ranker: `"hybrid"` (default: RRF fusion of keyword and BM25), `"keyword"` (TF plus coverage plus recency; no IDF, weaker on rare-term queries), or `"bm25"` (Okapi BM25 with the same scope bonus and recency). Every mode is deterministic lexical ranking; the project ships no embedding models, so vocabulary is the retrieval lever: query with the nouns a memory would contain. Per-call override beats `[behavior] search_mode`. The fused hybrid score lives on a smaller scale than the single-ranker scores; compare across modes via `relevance`, not raw `score`. The hybrid mode also runs the conversational repairs (`[behavior] conversational`, default on): when a query has a temporal reading ("how many weeks ago did I…", "what did I do in March?"), its temporal-scaffold words price as common words in the BM25 legs so the question's own syntax cannot outprice its content, a bare one- or two-digit numeral joins that class only when a time unit immediately follows it, and date-anchored items matching an explicit window get a bounded boost. Queries with no temporal reading are untouched, so `conversational = false` reproduces the plain ranking exactly. The rescue-expansion leg and the two usage-aware ranking factors 8.x carried behind flags are gone in 9.0: the retrieval bench still measures expansion as an arm, and the usage factors measured a wash on the owner's labelled replay.

Returns a list of hits. Each hit carries `id`, `scopes`, `relevance` (`"high"` / `"medium"` / `"low"`), `match_terms`, `matched_leg` (`"lexical"`; omitted in browse mode where nothing ranked), `snippet`, `created`, `updated`, `last_verified_at`, `verification`, `path_drift_checked` / `path_drift_missing` counts, `staleness_verdict`, `provenance`, a `use_token`, and a `commit_drift_count` when applicable (omitted when the caller is not in the memory's repo, the memory was never verified, or the memory has no claim anchors in this repo). Beside the count, `commit_drift_basis` names the axis it was measured on: `"reachability"`, the commits in `verified_head..HEAD`, for a record whose stamp recorded the checkout's HEAD and whose anchor the caller's HEAD still descends from; or `"author-date"`, the commits authored after `last_verified_at`, for a record verified without an anchor or whose anchor a rewritten history no longer reaches. When the body cites paths that no longer exist, or paths the user attested via `memory_verify`, the hit also carries `path_drift` with `{checked, missing, verified}` lists. A `claim_anchored_missing` key is added only when that bucket is non-empty: the subset of `missing` that moved `staleness_verdict` (an attested path that has since vanished, or a citation resolved against the memory's own recorded worktree). That list is the directly actionable one and arrives on the hit. The rest of `missing` is the prose-scraped half, shipped as evidence to weigh, not a tier (`path_drift_missing` stays the full-set count, so it can be non-zero on a `"fresh"` hit). An `expected_absent` key is added only when the memory carries a `verified_absent_paths` attestation that fired, and a `dropped_as_route` key only when the scanner suppressed a route-shaped candidate.

`provenance` is how the record entered the store, read from the store's log at every search: `local` (written, verified or restored through this host's own tool surface, and the log row that produced it verifies under the store's key), `imported` (brought in by `bettermemory migrate v8`), `synced` (reserved for the cross-host lane) or `unaccounted` (the row's pointer into the log is missing, names another record or fails its MAC: the planted shape, and also the shape of a log row appended without the key). The label is never read from the record; the [store](#the-store) section describes the pointer. A `synced` hit whose record carries a `last_verified_at` this host never made reports `verification.status: "remote"`, with the stamp kept in `last_verified_at`, `age_days: null`, a recommendation naming the local `memory_verify` that adopts it, and `staleness_verdict: spot_check_required` whatever the drift legs said; `verification.status` therefore takes four values, `never`, `stale`, `fresh` and `remote`. When this machine does not hold the store's key no pointer can be verified and no label derived: every hit then carries `trust_unavailable: true`, `provenance` is omitted, and a hit whose record carries a `last_verified_at` reads `spot_check_required` with a `verification.recommendation` naming the remedy. A stamp of unknown origin is not read as this host's. The key is absent whenever the store answered.

`snippet` is query-biased. A body inside the 200-character budget comes back whole; a longer one is windowed around the terms the query matched rather than truncated from the head, so the snippet shows why the hit came back. A window that does not start at the body head carries a leading `...` (and a trailing one unless it runs to the end of the body). Hits with no literal match terms (browse mode, an empty query) fall back to head of body, as do matches that landed on a scope rather than the body.

Hits also carry `recent_negative_outcomes` when the memory was `ignored` or `contradicted` within the last 30 days and not since `applied`. Each entry has shape `{outcome, most_recent_ts, count_in_window, session_id, note, claim_excerpt}`, at most one per outcome type. An `applied` event after a negative event clears the bucket. The field is omitted when no qualifying negatives exist.

Hits also carry `depends_on_resolved` when the hit's memory carries `depends_on` links: a list of `{id, scopes, summary, link_note}` entries, at most 3 per hit and 10 across the result set. Targets are pulled even when the query would not surface them, then re-filtered through the same `auto_scope` and disabled-scope rules, so a dependency edge cannot leak cross-project or hidden-scope content. Tombstoned targets are skipped. The field is omitted when the hit has no such links or every target resolves out.

Hits also carry `superseded_by` and/or `contradicts` when the hit's memory participates in a `supersedes` / `contradicts` link edge. `superseded_by` lists the active memories that supersede this hit (inbound `supersedes` edges); per the link contract the consumer should prefer them. `contradicts` lists memories in unresolved contradiction with this hit, in either direction (both endpoints surface it; reconcile via `memory_verify` / `memory_update`). Each entry is the same `{id, scopes, summary, link_note}` shape with the same caps and re-filter. Both keys are omitted when the hit has no such edges.

### `memory_show(id)`

Full body plus `verification` block, `path_drift` report (`null` when no drift), `staleness_verdict`, `use_token`, `provenance`, `commit_drift` block (`null` when not applicable; non-null shape is `{status, commits_since_verify, recommendation, basis}`, where `recommendation` is the actionable string to surface when `status == "drift"` and `basis` is `"reachability"` or `"author-date"` as described under `memory_search`), and typed inter-memory edges as `links` (forward) and `reverse_links` (entries from the target side carry `source_id` instead of `target_id`). The remote-stamp rule described under `memory_search` applies here, and so does `trust_unavailable: true` when this machine does not hold the store's key.

The non-null `commit_drift` block additionally carries `claim_drift: {checked, drifted}` when the memory declares [structured claims](#structured-claims) and the drift narrowing ran: `checked` is the claim count, `drifted` the rendered claims whose bindings the post-verify commits implicate. On `"clean"` a populated `checked` with empty `drifted` is the measurement that stood the memory down. The same sub-dict rides drifting search hits beside `commit_drift_count`.

Full return shape: `{id, scopes, confidence, source, category, created, updated, last_verified_at, verification, staleness_verdict, provenance, body, origin, path_drift, commit_drift, use_token, verified_paths, verified_commits, verified_versions, verified_absent_paths, claims, verified_head}` plus `corroborations` and `last_corroborated` (emitted as a pair, only once the count is non-zero), plus `actor` (who wrote the record; omitted when the writer declared nothing), plus `links` and `reverse_links` (each omitted when empty; `path_drift` / `commit_drift` take the opposite convention, always emitted with `null` as the no-signal value). The non-null `path_drift` report carries `{checked, missing, verified, expected_absent, dropped_as_route, claim_anchored_missing}`, all six buckets always present. A non-null `origin` always carries `source`: the channel that named the directory (`header`, `env`, `roots`) or `process-cwd`, the labelled fallback.

## Writing

### `memory_write(content, scopes, confidence?, source?, force?, acknowledge_transient?, acknowledge_scope_mismatch?, acknowledge_ungrounded?, acknowledge_credential?, acknowledge_user_claim?, category?, groundedness_check?, source_transcript?, claims?, supersedes?)`

In MCP every argument is keyword-only at the wire, so positional order is only consequential for Python callers reading this as a spec.

- `content: str`. Required; the only shipped lower bound is "non-empty after stripping". `[behavior] min_content_tokens` (default `0`, off) adds an opt-in floor on whitespace-token count for unattended or bulk callers. Below the floor the call raises rather than returning a status. The floor binds `memory_write` and not `memory_update`.
- `scopes: list[str]`. Required, non-empty.
- `confidence: str = "medium"`. One of `"low"`, `"medium"`, `"high"`.
- `source: str = "explicit-statement"`. One of `"explicit-statement"`, `"inferred"`, `"user-correction"`. `"user-correction"` is the tag for a memory created when the user contradicts an earlier inference: the body carries the corrected fact, the source records that the correction came from the user.
- `force: bool = False`. Bypass content dedup and tombstone dedup.
- `acknowledge_transient: bool = False`. Bypass the durability marker check. Logged as an override.
- `acknowledge_scope_mismatch: bool = False`. Bypass the scope-mismatch warning when a cross-project reference is intentional.
- `acknowledge_ungrounded: bool = False`. Override the groundedness gate when grounding sources (file reads, tool results) are not represented in the transcript.
- `acknowledge_credential: bool = False`. Bypass the credential-shaped-token check. Use only for a documented public or example value. Logged as an override, the detector `kind` only, never the value.
- `acknowledge_user_claim: bool = False`. Bypass the user-claim gate, which fires when the body predicates something about a person while `category` is not `"user-inference"`. Pass it when the subject is someone or something other than the user (a teammate, or a tool that "prefers" a setting). Otherwise re-issue as `category="user-inference"`, which is the remedy the gate's own hint names. Logged as an override, carrying the matched phrases.
- `category: str = "fact"`. One of `"fact"`, `"user-inference"`, `"ambient"`. `"user-inference"` is a claim about the user; it commits exactly as `"fact"` does, and the label is what keeps a stored inference distinguishable from an established fact, and correctable. `"ambient"` commits like `"fact"` but is excluded from the dead-weight curation rule (long bodies over 500 words attach a non-blocking `ambient_body_long` warning).
- `groundedness_check: bool = False`. Opt-in. When True and `source_transcript` is provided, the server walks the proposed body sentence by sentence and flags any whose content tokens overlap the transcript by less than 30%.
- `source_transcript: str | None = None`. The conversation turns that motivated this write. Required for the gate to fire.
- `claims: list[str] | None = None`. Structured claims the body makes about the current repo, in the four-shape syntax under [Structured claims](#structured-claims): `path`, `path::symbol`, `path::NAME=literal`, `!path`. Each claim is checked against the origin worktree at declaration and the write is refused, with a raised error naming each failing claim and what the tree says, when any claim is false. There is deliberately no `acknowledge_*` escape: the read side's trust in claims rests on every stored claim having been true at declaration. Declaring requires a worktree; the canonical rendered forms are what gets persisted, deduplicated, capped at 64 entries.
- `supersedes: list[str] | None = None`. Ids of active memories this write replaces. Each becomes a `supersedes` link on the new record (note `declared at write time`), which `memory_search` renders as `superseded_by` on the stale hit. Refused with a raised error when an id is not a ULID or names a memory that is not active; at most 16, deduplicated.

Write-time supersession (`[behavior] write_supersession = true`). On every commit the body is compared against the active set by `bettermemory.supersession.detect_supersession`, lexically and deterministically, and only when both bodies are claim-sized (at most 80 content tokens and 5 sentences). A stored memory about the same subject that the new body diverges from on a value (a number, a joined compound, a proper noun, or the token after a change cue) in the same slot gets a `supersedes` link when the new body carries a change cue (moved, switched, renamed, raised, no longer, the previous, again, and kin), with a note naming the cue and both values. The same divergence with no cue is filed for `memory_admin(action="conflicts")` rather than guessed at. At most five links and five filings per write; a target named in `supersedes` is excluded from detection. The rule was measured on the integrity benchmark's sealed corpus before it shipped (`docs/eval-results.md`); a false fact with a change cue can earn the link over the true fact, which `SECURITY.md` records. `memory_update(id, links=[])` on the new record clears a wrong link; the event log carries every link the detector set under `supersedes_detected`.

Result statuses:

- `"committed"`: write succeeded; payload includes the new id and `related` medium-overlap matches. When the write set links it also carries `supersedes`, one row per target (`{id, evidence: "declared"}` for a declared target, `{id, summary, evidence: "kin" | "context", cue, new_value, old_value}` for a detected one), and when it filed pairs, `conflicts_filed` (`{pair_id, id, summary, evidence, new_value, old_value}`) with a `hint` naming the conflicts action as the remedy. All three keys are absent when nothing was set.
- `"transient_warning"`: durability gate fired; `markers` listed.
- `"credential_warning"`: the body contains a secret-shaped token (vendor-prefixed API key, private-key PEM, JWT, or a guarded `password=…`-style assignment). `markers: [{kind, snippet}, ...]` returned with the secret span redacted from each snippet; nothing is persisted. Describe the secret instead of embedding it, or pass `acknowledge_credential=True`. Runs first, ahead of the durability gate.
- `"duplicate"`: content dedup fired; `matches` listed. The rejection also credits the top match a corroboration (recurrence is evidence, not waste): `corroboration_recorded: true` plus `corroborations`, the new persisted total, when the credit landed; `corroboration_recorded: false` when this session already credited that memory. Both keys are absent when the bump itself failed; the credit is best-effort and never turns a clean `"duplicate"` answer into an error. The right response is still `memory_update` on the matched id.
- `"previously_removed"`: tombstone dedup fired; `removed_matches` listed with their original `removed_reason`. Either drop the write or restore the tombstone through `memory_admin(action="restore")`.
- `"scope_mismatch"`: body cites a known `projects:<name>` scope's name (or a path under another project's tree) and that scope is not declared. `suggested_scopes` and `matches` returned.
- `"user_claim_warning"`: the body reads as a claim about the user (an "I prefer…" / "the user always…" / "`<Name>` prefers…" shape) while `category` is `"fact"` or `"ambient"`. `markers: [{phrase, sentence}, ...]` returned; nothing is persisted. The remedy is to re-issue with `category="user-inference"`. In the chain it sits after the credential and durability gates and ahead of both dedup gates.
- `"ungrounded"`: groundedness gate fired. `claims: [{sentence, overlap_ratio}, ...]` returned. No commit.

### `memory_update(id, content?, scopes?, confidence?, category?, links?, acknowledge_credential?, acknowledge_transient?, acknowledge_user_claim?, acknowledge_truncation?)`

At least one of `content`, `scopes`, `confidence`, `category`, `links` must be provided. `scopes` and `links` have REPLACE semantics (pass the full new list; `[]` clears).

Four body gates fire here, all only when `content` is provided, in the write path's order (credential first, so a secret is refused before any other gate records body-derived data in the event log):

- `{status: "credential_warning", markers: [...]}`: a body edit that introduces a secret-shaped token persists nothing. `acknowledge_credential=True` overrides; logged by `kind` only.
- `{status: "transient_warning", markers: [...]}`: the new body reads as transient state. `acknowledge_transient=True` overrides.
- `{status: "user_claim_warning", markers: [{phrase, sentence}, ...], hint}`: the edited body reads as a claim about the user while the record's post-edit category is `fact` / `ambient`. Judged against the category the record will have after the edit, so it does not fire when the record is already `user-inference`. `acknowledge_user_claim=True` overrides, for the case where the subject is someone or something other than the user; the detector's `we (?:use|prefer|avoid|always|never)` branch also fires on ordinary project prose ("We use ruff for linting in this repo."), which is the body this override exists to let through. For a genuine claim about the user the remedy is `memory_write(..., category="user-inference")`.
- `{status: "truncation_warning", previous_length, new_length, ends_with, hint}`: the edit makes the body shorter **and** leaves it ending on a character that is not sentence- or structure-terminal, which is what a body truncated in transit looks like. Nothing is persisted. `acknowledge_truncation=True` overrides. The shrink conjunct is load-bearing: the predicate alone would refuse every edit to a body legitimately ending on a bare identifier or list item, including edits that only grew it. `ends_with` is the last 60 characters of the rejected body. The event log records the two lengths and never the body text.

`category` accepts `"fact"` and `"ambient"`; `"user-inference"` is rejected, so a claim about the user is filed through `memory_write` rather than relabelled in place.

Preserves `id`, `created`, `source`. Bumps `updated`. Content changes reset `last_verified_at` to `null` and clear the `verified_paths` / `verified_commits` / `verified_versions` / `verified_absent_paths` attestation lists, `claims` and `verified_head`: the old attestation was for prose that no longer exists, and a claim declares what the old body asserted. Scope, confidence, category and links edits preserve verification. Re-declare claims for the new body via `memory_verify(id, claims=[...])`.

Optimistic-concurrency CAS. The handler snapshots the record's `updated` via `memory_show` (or an equivalent prior read) and the store refuses the write when another writer landed an update in between. Returns `{"status": "stale", "memory_id", "current_updated", "hint"}`; `current_updated` is the stored `updated` at the moment the CAS failed. No partial write. Re-fetch with `memory_show` and retry the edit on top of the current record; do not auto-retry from the same caller stack, the conflict may need reconciliation. Distinct from the not-found and tombstoned `ValueError` paths, which surface as raised exceptions.

### Inter-memory links

`links: list[MemoryLink]` is persisted on the record. Each `MemoryLink` is `{type, target_id, note?}`:

- `type`: one of `"supersedes"`, `"contradicts"`, `"extends"`, `"depends_on"`.
- `target_id`: a valid ULID, the other memory.
- `note`: optional free-form string.

Self-links are rejected. `memory_show` surfaces forward `links` on the source and `reverse_links` on the target. A link entry with an unknown type or invalid `target_id` is dropped on load, that entry only.

Two producers set `supersedes` links without a `memory_update`: the `supersedes` parameter of `memory_write` (note `declared at write time`) and write-time supersession detection (note `set at write time: ...`), both described under `memory_write`. The `contradicts` link's producer is the conflicts verdict under `memory_admin`.

## Lifecycle

### `memory_remove(id, reason)`

- `id: str`. Required.
- `reason: str`. Required, non-empty. Captured into the tombstone's `removed_reason` and surfaced by future `memory_write` calls whose new body overlaps.

Returns `{removed: id}`. Not found and already tombstoned surface as `ValueError` at the MCP boundary.

### `memory_admin(action="restore", id)`

- `id: str`. Must reference a tombstone.

Strips removal metadata; preserves `created` and `updated`. The trust fields are re-checked on the way back, the way `memory_verify` checks a stored record before it re-stamps one: a stored claim the origin worktree now contradicts, or an attested `verified_paths` entry that no longer exists here, is dropped, and `last_verified_at` is cleared whenever anything was (the stamp asserted the whole record, and the record that comes back is not the one that was verified). Scoping matches the verify handler's: claims and relative attestations are judged only against a live origin worktree, absolute attestations always. A restore that strips something carries `trust_stripped: {claims, verified_paths, verification_cleared, verified_head_dropped}` and a `hint`; the `restore` event carries `claims_dropped`, `attestations_dropped` and `verification_cleared`. The stamp's commit anchor is re-checked the same way: when the origin tree is live and its HEAD no longer descends from `verified_head`, the anchor is dropped while the stamp stays. `bettermemory tombstones restore` runs the same check. A restore that strips nothing preserves `last_verified_at`.

A restore stamps the record's provenance `local` and records a `restore` event, which makes remove-then-restore the way to re-admit a memory that reads `unaccounted`. Failure is always a `ValueError` at the MCP boundary: `"memory <id> is active; nothing to restore"` or `"no tombstone with id <id>"`. Either the active record exists with the full restored metadata, or the tombstone is untouched; re-fetch with `memory_show` to learn the end state.

### `memory_admin(action="tombstones", scopes?)`

- `scopes: list[str] | None = None`. Filter to tombstones carrying at least one of these scopes.

Returns `{tombstones: [...]}`, newest removal first; each row is `{id, scopes, confidence, category, summary, created, updated, last_verified_at, removed, removed_reason, removed_session}`. Tombstones in a scope the session has disabled are left out.

### `bettermemory rollback --by-actor <client> [--since <ISO_TS>] [--apply --yes] [--json]`

CLI-only; no MCP surface, because a targeted destructive rollback driven by an in-session model is a different risk posture. It removes the memories one actor wrote and leaves every other actor's in place. Each removal is an ordinary tombstone, so `bettermemory tombstones restore <ID>` or `memory_admin(action="restore")` puts a record back, with its `actor` intact.

- `--by-actor CLIENT` is **required**. Exact, case-sensitive, read through `identity.actor_matches`, the same rule the `client` filter on `memory_search` selects with. There is no default: a rollback with no selector would mean the whole store.
- `--since ISO_TS` filters `created`, never `updated`, because a record's `actor` and its `created` are stamped by the same write. Requires an explicit UTC offset or trailing `Z`. A far-future value only warns, because it selects nothing.
- `--apply` alone does **not** commit: it requires `--yes` as well and otherwise exits non-zero.

It selects authorship, not influence: the `actor` is stamped at write time and never restamped, so a record this client wrote and another later rewrote is removed, while one it only edited is not. A record that declared no client is never selected: an absent actor is not evidence of some other writer. The report prints the declined count as a first-class line, and its four populations partition the active set exactly (`selected + declined_undeclared + other_actor + out_of_window == total_active`), asserted on every run.

## Verification

### `memory_verify(id, note?, verified_paths?, verified_commits?, verified_versions?, verified_absent_paths?, claims?)`

- `id: str`. Required.
- `note: str | None = None`. Free-form; recorded in the event log.
- `verified_paths: list[str] | None = None`. The filesystem paths spot-checked.
- `verified_commits: list[str] | None = None`. The commit hashes spot-checked.
- `verified_versions: list[str] | None = None`. The version strings spot-checked.
- `verified_absent_paths: list[str] | None = None`. The mirror attestation: body-cited paths confirmed *intentionally* absent on this machine (a remote host's path, a platform-conditional location, a path the body cites because it is not the real one). Path drift reports them under `path_drift.expected_absent` instead of `missing`. Not for paths that merely went missing; that is real drift, fix the body.
- `claims: list[str] | None = None`. Same syntax and same declare-time oracle as `memory_write`'s `claims`; the check runs against the memory's recorded origin worktree, falling back to the caller's checkout for a record whose origin carries a repo but no `worktree_root` when the two resolve to the same repository. REPLACE semantics like the other four lists. Two behaviours with no counterpart on the other lists: a verify that does not pass `claims`, on a memory that stores them, re-runs the oracle over the stored claims first and refuses the stamp when any has gone false. The stored re-check is skipped when the origin worktree is not visible from this machine, never when the tree is present and disagrees. `claims=[]` is the explicit clear-and-stamp escape: audited, and it drops the memory back to incumbent-governed drift.

A verify that attests nothing on a memory whose cited paths resolve is refused. Evidence is what the call attests (`verified_paths`, `verified_absent_paths`, `claims`) or what a `None` preserves from the record; an explicit `[]` on every list is a clear, not evidence. "Resolve" means an absolute citation that exists on this machine or a relative one anchored in the memory's live origin worktree, so a preference with no path and a body whose cited files are gone keep the no-arg re-verify. The refusal names the resolved citations as the list to attest.

The stamp records the commit the memory's origin checkout stands at, as `verified_head` (the response carries it; `null` when no live checkout answers). It is the anchor the commit-drift leg counts from in reachability space, the commits in `verified_head..HEAD`, which hold a branch authored before the stamp and merged after it. Written whole on every stamp and cleared with `last_verified_at` on a body edit.

Bumps `last_verified_at` without touching `updated`. Idempotent. The attestation lists are persisted on the record. The path-drift detector uses `verified_paths` to mark previously attested paths that still exist as `verified`, downgrading the verdict. The commit-drift signal is claim-anchored: its count is narrowed to commits touching the memory's anchors (`verified_paths` plus paths the body cites), and a memory with no anchors in the caller's repo is exempt entirely (`commit_drift` reads `null`; the calendar window stays its backstop). **Attest paths whenever the memory cites any**, because `verified_paths` is the only one of the three lists the read path resolves; `verified_commits` and `verified_versions` are persisted and echoed back, provenance for the next reader rather than a signal. Calling with `verified_paths=None` preserves any prior attestation; an explicit `[]` clears it (same semantics for all four lists).

Optimistic-concurrency CAS. The fingerprint is `last_verified_at` (not `updated`; verify is orthogonal to content edits). Returns the same shape as `memory_update`'s stale response. No partial write: the prior verifier's `verified_*` lists are intact. Re-fetch, reassess your attestation against the now-current lists, and retry.

### Structured claims

One string per claim, four shapes:

- `src/pkg/mod.py`: a **path** claim. The file exists.
- `src/pkg/mod.py::name`: a **symbol** claim. `name` is a top-level `def`/`class` in that module (`ast.Module.body`; a method or nested def is not top-level; claim the enclosing class or the path).
- `src/pkg/mod.py::NAME=literal`: a **literal** claim. Module-level constant `NAME` is assigned that literal. Values compare in `repr` space after `ast.literal_eval` (`30` differs from `30.0` and from `'30'`); quote strings. Plain `ast.Assign` only.
- `!src/pkg/mod.py`: an **absence** claim. Nothing exists at that path. Declaration refuses while anything occupies it, and the drift polarity inverts: reappearance is the drift, and a verify on a memory whose absence claim now fails refuses the stamp like any other false stored claim. Path-only: `!path::x` is refused.

Paths are stored worktree-relative with forward slashes and must resolve inside the origin worktree (no `..`, no absolute escapes), for both polarities. Declaration is verification: `memory_write` / `memory_verify` run the oracle (`claims.check_claim`) and refuse a claim that is false right now.

What declaring buys: the commit-drift leg narrows. For a file named by at least one claim, commits escalate the staleness verdict only when the claim-level `weak` tier implicates a claimed binding: the changed lines are matched at column 0 against the claimed `def`/`class`/assignment (plus content anchors for literals), so method-body churn elsewhere in the file stops nagging. Files the body cites but never claims keep the any-touch rule, and so does a file carrying a literal claim whose value is a container (dict/list/set/tuple), whose source may span physical lines. On the 30-repository corpus (`bench/rot/results/multirepo-anchored-2026-07-30.json`) the weak tier costs **1.1 alerts per genuine catch at 94% precision** against the per-file incumbent's **3.4**. The detector (`claims.build_binding_index` / `claim_level_drift`) is the bench's own, promoted to the product.

Body edits clear claims (with `last_verified_at`); `memory_verify(id, claims=[...])` re-declares. A claim on a file git never tracked reads as phantom: commit drift not applicable.

## Curation

### `memory_record_use(memory_ids, outcome, note?, claim_excerpts?)`

- `memory_ids: list[str]`. Always plural, even for one memory.
- `outcome: str`. One of `"applied"`, `"ignored"`, `"contradicted"`, `"corrected"`. `"corrected"` is the audit-only sibling of `"contradicted"` for the noticed-and-fixed-inline workflow; it never raises the contradiction flag. No outcome changes ranking.
- `note: str | None = None`.
- `claim_excerpts: list[str | None] | None = None`. Parallel to `memory_ids`, one entry per id (max 500 chars), or `None` for "no specific claim". Recorded in the event log so an audit can trace any response back to the specific claim. Empty strings are rejected (pass `None` instead).

Returns `{recorded: [<memory_id>...], outcome}`, where `recorded` echoes the ids whose pending tokens were settled by this call, plus `claim_excerpts` when they were supplied.

Auto-settlement: every `memory_search` hit and `memory_show` response carries an opaque `use_token`. Unless `memory_record_use` is called, the retrieval settles as `outcome="applied"` automatically, normally at turn end by the Stop hook, otherwise by the in-process fallback on a later tool call once the token is both at least two handler entries old and past a wall-clock floor (`AUTO_COMMIT_MIN_AGE_SECONDS`, 600s, mirroring the hook's attribution window). Explicit calls win: the server purges the pending token before recording and writes `attribution="model"`.

Hook settlement: at turn end the Stop hook (`bettermemory hook stop`, which posts the turn to the daemon) matches the assistant's reply text against recently retrieved memory bodies, verbatim (case- and whitespace-normalised) or by distinctive-token containment. Matches emit `applied` with `attribution="hook"`, `auto=false` and the matched sentence as the `claim_excerpt`; the retrieved-but-unmatched remainder emits the plain `applied` with `auto=true, attribution="auto"` in the same pass. Both shapes carry `client_model` when known. The in-process pass reads the event log and purges any already-settled token, so each retrieval generates exactly one `applied` event. One caveat about the manual path only: `bettermemory hook stop --session-id <made-up>` breaks that invariant, because the settlement dedup spans the retrieval session and the supplied id. Pass `--dry-run` whenever you invoke the hook by hand to inspect a store.

### `memory_admin(action="health")`

Returns the aggregate rollup with the defaults `bettermemory health` also uses (a 30-day window, the top 10 heavily used memories, `behavior.heavily_used_min_applied` as the applied floor; the CLI's `--days`, `--top-k` and `--min-applied` change them, the tool does not): `generated_at`, `window_days`, `total_active_memories`, `total_events`, `distinct_sessions`, `dead_weight`, `cold_memories`, `heavily_used` (with per-row `applied=N (auto=X exp=Y)` split), `contradicted` (each row carries a `resolution_timeline`), `marker_stats`, `scope_distribution`, `scope_health`, `rare_scopes`, `actor_slices`, `orphan_use_events`, `verification_debt` (totals split by checkability: `never_verified_checkable` / `stale_checkable` count the rows carrying declared claims or drift anchors, the debt a verify pass can mechanically drain), `commit_drift_debt` (null when the server is not in a repo whose memories live in this store), `cross_repo_drift` (commit drift for memories anchored in repos the caller is not standing in: groups by recorded origin and `worktree_root`, re-identifies each directory as a checkout of the recorded repo before trusting it, runs the same claim-anchored legs there read-only, caps the roots walked per run with over-cap and unresolvable groups listed under `skipped`, and counts origin-less records under `unanchorable`), `provenance`, `silent_misses`, `recent_silent_misses` (the newest-first list of miss candidates, each `{event_id, top_hit_id, …}`; feed an `event_id` to the acknowledge_miss action), `cold_endorsement_memories`, `recommendations`, `telemetry_coverage` and `episode_volume`.

`telemetry_coverage` is the honesty gate on `dead_weight` and `cold_endorsement_memories`: `{hook_telemetry_events, covered, dead_weight_suppressed, cold_endorsement_suppressed, reason}`. Coverage means the event log carries Stop-hook settlement telemetry: a `use` event carrying `attribution="hook"` or stamped `triggered_from="stop_hook"`, or a `turn_audited` stamped `triggered_from="stop_hook"`. When `dead_weight_suppressed` is true the `dead_weight` bucket is empty by construction rather than because the store is clean: with nothing in a position to record an apply, `applied_count == 0` cannot tell an unhelpful memory apart from an unwired hook. `cold_endorsement_suppressed` makes the same statement about its bucket, and the two legs gate together. Wire the Stop hook and re-check.

`episode_volume` is the journal's size gauge: `{sessions, episodes, bytes, prunable_sessions, ttl_days}`, the aggregate only. Episode pruning is write-triggered: it runs on `episode(action="write")` and on `bettermemory episodes prune`, and nowhere else, so a read-only loop never collects. `prunable_sessions` is the actionable field; `bettermemory episodes prune --dry-run` lists them by name from the same predicate.

`provenance` is `{counts, unaccounted_total, unaccounted}`: `counts` is the per-label census over every active row (`local`, `imported`, `synced`, `unaccounted`), `unaccounted_total` is the one count that is a finding, and `unaccounted` lists those rows newest first (`{id, scopes, summary, created}`, capped at 20). It is `null` when this machine does not hold the store's key. The remedy is deliberate: nothing relabels a record. A memory you recognise is re-admitted through the store (`memory_remove`, then restore, which stamps `local` and records the restore); the rest are removed. `memory_verify` is not the accept path, because a verify vouches for the body's truth, not for how the row arrived.

`actor_slices` is the per-actor pivot over the caller identity stamped on every record and event: `{declared: [...], undeclared: {...}}`, where every entry is `{client, memories, memories_in_window, events, searches, applies, models, principals}`. Two counts reconcile exactly, because a memory carries exactly one actor: `sum(declared.memories) + undeclared.memories == total_active_memories` and `sum(declared.events) + undeclared.events == total_events`. `undeclared` is always present, including at zero: on any store with history it is the majority.

`cold_endorsement_memories` is the per-memory count of distinct memories with `retrieval_count >= N` and zero explicit applies: the ranker keeps surfacing a memory but the model never deliberately calls `memory_record_use(applied)` on it.

`recommendations: list[Recommendation]` distils the buckets into one-line actions. Each entry is `{kind, summary, action, count, memory_ids, scope}`, where `kind` is one of `"remove_dead_weight" | "resolve_contradicted" | "cleanup_cold_endorsements" | "verify_drifted" | "review_unaccounted" | "fix_typo_scopes"`, `memory_ids` is capped at 10 entries (the uncapped `count` reports the true size), and `scope` is populated only on scope-level recommendations. Size-driven kinds require at least 3 rows in the underlying bucket; `resolve_contradicted`, `review_unaccounted` and `fix_typo_scopes` surface from a single row.

The `silent_misses` rollup carries `{audited_total, miss_total, unique_miss_memories, no_signal_total}`. `audited_total` counts miss-capable audits only; `turn_audited` events whose verdict is `no_signal` are reported separately as `no_signal_total`, so a probe that structurally cannot measure a miss does not read as a healthy 0% miss rate. `miss_total` is the event count; `unique_miss_memories` the cardinality of the set of top-hit memory ids on those events. Misses whose top-hit memory has been tombstoned are dropped from both. The rollup honours a `silent_miss_cutoff` event when present, written by the acknowledge_miss action's `before` form.

`dead_weight` and `cold_memories` measure different failure modes: dead weight is "retrieved but did not help", cold is "the ranker is not surfacing this at all".

### `memory_admin(action="conflicts", scan?, id?, verdict?, note?)`

List and arbitrate memory-versus-memory contradiction candidates flagged by the corpus scan (near-identical pairs with a negation flip or a numeric divergence) or filed by `memory_write` (a claim-sized body that diverges on a value from a stored claim about the same subject with no change cue to say which is current, detector `value`, or `numeric` when both values are numbers). Detection is mechanical; the verdict is the model's. Modes are mutually exclusive:

- No arguments: list the pending candidates, strongest similarity first, both bodies inlined, at most 10.
- `scan: bool = False`: run detection over the active set now and merge new candidates into the queue; returns the merge counters under `scan` (`{added, resurrected, refreshed, dropped, gc_deferred, pending_rows_on_disk}`) plus the pending list. A scan is also the queue's only garbage collector for rows whose member has since been removed, and it collects only from a snapshot that accounts for every active memory; `gc_deferred` is `1` on a pass that could not prove that, and the rows survive untouched for the next pass. `pending_rows_on_disk` counts the `pending` rows the merge left in the queue, a different question from the top-level `pending_total`, which counts judgeable work.
- `id: str` + `verdict: str`: rule on one candidate. `verdict="contradiction"` (the pair genuinely disagrees) writes a `contradicts` link a→b (pass `note` with the why) and marks the candidate confirmed; follow up with `memory_verify` on the correct side and `memory_update` / `memory_remove` on the wrong one. Both members must still be active. `verdict="compatible"` (detector misfire) clears any standing `contradicts` link between the pair in either direction, then dismisses it; the dismissal is sticky unless either member's body later changes, which re-queues it under the same id. Either verdict records one `conflict_verdict` event.

Returns `{pending, pending_total}`, each pending row `{id, a, b, similarity, method, detector}` with `a` / `b` as `{id, body, scopes, updated}`, plus the `scan` counters in scan mode and a `resolved` echo (`status` of `"confirmed"` / `"dismissed"` / `"already_resolved"`, with `link_written` on a contradiction verdict and `links_cleared` on a compatible one) in resolve mode. `pending_total` counts the candidates that can be ruled on right now; a queued row whose member was removed since the last scan appears in neither, and a `hint` reports how many were left out.

What the detector sees, and what it does not. It looks only at pairs whose bodies already score above the dedup similarity threshold (0.75 Jaccard). A contradiction phrased differently enough to score below that is invisible here. Above the threshold, two signals fire: **negation polarity** (one body carries a negator and the other does not, or each body asserts a term the other negates within its own clause, checked in both directions) and **numeric divergence** (both sides carry number-bearing tokens the other lacks; a one-sided number is added detail). Neither reads meaning: an antonym contradiction with no negator, an argument swap, and anything the similarity gate never compared all walk past. Treat the queue as evidence, not as an inventory of every contradiction in the store.

### `memory_admin(action="acknowledge_miss", id, reason)` and `memory_admin(action="acknowledge_miss", before, reason?)`

- `id: str`. The per-event ULID stamped on a `search_miss` event, surfaced in the health report's `recent_silent_misses` list. `reason: str`, required, at least 8 characters: why this flagged miss is a false positive.
- `before: str`. An ISO-8601 timestamp with an explicit offset or trailing `Z`, at most a day in the future. Acknowledges every miss earlier than it at once by writing one `silent_miss_cutoff` event; the health and eval rollups honour the latest cutoff and drop `turn_audited` and `search_miss` events stamped earlier than it. Nothing is removed from the log. Refused when telemetry is off, because the cutoff is itself a telemetry event. This form replaces the `consolidate --acknowledge-misses-before` cutoff 8.x carried.

The per-event form emits a `miss_ack` event so the miss drops out of the actionable counters; it returns `{status: "acknowledged", event_id, reason}` and is idempotent. An id that names no `search_miss` returns `{status: "not_found"}` or `{status: "wrong_kind", kind}` with a hint. The bulk form returns `{status: "cutoff_recorded", cutoff_ts, reason}`.

### `memory_admin(action="rename_scope", old_scope, new_scope)`

Renames the scope on every active memory and every tombstone carrying it. Returns `{old_scope, new_scope, active: [ids], tombstoned: [ids], failed: [...]}`: the normalised scopes echoed back plus the ids of the records touched. `bettermemory rename-scope OLD NEW` is the CLI form, with `--no-tombstones` to leave tombstones alone.

## Session-local

### `memory_admin(action="disable_scope", scope)` and `memory_admin(action="enable_scope", scope)`

- `scope: str`. Singular.

Returns `{disabled_scopes: [...]}`, the session's full disabled set after the change. Resets when the server process restarts. Disabled scopes are filtered from `memory_search`, from the tombstones listing and from `episode(action="handoff")`.

## Identity (who is calling, and from where)

Every request resolves one caller, `bettermemory.identity.Caller`, before its handler runs, and three things read it: the write path stamps it on the record, the event recorder stamps it on every event, and the session registry keys per-client state on it. Two blocks, never merged:

- `actor {client, client_version, model, principal, session, sources}`. `principal` is **attested**: it comes only from the SDK's `authenticated_principal`, the `(client, issuer, subject)` triple of a verified OAuth token, and is `null` on every unauthenticated transport, stdio included. Everything else is **declared** and forgeable; `sources` maps each declared field that is set to the channel that supplied it. A declared value can never populate `principal`.
- `workspace {path, source}`. The directory `origin.capture` then describes (`cwd`, `repo`, `branch`, `worktree_root`), and the channel that named it.

Channels, in precedence order, each recording its own `source`:

| `source` | Channel | Fills |
| --- | --- | --- |
| `principal` | the SDK's attested OAuth principal (HTTP with a token verifier) | `actor.principal` |
| `header` | `x-bettermemory-client`, `x-bettermemory-client-version`, `x-bettermemory-model`, `x-bettermemory-workspace` request headers (HTTP only; client input) | the named field |
| `env` | `BETTERMEMORY_CLIENT`, `BETTERMEMORY_CLIENT_VERSION`, `BETTERMEMORY_MODEL`, `BETTERMEMORY_WORKSPACE` on the server process, the only out-of-band channel a stdio server has; `bettermemory init --client X` writes `BETTERMEMORY_CLIENT` into that client's config block | the named field |
| `client-info` | the `initialize` handshake's `clientInfo` (optional under the protocol; its absence is ordinary) | `actor.client`, `actor.client_version` |
| `transport` | the transport's own session id (`mcp-session-id` under streamable HTTP; none on stdio) | `actor.session` |
| `roots` | the first `file://` root a roots-capable client offers on `roots/list`, asked once per connection before the first tool call | `workspace` |
| `process-cwd` | the server process's working directory, the labelled fallback, never a silent default | `workspace` |
| `transcript` | the Claude Code hooks, which read a transcript rather than the wire: the transcript id is the session, the transcript's model is the model | `actor` on hook events |

Declared values are bounded client input: whitespace-trimmed, no control characters, at most 256 characters; anything else is dropped, not stored.

The `actor` block is stored only when something is set, and `origin.source` is always stored. The read surfaces (`memory_show`, the `committed` response) render the full actor shape, nulls included, and label the origin's source. Events carry `actor` when it is non-empty.

The session registry (disabled scopes, use tokens) keys on what can differ between two requests reaching one process: the attested principal, the transport session, and header-declared `client` / `model`, in that composite. Per-process channels (`env`, `client-info`) never key it, so a stdio client stays in the default bucket.

Reading it back: `memory_search` takes `client` / `model` filters over the two declared fields, as a `WHERE` on the candidate query. The rule lives in one function, `identity.actor_matches`, which both the ranked set and the BM25 corpus-IDF denominator read, so term rarity is never priced against memories the caller cannot see.

Identity is **evidence** (for attribution, filtering, per-model telemetry and targeted rollback) and never a permission boundary. No RBAC, no per-agent auth, no tenant isolation: `memory_show(id)` stays unrestricted, and the filters above select on a value the client declares about itself. `SECURITY.md` records what that means for a client that lies.

## Episodes (the journal beside memory)

Episodes are not memories. They are rows of their own table, their content is excluded from `memory_search` and the health buckets, and they have no durability gate. The one thing that crosses the tier boundary is aggregate size: the health report's `episode_volume`. Use them for loop-iteration takeaways, "what we tried", and any content `memory_write` would (correctly) reject as transient. A 30-day TTL on sessions runs on each write so the journal stays bounded.

**Episodes are the state channel, and a memory_write is how state becomes fact.** While a run is in flight, working state goes to `episode(action="write")`; at session close, a takeaway that hardened into something worth keeping is written with `memory_write` by the model that read it back through the handoff. The rule exists because the alternative is what happens without it: `memory_write`'s durability gate rejects state-shaped content, but rejecting it does not remove the need to write down "where I am in this run", so the state gets rephrased until it passes the gate and lands in the durable store dressed as a fact. A takeaway that survives to the end of its session has already been tested by the rest of that session, which is most of what the durability gate approximates from a single sentence. Whatever did not harden ages out on the TTL.

The optional `swarm_id` on a write is the multi-agent fan-in label: a coordinator fans out parallel sub-agents, each tags its episodes with the coordinator's session id, and `bettermemory episodes list` gathers them. It is a cross-cutting cohort label, orthogonal to the single-chain predecessor link the handoff resolves.

### `episode(action="write", body, takeaway?, scopes?, swarm_id?)`

Append a new episode for the current session.

- `body: str`. Required, non-empty. Free-form markdown. Capped by `[behavior] max_content_bytes` (default 1 MB).
- `takeaway: str | None`. One-sentence summary. Surfaced preferentially at handoff; falls back to the first body line when absent. Capped by `max_takeaway_bytes` (default 4 KB).
- `scopes: list[str] | None`. Empty list is valid; episodes are keyed by `session_id`, scopes are tags for filtering.
- `swarm_id: str | None = None`. The cohort id described above. The episode still lives under this writer's own session. Validated (charset and length) by the `Episode` model.

Returns `{status: "committed", id, session_id, created, scopes, takeaway, swarm_id, pruned_sessions: [<sid>...]}`. `session_id` is captured from the recorder; `origin` is captured the same way as `memory_write`. `pruned_sessions` lists the sessions that hit the TTL on this write.

### `episode(action="handoff", prior_session_id?, max_episodes?, include_bodies?)`

Read recent takeaways from a prior session. Designed as the first tool call at a loop iteration's entry.

- `prior_session_id: str | None`. When omitted, the handler resolves the most recent session in the event log whose id differs from the current recorder's. Pass explicitly when the caller already knows the parent session.
- `max_episodes: int | None`. Default `5`, cap `50`.
- `include_bodies: bool = False`. When `True`, every row whose `provenance` is not `unaccounted` also carries `body`. The takeaway is what the next iteration needs first; a body arrives only when a takeaway asks for its detail.

Auto-resolution applies two implicit filters when `prior_session_id` is omitted:

- **Caller-worktree strict equality.** A candidate session is only adopted when at least one of its episodes carries an `origin.worktree_root` equal to the caller's captured worktree, or (the zero-episode branch) when both are `None`. Two worktrees of the same repo never see each other's prior sessions.
- **Disabled-scopes cascade.** A candidate whose only visible takeaways are hidden by the session's disabled scopes does not count as a takeaway to adopt, so the walk rewinds past it toward an older visible takeaway. When the walk exhausts with no visible takeaway anywhere, the hidden immediately prior session is surfaced as `prior_session_id` with `episodes: []` and the scope-hide `note`.

Returns `{prior_session_id: str | None, episodes: [{id, created, takeaway, scopes, provenance, body?}, ...], note?: str}`. `prior_session_id is None` and `episodes == []` is the no-baseline case. When `prior_session_id != None`, branch on the optional `note` key, not on `episodes` alone: no `note` means the resolved session's recent takeaways are in `episodes`; a `note` means the immediately prior worktree session left nothing visible, and its text names the cause (**floor-only**: it called the handoff but no write followed; **zero-episode**: recorded activity but journaled nothing; **all-scope-hidden**: it journaled takeaways every one of which is in a scope this session disabled). `episodes` may be non-empty alongside a `note`: the walk rewinds past the empty or hidden session to an older real takeaway in this worktree and surfaces that.

**Provenance and the body rule.** Every row carries `provenance`, read from the store's log at each call: `local` when the row's pointer into the log verifies, `unaccounted` when it does not (the planted shape), `imported` for a row the migration brought in. `body` is present only when `include_bodies=True` and the label is not `unaccounted`; an unaccounted episode never carries a body on this surface, whatever the caller passed, and its takeaway and scopes stay with the label beside them. An explicit `prior_session_id` the event log never saw yields rows that all read `unaccounted`.

## The store

One SQLite file per store, `memory.sqlite` in the store directory (`src/bettermemory/store.py`). The directory resolves to `$BETTERMEMORY_DIR` if set, else `./.claude-memory/` if it exists, else `~/.claude-memory/`. The tables (memories, tombstones, episodes, conflicts, verifications, quarantine, imports) are the fold of the hash-chained `log` table (`src/bettermemory/log.py`): every mutation is a log row first and a table row second, in one transaction, and every telemetry event is a log row too. Each row carries an HMAC-SHA256 over its fields and the MAC of the row before it; the key is 32 random bytes kept outside the store, under the user config directory (`keys/<store_id>.key`, via `platformdirs.user_config_dir("bettermemory")`, or the directory `BETTERMEMORY_KEYS_DIR` names when it is set), and the store holds only the key's fingerprint. A head checkpoint beside the key (`<store_id>.head`) records the last row. The FTS5 table and its triggers are the v8 index's own, so the candidate query returns the same ids in the same order on the same rows.

Every memories, tombstones and episodes row carries `log_mac`, the MAC of the log row that produced it. The read surfaces verify that pointer per read: the log row exists, it is of the kind that produces such a row, its payload names the id, and its MAC verifies under the segment's key. A row that passes reads `local` (or `imported` / `synced` by its recorded label); a row that fails reads `unaccounted` on the next search or show without a refold. A row inserted around the store, and a log row appended without the key, both fail the same way. A store opened on a machine that does not hold its key cannot verify pointers and reports `trust_unavailable` on every row.

### `bettermemory log verify [--json]`

Walks the chain and reports one of three statuses. `tampered`: a row fails its MAC, the chain or the seq sequence breaks, the head names a row the log no longer holds, or a table differs from a replay of the log (a row nobody logged is listed as `unaccounted`, a logged row that is gone as `missing`). `unverifiable`: a segment of the chain was signed by a key this machine does not hold, or there is no head checkpoint. `ok` otherwise. The command never writes: a store opened for verification without its key is reported, not rekeyed. Exit status 0 on `ok`, 1 otherwise, 2 when the store directory holds no `memory.sqlite`. `--json` prints the full report (segments, problems by seq, the head, the fold per table).

### `bettermemory migrate v8 [--from DIR] [--to FILE] [--dry-run] [--json]`

Reads an 8.x store directory with the frozen v8 readers (`src/bettermemory/v8.py`) and writes its records into the store (`src/bettermemory/migrate_v8.py`). `DIR` defaults to the resolved store directory and `FILE` to `memory.sqlite` inside it; the store is created when absent and opened when present. Active memories become `imported` rows with their v8 filenames, inserted in the order a v8 rebuild indexed them, so the candidate query's tie order is the index's. Tombstones keep the links and corroboration rollup their files kept, and the active filename their name was made from. Episodes, conflicts and the ingest watermark take their tables. Every event, from every shard, archive and the legacy file, becomes a telemetry row under its original timestamp and session, its fields the payload plus `imported_from: "v8"`, with verbatim query text redacted the way every 9.x row is. The run opens with one `migrate_v8` control row naming the directory and the counts, and everything lands in one transaction: a failure part way leaves the store as it was.

The directory is never written. A re-run imports what is new and reports the rest as present: records by id, events by their exact row. Pending writes and write proposals are dropped with a count; the quarantine sidecar, the episode-pattern dismissals, the captures directory, the index and the lock files are left in place with a count; anything else is named as unknown and left alone. `--dry-run` prints the same report and writes nothing. Exit status zero with the report, two when `DIR` is not a directory, one when the store cannot be written.

### `bettermemory export [--output FILE] [--no-tombstones] [--scope S] [--strict]` and `bettermemory export --mirror DIR [--store FILE]`

The JSON export writes every active memory (and, unless `--no-tombstones`, every tombstone) with its full record, to stdout or `--output`; `--scope` narrows it. `--mirror` instead writes the store as a v8 directory under `DIR` (`src/bettermemory/mirror.py`): each active memory at its filename, tombstones under `.tombstones/` named by the v8 rule, episodes under `episodes/<session>/`, the bytes the v8 writers produced, pinned against golden files written by 8.0.0 (`tests/fixtures/v8/`), so a migrated store mirrors back to the directory it came from (`bench/parity/migrate_v8.py` records the round trip). `FILE` defaults to `memory.sqlite` in the resolved store directory and is opened without its key when the key is absent: an export reads and never rekeys. `DIR` must be absent, empty, or a mirror this command made before, which it marks with `.mirror.json`; anything else is refused, so a v8 store can never be written over. Inside a mirror, a file whose bytes already match is left alone, a changed or new one is written, and a file the store no longer holds is removed. The JSON export's flags do not combine with `--mirror`.

## CLI

`bettermemory` with no arguments is the MCP server over stdio. The commands:

| Command | What it does |
| --- | --- |
| `init --client X [--config-path P] [--print-only] [--name N] [--with-addendum]` | Register the server with a client, idempotently; `--with-addendum` prints the long-form policy. |
| `try [--json]` | The offline demo in a throwaway store. |
| `health [--days N] [--top-k N] [--min-applied N] [--json]` | The health report the `health` action returns. |
| `tombstones list [--scope S] [--json]`, `tombstones restore ID`, `tombstones prune --older-than DAYS [--dry-run]` | The removed memories: list, restore one (with the trust re-check), prune old ones. |
| `rename-scope OLD NEW [--no-tombstones] [--json]` | The `rename_scope` action. |
| `rollback --by-actor CLIENT [--since TS] [--apply --yes] [--json]` | Remove one actor's writes, as tombstones. |
| `episodes list [--json]`, `episodes prune [--ttl-days N] [--dry-run] [--json]` | The journal: list every session's episodes, prune past the TTL. |
| `export …` | The JSON export and the v8 mirror, above. |
| `migrate v8 …` | The 8.x migration, above. |
| `log verify [--json]` | The chain audit, above. |
| `eval [--since TS] [--scope S] [--report] [--tool-usage] [--threshold-sweep] [--widening-preview [--detail]] [--json] [--output FILE]` | The three effectiveness rates and their diagnostics ([eval.md](eval.md)). |
| `up [--foreground] [--port N]`, `down`, `status` | The local daemon for this store: one process that owns the store and serves the nine tools at `http://127.0.0.1:<port>/mcp` and the hook bodies under `/api/v1/hook/`. `bettermemory` with no arguments is the stdio shim in front of it; `serve` runs the in-process stdio server without it. |
| `hook session-start\|stop\|prompt [--quiet] [--dry-run]` | The Claude Code hook command; the plugin wires it. Reads the hook's stdin JSON, posts it to the daemon (starting one when none answers), prints the answer, always exits 0. `session-start`, `audit-turn` and `prompt-recall` are its 8.x aliases. |

## Naming conventions

These hold across the surface:

- `id` is the positional first argument when a tool acts on one memory, and the `id` parameter of a `memory_admin` action.
- `scopes` (plural) is the list-filter parameter; `scope` (singular) is the single-scope action parameter.
- Enum-typed parameters are plain `str` in the JSON surface and validated against closed sets at the handler (`confidence`, `source`, `category`, `outcome`, `mode`, `link.type`, `verdict`); `action` on `episode` and `memory_admin` is a literal enum in the served schema.
- Required arguments are always named in the description; defaults are conservative (`force=False`, `acknowledge_transient=False`, `include_bodies=False`, `category="fact"`).
- `memory_update` requires at least one of `content`, `scopes`, `confidence`, `category`, `links` at runtime, and each `memory_admin` action names the parameters it needs in a `ValueError` when one is missing; neither is expressible in JSON Schema.

The 9.x surface is the contract. Additions follow the rules in [`CONTRIBUTING.md`](../CONTRIBUTING.md). Removals, renames and default changes wait for a major; the next removal window is 10.0.
