"""W8 covers the never-verified memory too.

`memory_verify`'s optimistic-concurrency check fingerprinted only
`last_verified_at` — the field a concurrent ATTESTATION moves. A
concurrent EDIT moves the other axis: `Store.update` clears
`last_verified_at`, so on a never-verified memory the compare was
None == None, a vacuous pass, and the stamp landed on a body revision
the verifier never read. The fingerprint is now the snapshot pair
(`last_verified_at`, `updated`); the edit bumps `updated`, trips the
CAS, and the handler returns the structured `status="stale"` payload
instead of certifying prose nobody checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._mcp import call_tool as _mcp_call

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


async def test_verify_cas_trips_on_concurrent_edit_of_never_verified_memory(
    memory_dir: Path,
) -> None:
    """An edit landing between the handler's snapshot and the store's
    under-lock compare must surface as `status="stale"`, exactly like a
    concurrent attestation.

    The interleave is forced deterministically: the store's `load_one`
    is wrapped so the moment the handler takes its snapshot, an editor
    revises the body (bumping `updated`, clearing the already-None
    `last_verified_at`). Mutation-soundness: dropping `expected_updated`
    from the handler's `mark_verified` call (the pre-fix shape) makes
    this verify return `status="committed"` — a stamp on prose the
    caller never read — and the assertions below fail.
    """
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=cfg, store=store, state=SessionState())

    res = await _call(
        server,
        "memory_write",
        content="deploy target is the blue cluster",
        scopes=["infrastructure"],
    )
    mid = res["id"]

    real_load_one = store.load_one
    raced: dict[str, Any] = {}

    def racing_load_one(memory_id: str) -> Any:
        snap = real_load_one(memory_id)
        if memory_id == mid and "done" not in raced:
            raced["done"] = True
            edited = snap.model_copy(update={"body": "deploy target is the GREEN cluster"})
            store.update(edited)
        return snap

    store.load_one = racing_load_one  # type: ignore[method-assign]
    try:
        out = await _call(server, "memory_verify", id=mid)
    finally:
        store.load_one = real_load_one  # type: ignore[method-assign]

    assert out["status"] == "stale", out
    assert out["memory_id"] == mid

    # The winner's edit is intact and NO verification stamp landed on it.
    current = store.load_one(mid)
    assert "GREEN" in current.body
    assert current.last_verified_at is None, (
        "a verify that lost the race must not certify the edited body"
    )
