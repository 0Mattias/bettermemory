"""Episode credential gate and the credential hints (9.0.0rc12).

- `episode(action="write")` runs the credential detector over body and
  takeaway and refuses a hit in memory_write's `credential_warning` shape,
  with no acknowledge flag: nothing is written, and the event names the
  kinds and the fields, never the value.
- The memory_write and memory_update credential hints no longer name the
  removed `sync` lane.

Fixture note: secret-SHAPED fixtures are assembled from fragments via
`_shaped(...)` so the complete token literal never appears in source (see
tests/test_credentials.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bettermemory import events
from bettermemory.builder import build_server
from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.log import CONTROL_KINDS
from bettermemory.session import SessionState
from bettermemory.store import MUTATION_KINDS, Store

from ._mcp import call_tool
from ._review_rows import FACTS
from .test_credentials import _AWS, _STRIPE
from .test_membership_security import stand_in_checkout


def _shaped(*parts: str) -> str:
    """Join fragments into a secret-shaped value with no scannable literal."""
    return "".join(parts)


_GHO = _shaped("gho_", "1234567890abcdefABCDEF1234567890abcd")
_ASSIGNMENT = _shaped("password=", "Hunter2Abc9XyZ12Q")


def _raw_event_log(store: Store) -> str:
    """The telemetry rows' raw payload text, straight from the log table
    (the mutation rows carry committed bodies by design)."""
    return "".join(
        str(row["payload"])
        for row in store.conn.execute("SELECT kind, payload FROM log ORDER BY seq")
        if row["kind"] not in MUTATION_KINDS and row["kind"] not in CONTROL_KINDS
    )


def _rows(store: Store, kind: str) -> list[dict[str, Any]]:
    return [e for e in store.iter_events() if e.get("kind") == kind]


# ---------------------------------------------------------------------------
# episode(action="write") runs the credential gate
# ---------------------------------------------------------------------------


@pytest.fixture
def episode_server(
    memory_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Store]:
    """A server whose recorder writes into the store's log, run from a
    checkout (an episode is homed to the caller's checkout)."""
    stand_in_checkout(tmp_path_factory.mktemp("checkout"), monkeypatch)
    store = Store(memory_dir)
    state = SessionState()
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=state,
        recorder=Recorder(store=store, session_id=state.session_id),
    )
    return server, store


async def _episode(server: Any, **args: Any) -> dict[str, Any]:
    return await call_tool(server, "episode", {"action": "write", **args})


async def test_an_episode_body_with_a_secret_is_refused_and_not_written(
    episode_server: tuple[Any, Store],
) -> None:
    server, store = episode_server
    res = await _episode(
        server, body=f"iteration 3: deployed with {_AWS} from the runner"
    )
    assert res["status"] == "credential_warning"
    assert [(m["kind"], m["field"]) for m in res["markers"]] == [
        ("aws-access-key-id", "body")
    ]
    assert "[redacted:aws-access-key-id]" in res["markers"][0]["snippet"]
    assert "no acknowledgement" in res["hint"]
    assert "acknowledge_credential" not in res["hint"]
    assert _AWS not in json.dumps(res)
    assert store.episode_session_ids() == []
    assert not [r for r in store.log_rows() if r.kind == "episode_put"]
    (row,) = _rows(store, "episode_write")
    assert row["status"] == "credential_warning"
    assert row["credential_kinds"] == ["aws-access-key-id"]
    assert row["credential_fields"] == ["body"]
    assert events.HARNESS_SESSION_FIELD not in row
    assert _AWS not in _raw_event_log(store)


async def test_an_episode_takeaway_with_a_secret_is_refused(
    episode_server: tuple[Any, Store],
) -> None:
    server, store = episode_server
    res = await _episode(
        server,
        body="Rotated the billing credentials after the incident review.",
        takeaway=f"new key is {_STRIPE}",
    )
    assert res["status"] == "credential_warning"
    assert [(m["kind"], m["field"]) for m in res["markers"]] == [
        ("stripe-key", "takeaway")
    ]
    assert _STRIPE not in json.dumps(res)
    assert store.episode_session_ids() == []


async def test_both_fields_are_reported_when_both_carry_a_secret(
    episode_server: tuple[Any, Store],
) -> None:
    server, store = episode_server
    res = await _episode(server, body=f"token {_GHO}", takeaway=f"{_ASSIGNMENT}")
    assert res["status"] == "credential_warning"
    assert {(m["kind"], m["field"]) for m in res["markers"]} == {
        ("github-token", "body"),
        ("generic-secret-assignment", "takeaway"),
    }
    (row,) = _rows(store, "episode_write")
    assert row["credential_fields"] == ["body", "takeaway"]


async def test_a_clean_episode_still_commits(
    episode_server: tuple[Any, Store],
) -> None:
    server, store = episode_server
    res = await _episode(
        server,
        body="Iteration 2 tried the API key rotation script; it needs a dry-run flag.",
        takeaway="the rotation script wants a dry-run flag",
    )
    assert res["status"] == "committed", res
    assert len(store.episode_session_ids()) == 1


async def test_an_oversized_episode_is_refused_by_size_before_any_detector(
    episode_server: tuple[Any, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caps run first, as on memory_write: a multi-MB body never
    reaches the regexes."""
    from mcp.server.mcpserver.exceptions import ToolError

    from bettermemory.handlers import episode_write

    def boom(*_a: Any, **_k: Any) -> list[Any]:
        raise AssertionError("the detector ran before the size cap")

    monkeypatch.setattr(episode_write, "find_credential_markers", boom)
    server, _ = episode_server
    with pytest.raises(ToolError, match="max_content_bytes"):
        await _episode(server, body="x" * (2 * 1024 * 1024))


# ---------------------------------------------------------------------------
# The credential hints name no sync lane
# ---------------------------------------------------------------------------


async def test_the_credential_hints_name_no_sync_lane(memory_dir: Path) -> None:
    store = Store(memory_dir)
    server = build_server(
        config=Config(storage=StorageConfig(directory=str(memory_dir))),
        store=store,
        state=SessionState(),
    )
    secret_body = f"The deploy key is {_AWS} on the runner."
    refused = await call_tool(
        server, "memory_write", {"content": secret_body, "scopes": ["tools"]}
    )
    assert refused["status"] == "credential_warning"
    written = await call_tool(
        server, "memory_write", {"content": FACTS[3], "scopes": ["tools"]}
    )
    edited = await call_tool(
        server, "memory_update", {"id": written["id"], "content": secret_body}
    )
    assert edited["status"] == "credential_warning"
    for hint in (refused["hint"], edited["hint"]):
        assert "sync" not in hint and "via git" not in hint
        assert hint.startswith("The ")
        assert "Describe the secret without embedding it" in hint
        assert "acknowledge_credential=True" in hint
