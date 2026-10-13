"""A memory_write response lists at most `SIMILAR_HITS_SHOWN` similar
memories per list and counts the rest; the write event keeps every id.

On a store of a thousand memories a short body grazed dozens as "medium",
and one committed response outgrew the client's tool-output limit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory._response import SIMILAR_HITS_SHOWN
from bettermemory.config import Config, StorageConfig
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool

# Each seeded body holds every token of `_NEW_BODY` plus three of its own,
# so each scores medium against it (measured 0.444) and none against
# another seeded body at high.
_NEW_BODY = "kubernetes ingress nginx controller logging"


def _seed_body(i: int) -> str:
    return f"kubernetes ingress nginx controller tls variant{i} tuning{i} notes{i}"


@pytest.fixture
def server(store: Store, memory_dir: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    return build_server(config=cfg, store=store, state=SessionState())


def _write_events(store: Store, status: str) -> list[dict[str, Any]]:
    return [
        e
        for e in store.iter_events()
        if e.get("kind") == "write" and e.get("status") == status
    ]


async def test_committed_response_caps_related_and_counts_the_rest(
    server: Any, store: Store
) -> None:
    for i in range(32):
        store.write(content=_seed_body(i), scopes=["tools"])
    out = await call_tool(
        server, "memory_write", {"content": _NEW_BODY, "scopes": ["tools"]}
    )
    assert out["status"] == "committed", out
    assert len(out["related"]) == SIMILAR_HITS_SHOWN == 5
    assert out["related_more"] == 32 - 5
    assert "removed_related" not in out

    event = _write_events(store, "committed")[-1]
    assert len(event["related"]) == 32
    assert [h["id"] for h in out["related"]] == event["related"][:5]


async def test_committed_response_caps_removed_related(
    server: Any, store: Store
) -> None:
    for i in range(12):
        memory = store.write(content=_seed_body(i), scopes=["tools"])
        store.tombstone(memory.id, reason="archived")
    out = await call_tool(
        server, "memory_write", {"content": _NEW_BODY, "scopes": ["tools"]}
    )
    assert out["status"] == "committed", out
    assert len(out["removed_related"]) == 5
    assert out["removed_related_more"] == 12 - 5
    assert all(h["relevance"] == "medium-removed" for h in out["removed_related"])

    event = _write_events(store, "committed")[-1]
    assert len(event["removed_related"]) == 12
    assert [h["id"] for h in out["removed_related"]] == event["removed_related"][:5]


async def test_few_hits_carry_no_more_count(server: Any, store: Store) -> None:
    for i in range(5):
        store.write(content=_seed_body(i), scopes=["tools"])
        removed = store.write(content=_seed_body(100 + i), scopes=["tools"])
        store.tombstone(removed.id, reason="archived")
    out = await call_tool(
        server, "memory_write", {"content": _NEW_BODY, "scopes": ["tools"]}
    )
    assert out["status"] == "committed", out
    assert len(out["related"]) == 5
    assert len(out["removed_related"]) == 5
    assert "related_more" not in out
    assert "removed_related_more" not in out


async def test_duplicate_refusal_caps_matches(server: Any, store: Store) -> None:
    for i in range(7):
        store.write(content=f"{_NEW_BODY} v{i}", scopes=["tools"])
    out = await call_tool(
        server, "memory_write", {"content": _NEW_BODY, "scopes": ["tools"]}
    )
    assert out["status"] == "duplicate", out
    assert len(out["matches"]) == 5
    assert out["matches_more"] == 2
    event = _write_events(store, "duplicate")[-1]
    assert len(event["matches"]) == 7
    # The corroboration still credits the top match.
    assert event["corroborated_id"] == out["matches"][0]["id"]


async def test_previously_removed_refusal_caps_removed_matches(
    server: Any, store: Store
) -> None:
    for i in range(7):
        memory = store.write(content=f"{_NEW_BODY} v{i}", scopes=["tools"])
        store.tombstone(memory.id, reason="wrong")
    out = await call_tool(
        server, "memory_write", {"content": _NEW_BODY, "scopes": ["tools"]}
    )
    assert out["status"] == "previously_removed", out
    assert len(out["removed_matches"]) == 5
    assert out["removed_matches_more"] == 2
    event = _write_events(store, "previously_removed")[-1]
    assert len(event["removed_matches"]) == 7
