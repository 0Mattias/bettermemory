"""Provenance on the episode read surfaces (7.0.0).

An episode's label is read from the event log at each read
(`provenance.EpisodeEvidence`), never stored: `local` when an
`episode_write` event names the id, `untracked` when the log carries no
in-process event that could have named it, `unaccounted` otherwise.
`episode_search` keeps the body beside the label, the way `memory_show`
keeps a body beside an `unaccounted` memory; `episode_handoff`, the
reflexive first call of a loop iteration, delivers takeaways by default
and never a body for an unaccounted episode.

The planted shape every test here needs is a file written straight into
a session directory through `EpisodeStore.write`, which is what the
handler does minus the event it records.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bettermemory.config import Config, StorageConfig, TelemetryConfig
from bettermemory.episodes import EpisodeStore
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    return await _mcp_call(server, name, kwargs)


async def _rows(server: Any, **kwargs: Any) -> list[dict[str, Any]]:
    """`episode_search` rows. The SDK wraps a list-returning tool's
    structured content as `{"result": [...]}`; unwrap it."""
    res = await _call(server, "episode_search", **kwargs)
    if isinstance(res, dict) and "result" in res:
        return list(res["result"])
    return list(res)


def _config(memory_dir: Path, *, telemetry: bool = True) -> Config:
    return Config(
        storage=StorageConfig(directory=str(memory_dir)),
        telemetry=TelemetryConfig(enabled=telemetry),
    )


def _server(memory_dir: Path, *, telemetry: bool = True) -> Any:
    return build_server(
        config=_config(memory_dir, telemetry=telemetry),
        store=Store(memory_dir),
        state=SessionState(),
    )


def _session_with_body(memory_dir: Path, marker: str) -> str:
    ep_store = EpisodeStore(memory_dir)
    for sid in ep_store.iter_session_ids():
        if any(marker in e.body for e in ep_store.list_by_session(sid)):
            return sid
    raise AssertionError(f"no session holds {marker!r}")


def _plant(memory_dir: Path, session_id: str, marker: str) -> str:
    """A file written into `session_id`'s directory with no event."""
    planted = EpisodeStore(memory_dir).write(
        session_id=session_id,
        body=f"planted body {marker}",
        takeaway=f"planted takeaway {marker}",
    )
    return planted.id


# ---------------------------------------------------------------------------
# episode_search: the label rides beside the body
# ---------------------------------------------------------------------------


async def test_episode_search_rows_carry_the_provenance_label(
    memory_dir: Path,
) -> None:
    server_a = _server(memory_dir)
    await _call(server_a, "episode_write", body="journaled body", takeaway="from A")
    a_session = _session_with_body(memory_dir, "journaled body")
    planted_id = _plant(memory_dir, a_session, "in A's directory")

    rows = await _rows(_server(memory_dir), parent_session_id=a_session)
    by_id = {row["id"]: row for row in rows}
    assert len(by_id) == 2
    journaled = next(row for row in rows if row["takeaway"] == "from A")
    assert journaled["provenance"] == "local"
    assert by_id[planted_id]["provenance"] == "unaccounted"
    # The explicit read keeps the body beside the label, whatever it says.
    assert by_id[planted_id]["body"] == "planted body in A's directory"
    assert journaled["body"] == "journaled body"


async def test_episode_search_label_is_untracked_without_an_event_log(
    memory_dir: Path,
) -> None:
    """Telemetry off: no event names anything, so nothing is unaccounted
    either. Both the journaled and the planted episode read `untracked`."""
    server_a = _server(memory_dir, telemetry=False)
    await _call(server_a, "episode_write", body="quiet body", takeaway="from A")
    a_session = _session_with_body(memory_dir, "quiet body")
    _plant(memory_dir, a_session, "quietly")

    rows = await _rows(
        _server(memory_dir, telemetry=False), parent_session_id=a_session
    )
    assert [row["provenance"] for row in rows] == ["untracked", "untracked"]


async def test_episode_search_takeaway_only_rows_keep_the_label(
    memory_dir: Path,
) -> None:
    server_a = _server(memory_dir)
    await _call(server_a, "episode_write", body="scan me", takeaway="from A")
    rows = await _rows(_server(memory_dir), include_bodies=False)
    assert rows and all("body" not in row for row in rows)
    assert [row["provenance"] for row in rows] == ["local"]
