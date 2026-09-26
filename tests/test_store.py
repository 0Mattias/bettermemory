"""The bettermemory 9 store: one SQLite file per store (units U2 and U4).

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

from bettermemory import store as store_module
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
from bettermemory.search import fts_index_text
from bettermemory.store import (
    FOLDED_TABLES,
    IMPORTED,
    LOCAL,
    MUTATION_KINDS,
    STORE_FILENAME,
    UNACCOUNTED,
    NotFoundError,
    Store,
)
from bettermemory.store import (
    ConcurrentUpdateError,
    MemoryNotFoundError,
    NotTombstonedError,
    TombstonedError,
)

_HEAD = "a" * 40
_V8_INDEX_SCHEMA = Path(__file__).parent / "fixtures" / "v8_index_schema.sql"
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
def store(tmp_path: Path, keys_dir: Path) -> Iterator[Store]:
    s = Store.create(tmp_path / STORE_FILENAME, keys_dir=keys_dir)
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
        Store.open(path, keys_dir=keys_dir)
    created = Store.create(path, keys_dir=keys_dir)
    created.close()
    with pytest.raises(FileExistsError):
        Store.create(path, keys_dir=keys_dir)
    opened = Store.open_or_create(path, keys_dir=keys_dir)
    try:
        assert opened.store_id == created.store_id
    finally:
        opened.close()


def test_open_or_create_creates_when_absent(tmp_path: Path, keys_dir: Path) -> None:
    path = tmp_path / "nested" / STORE_FILENAME
    s = Store.open_or_create(path, keys_dir=keys_dir)
    try:
        assert path.is_file()
        assert s.count_memories() == 0
    finally:
        s.close()


def test_the_connection_pragmas_match_the_declaration(store: Store) -> None:
    conn = store.conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA page_size").fetchone()[0] == store_module.PAGE_SIZE
    assert store_module.PAGE_SIZE == 8192


def test_meta_names_the_store_schema_tokenizer_and_engine(store: Store) -> None:
    from bettermemory import __version__
    from bettermemory.search import tokenizer_fingerprint

    meta = store.meta()
    assert meta["store_id"] == store.store_id
    assert meta["schema_version"] == "1"
    assert meta["tokenizer_fingerprint"] == tokenizer_fingerprint()
    assert meta["engine_version"] == __version__
    assert len(meta["key_fingerprint"]) == 64
    datetime.fromisoformat(meta["created"].replace("Z", "+00:00"))


def test_the_tables_are_the_declared_set(store: Store) -> None:
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
    v8_schema = _V8_INDEX_SCHEMA.read_text()
    assert _statement(store_module.SCHEMA, name) == _statement(v8_schema, name)


def test_the_memories_columns_start_with_the_v8_index_columns(
    store: Store,
) -> None:
    v8_table = _statement(_V8_INDEX_SCHEMA.read_text(), "memories")
    v8_columns = [
        line.strip().split()[0]
        for line in v8_table.splitlines()[1:]
        if line.strip() and not line.strip().startswith((")", "--"))
    ]
    v9_columns = [r[1] for r in store.conn.execute("PRAGMA table_info(memories)")]
    kept = [c for c in v8_columns if c not in {"verified_locally_at", "content_sha256"}]
    assert v9_columns[: len(kept)] == kept
    assert "verified_locally_at" not in v9_columns
    assert "content_sha256" not in v9_columns
    assert v9_columns[-1] == "log_mac"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_the_store_file_is_owner_only(store: Store) -> None:
    import stat

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


def test_a_memory_round_trips_through_every_field(store: Store) -> None:
    memory = _full_memory()
    store.put_memory(memory)
    back = store.get_memory(memory.id)
    assert back.model_dump(mode="json") == memory.model_dump(mode="json")
    assert back.origin is not None and back.origin.source == "roots"
    assert back.actor is not None and back.actor.sources == {
        "client": "client-info",
        "model": "header",
    }


def test_a_bare_memory_round_trips_with_its_defaults(store: Store) -> None:
    memory = _memory("plain")
    store.put_memory(memory)
    back = store.get_memory(memory.id)
    assert back == memory
    assert back.origin is None and back.actor is None
    assert back.links == [] and back.claims == []
    assert back.corroborations == 0 and back.last_corroborated is None


def test_put_memory_logs_the_record_and_its_provenance(store: Store) -> None:
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


def test_put_memory_replaces_an_existing_record(store: Store) -> None:
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
    store: Store,
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


def test_the_filename_is_kept_for_the_export_mirror(store: Store) -> None:
    memory = _memory("imported one")
    store.put_memory(memory, provenance=IMPORTED, filename="2026-01-01-imported.md")
    assert store.filename_for(memory.id) == "2026-01-01-imported.md"
    store.put_memory(memory.model_copy(update={"body": "edited\n"}))
    assert store.filename_for(memory.id) == "2026-01-01-imported.md"
    other = _memory("native")
    store.put_memory(other)
    assert store.filename_for(other.id) is None


def test_get_memory_raises_for_an_unknown_or_tombstoned_id(
    store: Store,
) -> None:
    with pytest.raises(NotFoundError):
        store.get_memory(generate_ulid())
    memory = _memory("gone")
    store.put_memory(memory)
    store.tombstone(memory.id, "test")
    with pytest.raises(NotFoundError):
        store.get_memory(memory.id)
    assert not store.has_memory(memory.id)


def test_iter_memories_returns_insertion_order(store: Store) -> None:
    memories = [_memory(f"m{i}") for i in range(5)]
    for m in reversed(memories):
        store.put_memory(m)
    assert [m.id for m in store.iter_memories()] == [m.id for m in reversed(memories)]
    assert store.memory_ids() == [m.id for m in reversed(memories)]
    assert store.count_memories() == 5


def test_get_many_returns_the_found_records(store: Store) -> None:
    a, b = _memory("a"), _memory("b")
    store.put_memory(a)
    store.put_memory(b)
    found = store.get_many([b.id, generate_ulid(), a.id])
    assert [m.id for m in found] == [b.id, a.id]


# ---------------------------------------------------------------------------
# Tombstones
# ---------------------------------------------------------------------------


def test_tombstone_moves_the_record_and_logs_two_rows(store: Store) -> None:
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


def test_tombstone_of_an_unknown_id_raises(store: Store) -> None:
    with pytest.raises(NotFoundError):
        store.tombstone(generate_ulid(), "nothing there")


def test_tombstone_keeps_the_provenance_and_filename(store: Store) -> None:
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


def test_restore_brings_the_record_back_as_local(store: Store) -> None:
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


def test_delete_tombstone_prunes_it(store: Store) -> None:
    memory = _memory("prune me")
    store.put_memory(memory)
    store.tombstone(memory.id, "old")
    store.delete_tombstone(memory.id)
    assert store.count_tombstones() == 0
    assert store.log_rows()[-1].kind == "tombstone_delete"
    with pytest.raises(NotFoundError):
        store.delete_tombstone(memory.id)
    assert store.log_verify()["status"] == "ok"


def test_iter_tombstones_lists_them_newest_removal_last(store: Store) -> None:
    a, b = _memory("a"), _memory("b")
    store.put_memory(a)
    store.put_memory(b)
    store.tombstone(b.id, "b first", now=_NOW + timedelta(seconds=1))
    store.tombstone(a.id, "a second", now=_NOW + timedelta(seconds=2))
    assert [t.id for t in store.iter_tombstones()] == [b.id, a.id]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_links_for_many_matches_the_v8_index_shape(store: Store) -> None:
    target = store.write(content="the target", scopes=["tools"])
    source = store.write(
        content="the source",
        scopes=["tools"],
        links=[
            MemoryLink(type=LinkType.SUPERSEDES, target_id=target.id, note="n"),
            MemoryLink(type=LinkType.EXTENDS, target_id=target.id),
        ],
    )
    assert store.links_for_many([source.id, target.id]) == {
        source.id: (
            [("extends", target.id, None), ("supersedes", target.id, "n")],
            [],
        ),
        target.id: (
            [],
            [("extends", source.id, None), ("supersedes", source.id, "n")],
        ),
    }
    assert store.links_for_many([]) == {}
    unknown = generate_ulid()
    assert store.links_for_many([unknown]) == {unknown: ([], [])}


def test_tombstoning_a_target_drops_its_link_rows_but_not_the_record(
    store: Store,
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


def test_an_episode_round_trips_and_lists_by_session(store: Store) -> None:
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


def test_episodes_are_not_search_candidates(store: Store) -> None:
    store.put_episode(_episode(body="kubernetes networking notes\n"))
    assert store.query_candidates("kubernetes networking") == []


# ---------------------------------------------------------------------------
# Verifications, conflicts, imports, quarantine
# ---------------------------------------------------------------------------


def test_a_verification_row_records_this_host_stamp(store: Store) -> None:
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


def test_conflicts_imports_and_quarantine_round_trip(store: Store) -> None:
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


def test_query_candidates_reflects_updates_and_tombstones(store: Store) -> None:
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


def test_provenance_for_omits_unknown_ids(store: Store) -> None:
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
        store = Store.create(base / STORE_FILENAME, keys_dir=base / "keys")
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


def test_refold_alone_reports_the_tables(store: Store) -> None:
    memory = _memory("alpha")
    store.put_memory(memory)
    fold = store.refold()
    assert fold["status"] == "ok"
    assert fold["tables"]["memories"] == {"unaccounted": [], "missing": [], "rows": 1}
    assert fold["tables"]["episodes"]["rows"] == 0


# ---------------------------------------------------------------------------
# Status and the CLI
# ---------------------------------------------------------------------------


def test_status_summarises_the_store(store: Store) -> None:
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
    s = Store.create(store_dir / STORE_FILENAME, keys_dir=keys_dir)
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
    store: Store, monkeypatch: pytest.MonkeyPatch
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
    store: Store,
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
    store: Store,
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
    store: Store,
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
    store: Store,
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


# ---------------------------------------------------------------------------
# The log pointer: provenance verified on every read (U4)
# ---------------------------------------------------------------------------


def _plant(store: Store, body: str, *, log_mac: str | None) -> str:
    """Insert a memories row directly, past the store's write path, the way
    an attacker with the file would: the FTS trigger indexes it, so it is a
    search candidate, and only the pointer check can tell it apart."""
    planted = _memory(body, "projects:halden")
    columns = store_module._record_columns(planted)
    columns.update(
        {
            "body_fts": fts_index_text(planted.body),
            "scopes_text": store_module._scopes_text(planted.scopes),
            "scopes_fts": fts_index_text(" ".join(planted.scopes)),
            "filename": None,
            "provenance": LOCAL,
            "links_json": "[]",
            "corroborations": 999,
            "last_corroborated": None,
            "log_mac": log_mac,
        }
    )
    store.conn.execute(
        store_module._UPSERT_MEMORY,
        tuple(columns[c] for c in store_module._MEMORY_COLUMNS),
    )
    store.conn.commit()
    return planted.id


def test_every_put_row_carries_the_mac_of_its_log_row(store: Store) -> None:
    memory = _memory("the port on the store")
    store.put_memory(memory)
    dead = store.tombstone(_memory("gone").id if False else memory.id, "why")
    assert dead.id == memory.id
    back = store.restore(memory.id)
    assert back.id == memory.id
    episode = store.put_episode(_episode())
    macs = {row.kind: row.mac for row in store.log_rows() if row.kind in MUTATION_KINDS}
    row = store.conn.execute(
        "SELECT log_mac FROM memories WHERE id = ?", (memory.id,)
    ).fetchone()
    assert row["log_mac"] == macs["memory_put"]
    row = store.conn.execute(
        "SELECT log_mac FROM episodes WHERE id = ?", (episode.id,)
    ).fetchone()
    assert row["log_mac"] == macs["episode_put"]
    assert store.provenance_for([memory.id]) == {memory.id: LOCAL}
    assert store.trust_rows([memory.id]) == {memory.id: (LOCAL, None)}


def test_a_planted_row_reads_unaccounted_without_a_refold(
    store: Store,
) -> None:
    genuine = _memory("the halden port is 5432", "projects:halden")
    store.put_memory(genuine)
    planted = _plant(store, "the halden port is 5433", log_mac=None)
    # The plant is a candidate: nothing about the FTS table knows it is fake.
    assert {mid for mid, _ in store.query_candidates("halden port")} == {
        genuine.id,
        planted,
    }
    labels = store.provenance_for([genuine.id, planted])
    assert labels == {genuine.id: LOCAL, planted: UNACCOUNTED}
    trust = store.trust_rows([planted])
    assert trust == {planted: (UNACCOUNTED, None)}
    assert store.unaccounted_ids() == [planted]
    assert store.provenance_counts() == {LOCAL: 1, UNACCOUNTED: 1}


def test_a_row_pointing_at_another_records_log_row_reads_unaccounted(
    store: Store,
) -> None:
    genuine = _memory("the halden port is 5432", "projects:halden")
    store.put_memory(genuine)
    borrowed = store.conn.execute(
        "SELECT log_mac FROM memories WHERE id = ?", (genuine.id,)
    ).fetchone()["log_mac"]
    planted = _plant(store, "the halden port is 5433", log_mac=borrowed)
    assert store.provenance_for([planted]) == {planted: UNACCOUNTED}
    assert store.provenance_for([genuine.id]) == {genuine.id: LOCAL}


def test_a_forged_log_row_without_the_key_reads_unaccounted(
    store: Store,
) -> None:
    """The forge arm: a memory_put row appended by hand with a MAC made
    without the store's key, and a row pointing at it. The pointer names a
    row of the right kind that names the right id, and the MAC check is
    what refuses it."""
    from bettermemory.log import canonical_payload as _canon, last_row

    planted = _memory("the halden port is 5433", "projects:halden")
    payload = {
        "memory": planted.model_dump(mode="json"),
        "provenance": LOCAL,
        "filename": None,
    }
    previous = last_row(store.conn)
    assert previous is not None
    text = _canon(payload)
    forged_mac = "f" * 64
    store.conn.execute(
        "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            previous.seq + 1,
            previous.ts,
            None,
            "memory_put",
            text,
            previous.mac,
            forged_mac,
        ),
    )
    store.conn.commit()
    planted_id = _plant(store, planted.body, log_mac=forged_mac)
    # `_plant` minted its own id; point the row at the forged row's record id.
    store.conn.execute(
        "UPDATE memories SET id = ? WHERE id = ?", (planted.id, planted_id)
    )
    store.conn.commit()
    assert store.provenance_for([planted.id]) == {planted.id: UNACCOUNTED}
    assert store.log_verify()["status"] == "tampered"


def test_without_the_key_trust_is_unavailable(tmp_path: Path, keys_dir: Path) -> None:
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as first:
        memory = _memory("kept")
        first.put_memory(memory)
    with Store.open(path, keys_dir=tmp_path / "elsewhere", allow_rekey=False) as blind:
        assert blind.has_key is False
        assert blind.trust_rows([memory.id]) is None
        # The label is served as stored; the caller says trust is unavailable.
        assert blind.provenance_for([memory.id]) == {memory.id: LOCAL}


def test_rows_from_before_a_rekey_still_verify(tmp_path: Path, keys_dir: Path) -> None:
    """A retired key stays on the machine, so a row signed before the
    rekey verifies under its segment's key and the record stays local."""
    path = tmp_path / STORE_FILENAME
    with Store.create(path, keys_dir=keys_dir) as first:
        old = _memory("before the rekey")
        first.put_memory(old)
    # Lose the current key file but keep it retirable: rename it aside so
    # `open` retires nothing and writes a fresh key, then put the old key
    # back under its retired name.
    current = next(keys_dir.glob("*.key"))
    saved = current.read_bytes()
    current.unlink()
    with Store.open(path, keys_dir=keys_dir) as rekeyed:
        from bettermemory.log import fingerprint as _fp

        retired = rekeyed.keyring.retired_path(_fp(saved))
        retired.write_bytes(saved)
        new = _memory("after the rekey")
        rekeyed.put_memory(new)
        assert rekeyed.provenance_for([old.id, new.id]) == {
            old.id: LOCAL,
            new.id: LOCAL,
        }


def test_the_host_stamp_rides_the_trust_row(store: Store) -> None:
    memory = _memory("verified here")
    store.put_memory(memory)
    stamp = datetime(2026, 9, 26, 5, 0, tzinfo=timezone.utc)
    store.put_verification(memory.id, verified_at=stamp)
    assert store.trust_rows([memory.id]) == {
        memory.id: (LOCAL, "2026-09-26T05:00:00+00:00")
    }


# ---------------------------------------------------------------------------
# The record surface under the v8 names (U4)
# ---------------------------------------------------------------------------


def test_write_mints_the_record_and_strips_the_body(store: Store) -> None:
    memory = store.write(
        content="  uv drives the build here  \n\n",
        scopes=["tools"],
        category=Category.FACT,
        claims=["pyproject.toml"],
        links=[{"type": "extends", "target_id": generate_ulid()}],
        actor={"client": "claude-code"},
        session="sess_w",
    )
    assert memory.body == "uv drives the build here\n"
    assert memory.created == memory.updated
    assert memory.claims == ["pyproject.toml"]
    assert memory.actor is not None and memory.actor.client == "claude-code"
    assert memory.links[0].type == LinkType.EXTENDS
    assert store.load_one(memory.id) == memory
    assert store.provenance_for([memory.id]) == {memory.id: LOCAL}
    assert store.log_rows()[-1].session == "sess_w"


def test_update_is_a_compare_and_swap_on_updated(store: Store) -> None:
    memory = store.write(content="port 5432", scopes=["tools"])
    edited = store.update(memory.model_copy(update={"body": "port 5433\n"}))
    assert edited.body == "port 5433\n"
    assert edited.updated > memory.updated
    with pytest.raises(ConcurrentUpdateError):
        store.update(memory.model_copy(update={"body": "port 5434\n"}))
    forced = store.update(memory.model_copy(update={"body": "port 5434\n"}), force=True)
    assert store.load_one(memory.id).body == forced.body


def test_update_carries_the_rollup_and_optionally_the_verification(
    store: Store,
) -> None:
    memory = store.write(content="port 5432", scopes=["tools"])
    store.record_corroboration(memory.id)
    verified = store.mark_verified(memory.id, verified_paths=["pyproject.toml"])
    snapshot = store.load_one(memory.id)
    # A content edit: the handler clears verification in the snapshot it
    # hands over, and the store writes it as given; the rollup is kept.
    content_edit = store.update(
        snapshot.model_copy(
            update={"body": "port 1\n", "last_verified_at": None, "verified_paths": []}
        )
    )
    assert content_edit.corroborations == 1
    assert content_edit.last_verified_at is None
    assert content_edit.verified_paths == []
    # A metadata edit built on a stale verification snapshot keeps the
    # stored one.
    stale = content_edit.model_copy(
        update={"scopes": ["infrastructure"], "last_verified_at": None}
    )
    store.mark_verified(memory.id, verified_paths=["pyproject.toml"])
    current = store.load_one(memory.id)
    kept = store.update(
        stale.model_copy(update={"updated": current.updated}),
        preserve_verification=True,
    )
    assert kept.scopes == ["infrastructure"]
    assert kept.last_verified_at == current.last_verified_at
    assert kept.verified_paths == ["pyproject.toml"]
    assert verified.verified_paths == ["pyproject.toml"]


def test_mark_verified_writes_the_record_and_this_hosts_stamp(
    store: Store,
) -> None:
    memory = store.write(content="port 5432", scopes=["tools"])
    verified = store.mark_verified(
        memory.id,
        verified_paths=["pyproject.toml"],
        verified_absent_paths=["/nonexistent"],
        verified_head=_HEAD,
        claims=["pyproject.toml"],
    )
    assert verified.last_verified_at is not None
    assert verified.verified_head == _HEAD
    assert verified.updated == memory.updated
    rows = store.verifications_for([memory.id])[memory.id]
    assert len(rows) == 1
    assert rows[0]["machine_id"] is None
    assert rows[0]["verified_paths"] == ["pyproject.toml"]
    assert rows[0]["verified_head"] == _HEAD
    trust = store.trust_rows([memory.id])
    assert trust is not None
    assert trust[memory.id].verified_locally_at == rows[0]["verified_at"]
    kinds = [row.kind for row in store.log_rows()[-2:]]
    assert kinds == ["memory_put", "verification_put"]
    # The lists the caller left None stay; the head is always written.
    again = store.mark_verified(memory.id)
    assert again.verified_paths == ["pyproject.toml"]
    assert again.verified_head is None
    with pytest.raises(ValueError, match="full commit hash"):
        store.mark_verified(memory.id, verified_head="abc")
    with pytest.raises(ConcurrentUpdateError):
        store.mark_verified(
            memory.id, check_expected=True, expected_last_verified_at=None
        )


def test_record_corroboration_bumps_without_touching_updated(
    store: Store,
) -> None:
    memory = store.write(content="port 5432", scopes=["tools"])
    bumped = store.record_corroboration(memory.id)
    assert bumped.corroborations == 1
    assert bumped.last_corroborated is not None
    assert bumped.updated == memory.updated


def test_load_one_raises_the_v8_errors(store: Store) -> None:
    memory = store.write(content="port 5432", scopes=["tools"])
    store.tombstone(memory.id, "wrong", session="sess_t")
    with pytest.raises(TombstonedError, match="wrong"):
        store.load_one(memory.id)
    with pytest.raises(MemoryNotFoundError):
        store.load_one(generate_ulid())
    with pytest.raises(MemoryNotFoundError, match="invalid id"):
        store.load_one("not-a-ulid")
    with pytest.raises(TombstonedError):
        store.record_corroboration(memory.id)


def test_summaries_and_tombstone_listings(store: Store) -> None:
    a = store.write(content="Alpha fact. More detail.", scopes=["tools"])
    b = store.write(content="Beta fact", scopes=["infrastructure"])
    summaries = store.list_summaries(scopes=["tools"])
    assert [(s.id, s.summary) for s in summaries] == [(a.id, "Alpha fact")]
    assert store.load_all() == [a, b]
    assert list(store.iter_active()) == [a, b]
    assert store.load_many([b.id, generate_ulid(), a.id]) == [b, a]
    store.tombstone(a.id, "r1", session="sess_1", now=_NOW)
    store.tombstone(b.id, "r2", session="sess_2", now=_NOW + timedelta(seconds=1))
    dead = store.load_tombstones()
    assert [d.id for d in dead] == [b.id, a.id]
    rows = store.list_tombstones(scopes=["tools"])
    assert [(r.id, r.removed_reason, r.removed_session) for r in rows] == [
        (a.id, "r1", "sess_1")
    ]
    assert store.load_tombstone(a.id).removed_reason == "r1"
    with pytest.raises(MemoryNotFoundError):
        store.load_tombstone(generate_ulid())


def test_restore_trimmed_drops_what_the_caller_names(store: Store) -> None:
    memory = store.write(content="port 5432", scopes=["tools"], claims=["a.py", "b.py"])
    store.mark_verified(memory.id, verified_paths=["a.py", "b.py"], verified_head=_HEAD)
    store.tombstone(memory.id, "r")
    with pytest.raises(MemoryNotFoundError):
        store.restore_trimmed(generate_ulid())
    back = store.restore_trimmed(
        memory.id,
        drop_claims=["a.py"],
        drop_verified_paths=["b.py"],
        clear_verification=True,
        drop_verified_head=True,
    )
    assert back.claims == ["b.py"]
    assert back.verified_paths == ["a.py"]
    assert back.last_verified_at is None
    assert back.verified_head is None
    with pytest.raises(NotTombstonedError):
        store.restore_trimmed(memory.id)


def test_rename_scope_touches_active_and_tombstoned_records(
    store: Store,
) -> None:
    a = store.write(content="one", scopes=["tols", "tools"])
    b = store.write(content="two", scopes=["tols"])
    c = store.write(content="three", scopes=["infrastructure"])
    store.tombstone(b.id, "r")
    result = store.rename_scope("tols", "tools")
    assert result == {"active": [a.id], "tombstoned": [b.id]}
    assert store.load_one(a.id).scopes == ["tools"]
    assert store.load_one(a.id).updated > a.updated
    assert store.load_tombstone(b.id).scopes == ["tools"]
    assert store.load_one(c.id) == c
    assert store.rename_scope("tools", "tools") == {"active": [], "tombstoned": []}
    assert store.rename_scope("tols", "x", include_tombstones=False) == {
        "active": [],
        "tombstoned": [],
    }
    assert store.log_verify()["status"] == "ok"


def test_prune_tombstones_by_age(store: Store) -> None:
    old = store.write(content="old", scopes=["tools"])
    young = store.write(content="young", scopes=["tools"])
    store.tombstone(old.id, "r", now=_NOW - timedelta(days=90))
    store.tombstone(young.id, "r", now=_NOW - timedelta(days=1))
    assert store.prune_tombstones(timedelta(days=30), now=_NOW) == [old.id]
    assert [d.id for d in store.load_tombstones()] == [young.id]
    assert store.prune_tombstones(timedelta(days=30), now=_NOW) == []


def test_document_frequencies_equal_a_python_count(store: Store) -> None:
    from bettermemory.search import candidate_admitted, tokenize

    docs = [
        ("the halden port is 5432", ["projects:halden"], "https://x/halden.git"),
        ("the halden port moved", ["projects:halden"], "https://x/halden.git"),
        ("postgres port for verendo", ["projects:verendo"], "https://x/verendo.git"),
        ("port notes", ["tools"], None),
    ]
    for body, scopes, repo in docs:
        store.write(
            content=body,
            scopes=scopes,
            origin=Origin(repo=repo) if repo else None,
        )
    terms = ["port", "halden", "postgres", "absent"]

    def admit(scopes: list[str], origin: Any, actor: Any) -> bool:
        return candidate_admitted(
            scopes,
            origin,
            actor,
            scope_filter=None,
            excluded=set(),
            repo_filter="https://x/halden.git",
            worktree_filter=None,
        )

    resolved = store.document_frequencies(terms, admit=admit)
    assert resolved is not None
    size, body_df, scope_df = resolved
    admitted = [m for m in store.load_all() if admit(m.scopes, m.origin, m.actor)]
    assert size == len(admitted) == 3
    for term in terms:
        expected_body = sum(1 for m in admitted if term in tokenize(m.body))
        expected_any = sum(
            1
            for m in admitted
            if term in tokenize(m.body) or term in tokenize(" ".join(m.scopes))
        )
        assert body_df.get(term, 0) == expected_body, term
        assert scope_df.get(term, 0) == expected_any, term
    assert store.document_frequencies([], admit=admit) is None
    assert store.document_frequencies(["x"], admit=lambda *_: False) is None


def test_scope_counts_and_category_rows(store: Store) -> None:
    a = store.write(
        content="one", scopes=["tools", "projects:x"], category=Category.AMBIENT
    )
    b = store.write(
        content="two",
        scopes=["tools"],
        origin=Origin(repo="https://x/other.git"),
        category=Category.AMBIENT,
    )
    store.write(content="three", scopes=["career"])
    total, counts = store.scope_counts(admit=lambda scopes, origin: origin is None)
    assert total == 2
    assert counts == {"tools": 1, "projects:x": 1, "career": 1}
    rows = store.category_rows(category="ambient", admit=lambda *_: True)
    assert rows == sorted([a.id, b.id])
    assert store.category_rows(
        category="ambient", admit=lambda scopes, origin: origin is None
    ) == [a.id]


def test_links_with_status(store: Store) -> None:
    target = store.write(content="target", scopes=["tools"])
    source = store.write(
        content="source",
        scopes=["tools"],
        links=[{"type": "supersedes", "target_id": target.id, "note": "n"}],
    )
    outbound, inbound, trust = store.links_with_status(source.id)
    assert outbound == [("supersedes", target.id, "n")]
    assert inbound == []
    assert trust == (LOCAL, None)
    _, inbound, _ = store.links_with_status(target.id)
    assert inbound == [("supersedes", source.id, "n")]
    assert store.links_with_status(generate_ulid()) == ([], [], None)


def test_events_since_and_backward(store: Store) -> None:
    store.record_event("search", session="s", query="one")
    store.record_event("show", session="s", id="x")
    rows = store.log_rows()
    kinds = [e["kind"] for e in store.iter_events()]
    assert kinds == ["search", "show"]
    assert [e["kind"] for e in store.iter_events_backward()] == ["show", "search"]
    stamp = datetime.fromisoformat(rows[-1].ts.replace("Z", "+00:00"))
    assert [e["kind"] for e in store.events_since(stamp)] == ["show"] or [
        e["kind"] for e in store.events_since(stamp)
    ] == ["search", "show"]
    assert list(store.events_since(stamp + timedelta(days=1))) == []
    assert [e["kind"] for e in store.events_since(stamp - timedelta(days=1))] == [
        "search",
        "show",
    ]


def test_the_journal_writes_lists_prunes_and_measures(store: Store) -> None:
    old = store.write_episode(
        session_id="sess_old",
        body="  tried x  ",
        takeaway=" it fell over ",
        scopes=["projects:x"],
        now=_NOW - timedelta(days=45),
    )
    assert old.body == "tried x\n"
    assert old.takeaway == "it fell over"
    floor = store.write_floor(session_id="sess_new", now=_NOW - timedelta(days=1))
    assert floor.is_floor is True
    fresh = store.write_episode(
        session_id="sess_new", body="did y", swarm_id="coord", now=_NOW
    )
    with pytest.raises(ValueError):
        store.write_episode(session_id="sess_new", body="   ")
    assert [e.id for e in store.episodes_by_session("sess_new")] == [floor.id, fresh.id]
    assert [e.id for e in store.episodes_by_swarm("coord")] == [fresh.id]
    assert store.episode_session_ids() == ["sess_old", "sess_new"]
    volume = store.episode_volume(now=_NOW)
    assert volume.sessions == 2 and volume.episodes == 3
    assert volume.bytes > 0 and volume.prunable_sessions == 1
    assert store.prunable_episode_sessions(now=_NOW) == ["sess_old"]
    assert store.prune_episode_sessions(now=_NOW, keep_session_id="sess_old") == []
    assert store.prune_episode_sessions(now=_NOW) == ["sess_old"]
    assert store.episode_session_ids() == ["sess_new"]
    assert store.log_verify()["status"] == "ok"


def test_episode_provenance_reads_the_pointer(store: Store) -> None:
    written = store.write_episode(session_id="s", body="journaled")
    planted = _episode(session_id="s")
    store.conn.execute(
        "INSERT INTO episodes(id, session_id, created, body, scopes_json, takeaway, "
        "origin_json, is_floor, swarm_id, log_mac) VALUES (?, ?, ?, ?, '[]', NULL, "
        "NULL, 0, NULL, NULL)",
        (planted.id, "s", planted.created.isoformat(), "planted\n"),
    )
    store.conn.commit()
    assert store.episode_provenance([written.id, planted.id, generate_ulid()]) == {
        written.id: LOCAL,
        planted.id: UNACCOUNTED,
    }
