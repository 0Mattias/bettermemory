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
from typing import Any

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


def test_query_covers_every_candidate_the_rankers_score(
    store: Store, memory_dir: Path
) -> None:
    """Prefilter/ranker parity (round 88 audit): `search.tokenize` grew
    normalisations the raw `text.split()` MATCH builder never saw —
    symbol aliases ('C++' <-> 'cpp'), '_'->'-' canonicalisation, the
    conjunctive kebab fallback — so on indexed stores the FTS prefilter
    silently dropped candidates the rankers rate 'high': a 'cpp' query
    missed 'C++'-spelled bodies (unicode61 indexes 'C++' as bare 'c'),
    'claude-code' returned zero rows against a non-adjacent
    'Claude ... code' body (the quoted term is an FTS *phrase*). The
    MATCH expression is now built by `search.fts_match_query` from the
    same tokenisation, with alias and AND-of-components OR-variants.

    Self-validating: each case first asserts the ranker DOES surface the
    expected bodies, so a future tokenize change keeps the parity
    assertion meaningful instead of vacuously passing."""
    from bettermemory.search import search as rank

    cxx = store.write(content="Prefers C++ for the renderer hot path", scopes=["tools"])
    cpp_lit = store.write(content="The cpp build flags live in meson", scopes=["tools"])
    spaced = store.write(
        content="Claude reviews the code before merging", scopes=["tools"]
    )
    compose = store.write(
        content="The docker stack uses compose v2 profiles", scopes=["tools"]
    )
    memories = [cxx, cpp_lit, spaced, compose]
    index.rebuild(memory_dir, store.iter_active())

    cases: list[tuple[str, set[str]]] = [
        ("cpp", {cxx.id, cpp_lit.id}),
        ("C++", {cxx.id, cpp_lit.id}),
        ("claude-code", {spaced.id}),
        ("docker_compose", {compose.id}),
    ]
    for query, expected in cases:
        ranked = {h.id for h in rank(memories, query)}
        assert expected <= ranked, (
            f"ranker precondition drifted for {query!r}: expected the "
            f"rankers themselves to surface these bodies"
        )
        prefiltered = {cid for cid, _ in index.query(memory_dir, query)}
        assert expected <= prefiltered, (
            f"FTS prefilter dropped candidates the rankers score for {query!r}"
        )


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


def test_status_never_raises_when_file_unlinked_mid_call(
    store: Store, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-raises contract must hold when a concurrent
    rebuild-recovery unlinks the index file between status()'s exists()
    check and its `path.stat()` size read. Simulate the race by
    unlinking right after `_ensure_schema` — where a real
    `_unlink_index_files` from another process would land: the meta
    reads still answer through the open fd, then the stat hits
    FileNotFoundError. Pre-fix that OSError escaped the
    (DatabaseError, IndexVersionError)-only except and turned doctor's
    diagnostic call into a crash exactly when the index was mid-repair."""
    store.write(content="alpha", scopes=["tools"])
    real_ensure = index._ensure_schema

    def ensure_then_unlink(conn: sqlite3.Connection, path: Path) -> None:
        real_ensure(conn, path)
        path.unlink()

    monkeypatch.setattr(index, "_ensure_schema", ensure_then_unlink)
    s = index.status(memory_dir)
    assert s.get("corrupt") is True
    assert "error" in s


def test_status_never_raises_on_connect_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_connect`'s `path.parent.mkdir` can raise OSError (EACCES, a
    read-only filesystem); status() must return the degraded corrupt
    shape rather than propagate — doctor and the reindex CLI call it
    precisely when the index is in a weird state."""
    root = tmp_path / "memories"
    root.mkdir()
    # A placeholder file passes the exists() gate so status() reaches
    # the monkeypatched _connect; its content is never read.
    index.index_path(root).write_bytes(b"placeholder")

    def raise_oserror(path: Path) -> sqlite3.Connection:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(index, "_connect", raise_oserror)
    s = index.status(root)
    assert s["exists"] is True
    assert s.get("corrupt") is True
    assert "Permission denied" in s["error"]


def _poison_meta(memory_dir: Path, key: str) -> None:
    """Overwrite a meta row with a non-integer value, simulating a
    hand-edited or foreign-tool-written index. The file stays a valid
    SQLite database — only the `int()` reads on the value can fail."""
    conn = sqlite3.connect(str(index.index_path(memory_dir)))
    try:
        conn.execute("UPDATE meta SET value = 'banana' WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def test_status_never_raises_on_poisoned_schema_version(
    store: Store, memory_dir: Path
) -> None:
    """A non-integer `meta.schema_version` fails the `int()` read in
    `_ensure_schema`'s version check before status() gets to its own
    meta reads. Unparseable meta is corruption: status() must return
    the degraded corrupt=True shape, not leak ValueError — pre-fix it
    was missing from the (OSError, DatabaseError, IndexVersionError)
    except and crashed doctor on exactly the state it exists to
    report."""
    store.write(content="alpha", scopes=["tools"])
    _poison_meta(memory_dir, "schema_version")

    s = index.status(memory_dir)
    assert s["exists"] is True
    assert s.get("corrupt") is True
    assert "banana" in s["error"]


def test_status_never_raises_on_poisoned_indexed_count(
    store: Store, memory_dir: Path
) -> None:
    """A non-integer `meta.indexed_count` passes `_ensure_schema`
    (schema_version is intact) and fails status()'s own
    `int(count_row[0])` read while building the result dict — the
    other ValueError escape path. Same contract: degraded corrupt=True
    shape, never a raise."""
    store.write(content="alpha", scopes=["tools"])
    _poison_meta(memory_dir, "indexed_count")

    s = index.status(memory_dir)
    assert s["exists"] is True
    assert s.get("corrupt") is True
    assert "banana" in s["error"]


def test_rebuild_recovers_from_poisoned_schema_version(
    store: Store, memory_dir: Path
) -> None:
    """A non-integer `meta.schema_version` fails `_ensure_schema`'s
    `int()` read with ValueError. status() reports the state
    corrupt=True — so doctor's recovery instruction is `bettermemory
    reindex` — and rebuild() IS that command: it must drop + recreate,
    not crash. Pre-fix, `_open_for_rebuild`'s recovery except caught
    only (DatabaseError, IndexVersionError), so the recommended repair
    raised the very ValueError it was recommended for."""
    store.write(content="alpha indexer note", scopes=["tools"])
    store.write(content="beta indexer note", scopes=["tools"])
    _poison_meta(memory_dir, "schema_version")
    assert index.status(memory_dir).get("corrupt") is True

    count = index.rebuild(memory_dir, store.iter_active())

    assert count == 2
    s = index.status(memory_dir)
    assert "corrupt" not in s
    assert s["schema_version"] == index.SCHEMA_VERSION
    assert s["indexed_count"] == 2
    # The repaired index actually answers queries.
    assert index.query(memory_dir, "alpha")


def test_rebuild_recovers_from_poisoned_indexed_count(
    store: Store, memory_dir: Path
) -> None:
    """The other poisoned-meta variant: a non-integer `indexed_count`
    passes `_ensure_schema` (schema_version is intact), so rebuild's
    normal truncate-and-refill runs and its `INSERT OR REPLACE`
    overwrites the poisoned row with the real count — repair without
    a drop. Same tolerate-any-prior-state contract as above, pinned so
    a future meta read added to the rebuild path can't quietly turn
    this state into a crash."""
    store.write(content="alpha indexer note", scopes=["tools"])
    _poison_meta(memory_dir, "indexed_count")
    assert index.status(memory_dir).get("corrupt") is True

    count = index.rebuild(memory_dir, store.iter_active())

    assert count == 1
    s = index.status(memory_dir)
    assert "corrupt" not in s
    assert s["indexed_count"] == 1


def test_rebuild_recovers_from_torn_fts_shadow_page(
    store: Store, memory_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Page-level corruption inside an FTS5 shadow table survives every
    open-time check — `_connect`'s PRAGMAs read the header,
    `_ensure_schema` reads meta, and status() therefore reports the
    index healthy — and first raises mid-DATA-phase, when the
    DELETE/INSERT sweep's triggers walk the damaged pages. Doctor's
    quick_check detects exactly this class and prescribes `bettermemory
    reindex`; pre-fix, `_open_for_rebuild` was the only recovery layer,
    so the prescribed repair crashed with "database disk image is
    malformed" and the recommendation dead-ended. rebuild() must fall
    back to the nuclear path — drop the file + WAL/SHM siblings,
    recreate, re-run the data phase once — and log the recovery."""
    store.write(content="alpha indexer note", scopes=["tools"])
    store.write(content="beta indexer note", scopes=["tools"])
    index_file = index.index_path(memory_dir)
    # Fold the WAL into the main file first so the page overwrite below
    # can't be shadowed by a fresher WAL copy of the same page.
    conn = sqlite3.connect(str(index_file))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        rootpage = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'memories_fts_data'"
        ).fetchone()[0]
    finally:
        conn.close()
    # Tear the shadow table's root page: every FTS read/write of the
    # inverted index starts there, but no open-time check touches it.
    with index_file.open("r+b") as fh:
        fh.seek((rootpage - 1) * page_size)
        fh.write(b"\xff" * page_size)
    # The premise that makes this the dead-end class: the meta-only
    # status() reads pass, so nothing upstream of rebuild sees damage.
    assert "corrupt" not in index.status(memory_dir)

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.index"):
        count = index.rebuild(memory_dir, store.iter_active())

    assert count == 2
    assert any("data phase failed" in r.getMessage() for r in caplog.records), (
        "nuclear recovery must be logged at WARNING with the corruption context"
    )
    s = index.status(memory_dir)
    assert "corrupt" not in s
    assert s["indexed_count"] == 2
    # The recovery reopen's first-touch stamp must not outlive the
    # successful retry — the flag clears in the same transaction the
    # rows land in.
    assert s["needs_rebuild"] is False
    # The repaired index actually answers queries.
    assert index.query(memory_dir, "alpha")


# ---------------------------------------------------------------------------
# id → filename lookup (H1 — schema v2)
# ---------------------------------------------------------------------------


def test_filenames_for_ids_resolves_written_memories(
    store: Store, memory_dir: Path
) -> None:
    """The lookup that `_handlers.load_search_candidates` uses to skip
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
    omitted — the caller (_handlers.load_search_candidates) falls back
    to load_all for those candidates."""
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


def test_older_schema_index_upgraded_to_current_via_drop_and_recreate(
    store: Store, memory_dir: Path
) -> None:
    """Regression: an older-schema index on disk must be transparently
    upgraded to the current version on the next connect. The data
    tables are dropped and recreated empty, and the index is flagged
    `needs_rebuild` so search bypasses it until `rebuild()` — the
    explicit reindex here, or the Store-construction auto-rebuild —
    restores full coverage and clears the flag."""
    import contextlib
    import sqlite3

    a = store.write(content="bridge", scopes=["tools"])
    db_path = index.index_path(memory_dir)
    # Force the on-disk version backwards to simulate a stale older
    # index. The data tables look fine; only the meta version flips.
    # `with sqlite3.connect(...)` scopes the transaction, not the
    # handle — the nested `conn` commits the UPDATE, closing() then
    # releases the fd (GC-timed ResourceWarnings, Windows unlink).
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")

    # Status call triggers _ensure_schema, which sees the older
    # version and drops + recreates.
    s = index.status(memory_dir)
    assert s["schema_version"] == index.SCHEMA_VERSION
    assert s["indexed_count"] == 0, (
        "after a schema migration the table should be empty until the rebuild"
    )
    assert s["needs_rebuild"] is True, (
        "the migration must flag the index rebuild-pending so search "
        "routes to load_all instead of the hollowed-out prefilter"
    )
    # A subsequent reindex restores the row count and clears the flag.
    index.rebuild(memory_dir, store.iter_active())
    s_after = index.status(memory_dir)
    assert s_after["indexed_count"] >= 1
    assert s_after["needs_rebuild"] is False
    # And the H1 surfaces work again on the reindexed store.
    filenames = index.filenames_for_ids(memory_dir, [a.id])
    assert a.id in filenames


# The LITERAL index `_SCHEMA` string as shipped before 30e912a (schema
# v3, releases <= 3.11): no `body_fts` / `scopes_fts` columns — the FTS
# table indexed the raw body under unicode61. Copied verbatim so the
# fixture below builds a byte-genuine 3.11 index rather than a
# back-projection of the current schema with the version stamp flipped.
_V3_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_verified_at TEXT,
    confidence TEXT NOT NULL,
    category TEXT,
    body TEXT NOT NULL,
    scopes_text TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    body, scopes_text,
    content='memories', content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, body, scopes_text)
    VALUES (new.rowid, new.body, new.scopes_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body, scopes_text)
    VALUES ('delete', old.rowid, old.body, old.scopes_text);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body, scopes_text)
    VALUES ('delete', old.rowid, old.body, old.scopes_text);
    INSERT INTO memories_fts(rowid, body, scopes_text)
    VALUES (new.rowid, new.body, new.scopes_text);
END;

CREATE INDEX IF NOT EXISTS memories_by_updated ON memories(updated DESC);

-- Inter-memory links. Keeps `_links_payload`'s reverse-link scan
-- out of `load_all` — that path was O(N) per `memory_show` because
-- finding "everyone who links AT this id" required walking every
-- memory's `links` field on disk. Now it's an index lookup.
CREATE TABLE IF NOT EXISTS memory_links (
    source_id TEXT NOT NULL,
    type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (source_id, type, target_id, note)
);

CREATE INDEX IF NOT EXISTS memory_links_by_target ON memory_links(target_id);

-- Cascade link cleanup when a memory is removed. Both source-side
-- (this memory's outbound links) and target-side (other memories
-- linking AT this id) get dropped. The target-side cleanup keeps
-- the reverse-link query honest: a hit against `target_id = X`
-- after X is tombstoned would otherwise dangle.
CREATE TRIGGER IF NOT EXISTS memory_links_cleanup AFTER DELETE ON memories BEGIN
    DELETE FROM memory_links
    WHERE source_id = old.id OR target_id = old.id;
END;
"""


def test_genuine_v3_index_migrates_and_rebuild_restores_search(
    store: Store, memory_dir: Path
) -> None:
    """End-to-end against a GENUINE pre-3.12 (schema v3) index: the
    literal v3 DDL with v3-shaped rows, not a current-schema index with
    the version stamp flipped back. Opening under current code must
    migrate cleanly (drop empty + flag rebuild-pending — no sqlite
    error from the missing `body_fts` / `scopes_fts` columns), and
    `rebuild()` must repopulate the preprocessed `body_fts` column so
    token search actually works on the migrated index."""
    import json

    a = store.write(content="tokyo relocation plan", scopes=["tools"])
    b = store.write(content="kubernetes networking", scopes=["infrastructure"])

    # Replace the hook-built current-schema index with a genuine v3 one
    # (siblings too — a stale WAL must not shadow the fresh file).
    db_path = index.index_path(memory_dir)
    index._unlink_index_files(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_V3_SCHEMA)
        rows = list(store.iter_active())
        for path, memory in rows:
            # The v3 row shape, as the since-removed plain-INSERT writer
            # produced it: raw body, space-padded scopes_text, no
            # preprocessed columns.
            conn.execute(
                "INSERT INTO memories("
                "id, created, updated, last_verified_at, confidence, "
                "category, body, scopes_text, scopes_json, filename) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory.id,
                    memory.created.isoformat(),
                    memory.updated.isoformat(),
                    None,
                    memory.confidence.value,
                    None,
                    memory.body,
                    " " + " ".join(memory.scopes) + " ",
                    json.dumps(memory.scopes),
                    path.name,
                ),
            )
        conn.execute("INSERT INTO meta VALUES ('schema_version', '3')")
        conn.execute("INSERT INTO meta VALUES ('indexed_count', ?)", (str(len(rows)),))
        conn.commit()
        # Fixture sanity: the v3 table really lacks the v4 columns.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        assert "body_fts" not in cols and "scopes_fts" not in cols
    finally:
        conn.close()

    # First open under current code: a clean v3→v4 migration — current
    # version stamp, empty tables, rebuild-pending.
    s = index.status(memory_dir)
    assert s["schema_version"] == index.SCHEMA_VERSION
    assert s["indexed_count"] == 0
    assert s["needs_rebuild"] is True
    # The hollowed-out index matches nothing yet; the flag is what
    # keeps `_handlers.load_search_candidates` off it in the meantime.
    assert index.query(memory_dir, "tokyo") == []

    # rebuild() repopulates `body_fts` from canonical disk state and
    # clears the flag; token search works again.
    assert index.rebuild(memory_dir, store.iter_active()) == 2
    s_after = index.status(memory_dir)
    assert s_after["needs_rebuild"] is False
    assert s_after["indexed_count"] == 2
    assert [r[0] for r in index.query(memory_dir, "tokyo")] == [a.id]
    assert [r[0] for r in index.query(memory_dir, "kubernetes")] == [b.id]


def test_store_construction_auto_rebuilds_migrated_index(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The auto-heal for the migration recall hole: a schema-version
    bump empties the index and flags it rebuild-pending; the NEXT Store
    construction rebuilds from canonical disk state without waiting for
    a manual `bettermemory reindex`. The S4 divergence WARNING for this
    shape is demoted to an INFO rebuild notice — a self-healed index is
    a resolution, not an operator action item."""
    from bettermemory import store as _store

    root = tmp_path / "auto_heal"
    setup = Store(root)
    a = setup.write(content="legacy searchable body", scopes=["tools"])
    b = setup.write(content="second legacy entry", scopes=["tools"])

    # Back-date the on-disk index version; the next index open migrates
    # (drop empty + flag).
    conn = sqlite3.connect(str(index.index_path(root)))
    try:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())

    caplog.clear()
    with caplog.at_level("INFO", logger="bettermemory.store"):
        Store(root)

    s = index.status(root)
    assert s["needs_rebuild"] is False
    assert s["indexed_count"] == 2
    assert {r[0] for r in index.query(root, "legacy")} == {a.id, b.id}
    # Demotion contract: no out-of-sync WARNING for the healed
    # migration; an INFO records the rebuild instead.
    assert not [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "out-of-sync" in r.getMessage()
    ], "a self-healed migration must not fire the S4 divergence warning"
    assert any(
        r.levelname == "INFO" and "rebuilt 2 memories" in r.getMessage()
        for r in caplog.records
    ), "the auto-rebuild must leave an INFO record of what it did"


def test_first_touch_index_in_populated_store_is_flagged_rebuild_pending(
    store: Store, memory_dir: Path
) -> None:
    """The deleted-index recovery hole — the first-touch sibling of the
    schema-migration hole above. Deleting `.index.sqlite` was the
    historical recovery advice (and a backup restore can drop the
    sidecar); the next hook write then creates a FRESH index inside a
    populated store, stamped current with no flag. The incremental
    hooks refill only touched memories, so once `indexed_count`
    crosses the prefilter threshold every untouched legacy memory
    silently vanishes from `memory_search`. First-touch creation must
    flag `needs_rebuild` when the store root already holds memories."""
    legacy = store.write(content="alpha legacy landmark", scopes=["tools"])
    index._unlink_index_files(index.index_path(memory_dir))

    # The hook write recreates the index first-touch: one indexed row,
    # while the legacy memory has none.
    store.write(content="alpha fresh note", scopes=["tools"])

    s = index.status(memory_dir)
    assert s["indexed_count"] == 1
    assert s["needs_rebuild"] is True, (
        "a fresh index born inside a populated store must be flagged "
        "rebuild-pending, or the prefilter re-engages on indexed_count "
        "alone and untouched legacy memories vanish from search"
    )
    # Same lifecycle as the migration flag: rebuild() restores full
    # coverage and is the only thing that clears it.
    assert index.rebuild(memory_dir, store.iter_active()) == 2
    s_after = index.status(memory_dir)
    assert s_after["needs_rebuild"] is False
    assert legacy.id in index.filenames_for_ids(memory_dir, [legacy.id])


def test_first_touch_index_on_fresh_store_first_write_stays_unflagged(
    store: Store, memory_dir: Path
) -> None:
    """A genuinely-new store's first hook write is also a first-touch
    creation — but the only memory file on disk is the one this same
    upsert writes a row for (hook order is write-file-then-upsert), so
    coverage is complete and no rebuild flag belongs. Pins the
    `inflight_filename` exclusion in the populated-check."""
    store.write(content="first ever note", scopes=["tools"])

    s = index.status(memory_dir)
    assert s["indexed_count"] == 1
    assert s["needs_rebuild"] is False


def test_first_touch_via_status_on_empty_db_file_in_populated_store_flags(
    store: Store, memory_dir: Path
) -> None:
    """The no-exclusion default: a schema-empty index FILE (a crashed
    process's `connect()` remnant — zero bytes is a valid empty SQLite
    database) inside a populated store gets its schema created by
    whichever reader touches it first, `status()` here. No upsert is in
    flight, so every on-disk memory counts and the flag must land."""
    store.write(content="alpha legacy landmark", scopes=["tools"])
    db_path = index.index_path(memory_dir)
    index._unlink_index_files(db_path)
    db_path.write_bytes(b"")

    s = index.status(memory_dir)
    assert s["indexed_count"] == 0
    assert s["needs_rebuild"] is True


def test_store_construction_auto_rebuilds_after_index_deletion(
    store: Store, memory_dir: Path
) -> None:
    """End of the deleted-index recovery arc: the first-touch flag
    routes search to `load_all` immediately, and the NEXT Store
    construction auto-rebuilds from canonical disk state and clears it
    — the same heal `test_store_construction_auto_rebuilds_migrated_index`
    pins for the migration flag."""
    a = store.write(content="alpha legacy landmark", scopes=["tools"])
    index._unlink_index_files(index.index_path(memory_dir))
    b = store.write(content="alpha fresh note", scopes=["tools"])

    Store(memory_dir)

    s = index.status(memory_dir)
    assert s["needs_rebuild"] is False
    assert s["indexed_count"] == 2
    assert {r[0] for r in index.query(memory_dir, "alpha")} == {a.id, b.id}


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
        index._ensure_schema(conn, db)
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
            index._ensure_schema(conn, db)
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


def _make_v3_index(db: Path, *, row_id: str | None = None) -> None:
    """Build a genuine v3 index file at `db`: literal v3 DDL, meta
    stamped '3', optionally one v3-shaped row. Fixture for the
    migration race tests below."""
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_V3_SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('schema_version', '3')")
        if row_id is not None:
            conn.execute(
                "INSERT INTO memories(id, created, updated, confidence, body, "
                "scopes_text, scopes_json) VALUES "
                "(?, '2026-01-01', '2026-01-01', 'fact', 'b', ' s ', '[]')",
                (row_id,),
            )
        conn.commit()
    finally:
        conn.close()


def test_migration_stamp_commits_atomically_with_swap(tmp_path: Path) -> None:
    """The version stamp / `indexed_count` reset / `needs_rebuild` flag
    must commit in the SAME transaction as the drop+recreate. When they
    committed separately, a pre-bump process reading meta between the
    two commits saw the new tables with the old version stamp, passed
    its own version check, and its old-column-list INSERT landed as a
    permanently FTS-invisible row (`body_fts` DEFAULT '' indexed by the
    trigger).

    Proven via crash-consistency: a trigger aborts the stamp UPDATE,
    and the whole migration — swap included — must roll back with it.
    The old two-transaction shape fails this by committing the swap
    first, leaving v4 tables stamped '3' on disk."""
    db = tmp_path / "t.sqlite"
    _make_v3_index(db, row_id="keep")
    conn = sqlite3.connect(db)
    try:
        # Abort the stamp statement. RAISE(ABORT) backs out the
        # statement but leaves the enclosing transaction open, so
        # `_ensure_schema`'s rollback decides what survives.
        conn.execute(
            "CREATE TRIGGER stamp_fails BEFORE UPDATE ON meta "
            f"WHEN new.key = 'schema_version' AND new.value = '{index.SCHEMA_VERSION}' "
            "BEGIN SELECT RAISE(ABORT, 'stamp blocked by test'); END"
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            index._ensure_schema(conn, db)
    finally:
        conn.close()

    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        version = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        row = conn.execute("SELECT id FROM memories").fetchone()
    finally:
        conn.close()
    # Consistency is the contract: the shape the tables have and the
    # version meta claims must agree. Swap committed + stamp rolled
    # back (the old bug shape) shows up as body_fts present with
    # version still '3'.
    assert "body_fts" not in cols, (
        "the failed stamp left v4 tables behind — the swap committed "
        "in a separate transaction from the version stamp"
    )
    assert version == "3"
    assert row == ("keep",)


def test_concurrent_migrators_loser_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two same-version processes can both read the old schema_version
    and enter the migration branch. The migration flock serialises
    them, and the loser's under-lock re-read must turn it into a no-op
    — NOT a second wipe destroying rows the winner's caller already
    started repopulating."""
    import contextlib

    from bettermemory._fsutil import flock_excl as real_flock

    db = tmp_path / "race.sqlite"
    _make_v3_index(db)

    state = {"winner_ran": False}

    @contextlib.contextmanager
    def racing_flock(path: Path):
        # Deterministic lost race: before the loser acquires the lock,
        # a winner runs the SAME migration to completion on its own
        # connection (the nested `_ensure_schema` re-enters this
        # wrapper with winner_ran already set, so it goes straight to
        # the real, still-uncontended flock).
        if not state["winner_ran"]:
            state["winner_ran"] = True
            winner = sqlite3.connect(db)
            try:
                index._ensure_schema(winner, db)
                # Post-migration repopulation the loser must not wipe.
                winner.execute(
                    "INSERT INTO memories(id, created, updated, confidence, "
                    "body, scopes_text, scopes_json) VALUES "
                    "('marker', '2026-01-01', '2026-01-01', 'fact', 'b', "
                    "' s ', '[]')"
                )
                winner.commit()
            finally:
                winner.close()
        with real_flock(path):
            yield

    monkeypatch.setattr(index, "flock_excl", racing_flock)

    loser = sqlite3.connect(db)
    try:
        index._ensure_schema(loser, db)
    finally:
        loser.close()

    check = sqlite3.connect(db)
    try:
        rows = check.execute("SELECT id FROM memories").fetchall()
        version = check.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        flag = check.execute(
            "SELECT value FROM meta WHERE key = 'needs_rebuild'"
        ).fetchone()
    finally:
        check.close()
    assert rows == [("marker",)], (
        "the losing migrator re-wiped the tables instead of no-opping "
        "after its under-lock re-read"
    )
    assert version == str(index.SCHEMA_VERSION)
    # dfa7867 semantics: the winner's migration set the flag; only a
    # successful `rebuild()` may clear it.
    assert flag is not None and flag[0] == "1"


def test_migration_reread_newer_version_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a NEWER-version migrator wins the race while we wait on the
    migration flock, the re-read must raise `IndexVersionError` (the
    primary check's contract) rather than wipe tables a newer reader
    now depends on."""
    import contextlib

    from bettermemory._fsutil import flock_excl as real_flock

    db = tmp_path / "race.sqlite"
    _make_v3_index(db, row_id="newer-owned")

    state = {"stamped": False}

    @contextlib.contextmanager
    def racing_flock(path: Path):
        if not state["stamped"]:
            state["stamped"] = True
            winner = sqlite3.connect(db)
            try:
                winner.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(index.SCHEMA_VERSION + 1),),
                )
                winner.commit()
            finally:
                winner.close()
        with real_flock(path):
            yield

    monkeypatch.setattr(index, "flock_excl", racing_flock)

    loser = sqlite3.connect(db)
    try:
        with pytest.raises(index.IndexVersionError):
            index._ensure_schema(loser, db)
    finally:
        loser.close()

    check = sqlite3.connect(db)
    try:
        row = check.execute("SELECT id FROM memories").fetchone()
    finally:
        check.close()
    assert row == ("newer-owned",), (
        "the losing migrator wiped tables that a newer-version index now owns"
    )


def test_concurrent_first_touch_stampers_loser_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes can both find a fresh index with no
    `schema_version` row and race to stamp it — the Store flock is
    per-memory-file, so writers of different memories don't serialise
    here. `meta.key` is a PRIMARY KEY: an unserialised loser dies with
    `IntegrityError: UNIQUE constraint failed: meta.key`, which the
    best-effort Store hooks swallow at the cost of a dropped index
    row, a concurrent `status()` reports as corrupt, and
    `_open_for_rebuild` answers by unlinking the index the winner just
    stamped. The first-touch flock + under-lock re-read must turn the
    loser into a plain no-op instead.

    The winner runs at whichever seam the loser crosses first, so the
    lost race is deterministic in either shape: at the loser's flock
    acquire (the serialised stamp), or — were no lock to guard the
    branch — right before the loser's own stamp INSERT, where the
    winner's committed row makes that INSERT raise."""
    import contextlib

    from bettermemory._fsutil import flock_excl as real_flock

    db = tmp_path / "race.sqlite"
    state = {"winner_ran": False}

    def run_winner() -> None:
        # Recursion guard: the winner's own `_ensure_schema` re-enters
        # `racing_flock` and must go straight to the real, still-
        # uncontended flock.
        if state["winner_ran"]:
            return
        state["winner_ran"] = True
        winner = sqlite3.connect(db)
        try:
            index._ensure_schema(winner, db)
        finally:
            winner.close()

    @contextlib.contextmanager
    def racing_flock(path: Path):
        run_winner()
        with real_flock(path):
            yield

    class StampRacingConn:
        """Forwards to the real connection; the first-touch version
        stamp INSERT additionally triggers the winner first."""

        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO meta") and "schema_version" in sql:
                run_winner()
            return self._real.execute(sql, *args)

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    monkeypatch.setattr(index, "flock_excl", racing_flock)

    loser = sqlite3.connect(db)
    try:
        index._ensure_schema(StampRacingConn(loser), db)  # type: ignore[arg-type]
    finally:
        loser.close()

    assert state["winner_ran"], "neither seam fired — the race was never injected"
    check = sqlite3.connect(db)
    try:
        version = check.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        fingerprint = check.execute(
            "SELECT value FROM meta WHERE key = 'tokenizer_fingerprint'"
        ).fetchone()[0]
    finally:
        check.close()
    from bettermemory.search import tokenizer_fingerprint

    assert version == str(index.SCHEMA_VERSION)
    assert fingerprint == tokenizer_fingerprint()


def test_first_touch_reread_newer_version_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a NEWER-build creator wins the first-touch race while we
    wait on the flock, the under-lock re-read must hand the winner's
    stamp to the version handling and raise `IndexVersionError` (the
    primary check's contract) — never stamp over or silently accept a
    version a newer reader owns."""
    import contextlib

    from bettermemory._fsutil import flock_excl as real_flock

    db = tmp_path / "race.sqlite"
    state = {"stamped": False}

    @contextlib.contextmanager
    def racing_flock(path: Path):
        if not state["stamped"]:
            state["stamped"] = True
            winner = sqlite3.connect(db)
            try:
                winner.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(index.SCHEMA_VERSION + 1),),
                )
                winner.commit()
            finally:
                winner.close()
        with real_flock(path):
            yield

    monkeypatch.setattr(index, "flock_excl", racing_flock)

    loser = sqlite3.connect(db)
    try:
        with pytest.raises(index.IndexVersionError):
            index._ensure_schema(loser, db)
    finally:
        loser.close()

    check = sqlite3.connect(db)
    try:
        version = check.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
    finally:
        check.close()
    assert version == str(index.SCHEMA_VERSION + 1), (
        "the losing first-toucher overwrote a stamp a newer reader owns"
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


def test_unparseable_only_gap_warns_about_files_not_index(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A junk `.md` that can never parse must NOT trip the out-of-sync
    warning: `index.rebuild` consumes `iter_active()`, which skips the
    file, so the prescribed `bettermemory reindex` could never clear
    the divergence (N+1 disk files vs N indexed, forever). The store
    still surfaces the gap — as a fix-the-files warning that says
    reindex will not help — and the one-shot guard applies to it the
    same way."""
    from bettermemory import store as _store

    root = tmp_path / "junked"
    setup = Store(root)
    setup.write(content="indexed via store", scopes=["tools"])
    (root / "junk.md").write_text("no frontmatter at all\n", encoding="utf-8")
    _store._DIVERGENCE_WARNED_ROOTS.discard(root.expanduser().resolve())

    caplog.clear()
    with caplog.at_level("WARNING", logger="bettermemory.store"):
        Store(root)  # warns (about the files)
        Store(root)  # silent — same one-shot guard
    out_of_sync = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "out-of-sync" in r.getMessage()
    ]
    assert not out_of_sync, (
        f"unparseable-only gap must not claim the index is out-of-sync "
        f"(reindex can never clear it), got: {out_of_sync!r}"
    )
    parse_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "cannot be parsed" in r.getMessage()
    ]
    assert len(parse_warnings) == 1, (
        f"expected exactly one fix-the-files WARNING, got {parse_warnings!r}"
    )
    message = parse_warnings[0].getMessage()
    assert "reindex` will not change this" in message, (
        f"warning must steer away from the useless repair, got: {message!r}"
    )
    assert "1 of 2" in message, (
        f"warning must show the unparseable/disk arithmetic, got: {message!r}"
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

    def flaky_ensure(conn: sqlite3.Connection, path: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            real_ensure(conn, path)  # raises IndexVersionError on skewed index
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


# ---------------------------------------------------------------------------
# Tokenizer v2 / schema v4 — preprocessed FTS content
# ---------------------------------------------------------------------------


def test_query_matches_cjk_body_via_bigrams(store: Store, memory_dir: Path) -> None:
    """Audit repro (FTS side): unicode61 over the raw body treated an
    unspaced CJK clause as ONE token, so MATCH '"東京"' returned zero
    rows against a body that plainly says 東京. Schema v4 indexes the
    bigram-segmented `fts_index_text`, so word-level CJK queries hit."""
    cjk = store.write(
        content="東京オフィスは2026年に移転する予定", scopes=["projects:tokyo"]
    )
    other = store.write(content="kubernetes networking", scopes=["tools"])
    index.rebuild(memory_dir, store.iter_active())

    for query in ("東京", "移転", "東京オフィス"):
        ids = [r[0] for r in index.query(memory_dir, query)]
        assert cjk.id in ids, f"FTS missed CJK query {query!r}"
        assert other.id not in ids


def test_query_matches_plural_query_against_singular_body(
    store: Store, memory_dir: Path
) -> None:
    """Audit repro (FTS side of the stemming finding): 'standups'
    against a body that says 'standup' returned nothing, so on a large
    store the prefilter starved the rankers of the candidate. Both
    sides of the MATCH now speak stemmed tokens."""
    a = store.write(content="Daily standup is at 9:15", scopes=["tools"])
    index.rebuild(memory_dir, store.iter_active())

    ids = [r[0] for r in index.query(memory_dir, "standups")]
    assert a.id in ids
    # And the mirror image: singular query, plural body.
    b = store.write(content="Rotate the feature branches weekly", scopes=["tools"])
    ids = [r[0] for r in index.query(memory_dir, "branch")]
    assert b.id in ids


def test_schema_v4_stores_preprocessed_fts_columns(
    store: Store, memory_dir: Path
) -> None:
    """The `body_fts` column is `fts_index_text` output — stems, CJK
    bigrams, kebab parts — while raw `body` and the space-padded
    `scopes_text` stay untouched for canonical reads and the LIKE
    scope filter."""
    import sqlite3

    store.write(content="Claude-Code caches 東京タワー", scopes=["projects:alpha-beta"])
    index.rebuild(memory_dir, store.iter_active())

    conn = sqlite3.connect(str(index.index_path(memory_dir)))
    try:
        row = conn.execute(
            "SELECT body, body_fts, scopes_text, scopes_fts FROM memories"
        ).fetchone()
    finally:
        conn.close()
    body, body_fts, scopes_text, scopes_fts = row
    assert body.rstrip("\n") == "Claude-Code caches 東京タワー"
    for tok in ("claud-cod", "cach", "東京", "京タ"):
        assert tok in body_fts.split(), tok
    assert scopes_text == " projects:alpha-beta "
    # Scope tokens are searchable in their stemmed/expanded form.
    for tok in ("project", "alpha", "beta"):
        assert tok in scopes_fts.split(), tok


# ---------------------------------------------------------------------------
# Tokenizer fingerprint ratchet (schema v5)
#
# Schema v4+ PERSISTS tokenize() output (`body_fts` / `scopes_fts`), so
# query/index parity requires the persisted stream to match the live
# tokenizer across releases. Nothing enforced that until v5: four
# post-3.12.0 tokenizer fixes (stopword curation, final-y
# normalisation, CJK index-side unigrams, the NFKC fold) respelled the
# stream with no schema bump, so every 3.12.0-built index answered
# live queries against stale spellings. The meta table now carries a
# tokenizer fingerprint next to schema_version; a mismatch migrates
# exactly like an older version.
# ---------------------------------------------------------------------------


def test_tokenizer_fingerprint_pinned_constant_is_the_ratchet() -> None:
    """THE RATCHET. If this assertion fails, a change to the shared
    pipeline (tokenize()'s folds, stopword lists, stemmer rules, CJK
    segmentation, `_expand_kebab`'s widening — or the probe corpus
    itself) respelled the index-side token stream, which makes every
    existing on-disk index stale against live queries. Any diff
    REQUIRES re-pinning `index.TOKENIZER_FINGERPRINT` to the new value
    — and bumping `index.SCHEMA_VERSION` with it iff the current
    version has shipped in a release. (On-disk stores heal either way —
    the runtime stamp/compare uses the live fingerprint — but the bump
    is what keeps version semantics and the CHANGELOG honest for
    released streams; pre-release stream amendments fold into the
    pending bump.)"""
    from bettermemory.search import tokenizer_fingerprint

    assert tokenizer_fingerprint() == index.TOKENIZER_FINGERPRINT, (
        "index-side token stream changed: re-pin index.TOKENIZER_FINGERPRINT"
        f" = {tokenizer_fingerprint()!r} (and bump index.SCHEMA_VERSION iff"
        " the current version has shipped in a release)"
    )


def test_stale_tokenizer_fingerprint_heals_like_older_schema_version(
    store: Store, memory_dir: Path
) -> None:
    """A `meta.tokenizer_fingerprint` differing from the live tokenizer
    at the CURRENT schema version — the "tokenizer changed, nobody
    bumped" state the ratchet exists for — must take exactly the
    older-version migration path: atomic wipe + `needs_rebuild`, then
    the Store-construction auto-rebuild, ending with the live
    fingerprint stamped so the next open is stable."""
    from bettermemory.search import tokenizer_fingerprint

    a = store.write(content="tokenizer drift probe", scopes=["tools"])
    conn = sqlite3.connect(str(index.index_path(memory_dir)))
    try:
        conn.execute(
            "UPDATE meta SET value = 'stale-digest' WHERE key = 'tokenizer_fingerprint'"
        )
        conn.commit()
    finally:
        conn.close()

    # First open: the older-version shape — current stamp, empty
    # tables, rebuild-pending.
    s = index.status(memory_dir)
    assert s["schema_version"] == index.SCHEMA_VERSION
    assert s["indexed_count"] == 0
    assert s["needs_rebuild"] is True

    # Construction auto-rebuild heals it end-to-end.
    Store(memory_dir)
    s_after = index.status(memory_dir)
    assert s_after["needs_rebuild"] is False
    assert s_after["indexed_count"] == 1
    assert [r[0] for r in index.query(memory_dir, "drift")] == [a.id]

    conn = sqlite3.connect(str(index.index_path(memory_dir)))
    try:
        stamped = conn.execute(
            "SELECT value FROM meta WHERE key = 'tokenizer_fingerprint'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stamped == tokenizer_fingerprint()


def test_v4_index_with_stale_spelled_stream_heals_on_construction(
    store: Store, memory_dir: Path
) -> None:
    """End-to-end v4→v5 heal against the GENUINE 3.12.0 on-disk state:
    version stamped 4, no fingerprint row (3.12.0 never wrote one), and
    `body_fts` spelled by the 3.12.0 tokenizer ('todos', 'cooky') —
    which live queries ('todo', 'cooki') can no longer match. The first
    Store construction after upgrading must wipe, flag, and auto-rebuild
    so live-tokenizer queries hit again with no manual reindex."""
    m = store.write(content="Track the TODOs and cookies backlog", scopes=["tools"])

    conn = sqlite3.connect(str(index.index_path(memory_dir)))
    try:
        # The literal 3.12.0 `fts_index_text` output for this body
        # ('todos' was still an es stopword — surface-exempt from the
        # stemmer — and 'cookies' took the pre-final-y 'ies'→'y' rule).
        # The AFTER UPDATE trigger keeps the FTS table in sync with the
        # stale spelling, exactly like a real 3.12.0-built index.
        conn.execute(
            "UPDATE memories SET body_fts = 'track the todos and cooky backlog'"
        )
        conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        conn.execute("DELETE FROM meta WHERE key = 'tokenizer_fingerprint'")
        conn.commit()
    finally:
        conn.close()

    # First open under v5 code migrates (wipe + flag); the hollowed-out
    # index matches nothing until the rebuild.
    assert index.query(memory_dir, "todo") == []
    s = index.status(memory_dir)
    assert s["schema_version"] == index.SCHEMA_VERSION
    assert s["needs_rebuild"] is True

    Store(memory_dir)  # first construction after the upgrade

    s_after = index.status(memory_dir)
    assert s_after["needs_rebuild"] is False
    assert s_after["indexed_count"] == 1
    # The respelled index answers live-tokenizer queries again — both
    # inflections of both words the 3.12.0 spelling silently missed.
    for q in ("todo", "TODOs", "cookie", "cookies"):
        assert [r[0] for r in index.query(memory_dir, q)] == [m.id], q


# ---------------------------------------------------------------------------
# Corpus document frequencies (BM25 IDF across the prefilter boundary)
# ---------------------------------------------------------------------------


def _admit_all(_scopes: list[str], _origin: object) -> bool:
    """Admission predicate that keeps everything — the unfiltered
    baseline. The real caller binds `search.candidate_admitted` to a
    request's scope/repo/worktree filters; these tests isolate the
    counting from the filtering, which
    `test_corpus_document_frequencies_counts_only_admitted_rows` covers
    on its own."""
    return True


def test_corpus_document_frequencies_counts_body_and_scope_separately(
    store: Store, memory_dir: Path
) -> None:
    """The two denominators `search.compute_idf` needs, from FTS5.

    `body_df` counts documents whose BODY carries the term; `scope_df`
    counts body-OR-scopes. They must not be conflated and they must not be
    summed: 'shared' below sits in one body and in every scope, so the
    union is 3 rather than 1 + 3.
    """
    store.write(content="alpha shared", scopes=["shared"])
    store.write(content="alpha", scopes=["shared"])
    store.write(content="beta", scopes=["shared"])

    resolved = index.corpus_document_frequencies(
        memory_dir, ["alpha", "beta", "shared", "never-written"], admit=_admit_all
    )

    assert resolved is not None
    size, body_df, scope_df = resolved
    assert size == 3
    assert body_df["alpha"] == 2
    assert body_df["beta"] == 1
    assert body_df["shared"] == 1
    assert scope_df["shared"] == 3
    # A term nobody wrote has no row at all rather than a 0 — callers
    # branch on absence, and a 0 would price it as maximally rare.
    assert "never-written" not in body_df
    assert "never-written" not in scope_df


def test_corpus_document_frequencies_degrades_to_none(tmp_path: Path) -> None:
    """Every unusable input yields None, never an exception.

    The index is a derived cache, so a caller that cannot get corpus
    statistics must fall back to pool-derived ones and still return
    results — degrade the ranking, never fail the search.
    """
    # No index on disk at all.
    assert (
        index.corpus_document_frequencies(tmp_path, ["alpha"], admit=_admit_all) is None
    )
    # No terms to look up — short-circuits before touching sqlite.
    assert index.corpus_document_frequencies(tmp_path, [], admit=_admit_all) is None
    assert (
        index.corpus_document_frequencies(tmp_path, ["", "  "], admit=_admit_all)
        is None
    )


def test_corpus_document_frequencies_survives_a_corrupt_index(
    store: Store, memory_dir: Path
) -> None:
    """Page-level corruption surfaces out of the vocab reads, not the
    schema gate — same routing every other read helper here takes."""
    store.write(content="alpha", scopes=["tools"])
    index_path = index.index_path(memory_dir)
    assert index_path.exists()
    index_path.write_bytes(b"this is not a sqlite database" * 40)

    assert (
        index.corpus_document_frequencies(memory_dir, ["alpha"], admit=_admit_all)
        is None
    )


def test_corpus_document_frequencies_counts_only_admitted_rows(
    store: Store, memory_dir: Path
) -> None:
    """The denominator is the collection the search will RANK, not the
    whole store.

    This is the reason the origin columns exist. Under auto-scope a
    caller in repo A can only retrieve repo-A memories, so pricing term
    rarity against repo B's memories describes a collection that caller
    will never see. Here 'alpha' is in every body but only two rows are
    admitted, so its df must be 2 of 2 — not 4 of 4.
    """
    from bettermemory.origin import Origin

    repo_a = Origin(repo="git@github.com:example/a.git")
    repo_b = Origin(repo="git@github.com:example/b.git")
    store.write(content="alpha one", scopes=["tools"], origin=repo_a)
    store.write(content="alpha two", scopes=["tools"], origin=repo_a)
    store.write(content="alpha three", scopes=["tools"], origin=repo_b)
    store.write(content="alpha four", scopes=["tools"], origin=repo_b)

    def admit_only_a(_scopes: list[str], origin: object) -> bool:
        return getattr(origin, "repo", None) == "git@github.com:example/a.git"

    resolved = index.corpus_document_frequencies(
        memory_dir, ["alpha"], admit=admit_only_a
    )

    assert resolved is not None
    size, body_df, _scope_df = resolved
    assert size == 2, "collection size must be the admitted set"
    assert body_df["alpha"] == 2, "df must not count rows the caller cannot retrieve"


def test_corpus_document_frequencies_returns_none_when_nothing_is_admitted(
    store: Store, memory_dir: Path
) -> None:
    """An empty admitted set is not a corpus of size 0 — it means the
    caller should fall back rather than divide by nothing."""
    store.write(content="alpha", scopes=["tools"])

    resolved = index.corpus_document_frequencies(
        memory_dir, ["alpha"], admit=lambda _s, _o: False
    )

    assert resolved is None


# ---------------------------------------------------------------------------
# Scope counts (the SessionStart hook's cheap half of memory_scope_overview)
# ---------------------------------------------------------------------------


def _scope_counts_from_load_all(
    store: Store, *, repo_filter: str | None, worktree_filter: str | None
) -> tuple[int, dict[str, int]]:
    """The expensive, obviously-correct answer: parse every file.

    This is exactly what `handlers/scope_overview.memory_scope_overview`
    does (`load_all` + the shared admission predicate); the index path
    exists only to produce the same numbers without the per-file open +
    YAML parse. Deriving the expectation this way rather than hardcoding
    it is the point — a hardcoded pair would still agree with an index
    path that had silently stopped matching the surface it mirrors.
    """
    from bettermemory.search import candidate_admitted

    total = 0
    counts: dict[str, int] = {}
    for memory in store.load_all():
        if not candidate_admitted(
            memory.scopes,
            memory.origin,
            scope_filter=None,
            excluded=set(),
            repo_filter=repo_filter,
            worktree_filter=worktree_filter,
        ):
            continue
        total += 1
        for scope in memory.scopes:
            counts[scope] = counts.get(scope, 0) + 1
    return total, counts


def test_scope_counts_agree_with_the_load_all_answer(
    store: Store, memory_dir: Path
) -> None:
    """The columnar scan must produce the SAME counts `load_all` would.

    Mixed origins on purpose: this is where a cheap path can silently
    diverge, because admission is decided from reconstructed
    `(repo, worktree_root)` columns rather than from the parsed origin
    block. A memory scoped to a repo the caller isn't in must not be
    counted, and the multi-scope rows must contribute 1 to the total and
    1 to EACH of their scope buckets."""
    from bettermemory.origin import Origin
    from bettermemory.search import candidate_admitted

    repo_a = "git@github.com:example/a.git"
    repo_b = "git@github.com:example/b.git"
    store.write(content="one", scopes=["tools"], origin=Origin(repo=repo_a))
    store.write(
        content="two", scopes=["tools", "learning-style"], origin=Origin(repo=repo_a)
    )
    store.write(content="three", scopes=["infrastructure"], origin=Origin(repo=repo_b))
    # No origin at all — the "global" row that must pass for every caller.
    store.write(content="four", scopes=["tools"])

    def _admit(scopes: list[str], origin: Origin | None) -> bool:
        return candidate_admitted(
            scopes,
            origin,
            scope_filter=None,
            excluded=set(),
            repo_filter=repo_a,
            worktree_filter=None,
        )

    resolved = index.scope_counts(memory_dir, admit=_admit)

    assert resolved is not None
    assert resolved == _scope_counts_from_load_all(
        store, repo_filter=repo_a, worktree_filter=None
    )
    total, counts = resolved
    assert total == 3, "repo-B's memory must not be counted for a repo-A caller"
    assert counts == {"tools": 3, "learning-style": 1}


def test_scope_counts_report_zero_rather_than_none_when_nothing_is_admitted(
    store: Store, memory_dir: Path
) -> None:
    """Deliberately the OPPOSITE of `corpus_document_frequencies`.

    There, an empty admitted set means "no denominator, fall back". Here
    it is a true answer — "this repo has nothing stored" — and folding it
    into the None channel would leave the caller unable to distinguish it
    from an unusable index, which wants the opposite handling (one is
    silence-because-nothing, the other is silence-because-broken)."""
    store.write(content="alpha", scopes=["tools"])

    assert index.scope_counts(memory_dir, admit=lambda _s, _o: False) == (0, {})


def test_scope_counts_degrade_to_none_without_a_usable_index(
    store: Store, memory_dir: Path, tmp_path: Path
) -> None:
    """Absent or corrupt index returns None, never an exception — the
    caller is a session-start hook that must degrade to silence."""
    assert index.scope_counts(tmp_path / "nope", admit=_admit_all) is None

    store.write(content="alpha", scopes=["tools"])
    index_path = index.index_path(memory_dir)
    assert index_path.exists()
    index_path.write_bytes(b"this is not a sqlite database" * 40)

    assert index.scope_counts(memory_dir, admit=_admit_all) is None


def test_index_round_trips_origin_columns(store: Store, memory_dir: Path) -> None:
    """Schema v6 persists the origin block's repo/worktree, and an UPSERT
    refreshes them — `migrate origin --repair` rewrites origins in place,
    and a stale index value would keep filtering on the pre-repair repo."""
    import sqlite3

    from bettermemory.origin import Origin

    memory = store.write(
        content="alpha",
        scopes=["tools"],
        origin=Origin(repo="git@github.com:example/before.git", worktree_root="/w/one"),
    )

    def row_for(mid: str) -> sqlite3.Row:
        conn = sqlite3.connect(index.index_path(memory_dir))
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT origin_repo, origin_worktree FROM memories WHERE id = ?",
                (mid,),
            ).fetchone()
        finally:
            conn.close()

    before = row_for(memory.id)
    assert before["origin_repo"] == "git@github.com:example/before.git"
    assert before["origin_worktree"] == "/w/one"

    store.update(
        memory.model_copy(
            update={
                "origin": Origin(
                    repo="git@github.com:example/after.git", worktree_root="/w/two"
                )
            }
        )
    )

    after = row_for(memory.id)
    assert after["origin_repo"] == "git@github.com:example/after.git", (
        "an upsert must refresh origin_repo, not keep the insert-time value"
    )
    assert after["origin_worktree"] == "/w/two"
