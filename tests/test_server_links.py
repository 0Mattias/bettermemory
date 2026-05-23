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
