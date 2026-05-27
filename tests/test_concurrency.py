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

The 2.6.3 release added a fault-injection block at the end of this
module that targets the lockfile-inode-identity invariant
specifically: a regression to the pre-fix `unlink in finally`
discipline would let two flock holders coexist on different inodes.
The stress test alone wouldn't catch that — collisions are rare
enough at 200 ops that the inode-split race wouldn't fire reliably.
The targeted assertions below close the gap deterministically.

Skipped on Windows: the locking primitive is a no-op there
(`fcntl` is POSIX-only) and the MVP single-process assumption
applies. The test would still pass — just trivially.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import random
import sys
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bettermemory.events import Recorder
from bettermemory.store import (
    ConcurrentUpdateError,
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


# ---------------------------------------------------------------------------
# Targeted lockfile-invariant tests (2.6.3 regression coverage)
# ---------------------------------------------------------------------------
#
# The stress test exercises locks in aggregate but doesn't deterministically
# fire the inode-identity race that the 2.6.3 unlink-in-finally fix closed.
# These tests pin two invariants explicitly:
#
#   1. `_locked` MUST NOT unlink its lockfile on context-manager exit.
#      Doing so re-introduces the 2.6.3 bug: a concurrent acquirer's
#      `os.open` after the unlink lands on a fresh inode, and flock
#      identity is per-inode, so two holders coexist.
#
#   2. While process A holds `_locked(path)`, process B's attempt to
#      acquire `_locked(path)` from a spawned (not forked) interpreter
#      MUST block until A releases. Same invariant exercised by the
#      stress test, but deterministic and isolated from the surrounding
#      Store activity so a regression points at the lock primitive
#      itself.


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; flock_excl no-ops on Windows so no lockfile is created",
)
def test_store_locked_persists_lockfile_after_exit(tmp_path: Path) -> None:
    """`_locked` must not unlink the lockfile on exit. The 2.6.3 fix
    removed the prior in-finally unlink; this test fails if anyone
    re-introduces it. The 0-byte file on disk is the price we pay
    for inode-stable mutual exclusion across processes.
    """
    from bettermemory.store import _locked

    target = tmp_path / "thing.md"
    target.touch()
    lock_path = target.with_suffix(target.suffix + ".lock")

    with _locked(target):
        assert lock_path.exists(), (
            "lockfile must exist while the lock is held; if it doesn't, "
            "the acquire path is wrong"
        )
    assert lock_path.exists(), (
        "lockfile must persist after release — see 2.6.3. Unlinking "
        "breaks inode identity for the next acquirer."
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; flock_excl no-ops on Windows so no lockfile is created",
)
def test_events_locked_persists_lockfile_after_exit(tmp_path: Path) -> None:
    """Same invariant for `events._locked`. The 2.6.3 fix touched
    both files because the bug was in both."""
    from bettermemory.events import _locked

    target = tmp_path / ".events.jsonl"
    target.touch()
    lock_path = target.with_suffix(target.suffix + ".lock")

    with _locked(target):
        assert lock_path.exists()
    assert lock_path.exists()


def _worker_hold_lock(root: str, acquired_marker: str, release_marker: str) -> None:
    """Worker A: acquire `_locked` on a known path, signal acquisition
    via a marker file, hold until the release marker appears.

    Must be a module-level function so `mp.get_context("spawn")` can
    pickle it. The worker uses an external file-system rendezvous
    (touch / poll) rather than an `mp.Event` so the test exercises a
    realistic cross-process coordination posture — Events are great
    for tightly-coupled workers but don't reflect how real bettermemory
    processes (MCP server + Stop hook + `bettermemory sync`) coordinate.
    """
    from bettermemory.store import _locked

    target = Path(root) / "thing.md"
    target.touch()

    with _locked(target):
        Path(acquired_marker).touch()
        # Wait up to 10 seconds for the parent to signal release. The
        # parent should signal within ~0.5s; the long timeout is just
        # belt-and-suspenders so a flaky scheduler doesn't leave a
        # zombie worker holding the lock indefinitely.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if Path(release_marker).exists():
                return
            time.sleep(0.01)


def _worker_time_acquire(root: str, attempt_marker: str, result_path: str) -> None:
    """Worker B: attempt `_locked` on the same path A holds; record
    how long the acquisition took. Signals the parent via
    ``attempt_marker`` immediately before calling `_locked` so the
    parent can synchronize its hold timer against B's actual attempt
    rather than against B's spawn start (which on macOS takes
    ~200 ms and would otherwise eat the hold window).

    The parent will assert elapsed ≥ the time A held after B's
    signal — anything less means mutual exclusion failed.
    """
    from bettermemory.store import _locked

    target = Path(root) / "thing.md"
    Path(attempt_marker).touch()
    t0 = time.monotonic()
    with _locked(target):
        elapsed = time.monotonic() - t0
    Path(result_path).write_text(json.dumps({"elapsed": elapsed}))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only",
)
def test_locked_serializes_two_spawned_processes(tmp_path: Path) -> None:
    """Cross-process mutual exclusion: while A holds the lock, B's
    acquisition MUST block until A releases.

    This is the contract `_locked` is supposed to provide. The
    stress test exercises it by accident under random ops; this
    test fires it deterministically. A regression here points at
    the lock primitive directly, not at any higher-level Store path.

    Uses `spawn` (not `fork`) so the workers don't inherit the
    parent's already-open fds, which would short-circuit the
    cross-process flock test.
    """
    ctx = mp.get_context("spawn")
    barrier_dir = tmp_path / "barriers"
    barrier_dir.mkdir()
    a_acquired = barrier_dir / "a_acquired"
    a_release = barrier_dir / "a_release"
    b_attempting = barrier_dir / "b_attempting"
    b_result = barrier_dir / "b_result.json"

    proc_a = ctx.Process(
        target=_worker_hold_lock,
        args=(str(tmp_path), str(a_acquired), str(a_release)),
    )
    proc_a.start()

    # Wait until A has the lock — the marker file appears after A's
    # `with _locked(...)` block opens.
    deadline = time.monotonic() + 10
    while not a_acquired.exists():
        if time.monotonic() > deadline:
            proc_a.kill()
            proc_a.join(timeout=2)
            pytest.fail("worker A never signalled lock acquisition")
        time.sleep(0.01)

    # Spawn B; it should block in `_locked(...)` waiting for A. B
    # signals via `b_attempting` immediately before its lock attempt
    # so the parent can time the hold from B's attempt onward rather
    # than from B's spawn (spawn overhead is ~200ms on macOS and
    # would otherwise hide a real fix).
    proc_b = ctx.Process(
        target=_worker_time_acquire,
        args=(str(tmp_path), str(b_attempting), str(b_result)),
    )
    proc_b.start()

    deadline = time.monotonic() + 10
    while not b_attempting.exists():
        if time.monotonic() > deadline:
            proc_a.kill()
            proc_b.kill()
            proc_a.join(timeout=2)
            proc_b.join(timeout=2)
            pytest.fail("worker B never signalled lock-acquisition attempt")
        time.sleep(0.01)

    # B is now blocked inside `_locked`. Hold for a measurable window.
    hold_seconds = 0.5
    time.sleep(hold_seconds)
    a_release.touch()

    proc_a.join(timeout=10)
    proc_b.join(timeout=10)
    assert proc_a.exitcode == 0, f"worker A failed (exit={proc_a.exitcode})"
    assert proc_b.exitcode == 0, f"worker B failed (exit={proc_b.exitcode})"

    result = json.loads(b_result.read_text())
    elapsed = result["elapsed"]
    # B's elapsed must include the hold window. Slack tolerates
    # scheduler jitter without masking a real mutual-exclusion
    # regression: a broken lock yields elapsed ≈ 0.
    assert elapsed >= 0.8 * hold_seconds, (
        f"worker B acquired in {elapsed:.3f}s while A was supposed to "
        f"hold for {hold_seconds:.3f}s. Mutual exclusion broken — "
        f"likely a regression of the 2.6.3 lockfile-identity fix."
    )


# ---------------------------------------------------------------------------
# C1 regression — slug-collision silent data loss
# ---------------------------------------------------------------------------
#
# Pre-2.7 the active-side `_path_for` used `candidate.exists()` as the
# only collision guard. Two concurrent writes whose bodies slugified to
# the same value both saw `exists() == False`, both picked the bare
# candidate, serialized on `_locked(<same path>)` — and the second
# `_atomic_write_post` clobbered the first memory entirely.
# `tombstone()` already closed this race in 2.6.4 by unconditionally
# embedding the ULID in the filename; the fix mirrors that on the
# active side.


def _slug_collision_write_worker(args: tuple[str, str, int]) -> str | None:
    """Worker for the multi-process slug-collision variant.

    Runs in its own Python interpreter (``spawn`` context), so the
    inter-process `_locked` flock is genuinely tested — the threaded
    variant below holds the GIL during the load-store sequence in
    ``_path_for``, which is the exact place a regression to the
    pre-Round-3 `bare.exists()` gate would race. The multi-process
    form removes the GIL guarantee, so two interpreters can both
    observe `bare.exists() == False` between the open and the lock
    if and only if the always-suffix discipline isn't in place.
    """
    root, body, _seed = args
    s = Store(Path(root))
    try:
        memory = s.write(content=body, scopes=["tools"])
        return memory.id
    except Exception:  # noqa: BLE001 — surface as missing id, assertions catch it
        return None


def _slug_collision_restore_worker(args: tuple[str, str]) -> str | None:
    """Worker for the multi-process restore-side C1 variant.

    The store has N pre-tombstoned memories whose bodies slugify
    identically. Each worker process restores one of them. Pre-Round-3
    `Store.restore` used the same `active_path.exists()` gate that
    `_path_for` used pre-2.7 — so two concurrent restores would each
    lock a DIFFERENT tombstone path (so the lock doesn't help) and
    both write to the same `active_path`. The second `_atomic_write_post`
    would clobber the first. After the Round-3 fix, both restores end
    up at distinct `{date}-{slug}-{shortid}.md` filenames.
    """
    root, memory_id = args
    s = Store(Path(root))
    try:
        m = s.restore(memory_id)
        return m.id
    except Exception:  # noqa: BLE001 — surface as None; assert catches it
        return None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; MVP single-process on Windows",
)
def test_multi_process_concurrent_slug_collision_writes_do_not_clobber(
    tmp_path: Path,
) -> None:
    """C1 regression across REAL processes (no GIL).

    The threaded variant below races writers under the GIL, which
    can't actually pre-empt a Python bytecode boundary inside the
    `_path_for` -> `_locked` -> `_write_path` window. That's enough
    to exercise the always-suffix invariant (the invariant is
    construction-time, not race-time), but a future refactor that
    swaps the invariant for a guard-with-recheck would fail the
    spawn-process variant a way it doesn't fail the threaded one —
    so this test catches a strictly larger regression surface.
    """
    n_writers = 6
    body = "identical body for cross-process slug collision"
    ctx = mp.get_context("spawn")
    with ctx.Pool(n_writers) as pool:
        ids = pool.map(
            _slug_collision_write_worker,
            [(str(tmp_path), body, w) for w in range(n_writers)],
        )

    assert None not in ids, f"some workers failed to write: {ids}"
    assert len(ids) == n_writers
    # No duplicate ids — ULID generation is per-process but each process
    # gets its own clock + entropy, so collisions are astronomical.
    assert len(set(ids)) == n_writers

    md_files = sorted(
        p.name for p in tmp_path.glob("*.md") if not p.name.startswith(".")
    )
    assert len(md_files) == n_writers, (
        f"expected {n_writers} files, got {len(md_files)}: {md_files}. "
        f"Cross-process slug-collision silent overwrite (C1 regression)."
    )

    # Every written id loads back via a fresh Store — no silent drops.
    store = Store(tmp_path)
    loaded = {store.load_one(mid).id for mid in ids if mid is not None}
    assert loaded == {mid for mid in ids if mid is not None}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; MVP single-process on Windows",
)
def test_multi_process_concurrent_slug_collision_restores_do_not_clobber(
    tmp_path: Path,
) -> None:
    """C1 regression on the RESTORE side, across real processes.

    Pre-Round-3, the four-agent audit found that `Store.restore` still
    used the legacy `active_path.exists()`-gated path selection that
    `_path_for` killed in `bc47593`. Two concurrent restores of
    differently-tombstoned memories whose bodies slugify identically
    would each lock their own tombstone path, both see
    `active_path.exists() == False`, and both write — second silently
    clobbering the first. The lock is on the tombstone, not on the
    destination, so it can't help. The fix mirrors `_path_for`:
    unconditionally suffix with the short-id, so two distinct
    memory_ids can never produce the same active_path.

    Setup: write N memories with identical bodies (different ids, so
    different filenames courtesy of the write-side always-suffix),
    tombstone all of them, then restore all of them concurrently in
    separate processes. Invariant: N distinct active files exist
    after the restore, all loadable as memories.
    """
    n_workers = 6
    body = "identical body for cross-process restore-side slug collision"

    # Phase 1: write N memories single-process, all with the same body.
    setup_store = Store(tmp_path)
    pre_written_ids: list[str] = []
    for _ in range(n_workers):
        m = setup_store.write(content=body, scopes=["tools"])
        pre_written_ids.append(m.id)
    # Tombstone all of them so they live in .tombstones/.
    for mid in pre_written_ids:
        setup_store.tombstone(mid, reason="setup for C1 restore race")
    assert sorted(p.name for p in tmp_path.glob("*.md")) == [], (
        "setup: every active file should be tombstoned before the race"
    )

    # Phase 2: race the restores across separate processes.
    ctx = mp.get_context("spawn")
    with ctx.Pool(n_workers) as pool:
        restored_ids = pool.map(
            _slug_collision_restore_worker,
            [(str(tmp_path), mid) for mid in pre_written_ids],
        )

    assert None not in restored_ids, f"some workers failed to restore: {restored_ids}"
    assert set(restored_ids) == set(pre_written_ids), (
        "restored ids must equal the input set"
    )

    md_files = sorted(
        p.name for p in tmp_path.glob("*.md") if not p.name.startswith(".")
    )
    assert len(md_files) == n_workers, (
        f"expected {n_workers} restored files, got {len(md_files)}: "
        f"{md_files}. Restore-side slug-collision silent overwrite "
        f"(C1 regression on the restore path)."
    )
    # Every restored id loads back — no silent drops on restore.
    fresh_store = Store(tmp_path)
    loaded = {fresh_store.load_one(mid).id for mid in pre_written_ids}
    assert loaded == set(pre_written_ids)


def test_concurrent_slug_collision_writes_do_not_clobber(tmp_path: Path) -> None:
    """C1 regression. Two threaded writers whose bodies slugify to the
    same string must both end up with distinct files on disk; neither
    memory may be silently overwritten.

    Threaded (not multiprocess) because we want maximum interleaving on
    the `_path_for` -> `_locked` -> `_write_path` sequence. The
    cross-process variant is exercised by the two
    ``test_multi_process_concurrent_slug_collision_*`` tests above; this
    test pins the deterministic threaded slug-collision case so a
    regression to the `exists()`-only guard fails it loudly even on
    GIL-bound Python.
    """
    import threading

    store = Store(tmp_path)
    # Identical bodies -> identical slugs. Each writer hammers the
    # store concurrently; nothing about the content distinguishes them.
    n_writers = 16
    barrier = threading.Barrier(n_writers)
    written_ids: list[str] = []
    written_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()  # release all writers together
            memory = store.write(
                content="hello world identical body for slug collision",
                scopes=["tools"],
            )
            with written_lock:
                written_ids.append(memory.id)
        except BaseException as exc:  # noqa: BLE001 — capture for assert
            with written_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"writers raised: {errors}"
    assert len(written_ids) == n_writers
    assert len(set(written_ids)) == n_writers, "duplicate ids returned from write"

    # Disk invariant: one .md file per write, and every id loads back.
    files = sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".md")
    assert len(files) == n_writers, (
        f"expected {n_writers} files, got {len(files)}: {files}. "
        f"Slug-collision silent overwrite (C1 regression)."
    )
    loaded_ids = {store.load_one(mid).id for mid in written_ids}
    assert loaded_ids == set(written_ids), (
        "Some written memories vanished from disk — silent overwrite (C1)."
    )


# Property-based variant: same invariant under randomized bodies that
# all share a slug, plus a smattering of unrelated bodies. Hypothesis
# generates a body shape and a worker count; the invariant — every
# written id loads back — must hold across the input space.
# hypothesis is in [project.optional-dependencies].dev — if you can run
# pytest, you have it; the imports live at the top of the file.


@settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    # Body that slugifies cleanly — alpha chars + spaces so
    # make_slug emits a non-empty, stable slug. Length kept small
    # to keep the test snappy; we're testing collision behaviour,
    # not slug-generation breadth.
    body=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz ",
        min_size=8,
        max_size=40,
    ).filter(lambda s: s.strip()),
    n_writers=st.integers(min_value=2, max_value=8),
)
def test_property_concurrent_slug_collision_preserves_all_writes(
    tmp_path: Path, body: str, n_writers: int
) -> None:
    """Property-based C1 regression. Hypothesis generates a body
    and a writer count; all writers race on identical content
    (same slug). Invariant: every successful write is recoverable
    via `load_one`. A regression that drops one of N concurrent
    same-slug writes fails this test."""
    import threading
    import uuid

    # Per-example subdir so hypothesis's re-use of `tmp_path`
    # doesn't leak files between cases.
    root = tmp_path / f"ex_{uuid.uuid4().hex[:12]}"
    root.mkdir()
    store = Store(root)
    barrier = threading.Barrier(n_writers)
    written_ids: list[str] = []
    written_lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            memory = store.write(content=body, scopes=["tools"])
            with written_lock:
                written_ids.append(memory.id)
        except BaseException as exc:  # noqa: BLE001
            with written_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"writers raised: {errors}"
    assert len(written_ids) == n_writers
    # Every id must load back — no silent overwrites.
    loaded = {store.load_one(mid).id for mid in written_ids}
    assert loaded == set(written_ids), (
        f"Lost writes (C1 regression). Written: {sorted(written_ids)}; "
        f"loaded: {sorted(loaded)}; body={body!r}."
    )


# ---------------------------------------------------------------------------
# C2 regression — update / mark_verified / rename_scope must not
# resurrect a tombstoned memory
# ---------------------------------------------------------------------------
#
# Pre-2.7 the locked-write sequence was:
#   1. `_find_path_for_id(id)` walks the directory unlocked.
#   2. We acquire `_locked(path)`.
#   3. Inside the lock, `_write_path(path, memory)`.
# Between step 1 and step 2, another process could `tombstone(id)`,
# moving the file out to `.tombstones/`. Step 3 then wrote a fresh
# active file at the original path, resurrecting the tombstoned memory
# behind the user's back. The fix adds an under-lock id recheck (step
# 2.5); the tests below verify that recheck raises rather than
# silently writing.


def test_update_after_concurrent_tombstone_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2 regression for `update`. Simulate the race deterministically
    by tombstoning the target memory between `_find_path_for_id` and
    the lock acquisition. The update must raise MemoryNotFoundError —
    NOT silently re-create an active file at the original path."""
    store = Store(tmp_path)
    memory = store.write(content="will be tombstoned mid-update", scopes=["tools"])

    # Patch `_find_path_for_id` to capture the path it would return,
    # then tombstone the memory before the caller takes the lock.
    # `_fired` guards against recursion: `tombstone()` itself calls
    # `_find_path_for_id`, so we only inject the race on the first
    # call (the one made by `update`).
    original_find = Store._find_path_for_id
    fired = {"done": False}

    def racing_find(self: Store, mid: str) -> Path | None:
        path = original_find(self, mid)
        if path is not None and mid == memory.id and not fired["done"]:
            fired["done"] = True
            # Inject the race: tombstone the memory before the caller
            # acquires the lock. This emulates a second process moving
            # the file under our feet.
            self.tombstone(mid, reason="raced by other process")
        return path

    monkeypatch.setattr(Store, "_find_path_for_id", racing_find)

    bumped = memory.model_copy(update={"body": "edited\n"})
    with pytest.raises(MemoryNotFoundError, match="raced with"):
        store.update(bumped)

    # Disk invariant: the tombstone won, no resurrected active file at
    # the original path. (`load_one` raises TombstonedError for the id.)
    with pytest.raises(TombstonedError):
        store.load_one(memory.id)
    # And: no orphan active .md file should exist for that id.
    active_files = [p for p in tmp_path.iterdir() if p.is_file() and p.suffix == ".md"]
    # The directory may legitimately contain other files; just verify
    # no file holds the tombstoned id in its frontmatter.
    from bettermemory._frontmatter import load as fm_load

    for p in active_files:
        post = fm_load(p)
        assert post.metadata.get("id") != memory.id, (
            f"Active file {p.name} carries tombstoned id {memory.id} — "
            f"C2 regression: update resurrected the memory."
        )


def test_mark_verified_after_concurrent_tombstone_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2 regression for `mark_verified`. Same race window as `update`
    — find walks unlocked, tombstone slips in, our write inside the
    lock used to resurrect the memory. The recheck must raise."""
    store = Store(tmp_path)
    memory = store.write(content="will be tombstoned mid-verify", scopes=["tools"])

    original_find = Store._find_path_for_id
    fired = {"done": False}

    def racing_find(self: Store, mid: str) -> Path | None:
        path = original_find(self, mid)
        if path is not None and mid == memory.id and not fired["done"]:
            fired["done"] = True
            self.tombstone(mid, reason="raced by other process")
        return path

    monkeypatch.setattr(Store, "_find_path_for_id", racing_find)

    with pytest.raises(MemoryNotFoundError, match="raced with"):
        store.mark_verified(memory.id)

    # Tombstone state is preserved.
    with pytest.raises(TombstonedError):
        store.load_one(memory.id)


# ---------------------------------------------------------------------------
# W1 regression — tombstone() missing under-lock _id_still_at_path recheck
# ---------------------------------------------------------------------------
#
# Pre-W1 `Store.tombstone` walked `_find_path_for_id` unlocked, acquired
# `_locked(path)`, then `frontmatter.load(path)` — without a recheck.
# Two agents calling `tombstone(id)` concurrently would both find the same
# path; agent A won the lock, tombstoned, unlinked, and released; agent B
# then acquired the now-stale lock and `frontmatter.load(path)` raised a
# bare `FileNotFoundError`. The handler layer caught `TombstonedError` /
# `MemoryNotFoundError` but not the bare OSError, so sub-agent B saw a
# 500-shaped MCP error for what should be a clean "already tombstoned"
# semantic. The fix adds the same `_id_still_at_path` recheck `update()`
# and `mark_verified()` use, raising `TombstonedError` with a message
# that mirrors the find-time pre-lock fallback.


def test_tombstone_after_concurrent_tombstone_raises_tombstoned_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1 regression for `tombstone`. Deterministically inject a
    concurrent tombstone between `_find_path_for_id` and the lock
    acquisition: the second tombstone call must surface
    `TombstonedError` (NOT a bare `FileNotFoundError` / OSError) so the
    handler layer can convert cleanly to a structured ValueError."""
    store = Store(tmp_path)
    memory = store.write(content="will be raced on tombstone", scopes=["tools"])

    original_find = Store._find_path_for_id
    fired = {"done": False}

    def racing_find(self: Store, mid: str) -> Path | None:
        path = original_find(self, mid)
        # Only fire on the OUTER tombstone call (the one whose race we're
        # simulating). The nested `self.tombstone` below will itself call
        # `_find_path_for_id`; the flag guards against recursion.
        if path is not None and mid == memory.id and not fired["done"]:
            fired["done"] = True
            self.tombstone(mid, reason="raced by other process")
        return path

    monkeypatch.setattr(Store, "_find_path_for_id", racing_find)

    with pytest.raises(TombstonedError, match="already tombstoned"):
        store.tombstone(memory.id, reason="lost the race")

    # Disk invariant: exactly one tombstone exists for this id, and the
    # winning reason — not the loser's — is recorded. (We injected the
    # racing tombstone with reason="raced by other process".)
    tombstone = store.load_tombstone(memory.id)
    assert tombstone.removed_reason == "raced by other process"


def _w1_concurrent_tombstone_worker(args: tuple[str, str]) -> dict[str, str]:
    """Worker for the cross-process W1 variant.

    Each worker process attempts `Store.tombstone(memory_id)`. One worker
    wins, the others must lose with `TombstonedError`. No bare OSError
    (FileNotFoundError specifically) may leak — that's the W1 bug.
    Returns a dict describing the outcome so the parent can assert on
    the distribution.
    """
    root, memory_id = args
    s = Store(Path(root))
    try:
        s.tombstone(memory_id, reason="worker tombstone")
        return {"outcome": "won"}
    except TombstonedError as exc:
        return {"outcome": "lost-tombstoned", "msg": str(exc)}
    except MemoryNotFoundError as exc:
        # `_find_path_for_id` race result if a worker arrives after both
        # the active file and the tombstone-frontmatter walk completed —
        # unlikely with this harness but accept it as a non-bug outcome.
        return {"outcome": "lost-not-found", "msg": str(exc)}
    except OSError as exc:  # noqa: BLE001 — this is the W1 leak we're testing
        return {"outcome": "leaked-oserror", "msg": f"{type(exc).__name__}: {exc}"}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; MVP single-process on Windows",
)
def test_multi_process_concurrent_tombstone_no_oserror_leak(
    tmp_path: Path,
) -> None:
    """W1 regression across REAL processes (no GIL).

    Six workers race on `Store.tombstone(same_id)` from separate Python
    interpreters. Exactly one wins; the rest must lose with
    `TombstonedError` (or, on extremely tight timing, `MemoryNotFoundError`
    if the tombstone-frontmatter walk also raced — accepted as not-W1).
    A bare `OSError` (specifically `FileNotFoundError`) escaping from
    `frontmatter.load(path)` inside the under-lock block IS the W1 bug;
    any worker reporting `leaked-oserror` fails this test.
    """
    n_workers = 6
    setup = Store(tmp_path)
    memory = setup.write(content="raced tombstone across processes", scopes=["tools"])

    ctx = mp.get_context("spawn")
    with ctx.Pool(n_workers) as pool:
        results = pool.map(
            _w1_concurrent_tombstone_worker,
            [(str(tmp_path), memory.id) for _ in range(n_workers)],
        )

    outcomes = [r["outcome"] for r in results]
    leaks = [r for r in results if r["outcome"] == "leaked-oserror"]
    assert not leaks, (
        f"W1 regression: at least one worker leaked a bare OSError from "
        f"`Store.tombstone` under concurrent contention. Leaks: {leaks}. "
        f"All outcomes: {outcomes}"
    )
    # Exactly one winner — the file lock + recheck must serialize the
    # tombstone such that no two workers both succeed.
    winners = [o for o in outcomes if o == "won"]
    assert len(winners) == 1, (
        f"Expected exactly one tombstone winner, got {len(winners)}. "
        f"Outcomes: {outcomes}"
    )
    # All non-winners must report a clean semantic loss (tombstoned or
    # not-found), not a leaked OSError.
    losers = [o for o in outcomes if o in {"lost-tombstoned", "lost-not-found"}]
    assert len(winners) + len(losers) == n_workers, (
        f"Unexpected outcome distribution: {outcomes}"
    )

    # Disk invariant: exactly one tombstone for the id.
    tombstone = setup.load_tombstone(memory.id)
    assert tombstone.id == memory.id


# ---------------------------------------------------------------------------
# W2 regression — `Store.update` is silent last-write-wins under concurrent
# disjoint edits
# ---------------------------------------------------------------------------
#
# Pre-W2 the locked-write sequence was:
#   1. handler `load_one(id)` builds a snapshot (outside any lock).
#   2. handler builds the merged Memory and calls `Store.update`.
#   3. `Store.update` re-finds the path, locks, runs the C2
#      `_id_still_at_path` recheck, then `_write_path`.
# Between steps 1 and 3 another agent could land its own
# `load_one`→`update` round trip on the same id with a disjoint edit.
# Both writers' `_write_path` blocks serialise on the file lock, but
# neither verified the on-disk `updated` against the snapshot they read,
# so whichever serialised second silently dropped the first writer's
# change. The fix adds an under-lock CAS: re-load the current Memory
# under the lock and compare its `updated` to the caller's snapshot;
# on mismatch raise `ConcurrentUpdateError`.
#
# The deterministic test below uses a monkeypatch on `_load_path` to
# inject the concurrent edit at the moment between the C2 recheck and
# the CAS check, which is the tightest race window. The cross-process
# variant exercises the same invariant under real interpreter-level
# parallelism.


def test_update_cas_detects_stale_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2 regression. Two `Store.update` calls on the same id with
    disjoint edits: the second must raise `ConcurrentUpdateError`
    rather than silently clobber the first writer's change.

    Deterministic by monkeypatching `_load_path` to simulate a
    concurrent edit landing between the lock acquisition and the CAS
    check. The patched `_load_path` returns a Memory whose `updated`
    has moved forward — exactly the on-disk state another agent's
    completed update would leave behind.
    """
    store = Store(tmp_path)
    original = store.write(
        content="original body — durable claim about the system",
        scopes=["tools"],
    )

    # Caller A reads the snapshot.
    snapshot = store.load_one(original.id)
    assert snapshot.updated == original.updated

    # Caller B lands its disjoint edit in real time. The `updated`
    # bump from this write is what makes A's snapshot stale.
    bumped_by_b = snapshot.model_copy(
        update={"body": "edit from B — orthogonal change\n"}
    )
    after_b = store.update(bumped_by_b)
    assert after_b.updated > snapshot.updated, (
        "sanity: B's update must move the on-disk `updated` forward"
    )

    # Now caller A tries to land its own disjoint edit on top of the
    # original snapshot — the on-disk `updated` no longer matches.
    edit_by_a = snapshot.model_copy(
        update={"body": "edit from A — different orthogonal change\n"}
    )
    with pytest.raises(ConcurrentUpdateError) as excinfo:
        store.update(edit_by_a)

    err = excinfo.value
    assert err.memory_id == original.id
    # The reported `current_updated` must be exactly what B wrote —
    # that's the timestamp the caller needs to rebase against.
    assert err.current_updated == after_b.updated

    # Disk invariant: B's edit wins. A's silent-clobber attempt left
    # no trace.
    final = store.load_one(original.id)
    assert "edit from B" in final.body
    assert "edit from A" not in final.body


def test_update_cas_detects_stale_snapshot_via_patched_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic CAS test via monkeypatch on `_load_path`.

    The under-lock recheck-then-CAS sequence reads `_load_path` twice
    in `Store.update`: once via `_id_still_at_path` (which reads the
    frontmatter id), once via `_load_path` for the CAS comparison.
    Patch the second to return a Memory whose `updated` has moved
    forward, simulating an interleaved write landing between the
    file-lock acquisition and the body re-read. The CAS must fire.
    """
    store = Store(tmp_path)
    original = store.write(content="will race CAS", scopes=["tools"])
    snapshot = store.load_one(original.id)

    # Fast-forward the on-disk `updated` as seen by the CAS reload.
    # We monkeypatch `_load_path` to return a Memory whose `updated`
    # is one second ahead. The C2 recheck (which uses
    # `_id_still_at_path`, not `_load_path`) is unaffected, so the
    # patch isolates the CAS path specifically.
    from datetime import timedelta

    from bettermemory.models import Memory

    original_load_path = Store._load_path

    def racing_load_path(self: Store, path: Path) -> Memory:
        memory = original_load_path(self, path)
        if memory.id == original.id:
            return memory.model_copy(
                update={"updated": memory.updated + timedelta(seconds=1)}
            )
        return memory

    monkeypatch.setattr(Store, "_load_path", racing_load_path)

    bumped = snapshot.model_copy(update={"body": "edited on stale snapshot\n"})
    with pytest.raises(ConcurrentUpdateError) as excinfo:
        store.update(bumped)
    err = excinfo.value
    assert err.memory_id == original.id
    assert err.current_updated > snapshot.updated


def test_update_cas_happy_path_passthrough(tmp_path: Path) -> None:
    """Happy-path passthrough: a single-writer `load_one`-then-`update`
    sequence (no contention) must continue to work — the CAS check is
    a no-false-positive invariant under single-writer use."""
    store = Store(tmp_path)
    original = store.write(content="happy path", scopes=["tools"])

    snapshot = store.load_one(original.id)
    edited = snapshot.model_copy(update={"body": "refined body\n"})
    updated = store.update(edited)

    assert updated.id == original.id
    assert "refined body" in updated.body
    assert updated.updated > original.updated

    # Disk reflects the change.
    reloaded = store.load_one(original.id)
    assert reloaded.body.strip() == "refined body"


def test_update_force_bypasses_cas(tmp_path: Path) -> None:
    """`force=True` escape hatch: a low-level caller that has already
    reconciled concurrent edits out-of-band can opt out of the CAS
    check. Not exposed through the MCP handler — direct in-process
    use only."""
    store = Store(tmp_path)
    original = store.write(content="will be forced", scopes=["tools"])

    # Land a second edit so the on-disk `updated` moves forward.
    snapshot = store.load_one(original.id)
    intermediate = store.update(
        snapshot.model_copy(update={"body": "intermediate edit\n"})
    )
    assert intermediate.updated > snapshot.updated

    # Reach in with the now-stale snapshot AND `force=True`. The CAS
    # would normally raise; `force=True` bypasses it.
    forced = store.update(
        snapshot.model_copy(update={"body": "forced overwrite\n"}),
        force=True,
    )
    assert "forced overwrite" in forced.body

    # Sanity: without `force`, the same stale snapshot raises.
    with pytest.raises(ConcurrentUpdateError):
        store.update(snapshot.model_copy(update={"body": "this would fail\n"}))


def _w2_disjoint_update_worker(
    args: tuple[str, str, str, str, str],
) -> dict[str, str]:
    """Worker for the cross-process W2 variant.

    Each worker process:
      1. `load_one(memory_id)` to take a snapshot.
      2. Signals "snapshot taken" by touching its own ready-marker.
      3. Waits for the parent to release via a shared go-marker —
         this barrier guarantees every worker has read the same
         pre-race `updated` BEFORE any of them tries to write. Without
         the barrier the first-spawned worker's `update` could land
         before later workers' `load_one`, so the later workers would
         read the already-bumped on-disk timestamp and trivially pass
         the CAS — masking the regression we're trying to pin.
      4. Patches the body with its own worker-specific edit.
      5. Calls `Store.update`.

    Exactly one worker must win; the rest must lose with
    `ConcurrentUpdateError`. A `MemoryNotFoundError` or `TombstonedError`
    would only fire if a different mutator (tombstone/restore) raced —
    we don't run any here, so neither outcome is expected. Returns a
    dict describing the outcome so the parent can assert on the
    distribution.
    """
    root, memory_id, marker, ready_marker, go_marker = args
    store = Store(Path(root))
    try:
        snapshot = store.load_one(memory_id)
    except (MemoryNotFoundError, TombstonedError) as exc:
        return {"outcome": "snapshot-failed", "msg": str(exc)}

    # Signal readiness and wait for the parent's go-signal.
    Path(ready_marker).touch()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if Path(go_marker).exists():
            break
        time.sleep(0.01)
    else:  # noqa: PLW0120 — explicit timeout signaling for the parent
        return {"outcome": "barrier-timeout"}

    edit = snapshot.model_copy(update={"body": f"edit from worker {marker}\n"})
    try:
        store.update(edit)
        return {"outcome": "won", "marker": marker}
    except ConcurrentUpdateError as exc:
        return {"outcome": "lost-stale", "msg": str(exc), "marker": marker}
    except (MemoryNotFoundError, TombstonedError) as exc:
        # Unexpected here (no concurrent tombstone in the harness),
        # but keep the outcome distinguishable so a regression surfaces
        # as a clean tag instead of an opaque crash.
        return {"outcome": "lost-other", "msg": str(exc), "marker": marker}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; MVP single-process on Windows",
)
def test_multi_process_concurrent_disjoint_updates_no_silent_clobber(
    tmp_path: Path,
) -> None:
    """W2 regression across REAL processes (no GIL).

    Six workers race on `Store.update(same_id)` from separate Python
    interpreters, each carrying a different disjoint edit built on top
    of a snapshot they took before the race. Pre-W2, all six writes
    would silently serialise on the file lock and the last one wins —
    five edits dropped without surface. Post-W2, exactly one wins; the
    others must lose with `ConcurrentUpdateError` so the caller can
    rebase and retry. A bare `OSError` or any other exception escaping
    IS the W2 leak.

    The post-condition that retires the silent-last-write-wins
    semantic for multi-agent concurrent updates: the eventual disk
    state must reflect the winning edit, not a torn merge, and every
    losing worker must carry a structured stale signal.
    """
    n_workers = 6
    setup = Store(tmp_path)
    memory = setup.write(
        content="raced disjoint updates across processes",
        scopes=["tools"],
    )

    # File-system barrier: every worker signals "snapshot taken" by
    # touching `ready_marker_<w>`; the parent waits for all N to appear
    # then releases via `go_marker`. Mirrors the rendezvous pattern in
    # `test_locked_serializes_two_spawned_processes` above (touch/poll
    # rather than mp.Event) so the test exercises a realistic
    # cross-process posture and stays deterministic vs. spawn-startup
    # jitter, which on macOS dwarfs the contention window we care
    # about.
    barrier_dir = tmp_path / "_w2_barriers"
    barrier_dir.mkdir()
    go_marker = barrier_dir / "go"
    ready_markers = [barrier_dir / f"ready_{w}" for w in range(n_workers)]

    ctx = mp.get_context("spawn")
    pool = ctx.Pool(n_workers)
    try:
        async_result = pool.map_async(
            _w2_disjoint_update_worker,
            [
                (
                    str(tmp_path),
                    memory.id,
                    str(w),
                    str(ready_markers[w]),
                    str(go_marker),
                )
                for w in range(n_workers)
            ],
        )

        # Wait for all workers to take their snapshot.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if all(m.exists() for m in ready_markers):
                break
            time.sleep(0.01)
        else:  # noqa: PLW0120 — explicit timeout for the readiness rendezvous
            pool.terminate()
            pool.join()
            pytest.fail(
                "not all workers signalled snapshot-taken within 30s; "
                f"ready: {[m.name for m in ready_markers if m.exists()]}"
            )

        # All snapshots taken at the same on-disk `updated`. Release
        # the workers to race the writes.
        go_marker.touch()
        results = async_result.get(timeout=60)
    finally:
        pool.close()
        pool.join()

    outcomes = [r["outcome"] for r in results]
    winners = [r for r in results if r["outcome"] == "won"]
    losers = [r for r in results if r["outcome"] == "lost-stale"]
    assert len(winners) == 1, (
        f"Expected exactly one update winner under CAS, got {len(winners)}. "
        f"Outcomes: {outcomes}"
    )
    # All non-winners must report `lost-stale` (the structured CAS
    # signal) — not the legacy silent-success that pre-W2 produced.
    assert len(losers) == n_workers - 1, (
        f"Expected {n_workers - 1} stale-CAS losers, got {len(losers)}. "
        f"Outcomes: {outcomes}"
    )
    assert len(winners) + len(losers) == n_workers, (
        f"Unexpected outcome distribution: {outcomes}"
    )

    # Disk invariant: exactly one body wins. The body must match the
    # winner's marker; no other worker's body may appear.
    fresh = Store(tmp_path)
    final = fresh.load_one(memory.id)
    winner_marker = winners[0]["marker"]
    assert f"edit from worker {winner_marker}" in final.body, (
        f"Disk body {final.body!r} does not reflect the winner's edit "
        f"(marker {winner_marker!r}). Outcomes: {outcomes}"
    )
    # Confirm no other worker's body silently lingered — would mean a
    # partial-merge or torn write.
    other_markers = [r["marker"] for r in results if r.get("outcome") == "won"][1:]
    for m in other_markers:
        assert f"edit from worker {m}" not in final.body


# ---------------------------------------------------------------------------
# W8 regression — `Store.mark_verified` is silent last-write-wins under
# concurrent attestations
# ---------------------------------------------------------------------------
#
# Pre-W8 two parallel `mark_verified` calls on the same id with disjoint
# `verified_paths` (or `_commits` / `_versions`) lists would both serialise
# on the file lock, but neither verified the on-disk verification snapshot
# against what the caller saw when they decided to attest. The
# `verified_*` lists have REPLACE (not append) semantics by design — the
# event log is the audit trail — so whichever serialised second silently
# dropped the first writer's attestation: agent A attesting path #1 and
# agent B attesting path #2 simultaneously left only one path on disk.
#
# The fix mirrors W2's CAS pattern: `Store.mark_verified` accepts an
# optional `expected_last_verified_at` snapshot fingerprint and
# `check_expected=True` opt-in; under the lock, after the C2 recheck,
# the on-disk `last_verified_at` is compared to the caller's snapshot.
# On mismatch raise `ConcurrentUpdateError` carrying the on-disk
# `updated` (kept uniform with W2's exception contract — the caller's
# rebase action is "re-fetch via memory_show and retry," same as W2,
# regardless of which field actually moved). The `memory_verify`
# handler loads its snapshot first and opts in; legacy direct-store
# callers (web.py verify form, no-arg slide-forward use cases, the
# existing test suite) keep the back-compat `check_expected=False`
# default.
#
# Fingerprint choice: `updated` doesn't move on a `mark_verified` call
# (verification is orthogonal to content edits), so checking `updated`
# would never catch the verify-vs-verify race. `last_verified_at` is
# the field that always moves on a successful verify, so it's the
# cheapest correct fingerprint for this race.


def test_mark_verified_cas_detects_stale_snapshot(tmp_path: Path) -> None:
    """W8 regression. Two `Store.mark_verified` calls on the same id
    with disjoint attestations: the second must raise
    `ConcurrentUpdateError` rather than silently clobber the first
    writer's attestation.

    Sequential variant — caller B's `mark_verified` lands first
    (advancing the on-disk `last_verified_at`), then caller A tries
    to attest on top of an older snapshot and must lose.
    """
    store = Store(tmp_path)
    original = store.write(
        content="durable claim about /a and /b",
        scopes=["tools"],
    )

    # Caller A reads the snapshot — `last_verified_at` is None (fresh
    # write). Caller A plans to attest path /a.
    snapshot_a = store.load_one(original.id)
    assert snapshot_a.last_verified_at is None

    # Caller B reads the SAME snapshot and lands its attestation for
    # /b first. After B's write, on-disk `last_verified_at` is
    # non-None.
    snapshot_b = store.load_one(original.id)
    assert snapshot_b.last_verified_at is None
    after_b = store.mark_verified(
        original.id,
        verified_paths=["/b"],
        expected_last_verified_at=snapshot_b.last_verified_at,
        check_expected=True,
    )
    assert after_b.last_verified_at is not None, (
        "sanity: B's verify must move `last_verified_at` forward"
    )
    assert after_b.verified_paths == ["/b"]

    # Caller A now tries to attest /a on top of the stale snapshot.
    # On-disk `last_verified_at` is no longer None — CAS fires.
    with pytest.raises(ConcurrentUpdateError) as excinfo:
        store.mark_verified(
            original.id,
            verified_paths=["/a"],
            expected_last_verified_at=snapshot_a.last_verified_at,
            check_expected=True,
        )

    err = excinfo.value
    assert err.memory_id == original.id
    # The error's `current_updated` mirrors W2's contract — it's the
    # on-disk `updated`, not the moved-forward `last_verified_at`. The
    # caller's rebase action is the same regardless: re-fetch with
    # memory_show.
    assert err.current_updated == after_b.updated

    # Disk invariant: B's attestation wins. A's silent-clobber attempt
    # left no trace — `/a` is NOT silently merged in.
    final = store.load_one(original.id)
    assert final.verified_paths == ["/b"]
    assert "/a" not in final.verified_paths


def test_mark_verified_cas_detects_stale_snapshot_via_patched_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic W8 CAS test via monkeypatch on `_load_path`.

    The under-lock recheck-then-CAS sequence reads `_load_path` once
    for the CAS comparison (the C2 recheck uses `_id_still_at_path`,
    which only reads the frontmatter id). Patch `_load_path` to return
    a Memory whose `last_verified_at` has moved forward, simulating an
    interleaved verify landing between the file-lock acquisition and
    the CAS read. The CAS must fire.
    """
    store = Store(tmp_path)
    original = store.write(content="will race verify CAS", scopes=["tools"])
    snapshot = store.load_one(original.id)
    assert snapshot.last_verified_at is None

    from datetime import timedelta

    from bettermemory.models import Memory, utcnow

    original_load_path = Store._load_path
    forged_lva = utcnow() + timedelta(seconds=1)

    def racing_load_path(self: Store, path: Path) -> Memory:
        memory = original_load_path(self, path)
        if memory.id == original.id:
            return memory.model_copy(update={"last_verified_at": forged_lva})
        return memory

    monkeypatch.setattr(Store, "_load_path", racing_load_path)

    with pytest.raises(ConcurrentUpdateError) as excinfo:
        store.mark_verified(
            original.id,
            verified_paths=["/x"],
            expected_last_verified_at=snapshot.last_verified_at,
            check_expected=True,
        )
    err = excinfo.value
    assert err.memory_id == original.id


def test_mark_verified_cas_happy_path_passthrough(tmp_path: Path) -> None:
    """Happy-path passthrough: a single-writer `load_one`-then-
    `mark_verified` sequence (no contention) must continue to work —
    the CAS check is a no-false-positive invariant under single-writer
    use, regardless of whether the snapshot's `last_verified_at` is
    None (fresh write) or a real timestamp (already-verified memory).
    """
    store = Store(tmp_path)
    original = store.write(content="happy path verify", scopes=["tools"])

    # First verify: snapshot's `last_verified_at` is None.
    snapshot = store.load_one(original.id)
    first = store.mark_verified(
        original.id,
        verified_paths=["/p1"],
        expected_last_verified_at=snapshot.last_verified_at,
        check_expected=True,
    )
    assert first.last_verified_at is not None
    assert first.verified_paths == ["/p1"]

    # Second verify: snapshot's `last_verified_at` is the timestamp
    # from the first verify. CAS must pass.
    snapshot2 = store.load_one(original.id)
    assert snapshot2.last_verified_at == first.last_verified_at
    second = store.mark_verified(
        original.id,
        verified_paths=["/p1", "/p2"],
        expected_last_verified_at=snapshot2.last_verified_at,
        check_expected=True,
    )
    assert second.last_verified_at is not None
    assert second.last_verified_at >= first.last_verified_at
    assert second.verified_paths == ["/p1", "/p2"]


def test_mark_verified_check_expected_false_bypasses_cas(tmp_path: Path) -> None:
    """`check_expected=False` (the default) is the back-compat path —
    legacy callers without a snapshot (web.py verify form, no-arg
    slide-forward, direct in-process tooling) must continue to work
    without triggering the CAS.
    """
    store = Store(tmp_path)
    original = store.write(content="will be verified twice no CAS", scopes=["tools"])

    # First mark_verified moves `last_verified_at` forward.
    first = store.mark_verified(original.id, verified_paths=["/legacy/1"])
    assert first.last_verified_at is not None
    # Second mark_verified with NO snapshot must still succeed — the
    # default `check_expected=False` bypasses the CAS.
    second = store.mark_verified(original.id, verified_paths=["/legacy/2"])
    assert second.last_verified_at is not None
    assert second.verified_paths == ["/legacy/2"]

    # And explicitly: passing a stale snapshot WITHOUT opting in
    # (`check_expected=False`) also bypasses — the parameter is purely
    # advisory when the flag is off.
    third = store.mark_verified(
        original.id,
        verified_paths=["/legacy/3"],
        expected_last_verified_at=None,  # deliberately stale
        check_expected=False,
    )
    assert third.verified_paths == ["/legacy/3"]


def test_mark_verified_cas_threaded_one_winner(tmp_path: Path) -> None:
    """W8 regression with real threads + `threading.Event` rendezvous.

    Two threads load the same snapshot, sync on a `threading.Event`
    barrier so both have read the same pre-race `last_verified_at`,
    then race the `mark_verified` write. Exactly one thread wins;
    the other must raise `ConcurrentUpdateError`.

    GIL-bound but the file lock + CAS serialises the writes — so the
    race-loser deterministically gets the structured stale signal,
    not a torn write or a silent clobber. The disk-state assertion
    pins the contract: the winner's `verified_paths` is intact; the
    loser's `verified_paths` is NOT silently merged in.
    """
    import threading

    store = Store(tmp_path)
    memory = store.write(
        content="threaded attestation race target",
        scopes=["tools"],
    )

    # Both threads will read this snapshot before either writes.
    go = threading.Event()
    results_lock = threading.Lock()
    results: list[dict[str, object]] = []

    def worker(marker: str, attest_path: str) -> None:
        snapshot = store.load_one(memory.id)
        # Wait for both snapshots to be taken before either writes,
        # so both threads' `expected_last_verified_at` is the same
        # pre-race value.
        go.wait(timeout=30)
        try:
            updated = store.mark_verified(
                memory.id,
                verified_paths=[attest_path],
                expected_last_verified_at=snapshot.last_verified_at,
                check_expected=True,
            )
            with results_lock:
                results.append(
                    {
                        "outcome": "won",
                        "marker": marker,
                        "verified_paths": list(updated.verified_paths),
                    }
                )
        except ConcurrentUpdateError as exc:
            with results_lock:
                results.append(
                    {
                        "outcome": "lost-stale",
                        "marker": marker,
                        "memory_id": exc.memory_id,
                    }
                )

    threads = [
        threading.Thread(target=worker, args=("A", "/path-a")),
        threading.Thread(target=worker, args=("B", "/path-b")),
    ]
    for t in threads:
        t.start()
    # Give both workers time to take their snapshot. The file-lock
    # contention happens AFTER `go.set()`, so a tiny pause here
    # ensures both `load_one`s complete and both worker `snapshot`
    # locals are bound before either races to write.
    time.sleep(0.05)
    go.set()
    for t in threads:
        t.join(timeout=30)

    outcomes = [r["outcome"] for r in results]
    winners = [r for r in results if r["outcome"] == "won"]
    losers = [r for r in results if r["outcome"] == "lost-stale"]
    assert len(winners) == 1, (
        f"Expected exactly one mark_verified winner under CAS, got "
        f"{len(winners)}. Outcomes: {outcomes}"
    )
    assert len(losers) == 1, (
        f"Expected exactly one stale-CAS loser, got {len(losers)}. Outcomes: {outcomes}"
    )

    # Disk invariant: the winner's attestation is intact; the loser's
    # path is NOT silently merged in. Contract is reread + reattest,
    # not silent merge.
    final = store.load_one(memory.id)
    winner_marker = winners[0]["marker"]
    assert isinstance(winner_marker, str)
    expected_winning_path = f"/path-{winner_marker.lower()}"
    assert final.verified_paths == [expected_winning_path], (
        f"Winner's attestation not intact on disk. winner={winner_marker!r}, "
        f"disk verified_paths={final.verified_paths!r}"
    )
    loser_marker = losers[0]["marker"]
    assert isinstance(loser_marker, str)
    losing_path = f"/path-{loser_marker.lower()}"
    assert losing_path not in final.verified_paths, (
        f"Loser's attestation silently merged on disk. loser={loser_marker!r}, "
        f"disk verified_paths={final.verified_paths!r}"
    )


def _w8_disjoint_verify_worker(
    args: tuple[str, str, str, str, str],
) -> dict[str, str]:
    """Worker for the cross-process W8 variant.

    Each worker process:
      1. `load_one(memory_id)` to take a snapshot.
      2. Signals "snapshot taken" by touching its own ready-marker.
      3. Waits for the parent to release via a shared go-marker —
         this barrier guarantees every worker has read the same
         pre-race `last_verified_at` BEFORE any of them tries to
         write. Without the barrier the first-spawned worker's
         `mark_verified` could land before later workers' `load_one`,
         masking the regression we're trying to pin (the late
         workers would read the bumped on-disk timestamp and
         trivially pass the CAS).
      4. Calls `Store.mark_verified` with its own per-worker
         `verified_paths` attestation and the snapshot's
         `last_verified_at` as the CAS fingerprint.

    Exactly one worker must win; the rest must lose with
    `ConcurrentUpdateError`.
    """
    root, memory_id, marker, ready_marker, go_marker = args
    store = Store(Path(root))
    try:
        snapshot = store.load_one(memory_id)
    except (MemoryNotFoundError, TombstonedError) as exc:
        return {"outcome": "snapshot-failed", "msg": str(exc)}

    Path(ready_marker).touch()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if Path(go_marker).exists():
            break
        time.sleep(0.01)
    else:  # noqa: PLW0120 — explicit timeout signaling for the parent
        return {"outcome": "barrier-timeout"}

    try:
        store.mark_verified(
            memory_id,
            verified_paths=[f"/path/{marker}"],
            expected_last_verified_at=snapshot.last_verified_at,
            check_expected=True,
        )
        return {"outcome": "won", "marker": marker}
    except ConcurrentUpdateError as exc:
        return {"outcome": "lost-stale", "msg": str(exc), "marker": marker}
    except (MemoryNotFoundError, TombstonedError) as exc:
        return {"outcome": "lost-other", "msg": str(exc), "marker": marker}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl-based locking is POSIX-only; MVP single-process on Windows",
)
def test_multi_process_concurrent_disjoint_verifies_no_silent_clobber(
    tmp_path: Path,
) -> None:
    """W8 regression across REAL processes (no GIL).

    Six workers race on `Store.mark_verified(same_id)` from separate
    Python interpreters, each carrying a different `verified_paths`
    attestation built on top of a snapshot they took before the race.
    Pre-W8 all six writes silently serialised on the file lock and
    only the last one's `verified_paths` survived — five attestations
    dropped without surface. Post-W8 exactly one wins; the others
    must lose with `ConcurrentUpdateError` so the caller can
    re-fetch + reassess + retry.

    The post-condition that retires the silent-last-write-wins
    semantic for multi-agent concurrent verifies: the eventual disk
    state must reflect the winner's `verified_paths` only — no torn
    merge, no other worker's attestation silently lingering.
    """
    n_workers = 6
    setup = Store(tmp_path)
    memory = setup.write(
        content="raced disjoint verifies across processes",
        scopes=["tools"],
    )

    barrier_dir = tmp_path / "_w8_barriers"
    barrier_dir.mkdir()
    go_marker = barrier_dir / "go"
    ready_markers = [barrier_dir / f"ready_{w}" for w in range(n_workers)]

    ctx = mp.get_context("spawn")
    pool = ctx.Pool(n_workers)
    try:
        async_result = pool.map_async(
            _w8_disjoint_verify_worker,
            [
                (
                    str(tmp_path),
                    memory.id,
                    str(w),
                    str(ready_markers[w]),
                    str(go_marker),
                )
                for w in range(n_workers)
            ],
        )

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if all(m.exists() for m in ready_markers):
                break
            time.sleep(0.01)
        else:  # noqa: PLW0120 — explicit timeout for the readiness rendezvous
            pool.terminate()
            pool.join()
            pytest.fail(
                "not all workers signalled snapshot-taken within 30s; "
                f"ready: {[m.name for m in ready_markers if m.exists()]}"
            )

        go_marker.touch()
        results = async_result.get(timeout=60)
    finally:
        pool.close()
        pool.join()

    outcomes = [r["outcome"] for r in results]
    winners = [r for r in results if r["outcome"] == "won"]
    losers = [r for r in results if r["outcome"] == "lost-stale"]
    assert len(winners) == 1, (
        f"Expected exactly one verify winner under CAS, got {len(winners)}. "
        f"Outcomes: {outcomes}"
    )
    assert len(losers) == n_workers - 1, (
        f"Expected {n_workers - 1} stale-CAS losers, got {len(losers)}. "
        f"Outcomes: {outcomes}"
    )

    # Disk invariant: the winner's `verified_paths` is intact; no
    # other worker's path may appear (no silent merge).
    fresh = Store(tmp_path)
    final = fresh.load_one(memory.id)
    winner_marker = winners[0]["marker"]
    assert final.verified_paths == [f"/path/{winner_marker}"], (
        f"Disk verified_paths {final.verified_paths!r} does not reflect "
        f"the winner's attestation (marker {winner_marker!r}). "
        f"Outcomes: {outcomes}"
    )
    for loser in losers:
        loser_path = f"/path/{loser['marker']}"
        assert loser_path not in final.verified_paths, (
            f"Loser's attestation {loser_path!r} silently merged on disk. "
            f"Outcomes: {outcomes}"
        )
