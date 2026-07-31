"""End-to-end tests for typed inter-memory links (T2.2 of the v1.7 plan).

Covers persistence (links round-trip through frontmatter), the
memory_update wire surface for setting/replacing/clearing links, the
memory_show forward + reverse link payload, and the validation
guardrails (self-links, malformed types).
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


async def test_reverse_links_survive_rebuild_pending_partial_index(
    server: Any, memory_dir: Path
) -> None:
    """Regression: the rebuild-PENDING window with a PARTIALLY refilled
    index. A `SCHEMA_VERSION` migration drops the tables empty and sets
    `meta.needs_rebuild`; the incremental Store hooks then repopulate
    only whatever gets touched, so one post-upgrade write pushes
    `indexed_count` above zero while the linking memory's
    `memory_links` row is still missing. Pre-fix `_links_payload`'s
    only unusable-index signal was `indexed_count == 0`, so this state
    returned EMPTY reverse_links for the untouched legacy target with
    no fallback — the same hole class `_handlers.load_search_candidates`
    closes with its `needs_rebuild` gate, on the links surface.

    Deliberately avoids constructing a Store after the migration so
    the flag-handling in `_links_payload` is exercised on its own, not
    rescued by the construction-time auto-rebuild.
    """
    import sqlite3

    from bettermemory import index as _index

    a_id = await _seed(server, "old version of the fact")
    b_id = await _seed(server, "new version of the fact")
    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[{"type": "supersedes", "target_id": a_id}],
    )

    # Sanity: the healthy index serves the reverse link before the bump.
    shown_before = await _call(server, "memory_show", id=a_id)
    assert shown_before.get("reverse_links")

    # Back-date the on-disk index version; the next index op migrates
    # (drop empty + flag rebuild-pending).
    root = Path(memory_dir).expanduser().resolve()
    conn = sqlite3.connect(str(_index.index_path(root)))
    try:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(_index.SCHEMA_VERSION - 1),),
        )
        conn.commit()
    finally:
        conn.close()

    # A post-upgrade write triggers the migration AND repopulates one
    # row via the incremental hook: `indexed_count` is back above zero,
    # the flag is still set, and B's link row is still missing.
    await _seed(server, "unrelated post-upgrade write")
    status = _index.status(root)
    assert status["needs_rebuild"] is True
    assert status["indexed_count"] >= 1

    # The reverse link must still surface — via the load_all fallback,
    # since the flag marks the partial index unusable.
    shown = await _call(server, "memory_show", id=a_id)
    assert "reverse_links" in shown, (
        "reverse_links dropped during the rebuild-pending window "
        "(needs_rebuild set, indexed_count > 0)"
    )
    assert len(shown["reverse_links"]) == 1
    rev = shown["reverse_links"][0]
    assert rev["type"] == "supersedes"
    assert rev["source_id"] == b_id


@pytest.mark.parametrize("poisoned_key", ["schema_version", "indexed_count"])
async def test_show_survives_poisoned_index_meta(
    server: Any, memory_dir: Path, poisoned_key: str
) -> None:
    """A non-integer meta row (a hand-edited or foreign-tool-written
    index — the file stays a valid SQLite database) fails an `int()`
    read inside `links_for_with_status` with ValueError:
    `meta.schema_version` in `_ensure_schema`'s version check,
    `meta.indexed_count` in the helper's own count read. Neither is
    `sqlite3.DatabaseError` nor `IndexVersionError`, so pre-fix both
    escaped `_links_payload`'s corruption guard and hard-crashed
    memory_show for EVERY id until reindex — defeating the
    degrade-gracefully contract the guard exists to keep. Unparseable
    meta IS corruption (`index.status()` already classifies it so):
    the show must take the same reverse-scan fallback as a torn file,
    recovering the reverse link from the intact .md files."""
    import sqlite3

    from bettermemory import index as _index

    a_id = await _seed(server, "old version of the fact")
    b_id = await _seed(server, "new version of the fact")
    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[{"type": "supersedes", "target_id": a_id}],
    )

    # Sanity: the healthy index serves the reverse link before the
    # poisoning, so the post-poison assertion proves the fallback.
    shown_before = await _call(server, "memory_show", id=a_id)
    assert shown_before.get("reverse_links")

    root = Path(memory_dir).expanduser().resolve()
    conn = sqlite3.connect(str(_index.index_path(root)))
    try:
        conn.execute("UPDATE meta SET value = 'banana' WHERE key = ?", (poisoned_key,))
        conn.commit()
    finally:
        conn.close()

    # memory_show must not raise, and the reverse-scan fallback must
    # still recover the reverse link from the intact .md files.
    shown = await _call(server, "memory_show", id=a_id)
    assert shown["id"] == a_id
    assert "reverse_links" in shown, (
        "reverse_links dropped on poisoned index meta; the reverse-scan "
        "fallback should have recovered it from the .md files"
    )
    assert len(shown["reverse_links"]) == 1
    rev = shown["reverse_links"][0]
    assert rev["type"] == "supersedes"
    assert rev["source_id"] == b_id


async def test_show_survives_unwritable_root_during_index_migration(
    server: Any, memory_dir: Path
) -> None:
    """A store root that lost its write bit (permission change, backup
    restore) turns `_ensure_schema`'s migration branch into an OSError
    source: the migration serialises on `flock_excl`, whose
    `os.open(..., O_CREAT)` of the `.index.sqlite.lock` sidecar raises
    `PermissionError` when the sidecar doesn't exist yet. That is an
    `OSError` — not `ValueError` / `sqlite3.DatabaseError` /
    `IndexVersionError` — so pre-fix it escaped `_links_payload`'s
    corruption guard and hard-crashed memory_show, the only index-read
    surface still letting it out (`index.status()` declares the class
    load-bearing for its never-raises contract; the search-annotation
    surface catches Exception). The show must instead take the same
    reverse-scan fallback as a torn file — the .md bodies are intact
    and still readable, the root kept its read bit.

    The back-dating connection is HELD OPEN across the show so the
    index's -wal/-shm siblings survive the chmod. Without a live
    connection SQLite removes them on close, and `_connect`'s WAL
    pragma in the read-only dir then fails first with
    `sqlite3.DatabaseError` (already guarded) — the held connection is
    what makes the flock line reachable, mirroring the real trigger:
    another process has the index open when the permission change
    lands.
    """
    import os
    import sqlite3

    from bettermemory import index as _index

    a_id = await _seed(server, "old version of the fact")
    b_id = await _seed(server, "new version of the fact")
    await _call(
        server,
        "memory_update",
        id=b_id,
        links=[{"type": "supersedes", "target_id": a_id}],
    )

    # Sanity: the healthy index serves the reverse link before the
    # tampering, so the post-chmod assertion proves the fallback.
    shown_before = await _call(server, "memory_show", id=a_id)
    assert shown_before.get("reverse_links")

    root = Path(memory_dir).expanduser().resolve()
    idx_path = _index.index_path(root)
    lock_path = idx_path.with_suffix(idx_path.suffix + ".lock")
    # The sidecar must be absent so the flock's os.open has to O_CREAT
    # it — an existing sidecar opens fine in a read-only dir. A fresh
    # store never migrated, so nothing has created it yet.
    assert not lock_path.exists()

    conn = sqlite3.connect(str(idx_path))
    try:
        # Back-date the on-disk version so the next index op enters the
        # migration branch (the only path that acquires the flock).
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(_index.SCHEMA_VERSION - 1),),
        )
        conn.commit()
        os.chmod(root, 0o500)
        try:
            # On some platforms (CI runners as root, Windows) the chmod
            # doesn't actually deny write — the OSError can't be provoked.
            if os.access(root, os.W_OK):
                pytest.skip("filesystem ignored chmod; cannot exercise it")
            # memory_show must not raise: the PermissionError from the
            # lock sidecar's os.open routes to the reverse-scan
            # fallback, which recovers the link from the .md files.
            shown = await _call(server, "memory_show", id=a_id)
        finally:
            os.chmod(root, 0o700)  # restore so pytest can clean up
    finally:
        conn.close()

    assert shown["id"] == a_id
    assert "reverse_links" in shown, (
        "reverse_links dropped on the unwritable-root migration OSError; "
        "the reverse-scan fallback should have recovered it from the .md "
        "files"
    )
    assert len(shown["reverse_links"]) == 1
    rev = shown["reverse_links"][0]
    assert rev["type"] == "supersedes"
    assert rev["source_id"] == b_id


async def test_no_inbound_show_opens_index_twice(
    server: Any, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perf regression guard: a `memory_show` of a memory with NO
    inbound links, against a HEALTHY POPULATED index, must open the
    index connection EXACTLY TWICE — one purposeful open each for two
    distinct jobs, and no more.

    1. **id -> path resolve** (swarm-convergence Phase 1). `load_one`
       resolves the id through the index (`filenames_for_ids`) instead
       of walking + reparsing the whole active directory. That is the
       whole point of the change: one O(1) index open replaces an
       O(corpus) walk (the Phase-0 benchmark measured that walk at
       ~320 ms for a single lookup at 3200 memories).
    2. **links + status** via `links_for_with_status`. This branch once
       split into two opens — `links_for` for inbound links, then a
       separate `index.status(...)` for `indexed_count` — until they
       were folded into one connection. That fold is still guarded
       here: a THIRD open means either the `status()` split came back
       OR the Phase 1 resolve regressed into a double-open.

    We count every `index._connect` call during the show. The store's
    seed writes happen BEFORE the counter is installed, so the only
    opens measured are the read-path ones. Asserts 2; FAILS at 3 on
    either regression above.
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
    assert opens["count"] == 2, (
        f"no-inbound memory_show opened the index {opens['count']} times; "
        "expected exactly 2 (one id->path resolve + one links_for_with_status). "
        "A third open means the status() split regressed or the Phase 1 "
        "id resolve double-opened."
    )


async def test_links_omitted_when_empty(server: Any) -> None:
    """A memory with no links carries neither `links` nor
    `reverse_links` — absence-as-signal, so the consumer branches on key
    presence and the wire shape stays compact for the common case. Same
    shape as the `corroborations` / `last_corroborated` pair, and the
    OPPOSITE of what `path_drift` / `commit_drift` do: memory_show emits
    those two keys unconditionally, using `null` as the no-signal value.
    Both halves are asserted below, so the contrast is pinned by the
    test rather than only described here."""
    mid = await _seed(server, "lone memory")
    shown = await _call(server, "memory_show", id=mid)
    assert "links" not in shown
    assert "reverse_links" not in shown
    # The contrasting half. A freshly-written memory is unverified and
    # cites no paths, so neither drift signal applies — and memory_show
    # still emits both keys, null rather than absent.
    assert shown["path_drift"] is None
    assert shown["commit_drift"] is None


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
