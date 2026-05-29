# bettermemory roadmap

Published roadmap is part of the distribution strategy: people deciding between memory layers want to know where a project is going, not just where it's been. This document lists the planned work in roughly the order it will land. Plans change; the CHANGELOG is the source of truth for what shipped.

## Where we are (May 2026, v3.3.3)

- 24 MCP tools: 20 `memory_*` (retrieval, writing, lifecycle, verification, curation, session-local, plus `memory_proposals` for the opt-in write-reflex capture queue) + 4 `episode_*` (sibling tier for journal-shaped run-state — `/loop` iterations and subagent handoffs land in episodes; `episode_handoff` at iteration entry, `episode_write(takeaway=…)` at iteration exit, `episode_promote` distills a takeaway into a durable memory via the standard `memory_write` audit gate).
- `memory_search(since_prior_session=True)` filter restricts candidates to memories updated since the prior-session boundary in this worktree — answers "what's changed since I was last here?" without scanning the full store.
- `depends_on_resolved` inlines graph-edge target summaries on every hit (3 per hit, 10 total), closing the "graph in the schema, retrieval ignores it" gap.
- Proactive curation surface: `HealthReport.recommendations` distills bucket rollups into actionable one-line suggestions; inline `curation_hint` on the first successful `memory_write` per session when curation pressure crosses the configurable threshold; `recently_removed_in_worktree` on `memory_scope_overview` flags when the model is about to re-cover trimmed ground.
- FTS5 inverted index pre-filtering candidates above ~500 memories.
- Staleness verdict trifecta (calendar + path drift + commit drift) on every retrieval; `path_drift = {checked, missing, verified}` lists inline on every hit.
- `memory_record_use` with claim-level `claim_excerpts`; Stop-hook post-hoc substring-match attribution closes the "model didn't bother attaching the excerpt" gap (`attribution ∈ {model, hook, auto}`, exactly one event per retrieval).
- `memory_audit_turn` silent-miss probe, threshold rule versioned at `v1_top1_high`.
- `bettermemory consolidate --llm` Dreaming-defense pass with five proposal types (merge / resolve_contradiction / rewrite_relative_date / demote_tier / propose_new); `--from-transcript PATH` closes the writing-reflex gap by proposing new memories from a conversation, all under the same audit-gate accept loop.
- `bettermemory eval` CLI: `memory_helped_rate` / `endorsement_rate` / `silent_miss_rate` with Wilson 95% CIs.
- Curation-debt-clearing surface: `bettermemory consolidate --acknowledge-debt` (2.7.3) writes one explicit-applied event per cold-endorsement memory to clear the signal without touching bodies; `bettermemory consolidate --acknowledge-misses-before <ISO_TS>` (3.0.0) writes a `silent_miss_cutoff` event that retroactively drops pre-cutoff `turn_audited` / `search_miss` events from the rollup. Both are purely additive — no `--apply` gate, reversible by a follow-up event.
- Git-based cross-host sync via `bettermemory sync`.
- FastAPI curation UI (`bettermemory ui`).
- 1500+ tests, 80% coverage floor, Python 3.11–3.14, MIT.

## ~~Next~~ Shipped (2.5.0) — closing the recall gap

✅ **Optional `fastembed` embedding mode without a torch dep.** The new `[embeddings-fast]` extra wraps `fastembed` + ONNX Runtime (~50 MB total) as a drop-in replacement for the `[embeddings]` extra's `sentence-transformers` + PyTorch (~500 MB). `[behavior] semantic_provider = "auto"` picks torch when both are installed (existing `.embeddings.<model>.npz` caches stay byte-stable), otherwise fastembed; explicit `"torch"` / `"fastembed"` honoured even when the extra isn't installed (the per-provider WARNING surfaces the missing-extra hint). Provider-namespaced cache files (`.embeddings.fastembed.<model>.npz` vs the legacy `.embeddings.<model>.npz`) prevent vector mixing across providers. Default model `BAAI/bge-small-en-v1.5` (384-dim, ~33 MB ONNX) mirrors `all-MiniLM-L6-v2`'s dimensionality so cosine thresholds stay comparable. `bettermemory reindex --embeddings` warms the new cache after a provider swap. CI gains `test-embeddings-fast` pinned to Python 3.13 (fastembed wheels lag 3.14); see `pyproject.toml` for the `no_fastembed` / `no_torch_embeddings` pytest markers. *Why this matters: the bear case on bettermemory was "FTS5-only loses every public recall benchmark." That objection is now closed without compromising the no-database default.*

## ~~Next release~~ Harness shipped (3.3.3) — comparative publication

✅ **The comparative-evaluation harness landed at `tests/eval/`.** `tests/eval/comparative.py` builds a fixed conversational workload (`workload.py`), drives each system's adapter (`adapters.py`), and renders a publishable report: a capability matrix first (which systems even log the signals needed to compute the trio), then bettermemory's measured lanes, then an honest accounting of what didn't run and why. Runnable as a module — `python -m tests.eval.comparative` (text) or `--json` (machine-readable); `--k` sets the recall cutoff. bettermemory runs locally; Mem0 (OpenMemory self-host), Anthropic's reference `server-memory`, and claude-mem ship as capability-matrix stubs that report why they can't execute in-process rather than being silently dropped. The trio reuses `bettermemory eval`'s `RateCI` (Wilson 95% CIs); `memory_helped_rate` / `endorsement_rate` read `n/a` offline by design — they need a live agent emitting `record_use` events, and fabricating them would just relabel recall. Tests in `tests/eval/test_comparative.py`.

**Still pending: publish the numbers.** The harness exists; what remains is wiring live competitor runs (and agentmemory, which isn't yet an adapter) and the write-up. *Why this matters: every other comparison article in this market is about retrieval recall — a different question. Owning "did memory shape the reply?" is the lane-claim, and a published comparative is the grounding artifact for it.*

## ~~Next~~ Shipped (2.5.0) — `bettermemory eval`

✅ **`bettermemory eval` CLI**. Reads `.events.jsonl` plus the active store, reports `memory_helped_rate`, `endorsement_rate`, `silent_miss_rate` with Wilson 95% confidence intervals. Lists cold-endorsement memories and silent-miss candidates. JSON output for CI. Methodology in [`docs/eval.md`](eval.md); pure compute in `src/bettermemory/eval.py`; 52 tests in `tests/test_eval.py`.

**Comparative harness shipped in 3.3.3; numbers still pending.** The harness landed at `tests/eval/` (see the comparative-publication section above) — bettermemory runs locally, the competitors ride as capability-matrix stubs. The metric and the harness are owned territory — *every other comparison article in this market is about retrieval recall.* Owning *"did memory shape the reply?"* is the lane-claim. The `[embeddings]` and `[embeddings-fast]` extras (1.0.0 and 2.5.0) closed the install-friction gap; what remains is wiring live competitor runs + the write-up.

## ~~After that~~ Shipped (2.5.0, `--from-transcript` 2.6.0) — Dreaming defense via local consolidation

✅ **`bettermemory consolidate --llm`.** The four offline passes (dedup, demote-never-applied, cold-scope suggestions, scope-typo) now have a fifth sibling: cluster related memories, send each cluster + its `claim_excerpts` history to a local Ollama model (default) or to Anthropic / OpenAI (env keys), and let the model propose **merges**, **contradiction resolutions**, **relative-date-to-absolute rewrites** (today's date passed in the prompt so the model doesn't infer it from training data), and **tier demotions** for facts whose verifiable claims have been superseded.

**The audit-transparency moat.** Anthropic's Dreaming consolidates invisibly behind the agent surface — facts move and the model never sees the diff. bettermemory's `--llm` is the opposite: every proposal renders as a unified diff with the LLM's rationale, and `--apply` refuses to commit without either `--yes` (batch accept) or an interactive accept loop (per-proposal y/N prompt). Hallucinated memory IDs (the LLM produces a memory_id that wasn't in the cluster) are rejected at validation time *before* the diff renderer sees them — refusing on principle keeps the audit story clean.

**Default off.** No LLM call unless `--llm` is passed. The structural passes remain the default; `--apply` without `--llm` still commits only the structurally-safe operations. `--llm-provider` is `ollama` by default (localhost, no key), with `anthropic` / `openai` available behind their respective SDKs + env keys.

✅ **`--from-transcript` (writing-reflex gap).** The MCP contract asks the model to call `memory_write` whenever something durable enters the conversation; in practice the bar for "durable" is fuzzy and head-down task focus wins, so most writes get skipped. `bettermemory consolidate --llm --from-transcript PATH` reads the conversation (plain text, Markdown, or Claude Code session JSONL — autodetected) and asks the LLM to propose new memories worth saving. The fifth proposal type, `propose_new`, joins the existing four under the same `--apply`/`--yes`/interactive accept gate. Existing memories ride along as the "don't propose duplicates of these" context; `user-inference` category is forbidden (requires explicit user confirmation the consolidate path can't supply); `source_excerpt` provenance is stamped into the new body so the audit trail traces every claim back to a transcript turn.

## ~~After that~~ Shipped (2.7.0) — Claude Code auto-memory bridge

✅ **`bettermemory ingest --from <path>`.** Claude Code 2.x writes auto-memory to a per-project filesystem directory. The new CLI walks the source directory, parses each `.md` file's frontmatter (`name`, `description`, `metadata.type`), maps the type to a bettermemory category (`user` → `user-inference`, `feedback`/`project` → `fact`, `reference` → `ambient`), dedups against the active store and tombstone log, and writes survivors as ordinary records carrying an `imported-from-claude-code` provenance scope. The plugin SKILL.md banner was loosened from "don't write to that path" to "ingest it once if it exists" — the framing flipped from "fight" to "consume."

**Path auto-discovery + path-arg.** When `--from` is omitted, the CLI tries `~/.claude/projects/<sanitized-cwd-path>/memory/`; if no such directory exists, it exits with a hint. `--dry-run` reports the plan without committing; `--scope` appends extra scopes to every row.

**Out of scope (for this release).** Source-file mutation (writing back an "ingested" marker) was considered and rejected — dedup against the active store + tombstone log already makes re-ingestion safe, and modifying source files would race Claude Code's own auto-memory writes. If a user wants to delete the source dir after ingest, they do so manually.

## ~~After that~~ Shipped (2.7.0) — Trim-surface evidence

✅ **`bettermemory eval --tool-usage`.** Per-MCP-tool call-count rollup from the event log. Answers "which tools is the model actually reaching for?" without running `compute_health`. The intended use is the *evidence* underlying the next surface-trim decision: tools that haven't been called across multiple dogfood installs are candidates to move behind a power-user flag and out of the default `instructions` block. The map from event `kind` to tool name lives in `eval._TOOL_EVENT_KIND_TO_TOOL`; tools without a dedicated event (today: `memory_health`) surface with a zero count and a "no telemetry" caveat rather than being silently dropped.

✅ **`bettermemory eval --threshold-sweep`.** Counterfactual replay of logged `search_miss` events under alternative threshold rules (`v1_top1_high` current default + three strictly-stricter variants: `v2_top1_high_score_50`, `v3_top1_high_dominant`, `v4_top1_high_strict_combined`). Closes the calibration question `audit.py`'s docstring flags as open — *is v1 over-firing?* — by letting the maintainer see how many of the v1-flagged misses would still be flagged under a tighter rule. Sweep is *relative* (strictly-looser rules can't be replayed, because the companion `turn_audited` event doesn't carry `top_hits`); the limitation is documented and the alternative — bloating `turn_audited` with top_hits — is a deliberate trade-off, not a roadmap commitment.

## ~~After that~~ Shipped (2.7.0) — Session-aware curation hint

✅ **`memory_scope_overview` returns `curation_pending_new_since_last_session`.** The absolute `curation_pending` rollup stayed non-zero between sessions even after the user saw it, which made the session-start hint a candidate for nag-fatigue. The new sibling field uses the latest event from a different `session_id` as the boundary and recomputes the rollup over events emitted and memories created after that point — so the model branches on "*new* curation pressure" rather than the accumulated total. The field is `null` on the very first session (no prior boundary to delta against); the absolute view stays the fall-through.

## After that — operational polish

- **Encryption-at-rest option.** Today: plaintext on disk; threat model is OS-level encryption. Some users (and a future linter for credential-shaped strings at write time) want defense in depth. Investigate a `[encrypted]` extra with `age`-backed envelope encryption per file. Likely won't ship in 2026.
- **Status-only `bettermemory ui --tunnel`.** The FastAPI UI is local-only. A `--tunnel` flag wires up a one-shot Cloudflare or Tailscale Funnel tunnel for read-only browsing from another device. No mutations over the tunnel.

## Deliberately out of scope

These come up; here's why they aren't on the list:

- **Managed cloud SKU.** Local-first is a feature, not a constraint; competing with Mem0/Zep/Letta on managed infrastructure means competing on infrastructure I won't run as well as they will.
- **Team-shared multi-user store with permissions.** memctl owns that lane. The `bettermemory sync` git-based pattern handles "the same user across multiple machines" cleanly; "the same store across multiple users with RBAC" is a different product.
- **Knowledge-graph backend.** Zep/Graphiti/Cognee own that lane. Adding a graph backend dilutes the "plain markdown audit-friendly" story without meaningfully closing the gap.
- **Non-MCP SDK / REST endpoint.** MCP is the right protocol; the Linux Foundation + Anthropic + Block + OpenAI governance has stabilized it. Programmatic users can `import bettermemory` directly from Python.
- **Pursuing LongMemEval leaderboard top spot.** OMEGA at 95.4% and agentmemory at 95.2% reflect targeted retrieval-engineering investment that isn't bettermemory's wedge. We'll publish a respectable number from the comparative-publication pass; the goal is "competitive, not first."

## Get involved

The high-leverage contribution shapes:

- **Run `bettermemory eval` against your own usage** and open an issue with anomalies. The threshold rule for silent-miss detection (`v1_top1_high`) is calibrated against the author's data; recalibrating against more usage patterns is the open question.
- **Integration cookbooks** for clients that aren't Claude Code / Claude Desktop / Cursor / Continue / Cline. The MCP protocol is universal; the friction is the per-client config bit.
- **Memory-rot war stories.** If a stored memory misled you in a way the verification surface didn't catch, the bug report is the most valuable thing — it tells us where the drift extractor, threshold rule, or path-token grammar needs to widen.
