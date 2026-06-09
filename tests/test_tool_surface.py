"""The lean default tool surface and the `full_tool_surface` gate.

The shipped MCP server hides seven curation / power-user tools by default
(measured dead-or-rare in the dogfood event log, plus memory_curate which
executes the consolidate engine). They register only under
`full_tool_surface`, except `memory_proposals`, which also surfaces whenever
the opt-in `[proposals]` feature is on (it is that feature's UI). See
`builder._register_tools` and `BehaviorConfig.full_tool_surface`.
"""

from __future__ import annotations

from pathlib import Path

from bettermemory.builder import build_server
from bettermemory.config import (
    BehaviorConfig,
    Config,
    ProposalsConfig,
    StorageConfig,
    load_config,
)
from bettermemory.session import SessionState
from bettermemory.store import Store


# The seven tools gated out of the lean default surface.
_GATED = {
    "memory_health",
    "memory_curate",
    "memory_acknowledge_miss",
    "memory_rename_scope",
    "memory_restore",
    "memory_list_tombstones",
    "memory_proposals",
}

# A representative sample of the always-registered core (retrieval / write /
# verify / record-use / episode) surface. Not exhaustive — the count
# assertions below pin the totals.
_ALWAYS = {
    "memory_search",
    "memory_show",
    "memory_list",
    "memory_scope_overview",
    "memory_write",
    "memory_update",
    "memory_verify",
    "memory_record_use",
    "episode_write",
    "episode_handoff",
}

_LEAN_COUNT = 18
_FULL_COUNT = 25


async def _registered(
    tmp_path: Path,
    behavior: BehaviorConfig,
    proposals: ProposalsConfig | None = None,
) -> set[str]:
    cfg = Config(
        storage=StorageConfig(directory=str(tmp_path)),
        behavior=behavior,
        proposals=proposals or ProposalsConfig(),
    )
    mcp = build_server(config=cfg, store=Store(tmp_path), state=SessionState())
    return {tool.name for tool in await mcp.list_tools()}


async def test_lean_surface_omits_curation_tools(tmp_path: Path) -> None:
    """full_tool_surface=False (the shipped default): none of the six gated
    tools register, the core surface stays intact, total is 18."""
    names = await _registered(tmp_path, BehaviorConfig(full_tool_surface=False))
    assert names.isdisjoint(_GATED)
    assert _ALWAYS <= names
    assert len(names) == _LEAN_COUNT


async def test_full_surface_registers_everything(tmp_path: Path) -> None:
    """full_tool_surface=True: every gated tool is back; total is 25."""
    names = await _registered(tmp_path, BehaviorConfig(full_tool_surface=True))
    assert _GATED <= names
    assert len(names) == _FULL_COUNT


async def test_proposals_autosurfaces_when_feature_enabled(tmp_path: Path) -> None:
    """Lean surface, but [proposals] auto_propose is on — memory_proposals
    registers (it's that feature's review UI); the other five stay gated."""
    names = await _registered(
        tmp_path,
        BehaviorConfig(full_tool_surface=False),
        ProposalsConfig(auto_propose=True),
    )
    assert "memory_proposals" in names
    assert names.isdisjoint(_GATED - {"memory_proposals"})
    assert len(names) == _LEAN_COUNT + 1


def test_shipped_default_is_lean_despite_full_dataclass_default(
    tmp_path: Path,
) -> None:
    """The deliberate asymmetry: an explicitly-constructed Config defaults to
    the full surface (programmatic embedders), but the shipped server —
    load_config with no user-set key — is lean. Mirrors the documented
    exception in test_config.py's round-trip test."""
    assert BehaviorConfig().full_tool_surface is True
    # load_config writes DEFAULT_CONFIG (which sets no full_tool_surface key)
    # and reads it back; the loader default is lean.
    loaded = load_config(tmp_path / "config.toml")
    assert loaded.behavior.full_tool_surface is False
