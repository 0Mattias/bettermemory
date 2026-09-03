"""Regression tests for the read-modify-write lock discipline in
`store.py`.

The race fixed in these tests: prior to the fix, `mark_verified`,
`tombstone`, `restore`, and `rename_scope` each read a file off disk
*before* acquiring the file lock, then wrote *inside* the lock. A
concurrent writer landing between the read and the lock would have
its update silently clobbered when the original caller wrote back
the stale body.

With stdio MCP as the only consumer the race was hidden behind a
single-writer assumption, but 2.0 shipped writers that run alongside
the stdio server and mutate the same files — the sync wrapper
(sync.py) still does, as does any second server on the same store
(the since-removed web UI was the original second writer). The fix moves the read
inside the lock; these tests assert that invariant structurally by
tracing `_load_path` / `_locked` / `frontmatter.load` events.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

import bettermemory._frontmatter as _fm
from bettermemory import store as store_module
from bettermemory.store import Store


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install instrumentation that records `_load_path` calls,
    `frontmatter.load` calls (via the alias on `store_module`), and
    `_locked` enter/exit events to a list, then return that list.

    Each event is `"{verb}:{path.name}"` so the assertions can match
    per file when a single operation locks more than one path.
    """
    events: list[str] = []
    original_load_path = Store._load_path
    original_locked = store_module._locked
    original_fm_load = _fm.load

    def traced_load_path(self: Store, path: Path) -> Any:
        events.append(f"load:{path.name}")
        return original_load_path(self, path)

    def traced_fm_load(path: Path) -> Any:
        events.append(f"fm_load:{path.name}")
        return original_fm_load(path)

    @contextlib.contextmanager
    def traced_locked(path: Path) -> Generator[None, None, None]:
        events.append(f"lock_enter:{path.name}")
        with original_locked(path):
            yield
        events.append(f"lock_exit:{path.name}")

    monkeypatch.setattr(Store, "_load_path", traced_load_path)
    monkeypatch.setattr(store_module, "_locked", traced_locked)
    # `store.py` does `from . import _frontmatter as frontmatter`; that
    # alias and `_fm` are the same module object, so patching `_fm.load`
    # routes the tombstone/restore reads through the trace. (Patching
    # via `store_module.frontmatter` works at runtime but trips mypy's
    # strict re-export rule.)
    monkeypatch.setattr(_fm, "load", traced_fm_load)
    return events


def _assert_one_event_inside_lock(
    events: list[str], event: str, path_name: str
) -> None:
    """Assert at least one `{event}:{path_name}` sits between a
    matching `lock_enter` and `lock_exit` for the same path. This is
    the structural invariant the fix guarantees; the pre-fix code
    emitted the read event *before* any lock_enter for the same path.
    """
    target = f"{event}:{path_name}"
    in_lock = False
    found_inside = False
    for e in events:
        if e == f"lock_enter:{path_name}":
            in_lock = True
        elif e == f"lock_exit:{path_name}":
            in_lock = False
        elif e == target and in_lock:
            found_inside = True
            break
    assert found_inside, (
        f"no {target!r} event inside a _locked({path_name!r}) block — "
        f"the read happened outside the lock, leaving a TOCTOU window; "
        f"events={events}"
    )


# ---------------------------------------------------------------------------
# mark_verified
# ---------------------------------------------------------------------------


def test_mark_verified_loads_inside_lock(store: Store, traced: list[str]) -> None:
    memory = store.write(content="body", scopes=["tools"])
    path = next(p for p in store._iter_active_paths() if p.is_file())
    traced.clear()
    store.mark_verified(memory.id)
    _assert_one_event_inside_lock(traced, "load", path.name)


# ---------------------------------------------------------------------------
# tombstone
# ---------------------------------------------------------------------------


def test_tombstone_reads_inside_lock(store: Store, traced: list[str]) -> None:
    """`tombstone` reads via `frontmatter.load` (not `_load_path`)
    because the original is on its way out and we want the raw
    frontmatter for metadata mutation. The fix moves that read
    inside the lock alongside the tombstone write."""
    memory = store.write(content="body", scopes=["tools"])
    path = next(p for p in store._iter_active_paths() if p.is_file())
    traced.clear()
    store.tombstone(memory.id, reason="not needed")
    _assert_one_event_inside_lock(traced, "fm_load", path.name)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_reads_tombstone_inside_lock(store: Store, traced: list[str]) -> None:
    """`restore` locks on the tombstone path for the whole read +
    write + unlink sequence. The pre-fix code locked on the active
    path instead, leaving the tombstone read unguarded against a
    concurrent rename_scope on tombstones (which also mutates the
    file)."""
    memory = store.write(content="body", scopes=["tools"])
    store.tombstone(memory.id, reason="oops")
    tombstone_path = next(p for p in store._iter_tombstone_paths() if p.is_file())
    traced.clear()
    store.restore(memory.id)
    _assert_one_event_inside_lock(traced, "fm_load", tombstone_path.name)


# ---------------------------------------------------------------------------
# rename_scope
# ---------------------------------------------------------------------------


def test_rename_scope_loads_inside_lock(store: Store, traced: list[str]) -> None:
    store.write(content="body", scopes=["old-scope"])
    path = next(p for p in store._iter_active_paths() if p.is_file())
    traced.clear()
    store.rename_scope("old-scope", "new-scope")
    _assert_one_event_inside_lock(traced, "load", path.name)


def test_rename_scope_tombstone_reads_inside_lock(
    store: Store, traced: list[str]
) -> None:
    """The tombstone branch of `rename_scope` has the same TOCTOU
    surface as the active branch: a concurrent `restore` lands
    between an unlocked read and a locked write and gets clobbered.
    Symmetric fix — symmetric structural assertion."""
    memory = store.write(content="body", scopes=["old-scope"])
    store.tombstone(memory.id, reason="not needed")
    tombstone_path = next(p for p in store._iter_tombstone_paths() if p.is_file())
    traced.clear()
    store.rename_scope("old-scope", "new-scope")
    _assert_one_event_inside_lock(traced, "fm_load", tombstone_path.name)


def test_restore_index_upsert_under_active_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: the FTS index upsert in `restore()` must run under the
    ACTIVE-path lock (H1 invariant), not the tombstone-path lock. Pre-fix
    the upsert ran while only the tombstone lock was held, so a
    concurrent update()/verify() holding the active-path lock could
    interleave its SQLite upsert and leave index-vs-disk divergence.
    We trace `_locked` enter/exit and `_index_upsert_quietly` and assert
    the upsert lands inside an active-path lock block.
    """
    memory = store.write(content="body", scopes=["tools"])
    store.tombstone(memory.id, reason="oops")

    events: list[str] = []
    original_locked = store_module._locked
    original_upsert = store_module._index_upsert_quietly

    @contextlib.contextmanager
    def traced_locked(path: Path):
        events.append(f"lock_enter:{path.name}")
        with original_locked(path):
            yield
        events.append(f"lock_exit:{path.name}")

    def traced_upsert(root, mem, *, filename, provenance=None):
        events.append(f"upsert:{filename}")
        return original_upsert(root, mem, filename=filename, provenance=provenance)

    monkeypatch.setattr(store_module, "_locked", traced_locked)
    monkeypatch.setattr(store_module, "_index_upsert_quietly", traced_upsert)

    store.restore(memory.id)

    # Find the active file's name (the restore just recreated it).
    active_name = next(p.name for p in store._iter_active_paths() if p.is_file())

    # The upsert event must sit between a lock_enter and lock_exit for
    # the ACTIVE path — not merely inside the tombstone lock.
    in_active_lock = False
    upsert_under_active_lock = False
    for e in events:
        if e == f"lock_enter:{active_name}":
            in_active_lock = True
        elif e == f"lock_exit:{active_name}":
            in_active_lock = False
        elif e == f"upsert:{active_name}" and in_active_lock:
            upsert_under_active_lock = True
            break
    assert upsert_under_active_lock, (
        f"restore's index upsert did not run under "
        f"_locked({active_name!r}) — H1 active-path-lock invariant "
        f"violated; events={events}"
    )


# ---------------------------------------------------------------------------
# F6: restore's ACTIVE write + tombstone unlink must run UNDER the active-path
# lock (not just the re-load + index upsert). Pre-fix the active write and the
# source-tombstone unlink sat in the write->reload gap outside any active-path
# lock, so a concurrent tombstone()/memory_remove of the same id could acquire
# _locked(active_path) uncontended, read the just-written active file, write its
# own tombstone at the SAME `<stem>.<id>.tombstone.md` path this restore is
# about to unlink, and unlink the active file — after which this restore
# unlinked that fresh tombstone, leaving BOTH files gone (the record vanished).
# ---------------------------------------------------------------------------


def test_restore_active_write_under_active_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural: the active-record write in `restore()` runs between a
    `lock_enter` and `lock_exit` for the ACTIVE path. Pre-fix it ran before any
    active-path lock_enter, leaving the write->unlink window unguarded."""
    from bettermemory import _frontmatter as fm

    memory = store.write(content="body", scopes=["tools"])
    store.tombstone(memory.id, reason="oops")

    events: list[str] = []
    original_locked = store_module._locked
    original_write = store_module._atomic_write_post

    @contextlib.contextmanager
    def traced_locked(path: Path) -> Generator[None, None, None]:
        events.append(f"lock_enter:{path.name}")
        with original_locked(path):
            yield
        events.append(f"lock_exit:{path.name}")

    def traced_write(
        path: Path, post: Any, *, max_file_bytes: int = fm._MAX_WRITE_BYTES
    ):
        events.append(f"write:{path.name}")
        return original_write(path, post, max_file_bytes=max_file_bytes)

    monkeypatch.setattr(store_module, "_locked", traced_locked)
    monkeypatch.setattr(store_module, "_atomic_write_post", traced_write)

    store.restore(memory.id)
    active_name = next(p.name for p in store._iter_active_paths() if p.is_file())
    _assert_one_event_inside_lock(events, "write", active_name)


def test_restore_concurrent_tombstone_never_loses_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Race harness for the F6 both-files-deleted bug. We drive a concurrent
    `tombstone(id)` into restore's critical section right after the active file
    is written: with the fix, restore holds `_locked(active_path)` across the
    write + source-tombstone unlink, so the racer BLOCKS on that lock and only
    tombstones the fully-restored record afterwards — the record survives as
    exactly one of {active, tombstoned}. Reverting the fix (active write/unlink
    outside the active lock) lets the racer interleave and delete both files.
    """
    import threading
    import time

    from bettermemory import _frontmatter as fm

    memory = store.write(content="race body", scopes=["tools"])
    store.tombstone(memory.id, reason="removed once")

    original_write = store_module._atomic_write_post
    started = threading.Event()
    racer: dict[str, threading.Thread] = {}
    result: dict[str, str] = {}

    def concurrent_tombstone() -> None:
        started.set()
        try:
            store.tombstone(memory.id, reason="raced removal")
            result["outcome"] = "tombstoned"
        except Exception as exc:  # noqa: BLE001 — a raced removal may lose cleanly
            result["outcome"] = repr(exc)

    def hook_write(path: Path, post: Any, *, max_file_bytes: int = fm._MAX_WRITE_BYTES):
        # Restore's active write. Do the real write FIRST so the active file
        # exists, THEN launch the racer and give it time to find the active
        # file and reach `_locked(active_path)` — where the fix makes it block
        # (we still hold that lock) and the revert lets it interleave. Guard so
        # the racer's own tombstone write (which also routes through this patch)
        # doesn't relaunch.
        ret = original_write(path, post, max_file_bytes=max_file_bytes)
        if not racer:
            t = threading.Thread(target=concurrent_tombstone)
            racer["t"] = t
            t.start()
            started.wait(2.0)
            time.sleep(0.5)
        return ret

    monkeypatch.setattr(store_module, "_atomic_write_post", hook_write)

    try:
        store.restore(memory.id)
    except Exception:  # noqa: BLE001 — the reverted code raises; assert survival
        pass
    racer["t"].join(5.0)

    active_ids = {m.id for m in store.load_all()}
    tomb_ids = {t.id for t in store.load_tombstones()}
    assert (memory.id in active_ids) ^ (memory.id in tomb_ids), (
        f"record lost or duplicated by the restore/tombstone race — "
        f"active={memory.id in active_ids}, tombstoned={memory.id in tomb_ids}; "
        f"racer outcome={result.get('outcome')!r}"
    )
