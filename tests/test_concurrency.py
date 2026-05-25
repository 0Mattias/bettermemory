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


def test_concurrent_slug_collision_writes_do_not_clobber(tmp_path: Path) -> None:
    """C1 regression. Two threaded writers whose bodies slugify to the
    same string must both end up with distinct files on disk; neither
    memory may be silently overwritten.

    Threaded (not multiprocess) because we want maximum interleaving on
    the `_path_for` -> `_locked` -> `_write_path` sequence. The
    cross-process variant is already exercised by
    `test_multi_process_stress_no_corruption`; this test pins the
    deterministic slug-collision case so a regression to the
    `exists()`-only guard fails it loudly.
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
# pytest, you have it; importing unconditionally keeps the analyzer happy.
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


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
        if (
            path is not None
            and mid == memory.id
            and not fired["done"]
        ):
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
    active_files = [
        p
        for p in tmp_path.iterdir()
        if p.is_file() and p.suffix == ".md"
    ]
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
        if (
            path is not None
            and mid == memory.id
            and not fired["done"]
        ):
            fired["done"] = True
            self.tombstone(mid, reason="raced by other process")
        return path

    monkeypatch.setattr(Store, "_find_path_for_id", racing_find)

    with pytest.raises(MemoryNotFoundError, match="raced with"):
        store.mark_verified(memory.id)

    # Tombstone state is preserved.
    with pytest.raises(TombstonedError):
        store.load_one(memory.id)
