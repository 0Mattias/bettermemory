"""The key ring's lifecycle (ENG-106): what `bettermemory key show`,
`key export`, `key import`, `key adopt`, `key rotate` and `log
accept-history` do, so that no one recovers a store by placing or deleting
``<store_id>.<prefix>.key``, ``.origin``, ``.head`` or ``.path`` files by
hand.

Every command names the store the config resolves and the keys directory
the store reads (`store.resolve_keys_dir`). Each one that changes the key
ring writes a row of its own kind into the store's log, signed with the
store's key, naming what the ring held before and after: ``key_imported``
(`import_keys`), ``key_adopted`` (`adopt_ring`), ``history_accepted``
(`accept_history`) and ``rekey`` (`rotate_key`, the kind every key change
of the chain is, since the segments split at it). `show` and `export`
change nothing and write no row.

The refusals, by command (each returns ``refused`` with the reason in
``detail``; nothing is changed):

- the wrong store: `import` of a key export, or of a key file named
  ``<store_id>.key``, whose store id is not this store's;
- a key that is not this store's, or a file whose log does not carry the
  ring's history: `import` refuses a key that signs no segment of this
  store's log; `adopt` refuses a ring whose key is not the one the store
  names, and a file that does not fit the ring's origin anchor and head
  (`store_keys.ring_fit`, the history `log accept-history` accepts on
  purpose); `rotate` refuses a store whose key is not present;
- a ring bound elsewhere: `export`, `import` and `rotate` refuse a store
  file whose key ring belongs to another file still holding the store id
  (`store_keys.KEY_BOUND_ELSEWHERE`), and name `key adopt`, which is the
  one command that binds such a ring to this file;
- a file this user cannot read: the key file, a retired key, the binding
  or the file `import` reads, each named with what reading it said;
- a running daemon: `adopt` and `accept_history` take the daemon lock and
  the spawn lock of the store and of the file the ring was bound to, as
  `backup restore` takes its store's, and refuse while either is held.

`adopt`, `rotate` and `accept_history` without ``apply`` return the plan
(``planned``) and change nothing.

`survey_rings` and `clear_orphan_rings` are `bettermemory doctor`'s
``orphan_keys`` check and its ``--clear-orphan-keys``: the key rings whose
store is gone, and their deletion, which keeps every ring whose store
cannot be told gone.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._fsutil import (
    atomic_write_bytes,
    bounded_read,
    ensure_owner_only_dir,
    flock_excl,
    fsync_file,
)
from .log import (
    HISTORY_ACCEPTED,
    KEY_ADOPTED,
    KEY_BYTES,
    KEY_IMPORTED,
    ORIGIN_UNVERIFIABLE,
    REKEY,
    STORE_ID_PATTERN,
    Head,
    HistoryOrigin,
    KeyRing,
    KeyUnreadableError,
    LogRow,
    chain_segments,
    fingerprint,
    row_verifies,
)
from .store_keys import (
    HEAD_MISMATCH,
    HEAD_PAST,
    KEY_BOUND_ELSEWHERE,
    KEY_FOREIGN,
    KEY_MISSING,
    KEY_PRESENT,
    KEY_UNREADABLE,
    KeyState,
    _signs_a_segment,
    _store_id_at,
    ring_fit,
)
from .time_utils import isoformat_utc

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.store")

#: A command's outcomes, and the exit status each maps to: done (or
#: nothing to do) 0, refused or failed 1, a plan without ``--yes`` 3, the
#: code `backup restore` gives its plan.
DONE = "done"
UNCHANGED = "unchanged"
PLANNED = "planned"
REFUSED = "refused"
FAILED = "failed"
EXIT_CODES = {DONE: 0, UNCHANGED: 0, REFUSED: 1, FAILED: 1, PLANNED: 3}

#: What `export_keys` writes: one JSON object, owner-only, naming the store
#: id and every key the ring holds for it, hex-encoded. `import_keys`
#: reads this, or a key file as a keys directory holds it (32 raw bytes).
EXPORT_FORMAT = "bettermemory.key_export"
EXPORT_FORMAT_VERSION = 1
#: An export of a ring with many retired keys is a few kilobytes.
_IMPORT_CAP_BYTES = 1 << 20
#: A key file's name in a keys directory: the current key, or a retired one
#: under the first 16 hex characters of its fingerprint.
_KEY_FILE_NAME = re.compile(r"(?P<id>[0-9a-f]{32})(?:\.[0-9a-f]{16})?\.key")
_HEX_KEY = re.compile(r"[0-9a-f]{64}")


def keys_root(keys_dir: Path | str | None = None) -> Path:
    """``keys_dir``, or the keys directory the store reads
    (`store.resolve_keys_dir`: ``BETTERMEMORY_KEYS_DIR`` when set)."""
    if keys_dir is not None:
        return Path(keys_dir)
    from . import store as _store

    return _store.resolve_keys_dir()


def _state_root(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    from . import _daemon_client as client

    return client.resolve_state_dir()


def _report(command: str, store_file: Path, **fields: Any) -> dict[str, Any]:
    return {
        "command": command,
        "status": REFUSED,
        "detail": None,
        "store": str(store_file),
        **fields,
    }


def _refused(report: dict[str, Any], detail: str) -> dict[str, Any]:
    report["status"] = REFUSED
    report["detail"] = detail
    return report


def _bundle_refusal(store_file: Path) -> str | None:
    """The refusal every command gives a store file inside a backup bundle
    (`backup.bundle_refusal`): its snapshot is checked with `backup
    verify`, and a writing open would rewrite its header."""
    from .backup import bundle_refusal

    return bundle_refusal(store_file)


@contextlib.contextmanager
def _opened(store_file: Path, keys_dir: Path, *, read_only: bool) -> Iterator[Store]:
    """The store at ``store_file``, opened without a rekey: read-only for a
    command's reading, or as a reader that may write when its key is
    present (`Store.open` with ``allow_rekey=False``), for the row a
    command writes. Never the owner's opener, which would replace a
    missing key before `import` could put it back."""
    from .store import Store

    store = Store.open(store_file, keys_dir=keys_dir, read_only=read_only)
    try:
        yield store
    finally:
        store.close()


def _open_error(exc: BaseException) -> str:
    return f"the store does not open: {exc}"


def _same_path(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def daemon_locks(
    state_dir: Path, store_files: Sequence[Path], *, command: str
) -> Iterator[str | None]:
    """Hold the spawn lock and the daemon lock of each store file in
    ``store_files`` for the block, or yield why they could not be taken: a
    daemon serving one of them, or starting to, would go on signing with
    the ring the command changes. Taken as `backup restore` takes its
    store's (`backup._daemon_locks`); the reason names ``command``."""
    from . import _daemon_client as client

    held: list[Any] = []
    seen: set[Path] = set()
    reason: str | None = None
    try:
        ensure_owner_only_dir(state_dir, parents=True)
        for store_file in store_files:
            for lock_path, what in (
                (client.spawn_lock_file_for(state_dir, store_file), "is starting"),
                (client.lock_file_for(state_dir, store_file), "serves"),
            ):
                if lock_path in seen:
                    continue
                seen.add(lock_path)
                handle = client.try_lock(lock_path)
                if handle is None:
                    reason = (
                        f"a bettermemory daemon {what} {store_file} (it holds "
                        f"{lock_path}); close every session using bettermemory "
                        "(Claude Code, Codex, ...), run `bettermemory down`, then "
                        f"run `bettermemory {command}` again"
                    )
                    break
                held.append(handle)
            if reason is not None:
                break
    except OSError as exc:
        reason = f"the daemon locks under {state_dir} cannot be taken: {exc}"
    try:
        yield reason
    finally:
        for handle in reversed(held):
            client.release_lock(handle)


def _append_row(store: Store, kind: str, payload: dict[str, Any]) -> int:
    """Append one row of ``kind`` to ``store``'s log, signed with its key,
    in a write transaction of its own (`Store._transaction`, which moves
    the head checkpoint after the commit). Returns its seq."""
    with store._transaction() as tx:
        row = store._append(tx, kind, payload, session=None)
    return row.seq


def _state_refusal(state: KeyState) -> str:
    """Why a command that needs the store's own key refuses ``state``, in
    the words every reader says it in (`KeyState.summary`, `remedy`)."""
    if state.state == KEY_UNREADABLE and state.error is not None:
        return f"{state.error}; make it readable and run the command again"
    remedy = state.remedy
    return state.summary + (f"; {remedy}" if remedy else "")


# ---------------------------------------------------------------------------
# What the ring holds
# ---------------------------------------------------------------------------


def _held_keys(keyring: KeyRing) -> tuple[list[tuple[str, bytes, bool]], list[str]]:
    """Every key the ring holds for its store, current first: (fingerprint,
    key, current) each, and why each key file that is there could not be
    read. The current key that cannot be read is one of those."""
    keys: list[tuple[str, bytes, bool]] = []
    unread: list[str] = []
    try:
        current = keyring.load_current()
    except KeyUnreadableError as exc:
        current = None
        unread.append(str(exc))
    if current is not None:
        keys.append((fingerprint(current), current, True))
    for path in keyring.retired_paths():
        try:
            key = path.read_bytes()
        except OSError as exc:
            unread.append(f"the retired key file {path} cannot be read ({exc})")
            continue
        own = fingerprint(key)
        if all(own != held for held, _, _ in keys):
            keys.append((own, key, False))
    return keys, unread


def describe(store_file: Path, *, keys_dir: Path | None = None) -> dict[str, Any]:
    """`key show`: the store's key as every reader reads it
    (`store_keys.key_state`), the keys its ring holds by fingerprint (never
    a key), the head checkpoint and the origin anchor against the file's
    log (`store_keys.ring_fit`), and the binding. ``status`` is the key's
    state; ``detail`` is set only when the store does not open. Read-only:
    nothing is written to the store or the ring."""
    root = keys_root(keys_dir)
    report = _report("show", store_file)
    refusal = _bundle_refusal(store_file)
    if refusal is not None:
        return _refused(report, refusal)
    try:
        with _opened(store_file, root, read_only=True) as store:
            state = store.key_state
            ring = store.keyring
            fit = ring_fit(store.conn, ring, current_key=state.key)
            store_id = store.store_id
    except (ValueError, sqlite3.Error, OSError) as exc:
        return _refused(report, _open_error(exc))
    keys, unread = _held_keys(ring)
    origin = ring.read_origin()
    try:
        bound = ring.read_binding()
        binding: dict[str, Any] = {
            "path": None if bound is None else str(bound),
            "this_file": bound is not None and _same_path(bound, store_file),
        }
    except OSError as exc:
        binding = {"path": None, "this_file": False, "unreadable": str(exc)}
    fitted = fit.to_dict()
    origin_out = dict(fitted["origin"])
    if origin is not None and origin.key_fingerprint is not None:
        origin_out["key_fingerprint"] = origin.key_fingerprint
    report.update(
        {
            "status": state.state,
            "store_id": store_id,
            "keys_dir": str(ring.keys_dir),
            "summary": state.summary,
            "remedy": state.remedy,
            "expected_fingerprint": state.expected,
            "keys": [
                {"fingerprint": fp, "current": current} for fp, _, current in keys
            ],
            "unreadable": unread,
            "head": fitted["head"],
            "log_end": fitted["log_end"],
            "origin": origin_out,
            "binding": binding,
        }
    )
    return report


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def export_keys(
    store_file: Path,
    out: Path,
    *,
    keys_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """`key export`: write every key the ring holds for the store, current
    and retired, to ``out`` (one JSON object, `EXPORT_FORMAT`, replaced
    atomically and owner-only, 0600), so a key lost from the keys directory
    can be filed back with `import_keys`. Whoever holds the file can sign
    the store's rows. Refused for a store whose ring is bound to another
    file, a key file this user cannot read, and a ring that holds no key.
    The caller has checked that ``out`` is not the store's own file or in
    the keys directory. Changes nothing in the ring or the store."""
    root = keys_root(keys_dir)
    report = _report("export", store_file, out=str(out), keys=[])
    refusal = _bundle_refusal(store_file)
    if refusal is not None:
        return _refused(report, refusal)
    try:
        with _opened(store_file, root, read_only=True) as store:
            state = store.key_state
            ring = store.keyring
            store_id = store.store_id
    except (ValueError, sqlite3.Error, OSError) as exc:
        return _refused(report, _open_error(exc))
    if state.state in (KEY_UNREADABLE, KEY_BOUND_ELSEWHERE):
        return _refused(report, _state_refusal(state))
    keys, unread = _held_keys(ring)
    if unread:
        return _refused(
            report, "; ".join(unread) + "; make it readable and export again"
        )
    if not keys:
        return _refused(
            report,
            f"the key ring under {ring.keys_dir} holds no key for store "
            f"{store_id}: there is nothing to export",
        )
    moment = now if now is not None else datetime.now(timezone.utc)
    document = {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "store_id": store_id,
        "exported_at": isoformat_utc(moment.astimezone(timezone.utc)),
        "keys": [
            {"fingerprint": fp, "key": key.hex(), "current": current}
            for fp, key, current in keys
        ],
    }
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        atomic_write_bytes(out, data, mode_before_rename=0o600)
    except OSError as exc:
        report["status"] = FAILED
        report["detail"] = f"{out} cannot be written: {exc}"
        return report
    report["status"] = DONE
    report["store_id"] = store_id
    report["keys"] = [{"fingerprint": fp, "current": c} for fp, _, c in keys]
    return report


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


class _ImportFileError(ValueError):
    """The file `import_keys` was given is no key it reads."""


def read_key_file(source: Path) -> tuple[str | None, list[bytes]]:
    """The store id ``source`` names (None when it names none) and the keys
    it holds: an export (`EXPORT_FORMAT`), or a key file as a keys
    directory holds it, 32 raw bytes, whose store id is its name's
    (``<store_id>.key`` or ``<store_id>.<prefix>.key``) when it has that
    shape. Raises OSError when it cannot be read and `_ImportFileError`
    when it is neither, or an export whose key does not match the
    fingerprint it lists."""
    try:
        data = bounded_read(source, _IMPORT_CAP_BYTES)
    except ValueError as exc:
        raise _ImportFileError(str(exc)) from None
    if len(data) == KEY_BYTES:
        named = _KEY_FILE_NAME.fullmatch(source.name)
        return (None if named is None else named["id"]), [data]
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        document = None
    if not isinstance(document, dict) or document.get("format") != EXPORT_FORMAT:
        raise _ImportFileError(
            f"{source} is neither a key export (`bettermemory key export`) nor "
            f"a key file of {KEY_BYTES} bytes"
        )
    store_id = document.get("store_id")
    entries = document.get("keys")
    if not isinstance(store_id, str) or not isinstance(entries, list) or not entries:
        raise _ImportFileError(f"{source} names no store id or no key")
    keys: list[bytes] = []
    for entry in entries:
        hex_key = entry.get("key") if isinstance(entry, dict) else None
        named_fp = entry.get("fingerprint") if isinstance(entry, dict) else None
        if not isinstance(hex_key, str) or _HEX_KEY.fullmatch(hex_key) is None:
            raise _ImportFileError(f"{source} holds an entry that is not a key")
        key = bytes.fromhex(hex_key)
        if named_fp != fingerprint(key):
            raise _ImportFileError(
                f"{source} holds a key whose fingerprint is not the one it lists: "
                "the file was changed"
            )
        keys.append(key)
    return store_id, keys


def _write_new(path: Path, key: bytes) -> bool:
    """Create ``path`` holding ``key``, owner-only, only when nothing is
    there: False when a file is (an owner's open wrote a key meanwhile),
    which is never written over."""
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        return False
    try:
        os.write(fd, key)
        fsync_file(fd)
    finally:
        os.close(fd)
    return True


def import_keys(
    store_file: Path, source: Path, *, keys_dir: Path | None = None
) -> dict[str, Any]:
    """`key import`: file the keys in ``source`` (`read_key_file`) back into
    the store's key ring, each only when it signs a segment of the store's
    log (the first row of a segment its fingerprint names verifies under
    it, `store_keys._signs_a_segment`): the key the store's meta names
    becomes the current key when the ring has none, or holds one that is
    not the store's (that one is retired, never deleted); every other key
    is filed as a retired key, so the segment it signed verifies again. A
    key the ring holds already is left as it is. Then one ``key_imported``
    row, signed with the store's key, names what was filed.

    Refused: the wrong store (``source`` names another store id); a key
    that signs no segment of the log; a store whose ring is bound to
    another file, or whose key file cannot be read; ``source`` unreadable;
    and an import that would leave the ring without the store's own key,
    since nothing could then sign its record (an owner's open writes a new
    key first, after which the old one imports as a retired key)."""
    root = keys_root(keys_dir)
    report = _report("import", store_file, source=str(source), keys=[])
    refusal = _bundle_refusal(store_file)
    if refusal is not None:
        return _refused(report, refusal)
    try:
        claimed, keys = read_key_file(source)
    except _ImportFileError as exc:
        return _refused(report, str(exc))
    except OSError as exc:
        reason = exc.strerror or str(exc)
        return _refused(report, f"the key file {source} cannot be read ({reason})")
    try:
        with _opened(store_file, root, read_only=True) as store:
            state = store.key_state
            ring = store.keyring
            store_id = store.store_id
            segments = chain_segments(
                store.conn, keyring=ring, current_fingerprint=state.expected
            )
    except (ValueError, sqlite3.Error, OSError) as exc:
        return _refused(report, _open_error(exc))
    if claimed is not None and claimed != store_id:
        return _refused(
            report,
            f"{source} holds a key of store {claimed}, and the store at "
            f"{store_file} is store {store_id}: import it into that store",
        )
    if state.state in (KEY_UNREADABLE, KEY_BOUND_ELSEWHERE):
        return _refused(report, _state_refusal(state))
    held = {fp for fp, _, _ in _held_keys(ring)[0]}
    plan: list[dict[str, Any]] = []
    installs: list[tuple[dict[str, Any], bytes]] = []
    for key in keys:
        own = fingerprint(key)
        if own in held:
            plan.append({"fingerprint": own, "action": "held"})
        elif not _signs_a_segment(segments, key):
            plan.append({"fingerprint": own, "action": "signs_nothing"})
        else:
            slot = (
                "current"
                if own == state.expected and state.state in (KEY_MISSING, KEY_FOREIGN)
                else "retired"
            )
            entry = {"fingerprint": own, "action": slot}
            plan.append(entry)
            installs.append((entry, key))
            held.add(own)
    report["keys"] = plan
    report["store_id"] = store_id
    if not installs:
        if any(entry["action"] == "held" for entry in plan):
            report["status"] = UNCHANGED
            report["detail"] = (
                "the key ring holds every key that signs this log already"
            )
            return report
        return _refused(
            report,
            f"no key in {source} signs a segment of the log of the store at "
            f"{store_file}: none is this store's key",
        )
    present_after = state.state == KEY_PRESENT or any(
        entry["action"] == "current" for entry, _ in installs
    )
    if not present_after:
        return _refused(
            report,
            f"{_state_refusal(state)}. None of these keys is the store's own "
            f"(fingerprint {state.expected[:16]}), so nothing could sign the "
            "import's record: open the store once as its owner (the daemon, a "
            "hook or any writing command writes a new key and rekeys the log), "
            "then import this file again, and its keys are filed as retired keys",
        )
    retired_foreign: str | None = None
    try:
        ring.ensure_dir()
        for entry, key in installs:
            own = entry["fingerprint"]
            if entry["action"] == "current":
                if state.state == KEY_FOREIGN:
                    retired_foreign = ring.retire_current()
                if _write_new(ring.current_path(), key):
                    continue
                # An owner's open wrote a key meanwhile: never written over.
                entry["action"] = "retired"
            if not _write_new(ring.retired_path(own), key):
                existing = ring.retired_path(own).read_bytes()
                if existing != key:
                    raise OSError(
                        f"{ring.retired_path(own)} holds another key under the "
                        "same fingerprint prefix"
                    )
    except OSError as exc:
        report["status"] = FAILED
        report["detail"] = (
            f"the key ring under {ring.keys_dir} cannot be written: {exc}"
        )
        return report
    installed = [
        {"fingerprint": e["fingerprint"], "as": e["action"]}
        for e in plan
        if e["action"] in ("current", "retired")
    ]
    try:
        with _opened(store_file, root, read_only=False) as writer:
            if not writer.has_key:
                report["status"] = FAILED
                report["detail"] = (
                    "the keys were filed, and the store still opens without its "
                    f"key: {writer.key_state.summary}"
                )
                return report
            report["seq"] = _append_row(
                writer,
                KEY_IMPORTED,
                {
                    "installed": installed,
                    "retired_current": retired_foreign,
                    "source": str(source),
                },
            )
            report["log"] = writer.log_verify()["status"]
    except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
        report["status"] = FAILED
        report["detail"] = f"the keys were filed, and the record was not written: {exc}"
        return report
    report["status"] = DONE
    report["retired_current"] = retired_foreign
    return report


# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------


def _holds_store(path: Path, store_id: str) -> bool:
    """Does the store file at ``path`` still hold ``store_id``? A file that
    cannot be read to tell is taken to (`store_keys._bound_store_check`)."""
    try:
        return _store_id_at(path) == store_id
    except OSError:
        return True


def adopt_ring(
    store_file: Path,
    *,
    apply: bool,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """`key adopt`: bind the key ring to this store file on the record
    (`log.KeyRing.rebind_store`), whatever it is bound to now, and write a
    ``key_adopted`` row naming the binding before and after. What deleting
    ``<store_id>.path`` by hand did for a store copied with its keys
    directory, or moved beside its original: the ring's key, head and
    anchor become this file's, and the file it was bound to, if it still
    holds the store id, opens for reading only from then on.

    Refused: a ring that holds no key for the store, or a key that is not
    the one the store's meta names; a file that does not carry the ring's
    history (`store_keys.ring_fit`: the row at the origin anchor's seq is
    gone or changed, or the log does not reach the head), whose history
    `log accept-history` accepts instead; a binding or key file this user
    cannot read; a daemon serving or starting for this file or the bound
    one. ``unchanged`` when the ring is bound to this file already. Without
    ``apply``, the plan."""
    root = keys_root(keys_dir)
    resolved = Path(store_file).resolve()
    report = _report(
        "adopt", store_file, binding={"before": None, "after": str(resolved)}
    )
    refusal = _bundle_refusal(store_file)
    if refusal is not None:
        return _refused(report, refusal)
    try:
        with _opened(store_file, root, read_only=True) as store:
            state = store.key_state
            ring = store.keyring
            store_id = store.store_id
            fit = ring_fit(store.conn, ring, current_key=state.key)
    except (ValueError, sqlite3.Error, OSError) as exc:
        return _refused(report, _open_error(exc))
    report["store_id"] = store_id
    report["fit"] = fit.to_dict()
    try:
        before = ring.read_binding()
    except OSError as exc:
        return _refused(
            report,
            f"the binding {ring.binding_path()} cannot be read ({exc}); make it "
            "readable and adopt again",
        )
    report["binding"]["before"] = None if before is None else str(before)
    if state.state == KEY_UNREADABLE:
        return _refused(report, _state_refusal(state))
    if state.key is None:
        return _refused(
            report,
            f"the key ring under {ring.keys_dir} holds no key for store {store_id}: "
            "adopting it would bind a ring that cannot sign this file. `bettermemory "
            "key import FILE` files the store's key back first",
        )
    if fingerprint(state.key) != state.expected:
        return _refused(
            report,
            f"the key ring's key (fingerprint {fingerprint(state.key)[:16]}) is not "
            f"the one this store names ({state.expected[:16]}): it is not this "
            "store's ring to adopt. `bettermemory key import FILE` files the "
            "store's key back",
        )
    if not fit.fits:
        return _refused(
            report,
            f"this file does not carry the key ring's history: {fit.reason()}. "
            "If this file is the store and its history was replaced on purpose "
            "(copied over from another machine, or an older copy put back), "
            "`bettermemory log accept-history --yes` accepts it and binds the key "
            "ring to this file, on the record",
        )
    elsewhere = before is not None and not _same_path(before, resolved)
    report["previous_holds_store"] = (
        elsewhere and before is not None and _holds_store(before, store_id)
    )
    if before is not None and not elsewhere:
        report["status"] = UNCHANGED
        report["detail"] = "the key ring is bound to this file already"
        return report
    if not apply:
        report["status"] = PLANNED
        return report
    locked = [store_file] + ([before] if before is not None and elsewhere else [])
    with daemon_locks(_state_root(state_dir), locked, command="key adopt --yes") as why:
        if why is not None:
            return _refused(report, why)
        try:
            previous = ring.rebind_store(store_file)
        except OSError as exc:
            report["status"] = FAILED
            report["detail"] = (
                f"the key ring under {ring.keys_dir} cannot be bound: {exc}"
            )
            return report
        try:
            with _opened(store_file, root, read_only=False) as writer:
                if not writer.has_key:
                    raise RuntimeError(writer.key_state.summary)
                report["seq"] = _append_row(
                    writer,
                    KEY_ADOPTED,
                    {
                        "binding_before": None if previous is None else str(previous),
                        "binding_after": str(resolved),
                        "previous_holds_store": report["previous_holds_store"],
                    },
                )
        except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
            # Nothing is on the record, so the binding goes back as it was.
            if previous is not None:
                with contextlib.suppress(OSError):
                    ring.rebind_store(previous)
            report["status"] = FAILED
            report["detail"] = f"the binding was put back; the record failed: {exc}"
            return report
    report["status"] = DONE
    return report


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


def rotate_key(
    store_file: Path, *, apply: bool, keys_dir: Path | None = None
) -> dict[str, Any]:
    """`key rotate`: retire the store's current key (kept under its
    fingerprint, so the rows it signed still verify), write a new one, set
    meta to it and append the ``rekey`` row that opens its segment, signed
    with the new key: the rekey the owner's opener runs for a store whose
    key is missing, on a key that is present. Under the store's write lock,
    so a daemon serving the store signs its next row with the new key
    (`store_keys._signing_key`). The origin anchor does not move; the key
    it names stays held. Refused unless the key is present (a missing,
    foreign or unreadable key, or a ring bound elsewhere, in the words
    every reader says it in). Without ``apply``, the plan."""
    root = keys_root(keys_dir)
    report = _report("rotate", store_file)
    refusal = _bundle_refusal(store_file)
    if refusal is not None:
        return _refused(report, refusal)
    try:
        with _opened(store_file, root, read_only=True) as store:
            state = store.key_state
    except (ValueError, sqlite3.Error, OSError) as exc:
        return _refused(report, _open_error(exc))
    if state.state != KEY_PRESENT:
        return _refused(report, _state_refusal(state))
    report["old_fingerprint"] = state.expected
    if not apply:
        report["status"] = PLANNED
        return report
    try:
        with _opened(store_file, root, read_only=False) as writer:
            if not writer.has_key:
                return _refused(report, _state_refusal(writer.key_state))
            new, seq = _rotate(writer)
    except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
        report["status"] = FAILED
        report["detail"] = f"the key was not rotated: {exc}"
        return report
    report["status"] = DONE
    report["new_fingerprint"] = new
    report["seq"] = seq
    return report


def _rotate(store: Store) -> tuple[str, int]:
    """The rotation on an open store that holds its key, under its write
    lock: meta read again there, the current key retired and a new one
    written, meta set and the ``rekey`` row appended in one transaction. A
    failure inside puts the old key back as the current key, so the ring
    and meta never disagree."""
    ring = store.keyring
    old = store._key
    assert old is not None
    with store._transaction() as tx:
        recorded = tx.execute(
            "SELECT value FROM meta WHERE key = 'key_fingerprint'"
        ).fetchone()
        if recorded is None or str(recorded[0]) != fingerprint(old):
            raise RuntimeError(
                "the store was rekeyed since it was opened; run the rotation again"
            )
        ring.retire_current()
        try:
            new = ring.create_key()
            store._key = new
            store._key_state = KeyState(
                KEY_PRESENT, ring, store.path, fingerprint(new), key=new
            )
            tx.execute(
                "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'",
                (fingerprint(new),),
            )
            row = store._append(
                tx,
                REKEY,
                {
                    "old_fingerprint": fingerprint(old),
                    "new_fingerprint": fingerprint(new),
                    "cause": "rotate",
                },
                session=None,
            )
        except BaseException:
            store._key = old
            store._key_state = KeyState(
                KEY_PRESENT, ring, store.path, fingerprint(old), key=old
            )
            atomic_write_bytes(ring.current_path(), old, mode_before_rename=0o600)
            raise
    store._forget_segments()
    log.warning(
        "store %s: key %s retired and key %s written by `bettermemory key "
        "rotate`; the rekey row at seq %d opens its segment",
        store.path,
        fingerprint(old)[:16],
        fingerprint(new)[:16],
        row.seq,
    )
    return fingerprint(new), row.seq


# ---------------------------------------------------------------------------
# log accept-history
# ---------------------------------------------------------------------------


def _head_dict(head: Head | None) -> dict[str, Any] | None:
    return None if head is None else {"seq": head.seq, "mac": head.mac}


def _row_dict(row: LogRow | None) -> dict[str, Any] | None:
    return None if row is None else {"seq": row.seq, "mac": row.mac}


def _origin_dict(origin: HistoryOrigin | None) -> dict[str, Any] | None:
    return None if origin is None else origin.to_dict()


def _set_head(ring: KeyRing, head: Head | None) -> Head | None:
    """Write ``head`` as the head checkpoint whatever the current one says,
    or remove the checkpoint when ``head`` is None, under the lock
    `log.KeyRing.write_head` takes, and return the one it replaced: with a
    restore's (`backup._rewind_head`), the one place a head moves
    backwards, and the caller records it."""
    ring.ensure_dir()
    with flock_excl(ring.head_path()):
        before = ring.read_head()
        if head is None:
            with contextlib.suppress(FileNotFoundError):
                ring.head_path().unlink()
        else:
            payload = json.dumps({"seq": head.seq, "mac": head.mac, "ts": head.ts})
            atomic_write_bytes(
                ring.head_path(), payload.encode("utf-8"), mode_before_rename=0o600
            )
    return before


def _first_held(store: Store, key: bytes | None) -> dict[str, Any] | None:
    """The row the owner's open anchors a store with no anchor at
    (`Store._settle_origin`): the first row of the earliest segment a key
    the ring holds signs, when that row verifies under it. None when there
    is none yet (the open then settles a missing or foreign key first and
    anchors the ``rekey`` row it writes)."""
    segments = chain_segments(
        store.conn,
        keyring=store.keyring,
        current_fingerprint=store.key_state.expected,
        current_key=key,
    )
    held = next((segment for segment in segments if segment.held), None)
    if held is None or held.key is None or held.first_row is None:
        return None
    if not row_verifies(held.key, held.first_row):
        return None
    return {"seq": held.first_seq, "key_fingerprint": fingerprint(held.key)}


def accept_history(
    store_file: Path,
    *,
    apply: bool,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """`log accept-history`: accept the store file's history as it stands,
    on the record, where the key ring's origin anchor or head checkpoint
    says it was replaced: an origin that reads ``origin_mismatch`` (a store
    file copied over this host's copy again from the machine it came from;
    an anchor written before rc13 whose key was lost, which nothing can
    tell from a rewrite) or ``unverifiable`` (the anchor's key lost for
    good), or a head past the log's end or on a row with another MAC (an
    older copy put back by hand). What deleting ``<store_id>.origin`` and
    ``<store_id>.head`` by hand did, with a row and a record.

    Under the daemon and spawn locks of this file, and of the file the ring
    was bound to when that is another: the anchor and the head are set
    aside, the ring is bound to this file when it was bound to another, and
    the store is opened as its owner opens it (`Store.open` with
    ``allow_rekey=True``), which settles a missing or foreign key as every
    writing command does, anchors this host's history at the first row of
    the earliest segment a held key signs, and binds the ring. Then a
    ``history_accepted`` row, signed with the store's key, names the
    anchor, head and binding before and after and the log's end before, and
    the rewind record beside the head gets the same entry
    (`backup.restore_record_path`), so `log export` warns a cursor taken
    before it across the seqs it handed to new rows. A failure before the
    row puts the anchor, the head and the binding back.

    Refused: a key file this user cannot read; a binding that cannot be
    read; a daemon serving or starting for the store or the bound file.
    ``unchanged`` when the anchor holds (or there is none) and the head is
    on the log: nothing to accept (a ring bound to another file is then
    `key adopt`'s). Without ``apply``, the plan."""
    root = keys_root(keys_dir)
    resolved = Path(store_file).resolve()
    report = _report(
        "accept-history",
        store_file,
        origin={"before": None, "after": None},
        head={"before": None, "after": None},
        binding={"before": None, "after": str(resolved)},
        log_end_before=None,
    )
    refusal = _bundle_refusal(store_file)
    if refusal is not None:
        return _refused(report, refusal)
    try:
        with _opened(store_file, root, read_only=True) as store:
            state = store.key_state
            ring = store.keyring
            store_id = store.store_id
            fit = ring_fit(store.conn, ring, current_key=state.key)
            candidate = (
                _first_held(store, state.key) if state.state == KEY_PRESENT else None
            )
    except (ValueError, sqlite3.Error, OSError) as exc:
        return _refused(report, _open_error(exc))
    report["store_id"] = store_id
    report["key_state"] = state.state
    report["fit"] = fit.to_dict()
    report["origin"]["before"] = _origin_dict(ring.read_origin())
    report["head"]["before"] = _head_dict(ring.read_head())
    report["log_end_before"] = _row_dict(fit.log_end)
    report["anchor_at"] = candidate
    try:
        before = ring.read_binding()
    except OSError as exc:
        return _refused(
            report,
            f"the binding {ring.binding_path()} cannot be read ({exc}); make it "
            "readable and run the command again",
        )
    report["binding"]["before"] = None if before is None else str(before)
    if state.state == KEY_UNREADABLE:
        return _refused(report, _state_refusal(state))
    elsewhere = before is not None and not _same_path(before, resolved)
    replaced = (
        fit.origin.broken
        or fit.origin.status == ORIGIN_UNVERIFIABLE
        or fit.head_status in (HEAD_PAST, HEAD_MISMATCH)
    )
    if not replaced:
        report["status"] = UNCHANGED
        report["detail"] = (
            "nothing to accept: the origin anchor holds and the head checkpoint "
            "is on this file's log"
        ) + (
            f"; the key ring is bound to {before}, and `bettermemory key adopt "
            "--yes` binds it to this file"
            if elsewhere
            else ""
        )
        return report
    if not apply:
        report["status"] = PLANNED
        return report
    locked = [store_file] + ([before] if before is not None and elsewhere else [])
    command = "log accept-history --yes"
    with daemon_locks(_state_root(state_dir), locked, command=command) as why:
        if why is not None:
            return _refused(report, why)
        return _accept(
            report, store_file, root, ring, before if elsewhere else None, now
        )


def _accept(
    report: dict[str, Any],
    store_file: Path,
    root: Path,
    ring: KeyRing,
    moved_from: Path | None,
    now: datetime | None,
) -> dict[str, Any]:
    """`accept_history`'s steps under the daemon locks. ``moved_from`` is
    the other file the ring was bound to, which the binding leaves."""
    from .backup import _append_record, restore_record_path, utc_stamp
    from .store import Store

    moment = now if now is not None else datetime.now(timezone.utc)
    origin_before = ring.read_origin()
    head_before: Head | None = None
    try:
        ring.replace_origin(None)
        head_before = _set_head(ring, None)
        if moved_from is not None:
            ring.rebind_store(store_file)
        store = Store.open(store_file, keys_dir=root, allow_rekey=True)
    except (ValueError, sqlite3.Error, OSError) as exc:
        _put_back(ring, origin_before, head_before, moved_from)
        report["status"] = FAILED
        report["detail"] = (
            f"the owner's open failed ({exc}); the anchor, head and binding were "
            "put back"
        )
        return report
    try:
        if not store.has_key:
            raise RuntimeError(store.key_state.summary)
        report["origin"]["after"] = _origin_dict(ring.read_origin())
        try:
            bound = ring.read_binding()
        except OSError:
            bound = None
        report["binding"]["after"] = None if bound is None else str(bound)
        report["seq"] = _append_row(
            store,
            HISTORY_ACCEPTED,
            {
                "origin_before": report["origin"]["before"],
                "origin_after": report["origin"]["after"],
                "head_before": _head_dict(head_before),
                "log_end_before": report["log_end_before"],
                "binding_before": report["binding"]["before"],
                "binding_after": report["binding"]["after"],
                "origin_status_before": report["fit"]["origin"]["status"],
                "head_status_before": report["fit"]["head"]["status"],
            },
        )
    except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
        store.close()
        _put_back(ring, origin_before, head_before, moved_from)
        report["status"] = FAILED
        report["detail"] = (
            f"the acceptance was not recorded ({exc}); the anchor, head and "
            "binding were put back"
        )
        return report
    report["status"] = DONE
    try:
        report["head"]["after"] = _head_dict(ring.read_head())
        log_end = report["log_end_before"]
        record = restore_record_path(root, store.store_id)
        _append_record(
            record,
            {
                "restored_at": isoformat_utc(moment.astimezone(timezone.utc)),
                "accepted_history": True,
                "store": str(store_file),
                "store_id": store.store_id,
                "replaced_store_id": store.store_id,
                "head_before": _head_dict(head_before),
                "head_after": log_end,
                "log_end_before": log_end,
                "origin_before": report["origin"]["before"],
                "origin_after": report["origin"]["after"],
                "binding_before": report["binding"]["before"],
                "binding_after": report["binding"]["after"],
                "seq": report["seq"],
            },
            utc_stamp(moment),
        )
        report["record"] = str(record)
        report["log"] = store.log_verify()["status"]
    except (ValueError, sqlite3.Error, OSError) as exc:
        report["detail"] = f"accepted and recorded at seq {report['seq']}; then {exc}"
    finally:
        store.close()
    return report


def _put_back(
    ring: KeyRing,
    origin: HistoryOrigin | None,
    head: Head | None,
    binding: Path | None,
) -> None:
    """Put the anchor, the head and (when it was moved off ``binding``) the
    binding back as they were, after a failure before the acceptance was
    recorded."""
    with contextlib.suppress(OSError):
        ring.replace_origin(origin)
    with contextlib.suppress(OSError):
        _set_head(ring, head)
    if binding is not None:
        with contextlib.suppress(OSError):
            ring.rebind_store(binding)


# ---------------------------------------------------------------------------
# Orphaned key rings (`bettermemory doctor --clear-orphan-keys`)
# ---------------------------------------------------------------------------

#: How a key ring in the keys directory stands with its store
#: (`ring_standing`). ``orphaned``: the store file it is bound to is gone,
#: or holds another store or none; `clear_orphan_rings` deletes it.
#: ``in_use``: that file holds its store, a daemon serves that file, or it
#: is the ring of the store doctor checks. ``unreachable``: whether the
#: store is gone cannot be told (a path this user cannot read, or one whose
#: nearest existing directory is a filesystem root or a mount directory,
#: where a volume or share that is not mounted now would be). ``unbound``:
#: it names no store file (a ring from before 9.0.0rc13, or a binding that
#: does not read). Only ``orphaned`` rings are ever deleted.
RING_ORPHANED = "orphaned"
RING_IN_USE = "in_use"
RING_UNREACHABLE = "unreachable"
RING_UNBOUND = "unbound"

#: The directories volumes and shares are mounted in. A store file whose
#: nearest existing directory is one of these, a user's directory right
#: under ``/media`` or ``/run/media``, or a filesystem root may be on a
#: volume that is only unplugged.
_MOUNT_DIRS = frozenset({"/Volumes", "/mnt", "/media", "/run/media"})
_USER_MOUNT_DIRS = frozenset({"/media", "/run/media"})


@dataclass(frozen=True)
class RingStanding:
    """One key ring in the keys directory, how it stands with its store
    and why, and the names of its files there."""

    store_id: str
    state: str
    bound_to: Path | None
    reason: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "state": self.state,
            "bound_to": None if self.bound_to is None else str(self.bound_to),
            "reason": self.reason,
            "files": list(self.files),
        }


def _ring_ids(root: Path) -> list[str]:
    """The store ids the keys directory holds files of, sorted: every
    entry named ``<store_id>.<anything>``. [] when there is no keys
    directory; OSError when it cannot be listed."""
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return []
    ids = {name.partition(".")[0] for name in names if "." in name}
    return sorted(i for i in ids if STORE_ID_PATTERN.fullmatch(i))


def _ring_paths(root: Path, store_id: str) -> list[Path]:
    """Every file of the ring ``store_id`` under ``root``: its keys, head,
    anchor and binding, their lock sidecars and its restore record. The id
    is 32 hex digits (`KeyRing`), so it globs as itself."""
    return sorted(p for p in root.glob(f"{store_id}.*") if not p.is_dir())


def _nearest_directory(path: Path) -> Path | None:
    """The nearest ancestor of ``path`` that exists, or None when none
    does (a drive or a share that is not there). OSError when one cannot
    be read to tell."""
    for parent in path.parents:
        try:
            os.stat(parent)
        except (FileNotFoundError, NotADirectoryError):
            continue
        return parent
    return None


def _maybe_unmounted(directory: Path | None) -> bool:
    """Whether a store file that is gone, under ``directory`` as its
    nearest existing directory, may be on a volume or share that is not
    mounted now rather than deleted."""
    if directory is None or directory.parent == directory:
        return True
    return (
        directory.as_posix() in _MOUNT_DIRS
        or directory.parent.as_posix() in _USER_MOUNT_DIRS
    )


def _daemon_serves(state_dir: Path, store_file: Path) -> bool:
    """Whether a daemon holds the lock of ``store_file``: one still serving
    a deleted store goes on writing its ring's head. Taken for an instant
    and given back; a lock file that is not there is held by nobody and is
    not created. A lock that cannot be tried is taken as held."""
    from . import _daemon_client as client

    lock = client.lock_file_for(state_dir, store_file)
    try:
        if not lock.is_file():
            return False
        handle = client.try_lock(lock)
    except OSError:
        return True
    if handle is None:
        return True
    client.release_lock(handle)
    return False


def ring_standing(
    root: Path, store_id: str, *, keep: frozenset[str], state_dir: Path
) -> RingStanding:
    """How the key ring ``store_id`` under ``root`` stands with its store
    (`RING_ORPHANED` and the others), read without writing: the binding,
    the store id the bound file holds (`store_keys._store_id_at`, which
    reads it without opening it as a store), the daemon lock of that file
    and, for a file that is gone, the nearest directory on its way.
    ``keep`` names store ids that are in use whatever the binding says."""
    ring = KeyRing(root, store_id)
    files = tuple(p.name for p in _ring_paths(root, store_id))

    def standing(state: str, reason: str, bound: Path | None = None) -> RingStanding:
        return RingStanding(store_id, state, bound, reason, files)

    if store_id in keep:
        return standing(RING_IN_USE, "it is the key ring of the store doctor checks")
    try:
        bound = ring.read_binding()
    except OSError as exc:
        return standing(
            RING_UNREACHABLE, f"its binding {ring.binding_path()} cannot be read: {exc}"
        )
    if bound is None:
        return standing(
            RING_UNBOUND,
            "it names no store file (a key ring from before 9.0.0rc13, or a "
            "binding that does not read)",
        )
    try:
        held = _store_id_at(bound)
        if held == store_id:
            return standing(RING_IN_USE, f"{bound} holds its store", bound)
        if _daemon_serves(state_dir, bound):
            return standing(RING_IN_USE, f"a bettermemory daemon serves {bound}", bound)
        if held is not None:
            return standing(RING_ORPHANED, f"{bound} holds another store", bound)
        real = Path(os.path.realpath(bound))
        if os.path.lexists(real):
            return standing(RING_ORPHANED, f"{bound} holds no store", bound)
        nearest = _nearest_directory(real)
    except OSError as exc:
        return standing(
            RING_UNREACHABLE,
            f"{bound} cannot be read to tell whether the store is there: {exc}",
            bound,
        )
    if _maybe_unmounted(nearest):
        where = "no directory" if nearest is None else str(nearest)
        return standing(
            RING_UNREACHABLE,
            f"{bound} is not there and the nearest directory on its way is {where}: "
            "it may be on a volume or share that is not mounted now",
            bound,
        )
    return standing(RING_ORPHANED, f"{bound} is gone", bound)


@dataclass(frozen=True)
class RingSurvey:
    """Every key ring in the keys directory and how it stands
    (`survey_rings`), with the store ids taken as in use."""

    keys_dir: Path
    rings: tuple[RingStanding, ...]
    keep: frozenset[str]

    def of(self, state: str) -> list[RingStanding]:
        return [ring for ring in self.rings if ring.state == state]

    def summary(self) -> str:
        """One line: the orphaned rings, or that there are none, and the
        rings kept because their store cannot be told gone."""
        orphaned = len(self.of(RING_ORPHANED))
        total = len(self.rings)
        rings = f"{total} key ring{'' if total == 1 else 's'} under {self.keys_dir}"
        if orphaned == 1:
            line = f"{rings}: 1 belongs to a store that is gone"
        elif orphaned:
            line = f"{rings}: {orphaned} belong to stores that are gone"
        elif total:
            line = f"{rings}, none orphaned"
        else:
            line = f"no key rings under {self.keys_dir}"
        kept = [
            f"{len(found)} {what}"
            for found, what in (
                (self.of(RING_UNREACHABLE), "whose store cannot be reached now"),
                (self.of(RING_UNBOUND), "whose binding names no store file"),
            )
            if found
        ]
        if kept:
            line += f"; kept: {' and '.join(kept)}"
        return line

    def remedy(self) -> str | None:
        if not self.of(RING_ORPHANED):
            return None
        return (
            "run `bettermemory doctor --clear-orphan-keys` to delete them; a store "
            "moved by hand and not opened since reads as gone, so run `bettermemory "
            "key adopt --yes` with BETTERMEMORY_DIR naming its directory first to "
            "keep its key ring"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys_dir": str(self.keys_dir),
            "rings": len(self.rings),
            "in_use": len(self.of(RING_IN_USE)),
            "orphaned": [ring.to_dict() for ring in self.of(RING_ORPHANED)],
            "unreachable": [ring.to_dict() for ring in self.of(RING_UNREACHABLE)],
            "unbound": [ring.store_id for ring in self.of(RING_UNBOUND)],
        }


def survey_rings(
    store_file: Path | None,
    *,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
) -> RingSurvey:
    """Every key ring in the keys directory and how it stands with its
    store (`ring_standing`). The ring of the store at ``store_file`` (the
    one doctor checks) is in use whatever its binding says. Reads only;
    OSError when the keys directory cannot be listed."""
    root = keys_root(keys_dir)
    keep: set[str] = set()
    if store_file is not None:
        with contextlib.suppress(OSError):
            current = _store_id_at(Path(store_file))
            if current is not None:
                keep.add(current)
    state = _state_root(state_dir)
    kept = frozenset(keep)
    rings = tuple(
        ring_standing(root, store_id, keep=kept, state_dir=state)
        for store_id in _ring_ids(root)
    )
    return RingSurvey(root, rings, kept)


def _clear_ring(
    root: Path, store_id: str, *, keep: frozenset[str], state_dir: Path
) -> tuple[RingStanding, list[str] | None]:
    """Read the ring ``store_id`` again under its binding's lock, as
    `log.KeyRing.bind_store` writes a binding, and delete its files when it
    still reads orphaned: an owner's open of a store moved to a new path in
    the meantime binds the ring first, or waits for this. The standing, and
    the names deleted (None when it no longer read orphaned). The lock
    sidecar goes after the lock is given back."""
    ring = KeyRing(root, store_id)
    binding = ring.binding_path()
    lock = binding.with_suffix(binding.suffix + ".lock")
    with flock_excl(binding):
        standing = ring_standing(root, store_id, keep=keep, state_dir=state_dir)
        if standing.state != RING_ORPHANED:
            return standing, None
        removed: list[str] = []
        for path in _ring_paths(root, store_id):
            if path != lock:
                path.unlink(missing_ok=True)
                removed.append(path.name)
    with contextlib.suppress(OSError):
        lock.unlink(missing_ok=True)
        removed.append(lock.name)
    return standing, removed


def clear_orphan_rings(
    store_file: Path | None,
    *,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """`bettermemory doctor --clear-orphan-keys`: delete every key ring in
    the keys directory whose store is gone (`survey_rings`, `RING_ORPHANED`)
    with all its files: keys, head, anchor, binding, lock sidecars and
    restore record. A ring whose store cannot be told gone, that names no
    store file, or that a daemon or the store at ``store_file`` holds is
    never deleted; each orphaned one is read again under its binding's lock
    first (`_clear_ring`). What was deleted (``cleared``), what read
    otherwise the second time (``kept``) and what could not be deleted
    (``failed``, with the error)."""
    survey = survey_rings(store_file, keys_dir=keys_dir, state_dir=state_dir)
    state = _state_root(state_dir)
    cleared: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for first in survey.of(RING_ORPHANED):
        try:
            standing, removed = _clear_ring(
                survey.keys_dir, first.store_id, keep=survey.keep, state_dir=state
            )
        except OSError as exc:
            failed.append(
                {"store_id": first.store_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if removed is None:
            kept.append(standing.to_dict())
        else:
            cleared.append({**standing.to_dict(), "files": removed})
    return {
        "keys_dir": str(survey.keys_dir),
        "cleared": cleared,
        "kept": kept,
        "failed": failed,
    }


__all__ = [
    "DONE",
    "EXIT_CODES",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "FAILED",
    "PLANNED",
    "REFUSED",
    "RING_IN_USE",
    "RING_ORPHANED",
    "RING_UNBOUND",
    "RING_UNREACHABLE",
    "UNCHANGED",
    "RingStanding",
    "RingSurvey",
    "accept_history",
    "adopt_ring",
    "clear_orphan_rings",
    "daemon_locks",
    "describe",
    "export_keys",
    "import_keys",
    "keys_root",
    "read_key_file",
    "ring_standing",
    "rotate_key",
    "survey_rings",
]
