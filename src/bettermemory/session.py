"""Per-session, in-memory state.

Tracked here:
- Scopes disabled for the rest of the session.
- Pending memory writes awaiting `memory_write_confirm`.

MCP servers run one process per client, so a module-level singleton is fine
for the MVP — see Limitations in README. Phase 2 may push this into SQLite
if multi-session sharing becomes useful.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any


# Pending writes expire after this many seconds — keeps stale entries from
# accumulating if the consumer never confirms or cancels.
_PENDING_TTL_SECONDS = 60 * 60  # 1 hour


def _new_session_id() -> str:
    """Per-process session identifier. Stamped onto every event in the log
    so retrieval-vs-write streams can be correlated to a single client."""
    return "sess_" + secrets.token_hex(8)


@dataclass
class PendingWrite:
    """A `memory_write` call awaiting confirmation."""

    pending_id: str
    payload: dict[str, Any]  # the kwargs to pass to Store.write
    created_at: float  # monotonic-ish epoch seconds


@dataclass
class SessionState:
    """Mutable per-session state. Resets when the server restarts."""

    session_id: str = field(default_factory=_new_session_id)
    disabled_scopes: set[str] = field(default_factory=set)
    pending_writes: dict[str, PendingWrite] = field(default_factory=dict)

    # ---- scopes ----------------------------------------------------------

    def disable(self, scope: str) -> set[str]:
        self.disabled_scopes.add(scope)
        return set(self.disabled_scopes)

    def enable(self, scope: str) -> set[str]:
        self.disabled_scopes.discard(scope)
        return set(self.disabled_scopes)

    # ---- pending writes --------------------------------------------------

    def stage_write(self, payload: dict[str, Any]) -> PendingWrite:
        """Park a write request awaiting `memory_write_confirm`."""
        self._evict_expired()
        pending_id = "pending_" + secrets.token_hex(8)
        pending = PendingWrite(
            pending_id=pending_id,
            payload=payload,
            created_at=time.time(),
        )
        self.pending_writes[pending_id] = pending
        return pending

    def take_pending(self, pending_id: str) -> PendingWrite | None:
        """Pop a pending write off the queue. Returns None if missing/expired."""
        self._evict_expired()
        return self.pending_writes.pop(pending_id, None)

    def cancel_pending(self, pending_id: str) -> bool:
        """Discard a pending write without committing. True if it existed."""
        return self.pending_writes.pop(pending_id, None) is not None

    def _evict_expired(self) -> None:
        cutoff = time.time() - _PENDING_TTL_SECONDS
        stale = [pid for pid, p in self.pending_writes.items() if p.created_at < cutoff]
        for pid in stale:
            del self.pending_writes[pid]

    # ---- lifecycle -------------------------------------------------------

    def reset(self) -> None:
        # `session_id` is intentionally NOT reset — it's a stable per-process
        # tag used in the event log, and a reset() is meant to clear in-memory
        # session state (disabled scopes, pending writes), not to "rotate" the
        # process identity.
        self.disabled_scopes.clear()
        self.pending_writes.clear()


# Singleton — server.py imports this and threads it into tool handlers.
_state = SessionState()


def get_state() -> SessionState:
    return _state
