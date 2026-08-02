# 2026-07-31 — index health certified a stale index

**Reported by:** self-found, by an audit pass pointed at `bettermemory doctor` itself. No issue — the check reported green, which is the whole finding.
**bettermemory version at time of report:** 3.33.0. The branch responsible was present from `0eab9a7` (2026-07-03), the commit that introduced the `index_health` check, and shipped in every release from v3.13.0 onward.
**Fixed in:** 3.34.0
**Status:** fixed

## Symptom

`index_health` is the instrument that tells the reader the search index still describes the memories on disk. Asked about a store whose index had stopped describing it, it said this:

```
✓ index_health: Index healthy: 2 memories indexed (matches disk; PRAGMA quick_check passed).
```

The store behind that line held one memory the index had never seen and one index row for a memory that had been deleted. "Matches disk" was a claim about a row count. Nothing had compared the index to disk in any other sense.

Two shapes reproduce it, both reachable through the workflow this project advertises as its differentiator — one file per memory, grep-able and hand-editable ("Storage" in `docs/internals.md`).

**Identity swap.** Write two memories through the Store API, then remove one file and drop another in by hand:

```
✓ index_health: Index healthy: 2 memories indexed (matches disk; PRAGMA quick_check passed).
```

**Hand-edit.** Write one memory, then edit its scopes and its body in place — same id, same filename, same count:

```
✓ index_health: Index healthy: 1 memories indexed (matches disk; PRAGMA quick_check passed).
```

The index still held the pre-edit row:

```
('01KYXG8EQ2K6HNP5KP87M12RTV', '["tools"]', 'deploys go through the staging pipeline\n')
```

while the file on disk read `scopes: [infrastructure]` and `deploys go straight to production, no staging`. The two statements are opposites, one of them is the one the store serves, and doctor's answer was a checkmark.

The startup divergence warning was silent on both stores. Constructing a `Store` against each and collecting everything `store._INDEX_LOG` emitted:

```
repro  -> []
repro2 -> []
```

### Blast radius

Bounded by store size, and the bound is stated exactly below.

The maintainer's live store holds 239 memories. `_handlers._INDEX_THRESHOLD_DEFAULT` is 500, and `_handlers.load_search_candidates` only draws candidates from the FTS index at or above that threshold — below it every `memory_search` runs a full `load_all` off disk. So on a store this size a stale index does **not** poison search candidate selection. What it does poison:

- **The session-start scope table.** `cli/session_start_cmd.py` builds it from `index.scope_counts`, so it publishes the index's scopes, not the files'. On the hand-edited store it still does, post-fix, by design:

  ```
  bettermemory: 1 memory is in scope for this repository.
  Top scopes: tools (1).
  ```

  The file says `infrastructure`.

- **The persisted FTS text**, which is what a store that crosses 500 memories will select candidates from. Above the threshold this stops being a display problem and becomes a retrieval problem: the query is matched against text no memory contains any more, and the memory that does contain it is not a candidate.

The order of those two matters. The damage is currently cosmetic *for this store*, and it silently becomes retrieval damage at a size boundary nobody watches.

## Root cause

One line, in `_check_index_health` (`src/bettermemory/doctor.py`), as of the fix commit's parent:

```python
indexed_count = int(status.get("indexed_count", 0) or 0)
if indexed_count == disk_count:
    return Diagnosis(
        name="index_health",
        status="ok",
        message=(
            f"Index healthy: {indexed_count} memories indexed "
            f"(matches disk; PRAGMA quick_check passed)."
        ),
        details=details,
    )
```

Equal counts returned `ok` directly. Everything downstream of that return — the parse-aware refinement, the unparseable-file arithmetic — existed only on the *unequal*-count path, so the identity comparison was unreachable at exactly the counts an out-of-band swap produces. `store._warn_on_index_divergence` carries the identical early return and was unreachable in the same way, which is why its warning list was empty above.

This is the **third** time this check has certified an index that did not describe the store. Both prior occurrences shipped in v3.13.0 on 2026-07-03: the check's own introduction, which certified over a count that ignored unparseable files, and the torn-interior-page case, where `index.status()`'s meta-only reads returned clean counts over a corrupt b-tree. Each was fixed by adding evidence — a parse walk, then `PRAGMA quick_check` — beneath the same unchanged `==` gate. The gate was never the thing that got examined.

Two docstrings shipped alongside the defect asserting the opposite of the code:

- `_build_context_block` in `src/bettermemory/cli/session_start_cmd.py` claimed "the number the model sees here is **provably** the number those surfaces would report". The only gate was `indexed_count != disk_count`, which proves nothing about which memories those rows describe.
- `store._warn_on_index_divergence` claimed "The raw count comparison is only the cheap TRIGGER, never the verdict." For an equal-count identity swap the count was the entire verdict, and the verdict was silence.

Both were true of the code below the gate and false of the gate itself. That is the recurring shape: a docstring describing the interesting half of a function while the uninteresting half decides the answer.

## Fix

Four changes, each standing alone.

**The message states only what was checked.** `Index healthy: N memories indexed (matches disk; ...)` became an inventory of the legs that actually ran. A reader can now tell a count comparison from a reconciliation by reading the sentence.

**Identity leg.** After `quick_check`, `_reconcile_index_against_disk` (new, `src/bettermemory/doctor.py`) calls `store.scan_active_memory_ids` and routes the result through `store._has_confirmed_index_gap` — deliberately NOT a raw set difference against `index.indexed_ids`. Every Store mutator lands the `.md` and commits the index row as two steps inside one `_locked()` block, so a raw diff taken against a store a fleet is writing to reports a hole that closes a millisecond later; that helper's own comment records a 150ms re-poll still firing false at 12 and 24 concurrent agents on `bench/swarm.py`. It re-resolves each candidate under the writer's own file lock, which synchronises with the writer instead of guessing how long it will take.

**Content leg.** The identity leg cannot see a hand-edit — same id, same file. `_index_content_rows` reads `SELECT id, scopes_json, body FROM memories` off the index read-only and compares it against the memories doctor already loads. `index._upsert_memory` writes `json.dumps(memory.scopes)` and `memory.body` verbatim, so an untouched store compares byte-equal on both columns: measured on the live 239-memory store, zero mismatches on either.

That comparison needed the parsed memories, and doctor was loading them twice already (`_check_memory_parse_health` and `_check_memory_body_completeness` each ran their own `Store(directory).load_all()`). Rather than add a third, `_MemoryLoad` performs one load that all three checks share. Cost is the smaller half of the reason: three independent samples of a directory another agent may be writing to can disagree with each other, and two checks reporting on two different snapshots is how a report contradicts itself. The `--fix` path still constructs its own, because re-running `index_health` after a rebuild must see the store as it is now.

Both legs record in `details` whether they RAN — `identity_reconciled`, `content_reconciled` — because "reconciled and clean" and "could not reconcile" are different claims and this check's entire history is of the second being reported as the first. A leg that could not run reads as a finding, not as a pass. The unparseable-files `ok` branch reconciles too: a certification is a certification.

**The session-start gate takes the strictly stronger free upgrade.** `store.active_memory_filenames` (new) returns the filename set from the directory listing that command already performs, and `_indexed_filenames` compares it against the index's `filename` column via `index.indexed_ids` + `index.filenames_for_ids`. No parse, no second walk, and it catches the identity swap:

```
[bettermemory] session-start: the index's 2 row(s) name a different set of files
than the 2 on disk — skipping the hint rather than publishing a scope table built
from memories that are no longer there. `bettermemory reindex` reconciles them.
```

`count_active_memory_files` now delegates to that helper so there is one definition of the filter. The "provably" claim was rewritten rather than kept: the two gates establish that the index holds one row per file, named for that file, and they establish nothing about whether each row's stored scopes still match its file's. Catching that needs a parse of every file, which is the cost this command exists to avoid, so it stays doctor's job — and the comment now says so instead of implying it was already handled.

`store._warn_on_index_divergence` kept its code and lost its false docstring. Reconciling on every `Store()` construction would put a full parse walk plus a lock dance on the server's boot path, which is the cost that gate exists to avoid. The docstring now names the one shape it misses and points at the surface that catches it, which is the honest version of the same design.

## Verification

In `tests/test_doctor.py`:

- `test_index_health_warns_when_ids_diverge_at_equal_count` — remove one memory, add another out of band.
- `test_index_health_warns_when_a_body_was_hand_edited` — same id, retagged and retexted.
- `test_index_health_certification_names_only_what_it_checked` — the `ok` message names its legs; a future edit cannot quietly re-broaden the claim past the evidence.

The first two assert the reconciliation RAN, not merely that the status came out `warn`: each spies on the leg it targets (`_has_confirmed_index_gap` called with this root, `_index_content_rows` called with this index path) and asserts the call happened before asserting anything about the verdict. Asserting the status alone would pass against a check that guessed right for the wrong reason — the mistake point 3 of [`2026-07-25-doctor-false-green-on-importable-extra.md`](2026-07-25-doctor-false-green-on-importable-extra.md) was written about. Both also assert the prescribed `bettermemory reindex` actually clears the warning.

Each was checked against the unfixed code. Restoring the pre-fix early return turns all three red, and the two that matter fail on the spy, not on the status:

```
assert gap_calls == [tmp_path], "the identity leg never ran"
AssertionError: the identity leg never ran
```

Then each leg was blinded separately. Forcing `identity_gap = False` reds the identity test alone; discarding the content rows reds the hand-edit test alone. Neither test is riding on the other's leg.

`tests/test_doctor.py tests/test_store.py tests/test_index.py` — 348 passed. On the live 239-memory store the check still certifies, with the fuller sentence and no false positive:

```
✓ index_health: Index healthy: 239 rows; row count matches disk; PRAGMA quick_check
  passed; every id and every body/scope list reconciled against disk.
```

No migration. Nothing on disk changes shape; the check reads more and claims less.

## What the surface should do differently

1. **A certification names its evidence, or it is not a certification.** "Index healthy … matches disk" survived three incidents because it read like a conclusion about the index while being a statement about one integer. Every `ok` this project emits should be legible as an inventory of what ran — which is also the only form a reader can catch being wrong.
2. **Fixing a false green by adding evidence beneath the gate leaves the gate.** Twice the repair was a new probe (a parse walk, then `quick_check`) wired in below an untouched `if a == b: return ok`. Both times the new probe was real and both times the shape that produced the incident was still reachable. When a check certifies wrongly, the thing to audit is the branch that returned, not only the evidence it had.
3. **Cheap-trigger designs need the missed shape written down at the gate.** The startup check's gate is a legitimate performance decision; what made it a defect was a docstring one paragraph below claiming the count was never the verdict. A gate that trades coverage for cost must name what it drops and which surface picks it up, in the branch that does the dropping.
4. **"Ran and found nothing" and "did not run" must be distinguishable in the output.** Both legs now report their own execution in `details`. Every check here that can skip a leg should — a report that cannot distinguish those two is a report whose greens are unfalsifiable.
5. **The blast radius of a stale index is a function of store size, and nobody is watching the boundary.** Below `_INDEX_THRESHOLD_DEFAULT` this defect was cosmetic; above it, it is retrieval. A store crossing 500 memories silently promotes a class of latent index damage into wrong search results, with no surface that says so.

## References

- Introduced: `0eab9a7` (2026-07-03), the commit that added the `index_health` check; shipped in v3.13.0.
- Prior occurrences, both v3.13.0: the check's introduction (CHANGELOG, "`bettermemory doctor` checks FTS index health, and the index-divergence warning is parse-aware") and `4f6aac3`, the torn-interior-page fix (CHANGELOG, "Page-level index corruption degrades instead of crashing").
- Related code: `_check_index_health`, `_reconcile_index_against_disk`, `_index_content_rows` and `_MemoryLoad` in `src/bettermemory/doctor.py`; `_has_confirmed_index_gap`, `scan_active_memory_ids`, `active_memory_filenames` and `_warn_on_index_divergence` in `src/bettermemory/store.py`; `_indexed_filenames` and `_build_context_block` in `src/bettermemory/cli/session_start_cmd.py`; `load_search_candidates` in `src/bettermemory/_handlers.py`.
- Related incidents: [`2026-07-25-doctor-false-green-on-importable-extra.md`](2026-07-25-doctor-false-green-on-importable-extra.md) — the same failure class, a green computed over the wrong input, and the source of the assert-that-the-check-ran discipline these tests follow.
