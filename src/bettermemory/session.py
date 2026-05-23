"""Per-session, in-memory state.

Tracked here:
- Scopes disabled for the rest of the session.
- Pending memory writes awaiting `memory_write_confirm`.
- Pending use-tokens issued by `memory_search` / `memory_show` so older
  retrievals can be auto-committed as `applied` on the next memory_*
  call (the auto-`record_use` flow). The model can override the auto
  outcome by calling `memory_record_use(memory_ids=[...], outcome=...)`
  before the token expires.

For the stdio transport (one MCP client per server process), a single
`SessionState` is correct: there's exactly one client to track. For
transports that serve multiple clients from one server process (HTTP,
SSE), each client needs its own state — otherwise client A's pending
write could be confirmed by client B, and client A's `disabled_scopes`
would silently bleed into client B's searches. The `SessionRegistry`
in this module is the routing layer: it hands out (and lazily creates)
a distinct `SessionState` per client identifier.

The single-state and registry shapes are unified through the
`SessionSource` protocol so tests can pass a concrete `SessionState`
directly (no per-client routing needed when there's only one) and
production can pass a `SessionRegistry` (per-client when a stable
client_id is available, falling back to a shared "default" state when
not). `server._register_tools` calls `sessions.for_request(ctx)` at
the entry of every tool handler; the resolution layer is invisible
to the handler bodies.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias

if TYPE_CHECKING:
    # `mcp.server.fastmcp.Context` is the FastMCP request-scoped context
    # that exposes `client_id` and `request_id`. It's imported under
    # TYPE_CHECKING so the session module stays usable in environments
    # that don't have the MCP server extras loaded (the same way the
    # rest of the package handles optional imports). `Context` is
    # generic over three type parameters that callers here don't
    # constrain — alias it as `_Ctx` so mypy gets the explicit
    # `Any, Any, Any` once instead of every annotation site repeating it.
    from mcp.server.fastmcp import Context

    _Ctx: TypeAlias = Context[Any, Any, Any]


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

    # ---- SessionSource protocol -----------------------------------------

    def for_request(self, ctx: "_Ctx | None") -> "SessionState":
        """Return this state regardless of `ctx`.

        Lets a bare `SessionState` satisfy the `SessionSource` protocol,
        so tests that construct a single state and pass it into
        `build_server(state=...)` keep working — the handlers call
        `state.for_request(ctx)` uniformly and get the same instance
        back. Per-client routing only kicks in when a `SessionRegistry`
        is used instead.
        """
        return self


class SessionSource(Protocol):
    """The minimum interface every tool handler depends on for state.

    Both `SessionState` (single-state, test-friendly) and
    `SessionRegistry` (per-client routing, production-friendly) satisfy
    this protocol, so `_register_tools` can take either without
    branching. `for_request(ctx)` is the entry point: pass the
    FastMCP `Context` (or `None` when not running under FastMCP) and
    receive the right `SessionState` for this request.
    """

    def for_request(self, ctx: "_Ctx | None") -> SessionState: ...


# Key under which clients that don't expose a stable identifier (e.g.
# stdio transport, where `ctx.client_id` may be None) share a single
# SessionState. Anything else opts into per-client isolation by passing
# a real client_id through.
_DEFAULT_CLIENT_KEY = "__default__"


@dataclass
class SessionRegistry:
    """Per-client `SessionState` map for multi-client server processes.

    Keys are FastMCP `client_id` strings (or `_DEFAULT_CLIENT_KEY` when
    the transport doesn't supply one). States are created lazily on
    first `for_request` for a given key, so an idle client doesn't
    pre-allocate anything. Eviction is not currently performed — the
    state objects are small (a handful of dicts) and the realistic
    fan-out is one-digit clients per server process. If a long-running
    server starts seeing hundreds of distinct client_ids, swap the
    plain dict for an LRU.

    The registry is intentionally a stateful object rather than a
    module-level singleton: tests construct fresh ones via
    `SessionRegistry()`, and the package-level `get_default_registry()`
    is the single production instance shared across `main()` calls in
    the same process.
    """

    _states: dict[str, SessionState] = field(default_factory=dict)

    def for_request(self, ctx: "_Ctx | None") -> SessionState:
        key = self._key_for_ctx(ctx)
        # `setdefault` is atomic on CPython dict, so two concurrent
        # callers observing a missing key both receive the same
        # SessionState instance — the alternative (`get` then
        # `__setitem__`) is a TOCTOU window where the second writer
        # wipes the first writer's `pending_writes` / `disabled_scopes`
        # / `turn_counter`. Today stdio collapses every request into
        # `_DEFAULT_CLIENT_KEY`, so this race is dormant; the moment
        # an HTTP/SSE transport starts fanning distinct `client_id`s
        # in parallel (anticipated in the class docstring above) the
        # `setdefault` is what keeps each client's state intact.
        state = self._states.get(key)
        if state is None:
            state = self._states.setdefault(key, SessionState())
        return state

    @staticmethod
    def _key_for_ctx(ctx: "_Ctx | None") -> str:
        if ctx is None:
            return _DEFAULT_CLIENT_KEY
        try:
            client_id = ctx.client_id
        except (AttributeError, ValueError):
            # `ctx.client_id` reads the request context and may raise
            # ValueError if no request is in progress (FastMCP construct
            # outside a tool call). Treat that as "no identifier" and
            # bucket into the default; the alternative would be to
            # crash the tool call for a degenerate context shape.
            return _DEFAULT_CLIENT_KEY
        if not client_id:
            return _DEFAULT_CLIENT_KEY
        return str(client_id)

    def known_keys(self) -> set[str]:
        """Snapshot of the registered session keys. Test-only; lets
        suites assert that two distinct clients produced two distinct
        states, or that one client reused the same state across calls."""
        return set(self._states)


# Process-wide default registry. `main()` uses this; tests that want
# isolation construct their own via `SessionRegistry()`.
_default_registry = SessionRegistry()


def get_default_registry() -> SessionRegistry:
    """Return the process-wide default registry.

    Production code reaches for this when no explicit registry was
    passed; tests construct their own to keep per-test state
    isolated. The registry is mutable, so callers that depend on
    starting clean should construct their own — don't rely on
    `get_default_registry()` being empty.
    """
    return _default_registry


def get_state() -> SessionState:
    """Back-compat shim for callers that want a single `SessionState`.

    Equivalent to `get_default_registry().for_request(None)` — returns
    the "no-client-id" entry of the process-wide registry. Existing
    tests and the stdio-transport entry point keep working; new code
    that wants per-client isolation should call `for_request(ctx)` on
    a registry instead.
    """
    return _default_registry.for_request(None)
