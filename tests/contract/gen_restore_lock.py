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
)

#: The marker nested 70,000 lists deep. Whether `json.loads` raises
#: RecursionError there is the interpreter's and the system's (3.14
#: measures the C stack), so its row is in ``store_restore_lock_python.json``.
_DEEP: tuple[str, list[list[Any]]] = ("deep", [["5b", 70000]])


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


# ---------------------------------------------------------------------------
# A restore's own hold (`acquire`, `RestoreLock.mark` and `release`,
# `held_here`), `probe`, `unlock` and `state_finding`.
# ---------------------------------------------------------------------------

_HOLD_WAIT = 0.2

#: A restore that was killed left this marker (a pid no test runs as).
_KILLED = {
    "bundle": "old\nbundle.tar",
    "format": restore_lock.MARKER_FORMAT,
    "pid": 1,
    "stage": "swap",
    "started_at": "2026-01-01T00:00:00Z",
}


class _Scrub:
    """Spells the case's paths, this process's pid and the clock's time as
    placeholders: ``<lock>``, ``<store>``, ``<pid>``, ``<started_at>``."""

    def __init__(self, store: Path) -> None:
        self.store = str(store)
        self.lock = str(restore_lock.lock_path(store))
        self.pid = os.getpid()
        self.started: str | None = None

    def text(self, value: str) -> str:
        out = value.replace(self.lock, "<lock>").replace(self.store, "<store>")
        out = out.replace(f'"pid": {self.pid}', '"pid": "<pid>"')
        out = out.replace(f"by process {self.pid}", "by process <pid>")
        if self.started is not None:
            out = out.replace(self.started, "<started_at>")
        return out

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return self.marker(value)
        return value

    def marker(self, marker: dict[str, Any] | None) -> Any:
        if marker is None:
            return None
        out: dict[str, Any] = {}
        for key, value in marker.items():
            if key == "unreadable":
                out[key] = "<unreadable>"
            elif key == "pid" and value == self.pid and type(value) is int:
                out[key] = "<pid>"
            else:
                out[key] = self.value(value)
        return out


def _error(exc: BaseException) -> dict[str, Any]:
    return {"error": type(exc).__name__}


def _in_thread(call: Any) -> Any:
    import threading

    box: list[Any] = []
    thread = threading.Thread(target=lambda: box.append(call()))
    thread.start()
    thread.join()
    return box[0]


def _finding(store: Path, scrub: _Scrub) -> Any:
    try:
        state, summary, remedy, details = restore_lock.state_finding(store)
    except RecursionError as exc:
        return _error(exc)
    return [state, scrub.text(summary), remedy, scrub.marker(details)]


def _held_here(store: Path) -> Any:
    try:
        return restore_lock.held_here(store)
    except OSError as exc:
        return _error(exc)


def _hold_op(op: list[Any], store: Path, scrub: _Scrub, box: dict[str, Any]) -> Any:
    kind = op[0]
    lock = restore_lock.lock_path(store)
    if kind in ("acquire", "acquire_again"):
        try:
            held = restore_lock.acquire(store, wait=_HOLD_WAIT)
        except (OSError, RecursionError) as exc:
            return _error(exc)
        if held is None:
            return {"held": False}
        if kind == "acquire_again":
            held.release(clear=False)
            return {"held": True}
        box["lock"] = held
        return {
            "held": True,
            "previous": scrub.marker(held.previous),
            "record": held.record,
            "mode": oct(os.stat(lock).st_mode & 0o777),
        }
    if kind == "mark":
        box["lock"].mark(**op[1])
        data = lock.read_bytes().decode("ascii")
        scrub.started = json.loads(data)["started_at"]
        return scrub.text(data)
    if kind == "release":
        if "lock" not in box:
            return None
        box.pop("lock").release(clear=op[1])
        return sorted(p.name for p in store.parent.iterdir())
    if kind == "held_here":
        return _held_here(store)
    if kind == "held_here_thread":
        return _in_thread(lambda: _held_here(store))
    if kind == "state_finding":
        return _finding(store, scrub)
    if kind == "state_finding_thread":
        return _in_thread(lambda: _finding(store, scrub))
    if kind == "unlock":
        try:
            report = restore_lock.unlock(store, apply=op[1])
        except (OSError, RecursionError) as exc:
            return _error(exc)
        return {k: scrub.value(v) for k, v in report.items()}
    if kind == "close_helper":
        box.pop("helper").close()
        return None
    assert kind == "names", kind
    if not store.parent.exists():
        return None
    return sorted(p.name for p in store.parent.iterdir())


_LIFE: list[list[Any]] = [
    ["acquire"],
    ["held_here"],
    ["held_here_thread"],
    ["state_finding"],
    ["state_finding_thread"],
    ["acquire_again"],
    ["unlock", True],
    ["mark", {"stage": "swap", "bundle": "b.tar"}],
    ["mark", {"stage": "verify", "note": "é ", "count": 3}],
    ["state_finding_thread"],
    ["release", False],
    ["state_finding"],
    ["held_here"],
    ["unlock", False],
    ["unlock", True],
    ["names"],
    ["unlock", True],
]


def _hold_case(
    root: Path,
    name: str,
    ops: list[list[Any]],
    *,
    marker: list[list[Any]] | None = None,
    helper: str | None = None,
    record_mode: bool = False,
    lock_dir: bool = False,
    missing_dir: bool = False,
    dotted: bool = False,
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
    box: dict[str, Any] = {}
    if helper is not None:
        box["helper"] = _Helper(lock if record_mode else store_dir, helper)
    given = Path(f"{store_dir}/./{store.name}") if dotted else store
    scrub = _Scrub(store)
    saved = restore_lock.OPEN_WAIT_SECONDS
    restore_lock.OPEN_WAIT_SECONDS = _HOLD_WAIT
    results: list[Any] = []
    try:
        for op in ops:
            results.append(_hold_op(op, given, scrub, box))
    finally:
        restore_lock.OPEN_WAIT_SECONDS = saved
        if "lock" in box:
            box["lock"].release(clear=False)
        if "helper" in box:
            box["helper"].close()
        if key is not None:
            restore_lock._RECORD_MODE_DIRS.discard(key)
    return {
        "name": name,
        "marker": marker,
        "helper": helper,
        "record_mode": record_mode,
        "lock_dir": lock_dir,
        "missing_dir": missing_dir,
        "dotted": dotted,
        "ops": ops,
        "results": results,
    }


def _full(*extra: str) -> list[list[Any]]:
    ops: list[list[Any]] = [["state_finding"], ["held_here"], ["acquire"]]
    ops += [["release", False]]
    for e in extra:
        ops += [[e], ["release", False]] if e == "acquire" else [[e]]
    return ops + [["unlock", False], ["unlock", True], ["names"]]


def _hold_cases(root: Path) -> list[dict[str, Any]]:
    killed = _j(_KILLED)
    bundle_list = _j(dict(_KILLED, bundle=["x"]))
    return [
        _hold_case(root, "life", _LIFE),
        _hold_case(root, "life_record", _LIFE, record_mode=True),
        _hold_case(root, "life_dotted", _LIFE, dotted=True),
        _hold_case(root, "killed", _full("state_finding_thread"), marker=killed),
        _hold_case(
            root,
            "killed_take_over",
            [
                ["acquire"],
                ["mark", {"stage": "again"}],
                ["release", True],
                ["names"],
                ["state_finding"],
            ],
            marker=killed,
        ),
        _hold_case(root, "killed_record", _full(), marker=killed, record_mode=True),
        _hold_case(root, "bundle_not_text", [["state_finding"]], marker=bundle_list),
        _hold_case(root, "marker_not_json", _full(), marker=[[b"{cut".hex(), 1]]),
        _hold_case(root, "marker_blank", _full(), marker=[["200a", 1]]),
        _hold_case(root, "marker_deep", _full(), marker=[["5b", 70000]]),
        _hold_case(
            root, "held_shared", _full("close_helper", "acquire"), helper="shared"
        ),
        _hold_case(
            root,
            "held_shared_unlock",
            [["unlock", True], ["state_finding"], ["close_helper"], ["unlock", True]],
            marker=[["", 1]],
            helper="shared",
        ),
        _hold_case(
            root,
            "held_exclusive",
            _full(),
            marker=killed,
            helper="exclusive",
        ),
        _hold_case(
            root,
            "record_held_exclusive",
            _full(),
            marker=[["", 1]],
            helper="exclusive",
            record_mode=True,
        ),
        _hold_case(
            root,
            "record_held_shared",
            _full(),
            marker=[["", 1]],
            helper="shared",
            record_mode=True,
        ),
        _hold_case(root, "record_free_no_file", _full(), record_mode=True),
        _hold_case(root, "lock_dir", _full(), lock_dir=True),
        _hold_case(root, "lock_dir_record", _full(), lock_dir=True, record_mode=True),
        _hold_case(root, "missing_dir", _full(), missing_dir=True),
    ]


def _probe_case(root: Path, name: str, setup: str) -> dict[str, Any]:
    import sqlite3

    case = root / name
    case.mkdir(parents=True)
    store = case / "memory.sqlite"
    given = store
    other = None
    if setup == "dir":
        store.mkdir()
    elif setup == "empty":
        store.write_bytes(b"")
    elif setup == "text":
        store.write_bytes(b"not a database, and longer than a header could be" * 4)
    elif setup != "missing":
        conn = sqlite3.connect(store, isolation_level=None)
        if setup.startswith("wal"):
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        if setup in ("rollback_closed", "wal_closed"):
            conn.close()
        else:
            other = conn
            if setup == "rollback_reading":
                conn.execute("BEGIN")
                conn.execute("SELECT x FROM t").fetchall()
            else:
                conn.execute("SELECT x FROM t").fetchall()
            if setup == "wal_open_dotted":
                given = Path(f"{case}/./{store.name}")
            if setup == "wal_open_link":
                given = case / "link.sqlite"
                given.symlink_to(store)
    try:
        found = restore_lock.probe(given)
    finally:
        if other is not None:
            other.close()
    return {
        "name": name,
        "setup": setup,
        "probe": (
            None
            if found is None
            else found.replace(str(given), "<given>").replace(str(store), "<store>")
        ),
        "names_after": sorted(p.name for p in case.iterdir()),
    }


_PROBES = (
    "missing",
    "dir",
    "empty",
    "text",
    "rollback_closed",
    "rollback_idle",
    "rollback_reading",
    "wal_closed",
    "wal_open",
    "wal_open_dotted",
    "wal_open_link",
)


def generate_hold() -> dict[str, bytes]:
    with world() as w:
        holds = _hold_cases(w.root / "hold")
        probes = [_probe_case(w.root / "probe", n, n) for n in _PROBES]
    document = {
        "marker_format": restore_lock.MARKER_FORMAT,
        "restore_wait_seconds": restore_lock.RESTORE_WAIT_SECONDS,
        "unlock_exit": restore_lock.UNLOCK_EXIT,
        "holders": [restore_lock.HOLDER_RESTORE, restore_lock.HOLDER_READER],
        "holds": holds,
        "probes": probes,
    }
    return {
        "store_restore_lock_hold.json": (
            json.dumps(document, indent=1, sort_keys=True) + "\n"
        ).encode("utf-8")
    }


def generate_python() -> dict[str, bytes]:
    """The `admit` row of the marker nested 70,000 lists deep."""
    with world() as w:
        admit = [_admit_case(w.root / "admit" / "markers", *_DEEP)]
    return {
        "store_restore_lock_python.json": (
            json.dumps({"admit": admit}, indent=1, sort_keys=True) + "\n"
        ).encode("utf-8")
    }
