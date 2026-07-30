# Roadmap

Planned work, in rough priority order. Plans change; the
[CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped.

## Planned

- **Write-path hardening, remaining items.** `apply_write_gates` is the
  shared gate chain and `memory_verify` refuses unverifiable path
  attestations (both Unreleased). Remaining:
  1. Reconcile the private gate copies —
     `consolidate._apply_llm_proposal` and
     `handlers/proposals.accept_proposal` — against the shared chain.
     Both deviate from `memory_write` deliberately, so this is policy
     review, not a mechanical reroute.
  2. Provenance on the read surface, after a design change: a tier
     derived from local write events would label an injection-driven
     write `locally-written` — its cleanest tier — so it cannot see the
     reachable attack. Prefer recording what source material was in
     context at write time; `groundedness_check` / `source_transcript`
     are the existing seed.
  3. `sync pull` trust boundary. `sync.py` pulls and re-indexes with no
     content validation, and `SECURITY.md` does not name sync as
     attacker-reachable. The one genuinely remote path.
- **Standing tier.** Opt-in retrieval cannot serve knowledge whose
  trigger condition is not knowing you need it. The `ambient` category
  is still retrieval-gated and `memory_scope_overview` returns counts
  only, so nothing in the product delivers unconditionally. Candidate:
  a hard-budgeted tier delivered at session start — the delivery shape
  `episode_handoff` already uses — under the same verification
  discipline as the rest of the store. Prior art: Letta's core-memory
  blocks; the differentiator is the budget and the verification.
- **Claims-at-write.** A real-prose claim extractor is an open problem
  only because extraction is post-hoc; the author of a memory knows
  what it is claiming at write time. Structured claims on the
  write/verify surface (the shape `verified_paths` already has) would
  give `build_binding_index` real input — which the measured `weak`
  drift tier (2.0 vs 25.1 alerts per catch, corpus-only today) needs
  before it can ship. Backfill is one curation pass over the ~143
  checkable live bodies.
- **Event-time on the memory record.** Every timestamp on `Memory`
  (`created`, `updated`, `last_verified_at`, `last_corroborated`) is
  storage time; nothing represents when a fact is *about*, or when it
  stops being true. `_recency_factor` is deliberately a maintenance
  signal — a 1.1x-capped bump on `max(created, updated)` — not a
  temporal one. Zep's Graphiti ships validity intervals and
  point-in-time queries today, so this is a gap against shipping
  product rather than a nicety.
- **Encryption at rest.** An `[encrypted]` extra with `age`-backed
  per-file envelope encryption, complementing the write-time
  credential check. Not expected in 2026.
- **Relevance-label v2 default flip — w2 dropped; next candidate
  needs a rule-signature change.** The measurement half shipped in
  3.14.0, and three hand-labeling passes over live turns have now
  scored the widening candidates against a ≥~70% precision gate.
  `w1_top1_v2_high` (the bare matched-token floor) was ruled out at
  ~15–30%. `w2_top1_v2_high_from_medium` (promote medium→high only)
  held ~48–54% across three independent windows — ~51% combined over
  79 labeled promotions — and is **dropped as the flip candidate**
  per the recorded band rule. Both stay in `WIDENING_RULES` as
  preview-only baselines; the live label and the shadow contract are
  unchanged. Passes:
  [2026-07-08](eval/widening-labeling-2026-07-08.md) ·
  [2026-07-22](eval/widening-labeling-2026-07-22.md) ·
  [2026-07-29](eval/widening-labeling-2026-07-29.md).

  The successor worth one labeling pass is w2 **minus flags whose top
  hit the same session had just written or updated** — content already
  in context, so an impossible retrieval win. Those are 32% of the
  latest cohort, and excluding them lifts the charitable read to ~71%
  (strict ~38%), which is why it earns a pass and not a flip. It
  cannot be added as a registry entry: `ThresholdRule.check` is a pure
  per-turn predicate, so this needs a per-memory mutation index (from
  the `write`/`update` stream) plumbed through both widening lanes.
  Note for whoever builds it — the exclusion is **not** a session-id
  join: mutation events carry the MCP server session, `turn_audited`
  carries the client session UUID, and the namespaces do not map.
  Once it exists it replays over history already on disk, so no new
  observation window is needed.

## Not planned

- **Managed cloud SKU.** Local-first is the design, not a missing
  feature.
- **Team-shared multi-user store / RBAC.** `sync` handles one user on
  many machines; many users on one store is a different product.
- **Knowledge-graph backend.** Typed links cover what retrieval needs;
  a graph store gives up the plain-markdown format.
- **Non-MCP SDK / REST endpoint.** Programmatic users can `import
  bettermemory` directly — see
  [examples/programmatic_client.py](../examples/programmatic_client.py).
- **Removing `verified_commits` / `verified_versions` in 3.x.** The
  compatibility contract forbids removing a parameter within a major;
  they are documented as audit-trail-only. A 4.0 question at most.
- **Gating the low-use episode tools out of the lean surface.**
  Evaluated against the event log; not available — the shipped plugin
  skill, the system-prompt addendum, and the swarm fan-in path depend
  on them. Rationale at the episode block in `builder.py`; the
  per-turn cost was addressed by trimming `DESC_EPISODE_SEARCH`.

## Contributing

High-leverage contributions:

- Run `bettermemory eval` against your own usage and file anomalies.
  The silent-miss threshold rule is calibrated on one user's data;
  more distributions is the open question.
- Setup notes for MCP clients beyond the five in
  [clients.md](clients.md).
- Reports of stored memories that misled you in a way the verification
  surface did not catch — those locate exactly where the drift
  detection needs to widen.
