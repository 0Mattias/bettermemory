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
import threading
import time
from collections import OrderedDict
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
    # Pending writes that hit `_PENDING_TTL_SECONDS` before confirmation.
    # Drained by `pop_recently_expired()` so handlers can emit one
    # `pending_expired` event per drop and `memory_write_confirm` can
    # tell "expired" apart from "never existed". Garbage-collected on
    # the same eviction pass once each id has been expired for one full
    # TTL — at that point the model has no live reference to it.
    _expired_pending: dict[str, "PendingWrite"] = field(default_factory=dict)
    _expired_pending_at: dict[str, float] = field(default_factory=dict)
    # One-shot per-session marker for the passive curation-pressure
    # check that may inline a hint on the first `memory_write` of a
    # session. Set True the first time the check runs regardless of
    # whether the threshold was crossed — dead_weight, drifted, and
    # endorsement_debt all accumulate across sessions and don't move
    # meaningfully within one, so a single check at first write is
    # the right cadence. Keeps the model from re-walking the event
    # log on every subsequent write.
    curation_hint_checked: bool = False

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
        """Move stale pending writes into the `_expired_pending` queue.

        The handler drains the queue via `pop_recently_expired()` and
        emits a `pending_expired` event for each entry — without that,
        the model has no way to tell that its 61-minute-later "yes,
        save it" confirmation lost the race with the TTL. Entries that
        have themselves been expired for one full TTL are GC'd here:
        once the original 1h window has passed twice, the model isn't
        plausibly still referencing the id.
        """
        now = time.time()
        cutoff = now - _PENDING_TTL_SECONDS
        stale = [pid for pid, p in self.pending_writes.items() if p.created_at < cutoff]
        for pid in stale:
            self._expired_pending[pid] = self.pending_writes.pop(pid)
            self._expired_pending_at[pid] = now
        for pid in list(self._expired_pending_at):
            if self._expired_pending_at[pid] < cutoff:
                self._expired_pending_at.pop(pid, None)
                self._expired_pending.pop(pid, None)

    def pop_recently_expired(self) -> list["PendingWrite"]:
        """Drain and return pending writes evicted since the last drain.

        Returned in insertion order. The handler emits one
        `pending_expired` event per entry; the entries themselves stay
        out of the live `pending_writes` map regardless. Idempotent —
        a second call returns an empty list until the next eviction.
        """
        drained = list(self._expired_pending.values())
        self._expired_pending.clear()
        # `_expired_pending_at` stays populated so `take_pending` can
        # still distinguish "recently expired" from "never existed"
        # for the duration of one TTL window.
        return drained

    def was_recently_expired(self, pending_id: str) -> bool:
        """True if `pending_id` was evicted within the past TTL window."""
        return pending_id in self._expired_pending_at

    # ---- turns and use-tokens -------------------------------------------

    def advance_turn(self) -> int:
        """Bump the per-session turn counter. Returns the new value.

        Called at the entry of every memory_* tool handler so the
        auto-commit pass has a stable monotonic clock to compare token
        ages against. Also evicts wall-clock-expired tokens AND any
        pending writes that crossed their TTL — the latter populates
        `_expired_pending` so `_drain_pending_expired` (handler-side)
        can emit one event per drop and the confirm handler can
        distinguish "expired" from "never existed."
        """
        self.turn_counter += 1
        self._evict_expired_use_tokens()
        self._evict_expired()
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
        self._expired_pending.clear()
        self._expired_pending_at.clear()

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


class SessionRegistry:
    """Per-client `SessionState` map for multi-client server processes.

    Keys are FastMCP `client_id` strings (or `_DEFAULT_CLIENT_KEY` when
    the transport doesn't supply one). States are created lazily on
    first `for_request` for a given key, so an idle client doesn't
    pre-allocate anything.

    Backed by an `OrderedDict` with an LRU eviction cap (`max_clients`,
    default 256): on each `for_request` an existing key is touched to
    the end via `move_to_end`, and inserting past the cap evicts the
    oldest entry. The cap matters under HTTP/SSE transports that fan
    arbitrary client_ids through one server process — without it, the
    map grows unbounded for the lifetime of the process. The stdio
    transport collapses every request into `_DEFAULT_CLIENT_KEY`, so a
    long-running stdio process sees a fixed map of size 1; the LRU is
    inert there.

    A `threading.Lock` guards mutations because the touch+evict pass
    is non-atomic (read, move, possibly pop) and HTTP/SSE transports
    can dispatch concurrent requests. The pre-LRU shape relied on
    `dict.setdefault` being atomic on CPython; the OrderedDict path
    can't replicate that without an explicit lock.

    The registry is intentionally a stateful object rather than a
    module-level singleton: tests construct fresh ones via
    `SessionRegistry()`, and the package-level `get_default_registry()`
    is the single production instance shared across `main()` calls in
    the same process.
    """

    # Default cap. 256 is generous — the typical case is one client
    # per server (stdio). HTTP/SSE deployments with more concurrent
    # clients should pass an explicit value sized to expected fan-out.
    DEFAULT_MAX_CLIENTS = 256

    def __init__(self, max_clients: int = DEFAULT_MAX_CLIENTS) -> None:
        self._states: "OrderedDict[str, SessionState]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_clients = max_clients
        self._evicted_count = 0

    def for_request(self, ctx: "_Ctx | None") -> SessionState:
        key = self._key_for_ctx(ctx)
        # The touch-on-access + insert-with-eviction pass mutates two
        # pieces of state and must be atomic against concurrent callers
        # — otherwise two requests for the same new client_id could
        # each insert a fresh SessionState and one would overwrite the
        # other (losing pending_writes / disabled_scopes / turn_counter).
        # The pre-LRU shape used `dict.setdefault`'s atomicity for the
        # same guarantee; the OrderedDict path needs an explicit lock.
        with self._lock:
            state = self._states.get(key)
            if state is not None:
                # Touch-on-access: move the existing entry to the
                # end so it's the most-recently-used.
                self._states.move_to_end(key)
                return state
            state = SessionState()
            self._states[key] = state
            # Evict the oldest entry if we crossed the cap. `popitem(last=False)`
            # removes the front of the OrderedDict — the least-recently-used.
            if len(self._states) > self.max_clients:
                self._states.popitem(last=False)
                self._evicted_count += 1
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
        with self._lock:
            return set(self._states)

    def stats(self) -> dict[str, int]:
        """Debug-visible counters: current size, lifetime evictions, cap.

        Exposed so HTTP-transport deployments can spot a runaway
        client_id fan-out (where `evicted` climbs monotonically). The
        snapshot is a point-in-time read under the lock; the values
        may drift the moment the lock is released.
        """
        with self._lock:
            return {
                "size": len(self._states),
                "evicted": self._evicted_count,
                "max_clients": self.max_clients,
            }


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
