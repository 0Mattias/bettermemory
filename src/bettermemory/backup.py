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
  names that this host holds. A bundle that carries its key verifies and
  restores anywhere, and anyone holding it can sign rows as the store, so
  the default leaves the keys out and the manifest names the fingerprints
  they must match.
* ``manifest.json``, written last, so a bundle without one is a bundle
  whose creation did not finish. It records the format, the bettermemory
  version, the store id, the key fingerprints, the head (seq and MAC), row
  counts per table, the size and SHA-256 of every other file, and the time.
  It is signed: ``mac`` is an HMAC under the store's current key, which no
  file in the bundle replaces without the key. In the live store the head
  checkpoint sits in the keys directory, apart from the store file, which
  is what lets it catch a deleted tail: an editor of the store file cannot
  move it. In a bundle the head sits beside the snapshot, and the signature
  restores the separation: an editor who truncates the snapshot's log and
  rewrites the head and the hashes to match cannot re-sign the manifest. A
  bundle written where the store's key is not held carries no signature and
  verifies as ``unverifiable`` at best.

Verification never opens a bundle's file in place. `Store.open` runs the
additive schema step and sets WAL mode on the file it opens, so verifying a
backup where it lies would change it. `verify_bundle` checks every file
against the manifest, checks the signature, then copies the snapshot into a
private temporary directory beside a temporary key ring (the keys from the
bundle first, then from the keys directory, and the head from the signed
manifest) and runs the store's own `log_verify` there: MACs, chain,
segments, head and the fold, with no change to `bettermemory.log`.

Restore. Putting an older copy back by hand leaves `log verify` reading
``tampered`` for good: `KeyRing.write_head` only moves forward, so the head
names a row the restored log does not hold (``tail_deleted``) and, once new
rows pass it, a row whose MAC differs (``mismatch``). `restore_bundle`
rewinds the head to the bundle's on the record instead. In order: it takes
the store's daemon lock and the spawn lock (`_daemon_client.lock_file_for`
and `spawn_lock_file_for`), refusing while anything holds either, so no
daemon serves mid-restore and no hook, `up` or newly started shim spawns
one (each spawns through `ensure_daemon`, under the spawn lock); it
verifies the bundle and refuses
one that does not verify; without ``apply`` it stops there with the plan.
Then it copies the snapshot into a temporary file beside the store, moves
the store file and its ``-wal`` and ``-shm`` aside as
``<store>.pre-restore-<UTC stamp>`` (with ``-wal`` and ``-shm`` suffixes,
so SQLite pairs them again on open; nothing is deleted), renames the copy
into place, installs any key the restored log needs that the keys directory
lacks, rewinds the head under the head's own lock, records the rewind in
``<store_id>.backup_restore.json`` beside the head, and runs `log_verify`
on the restored store, which must read ``ok``.

The order is chosen for what a failure leaves. The file swap is the step
most likely to fail (Windows refuses to rename a file another process has
open), so it runs before keys or head change, and a failed swap renames
back what it moved. A WAL left beside a copied-in store is replayed over
it, which is why the orphan notice's manual steps delete ``-wal`` and
``-shm`` first; here they are moved aside with their store and the swap
refuses to put a file in place while any of the three names is taken.

Not defended: a process that opens the store without either lock while the
restore runs. The in-process server (`bettermemory serve`, and the shim's
fallback when no daemon can be reached), the CLI's writing commands and a
running shim's reconnect (which spawns a daemon directly; that daemon opens
the store before it finds the lock taken, and exits) all can. The refusal
text says to close every session and run `bettermemory down` first, as the
orphan notice does, and the swap refuses to land on a path something
reopened meanwhile.
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
from typing import Any

from ._fsutil import (
    atomic_write_bytes,
    bounded_read,
    ensure_owner_only_dir,
    flock_excl,
    fsync_dir,
    fsync_file,
    replace_atomic,
)
from .config import STORE_FILENAME
from .log import REKEY, Head, KeyRing, canonical_payload, fingerprint, last_row
from .time_utils import isoformat_utc

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
#: A store id: `Store.create` makes 32 hex digits, and this takes any name
#: of letters, digits, ``_`` and ``-``. The id names the files of the key
#: ring (`log.KeyRing`), in the bundle and in the keys directory, so an id
#: carrying a separator or ``..`` would steer create's key and head writes
#: out of the bundle and restore's out of the keys directory. A snapshot
#: or a manifest that names any other id is not a bundle this reads.
_STORE_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,128}")
#: A SHA-256 or HMAC-SHA256 hex digest: every file hash, key fingerprint
#: and MAC a manifest holds. Each is held to this shape before anything
#: compares it, since `hmac.compare_digest` raises on a non-ASCII string.
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
        "size_mismatch",
        "sha256_mismatch",
        "snapshot_unreadable",
        "claim_mismatch",
        "head_mismatch",
        "manifest_mac_mismatch",
    }
)
# Problems that leave a claim unproven rather than disproven: no key for a
# segment, or a manifest with no signature to check. Verdict `unverifiable`.
UNVERIFIABLE_PROBLEMS = frozenset({"key_absent", "manifest_unsigned"})

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
    field but ``mac`` itself."""
    body = {name: value for name, value in manifest.items() if name != "mac"}
    message = _MANIFEST_MAC_CONTEXT + canonical_payload(body).encode("utf-8")
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


def _read_only_uri(path: Path, *, immutable: bool = False) -> str:
    uri = path.resolve().as_uri() + "?mode=ro"
    return uri + "&immutable=1" if immutable else uri


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
    does not exist."""
    from . import __version__
    from . import store as _store

    store_file = Path(store_file)
    if not store_file.is_file():
        raise BackupError(f"no bettermemory 9 store at {store_file}")
    keys_root = Path(keys_dir) if keys_dir is not None else _store.resolve_keys_dir()
    parent = Path(out_dir) if out_dir is not None else default_backups_dir(store_file)
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
                    warnings.append(
                        f"the retired key {fp[:16]} is not under {keys_root}; "
                        "its segment of the log verifies only where it is held"
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
    if log_status == TAMPERED:
        return TAMPERED
    if names & UNVERIFIABLE_PROBLEMS or log_status == UNVERIFIABLE:
        return UNVERIFIABLE
    if log_status is None:
        return MISMATCHED
    return OK


def verify_bundle(bundle: Path, *, keys_dir: Path | None = None) -> dict[str, Any]:
    """Check the bundle at `bundle` and return the report; ``status`` is
    ``ok``, ``mismatched`` (a file or the manifest is not what was written),
    ``tampered`` (the snapshot's log or tables fail verification) or
    ``unverifiable`` (a segment's key is in neither the bundle nor
    `keys_dir`, default `store.resolve_keys_dir`, or the manifest is
    unsigned). Reads the bundle and writes nothing to it: the log
    verification runs on a copy in a private temporary directory."""
    from . import store as _store

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
    report.update(
        store_id=store_id,
        created_at=manifest.get("created_at"),
        bettermemory_version=manifest.get("bettermemory_version"),
        head=dict(manifest["head"]),
        key_included=manifest.get("key_included"),
    )
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
            digest, size = _file_digest(path)
            if size != entry["size"]:
                result["status"] = "size_mismatch"
                problems.append({"problem": "size_mismatch", "file": name})
            elif not hmac.compare_digest(digest, entry["sha256"]):
                result["status"] = "sha256_mismatch"
                problems.append({"problem": "sha256_mismatch", "file": name})
        report["files"].append(result)
    snapshot = bundle / SNAPSHOT_NAME
    if not snapshot.is_file():
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
        keys: dict[str, bytes] = {}
        wanted = dict.fromkeys(
            (*facts.key_fingerprints, str(manifest["key_fingerprint"]))
        )
        for fp in wanted:
            source: str | None = None
            key = bundle_ring.key_for_fingerprint(fp)
            if key is not None:
                source = "bundle"
            else:
                key = live_ring.key_for_fingerprint(fp)
                source = "keys_dir" if key is not None else None
            report["keys"].append({"fingerprint": fp, "source": source})
            if key is None:
                problems.append({"problem": "key_absent", "fingerprint": fp})
                continue
            keys[fp] = key
            _write_private(scratch_ring.retired_path(fp), key)
            if fp == facts.key_fingerprint:
                _write_private(scratch_ring.current_path(), key)
        signing_key = keys.get(str(manifest["key_fingerprint"]))
        if "mac" not in manifest:
            report["manifest_mac"] = "absent"
            problems.append({"problem": "manifest_unsigned"})
        elif signing_key is None:
            report["manifest_mac"] = UNVERIFIABLE
        elif hmac.compare_digest(
            manifest_mac(signing_key, manifest), str(manifest["mac"])
        ):
            report["manifest_mac"] = OK
        else:
            report["manifest_mac"] = "mismatch"
            problems.append({"problem": "manifest_mac_mismatch"})
        _write_private(scratch_ring.head_path(), _head_bytes(head))
        try:
            opened = _store.Store.open(
                copy, keys_dir=scratch_ring.keys_dir, allow_rekey=False
            )
            try:
                log_report = opened.log_verify()
            finally:
                opened.close()
        except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
            # A newer schema, a page the replay cannot read, or a row whose
            # MAC is not a digest (`hmac.compare_digest` raises TypeError on
            # a non-ASCII one): the bundle is not one this build can
            # verify, whatever its hashes say.
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


def _current_store_id(store_file: Path) -> str | None:
    """The id of the store at `store_file`, read with ``immutable=1`` so the
    read creates no ``-wal`` or ``-shm`` and takes no lock; None when there
    is no store or its meta cannot be read that way (a store whose first
    rows are still only in its WAL). Shown in the plan, never acted on."""
    if not store_file.is_file():
        return None
    try:
        conn = sqlite3.connect(
            _read_only_uri(store_file, immutable=True), uri=True, isolation_level=None
        )
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return None if row is None else str(row[0])


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
    move that stands, the caller's to report whether this returns or raises:
    a move whose way back is taken (a process opened the store meanwhile)
    stays in it."""
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
        for step in reversed(list(moved)):
            with contextlib.suppress(OSError):
                if not Path(step["from"]).exists():
                    replace_atomic(Path(step["to"]), Path(step["from"]))
                    moved.remove(step)
        raise
    fsync_dir(store_file.parent)


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


def restore_bundle(
    bundle: Path,
    store_file: Path,
    *,
    apply: bool,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restore the bundle at `bundle` over the store at `store_file` and
    return the report; ``status`` is ``restored``, ``planned`` (`apply`
    false: the plan, nothing changed), ``refused`` (a daemon holds the
    store, or the bundle does not verify) or ``failed`` (a step raised, or
    the restored store does not verify; ``detail`` and ``moved_aside`` say
    what is where). `keys_dir` and `state_dir` default to the ones the
    store and its daemon resolve. See the module docstring for the order
    and why."""
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
        "record": None,
        "log": None,
    }
    with _daemon_locks(state_root, store_file) as reason:
        if reason is not None:
            report["detail"] = reason
            return report
        verification = verify_bundle(bundle, keys_dir=keys_root)
        report["verify"] = verification
        if verification["status"] != OK:
            report["detail"] = (
                f"the bundle does not verify ({verification['status']}); run "
                f"`bettermemory backup verify {bundle}` for the report"
            )
            return report
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
        actions = _key_actions(live, bundle_ring, needed, final)
        aside = _aside_base(store_file, stamp)
        before = live.read_head()
        record = restore_record_path(keys_root, store_id)
        report["plan"] = {
            "current_store_id": _current_store_id(store_file),
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
            "record": str(record),
        }
        if not apply:
            report["status"] = PLANNED
            return report
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
                _swap_in(prepared, store_file, aside, moved)
            finally:
                with contextlib.suppress(OSError):
                    if prepared.exists():
                        prepared.unlink()
            _install_keys(live, bundle_ring, actions)
            report["keys_installed"] = list(actions)
            replaced = _rewind_head(live, head)
            report["head"] = {
                "before": None
                if replaced is None
                else {"seq": replaced.seq, "mac": replaced.mac},
                "after": {"seq": head.seq, "mac": head.mac},
            }
            _append_record(
                record,
                {
                    "restored_at": head.ts,
                    "bundle": str(bundle),
                    "bundle_created_at": verification["created_at"],
                    "store": str(store_file),
                    "store_id": store_id,
                    "replaced_store_id": report["plan"]["current_store_id"],
                    "head_before": report["head"]["before"],
                    "head_after": report["head"]["after"],
                    "moved_aside": report["moved_aside"],
                    "keys_installed": [a["fingerprint"] for a in actions],
                },
                stamp,
            )
            report["record"] = str(record)
            restored = _store.Store.open(
                store_file, keys_dir=keys_root, allow_rekey=False
            )
            try:
                report["log"] = restored.log_verify()
            finally:
                restored.close()
        except (OSError, sqlite3.Error, BackupError, ValueError) as exc:
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
    "create_backup",
    "default_backups_dir",
    "manifest_mac",
    "read_manifest",
    "restore_bundle",
    "restore_record_path",
    "utc_stamp",
    "verify_bundle",
]
