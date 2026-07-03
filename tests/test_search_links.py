"""memory_search surfaces supersedes / contradicts edges as trust signals.

The MemoryLink schema has carried these edge types since 2.x, but retrieval
never acted on them. `attach_link_annotations` now surfaces them post-rank
(additive — it never reorders or drops a hit): `superseded_by` (active
memories that supersede this hit) and `contradicts` (memories in unresolved
contradiction with it). These tests pin the search-time activation; the
memory_show / reverse_links surface is covered by test_server_links.
"""

from __future__ import annotations

import json
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
    content, structured = await server.call_tool(name, kwargs)
    if structured is not None:
        return structured
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    return None


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
# links_for_many — bulk link resolution. attach_link_annotations is default-on
# on every hit-producing search; the per-hit links_for opened the index file
# once per hit (up to 50). links_for_many folds that into one connection.
# ---------------------------------------------------------------------------


async def test_links_for_many_matches_per_id_links_for(
    server: Any, memory_dir: Path
) -> None:
    """Bulk `links_for_many` returns, for each id, exactly what the per-id
    `links_for` returns — same tuple shapes and ordering."""
    from bettermemory.index import links_for, links_for_many

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
    bulk, needs_rebuild = links_for_many(memory_dir, ids)
    assert needs_rebuild is False
    assert set(bulk) == set(ids)
    for mid in ids:
        assert bulk[mid] == links_for(memory_dir, mid)


async def test_links_for_many_opens_index_once(
    server: Any, memory_dir: Path, monkeypatch: Any
) -> None:
    """ONE index connection for N ids, not one open per id — the regression the
    bulk helper exists to fix."""
    import bettermemory.index as index_mod

    ids = [
        (await _call(server, "memory_write", content=body, scopes=["tools"]))["id"]
        for body in ("one alpha", "two beta", "three gamma")
    ]

    calls = {"n": 0}
    real_connect = index_mod._connect

    def counting_connect(path: Path) -> Any:
        calls["n"] += 1
        return real_connect(path)

    monkeypatch.setattr(index_mod, "_connect", counting_connect)
    index_mod.links_for_many(memory_dir, ids)
    assert calls["n"] == 1


async def test_links_for_many_absent_index_returns_empty_per_id(tmp_path: Path) -> None:
    """No index file -> every requested id maps to ([], []), the best-effort
    no-op contract `links_for` already honors (and never creates the file).
    The flag reads False — nothing was migrated, so the empty map IS the
    correct no-index answer, not a partial one to distrust."""
    from bettermemory.index import index_path, links_for_many

    empty_root = tmp_path / "no-index"
    empty_root.mkdir()
    result = links_for_many(empty_root, ["01ABC", "01DEF"])
    assert result == ({"01ABC": ([], []), "01DEF": ([], [])}, False)
    assert not index_path(empty_root).exists()


async def test_links_for_many_reports_needs_rebuild_flag(
    server: Any, memory_dir: Path
) -> None:
    """The second return element mirrors `links_for_with_status`: the
    `meta.needs_rebuild` flag read on the SAME connection. Healthy index
    -> False; flag stamped (the post-schema-migration state) -> True,
    telling `attach_link_annotations` the map may be missing edges from
    every source untouched since the migration."""
    import sqlite3

    from bettermemory.index import index_path, links_for_many

    a = await _call(server, "memory_write", content="flag probe note", scopes=["tools"])
    _, needs_rebuild = links_for_many(memory_dir, [a["id"]])
    assert needs_rebuild is False

    conn = sqlite3.connect(str(index_path(memory_dir)))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('needs_rebuild', '1')"
        )
        conn.commit()
    finally:
        conn.close()

    _, needs_rebuild = links_for_many(memory_dir, [a["id"]])
    assert needs_rebuild is True


async def test_link_annotations_merge_keeps_index_edges_outside_candidates(
    server: Any, memory_dir: Path
) -> None:
    """Union, not replace: while `needs_rebuild` is set the candidate-scan
    fallback MERGES with the index answer. An inbound edge whose source
    lies outside the caller's `memories` list (the `since_prior_session`
    post-boundary slice is the production case) but is still present in
    the partially refilled index would be lost by a replace — the window
    would then drop annotations the index alone was already serving.
    Driven directly against `attach_link_annotations` so the candidate
    list can be narrowed to exclude the superseder."""
    import sqlite3

    from bettermemory._response import ResponseBuilder
    from bettermemory.index import index_path
    from bettermemory.models import Confidence, MemoryHit

    # Constructed BEFORE the flag lands — no Store construction afterwards,
    # so the construction-time auto-rebuild can't clear it.
    store = Store(memory_dir)
    a = await _call(
        server, "memory_write", content="narrow slice target", scopes=["tools"]
    )
    b = await _call(
        server, "memory_write", content="superseder outside the slice", scopes=["tools"]
    )
    await _call(
        server,
        "memory_update",
        id=b["id"],
        links=[{"type": "supersedes", "target_id": a["id"]}],
    )
    memory_a = store.load_one(a["id"])

    # Stamp the flag while leaving the hook-written link rows intact —
    # the partially-refilled state where B happens to be back in the index.
    conn = sqlite3.connect(str(index_path(memory_dir)))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('needs_rebuild', '1')"
        )
        conn.commit()
    finally:
        conn.close()

    hit = MemoryHit(
        id=a["id"],
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        snippet="narrow slice target",
        score=1.0,
        relevance="high",
        created=memory_a.created,
        updated=memory_a.updated,
    )
    out: list[dict[str, Any]] = [{"id": a["id"]}]
    ResponseBuilder(stale_after_days=30).attach_link_annotations(
        out, [hit], [memory_a], store=store
    )
    assert "superseded_by" in out[0], (
        "the index-held edge (source outside the candidate list) was "
        "dropped by the rebuild-pending fallback — merge, don't replace"
    )
    assert [e["id"] for e in out[0]["superseded_by"]] == [b["id"]]


# ---------------------------------------------------------------------------
# Unreadable-index fallback — a corrupt or version-newer index makes
# links_for_many raise. The annotation must take the same candidate scan as
# the rebuild-pending window, not silently drop the superseded_by /
# contradicts warnings exactly when the index is broken.
# ---------------------------------------------------------------------------


async def test_link_annotations_survive_corrupt_index(
    server: Any, memory_dir: Path
) -> None:
    """Garbage where the SQLite header should be: `links_for_many` raises
    `sqlite3.DatabaseError`, and `status()` reports the same state
    `corrupt=True` so the candidate loader serves `load_all` — the scan
    fallback recovers the inbound `supersedes` edge from those candidates.
    Pre-fix the broad except mapped the failure to an empty links map and
    the `superseded_by` suppression signal died silently."""
    from bettermemory.index import index_path

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

    # Torn-file corruption, past the 100-byte header so SQLite can't read
    # it as fresh-empty. The -wal/-shm siblings go too — a surviving
    # journal must not paper over the torn main file.
    path = index_path(memory_dir)
    path.write_bytes(b"deliberately not a sqlite header " * 8)
    for suffix in ("-wal", "-shm"):
        sibling = path.with_suffix(path.suffix + suffix)
        if sibling.exists():
            sibling.unlink()

    hit_a = _hit(await _search(server, "auth JWT session tokens"), a["id"])
    assert "superseded_by" in hit_a, (
        "a corrupt index must fall back to the candidate scan, not "
        "silently drop the superseded_by warning"
    )
    assert [e["id"] for e in hit_a["superseded_by"]] == [b["id"]]


async def test_link_annotations_survive_version_newer_index(
    server: Any, memory_dir: Path, monkeypatch: Any
) -> None:
    """`IndexVersionError` — the on-disk schema is newer than this reader —
    takes the same candidate-scan fallback as corruption. Driven directly
    against `attach_link_annotations` with a raising `links_for_many` so
    the exception path is pinned without staging real migration state.
    `memories` models the loader output for this window: `status()`
    reports it `corrupt=True`, so `load_all` served the full corpus."""
    from bettermemory import index as index_mod
    from bettermemory._response import ResponseBuilder
    from bettermemory.models import Confidence, MemoryHit

    store = Store(memory_dir)
    a = await _call(
        server, "memory_write", content="version probe target", scopes=["tools"]
    )
    b = await _call(
        server, "memory_write", content="newer schema superseder", scopes=["tools"]
    )
    await _call(
        server,
        "memory_update",
        id=b["id"],
        links=[{"type": "supersedes", "target_id": a["id"]}],
    )
    memory_a = store.load_one(a["id"])
    memory_b = store.load_one(b["id"])

    def raising(root: Path, ids: Any) -> Any:
        raise index_mod.IndexVersionError("index schema version 99 is newer")

    monkeypatch.setattr(index_mod, "links_for_many", raising)

    hit = MemoryHit(
        id=a["id"],
        scopes=["tools"],
        confidence=Confidence.MEDIUM,
        snippet="version probe target",
        score=1.0,
        relevance="high",
        created=memory_a.created,
        updated=memory_a.updated,
    )
    out: list[dict[str, Any]] = [{"id": a["id"]}]
    ResponseBuilder(stale_after_days=30).attach_link_annotations(
        out, [hit], [memory_a, memory_b], store=store
    )
    assert "superseded_by" in out[0], (
        "a version-newer index must fall back to the candidate scan, not "
        "silently drop the superseded_by warning"
    )
    assert [e["id"] for e in out[0]["superseded_by"]] == [b["id"]]


async def test_link_annotations_survive_poisoned_index_meta(
    server: Any, memory_dir: Path
) -> None:
    """A non-integer `meta.schema_version` (a hand-edited or foreign-
    tool-written index — the file stays a valid SQLite database) fails
    `_ensure_schema`'s `int()` version read with ValueError before
    `links_for_many` runs its queries — a third failure mode alongside
    DatabaseError / IndexVersionError. The broad except around
    `links_for_many` already catches it; this pins that the
    poisoned-meta ValueError takes the same candidate-scan fallback
    (`status()` reports the state corrupt=True, so the loader served
    the full corpus) instead of ever crashing the search or silently
    dropping the `superseded_by` warning."""
    import sqlite3

    from bettermemory.index import index_path

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

    conn = sqlite3.connect(str(index_path(memory_dir)))
    try:
        conn.execute("UPDATE meta SET value = 'banana' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    hit_a = _hit(await _search(server, "auth JWT session tokens"), a["id"])
    assert "superseded_by" in hit_a, (
        "poisoned index meta must take the candidate-scan fallback, not "
        "silently drop the superseded_by warning"
    )
    assert [e["id"] for e in hit_a["superseded_by"]] == [b["id"]]


async def test_link_annotation_failure_never_breaks_search(
    server: Any, monkeypatch: Any
) -> None:
    """The broad except stays the OUTERMOST guard: when both the index read
    and the candidate-scan fallback raise, the annotation degrades to
    absent keys — the search itself still returns the hit."""
    import bettermemory._response as response_mod
    from bettermemory import index as index_mod

    a = await _call(
        server, "memory_write", content="guard probe fact alpha", scopes=["tools"]
    )

    def raising(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(index_mod, "links_for_many", raising)
    monkeypatch.setattr(response_mod, "_links_map_with_candidate_scan", raising)

    hit_a = _hit(await _search(server, "guard probe fact alpha"), a["id"])
    assert "superseded_by" not in hit_a
    assert "contradicts" not in hit_a
