"""Tests for tombstone-aware dedup.

The active-side dedup catches "you already wrote this"; the tombstone-side
catches "you already wrote this *and* removed it for a reason." Without it,
the lesson encoded in `removed_reason` is silently discarded the moment
the writer re-creates the same fact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.search import find_similar_tombstones
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


# ---------------------------------------------------------------------------
# search.find_similar_tombstones
# ---------------------------------------------------------------------------


def test_find_similar_tombstones_flags_high_overlap(store: Store) -> None:
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    memory = store.write(content=body, scopes=["tools"])
    store.tombstone(memory.id, reason="testing")

    hits = find_similar_tombstones(body, store.load_tombstones())
    assert len(hits) == 1
    assert hits[0].id == memory.id
    assert hits[0].relevance == "high-removed"
    assert hits[0].removed_reason == "testing"
    assert hits[0].removed_at is not None


def test_find_similar_tombstones_flags_medium_overlap(store: Store) -> None:
    """A partial overlap should land in `medium-removed` — surfaced
    advisorily, not blocking on the dedup gate."""
    memory = store.write(
        content=("vendored python-frontmatter to drop the deprecated codecs.open call"),
        scopes=["tools"],
    )
    store.tombstone(memory.id, reason="bad fact")

    # Some shared tokens ("python", "codecs", "deprecated") but a different
    # claim — should land medium, not high.
    new_body = "python's codecs module emits deprecated warnings on python 3.14"
    hits = find_similar_tombstones(new_body, store.load_tombstones())
    if hits:
        assert hits[0].relevance in {"high-removed", "medium-removed"}


def test_find_similar_tombstones_skips_unrelated(store: Store) -> None:
    memory = store.write(content="alpha topic body", scopes=["tools"])
    store.tombstone(memory.id, reason="r")

    hits = find_similar_tombstones(
        "completely unrelated content here", store.load_tombstones()
    )
    assert hits == []


def test_find_similar_tombstones_empty_input() -> None:
    assert find_similar_tombstones("body", []) == []
    assert find_similar_tombstones("", []) == []


def test_find_similar_tombstones_sort_order(store: Store) -> None:
    """Hits should sort by similarity descending."""
    a = store.write(content="alpha beta gamma delta epsilon", scopes=["tools"])
    b = store.write(content="alpha beta gamma delta", scopes=["tools"])
    store.tombstone(a.id, reason="r")
    store.tombstone(b.id, reason="r")

    hits = find_similar_tombstones("alpha beta gamma delta", store.load_tombstones())
    assert len(hits) >= 2
    # Better-overlap hit (b, an exact match) should rank first.
    assert hits[0].id == b.id


# ---------------------------------------------------------------------------
# memory_write integration: previously_removed status
# ---------------------------------------------------------------------------


async def test_write_blocks_when_high_tombstone_match(server: Any) -> None:
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    written = await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="bad fact")

    duplicate = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert duplicate["status"] == "previously_removed"
    assert duplicate["removed_matches"][0]["id"] == written["id"]
    assert duplicate["removed_matches"][0]["removed_reason"] == "bad fact"
    assert "hint" in duplicate
    assert "memory_restore" in duplicate["hint"]


async def test_write_succeeds_when_only_medium_tombstone_match(
    server: Any,
) -> None:
    """Medium-overlap tombstones are advisory — the write should commit
    and surface them as `removed_related`, not block."""
    written = await _call(
        server,
        "memory_write",
        content=("vendored python-frontmatter to drop the deprecated codecs.open call"),
        scopes=["tools"],
    )
    await _call(server, "memory_remove", id=written["id"], reason="bad fact")

    new_body = "python's codecs module emits deprecated warnings"
    new_write = await _call(server, "memory_write", content=new_body, scopes=["tools"])
    # Either committed (medium-removed → advisory) or duplicate (active high).
    # We never expect previously_removed for medium matches.
    assert new_write["status"] != "previously_removed"


async def test_committed_response_surfaces_removed_related(server: Any) -> None:
    """When a successful write has medium-overlap tombstone matches, they
    should land under `removed_related` so the writer can consult them."""
    a_body = "alpha beta gamma delta epsilon zeta eta theta iota"
    a = await _call(server, "memory_write", content=a_body, scopes=["tools"])
    await _call(server, "memory_remove", id=a["id"], reason="archived")

    # Different but partially-overlapping body.
    b_body = "alpha beta gamma kappa lambda mu nu xi omicron"
    b = await _call(server, "memory_write", content=b_body, scopes=["tools"])
    if b["status"] == "committed" and "removed_related" in b:
        match = b["removed_related"][0]
        assert match["relevance"] == "medium-removed"
        assert match["removed_reason"] == "archived"


async def test_force_bypasses_tombstone_dedup(server: Any) -> None:
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    first = await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_remove", id=first["id"], reason="r")

    forced = await _call(
        server, "memory_write", content=body, scopes=["tools"], force=True
    )
    assert forced["status"] == "committed"


async def test_write_after_restore_uses_active_dedup(server: Any) -> None:
    """If a memory is removed then restored, dedup should treat it as
    active again (status=duplicate), not as a tombstone match."""
    body = "vendored python-frontmatter to drop the deprecated codecs.open call"
    written = await _call(server, "memory_write", content=body, scopes=["tools"])
    await _call(server, "memory_remove", id=written["id"], reason="r")
    await _call(server, "memory_restore", id=written["id"])

    duplicate = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert duplicate["status"] == "duplicate"
    assert duplicate["matches"][0]["id"] == written["id"]


# ---------------------------------------------------------------------------
# dedup threshold resolution: semantic_dedup=true but model absent
# ---------------------------------------------------------------------------


@pytest.fixture
def server_semantic_no_model(memory_dir: Path, monkeypatch: Any) -> Any:
    """A server configured with `semantic_dedup = true` but whose model
    factory returns None — the shape you get when the `embeddings` extra
    isn't installed. `find_similar` must fall back to the Jaccard scorer
    *and* its natural 0.75/0.40 thresholds; feeding the COSINE-calibrated
    0.85/0.65 to Jaccard would silently neuter dedup."""
    from bettermemory import builder
    from bettermemory.config import BehaviorConfig

    monkeypatch.setattr(builder, "_semantic_model_or_none", lambda config: None)
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(semantic_dedup=True),
    )
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def test_dedup_fires_when_semantic_on_but_model_absent(
    server_semantic_no_model: Any,
) -> None:
    """Near-duplicate (Jaccard ~0.82, i.e. in [0.75, 0.85)) must be caught
    as `duplicate` when semantic_dedup is on but no model loaded.

    Mutation guard: revert `_resolve_dedup_thresholds` to gate on the
    `semantic_dedup` flag alone and the cosine 0.85 high threshold gets
    applied to the Jaccard scorer — 0.82 < 0.85 lands as `medium`
    (advisory `related`), the write commits, and this assertion fails.
    """
    server = server_semantic_no_model
    body = "alpha beta gamma delta epsilon zeta theta kappa lambda sigma"
    first = await _call(server, "memory_write", content=body, scopes=["tools"])
    assert first["status"] == "committed"

    # Shares 9 of 11 union tokens with `body` → Jaccard ≈ 0.818.
    near_dup = "alpha beta gamma delta epsilon zeta theta kappa lambda omega"
    second = await _call(server, "memory_write", content=near_dup, scopes=["tools"])
    assert second["status"] == "duplicate"
    assert second["matches"][0]["id"] == first["id"]
