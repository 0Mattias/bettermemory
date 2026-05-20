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
