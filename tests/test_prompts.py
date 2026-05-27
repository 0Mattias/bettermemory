"""Drift tests for the model-facing policy surfaces.

Three surfaces carry the policy the model reads:

1. `SYSTEM_PROMPT_ADDENDUM` (in `prompts.py`) — programmatic embedding.
2. The fenced block in `docs/system_prompt.md` — copy-paste for humans.
3. `plugin/skills/bettermemory/SKILL.md` — loaded when the plugin's skill
   activates.

(1) and (2) are byte-equal by design — both are the same advanced-tightening
addendum, exposed two ways. (3) is the policy-as-companion surface for plugin
users; intentionally shorter and policy-focused, NOT a full tool inventory.

Failure modes guarded here:

- **Doc/code drift** between (1) and (2). Verbatim string comparison.

- **Tool-name drift** on (1) and (3): someone renames or removes an MCP
  tool but forgets to update the policy surface. Future clients then get
  told to call a tool the server doesn't expose. One-way parity tests
  against `build_server`'s registered tool names cover this — for each
  surface, every `memory_*` name it mentions must resolve to a real tool
  on the server. The reverse direction is intentionally NOT enforced
  (the skill is policy, not inventory; not every server tool needs to
  appear there).
"""

from __future__ import annotations

import re
from pathlib import Path

from bettermemory.config import Config, StorageConfig
from bettermemory.prompts import SYSTEM_PROMPT_ADDENDUM
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store


# Match the first ```...``` fence in the doc — the addendum is the first one.
_DOC_FENCE_RE = re.compile(r"```\n(.*?)```", re.DOTALL)

# Match any identifier of the form `memory_*` or `episode_*` appearing
# in the addendum that is NOT immediately followed by `=` (which would
# mark it as a keyword-argument name like `memory_ids=[...]`, not a
# tool reference). The addendum uses tool names in the explicit
# "Available tools:" list and in call shapes ("call
# memory_write_confirm(...)"); both populations need to map to real
# tools on the server. The episode_* family was added when the loop
# story shipped — keep the regex covering both so a future rename of
# either family catches the same parity check.
_TOOL_REF_RE = re.compile(r"\b((?:memory|episode)_[a-z_]+)\b(?!\s*=)")


def test_addendum_matches_docs() -> None:
    doc_path = Path(__file__).resolve().parents[1] / "docs" / "system_prompt.md"
    text = doc_path.read_text(encoding="utf-8")
    matches = _DOC_FENCE_RE.findall(text)
    assert matches, f"no fenced code block in {doc_path}"

    canonical = matches[0].strip()
    expected = SYSTEM_PROMPT_ADDENDUM.strip()
    assert canonical == expected, (
        "SYSTEM_PROMPT_ADDENDUM in prompts.py has drifted from "
        "docs/system_prompt.md. Update both in sync."
    )


async def test_addendum_tool_names_exist_on_server(tmp_path: Path) -> None:
    """Every `memory_*` tool referenced in the addendum is registered on the server.

    The previous version of this test only enforced parity between the
    addendum and the doc copy — renaming a tool on the server (or dropping
    one) would not fail the suite, and the addendum would silently start
    referencing a tool the server doesn't expose. This closes that gap.

    Direction is intentionally one-way: every name the addendum mentions
    must exist on the server. The reverse — every server tool must appear
    in the addendum — would be too strict (it's reasonable to ship a new
    tool one release without yet documenting it in the advanced-tightening
    surface), and the README/api.md cover the full inventory.
    """
    # Hermetic server build: tmp_path-backed store and a fresh SessionState
    # so the module-level singleton from `get_state()` isn't shared with
    # other tests. The list_tools call doesn't write anything to disk.
    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    registered = {tool.name for tool in await mcp.list_tools()}

    referenced = set(_TOOL_REF_RE.findall(SYSTEM_PROMPT_ADDENDUM))
    # Strip kwarg-shaped names the regex over-includes (`memory_ids`
    # is a parameter on `memory_record_use`, not a tool). Same
    # allowlist as the SKILL.md test below — keep them in sync.
    KNOWN_KWARGS = {"memory_ids", "episode_id"}
    referenced = {
        name
        for name in referenced
        if not name.endswith("_") and name not in KNOWN_KWARGS
    }

    missing = referenced - registered
    assert not missing, (
        f"SYSTEM_PROMPT_ADDENDUM references tools that aren't registered "
        f"on the server: {sorted(missing)}. Either rename the tool back, "
        f"register the new tool, or update the addendum to match."
    )


async def test_skill_tool_names_exist_on_server(tmp_path: Path) -> None:
    """Every `memory_*` tool referenced in the plugin's SKILL.md is
    registered on the server.

    Symmetric to the addendum check above, with the same one-way
    direction. SKILL.md is the policy companion for plugin users and
    deliberately doesn't enumerate every tool — it covers retrieval,
    writing, verification, record-use, and curation by name and lets
    the per-tool descriptions carry the rest. Tools the skill DOES
    name must still resolve on the server, or a rename would leave
    the plugin telling the model to call a nonexistent name.
    """
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "plugin"
        / "skills"
        / "bettermemory"
        / "SKILL.md"
    )
    skill_text = skill_path.read_text(encoding="utf-8")

    cfg = Config(storage=StorageConfig(directory=str(tmp_path)))
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    registered = {tool.name for tool in await mcp.list_tools()}

    referenced = set(_TOOL_REF_RE.findall(skill_text))
    # Strip kwarg-shaped names that the regex over-includes — `memory_ids`
    # is a parameter on `memory_record_use`, not a tool. Anything that
    # isn't actually registered AND isn't a real `memory_*` tool can
    # only be a kwarg name or doc artifact; the parameter-form regex on
    # `_TOOL_REF_RE` already drops `name=`, but a bare `memory_ids` in
    # prose still matches. Explicit allowlist of known kwargs keeps the
    # assertion's signal sharp.
    KNOWN_KWARGS = {"memory_ids", "episode_id"}
    referenced = {
        name
        for name in referenced
        if not name.endswith("_") and name not in KNOWN_KWARGS
    }

    missing = referenced - registered
    assert not missing, (
        f"SKILL.md references tools that aren't registered on the "
        f"server: {sorted(missing)}. Either rename the tool back, "
        f"register the new tool, or update SKILL.md to match."
    )
