"""Verified online backups of a bettermemory 9 store, and a safe restore.

A backup is a *bundle*: one directory, owner-only, that holds everything
needed to put the store back and to prove it is the store that was saved.

* ``memory.sqlite`` is a snapshot taken with SQLite's online backup API
  from a read-only connection. Under WAL a reader sees one committed state
  for the whole copy while the daemon keeps writing, so the snapshot is
  consistent without stopping anything: a transaction open in another
  connection is not in it, and nothing it copies is half a transaction. A
  plain file copy can miss the WAL or catch the main file between two
  checkpoints. The snapshot is switched to rollback journal mode before it
  is hashed, so it is one self-contained file that opens without a ``-wal``
  or ``-shm`` beside it.
* ``<store_id>.head`` is the head checkpoint at the snapshot: the
  snapshot's own last log row, in the format ``log.KeyRing`` reads. It is
  not the live head file read at some moment near the copy, which can lag
  the snapshot (a writer committed and has not yet moved the head) or lead
  it (a writer committed after the snapshot began), and a head that leads
  the log reads ``tail_deleted``. The bundle's files are named the way a
  keys directory names them, so the bundle directory is itself a key ring
  for its store (`KeyRing(bundle, store_id)`).
* ``<store_id>.key`` and the retired ``<store_id>.<prefix>.key`` files,
  only with ``include_key``: every key a segment of the snapshot's log
  names that this host holds. Anyone holding such a bundle can sign rows
  as the store, so the default leaves the keys out and the manifest names
  the fingerprints they must match. A bundle that carries the key its
  manifest is signed with is also one anyone who can write it can remake
  under a key of their own: the signature shows the bundle is whole, not
  who wrote it. So a signature counts only under a key the keys directory
  holds; one made with a key only the bundle carries reads
  ``self_signed`` and the bundle ``unverifiable``, and a restore puts it
  back (installing its key as the store's current key and its origin
  anchor) only when told to trust the bundle's key, as on a fresh machine
  restoring from a bundle kept where no one else can write.
* ``manifest.json``, written last, so a bundle without one is a bundle
  whose creation did not finish. It records the format, the bettermemory
  version, the store id, the key fingerprints, the head (seq and MAC), row
  counts per table, the size and SHA-256 of every other file, and the time.
  It is signed: ``mac`` is an HMAC under the store's current key, which no
  file in the bundle replaces without the key (the keys directory's copy
  of it: see above for a bundle that carries its own). In the live store
  the head checkpoint sits in the keys directory, apart from the store
  file, which is what lets it catch a deleted tail: an editor of the store
  file cannot move it. In a bundle the head sits beside the snapshot, and the signature
  restores the separation: an editor who truncates the snapshot's log and
  rewrites the head and the hashes to match cannot re-sign the manifest. A
  bundle written where the store's key is not held carries no signature and
  verifies as ``unverifiable`` at best. When this host's key ring holds an
  origin anchor for the store (where its history of the store begins,
  `log.KeyRing.read_origin`) and the snapshot reaches it, the manifest
  records it as ``origin``, so the signature covers it as it covers the
  head.

Verification never opens a bundle's file in place. `Store.open` runs the
additive schema step and sets WAL mode on the file it opens, so verifying a
backup where it lies would change it. `verify_bundle` checks every file
against the manifest, checks the signature, then copies the snapshot into a
private temporary directory beside a temporary key ring (the keys from the
keys directory first, then from the bundle, and the head and the origin
anchor from the signed manifest) and runs the store's own `log_verify`
there: MACs, chain, segments, head, origin and the fold, with no change to
`bettermemory.log`.

Restore. Putting an older copy back by hand leaves `log verify` reading
``tampered`` for good: `KeyRing.write_head` only moves forward, so the head
names a row the restored log does not hold (``tail_deleted``) and, once new
rows pass it, a row whose MAC differs (``mismatch``). `restore_bundle`
rewinds the head to the bundle's on the record instead. In order: it
refuses a store file that is itself inside a bundle (its snapshot would be
replaced); it takes the store's daemon lock and the spawn lock
(`_daemon_client.lock_file_for` and `spawn_lock_file_for`), refusing while
anything holds either, so no daemon serves mid-restore and no hook, `up` or
newly started shim spawns one (each spawns through `ensure_daemon`, under the spawn lock); it
verifies the bundle and refuses one that does not verify, save a
``self_signed`` one the caller said to trust (``trust_bundle_key``);
without ``apply`` it stops there with the plan. With it, it takes the
store's restore lock (`restore_lock`), which every writing open respects
from then on, and refuses while any process has the store open
(`restore_lock.probe`: `serve`, the shim's in-process fallback, a CLI
command or a hook, none of which takes the daemon locks).
Then it copies the snapshot into a temporary file beside the store, moves
the store file and its ``-wal`` and ``-shm`` aside as
``<store>.pre-restore-<UTC stamp>`` (with ``-wal`` and ``-shm`` suffixes,
so SQLite pairs them again on open; nothing is deleted), renames the copy
into place, installs any key the restored log needs that the keys directory
lacks, rewinds the head under the head's own lock, installs the bundle's
origin anchor or removes the one the keys directory holds when the bundle
records none (a restored store is never judged against an anchor of a
history it does not hold; the next writing open anchors it again), binds
the key ring to the restored file (`KeyRing.rebind_store`: its keys, head
and anchor are now that file's, and a ring left bound to a store file
elsewhere would open the restored one for reading only), records the
rewind in ``<store_id>.backup_restore.json`` beside the head, appends a
``store_restored`` control row carrying the same entry to the restored
log (`_append_restore_row`), and runs `log_verify` on the restored
store, which must read ``ok``.

The order is chosen for what a failure leaves. The file swap is the step
most likely to fail (Windows refuses to rename a file another process has
open), so it runs before keys or head change, and a failed swap renames
back what it moved. A WAL left beside a copied-in store is replayed over
it, which is why the orphan notice's manual steps delete ``-wal`` and
``-shm`` first; here they are moved aside with their store and the swap
refuses to put a file in place while any of the three names is taken.

The restore lock is held until the restore returns. A restore that
finished, or failed before the store file was replaced, removes its
marker; one that failed after leaves it, as does one that took over the
marker of an earlier restore that did not finish and then failed, and
every writing open refuses the part-restored store until a restore
finishes or `backup unlock` removes it (`restore_lock.unlock`). A writer
that opens without `store._connect` is not held off; none in this
package does.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._fsutil import (
    atomic_write_bytes,
    bounded_read,
    ensure_owner_only_dir,
    flock_excl,
    fsync_dir,
    fsync_file,
    replace_atomic,
    sqlite_file_uri,
)
from . import restore_lock
from .config import STORE_FILENAME
from .log import (
    REKEY,
    STORE_ID_PATTERN,
    STORE_RESTORED,
    Head,
    HistoryOrigin,
    KeyRing,
    KeyUnreadableError,
    canonical_payload,
    fingerprint,
    last_row,
    parse_origin,
    same_mac,
)
from .time_utils import isoformat_utc

if TYPE_CHECKING:
    from .store import Store

#: The manifest's ``format`` and the highest ``format_version`` this build
#: reads. A change a reader of version 1 would misread bumps the version.
BUNDLE_FORMAT = "bettermemory.backup"
BUNDLE_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
SNAPSHOT_NAME = STORE_FILENAME
#: Where `create_backup` puts bundles when the caller names no directory:
#: beside the store file, one directory per bundle under this one.
BACKUPS_DIR_NAME = "backups"
#: The rewind record beside the head: ``<store_id>.backup_restore.json``.
RESTORE_RECORD_SUFFIX = ".backup_restore.json"
RESTORE_RECORD_FORMAT = "bettermemory.backup_restore"
#: What the manifest's signature is computed over, ahead of the manifest's
#: canonical JSON. A log row's MAC input starts with a field marker byte
#: (``\x00`` or ``\x01``, `log.compute_mac`), never with this text, so the
#: one key cannot be made to sign a manifest as a row or a row as a manifest.
_MANIFEST_MAC_CONTEXT = b"bettermemory-backup-manifest-v1\x00"
#: A manifest is a few kilobytes; anything past this is not one.
_MANIFEST_CAP_BYTES = 1 << 20
_DIGEST_CHUNK = 1 << 20
#: A store id: the 32 lowercase hex digits `Store.create` mints, the one
#: shape the key ring takes (`log.STORE_ID_PATTERN`; `log.KeyRing` raises
#: ValueError on any other). The id names the ring's files, in the bundle
#: and in the keys directory, so an id carrying a separator or ``..``
#: would steer create's key and head writes out of the bundle and
#: restore's out of the keys directory. A snapshot or a manifest that
#: names any other id is not a bundle this reads, and is refused as one
#: before a key ring is built from it.
_STORE_ID_RE = STORE_ID_PATTERN
#: A SHA-256 or HMAC-SHA256 hex digest: every file hash, key fingerprint
#: and MAC a manifest holds. Each is held to this shape before anything
#: compares it, and compared as bytes (`log.same_mac`) as well, so a value
#: that reaches a comparison some other way reads as a mismatch: the
#: manifest is whatever a writer of the bundle put there, and
#: `hmac.compare_digest` raises TypeError on a str that is not ASCII.
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

# Verification verdicts, and the exit status each maps to.
OK = "ok"
MISMATCHED = "mismatched"
TAMPERED = "tampered"
UNVERIFIABLE = "unverifiable"
VERIFY_EXIT = {OK: 0, MISMATCHED: 1, TAMPERED: 1, UNVERIFIABLE: 2}

# Problems that mean the bundle is not what its manifest says it is: a file
# changed, missing or unreadable, or a manifest that is malformed, unsigned
# by the key it names, or contradicts the snapshot. Each makes the verdict
# `mismatched`, ahead of anything the log verification says about rows in a
# file that is not the one the manifest describes.
MISMATCH_PROBLEMS = frozenset(
    {
        "manifest_missing",
        "manifest_invalid",
        "format_unsupported",
        "file_missing",
        "file_unreadable",
        "size_mismatch",
        "sha256_mismatch",
        "snapshot_unreadable",
        "claim_mismatch",
        "head_mismatch",
        "manifest_mac_mismatch",
    }
)
# Problems that leave a claim unproven rather than disproven: no key for a
# segment, a manifest with no signature to check, or one signed with a key
# only the bundle carries, which anyone able to write the bundle could have
# made. Verdict `unverifiable`.
SELF_SIGNED = "self_signed"
UNVERIFIABLE_PROBLEMS = frozenset(
    {"key_absent", "manifest_unsigned", "manifest_self_signed"}
)
# A segment under a key only the bundle carries that follows a segment
# under a key the keys directory holds: the shape the live store reads as
# `foreign_segment` (`log.verify_chain`), rows written without this host's
# key into history it signed. Verdict `tampered`, so no flag restores it.
TAMPER_PROBLEMS = frozenset({"foreign_segment"})

# Restore outcomes, and the exit status each maps to.
RESTORED = "restored"
PLANNED = "planned"
REFUSED = "refused"
FAILED = "failed"
RESTORE_EXIT = {RESTORED: 0, REFUSED: 1, FAILED: 1, PLANNED: 3}


class BackupError(RuntimeError):
    """A backup could not be written, or a restore could not go on; the
    message says why and, where something was changed, what."""


def utc_stamp(now: datetime | None = None) -> str:
    """``20261012T053900Z``: the stamp a bundle's directory and a moved-aside
    store carry, the same shape as a saved orphan's (`daemon._utc_stamp`,
    not imported, to keep the daemon's imports off the CLI's path)."""
    moment = now if now is not None else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_backups_dir(store_file: Path) -> Path:
    """``<store dir>/backups``: where bundles go when no directory is named."""
    return Path(store_file).parent / BACKUPS_DIR_NAME


def restore_record_path(keys_dir: Path, store_id: str) -> Path:
    """``<keys dir>/<store_id>.backup_restore.json``, beside the head it
    records the rewinds of, named after the store as the key ring's files
    are, since one keys directory serves every store on the host."""
    return Path(keys_dir) / f"{store_id}{RESTORE_RECORD_SUFFIX}"


def manifest_mac(key: bytes, manifest: Mapping[str, Any]) -> str:
    """The manifest's signature: HMAC-SHA256 under the store's key over the
    context string and the canonical JSON (`log.canonical_payload`) of every
    field but ``mac`` itself.

    Encoded with ``surrogatepass``, as `log.same_mac` compares: a manifest
    is whatever a writer of the bundle put there, and JSON spells a lone
    surrogate (``"\\ud800"``) that plain UTF-8 refuses to encode, so a
    field holding one reached `verify_bundle` as a UnicodeEncodeError
    instead of a MAC that does not match. The store path a manifest
    records (``source``) can hold one too: a POSIX directory name that is
    not UTF-8 decodes with ``surrogateescape``. The encoding of every
    other string is unchanged, so every bundle signed before reads the
    same MAC."""
    body = {name: value for name, value in manifest.items() if name != "mac"}
    message = _MANIFEST_MAC_CONTEXT + canonical_payload(body).encode(
        "utf-8", errors="surrogatepass"
    )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _write_private(path: Path, data: bytes) -> None:
    """Atomic and durable, owner-only from the moment it is visible."""
    atomic_write_bytes(path, data, mode_before_rename=0o600)


def _file_digest(path: Path) -> tuple[str, int]:
    """The SHA-256 and size of `path`, read in chunks: a snapshot can be
    larger than anyone wants in memory at once."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _sibling(path: Path, suffix: str) -> Path:
    """``memory.sqlite`` plus ``-wal`` or ``-shm``: SQLite's names, made by
    appending to the whole file name."""
    return path.with_name(path.name + suffix)


_SIBLING_SUFFIXES = ("", "-wal", "-shm")


def _tighten_wal_siblings(path: Path) -> None:
    """Owner-only modes on a store's ``-wal`` and ``-shm``. A read-only
    connection to a WAL store creates both when they are absent (SQLite
    needs the shared-memory index to read under WAL) at the process umask,
    where the store's own opener (`store._connect`) makes them 0600."""
    for suffix in ("-wal", "-shm"):
        sibling = _sibling(path, suffix)
        if sibling.exists():
            with contextlib.suppress(OSError):
                os.chmod(sibling, 0o600)


def _read_only_uri(path: Path) -> str:
    return sqlite_file_uri(path.resolve()) + "?mode=ro"


@contextlib.contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a store file that is not this module's to
    change: a bundle's snapshot or a temporary copy of one. Never the live
    store, which `_snapshot` reads through its own connection."""
    conn = sqlite3.connect(_read_only_uri(path), uri=True, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


def _snapshot(source: Path, target: Path) -> None:
    """Copy the live store at `source` into the new file `target` through
    SQLite's online backup API, then make `target` self-contained.

    The source connection is read-only (``mode=ro``): it takes one read
    transaction for the length of the copy (``pages=-1`` copies in one
    step, so the backup never restarts under a writer) and cannot write to
    the store whatever happens. `target` is created 0600 before SQLite
    opens it, so its journal files, which SQLite creates with the database
    file's own mode, are owner-only too."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    src = sqlite3.connect(
        _read_only_uri(source), uri=True, timeout=5.0, isolation_level=None
    )
    try:
        src.execute("SELECT count(*) FROM sqlite_master").fetchone()
        _tighten_wal_siblings(source)
        dst = sqlite3.connect(str(target), isolation_level=None)
        try:
            src.backup(dst)
            # The copy inherits the source's WAL mode in its header; DELETE
            # mode folds it into the one file and records that in the header,
            # so the bundle's file opens on its own anywhere.
            dst.execute("PRAGMA journal_mode = DELETE").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
    with target.open("r+b") as handle:
        fsync_file(handle.fileno())


@dataclass(frozen=True)
class SnapshotFacts:
    """What a snapshot says about itself, read straight from its tables."""

    store_id: str
    key_fingerprint: str
    schema_version: int
    head: Head
    key_fingerprints: tuple[str, ...]
    counts: dict[str, int] = field(default_factory=dict)


def _segment_fingerprints(conn: sqlite3.Connection, final: str) -> tuple[str, ...]:
    """Every key the log's segments name, in chain order, read from the
    rekey rows the way `log.verify_chain` reads them: the first segment's
    key is the earliest rekey row's old fingerprint, each rekey row starts a
    segment under its new one, and a log with no rekey row is all under the
    store's current key. `final` (meta's fingerprint) is always included."""
    names: list[str] = []
    for (payload,) in conn.execute(
        "SELECT payload FROM log WHERE kind = ? ORDER BY seq", (REKEY,)
    ):
        try:
            parsed = json.loads(payload)
            old, new = parsed["old_fingerprint"], parsed["new_fingerprint"]
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        if not names:
            names.append(old)
        names.append(new)
    names.append(final)
    return tuple(dict.fromkeys(name for name in names if name))


def _snapshot_facts(path: Path) -> SnapshotFacts:
    """The store id, key fingerprints, head and row counts of the store file
    at `path`, read without `Store.open`, which would change the file."""
    from .store import FOLDED_TABLES, MEMBERSHIP_TABLES

    with _read_only(path) as conn:
        meta = {
            str(key): str(value)
            for key, value in conn.execute("SELECT key, value FROM meta")
        }
        store_id = meta.get("store_id")
        final = meta.get("key_fingerprint")
        if not store_id or not final:
            raise BackupError(f"{path} carries no store id or key fingerprint")
        if not _STORE_ID_RE.fullmatch(store_id):
            raise BackupError(
                f"{path} carries a store id that is not one: {store_id!r}"
            )
        try:
            row = last_row(conn)
            schema_version = int(meta.get("schema_version", "0") or 0)
        except (ValueError, TypeError) as exc:
            # A seq or a schema version that is not a number: rows changed
            # outside the store, which `verify_bundle` reports as a snapshot
            # that does not read, not as a traceback.
            raise BackupError(f"{path} does not read as a store: {exc}") from exc
        if row is None:
            raise BackupError(f"{path} has an empty log")
        present = {
            str(name)
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        counts = {
            table: int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            for table in ("log", "meta", *FOLDED_TABLES, *MEMBERSHIP_TABLES)
            if table in present
        }
        return SnapshotFacts(
            store_id=store_id,
            key_fingerprint=final,
            schema_version=schema_version,
            head=Head(seq=row.seq, mac=row.mac, ts=row.ts),
            key_fingerprints=_segment_fingerprints(conn, final),
            counts=counts,
        )


def _head_bytes(head: Head) -> bytes:
    """A head checkpoint in the shape `KeyRing.write_head` writes."""
    return json.dumps({"seq": head.seq, "mac": head.mac, "ts": head.ts}).encode("utf-8")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def _claim_bundle_dir(parent: Path, stamp: str) -> Path:
    """A new, empty, owner-only directory under `parent` named `stamp`, or
    `stamp-2`, `stamp-3` when a bundle from the same second is there. The
    ``mkdir`` that fails on an existing name is the claim, so two runs in
    one second never share a bundle."""
    counter = 1
    while True:
        name = stamp if counter == 1 else f"{stamp}-{counter}"
        candidate = parent / name
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            counter += 1
            continue
        ensure_owner_only_dir(candidate)
        return candidate


def create_backup(
    store_file: Path,
    *,
    out_dir: Path | None = None,
    keys_dir: Path | None = None,
    include_key: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write a bundle of the store at `store_file` under `out_dir` (default
    `default_backups_dir`) and return ``{"bundle", "manifest", "warnings"}``.

    `out_dir` is created owner-only when it is missing and left as it is
    when it exists: a directory the caller names may be shared on purpose.
    The bundle directory under it is always new and 0700, and every file in
    it 0600. `keys_dir` is where the store's keys are read from (default
    `store.resolve_keys_dir`). With `include_key` the keys go into the
    bundle and a store whose current key is not held is refused; without
    it, a store whose key is not held still gets a bundle, unsigned, with a
    warning. On any failure the partial bundle is removed and
    `BackupError` raised: a bundle directory either holds a manifest or
    does not exist.

    A store file inside a backup bundle (`BETTERMEMORY_DIR` naming the
    bundle) is that bundle's snapshot, and is refused with the openers'
    refusal (`bundle_refusal`) before anything is created: the default
    directory, ``backups`` beside the store file, is inside the bundle,
    which would then hold more than its manifest lists. A bundle is kept
    by copying its directory as it is; its store is backed up from a copy
    of the file made outside it, as the refusal says. An `out_dir` that is
    itself a bundle, or a directory in one, is refused before anything is
    created for the same reason (`_bundle_at`).

    A current key file that is there and cannot be read refuses the
    backup, with or without `include_key`, naming the file and what the
    read said (`_unreadable_key_files`): the bundle would be unsigned,
    and an unsigned bundle never verifies, so no restore takes it (a
    scheduled backup would go on writing bundles that cannot be
    restored). Only a key that is not there gives an unsigned bundle,
    with a warning."""
    from . import __version__
    from . import store as _store

    store_file = Path(store_file)
    if not store_file.is_file():
        raise BackupError(f"no bettermemory 9 store at {store_file}")
    refusal = bundle_refusal(store_file)
    if refusal is not None:
        raise BackupError(refusal)
    keys_root = Path(keys_dir) if keys_dir is not None else _store.resolve_keys_dir()
    parent = Path(out_dir) if out_dir is not None else default_backups_dir(store_file)
    within = _bundle_at(parent)
    if within is not None:
        raise BackupError(
            f"{parent} is a backup bundle or is in one ({within}), and a bundle "
            "written under it would leave that bundle holding files its "
            "manifest does not list: name a directory outside every bundle "
            "with --out"
        )
    moment = now if now is not None else datetime.now(timezone.utc)
    created_at = isoformat_utc(moment.astimezone(timezone.utc))
    warnings: list[str] = []
    try:
        if not parent.exists():
            ensure_owner_only_dir(parent, parents=True)
        bundle = _claim_bundle_dir(parent, utc_stamp(moment))
    except OSError as exc:
        raise BackupError(f"cannot create a bundle under {parent}: {exc}") from exc
    try:
        snapshot = bundle / SNAPSHOT_NAME
        _snapshot(store_file, snapshot)
        facts = _snapshot_facts(snapshot)
        live = KeyRing(keys_root, facts.store_id)
        held = {fp: live.key_for_fingerprint(fp) for fp in facts.key_fingerprints}
        current_key = held.get(facts.key_fingerprint)
        unreadable = _unreadable_key_files(
            live, facts.key_fingerprint, [fp for fp, key in held.items() if key is None]
        )
        current_unreadable = unreadable.get(facts.key_fingerprint)
        if current_unreadable is not None:
            raise BackupError(
                f"{current_unreadable}: the manifest could not be signed, and a "
                "bundle whose manifest is not signed never verifies, so no "
                "restore takes it; make it readable and run the backup again"
            )
        written = [snapshot]
        if include_key:
            if current_key is None:
                raise BackupError(
                    f"--include-key: no key under {keys_root} matches the "
                    f"store's fingerprint {facts.key_fingerprint[:16]}"
                )
            ring = KeyRing(bundle, facts.store_id)
            for fp, key in held.items():
                if key is None:
                    why = unreadable.get(fp)
                    warnings.append(
                        (
                            f"the retired key {fp[:16]}: {why}"
                            if why is not None
                            else f"the retired key {fp[:16]} is not under {keys_root}"
                        )
                        + "; its segment of the log verifies only where it is held"
                    )
                    continue
                path = (
                    ring.current_path()
                    if fp == facts.key_fingerprint
                    else ring.retired_path(fp)
                )
                _write_private(path, key)
                written.append(path)
        head_path = KeyRing(bundle, facts.store_id).head_path()
        bundle_head = Head(seq=facts.head.seq, mac=facts.head.mac, ts=created_at)
        _write_private(head_path, _head_bytes(bundle_head))
        written.append(head_path)
        files: dict[str, dict[str, Any]] = {}
        for path in written:
            digest, size = _file_digest(path)
            files[path.name] = {"sha256": digest, "size": size}
        manifest: dict[str, Any] = {
            "format": BUNDLE_FORMAT,
            "format_version": BUNDLE_FORMAT_VERSION,
            "bettermemory_version": __version__,
            "created_at": created_at,
            "source": str(store_file),
            "store_id": facts.store_id,
            "schema_version": facts.schema_version,
            "key_fingerprint": facts.key_fingerprint,
            "key_fingerprints": list(facts.key_fingerprints),
            "key_included": include_key,
            "head": {"seq": facts.head.seq, "mac": facts.head.mac},
            "counts": facts.counts,
            "files": files,
        }
        # The origin anchor, as the head is recorded: what a restore puts
        # back so the restored store is checked against where this host's
        # history of it begins. An anchor past the snapshot's last row was
        # written after the snapshot began (the takeover that wrote it
        # committed meanwhile) and names a row the bundle does not hold.
        origin = live.read_origin()
        if origin is not None and origin.seq <= facts.head.seq:
            manifest["origin"] = origin.to_dict()
        elif origin is not None:
            warnings.append(
                f"the origin anchor at seq {origin.seq} is past the snapshot's "
                f"last row (seq {facts.head.seq}); the bundle records no anchor, "
                "and a restore of it leaves the next writing open to anchor one"
            )
        if current_key is not None:
            manifest["mac"] = manifest_mac(current_key, manifest)
        else:
            warnings.append(
                f"no key under {keys_root} matches the store's fingerprint "
                f"{facts.key_fingerprint[:16]}: the manifest is not signed, and "
                "`backup verify` reads the bundle as unverifiable"
            )
        text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        _write_private(bundle / MANIFEST_NAME, text.encode("utf-8"))
        fsync_dir(bundle)
        return {"bundle": str(bundle), "manifest": manifest, "warnings": warnings}
    except BaseException as exc:
        shutil.rmtree(bundle, ignore_errors=True)
        if isinstance(exc, (OSError, sqlite3.Error)):
            raise BackupError(
                f"backup of {store_file} failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class _ManifestProblem(Exception):
    def __init__(self, problem: str, detail: str) -> None:
        super().__init__(detail)
        self.problem = {"problem": problem, "detail": detail}


def _plain_name(name: object) -> bool:
    """A file name the manifest may list: one component, inside the bundle."""
    return (
        isinstance(name, str)
        and name not in ("", ".", "..", MANIFEST_NAME)
        and Path(name).name == name
        and "/" not in name
        and "\\" not in name
    )


def _hex_digest(value: object) -> bool:
    """A lowercase SHA-256 hex digest, the shape of every hash, fingerprint
    and MAC a manifest holds (`_HEX_DIGEST_RE`)."""
    return isinstance(value, str) and _HEX_DIGEST_RE.fullmatch(value) is not None


def _bundle_at(directory: Path) -> Path | None:
    """The manifest of the backup bundle `directory` is, or is directly
    in, or None: a directory `create_backup` must not write a bundle under,
    since the bundle there would then hold files its manifest does not
    list. Checked as spelled and resolved, with the manifest test the
    openers' refusal uses (`_may_be_bundle_manifest`)."""
    candidates = [directory]
    with contextlib.suppress(OSError, RuntimeError):
        candidates.append(directory.resolve())
    for candidate in candidates:
        for place in (candidate, candidate.parent):
            manifest = place / MANIFEST_NAME
            try:
                if manifest.is_file() and _may_be_bundle_manifest(manifest):
                    return manifest
            except OSError:
                continue
    return None


def _unreadable_key_files(
    ring: KeyRing, current: str, missing: Sequence[str]
) -> dict[str, str]:
    """For each fingerprint in `missing` (no key with it was read), why: the
    key file that should hold it is there and cannot be read, in the words
    the owner's open uses for the current key (`KeyRing.load_current`).
    A fingerprint whose file is simply absent, or holds another key, is
    left out. `KeyRing.key_for_fingerprint` reads every failure as no key,
    so `create_backup` reported an unreadable key file (a permission, a
    directory in its place, a volume not mounted under a link) as no key
    matching the store's fingerprint."""
    found: dict[str, str] = {}
    for fp in missing:
        if fp == current:
            try:
                ring.load_current()
            except KeyUnreadableError as exc:
                found[fp] = (
                    f"the key file {exc.path} is there and cannot be read ({exc.reason})"
                )
                continue
        path = ring.retired_path(fp)
        try:
            path.read_bytes()
        except FileNotFoundError:
            if path.is_symlink():
                found[fp] = (
                    f"the key file {path} is there and cannot be read (a "
                    "symbolic link to a file that is not there)"
                )
        except OSError as exc:
            reason = exc.strerror or str(exc) or type(exc).__name__
            found[fp] = f"the key file {path} is there and cannot be read ({reason})"
    return found


def bundle_manifest_beside(path: Path) -> Path | None:
    """The manifest of the backup bundle whose file `path` names, or None.

    A bundle's files are read, never opened in place: opening a snapshot
    as a store switches its journal mode to WAL, which rewrites its
    header, and the bundle then fails its signed hash, so `verify_bundle`
    reads it mismatched and `restore_bundle` refuses it. `path` is checked
    as spelled and resolved, so a symlink to a bundle's file, or a path
    through a symlink to the bundle directory, is caught. A hard link is
    another name for the file in a directory of its own, which no path
    check sees; `bundle_refusal` checks the link count for that.

    ``manifest.json`` is a common name, and the writing openers refuse on
    this too (`cli._common.cli_context`, the daemon, `serve`), so a store
    directory that holds another tool's file of that name must not read
    as a bundle: the file counts when it is a JSON object naming this
    format, or when it cannot be read or parsed at all (a bundle whose
    manifest broke is still a bundle, and its snapshot still must not be
    opened in place). A JSON document of another shape is not a bundle's."""
    candidates = [path]
    with contextlib.suppress(OSError, RuntimeError):
        candidates.append(path.resolve())
    for candidate in candidates:
        manifest = candidate.parent / MANIFEST_NAME
        try:
            if manifest.is_file() and _may_be_bundle_manifest(manifest):
                return manifest
        except OSError:
            continue
    return None


def _may_be_bundle_manifest(manifest: Path) -> bool:
    """Whether the file at `manifest` may be a bundle's: anything but a
    JSON document that is not an object naming `BUNDLE_FORMAT`. Read with
    the manifest reader's cap; a file past it, or one that does not read
    or parse, counts, since refusing a store is recoverable (copy it out)
    and opening a bundle's snapshot in place is not."""
    try:
        document = json.loads(bounded_read(manifest, _MANIFEST_CAP_BYTES))
    except (OSError, ValueError, RecursionError):
        return True
    return isinstance(document, dict) and document.get("format") == BUNDLE_FORMAT


def has_other_names(path: Path) -> bool:
    """Whether the file at `path` has more than one name (hard links). A
    store file has one unless it was linked; a hard link to a bundle's
    snapshot is that snapshot under a name no manifest sits beside."""
    try:
        return path.stat().st_nlink > 1
    except OSError:
        return False


def bundle_refusal(path: Path, *, named: bool = False) -> str | None:
    """Why a command must not open the store file at `path`, or None.

    The one refusal every CLI path that opens a store a user can point at
    gives before it opens it (`BETTERMEMORY_DIR` or the config naming a
    bundle directory, a ``--store``-like flag naming a bundle's file),
    each with its own exit code for a store it cannot read: opening the
    file, even read-only through `Store.open`, switches the snapshot to
    WAL and the bundle then fails its signed hash, and a writing opener
    would also add rows to it. A file with other names (hard links) is
    refused too, since it may be a bundle's snapshot under a name no
    manifest sits beside (`has_other_names`): a file the user named on
    the command line (`named`) and the store file the configuration
    resolves (`BETTERMEMORY_DIR` or ``[storage] directory`` naming a
    directory that holds such a link) alike; `named` only chooses which
    to point at the copy. Each message says to copy the file out first: a
    copy has one name and no manifest beside it, and opening it changes
    nothing in the bundle."""
    manifest = bundle_manifest_beside(path)
    if manifest is not None:
        return (
            f"{path} is in a backup bundle ({manifest} is beside it), and "
            "opening it would change it and fail the bundle's hash: copy "
            f"{path.name} out of the bundle to a directory of its own first and "
            "point the command at the copy (`bettermemory backup verify` checks "
            "a bundle where it is)"
        )
    if has_other_names(path):
        target = (
            "the command at the copy"
            if named
            else "BETTERMEMORY_DIR or `[storage] directory` at that directory"
        )
        return (
            f"{path} has other names (hard links), and opening it would change "
            "the file under each of them, a backup bundle's snapshot among "
            f"them: copy {path.name} to a directory of its own first and point "
            f"{target}"
        )
    return None


def read_manifest(bundle: Path) -> dict[str, Any]:
    """The bundle's manifest, checked for shape; `_ManifestProblem` names
    what is wrong with one that is missing or malformed."""
    path = Path(bundle) / MANIFEST_NAME
    if not path.is_file():
        raise _ManifestProblem("manifest_missing", f"no {MANIFEST_NAME} in {bundle}")
    try:
        manifest = json.loads(bounded_read(path, _MANIFEST_CAP_BYTES))
    except (OSError, ValueError) as exc:
        raise _ManifestProblem("manifest_invalid", str(exc)) from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
        raise _ManifestProblem("manifest_invalid", f"not a {BUNDLE_FORMAT} manifest")
    version = manifest.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise _ManifestProblem("manifest_invalid", "format_version is not a number")
    if version > BUNDLE_FORMAT_VERSION:
        raise _ManifestProblem(
            "format_unsupported",
            f"bundle format {version}; this build reads up to {BUNDLE_FORMAT_VERSION}",
        )
    head = manifest.get("head")
    files = manifest.get("files")
    if (
        not isinstance(manifest.get("store_id"), str)
        or not isinstance(manifest.get("key_fingerprint"), str)
        or not isinstance(head, dict)
        or not isinstance(head.get("seq"), int)
        or not isinstance(head.get("mac"), str)
        or not isinstance(files, dict)
    ):
        raise _ManifestProblem(
            "manifest_invalid", "store_id, key_fingerprint, head or files malformed"
        )
    if not _STORE_ID_RE.fullmatch(manifest["store_id"]):
        raise _ManifestProblem(
            "manifest_invalid", f"store_id {manifest['store_id']!r} is not a store id"
        )
    if (
        not _hex_digest(manifest["key_fingerprint"])
        or not _hex_digest(head["mac"])
        or ("mac" in manifest and not _hex_digest(manifest["mac"]))
    ):
        raise _ManifestProblem(
            "manifest_invalid", "key_fingerprint, head mac or mac is not a hex digest"
        )
    if "origin" in manifest and parse_origin(manifest["origin"]) is None:
        raise _ManifestProblem(
            "manifest_invalid", "origin is not a seq and a hex digest MAC"
        )
    for name, entry in files.items():
        if (
            not _plain_name(name)
            or not isinstance(entry, dict)
            or not _hex_digest(entry.get("sha256"))
            or not isinstance(entry.get("size"), int)
        ):
            raise _ManifestProblem("manifest_invalid", f"file entry {name!r}")
    required = (SNAPSHOT_NAME, KeyRing(bundle, manifest["store_id"]).head_path().name)
    for name in required:
        if name not in files:
            raise _ManifestProblem("manifest_invalid", f"{name} is not listed")
    return manifest


def _verdict(problems: Sequence[Mapping[str, Any]], log_status: str | None) -> str:
    names = {str(p["problem"]) for p in problems}
    if names & MISMATCH_PROBLEMS:
        return MISMATCHED
    if log_status == TAMPERED or names & TAMPER_PROBLEMS:
        return TAMPERED
    if names & UNVERIFIABLE_PROBLEMS or log_status == UNVERIFIABLE:
        return UNVERIFIABLE
    if log_status is None:
        return MISMATCHED
    return OK


def verify_bundle(bundle: Path, *, keys_dir: Path | None = None) -> dict[str, Any]:
    """Check the bundle at `bundle` and return the report; ``status`` is
    ``ok``, ``mismatched`` (a file or the manifest is not what was written),
    ``tampered`` (the snapshot's log or tables fail verification, or a
    segment under a key only the bundle carries follows one under a key
    `keys_dir` holds: ``foreign_segment``, as the live store reads it) or
    ``unverifiable`` (a segment's key is in neither `keys_dir`, default
    `store.resolve_keys_dir`, nor the bundle, the manifest is unsigned, or
    it is signed with a key only the bundle carries: ``manifest_mac`` then
    reads ``self_signed``). Reads the bundle and writes nothing to it: the
    log verification runs on a copy in a private temporary directory."""
    from . import store as _store
    from .store_index import drift_named, index_problems

    bundle = Path(bundle)
    keys_root = Path(keys_dir) if keys_dir is not None else _store.resolve_keys_dir()
    problems: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "status": MISMATCHED,
        "bundle": str(bundle),
        "store_id": None,
        "created_at": None,
        "bettermemory_version": None,
        "head": None,
        "origin": None,
        "key_included": None,
        "files": [],
        "manifest_mac": None,
        "keys": [],
        "log": None,
        "problems": problems,
    }
    try:
        manifest = read_manifest(bundle)
    except _ManifestProblem as exc:
        problems.append(exc.problem)
        return report
    store_id = str(manifest["store_id"])
    origin = parse_origin(manifest.get("origin"))
    report.update(
        store_id=store_id,
        created_at=manifest.get("created_at"),
        bettermemory_version=manifest.get("bettermemory_version"),
        head=dict(manifest["head"]),
        origin=None if origin is None else origin.to_dict(),
        key_included=manifest.get("key_included"),
    )
    snapshot_unreadable = False
    for name, entry in sorted(manifest["files"].items()):
        path = bundle / name
        result: dict[str, Any] = {
            "name": name,
            "status": OK,
            "sha256": entry["sha256"],
            "size": entry["size"],
        }
        if not path.is_file():
            result["status"] = "missing"
            problems.append({"problem": "file_missing", "file": name})
        else:
            try:
                digest, size = _file_digest(path)
            except OSError as exc:
                # A file this account cannot read (its mode, a lock, a
                # read error) proves nothing either way about what the
                # manifest says of it.
                result["status"] = "unreadable"
                problems.append(
                    {"problem": "file_unreadable", "file": name, "detail": str(exc)}
                )
                report["files"].append(result)
                snapshot_unreadable = snapshot_unreadable or name == SNAPSHOT_NAME
                continue
            if size != entry["size"]:
                result["status"] = "size_mismatch"
                problems.append({"problem": "size_mismatch", "file": name})
            elif not same_mac(digest, str(entry["sha256"])):
                result["status"] = "sha256_mismatch"
                problems.append({"problem": "sha256_mismatch", "file": name})
        report["files"].append(result)
    snapshot = bundle / SNAPSHOT_NAME
    if not snapshot.is_file() or snapshot_unreadable:
        return report
    bundle_ring = KeyRing(bundle, store_id)
    live_ring = KeyRing(keys_root, store_id)
    head = Head(
        seq=int(manifest["head"]["seq"]),
        mac=str(manifest["head"]["mac"]),
        ts=str(manifest.get("created_at", "")),
    )
    bundle_head = bundle_ring.read_head()
    if bundle_head is None or (bundle_head.seq, bundle_head.mac) != (
        head.seq,
        head.mac,
    ):
        problems.append(
            {
                "problem": "head_mismatch",
                "detail": f"{bundle_ring.head_path().name} does not name the "
                "manifest's head",
            }
        )
    with tempfile.TemporaryDirectory(
        prefix="bettermemory-backup-", ignore_cleanup_errors=True
    ) as scratch:
        copy = Path(scratch) / "store" / STORE_FILENAME
        ensure_owner_only_dir(copy.parent)
        shutil.copyfile(snapshot, copy)
        try:
            facts = _snapshot_facts(copy)
        except (sqlite3.Error, BackupError) as exc:
            problems.append({"problem": "snapshot_unreadable", "detail": str(exc)})
            report["status"] = _verdict(problems, None)
            return report
        for claim, found in (
            ("store_id", facts.store_id),
            ("key_fingerprint", facts.key_fingerprint),
        ):
            if manifest[claim] != found:
                problems.append(
                    {"problem": "claim_mismatch", "field": claim, "found": found}
                )
        if (facts.head.seq, facts.head.mac) != (head.seq, head.mac):
            problems.append(
                {
                    "problem": "claim_mismatch",
                    "field": "head",
                    "found": {"seq": facts.head.seq, "mac": facts.head.mac},
                }
            )
        scratch_ring = KeyRing(Path(scratch) / "keys", store_id)
        scratch_ring.ensure_dir()
        # The keys directory first: a key it holds is one this host already
        # trusts, and the report says so. A key found only in the bundle
        # comes with the bundle and is worth what the bundle is.
        keys: dict[str, tuple[bytes, str]] = {}
        wanted = dict.fromkeys(
            (*facts.key_fingerprints, str(manifest["key_fingerprint"]))
        )
        for fp in wanted:
            source: str | None = None
            key = live_ring.key_for_fingerprint(fp)
            if key is not None:
                source = "keys_dir"
            else:
                key = bundle_ring.key_for_fingerprint(fp)
                source = "bundle" if key is not None else None
            report["keys"].append({"fingerprint": fp, "source": source})
            if key is None or source is None:
                problems.append({"problem": "key_absent", "fingerprint": fp})
                continue
            keys[fp] = (key, source)
            _write_private(scratch_ring.retired_path(fp), key)
            if fp == facts.key_fingerprint:
                _write_private(scratch_ring.current_path(), key)
        # The snapshot's log is checked under every key found, so a
        # segment under a key only the bundle carries verifies whatever
        # comes before it. The live store reads a segment whose key this
        # host does not hold, after one its key signs, as rows written
        # into its history without its key (`foreign_segment`), and so
        # does this check: otherwise a writer of the bundle could rekey
        # the owner's snapshot to a key of their own, add rows, and have
        # the log read `ok` and the bundle wait only on --trust-bundle-key.
        # An --include-key bundle on a machine that holds none of its keys
        # has no held segment and is not affected.
        held_seen = False
        for fp in facts.key_fingerprints:
            found_key = keys.get(fp)
            if found_key is None:
                continue
            if found_key[1] == "keys_dir":
                held_seen = True
            elif held_seen:
                problems.append({"problem": "foreign_segment", "fingerprint": fp})
        signing = keys.get(str(manifest["key_fingerprint"]))
        if "mac" not in manifest:
            report["manifest_mac"] = "absent"
            problems.append({"problem": "manifest_unsigned"})
        elif signing is None:
            report["manifest_mac"] = UNVERIFIABLE
        elif not same_mac(manifest_mac(signing[0], manifest), str(manifest["mac"])):
            report["manifest_mac"] = "mismatch"
            problems.append({"problem": "manifest_mac_mismatch"})
        elif signing[1] == "bundle":
            # The MAC holds, under a key the bundle itself supplied: anyone
            # who can write the bundle can remake it under a key of their
            # own and sign it so. It proves the bundle is whole, not that
            # this store's key holder wrote it.
            report["manifest_mac"] = SELF_SIGNED
            problems.append(
                {
                    "problem": "manifest_self_signed",
                    "fingerprint": str(manifest["key_fingerprint"]),
                }
            )
        else:
            report["manifest_mac"] = OK
        _write_private(scratch_ring.head_path(), _head_bytes(head))
        # The anchor the signed manifest records, so `log_verify` checks the
        # snapshot against where the host's history of the store begins.
        if origin is not None:
            scratch_ring.write_origin(
                seq=origin.seq, mac=origin.mac, key_fingerprint=origin.key_fingerprint
            )
        try:
            with drift_named(f"the store in bundle {bundle}"):
                opened = _store.Store.open(
                    copy, keys_dir=scratch_ring.keys_dir, allow_rekey=False
                )
            try:
                log_report = opened.log_verify()
                problems.extend(index_problems(opened))
            finally:
                opened.close()
        except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
            # A newer schema, or a page or a row the replay cannot read (a
            # seq that is not a number): the bundle is not one this build
            # can verify, whatever its hashes say. A MAC that is not a
            # digest reads as one that does not verify (`log.same_mac`),
            # and the log check below reports it.
            problems.append({"problem": "snapshot_unreadable", "detail": str(exc)})
            report["status"] = _verdict(problems, None)
            return report
    # The report names the bundle's file, not the scratch copy it was run on.
    log_report["path"] = str(snapshot)
    report["log"] = log_report
    report["status"] = _verdict(problems, str(log_report["status"]))
    return report


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def _aside_base(store_file: Path, stamp: str) -> Path:
    """``<store>.pre-restore-<stamp>``, with a counter when any of the three
    names a store and its WAL siblings would take there is already taken."""
    counter = 1
    while True:
        suffix = stamp if counter == 1 else f"{stamp}-{counter}"
        base = store_file.with_name(f"{store_file.name}.pre-restore-{suffix}")
        if not any(_sibling(base, s).exists() for s in _SIBLING_SUFFIXES):
            return base
        counter += 1


def _store_facts(store_file: Path) -> tuple[str | None, int | None]:
    """The store id and the last seq of the log in the store file at
    `store_file`, each None when there is no store file, no such row, or
    the file does not read.

    Read in one connection through `store_schema.connect_read_only`, the
    read-only commands' open: in place with ``mode=ro`` where that creates
    nothing (a rollback-journal file without a ``-wal``, a store with its
    ``-wal`` and ``-shm`` beside it), else from a private copy of the file
    and of its ``-wal``, removed here. Rows a crash left only in the
    ``-wal`` count, no ``-wal`` or ``-shm`` is created beside the store, no
    lock is taken on it, and the file is admitted first as every opener
    admits it: one every opener refuses reads as neither. ``immutable=1``
    on the live file, which these reads used, saw none of a ``-wal``'s
    rows (the log's end stopped at the last checkpoint, and a store whose
    first rows were only in its ``-wal`` had no id) and could read a page
    a checkpoint was writing."""
    from .store import CLI_BUSY_TIMEOUT_SECONDS
    from .store_schema import connect_read_only

    if not store_file.is_file():
        return None, None
    try:
        conn, scratch = connect_read_only(store_file, timeout=CLI_BUSY_TIMEOUT_SECONDS)
    except (sqlite3.Error, ValueError, OSError):
        return None, None
    store_id: str | None = None
    end: int | None = None
    try:
        with contextlib.suppress(sqlite3.Error):
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'store_id'"
            ).fetchone()
            store_id = None if row is None else str(row[0])
        with contextlib.suppress(sqlite3.Error):
            row = conn.execute("SELECT MAX(seq) FROM log").fetchone()
            if row is not None and type(row[0]) is int:
                end = row[0]
    finally:
        conn.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return store_id, end


def _log_end(store_file: Path) -> int | None:
    """The last seq of the log in the store file at `store_file`, rows
    still only in a ``-wal`` a crash left behind included (`_store_facts`);
    None when there is no store, its log is empty or it does not read.

    Recorded beside the head checkpoint the restore replaced. A head that
    was missing (deleted in the `origin_mismatch` recovery, or a keys
    directory put back from a copy that held only the key) or behind the
    log left the range the restore rewound unknown to
    `audit_export.rewinds_across`, and an export cursor inside it was not
    warned about."""
    return _store_facts(store_file)[1]


def _current_store_id(store_file: Path) -> str | None:
    """The id of the store at `store_file`, a ``-wal`` a crash left behind
    included (`_store_facts`); None when there is no store or its meta does
    not read."""
    return _store_facts(store_file)[0]


def _replaced_log_end(
    store_file: Path,
    store_id: str,
    facts: tuple[str | None, int | None],
    bound: Path | None,
) -> dict[str, Any] | None:
    """Where the log this restore replaces ended, as ``{seq, path}`` (the
    file it was read from), or None when no copy of the store on this host
    says. `facts` is `_store_facts(store_file)`, read by the caller.

    The store at `store_file` when it holds `store_id`; else the file the
    key ring is bound to (`bound`) when that is another file that holds
    it: a restore into another store directory while the store the bundle
    was taken from is still there, whose log is the one the restore
    rewinds the head checkpoint from. The record keeps it beside the head
    the restore replaced (`audit_export.rewinds_across` reads the later of
    the two), so a head checkpoint that was missing or behind the log
    still leaves the rewound range known. A store file with another id
    is another store's log and says nothing of this one's. With neither,
    and no head checkpoint, the host held no copy of the store, and the
    record says so by naming neither end."""
    found, end = facts
    if found == store_id:
        return None if end is None else {"seq": end, "path": str(store_file)}
    if bound is None:
        return None
    with contextlib.suppress(OSError):
        if store_file.exists() and os.path.samefile(bound, store_file):
            return None
    found, end = _store_facts(bound)
    if found != store_id or end is None:
        return None
    return {"seq": end, "path": str(bound)}


def _key_actions(
    live: KeyRing, bundle: KeyRing, needed: Sequence[str], final: str
) -> list[dict[str, Any]]:
    """What the keys directory needs so the restored store opens with its
    key and every segment verifies: the final key made current (retiring a
    different current key, never deleting it) unless it already is, and any
    retired key a segment names that the directory lacks. A store opened
    under a current key that does not match its fingerprint is rekeyed by
    the owner's opener, which is the churn this avoids."""
    actions: list[dict[str, Any]] = []
    current = live.load_current()
    current_fp = None if current is None else fingerprint(current)
    if current_fp != final:
        source = "keys_dir" if live.key_for_fingerprint(final) is not None else "bundle"
        actions.append(
            {
                "fingerprint": final,
                "action": "make_current",
                "source": source,
                "retires": current_fp,
            }
        )
    for fp in needed:
        if fp == final or live.key_for_fingerprint(fp) is not None:
            continue
        if bundle.key_for_fingerprint(fp) is not None:
            actions.append(
                {
                    "fingerprint": fp,
                    "action": "add_retired",
                    "source": "bundle",
                    "retires": None,
                }
            )
    return actions


def _install_keys(
    live: KeyRing, bundle: KeyRing, actions: Sequence[Mapping[str, Any]]
) -> None:
    live.ensure_dir()
    for action in actions:
        fp = str(action["fingerprint"])
        key = live.key_for_fingerprint(fp) or bundle.key_for_fingerprint(fp)
        if key is None:
            raise BackupError(
                f"the key {fp[:16]} is in neither the bundle nor {live.keys_dir}"
            )
        if action["action"] == "make_current":
            if live.load_current() is not None:
                live.retire_current()
            _write_private(live.current_path(), key)
        else:
            _write_private(live.retired_path(fp), key)


def _rewind_head(ring: KeyRing, head: Head) -> Head | None:
    """Write `head` as the head checkpoint whatever the current one says,
    under the lock `KeyRing.write_head` takes, and return the one it
    replaced. The one place a head moves backwards, and it is recorded."""
    ring.ensure_dir()
    with flock_excl(ring.head_path()):
        before = ring.read_head()
        _write_private(ring.head_path(), _head_bytes(head))
    return before


def _origin_dict(origin: HistoryOrigin | None) -> dict[str, Any] | None:
    """An origin anchor as a report and the rewind record carry it."""
    return None if origin is None else origin.to_dict()


def _path_str(path: Path | None) -> str | None:
    """A key ring's binding as a report and the rewind record carry it."""
    return None if path is None else str(path)


def _append_restore_row(
    store: Store, entry: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Append the ``store_restored`` control row to the restored store's
    log and return it as ``{seq, mac}``: the restore record's `entry` and
    this build's version, signed under the key the restore made current.
    None, and nothing written, when this build opens the restored store
    for reading only (``PRAGMA query_only``: a store waiting on a schema
    step this build runs, `store_steps.run_steps`, or one whose versions
    keep this build to reading): such a store writes nothing until it is
    opened for writing, and the record keeps the entry.

    The record beside the head says what a restore did only on the host
    that ran it, and only while the keys directory keeps it; the row puts
    it on the log itself, chained after the bundle's head, so `log
    export` ships it to a SIEM as a ``control`` row, `memory history`
    shows it in the history of every record older than it, and `log
    verify` checks its MAC as any row's. It is written through the
    store's own transaction, so the head checkpoint moves to it as after
    any write (the record's ``head_after`` stays the bundle's head, where
    the rewound range ends). A store that cannot append (its key is not
    held) raises, and the restore reports itself failed."""
    from . import __version__

    if store.conn.execute("PRAGMA query_only").fetchone()[0]:
        return None
    payload = {**entry, "engine_version": __version__}
    with store._transaction() as tx:
        row = store._append(tx, STORE_RESTORED, payload, session=None)
    return {"seq": row.seq, "mac": row.mac}


def _append_record(path: Path, entry: Mapping[str, Any], stamp: str) -> None:
    """Add `entry` to the rewind record at `path`. A record that does not
    parse is moved aside, not overwritten, and a new one started."""
    restores: list[Any] = []
    with flock_excl(path):
        if path.exists():
            try:
                existing = json.loads(bounded_read(path, _MANIFEST_CAP_BYTES))
                if not isinstance(existing, dict) or not isinstance(
                    existing.get("restores"), list
                ):
                    raise ValueError("not a restore record")
                restores = list(existing["restores"])
            except (OSError, ValueError):
                replace_atomic(path, path.with_name(f"{path.name}.unreadable-{stamp}"))
        restores.append(dict(entry))
        document = {"format": RESTORE_RECORD_FORMAT, "restores": restores}
        _write_private(
            path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        )


def _swap_in(
    prepared: Path, store_file: Path, aside: Path, moved: list[dict[str, str]]
) -> None:
    """Move the store file and its WAL siblings to `aside`, then rename
    `prepared` into the store's place. A failure part way renames back what
    was moved and raises, leaving the store as it was. `moved` holds every
    move that stands, the caller's to report whether this returns or raises.

    The way back takes the store whole or not at all. When a name this
    vacated is taken again (a process opened the store meanwhile), every
    move stands: a ``-wal`` put back beside a file it was never part of
    would be replayed into it, and the store aside would lose the rows only
    its WAL holds. Otherwise the store file goes back first, and a move
    back that fails leaves the rest aside, as `moved` says."""
    try:
        for suffix in _SIBLING_SUFFIXES:
            source = _sibling(store_file, suffix)
            if source.exists():
                target = _sibling(aside, suffix)
                replace_atomic(source, target)
                moved.append({"from": str(source), "to": str(target)})
        taken = [
            str(_sibling(store_file, s))
            for s in _SIBLING_SUFFIXES
            if _sibling(store_file, s).exists()
        ]
        if taken:
            raise BackupError(
                "another process opened the store while it was being moved "
                f"aside ({', '.join(taken)} reappeared); close every session "
                "using bettermemory and run the restore again"
            )
        replace_atomic(prepared, store_file)
    except BaseException:
        if not any(Path(step["from"]).exists() for step in moved):
            for step in list(moved):
                try:
                    replace_atomic(Path(step["to"]), Path(step["from"]))
                except OSError:
                    break
                moved.remove(step)
        raise
    fsync_dir(store_file.parent)


def _self_signed_fingerprint(verification: Mapping[str, Any]) -> str | None:
    """The key a ``self_signed`` manifest is signed with, or None."""
    for problem in verification["problems"]:
        if problem.get("problem") == "manifest_self_signed":
            return str(problem.get("fingerprint", ""))
    return None


def _only_self_signed(verification: Mapping[str, Any]) -> bool:
    """True when the bundle verifies in every respect but one: its
    manifest is signed with a key only the bundle carries. Every file
    matches the manifest and the log reads ``ok``; what is missing is any
    sign that the store's key holder, not whoever could write the bundle,
    made it."""
    names = {str(problem.get("problem")) for problem in verification["problems"]}
    log = verification["log"]
    return names == {"manifest_self_signed"} and log is not None and log["status"] == OK


def _self_signed_refusal(verification: Mapping[str, Any], keys_root: Path) -> str:
    """Why a ``self_signed`` bundle is refused, and how to restore it."""
    fp = _self_signed_fingerprint(verification) or ""
    try:
        current = KeyRing(keys_root, str(verification["store_id"])).load_current()
    except OSError:
        holds = "whose current key for this store cannot be read"
    else:
        holds = (
            f"which holds {fingerprint(current)[:16]} as this store's current key"
            if current is not None
            else "which holds no current key for this store"
        )
    return (
        f"the bundle is signed only by a key the bundle carries ({fp[:16]}), "
        f"not by one under {keys_root}, {holds}. The signature shows the "
        "bundle is whole, not who made it, and the restore would make that "
        "key the store's current key. If the bundle comes from a place only "
        "you can write, run the restore again with --trust-bundle-key"
    )


@contextlib.contextmanager
def _daemon_locks(state_dir: Path, store_file: Path) -> Iterator[str | None]:
    """Hold the store's spawn lock and its daemon lock for the block, or
    yield the reason they could not be taken. The daemon lock is what a
    serving daemon holds for its whole life, so taking it proves none
    serves and keeps one from starting to; the spawn lock keeps a hook or
    the shim's first call from spawning one meanwhile, which would open the
    store before it found the lock taken."""
    from . import _daemon_client as client

    held: list[Any] = []
    reason: str | None = None
    try:
        ensure_owner_only_dir(state_dir, parents=True)
        for lock_path, what in (
            (client.spawn_lock_file_for(state_dir, store_file), "is starting"),
            (client.lock_file_for(state_dir, store_file), "serves"),
        ):
            handle = client.try_lock(lock_path)
            if handle is None:
                reason = (
                    f"a bettermemory daemon {what} {store_file} (it holds "
                    f"{lock_path}); close every session using bettermemory "
                    "(Claude Code, Codex, ...), run `bettermemory down`, then "
                    "run the restore again"
                )
                break
            held.append(handle)
    except OSError as exc:
        reason = f"the daemon locks under {state_dir} cannot be taken: {exc}"
    try:
        yield reason
    finally:
        for handle in reversed(held):
            client.release_lock(handle)


def _take_restore_lock(
    store_file: Path, bundle: Path, report: dict[str, Any]
) -> restore_lock.RestoreLock | None:
    """The store's restore lock, held, with no process holding the store
    open (`restore_lock.probe`), or None with ``detail`` saying why: the
    lock is held (another restore, or an opener that kept it), it cannot
    be made, or a process has the store open. Every writing open is
    refused from here until the lock is released (`restore_lock`)."""
    lock = restore_lock.lock_path(store_file)
    try:
        ensure_owner_only_dir(store_file.parent, parents=True)
        held = restore_lock.acquire(store_file)
    except OSError as exc:
        report["detail"] = f"the restore lock {lock} cannot be taken: {exc}"
        return None
    if held is None:
        report["detail"] = (
            f"another restore of {store_file} is running, or a process is "
            f"opening the store (it holds {lock}); run the restore again when "
            "it has finished"
        )
        return None
    report["lock"] = {"path": str(lock), "unfinished": held.previous, "kept": False}
    try:
        held.mark(
            bundle=str(bundle), stage="checking that no process has the store open"
        )
        reason = restore_lock.probe(store_file)
    except BaseException:
        held.release(clear=held.previous is None)
        raise
    if reason is not None:
        # A marker taken over stays: the store is still part restored.
        held.release(clear=held.previous is None)
        report["detail"] = reason
        return None
    return held


def _release_restore_lock(
    held: restore_lock.RestoreLock, report: dict[str, Any], swapped: list[bool]
) -> None:
    """Give the restore lock back. Its marker is removed when the restore
    finished, or failed before the store file was replaced on a store no
    unfinished restore had left part restored; otherwise it stays, and
    every writing open refuses the part-restored store until a restore
    finishes or `backup unlock` removes it, which ``detail`` says."""
    keep = report["status"] != RESTORED and (swapped[0] or held.previous is not None)
    held.release(clear=not keep)
    report["lock"]["kept"] = keep
    if keep:
        report["detail"] = (
            f"{report['detail'] or 'the restore did not finish'}; writing opens "
            f"of {held.store_file} are refused "
            f"until it is finished: run the restore again, or keep the store as "
            "it stands with `bettermemory backup unlock --yes`"
        )


def restore_bundle(
    bundle: Path,
    store_file: Path,
    *,
    apply: bool,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
    trust_bundle_key: bool = False,
) -> dict[str, Any]:
    """Restore the bundle at `bundle` over the store at `store_file` and
    return the report; ``status`` is ``restored``, ``planned`` (`apply`
    false: the plan, nothing changed), ``refused`` (a daemon holds the
    store, a process has it open, another restore holds its lock, or the
    bundle does not verify) or ``failed`` (a step raised, or the restored
    store does not verify; ``detail`` and ``moved_aside`` say what is
    where, and ``lock`` whether writing opens stay refused). The plan's
    ``unfinished_restore`` is the marker a restore that did not finish
    left on the store's lock, which this one takes over; ``lock`` is
    ``{path, unfinished, kept}`` once the lock was taken. `keys_dir` and
    `state_dir` default to the ones the
    store and its daemon resolve. See the module docstring for the order
    and why.

    A bundle whose only shortfall is a manifest signed with a key it
    carries itself (`verify_bundle`'s ``self_signed``) is refused unless
    `trust_bundle_key`: restoring it makes that key the store's current
    key, retiring the one the keys directory holds, and installs its
    origin anchor, so the bundle's writer would hold the store's key from
    then on. The record names the key so trusted."""
    from . import _daemon_client as client
    from . import store as _store

    bundle = Path(bundle)
    store_file = Path(store_file)
    keys_root = Path(keys_dir) if keys_dir is not None else _store.resolve_keys_dir()
    state_root = (
        Path(state_dir) if state_dir is not None else client.resolve_state_dir()
    )
    moment = now if now is not None else datetime.now(timezone.utc)
    stamp = utc_stamp(moment)
    report: dict[str, Any] = {
        "status": REFUSED,
        "detail": None,
        "bundle": str(bundle),
        "store": str(store_file),
        "verify": None,
        "plan": None,
        "moved_aside": [],
        "keys_installed": [],
        "head": None,
        "origin": None,
        "binding": None,
        "record": None,
        "row": None,
        "log": None,
        "lock": None,
    }
    # A store resolved inside a bundle (`BETTERMEMORY_DIR` naming one) is
    # that bundle's snapshot: the restore would move it aside and copy
    # another bundle's database over it, and the bundle would be gone.
    within = bundle_manifest_beside(store_file)
    if within is not None:
        report["detail"] = (
            f"the store {store_file} is in a backup bundle ({within} is beside "
            "it), and a restore would replace that bundle's snapshot: restore "
            "into a store directory outside every bundle"
        )
        return report
    with (
        _daemon_locks(state_root, store_file) as reason,
        contextlib.ExitStack() as stack,
    ):
        if reason is not None:
            report["detail"] = reason
            return report
        verification = verify_bundle(bundle, keys_dir=keys_root)
        report["verify"] = verification
        self_signed = _only_self_signed(verification)
        if verification["status"] != OK and not (self_signed and trust_bundle_key):
            report["detail"] = (
                _self_signed_refusal(verification, keys_root)
                if self_signed
                else (
                    f"the bundle does not verify ({verification['status']}); run "
                    f"`bettermemory backup verify {bundle}` for the report"
                )
            )
            return report
        trusted_key = _self_signed_fingerprint(verification) if self_signed else None
        store_id = str(verification["store_id"])
        live = KeyRing(keys_root, store_id)
        bundle_ring = KeyRing(bundle, store_id)
        head = Head(
            seq=int(verification["head"]["seq"]),
            mac=str(verification["head"]["mac"]),
            ts=isoformat_utc(moment.astimezone(timezone.utc)),
        )
        needed = [str(k["fingerprint"]) for k in verification["keys"]]
        final = str(verification["log"]["key_fingerprint"])
        try:
            actions = _key_actions(live, bundle_ring, needed, final)
        except KeyUnreadableError as exc:
            # Read as missing, the store's current key would be written over
            # by the bundle's. Refused before anything changes.
            report["detail"] = f"{exc}; make it readable and run the restore again"
            return report
        aside = _aside_base(store_file, stamp)
        before = live.read_head()
        facts = _store_facts(store_file)
        current_store_id = facts[0]
        origin = parse_origin(verification["origin"])
        origin_before = live.read_origin()
        try:
            bound = live.read_binding()
        except OSError:
            bound = None
        binding_before = _path_str(bound)
        binding_after = str(store_file.resolve())
        log_end = _replaced_log_end(store_file, store_id, facts, bound)
        record = restore_record_path(keys_root, store_id)
        report["plan"] = {
            "current_store_id": current_store_id,
            "bundle_store_id": store_id,
            "move_aside": [
                {"from": str(_sibling(store_file, s)), "to": str(_sibling(aside, s))}
                for s in _SIBLING_SUFFIXES
                if _sibling(store_file, s).exists()
            ],
            "keys": actions,
            "head": {
                "before": None
                if before is None
                else {"seq": before.seq, "mac": before.mac},
                "after": {"seq": head.seq, "mac": head.mac},
            },
            "log_end_before": log_end,
            "origin": {
                "before": _origin_dict(origin_before),
                "after": _origin_dict(origin),
            },
            "binding": {"before": binding_before, "after": binding_after},
            "record": str(record),
            "trusted_bundle_key": trusted_key,
            "unfinished_restore": restore_lock.read_marker(store_file),
        }
        if not apply:
            report["status"] = PLANNED
            return report
        held = _take_restore_lock(store_file, bundle, report)
        if held is None:
            return report
        swapped = [False]
        stack.callback(_release_restore_lock, held, report, swapped)
        # Read again now that no process has the store open: a writer that
        # closed after the plan was read may have moved the log on.
        log_end = _replaced_log_end(
            store_file, store_id, _store_facts(store_file), bound
        )
        report["status"] = FAILED
        moved: list[dict[str, str]] = report["moved_aside"]
        try:
            ensure_owner_only_dir(store_file.parent, parents=True)
            handle, temp_name = tempfile.mkstemp(
                prefix=f".{store_file.name}.restore-",
                suffix=".tmp",
                dir=store_file.parent,
            )
            os.close(handle)
            prepared = Path(temp_name)
            try:
                shutil.copyfile(bundle / SNAPSHOT_NAME, prepared)
                os.chmod(prepared, 0o600)
                with prepared.open("r+b") as fh:
                    fsync_file(fh.fileno())
                # The bundle is read twice, once to verify and once here: the
                # copy put in place must be the file the signed manifest
                # hashed, not one swapped into the bundle in between.
                expected = next(
                    f for f in verification["files"] if f["name"] == SNAPSHOT_NAME
                )
                if _file_digest(prepared) != (expected["sha256"], expected["size"]):
                    raise BackupError(
                        f"{bundle / SNAPSHOT_NAME} changed after it was verified"
                    )
                held.mark(stage="moving the store aside")
                try:
                    _swap_in(prepared, store_file, aside, moved)
                finally:
                    swapped[0] = bool(moved) or not prepared.exists()
            finally:
                with contextlib.suppress(OSError):
                    if prepared.exists():
                        prepared.unlink()
            held.mark(stage="installing the keys and rewinding the head")
            _install_keys(live, bundle_ring, actions)
            report["keys_installed"] = list(actions)
            replaced = _rewind_head(live, head)
            report["head"] = {
                "before": None
                if replaced is None
                else {"seq": replaced.seq, "mac": replaced.mac},
                "after": {"seq": head.seq, "mac": head.mac},
            }
            report["origin"] = {
                "before": _origin_dict(live.replace_origin(origin)),
                "after": _origin_dict(origin),
            }
            # The ring's keys, head and anchor are the restored file's now;
            # a binding left on another file that holds the store id would
            # open this one for reading only (`store_keys._claim_key_ring`).
            report["binding"] = {
                "before": _path_str(live.rebind_store(store_file)),
                "after": binding_after,
            }
            entry: dict[str, Any] = {
                "restored_at": head.ts,
                "bundle": str(bundle),
                "bundle_created_at": verification["created_at"],
                "store": str(store_file),
                "store_id": store_id,
                "replaced_store_id": report["plan"]["current_store_id"],
                "head_before": report["head"]["before"],
                "head_after": report["head"]["after"],
                "log_end_before": log_end,
                "origin_before": report["origin"]["before"],
                "origin_after": report["origin"]["after"],
                "binding_before": report["binding"]["before"],
                "binding_after": report["binding"]["after"],
                "moved_aside": report["moved_aside"],
                "keys_installed": [a["fingerprint"] for a in actions],
                "trusted_bundle_key": trusted_key,
            }
            _append_record(record, entry, stamp)
            report["record"] = str(record)
            held.mark(stage="verifying the restored store")
            restored = _store.Store.open(
                store_file, keys_dir=keys_root, allow_rekey=False
            )
            try:
                report["row"] = _append_restore_row(restored, entry)
                report["log"] = restored.log_verify()
            finally:
                restored.close()
        except (OSError, sqlite3.Error, BackupError, ValueError, RuntimeError) as exc:
            report["detail"] = f"{exc.__class__.__name__}: {exc}"
            return report
        if report["log"]["status"] == OK:
            report["status"] = RESTORED
        else:
            report["detail"] = (
                f"the restored store reads {report['log']['status']} under `log verify`"
            )
        return report


__all__ = [
    "BACKUPS_DIR_NAME",
    "BUNDLE_FORMAT",
    "BUNDLE_FORMAT_VERSION",
    "FAILED",
    "MANIFEST_NAME",
    "MISMATCHED",
    "OK",
    "PLANNED",
    "REFUSED",
    "RESTORED",
    "RESTORE_EXIT",
    "SNAPSHOT_NAME",
    "TAMPERED",
    "UNVERIFIABLE",
    "VERIFY_EXIT",
    "BackupError",
    "SnapshotFacts",
    "bundle_manifest_beside",
    "bundle_refusal",
    "create_backup",
    "default_backups_dir",
    "has_other_names",
    "manifest_mac",
    "read_manifest",
    "restore_bundle",
    "restore_record_path",
    "utc_stamp",
    "verify_bundle",
]
