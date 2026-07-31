"""End-to-end MCP tests for the `mode` parameter on memory_search.

Companion to the unit tests in test_search_modes.py — these run the full
handler path so we exercise the validation, semantic_model factory
resolution, and result-shape contract over the wire.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import BehaviorConfig, Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def _seed(server: Any, body: str, scopes: list[str] | None = None) -> str:
    res = await _call(
        server,
        "memory_write",
        content=body,
        scopes=scopes or ["tools"],
    )
    return res["id"]


async def test_default_mode_is_hybrid(server: Any) -> None:
    """No mode parameter and no config setting => hybrid mode (default
    since 2.6.8). Pin the default so a future flip is an obvious diff."""
    await _seed(server, "python list comprehension and generators")
    await _seed(server, "rust borrow checker and lifetimes")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits
    # Hybrid fuses via RRF: scores land in the small ~0.01-0.05 range.
    # Keyword would produce scores >= 0.5. This bound separates the two.
    assert 0 < hits[0]["score"] < 0.1


async def test_mode_bm25_returns_hits(server: Any) -> None:
    """BM25 mode through the MCP wire produces hits with the same shape
    as keyword mode. The score scale differs but the rest of the hit
    structure is identical."""
    await _seed(server, "python list comprehension")
    await _seed(server, "python decorators and closures")

    hits = _unwrap(await _call(server, "memory_search", query="python", mode="bm25"))
    assert len(hits) == 2
    assert all(h["score"] > 0 for h in hits)
    # The shape of each hit should match keyword mode's contract.
    assert all({"id", "score", "relevance", "scopes"} <= set(h) for h in hits)


async def test_mode_hybrid_without_embeddings_falls_back_gracefully(
    server: Any,
) -> None:
    """Hybrid mode without the embeddings extra installed: fuses
    keyword + BM25 only. The "I asked for the best, but I don't have
    sentence-transformers" path. Should produce hits, not error."""
    await _seed(server, "python list comprehension")
    await _seed(server, "rust borrow checker")

    hits = _unwrap(await _call(server, "memory_search", query="python", mode="hybrid"))
    assert hits
    # The fused score lives in the small RRF range.
    assert all(0 < h["score"] < 0.1 for h in hits)


async def test_mode_semantic_without_embeddings_raises(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mode="semantic"` is an explicit ask — failing softly would hide
    the deps issue from the caller. The error should mention the extra
    so the user can act on the message.

    The no-extra condition is now simulated rather than inherited from
    the environment. It used to hold for free because the default config
    resolved no model whatever was installed; now an importable extra
    routes one into `hybrid`, so on a machine (or CI leg) with the extra
    present this test was asserting against its own environment instead
    of its contract, and would pass or fail by accident."""
    monkeypatch.setattr(
        "bettermemory.semantic_setup._embeddings_extra_importable", lambda: False
    )
    monkeypatch.setattr("bettermemory.builder._semantic_model_or_none", lambda _c: None)
    await _seed(server, "anything")

    with pytest.raises(Exception, match="embeddings extra"):
        await _call(server, "memory_search", query="x", mode="semantic")


async def test_mode_invalid_value_raises(server: Any) -> None:
    """Unknown mode string is a caller bug — raise with the list of
    valid values rather than silently returning empty."""
    await _seed(server, "anything")

    with pytest.raises(Exception, match="unknown search mode"):
        await _call(server, "memory_search", query="x", mode="not-a-mode")


async def test_config_search_mode_sets_default(memory_dir: Path) -> None:
    """Setting `behavior.search_mode = "bm25"` in config makes BM25 the
    default for calls that don't pass `mode`. The MCP parameter still
    overrides per-call."""
    cfg = Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(search_mode="bm25"),
    )
    srv = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=SessionState(),
    )
    await _seed(srv, "python list comprehension")

    # No mode parameter => use config default (bm25).
    hits = _unwrap(await _call(srv, "memory_search", query="python"))
    assert hits

    # Per-call override beats config.
    hits_explicit = _unwrap(
        await _call(srv, "memory_search", query="python", mode="keyword")
    )
    # Scores will differ between modes, but the same memory should appear.
    assert hits[0]["id"] == hits_explicit[0]["id"]


async def test_mode_keyword_preserves_existing_hit_shape(server: Any) -> None:
    """Regression: the mode dispatch refactor shouldn't change the
    field set on each hit. Pin the contract so a future per-mode
    customisation doesn't drop a field the consumers expect."""
    await _seed(server, "python notes", scopes=["projects:foo"])

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits
    h = hits[0]
    # The pre-T1.2 shape that consumers rely on:
    for field in (
        "id",
        "scopes",
        "confidence",
        "snippet",
        "score",
        "relevance",
        "match_terms",
        "created",
        "updated",
        "staleness_verdict",
        "path_drift_checked",
        "path_drift_missing",
    ):
        assert field in h, f"missing field {field!r} in hit"
