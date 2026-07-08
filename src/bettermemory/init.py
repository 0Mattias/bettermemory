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


def command_launches_bettermemory(
    command: object,
    args: object,
    binary: str,
) -> bool:
    """Recognize whether an ``mcpServers`` entry's ``command``/``args`` pair
    launches *our* server. Init's legacy-migration gate and doctor's
    stale-path scan share this ONE definition so the two can't drift into
    disagreeing about what "a bettermemory entry" is.

    Any one of these shapes matches:

    * ``command`` equals the resolved absolute ``binary`` path we'd write.
    * a bare (non-absolute) console-script whose basename is
      ``bettermemory`` — the form ``docs/clients.md`` and
      ``docs/installation.md`` bless (``"command": "bettermemory"``).
    * an absolute ``command`` that exists on disk and resolves
      (symlink-aware) to the same target as ``binary`` — the
      ``~/.local/bin`` symlink vs. uv-tool canonical-path case.
    * the ``uvx``/``uv`` runner shape the plugin's ``.mcp.json`` ships:
      ``"command": "uvx", "args": ["bettermemory"]``.

    A byte-exact ``command == binary`` gate (the pre-fix logic) silently
    no-ops migration for every blessed shape but the first.
    """
    if not isinstance(command, str):
        return False
    if command == binary:
        return True
    cmd_path = Path(command)
    if not cmd_path.is_absolute() and cmd_path.name == DEFAULT_SERVER_NAME:
        return True
    if cmd_path.name in {"uvx", "uv"} and isinstance(args, list):
        if any(a == DEFAULT_SERVER_NAME for a in args if isinstance(a, str)):
            return True
    if cmd_path.is_absolute() and cmd_path.exists():
        try:
            if cmd_path.resolve() == Path(binary).resolve():
                return True
        except OSError:
            # `resolve()` can raise on a broken symlink; treat as no match.
            pass
    return False


def _config_signature(path: Path) -> tuple[int, int]:
    """Cheap change-detector for the target config: ``(mtime_ns, size)``.
    Snapshotted right after the read and re-checked right before the
    atomic write so a concurrent writer (the live client that owns
    ``~/.claude.json``) mutating the file under us aborts loudly instead
    of being silently clobbered."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


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
    (`bettermemory`) and a stale `memory` entry that launches our server
    already exists, the legacy entry's user-set keys are carried forward
    (env.BETTERMEMORY_DIR, cwd, timeout, transport headers), the legacy
    entry is removed, and the result includes `migrated_from_legacy=True`.
    This keeps users upgrading from 1.0 from ending up with the server
    registered twice (which would surface every tool twice in the model's
    tool list). Recognition uses `command_launches_bettermemory` (shared
    with doctor) so the blessed config shapes — bare `command:
    bettermemory`, the `uvx`+args plugin shape, a `~/.local/bin` symlink —
    all migrate; a `memory` entry pointing at a DIFFERENT binary is left
    alone in case the user is intentionally hosting two memory servers.

    Concurrency: the whole read-modify-write is held under
    `_fsutil.flock_excl` (so two bettermemory writers serialise) and the
    file is re-stat'd immediately before the atomic write — a change by a
    non-locking writer (the live Claude Code process that RMWs
    `~/.claude.json`) aborts with a ValueError rather than clobbering the
    client's update.

    Raises ValueError when the existing file is not valid JSON, does not
    have an object at the root or at `mcpServers`, or changed on disk
    mid-write. We deliberately refuse to touch a malformed or racing
    config rather than overwrite it — fixing the file by hand (or
    re-running) is the right move."""
    if binary is None:
        binary = find_binary()

    target_path.parent.mkdir(parents=True, exist_ok=True)

    # `~/.claude.json` (the Claude Code user-scope target) is owned by a
    # live Claude Code process that read-modify-writes it on many events.
    # An UNLOCKED RMW here races that writer: we read a snapshot, the
    # client rewrites the file, then our atomic rename lands and silently
    # drops the client's update. Hold the cross-process `flock_excl` for
    # the whole RMW so two bettermemory writers serialise, AND re-stat the
    # file immediately before the atomic write so a change by the
    # NON-locking client aborts loudly instead of clobbering. It's a
    # single lock, so the lock order is trivial and deadlock-free.
    with _fsutil.flock_excl(target_path):
        baseline_sig: tuple[int, int] | None = None
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
            baseline_sig = _config_signature(target_path)
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
        # string) has opinions; don't second-guess. Recognition uses the
        # shared `command_launches_bettermemory` helper (NOT byte-exact
        # `command == binary`) so the blessed shapes this project's own docs
        # ship — bare `command: bettermemory`, the `uvx`+args plugin shape, a
        # `~/.local/bin` symlink — are migrated instead of silently no-op'd.
        # A legacy entry pointing at a different binary stays put.
        legacy_raw = mcp_servers.get(LEGACY_SERVER_NAME)
        legacy_present = (
            name == DEFAULT_SERVER_NAME
            and isinstance(legacy_raw, dict)
            and command_launches_bettermemory(
                legacy_raw.get("command"), legacy_raw.get("args"), binary
            )
        )

        # Build the new-name entry as a UNION that preserves legacy-only keys
        # while letting an existing new-name entry win on conflicts. A user
        # may have added keys — notably `env` (BETTERMEMORY_DIR relocates the
        # whole store), but also `cwd`, `timeout`, transport `headers`, or
        # `disabled`. On the RENAME path those keys live ONLY under
        # LEGACY_SERVER_NAME; on a re-run after an upgrade under `name`; in
        # the both-exist case, under either. The pre-fix code seeded from the
        # legacy entry ONLY when no new-name entry existed yet deleted the
        # legacy entry UNCONDITIONALLY — so in the both-exist case legacy-only
        # keys were silently dropped and a relocated store looked gone from
        # that client. We own only type/command/args; everything else is kept.
        legacy_entry: dict[str, Any] = (
            legacy_raw if legacy_present and isinstance(legacy_raw, dict) else {}
        )
        existing_raw = mcp_servers.get(name)
        existing_entry: dict[str, Any] = (
            existing_raw if isinstance(existing_raw, dict) else {}
        )

        new_entry: dict[str, Any] = {**legacy_entry, **existing_entry}
        # Deep-merge `env` so a BETTERMEMORY_DIR set on EITHER side survives;
        # the new-name entry wins on a per-variable conflict.
        merged_env: dict[str, Any] = {}
        if isinstance(legacy_entry.get("env"), dict):
            merged_env.update(legacy_entry["env"])
        if isinstance(existing_entry.get("env"), dict):
            merged_env.update(existing_entry["env"])

        new_entry["type"] = "stdio"
        new_entry["command"] = binary
        new_entry["args"] = []
        new_entry["env"] = merged_env

        # Drop remote-transport-only keys so the forced stdio entry can't
        # become a hybrid (`url`/`headers` alongside `command` — a strict
        # client schema can reject the whole file, taking down every OTHER
        # MCP server in it). This is a DENYLIST, not an allowlist: every
        # legitimate stdio key the user set (`cwd`, `timeout`, `disabled`,
        # client-specific `autoApprove`/`alwaysAllow`, …) is preserved by the
        # union above; only the keys meaningless for a stdio launch are shed.
        for _remote_only_key in ("url", "headers"):
            new_entry.pop(_remote_only_key, None)

        # Born ENABLED: `disabled` survives only when the user set it on the
        # SURVIVING (new-name) entry. A stale `disabled: true` inherited from
        # the legacy entry on the rename path would leave the migrated server
        # disabled while _print_patch_summary reports unqualified success.
        if "disabled" in new_entry and "disabled" not in existing_entry:
            del new_entry["disabled"]

        # Idempotency check: same name, same shape, no legacy to migrate →
        # no rewrite needed.
        if (
            name in mcp_servers
            and mcp_servers[name] == new_entry
            and not legacy_present
        ):
            return {
                "action": "noop",
                "path": str(target_path),
                "name": name,
            }

        action = "updated" if name in mcp_servers else "added"
        mcp_servers[name] = new_entry

        if legacy_present:
            del mcp_servers[LEGACY_SERVER_NAME]

        # Concurrency guard: a non-locking writer (the live Claude Code
        # process that owns `~/.claude.json`) may have rewritten the file
        # after we snapshotted it. Re-stat immediately before the atomic
        # replace; if mtime/size moved, abort rather than clobber the
        # client's update. Raised as ValueError so the CLI renders a clean
        # "re-run" message (exit 2) instead of a traceback.
        if baseline_sig is not None and _config_signature(target_path) != baseline_sig:
            raise ValueError(
                f"config at {target_path} changed under us between read and "
                f"write (another process, likely the running client, wrote "
                f"it). Nothing was modified; re-run init."
            )

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
