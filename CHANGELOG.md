# Changelog

Notable changes between releases. Format follows
[Keep a Changelog](https://keepachangelog.com/) loosely; the project uses
0.x SemVer where minor bumps are additive feature drops and patches are
fixes.

## Unreleased

### Added

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
