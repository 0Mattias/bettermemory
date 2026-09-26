"""The event log a test reads back, over a real store.

Emits through the canonical `Recorder` and reads back through the
store's `iter_events`, so the shape always matches what production
writes: a test that asserts a consumer's behaviour fails at suite time
if the producer's field names drift.
"""

from __future__ import annotations

from typing import Any

from bettermemory.events import Recorder


class EventLog:
    def __init__(self, store: Any, session_id: str = "sess-test") -> None:
        self.store = store
        self.session_id = session_id
        self.recorder = Recorder(store=store, session_id=session_id)

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one event and return it as it landed in the log."""
        self.recorder.record(kind, **fields)
        return self.last_event

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self.store.iter_events())

    @property
    def last_event(self) -> dict[str, Any]:
        events = self.events
        if not events:
            raise IndexError("event log is empty")
        return events[-1]
