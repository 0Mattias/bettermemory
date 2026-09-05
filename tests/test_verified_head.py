"""The stamp's commit anchor on the record: `Memory.verified_head`.

`memory_verify` records the commit the origin checkout stood at, and the
commit-drift leg counts `rev-list <anchor>..HEAD` from it. This module
pins the RECORD side — how the anchor is written, read, cleared, carried
through a tombstone and mirrored into the index — independent of the
counting, which `tests/test_server_commit_drift.py` covers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bettermemory import index
from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.models import Memory
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

_SHA = "3b1f9c0d2e4a6b8c0d1e2f3a4b5c6d7e8f9a0b1c"
_OTHER = "9f8e7d6c5b4a39281706f5e4d3c2b1a0f9e8d7c6"


def _seed(store: Store) -> Memory:
    return store.write(
        content="The drift gate lives in `pkg/mod.py` beside the notes.",
        scopes=["tools"],
        origin=Origin(cwd="/tmp/x", repo="git@github.com:example/foo.git"),
    )


def _build(memory_dir: Path) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True),
    )
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=recorder
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _row_anchor(root: Path, memory_id: str) -> str | None:
    conn = sqlite3.connect(str(index.index_path(root)))
    try:
        row = conn.execute(
            "SELECT verified_head FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def test_the_stamp_records_the_anchor_and_the_file_carries_it(
    memory_dir: Path,
) -> None:
    store = Store(memory_dir)
    memory = _seed(store)
    stamped = store.mark_verified(memory.id, verified_head=_SHA.upper())
    # Normalised to the lowercase spelling git prints.
    assert stamped.verified_head == _SHA
    assert store.load_one(memory.id).verified_head == _SHA
    path = store._find_path_for_id(memory.id)
    assert path is not None
    assert f"verified_head: {_SHA}" in path.read_text(encoding="utf-8")
    # A fresh Store reads the same record back: the round trip is the
    # frontmatter, not a cached object.
    assert Store(memory_dir).load_one(memory.id).verified_head == _SHA


def test_a_stamp_with_no_anchor_clears_the_previous_one(memory_dir: Path) -> None:
    """Whole on every stamp: a verify that had no checkout to read is
    not anchored at the previous stamp's commit."""
    store = Store(memory_dir)
    memory = _seed(store)
    store.mark_verified(memory.id, verified_head=_SHA)
    again = store.mark_verified(memory.id)
    assert again.last_verified_at is not None
    assert again.verified_head is None
    path = store._find_path_for_id(memory.id)
    assert path is not None
    assert "verified_head" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["main", "3b1f9c0", "--output=/tmp/x", "HEAD"])
def test_the_store_refuses_an_anchor_that_is_not_a_full_hash(
    memory_dir: Path, bad: str
) -> None:
    """The read side hands the anchor to git as a revision, so the one
    admissible shape is a full commit hash — a ref name or an
    option-shaped string never reaches an argv."""
    store = Store(memory_dir)
    memory = _seed(store)
    with pytest.raises(ValueError, match="full commit hash"):
        store.mark_verified(memory.id, verified_head=bad)
    assert store.load_one(memory.id).last_verified_at is None


def test_the_model_normalises_and_refuses_on_the_same_rule() -> None:
    kwargs: dict[str, Any] = dict(
        id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        created="2026-01-01T00:00:00+00:00",
        updated="2026-01-01T00:00:00+00:00",
        scopes=["tools"],
        confidence="high",
        source="explicit-statement",
        body="x\n",
    )
    assert Memory(**kwargs, verified_head=_SHA.upper()).verified_head == _SHA
    assert Memory(**kwargs, verified_head="a" * 64).verified_head == "a" * 64
    assert Memory(**kwargs).verified_head is None
    with pytest.raises(ValueError, match="full commit hash"):
        Memory(**kwargs, verified_head="main")


def test_a_hand_written_anchor_that_is_not_a_hash_reads_as_none(
    memory_dir: Path,
) -> None:
    """The loader drops what the store would have refused, so a
    frontmatter edit cannot smuggle a revision string to git."""
    store = Store(memory_dir)
    memory = _seed(store)
    path = store._find_path_for_id(memory.id)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    text = text.replace(f"id: {memory.id}", f"id: {memory.id}\nverified_head: main", 1)
    path.write_text(text, encoding="utf-8")
    assert Store(memory_dir).load_one(memory.id).verified_head is None


async def test_a_body_edit_clears_the_anchor_and_a_metadata_edit_keeps_it(
    memory_dir: Path,
) -> None:
    """The anchor names the tree state the OLD prose was checked
    against, so it leaves with `last_verified_at` on a content edit and
    survives a scope retag exactly as the stamp does."""
    store = Store(memory_dir)
    memory = _seed(store)
    store.mark_verified(memory.id, verified_head=_SHA)
    server = _build(memory_dir)

    retagged = await _call(server, "memory_update", id=memory.id, scopes=["tools", "x"])
    assert retagged["status"] == "committed"
    kept = store.load_one(memory.id)
    assert kept.last_verified_at is not None
    assert kept.verified_head == _SHA

    edited = await _call(
        server,
        "memory_update",
        id=memory.id,
        content="The drift gate moved to `pkg/other.py` beside the notes.",
    )
    assert edited["status"] == "committed"
    cleared = store.load_one(memory.id)
    assert cleared.last_verified_at is None
    assert cleared.verified_head is None


def test_a_tombstone_carries_the_anchor_and_a_restore_can_drop_it(
    memory_dir: Path,
) -> None:
    store = Store(memory_dir)
    memory = _seed(store)
    store.mark_verified(memory.id, verified_head=_SHA)
    store.tombstone(memory.id, reason="test")
    assert store.load_tombstone(memory.id).verified_head == _SHA

    restored = store.restore(memory.id)
    assert restored.verified_head == _SHA
    assert restored.last_verified_at is not None

    store.tombstone(memory.id, reason="again")
    dropped = store.restore(memory.id, drop_verified_head=True)
    # The anchor goes; the stamp itself is untouched — the record is
    # still the one that was verified, it just no longer says where.
    assert dropped.verified_head is None
    assert dropped.last_verified_at is not None


async def test_memory_show_carries_the_anchor_beside_the_attestations(
    memory_dir: Path,
) -> None:
    store = Store(memory_dir)
    memory = _seed(store)
    server = _build(memory_dir)
    shown = await _call(server, "memory_show", id=memory.id)
    assert shown["verified_head"] is None
    store.mark_verified(memory.id, verified_head=_SHA)
    shown = await _call(server, "memory_show", id=memory.id)
    assert shown["verified_head"] == _SHA
    assert shown["last_verified_at"] is not None


def test_the_index_row_mirrors_the_anchor_and_a_rebuild_reads_it_back(
    memory_dir: Path,
) -> None:
    assert index.SCHEMA_VERSION == 10
    store = Store(memory_dir)
    memory = _seed(store)
    assert _row_anchor(memory_dir, memory.id) is None
    store.mark_verified(memory.id, verified_head=_SHA)
    assert _row_anchor(memory_dir, memory.id) == _SHA
    store.mark_verified(memory.id, verified_head=_OTHER)
    # Never COALESCEd: the row follows the record, so a stamp that
    # changes or drops the anchor changes or drops the column.
    assert _row_anchor(memory_dir, memory.id) == _OTHER
    store.mark_verified(memory.id)
    assert _row_anchor(memory_dir, memory.id) is None
    store.mark_verified(memory.id, verified_head=_SHA)
    index.rebuild(memory_dir, store.iter_active())
    assert _row_anchor(memory_dir, memory.id) == _SHA
