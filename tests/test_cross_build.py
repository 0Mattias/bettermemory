"""A store written by one build and opened by another, both ways
(ENG-107).

The other build is simulated by patching the constants and the registry
the open reads at run time: `store_schema.SCHEMA_REVISION` and the minimums
a new store carries (`MIN_READER_REVISION`, `MIN_WRITER_REVISION`), the
schema steps `store_steps.run_steps` applies (`store_steps.STEPS`), and the
package version on `bettermemory`. Every patch names its module by its
dotted path, so a build without the contract fails each test here rather
than the module's collection.

A newer store meets an older build: one a later build changed additively
is read and written by it, one that needs a later writer is read only, and
one that needs a later reader is refused. An older store meets a newer
build: one this build wrote, and one from before the contract, is brought
up to the newer revision at the newer build's first writing open, which
backs it up first when a step changes what is there.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import bettermemory
from bettermemory import backup, store_schema
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store

# The contract's meta keys, spelled as a build from another release reads
# them in the file (CONTRIBUTING.md, "Store versions").
SCHEMA_REVISION_KEY = "schema_revision"
MIN_READER_KEY = "min_reader_revision"
MIN_WRITER_KEY = "min_writer_revision"
ENGINE_VERSION_KEY = "engine_version"
SCHEMA_STEPS_KEY = "schema_steps"
_VERSION_KEYS = (SCHEMA_REVISION_KEY, MIN_READER_KEY, MIN_WRITER_KEY)

_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
THIS = bettermemory.__version__
LATER = "9.1.0"


def _memory(body: str) -> Memory:
    return Memory(
        id=generate_ulid(),
        created=_NOW,
        updated=_NOW,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body + "\n",
    )


def _edited(memory: Memory, body: str) -> Memory:
    return memory.model_copy(
        update={"body": body + "\n", "updated": memory.updated + timedelta(minutes=1)}
    )


# --- the later build's steps ------------------------------------------------


def _adds_a_ranking(conn: sqlite3.Connection, path: Path, meta: dict[str, str]) -> None:
    """An additive step 2: a table and a nullable column this build never
    reads, added only where missing, in a transaction of its own."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)")}
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("CREATE TABLE IF NOT EXISTS later_ranks (id TEXT PRIMARY KEY, w REAL)")
    if "later_rank" not in columns:
        conn.execute("ALTER TABLE memories ADD COLUMN later_rank REAL")
    conn.execute("COMMIT")


def _rewrites_bodies(
    conn: sqlite3.Connection, path: Path, meta: dict[str, str]
) -> None:
    """A step that is not additive: it changes what the store holds, inside
    the runner's transaction (a later build keeping a value an older writer
    would leave stale)."""
    conn.execute("CREATE TABLE later_digest (id TEXT PRIMARY KEY, digest TEXT)")
    conn.execute("INSERT INTO later_digest SELECT id, substr(body, 1, 8) FROM memories")


@dataclass(frozen=True)
class Build:
    """A build as the open sees it: its revision, the minimums a store it
    creates carries, its package version, and the steps it knows beyond
    this build's."""

    revision: int
    reader: int
    writer: int
    version: str
    steps: Sequence[tuple[str, bool, int, int, Any]] = ()


def runner() -> Any:
    return importlib.import_module("bettermemory.store_steps")


@contextlib.contextmanager
def running(build: Build) -> Iterator[None]:
    steps = runner()
    later = tuple(
        steps.Step(
            number=len(steps.STEPS) + i + 1,
            name=name,
            additive=additive,
            reader=reader,
            writer=writer,
            apply=apply,
        )
        for i, (name, additive, reader, writer, apply) in enumerate(build.steps)
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("bettermemory.store_steps.STEPS", (*steps.STEPS, *later))
        patch.setattr("bettermemory.store_schema.SCHEMA_REVISION", build.revision)
        patch.setattr("bettermemory.store_schema.MIN_READER_REVISION", build.reader)
        patch.setattr("bettermemory.store_schema.MIN_WRITER_REVISION", build.writer)
        patch.setattr(bettermemory, "__version__", build.version)
        yield


#: This build's revision and the minimums a store it creates carries, as
#: meta holds them, and the revision a later build's one step takes a
#: store to.
OURS = store_schema.SCHEMA_REVISION
_READER = store_schema.MIN_READER_REVISION
_WRITER = store_schema.MIN_WRITER_REVISION
OURS_KEYS = [str(OURS), str(_READER), str(_WRITER)]
NEXT = OURS + 1

#: A later build whose step is additive: it raises the revision only.
ADDITIVE = Build(
    revision=NEXT,
    reader=_READER,
    writer=_WRITER,
    version=LATER,
    steps=(("a ranking older builds ignore", True, _READER, _WRITER, _adds_a_ranking),),
)
#: A later build whose step changes what is there and leaves older
#: writers stale: it raises the writer minimum.
NEEDS_WRITER = Build(
    revision=NEXT,
    reader=_READER,
    writer=NEXT,
    version=LATER,
    steps=(
        ("a digest older writers leave stale", False, _READER, NEXT, _rewrites_bodies),
    ),
)
#: A later build whose step older readers would misread.
NEEDS_READER = Build(
    revision=NEXT,
    reader=NEXT,
    writer=NEXT,
    version=LATER,
    steps=(("a digest older readers misread", False, NEXT, NEXT, _rewrites_bodies),),
)


# --- reading the file as another build would ---------------------------------


def meta_of(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()


def versions_of(path: Path) -> list[str | None]:
    meta = meta_of(path)
    return [meta.get(key) for key in _VERSION_KEYS]


def tables_of(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            str(name)
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()


def bundles(path: Path) -> list[Path]:
    root = backup.default_backups_dir(path)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / backup.MANIFEST_NAME).is_file())


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "store" / STORE_FILENAME


def _written_by_this_build(path: Path) -> Memory:
    with Store.create(path) as store:
        return store.put_memory(_memory("A record this build wrote."))


# --- a newer store, an older build -------------------------------------------


def test_an_older_build_reads_and_writes_a_store_a_later_build_changed_additively(
    path: Path,
) -> None:
    first = _written_by_this_build(path)
    with running(ADDITIVE), Store.open(path, allow_rekey=True) as later:
        later_write = later.put_memory(_memory("A record the later build wrote."))
        later._conn.execute(
            "UPDATE memories SET later_rank = 0.5 WHERE id = ?", (first.id,)
        )
        assert later.log_verify()["status"] == "ok"
    assert versions_of(path) == [str(NEXT), *OURS_KEYS[1:]]
    assert meta_of(path)[ENGINE_VERSION_KEY] == LATER

    with Store.open(path, allow_rekey=True) as older:
        assert older.count_memories() == 2
        assert older.get_memory(later_write.id).body == later_write.body
        older.put_memory(_edited(first, "The older build edited this record."))
        older.put_memory(_memory("A record the older build wrote."))
        assert older.log_verify()["status"] == "ok"
    # Nothing the older build wrote lowered the later build's record of
    # itself, and the column it does not know kept its value (a column
    # every write must keep up would raise the writer minimum instead).
    assert versions_of(path) == [str(NEXT), *OURS_KEYS[1:]]
    assert meta_of(path)[ENGINE_VERSION_KEY] == LATER
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        (rank,) = conn.execute(
            "SELECT later_rank FROM memories WHERE id = ?", (first.id,)
        ).fetchone()
    finally:
        conn.close()
    assert rank == 0.5

    with running(ADDITIVE), Store.open(path, allow_rekey=True) as later:
        assert later.count_memories() == 3
        assert (
            later.get_memory(first.id).body == "The older build edited this record.\n"
        )
        assert later.log_verify()["status"] == "ok"
    steps = json.loads(meta_of(path)[SCHEMA_STEPS_KEY])
    assert [(e["step"], e["backup"]) for e in steps] == [(NEXT, None)]


def test_an_older_build_only_reads_a_store_that_needs_a_later_writer(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first = _written_by_this_build(path)
    with running(NEEDS_WRITER), Store.open(path, allow_rekey=True) as later:
        later.put_memory(_memory("A record the later build wrote."))
    assert versions_of(path) == [str(NEXT), str(_READER), str(NEXT)]
    assert len(bundles(path)) == 1
    before = meta_of(path)

    with Store.open(path, allow_rekey=True) as older:
        assert older.count_memories() == 2
        assert older.get_memory(first.id).body == first.body
        assert older.log_verify()["status"] == "ok"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            older.put_memory(_edited(first, "An edit the older build may not make."))
    assert meta_of(path) == before
    assert any("open for reading only" in r.getMessage() for r in caplog.records)

    with running(NEEDS_WRITER), Store.open(path, allow_rekey=True) as later:
        later.put_memory(_edited(first, "The later build still writes it."))
        assert later.log_verify()["status"] == "ok"


def test_an_older_build_refuses_a_store_that_needs_a_later_reader(path: Path) -> None:
    _written_by_this_build(path)
    with running(NEEDS_READER), Store.open(path, allow_rekey=True):
        pass
    assert versions_of(path) == [str(NEXT)] * 3
    before = meta_of(path)
    for allow_rekey in (False, True):
        with pytest.raises(ValueError) as refused:
            Store.open(path, allow_rekey=allow_rekey)
        assert type(refused.value).__name__ == "StoreVersionError"
        message = str(refused.value)
        assert f"needs a reader at schema revision {NEXT}" in message
        assert f"its newest writer was bettermemory {LATER}" in message
        assert f"this build, bettermemory {THIS}, reads revision {OURS}" in message
    assert meta_of(path) == before


def test_a_store_a_later_build_created_carries_its_minimums_for_an_older_build(
    path: Path,
) -> None:
    """A store the later build created, rather than stepped, carries the
    later build's minimums from the start, and an older build applies the
    same rule to it."""
    with running(NEEDS_WRITER), Store.create(path) as later:
        later.put_memory(_memory("A record the later build wrote at creation."))
    assert versions_of(path) == [str(NEXT), str(_READER), str(NEXT)]
    assert bundles(path) == []
    with Store.open(path, allow_rekey=True) as older:
        assert older.count_memories() == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            older.put_memory(_memory("A write the older build may not make."))


def test_a_revision_1_build_only_reads_a_store_step_2_marked(path: Path) -> None:
    """Step 2 (ENG-200) adds the ``returned`` table, which every write that
    brings a record back into force keeps up: a build of revision 1 would
    bring one back unmarked, so it opens the store for reading only, and
    still reads it, its marks among the rest."""
    written = _written_by_this_build(path)
    with Store.open(path, allow_rekey=True) as store:
        replacer = store.write(
            content="The record that replaced this build's first one.",
            scopes=["tools"],
            links=[{"type": "supersedes", "target_id": written.id}],
        )
        store.tombstone(replacer.id, "it was wrong")
        assert written.id in store.returned_rows()
    assert versions_of(path) == OURS_KEYS
    assert "returned" in tables_of(path)
    earlier = runner().STEPS[:1]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("bettermemory.store_steps.STEPS", earlier)
        patch.setattr("bettermemory.store_schema.SCHEMA_REVISION", 1)
        patch.setattr("bettermemory.store_schema.MIN_READER_REVISION", 1)
        patch.setattr("bettermemory.store_schema.MIN_WRITER_REVISION", 1)
        patch.setattr(bettermemory, "__version__", "9.0.0rc14")
        with Store.open(path, allow_rekey=True) as older:
            assert older.get_memory(written.id).body == written.body
            assert older.log_verify()["status"] == "ok"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                older.tombstone(written.id, "a removal the older build may not make")
    assert versions_of(path) == OURS_KEYS


# --- an older store, a newer build -------------------------------------------


def test_a_later_build_steps_a_store_this_build_wrote(path: Path) -> None:
    first = _written_by_this_build(path)
    assert versions_of(path) == OURS_KEYS
    with running(NEEDS_WRITER):
        # A reader's open of the later build runs no step: it reads the
        # store as it stands, before the step, and writes nothing.
        with Store.open(path) as early:
            assert early.get_memory(first.id).body == first.body
            assert early.log_verify()["status"] == "ok"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                early.put_memory(_edited(first, "A reader's open writes nothing."))
        assert versions_of(path) == OURS_KEYS
        assert bundles(path) == []
        with Store.open(path, allow_rekey=True) as later:
            assert later.get_memory(first.id).body == first.body
            assert "later_digest" in tables_of(path)
        with Store.open(path) as reader:
            assert reader.count_memories() == 1
        # The later build verifies the bundle it took before its step.
        (bundle,) = bundles(path)
        assert backup.verify_bundle(bundle)["status"] == backup.OK
    assert versions_of(path) == [str(NEXT), str(_READER), str(NEXT)]
    assert versions_of(bundle / backup.SNAPSHOT_NAME) == OURS_KEYS
    steps = json.loads(meta_of(path)[SCHEMA_STEPS_KEY])
    assert [(e["step"], e["by"]) for e in steps] == [(NEXT, LATER)]


def test_a_later_build_steps_a_store_from_before_the_contract(path: Path) -> None:
    """A store 9.0.0rc13 or earlier wrote carries no revision: the later
    build's first writing open records this build's steps, then backs the
    store up and runs its own step, in one open."""
    first = _written_by_this_build(path)
    conn = sqlite3.connect(str(path))
    with conn:
        conn.executemany(
            "DELETE FROM meta WHERE key = ?",
            [(k,) for k in (*_VERSION_KEYS, SCHEMA_STEPS_KEY)],
        )
    conn.close()
    assert versions_of(path) == [None, None, None]
    with running(NEEDS_WRITER):
        with Store.open(path, allow_rekey=True) as later:
            assert later.get_memory(first.id).body == first.body
            assert later.log_verify()["status"] == "ok"
        (bundle,) = bundles(path)
        assert backup.verify_bundle(bundle)["status"] == backup.OK
    assert versions_of(path) == [str(NEXT), str(_READER), str(NEXT)]
    steps = json.loads(meta_of(path)[SCHEMA_STEPS_KEY])
    assert [(e["step"], e["backup"]) for e in steps] == [
        *((step.number, None) for step in runner().STEPS),
        (NEXT, str(bundle.relative_to(path.parent))),
    ]
    # The bundle is the store as the later step found it: this build's
    # steps recorded, not the later one.
    assert versions_of(bundle / backup.SNAPSHOT_NAME) == OURS_KEYS

    # `backup restore` under the later build puts that store back, and
    # this build, which the step left only reading, writes it again.
    with running(NEEDS_WRITER):
        report = backup.restore_bundle(bundle, path, apply=True)
        assert report["status"] == backup.RESTORED, report["detail"]
        assert versions_of(path) == OURS_KEYS
        with Store.open(path) as reader:
            assert reader.get_memory(first.id).body == first.body
    with Store.open(path, allow_rekey=True) as older:
        older.put_memory(_edited(first, "This build writes the restored store."))
        assert older.log_verify()["status"] == "ok"


def test_a_later_build_leaves_a_store_an_older_build_only_reads_as_it_is(
    path: Path,
) -> None:
    """An additive later step is recorded at the later build's writing
    open, and an older build's open afterwards neither runs nor undoes it:
    each build reads the store at its own revision."""
    _written_by_this_build(path)
    with running(ADDITIVE), Store.open(path, allow_rekey=True):
        pass
    with Store.open(path, allow_rekey=True) as older:
        assert older.log_verify()["status"] == "ok"
    with Store.open(path) as reader:
        assert reader.count_memories() == 1
    assert versions_of(path) == [str(NEXT), *OURS_KEYS[1:]]
    assert {"later_ranks", "memories"} <= tables_of(path)
