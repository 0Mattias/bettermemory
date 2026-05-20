# Changelog

Notable changes between releases. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely. From 1.0
onward the project uses semver in the standard way: major bumps for
breaking changes, minor for additive features, patch for fixes. The
[compatibility contract](CONTRIBUTING.md#versioning-and-the-compatibility-contract)
spells out exactly what's stable.

## 2.5.0 - 2026-05-20

**Verification-grade memory lane: positioning + eval CLI + recall fix +
Dreaming defense.** The verification-first rebrand, the `bettermemory
eval` CLI (`memory_helped_rate`, `endorsement_rate`, `silent_miss_rate`),
the `[embeddings-fast]` extra that closes the recall objection without
PyTorch, AND the `bettermemory consolidate --llm` Dreaming-defense pass
that proposes merges / contradiction resolutions / relative-date rewrites
/ tier demotions from a local Ollama model — refusing to commit any of
them without your explicit accept. The narrative phrase: *Anthropic's
Dreaming consolidates invisibly; bettermemory's `--llm` shows every
proposed diff and refuses to commit without your accept.*

### Added

- **`bettermemory consolidate --llm`: LLM-driven consolidation pass.**
  Extends the existing four offline passes (dedup, demote-never-applied,
  cold-scope, scope-typo) with a fifth that clusters related memories
  and asks an LLM to propose four kinds of mutation: `merge` (combine
  near-duplicates into a single keeper, tombstone the rest),
  `resolve_contradiction` (pick a winner from two memories that
  disagree, tombstone the loser), `rewrite_relative_date` (substitute
  absolute dates for "today" / "last week" phrases, with today's date
  passed via the prompt so the model doesn't infer it from stale
  training data), and `demote_tier` (retag `fact` -> `ambient` when
  the verifiable claim has been superseded). The `--llm-provider` flag
  picks between `ollama` (default — local HTTP on port 11434, no
  egress, no API key), `anthropic` (env `ANTHROPIC_API_KEY`, lazy-
  imports the `anthropic` SDK), and `openai` (env `OPENAI_API_KEY`,
  lazy-imports `openai`). Dry-run by default; `--apply` requires
  *either* `--yes` (batch accept) or an interactive TTY (per-proposal
  prompt). Hallucinated memory IDs (LLM produces a memory_id not
  in the cluster) are rejected at validation time *before* the diff
  renderer sees them. New module `src/bettermemory/llm.py` carries the
  proposal dataclasses, provider abstraction, prompt builder, validator,
  cluster builder (union-find on near-duplicate pairs + contradiction-
  event seeding), and the unified-diff renderer; `consolidate.py` gains
  `consolidate_llm()`, `_apply_llm_proposal()`, and the
  `LLMConsolidateReport` / `LLMProposalAction` / `LLMClusterFailure`
  dataclasses. 38 tests across `tests/test_llm.py` and
  `tests/test_consolidate_llm.py` cover validation, hallucination
  rejection, the apply gate, each proposal-type application, and the
  per-cluster failure-isolation contract.
- **`[embeddings-fast]` extra: fastembed + ONNX Runtime.** Same
  retrieval surface as `[embeddings]` (sentence-transformers), a tenth
  the install size. Default model `BAAI/bge-small-en-v1.5` (384-dim,
  ~33 MB ONNX) mirrors the dimensionality of `all-MiniLM-L6-v2` so
  cosine thresholds remain comparable. `[behavior] semantic_provider`
  picks between providers: `"auto"` (default) prefers torch when both
  installed (existing `.embeddings.<model>.npz` caches stay
  byte-stable), then fastembed, then Jaccard fallback. Explicit
  `"torch"` or `"fastembed"` honoured even when the extra isn't
  installed — the per-provider WARNING surfaces the missing-extra
  hint. Persistent cache namespaces by provider:
  `.embeddings.<model>.npz` (torch, legacy layout) vs
  `.embeddings.fastembed.<model>.npz` so flipping providers produces
  a fresh file rather than mixing incompatible vectors. CI gains a
  `test-embeddings-fast` job pinned to Python 3.13 (fastembed wheels
  lag 3.14); see `pyproject.toml` for the matching
  `no_fastembed` / `no_torch_embeddings` pytest markers.
- **`bettermemory reindex --embeddings`.** After rebuilding the FTS5
  index, re-embed every active body into the persistent cache.
  Provider+model-namespaced, so a config swap from torch to fastembed
  needs warming the new cache file — this is the surface for that
  warming pass. Reports `embedded` count, resolved provider/model,
  and the cache path on success; clean exit with an actionable
  message when the provider isn't available.
- **`bettermemory eval` CLI subcommand**. Reads `iter_all_events`
  output plus the active store, joins on memory id, and reports the
  three rates with Wilson 95% confidence intervals. Flags:
  `--since {N{s|m|h|d}|all}` (default `30d`, mirroring
  `verification_stale_days`), `--scope SCOPE`, `--min-retrievals N`
  (default 5, shared with `health._ENDORSEMENT_DEBT_MIN_RETRIEVALS`),
  `--silent-miss-limit N` (default 20), `--json`. Text renderer
  includes the rates, the endorsement-debt rows, and the recent
  silent-miss candidates; JSON renderer carries every count + CI
  bound for CI pipelines. Pure compute layer in
  `src/bettermemory/eval.py` (`compute_eval`, `parse_since`,
  `_wilson_interval`, `render_text`); 52 tests in
  `tests/test_eval.py` cover each numerator/denominator path,
  scope/since filtering, the ambient + tombstoned + has-explicit
  endorsement-debt exclusions, the silent-miss buffer cap, and a
  CLI smoke run.
- **`docs/eval.md`** defines the three rates publicly so they're
  citable by any system that exposes the right telemetry, not just
  this one. Includes the 2×3 healthy-vs-pathological matrix,
  comparison to LongMemEval, the CLI shape, and the calibration
  caveats (the `v1_top1_high` threshold rule's behaviour on real
  distributions is the open question).
- **`docs/blog/memory-is-rotting.md`** standalone post draft on the
  motivating problem (auth-middleware example), the staleness-verdict
  trifecta, claim-level audit, and the endorsement-debt category.
  Designed for HN / Lobsters / r/LocalLLaMA discussion.
- **`docs/ROADMAP.md`** publicly commits the next four work items
  (optional fastembed embeddings, the eval CLI shipped here, local
  `consolidate --llm` Dreaming-equivalent, Claude Code auto-memory
  ingest bridge) and the deliberately-out-of-scope list (managed
  cloud, multi-user RBAC, graph backend, non-MCP SDK, LongMemEval
  leaderboard chase).

### Changed (positioning, no behaviour change)

- **README, pyproject `description`, plugin marketplace, plugin
  README, and SKILL frontmatter rebranded around verification.**
  Hero line is now "Memory you can verify"; the comparison table
  bolds the four rows where bettermemory uniquely runs (per-hit
  staleness verdict, claim-level audit trail, user-inference
  confirmation tier, endorsement-debt visibility); the Features
  list leads with a "Verification surface" section. The previous
  "persistent memory for Claude Code, retrieved on demand" framing
  remained accurate but didn't differentiate from a now-commoditized
  local-MCP memory market (claude-mem at 65k stars, a dozen
  SQLite-FTS5 clones, Anthropic's free vendor-native auto-memory +
  Dreaming). The verification surface is the lane no funded
  competitor (Mem0, Zep, Letta, Cognee, Supermemory, claude-mem)
  occupies; the rebrand makes that legible at the PyPI/GitHub
  surface.

## 2.4.0 - 2026-05-20

**Path-drift extractor: narrow single-segment routes.** A bugfix
release, but tagged minor because it changes the path-candidate set
the `path_drift` signal acts on — consumers tracking
`path_drift_missing` counts will see lower numbers on bodies that
document URL routes inline.

### Fixed (correctness)

- **`/verify`-shaped URL routes no longer flagged as missing
  filesystem paths.** The path extractor in `verify.py` was treating
  backtick-wrapped single-segment absolute paths (`/verify`,
  `/healthz`, `/login`, `/api`) as filesystem candidates and
  stat'ing them. They reliably failed the stat and surfaced as
  `path_drift_missing` on every retrieval of any memory whose body
  documents a route-typed API. The canonical bite: the bettermemory
  memory documenting the 2.0.0 web UI fix ("Web UI ``/verify`` POST:
  CSRF Origin check and length cap.") produced a phantom drift
  signal on every search. New helper
  `verify._is_single_segment_routelike` rejects extensionless
  single-segment absolute paths at extraction time. Multi-segment
  paths (`/Users/...`, `/etc/foo.conf`), home-relative paths
  (`~/...`), Windows paths, and extensioned single-segment paths
  (`/foo.txt`) are unaffected. Bare top-level system dirs (`/etc`,
  `/var`, `/usr`) get filtered too but always exist on the systems
  this runs on, so no real drift signal is lost. Five regression
  tests in `tests/test_verify.py` cover the production bite, the
  broader route class, and the unaffected-by-narrowing edges.

## 2.3.2 - 2026-05-20

**Polish release.** One model-facing terminology fix on top of
2.3.1, plus dependabot bumps on the release workflow's action
versions. No code-logic, schema, or surface changes.

### Fixed (housekeeping)

- **`DESC_MEMORY_RECORD_USE` terminology drift.** The first sentence
  said "auto-recorded" while the next sentence and every other
  surface (docs, SKILL.md, this CHANGELOG) said "auto-committed".
  Model-facing string, so consistency lands every Claude session.
  Aligned to "auto-committed".

### Chores

- Bump CI release-workflow actions: `actions/upload-artifact` v4→v7,
  `actions/download-artifact` v4→v8, `softprops/action-gh-release`
  v2→v3. Workflow-only; no behavior change for downstream users.

## 2.3.1 - 2026-05-20

**Audit-pass follow-up.** 2.3.0 cut as a single rebased push of 12
audit commits — the chain never ran CI individually, so the first
push to main failed format check (16 unrun files) and the format
fix-up exposed a mypy regression in the new lock-discipline tests.
This patch closes the deeper bugs the rebase-squash papered over.
All fixes are correctness-only; no surface or schema changes.

### Fixed (correctness)

- **`rename_scope` tombstone branch TOCTOU.** The 2.3.0 lock-reads
  fix covered the active-side branch but left the tombstone branch
  reading `frontmatter.load(tpath)` outside the lock. A concurrent
  `restore` could land between the read and the write and have its
  rewrite clobbered. The read now lives inside the same
  `_locked(tpath)` block as the write. Regression test in
  `tests/test_store_locking.py` traces `fm_load` against the
  tombstone path's `_locked` window.

- **Index `filename` column wrong for collision-suffixed files.**
  The schema-v2 id → filename lookup derived its filename via
  `_filename_for(memory)`, which only knew `(created, slug)` — no
  collision suffix. Two memories sharing a date+slug had their
  index rows both pointing at the unsuffixed file; a search hit on
  the second memory resolved to the first memory's body, tripped
  the `memory.id != cid` defense, and got dropped. Fix threads the
  actual filename through `index.upsert(..., *, filename=)`,
  `index.rebuild(..., items: Iterable[tuple[Path, Memory]])`, and
  `_index_upsert_quietly(..., *, filename=)`. New helper
  `Store.iter_active()` yields `(path, memory)` pairs for the
  rebuild path. Regression test in `tests/test_index.py`.

- **`_load_search_candidates` empty-loaded fallback.** When the
  FTS pre-filter returned candidate ids but every filename lookup
  missed (pre-v2 schema rows, every match tombstoned mid-search,
  etc.), the handler returned an empty list — the comment in
  `_filename_for` had claimed the `load_all` fallback covered that
  case; it didn't. The fallback is now actually wired up. Regression
  test blanks every filename column in the index and confirms
  search still surfaces the matching memory.

- **Stop-hook event missing `assistant_present`.** `hook.run_audit`
  accepted `assistant_response: str | None` but never wrote it to
  the `turn_audited` event, while the in-process
  `memory_audit_turn` handler did emit the flag. Downstream rollups
  joining the two event sources saw an inconsistent field shape.
  Hook now mirrors the handler. Regression tests cover both
  branches (text block present → True; thinking/tool_use only →
  False).

### Fixed (housekeeping)

- **`index._connect` style cleanup.** `import contextlib` and
  `import os as _os` moved from inside the function to module top;
  `except BaseException` narrowed to `except Exception` so a
  `KeyboardInterrupt` / `SystemExit` during connect setup doesn't
  get swallowed silently.

- **`test_store_locking.py` mypy regression** (already in main via
  `1f72222`). The fixture patched `store_module.frontmatter.load`,
  which tripped mypy's strict re-export rule since
  `store.py` aliases the module via `from . import _frontmatter as
  frontmatter`. Switched the patch target to the already-imported
  `_fm` alias.

## 2.3.0 - 2026-05-20

**Production-readiness audit pass.** 12 commits closing ~28 audit
findings across correctness, security, performance, and the
model-facing surface. The release is cut as a minor bump because
the FTS5 index schema goes from v1 to v2 (drop-and-rebuild on first
launch, transparent), a new `bettermemory audit-turn` CLI subcommand
ships, and the plugin manifest now declares a Stop hook that fires
silent-miss telemetry on every assistant turn. Schema_version on
memory files stays at 1 — no on-disk format change.

### Fixed (correctness)

- **TOCTOU race in mutation paths.** `mark_verified` / `tombstone` /
  `restore` / `rename_scope` previously read the target file before
  acquiring the file lock, opening a window where a concurrent
  `update` from `web.py` or `sync.py` could be silently clobbered.
  Reads now happen inside the same `_locked()` block as the write.
- **FTS5 index drift on `rename_scope` and `restore`.** Renaming a
  scope wrote the new list to disk without updating the index's
  `scopes_text` column; BM25 ranking on the renamed scope read
  against stale text until the next manual reindex. `restore` was
  missing the index upsert entirely — restored memories were absent
  from indexed search until reindex. Both paths now call
  `_index_upsert_quietly` after the file write.
- **Consolidate failures aggregated, not silently swallowed.** The
  dedup / demotion apply loops logged per-failure warnings but
  never rolled them up; a run hitting 10 disk-full errors scrolled
  past the user's terminal with no summary signal. Added
  `ConsolidateFailure` plus a `failures` list on `ConsolidateReport`;
  both the text and JSON renderings now show the rollup.
- **SQLite connection leak on PRAGMA failure.** `index._connect`
  could leak the open `sqlite3.Connection` when a PRAGMA raised
  mid-setup (corrupt or zero-byte DB). Surfaced as
  `ResourceWarning: unclosed database` in two tests. Now wraps the
  post-connect setup in a try/except that closes on failure.

### Added (security)

- **Sync stderr redaction in push/pull error paths.** The default
  `_run_git` path already scrubbed credentialed URLs through
  `_redact_text`; the `push` and `pull` paths built their own
  `SyncError` from raw stderr to attach actionable hints, and that
  branch leaked the URL. Both now wrap the surfaced text.
- **Symlink rejection in store iteration.** `_iter_active_paths`
  and `_iter_tombstone_paths` previously called `entry.is_file()`,
  which follows symlinks. A hostile remote pushing `something.md`
  as a symlink to an arbitrary readable file would have its target
  loaded and parsed on the next `load_all`. Now skips
  `is_symlink()` entries.
- **CSRF header-less POST rejection.** `web._same_origin` accepted
  state-changing POSTs that arrived without `Origin` or `Referer`
  headers on the rationale that some browsers strip Referer. In
  practice modern browsers send Origin reliably; a header-less
  POST is a non-browser tool (`curl -X POST`) hitting the endpoint
  directly. Header-less POSTs are now rejected; CLI scripts that
  drive the UI should set `-H "Origin: http://127.0.0.1:<port>"`.
- **YAML body-size cap in frontmatter parser.** `_frontmatter.loads`
  uses YAML `SafeLoader`, which protects against `!!python/object`
  but not against alias-expansion DoS (the "billion laughs" pattern).
  The store widens its trust boundary once `sync pull` is in use
  (a remote can write into the memory directory); a 64 KB pre-flight
  check now rejects oversized frontmatter before `yaml.load` sees it.
- **`note` field length cap on memory_verify / memory_record_use.**
  The web `/verify` endpoint already capped `note` at 500 chars; the
  MCP entry points didn't, leaving a hostile-client surface to
  inflate the JSONL event log. Same 500-char ceiling now enforced
  on the MCP side.
- **Git argv validation.** `sync.init` / `push` / `pull` validate
  `remote` and `default_branch` against `^[A-Za-z0-9][A-Za-z0-9._/-]*$`
  before passing positionally to git. Belt-and-suspenders against a
  value like `--exec=evil` being parsed as a flag in older gits.
- **`--no-tags` on `git pull --rebase`.** Hostile / sloppy remotes
  pushing refs under `refs/tags/` would otherwise be mirrored into
  the local `.git/refs/tags/`; a tag named `main` could shadow the
  branch on a later checkout.
- **0o600 permissions on data files.** Memory `.md` files, the
  event log `.events.jsonl`, the SQLite index and its WAL/SHM
  siblings all inherit the user umask (typically 0o644 — world-
  readable on default Linux/macOS). Lock files already used 0o600;
  the data path now matches. Best-effort; no-op on Windows.
- **System-directory warning on misconfigured `BETTERMEMORY_DIR`.**
  `config.resolved_directory` now logs a WARNING when the resolved
  path lives under `/etc`, `/usr`, `/bin`, `/sbin`, `/boot`, `/dev`,
  `/proc`, or `/sys`. Catches the typical footgun where someone
  typed a system path by mistake; `/var` is intentionally excluded
  because macOS routes the per-user tmp through `/var/folders/...`.

### Added (features)

- **`bettermemory audit-turn` CLI subcommand** wraps the silent-
  miss audit (previously only the `memory_audit_turn` MCP tool) for
  client-side hook invocation. Reads the Claude Code Stop-hook
  stdin JSON (session_id + transcript_path), parses the transcript
  to find the latest user message, and runs `probe_for_miss`
  against the active store. Always exits 0 by design so a hook
  misfire never breaks the turn-end pipeline.
- **Plugin Stop hook** in `plugin/hooks/hooks.json` declares the
  binding: `uvx bettermemory audit-turn --quiet || true`. Closes
  the silent-miss feedback loop without requiring the model to
  remember to call the MCP tool. The `|| true` is belt-and-
  suspenders so an old PyPI snapshot or an `uvx` cold-start issue
  never surfaces as a Claude Code error banner.
- **Cross-process session-disabled-scopes divergence (known
  limitation)**: the Stop hook can't read the MCP server's
  in-memory `SessionState`, so scopes the user disabled in the
  current session via `memory_scope_disable` are still in scope
  for the hook's audit. Stop-hook events carry
  `triggered_from="stop_hook"` so downstream rollups can
  distinguish; the model-side `memory_audit_turn` events remain
  the strict source of truth.

### Performance

- **FTS5 schema v2: id→filename + memory_links tables.** Two hot
  paths in `_handlers.py` were still `load_all`-ing per call
  despite the index being available:
  - `_load_search_candidates` intersected the FTS candidate set
    against `store.load_all()` for every search.
  - `_links_payload` walked every active memory's frontmatter on
    every `memory_show` to compute reverse-links.
  Schema v2 adds a `filename` column to the `memories` table and a
  separate `memory_links` table (with a DELETE-cascade trigger so
  reverse-link queries don't dangle on tombstone). Both handlers
  use the new index helpers; the index now does what it always
  said it did.
- **v1 → v2 migration**: `_ensure_schema` detects the version
  mismatch, drops the data tables, and recreates empty. The Store
  hooks repopulate gradually as writes land; `bettermemory reindex`
  does the explicit full rebuild. The fallback in
  `_load_search_candidates` routes to `load_all` while the index is
  empty, so search keeps working through the transition with no
  user-visible break.
- **Index drift defense on candidate loads** (review fix): after
  resolving a candidate id to a filename and reading the file,
  verify the loaded memory's id matches the candidate id before
  appending to the result. Catches the `sync pull` window where
  the index's filename column briefly points at a path whose body
  has changed.

### Changed (model-facing surface)

- **Tool descriptions trimmed.** `DESC_MEMORY_SEARCH`, `_WRITE`,
  `_SHOW`, `_HEALTH`, `_UPDATE`, `_VERIFY`, `_RECORD_USE`, and
  `_SCOPE_OVERVIEW` were rewritten around "API surface + branching
  cues" rather than repeating policy that lives in the system
  `instructions` block and SKILL.md. Combined: 23,202 → 16,774
  chars (~5,800 → ~4,193 tokens). Every branching field a model
  needs to call the tool correctly survives.
- **`SYSTEM_PROMPT_ADDENDUM` restructured around the same quick-card
  opener SKILL.md uses.** The previous addendum was prose-heavy
  and lacked the decide-at-a-glance table; the rewrite is closer
  in shape to the skill, which makes "addendum and skill carry the
  same policy" closer to true. 8,255 → 6,512 chars (~2,063 → ~1,628
  tokens). The byte-equality drift test against
  `docs/system_prompt.md` is updated to match.
- **`memory_scope_overview` description** now spells out all seven
  keys it returns (was claiming three) including the load-bearing
  `curation_pending` rollup the addendum tells the model to read
  at session start.
- **18-tool count corrected.** The 2.1.0 release added
  `memory_audit_turn` (the 18th tool); the 2.1.1 docs-condense pass
  propagated the prior "17 tools" count throughout the live
  surface. README, api.md, plugin/README, CONTRIBUTING, and the
  server.py registration comment now read "18".

### Changed (tests + dev workflow)

- **`tests/test_sync.py` sandboxed.** The fixture was running
  `git config --global user.{email,name}` to make commits work,
  which silently overwrote the developer's `~/.gitconfig` on every
  local run. Fix: redirect git's global config to a per-test tmp
  file via `GIT_CONFIG_GLOBAL`.
- **Subprocess tests gated.** Five tests that invoke
  `bettermemory` as a subprocess fail on local checkouts without
  `pip install -e .`; now skip with a clear "run `pip install -e .`
  locally" reason rather than failing loud.
- **Three new structural drift tests** in `tests/test_prompts.py`:
  every `memory_*` name referenced in `SYSTEM_PROMPT_ADDENDUM` and
  in the plugin's `SKILL.md` must resolve to a tool the server
  registers. A rename or removal that forgets the policy surfaces
  shows up here.
- **In-process CLI coverage.** New tests for `consolidate` (text +
  --json), `tombstones list` (text + --json), `export -o`,
  `reindex`, and `migrate origin` exercise the dispatch arms that
  the subprocess tests previously protected. `server.py` coverage:
  41% → 55%.

### Documentation

- `examples/memories/*.md` files now carry `schema_version: 1` as
  the first frontmatter key (matching what `store.py` actually
  writes; the previous examples were missing the field).
- `CHANGELOG.md:7` anchor link to CONTRIBUTING.md was broken
  (`#versioning-and-the-1x-compatibility-contract` — the `1x-`
  was dropped during the 2.0 rewrite). Fixed.
- `docs/api.md` `memory_write` parameter signature reordered to
  match `_handlers.py`.
- The 2.2.0 entry's lede ("No code... behaviour is byte-identical")
  was self-contradicting against the `_handlers.py` / `audit.py` /
  `groundedness.py` edits listed two paragraphs below. Lede
  reworded to "No behavioural changes — only docstrings and code
  comments were touched."

### Deferred

- L9 (`time.sleep(0.01)` → freezegun-style explicit timestamps in
  test fixtures). The refactor needs a fixture that monkeypatches
  `utcnow` across every consumer module — multiple
  `from .models import utcnow` sites capture the reference at
  import time, so patching the canonical source doesn't propagate.
  The sleeps work today; the refactor warrants its own focused
  commit rather than bundling here.

## 2.2.0 - 2026-05-20

**Documentation tone pass.** No behavioural, on-disk-format, or
tool-surface changes; the source-file edits below are scoped to
docstrings and code comments, so runtime behaviour is byte-identical
to 2.1.1. The release is cut as a minor bump rather than a patch
because the public-facing language in `README.md` and
`docs/v1.6-plan.md` changed materially — anyone linking to the
prior README will land on a different framing of the project.

### Changed

- `README.md` comparison table reworked. The previous version
  compared bettermemory to mem0 / Letta / Zep / Cognee / Anthropic
  Memory Tool in a "Yes / No" scoreboard format, included a
  "Production junk-rate report" row citing one specific issue in a
  competitor's tracker, and several cells were stale or factually
  off (the mem0 retrieval contract, mem0's typed graph edges, mem0's
  temporal reasoning, Cognee's explicit `search()` API, the
  cross-host sync "Cloud-only" framing for self-hostable
  competitors). Rewritten as a neutral six-row design-space table
  that describes each system in its own terms.
- `README.md` opening framing and "Out of scope" section rewritten
  to lead with what bettermemory *is*, not what other tools aren't.
  Removed the "home-lab notes" example. The "Origins" personal
  anecdote was condensed into a short "Design notes" paragraph
  focused on the motivating problem and the design response.
- `docs/v1.6-plan.md` rewritten as a clean historical planning
  record. The May 2026 landscape snapshot now describes each related
  project in its own design language; competitive-pitch and
  weakness columns dropped. The tier-1 heading no longer reads as a
  rebuttal frame.
- `CHANGELOG.md` 2.0.0 entries cleaned. The descriptive prose for
  T1.1 (provenance), T1.3 (groundedness gate), and the
  claim-excerpts feature no longer names a specific competitor or
  cites a specific issue number when describing the failure mode
  these features address. The technical descriptions are preserved
  verbatim.
- `src/bettermemory/audit.py`, `src/bettermemory/groundedness.py`,
  and `src/bettermemory/_handlers.py` (groundedness-check comment +
  one tool-description example string) had the same scrubbing pass
  applied — module docstrings now describe the auto-extraction
  failure mode generically rather than via a competitor's bug
  tracker.

## 2.1.1 - 2026-05-20

**Documentation pass.** No code or on-disk format changes; the
exported `SYSTEM_PROMPT_ADDENDUM` constant is shorter but carries
the same load-bearing policy. Plugin users get a shorter `SKILL.md`
on next install; programmatic consumers of `SYSTEM_PROMPT_ADDENDUM`
get the trimmed body.

### Changed

- `README.md` rewritten end-to-end. ~66% shorter (489 → 164 lines).
  Lost the per-feature "(new in 2.0)" / "(new in 2.1)" markers
  (CHANGELOG owns history), the duplicate install paths, the full
  17-row tool table (now a grouped list pointing at `docs/api.md`),
  and the internals deep-dives that belong in `/docs` (event log,
  durability check internals, groundedness gate internals,
  performance characteristics, full config sample). Comparison
  table trimmed from 16 to 10 rows. The PyPI landing page renders
  this README, so the change ships to PyPI on next release.
- `plugin/README.md`, `plugin/skills/bettermemory/SKILL.md`,
  `docs/installation.md`, `docs/clients.md`, `docs/system_prompt.md`,
  and `src/bettermemory/prompts.py` (`SYSTEM_PROMPT_ADDENDUM`)
  condensed in the same pass. The drift test keeps the addendum and
  its doc copy byte-identical.
- `docs/api.md` reorganized. The previous version listed
  `memory_write` twice (once in the retrieval section, then again
  under writing), put `memory_show` after `memory_write`, and was
  missing `memory_audit_turn` entirely. Tools now appear in the
  documented group order (Retrieval, Writing, Lifecycle,
  Verification, Curation, Session-local) and all 18 are covered.

## 2.1.0 - 2026-05-20

**Silent-miss telemetry and endorsement-debt curation.** Two additive
features close the false-negative half of the opt-in retrieval
contract and add a "weakly endorsed" curation pivot. No on-disk
breaking changes — every new wire field is opt-in or absence-as-signal,
SCHEMA_VERSION stays at 1, and legacy events load unchanged
(`auto`-absent reads as explicit so pre-auto-commit history isn't
silently relabelled). Test count: 970 → 1021 (+51).

### Added

- `memory_audit_turn` MCP tool. Fires from a client-side end-of-turn
  hook with the user's message; runs a search probe over the active
  store using the model's configured search mode and asks whether a
  `search` or `show` event fired in the same session within
  `lookback_seconds` (default 60s, clamped to [1, 600]). When a
  high-relevance hit exists AND no retrieval happened in the window,
  emits a `search_miss` event so curation views surface the rate.
  Always emits `turn_audited` so audit cadence is visible in the log
  even when nothing's flagged. The threshold rule is versioned
  (`THRESHOLD_RULE_V1 = "v1_top1_high"`) and recorded on every event
  so a later calibration pass can replay historical logs under a new
  threshold without losing the audit trail. Surface:
  `bettermemory.audit` module exports `probe_for_miss`,
  `MissReport`, `MissHit`, `DEFAULT_LOOKBACK_SECONDS`,
  `THRESHOLD_RULE_V1`.
- Auto-vs-explicit applied count split on `MemoryStats`.
  `applied_count` (the total) is now backed by `auto_applied_count`
  (the server's auto-commit pass) plus `explicit_applied_count`
  (model called `memory_record_use` directly), with
  `endorsement_ratio = explicit / total` (or `None` on zero applies).
  Legacy events without the `auto` field count as explicit so
  pre-auto-commit history reads cleanly. The `heavily_used` render
  in `memory_health` now shows `applied=N (auto=X exp=Y)`.
- `endorsement_debt` rollup on `HealthReport` and `curation_counts`.
  The "weakly endorsed" bucket: memories the ranker keeps surfacing
  (`retrieval_count >= 5`) that the model never deliberately reaches
  for (`explicit_applied_count == 0`). Complement to `dead_weight`
  (never applied at all, auto included): dead_weight says the model
  doesn't even let the auto pass run on this; endorsement_debt says
  applies happened, but every single one was the auto fallback.
  Ambient memories are excluded — their value is implicit and
  explicit use events are structurally rare. Capped rows for inline
  display plus an uncapped `total` for bucket size. Threshold
  tunable via `endorsement_debt_min_retrievals` (clamped to >=1).
- `silent_misses` rollup on `HealthReport` and `curation_counts`.
  Counts `turn_audited` (denominator) and `search_miss` (numerator)
  events; the two-count shape distinguishes "stalled hook"
  (audited_total=0) from "healthy run" (audited_total>>0,
  miss_total=0). `memory_scope_overview.curation_pending` surfaces
  the miss numerator alongside `endorsement_debt` so session-start
  signals whether either pile is non-empty.

### Internal

- Health renderer fix: rename the inner `rate_pct` binding in the
  silent-misses block so it doesn't shadow the marker-stats one
  inside the same function scope.

## 2.0.0 - 2026-05-16

**Verification-grade memory.** The 1.6 plan in `docs/v1.6-plan.md`
shipped as one major release: nine features in three tiers turn
bettermemory into the only memory MCP with claim-level provenance,
write-time hallucination detection, an FTS5 inverted index over the
file-backed store, git-based cross-host sync, and a local web UI for
curation. Test count: 821 → 970 (+149). No on-disk breaking changes
— legacy memories load unchanged, every new wire field is opt-in or
absence-as-signal, and the SCHEMA_VERSION stays at 1. The 2.0 bump
reflects scope, not incompatibility.

What's new at a glance:

| Tier | Feature | Closes |
|---|---|---|
| T1.1 | Claim-level provenance (`claim_excerpts` on `memory_record_use`) | the hallucination-amplification gap in auto-extracting systems |
| T1.2 | Hybrid retrieval (BM25 + Jaccard + semantic via RRF) | the "keyword-only search" rebuttal |
| T1.3 | Write-time groundedness gate on `memory_write` | the HaluMem benchmark, operationalised |
| T2.1 | `bettermemory consolidate` CLI | the Letta sleep-time gap, no dual-agent topology |
| T2.2 | Typed inter-memory links (supersedes / contradicts / extends / depends_on) | graph-lite without graph DB infra |
| T2.3 | `recent_negative_outcomes` annotation on search hits | "model keeps re-suggesting rejected memories" |
| T3.1 | SQLite FTS5 inverted index + `bettermemory reindex` CLI | the load_all linear-scan ceiling at ~5-10K |
| T4.1 | `bettermemory sync` (git-based) | cross-host replication without a custom protocol |
| T4.3 | `bettermemory ui` local web UI | curation surfaces where a UI beats tool calls |

The competitive landscape (May 2026) is detailed in
`docs/v1.6-plan.md`. Per-feature detail follows.

### Added

- Local web UI (T4.3 of the 1.6 plan in `docs/v1.6-plan.md`). A
  small FastAPI app surfacing the curation surfaces that beat
  tool calls in a browser: memory_health rollups (active count,
  never-verified, stale verifications, dead-weight, cold,
  unresolved contradictions), a searchable memory list with scope
  filter, per-memory detail view showing body / scopes /
  timestamps / verified paths / typed links, and a one-click
  "Mark verified now" form that bumps `last_verified_at` and 303s
  back to the detail page (PRG pattern — refreshes don't repeat
  the verify). Tombstone browser with removal reasons. Run via
  `bettermemory ui --host 127.0.0.1 --port 8765` (local-only by
  default; binding non-loopback logs a warning since the UI
  exposes curation surfaces). The handler renders inline HTML
  with `html.escape` everywhere — no template engine, no JS
  framework, no XSS via memory_write. Gated behind a new
  optional `[ui]` extra (fastapi + uvicorn + httpx); the CLI
  prints a clean install hint when the extra is missing. No
  editing surface — writes happen in-conversation, the UI is
  read-mostly with verify as the one mutation since "I just
  spot-checked this" is a natural human action. Surface:
  `bettermemory.web` module exports `build_app(config, store)`
  for callers who want to mount the app under their own server
  and `serve(config, host=, port=)` for the standard uvicorn
  case.
- `bettermemory sync` CLI subcommand for cross-host replication
  (T4.1 of the 1.6 plan in `docs/v1.6-plan.md`). Thin wrapper over
  git — the memory directory is already plain markdown, so git's
  history / distributed copies / three-way merge handle the
  interesting cases without a custom protocol. Five subcommands:
  `sync init [--remote URL]` initialises the dir as a git repo and
  writes a `.gitignore` that excludes the regenerable caches
  (`.index.sqlite`, `.events.jsonl`, `.embeddings.*.npz`, lock
  files, doctor probes); `sync status` reports branch, pending
  changes, and remote ahead/behind counts; `sync push` stages,
  commits with a default `bettermemory: sync` message, and pushes
  (no-op when nothing changed locally — the `committed=False`
  signal in the response distinguishes this from "pushed prior
  commits"); `sync pull` rebase-pulls and rebuilds the FTS5 index
  so the runtime view matches the new file contents (Store hooks
  bypassed during the merge); `sync auto` is pull-then-push, the
  shell-alias / cron one-shot. `--set-upstream` is automatic on
  the first push so a subsequent `pull` has a tracking branch.
  Merge conflicts fall through to git's normal flow — `git
  rebase --continue` from the memory directory once resolved.
  Surface: `bettermemory.sync` module exports `init()`, `status()`,
  `push()`, `pull()`, `auto()`, the `SyncStatus` dataclass, the
  `SyncError` exception, and the `DEFAULT_COMMIT_MESSAGE` constant.
- SQLite FTS5 inverted index (T3.1 of the 1.6 plan in
  `docs/v1.6-plan.md`). Files on disk stay canonical; the index
  is a derived cache at `<store>/.index.sqlite`. Schema: a
  `memories` table mirroring the on-disk records plus an FTS5
  virtual table over body + scope text, kept in sync by three
  triggers. Store hooks keep the index live on every `write`,
  `update`, and `tombstone`. Index hooks are best-effort: a
  corrupted database or missing file logs a warning and lets the
  canonical write proceed, so on-disk truth is never blocked on
  an index failure. New CLI subcommand `bettermemory reindex`
  rebuilds the index from scratch (use it after hand-editing
  memory files or restoring from backup). A schema-version field
  in the `meta` table refuses to load indexes newer than the
  reader supports. `memory_search` now uses the index as a
  candidate pre-filter when `indexed_count >= 500` (tunable via
  the `BETTERMEMORY_INDEX_THRESHOLD` env var): up to 50
  candidates from the FTS5 query, then the existing rankers
  reorder within that pool. Falls back to `load_all` when the
  index is missing, corrupt, below threshold, or returns zero
  candidates — small stores see byte-stable behaviour, large
  stores skip the linear scan that starts to bite at ~5-10K
  memories. Surface: `bettermemory.index` module exports
  `rebuild()`, `upsert()`, `remove()`, `query()`, `status()`,
  the `IndexVersionError` exception, and the `INDEX_FILENAME` /
  `SCHEMA_VERSION` constants.
- Typed inter-memory links (T2.2 of the 1.6 plan in
  `docs/v1.6-plan.md`). New `links` field on the `Memory` model:
  a list of `{type, target_id, note?}` entries where `type` is one
  of `supersedes`, `contradicts`, `extends`, `depends_on`.
  Persisted in YAML frontmatter; legacy memories load with an empty
  list. Settable via the new `links` parameter on `memory_update`
  (REPLACE semantics: pass the full new list, or `[]` to clear).
  Self-links are rejected; target_id must be a valid ULID. Surface
  at retrieval is bidirectional: `memory_show` on a source memory
  returns the forward `links`; `memory_show` on the target carries
  `reverse_links` (with `source_id` instead of `target_id`) so the
  consumer sees the relationship from either side. Forward-compat
  guarantee: unknown link types on disk load silently as empty
  rather than failing the whole record. Graph-lite without the
  graph DB infra burden — adopted from mcp-memory-service's typed-
  edges idea but plumbed into retrieval, not just storage.
- Write-time groundedness gate on `memory_write` (T1.3 of the 1.6
  plan in `docs/v1.6-plan.md`). Optional, opt-in via the new
  `groundedness_check=True` parameter plus a `source_transcript`
  (recent conversation turns). The server walks the body sentence-
  by-sentence and flags any sentence whose stopword-stripped, kebab-
  expanded content tokens overlap the transcript's token set by less
  than 30% — the "fact pulled from thin air" failure mode common to
  auto-extracting memory systems. Returns
  `{status: "ungrounded", claims: [{sentence, overlap_ratio}, ...]}`
  instead of committing; the caller can rephrase or pass the new
  `acknowledge_ungrounded=True` override (same family as
  `acknowledge_transient` and `acknowledge_scope_mismatch`) when
  they have other grounding sources (a file read, a tool result)
  not represented in the transcript. Off by default — back-compat
  for every existing caller. Implements HaluMem-style operation-
  level write-time hallucination evaluation inline.
  Surface: `bettermemory.groundedness` module exports
  `check_groundedness()`, the `UngroundedClaim` dataclass, and the
  threshold constants for callers wanting to wire the gate into
  alternate flows.
- Negative-outcome annotations on `memory_search` hits (T2.3 of the
  1.6 plan in `docs/v1.6-plan.md`). When a hit's memory has been
  `ignored` or `contradicted` within the last 30 days AND not since
  been `applied`, the hit carries a `recent_negative_outcomes` field
  — a list (at most one entry per outcome type, so two entries max)
  describing the rejection. Each entry has `outcome`,
  `most_recent_ts`, `count_in_window`, `session_id`, `note`, and
  `claim_excerpt` (when the original record_use carried one — T1.1
  integration). The supersession rule is the load-bearing semantic:
  an `applied` event after a negative event clears the negative-
  bucket entries, because the user already validated the memory
  after the rejection; surfacing the rejection then would be
  misleading. `corrected` outcomes never surface (audit-only — the
  drift was salvaged inline). The field is OMITTED from the hit when
  no qualifying negatives exist — absence is the default. Stops the
  "model keeps re-suggesting memories the user already rejected"
  failure mode without any state on the client side. One event-log
  iteration per search call, then per-id bucketing; cost is bounded
  regardless of result count.
- `bettermemory consolidate` CLI subcommand (T2.1 of the 1.6 plan in
  `docs/v1.6-plan.md`). Offline curation pass over the store with
  four operations: near-duplicate dedup (semantic when the
  embeddings extra is installed, Jaccard otherwise — the newer-
  `updated` member wins, ties broken by `verified_paths` attestation
  then ULID), demote-never-applied (mirrors `memory_health`'s
  dead-weight rule; retags `category=ambient` so the memory stops
  appearing in the dead-weight bucket without losing the body),
  cold-scope suggestions (scopes whose newest memory has aged past
  `--cold-scope-days` AND with no `applied` events on any member),
  and scope-typo pairs (Levenshtein ≤ `--typo-distance` neighbors;
  the scope with more memories is the keeper, the lesser is the
  proposed typo). Dry-run by default; `--apply` commits dedup
  tombstones and category demotions (cold scopes and typo pairs
  stay suggest-only regardless — they touch shape that needs human
  review). `--json` for machine consumption. Closes the Letta-style
  sleep-time consolidation gap without the dual-agent topology.
  Surface: `bettermemory.consolidate` module exports
  `consolidate()`, the per-pass `find_*` helpers, the
  `ConsolidateReport` dataclass, and `render_text` / `render_json`
  for callers that want the data without going through the CLI.
- Claim-level provenance on `memory_record_use` (T1.1 of the 1.6 plan
  in `docs/v1.6-plan.md`). New optional `claim_excerpts` parameter —
  a list parallel to `memory_ids` (same length, one entry per id, or
  `None` per slot for "no specific claim noted") carrying the
  load-bearing phrase the model applied, ignored, contradicted, or
  corrected from each memory. Stored in the event log so a later
  audit can trace any response back to the specific claim, not just
  the memory id. Excerpts strip surrounding whitespace, reject empty
  strings (use `None` for "no claim"), and cap at 500 chars to keep
  the audit log small and discourage dumping bodies. Byte-stable on
  the wire when not used: existing event-log readers don't see a new
  null key on every old event. Works for all four record_use
  outcomes; especially useful for `contradicted` and `corrected` so
  the audit log records which claim was wrong, not just that the
  memory had drift. Closes the provenance gap in auto-extraction
  systems, which amplify hallucinations because the audit trail
  doesn't tie a wrong response back to the specific stored claim
  that caused it.
- Hybrid retrieval for `memory_search` (T1.2 of the 1.6 plan in
  `docs/v1.6-plan.md`). The original keyword scorer (TF + coverage +
  recency) is now one of four selectable rankers; the new ones are
  Okapi BM25 (IDF-weighted, TF-saturated, length-normalised — closes
  the recall gap on rare-term queries), sentence-transformers cosine
  (paraphrase matching when the embeddings extra is installed), and
  hybrid (Reciprocal Rank Fusion over keyword + BM25, plus semantic
  when available). Selection is per-call via the new `mode` parameter
  on `memory_search` (`"keyword"` | `"bm25"` | `"semantic"` |
  `"hybrid"`) or globally via `[behavior] search_mode` in config. The
  default stays `keyword` in 2.0.0 to keep ranking byte-stable; the
  flip to `hybrid` is planned for a later release once dogfooding
  shakes out regressions. Hybrid mode without the embeddings extra
  degrades gracefully to keyword + BM25 fusion; `mode="semantic"`
  without the extra raises with an install hint. The fused-score
  scale (~0.01 – 0.05 from `1/(k+rank)` summed) differs from the
  single-ranker scales, so consumers should keep using the
  `relevance` label, not the raw `score`, for cross-mode comparison.
  Surface: `bettermemory.search.compute_idf`,
  `score_memory_bm25`, `reciprocal_rank_fusion`, and the `SearchMode`
  Literal type are exported for callers that want to wire the
  rankers directly without going through `search()`.

### Fixed

Five fixes landed inside the v2.0.0 tag window: the initial
`release: 2.0.0` commit went out, two CI failures (sdist excludes
and a ruff format miss) blocked the Release workflow, and during
the retag cycle these five fixes were picked up before the green
tag landed. All are documented here for the audit trail.

- `memory_write` and `bettermemory consolidate`: in 3+ way duplicate
  clusters, the dedup pass could tombstone a memory it had crowned
  as the keeper of an earlier pair (when the same id appeared as
  the duplicate in a later pair), leaving the first pair's
  "near-duplicate of X" tombstone reason dangling against a
  now-tombstoned X. The apply loop now tracks `keepers_so_far`
  alongside `tombstoned_ids` and skips any pair that names a prior
  keeper as its duplicate.
- `memory_update`: editing the body reset `last_verified_at` to
  null but left `verified_paths`, `verified_commits`, and
  `verified_versions` populated from the prior content. Those
  attestations were attached to prose that no longer exists, so a
  later `memory_search` could read a stale `verified_paths` set
  against new body text and suppress the path-drift signal it
  should have produced. Body-edit updates now clear the structured
  attestation lists in lockstep with `last_verified_at`. Scope,
  confidence, category, and links edits still preserve the
  attestation (they don't touch the body's claims).
- `memory_search`: `MemoryHit.category` is declared as
  `Category | None` on the model, but `_build_hit` constructed the
  hit without the field, so every result silently carried
  `category=None` regardless of the stored memory's actual category.
  Hits now carry the persisted category, surfacing ambient and
  user-inference markers to callers that filter on it.
- `bettermemory sync status`: `git status --porcelain` v1 uses a
  fixed-width `XY␣path` shape where the X char is a space for
  modified-not-staged files. The previous `line.partition(" ")`
  split dropped the status char into the path, recording the
  modified file as `"M filename"` in `SyncStatus.modified`. Now
  parsed by position. Separately, `init` action strings,
  `SyncStatus.remote_url`, and `SyncError` messages echoed credentialed
  HTTPS remote URLs (`https://user:token@github.com/...`) verbatim,
  so a piped `bettermemory sync status --json` or a `git push`
  failure surfaced the token. Added `_redact_url` and `_redact_text`
  helpers that mask the userinfo segment while leaving SSH URLs
  (`git@host:path`) alone.
- `bettermemory ui`: the one state-changing endpoint
  (`POST /memories/{id}/verify`) now requires the request's Origin
  (preferred) or Referer header to point at a loopback host —
  loopback binding alone doesn't stop a malicious page in another
  browser tab from POSTing to localhost and forging a verify event,
  which would corrupt a load-bearing trust signal. Header-less POSTs
  (server-rendered classic forms under stricter referrer-policy
  settings) still fall through, since refusing every header-less
  POST would break the normal in-UI flow; the guard catches the
  case where a third-party origin actively attaches its own header
  (the default cross-site form behaviour in mainstream browsers).
  The `note` form field is also now capped at 500 chars (matching
  `claim_excerpts` on `memory_record_use`) so a paste-bomb can't
  inflate the event log.

## 1.5.0 - 2026-05-13

A multi-agent audit pass surfaced six bugs and one missing feature
spread across the data, search, verify, and origin layers. Six fix
commits and one feature commit landed off that audit. No on-disk
format changes — `Origin.worktree_root` is an additive optional field
and legacy memories without it pass through every new filter
unchanged. Consumers pinned to `>=1.4.2` upgrade transparently;
behaviour changes are observable but each one is a bug fix in the
direction the docstring already promised.

### Added

- `Origin.worktree_root` is captured at write time via
  `git rev-parse --show-toplevel` and threaded through the auto-scope
  filter on `memory_search` and `memory_scope_overview`. Fixes the
  audit's "worktree leakage" scenario where two `git worktree add`
  checkouts of one repo shared `origin.repo` and so cross-contaminated
  each other's search results — repo-only matching had no signal to
  tell sibling worktrees apart, and a memory written from
  `~/repo-feature/` would surface in a search run from
  `~/repo-bugfix/`. Worktree filtering rides on the same
  `auto_scope` toggle as repo filtering (one knob, not two), and a
  legacy memory with no `worktree_root` field always passes the new
  filter, so nothing pre-existing gets silently hidden.

### Fixed

- **`migrate.py` durability**: `migrate_origin_in_directory` was the
  only persistent-write site that bypassed
  `store._atomic_write_post`'s fsync discipline — bare `write_bytes`
  + `replace`. POSIX guarantees the rename is atomic but doesn't
  guarantee the data backing it is on disk; a power loss between
  rename and the next background flush could leave a zero-byte file
  at the target path. Now mirrors the helper's flush + fsync_file +
  rename + fsync_dir sequence.
- **Unicode tokenization**: `_TOKEN_RE = r"[a-z0-9][a-z0-9\-_]*"`
  was ASCII-only after `.lower()` reduced casing, so accented
  codepoints fell out of the character class and `tokenize("Niño café")`
  returned `['ni', 'o', 'caf']`. Any non-English memory body was
  effectively unsearchable. Switched to `\w[\w\-]*` so the codepoints
  stay whole; a query for "café" now finds a memory body about the
  café del puerto.
- **Search tiebreaker**: `hits.sort(key=lambda h: (h.score, h.created),
  reverse=True)` left ordering undefined for two memories sharing
  both score AND created timestamp — a real case under
  microsecond-tied writes and clock-mocked tests. Added `h.id` as
  the final discriminator; ULID-shaped ids sort lexically by time so
  the tiebreaker also gives "newer wins" semantics.
- **`store._as_dt` naive-string branch**: the `datetime` branch
  coerced naive values to UTC-aware before returning, but the `str`
  branch handed back whatever `datetime.fromisoformat` produced. A
  hand-edited frontmatter with a *quoted* timestamp like
  `'last_verified_at: "2025-01-01T10:00:00"'` flowed through the str
  branch as a naive datetime, then crashed
  `health.compute_health` on the first `naive < aware_cutoff`
  comparison. Both branches now coerce to UTC-aware.
- **Verify staleness boundary**: `age_days >= threshold` made a
  memory at exactly `stale_after_days` flip from fresh to stale at
  midnight UTC on the boundary day. The intuitive reading of "fresh
  for 30 days, then stale" naturally means "stale starts on day 31",
  so the comparison is now strict-greater on actual elapsed seconds.
  The `stale_after_days=0` carve-out still works because any
  measurable elapsed time satisfies `age_seconds > 0`.
- **`verify.detect_path_drift` `verified_paths` normalisation**:
  body candidates passed through `_normalize_candidate` (trims
  trailing punctuation) before the set-membership check, but
  `verified_paths` only went through `_normalize_for_compare`. So an
  attestation like `verified_paths=["/path/to/foo,"]` (trailing
  comma copied from prose) failed to match the body candidate
  `/path/to/foo` that had already been trimmed. Both sides now run
  through the same trim/validate pipeline.
- **`doctor.py` probe-write cleanup**: ENOSPC mid-write could leave
  a zero-byte `.doctor-probe` file in the user's store directory
  because the `unlink` was reached only on the success path. Moved
  to a `finally` arm with `missing_ok=True`.
- **`semantic.py` flush durability**: `flush_persistent_cache`
  renamed `.npz.tmp` into place without an explicit `fsync` and
  orphaned the `.tmp` if `np.savez_compressed` raised mid-write.
  Added `fsync_file` before close and a cleanup arm on failure;
  brings the cache flush in line with the rest of the store's
  durability discipline.

## 1.4.2 - 2026-05-13

Metadata and CI hygiene. No runtime, wire-shape, or on-disk format
changes versus 1.4.1; consumers pinned to `>=1.4.1` upgrade
transparently.

- pyproject `description` (PyPI's "summary" field) and the plugin
  manifest description now read "Persistent memory for Claude Code,
  retrieved on demand." — matching the GitHub About text. The old
  string ("Local file-backed memory MCP server with retrieval-on-demand")
  was correct but mechanical; the new line is the project's actual
  positioning.
- `tests/test_events.py::test_rotation_fsyncs_archive_after_gzip_trailer_is_flushed`
  now gates its `fcntl` block behind `sys.platform == "win32"` so
  `mypy --strict` resolves the body under POSIX stubs on Linux/macOS
  and skips it on win32. The `@pytest.mark.skipif` decorator already
  prevented runtime execution; only the type-checker pass needed the
  narrowing.
- `tests/test_config.py` `resolved_directory` tests now redirect
  `Path.home()` via a `_set_fake_home(monkeypatch, home)` helper that
  sets both `HOME` (POSIX) and `USERPROFILE` (Windows). Setting only
  `HOME` was a no-op on Windows — `ntpath.expanduser` reads
  `USERPROFILE` first — so three tests had been silently asserting
  against the runner's real home directory.

## 1.4.1 - 2026-05-13

Republish from cleaned history. No code, behavior, wire-shape, or
on-disk format changes versus 1.4.0 — pyproject `version` bumped so a
fresh PyPI artifact can be published after the project's release
history was reset. Any consumer pinned to `>=1.4.0` upgrades
transparently.

## 1.4.0 - 2026-05-13

Audit-fixes release. Internal hardening across durability, the
server-side state shape, the surface-filter callsites, and the
2972-line server module. The wire surface (17 MCP tools, names,
schemas, JSON shapes) is unchanged from 1.3.x; every public default
is preserved. Most installs will notice nothing — that's the
intended shape for a minor bump driven by infrastructure work.

The one user-facing addition: a `SessionRegistry` routing layer that
makes a single long-running server process safe to serve multiple
MCP clients (each `Context.client_id` resolves to its own
`SessionState`, so pending writes / disabled scopes / use-tokens
never leak between clients). For stdio (the primary transport,
one client per process), this collapses to a single state under a
default-bucket key — same observable behavior as before.
`build_server(state=...)` still accepts a bare `SessionState` for
back-compat with the existing single-client test surface.

### Added

- **`SessionRegistry` for multi-client server processes**
  (`src/bettermemory/session.py`). New `SessionSource` protocol;
  `SessionState` (single client) and `SessionRegistry` (multi
  client) both satisfy it. `build_server` defaults to the
  process-wide `get_default_registry()`. All 17 tool handlers
  gained `ctx: Context | None = None` and resolve their per-request
  state via `sessions.for_request(ctx)` at entry. Ten new unit +
  end-to-end isolation tests in `tests/test_session_registry.py`.
  The unbounded `_states` dict is a documented trade-off matched
  to the current stdio-primary deployment; revisit if HTTP/SSE
  becomes a supported transport.
- **`should_include_for_caller`** in `origin.py` — the canonical
  surface-filter for "this memory belongs to this caller's
  project," shared between `memory_search` and
  `memory_scope_overview`. Commit-drift callsites continue to
  use the stricter `repos_match` (no global-memory pass-through),
  documented in the helper docstring.
- New `tests/test_config.py` with 18 unit tests covering TOML
  coercion (bool/int/float/str) and the `resolved_directory`
  resolution tree (env / explicit / project-scoped / global
  fallback, plus the `~`-expansion and defensive-against-a-file
  cases).
- New `tests/test_addendum_tool_names_exist_on_server`: parses
  every `memory_*` ref out of `SYSTEM_PROMPT_ADDENDUM` and asserts
  it exists as a registered tool on `build_server()`. Catches
  doc-drift between the prompt addendum and the actual tool
  surface before it ships.
- New `tests/test_events.py::test_rotation_fsyncs_archive_after_
  gzip_trailer_is_flushed`: pins the structural shape of the
  rotation fsync (see the durability fix below).

### Changed

- **Server split** (`src/bettermemory/server.py` → `_handlers.py` +
  `_response.py`). The 2972-line `server.py` shrinks to 1014 lines
  of wiring + CLI; the 17 tool handlers move to a `ToolHandlers`
  class on `_handlers.py`, the JSON-shaping helpers move to a
  `ResponseBuilder` class on `_response.py`. Wiring is unchanged —
  every tool name, every schema, every response shape is identical
  to 1.3.x; tests reach handlers the same way (via
  `mcp._tool_manager.get_tool(name).fn`, which resolves to the
  bound method post-split). Pure refactor; the only visible delta
  is `find handlers/ -size` is now actually a useful operation.
- **Tiered git logging** (`src/bettermemory/origin.py:_git`). The
  common "not a git repository" case now logs at DEBUG instead of
  WARNING — clears the noise on installs where memories live in a
  non-repo directory. Real failure modes (missing binary, command
  timeout, OSError) stay at WARNING.

### Fixed

- **fsync on every persistent write**
  (`src/bettermemory/_fsutil.py`, `store.py`, `events.py`). Atomic
  writes were tmp-file + rename, but the actual data and the
  directory inode were left for the kernel's writeback to flush at
  its own pace. A power-loss between rename and the next writeback
  could leave a zero-length file or a missing entry in the parent
  directory. All four persistent-write paths in store.py now route
  through a single `_atomic_write_post` that fsyncs the file before
  the rename and the parent directory after; the event log fsyncs
  each append and the rotation archive. Side-fix: `rename_scope`'s
  tombstone overwrite was previously a non-atomic in-place
  truncating write — now goes through tmp+rename like the other
  persistent paths.
- **Rotation fsync runs after the gzip trailer is flushed**
  (`src/bettermemory/events.py`). The initial durability commit
  fsynced the gzip write fd from inside the `with gzip.open(...)
  as dst:` block, but `GzipFile.close()` writes the CRC32+ISIZE
  trailer at `with` exit — so the fsync race could leave a body-
  only archive that `gzip.open(...)` rejects on read. Now re-opens
  the archive read-only after the `with` block and fsyncs that
  fresh fd. Source memory files were never affected; bounded to
  archived-audit-log corruption.

### Internal

- The `_FakeCtx` duck-type in `tests/test_session_registry.py`
  picked up a `_fake_ctx` `Any`-typed helper so strict mypy
  accepts it where `for_request` expects a `Context[Any, Any, Any]`
  — clears nine pre-existing arg-type errors without changing the
  test semantics.

## 1.3.2 - 2026-05-13

Writing-policy calibration. The on-disk format, the wire surface,
every public default, and the 17-tool count are unchanged. Four
shipped strings did change content (the FastMCP `instructions`
block, the `memory_write` tool description, the plugin `SKILL.md`,
and `SYSTEM_PROMPT_ADDENDUM` with the matching fenced block in
`docs/system_prompt.md`), which is why this rides as a patch
release rather than a docs-only commit, matching the precedent
set by 1.3.1. The README was brought up to date alongside, so the
at-a-glance pitch surfaces the new write-side axis.

Pre-1.3.2 the docs carried the mechanics of writing (durability
check, dedup, confirmation tiers) but no positive triggers, no
text telling the model WHEN to write. A reading model defaulted
to not writing, producing the failure mode where a session
retrieves memory but records nothing, and the user re-explains
the same project context every chat. This release adds an
explicit, symmetric "writing is PROACTIVE" rule to every
model-facing surface, parallel to the existing "retrieval is
OPT-IN" rule. The opt-in retrieval contract is preserved verbatim;
only the write-side calibration changed.

### Changed

- **MCP `instructions` block (`src/bettermemory/server.py`).** Adds
  a "Writing is the OPPOSITE axis: PROACTIVE" paragraph with the
  four triggers (user states a preference; a project decision the
  user concurred with; a tool / infra / config fact entering the
  work; a unit of work finishing with a why git won't capture) and
  the load-bearing summary "your job is to capture". The retrieval
  paragraph was compressed to keep the full block under the
  1700-char Claude Code truncation budget (1672 chars / 1686 bytes
  post-edit, 28 chars headroom). The opt-in retrieval contract
  phrasing ("Memory is OPT-IN retrieval... Default to NOT
  retrieving.") is preserved verbatim.
- **`memory_write` tool description (`src/bettermemory/server.py`).**
  Leads with "Call this PROACTIVELY whenever something durable
  enters the conversation" and the same four triggers. The previous
  opening ("Durable facts only") moves to a second paragraph behind
  the trigger guidance so the trigger lands first on a reading
  model.
- **Plugin `SKILL.md` (`plugin/skills/bettermemory/SKILL.md`).**
  Adds a "When to write" section paralleling the existing "When to
  retrieve" section, with the same four triggers and an explanation
  of how the structural guardrails (durability, dedup, pending tier,
  scope-mismatch) make aggressive writing safe. The Quick-card
  "Write?" row leads with "proactive, something durable just
  entered the conversation, then yes."
- **`SYSTEM_PROMPT_ADDENDUM` (`src/bettermemory/prompts.py`) and the
  matching fenced block in `docs/system_prompt.md`.** Adds the
  identical PROACTIVE-writing preamble and four-trigger list at the
  top of the "Writing and updating memory" section, before the
  existing mechanics bullets. The drift test in
  `tests/test_prompts.py` continues to pin these to byte-equality.
- **`README.md`.** "What you get" gains a sibling **Proactive
  writing** bullet next to the existing **Opt-in retrieval**
  bullet so the dual-axis framing is visible on the at-a-glance
  pitch. The "Install in Claude Code" line names both policies the
  SKILL carries (previously: just "the opt-in retrieval policy"),
  and the "How the policy lands at the system-prompt level"
  paragraph names the proactive-writing rule alongside the
  retrieval contract.

### Added

- **`must_have` regression in `tests/test_server.py` for the
  writing-side calibration.** The existing instructions-block
  regression test now asserts the rendered block contains the
  load-bearing write-side phrases (`"memory_write"`, `"PROACTIVE"`,
  and `"your job is to capture"`) alongside the existing
  retrieval-side phrases. Without this, a future shorten-pass could
  silently un-do the write-side calibration the way the project's
  previous "lock writing down further" regression nearly did on the
  retrieval side.

### Fixed

- **`CHANGELOG.md` 1.3.0 heading restored.** The
  `## 1.3.0 - 2026-05-10` heading went missing in an earlier edit,
  leaving the 1.3.0 entry's body content (the `category` parameter
  on `memory_update` and the slug-builder double-date fix) visually
  merged into the 1.3.1 entry. The heading is restored above the
  orphaned "Same-day minor following 1.2.2" rationale paragraph; no
  entry body content changed.

## 1.3.1 - 2026-05-10

Documentation and prose pass. The on-disk format, the wire surface,
and every public default are unchanged. Two shipped strings did
change content (the FastMCP `instructions` block and
`SYSTEM_PROMPT_ADDENDUM` in `prompts.py`), which is why this rides
as a patch release rather than a docs-only commit. Both still pass
the byte-budget regression test and carry the same load-bearing
phrases.

### Changed

- **README, `docs/`, `plugin/README.md`, `plugin/skills/bettermemory/SKILL.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.** Rewritten
  to drop em dashes throughout and to read in shorter, more direct
  sentences. Same content, different prose. The audit pass also
  added `category="ambient"` to the listed stable enum values in
  `CONTRIBUTING.md` (it was already shipped in 1.2 but missing from
  the compatibility-contract list), the `category` parameter on
  `memory_update` and the `verified_paths` / `verified_commits` /
  `verified_versions` parameters on `memory_verify` to the README's
  tool table (additions in 1.2 and 1.3 that the previous table
  omitted), and the v1.2 / v1.3 surface entries to `docs/api.md`
  (`staleness_verdict`, auto-`record_use`, `cold_memories`,
  `curation_pending`, `acknowledge_scope_mismatch`, the structured
  `verified_*` parameters, and the additional update flow).
- **`SYSTEM_PROMPT_ADDENDUM` in `src/bettermemory/prompts.py` and
  the matching fenced block in `docs/system_prompt.md`.** Same
  policy, em dashes removed. The drift test in `tests/test_prompts.py`
  still pins these to byte-equality.
- **MCP `instructions` block in `src/bettermemory/server.py`.** One
  em dash replaced with a sentence break; final body stays well
  inside the 1700-char regression budget.
- **CHANGELOG version headings.** Switched from `## X.Y.Z — date`
  to `## X.Y.Z - date` so future entries match the project's
  no-em-dash style. Historical entry bodies were left alone (they
  are immutable release records).

## 1.3.0 - 2026-05-10

Same-day minor following 1.2.2. Two surface changes shaken out by a
maintenance audit pass: an additive `category` parameter on
`memory_update` (so legacy memories written before the `ambient`
tier can be retagged without remove+rewrite) and a slug-builder fix
for bodies that begin with their own date. Both are purely
additive on the wire — legacy clients never pass `category` to
`memory_update`, and the slug change only fires when the body's
first line starts with an ISO date.

### Added

- **`category` parameter on `memory_update`.** Joins `content`,
  `scopes`, and `confidence` as an updatable field. Accepts
  `"fact"` and `"ambient"` (the same values `memory_write` accepts
  except `"user-inference"`, which is rejected here — that
  category gates the pending-confirm WRITE flow and there is no
  equivalent gate on update). The motivating case: legacy
  memories written before 1.2.0 carry no category and read as
  `fact` for runtime semantics, but ambient-class memories
  (user-identity blurbs, persistent-environment quirks) really
  belong in the `ambient` bucket so they're excluded from the
  dead-weight curation rule. Pre-1.3 the only retag path was
  remove+rewrite, which lost the original `created` timestamp and
  littered `.tombstones/`. Category retags are metadata-only —
  `last_verified_at` is preserved across the change, the same way
  scope and confidence edits preserve it. Seven new regression
  tests in `tests/test_server.py` cover retag-to-ambient,
  retag-back-to-fact, verification-preserved, omission-preserves,
  user-inference rejected, unknown rejected, and category-only
  satisfying the at-least-one-field guard.

### Fixed

- **Slug builder no longer duplicates a leading ISO date.**
  `make_slug` in `src/bettermemory/models.py` now strips a leading
  `YYYY-MM-DD` (and the optional `Thh:mm[:ss][Z|±hh:mm]` time
  fragment) from the first line of the body before word-splitting.
  Without the strip, a body starting with "2026-05-07 tightened
  the mvp" produced a slug `2026-05-07-tightened-the-mvp` which
  `build_filename` then prefixed *again* with the memory's
  `created` date — the maintainer's own store had a real
  `2026-05-07-2026-05-07-tightened-the-mvp.md` file as evidence.
  The strip is conservative: only a leading date is touched, so a
  date in the middle of a title (`released 2026-05-07 cut`)
  survives, a bare year (`2026 retro`) is left alone, and a partial
  date (`2026-05 monthly review`) is preserved. New
  `tests/test_models_slug.py` (18 tests) covers the regression
  plus the keep-existing-behaviour cases.

## 1.2.2 - 2026-05-10

Same-day patch following 1.2.1. The path-drift extractor was firing
phantom `path_drift_missing` entries on memories whose bodies use
documentation-placeholder paths (`/etc/foo`, `/path/to/file`,
`/foo/bar`) to illustrate path-typed APIs — discovered when the
project's own overview memory documented `verified_paths` semantics
with `/etc/foo` as the example path and read back as
`staleness_verdict: "spot_check_recommended"` immediately after
being verified.

### Fixed

- **Documentation-placeholder paths no longer trigger phantom drift.**
  `_normalize_candidate` in `verify.py` now consults a small frozen
  set of canonical placeholder paths (`/etc/foo`, `/etc/bar`,
  `/etc/baz`, `/foo`, `/foo/bar`, `/foo/baz`, `/foo/bar/baz`,
  `/path/to`) plus two prefix patterns (`/path/to/...`,
  `~/path/to/...`). Candidates that match are dropped before disk
  stat, the same way URLs and SSH-style remotes are. Single-extension
  variants are also dropped — `/etc/foo.conf` reads as a placeholder
  via the `/etc/foo` entry. The list is deliberately narrow:
  terminal-component `foo` / `bar` are NOT filtered, so the
  `/tmp/foo`-shaped tmp-path test fixtures real test suites use
  remain valid path candidates. Seven new regression tests in
  `tests/test_verify.py` lock the contract — including a
  `test_dot_prefixed_real_path_not_misclassified_as_placeholder`
  test pinning that `~/.claude-memory` and similar leading-dot
  paths don't trip the extension-stripping branch.

Same-day docs-surface follow-up to 1.2.0. The v1.2.0 release added
`staleness_verdict`, auto-`record_use` via `use_token`,
`curation_pending` rollup, `category="ambient"`, `scope_mismatch`
warning, and `verified_claims` on `memory_verify`, but the doc
surfaces that ship to clients (the FastMCP `instructions` block, the
plugin `SKILL.md`) hadn't been updated to mention them. A model
installing 1.2.0 would still see the v1.1 contract and miss the
headline UX wins. 1.2.1 brings both surfaces in line.

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
  match `pyproject.toml`. No dependency changes.

## 1.2.0 - 2026-05-10

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

## 1.1.1 - 2026-05-09

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

## 1.1.0 - 2026-05-09

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

## 1.0.0 - 2026-05-08

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
