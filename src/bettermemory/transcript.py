"""Reading a Claude Code transcript from its end, for the hooks.

Two reads, both newest row first through one reader (`ReverseLines`):

- `stop_turn`, the Stop's turn: the user rows that carry the turn's
  ``promptId`` (the Stop payload's ``prompt_id``, else the newest user
  row's), the owner's text of the newest of them the definition accepts
  (`owner_prompt.owner_prompt`), and the reply and model of the assistant
  rows after it. It never reads a prompt from another turn.
- `compact_marker`, the newest ``compact_boundary`` row stamped near a
  moment: the marker a compaction leaves, which the recall's segments
  open on.

Format reference (Claude Code transcripts as written on 2026-10-19): one
JSON object per line. A user row carries ``promptId``, the harness turn
it belongs to, on the row the turn was submitted with and on every tool
result row of the turn; an assistant row carries none, and belongs to the
turn of the nearest user row before it. One turn can hold several
submitted rows: the owner's words, a task notification delivered into the
turn, an interrupt line, a compaction summary.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .owner_prompt import is_submitted, owner_prompt, text_blocks
from .time_utils import parse_event_ts

#: The bytes one read takes: the tail first, then one chunk at a time
#: toward the start of the file.
CHUNK_BYTES = 1_048_576

#: The most bytes a walk covers, counted back from where the file ended
#: when it was opened. A turn whose tool results outgrew the tail (17 of
#: the 62 owner turns of the 122 rc3 Stops replayed on 2026-10-19 began
#: 1 to 64 MiB before the Stop) is still read; a turn begun further back
#: is not, and its Stop records nothing.
WALK_BYTES = 64 * 1_048_576


class ReverseLines:
    """The complete lines of a file, newest first, without their newline,
    as far back as `budget` bytes from where the file ended when it was
    opened; iterate once.

    Each chunk is read with the one byte before it, which says whether
    the chunk's first byte starts a line. When it does not, the bytes up
    to the chunk's first newline are the end of a line whose start lies
    further back: they are carried into the next, earlier chunk, and a
    line longer than a chunk keeps accumulating until its start arrives.
    A line the budget cuts is not yielded; one that starts exactly where
    the budget ends is. Bytes appended after the open are not read, and
    an OSError ends the iteration. With `needle`, a chunk whose complete
    lines do not contain it yields nothing, so a scan for a rare row
    reads bytes without splitting them; a line it yields may still lack
    the needle when another line of its chunk holds it.

    `reached_start` says, once the iteration is over, whether it reached
    the file's first byte (False when the budget ended it first)."""

    def __init__(
        self,
        path: Path,
        *,
        chunk_bytes: int = CHUNK_BYTES,
        budget: int = WALK_BYTES,
        needle: bytes | None = None,
    ) -> None:
        self._path = path
        self._chunk_bytes = chunk_bytes
        self._budget = budget
        self._needle = needle
        self.reached_start = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            with self._path.open("rb") as handle:
                end = os.fstat(handle.fileno()).st_size
                floor = max(0, end - self._budget)
                # The bytes of the line that runs across `end`, from `end` to
                # its newline; None when a line starts at `end`, which the
                # previous chunk yielded. The file's end is the start of the
                # last line, empty when the file ends with a newline.
                carry: bytes | None = b""
                while end > floor:
                    start = max(floor, end - self._chunk_bytes)
                    lead = 1 if start > 0 else 0
                    handle.seek(start - lead)
                    data = handle.read(end - start + lead)
                    if len(data) != end - start + lead:
                        return  # truncated under the read
                    # Without a carry the chunk's last byte is the newline
                    # before the line already yielded.
                    buf = data + carry if carry is not None else data[:-1]
                    end = start
                    if not lead:
                        lines, carry = buf, None
                    elif buf.startswith(b"\n"):
                        lines, carry = buf[1:], None
                    else:
                        newline = buf.find(b"\n")
                        if newline == -1:
                            carry = buf[1:]
                            continue
                        lines, carry = buf[newline + 1 :], buf[1:newline]
                    if self._needle is not None and self._needle not in lines:
                        continue
                    yield from reversed(lines.split(b"\n"))
                self.reached_start = end == 0
        except OSError:
            return


def _row(line: bytes) -> dict[str, Any] | None:
    """A line's JSON object, or None for a blank line, a broken one, or
    anything else."""
    if not line.strip():
        return None
    try:
        row = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _prompt_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("promptId")
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class StopTurn:
    """The turn a Stop ends, as the transcript holds it.

    `prompt` is the owner's text of the turn's newest submitted row the
    definition accepts, as the probe reads it; None when the turn has
    submitted rows and none is the owner's. `reply` is the newest
    assistant text of the turn after that row, or anywhere in the turn
    when none is the owner's; None when there is none. `model` is the
    newest assistant row's model in the same rows, with or without
    text."""

    prompt: str | None
    reply: str | None
    model: str | None


class _Pending:
    """The assistant rows read since the last user row, newest first:
    the newest text and the newest model among them."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.model: str | None = None

    def take(self, row: Mapping[str, Any]) -> None:
        message = row.get("message")
        if not isinstance(message, dict):
            return
        if self.model is None:
            model = message.get("model")
            if isinstance(model, str) and model:
                self.model = model
        if self.text is None:
            self.text = text_blocks(message.get("content"))


def stop_turn(
    path: Path,
    prompt_id: str | None,
    *,
    chunk_bytes: int = CHUNK_BYTES,
    budget: int = WALK_BYTES,
) -> StopTurn | None:
    """The turn the Stop names, read newest row first; None when the
    transcript does not say what the turn's prompt was.

    The turn is the user rows carrying `prompt_id`; without one, the
    rows carrying the newest user row's ``promptId`` (a tool result row
    carries its turn's id as the submitted row does). Rows of other
    turns newer than the turn's are passed over, and the first user row
    of another turn older than the turn's newest row ends it, so no
    prompt is ever read from another turn. In a transcript whose rows
    carry no ids the turn is its newest submitted row
    (`owner_prompt.is_submitted`) and the tool results after it.

    Within the turn the newest submitted row the definition accepts is
    its prompt, and the walk stops there; the reply and the model come
    from the assistant rows after it. When the turn ends, or the file
    starts, with submitted rows of the turn read and none accepted, the
    turn is not the owner's (`prompt` None). None when no submitted row
    of the turn was read, or when the budget ended inside the turn before
    an accepted row or the turn's start was read: the caller then records
    nothing, as it does for an unreadable file."""
    lines = ReverseLines(path, chunk_bytes=chunk_bytes, budget=budget)
    target = prompt_id
    in_turn = False
    submitted = False
    pending = _Pending()
    reply: str | None = None
    model: str | None = None
    for line in lines:
        row = _row(line)
        if row is None:
            continue
        kind = row.get("type")
        if kind == "assistant":
            pending.take(row)
            continue
        if kind != "user":
            continue
        row_id = _prompt_id(row)
        if not in_turn:
            if target is not None and row_id != target:
                pending = _Pending()  # a newer turn's rows
                continue
            target, in_turn = row_id, True
        elif row_id != target:
            break  # an older turn: this one began after it
        if reply is None:
            reply = pending.text
        if model is None:
            model = pending.model
        pending = _Pending()
        if not is_submitted(row):
            continue
        submitted = True
        prompt = owner_prompt(row)
        if prompt is not None:
            return StopTurn(prompt=prompt, reply=reply, model=model)
        if target is None:
            break  # without ids, a submitted row ends its turn
    else:
        if in_turn and not lines.reached_start:
            return None  # the budget ended inside the turn: undecided
    if not submitted:
        return None
    return StopTurn(prompt=None, reply=reply, model=model)


@dataclass(frozen=True)
class CompactMarker:
    """A ``compact_boundary`` row: `key` is its ``uuid`` (its timestamp
    when it has none), `at` its timestamp."""

    key: str
    at: datetime


def compact_marker(
    path: Path,
    *,
    near: datetime,
    window: timedelta,
    chunk_bytes: int = CHUNK_BYTES,
    budget: int = WALK_BYTES,
) -> CompactMarker | None:
    """The newest ``compact_boundary`` row of the transcript stamped
    within `window` of `near`, read newest first and as far back as
    `budget`; None when there is none. Only the chunks that hold the
    subtype's name are split and parsed (the reader's `needle`)."""
    lines = ReverseLines(
        path, chunk_bytes=chunk_bytes, budget=budget, needle=b"compact_boundary"
    )
    for line in lines:
        row = _row(line)
        if (
            row is None
            or row.get("type") != "system"
            or row.get("subtype") != "compact_boundary"
        ):
            continue
        stamp = parse_event_ts(row.get("timestamp"))
        if stamp is None or abs(stamp - near) > window:
            continue
        uuid = row.get("uuid")
        key = uuid if isinstance(uuid, str) and uuid else str(row.get("timestamp"))
        return CompactMarker(key=key, at=stamp)
    return None


__all__ = [
    "CHUNK_BYTES",
    "WALK_BYTES",
    "CompactMarker",
    "ReverseLines",
    "StopTurn",
    "compact_marker",
    "stop_turn",
]
