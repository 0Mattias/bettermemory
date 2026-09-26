"""Provenance on the episode handoff surface.

An episode's label comes from the store's pointer check at each read:
`local` when the row's pointer into the log verifies, `unaccounted` when
it does not. `episode(action="handoff")`, the reflexive first call of a
loop iteration, delivers takeaways by default and never a body for an
unaccounted episode.

The planted shape every test here needs is a row inserted into the
`episodes` table by SQL with a NULL `log_mac`: what the store writes
minus the log row that would vouch for it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bettermemory.config import Config, StorageConfig
from bettermemory.models import generate_ulid
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


async def _handoff(server: Any, **kwargs: Any) -> dict[str, Any]:
    return await _call(server, "episode", action="handoff", **kwargs)


def _server(memory_dir: Path) -> Any:
    return build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=Store(memory_dir),
        state=SessionState(),
    )


def _session_with_body(memory_dir: Path, marker: str) -> str:
    store = Store.open(memory_dir)
    for sid in store.episode_session_ids():
        if any(marker in e.body for e in store.episodes_by_session(sid)):
            return sid
    raise AssertionError(f"no session holds {marker!r}")


def _plant(memory_dir: Path, session_id: str, marker: str) -> str:
    """A row inserted into `session_id` with no log row behind it."""
    store = Store.open(memory_dir)
    planted_id = generate_ulid()
    with store.conn:
        store.conn.execute(
            "INSERT INTO episodes(id, session_id, created, body, scopes_json, "
            "takeaway, origin_json, is_floor, swarm_id, log_mac) "
            "VALUES (?, ?, ?, ?, '[]', ?, NULL, 0, NULL, NULL)",
            (
                planted_id,
                session_id,
                datetime.now(timezone.utc).isoformat(),
                f"planted body {marker}",
                f"planted takeaway {marker}",
            ),
        )
    assert store.episode_provenance([planted_id]) == {planted_id: "unaccounted"}
    return planted_id


# ---------------------------------------------------------------------------
# episode handoff: takeaways by default, bodies on request, never unaccounted
# ---------------------------------------------------------------------------


async def test_episode_handoff_delivers_takeaways_by_default_and_bodies_on_request(
    memory_dir: Path,
) -> None:
    server_a = _server(memory_dir)
    await _call(
        server_a, "episode", action="write", body="journaled body", takeaway="from A"
    )

    server_b = _server(memory_dir)
    res = await _handoff(server_b)
    (row,) = res["episodes"]
    assert row["takeaway"] == "from A"
    assert row["provenance"] == "local"
    assert "body" not in row

    res = await _handoff(server_b, include_bodies=True)
    (row,) = res["episodes"]
    assert row["body"] == "journaled body"
    assert row["provenance"] == "local"

    events = Store.open(memory_dir).iter_events()
    handoffs = [e for e in events if e.get("kind") == "episode_handoff"]
    assert [e["include_bodies"] for e in handoffs] == [False, True]


async def test_episode_handoff_never_delivers_an_unaccounted_body(
    memory_dir: Path,
) -> None:
    """A row planted into a legitimate session rides the auto-resolved
    handoff with its takeaway and the `unaccounted` label, and no body
    even when bodies were asked for. The journaled episode beside it
    delivers its body."""
    server_a = _server(memory_dir)
    await _call(
        server_a, "episode", action="write", body="journaled body", takeaway="from A"
    )
    a_session = _session_with_body(memory_dir, "journaled body")
    planted_id = _plant(memory_dir, a_session, "in A's session")

    res = await _handoff(_server(memory_dir), include_bodies=True)
    assert res["prior_session_id"] == a_session
    assert "note" not in res
    by_id = {row["id"]: row for row in res["episodes"]}
    planted = by_id.pop(planted_id)
    assert planted["provenance"] == "unaccounted"
    assert "body" not in planted
    assert planted["takeaway"] == "planted takeaway in A's session"
    (journaled,) = by_id.values()
    assert journaled["provenance"] == "local"
    assert journaled["body"] == "journaled body"


async def test_episode_handoff_explicit_planted_session_reads_unaccounted(
    memory_dir: Path,
) -> None:
    """An explicit `prior_session_id` is read verbatim, and a session the
    log never saw yields rows that all read `unaccounted`, bodies
    withheld whatever the caller passed."""
    # One in-process event, so the log covers the plants' creation.
    await _call(_server(memory_dir), "memory_search", query="anything")
    _plant(memory_dir, "sess_planted01", "one")
    _plant(memory_dir, "sess_planted01", "two")

    res = await _handoff(
        _server(memory_dir), prior_session_id="sess_planted01", include_bodies=True
    )
    assert res["prior_session_id"] == "sess_planted01"
    assert [row["provenance"] for row in res["episodes"]] == [
        "unaccounted",
        "unaccounted",
    ]
    assert all("body" not in row for row in res["episodes"])
    assert [row["takeaway"] for row in res["episodes"]] == [
        "planted takeaway one",
        "planted takeaway two",
    ]
