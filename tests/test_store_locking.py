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
from pathlib import Path
from typing import Any, Iterator

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
    def traced_locked(path: Path) -> Iterator[None]:
        events.append(f"lock_enter:{path.name}")
        with original_locked(path):
            yield
        events.append(f"lock_exit:{path.name}")

    monkeypatch.setattr(Store, "_load_path", traced_load_path)
    monkeypatch.setattr(store_module, "_locked", traced_locked)
    # `store.py` does `from . import _frontmatter as frontmatter` — patch
    # the alias on `store_module` so the calls inside tombstone/restore
    # route through the trace.
    monkeypatch.setattr(store_module.frontmatter, "load", traced_fm_load)
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
