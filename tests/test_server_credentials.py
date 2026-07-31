"""Integration tests for the credential gate in memory_write / memory_update.

Pins the contract end-to-end through the MCP tool surface: a secret-shaped
body returns `credential_warning` without persisting anything, the raw
secret never reaches the event log, `acknowledge_credential=True` overrides
(recording the kind, not the value), and the gate fires before the
durability gate — and can't be bypassed via memory_update.

Fixture note: secret-SHAPED fixtures are assembled from fragments via
`_shaped(...)` so the complete token literal never appears in source — a
credential gate's own fixtures otherwise trip push-protection secret
scanners and block the push (see tests/test_credentials.py).
"""

from __future__ import annotations
from ._mcp import call_tool as _mcp_call

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder, iter_events
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


def _shaped(*parts: str) -> str:
    """Join fragments into a secret-shaped value with no scannable literal."""
    return "".join(parts)


# Public AWS example key (a shape fixture, never a live credential) + the
# synthetic secrets these tests feed the gate — all fragment-assembled.
_AWS = _shaped("AKIA", "IOSFODNN7EXAMPLE")
_OPENAI = _shaped("sk-", "abcdEFGH1234ijklMNOP5678")
_ANTHROPIC = _shaped("sk-ant-", "api03-SUPERsecretVALUE0123456789abcd")
_GITHUB = _shaped("ghp_", "1234567890abcdefABCDEF1234567890abcd")


@pytest.fixture
def server_with_events(memory_dir: Path) -> tuple[Any, Path]:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(root=memory_dir, session_id=state.session_id)
    server = build_server(
        config=cfg,
        store=Store(memory_dir),
        state=state,
        recorder=rec,
    )
    return server, memory_dir


async def _call(server: Any, name: str, **kwargs: Any) -> Any:
    """Invoke a tool and return its structured payload.

    Delegates to `tests/_mcp.py`, which owns the SDK's return shape so
    the mcp 2.x port edits one function rather than forty-four.
    """
    return await _mcp_call(server, name, kwargs)


# ---------------------------------------------------------------------------
# Secret-shaped bodies are blocked by default
# ---------------------------------------------------------------------------


async def test_credential_body_returns_warning(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content=f"My prod AWS access key is {_AWS}.",
        scopes=["infrastructure"],
    )
    assert res["status"] == "credential_warning"
    kinds = [m["kind"] for m in res["markers"]]
    assert "aws-access-key-id" in kinds


async def test_credential_warning_does_not_persist(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    await _call(
        server,
        "memory_write",
        content=f"openai key {_OPENAI} for the bot",
        scopes=["tools"],
    )
    listing = await _call(server, "memory_list")
    listing = (
        listing.get("result", listing)
        if isinstance(listing, dict) and "result" in listing
        else listing
    )
    assert listing == []


async def test_warning_response_redacts_secret(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content=f"prod key {_AWS} do not lose",
        scopes=["infrastructure"],
    )
    snippet = res["markers"][0]["snippet"]
    assert _AWS not in snippet
    assert "[redacted:aws-access-key-id]" in snippet


# ---------------------------------------------------------------------------
# The raw secret must never reach the event log
# ---------------------------------------------------------------------------


async def test_secret_never_written_to_event_log(
    server_with_events: tuple[Any, Path],
) -> None:
    """The strongest contract: the value the gate refused must not be
    recoverable from `.events.jsonl` — not in the warning event, not
    anywhere. We read the raw bytes, not the parsed events."""
    server, memory_dir = server_with_events
    secret = _ANTHROPIC
    await _call(
        server,
        "memory_write",
        content=f"anthropic key {secret} for prod",
        scopes=["tools"],
    )
    # Override path: even when committed, the value must stay out of the log.
    await _call(
        server,
        "memory_write",
        content=f"documented example key {secret}",
        scopes=["tools"],
        acknowledge_credential=True,
    )
    # Scan every active shard segment (and any legacy log), not one
    # file — the event log is sharded. The glob matches `.events.jsonl`
    # and `.events.NN.jsonl` but not the `.gz` archives.
    raw = "".join(
        p.read_text(encoding="utf-8") for p in sorted(memory_dir.glob(".events*.jsonl"))
    )
    assert secret not in raw
    # The kind, however, is logged so override-rate analytics works.
    assert "openai-anthropic-key" in raw


# ---------------------------------------------------------------------------
# Override path
# ---------------------------------------------------------------------------


async def test_acknowledge_credential_commits(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content=f"The AWS docs use the example key {_AWS} in their tutorial.",
        scopes=["reference"],
        acknowledge_credential=True,
    )
    assert res["status"] == "committed"


async def test_acknowledge_credential_records_kinds(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content=f"example key {_AWS} from the public AWS tutorial",
        scopes=["reference"],
        acknowledge_credential=True,
    )
    write_events = [e for e in iter_events(memory_dir) if e["kind"] == "write"]
    assert write_events
    e = write_events[-1]
    assert e["status"] == "committed"
    assert "aws-access-key-id" in e.get("credentials_acknowledged", [])


async def test_clean_body_records_empty_credentials_acknowledged(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content="The auth service uses JWT with rotating refresh tokens.",
        scopes=["projects:auth"],
    )
    write_events = [e for e in iter_events(memory_dir) if e["kind"] == "write"]
    assert write_events[-1]["status"] == "committed"
    assert write_events[-1]["credentials_acknowledged"] == []


# ---------------------------------------------------------------------------
# Ordering: credential fires before durability
# ---------------------------------------------------------------------------


async def test_credential_fires_before_durability(
    server_with_events: tuple[Any, Path],
) -> None:
    """A body with BOTH a secret and a transient marker returns
    credential_warning — the higher-severity refusal wins."""
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content=f"Currently the prod key is {_AWS}.",
        scopes=["infrastructure"],
    )
    assert res["status"] == "credential_warning"


async def test_acknowledged_credential_still_hits_durability(
    server_with_events: tuple[Any, Path],
) -> None:
    """Once acknowledge_credential passes the credential gate, the durability
    gate runs normally — a body that is also transient still warns."""
    server, _ = server_with_events
    res = await _call(
        server,
        "memory_write",
        content=f"Currently the example key is {_AWS}.",
        scopes=["infrastructure"],
        acknowledge_credential=True,
    )
    assert res["status"] == "transient_warning"


# ---------------------------------------------------------------------------
# Telemetry of the warning itself
# ---------------------------------------------------------------------------


async def test_credential_warning_logs_event_with_kinds(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    await _call(
        server,
        "memory_write",
        content=f"prod key {_AWS}",
        scopes=["infrastructure"],
    )
    write_events = [e for e in iter_events(memory_dir) if e["kind"] == "write"]
    assert len(write_events) == 1
    e = write_events[0]
    assert e["status"] == "credential_warning"
    assert "aws-access-key-id" in e["credential_kinds"]


# ---------------------------------------------------------------------------
# memory_update must not bypass the gate (review finding #7)
# ---------------------------------------------------------------------------


async def test_update_body_with_credential_warns_and_does_not_persist(
    server_with_events: tuple[Any, Path],
) -> None:
    """A secret can't be smuggled into the store by EDITING a memory: a body
    update that introduces a secret returns credential_warning and the stored
    body is unchanged."""
    server, _ = server_with_events
    created = await _call(
        server,
        "memory_write",
        content="The auth service uses JWT with rotating refresh tokens.",
        scopes=["projects:auth"],
    )
    mid = created["id"]
    upd = await _call(
        server,
        "memory_update",
        id=mid,
        content=f"the prod key is {_AWS}",
    )
    assert upd["status"] == "credential_warning"
    assert "aws-access-key-id" in [m["kind"] for m in upd["markers"]]
    # The stored body must be unchanged — nothing persisted.
    shown = await _call(server, "memory_show", id=mid)
    assert _AWS not in shown["body"]
    assert "rotating refresh tokens" in shown["body"]


async def test_update_secret_never_in_event_log(
    server_with_events: tuple[Any, Path],
) -> None:
    server, memory_dir = server_with_events
    created = await _call(
        server, "memory_write", content="auth notes.", scopes=["projects:auth"]
    )
    secret = _GITHUB
    await _call(server, "memory_update", id=created["id"], content=f"token {secret}")
    # Scan every active shard segment (and any legacy log), not one
    # file — the event log is sharded. The glob matches `.events.jsonl`
    # and `.events.NN.jsonl` but not the `.gz` archives.
    raw = "".join(
        p.read_text(encoding="utf-8") for p in sorted(memory_dir.glob(".events*.jsonl"))
    )
    assert secret not in raw
    assert "github-token" in raw


async def test_update_acknowledge_credential_commits(
    server_with_events: tuple[Any, Path],
) -> None:
    server, _ = server_with_events
    created = await _call(
        server, "memory_write", content="aws docs notes.", scopes=["reference"]
    )
    upd = await _call(
        server,
        "memory_update",
        id=created["id"],
        content=f"The public AWS tutorial uses {_AWS} as its example.",
        acknowledge_credential=True,
    )
    assert upd["status"] == "committed"


async def test_update_acknowledge_credential_records_kinds(
    server_with_events: tuple[Any, Path],
) -> None:
    """The override marker is auditable on the UPDATE surface too. An
    acknowledged body edit must record the detector KIND on the update
    success event (never the value), exactly as the write path does
    (write.py) and the proposal-accept choke point (proposals.py). Without
    it the too-loose-detector override-rate signal and a forensic
    `grep credentials_acknowledged` sweep silently miss every secret
    introduced by EDIT rather than by write."""
    server, memory_dir = server_with_events
    created = await _call(
        server, "memory_write", content="aws docs notes.", scopes=["reference"]
    )
    upd = await _call(
        server,
        "memory_update",
        id=created["id"],
        content=f"The public AWS tutorial uses {_AWS} as its example.",
        acknowledge_credential=True,
    )
    assert upd["status"] == "committed"
    # The committed update event (it carries `fields`, unlike the
    # credential_warning / stale short-circuits) must log the acknowledged
    # detector kind so override-rate analytics covers the edit surface.
    committed = [
        e for e in iter_events(memory_dir) if e["kind"] == "update" and e.get("fields")
    ]
    assert committed, "no committed update event recorded"
    assert "aws-access-key-id" in committed[-1].get("credentials_acknowledged", [])
    # ...and the raw secret must never reach the event log — kind only.
    # Scan every active shard segment (and any legacy log), not one
    # file — the event log is sharded. The glob matches `.events.jsonl`
    # and `.events.NN.jsonl` but not the `.gz` archives.
    raw = "".join(
        p.read_text(encoding="utf-8") for p in sorted(memory_dir.glob(".events*.jsonl"))
    )
    assert _AWS not in raw
