# Changelog

Notable changes between releases. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely. From 1.0
onward the project uses semver in the standard way: major bumps for
breaking changes, minor for additive features, patch for fixes. The
[1.x compatibility contract](CONTRIBUTING.md#versioning-and-the-1x-compatibility-contract)
spells out exactly what's stable.

## Unreleased

### Changed

- **MCP `instructions` block (`src/bettermemory/server.py`).** Rewritten
  to surface the v1.2 headline UX wins where every compliant client
  sees them: `staleness_verdict` (the rolled-up branch-on-this-first
  field) and the auto-`record_use` flow (the most-forgotten step is
  now opt-out). Previously the block named the three underlying
  staleness signals individually and described `memory_record_use` as
  a per-response obligation, neither of which matched the v1.2
  surface. Tightened the false-positives sentence and the
  session-start hint to make room for the new content under the
  1700-char regression budget (final body 1681 chars / 1687 utf-8
  bytes; all must-have phrases preserved). Also adds the
  `curation_pending` rollup mention to the session-start paragraph
  and `verified_paths` to the verify guidance.
- **Plugin `SKILL.md`.** Now opens with a six-row quick-card
  (Search? / Write? / Category? / Outcome? / Verify? / Scope?) so the
  decision rules are cheap to keep in working memory; the
  prose-heavy reference moves below it. Updated to the full v1.2
  surface throughout: `staleness_verdict` rollup as the
  branch-on-this-first field, auto-`record_use` via `use_token`,
  `category="ambient"`, `scope_mismatch` warning at write time,
  structured `verified_claims` on `memory_verify`, and the
  `dead_weight` / `cold_memories` split with the matching
  `curation_pending` rollup on `memory_scope_overview`.

### Fixed

- **Stale `uv.lock` package version.** The lockfile still pinned
  `bettermemory==0.1.0` from before the 1.0 release; refreshed to
  match `pyproject.toml`'s `1.2.0`. No dependency changes.

## 1.2.0 — 2026-05-10

Seven additive surface changes targeting the curation-and-feedback
loop. All purely additive on disk (`SCHEMA_VERSION` stays at 1) and
on the wire (legacy clients still get the same shape modulo the new
fields). Two themes: making the use-recording flow opt-out instead
of opt-in, and tightening the staleness-and-curation signal so the
model can self-prioritise without paying the full `memory_health`
cost on every turn.

### Added

- **`category="ambient"` on `memory_write`.** Joins `fact` (default,
  unchanged) and `user-inference` (existing pending-write gate). Use
  for atmospheric / response-shaping memories that don't make crisp
  verifiable claims (user identity, persistent environment quirks).
  Persisted on the memory record (legacy memories load with
  `category=None`; runtime treats that as the legacy fact-default).
  Ambient memories are excluded from the dead-weight curation rule
  because their value is implicit. A non-blocking
  `ambient_body_long` warning attaches to commits whose body exceeds
  500 words, so ambient memories don't drift into catch-all dumps.
- **`cold_memories` bucket on `memory_health`.** Memories created
  before the window with zero retrievals — distinct from
  `dead_weight`, which now means "retrieved but never `applied`".
  The two together separate "ranker isn't surfacing this memory"
  from "model retrieves but never gets value", so a curation pass
  can act on the right axis. `ScopeHealth.cold` mirrors `dead` for
  the per-scope rollup; `bettermemory health` text rendering shows
  both sections.
- **`staleness_verdict` derived field on every retrieval.** One of
  `"fresh" | "spot_check_recommended" | "spot_check_required"`,
  rolled up from `verification.status`, `path_drift_missing`, and
  `commit_drift_count`. Surfaced on `memory_show`, every
  `memory_search` hit (re-derived for the expanded top hit once
  body-level drift is known), `memory_list`, and the `with_bodies`
  list shape. The underlying signals stay; the verdict is the
  load-bearing field consumers should branch on first.
- **Auto-`record_use` via `use_token`.** Every `memory_search` hit and
  `memory_show` response now includes an opaque `use_token`. If the
  model doesn't call `memory_record_use` within ~2 turns, the server
  auto-commits the retrieval as `outcome="applied"` on the next
  memory_* call (logged with `auto=true`). The mechanical
  bookkeeping that was the most-forgotten step is now opt-out
  instead of opt-in. Explicit `memory_record_use(memory_ids=[...],
  outcome="ignored"|"contradicted"|"corrected")` still wins — the
  override path purges the pending token before recording so the
  auto-commit can't shadow the explicit outcome.
- **`curation_pending` rollup on `memory_scope_overview`.** Five
  integer counts — `{stale, never_verified, drifted, cold, dead}`
  — derived from the same logic as `memory_health` but without
  row materialisation. Lets the model spot pending curation at
  session start without paying the full health cost.
- **`scope_mismatch` warning at `memory_write` time.** Same design
  family as `transient_warning` and `duplicate`. If the body cites a
  known `projects:<name>` scope's name token (or a path under
  another project's tree) AND that scope isn't in the declared
  scope list, the write returns
  `{status:"scope_mismatch", suggested_scopes:[...], matches:[...]}`
  instead of committing. Override via
  `acknowledge_scope_mismatch=True` for legitimate cross-project
  references.
- **Structured `verified_claims` on `memory_verify`.**
  `verified_paths`, `verified_commits`, and `verified_versions`
  optional list parameters (caller passes the actual claims they
  spot-checked). Persisted on the memory record. The path-drift
  detector now surfaces a `verified` set on `PathDriftReport` (paths
  in the body that the caller previously attested AND that still
  exist on disk). The commit-drift signal narrows the count to
  commits that actually touched any of `verified_paths` — a memory
  verified for `[/etc/foo]` reads as `clean` even when the
  surrounding project moved, as long as `/etc/foo` itself didn't.
  `commits_since_touching_paths` in `origin.py` is the new git
  helper underneath. Calling `memory_verify` with `verified_paths=None`
  preserves any prior attestation; an explicit empty list `[]` clears
  it.

### Changed

- **`dead_weight` rule.** Was: `created_before_window AND applied_count == 0`.
  Now: `created_before_window AND retrieval_count > 0 AND applied_count == 0`,
  with ambient-category memories excluded entirely. Memories that aren't
  being retrieved are no longer mis-classified as dead — they go to the
  new `cold_memories` bucket where the curation question is "does the
  trigger for this memory still exist?", not "is the body misleading?".
- **`Memory` frontmatter.** New optional fields: `category`,
  `verified_paths`, `verified_commits`, `verified_versions`. Additive;
  legacy memories load cleanly with default values. Unknown category
  values fall back to `None` rather than raising — older readers
  encountering a future-introduced category degrade to fact semantics.

### Internal

- New `scope_match.py` module with `detect_scope_mismatch`,
  `collect_project_scopes`, `collect_project_roots`. Mirrors the
  shape of `durability.py`'s transient-marker module.
- New `compute_staleness_verdict` helper in `verify.py`. Single
  source of truth for the three-valued rollup.
- New `curation_counts` helper in `health.py`. Reuses the partitioning
  logic from `compute_health` but skips row materialisation; numerical
  contract locked in via tests.
- New `commits_since_touching_paths` helper in `origin.py`. Path-filtered
  variant of `commits_since`; returns `None` (not 0) when no useful
  filter survives the repo-root resolve, so the verified-paths
  short-circuit falls back to the unfiltered count rather than
  under-reporting drift.
- `SessionState` extended with `pending_use_tokens`, `turn_counter`,
  `issue_use_tokens`, `consume_old_tokens`, `purge_use_token`,
  `advance_turn`. The auto-`record_use` flow is implemented as a
  per-handler `_advance_turn(state, recorder)` call at every
  memory_* tool entry.

## 1.1.1 — 2026-05-09

Packaging metadata patch. The 1.1.0 PyPI listing rendered without a
"Project links" sidebar because `pyproject.toml` had no `[project.urls]`
table — visitors landed on the package page with no path back to source,
issues, or release notes. No code changes; PyPI re-publish only.

### Added

- **`[project.urls]` table in `pyproject.toml`.** Surfaces Homepage,
  Repository, Issues, and Changelog links on the PyPI project page's
  "Project links" rail. Without these, the package page on PyPI had no
  path back to GitHub, the issue tracker, or the changelog. Picked up
  automatically at wheel-build time — no other plumbing required.

## 1.1.0 — 2026-05-09

Three themes:

1. **A third staleness axis on every retrieval** — repo-aware
   commit-drift, the cwd-aware sibling of `verification` and
   `path_drift`. Catches the failure mode where calendar verification
   reads fresh but the project the memory describes has moved on.
2. **Structural fixes for the audit-after-fix workflow** that left
   `has_unresolved_contradiction` stuck — a new `corrected` outcome
   on `memory_record_use`, plus a `resolution_timeline` on each stuck
   row so the next mis-step is self-diagnosable.
3. **Distribution** — a Claude Code plugin (`/plugin install
   bettermemory@bettermemory`) that bundles the MCP server
   registration with a system-prompt-level skill, plus install-friction
   cleanup (canonical snippet shape, namespaced default entry name,
   `--version` flag, `importlib.metadata`-sourced `__version__`,
   trimmed MCP `instructions` block that fits under Claude Code's
   truncation cap).

### Added

#### Retrieval & verification

- **`commit_drift` advisory on retrieval.** Repo-aware sibling of
  `verification` and `path_drift`. When the caller is in a checkout of
  a memory's origin repo, `memory_show` and
  `memory_search(expand_top=True)` attach a `commit_drift` block:
  `status` is `"clean"` (zero commits since the last `memory_verify`)
  or `"drift"` (the count is positive). On `"drift"`, `recommendation`
  is an actionable string. Absent on the response when the caller
  isn't in any repo, is in a different repo, or the memory was never
  verified — silence beats a noisy "unknown" branch every consumer
  would have to filter. `memory_search` hits also carry a cheap
  per-row `commit_drift_count` integer for triage without an
  `expand_top` round-trip. Closes the gap where
  `verification.status == "fresh"` only proves calendar freshness
  while the repo can sit several commits ahead. Implemented as
  `verify.compute_commit_drift` plus two helpers in `origin.py`:
  `commits_since(cwd, since)` (per-memory cost) and
  `commit_author_timestamps(cwd)` (one git call + bisect, used by the
  health rollup so the cost is independent of memory count).

#### Curation

- **`commit_drift_debt` rollup on `memory_health`.** When the server
  is in a repo whose memories live in this store, surfaces rows whose
  verification anchor sits behind HEAD, sorted most-commits-ahead
  first. Capped row list (top 20) plus an uncapped `total_drifted`
  count, matching the `verification_debt` shape. `current_repo` and
  `current_cwd` are echoed back. Null when the server isn't in a
  repo, git is unreachable, or no memory's origin matches the current
  repo. Distinct from `verification_debt`: that bucket asks "how long
  since I checked?", this one asks "did the world I was checking
  against move?".

- **`verification_debt` rollup on `memory_health`.** Partitions active
  memories into `never_verified` / `stale` / `fresh` against the
  configured `behavior.verification_stale_days` threshold. Capped row
  lists (top 20, oldest-first) for inline display, plus uncapped
  totals so a curation pass can tell "5 stale" from "500 stale"
  without enumerating. The three counts always sum to
  `total_active_memories`. Surfaced in both the JSON tool output and
  the `bettermemory health` CLI's text rendering.

- **`corrected` outcome on `memory_record_use`.** A fourth value
  alongside `applied` / `ignored` / `contradicted`, for the
  noticed-and-fixed-inline workflow: the caller has already run
  `memory_update` and/or `memory_verify` in the same turn, and this
  event is the audit-trail entry. Audit-only — increments a new
  `corrected_count` on `MemoryStats` but never raises the
  `has_unresolved_contradiction` flag, so the previous foot-gun
  (`contradicted` logged *after* the fix → flag stuck because the
  event timestamp landed later than the resolution) is gone
  structurally.

- **`resolution_timeline` on each `memory_health.contradicted` row.**
  Chronological list of `update` / `verify` / `contradicted` /
  `corrected` events for the memory, with their notes. Lets the model
  self-diagnose a stuck flag as out-of-order audit logging (resolution
  events present but predating the contradicted event) vs. genuinely
  unresolved (no resolution events after the contradiction) without
  grepping `.events.jsonl`. Only populated for rows in the
  contradicted bucket; other rows keep an empty list.

#### Writing

- **`category="user-inference"` structural confirmation tier on
  `memory_write`.** A second value alongside the default
  `category="fact"`. When the caller passes `"user-inference"` the
  write goes pending and returns
  `{status:"pending", pending_id, pending_reason:"user-inference"}`
  instead of committing — the consumer is expected to ask the user
  conversationally before calling `memory_write_confirm(pending_id)`
  (or `memory_write_cancel(pending_id)` if the user declines). Fires
  regardless of the global `behavior.require_write_confirmation`
  config: misattribution sticks, so the user always gets the veto on
  claims about themselves. Project / infra / reference / tooling
  facts continue to commit immediately under the default category.

#### CLI

- **`bettermemory export`** dumps the active memory store (and
  tombstones, by default) as a single self-describing JSON document.
  Round-trippable; intended for backup, machine-to-machine migration,
  or feeding an external indexer. Writes to stdout unless `--output`
  is given.

- **`bettermemory --version`** prints `bettermemory <version>` and
  exits 0. The version is sourced from `importlib.metadata`, so it
  matches whatever `pip show bettermemory` reports — single source of
  truth, no drift.

#### Distribution

- **Claude Code plugin** (`/plugin marketplace add 0Mattias/bettermemory`
  → `/plugin install bettermemory@bettermemory`). The repo doubles as
  a plugin marketplace; `.claude-plugin/marketplace.json` at the root
  lists `plugin/` as the plugin source. The plugin bundles
  `plugin/.mcp.json` (registers the MCP server via `uvx bettermemory`,
  so users only need `uv` on PATH) plus
  `plugin/skills/bettermemory/SKILL.md` (the long-form policy as a
  Claude Code skill, which loads into the system prompt without the
  truncation cap that limits the MCP `instructions` block). Manual
  install (`bettermemory init --client claude-code`) remains supported
  and unchanged in shape.

- **Public-repo hygiene files**: `SECURITY.md` (threat model + private
  disclosure flow + supported-versions matrix), `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), `.github/ISSUE_TEMPLATE/` (install
  failure, bug report, feature request — install failure asks for
  `bettermemory doctor --json` output up front),
  `.github/pull_request_template.md`.

#### Tests

- **CLI smoke tests** (`tests/test_cli_smoke.py`). 17 tests pinning
  the argparse glue: `--help`, `--version`, every subcommand's
  `--help`, in-process invocations of `health` / `doctor` / `init` /
  unknown-subcommand exit code, plus two subprocess tests that pin
  the `python -m bettermemory` packaging path. Lifts `server.py`
  coverage from 65 % → 74 %.

- **Plugin manifest tests** (`tests/test_plugin.py`). Cheap validation
  guards: every plugin file exists, every JSON manifest parses,
  `marketplace.json` lists the plugin under the expected source path,
  the plugin manifest carries the conventional fields,
  `plugin/.mcp.json` registers the server under the canonical
  `bettermemory` key, the `SKILL.md` frontmatter has a non-trivial
  description and references the load-bearing tools. Plus
  version-sync tests that catch the case where `pyproject.toml`,
  `plugin/.claude-plugin/plugin.json`, and
  `.claude-plugin/marketplace.json` drift apart.

- **Version + instructions-budget regression tests**
  (`tests/test_version.py`, two checks in `tests/test_server.py`).
  Pin `bettermemory.__version__ == importlib.metadata.version("bettermemory")`,
  pin the `--version` output prefix, pin the MCP `instructions` body
  length under Claude Code's ~1.8KB truncation budget with both an
  upper bound (regrowth catch) and a lower bound (accidental wipe
  catch), pin a small set of load-bearing phrases in the body so a
  trimming pass that drops one shows up in CI.

### Changed

- **Default MCP entry name renamed `memory` → `bettermemory`.** The
  1.0 default was generic enough to collide with other MCP servers
  and with Claude Code's evolving built-in memory features; the new
  default is unambiguous. `bettermemory init --client X` detects a
  legacy `memory` entry whose `command` resolves to the same binary
  and removes it as part of the patch — upgrading users don't end up
  with the server registered twice. Migration is binary-equality
  gated, so a `memory` entry pointing at a different memory MCP
  server is left alone. The `--name` flag still overrides the default
  if you want the short name back.

- **MCP snippet shape includes `type: "stdio"` and `env: {}`.** Both
  optional in the MCP spec but match what `claude mcp add` and Claude
  Code 2.x write — the snippet now looks the same as the user's
  hand-added entries instead of looking deliberately minimal.

- **`bettermemory.__version__` reads from `importlib.metadata`.** The
  prior 0.x-era hard-coded literal had drifted past 1.0; switching to
  the metadata source makes drift structurally impossible. Falls back
  to `"0+unknown"` only when the package is imported from a source
  tree without an install (rare); a regression test pins the equality
  to `pip show`'s reported version.

- **MCP `instructions` block trimmed to ~1.6KB.** Claude Code 2.1.x
  truncates the block at roughly 1.8KB and renders an ellipsis
  mid-sentence; the previous body was over the cap and lost the
  writing-discipline tail to truncation. The trimmed body keeps the
  load-bearing parts (opt-in retrieval, when to / when not to call
  `memory_search`, the session-start hint, the transparency
  requirement, the verification rule) and pushes structural detail
  down to the individual tool descriptions, which are not subject to
  the same truncation. The longer-form policy lives in
  `docs/system_prompt.md` and in the plugin's `SKILL.md`.

### Fixed

- **`memory_verify` now resolves an unresolved contradiction.** Before
  this, only `memory_update` (which bumps `updated`) cleared the
  `has_unresolved_contradiction` flag. That left a sticky-flag failure
  mode: a session detects a contradiction, fixes the body, calls
  `memory_verify`, and *then* logs `record_use(contradicted)` after
  the fact — the event's timestamp lands later than the verify, so
  the flag never clears. The new rule treats either `updated` *or*
  `last_verified_at` newer than `last_contradicted_at` as resolution.
  Legacy stuck flags clear by re-running `memory_verify` after the
  contradiction event. The new `corrected` outcome (above) is the
  forward-looking fix for the same workflow.

- **`rare_scopes` only flags singletons that look like typos.** The
  previous heuristic flagged every `n=1` scope, which produced too
  many false positives — narrow legitimate scopes like `career` or
  `personal-context` got reported as suspect. The bucket now requires
  the singleton to be within Levenshtein distance 2 of another scope
  (`projct:foo` against `projects:foo`, `tool` against `tools`,
  `bug` / `bugs` pairs). Standalone narrow singletons no longer trip
  the bucket. Implemented via a new module-private
  `_edit_distance_within(a, b, max_dist)` helper in `health.py`.

- **`path_drift` no longer flags slash-prefixed CLI invocations as
  missing paths.** The extractor was treating backtick-wrapped Claude
  Code slash commands (`/plugin marketplace add owner/repo`,
  `/plugin install foo@bar`) and shell invocations starting at an
  absolute binary (`/usr/bin/env python -m bettermemory`) as path
  candidates, then routing them to `path_drift_missing` when the
  whole-string `Path.exists()` returned False — noisy false positives
  on any memory that quoted the install path or a shell example. The
  extractor now distinguishes command shape from real-paths-with-
  internal-whitespace by counting slashes: a CLI has either a
  single-slash command name as its first whitespace-separated chunk
  (`/plugin install …`) or 2+ adjacent slashless argument tokens
  (`/usr/bin/env python -m foo`), while a real path with internal
  whitespace (`/Users/Some User/file`) has slashes separating each
  directory boundary, so neither pattern fires. Counts `\` as well
  as `/` so Windows paths with internal spaces still pass. Five new
  regression tests in `tests/test_verify.py`.

## 1.0.0 — 2026-05-08

The first stable release. Three things changed under the hood between
the last 0.x and this one — they collectively retire every "but" the
README used to publish.

- **The system-prompt addendum is no longer required for correctness.**
  The opt-in retrieval policy, transparency requirement, and
  verification obligation now live in the MCP server's
  `instructions` block, which clients surface at the system-prompt
  level. Strangers who install bettermemory and skip the addendum get
  the right behavior anyway. The addendum file remains as the
  advanced tightening surface (full scope hygiene, confirmation-tier
  policy, expanded record-use guidance), but nothing breaks without
  it.

- **One-command onboarding.** `bettermemory init --client X` writes
  the right MCP config snippet into the right file for Claude Code,
  Claude Desktop, Cursor, Continue, or Cline. Idempotent: re-run
  with the same arguments is a no-op, with a different binary path
  is an update. `bettermemory doctor` diagnoses the most common
  install failures (binary on PATH, storage writable, MCP client
  configs cross-checked against the resolved binary path) with a
  one-line fix hint per check.

- **The 1.x contract is contractual.** Every memory and tombstone
  carries `schema_version: 1` in its frontmatter. Within 1.x the
  on-disk format and the 17-tool surface (signatures, defaults,
  return shapes — all pinned in `docs/api.md`) are stable. A
  multi-process concurrency stress test exercises the cross-process
  fcntl locks under contention to retire the previous "untested"
  caveat. Property-based tests pin the store's identity / round-trip
  / tombstone-restore invariants under random input.

Other surface updates: storage benchmark + Performance section in
the README documenting the practical ceiling (~50k memories before
the no-index walk dominates), CONTRIBUTING.md with the explicit
deprecation policy, programmatic-client example in
`examples/programmatic_client.py`, PyPI release workflow with
trusted publishing.

### Added

### Added

- **Cline support in `bettermemory init`** plus a comprehensive
  per-client setup doc at `docs/clients.md`.
  - Cline (the VS Code extension by `saoudrizwan.claude-dev`)
    joins the `--client` choices alongside claude-code,
    claude-desktop, cursor, and continue. Default target is the
    standard VS Code `globalStorage` path; Code-Insiders /
    Codium / VSCodium variants override via `--config-path`.
  - `docs/clients.md` collects the canonical config snippet and
    config-file location for each supported client, plus the
    expected restart behavior (Claude Desktop loads at startup
    and needs a restart; Continue auto-reloads; Cursor reloads
    per-window). The "snippet shape" is the same `mcpServers`
    map for every client because that's what the MCP spec
    standardizes — only the file path varies.

- **Programmatic client example**
  (`examples/programmatic_client.py`). A self-contained Python
  script that spawns `bettermemory` over stdio (via the official
  `mcp` SDK that's already a runtime dep, so no extra install)
  and walks through write → search → show → remove. Useful as a
  reference for integration tests, custom agents that want
  memory tools without a third-party MCP host, and one-off
  scripted curation passes. Defaults to a tmp dir for
  `BETTERMEMORY_DIR` so it never touches the user's real
  store; falls back to `python -m bettermemory` when no
  installed `bettermemory` binary is on PATH.

- **`CONTRIBUTING.md`**, with the explicit 1.x compatibility
  contract. Covers local dev setup, PR conventions, the
  versioning + deprecation policy, and the project values that
  shape review judgment. Headline: within 1.x, the surface in
  `docs/api.md` and the on-disk format pinned by
  `models.SCHEMA_VERSION` are stable; renames / removals /
  semantic redefinitions land at 2.0 with a documented
  migration path. Deprecation cycle requires at least one
  minor's notice in the changelog plus a one-time WARNING log
  per process before any 2.0 removal.

- **API surface document** (`docs/api.md`) pinning the 17-tool
  surface as the 1.0 contract. Covers signatures, defaults,
  return-status shapes (e.g. memory_write's
  `ok` / `transient_warning` / `duplicate` /
  `previously_removed` / `pending` discrimination), and the
  audit conclusions for each consistency dimension we checked
  (naming, plural-vs-singular, required-vs-optional,
  enums-as-strings, mutually-exclusive optionals). Findings:
  no signature requires a rename or default change before 1.0;
  the surface is frozen. README links to the doc from the
  Tools section.

- **Property-based tests for `Store` invariants**
  (`tests/test_store_properties.py`, six properties under
  `hypothesis`). Each property mints its own per-example subdir
  under `tmp_path` so hypothesis's fixture-reuse model doesn't
  accumulate disk state across examples. Properties under test:
  write round-trip identity (body and scopes survive); update
  preserves `id` + `created` and bumps `updated` monotonically;
  tombstone-then-restore is body- and timestamp-preserving;
  `mark_verified` is idempotent and monotonic; `load_all` is
  order-deterministic across consecutive calls; independent
  writes don't pollute each other on disk. `max_examples=10` per
  property — each example does real disk I/O, so the goal is
  breadth of input space (Unicode, near-empty strings, scope
  shapes that pass the regex but stress the formatter), not
  exhaustive enumeration. Adds `hypothesis>=6.0` to dev deps.

- **Storage benchmark** (`bench/storage.py`) and a **Performance
  characteristics** section in the README. Bench measures `Store.write`
  throughput, `Store.load_all` full-corpus scan, and `search()` keyword
  scoring across configurable corpus sizes. Default sizes are
  `1000,10000,50000`; output is a markdown table or `--json`. Bench
  runs in a `tempfile.mkdtemp` directory and tears it down on exit
  rather than ever touching the user's real `~/.claude-memory/`.

  Numbers from one run on Apple Silicon ship in the README so users
  have a reference shape for the latency curve before doing the bench
  themselves: ~16 ms search median at 1k memories, ~170 ms at 10k,
  ~1 s at 50k. Practical ceiling for the current
  no-index-walk-every-file architecture sits around 50k memories,
  which is far past where most stores ever grow if curated.

- **`schema_version` on frontmatter** (`models.SCHEMA_VERSION`,
  currently `1`). Every new memory and tombstone written by the
  store carries `schema_version: 1` as the first frontmatter
  key. Readers default to `1` when the field is absent — that's
  the implicit version of memories written before this constant
  existed (additive-fields-only era, where backward compat held
  by virtue of every new field being `Optional`).

  Forward-compatibility rule (now contractual rather than
  implicit): a reader that sees a memory whose `schema_version`
  is *strictly greater* than its own `SCHEMA_VERSION` raises
  `ValueError` on load. `Store.load_all` and
  `Store.load_tombstones` catch that and skip the file with a
  logged warning; `bettermemory doctor`'s `memory_parse_health`
  check surfaces the count gap. Net effect: a user who downgrades
  bettermemory after writing some memories under a newer minor
  sees those memories drop out of the retrieval surface (and
  flagged by doctor) rather than risk silent semantic
  misinterpretation. Tombstones share the same gate.

  Within a major version, bumps remain additive-only — new
  optional fields, never renamed, never removed, never
  re-defined. A *major* bump (1 → 2) is reserved for genuinely
  breaking format changes and will ship alongside a
  `bettermemory migrate` subcommand. The constant stays at 1
  until that day.

- **Multi-process concurrency stress test** (`tests/test_concurrency.py`).
  The README previously hedged: *"A file-lock guard is in place;
  multi-process is still untested."* This test spawns four worker
  processes (each its own Python interpreter via `spawn`, not `fork`,
  so the cross-process fcntl lock is actually exercised) and runs 50
  random write / update / remove / restore operations per worker on a
  shared store directory. Post-conditions assert: every active `.md`
  file parses cleanly (no torn writes), every tombstone carries the
  expected removal frontmatter, the event log is fully parseable JSONL
  (no half-line corruption at the append boundary), the active +
  tombstoned file count matches the worker write totals (no lost or
  duplicated IDs), and the lock isn't pathologically over-contended
  (concurrency_errors stay below ~25% of total attempts). The README
  caveat is updated accordingly: multi-process on Unix is now an
  exercised guarantee. Windows still falls back to a no-op lock — the
  MVP single-process recommendation stands there.

- **`bettermemory doctor` subcommand.** Self-diagnostic for the
  install: a series of independent checks that each return an
  `ok` / `warn` / `fail` verdict with an actionable fix hint when
  not ok. Exit code is `0` / `1` / `2` so the command is
  scriptable.

  Checks: Python version, binary on `$PATH` (warn when missing —
  GUI MCP clients spawn with a minimal PATH), config loadable,
  storage directory exists/writable (probe-write a sentinel file),
  memory frontmatter parses on every active memory, event log
  writable, semantic-dedup extras present when `semantic_dedup =
  true`, and a cross-check of every known MCP client's config file
  against the resolved binary path (catches the "I reinstalled
  bettermemory into a different venv and now nothing works"
  failure mode — the registered command path is stale).

  Each check is wrapped in `try/except` so a single broken probe
  surfaces as a `fail` diagnosis rather than crashing the whole
  report. JSON output (`--json`) is the machine-readable view for
  tooling; text output uses ✓ / ⚠ / ✗ glyphs and includes the fix
  hint inline. The `docs/installation.md` troubleshooting section
  now leads with `bettermemory doctor` rather than walking down
  the failure list manually.

- **`bettermemory init` subcommand.** One-shot onboarding that
  replaces the old "find your client's MCP config file, hand-edit
  the JSON" step. Two modes:
  - Show-and-tell (no flag): prints the resolved `bettermemory`
    binary path, the canonical `mcpServers` snippet, and a list of
    common per-client config locations with `[✓]` markers showing
    which already exist on the machine.
  - Patch (`--client X`): idempotently merges the bettermemory entry
    into the named client's MCP config file (creating parents and
    the file if missing). Re-running with an unchanged target is a
    no-op; a stale binary path is updated rather than duplicated;
    other entries in `mcpServers` are preserved.

  Supported clients: `claude-code`, `claude-desktop`, `cursor`,
  `continue`. Each entry is one getter function in `init.py`'s
  registry; adding a new client is a single-file change.

  Additional flags: `--print-only` (dump snippet without writing,
  useful for `| jq`), `--json` (structured output for tooling),
  `--name` (override the `mcpServers` key, default `memory`),
  `--config-path` (override the default target path for `--client`),
  `--with-addendum` (also print the optional advanced-tightening
  addendum from `docs/system_prompt.md`).

  README install instructions now lead with `bettermemory init
  --client X` rather than walking through manual JSON editing.
  `docs/installation.md` reframed in the same shape.

- **PyPI release workflow** (`.github/workflows/release.yml`).
  Tag-triggered: pushing `v<X.Y.Z>` runs the full gating suite (ruff,
  format, mypy strict, pytest with coverage floor), builds the wheel
  + sdist via `uv build`, publishes to PyPI through trusted
  publishing (no API tokens in repo secrets), and creates a GitHub
  release with auto-generated notes. Manual `workflow_dispatch`
  trigger supports a TestPyPI dry-run path. The build job verifies
  pyproject.toml version matches the tag before any artifact ships,
  so an off-by-one tag fails fast. Process documented in
  `docs/release.md`, including the one-time PyPI-side trusted-publisher
  setup. The 1.0 tag uses this workflow to publish — strangers get
  `uv tool install bettermemory` from PyPI directly.

### Changed

- **System-prompt addendum is no longer required for correctness.**
  Previously, `docs/system_prompt.md` was an explicit setup step
  (README "Quick start" step 3, with a bold warning that *"without
  this, the model will overuse memory"*). The opt-in policy,
  transparency requirement, and verification obligation now live in
  the server-level FastMCP `instructions` block — which every MCP
  client surfaces at the system-prompt level — and in each tool's
  `description`, refreshed per-call. A fresh install of bettermemory
  behaves correctly without copying anything from
  `docs/system_prompt.md`.

  The addendum file remains the canonical surface for **advanced
  tightening**: fuller scope hygiene, the confirmation-tier policy
  for preferences vs. facts, expanded record-use guidance,
  detailed verification ceremony. It complements the server
  `instructions`; it does not replace them.

  Touched: `src/bettermemory/server.py` (instructions block expanded
  from a 3-sentence hint to the full opt-in / transparency / verify
  briefing; per-tool descriptions on `memory_search`, `memory_show`,
  `memory_write`, and `memory_verify` extended to carry the
  obligations alongside their parameter docs). No behavior change in
  handlers; this is documentation-surface only. README and
  `docs/installation.md` updated to reframe the addendum as
  optional.

### Added

- **Structured `verification` block on every retrieval.** `last_verified_at`
  used to be a raw timestamp the consuming model had to do staleness
  arithmetic on — and prose-only guidance ("spot-check before relying")
  failed open whenever the model's attention wavered. A real-world
  drift escaped the system this way (a memory whose tool list lagged
  the code by three new tools went undetected because the consumer
  didn't notice `last_verified_at: null`). Retrieval responses now
  carry a structured verdict the model cannot easily skim past:

  ```json
  "verification": {
    "status": "never" | "stale" | "fresh",
    "last_verified_at": "<iso>" | null,
    "age_days": <int> | null,
    "recommendation": "<actionable string>" | null,
    "stale_after_days": <int>
  }
  ```

  - `status="never"` when the memory has not been spot-checked since
    write — `recommendation` carries an explicit "spot-check before
    relying, then call memory_verify" instruction.
  - `status="stale"` past `behavior.verification_stale_days` (default
    30, mirroring `recency_boost_half_life_days`) — `recommendation`
    names the age in days and asks for a re-spot-check.
  - `status="fresh"` within the window — `recommendation: null` is
    the explicit "nothing to do" signal so consumers branch on a
    stable shape.

  Surfaced on `memory_show`, every `memory_search` hit, every
  `memory_list` row (both summary and `with_bodies=True` variants).
  `last_verified_at` is preserved as a top-level field for back-compat;
  the new block is the structured replacement. The system-prompt
  addendum was rewritten to direct the model to branch on
  `verification.status` rather than read the prose. New
  `compute_verification_status` / `VerificationStatus` exports in
  `bettermemory.verify`. New `behavior.verification_stale_days` config
  knob — set to 0 to mark every verified memory stale immediately
  (test affordance), or raise the threshold for caches of facts whose
  ground truth changes slowly.

- **First-class tombstone lifecycle.** Removed memories used to be a
  black hole on the read side — invisible to dedup, invisible to search,
  with no path to restore short of hand-editing files. They now have a
  full lifecycle:
  - **`memory_list_tombstones(scopes?)`** lists removed records with
    their full removal metadata (`removed`, `removed_reason`,
    `removed_session`). Mirrors `memory_list` body-stripping for
    cheap triage. Sorted most-recent-first.
  - **`memory_restore(id)`** brings a tombstone back to the active
    set. Strips the removal frontmatter, preserves `created`,
    `updated`, and `last_verified_at` — the body didn't change while
    the record was gone, so the recency boost stays honest. Raises
    `NotTombstonedError` on active ids and `MemoryNotFoundError` on
    unknown ones; the asymmetry routes the caller to `memory_update`
    when they actually meant to edit.
  - **`bettermemory tombstones list` / `prune` CLI subcommands.**
    `list` mirrors the MCP tool. `prune --older-than DAYS` is the
    only path that hard-deletes tombstones; the default cutoff comes
    from new config knob `behavior.tombstone_retention_days` (0 means
    "no default — the flag is required"). Active memories are
    untouched. `--dry-run` previews; the prune is atomic and returns
    pruned ids in chronological order.
  - **`removed_session` frontmatter on tombstones.** The originating
    session id is now stamped into the file itself, so the join from
    a tombstone back to the session that produced it survives event-
    log rotation. Additive: legacy tombstones load with
    `removed_session=None`.
  - **`Store.restore`, `Store.list_tombstones`, `Store.load_tombstone`,
    `Store.prune_tombstones`** as the underlying API. The `restore`
    path handles active-filename collisions (when a new memory has
    squatted the slug since removal) by falling back to a short-id
    suffix, the same rule the active write path already uses.
- **Tombstone-aware dedup.** `memory_write` now scores the new body
  against the tombstone set in addition to active memories. A high
  overlap with a removed memory returns `status="previously_removed"`
  carrying the original `removed_reason` — the lesson encoded in the
  removal isn't silently re-discarded on re-write. The model can
  inspect the reason and either drop the write, call
  `memory_restore(id)` if the fact is now correct, or pass `force=true`
  if the new memory is meaningfully different. Medium-overlap
  tombstone matches surface as `removed_related` on a successful
  write, parallel to the active-side `related`. `SimilarHit` grew
  optional `removed_at` and `removed_reason` fields populated only
  for tombstone matches; the `relevance` ladder gained
  `"high-removed"` and `"medium-removed"` labels.
- **New `find_similar_tombstones` in `bettermemory.search`.** Mirrors
  `find_similar` for the tombstone path with both Jaccard and
  semantic-cosine modes. The semantic cache key uses the tombstone's
  `removed` timestamp, distinct from the active path's `updated`,
  so a restore-then-tombstone cycle produces correct cache
  invalidation.
- **`memory_rename_scope(old, new, include_tombstones?)`.** The cheap
  fix for typo'd or deprecated scopes (`projct:foo` -> `projects:foo`,
  `infra` -> `infrastructure`). Walks active memories — and
  tombstones, by default — and replaces the old scope with the new
  one, deduplicating if the new scope was already present. Bumps
  `updated` (metadata moved); preserves `last_verified_at` (the body
  didn't change). Validates both scopes against the standard scope
  format; rejects renames into a non-allowed scope when
  `[scopes] allowed` is non-empty. Returns
  `{active: [ids], tombstoned: [ids]}` for records actually modified.
- **`memory_health` observability extensions:**
  - **`scope_health`**, a per-scope rollup with active/dead/contradicted
    counts and an applied-events sum. Sorted by `active` descending so
    the heaviest-trafficked scopes lead. Sum of `active` across scopes
    can exceed `total_active_memories` because a memory tagged with N
    scopes is counted in each — that's the right shape for "where is
    the rot concentrated?"
  - **`rare_scopes`**, the singleton-scope bucket. Most often these
    are typos worth fixing via `memory_rename_scope`; occasionally
    they're legitimate one-offs to promote deliberately.
  - **`orphan_use_events`**, a counter of `memory_record_use` events
    whose memory_ids resolved to neither active nor tombstoned
    records. Growing counts are the smoke test for fabricated ULIDs
    on the model side.
- **Path-drift counts on every search hit.** `MemoryHit` carries
  `path_drift_checked` and `path_drift_missing` integers regardless
  of `expand_top`. The model can self-triage without a memory_show
  round-trip — high `path_drift_missing` is the cue to expand.
  `expand_top=True` continues to surface the full `PathDriftReport`
  on the top hit. Cost: one regex pass + up to 8 stat() calls per
  matched memory; the bodies are already in memory at search time.
- **Persistent embedding cache.** Behind
  `configure_persistent_cache(root, model_name)`, the in-process
  semantic-dedup cache flushes to
  `<root>/.embeddings.<safe_model>.npz` at the end of each
  `find_similar` call and rehydrates lazily on first use. A fresh
  MCP server doesn't have to re-embed the whole store. Wired up
  automatically when `[behavior] semantic_dedup = true`. Atomic
  `.tmp` + rename for crash safety; corrupt files log a WARNING
  and fall back to in-memory only. Model name is namespaced into
  the filename so swapping models produces a new file rather than
  mixing incompatible vectors. Numpy-only — degrades gracefully
  when the embeddings extra isn't installed.
- **`load_all` and `load_tombstones` are race-safe.** Both now catch
  `OSError` (notably `FileNotFoundError`) in addition to
  `ValueError`/`KeyError`, so a concurrent tombstone or prune that
  moves a file out from under the iteration yields the surviving
  records rather than crashing the call. Closes the gap where
  `memory_list(with_bodies=True)` could blow up mid-iteration.
- New `bettermemory.models.TombstonedMemory` and `TombstonedSummary`
  Pydantic models. Distinct types from `Memory` / `MemorySummary`
  so the type checker catches accidental mixing of active and
  removed records in callers walking both.
- New `bettermemory.store.NotTombstonedError` for the
  active-id-on-restore path.

### Changed

- **`memory_remove` stamps `removed_session` on tombstones.** The
  active session id is captured at removal time via the new
  `Store.tombstone(..., session_id=...)` keyword argument. Existing
  callers that don't pass `session_id` still work; the field is
  omitted from frontmatter when absent.
- **Auto-scope filter documented as a UX filter, not access control.**
  `memory_show(id)` doesn't auto-scope, by design — the threat model
  is "don't surface irrelevant memories by accident", not "prevent
  cross-project information flow". For real isolation, use
  project-scoped stores via `./.claude-memory/` or `BETTERMEMORY_DIR`.
  Clarified in `origin.py` module docstring and the README.
- **`memory_write` dedup short-circuit order:** active high-overlap
  match wins over tombstone high-overlap match, since there's a live
  record to update. Medium matches from both passes surface as
  `related` and `removed_related` respectively. `force=true`
  bypasses both gates as before.
- **`memory_search` description updated** to advertise
  `path_drift_checked` / `path_drift_missing` on every hit.
- **`memory_health` description updated** to advertise `scope_health`,
  `rare_scopes`, and `orphan_use_events`.
- **`SYSTEM_PROMPT_ADDENDUM` updated** with the new tools, the
  tombstone-aware dedup contract, and scope-hygiene guidance.

### Behavior changes worth flagging

- A `memory_write` whose body re-creates a previously-removed memory
  no longer commits silently. It returns `status="previously_removed"`.
  This is intentional and is the whole point of tombstone-aware
  dedup. The `force=true` override is the same one used for active
  duplicates. Tests that asserted the old "tombstones are ignored"
  invariant have been rewritten.

- **`memory_verify` tool + `last_verified_at` field.** The orthogonal
  axis to content edits: `memory_verify(id, note=...)` bumps
  `last_verified_at` to now after the caller has spot-checked the body's
  claims against ground truth. Distinct from `updated`, which moves
  whenever `memory_update` rewrites content — verification is "a
  human/agent confirmed reality matched the body on this date", editing
  is "the body changed on this date". A typo fix bumps `updated` but
  not `last_verified_at`; a verify call bumps `last_verified_at` but
  not `updated`. The field is surfaced on every retrieval response
  (`memory_show`, `memory_search` hits, `memory_list`, `_committed`,
  `MemoryStats`) so staleness is visible at a glance — `null` means
  "never verified since write". Frontmatter is additive: legacy
  memories without the field load fine; malformed values silently fall
  back to `None` rather than crashing the load. Idempotent (calling
  twice slides the timestamp forward); records a `kind: "verify"` event
  with the optional note.
- **`memory_scope_overview` tool.** Cheap session-start hint —
  per-scope counts (no bodies, IDs, or summaries) so the model can
  decide whether `memory_search` is likely to be fruitful before
  spending tokens on it. Auto-scoped to the current repo by default
  (uses bit-identical `repos_match` semantics as `memory_search`, so
  "5 here" reconciles with "5 in search"). Returns
  `{current_repo, current_cwd, auto_scope, scopes, total,
  disabled_scopes}` with scopes sorted count-desc then name-asc for
  determinism. Respects session-disabled scopes. Pass
  `auto_scope=False` for the cross-project view.
- **Path-drift detection on retrieval.** `memory_show` and
  `memory_search(expand_top=True)` extract path-shaped tokens from the
  body and stat them. Drift is surfaced as `path_drift.missing` —
  advisory, not a verdict (could be a temporary mount or a path on a
  different machine). `path_drift` is `null` when no drift is found so
  the consumer branches cleanly. Detection covers backtick-wrapped
  paths (highest precision), bare absolute Unix paths, `~/`-rooted
  paths, and Windows drive-letter paths; URLs, SSH remotes,
  `user@host:path`, and short paths (`/x`) are filtered. Two-pass
  extraction with backtick spans masked before the bare scan avoids
  double-counting. Capped at 8 paths per body and 512 chars per path.
  `OSError` from `Path.exists()` (permission denied, ELOOP, etc.) is
  treated as missing — semantically correct for a staleness signal.
- New `bettermemory.verify` module: `PathDriftReport`,
  `detect_path_drift()`.
- `Store.mark_verified(id)` — bumps `last_verified_at` without
  touching `updated`.
- **`bettermemory health --min-applied N` CLI flag** to override the
  configured `heavily_used_min_applied` floor for one invocation.
- **`bettermemory migrate origin` CLI subcommand.** One-shot backfill for
  legacy memories that pre-date the auto-scope feature (no `origin:`
  block in frontmatter). Three routing modes, in priority order:
  - **`--scope-repo SCOPE=URL`** (repeatable): route by tag. Right tool
    for global memory directories whose memories already carry
    `projects:<name>` scopes — first matching scope wins. Memories
    matching nothing in the map fall through.
  - **`--repo URL`**: force-tag every legacy memory with this URL.
    Coarse — only right when you know all memories in the dir really
    do come from one repo.
  - **Auto-inference**: when memory_dir's parent is itself a git repo
    (project-scoped layout), the repo URL is read from `git config`
    and the parent path becomes the origin's cwd.

  `cwd` is set only on the auto-inferred path — that's the only mode
  where we have legitimate evidence for a per-memory cwd. The other
  modes leave it null rather than fabricating one. Branch is always
  null since we don't know the original.

  Idempotent (memories with existing origin are skipped), atomic per
  file (`.tmp` + rename), `--dry-run` to preview. Tombstones are
  skipped — backfilling origin into a removal record would change the
  audit log retroactively.
- New `bettermemory.migrate` module: `infer_origin_for_memory_dir`,
  `migrate_origin_in_directory`, `MigrationReport`.
- **Semantic dedup (opt-in).** Behind `[behavior] semantic_dedup = true`,
  `memory_write` dedup uses sentence-transformers cosine similarity
  instead of Jaccard on token sets — catches paraphrases ("the database"
  vs "Postgres") that lexical overlap misses. Requires the `embeddings`
  extra (`pip install bettermemory[embeddings]`); falls back to Jaccard
  with a single WARNING log if the extra isn't installed, so flipping
  the toggle without the deps is safe. Embeddings are cached per-process
  keyed by `(memory_id, updated)`, so an updated memory busts its own
  cache entry. New config knobs: `semantic_model_name` (default
  `"all-MiniLM-L6-v2"`), `semantic_high_threshold` (0.85),
  `semantic_medium_threshold` (0.65).
- New `bettermemory.semantic` module: `get_model()`, `cached_embed()`,
  `cosine_similarity_normalized()`, `reset_caches()`. Imports of the
  optional extra are lazy — the module loads cleanly without it.
- **`memory_health` tool + `bettermemory health` CLI subcommand.**
  Aggregates the event log against the active store and returns a
  structured report: dead-weight memories (created beyond `window_days`
  ago, never `applied`), heavily-used memories (top-K by `applied`
  count), unresolved contradictions (memories with a `contradicted`
  event whose timestamp is after their last `updated`), per-marker fire
  and override rates for the durability gate, and the scope distribution
  histogram. The CLI mirrors the tool — `bettermemory health` for
  human-readable text, `bettermemory health --json` for machine-readable.
  Use the CLI for offline curation; use the tool for in-conversation
  introspection.
- New `bettermemory.health` module: `compute_health()`, `HealthReport`,
  `MemoryStats`, `MarkerStats`, `render_text()`, `render_json()`,
  `report_for_directory()`.
- `bettermemory.__main__` shim so `python -m bettermemory` mirrors the
  installed `bettermemory` script.
- **`memory_record_use` tool.** The model reports how a retrieved memory
  landed — `"applied"` (shaped the response), `"ignored"` (off-topic),
  or `"contradicted"` (user/state contradicted the stored fact) — with
  an optional free-form `note`. Each call writes one `kind: "use"` event
  to the log. This is the feedback signal that makes dead-weight pruning
  and contradiction surfacing possible in the upcoming memory_health
  view; without it, retrieval is write-only from the model's POV.
- **Auto-scope metadata.** `memory_write` captures the current cwd, git
  remote URL, and branch at write time and persists them under an `origin:`
  block in the memory's frontmatter. `memory_search(auto_scope=True)` (the
  new default) filters results to memories whose `origin.repo` matches the
  caller's current repo — addressing the cross-project leakage failure
  mode where a memory written for Project A surfaces during Project B
  conversations. Legacy memories (no `origin` field) and writes from
  outside any repo are treated as global and always surface. `auto_scope`
  is logged on each search event so the filter's behaviour is auditable.
  `memory_show` and `memory_list(with_bodies=True)` surface the full
  origin so a caller can verify which repo a memory came from.
- New `bettermemory.origin` module: `Origin` (Pydantic model), `capture()`,
  `repos_match()` (URL-form-agnostic equality — `git@github.com:o/r.git`
  and `https://github.com/o/r` describe the same project).
- **Append-only event log** at `<storage>/.events.jsonl`. Every tool call
  records one JSON line: query/returned IDs for searches, status/scopes for
  writes, ID for shows/updates/removes, etc. Auto-rotates (gzip) when the
  active log crosses `[telemetry] max_bytes` (default 10 MB). Each event
  carries a per-process `session` id so retrieval and write streams can be
  correlated.
- **`[telemetry]` config section** with `enabled` (default `true`) and
  `max_bytes` (default `10_000_000`). `enabled = false` makes every event a
  no-op and never creates the log file.
- **Structural durability gate.** `memory_write` now runs a regex check
  against the body for transient-state markers ("currently", "today I",
  "we just", "the new", commit-SHA-like hex tokens, etc.) before dedup.
  Hits return `{status: "transient_warning", markers: [...]}` instead of
  committing. Pass `acknowledge_transient=True` to override after rephrasing
  or deciding the marker is durable in context. Overrides are recorded in
  the event log per-marker so we can compute the false-positive rate and
  trim the marker list against real traffic.
- New `bettermemory.durability` module: `TRANSIENT_PHRASE_MARKERS` (the
  canonical list, single source of truth) and `find_transient_markers()`.
- New `bettermemory.events` module: `Recorder`, `iter_events`,
  `iter_all_events`.

### Changed

- **`heavily_used_min_applied` threshold (default 3).** New config knob
  in `[behavior]` floors the `heavily_used` bucket on `applied_count` —
  at 1 the bucket was dominated by one-off acknowledgements rather than
  repeat-use signal. Threaded through `compute_health`,
  `report_for_directory`, the `memory_health` tool (`min_applied` arg),
  and the `bettermemory health --min-applied` CLI flag. Clamped to ≥1
  internally so a misconfigured `0` doesn't dump every memory into the
  bucket. Lower it to 1 on a fresh store; raise it as the event log
  matures.
- **`MemoryStats` carries `last_verified_at`** so a curation pass can
  treat applied count and verification age as orthogonal staleness axes
  without a second round-trip through the store.
- **`memory_update` resets `last_verified_at` when content changes.**
  The prior verification was for prose that no longer exists; resetting
  forces the caller to spot-check the new body before a downstream
  consumer trusts the timestamp. Scope-only or confidence-only updates
  preserve `last_verified_at` since the body's claims didn't move.
- **`SYSTEM_PROMPT_ADDENDUM` hoists the no-filesystem-memory override
  to the first paragraph.** Many client harnesses ship their own
  filesystem-backed memory description in their default system prompt;
  the override has to land at the top so it wins before any later
  instruction can re-frame the model into filesystem mode. Also
  documents the new tools (`memory_verify`, `memory_scope_overview`),
  the staleness signals (`last_verified_at`, `path_drift`), and the
  session-start hint pattern. `docs/system_prompt.md` updated to match.
- `SYSTEM_PROMPT_ADDENDUM` rewritten so the durability rule references the
  structural enforcement rather than enumerating markers. The model gets
  the principle from the prompt and the specific marker that fired from
  the tool response. `docs/system_prompt.md` updated to match.
- `SYSTEM_PROMPT_ADDENDUM` lists the full current tool surface
  (`memory_health`, `memory_write_confirm`, `memory_write_cancel` were
  missing) and explicitly overrides any harness-injected file-based
  memory directory (e.g. `~/.claude/projects/*/memory/` or a `MEMORY.md`
  index). The Claude Code harness injects a `# Memory` section pointing
  at a per-project filesystem path; without an explicit override in the
  addendum the model sees two memory systems and splits facts between
  them. The `memory_record_use` paragraph now also references
  `memory_health` so the dead-weight feedback loop is visible from the
  prompt itself. `docs/system_prompt.md` updated to match.
- `build_server()` accepts an optional `recorder=` argument. When omitted,
  a `Recorder` is constructed from the resolved `Config`.
- `SessionState` now carries a stable `session_id` for the lifetime of the
  process. `state.reset()` deliberately preserves it.
- **Testing & CI hardening.** Several gaps caught in one sweep:
  - Python 3.14 added to the matrix (was 3.11–3.13). macOS and Windows
    slots added (one Python version each, 3.13) for platform coverage —
    `platformdirs`, `fcntl`-based locking, and the macOS UF_HIDDEN
    workaround in `tests/conftest.py` are all platform-sensitive.
  - `--cov-fail-under=80` enforces a coverage floor (current 85.32%).
  - `ruff format --check` runs in CI; the tree was reformatted to bring
    26 previously-unchecked files into compliance.
  - `mypy --strict` runs in CI, configured in `pyproject.toml` (strict
    on `src/bettermemory`, looser on `tests/` to avoid pytest-fixture
    `Any` noise). A `py.typed` marker ships with the package so
    consumers get types. `types-PyYAML` and `mypy` added to the `dev`
    extra.
  - New `test-embeddings` CI job installs `--extra embeddings` and
    runs `pytest -m "not no_extras"` so the cosine-similarity code
    path is exercised against a real `sentence-transformers` install.
    The three `test_semantic.py` tests that assert *absence* of the
    extra are tagged `@pytest.mark.no_extras` (registered in
    `pyproject.toml`).
  - `.pre-commit-config.yaml` mirrors the cheap CI checks (ruff,
    end-of-file-fixer, trailing-whitespace, yaml/toml syntax) for
    local pre-push catch.
  - `.github/dependabot.yml` keeps `github-actions` and `pip` deps
    current on a weekly cadence with grouped runtime/dev PRs.
