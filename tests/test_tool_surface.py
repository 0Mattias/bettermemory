"""The nine-tool surface and its per-session budget.

bettermemory 9 serves nine tools, always: seven single tools and two
dispatch tools (`episode`, `memory_admin`). There is no gated surface and
no configuration that changes the list. The budget test measures what a
client pastes into context every turn: the descriptions, the input
schemas as served (titles and `default: null` scrubbed) and the server
instructions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bettermemory.builder import INSTRUCTIONS, build_server
from bettermemory.config import Config, StorageConfig
from bettermemory.session import SessionState
from bettermemory.store import Store

NINE = {
    "memory_search",
    "memory_show",
    "memory_write",
    "memory_update",
    "memory_remove",
    "memory_verify",
    "memory_record_use",
    "episode",
    "memory_admin",
}

# The ceiling a regression trips. The bettermemory 9 declaration predicted
# 7,400 characters and set 8,000 as its miss line; the surface measured
# 8,241 at U4a with the descriptions at the floor of meaning and the
# schema envelope at 4,591. D1 removed the groundedness parameters from
# memory_write (design item 8, owner ruling 2026-09-30), which took the
# surface to 7,991, so the ceiling is the declared 8,000.
SESSION_BUDGET_CHARS = 8_000

# The parameters D1 removed with the groundedness gate. None may return.
REMOVED_WRITE_PARAMETERS = {
    "groundedness_check",
    "source_transcript",
    "acknowledge_ungrounded",
}


async def _server(tmp_path: Path) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    return build_server(config=cfg, store=Store(tmp_path), state=SessionState())


async def test_nine_tools(tmp_path: Path) -> None:
    """The U4 ruling: nine tools, always, with no gated surface."""
    mcp = await _server(tmp_path)
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == NINE


async def test_every_tool_has_a_description_and_a_schema(tmp_path: Path) -> None:
    mcp = await _server(tmp_path)
    for tool in await mcp.list_tools():
        assert tool.description and tool.description.strip(), tool.name
        assert tool.input_schema.get("type") == "object", tool.name


async def test_served_schemas_carry_no_titles_and_no_null_defaults(
    tmp_path: Path,
) -> None:
    mcp = await _server(tmp_path)
    for tool in await mcp.list_tools():
        blob = json.dumps(tool.input_schema)
        assert '"title"' not in blob, tool.name
        assert '"default": null' not in blob, tool.name


async def test_session_surface_under_budget(tmp_path: Path) -> None:
    mcp = await _server(tmp_path)
    tools = await mcp.list_tools()
    descriptions = sum(len(t.description or "") for t in tools)
    schemas = sum(len(json.dumps(t.input_schema, separators=(",", ":"))) for t in tools)
    total = descriptions + schemas + len(INSTRUCTIONS)
    assert total <= SESSION_BUDGET_CHARS, (
        f"served surface is {total} chars (descriptions {descriptions}, "
        f"schemas {schemas}, instructions {len(INSTRUCTIONS)}); "
        f"the ceiling is {SESSION_BUDGET_CHARS}"
    )


async def test_memory_write_schema_has_no_groundedness_parameters(
    tmp_path: Path,
) -> None:
    mcp = await _server(tmp_path)
    by_name = {t.name: t for t in await mcp.list_tools()}
    write = by_name["memory_write"]
    properties = set(write.input_schema["properties"])
    assert properties.isdisjoint(REMOVED_WRITE_PARAMETERS), properties
    blob = json.dumps(write.input_schema).lower() + (write.description or "").lower()
    assert "grounded" not in blob
    assert "transcript" not in blob


def test_instructions_fit_claude_code() -> None:
    """Claude Code truncates the instructions block past about 1.8 KB."""
    assert 700 <= len(INSTRUCTIONS) <= 1_500
    assert len(INSTRUCTIONS.encode("utf-8")) <= 1_750


async def test_dispatch_actions_are_enumerated(tmp_path: Path) -> None:
    mcp = await _server(tmp_path)
    by_name = {t.name: t for t in await mcp.list_tools()}
    episode = by_name["episode"].input_schema["properties"]["action"]
    assert episode["enum"] == ["write", "handoff"]
    admin = by_name["memory_admin"].input_schema["properties"]["action"]
    assert admin["enum"] == [
        "restore",
        "tombstones",
        "health",
        "rename_scope",
        "conflicts",
        "acknowledge_miss",
        "disable_scope",
        "enable_scope",
    ]
    assert by_name["episode"].input_schema["required"] == ["action"]
    assert by_name["memory_admin"].input_schema["required"] == ["action"]
