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
wait cut short, and its detail.

Then the writing open's side (`restore_lock.admit`, its `confirm`, and
`restore_state`): a free directory, one a helper process holds shared or
exclusively, markers of every shape `_read_marker` and `describe` tell
apart (each written as hex pieces, ``[hex, count]``), an unreadable
marker (a directory at the lock path), the store's directory missing,
and the record mode, where `confirm` also meets a store file swapped
after `admit` and a marker left after it. Messages spell the store path
``<store>`` and the lock ``<lock>``; an unreadable marker's text (the C
library's) is not recorded, only its key. POSIX only (Windows has no
case)."""

from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import sys
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


def _j(value: Any) -> list[list[Any]]:
    return [[json.dumps(value, sort_keys=True).encode("utf-8").hex(), 1]]


#: Each marker case: its name and the marker's bytes as ``[hex, count]``
#: pieces (None: no marker file).
_MARKERS: tuple[tuple[str, list[list[Any]] | None], ...] = (
    ("none", None),
    ("empty", [["", 1]]),
    ("whitespace", [[b" \t\n\r\x0b\x0c".hex(), 1]]),
    ("vt_only", [["0b", 3]]),
    ("nbsp_not_space", [["c2a0", 1]]),
    (
        "full",
        _j(
            {
                "started_at": "2026-01-01T00:00:00Z",
                "pid": 42,
                "bundle": "b.tar",
                "stage": "swap",
            }
        ),
    ),
    ("pid_only", _j({"pid": 7})),
    ("pid_bool", _j({"pid": True})),
    ("pid_float", _j({"pid": 7.0})),
    ("pid_big", [[b'{"pid": 123456789012345678901234567890}'.hex(), 1]]),
    ("wrong_types", _j({"started_at": 1, "bundle": ["x"], "stage": None, "pid": "7"})),
    ("unprintable", _j({"bundle": "a\nb\u200b\u2028", "stage": "x\ty\x7f"})),
    ("lone_surrogate", [[b'{"stage": "\\ud800"}'.hex(), 1]]),
    ("utf8_surrogate", [[b'{"stage": "\xed\xa0\x80"}'.hex(), 1]]),
    ("dup_keys", [[b'{"stage": "a", "stage": "b"}'.hex(), 1]]),
    ("empty_object", _j({})),
    ("list", _j([1])),
    ("string", _j("x")),
    ("number", _j(5)),
    ("null", _j(None)),
    ("truncated", [[b'{"pid": 4'.hex(), 1]]),
    ("vt_around_json", [[b'\x0b{"pid": 1}'.hex(), 1]]),
    ("bad_utf8", [[b'{"stage": "\xff"}'.hex(), 1]]),
    ("utf16_le_no_bom", [['{"stage": "le"}'.encode("utf-16-le").hex(), 1]]),
    ("utf16_bom_le_mark", [["fffe" + '{"stage": "le"}'.encode("utf-16-le").hex(), 1]]),
    ("utf16_bom_be", [["feff" + '{"stage": "be"}'.encode("utf-16-be").hex(), 1]]),
    ("utf16_be_no_bom", [['{"stage": "be"}'.encode("utf-16-be").hex(), 1]]),
    ("utf32_le_no_bom", [['{"stage": "32"}'.encode("utf-32-le").hex(), 1]]),
    ("utf32_be_no_bom", [['{"stage": "32"}'.encode("utf-32-be").hex(), 1]]),
    ("utf32_bom", [["fffe0000" + '{"stage": "32"}'.encode("utf-32-le").hex(), 1]]),
    ("utf16_odd", [['{"stage": "x"}'.encode("utf-16-le").hex() + "00", 1]]),
    ("utf8_sig", [["efbbbf" + b'{"stage": "sig"}'.hex(), 1]]),
    ("utf8_sig_twice", [["efbbbfefbbbf" + b'{"stage": "sig"}'.hex(), 1]]),
    ("past_cap_blank", [["20", 1 << 16], [b'{"pid": 1}'.hex(), 1]]),
    ("past_cap_cut", [[b'{"stage": "x"'.hex(), 1], ["20", 1 << 16], ["7d", 1]]),
    ("at_cap", [[b'{"stage": "x"'.hex(), 1], ["20", (1 << 16) - 14], ["7d", 1]]),
    ("deep", [["5b", 70000]]),
)


def _marker_bytes(pieces: list[list[Any]]) -> bytes:
    return b"".join(bytes.fromhex(h) * n for h, n in pieces)


_HELPER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
    "fcntl.flock(fd, fcntl.LOCK_SH if sys.argv[2] == 'shared' else fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


class _Helper:
    """A process holding `path` locked shared or exclusively until closed."""

    def __init__(self, path: Path, mode: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HELPER, str(path), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        assert self.proc.stdout.readline().strip() == "held"

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=30)


def _refusal(exc: BaseException, store: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"error": type(exc).__name__}
    if isinstance(exc, restore_lock.RestoreLockedError):
        lock = restore_lock.lock_path(store)
        out["message"] = (
            str(exc).replace(str(lock), "<lock>").replace(str(store), "<store>")
        )
        out["running"] = exc.running
        out["marker_keys"] = None if exc.marker is None else sorted(exc.marker)
    return out


def _state(store: Path) -> dict[str, Any]:
    try:
        state, record = restore_lock.restore_state(store, wait=0.2)
    except RecursionError as exc:
        return {"error": type(exc).__name__}
    return {
        "state": state,
        "marker_keys": None if record is None else sorted(record),
        "described": None if record is None else restore_lock.describe(record),
    }


def _admit_case(
    root: Path,
    name: str,
    marker: list[list[Any]] | None,
    *,
    helper: str | None = None,
    record_mode: bool = False,
    lock_dir: bool = False,
    missing_dir: bool = False,
    swap: bool = False,
    marker_after: bool = False,
) -> dict[str, Any]:
    case = root / name
    store_dir = case / "store"
    store = store_dir / "memory.sqlite"
    lock = restore_lock.lock_path(store)
    case.mkdir(parents=True)
    if not missing_dir:
        store_dir.mkdir()
        store.write_bytes(b"")
    if marker is not None:
        lock.write_bytes(_marker_bytes(marker))
    if lock_dir:
        lock.mkdir()
    key = None
    if record_mode:
        found = os.stat(store_dir)
        key = (found.st_dev, found.st_ino)
        restore_lock._RECORD_MODE_DIRS.add(key)
    held = None
    if helper is not None:
        held = _Helper(lock if record_mode else store_dir, helper)
    saved = restore_lock.OPEN_WAIT_SECONDS
    restore_lock.OPEN_WAIT_SECONDS = 0.2
    row: dict[str, Any] = {"name": name}
    try:
        try:
            hold = restore_lock.admit(store)
        except (restore_lock.RestoreLockedError, RecursionError) as exc:
            row["admit"] = {"refused": True, **_refusal(exc, store)}
        else:
            kind = (
                "none"
                if hold is None
                else "shared"
                if isinstance(hold, restore_lock._SharedHold)
                else "record"
            )
            row["admit"] = {"refused": False, "admission": kind}
            if swap:
                fresh = store_dir / "fresh.sqlite"
                fresh.write_bytes(b"")
                os.replace(fresh, store)
            if marker_after:
                lock.write_bytes(_marker_bytes(_j({"stage": "late"})))
            if hold is not None:
                try:
                    hold.confirm()
                except restore_lock.RestoreLockedError as exc:
                    row["confirm"] = {"refused": True, **_refusal(exc, store)}
                else:
                    row["confirm"] = {"refused": False}
                hold.close()
        row["restore_state"] = _state(store)
    finally:
        restore_lock.OPEN_WAIT_SECONDS = saved
        if held is not None:
            held.close()
        if key is not None:
            restore_lock._RECORD_MODE_DIRS.discard(key)
    return {
        "marker": marker,
        "helper": helper,
        "record_mode": record_mode,
        "lock_dir": lock_dir,
        "missing_dir": missing_dir,
        "swap": swap,
        "marker_after": marker_after,
        **row,
    }


def _admit_cases(root: Path) -> list[dict[str, Any]]:
    full = dict(_MARKERS)["full"]
    rows = [_admit_case(root / "markers", n, m) for n, m in _MARKERS]
    rows += [
        _admit_case(root, "held_shared", None, helper="shared"),
        _admit_case(root, "held_shared_marker", full, helper="shared"),
        _admit_case(root, "held_exclusive", None, helper="exclusive"),
        _admit_case(root, "held_exclusive_marker", full, helper="exclusive"),
        _admit_case(root, "unreadable", None, lock_dir=True),
        _admit_case(root, "missing_dir", None, missing_dir=True),
        _admit_case(root, "record_absent", None, record_mode=True),
        _admit_case(root, "record_empty", [["", 1]], record_mode=True),
        _admit_case(root, "record_marker", full, record_mode=True),
        _admit_case(
            root, "record_held", [["", 1]], record_mode=True, helper="exclusive"
        ),
        _admit_case(
            root, "record_held_shared", [["", 1]], record_mode=True, helper="shared"
        ),
        _admit_case(root, "record_lock_dir", None, record_mode=True, lock_dir=True),
        _admit_case(root, "record_swapped", None, record_mode=True, swap=True),
        _admit_case(
            root, "record_empty_swapped", [["", 1]], record_mode=True, swap=True
        ),
        _admit_case(
            root, "record_marker_after", None, record_mode=True, marker_after=True
        ),
        _admit_case(root, "dir_swapped", None, swap=True),
        _admit_case(root, "dir_marker_after", None, marker_after=True),
    ]
    return rows


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
        admit = _admit_cases(w.root / "admit")
    document = {
        "lock_suffix": restore_lock.LOCK_SUFFIX,
        "open_wait_seconds": restore_lock.OPEN_WAIT_SECONDS,
        "poll_seconds": restore_lock._POLL_SECONDS,
        "restore_running_status": restore_lock.RESTORE_RUNNING_STATUS,
        "cases": cases,
        "record_mode": record,
        "paths": paths,
        "restore_meets_reader": restore,
        "admit": admit,
    }
    return {
        "store_restore_lock.json": (
            json.dumps(document, indent=1, sort_keys=True) + "\n"
        ).encode("utf-8")
    }
