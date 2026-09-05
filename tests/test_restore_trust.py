"""A restore re-checks the trust the tombstone carried.

`memory_restore` used to re-admit `last_verified_at`, the attestations and
the claims exactly as the tombstone held them, with no oracle re-check —
the remaining half of the 2026-09-01 integrity recon's third weak point.
The tree moves while a record sits tombstoned, so the two checks the verify
handler runs before it re-stamps a stored record run on the way back,
with the same scoping, and whatever fails leaves with the removal fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.handlers.restore import restore_with_trust_check, trust_strip_for
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

_BODY = "The drift gate lives in `pkg/mod.py` beside the notes."


def _tree(tmp_path: Path, timeout: str = "30") -> Path:
    root = tmp_path / "worktree"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "mod.py").write_text(f"TIMEOUT = {timeout}\n", encoding="utf-8")
    return root


def _seed(
    store: Store,
    root: Path,
    *,
    claims: list[str] = (),  # type: ignore[assignment]
    verified_paths: list[str] = (),  # type: ignore[assignment]
) -> str:
    """A verified, then tombstoned, record whose trust fields the store
    accepted verbatim — `Store.write` and `Store.mark_verified` are the
    policy-free primitives, so the fixture can hold whatever a tombstone
    might carry."""
    memory = store.write(
        content=_BODY,
        scopes=["tools"],
        origin=Origin(cwd=str(root), worktree_root=str(root)),
        claims=list(claims),
    )
    store.mark_verified(memory.id, verified_paths=list(verified_paths))
    assert store.load_one(memory.id).last_verified_at is not None
    store.tombstone(memory.id, "test")
    return memory.id


def _build(memory_dir: Path) -> Any:
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(full_tool_surface=True),
    )
    state = SessionState()
    recorder = Recorder(root=memory_dir, session_id=state.session_id, enabled=True)
    return build_server(
        config=cfg, store=Store(memory_dir), state=state, recorder=recorder
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    res = await _mcp_call(server, name, kwargs)
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _restore_events(memory_dir: Path) -> list[dict[str, Any]]:
    return [e for e in iter_events(memory_dir) if e.get("kind") == "restore"]


async def test_a_claim_the_tree_now_contradicts_leaves_with_the_stamp(
    memory_dir: Path, tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    store = Store(memory_dir)
    mid = _seed(store, root, claims=["pkg/mod.py::TIMEOUT=30"])
    (root / "pkg" / "mod.py").write_text("TIMEOUT = 60\n", encoding="utf-8")

    server = _build(memory_dir)
    res = await _call(server, "memory_restore", id=mid)
    assert res["status"] == "committed" and res["id"] == mid
    assert res["trust_stripped"] == {
        "claims": ["pkg/mod.py::TIMEOUT=30"],
        "verified_paths": [],
        "verification_cleared": True,
    }
    assert "memory_verify" in res["hint"]
    restored = store.load_one(mid)
    assert restored.claims == []
    assert restored.last_verified_at is None
    assert restored.body.strip() == _BODY
    (event,) = _restore_events(memory_dir)
    assert event["claims_dropped"] == ["pkg/mod.py::TIMEOUT=30"]
    assert event["verification_cleared"] is True
    assert "attestations_dropped" not in event


async def test_an_attested_path_that_vanished_leaves_with_the_stamp(
    memory_dir: Path, tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    store = Store(memory_dir)
    attested = tmp_path / "conf.toml"
    attested.write_text("x = 1\n", encoding="utf-8")
    mid = _seed(
        store, root, claims=["pkg/mod.py::TIMEOUT=30"], verified_paths=[str(attested)]
    )
    attested.unlink()

    server = _build(memory_dir)
    res = await _call(server, "memory_restore", id=mid)
    assert res["trust_stripped"] == {
        "claims": [],
        "verified_paths": [str(attested)],
        "verification_cleared": True,
    }
    restored = store.load_one(mid)
    assert restored.verified_paths == []
    # The claim still holds and stays; only the stamp over the record goes.
    assert restored.claims == ["pkg/mod.py::TIMEOUT=30"]
    assert restored.last_verified_at is None
    (event,) = _restore_events(memory_dir)
    assert event["attestations_dropped"] == [str(attested)]
    assert "claims_dropped" not in event


async def test_trust_that_still_holds_comes_back_intact(
    memory_dir: Path, tmp_path: Path
) -> None:
    root = _tree(tmp_path)
    store = Store(memory_dir)
    attested = tmp_path / "still.toml"
    attested.write_text("x = 1\n", encoding="utf-8")
    mid = _seed(
        store, root, claims=["pkg/mod.py::TIMEOUT=30"], verified_paths=[str(attested)]
    )
    stamp = store.load_tombstone(mid).last_verified_at

    server = _build(memory_dir)
    res = await _call(server, "memory_restore", id=mid)
    assert "trust_stripped" not in res and "hint" not in res
    restored = store.load_one(mid)
    assert restored.last_verified_at == stamp
    assert restored.claims == ["pkg/mod.py::TIMEOUT=30"]
    assert restored.verified_paths == [str(attested)]
    (event,) = _restore_events(memory_dir)
    assert not {"claims_dropped", "attestations_dropped", "verification_cleared"} & set(
        event
    )


def test_a_dead_origin_worktree_is_not_a_counterexample(
    memory_dir: Path, tmp_path: Path
) -> None:
    """A synced replica carries a root this machine never had: stored
    claims and relative attestations cannot be judged there and stay;
    an absolute attestation was an on-this-machine observation and is
    judged regardless."""
    root = tmp_path / "never-here"
    store = Store(memory_dir)
    gone = tmp_path / "gone.toml"
    mid = _seed(
        store,
        root,
        claims=["pkg/mod.py::TIMEOUT=30"],
        verified_paths=["pkg/mod.py", str(gone)],
    )
    strip = trust_strip_for(store.load_tombstone(mid))
    assert strip.claims == []
    assert strip.verified_paths == [str(gone)]

    memory, applied = restore_with_trust_check(store, mid)
    assert applied == strip
    assert memory.claims == ["pkg/mod.py::TIMEOUT=30"]
    assert memory.verified_paths == ["pkg/mod.py"]
    assert memory.last_verified_at is None


def test_the_cli_restore_runs_the_same_check(
    memory_dir: Path, tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    from bettermemory.cli import tombstones as cli_tombstones

    root = _tree(tmp_path)
    store = Store(memory_dir)
    mid = _seed(store, root, claims=["pkg/mod.py::TIMEOUT=30"])
    (root / "pkg" / "mod.py").write_text("TIMEOUT = 60\n", encoding="utf-8")

    monkeypatch.setenv("BETTERMEMORY_DIR", str(memory_dir))
    cli_tombstones._cli_tombstones_restore(memory_id=mid, json_out=False, parser=None)
    out = capsys.readouterr().out
    assert f"Restored {mid}" in out
    assert "dropped 1 claim(s) and 0 attested path(s)" in out
    assert store.load_one(mid).claims == []
    (event,) = _restore_events(memory_dir)
    assert event["claims_dropped"] == ["pkg/mod.py::TIMEOUT=30"]
