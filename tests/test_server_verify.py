"""memory_verify's PRESERVING re-verify re-checks the stored attestations.

The attestation-existence gate originally ran only on a NEWLY passed
`verified_paths` list. `verified_paths=None` preserves the stored lists
(`Store.mark_verified` None-preserves) and used to stamp
`last_verified_at` without re-checking them — asymmetric with stored
CLAIMS, which the same handler re-runs through the declare-time oracle
on every stamp, under the rationale that stamping asserts the whole
record still matches reality. A stored attestation whose target has
since been deleted is exactly such a recorded counterexample, and the
read side cannot recover it: an absolute attestation the prose never
cites is inert forever (`unverifiable_attestations`' docstring), so the
documented no-arg slide-the-timestamp path re-minted `fresh` on top of
it for another whole freshness window.

Scoping mirrors the stored-claims re-check: absolute entries are checked
always (they were attested as on-this-machine observations); relative
entries only when the origin worktree is a live directory here — a
synced replica must not be refused wholesale over a root this machine
never had (`_refuse_unverifiable_stored_attestations`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ._mcp import call_tool as _mcp_call

from bettermemory.config import Config, StorageConfig
from bettermemory.handlers.verify import _refuse_unverifiable_stored_attestations
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


async def test_preserving_reverify_refuses_vanished_stored_attestation(
    memory_dir: Path, tmp_path: Path
) -> None:
    """The exact slide-the-timestamp sequence the gate closes: attest a
    real absolute path, delete the file, then re-verify with
    `verified_paths=None`. Pre-fix the stored list was preserved
    unchecked and the stamp landed — `fresh` resting on nothing for
    another window. The refusal must leave the prior stamp untouched,
    and the documented remedy (a corrected list, with the vanished
    entry moved to `verified_absent_paths`) must go through."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=cfg, store=store, state=SessionState())

    attested = tmp_path / "thing.conf"
    attested.write_text("key = value\n", encoding="utf-8")

    res = await _call(
        server,
        "memory_write",
        content="the service reads its config from a mounted file",
        scopes=["infrastructure"],
    )
    mid = res["id"]
    first = await _call(server, "memory_verify", id=mid, verified_paths=[str(attested)])
    assert first["verified"] == mid
    stamp_after_first = store.load_one(mid).last_verified_at
    assert stamp_after_first is not None

    attested.unlink()

    with pytest.raises(Exception, match="stored path"):
        await _call(server, "memory_verify", id=mid, note="still good")

    # The refusal is total: the prior stamp survives, no new one landed.
    assert store.load_one(mid).last_verified_at == stamp_after_first

    # Remedy from the error message: replace the stored list, moving the
    # intentionally-absent entry to `verified_absent_paths`.
    ok = await _call(
        server,
        "memory_verify",
        id=mid,
        verified_paths=[],
        verified_absent_paths=[str(attested)],
    )
    assert ok["verified"] == mid
    assert ok["verified_paths"] == []
    assert ok["verified_absent_paths"] == [str(attested)]


async def test_preserving_reverify_slides_timestamp_when_attestations_hold(
    memory_dir: Path, tmp_path: Path
) -> None:
    """Positive control: the documented idempotent no-arg re-verify is
    preserved when the stored attestations still stat — the gate refuses
    counterexamples, not the convenience."""
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    store = Store(memory_dir)
    server = build_server(config=cfg, store=store, state=SessionState())

    attested = tmp_path / "still-here.toml"
    attested.write_text("x = 1\n", encoding="utf-8")

    res = await _call(
        server,
        "memory_write",
        content="build flags live in the pinned toml",
        scopes=["tools"],
    )
    mid = res["id"]
    await _call(server, "memory_verify", id=mid, verified_paths=[str(attested)])

    again = await _call(server, "memory_verify", id=mid, note="spot-checked again")
    assert again["verified"] == mid
    # The stored attestation rides along unchanged onto the fresh stamp.
    assert again["verified_paths"] == [str(attested)]


def test_stored_attestation_recheck_scoping(tmp_path: Path) -> None:
    """The scoping split, at the helper: ABSOLUTE stored entries are
    checked regardless of root liveness (they were attested as
    on-this-machine observations), while RELATIVE ones read as
    could-not-ask when the origin worktree is not a live directory here
    — the synced-replica case, where joining onto a dead root would
    refuse every re-verify from that host wholesale."""
    dead_root = tmp_path / "no-such-checkout"
    live_root = tmp_path / "checkout"
    live_root.mkdir()
    (live_root / "docs").mkdir()
    (live_root / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")

    # Relative entry, dead root: skipped — no refusal manufactured.
    _refuse_unverifiable_stored_attestations(
        ["docs/spec.md"], origin_root=str(dead_root)
    )

    # Relative entry, live root, file present: passes.
    _refuse_unverifiable_stored_attestations(
        ["docs/spec.md"], origin_root=str(live_root)
    )

    # Relative entry, live root, file gone: refused.
    with pytest.raises(ValueError, match="stored path"):
        _refuse_unverifiable_stored_attestations(
            ["docs/gone.md"], origin_root=str(live_root)
        )

    # Absolute entry: checked even when the root is dead.
    gone_abs = tmp_path / "vanished.conf"
    with pytest.raises(ValueError, match="stored path"):
        _refuse_unverifiable_stored_attestations(
            [str(gone_abs)], origin_root=str(dead_root)
        )
