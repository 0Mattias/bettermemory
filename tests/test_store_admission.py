"""A store file this build did not write is refused before it is read (ENG-172).

A repository can ship ``.claude-memory/memory.sqlite``, and ``[storage]
cwd_store`` (on by default) makes it the store every entry path opens in
that checkout. One whose ``meta`` is a view over an endless recursive CTE
hung `security audit`, `doctor`, the daemon's open and the hooks for good;
one with a trigger of its own on ``log`` hung the first write, which the
session-start hook makes. Every opener now admits the file before it reads
a value from it (`store_schema.admit_store`): ``trusted_schema`` off, the
schema checked against this build's own objects, and the reads made before
the file is admitted held to `store_schema.ADMISSION_DEADLINE_SECONDS`. A
file that fails is refused with one finding, and this build writes
nothing to it.

The cold review built three stores that passed that check and still hung
every surface: a ``meta`` whose ``value`` is a generated column over one
costly ``instr`` call (one virtual-machine step no progress handler can
interrupt), a ``sqlite_stat1`` rewritten the same way (SQLite reads it
while the schema loads, before any check looks), and a CHECK on ``log``
that every insert runs. Each is refused now (the length limit during
admission, SQLite's own text for its statistics tables, and the column
check of each table this build creates), and so is a schema SQLite
cannot parse, which ended ``up --foreground`` in a traceback.

A later build of the major may add to a store what an older build must
still read and write (ENG-107, CONTRIBUTING.md, "Store versions"): a plain
table, a nullable column with a literal default, an index on columns.
Each is admitted and read on the file as this build's own tables are, so
the review's three placed on a table this build never creates are still
refused in time. What a later build may not add without raising the
reader minimum (a view, a trigger, a virtual table, an index on an
expression or with a WHERE) is refused, and a refused store whose meta
says a later build wrote it is told to upgrade (`LaterStoreError`).

The four surfaces run in a child process each, from the repository's
directory with no ``BETTERMEMORY_DIR``, so the store is the one
``cwd_store`` picks, under a harness timeout (`_CHILD_TIMEOUT`): against a
build without the check the child never returns, and the timeout ends the
run as a failure instead of hanging the suite. Inside the child the call
that refuses is timed and printed, and must come back well under the
deadline (`_REFUSAL_BOUND`).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import pytest

from bettermemory import doctor, posture, store_schema
from bettermemory.cli import _common
from bettermemory.config import PROJECT_DIR_NAME
from bettermemory.log import KeyRing
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.store import STORE_FILENAME, Store
from bettermemory.store_keys import _store_id_at
from bettermemory.store_schema import MEMBERSHIP_TABLES

from .conftest import shielded_child_env

_T = TypeVar("_T")

#: What every refusal of a file's schema says. The tests match the refusal
#: as the ValueError it is and read `store_schema` names where they are
#: used, so this file collects on a build without the check, where each
#: test then fails on its own (`_in_time`, `_CHILD_TIMEOUT`).
_REFUSED = "not a store this build wrote"

#: How long a child may run before the harness ends it: far past the
#: admission deadline, so only a child that hangs reaches it.
_CHILD_TIMEOUT = 30.0
#: How long the refusing call may take inside the child: a quarter of the
#: 2 s admission deadline (a test below holds the two together), which a
#: refusal by the schema check never comes near (it reads one table of
#: about 70 rows). A literal, so this file collects on a build without
#: the deadline.
_REFUSAL_BOUND = 0.5

#: A query that never returns a row and never ends.
_ENDLESS = (
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
    "SELECT 'store_id' AS key, x AS value FROM n WHERE x < 0"
)

#: A ``meta`` whose ``value`` is a generated column over one costly
#: ``instr`` call (the cold review's first store, ENG-172 H1).
_SLOW_META = (
    "PRAGMA writable_schema = ON",
    "UPDATE sqlite_master SET sql = 'CREATE TABLE meta (key TEXT PRIMARY KEY, "
    "value TEXT GENERATED ALWAYS AS (v || substr(instr(printf(''%.*c'', "
    "10000000 + length(v) * 0, ''a''), printf(''%.*c'', 100000, ''a'') || ''b''), "
    "1, 0)) VIRTUAL, v TEXT)' WHERE type = 'table' AND name = 'meta'",
    "PRAGMA writable_schema = OFF",
)
#: The text the cold review gave ``sqlite_stat1``: a generated ``stat``.
_SLOW_STAT1 = (
    "PRAGMA writable_schema = ON",
    "UPDATE sqlite_master SET sql = 'CREATE TABLE sqlite_stat1(tbl, idx, stat "
    "GENERATED ALWAYS AS (s || substr(instr(printf(''%.*c'', 10000000 + "
    "length(s) * 0, ''a''), printf(''%.*c'', 100000, ''a'') || ''b''), 1, 0)) "
    "VIRTUAL, s)' WHERE name = 'sqlite_stat1'",
    "PRAGMA writable_schema = OFF",
)

#: The ways a repository's store is made to hang a reader: ``meta`` as an
#: endless view (every reader), and a trigger of its own on ``log`` whose
#: body never ends (every writer of a row, the session-start hook's brief
#: among them).
_POISONS: dict[str, tuple[str, ...]] = {
    "view": ("DROP TABLE meta", f"CREATE VIEW meta AS {_ENDLESS}"),
    "trigger": (
        "CREATE TRIGGER log_spin AFTER INSERT ON log BEGIN "
        f"SELECT count(*) FROM ({_ENDLESS}); END",
    ),
    # The cold review's three (ENG-172 H1): each passed the schema check by
    # its tables' names. One `instr` over ten million characters is one
    # step that takes seconds.
    "generated": _SLOW_META,
    "stat1": ("ANALYZE", *_SLOW_STAT1),
    "check": (
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = substr(sql, 1, length(sql) - 1) || "
        "', CHECK (instr(printf(''%.*c'', 10000000, ''a''), printf(''%.*c'', "
        "100000, ''a'') || ''b'') = 0))' WHERE type = 'table' AND name = 'log'",
        "PRAGMA writable_schema = OFF",
    ),
    # A schema text SQLite cannot parse (ENG-172 M2).
    "malformed": (
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = 'CREATE VIEW meta AS SELECT nonsense FROM ((' "
        "WHERE name = 'meta'",
        "PRAGMA writable_schema = OFF",
    ),
    # The review's three on a table this build never creates, which a
    # later build may add (ENG-107) and which is admitted as a plain table
    # only once its columns are read: its generated column, read unbounded,
    # takes about 20 s, and this build never evaluates it. Its row is
    # written before the column is, as the review's meta is built.
    "later_generated": (
        "CREATE TABLE later_slow (v TEXT)",
        "INSERT INTO later_slow (v) VALUES ('x')",
        "PRAGMA writable_schema = ON",
        "UPDATE sqlite_master SET sql = 'CREATE TABLE later_slow (w TEXT GENERATED "
        "ALWAYS AS (v || substr(instr(printf(''%.*c'', 10000000 + length(v) * 0, "
        "''a''), printf(''%.*c'', 100000, ''a'') || ''b''), 1, 0)) VIRTUAL, v TEXT)' "
        "WHERE name = 'later_slow'",
        "PRAGMA writable_schema = OFF",
    ),
    "later_stat1": (
        "CREATE TABLE later_ranks (id TEXT PRIMARY KEY, w REAL)",
        "CREATE INDEX later_by_w ON later_ranks(w)",
        "INSERT INTO later_ranks VALUES ('a', 0.5), ('b', 0.25)",
        "ANALYZE",
        *_SLOW_STAT1,
    ),
    "later_check": (
        "CREATE TABLE later_checked (x TEXT, CHECK (instr(printf('%.*c', "
        "10000000, 'a'), printf('%.*c', 100000, 'a') || 'b') = 0))",
    ),
    # The slow meta in a store whose meta says a later build wrote it: a
    # refused file's revision is read only from a meta the table check
    # admits, so this one is never read and the refusal stays the schema's.
    "later_revision_generated": (
        "UPDATE meta SET value = '2' WHERE key = 'schema_revision'",
        *_SLOW_META,
    ),
    # A unique index this build never creates on a table it writes (the
    # review of the ENG-107 reconciliation, H1): admitted, it turned this
    # build's INSERT OR REPLACE into quarantine, its INSERT OR IGNORE into
    # memory_links and FTS5's REPLACE into the docsize table into silent
    # deletes and drops, and the log then read tampered.
    "unique_quarantine": ("CREATE UNIQUE INDEX later_q ON quarantine(reason)",),
    "unique_links": ("CREATE UNIQUE INDEX later_l ON memory_links(source_id)",),
    "unique_fts_docsize": ("CREATE UNIQUE INDEX later_s ON memories_fts_docsize(sz)",),
    # One of this build's index names in capitals, on a table (H2): SQLite
    # compares names in any case, so the additive step's CREATE INDEX failed
    # on it and the owner's opener ended in a traceback.
    "upper_name": (
        "DROP INDEX projects_by_merged_into",
        "CREATE TABLE PROJECTS_BY_MERGED_INTO (x)",
    ),
}


def _sql(path: Path, *statements: str) -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()


#: How long an in-process call that could hang is waited for (`_in_time`).
_HANG_TIMEOUT = 10.0


def _in_time(call: Callable[[], _T]) -> _T:
    """`call()`'s value, or its exception, from a daemon thread waited on
    for `_HANG_TIMEOUT`: the harness's bound on an in-process call that a
    build without the check never returns from. SQLite gives the thread
    no way to be stopped, so on that build it spins on until the run
    ends; the test fails at once all the same."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_HANG_TIMEOUT)
    if thread.is_alive():
        pytest.fail(
            f"the call did not return in {_HANG_TIMEOUT:g} s: the store hung it"
        )
    if "error" in box:
        raise box["error"]
    value: _T = box["value"]
    return value


def _snapshot(path: Path) -> tuple[bytes, int, list[str]]:
    """The file's bytes, its mode and every name beside it."""
    return (
        path.read_bytes(),
        stat.S_IMODE(path.stat().st_mode),
        sorted(p.name for p in path.parent.iterdir()),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose ``.claude-memory`` holds a store this build wrote."""
    root = tmp_path / "repo"
    path = root / PROJECT_DIR_NAME / STORE_FILENAME
    Store.create(path).close()
    return root


def _poisoned(repo: Path, poison: str) -> Path:
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *_POISONS[poison])
    return path


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def _master(path: Path) -> list[tuple[str, str, str, str | None]]:
    conn = sqlite3.connect(str(path))
    try:
        return [
            (str(r[0]), str(r[1]), str(r[2]), r[3])
            for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
        ]
    finally:
        conn.close()


def test_every_store_this_build_writes_is_admitted(repo: Path) -> None:
    """A fresh store; one an rc3 build left without the membership tables
    and the additive index, which a user then ran ANALYZE on; one an
    earlier build left without an optional column, whose table text is
    not this build's; each opens."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    assert store_schema.schema_refusal(_master(path)) is None
    older = repo / "older" / STORE_FILENAME
    older.parent.mkdir()
    shutil.copy(path, older)
    _sql(
        older,
        *(f"DROP TABLE {table}" for table in MEMBERSHIP_TABLES),
        "DROP INDEX log_corroborations",
        "ANALYZE",
    )
    rows = _master(older)
    assert any(name == "sqlite_stat1" for _, name, _, _ in rows)
    assert store_schema.schema_refusal(rows) is None
    lacking = repo / "lacking" / STORE_FILENAME
    lacking.parent.mkdir()
    shutil.copy(path, lacking)
    _sql(lacking, "ALTER TABLE checkouts DROP COLUMN unresolved_json")
    assert store_schema.schema_refusal(_master(lacking)) is None
    for each in (older, lacking):
        Store.open(each).close()


@pytest.mark.parametrize(
    ("statements", "named"),
    [
        (_POISONS["view"], "view named 'meta'"),
        ((f"CREATE VIEW recent AS {_ENDLESS}",), "view named 'recent'"),
        (_POISONS["trigger"], "trigger named 'log_spin'"),
        (
            (
                "DROP TABLE memories_fts",
                "CREATE VIRTUAL TABLE memories_fts USING fts5(body_fts)",
            ),
            "table 'memories_fts' is not the one",
        ),
        (
            (
                "DROP TRIGGER memories_ai",
                "CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN "
                f"SELECT count(*) FROM ({_ENDLESS}); END",
            ),
            "trigger 'memories_ai' is not the one",
        ),
        (
            (
                "DROP INDEX log_by_kind",
                "CREATE INDEX log_by_kind ON log(kind, seq, ts)",
            ),
            "index 'log_by_kind' is not the one",
        ),
        (
            (
                "DROP TABLE quarantine",
                "CREATE VIRTUAL TABLE quarantine USING fts5(name)",
            ),
            "'quarantine' is a virtual table",
        ),
    ],
)
def test_an_object_this_build_does_not_create_is_refused(
    repo: Path, statements: tuple[str, ...], named: str
) -> None:
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *statements)
    why = store_schema.schema_refusal(_master(path))
    assert why is not None and named in why


@pytest.mark.parametrize(
    ("statements", "named"),
    [
        (
            (
                "DROP TABLE imports",
                "CREATE TABLE imports(id TEXT, at TEXT DEFAULT (datetime('now')))",
            ),
            "default that is not a literal",
        ),
        (
            (
                "DROP TABLE imports",
                "CREATE TABLE imports(id TEXT REFERENCES memories(id))",
            ),
            "foreign key",
        ),
        (
            ("DROP TABLE imports", "CREATE TABLE imports(id TEXT CHECK (id <> ''))"),
            "has a check",
        ),
        (
            (
                "DROP TABLE imports",
                "CREATE TABLE imports(id TEXT, n AS (length(id)))",
            ),
            "generated column",
        ),
        (
            (
                "ANALYZE",
                "PRAGMA writable_schema = ON",
                "UPDATE sqlite_master SET sql = 'CREATE TABLE sqlite_stat1(tbl, idx, "
                "stat)' WHERE name = 'sqlite_stat1'",
                "PRAGMA writable_schema = OFF",
            ),
            "not the one SQLite writes",
        ),
    ],
)
def test_a_table_of_this_builds_name_that_runs_an_expression_is_refused(
    repo: Path, statements: tuple[str, ...], named: str
) -> None:
    """A plain table is admitted by its name; what it could make SQLite run
    is read from the file (`store_schema.table_refusal`), and SQLite's own
    statistics tables pass only with the text SQLite gives them."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *statements)
    with pytest.raises(ValueError, match=_REFUSED) as refused:
        _in_time(lambda: Store.open(path))
    assert named in str(refused.value)


@pytest.mark.parametrize("poison", sorted(_POISONS))
@pytest.mark.parametrize("allow_rekey", [False, True])
def test_every_open_refuses_it_and_writes_nothing(
    repo: Path, poison: str, allow_rekey: bool
) -> None:
    """The reader's and the owner's open alike: refused in one error
    naming the object, quickly, with the file's bytes, mode and siblings as
    they were (no WAL switch, no chmod, no additive step)."""
    path = _poisoned(repo, poison)
    os.chmod(path, 0o644)
    before = _snapshot(path)
    started = time.perf_counter()
    with pytest.raises(ValueError, match=_REFUSED) as refused:
        _in_time(lambda: Store.open(path, allow_rekey=allow_rekey))
    assert time.perf_counter() - started < _REFUSAL_BOUND
    assert isinstance(refused.value, store_schema.StoreRefusedError)
    assert not refused.value.timed_out
    assert _snapshot(path) == before


def test_a_read_that_runs_past_the_deadline_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose schema is this build's can still be made slow to read:
    a ``meta`` of half a million rows. The deadline ends the read, and the
    refusal says it ran out of time (``timed_out``), not that the schema is
    foreign; with the deadline at its value the same file opens."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(
        path,
        "INSERT INTO meta(key, value) WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL "
        "SELECT x + 1 FROM n WHERE x < 500000) SELECT 'padding' || x, x FROM n",
    )
    with monkeypatch.context() as patched:
        patched.setattr(store_schema, "ADMISSION_DEADLINE_SECONDS", 0.01)
        with pytest.raises(ValueError, match="ran past") as refused:
            Store.open(path)
    assert isinstance(refused.value, store_schema.StoreRefusedError)
    assert refused.value.timed_out
    Store.open(path).close()


def test_the_refusal_bound_is_well_under_the_deadline() -> None:
    assert _REFUSAL_BOUND <= store_schema.ADMISSION_DEADLINE_SECONDS / 4


def test_the_deadline_ends_an_endless_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline itself, on a statement that never ends: interrupted
    once the deadline has passed, raised as a timed-out refusal, and the
    progress handler removed after."""
    monkeypatch.setattr(store_schema, "ADMISSION_DEADLINE_SECONDS", 0.2)
    path = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        started = time.perf_counter()
        with pytest.raises(ValueError, match="ran past") as refused:
            with store_schema._deadline(conn, path, "test"):
                conn.execute(_ENDLESS).fetchall()
        took = time.perf_counter() - started
        assert isinstance(refused.value, store_schema.StoreRefusedError)
        assert refused.value.timed_out
        assert 0.2 <= took < 1.2
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_a_bound_file_replaced_by_a_foreign_one_reads_as_no_store(
    repo: Path, tmp_path: Path
) -> None:
    """The key ring's binding names a path; a file put there since (a
    ``git pull`` over the bound store) is admitted before its id is read,
    and a foreign one holds no store, at once, where its read hung the
    open of every other file with the id."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    copy = tmp_path / "copy" / STORE_FILENAME
    copy.parent.mkdir()
    shutil.copy(path, copy)
    with Store.open(path) as bound:
        store_id = bound.store_id
    _poisoned(repo, "view")
    started = time.perf_counter()
    assert _in_time(lambda: _store_id_at(path)) is None
    with Store.open_or_create(copy) as opened:
        assert opened.has_key
        assert opened.store_id == store_id
    assert time.perf_counter() - started < _REFUSAL_BOUND * 4
    ring = KeyRing(Path(os.environ["BETTERMEMORY_KEYS_DIR"]), store_id)
    assert ring.read_binding() == copy.resolve()


def _memory(body: str) -> Memory:
    now = datetime.now(timezone.utc)
    return Memory(
        id=generate_ulid(),
        created=now,
        updated=now,
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        source=Source.EXPLICIT,
        body=body.strip() + "\n",
    )


def test_a_bound_store_with_one_more_index_keeps_the_ring(
    repo: Path, tmp_path: Path
) -> None:
    """The cold review's H2: the bound store gains one object this build
    refuses (another build's index on an expression, or one added by
    hand; an index on columns alone is admitted, ENG-107). It may still
    hold the store's id, so the key ring stays bound to it: a copy opens
    read-only and writes nothing, and the original, once the index is
    gone, verifies clean. Read as no store, the ring went to the copy,
    which signed with the key and moved the head, and the original read
    tampered (``tail_deleted``)."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    copy = tmp_path / "copy" / STORE_FILENAME
    copy.parent.mkdir()
    with Store.open_or_create(path) as bound:
        bound.put_memory(_memory("the staging database listens on 5432"))
        store_id = bound.store_id
    shutil.copy(path, copy)
    _sql(path, "CREATE INDEX extra ON memories(lower(created))")
    with pytest.raises(OSError):
        _in_time(lambda: _store_id_at(path))
    with Store.open_or_create(copy) as opened:
        assert opened.has_key is False
        rows = len(opened.log_rows())
        with pytest.raises(RuntimeError, match="open for reading only"):
            opened.put_memory(_memory("written into the copy"))
        assert len(opened.log_rows()) == rows
    ring = KeyRing(Path(os.environ["BETTERMEMORY_KEYS_DIR"]), store_id)
    assert ring.read_binding() == path.resolve()
    _sql(path, "DROP INDEX extra")
    with Store.open_or_create(path) as original:
        assert original.has_key
        assert original.log_verify()["status"] == "ok"


def test_the_owners_opener_ends_in_one_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`open_owner_store`, which `cli_context`, `serve` and the daemon's
    `up --foreground` open through: exit 1 and the refusal on one line."""
    path = _poisoned(repo, "view")
    with pytest.raises(SystemExit) as exited:
        _in_time(lambda: _common.open_owner_store(path))
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert _REFUSED in err


# ---------------------------------------------------------------------------
# What a later build of the major adds (ENG-107)
# ---------------------------------------------------------------------------

#: What a later build may add without raising ``min_reader_revision``
#: (CONTRIBUTING.md, "Store versions"), each admitted: a plain table (with
#: the index SQLite makes for its key), nullable columns with literal
#: defaults, and indexes on columns, on this build's table and on its own.
_LATER_ADDITIONS: dict[str, tuple[str, ...]] = {
    "table": (
        "CREATE TABLE later_ranks (id TEXT PRIMARY KEY, w REAL NOT NULL DEFAULT 0, "
        "note TEXT DEFAULT 'none', blob BLOB DEFAULT X'00')",
    ),
    "columns": (
        "ALTER TABLE memories ADD COLUMN later_rank REAL",
        "ALTER TABLE memories ADD COLUMN later_tag TEXT DEFAULT 'x'",
        "ALTER TABLE memories ADD COLUMN later_n TEXT NOT NULL DEFAULT ''",
    ),
    "literal defaults": (
        "ALTER TABLE memories ADD COLUMN later_flag INTEGER DEFAULT FALSE",
        "ALTER TABLE memories ADD COLUMN later_on INTEGER DEFAULT TRUE",
        "ALTER TABLE memories ADD COLUMN later_w REAL DEFAULT 1e3",
        "ALTER TABLE memories ADD COLUMN later_h INTEGER DEFAULT 0x10",
        "ALTER TABLE memories ADD COLUMN later_e REAL DEFAULT -1.5e-3",
    ),
    "NOT NULL on its table": ("CREATE TABLE later_n (x TEXT NOT NULL)",),
    "index": (
        'CREATE INDEX later_by_created ON memories(created DESC, "id" COLLATE NOCASE)',
    ),
    "index on its table": (
        'CREATE TABLE "later digest" (id TEXT, digest BLOB, UNIQUE (id))',
        'CREATE UNIQUE INDEX "later by digest" ON "later digest"(digest, id)',
    ),
}


@pytest.mark.parametrize("addition", sorted(_LATER_ADDITIONS))
def test_what_a_later_build_may_add_is_admitted(repo: Path, addition: str) -> None:
    """Admitted by every opener, and this build reads and writes the store
    beside it: the reader's open, the owner's, a write and a verify."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *_LATER_ADDITIONS[addition])
    assert store_schema.schema_refusal(_master(path)) is None
    with Store.open(path) as reader:
        assert reader.count_memories() == 0
    with Store.open(path, allow_rekey=True) as owner:
        owner.put_memory(_memory("the build server is named after a moon"))
        assert owner.log_verify()["status"] == "ok"
    with Store.open(path) as reader:
        assert reader.count_memories() == 1


#: What a later build may not add without raising ``min_reader_revision``,
#: each refused on a table, index, view or trigger this build never creates:
#: a table that computes a column, checks a row, refers to another table or
#: defaults to an expression, an index on an expression or with a WHERE, a
#: view, a trigger, a virtual table, and a name this build gives another
#: kind of object.
_LATER_REFUSED: dict[str, tuple[tuple[str, ...], str]] = {
    "generated column": (
        ("CREATE TABLE later_x (v TEXT, n AS (length(v)))",),
        "its table 'later_x' has a generated column 'n'",
    ),
    "check": (("CREATE TABLE later_x (v TEXT CHECK (v <> ''))",), "has a check"),
    "foreign key": (
        ("CREATE TABLE later_x (id TEXT REFERENCES memories(id))",),
        "its table 'later_x' has a foreign key",
    ),
    "expression default": (
        ("CREATE TABLE later_x (at TEXT DEFAULT (datetime('now')))",),
        "default that is not a literal",
    ),
    "expression index": (
        ("CREATE INDEX later_x ON memories(lower(body))",),
        "index named 'later_x'",
    ),
    "partial index": (
        ("CREATE INDEX later_x ON memories(created) WHERE created > ''",),
        "index named 'later_x'",
    ),
    "view": (
        ("CREATE VIEW later_x AS SELECT id FROM memories",),
        "view named 'later_x'",
    ),
    "trigger": (
        ("CREATE TRIGGER later_x AFTER INSERT ON memories BEGIN SELECT 1; END",),
        "trigger named 'later_x'",
    ),
    "virtual table": (
        ("CREATE VIRTUAL TABLE later_x USING fts5(v)",),
        "virtual table named 'later_x'",
    ),
    "another object's name": (
        ("DROP INDEX log_by_kind", "CREATE TABLE log_by_kind (x)"),
        "a name this build gives another object",
    ),
    "another object's name in capitals": (
        _POISONS["upper_name"],
        "its table 'PROJECTS_BY_MERGED_INTO' has a name this build gives another",
    ),
    "the full-text table's name in capitals": (
        (
            "DROP TABLE memories_fts",
            "CREATE TABLE MEMORIES_FTS (memories_fts, body_fts, scopes_fts)",
        ),
        "its table 'MEMORIES_FTS' is not the one this build creates",
    ),
    "unique index": (
        _POISONS["unique_quarantine"],
        "its unique index 'later_q' is on 'quarantine', which this build writes",
    ),
    "unique index on a full-text shadow table": (
        _POISONS["unique_fts_docsize"],
        "its unique index 'later_s' is on 'memories_fts_docsize'",
    ),
    "unique constraint": (
        (
            "CREATE TABLE quarantine_new (name TEXT PRIMARY KEY, reason TEXT NOT NULL "
            "UNIQUE, detail TEXT, remote TEXT, at TEXT NOT NULL, size INTEGER, "
            "sha256 TEXT)",
            "DROP TABLE quarantine",
            "ALTER TABLE quarantine_new RENAME TO quarantine",
        ),
        "its unique index 'sqlite_autoindex_quarantine_2' is on 'quarantine'",
    ),
    # The review's L1, on the base too: the key moved to another column
    # keeps its index's name, and this build's INSERT OR REPLACE then
    # deleted a quarantine row whose reason another row had.
    "moved primary key": (
        (
            "PRAGMA writable_schema = ON",
            "UPDATE sqlite_master SET sql = replace(replace(sql, 'name TEXT PRIMARY "
            "KEY', 'name TEXT'), 'reason TEXT NOT NULL', 'reason TEXT NOT NULL "
            "PRIMARY KEY') WHERE name = 'quarantine'",
            "PRAGMA writable_schema = OFF",
        ),
        "its index 'sqlite_autoindex_quarantine_1' is not on the columns this build",
    ),
    "NOT NULL with no default": (
        (
            "ALTER TABLE memories ADD COLUMN later_n TEXT",
            "PRAGMA writable_schema = ON",
            "UPDATE sqlite_master SET sql = replace(sql, 'later_n TEXT', "
            "'later_n TEXT NOT NULL') WHERE name = 'memories'",
            "PRAGMA writable_schema = OFF",
        ),
        "its table 'memories' needs 'later_n', which this build leaves out",
    ),
}


@pytest.mark.parametrize("refused", sorted(_LATER_REFUSED))
def test_what_a_later_build_may_not_add_is_refused(repo: Path, refused: str) -> None:
    """Refused by the reader's and the owner's open alike, at once, in the
    admission's words, the file left as it was."""
    statements, named = _LATER_REFUSED[refused]
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _sql(path, *statements)
    before = _snapshot(path)
    for allow_rekey in (False, True):
        started = time.perf_counter()
        with pytest.raises(ValueError, match=_REFUSED) as raised:
            _in_time(lambda: Store.open(path, allow_rekey=allow_rekey))
        assert time.perf_counter() - started < _REFUSAL_BOUND
        assert named in str(raised.value)
        assert isinstance(raised.value, store_schema.StoreRefusedError)
        assert not isinstance(raised.value, store_schema.StoreVersionError)
    assert _snapshot(path) == before


def _collated(path: Path) -> None:
    """An index on a collation its writer registered and this build does
    not: every write to the table fails in this build."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.create_collation("later", lambda a, b: (a > b) - (a < b))
        conn.execute("CREATE INDEX later_x ON memories(body COLLATE later)")
    finally:
        conn.close()


def _quoted_name(path: Path) -> None:
    """An index on a double-quoted name no column has, which SQLite takes
    for a string, and so for an expression."""
    try:
        _sql(path, 'CREATE INDEX later_x ON memories("no_such_column")')
    except sqlite3.OperationalError as exc:  # pragma: no cover - SQLITE_DQS off
        pytest.skip(f"this SQLite takes no double-quoted string ({exc})")


@pytest.mark.parametrize(
    ("make", "named"),
    [
        (_collated, "sorts by a collation SQLite does not build in"),
        (_quoted_name, "is on an expression"),
    ],
)
def test_an_index_that_reads_as_columns_is_held_to_what_sqlite_made(
    repo: Path, make: Callable[[Path], None], named: str
) -> None:
    """Each index's text is columns alone (`schema_refusal` passes it);
    what SQLite made of it is read on the file (`index_refusal`)."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    make(path)
    assert store_schema.schema_refusal(_master(path)) is None
    with pytest.raises(ValueError, match=_REFUSED) as raised:
        _in_time(lambda: Store.open(path, allow_rekey=True))
    assert f"its index 'later_x' {named}" in str(raised.value)


def _written_later(path: Path, *, reader: str, revision: str = "2") -> None:
    """Meta saying a later build (9.1.0) wrote the store at `revision`,
    with `reader` as its minimum reader, and a view this build refuses."""
    _sql(
        path,
        f"UPDATE meta SET value = '{revision}' WHERE key = 'schema_revision'",
        f"UPDATE meta SET value = '{reader}' WHERE key = 'min_reader_revision'",
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('engine_version', '9.1.0')",
        "CREATE VIEW later_view AS SELECT id FROM memories",
    )


@pytest.mark.parametrize(
    ("reader", "said", "key"),
    [
        (
            "1",
            "is at schema revision 2 (meta's schema_revision) (its newest writer "
            "was bettermemory 9.1.0), and this build, bettermemory",
            "schema_revision",
        ),
        (
            "2",
            "needs a reader at schema revision 2 or later (meta's "
            "min_reader_revision) (its newest writer was bettermemory 9.1.0); "
            "this build, bettermemory",
            "min_reader_revision",
        ),
    ],
)
def test_a_refused_store_a_later_build_wrote_says_to_upgrade(
    repo: Path, reader: str, said: str, key: str
) -> None:
    """A store whose ``schema_revision`` is above this build's holds what
    a later build may have added: the refusal is the version refusal, in
    `check_versions`' words when the reader minimum is above this build's
    too, not "not a store this build wrote". It is still a refusal of the
    file: the key ring's read of it cannot tell, and keeps the ring."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _written_later(path, reader=reader)
    before = _snapshot(path)
    for allow_rekey in (False, True):
        started = time.perf_counter()
        with pytest.raises(ValueError, match="upgrade bettermemory") as raised:
            _in_time(lambda: Store.open(path, allow_rekey=allow_rekey))
        assert time.perf_counter() - started < _REFUSAL_BOUND
        refused = raised.value
        message = str(refused)
        assert said in message
        assert "reads revision 1" in message
        assert "does not admit what it holds (it holds a view named 'later_view'" in (
            message
        )
        assert message.endswith(": upgrade bettermemory to open it")
        assert _REFUSED not in message
        assert isinstance(refused, store_schema.StoreVersionError)
        assert (refused.key, refused.needed) == (key, 2)
        assert isinstance(refused, store_schema.StoreRefusedError)
        assert refused.holds_meta is True and not refused.timed_out
    assert _snapshot(path) == before
    with pytest.raises(OSError):
        _in_time(lambda: _store_id_at(path))


@pytest.mark.parametrize("revision", ["1", "0", "two", ""])
def test_a_refused_store_at_this_builds_revision_keeps_the_schema_refusal(
    repo: Path, revision: str
) -> None:
    """A revision at or below this build's, or one that is not a whole
    number, says nothing of a later build: the admission's refusal."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _written_later(path, reader="2", revision=revision)
    with pytest.raises(ValueError, match=_REFUSED) as raised:
        _in_time(lambda: Store.open(path))
    assert "view named 'later_view'" in str(raised.value)
    assert not isinstance(raised.value, store_schema.StoreVersionError)
    assert isinstance(raised.value, store_schema.StoreRefusedError)
    assert raised.value.holds_meta is True


def _later_refusal(repo: Path) -> tuple[Path, ValueError]:
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _written_later(path, reader="1")
    with pytest.raises(ValueError) as raised:
        Store.open(path)
    return path, raised.value


def test_doctor_advises_the_upgrade_for_a_store_a_later_build_wrote(
    repo: Path,
) -> None:
    """doctor reads the version refusal before the refused file's: the
    remedy is the build to open it with, not "move it aside"."""
    path, refused = _later_refusal(repo)
    remedy = doctor._open_remedy(path, refused)
    assert "reads schema revision 2 or later (meta's schema_revision)" in remedy
    assert "move" not in remedy


def test_the_audit_reports_the_upgrade_for_a_store_a_later_build_wrote(
    repo: Path, tmp_path: Path
) -> None:
    """The audit's key finding for it is a warning that says to upgrade, not
    the high finding for a file a repository shipped."""
    path, _refused = _later_refusal(repo)
    _meta, error, refused = posture._meta_or_error(path)
    assert refused is not None
    finding = posture._key(path, tmp_path / "keys", None, error, None, refused=refused)
    assert (finding.id, finding.status) == ("key.match", "warn")
    assert finding.fix is not None and finding.fix.startswith("upgrade bettermemory")
    assert "upgrade bettermemory to open it" in finding.summary


def test_admission_reads_each_index_it_does_not_create_once(repo: Path) -> None:
    """One read of each index this build never creates and none of a whole
    table's index list per index, so admission grows with the indexes, not
    their square (6,000 plain indexes on one table were refused as a
    timeout when it read the list for each). Counted from the statements
    SQLite runs, not timed."""
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    count = 40
    _sql(
        path,
        "CREATE TABLE later_t (x, y)",
        *(f"CREATE INDEX later_{n} ON later_t(x, y)" for n in range(count)),
    )
    conn = sqlite3.connect(str(path), isolation_level=None)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        store_schema.admit_store(conn, path)
    finally:
        conn.close()
    reads = [s for s in statements if s.startswith("PRAGMA index")]
    assert sum("later_" in s for s in reads) == count
    assert not any("index_list" in s for s in statements)


def test_the_owners_opener_says_to_upgrade_in_one_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = repo / PROJECT_DIR_NAME / STORE_FILENAME
    _written_later(path, reader="1")
    with pytest.raises(SystemExit) as exited:
        _in_time(lambda: _common.open_owner_store(path))
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "upgrade bettermemory to open it" in err


# ---------------------------------------------------------------------------
# The four surfaces, each in a child under the harness timeout
# ---------------------------------------------------------------------------

#: Each child imports what the surface imports on first use, then times the
#: call that meets the store and prints one JSON line: ``elapsed`` and what
#: the surface reported.
_IMPORTS = """
import json, time
from pathlib import Path
import bettermemory.backup, bettermemory.config, bettermemory.recall
import bettermemory.store
"""
_AUDIT = (
    _IMPORTS
    + """
from bettermemory import posture
store = Path.cwd() / ".claude-memory" / "memory.sqlite"
started = time.perf_counter()
meta, error, refused = posture._meta_or_error(store)
elapsed = time.perf_counter() - started
report = posture.audit(cwd=Path.cwd())
keys = [f for f in report.findings if f.id == "key.match"]
print(json.dumps({"elapsed": elapsed, "refused": refused is not None,
                  "findings": [[f.status, f.summary] for f in keys]}))
"""
)
_DOCTOR = (
    _IMPORTS
    + """
from bettermemory import doctor
cwd = Path.cwd()
started = time.perf_counter()
store, key = doctor.check_store(
    doctor._Context(cwd=cwd, directory=cwd / ".claude-memory"))
elapsed = time.perf_counter() - started
checks = {c.id: c for c in doctor.run_checks(cwd=cwd)}
print(json.dumps({"elapsed": elapsed, "store": [store.status, store.summary],
                  "key": key.status, "run": checks["store"].status}))
"""
)
_DAEMON_OPEN = (
    _IMPORTS
    + """
from bettermemory.cli._common import cli_context
started = time.perf_counter()
try:
    cli_context(migrate=False)
    outcome = "opened"
except SystemExit as exc:
    outcome = f"exit {exc.code}"
print(json.dumps({"elapsed": time.perf_counter() - started, "outcome": outcome}))
"""
)
_HOOK = (
    _IMPORTS
    + """
from bettermemory.cli import session_start_cmd
started = time.perf_counter()
try:
    block = session_start_cmd._build_context_block(session_id="s1", source="startup")
    outcome = f"block {block!r}"
except Exception as exc:
    outcome = f"{type(exc).__name__}: {exc}"
print(json.dumps({"elapsed": time.perf_counter() - started, "outcome": outcome}))
"""
)


def _child_env(tmp_path: Path) -> dict[str, str]:
    """This test's directories, and no ``BETTERMEMORY_DIR``: the store is
    the one ``cwd_store`` picks in the child's working directory."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = shielded_child_env()
    env.pop("BETTERMEMORY_DIR", None)
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        APPDATA=str(home / "AppData" / "Roaming"),
        BETTERMEMORY_STATE_DIR=str(tmp_path / "state"),
        GIT_CEILING_DIRECTORIES=str(tmp_path),
    )
    return env


def _run(
    argv: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_CHILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{argv[1:3]} did not finish in {_CHILD_TIMEOUT:g} s "
            f"(ran {time.monotonic() - started:.1f} s): the store hung it"
        )


def _reported(code: str, repo: Path, tmp_path: Path) -> dict[str, Any]:
    done = _run([sys.executable, "-c", code], cwd=repo, env=_child_env(tmp_path))
    assert done.returncode == 0, done.stderr
    reported: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    elapsed = reported["elapsed"]
    assert isinstance(elapsed, float) and elapsed < _REFUSAL_BOUND, reported
    return reported


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_the_audit_refuses_it_in_one_finding(
    repo: Path, tmp_path: Path, poison: str
) -> None:
    """One ``key.match`` finding, high; a schema SQLite cannot read is the
    audit's warning for a meta it cannot read, as for a file that is not a
    database (`test_posture.test_unreadable_meta_is_a_warning`)."""
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    reported = _reported(_AUDIT, repo, tmp_path)
    findings: list[list[str]] = reported["findings"]
    assert len(findings) == 1
    status, summary = findings[0]
    if poison == "malformed":
        assert reported["refused"] is False
        assert status == "warn" and "meta cannot be read" in summary
    else:
        assert reported["refused"] is True
        assert status == "high" and _REFUSED in summary
    assert _snapshot(path) == before


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_doctor_refuses_it_in_one_finding(
    repo: Path, tmp_path: Path, poison: str
) -> None:
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    reported = _reported(_DOCTOR, repo, tmp_path)
    store: list[str] = reported["store"]
    status, summary = store
    assert status == "fail" and _REFUSED in summary
    assert reported["key"] == "warn"  # not checked: the store does not open
    assert reported["run"] == "fail"
    assert _snapshot(path) == before


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_the_daemons_open_refuses_it(repo: Path, tmp_path: Path, poison: str) -> None:
    """What `up --foreground` opens through (`cli_context(migrate=False)`),
    timed; then the daemon itself, which exits 1 with the line and no
    traceback instead of serving."""
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    assert _reported(_DAEMON_OPEN, repo, tmp_path)["outcome"] == "exit 1"
    env = _child_env(tmp_path)
    done = _run(
        [sys.executable, "-m", "bettermemory", "up", "--foreground", "--port", "0"],
        cwd=repo,
        env=env,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert _REFUSED in done.stderr
    assert "Traceback" not in done.stderr
    assert _snapshot(path) == before


@pytest.mark.parametrize("poison", sorted(_POISONS))
def test_the_session_start_hook_refuses_it(
    repo: Path, tmp_path: Path, poison: str
) -> None:
    """The in-process hook path (`session-start`, which records the brief
    it shows as a log row): refused, timed, and as a command it prints no
    block and exits 0 with one stderr note, as every hook failure does."""
    path = _poisoned(repo, poison)
    before = _snapshot(path)
    outcome = _reported(_HOOK, repo, tmp_path)["outcome"]
    assert isinstance(outcome, str)
    assert outcome.startswith("StoreRefusedError") and "not a store" in outcome
    done = _run(
        [sys.executable, "-m", "bettermemory", "session-start", "--session-id", "s1"],
        cwd=repo,
        env=_child_env(tmp_path),
    )
    assert done.returncode == 0
    assert done.stdout == ""
    assert _REFUSED in done.stderr
    assert _snapshot(path) == before
