"""Storage-layer tests for `EpisodeStore`.

Handler-level tests live in `tests/test_server.py` alongside the other
MCP tool tests. The cuts here pin the on-disk shape and prune semantics
so a future refactor can't silently break the format or the TTL
contract.
"""

from __future__ import annotations

import stat
import sys
import time
from pathlib import Path

import pytest

from bettermemory.episodes import EpisodeStore
from bettermemory.origin import Origin


@pytest.fixture
def episode_store(tmp_path: Path) -> EpisodeStore:
    return EpisodeStore(tmp_path)


def test_write_creates_session_dir_lazily(episode_store: EpisodeStore) -> None:
    """The `episodes/` subtree only appears on first write — a fresh
    install with no episodes leaves no empty dir behind."""
    assert not episode_store.episodes_dir.exists()
    episode_store.write(session_id="sess_aaaa1111", body="hello")
    assert episode_store.episodes_dir.exists()
    assert (episode_store.episodes_dir / "sess_aaaa1111").is_dir()


def test_write_persists_takeaway_and_scopes(episode_store: EpisodeStore) -> None:
    ep = episode_store.write(
        session_id="sess_aaaa1111",
        body="iteration body",
        takeaway="one-line summary",
        scopes=["projects:foo", "tools"],
    )
    loaded = episode_store.list_by_session("sess_aaaa1111")
    assert len(loaded) == 1
    assert loaded[0].id == ep.id
    assert loaded[0].takeaway == "one-line summary"
    assert loaded[0].scopes == ["projects:foo", "tools"]


def test_write_persists_origin(episode_store: EpisodeStore) -> None:
    origin = Origin(
        cwd="/tmp/work",
        repo="https://github.com/0Mattias/example",
        branch="main",
        worktree_root="/tmp/work",
    )
    ep = episode_store.write(
        session_id="sess_aaaa1111",
        body="origin test",
        origin=origin,
    )
    loaded = episode_store.list_by_session("sess_aaaa1111")
    assert loaded[0].origin is not None
    assert loaded[0].origin.repo == "https://github.com/0Mattias/example"
    assert loaded[0].id == ep.id


def test_list_by_session_sorts_oldest_first(episode_store: EpisodeStore) -> None:
    """ULIDs sort lexically by creation timestamp; list_by_session
    surfaces them oldest first so a handoff caller can take the most
    recent N from the tail."""
    a = episode_store.write(session_id="sess_aaaa1111", body="first")
    time.sleep(0.005)  # ms-resolution ULID needs a beat to bump
    b = episode_store.write(session_id="sess_aaaa1111", body="second")
    eps = episode_store.list_by_session("sess_aaaa1111")
    assert [e.id for e in eps] == [a.id, b.id]


def test_rejects_traversal_in_session_id(episode_store: EpisodeStore) -> None:
    """A session_id containing `/` or `..` would let a hostile caller
    escape the episodes subtree. Reject at the storage boundary."""
    with pytest.raises(ValueError):
        episode_store.write(session_id="../etc/passwd", body="x")
    with pytest.raises(ValueError):
        episode_store.write(session_id="a/b", body="x")


def test_prune_drops_sessions_past_ttl(episode_store: EpisodeStore) -> None:
    """Sessions whose newest episode mtime is past the TTL get rmtree'd."""
    episode_store.write(session_id="sess_old", body="ancient")
    episode_store.write(session_id="sess_new", body="fresh")

    # Backdate the "old" session's files past the TTL.
    old_dir = episode_store.episodes_dir / "sess_old"
    for f in old_dir.iterdir():
        past = time.time() - (40 * 24 * 60 * 60)  # 40 days ago
        import os as _os

        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_old" in pruned
    assert "sess_new" not in pruned
    assert not (episode_store.episodes_dir / "sess_old").exists()
    assert (episode_store.episodes_dir / "sess_new").exists()


def test_prune_respects_keep_session_id(episode_store: EpisodeStore) -> None:
    """The active session's dir is exempt from pruning even if its
    newest mtime is past the TTL — a session that paused for >30d
    shouldn't lose its own scratch when it resumes writing."""
    episode_store.write(session_id="sess_active", body="paused-then-resumed")
    active_dir = episode_store.episodes_dir / "sess_active"
    for f in active_dir.iterdir():
        past = time.time() - (40 * 24 * 60 * 60)
        import os as _os

        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(
        ttl_days=30, keep_session_id="sess_active"
    )
    assert "sess_active" not in pruned
    assert (episode_store.episodes_dir / "sess_active").exists()


def test_prune_zero_ttl_is_noop(episode_store: EpisodeStore) -> None:
    """`ttl_days <= 0` disables the prune entirely. Used by callers
    that want to manage retention explicitly via the CLI rather than
    on every write."""
    episode_store.write(session_id="sess_aaaa1111", body="any")
    pruned = episode_store.prune_old_sessions(ttl_days=0)
    assert pruned == []
    assert (episode_store.episodes_dir / "sess_aaaa1111").exists()


def test_prune_locked_recheck_skips_race_winner(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-MCP race: process A's `prune_old_sessions` does an
    unlocked stat, decides a session dir is stale, then acquires the
    per-session flock. Between the stat and the flock-acquire,
    process B's `episode_write` slipped in and rename'd a fresh
    `<ulid>.md` into the dir — mtime is now well past the cutoff
    and the dir is logically live again. The recheck under the lock
    must see the fresh mtime and skip the `shutil.rmtree`, otherwise
    A wipes B's just-committed episode.

    Pin by stubbing `_newest_mtime_in_dir` to return stale on the
    first call (the unlocked walk) and fresh on the second (the
    locked recheck), reproducing the exact race window
    deterministically without threading."""
    import bettermemory.episodes as episodes_mod
    import os as _os

    episode_store.write(session_id="sess_raced", body="real file")
    raced_dir = episode_store.episodes_dir / "sess_raced"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in raced_dir.iterdir():
        _os.utime(f, (past, past))

    cutoff_epoch = time.time() - 30 * 24 * 60 * 60
    real_newest = episodes_mod._newest_mtime_in_dir
    calls: list[Path] = []

    def staggered_newest(path: Path) -> float | None:
        calls.append(path)
        if path == raced_dir:
            # First call (unlocked walk): return stale so prune
            # decides to delete. Second call (locked recheck):
            # return fresh so prune skips. Other paths get the
            # real implementation untouched.
            if calls.count(raced_dir) == 1:
                return cutoff_epoch - 1.0
            return time.time()
        return real_newest(path)

    monkeypatch.setattr(episodes_mod, "_newest_mtime_in_dir", staggered_newest)

    pruned = episode_store.prune_old_sessions(ttl_days=30)

    assert "sess_raced" not in pruned, (
        f"prune deleted a session whose locked-recheck saw a fresh mtime: {pruned}"
    )
    assert raced_dir.exists(), (
        "session_dir was rmtree'd despite the locked-recheck seeing fresh mtime"
    )
    # Both walks must have happened — the unlocked stat and the
    # locked recheck. A single call would mean the recheck was
    # skipped and the race window is still open.
    assert calls.count(raced_dir) == 2, (
        f"expected unlocked stat + locked recheck (2 calls on raced_dir), "
        f"saw {calls.count(raced_dir)}"
    )


def test_prune_blocks_while_writer_holds_flock(
    episode_store: EpisodeStore,
) -> None:
    """Direct pin on the lock-acquire semantics: while the writer
    side holds the per-session flock, a concurrent `prune_old_sessions`
    must BLOCK on the flock-acquire (not race past it and rmtree
    the session dir). Use a thread to hold the writer's flock,
    launch the prune in a second thread, and assert the prune
    cannot complete until the writer releases."""
    import os as _os
    import threading
    from bettermemory._fsutil import flock_excl

    episode_store.write(session_id="sess_held", body="seed")
    held_dir = episode_store.episodes_dir / "sess_held"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in held_dir.iterdir():
        _os.utime(f, (past, past))

    lock_anchor = episode_store.episodes_dir / ".session-sess_held"
    writer_holding = threading.Event()
    writer_release = threading.Event()

    def hold_writer_lock() -> None:
        with flock_excl(lock_anchor):
            writer_holding.set()
            writer_release.wait(timeout=5.0)

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    try:
        writer_holding.wait(timeout=5.0)
        assert writer_holding.is_set()

        prune_done = threading.Event()
        prune_result: list[list[str]] = []

        def background_prune() -> None:
            prune_result.append(episode_store.prune_old_sessions(ttl_days=30))
            prune_done.set()

        pt = threading.Thread(target=background_prune)
        pt.start()
        # Give the prune a generous window to (incorrectly) race
        # through and rmtree the held dir. If it completes here,
        # the flock isn't serialising — that's the bug we're
        # protecting against.
        time.sleep(0.1)
        assert not prune_done.is_set(), (
            "prune raced through the per-session flock — writer is not "
            "serialising the rmtree window"
        )
        assert held_dir.exists(), "prune rmtree'd the dir before lock release"

        writer_release.set()
        pt.join(timeout=5.0)
        assert prune_done.is_set()
        # After the writer released, prune acquires the lock, rechecks
        # mtime — which is still past cutoff (we backdated the seed
        # file, no writer bumped it) — and proceeds with the rmtree.
        assert "sess_held" in prune_result[0]
        assert not held_dir.exists()
    finally:
        writer_release.set()
        holder.join(timeout=5.0)


def test_prune_still_deletes_truly_stale_dirs(
    episode_store: EpisodeStore,
) -> None:
    """Regression pin: the flock + recheck logic must NOT break the
    base case — a stale session dir with no concurrent writer still
    gets deleted on the next prune pass. Without this pin a future
    refactor could leave the recheck always-truthy and silently turn
    `prune_old_sessions` into a no-op."""
    import os as _os

    episode_store.write(session_id="sess_truly_stale", body="ancient")
    stale_dir = episode_store.episodes_dir / "sess_truly_stale"
    past = time.time() - (40 * 24 * 60 * 60)
    for f in stale_dir.iterdir():
        _os.utime(f, (past, past))

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_truly_stale" in pruned
    assert not stale_dir.exists()
    # The 0-byte lockfile is INTENTIONALLY left behind after rmtree —
    # unlinking would open a flock-identity-per-inode race window where
    # a peer process holding the lock on the old inode coexists with a
    # fresh acquirer that O_CREAT'd a new inode and believes itself the
    # holder. See the 2.6.3 audit note in `_fsutil.flock_excl`. Orphan
    # cost is 0 bytes per pruned session — negligible vs. correctness.
    lock_path = episode_store.episodes_dir / ".session-sess_truly_stale.lock"
    assert lock_path.exists(), (
        "lockfile must persist after rmtree to keep flock identity stable"
    )


def test_prune_treats_vanished_dir_as_success(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peer-prune race: another bettermemory process pruning the
    same store rmtree'd the session dir between OUR unlocked mtime
    stat and OUR flock acquisition. `prune_old_sessions` must treat
    the resulting `FileNotFoundError` from `shutil.rmtree` as
    "already gone, success" and append the session to `pruned`, not
    swallow it as a generic OSError and lose the bookkeeping."""
    import os as _os
    import shutil as _shutil

    episode_store.write(session_id="sess_peer_raced", body="will vanish")
    target = episode_store.episodes_dir / "sess_peer_raced"
    past = time.time() - (40 * 24 * 60 * 60)

    for f in target.iterdir():
        _os.utime(f, (past, past))

    # Patch `shutil.rmtree` (the binding the episodes module imported
    # via `import shutil`) to wipe the dir AND raise FileNotFoundError
    # — simulating a peer prune that won the race.
    real_rmtree = _shutil.rmtree

    def racing_rmtree(path: Path | str) -> None:
        real_rmtree(path)
        raise FileNotFoundError(2, "No such file or directory", str(path))

    monkeypatch.setattr(_shutil, "rmtree", racing_rmtree)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_peer_raced" in pruned, (
        "prune should record a peer-raced rmtree as a successful prune"
    )
    assert not target.exists()


def test_writer_progresses_while_prune_waits_on_lock(
    episode_store: EpisodeStore,
) -> None:
    """Reverse-direction race: a `prune_old_sessions` call holds the
    per-session flock (mtime-recheck phase) while the active session
    writes a new episode. The writer must block on the flock, then
    proceed normally when prune releases — NOT fail, deadlock, or
    silently skip the write.

    Pins that the lock is released cleanly by prune even on the
    skip-delete branch (the locked recheck sees fresh mtime), so
    the writer can immediately complete."""
    import threading
    from bettermemory._fsutil import flock_excl

    episode_store.write(session_id="sess_active", body="seed")

    lock_anchor = episode_store.episodes_dir / ".session-sess_active"
    prune_holding = threading.Event()
    prune_release = threading.Event()

    def prune_holds_lock() -> None:
        """Hold the per-session flock for a beat to simulate the
        prune sitting inside its locked recheck → rmtree section."""
        with flock_excl(lock_anchor):
            prune_holding.set()
            prune_release.wait(timeout=5.0)

    t = threading.Thread(target=prune_holds_lock)
    t.start()
    try:
        prune_holding.wait(timeout=5.0)
        assert prune_holding.is_set()

        # Writer must block on the flock. Launch the write in a
        # background thread so we can assert it's blocked, then
        # release the lock and assert the write completes.
        writer_done = threading.Event()
        write_error: list[BaseException] = []

        def background_write() -> None:
            try:
                episode_store.write(session_id="sess_active", body="post-lock episode")
            except BaseException as exc:  # noqa: BLE001
                write_error.append(exc)
            finally:
                writer_done.set()

        wt = threading.Thread(target=background_write)
        wt.start()
        # The writer should NOT complete while prune holds the lock.
        # Give it a brief window to (incorrectly) sneak through.
        time.sleep(0.1)
        assert not writer_done.is_set(), (
            "writer raced through the per-session flock — prune lock is "
            "not serialising writes"
        )

        prune_release.set()
        wt.join(timeout=5.0)
        assert writer_done.is_set(), "writer never completed after lock release"
        assert not write_error, f"writer raised: {write_error[0]!r}"

        eps = episode_store.list_by_session("sess_active")
        bodies = [e.body.strip() for e in eps]
        assert "post-lock episode" in bodies, (
            "writer's episode did not land after the prune lock released"
        )
    finally:
        prune_release.set()
        t.join(timeout=5.0)


def test_write_is_atomic_and_durable(
    episode_store: EpisodeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write path must (a) leave no `.tmp` artifacts behind,
    (b) chmod the final file to 0o600 on POSIX, and (c) call both
    durability primitives (`fsync_file` on the open fd, `fsync_dir` on
    the parent). Pre-fix `_write_path` used `Path.write_text` +
    `os.replace` with no fsyncs, so power-loss between rename and
    kernel flush could leave a zero-byte `<ulid>.md` at the target."""
    fsync_file_calls: list[int] = []
    fsync_dir_calls: list[Path] = []

    import bettermemory.episodes as episodes_mod

    def spy_fsync_file(fd: int) -> None:
        fsync_file_calls.append(fd)

    def spy_fsync_dir(p: Path) -> None:
        fsync_dir_calls.append(p)

    monkeypatch.setattr(episodes_mod, "fsync_file", spy_fsync_file)
    monkeypatch.setattr(episodes_mod, "fsync_dir", spy_fsync_dir)

    ep = episode_store.write(session_id="sess_aaaa1111", body="durable")

    session_dir = episode_store.episodes_dir / "sess_aaaa1111"
    target = session_dir / f"{ep.id}.md"
    assert target.is_file()

    # No `.tmp` artifacts left behind after a successful write.
    stragglers = [
        p for p in session_dir.iterdir() if p.suffix == ".tmp" or ".tmp" in p.name
    ]
    assert stragglers == [], f"unexpected tmp artifacts: {stragglers}"

    # Both fsync primitives were invoked — fsync_file on the tmp fd
    # before rename, fsync_dir on the session dir after rename.
    assert len(fsync_file_calls) == 1
    assert fsync_dir_calls == [session_dir]

    # 0o600 mode on POSIX. Windows has no mode bits, so skip there.
    if sys.platform != "win32":
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_excluded_from_memory_store_iteration(tmp_path: Path) -> None:
    """Episodes must not appear in `Store.load_all` — episodes live in
    a sibling subdirectory (`episodes/`), so the memory store's
    `_iter_active_paths` (which uses `iterdir` on the root) should
    skip directory entries naturally. This pin catches a future
    refactor that accidentally recurses or globs."""
    from bettermemory.store import Store

    store = Store(tmp_path)
    ep_store = EpisodeStore(tmp_path)
    ep_store.write(session_id="sess_aaaa1111", body="not a memory")

    memories = store.load_all()
    assert memories == []


def test_prune_empty_dir_holds_flock_while_writer_runs(
    episode_store: EpisodeStore,
) -> None:
    """Empty-dir branch of `prune_old_sessions` must respect the same
    per-session flock the past-cutoff branch does. Symmetric to
    `test_writer_progresses_while_prune_waits_on_lock` and
    `test_prune_blocks_while_writer_holds_flock` but for the empty-dir
    branch: a writer holds the per-session flock with an empty
    session_dir in place; the concurrent prune must BLOCK on the
    flock-acquire (not race past it and rmdir the dir that the writer
    just `mkdir`'d, about to land a tempfile into).
    """
    import threading
    from bettermemory._fsutil import flock_excl

    # Set up an empty session_dir without writing any episode — this
    # is the exact shape the bug targets (writer has mkdir'd but not
    # yet rename'd its tempfile into place).
    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    empty_dir = episode_store.episodes_dir / "sess_empty"
    empty_dir.mkdir(mode=0o700)

    lock_anchor = episode_store.episodes_dir / ".session-sess_empty"
    writer_holding = threading.Event()
    writer_release = threading.Event()

    def hold_writer_lock() -> None:
        with flock_excl(lock_anchor):
            writer_holding.set()
            writer_release.wait(timeout=5.0)

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    try:
        writer_holding.wait(timeout=5.0)
        assert writer_holding.is_set()

        prune_done = threading.Event()
        prune_result: list[list[str]] = []

        def background_prune() -> None:
            prune_result.append(episode_store.prune_old_sessions(ttl_days=30))
            prune_done.set()

        pt = threading.Thread(target=background_prune)
        pt.start()
        # Give the prune a generous window to (incorrectly) race
        # through and rmdir the held dir. If it completes here, the
        # flock isn't serialising the empty-dir branch — that's the
        # bug we're protecting against.
        time.sleep(0.1)
        assert not prune_done.is_set(), (
            "prune raced through the per-session flock on the empty-dir "
            "branch — writer's mkdir+tempfile window is not serialised"
        )
        assert empty_dir.exists(), "prune rmdir'd the empty dir before lock release"

        writer_release.set()
        pt.join(timeout=5.0)
        assert prune_done.is_set()
        # After the writer released, the prune acquires the lock, the
        # recheck still sees an empty dir (we never landed a file),
        # and the rmdir succeeds.
        assert "sess_empty" in prune_result[0]
        assert not empty_dir.exists()
    finally:
        writer_release.set()
        holder.join(timeout=5.0)


def test_prune_empty_dir_recheck_skips_when_writer_landed_after_unlocked_walk(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-MCP race on the empty-dir branch: process A's
    `prune_old_sessions` does an unlocked stat on an empty session_dir,
    decides to rmdir. Between the stat and the flock-acquire, process
    B's `episode_write` slipped in, completed its mkdir + tempfile
    rename — the dir is no longer empty. The locked recheck must see
    the fresh mtime and SKIP the rmdir, otherwise A wipes a directory
    that's logically live.

    Pin by stubbing `_newest_mtime_in_dir` to return None on the first
    call (the unlocked walk) and a fresh mtime on the second call
    (the locked recheck), reproducing the exact race deterministically
    without threading."""
    import bettermemory.episodes as episodes_mod

    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    raced_dir = episode_store.episodes_dir / "sess_emptied_then_filled"
    raced_dir.mkdir(mode=0o700)

    real_newest = episodes_mod._newest_mtime_in_dir
    calls: list[Path] = []

    def staggered_newest(path: Path) -> float | None:
        calls.append(path)
        if path == raced_dir:
            # First call (unlocked walk): return None so prune takes
            # the empty-dir branch. Second call (locked recheck):
            # return a fresh mtime so prune skips the rmdir. Other
            # paths get the real implementation untouched.
            if calls.count(raced_dir) == 1:
                return None
            return time.time()
        return real_newest(path)

    monkeypatch.setattr(episodes_mod, "_newest_mtime_in_dir", staggered_newest)

    pruned = episode_store.prune_old_sessions(ttl_days=30)

    assert "sess_emptied_then_filled" not in pruned, (
        f"prune deleted an empty dir whose locked-recheck saw a fresh "
        f"mtime (writer landed during the race window): {pruned}"
    )
    assert raced_dir.exists(), (
        "session_dir was rmdir'd despite the locked-recheck seeing fresh mtime"
    )
    # Both walks must have happened — the unlocked stat and the
    # locked recheck. A single call would mean the recheck was
    # skipped and the race window is still open.
    assert calls.count(raced_dir) == 2, (
        f"expected unlocked stat + locked recheck (2 calls on raced_dir), "
        f"saw {calls.count(raced_dir)}"
    )


def test_prune_empty_dir_still_deletes_truly_empty_session(
    episode_store: EpisodeStore,
) -> None:
    """Regression pin: the flock + recheck logic on the empty-dir
    branch must NOT break the base case — a session_dir with no
    files and no concurrent writer still gets deleted on the next
    prune pass. Without this pin a future refactor could leave the
    locked recheck always-truthy and silently turn the empty-dir
    branch into a no-op."""
    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    empty_dir = episode_store.episodes_dir / "sess_truly_empty"
    empty_dir.mkdir(mode=0o700)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_truly_empty" in pruned
    assert not empty_dir.exists()


def test_prune_empty_dir_treats_vanished_dir_as_success(
    episode_store: EpisodeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Peer-prune race on the empty-dir branch: another bettermemory
    process pruning the same store rmdir'd the session dir between
    OUR unlocked mtime stat and OUR flock acquisition. The empty-dir
    branch must treat the resulting `FileNotFoundError` from `rmdir`
    as "already gone, success" and append the session to `pruned`,
    not swallow it as a generic OSError and lose the bookkeeping.

    Patch `Path.rmdir` to raise FileNotFoundError, simulating a peer
    prune that won the race."""
    episode_store.episodes_dir.mkdir(mode=0o700, exist_ok=True)
    target = episode_store.episodes_dir / "sess_peer_raced_empty"
    target.mkdir(mode=0o700)

    real_rmdir = Path.rmdir
    raised = {"done": False}

    def racing_rmdir(self: Path) -> None:
        # Actually remove the dir (so the post-condition holds), then
        # raise FileNotFoundError on the first call against our target
        # to simulate a peer prune that won. Other rmdir callers
        # (none in this test, but defensive) get the real behaviour.
        if self == target and not raised["done"]:
            real_rmdir(self)
            raised["done"] = True
            raise FileNotFoundError(2, "No such file or directory", str(self))
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", racing_rmdir)

    pruned = episode_store.prune_old_sessions(ttl_days=30)
    assert "sess_peer_raced_empty" in pruned, (
        "empty-dir branch should record a peer-raced rmdir as a successful prune"
    )
    assert not target.exists()
