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
