"""Fixture: a store `Store.create` makes under fixed values, and what it
refuses (``store_create.json``).

The native build creates stores too (``native/``, `store::create`) and is
held to this file: every table's rows, ``sqlite_master``, the pragmas a
file keeps, meta unmasked, the first log row whole, the key ring's file
names, texts and modes, and the store directory's contents.
``store_schema.jsonl`` masks what this file fixes; ``state_files.json``
gives the key ring's texts with placeholders.

What is fixed, and how: the random source `Store.create` draws from
(`secrets.token_hex` answers the store id, then the membership id; the
digest build's own token stays random and reaches no byte;
`secrets.token_bytes(32)` answers the log fixture's KEY_A), the clock
(`store.datetime.now` answers ``created``; `log._utcnow_iso` answers the
log row's ``ts``, then the head file's) and `bettermemory.__version__`.
Each fixed value is checked to have been drawn exactly as listed.

Gates. The bytes depend on the tokenizer's fingerprint and the digest's
(meta, the first row's payload and MAC, ``digest_state``), which read
`unicodedata`: Unicode-gated. Not on the SQLite version: the schema texts
are the reference's own strings, which SQLite keeps as given, the pragmas
read here are the ones the file carries, and FTS5's shadow tables are
compared as they are; a reference SQLite that wrote them otherwise is a
difference to see, not to hide: a SQLite whose FTS5 shadow rows or schema
bookkeeping (``schema_version``, the shadow tables' text) differ fails the
native test loudly, on purpose. Then compare the two SQLites' FTS5 and
schema changes and decide whether the native build follows; do not gate
the file. Not on the platform: modes are the ones
the product asks for (checked on POSIX, written alike everywhere, as
``state_files.json`` does), the binding's path is written as
``<scratch>/...`` with ``/``, and each refusal is its class and errno
name, never an operating system's message. The refusal cases are the
ones every platform can set up (no mode bits, no symbolic links).
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bettermemory
from bettermemory import log as blog
from bettermemory import store as bstore
from bettermemory import store_digest
from bettermemory.store import Store

from ._common import json_doc
from ._sandbox import Scrubber
from .gen_daemon import VERSION
from .gen_log import KEY_A, STORE_ID

MEMBERSHIP_ID = "fedcba9876543210fedcba9876543210"
#: `created` (meta and the payload), the log row's ts, the head's ts: far
#: from any run's clock; the head's has no fraction, as `isoformat` writes
#: a whole second.
CREATED = datetime(2001, 2, 3, 4, 5, 6, 123456, tzinfo=timezone.utc)
LOG_TS = datetime(2001, 2, 3, 4, 5, 6, 234567, tzinfo=timezone.utc)
HEAD_TS = datetime(2001, 2, 3, 4, 5, 7, tzinfo=timezone.utc)

_PRAGMAS = (
    "page_size",
    "journal_mode",
    "auto_vacuum",
    "encoding",
    "user_version",
    "application_id",
    "schema_version",
    "freelist_count",
)


@contextmanager
def _fixed() -> Iterator[None]:
    hexes = [STORE_ID, MEMBERSHIP_ID]
    keys = [KEY_A]
    stamps = [LOG_TS, HEAD_TS]

    def token_hex(nbytes: int | None = None) -> str:
        return hexes.pop(0) if hexes else os.urandom(nbytes or 32).hex()

    def token_bytes(nbytes: int | None = None) -> bytes:
        if nbytes == len(KEY_A) and keys:
            return keys.pop()
        return os.urandom(nbytes or 32)

    def utcnow_iso() -> str:
        if not stamps:
            raise AssertionError("the clock was read more often than fixed")
        return stamps.pop(0).isoformat().replace("+00:00", "Z")

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return CREATED

    with (
        patch.object(secrets, "token_hex", token_hex),
        patch.object(secrets, "token_bytes", token_bytes),
        patch.object(blog, "_utcnow_iso", utcnow_iso),
        patch.object(bstore, "datetime", _Clock),
        patch.object(bettermemory, "__version__", VERSION),
    ):
        yield
    if hexes or keys or stamps:
        raise AssertionError(f"fixed values not drawn: {hexes} {len(keys)} {stamps}")


def _cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    if isinstance(value, float):
        return {"real": repr(value)}
    return value


def _tables(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    names = [
        str(n)
        for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    for name in names:
        try:
            cursor = conn.execute(f'SELECT rowid, * FROM "{name}" ORDER BY rowid')
            rowid = True
        except sqlite3.OperationalError:
            cursor = conn.execute(f'SELECT * FROM "{name}" ORDER BY 1')
            rowid = False
        columns = [d[0] for d in cursor.description]
        out[name] = {
            "columns": columns,
            "rowid": rowid,
            "rows": [[_cell(v) for v in row] for row in cursor.fetchall()],
        }
    return out


def _mode(path: Path, wanted: int) -> str:
    if os.name == "posix":
        seen = stat.S_IMODE(os.lstat(path).st_mode)
        if seen != wanted:
            raise AssertionError(f"{path.name}: mode {seen:o}, expected {wanted:o}")
    return f"{wanted:04o}"


def _listing(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() + ("/" if p.is_dir() else "")
        for p in root.rglob("*")
    )


def _created(orphan: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw, _fixed():
        root = Path(raw)
        clean = Scrubber(root, now=datetime.now(timezone.utc))
        store_dir = root / "store"
        if orphan:
            store_dir.mkdir()
            (store_dir / "memory.sqlite.orphaned-20010203T040506Z.sqlite").write_bytes(
                b""
            )
        store = Store.create(store_dir, keys_dir=root / "keys")
        # Read on the connection that committed the first row (ENG-328).
        writer_synchronous = store.conn.execute("PRAGMA synchronous").fetchone()[0]
        store.close()
        path = store_dir / "memory.sqlite"
        directory = [
            {
                "name": entry.name,
                "mode": _mode(entry, 0o600)
                if entry.name == "memory.sqlite"
                else "(not the store's)",
            }
            for entry in sorted(store_dir.iterdir())
        ]
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            master = [
                list(row)
                for row in conn.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "ORDER BY type, name"
                )
            ]
            pragmas = {p: conn.execute(f"PRAGMA {p}").fetchone()[0] for p in _PRAGMAS}
            tables = _tables(conn)
        finally:
            conn.close()
        keys = root / "keys"
        ring = []
        for entry in sorted(keys.iterdir()):
            item: dict[str, Any] = {"name": entry.name, "mode": _mode(entry, 0o600)}
            if entry.suffix in (".head", ".origin", ".path"):
                item["text"] = clean.paths(entry.read_text(encoding="utf-8"))
            elif entry.suffix == ".key":
                item["sha256"] = hashlib.sha256(entry.read_bytes()).hexdigest()
            else:
                item["size"] = entry.stat().st_size
            ring.append(item)
        return {
            "store_dir_mode": _mode(store_dir, 0o700),
            "keys_dir_mode": _mode(keys, 0o700),
            "store_dir": directory,
            "sqlite_master": master,
            "pragmas": pragmas,
            "tables": tables,
            "key_ring": ring,
            "writer_synchronous": writer_synchronous,
        }


def _other_wal(root: Path) -> None:
    """Another database's live WAL and its index at the store's name: a
    WAL-mode file with autocheckpoint off, one table and a row, its
    ``-wal`` and ``-shm`` copied while it is open."""
    other = root / "other.sqlite"
    conn = sqlite3.connect(other, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('store_id', ?)", ("f" * 32,))
        for suffix in ("-wal", "-shm"):
            shutil.copyfile(
                f"{other}{suffix}", root / "store" / f"memory.sqlite{suffix}"
            )
    finally:
        conn.close()


def _junk(root: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        (root / "store" / f"memory.sqlite{suffix}").write_bytes(b"junk" * 64)


def _hot_journal(root: Path) -> None:
    """A rollback journal of another database's open transaction (a hot
    journal), copied while the transaction is open."""
    other = root / "other.sqlite"
    conn = sqlite3.connect(other, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("CREATE TABLE a (b BLOB)")
        conn.execute("INSERT INTO a VALUES (zeroblob(4000))")
        conn.execute("PRAGMA cache_size = 1")
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO a VALUES (zeroblob(4000))", [() for _ in range(64)]
        )
        shutil.copyfile(f"{other}-journal", root / "store" / "memory.sqlite-journal")
        conn.execute("ROLLBACK")
    finally:
        conn.close()


_SIDECARS: dict[str, tuple[str, Callable[[Path], None]]] = {
    "another_stores_live_wal": (
        "store/memory.sqlite-wal and -shm: another WAL database's, live (one "
        "table meta, store_id f*32, autocheckpoint off)",
        _other_wal,
    ),
    "junk_sidecars": (
        "store/memory.sqlite-wal, -shm and -journal: 256 bytes of 'junk'",
        _junk,
    ),
    "hot_journal": (
        "store/memory.sqlite-journal: another database's hot rollback journal",
        _hot_journal,
    ),
}


def _created_over(setup: Callable[[Path], None]) -> dict[str, Any]:
    """Create where stale sidecars stand at the store's name, then what is
    in the store directory, whose store id the file opened there holds,
    and its `log_verify`."""
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "store").mkdir()
        setup(root)
        before = sorted(p.name for p in (root / "store").iterdir())
        with _fixed():
            Store.create(root / "store", keys_dir=root / "keys").close()
        after = sorted(p.name for p in (root / "store").iterdir())
        reader = Store.open(root / "store", keys_dir=root / "keys", read_only=True)
        try:
            status = reader.log_verify()["status"]
            store_id = reader.conn.execute(
                "SELECT value FROM meta WHERE key = 'store_id'"
            ).fetchone()[0]
        finally:
            reader.close()
        return {
            "before": before,
            "after_create": after,
            "store_id": store_id,
            "log_verify": status,
        }


def _refused(setup: Callable[[Path], tuple[Path, Path]]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as raw, _fixed():
        root = Path(raw)
        clean = Scrubber(root, now=datetime.now(timezone.utc))
        store, keys = setup(root)
        before = _listing(root)
        try:
            Store.create(store, keys_dir=keys).close()
        except Exception as exc:
            refusal: dict[str, Any] = {"class": type(exc).__name__}
            number = getattr(exc, "errno", None)
            if isinstance(exc, OSError) and number is not None:
                refusal["errno"] = errno.errorcode.get(number, str(number))
            else:
                refusal["message"] = clean.paths(str(exc))
        else:
            raise AssertionError("created where a refusal was expected")
        after = _listing(root)
        refusal["before"] = before
        refusal["left"] = sorted(set(after) - set(before))
        # The fixed values were not all drawn: drain them, so `_fixed`
        # checks nothing a refusal never reached.
        secrets.token_hex()
        secrets.token_hex()
        secrets.token_bytes(len(KEY_A))
        blog._utcnow_iso()
        blog._utcnow_iso()
        return refusal


def _store_file(root: Path) -> tuple[Path, Path]:
    (root / "store").mkdir()
    (root / "store" / "memory.sqlite").write_bytes(b"")
    return root / "store", root / "keys"


def _store_dir(root: Path) -> tuple[Path, Path]:
    (root / "store" / "memory.sqlite").mkdir(parents=True)
    return root / "store", root / "keys"


def _parent_file(root: Path) -> tuple[Path, Path]:
    (root / "store").write_bytes(b"")
    return root / "store", root / "keys"


def _keys_file(root: Path) -> tuple[Path, Path]:
    (root / "keys").write_bytes(b"")
    return root / "store", root / "keys"


#: What an unfinished restore leaves beside the store (`restore_lock`'s
#: marker), with a control character a refusal must not print raw.
MARKER = (
    '{"pid": 12, "started_at": "2001-02-03T04:05:06Z", '
    '"bundle": "/bundles/b\\u001b1", "stage": "swap"}'
)


def _marker(root: Path) -> tuple[Path, Path]:
    (root / "store").mkdir()
    (root / "store" / "memory.sqlite.restore-lock").write_text(MARKER, "utf-8")
    return root / "store", root / "keys"


_REFUSALS: dict[str, tuple[str, Callable[[Path], tuple[Path, Path]]]] = {
    "store_file_exists": (
        "store/memory.sqlite is an empty file; create(store, keys)",
        _store_file,
    ),
    "store_path_is_a_directory": (
        "store/memory.sqlite is a directory; create(store, keys)",
        _store_dir,
    ),
    "store_parent_is_a_file": ("store is a file; create(store, keys)", _parent_file),
    "keys_dir_is_a_file": ("keys is a file; create(store, keys)", _keys_file),
    "restore_marker_beside": (
        "store/memory.sqlite.restore-lock holds the marker MARKER; create(store, keys)",
        _marker,
    ),
}


def generate() -> dict[str, bytes]:
    doc = {
        "fixture": "store_create",
        "read": (
            "created: Store.create(<scratch>/store, keys_dir=<scratch>/keys) under "
            "the fixed values; created_after_orphans: the same with an empty "
            "store/memory.sqlite.orphaned-20010203T040506Z.sqlite beside it first. "
            "tables: every table (sqlite_master type 'table', by name), its "
            "columns, and its rows in rowid order (rowid first) or, for a "
            "WITHOUT ROWID table, by the first column; a value is JSON, a blob "
            "{blob: hex}, a float {real: repr}. key_ring: each file of the keys "
            "directory, its mode as asked for (checked on POSIX), the text of "
            "head, origin and binding (the scratch directory as <scratch>), the "
            "key's sha256. refusals: each case's setup, the class raised, the "
            "errno's name for an OSError, else the message, and the entries the "
            "refused create left beside the setup's."
        ),
        "fixed": {
            "store_id": STORE_ID,
            "membership_store_id": MEMBERSHIP_ID,
            "key": "the log fixture's KEY_A, bytes 0..31",
            "key_fingerprint": hashlib.sha256(KEY_A).hexdigest(),
            "created": CREATED.isoformat().replace("+00:00", "Z"),
            "log_ts": LOG_TS.isoformat().replace("+00:00", "Z"),
            "head_ts": HEAD_TS.isoformat().replace("+00:00", "Z"),
            "engine_version": VERSION,
        },
        "marker": MARKER,
        "digest_fingerprint": store_digest.fingerprint(),
        "digest_schema": store_digest._SCHEMA,
        "created": _created(orphan=False),
        "created_after_orphans": _created(orphan=True),
        "created_over_sidecars": {
            name: {"setup": text, **_created_over(setup)}
            for name, (text, setup) in _SIDECARS.items()
        },
        "refusals": {
            name: {"setup": text, **_refused(setup)}
            for name, (text, setup) in _REFUSALS.items()
        },
    }
    return {"store_create.json": json_doc(doc)}
