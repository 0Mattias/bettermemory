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

import threading
from pathlib import Path

import pytest

from bettermemory import index as _index
from bettermemory import store as _store
from bettermemory.store import (
    MemoryNotFoundError,
    Store,
    TombstonedError,
    _indexed_path_for_id,
)

_OUT_OF_BAND_TEMPLATE = (
    "---\n"
    "schema_version: 1\n"
    "id: {mid}\n"
    "created: 2026-01-01T00:00:00Z\n"
    "updated: 2026-01-01T00:00:00Z\n"
    "scopes:\n  - t\n"
    "confidence: medium\n"
    "source: explicit-statement\n"
    "---\n"
    "body written outside the Store API\n"
)


def _divergence_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and "out-of-sync" in r.getMessage()
    ]


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


# ---------------------------------------------------------------------------
# Startup divergence check vs. concurrent writers
#
# The fleet benchmark (`bench/swarm.py --agents 2,4 --ops 40
# --seed-corpus 10`) reported "FTS5 index appears out-of-sync with disk
# (index=N, disk=N+1)" on a store driven ENTIRELY through the Store API
# — in 8 of 8 runs. The index was never actually missing a row: the
# post-run state reconciled exactly (26/26, 41/41, 41/41 rows vs disk,
# zero orphans). The warning was reading two counters that every
# mutator updates at two different instants — `write` lands the .md
# (`_write_path`) and only then commits the index row
# (`_index_upsert_quietly`); `tombstone` unlinks the file and only then
# drops the row — while holding no lock against either. Sampling in
# between produced a gap that did not exist a millisecond later, blamed
# it on "a process bypassing memory_write", and prescribed a
# `bettermemory reindex` that had nothing to repair. It also burned the
# one-shot-per-root warning budget, so the process could never report a
# REAL desync afterwards.
#
# These tests hold that window open deterministically (the index commit
# is delayed, which is what SQLite write contention does in production)
# and pin both halves: an in-flight mutation stays silent and keeps the
# budget; an out-of-band file still warns.
# ---------------------------------------------------------------------------


def test_inflight_write_does_not_report_the_index_as_out_of_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reported bug, reduced. A concurrent `Store.write` is caught
    between its file write and its index commit — disk=2, index=1 —
    which is the exact state every swarm agent samples. Constructing a
    Store there must NOT claim the index is out-of-sync: the gap is a
    snapshot artifact of a healthy store, and the id it names resolves
    the moment the writer's upsert lands."""
    root = tmp_path / "inflight"
    store = Store(root)
    store.write(content="already indexed claim about ports", scopes=["t"])
    resolved = root.expanduser().resolve()

    real_upsert = _store._index_upsert_quietly
    file_on_disk = threading.Event()
    release_upsert = threading.Event()

    def _delayed_upsert(idx_root: Path, memory: object, *, filename: str) -> None:
        # `_write_path` has already returned, so the .md is on disk
        # while the index row is not yet committed — the window.
        file_on_disk.set()
        assert release_upsert.wait(10)
        real_upsert(idx_root, memory, filename=filename)

    monkeypatch.setattr(_store, "_index_upsert_quietly", _delayed_upsert)
    writer = threading.Thread(
        target=lambda: store.write(content="in-flight claim about ports", scopes=["t"])
    )
    writer.start()
    try:
        assert file_on_disk.wait(10), "writer never reached the index upsert"
        assert len(list(root.glob("*.md"))) == 2
        assert len(_index.indexed_ids(resolved)) == 1

        _store._DIVERGENCE_WARNED_ROOTS.discard(resolved)
        # Let the upsert commit while the construction check is still
        # settling — exactly how the real race resolves itself.
        threading.Timer(0.02, release_upsert.set).start()
        caplog.clear()
        with caplog.at_level("WARNING", logger="bettermemory.store"):
            Store(root)
    finally:
        release_upsert.set()
        writer.join(10)

    assert not _divergence_warnings(caplog), (
        "a write caught between its file step and its index step is a "
        "healthy store sampled mid-flight, not a desync; got: "
        f"{_divergence_warnings(caplog)!r}"
    )


def test_inflight_write_does_not_burn_the_one_shot_warning_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Staying silent is only half the fix. The divergence warning fires
    once per root per process, so a false positive that marks the root
    as warned would leave a genuine out-of-band file permanently
    unreported by that process. After the transient gap settles, a real
    desync on the same root must still warn."""
    root = tmp_path / "budget"
    store = Store(root)
    store.write(content="already indexed claim about ports", scopes=["t"])
    resolved = root.expanduser().resolve()

    real_upsert = _store._index_upsert_quietly
    file_on_disk = threading.Event()
    release_upsert = threading.Event()

    def _delayed_upsert(idx_root: Path, memory: object, *, filename: str) -> None:
        file_on_disk.set()
        assert release_upsert.wait(10)
        real_upsert(idx_root, memory, filename=filename)

    monkeypatch.setattr(_store, "_index_upsert_quietly", _delayed_upsert)
    writer = threading.Thread(
        target=lambda: store.write(content="in-flight claim about ports", scopes=["t"])
    )
    writer.start()
    try:
        assert file_on_disk.wait(10)
        _store._DIVERGENCE_WARNED_ROOTS.discard(resolved)
        threading.Timer(0.02, release_upsert.set).start()
        Store(root)  # transient gap — must not mark the root as warned
    finally:
        release_upsert.set()
        writer.join(10)

    assert resolved not in _store._DIVERGENCE_WARNED_ROOTS, (
        "a transient gap must not consume the one-shot budget a real desync needs"
    )

    # Now a genuine out-of-band file, same root, same process.
    (root / "2026-01-01-external.md").write_text(
        _OUT_OF_BAND_TEMPLATE.format(mid="01HXYZAAAAAAAAAAAAAAAAAAAA"),
        encoding="utf-8",
    )
    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)
    warnings = _divergence_warnings(caplog)
    assert len(warnings) == 1, (
        f"a genuine out-of-band file must still warn after a transient "
        f"gap settled on the same root, got: {warnings!r}"
    )
    assert "bettermemory reindex" in warnings[0]


def test_inflight_tombstone_does_not_report_the_index_as_out_of_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The mirror-image window: `tombstone` unlinks the active file
    before dropping the index row, so a concurrent reader samples
    index=2, disk=1 — the same artifact pointing the other way. It must
    stay silent too; the dangling row retires itself."""
    root = tmp_path / "inflight_tombstone"
    store = Store(root)
    store.write(content="survivor claim about ports", scopes=["t"])
    doomed = store.write(content="doomed claim about ports", scopes=["t"])
    resolved = root.expanduser().resolve()

    real_remove = _store._index_remove_quietly
    file_gone = threading.Event()
    release_remove = threading.Event()

    def _delayed_remove(idx_root: Path, memory_id: str) -> None:
        file_gone.set()
        assert release_remove.wait(10)
        real_remove(idx_root, memory_id)

    monkeypatch.setattr(_store, "_index_remove_quietly", _delayed_remove)
    remover = threading.Thread(target=lambda: store.tombstone(doomed.id, reason="x"))
    remover.start()
    try:
        assert file_gone.wait(10), "remover never reached the index removal"
        assert len(list(root.glob("*.md"))) == 1
        assert len(_index.indexed_ids(resolved)) == 2

        _store._DIVERGENCE_WARNED_ROOTS.discard(resolved)
        threading.Timer(0.02, release_remove.set).start()
        caplog.clear()
        with caplog.at_level("WARNING", logger="bettermemory.store"):
            Store(root)
    finally:
        release_remove.set()
        remover.join(10)

    assert not _divergence_warnings(caplog), (
        "a tombstone caught between its unlink and its index removal is "
        f"not a desync; got: {_divergence_warnings(caplog)!r}"
    )


def test_out_of_band_file_still_warns_despite_the_settle_poll(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The settle poll must not become a blanket mute. A `.md` dropped
    in by an external editor / `sync pull` never acquires an index row,
    so it survives every re-poll and still produces the actionable
    warning with the real counts."""
    root = tmp_path / "genuine"
    store = Store(root)
    store.write(content="indexed via store", scopes=["t"])
    (root / "2026-01-01-external.md").write_text(
        _OUT_OF_BAND_TEMPLATE.format(mid="01HXYZBBBBBBBBBBBBBBBBBBBB"),
        encoding="utf-8",
    )
    _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)
    warnings = _divergence_warnings(caplog)
    assert len(warnings) == 1, f"expected one warning, got {warnings!r}"
    assert "index=1" in warnings[0] and "disk=2" in warnings[0]


def test_indexed_ids_mirrors_the_rows_the_store_actually_wrote(
    tmp_path: Path,
) -> None:
    """`index.indexed_ids` is the identity counterpart to
    `meta.indexed_count` the divergence check reasons over. It reports
    exactly the ids with rows — tombstoning retires one — and answers
    the empty set (not an error) when the index file is absent."""
    store = Store(tmp_path)
    kept = store.write(content="kept claim about ports", scopes=["t"])
    dropped = store.write(content="dropped claim about ports", scopes=["t"])
    assert _index.indexed_ids(store.root) == {kept.id, dropped.id}

    store.tombstone(dropped.id, reason="gone")
    assert _index.indexed_ids(store.root) == {kept.id}

    _index.index_path(store.root).unlink()
    assert _index.indexed_ids(store.root) == set()
