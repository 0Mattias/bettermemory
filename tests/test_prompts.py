"""Drift tests for the system-prompt addendum.

The addendum is the canonical instruction surface for advanced tightening,
exported two ways: as `SYSTEM_PROMPT_ADDENDUM` for programmatic embedding
and as a fenced code block in `docs/system_prompt.md` for humans copying
into their CLAUDE.md. There's no build step generating one from the other,
so we gate against drift here.

Two failure modes we want to catch:

1. **Doc/code drift**: someone edits one but not the other. The verbatim
   string comparison covers this.

2. **Tool-name drift**: someone renames or removes an MCP tool but forgets
   to update the addendum's "Available tools:" list. The addendum's prose
   throughout still talks about that tool by name, so future clients reading
   it get told to call a tool the server doesn't expose. The parity test
   against `build_server`'s registered tool names covers this.
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

# Match any identifier of the form `memory_*` appearing in the addendum
# that is NOT immediately followed by `=` (which would mark it as a
# keyword-argument name like `memory_ids=[...]`, not a tool reference).
# The addendum uses tool names in the explicit "Available tools:" list
# and in call shapes ("call memory_write_confirm(...)"); both populations
# need to map to real tools on the server.
_TOOL_REF_RE = re.compile(r"\b(memory_[a-z_]+)\b(?!\s*=)")


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
    # Drop tool-prefix-shaped strings that are obviously placeholder
    # references in prose (none exist today, but if the addendum starts
    # talking about a hypothetical `memory_FOO` we don't want the test
    # to fail for a doc artifact). The current addendum uses real
    # names exclusively, so this is just future-proofing.
    referenced = {name for name in referenced if not name.endswith("_")}

    missing = referenced - registered
    assert not missing, (
        f"SYSTEM_PROMPT_ADDENDUM references tools that aren't registered "
        f"on the server: {sorted(missing)}. Either rename the tool back, "
        f"register the new tool, or update the addendum to match."
    )
