"""Provenance: how each memory entered the store (schema v7).

The label is index-resident and derived at `index.rebuild` from the
event log and the sync repo (`provenance.py`); the Store's own creation
paths stamp `local` at their upsert. These tests drive `index.rebuild`
and `index.upsert` directly with HAND-PLANTED files (a valid memory
written by a throwaway Store and copied in, so the target index sees
neither a hook upsert nor an event), which is the one shape every rule
in the derivation order has to distinguish. Each rule has a case that
fails when the rule is removed.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bettermemory import index, provenance
from bettermemory.events import Recorder
from bettermemory.store import Store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scratch_write(tmp_path: Path, name: str, body: str) -> tuple[str, Path]:
    """A valid memory file produced by a throwaway Store, not yet in the
    store under test. Returns `(id, source_path)`; `created` is now."""
    scratch = tmp_path / name
    written = Store(scratch).write(content=body, scopes=["tools"])
    source = next(p for p in scratch.glob("*.md"))
    return written.id, source


def _copy_in(source: Path, memory_dir: Path) -> str:
    """Plant a scratch-written file by hand. Returns its filename."""
    shutil.copy2(source, memory_dir / source.name)
    return source.name


def _plant(tmp_path: Path, memory_dir: Path, name: str, body: str) -> tuple[str, str]:
    memory_id, source = _scratch_write(tmp_path, name, body)
    return memory_id, _copy_in(source, memory_dir)


def _recorder(memory_dir: Path, session: str = "sess-provenance") -> Recorder:
    return Recorder(root=memory_dir, session_id=session)


def _rebuild(store: Store) -> int:
    return index.rebuild(store.root, store.iter_active())


def _drop_events(memory_dir: Path) -> None:
    for path in memory_dir.glob(".events*"):
        path.unlink()


def _git(memory_dir: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=provenance-test",
            "-c",
            "user.email=provenance@test.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=memory_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Schema and the write-side join
# ---------------------------------------------------------------------------


def test_schema_is_seven_with_the_provenance_column(
    store: Store, memory_dir: Path
) -> None:
    store.write(content="a memory that opens the index", scopes=["tools"])
    status = index.status(memory_dir)
    assert status["schema_version"] == index.SCHEMA_VERSION == 7
    with sqlite3.connect(index.index_path(memory_dir)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    assert "provenance" in columns


def test_creation_id_reads_only_write_side_kinds() -> None:
    """The join is one rule: retrieval kinds, pending writes, listed or
    dismissed proposals and updates establish nothing."""
    assert (
        provenance.creation_id({"kind": "write", "status": "committed", "id": "A"})
        == "A"
    )
    assert (
        provenance.creation_id({"kind": "write", "status": "ingested", "id": "B"})
        == "B"
    )
    assert (
        provenance.creation_id(
            {"kind": "write", "status": "pending", "pending_id": "p"}
        )
        is None
    )
    assert (
        provenance.creation_id({"kind": "write", "status": "rejected", "id": "C"})
        is None
    )
    assert provenance.creation_id({"kind": "write_confirm", "id": "D"}) == "D"
    assert provenance.creation_id({"kind": "restore", "id": "E"}) == "E"
    assert provenance.creation_id({"kind": "consolidate_write", "id": "F"}) == "F"
    assert (
        provenance.creation_id(
            {"kind": "memory_proposals", "action": "accept", "id": "G"}
        )
        == "G"
    )
    assert (
        provenance.creation_id(
            {"kind": "memory_proposals", "action": "dismiss", "id": "G"}
        )
        is None
    )
    assert provenance.creation_id({"kind": "episode_promote", "memory_id": "H"}) == "H"
    assert (
        provenance.creation_id({"kind": "episode_promote", "memory_id": None}) is None
    )
    assert provenance.creation_id({"kind": "update", "id": "I"}) is None
    assert provenance.creation_id({"kind": "search", "returned": ["J"]}) is None
    assert provenance.creation_id({"kind": "show", "id": "K"}) is None


def test_pulled_files_reads_only_sync_pull_events() -> None:
    assert provenance.pulled_files({"kind": "sync_pull", "files": ["a.md", "", 3]}) == [
        "a.md"
    ]
    assert provenance.pulled_files({"kind": "sync_pull"}) == []
    assert provenance.pulled_files({"kind": "write", "files": ["a.md"]}) == []


def test_upsert_stamps_a_label_and_none_preserves_it(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    memory_id, filename = _plant(
        tmp_path, memory_dir, "scratch-a", "an upserted memory"
    )
    memory = store.load_one(memory_id)
    index.upsert(memory_dir, memory, filename=filename)
    assert index.provenance_for(memory_dir, [memory_id]) == {}, (
        "a hook upsert with no claim lands unlabelled and the read surface omits it"
    )
    assert index.provenance_counts(memory_dir) == {"unclassified": 1}
    index.upsert(memory_dir, memory, filename=filename, provenance="local")
    assert index.provenance_for(memory_dir, [memory_id]) == {memory_id: "local"}
    index.upsert(memory_dir, memory, filename=filename, provenance=None)
    assert index.provenance_for(memory_dir, [memory_id]) == {memory_id: "local"}, (
        "an update carries no claim about how the memory entered; the label stays"
    )
    index.upsert(memory_dir, memory, filename=filename, provenance="unaccounted")
    assert index.provenance_for(memory_dir, [memory_id]) == {memory_id: "unaccounted"}


# ---------------------------------------------------------------------------
# The derivation order at rebuild
# ---------------------------------------------------------------------------


def test_no_events_at_all_reads_untracked(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    a, _ = _plant(tmp_path, memory_dir, "scratch-a", "first planted memory")
    b, _ = _plant(tmp_path, memory_dir, "scratch-b", "second planted memory")
    assert _rebuild(store) == 2
    assert index.provenance_for(memory_dir, [a, b]) == {a: "untracked", b: "untracked"}


def test_every_creation_kind_joins_to_local(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    """Planted BEFORE any event so `created` predates the log: rule 1
    (the creation join) has to carry each one on its own, ahead of the
    log-coverage rule that would otherwise read them untracked."""
    ids = {
        name: _plant(tmp_path, memory_dir, f"scratch-{name}", f"memory {name} body")[0]
        for name in (
            "committed",
            "ingested",
            "confirm",
            "restore",
            "accept",
            "promote",
            "consolidate",
            "touched",
        )
    }
    recorder = _recorder(memory_dir)
    recorder.record("write", status="committed", id=ids["committed"], scopes=["tools"])
    recorder.record("write", status="ingested", id=ids["ingested"], scopes=["tools"])
    recorder.record(
        "write_confirm", pending_id="p1", id=ids["confirm"], scopes=["tools"]
    )
    recorder.record("restore", id=ids["restore"], scopes=["tools"])
    recorder.record(
        "memory_proposals", action="accept", proposal_id="x", id=ids["accept"]
    )
    recorder.record("episode_promote", episode_id="e", memory_id=ids["promote"])
    recorder.record("consolidate_write", id=ids["consolidate"], scopes=["tools"])
    # Touched, never created: an update and a retrieval name the id.
    recorder.record("update", id=ids["touched"], scopes=["tools"])
    recorder.record("search", returned=[ids["touched"]], relevance=["high"])
    _rebuild(store)
    labels = index.provenance_for(memory_dir, list(ids.values()))
    for name, memory_id in ids.items():
        expected = "untracked" if name == "touched" else "local"
        assert labels[memory_id] == expected, f"{name}: {labels[memory_id]}"


def test_planted_inside_the_log_window_reads_unaccounted(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    """The log covers the creation window, nothing wrote it, nothing
    pulled it: the hand-planted shape."""
    _recorder(memory_dir).record("search", returned=[], relevance=[])
    a, _ = _plant(tmp_path, memory_dir, "scratch-a", "planted after the log started")
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a]) == {a: "unaccounted"}
    assert index.provenance_rows(memory_dir, label="unaccounted") == [a]


def test_predates_log_reads_untracked_until_a_baseline_makes_it_new(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    """Rule 5 both ways. A memory older than the log is honest silence
    on the first classified rebuild; the same shape arriving AFTER that
    baseline exists is a new id claiming to predate the log, which the
    log would have seen arrive: unaccounted."""
    a, a_source = _scratch_write(tmp_path, "scratch-a", "written before the log")
    b, b_source = _scratch_write(tmp_path, "scratch-b", "also written before the log")
    _copy_in(a_source, memory_dir)
    _recorder(memory_dir).record("search", returned=[], relevance=[])
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a]) == {a: "untracked"}
    _copy_in(b_source, memory_dir)
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b]) == {
        a: "untracked",
        b: "unaccounted",
    }


def test_sync_pull_event_marks_the_file_synced(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    recorder = _recorder(memory_dir)
    recorder.record("search", returned=[], relevance=[])
    a, a_name = _plant(tmp_path, memory_dir, "scratch-a", "arrived by pull")
    b, _ = _plant(tmp_path, memory_dir, "scratch-b", "arrived by hand")
    recorder.record("sync_pull", remote="origin", files=[a_name])
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b]) == {a: "synced", b: "unaccounted"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_tracked_in_the_sync_repo_reads_synced(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    """Rule 6: a pull made before the `sync_pull` event existed left no
    event, but the file is tracked in the store's own repo. Only files
    the repo tracks read synced; an untracked planted sibling does not."""
    _git(memory_dir, "init", "-q")
    _recorder(memory_dir).record("search", returned=[], relevance=[])
    a, a_name = _plant(tmp_path, memory_dir, "scratch-a", "committed in the sync repo")
    b, _ = _plant(tmp_path, memory_dir, "scratch-b", "never committed")
    _git(memory_dir, "add", "--", a_name)
    _git(memory_dir, "commit", "-q", "-m", "pulled")
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b]) == {a: "synced", b: "unaccounted"}


def test_nested_parent_repo_is_not_a_sync_repo(tmp_path: Path) -> None:
    """A store inside somebody else's checkout must not read the parent's
    tracking as its own."""
    _git(tmp_path, "init", "-q")
    nested = tmp_path / "memories"
    nested.mkdir()
    assert provenance.is_sync_repo(nested) is False
    assert provenance.gather_evidence(nested).tracked_files is None


# ---------------------------------------------------------------------------
# Stickiness, the stash, and the reset
# ---------------------------------------------------------------------------


def _local_and_unaccounted(
    store: Store, memory_dir: Path, tmp_path: Path
) -> tuple[str, str]:
    recorder = _recorder(memory_dir)
    recorder.record("search", returned=[], relevance=[])
    a, _ = _plant(tmp_path, memory_dir, "scratch-a", "locally created")
    b, _ = _plant(tmp_path, memory_dir, "scratch-b", "planted by hand")
    recorder.record("write", status="committed", id=a, scopes=["tools"])
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b]) == {a: "local", b: "unaccounted"}
    return a, b


def test_local_and_unaccounted_are_sticky_when_the_log_goes_away(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    a, b = _local_and_unaccounted(store, memory_dir, tmp_path)
    _drop_events(memory_dir)
    c, _ = _plant(
        tmp_path, memory_dir, "scratch-c", "planted after the log was deleted"
    )
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b, c]) == {
        a: "local",
        b: "unaccounted",
        c: "untracked",
    }


def test_labels_survive_a_tokenizer_drop_through_the_stash(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    """`_ensure_schema` drops the data tables on a fingerprint mismatch.
    With the events gone too, only the `meta.provenance_carry` stash can
    explain the labels coming back."""
    a, b = _local_and_unaccounted(store, memory_dir, tmp_path)
    _drop_events(memory_dir)
    db_path = index.index_path(memory_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE meta SET value = 'stale' WHERE key = 'tokenizer_fingerprint'"
        )
    status = index.status(memory_dir)
    assert status["needs_rebuild"] is True
    assert status["indexed_count"] == 0
    with sqlite3.connect(db_path) as conn:
        stash = conn.execute(
            "SELECT value FROM meta WHERE key = 'provenance_carry'"
        ).fetchone()
    assert stash is not None and a in stash[0] and b in stash[0]
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b]) == {a: "local", b: "unaccounted"}
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key = 'provenance_carry'"
            ).fetchone()
            is None
        ), "the rebuild consumes the stash"


def test_deleting_the_index_reclassifies_from_events_alone(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    a, b = _local_and_unaccounted(store, memory_dir, tmp_path)
    db_path = index.index_path(memory_dir)
    for sibling in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        if sibling.exists():
            sibling.unlink()
    assert index.provenance_for(memory_dir, [a, b]) == {}
    assert index.provenance_counts(memory_dir) is None
    assert index.provenance_rows(memory_dir, label="local") is None
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b]) == {a: "local", b: "unaccounted"}
    _drop_events(memory_dir)
    for sibling in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        if sibling.exists():
            sibling.unlink()
    _rebuild(store)
    assert index.provenance_for(memory_dir, [a, b]) == {
        a: "untracked",
        b: "untracked",
    }, "no index, no events: nothing can speak, and nothing is sticky"


def test_counts_cover_every_row(store: Store, memory_dir: Path, tmp_path: Path) -> None:
    _, b = _local_and_unaccounted(store, memory_dir, tmp_path)
    c, c_name = _plant(tmp_path, memory_dir, "scratch-c", "upserted without a claim")
    index.upsert(memory_dir, store.load_one(c), filename=c_name)
    assert index.provenance_counts(memory_dir) == {
        "local": 1,
        "unaccounted": 1,
        "unclassified": 1,
    }
    _rebuild(store)
    assert index.provenance_counts(memory_dir) == {"local": 1, "unaccounted": 2}
    rows = index.provenance_rows(memory_dir, label="unaccounted")
    assert rows is not None and set(rows) == {b, c}
    assert rows == sorted(
        rows, key=lambda memory_id: store.load_one(memory_id).created, reverse=True
    ), "newest created first"


# ---------------------------------------------------------------------------
# The Store's own creation paths stamp `local` at the upsert
# ---------------------------------------------------------------------------


def test_store_write_stamps_local_before_any_rebuild(
    store: Store, memory_dir: Path
) -> None:
    memory = store.write(content="written through the store", scopes=["tools"])
    assert index.provenance_for(memory_dir, [memory.id]) == {memory.id: "local"}
    assert index.provenance_counts(memory_dir) == {"local": 1}


def test_store_write_stays_local_with_no_events_and_a_rebuild(
    store: Store, memory_dir: Path
) -> None:
    """No recorder runs here: the stamp, not an event, is what makes the
    write local, and the sticky rule carries it through a rebuild that
    finds no events at all."""
    memory = store.write(content="written with telemetry off", scopes=["tools"])
    _rebuild(store)
    assert index.provenance_for(memory_dir, [memory.id]) == {memory.id: "local"}


def test_update_and_verify_keep_the_label(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    _, b = _local_and_unaccounted(store, memory_dir, tmp_path)
    planted = store.load_one(b)
    store.update(planted.model_copy(update={"body": "edited by hand later\n"}))
    assert index.provenance_for(memory_dir, [b]) == {b: "unaccounted"}
    store.mark_verified(b)
    assert index.provenance_for(memory_dir, [b]) == {b: "unaccounted"}


def test_restore_stamps_local(store: Store, memory_dir: Path, tmp_path: Path) -> None:
    _, b = _local_and_unaccounted(store, memory_dir, tmp_path)
    store.tombstone(b, reason="planted")
    assert index.provenance_for(memory_dir, [b]) == {}
    store.restore(b)
    assert index.provenance_for(memory_dir, [b]) == {b: "local"}
