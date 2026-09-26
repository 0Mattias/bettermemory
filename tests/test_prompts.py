"""Drift tests for the model-facing policy surfaces.

Three surfaces carry the policy the model reads:

1. `SYSTEM_PROMPT_ADDENDUM` in `prompts.py`, for programmatic embedding.
2. The fenced block in `docs/system_prompt.md`, the copy-paste for humans.
3. `plugin/skills/bettermemory/SKILL.md`, loaded when the plugin's skill
   activates.

(1) and (2) are byte-equal by design. (3) is the policy companion for
plugin users, shorter and not a full inventory. Two failure modes are
guarded: drift between (1) and (2), and a tool name in (1) or (3) that
the server no longer registers.
"""

from __future__ import annotations

import re
from pathlib import Path

from bettermemory.config import Config, StorageConfig
from bettermemory.prompts import SYSTEM_PROMPT_ADDENDUM
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

_REPO = Path(__file__).resolve().parents[1]
_SKILL = _REPO / "plugin" / "skills" / "bettermemory" / "SKILL.md"

# The first fenced block in the doc is the addendum.
_DOC_FENCE_RE = re.compile(r"```\n(.*?)```", re.DOTALL)
_TOOL_REF_RE = re.compile(
    r"\b(memory_(?:search|show|write|update|remove|verify|record_use|admin)|episode)\b"
)


def _tool_refs(text: str) -> set[str]:
    return set(_TOOL_REF_RE.findall(text))


async def _registered_tool_names(tmp_path: Path) -> set[str]:
    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    return {tool.name for tool in await mcp.list_tools()}


def test_addendum_matches_docs() -> None:
    text = (_REPO / "docs" / "system_prompt.md").read_text(encoding="utf-8")
    matches = _DOC_FENCE_RE.findall(text)
    assert matches, "no fenced code block in docs/system_prompt.md"
    assert matches[0].strip() == SYSTEM_PROMPT_ADDENDUM.strip(), (
        "SYSTEM_PROMPT_ADDENDUM in prompts.py has drifted from "
        "docs/system_prompt.md. Update both in sync."
    )


def test_addendum_names_the_nine_tools() -> None:
    for name in (
        "memory_search",
        "memory_show",
        "memory_write",
        "memory_update",
        "memory_remove",
        "memory_verify",
        "memory_record_use",
        "episode",
        "memory_admin",
    ):
        assert name in SYSTEM_PROMPT_ADDENDUM, name


async def test_addendum_tool_names_exist_on_server(tmp_path: Path) -> None:
    """Every tool the addendum names is one the server registers."""
    registered = await _registered_tool_names(tmp_path)
    referenced = _tool_refs(SYSTEM_PROMPT_ADDENDUM)
    assert referenced, "the addendum references no tools at all; regex broke."
    missing = referenced - registered
    assert not missing, f"addendum names unregistered tools: {sorted(missing)}"


async def test_skill_tool_names_exist_on_server(tmp_path: Path) -> None:
    """Every tool the plugin's SKILL.md names is one the server registers."""
    registered = await _registered_tool_names(tmp_path)
    referenced = _tool_refs(_SKILL.read_text(encoding="utf-8"))
    assert referenced, "SKILL.md references no tools at all; regex broke."
    missing = referenced - registered
    assert not missing, f"SKILL.md names unregistered tools: {sorted(missing)}"
