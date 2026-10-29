"""The store and key checks of `bettermemory doctor`: `check_store`,
the store file and its key from one read-only open; `check_restore`, a
restore that holds the store or did not finish; and `check_index`, the
search index as `store_index.doctor_check` reads it.

They live apart from `doctor.py` to keep that module under the package's
2,000-line bound (ENG-96). They import nothing from `doctor`: they build
their verdicts from `doctor_model`, and `doctor` imports this module,
re-exports the checks and the names its tests read, and runs the checks
in their place in `CHECK_IDS`. Each other module they read (`store`,
`store_schema`, `store_index`, `backup`, `restore_lock`) is imported in
the function that reads it, as it was in `doctor`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from .doctor_model import (
    FAIL,
    OK,
    WARN,
    Check,
    Finding,
    _Context,
    _from_findings,
    _not_checked,
)

if TYPE_CHECKING:
    from pathlib import Path

    from .log import Head, KeyRing
    from .store_keys import KeyState

# ---------------------------------------------------------------------------
# store and key
# ---------------------------------------------------------------------------


def _open_remedy(path: Path, exc: Exception) -> str:
    """The fix for a store `Store.open` refused, by what refused it. Only
    a file SQLite cannot read as a database, or one without its identity,
    is damaged; a lock held past the busy timeout, a file or directory
    this user cannot open, and an I/O error are not, and restoring a copy
    over a sound store would lose every write since the copy. A file
    every opener refuses (`store_schema.StoreRefusedError`) is not damaged
    either: it is not a store this build wrote, and nothing reads it."""
    from .store import StoreRefusedError
    from .store_schema import StoreVersionError

    cause = getattr(exc, "__cause__", None)
    if (
        isinstance(exc, StoreRefusedError)
        and isinstance(cause, sqlite3.DatabaseError)
        and getattr(cause, "sqlite_errorcode", 0) & 0xFF in (11, 26)
    ):
        # A schema SQLite cannot read (SQLITE_CORRUPT, SQLITE_NOTADB;
        # `admit_store`): damaged, or not a database at all, as before the
        # check, and restoring a copy is the fix.
        exc = cause
    if isinstance(exc, StoreVersionError) and exc.needed is not None:
        return (
            f"open it with a bettermemory build that reads schema revision "
            f"{exc.needed} or later (meta's {exc.key}), or run `bettermemory "
            "backup restore` under that build; doctor leaves the file as it is"
        )
    if isinstance(exc, StoreRefusedError):
        if exc.timed_out:
            return (
                f"check the disk under {path} and run doctor again; a store this "
                "build wrote answers its first reads at once, and doctor leaves "
                "the file as it is"
            )
        return (
            f"move {path} aside and find out what wrote it: a store this build "
            "writes holds only its own schema's tables, indexes and triggers, and "
            "one a repository shipped is not yours to serve; doctor leaves the "
            "file as it is"
        )
    if "this build reads up to" in str(exc):
        return "upgrade bettermemory: a newer release wrote this store"
    if isinstance(exc, StoreVersionError) and exc.key is not None:
        return (
            f"set meta's {exc.key} back to a whole number, or restore the store "
            "from a backup taken before the edit; doctor leaves the file as it is"
        )
    if isinstance(exc, (sqlite3.OperationalError, OSError)):
        return (
            f"check that {path} and its directory are readable and writable by "
            "this user and that no process holds the store locked, then run "
            "doctor again; doctor leaves the file as it is"
        )
    return "restore the store from a copy; doctor leaves the file as it is"


#: The remedy of a head checkpoint the log does not end at: the commands
#: that read it, roll back to a backup, or accept an older copy on the
#: record, never a key-ring file to place or delete.
_ROLLBACK_REMEDY = (
    "run `bettermemory log verify`; to go back to a backup, "
    "`bettermemory backup restore`; for an older copy put back on "
    "purpose, `bettermemory log accept-history --yes` accepts its "
    "history on the record"
)


def _head_finding(
    *,
    head_seq: int | None,
    head_mac: str | None,
    tail: tuple[int, str] | None,
    head_path: Path,
) -> Finding | None:
    """The head checkpoint against the log's last row, the one comparison
    of `log verify`'s that costs a single indexed read: an absent head
    leaves the log's end unverifiable, and a head past the last row, or
    naming the last row with another MAC, means the log lost or changed
    rows the checkpoint saw (a tail cut, or an older copy restored by
    hand). Doctor does not walk the chain; `log verify` does, and is the
    remedy."""
    if head_seq is None:
        return Finding(
            WARN,
            f"no head checkpoint at {head_path}: `bettermemory log verify` cannot "
            "check the log's end",
            "the next write records one; nothing in the keys directory is placed "
            "by hand",
        )
    if tail is None or head_seq > tail[0]:
        last = (
            "the log has no rows" if tail is None else f"the log ends at seq {tail[0]}"
        )
        return Finding(
            WARN,
            f"the head checkpoint names seq {head_seq}, but {last}",
            _ROLLBACK_REMEDY,
        )
    if head_seq == tail[0] and head_mac != tail[1]:
        return Finding(
            WARN,
            f"the head checkpoint names seq {head_seq} with another MAC than the log's",
            _ROLLBACK_REMEDY,
        )
    return None


def _bundle_check(path: Path, details: dict[str, Any]) -> list[Check] | None:
    """The `store` failure for a store file inside a backup bundle (the
    config or `BETTERMEMORY_DIR` naming a bundle directory), or None.
    Doctor does not open it: even its read-only open switches the
    snapshot to WAL, which rewrites the header, and the bundle would then
    fail its signed hash (`backup.bundle_manifest_beside`). It reads as a
    store that does not open, `fail` and exit 1, with the key unchecked."""
    from .backup import bundle_manifest_beside

    manifest = bundle_manifest_beside(path)
    if manifest is None:
        return None
    details["bundle_manifest"] = str(manifest)
    return [
        Check(
            "store",
            FAIL,
            f"{path} is in a backup bundle ({manifest} is beside it); doctor "
            "does not open it, since opening it would change it and fail the "
            "bundle's hash",
            f"copy {path.name} out of the bundle to a directory of its own first "
            "and point the store directory at the copy; `bettermemory backup "
            "verify` checks a bundle where it is",
            details,
        ),
        _not_checked("key", "the store is a backup bundle's file", "copy it out first"),
    ]


def check_restore(ctx: _Context) -> Check:
    """A restore that did not finish (fail) or one running (warn): either
    refuses every writing open of the store, which the store check's
    read-only open does not meet (`restore_lock.state_finding`)."""
    from .restore_lock import CLEAR, RUNNING, state_finding
    from .store import STORE_FILENAME

    if ctx.directory is None:
        why, first = "the store's directory is not known", "fix the config first"
        return _not_checked("restore", why, first)
    state, summary, remedy, details = state_finding(ctx.directory / STORE_FILENAME)
    status = {CLEAR: OK, RUNNING: WARN}.get(state, FAIL)
    return Check("restore", status, summary, remedy, details)


def _missing_store_checks(
    directory: Path, path: Path, details: dict[str, Any]
) -> list[Check]:
    """Check a store directory with no store file in it; returns the store and key checks."""
    from .store import STORE_FILENAME, holds_v8_files

    if path.exists():
        return [
            Check(
                "store",
                FAIL,
                f"{path} is not a file",
                f"move whatever is at {path} aside",
                details,
            ),
            _not_checked("key", "the store does not open", "fix the store first"),
        ]
    if holds_v8_files(directory):
        return [
            Check(
                "store",
                FAIL,
                f"{directory} holds a bettermemory 8 store and no "
                f"{STORE_FILENAME}: no daemon starts there",
                "run `bettermemory migrate v8` to import it",
                details,
            ),
            Check("key", OK, "no store yet; its key is written with it"),
        ]
    return [
        Check(
            "store",
            OK,
            f"no store yet at {path}; the first session creates it",
            None,
            details,
        ),
        Check("key", OK, "no store yet; its key is written with it"),
    ]


def _key_check(
    keyring: KeyRing,
    state: KeyState,
    status: dict[str, Any],
    head: Head | None,
    tail: sqlite3.Row | None,
) -> Check:
    """Check the key the open read and, when it is present, the head against the log's last row; returns the key check."""
    from .store import KEY_PRESENT

    key_details: dict[str, Any] = {
        "keys_dir": str(keyring.keys_dir),
        **state.details(),
        "head_seq": status["head_seq"],
    }
    if state.state == KEY_PRESENT:
        head_finding = _head_finding(
            head_seq=None if head is None else head.seq,
            head_mac=None if head is None else head.mac,
            tail=None if tail is None else (int(tail[0]), str(tail[1])),
            head_path=keyring.head_path(),
        )
        key_check = _from_findings(
            "key",
            [] if head_finding is None else [head_finding],
            ok_summary=state.summary,
            details=key_details,
        )
    else:
        key_check = Check("key", FAIL, state.summary, state.remedy, key_details)
    return key_check


def check_store(ctx: _Context) -> list[Check]:
    """The store file (`store`) and its key (`key`), from one read-only
    open. A missing store is the first-run state, not a fault: the first
    session creates it. A directory that holds an un-migrated 8.x store
    and no store file is one: a daemon refuses to start there. The key
    check renders the state the open read (`store.Store.key_state`,
    `store_keys.key_state`) in its words, as posture's ``key.match``
    finding does: ok only when the key is present, a failure naming the
    remedy when it is unreadable, missing, foreign or bound elsewhere."""
    from .store import (
        SCHEMA_VERSION,
        STORE_FILENAME,
        Store,
    )
    from .store_index import index_report

    if ctx.directory is None:
        why = "the store's directory is not known"
        remedy = "fix the config first"
        return [_not_checked("store", why, remedy), _not_checked("key", why, remedy)]
    path = ctx.directory / STORE_FILENAME
    details: dict[str, Any] = {"path": str(path)}
    if not path.is_file():
        return _missing_store_checks(ctx.directory, path, details)
    bundle = _bundle_check(path, details)
    if bundle is not None:
        return bundle
    try:
        store = Store.open(path, read_only=True)
    except (ValueError, sqlite3.Error, OSError) as exc:
        return [
            Check(
                "store",
                FAIL,
                f"{path} does not open: {exc}",
                _open_remedy(path, exc),
                details,
            ),
            _not_checked("key", "the store does not open", "fix the store first"),
        ]
    with store:
        status = store.status()
        orphans = store.store_orphans()
        keyring = store.keyring
        state = store.key_state
        head = keyring.read_head()
        tail = store.conn.execute(
            "SELECT seq, mac FROM log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        ctx.index = index_report(store)
    details.update(
        {
            "store_id": status["store_id"],
            "schema_version": status["schema_version"],
            "memories": status["memories"],
            "tombstones": status["tombstones"],
            "episodes": status["episodes"],
            "log_rows": status["log_rows"],
            "size_bytes": status["size_bytes"],
            "orphans": [str(orphan) for orphan in orphans],
        }
    )
    findings: list[Finding] = []
    if status["schema_version"] != SCHEMA_VERSION:
        # `Store.open` refused a newer schema above, so this one is older
        # than any release writes: upgrading would not help.
        findings.append(
            Finding(
                WARN,
                f"{path} is schema {status['schema_version']}; this build "
                f"writes schema {SCHEMA_VERSION}",
                f"no release writes schema {status['schema_version']}: run "
                "`bettermemory log verify` to check the store",
            )
        )
    if orphans:
        name = path.name
        findings.append(
            Finding(
                WARN,
                f"{path} was created fresh after the previous store was lost; "
                f"that store is saved beside it as {orphans[-1].name}",
                f"to restore it: close every session using bettermemory, run "
                f"`bettermemory down`, delete {name}-wal and {name}-shm, then copy "
                f"the saved file over {name}; delete the saved file to keep the "
                "fresh store",
            )
        )
    store_check = _from_findings(
        "store",
        findings,
        ok_summary=(
            f"{path} opens: schema {status['schema_version']}, "
            f"{status['memories']} memories, {status['log_rows']} log rows"
        ),
        details=details,
    )
    key_check = _key_check(keyring, state, status, head, tail)
    return [store_check, key_check]


def check_index(ctx: _Context) -> Check:
    """The store file's pages and its search index (`index`):
    `store_index.doctor_check`, on what the store check's open found, or
    a read-only open of its own when that check reached none."""
    from .store_index import doctor_check

    return doctor_check(ctx.directory, ctx.index)
