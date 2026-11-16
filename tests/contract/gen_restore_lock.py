"""The reader's side of the restore lock (`restore_lock.reading`), as the
native read-only open ports it: what a read meets while a restore holds
the lock (a helper holding the store's directory exclusively, as
`restore_lock.acquire` does), with a restore's marker left and no lock
held, and with neither. Each case records whether the read was refused,
the error class and its text (the store path spelled ``<store>``), the
exit status the read-only commands give it, and the names in the store's
directory before and after (a read makes nothing).

Then the record mode (a POSIX directory ``flock`` cannot lock, forced by
putting the directory in ``_RECORD_MODE_DIRS`` as `_take` does when
``flock`` fails): no record file, one held exclusively, one free. Then
the path as given (the lock is on the parent of the path, unresolved): a
relative path, one through ``..``, a symbolic link to the store file in
another directory, and a symbolic link to the store's directory, each
read while a helper holds the real directory. Then a restore meeting a
read that holds the lock: `backup._take_restore_lock` with `acquire`'s
wait cut short, and its detail. POSIX only (Windows has no case)."""

from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from bettermemory import backup, restore_lock

from ._sandbox import world

_MARKER = {
    "format": restore_lock.MARKER_FORMAT,
    "pid": 1,
    "started_at": "2026-01-01T00:00:00Z",
    "step": "swap",
}


def _case(store_dir: Path, name: str, *, held: bool, marker: bool) -> dict[str, Any]:
    # POSIX only (`generate.POSIX_ONLY`), so loaded here and not at the
    # top, and as Any: on Windows a type checker's `fcntl` has no flock
    # (the main CI job's mypy step failed there on a plain import).
    fcntl: Any = importlib.import_module("fcntl")

    store_dir.mkdir()
    store = store_dir / "memory.sqlite"
    store.write_bytes(b"")
    if marker:
        restore_lock.lock_path(store).write_text(
            json.dumps(_MARKER, sort_keys=True) + "\n", encoding="utf-8"
        )
    helper = os.open(store_dir, os.O_RDONLY)
    try:
        if held:
            fcntl.flock(helper, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = sorted(p.name for p in store_dir.iterdir())
        saved = restore_lock.OPEN_WAIT_SECONDS
        restore_lock.OPEN_WAIT_SECONDS = 0.2
        try:
            hold = restore_lock.reading(store)
        except restore_lock.RestoreRunningError as exc:
            outcome: dict[str, Any] = {
                "refused": True,
                "error": type(exc).__name__,
                "message": str(exc).replace(str(store), "<store>"),
                "status": restore_lock.RESTORE_RUNNING_STATUS,
            }
        else:
            outcome = {"refused": False, "holds": hold is not None}
            if hold is not None:
                # While the read holds it, a restore cannot take it.
                try:
                    fcntl.flock(helper, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    outcome["restore_kept_off"] = True
                else:
                    outcome["restore_kept_off"] = False
                    fcntl.flock(helper, fcntl.LOCK_UN)
                hold.close()
        finally:
            restore_lock.OPEN_WAIT_SECONDS = saved
        after = sorted(p.name for p in store_dir.iterdir())
    finally:
        os.close(helper)
    return {
        "name": name,
        "held": held,
        "marker": marker,
        **outcome,
        "names_before": before,
        "names_after": after,
    }


def _try_reading(path: Path) -> dict[str, Any]:
    saved = restore_lock.OPEN_WAIT_SECONDS
    restore_lock.OPEN_WAIT_SECONDS = 0.2
    try:
        hold = restore_lock.reading(path)
    except restore_lock.RestoreRunningError as exc:
        return {"refused": True, "error": type(exc).__name__}
    finally:
        restore_lock.OPEN_WAIT_SECONDS = saved
    if hold is not None:
        hold.close()
    return {"refused": False, "holds": hold is not None}


def _record_case(root: Path, name: str, *, record: bool, held: bool) -> dict[str, Any]:
    # POSIX only (`generate.POSIX_ONLY`), so loaded here and not at the
    # top, and as Any: on Windows a type checker's `fcntl` has no flock
    # (the main CI job's mypy step failed there on a plain import).
    fcntl: Any = importlib.import_module("fcntl")

    store_dir = root / name
    store_dir.mkdir()
    store = store_dir / "memory.sqlite"
    store.write_bytes(b"")
    lock = restore_lock.lock_path(store)
    if record:
        lock.write_text(json.dumps(_MARKER, sort_keys=True) + "\n", encoding="utf-8")
    key = (os.stat(store_dir).st_dev, os.stat(store_dir).st_ino)
    restore_lock._RECORD_MODE_DIRS.add(key)
    helper = os.open(lock, os.O_RDONLY) if record else None
    try:
        if held and helper is not None:
            fcntl.flock(helper, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = sorted(p.name for p in store_dir.iterdir())
        outcome = _try_reading(store)
        after = sorted(p.name for p in store_dir.iterdir())
    finally:
        restore_lock._RECORD_MODE_DIRS.discard(key)
        if helper is not None:
            os.close(helper)
    return {
        "name": name,
        "record": record,
        "held": held,
        **outcome,
        "names_before": before,
        "names_after": after,
    }


#: Each path case: its name, the layout under the case root, and the path
#: read, relative to the case root (the read runs there).
_PATH_CASES = (
    ("relative", "real/memory.sqlite"),
    ("dotdot", "other/../real/memory.sqlite"),
    ("symlinked_file", "linkdir/memory.sqlite"),
    ("symlinked_dir", "ldir/memory.sqlite"),
)


def _path_case(root: Path, name: str, given: str) -> dict[str, Any]:
    # POSIX only (`generate.POSIX_ONLY`), so loaded here and not at the
    # top, and as Any: on Windows a type checker's `fcntl` has no flock
    # (the main CI job's mypy step failed there on a plain import).
    fcntl: Any = importlib.import_module("fcntl")

    case = root / name
    real = case / "real"
    real.mkdir(parents=True)
    (real / "memory.sqlite").write_bytes(b"")
    (case / "other").mkdir()
    (case / "linkdir").mkdir()
    (case / "linkdir" / "memory.sqlite").symlink_to(real / "memory.sqlite")
    (case / "ldir").symlink_to(real, target_is_directory=True)
    helper = os.open(real, os.O_RDONLY)
    saved = os.getcwd()
    try:
        fcntl.flock(helper, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.chdir(case)
        outcome = _try_reading(Path(given))
    finally:
        os.chdir(saved)
        os.close(helper)
    return {"name": name, "path": given, "real_dir_held": True, **outcome}


def _restore_meets_reader(root: Path) -> dict[str, Any]:
    store_dir = root / "restore_meets_reader"
    store_dir.mkdir(mode=stat.S_IRWXU)
    store = store_dir / "memory.sqlite"
    store.write_bytes(b"")
    hold = restore_lock.reading(store)
    original = restore_lock.acquire
    setattr(restore_lock, "acquire", lambda s, wait=0.2: original(s, wait=wait))
    report: dict[str, Any] = {}
    try:
        held = backup._take_restore_lock(store, Path("bundle.tar"), report)
    finally:
        setattr(restore_lock, "acquire", original)
        if hold is not None:
            hold.close()
    lock = restore_lock.lock_path(store)
    detail = str(report.get("detail", ""))
    return {
        "name": "restore_meets_reader",
        "reader_holds": hold is not None,
        "restore_took_lock": held is not None,
        "detail": detail.replace(str(lock), "<lock>").replace(str(store), "<store>"),
        "names_after": sorted(p.name for p in store_dir.iterdir()),
    }


def generate() -> dict[str, bytes]:
    with world() as w:
        cases = [
            _case(w.root / "running", "running", held=True, marker=True),
            _case(
                w.root / "running_no_marker",
                "running_no_marker",
                held=True,
                marker=False,
            ),
            _case(w.root / "unfinished", "unfinished", held=False, marker=True),
            _case(w.root / "clear", "clear", held=False, marker=False),
        ]
        record = [
            _record_case(w.root, "record_absent", record=False, held=False),
            _record_case(w.root, "record_held", record=True, held=True),
            _record_case(w.root, "record_free", record=True, held=False),
        ]
        paths = [_path_case(w.root / "paths", n, g) for n, g in _PATH_CASES]
        restore = _restore_meets_reader(w.root)
    document = {
        "lock_suffix": restore_lock.LOCK_SUFFIX,
        "open_wait_seconds": restore_lock.OPEN_WAIT_SECONDS,
        "poll_seconds": restore_lock._POLL_SECONDS,
        "restore_running_status": restore_lock.RESTORE_RUNNING_STATUS,
        "cases": cases,
        "record_mode": record,
        "paths": paths,
        "restore_meets_reader": restore,
    }
    return {
        "store_restore_lock.json": (
            json.dumps(document, indent=1, sort_keys=True) + "\n"
        ).encode("utf-8")
    }
