# Roadmap

Planned work, in rough priority order. Plans change; the
[CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped.

## Planned

- **Truncation as a write-time gate, deferred on budget.** `doctor`'s
  `memory_body_completeness` check reports bodies that end mid-sentence,
  which is detection after the fact. The gate that would prevent the loss
  belongs on `memory_update`, where both the old and the new body are in
  hand: return `status="truncation_warning"` when a body edit shrinks the
  record and ends mid-sentence, with `acknowledge_truncation=True` as the
  override, mirroring `credential_warning`.
  **What blocks it is measured, not doubt about the value.** The predicate
  is 0.4% false positive and 93.9% recall on a 234-record store, and a
  false positive costs one round-trip. But the parameter plus a
  description sentence on `memory_update` runs ~112–150 characters against
  **127 characters** of margin before `_DESC_BUDGET_PRESSURE` warns
  (25,773 live against a 26,000 ceiling). Ship it in a cycle that also
  frees DESC budget, or alongside a ratchet-down; do not spend the last
  of the margin on it.
  Rejected alternatives, recorded so they are not re-derived: a
  "new body is a strict prefix of the old" guard is 0% false positive but
  misses the incident that motivated this (it was a rewrite that got cut,
  not a prefix); "new body is >30% shorter" false-positives on condensing
  edits, the single most common update shape on the dogfood store.
- **Write-path hardening, remaining items.** `apply_write_gates` is the
  shared gate chain and `memory_verify` refuses unverifiable path
  attestations (both shipped in 3.31.0). Remaining:
  1. Reconcile the private gate copies against the shared chain.
     `handlers/proposals.accept_proposal` now calls `apply_write_gates`;
     `consolidate._apply_llm_proposal` still hand-rolls size, transient
     and similarity, each with a deliberate rationale (it gates the LLM
     claim, not the stamped body), so that half is policy review rather
     than a mechanical reroute. `ingest.apply_ingest_plan` is the third:
     it bypasses `_validate_write_payload` entirely, and the residue
     worth closing is that it can plant scopes outside a configured
     `[scopes] allowed` whitelist.
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

### Small and anchored

Each of these is scoped and has a known landing site; none is large
enough to earn its own entry above.

- **The transient-reject hint never names `episode_write`.** The hint in
  `handlers/write.py` offers two remedies — rephrase to the durable form,
  or `acknowledge_transient=True` — and does not mention the tier that
  exists to hold exactly that content. The reverse direction is already
  covered in `handlers/episode_write.py`, so the gap is one-way. No test
  pins the string and reject hints are runtime payloads, so this costs
  nothing against the resident budget.
- **`SYSTEM_PROMPT_ADDENDUM` predates the episodes-as-state-channel
  rewrite.** The block in `prompts.py` is mirrored byte-for-byte in
  `docs/system_prompt.md` and pinned equal by `tests/test_prompts.py`. It
  still describes episodes as a place you *may* use, and its loop-iteration
  pattern predates both the cheap-scan parameters and the minting moment
  that `plugin/skills/bettermemory/SKILL.md` now leads with. Target
  net-neutral, not net-add: `docs/system_prompt.md` warns that Claude Code
  truncates the paste at ~1.8 KB.
- **`cold_endorsement_memories` is not gated by the telemetry-coverage
  predicate, and that is a decision, not an implementation.** Only
  `dead_weight` is gated in `health.py`. On a hookless store the
  cold-endorsement leg over-fires exactly where the gate exists to stop
  over-firing, and it reaches the model through the curation hint's
  pressure sum — the one curation surface delivered without asking. Two
  viable shapes: widen the coverage gate to cover the leg, or drop the leg
  from the pressure sum when coverage is absent. Hard constraint: the
  `curation_pending` key set is pinned by set-equality, dict-equality and a
  DESC-prose regex, so the fix must not add a key.
- **`doctor`'s `turn_audited` count does not check `triggered_from`.** An
  in-process `memory_audit_turn` stamps `triggered_from="mcp_tool"`, so an
  MCP-only store reads as "hook is wired" — the conflation fixed everywhere
  else. Three verdict branches and a published info key read the counter,
  so tightening it changes what `doctor` says on stores that are genuinely
  fine but MCP-driven.
- **`episode_search(ids=…)` has no by-filename fast path, and the win is
  unmeasured.** The refusal is written into `handlers/episode_search.py`
  with its reasoning, and the validator to mirror already exists in
  `episodes.py`. Two things to know first: on a bare `ids`-only call the
  candidate loop still walks every session, so the saving is proportional
  to session count rather than to 1; and three behaviours ride the
  post-load loop (the floor skip, the `since`/scope filters, the datetime
  sort) that any short-circuit must preserve. Bench before building.

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
- **A "core" tool-surface preset — a third registration tier below the
  default lean surface.** Measured and closed. Every tool such a preset
  would drop is named by shipped guidance as a call the model is
  supposed to make, so a genuinely flow-complete core *is* the lean
  surface and saves nothing: `memory_show` is the rebase step both
  optimistic-concurrency stale hints hand back, `memory_remove` is the
  only action `memory_health`'s two largest recommendations offer and
  it is the one tool with no `bettermemory` CLI counterpart to fall
  back on, `memory_scope_disable` is instructed verbatim by the
  system-prompt addendum and the plugin skill (and
  `memory_scope_enable` is its documented undo), and `memory_list` sits
  in the addendum's tool headline and in `memory_audit_turn`'s
  retrieval-event set. Dropping all five anyway is 9% of the resident
  tool surface and breaks four of those. Meanwhile a schema-deferring
  client already pays under 1% by listing tool names and fetching
  schemas on demand — the same win, two orders of magnitude larger, for
  free — which is why the server's instructions block names the four
  tools to load first instead. Rationale next to the knob in
  `config.py`; the per-tool figures come from
  `tests/test_resident_footprint.py`, which measures them on every run.
- **Merging the micro-tool pairs in 3.x** — `memory_write_confirm` /
  `memory_write_cancel` and `memory_scope_enable` /
  `memory_scope_disable` into one call each. The compatibility contract
  forbids removing a tool within a major, and the economics are
  backwards without the removal: a merged replacement can only be
  *added* in a minor, so inside 3.x it would grow the description
  budget rather than shrink it. A 4.0 question — deprecation cycle
  first, removal at the major with migration notes.

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
