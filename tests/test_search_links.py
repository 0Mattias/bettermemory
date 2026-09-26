"""memory_search surfaces supersedes / contradicts edges as trust signals.

The MemoryLink schema has carried these edge types since 2.x, but retrieval
never acted on them. `attach_link_annotations` now surfaces them post-rank
(additive — it never reorders or drops a hit): `superseded_by` (active
memories that supersede this hit) and `contradicts` (memories in unresolved
contradiction with it). These tests pin the search-time activation; the
memory_show / reverse_links surface is covered by test_server_links.
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def server(memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=Store(memory_dir), state=SessionState())


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


async def _search(server: Any, query: str) -> list[dict[str, Any]]:
    res = await _call(server, "memory_search", query=query, auto_scope=False)
    return res.get("result", res) if isinstance(res, dict) else res


def _hit(hits: list[dict[str, Any]], mid: str) -> dict[str, Any]:
    return next(h for h in hits if h["id"] == mid)


async def test_superseded_by_surfaces_on_search_hit(server: Any) -> None:
    """When B supersedes A, a search hit for A carries `superseded_by: [B]`,
    even though B's body doesn't match the query (targeted-load path)."""
    a = await _call(
        server,
        "memory_write",
        content="the auth subsystem validates JWT session tokens",
        scopes=["tools"],
    )
    b = await _call(
        server,
        "memory_write",
        content="unrelated replacement note xyzzy",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_update",
        id=b["id"],
        links=[{"type": "supersedes", "target_id": a["id"]}],
    )

    hits = await _search(server, "auth JWT session tokens")
    hit_a = _hit(hits, a["id"])
    assert "superseded_by" in hit_a
    assert [e["id"] for e in hit_a["superseded_by"]] == [b["id"]]
    assert hit_a["superseded_by"][0]["summary"]


async def test_contradicts_surfaces_both_directions(server: Any) -> None:
    """A `contradicts` edge surfaces on BOTH endpoints' hits (symmetric)."""
    # Lexically distinct bodies (so the write-dedup gate doesn't reject the
    # second) that both surface on a shared query.
    a = await _call(
        server,
        "memory_write",
        content="deploy windows are open every Friday afternoon",
        scopes=["tools"],
    )
    b = await _call(
        server,
        "memory_write",
        content="a hard production freeze blocks all Friday shipping",
        scopes=["tools"],
    )
    await _call(
        server,
        "memory_update",
        id=a["id"],
        links=[{"type": "contradicts", "target_id": b["id"]}],
    )

    hits = await _search(server, "Friday deploy freeze shipping")
    # Outbound: A points at B.
    assert [e["id"] for e in _hit(hits, a["id"])["contradicts"]] == [b["id"]]
    # Inbound: B is pointed at by A — same edge, surfaced on B too.
    assert [e["id"] for e in _hit(hits, b["id"])["contradicts"]] == [a["id"]]


async def test_no_links_omits_annotation_keys(server: Any) -> None:
    """A plain memory carries neither key — absence-as-signal."""
    await _call(
        server, "memory_write", content="a plain unlinked fact", scopes=["tools"]
    )
    hit = (await _search(server, "plain unlinked fact"))[0]
    assert "superseded_by" not in hit
    assert "contradicts" not in hit


async def test_superseded_by_skips_tombstoned_superseder(server: Any) -> None:
    """If the superseding memory is tombstoned, the annotation is dropped
    (the edge isn't actionable) rather than surfacing a dead reference."""
    a = await _call(
        server,
        "memory_write",
        content="config lives in settings.toml",
        scopes=["tools"],
    )
    b = await _call(server, "memory_write", content="superseder body", scopes=["tools"])
    await _call(
        server,
        "memory_update",
        id=b["id"],
        links=[{"type": "supersedes", "target_id": a["id"]}],
    )
    await _call(server, "memory_remove", id=b["id"], reason="no longer relevant")

    hit_a = _hit(await _search(server, "config settings toml"), a["id"])
    assert "superseded_by" not in hit_a


# ---------------------------------------------------------------------------
# links_for_many: bulk link resolution. attach_link_annotations is default-on
# on every hit-producing search; the bulk read serves every hit (up to 50)
# in two IN (...) queries instead of one read per hit.
# ---------------------------------------------------------------------------


async def test_links_for_many_matches_per_id_links_with_status(
    server: Any, memory_dir: Path
) -> None:
    """Bulk `links_for_many` returns, for each id, exactly what the per-id
    `links_with_status` returns: same tuple shapes and ordering."""
    a = await _call(
        server, "memory_write", content="alpha base note one", scopes=["tools"]
    )
    b = await _call(
        server, "memory_write", content="beta superseding note two", scopes=["tools"]
    )
    c = await _call(
        server, "memory_write", content="gamma clashing note three", scopes=["tools"]
    )
    await _call(
        server,
        "memory_update",
        id=b["id"],
        links=[{"type": "supersedes", "target_id": a["id"]}],
    )
    await _call(
        server,
        "memory_update",
        id=c["id"],
        links=[{"type": "contradicts", "target_id": a["id"], "note": "clash"}],
    )

    ids = [a["id"], b["id"], c["id"]]
    store = Store.open(memory_dir)
    bulk = store.links_for_many(ids)
    assert set(bulk) == set(ids)
    for mid in ids:
        outbound, inbound, _trust = store.links_with_status(mid)
        assert bulk[mid] == (outbound, inbound)
    assert bulk[a["id"]][1] == [
        ("contradicts", c["id"], "clash"),
        ("supersedes", b["id"], None),
    ]


async def test_links_for_many_unknown_ids_return_empty_per_id(tmp_path: Path) -> None:
    """Ids the store holds no link rows for come back as empty pairs, one
    entry per id asked, so the annotation never KeyErrors on a hit."""
    store = Store(tmp_path / "empty")
    result = store.links_for_many(["01ABC", "01DEF"])
    assert result == {"01ABC": ([], []), "01DEF": ([], [])}


async def test_link_annotation_failure_never_breaks_search(
    server: Any, monkeypatch: Any
) -> None:
    """The broad except stays the OUTERMOST guard: when the store's link
    read raises, the annotation degrades to absent keys and the search
    itself still returns the hit."""
    a = await _call(
        server, "memory_write", content="guard probe fact alpha", scopes=["tools"]
    )

    def raising(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(Store, "links_for_many", raising)

    hit_a = _hit(await _search(server, "guard probe fact alpha"), a["id"])
    assert "superseded_by" not in hit_a
    assert "contradicts" not in hit_a
