# Changelog

Notable changes between releases. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely; the project uses
0.x SemVer where minor bumps are additive feature drops and patches are
fixes.

## Unreleased

### Added

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
  setup. Until this lands on PyPI for real, install remains
  clone-and-`uv tool install .`; the README install line will flip
  to `uv tool install bettermemory` at the 1.0 release.

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
