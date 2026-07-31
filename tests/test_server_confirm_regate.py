"""Confirm-time re-gating, and the staged writes that outlive a restart.

Two holes closed together, because the second serializes what the first
adds to a `PendingWrite`.

**The re-gate.** `memory_write_confirm` replayed the staged payload through
ZERO gates. A pending write can sit for an hour and the store is not frozen
while it does: the duplicate it is now a duplicate OF, or the tombstone it
now overlaps, can both land during the wait. Every one of those committed
unchecked. The naive fix is worse than the hole — `take_pending` POPS, so a
gate refusal after it destroys the write it refused, leaving nothing to
re-confirm and orphaning the `episode_promote` linkage. Hence the peek /
judge / take order these tests pin, and the `pending_retained` contract.

The second trap is invisible until you write the test: the original call's
`force` / `acknowledge_*` flags were not stored on the `PendingWrite`, so a
re-gate with everything False re-refuses exactly the writes the caller
already forced or acknowledged at staging time — and does it with a hint
naming overrides that `memory_write_confirm` has no parameter for.

**The persistence.** Pending writes were an in-process dict that died on
restart with no event: a user answering "yes, save it" after a server
restart got "no pending write with id …", indistinguishable from a typo.
The sidecar copies `ProposalQueue`'s idiom, and the tests below pin the
three things a naive copy gets wrong — the payload does not JSON-round-trip
(`origin` is a pydantic model), the rows must stay isolated per client (the
`SessionRegistry` exists to stop cross-client confirm and a shared file
would hand that back), and the TTL has to be re-applied on load or the
"expired" / "never existed" distinction degrades to the latter.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call, fake_ctx as _mcp_fake_ctx

import json
import sys
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from bettermemory import session as session_mod
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.handlers.write import (
    CredentialGate,
    DedupActiveGate,
    DedupTombstoneGate,
    GateContext,
    _CONFIRM_GATES,
    _confirm_gate_context,
    _WRITE_GATES,
)
from bettermemory.models import Category, Confidence, Source
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import (
    GATE_FLAG_KEYS,
    PENDING_WRITES_FILENAME,
    PendingWrite,
    PendingWriteLog,
    SessionRegistry,
    SessionState,
)
from bettermemory.store import Store


def _fake_ctx(client_id: str) -> Any:
    """A stand-in `Context` carrying `client_id`, from `tests/_mcp.py`.

    The forged shape used to be a private copy here and a byte-identical
    private copy in `tests/test_session_registry.py`; the 2.x port moved
    the client id off `Context.client_id` and both broke at once. It lives
    in tests/_mcp.py now for the same reason the return-shape unpack does.
    """
    return _mcp_fake_ctx(client_id)


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


async def _call_as(server: Any, name: str, client_id: str, **kwargs: Any) -> Any:
    """Invoke a tool as a named client.

    Reaches the handler function directly rather than going through
    `call_tool`, because `ctx` is injected by the wire layer and is not a
    schema parameter — passed through `call_tool` it never arrives, and
    every client silently collapses into the default bucket. That failure
    mode makes an isolation test pass for the wrong reason, which is the
    one thing an isolation test must not do. Same idiom as
    `tests/test_session_registry.py`."""
    fn = server._tool_manager.get_tool(name).fn
    return await fn(ctx=_fake_ctx(client_id), **kwargs)


def _boot(memory_dir: Path, *, sessions: Any = None) -> tuple[Any, Any]:
    """One server process over `memory_dir`. Calling it twice against the
    same directory is what "a restart" means in this module: fresh
    `SessionState`, fresh `Store`, same disk."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = sessions if sessions is not None else SessionState()
    session_id = getattr(state, "session_id", "sess_registry")
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=Recorder(root=memory_dir, session_id=session_id),
    )
    return server, state


@pytest.fixture
def booted(memory_dir: Path) -> tuple[Any, SessionState, Path]:
    server, state = _boot(memory_dir)
    return server, state, memory_dir


async def _stage(server: Any, content: str, **kwargs: Any) -> str:
    """Stage a write and return its pending id. `user-inference` is the
    structural always-pending tier, so this needs no config flag."""
    res = await _call(
        server,
        "memory_write",
        content=content,
        scopes=kwargs.pop("scopes", ["learning-style"]),
        category=kwargs.pop("category", "user-inference"),
        **kwargs,
    )
    assert res["status"] == "pending", res
    return str(res["pending_id"])


def _backdate_on_disk(memory_dir: Path) -> None:
    """Age every staged row past the TTL, in the sidecar rather than in
    memory — the point of these tests is what the LOAD does with a row it
    did not write."""
    sidecar = memory_dir / PENDING_WRITES_FILENAME
    rows = [json.loads(line) for line in sidecar.read_text().splitlines() if line]
    for row in rows:
        row["created_at"] = time.time() - session_mod._PENDING_TTL_SECONDS - 1
    sidecar.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _lands_during_the_wait(memory_dir: Path, content: str) -> Any:
    """Put a memory in the store behind the server's back.

    Straight through `Store.write` rather than `memory_write`, because what
    is being modelled is "the store is not frozen while a confirmation is
    outstanding" — a sync pull, another client, a CLI import. Routing it
    through the write gates instead would couple these tests to whether
    THIS body happens to clear every one of them, which is a question about
    a different gate."""
    return Store(memory_dir).write(content=content, scopes=["learning-style"])


# ---------------------------------------------------------------------------
# The hole: a duplicate that lands during the TTL
# ---------------------------------------------------------------------------


async def test_duplicate_landing_during_the_ttl_is_refused_at_confirm(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """THE DEFECT. Confirm ran no gates, so the twin written between
    staging and confirmation was committed alongside its original — the
    exact parallel entry `DedupActiveGate` exists to prevent, reachable by
    doing nothing but waiting."""
    server, state, memory_dir = booted
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(server, body)

    # The twin lands while the confirmation is outstanding.
    twin = _lands_during_the_wait(memory_dir, body)

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "duplicate", res
    assert [m["id"] for m in res["matches"]] == [twin.id]
    # One memory in the store, not two.
    assert len(Store(memory_dir).load_all()) == 1


async def test_a_refused_confirm_leaves_the_pending_confirmable(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """The trap that makes the naive fix wrong: `take_pending` pops, so a
    gate refusal placed after it would destroy the staged write it just
    refused. The caller would be told "duplicate" AND lose the thing it was
    asked to decide about."""
    server, state, memory_dir = booted
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(server, body)
    _lands_during_the_wait(memory_dir, body)

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["pending_retained"] is True
    assert res["pending_id"] == pending_id
    assert pending_id in state.pending_writes

    # Still a live handle: cancelling it reports that it existed.
    cancelled = await _call(server, "memory_write_cancel", pending_id=pending_id)
    assert cancelled["existed"] is True


async def test_a_refused_confirm_says_where_the_overrides_live(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """The gate hints offer `force=True` — a `memory_write` parameter.
    `memory_write_confirm` takes a pending id and nothing else, so a hint
    handed through unedited tells the model to retry a call that cannot
    carry the fix."""
    server, _, memory_dir = booted
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(server, body)
    _lands_during_the_wait(memory_dir, body)

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert "memory_write_cancel" in res["hint"]
    assert "memory_write parameters" in res["hint"]


async def test_a_tombstone_landing_during_the_ttl_is_refused_at_confirm(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """The other store-dependent verdict: the claim was removed WHILE the
    confirmation was outstanding. Committing it anyway re-creates, as a
    parallel entry, exactly what the user had just deleted."""
    server, state, memory_dir = booted
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(server, body)

    twin = _lands_during_the_wait(memory_dir, body)
    await _call(server, "memory_remove", id=twin.id, reason="superseded by a rewrite")

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "previously_removed", res
    assert pending_id in state.pending_writes
    assert Store(memory_dir).load_all() == []


async def test_a_clean_confirm_still_commits_and_consumes(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """The re-gate must be invisible on the path everyone actually takes."""
    server, state, memory_dir = booted
    pending_id = await _stage(server, "The user prefers tabs over spaces.")

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "committed"
    assert pending_id not in state.pending_writes
    assert len(Store(memory_dir).load_all()) == 1


# ---------------------------------------------------------------------------
# Trap two: the flags the original call carried
# ---------------------------------------------------------------------------


async def test_a_force_staged_write_confirms_cleanly(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """THE DEFECT, second half. `force=True` skips `DedupActiveGate` at
    staging time; a re-gate that judges with all flags False re-applies the
    very gate the caller overrode, and refuses at confirm a write it
    accepted at stage. Worse, it reads as a NEW finding rather than the one
    already answered."""
    server, state, memory_dir = booted
    body = "The user prefers terse code-driven explanations over prose walls."
    _lands_during_the_wait(memory_dir, body)

    pending_id = await _stage(server, body, force=True)
    staged = state.pending_writes[pending_id]
    assert staged.gate_flags["force"] is True

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "committed", res
    assert len(Store(memory_dir).load_all()) == 2


async def test_an_acknowledged_credential_confirms_cleanly(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """Same trap on the credential axis, and the sharper one: the body has
    not changed since staging, so a `credential_warning` at confirm is
    guaranteed to be the identical verdict the caller already overrode."""
    server, state, memory_dir = booted
    pending_id = await _stage(
        server,
        "The user's throwaway demo login is password=hunter2, published in "
        "the workshop handout.",
        acknowledge_credential=True,
    )
    assert state.pending_writes[pending_id].gate_flags["acknowledge_credential"]

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "committed", res


async def test_an_unacknowledged_credential_is_still_refused_at_confirm(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """The flags carry the caller's decision forward; they must not become
    a blanket amnesty. A staged write that never acknowledged anything is
    still judged."""
    server, state, memory_dir = booted
    payload = {
        "content": "aws key AKIAIOSFODNN7EXAMPLE stays on the deploy box",
        "scopes": ["infrastructure"],
        "confidence": Confidence.MEDIUM,
        "source": Source.EXPLICIT,
        "category": Category.USER_INFERENCE,
        "origin": None,
    }
    # Staged directly: `memory_write` would have refused this at the door,
    # which is the point — the gate has to still be there at confirm.
    pending = state.stage_write(payload)

    res = await _call(server, "memory_write_confirm", pending_id=pending.pending_id)
    assert res["status"] == "credential_warning", res
    assert res["pending_retained"] is True
    assert Store(booted[2]).load_all() == []


def test_every_staged_gate_flag_reaches_the_confirm_context() -> None:
    """`GATE_FLAG_KEYS` is a roster of `GateContext` FIELD NAMES, and
    `_confirm_gate_context` spells the assignment out one by one for the
    type checker. Two hand-maintained lists that must agree: a flag added
    to one and forgotten in the other is silently dropped at confirm, which
    is precisely the bug the flags exist to fix."""
    field_names = {f.name for f in fields(GateContext)}
    assert set(GATE_FLAG_KEYS) <= field_names

    for key in GATE_FLAG_KEYS:
        pending = PendingWrite(
            pending_id="pending_probe",
            payload={"content": "x", "scopes": ["tools"]},
            created_at=time.time(),
            gate_flags={key: True},
        )
        gc = _confirm_gate_context(pending)
        assert getattr(gc, key) is True, f"{key} did not reach the GateContext"
        for other in GATE_FLAG_KEYS:
            if other != key:
                assert getattr(gc, other) is False


def test_the_confirm_chain_is_the_store_dependent_subset() -> None:
    """Which gates re-run is a decision, not an accident.

    Adding a BODY gate here creates a refusal with no legal escape:
    `memory_write_confirm` has no acknowledge parameter, and the body has
    not changed since staging, so the verdict can only repeat one the
    caller already answered. Adding `PendingGate` would stage the write a
    second time. The instances are the chain's own, so confirm inherits
    `_WRITE_GATES`'s ordering rather than pinning a second copy of it."""
    assert tuple(type(g) for g in _CONFIRM_GATES) == (
        CredentialGate,
        DedupActiveGate,
        DedupTombstoneGate,
    )
    for gate in _CONFIRM_GATES:
        assert any(gate is g for g in _WRITE_GATES)
    order = [i for i, g in enumerate(_WRITE_GATES) if g in _CONFIRM_GATES]
    assert order == sorted(order)


async def test_a_refused_confirm_preserves_the_promotion_linkage(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """A staged `episode_promote` carries a linkage that tells the confirm
    handler to delete the source episode. A refusal is not a resolution, so
    the linkage — like the pending itself — has to survive for the retry.
    `memory_write_cancel` pops it deliberately (the pending is gone by
    then); the refusal path must not."""
    server, state, memory_dir = booted
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(server, body)
    state.stash_promotion_episode(pending_id, "sess_source", "ep_source")
    _lands_during_the_wait(memory_dir, body)

    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "duplicate"
    assert state.take_promotion_episode(pending_id) == ("sess_source", "ep_source")


async def test_a_refused_confirm_records_a_refusal_not_a_commit(
    booted: tuple[Any, SessionState, Path],
) -> None:
    """The audit trail has to show what happened. A refusal that recorded
    nothing would leave a `write` event with `status='pending'` as the last
    word on this id, which reads as an outstanding confirmation forever;
    one that recorded a plain `write_confirm` would read as a commit."""
    server, _, memory_dir = booted
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(server, body)
    _lands_during_the_wait(memory_dir, body)
    await _call(server, "memory_write_confirm", pending_id=pending_id)

    confirms = [e for e in iter_events(memory_dir) if e["kind"] == "write_confirm"]
    assert len(confirms) == 1
    assert confirms[0]["status"] == "duplicate"
    assert confirms[0]["pending_retained"] is True
    assert confirms[0]["pending_id"] == pending_id
    # No committed memory id, and no `episode_id` — `episode_handoff` reads
    # that key as proof a deferred promotion's delete ran, and nothing was
    # deleted here.
    assert "id" not in confirms[0]
    assert confirms[0].get("episode_id") is None


# ---------------------------------------------------------------------------
# Persistence: the staged write survives the process that staged it
# ---------------------------------------------------------------------------


async def test_a_staged_write_survives_a_restart(memory_dir: Path) -> None:
    """THE DEFECT. `pending_writes` was an in-process dict, so a restart
    dropped every staged write with no event — and the user answering "yes,
    save it" afterwards got "no pending write with id …", the same message a
    typo produces."""
    first, _ = _boot(memory_dir)
    pending_id = await _stage(first, "The user prefers tabs over spaces.")

    second, state = _boot(memory_dir)
    res = await _call(second, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "committed", res
    assert len(Store(memory_dir).load_all()) == 1


async def test_the_persisted_payload_round_trips_origin_and_enums(
    memory_dir: Path,
) -> None:
    """`payload` holds three `str`-subclass enums (which `json.dumps`
    happens to emit) and an `origin` that is a pydantic model (which makes
    it RAISE). A naive `json.dumps(pending.payload)` writes nothing at all,
    and because persistence is best-effort the failure is a log line — the
    feature silently degrades to the in-process behaviour it replaced.

    So this asserts the row reached disk, and that what comes back is typed
    the way `Store.write` needs rather than left as loose strings."""
    first, _ = _boot(memory_dir)
    pending_id = await _stage(first, "The user prefers tabs over spaces.")

    sidecar = memory_dir / PENDING_WRITES_FILENAME
    assert sidecar.exists(), "nothing was persisted"
    rows = [json.loads(line) for line in sidecar.read_text().splitlines() if line]
    assert [r["pending_id"] for r in rows] == [pending_id]
    assert rows[0]["payload"]["origin"] is not None

    reloaded = SessionState()
    reloaded.bind_pending_log(memory_dir)
    payload = reloaded.pending_writes[pending_id].payload
    assert isinstance(payload["category"], Category)
    assert isinstance(payload["confidence"], Confidence)
    assert isinstance(payload["source"], Source)
    assert isinstance(payload["origin"], Origin)

    # And the rehydrated payload is still a legal `Store.write` call.
    second, _ = _boot(memory_dir)
    res = await _call(second, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "committed"
    assert Store(memory_dir).load_all()[0].origin is not None


async def test_the_gate_flags_survive_the_restart_too(memory_dir: Path) -> None:
    """The ordering constraint between the two halves of this module: the
    flags the re-gate depends on are useless if the sidecar drops them, and
    a restart would then re-refuse a force-staged write on a store that has
    legitimately grown a twin since."""
    first, _ = _boot(memory_dir)
    body = "The user prefers terse code-driven explanations over prose walls."
    _lands_during_the_wait(memory_dir, body)
    pending_id = await _stage(first, body, force=True)

    second, state = _boot(memory_dir)
    state.bind_pending_log(memory_dir)
    assert state.pending_writes[pending_id].gate_flags["force"] is True
    res = await _call(second, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "committed", res


async def test_the_promotion_linkage_survives_the_restart_too(
    memory_dir: Path,
) -> None:
    """A regression this feature would otherwise CREATE.

    Before the sidecar, a restart lost the staged write, so a promotion's
    source episode legitimately stayed on disk for a retry. Persisting the
    write but not its linkage produces a state the old code could not: the
    confirm commits the memory AND leaves the journal entry behind as the
    duplicate the linkage exists to delete — and `episode_handoff` reads the
    resulting unstamped `write_confirm` as an unresolved promotion."""
    first, state = _boot(memory_dir)
    pending_id = await _stage(first, "The user prefers tabs over spaces.")
    state.stash_promotion_episode(pending_id, "sess_source", "ep_source")

    _, reloaded = _boot(memory_dir)
    reloaded.bind_pending_log(memory_dir)
    assert reloaded.take_promotion_episode(pending_id) == ("sess_source", "ep_source")


async def test_a_restart_does_not_hand_one_client_another_client_s_write(
    memory_dir: Path,
) -> None:
    """The `SessionRegistry` exists to stop client B confirming client A's
    staged user-inference write. A sidecar keyed by pending id alone would
    hand that back through the disk — every restart would re-pool the
    staged writes of every client that ever used the store."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    first = build_server(config=cfg, store=Store(memory_dir), state=SessionRegistry())
    res = await _call_as(
        first,
        "memory_write",
        "client-alice",
        content="The user prefers tabs over spaces.",
        scopes=["learning-style"],
        category="user-inference",
    )
    pending_id = res["pending_id"]

    # A new process over the same store.
    second = build_server(config=cfg, store=Store(memory_dir), state=SessionRegistry())
    with pytest.raises(ValueError, match="no pending write"):
        await _call_as(
            second, "memory_write_confirm", "client-bob", pending_id=pending_id
        )
    committed = await _call_as(
        second, "memory_write_confirm", "client-alice", pending_id=pending_id
    )
    assert committed["status"] == "committed"


async def test_one_client_s_rewrite_keeps_the_other_client_s_rows(
    memory_dir: Path,
) -> None:
    """The sidecar is one file for every client of a store, and every
    mutation rewrites it. A `save` that wrote only the calling client's
    rows would silently delete everyone else's staged writes — a data-loss
    bug that only shows up with two clients and a restart between them."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionRegistry())
    ids = {}
    for name in ("alice", "bob"):
        res = await _call_as(
            server,
            "memory_write",
            f"client-{name}",
            content=f"The user prefers {name}-flavoured tooling defaults.",
            scopes=["learning-style"],
            category="user-inference",
        )
        ids[name] = res["pending_id"]

    # Alice resolves hers; bob's row must be untouched by that rewrite.
    await _call_as(
        server, "memory_write_cancel", "client-alice", pending_id=ids["alice"]
    )
    log = PendingWriteLog(memory_dir)
    assert [r.pending_id for r in log.load("client-alice")] == []
    assert [r.pending_id for r in log.load("client-bob")] == [ids["bob"]]


async def test_expiry_across_a_restart_reads_as_expired_not_missing(
    memory_dir: Path,
) -> None:
    """The TTL has to be re-applied on LOAD. Without it a restart either
    resurrects a write the 1-hour window already killed, or — if the row is
    dropped on sight — collapses "expired" into "never existed" and takes
    the targeted error message with it. Both were live risks: the two cases
    raise different `ValueError`s precisely so the model can tell a lost
    race from a typo."""
    first, _ = _boot(memory_dir)
    pending_id = await _stage(first, "The user prefers tabs over spaces.")
    _backdate_on_disk(memory_dir)

    second, state = _boot(memory_dir)
    with pytest.raises(Exception, match="expired before confirmation"):
        await _call(second, "memory_write_confirm", pending_id=pending_id)
    assert state.was_recently_expired(pending_id)

    # And the eviction is VISIBLE: the drain that runs on the binding turn
    # emits the event, rather than the loss being silent as it was before.
    expired = [e for e in iter_events(memory_dir) if e["kind"] == "pending_expired"]
    assert [e["pending_id"] for e in expired] == [pending_id]


async def test_the_load_itself_applies_the_ttl(memory_dir: Path) -> None:
    """The sibling above passes even if the load adopts a stale row
    verbatim, because `_evict_expired` runs on every access path and would
    sweep it a moment later. So this one observes the state the instant the
    load returns — `pending_writes` and `was_recently_expired` are both
    plain reads — which is the only place the load's own verdict is
    visible. Separate store, because draining the eviction queue here would
    consume the event the sibling asserts on."""
    first, _ = _boot(memory_dir)
    pending_id = await _stage(first, "The user prefers tabs over spaces.")
    _backdate_on_disk(memory_dir)

    fresh = SessionState()
    fresh.bind_pending_log(memory_dir)
    assert pending_id not in fresh.pending_writes
    assert fresh.was_recently_expired(pending_id)
    assert [p.pending_id for p in fresh.pop_recently_expired()] == [pending_id]


async def test_an_expiry_marker_keeps_its_original_window_across_restarts(
    memory_dir: Path,
) -> None:
    """The `was_recently_expired` window is one TTL from the EVICTION, and
    it has to stay anchored there. Re-stamping it at every load would make
    a store that is opened daily answer "expired" forever; dropping the
    marker would make the second restart answer "never existed" for a write
    the user is still asking about."""
    first, state = _boot(memory_dir)
    pending_id = await _stage(first, "The user prefers tabs over spaces.")
    state.pending_writes[pending_id].created_at = (
        time.time() - session_mod._PENDING_TTL_SECONDS - 1
    )
    await _call(first, "memory_list")  # advances the turn -> evicts + persists

    log = PendingWriteLog(memory_dir)
    (row,) = log.load(state.client_key)
    assert row.pending is None and row.expired_at is not None
    evicted_at = row.expired_at

    second, second_state = _boot(memory_dir)
    with pytest.raises(Exception, match="expired before confirmation"):
        await _call(second, "memory_write_confirm", pending_id=pending_id)
    (row_again,) = PendingWriteLog(memory_dir).load(second_state.client_key)
    assert row_again.expired_at == evicted_at
    # Read through the private map because it is the only observation point:
    # `was_recently_expired` answers True either way, and a re-stamped
    # timestamp only shows up an hour later, as a marker that refuses to age
    # out on a store that gets opened every day.
    assert second_state._expired_pending_at[pending_id] == evicted_at


async def test_a_committed_or_cancelled_write_leaves_no_row_behind(
    memory_dir: Path,
) -> None:
    """The mirror is a mirror. A confirm or a cancel that cleared memory but
    not disk would let the next restart re-adopt a write that has already
    been committed once — a duplicate the caller never asked for.

    "No row" means no ADOPTABLE row: `load` is the adoption feed and the
    consumed tombstone the claim leaves behind is deliberately invisible to
    it (see the resurrection tests below, which are the reason the
    tombstone exists at all)."""
    server, state = _boot(memory_dir)
    confirmed = await _stage(server, "The user prefers tabs over spaces.")
    cancelled = await _stage(server, "The user reviews PRs in the morning.")
    await _call(server, "memory_write_confirm", pending_id=confirmed)
    await _call(server, "memory_write_cancel", pending_id=cancelled)

    assert PendingWriteLog(memory_dir).load(state.client_key) == []
    _, reloaded = _boot(memory_dir)
    reloaded.bind_pending_log(memory_dir)
    assert reloaded.pending_writes == {}


async def test_the_sidecar_is_written_private(memory_dir: Path) -> None:
    """A staged row is a memory body the user has NOT agreed to store —
    the user-inference tier stages precisely so they can veto it. It is
    written with the mode set before the rename, so there is no
    world-readable instant at the visible name (`atomic_write_bytes`'s
    `mode_before_rename`, the discipline the proposals queue uses for raw
    captured text)."""
    server, _ = _boot(memory_dir)
    await _stage(server, "The user prefers tabs over spaces.")
    sidecar = memory_dir / PENDING_WRITES_FILENAME
    assert sidecar.exists()
    # Windows has no POSIX mode bits — it reports 0o666 whatever we ask for.
    # Same skip the episode-store privacy test takes.
    if sys.platform != "win32":
        mode = sidecar.stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)


async def test_a_malformed_row_does_not_blind_the_rest(memory_dir: Path) -> None:
    """Same defensive-skip discipline as `ProposalQueue.load`: one
    unparseable line must not cost the other staged writes. Nothing here is
    a durable memory, so the skip (destructive at the next rewrite) is the
    same trade that queue already makes."""
    first, state = _boot(memory_dir)
    keeper = await _stage(first, "The user prefers tabs over spaces.")
    sidecar = memory_dir / PENDING_WRITES_FILENAME
    sidecar.write_text("{not json at all\n" + sidecar.read_text())

    _, reloaded = _boot(memory_dir)
    reloaded.bind_pending_log(memory_dir)
    assert list(reloaded.pending_writes) == [keeper]


async def test_an_unwritable_sidecar_does_not_break_staging(
    memory_dir: Path,
) -> None:
    """Persistence is best-effort by contract. A store root that cannot be
    written — a read-only mount, a permission change, a directory sitting
    where the file should be — degrades this feature to the in-process
    behaviour it replaced. It must never turn a working `memory_write` into
    an error the model has to reason about."""
    (memory_dir / PENDING_WRITES_FILENAME).mkdir()
    server, state = _boot(memory_dir)
    pending_id = await _stage(server, "The user prefers tabs over spaces.")
    assert pending_id in state.pending_writes
    res = await _call(server, "memory_write_confirm", pending_id=pending_id)
    assert res["status"] == "committed"


async def test_reset_clears_the_mirror_and_not_just_the_memory(
    memory_dir: Path,
) -> None:
    """`reset()` is the "forget this session" lever. A version that cleared
    only the in-memory maps would leave every staged write on disk, and the
    next `bind_pending_log` would hand them straight back — a reset that
    un-resets itself."""
    server, state = _boot(memory_dir)
    await _stage(server, "The user prefers tabs over spaces.")
    state.reset()

    assert PendingWriteLog(memory_dir).load(state.client_key) == []
    _, reloaded = _boot(memory_dir)
    reloaded.bind_pending_log(memory_dir)
    assert reloaded.pending_writes == {}


def test_an_unbound_state_writes_nothing(memory_dir: Path) -> None:
    """`bind_pending_log` is the opt-in. Every `SessionState()` a test or an
    embedder constructs must stay a pure in-memory object until someone
    tells it which store it belongs to — otherwise the mirror starts
    guessing at a root."""
    state = SessionState()
    state.stage_write({"content": "x", "scopes": ["tools"]})
    assert list(memory_dir.iterdir()) == []


async def test_the_default_write_path_stays_a_single_rewrite_per_turn(
    memory_dir: Path,
) -> None:
    """`_evict_expired` runs at the entry of EVERY tool call. Persisting
    unconditionally there would take the sidecar lock and rewrite the file
    on each one, to write back exactly what it just read. The mirror
    updates when the staged set moves and not otherwise."""
    server, state = _boot(memory_dir)
    await _stage(server, "The user prefers tabs over spaces.")
    sidecar = memory_dir / PENDING_WRITES_FILENAME
    before = sidecar.stat().st_mtime_ns

    for _ in range(3):
        await _call(server, "memory_list")
    assert sidecar.stat().st_mtime_ns == before


# ---------------------------------------------------------------------------
# The sidecar is shared: no snapshot may resurrect a consumed pending id
# ---------------------------------------------------------------------------
#
# Persisting the staged set created a failure mode the in-process dict could
# not have: two live `SessionState`s can now share a client key AND a store
# root (two stdio servers both bucket into `__default__`), and a snapshot
# write from either one silently speaks for both. Nothing else in the module
# above can see this, because every test there uses one live state at a time.


def _rows_on_disk(memory_dir: Path) -> list[dict[str, Any]]:
    sidecar = memory_dir / PENDING_WRITES_FILENAME
    if not sidecar.exists():
        return []
    return [json.loads(line) for line in sidecar.read_text().splitlines() if line]


async def test_a_stale_writer_cannot_resurrect_a_consumed_pending(
    memory_dir: Path,
) -> None:
    """THE DEFECT. `save` replaced ALL of a client's rows with the calling
    state's live snapshot, so a state that staged BEFORE another one
    confirmed wrote the CONSUMED row back onto disk on its very next stage.
    The id became confirmable a second time: one `memory_write`, two durable
    memories with identical bodies, one pending id.

    `force=True` is not decoration — it is what makes the sequence bite. The
    confirm-time dedup gate masks the resurrection for any body dedup can
    see; with the duplicate check overridden at staging time, nothing else
    stands between the second confirm and the store."""
    a_server, _ = _boot(memory_dir)
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(a_server, body, force=True)

    # A second process, same client key, same store: it confirms.
    b_server, _ = _boot(memory_dir)
    first = await _call(b_server, "memory_write_confirm", pending_id=pending_id)
    assert first["status"] == "committed", first

    # A is still live and knows nothing about that commit. Its next stage
    # must carry its OWN new row to disk without speaking for the consumed
    # one.
    other_id = await _stage(
        a_server, "The user reviews PRs first thing in the morning."
    )

    c_server, _ = _boot(memory_dir)
    with pytest.raises(Exception, match="no pending write"):
        await _call(c_server, "memory_write_confirm", pending_id=pending_id)
    assert len(Store(memory_dir).load_all()) == 1

    # And the fix is a merge, not a wipe: A's unrelated staged write is
    # still on disk and still confirmable from the fresh process.
    still_good = await _call(c_server, "memory_write_confirm", pending_id=other_id)
    assert still_good["status"] == "committed", still_good
    assert len(Store(memory_dir).load_all()) == 2


async def test_a_live_state_cannot_confirm_what_another_state_committed(
    memory_dir: Path,
) -> None:
    """The same double-commit reached from the other side: a state that
    adopted the row while it was still live holds it in memory, so no amount
    of care at ADOPTION time can help. The claim has to be durable and
    one-shot, taken at confirm time under the lock."""
    a_server, _ = _boot(memory_dir)
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(a_server, body, force=True)

    # C boots and adopts the row while it is still live.
    c_server, c_state = _boot(memory_dir)
    c_state.bind_pending_log(memory_dir)
    assert pending_id in c_state.pending_writes

    committed = await _call(a_server, "memory_write_confirm", pending_id=pending_id)
    assert committed["status"] == "committed"

    with pytest.raises(Exception, match="no pending write"):
        await _call(c_server, "memory_write_confirm", pending_id=pending_id)
    assert len(Store(memory_dir).load_all()) == 1
    assert pending_id not in c_state.pending_writes


def test_take_pending_raises_when_another_session_won_the_claim(
    memory_dir: Path,
) -> None:
    """The last inch, and the reason this is a raise rather than a None:
    `memory_write_confirm` ignores `take_pending`'s return value and commits
    the payload it peeked a few lines earlier. A quiet None for a REFUSED
    claim would therefore write the memory anyway — the failure would be
    invisible at exactly the moment it matters. Reached directly because the
    handler's own peek closes this window first."""
    a = SessionState()
    a.bind_pending_log(memory_dir)
    staged = a.stage_write({"content": "The user prefers tabs.", "scopes": ["tools"]})

    b = SessionState()
    b.bind_pending_log(memory_dir)
    assert b.take_pending(staged.pending_id) is not None

    with pytest.raises(session_mod.PendingAlreadyConsumed):
        a.take_pending(staged.pending_id)
    assert staged.pending_id not in a.pending_writes
    # An id that was never staged at all stays a plain None — the raise is
    # about a REFUSED claim, not about every miss.
    assert a.take_pending("pending_never") is None


async def test_a_confirm_that_loses_the_claim_mid_flight_writes_nothing(
    memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same last inch, through the real handler.

    `peek_pending`'s sidecar read makes the common case a clean error, but
    it cannot close the window between the peek and the commit — the other
    process can consume the id in exactly that gap. Blinding the peek is how
    that gap is made reachable: what has to hold afterwards is that the
    refused claim stops the write, not merely that it is reported.
    `force=True` again, so no gate refusal can stand in for the claim and
    let this pass for the wrong reason."""
    a_server, _ = _boot(memory_dir)
    body = "The user prefers terse code-driven explanations over prose walls."
    pending_id = await _stage(a_server, body, force=True)

    c_server, c_state = _boot(memory_dir)
    c_state.bind_pending_log(memory_dir)
    assert (await _call(a_server, "memory_write_confirm", pending_id=pending_id))[
        "status"
    ] == "committed"

    monkeypatch.setattr(PendingWriteLog, "is_consumed", lambda *a, **k: False)
    with pytest.raises(Exception, match="already resolved by another session"):
        await _call(c_server, "memory_write_confirm", pending_id=pending_id)
    assert len(Store(memory_dir).load_all()) == 1


async def test_a_cancel_is_a_durable_claim_too(memory_dir: Path) -> None:
    """A cancel resolves the id just as finally as a commit does. If it
    merely cleared the calling state's memory, a stale sibling's next stage
    would put the declined write back on disk and the next restart would
    offer the user a memory they had already said no to."""
    a_server, _ = _boot(memory_dir)
    pending_id = await _stage(a_server, "The user prefers tabs over spaces.")

    b_server, b_state = _boot(memory_dir)
    b_state.bind_pending_log(memory_dir)
    assert pending_id in b_state.pending_writes  # adopted while live

    cancelled = await _call(a_server, "memory_write_cancel", pending_id=pending_id)
    assert cancelled["existed"] is True

    # B's stale copy cannot resurrect it, and cannot commit it.
    assert b_state.cancel_pending(pending_id) is False
    await _stage(b_server, "The user reviews PRs first thing in the morning.")
    _, reloaded = _boot(memory_dir)
    reloaded.bind_pending_log(memory_dir)
    assert pending_id not in reloaded.pending_writes
    assert Store(memory_dir).load_all() == []


async def test_the_consumed_tombstone_is_collected_once_it_is_unreachable(
    memory_dir: Path,
) -> None:
    """The tombstone is what stops the resurrection, so it cannot be dropped
    eagerly — but a marker per confirmed write, kept forever, is a file that
    only grows. One full TTL past the claim is the safe horizon: any
    in-memory copy of that id has itself crossed the pending TTL by then and
    been evicted, so there is nothing left that could offer it back."""
    server, _ = _boot(memory_dir)
    pending_id = await _stage(server, "The user prefers tabs over spaces.")
    await _call(server, "memory_write_confirm", pending_id=pending_id)

    (tombstone,) = _rows_on_disk(memory_dir)
    assert tombstone["pending_id"] == pending_id
    assert tombstone["consumed_at"] is not None

    # Age it past the horizon and let the next process compact on bind.
    tombstone["consumed_at"] = time.time() - session_mod._PENDING_TTL_SECONDS - 1
    (memory_dir / PENDING_WRITES_FILENAME).write_text(json.dumps(tombstone) + "\n")
    fresh = SessionState()
    fresh.bind_pending_log(memory_dir)
    assert _rows_on_disk(memory_dir) == []


async def test_one_client_s_claim_keeps_another_client_s_rows(
    memory_dir: Path,
) -> None:
    """The per-client isolation `save` used to provide by rewriting only one
    client's rows has to survive the switch to deltas — and now also across
    the tombstone: alice's consumed marker must not shadow bob's live row,
    which is what a sidecar keyed by pending id alone would produce."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    server = build_server(config=cfg, store=Store(memory_dir), state=SessionRegistry())
    ids = {}
    for name in ("alice", "bob"):
        res = await _call_as(
            server,
            "memory_write",
            f"client-{name}",
            content=f"The user prefers {name}-flavoured tooling defaults.",
            scopes=["learning-style"],
            category="user-inference",
        )
        ids[name] = res["pending_id"]

    await _call_as(
        server, "memory_write_confirm", "client-alice", pending_id=ids["alice"]
    )
    log = PendingWriteLog(memory_dir)
    assert [r.pending_id for r in log.load("client-alice")] == []
    assert log.is_consumed("client-alice", ids["alice"]) is True
    assert [r.pending_id for r in log.load("client-bob")] == [ids["bob"]]
    # Bob's id is not consumed just because alice's is filed nearby.
    assert log.is_consumed("client-bob", ids["bob"]) is False
    committed = await _call_as(
        server, "memory_write_confirm", "client-bob", pending_id=ids["bob"]
    )
    assert committed["status"] == "committed"
