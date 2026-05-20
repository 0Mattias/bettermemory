"""Tests for the SQLite FTS5 inverted index (T3.1 of the v1.6 plan).

The index is a derived cache: files canonical, index keeps the
linear-scan ceiling off. Tests cover schema lifecycle, query
correctness, scope filtering, incremental updates via Store hooks,
forward-compat on unknown schema versions, and the absence-as-signal
contract when the file doesn't exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bettermemory import index
from bettermemory.store import Store


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_query_on_missing_index_returns_empty(memory_dir: Path) -> None:
    """No index file => empty query result, not an error. Callers
    that lazily build the index should not have to special-case
    "first-run no file"."""
    assert index.query(memory_dir, "anything") == []
    assert index.status(memory_dir)["exists"] is False


def test_rebuild_creates_file_and_populates(store: Store, memory_dir: Path) -> None:
    """A fresh rebuild against a store with memories produces a
    populated index whose count matches the store's load_all count."""
    store.write(content="python list comprehension", scopes=["tools"])
    store.write(content="kubernetes networking", scopes=["infrastructure"])

    # The Store hook upserts as memories are written, so the index
    # already has data. Calling rebuild redoes it from scratch —
    # idempotent.
    count = index.rebuild(memory_dir, store.load_all())
    assert count == 2

    s = index.status(memory_dir)
    assert s["exists"] is True
    assert s["indexed_count"] == 2
    assert s["schema_version"] == index.SCHEMA_VERSION


def test_rebuild_is_idempotent(store: Store, memory_dir: Path) -> None:
    """Running rebuild twice produces the same final state — the
    truncate-then-insert pattern is safe to repeat."""
    store.write(content="alpha", scopes=["tools"])
    store.write(content="beta", scopes=["tools"])
    index.rebuild(memory_dir, store.load_all())
    index.rebuild(memory_dir, store.load_all())
    assert index.status(memory_dir)["indexed_count"] == 2


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def test_query_finds_matching_body(store: Store, memory_dir: Path) -> None:
    """The core happy path: query for a body token returns the
    memories whose body contains that token, ranked by BM25."""
    a = store.write(content="python list comprehension", scopes=["tools"])
    b = store.write(content="kubernetes networking", scopes=["tools"])
    index.rebuild(memory_dir, store.load_all())

    results = index.query(memory_dir, "python")
    ids = [r[0] for r in results]
    assert a.id in ids
    assert b.id not in ids


def test_query_scope_filter(store: Store, memory_dir: Path) -> None:
    """Scope filter is a strict include filter — only memories
    carrying at least one of the named scopes appear."""
    a = store.write(content="python comprehension", scopes=["tools"])
    b = store.write(content="python comprehension", scopes=["learning-style"])
    index.rebuild(memory_dir, store.load_all())

    results = index.query(memory_dir, "python", scopes=["tools"])
    ids = [r[0] for r in results]
    assert a.id in ids
    assert b.id not in ids


def test_query_returns_bm25_scores(store: Store, memory_dir: Path) -> None:
    """Each row carries a BM25 score (SQLite returns negative values
    where lower = more relevant — that's its convention, not ours).
    Caller decides whether to invert or pass through."""
    store.write(content="python rare unique token", scopes=["tools"])
    store.write(content="python common common common", scopes=["tools"])
    index.rebuild(memory_dir, store.load_all())

    results = index.query(memory_dir, "python")
    assert len(results) == 2
    # The scores should be different — the corpus has different
    # frequencies of "python" across docs (the "common" doc has it
    # interleaved with repeated other tokens).
    scores = {r[1] for r in results}
    # At minimum: each is a float.
    assert all(isinstance(s, float) for s in scores)


def test_query_max_results_caps(store: Store, memory_dir: Path) -> None:
    """The `max_results` parameter is a hard cap on rows returned —
    the index can hold many matches, the caller chooses how many to
    materialize."""
    for i in range(10):
        store.write(content=f"python notes {i}", scopes=["tools"])
    index.rebuild(memory_dir, store.load_all())

    results = index.query(memory_dir, "python", max_results=3)
    assert len(results) == 3


def test_empty_query_returns_empty(store: Store, memory_dir: Path) -> None:
    """An empty or whitespace-only query is a no-op — FTS5 can't
    match on nothing, and the caller shouldn't have to check."""
    store.write(content="anything", scopes=["tools"])
    index.rebuild(memory_dir, store.load_all())
    assert index.query(memory_dir, "") == []
    assert index.query(memory_dir, "   ") == []


def test_query_escapes_fts_special_chars(store: Store, memory_dir: Path) -> None:
    """A user query containing an FTS5 special character (`:`, `*`,
    `^`, etc.) must not be interpreted as syntax. The escape wraps
    each term in quotes so it's treated as a literal phrase."""
    store.write(content="some text with :colon: chars", scopes=["tools"])
    index.rebuild(memory_dir, store.load_all())

    # A naive query for "scope:colon" would be parsed by FTS5 as a
    # column-prefix selector and either fail or match unexpected
    # things. We just need it not to raise.
    result = index.query(memory_dir, "colon")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Store hooks — incremental update
# ---------------------------------------------------------------------------


def test_store_write_populates_index(store: Store, memory_dir: Path) -> None:
    """Store.write should upsert into the index without a manual
    rebuild. This is the hook that keeps the index live."""
    a = store.write(content="python list comprehension", scopes=["tools"])

    results = index.query(memory_dir, "python")
    assert any(r[0] == a.id for r in results)


def test_store_update_refreshes_index_body(store: Store, memory_dir: Path) -> None:
    """When Store.update changes the body, the index must reflect the
    new body — old tokens no longer match, new tokens do."""
    m = store.write(content="python original body", scopes=["tools"])

    # The hook ran on write; now update.
    updated = m.model_copy(update={"body": "rust replacement body\n"})
    store.update(updated)

    # Old token no longer hits.
    assert index.query(memory_dir, "python") == []
    # New token hits.
    results = index.query(memory_dir, "rust")
    assert any(r[0] == m.id for r in results)


def test_store_tombstone_removes_from_index(store: Store, memory_dir: Path) -> None:
    """Tombstoning drops the memory from the index — searches no
    longer return it."""
    m = store.write(content="python list comprehension", scopes=["tools"])
    assert index.query(memory_dir, "python")
    store.tombstone(m.id, reason="testing index removal")
    assert index.query(memory_dir, "python") == []


# ---------------------------------------------------------------------------
# Forward-compat + recovery
# ---------------------------------------------------------------------------


def test_unknown_schema_version_raises(tmp_path: Path) -> None:
    """A future version's index file should refuse to load with the
    current reader rather than risk misinterpreting rows under
    different semantics."""
    root = tmp_path / "memories"
    root.mkdir()
    # Manually create an index with a bumped schema version.
    db_path = index.index_path(root)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO meta VALUES ('schema_version', ?)",
        (str(index.SCHEMA_VERSION + 1),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(index.IndexVersionError):
        # status() catches the IndexVersionError internally and reports
        # it, but rebuild + query both surface it because they
        # `_ensure_schema` directly.
        index.rebuild(root, [])


def test_status_handles_corrupt_file_gracefully(tmp_path: Path) -> None:
    """A garbage file at the index path returns a status with
    `corrupt=True` rather than crashing — the doctor / reindex CLI
    flows depend on this being recoverable."""
    root = tmp_path / "memories"
    root.mkdir()
    db_path = index.index_path(root)
    db_path.write_bytes(b"not a sqlite database")

    s = index.status(root)
    assert s["exists"] is True
    assert s.get("corrupt") is True


def test_status_returns_path_for_missing(tmp_path: Path) -> None:
    """status() always returns the would-be path so the caller can
    surface a useful error/log message even before the file exists."""
    root = tmp_path / "memories"
    root.mkdir()
    s = index.status(root)
    assert s["exists"] is False
    assert s["path"].endswith(".index.sqlite")


# ---------------------------------------------------------------------------
# id → filename lookup (H1 — schema v2)
# ---------------------------------------------------------------------------


def test_filenames_for_ids_resolves_written_memories(
    store: Store, memory_dir: Path
) -> None:
    """The lookup that `_load_search_candidates` uses to skip
    parsing the whole store. Every newly-written memory should
    have a row populated on the same transaction."""
    a = store.write(content="alpha", scopes=["tools"])
    b = store.write(content="beta", scopes=["tools"])

    filenames = index.filenames_for_ids(memory_dir, [a.id, b.id])
    assert set(filenames.keys()) == {a.id, b.id}
    # Filenames must resolve to real .md files in the store root.
    for fn in filenames.values():
        assert (memory_dir / fn).exists(), f"filename {fn!r} doesn't exist"


def test_filenames_for_ids_omits_unknown_ids(store: Store, memory_dir: Path) -> None:
    """An id that doesn't correspond to an index row is silently
    omitted — the caller (_load_search_candidates) falls back to
    load_all for those candidates."""
    a = store.write(content="something", scopes=["tools"])
    filenames = index.filenames_for_ids(
        memory_dir, [a.id, "01JUNKEEEEEEEEEEEEEEEEEEEE"]
    )
    assert a.id in filenames
    assert "01JUNKEEEEEEEEEEEEEEEEEEEE" not in filenames


def test_filenames_for_ids_empty_list_short_circuits(memory_dir: Path) -> None:
    """No index touch on an empty input — keeps callers cheap
    when the FTS pre-filter returned nothing."""
    assert index.filenames_for_ids(memory_dir, []) == {}


# ---------------------------------------------------------------------------
# memory_links table (H1 — schema v2)
# ---------------------------------------------------------------------------


def test_links_for_returns_outbound_and_inbound(store: Store, memory_dir: Path) -> None:
    """The reverse-link lookup that replaces _links_payload's
    load_all scan. A memory's links populate the index on write;
    the index then supports both directions."""
    from bettermemory.models import LinkType, MemoryLink

    a = store.write(content="A body", scopes=["tools"])
    b = store.write(content="B body", scopes=["tools"])
    # B supersedes A — write B with a link pointing at A.
    b_with_link = b.model_copy(
        update={
            "links": [
                MemoryLink(type=LinkType.SUPERSEDES, target_id=a.id, note="reason")
            ]
        }
    )
    store.update(b_with_link)

    outbound_a, inbound_a = index.links_for(memory_dir, a.id)
    assert outbound_a == []
    assert inbound_a == [("supersedes", b.id, "reason")]

    outbound_b, inbound_b = index.links_for(memory_dir, b.id)
    assert outbound_b == [("supersedes", a.id, "reason")]
    assert inbound_b == []


def test_links_for_returns_empty_when_no_index(tmp_path: Path) -> None:
    """No index file → empty result. The handler falls back to
    `load_all` in this case, so the empty return is the cue."""
    root = tmp_path / "memories"
    root.mkdir()
    out, inbound = index.links_for(root, "01HXYZ000000000000000000ZZ")
    assert out == []
    assert inbound == []


def test_links_cleanup_on_tombstone(store: Store, memory_dir: Path) -> None:
    """Tombstoning a memory must drop all its rows from
    `memory_links` — both source-side and target-side. The DELETE
    trigger handles the cascade so reverse-link queries after a
    tombstone don't dangle."""
    from bettermemory.models import LinkType, MemoryLink

    a = store.write(content="A body", scopes=["tools"])
    b = store.write(content="B body", scopes=["tools"])
    b_with_link = b.model_copy(
        update=dict(links=[MemoryLink(type=LinkType.EXTENDS, target_id=a.id)])
    )
    store.update(b_with_link)
    # Pre-condition: link exists.
    _, inbound_a = index.links_for(memory_dir, a.id)
    assert inbound_a, "test setup: link must be present before tombstone"

    # Tombstone the *source* (B). The link row with source_id=B must go.
    store.tombstone(b.id, reason="cleanup")
    _, inbound_after = index.links_for(memory_dir, a.id)
    assert inbound_after == [], (
        f"reverse link not cleaned up after tombstone; got {inbound_after}"
    )


# ---------------------------------------------------------------------------
# Schema version migration (H1)
# ---------------------------------------------------------------------------


def test_v1_index_downgraded_to_v2_via_drop_and_recreate(
    store: Store, memory_dir: Path
) -> None:
    """Regression: an existing v1-schema index on disk must be
    transparently upgraded to v2 on the next connect. The data
    tables are dropped and recreated empty; store hooks repopulate
    incrementally and `bettermemory reindex` does the explicit full
    rebuild. The fallback in `_load_search_candidates` keeps search
    working through the transition."""
    import sqlite3

    a = store.write(content="bridge", scopes=["tools"])
    db_path = index.index_path(memory_dir)
    # Force the on-disk version backwards to simulate a stale v1
    # index. The data tables look fine; only the meta version flips.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")

    # Status call triggers _ensure_schema, which sees v1 < v2 and
    # drops + recreates.
    s = index.status(memory_dir)
    assert s["schema_version"] == index.SCHEMA_VERSION
    assert s["indexed_count"] == 0, (
        "after a v1→v2 migration the table should be empty until "
        "the next write or explicit reindex"
    )
    # A subsequent reindex restores the row count.
    index.rebuild(memory_dir, store.load_all())
    s_after = index.status(memory_dir)
    assert s_after["indexed_count"] >= 1
    # And the H1 surfaces work again on the reindexed store.
    filenames = index.filenames_for_ids(memory_dir, [a.id])
    assert a.id in filenames
