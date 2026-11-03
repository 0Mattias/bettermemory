"""A call on a record that is not there as the call needs it is refused in
the store's words, unquoted (ENG-98).

The store answers a removed, unknown or still-active id with an error of
the KeyError family, and a KeyError's ``str()`` is its message quoted, as
a key is printed. The tool boundary refuses each with its message
(`_refusals_as_tool_errors`). memory_show, memory_update, memory_verify,
memory_remove and memory_admin's restore used to turn the error into a
ValueError of its ``str()`` first, so the client read the message inside
quotes; they now let it reach the boundary as the store raised it. A
refusal that quotes the store's words inside its own (memory_write's
``supersedes``, a conflict verdict on a removed member) takes them from
`store_schema.absent_text`, as the boundary does, and so does the CLI's
``tombstones restore``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from bettermemory._handlers import RefusedError
from bettermemory.config import Config, StorageConfig, load_config
from bettermemory.events import Recorder
from bettermemory.handlers import verify as verify_handler
from bettermemory.models import Memory, generate_ulid
from bettermemory.server import build_server
from bettermemory.server import main as cli_main
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool


@pytest.fixture
def server(store: Store) -> Any:
    state = SessionState()
    rec = Recorder(store=store, session_id=state.session_id, enabled=True)
    cfg = Config(storage=StorageConfig(directory=str(store.path.parent)))
    return build_server(config=cfg, store=store, state=state, recorder=rec)


async def _refused(server: Any, name: str, arguments: dict[str, Any]) -> str:
    """The text of the refusal a tool call is answered with, after
    checking it is a refusal with that text on the wire and no quote
    around it."""
    with pytest.raises(ToolError) as caught:
        await call_tool(server, name, arguments)
    refusal = caught.value.__cause__
    assert isinstance(refusal, RefusedError), caught.value
    said = str(refusal)
    assert str(caught.value).endswith(said)
    assert not said.startswith("'") and not said.endswith("'"), said
    return said


@pytest.fixture
def removed(store: Store) -> Memory:
    memory = store.write(content="the release runs from the tag", scopes=["global"])
    store.tombstone(memory.id, "superseded by the signed tag")
    return memory


async def test_show_of_an_unknown_id_is_refused_unquoted(server: Any) -> None:
    """memory_show on an id the store never held says so, unquoted."""
    unknown = generate_ulid()
    said = await _refused(server, "memory_show", {"id": unknown})
    assert said == f"no memory with id {unknown}"


async def test_show_of_a_removed_id_is_refused_unquoted(
    server: Any, removed: Memory
) -> None:
    """memory_show on a removed id names the removal and its reason, unquoted."""
    said = await _refused(server, "memory_show", {"id": removed.id})
    assert said == f"memory {removed.id} was removed: superseded by the signed tag"


async def test_update_of_an_unknown_id_is_refused_unquoted(server: Any) -> None:
    """memory_update on an id the store never held says so, unquoted."""
    unknown = generate_ulid()
    said = await _refused(
        server, "memory_update", {"id": unknown, "content": "the release runs daily"}
    )
    assert said == f"no memory with id {unknown}"


async def test_update_of_a_removed_id_is_refused_unquoted(
    server: Any, removed: Memory
) -> None:
    """memory_update on a removed id names the removal, unquoted."""
    said = await _refused(
        server, "memory_update", {"id": removed.id, "content": "the release runs daily"}
    )
    assert said == f"memory {removed.id} was removed: superseded by the signed tag"


async def test_verify_of_an_unknown_id_is_refused_unquoted(server: Any) -> None:
    """memory_verify on an id the store never held says so, unquoted."""
    unknown = generate_ulid()
    said = await _refused(server, "memory_verify", {"id": unknown, "note": "checked"})
    assert said == f"no memory with id {unknown}"


async def test_verify_of_a_removed_id_is_refused_unquoted(
    server: Any, removed: Memory
) -> None:
    """memory_verify on a removed id names the removal, unquoted."""
    said = await _refused(
        server, "memory_verify", {"id": removed.id, "note": "checked"}
    )
    assert said == f"memory {removed.id} was removed: superseded by the signed tag"


async def test_a_verify_that_loses_to_a_removal_is_refused_unquoted(
    store: Store, server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memory_verify reads its snapshot, then another writer removes the
    record before the stamp's transaction: the store's refusal under its
    lock reaches the client unquoted, as the snapshot read's does."""
    memory = store.write(content="the release runs from the tag", scopes=["global"])
    real = verify_handler._verified_head

    def removed_first(*args: Any, **kwargs: Any) -> Any:
        # The last read before the stamp, after the snapshot.
        store.tombstone(memory.id, "removed by the other writer")
        return real(*args, **kwargs)

    monkeypatch.setattr(verify_handler, "_verified_head", removed_first)
    said = await _refused(server, "memory_verify", {"id": memory.id, "note": "checked"})
    assert said == f"memory {memory.id} was removed: removed by the other writer"
    assert not store.has_memory(memory.id)


async def test_removing_an_id_twice_is_refused_unquoted(
    store: Store, server: Any
) -> None:
    """The second memory_remove of one id names the first removal's reason,
    unquoted."""
    memory = store.write(content="the release runs from the tag", scopes=["global"])
    await call_tool(server, "memory_remove", {"id": memory.id, "reason": "gone"})
    said = await _refused(
        server, "memory_remove", {"id": memory.id, "reason": "gone again"}
    )
    assert said == f"memory {memory.id} was removed: gone"


async def test_restoring_an_active_id_is_refused_unquoted(
    store: Store, server: Any
) -> None:
    """memory_admin's restore of an active id says there is nothing to
    restore, unquoted."""
    memory = store.write(content="the release runs from the tag", scopes=["global"])
    said = await _refused(
        server, "memory_admin", {"action": "restore", "id": memory.id}
    )
    assert said == f"memory {memory.id} is active; nothing to restore"


async def test_restoring_an_unknown_id_is_refused_unquoted(server: Any) -> None:
    """memory_admin's restore of an id with no tombstone says so, unquoted."""
    unknown = generate_ulid()
    said = await _refused(server, "memory_admin", {"action": "restore", "id": unknown})
    assert said == f"no tombstone with id {unknown}"


async def test_a_write_superseding_a_removed_id_names_the_removal_unquoted(
    server: Any, removed: Memory
) -> None:
    """memory_write declaring it supersedes a removed id is refused with
    the removal in the store's words, unquoted, after the id it names."""
    said = await _refused(
        server,
        "memory_write",
        {
            "content": "the release runs from the signed tag",
            "scopes": ["global"],
            "supersedes": [removed.id],
        },
    )
    assert said.endswith(
        f"is not an active memory: memory {removed.id} was removed: "
        "superseded by the signed tag"
    )


def test_cli_restore_of_an_unknown_id_prints_the_message_unquoted(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``tombstones restore`` of an id with no tombstone exits 2 with the
    store's message after ``error:``, unquoted."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path))
    Store(load_config().resolved_directory()).close()
    unknown = generate_ulid()
    monkeypatch.setattr(sys, "argv", ["bettermemory", "tombstones", "restore", unknown])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.rstrip("\n").endswith(f"error: no tombstone with id {unknown}"), err
