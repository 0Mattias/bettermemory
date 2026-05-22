"""Test helpers that emit events via the real ``Recorder``.

The 2.6.2 and 2.6.3 releases each shipped a production bug because a
consumer (``consolidate.find_demotion_candidates``,
``llm._collect_contradiction_targets``) read event fields under a
field-name shape that didn't match what the canonical ``Recorder``
emits. Tests passed because the test fixtures hand-built event dicts
that *also* used the wrong shape, so the production-versus-test
divergence never surfaced under CI.

This module is the structural answer. ``EventLog`` wraps a real
``Recorder`` writing into a real ``tmp_path``-rooted directory. Tests
call ``log.emit(kind, **fields)`` instead of hand-rolling
``{"kind": ..., ...}`` literals. The event that lands in
``log.events`` matches production's shape *byte-for-byte* because it
goes through the same code path the in-process MCP handler and the
Stop hook use.

Discipline:

- New tests for event consumers SHOULD use ``EventLog`` instead of
  hand-built event dicts.
- Existing tests that hand-build can keep working through consumer-
  side legacy-name fallback (see ``consolidate.py:398``,
  ``llm.py:896-906``), but each surface that gains a new field is
  one ``EventLog`` migration away from being immune to the
  fixture-divergence class.

Usage::

    def test_demote_dead_weight(event_log):
        a = _make_memory("foo")
        event_log.emit("write", id=a.id, status="committed")
        event_log.emit("search", returned=[a.id], relevance=["high"])
        candidates = find_demotion_candidates(events=event_log.events)
        ...
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bettermemory.events import EVENT_LOG_FILENAME, Recorder, iter_events


class EventLog:
    """Real ``Recorder``-backed event log for tests.

    Emits via the canonical ``Recorder``; reads back via
    ``iter_events``. The shape always matches what production writes,
    so a test that asserts a consumer's behaviour will fail at
    suite time if the producer's field names ever drift.

    Construct directly with a path, or use the ``event_log`` pytest
    fixture (defined in ``conftest.py``).
    """

    def __init__(self, root: Path, session_id: str = "sess-test") -> None:
        self.root = root
        self.session_id = session_id
        self.recorder = Recorder(root=root, session_id=session_id)

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one event of ``kind`` with ``fields`` merged in.

        Returns the decoded event as it landed in the log — the same
        shape any consumer would see via ``iter_events``. Useful for
        ``assert event == log.emit(...)``-style tests that pin the
        canonical shape explicitly.
        """
        self.recorder.record(kind, **fields)
        return self.last_event

    @property
    def events(self) -> list[dict[str, Any]]:
        """All events from the active log, in append order."""
        return list(iter_events(self.root))

    @property
    def last_event(self) -> dict[str, Any]:
        """Most-recent event from the active log.

        Reads only the last line for speed; on a fresh empty log
        raises ``IndexError`` rather than silently returning ``None``
        so a caller that expects an event but the recorder is
        disabled or the path is wrong fails loudly.
        """
        path = self.root / EVENT_LOG_FILENAME
        # Read the last line directly. Cheaper than parsing the whole
        # log when callers only want the most-recent record (common
        # case: assert immediately after ``emit``).
        with path.open("rb") as fh:
            content = fh.read().rstrip(b"\n")
        if not content:
            raise IndexError("event log is empty")
        last_newline = content.rfind(b"\n")
        line = content[last_newline + 1 :] if last_newline != -1 else content
        return json.loads(line)
