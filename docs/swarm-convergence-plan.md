# Swarm convergence plan

Making "a fleet of agents converges on one memory store" a true,
benchmarked claim instead of a marketing line.

Status: spec / not started. This is a design doc, not a shipped
feature. The [CHANGELOG](../CHANGELOG.md) is the source of truth for
what actually exists.

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

- **Safety: DONE.** Per-memory-file `fcntl`/`msvcrt` locking
  (`_fsutil.flock_excl`), so disjoint memories never contend. The
  SQLite FTS5 index upsert runs *inside* the per-file lock
  ([index.py:32-45](../src/bettermemory/index.py)), so index order
  matches disk order even under concurrent same-id writes (the audit
  H1 fix). Files are canonical, the index is a rebuildable cache.
- **Same-memory races: DONE.** `Store.update` does an optimistic-CAS
  on the `updated` snapshot under the lock
  ([store.py:393-478](../src/bettermemory/store.py)) and raises
  `ConcurrentUpdateError` instead of silently clobbering. Tombstone /
  rename races are rechecked under-lock.
- **Read-side cohort: PARTIAL.** `swarm_id` exists, but only on
  **episodes** (session run-state), for `episode_search(swarm_id=)` /
  `list_by_swarm` fan-in ([episodes.py:92-114](../src/bettermemory/episodes.py)).
  Durable memory has no swarm/agent concept.

### Where the gaps are

- **Liveness: NO.** Every operation, including reads, appends one line
  to a single `<root>/.events.jsonl` under one global lock
  ([events.py:235-245](../src/bettermemory/events.py)). At 4 agents
  this is free. At 200 agents doing frequent ops it is *the* ceiling:
  the whole fleet funnels through one flock. Current concurrency
  coverage is 4 workers x 50 ops
  ([tests/test_concurrency.py](../tests/test_concurrency.py)) — a
  correctness test, never a throughput one. We have no measured agent
  ceiling at all.
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
  conflicts with "no auto-resolution" (README limitation).

The honest one-line summary of today: *multiple agents can safely
share one store at small scale; it does not yet converge and its
ceiling is unmeasured.* The plan below closes exactly that sentence.

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

- **Safety holds.** Zero corruption on every run through 24 agents:
  every .md parsed, every event-log line valid JSON, no agent crashed.
  Property 1 is real and now benchmarked, not asserted.
- **Throughput** climbs to a peak at core count (~318 ops/s at 12
  agents) then degrades under oversubscription (198 at 24). Past
  physical cores you measure the OS scheduler; a real ceiling needs a
  many-core box.
- **The event-log lock is real but modest.** Logging on vs off cost
  7–17% of throughput across runs — worth removing, not the headline.

The headline was somewhere else. A single-process corpus-scaling probe
(update = load-by-id + write, timed across store sizes):

| corpus | update p50 | load-by-id p50 |
|-------:|-----------:|---------------:|
|     50 |     8.6 ms |         5.8 ms |
|    200 |    31.7 ms |        30.0 ms |
|    800 |    75.9 ms |       126.8 ms |
|   3200 |   320.6 ms |       521.0 ms |

**By-id operations are O(corpus).** `Store._find_path_for_id` and
`load_one` walk and parse the entire active directory to resolve one
id (store.py:298, store.py:1648). At 3200 memories a single update
costs ~320 ms and a by-id read ~520 ms, climbing ~linearly. On the
exact scenario this plan is for — a fleet accumulating a large shared
store — this dominates everything else, the event-log lock included.
The index already carries the id→filename map (`index.filenames_for_ids`);
the mutation path just doesn't use it.

Honest one-liner from Phase 0: *one store sustains ~300 ops/s across
up to ~a-dozen agents on a 12-core box with zero corruption; the first
thing that will actually stop a growing fleet is O(corpus) by-id
lookup, not the event-log lock.*

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
`tombstone` / `show`) both use it. Nine tests in
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
corruption. The event-log tax dropped to ~1% now that ops are faster,
confirming it is the right *next* (smaller) target, not the first one.

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

### Phase 1 — Shard the event log (kill the global lock)

The one true global serialization point. Remove it.

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

Risk: low. Append-only, per-owner files, read-time merge is a natural
fit. This is the highest-throughput-per-line-of-code change in the
plan and probably moves the ceiling by an order of magnitude on its
own. Gate on Phase 0.

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
- **+ Phase 1:** "scales to a fleet — no global lock; throughput
  climbs with agents." (The 200 becomes real, whatever the real N is.)
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
> global lock, benchmarked at N agents with zero corruption, every
> memory attributed to the agent that wrote it.

Phase 3 makes "converge" a strong word. Phase 4 makes it a strong
demo. Phase 5 is the cross-machine stretch. Recommendation: land 0-2
as the first milestone, re-post from a place of measured truth, then
decide 3-5 against real fleet usage rather than a launch deadline.
