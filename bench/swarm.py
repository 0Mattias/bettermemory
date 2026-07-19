"""Fleet-concurrency benchmark for bettermemory (swarm-convergence Phase 0).

Answers the question the LinkedIn draft couldn't: how many agents can
actually share one store, and where does throughput stop scaling?

It spawns N real agent processes (fresh interpreters under
`multiprocessing spawn`, the same posture as tests/test_concurrency.py
— `fork` would share the parent's fds and short-circuit the
cross-process locks) against one shared store. Each agent runs a
realistic, read-heavy op-mix (search / write / update / verify /
remove) on its OWN memories plus a shared read corpus.

Where the contention actually is at HEAD — three tiers, because
naming the wrong one is how a contention benchmark misleads:

*Per-memory, disjoint.* Every mutator holds an exclusive `flock` on
the single `.md` it touches (`store._locked(path)`). Agents only
ever mutate memories they wrote themselves, and bodies carry the
agent id so even fresh-write filename slugs rarely collide — these
locks stay disjoint, and the run measures the store rather than
same-memory fighting.

*Per-shard, 16-way.* The active event log has been striped since
3.24.0. A `Recorder` appends to `.events.NN.jsonl` where
`NN = crc32(session_id) % SHARD_COUNT`, and locks only that shard.
Each agent is its own session (`agent-<n>`), so appends collide only
when two agents hash to the same shard — chance below `SHARD_COUNT`
(16) agents, guaranteed above it.

*Store-global — the remaining bottleneck.* `.index.sqlite`. Every
write / update / verify / remove calls `index.upsert` / `index.remove`
from INSIDE the per-file flock, and SQLite's WAL mode admits many
concurrent readers but exactly ONE writer per database. So every
mutation the whole fleet performs funnels through that single
writer, and each of those transactions additionally re-derives
`meta.indexed_count` with a `SELECT COUNT(*)` over `memories`.
Searches are WAL readers: they neither block nor are blocked.

So the sweep measures a ~50% read half that scales with cores against
a ~50% mutation half that serialises on one SQLite writer. When the
curve flattens, that is the first thing to suspect — not the event
log, which stopped being a store-global lock in 3.24.0.

Three things come out:

1. A scaling curve — sustained ops/sec and p50/p99 latency as the
   agent count climbs. This is the real, measured number that
   replaces the invented "200+".
2. The event-log tax — the same workload run with event-logging on
   vs off at the top agent count. Before 3.24.0 this measured a
   store-global append lock, and the 7-17% it came back with is what
   motivated sharding. Post-sharding the gap is the residual
   *per-event* cost — redaction, append, fsync, plus whatever
   same-shard collisions the agent count forces — not cross-session
   serialisation.
3. A corruption check — after every run: no agent process crashed,
   every active .md parses and the parsed count matches the files on
   disk, every tombstone carries `removed` frontmatter, and every
   active event segment is valid JSONL end to end. The gate prints
   the evidence it collected (segments opened, event lines parsed,
   memories parsed) next to its verdict, because a gate that passes
   without having read anything is worse than no gate — see
   `_check_event_log`, which is exactly how this one failed before.
   This is the "zero corruption" half of any honest claim.

Usage:

    venv/bin/python bench/swarm.py                      # default sweep
    venv/bin/python bench/swarm.py --agents 1,4,16,64,128
    venv/bin/python bench/swarm.py --ops 300 --json
    venv/bin/python bench/swarm.py --keep                # keep the tmp store

Numbers are specific to the hardware you run on; the value is the
*shape* — does throughput keep climbing with agents or flatten, and
how big is the event-log gap. Disposable: stores are created under a
tmp dir and removed on exit unless you pass --keep.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Add `src/` to sys.path so this script is runnable without an install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


from bettermemory import index as _index  # noqa: E402
from bettermemory.events import (  # noqa: E402
    SHARD_COUNT,
    Recorder,
    _active_segment_paths,
)
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import (  # noqa: E402
    ConcurrentUpdateError,
    MemoryNotFoundError,
    NotTombstonedError,
    Store,
    TombstonedError,
    _parse_memory_file,
)

# A small shared vocabulary so seeded bodies and agent writes carry
# overlapping terms — searches then do real ranking work instead of
# ranking empty strings.
_VOCAB = (
    "deploy staging fly render config port middleware auth token cache "
    "index tombstone verify commit rollback migration schema worker lock "
    "latency throughput cohort swarm episode scope confidence retrieval "
    "postgres redis nginx docker kernel socket timeout backoff retry"
).split()

# Realistic agent op-mix (weights). Read-heavy: agents search far more
# than they write. update/verify/remove act on the agent's OWN
# memories, so per-file locks stay disjoint and the shared contention
# is the store-global one — the single SQLite writer on `.index.sqlite`
# that every mutation passes through — not same-memory fighting.
# Roughly half these ops mutate, so half of them hit that writer.
_OPS = ("search", "write", "update", "verify", "remove")
_WEIGHTS = (50, 18, 18, 12, 2)


def _body(rng: random.Random, agent_id: int) -> str:
    words = rng.sample(_VOCAB, k=8)
    return (
        f"agent {agent_id} durable claim: {' '.join(words)} "
        f"(item {rng.randint(0, 10**9)})"
    )


def _query(rng: random.Random) -> str:
    return " ".join(rng.sample(_VOCAB, k=rng.randint(2, 3)))


def _agent(args: tuple[str, int, int, int, bool]) -> dict[str, Any]:
    """One agent process: run `num_ops` mixed operations against the
    shared store, timing each and recording an event per op when
    `record_events` is set — mirroring the MCP handler telemetry that
    fires on every real tool call. Each agent passes its own
    `session_id`, so its `Recorder` lands on the shard
    `crc32(session_id) % SHARD_COUNT` and takes only that shard's
    append lock; the store-global serialisation the mutating ops below
    hit is the single SQLite writer on `.index.sqlite`, not the log.

    Returns per-op latencies and an outcome tally. Contention outcomes
    (a peer tombstoned our target, or the CAS rejected a stale
    snapshot) are counted, never raised — the benchmark asserts on the
    aggregate, like the store's own concurrency test.
    """
    root, agent_id, num_ops, seed, record_events = args
    store = Store(Path(root))
    recorder = (
        Recorder(Path(root), session_id=f"agent-{agent_id}") if record_events else None
    )
    rng = random.Random(seed * 100003 + agent_id + 17)

    owned: list[str] = []
    lat: list[float] = []
    tally = {
        "search": 0,
        "write": 0,
        "update": 0,
        "verify": 0,
        "remove": 0,
        "cas_reject": 0,
        "conc_err": 0,
    }

    for _ in range(num_ops):
        op = rng.choices(_OPS, weights=_WEIGHTS, k=1)[0]
        if op in ("update", "verify", "remove") and not owned:
            op = "write"

        t0 = time.perf_counter()
        try:
            if op == "write":
                memory = store.write(
                    content=_body(rng, agent_id), scopes=[f"agents:{agent_id}"]
                )
                owned.append(memory.id)
                if recorder is not None:
                    recorder.record(
                        "write", status="committed", id=memory.id, scopes=memory.scopes
                    )
                tally["write"] += 1

            elif op == "search":
                # The realistic read path the handler uses: FTS5 index
                # for a bounded candidate set, resolve ids -> filenames
                # via the index, and parse just those files by path —
                # O(candidates). NOT `store.load_one` per id, which
                # walks the whole directory (store.py:_find_path_for_id)
                # and turns one search into 20 full-corpus scans.
                q = _query(rng)
                pairs = _index.query(store.root, q, max_results=20)
                fnames = _index.filenames_for_ids(store.root, [mid for mid, _ in pairs])
                cands = []
                for fname in fnames.values():
                    try:
                        cands.append(_parse_memory_file(store.root / fname))
                    except (FileNotFoundError, ValueError, OSError):
                        pass  # file raced away / torn — skip, like the handler
                hits = run_search(cands, q, max_results=5) if cands else []
                if recorder is not None:
                    recorder.record("search", n_hits=len(hits))
                tally["search"] += 1

            elif op == "update":
                target = rng.choice(owned)
                memory = store.load_one(target)
                store.update(
                    memory.model_copy(update={"body": memory.body + " refinement\n"})
                )
                if recorder is not None:
                    recorder.record("update", id=target)
                tally["update"] += 1

            elif op == "verify":
                target = rng.choice(owned)
                store.mark_verified(target, verified_paths=[f"/srv/agent{agent_id}"])
                if recorder is not None:
                    recorder.record("verify", id=target)
                tally["verify"] += 1

            elif op == "remove":
                target = rng.choice(owned)
                store.tombstone(target, reason="bench")
                owned.remove(target)
                if recorder is not None:
                    recorder.record("remove", id=target)
                tally["remove"] += 1

        except ConcurrentUpdateError:
            tally["cas_reject"] += 1
        except (
            MemoryNotFoundError,
            TombstonedError,
            NotTombstonedError,
            FileNotFoundError,
        ):
            tally["conc_err"] += 1
        lat.append(time.perf_counter() - t0)

    return {"tally": tally, "lat": lat}


def _pct(sorted_vals: list[float], p: float) -> float:
    """p-th percentile (0-100) of an already-sorted list, linear interp."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (p / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _seed_corpus(root: Path, n: int, seed: int) -> None:
    """Single-process pre-seed so agent searches have real content to
    rank from op one."""
    store = Store(root)
    rng = random.Random(seed)
    for _ in range(n):
        store.write(content=_body(rng, -1), scopes=["seed"])


def _check_invariants(
    root: Path, results: list[dict[str, Any]], *, expect_events: bool
) -> dict[str, Any]:
    """Post-run corruption check. Four things, in order:

    1. No agent process crashed (a crash surfaces as a non-dict result).
    2. Every active `.md` parses, AND the parsed count equals the
       number of `.md` files on disk (`Store.load_all` skips malformed
       files, so a gap means a torn write slipped through).
    3. Every tombstone carries `removed` frontmatter.
    4. Every active event segment is valid JSONL end to end.

    Returns a report dict with a boolean `ok`, the `problems` list, and
    the COUNTS the gate actually inspected (`md_parsed`, `segments`,
    `event_lines`, `event_bytes`) — the caller prints those next to the
    verdict so a pass is auditable rather than merely asserted.

    `expect_events` is the run's own `events` flag. When it is set the
    check is required to have READ something — see `_check_event_log`
    for why a corruption gate that reads nothing is worse than no gate.
    """
    problems: list[str] = []

    # No agent crashed (a crash would surface as a non-dict / missing key).
    if not all(isinstance(r, dict) and "tally" in r for r in results):
        problems.append("an agent process crashed or returned a bad result")

    # Every active .md loads, and the on-disk count matches what parsed
    # (Store.load_all is defensive and skips malformed files, so a
    # mismatch means a torn write slipped through).
    store = Store(root)
    md_files = [p for p in root.glob("*.md") if not p.name.startswith(".")]
    parsed = store.load_all()
    if len(parsed) != len(md_files):
        problems.append(
            f"active .md parse gap: {len(parsed)} parsed vs {len(md_files)} on disk"
        )

    # Tombstones carry removal frontmatter.
    tdir = root / ".tombstones"
    if tdir.exists():
        from bettermemory._frontmatter import loads as fm_loads

        for tpath in tdir.glob("*.md"):
            post = fm_loads(tpath.read_text(encoding="utf-8"))
            if "removed" not in post.metadata:
                problems.append(f"tombstone {tpath.name} missing `removed`")
                break

    # Event log is fully-parseable JSONL (no torn append lines).
    log = _check_event_log(root, expect_events=expect_events)
    problems.extend(log["problems"])

    return {
        "ok": not problems,
        "problems": problems,
        "md_parsed": len(parsed),
        "md_on_disk": len(md_files),
        "segments": log["segments"],
        "event_bytes": log["event_bytes"],
        "event_lines": log["event_lines"],
    }


def _check_event_log(root: Path, *, expect_events: bool) -> dict[str, Any]:
    """Verify every active event segment is fully-parseable JSONL, and
    that we actually read something.

    Segments are enumerated through `events._active_segment_paths` —
    the SAME helper the product's readers use — never a hard-coded
    filename. This gate used to open a literal `root / ".events.jsonl"`,
    which stopped existing in 3.24.0 when the active log became the
    sharded `.events.NN.jsonl` set. `Path.exists()` was simply False on
    every store the benchmark creates, so the loop body never ran and
    the check reported "no corruption" without having opened a byte.
    Going through the product's own helper means the benchmark cannot
    drift away from the layout again.

    The vacuity guard below is the other half: a run that recorded
    events MUST have found segments and read a positive number of bytes
    and lines. A silent pass on zero input is treated as a failure of
    the gate, not a clean bill of health — the numbers in
    docs/swarm-convergence-plan.md rest on this check having run.
    """
    problems: list[str] = []
    segments = _active_segment_paths(root)
    event_bytes = 0
    event_lines = 0

    for seg in segments:
        try:
            event_bytes += seg.stat().st_size
            raw_text = seg.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"event segment {seg.name} is unreadable: {exc}")
            continue
        for raw in raw_text.splitlines():
            if not raw.strip():
                continue
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                problems.append(
                    f"event log has a malformed (torn) JSON line in {seg.name}"
                )
                break
            event_lines += 1

    if expect_events:
        if not segments:
            problems.append(
                "event-log check found NO active segments — the corruption "
                "gate read nothing, so its verdict is meaningless"
            )
        elif event_bytes <= 0 or event_lines <= 0:
            problems.append(
                f"event-log check read {event_bytes} bytes / {event_lines} "
                f"events from {len(segments)} segment(s) — the corruption "
                "gate had no input to check"
            )

    return {
        "problems": problems,
        "segments": len(segments),
        "event_bytes": event_bytes,
        "event_lines": event_lines,
    }


def _aggregate(
    n_agents: int, ops: int, wall: float, results: list[dict[str, Any]]
) -> dict[str, Any]:
    all_lat: list[float] = []
    tally = {
        "search": 0,
        "write": 0,
        "update": 0,
        "verify": 0,
        "remove": 0,
        "cas_reject": 0,
        "conc_err": 0,
    }
    for r in results:
        all_lat.extend(r["lat"])
        for k, v in r["tally"].items():
            tally[k] += v

    completed = sum(tally[k] for k in ("search", "write", "update", "verify", "remove"))
    attempts = n_agents * ops
    all_lat.sort()
    return {
        "agents": n_agents,
        "ops_per_agent": ops,
        "attempts": attempts,
        "completed": completed,
        "wall_s": wall,
        "throughput_ops_s": completed / wall if wall > 0 else 0.0,
        "lat_p50_ms": _pct(all_lat, 50) * 1000,
        "lat_p99_ms": _pct(all_lat, 99) * 1000,
        "lat_max_ms": (all_lat[-1] * 1000) if all_lat else 0.0,
        "cas_reject": tally["cas_reject"],
        "conc_err": tally["conc_err"],
        "tally": tally,
    }


def _run_one(
    base: Path, n_agents: int, ops: int, seed_corpus: int, seed: int, events: bool
) -> dict[str, Any]:
    """One sweep point on a FRESH store dir so runs don't carry state."""
    root = base / f"store_{n_agents}a_{'ev' if events else 'noev'}"
    root.mkdir(parents=True, exist_ok=True)
    _seed_corpus(root, seed_corpus, seed)

    ctx = mp.get_context("spawn")
    t0 = time.perf_counter()
    with ctx.Pool(n_agents) as pool:
        results = pool.map(
            _agent,
            [(str(root), a, ops, seed, events) for a in range(n_agents)],
        )
    wall = time.perf_counter() - t0

    metrics = _aggregate(n_agents, ops, wall, results)
    metrics["events"] = events
    metrics["corruption"] = _check_invariants(root, results, expect_events=events)
    return metrics


def _format_text(sweep: list[dict[str, Any]], ab: dict[str, Any]) -> str:
    lines = []
    lines.append("")
    lines.append("bettermemory fleet benchmark — swarm-convergence Phase 0")
    lines.append("=" * 62)
    lines.append("")
    lines.append(
        "| agents | completed | wall s | ops/s  | p50 ms | p99 ms | cas-rej | corrupt |"
    )
    lines.append(
        "|-------:|----------:|-------:|-------:|-------:|-------:|--------:|:-------:|"
    )
    for m in sweep:
        corrupt = "OK" if m["corruption"]["ok"] else "FAIL"
        lines.append(
            f"| {m['agents']:>6} | {m['completed']:>9} | {m['wall_s']:>6.2f} "
            f"| {m['throughput_ops_s']:>6.0f} | {m['lat_p50_ms']:>6.2f} "
            f"| {m['lat_p99_ms']:>6.2f} | {m['cas_reject']:>7} | {corrupt:^7} |"
        )
    lines.append("")

    # Peak + honest one-liner.
    peak = max(sweep, key=lambda m: m["throughput_ops_s"])
    all_ok = all(m["corruption"]["ok"] for m in sweep)
    lines.append(
        f"Peak sustained: {peak['throughput_ops_s']:.0f} ops/s at "
        f"{peak['agents']} agents."
    )
    # The corruption verdict carries its own evidence: what the gate
    # actually opened. "Zero corruption" next to `0 segments / 0 event
    # lines` is a gate that read nothing and must read as such — that
    # is precisely how the pre-fix version survived (see
    # `_check_event_log`). Printed unconditionally, so a FAIL row is
    # just as auditable as a pass.
    md_seen = sum(m["corruption"]["md_parsed"] for m in sweep)
    segs = sum(m["corruption"]["segments"] for m in sweep)
    ev_lines = sum(m["corruption"]["event_lines"] for m in sweep)
    ev_bytes = sum(m["corruption"]["event_bytes"] for m in sweep)
    lines.append(
        f"Corruption gate: {'zero corruption' if all_ok else 'FAILED'} across "
        f"{len(sweep)} sweep point(s) — inspected {md_seen} memory file(s), "
        f"{segs} active event segment(s), {ev_lines} event line(s) "
        f"({ev_bytes} bytes) parsed as JSON."
    )
    if not all_ok:
        for m in sweep:
            for problem in m["corruption"]["problems"]:
                lines.append(f"  corruption [{m['agents']} agents]: {problem}")

    # Event-log tax.
    on, off = ab["on"], ab["off"]
    if on["throughput_ops_s"] > 0:
        tax = 100 * (1 - on["throughput_ops_s"] / off["throughput_ops_s"])
        speedup = off["throughput_ops_s"] / on["throughput_ops_s"]
        lines.append(
            f"Event-log cost at {on['agents']} agents: "
            f"{on['throughput_ops_s']:.0f} ops/s with logging on vs "
            f"{off['throughput_ops_s']:.0f} off "
            f"({tax:.0f}% of throughput, {speedup:.2f}x). "
            f"The active log is now {SHARD_COUNT}-way sharded (no global "
            f"lock), so this residual is per-event redaction + fsync, not "
            f"cross-session contention."
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fleet-concurrency benchmark (swarm-convergence Phase 0)."
    )
    cpu = os.cpu_count() or 4
    parser.add_argument(
        "--agents",
        default=f"1,2,4,8,{cpu},{2 * cpu}",
        help=(
            "comma-separated agent counts to sweep. Default derives from "
            f"core count (here: 1,2,4,8,{cpu},{2 * cpu}). Past ~2x physical "
            "cores you measure the OS scheduler, not the store."
        ),
    )
    parser.add_argument(
        "--ops", type=int, default=150, help="operations per agent. Default 150."
    )
    parser.add_argument(
        "--seed-corpus",
        type=int,
        default=40,
        help="memories pre-seeded before the swarm runs. Default 40.",
    )
    parser.add_argument(
        "--seed", type=int, default=1234, help="RNG seed. Default 1234."
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output.")
    parser.add_argument("--keep", action="store_true", help="keep the tmp store dir.")
    args = parser.parse_args()

    agent_counts = sorted({int(x) for x in args.agents.split(",") if x.strip()})
    base = Path(tempfile.mkdtemp(prefix="bettermemory-swarm-"))

    if any(n > 3 * cpu for n in agent_counts):
        print(
            f"warning: agent counts above ~{3 * cpu} (3x this box's {cpu} cores) "
            "oversubscribe the CPU — those rows measure scheduler thrash, not "
            "store throughput. Run them on a bigger box for a real number.",
            file=sys.stderr,
        )

    try:
        sweep: list[dict[str, Any]] = []
        for n in agent_counts:
            print(f"running {n} agents x {args.ops} ops…", file=sys.stderr, flush=True)
            sweep.append(
                _run_one(base, n, args.ops, args.seed_corpus, args.seed, events=True)
            )

        # A/B the event-log lock at a real-contention but non-thrashing
        # point: the largest swept count that stays within ~2x cores.
        ab_candidates = [n for n in agent_counts if n <= 2 * cpu] or agent_counts
        ab_n = max(ab_candidates)
        print(f"A/B event-log tax at {ab_n} agents…", file=sys.stderr, flush=True)
        on = next((m for m in sweep if m["agents"] == ab_n and m["events"]), None)
        if on is None:
            on = _run_one(
                base, ab_n, args.ops, args.seed_corpus, args.seed, events=True
            )
        ab = {
            "on": on,
            "off": _run_one(
                base, ab_n, args.ops, args.seed_corpus, args.seed, events=False
            ),
        }

        if args.json:
            print(json.dumps({"sweep": sweep, "ab": ab}, indent=2))
        else:
            print(_format_text(sweep, ab))
        return 0
    finally:
        if args.keep:
            print(f"kept store dir: {base}", file=sys.stderr)
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
