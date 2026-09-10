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
load-bearing parts (see `prompts.py` + `builder.build_server`); the
addendum is now an optional tightening document, not part of the
required setup.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from . import _fsutil
from .identity import ENV_CLIENT
from .prompts import SYSTEM_PROMPT_ADDENDUM


# ---------------------------------------------------------------------------
# Per-client config locations
# ---------------------------------------------------------------------------


#: A JSON document with an object under `mcpServers` — the shape Claude
#: Code, Claude Desktop, Cursor, Cline and legacy Continue share, written
#: by `patch_client_config`.
FORMAT_MCP_SERVERS_JSON = "mcpServers-json"
#: Hermes Agent's `~/.hermes/config.yaml`: a YAML map under a top-level
#: `mcp_servers` key, one entry per server name, written by
#: `patch_hermes_config`.
FORMAT_HERMES_YAML = "hermes-yaml"
#: The top-level key Hermes reads its MCP servers from.
HERMES_SERVERS_KEY = "mcp_servers"


@dataclass(frozen=True)
class ClientPaths:
    """A known MCP client and the candidate config paths we know about
    for it. `paths[0]` is the default target when `--client` is set
    without `--config-path`; later entries are alternatives surfaced in
    show-and-tell mode so the user knows their options. `format` names
    the document shape at those paths, which decides the patcher init
    runs and the loader `doctor` reads entries through."""

    name: str
    description: str
    paths: tuple[Path, ...]
    format: str = FORMAT_MCP_SERVERS_JSON


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


def _hermes_paths() -> ClientPaths:
    """Hermes Agent (Nous Research). One long-lived gateway process reads
    `~/.hermes/config.yaml` at startup and registers every server in its
    `mcp_servers` map; an interactive session reloads the map when the
    file changes. Stdio servers take `command`, `args` and `env` — no
    cwd — so the entry declares the client and says nothing about a
    workspace: the gateway serves many projects from one directory, and
    the labeled process-cwd fallback is the honest record of that (a
    `BETTERMEMORY_WORKSPACE` in the `env` block overrides it for a
    single-project install; see docs/clients.md)."""
    return ClientPaths(
        name="hermes",
        description="Hermes Agent",
        paths=(Path.home() / ".hermes" / "config.yaml",),
        format=FORMAT_HERMES_YAML,
    )


# Registry. Keys are the values accepted by `--client`. Adding a new
# client is one entry here plus a getter above (and its `format`, when
# the file is not a JSON `mcpServers` object). `cli/init.py` lists the
# same keys as argparse choices; a test pins the two together.
KNOWN_CLIENTS: dict[str, Callable[[], ClientPaths]] = {
    "claude-code": _claude_code_paths,
    "claude-desktop": _claude_desktop_paths,
    "cursor": _cursor_paths,
    "continue": _continue_paths,
    "cline": _cline_paths,
    "hermes": _hermes_paths,
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


CONTINUE_LEGACY_WARNING = (
    "warning: the `continue` client target writes an OBJECT-shaped "
    "`mcpServers` into `~/.continue/config.json`, but current Continue "
    "reads MCP servers as a YAML LIST in `~/.continue/config.yaml` (or "
    "individual files under `~/.continue/mcpServers/`) — the config.json "
    "object shape is a DEPRECATED format current Continue ignores. The "
    "entry below is written for backward compatibility with legacy "
    "Continue only; on a current install add this instead:\n"
    "  mcpServers:\n"
    "    - name: bettermemory\n"
    "      command: <the binary path printed below>\n"
    "      args: []\n"
    "See docs/clients.md (Continue section) for details."
)
"""Emitted to stderr when `--client continue` is targeted. Continue's
current released schema (verified 2026-07 against docs.continue.dev/
customize/deep-dives/mcp) takes `mcpServers` as a LIST in `config.yaml`;
`config.json` is documented as deprecated. Rather than silently write a
shape current Continue drops on the floor, init warns and points at the
correct YAML list form — see docs/clients.md."""


def server_snippet(
    *,
    name: str = DEFAULT_SERVER_NAME,
    binary: str | None = None,
    client: str | None = None,
) -> dict[str, Any]:
    """Return the canonical `mcpServers` entry for bettermemory. Suitable
    for direct embedding in any MCP client's config file.

    The shape includes `type: "stdio"` and `env: {}` even though both are
    optional — they match what `claude mcp add` produces and what Claude
    Code 2.x writes by default, so the snippet is recognizable next to
    the user's other entries instead of looking deliberately minimal.

    `client` names the client the entry is for; when given, the `env`
    block declares it as `BETTERMEMORY_CLIENT` so every write the server
    records carries the client's name even where the handshake's
    `clientInfo` is absent — a stdio server's only out-of-band channel
    (`identity.SOURCE_ENV`)."""
    if binary is None:
        binary = find_binary()
    env: dict[str, Any] = {ENV_CLIENT: client} if client else {}
    return {
        "mcpServers": {
            name: {
                "type": "stdio",
                "command": binary,
                "args": [],
                "env": env,
            }
        }
    }


def hermes_snippet(
    *,
    name: str = DEFAULT_SERVER_NAME,
    binary: str | None = None,
    client: str | None = None,
) -> dict[str, Any]:
    """The `mcp_servers` entry Hermes Agent reads from `~/.hermes/config.yaml`:
    the stdio keys Hermes documents (`command`, `args`, `env`) and no
    `type`, which Hermes does not define. `client` lands in `env` as
    `BETTERMEMORY_CLIENT`, the only out-of-band channel a stdio server
    has (`identity.SOURCE_ENV`); `None` leaves `env` empty."""
    if binary is None:
        binary = find_binary()
    env: dict[str, Any] = {ENV_CLIENT: client} if client else {}
    return {HERMES_SERVERS_KEY: {name: {"command": binary, "args": [], "env": env}}}


def hermes_snippet_text(snippet: Mapping[str, Any]) -> str:
    """`hermes_snippet` rendered as the YAML block a user pastes into
    `~/.hermes/config.yaml`."""
    return _dump_yaml_block(snippet)


def _dump_yaml_block(data: Mapping[str, Any], *, indent: int = 0, step: int = 2) -> str:
    """Render `data` as block-style YAML, every non-blank line shifted right
    by `indent` columns and nested mappings stepping in by `step`. The
    pure-Python SafeDumper the store also uses (store.py says why),
    insertion order kept, no line wrapping so a long binary path stays on
    one line; an empty list or map still renders inline (`[]`, `{}`),
    which is the shape Hermes's own examples use."""
    if not 2 <= step <= 9:
        step = 2  # outside this range PyYAML falls back to 2, silently
    text: str = yaml.dump(
        dict(data),
        Dumper=yaml.SafeDumper,
        sort_keys=False,
        default_flow_style=False,
        indent=step,
        allow_unicode=True,
        width=100_000,
    )
    if indent == 0:
        return text
    pad = " " * indent
    return "".join(
        pad + line if line.strip() else line for line in text.splitlines(keepends=True)
    )


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
    # `.stem.lower()` (not `.name`) so the Windows `uvx.exe` / `Uv.exe`
    # spellings of the same runner are recognized too.
    if cmd_path.stem.lower() in {"uvx", "uv"} and isinstance(args, list):
        if _uv_args_run_bettermemory(args):
            return True
    if cmd_path.is_absolute() and cmd_path.exists():
        try:
            if cmd_path.resolve() == Path(binary).resolve():
                return True
        except OSError:
            # `resolve()` can raise on a broken symlink; treat as no match.
            pass
    return False


# uv/uvx flags that consume the NEXT token as their value. Only the ones that
# matter for telling "the package uvx runs" apart from its neighbors: a
# dependency injected via `--with` / `--from` must not make an unrelated
# server entry look like ours. An unknown value-taking flag degrades to
# treating its value as the positional — strictly narrower than the any-arg
# scan this replaces.
_UV_VALUE_FLAGS = {
    "--from",
    "--with",
    "--with-requirements",
    "--python",
    "-p",
    "--index",
    "--index-url",
    "--extra-index-url",
    "--default-index",
    "--constraint",
    "-c",
    "--exclude-newer",
    "--directory",
    "--project",
    "--config-file",
}

# uv subcommand words that still mean "run a tool" — `uv tool run X`,
# `uv run X`, `uv x X` — skipped before the positional walk.
_UV_RUN_SUBCOMMANDS = {"tool", "run", "x"}

# Version-pin separators uvx/PEP 508 accept directly after a distribution
# name: `bettermemory@latest`, `bettermemory==3.15.0`, `bettermemory>=3`, …
_UV_PIN_SEPARATORS = ("@", "==", ">=", "<=", "~=", "!=", ">", "<")


def _names_bettermemory_package(token: str) -> bool:
    """True when `token` names the bettermemory distribution — bare or
    version-pinned. The separator must follow the name IMMEDIATELY so a
    different distribution that merely starts with the string
    (`bettermemory-evil@1.0`) cannot match."""
    if token == DEFAULT_SERVER_NAME:
        return True
    return any(
        token.startswith(DEFAULT_SERVER_NAME + sep) for sep in _UV_PIN_SEPARATORS
    )


def _uv_args_run_bettermemory(args: list[Any]) -> bool:
    """True when a uv/uvx arg vector RUNS bettermemory, as opposed to merely
    depending on it.

    The any-arg scan this replaces matched `bettermemory` in ANY position, so
    `uv run --with bettermemory other-mcp-server` — bettermemory as a
    dependency of a FOREIGN server — was recognized as ours (and init would
    delete and rewrite that entry: the too-broad direction), while the
    version-pinned shapes uvx documents (`bettermemory@latest`,
    `bettermemory==3.15.0`) matched nothing and doctor reported a healthy
    install missing (the too-narrow direction). Walk the vector instead:
    skip run-ish subcommand words, skip flags (consuming the value token for
    flags known to take one), and test the FIRST real positional — the
    package/command uv actually runs."""
    tokens = [a for a in args if isinstance(a, str)]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _UV_RUN_SUBCOMMANDS:
            i += 1
            continue
        if tok.startswith("-"):
            if "=" in tok:
                i += 1  # `--from=pkg` is self-contained
            elif tok in _UV_VALUE_FLAGS:
                i += 2  # flag + its value token
            else:
                i += 1  # bare flag (`-q`, `--no-cache`, …)
            continue
        return _names_bettermemory_package(tok)
    return False


def _config_signature(path: Path) -> tuple[int, int]:
    """Cheap change-detector for the target config: ``(mtime_ns, size)``.
    Snapshotted right BEFORE the read (so the baseline describes the exact
    bytes we are about to read and overwrite) and re-checked right before
    the atomic write so a concurrent writer (the live client that owns
    ``~/.claude.json``) mutating the file under us aborts loudly instead
    of being silently clobbered. Capturing it after the read would leave an
    unguarded read->stat window: a write landing there folds into the
    baseline and the pre-write re-stat then matches, clobbering it."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def _heal_stale_sidecar_lockfile(target_path: Path) -> Path | None:
    """Remove the 0-byte ``<target>.lock`` REGULAR FILE bettermemory 3.15.0
    left next to client configs.

    3.15.0's RMW lock used the shared flock sidecar convention — a persistent
    0-byte regular file at ``<target>.lock``. That name collides with the
    mkdir-style DIRECTORY lock some clients take on their own config: Claude
    Code (proper-lockfile) acquires ``~/.claude.json.lock`` via mkdir and
    clears stale locks via rmdir, so a regular file squatting there reads as
    "lock held" (mkdir → EEXIST) and the stale-cleanup dies (rmdir → ENOTDIR)
    forever — the client cannot persist config until the file is hand-deleted.
    bettermemory now locks a private ``<target>.bettermemory.lock`` sidecar
    and never touches ``<target>.lock``; this heal removes only the exact
    artifact 3.15.0 created — a REGULAR (non-directory, non-symlink) EMPTY
    file — and leaves anything else (a client's live lock directory, a
    non-empty file some other tool owns) alone. Removing it under a
    concurrently-held 3.15.0 flock would degrade that older process's
    serialization for one run — bounded to the mixed-version upgrade window
    and strictly better than leaving the client's config lock wedged.
    """
    legacy_lock = target_path.with_suffix(target_path.suffix + ".lock")
    try:
        if (
            legacy_lock.is_file()
            and not legacy_lock.is_symlink()
            and legacy_lock.stat().st_size == 0
        ):
            legacy_lock.unlink()
            return legacy_lock
    except OSError:
        # Healing is best-effort; a permission error must not fail init.
        pass
    return None


def patch_client_config(
    target_path: Path,
    *,
    name: str = DEFAULT_SERVER_NAME,
    binary: str | None = None,
    client: str | None = None,
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
    `_fsutil.flock_excl` on a PRIVATE `<target>.bettermemory.lock` sidecar
    (so two bettermemory writers serialise without squatting on
    `<target>.lock`, a name the owning client's own locking protocol may
    use — Claude Code takes a mkdir-style directory lock there), and the
    file is re-checked immediately before the atomic write: a change to a
    pre-existing file (mtime/size signature moved) or a file CREATED under
    us by a non-locking writer aborts with a ValueError rather than
    clobbering the client's update. The guard covers the read→pre-write
    window; the few milliseconds between that re-check and the atomic
    rename remain unguarded against a non-cooperating writer — with no
    shared lock protocol between the processes a residual window is
    irreducible, which is why the re-check sits as late as possible.
    A leftover 3.15.0 `<target>.lock` regular file (which wedges Claude
    Code's own config lock) is healed on entry; the result carries
    `removed_stale_lockfile` when that happened.

    Raises ValueError when the existing file is not valid JSON, does not
    have an object at the root or at `mcpServers`, or changed on disk
    mid-write. We deliberately refuse to touch a malformed or racing
    config rather than overwrite it — fixing the file by hand (or
    re-running) is the right move."""
    if binary is None:
        binary = find_binary()

    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Heal the 3.15.0 artifact BEFORE locking: a poisoned `<target>.lock`
    # regular file wedges the owning client's own mkdir-style lock until
    # removed (see `_heal_stale_sidecar_lockfile`). Our lock below lives at
    # a different, bettermemory-private name, so ordering is free.
    healed_lock = _heal_stale_sidecar_lockfile(target_path)

    # `~/.claude.json` (the Claude Code user-scope target) is owned by a
    # live Claude Code process that read-modify-writes it on many events.
    # An UNLOCKED RMW here races that writer: we read a snapshot, the
    # client rewrites the file, then our atomic rename lands and silently
    # drops the client's update. Hold the cross-process `flock_excl` for
    # the whole RMW so two bettermemory writers serialise, AND re-check the
    # file immediately before the atomic write so a change by the
    # NON-locking client aborts loudly instead of clobbering. It's a
    # single lock, so the lock order is trivial and deadlock-free.
    #
    # The sidecar is the PRIVATE `<target>.bettermemory.lock`, never
    # `<target>.lock`: that default name collides with Claude Code's own
    # proper-lockfile directory lock on `~/.claude.json.lock` — our
    # persistent regular file there broke the client's mkdir/rmdir cycle
    # (EEXIST, then ENOTDIR forever), and the client's live lock DIRECTORY
    # made our `os.open` die with EISDIR. Distinct names remove the
    # collision in both directions; interference with the client's
    # unlocked writes stays covered by the signature guard below.
    with _fsutil.flock_excl(target_path, lock_suffix=".bettermemory.lock"):
        baseline_sig: tuple[int, int] | None = None
        if target_path.exists():
            # Snapshot the on-disk signature BEFORE the read, not after. The
            # baseline must describe the exact bytes we are about to read and
            # overwrite. Capturing it after read_text() leaves an unguarded
            # read->stat window: a non-locking client write landing between the
            # read and the stat folds into the baseline, so the pre-write
            # re-stat matches and we silently clobber the client's update.
            # Snapshotting first means any write after this point moves the
            # signature, so the pre-write re-stat mismatches and aborts loudly.
            baseline_sig = _config_signature(target_path)
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
        # Declare the client to the server (`identity.SOURCE_ENV`); a value
        # the user already set wins, declared is declared.
        if client is not None:
            merged_env.setdefault(ENV_CLIENT, client)

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
        # after we snapshotted it. Re-check immediately before the atomic
        # replace; abort rather than clobber the client's update. Raised as
        # ValueError so the CLI renders a clean "re-run" message (exit 2)
        # instead of a traceback. Two shapes:
        #
        # 1. The file did not exist at read time but does now — a writer
        #    CREATED it under us. Without this arm the skeleton doc below
        #    would silently replace the client's brand-new config: the same
        #    clobber class the signature guard closes, on the create path.
        # 2. The file existed and its mtime/size signature moved.
        # Both arms live in `_refuse_if_moved`, shared with the YAML path.
        _refuse_if_moved(target_path, baseline_sig)

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
        if healed_lock is not None:
            result["removed_stale_lockfile"] = str(healed_lock)
        return result


def _refuse_if_moved(target_path: Path, baseline_sig: tuple[int, int] | None) -> None:
    """The pre-write half of the concurrency guard both patchers share.

    `baseline_sig` is the `_config_signature` snapshotted before the read
    (`None` when the file did not exist then). Two shapes abort, each as
    a ValueError the CLI renders as a clean "re-run" message: the file
    was CREATED under us by a non-locking writer, so the skeleton we
    built would replace the client's brand-new config; or it existed and
    its mtime/size signature moved, so the bytes we read are not the
    bytes on disk. The re-check sits as late as possible before the
    atomic replace; the few milliseconds after it stay unguarded, which
    no shared lock protocol between the processes can close."""
    if baseline_sig is None and target_path.exists():
        raise ValueError(
            f"config at {target_path} was created under us between read "
            f"and write (another process, likely the running client, "
            f"wrote it). Nothing was modified; re-run init."
        )
    if baseline_sig is not None and _config_signature(target_path) != baseline_sig:
        raise ValueError(
            f"config at {target_path} changed under us between read and "
            f"write (another process, likely the running client, wrote "
            f"it). Nothing was modified; re-run init."
        )


# ---------------------------------------------------------------------------
# Hermes Agent: the YAML `mcp_servers` map
# ---------------------------------------------------------------------------


#: The keys that make a Hermes entry an HTTP server (`url`, `headers`,
#: `auth`, `identity_header`); shed from the stdio entry init writes so it
#: cannot become a hybrid Hermes refuses or misreads — the JSON path's
#: `url`/`headers` rule, extended to Hermes's own HTTP keys.
_HERMES_HTTP_ONLY_KEYS = ("url", "headers", "auth", "identity_header")

_YAML_NULL_TAG = "tag:yaml.org,2002:null"


def _mapping_pair(
    node: yaml.MappingNode, key: str
) -> tuple[yaml.Node, yaml.Node] | None:
    """The `(key, value)` node pair under `key` in a composed mapping — the
    LAST one when the key repeats, which is the one SafeLoader keeps, so
    the splice and the loaded document agree on which entry is live."""
    found: tuple[yaml.Node, yaml.Node] | None = None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            found = (key_node, value_node)
    return found


def _splice_hermes_entry(
    text: str,
    servers_pair: tuple[yaml.Node, yaml.Node] | None,
    *,
    name: str,
    entry: dict[str, Any],
    servers: dict[str, Any],
) -> str:
    """Return `text` with the `name` entry of `mcp_servers` set to `entry`,
    touching nothing else.

    Four shapes, decided by what `yaml.compose` found:

    * no `mcp_servers` key — a block carrying only our entry is appended
      after the last line of the file;
    * `mcp_servers:` with a null value, or a flow mapping (`{}`,
      `{a: {...}}`) — the key's span is rewritten as a block mapping
      holding every entry it had plus ours, a trailing comment on that
      line kept on the key line;
    * a block mapping without our entry — ours is inserted as its first
      child, at the children's own indentation;
    * a block mapping with our entry — that entry's lines are replaced,
      and the blank and comment lines after it (which belong to the next
      sibling, or to the owner) are left where they are.

    `servers` is the loaded `mcp_servers` map (empty when absent or
    null); it feeds only the rewrite of a null or flow block, where the
    other entries have to be rendered again."""
    lines = text.splitlines(keepends=True)

    if servers_pair is None:
        block = _dump_yaml_block({HERMES_SERVERS_KEY: {name: entry}})
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix.strip():
            prefix += "\n"
        return prefix + block

    key_node, value_node = servers_pair
    key_col = key_node.start_mark.column

    block_map = value_node if isinstance(value_node, yaml.MappingNode) else None
    if block_map is None or block_map.flow_style:
        merged = dict(servers)
        merged[name] = entry
        block = _dump_yaml_block({HERMES_SERVERS_KEY: merged}, indent=key_col)
        start = key_node.start_mark.index
        value_end = value_node.end_mark.index
        newline = text.find("\n", value_end)
        line_end = len(text) if newline == -1 else newline + 1
        rest = text[value_end:line_end].strip()
        first, sep, remainder = block.partition("\n")
        first = first.lstrip(" ")
        if rest:
            first = f"{first} {rest}"
        return text[:start] + first + sep + remainder + text[line_end:]

    child_col = block_map.value[0][0].start_mark.column
    step = child_col - key_col if child_col > key_col else 2

    entry_pair = _mapping_pair(block_map, name)
    if entry_pair is None:
        block = _dump_yaml_block({name: entry}, indent=child_col, step=step)
        insert_at = key_node.start_mark.line + 1
        return "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])

    entry_key, entry_value = entry_pair
    entry_col = entry_key.start_mark.column
    inner_step = step
    if (
        isinstance(entry_value, yaml.MappingNode)
        and not entry_value.flow_style
        and entry_value.value
    ):
        inner_col = entry_value.value[0][0].start_mark.column
        if inner_col > entry_col:
            inner_step = inner_col - entry_col
    start_line = entry_key.start_mark.line
    if entry_value.end_mark.index >= len(text):
        end_line = len(lines)
    elif isinstance(entry_value, yaml.CollectionNode) and not entry_value.flow_style:
        # A block collection's end mark is the next token, at the start
        # of a later line: exclusive.
        end_line = entry_value.end_mark.line
    else:
        # A scalar or flow value ends mid-line: take that line whole.
        end_line = entry_value.end_mark.line + 1
    while end_line - 1 > start_line:
        tail = lines[end_line - 1].strip()
        if tail == "" or tail.startswith("#"):
            end_line -= 1
        else:
            break
    block = _dump_yaml_block({name: entry}, indent=entry_col, step=inner_step)
    return "".join(lines[:start_line]) + block + "".join(lines[end_line:])


def _splice_holds(
    reparsed: Any, before: dict[str, Any], *, name: str, entry: dict[str, Any]
) -> bool:
    """True when the patched document carries `entry` under `name` and
    nothing else moved: every other top-level key and every other server
    compares equal to the document read before the splice."""
    if not isinstance(reparsed, dict):
        return False
    after_servers = reparsed.get(HERMES_SERVERS_KEY)
    if not isinstance(after_servers, dict) or after_servers.get(name) != entry:
        return False
    before_servers = before.get(HERMES_SERVERS_KEY)
    if not isinstance(before_servers, dict):
        before_servers = {}
    if {k: v for k, v in before_servers.items() if k != name} != {
        k: v for k, v in after_servers.items() if k != name
    }:
        return False
    return {k: v for k, v in before.items() if k != HERMES_SERVERS_KEY} == {
        k: v for k, v in reparsed.items() if k != HERMES_SERVERS_KEY
    }


def patch_hermes_config(
    target_path: Path,
    *,
    name: str = DEFAULT_SERVER_NAME,
    binary: str | None = None,
    client: str | None = None,
) -> dict[str, Any]:
    """Idempotently merge the bettermemory entry into Hermes Agent's
    `config.yaml` — the YAML twin of `patch_client_config`, under the
    same discipline: the private `<target>.bettermemory.lock` flock, the
    pre-read signature re-checked before the write, the atomic replace,
    and the same result dict (`action` is `added`, `updated` or `noop`).

    Merge semantics match the JSON path. init owns `command`, `args` and
    `env`; every other key on an existing entry (`idle_timeout_seconds`,
    `enabled`, a `tools` filter, …) survives; `env` is deep-merged and
    `BETTERMEMORY_CLIENT` is set only when absent, so a value the user
    declared wins. The HTTP-only keys are shed so a stdio entry cannot
    turn into a hybrid.

    The edit is a SPLICE, not a round-trip. PyYAML drops comments, and a
    Hermes config is a commented, hand-ordered document the owner
    maintains; rewriting it whole would erase that. `yaml.compose` gives
    the position of the `mcp_servers` key and of our entry, and only that
    region changes (`_splice_hermes_entry` lists the shapes). Before
    anything is written the spliced text is parsed again and must load to
    exactly the intended entry with every other key and server unchanged;
    a splice that fails that check is refused, never written.

    Raises ValueError when the file is not valid YAML, its root is not a
    block mapping, `mcp_servers` is present but not a mapping, the splice
    fails its own re-parse, or the file changed on disk mid-write — the
    same "fix by hand or re-run" conditions the JSON path refuses on."""
    if binary is None:
        binary = find_binary()

    target_path.parent.mkdir(parents=True, exist_ok=True)
    healed_lock = _heal_stale_sidecar_lockfile(target_path)

    with _fsutil.flock_excl(target_path, lock_suffix=".bettermemory.lock"):
        baseline_sig: tuple[int, int] | None = None
        text = ""
        if target_path.exists():
            baseline_sig = _config_signature(target_path)
            text = target_path.read_text(encoding="utf-8")
        try:
            root = yaml.compose(text, Loader=yaml.SafeLoader)
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"existing config at {target_path} is not valid YAML: {exc}. "
                f"Fix the file by hand or remove it before re-running init."
            ) from exc
        root_map: yaml.MappingNode | None = None
        if isinstance(root, yaml.MappingNode) and not root.flow_style:
            root_map = root
        elif root is not None:
            raise ValueError(
                f"existing config at {target_path} does not have a block "
                f"mapping at its root; expected top-level `key: value` lines."
            )
        document: dict[str, Any] = loaded if isinstance(loaded, dict) else {}

        servers_pair = (
            _mapping_pair(root_map, HERMES_SERVERS_KEY)
            if root_map is not None
            else None
        )
        if servers_pair is not None:
            value_node = servers_pair[1]
            is_null = (
                isinstance(value_node, yaml.ScalarNode)
                and value_node.tag == _YAML_NULL_TAG
            )
            if not is_null and not isinstance(value_node, yaml.MappingNode):
                raise ValueError(
                    f"existing `{HERMES_SERVERS_KEY}` field in {target_path} is "
                    f"not a mapping; expected one entry per server name."
                )
        loaded_servers = document.get(HERMES_SERVERS_KEY)
        servers: dict[str, Any] = (
            loaded_servers if isinstance(loaded_servers, dict) else {}
        )
        existing_raw = servers.get(name)
        existing_entry: dict[str, Any] = (
            existing_raw if isinstance(existing_raw, dict) else {}
        )

        # The same union the JSON path builds: the user's keys survive, `env`
        # deep-merges, the declared client is set only when absent, and the
        # keys that would make the entry an HTTP server are shed.
        new_entry: dict[str, Any] = dict(existing_entry)
        merged_env: dict[str, Any] = {}
        if isinstance(existing_entry.get("env"), dict):
            merged_env.update(existing_entry["env"])
        if client is not None:
            merged_env.setdefault(ENV_CLIENT, client)
        new_entry["command"] = binary
        new_entry["args"] = []
        new_entry["env"] = merged_env
        for http_only_key in _HERMES_HTTP_ONLY_KEYS:
            new_entry.pop(http_only_key, None)

        if name in servers and servers[name] == new_entry:
            return {"action": "noop", "path": str(target_path), "name": name}
        action = "updated" if name in servers else "added"

        new_text = _splice_hermes_entry(
            text, servers_pair, name=name, entry=new_entry, servers=servers
        )
        # The splice is checked, not trusted: the patched document has to
        # load back to exactly the intended entry with every other key and
        # every other server unchanged, or nothing is written.
        try:
            reparsed = yaml.safe_load(new_text)
        except yaml.YAMLError:
            reparsed = None
        if not _splice_holds(reparsed, document, name=name, entry=new_entry):
            raise ValueError(
                f"refusing to write {target_path}: the patched document did "
                f"not parse back to the intended `{HERMES_SERVERS_KEY}.{name}` "
                f"entry with everything else unchanged. Nothing was modified; "
                f"add the entry by hand (`bettermemory init --client hermes "
                f"--print-only` prints it)."
            )

        _refuse_if_moved(target_path, baseline_sig)
        _fsutil.atomic_write_bytes(target_path, new_text.encode("utf-8"))
        result: dict[str, Any] = {
            "action": action,
            "path": str(target_path),
            "name": name,
            "binary": binary,
        }
        if healed_lock is not None:
            result["removed_stale_lockfile"] = str(healed_lock)
        return result


def read_server_entries(
    path: Path, *, config_format: str = FORMAT_MCP_SERVERS_JSON
) -> dict[str, Any]:
    """The server map of one client config, read by document shape: the
    object under `mcpServers` for the JSON clients, the mapping under
    `mcp_servers` for Hermes. An empty file, a non-object root or an
    absent key read as an empty map — nothing registered — while a file
    that does not parse raises ValueError, so `doctor` can tell "nothing
    here" from "cannot read", the distinction its `unreadable` finding
    exists for. OSError and UnicodeDecodeError propagate untouched."""
    text = path.read_text(encoding="utf-8")
    data: Any
    if config_format == FORMAT_HERMES_YAML:
        try:
            data = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as exc:
            raise ValueError(f"not valid YAML: {exc}") from exc
        key = HERMES_SERVERS_KEY
    else:
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"
            ) from exc
        key = "mcpServers"
    if not isinstance(data, dict):
        return {}
    servers = data.get(key)
    return servers if isinstance(servers, dict) else {}


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
    for key in KNOWN_CLIENTS:
        print(f"  bettermemory init --client {key}")
    print()
    print("Hermes Agent reads the same entry as YAML under `mcp_servers`;")
    print("  `bettermemory init --client hermes --print-only` prints that shape.")
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


def _client_view(client_paths: ClientPaths) -> dict[str, Any]:
    """One client's row in the `--json` view."""
    return {
        "description": client_paths.description,
        "paths": [str(p) for p in client_paths.paths],
        "default_target": str(client_paths.paths[0]),
        "format": client_paths.format,
    }


def _patch_for(
    client_paths: ClientPaths,
    target: Path,
    *,
    name: str,
    binary: str,
    client: str | None,
) -> dict[str, Any]:
    """Run the patcher the client's `format` calls for."""
    if client_paths.format == FORMAT_HERMES_YAML:
        return patch_hermes_config(target, name=name, binary=binary, client=client)
    return patch_client_config(target, name=name, binary=binary, client=client)


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
    even when callers don't go through argparse.

    The client's `format` picks the writer and the printed shape: the
    JSON `mcpServers` snippet for every JSON client, the YAML
    `mcp_servers` block for Hermes."""
    if name is None:
        name = DEFAULT_SERVER_NAME
    if client is not None and client not in KNOWN_CLIENTS:
        # argparse choices= should catch this, but stay defensive.
        raise ValueError(
            f"unknown client {client!r}; choose from {sorted(KNOWN_CLIENTS.keys())}"
        )
    binary = find_binary()
    snippet = server_snippet(name=name, binary=binary, client=client)
    client_paths = KNOWN_CLIENTS[client]() if client is not None else None
    yaml_text: str | None = None
    if client_paths is not None and client_paths.format == FORMAT_HERMES_YAML:
        yaml_text = hermes_snippet_text(
            hermes_snippet(name=name, binary=binary, client=client)
        )

    # Continue's current released schema takes `mcpServers` as a LIST in
    # `config.yaml`; the object-in-`config.json` shape this client target
    # writes is a deprecated format current Continue ignores. Warn loudly
    # (to stderr, so `--json` stdout stays clean) instead of silently
    # writing a shape that does nothing — see CONTINUE_LEGACY_WARNING and
    # docs/clients.md.
    if client == "continue":
        sys.stderr.write(f"bettermemory init: {CONTINUE_LEGACY_WARNING}\n")

    if json_out:
        out: dict[str, Any] = {
            "binary": binary,
            "snippet": snippet,
            "clients": {
                key: _client_view(getter()) for key, getter in KNOWN_CLIENTS.items()
            },
        }
        if yaml_text is not None:
            out["snippet_yaml"] = yaml_text
        if with_addendum:
            out["system_prompt_addendum"] = SYSTEM_PROMPT_ADDENDUM
        if client_paths is not None and not print_only:
            target = config_path or client_paths.paths[0]
            out["patch"] = _patch_for(
                client_paths, target, name=name, binary=binary, client=client
            )
        print(json.dumps(out, indent=2))
        return

    if client_paths is None:
        _print_show_and_tell(
            binary=binary,
            snippet=snippet,
            with_addendum=with_addendum,
        )
        return

    target = config_path or client_paths.paths[0]

    if print_only:
        if yaml_text is not None:
            print(yaml_text, end="")
        else:
            print(json.dumps(snippet, indent=2))
        print(f"\n# Save the above to: {target}")
        return

    result = _patch_for(client_paths, target, name=name, binary=binary, client=client)
    _print_patch_summary(
        result=result,
        binary=binary,
        with_addendum=with_addendum,
    )
