# 2026-07-30 — `ingest --force` refused by the gate it was passed to bypass

**Reported by:** self-found, hours after the commit that caused it, by the fresh-eyes write-path audit of 2026-07-30 — and confirmed by running it rather than by reading it.
**bettermemory version at time of report:** 3.30.0 plus unreleased `main`. The defect existed only on `main`: it was introduced by `0073c70` (2026-07-30) and never reached a tag, so no released version ever carried it. `--force` behaved correctly in every version from v2.7.1 through v3.30.0.
**Fixed in:** v3.31.0 (`3b74b18`).
**Status:** fixed

## Symptom

`bettermemory ingest --force` writes nothing and explains itself by telling the operator to pass the flag they just passed:

```
Total files          1
  wrote               0
  skip invalid         1

  [skip_invalid  ] dup.md                                   type=feedback   scopes=imported-from-claude-code,feedback
      write gate refused: duplicate — An existing memory has high content overlap
      with this write. Prefer memory_update on the matched id over creating a
      parallel entry. Pass force=True if the new memory is meaningfully different.
```

`--force` has exactly one documented job, stated in its own `--help`: "Skip the active-store dedup gate." It skipped it in the plan and then met it again at commit. A `--dry-run` of the same command reported `would write 1`, because the plan is all a dry-run has.

This is the mirror image of the two incidents below it in the index. Those were false greens — a check that said "fine" over a store that was not. This is a false red: a check refusing a write the operator had explicitly authorised, with a remedy that had already been applied. The cost to the reader is the same either way. A verdict you cannot act on is not better than a verdict that is wrong.

## Root cause

Two facts that are each correct alone.

**One flag, two gates.** `GateContext.force` is read by `DedupActiveGate` and by `DedupTombstoneGate` in `src/bettermemory/handlers/write.py` — the same field, the same early `return Continue()`. Ingest's contract for `--force` is asymmetric on purpose: bypass the active-store check, never the tombstone check, so a memory the user deliberately removed cannot be resurrected by re-importing the file it came from. That asymmetry is why `ingest._gate_context` could not simply set `force=True`, and it is why it set every override to `False` instead.

**A parameter with nowhere to go.** `apply_ingest_plan` had no `force` parameter, so `cli/ingest.py` forwarded the flag to `compute_ingest_plan` and to nothing else. Before `0073c70` that was the complete implementation: `apply_ingest_plan` ran no write gates at all, so a plan row marked `write` was written, full stop. Bypassing dedup at plan time *was* bypassing dedup.

`0073c70` gave the apply loop the shared content-gate chain — the right change, and one that closed a real hole (a credential or a transient marker in an authored file used to import silently). It also put a second, independent dedup decision underneath the first one, and the flag that governed the first had no way to reach the second.

**Why CI was green through it.** `tests/test_ingest.py::TestDedup::test_force_bypasses_active_dedup_but_not_tombstones` had guarded this flag since `f031f93` (2026-05-24), the commit that introduced it. It ended here:

```python
        # With --force the dedup gate is bypassed; a new write lands.
        plan_force = compute_ingest_plan(..., force=True)
        assert plan_force.rows[0].action == "write"
```

The comment states the behaviour the flag promises. The assertion checks that a *plan* promised it. `apply_ingest_plan` is never called, so nothing in the test could notice that the promise was withdrawn one call later. The companion tombstone test stopped at the same place. `0073c70` added four tests for the new gate chain; three assert that a gate fires and one that a clean row still lands, and none asserts that `--force` still gets past the gate it is aimed at.

## Fix

`apply_ingest_plan` takes `force` and drops `DedupActiveGate` from its gate tuple — it does **not** set `GateContext.force`:

```python
def _content_gates(*, force: bool) -> tuple[Any, ...]:
    if not force:
        return CONTENT_GATES
    return tuple(g for g in CONTENT_GATES if not isinstance(g, DedupActiveGate))
```

Filtering by type is what keeps the asymmetry mechanical rather than conventional. `GateContext.force` would have taken `DedupTombstoneGate` down with it, and no ingest test would have failed: the compute side refuses tombstone twins first, so the apply-side tombstone gate is unreachable in the ordinary case. It becomes reachable exactly when it matters — a memory tombstoned between plan and commit, or a twin only the semantic scorer recognises — which is precisely the case a test written from the ordinary case would not have.

`cli/ingest.py` now forwards the flag to both halves.

No compatibility consequence: `force` is a new keyword parameter defaulting to `False`, and `--force` returns to the behaviour its `--help` has described since v2.7.1.

## Verification

In `tests/test_ingest.py`:

- `TestDedup::test_force_bypasses_active_dedup_but_not_tombstones` — extended past the plan. Applies the forced plan and asserts `written_id` is set, that the id is in `store.load_all()`, and that the store grew to two. This is the assertion the original test's own comment described.
- `TestDedup::test_force_does_not_resurrect_tombstoned_memory` — likewise extended: the replay plan is now applied, and the store must still be empty afterwards.
- `TestDedup::test_force_refuses_a_tombstone_twin_at_apply_time_too` — new, and the one that fails if `GateContext.force` is ever used as the vehicle. It plans while the memory is still active (so the row is `write`), tombstones it, then commits the already-computed forced plan; the row must come back `skip_invalid` with `previously_removed`.
- `TestCLI::test_ingest_force_commits_the_duplicate` — new, through `main()`. The defect was the threading, and both halves work in isolation, so only a test that crosses the CLI boundary can see it.

Each was checked against the unfixed code: reverting the CLI threading fails the CLI test, and reverting the tuple filter to `GateContext.force=True` fails the tombstone-twin test.

## What the surface should do differently

1. **A test that asserts an intermediate artifact does not test the behaviour a flag promises.** A plan is a prediction. Pinning the prediction and calling it coverage is how a flag becomes decorative without a single test turning red — and the give-away was written down in the test itself, in a comment that claimed more than the assertion below it. Where a comment and an assertion disagree about what is being tested, the comment is the specification and the assertion is the bug.
2. **Adding a policy layer under an existing one re-opens every flag that governed the old one.** `0073c70` moved ingest from "no write policy" to "the shared chain" and correctly reasoned about which gates should apply. What it did not enumerate is which *existing overrides* had to keep reaching the new layer — one flag, and it was in the CLI's own `--help`. That enumeration belongs in the checklist for any future path that adopts the shared chain: `accept_proposal` has since adopted it (its overrides enumerated, as `docs/api.md` documents); `consolidate` remains unconverted and carries override flags of its own.
3. **One field governing two gates is a coupling, not a convenience.** `GateContext.force` reads naturally and means two different things to two different callers. Ingest needs half of it; the fix expresses that by choosing gates instead of setting flags, which is checkable by a type filter rather than by remembering. A flag whose two readers can diverge should be two flags, or the divergence should be expressed structurally — as it now is here.

## References

- Introduced: `0073c70`, "feat(write): shared write-gate chain; ingest now runs the content gates" (2026-07-30, unreleased).
- `--force` and its plan-only guard test: `f031f93` (2026-05-24), first released in v2.7.1.
- Found by: the write-path lane of the 2026-07-30 audit, filed as item F1 of that round's plan. The plan and its fact packs were retired once drained; the fix and this postmortem are the record.
- Related code: `_content_gates` and `apply_ingest_plan` in `src/bettermemory/ingest.py`; `DedupActiveGate`, `DedupTombstoneGate` and `CONTENT_GATES` in `src/bettermemory/handlers/write.py`; the flag's threading in `src/bettermemory/cli/ingest.py`.
- Related incidents: [`2026-07-26-staleness-verdict-constant-function.md`](2026-07-26-staleness-verdict-constant-function.md) and [`2026-07-25-doctor-false-green-on-importable-extra.md`](2026-07-25-doctor-false-green-on-importable-extra.md) — both false greens; this one is the same class inverted.
