# Swarm convergence plan

Making "a fleet of agents converges on one memory store" a true,
benchmarked claim instead of a marketing line.

Status: partly shipped. **Phase 0** (fleet benchmark, `bench/swarm.py`),
**Phase 1** (index-backed by-id lookup) and **Phase 1b** (sharded
event-log active file, v3.24.0) have shipped; **Phases 2-5 remain
spec.** So this is a plan doc with a shipped prefix, not a pure design
doc — read each phase's own heading for its state. The
[CHANGELOG](../CHANGELOG.md) is the source of truth for what actually
exists, including the errata correcting what the 3.24.0 entry claimed.

## Scope, and the line we do not cross

A **swarm** is one user's fleet of agents: N agents in the same trust
domain, launched by (or coordinated for) that user, working a shared
task against that user's own store. Every agent is trusted equally.
This is the same thing `swarm_id` already means for episodes today (a
coordinator's cohort, N:1 fan-in).

This is NOT multi-user, and it does not add RBAC, per-agent auth, or
tenant isolation. "Many users on one store" stays where the
[roadmap](ROADMAP.md) puts it: a different product, not planned. If a
future need is "two people share a store with different permissions,"
that is out of scope here and stays out.

## What "converge" has to mean

Vague convergence is how we got the LinkedIn line that couldn't
survive contact with the code. Pin it. Given N agents operating
concurrently against one store, the store is **convergent** when all
six hold:

1. **Safety** — no operation corrupts another's; every write is
   atomic and the derived index stays consistent with disk.
2. **Liveness** — N agents make forward progress with no global
   serialization point; throughput scales with agents until the disk,
   not a lock, is the limit.
3. **Non-duplication** — when M agents independently discover the same
   fact, the store collapses toward one memory carrying M
   corroborations, not M near-duplicate memories.
4. **Disjoint-merge** — when two agents add different but compatible
   knowledge to the same memory, both survive automatically; only a
   genuine overlapping conflict surfaces to a caller.
5. **Provenance** — every memory records which agent and which swarm
   produced it, so a coordinator can attribute, audit, and roll back
   one agent's contributions without touching the rest.
6. **Cross-host convergence** — agents on different machines reach the
   same state without a human resolving a git conflict.

### Where we already are (verified against the code, 2026-07-18)

Not starting from zero. The correctness floor is genuinely built:

*Citations here and below name **symbols**, not line numbers. This doc
shipped six `file.py:NNN` references; resolved against HEAD with an
AST walk, four of the six no longer point at the code they claimed:
`index.py:32-45` and `events.py:237` land in prose rather than code
(the module docstring's "Concurrency" paragraph, and `redact_query`'s
docstring — the function is `redact_query`, module-level and public,
not `_redact_query`, which matches no symbol defined anywhere under
`src/`); `store.py:393-478` straddles two
functions, opening inside `Store.write` and closing inside the
`Store.update` it meant to cite; and `store.py:1648` lands in
`Store._path_for`, 14 lines short of the `Store._find_path_for_id` it
names. Two still land: `store.py:298` is exactly `Store.load_one`, and
`episodes.py:92-114` brackets the `swarm_id` plumbing inside
`EpisodeStore.write` (its parameter and its assignment), though not
the `list_by_swarm` the same bullet also names. A symbol survives the next edit;
a line number does not — and note that the earlier revision of this
very paragraph got its own tally wrong, claiming five misses and a
single survivor.*

- **Safety: DONE.** Per-memory-file `fcntl`/`msvcrt` locking
  (`_fsutil.flock_excl`), so disjoint memories never contend. The
  SQLite FTS5 index upsert runs *inside* the per-file lock — every
  mutator calls `_index_upsert_quietly` / `_index_remove_quietly`
  from within its own `_locked(...)` block
  ([store.py](../src/bettermemory/store.py)) — so index order
  matches disk order even under concurrent same-id writes (the audit
  H1 fix). Files are canonical, the index is a rebuildable cache.
- **Same-memory races: DONE.** `Store.update` does an optimistic-CAS
  on the `updated` snapshot under the lock
  ([store.py](../src/bettermemory/store.py)) and raises
  `ConcurrentUpdateError` instead of silently clobbering. Tombstone /
  rename races are rechecked under-lock.
- **Read-side cohort: PARTIAL.** `swarm_id` exists, but only on
  **episodes** (session run-state): `EpisodeStore.write` accepts the
  tag and `EpisodeStore.list_by_swarm`
  ([episodes.py](../src/bettermemory/episodes.py)) fans it back in,
  which is what the `episode_search(swarm_id=)` handler calls.
  Durable memory has no swarm/agent concept.

### Where the gaps are

- **Liveness: PARTIAL.** Every operation, including reads, appends one
  line to the event log — but since 3.24.0 (Phase 1b) that log is no
  longer a single global file. The active log is 16 shards,
  `.events.NN.jsonl`, and a recorder picks its shard by
  `crc32(session_id) % SHARD_COUNT` (`Recorder.__post_init__` in
  [events.py](../src/bettermemory/events.py)), so writers from
  different sessions append to different files and no longer contend on
  one flock. The Phase-0 measurement below put that lock at 7-17% of
  throughput. **What the tax is post-sharding is not currently a
  number this project can defend** — the `~1%` that used to sit here
  came from a single unreplicated A/B, and an attempt to replicate it
  produced a spread that swamps it and does not even fix its sign. See
  the event-log tax bullet under Phase 0 results for the data. The
  residual cost is per-event redaction + fsync rather than lock wait,
  which is a statement about *where* the work goes, not how much.

  What remains is a *different* global write serialisation point: the
  FTS5 index. Every memory mutation (`write`, `update`,
  `mark_verified`, `tombstone`, `restore`, `rename_scope`) writes the
  single `<root>/.index.sqlite`, and SQLite admits one writer at a time
  even in WAL mode. Phase 1b below was billed as removing "the one true
  global serialization point" — it was not the only one; the index
  outlived it, and closing that is unclaimed work.

  Test coverage is still the weak half:
  [tests/test_concurrency.py](../tests/test_concurrency.py)'s flagship
  invariant test runs 4 spawned workers x 50 ops, and its other
  multi-process tests top out at 6 workers. Those are correctness
  tests, never throughput ones.
- **Non-duplication: NO.** The write-dedup gate checks the *committed*
  store. Two agents discovering the same fact in the same second never
  see each other's in-flight write, so a swarm produces N copies of
  "the API base is X."
- **Disjoint-merge: NO.** Same-memory concurrent edits are
  last-writer-wins-or-reject. Correct, but for a swarm every conflict
  is a retry, not a merge; two agents enriching one fact can't both
  land.
- **Provenance: NO** (for durable memory — see PARTIAL above).
- **Cross-host: NO.** `sync` is git; conflicts are git merge
  conflicts with "no auto-resolution"
  ([internals.md](internals.md#limitations) limitation — moved there
  from the README by `e2d43b4`).

The honest one-line summary of today: *multiple agents can safely
share one store at small scale, and one point on that curve is now
measured rather than guessed at — but the ceiling is not, and it does
not yet converge.*

The word "ceiling" is deliberately withheld. Phase 0 measured a
**point**: one 12-core box, throughput peaking at about core count and
falling off beyond it. Phase 0's own text says why that is not a
ceiling — *"past physical cores you measure the OS scheduler; a real
ceiling needs a many-core box"* — and no many-core run has been done.
An earlier revision of this line claimed the ceiling was measured;
that overstated the project's own benchmark in the project's own
favour, which is the exact bias this doc exists to resist. The plan
below closes the rest of that sentence.

## Phase 0 results (measured 2026-07-18)

Ran `bench/swarm.py` on a 12-core machine (seed corpus 40, 150 ops per
agent, read-heavy mix). The measurement corrected the plan — see the
reprioritization note under Phases.

Fleet scaling, one shared store:

| agents | ops/s | p50 ms | p99 ms | corruption |
|-------:|------:|-------:|-------:|:----------:|
|      1 |   108 |    6.7 |     30 |     OK     |
|      2 |   199 |    7.0 |     31 |     OK     |
|      4 |   277 |    7.4 |     66 |     OK     |
|      8 |   299 |    8.8 |    135 |     OK     |
|     12 |   318 |   11.3 |    204 |     OK     |
|     24 |   198 |   10.0 |    739 |     OK     |

- **Safety holds, and the event-log half has now been re-validated
  against the sharded layout.** These rows were taken at `90d10b9`
  (2026-07-18 21:12), when the active log was still the single
  `.events.jsonl` and the benchmark's gate opened exactly that file —
  so as measured, "every .md parsed, every event-log line valid JSON,
  no agent crashed" was founded.

  It stopped being founded about seventy minutes later. `59a1e08`
  (3.24.0, authored 2026-07-18 22:22 — 1 h 10 m after `90d10b9`)
  sharded the active log into `.events.NN.jsonl` and the gate kept
  opening the hard-coded `.events.jsonl`, which no longer exists on
  any store the benchmark creates: `exists()` was False, the parse
  loop never ran, and the gate reported clean having read zero bytes.
  Every corruption claim made between `59a1e08` and `0f2789c`
  (2026-07-19 06:58, which enumerates segments through the product's
  own `events._active_segment_paths` and now fails loudly when a run
  that recorded events verified nothing) is therefore unfounded — the
  3.24.0 release commit's "zero-corruption unchanged" included. The
  rows above were never re-run against the sharded layout.

  **They have been now.** `bench/swarm.py` default sweep at HEAD
  (2026-07-19, same 12-core box, repaired gate): zero corruption on
  every run through 24 agents, with the gate carrying its evidence —
  **7,650 event-log lines across 43 active segments (≈792 KB), every
  line valid JSON**, every `.md` parsed, no agent crashed, 7,650 of
  7,650 ops completed. Property 1 is benchmarked rather than asserted,
  on the current on-disk layout.
- **Throughput** climbs to a peak at core count (~318 ops/s at 12
  agents) then degrades under oversubscription (198 at 24). Past
  physical cores you measure the OS scheduler; a real ceiling needs a
  many-core box.
- **The event-log lock is real but modest.** Logging on vs off cost
  7–17% of throughput across runs — worth removing, not the headline.
  (That figure is pre-sharding, 2026-07-18.)
- **The post-sharding event-log tax is below what this benchmark can
  resolve.** `bench/swarm.py` reports `tax = 100 * (1 - on/off)`, so a
  negative tax means the logging-on arm measured *faster*. Twelve A/B
  pairs at HEAD on one 12-core box (2026-07-19):

  | agents | pairs | tax range | mean |
  |-------:|------:|:----------|-----:|
  |     24 |     7 | -41.5% … +15.0% | -5.1% |
  |      8 |     5 | +6.6% … +56.5%  | +27.8% |

  At 24 agents the sign is not determined; at 8 agents the magnitude
  spans nearly an order of magnitude. Either way the run-to-run spread
  is far larger than the ~1% this doc used to claim, so **that figure
  is retracted rather than replaced** — no honest single number is
  available from this instrument as it stands.

  Two caveats, both cutting against over-reading the table. First, the
  box was **not quiesced** (load average ~18 on 12 cores, sibling
  processes competing), which inflates the spread; these runs are
  evidence that the A/B is not robust to background load, *not* proof
  that a quiet box would also fail to resolve the effect, and they do
  not establish that the original ~1% was wrong at the time it was
  taken. Second, the throughput drift is itself the tell: the identical
  8-agent logging-on configuration ranged 197→532 ops/s across five
  sequential runs (2.7x). An instrument in which one arm moves 2.7x
  between repetitions of the identical configuration cannot certify a
  single-digit difference between arms. Resolving
  this needs a quiesced box and enough repetitions to put an interval
  around the estimate — neither has been done, and until it is, "we
  cannot measure this reliably" is the finding.

The headline was somewhere else. A single-process corpus-scaling probe
(update = load-by-id + write, timed across store sizes):

| corpus | update p50 | load-by-id p50 |
|-------:|-----------:|---------------:|
|     50 |     8.6 ms |         5.8 ms |
|    200 |    31.7 ms |        30.0 ms |
|    800 |    75.9 ms |       126.8 ms |
|   3200 |   320.6 ms |       521.0 ms |

**By-id operations were O(corpus)** — past tense, and true only of the
code as it stood on 2026-07-18; Phase 1 below fixed it, so do not read
this paragraph as a description of HEAD. `Store._find_path_for_id` and
`Store.load_one` walked and parsed the entire active directory to
resolve one id. At 3200 memories a single update
costs ~320 ms and a by-id read ~520 ms, climbing ~linearly. On the
exact scenario this plan is for — a fleet accumulating a large shared
store — this dominates everything else, the event-log lock included.
The index already carries the id→filename map (`index.filenames_for_ids`);
the mutation path just doesn't use it.

Honest one-liner from Phase 0: *one store sustained ~300 ops/s across
up to ~a-dozen agents on a 12-core box with zero corruption; the first
thing that will actually stop a growing fleet is O(corpus) by-id
lookup, not the event-log lock.* (Past tense throughout: this is the
pre-Phase-1 store. HEAD is faster, but by how much is not a settled
number — see the peak-throughput caveat under Phase 1.)

## Phases

**Reprioritized by the Phase 0 data.** The original order led with the
event-log shard (old Phase 1); the measurement demotes it under a new
**Phase 1: index-back the by-id path** — resolve `_find_path_for_id` /
`load_one` through `index.filenames_for_ids` (O(1) lookup) with the
`load_all` walk as the fallback for index-miss/corrupt. Bigger
throughput win at scale, lower risk (the map and lookup already
exist), and it's the difference between a fleet's shared store staying
usable past a few thousand memories or not. Event-log sharding stays in
the plan, demoted to after it.

**Phase 1 — SHIPPED 2026-07-18.** `_indexed_path_for_id` resolves an
id to its path via the index and returns it only when the named file
still carries the id (`_id_still_at_path`); a stale / absent / lying
index yields `None` and the caller falls back to the authoritative
walk, so the index can only make the lookup faster, never wrong.
`load_one` and `_find_path_for_id` (which backs `update` / `verify` /
`tombstone` / `show`) both use it. Eight tests in
`tests/test_indexed_lookup.py` pin the safety property (absent, stale,
wrong-file, unindexed, tombstoned all stay correct). Measured effect:

| corpus | update p50 before | after | by-id read before | after |
|-------:|------------------:|------:|------------------:|------:|
|     50 |            8.6 ms | 2.4 ms |          5.8 ms | 0.9 ms |
|    800 |           75.9 ms | 2.5 ms |        126.8 ms | 0.9 ms |
|   3200 |          320.6 ms | 2.7 ms |        521.0 ms | 0.9 ms |

Flat across corpus size — O(corpus) became O(1). At the fleet level
(same `bench/swarm.py`, 12-core box), peak throughput went **318 →
970 ops/s** and p99 latency at 24 agents **739 → 96 ms**, still zero
corruption. The event-log tax was recorded here as ~1% now that ops
are faster, and read as confirmation that it is the right *next*
(smaller) target rather than the first one. **The ordering conclusion
survives; the number does not** — it was one A/B pair, and the
replication attempt under Phase 0 above could not recover it or even
pin its sign. What actually orders the phases is the corpus-scaling
table immediately above, which does not depend on any throughput
figure: by-id update p50 at 3200 memories went from 320.6 ms to
2.7 ms, and stopped growing with corpus size. An O(corpus) term that reaches
hundreds of milliseconds outranks an event-log tax whose *pre-sharding*
measurement was 7-17% of throughput, whatever the post-sharding
residual turns out to be.

Two caveats on those fleet numbers, both found by re-running the
benchmark rather than by re-reading it. First, they were taken at
`096218e`, before the event log was sharded, so the corruption clause
was founded when written — but see the Phase 0 safety bullet for the
window in which it stopped being. Second, **no peak-throughput figure
in this doc has been replicated.** 970 was one sample; a later HEAD
re-run recorded 811 ops/s with p99 117 ms at 24 agents, also one
sample; two further default sweeps at HEAD peaked at **410 and 464
ops/s** (2026-07-19, 12-core box under concurrent load — see the
quiescence caveat in the Phase 0 event-log tax bullet). Those last two
are depressed by background load and are *not* offered as refutations
of 811 or 970. Taken together the four numbers say only that this
benchmark's absolute throughput is dominated by machine conditions
that none of the runs controlled for. **Do not quote any of them as
"the" number.** The reproducible part of the sweep is the structural
evidence, not the rate: both HEAD sweeps emitted exactly 7,650
event-log lines across exactly 43 active segments with zero
corruption, byte-identical totals, because the workload is seeded.

Ordered by leverage-over-risk. Each phase is independently shippable
and moves the honest claim forward (see the claim ladder at the end).

### Phase 0 — Benchmark the floor (do this first, always)

Replace the invented "200+" with a measured number before building
anything.

- New `bettermemory bench` (or a `tests/bench_swarm.py` harness)
  extending the existing spawn-N-processes model: `--agents N --ops M
  --mix write,search,update,verify`. Real spawned interpreters, shared
  store, no mocks — same discipline as `test_concurrency.py`.
- Emits: throughput (ops/sec), p50/p99 op latency, CAS-reject rate,
  mean lock-wait split by lock (event log vs memory file vs index),
  and the full correctness-invariant check at the end.
- Becomes the **regression gate** every later phase runs against.

Deliverable: a sentence you can post. "Benchmarked at N agents, X
ops/sec sustained, zero corruption across the invariant suite."
Whatever N and X are, they're real. This single step converts the
category of claim from marketing to fact.

### Phase 1b — Shard the event log — SHIPPED 2026-07-19 (v3.24.0)

The event log's global write lock. Removed. (It was billed at the time
as "the one true global serialization point" — it was not. Every memory
mutation still serialises on the single `.index.sqlite`; see the
Liveness bullet above and the 3.24.0 erratum in the CHANGELOG.)

Shipped as fixed-K striping rather than the per-session files sketched
below — one file per session proliferates unboundedly and blows a
reader's open-fd budget, whereas 16 fixed shards bound both. The
active log splits into `.events.NN.jsonl` (NN = `crc32(session_id) %
16`), so writers from different sessions append to different files and
no longer contend on one flock. `iter_events` merges the shards plus
any pre-sharding legacy `.events.jsonl` by event `ts` (a
`heapq.merge`, open fds bounded by the shard count). `sync` excludes
the shard files (they carry query text). The event-log tax was
recorded at the time as dropping from ~7-17% to ~1%; **the ~1% has
since been retracted as unreplicable** — see the event-log tax bullet
under Phase 0 results. Note also that the lock did not go away, it was
striped: `Recorder.record` still appends under `_locked(self.path)`,
now per shard. Two sessions whose `crc32` lands on the same shard —
guaranteed once more than 16 are live — still serialise against each
other. So "the residual is redaction + fsync, not lock wait" is a
claim about the uncontended case, and it too is unquantified.

**Four** new tests in `tests/test_events.py` pin striping, per-session
shard stability, cross-shard merge order, and legacy backward-compat:
`test_same_session_maps_to_a_stable_shard`,
`test_sessions_stripe_across_multiple_shard_files`,
`test_iter_events_merges_shards_preserving_per_session_order` and
`test_legacy_events_jsonl_merges_in_after_sharding`. This doc said
"9"; the release commit `59a1e08` adds exactly those four test
functions and its other test edits are helper refactors. The "9"
appears to have been carried over from the *other* commit in the
`v3.24.0` tag, `096218e`, whose message likewise claims nine for
`tests/test_indexed_lookup.py` — a file that contains eight test
functions and no parametrisation. The CHANGELOG's 3.24.0 erratum
corrected the same overcount in the release entry; this instance was
missed at the time.

**Two claims this section shipped with have since been falsified in
code, not just in prose.** They read "rotation, archives, and crash
recovery are unchanged and shared" and "because every other reader
composes on top of `iter_events`, nothing downstream changed":

- *Rotation was not safely shareable across shards.* The archive /
  `.rotating` stem was `.events-{ts}` at one-second resolution with no
  shard component, and only the `.gz` archive was probed before a name
  was taken — check-then-act, safe only while the removed global
  append lock made rotations mutually exclusive. Two shards crossing
  `max_bytes` in the same UTC second derived the identical holding
  path and the second rename destroyed the first shard's whole renamed
  segment. Uniform crc32 striping fills shards *in phase*, so that was
  the correlated case under exactly the swarm workload sharding was
  built for. Crash recovery had the mirror bug: it could not tell a
  crash orphan from another shard's live in-flight rotation.
- *Not every reader composed on `iter_events`.* `iter_all_events`
  yielded all archives and then all active segments, which stopped
  being chronological the moment shards rotated independently — a
  quiet shard's active segment can hold events older than a busy
  shard's fresh archive. `iter_events_window`'s rotation shield
  derived one global `oldest_ts` across all 16 shards and was
  effectively dead on any store older than a session.

Both were fixed after v3.25.2 by `eace517`, which partitions the
rotation namespace by shard (`.events-{ts}-s{NN}`), probes the
`.rotating` path as well as the archive, scopes orphan recovery to the
owning shard under a store-wide `.events-rotate.lock`, decides window
coverage per segment, and makes `iter_all_events` a real `heapq.merge`
so the chronological guarantee is earned rather than asserted. At time
of writing that fix is on `main` and **unreleased**.

The original per-session sketch, kept for the record:

- Replace the single `.events.jsonl` with per-writer segments:
  `.events/<session_id>.jsonl`. Each agent owns its segment and
  appends with no cross-agent lock (a writer contends only with
  itself).
- Readers (eval, health, audit, `list_events`) already consume the log
  analytically. They merge segments at read time; events carry ISO
  timestamps, so global order is a merge-sort, not a write-time
  invariant. Rotation/gzip becomes per-segment.
- Backward-compat: a legacy single `.events.jsonl` is just "segment
  zero" at read time; no migration required, `reindex`-free.

Risk assessment as originally written, kept for the record: *"Low.
Append-only, per-owner files, read-time merge is a natural fit. This
is the highest-throughput-per-line-of-code change in the plan and
probably moves the ceiling by an order of magnitude on its own. Gate
on Phase 0."*

Phase 0 falsified the last sentence before it was built, which is what
gating on a benchmark is for: the event-log lock measured 7-17% of
throughput, and removing a cost of at most 17% cannot yield a 10x
speedup no matter how completely it is removed — that is arithmetic,
and it does not depend on the shipped tax, which is just as well since
the ~1% once quoted here has been retracted as unreplicable (see the
Phase 0 event-log tax bullet). The order of magnitude came from Phase 1
(by-id lookup) instead: by-id update p50 at 3200 memories went
320.6 ms → 2.7 ms, which is the one place in this doc a 10x-plus claim
is carried by a latency measurement rather than a single throughput
sample. The "low risk" half did not hold
either — see the two falsified claims above; sharding the writes
without also partitioning the rotation namespace cost a data-loss
defect that took until `eace517` to close.

### Phase 2 — Swarm provenance on durable memory

Generalize the episodes `swarm_id` to durable memories.

- Additive frontmatter: optional `swarm_id` and `agent_id`, emitted
  only when set — exact discipline the episode writer already uses, so
  non-swarm memories serialize byte-identically to today.
- Threads through `write` / `update` and the MCP write handler; new
  `memory_search(swarm_id=)` filter; `memory_health` gains a per-swarm
  slice.
- Unlocks the coordinator story: "what did my fleet learn this run,"
  attribution per agent, and targeted rollback ("agent 7 drifted —
  tombstone its cohort's contributions, leave everyone else's").

Risk: low, purely additive. This is the backbone Phases 3-4 stand on.

### Phase 3 — Convergent dedup (collapse concurrent rediscovery)

Two halves, one goal: M agents find the same fact, store keeps one.

- **(a) In-flight claim reservation.** A small `inflight_claims` table
  in the existing index SQLite db (WAL, already multi-process). Before
  a swarm agent commits a *new* memory, it reserves a content
  fingerprint (reuse the dedup tokenizer's raw token-set / a simhash).
  A peer mid-write on a matching fingerprint makes the later writer
  attach a corroboration to the winner instead of creating a
  duplicate. Closes the committed-store-only blind spot.
- **(b) Swarm-aware consolidation.** Extend the existing
  `consolidate` pass with a cohort mode that folds same-run near-dups
  into one memory, unioning provenance (all M agents) and boosting
  confidence by corroboration count. New mode on an existing engine,
  not a new engine.

Risk: medium. (a) touches the hot write path — keep it best-effort and
behind the same "index failure never fails the canonical write"
contract the index upsert already honors. (b) is offline and safe.

### Phase 4 — Disjoint-merge instead of CAS-reject

Turn most same-memory swarm conflicts into automatic convergence.

- Only engages *when the CAS would otherwise reject*. The caller
  already carries the pre-edit snapshot (`updated`); load the common
  ancestor and attempt a structured 3-way merge on the body. Memories
  are short; sentence/line-level 3-way resolves disjoint additions
  cleanly.
- Irreconcilable overlap still raises `ConcurrentUpdateError` —
  Property 1 (safety) is never traded for convergence. Last-writer
  reject stays the fallback; merge is the optimization on top.
- Append-only frontmatter (verified_paths, links, corroborations)
  unions rather than conflicts.

Risk: medium-high (merge correctness). Mitigated by gating strictly
behind the existing CAS, requiring the ancestor snapshot, and heavy
property tests through the Phase 0 harness. Ship dark behind a config
flag first.

### Phase 5 — Cross-host convergence (optional, hardest)

Auto-resolve `sync` instead of producing git conflicts.

- Memories are per-file with monotonic `updated`. That makes a
  per-file CRDT with the frontmatter as state tractable: last-writer
  by `updated` for scalars, union for append-only lists, tombstone
  wins over active (delete propagates). `sync pull` applies the merge
  function instead of leaving a conflict.
- Highest risk and effort; genuinely optional. Everything above is
  single-host / shared-filesystem and stands without it.

## Test gate

Every phase runs the Phase 0 harness (throughput did not regress,
zero corruption) plus targeted unit/property tests. Real spawned
processes, never mocks — mocks are a regression guard layered on top
of a live run, never a substitute (house rule). No phase merges until
its benchmark number is recorded in this doc.

## The claim ladder — what each phase lets you honestly say

- **Today:** "multiple agents can safely share one store." (True,
  small-scale, unmeasured.)
- **+ Phase 0:** "benchmarked at N agents, X ops/sec, zero
  corruption." (A number, not a guess.)
- **+ Phases 1 and 1b (both shipped):** "scales to a fleet — no global
  *event-log append* lock; by-id work is O(1); throughput climbs with
  agents up to about core count." (The 200 becomes real, whatever the
  real N is.) Three qualifications, all load-bearing. The unqualified
  "no global lock" this rung used to license is **not** sayable and
  never was: every memory mutation still serialises on the single
  `.index.sqlite`. "Append" is not decoration either — appends take a
  per-shard flock, so sessions colliding on one of the 16 shards still
  serialise, and rotation briefly takes a store-wide
  `.events-rotate.lock` to sweep crash orphans (deliberately kept off
  the common append path). And "throughput climbs with agents" is a
  shape, not a rate — this doc no longer stands behind any particular
  ops/s figure. Even "up to about core count" is softer than it looks:
  two consecutive HEAD sweeps on the same 12-core box peaked at 12 and
  at 24 agents respectively, so the fall-off past core count that the
  Phase 0 table shows reproduced in only one of the two. The monotone
  climb up to core count did reproduce in both; where it turns over did
  not. Note also that the sweep always runs agent counts in ascending
  order on a machine whose condition drifts, which confounds "more
  agents" with "later in the run". Say which lock you removed, or say
  nothing.
- **+ Phases 2-3:** "a fleet converges: independent agents collapse
  onto shared memories with provenance, instead of multiplying them."
- **+ Phase 4:** "agents enrich the same memory concurrently and both
  contributions survive."
- **+ Phase 5:** "the fleet converges across machines, not just one
  host."

## Recommended cut line

Phases **0-2** are the high-leverage, low-risk core. They make this
claim fully true and defensible:

> Built for agent fleets: multiple agents share one store with no
> global event-log append lock, benchmarked at N agents with zero
> corruption, every memory attributed to the agent that wrote it.

(The qualifier is load-bearing. This blurb previously read "with no
global lock" — false while the FTS5 index still admits one writer at a
time store-wide. Closing that is unclaimed work, not a shipped
property; do not post the unqualified version until it is. Note also
what this blurb does *not* say: it claims zero corruption at N agents,
which the benchmark does evidence reproducibly, and it claims no ops/s
figure, which the benchmark currently cannot.)

Phase 3 makes "converge" a strong word. Phase 4 makes it a strong
demo. Phase 5 is the cross-machine stretch. Recommendation: land 0-2
as the first milestone, re-post from a place of measured truth, then
decide 3-5 against real fleet usage rather than a launch deadline.
