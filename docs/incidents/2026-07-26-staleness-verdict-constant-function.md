# 2026-07-26 — staleness-verdict constant function

**Reported by:** self-found via `bench/rot`, which named it the most actionable finding it produced. No issue — nobody outside could have seen it.
**bettermemory version at time of report:** 3.29.0. The branch responsible was present from `252b0fa` (2026-05-10), the commit that introduced `compute_staleness_verdict`, and shipped in every release from v1.4.1 onward.
**Fixed in:** v3.30.0 (`58a4fa4`)
**Status:** fixed

## Symptom

`staleness_verdict` is the field this project tells consumers to branch on first. Past `verification_stale_days` (default 30) it was not a signal at all: a `never`/`stale` verification status pre-empted **both** drift inputs, so every calendar-stale memory reported `spot_check_required` no matter what path drift and commit drift had actually found.

Nothing looked broken from the outside — the field fires, and a flag is what a reader expects from an old memory. `bench/rot` is what made it visible. Measured against this project's own history, the `shipped_default` arm flagged 100% of claims in every class and both pinned windows: Youden's J = 0.000, Fisher p = 1.000 — arithmetically identical to `always_flag`. Both `shipped_default` rows, pre-fix and post-fix, are kept side by side in the reference-classifier table in `bench/rot/README.md` so the fix is auditable rather than retroactive.

The detector could not be wrong, which is the same thing as saying it could not be right. Every leg carrying discrimination was unreachable in the configuration most users run.

## Root cause

**Calendar age** erasing **commit drift** — one branch, not a mistuned threshold.

`compute_staleness_verdict`, `src/bettermemory/verify.py` as of `58a4fa4^`:

```python
if verification.status in _VERDICT_RAISE_STATUSES:
    return _VERDICT_REQUIRED
drifty = path_drift_missing > 0 or (
    commit_drift_count is not None and commit_drift_count > 0
)
```

The early return sat above the `drifty` computation, so past the window the drift inputs were dead parameters.

The first diagnosis blamed the 400-day anchor `bench/rot` uses for that arm. It was not the anchor: no anchor value could have reached the drift legs. The commit-drift leg's own documentation had stated the intended division of labour from the other side all along — a memory with no path-shaped claims is exempt from commit drift, and *"calendar staleness remains the backstop for that class"* (`commit_drift_anchor_paths` in `src/bettermemory/verify.py`). A backstop is what you fall back to when the measurement cannot speak, not something that overrides the measurement when it can.

Second-order cause, and the one most likely to recur: `_response.attach_commit_drift_counts` re-implemented the same ladder against shared constants for the per-search recompute, guarded only by comments instructing the reader to mirror the gate in `compute_staleness_verdict` — three of them, by the fix commit's count. They named the hazard exactly and prevented nothing.

## Fix

`58a4fa4`. A calendar-stale memory now reads `fresh` when its commit-drift leg returns a **measured zero** — no commit touched anything the memory cites since its own `last_verified_at`, which is the question the calendar leg is a crude proxy for. The measurement wins; the proxy yields. Drift still raises the verdict exactly as before, so the change only ever lowers one.

Three guards keep the demotion from becoming a false green, and they are the load-bearing part:

- **`never` never demotes.** No anchor means no "since when", so a zero would be meaningless rather than reassuring.
- **`commit_drift_count is None` never demotes.** `None` means the leg could not ask — no origin repo, caller standing elsewhere, no anchor. Absence of evidence is not evidence of freshness, and this is what keeps preference and lesson memories loud.
- **Path existence alone never demotes.** "The file still exists" is a weaker question than "nothing touched it since you checked".

The ladder now lives in exactly one place: `verdict_from_signals` in `src/bettermemory/verify.py`, new public API, called by both `compute_staleness_verdict` and `_response.attach_commit_drift_counts`.

No migration. The verdict is computed per read from the stored verification status; nothing on disk changes shape.

## Verification

In `tests/test_verify.py`, one test per guard, each mutation-checked — dropping the measured-zero requirement or the `never` carve-out fails exactly the case it names:

- `test_verdict_stale_demotes_on_measured_zero_commit_drift`
- `test_verdict_stale_does_not_demote_when_commit_leg_silent`
- `test_verdict_stale_does_not_demote_on_path_evidence_alone`
- `test_verdict_never_never_demotes`
- `test_verdict_drift_still_raises_a_stale_memory`
- `test_compute_staleness_verdict_delegates_to_primitive` — the one-implementation invariant, so the two emission sites cannot desync again

In `tests/test_bench_rot.py`, `test_the_shipped_default_is_not_a_constant_function` pins the invariant that says the pre-emption is gone: **arm convergence**. `shipped_default` must score exactly what `drift_only_relative_cite` scores — the arm with the calendar leg disabled — and its `ALL` flag rate must stay below 1.0. It reads the committed results rather than re-running the corpus, so it costs nothing in CI:

| window artifact | claims | `shipped_default` ALL flag rate | J before | J after |
| --- | --- | --- | --- | --- |
| `bench/rot/results/bettermemory-30d-2026-07-26.json` | 820 | 89.2% | 0.000 | 0.111 |
| `bench/rot/results/bettermemory-60d-2026-07-26.json` | 675 | 96.7% | 0.000 | 0.034 |

The "after" columns are the committed artifacts, re-readable at any time. The "before" column is not — the fix overwrote both files — so it is pinned in prose instead: the 60d row survives verbatim in `bench/rot/README.md`'s reference-classifier table, and `58a4fa4`'s body carries both windows. Where a number here cannot be re-derived from an artifact, that is the reason and this is the source.

The ceiling did not move. J = 0.034 is the same weak signal the informative arm always had; the default now *reaches* it instead of being erased before it gets there. The other arms, the claim-level detectors and the reference baselines reproduce identically, which is the result-neutrality evidence for everything not targeted.

`test_a_silent_commit_leg_is_not_reported_as_a_measured_zero` guards a side finding: `bench/rot`'s own `verdict_for` passed `0` for a silent commit leg, conflating "could not ask" with "measured zero". Harmless before the fix, because the calendar pre-empted everything — after it, that conflation would have manufactured a false green *inside the instrument that measures the guard against false greens*. It never fired in either published window, so the numbers stand either way.

Live-store check before shipping, recorded in `58a4fa4`'s commit body: at the shipped default, zero of 209 memories changed verdict. Forcing the window to 0 days demoted 18 of 209 — the `None` guard blocked the other 191, so the demotion is narrow by construction rather than by luck. Two of the eighteen were spot-checked against raw `git log`: 0 commits touched their anchors while 181 and 99 landed repo-wide.

## What the surface should do differently

1. **`None` and `0` are load-bearingly different, everywhere.** "Could not ask" and "asked and got zero" must never collapse into the same value. The same conflation appeared twice in one day — once in the verdict ladder, once in the benchmark that measures it — which suggests the type, not the discipline, should carry it.
2. **A backstop must not outrank the measurement it backs up.** Any future signal added to the rollup needs an explicit answer to "what happens when this and the measurement disagree", written down before it ships.
3. **Ship no rollup without a reference classifier beside it.** `always_flag` and `never_flag` both score J = 0.000; a detector indistinguishable from either is not a detector. That comparison was computable from the day the verdict was introduced, and nothing computed it until `bench/rot` existed.
4. **Mirrored implementations guarded by comments are a defect with a countdown.** The copy carried explicit instructions to mirror `compute_staleness_verdict`, and the semantic change still landed on one site only. Share the primitive.

## References

- Fix: `58a4fa4`, "fix(verdict): the calendar leg was erasing the measurement it exists to back up" — CHANGELOG section "Fixed — the staleness verdict was a constant function at the shipped default" under `## 3.30.0 - 2026-07-26`.
- Introduced: `252b0fa` (2026-05-10), the commit that added `compute_staleness_verdict`.
- Benchmark: `bench/rot/README.md` — reference-classifier table (both `shipped_default` rows) and item 4 of its open-work list, which is the still-unsolved half: evidence of a different *kind*, not a better threshold.
- Related code: `verdict_from_signals` and `compute_staleness_verdict` in `src/bettermemory/verify.py`; `attach_commit_drift_counts` in `src/bettermemory/_response.py`.
- Related incidents: [`2026-07-25-doctor-false-green-on-importable-extra.md`](2026-07-25-doctor-false-green-on-importable-extra.md) — the same failure class one day earlier, a green computed over the wrong input.
