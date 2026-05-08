"""Tests for the tombstone lifecycle: removed_session, list, load, restore,
prune.

These tests close the feedback hole where tombstones used to be a black hole
on the read side. The lifecycle here is what makes them first-class: a
removal can be inspected, undone, or audit-pruned, and the originating
session is durably stamped on the file independent of the event log.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bettermemory.models import generate_ulid
from bettermemory.store import (
    MemoryNotFoundError,
    NotTombstonedError,
    Store,
)


# ---------------------------------------------------------------------------
# removed_session frontmatter
# ---------------------------------------------------------------------------


def test_tombstone_with_session_id_writes_removed_session(store: Store) -> None:
    """The session id passed at tombstone-time should land on the file
    so the join survives event-log rotation."""
    memory = store.write(content="x", scopes=["tools"])
    path = store.tombstone(memory.id, reason="bad fact", session_id="sess_abc")
    assert "removed_session: sess_abc" in path.read_text()


def test_tombstone_without_session_id_omits_field(store: Store) -> None:
    """Legacy callers (or tests that don't care) shouldn't get a literal
    `removed_session: null` in frontmatter — visual noise."""
    memory = store.write(content="x", scopes=["tools"])
    path = store.tombstone(memory.id, reason="bad")
    text = path.read_text()
    assert "removed:" in text
    assert "removed_reason:" in text
    assert "removed_session" not in text


def test_load_tombstone_carries_session_id(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="r", session_id="sess_xyz")
    tombstone = store.load_tombstone(memory.id)
    assert tombstone.removed_session == "sess_xyz"
    assert tombstone.removed_reason == "r"
    assert tombstone.id == memory.id


def test_load_tombstone_legacy_without_session_field(memory_dir: Path) -> None:
    """A tombstone written before the removed_session field shipped should
    still load — the field is additive."""
    legacy = memory_dir / ".tombstones" / "2025-01-01-legacy.tombstone.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\n"
        f"id: {generate_ulid()}\n"
        "created: 2025-01-01T00:00:00Z\n"
        "updated: 2025-01-01T00:00:00Z\n"
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "removed: 2025-06-01T00:00:00Z\n"
        "removed_reason: legacy removal\n"
        "---\n"
        "legacy body\n"
    )
    store = Store(memory_dir)
    tombstones = store.load_tombstones()
    assert len(tombstones) == 1
    assert tombstones[0].removed_session is None
    assert tombstones[0].removed_reason == "legacy removal"


# ---------------------------------------------------------------------------
# list_tombstones
# ---------------------------------------------------------------------------


def test_list_tombstones_returns_recent_first(store: Store) -> None:
    a = store.write(content="alpha", scopes=["tools"])
    b = store.write(content="beta", scopes=["tools"])
    store.tombstone(a.id, reason="first")
    time.sleep(0.01)
    store.tombstone(b.id, reason="second")

    summaries = store.list_tombstones()
    assert [s.id for s in summaries] == [b.id, a.id]


def test_list_tombstones_filters_by_scope(store: Store) -> None:
    a = store.write(content="alpha", scopes=["tools"])
    b = store.write(content="beta", scopes=["infrastructure"])
    store.tombstone(a.id, reason="r")
    store.tombstone(b.id, reason="r")

    only_tools = store.list_tombstones(scopes=["tools"])
    assert len(only_tools) == 1
    assert only_tools[0].id == a.id


def test_list_tombstones_strips_body(store: Store) -> None:
    memory = store.write(
        content="One sentence. A second sentence that should be invisible.",
        scopes=["tools"],
    )
    store.tombstone(memory.id, reason="r")
    summaries = store.list_tombstones()
    assert len(summaries) == 1
    assert "second sentence" not in summaries[0].summary


def test_list_tombstones_empty_when_none_removed(store: Store) -> None:
    store.write(content="x", scopes=["tools"])
    assert store.list_tombstones() == []


# ---------------------------------------------------------------------------
# load_tombstone
# ---------------------------------------------------------------------------


def test_load_tombstone_invalid_id_raises(store: Store) -> None:
    with pytest.raises(MemoryNotFoundError):
        store.load_tombstone("not-a-ulid")


def test_load_tombstone_unknown_id_raises(store: Store) -> None:
    with pytest.raises(MemoryNotFoundError):
        store.load_tombstone(generate_ulid())


def test_load_tombstone_skips_active(store: Store) -> None:
    """An active id is not a tombstone — load_tombstone should not return
    it. The active load path is `load_one`."""
    memory = store.write(content="x", scopes=["tools"])
    with pytest.raises(MemoryNotFoundError):
        store.load_tombstone(memory.id)


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def test_restore_moves_file_back_and_strips_removal(store: Store) -> None:
    memory = store.write(content="goodbye world", scopes=["tools"])
    store.tombstone(memory.id, reason="oops", session_id="sess_a")

    restored = store.restore(memory.id)
    assert restored.id == memory.id
    assert restored.body.strip() == "goodbye world"

    # The file is back in the active set.
    reloaded = store.load_one(memory.id)
    assert reloaded.id == memory.id

    # The tombstone file is gone.
    with pytest.raises(MemoryNotFoundError):
        store.load_tombstone(memory.id)

    # No removal frontmatter on the active file.
    md = next(p for p in store.root.iterdir() if p.suffix == ".md")
    text = md.read_text()
    assert "removed:" not in text
    assert "removed_reason" not in text
    assert "removed_session" not in text


def test_restore_preserves_timestamps(store: Store) -> None:
    """Restore is a metadata strip + file move; the body didn't change,
    so `updated` should not move. A freshly-restored ten-year-old memory
    must rank like a ten-year-old memory in the recency boost."""
    memory = store.write(content="x", scopes=["tools"])
    original_created = memory.created
    original_updated = memory.updated

    time.sleep(0.01)
    store.tombstone(memory.id, reason="r")
    time.sleep(0.01)
    restored = store.restore(memory.id)

    assert restored.created == original_created
    assert restored.updated == original_updated


def test_restore_preserves_last_verified_at(store: Store) -> None:
    """Verification is a property of the body content. Removing then
    restoring doesn't change the body, so the verification stamp travels."""
    memory = store.write(content="x", scopes=["tools"])
    verified = store.mark_verified(memory.id)
    assert verified.last_verified_at is not None

    store.tombstone(memory.id, reason="r")
    restored = store.restore(memory.id)
    assert restored.last_verified_at == verified.last_verified_at


def test_restore_active_id_raises_not_tombstoned(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    with pytest.raises(NotTombstonedError):
        store.restore(memory.id)


def test_restore_unknown_id_raises_not_found(store: Store) -> None:
    with pytest.raises(MemoryNotFoundError):
        store.restore(generate_ulid())


def test_restore_invalid_id_raises_not_found(store: Store) -> None:
    with pytest.raises(MemoryNotFoundError):
        store.restore("not-a-ulid")


def test_restore_handles_active_filename_collision(store: Store) -> None:
    """If a same-date+slug active memory exists when restoring, the
    restored file should land at a non-colliding path rather than
    clobbering the active file."""
    memory = store.write(content="hello world", scopes=["tools"])
    store.tombstone(memory.id, reason="r")

    # Squat the slug with a different active memory before restoring.
    decoy = store.write(content="hello world", scopes=["tools"])
    assert decoy.id != memory.id

    restored = store.restore(memory.id)
    assert restored.id == memory.id

    # Both memories are now active and load cleanly.
    assert store.load_one(memory.id).id == memory.id
    assert store.load_one(decoy.id).id == decoy.id


def test_restore_then_remove_again_roundtrip(store: Store) -> None:
    """Tombstone -> restore -> tombstone -> restore should leave the
    store in the same logical state as the initial write."""
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="first removal")
    store.restore(memory.id)
    store.tombstone(memory.id, reason="second removal")
    restored = store.restore(memory.id)
    assert restored.id == memory.id
    assert store.load_one(memory.id).id == memory.id


# ---------------------------------------------------------------------------
# prune_tombstones
# ---------------------------------------------------------------------------


def test_prune_deletes_old_tombstones(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="r")

    # Pretend the tombstone is from a year ago.
    far_future = datetime.now(timezone.utc) + timedelta(days=400)
    pruned = store.prune_tombstones(timedelta(days=30), now=far_future)
    assert pruned == [memory.id]
    # Disk reflects the deletion.
    assert store.load_tombstones() == []


def test_prune_leaves_recent_tombstones(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="r")
    pruned = store.prune_tombstones(timedelta(days=30))
    assert pruned == []
    assert len(store.load_tombstones()) == 1


def test_prune_returns_ids_in_chronological_order(store: Store) -> None:
    a = store.write(content="alpha", scopes=["tools"])
    b = store.write(content="beta", scopes=["tools"])
    c = store.write(content="gamma", scopes=["tools"])

    store.tombstone(a.id, reason="r")
    time.sleep(0.01)
    store.tombstone(b.id, reason="r")
    time.sleep(0.01)
    store.tombstone(c.id, reason="r")

    far_future = datetime.now(timezone.utc) + timedelta(days=400)
    pruned = store.prune_tombstones(timedelta(days=30), now=far_future)
    # Oldest removal first.
    assert pruned == [a.id, b.id, c.id]


def test_prune_idempotent(store: Store) -> None:
    """Running prune twice is safe — the second call deletes nothing."""
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="r")

    far_future = datetime.now(timezone.utc) + timedelta(days=400)
    first = store.prune_tombstones(timedelta(days=30), now=far_future)
    second = store.prune_tombstones(timedelta(days=30), now=far_future)
    assert first == [memory.id]
    assert second == []


def test_prune_leaves_active_memories_alone(store: Store) -> None:
    """Pruning the tombstone audit log must never touch active memories."""
    active = store.write(content="active", scopes=["tools"])
    removed = store.write(content="removed", scopes=["tools"])
    store.tombstone(removed.id, reason="r")

    far_future = datetime.now(timezone.utc) + timedelta(days=400)
    store.prune_tombstones(timedelta(days=30), now=far_future)

    assert store.load_one(active.id).id == active.id
    assert store.load_tombstones() == []


def test_prune_empty_store_returns_empty(store: Store) -> None:
    assert store.prune_tombstones(timedelta(days=30)) == []
