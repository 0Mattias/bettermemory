"""Tests for the FTS5 candidate pre-filter on memory_search (T3.1 phase B).

The integration: when the store size exceeds BETTERMEMORY_INDEX_THRESHOLD
(default 500), memory_search queries the index for up to 50 candidate
ids and only loads those memories instead of walking the full active
set. Falls back to load_all when:

- the query is empty
- the index file doesn't exist
- the index is corrupt
- the indexed_count is below the threshold
- the index returns zero candidates (stale index suspected)

The fallback contract is load-bearing: result quality on small stores
must match the pre-phase-B behaviour exactly.
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


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_search_uses_load_all_on_small_store(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default threshold (500) is far above typical test corpus size,
    so the index pre-filter shouldn't activate. Pin the byte-stable
    behaviour: search results match what load_all + the rankers
    would have produced pre-T3.1."""
    monkeypatch.delenv("BETTERMEMORY_INDEX_THRESHOLD", raising=False)
    await _call(
        server, "memory_write", content="python list comprehension", scopes=["tools"]
    )
    await _call(server, "memory_write", content="rust borrow checker", scopes=["tools"])

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits
    assert hits[0]["match_terms"] == ["python"]


async def test_search_uses_index_when_threshold_crossed(
    server: Any, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lowering the threshold via env var puts the search through the
    index path. Result must still surface the matching memory — the
    index is a candidate filter, not a different ranker."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    await _call(
        server, "memory_write", content="python list comprehension", scopes=["tools"]
    )
    await _call(server, "memory_write", content="rust borrow checker", scopes=["tools"])

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits
    assert any("python" in h["match_terms"] for h in hits)
    # Other ranker fields still populate.
    assert all("score" in h for h in hits)


async def test_search_falls_back_when_index_empty_match(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query that returns zero index candidates triggers the
    load_all fallback so we don't silently miss recent writes that
    aren't reflected in a stale index. Test: query for a token that
    doesn't appear in any body — the index returns []; load_all
    returns [] too; the search returns []. The fallback's
    invocation isn't directly observable, but the result equivalence
    is."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    await _call(server, "memory_write", content="python notes", scopes=["tools"])

    hits = _unwrap(await _call(server, "memory_search", query="unrelated-token-xyz"))
    assert hits == []


async def test_search_falls_back_when_index_missing(
    server: Any, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the index file mid-process should not break searches.
    The handler detects exists=False via index.status and routes to
    load_all. The store's incremental hooks will recreate the index
    on the next write — but for read-only sessions the fallback
    keeps things working."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    await _call(
        server, "memory_write", content="python list comprehension", scopes=["tools"]
    )

    # Now delete the index file. The next search must still find the
    # memory via the load_all fallback.
    from bettermemory import index as _index

    _index.index_path(memory_dir).unlink()

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits
    assert any("python" in h["match_terms"] for h in hits)


async def test_search_falls_back_when_index_corrupt(
    server: Any, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overwriting the index with garbage simulates a corrupt SQLite
    file (mid-write crash, partial restore). The status check
    surfaces corrupt=True; the handler falls back to load_all."""
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    await _call(
        server, "memory_write", content="python list comprehension", scopes=["tools"]
    )

    from bettermemory import index as _index

    _index.index_path(memory_dir).write_bytes(b"not a sqlite database")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits


@pytest.mark.parametrize("corruption", ["garbage", "version_newer"])
async def test_show_falls_back_when_index_corrupt(
    server: Any,
    memory_dir: Path,
    corruption: str,
) -> None:
    """memory_show must NOT hard-crash when the FTS5 index is unusable.

    Parallels `test_search_falls_back_when_index_corrupt`: a torn /
    version-newer `.index.sqlite` is an anticipated operational state
    (the Store S4 divergence warning calls it out) and the index is a
    regenerable best-effort cache — the canonical `.md` bodies are
    intact, so the canonical single-id read path must degrade
    gracefully instead of raising a protocol error for EVERY id until
    reindex.

    `_links_payload` calls `index.links_for_with_status`, whose
    `_ensure_schema` raises `sqlite3.DatabaseError` on a
    truncated/garbage file and `index.IndexVersionError` when the
    on-disk `schema_version` is newer than this reader. Both are now
    caught and routed to the same zero-row reverse-scan fallback that
    serves the absent/empty-index case — which reads the `.md` files,
    so the B->A `supersedes` reverse link STILL surfaces on A even
    though the index can't answer.

    Both corruption modes are covered via parametrize:
    - `garbage`: overwrite the file with non-SQLite bytes
      (mid-write crash / partial restore) -> `sqlite3.DatabaseError`.
    - `version_newer`: stamp a `schema_version` higher than the code
      supports (a downgrade / forward-incompatible index) ->
      `index.IndexVersionError`.
    """
    import sqlite3

    from bettermemory import index as _index

    # B supersedes A; the reverse link lives in B's .md frontmatter.
    a = _unwrap(
        await _call(server, "memory_write", content="old fact", scopes=["tools"])
    )
    b = _unwrap(
        await _call(server, "memory_write", content="new fact", scopes=["tools"])
    )
    await _call(
        server,
        "memory_update",
        id=b["id"],
        links=[{"type": "supersedes", "target_id": a["id"]}],
    )

    # Sanity: the healthy index serves the reverse link before we
    # corrupt it, so the post-corruption assertion proves the fallback
    # (not a coincidentally-empty link set).
    shown_before = _unwrap(await _call(server, "memory_show", id=a["id"]))
    assert shown_before.get("reverse_links")

    index_file = _index.index_path(memory_dir)
    if corruption == "garbage":
        # Truncated / torn SQLite file -> sqlite3.DatabaseError out of
        # links_for_with_status.
        index_file.write_bytes(b"not a sqlite database")
    else:
        # On-disk schema newer than this reader -> IndexVersionError out
        # of _ensure_schema. Stamp directly into the existing meta row.
        with sqlite3.connect(str(index_file)) as conn:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                (str(_index.SCHEMA_VERSION + 1),),
            )
            conn.commit()

    # memory_show must not raise, and the reverse-scan fallback must
    # still recover the reverse link from the intact .md files.
    shown = _unwrap(await _call(server, "memory_show", id=a["id"]))
    assert shown["id"] == a["id"]
    assert "reverse_links" in shown, (
        "reverse_links dropped when the index was corrupt; the "
        "reverse-scan fallback should have recovered it from the .md files"
    )
    assert len(shown["reverse_links"]) == 1
    rev = shown["reverse_links"][0]
    assert rev["type"] == "supersedes"
    assert rev["source_id"] == b["id"]


async def test_index_threshold_env_var_resets_per_call(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The threshold is read at search time (not handler-init time)
    so test isolation works — bumping the env var mid-test changes
    behaviour on the next search. Pin the contract so a future
    refactor that caches the threshold won't silently break test
    setups."""
    await _call(
        server, "memory_write", content="python list comprehension", scopes=["tools"]
    )

    # Default threshold: load_all path.
    monkeypatch.delenv("BETTERMEMORY_INDEX_THRESHOLD", raising=False)
    hits_default = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits_default

    # Lowered threshold: index path.
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    hits_indexed = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits_indexed

    # Same memory surfaces both ways — quality byte-stable.
    assert hits_default[0]["id"] == hits_indexed[0]["id"]


async def test_search_skips_candidate_when_index_filename_drifts(
    server: Any, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the H4 review finding. `sync pull` (and any
    out-of-band rewrite of the memory directory) can leave the
    index's `filename` column pointing at a file whose body now
    belongs to a different memory id. Without an id-equality
    check the handler would score the candidate's FTS hit against
    the wrong body. The fix verifies `memory.id == candidate_id`
    after loading; mismatched files are silently dropped."""
    import sqlite3

    from bettermemory import index as _index

    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    a = _unwrap(
        await _call(
            server,
            "memory_write",
            content="python list comprehension",
            scopes=["tools"],
        )
    )
    b = _unwrap(
        await _call(
            server,
            "memory_write",
            content="kubernetes networking notes",
            scopes=["infra"],
        )
    )

    # Drift: rewrite the index so a.id's row points at b's filename.
    # This simulates the post-sync-pull state where files moved
    # behind the index's back. A search for "python" still finds a
    # via FTS (the body column is correct), but the filename lookup
    # delivers b's file. Without the defense, the handler returns
    # b's body scored against a's query and surfaces it as a hit
    # for "python".
    db_path = _index.index_path(memory_dir)
    with sqlite3.connect(str(db_path)) as conn:
        b_filename = conn.execute(
            "SELECT filename FROM memories WHERE id = ?", (b["id"],)
        ).fetchone()[0]
        conn.execute(
            "UPDATE memories SET filename = ? WHERE id = ?",
            (b_filename, a["id"]),
        )

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    # The mis-pointed candidate is silently dropped; the search
    # falls back through the rest of the pipeline. b is not in
    # `hits` masquerading as a "python" match.
    for h in hits:
        if h["id"] == a["id"]:
            # If the drift defense fired correctly, a may be missing
            # entirely from the hit list (because its filename lookup
            # returned b's body and the id check rejected it) — that's
            # acceptable, the post-pull reindex restores it.
            continue
        assert h["id"] != b["id"] or "python" in (h.get("body") or ""), (
            f"index drift produced a wrong hit: id={h['id']} surfaced "
            f"for query 'python' but body is not python-related"
        )


async def test_search_falls_back_to_load_all_when_all_filenames_drift(
    server: Any, memory_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the audit finding that pairs with the H4 fix.

    When `_index.query` returns candidate ids but the filename lookup
    fails for *every* candidate (pre-v2 schema rows, the id-drift
    defense above rejecting every load, etc.), the previous shape
    returned an empty list — search silently missed results that
    would have surfaced under `load_all`. The fallback now catches
    this: empty `loaded` after a non-empty `candidate_pairs` routes
    through `load_all` so the FTS hit isn't lost.
    """
    import sqlite3

    from bettermemory import index as _index

    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    await _call(
        server,
        "memory_write",
        content="python list comprehension",
        scopes=["tools"],
    )

    # Drift every filename: blank the column for every row. FTS still
    # matches on body, but `filenames_for_ids` returns empty (the
    # function skips rows with empty filenames). Pre-fix, the handler
    # returned [] — the test would see zero hits despite the body
    # matching.
    db_path = _index.index_path(memory_dir)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE memories SET filename = ''")

    hits = _unwrap(await _call(server, "memory_search", query="python"))
    # Fallback to load_all must surface the matching memory.
    assert hits, (
        "search returned empty when every index filename was stale; "
        "the load_all fallback should have caught this"
    )
    assert any("python" in h["match_terms"] for h in hits)


async def test_expand_top_survives_oserror_reading_body(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient OSError while loading the top hit's body for
    `expand_top` must NOT abort the whole search: the inline body is
    dropped but the ranked hits the caller already has are still
    returned. Before the fix only MemoryNotFoundError / TombstonedError
    were caught, so a flaky read of one body (vanished file, EIO on a
    network mount) raised straight out of memory_search.

    `Store.load_one` is reached only on the expand_top body-load path —
    the small-store candidate pool comes from `load_all`, which uses
    `_load_path`, not `load_one` — so patching `load_one` to raise
    isolates exactly the enrichment step under test.
    """
    await _call(
        server,
        "memory_write",
        content="alpha beta gamma the quick brown fox jumps over",
        scopes=["tools"],
    )

    def _raise_oserror(self: Store, memory_id: str) -> Any:
        raise OSError("transient read failure on the backing file")

    monkeypatch.setattr(Store, "load_one", _raise_oserror)

    # Full-coverage query so the top hit's relevance is "high" and the
    # expand_top branch actually fires.
    hits = _unwrap(
        await _call(
            server,
            "memory_search",
            query="alpha beta gamma quick brown fox jumps over",
            expand_top=True,
        )
    )
    # The search did not raise; the top hit still came back, just without
    # the inline body the failed expansion would have added.
    assert hits, "search aborted on a transient OSError reading the top-hit body"
    assert hits[0]["relevance"] == "high"
    assert "body" not in hits[0]


async def test_depends_on_targeted_load_survives_oserror(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient OSError while targeted-loading a `depends_on` target
    must NOT abort the whole search (FIX 3).

    `attach_depends_on_resolved` runs BEFORE `expand_top` and, when a
    hit's `depends_on` target isn't in the query's FTS prefilter
    candidate set, loads it directly via `store.load_one`. That loop
    only caught `(MemoryNotFoundError, TombstonedError)` — NOT OSError —
    so a flaky read (EIO, vanished file on a network mount) of the
    target's backing file raised straight out of `memory_search`,
    aborting an otherwise-successful search. The fix widens the catch
    to include OSError, silently dropping the unreadable target from the
    auto-pull (same as a missing/tombstoned one).

    Setup is load-bearing and differs from
    `test_expand_top_survives_oserror_reading_body`: that test has NO
    `depends_on` link, so the targeted-load path never invokes
    `load_one`. Here B `depends_on` A, and A's body deliberately does
    NOT match the query — so A is absent from B's prefilter candidate
    set and the targeted-load branch fires `load_one(A)`, which we
    patch to raise. `expand_top` is left off so the only `load_one`
    call under test is the depends-on one.

    Forces the FTS index pre-filter (`BETTERMEMORY_INDEX_THRESHOLD=1`).
    That's REQUIRED, not incidental: on the default (load_all) path the
    candidate set is the whole store, so A is always present in the
    side-map and the targeted-load branch never fires `load_one`. The
    index pre-filter narrows candidates to query-relevant rows (just B),
    so A is genuinely absent and the `store.load_one(A)` fallback — the
    line under test — actually runs.
    """
    # Route the search through the index candidate pre-filter so the
    # candidate pool excludes A (non-matching body) and the targeted
    # load fires. The index loads candidates via `_load_path`, not
    # `load_one`, so the patched `load_one` below only intercepts the
    # depends-on targeted load.
    monkeypatch.setenv("BETTERMEMORY_INDEX_THRESHOLD", "1")
    # A: the dependency target. Distinct vocabulary so a query for
    # "python" never surfaces it via FTS — it can only be reached
    # through B's depends_on edge (the targeted-load path).
    a = _unwrap(
        await _call(
            server,
            "memory_write",
            content="kubernetes networking internals and the cluster overlay",
            scopes=["tools"],
        )
    )
    # B: the hit. Matches the query and depends_on A.
    b = _unwrap(
        await _call(
            server,
            "memory_write",
            content="python list comprehension notes",
            scopes=["tools"],
        )
    )
    # Wire the depends_on edge B -> A via memory_update (REPLACE
    # semantics on `links`). Done BEFORE the monkeypatch so the update's
    # own load isn't broken by the patched load_one.
    await _call(
        server,
        "memory_update",
        id=b["id"],
        links=[{"type": "depends_on", "target_id": a["id"]}],
    )

    def _raise_oserror(self: Store, memory_id: str) -> Any:
        raise OSError("transient read failure on the depends-on target")

    monkeypatch.setattr(Store, "load_one", _raise_oserror)

    # Query hits B; the targeted-load of A (not in the candidate set)
    # raises OSError. On the unfixed code that propagates and aborts the
    # search; on the fixed code A is silently dropped from the auto-pull.
    hits = _unwrap(await _call(server, "memory_search", query="python"))
    assert hits, (
        "search aborted on a transient OSError targeted-loading a depends_on target"
    )
    by_id = {h["id"]: h for h in hits}
    assert b["id"] in by_id, "the matching hit B must still be returned"
    # A was unreadable, so the auto-pull resolves nothing — the field is
    # absent (same absence-as-signal as a missing/tombstoned target).
    assert not by_id[b["id"]].get("depends_on_resolved")
