"""Quiesce legacy connections before introducing a writer they cannot gate.

Revision 5 checks the writer minimum inside every write transaction. Earlier
connections only checked at open, so an upgrade that first excludes them must
prove they are closed. The restore lock keeps new openers off that proof and
the schema steps; SQLite's exclusive mode finds already-open WAL connections.
The mode returns to normal before applying steps so their backup readers work.
No restore marker is written, and a crash leaves an idempotent pending step.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from . import restore_lock
from ._printable import quoted
from .store_versions import StoreVersionError

# The first build that rechecks the writer minimum within its write lock.
TRANSACTION_WRITER_REVISION = 5


def _prove_exclusive(conn: sqlite3.Connection, path: Path) -> None:
    timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    mode = str(conn.execute("PRAGMA locking_mode").fetchone()[0])
    try:
        conn.execute("PRAGMA busy_timeout = 0")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise StoreVersionError(
            f"{quoted(str(path))} needs a newer writer; another connection has it open. "
            "Close the daemon and every session using this store, then open it again. "
            "No schema step has run."
        ) from exc
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute(f"PRAGMA locking_mode = {mode}")
        # SQLite releases an exclusive-mode hold at the next read after
        # returning to normal. The opener barrier remains held across it.
        conn.execute("PRAGMA schema_version").fetchone()
        conn.execute(f"PRAGMA busy_timeout = {timeout}")


@contextlib.contextmanager
def quiescent(conn: sqlite3.Connection, path: Path) -> Iterator[None]:
    borrowed = restore_lock.held_here(path)
    held = None if borrowed else restore_lock.acquire(path)
    if not borrowed and held is None:
        raise StoreVersionError(
            f"{quoted(str(path))} cannot be upgraded while another opener or restore holds it; open it again"
        )
    try:
        if held is not None and held.previous is not None:
            raise StoreVersionError(
                f"{quoted(str(path))} has an unfinished restore; finish it before upgrading"
            )
        _prove_exclusive(conn, path)
        yield
    finally:
        if held is not None:
            held.release(clear=held.previous is None)
