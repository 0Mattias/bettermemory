"""Fixtures: `_fsutil.py`'s first unit (``fsutil.jsonl``,
``fsutil_python.jsonl``).

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

The ``_python`` file holds the rows whose message is the interpreter's
(`BufferedReader.read`'s refusal of a length below -1 and the overflow of
``max_bytes + 1``).
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from collections.abc import Sequence
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
