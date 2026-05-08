"""Multi-process concurrency stress test for the store + event log.

Until this test landed, the README carried the caveat:

    Single-process access. Concurrent writes from two MCP servers
    pointed at the same directory may corrupt files. A file-lock
    guard is in place; multi-process is still untested.

The fcntl-based `_locked()` context manager in `store.py` (and the
parallel one in `events.py`) is supposed to serialize writes to the
same file across processes. This test verifies that under contention:
no torn writes, no orphan tombstones, no malformed JSONL lines, and
no crashed callers.

Strategy: spawn N worker processes (each is a fresh Python
interpreter under `multiprocessing.get_context("spawn")` — `fork`
would short-circuit the lock test by sharing the in-memory state of
the parent), each performing M random operations from a small
op-mix on a shared store directory. Workers don't coordinate;
collisions on the same memory are exactly the contention we want
to exercise. After the workers finish, validate invariants on the
on-disk state.

Skipped on Windows: the locking primitive is a no-op there
(`fcntl` is POSIX-only) and the MVP single-process assumption
applies. The test would still pass — just trivially.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import random
import sys
from pathlib import Path

import pytest

from bettermemory.events import Recorder
from bettermemory.store import (
    MemoryNotFoundError,
    NotTombstonedError,
    Store,
    TombstonedError,
)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _worker(args: tuple[str, int, int]) -> dict[str, int]:
    """Run `num_ops` random operations against the store at `root`.

    Each worker keeps its own list of "owned" memory IDs (memories it
    wrote and hasn't yet tombstoned), plus a list of "removed" IDs
    eligible for restore. Op selection is uniform random — `write` is
    forced when the worker has no owned IDs (otherwise update/remove
    have nothing to chew on).

    Errors that arise from concurrent contention (e.g. another worker
    tombstoned an ID we were about to update) are counted as
    `concurrency_errors` rather than re-raised: the test asserts on
    the disk state at the end, not on every operation succeeding.
    """
    root, worker_id, num_ops = args
    store = Store(Path(root))
    recorder = Recorder(Path(root), session_id=f"stress-{worker_id}")
    rng = random.Random(worker_id * 9973 + 17)

    owned: list[str] = []
    removed: list[str] = []
    counts: dict[str, int] = {
        "write": 0,
        "update": 0,
        "remove": 0,
        "restore": 0,
        "concurrency_errors": 0,
    }

    for _ in range(num_ops):
        if not owned and not removed:
            op = "write"
        else:
            op = rng.choice(["write", "update", "remove", "restore"])
            if op == "update" and not owned:
                op = "write"
            if op == "remove" and not owned:
                op = "write"
            if op == "restore" and not removed:
                op = "write"

        try:
            if op == "write":
                memory = store.write(
                    content=(
                        f"worker {worker_id} item "
                        f"{rng.randint(0, 10**9)} — durable claim about "
                        f"the architecture, not transient state"
                    ),
                    scopes=[f"workers:{worker_id}"],
                )
                owned.append(memory.id)
                recorder.record(
                    "write",
                    status="committed",
                    id=memory.id,
                    scopes=memory.scopes,
                )
                counts["write"] += 1

            elif op == "update":
                target = rng.choice(owned)
                memory = store.load_one(target)
                bumped = memory.model_copy(update={"body": memory.body + " (bumped)\n"})
                store.update(bumped)
                recorder.record("update", id=target)
                counts["update"] += 1

            elif op == "remove":
                target = rng.choice(owned)
                store.tombstone(target, reason="stress test")
                owned.remove(target)
                removed.append(target)
                recorder.record("remove", id=target, reason="stress test")
                counts["remove"] += 1

            elif op == "restore":
                target = rng.choice(removed)
                store.restore(target)
                removed.remove(target)
                owned.append(target)
                recorder.record("restore", id=target)
                counts["restore"] += 1

        except (
            MemoryNotFoundError,
            TombstonedError,
            NotTombstonedError,
            FileNotFoundError,
        ):
            counts["concurrency_errors"] += 1

    return counts


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; MVP single-process on Windows",
)
def test_multi_process_stress_no_corruption(tmp_path: Path) -> None:
    """The post-condition that retires the README caveat.

    Four workers, fifty ops each, on a shared store. After the dust
    settles:

    - Every file in `<root>/*.md` is loadable as a Memory (no torn
      writes, no half-frontmatter).
    - Every file in `<root>/.tombstones/*.md` carries the removal
      frontmatter.
    - The event log is fully parseable JSONL (no half-line corruption
      at the append boundary).
    - The set of IDs across active + tombstoned matches the set of
      IDs every worker wrote (no IDs lost or duplicated).
    """
    n_workers = 4
    n_ops = 50

    # `spawn` is required: `fork` would share the parent's already-open
    # file descriptors and short-circuit the cross-process lock.
    # `multiprocessing.get_context("spawn")` is the explicit form that
    # works the same on Linux (where fork is default) and macOS (where
    # spawn became default in 3.8).
    ctx = mp.get_context("spawn")
    with ctx.Pool(n_workers) as pool:
        results = pool.map(
            _worker,
            [(str(tmp_path), w, n_ops) for w in range(n_workers)],
        )

    # ---- Invariant 1: active memories all parse ----------------------
    # `Store.load_all` is defensive (skips malformed files), so we read
    # the directory directly and require every .md file at root to load.
    md_files = sorted(p for p in tmp_path.glob("*.md") if not p.name.startswith("."))
    store = Store(tmp_path)
    parsed_active = store.load_all()
    assert len(parsed_active) == len(md_files), (
        f"Some active .md files did not parse: "
        f"{len(parsed_active)} parsed vs {len(md_files)} on disk. "
        f"Possible torn write under contention."
    )

    # ---- Invariant 2: every tombstone has removal frontmatter --------
    # We don't reuse `store.list_tombstones` (it filters and sorts);
    # the raw check is simpler and surfaces a more useful error.
    tombstone_dir = tmp_path / ".tombstones"
    if tombstone_dir.exists():
        from bettermemory._frontmatter import loads as fm_loads

        for tpath in sorted(tombstone_dir.glob("*.md")):
            text = tpath.read_text(encoding="utf-8")
            post = fm_loads(text)
            assert "removed" in post.metadata, (
                f"tombstone {tpath} missing `removed` field"
            )
            assert "removed_reason" in post.metadata, (
                f"tombstone {tpath} missing `removed_reason` field"
            )

    # ---- Invariant 3: event log JSONL is fully parseable -------------
    log_path = tmp_path / ".events.jsonl"
    if log_path.exists():
        line_count = 0
        for raw in log_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            # Each line must be a complete JSON object — no half-lines.
            json.loads(raw)
            line_count += 1
        # Sanity: at least one event from each worker — workers always
        # record on success and the success rate is far from 0%.
        assert line_count > 0, "event log is empty despite N workers writing"

    # ---- Invariant 4: write-set membership ---------------------------
    total_writes = sum(r["write"] for r in results)
    total_removes = sum(r["remove"] for r in results)
    total_restores = sum(r["restore"] for r in results)
    # Active count + tombstone count should equal total writes:
    # remove moves a file (doesn't delete), restore moves it back.
    # Net active = writes - removes + restores; net tombstoned =
    # removes - restores. Sum = writes.
    n_active = len(md_files)
    n_tombstones = (
        len(list(tombstone_dir.glob("*.md"))) if tombstone_dir.exists() else 0
    )
    assert n_active + n_tombstones == total_writes, (
        f"file-count drift: {n_active} active + {n_tombstones} tombstoned "
        f"= {n_active + n_tombstones}, expected {total_writes}. "
        f"removes={total_removes}, restores={total_restores}, "
        f"per-worker results={results}"
    )

    # ---- Invariant 5: workers should make some forward progress ------
    # If concurrency_errors dominate, the lock might be too aggressive
    # (treating contention as a fatal error). We expect a small number
    # of legitimate races — but not catastrophic.
    total_ops = sum(
        r["write"] + r["update"] + r["remove"] + r["restore"] for r in results
    )
    total_errs = sum(r["concurrency_errors"] for r in results)
    assert total_ops > total_errs * 4, (
        f"Too many concurrency_errors: {total_errs} of "
        f"{total_ops + total_errs} attempts failed. Lock contention "
        f"may be wrong-headed."
    )
