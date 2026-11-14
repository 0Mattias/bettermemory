"""memory_search's optional reads past its pool.

A search ranks the pool the full-text index draws for its query
(`handlers.search.resolve_search_pool`, tests/test_search_pool.py). Two
enrichments read a record the pool does not hold: `expand_top` reads the
top hit's body, and a hit's `depends_on` target that does not match the
query is loaded by id. Neither read may abort the search when it fails.
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


def _unwrap(res: Any) -> Any:
    return res.get("result", res) if isinstance(res, dict) and "result" in res else res


async def test_expand_top_survives_oserror_reading_body(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient OSError while loading the top hit's body for
    `expand_top` must NOT abort the whole search: the inline body is
    dropped but the ranked hits the caller already has are still
    returned. Before the fix only MemoryNotFoundError / TombstonedError
    were caught, so a flaky read of one body (vanished file, EIO on a
    network mount) raised straight out of memory_search.

    `Store.load_one` is reached only on the expand_top body-load path
    (the pool and the returned hits are read by other loaders), so
    patching `load_one` to raise isolates exactly the enrichment step
    under test.
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
    hit's `depends_on` target isn't in the query's pool, loads it
    directly via `store.load_one`. That loop
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
    NOT match the query — so A is absent from the pool (the records
    the query matches, just B) and the targeted-load branch fires
    `load_one(A)`, which we patch to raise. `expand_top` is left off so
    the only `load_one` call under test is the depends-on one.
    """
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
