"""Index-backed by-id resolution (swarm-convergence Phase 1).

`load_one` / `_find_path_for_id` resolve an id through the FTS5 index in
O(1) instead of walking + reparsing the whole active directory (the
Phase-0 benchmark measured that walk taking a single update from ~9 ms
at 50 memories to ~320 ms at 3200).

The load-bearing property is not just "it's faster" but "it stays
correct": the index is only ever a speed hint. A stale, absent, or
outright lying index must never change the ANSWER — only how fast it's
reached. These tests pin both halves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bettermemory import index as _index
from bettermemory.store import (
    MemoryNotFoundError,
    Store,
    TombstonedError,
    _indexed_path_for_id,
)


def test_fast_path_serves_lookup_without_walking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a healthy index, neither `load_one` nor `_find_path_for_id`
    touches the O(corpus) walk. We prove it by making the walk explode:
    a passing test means the index fast path served the lookup."""
    store = Store(tmp_path)
    m = store.write(content="durable claim about the deploy pipeline", scopes=["t"])

    def _boom(self: Store) -> object:
        raise AssertionError("fell back to the directory walk on a healthy index")

    monkeypatch.setattr(Store, "_iter_active_paths", _boom)

    assert store.load_one(m.id).id == m.id
    assert store._find_path_for_id(m.id) is not None
    assert _indexed_path_for_id(store.root, m.id) is not None


def test_fast_path_returns_the_correct_memory_in_a_larger_store(tmp_path: Path) -> None:
    """Sanity that the O(1) resolve returns the RIGHT file, not just a
    file, across a store big enough that a mixup would show."""
    store = Store(tmp_path)
    ids = [
        store.write(content=f"claim number {i} about config and ports", scopes=["t"]).id
        for i in range(30)
    ]
    for mid in ids:
        assert store.load_one(mid).id == mid


def test_absent_index_falls_back_to_walk(tmp_path: Path) -> None:
    """Delete the derived cache: the answer is unchanged, served by the
    authoritative walk. Files are canonical."""
    store = Store(tmp_path)
    m = store.write(content="claim survives index deletion", scopes=["t"])
    _index.index_path(store.root).unlink()
    assert not _index.index_path(store.root).exists()

    assert store.load_one(m.id).id == m.id
    found = store._find_path_for_id(m.id)
    assert found is not None and found.exists()


def test_stale_index_hint_is_rejected_and_the_walk_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index lies — points the id at a filename that doesn't carry
    it. `_id_still_at_path` must reject the hint so the walk finds the
    truth. This is the core safety property: a stale row can't produce
    a wrong answer."""
    store = Store(tmp_path)
    m = store.write(content="claim the index will lie about", scopes=["t"])

    monkeypatch.setattr(
        _index,
        "filenames_for_ids",
        lambda root, ids: {m.id: "this-file-does-not-exist.md"},
    )

    # The hint is rejected outright.
    assert _indexed_path_for_id(store.root, m.id) is None
    # And the lookups still return the truth via the walk.
    assert store.load_one(m.id).id == m.id
    assert store._find_path_for_id(m.id) is not None


def test_index_pointing_at_a_different_memory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subtler stale case: the index names a real file, but one that
    belongs to a DIFFERENT memory (slug reuse / drift). The id-recheck
    must catch the mismatch, not hand back the wrong memory."""
    store = Store(tmp_path)
    target = store.write(content="the memory we actually want", scopes=["t"])
    other = store.write(content="a completely different memory", scopes=["t"])
    other_path = store._find_path_for_id(other.id)
    assert other_path is not None

    monkeypatch.setattr(
        _index,
        "filenames_for_ids",
        lambda root, ids: {target.id: other_path.name},
    )

    assert _indexed_path_for_id(store.root, target.id) is None
    assert store.load_one(target.id).id == target.id  # not `other`


def test_unindexed_active_file_found_via_walk(tmp_path: Path) -> None:
    """An active file with no index row (recent write not yet indexed,
    or an external editor drop) is still found — index miss routes to
    the walk."""
    store = Store(tmp_path)
    m = store.write(content="active but absent from the index", scopes=["t"])
    _index.remove(store.root, m.id)  # drop the row, keep the file
    assert _index.filenames_for_ids(store.root, [m.id]) == {}

    assert store.load_one(m.id).id == m.id
    assert store._find_path_for_id(m.id) is not None


def test_tombstoned_id_still_raises_tombstoned_error(tmp_path: Path) -> None:
    """Tombstoning removes the index row, so the fast path misses and
    the fallback walks active (miss) then tombstones (hit) — the
    TombstonedError semantic is preserved."""
    store = Store(tmp_path)
    m = store.write(content="will be tombstoned", scopes=["t"])
    store.tombstone(m.id, reason="gone")

    with pytest.raises(TombstonedError):
        store.load_one(m.id)
    assert store._find_path_for_id(m.id) is None


def test_missing_id_still_raises_not_found(tmp_path: Path) -> None:
    """A never-written valid ulid resolves to nothing through both the
    index and the walk."""
    store = Store(tmp_path)
    store.write(content="something unrelated", scopes=["t"])
    ghost = "01JZZZZZZZZZZZZZZZZZZZZZZZZ"  # valid ulid shape, never written

    with pytest.raises(MemoryNotFoundError):
        store.load_one(ghost)
    assert store._find_path_for_id(ghost) is None
