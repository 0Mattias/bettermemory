"""End-to-end tests for typed inter-memory links (T2.2 of the v1.7 plan).

Covers persistence (links round-trip through frontmatter), the
memory_update wire surface for setting/replacing/clearing links, the
memory_show forward + reverse link payload, and the validation
guardrails (self-links, malformed types).
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


async def _seed(server: Any, body: str) -> str:
    res = await _call(server, "memory_write", content=body, scopes=["tools"])
    return res["id"]


async def test_links_round_trip_through_memory_show(server: Any) -> None:
    """Set a `supersedes` link via memory_update, then read it back via
    memory_show. The full link payload (type, target_id, note) must
    appear in the response."""
    a_id = await _seed(server, "old version of the fact")
    b_id = await _seed(server, "new version of the fact")

    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[
            {
                "type": "supersedes",
                "target_id": a_id,
                "note": "rewrote after the audit",
            }
        ],
    )

    shown = await _call(server, "memory_show", id=b_id)
    assert "links" in shown
    assert len(shown["links"]) == 1
    link = shown["links"][0]
    assert link["type"] == "supersedes"
    assert link["target_id"] == a_id
    assert link["note"] == "rewrote after the audit"


async def test_reverse_links_surface_on_target(server: Any) -> None:
    """When B supersedes A, memory_show on A must surface that A is
    superseded by B (via `reverse_links`). Without this the relationship
    is one-way at read time and the retrieval consumer can't tell when
    a memory has been replaced elsewhere."""
    a_id = await _seed(server, "old version of the fact")
    b_id = await _seed(server, "new version of the fact")

    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[{"type": "supersedes", "target_id": a_id}],
    )

    shown = await _call(server, "memory_show", id=a_id)
    assert "reverse_links" in shown
    assert len(shown["reverse_links"]) == 1
    rev = shown["reverse_links"][0]
    assert rev["type"] == "supersedes"
    assert rev["source_id"] == b_id


def _force_post_schema_bump_empty_index(memory_dir: Path) -> None:
    """Drive the index into the documented post-`SCHEMA_VERSION`-bump
    state: file PRESENT but tables dropped EMPTY (`indexed_count == 0`).

    We don't edit index.py. Instead we reproduce exactly what an
    upgrading user hits: stamp an older `schema_version` into the
    on-disk `meta` table, then trigger one index op. `_ensure_schema`
    sees on-disk < code `SCHEMA_VERSION` and drops + recreates the
    data tables empty (resetting `indexed_count` to 0) while leaving
    the index FILE in place — the rows then refill lazily per-write
    or via `bettermemory reindex`. `index.status(...)` performs that
    first op, so on return the index is present-but-empty.
    """
    import sqlite3

    from bettermemory import index as _index

    path = _index.index_path(Path(memory_dir).expanduser().resolve())
    assert path.exists(), "index file should exist before the simulated bump"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(_index.SCHEMA_VERSION - 1),),
        )
        conn.commit()
    finally:
        conn.close()
    # One index op triggers _ensure_schema's older-version drop+recreate.
    root = Path(memory_dir).expanduser().resolve()
    status = _index.status(root)
    assert path.exists(), "index FILE must still exist post-rebuild"
    assert status.get("indexed_count", 0) == 0, (
        "index should report empty after the simulated schema-bump rebuild"
    )


async def test_reverse_links_survive_post_schema_bump_empty_index(
    server: Any, memory_dir: Path
) -> None:
    """Regression: a `SCHEMA_VERSION` bump drops+recreates the index
    tables EMPTY on the first index op after an upgrade — the index
    FILE stays present but `indexed_count` resets to 0 until the rows
    refill (lazily per-write, or via `bettermemory reindex`). During
    that window `links_for` returns `[]`, and an `exists()`-only
    fallback in `_links_payload` would emit NO reverse_links even
    though the linking memory is intact on disk.

    The widened fallback also routes the present-but-empty index
    (`index.status(...).indexed_count == 0`) to the `load_all`
    reverse-link scan, so reverse_links stay correct through the
    rebuild window. This test FAILS against the old
    `not index_path(...).exists()` guard (file present → no fallback →
    no reverse_links) and PASSES with the empty-index check.
    """
    a_id = await _seed(server, "old version of the fact")
    b_id = await _seed(server, "new version of the fact")
    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[{"type": "supersedes", "target_id": a_id}],
    )

    # Sanity: the index serves reverse_links before the bump.
    shown_before = await _call(server, "memory_show", id=a_id)
    assert shown_before.get("reverse_links")

    # Reproduce the post-upgrade present-but-empty index state.
    _force_post_schema_bump_empty_index(memory_dir)

    # The reverse link must still surface — via the load_all fallback,
    # since the present-but-empty index can't answer.
    shown = await _call(server, "memory_show", id=a_id)
    assert "reverse_links" in shown, (
        "reverse_links dropped during the post-schema-bump empty-index window"
    )
    assert len(shown["reverse_links"]) == 1
    rev = shown["reverse_links"][0]
    assert rev["type"] == "supersedes"
    assert rev["source_id"] == b_id


async def test_no_inbound_show_opens_index_once(
    server: Any, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perf regression guard: a `memory_show` of a memory with NO
    inbound links, against a HEALTHY POPULATED index, must open the
    index connection EXACTLY ONCE.

    Most memories are not link targets, so the no-inbound branch is the
    common `memory_show` path. The reverse-link fallback once read the
    inbound links via `links_for` (one open) and then — on this same
    common branch — called `index.status(...)` to check `indexed_count`
    (a SECOND full open: `_connect` + PRAGMAs + `_ensure_schema`'s
    `executescript`). Folding the count into the single `links_for`
    open (`links_for_with_status`) removes that second connection.

    We count every `index._connect` call during the show. The store's
    seed writes happen BEFORE the counter is installed, so the only
    opens measured are the read-path ones. This asserts 1; it FAILS at
    2 on the pre-fix code (links_for + status).
    """
    from bettermemory import index as _index

    # A memory that is not a link target: it has zero inbound links,
    # and the index is healthy + populated (indexed_count >= 1). This
    # is the populated-but-no-inbound branch of `_links_payload`.
    a_id = await _seed(server, "standalone memory, never a link target")

    # Sanity: the index really is populated (so a zero count can only
    # mean the regression, never a genuinely empty index). This
    # `status()` open happens BEFORE the counter is installed below.
    root = Path(memory_dir).expanduser().resolve()
    assert _index.status(root).get("indexed_count", 0) >= 1

    real_connect = _index._connect
    opens = {"count": 0}

    def counting_connect(path: Path) -> Any:
        opens["count"] += 1
        return real_connect(path)

    monkeypatch.setattr(_index, "_connect", counting_connect)

    shown = await _call(server, "memory_show", id=a_id)

    # Sanity: healthy index, no inbound links -> no reverse_links, no
    # load_all fallback (which would itself add no index opens, but the
    # absence confirms we're on the populated-no-inbound branch).
    assert "reverse_links" not in shown
    assert opens["count"] == 1, (
        f"no-inbound memory_show opened the index {opens['count']} times; "
        "expected exactly 1 (a second open is the status() regression)"
    )


async def test_links_omitted_when_empty(server: Any) -> None:
    """A memory with no links must not carry the `links` field in the
    response — same absence-as-signal contract as `path_drift` and
    `commit_drift`. Keeps the wire shape compact for the common case."""
    mid = await _seed(server, "lone memory")
    shown = await _call(server, "memory_show", id=mid)
    assert "links" not in shown
    assert "reverse_links" not in shown


async def test_self_link_rejected(server: Any) -> None:
    """A memory linking to its own id is incoherent (a memory can't
    supersede itself) and would foul up the retrieval-side
    suppression logic. Reject at the handler with a clear error."""
    mid = await _seed(server, "self-referencing test")
    with pytest.raises(Exception, match="self-link|own id|incoherent"):
        await _call(
            server,
            "memory_update",
            id=mid,
            links=[{"type": "supersedes", "target_id": mid}],
        )


async def test_links_replace_semantics(server: Any) -> None:
    """memory_update with `links=[...]` REPLACES the link list, not
    appends. Passing an empty list clears all links. Matches the
    `scopes` parameter's contract — simpler than diff-based add/remove."""
    a_id = await _seed(server, "memory a")
    b_id = await _seed(server, "memory b")
    c_id = await _seed(server, "memory c")

    # Set one link.
    await _call(
        server,
        "memory_update",
        id=c_id,
        links=[{"type": "supersedes", "target_id": a_id}],
    )

    # Replace with a different single link — the original is gone.
    await _call(
        server,
        "memory_update",
        id=c_id,
        links=[{"type": "contradicts", "target_id": b_id}],
    )
    shown = await _call(server, "memory_show", id=c_id)
    assert len(shown["links"]) == 1
    assert shown["links"][0]["type"] == "contradicts"
    assert shown["links"][0]["target_id"] == b_id

    # Clear with empty list.
    await _call(server, "memory_update", id=c_id, links=[])
    shown = await _call(server, "memory_show", id=c_id)
    assert "links" not in shown


async def test_all_four_link_types_round_trip(server: Any) -> None:
    """The four link types — supersedes, contradicts, extends,
    depends_on — must all persist and surface unchanged. A new link
    type added to the enum would surface here as a test failure if
    the round-trip lost it silently."""
    target_id = await _seed(server, "the target")
    source_id = await _seed(server, "the source")

    await _call(
        server,
        "memory_update",
        id=source_id,
        links=[
            {"type": "supersedes", "target_id": target_id},
            {"type": "contradicts", "target_id": target_id},
            {"type": "extends", "target_id": target_id},
            {"type": "depends_on", "target_id": target_id},
        ],
    )
    shown = await _call(server, "memory_show", id=source_id)
    types = {link["type"] for link in shown["links"]}
    assert types == {"supersedes", "contradicts", "extends", "depends_on"}


async def test_links_with_invalid_type_rejected(server: Any) -> None:
    """An unknown link type is a caller bug. Reject loudly at the
    handler boundary."""
    a_id = await _seed(server, "a")
    b_id = await _seed(server, "b")
    with pytest.raises(Exception, match=r"links\[0\] invalid"):
        await _call(
            server,
            "memory_update",
            id=b_id,
            links=[{"type": "not-a-real-type", "target_id": a_id}],
        )


async def test_links_with_invalid_target_id_rejected(server: Any) -> None:
    """target_id must be a valid ULID. A non-ULID string is a caller
    bug and means the link can never resolve to a memory."""
    mid = await _seed(server, "anything")
    with pytest.raises(Exception, match="target_id must be a valid ULID"):
        await _call(
            server,
            "memory_update",
            id=mid,
            links=[{"type": "supersedes", "target_id": "not-a-ulid"}],
        )


async def test_multiple_link_types_to_same_target_allowed(server: Any) -> None:
    """A memory can carry several different-typed links to the same
    target — e.g. "extends X" + "depends_on X". The runtime doesn't
    enforce uniqueness on (target_id, type) because the semantics are
    coherent: a memory can both extend and depend on another."""
    target_id = await _seed(server, "the target")
    source_id = await _seed(server, "the source")
    await _call(
        server,
        "memory_update",
        id=source_id,
        links=[
            {"type": "extends", "target_id": target_id},
            {"type": "depends_on", "target_id": target_id},
        ],
    )
    shown = await _call(server, "memory_show", id=source_id)
    assert len(shown["links"]) == 2


async def test_broken_link_to_tombstoned_memory_still_surfaces(server: Any) -> None:
    """A link to a memory that's since been tombstoned should still
    show up in memory_show — broken links are surfaced, not silently
    dropped. The consumer decides whether to follow them (and can
    use memory_list_tombstones to find the target if it was removed)."""
    a_id = await _seed(server, "memory a, will be removed")
    b_id = await _seed(server, "memory b, holds the link")

    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[{"type": "supersedes", "target_id": a_id}],
    )
    await _call(server, "memory_remove", id=a_id, reason="testing broken link")

    shown = await _call(server, "memory_show", id=b_id)
    # The link is still on disk — the source memory's frontmatter
    # doesn't know the target moved.
    assert len(shown["links"]) == 1
    assert shown["links"][0]["target_id"] == a_id


async def test_oversized_link_notes_rejected_not_silently_lost(
    server: Any, memory_dir: Path
) -> None:
    """Regression: link notes are unbounded per-entry, so 64 large notes can
    push the serialized frontmatter past the 64 KB YAML ceiling. Before the
    write-side frontmatter guard, memory_update reported success while
    producing a file that fails to parse on the next read — silently dropping
    the record from search/list/show/health. The update must now be REJECTED
    and the record left intact, not written-then-vanished.
    """
    a_id = await _seed(server, "link target")
    b_id = await _seed(server, "the memory we must not lose")

    # 64 links (the model count cap) each with a ~1.1 KB note -> ~70 KB
    # frontmatter, over the 64 KB ceiling.
    links = [
        {"type": "extends", "target_id": a_id, "note": f"{'x' * 1100}-{i}"}
        for i in range(64)
    ]
    with pytest.raises(Exception, match="exceeds|cap"):
        await _call(server, "memory_update", id=b_id, links=links)

    # The record must still be intact and retrievable — not silently dropped.
    shown = await _call(server, "memory_show", id=b_id)
    assert shown["id"] == b_id
    assert len(shown.get("links", [])) < 64  # the oversized update did not land

    # And the store can still load every memory (nothing vanished on disk).
    store = Store(memory_dir)
    assert len(store.load_all()) == 2


async def test_links_over_count_cap_rejected_not_silently_lost(
    server: Any, memory_dir: Path
) -> None:
    """Regression: memory_update never checked len(links), but the merge uses
    model_copy(update=...) which SKIPS Memory._check_links's 64-entry cap. A
    65-link update therefore reported status="committed" while writing a file
    that re-validation through the Memory(...) ctor rejects — load_all/load_one
    catch-and-skip it, so the record SILENTLY VANISHES from every read surface.
    The update must be REJECTED (mirroring the scopes-cap guard on update) and
    the record left intact, not written-then-vanished.
    """
    a_id = await _seed(server, "link target")
    b_id = await _seed(server, "the memory we must not lose")

    # 65 links: one over the model's 64-entry cap. Notes are tiny here, so the
    # serialized frontmatter stays well under the 64 KB YAML ceiling — this
    # isolates the COUNT cap from the byte-ceiling guard exercised above.
    links = [{"type": "extends", "target_id": a_id} for _ in range(65)]
    with pytest.raises(Exception, match="links list capped at 64 entries"):
        await _call(server, "memory_update", id=b_id, links=links)

    # The record must still be intact and retrievable — not silently dropped.
    shown = await _call(server, "memory_show", id=b_id)
    assert shown["id"] == b_id
    assert len(shown.get("links", [])) == 0  # the over-cap update did not land

    # And the store can still load every memory (nothing vanished on disk).
    store = Store(memory_dir)
    assert len(store.load_all()) == 2


async def test_links_at_count_cap_accepted(server: Any, memory_dir: Path) -> None:
    """Exactly 64 links is accepted — the cap is inclusive, matching
    Memory._check_links (`len(v) > 64`). Guards against an off-by-one that
    would reject the boundary the model itself permits."""
    a_id = await _seed(server, "link target")
    b_id = await _seed(server, "the source")

    links = [{"type": "extends", "target_id": a_id} for _ in range(64)]
    res = await _call(server, "memory_update", id=b_id, links=links)
    assert res["status"] == "committed"

    shown = await _call(server, "memory_show", id=b_id)
    assert len(shown["links"]) == 64
