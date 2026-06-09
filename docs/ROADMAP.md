# bettermemory roadmap

A published roadmap is part of the pitch: people choosing between memory layers want to know where a project is going, not just where it's been. This lists the planned work in rough priority order. Plans change — **the [CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped.**

## Where we are — v3.8.0 (June 2026)

The core is built and battle-hardened. The differentiated surface is live. After a run of correctness sweeps, 3.7.0 rotated back to feature work — execute-the-curation, onboarding, retrieval-quality levers, and the eval driver that unblocks the comparative — and 3.8.0 added the write-time credential check that keeps secrets out of the plain-text store.

- **25 MCP tools, 18 registered by default** (the lean surface since 3.4.0 — seven curation/power-user tools gate behind `[behavior] full_tool_surface`, six with a direct CLI counterpart — including `memory_curate`, which wraps `consolidate` — and `memory_acknowledge_miss` MCP-only); four of the always-on defaults are the `episode_*` journal / run-state tier.
- **Staleness verdict on every retrieval** — calendar age + filesystem path drift + git commit drift folded into one `staleness_verdict`, with the inline `path_drift = {checked, missing, verified}` list.
- **Claim-level audit trail** — `memory_record_use(claim_excerpts=…)` plus Stop-hook substring attribution (`attribution ∈ {model, hook, auto}`, exactly one event per retrieval) and the `memory_audit_turn` silent-miss probe.
- **Hybrid retrieval** (RRF over keyword + BM25, plus semantic via the `[embeddings]` or `[embeddings-fast]` extra) as the zero-dep default.
- **Dreaming-defense consolidation** — `bettermemory consolidate --llm` (merge / resolve-contradiction / rewrite-date / demote) and `--from-transcript` (propose-new), every change a reviewable diff under an audit gate.
- **The eval surface** — `bettermemory eval` reports `memory_helped_rate` / `endorsement_rate` / `silent_miss_rate` with Wilson 95% CIs; `--tool-usage` and `--threshold-sweep` back the calibration decisions. The comparative harness lives at `tests/eval/`.
- **Compounding across agents and time** — the `episode_*` journal tier, swarm fan-in (`episode_write` / `episode_search(swarm_id=…)`), opt-in auto-consolidation, and write-reflex proposals (`memory_proposals` + `[proposals]`).
- **Scale & ops** — FTS5 inverted index above ~500 memories, git-based cross-host `sync`, a FastAPI curation UI, and a `doctor` that catches the common install failures.
- **Hardened** — whole-codebase correctness audits (3.3.4, 3.5.0, 3.6.5), concurrent multi-agent store access (3.2.0), and cross-platform (incl. Windows) fixes, each landed with a regression test. ~2,090 tests, 80% coverage floor, Python 3.11–3.14, MIT.

## Recently shipped

The CHANGELOG has the release-by-release detail; the arc, by theme:

- **Secrets stay out of the store (3.8.0).** A write-time credential check refuses a `memory_write` whose body embeds a secret-shaped token — vendor-prefixed API keys (AWS / OpenAI-Anthropic / GitHub / Slack / Google / Stripe), a private-key PEM block, a JWT, or a guarded `password=…` assignment. The store is plain-text markdown that `sync` pushes across hosts via git, so a pasted key would otherwise rot there unencrypted and in the audit log; the matched value is redacted from both the warning and `.events.jsonl`. Precision-first (it never fires on prose that merely *mentions* "api_key"), default-on, with `acknowledge_credential=True` for a documented public/example value — structurally the same gate shape as the durability check.
- **Post-release diff-audit (3.7.1).** A reactive adversarial sweep of the 3.7.0 feature commits caught three real issues and fixed them: a default-on per-hit SQLite open in the new `supersedes`/`contradicts` search activation (batched into one connection via `index.links_for_many`), and two honesty defects in the live-agent eval driver — `searched` was derived from `bool(hits)` (a ranker tautology that collapsed the live `silent_miss_rate`) and citation excerpts were validated against the truncated snippet instead of the body. Each landed with a regression test, and the fixes were themselves diff-audited.
- **Rotating onto value (3.7.0).** After the 3.6.x correctness sweeps hit diminishing returns, a feature release: `memory_curate` (execute the cleanup `memory_health` only diagnosed, dry-run by default), `bettermemory try` (a 60s offline staleness-verdict demo), opt-in usage-aware ranking (`endorsement_boost`), the long-dormant `supersedes`/`contradicts` links activated as retrieval trust signals, and the live-agent eval driver that closes the comparative's open piece. Plus a subtraction pass that removed accreted duplication (adversarially verified, so deliberate defense-in-depth stayed put).
- **Recall parity without a database (2.5.0).** The `[embeddings-fast]` extra wraps fastembed + ONNX Runtime (~50 MB) as a drop-in for the `[embeddings]` torch path (~500 MB), closing the "FTS5-only loses recall benchmarks" objection without compromising the no-database default.
- **The eval & audit surface (2.5.0 → 2.7.0, harness in 3.3.3).** The metric trio with confidence intervals, the per-tool call-count rollup (`--tool-usage`), counterfactual silent-miss replay (`--threshold-sweep`), and the comparative harness at `tests/eval/` (bettermemory runs locally; competitors ride as honest capability-matrix stubs).
- **Dreaming defense via local consolidation (2.5.0, `--from-transcript` 2.6.0).** Where Anthropic's Dreaming consolidates invisibly, `--llm` renders every proposal as a diff with rationale and refuses to commit without explicit accept.
- **Compounding memory (3.1.0 → 3.3.0).** The `episode_*` tier, swarm fan-in for multi-agent run-state, opt-in self-improving consolidation, and the write-reflex proposal queue — all off by default, all auditable.
- **The lean default surface (3.4.0).** Dropped the default tool count from 24 to 18 on dogfood evidence (`eval --tool-usage`), trimming the per-turn tool-description context the project exists to minimise.
- **Battle-hardening (3.2.x → 3.6.5).** Concurrent-access store hardening, whole-codebase correctness audits, and a run of Windows / cross-platform fixes — including correctness gaps where a headline feature was silently not firing in practice. The 3.6.5 sweep was the first run *at the shipped tree* (every source file read end to end, not just the recent diff), catching a whole-store read-path DoS and an embedding fail-open that diff-only audits had never looked at.

## Planned

- **Publish the comparative numbers.** The harness exists and the live-agent *driver* has landed (`tests/eval/driver.py`: an `Agent` protocol + `run_driver` that turns real cite-decisions into the full trio, with a deterministic `ScriptedAgent` proving the compute path in CI and a key-gated `LiveAgent` for the real run). What remains is running the `LiveAgent`, wiring live competitor runs (Mem0 / claude-mem / Anthropic's reference server, and agentmemory as a new adapter), and the write-up. *Why it matters:* every other comparison article in this market measures retrieval recall. Owning *"did memory shape the reply?"* is the lane-claim, and a published comparative is its grounding artifact. The goal is "competitive, not first" — OMEGA and agentmemory sit at ~95% on LongMemEval off targeted retrieval-engineering that isn't this project's wedge.
- **Encryption-at-rest option.** Today the threat model is OS-level disk encryption. A `[encrypted]` extra with `age`-backed per-file envelope encryption would add defense in depth — the natural complement to the write-time credential check (3.8.0), which keeps an *accidental* secret out but doesn't encrypt the deliberate store. Likely won't ship in 2026.
- **Status-only `bettermemory ui --tunnel`.** A one-shot Cloudflare or Tailscale Funnel for read-only browsing from another device. No mutations over the tunnel.

## Deliberately out of scope

These come up; here's why they aren't on the list:

- **Managed cloud SKU.** Local-first is a feature, not a constraint. Competing with Mem0 / Zep / Letta on managed infrastructure means competing on infrastructure I won't run as well as they will.
- **Team-shared multi-user store with RBAC.** The `bettermemory sync` git pattern handles "one user, many machines" cleanly; "many users, one store, with permissions" is a different product.
- **Knowledge-graph backend.** Zep / Graphiti / Cognee own that lane. A graph backend dilutes the "plain markdown, audit-friendly" story without meaningfully closing the gap.
- **Non-MCP SDK / REST endpoint.** MCP is the right protocol and its governance has stabilized. Programmatic users can `import bettermemory` directly — see [`examples/programmatic_client.py`](../examples/programmatic_client.py).

## Get involved

The high-leverage contribution shapes:

- **Run `bettermemory eval` against your own usage** and open an issue with anomalies. The silent-miss threshold rule (`v1_top1_high`) is calibrated against the author's data; recalibrating against more usage patterns is the open question.
- **Integration cookbooks** for clients beyond Claude Code / Claude Desktop / Cursor / Continue / Cline. The protocol is universal; the friction is the per-client config bit.
- **Memory-rot war stories.** If a stored memory misled you in a way the verification surface *didn't* catch, that bug report is the most valuable thing you can file — it tells us where the drift extractor, threshold rule, or path-token grammar needs to widen.
