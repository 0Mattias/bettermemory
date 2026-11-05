"""Admitting a store file (ENG-172): the check every opener of an existing
store file runs before it reads a value from it (`admit_store`), the
deadline its reads run under (`_deadline`), an admitted file's meta read
(`read_store_meta`), and the refusal (`StoreRefusedError`, and
`LaterStoreError` for a file a later build wrote). What the check admits
is this build's own schema, read from `store_schema`'s texts, and what a
later build of the major may add (CONTRIBUTING.md, "Store versions").
`store` re-exports every name a caller reads. It lives apart from
`store_schema` to keep each module under the package's 2,000-line bound
(ENG-96); `store_schema.SCHEMA_REVISION` is read at call time, so a test
that patches it there stands another build in here too.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from . import store_schema as _schema
from .store_schema import (
    _OPTIONAL_COLUMNS,
    ADDITIVE_SCHEMA,
    MEMBERSHIP_SCHEMA,
    PROPOSALS_INDEX_SCHEMA,
    SCHEMA,
)
from .store_versions import (
    ENGINE_VERSION_KEY,
    MIN_READER_KEY,
    SCHEMA_REVISION_KEY,
    StoreVersionError,
    _shown,
    _WHOLE,
)


#: How long a store file's reads may run while it is admitted, in seconds,
#: counted from a block's first progress callback (`_deadline`): the schema
#: reads together (`admit_store`), then a refused file's version keys
#: (`_refuse_later`) or an admitted file's meta (`read_store_meta`) in a
#: window of their own; a wait for another connection's lock comes first and
#: is the busy timeout's. The schema reads take ``sqlite_master`` (about 70
#: rows in every store this build writes) and each plain table's and other
#: index's columns: measured on this tree (scratch run, Apple silicon, 200
#: opens) all of it, meta included, took 0.71 ms at the median and 1.0 ms
#: at most, on a fresh store and on one of 2,000 memories alike, so 2 s cuts
#: nothing this build wrote and stays well inside the 15 s a hook's client
#: waits (`_daemon_client.HOOK_TIMEOUT_SECONDS`). A meta of 8 million rows,
#: read whole, took 2.15 s; under this deadline its open was refused at 2.09 s.
#: The shape check of this build's tables (`shape_refusal`) added about
#: 0.6 ms at the median (1.58 against 1.00 ms, 300 admissions of a fresh
#: store, Apple silicon).
ADMISSION_DEADLINE_SECONDS = 2.0

#: SQLite virtual-machine steps between two looks at the clock while a
#: file is admitted (`sqlite3.Connection.set_progress_handler`). Small, so
#: a statement of few steps, each made costly, is still looked at.
_PROGRESS_STEPS = 100

#: The longest string or blob a statement may make while a file is
#: admitted (SQLITE_LIMIT_LENGTH, `admit_store`). One built-in call is one
#: virtual-machine step a progress handler cannot interrupt, and its work
#: can grow with its arguments' length (``instr`` is O(n*m)); SQLite runs a
#: schema's expressions (``sqlite_stat1`` is read while the schema loads)
#: before any check can look at them. Under 16 KiB one such call costs
#: about a millisecond; the longest text this build's schema holds is
#: 1,015 characters, and a statistics row is a few numbers.
_ADMISSION_MAX_LENGTH = 1 << 14

#: The tables SQLite itself writes into a database it is asked to analyse
#: (`ANALYZE`, ``PRAGMA optimize``), which a user or a tool may have run on
#: a store, with the text SQLite gives each: statistics the query planner
#: reads while the schema loads. Any other text (a generated column, say)
#: is refused.
_SQLITE_STATISTICS = {
    "sqlite_stat1": "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
    "sqlite_stat4": "CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)",
}

#: A column default that is a literal in SQLite's grammar, in parentheses
#: or not: NULL, TRUE, FALSE, a number (hex, or with an exponent), a string
#: or a blob. Every default this build's tables give is one.
_LITERAL = re.compile(
    r"(?i)\(*\s*(NULL|TRUE|FALSE|[-+]?(0X[0-9A-F]+|(\d+(\.\d*)?|\.\d+)(E[-+]?\d+)?)|"
    r"'(?:[^']|'')*'|X'[0-9A-F]*')\s*\)*"
)
#: What a table's text quotes or comments out, so a word in it is not read
#: as a keyword (`table_refusal`).
_QUOTED = re.compile(
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/",
    re.S,
)
#: A virtual table's text, a unique index's, and an index's on columns alone,
#: once `_blanked`: ``CREATE [UNIQUE] INDEX name ON table (column [COLLATE
#: name] [ASC|DESC], ...)``, no expression, no ``WHERE``; SQLite's collations.
_VIRTUAL = re.compile(r"\s*CREATE\s+VIRTUAL\b", re.I)
_UNIQUE = re.compile(r"\s*CREATE\s+UNIQUE\b", re.I)
_COLUMN = r"[\w$]+(?:\s+COLLATE\s+\w+)?(?:\s+(?:ASC|DESC))?"
_PLAIN_INDEX = re.compile(
    rf"\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+[\w$]+\s+ON\s+[\w$]+\s*"
    rf"\(\s*{_COLUMN}(?:\s*,\s*{_COLUMN})*\s*\)\s*;?\s*",
    re.I,
)
_BUILTIN_COLLATIONS = frozenset({"BINARY", "NOCASE", "RTRIM"})


def _blanked(sql: str | None) -> str:
    """`sql` with `_QUOTED`'s names as ``q``, comments as spaces, strings ``'``."""
    return _QUOTED.sub(
        lambda m: {"'": "'", "-": " ", "/": " "}.get(m.group(0)[0], " q "), sql or ""
    )


#: SQLite's primary result codes for a file whose schema cannot be read:
#: SQLITE_CORRUPT (a schema text SQLite cannot parse among them) and
#: SQLITE_NOTADB.
_UNREADABLE_SCHEMA_CODES = frozenset({11, 26})


class StoreRefusedError(ValueError):
    """A store file every opener refuses before it reads a value from it
    (`admit_store`): its schema holds an object this build does not admit
    (`schema_refusal`, `table_refusal`, `index_refusal`), or a read of its
    schema or its meta ran past `ADMISSION_DEADLINE_SECONDS`. A ValueError, as every other refusal of
    `Store.open` for a file this build does not read is, so each caller's
    handling of those reports this one too, in its own single line.
    ``timed_out`` tells the deadline from the schema: a file whose read
    ran long may be a store this build wrote, on a disk that stalled.
    ``holds_meta`` says whether the refused file has a ``meta`` table,
    None when its schema was not read to tell: one that has may still hold
    a store's id (a store with one more view or trigger), which the key
    ring's binding check reads as a file it cannot read to tell
    (`store_keys._store_id_at`), not as no store."""

    def __init__(
        self,
        path: Path,
        why: str,
        *,
        timed_out: bool = False,
        holds_meta: bool | None = None,
    ) -> None:
        self.path = path
        self.why = why
        self.timed_out = timed_out
        self.holds_meta = holds_meta
        super().__init__(f"{path} is not opened: {why}")


class LaterStoreError(StoreRefusedError, StoreVersionError):
    """A refused store file whose meta's ``schema_revision`` is above this
    build's (`_refuse_later`): it says to upgrade, not that the file is
    foreign. A `StoreRefusedError`, so every opener reports it, and a
    `StoreVersionError` whose `key` and `needed` name the revision needed."""

    def __init__(
        self, path: Path, why: str, message: str, *, key: str, needed: int
    ) -> None:
        StoreRefusedError.__init__(self, path, why, holds_meta=True)
        StoreVersionError.__init__(self, message, key=key, needed=needed)


_BUILT_OBJECTS: dict[tuple[str, str], tuple[str, str | None, bool]] = {}

#: The texts a store this build creates is built from (`Store.create`).
_SCHEMA_TEXTS = (SCHEMA, MEMBERSHIP_SCHEMA, ADDITIVE_SCHEMA, PROPOSALS_INDEX_SCHEMA)


def _built_objects() -> dict[tuple[str, str], tuple[str, str | None, bool]]:
    """Every object this build's schema creates, by (type, name), names in
    lower case as SQLite compares them: the table it is on, its text, and
    whether that text must match. Read once from an in-memory database built
    from the schema texts (`_SCHEMA_TEXTS`), so the list is the schema
    itself and cannot drift from it. Indexes, triggers and the full-text
    table must match their text: each is the same text in every build since
    the store was one file (checked over the history of `store.py` and this
    module), or since the step that added it. A plain table is matched by name, since `_add_membership_tables` adds columns to an
    older store's tables and SQLite rewrites their text when it does, and is
    read on the file (`table_refusal`); its text here is the columns this
    build declares NOT NULL with no default. An index SQLite made for a
    UNIQUE or PRIMARY KEY has no text: its key columns stand for it, read on
    the file too (`index_refusal`). No UNIQUE, PRIMARY KEY or NOT NULL of
    this build's tables has moved or gone in that history."""
    if not _BUILT_OBJECTS:
        conn = sqlite3.connect(":memory:")
        try:
            for script in _SCHEMA_TEXTS:
                conn.executescript(script)
            master = "SELECT type, name, tbl_name, sql FROM sqlite_master"
            built: dict[tuple[str, str], tuple[str, str | None, bool]] = {}
            for kind, name, table, sql in conn.execute(master).fetchall():
                exact = sql is not None and (
                    kind != "table" or bool(_VIRTUAL.match(sql))
                )
                if not exact:
                    pragma = "table_xinfo" if kind == "table" else "index_info"
                    columns = conn.execute(f'PRAGMA {pragma}("{name}")').fetchall()
                    sql = ",".join(
                        str(c[1] if kind == "table" else c[2]).lower()
                        for c in columns
                        if kind != "table" or (c[3] and c[4] is None)
                    )
                built[str(kind), str(name).lower()] = (str(table).lower(), sql, exact)
        finally:
            conn.close()
        _BUILT_OBJECTS.update(built)
    return _BUILT_OBJECTS


def schema_refusal(rows: Iterable[Sequence[Any]]) -> str | None:
    """Why a store file whose ``sqlite_master`` holds `rows` (type, name,
    table, text) is not one this build may read, or None. Names compare in
    any case, as SQLite's do. An object this build creates (`_built_objects`)
    must be on the same table, an index, a trigger or the full-text table
    with the same text, a plain table not virtual; SQLite's statistics
    tables pass with SQLite's own text. One it never creates, as a later
    build of the major may add (CONTRIBUTING.md, "Store versions"), passes
    as a plain table or an index on columns alone (`_PLAIN_INDEX`, or
    SQLite's own for a UNIQUE or PRIMARY KEY) under a name none of this
    build's has, and `admit_store` reads it on the file. A view, a trigger
    this build did not write, a virtual table, an index on an expression or
    with a WHERE, or one of this build's with another text runs SQL of the
    file's choosing when this build reads or writes it; a unique index on a
    table this build writes turns its INSERT OR REPLACE, its INSERT OR
    IGNORE and FTS5's REPLACE into a silent delete or drop of rows."""
    built = _built_objects()
    names = {name for _kind, name in built}
    tables = {name for kind, name in built if kind == "table"}
    for kind, name, table, sql in rows:
        kind, name, table, low = str(kind), str(name), str(table), str(name).lower()
        if kind == "table" and low in _SQLITE_STATISTICS:
            if sql != _SQLITE_STATISTICS[low]:
                return f"its table {name!r} is not the one SQLite writes"
            continue
        blanked = _blanked(sql)
        virtual = _VIRTUAL.match(blanked) is not None
        expected = built.get((kind, low))
        if expected is None:
            never = f"named {name!r}, which this build never creates"
            if kind == "table" and virtual:
                return f"it holds a virtual table {never}"
            if kind == "index" and not (
                _PLAIN_INDEX.fullmatch(blanked)
                or (sql is None and low.startswith("sqlite_autoindex_"))
            ):
                return f"it holds an index {never}, on more than columns"
            if kind not in ("table", "index"):
                return f"it holds a {kind} {never}"
            if low in names:
                return f"its {kind} {name!r} has a name this build gives another object"
            unique = sql is None or _UNIQUE.match(blanked) is not None
            if kind == "index" and unique and table.lower() in tables:
                return f"its unique index {name!r} is on {table!r}, which this build writes"
            continue
        on, text, exact = expected
        if table.lower() != on:
            return f"its {kind} {name!r} is on {table!r}, not on {on!r}"
        if (exact and sql != text) or (kind == "index" and not exact and sql):
            return f"its {kind} {name!r} is not the one this build creates"
        if not exact and virtual:
            return f"its table {name!r} is a virtual table, which this build never makes it"
    return None


def table_refusal(
    conn: sqlite3.Connection, name: str, sql: str | None, filled: str | None = None
) -> str | None:
    """Why the plain table ``name``, as the file on `conn` defines it
    (``sql``), could make SQLite run an expression of the file's choosing
    when this build reads or writes it, or None: a generated column, a
    default that is not a literal, a CHECK, or a foreign key; and, on a
    table this build writes (``filled``, the columns it declares NOT NULL
    with no default, `_built_objects`), a NOT NULL column with no default
    its inserts leave out, on which every one would fail. A table is
    admitted by name, or by kind (`schema_refusal`), so a computed ``meta``
    ``value``, a check on every ``log`` insert, or a later build's table
    that computes a column is refused here. None of this build's tables has
    any: their defaults are ``''``, ``'[]'``, ``'{}'`` or ``0``."""
    quoted = '"' + name.replace('"', '""') + '"'
    for row in conn.execute(f"PRAGMA table_xinfo({quoted})"):
        column, notnull, default, hidden = row[1], row[3], row[4], row[6]
        if notnull and default is None and filled is not None:
            if str(column).lower() not in filled.split(","):
                return (
                    f"its table {name!r} needs {column!r}, which this build leaves out"
                )
        if hidden:
            return f"its table {name!r} has a generated column {column!r}"
        if default is not None and not _LITERAL.fullmatch(str(default)):
            return (
                f"its table {name!r} gives {column!r} a default that is not a literal"
            )
    if conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchone() is not None:
        return f"its table {name!r} has a foreign key"
    if re.search(r"\bCHECK\b", _QUOTED.sub(" ", sql or ""), re.I):
        return f"its table {name!r} has a check"
    return None


def index_refusal(
    conn: sqlite3.Connection, name: str, columns: str | None = None
) -> str | None:
    """Why ``name``, an index this build never creates, could make SQLite run
    an expression of the file's choosing when this build writes its table,
    or None: an expression among its columns (a quoted name no column has is
    a string, so one) or a collation SQLite does not build in; a WHERE is
    the text's to refuse (`_PLAIN_INDEX`). With ``columns``, it is SQLite's
    index for a UNIQUE or PRIMARY KEY of this build's table, which must be on
    those key columns: moved, this build's INSERT OR REPLACE deletes rows.
    One read of the index on the file, so admission is linear in indexes."""
    quoted = '"' + name.replace('"', '""') + '"'
    rows = conn.execute(f"PRAGMA index_xinfo({quoted})").fetchall()
    for row in rows:
        if row[1] == -2:
            return f"its index {name!r} is on an expression"
        if str(row[4]).upper() not in _BUILTIN_COLLATIONS:
            return f"its index {name!r} sorts by a collation SQLite does not build in"
    keys = ",".join(str(row[2]).lower() for row in rows if row[5])
    if columns is not None and keys != columns:
        return f"its index {name!r} is not on the columns this build gives it"
    return None


class _TableShape(NamedTuple):
    """What of a plain table this build's statements rely on, read with
    ``PRAGMA table_xinfo`` and ``index_list`` rather than from its text,
    which SQLite rewrites on ``ALTER TABLE``: each column's type affinity,
    NOT NULL, default and place in the primary key, by name in lower case;
    the key of each UNIQUE and PRIMARY KEY constraint, as its columns with
    their collations; and whether it is WITHOUT ROWID."""

    columns: dict[str, tuple[str, bool, str | None, int]]
    keys: frozenset[tuple[tuple[str, str], ...]]
    without_rowid: bool


def _affinity(declared: str) -> str:
    """The type affinity SQLite gives a column declared `declared`, by the
    rules of section 3.1 of its datatype page, in their order."""
    upper = declared.upper()
    if "INT" in upper:
        return "INTEGER"
    if any(word in upper for word in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in upper or not upper.strip():
        return "BLOB"
    if any(word in upper for word in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _default(raw: object) -> str | None:
    """A column's default as ``table_xinfo`` gives it, without the
    parentheses around it and, unless it is a string, in capitals;
    `table_refusal` has refused one that is not a literal."""
    if raw is None:
        return None
    text = str(raw).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text if text.startswith("'") else text.upper()


def _constraint_indexes(rows: Iterable[Sequence[Any]]) -> dict[str, list[str]]:
    """The indexes SQLite made for each table's UNIQUE and PRIMARY KEY
    constraints, by table in lower case, from ``sqlite_master``'s rows
    (type, name, table, text): the ``sqlite_autoindex_`` ones, which have no
    text. A WITHOUT ROWID table's primary key is the table and has no row."""
    found: dict[str, list[str]] = {}
    for kind, name, table, sql in rows:
        if kind == "index" and sql is None:
            if str(name).lower().startswith("sqlite_autoindex_"):
                found.setdefault(str(table).lower(), []).append(str(name))
    return found


def _table_shape(
    conn: sqlite3.Connection, name: str, sql: str | None, constraints: Sequence[str]
) -> _TableShape:
    """The shape (`_TableShape`) of the plain table ``name`` on `conn`,
    whose text is ``sql`` and whose constraints' indexes are `constraints`
    (`_constraint_indexes`); a WITHOUT ROWID table's primary key is read by
    the table's name. An index a CREATE INDEX made is not part of it: the
    admission holds each of those to its text (`schema_refusal`). One read
    of the table's columns and one of each constraint's index, so the
    admission stays linear in the indexes."""
    quoted = '"' + name.replace('"', '""') + '"'
    columns = {
        str(row[1]).lower(): (
            _affinity(str(row[2] or "")),
            bool(row[3]),
            _default(row[4]),
            int(row[5]),
        )
        for row in conn.execute(f"PRAGMA table_xinfo({quoted})")
    }
    without_rowid = re.search(r"\bWITHOUT\s+ROWID\b", _blanked(sql), re.I) is not None
    keys = frozenset(
        tuple(
            (str(column[2]).lower(), str(column[4]).upper())
            for column in conn.execute(
                "PRAGMA index_xinfo(" + '"' + index.replace('"', '""') + '")'
            )
            if column[5]
        )
        for index in (*constraints, *((name,) if without_rowid else ()))
    )
    return _TableShape(columns, keys, without_rowid)


_BUILT_SHAPES: dict[str, _TableShape] = {}


def _built_shapes() -> dict[str, _TableShape]:
    """The shape (`_TableShape`) of each plain table this build creates, by
    name in lower case, read once from an in-memory database built from the
    schema texts, as `_built_objects` is. Left out are SQLite's own
    tables and the full-text table's shadow tables, whose shape is FTS5's
    and may differ between SQLite's versions; the admission holds those to
    their kind and their indexes."""
    if not _BUILT_SHAPES:
        conn = sqlite3.connect(":memory:")
        try:
            for script in _SCHEMA_TEXTS:
                conn.executescript(script)
            master = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
            ).fetchall()
            constraints = _constraint_indexes(master)
            tables = [(str(n), sql) for kind, n, _t, sql in master if kind == "table"]
            virtual = tuple(
                f"{name.lower()}_" for name, sql in tables if _VIRTUAL.match(sql or "")
            )
            shapes = {
                name.lower(): _table_shape(
                    conn, name, sql, constraints.get(name.lower(), [])
                )
                for name, sql in tables
                if not _VIRTUAL.match(sql or "")
                and not name.lower().startswith(("sqlite_", *virtual))
            }
        finally:
            conn.close()
        _BUILT_SHAPES.update(shapes)
    return _BUILT_SHAPES


def shape_refusal(
    conn: sqlite3.Connection, name: str, sql: str | None, constraints: Sequence[str]
) -> str | None:
    """Why ``name``, one of this build's plain tables as the file on `conn`
    defines it (``sql``, and `constraints`, its constraints' indexes:
    `_constraint_indexes`), is not the table this build's statements read
    and write, or None (ENG-107, review L4). The admission takes a plain table
    of one of its names as its own (`schema_refusal`), and an earlier build
    admits a plain table under any name it does not use, so a store can
    hold a table under the name a later step creates with ``CREATE TABLE IF
    NOT EXISTS``, which would take it as made: with no key, a rowid alias
    or another collation, this build's INSERT OR REPLACE adds a second row
    or fails on a datatype mismatch, and a missing column fails every
    statement that names it. So each column this build declares must be
    there (one of `_OPTIONAL_COLUMNS` may be missing) with its affinity, NOT
    NULL, default and place in the primary key, and the table must carry
    this build's UNIQUE and PRIMARY KEY keys and no other, WITHOUT ROWID
    when this build's is. A column this build does not declare passes, as
    a later build's does, and the text is not compared, since SQLite
    rewrites it on ALTER TABLE. Every table each release from 9.0.0rc1
    made has its shape here (`tests/test_store_admission.py`)."""
    want = _built_shapes().get(name.lower())
    if want is None:
        return None
    have = _table_shape(conn, name, sql, constraints)
    optional = _OPTIONAL_COLUMNS.get(name.lower(), frozenset())
    for column, shape in want.columns.items():
        found = have.columns.get(column)
        if found is None and column not in optional:
            return f"its table {name!r} lacks {column!r}, which this build writes"
        if found is not None and found != shape:
            return f"its table {name!r} declares {column!r} otherwise than this build"
    if have.keys != want.keys:
        return f"its table {name!r} is not keyed as this build keys it"
    if have.without_rowid != want.without_rowid:
        layout = "WITHOUT ROWID" if have.without_rowid else "a rowid table"
        return f"its table {name!r} is {layout}, unlike this build's"
    return None


@contextlib.contextmanager
def _deadline(
    conn: sqlite3.Connection, path: Path, what: str, *, length: int | None = None
) -> Iterator[None]:
    """Run the statements inside the block within `ADMISSION_DEADLINE_SECONDS`
    of virtual-machine time together: a progress handler that starts its
    clock at its first call and interrupts the running one once the deadline
    has passed, which SQLite reports as SQLITE_INTERRUPT and this raises as
    `StoreRefusedError` (``timed_out``). With ``length``, no string or blob
    longer than that is made inside the block (SQLITE_LIMIT_LENGTH), and
    one that would be is a refusal too. The handler is removed, and the
    limit put back, on the way out. Read at call time, so a test can
    shorten it."""
    seconds = ADMISSION_DEADLINE_SECONDS
    started: list[float] = []
    fired: list[bool] = []

    def over() -> int:
        now = time.monotonic()
        if not started:
            started.append(now)
            return 0
        if now - started[0] > seconds:
            fired.append(True)
            return 1
        return 0

    conn.set_progress_handler(over, _PROGRESS_STEPS)
    longest = None
    if length is not None:
        longest = conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, length)
    try:
        yield
    except sqlite3.DataError as exc:
        raise StoreRefusedError(
            path, f"it is not a store this build wrote ({exc} in its {what})"
        ) from exc
    except sqlite3.OperationalError as exc:
        if not fired:
            raise
        raise StoreRefusedError(
            path,
            f"reading its {what} ran past the {seconds:g} s a store file is given "
            "before it is admitted",
            timed_out=True,
        ) from exc
    finally:
        conn.set_progress_handler(None, 0)
        if longest is not None:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, longest)


def admit_store(conn: sqlite3.Connection, path: Path) -> None:
    """Admit the store file `conn` was just opened on, before any other
    statement this build makes reads it or writes to it: ``trusted_schema``
    off for the connection's life, then, under the deadline and with no
    string longer than `_ADMISSION_MAX_LENGTH` (`_deadline`), its
    ``sqlite_master`` checked (`schema_refusal`), and each plain table
    (`table_refusal`) and each index this build never creates
    (`index_refusal`) read on the file, each of this build's own tables
    held to the shape it gives it (`shape_refusal`). A file that fails, or whose schema
    SQLite cannot read (SQLITE_CORRUPT, SQLITE_NOTADB), is refused with one
    `StoreRefusedError` (`LaterStoreError` when its meta says a later build
    wrote it: `_refuse_later`), and the caller closes the connection.
    `read_store_meta` then reads an admitted file's meta under the deadline.

    What a refusal leaves: this build writes nothing to the file
    (`store._connect` switches it to WAL, sets the file modes and runs the
    additive step only after this). SQLite does what it does on opening
    any database: on a WAL-mode file it makes the ``-wal`` and ``-shm``
    while it reads the schema and removes them at the close, and a ``-wal``
    or a hot ``-journal`` that came with the file is replayed into it or
    rolled back before the first statement's first step, so outside the
    deadline.

    Which files are checked: every existing file any opener reads, the
    state directory's own store included; only a file `Store.create` has
    just written on the same connection goes unchecked. A path says nothing
    of the bytes now at it (a repository's ``.claude-memory/memory.sqlite``,
    reached through ``[storage] cwd_store``, is whatever the clone holds,
    and a ``git pull``, or a restored or synced copy, can replace a store
    this build wrote), the key ring's binding (`log.KeyRing.bind_store`)
    names a path, not bytes, and the store id that finds the ring is read
    from the file. So the boundary is the connection: what this build wrote
    on it, and nothing else. The check costs one read of about 70 rows.

    Why these: a view, or a trigger of the file's choosing, runs SQL the
    file chose whenever this build reads the table it stands for or writes
    beside it, and a recursive one never ends (a ``meta`` that is a view
    over an endless recursive CTE hung `security audit`, `doctor`, the
    daemon and every hook); ``trusted_schema`` off keeps a table's
    defaults, checks and generated columns to functions SQLite marks
    harmless; the length limit bounds what one of those can cost while the
    schema loads (``sqlite_stat1`` is read then); the table and index
    checks refuse a table that computes a column, checks a row, defaults to
    an expression or refers to another table, and an index that computes
    its key, holds a WHERE, or is unique on a table this build writes (its
    INSERT OR REPLACE would delete rows); and the deadline bounds the reads
    made before the file is known, a schema too large to parse among them. Once a file
    is admitted its objects are this build's own or a later build's plain
    tables and indexes on columns (what CONTRIBUTING.md's "Store versions"
    lets a later build add without raising ``min_reader_revision``), which
    run nothing of the file's choosing, so each later statement's work is
    bounded by the rows it holds and the deadline is lifted."""
    with _deadline(conn, path, "schema", length=_ADMISSION_MAX_LENGTH):
        try:
            conn.execute("PRAGMA trusted_schema = OFF")
            rows = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code not in _UNREADABLE_SCHEMA_CODES:
                raise
            raise StoreRefusedError(
                path,
                "it is not a store this build wrote, or it is damaged: its "
                f"schema cannot be read ({exc})",
            ) from exc
        why = schema_refusal(rows)
        constraints = _constraint_indexes(rows)
        for kind, name, _table, sql in rows if why is None else ():
            kind, name = str(kind), str(name)
            expected = _built_objects().get((kind, name.lower()))
            known = None if expected is None else expected[1]
            if kind == "table" and name.lower() not in _SQLITE_STATISTICS:
                if expected is None or not expected[2]:
                    why = table_refusal(conn, name, sql, known)
                if why is None and expected is not None and not expected[2]:
                    own = constraints.get(name.lower(), [])
                    why = shape_refusal(conn, name, sql, own)
            elif kind == "index" and (expected is None or not expected[2]):
                why = index_refusal(conn, name, known)
            if why is not None:
                break
    if why is not None:
        texts = {str(n).lower(): s for k, n, _t, s in rows if str(k) == "table"}
        if "meta" in texts:
            _refuse_later(conn, path, texts["meta"], why)
        raise StoreRefusedError(
            path,
            f"it is not a store this build wrote ({why})",
            holds_meta="meta" in texts,
        )


def _refuse_later(
    conn: sqlite3.Connection, path: Path, sql: str | None, why: str
) -> None:
    """Raise `LaterStoreError` when a file `admit_store` refused has a
    ``schema_revision`` above this build's (a later build may have added
    what this build refuses), in `check_versions`' words when its
    ``min_reader_revision`` is too, and with what it does not admit either
    way: a forged revision never reads exactly as a later store. Three keys are read, under the deadline
    and the length limit, from a plain ``meta`` `table_refusal` admits; a
    meta not read so, or a revision not a whole number, leaves the refusal."""
    from . import __version__

    keys = (SCHEMA_REVISION_KEY, MIN_READER_KEY, ENGINE_VERSION_KEY)
    try:
        with _deadline(conn, path, "meta table", length=_ADMISSION_MAX_LENGTH):
            if _VIRTUAL.match(_blanked(sql)) or table_refusal(conn, "meta", sql):
                return
            query = "SELECT key, value FROM meta WHERE key IN (?, ?, ?)"
            found = dict(conn.execute(query, keys).fetchall())
    except (StoreRefusedError, sqlite3.Error):
        return
    revision, reader, engine = (str(found.get(key, "")) for key in keys)
    if not _WHOLE.fullmatch(revision) or int(revision) <= _schema.SCHEMA_REVISION:
        return
    newest = f" (its newest writer was bettermemory {_shown(engine)})" if engine else ""
    ours = f"this build, bettermemory {__version__}, reads revision {_schema.SCHEMA_REVISION}"
    key, needed = SCHEMA_REVISION_KEY, int(revision)
    said = f"is at schema revision {needed} (meta's {key}){newest}, and {ours}"
    if _WHOLE.fullmatch(reader) and int(reader) > _schema.SCHEMA_REVISION:
        key, needed = MIN_READER_KEY, int(reader)
        said = f"needs a reader at schema revision {needed} or later (meta's {key})"
        said += f"{newest}; {ours}"
    message = f"{path} {said} and does not admit what it holds ({why}): upgrade "
    message += "bettermemory to open it"
    raise LaterStoreError(path, why, message, key=key, needed=needed)


def read_store_meta(conn: sqlite3.Connection, path: Path) -> dict[str, str]:
    """The meta table of a store file `admit_store` admitted, read under
    the same deadline: its rows are the file's to choose, as many as it
    likes. A file with no meta table raises `sqlite3.OperationalError`, as
    the read always did."""
    with _deadline(conn, path, "meta table"):
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {str(row[0]): str(row[1]) for row in rows}
