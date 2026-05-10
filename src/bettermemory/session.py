"""Per-session, in-memory state.

Tracked here:
- Scopes disabled for the rest of the session.
- Pending memory writes awaiting `memory_write_confirm`.
- Pending use-tokens issued by `memory_search` / `memory_show` so older
  retrievals can be auto-committed as `applied` on the next memory_*
  call (the auto-`record_use` flow). The model can override the auto
  outcome by calling `memory_record_use(memory_ids=[...], outcome=...)`
  before the token expires.

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

# Pending use-tokens are evicted on a wall-clock TTL as a safety net for
# very long-lived sessions. The primary expiry is the *turn-counter*
# delta inside `consume_old_tokens` — wall-clock is only the last-resort
# cleanup so the token map can't grow without bound.
_PENDING_USE_TOKEN_TTL_SECONDS = 30 * 60  # 30 minutes

# Default turn delta after which a pending use-token is considered
# "old enough" to auto-commit as `applied`. Two turns means: the search
# happens on turn N, and by turn N+2 (two more memory_* calls later)
# we treat the retrieval as having shaped a response. One turn is too
# eager (the model's very next call could be unrelated); three turns
# is too forgiving (the auto-applied event would land far enough away
# from the retrieval that a `record_use(ignored)` override could miss
# its window). Tunable on the call-site for tests.
DEFAULT_USE_TOKEN_TTL_TURNS = 2


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
class PendingUseToken:
    """A retrieval the model has been told about but not yet recorded
    a `record_use` event for.

    Issued by `memory_search` / `memory_show` and folded into the
    response so the model gets an opaque correlation handle (`use_token`).
    The server tracks the token internally; the next memory_* call from
    the same session lets the auto-commit pass that runs at handler
    entry sweep tokens older than `ttl_turns` and emit one
    `record_use` event per batch with `outcome="applied"` and
    `auto=True`.

    `issued_at_turn` is the session-local turn counter at issue time;
    `issued_at` is the wall-clock for the safety-net eviction.
    """

    token: str
    memory_id: str
    issued_at: float
    issued_at_turn: int


@dataclass
class SessionState:
    """Mutable per-session state. Resets when the server restarts."""

    session_id: str = field(default_factory=_new_session_id)
    disabled_scopes: set[str] = field(default_factory=set)
    pending_writes: dict[str, PendingWrite] = field(default_factory=dict)
    # `pending_use_tokens` is keyed by `memory_id` rather than token
    # value because the override path takes ids (the model rarely sees
    # the opaque token; it sees the memory it's overriding). Token
    # values stay opaque to consumers so they can't leak the id back
    # via the wire shape.
    pending_use_tokens: dict[str, PendingUseToken] = field(default_factory=dict)
    # Monotonic per-session turn counter. Bumped at the entry of every
    # memory_* tool call (via `advance_turn`). Auto-commit decisions
    # are based on the delta between the current turn and each
    # token's `issued_at_turn`.
    turn_counter: int = 0

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

    # ---- turns and use-tokens -------------------------------------------

    def advance_turn(self) -> int:
        """Bump the per-session turn counter. Returns the new value.

        Called at the entry of every memory_* tool handler so the
        auto-commit pass has a stable monotonic clock to compare token
        ages against. Also evicts wall-clock-expired tokens so the
        map can't grow without bound.
        """
        self.turn_counter += 1
        self._evict_expired_use_tokens()
        return self.turn_counter

    def issue_use_tokens(self, memory_ids: list[str]) -> dict[str, str]:
        """Mint one opaque token per `memory_id`.

        Returns `{memory_id: token}`. Re-issuing for an id whose previous
        token is still pending replaces it: the search-then-search-again
        pattern shouldn't accumulate phantom tokens for the same memory.
        Called by `memory_search` and `memory_show` after their event
        records have been emitted.
        """
        out: dict[str, str] = {}
        now = time.time()
        for mid in memory_ids:
            token = "use_" + secrets.token_hex(8)
            self.pending_use_tokens[mid] = PendingUseToken(
                token=token,
                memory_id=mid,
                issued_at=now,
                issued_at_turn=self.turn_counter,
            )
            out[mid] = token
        return out

    def consume_old_tokens(
        self,
        *,
        ttl_turns: int = DEFAULT_USE_TOKEN_TTL_TURNS,
        override_ids: set[str] | None = None,
    ) -> list[str]:
        """Pop and return memory_ids whose tokens are older than `ttl_turns`.

        `override_ids` are excluded — used by the explicit
        `memory_record_use` path so a caller's deliberate choice
        beats the auto-commit. Returned ids are removed from the
        pending map atomically.

        The list is in deterministic order (insertion order, which on
        Python 3.7+ dict preserves) so the resulting auto-commit event
        is reproducible across runs.
        """
        if override_ids is None:
            override_ids = set()
        cutoff_turn = self.turn_counter - ttl_turns
        ready: list[str] = []
        for mid, tok in list(self.pending_use_tokens.items()):
            if mid in override_ids:
                continue
            if tok.issued_at_turn <= cutoff_turn:
                ready.append(mid)
        for mid in ready:
            del self.pending_use_tokens[mid]
        return ready

    def purge_use_token(self, memory_id: str) -> bool:
        """Drop the pending token for `memory_id`. True if one existed.

        Used by the explicit-override path so a `memory_record_use`
        for an id removes its token before the auto-commit pass can
        fire. Idempotent — calling on an unknown id is a no-op.
        """
        return self.pending_use_tokens.pop(memory_id, None) is not None

    def _evict_expired_use_tokens(self) -> None:
        cutoff = time.time() - _PENDING_USE_TOKEN_TTL_SECONDS
        stale = [
            mid
            for mid, tok in self.pending_use_tokens.items()
            if tok.issued_at < cutoff
        ]
        for mid in stale:
            del self.pending_use_tokens[mid]

    # ---- lifecycle -------------------------------------------------------

    def reset(self) -> None:
        # `session_id` is intentionally NOT reset — it's a stable per-process
        # tag used in the event log, and a reset() is meant to clear in-memory
        # session state (disabled scopes, pending writes), not to "rotate" the
        # process identity. The turn counter likewise stays — resetting it
        # mid-session would mis-age any still-pending use-tokens.
        self.disabled_scopes.clear()
        self.pending_writes.clear()
        self.pending_use_tokens.clear()


# Singleton — server.py imports this and threads it into tool handlers.
_state = SessionState()


def get_state() -> SessionState:
    return _state
