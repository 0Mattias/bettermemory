"""Validate the Claude Code plugin manifests at the repo root.

The plugin scaffold is documentation/distribution rather than runtime
code — Python doesn't import any of it. But it ships in the repo, and
a malformed manifest or a version drift between `pyproject.toml`,
`.claude-plugin/marketplace.json`, and `plugin/.claude-plugin/plugin.json`
would break a real install. These tests are cheap and catch the typos
before they become a user-visible "the plugin won't enable" report.

A note on layout: the repo root carries `.claude-plugin/marketplace.json`
(declaring the repo as a plugin marketplace) plus a `plugin/`
subdirectory with the actual plugin (`.claude-plugin/plugin.json`,
`.mcp.json`, `skills/`, `README.md`). The marketplace points at
`./plugin` as the plugin source. Putting the plugin in a subdirectory
keeps `.mcp.json` from being auto-loaded as a project-scoped MCP
config when working *in* the bettermemory repo itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Repo root is two parents up from this test file (tests/test_plugin.py).
REPO_ROOT = Path(__file__).resolve().parent.parent

MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_PATH = REPO_ROOT / "plugin"
PLUGIN_MANIFEST_PATH = PLUGIN_PATH / ".claude-plugin" / "plugin.json"
PLUGIN_MCP_PATH = PLUGIN_PATH / ".mcp.json"
PLUGIN_SKILL_PATH = PLUGIN_PATH / "skills" / "bettermemory" / "SKILL.md"
PLUGIN_README_PATH = PLUGIN_PATH / "README.md"
PLUGIN_HOOKS_PATH = PLUGIN_PATH / "hooks" / "hooks.json"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_pyproject_version() -> str:
    """Read `[project].version` from pyproject.toml. Pin a tomllib
    import rather than a third-party parser — Python 3.11+ has it
    in stdlib, and the project's `requires-python` is already 3.11."""
    import tomllib

    with PYPROJECT_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


# ---------------------------------------------------------------------------
# File existence — the plugin scaffold is a small set of files and
# every one is load-bearing. A missing one breaks the install.
# ---------------------------------------------------------------------------


def test_marketplace_json_exists() -> None:
    assert MARKETPLACE_PATH.exists(), (
        f"marketplace manifest is missing at {MARKETPLACE_PATH.relative_to(REPO_ROOT)}"
    )


def test_plugin_manifest_exists() -> None:
    assert PLUGIN_MANIFEST_PATH.exists(), (
        f"plugin manifest is missing at {PLUGIN_MANIFEST_PATH.relative_to(REPO_ROOT)}"
    )


def test_plugin_mcp_config_exists() -> None:
    assert PLUGIN_MCP_PATH.exists(), (
        f".mcp.json is missing from the plugin at "
        f"{PLUGIN_MCP_PATH.relative_to(REPO_ROOT)} — the plugin would "
        f"install but not register the MCP server"
    )


def test_plugin_skill_exists() -> None:
    assert PLUGIN_SKILL_PATH.exists(), (
        f"SKILL.md is missing at {PLUGIN_SKILL_PATH.relative_to(REPO_ROOT)} — "
        f"users would get the MCP tools but no system-prompt policy"
    )


def test_plugin_readme_exists() -> None:
    assert PLUGIN_README_PATH.exists()


# ---------------------------------------------------------------------------
# JSON validity — argparse for the plugin manifest. A trailing comma
# or smart quote here breaks the install.
# ---------------------------------------------------------------------------


def test_marketplace_json_is_valid_json() -> None:
    json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))


def test_plugin_manifest_is_valid_json() -> None:
    json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_plugin_mcp_config_is_valid_json() -> None:
    json.loads(PLUGIN_MCP_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Manifest schema — minimum required fields per Claude Code plugin docs.
# ---------------------------------------------------------------------------


def test_marketplace_lists_the_bettermemory_plugin() -> None:
    """The marketplace must enumerate the plugin under a recognizable
    name and point at the `plugin/` source. Without the source pointer,
    `/plugin install bettermemory@bettermemory` would 404 the plugin
    inside the marketplace."""
    data = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
    assert data["name"] == "bettermemory"
    plugins = data.get("plugins", [])
    assert len(plugins) >= 1
    bm = next((p for p in plugins if p.get("name") == "bettermemory"), None)
    assert bm is not None, "marketplace must list a plugin named `bettermemory`"
    # Source must point at the plugin directory; relative paths are
    # resolved relative to the marketplace's own location.
    assert bm.get("source") in ("./plugin", "plugin"), (
        f"unexpected plugin source {bm.get('source')!r} — expected './plugin'"
    )
    assert bm.get("description"), "plugin description must be non-empty"


def test_plugin_manifest_has_required_fields() -> None:
    """Per Claude Code docs the manifest needs `name` + `version`
    minimum; the schema's `description`, `author`, `license`, and
    `homepage` are conventional but not strictly required. We assert
    the conventional set so a sloppy update doesn't ship without
    them."""
    data = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))
    for key in ("name", "version", "description", "author", "license", "homepage"):
        assert key in data, f"plugin.json missing required field: {key!r}"
    assert data["name"] == "bettermemory"
    assert data["license"] == "MIT"


def test_plugin_mcp_config_registers_bettermemory_under_canonical_name() -> None:
    """The MCP server entry name becomes the prefix on the model's
    tool names (`mcp__bettermemory__memory_search`, etc.). Renaming
    this key is a breaking change for any consumer that hard-codes
    tool names — pin it. The shape includes `type/command/args/env`
    to match what Claude Code's own `claude mcp add` writes."""
    data = json.loads(PLUGIN_MCP_PATH.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    servers = data["mcpServers"]
    assert "bettermemory" in servers, (
        "plugin .mcp.json must register the server under the key "
        "`bettermemory` so tool names match what users on the manual "
        "install path see"
    )
    entry = servers["bettermemory"]
    assert entry.get("type") == "stdio"
    assert entry.get("command")
    assert isinstance(entry.get("args"), list)
    assert isinstance(entry.get("env"), dict)


# ---------------------------------------------------------------------------
# Version sync — the same number lives in three places. They must agree
# at release time, otherwise the marketplace lists a different version
# than the published wheel.
# ---------------------------------------------------------------------------


def test_plugin_manifest_version_matches_pyproject() -> None:
    """`pyproject.toml` is the single source of truth for the package
    version. The plugin manifest carries its own version field for
    Claude Code's internal versioning; if they diverge, users on the
    plugin path would see a different reported version than `pip show`
    or `bettermemory --version`. Keep them in lockstep."""
    pyproject_v = _load_pyproject_version()
    manifest_v = json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))["version"]
    assert manifest_v == pyproject_v, (
        f"plugin/.claude-plugin/plugin.json version ({manifest_v!r}) "
        f"does not match pyproject.toml version ({pyproject_v!r}). "
        f"Bump both at release time."
    )


def test_marketplace_version_matches_pyproject() -> None:
    """Same idea for the marketplace's metadata.version — surfaced in
    the listing UI and in `claude mcp list` output."""
    pyproject_v = _load_pyproject_version()
    market = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
    market_v = market.get("metadata", {}).get("version")
    assert market_v == pyproject_v, (
        f".claude-plugin/marketplace.json metadata.version ({market_v!r}) "
        f"does not match pyproject.toml version ({pyproject_v!r}). "
        f"Bump both at release time."
    )


# ---------------------------------------------------------------------------
# SKILL.md — frontmatter shape + content sanity.
# ---------------------------------------------------------------------------


def test_skill_has_frontmatter_with_name_and_description() -> None:
    """Skills require YAML frontmatter at the top with `name` and
    `description`. The description is what tells the model when to load
    the skill — an empty or missing description is a silent failure
    (the skill exists but is never invoked)."""
    body = PLUGIN_SKILL_PATH.read_text(encoding="utf-8")
    assert body.startswith("---\n"), (
        "SKILL.md must start with a `---` frontmatter delimiter"
    )
    # Hand-parse the frontmatter rather than depend on a YAML library
    # (this test would otherwise need to import pyyaml unnecessarily;
    # the frontmatter is small and well-formed enough for a regex-free
    # walk).
    end = body.find("\n---\n", 4)
    assert end > 0, "SKILL.md frontmatter is unterminated"
    fm = body[4:end]
    assert "name:" in fm
    assert "description:" in fm
    # Description is what the model uses to triage; long-enough to be
    # a useful trigger but short enough to fit alongside other skills'
    # descriptions in the available-skills list.
    desc_line = next(
        (line for line in fm.splitlines() if line.startswith("description:")),
        None,
    )
    assert desc_line is not None
    description = desc_line.split(":", 1)[1].strip()
    assert len(description) >= 80, (
        f"skill description is suspiciously short ({len(description)} chars) "
        f"— the model needs enough text to know when to load this skill"
    )


def test_skill_mentions_load_bearing_tools() -> None:
    """A skill that doesn't enumerate the tools the user is supposed to
    invoke is just a vibes document. Pin the names so a refactor that
    drops one shows up here."""
    body = PLUGIN_SKILL_PATH.read_text(encoding="utf-8")
    for tool in (
        "memory_search",
        "memory_write",
        "memory_show",
        "memory_verify",
        "memory_record_use",
        "memory_scope_overview",
    ):
        assert tool in body, (
            f"skill body should reference the {tool!r} tool — without it "
            f"the model has no anchor to call the right thing"
        )


@pytest.mark.parametrize(
    "phrase",
    [
        # The opt-in policy is the whole reason the project exists.
        "OPT-IN retrieval",
        # The transparency requirement — without this the skill is
        # incomplete.
        "transparency",
        # The verification obligation — separates "use stale memory" from
        # "spot-check stale memory."
        "verify",
    ],
)
def test_skill_carries_load_bearing_phrases(phrase: str) -> None:
    body = PLUGIN_SKILL_PATH.read_text(encoding="utf-8").lower()
    assert phrase.lower() in body, (
        f"skill is missing the load-bearing phrase {phrase!r}"
    )


# ---------------------------------------------------------------------------
# Stop hook — wired so memory_audit_turn fires at end-of-turn for plugin
# users without manual settings.json edits.
# ---------------------------------------------------------------------------


def test_plugin_ships_stop_hook() -> None:
    """The 2.1 release shipped `memory_audit_turn` for silent-miss
    telemetry but didn't ship the Stop hook that fires it. Without
    the hook, the audit was opt-in-on-top-of-opt-in — the model had
    to choose to call it AND the user had to manually wire
    settings.json. The plugin now ships hooks/hooks.json declaring
    the Stop binding so plugin install is enough."""
    assert PLUGIN_HOOKS_PATH.exists(), (
        f"plugin hooks manifest missing at "
        f"{PLUGIN_HOOKS_PATH.relative_to(REPO_ROOT)}"
    )


def test_stop_hook_calls_audit_turn() -> None:
    """The Stop binding must invoke `bettermemory audit-turn`. Pin
    the exact command so a rename of the CLI subcommand or the hook
    config shape shows up here, not as a silent telemetry regression
    when users next install the plugin."""
    body = json.loads(PLUGIN_HOOKS_PATH.read_text(encoding="utf-8"))
    assert "hooks" in body, "missing top-level 'hooks' key"
    assert "Stop" in body["hooks"], "Stop event binding missing"
    stop_entries = body["hooks"]["Stop"]
    assert stop_entries, "Stop entry list is empty"
    # Find the command-form hook.
    command_hooks = [
        h
        for entry in stop_entries
        for h in entry.get("hooks", [])
        if h.get("type") == "command"
    ]
    assert command_hooks, "no command-form hook under Stop"
    matched = [
        h
        for h in command_hooks
        if "bettermemory audit-turn" in h.get("command", "")
    ]
    assert matched, (
        f"none of the Stop command hooks call `bettermemory audit-turn`; "
        f"got: {[h.get('command') for h in command_hooks]}"
    )


def test_stop_hook_has_reasonable_timeout() -> None:
    """The hook fires on every Stop event. A missing or huge timeout
    would block turn completion if the CLI ever wedged. Pin a
    sub-minute upper bound to lock in the principle — hooks must not
    visibly block."""
    body = json.loads(PLUGIN_HOOKS_PATH.read_text(encoding="utf-8"))
    command_hooks = [
        h
        for entry in body["hooks"]["Stop"]
        for h in entry.get("hooks", [])
        if h.get("type") == "command"
    ]
    for hook in command_hooks:
        timeout = hook.get("timeout")
        assert timeout is not None, (
            f"Stop hook is missing a timeout field: {hook!r}"
        )
        assert 0 < timeout <= 60, (
            f"Stop hook timeout {timeout!r} is outside the 1..60s window "
            f"— hooks must not visibly block turn completion"
        )
