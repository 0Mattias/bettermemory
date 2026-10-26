"""`bettermemory.transcript`: the reverse reader, the Stop's turn and the
compaction marker (RS-F5), on synthetic transcripts whose chunk
boundaries and budgets are placed byte for byte.

The reader is checked against a model of what it must yield (every line
that starts inside the budget, newest first) at many chunk sizes and
every budget. The Stop's turn is checked on the shapes a real transcript
takes: a row across a chunk boundary or across the tail's first byte,
rows longer than a chunk, the owner's row at byte 0, a file growing or
shrinking under the read, CRLF line ends, a character split by a
boundary, a chunk starting exactly at a row, the budget ending at a
row's first byte, a reply earlier in the turn, no reply in the turn, and
a newer turn the harness composed in front of an older owner prompt.
Small chunks stand in for the 1 MiB ones: the code path is the same at
any size.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any

import pytest

from bettermemory import transcript as transcript_module
from bettermemory.owner_prompt import PROMPT_CUT_CHARS
from bettermemory.time_utils import isoformat_utc
from bettermemory.transcript import (
    CHUNK_BYTES,
    WALK_BYTES,
    CompactMarker,
    ReverseLines,
    compact_marker,
    interrupted_turns,
    owner_words,
    stop_turn,
)

from . import _transcript_rows as tr

CHUNK = 1024
OWNER = "the owner asks about the backup window"
REPLY = "the reply of this turn"
MODEL = "claude-test"
OLDER = (tr.user("an older ask", "p0"), tr.assistant("an older reply", "older-model"))


def _turn(
    path: Path, prompt_id: str | None, **limits: int
) -> tuple[str | None, str | None, str | None] | None:
    """`stop_turn` with chunks of `CHUNK` bytes, as (prompt, reply, model)."""
    limits.setdefault("chunk_bytes", CHUNK)
    turn = stop_turn(path, prompt_id, **limits)
    return None if turn is None else (turn.prompt, turn.reply, turn.model)


def _inside(data: bytes, start: int, length: int, distance: int) -> bool:
    """Whether the chunk boundary `distance` bytes before the end of
    `data` falls strictly inside the line at `start` of `length` bytes
    (after its first byte, before its newline)."""
    boundary = len(data) - distance
    return start < boundary < start + length - 1


def _pinned(
    path: Path,
    prompt: dict[str, Any],
    *,
    point: int,
    distance: int,
    newline: str = "\n",
) -> tuple[bytes, int]:
    """A transcript of an older turn, then `prompt` (a row of turn p1), a
    tool result of p1 and the reply, the tool result sized so that byte
    `point` of the prompt's line lies exactly `distance` bytes before the
    end of the file. The file's bytes and the prompt line's first byte."""
    head = tr.dump(*OLDER, newline=newline)
    line = tr.dump(prompt, newline=newline)
    reply = tr.dump(tr.assistant(REPLY), newline=newline)
    start = len(head)
    size = start + point + distance
    base = len(tr.dump(tr.tool_result(0, "p1"), newline=newline))
    fill = size - start - len(line) - len(reply)
    assert fill >= base, (fill, base)
    filler = tr.dump(tr.tool_result(fill - base, "p1"), newline=newline)
    data = head + line + filler + reply
    assert len(data) == size
    path.write_bytes(data)
    return data, start


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------

SAMPLE = b"".join(
    [
        b'{"a":1}\n',
        b"\n",
        b"x" * 37 + b"\n",
        "é\U0001f642 a multibyte line".encode() + b"\r\n",
        b"y" * 90 + b"\n",
        b"short\n",
        b"the last line",
    ]
)


def _expected(data: bytes, budget: int) -> list[bytes]:
    """What `ReverseLines` must yield for `data` under `budget`: every
    line (the bytes between newlines; the last one empty when the data
    ends with a newline) that starts at or after the budget's floor,
    newest first, and nothing when the budget covers no byte."""
    floor = max(0, len(data) - budget)
    if floor >= len(data):
        return []
    lines: list[bytes] = []
    start = 0
    for line in data.split(b"\n"):
        if start >= floor:
            lines.append(line)
        start += len(line) + 1
    return lines[::-1]


@pytest.mark.parametrize("ending", [b"", b"\n"])
def test_the_reader_yields_every_line_of_its_budget_newest_first(
    tmp_path: Path, ending: bytes
) -> None:
    """At every chunk size, a line longer than a chunk included, and at
    every budget: a line the budget cuts is not yielded, one that starts
    exactly at its first byte is (the one byte read ahead of each chunk
    says so), and `reached_start` says whether the budget covered the
    file."""
    data = SAMPLE + ending
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    whole = len(data)
    for chunk in range(1, whole + 2):
        lines = ReverseLines(path, chunk_bytes=chunk, budget=whole)
        assert list(lines) == _expected(data, whole), chunk
        assert lines.reached_start, chunk
    for chunk in (1, 2, 3, 7, 16, 64, whole):
        for budget in range(whole + 2):
            lines = ReverseLines(path, chunk_bytes=chunk, budget=budget)
            assert list(lines) == _expected(data, budget), (chunk, budget)
            assert lines.reached_start is (budget >= whole), (chunk, budget)


def test_the_reader_yields_nothing_for_an_empty_or_missing_file(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert list(ReverseLines(empty, chunk_bytes=4, budget=100)) == []
    assert list(ReverseLines(tmp_path / "missing.jsonl")) == []


def test_bytes_appended_during_the_read_are_not_read(tmp_path: Path) -> None:
    """The file's end is taken once, when it is opened; rows written
    while the walk runs lie after it."""
    data = SAMPLE + b"\n"
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    lines = iter(ReverseLines(path, chunk_bytes=16, budget=len(data)))
    first = next(lines)
    tr.append(path, *({"appended": n} for n in range(50)))
    assert [first, *lines] == _expected(data, len(data))


def test_the_read_ends_when_the_file_shrinks_under_it(tmp_path: Path) -> None:
    """A chunk that cannot be read whole ends the iteration: nothing
    after it is yielded, not even the bytes carried from the chunk
    before."""
    data = b"".join(f"line-{n:04d}\n".encode() for n in range(5))
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    lines = ReverseLines(path, chunk_bytes=7, budget=len(data))
    walk = iter(lines)
    assert next(walk) == b""  # the empty line after the last newline
    assert next(walk) == b"line-0004"
    path.write_bytes(b"")
    assert list(walk) == []
    assert not lines.reached_start


def test_with_a_needle_a_chunk_without_it_yields_nothing(tmp_path: Path) -> None:
    """With chunks shorter than a line each chunk completes at most one
    line, so the prefilter acts line by line; with one chunk for the
    whole file every line of it is yielded, as the docstring says."""
    rows = [f"row-{n:03d}-plain" for n in range(20)]
    rows[7] = "row-007-NEEDLE"
    data = ("\n".join(rows) + "\n").encode()
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    found = ReverseLines(path, chunk_bytes=5, budget=len(data), needle=b"NEEDLE")
    assert list(found) == [b"row-007-NEEDLE"]
    assert found.reached_start
    assert len(list(ReverseLines(path, chunk_bytes=5, budget=len(data)))) == 21
    whole = ReverseLines(
        path, chunk_bytes=len(data), budget=len(data), needle=b"NEEDLE"
    )
    assert list(whole) == _expected(data, len(data))
    absent = ReverseLines(path, chunk_bytes=5, budget=len(data), needle=b"ABSENT")
    assert list(absent) == []


def test_the_walk_reads_a_mebibyte_at_a_time_and_64_at_most() -> None:
    """The bounds the 2026-10-19 replay measured (`transcript.WALK_BYTES`
    names it); a change to either is deliberate."""
    assert (CHUNK_BYTES, WALK_BYTES) == (1_048_576, 64 * 1_048_576)


class _Counted:
    """A file handle that records the size of every read."""

    def __init__(self, handle: Any, reads: list[int]) -> None:
        self._handle = handle
        self._reads = reads

    def __enter__(self) -> _Counted:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def fileno(self) -> int:
        return int(self._handle.fileno())

    def seek(self, offset: int) -> int:
        return int(self._handle.seek(offset))

    def read(self, size: int = -1) -> bytes:
        data = bytes(self._handle.read(size))
        self._reads.append(len(data))
        return data


def test_each_read_takes_one_chunk_and_the_byte_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory holds one chunk and the line carried across it, never the
    file: a walk to the start reads the file once, a chunk and its one
    byte ahead at a time, and no more bytes than that."""
    data = b"".join(f"row {n:05d} {'z' * (n % 37)}\n".encode() for n in range(300))
    path = tmp_path / "t.jsonl"
    path.write_bytes(data)
    reads: list[int] = []
    real_open = type(path).open

    def counting_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        return _Counted(real_open(self, *args, **kwargs), reads)

    monkeypatch.setattr(type(path), "open", counting_open)
    lines = list(ReverseLines(path, chunk_bytes=CHUNK, budget=len(data)))
    monkeypatch.undo()
    chunks = -(-len(data) // CHUNK)
    assert lines == _expected(data, len(data))
    assert reads == [CHUNK + 1] * (chunks - 1) + [len(data) - (chunks - 1) * CHUNK]


# ---------------------------------------------------------------------------
# The Stop's turn: rows placed across chunk boundaries and the budget
# ---------------------------------------------------------------------------


def test_an_owner_row_across_the_tails_first_byte(tmp_path: Path) -> None:
    """The first chunk (the tail) begins inside the owner's row: the
    part in the tail is carried, the rest read with the next chunk."""
    text = OWNER + " " + "q" * 300
    prompt = tr.user(text, "p1")
    length = len(tr.dump(prompt))
    path = tmp_path / "t.jsonl"
    data, start = _pinned(path, prompt, point=length // 2, distance=CHUNK)
    assert _inside(data, start, length, CHUNK)
    assert _turn(path, "p1") == (text, REPLY, MODEL)
    assert _turn(path, None) == (text, REPLY, MODEL)


def test_an_owner_row_across_a_chunk_boundary_further_back(tmp_path: Path) -> None:
    text = OWNER + " " + "q" * 300
    prompt = tr.user(text, "p1")
    length = len(tr.dump(prompt))
    path = tmp_path / "t.jsonl"
    data, start = _pinned(path, prompt, point=length // 3, distance=3 * CHUNK)
    assert _inside(data, start, length, 3 * CHUNK)
    assert _turn(path, "p1") == (text, REPLY, MODEL)


def test_a_row_longer_than_a_chunk_after_the_owners(tmp_path: Path) -> None:
    """The tool result between the prompt and the reply spans several
    chunks: its bytes accumulate until its first byte is read."""
    prompt = tr.user(OWNER, "p1")
    path = tmp_path / "t.jsonl"
    data, start = _pinned(path, prompt, point=5, distance=4 * CHUNK + 17)
    length = len(tr.dump(prompt))
    filler = data.index(b"\n", start + length) - (start + length) + 1
    assert filler > 3 * CHUNK
    assert _turn(path, "p1") == (OWNER, REPLY, MODEL)


def test_an_owner_row_longer_than_a_chunk(tmp_path: Path) -> None:
    """The owner's row spans several chunks; its prompt is read whole and
    cut where the probe cuts it."""
    text = OWNER + " " + "h" * (3 * CHUNK)
    prompt = tr.user(text, "p1")
    length = len(tr.dump(prompt))
    assert length > 3 * CHUNK
    path = tmp_path / "t.jsonl"
    data, start = _pinned(path, prompt, point=CHUNK + 5, distance=3 * CHUNK + 500)
    assert any(_inside(data, start, length, k * CHUNK) for k in range(1, 6))
    assert _turn(path, "p1") == (text[:PROMPT_CUT_CHARS], REPLY, MODEL)


def test_an_owner_row_at_byte_zero(tmp_path: Path) -> None:
    """The first chunk to reach the file's start reads no byte before
    it: the owner's row there starts a line."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user(OWNER, "p1"),
        tr.assistant(None),
        tr.tool_result(2 * CHUNK + 33, "p1"),
        tr.assistant(REPLY),
    )
    assert path.read_bytes().startswith(tr.dump(tr.user(OWNER, "p1")))
    assert path.stat().st_size > 2 * CHUNK
    assert _turn(path, "p1") == (OWNER, REPLY, MODEL)
    assert _turn(path, None) == (OWNER, REPLY, MODEL)


def test_rows_written_during_the_walk_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer turn written while the Stop reads the file is not this
    Stop's: the walk reads up to where the file ended when it opened."""
    path = tmp_path / "t.jsonl"
    _pinned(path, tr.user(OWNER, "p1"), point=0, distance=3 * CHUNK)
    real = transcript_module._row
    grown: list[bool] = []

    def growing(line: bytes) -> dict[str, Any] | None:
        if not grown:
            tr.append(path, tr.user("a newer ask", "p2"), tr.assistant("a newer reply"))
            grown.append(True)
        return real(line)

    monkeypatch.setattr(transcript_module, "_row", growing)
    assert _turn(path, None) == (OWNER, REPLY, MODEL)
    assert grown


def test_crlf_line_ends(tmp_path: Path) -> None:
    text = OWNER + " " + "q" * 300
    prompt = tr.user(text, "p1")
    length = len(tr.dump(prompt, newline="\r\n"))
    path = tmp_path / "t.jsonl"
    data, start = _pinned(
        path, prompt, point=length // 2, distance=2 * CHUNK, newline="\r\n"
    )
    assert data.count(b"\r\n") == data.count(b"\n") == 5
    assert _inside(data, start, length, 2 * CHUNK)
    assert _turn(path, "p1") == (text, REPLY, MODEL)
    assert _turn(path, None) == (text, REPLY, MODEL)


@pytest.mark.parametrize("distance", [CHUNK, 2 * CHUNK])
def test_a_character_split_by_a_chunk_boundary(tmp_path: Path, distance: int) -> None:
    """The boundary falls on a continuation byte of a four-byte character
    of the owner's row, in the tail and further back: rows are decoded
    only once whole."""
    text = "the owner writes " + "é" * 300 + "\U0001f642" * 100
    prompt = tr.user(text, "p1")
    line = tr.dump(prompt)
    point = line.index("\U0001f642".encode()) + 2
    assert 0x80 <= line[point] < 0xC0
    path = tmp_path / "t.jsonl"
    data, start = _pinned(path, prompt, point=point, distance=distance)
    assert 0x80 <= data[len(data) - distance] < 0xC0
    assert _turn(path, "p1") == (text, REPLY, MODEL)


@pytest.mark.parametrize("distance", [CHUNK, 2 * CHUNK])
def test_a_chunk_that_starts_exactly_at_the_owners_row(
    tmp_path: Path, distance: int
) -> None:
    """The byte before the chunk is a newline: the chunk starts a line,
    the owner's row is read once and whole."""
    path = tmp_path / "t.jsonl"
    data, start = _pinned(path, tr.user(OWNER, "p1"), point=0, distance=distance)
    assert len(data) - distance == start and data[start - 1 : start] == b"\n"
    assert _turn(path, "p1") == (OWNER, REPLY, MODEL)


def test_the_budget_ending_at_the_owners_first_byte(tmp_path: Path) -> None:
    """A budget that ends exactly at the row's first byte still reads it
    (one byte is read ahead of the budget to say that a line starts
    there); a budget one byte shorter cuts the row, and the turn is
    undecided: nothing is recorded."""
    path = tmp_path / "t.jsonl"
    data, start = _pinned(path, tr.user(OWNER, "p1"), point=0, distance=3 * CHUNK)
    budget = len(data) - start
    assert _turn(path, "p1", budget=budget) == (OWNER, REPLY, MODEL)
    assert _turn(path, "p1", budget=budget + 1) == (OWNER, REPLY, MODEL)
    assert _turn(path, "p1", budget=budget - 1) is None
    assert _turn(path, None, budget=budget - 1) is None


# ---------------------------------------------------------------------------
# The Stop's turn: which rows are its own
# ---------------------------------------------------------------------------


def test_a_wrap_up_turn_replies_with_its_own_newest_text(tmp_path: Path) -> None:
    """The turn ends on a tool call with no text (a wrap-up): the reply is
    the newest text written after the prompt, not the first."""
    path = tr.write(
        tmp_path / "t.jsonl",
        *OLDER,
        tr.user(OWNER, "p1"),
        tr.assistant("mid-turn text"),
        tr.assistant(None),
        tr.tool_result(2_000, "p1"),
        tr.assistant("closing text"),
        tr.assistant(None),
        tr.tool_result(50, "p1"),
    )
    assert _turn(path, "p1") == (OWNER, "closing text", MODEL)
    assert _turn(path, None) == (OWNER, "closing text", MODEL)


def test_a_wrap_up_turn_without_text_has_no_reply(tmp_path: Path) -> None:
    """No text this turn: no reply, never the previous turn's (RS-F3's
    fallback took it)."""
    path = tr.write(
        tmp_path / "t.jsonl",
        *OLDER,
        tr.user(OWNER, "p1"),
        tr.assistant(None),
        tr.tool_result(2 * CHUNK, "p1"),
        tr.assistant(None),
        tr.tool_result(50, "p1"),
    )
    assert _turn(path, "p1") == (OWNER, None, MODEL)
    assert _turn(path, None) == (OWNER, None, MODEL)
    unstamped = tr.write(
        tmp_path / "unstamped.jsonl",
        tr.user("an older ask"),
        tr.assistant("an older reply"),
        tr.user(OWNER),
        tr.assistant(None),
        tr.tool_result(50),
        tr.assistant(None),
    )
    assert _turn(unstamped, None) == (OWNER, None, MODEL)


@pytest.mark.parametrize(
    "composed",
    [
        tr.user("a background task finished", "p1", turnOrigin="task_notification"),
        tr.user("<system-reminder>\nlinked sessions\n</system-reminder>", "p1"),
        tr.user("<task-notification>done</task-notification>", "p1"),
    ],
)
def test_a_newer_composed_turn_never_reads_the_older_owner_prompt(
    tmp_path: Path, composed: dict[str, Any]
) -> None:
    """The newest turn is the harness's: it has no owner prompt, and the
    owner's prompt of the turn before is not taken in its place (RS-F3's
    walk took an older turn's prompt in 58 of the 60 such Stops of the
    replay)."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user(OWNER, "p0"),
        tr.assistant("the owner's reply"),
        composed,
        tr.assistant("the composed turn's reply"),
    )
    assert _turn(path, "p1") == (None, "the composed turn's reply", MODEL)
    assert _turn(path, None) == (None, "the composed turn's reply", MODEL)
    assert _turn(path, "p0") == (OWNER, "the owner's reply", MODEL)


def test_the_payload_names_the_turn_past_newer_ones(tmp_path: Path) -> None:
    """A Stop whose turn is not the newest reads its own rows: the newer
    turn's reply and model are not this turn's."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user("the first ask", "p1"),
        tr.assistant("the first reply", "first-model"),
        tr.user("the second ask", "p2"),
        tr.assistant("the second reply", "second-model"),
    )
    assert _turn(path, "p1") == ("the first ask", "the first reply", "first-model")
    assert _turn(path, "p2") == ("the second ask", "the second reply", "second-model")
    assert _turn(path, None) == ("the second ask", "the second reply", "second-model")


def test_no_row_of_the_named_turn_reads_nothing(tmp_path: Path) -> None:
    path = tr.write(tmp_path / "t.jsonl", *OLDER, tr.user(OWNER, "p1"))
    assert _turn(path, "p9") is None
    only_results = tr.write(
        tmp_path / "results.jsonl", tr.tool_result(10, "p1"), tr.assistant(REPLY)
    )
    assert _turn(only_results, "p1") is None
    assert _turn(only_results, None) is None


def test_a_composed_row_ahead_of_an_owner_prompt_past_the_budget_is_undecided(
    tmp_path: Path,
) -> None:
    """The turn's newest submitted row is a notification and its owner
    prompt lies beyond the budget: the walk cannot say whether the turn
    is the owner's, so the Stop records nothing rather than calling it a
    turn without an owner prompt."""
    path = tr.write(
        tmp_path / "t.jsonl",
        *OLDER,
        tr.user(OWNER, "p1"),
        tr.tool_result(3 * CHUNK, "p1"),
        tr.user("a background task finished", "p1", turnOrigin="task_notification"),
        tr.assistant(REPLY),
    )
    assert _turn(path, "p1", budget=2 * CHUNK) is None
    assert _turn(path, "p1") == (OWNER, REPLY, MODEL)


def test_a_composed_turn_is_decided_at_the_turn_before(tmp_path: Path) -> None:
    """A row of the turn before ends this one: the walk stops there, so a
    budget that ends further back does not leave the turn undecided."""
    older = (
        tr.user("an older ask", "p0"),
        tr.tool_result(3 * CHUNK, "p0"),
        tr.tool_result(40, "p0"),
    )
    composed = (
        tr.user("a background task finished", "p1", turnOrigin="task_notification"),
        tr.assistant(REPLY),
    )
    path = tr.write(tmp_path / "t.jsonl", *older, *composed)
    reach = len(tr.dump(older[-1], *composed)) + 10
    assert _turn(path, "p1", budget=reach) == (None, REPLY, MODEL)
    assert _turn(path, None, budget=reach) == (None, REPLY, MODEL)


def test_a_compaction_summary_inside_the_turn_is_passed(tmp_path: Path) -> None:
    """An automatic compaction keeps the turn's id on its summary row; the
    owner's prompt before it is the turn's, and the reply after it."""
    path = tr.write(
        tmp_path / "t.jsonl",
        *OLDER,
        tr.user(OWNER, "p1"),
        tr.assistant(None),
        tr.tool_result(100, "p1"),
        tr.user("This session is being continued.", "p1", isCompactSummary=True),
        tr.assistant(REPLY),
    )
    assert _turn(path, "p1") == (OWNER, REPLY, MODEL)


def test_an_interrupt_line_inside_the_turn_is_passed(tmp_path: Path) -> None:
    path = tr.write(
        tmp_path / "t.jsonl",
        *OLDER,
        tr.user(OWNER, "p1"),
        tr.assistant("a partial answer"),
        tr.user("[Request interrupted by user]", "p1"),
    )
    assert _turn(path, "p1") == (OWNER, "a partial answer", MODEL)


def test_replayed_rows_of_an_older_turn_do_not_join_the_newest(
    tmp_path: Path,
) -> None:
    """A resumed or forked file repeats an older turn's rows: the newest
    turn is still read alone, and the repeated turn reads as it did."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user("the first ask", "p1"),
        tr.assistant("the first reply"),
        tr.user("the first ask", "p1"),
        tr.assistant("the first reply"),
        tr.user("the second ask", "p2"),
        tr.assistant("the second reply"),
    )
    assert _turn(path, None) == ("the second ask", "the second reply", MODEL)
    assert _turn(path, "p1") == ("the first ask", "the first reply", MODEL)


def test_without_a_payload_id_the_newest_user_rows_turn_is_read(
    tmp_path: Path,
) -> None:
    """The newest user row names the turn, a tool result row as well as a
    submitted one: a turn whose submitted row the file does not hold is
    undecided, and the older turn's prompt is not read in its place."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user("the first ask", "p1"),
        tr.assistant("the first reply"),
        tr.tool_result(50, "p2"),
        tr.assistant("a reply of the second turn"),
    )
    assert _turn(path, None) is None
    assert _turn(path, "p1") == ("the first ask", "the first reply", MODEL)


def test_without_prompt_ids_the_newest_submitted_row_is_the_prompt(
    tmp_path: Path,
) -> None:
    """A transcript written before rows carried ``promptId``: the newest
    submitted row is the turn's prompt, and the tool results after it are
    the turn's own."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user("an older ask"),
        tr.assistant("an older reply"),
        tr.user(OWNER),
        tr.assistant(None),
        tr.tool_result(50),
        tr.assistant(REPLY),
    )
    assert _turn(path, None) == (OWNER, REPLY, MODEL)


def test_a_row_without_an_id_is_its_own_turn(tmp_path: Path) -> None:
    """Without ids a submitted row ends its turn: judged alone and
    decided there, though the budget ends in the rows before it, and an
    older row without an id is not read in its place."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user(OWNER, "p0"),
        tr.assistant("x" * (3 * CHUNK)),
        tr.user("a background task finished", turnOrigin="task_notification"),
        tr.assistant(REPLY),
    )
    tail = tr.dump(
        tr.user("a background task finished", turnOrigin="task_notification"),
        tr.assistant(REPLY),
    )
    assert _turn(path, None, budget=len(tail) + 10) == (None, REPLY, MODEL)
    assert _turn(path, None) == (None, REPLY, MODEL)
    unstamped = tr.write(
        tmp_path / "unstamped.jsonl",
        tr.user(OWNER),
        tr.assistant("the owner's reply"),
        tr.user("<task-notification>done</task-notification>"),
        tr.assistant(REPLY),
    )
    assert _turn(unstamped, None) == (None, REPLY, MODEL)


def test_the_model_is_the_turns_newest(tmp_path: Path) -> None:
    """The newest assistant row of the turn names the model, with or
    without text; a turn without one has none, never the older turn's."""
    path = tr.write(
        tmp_path / "t.jsonl",
        *OLDER,
        tr.user(OWNER, "p1"),
        tr.assistant(REPLY, "text-model"),
        tr.assistant(None, "newest-model"),
    )
    assert _turn(path, "p1") == (OWNER, REPLY, "newest-model")
    bare = tr.write(tmp_path / "bare.jsonl", *OLDER, tr.user(OWNER, "p1"))
    assert _turn(bare, "p1") == (OWNER, None, None)


# ---------------------------------------------------------------------------
# The compaction marker
# ---------------------------------------------------------------------------

NEAR = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
WINDOW = timedelta(seconds=60)


def _at(seconds: float) -> datetime:
    return NEAR + timedelta(seconds=seconds)


def test_the_newest_marker_within_the_window_is_read(tmp_path: Path) -> None:
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user(OWNER, "p1"),
        tr.boundary(_at(-50), "b-1"),
        tr.assistant(REPLY),
        tr.boundary(_at(20), "b-2"),
        tr.user("later", "p2"),
    )
    assert compact_marker(path, near=NEAR, window=WINDOW) == CompactMarker(
        key="b-2", at=_at(20)
    )
    assert compact_marker(path, near=_at(-100), window=WINDOW) == CompactMarker(
        key="b-1", at=_at(-50)
    )


@pytest.mark.parametrize("offset", [-60.0, 60.0])
def test_the_window_holds_its_edges(tmp_path: Path, offset: float) -> None:
    edge = tr.write(tmp_path / "edge.jsonl", tr.boundary(_at(offset), "b-1"))
    assert compact_marker(edge, near=NEAR, window=WINDOW) == CompactMarker(
        key="b-1", at=_at(offset)
    )
    past = tr.write(tmp_path / "past.jsonl", tr.boundary(_at(offset * 61 / 60), "b-1"))
    assert compact_marker(past, near=NEAR, window=WINDOW) is None


def test_a_marker_without_a_uuid_is_keyed_by_its_timestamp(tmp_path: Path) -> None:
    row = tr.boundary(NEAR)
    del row["uuid"]
    path = tr.write(tmp_path / "t.jsonl", row)
    marker = compact_marker(path, near=NEAR, window=WINDOW)
    assert marker == CompactMarker(key=row["timestamp"], at=NEAR)


def test_only_a_compact_boundary_row_is_a_marker(tmp_path: Path) -> None:
    """A row that names the subtype in its text, a system row of another
    subtype, and a boundary with an unreadable stamp are not markers."""
    other = tr.boundary(NEAR, "b-other")
    other["subtype"] = "informational"
    other["content"] = "compact_boundary"
    unreadable = tr.boundary(NEAR, "b-unreadable")
    unreadable["timestamp"] = "not a time"
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user(
            "what does compact_boundary mean", "p1", timestamp="2026-10-19T12:00:00Z"
        ),
        other,
        unreadable,
    )
    assert compact_marker(path, near=NEAR, window=WINDOW) is None


def test_the_marker_is_read_as_far_back_as_the_budget(tmp_path: Path) -> None:
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.boundary(NEAR, "b-1"),
        tr.tool_result(3 * CHUNK, "p1"),
        tr.assistant(REPLY),
    )
    size = path.stat().st_size
    found = compact_marker(
        path, near=NEAR, window=WINDOW, chunk_bytes=CHUNK, budget=size
    )
    assert found == CompactMarker(key="b-1", at=NEAR)
    assert (
        compact_marker(
            path, near=NEAR, window=WINDOW, chunk_bytes=CHUNK, budget=size - 1
        )
        is None
    )


# ---------------------------------------------------------------------------
# The owner's words (RS-F7 part 4: the brief's probe)
# ---------------------------------------------------------------------------


def _wordy(tmp_path: Path) -> Path:
    """Three owner turns with everything the definition passes over between
    them: a reply, a tool result, a reminder the app composed, a
    compaction summary, an interrupt line and a turn another process
    drove."""
    return tr.write(
        tmp_path / "t.jsonl",
        tr.user("first words", "p1"),
        tr.assistant("a reply"),
        tr.tool_result(50, "p1"),
        tr.user("second words", "p2"),
        tr.user("<system-reminder>a reminder</system-reminder>", "p3", isMeta=True),
        {
            "type": "user",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "a summary of the conversation"},
        },
        tr.user("[Request interrupted by user]", "p4"),
        tr.user("an sdk turn", "p5", turnOrigin="sdk"),
        tr.user("third words", "p6"),
    )


def test_owner_words_are_the_owners_rows_oldest_first(tmp_path: Path) -> None:
    path = _wordy(tmp_path)
    assert (
        owner_words(path, chars=12_000, chunk_bytes=CHUNK)
        == "first words\n\nsecond words\n\nthird words"
    )
    # Uncut: a prompt longer than the probe's cut is kept whole.
    long = "x" * (PROMPT_CUT_CHARS + 50)
    tr.append(path, tr.user(long, "p7"))
    assert owner_words(path, chars=12_000, chunk_bytes=CHUNK).endswith(
        "words\n\n" + long
    )


def test_owner_words_keep_the_newest_chars(tmp_path: Path) -> None:
    path = _wordy(tmp_path)
    assert owner_words(path, chars=11, chunk_bytes=CHUNK) == "third words"
    assert owner_words(path, chars=20, chunk_bytes=CHUNK) == "d words\n\nthird words"
    assert owner_words(path, chars=0, chunk_bytes=CHUNK) == ""


def test_owner_words_of_no_file_or_no_owner_row_are_empty(tmp_path: Path) -> None:
    assert owner_words(tmp_path / "missing.jsonl", chars=100) == ""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.assistant("a reply"),
        tr.user("[Request interrupted by user]", "p1"),
        tr.user("an sdk turn", "p2", turnOrigin="sdk"),
    )
    assert owner_words(path, chars=100, chunk_bytes=CHUNK) == ""


def test_owner_words_of_a_path_that_is_no_regular_file_are_empty(
    tmp_path: Path,
) -> None:
    """A named pipe is not opened: a read of it would block the hook's
    worker until something wrote to it."""
    if not hasattr(os, "mkfifo"):  # pragma: no cover - non-unix
        pytest.skip("os.mkfifo not available")
    # POSIX-only; the hasattr guard above is what mypy on Windows narrows on
    # (tests/test_fsutil.py gates its FIFO the same way).
    fifo = tmp_path / "t.jsonl"
    os.mkfifo(fifo)
    assert owner_words(fifo, chars=100) == ""
    assert owner_words(tmp_path, chars=100) == ""


def test_owner_words_read_as_far_back_as_the_budget(tmp_path: Path) -> None:
    path = _wordy(tmp_path)
    data = path.read_bytes()
    last = data.rstrip(b"\n").rfind(b"\n") + 1
    # A budget ending exactly at the newest row's first byte yields it alone.
    assert owner_words(
        path, chars=12_000, chunk_bytes=CHUNK, budget=len(data) - last
    ) == ("third words")


# ---------------------------------------------------------------------------
# Interrupted turns: the interrupt row's promptId names the turn
# ---------------------------------------------------------------------------

SINCE = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _stamp(seconds: int) -> str:
    """A row timestamp `seconds` after `SINCE`."""
    return isoformat_utc(SINCE + timedelta(seconds=seconds))


def test_an_interrupted_turn_is_read_with_the_interrupt_rows_id(
    tmp_path: Path,
) -> None:
    """Newest row first, the interrupt row opens the group and its
    ``promptId`` is the turn's; the owner's row of the same id supplies
    the prompt, the assistant rows between them the partial reply and
    the model, and `at` is the interrupt row's stamp (the newest
    submitted row's). A turn before `since` and the turn after are not
    returned."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user("an older ask", "p0", timestamp=_stamp(-5)),
        tr.assistant("an older reply"),
        tr.user(OWNER, "p1", timestamp=_stamp(1)),
        tr.assistant(None),
        tr.tool_result(40, "p1"),
        tr.assistant("a partial reply"),
        tr.user(
            "[Request interrupted by user for tool use]", "p1", timestamp=_stamp(3)
        ),
        tr.user("ok thanks", "p2", timestamp=_stamp(5)),
        tr.assistant("reply two"),
    )
    (turn,) = interrupted_turns(path, since=SINCE, chunk_bytes=CHUNK)
    assert turn.prompt_id == "p1"
    assert (turn.prompt, turn.reply, turn.model) == (OWNER, "a partial reply", MODEL)
    assert turn.at == SINCE + timedelta(seconds=3)


def test_an_interrupt_row_without_a_prompt_id_names_no_turn(
    tmp_path: Path,
) -> None:
    """A row without ``promptId`` is a turn of its own (as in `stop_turn`):
    an interrupt row carrying none holds no prompt and is dropped, and the
    owner's row before it opens a turn nothing interrupted, so nothing is
    returned. The same when the ids begin after the owner's row."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user(OWNER, "p1", timestamp=_stamp(1)),
        tr.assistant("a partial reply"),
        tr.user("[Request interrupted by user]", timestamp=_stamp(3)),
        tr.user("ok thanks", "p2", timestamp=_stamp(5)),
        tr.assistant("reply two"),
    )
    assert interrupted_turns(path, since=SINCE, chunk_bytes=CHUNK) == []
    path = tr.write(
        tmp_path / "u.jsonl",
        tr.user(OWNER, timestamp=_stamp(1)),
        tr.assistant("a partial reply"),
        tr.user("[Request interrupted by user]", "p1", timestamp=_stamp(3)),
        tr.user("ok thanks", "p2", timestamp=_stamp(5)),
        tr.assistant("reply two"),
    )
    assert interrupted_turns(path, since=SINCE, chunk_bytes=CHUNK) == []


def test_an_unstamped_interrupt_row_takes_the_owners_rows_stamp(
    tmp_path: Path,
) -> None:
    """`at` is the newest submitted row's stamp that parses; a turn none of
    whose submitted rows is stamped is not returned."""
    path = tr.write(
        tmp_path / "t.jsonl",
        tr.user(OWNER, "p1", timestamp=_stamp(1)),
        tr.assistant("a partial reply"),
        tr.user("[Request interrupted by user]", "p1"),
        tr.user("ok thanks", "p2", timestamp=_stamp(5)),
        tr.assistant("reply two"),
    )
    (turn,) = interrupted_turns(path, since=SINCE, chunk_bytes=CHUNK)
    assert (turn.prompt_id, turn.at) == ("p1", SINCE + timedelta(seconds=1))
    path = tr.write(
        tmp_path / "u.jsonl",
        tr.user(OWNER, "p1"),
        tr.assistant("a partial reply"),
        tr.user("[Request interrupted by user]", "p1"),
        tr.user("ok thanks", "p2", timestamp=_stamp(5)),
        tr.assistant("reply two"),
    )
    assert interrupted_turns(path, since=SINCE, chunk_bytes=CHUNK) == []
