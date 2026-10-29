"""The stores a hook refuses before it starts a daemon, read with the
standard library alone.

Every writing open of the store refuses a store inside a backup bundle
(`backup.bundle_refusal`) and a store a restore holds or did not finish
(`restore_lock.restore_state`). A hook refuses them too, before it starts
a daemon that would only exit on the store; and since the hook path
loads no module of the package past the client's few (`_daemon_client`),
this module reads the same files the same way with the standard
library. `tests/test_hook_client.py` holds each constant, reading and
refusal equal to the module it copies. `_daemon_client.hook_main` calls
`store_refusal`; the module lives apart from the client to keep each
under the package's 2,000-line bound (ENG-96), and every hook loads it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

#: `backup.MANIFEST_NAME`, `backup.BUNDLE_FORMAT` and the cap
#: `backup._may_be_bundle_manifest` reads a manifest with, spelled here
#: because the hook path imports no module of the package past the
#: client's few; tests/test_hook_client.py pins each equal to its source.
BUNDLE_MANIFEST_NAME = "manifest.json"
BUNDLE_FORMAT = "bettermemory.backup"
BUNDLE_MANIFEST_CAP = 1 << 20
#: `restore_lock.LOCK_SUFFIX`, the cap its marker reader reads with, and
#: the byte a Windows restore locks, likewise.
RESTORE_LOCK_SUFFIX = ".restore-lock"
RESTORE_MARKER_CAP = 1 << 16
RESTORE_LOCK_BYTE = 1 << 20
#: `restore_lock._RETRY_ERRNOS`: flock's errnos for a lock another holds.
_LOCK_HELD_ERRNOS = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EINTR})
#: `history._UNPRINTABLE`: the categories `printable` writes as escapes.
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def printable(text: str) -> str:
    """`history.printable`: control, format and separator characters and
    lone surrogates written as their Python escape, a tab kept, so a line
    stays one line."""
    import unicodedata

    return "".join(
        ch.encode("unicode_escape").decode("ascii")
        if ch != "\t" and unicodedata.category(ch) in _UNPRINTABLE
        else ch
        for ch in text
    )


def _may_be_bundle_manifest(manifest: Path) -> bool:
    """`backup._may_be_bundle_manifest`: anything but a JSON document that
    is not an object naming `BUNDLE_FORMAT`, a file that is not a regular
    one, is past the cap or does not read or parse counting."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(manifest, flags)
        with open(fd, "rb", closefd=True) as fh:
            found = os.fstat(fh.fileno())
            if not stat.S_ISREG(found.st_mode) or found.st_size > BUNDLE_MANIFEST_CAP:
                return True
            document = json.loads(fh.read(BUNDLE_MANIFEST_CAP))
    except (OSError, ValueError, RecursionError):
        return True
    return isinstance(document, dict) and document.get("format") == BUNDLE_FORMAT


def bundle_refusal(path: Path) -> str | None:
    """`backup.bundle_refusal(path)` for the store the configuration
    resolves: a manifest that may be a bundle's beside the file, as
    spelled or resolved, or a file with other names (hard links)."""
    candidates = [path]
    with contextlib.suppress(OSError, RuntimeError):
        candidates.append(path.resolve())
    for candidate in candidates:
        manifest = candidate.parent / BUNDLE_MANIFEST_NAME
        try:
            if manifest.is_file() and _may_be_bundle_manifest(manifest):
                return (
                    f"{path} is in a backup bundle ({manifest} is beside it), and "
                    "opening it would change it and fail the bundle's hash: copy "
                    f"{path.name} out of the bundle to a directory of its own first "
                    "and point the command at the copy (`bettermemory backup verify` "
                    "checks a bundle where it is)"
                )
        except OSError:
            continue
    try:
        linked = path.stat().st_nlink > 1
    except OSError:
        linked = False
    if not linked:
        return None
    return (
        f"{path} has other names (hard links), and opening it would change "
        "the file under each of them, a backup bundle's snapshot among them: "
        "restore it in place from a bundle (`bettermemory backup restore "
        f"BUNDLE --yes`, which gives {path.name} a file of its own), or copy "
        f"{path.name} to a directory of its own first, point BETTERMEMORY_DIR or "
        "`[storage] directory` at that directory and run `bettermemory key "
        "adopt --yes` there (until then the key ring stays bound to this path, "
        "and the copy opens for reading only)"
    )


def _restore_marker(fd: int) -> dict[str, Any] | None:
    """`restore_lock._read_marker`: None for an empty record, the record
    when it reads as one, an empty dict for anything else there."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, RESTORE_MARKER_CAP)
    except OSError as exc:
        return {"unreadable": str(exc)}
    if not data.strip():
        return None
    try:
        document = json.loads(data)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _restore_marker_at(record: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(record, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        return _restore_marker(fd)
    finally:
        os.close(fd)


def _record_lock_held(fd: int) -> bool:
    """Whether another holds the lock on the record file at `fd`, taken
    and given back at once (`restore_lock._try_lock`); a file ``flock``
    cannot lock reads as not held, as the restore lock takes it."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, RESTORE_LOCK_BYTE, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        os.lseek(fd, RESTORE_LOCK_BYTE, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        return exc.errno in _LOCK_HELD_ERRNOS
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def restore_state(store_path: Path) -> tuple[str, dict[str, Any] | None]:
    """`restore_lock.restore_state(store_path)` for a process that holds no
    restore lock: ``("running", record)`` while a restore holds the lock,
    ``("unfinished", record)`` when none does and a restore that did not
    finish left its record, ``("clear", None)`` otherwise. The lock is the
    store's directory under ``flock``, or the record file on Windows and
    where ``flock`` cannot lock the directory."""
    record = store_path.with_name(store_path.name + RESTORE_LOCK_SUFFIX)
    if sys.platform != "win32":
        import fcntl

        try:
            fd = os.open(store_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return "clear", None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _LOCK_HELD_ERRNOS:
                return "running", _restore_marker_at(record)
        else:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            marker = _restore_marker_at(record)
            return ("clear", None) if marker is None else ("unfinished", marker)
        finally:
            os.close(fd)
    try:
        fd = os.open(record, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return "clear", None
    try:
        held = _record_lock_held(fd)
        marker = _restore_marker(fd)
    finally:
        os.close(fd)
    if held:
        return "running", marker
    return ("clear", None) if marker is None else ("unfinished", marker)


def _describe_restore(marker: dict[str, Any]) -> str:
    """`restore_lock.describe`: what a restore's record says."""
    parts: list[str] = []
    started = marker.get("started_at")
    pid = marker.get("pid")
    if isinstance(started, str):
        parts.append(f"started {started}")
    if isinstance(pid, int) and not isinstance(pid, bool):
        parts.append(f"by process {pid}")
    bundle = marker.get("bundle")
    if isinstance(bundle, str):
        parts.append(f"from {bundle}")
    stage = marker.get("stage")
    if isinstance(stage, str):
        parts.append(f"stopped at {stage}")
    return printable(", ".join(parts) if parts else "no record of it reads")


def restore_refusal(store_path: Path) -> str | None:
    """The refusal a writing open of the store gets from its restore lock
    (`restore_lock.RestoreLockedError`), read without opening the store or
    waiting (`restore_state`), or None."""
    state, marker = restore_state(store_path)
    if state == "clear":
        return None
    record = store_path.with_name(store_path.name + RESTORE_LOCK_SUFFIX)
    if state == "running":
        why = (
            f"a restore of it is running (its record is {record}); open it "
            "again when the restore has finished"
        )
    else:
        bundle = (marker or {}).get("bundle")
        again = printable(
            f"`bettermemory backup restore {bundle} --yes`"
            if isinstance(bundle, str)
            else "`bettermemory backup restore BUNDLE --yes`"
        )
        why = (
            f"a restore of it did not finish ({_describe_restore(marker or {})}; "
            f"{record} keeps its record), and the store may be part restored. "
            f"Run the restore again to finish it ({again}), or open the "
            "store as it stands after `bettermemory backup unlock --yes`"
        )
    return f"{store_path} is not opened: {why}"


def store_refusal(store_path: Path) -> str | None:
    """Why a hook must not start a daemon for `store_path`, whose open would
    refuse it: a store inside a backup bundle (`bundle_refusal`), or one a
    restore holds or did not finish (`restore_refusal`). None otherwise."""
    return bundle_refusal(store_path) or restore_refusal(store_path)


__all__ = [
    "BUNDLE_FORMAT",
    "BUNDLE_MANIFEST_CAP",
    "BUNDLE_MANIFEST_NAME",
    "RESTORE_LOCK_BYTE",
    "RESTORE_LOCK_SUFFIX",
    "RESTORE_MARKER_CAP",
    "bundle_refusal",
    "printable",
    "restore_refusal",
    "restore_state",
    "store_refusal",
]
