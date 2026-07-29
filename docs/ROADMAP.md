# Roadmap

Planned work, in rough priority order. Plans change; the
[CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped.

## Planned

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
