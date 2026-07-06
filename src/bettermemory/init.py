"""`bettermemory init` — onboard a fresh install in one command.

Two modes:

1. **Show-and-tell** (no `--client` flag). Prints the detected
   `bettermemory` binary path, the canonical MCP config snippet, the
   common per-client config-file locations (with existence markers), and
   the post-install verification ping. The user copies the snippet into
   their client by hand. Useful for clients we don't auto-patch and for
   "just tell me what to do" exploration.

2. **Patch** (`--client X`). Idempotently merges the bettermemory entry
   into the named client's MCP config file, creating parent dirs and
   the file if missing. Existing entries with identical content become
   no-ops; a different entry under the same name is updated rather than
   duplicated. Stranger-friendly install: one command and the client is
   wired up.

`--print-only` short-circuits to "just dump the JSON snippet" — useful
for pipelines like `bettermemory init --client cursor --print-only |
jq …`. `--json` returns a structured machine-readable view of all of
the above (binary path, snippet, known client paths, optional patch
result, and the addendum if requested) for tooling that wants to
introspect.

The addendum is `--with-addendum`-gated rather than printed by default
because the server-level MCP `instructions` block already carries the
load-bearing parts (see `prompts.py` + `server._build_mcp`); the
addendum is now an optional tightening document, not part of the
required setup.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import _fsutil
from .prompts import SYSTEM_PROMPT_ADDENDUM


# ---------------------------------------------------------------------------
# Per-client config locations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientPaths:
    """A known MCP client and the candidate config paths we know about
    for it. `paths[0]` is the default target when `--client` is set
    without `--config-path`; later entries are alternatives surfaced in
    show-and-tell mode so the user knows their options."""

    name: str
    description: str
    paths: tuple[Path, ...]


def _claude_code_paths() -> ClientPaths:
    # Claude Code reads `~/.claude.json` for user-scope MCP servers, and
    # `./.mcp.json` (project root) for project-scope. We default to user
    # scope on auto-patch — most strangers want "this works everywhere",
    # not "this works in one repo".
    return ClientPaths(
        name="claude-code",
        description="Claude Code CLI",
        paths=(
            Path.home() / ".claude.json",
            Path.cwd() / ".mcp.json",
        ),
    )


def _claude_desktop_paths() -> ClientPaths:
    home = Path.home()
    sys_name = platform.system()
    # Per-platform Claude Desktop config locations. Documented at
    # https://modelcontextprotocol.io/quickstart/user — we mirror those
    # rather than re-derive via platformdirs because Claude Desktop
    # ignores the freedesktop spec on Linux (it uses `~/.config/Claude`
    # rather than `$XDG_CONFIG_HOME/Claude`).
    if sys_name == "Darwin":
        path = (
            home
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    elif sys_name == "Windows":
        appdata = os.environ.get("APPDATA")
        roaming = Path(appdata) if appdata else home / "AppData" / "Roaming"
        path = roaming / "Claude" / "claude_desktop_config.json"
    else:
        path = home / ".config" / "Claude" / "claude_desktop_config.json"
    return ClientPaths(
        name="claude-desktop",
        description="Claude Desktop",
        paths=(path,),
    )


def _cursor_paths() -> ClientPaths:
    # Cursor: user-scope at `~/.cursor/mcp.json`; project-scope at
    # `<repo>/.cursor/mcp.json`. Same pattern as Claude Code.
    return ClientPaths(
        name="cursor",
        description="Cursor",
        paths=(
            Path.home() / ".cursor" / "mcp.json",
            Path.cwd() / ".cursor" / "mcp.json",
        ),
    )


def _continue_paths() -> ClientPaths:
    return ClientPaths(
        name="continue",
        description="Continue",
        paths=(Path.home() / ".continue" / "config.json",),
    )


def _cline_paths() -> ClientPaths:
    """Cline (VS Code extension by saoudrizwan). The MCP settings
    path lives inside VS Code's `globalStorage`. We default to the
    standard VS Code path; users on Code-Insiders / Codium /
    Cursor-as-VS-Code can override via `--config-path` if their
    editor variant uses a different prefix."""
    home = Path.home()
    sys_name = platform.system()
    if sys_name == "Darwin":
        prefix = home / "Library" / "Application Support" / "Code"
    elif sys_name == "Windows":
        appdata = os.environ.get("APPDATA")
        prefix = (
            Path(appdata) / "Code" if appdata else home / "AppData" / "Roaming" / "Code"
        )
    else:
        prefix = home / ".config" / "Code"
    cline = (
        prefix
        / "User"
        / "globalStorage"
        / "saoudrizwan.claude-dev"
        / "settings"
        / "cline_mcp_settings.json"
    )
    return ClientPaths(
        name="cline",
        description="Cline (VS Code extension)",
        paths=(cline,),
    )


# Registry. Keys are the values accepted by `--client`. Adding a new
# client is one entry here plus a getter above.
KNOWN_CLIENTS: dict[str, Callable[[], ClientPaths]] = {
    "claude-code": _claude_code_paths,
    "claude-desktop": _claude_desktop_paths,
    "cursor": _cursor_paths,
    "continue": _continue_paths,
    "cline": _cline_paths,
}


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


def find_binary() -> str:
    """Resolve the absolute path to the `bettermemory` binary as a fresh
    shell would see it. The result is what we'd write into the MCP
    client's config — clients spawn the server in their own process,
    not ours, so a relative path or shell alias would fail at runtime.

    Resolution order:
    1. `shutil.which("bettermemory")` on the user's PATH.
    2. `sys.argv[0]` if it's already absolute and exists (covers
       `python -m bettermemory init` invocations from a venv).
    3. Bare `"bettermemory"` as a last-resort fallback — assumes the
       user can fix PATH themselves.
    """
    binary = shutil.which("bettermemory")
    if binary:
        return str(Path(binary).resolve())

    candidate = Path(sys.argv[0])
    # Require the argv0 basename to actually be the bettermemory binary —
    # mirrors the guard in doctor._check_binary_on_path. Under
    # `python -m bettermemory …`, argv[0] is the package's `__main__.py`,
    # whose path matches no MCP client's console-script entry; returning it
    # would make doctor flag every correct config as a "stale binary path".
    # Fall through to the bare-string fallback in that case instead.
    if (
        candidate.is_absolute()
        and candidate.exists()
        and "bettermemory" in candidate.name
    ):
        return str(candidate.resolve())

    return "bettermemory"


# ---------------------------------------------------------------------------
# Snippet & patch
# ---------------------------------------------------------------------------


DEFAULT_SERVER_NAME = "bettermemory"
"""Default key under `mcpServers`. Was `memory` in 1.0; renamed in 1.1
because `memory` is a generic word that collides with other MCP servers
(and with Claude Code's own evolving memory features). Patch_client_config
detects a legacy `memory` entry pointing at our binary and migrates it
forward — the rename is invisible to existing users."""

LEGACY_SERVER_NAME = "memory"
"""The 1.0 default. Migrated forward by patch_client_config when an
upgrade lands."""


def server_snippet(
    *,
    name: str = DEFAULT_SERVER_NAME,
    binary: str | None = None,
) -> dict[str, Any]:
    """Return the canonical `mcpServers` entry for bettermemory. Suitable
    for direct embedding in any MCP client's config file.

    The shape includes `type: "stdio"` and `env: {}` even though both are
    optional — they match what `claude mcp add` produces and what Claude
    Code 2.x writes by default, so the snippet is recognizable next to
    the user's other entries instead of looking deliberately minimal."""
    if binary is None:
        binary = find_binary()
    return {
        "mcpServers": {
            name: {
                "type": "stdio",
                "command": binary,
                "args": [],
                "env": {},
            }
        }
    }


def patch_client_config(
    target_path: Path,
    *,
    name: str = DEFAULT_SERVER_NAME,
    binary: str | None = None,
) -> dict[str, Any]:
    """Idempotently merge the bettermemory entry into the named MCP
    client config file. Creates parent dirs and the file if missing.
    Returns a result dict with `{action, path, name, binary?,
    migrated_from_legacy?}`. `action` is one of `"added"`, `"updated"`,
    or `"noop"`.

    Legacy migration: when writing under the new default name
    (`bettermemory`) and a stale `memory` entry whose `command` resolves
    to the same binary already exists, the legacy entry is removed and
    the result includes `migrated_from_legacy=True`. This keeps users
    upgrading from 1.0 from ending up with the server registered twice
    (which would surface every tool twice in the model's tool list).
    Migration only triggers on exact-binary match — a `memory` entry
    pointing at a different binary is left alone in case the user is
    intentionally hosting two memory servers.

    Raises ValueError when the existing file is not valid JSON or does
    not have an object at the root or at `mcpServers`. We deliberately
    refuse to touch a malformed config rather than overwrite it — fixing
    the file by hand is the right move."""
    if binary is None:
        binary = find_binary()

    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.exists():
        try:
            text = target_path.read_text(encoding="utf-8")
            existing = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"existing config at {target_path} is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, col {exc.colno}). Fix the file by hand "
                f"or remove it before re-running init."
            ) from exc
        if not isinstance(existing, dict):
            raise ValueError(
                f"existing config at {target_path} has a non-object root; "
                f"expected `{{...}}`."
            )
    else:
        existing = {}

    mcp_servers = existing.setdefault("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        raise ValueError(
            f"existing `mcpServers` field in {target_path} is not an object; "
            f"expected `{{...}}`."
        )

    # Legacy-name migration: only when writing under the new default
    # name. A user who explicitly passes `--name memory` (or some other
    # string) has opinions; don't second-guess. The match is on `command`
    # alone — a legacy entry pointing at a different binary stays put.
    legacy_present = (
        name == DEFAULT_SERVER_NAME
        and LEGACY_SERVER_NAME in mcp_servers
        and isinstance(mcp_servers[LEGACY_SERVER_NAME], dict)
        and mcp_servers[LEGACY_SERVER_NAME].get("command") == binary
    )

    # MERGE the canonical keys into any existing entry rather than replacing
    # it wholesale. A user may have added keys to their bettermemory entry —
    # notably `env` (BETTERMEMORY_DIR relocates the whole store), but also
    # `disabled`, `timeout`, or transport overrides. Re-running init after an
    # upgrade (exactly when the docstring says migration runs) must NOT silently
    # drop them; clobbering `env.BETTERMEMORY_DIR` makes the server boot against
    # the default dir and the user's store looks empty/gone from that client.
    # We own only type/command/args; everything else the user set is preserved,
    # and `env` defaults to {} only when absent.
    #
    # On the RENAME path the user's keys live under LEGACY_SERVER_NAME, not
    # `name` — so when there's no entry under the new name yet but a legacy
    # entry is being migrated, seed from the legacy entry so its env/disabled/
    # timeout/transport keys carry forward before the old entry is deleted.
    # Otherwise a user who relocated their store via env.BETTERMEMORY_DIR would
    # boot against the default dir post-migration and their store looks gone.
    existing_entry = mcp_servers.get(name)
    seed_entry = existing_entry
    if not isinstance(seed_entry, dict) and legacy_present:
        seed_entry = mcp_servers[LEGACY_SERVER_NAME]
    new_entry: dict[str, Any] = dict(seed_entry) if isinstance(seed_entry, dict) else {}
    new_entry["type"] = "stdio"
    new_entry["command"] = binary
    new_entry["args"] = []
    new_entry.setdefault("env", {})

    # Idempotency check: same name, same shape, no legacy to migrate →
    # no rewrite needed.
    if name in mcp_servers and mcp_servers[name] == new_entry and not legacy_present:
        return {
            "action": "noop",
            "path": str(target_path),
            "name": name,
        }

    action = "updated" if name in mcp_servers else "added"
    mcp_servers[name] = new_entry

    if legacy_present:
        del mcp_servers[LEGACY_SERVER_NAME]

    # Atomic + durable write via `_fsutil.atomic_write_bytes`: a plain
    # `target_path.write_text(...)` here would truncate the file before
    # writing the new content, so power loss / process kill mid-write
    # could leave the user with an empty `~/.claude.json` — every MCP
    # server they had registered (not just bettermemory) gone. The
    # helper writes to a tmp sibling, fsyncs, atomic-renames into place,
    # and fsyncs the parent directory.
    _fsutil.atomic_write_bytes(
        target_path,
        (json.dumps(existing, indent=2) + "\n").encode("utf-8"),
    )
    result: dict[str, Any] = {
        "action": action,
        "path": str(target_path),
        "name": name,
        "binary": binary,
    }
    if legacy_present:
        result["migrated_from_legacy"] = True
    return result


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _print_show_and_tell(
    *,
    binary: str,
    snippet: dict[str, Any],
    with_addendum: bool,
) -> None:
    print(f"bettermemory binary: {binary}")
    print()
    print("Add this to your MCP client's config:")
    print()
    print(json.dumps(snippet, indent=2))
    print()
    print("Common config locations (✓ = file exists):")
    for key, getter in KNOWN_CLIENTS.items():
        cp = getter()
        print(f"  {key} — {cp.description}")
        for p in cp.paths:
            mark = "✓" if p.exists() else " "
            print(f"    [{mark}] {p}")
    print()
    print("To auto-patch one of these, re-run with --client:")
    print("  bettermemory init --client claude-code")
    print("  bettermemory init --client claude-desktop")
    print("  bettermemory init --client cursor")
    print("  bettermemory init --client continue")
    print("  bettermemory init --client cline")
    print()
    print("Verify the install once you've restarted the client:")
    print('  ask the model "what memory tools do you have?"')
    if with_addendum:
        print()
        print("--- Optional advanced-tightening addendum ---")
        print("(server-level MCP instructions already carry the load-bearing")
        print(" policy; this addendum is for tighter scope hygiene and")
        print(" expanded record-use guidance — paste into your CLAUDE.md)")
        print()
        print(SYSTEM_PROMPT_ADDENDUM)


def _print_patch_summary(
    *,
    result: dict[str, Any],
    binary: str,
    with_addendum: bool,
) -> None:
    if result["action"] == "noop":
        print(f"already configured at {result['path']} (no change)")
    elif result["action"] == "updated":
        print(f"updated existing `{result['name']}` entry in {result['path']}")
    else:
        print(f"added `{result['name']}` to {result['path']}")
    if result.get("migrated_from_legacy"):
        # 1.0 → 1.1 rename of the default key. Tell the user we cleaned
        # up the old entry so they don't see two registrations.
        print(
            f"removed legacy `{LEGACY_SERVER_NAME}` entry pointing at the "
            f"same binary (1.0 → 1.1 default-name rename)."
        )
    print(f"binary: {binary}")
    print()
    print("Restart your MCP client to pick up the change, then ask the")
    print('model: "what memory tools do you have?"')
    if with_addendum:
        print()
        print("--- Optional advanced-tightening addendum ---")
        print("(server-level MCP instructions already carry the load-bearing")
        print(" policy; paste this into your CLAUDE.md only if you want the")
        print(" expanded discipline)")
        print()
        print(SYSTEM_PROMPT_ADDENDUM)


def cli_init(
    *,
    client: str | None,
    print_only: bool,
    json_out: bool,
    name: str | None,
    with_addendum: bool,
    config_path: Path | None,
) -> None:
    """Entry point invoked from `server.main()` argparse dispatch.

    `name=None` resolves to `DEFAULT_SERVER_NAME`. Keeping the default
    in the module-level constant rather than the argparse layer means
    the snippet/patch helpers and the CLI agree on what "default" means
    even when callers don't go through argparse."""
    if name is None:
        name = DEFAULT_SERVER_NAME
    binary = find_binary()
    snippet = server_snippet(name=name, binary=binary)

    if json_out:
        out: dict[str, Any] = {
            "binary": binary,
            "snippet": snippet,
            "clients": {
                key: {
                    "description": getter().description,
                    "paths": [str(p) for p in getter().paths],
                    "default_target": str(getter().paths[0]),
                }
                for key, getter in KNOWN_CLIENTS.items()
            },
        }
        if with_addendum:
            out["system_prompt_addendum"] = SYSTEM_PROMPT_ADDENDUM
        if client is not None and not print_only:
            target = config_path or KNOWN_CLIENTS[client]().paths[0]
            out["patch"] = patch_client_config(target, name=name, binary=binary)
        print(json.dumps(out, indent=2))
        return

    if client is None:
        _print_show_and_tell(
            binary=binary,
            snippet=snippet,
            with_addendum=with_addendum,
        )
        return

    if client not in KNOWN_CLIENTS:
        # argparse choices= should catch this, but stay defensive.
        raise ValueError(
            f"unknown client {client!r}; choose from {sorted(KNOWN_CLIENTS.keys())}"
        )

    target = config_path or KNOWN_CLIENTS[client]().paths[0]

    if print_only:
        print(json.dumps(snippet, indent=2))
        print(f"\n# Save the above to: {target}")
        return

    result = patch_client_config(target, name=name, binary=binary)
    _print_patch_summary(
        result=result,
        binary=binary,
        with_addendum=with_addendum,
    )
