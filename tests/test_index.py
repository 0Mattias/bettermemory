"""Tests for the SQLite FTS5 inverted index (T3.1 of the v1.6 plan).

The index is a derived cache: files canonical, index keeps the
linear-scan ceiling off. Tests cover schema lifecycle, query
correctness, scope filtering, incremental updates via Store hooks,
forward-compat on unknown schema versions, and the absence-as-signal
contract when the file doesn't exist.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bettermemory import index
from bettermemory.models import (
    Confidence,
    LinkType,
    Memory,
    MemoryLink,
    Source,
    generate_ulid,
)
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
    count = index.rebuild(memory_dir, store.iter_active())
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
    index.rebuild(memory_dir, store.iter_active())
    index.rebuild(memory_dir, store.iter_active())
    assert index.status(memory_dir)["indexed_count"] == 2


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def test_query_finds_matching_body(store: Store, memory_dir: Path) -> None:
    """The core happy path: query for a body token returns the
    memories whose body contains that token, ranked by BM25."""
    a = store.write(content="python list comprehension", scopes=["tools"])
    b = store.write(content="kubernetes networking", scopes=["tools"])
    index.rebuild(memory_dir, store.iter_active())

    results = index.query(memory_dir, "python")
    ids = [r[0] for r in results]
    assert a.id in ids
    assert b.id not in ids


def test_query_scope_filter(store: Store, memory_dir: Path) -> None:
    """Scope filter is a strict include filter — only memories
    carrying at least one of the named scopes appear."""
    a = store.write(content="python comprehension", scopes=["tools"])
    b = store.write(content="python comprehension", scopes=["learning-style"])
    index.rebuild(memory_dir, store.iter_active())

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
    index.rebuild(memory_dir, store.iter_active())

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
    index.rebuild(memory_dir, store.iter_active())

    results = index.query(memory_dir, "python", max_results=3)
    assert len(results) == 3


def test_empty_query_returns_empty(store: Store, memory_dir: Path) -> None:
    """An empty or whitespace-only query is a no-op — FTS5 can't
    match on nothing, and the caller shouldn't have to check."""
    store.write(content="anything", scopes=["tools"])
    index.rebuild(memory_dir, store.iter_active())
    assert index.query(memory_dir, "") == []
    assert index.query(memory_dir, "   ") == []


def test_query_escapes_fts_special_chars(store: Store, memory_dir: Path) -> None:
    """A user query containing an FTS5 special character (`:`, `*`,
    `^`, etc.) must not be interpreted as syntax. The escape wraps
    each term in quotes so it's treated as a literal phrase."""
    store.write(content="some text with :colon: chars", scopes=["tools"])
    index.rebuild(memory_dir, store.iter_active())

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


def test_unknown_schema_version_refuses_on_read(tmp_path: Path) -> None:
    """A future version's index file should refuse to load on the READ
    path with the current reader rather than risk misinterpreting rows
    under different semantics. (`rebuild` is the repair path and instead
    RECOVERS — see test_rebuild_recovers_from_newer_schema_version.)"""
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
        # query() `_ensure_schema`s directly and surfaces the version
        # mismatch; status() catches it and reports corrupt=True; rebuild()
        # recovers by dropping + recreating (the two tests below).
        index.query(root, "anything")


def test_rebuild_recovers_from_corrupt_index_file(
    store: Store, memory_dir: Path
) -> None:
    """rebuild() is the documented recovery primitive for a corrupt
    index, so it must succeed against a torn/garbage .db rather than
    crash on it. Pre-fix, `_connect`'s lazy header validation raised
    sqlite3.DatabaseError ("file is not a database") and rebuild
    propagated it as an unhandled traceback — the one command meant to
    repair the index was guaranteed to fail on exactly that input."""
    store.write(content="alpha indexer note", scopes=["tools"])
    store.write(content="beta indexer note", scopes=["tools"])
    # Overwrite the index with garbage — "file is not a database".
    index_file = index.index_path(memory_dir)
    index_file.write_bytes(b"not a sqlite database at all " * 16)
    assert index.status(memory_dir).get("corrupt") is True

    count = index.rebuild(memory_dir, store.iter_active())

    assert count == 2
    s = index.status(memory_dir)
    assert s["exists"] is True
    assert "corrupt" not in s
    assert s["indexed_count"] == 2
    assert s["schema_version"] == index.SCHEMA_VERSION
    # The repaired index actually answers queries.
    assert index.query(memory_dir, "alpha")


def test_rebuild_recovers_from_newer_schema_version(
    store: Store, memory_dir: Path
) -> None:
    """An on-disk schema_version newer than this code makes
    `_ensure_schema` raise IndexVersionError — whose own message tells
    the user to run `bettermemory reindex`. rebuild() must therefore
    recover by dropping + recreating, not crash with the very error it
    instructs the user to resolve with this command."""
    store.write(content="alpha indexer note", scopes=["tools"])
    # Poison the on-disk schema version to a future value.
    index_file = index.index_path(memory_dir)
    conn = sqlite3.connect(str(index_file))
    try:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(index.SCHEMA_VERSION + 1),),
        )
        conn.commit()
    finally:
        conn.close()
    assert index.status(memory_dir).get("corrupt") is True

    count = index.rebuild(memory_dir, store.iter_active())

    assert count == 1
    s = index.status(memory_dir)
    assert "corrupt" not in s
    assert s["schema_version"] == index.SCHEMA_VERSION


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


def test_filenames_for_ids_resolves_collision_suffixed_files(
    store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when two memories share a date+slug, the second
    one lands at `<slug>-<short_id>.md`. The previous implementation
    re-derived the index `filename` column from `(created, slug)`,
    which silently pointed both index rows at the unsuffixed file —
    a search hit on the collision-suffixed memory would resolve to
    the *other* memory's body, then trip the `memory.id != cid`
    guard and get dropped from results. With the threaded-through
    filename, both memories are findable."""
    # Force two writes onto the same date+slug.
    fixed = datetime(2025, 3, 14, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("bettermemory.store.utcnow", lambda: fixed)
    monkeypatch.setattr("bettermemory.models.utcnow", lambda: fixed)

    a = store.write(content="hello world", scopes=["tools"])
    b = store.write(content="hello world", scopes=["tools"])
    assert a.id != b.id

    filenames = index.filenames_for_ids(memory_dir, [a.id, b.id])
    # Both ids resolve to actual files (one base, one suffixed).
    assert set(filenames.keys()) == {a.id, b.id}
    for fn in filenames.values():
        assert (memory_dir / fn).exists(), (
            f"filename {fn!r} stored in index doesn't exist on disk"
        )
    # The two filenames must be distinct — the bug was both pointing
    # at the unsuffixed sibling.
    assert filenames[a.id] != filenames[b.id]


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


def test_duplicate_typed_link_notes_round_trip(memory_dir: Path) -> None:
    """Two on-disk links sharing `(type, target_id)` but carrying
    different notes must BOTH survive in the reverse-link index.

    Regression: `_sync_links` did `INSERT OR IGNORE` into a
    `memory_links` table keyed on `(source_id, type, target_id)`, so the
    second note collided and was silently dropped — the in-memory
    reverse index lost a note the canonical file keeps. The on-disk
    `links` list is a plain list with no dedup validator, so a memory
    can legitimately hold two `extends` edges to the same target with
    distinct notes; the schema-v3 key includes `note`, so both rows
    persist and `links_for` mirrors disk.

    Driven through `index.upsert` directly (not `store.update`) because
    the divergence the audit describes is exactly the index-vs-disk one:
    a link list that reaches the index from a hand-edited file. The
    model layer does NOT dedup links — `Memory._check_links` only
    rejects self-links and caps the list at 64 — so the index mirror is
    solely responsible for collapsing exact duplicates while keeping
    distinct-note rows.
    """
    target = generate_ulid()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    src = Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="body\n",
        links=[
            MemoryLink(type=LinkType.EXTENDS, target_id=target, note="first note"),
            MemoryLink(type=LinkType.EXTENDS, target_id=target, note="second note"),
        ],
    )
    index.upsert(memory_dir, src, filename="src.md")

    outbound, _inbound = index.links_for(memory_dir, src.id)
    notes = sorted(note or "" for (_type, _tid, note) in outbound)
    assert notes == ["first note", "second note"]
    # Both edges share the same type and target — only the note differs.
    assert all(t == "extends" and tid == target for (t, tid, _n) in outbound)

    # The duplicate is preserved from the target's inbound side too.
    _out_t, inbound_t = index.links_for(memory_dir, target)
    in_notes = sorted(note or "" for (_type, _sid, note) in inbound_t)
    assert in_notes == ["first note", "second note"]


def test_exact_duplicate_no_note_links_collapse(memory_dir: Path) -> None:
    """Two IDENTICAL no-note links of the same type collapse to exactly
    one row, while two same-`(type, target_id)` links with DISTINCT
    notes still yield two rows.

    Regression for the schema-v3 widening (PK gained `note`): SQLite
    treats NULL as DISTINCT in a primary key, and `MemoryLink.note`
    defaults to None — the common case — so under a bare `INSERT OR
    IGNORE` two exact-duplicate note=NULL links each satisfied the PK
    and produced two identical reverse-link rows (v2 collapsed them to
    one). The model layer does not dedup (`_check_links` only rejects
    self-links and caps at 64), so `_sync_links` must pre-dedup over the
    full key tuple. This pins both halves of the invariant: NULL-note
    exact duplicates collapse (matching v2), distinct notes survive.
    """
    target = generate_ulid()
    target_two = generate_ulid()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    src = Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="body\n",
        links=[
            # Two exact-duplicate no-note links (note defaults to None) —
            # must collapse to one row.
            MemoryLink(type=LinkType.EXTENDS, target_id=target),
            MemoryLink(type=LinkType.EXTENDS, target_id=target),
            # Two same-(type, target) links with distinct notes — must
            # both survive.
            MemoryLink(type=LinkType.EXTENDS, target_id=target_two, note="x"),
            MemoryLink(type=LinkType.EXTENDS, target_id=target_two, note="y"),
        ],
    )
    index.upsert(memory_dir, src, filename="src.md")

    outbound, _inbound = index.links_for(memory_dir, src.id)
    # Three rows total: one collapsed no-note edge + two distinct-note edges.
    assert len(outbound) == 3, f"expected 3 link rows, got {outbound!r}"

    # The no-note duplicate collapsed to exactly one row.
    no_note_to_target = [
        (t, tid, n)
        for (t, tid, n) in outbound
        if tid == target and t == "extends" and n is None
    ]
    assert len(no_note_to_target) == 1, (
        f"exact-duplicate note=NULL links did not collapse: {no_note_to_target!r}"
    )

    # The distinct-note pair both survived.
    distinct_notes = sorted(
        n for (t, tid, n) in outbound if tid == target_two and n is not None
    )
    assert distinct_notes == ["x", "y"], (
        f"distinct-note links were not both preserved: {distinct_notes!r}"
    )

    # The collapse is visible from the target's inbound (reverse_links) side
    # too — exactly one inbound row, not two.
    _out_t, inbound_t = index.links_for(memory_dir, target)
    assert len(inbound_t) == 1, (
        f"expected exactly one reverse_links entry for the collapsed "
        f"no-note duplicate, got {inbound_t!r}"
    )


def test_rebuild_preserves_duplicate_typed_link_notes(memory_dir: Path) -> None:
    """The full-rebuild path mirrors disk the same way the incremental
    upsert does — both notes of a duplicate-typed link survive a
    `rebuild` from the on-disk truth."""
    target = generate_ulid()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    src = Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body="body\n",
        links=[
            MemoryLink(type=LinkType.EXTENDS, target_id=target, note="alpha"),
            MemoryLink(type=LinkType.EXTENDS, target_id=target, note="beta"),
        ],
    )
    index.rebuild(memory_dir, [(Path("src.md"), src)])

    outbound, _inbound = index.links_for(memory_dir, src.id)
    notes = sorted(note or "" for (_type, _tid, note) in outbound)
    assert notes == ["alpha", "beta"]


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
    index.rebuild(memory_dir, store.iter_active())
    s_after = index.status(memory_dir)
    assert s_after["indexed_count"] >= 1
    # And the H1 surfaces work again on the reindexed store.
    filenames = index.filenames_for_ids(memory_dir, [a.id])
    assert a.id in filenames


def test_schema_rebuild_executescript_is_transactional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the 2.6.4 audit. `_ensure_schema`'s v1→v2
    rebuild must keep the DROP + CREATE atomic — a failure on the
    CREATE side must roll the DROP back so the on-disk memories
    table survives.

    The 2.6.4 bug was `conn.execute("BEGIN IMMEDIATE")` followed by
    a separate `conn.executescript(...)`: `executescript` implicitly
    commits any pending transaction before it runs, so the BEGIN
    wrapped nothing and the DROP would be auto-committed by the time
    the CREATE failed. The 2.6.5 fix moves BEGIN/COMMIT inside the
    executescript string.

    Exercises the production code path (not a stdlib-property pin):
    sets up a v1 index with a row, injects a broken `_SCHEMA` so the
    v1→v2 rebuild's CREATE phase fails, calls `_ensure_schema`, and
    asserts the row survives. A regression to the 2.6.4 bug shape
    would commit the DROP and lose the row; the fix preserves it.
    """
    db = tmp_path / "t.sqlite"
    conn = sqlite3.connect(db)
    try:
        # Initial setup: build a current-schema index with one row,
        # then force the meta version backwards so the next
        # `_ensure_schema` call enters the v1→v2 migration branch.
        index._ensure_schema(conn)
        conn.execute(
            "INSERT INTO memories(id, created, updated, confidence, body, "
            "scopes_text, scopes_json) VALUES "
            "('keep', '2026-01-01', '2026-01-01', 'fact', 'b', 's', '[]')"
        )
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    # Inject a broken `_SCHEMA` so the v1→v2 rebuild's CREATE phase
    # fails after the DROPs run. A non-atomic rebuild commits the
    # DROP and loses the row; the atomic fix rolls it back.
    monkeypatch.setattr(index, "_SCHEMA", "CREATE TABLE broken (broken syntax;")

    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.Error):
            index._ensure_schema(conn)
    finally:
        conn.close()

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT id FROM memories").fetchone()
    finally:
        conn.close()
    assert row == ("keep",), (
        "v1→v2 rebuild left the on-disk memories table without its "
        "original row — the DROP was committed before the failure, "
        "exactly the 2.6.4 bug shape"
    )


# ---------------------------------------------------------------------------
# Startup divergence check (S4)
#
# Out-of-band `.md` writes (external editor, `sync pull`, a sub-agent
# using the generic `Write` tool on a memory file path) silently desync
# the FTS5 index from disk. `Store.__post_init__` runs a one-shot
# divergence check at construction and emits a single WARNING per
# `(root,)` so the operator can `bettermemory reindex` before the stale
# index cascades into a wrong answer. The four tests below pin the
# four interesting cases: aligned (silent), diverged (warns), repeated
# construction on the same root (one warning), and two distinct roots
# (one warning each).
# ---------------------------------------------------------------------------


def test_aligned_store_construction_emits_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The happy path: a store whose disk and index counts agree
    constructs silently. Every memory was written through the Store
    API, so the index and disk are in lockstep — no warning fires."""
    from bettermemory import store as _store

    # Use a tmp_path that the module-level guard set hasn't seen.
    root = tmp_path / "aligned"
    setup = Store(root)
    setup.write(content="alpha", scopes=["tools"])
    setup.write(content="beta", scopes=["tools"])

    # Clear the warned-roots set so a second construction on the same
    # root would warn if the counts didn't match. (They do, so it
    # shouldn't warn — that's the assertion.)
    _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)
    divergence_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and ("out-of-sync" in r.getMessage() or "corrupt" in r.getMessage())
    ]
    assert not divergence_warnings, (
        f"aligned store should construct silently, got: {divergence_warnings!r}"
    )


def test_diverged_store_construction_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A `.md` file landed on disk via a path other than the Store API
    (here: a direct `write_text` simulating `sync pull` / an external
    editor / a sub-agent using the generic Write tool). On the next
    Store construction the divergence check fires and surfaces the
    actionable `bettermemory reindex` hint."""
    from bettermemory import store as _store

    root = tmp_path / "diverged"
    # Seed an index that's in sync with one existing memory, then
    # add a second file out-of-band so the index says 1 and disk
    # says 2 — the canonical out-of-sync shape S4 catches.
    setup = Store(root)
    setup.write(content="indexed via store", scopes=["tools"])

    # Reset the warned-roots set so the next construction is fresh.
    _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())

    out_of_band = root / "2026-01-01-external-write.md"
    out_of_band.write_text(
        "---\n"
        "schema_version: 1\n"
        "id: 01HXYZAAAAAAAAAAAAAAAAAAAA\n"
        "created: 2026-01-01T00:00:00Z\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "body written outside the Store API\n",
        encoding="utf-8",
    )

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)
    divergence_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "out-of-sync" in r.getMessage()
    ]
    assert len(divergence_warnings) == 1, (
        f"expected exactly one out-of-sync WARNING, got {divergence_warnings!r}"
    )
    message = divergence_warnings[0].getMessage()
    assert "bettermemory reindex" in message, (
        f"warning must surface the actionable reindex hint, got: {message!r}"
    )
    assert "index=1" in message and "disk=2" in message, (
        f"warning must report the actual counts, got: {message!r}"
    )


def test_divergence_warning_fires_only_once_per_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Construct two Stores on the same diverged root. The first
    construction must warn; the second must stay silent — the
    one-shot guard keeps the log clean for the `Store(root).write(...)`
    one-liner pattern and the many-Stores-per-root concurrency
    tests."""
    from bettermemory import store as _store

    root = tmp_path / "one_shot"
    setup = Store(root)
    setup.write(content="indexed via store", scopes=["tools"])
    _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())

    # Drop in an out-of-band file so the index is now stale.
    (root / "2026-01-01-second-external.md").write_text(
        "---\n"
        "schema_version: 1\n"
        "id: 01HXYZBBBBBBBBBBBBBBBBBBBB\n"
        "created: 2026-01-01T00:00:00Z\n"
        "updated: 2026-01-01T00:00:00Z\n"
        "scopes:\n  - tools\n"
        "confidence: medium\n"
        "source: explicit-statement\n"
        "---\n"
        "second out-of-band body\n",
        encoding="utf-8",
    )

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)  # warns
        Store(root)  # silent
        Store(root)  # silent
    divergence_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "out-of-sync" in r.getMessage()
    ]
    assert len(divergence_warnings) == 1, (
        f"expected exactly one warning across three constructions on the "
        f"same diverged root, got {divergence_warnings!r}"
    )


def test_divergence_warning_is_independent_per_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two distinct diverged roots each get their own warning — the
    one-shot guard is per-root, not global. Otherwise a process that
    serves multiple memory directories (testing, a long-lived
    `bettermemory ui` instance pointing at swapped configs) would
    suppress the second root's warning silently."""
    from bettermemory import store as _store

    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    for root in (root_a, root_b):
        setup = Store(root)
        setup.write(content="indexed", scopes=["tools"])
        _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())
        (root / "2026-01-01-extra.md").write_text(
            "---\n"
            "schema_version: 1\n"
            f"id: 01HXYZCCCCCCCCCCCCCCCCCC{('AA' if root is root_a else 'BB')}\n"
            "created: 2026-01-01T00:00:00Z\n"
            "updated: 2026-01-01T00:00:00Z\n"
            "scopes:\n  - tools\n"
            "confidence: medium\n"
            "source: explicit-statement\n"
            "---\n"
            "out-of-band body\n",
            encoding="utf-8",
        )

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root_a)
        Store(root_b)
    divergence_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "out-of-sync" in r.getMessage()
    ]
    assert len(divergence_warnings) == 2, (
        f"each distinct root must get its own warning, got {divergence_warnings!r}"
    )


def test_corrupt_index_emits_divergence_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A garbage / truncated index file still surfaces the divergence
    warning. `index.status()` reports `corrupt=True` in that case;
    the indexed count is unknowable but the fix is the same — rerun
    `bettermemory reindex`."""
    from bettermemory import index, store as _store

    root = tmp_path / "corrupt"
    setup = Store(root)
    setup.write(content="seed", scopes=["tools"])

    # Stomp the on-disk index with garbage so `status()` reports it
    # as corrupt on the next read.
    db_path = index.index_path(root)
    db_path.write_bytes(b"this is not a sqlite database")

    _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "corrupt" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        f"a corrupt index should produce exactly one corruption "
        f"warning, got {warnings!r}"
    )
    assert "bettermemory reindex" in warnings[0].getMessage()


def test_rebuild_recovery_closes_connection_on_compound_failure(
    store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compound failure: a version-skewed index makes the first
    _ensure_schema raise IndexVersionError (-> recovery unlinks + reopens),
    then the POST-unlink _ensure_schema ALSO fails (a disk error). The
    reopened connection must still be closed — no leaked sqlite handle
    (the `ResourceWarning: unclosed database` contract _connect's docstring
    pins). Detect closure directly: a closed sqlite3 connection raises
    ProgrammingError on execute. Regression introduced + fixed in the
    post-3.6.0 sweep (_open_for_rebuild)."""
    store.write(content="alpha indexer note", scopes=["tools"])
    index_file = index.index_path(memory_dir)
    # Poison the on-disk schema version so the FIRST _ensure_schema raises.
    poison = sqlite3.connect(str(index_file))
    try:
        poison.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(index.SCHEMA_VERSION + 1),),
        )
        poison.commit()
    finally:
        poison.close()

    opened: list[sqlite3.Connection] = []
    real_connect = index._connect

    def tracking_connect(path: Path) -> sqlite3.Connection:
        conn = real_connect(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(index, "_connect", tracking_connect)

    real_ensure = index._ensure_schema
    calls = {"n": 0}

    def flaky_ensure(conn: sqlite3.Connection) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            real_ensure(conn)  # raises IndexVersionError on the skewed index
        else:
            raise sqlite3.OperationalError("simulated disk I/O error")

    monkeypatch.setattr(index, "_ensure_schema", flaky_ensure)

    with pytest.raises(sqlite3.OperationalError):
        index.rebuild(memory_dir, store.iter_active())

    assert opened, "expected the recovery path to open at least one connection"
    for conn in opened:
        # A closed connection raises ProgrammingError; a leaked open one
        # would succeed here.
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_rebuild_unlink_failure_leaves_main_db_intact(
    store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If removing a -wal/-shm sibling fails mid-recovery (Windows lock /
    EACCES), recovery must NOT leave the main .db deleted with an orphaned
    WAL — a worse state than before for a repair primitive.
    _unlink_index_files removes siblings BEFORE the main .db, so a
    single-point failure on -wal leaves the .db intact and the error
    surfaces cleanly (retryable). Regression introduced + fixed in the
    post-3.6.0 sweep."""
    store.write(content="alpha indexer note", scopes=["tools"])
    index_file = index.index_path(memory_dir)
    wal = index_file.with_suffix(index_file.suffix + "-wal")
    wal.write_bytes(b"stale wal bytes")  # a -wal to trip on
    # Corrupt the .db so recovery (unlink) is triggered via _connect.
    index_file.write_bytes(b"not a sqlite database at all " * 4)

    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *a: object, **k: object) -> None:
        if self.name.endswith("-wal"):
            raise PermissionError(13, "file is locked")
        return real_unlink(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with pytest.raises(PermissionError):
        index.rebuild(memory_dir, store.iter_active())

    # -wal is unlinked first and fails, so the main .db is never removed:
    # consistent, retryable state — not a .db-gone / WAL-orphaned mess.
    assert index_file.exists()
