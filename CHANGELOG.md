# Changelog

Notable changes between releases. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely. From 1.0
onward the project uses semver in the standard way: major bumps for
breaking changes, minor for additive features, patch for fixes. The
[compatibility contract](CONTRIBUTING.md#versioning-and-the-compatibility-contract)
spells out exactly what's stable.

## Unreleased

**Companion escape hatch for the 2.7.3 cwd-suppression fix.** v2.7.3
stopped emitting same-repo silent-miss false positives going forward,
but the events log still carried the batch of pre-fix `search_miss` /
`turn_audited` rows that skew the miss-rate rollup. This release adds
an additive CLI cutoff so that batch can be invalidated without
rewriting the log.

### Added — Curation

- **`bettermemory consolidate --acknowledge-misses-before <ISO_TS>`**
  writes one `silent_miss_cutoff` event with `cutoff_ts=<ISO_TS>`
  through the shared `Recorder`. `compute_health` and
  `curation_counts` honor the latest `cutoff_ts` they observe and
  drop any `turn_audited` / `search_miss` events earlier than it —
  filtering both numerator and denominator so the miss-rate metric
  doesn't skew low or high. Mirrors `--acknowledge-debt`'s surface:
  always commits, no `--apply` gate (events are additive and a
  misapplied cutoff can be superseded by a later one), text and JSON
  output, validates the ISO timestamp up front so a typo surfaces as
  exit 1 instead of writing an event the rollup will silently
  ignore. `silent_miss_cutoff` is classified as a side-effect kind in
  `eval.py` so the tool-usage rollup doesn't count CLI admin
  operations as tool invocations — same rationale as `search_miss`
  and `pending_expired`.

### Fixed — Audit follow-up

- **`--acknowledge-misses-before` no longer silently stamps naive
  timestamps as UTC.** A bare ISO timestamp without an offset (e.g.
  `2026-05-25T10:00:00`) from a non-UTC user used to produce an
  off-by-zone cutoff with no warning; the CLI now rejects naive
  input and prints a clear stderr message pointing at the explicit
  offset / `Z` syntax it accepts.
- **`--acknowledge-misses-before` refuses to run with telemetry
  disabled** instead of returning exit 0 having written nothing.
  The cutoff is itself a telemetry event; a disabled `Recorder`
  swallows the write so the user thought the cutoff had landed when
  it had not. Post-write verification reads the events log back to
  catch any remaining silent-failure modes (chmod failure, I/O
  error). The CLI errors with exit 1 and a clear message in either
  case.
- **`compute_health` and `curation_counts` now use the same
  `cutoff_ts` parser** (`_ensure_utc(_parse_event_ts(...))`).
  Previously the two paths used different parsers, so a naive
  `cutoff_ts` value could produce divergent rollups against the
  same store. Event timestamps in `compute_health` are also normalized
  to UTC so any legacy naive `ts` compares cleanly against the
  aware cutoff.
- **`curation_counts(since=...)` resolves `silent_miss_cutoff`
  events before applying the delta filter.** Previously a cutoff
  event whose own `ts` fell under `since` was silently dropped,
  causing a delta run to over-count pre-cutoff misses. Cutoffs are
  global markers — they now always apply regardless of `--since`.

### Fixed — Docs & examples

- README on-disk-format example: `origin.worktree` corrected to
  `origin.worktree_root` (the actual field name the writer emits);
  example ULIDs expanded from 12 chars to the 26-char form the
  validator requires.
- `examples/memories/2025-05-10-ci-runner-migration.md` now has a
  resolvable `supersedes` target — added
  `examples/memories/2025-02-10-atlas-jenkins-ci.md` as the
  predecessor so the link demonstrates the feature it markets
  instead of dangling.
- `examples/memories/README.md`: the path-drift relationship is
  corrected (drift is computed against body-cited paths with
  `verified_paths` *excluding* matched paths from the signal, not
  "against verified_paths"); the legacy-memory claim now correctly
  states such memories surface as `spot_check_required` rather than
  `fresh`.
- `examples/programmatic_client.py`: the per-hit
  `staleness_verdict` print loop now iterates over the actual MCP
  response shape (one `TextContent` per hit) instead of the
  non-existent `{"hits": [...]}` envelope; run command updated from
  `venv/bin/python` to `uv run python`.

### Changed — Module layout

- **`build_server` extracted to `bettermemory.builder`.** `build_server`
  and `_register_tools` now live in a new `bettermemory.builder`
  module; `cli/serve.py` can import them at module top level instead
  of through the previous function-local lazy import that worked
  around the `cli ↔ server` cycle. `bettermemory.server` becomes a
  back-compat re-export shim — `from bettermemory.server import
  build_server` keeps working unchanged.
- **`bettermemory.__init__` imports from canonical homes.** Package
  init now pulls `build_server` from `.builder` and `main` from
  `.cli` directly, bypassing the `server.py` shim. Public surface
  re-exported from the package root is unchanged.

### Removed — Defensive `bettermemory.server` re-exports

- **`bettermemory.server.__all__` trimmed to its actually-used
  surface.** After verifying zero in-tree consumers, the following
  symbols were dropped from `bettermemory.server`: `_register_tools`,
  `load_config`, and three `_*_semantic*` helpers. Downstream code
  doing `from bettermemory.server import load_config` or `from
  bettermemory.server import _register_tools` will now raise
  `ImportError`; the canonical paths are `bettermemory.config.load_config`
  and `bettermemory.builder._register_tools` respectively. The shim's
  retained surface is `build_server`, `main`, `SYSTEM_PROMPT_ADDENDUM`,
  `capture_origin`, and three `_cli_*` helpers (the `_cli_*` trio kept
  because the test suite monkeypatches them at the `server.` import
  path). This is a soft API break — narrow in scope, but consumers
  pinning to the old import paths must update.

### Added — Diagnostics

- **`bettermemory doctor` detects `.dist-info` dirs missing canonical
  `METADATA`.** A new check walks `site-packages` and warns on any
  `*.dist-info` directory whose `METADATA` is absent or non-readable —
  the exact failure mode that surfaces when iCloud Drive renames
  `METADATA` to `METADATA 2` mid-sync and crashes the MCP server with
  a `-32000` pydantic validation failure. Sites scanned cover both
  `site.getsitepackages()` and (when `ENABLE_USER_SITE` is true)
  `site.getusersitepackages()`, so `pip install --user` installs are
  also covered. +4 tests for the detector.

### Added — Test-suite hygiene

- **Platform-mocked coverage for the Windows `flock` branch.**
  `_flock_windows` is now exercised from a POSIX dev box via a
  `_FakeMsvcrt` injected through `sys.modules`. Tests cover the
  retry/backoff loop under simulated contention, the `LK_UNLCK` /
  `LK_NBLCK` symmetry, and `BETTERMEMORY_FLOCK_TIMEOUT` env-var
  parsing including the invalid-string fallback. +6 tests; first
  coverage of the Windows branch outside CI.
- **Direct-import smoke tests at the package boundaries.**
  `tests/test_direct_imports.py` imports every public module under
  `handlers/` (15) and `cli/` (14) and snapshots the full parameter
  signature (name, default, and `POSITIONAL_OR_KEYWORD` kind) of each
  handler, so signature drift at the import boundary fails at
  collection time rather than masquerading as a runtime `AttributeError`
  in a downstream consumer. +30 tests.

### Fixed — Doctor dist-info detector

- **Empty `METADATA` files no longer pass the `.is_file()` check.**
  The original predicate accepted zero-byte `METADATA` even though
  the pydantic loader still rejects it; the check now requires
  `is_file() AND stat().st_size > 0` so the doctor flags the empty
  case alongside the missing-file case. A whitespace-only `METADATA`
  (e.g. `"   \n  \n"` from a partial sync or manual edit) also slips
  past size > 0 while still tripping the same downstream crash, so
  the predicate now additionally reads the first 256 bytes and
  requires the canonical `Name:` header that `importlib.metadata.
  version()` parses. +2 tests pinning the zero-byte and
  whitespace-only paths.
- **`_discover_site_packages` honours `site.ENABLE_USER_SITE`.**
  Previously only `site.getsitepackages()` was scanned, so a
  `pip install --user` install with a broken dist-info would silently
  evade the detector. The user-site path is now included when (and
  only when) `ENABLE_USER_SITE` is truthy. +2 tests covering the
  enabled and disabled branches.

### Fixed — Test rigour

- **Windows `flock` env-var fallback test proves the fallback is
  non-zero.** `test_env_var_invalid_string_falls_back_to_default`
  previously asserted only that the call returned without raising; it
  now uses `always_fail=True` + `pytest.raises(TimeoutError)` + a
  retry-count assertion to prove the default timeout actually elapsed,
  catching a "fallback silently resolves to 0" regression class the
  weaker assertion would have missed.
- **Backoff test asserts monotonic growth and the 100 ms cap.**
  `test_retries_with_backoff_until_acquired` now records every
  `time.sleep` duration and asserts the sequence is monotonically
  non-decreasing and that no single sleep exceeds the 100 ms ceiling
  the production loop enforces. Prior assertion only counted retries.

### Documentation

- Stale docstring / comment refresh across `server.py`,
  `cli/__init__.py`, `builder.py`, and `cli/export.py` — six spots
  that still described the pre-2.7.3 single-module layout were
  updated to point at the post-extract structure (canonical homes
  in `builder.py`, the shim role of `server.py`, etc.). Code paths
  unchanged.

## 2.7.3 - 2026-05-25

**Post-2.7.2 dogfood audit follow-up.** The threshold-sweep on the
2.7.x `search_miss` log surfaced that 20 of 21 replayable misses were
probes asked from inside the matching project repo ("update
bettermemory" / "push it" / "is X up to date"); the model had source
open and didn't need a `memory_search`. This release suppresses that
class. A second fix adds a CLI path to clear the related
`endorsement_debt` curation bucket without touching memory bodies.

### Fixed — Audit false-positive class

- **`audit.probe_for_miss` suppresses misses when the caller's repo
  matches a top-hit memory's `origin.repo` AND the hit carries a
  `projects:` scope.** Returns `verdict="ok"` for the suppressed case
  so no `search_miss` event is emitted; `turn_audited` still records
  the verdict. The auto-scope filter on `run_search` already covers
  most of this at the search level, but the explicit check keeps the
  suppression self-contained for offline callers (eval replays,
  curation passes) that bypass auto-scope. Both predicates are
  load-bearing — a `projects:` hit from another repo still flags
  (real cross-project miss), and a same-repo global memory still
  flags (no project boundary to suppress against). Uses `repos_match`
  for URL normalisation so SSH and HTTPS forms of the same remote
  compare equal.

### Added — Curation

- **`bettermemory consolidate --acknowledge-debt`** walks the
  `endorsement_debt` bucket (memories the ranker keeps surfacing
  where every applied event came from the auto-fallback path) and
  writes one explicit `use(applied, auto=False,
  attribution="cli_acknowledge_debt")` event per id. Retroactively
  clears the curation signal without altering bodies or scopes.
  Always commits (additive; reversible with a `corrected` follow-up
  on `memory_record_use`), goes through the shared `Recorder` so
  file locking and rotation match the other CLI write paths. Filter
  re-derived inline because `EndorsementDebt.rows` caps at 20 for
  inline display and the CLI needs every debt id.

### Deferred — Threshold-rule v5

The sweep on the live log shows v3 (dominance, 2× ratio) drops only
1 of 21 misses, and v2/v4 (score floor) drops 10 but depends on the
keyword score scale — silently breaks once the running server picks
up the hybrid default. After the cwd-suppression ships, ~1 miss
remains in the corpus — too thin to calibrate a new rule against.
Deferred to a fresh dogfood window so v5 can be designed against
post-suppression false positives.

## 2.7.2 - 2026-05-25

**Windows CI repair.** The 2.7.0 auto-memory-bridge work shipped Windows-only
regressions that only surfaced in the 2.7.1 CI run (masked by the version-sync
failures that fired first). All three fixes are test-side or pure path
normalisation; no runtime behaviour change on POSIX.

### Fixed — Windows test compatibility

- **`discover_default_source_root` normalises Windows paths.** `cwd.resolve()`
  on Windows produces backslash-separated paths with a drive-letter prefix;
  swapping to `as_posix()` + stripping the `:` from `C:/Users/...` keeps the
  sanitisation a one-liner that produces a valid filename on both platforms.
  POSIX output is unchanged (`as_posix()` is a no-op on POSIX absolute paths).
- **`test_finds_auto_memory_for_simple_cwd` and `_for_dotted_cwd` mirror the
  production normalisation.** Previously the tests computed an expected
  sanitised name using the old `str(...).lstrip("/")` form, which on Windows
  leaves a `C:\` prefix that `mkdir` rejects with `WinError 123` before the
  assertion runs.
- **`test_kind_map_parity_with_recorder_call_sites` reads source files as
  UTF-8.** The default `read_text()` uses the locale encoding (`cp1252` on
  Windows), which couldn't decode a non-ASCII byte in a bettermemory docstring.

## 2.7.1 - 2026-05-24

**Post-2.7.0 audit follow-up + concurrency test coverage.** A four-agent
production-readiness audit of the 2.7.0 branch flagged security, eval-correctness,
long-running-mode, and test-hygiene gaps; this release bundles every fix.
A review pass on top added two `SessionRegistry` concurrency tests that prove
the `threading.Lock` actually serializes contention (the prior sequential
tests only proved the `OrderedDict` mechanics) and dropped a dead
`# type: ignore` that mypy was already flagging.

### Fixed — Security & robustness

- **`ingest`: symlinks skipped before any read.** A hostile auto-memory
  directory containing `secret.md -> /etc/shadow` could otherwise smuggle the
  target's contents into a memory record. A new `skip_symlink` action surfaces
  in the rendered plan summary so the skip is auditable.
- **`audit.turn_audited_fields` / `search_miss_fields` reject unknown
  `triggered_from` values at runtime.** Mirrors the 2.6.7 search-mode pattern;
  typos now fail fast at the dispatch boundary instead of silently producing
  unsplittable eval rows.
- **`events.redact_query` strips known secret shapes before the 32-char
  preview.** Five patterns (`sk-ant-…`, `sk-…`, `ghp_…`, `github_pat_…`,
  `AKIA…`); a GitHub PAT or AWS key can no longer leak via partial-token
  capture in the truncation. `SECURITY.md` carries a threat-model note for
  the query-redaction defense-in-depth.

### Fixed — Eval correctness

- **`compute_eval` dedupes `memory_ids` within a single `record_use` event.**
  `docs/eval.md` spells out the per-id denominator semantics so consumers
  know exactly what each count represents.
- **`RateCI.torn_read` flag set when numerator > denominator.** The renderer
  emits an explicit warning line, and `to_dict` exposes the flag so CI
  consumers can branch on it (a torn read indicates log rotation raced).
- **Wilson interval pinned against numerical gold** — `(50, 100)` and
  `(1, 10)` now assert exact bounds with tight tolerance, distinguishing
  Wilson from naive Wald. The prior six structural assertions would have
  passed with either formula.

### Added — Long-running-mode preparation

- **`SessionRegistry` is now LRU-bounded under a lock.** `OrderedDict`
  backing with a `max_clients=256` cap, `threading.Lock` for atomic
  touch+insert+evict, and a `stats()` introspection surface. The stdio
  transport collapses every request into one bucket, so behaviour there is
  unchanged; the LRU and lock matter for HTTP/SSE transports that fan
  arbitrary `client_id` values through one process.
- **`_already_recorded_pending_ids` scans the event log in reverse** and
  early-exits on `ev_ts < oldest_pending_issued_at`, bounding the hot-path
  scan to the pending-token window rather than the full 10 MB active log.

### Added — Test-suite & test-env hygiene

- **`conftest.pytest_collection_modifyitems` auto-skips `no_extras` /
  `no_torch_embeddings` / `no_fastembed`** when the relevant extra IS
  installed locally, eliminating false failures on dev machines that have
  the optional dependencies present.
- **`test_consolidate` subprocess gate probes `bettermemory --help`**
  instead of relying solely on `shutil.which`, catching stale editable
  installs where the binary is on `PATH` but the import is broken.
- **New `test_kind_map_parity_with_recorder_call_sites`** AST-walks `src/`
  and asserts every `recorder.record()` kind appears in either the tool-event
  map or the side-effect set. Guards against the unmapped-footer slow-drift
  bug class — extractor limitations documented inline.
- **Two new concurrent `SessionRegistry` tests.** 32-thread same-key
  contention must return one `is`-identity state; 8×25 distinct-key insertion
  past `cap=16` must preserve `size + evicted == total_inserts`. Without
  these, removing the lock would still pass the rest of the suite — the
  regression would only surface in production under HTTP/SSE fan-out.

### Build

- mypy ✓, ruff ✓, 1387 passed / 9 skipped / 0 failed (CI baseline; dev
  machines with extras installed see 11 skipped via the new auto-skip).

## 2.7.0 - 2026-05-24

**Calibration evidence + Claude Code auto-memory bridge.** Four additions land
together because they answer questions the project has flagged as open for
several releases: *which MCP tools is the model actually reaching for?* (data
for the "trim the surface" roadmap item), *is `v1_top1_high` over-firing?*
(the calibration question `audit.py`'s docstring calls out), *how do we
prompt for curation without nagging across sessions?* (the rollup-vs-delta
gap on `memory_scope_overview`), and *how do users who already accumulated
Claude Code auto-memory upgrade?* (the bridge from
`~/.claude/projects/*/memory/` into bettermemory's audit layer).

Net effect: ~80 new tests, ~3500 lines of code + docs (≈1700 of which are
tests), zero changes to the on-disk schema, zero changes to the 18-tool MCP
surface. Existing memories load and search unchanged.

### Added — `bettermemory eval --tool-usage`

- **Per-MCP-tool call-count rollup from the event log.** One row per known
  tool with absolute counts, share of total, and a bar visualisation. Tools
  without a dedicated event (today: `memory_health`) surface with a
  zero count and a "no telemetry" annotation rather than being silently
  dropped — distinguishes "this tool is not counted" from "this tool was
  never called." A new map (`eval._TOOL_EVENT_KIND_TO_TOOL`) collapses the
  per-tool event-kind variants (`write` can land with `status="ok"`,
  `"pending"`, `"duplicate"`, etc., but it's still one tool call;
  `memory_audit_turn` always emits `turn_audited` and *optionally* a
  `search_miss` side-effect — counting raw kinds would double-count it).
  Honours `--since` and `--json`; ignores the rate-mode knobs.
- **Unmapped-event-kind footer.** A future contributor who adds a new MCP
  tool without updating the map will see the unmapped kind surface in the
  output's footer rather than have its calls vanish silently. Guardrail
  against map drift over time.

### Added — `bettermemory eval --threshold-sweep`

- **Counterfactual replay of logged `search_miss` events under alternative
  threshold rules.** Closes the calibration question
  `audit.py`'s docstring flags as open. Bundled rules (all stricter than v1
  so the comparison is well-defined):

  - `v1_top1_high` — current default (reference).
  - `v2_top1_high_score_50` — v1 + top-1 score >= 50. Filters single-token
    high-coverage hits.
  - `v3_top1_high_dominant` — v1 + top-1 score >= 2× top-2 score. Distinguishes
    obvious match from borderline tie.
  - `v4_top1_high_strict_combined` — intersection of v2 and v3.

  On the maintainer's dogfood log (~14 memories, 12 replayable misses since
  2.6.4), v2 would halve the v1 miss count — direct evidence the score
  floor is a defensible tightening. Adding a new rule is two lines (a checker
  function + a `ThresholdRule` entry in `eval.THRESHOLD_RULES`).
- **Honest about its limitation.** The sweep is *relative*: strictly-looser
  rules can't be evaluated from the log alone because the companion
  `turn_audited` event doesn't carry `top_hits`. The caveat is in the
  text rendering, in the docs, and in the module docstring. Going further
  would mean adding `top_hits` to every `turn_audited` event, which bloats
  the log meaningfully — kept as a deliberate trade-off, not a roadmap commitment.
- **Pre-2.6.4 event compatibility.** Legacy hook-originated `search_miss`
  events that carry only `top_hit_ids` (no relevance label) can't be
  replayed; they surface in `skipped_legacy_event_count` so the
  `replayable_misses` denominator stays honest.

### Added — `bettermemory ingest`

- **Bridge from Claude Code auto-memory.** New CLI subcommand walks
  `~/.claude/projects/<sanitized-cwd>/memory/` (or any path passed via
  `--from`), parses the auto-memory format (frontmatter `name`,
  `description`, both nested `metadata.type` and flat top-level `type:`
  shapes the auto-memory feature has emitted across versions, plus body),
  maps the type to a bettermemory category, dedups against the active
  store and tombstone log via the existing `find_similar` /
  `find_similar_tombstones` Jaccard pass, and writes survivors as
  ordinary records carrying an `imported-from-claude-code` provenance
  scope plus a type-derived second scope (`feedback`, `project-context`,
  `user-inferences`, `reference`). Auto-discovery's path sanitiser
  replaces both `/` and `.` with `-` to match Claude Code's on-disk
  layout — so a worktree at `~/projects/foo/.claude/worktrees/bar`
  resolves correctly rather than silently missing.
- **Category mapping.** `user` → `Category.USER_INFERENCE`, `feedback` →
  `Category.FACT`, `project` → `Category.FACT`, `reference` →
  `Category.AMBIENT`, anything else / missing → `Category.FACT`. The MCP
  write handler's always-pending gate for `user-inference` does NOT
  apply to ingest — an ingest run is the user telling bettermemory "these
  pre-existing user-curated files are mine, ingest them" and routing each
  one through pending-confirm would be ergonomic theatre. The category
  still lands on the record so downstream curation treats them as
  user-claim memories.
- **No source-file mutation.** Considered and rejected — modifying the
  source `.md` files would race Claude Code's own auto-memory writes, and
  the dedup gate already makes re-ingestion safe (matching content
  Jaccards at 1.0 and trips the high-similarity threshold). Re-running
  ingest on an already-ingested source produces the expected
  `skip_duplicate` rows.
- **Plugin SKILL.md banner loosened.** The pre-2.7.0 banner read
  *"Do not fragment memory across ad-hoc files alongside …
  `~/.claude/projects/*/memory/` …"* — implicitly framing the auto-memory
  feature as adversarial. The new banner names the auto-memory path
  specifically and points to the ingest CLI, flipping the framing from
  "fight" to "consume rather than fight."

### Added — `memory_scope_overview` delta field

- **`curation_pending_new_since_last_session`.** Sibling to the existing
  absolute `curation_pending` dict. Same key shape (`stale`,
  `never_verified`, `drifted`, `cold`, `dead`, `silent_misses`,
  `endorsement_debt`) but counted only against events emitted and
  memories created after the latest event from a session other than
  the current one. The field is `null` when no prior session exists in
  the event log (first session ever, or after a wipe) — the model branches
  on null vs. dict to tell "no baseline" apart from "nothing new since
  baseline." The tool description tells the model: prompt about curation
  based on the *delta*, surface the *absolute* on demand.
- **`find_prior_session_boundary` helper.** Pure function in `health.py`
  that walks the event stream once and returns the max ts among events
  whose `session` field differs from the caller's current `session_id`.
  Accepts both the canonical `session` and the legacy `session_id` field
  names so pre-unification archives still resolve to a usable boundary.
  Materialisation in the handler is intentional — the handler runs three
  passes over the same in-memory event list (absolute rollup, boundary
  helper, delta rollup); the events list is bounded by the active log
  + rotated archives (same scale `compute_health` already pays at
  session-start), and re-walking the iterator three times would do
  three times the I/O for the same result.
- **`curation_counts` gains a `since` parameter.** When set, filters
  events to `ts > since` and memories to `created > since` (the
  boundary value IS the prior session's last event timestamp, so it
  belongs to the prior session, not the delta). The same helper
  produces both the absolute and delta views from the handler — no
  parallel implementation to drift.

### Changed — plugin SKILL.md banner

- The pre-2.7.0 anti-fragmentation banner named
  `~/.claude/projects/*/memory/` as forbidden alongside ad-hoc files
  like `MEMORY.md`. The new banner singles that path out specifically as
  *"ingest it once if it exists"* and links to `bettermemory ingest`.
  Lets users who came to bettermemory after months of auto-memory
  accumulation upgrade cleanly. The `MEMORY.md` / scratch-markdown
  proscription is preserved verbatim.

### Pre-tag audit fixes (folded in)

A second-pass audit of the 2.7.0 surface (four parallel fresh-eyes
agents) caught several correctness gaps in the new features.
Addressed before the tag rather than as a 2.7.1:

- **`memory_scope_overview` delta is correct under SessionRegistry.**
  The handler was passing `state.session_id` to
  `find_prior_session_boundary`, but every event the recorder writes
  carries the recorder's process-lifetime `session_id` (a different
  value when SessionRegistry is in use). In multi-client mode that
  collapsed the delta to ~empty because the handler treated every
  recorded event as "from another session." The handler now passes
  `self.recorder.session_id`. Regression test:
  `test_scope_overview_delta_uses_recorder_session_not_state`.
- **`curation_counts(since=…)` boundary is exclusive.** The filter was
  strict `<` (`ev_ts < since` skipped), which meant the prior session's
  last event itself slipped into the delta. Now `<=` for both event
  timestamp and memory `created`. The CHANGELOG promise of "events
  emitted and memories created since the previous session ended"
  required the boundary value to be exclusive (it IS the prior
  session's last event ts). Lock-in tests:
  `test_curation_counts_since_filter_is_exclusive_at_boundary`,
  `test_curation_counts_since_excludes_old_memory_aging_into_stale`.
- **`memory_list` events count as retrievals in `compute_eval`.**
  `audit.py:88` treats `{"search","show","list"}` as the retrieval set;
  `compute_eval` was only counting `search` + `show`, narrowing the
  `retrieval_occurrences` denominator vs. the audit cadence and
  distorting `memory_helped_rate` downward for workflows that lean on
  `memory_list`. Lock-in test:
  `TestComputeEvalListKind.test_list_event_counts_as_retrieval`.
- **`v1_drift` surfaces in `compute_threshold_sweep`.** The previous
  docstring promised v1's replay must equal `replayable_misses` but
  the production helper never raised on mismatch — only a 3-event
  synthetic test enforced it. New `v1_drift` field on
  `ThresholdSweepReport` carries `replayable_misses - v1_would_flag`;
  the text renderer surfaces a warning line when non-zero.
- **`_parse_ts` returns tz-aware on naive ISO input.** The recorder
  always writes `Z`-suffixed timestamps, but external producers or
  older binaries could emit naive ISO strings. `_parse_ts` was
  returning naive datetimes for those, and the downstream `<` against
  the tz-aware cutoff would raise `TypeError` mid-iteration. Naive
  inputs are now stamped as UTC.
- **`recent_retrieval_count` excludes `bool`.** `isinstance(True, int)`
  is True in Python; a stray `True` / `False` in the field would
  silently count as 1 / 0. Bools are now guarded out at both
  `_silent_miss_from_event` and the threshold-sweep replay.
- **`bettermemory ingest --force` for parity with `memory_write`.** The
  active-store dedup gate can be bypassed for the rare case of a
  legitimately-near auto-memory record. Tombstone dedup remains in
  force — re-importing a deliberately-removed memory stays disallowed.
- **`apply_ingest_plan` no longer swallows `MemoryError` per row.**
  The bare `except Exception` would retry-and-eat disk-full / OOM
  errors on every subsequent row. Narrowed to `(ValueError, OSError)`
  so hard system failures propagate instead of being relabeled as
  per-row `skip_invalid`.
- **`_TYPE_TO_CATEGORY` / `_TYPE_TO_EXTRA_SCOPE` key invariant.** A
  module-import-time assert pins the two maps to the same key set,
  catching typos that would otherwise silently downgrade ingest
  behaviour (missing extra-scope loses the type-derived scope;
  missing category falls back to `FACT`).
- **`IngestPlan.summary` zero-init is driven by `_ACTIONS`.** The
  hardcoded zero-init list silently dropped any future `Action`
  literal a contributor added. Now derived from the single `_ACTIONS`
  tuple.
- **Nested-vs-flat `type:` precedence is documented and tested.** Both
  shapes ship in real auto-memory directories. Precedence: nested
  wins on conflict. Lock-in test: `test_nested_type_wins_when_both_present`.
- **`discover_default_source_root` positive tests.** The 2.7.0 audit
  added dot-replacement to the path sanitiser; until now no positive
  test exercised the sanitiser at all, so a refactor that reverted to
  slash-only behaviour would pass the negative test silently. Tests
  added for both the simple and `.claude/worktrees/*`-style dotted
  cases.
- **Tone polish.** `DESC_MEMORY_SCOPE_OVERVIEW` "drifting into stale"
  changed to "aging into stale" to disambiguate from the separate
  `drifted` bucket. Tool-usage footer wording acknowledges side-effect
  event kinds. `_humanize_seconds` no longer prints "1 day" for 1d
  while emitting "30d" elsewhere. `docs/eval.md` example block now
  matches actual `render_text` output.

## 2.6.8 - 2026-05-24

**External audit follow-up.** A four-agent fresh-eyes audit of 2.6.7
found one ranker default that underperformed by design, three event-log
and session-state correctness issues the dogfood ~14-memory scale would
never hit, one privacy-by-default gap, and one README over-promise.
Every finding is fixed in this release. The two findings the audit
flagged but the maintainer kept out of scope — empirical recalibration
of the 30-day staleness window and the 0.3 semantic threshold against
a real dataset — are tracked for the next minor; both are observability
questions the eval CLI was designed to answer once enough turns of
dogfood traffic exist.

### Changed — default `search_mode` is now `hybrid`

- **`behavior.search_mode` defaults to `"hybrid"` (was `"keyword"`).**
  Hybrid runs RRF over keyword + BM25 (and semantic when the
  `[embeddings]` extra is installed), gracefully degrading to
  keyword + BM25 fusion when no embedding extra is present — so the
  flip doesn't add a dep requirement. The legacy keyword scorer
  lacks IDF weighting and underperforms on rare-term queries. The
  1.6.0 default is still selectable explicitly via `mode="keyword"`
  for byte-stable behaviour on identifier-heavy queries. `docs/api.md`,
  the `DEFAULT_CONFIG` template, and `BehaviorConfig.search_mode`
  all carry matching comments now.

### Fixed — HIGH: event log rotation could double-count on crash

- **`events._rotate_if_needed` had a crash window between gzip-close
  and source unlink that left both files present.** Recovery via
  `iter_all_events` then yielded every rotated event twice — and the
  eval framework's `silent_miss_rate` / `endorsement_rate` numerators
  are computed off that stream, so a single crashed rotation would
  inflate the denominators for the lifetime of the active log. Fixed
  with a `.rotating` two-phase rename: the active log is atomically
  renamed to `.events-{ts}.jsonl.rotating` *first*, then compressed
  into a `.jsonl.gz.tmp` sibling, then renamed atomically to the
  canonical `.gz`, then the `.rotating` holding file is unlinked. A
  crash at any step is recoverable on the next rotation via a sweep
  at the top of `_rotate_if_needed`. Archives inherit the active-log's
  `0o600` permissions via an explicit `chmod` before the canonical
  rename. `iter_all_events` reads orphan `.rotating` files only when
  no matching `.gz` exists.

### Fixed — HIGH: silent pending-write expiry

- **`SessionState._evict_expired` dropped pending writes silently.**
  A user saying "yes, save it" 61+ minutes after the prompt got back
  `no pending write with id ...` — the eviction was indistinguishable
  from a typo and left no event-log trail. Two related changes:
  - `_advance_turn` now calls `state._evict_expired()` and any drop
    populates `_expired_pending`, which `_drain_pending_expired`
    consumes to emit one `pending_expired` event per drop (carrying
    the `pending_id`, the `ttl_seconds` that elapsed, and the
    proposed-memory `category` so downstream curation can flag lost
    `user-inference` confirmations specifically).
  - `memory_write_confirm` now consults
    `SessionState.was_recently_expired(pending_id)` and raises a
    targeted error — `"pending write {pid!r} expired before
    confirmation (the 1-hour TTL elapsed). The proposed memory was
    not saved. Re-stage with memory_write to create a fresh pending
    id."` — so the model knows whether to apologise-and-re-ask or
    debug a phantom id.

### Fixed — HIGH: auto-`record_use` race when log events landed after the scan

- **`_advance_turn`'s pre-consume dedup scanned only
  `attribution="hook"` events and matched on `(session, memory_id)`
  alone.** Two failure modes followed:
  - A model that did `memory_search` → `memory_record_use(applied)`
    → `memory_search` (same id) had its *fresh* second token
    falsely purged by the *stale* first record_use event, dropping
    the auto-commit cadence on a legitimate new retrieval.
  - Any non-hook attribution that landed in the log after the search
    but before the auto-fire could produce two `use` events for the
    same `(turn, memory_id)` pair.
  Replaced with `_already_recorded_pending_ids`, which: (a) covers
  any `use` event regardless of `attribution` tier, and (b) gates
  on `event.ts >= token.issued_at` so a stale event from a prior
  retrieval cycle no longer purges a fresh token. The hook-only
  function name is kept as a backwards-compat alias.

### Fixed — MED: search query text logged verbatim to disk by default

- **`Recorder.record` wrote `query` / `probe_query` field values
  verbatim.** A user pasting `key=sk-very-secret-...` into a
  `memory_search` landed the full secret on disk. Two changes:
  - New `telemetry.log_queries_verbatim` flag (default `false` since
    2.6.8) replaces the field with `{"hash": "<16-hex sha256
    prefix>", "preview": "<first 32 chars>", "len": N}` before the
    event is serialised. Cross-event correlation still works (a
    repeated query has the same hash); the first 32 characters
    survive for triage; the full body is not recoverable.
  - Rotated archives now match the active log's `0o600` permissions
    (was umask-default — defense-in-depth so the chmod miss on the
    archive doesn't undo the active-log permission story).
  Set `telemetry.log_queries_verbatim = true` to restore the legacy
  shape for ranker debugging.

### Fixed — LOW: README over-promised "every use is logged with claim-level excerpt"

- The phrasing implied every retrieval landed in the log with an
  excerpt. In practice only the *model-explicit*
  (`memory_record_use(claim_excerpts=…)`) and *hook-attributed*
  (Stop hook substring match) tiers carry excerpts; the
  `attribution="auto"` fallback for retrievals neither path covers
  has no excerpt and is excluded from `memory_helped_rate`'s
  numerator. README and the "Claim-level audit trail" bullet now
  spell out the three tiers and which one carries excerpts.

## 2.6.7 - 2026-05-23

**Post-2.6.6 audit follow-up.** A six-agent meta-audit of the 2.6.6
release found two HIGH-severity contract drifts (one doc, one
test), eight MEDIUM items spanning correctness / concurrency /
contract / release-hygiene, and three LOW hardening items. Two
agents flagged the same `consolidate.py:407` legacy-fallback gap
independently. Every finding worth fixing in code is in this
release; the LOWs explicitly out of scope (web UI defense-in-depth
headers, `init.py` atomic write, sub-microsecond chmod window on
freshly-created index/embedding files) are squarely inside the
documented same-machine trust model.

### Fixed — HIGH: contract drift between handler and docs

- **`docs/api.md` memory_write success status said `"ok"`,
  emitted value is `"committed"`.** The exact drift CHANGELOG
  2.6.2 announced as fixed in `DESC_MEMORY_WRITE` — but
  `docs/api.md` (the file `CONTRIBUTING.md:44` calls "pinned"
  as the stability contract) was never updated in lockstep.
  A library author or programmatic client branching on the
  documented value got a no-match against every successful
  write. Fixed to `"committed"` matching the handler description
  and `_response.py:277`.

### Fixed — HIGH: regression test that didn't exercise the production path

- **`test_find_similar_dispatches_to_jaccard_without_model`
  asserted `len(hits) >= 0` — tautology.** The docstring
  promised "two bodies that share no tokens shouldn't surface
  via Jaccard," but the test bodies shared the token `postgres`,
  the inline comment contradicted the docstring, and the
  assertion was a tautology. Same shape as the 2.6.6
  `test_schema_rebuild_executescript_is_transactional` rewrite —
  a test named for a production branch it never exercised.
  Rewritten with a positive case (shared distinctive tokens →
  Jaccard hit at similarity > 0.40) plus a negative case
  (disjoint token sets → no hit, no exception, no hidden
  semantic-fallback path) so the dispatch boundary is pinned.

### Fixed — MEDIUM: incomplete generalisation (two agents flagged independently)

- **`consolidate.py:407` `find_demotion_candidates` missed the
  `memory_ids` legacy fallback.** Read `event.get("returned") or
  event.get("hit_ids") or []`, missing the canonical-first /
  legacy-second / oldest-third chain every other call site
  uses (`eval.py:361`, `hook.py:365-367`, `health.py:699`,
  `health.py:1423`). Two of the six audit agents flagged this
  independently. Same pattern as the 2.6.6 `health.py:679` fix
  — the 2.6.5 sweep missed two sites, not one. Fix: insert
  `memory_ids` between `returned` and `hit_ids`.

### Fixed — MEDIUM: telemetry false positives

- **`audit.py` `_RETRIEVAL_EVENT_KINDS` whitelist excluded
  `list` events.** `memory_list(scopes=[…])` returns ids (and
  bodies when `with_bodies=True`) and logs `kind="list",
  returned=[…]` — same retrieval semantics as `search` and
  `show`, but the silent-miss probe only counted the first two.
  A model using `memory_list` to triage would be flagged for a
  silent miss even though it had the content in front of it.
  Fix: add `list` to the frozenset.

### Fixed — MEDIUM: dormant concurrency TOCTOU

- **`SessionRegistry.for_request` had a read-then-write race
  on shared mutable state with no lock.** Two concurrent
  callers observing the same missing `client_id` both create
  fresh `SessionState` instances and the second `__setitem__`
  wipes the first writer's `pending_writes` / `disabled_scopes`
  / `turn_counter`. Stdio collapses every request into a single
  default-client key so the race is dormant today, but the
  class docstring anticipates an HTTP/SSE transport that fans
  distinct `client_id`s in parallel — at which point the race
  becomes live and silent. Fix: `dict.setdefault` is atomic on
  CPython, so concurrent callers observing a missing key all
  receive the same `SessionState` instance.

### Fixed — MEDIUM: tool description omitted a returned bucket

- **`DESC_MEMORY_HEALTH` listed `dead_weight` but never
  mentioned `cold_memories`.** Description claimed
  `dead_weight = created > window_days AND never applied`.
  Code additionally requires `retrieval_count > 0`;
  never-retrieved memories route to the separate `cold_memories`
  bucket the description never named. A model curating against
  `dead_weight` would miss the cold subset entirely. Fix:
  mirror the `docs/api.md` framing — dead is *"retrieved but
  didn't help"*, cold is *"the ranker isn't surfacing this at
  all"*.

### Fixed — MEDIUM: source enum under-documented

- **`docs/api.md` listed only `"explicit-statement"` and
  `"inferred"` for `memory_write.source`.** `models.py` defines
  a third value `"user-correction"` that the handler accepts
  and that `examples/memories/2025-04-15-projects-foo-stack.md`
  actively uses. The doc is the contract; the validator was
  wider than the contract. Fix: include `"user-correction"`
  with prose covering the post-hoc-correction semantics.

### Fixed — MEDIUM: ROADMAP version stale

- **`docs/ROADMAP.md` "Where we are" pinned at v2.6.3.**
  CHANGELOG 2.6.3 flagged "this count rots fast" but no test
  pins the version, so the same drift recurred immediately
  through 2.6.4 / 2.6.5 / 2.6.6. Fix: bump to v2.6.6. (A
  sync-guard test would close this structurally; out of scope
  for this round.)

### Fixed — MEDIUM: dispatch path defense-in-depth + test

- **`search.search()` had no runtime mode validator.** The
  `SearchMode` Literal pinned modes at the type-checker layer
  but Python doesn't enforce Literals at call time, so any
  unknown string from a future programmatic caller would fall
  through the if/elif chain into the `else` branch and silently
  run hybrid. Fix: runtime validator at the dispatch boundary;
  unknown modes raise `ValueError` with the closed-set message.
  `test_mode_invalid_returns_typed_error` was the test that
  named the missing validator ("we can't easily check this at
  runtime") — now actually exercises the production rejection.

### Fixed — MEDIUM: weak structural pin on cold_memories

- **`test_cold_memories_field_returned_by_health` only asserted
  `isinstance(res["cold_memories"], list)`.** A regression that
  always returned `[]` would pass. Rewritten to drive the
  routing predicate end-to-end (write a fact memory, call
  `memory_health(window_days=0)`, assert the id lands in
  `cold_memories` AND NOT in `dead_weight`). Misroutes between
  the two buckets now fail this test.

### Fixed — MEDIUM: broad pytest.raises pattern

- **30 sites used `pytest.raises(Exception)` without `match=`**
  across test_server.py, test_server_record_use.py,
  test_rename_scope.py, test_server_v12_features.py,
  test_session_registry.py, test_audit.py,
  test_server_tombstones.py, test_server_links.py. Bare
  `Exception` catches any error type with any message — a
  refactor that swapped a clean `ValueError` for
  `AttributeError: 'NoneType'` would keep tests green while
  users see uninformative tracebacks at the MCP boundary. Fix:
  every site now pins the actual error-message substring via
  `match="…"`, following the pattern test_server.py:205
  established. Where two validation layers can fire (pydantic
  vs. handler `isinstance` checks), the regex covers both so a
  rearrangement of validation order doesn't fail tests for the
  wrong reason.

### Fixed — LOW: discipline alignment

- **`auto` discriminator strictness drift** — `health.py:736`
  used `is True`, `eval.py:387` used `bool(...)`. Production
  traffic only writes literal True so no current bug, but
  identical structural class to the session-id sweep
  2.6.5/2.6.6 already addressed. Fix: aligned `eval.py:387` to
  `ev.get("auto") is True`.

- **`events.py:_archive_sort_key` mis-parsed session_ids with
  internal dashes** (Claude Code session_id is a full UUID).
  Naive `inner.split("-")[-1]` fell into the numeric-tail
  branch for UUIDs whose last hex chunk happened to be all
  digits, producing a wrong sort key. Most consumers use the
  embedded `ts` so impact is small, but the parser was broken.
  Fix: regex anchored to end-of-string for the `-N` counter
  suffix.

### Fixed — LOW: per-tool description wording

- **`DESC_MEMORY_SEARCH`** now explicitly notes that memories
  with no recorded origin pass `auto_scope=True` as global —
  mirrors the sibling `DESC_MEMORY_SCOPE_OVERVIEW` wording.

- **`DESC_MEMORY_RECORD_USE`** now documents the empty-string
  rejection on `claim_excerpts` (handler raises ValueError
  pointing the caller at `None` for "no specific claim").

## 2.6.6 - 2026-05-23

**Post-2.6.5 audit follow-up.** A four-agent meta-audit of the 2.6.5
"post-2.6.4 audit follow-up" release found two items: one
pattern-discipline gap that 2.6.5 swept everywhere else but missed,
and one regression test that pinned a stdlib property instead of
the production call site it was named after.

### Fixed — incomplete generalisation

- **`health.py` distinct-session aggregation read canonical-only.**
  The 2.6.5 sweep applied the canonical-first / legacy-second
  fallback to five other `health.py` event reads but missed line
  679's `sess = ev.get("session")`. The Recorder stamps `session`
  on most canonical events, but `turn_audited` / `search_miss` use
  `session_id` as their canonical field — under-counting the
  distinct-session metric in `compute_health`'s rollup whenever
  those event kinds were the only events in a session. Fix:
  `ev.get("session") or ev.get("session_id")`, matching the
  pattern applied at the four other `health.py` sites.

### Fixed — regression test that didn't exercise the production path

- **`test_schema_rebuild_executescript_is_transactional` pinned a
  stdlib property, not the production call site.** The 2.6.5 test
  opened a raw `sqlite3.Connection`, hand-rolled the `BEGIN
  IMMEDIATE … COMMIT`-embedded executescript pattern, and asserted
  SQLite genuinely wraps. That verifies the property the fix
  depends on, but a regression in `_ensure_schema` itself (e.g.,
  reverting to the 2.6.4 shape: `conn.execute("BEGIN IMMEDIATE")`
  then a separate `executescript`) would still pass the test —
  the production code path isn't called. Rewrite: sets up a v1
  index with a row, injects a broken `_SCHEMA` via monkeypatch,
  calls `index._ensure_schema` directly, asserts the row survives.
  The 2.6.4 buggy shape would commit the DROP in autocommit mode
  before the failing CREATE, losing the row; the 2.6.5 fix
  preserves it.

## 2.6.5 - 2026-05-23

**Post-2.6.4 audit follow-up.** A six-agent meta-audit of the 2.6.4
"structural audit" release found several claims that didn't hold and
one regression the release itself introduced. This release addresses
every HIGH and MEDIUM finding plus the LOW/NIT items with clean
fixes.

### Fixed — live bugs in 2.6.4

- **`bounded_read` flattened `FileNotFoundError` to bare `OSError`.**
  The 2.6.4 catch-and-re-raise turned `Store.restore` and
  `Store.rename_scope`'s `except FileNotFoundError` handlers into
  dead code AND made `test_concurrency`'s stress test flaky (~20%
  under contention) — a regression introduced by the audit release
  itself. Fix: drop the re-wrap; `path.stat()` raises its native
  subclass unchanged.

- **`index._ensure_schema` `BEGIN IMMEDIATE` wrapped nothing.**
  Python's `sqlite3.executescript()` implicitly commits any pending
  transaction before it runs, so 2.6.4's
  `conn.execute("BEGIN IMMEDIATE")` followed by two `executescript()`
  calls left the DROP/CREATE unprotected — a concurrent reader could
  still see "no such table: memories" mid-rebuild, exactly the gap
  the 2.6.4 fix claimed to close. Fix: move `BEGIN`/`COMMIT` inside
  the executescript string. Verified atomic on CPython 3.11–3.13.

- **`consolidate` merge-rollback orphaned tombstoned duplicates.**
  In a 3+-member cluster, if dup A tombstoned but dup B failed,
  2.6.4's rollback restored the keeper but left A's content in
  *neither* the keeper (rolled back, never received the merge) nor
  the active set (tombstoned) — silent data loss until a manual
  `memory_restore`. Fix: track successfully-tombstoned ids and
  `store.restore()` them on failure.

- **`_frontmatter.load` silently masked UTF-8 corruption.** 2.6.4
  swapped `read_text(encoding="utf-8")` (raises on invalid UTF-8)
  for `decode("utf-8", errors="replace")` (silent U+FFFD
  substitution). A corrupt memory file then loaded into the
  retrieval surface, `doctor` reported it clean, and the next
  mutator rewrote the file — laundering the corruption permanently.
  Fix: strict decode, raise `ValueError` so the store's
  malformed-file skip path fires (the pre-2.6.4 contract).

- **`health.py` (×4) and `eval.py` (×2) missed the legacy-name
  fallback.** 2.6.4 applied the canonical-first / legacy-second
  discipline to five consumers but skipped the core curation engine
  (`compute_health`, `curation_counts`) and the silent-miss eval
  renderer. Same discipline applied.

- **`sync.py` push/pull lock did NOT coordinate with `Store.write`**
  as the 2.6.4 CHANGELOG claimed. `.sync.lock` is a different inode
  from per-memory `<id>.md.lock`, so `flock` never serialized them.
  The lock genuinely serializes sync-vs-sync (push-vs-push,
  push-vs-pull). 2.6.4 CHANGELOG and in-code comments corrected.
  True sync↔Store coordination would require global write
  serialization and is left as a deliberate future decision.

### Fixed — incomplete generalisations

- **Shared `turn_audited` / `search_miss` field builders**
  (`audit.turn_audited_fields`, `audit.search_miss_fields`). The
  2.6.4 audit found the Stop hook and the in-process MCP handler
  emitting these events with hand-copied kwarg lists that had
  *already* drifted (`triggered_from` on one, absent on the other).
  Both producers now route through the shared builders, so they
  cannot drift again. Handler tags `triggered_from="mcp_tool"`;
  `search_miss` carries `recent_retrieval_count` so
  `eval._silent_miss_from_event`'s column stops being permanently
  blank.

- **`semantic` stale-dimension cache crashed `memory_write`.**
  `cosine_similarity_normalized` with `zip(strict=True)` (added in
  2.6.4) raises `ValueError` on mismatched-dimension vectors —
  uncaught on the `memory_write` → `find_similar` path. When a
  persistent embedding cache was written under one model checkpoint
  and hydrated under another (same `model_name`, different output
  dimension), every comparison raised and the whole handler failed.
  New `semantic._note_model_dimension` learns the live dimension
  from encodes the callers already do (no probe encode) and purges
  stale entries; `find_similar` / `_search` /
  `find_similar_tombstones` prime it from their query encode;
  `_find_dedup_semantic` gets a defensive `except ValueError`.

- **`bounded_tail_read` hung on writer-less FIFO** via
  `consolidate._load_transcript`. Pointing
  `consolidate --llm --from-transcript` at a FIFO would block
  `open("rb")` forever. Added `is_file()` guard mirroring the hook's;
  corrected `bounded_tail_read`'s docstring FIFO language.

### Fixed — LOW / NIT

- `audit._count_recent_retrievals` — added the canonical-first
  session fallback the 2.6.4 release applied everywhere else but
  missed on the silent-miss probe's own hot path.
- `_handlers.py` comment falsely claimed "canonical handler writes
  both `session` and `session_id`" — corrected (the Recorder always
  stamps `session`, never `session_id`; only events whose producer
  passes `session_id=` explicitly carry both).
- `tests/test_event_helpers.py` "contract" test emitted
  `claim_excerpts` / `lookback_seconds` / `probe_query` / `query`
  without asserting them — a producer-side rename would have slipped
  through the very test it was supposed to pin. Assertions added.
- `tests/test_changelog.py` — added `encoding="utf-8"` to the
  `plugin.json` / `marketplace.json` reads (the same anti-pattern
  the 2.6.4 CI fix corrected for `CHANGELOG.md` in the same file).
- CHANGELOG 2.6.4 entry corrections: `hook.py:_run_audit` →
  `run_audit` (function name); "Six other consumers" → "Five"
  (matches the parenthetical); the false sync-vs-Store coordination
  claim corrected as noted above.
- `llm.py` Ollama-truncation comment described an unimplemented
  "empty `done` flag (older)" branch — tightened to what the code
  actually checks.

### Added — regression tests

- `test_fsutil.test_missing_file_raises_filenotfounderror` pins the
  `OSError` subclass contract (the test that should have caught the
  2.6.4 flattening regression but only asserted `OSError`).
- `test_frontmatter.test_load_rejects_invalid_utf8` pins the
  strict-decode contract.
- `test_index.test_schema_rebuild_executescript_is_transactional`
  pins the `executescript`-with-embedded-`BEGIN` atomicity property
  the fix relies on.
- `test_consolidate_llm.test_merge_rollback_restores_earlier_tombstoned_duplicates`
  covers the multi-duplicate rollback.
- `test_consolidate_llm.test_load_transcript_does_not_hang_on_fifo`
  — daemon-thread regression that pins the FIFO guard without
  hanging the suite on regression.
- `test_audit.test_event_field_builders_pin_canonical_shape` pins
  the shared builders' output, including the two 2.6.4-audit gaps
  (`triggered_from` and `recent_retrieval_count` on `search_miss`).
- `test_semantic.test_stale_dimension_cache_entries_are_purged`
  pins the dimension reconcile.

## 2.6.4 - 2026-05-21

**Fourth audit pass over the 2.6.x surface, this one structural.** The
prior three audits found instances of named bug classes; each fix
landed in one location while the same pattern lived elsewhere
unchecked. This release inverts the discipline: instead of finding
more instances, it makes whole classes structurally impossible. Six
parallel agent audits hunted (1) bounds enforcement, (2) field-name
drift, (3) cross-process concurrency primitives, (4) pattern non-
generalization from prior fixes, (5) test-fixture honesty, and (6)
novel bug classes the first three passes missed. The big find: the
2.1.0 silent-miss telemetry flagship is partially broken for hook-
originated events — three audits walked past it.

### Added — structural foundations

- **`_fsutil.bounded_read` / `bounded_tail_read` / `bounded_stream_read`.**
  Single point of enforcement for resource caps on input. The 2.6.2
  and 2.6.3 releases fixed three separate unbounded-read defects (the
  consolidate transcript, the hook transcript, the byte-vs-char trap
  on the cap constant); the underlying class is one this codebase
  kept producing because each call site re-derived its own
  `.read(N)` discipline. Centralising here means the next time
  someone adds a "read this user-controlled file" helper, the cap
  honours bytes (not characters), the error path is named
  (`ValueError`, not OOM), and the byte-vs-char trap is structurally
  impossible because the helpers open in binary mode. Unit-tested
  against the 4-byte-codepoint case directly.
- **`_fsutil.flock_excl` — single definition for the locking
  primitive.** `store.py:_locked` and `events.py:_locked` had been
  duplicate implementations of the same fcntl-based exclusive
  lock since the start. The 2.6.3 audit-pass-of-audit-pass fix
  touched both files because the unlink-in-finally bug lived in both
  copies. This release lifts the canonical definition to
  `_fsutil.flock_excl`; store / events / sync all alias to it.
  Future locking-discipline fixes land in one place — the
  3× duplication that the 2.6.3 audit cycle had to chase is gone.
- **`tests/_event_helpers.EventLog` + `event_log` fixture.** Real
  `Recorder`-backed event log for tests. The 2.6.2 and 2.6.3 bugs
  both shipped because test fixtures hand-built event dicts with
  field names the canonical `Recorder` doesn't emit (`memory_search`
  / `memory_ids` / `hit_ids` instead of `search` / `ids` /
  `returned`). Tests passed, production silently failed. `EventLog`
  routes through the real `Recorder` so any future field rename
  fails the suite at write time instead of shipping. Includes
  `test_shape_matches_real_handlers_emission` which pins the
  canonical key set explicitly — drift trips the suite.
- **Multi-process lockfile fault-injection tests
  (`test_concurrency.py`).** Two new deterministic tests:
  `test_store_locked_persists_lockfile_after_exit` and
  `test_events_locked_persists_lockfile_after_exit` assert that the
  lockfile must NOT be unlinked on context-manager exit (the exact
  2.6.3 regression). `test_locked_serializes_two_spawned_processes`
  spawns two interpreters and asserts B blocks on A's lock for the
  hold window. The stress test alone wouldn't catch a regression of
  the inode-identity invariant — these close that gap.
- **`test_changelog.py` — CHANGELOG hygiene lint.** Asserts every
  `## <version> -` heading is well-formed AND the version in
  `pyproject.toml` has a matching entry. The 2.6.2 release noted
  three missing-heading defects (1.2.1, 1.3.0, 2.6.0); the prose
  body in each case was intact but the heading had silently
  disappeared. Also pins `plugin.json` and `marketplace.json`
  version against `pyproject.toml` since the recurring foot-gun
  of one-of-three drifting bit the project on three separate
  releases. The next missing-heading or version-drift instance
  trips CI instead of an audit pass.

### Fixed — live shipping bugs

- **CRITICAL: silent-miss telemetry partially broken for hook-
  originated events (the 2.1.0 flagship).** `hook.py:run_audit`
  emitted `search_miss` with `top_hit_ids=[strings]` and omitted
  `threshold_rule` / `lookback_seconds`, while `_handlers.py:_advance_turn`
  (the in-process MCP handler) emits `top_hits=[dicts]` with both
  fields. `eval.py:_silent_miss_from_event` reads the canonical
  names — so every hook-originated silent miss surfaced in `bettermemory
  eval` showed blank `top_missed_id` / `top_missed_relevance` /
  `threshold_rule` columns. The Stop hook is the *primary* production
  source of search_miss events (model-side `memory_audit_turn` rarely
  fires unprompted), so the flagship eval feature was running blind
  on real traffic. **Three audit passes missed this.** Fix: hook
  now emits the canonical shape (`session_id=` kwarg, `top_hits=
  [h.to_dict() for h in report.top_hits]`, `threshold_rule`,
  `lookback_seconds`, `probe_mode`); eval reader tolerates the
  legacy `top_hit_ids` shape with `None` relevance so pre-2.6.4
  archived events still render the id column. Regression coverage
  in `test_hook.py` (pins canonical shape on every hook emission)
  and `test_eval.py:test_legacy_hook_top_hit_ids_shape_still_renders`.
- **HIGH: `_frontmatter.load` read whole file before YAML cap fired.**
  `_frontmatter.py:108-110` called `Path.read_text()` with no
  pre-flight size check. The existing `_MAX_YAML_BYTES = 64 KiB`
  cap only protects the frontmatter region — a hostile `sync pull`
  pushing a multi-GB `.md` would OOM the loader before the YAML
  parser ran. Three audit agents flagged this independently. Fix:
  stat-rejects above `_MAX_FILE_BYTES = 1 MiB` (250× the largest
  legitimate memory body, 16× the YAML cap) using `bounded_read`.
- **HIGH: `hook.py:main` read entire stdin payload with no cap.**
  `sys.stdin.read()` before `json.loads` would buffer GB of
  garbage from a misbehaving pipe writer into memory before the
  parser got a chance to reject. Stop hooks fire on every assistant
  turn — the blast radius is wide. Fix: `bounded_stream_read(
  sys.stdin.buffer, 64 KiB)` with oversized-payload treated as
  malformed (silent no-op, preserving the hook's "never break the
  turn end" contract).
- **HIGH: `migrate.py` rewrote memory files without `_locked`.**
  `migrate_origin_in_directory` walked the active set and
  read-modify-wrote each file via tmp+rename WITHOUT acquiring the
  per-file lock the rest of the store uses. A concurrent
  `Store.update` / `tombstone` / `mark_verified` from a running MCP
  server could land between the migrate read and the migrate
  rename, silently losing the in-flight edit. Fix: wrap each
  per-file RMW in `_locked(path)` AND route through
  `_atomic_write_post` (which the rest of the store already uses) —
  the migrate path was also dropping the 0o600 chmod, so
  post-migration files inherited umask (0o644) and ended up
  world-readable. Two fixes for the price of one structural
  consolidation.
- **MEDIUM: `sync.py push` / `pull` ran git operations with no
  mutual exclusion.** Two concurrent `bettermemory sync` runs (push
  racing push, or push racing pull) interleave their `git add` /
  `commit` / `pull --rebase` with nothing serializing them. Fix:
  both functions now hold `flock_excl(root / ".sync")` for the
  duration of the git operation sequence, making each sync op an
  atomic boundary against the other. The lock covers pull's reindex
  too so the FTS5 rebuild sees the same on-disk state the rebase
  landed. Pull's error message gained the `git rebase --abort`
  recovery hint for the crash-mid-rebase case. *Known limitation
  (corrected post-release): this lock does NOT coordinate against
  the in-process `Store`.* `Store.write` holds a per-memory-file
  lock on a different inode, so a `Store.write` landing mid-`git
  add -A` can still stage a half-written file-set (at worst one
  commit stale; the next sync corrects it). True sync↔Store
  coordination would require `Store`'s mutators to take the
  `.sync` lock too — a global write-serialization tradeoff
  deferred as a separate decision.
- **HIGH: LLM providers (Ollama, OpenAI) had no output-token cap.**
  Ollama call had no `num_predict` in `options`; the OpenAI
  provider passed no `max_tokens` (while Anthropic had carried
  `max_tokens=2048` from the start). A runaway local model can
  return arbitrarily many tokens; httpx buffers the whole body
  before `.json()` so the consolidate process OOMs on the response
  side. Fix: shared `DEFAULT_MAX_OUTPUT_TOKENS = 2048` enforced
  on all three providers. Plus a new `LLMResponseTruncated`
  exception raised when the provider signals it hit the cap
  (`done_reason="length"` / `stop_reason="max_tokens"` /
  `finish_reason="length"`) — pre-2.6.4 the truncated JSON
  silently fell through `parse_and_validate` as malformed, hiding
  the real root cause from the operator. Now the consolidate
  report surfaces "raise the cap or split the cluster" explicitly.
- **MEDIUM: `store.prune_tombstones` and `store.tombstone`
  concurrency.** `prune_tombstones` read+stat+unlink each
  tombstone WITHOUT the per-file lock — a concurrent `restore(id)`
  race could either un-tombstone or double-unlink. Fix: wrap the
  per-tombstone read/unlink in `_locked(path)`. Separately, the
  tombstone-naming TOCTOU (`if target.exists(): target = ...
  ULID-suffixed`) is killed by always using the ULID-suffixed
  filename — unique by construction, no race possible. Existing
  unsuffixed tombstones on disk continue to load (the reader keys
  off the `id` field, not the filename).
- **MEDIUM: `index.py:_ensure_schema` downgrade ran outside a
  transaction.** On schema-version-down, `DROP TABLE` then
  `CREATE TABLE` ran in autocommit. A parallel connection
  opening between the drop and the create saw a schema with no
  `memories` table and SELECTs failed (no BUSY raised because
  the table simply wasn't there yet). Fix: wrap drop+recreate
  in `BEGIN IMMEDIATE` ... `COMMIT` with rollback on exception.
- **MEDIUM: `semantic.flush_persistent_cache` cache flush race.**
  Two MCP servers in the same memory dir would both write
  `<root>/.embeddings.npz.tmp` and race the rename, last-writer
  -wins corrupting whichever lost. Plus no `fsync_dir`, no
  `chmod 0o600` (vector representations of memory bodies have
  the same privacy bar as the source memories). Fix:
  process-unique tmp name (`.tmp.<pid>`), `flock_excl` around
  the rename, `os.chmod(_PERSISTENT_PATH, 0o600)` post-rename,
  `fsync_dir` post-chmod.
- **MEDIUM: `events.py` chmod 0o600 failure silently suppressed.**
  `contextlib.suppress(OSError)` on the chmod meant a failure
  left the log world-readable with no signal. Fix: log WARNING
  on failure so the operator can investigate (typical causes:
  noexec/nosuid container mounts, restricted filesystems).
- **MEDIUM: `semantic.cosine_similarity_normalized` truncated to
  shorter input on dimension mismatch.** `zip(a, b)` over
  different-length vectors produced a similarity over the
  overlap only — meaningless number that still passed the
  threshold. The case fires when a persistent cache from one
  embedding model is read against another (config swap without
  `flush_persistent_cache`). Fix: `zip(a, b, strict=True)`
  raises `ValueError` on dimension mismatch.
- **MEDIUM: LLM merge apply had no partial-failure recovery.**
  `consolidate.apply_llm_proposal` updated the keeper's body
  then iterated `store.tombstone(dup_id)` for each duplicate.
  If the third of five tombstones failed, the keeper had the
  merged body while two duplicates were still active —
  retrieval would surface both the merged record and the
  unmerged duplicates. Fix: catch exception in the tombstone
  loop, roll back the keeper to its pre-merge body, then
  re-raise. The rollback is best-effort but the raise gives
  the operator a clean signal instead of silent half-done state.

### Fixed — pattern-generalization (event consumer fallbacks)

- The 2.6.3 fix added tolerant `event.get("returned") or
  event.get("memory_ids")` reads to `llm.py` only. Five other
  consumers (`_handlers.py`, `_response.py`, `hook.py`,
  `consolidate.find_demotion_candidates`,
  `consolidate.find_cold_scopes`) read canonical-only — pre-2.6.3
  archived events on disk were silently dropped from those passes.
  All five now use the same canonical-first-then-legacy discipline.
  Same fix applied to `session` / `session_id` divergence:
  pre-2.6.4 hook wrote `session=`, handler writes `session_id=`,
  the Recorder auto-stamps `session`. All consumers now read
  `event.get("session") or event.get("session_id")`.

### Changed

- **`tests/test_eval.py:_ev()` helper no longer fabricates
  impossible session shapes.** Pre-2.6.4 the helper hardcoded
  `session: "sess-test"` regardless of any `session_id=` the
  caller passed; the resulting event had both fields
  disagreeing — a state production never produces (both fields
  derive from `state.session_id`). Helper now omits the
  `session` default when the caller provides either field.
  Migration to `_canonical_event` for legacy hand-built
  fixtures is follow-up work; the helper docstring directs
  new tests to the `event_log` fixture.

### Audit framing — why a fourth pass

After three audits in one day the question shifted from "are
there more instances of these bug classes?" to "why do these
bug classes keep producing new instances?" The structural fixes
above (single helpers for byte caps + flock + test fixtures)
make the *class* impossible — not "the next instance harder to
find." That's the differential from the 2.6.1 / 2.6.2 / 2.6.3
audit-pass approach, which was finding instances of named bug
classes one at a time. Two of four 2.6.3 bug classes (byte-vs-
char, field-name drift) are now structurally impossible at the
write site. Concurrency primitive duplication is gone (3× → 1×).
The fourth pass found one critical-severity live bug
(silent-miss flagship broken for hook traffic) that three prior
passes missed — confirming the audit-of-audit-of-audit
diminishing-returns thesis: more audits surface different bugs
not because they're more thorough, but because each pass's
attention budget runs out before exhausting the surface.

Suite: 1277 passed, 9 skipped (+32 tests vs 2.6.3, including
the structural-tripwire trio: byte-cap unit tests, EventLog
shape-pinning, lockfile fault-injection).

## 2.6.3 - 2026-05-21

**Audit-pass-of-the-audit-pass-of-the-audit-pass.** A third multi-agent
review of the 2.6.2 surface — this time scoped to "find the bugs the
last two audits' fix patterns should have generalized" — caught one
CRITICAL concurrency bug, two HIGH transcript-read DoS surfaces, and a
latent field-name drift in `llm.py` that is the same class as the
`find_demotion_candidates` bug 2.6.2 fixed in `consolidate.py`. Plus the
matching docs follow-ups. No on-disk format changes, no wire-shape
changes; one new constant and one new test surface.

### Fixed

- **`store.py:_locked` and `events.py:_locked` unlinked the lockfile
  inside `finally`, silently breaking mutual exclusion under
  contention.** The context manager opened `lock_path` with
  `O_CREAT`, `flock()`-ed the fd, then on exit unlocked → closed →
  **unlinked** the file. If process A unlinks the lockfile after B has
  already opened it but before C calls `os.open(lock_path, O_CREAT)`,
  B keeps its fd on the now-defunct inode and C creates a fresh one;
  flock identity is per-inode, so B and C then both believe they hold
  the lock. The bug was invisible under low contention (each lock
  acquire-and-release was serial within one process) but loosens to
  full lost-update territory under cross-process load — exactly the
  scenario `bettermemory sync` and any future multi-client HTTP/SSE
  posture introduces. Fix: drop the unlink; persist the 0-byte
  lockfile so every `os.open` sees the same inode. Comment in both
  files records the trade-off so a future "the lockfiles are clutter,
  let's clean them up" PR fails the review instead of the production.
- **`hook.py:_extract_last_exchange` read the entire transcript with
  no cap.** Same OOM class the 2.6.2 release fixed in
  `consolidate.py:_load_transcript` for the consolidate path, left
  unaddressed in the Stop-hook path. Claude Code transcripts grow
  monotonically over a session; in extended pairing sessions the JSONL
  reaches hundreds of MB, and the hook fires after every assistant
  turn. The old read+splitlines pattern allocated the whole file twice
  before the reverse walk even started. Fix: seek to the end and read
  the trailing `_TRANSCRIPT_TAIL_READ_BYTES = 1_048_576` bytes, then
  discard the first partial line (the next newline starts a complete
  record). The hook only needs the latest user+assistant pair, which
  always sits at the tail of an append-only log — the head bytes are
  dead weight. Unseekable streams (FIFOs from `mkfifo`-based fixtures)
  fall back to a bounded forward read; binary-mode read with
  `errors="replace"` decode handles UTF-8 codepoints split at the
  truncation boundary.
- **`consolidate.py:_load_transcript` counted characters, not bytes,
  despite the cap constant being named `_TRANSCRIPT_READ_CAP_BYTES`.**
  The 2.6.2 release added `fh.read(_TRANSCRIPT_READ_CAP_BYTES)` on a
  *text*-mode stream, which reads at most that many *characters*.
  Worst-case multibyte UTF-8 (4 bytes/char) read up to ~4 MiB into
  memory before the cap kicked in — defeating the "1 MiB hard cap"
  the comment claimed. Fix: open in binary mode, read raw bytes,
  decode with `errors="replace"` so a partial codepoint at the
  truncation boundary doesn't raise. Same byte-vs-char trap as the
  classic `max-length` validators in HTTP frameworks; the fix is one
  call-shape swap.
- **`llm.py:_collect_contradiction_targets` and
  `_build_cluster_member` read the wrong event field names.** Code
  checked `kind == "memory_search"` / `"memory_record_use"` and
  `event.get("memory_ids")`, but the canonical `Recorder` writes
  `kind="search"` / `"use"` with `returned=[…]` / `ids=[…]` (see
  `_handlers.py:1049` and `_handlers.py:2039`). **Same class as the
  `find_demotion_candidates` bug 2.6.2 fixed** in `consolidate.py`:
  the tests passed because the fixtures used the legacy field names,
  so the production-shape mismatch never surfaced under CI. Result:
  contradiction clusters silently always empty against real event
  logs; the LLM never saw the `contradicted` signal it relies on to
  judge whether a near-duplicate pair is actually in opposition. Fix:
  read the canonical names with a tolerant fallback to the legacy
  shape (mirroring 2.6.2's `event.get("returned") or
  event.get("hit_ids")` discipline), plus `event.get("session") or
  event.get("session_id", "")` to tolerate both auto-emitted and
  hand-rolled session keys. New regression test
  `test_build_clusters_seeds_contradiction_from_real_recorder` in
  `tests/test_llm.py` round-trips events through a real `Recorder` so
  a future drop of the canonical-name path fails at suite time — the
  same discipline the 2.6.2 demotion fix established.

### Changed

- **`SECURITY.md` — corrected the Web UI CSRF claim.** The hardening
  notes still described the pre-2.3.0 permissive header-less POST
  behavior ("Header-less POSTs fall through… refusing every
  header-less POST would break the normal in-UI flow"). 2.3.0 closed
  that path: `web.py:_same_origin` now returns False when both
  `Origin` and `Referer` are absent, and the existing `test_web.py`
  regression coverage pins it. The doc now reads "Header-less POSTs
  are rejected." with the CLI-scripting escape hatch (`-H "Origin:
  http://127.0.0.1:<port>"`) called out explicitly. Security
  documentation overstating an attack surface that has already been
  closed is worse than understating it, but only by a hair — the fix
  brings the prose in line with the code.
- **`CHANGELOG.md` — restored the missing `## 1.2.1 - 2026-05-10`
  heading.** Same defect 2.6.2 fixed for `## 2.6.0` (and the 1.3.2
  entry already noted for 1.3.0 itself). The 1.2.1 narrative flowed
  out of the 1.2.2 entry without a separator; renderers walking the
  heading hierarchy saw 1.2.2's `### Fixed` body continuing into the
  1.2.1 prose. **Pattern-recognition note** (load-bearing for future
  audits): three releases now have shipped without their `##` heading.
  Worth adding a CI lint that asserts every `## <version> -` heading
  has a matching `[project] version = "<version>"` entry in
  `pyproject.toml`'s history (or a release tag) so the next instance
  trips the suite instead of an audit pass.
- **`docs/ROADMAP.md` — version pin and test count.** "Where we are"
  header read `(May 2026, v2.6.0)`; bumped to `v2.6.3`. The
  `1234 tests` line was off by ~20 after the 2.6.1 / 2.6.2 / 2.6.3
  additions; replaced with `1200+ tests` so the next minor doesn't
  drift again from the same precise-number-rots-fast root cause.

## 2.6.2 - 2026-05-21

**Audit-pass-of-the-audit-pass.** A multi-agent re-audit of the 2.6.1
surface caught four real correctness gaps in the consolidate path that
the previous read-through missed, plus several doc-vs-behavior drifts
worth fixing while the surface was warm. No on-disk format changes, no
wire-shape changes — only existing-but-skipped checks getting wired up
correctly.

### Fixed

- **`consolidate.py:find_demotion_candidates` was reading the wrong
  event field.** The demotion pass keyed retrieval counts off
  `event.get("hit_ids")`, but `_handlers.memory_search` records the
  result-id list as `returned` — has done since the recorder shape
  stabilised. Every real event log silently scored zero retrievals,
  which means the demote-never-applied rule never proposed a single
  candidate against a production store. The unit tests passed because
  the synthetic fixtures used the legacy `hit_ids` field. Now reads
  `returned` with a `hit_ids` fallback for any pre-rename event logs;
  added a regression test (`test_consolidate.py`) that rounds through
  a real `Recorder` so a future drop of the `returned`-aware path
  fails at suite time.
- **`consolidate.py:_apply_llm_proposal` `propose_new` branch
  bypassed every write-time guardrail.** `memory_write` runs
  `find_similar` (active + tombstones), `find_transient_markers`, and
  the new 2.6.1 `max_content_bytes` cap before committing — but the
  LLM-proposed branch went straight to `store.write` with none of
  them. Since the LLM only sees ~8 cluster members as "don't
  duplicate these" context, dedup against the full active set is
  load-bearing: without it, `consolidate --llm --from-transcript`
  would happily re-create memories the user already wrote (or
  already removed). All four gates now fire before the write; gate
  failures raise `RuntimeError` which `consolidate_llm` already
  records as `LLMClusterFailure`, so the operator sees the rejection
  reason in the report.
- **`consolidate.py:_load_transcript` had no read-size cap.** The
  whole transcript file was `path.read_text()`-ed before any
  truncation — same unbounded-input class 2.6.1's `max_content_bytes`
  work closed for memory bodies. A multi-GB transcript would OOM the
  process. Now caps the read at 1 MiB via a single `f.read(N)` on the
  text stream; the downstream prompt builder still truncates again
  at `MAX_TRANSCRIPT_CHARS` (12 KB).
- **`llm.py:_validate_propose_new` didn't call `validate_scope`.** A
  syntactically-bad scope (e.g. `"foo bar"`, anything outside the
  lowercase-alphanumeric-plus-hyphens-and-colons grammar) passed
  validation and crashed at apply time — *after* the user had
  already seen and accepted the `+ NEW MEMORY` diff. Now uses the
  same `validate_scope` helper `memory_write`'s payload validator
  does, so bad scopes are rejected before the diff renderer sees
  them.
- **`store.py` — three bare `except Exception:` narrowed.**
  `_find_tombstone_path_for_id` (line 600), `rename_scope`'s
  tombstone branch (line 845), and `_find_path_for_id` (line 952)
  were catching every exception from `frontmatter.load`, including
  ones that should propagate (e.g. `MemoryError` on a pathological
  file). Tightened to `(ValueError, KeyError, OSError)` to match the
  rest of the file's convention. Tests unchanged; no behavioral
  difference on the well-formed-frontmatter happy path.
- **`examples/memories/*.md` were silently broken.** All three
  placeholder IDs (`01HXYZTUTORIALSTYLEEXAMPLE`,
  `01HXYZHOMELABNETWORKEXAMPL`,
  `01HXYZPROJECTFOOSTACKEXAMP`) contained `I` / `L` / `O` / `U`
  characters, which the Crockford-base32 `_ULID_RE` rejects. `Memory()`
  validation failed and `Store.load_all` swallowed the `ValidationError`
  — so following the README's "drop these into `~/.claude-memory/`"
  instruction produced an empty store with no error visible to the
  user. Regenerated three valid IDs via `generate_ulid()`.

### Changed

- **`CHANGELOG.md` — restored the missing `## 2.6.0 - 2026-05-21`
  heading.** The 2.6.1 insertion landed without recreating the
  separator between releases, so the 2.6.1 "Fixed" bullets flowed
  directly into the 2.6.0 body text. Cosmetic but unambiguously
  wrong for changelog consumers (rendered HTML, parsers that walk
  the heading hierarchy). Also added the missing 2.6.1 "Fixed"
  bullet for commit `00521d5` — the deleted-CWD Stop-hook fix
  shipped in 2.6.1 but never made it into the changelog entry — and
  scoped the 2.4.0 system-dirs claim to clarify Windows behaviour.
- **`SECURITY.md` — reworded the YAML claim.** "No `yaml.load`
  anywhere" was an overclaim — `_frontmatter.py:100` does call
  `yaml.load(..., Loader=yaml.SafeLoader)`. Same safety property,
  but the literal sentence was wrong. Now reads "every `yaml.load`
  call pins `Loader=yaml.SafeLoader`" plus the 64 KB pre-flight cap
  noted in the parser as belt-and-suspenders.
- **`CONTRIBUTING.md` — inlined the macOS `UF_HIDDEN` explanation.**
  The previous text cross-referenced a README "macOS gotcha"
  section that doesn't exist. Now self-contained, with the
  `chflags nohidden .venv` recovery command for an already-flagged
  directory.
- **`docs/installation.md` — added the `[embeddings-fast]` extra.**
  Shipped in 2.5.0 but the install page only listed `[embeddings]`
  and `[ui]`. New entry calls out the PyTorch-vs-ONNX trade-off (~500
  MB vs ~50 MB on disk) and the `[behavior] semantic_provider` knob
  for selecting fastembed explicitly when both extras are installed.
- **`_handlers.py:DESC_MEMORY_WRITE`** — the documented success
  status was `"ok"` but the actual emitted value is `"committed"`
  (`_response.committed` has been stable on `"committed"` since 2.x).
  Models branching on the documented value got a no-match. Now
  matches the emission.
- **`server.py` module docstring** — Curation list extended to
  include `memory_audit_turn`. Was 17 of 18; registration was always
  correct, the docstring was stale.

## 2.6.1 - 2026-05-21

**Audit-pass follow-up.** A read-through of the 2.6.0 surface
surfaced one defensible bug, two defence-in-depth gaps, an unbounded
input on `memory_write`, and a smoke test that was conflating benign
lifecycle events with the failure mode it was meant to catch. None of
the changes touch the on-disk format, the wire shape, or any contract
the model branches on; older callers see byte-stable behaviour.

### Added

- **`[behavior] max_content_bytes` write-time cap (default 1 MB,
  `0` disables).** Closes the only unbounded-input surface left after
  the YAML / note / origin trust-boundary work in 1.x. The event log
  already rotates at 10 MB, but the memory file itself was previously
  unbounded — a runaway model or hostile client could fill disk with a
  multi-gigabyte body. `memory_write` and `memory_update` now share a
  `_validate_content_size` helper that measures encoded UTF-8 byte
  length (the unit that actually lands on disk and in the JSONL log,
  not character count) and raises a clear `ValueError` past the cap.
  Existing on-disk memories are never re-validated, so raising the cap
  downward never rejects already-stored data.

### Changed

- **`orphan_use_events` is now a clean fabrication smoke test.** The
  rollup previously incremented on every `record_use` referencing an
  id not in the active store — which conflated benign
  tombstone-after-use lifecycle events with model hallucination. The
  CLAUDE.md / health output advised treating a growing count as "model
  is hallucinating ids", but in practice the count was dominated by
  legitimate post-tombstone references. `compute_health` now accepts
  an optional `tombstoned_ids` set; ids in that set are filtered out
  of the orphan count. `report_for_directory` passes the live
  tombstone set from `store.load_tombstones()`, so production callers
  via the MCP tool and CLI subcommand get the sharpened signal.
  Callers that don't pass `tombstoned_ids` see the legacy conflated
  count — backward compatibility for offline tooling that builds
  events without a live store.

### Fixed

- **`store.py:Store.__post_init__` — explicit `mode=0o700` on the
  tombstone directory.** Previously `mkdir(exist_ok=True)` relied on
  the caller's umask for owner-only permissions. On a system with a
  loose umask (0o027 or higher), the tombstone directory could be
  group- or world-listable. The active memory dir has always been
  owner-only via the per-file 0o600 fanout; the tombstone dir now
  matches at the directory level too. Tombstones carry the same trust
  boundary as active memories (paths in `removed_reason`, body hashes
  for dedup), so directory listing should require the owner.
- **`web.py` `/memories?scope=…` query param now validates.** The
  scope query param fed straight into `store.list_summaries` with no
  validation — not an injection vector (set-intersection on scope
  strings, no SQL exposure), but inconsistent with the MCP handlers
  that all call `validate_scope`. A malformed scope (e.g.
  `?scope=../etc/passwd`) silently returned an empty list, masking the
  user's typo as "no results". The route now returns a clear 400 with
  the same error message MCP handlers produce.
- **`_response.py:_attach_commit_drift_to_hits` — defensive `.get()`
  on `path_drift_missing` during late verdict recomputation.** The
  late recompute reads `hit_dict["path_drift_missing"]` set earlier by
  `hit_to_dict`. The dependency was safe today but invisible across
  function boundaries; a future refactor that changed when the field
  attached would `KeyError` retrieval. Now uses `.get("…", 0)` — costs
  nothing, removes the implicit invariant.
- **`attribution.py:_SENTENCE_SPLIT_RE` docstring.** The comment
  claimed the trailing-space requirement avoided breaking
  abbreviations into pseudo-sentences. It only achieves that for
  decimal numbers (`1.5`) and version strings (`v2.6.0`) where the
  dot is followed by a digit; prose abbreviations like `Dr. Smith` or
  `e.g. foo` do split. The over-split is accepted by design (the same
  boundary is tested from the other side, so attribution survives the
  loss of one fragment), but the comment was misleading and would
  have steered a future maintainer toward the wrong fix. Comment
  rewritten to match what the regex actually does.
- **`hook.py` Stop-hook tolerates a deleted CWD.** The audit path
  read `Path.cwd()` so the per-turn event-log walk could attach an
  `origin` field; if the user `rm -rf`-ed the project directory mid-
  session (or any other producer of `FileNotFoundError` /  `OSError`
  on `getcwd`), the hook would tear down the whole session instead of
  just dropping the attribution. Now: catch `(FileNotFoundError,
  OSError)` and continue with `origin=None`. The Stop hook is best-
  effort; one cwd-resolution failure shouldn't kill the rest of the
  attribution pass.

## 2.6.0 - 2026-05-21

**Three writing-reflex / audit-attribution levers that close the gap
between the verification contract and what the model actually does.**
The MCP contract asks the model to attach `claim_excerpts` on explicit
`memory_record_use`, to spot-check claims when the staleness verdict
isn't fresh, and to call `memory_write` whenever something durable
enters the conversation. In dogfood the model defaults to the cheap
auto-commit path on all three — `memory_helped_rate` reads 0%, the
spot-check ceremony asks the model to recompute what the server
already knows, and most durable facts never get written. This release
closes each gap by moving the load-bearing work off the model:
the server already knows which paths drifted, the Stop hook already
sees the assistant reply, and `consolidate --llm --from-transcript`
already has the conversation. None of the three change the surface
contract; they just stop asking the model to do something the system
is in a better position to do itself.

### Added

- **`path_drift` lists inline on every search hit.** The search
  pipeline already runs `detect_path_drift` inside `_build_hit` but
  was discarding the actual path lists, keeping only the integer
  counts. The model got a non-fresh `staleness_verdict` with no
  actionable handle — its only options were `memory_show` round-trips
  or manually re-scanning the snippet. New `MemoryHit` fields
  (`path_drift_checked_paths` / `path_drift_missing_paths` /
  `path_drift_verified_paths`) carry the `PathDriftReport`'s lists
  through and the search response surfaces them under
  `path_drift = {checked, missing, verified}` — the same key shape
  `memory_show` already uses. A `spot_check_recommended` hit with
  `path_drift.missing = ["src/auth/middleware.py"]` is now directly
  actionable: `memory_update` the rotted bit or `memory_verify` the
  rest, no round-trip needed. Side effect: `_build_hit` now passes
  `verified_paths` to `detect_path_drift` (it wasn't before — the
  `verified` field of the report was always empty on search hits, bug
  fix). The spot-check ceremony language across
  `DESC_MEMORY_SEARCH`, `SYSTEM_PROMPT_ADDENDUM`,
  `plugin/skills/bettermemory/SKILL.md`, and `docs/api.md` updated:
  the previous contract asked the model to recompute what the server
  already knew; the new contract reads the missing-paths list
  directly.
- **Stop-hook post-hoc `claim_excerpt` attribution.** New
  `attribution.py` module runs a precision-tuned substring match
  (sentences ≥6 tokens AND ≥30 chars, stopword-filtered,
  case- and whitespace-normalised) between recently-retrieved memory
  bodies and the assistant reply text. When a body sentence appears
  in the reply, the Stop hook emits one `record_use` event per
  (memory, matched-sentence) pair with `outcome="applied"`,
  `auto=false`, `attribution="hook"`, and the matched phrase as the
  `claim_excerpt`. New `attribution` field on `use` events with three
  tiers — `model` (explicit by AI), `hook` (substring-match), `auto`
  (the fallback). Older events without the field fall back at read
  time (`auto=false → model`, `auto=true → auto`). `_advance_turn`
  reads recent events at the start of each `memory_*` call, purges
  hook-attributed ids from the pending map, and skips the
  auto-commit pass for them — so each retrieval generates exactly one
  `applied` event (hook, model, or auto). The hook also filters
  memory_ids that already have any `use` event in the lookback window
  (600s default), so a model that DID record use explicitly doesn't
  get a redundant hook attribution. `bettermemory eval` and
  `docs/eval.md` updated to describe the tier; the three-way split
  surfaces in the `applied_total` / `applied_explicit` counts so
  consumers can recompute against a stricter "model only" definition.
  Tests: 11 new in `tests/test_attribution.py`, 3 new in
  `tests/test_hook.py`, 1 new in `tests/test_server_v12_features.py`.
- **`bettermemory consolidate --llm --from-transcript PATH`.** The
  MCP contract asks the model to call `memory_write` whenever
  something durable enters the conversation; in practice the bar for
  "durable" is fuzzy and head-down task focus wins. The new flag
  reads the conversation after the fact (plain text, Markdown, or
  Claude Code session JSONL — autodetected by extension) and asks
  the LLM to propose new memories worth saving. Fifth proposal type
  `propose_new(scope, category, body, source_excerpt, rationale)`
  joins the existing four (merge / resolve_contradiction /
  rewrite_relative_date / demote_tier) under the same audit gate —
  every proposal renders as a "+ NEW MEMORY" diff preview, `--apply`
  requires either `--yes` (batch accept) or an interactive y/N
  prompt. Hallucination defences fire: `scope` must be non-empty and
  not "general" (the catch-all); `category` must be `fact` or
  `ambient` — never `user-inference` (that tier requires explicit
  user confirmation the consolidate path can't supply);
  `source_excerpt` is required and capped at 500 chars; `body` must
  be non-empty. The excerpt is stamped into the new body as a
  provenance line so future audits trace each claim back to a
  transcript turn. Existing memories (most-recently-updated, capped
  at 8) ride along as the "don't propose duplicates of these"
  context. Cluster shape extended with optional `transcript: str |
  None` and `cluster_kind="transcript_facts"`; existing cluster types
  unaffected. Tests: 9 new in `tests/test_llm.py`, 6 new in
  `tests/test_consolidate_llm.py`. Full suite: 1234 passed, 9
  skipped.
- **`docs/incidents/` postmortem scaffold.** A public-postmortem
  directory for reported memory-rot bugs — cases where the
  verification trifecta (calendar age + path drift + commit drift)
  missed a stale claim or fired on a fresh one. Competing memory
  systems don't surface their drift bugs because their architecture
  doesn't expose drift to begin with; bettermemory's contract puts
  the verdict in every retrieval response, so we owe a public
  accounting when the verdict was wrong. `README.md` explains
  why-and-how-to-file; `TEMPLATE.md` is the fillable shape (Symptom
  / Root cause / Fix / Verification / What the surface should do
  differently). Index is empty until the first report lands.

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
  `/var`, `/usr`) get filtered too — on POSIX they exist by
  definition so the filter is a no-op for real drift, and on Windows
  they don't exist as bare roots so filtering them strips a false-
  positive at zero cost. Five regression
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

## 1.2.1 - 2026-05-10

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
