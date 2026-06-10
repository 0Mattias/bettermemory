# Extractor false-signal hunt — 2026-06-09 (DRAINED 2026-06-10)

Provenance: a 4-round multi-agent hunt (224 agents; 10 heuristic surfaces;
every finding adversarially verified with a runnable repro by an independent
agent) over heuristic-extraction false signals, run at HEAD 874b0b0 / v3.8.0.
The full finding detail (description, example input, verifier notes,
suggested fix) remains in the sibling JSON, kept as the archival artifact:
`extractor-hunt-2026-06-09.json`.

## Disposition

The first 24 findings (all `verify.py` path-extraction classes, two HIGHs,
two credential-coverage gaps) shipped in **v3.9.0**. The remaining **146**
were drained in the 2026-06-10 audit-loop rounds 84–86, each finding
re-triaged against then-current HEAD by an independent skeptic, fixed in
file-disjoint parallel batches, and adversarially verified:

- **~122 fixed** with a regression test each (111 in the round-84 per-module
  drain + the cross-module defers landed in rounds 85–86, including the
  repairs the adversarial verifiers forced: a credentials timestamp
  false-positive regression, a groundedness test-dodge, and a proposals
  telemetry production no-op).
- **16 refuted** as documented conservative trade-offs after independent
  re-judgment (e.g. `i just`/`we just` markers kept on live override-rate
  telemetry; the diceware false negative kept because class-mix is the
  credential gate's documented precision anchor; the eval `v1` rank-1-only
  threshold kept because `turn_audited` events carry no `top_hits`, making a
  looser rule uncalibratable from existing logs).
- **2 already fixed** en route by earlier batches in the same drain.
- **The rest parked as feature-class work** (see below) — proportionate
  fixes need new architecture, not heuristic tweaks.

Per-finding dispositions are journaled in the audit-loop episodes
(scope `projects:bettermemory`, 2026-06-10) and the queue memory
(`audit-loop-state`).

## Parked (feature-class residuals)

- **Tokenization v2** — CJK bigram segmentation, stemming, multilingual
  stopword lists, and tokenizer-level apostrophe normalization in the shared
  pipeline (`search.py` + `index.py` FTS `SCHEMA_VERSION` bump; consumers:
  groundedness, consolidate, audit). One coherent feature covering the six
  deferred segmentation/inflection findings.
- **Write-time origin hints for Desktop / web-UI writes** — the ingest leg
  landed in round 85; the other two write paths need an explicit origin-hint
  surface (design work, not capture plumbing).
- **Proposals reviewed-excerpt tombstone** — a dismissed proposal re-proposes
  on every recurrence; fixing it is a new on-disk artifact with its own
  lifecycle decisions.
- **`ssh -G` host-alias resolution** for origin matching — declined: a new
  external-binary dependency class with environment-dependent semantics.
