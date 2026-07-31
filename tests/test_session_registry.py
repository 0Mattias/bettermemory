"""Unit + integration tests for `SessionRegistry`.

The registry is the routing layer for multi-client server processes:
each request's client id resolves to its own `SessionState`, so
pending writes / disabled scopes / use-tokens from one client can't
leak into another. The pre-registry shape was a process-level
singleton — fine for stdio (one client per process), wrong the moment
two clients hit the same process.

Two test layers:

* Unit: `SessionRegistry` directly — key derivation from a fake
  Context, lazy state creation, idempotent reuse, the no-ctx /
  no-client-id fallback.
* Integration: build two servers backed by the *same* `SessionRegistry`,
  issue a pending write from one with a forged `client_id` context,
  confirm/cancel from the other with a different `client_id`, and
  assert the pending-write isolation holds end-to-end.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import (
    SessionRegistry,
    SessionState,
    get_default_registry,
)
from bettermemory.store import Store
from ._mcp import fake_ctx as _mcp_fake_ctx


def _fake_ctx(*, client_id: str | None = None) -> Any:
    """A stand-in `Context` carrying `client_id`, from `tests/_mcp.py`.

    Kept as a keyword-only wrapper so the call sites below read the way
    they always have. The forged shape itself lives in tests/_mcp.py
    because it mirrors the SDK's request shape, which moved in the 2.x
    port — see that module for why two private copies of it were a tax.
    """
    return _mcp_fake_ctx(client_id)


# ---------------------------------------------------------------------------
# Unit: SessionRegistry routing
# ---------------------------------------------------------------------------


def test_registry_returns_same_state_for_same_client_id() -> None:
    """Calling `for_request` twice with the same client_id returns the same
    state. Without this, every tool call would mint a fresh state and
    pending writes / use-tokens would never carry between calls within
    one client session."""
    registry = SessionRegistry()
    ctx_a1 = _fake_ctx(client_id="client-A")
    ctx_a2 = _fake_ctx(client_id="client-A")

    state1 = registry.for_request(ctx_a1)
    state2 = registry.for_request(ctx_a2)

    assert state1 is state2


def test_registry_returns_distinct_states_for_distinct_client_ids() -> None:
    """The whole point of the registry: client A and client B get
    isolated state. Identity (not just equality) — the SessionState
    objects must be different instances so mutations on one don't bleed."""
    registry = SessionRegistry()
    state_a = registry.for_request(_fake_ctx(client_id="A"))
    state_b = registry.for_request(_fake_ctx(client_id="B"))

    assert state_a is not state_b
    assert state_a.session_id != state_b.session_id


def test_registry_falls_back_to_default_state_when_ctx_is_none() -> None:
    """A None ctx (in-process call, no FastMCP request) maps to the
    default key. The stdio transport's `build_server` recorder
    construction calls `for_request(None)` to read a stable
    session_id — that path must work without raising."""
    registry = SessionRegistry()
    state = registry.for_request(None)
    state_again = registry.for_request(None)
    assert state is state_again


def test_registry_falls_back_to_default_state_when_client_id_is_empty() -> None:
    """An empty-string or None `client_id` on the Context maps to the
    default bucket. Some transports populate client_id late or not at
    all; treat absence the same as None rather than minting a new
    state for every empty-id request."""
    registry = SessionRegistry()
    state_empty = registry.for_request(_fake_ctx(client_id=""))
    state_none_ctx = registry.for_request(None)

    assert state_empty is state_none_ctx


def test_registry_does_not_collide_default_key_with_real_client_id() -> None:
    """A client that picks `__default__` as its literal client_id (or
    whatever the internal sentinel happens to be) shouldn't end up
    sharing state with the no-id bucket. We aren't going to enforce
    that contract on FastMCP's client_id field; this test just pins the
    current behavior so a future change is intentional rather than
    accidental."""
    registry = SessionRegistry()
    default_state = registry.for_request(None)
    spoof_state = registry.for_request(_fake_ctx(client_id="__default__"))

    # Today the keys collide (both bucket into the same sentinel).
    # If that ever changes, this assertion flips — fine, just be
    # deliberate about it.
    assert default_state is spoof_state


def test_registry_known_keys_reflects_inserted_clients() -> None:
    registry = SessionRegistry()
    registry.for_request(_fake_ctx(client_id="A"))
    registry.for_request(_fake_ctx(client_id="B"))
    registry.for_request(None)

    keys = registry.known_keys()
    assert "A" in keys
    assert "B" in keys
    # The "no-id" bucket is keyed under the module-level sentinel.
    assert any(k.startswith("__") for k in keys)


def test_session_state_satisfies_session_source_protocol() -> None:
    """A bare `SessionState` (back-compat shape) must satisfy the
    `SessionSource` protocol: `for_request(ctx)` returns itself,
    regardless of ctx. This is what lets every existing test pass
    `state=SessionState()` to `build_server` and keep working."""
    shared = SessionState()
    assert shared.for_request(None) is shared
    assert shared.for_request(_fake_ctx(client_id="anyone")) is shared


def test_get_default_registry_is_a_stable_singleton() -> None:
    """`get_default_registry()` returns the same registry across calls —
    that's what makes "no explicit state" production calls share state
    correctly across the process. Tests that want isolation construct
    their own `SessionRegistry`."""
    assert get_default_registry() is get_default_registry()


# ---------------------------------------------------------------------------
# Integration: two clients on one server are isolated
# ---------------------------------------------------------------------------


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool with explicit ctx routing.

    `server.call_tool(name, kwargs)` goes through FastMCP's schema-driven
    dispatch, which strips any kwarg whose corresponding parameter isn't
    in the public tool schema — including `ctx`, because FastMCP excludes
    `Context`-typed parameters from the schema by design. Routing the
    test ctx through call_tool would silently land at `ctx=None` in the
    handler, and every "client" would resolve to the default registry
    bucket. That's the exact failure mode this test exists to catch.

    Reaching the unwrapped handler via `_tool_manager.get_tool(name).fn`
    bypasses the schema layer. We're testing the registry routing on the
    server-side glue, not the wire format — direct call is the right
    surface. FastMCP itself injects ctx the same way at runtime, just
    through its own request-context lookup rather than a test kwarg.
    """
    fn = server._tool_manager.get_tool(name).fn
    return await fn(**kwargs)


@pytest.fixture
def two_client_server(
    tmp_path: Path,
) -> tuple[Any, SessionRegistry, dict[str, str]]:
    """A confirming-write server backed by a single `SessionRegistry`.

    `require_write_confirmation=True` forces `memory_write` into the
    pending-tier path — that's the surface where session-state
    isolation matters most: a pending write from one client must not
    be confirmable by another.

    Returns (server, registry, client_ctxs) — client_ctxs is a small
    dict of name -> client_id strings so callers can vary it per
    request without re-typing the literal.
    """
    from bettermemory.config import BehaviorConfig

    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=BehaviorConfig(require_write_confirmation=True),
    )
    registry = SessionRegistry()
    server = build_server(config=cfg, store=Store(tmp_path), state=registry)
    return server, registry, {"alice": "client-alice", "bob": "client-bob"}


async def test_pending_write_is_isolated_between_clients(
    two_client_server: tuple[Any, SessionRegistry, dict[str, str]],
) -> None:
    """The load-bearing audit fix: alice stages a pending write, bob
    can't confirm or cancel it.

    Tests inject a forged Context via the `ctx` kwarg that every
    handler now accepts. FastMCP normally injects this at the wire
    layer; passing it explicitly here lets us simulate two distinct
    clients hitting the same in-process server.
    """
    server, registry, ids = two_client_server

    # Alice opens a pending write under her client_id.
    alice_ctx = _fake_ctx(client_id=ids["alice"])
    pending = await _call(
        server,
        "memory_write",
        content="alice's durable preference about tabs vs spaces",
        scopes=["learning-style"],
        ctx=alice_ctx,
    )
    assert pending["status"] == "pending"
    pending_id = pending["pending_id"]

    # Bob tries to cancel — under bob's client_id, the registry hands
    # him a separate SessionState that knows nothing about alice's
    # pending. The cancel reports "existed=False", confirming the
    # isolation.
    bob_ctx = _fake_ctx(client_id=ids["bob"])
    bob_cancel = await _call(
        server,
        "memory_write_cancel",
        pending_id=pending_id,
        ctx=bob_ctx,
    )
    assert bob_cancel["existed"] is False, (
        "bob should not be able to cancel alice's pending write — if this "
        "assertion fails, the SessionRegistry isn't isolating pending state "
        "between clients and the audit's M2 fix has regressed."
    )

    # Bob trying to confirm raises — same reason.
    with pytest.raises(Exception, match="no pending write"):
        await _call(
            server,
            "memory_write_confirm",
            pending_id=pending_id,
            ctx=bob_ctx,
        )

    # Alice's pending is still hers to commit.
    committed = await _call(
        server,
        "memory_write_confirm",
        pending_id=pending_id,
        ctx=alice_ctx,
    )
    assert committed["status"] == "committed"


async def test_disabled_scopes_are_isolated_between_clients(
    two_client_server: tuple[Any, SessionRegistry, dict[str, str]],
) -> None:
    """A scope disabled by alice must not affect bob's searches.

    Pre-registry, `state.disabled_scopes` was a process-level set;
    once alice ran `memory_scope_disable("tools")`, bob's
    `memory_search` would silently drop tool-scoped hits without his
    consent. The registry isolates `disabled_scopes` per client.
    """
    server, registry, ids = two_client_server
    alice_ctx = _fake_ctx(client_id=ids["alice"])
    bob_ctx = _fake_ctx(client_id=ids["bob"])

    # Alice disables `tools`.
    alice_disable = await _call(
        server,
        "memory_scope_disable",
        scope="tools",
        ctx=alice_ctx,
    )
    assert "tools" in alice_disable["disabled_scopes"]

    # Bob queries his own disabled set — should be empty.
    bob_disable_check = await _call(
        server,
        "memory_scope_enable",  # idempotent; "enable a scope that wasn't disabled" reports empty
        scope="never-disabled",
        ctx=bob_ctx,
    )
    assert bob_disable_check["disabled_scopes"] == [], (
        "bob's disabled_scopes leaked from alice's session — the registry "
        "isn't isolating per-client session state."
    )


# ---------------------------------------------------------------------------
# Unit: LRU eviction
# ---------------------------------------------------------------------------


def test_session_registry_evicts_oldest_when_full() -> None:
    """Inserting one client_id past `max_clients` evicts the oldest entry.

    Without the LRU cap, a long-running HTTP/SSE server would accumulate
    state for every distinct client_id ever connected — an unbounded
    memory leak. The eviction kicks in on the insert-past-cap path,
    drops the front of the OrderedDict (least-recently-used), and bumps
    the `evicted` counter exposed via `stats()`.
    """
    registry = SessionRegistry(max_clients=3)
    s0 = registry.for_request(_fake_ctx(client_id="c0"))
    registry.for_request(_fake_ctx(client_id="c1"))
    registry.for_request(_fake_ctx(client_id="c2"))
    assert registry.stats() == {"size": 3, "evicted": 0, "max_clients": 3}

    # One past the cap — c0 (oldest) gets evicted.
    registry.for_request(_fake_ctx(client_id="c3"))
    stats = registry.stats()
    assert stats["size"] == 3
    assert stats["evicted"] == 1
    assert stats["max_clients"] == 3

    keys = registry.known_keys()
    assert "c0" not in keys, "oldest entry should have been evicted"
    assert {"c1", "c2", "c3"} <= keys

    # And a fresh `for_request` for c0 mints a NEW state (different
    # identity from the original) — proving the original was actually
    # dropped, not just moved.
    s0_again = registry.for_request(_fake_ctx(client_id="c0"))
    assert s0_again is not s0
    # Inserting c0 fresh evicted the next-oldest (c1) since the cap was full.
    assert registry.stats()["evicted"] == 2


def test_session_registry_touches_on_access() -> None:
    """A `for_request` against an EXISTING key moves it to the end of
    the LRU. So if A, B, C are inserted in order, touching A again,
    then inserting D, the entry evicted must be B — A is now the
    most-recently-used, not the oldest.

    Without touch-on-access, a long-lived client would still get
    evicted just because newer clients keep landing — the cap would
    behave as a FIFO instead of an LRU.
    """
    registry = SessionRegistry(max_clients=3)
    s_a = registry.for_request(_fake_ctx(client_id="A"))
    registry.for_request(_fake_ctx(client_id="B"))
    registry.for_request(_fake_ctx(client_id="C"))

    # Touch A — it should now be the most-recently-used.
    s_a_again = registry.for_request(_fake_ctx(client_id="A"))
    assert s_a_again is s_a  # same instance — touch, not re-create

    # Insert D — pushes past the cap, evicts the current oldest (B).
    registry.for_request(_fake_ctx(client_id="D"))

    keys = registry.known_keys()
    assert "B" not in keys, (
        "B should have been evicted as the oldest after A was touched; "
        "if A was evicted instead, touch-on-access isn't working"
    )
    assert {"A", "C", "D"} <= keys
    assert registry.stats()["evicted"] == 1


# ---------------------------------------------------------------------------
# Concurrency: the threading.Lock actually serializes contention
# ---------------------------------------------------------------------------
#
# The sequential LRU tests above prove the OrderedDict mechanics are right;
# these prove the lock isn't load-bearing in theory only. HTTP/SSE transports
# can dispatch concurrent requests against the same registry, and the
# touch+insert+evict pass is a read-modify-write that races without the lock.
# Without these tests, removing the lock would still pass the rest of the
# suite — the regression would only show up in production under fan-out.


def test_session_registry_same_key_concurrent_for_request_returns_one_state() -> None:
    """N threads racing on the same fresh client_id must all receive the
    same `SessionState` instance.

    Without the lock, two threads observing `self._states.get(key) is None`
    simultaneously would each construct a fresh `SessionState` and the
    second `self._states[key] = state` would silently overwrite the first.
    Any pending writes / use-tokens on the lost state would vanish. We
    can't observe the overwrite directly (both states would have the
    same external shape), but we CAN observe the identity divergence:
    if any two threads got different `is`-identity states, the lock
    failed to serialize.
    """
    registry = SessionRegistry(max_clients=64)
    n_threads = 32
    start = threading.Event()
    results: list[SessionState] = []
    results_lock = threading.Lock()

    def worker() -> None:
        start.wait()  # release all threads simultaneously for maximum contention
        state = registry.for_request(_fake_ctx(client_id="hot-key"))
        with results_lock:
            results.append(state)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "worker hung — possible deadlock in for_request"

    assert len(results) == n_threads
    first = results[0]
    assert all(s is first for s in results), (
        "concurrent for_request on the same key returned different SessionState "
        "instances — the lock failed to serialize the read-modify-write pass and "
        "later writers overwrote earlier ones. Pending writes on the lost states "
        "would silently vanish in production under HTTP/SSE fan-out."
    )
    assert registry.stats()["size"] == 1


def test_session_registry_concurrent_distinct_inserts_preserve_size_invariant() -> None:
    """Under concurrent insertion of distinct keys past the cap, the
    invariant `size + evicted == total_unique_inserts` must hold.

    This catches a different failure mode than the same-key test: races
    in the eviction path. If `len(self._states) > self.max_clients` and
    the subsequent `popitem(last=False)` weren't atomic with the insert,
    two threads could each see size==cap+1, each pop, and the registry
    would end up under-sized (with `evicted` over-counted) or, worse,
    leave the cap exceeded. The accounting equation makes the failure
    visible without needing to assert specific surviving keys (which
    would depend on scheduling).
    """
    registry = SessionRegistry(max_clients=16)
    n_threads = 8
    inserts_per_thread = 25  # 200 distinct keys total, well past cap
    start = threading.Event()
    errors: list[BaseException] = []

    def worker(thread_idx: int) -> None:
        try:
            start.wait()
            for i in range(inserts_per_thread):
                registry.for_request(_fake_ctx(client_id=f"t{thread_idx}-k{i}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "worker hung — possible deadlock in for_request"

    assert not errors, f"workers raised: {errors!r}"

    total_inserts = n_threads * inserts_per_thread
    stats = registry.stats()
    assert stats["size"] == registry.max_clients, (
        f"size={stats['size']} != cap={registry.max_clients}; "
        f"the eviction/insert pair raced and left the registry mis-sized"
    )
    assert stats["size"] + stats["evicted"] == total_inserts, (
        f"size ({stats['size']}) + evicted ({stats['evicted']}) "
        f"!= total inserts ({total_inserts}); the eviction counter and "
        f"the dict size disagree, which means an insert or pop was lost "
        f"to a race"
    )


# ---------------------------------------------------------------------------
# The silent-injection guard the mcp 2.x port needs
# ---------------------------------------------------------------------------


def test_the_handler_context_alias_is_the_sdk_class_injection_matches() -> None:
    """Every test above forges its own `ctx`, so none of them can see the
    one way this whole layer fails silently.

    The SDK decides whether to inject a `Context` by comparing a handler's
    resolved type hints against its own `Context` CLASS. If the alias in
    `handlers/_shared.py` ever resolves to a different class than the
    installed SDK injects — a partially-applied mcp 2.x port, a shim branch
    that picks wrong, two SDK copies on `sys.path` — injection stops firing,
    `ctx` arrives as `None` on every call, and `SessionRegistry._key_for_ctx`
    buckets every client into `_DEFAULT_CLIENT_KEY`. By design: that method
    swallows exactly this shape rather than crashing a tool call.

    So there is no exception, no failing assertion anywhere else in this
    file, and multi-client isolation is simply gone — the failure the
    registry exists to prevent, arriving with a green suite.

    This asserts the identity positively: the annotation a registered
    handler actually carries must resolve to the class the SDK injects.
    """
    import typing

    from mcp.server.mcpserver import Context as SDKContext

    from bettermemory.handlers import _shared

    # The project's alias fills the SDK generic with `Any`. `Context` is a
    # pydantic model, so subscripting it builds a real concrete SUBCLASS
    # rather than a typing alias object — `get_origin` returns None for it
    # and `is` would never hold. Subclass identity is also the relation the
    # SDK's own injection uses, so it is the right thing to assert: 1.x
    # resolves a handler's `Context`-typed parameter with a lenient
    # issubclass check, and 2.0.0 matches the class out of
    # `typing.get_type_hints`.
    alias = typing.get_origin(_shared.Context) or _shared.Context
    assert isinstance(alias, type) and issubclass(alias, SDKContext), (
        f"handlers._shared.Context resolves to {alias!r}, which is not the "
        f"{SDKContext!r} the installed SDK injects. Context injection will "
        f"not fire and every client will collapse into the default session."
    )


def test_a_registered_handler_still_declares_a_context_parameter(
    two_client_server: tuple[Any, SessionRegistry, dict[str, str]],
) -> None:
    """Companion to the identity check: the alias being right is worth
    nothing if the parameter carrying it is dropped from a handler.

    Reads the unwrapped function out of the tool registry — the same
    surface `_call` above uses — and asserts the `ctx` parameter survives
    with an annotation that resolves, rather than a stale string left
    behind by `from __future__ import annotations`.
    """
    import typing

    server, _registry, _clients = two_client_server
    fn = server._tool_manager.get_tool("memory_write").fn
    hints = typing.get_type_hints(fn)
    assert "ctx" in hints, (
        "memory_write no longer declares a `ctx` parameter; session routing "
        "has nothing to key on and every caller shares one SessionState"
    )
