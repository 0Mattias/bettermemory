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

One JSON entry was **filed and fixed by the same commit**, so it should
not have been in the parked 146: *"worktree_root strict equality hides
ALL memories for a repo after the checkout moves, is re-cloned, or the
store syncs to another machine"* (`src/bettermemory/origin.py`, severity
`medium`). Not "never open", as an earlier revision of this section
said: at the hunt's base 874b0b0 — the commit v3.8.0 tags —
`worktrees_match` was plain string equality, and the reported blackout
reproduces against that core. b0ab779, the commit that
wrote this queue, gave the same helper its linked-worktree and
dead-worktree relaxations in that one change, shipped in v3.9.0 as the
"Auto-scope (HIGH): linked-worktree blackout" entry; the dead-worktree
leg is what reaches this finding, degrading to repo-level matching once
the recorded `worktree_root` no longer exists on disk. The HIGH it
shares that helper with is named in b0ab779's message and in the v3.9.0
changelog, not in the JSON — the parked file carries no HIGH entries at
all (124 medium, 22 low).

Re-measured at 3.28.0 (in 5a960a3) against the surface rather than the
helper: a memory written in one checkout still comes back from
`memory_search`, and still counts in `memory_scope_overview`'s `total`,
after the directory is renamed and after arriving over `sync` carrying
another machine's absolute path; put the pre-3.9.0 equality core back and
the same probe's search comes back empty with `total` at 0. A second checkout still live on disk stays isolated —
the boundary of the degrade, not a hole in it. Those three cases are
pinned end-to-end in the checkout-path-lifecycle section of
`tests/test_server_origin.py`
(`test_project_memories_survive_a_checkout_move`,
`test_synced_memory_from_another_machine_surfaces_locally`,
`test_second_live_checkout_of_one_repo_stays_isolated`), with
`test_dead_worktree_memory_degrades_to_repo_match` in
`tests/test_origin.py` as the unit-level twin.

One site the entry named does NOT inherit that degrade:
`_count_recent_tombstones`
(`src/bettermemory/handlers/scope_overview.py`) still compares the
recorded and caller `worktree_root` with raw `!=`. Measured on the same
rename, `memory_scope_overview`'s `recently_removed_in_worktree` goes
1 → 0 while `total` stays 1, and `auto_scope=False` counts it again.
That field is worktree-scoped by name and by its own docstring, so
whether it should follow the relaxation is an open question rather than
a filed defect. Which splits how to read the entry: on retrieval it
describes v3.8.0's helper and the tests above are the record of current
behaviour; on the tombstone window its description still holds.

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
