"""Unit + integration tests for `SessionRegistry`.

The registry is the routing layer for multi-client server processes:
each FastMCP `Context.client_id` resolves to its own `SessionState`, so
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

from dataclasses import dataclass
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


# `SessionRegistry._key_for_ctx` only reads `ctx.client_id` — a duck-typed
# stand-in is enough for unit tests. Using a real FastMCP Context would
# require constructing a full request context just to set one attribute.
@dataclass
class _FakeCtx:
    client_id: str | None = None


def _fake_ctx(*, client_id: str | None = None) -> Any:
    """Return a `_FakeCtx` typed as `Any` so strict mypy accepts it where
    `for_request` expects a real FastMCP `Context`. The registry only
    reads `.client_id`, so the duck-typed stand-in is structurally
    compatible; the cast is purely a type-checker concession."""
    return _FakeCtx(client_id=client_id)


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
