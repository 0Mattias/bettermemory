# Roadmap

Planned work, in rough priority order. Plans change; the
[CHANGELOG](../CHANGELOG.md) is the source of truth for what shipped.

## Planned

- **Encryption at rest.** An `[encrypted]` extra with `age`-backed
  per-file envelope encryption, complementing the write-time
  credential check. Not expected in 2026.
- **Read-only `bettermemory ui --tunnel`.** One-shot Cloudflare or
  Tailscale Funnel for browsing from another device. No mutations over
  the tunnel.
- **Relevance-label v2 default flip.** The forward-looking half
  shipped in 3.14.0: every audited turn now logs its top hits' raw
  coverage features plus the shadow `relevance_v2` label (coverage
  fraction OR an absolute matched-token floor), and `bettermemory eval
  --widening-preview` replays candidate rules over that stream. What
  remains is the flip itself — promote the v2 formula to the live
  `relevance` label (unlocking `expand_top` and the miss probe on the
  long-query cohort) once a few weeks of live calibration data show an
  acceptable widening delta.

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
