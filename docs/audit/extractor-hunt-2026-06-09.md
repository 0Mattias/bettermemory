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

One JSON entry was **listed in error and never open**: *"worktree_root
strict equality hides ALL memories for a repo after the checkout moves,
is re-cloned, or the store syncs to another machine"*
(`src/bettermemory/origin.py`). It is the medium-rated twin of the HIGH
that b0ab779 — the parking commit itself — fixed, shipped in v3.9.0:
`worktrees_match` gained the linked-worktree and dead-worktree
relaxations, so a memory whose recorded `worktree_root` no longer exists
degrades to repo-level matching. Re-measured at 3.28.0 against the
surface rather than the helper: a memory written in one checkout still
comes back from `memory_search`/`memory_scope_overview` after the
directory is renamed, and after arriving over `sync` carrying another
machine's absolute path; a second checkout that is still live on disk
stays isolated, which is the boundary of the degrade. Those three are
pinned end-to-end in `tests/test_server_origin.py` — read them rather
than the JSON entry for the current behaviour.

Per-finding dispositions are journaled in the audit-loop episodes
(scope `projects:bettermemory`, 2026-06-10) and the queue memory
(`audit-loop-state`).

## Parked (feature-class residuals)

- **Tokenization v2** — ~~CJK bigram segmentation, stemming, multilingual
  stopword lists, and tokenizer-level apostrophe normalization in the shared
  pipeline (`search.py` + `index.py` FTS `SCHEMA_VERSION` bump; consumers:
  groundedness, consolidate, audit). One coherent feature covering the six
  deferred segmentation/inflection findings.~~ **Shipped in v3.12.0**
  (2026-07-02): CJK bigrams, a light plural stemmer, sv/de/fr/es stopword
  sets, full-width sentence boundaries in groundedness, and FTS schema v4
  (the index now stores the pipeline's own token stream, making
  prefilter/ranker parity structural). The apostrophe-normalization leg had
  already landed earlier as `search._CONTRACTION_RE` +
  groundedness's `_APOSTROPHE_RE`; v3.12.0 closes the remaining five
  findings — each carries a regression repro in the suite.
- **Write-time origin hints for Desktop / web-UI writes** — the ingest leg
  landed in round 85; the other two write paths need an explicit origin-hint
  surface (design work, not capture plumbing).
- **Proposals reviewed-excerpt tombstone** — a dismissed proposal re-proposes
  on every recurrence; fixing it is a new on-disk artifact with its own
  lifecycle decisions.
- **`ssh -G` host-alias resolution** for origin matching — declined: a new
  external-binary dependency class with environment-dependent semantics.
