"""Tests for store.py — filesystem CRUD and tombstone behavior."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from memory_mcp.models import Confidence, Source, generate_ulid, is_valid_ulid
from memory_mcp.store import (
    MemoryNotFoundError,
    Store,
    TombstonedError,
)


def test_write_and_read_back(store: Store) -> None:
    memory = store.write(
        content="Prefer code-driven tutorials.",
        scopes=["learning-style", "tools"],
    )
    assert is_valid_ulid(memory.id)

    loaded = store.load_one(memory.id)
    assert loaded.id == memory.id
    assert loaded.scopes == ["learning-style", "tools"]
    assert loaded.confidence is Confidence.MEDIUM
    assert loaded.source is Source.EXPLICIT
    assert "code-driven tutorials" in loaded.body
    # Filename embeds the date.
    assert any(p.name.startswith(memory.created.strftime("%Y-%m-%d")) for p in store.root.iterdir())


def test_write_records_creation_and_update_timestamps(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    assert memory.created == memory.updated


def test_update_bumps_updated_only(store: Store) -> None:
    memory = store.write(content="initial", scopes=["tools"])
    original_created = memory.created
    time.sleep(0.01)

    new = memory.model_copy(update={"body": "edited\n"})
    updated = store.update(new)

    assert updated.created == original_created
    assert updated.updated > original_created
    # Disk reflects the change.
    re_loaded = store.load_one(memory.id)
    assert "edited" in re_loaded.body


def test_load_all_skips_tombstoned(store: Store) -> None:
    a = store.write(content="alive", scopes=["tools"])
    b = store.write(content="dying", scopes=["tools"])

    store.tombstone(b.id, reason="superseded")

    ids = {m.id for m in store.load_all()}
    assert a.id in ids
    assert b.id not in ids


def test_tombstone_preserves_body_and_adds_removal_metadata(store: Store) -> None:
    memory = store.write(content="goodbye world", scopes=["tools"])
    path = store.tombstone(memory.id, reason="user said so")

    assert path.exists()
    text = path.read_text()
    assert "goodbye world" in text
    assert "removed:" in text
    assert "user said so" in text
    # Tombstone lives under .tombstones/.
    assert path.parent == store.tombstone_dir


def test_tombstoned_memory_load_one_raises_clearly(store: Store) -> None:
    memory = store.write(content="x", scopes=["tools"])
    store.tombstone(memory.id, reason="bad fact")

    with pytest.raises(TombstonedError) as excinfo:
        store.load_one(memory.id)
    assert "bad fact" in str(excinfo.value)


def test_load_one_missing_id(store: Store) -> None:
    with pytest.raises(MemoryNotFoundError):
        store.load_one(generate_ulid())


def test_invalid_scope_rejected_at_write(store: Store) -> None:
    with pytest.raises(ValidationError):
        # Capital letters not allowed.
        store.write(content="x", scopes=["Tools"])
    with pytest.raises(ValidationError):
        # Whitespace not allowed.
        store.write(content="x", scopes=["my tools"])


def test_empty_scopes_rejected(store: Store) -> None:
    with pytest.raises(ValidationError):
        store.write(content="x", scopes=[])


def test_filename_collision_doesnt_clobber(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force two writes to the same date+slug.
    fixed = datetime(2025, 3, 14, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("memory_mcp.store.utcnow", lambda: fixed)
    monkeypatch.setattr("memory_mcp.models.utcnow", lambda: fixed)

    a = store.write(content="hello world", scopes=["tools"])
    b = store.write(content="hello world", scopes=["tools"])
    assert a.id != b.id

    files = sorted(p.name for p in store.root.iterdir() if p.suffix == ".md")
    assert len(files) == 2
    # Both memories survive.
    assert {store.load_one(a.id).id, store.load_one(b.id).id} == {a.id, b.id}


def test_list_summaries_filters_by_scope(store: Store) -> None:
    store.write(content="python notes", scopes=["learning-style"])
    store.write(content="home lab notes", scopes=["infrastructure"])

    only_infra = store.list_summaries(scopes=["infrastructure"])
    assert len(only_infra) == 1
    assert only_infra[0].scopes == ["infrastructure"]


def test_list_summaries_strips_body(store: Store) -> None:
    store.write(
        content="One sentence. A second sentence that should be invisible.",
        scopes=["tools"],
    )
    summaries = store.list_summaries()
    assert len(summaries) == 1
    # Summary is the first sentence (or first 80 chars).
    assert "second sentence" not in summaries[0].summary


def test_round_trip_through_disk(memory_dir: Path) -> None:
    """A second Store on the same directory sees the same memories."""
    s1 = Store(memory_dir)
    a = s1.write(content="persistent", scopes=["tools"])

    s2 = Store(memory_dir)
    assert s2.load_one(a.id).body.strip() == "persistent"
