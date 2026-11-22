"""Fixtures: `transcript`'s reader and Stop turn (``transcript.jsonl``,
``transcript_python.jsonl``), and its interrupted turns, owner's words
and compaction marker (``transcript_turns.jsonl``).

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

``transcript_turns.jsonl`` (not gated) pins `interrupted_turns`,
`owner_words` and `compact_marker`, and through them `InterruptedTurn`
and `CompactMarker`. A ``file`` row holds a
file's runs (``f``) and its index (``fi``); every other row names its
file by ``fi``. Kinds of row, each with ``c`` and ``b`` as above and ``q`` the calls into `owner_prompt` (as above, plus
``["x", answer]`` `submitted_text` and ``["w", answer]`` `owner_prompt`
with ``cut=False``):

- ``it``: `interrupted_turns` since ``s`` (an ISO text): ``r`` the turns
  (``{"id", "prompt", "reply", "model"}`` as `text_field` dicts or null,
  and ``at`` its `isoformat()`), or an exception's name.
- ``ow``: `owner_words` with ``chars`` (``n``): ``r`` a `text_field`
  dict; ``x`` ``"missing"`` or ``"dir"`` for no regular file.
- ``cm``: `compact_marker` near ``near`` (ISO text) within ``w``
  microseconds: ``r`` ``{"key", "at"}`` or null, or an exception's name.

Every datetime is the reference's own `datetime.fromisoformat` of the
row's text; no row holds a clock reading.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
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


def _recorder(calls: list[list[Any]]) -> Any:
    is_submitted = _owner.is_submitted
    owner_prompt = _owner.owner_prompt
    submitted_text = _owner.submitted_text
    text_blocks = _owner.text_blocks

    def s(row: Any) -> bool:
        answer = is_submitted(row)
        calls.append(["s", answer])
        return answer

    def o(row: Any, *, cut: bool = True) -> str | None:
        answer = owner_prompt(row, cut=cut)
        calls.append(["o" if cut else "w", _opt(answer)])
        return answer

    def x(row: Any) -> str | None:
        answer = submitted_text(row)
        calls.append(["x", _opt(answer)])
        return answer

    def t(content: Any) -> str | None:
        answer = text_blocks(content)
        calls.append(["t", _opt(answer)])
        return answer

    return (
        mock.patch("bettermemory.transcript.is_submitted", s),
        mock.patch("bettermemory.transcript.owner_prompt", o),
        mock.patch("bettermemory.transcript.submitted_text", x),
        mock.patch("bettermemory.transcript.text_blocks", t),
    )


def _iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _turns(
    scratch: Path, data: bytes, since: str, c: int = 64, b: int = 1 << 30
) -> dict[str, Any]:
    calls: list[list[Any]] = []
    record: dict[str, Any] = {"k": "it", "f": _runs(data), "s": since, "c": c, "b": b}
    p1, p2, p3, p4 = _recorder(calls)
    with _file(scratch, data) as path, p1, p2, p3, p4:
        try:
            got = transcript.interrupted_turns(
                path, since=_iso(since), chunk_bytes=c, budget=b
            )
        except (RecursionError, TypeError) as exc:
            record["r"] = type(exc).__name__
        else:
            record["r"] = [
                {
                    "id": _opt(turn.prompt_id),
                    "prompt": _opt(turn.prompt),
                    "reply": _opt(turn.reply),
                    "model": _opt(turn.model),
                    "at": turn.at.isoformat(),
                }
                for turn in got
            ]
    record["q"] = calls
    return record


def _words(
    scratch: Path,
    data: bytes,
    n: int,
    c: int = 64,
    b: int = 1 << 30,
    fault: str | None = None,
) -> dict[str, Any]:
    calls: list[list[Any]] = []
    record: dict[str, Any] = {"k": "ow", "f": _runs(data), "n": n, "c": c, "b": b}
    p1, p2, p3, p4 = _recorder(calls)
    with _file(scratch, data) as path, p1, p2, p3, p4:
        target = path
        if fault == "missing":
            target = scratch / "absent.jsonl"
        elif fault == "dir":
            target = scratch
        if fault is not None:
            record["x"] = fault
        got = transcript.owner_words(target, chars=n, chunk_bytes=c, budget=b)
    record["r"] = text_field("v", got)
    record["q"] = calls
    return record


def _marker(
    scratch: Path, data: bytes, near: str, w: int, c: int = 64, b: int = 1 << 30
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "k": "cm",
        "f": _runs(data),
        "near": near,
        "w": w,
        "c": c,
        "b": b,
    }
    with _file(scratch, data) as path:
        try:
            got = transcript.compact_marker(
                path,
                near=_iso(near),
                window=timedelta(microseconds=w),
                chunk_bytes=c,
                budget=b,
            )
        except (RecursionError, TypeError) as exc:
            record["r"] = type(exc).__name__
        else:
            record["r"] = (
                None
                if got is None
                else {**text_field("key", got.key), "at": got.at.isoformat()}
            )
    return record


def _ts(second: int, zone: str = "Z") -> str:
    return f"2026-01-01T00:00:{second:02d}{zone}"


def _turn_rows(scratch: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    stop = "[Request interrupted by user]"
    base = _lines(
        [
            _u("p0", "oldest", timestamp=_ts(1)),
            _a("r0"),
            _u("p1", "first words", timestamp=_ts(10)),
            _a("partial", model="m-a"),
            _u("p1", stop, timestamp=_ts(12)),
            _u("p2", "second words", timestamp=_ts(20)),
            _a(None, model="m-t"),
            _tool("p2"),
            _a("tooling", model="m-b"),
            _u("p2", "  " + stop + " for tool use]", timestamp=_ts(25)),
            _u("p3", "third, not cut", timestamp=_ts(30)),
            _a("done"),
        ]
    )
    for since in (_ts(0), _ts(1), _ts(10), _ts(11), _ts(12), _ts(25), _ts(59)):
        out.append(_turns(scratch, base, since))
    for c in (1, 7, 33):
        out.append(_turns(scratch, base, _ts(0), c))
    for b in (40, 200, 400):
        out.append(_turns(scratch, base, _ts(0), 16, b))
    # Offsets: the walk compares instants; `at` keeps the row's offset.
    out.append(_turns(scratch, base, "2026-01-01T01:00:11+01:00"))
    out.append(_turns(scratch, base, "2025-12-31T23:00:11-01:00"))
    # A naive `since` cannot be ordered against an aware stamp.
    out.append(_turns(scratch, base, "2026-01-01T00:00:00"))
    out.append(_turns(scratch, _lines([_u("p1", "x"), _u("p1", stop)]), "2026-01-01"))
    # Timestamps that are missing, null, a number, an object, a list, a
    # naive text, an offset text and text that does not parse.
    for value in (None, 7, 1.5, {"a": 1}, ["x"], "2026-01-01T00:00:40", "nope"):
        rows = [
            _u("p1", "typed", timestamp=_ts(10)),
            _u("p1", stop, timestamp=value),
            _u("p1", "after", timestamp=_ts(11)),
        ]
        out.append(_turns(scratch, _lines(rows), _ts(5)))
    noted = [_u("p1", "typed"), _u("p1", stop)]
    out.append(_turns(scratch, _lines(noted), _ts(5)))
    stamped = [_u("p1", "typed"), _u("p1", stop, timestamp="2026-01-01T03:00:00+03:00")]
    out.append(_turns(scratch, _lines(stamped), _ts(5)))
    # Only the interrupt line: no prompt, not returned.
    out.append(_turns(scratch, _lines([_u("p1", stop, timestamp=_ts(9))]), _ts(0)))
    # Rows without ids: each is its own turn.
    noids = [
        _u(None, "typed", timestamp=_ts(10)),
        _a("ra"),
        _u(None, stop, timestamp=_ts(11)),
        _u("", "again", timestamp=_ts(12)),
        _u("", stop, timestamp=_ts(13)),
    ]
    out.append(_turns(scratch, _lines(noids), _ts(0)))
    # Interleaved ids, a prompt older than a later interrupt, harness rows.
    mixed = [
        _u("p1", "one", timestamp=_ts(10)),
        _a("a1", model="m1"),
        _u("p1", "<task-notification>n</task-notification>", isMeta=True),
        _u("p1", stop, timestamp=_ts(11)),
        _a("between"),
        _u("p2", "two", timestamp=_ts(12)),
        _u("p1", "back to one", timestamp=_ts(13)),
        _u("p1", stop, timestamp=_ts(14)),
        _u("p1", "summary", isCompactSummary=True, timestamp=_ts(15)),
        _a("tail", model=""),
        _u("p1", [{"type": "text", "text": stop}], timestamp=_ts(16)),
        _u("p1", [{"type": "text", "text": "listed"}], timestamp=_ts(17)),
        {"type": "assistant", "message": "flat"},
        b"junk",
        {"type": "system", "subtype": "compact_boundary", "timestamp": _ts(18)},
        _tool("p1"),
        _a("last", model="m9"),
    ]
    out.append(_turns(scratch, _lines(mixed), _ts(0)))
    out.append(_turns(scratch, _lines(mixed, sep=b"\r\n", trail=False), _ts(0), 5))
    out.append(_turns(scratch, b"", _ts(0)))
    return out


def _word_rows(scratch: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows = _lines(
        [
            _u("p0", "alpha beta"),
            _a("reply"),
            _u("p0", "<task-notification>x</task-notification>", isMeta=True),
            _u("p1", "[Request interrupted by user]"),
            _u("p1", "summary", isCompactSummary=True),
            _tool("p1"),
            _u("p2", [{"type": "text", "text": "list words"}]),
            _u("p2", "   "),
            {"type": "assistant", "message": {"content": '"user" said'}},
            b'{"note": ""user""}',
            b'"user"',
            _u("p3", "gamma \u00e9\U0001f600 delta " + "z" * 40),
        ]
    )
    for n in (-1, 0, 1, 5, 12, 50, 51, 52, 53, 60, 64, 66, 200, 10**12):
        out.append(_words(scratch, rows, n))
    for c in (1, 3, 20):
        out.append(_words(scratch, rows, 200, c))
    for b in (60, 150):
        out.append(_words(scratch, rows, 200, 16, b))
    out.append(_words(scratch, rows, 10, fault="missing"))
    out.append(_words(scratch, rows, 10, fault="dir"))
    out.append(_words(scratch, b"", 10))
    out.append(_words(scratch, _lines([{"type": "user"}, _u(None, "")]), 10))
    return out


def _marker_rows(scratch: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def boundary(ts: Any, uuid: Any = "absent", **extra: Any) -> dict[str, Any]:
        row: dict[str, Any] = {"type": "system", "subtype": "compact_boundary"}
        if ts != "absent":
            row["timestamp"] = ts
        if uuid != "absent":
            row["uuid"] = uuid
        row.update(extra)
        return row

    rows = _lines(
        [
            boundary(_ts(10), "u-old"),
            _u("p1", "compact_boundary is a word here"),
            boundary(_ts(20), ""),
            boundary(_ts(30), 5),
            boundary(None, "u-null"),
            boundary(7, "u-num"),
            boundary({"t": 1}, "u-obj"),
            boundary("absent", "u-none"),
            boundary("bad", "u-bad"),
            {"type": "user", "subtype": "compact_boundary", "timestamp": _ts(40)},
            {"type": "system", "subtype": "other", "timestamp": _ts(40)},
            boundary("2026-01-01T02:00:35+02:00", "u-zone"),
            boundary(_ts(38) + "Z", "u-zz"),
        ]
    )
    sec = 1_000_000
    for near, w in (
        (_ts(35), 0),
        (_ts(36), sec),
        (_ts(36), sec - 1),
        (_ts(30), 0),
        (_ts(31), sec),
        (_ts(25), 5 * sec),
        (_ts(24), 4 * sec),
        (_ts(12), 2 * sec),
        (_ts(59), sec),
        (_ts(35), -1),
        ("2026-01-01T00:00:35+05:00", 5 * 3600 * sec),
    ):
        out.append(_marker(scratch, rows, near, w))
    for c in (1, 9, 100):
        out.append(_marker(scratch, rows, _ts(20), 0, c))
    for b in (100, 500):
        out.append(_marker(scratch, rows, _ts(12), 2 * sec, 32, b))
    out.append(_marker(scratch, rows, "2026-01-01T00:00:35", sec))
    out.append(_marker(scratch, b"", _ts(1), sec))
    out.append(_marker(scratch, _lines([boundary("2026-01-01")]), "2026-01-01", 0))
    return out


def generate_turns() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        cases = _turn_rows(scratch) + _word_rows(scratch) + _marker_rows(scratch)
    # Each file is written once, as a ``file`` row, and a case names it by
    # its index (``fi``) in place of ``f``.
    files: list[Any] = []
    records: list[dict[str, Any]] = []
    for case in cases:
        runs = case.pop("f")
        if runs not in files:
            files.append(runs)
            records.append({"k": "file", "fi": len(files) - 1, "f": runs})
        records.append({"fi": files.index(runs), **case})
    return {"transcript_turns.jsonl": jsonl(records)}
