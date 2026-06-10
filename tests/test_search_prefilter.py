"""Regression tests for the FTS prefilter cap-starvation guard (round 85).

`_handlers._load_search_candidates` threads only `scopes` into the FTS5
top-50 prefilter; the repo/worktree auto-scope filter and session-disabled
scopes apply post-cap in `search._filter_candidates`. Without the guard in
`handlers/search.py`, a store with >50 memories where the caller's
in-filter matches all rank #51+ globally returns ZERO hits on the
prefilter path even though matching memories exist — the cap-starvation
failure mode the `_load_search_candidates` docstring itself names. The
guard dry-runs the authoritative filter on a cap-saturated slice and
reloads the full corpus when fewer than `max_results` candidates survive.

Lives in its own file (not test_search*.py — those belong to another
batch this round) and exercises the guard through the MCP tool surface,
mirroring tests/test_server_search_index.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory import index
from bettermemory.config import Config, StorageConfig
from bettermemory.origin import Origin
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

REPO_A = "git@github.com:example/repo-a.git"
REPO_B = "git@github.com:example/repo-b.git"


def _build_server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


def _pin_caller_origin(monkeypatch: pytest.MonkeyPatch, origin: Origin) -> None:
    """Pin what `capture_origin` reports for the searching session.

    The handler resolves it through `_handlers` at call time
    (`from .. import _handlers as _h; _h.capture_origin()`), so patching
    the module binding is enough — same pattern as
    tests/test_server_origin.py, but monkeypatch-scoped so it can't leak
    across tests."""
    import bettermemory._handlers as handlers_module

    def fake_capture(cwd: Path | None = None) -> Origin:
        return origin

    monkeypatch.setattr(handlers_module, "capture_origin", fake_capture)


def _write_dense(store: Store, n: int, *, scopes: list[str], origin: Origin | None):
    """`n` short, term-dense "alpha" memories — high BM25, they
    monopolize the FTS top-50 candidate slice."""
    dense = "alpha " * 6
    for i in range(n):
        store.write(content=f"{dense}filler-{i}", scopes=scopes, origin=origin)


def _write_weak(store: Store, n: int, *, scopes: list[str], origin: Origin | None):
    """`n` long memories mentioning "alpha" exactly once — low BM25,
    ranked past the dense block. Returns their ids."""
    padding = " ".join(f"pad{j}" for j in range(40))
    ids = []
    for i in range(n):
        m = store.write(
            content=f"alpha appears once here {padding} doc-{i}",
            scopes=scopes,
            origin=origin,
        )
        ids.append(m.id)
    return ids


def _assert_starved_precondition(memory_dir: Path, weak_ids: list[str]) -> None:
    """Self-validating ranking precondition: the weak matches must rank
    past the 50-row FTS cap, or the test isn't exercising starvation."""
    top50 = {cid for cid, _ in index.query(memory_dir, "alpha", max_results=50)}
    assert len(top50) == 50
    assert not (set(weak_ids) & top50), (
        "precondition drift: weak matches landed inside the FTS top-50, "
        "so the prefilter slice is not starved — densify the decoys"
    )


async def test_repo_filter_starvation_reloads_full_corpus(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round-85 repro: 60 term-dense repo-B matches monopolize the
    FTS top-50; the caller's 3 repo-A matches rank #51+. The prefilter
    slice is 50 repo-B candidates, all of which the post-cap auto-scope
    filter strips — pre-guard, the search returned zero hits while
    matching in-repo memories existed. The guard detects the starved
    cap-saturated slice and reloads via load_all."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    _write_dense(store, 60, scopes=["tools"], origin=Origin(repo=REPO_B))
    a_ids = _write_weak(store, 3, scopes=["tools"], origin=Origin(repo=REPO_A))
    index.rebuild(memory_dir, store.iter_active())
    _assert_starved_precondition(memory_dir, a_ids)

    server = _build_server(memory_dir)
    _pin_caller_origin(monkeypatch, Origin(cwd="/projects/a", repo=REPO_A))

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert {h["id"] for h in hits} == set(a_ids), (
        "auto-scoped search starved by the FTS top-50 cap: the in-repo "
        "matches ranked past the cap must still surface via the reload"
    )


async def test_disabled_scope_starvation_reloads_full_corpus(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session-disabled-scope variant: the post-cap filter here is
    `state.disabled_scopes` (no repo/worktree filter in play — the
    caller origin carries no repo). 60 dense matches in the disabled
    scope saturate the prefilter slice; the 3 in-scope matches rank
    past the cap and must still surface."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    _write_dense(store, 60, scopes=["noise"], origin=None)
    tool_ids = _write_weak(store, 3, scopes=["tools"], origin=None)
    index.rebuild(memory_dir, store.iter_active())
    _assert_starved_precondition(memory_dir, tool_ids)

    server = _build_server(memory_dir)
    _pin_caller_origin(monkeypatch, Origin(cwd="/tmp/elsewhere"))
    await _call(server, "memory_scope_disable", scope="noise")

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert {h["id"] for h in hits} == set(tool_ids)


async def test_prefilter_path_still_serves_when_filters_leave_survivors(
    memory_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control pinning the guard's cost profile: when the post-cap
    filters leave at least `max_results` survivors in the cap-saturated
    slice, the search keeps serving from the prefilter candidates — no
    load_all reload. Spied via a counting wrapper on Store.load_all."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    _write_dense(store, 60, scopes=["tools"], origin=Origin(repo=REPO_A))
    index.rebuild(memory_dir, store.iter_active())

    server = _build_server(memory_dir)
    _pin_caller_origin(monkeypatch, Origin(cwd="/projects/a", repo=REPO_A))

    calls = {"n": 0}
    real_load_all = Store.load_all

    def counting_load_all(self: Store, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real_load_all(self, *args, **kwargs)

    monkeypatch.setattr(Store, "load_all", counting_load_all)

    hits = _unwrap(await _call(server, "memory_search", query="alpha"))
    assert len(hits) == 5  # default max_results, served from the slice
    assert calls["n"] == 0, (
        "the guard must not pay the load_all reload when the prefilter "
        "slice already has enough in-filter survivors"
    )
