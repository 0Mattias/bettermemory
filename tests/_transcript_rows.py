"""Synthetic Claude Code transcript rows for the hook tests: the shapes
`transcript.stop_turn` and `transcript.compact_marker` read, with no
text from any real transcript."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from bettermemory.time_utils import isoformat_utc


def user(text: str, prompt_id: str | None = None, **marks: Any) -> dict[str, Any]:
    """A submitted user row; `marks` are row keys such as ``turnOrigin``
    or ``isMeta``."""
    row: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": text}}
    if prompt_id is not None:
        row["promptId"] = prompt_id
    row.update(marks)
    return row


def tool_result(size: int, prompt_id: str | None = None) -> dict[str, Any]:
    """A user row carrying a tool result of about `size` bytes."""
    row: dict[str, Any] = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "r" * size}
            ],
        },
    }
    if prompt_id is not None:
        row["promptId"] = prompt_id
    return row


def assistant(text: str | None, model: str = "claude-test") -> dict[str, Any]:
    """An assistant row: one text block, or a tool use when `text` is None."""
    block: dict[str, Any] = (
        {"type": "text", "text": text}
        if text is not None
        else {"type": "tool_use", "name": "Bash", "input": {}}
    )
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "content": [block]},
    }


def boundary(at: datetime, uuid: str = "boundary-1") -> dict[str, Any]:
    """A compaction marker: the `compact_boundary` system row."""
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": uuid,
        "timestamp": isoformat_utc(at),
        "compactMetadata": {"trigger": "manual"},
    }


def dump(*rows: dict[str, Any], newline: str = "\n") -> bytes:
    """`rows` as JSONL bytes, each line ending in `newline`, non-ASCII text
    raw as Claude Code writes it."""
    return "".join(
        json.dumps(row, ensure_ascii=False) + newline for row in rows
    ).encode("utf-8")


def write(path: Path, *rows: dict[str, Any], newline: str = "\n") -> Path:
    """`rows` as a JSONL file at `path` (`dump`)."""
    path.write_bytes(dump(*rows, newline=newline))
    return path


def append(path: Path, *rows: dict[str, Any]) -> Path:
    """`rows` added at the end of the JSONL file at `path` (`dump`)."""
    with path.open("ab") as handle:
        handle.write(dump(*rows))
    return path
