"""The quarantine sidecar (`quarantine.py`) and the store's exclusion of
the files it names.

A quarantined file is a pulled memory file the admission chain refused.
It stays on disk and under git; the sidecar is what keeps it out of the
active set. These tests drive the sidecar directly and check that every
disk walk the store performs honours it: the Store-bound iteration, the
Store-free counters doctor and the startup warning read, the id lookups,
and the index rebuild that feeds on `iter_active`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from bettermemory import index, quarantine, store as store_module
from bettermemory.quarantine import (
    QUARANTINE_FILENAME,
    QuarantineEntry,
    file_digest,
    load_quarantine,
    quarantined_names,
    save_quarantine,
    sidecar_unreadable,
)
from bettermemory.store import MemoryNotFoundError, Store


def _entry(name: str, reason: str = "credential") -> QuarantineEntry:
    return QuarantineEntry(
        filename=name,
        reason=reason,
        detail="github_pat",
        remote="origin",
        pulled_at="2026-09-03T00:00:00+00:00",
        size=123,
        sha256="ab" * 32,
    )


# ---------------------------------------------------------------------------
# The sidecar
# ---------------------------------------------------------------------------


def test_save_then_load_round_trips_and_sorts(tmp_path: Path) -> None:
    entries = {"b.md": _entry("b.md", "oversize"), "a.md": _entry("a.md")}
    save_quarantine(tmp_path, entries)
    assert load_quarantine(tmp_path) == entries
    raw = json.loads((tmp_path / QUARANTINE_FILENAME).read_text())
    assert raw["version"] == 1
    assert list(raw["files"]) == ["a.md", "b.md"]
    assert raw["files"]["a.md"]["sha256"] == "ab" * 32


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_sidecar_is_private_to_the_owner(tmp_path: Path) -> None:
    save_quarantine(tmp_path, {"a.md": _entry("a.md")})
    mode = (tmp_path / QUARANTINE_FILENAME).stat().st_mode & 0o777
    assert mode == 0o600


def test_saving_nothing_removes_the_sidecar(tmp_path: Path) -> None:
    save_quarantine(tmp_path, {"a.md": _entry("a.md")})
    assert (tmp_path / QUARANTINE_FILENAME).exists()
    save_quarantine(tmp_path, {})
    assert not (tmp_path / QUARANTINE_FILENAME).exists()
    # Idempotent on an absent file.
    save_quarantine(tmp_path, {})
    assert quarantined_names(tmp_path) == frozenset()


def test_absent_sidecar_reads_empty_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="bettermemory.quarantine"):
        assert load_quarantine(tmp_path) == {}
        assert quarantined_names(tmp_path) == frozenset()
    assert caplog.records == []
    assert sidecar_unreadable(tmp_path) is None


@pytest.mark.parametrize(
    "text",
    ["not json", '{"files": []}', "[]", '{"version": 1}'],
)
def test_unreadable_sidecar_reads_empty_with_a_warning_and_is_reportable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    (tmp_path / QUARANTINE_FILENAME).write_text(text)
    with caplog.at_level(logging.WARNING, logger="bettermemory.quarantine"):
        assert load_quarantine(tmp_path) == {}
    assert any("quarantine sidecar" in r.getMessage() for r in caplog.records)
    assert sidecar_unreadable(tmp_path) is not None


def test_load_drops_entries_that_cannot_be_trusted(tmp_path: Path) -> None:
    """A filename with a separator could name a file outside the root;
    an unknown reason is not one this code wrote. Both are dropped on
    read rather than honoured."""
    payload = {
        "version": 1,
        "files": {
            "ok.md": _entry("ok.md").to_dict(),
            "../escape.md": _entry("../escape.md").to_dict(),
            "sub/dir.md": _entry("sub/dir.md").to_dict(),
            "weird.md": {**_entry("weird.md").to_dict(), "reason": "vibes"},
            "shape.md": "not a dict",
        },
    }
    (tmp_path / QUARANTINE_FILENAME).write_text(json.dumps(payload))
    assert set(load_quarantine(tmp_path)) == {"ok.md"}


def test_from_dict_coerces_the_optional_fields() -> None:
    entry = QuarantineEntry.from_dict(
        "a.md", {"reason": "unparseable", "size": True, "sha256": 5}
    )
    assert entry is not None
    assert entry.size == 0
    assert entry.sha256 is None
    assert entry.detail == ""
    assert entry.remote == ""


def test_file_digest_hashes_within_the_cap_and_stats_above_it(tmp_path: Path) -> None:
    small = tmp_path / "small.md"
    small.write_bytes(b"hello")
    size, digest = file_digest(small, max_bytes=1024)
    assert size == 5
    assert digest == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * 2048)
    size, digest = file_digest(big, max_bytes=1024)
    assert size == 2048
    assert digest is None


# ---------------------------------------------------------------------------
# The store honours the sidecar on every walk
# ---------------------------------------------------------------------------


def _filename_of(store: Store, memory_id: str) -> str:
    return next(p.name for p in store.root.glob("*.md") if memory_id.lower() in p.name)


def test_every_active_walk_skips_a_quarantined_file(memory_dir: Path) -> None:
    store = Store(memory_dir)
    kept = store.write(content="kept memory about kubernetes", scopes=["tools"])
    held = store.write(content="held memory about a deploy token", scopes=["tools"])
    held_name = _filename_of(store, held.id)

    save_quarantine(memory_dir, {held_name: _entry(held_name)})

    assert [m.id for m in store.load_all()] == [kept.id]
    assert [p.name for p, _ in store.iter_active()] != [held_name]
    assert {p.name for p, _ in store.iter_active()} == {_filename_of(store, kept.id)}
    assert held_name not in store_module.active_memory_filenames(memory_dir)
    assert store_module.count_active_memory_files(memory_dir) == 1
    ids, unparseable = store_module.scan_active_memory_ids(memory_dir)
    assert set(ids) == {kept.id}
    assert unparseable == 0
    assert [s.id for s in store.list_summaries()] == [kept.id]
    # The file itself is untouched on disk.
    assert (memory_dir / held_name).exists()


def test_load_one_refuses_a_quarantined_id_through_both_lookup_paths(
    memory_dir: Path,
) -> None:
    """`_find_path_for_id` tries the index first and walks second. The
    row survives until the next rebuild (a pull with `--no-reindex`), so
    the fast path has to refuse the name on its own."""
    store = Store(memory_dir)
    held = store.write(content="held memory about a deploy token", scopes=["tools"])
    held_name = _filename_of(store, held.id)
    # The index still resolves the id to the file.
    assert index.filenames_for_ids(memory_dir, [held.id]) == {held.id: held_name}

    save_quarantine(memory_dir, {held_name: _entry(held_name)})

    assert store_module._indexed_path_for_id(memory_dir, held.id) is None
    assert store._find_path_for_id(held.id) is None
    with pytest.raises(MemoryNotFoundError):
        store.load_one(held.id)
    with pytest.raises(MemoryNotFoundError):
        store.mark_verified(held.id)


def test_rebuild_drops_a_quarantined_row_and_release_restores_it(
    memory_dir: Path,
) -> None:
    store = Store(memory_dir)
    kept = store.write(content="kept memory about kubernetes", scopes=["tools"])
    held = store.write(content="held memory about a deploy token", scopes=["tools"])
    held_name = _filename_of(store, held.id)
    assert index.indexed_ids(memory_dir) == {kept.id, held.id}

    save_quarantine(memory_dir, {held_name: _entry(held_name)})
    index.rebuild(memory_dir, store.iter_active())
    assert index.indexed_ids(memory_dir) == {kept.id}
    assert index.status(memory_dir)["indexed_count"] == 1

    save_quarantine(memory_dir, {})
    index.rebuild(memory_dir, store.iter_active())
    assert index.indexed_ids(memory_dir) == {kept.id, held.id}
    assert store.load_one(held.id).id == held.id


def test_tombstones_are_not_affected_by_the_sidecar(memory_dir: Path) -> None:
    """The sidecar names active files only. A tombstone whose original
    filename appears in it is still listed and restorable: quarantine
    is an admission verdict, not a lifecycle state."""
    store = Store(memory_dir)
    memory = store.write(content="a memory to remove and restore", scopes=["tools"])
    name = _filename_of(store, memory.id)
    store.tombstone(memory.id, reason="testing")
    save_quarantine(memory_dir, {name: _entry(name)})
    assert [t.id for t in store.load_tombstones()] == [memory.id]


def test_quarantine_sidecar_is_gitignored_by_the_sync_layer() -> None:
    from bettermemory import sync

    assert QUARANTINE_FILENAME in sync._GITIGNORE_LINES
    assert quarantine.QUARANTINE_FILENAME.startswith(".")
