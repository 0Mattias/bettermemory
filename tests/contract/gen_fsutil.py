"""Fixtures: `_fsutil.py`'s first two units (``fsutil.jsonl``,
``fsutil_python.jsonl``, ``fsutil_flock.jsonl``,
``fsutil_flock_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/fsutil.rs``. This pins
`unlink_retrying` (with `_REPLACE_ATTEMPTS` and `_REPLACE_BACKOFF_S`) and
`bounded_stream_read`.

One JSON object per line, keys sorted, ASCII; the first line is a header
with the count of the lines after it. Every other line is one of:

- ``{"k": "u", "win": bool, "f": [...], "s": [...] or null, "o": ...}``:
  `unlink_retrying` with ``sys.platform`` patched to ``win32`` (or
  ``linux``), ``os.unlink`` raising, at its n-th call, the n-th class
  of ``f`` (a class name; ``null`` succeeds; past the end succeeds),
  ``time.sleep`` recorded and ``still`` answering ``s`` in turn (``null``
  passes no ``still``). ``o`` is ``{"sl": [repr of each pause],
  "asked": n, "tries": n, "r": "ok" or [class, index of the call whose
  error propagated]}``;
- ``{"k": "b", "src": "bytesio" or "pipe", "n": size, "max": max_bytes,
  "o": {"len": n, "sha": hex} or [class, message]}``: `bounded_stream_read`
  over ``n`` bytes, byte ``i`` being ``(7 * i + 3) % 256``, in an
  ``io.BytesIO`` or behind a pipe (an ``os.fdopen`` reader, a thread
  writing).

``fsutil_flock.jsonl`` pins `_flock_windows` (with `_FLOCK_WARNED` and
`FLOCK_TIMEOUT_SECONDS`) and `flock_excl`'s lock path:

- ``{"k": "w", "calls": [...], "o": [...]}``: calls of `_flock_windows`
  on ``Path("x.lock")`` in turn, `_FLOCK_WARNED` false at the first.
  Each call is ``{"m": msvcrt imports, "open": os.open succeeds, "lf":
  failures of ``msvcrt.locking`` before it succeeds (-1: always),
  "unlock": the release succeeds, "close": os.close succeeds, "mono":
  [start, step] (the n-th ``time.monotonic()`` is ``start + n * step``),
  "t": FLOCK_TIMEOUT_SECONDS}``; its outcome ``{"ev": [...], "warn":
  [logged text], "r": "held", "unlocked" or ["TimeoutError", text]}``,
  the events being ``open``, ``mono``, ``lock 1`` (LK_NBLCK of one
  byte), ``sleep <repr>``, ``yield``, ``unlock 1`` and ``close``. The
  module is stubbed in ``sys.modules`` and ``os.open``, ``os.close``,
  the clock and the pause scripted, so the rows hold on every host;
- ``{"k": "p", "path": posix text, "sfx": lock_suffix, "o": posix text
  of the lock path}``: `flock_excl` with the Windows branch recording
  its argument and ``mkdir`` recorded, for names and suffixes that every
  release and system answer alike.

``fsutil_flock_python.jsonl`` holds the lock paths refused, ``"o":
["ValueError", text]``, whose wording is the release's, and every row of
a name ending in a dot or a suffix ``.`` or ``..``, which CPython 3.14
answers differently.

The ``_python`` file holds the rows whose message is the interpreter's
(`BufferedReader.read`'s refusal of a length below -1 and the overflow of
``max_bytes + 1``).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import sys
import threading
import time
import types
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import _fsutil

from ._common import jsonl

_CLASSES: dict[str, type[OSError]] = {
    "PermissionError": PermissionError,
    "FileNotFoundError": FileNotFoundError,
    "IsADirectoryError": IsADirectoryError,
    "OSError": OSError,
}
# Each class's own errno: `OSError(13, ...)` would come back a
# PermissionError.
_ERRNOS = {
    "PermissionError": 13,
    "FileNotFoundError": 2,
    "IsADirectoryError": 21,
    "OSError": 5,
}


def _unlink_row(
    win: bool, fails: Sequence[str | None], still: list[bool] | None
) -> dict[str, Any]:
    tries = 0
    raised: list[BaseException] = []
    sleeps: list[str] = []
    asked = 0

    def fake_unlink(_path: object) -> None:
        nonlocal tries
        n = tries
        tries += 1
        name = fails[n] if n < len(fails) else None
        if name is not None:
            error = _CLASSES[name](_ERRNOS[name], "scripted")
            raised.append(error)
            raise error

    def fake_still() -> bool:
        nonlocal asked
        assert still is not None
        answer = still[asked]
        asked += 1
        return answer

    result: Any
    with (
        patch("sys.platform", "win32" if win else "linux"),
        patch.object(os, "unlink", fake_unlink),
        patch.object(time, "sleep", lambda s: sleeps.append(repr(s))),
    ):
        try:
            _fsutil.unlink_retrying(Path("x"), None if still is None else fake_still)
            result = "ok"
        except OSError as exc:
            result = [type(exc).__name__, raised.index(exc)]
    return {
        "k": "u",
        "win": win,
        "f": list(fails),
        "s": still,
        "o": {"sl": sleeps, "asked": asked, "tries": tries, "r": result},
    }


def _unlink_rows() -> list[dict[str, Any]]:
    rows = []
    perm = "PermissionError"
    for win in (False, True):
        for k in range(7):
            rows.append(_unlink_row(win, [perm] * k, None))
            rows.append(_unlink_row(win, [perm] * k, [True] * 6))
        for other in ("FileNotFoundError", "IsADirectoryError", "OSError"):
            rows.append(_unlink_row(win, [other], None))
            for k in range(1, 6):
                rows.append(_unlink_row(win, [perm] * k + [other], [True] * 6))
        for j in range(5):
            answers = [True] * j + [False] + [True] * 5
            rows.append(_unlink_row(win, [perm] * 6, answers))
            rows.append(_unlink_row(win, [perm] * (j + 1), answers))
        rows.append(_unlink_row(win, [None, perm], [True] * 6))
    return rows


def _data(n: int) -> bytes:
    return bytes((7 * i + 3) % 256 for i in range(n))


def _outcome(read: Any) -> Any:
    try:
        raw = read()
    except (ValueError, OverflowError) as exc:
        return [type(exc).__name__, str(exc)]
    return {"len": len(raw), "sha": hashlib.sha256(raw).hexdigest()}


def _bytesio(n: int, max_bytes: int) -> Any:
    return _outcome(
        lambda: _fsutil.bounded_stream_read(io.BytesIO(_data(n)), max_bytes)
    )


def _pipe(n: int, max_bytes: int) -> Any:
    r, w = os.pipe()
    data = _data(n)

    def writer() -> None:
        # The close flushes too, and fails once the reader has gone:
        # BrokenPipeError on POSIX, EINVAL on Windows.
        try:
            with os.fdopen(w, "wb") as out:
                out.write(data)
        except OSError:
            pass

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        with os.fdopen(r, "rb") as stream:
            return _outcome(lambda: _fsutil.bounded_stream_read(stream, max_bytes))
    finally:
        thread.join()


def _stream_rows(python: bool) -> list[dict[str, Any]]:
    rows = []
    if python:
        cases = [(0, -3), (5, -3), (5, -100), (0, 2**63 - 1)]
        rows = [
            {"k": "b", "src": "pipe", "n": n, "max": m, "o": _pipe(n, m)}
            for n, m in cases
        ]
        m = 2**63 - 1
        rows.append({"k": "b", "src": "bytesio", "n": 3, "max": m, "o": _bytesio(3, m)})
        return rows
    for m in (0, 1, 5, 64, 8191, 8192, 70000):
        for n in sorted({0, max(m - 1, 0), m, m + 1, m + 2, 2 * m + 9}):
            rows.append(
                {"k": "b", "src": "bytesio", "n": n, "max": m, "o": _bytesio(n, m)}
            )
            rows.append({"k": "b", "src": "pipe", "n": n, "max": m, "o": _pipe(n, m)})
    for m in (-1, -2):
        for n in (0, 1, 9):
            rows.append(
                {"k": "b", "src": "bytesio", "n": n, "max": m, "o": _bytesio(n, m)}
            )
            rows.append({"k": "b", "src": "pipe", "n": n, "max": m, "o": _pipe(n, m)})
    return rows


def generate() -> dict[str, bytes]:
    """``fsutil.jsonl``: not gated."""
    rows = [*_unlink_rows(), *_stream_rows(python=False)]
    return {"fsutil.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])}


def generate_python() -> dict[str, bytes]:
    """``fsutil_python.jsonl``: gated on the Python release."""
    rows = _stream_rows(python=True)
    return {"fsutil_python.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])}


_LOCK = Path("x.lock")


def _windows_call(call: dict[str, Any]) -> dict[str, Any]:
    events: list[str] = []
    warnings: list[str] = []
    attempts = 0
    reads = 0

    def locking(_fd: int, mode: int, nbytes: int) -> None:
        nonlocal attempts
        if mode == 0:
            events.append(f"unlock {nbytes}")
            if not call["unlock"]:
                raise OSError(13, "scripted")
            return
        events.append(f"lock {nbytes}")
        n = attempts
        attempts += 1
        if call["lf"] < 0 or n < call["lf"]:
            raise OSError(36, "scripted")

    def fake_open(_path: object, _flags: int, *_mode: int) -> int:
        events.append("open")
        if not call["open"]:
            raise OSError(13, "scripted")
        return 99

    def fake_close(_fd: int) -> None:
        events.append("close")
        if not call["close"]:
            raise OSError(9, "scripted")

    def monotonic() -> float:
        nonlocal reads
        events.append("mono")
        start, step = call["mono"]
        value = start + reads * step
        reads += 1
        return float(value)

    def warning(msg: str, *args: object) -> None:
        warnings.append(msg % args)

    stub = types.ModuleType("msvcrt")
    stub.locking = locking  # type: ignore[attr-defined]
    stub.LK_NBLCK = 2  # type: ignore[attr-defined]
    stub.LK_UNLCK = 0  # type: ignore[attr-defined]
    logger = logging.getLogger("bettermemory._fsutil")
    result: Any
    with (
        patch.dict(sys.modules, {"msvcrt": stub if call["m"] else None}),
        patch.object(os, "open", fake_open),
        patch.object(os, "close", fake_close),
        patch.object(time, "monotonic", monotonic),
        patch.object(time, "sleep", lambda s: events.append(f"sleep {s!r}")),
        patch.object(logger, "warning", warning),
        patch.object(_fsutil, "FLOCK_TIMEOUT_SECONDS", call["t"]),
    ):
        gen = _fsutil._flock_windows(_LOCK)
        try:
            next(gen)
        except TimeoutError as exc:
            result = ["TimeoutError", str(exc)]
        else:
            events.append("yield")
            result = "held" if "lock 1" in events else "unlocked"
            for _ in gen:  # pragma: no cover - a second yield
                raise AssertionError("_flock_windows yielded twice")
    return {"ev": events, "warn": warnings, "r": result}


def _call(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "m": True,
        "open": True,
        "lf": 0,
        "unlock": True,
        "close": True,
        "mono": [100.0, 0.001],
        "t": 30.0,
    }
    base.update(over)
    return base


def _windows_row(calls: list[dict[str, Any]]) -> dict[str, Any]:
    with patch.object(_fsutil, "_FLOCK_WARNED", False):
        outcomes = [_windows_call(c) for c in calls]
    return {"k": "w", "calls": calls, "o": outcomes}


def _windows_rows() -> list[dict[str, Any]]:
    rows = [_windows_row([_call(lf=k)]) for k in (0, 1, 2, 3, 5, 6, 7, 9)]
    rows += [
        _windows_row([_call(unlock=False)]),
        _windows_row([_call(close=False)]),
        _windows_row([_call(unlock=False, close=False, lf=2)]),
        _windows_row([_call(m=False)]),
        _windows_row([_call(open=False)]),
        _windows_row([_call(m=False), _call(m=False), _call()]),
        _windows_row([_call(open=False), _call(m=False), _call(open=False)]),
        _windows_row([_call(m=False), _call(open=False)]),
        _windows_row([_call(lf=1), _call(open=False), _call(lf=0)]),
    ]
    # The deadline: reached exactly, passed, never; timeouts whose one
    # decimal rounds half to even.
    for t, start, step, lf in (
        (1.0, 0.0, 0.5, -1),
        (1.0, 0.0, 0.5, 2),
        (1.0, 0.0, 0.5, 1),
        (0.25, 0.0, 0.1, -1),
        (1.25, 0.0, 0.5, -1),
        (0.05, 5.0, 0.01, -1),
        (0.0, 7.0, 0.0, -1),
        (0.0, 7.0, 0.0, 0),
        (30.0, 0.0, 1.0, -1),
        (30.0, 0.0, 0.1, 12),
        (2.5, 1e9, 0.25, -1),
        (0.35, 0.0, 0.05, -1),
        (-1.0, 0.0, 0.0, -1),
    ):
        rows.append(_windows_row([_call(t=t, mono=[start, step], lf=lf)]))
    rows.append(_windows_row([_call(t=0.0, mono=[0.0, 0.0], lf=-1, close=False)]))
    return rows


_PATHS = (
    "/",
    ".",
    "x",
    "x.txt",
    "d/x.json",
    "/r/d/x.tar.gz",
    ".hidden",
    "a.",
    "a..b",
    "..",
    "d/..",
)
_SUFFIXES = (".lock", ".bettermemory.lock", "", ".", "lock", "..", "a/b", ".\u00e9")


def _lock_path_row(path: str, suffix: str) -> dict[str, Any]:
    seen: list[str] = []

    def record(lock_path: Path) -> Iterator[None]:
        seen.append(lock_path.as_posix())
        yield

    result: Any
    with (
        patch("sys.platform", "win32"),
        patch.object(_fsutil, "_flock_windows", record),
        patch.object(Path, "mkdir", lambda *_a, **_k: None),
    ):
        try:
            with _fsutil.flock_excl(Path(path), lock_suffix=suffix):
                pass
            result = seen[0]
        except ValueError as exc:
            result = ["ValueError", str(exc)]
    return {"k": "p", "path": path, "sfx": suffix, "o": result}


def _release_bound(path: str, suffix: str) -> bool:
    """Whether the answer follows the release: 3.14 reads a name ending in
    a dot as having the suffix ``.`` and takes ``.`` as a suffix, where 3.13
    refuses both."""
    return path.endswith(".") or suffix in (".", "..")


def _lock_path_rows(python: bool) -> list[dict[str, Any]]:
    rows = []
    for path in _PATHS:
        for suffix in _SUFFIXES:
            row = _lock_path_row(path, suffix)
            gated = _release_bound(path, suffix) or isinstance(row["o"], list)
            if gated == python:
                rows.append(row)
    return rows


def generate_flock() -> dict[str, bytes]:
    """``fsutil_flock.jsonl``: not gated."""
    rows = [*_windows_rows(), *_lock_path_rows(python=False)]
    return {"fsutil_flock.jsonl": jsonl([{"k": "header", "cases": len(rows)}, *rows])}


def generate_flock_python() -> dict[str, bytes]:
    """``fsutil_flock_python.jsonl``: gated on the Python release."""
    rows = _lock_path_rows(python=True)
    data = jsonl([{"k": "header", "cases": len(rows)}, *rows])
    return {"fsutil_flock_python.jsonl": data}
