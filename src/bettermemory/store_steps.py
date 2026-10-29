"""The store's numbered schema steps and the runner that applies them
(ENG-107).

A step is one change to the store's schema, numbered in the order builds
made them. `store_schema.SCHEMA_REVISION` is the number of the newest step
this build knows, and meta's ``schema_revision`` the newest step a store
has had: a store this build creates has had every step (`Store.create`
writes the revision with the tables), and an open by an owner
(``allow_rekey``) runs, in order, each step whose number is above the
store's revision (`run_steps`, from `store_versions.check_versions`, after
the store's minimums have admitted this build). A reader's open runs none.

Each step says what it does to the builds around it (CONTRIBUTING.md,
"Store versions"): `reader` and `writer` are the minimum reader and writer
revisions a store needs once the step has run, and the runner raises
meta's minimums to them, never lowering one. `store_schema`'s
`MIN_READER_REVISION` and `MIN_WRITER_REVISION`, the minimums a store this
build creates carries, are the highest any step names
(`tests/test_store_steps.py` pins it).

- An additive step adds what a store lacks and nothing else: its `apply`
  is idempotent, takes its own write transaction, and leaves a store it
  cannot write (a read-only file) as it is. The runner records its number
  after it. A crash between the two runs it again at the next open, which
  finds nothing to add.
- A step that is not additive changes what is there. The runner takes the
  store's write lock (``BEGIN IMMEDIATE``), reads the revision again under
  it (another process may have run the step meanwhile), writes a backup
  bundle of the store as it stands (`backup.create_backup`: an online
  snapshot read through its own connection while the lock keeps every
  writer out, so the bundle is exactly the store before the step, with
  the manifest signed under the store's key when this machine holds it),
  verifies the bundle (`backup.verify_bundle`: its files against the
  manifest and its log and tables folded again), then runs `apply` inside
  that one transaction and records the step in the same transaction. A
  failure anywhere, a crash included, leaves the store as it was and
  readable by every build that read it before; the bundle stays where it
  was written, and the next owner's open takes another and runs the step
  again. A step whose bundle cannot be written, or does not verify (any
  status but ``ok``, or ``unverifiable`` for a store whose key is not
  held), does not run, and the open fails naming why. A reader's open of
  a store waiting on such a step reads the store as it stands and writes
  nothing (``PRAGMA query_only``): `backup verify` and `backup restore`
  read a bundle taken before the step so, and a step must leave the store
  readable by its own build before it has run.

Each step the runner applies is also listed in meta's ``schema_steps``,
a JSON list of ``{step, name, applied, by, backup}`` (``backup`` the bundle
directory relative to the store's, null for an additive step), so the
bundle taken before a step can be found again. The list is a record for
people: no check reads it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .store_schema import (
    MIN_READER_KEY,
    MIN_WRITER_KEY,
    SCHEMA_REVISION_KEY,
    StoreVersionError,
    _add_membership_tables,
)
from .time_utils import isoformat_utc

log = logging.getLogger("bettermemory.store")

#: meta's list of the steps the runner applied (the module docstring).
SCHEMA_STEPS_KEY = "schema_steps"

#: What a step's `apply` is called with: the opener's connection, the store
#: file and the opener's meta, which an additive step may update.
Apply = Callable[[sqlite3.Connection, Path, dict[str, str]], None]


@dataclass(frozen=True)
class Step:
    """One numbered change to the store's schema. `reader` and `writer`
    are the minimum reader and writer revisions a store needs once the
    step has run; `additive` says how the runner applies it (the module
    docstring)."""

    number: int
    name: str
    additive: bool
    reader: int
    writer: int
    apply: Apply


#: Every step this build knows, in order, numbered from 1 without a gap.
#: Step 1 is the additive step of decision D9 (`_add_membership_tables`):
#: the membership tables, the store id of design 8.7, the optional
#: membership columns and the indexes of `ADDITIVE_SCHEMA`. `Store.open`
#: also runs it at every open, as before this runner, so a store whose
#: file could not be written then gets what it lacks at a later open; on
#: a store that has it, it reads `sqlite_master` once and writes nothing.
#: Revision 1 is the first revision of the contract, so its minimums are
#: 1: a build that reads them reads revision 1.
STEPS: tuple[Step, ...] = (
    Step(
        number=1,
        name="the membership tables and the additive indexes",
        additive=True,
        reader=1,
        writer=1,
        apply=_add_membership_tables,
    ),
)


def _read_versions(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "SELECT key, value FROM meta WHERE key IN (?, ?, ?, ?)",
            (SCHEMA_REVISION_KEY, MIN_READER_KEY, MIN_WRITER_KEY, SCHEMA_STEPS_KEY),
        )
    }


def _whole(raw: str | None) -> int:
    """A revision as meta holds it; `check_versions` has refused a store
    whose value is not a whole number, so anything else here reads as 0."""
    if raw is None or not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
        return 0
    return int(raw)


def _step_entries(raw: str | None) -> list[object]:
    try:
        entries = json.loads(raw) if raw is not None else []
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


def _record(
    conn: sqlite3.Connection,
    meta: dict[str, str],
    step: Step,
    backup: str | None,
    version: str,
) -> None:
    """Record `step` in meta inside the caller's transaction: the revision
    raised to its number, the minimums to its own, never lowered, and an
    entry appended to ``schema_steps``. Read again on `conn`, under the
    caller's write lock."""
    current = _read_versions(conn)
    entries = _step_entries(current.get(SCHEMA_STEPS_KEY))
    entries.append(
        {
            "step": step.number,
            "name": step.name,
            "applied": isoformat_utc(datetime.now(timezone.utc)),
            "by": version,
            "backup": backup,
        }
    )
    values = {
        SCHEMA_REVISION_KEY: str(
            max(_whole(current.get(SCHEMA_REVISION_KEY)), step.number)
        ),
        MIN_READER_KEY: str(max(_whole(current.get(MIN_READER_KEY)), step.reader)),
        MIN_WRITER_KEY: str(max(_whole(current.get(MIN_WRITER_KEY)), step.writer)),
        SCHEMA_STEPS_KEY: json.dumps(entries, sort_keys=True),
    }
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )
    meta.update(values)


def _run_additive(
    conn: sqlite3.Connection, path: Path, meta: dict[str, str], step: Step, version: str
) -> bool:
    """Apply an additive step, then record it in a transaction of its own.
    False when the record could not be written (a read-only file, or a
    writer holding the lock past the busy timeout): the step then stays
    pending, and the runner stops so no later step is recorded above it."""
    step.apply(conn, path, meta)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _whole(_read_versions(conn).get(SCHEMA_REVISION_KEY)) < step.number:
                _record(conn, meta, step, None, version)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.OperationalError as exc:
        log.info(
            "store %s: schema step %d (%s) not recorded (%s); the next writing "
            "open records it",
            path,
            step.number,
            step.name,
            exc,
        )
        return False
    return True


def _backup(path: Path, keys_dir: Path | None, step: Step) -> str:
    """The bundle taken before `step`, as its directory relative to the
    store's. Raises `StoreVersionError` when it cannot be written."""
    from . import backup

    try:
        made = backup.create_backup(path, keys_dir=keys_dir)
    except backup.BackupError as exc:
        raise StoreVersionError(
            f"{path} needs schema step {step.number} ({step.name}), which runs "
            f"only after a backup, and the backup failed: {exc}; the store is "
            "unchanged"
        ) from exc
    for warning in made["warnings"]:
        log.warning(
            "store %s: backup before schema step %d: %s", path, step.number, warning
        )
    return os.path.relpath(made["bundle"], path.parent)


def _verified(path: Path, keys_dir: Path | None, step: Step, bundle: str) -> None:
    """Check the bundle written before `step` (`backup.verify_bundle`),
    under the runner's write lock. Raises `StoreVersionError` unless it
    reads ``ok``, or ``unverifiable`` (the manifest of a store whose key
    this machine does not hold is unsigned, which `_backup` warned of)."""
    from . import backup

    report = backup.verify_bundle(path.parent / bundle, keys_dir=keys_dir)
    status = report["status"]
    if status in (backup.OK, backup.UNVERIFIABLE):
        return
    problems = ", ".join(str(p.get("problem")) for p in report["problems"][:3])
    raise StoreVersionError(
        f"{path} needs schema step {step.number} ({step.name}); its backup "
        f"{bundle} reads {status} ({problems}), so the step did not run and the "
        "store is unchanged"
    )


def _run_with_backup(
    conn: sqlite3.Connection,
    path: Path,
    meta: dict[str, str],
    step: Step,
    keys_dir: Path | None,
    version: str,
) -> bool:
    """Apply a step that is not additive: under the write lock, after a
    backup, in one transaction with its record (the module docstring).
    False when the store, read again under the lock, has had the step."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise StoreVersionError(
            f"{path} needs schema step {step.number} ({step.name}) and cannot "
            f"be written now ({exc}); the store is unchanged"
        ) from exc
    try:
        current = _read_versions(conn)
        if _whole(current.get(SCHEMA_REVISION_KEY)) >= step.number:
            meta.update(current)
            conn.execute("COMMIT")
            return False
        bundle = _backup(path, keys_dir, step)
        _verified(path, keys_dir, step, bundle)
        step.apply(conn, path, meta)
        _record(conn, meta, step, bundle, version)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    log.warning(
        "store %s: schema step %d (%s) applied; the store as it was before it is "
        "the bundle %s",
        path,
        step.number,
        step.name,
        bundle,
    )
    return True


def run_steps(
    conn: sqlite3.Connection,
    path: Path,
    meta: dict[str, str],
    *,
    writing: bool,
    keys_dir: Path | str | None = None,
) -> tuple[int, ...]:
    """Bring the store up to this build's revision and return the numbers
    of the steps applied. Each step above meta's ``schema_revision`` runs
    in order at an owner's open (`writing`); a reader's open applies none,
    and while a step that is not additive is pending it reads the store as
    it stands with every write refused (``PRAGMA query_only``), as a
    reader of a backup bundle taken before the step does. `keys_dir` is
    the opener's keys directory, which signs a backup's manifest."""
    from . import __version__

    pending = [s for s in STEPS if s.number > _whole(meta.get(SCHEMA_REVISION_KEY))]
    if not pending:
        return ()
    if not writing:
        if any(not step.additive for step in pending):
            conn.execute("PRAGMA query_only = ON")
        return ()
    keys = Path(keys_dir) if keys_dir is not None else None
    applied: list[int] = []
    for step in pending:
        if step.additive:
            if not _run_additive(conn, path, meta, step, __version__):
                break
        elif not _run_with_backup(conn, path, meta, step, keys, __version__):
            continue
        applied.append(step.number)
    return tuple(applied)
