# API surface (1.x)

This document is the contractual list of MCP tools bettermemory
exposes. Signatures, defaults, and return shapes are stable within
the 1.x line — see [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the
deprecation policy. The 1.x surface was frozen at 1.0; subsequent
additions to the surface are permitted (and several have landed in
1.1 — `commit_drift` block on retrieval hits, `category` parameter
on `memory_write`, `corrected` outcome on `memory_record_use`,
`verification_debt` and `commit_drift_debt` rollups on
`memory_health`); renames, removals, and semantic redefinitions are
forbidden until a 2.0 bump.

The 17 tools group naturally:

- **Retrieval** — `memory_search`, `memory_show`, `memory_list`,
  `memory_scope_overview`
- **Writing** — `memory_write` (+ `memory_write_confirm` /
  `memory_write_cancel` for the staged-write flow), `memory_update`
- **Lifecycle** — `memory_remove`, `memory_restore`,
  `memory_list_tombstones`
- **Verification** — `memory_verify`
- **Curation** — `memory_record_use`, `memory_health`,
  `memory_rename_scope`
- **Session-local** — `memory_scope_disable`, `memory_scope_enable`

## Retrieval

### `memory_search(query, scopes?, max_results?, expand_top?, auto_scope?)`

Rank stored memories against a free-text query.

- `query: str` — required.
- `scopes: list[str] | None = None` — when set, only memories
  carrying at least one of these scopes are eligible.
- `max_results: int | None = None` — falls through to
  `behavior.default_max_results` (config default `5`). Capped at
  50.
- `expand_top: bool = False` — when the top hit's relevance is
  `"high"`, inline its full body and `path_drift` report so the
  caller can act without a `memory_show` round-trip. No-op for
  non-`"high"` top hits.
- `auto_scope: bool = True` — filter by the caller's current
  git repo. Memories with no recorded `origin` (legacy entries
  or writes from outside any repo) are treated as global and
  always pass.

Returns a list of hits. Each hit carries `id`, `scopes`,
`relevance` (`"high"` / `"medium"` / `"low"`), `match_terms`,
`snippet`, `created`, `updated`, `last_verified_at`, `verification`,
`path_drift_checked` / `path_drift_missing` integer counts, and
(when applicable) a `commit_drift_count` integer. The
`commit_drift_count` field is OMITTED from the hit when the signal
isn't applicable: caller not in any repo, hit from a different
repo, or hit has never been verified. A non-zero value is the cue
to spot-check the memory even when `verification.status == "fresh"`,
because the project has moved since the last `memory_verify`.

### `memory_show(id)`

- `id: str` — required, the memory's ULID.

Returns the full body plus `verification` (full block), `path_drift`
(full report when there's drift; `null` otherwise), and `commit_drift`
(full block when applicable; `null` when the caller isn't in the
matching repo or the memory was never verified). The `commit_drift`
block carries `status` (`"clean"` or `"drift"`),
`commits_since_verify` (the integer count), and `recommendation`
(actionable string on `"drift"`, null on `"clean"`).

### `memory_list(scopes?, with_bodies?)`

- `scopes: list[str] | None = None` — filter, same shape as
  `memory_search`.
- `with_bodies: bool = False` — opt in to inlining full bodies.
  Triage default returns body-stripped summaries.

### `memory_scope_overview(auto_scope?)`

Cheap session-start hint; counts per scope without bodies or IDs.

- `auto_scope: bool = True` — same semantics as
  `memory_search.auto_scope`.

Returns `{current_repo, scopes: {scope: count}, total}`.

## Writing

### `memory_write(content, scopes, confidence?, source?, category?, force?, acknowledge_transient?)`

- `content: str` — required.
- `scopes: list[str]` — required, non-empty.
- `confidence: str = "medium"` — one of `"low"`, `"medium"`,
  `"high"`. (Plain string in the JSON surface; validated against
  the `Confidence` enum at the handler.)
- `source: str = "explicit-statement"` — one of
  `"explicit-statement"`, `"inferred"`. Same string-vs-enum note.
- `category: str = "fact"` — one of `"fact"` or `"user-inference"`.
  `"fact"` (default) commits immediately; `"user-inference"`
  structurally goes pending and returns
  `{status: "pending", pending_id, pending_reason: "user-inference"}`
  regardless of the global `behavior.require_write_confirmation`
  config. Use `"user-inference"` for memories that capture claims
  about the user themselves (preferences, beliefs, working style)
  so the user always gets the conversational veto on
  misattribution.
- `force: bool = False` — bypass content dedup AND tombstone
  dedup. Reserve for the case where the new memory is genuinely
  adjacent to an existing match, not a duplicate of it.
- `acknowledge_transient: bool = False` — bypass the durability
  marker check. Logged as an override.

Possible result statuses: `"ok"` (committed; payload includes
the new memory id and any `related` medium-overlap matches),
`"transient_warning"` (durability gate fired; markers listed),
`"duplicate"` (content dedup fired; `matches` listed),
`"previously_removed"` (tombstone dedup fired; `removed_matches`
listed with their original `removed_reason`), `"pending"` (when
`category="user-inference"` is passed, OR when
`require_write_confirmation = true` in config — `pending_reason`
distinguishes the two; caller must follow with
`memory_write_confirm` either way).

### `memory_write_confirm(pending_id)` / `memory_write_cancel(pending_id)`

- `pending_id: str` — returned by a `memory_write` whose result
  was `"pending"`. Pending entries expire after one hour.

### `memory_update(id, content?, scopes?, confidence?)`

- `id: str` — required.
- `content`, `scopes`, `confidence` — at least one must be
  provided. `scopes` has replace semantics (provide the full new
  list, not a delta).

Preserves `id`, `created`, `source`. Bumps `updated`. Resets
`last_verified_at` to `null` on content change (the old
verification was for prose that no longer exists).

## Lifecycle

### `memory_remove(id, reason)`

- `id: str` — required.
- `reason: str` — required, non-empty. Captured into the
  tombstone's `removed_reason` and surfaced by future
  `memory_write` calls whose new body overlaps the removed body.

### `memory_restore(id)`

- `id: str` — must reference a tombstone (active IDs raise).

Strips removal frontmatter; preserves `created`, `updated`,
`last_verified_at`.

### `memory_list_tombstones(scopes?)`

- `scopes: list[str] | None = None` — filter as in
  `memory_list`.

## Verification

### `memory_verify(id, note?)`

- `id: str` — required.
- `note: str | None = None` — free-form; recorded in the event
  log.

Bumps `last_verified_at` to now without touching `updated`.
Idempotent.

## Curation

### `memory_record_use(memory_ids, outcome, note?)`

- `memory_ids: list[str]` — IDs that shaped the consuming
  response. Always plural even for a single memory.
- `outcome: str` — one of `"applied"`, `"ignored"`,
  `"contradicted"`, `"corrected"`. `"corrected"` is the audit-only
  sibling of `"contradicted"` — for the noticed-and-fixed-inline
  workflow where the caller has already run `memory_update` or
  `memory_verify` in the same turn. `"corrected"` increments a
  separate `corrected_count` on `MemoryStats` and never raises the
  `has_unresolved_contradiction` flag, so the previous foot-gun
  ("logged contradicted after the fix → flag stuck because event
  ts > resolution ts") is gone structurally.
- `note: str | None = None`.

### `memory_health(window_days?, heavily_used_top_k?, min_applied?)`

- `window_days: int = 30` — dead-weight cutoff window.
- `heavily_used_top_k: int = 10`.
- `min_applied: int | None = None` — falls through to
  `behavior.heavily_used_min_applied` (config default `3`).

Returns the aggregate health rollup: `total_active_memories`,
`total_events`, `distinct_sessions`, `dead_weight`, `heavily_used`,
`contradicted` (rows in this bucket carry a `resolution_timeline`
so a stuck flag can be self-diagnosed as out-of-order audit logging
vs. genuinely unresolved), `marker_stats`, `scope_distribution`,
`scope_health`, `rare_scopes` (singletons within Levenshtein
distance 2 of another scope — almost always real typos),
`orphan_use_events` (a fabricated-id smoke test), `verification_debt`
(partitions active memories into `never_verified` / `stale` / `fresh`
against `behavior.verification_stale_days`), and `commit_drift_debt`
(populated when the server runs in a repo whose memories live in
this store; surfaces rows whose verification anchor sits behind
HEAD, sorted most-commits-ahead first; `null` otherwise).

### `memory_rename_scope(old_scope, new_scope, include_tombstones?)`

- `old_scope: str` / `new_scope: str` — required.
- `include_tombstones: bool = True` — by default also rewrites
  scopes on tombstoned memories.

Returns `{active: [ids], tombstoned: [ids]}` for the records
that were actually touched.

## Session-local

### `memory_scope_disable(scope)` / `memory_scope_enable(scope)`

- `scope: str` — singular. Multi-scope mute is intentionally
  one-tool-call-per-scope; the muted set is small in practice.

Resets when the server process restarts. Disabled scopes are
filtered from `memory_search`, `memory_list`, and
`memory_scope_overview`.

## Audit conclusions (recorded for the 1.0 freeze, still applicable in 1.x)

The 1.0 surface audit deliberately compared every signature
against the patterns elsewhere in the API. The conclusions still
hold for the 1.x line:

- **Naming consistency.** `id` is always the positional first
  argument when a tool acts on one memory. `scopes` is always
  the parameter name for a list filter; `scope` (singular) is
  always the parameter name for a single-scope action. No
  hidden synonyms.
- **Plural vs singular.** `memory_record_use(memory_ids: list)`
  is plural even for the single-id case so the JSON surface is
  consistent with batch use; the cost is one extra `[…]` in the
  most common call shape, which is mild.
- **Required vs optional.** Required arguments are always
  named in the description; defaults are sensible
  (configuration-driven where applicable, conservative
  otherwise — `force=False`, `acknowledge_transient=False`,
  `with_bodies=False`, `category="fact"`).
- **Enums-as-strings.** `confidence`, `source`, `category`, and
  `outcome` are typed as `str` in the JSON surface and validated
  against closed sets at the handler. JSON Schema can't model
  Python enums in a way most MCP clients render well; the
  closed-set validation gives equivalent safety with a friendlier
  wire format.
- **Mutually-exclusive optionals.** `memory_update` requires at
  least one of `content` / `scopes` / `confidence` at runtime.
  This isn't expressible in the JSON Schema published to
  clients, but the handler returns a clear error message when
  no field is set.

The 1.x surface is the contract. Additions follow the
permitted-within-1.x rules in `CONTRIBUTING.md`; removals or
renames wait for 2.0.
