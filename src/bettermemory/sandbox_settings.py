"""The Claude Code sandbox settings for this host's real paths.

SECURITY.md's standing guard ("An agent that changes the user's rules")
holds against an agent that works through the tools. An agent with a shell
runs as the user, so it can open the store file, read the key and take the
daemon's bearer token from the state file, and with the token post a
prompt of its own as the user's words. The line between the two is the
harness's to draw: Claude Code's sandbox confines the commands the agent
runs, while the hooks and the MCP server run outside it. docs/installation.md
section 5 gave the settings as a hand-written block with the Linux paths
(``~/.config/bettermemory``, ``~/.local/state/bettermemory``). On macOS the
config, keys and state directories are all under ``~/Library/Application
Support/bettermemory``, so the block pasted there denied two directories
that do not exist and left the key and the token readable. It also named
neither the ``BETTERMEMORY_*`` overrides nor a ``[storage] directory``.

This module builds the block from what the running config and environment
resolve: the store directory by the config's own rule
(`Config.resolved_directory`), the home default ``~/.claude-memory`` beside
it, the keys directory (`store_schema.resolve_keys_dir`), the config directory
(`config.default_config_path`) and the state directory
(`_daemon_client.resolve_state_dir`). A directory inside another one in the
list is dropped, since denying the outer one covers it: on Linux the keys
directory sits under the config directory, and on macOS the config and the
state directory are one directory. With no override set, the Linux output is
the documented block, entry for entry.

Two spellings, because Claude Code reads two path grammars. The
``sandbox.filesystem`` lists take ``~/x`` for the home directory and ``/x``
for an absolute path. The ``Read(...)`` and ``Edit(...)`` permission rules
take ``~/x`` for the home directory too, but a single leading slash there is
relative to the settings file, and an absolute path is written ``//x``
(``//c/x`` for a Windows drive). A store named by ``BETTERMEMORY_DIR=/srv/bm``
is ``/srv/bm`` in ``denyRead`` and ``Read(//srv/bm/**)`` in the rules.

The rules also deny ``.claude-memory/**`` relative to the working directory,
which as a deny rule matches the directory at any depth under it: a
project-local store, the one the config's third rule picks, is then closed
to Claude Code's own Read and Edit tools in every project. The sandbox's own
``denyRead`` has no spelling for a path relative to the session's working
directory (a relative entry there is relative to the settings file), so a
project-local store stays readable to the sandboxed shell, and writable,
since the sandbox lets commands write in the working directory; the notes
say so.

The daemon's port is not a path and has no key of its own in the block. The
network side of the promise is a list of things to leave out
(docs/installation.md: no ``localhost``, ``*.localhost`` or ``127.0.0.1``
entry without a port in ``sandbox.network.allowedDomains``, no
``allowLocalBinding``, no ``excludedCommands``), so the port goes into the
notes and the JSON report, where the user can check an allowlist against it.

``--format`` names the settings file the block is for. ``settings`` is the
user's ``~/.claude/settings.json`` (``$CLAUDE_CONFIG_DIR/settings.json`` when
that is set). A project's own ``.claude/settings.json`` sits above it and can
switch the sandbox off for its folder, so the user file is a convenience,
not the guarantee. ``managed`` is the managed-settings file, which nothing
below it overrides, at the system path Claude Code reads on this OS. The keys
are the same; what differs is where the block goes and what the notes warn
about. Claude Code's managed-only lock keys (``allowManagedReadPathsOnly``
and the like) are not added: each changes what the user's own settings may
do, which is the administrator's decision, not this command's.

Read-only and print-only. Nothing here writes a settings file, creates a
directory or loads the config through `config.load_config`'s first-run path
(which writes a default config.toml): `load_config_read_only` reads the
user's file where it exists and the managed file either way, and writes
neither. The audit (`posture`) builds the same plan
and compares the settings files it can read against it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from . import claude_paths
from . import config as _config
from ._daemon_client import STATE_DIR_ENV
from .config import (
    ENV_DIR_OVERRIDE,
    GLOBAL_DIR_NAME,
    KEYS_DIR_ENV,
    PROJECT_DIR_NAME,
    Config,
    load_config,
)

#: The values of ``--format``: the user settings file, or the managed one.
FORMATS: tuple[str, ...] = ("settings", "managed")

#: The rules for a project-local store, relative to the session's working
#: directory. As deny rules, a single-segment relative pattern matches the
#: directory at any depth under it (Claude Code's permission documentation).
PROJECT_RULES: tuple[str, ...] = (
    f"Read({PROJECT_DIR_NAME}/**)",
    f"Edit({PROJECT_DIR_NAME}/**)",
)


@dataclass(frozen=True)
class Location:
    """One directory the agent's shell must not reach.

    `role` is what bettermemory keeps there (``store``, ``home store``,
    ``keys``, ``config``, ``state``), `path` the directory, absolute and
    resolved, and `source` what chose it: the variable, the config key or
    the platform default, so a reader can tell an override from the
    default."""

    role: str
    path: Path
    source: str


@dataclass(frozen=True)
class SandboxPlan:
    """The settings block for this host and what it was built from.

    `locations` holds every directory the config and environment resolve,
    `denied` the ones the block names after a directory inside another was
    dropped. `settings` is the block itself, the JSON a user merges into a
    Claude Code settings file. `destination` is the file the block is for
    under `format`; `ports` the daemon's configured port and the one its
    state file records, when they differ."""

    format: str
    platform: str
    locations: tuple[Location, ...]
    denied: tuple[Location, ...]
    settings: dict[str, Any]
    destination: Path
    ports: tuple[int, ...]
    notes: tuple[str, ...] = field(default=())

    def to_json(self, home: Path | None) -> dict[str, Any]:
        """The ``--json`` report: the block, the directories with both
        spellings, the port and the notes."""
        return {
            "format": self.format,
            "platform": self.platform,
            "destination": str(self.destination),
            "settings": self.settings,
            "locations": [
                {
                    "role": loc.role,
                    "path": str(loc.path),
                    "source": loc.source,
                    "denied": loc in self.denied,
                    "sandbox_path": sandbox_path(loc.path, home),
                    "rule_path": rule_path(loc.path, home),
                }
                for loc in self.locations
            ],
            "ports": list(self.ports),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Reading the config without writing one
# ---------------------------------------------------------------------------


def load_config_read_only(path: Path | None = None) -> Config:
    """The config `load_config` would return, without its first-run write.

    `load_config` creates a default config.toml where none exists and says
    so on stderr. A command that promises to write nothing cannot call it on
    a fresh account, so a missing file reads as the defaults it would have
    written (``create=False``), with the organisation-managed file still
    applied over them: a freshly provisioned account, a managed file and no
    config.toml yet, reports the store the daemon serves, not the home
    default. A malformed file still raises, exactly as `load_config` does:
    the caller decides whether that is an error or a finding."""
    config_path = path or _config.default_config_path()
    return load_config(config_path, create=False)


# ---------------------------------------------------------------------------
# The directories
# ---------------------------------------------------------------------------


def home_dir() -> Path | None:
    """The home directory, resolved, or None where none can be found."""
    try:
        return Path.home().resolve()
    except (RuntimeError, OSError):
        return None


def store_rule(config: Config, directory: Path, *, cwd: Path | None = None) -> str:
    """Which rule of `Config.resolved_directory` chose `directory`.

    The order is the config's own: the variable, the config key, a
    ``./.claude-memory`` in the working directory, the home default. A
    working directory that is the home directory resolves its project
    directory to the home default, and reads as the default."""
    if os.environ.get(ENV_DIR_OVERRIDE):
        return ENV_DIR_OVERRIDE
    if config.storage.directory:
        return "[storage] directory"
    home = home_dir()
    if home is not None and directory == (home / GLOBAL_DIR_NAME).resolve():
        return "home default"
    try:
        here = (cwd if cwd is not None else Path.cwd()).resolve()
    except OSError:
        return "home default"
    if directory == (here / PROJECT_DIR_NAME).resolve():
        return f"./{PROJECT_DIR_NAME}"
    return "home default"


def locations(config: Config, *, cwd: Path | None = None) -> list[Location]:
    """Every directory the block must deny, in the documented order: the
    store, the home default store, the keys, the config and the state
    directory. Each path is absolute and resolved, since the sandbox
    applies to real paths (macOS's ``/tmp`` is ``/private/tmp``)."""
    from . import _daemon_client, store as _store

    store_dir = config.resolved_directory(cwd=cwd).resolve()
    found = [Location("store", store_dir, store_rule(config, store_dir, cwd=cwd))]
    home = home_dir()
    if home is not None:
        found.append(
            Location("home store", (home / GLOBAL_DIR_NAME).resolve(), "home default")
        )
    found.append(
        Location(
            "keys",
            _store.resolve_keys_dir().expanduser().resolve(),
            KEYS_DIR_ENV if os.environ.get(KEYS_DIR_ENV) else "user config directory",
        )
    )
    config_dir = (
        config.config_path.parent
        if config.config_path is not None
        else _config.default_config_path().parent
    )
    found.append(
        Location("config", config_dir.expanduser().resolve(), "user config directory")
    )
    found.append(
        Location(
            "state",
            _daemon_client.resolve_state_dir().expanduser().resolve(),
            STATE_DIR_ENV if os.environ.get(STATE_DIR_ENV) else "user state directory",
        )
    )
    return found


def denied_locations(found: list[Location]) -> list[Location]:
    """`found` without the directories another one covers: a duplicate
    after its first appearance, and a directory inside another in the list
    wherever that other one stands."""
    kept: list[Location] = []
    for index, loc in enumerate(found):
        covered = False
        for other_index, other in enumerate(found):
            if other_index == index:
                continue
            if other.path == loc.path:
                if other_index < index:
                    covered = True
            elif other.path in loc.path.parents:
                covered = True
        if not covered:
            kept.append(loc)
    return kept


# ---------------------------------------------------------------------------
# Spelling a path for Claude Code
# ---------------------------------------------------------------------------


def _under_home(path: PurePath, home: Path | None) -> str | None:
    """`path` relative to `home`, in POSIX form, or None when it is not
    under it. The home directory itself is the empty string."""
    if home is None:
        return None
    try:
        relative = path.relative_to(home)
    except (ValueError, TypeError):
        return None
    return relative.as_posix() if str(relative) != "." else ""


def sandbox_path(path: PurePath, home: Path | None) -> str:
    """`path` as a ``sandbox.filesystem`` entry: ``~/x`` under the home
    directory, the absolute path otherwise (``/x``; a Windows path keeps its
    drive, though the sandbox does not run on native Windows)."""
    relative = _under_home(path, home)
    if relative is not None:
        return "~" if relative == "" else f"~/{relative}"
    return path.as_posix()


def rule_path(path: PurePath, home: Path | None) -> str:
    """`path` as the path of a ``Read(...)`` or ``Edit(...)`` rule: ``~/x``
    under the home directory, ``//x`` for any other absolute path. A single
    leading slash in a rule is relative to the settings file, so an
    absolute path needs the second one. Claude Code matches Windows paths in
    POSIX form, ``C:\\x`` as ``/c/x``, so a drive path is ``//c/x``."""
    relative = _under_home(path, home)
    if relative is not None:
        return "~" if relative == "" else f"~/{relative}"
    windows = PureWindowsPath(str(path))
    if windows.drive and windows.drive.endswith(":"):
        rest = windows.relative_to(windows.anchor).as_posix()
        drive = f"//{windows.drive[0].lower()}"
        return drive if rest == "." else f"{drive}/{rest}"
    return "/" + path.as_posix()


def deny_rules(denied: list[Location], home: Path | None) -> list[str]:
    """The ``permissions.deny`` entries: a Read and an Edit rule for each
    denied directory, in the documented order, then the project-relative
    pair. A Read deny rule already blocks the Edit and Write tools on its
    path; the Edit rule covers NotebookEdit, which it does not."""
    rules: list[str] = []
    for loc in denied:
        spelled = rule_path(loc.path, home)
        rules.append(f"Read({spelled}/**)")
        rules.append(f"Edit({spelled}/**)")
    rules.extend(PROJECT_RULES)
    return rules


def settings_block(denied: list[Location], home: Path | None) -> dict[str, Any]:
    """The block docs/installation.md section 5 documents, for `denied`."""
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": [sandbox_path(loc.path, home) for loc in denied],
            },
        },
        "permissions": {"deny": deny_rules(denied, home)},
    }


# ---------------------------------------------------------------------------
# Where the block goes
# ---------------------------------------------------------------------------


def claude_user_settings_path() -> Path:
    """The user's Claude Code settings file: ``$CLAUDE_CONFIG_DIR`` or
    ``~/.claude``, then ``settings.json`` (`claude_paths.user_settings_path`,
    which init, doctor and the audit read it through too)."""
    return claude_paths.user_settings_path()


def claude_managed_settings_path(platform: str | None = None) -> Path:
    """The managed-settings file Claude Code reads on `platform`
    (``sys.platform`` by default): ``/Library/Application Support/ClaudeCode``
    on macOS, ``C:\\Program Files\\ClaudeCode`` on Windows and
    ``/etc/claude-code`` elsewhere. Claude Code no longer reads the legacy
    ``C:\\ProgramData`` location, so it is not offered. A module function so
    the tests, and the audit's tests, never read the real system file."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if platform == "win32":
        base = os.environ.get("ProgramFiles") or r"C:\Program Files"
        return Path(base) / "ClaudeCode" / "managed-settings.json"
    return Path("/etc/claude-code/managed-settings.json")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def daemon_ports(config: Config, store_dir: Path) -> list[int]:
    """The configured port, then the port the store's daemon recorded in
    its state file when one is there and differs: a held port makes the
    daemon fall back to an ephemeral one (`daemon.bind`)."""
    from . import _daemon_client
    from .config import STORE_FILENAME

    ports = [int(config.daemon.port)]
    state = _daemon_client.read_state(
        _daemon_client.resolve_state_dir(), store_dir / STORE_FILENAME
    )
    if state is not None and state["port"] not in ports:
        ports.append(int(state["port"]))
    return ports


def build_plan(
    config: Config,
    *,
    fmt: str = "settings",
    platform: str | None = None,
    cwd: Path | None = None,
) -> SandboxPlan:
    """The block for this host, under `fmt` (one of `FORMATS`)."""
    if fmt not in FORMATS:
        raise ValueError(
            f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}"
        )
    platform = sys.platform if platform is None else platform
    home = home_dir()
    found = locations(config, cwd=cwd)
    denied = denied_locations(found)
    ports = daemon_ports(config, found[0].path)
    destination = (
        claude_managed_settings_path(platform)
        if fmt == "managed"
        else claude_user_settings_path()
    )
    notes = _notes(
        fmt=fmt,
        platform=platform,
        found=found,
        denied=denied,
        ports=ports,
        destination=destination,
        home=home,
    )
    return SandboxPlan(
        format=fmt,
        platform=platform,
        locations=tuple(found),
        denied=tuple(denied),
        settings=settings_block(denied, home),
        destination=destination,
        ports=tuple(ports),
        notes=tuple(notes),
    )


def _notes(
    *,
    fmt: str,
    platform: str,
    found: list[Location],
    denied: list[Location],
    ports: list[int],
    destination: Path,
    home: Path | None,
) -> list[str]:
    """What the block cannot say for itself, in the order a reader acts on
    it: where it goes, the network entries to leave out, then what it does
    not cover on this host."""
    notes: list[str] = []
    if fmt == "managed":
        notes.append(
            f"Install as {destination}, or as a file in managed-settings.d/ beside "
            "it; placing it needs administrator rights. Nothing in a user's or a "
            "project's settings overrides it. The paths are this account's: on a "
            "fleet they hold for every user only where no BETTERMEMORY_* variable "
            "or [storage] directory differs between accounts."
        )
    else:
        notes.append(
            f"Merge into {destination}. A project's .claude/settings.json can "
            "switch the sandbox off for its folder; delivered as managed settings "
            "(--format managed) it holds everywhere."
        )
    listed = ", ".join(str(port) for port in ports)
    notes.append(
        f"The daemon listens on 127.0.0.1, port {listed}. Keep localhost, "
        "*.localhost and 127.0.0.1 out of sandbox.network.allowedDomains unless "
        "the entry names a port, and never this one: an entry without a port "
        "opens every port on the host, the daemon's with it. Leave "
        "sandbox.network.allowLocalBinding off and sandbox.excludedCommands "
        "empty: an excluded command runs as the user."
    )
    notes.append(
        f"The rules for {PROJECT_DIR_NAME}/** keep Claude Code's own Read and "
        "Edit tools off a project-local store; the sandbox has no entry for a "
        "path relative to the session's working directory, so the sandboxed "
        "shell can still read one there, and write it, since the sandbox lets "
        "commands write in the working directory. A store named by "
        "BETTERMEMORY_DIR or [storage] directory outside the working "
        "directory has no such gap."
    )
    store = found[0]
    if store.source == f"./{PROJECT_DIR_NAME}":
        notes.append(
            f"This store was chosen by ./{PROJECT_DIR_NAME} in the working "
            f"directory ({store.path}). The block names it, but other projects' "
            "stores are their own directories."
        )
    if platform == "win32":
        notes.append(
            "The Claude Code sandbox does not run on native Windows: there only "
            "the permissions.deny rules apply, and they cover Claude Code's own "
            "tools, not the commands the agent runs."
        )
    for loc in denied:
        if (home is not None and loc.path == home) or loc.path == Path(loc.path.anchor):
            notes.append(
                f"The {loc.role} directory resolves to {loc.path}: denying it "
                "denies everything under it. Point the variable or config key "
                "that chose it at a directory of its own."
            )
    return notes


__all__ = [
    "FORMATS",
    "PROJECT_RULES",
    "Location",
    "SandboxPlan",
    "build_plan",
    "claude_managed_settings_path",
    "claude_user_settings_path",
    "denied_locations",
    "deny_rules",
    "home_dir",
    "load_config_read_only",
    "locations",
    "rule_path",
    "sandbox_path",
    "settings_block",
    "store_rule",
]
