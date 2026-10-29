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
store is shown gone, and their move to the keys directory's quarantine
(nothing is deleted), which keeps every ring whose store is not shown
gone.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
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
    replace_atomic,
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
    last_row,
    row_verifies,
)
from .store_keys import (
    HEAD_MISMATCH,
    HEAD_PAST,
    KEY_BOUND_ELSEWHERE,
    KEY_FOREIGN,
    KEY_MISSING,
    KEY_PRESENT,
    KEY_UNCLAIMED,
    KEY_UNREADABLE,
    KeyState,
    _signs_a_segment,
    _store_id_at,
    finish_rotation,
    pending_key_path,
    ring_fit,
    write_key_once,
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


def daemon_locks(
    state_dir: Path, store_files: Sequence[Path], *, command: str
) -> contextlib.AbstractContextManager[str | None]:
    """The spawn lock and the daemon lock of each store file in
    ``store_files`` for the block, or why they could not be taken
    (`_daemon_client.daemon_locks`, which `backup restore` takes too): a
    daemon serving one of them, or starting to, would go on signing with
    the ring the command changes. The reason names ``command``."""
    from . import _daemon_client as client

    return client.daemon_locks(state_dir, store_files, command=command)


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


def export_exposure(out: Path) -> list[str]:
    """Why the export at ``out`` sits where keys should not, as warnings
    (cold review L6): in a git working tree, found from the repository's
    files (`githead.locate`, which runs no git), ignored or not, since one
    `git add` puts every key of the store in the repository's history and
    keys belong in no working tree; in a synced folder
    (`posture.synced_root`), from which the keys leave this host. Empty
    when neither; the export is written either way."""
    from . import githead
    from .posture import synced_root

    directory = out.parent
    warnings: list[str] = []
    held = githead.locate(directory)
    if held is not None:
        warnings.append(
            f"{out} is inside the git working tree at {held.worktree_root}: "
            "one `git add` puts every key of the store in the repository's "
            "history; keep the export outside any working tree"
        )
    service = synced_root(directory)
    if service is not None:
        warnings.append(
            f"{out} is in a {service} folder: the keys leave this host with "
            "whatever the provider sees; keep the export where only you can "
            "read it"
        )
    return warnings


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
    the keys directory. ``warnings`` names a git working tree or a synced
    folder ``out`` is in (`export_exposure`); the export is written all
    the same. Changes nothing in the ring or the store."""
    root = keys_root(keys_dir)
    report = _report("export", store_file, out=str(out), keys=[], warnings=[])
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
    if state.state in (KEY_UNREADABLE, KEY_BOUND_ELSEWHERE, KEY_UNCLAIMED):
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
    report["warnings"] = export_exposure(out)
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
    if state.state in (KEY_UNREADABLE, KEY_BOUND_ELSEWHERE, KEY_UNCLAIMED):
        return _refused(report, _state_refusal(state))
    held = {fp for fp, _, _ in _held_keys(ring)[0]}
    plan: list[dict[str, Any]] = []
    installs: list[tuple[dict[str, Any], bytes]] = []
    for key in keys:
        own = fingerprint(key)
        # The store's own key, when it reads missing or foreign, goes back as
        # the current key even when the ring holds a retired copy of it.
        current = own == state.expected and state.state in (KEY_MISSING, KEY_FOREIGN)
        if own in held and not current:
            plan.append({"fingerprint": own, "action": "held"})
        elif own not in held and not _signs_a_segment(segments, key):
            plan.append({"fingerprint": own, "action": "signs_nothing"})
        else:
            slot = "current" if current else "retired"
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
                if write_key_once(ring.current_path(), key):
                    continue
                # An owner's open wrote a key meanwhile: never written over.
                entry["action"] = "retired"
            if not write_key_once(ring.retired_path(own), key):
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
    store_file: Path,
    *,
    apply: bool,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """`key rotate`: write a new key for the store, set meta to it and
    append the ``rekey`` row that opens its segment, signed with the new
    key, keeping the old key under its fingerprint so the rows it signed
    still verify (`_rotate`). The current key file is never absent while it
    runs, so an owner's open racing it never reads the key missing and
    never writes one. Under the store's daemon and spawn locks, refused
    while a daemon serves or starts for the store (naming `bettermemory
    down`), and its write lock. The origin anchor does not move; the key it
    names stays held. Refused unless the key is present (a missing,
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
    locks = daemon_locks(
        _state_root(state_dir), [store_file], command="key rotate --yes"
    )
    with locks as why:
        if why is not None:
            return _refused(report, why)
        try:
            with _opened(store_file, root, read_only=False) as writer:
                if not writer.has_key:
                    return _refused(report, _state_refusal(writer.key_state))
                new, seq, pending = _rotate(writer)
        except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
            report["status"] = FAILED
            report["detail"] = f"the key was not rotated: {exc}"
            return report
    report["status"] = DONE
    report["new_fingerprint"] = new
    report["seq"] = seq
    if pending is not None:
        report["detail"] = (
            f"the rotation is committed at seq {seq}, and the new key is still at "
            f"{pending}: the next open of the store makes it the current key"
        )
    return report


def _meta_fingerprint(store: Store) -> str | None:
    """The key fingerprint the store's meta names now, read outside any
    transaction; None when it cannot be read."""
    try:
        row = store.conn.execute(
            "SELECT value FROM meta WHERE key = 'key_fingerprint'"
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def _take_back(path: Path, key: bytes) -> None:
    """Remove ``path`` when it holds exactly ``key``: what a rotation that
    did not commit wrote (a write that failed after the file landed
    included), and nothing else."""
    with contextlib.suppress(OSError):
        if path.read_bytes() == key:
            path.unlink()


def _rotate(store: Store) -> tuple[str, int, Path | None]:
    """The rotation on an open store that holds its key, in this order,
    so that the ring and meta agree after a crash or a failure at any step
    and the current key file is never absent:

    1. under the write lock, meta read again (another connection's rekey
       refuses the rotation);
    2. the old key copied to its retired name (``<id>.<prefix>.key``), a
       file only ever created, never written over;
    3. the new key written to `store_keys.pending_key_path`;
    4. meta set and the ``rekey`` row appended, signed with the new key,
       and the transaction committed;
    5. the pending key renamed over the current key file
       (`store_keys.finish_rotation`), which an open that finds a committed
       rotation unfinished does too.

    A failure before the commit takes back what 2 and 3 wrote (a retired
    copy this rotation created, the pending key), once meta is read naming
    the old key; when meta cannot be read the files stay (a pending key
    meta does not name is never used). Returns the new
    fingerprint, the rekey row's seq, and the pending path when step 5 did
    not happen (the next open finishes it)."""
    ring = store.keyring
    old = store._key
    assert old is not None
    new = secrets.token_bytes(KEY_BYTES)
    pending = pending_key_path(ring)
    retired = ring.retired_path(fingerprint(old))
    retired_was_there = os.path.lexists(retired)
    try:
        with store._transaction() as tx:
            recorded = tx.execute(
                "SELECT value FROM meta WHERE key = 'key_fingerprint'"
            ).fetchone()
            if recorded is None or str(recorded[0]) != fingerprint(old):
                raise RuntimeError(
                    "the store was rekeyed since it was opened; run the rotation again"
                )
            ring.ensure_dir()
            if not write_key_once(retired, old) and retired.read_bytes() != old:
                raise OSError(f"{retired} holds another key under the same prefix")
            atomic_write_bytes(pending, new, mode_before_rename=0o600)
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
        if _meta_fingerprint(store) != fingerprint(new):
            store._key = old
            store._key_state = KeyState(
                KEY_PRESENT, ring, store.path, fingerprint(old), key=old
            )
            if _meta_fingerprint(store) == fingerprint(old):
                _take_back(pending, new)
                if not retired_was_there:
                    _take_back(retired, old)
            raise
        # The commit landed and what failed came after it: finish below.
        committed = last_row(store.conn)
        assert committed is not None
        row = committed
    store._forget_segments()
    finished = finish_rotation(ring, store.path, fingerprint(new))
    if finished is None:
        # An open that found the rotation committed may have finished it.
        with contextlib.suppress(KeyUnreadableError):
            finished = ring.load_current()
        if finished is not None and fingerprint(finished) != fingerprint(new):
            finished = None
    log.warning(
        "store %s: key %s retired and key %s written by `bettermemory key "
        "rotate`; the rekey row at seq %d opens its segment",
        store.path,
        fingerprint(old)[:16],
        fingerprint(new)[:16],
        row.seq,
    )
    return fingerprint(new), row.seq, (None if finished is not None else pending)


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


def _first_held(store: Store, key: bytes | None) -> HistoryOrigin | None:
    """The anchor the owner's open puts on a store with no anchor
    (`Store._settle_origin`): the first row of the earliest segment a key
    the ring holds signs, when that row verifies under it, named by that
    key. None when there is none."""
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
    return HistoryOrigin(
        seq=held.first_row.seq,
        mac=held.first_row.mac,
        key_fingerprint=fingerprint(held.key),
    )


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
    was bound to when that is another (`_accept`, whose order leaves a
    recorded acceptance or none after a crash at any step): the ring is
    bound to this file when it is bound to another or to none, the store
    is opened as its owner opens it (`Store.open` with
    ``allow_rekey=True``), which settles a missing or foreign key as every
    writing command does, a ``history_accepted`` row, signed with the
    store's key, names the anchor, head and binding before and after and
    the log's end before, then the anchor moves to the first row of the
    earliest segment a held key signs and the head to that row, and the
    rewind record beside the head gets the same entry
    (`backup.restore_record_path`), so `log export` warns a cursor taken
    before it across the seqs it handed to new rows.

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
    report["anchor_at"] = (
        None
        if candidate is None
        else {"seq": candidate.seq, "key_fingerprint": candidate.key_fingerprint}
    )
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
            report, store_file, root, ring, before, before is None or elsewhere, now
        )


def _restore_binding(ring: KeyRing, binding: Path | None) -> None:
    """Put the ring's binding back to ``binding``: that file, or none."""
    with contextlib.suppress(OSError):
        if binding is not None:
            ring.rebind_store(binding)
        else:
            with flock_excl(ring.binding_path()):
                ring.binding_path().unlink(missing_ok=True)


def _accept(
    report: dict[str, Any],
    store_file: Path,
    root: Path,
    ring: KeyRing,
    bound_before: Path | None,
    rebind: bool,
    now: datetime | None,
) -> dict[str, Any]:
    """`accept_history`'s steps under the daemon locks, in an order that
    leaves, after a crash at any step, a recorded acceptance or nothing
    accepted (the anchor and head as they were):

    1. the ring bound to this file, when ``rebind`` (bound to another file,
       ``bound_before``, or to none): the open in 2 is given no key
       otherwise;
    2. the store opened as its owner opens it (a missing or foreign key
       settled, with its own ``rekey`` row);
    3. the ``history_accepted`` row, naming the anchor 4 writes (the first
       row of the earliest segment a held key signs, `_first_held`);
    4. the anchor replaced (`log.KeyRing.replace_origin`), then the head
       moved to the row;
    5. the rewind record.

    The anchor and head are read before anything runs. A failure before 3
    puts back what 1 changed and says the anchor and head were not
    touched; a failure in 4 puts the anchor back, keeps the head, and says
    the acceptance is recorded and not applied (running the command again
    applies it). Each failure names its step."""
    from .backup import _append_record, restore_record_path, utc_stamp
    from .store import Store

    moment = now if now is not None else datetime.now(timezone.utc)
    origin_before = ring.read_origin()
    head_before = ring.read_head()
    step = "binding the key ring to this file"
    rebound = False
    try:
        if rebind:
            ring.rebind_store(store_file)
            rebound = True
        step = "the owner's open"
        store = Store.open(store_file, keys_dir=root, allow_rekey=True)
    except (ValueError, sqlite3.Error, OSError) as exc:
        if rebound:
            _restore_binding(ring, bound_before)
        report["status"] = FAILED
        report["detail"] = (
            f"{step} failed ({exc}); "
            + ("the binding was put back, and " if rebound else "")
            + "the anchor and head were not touched"
        )
        return report
    try:
        if not store.has_key:
            raise RuntimeError(store.key_state.summary)
        anchor = _first_held(store, store._key)
        report["origin"]["after"] = _origin_dict(anchor)
        try:
            bound = ring.read_binding()
        except OSError:
            bound = None
        report["binding"]["after"] = None if bound is None else str(bound)
        with store._transaction() as tx:
            row = store._append(
                tx,
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
                session=None,
            )
        report["seq"] = row.seq
    except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
        store.close()
        if rebound:
            _restore_binding(ring, bound_before)
        report["status"] = FAILED
        report["detail"] = (
            f"the acceptance was not recorded ({exc}); "
            + ("the binding was put back, and " if rebound else "")
            + "the anchor and head were not touched"
        )
        return report
    step = "setting the origin anchor"
    try:
        ring.replace_origin(anchor)
        step = "moving the head checkpoint to the acceptance row"
        stamp = isoformat_utc(moment.astimezone(timezone.utc))
        _set_head(ring, Head(seq=row.seq, mac=row.mac, ts=stamp))
    except OSError as exc:
        with contextlib.suppress(OSError):
            ring.replace_origin(origin_before)
        store.close()
        report["status"] = FAILED
        report["detail"] = (
            f"the acceptance is recorded at seq {row.seq}, and {step} failed "
            f"({exc}); the anchor and head are as they were: run `bettermemory "
            "log accept-history --yes` again to apply it"
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
        store._forget_segments()
        report["log"] = store.log_verify()["status"]
    except (ValueError, sqlite3.Error, OSError) as exc:
        report["detail"] = f"accepted and recorded at seq {report['seq']}; then {exc}"
    finally:
        store.close()
    return report


# ---------------------------------------------------------------------------
# Orphaned key rings (`bettermemory doctor --clear-orphan-keys`)
# ---------------------------------------------------------------------------

#: How a key ring in the keys directory stands with its store
#: (`ring_standing`). ``orphaned``: its store is shown gone (the bound
#: file's directory is there, can be read, holds other entries and none by
#: the file's name); `clear_orphan_rings` moves it to the quarantine
#: directory. ``in_use``: the bound file holds its store, a daemon serves
#: that file, or it is the ring of the store doctor checks. ``kept``: the
#: store is not shown gone (its directory is not there, is empty, cannot be
#: read or did not answer in `location.PROBE_BUDGET`; a file by its name is
#: there, holding another store, none, or one that cannot be read; the
#: binding cannot be read). ``unbound``: it names no store file. Only
#: ``orphaned`` rings are ever moved, and no ring file is ever deleted.
RING_ORPHANED = "orphaned"
RING_IN_USE = "in_use"
RING_KEPT = "kept"
RING_UNBOUND = "unbound"

#: The directory under the keys directory the clear moves a ring into, one
#: owner-only directory per store id, each with `QUARANTINE_NOTE`.
QUARANTINE_DIR = "quarantine"
QUARANTINE_NOTE = "note.txt"


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
    anchor and binding, a rotation's pending key, their lock sidecars and
    its restore record. The id is 32 hex digits (`KeyRing`), so it globs as
    itself."""
    return sorted(p for p in root.glob(f"{store_id}.*") if not p.is_dir())


def quarantine_path(root: Path, store_id: str) -> Path:
    """Where `clear_orphan_rings` moves the ring ``store_id`` of the keys
    directory ``root``."""
    return root / QUARANTINE_DIR / store_id


def _shown_gone(bound: Path, store_id: str) -> tuple[str, str]:
    """How the store file ``bound`` stands, read without writing: in use
    when it holds the store ``store_id``; orphaned only when it is shown
    gone (its directory is there, readable and not empty, and holds no
    entry by its name); kept otherwise, with why. OSError when it cannot be
    read to tell, which the caller keeps."""
    try:
        os.lstat(bound)
    except FileNotFoundError:
        pass
    else:
        held = _store_id_at(bound)
        if held == store_id:
            return RING_IN_USE, f"{bound} holds its store"
        if held is None:
            return RING_KEPT, (
                f"{bound} is there and does not read as a store (a damaged "
                "store keeps its key ring)"
            )
        return RING_KEPT, f"{bound} holds another store now ({held})"
    directory = bound.parent
    if not os.listdir(directory):
        return RING_KEPT, (
            f"{directory} is empty: a mount point whose volume is not mounted, "
            "or a folder emptied by hand, reads the same"
        )
    return (
        RING_ORPHANED,
        f"{bound} is gone: {directory} is there and holds no {bound.name}",
    )


def _probe(bound: Path, store_id: str) -> tuple[str, str]:
    """`_shown_gone` of ``bound``, bounded by `location.PROBE_BUDGET`
    (`location.bounded_read`, ENG-156): a hard-mounted share that does not
    answer keeps the ring rather than holding doctor."""
    from . import location

    budget = location.PROBE_BUDGET
    try:
        answer = location.bounded_read(
            str(bound), "orphan_keys", lambda: _shown_gone(bound, store_id), budget
        )
    except (FileNotFoundError, NotADirectoryError):
        return RING_KEPT, (
            f"{bound.parent} is not there: a store moved or deleted with its "
            "directory, a link on the way that points nowhere, or a volume not "
            "mounted reads the same"
        )
    except OSError as exc:
        return (
            RING_KEPT,
            f"{bound} cannot be read to tell whether the store is there: {exc}",
        )
    if answer is location.UNANSWERED:
        return RING_KEPT, f"{bound} did not answer in {budget:g} s"
    state, reason = answer
    return str(state), str(reason)


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
    the bound file and its directory (`_shown_gone`, bounded by
    `location.PROBE_BUDGET`) and the daemon lock of that file. ``keep``
    names store ids that are in use whatever the binding says."""
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
            RING_KEPT, f"its binding {ring.binding_path()} cannot be read: {exc}"
        )
    if bound is None:
        return standing(
            RING_UNBOUND,
            "it names no store file (a key ring from before 9.0.0rc13, or a "
            "binding that does not read)",
        )
    state, reason = _probe(bound, store_id)
    if state == RING_ORPHANED and _daemon_serves(state_dir, bound):
        return standing(RING_IN_USE, f"a bettermemory daemon serves {bound}", bound)
    return standing(state, reason, bound)


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
        rings kept because their store is not shown gone."""
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
                (self.of(RING_KEPT), "whose store is not shown gone"),
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
            "run `bettermemory doctor --clear-orphan-keys` for the plan, then with "
            f"`--yes` to move them to {self.keys_dir / QUARANTINE_DIR} (nothing is "
            "deleted; `bettermemory key import` takes a key back); a store moved "
            "by hand and not opened since reads as gone, so run `bettermemory key "
            "adopt --yes` with BETTERMEMORY_DIR naming its directory first"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys_dir": str(self.keys_dir),
            "rings": len(self.rings),
            "in_use": len(self.of(RING_IN_USE)),
            "orphaned": [ring.to_dict() for ring in self.of(RING_ORPHANED)],
            "kept": [ring.to_dict() for ring in self.of(RING_KEPT)],
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


def _quarantine_ring(
    root: Path,
    store_id: str,
    *,
    keep: frozenset[str],
    state_dir: Path,
    moment: datetime,
) -> tuple[RingStanding, list[str] | None]:
    """Read the ring ``store_id`` again under its binding's lock, as
    `log.KeyRing.bind_store` writes a binding, and move its files to
    `quarantine_path` when it still reads orphaned: an owner's open of a
    store moved to a new path in the meantime binds the ring first, or
    waits for this. A line naming the bound path, the reason and the way
    back goes into `QUARANTINE_NOTE` first; the binding moves last, so a
    run cut short leaves a ring that reads orphaned and moves on the next.
    A name already in the quarantine is never written over (OSError). The
    standing, and the names moved (None when it no longer read orphaned).
    The binding's lock sidecar follows after the lock is given back."""
    ring = KeyRing(root, store_id)
    binding = ring.binding_path()
    lock = binding.with_suffix(binding.suffix + ".lock")
    target = quarantine_path(root, store_id)
    with flock_excl(binding):
        standing = ring_standing(root, store_id, keep=keep, state_dir=state_dir)
        if standing.state != RING_ORPHANED:
            return standing, None
        files = [p for p in _ring_paths(root, store_id) if p not in (lock, binding)]
        files.append(binding)
        ensure_owner_only_dir(target, parents=True)
        taken = [p.name for p in files if os.path.lexists(target / p.name)]
        if taken:
            raise FileExistsError(
                f"{target} holds {', '.join(taken)} already: move them out of the "
                "quarantine, then run the clear again"
            )
        note = (
            f"{isoformat_utc(moment)} `bettermemory doctor --clear-orphan-keys "
            f"--yes` moved the key ring of store {store_id} here: it was bound to "
            f"{standing.bound_to}, and {standing.reason}. To take it back, run "
            "`bettermemory key import FILE` for each .key file here with "
            "BETTERMEMORY_DIR naming the store's directory.\n"
        )
        with open(target / QUARANTINE_NOTE, "a", encoding="utf-8") as handle:
            handle.write(note)
        moved: list[str] = []
        for path in files:
            replace_atomic(path, target / path.name)
            moved.append(path.name)
    with contextlib.suppress(OSError):
        if not os.path.lexists(target / lock.name):
            replace_atomic(lock, target / lock.name)
            moved.append(lock.name)
    return standing, moved


def clear_orphan_rings(
    store_file: Path | None,
    *,
    apply: bool,
    keys_dir: Path | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """`bettermemory doctor --clear-orphan-keys`: move every key ring in the
    keys directory whose store is shown gone (`survey_rings`,
    `RING_ORPHANED`) with all its files (keys, head, anchor, binding, lock
    sidecars and restore record) to `quarantine_path`, owner-only, beside a
    note naming the bound path and the reason; `key import` takes a key
    back from there. Nothing is deleted. A ring whose store is not shown
    gone, that names no store file, or that a daemon or the store at
    ``store_file`` holds is never moved; each orphaned one is read again
    under its binding's lock first (`_quarantine_ring`). Without ``apply``,
    the plan (``planned``) and nothing moved. What was moved (``moved``,
    with where), what read otherwise the second time (``kept``) and what
    could not be moved (``failed``, with the error)."""
    survey = survey_rings(store_file, keys_dir=keys_dir, state_dir=state_dir)
    state = _state_root(state_dir)
    moment = now if now is not None else datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "keys_dir": str(survey.keys_dir),
        "quarantine": str(survey.keys_dir / QUARANTINE_DIR),
        "status": DONE if apply else PLANNED,
        "planned": [
            {
                **ring.to_dict(),
                "to": str(quarantine_path(survey.keys_dir, ring.store_id)),
            }
            for ring in survey.of(RING_ORPHANED)
        ],
        "moved": [],
        "kept": [],
        "failed": [],
    }
    if not apply:
        return report
    for first in survey.of(RING_ORPHANED):
        try:
            standing, moved = _quarantine_ring(
                survey.keys_dir,
                first.store_id,
                keep=survey.keep,
                state_dir=state,
                moment=moment,
            )
        except OSError as exc:
            report["failed"].append(
                {"store_id": first.store_id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if moved is None:
            report["kept"].append(standing.to_dict())
        else:
            target = quarantine_path(survey.keys_dir, first.store_id)
            report["moved"].append(
                {**standing.to_dict(), "files": moved, "to": str(target)}
            )
    return report


__all__ = [
    "DONE",
    "EXIT_CODES",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "FAILED",
    "PLANNED",
    "QUARANTINE_DIR",
    "QUARANTINE_NOTE",
    "REFUSED",
    "RING_IN_USE",
    "RING_KEPT",
    "RING_ORPHANED",
    "RING_UNBOUND",
    "UNCHANGED",
    "RingStanding",
    "RingSurvey",
    "accept_history",
    "adopt_ring",
    "clear_orphan_rings",
    "daemon_locks",
    "describe",
    "export_exposure",
    "export_keys",
    "import_keys",
    "keys_root",
    "quarantine_path",
    "read_key_file",
    "ring_standing",
    "rotate_key",
    "survey_rings",
]
