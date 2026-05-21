# bettermemory roadmap

Published roadmap is part of the distribution strategy: people deciding between memory layers want to know where a project is going, not just where it's been. This document lists the planned work in roughly the order it will land. Plans change; the CHANGELOG is the source of truth for what shipped.

## Where we are (May 2026, v2.6.3)

- 18 MCP tools across retrieval, writing, lifecycle, verification, curation, and session-local controls.
- FTS5 inverted index pre-filtering candidates above ~500 memories.
- Staleness verdict trifecta (calendar + path drift + commit drift) on every retrieval; `path_drift = {checked, missing, verified}` lists inline on every hit.
- `memory_record_use` with claim-level `claim_excerpts`; Stop-hook post-hoc substring-match attribution closes the "model didn't bother attaching the excerpt" gap (`attribution ∈ {model, hook, auto}`, exactly one event per retrieval).
- `memory_audit_turn` silent-miss probe, threshold rule versioned at `v1_top1_high`.
- `bettermemory consolidate --llm` Dreaming-defense pass with five proposal types (merge / resolve_contradiction / rewrite_relative_date / demote_tier / propose_new); `--from-transcript PATH` closes the writing-reflex gap by proposing new memories from a conversation, all under the same audit-gate accept loop.
- `bettermemory eval` CLI: `memory_helped_rate` / `endorsement_rate` / `silent_miss_rate` with Wilson 95% CIs.
- Git-based cross-host sync via `bettermemory sync`.
- FastAPI curation UI (`bettermemory ui`).
- 1200+ tests, 80% coverage floor, Python 3.11–3.14, MIT.

## ~~Next~~ Shipped (Unreleased) — closing the recall gap

✅ **Optional `fastembed` embedding mode without a torch dep.** The new `[embeddings-fast]` extra wraps `fastembed` + ONNX Runtime (~50 MB total) as a drop-in replacement for the `[embeddings]` extra's `sentence-transformers` + PyTorch (~500 MB). `[behavior] semantic_provider = "auto"` picks torch when both are installed (existing `.embeddings.<model>.npz` caches stay byte-stable), otherwise fastembed; explicit `"torch"` / `"fastembed"` honoured even when the extra isn't installed (the per-provider WARNING surfaces the missing-extra hint). Provider-namespaced cache files (`.embeddings.fastembed.<model>.npz` vs the legacy `.embeddings.<model>.npz`) prevent vector mixing across providers. Default model `BAAI/bge-small-en-v1.5` (384-dim, ~33 MB ONNX) mirrors `all-MiniLM-L6-v2`'s dimensionality so cosine thresholds stay comparable. `bettermemory reindex --embeddings` warms the new cache after a provider swap. CI gains `test-embeddings-fast` pinned to Python 3.13 (fastembed wheels lag 3.14); see `pyproject.toml` for the `no_fastembed` / `no_torch_embeddings` pytest markers. *Why this matters: the bear case on bettermemory was "FTS5-only loses every public recall benchmark." That objection is now closed without compromising the no-database default.*

## Next release — comparative publication

**Run `bettermemory eval` against the field and publish the numbers.** The eval CLI shipped in Unreleased, and `[embeddings-fast]` now closes the install-friction gap that would have made apples-to-apples retrieval comparisons awkward. The harness builds a fixed conversational workload, runs each system end-to-end, computes the trio (`memory_helped_rate`, `endorsement_rate`, `silent_miss_rate`), and reports with Wilson CIs. Systems to include: bettermemory, Mem0 (OpenMemory self-host), Anthropic's reference `server-memory`, claude-mem, and agentmemory. *Why this matters: every other comparison article in this market is about retrieval recall — a different question. Owning "did memory shape the reply?" is the lane-claim, and a published comparative is the grounding artifact for it.* Harness shape: `tests/eval/comparative.py`.

## ~~Next~~ Shipped (Unreleased) — `bettermemory eval`

✅ **`bettermemory eval` CLI**. Reads `.events.jsonl` plus the active store, reports `memory_helped_rate`, `endorsement_rate`, `silent_miss_rate` with Wilson 95% confidence intervals. Lists endorsement-debt memories and silent-miss candidates. JSON output for CI. Methodology in [`docs/eval.md`](eval.md); pure compute in `src/bettermemory/eval.py`; 52 tests in `tests/test_eval.py`.

**Still pending: comparative publication.** Run the same workload against bettermemory, Mem0 (OpenMemory self-host), Anthropic's reference `server-memory`, claude-mem, and agentmemory. Publish the numbers. The metric and the harness are owned territory — *every other comparison article in this market is about retrieval recall.* Owning *"did memory shape the reply?"* is the lane-claim. Harness shape: `tests/eval/comparative.py` to land alongside the embedding extra.

## ~~After that~~ Shipped (Unreleased) — Dreaming defense via local consolidation

✅ **`bettermemory consolidate --llm`.** The four offline passes (dedup, demote-never-applied, cold-scope suggestions, scope-typo) now have a fifth sibling: cluster related memories, send each cluster + its `claim_excerpts` history to a local Ollama model (default) or to Anthropic / OpenAI (env keys), and let the model propose **merges**, **contradiction resolutions**, **relative-date-to-absolute rewrites** (today's date passed in the prompt so the model doesn't infer it from training data), and **tier demotions** for facts whose verifiable claims have been superseded.

**The audit-transparency moat.** Anthropic's Dreaming consolidates invisibly behind the agent surface — facts move and the model never sees the diff. bettermemory's `--llm` is the opposite: every proposal renders as a unified diff with the LLM's rationale, and `--apply` refuses to commit without either `--yes` (batch accept) or an interactive accept loop (per-proposal y/N prompt). Hallucinated memory IDs (the LLM produces a memory_id that wasn't in the cluster) are rejected at validation time *before* the diff renderer sees them — refusing on principle keeps the audit story clean.

**Default off.** No LLM call unless `--llm` is passed. The structural passes remain the default; `--apply` without `--llm` still commits only the structurally-safe operations. `--llm-provider` is `ollama` by default (localhost, no key), with `anthropic` / `openai` available behind their respective SDKs + env keys.

✅ **`--from-transcript` (writing-reflex gap).** The MCP contract asks the model to call `memory_write` whenever something durable enters the conversation; in practice the bar for "durable" is fuzzy and head-down task focus wins, so most writes get skipped. `bettermemory consolidate --llm --from-transcript PATH` reads the conversation (plain text, Markdown, or Claude Code session JSONL — autodetected) and asks the LLM to propose new memories worth saving. The fifth proposal type, `propose_new`, joins the existing four under the same `--apply`/`--yes`/interactive accept gate. Existing memories ride along as the "don't propose duplicates of these" context; `user-inference` category is forbidden (requires explicit user confirmation the consolidate path can't supply); `source_excerpt` provenance is stamped into the new body so the audit trail traces every claim back to a transcript turn.

## After that — Claude Code auto-memory bridge

**`bettermemory ingest --from ~/.claude/projects/*/memory`.** Claude Code 2.x writes auto-memory to a per-project filesystem directory. Today bettermemory's plugin lands an instruction in the system prompt to consolidate memory in bettermemory's tools instead. The bridge inverts: import any auto-memory that exists, promote it into the scoped/verified/dedup'd bettermemory store, and emit a one-line note in the source directory pointing at the bettermemory ID.

**Why this matters.** Claude Code's auto-memory is on by default and has cultural inertia we can't and shouldn't try to break. Consuming it (rather than fighting it) makes bettermemory a *strict upgrade path* — users keep the ergonomic capture, gain the verification surface. Positioning is "upgrade Claude Code's filesystem memory into the audit layer."

## After that — operational polish

- **Trim the MCP surface where models don't use it.** 18 tools is at the high end of what models reliably engage with. After landing the eval CLI, look at retrieval counts per tool name: anything that's never been called in dogfood usage moves behind a power-user flag and out of the default `instructions` block.
- **Encryption-at-rest option.** Today: plaintext on disk; threat model is OS-level encryption. Some users (and a future linter for credential-shaped strings at write time) want defense in depth. Investigate a `[encrypted]` extra with `age`-backed envelope encryption per file. Likely won't ship in 2026.
- **Status-only `bettermemory ui --tunnel`.** The FastAPI UI is local-only. A `--tunnel` flag wires up a one-shot Cloudflare or Tailscale Funnel tunnel for read-only browsing from another device. No mutations over the tunnel.

## Deliberately out of scope

These come up; here's why they aren't on the list:

- **Managed cloud SKU.** Local-first is a feature, not a constraint; competing with Mem0/Zep/Letta on managed infrastructure means competing on infrastructure I won't run as well as they will.
- **Team-shared multi-user store with permissions.** memctl owns that lane. The `bettermemory sync` git-based pattern handles "the same user across multiple machines" cleanly; "the same store across multiple users with RBAC" is a different product.
- **Knowledge-graph backend.** Zep/Graphiti/Cognee own that lane. Adding a graph backend dilutes the "plain markdown audit-friendly" story without meaningfully closing the gap.
- **Non-MCP SDK / REST endpoint.** MCP is the right protocol; the Linux Foundation + Anthropic + Block + OpenAI governance has stabilized it. Programmatic users can `import bettermemory` directly from Python.
- **Pursuing LongMemEval leaderboard top spot.** OMEGA at 95.4% and agentmemory at 95.2% reflect targeted retrieval-engineering investment that isn't bettermemory's wedge. We'll publish a respectable number once embeddings ship; the goal is "competitive, not first."

## Get involved

The high-leverage contribution shapes:

- **Run `bettermemory eval` against your own usage** once it lands and open an issue with anomalies. The threshold rule for silent-miss detection (`v1_top1_high`) is calibrated against the author's data; recalibrating against more usage patterns is the open question.
- **Integration cookbooks** for clients that aren't Claude Code / Claude Desktop / Cursor / Continue / Cline. The MCP protocol is universal; the friction is the per-client config bit.
- **Memory-rot war stories.** If a stored memory misled you in a way the verification surface didn't catch, the bug report is the most valuable thing — it tells us where the drift extractor, threshold rule, or path-token grammar needs to widen.
