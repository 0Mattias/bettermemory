"""The compatibility contract's versions in a store's meta (ENG-107).

Every open reads the store's minimum reader and writer revisions: below
the first the open is refused, below the second the store opens for
reading only, and a writing open records this build's revision, minimums
and version. The other build is simulated by patching the version
constants on `store_schema`, the module that reads them, and the package
version on `bettermemory`, which `store_versions.check_versions` and
`Store.create` read at call time.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

import bettermemory
from bettermemory import store_schema, store_steps
from bettermemory.config import Config, StorageConfig
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import STORE_FILENAME, Store

from ._mcp import call_tool, crash_text

# The meta keys are the contract's names (CONTRIBUTING.md), spelled here so
# a test reads what a build from another release would find in the file.
SCHEMA_REVISION_KEY = "schema_revision"
MIN_READER_KEY = "min_reader_revision"
MIN_WRITER_KEY = "min_writer_revision"
ENGINE_VERSION_KEY = "engine_version"

_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
_VERSION_KEYS = (SCHEMA_REVISION_KEY, MIN_READER_KEY, MIN_WRITER_KEY)

# This build's revision and the minimums a store it creates carries, and
# the revision of a later build past it.
OURS = store_schema.SCHEMA_REVISION
_READER = store_schema.MIN_READER_REVISION
_WRITER = store_schema.MIN_WRITER_REVISION
NEXT = OURS + 1


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


@contextlib.contextmanager
def build(*, revision: int, reader: int, writer: int, version: str) -> Iterator[None]:
    """Another build for the length of the block: its schema revision, the
    minimums a store it creates carries, and its package version. It runs
    the steps up to its revision, as that build's own `store_steps.STEPS`
    would."""
    steps = tuple(step for step in store_steps.STEPS if step.number <= revision)
    with pytest.MonkeyPatch.context() as patch:
        # raising=False: on a build without the contract the names are
        # bound and read by nothing, and the test fails on what it checks.
        patch.setattr(store_schema, "SCHEMA_REVISION", revision, raising=False)
        patch.setattr(store_schema, "MIN_READER_REVISION", reader, raising=False)
        patch.setattr(store_schema, "MIN_WRITER_REVISION", writer, raising=False)
        patch.setattr(store_steps, "STEPS", steps)
        patch.setattr(bettermemory, "__version__", version)
        yield


def meta_of(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(str(path))
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()


def set_meta(path: Path, **values: str | None) -> None:
    """Edit meta as an editor of the file (or a build from before the
    contract, for a key set to None) would: past the store."""
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            for key, value in values.items():
                if value is None:
                    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        (key, value),
                    )
    finally:
        conn.close()


def log_rows(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM log").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "store" / STORE_FILENAME


def test_the_contracts_names_are_the_modules() -> None:
    assert store_schema.SCHEMA_REVISION_KEY == SCHEMA_REVISION_KEY
    assert store_schema.MIN_READER_KEY == MIN_READER_KEY
    assert store_schema.MIN_WRITER_KEY == MIN_WRITER_KEY
    assert store_schema.ENGINE_VERSION_KEY == ENGINE_VERSION_KEY
    assert issubclass(store_schema.StoreVersionError, ValueError)


def test_a_new_store_carries_this_builds_revision_minimums_and_version(
    path: Path,
) -> None:
    Store.create(path).close()
    meta = meta_of(path)
    assert meta.get(SCHEMA_REVISION_KEY) == str(OURS)
    assert meta.get(MIN_READER_KEY) == str(_READER)
    assert meta.get(MIN_WRITER_KEY) == str(_WRITER)
    # Step 2 (ENG-200) is additive, and every write keeps its marks up.
    assert (OURS, _READER, _WRITER) == (2, 1, 2)
    assert meta[ENGINE_VERSION_KEY] == bettermemory.__version__


def test_a_store_from_before_the_contract_gets_its_keys_at_a_writing_open(
    path: Path,
) -> None:
    """A store an earlier candidate created carries no revision and no
    minimums, which read as 0: every build opens it. A reader's open
    writes nothing; the first writing open records the keys."""
    with build(revision=1, reader=1, writer=1, version="9.0.0rc12"):
        Store.create(path).close()
    set_meta(path, **dict.fromkeys(_VERSION_KEYS))
    with Store.open(path) as reader:
        assert reader.count_memories() == 0
    assert not set(_VERSION_KEYS) & set(meta_of(path))
    with build(revision=1, reader=1, writer=1, version="9.0.0rc14"):
        Store.open(path, allow_rekey=True).close()
    meta = meta_of(path)
    assert [meta.get(key) for key in _VERSION_KEYS] == ["1", "1", "1"]
    assert meta[ENGINE_VERSION_KEY] == "9.0.0rc14"


def test_engine_version_names_the_newest_build_that_opened_it_to_write(
    path: Path,
) -> None:
    with build(revision=1, reader=1, writer=1, version="9.0.0rc13"):
        Store.create(path).close()

    def opened(version: str, *, writing: bool = True) -> str:
        with build(revision=1, reader=1, writer=1, version=version):
            Store.open(path, allow_rekey=writing).close()
        return meta_of(path)[ENGINE_VERSION_KEY]

    assert opened("9.0.0rc14") == "9.0.0rc14"
    # An older writer leaves the newer one named.
    assert opened("9.0.0rc13") == "9.0.0rc14"
    # A reader's open records nothing, however new.
    assert opened("9.1.0", writing=False) == "9.0.0rc14"
    # A dev release sorts before its version's candidates.
    assert opened("9.0.0.dev1") == "9.0.0rc14"
    # A local part is left off for the order and kept in the record.
    assert opened("9.0.0+local") == "9.0.0+local"
    # A version with no place in the order changes nothing.
    assert opened("not-a-version") == "9.0.0+local"
    # A stored one with no place in the order gives way to one that has.
    set_meta(path, **{ENGINE_VERSION_KEY: "9\x1b[2J"})
    assert opened("9.0.0") == "9.0.0"


def test_a_writing_open_never_lowers_what_a_newer_build_recorded(
    path: Path,
) -> None:
    """A newer build at revision 3 that older writers may still write (its
    steps were additive): this build writes it, and leaves the revision,
    the minimums and the newer build's version as they were."""
    with build(revision=3, reader=1, writer=1, version="9.3.0"):
        Store.create(path).close()
    with Store.open(path, allow_rekey=True) as older:
        older.put_memory(_memory("an older build's write"))
    meta = meta_of(path)
    assert [meta.get(key) for key in _VERSION_KEYS] == ["3", "1", "1"]
    assert meta[ENGINE_VERSION_KEY] == "9.3.0"


def test_a_store_that_needs_a_newer_reader_is_refused(path: Path) -> None:
    with build(revision=3, reader=3, writer=3, version="9.4.0"):
        with Store.create(path) as newer:
            newer.put_memory(_memory("written by the newer build"))
    rows = log_rows(path)
    meta = meta_of(path)
    for opener in (
        lambda: Store.open(path),
        lambda: Store.open(path, allow_rekey=True),
        lambda: Store.open_or_create(path),
        lambda: Store(path.parent),
    ):
        with pytest.raises(ValueError) as caught:
            opener()
        assert type(caught.value).__name__ == "StoreVersionError"
        text = str(caught.value)
        assert "needs a reader at schema revision 3 or later" in text
        assert "its newest writer was bettermemory 9.4.0" in text
        assert f"this build, bettermemory {bettermemory.__version__}" in text
        assert f"reads revision {OURS}" in text
    assert log_rows(path) == rows
    assert meta_of(path) == meta


def test_a_store_that_needs_a_newer_writer_opens_for_reading_only(
    path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every write fails as on read-only media, through the opener's
    connection and through one `reopen` makes, and nothing is recorded:
    the log, meta and the newer build's version stay as they were."""
    with build(revision=NEXT, reader=_READER, writer=NEXT, version="9.2.0"):
        with Store.create(path) as newer:
            kept = newer.put_memory(_memory("written by the newer build"))
    rows = log_rows(path)
    meta = meta_of(path)
    caplog.set_level(logging.WARNING, logger="bettermemory.store")
    for allow_rekey in (True, False):
        with Store.open(path, allow_rekey=allow_rekey) as older:
            assert older.get_memory(kept.id).body == kept.body
            assert older.log_verify()["status"] == "ok"
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                older.put_memory(_memory("an older build's write"))
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                older.update(kept.model_copy(update={"body": "edited\n"}))
            worker = older.reopen()
            try:
                assert worker.count_memories() == 1
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    worker.tombstone(kept.id, reason="an older build's removal")
            finally:
                worker.close()
    assert log_rows(path) == rows
    assert meta_of(path) == meta
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    reading_only = [line for line in warned if "is open for reading only" in line]
    assert len(reading_only) == 1, warned
    assert f"needs a writer at schema revision {NEXT} or later" in reading_only[0]
    assert "its newest writer was bettermemory 9.2.0" in reading_only[0]


def test_an_owners_open_below_the_writer_minimum_writes_no_key(
    path: Path, keys_dir: Path
) -> None:
    """The owner's opener rekeys a store whose key is not on this machine.
    Below the writer minimum it opens as a reader instead: no key file, no
    `rekey` row, no binding or anchor is written."""
    with build(revision=NEXT, reader=_READER, writer=NEXT, version="9.2.0"):
        Store.create(path).close()
    rows = log_rows(path)
    for key in list(keys_dir.glob("*.key")):
        key.rename(key.with_suffix(".parked"))
    files = sorted(p.name for p in keys_dir.iterdir())
    with Store.open(path, allow_rekey=True) as older:
        assert not older.has_key
        assert older.count_memories() == 0
    assert sorted(p.name for p in keys_dir.iterdir()) == files
    assert log_rows(path) == rows


@pytest.mark.parametrize("value", ["two", "-1", "1.0", " 1", ""])
@pytest.mark.parametrize("key", [SCHEMA_REVISION_KEY, MIN_READER_KEY, MIN_WRITER_KEY])
def test_a_version_key_that_is_not_a_whole_number_is_refused(
    path: Path, key: str, value: str
) -> None:
    Store.create(path).close()
    set_meta(path, **{key: value})
    with pytest.raises(ValueError, match=f"carries a {key} that is not a whole"):
        Store.open(path, allow_rekey=True)


def _hits(found: object) -> list[dict[str, object]]:
    hits = found.get("result", found) if isinstance(found, dict) else found
    assert isinstance(hits, list)
    return hits


async def test_a_store_open_for_reading_only_answers_reads_through_the_server(
    memory_dir: Path,
) -> None:
    """A store whose writer minimum is above this build: through the MCP
    server `memory_search` and `memory_show` answer as on a store this
    build writes (the caller's resolution runs in a transaction that is
    rolled back), a write is refused in words, and the store gains no row."""
    config = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=config, store=store, state=SessionState())
    written = await call_tool(
        server,
        "memory_write",
        {
            "content": "The staging subnet moved before a later build raised the "
            "writer minimum.",
            "scopes": ["tools"],
        },
    )
    store.close()
    path = memory_dir / STORE_FILENAME
    set_meta(path, **{MIN_WRITER_KEY: str(NEXT)})
    rows = log_rows(path)

    store = Store(memory_dir)
    try:
        server = build_server(config=config, store=store, state=SessionState())
        found = await call_tool(server, "memory_search", {"query": "staging subnet"})
        assert written["id"] in [hit["id"] for hit in _hits(found)]
        shown = await call_tool(server, "memory_show", {"id": written["id"]})
        assert shown["id"] == written["id"]
        assert "staging subnet moved" in str(shown["body"])
        with pytest.raises(Exception) as refused:
            await call_tool(
                server,
                "memory_write",
                {"content": "A write this build may not make.", "scopes": ["tools"]},
            )
        assert "open for reading only, so this call wrote nothing" in crash_text(
            refused.value
        )
    finally:
        store.close()
    assert log_rows(path) == rows
    assert meta_of(path)[MIN_WRITER_KEY] == str(NEXT)
