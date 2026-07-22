# Roadmap

Planned work, in rough priority order. Plans change; the
[CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped.

## Planned

- **Encryption at rest.** An `[encrypted]` extra with `age`-backed
  per-file envelope encryption, complementing the write-time
  credential check. Not expected in 2026.
- **Relevance-label v2 default flip — now targeting the tightened
  w2 formula.** The measurement half shipped in 3.14.0; the first
  live calibration read (2026-07-08, 103 replayable turns,
  hand-labeled via the `--widening-preview --detail` lane added in
  3.16.0) was decisive: the bare matched-token floor
  (`w1_top1_v2_high`) flagged 32 turns at roughly 15–30% precision.
  Its v1-low→high promotions — long pasted messages crossing
  `matched_unique >= 4` against any domain-adjacent memory at
  coverage ~0.2 — were almost pure noise, while its v1-medium→high
  promotions read ~50% precision and contained every clearly-real
  catch. `w2_top1_v2_high_from_medium` (promote medium→high only)
  is the flip candidate now; w1 as-is is ruled out. Methodology and
  aggregates: [eval/widening-labeling-2026-07-08.md](
  eval/widening-labeling-2026-07-08.md). The second pass
  (2026-07-22, 37 newly-accrued promotions) read ~54% charitable /
  ~30% strict — the same ~50% picture on 3.4× the data:
  [eval/widening-labeling-2026-07-22.md](
  eval/widening-labeling-2026-07-22.md). Holding per the 50–70%
  band rule; the ~mid-August pass is decisive — flat again means
  drop, unless a refined candidate (scope-token-gated promotion,
  same-session top hits excluded) supersedes w2 first.

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
