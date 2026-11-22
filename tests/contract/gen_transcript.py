"""Fixtures: `transcript`'s reader and Stop turn (``transcript.jsonl``,
``transcript_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/transcript``. This pins it to
`transcript.ReverseLines`, `_row`, `_prompt_id` and `stop_turn`, and
through them `_Pending` and `StopTurn`.

Every file is written in a scratch folder by the generator; a row holds
the file's bytes as runs (``f``: ``[[hex, count], ...]``), never its path.
Kinds of row:

- ``rl``: `ReverseLines` over ``f`` with ``c`` (chunk_bytes), ``b``
  (budget) and ``n`` (needle, hex; absent for None): the lines yielded
  (``l``, hex each) and ``rs`` (`reached_start` afterwards). ``x`` names
  a fault: ``"missing"`` (no file), ``"dir"`` (a folder), ``["big", k]``
  (`os.fstat` answers ``k`` bytes more than the file holds) or
  ``["cut", k, size]`` (after the ``k``-th line is yielded the file is
  truncated to ``size`` bytes).
- ``rld``: the same over a large file given by a rule, the lines pinned
  as their count and the SHA-256 of their bytes, each followed by a
  newline (``rule``: ``"lines"``, line ``i`` of ``count`` being
  ``line-<i>-`` and ``i % 97`` times ``x``).
- ``row``: `_row` of ``line`` (runs, as ``f``): ``r`` the dict's `neutral` form, or
  null.
- ``st``: `stop_turn` over ``f`` with ``p`` (prompt_id, null for None),
  ``c`` and ``b``: ``r`` (``{"prompt", "reply", "model"}``, each a
  `text_field` dict ``{"v": ...}`` or null; null for None) and ``q``, the
  calls made into `owner_prompt` in order with their answers
  (``["s", bool]`` `is_submitted`, ``["o", answer]`` `owner_prompt` with
  its default cut, ``["t", answer]`` `text_blocks`), which the native
  test's `OwnerPrompt` gives back.

``transcript_python.jsonl`` holds the rows whose answer is where the
JSON decoder stops recursing (a line nested 20,000 deep): CPython 3.13
on macOS raises RecursionError out of `stop_turn` and `_row`, which other
releases and systems may not. No row sits near the decoder's limit of
9,999 containers: how near a raise comes there depends on the frames
already on the stack, which differ between the generator and the test
that compares its output.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from bettermemory import owner_prompt as _owner
from bettermemory import transcript

from ._common import jsonl, neutral, text_field


def _runs(data: bytes) -> list[list[Any]]:
    """``data`` as runs: a byte repeated 32 times or more is one run."""
    out: list[list[Any]] = []
    plain = bytearray()
    i = 0
    while i < len(data):
        j = i
        while j < len(data) and data[j] == data[i]:
            j += 1
        if j - i >= 32:
            if plain:
                out.append([plain.hex(), 1])
                plain = bytearray()
            out.append([data[i : i + 1].hex(), j - i])
        else:
            plain += data[i:j]
        i = j
    if plain:
        out.append([plain.hex(), 1])
    return out


def _opt(text: str | None) -> dict[str, Any] | None:
    return None if text is None else text_field("v", text)


@contextmanager
def _file(scratch: Path, data: bytes) -> Iterator[Path]:
    path = scratch / "t.jsonl"
    path.write_bytes(data)
    try:
        yield path
    finally:
        path.unlink()


def _reverse(
    scratch: Path,
    data: bytes,
    c: int,
    b: int,
    needle: bytes | None = None,
    fault: Any = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"k": "rl", "f": _runs(data), "c": c, "b": b}
    if needle is not None:
        record["n"] = needle.hex()
    if fault is not None:
        record["x"] = fault
    if fault == "missing":
        path = scratch / "absent.jsonl"
        lines = transcript.ReverseLines(path, chunk_bytes=c, budget=b, needle=needle)
        got = list(lines)
    elif fault == "dir":
        path = scratch / "folder"
        path.mkdir()
        lines = transcript.ReverseLines(path, chunk_bytes=c, budget=b, needle=needle)
        got = list(lines)
        path.rmdir()
    else:
        with _file(scratch, data) as path:
            lines = transcript.ReverseLines(
                path, chunk_bytes=c, budget=b, needle=needle
            )
            if isinstance(fault, list) and fault[0] == "big":
                real = os.fstat

                def inflated(fd: int) -> Any:
                    st = real(fd)
                    return mock.Mock(st_size=st.st_size + fault[1])

                with mock.patch("bettermemory.transcript.os.fstat", inflated):
                    got = list(lines)
            elif isinstance(fault, list) and fault[0] == "cut":
                got = []
                for line in lines:
                    got.append(line)
                    if len(got) == fault[1]:
                        os.truncate(path, fault[2])
            else:
                got = list(lines)
    record["l"] = [line.hex() for line in got]
    record["rs"] = lines.reached_start
    return record


def _u(pid: str | None, content: Any, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": content},
    }
    if pid is not None:
        row["promptId"] = pid
    row.update(extra)
    return row


def _a(text: str | None, model: Any = "m-1", **extra: Any) -> dict[str, Any]:
    content: Any = [{"type": "text", "text": text}] if text is not None else []
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if model is not None:
        message["model"] = model
    row: dict[str, Any] = {"type": "assistant", "message": message}
    row.update(extra)
    return row


def _tool(pid: str | None) -> dict[str, Any]:
    return _u(pid, [{"type": "tool_result", "content": "ok"}])


def _lines(rows: list[Any], sep: bytes = b"\n", trail: bool = True) -> bytes:
    parts = []
    for row in rows:
        if isinstance(row, bytes):
            parts.append(row)
        else:
            parts.append(json.dumps(row).encode())
    return sep.join(parts) + (sep if trail else b"")


def _stop(
    scratch: Path, data: bytes, pid: str | None, c: int, b: int
) -> dict[str, Any]:
    calls: list[list[Any]] = []
    is_submitted = _owner.is_submitted
    owner_prompt = _owner.owner_prompt
    text_blocks = _owner.text_blocks

    def s(row: Any) -> bool:
        answer = is_submitted(row)
        calls.append(["s", answer])
        return answer

    def o(row: Any, *, cut: bool = True) -> str | None:
        assert cut is True
        answer = owner_prompt(row)
        calls.append(["o", _opt(answer)])
        return answer

    def t(content: Any) -> str | None:
        answer = text_blocks(content)
        calls.append(["t", _opt(answer)])
        return answer

    record: dict[str, Any] = {"k": "st", "f": _runs(data), "p": pid, "c": c, "b": b}
    with (
        _file(scratch, data) as path,
        mock.patch("bettermemory.transcript.is_submitted", s),
        mock.patch("bettermemory.transcript.owner_prompt", o),
        mock.patch("bettermemory.transcript.text_blocks", t),
    ):
        try:
            turn = transcript.stop_turn(path, pid, chunk_bytes=c, budget=b)
        except RecursionError:
            record["r"] = "RecursionError"
        else:
            record["r"] = (
                None
                if turn is None
                else {
                    "prompt": _opt(turn.prompt),
                    "reply": _opt(turn.reply),
                    "model": _opt(turn.model),
                }
            )
    record["q"] = calls
    return record


def _row(line: bytes) -> dict[str, Any]:
    try:
        row = transcript._row(line)
    except RecursionError:
        return {"k": "row", "line": _runs(line), "r": "RecursionError"}
    return {"k": "row", "line": _runs(line), "r": None if row is None else neutral(row)}


_SMALL = [
    b"",
    b"a",
    b"a\n",
    b"\n",
    b"\n\n",
    b"a\nbb\nccc",
    b"a\nbb\nccc\n",
    b"a\r\nbb\r\n\r\nccc\r\n",
    b"one\n\ntwo\nthree-long-line\n4\n",
    b"\xff\xfe\n\xc3(\nok\n",
]

_CHUNKS = [1, 2, 3, 4, 7, 64]
_BUDGETS = [-3, 0, 1, 2, 5, 9, 1 << 30]


def _reverse_rows(scratch: Path) -> list[dict[str, Any]]:
    out = []
    for data in _SMALL:
        for c in _CHUNKS:
            for b in _BUDGETS:
                out.append(_reverse(scratch, data, c, b))
    hay = b"alpha\nbeta\ngamma\ndelta\nepsilon\n"
    for c in (3, 6, 11, 64):
        for needle in (b"", b"mm", b"zeta", b"a\nd", b"\n", b"eps"):
            out.append(_reverse(scratch, hay, c, 1 << 30, needle))
    out.append(_reverse(scratch, hay, 4, 1 << 30, fault="missing"))
    out.append(_reverse(scratch, hay, 4, 1 << 30, fault="dir"))
    for extra in (1, 7):
        out.append(_reverse(scratch, hay, 64, 1 << 30, fault=["big", extra]))
        out.append(_reverse(scratch, hay, 6, 1 << 30, fault=["big", extra]))
    for k, size in ((1, 10), (2, 0), (1, 31), (3, 6)):
        out.append(_reverse(scratch, hay, 6, 1 << 30, fault=["cut", k, size]))
    # A line longer than several chunks, the budget ending inside it, and a
    # line starting exactly where the budget ends.
    long = b"head\n" + b"y" * 100 + b"\ntail\n"
    for c, b in ((7, 1 << 30), (7, 106), (7, 107), (7, 108), (33, 50), (200, 6)):
        out.append(_reverse(scratch, long, c, b))
    return out


def _big_lines(count: int) -> bytes:
    return b"".join(b"line-%d-" % i + b"x" * (i % 97) + b"\n" for i in range(count))


def _digest_rows(scratch: Path) -> list[dict[str, Any]]:
    out = []
    count = 40_000
    data = _big_lines(count)
    for c, b, needle in (
        (transcript.CHUNK_BYTES, transcript.WALK_BYTES, None),
        (transcript.CHUNK_BYTES, 1_500_000, None),
        (65_536, 1_000_000, b"line-3999"),
        (4_093, transcript.WALK_BYTES, None),
    ):
        with _file(scratch, data) as path:
            lines = transcript.ReverseLines(
                path, chunk_bytes=c, budget=b, needle=needle
            )
            sha = hashlib.sha256()
            n = 0
            for line in lines:
                sha.update(line + b"\n")
                n += 1
        record: dict[str, Any] = {
            "k": "rld",
            "rule": "lines",
            "count": count,
            "size": len(data),
            "c": c,
            "b": b,
            "yielded": n,
            "sha256": sha.hexdigest(),
            "rs": lines.reached_start,
        }
        if needle is not None:
            record["n"] = needle.hex()
        out.append(record)
    return out


def _row_rows() -> list[dict[str, Any]]:
    lines = [
        b"",
        b"   \t\r\x0b\x0c",
        b"\xc2\xa0",
        b"{}",
        b' {"a": 1} ',
        b"[1, 2]",
        b'"text"',
        b"3",
        b"null",
        b"{broken",
        b'{"a": "\xff\xfe"}',
        b'{"a": "\xed\xa0\x80"}',
        b'{"a": "\\ud800"}',
        b'\xef\xbb\xbf{"a": 1}',
        b'{"a": 1, "a": 2, "b": [true, null, 1.5]}',
        b'{"n": ' + b"1" * 4301 + b"}",
        b'{"n": NaN}',
        b"[" * 500 + b"]" * 500,
    ]
    return [_row(line) for line in lines]


def _stop_rows(scratch: Path) -> list[dict[str, Any]]:
    big = 1 << 30
    cases: list[tuple[bytes, str | None, int, int]] = []

    def add(data: bytes, pid: str | None, c: int = 64, b: int = big) -> None:
        cases.append((data, pid, c, b))

    plain = _lines(
        [
            _u("p0", "older turn"),
            _a("old reply"),
            _u("p1", "hello there"),
            _a(None, model="m-think"),
            _tool("p1"),
            _a("final words", model="m-2"),
        ]
    )
    for pid in (None, "p1", "p0", "p9", ""):
        add(plain, pid)
    for c in (1, 5, 17):
        add(plain, None, c)
    for b in (10, 60, 120, 200, 260):
        add(plain, None, 16, b)
    # No ids: the newest submitted row ends the turn.
    noids = _lines([_u(None, "first"), _a("r1"), _u(None, "second"), _a("r2")])
    add(noids, None)
    add(noids, "p1")
    # An empty promptId counts as no id, on the row and as the argument.
    emptyid = _lines([_u(None, "first"), _a("r1"), _u("", "second"), _a("r2")])
    for pid in (None, ""):
        add(emptyid, pid)
    # Submitted rows of the turn none of which is the owner's.
    notowner = _lines(
        [
            _u("p0", "before"),
            _u("p1", "real words"),
            _a("mid"),
            _u("p1", "<task-notification>x</task-notification>", isMeta=True),
            _a("after", model=""),
            _u("p1", "   "),
            _a("last", model=7),
        ]
    )
    for pid in (None, "p1"):
        add(notowner, pid)
    onlymeta = _lines([_u("p1", "m", isMeta=True), _a("r")])
    add(onlymeta, None)
    add(onlymeta, None, 4, 20)
    # Only tool results: no submitted row.
    add(_lines([_tool("p1"), _a("r")]), None)
    # Rows that are not rows.
    junk = _lines(
        [
            b"",
            b"not json",
            b"[1]",
            b"\xff\xfe",
            {"type": "system"},
            {"type": "user", "promptId": 3, "message": "x"},
            _u("p2", [{"type": "text", "text": "list prompt"}, {"type": "image"}]),
            {"type": "assistant", "message": "flat"},
            {"type": "assistant"},
            _a("", model=None),
            {"type": "assistant", "message": {"content": "str", "model": "m-s"}},
            _a("kept", model="m-k"),
        ],
        sep=b"\r\n",
        trail=False,
    )
    for pid in (None, "p2"):
        add(junk, pid)
    for c in (2, 9):
        add(junk, None, c)
    add(b"", None)
    add(b"\n\n\n", "p1")
    # The interrupt line and a compaction summary in a turn.
    interrupted = _lines(
        [
            _u("p5", "do the thing"),
            _a("working"),
            _u("p5", "[Request interrupted by user]"),
            _u("p5", "summary", isCompactSummary=True),
        ]
    )
    add(interrupted, None)
    add(interrupted, "p5", 8, 90)
    return [_stop(scratch, *case) for case in cases]


def generate() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        header = {
            "k": "header",
            "CHUNK_BYTES": transcript.CHUNK_BYTES,
            "WALK_BYTES": transcript.WALK_BYTES,
            "INTERRUPT_OPENING": _owner.INTERRUPT_OPENING,
        }
        records = (
            [header]
            + _reverse_rows(scratch)
            + _digest_rows(scratch)
            + _row_rows()
            + _stop_rows(scratch)
        )
    return {"transcript.jsonl": jsonl(records)}


def generate_python() -> dict[str, bytes]:
    deep = b"[" * 20_000 + b"]" * 20_000
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        line = b'{"type": "user", "x": ' + deep + b"}"
        records = [
            _row(deep),
            _stop(scratch, line + b"\n", None, 64, 1 << 30),
            _stop(scratch, _lines([_u("p1", "hi"), _a("r")]) + line, None, 64, 1 << 30),
        ]
    return {"transcript_python.jsonl": jsonl(records)}
