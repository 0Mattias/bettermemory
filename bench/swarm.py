"""Fleet-concurrency benchmark for bettermemory (swarm-convergence Phase 0).

Answers the question the LinkedIn draft couldn't: how many agents can
actually share one store, and where does throughput stop scaling?

It spawns N real agent processes (fresh interpreters under
`multiprocessing spawn`, the same posture as tests/test_concurrency.py
— `fork` would share the parent's fds and short-circuit the
cross-process locks) against one shared store. Each agent runs a
realistic, read-heavy op-mix (search / write / update / verify /
remove) on its OWN memories plus a shared read corpus.

Where the contention is on the bettermemory 9 store: one SQLite file
per store, WAL mode, a 5-second busy timeout. WAL admits many
concurrent readers and exactly one writer per database, and every
mutation the fleet performs is one transaction that appends a signed
log row and applies it to its table, so the write half of the op-mix
serialises on that writer while searches read without blocking. There
are no per-file locks and no per-shard locks any more: the event a
mutation records is a row in the same log, inside the same
transaction. Agents only ever mutate memories they wrote themselves,
so the CAS on `updated` rarely fires and the run measures the store
rather than same-memory fighting.

Three things come out:

1. A scaling curve — sustained ops/sec and p50/p99 latency as the
   agent count climbs. This is the real, measured number that
   replaces the invented "200+".
2. The event-log tax — the same workload run with event-logging on
   vs off at the top agent count. A telemetry row is one more signed
   log row per op, in its own transaction, so the gap is the per-event
   cost: redaction, the MAC, the append and the fsync behind it.
3. A corruption check — after every run: no agent process crashed,
   every active record loads and the loaded count matches the table's
   row count, every tombstone carries `removed`, and `log_verify`
   reads `ok` (every MAC verifies, the chain is unbroken, the head
   names the last row, and every table matches a refold of the log).
   The gate prints the evidence it collected (records loaded, log rows
   verified, telemetry rows read) next to its verdict, because a gate
   that passes without having read anything is worse than no gate.
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


from bettermemory.events import Recorder  # noqa: E402
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import (  # noqa: E402
    ConcurrentUpdateError,
    MemoryNotFoundError,
    NotTombstonedError,
    Store,
    TombstonedError,
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
# memories, so the shared contention is the store's single SQLite
# writer that every mutation passes through, not same-memory fighting.
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


def _agent(args: tuple[str, str, int, int, int, bool]) -> dict[str, Any]:
    """One agent process: run `num_ops` mixed operations against the
    shared store, timing each and recording an event per op when
    `record_events` is set, mirroring the MCP handler telemetry that
    fires on every real tool call. Each agent opens its own connection
    to the one store file under its own `session_id`; every mutation
    and every event is a transaction on the store's single writer.

    Returns per-op latencies and an outcome tally. Contention outcomes
    (a peer tombstoned our target, or the CAS rejected a stale
    snapshot) are counted, never raised; the benchmark asserts on the
    aggregate.
    """
    root, keys_dir, agent_id, num_ops, seed, record_events = args
    store = Store(Path(root), keys_dir=Path(keys_dir))
    recorder = (
        Recorder(store=store, session_id=f"agent-{agent_id}") if record_events else None
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
                # The realistic read path the handler uses: the FTS5
                # candidate query for a bounded set, then the rows by
                # id, O(candidates); a candidate a peer tombstoned in
                # between is skipped by `load_many`, like the handler.
                q = _query(rng)
                pairs = store.query_candidates(q, max_results=20)
                cands = store.load_many([mid for mid, _ in pairs])
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

    store.close()
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


def _seed_corpus(root: Path, keys_dir: Path, n: int, seed: int) -> None:
    """Single-process pre-seed so agent searches have real content to
    rank from op one."""
    rng = random.Random(seed)
    with Store(root, keys_dir=keys_dir) as store:
        for _ in range(n):
            store.write(content=_body(rng, -1), scopes=["seed"])


def _check_invariants(
    root: Path, keys_dir: Path, results: list[dict[str, Any]], *, expect_events: bool
) -> dict[str, Any]:
    """Post-run corruption check. Four things, in order:

    1. No agent process crashed (a crash surfaces as a non-dict result).
    2. Every active record loads, AND the loaded count equals the
       memories table's row count.
    3. Every tombstone carries `removed`.
    4. `log_verify` reads `ok`: every row's MAC verifies under the key,
       the chain is unbroken, the head names the last row, and every
       table equals a refold of the log.

    Returns a report dict with a boolean `ok`, the `problems` list, and
    the COUNTS the gate actually inspected (`records_loaded`,
    `records_rows`, `log_rows`, `event_rows`), which the caller prints
    next to the verdict so a pass is auditable rather than merely
    asserted.

    `expect_events` is the run's own `events` flag. When it is set the
    check is required to have READ telemetry rows: a corruption gate
    that reads nothing is worse than no gate.
    """
    problems: list[str] = []

    if not all(isinstance(r, dict) and "tally" in r for r in results):
        problems.append("an agent process crashed or returned a bad result")

    with Store(root, keys_dir=keys_dir) as store:
        loaded = store.load_all()
        rows = store.count_memories()
        if len(loaded) != rows:
            problems.append(f"active record gap: {len(loaded)} loaded vs {rows} rows")
        for summary in store.list_tombstones():
            if summary.removed is None:
                problems.append(f"tombstone {summary.id} missing `removed`")
                break
        report = store.log_verify()
        if report["status"] != "ok":
            problems.append(
                f"log verify reads {report['status']}: "
                f"{[p for p in report.get('problems', [])][:3]}"
            )
        log_rows = int(report.get("rows") or 0)
        event_rows = sum(1 for _ in store.iter_events())

    if expect_events and event_rows <= 0:
        problems.append(
            "the event-log check read no telemetry rows; the corruption gate "
            "had no input to check"
        )

    return {
        "ok": not problems,
        "problems": problems,
        "records_loaded": len(loaded),
        "records_rows": rows,
        "log_rows": log_rows,
        "event_rows": event_rows,
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
    # The store's key lives under the run's own directory, never under
    # the user's config directory, so a throwaway store leaves no key.
    keys_dir = base / "keys"
    _seed_corpus(root, keys_dir, seed_corpus, seed)

    ctx = mp.get_context("spawn")
    t0 = time.perf_counter()
    with ctx.Pool(n_agents) as pool:
        results = pool.map(
            _agent,
            [(str(root), str(keys_dir), a, ops, seed, events) for a in range(n_agents)],
        )
    wall = time.perf_counter() - t0

    metrics = _aggregate(n_agents, ops, wall, results)
    metrics["events"] = events
    metrics["corruption"] = _check_invariants(
        root, keys_dir, results, expect_events=events
    )
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
    # actually read. "Zero corruption" next to zero rows is a gate that
    # read nothing and must read as such. Printed unconditionally, so a
    # FAIL row is just as auditable as a pass.
    records = sum(m["corruption"]["records_loaded"] for m in sweep)
    log_rows = sum(m["corruption"]["log_rows"] for m in sweep)
    ev_rows = sum(m["corruption"]["event_rows"] for m in sweep)
    lines.append(
        f"Corruption gate: {'zero corruption' if all_ok else 'FAILED'} across "
        f"{len(sweep)} sweep point(s); inspected {records} record(s), "
        f"{log_rows} log row(s) verified, {ev_rows} telemetry row(s) read."
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
            f"Each event is one more signed row on the store's single "
            f"writer, so this is the per-event MAC, append and fsync."
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
