"""The bettermemory 9 store: one SQLite file per store (unit U2).

The v8 store was a directory of markdown files with a derived FTS5 index
beside it. The v9 store is one file whose tables are the records, whose
FTS5 table and triggers are the v8 index's verbatim, and whose every
mutation is a row of the hash-chained log. These tests pin the schema
parity with v8, the record round trips, the candidate query against the
v8 index on the same rows, the fold property, and the CLI command.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from bettermemory import index as v8_index
from bettermemory import sqlite_store
from bettermemory.identity import Actor
from bettermemory.log import MIGRATE_V8, STORE_CREATED
from bettermemory.models import (
    Category,
    Confidence,
    Episode,
    LinkType,
    Memory,
    MemoryLink,
    Source,
    TombstonedMemory,
    generate_ulid,
)
from bettermemory.origin import Origin
from bettermemory.sqlite_store import (
    FOLDED_TABLES,
    IMPORTED,
    LOCAL,
    MUTATION_KINDS,
    STORE_FILENAME,
    NotFoundError,
    SqliteStore,
)
from bettermemory.store import Store as V8Store

_HEAD = "a" * 40
_NOW = datetime(2026, 9, 26, 1, 2, 3, 456000, tzinfo=timezone.utc)


def _memory(body: str, *scopes: str, **overrides: Any) -> Memory:
    fields: dict[str, Any] = dict(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=list(scopes) or ["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )
    fields.update(overrides)
    return Memory(**fields)


def _full_memory() -> Memory:
    target = generate_ulid()
    return _memory(
        "the whole record, every field set",
        "projects:foo",
        "tools",
        confidence=Confidence.HIGH,
        source=Source.INFERRED,
        origin=Origin(
            cwd="/w/foo",
            repo="https://example.com/foo.git",
            branch="main",
            worktree_root="/w/foo",
            source="roots",
        ),
        actor=Actor(
            client="claude-code",
            client_version="2.1.281",
            model="claude-fable-5-1",
            sources={"client": "client-info", "model": "header"},
        ),
        last_verified_at=_NOW + timedelta(hours=1),
        category=Category.USER_INFERENCE,
        verified_paths=["src/a.py", "src/b.py"],
        verified_commits=["abc1234"],
        verified_versions=["8.0.0"],
        verified_absent_paths=["tools/gone.py"],
        claims=["src/a.py::main", "!tools/gone.py"],
        verified_head=_HEAD,
        links=[
            MemoryLink(type=LinkType.SUPERSEDES, target_id=target, note="newer"),
            MemoryLink(type=LinkType.EXTENDS, target_id=target),
        ],
        corroborations=2,
        last_corroborated=_NOW + timedelta(days=1),
    )


def _episode(session_id: str = "sess_a", **overrides: Any) -> Episode:
    fields: dict[str, Any] = dict(
        id=generate_ulid(),
        session_id=session_id,
        created=_NOW,
        body="tried the thing\n",
        scopes=["projects:foo"],
        takeaway="it worked",
        origin=Origin(cwd="/w/foo", worktree_root="/w/foo"),
        swarm_id="sess_coordinator",
    )
    fields.update(overrides)
    return Episode(**fields)


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    return tmp_path / "keys"


@pytest.fixture
def store(tmp_path: Path, keys_dir: Path) -> Iterator[SqliteStore]:
    s = SqliteStore.create(tmp_path / STORE_FILENAME, keys_dir=keys_dir)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


def test_create_refuses_an_existing_file_and_open_refuses_a_missing_one(
    tmp_path: Path, keys_dir: Path
) -> None:
    path = tmp_path / STORE_FILENAME
    with pytest.raises(FileNotFoundError):
        SqliteStore.open(path, keys_dir=keys_dir)
    created = SqliteStore.create(path, keys_dir=keys_dir)
    created.close()
    with pytest.raises(FileExistsError):
        SqliteStore.create(path, keys_dir=keys_dir)
    opened = SqliteStore.open_or_create(path, keys_dir=keys_dir)
    try:
        assert opened.store_id == created.store_id
    finally:
        opened.close()


def test_open_or_create_creates_when_absent(tmp_path: Path, keys_dir: Path) -> None:
    path = tmp_path / "nested" / STORE_FILENAME
    s = SqliteStore.open_or_create(path, keys_dir=keys_dir)
    try:
        assert path.is_file()
        assert s.count_memories() == 0
    finally:
        s.close()


def test_the_connection_pragmas_match_the_declaration(store: SqliteStore) -> None:
    conn = store.conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA page_size").fetchone()[0] == sqlite_store.PAGE_SIZE
    assert sqlite_store.PAGE_SIZE == 8192


def test_meta_names_the_store_schema_tokenizer_and_engine(store: SqliteStore) -> None:
    from bettermemory import __version__
    from bettermemory.search import tokenizer_fingerprint

    meta = store.meta()
    assert meta["store_id"] == store.store_id
    assert meta["schema_version"] == "1"
    assert meta["tokenizer_fingerprint"] == tokenizer_fingerprint()
    assert meta["engine_version"] == __version__
    assert len(meta["key_fingerprint"]) == 64
    datetime.fromisoformat(meta["created"].replace("Z", "+00:00"))


def test_the_tables_are_the_declared_set(store: SqliteStore) -> None:
    names = {
        row[0]
        for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    expected = {
        "meta",
        "memories",
        "memories_fts",
        "tombstones",
        "memory_links",
        "episodes",
        "log",
        "verifications",
        "conflicts",
        "pending_writes",
        "imports",
        "quarantine",
    }
    assert expected <= names
    assert set(FOLDED_TABLES) == {
        "memories",
        "memory_links",
        "tombstones",
        "episodes",
        "verifications",
        "conflicts",
        "imports",
        "quarantine",
    }


# ---------------------------------------------------------------------------
# DDL parity with the v8 index
# ---------------------------------------------------------------------------


def _statement(sql: str, name: str) -> str:
    """The `CREATE ... IF NOT EXISTS <name>` statement in `sql`, verbatim.
    A trigger runs to its `END;`; anything else to its first `;`."""
    match = re.search(
        rf"CREATE (?:VIRTUAL TABLE|TABLE|TRIGGER|INDEX) IF NOT EXISTS {name}\b", sql
    )
    assert match is not None, name
    start = match.start()
    if "TRIGGER" in match.group(0):
        end = sql.index("END;", start) + len("END;")
    else:
        end = sql.index(";", start) + 1
    return sql[start:end]


@pytest.mark.parametrize(
    "name",
    [
        "memories_fts",
        "memories_ai",
        "memories_ad",
        "memories_au",
        "memories_by_updated",
        "memory_links",
        "memory_links_by_target",
        "memory_links_cleanup",
    ],
)
def test_the_fts_and_link_ddl_is_the_v8_index_ddl_verbatim(name: str) -> None:
    assert _statement(sqlite_store.SCHEMA, name) == _statement(v8_index._SCHEMA, name)


def test_the_memories_columns_start_with_the_v8_index_columns(
    store: SqliteStore, tmp_path: Path
) -> None:
    v8_root = tmp_path / "v8"
    v8 = V8Store.open(v8_root)
    v8.write(content="anything", scopes=["tools"])
    v8_conn = sqlite3.connect(str(v8_index.index_path(v8_root)))
    v8_columns = [r[1] for r in v8_conn.execute("PRAGMA table_info(memories)")]
    v8_conn.close()
    v9_columns = [r[1] for r in store.conn.execute("PRAGMA table_info(memories)")]
    kept = [c for c in v8_columns if c not in {"verified_locally_at", "content_sha256"}]
    assert v9_columns[: len(kept)] == kept
    assert "verified_locally_at" not in v9_columns
    assert "content_sha256" not in v9_columns


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_store_file_is_owner_only(store: SqliteStore) -> None:
    import stat

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


def test_a_memory_round_trips_through_every_field(store: SqliteStore) -> None:
    memory = _full_memory()
    store.put_memory(memory)
    back = store.get_memory(memory.id)
    assert back.model_dump(mode="json") == memory.model_dump(mode="json")
    assert back.origin is not None and back.origin.source == "roots"
    assert back.actor is not None and back.actor.sources == {
        "client": "client-info",
        "model": "header",
    }


def test_a_bare_memory_round_trips_with_its_defaults(store: SqliteStore) -> None:
    memory = _memory("plain")
    store.put_memory(memory)
    back = store.get_memory(memory.id)
    assert back == memory
    assert back.origin is None and back.actor is None
    assert back.links == [] and back.claims == []
    assert back.corroborations == 0 and back.last_corroborated is None


def test_put_memory_logs_the_record_and_its_provenance(store: SqliteStore) -> None:
    memory = _memory("plain")
    store.put_memory(memory, session="sess_a")
    row = store.log_rows()[-1]
    assert row.kind == "memory_put"
    assert row.session == "sess_a"
    payload = json.loads(row.payload)
    assert payload["memory"]["id"] == memory.id
    assert payload["memory"]["body"] == memory.body
    assert payload["provenance"] == LOCAL
    assert payload["filename"] is None


def test_put_memory_replaces_an_existing_record(store: SqliteStore) -> None:
    memory = _memory("first", "tools")
    store.put_memory(memory)
    updated = memory.model_copy(
        update={
            "body": "second\n",
            "scopes": ["projects:foo"],
            "updated": _NOW + timedelta(minutes=1),
        }
    )
    store.put_memory(updated)
    assert store.count_memories() == 1
    back = store.get_memory(memory.id)
    assert back.body == "second\n"
    assert back.scopes == ["projects:foo"]
    assert [r.kind for r in store.log_rows()][1:] == ["memory_put", "memory_put"]


def test_provenance_is_kept_across_updates_and_replaced_when_stated(
    store: SqliteStore,
) -> None:
    memory = _memory("imported one")
    store.put_memory(memory, provenance=IMPORTED, filename="2026-01-01-imported.md")
    assert store.provenance_for([memory.id]) == {memory.id: IMPORTED}
    store.put_memory(memory.model_copy(update={"body": "edited\n"}))
    assert store.provenance_for([memory.id]) == {memory.id: IMPORTED}
    store.put_memory(memory, provenance=LOCAL)
    assert store.provenance_for([memory.id]) == {memory.id: LOCAL}
    with pytest.raises(ValueError):
        store.put_memory(memory, provenance="untracked")


def test_the_filename_is_kept_for_the_export_mirror(store: SqliteStore) -> None:
    memory = _memory("imported one")
    store.put_memory(memory, provenance=IMPORTED, filename="2026-01-01-imported.md")
    assert store.filename_for(memory.id) == "2026-01-01-imported.md"
    store.put_memory(memory.model_copy(update={"body": "edited\n"}))
    assert store.filename_for(memory.id) == "2026-01-01-imported.md"
    other = _memory("native")
    store.put_memory(other)
    assert store.filename_for(other.id) is None


def test_get_memory_raises_for_an_unknown_or_tombstoned_id(
    store: SqliteStore,
) -> None:
    with pytest.raises(NotFoundError):
        store.get_memory(generate_ulid())
    memory = _memory("gone")
    store.put_memory(memory)
    store.tombstone(memory.id, "test")
    with pytest.raises(NotFoundError):
        store.get_memory(memory.id)
    assert not store.has_memory(memory.id)


def test_iter_memories_returns_insertion_order(store: SqliteStore) -> None:
    memories = [_memory(f"m{i}") for i in range(5)]
    for m in reversed(memories):
        store.put_memory(m)
    assert [m.id for m in store.iter_memories()] == [m.id for m in reversed(memories)]
    assert store.memory_ids() == [m.id for m in reversed(memories)]
    assert store.count_memories() == 5


def test_get_many_returns_the_found_records(store: SqliteStore) -> None:
    a, b = _memory("a"), _memory("b")
    store.put_memory(a)
    store.put_memory(b)
    found = store.get_many([b.id, generate_ulid(), a.id])
    assert [m.id for m in found] == [b.id, a.id]


# ---------------------------------------------------------------------------
# Tombstones
# ---------------------------------------------------------------------------


def test_tombstone_moves_the_record_and_logs_two_rows(store: SqliteStore) -> None:
    memory = _full_memory()
    store.put_memory(memory, session="sess_w")
    removed_at = _NOW + timedelta(days=2)
    dead = store.tombstone(memory.id, "superseded", session="sess_r", now=removed_at)
    assert isinstance(dead, TombstonedMemory)
    assert dead.id == memory.id
    assert dead.body == memory.body
    assert dead.removed == removed_at
    assert dead.removed_reason == "superseded"
    assert dead.removed_session == "sess_r"
    assert dead.verified_head == _HEAD
    assert dead.claims == memory.claims
    assert store.count_memories() == 0
    assert store.count_tombstones() == 1
    assert store.get_tombstone(memory.id) == dead
    kinds = [r.kind for r in store.log_rows()]
    assert kinds[-2:] == ["memory_delete", "tombstone_put"]
    assert store.log_rows()[-1].session == "sess_r"
    assert store.log_verify()["status"] == "ok"


def test_tombstone_of_an_unknown_id_raises(store: SqliteStore) -> None:
    with pytest.raises(NotFoundError):
        store.tombstone(generate_ulid(), "nothing there")


def test_tombstone_keeps_the_provenance_and_filename(store: SqliteStore) -> None:
    memory = _memory("imported")
    store.put_memory(memory, provenance=IMPORTED, filename="2026-01-01-imported.md")
    store.tombstone(memory.id, "gone")
    row = store.conn.execute(
        "SELECT provenance, filename FROM tombstones WHERE id = ?", (memory.id,)
    ).fetchone()
    assert (row["provenance"], row["filename"]) == (
        IMPORTED,
        "2026-01-01-imported.md",
    )


def test_restore_brings_the_record_back_as_local(store: SqliteStore) -> None:
    memory = _full_memory()
    store.put_memory(memory, provenance=IMPORTED)
    store.tombstone(memory.id, "oops")
    back = store.restore(memory.id, session="sess_x")
    assert back.model_dump(mode="json") == memory.model_dump(mode="json")
    assert store.count_tombstones() == 0
    assert store.provenance_for([memory.id]) == {memory.id: LOCAL}
    kinds = [r.kind for r in store.log_rows()]
    assert kinds[-2:] == ["tombstone_delete", "memory_put"]
    with pytest.raises(NotFoundError):
        store.restore(memory.id)
    assert store.log_verify()["status"] == "ok"


def test_delete_tombstone_prunes_it(store: SqliteStore) -> None:
    memory = _memory("prune me")
    store.put_memory(memory)
    store.tombstone(memory.id, "old")
    store.delete_tombstone(memory.id)
    assert store.count_tombstones() == 0
    assert store.log_rows()[-1].kind == "tombstone_delete"
    with pytest.raises(NotFoundError):
        store.delete_tombstone(memory.id)
    assert store.log_verify()["status"] == "ok"


def test_iter_tombstones_lists_them_newest_removal_last(store: SqliteStore) -> None:
    a, b = _memory("a"), _memory("b")
    store.put_memory(a)
    store.put_memory(b)
    store.tombstone(b.id, "b first", now=_NOW + timedelta(seconds=1))
    store.tombstone(a.id, "a second", now=_NOW + timedelta(seconds=2))
    assert [t.id for t in store.iter_tombstones()] == [b.id, a.id]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_links_for_many_matches_the_v8_index_shape(
    store: SqliteStore, tmp_path: Path
) -> None:
    v8_root = tmp_path / "v8"
    v8 = V8Store.open(v8_root)
    target = v8.write(content="the target", scopes=["tools"])
    source = v8.write(
        content="the source",
        scopes=["tools"],
        links=[
            MemoryLink(type=LinkType.SUPERSEDES, target_id=target.id, note="n"),
            MemoryLink(type=LinkType.EXTENDS, target_id=target.id),
        ],
    )
    for _, m in v8.iter_active():
        store.put_memory(m)
    expected, _ = v8_index.links_for_many(v8_root, [source.id, target.id])
    assert store.links_for_many([source.id, target.id]) == expected
    assert store.links_for_many([]) == {}
    unknown = generate_ulid()
    assert store.links_for_many([unknown]) == {unknown: ([], [])}


def test_tombstoning_a_target_drops_its_link_rows_but_not_the_record(
    store: SqliteStore,
) -> None:
    target = _memory("target")
    source = _memory(
        "source",
        links=[MemoryLink(type=LinkType.DEPENDS_ON, target_id=target.id)],
    )
    store.put_memory(target)
    store.put_memory(source)
    assert store.links_for_many([target.id])[target.id][1] == [
        ("depends_on", source.id, None)
    ]
    store.tombstone(target.id, "gone")
    assert store.links_for_many([target.id])[target.id] == ([], [])
    assert store.get_memory(source.id).links[0].target_id == target.id


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------


def test_an_episode_round_trips_and_lists_by_session(store: SqliteStore) -> None:
    first = _episode("sess_a")
    floor = _episode("sess_b", body="\n", takeaway=None, scopes=[], is_floor=True)
    store.put_episode(first, session="sess_a")
    store.put_episode(floor)
    assert store.get_episode(first.id).model_dump(mode="json") == first.model_dump(
        mode="json"
    )
    assert store.get_episode(floor.id).is_floor is True
    assert [e.id for e in store.iter_episodes()] == [first.id, floor.id]
    assert [e.id for e in store.iter_episodes(session_id="sess_b")] == [floor.id]
    assert store.count_episodes() == 2
    assert store.log_rows()[-1].kind == "episode_put"
    store.delete_episode(first.id)
    assert store.count_episodes() == 1
    with pytest.raises(NotFoundError):
        store.get_episode(first.id)
    with pytest.raises(NotFoundError):
        store.delete_episode(first.id)
    assert store.log_verify()["status"] == "ok"


def test_episodes_are_not_search_candidates(store: SqliteStore) -> None:
    store.put_episode(_episode(body="kubernetes networking notes\n"))
    assert store.query_candidates("kubernetes networking") == []


# ---------------------------------------------------------------------------
# Verifications, conflicts, imports, quarantine
# ---------------------------------------------------------------------------


def test_a_verification_row_records_this_host_stamp(store: SqliteStore) -> None:
    memory = _memory("verified")
    store.put_memory(memory)
    at = _NOW + timedelta(hours=3)
    store.put_verification(
        memory.id,
        verified_at=at,
        verified_head=_HEAD,
        verified_paths=["src/a.py"],
        verified_commits=["abc1234"],
        verified_versions=["8.0.0"],
        verified_absent_paths=["tools/gone.py"],
        session="sess_v",
    )
    rows = store.verifications_for([memory.id])
    assert rows == {
        memory.id: [
            {
                "memory_id": memory.id,
                "machine_id": None,
                "verified_at": at.isoformat(),
                "verified_head": _HEAD,
                "verified_paths": ["src/a.py"],
                "verified_commits": ["abc1234"],
                "verified_versions": ["8.0.0"],
                "verified_absent_paths": ["tools/gone.py"],
            }
        ]
    }
    later = at + timedelta(hours=1)
    store.put_verification(memory.id, verified_at=later)
    assert len(store.verifications_for([memory.id])[memory.id]) == 1
    assert store.verifications_for([memory.id])[memory.id][0]["verified_at"] == (
        later.isoformat()
    )
    store.put_verification(memory.id, verified_at=later, machine_id="mach-2")
    assert len(store.verifications_for([memory.id])[memory.id]) == 2
    store.delete_verification(memory.id, machine_id="mach-2")
    store.delete_verification(memory.id)
    assert store.verifications_for([memory.id]) == {}
    assert store.log_verify()["status"] == "ok"


def test_conflicts_imports_and_quarantine_round_trip(store: SqliteStore) -> None:
    conflict = {
        "id": "pair-1",
        "a_id": generate_ulid(),
        "b_id": generate_ulid(),
        "summary_a": "a",
        "summary_b": "b",
        "similarity": 0.5,
        "method": "polarity",
        "detector": "numeric",
        "created": "2026-09-26T00:00:00Z",
        "status": "pending",
        "verdict_ts": None,
        "note": None,
        "verdict_hash_a": None,
        "verdict_hash_b": None,
    }
    store.put_conflict(conflict)
    assert store.list_conflicts() == [conflict]
    store.put_conflict({**conflict, "status": "confirmed"})
    assert store.list_conflicts()[0]["status"] == "confirmed"
    store.delete_conflict("pair-1")
    assert store.list_conflicts() == []

    store.put_import(
        "/w/notes.md",
        content_hash="sha256:abc",
        imported_at="2026-09-26T00:00:00Z",
        memory_id=generate_ulid(),
    )
    assert store.list_imports()[0]["source"] == "/w/notes.md"
    store.delete_import("/w/notes.md")
    assert store.list_imports() == []

    store.put_quarantine(
        "2026-01-01-bad.md",
        reason="credential",
        detail="a token shape",
        remote="origin",
        at="2026-09-26T00:00:00Z",
        size=12,
        sha256="f" * 64,
    )
    assert store.list_quarantine()[0]["reason"] == "credential"
    store.delete_quarantine("2026-01-01-bad.md")
    assert store.list_quarantine() == []
    kinds = [r.kind for r in store.log_rows()]
    for kind in (
        "conflict_put",
        "conflict_delete",
        "import_put",
        "import_delete",
        "quarantine_put",
        "quarantine_delete",
    ):
        assert kind in kinds and kind in MUTATION_KINDS
    assert store.log_verify()["status"] == "ok"


# ---------------------------------------------------------------------------
# The candidate query against the v8 index
# ---------------------------------------------------------------------------


_DOCS = [
    ("python list comprehension and generator expressions", ["tools"]),
    ("kubernetes networking with calico and cilium", ["infrastructure"]),
    ("kubernetes ingress controllers compared", ["infrastructure", "projects:foo"]),
    ("claude-code hooks fire on session start", ["tools", "projects:foo"]),
    ("the deploy script reads DEPLOY_TOKEN from the environment", ["projects:foo"]),
    ("docker-containers restart on failure by policy", ["infrastructure"]),
    ("python packaging with uv and hatchling", ["tools"]),
    ("networking notes for the homelab box", ["infrastructure", "personal"]),
]


def _paired_stores(tmp_path: Path, keys_dir: Path) -> tuple[Path, SqliteStore]:
    v8_root = tmp_path / "v8"
    v8 = V8Store.open(v8_root)
    for i, (body, scopes) in enumerate(_DOCS):
        v8.write(
            content=body,
            scopes=scopes,
            actor=Actor(client="claude-code") if i % 2 else None,
        )
    v8_index.rebuild(v8_root, v8.iter_active())
    s9 = SqliteStore.create(tmp_path / STORE_FILENAME, keys_dir=keys_dir)
    for _, memory in v8.iter_active():
        s9.put_memory(memory)
    return v8_root, s9


@pytest.mark.parametrize(
    ("query", "scopes", "client"),
    [
        ("kubernetes networking", None, None),
        ("kubernetes networking", ["infrastructure"], None),
        ("kubernetes", ["projects:foo", "personal"], None),
        ("python", None, "claude-code"),
        ("python", None, "cursor"),
        ("docker-containers", None, None),
        ("claude-code hooks", ["tools"], "claude-code"),
        ("nothing matches this", None, None),
        ("", None, None),
        ('"quoted" AND (special:chars*)', None, None),
    ],
)
def test_query_candidates_equals_the_v8_index_query(
    tmp_path: Path,
    keys_dir: Path,
    query: str,
    scopes: list[str] | None,
    client: str | None,
) -> None:
    v8_root, s9 = _paired_stores(tmp_path, keys_dir)
    try:
        for max_results in (100, 2):
            expected = v8_index.query(
                v8_root, query, scopes=scopes, client=client, max_results=max_results
            )
            got = s9.query_candidates(
                query, scopes=scopes, client=client, max_results=max_results
            )
            assert got == expected
    finally:
        s9.close()


def test_query_candidates_reflects_updates_and_tombstones(store: SqliteStore) -> None:
    memory = _memory("kubernetes networking")
    store.put_memory(memory)
    assert [i for i, _ in store.query_candidates("kubernetes")] == [memory.id]
    store.put_memory(memory.model_copy(update={"body": "python packaging\n"}))
    assert store.query_candidates("kubernetes") == []
    assert [i for i, _ in store.query_candidates("python")] == [memory.id]
    store.tombstone(memory.id, "gone")
    assert store.query_candidates("python") == []
    store.restore(memory.id)
    assert [i for i, _ in store.query_candidates("python")] == [memory.id]


def test_provenance_for_omits_unknown_ids(store: SqliteStore) -> None:
    memory = _memory("known")
    store.put_memory(memory)
    unknown = generate_ulid()
    assert store.provenance_for([memory.id, unknown]) == {memory.id: LOCAL}
    assert store.provenance_for([]) == {}


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


_body = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=40
)
_ops = st.lists(
    st.tuples(
        st.sampled_from(
            [
                "put",
                "update",
                "tombstone",
                "restore",
                "prune",
                "episode",
                "drop_episode",
                "verify",
                "event",
            ]
        ),
        st.integers(min_value=0, max_value=4),
        _body,
    ),
    max_size=25,
)


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(ops=_ops)
def test_any_sequence_of_mutations_refolds_to_the_tables(ops: list[Any]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        store = SqliteStore.create(base / STORE_FILENAME, keys_dir=base / "keys")
        try:
            active: dict[int, Memory] = {}
            dead: set[int] = set()
            episodes: dict[int, Episode] = {}
            ids: dict[int, str] = {}
            for op, slot, text in ops:
                if op == "put" and slot not in active and slot not in dead:
                    memory = _memory(text)
                    ids[slot] = memory.id
                    store.put_memory(memory)
                    active[slot] = memory
                elif op == "update" and slot in active:
                    memory = active[slot].model_copy(update={"body": text + "\n"})
                    store.put_memory(memory)
                    active[slot] = memory
                elif op == "tombstone" and slot in active:
                    store.tombstone(active.pop(slot).id, text)
                    dead.add(slot)
                elif op == "restore" and slot in dead:
                    dead.discard(slot)
                    active[slot] = store.restore(ids[slot])
                elif op == "prune" and slot in dead:
                    dead.discard(slot)
                    store.delete_tombstone(ids[slot])
                elif op == "episode":
                    episode = _episode(f"sess_{slot}", body=text + "\n")
                    store.put_episode(episode)
                    episodes[slot] = episode
                elif op == "drop_episode" and slot in episodes:
                    store.delete_episode(episodes.pop(slot).id)
                elif op == "verify" and slot in active:
                    store.put_verification(
                        active[slot].id, verified_at=_NOW, verified_paths=[text]
                    )
                elif op == "event":
                    store.record_event("search", session="sess_h", query=text)
            report = store.log_verify()
            assert report["status"] == "ok", report
            assert report["fold"]["status"] == "ok"
            assert store.count_memories() == len(active)
            assert store.count_tombstones() == len(dead)
            if active:
                victim = next(iter(active.values()))
                outside = sqlite3.connect(str(store.path))
                outside.execute(
                    "UPDATE memories SET body = body || 'x' WHERE id = ?", (victim.id,)
                )
                outside.commit()
                outside.close()
                report = store.log_verify()
                assert report["status"] == "tampered"
                assert report["unaccounted_ids"] == [victim.id]
        finally:
            store.close()


def test_refold_alone_reports_the_tables(store: SqliteStore) -> None:
    memory = _memory("alpha")
    store.put_memory(memory)
    fold = store.refold()
    assert fold["status"] == "ok"
    assert fold["tables"]["memories"] == {"unaccounted": [], "missing": [], "rows": 1}
    assert fold["tables"]["episodes"]["rows"] == 0


# ---------------------------------------------------------------------------
# Status and the CLI
# ---------------------------------------------------------------------------


def test_status_summarises_the_store(store: SqliteStore) -> None:
    store.put_memory(_memory("alpha"))
    store.put_episode(_episode())
    status = store.status()
    assert status["path"] == str(store.path)
    assert status["store_id"] == store.store_id
    assert status["schema_version"] == 1
    assert status["memories"] == 1
    assert status["tombstones"] == 0
    assert status["episodes"] == 1
    assert status["log_rows"] == 3
    assert status["size_bytes"] > 0
    assert status["key_present"] is True
    assert status["key_fingerprint"] == store.key_fingerprint
    assert status["head_seq"] == 3


def _run_cli(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    from bettermemory.cli import main

    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    return int(exc.value.code or 0)


def test_log_verify_command_reports_ok_then_tampering(
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    monkeypatch.setattr("bettermemory.log.default_keys_dir", lambda: keys_dir)
    s = SqliteStore.create(store_dir / STORE_FILENAME, keys_dir=keys_dir)
    memory = _memory("alpha")
    s.put_memory(memory)
    s.close()

    assert _run_cli(["log", "verify", "--json"], monkeypatch) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["rows"] == 2

    assert _run_cli(["log", "verify"], monkeypatch) == 0
    text = capsys.readouterr().out
    assert "ok" in text and str(store_dir / STORE_FILENAME) in text

    outside = sqlite3.connect(str(store_dir / STORE_FILENAME))
    outside.execute("UPDATE memories SET body = 'x' WHERE id = ?", (memory.id,))
    outside.commit()
    outside.close()
    assert _run_cli(["log", "verify"], monkeypatch) == 1
    text = capsys.readouterr().out
    assert "tampered" in text and memory.id in text


def test_log_verify_command_refuses_a_directory_without_a_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    assert _run_cli(["log", "verify"], monkeypatch) == 2
    assert STORE_FILENAME in capsys.readouterr().err


def test_log_without_a_subcommand_prints_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(["log"], monkeypatch) == 2
    assert "verify" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# U3: batches, direct tombstones, imported events, the migration control row
# ---------------------------------------------------------------------------


def test_a_batch_commits_everything_together_and_moves_the_head_once(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[int] = []
    original = store.keyring.write_head

    def counting(*, seq: int, mac: str) -> None:
        writes.append(seq)
        original(seq=seq, mac=mac)

    monkeypatch.setattr(store.keyring, "write_head", counting)
    a, b = _memory("alpha"), _memory("beta")
    with store.batch() as batched:
        assert batched is store
        store.put_memory(a)
        store.put_memory(b)
        store.put_episode(_episode())
        store.tombstone(a.id, "gone inside the batch")
    assert store.count_memories() == 1 and store.count_tombstones() == 1
    assert len(store.log_rows()) == 6
    head = store.keyring.read_head()
    assert head is not None and head.seq == 6
    assert writes == [6]

    with pytest.raises(RuntimeError, match="stop"):
        with store.batch():
            store.put_memory(_memory("gamma"))
            store.delete_tombstone(a.id)
            raise RuntimeError("stop")
    assert store.count_memories() == 1 and store.count_tombstones() == 1
    assert len(store.log_rows()) == 6
    assert writes == [6]

    store.put_memory(_memory("delta"))
    assert store.count_memories() == 2 and writes == [6, 7]
    assert store.log_verify()["status"] == "ok"

    with store.batch():
        with pytest.raises(RuntimeError, match="already open"):
            with store.batch():
                pass


def test_put_tombstone_inserts_directly_and_refuses_an_active_id(
    store: SqliteStore,
) -> None:
    memory = _full_memory()
    dead = TombstonedMemory(
        **{
            name: getattr(memory, name)
            for name in TombstonedMemory.model_fields
            if name not in ("removed", "removed_reason", "removed_session")
        },
        removed=_NOW + timedelta(days=2),
        removed_reason="gone",
        removed_session="sess_x",
    )
    store.put_tombstone(
        dead,
        provenance=IMPORTED,
        filename="2026-09-26-full.md",
        links=memory.links,
        corroborations=memory.corroborations,
        last_corroborated=memory.last_corroborated,
    )
    assert store.get_tombstone(memory.id) == dead
    row = store.conn.execute(
        "SELECT * FROM tombstones WHERE id = ?", (memory.id,)
    ).fetchone()
    assert row["provenance"] == IMPORTED
    assert row["filename"] == "2026-09-26-full.md"
    assert json.loads(row["links_json"]) == [
        link.model_dump(mode="json", exclude_none=True) for link in memory.links
    ]
    assert row["corroborations"] == 2
    assert memory.last_corroborated is not None
    assert row["last_corroborated"] == memory.last_corroborated.isoformat()

    restored = store.restore(memory.id)
    assert restored == memory
    assert store.filename_for(memory.id) == "2026-09-26-full.md"
    with pytest.raises(ValueError, match="active"):
        store.put_tombstone(dead)
    with pytest.raises(ValueError, match="provenance"):
        store.put_tombstone(
            TombstonedMemory(**{**dead.model_dump(), "id": generate_ulid()}),
            provenance="elsewhere",
        )

    other = _memory("another record")
    other_dead = TombstonedMemory(
        **{
            name: getattr(other, name)
            for name in TombstonedMemory.model_fields
            if name not in ("removed", "removed_reason", "removed_session")
        },
        removed=_NOW,
        removed_reason="gone",
    )
    store.put_tombstone(other_dead, links=[{"type": "extends", "target_id": memory.id}])
    row = store.conn.execute(
        "SELECT provenance, filename, links_json FROM tombstones WHERE id = ?",
        (other.id,),
    ).fetchone()
    assert row["provenance"] == LOCAL and row["filename"] is None
    assert json.loads(row["links_json"]) == [
        {"target_id": memory.id, "type": "extends"}
    ]
    assert store.log_verify()["status"] == "ok"


def test_import_event_keeps_the_original_ts_and_session_and_redacts(
    store: SqliteStore,
) -> None:
    event = {
        "ts": "2026-07-20T04:17:40.971883Z",
        "session": "sess_v8",
        "kind": "search",
        "id": "01ABC",
        "query": "kubernetes networking secrets and more words here",
        "probe_query": {"hash": "x", "preview": "p", "len": 3},
        "returned": ["01ABC"],
    }
    row = store.import_event(event)
    assert row.ts == event["ts"] and row.session == "sess_v8"
    assert row.kind == "search" and row.seq == 2
    payload = json.loads(row.payload)
    assert payload["imported_from"] == "v8"
    assert set(payload["query"]) == {"hash", "preview", "len"}
    assert payload["query"]["preview"] == str(event["query"])[:32]
    assert payload["probe_query"] == event["probe_query"]
    assert not {"ts", "session", "kind"} & set(payload)
    assert list(store.iter_events()) == [
        {
            "ts": event["ts"],
            "session": "sess_v8",
            "kind": "search",
            "id": "01ABC",
            "query": payload["query"],
            "probe_query": event["probe_query"],
            "returned": ["01ABC"],
            "imported_from": "v8",
        }
    ]
    assert store.imported_event_keys() == {(row.ts, row.session, row.kind, row.payload)}

    store.record_event("show", session="sess_live", id="01ABC")
    assert len(store.imported_event_keys()) == 1
    assert [e["kind"] for e in store.iter_events()] == ["search", "show"]
    assert [e["kind"] for e in store.iter_events(since_seq=2)] == ["show"]

    unstamped = store.import_event({"kind": "list", "session": None})
    assert unstamped.session is None and unstamped.ts.endswith("Z")
    for bad in (
        {"kind": "memory_put"},
        {"kind": STORE_CREATED},
        {"kind": MIGRATE_V8},
        {"kind": ""},
        {"ts": "x"},
    ):
        with pytest.raises(ValueError):
            store.import_event(bad)
    assert store.log_verify()["status"] == "ok"


def test_record_migration_is_a_control_row_the_fold_ignores(
    store: SqliteStore,
) -> None:
    row = store.record_migration(source="the-v8-directory", counts={"memories": 1})
    assert row.kind == MIGRATE_V8 and row.seq == 2
    payload = json.loads(row.payload)
    assert payload["source"] == "the-v8-directory"
    assert payload["counts"] == {"memories": 1}
    assert payload["imported_from"] == "v8"
    assert payload["engine_version"]
    store.put_memory(_memory("alpha"))
    assert store.refold()["status"] == "ok"
    assert store.log_verify()["status"] == "ok"
    assert list(store.iter_events()) == []
    with pytest.raises(ValueError):
        store.record_event(MIGRATE_V8)


def test_the_row_iterators_carry_the_filename_and_provenance(
    store: SqliteStore,
) -> None:
    memory = _full_memory()
    store.put_memory(memory, provenance=IMPORTED, filename="x.md")
    rows = list(store.iter_memory_rows())
    assert rows[0].memory == memory
    assert rows[0].filename == "x.md" and rows[0].provenance == IMPORTED
    store.tombstone(memory.id, "gone")
    assert list(store.iter_memory_rows()) == []
    dead = list(store.iter_tombstone_rows())[0]
    assert dead.tombstone.id == memory.id
    assert dead.filename == "x.md" and dead.provenance == IMPORTED
    assert [link["target_id"] for link in dead.links] == [
        link.target_id for link in memory.links
    ]
    assert dead.corroborations == 2
    assert dead.last_corroborated == memory.last_corroborated
