"""Regression tests for the read-modify-write lock discipline in
`store.py`.

The race fixed in these tests: prior to the fix, `mark_verified`,
`tombstone`, `restore`, and `rename_scope` each read a file off disk
*before* acquiring the file lock, then wrote *inside* the lock. A
concurrent writer landing between the read and the lock would have
its update silently clobbered when the original caller wrote back
the stale body.

With stdio MCP as the only consumer the race was hidden behind a
single-writer assumption, but the web UI (web.py) and the sync
wrapper (sync.py) shipped in 2.0 — each of them runs alongside the
stdio server and can mutate the same files. The fix moves the read
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

    def traced_upsert(root, mem, *, filename):
        events.append(f"upsert:{filename}")
        return original_upsert(root, mem, filename=filename)

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
