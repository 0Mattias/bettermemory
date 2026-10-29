"""Claude Code's sandbox settings, the last check of `bettermemory security
audit`, and `Finding`, the result every check of the audit yields.

The check (`_claude`) reads Claude Code's settings files at every level
it reads them (`read_settings`, `_read_claude_documents`, held in
`_ClaudeSettings`) and reports the gaps between the sandbox they
configure and what `bettermemory security sandbox` would print for this
host: the switch (`_sandbox_switch_gaps`), the filesystem
(`_filesystem_gaps`) and the network (`_network_gaps`, with
`_loopback_entries` and `_reaches_loopback`). `posture` runs it after
its own checks.

It lives apart from `posture.py` to keep that module under the package's
2,000-line bound (ENG-96): it is a reader of another program's files,
complete on its own. It imports nothing from `posture`. `Finding` and
`_LEGACY_IPV4` came with it so that it need not: `posture` imports this
module, re-exports `Finding`, `read_settings` and `_loopback_entries`
under their old names and calls `_claude`. Its imports are the standard
library, `claude_paths` and `sandbox_settings`, as `posture`'s were.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import claude_paths, sandbox_settings

if TYPE_CHECKING:
    from .config import Config

#: An IPv4 address in one of the legacy spellings the C resolver
#: (``inet_aton``) still accepts: fewer than four parts (``127.1``), one
#: number (``2130706433``), octal or hex parts (``0177.0.0.1``,
#: ``0x7f.1``). Held to digits, hex digits, ``x`` and dots before it is
#: parsed, since ``inet_aton`` stops at the first space and would read
#: ``127.0.0.1 junk`` as an address.
_LEGACY_IPV4 = re.compile(r"[0-9][0-9a-fA-FxX.]*")


@dataclass(frozen=True)
class Finding:
    """One check's result. `id` is stable for scripts (``mode.key``,
    ``key.match``); `summary` is the one line a person reads; `fix` the
    one-line remedy where there is one; `details` the values behind the
    summary, for ``--json``."""

    id: str
    status: str
    summary: str
    path: str | None = None
    fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "path": self.path,
            "fix": self.fix,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Claude Code's sandbox settings
# ---------------------------------------------------------------------------


def read_settings(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """A Claude Code settings file as a dict, ``(None, None)`` when it is
    absent, ``(None, why)`` when it cannot be read or is not a JSON
    object. Read, never written, through Claude Code's one bounded reader
    (`claude_paths.read_settings_text`, at most
    `claude_paths.SETTINGS_CAP_BYTES`), which doctor reads the same
    files through."""
    try:
        text = claude_paths.read_settings_text(path)
    except FileNotFoundError:
        return None, None
    except UnicodeDecodeError as exc:
        return None, f"not JSON ({exc})"
    except (OSError, ValueError) as exc:
        return None, str(exc)
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, f"not JSON ({exc})"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    return data, None


def _dig(document: dict[str, Any], *keys: str) -> Any:
    value: Any = document
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _strings(value: Any) -> list[str]:
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _settings_paths(platform: str) -> list[tuple[str, Path]]:
    """The settings files the audit reads, in the order Claude Code merges
    them: the managed file, then its drop-in directory's ``*.json`` files in
    name order (hidden ones skipped, as Claude Code skips them), then the
    user's file, in the directory ``CLAUDE_CONFIG_DIR`` names when it is
    set (`claude_paths.user_settings_path`). A project's own settings
    depend on where Claude Code starts, and are not read."""
    managed = sandbox_settings.claude_managed_settings_path(platform)
    paths: list[tuple[str, Path]] = [("managed", managed)]
    drop_ins = managed.parent / "managed-settings.d"
    try:
        paths.extend(
            ("managed", path)
            for path in sorted(drop_ins.glob("*.json"))
            if not path.name.startswith(".")
        )
    except OSError:
        pass
    paths.append(("user", claude_paths.user_settings_path()))
    return paths


def _expand(entry: str, home: Path | None) -> Path | None:
    """A settings path as an absolute path: ``~``/``~/x`` under the home
    directory, ``/x`` and ``//x`` absolute. A relative entry is relative to
    a settings file or a working directory and is not resolved."""
    if entry in ("~", "~/"):
        return home
    if entry.startswith("~/"):
        return None if home is None else home / entry[2:]
    if entry.startswith("/"):
        return Path("/" + entry.lstrip("/"))
    return None


def _spellings(path: Path) -> set[Path]:
    """`path` as written and as resolved: a settings entry names the path
    the user typed, the plan the real one (macOS's ``/tmp`` is
    ``/private/tmp``)."""
    found = {path}
    try:
        found.add(path.resolve())
    except OSError:
        pass
    return found


def _levels_above(base: Path | None, target: Path) -> int | None:
    """How many directories `base` sits above `target`: 0 for `target`
    itself, None when it is neither `target` nor a directory above it."""
    if base is None:
        return None
    levels = [
        len(target.parts) - len(candidate.parts)
        for candidate in _spellings(base)
        if candidate == target or candidate in target.parents
    ]
    return min(levels) if levels else None


def _covers(base: Path | None, target: Path) -> bool:
    return _levels_above(base, target) is not None


def _literal_prefix(entry: str) -> str:
    """`entry` up to the directory before its first wildcard, the directory
    a glob entry can match under; `entry` itself when it has none."""
    for index, char in enumerate(entry):
        if char in "*?[":
            return entry[: entry.rfind("/", 0, index) + 1]
    return entry


def _reopened(
    target: Path, deny_read: list[str], allow_read: list[str], home: Path | None
) -> list[str]:
    """The ``allowRead`` entries that open `target`, or part of it, to the
    sandboxed shell again. Where read rules overlap, the narrower path wins
    (Claude Code's sandboxing documentation): an entry inside `target`
    re-opens that part of it, and one between `target` and the narrowest
    ``denyRead`` entry above it re-opens all of it. An entry at that deny's
    own path counts too, since neither is narrower. A wildcard entry is
    read up to the directory before its first wildcard; a relative entry
    is not read. Empty when no ``denyRead`` entry covers `target`, which
    the deny check reports on its own."""
    nearest = min(
        (
            levels
            for levels in (_levels_above(_expand(e, home), target) for e in deny_read)
            if levels is not None
        ),
        default=None,
    )
    if nearest is None:
        return []
    found: list[str] = []
    for entry in allow_read:
        base = _expand(_literal_prefix(entry), home)
        if base is None:
            continue
        above = _levels_above(base, target)
        inside = any(_levels_above(target, real) for real in _spellings(base))
        if (above is not None and above <= nearest) or inside:
            found.append(entry)
    return found


def _rule_covers(rules: list[str], tool: str, target: Path, home: Path | None) -> bool:
    """Whether a deny rule for `tool` covers every file under `target`: the
    bare tool name, ``//**`` (the whole filesystem), or a pattern ending in
    ``/**`` whose base is `target` or a directory above it. Patterns with
    other wildcards and relative patterns are not interpreted: a relative
    ``**`` matches under the session's working directory only."""
    for rule in rules:
        if rule in (tool, f"{tool}(//**)"):
            return True
        if not (rule.startswith(f"{tool}(") and rule.endswith("/**)")):
            continue
        pattern = rule[len(tool) + 1 : -len("/**)")]
        if "*" in pattern:
            continue
        if pattern.startswith("~"):
            base = _expand(pattern, home)
        elif pattern.startswith("//"):
            base = Path("/" + pattern.lstrip("/"))
        else:
            continue
        if _covers(base, target):
            return True
    return False


def _reaches_loopback(host: str) -> bool:
    """Whether a host in ``sandbox.network.allowedDomains`` reaches this
    machine's loopback listeners, the daemon's among them, in any spelling
    a resolver reads: ``*`` (every host); ``localhost`` and ``*.localhost``,
    the names Claude Code's proxy lets resolve to loopback, with a root
    dot (``localhost.``) or without; and an address, parsed rather than
    matched as text, so every address in ``127.0.0.0/8``, ``::1``, an
    IPv4-mapped one (``::ffff:127.0.0.1``), the unspecified address
    (``0.0.0.0``, ``::``: a connection to it reaches the host's own
    listeners on macOS and Linux) and the legacy IPv4 spellings
    (``127.1``, ``2130706433``) all count."""
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    name = name.rstrip(".")
    if name in ("*", "localhost", "*.localhost") or name.endswith(".localhost"):
        return True
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        if not _LEGACY_IPV4.fullmatch(name):
            return False
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(name))
        except (OSError, ValueError):
            return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _loopback_entries(domains: list[str], ports: tuple[int, ...]) -> list[str]:
    """The ``allowedDomains`` entries that let sandboxed commands reach this
    machine's loopback on the daemon's port or on every port: a host that
    reaches loopback (`_reaches_loopback`) with no port, or with the
    daemon's. The daemon binds 127.0.0.1 alone (`_daemon_client.HOST`), so
    the spellings of that address, ``localhost``, ``*`` and the unspecified
    address reach the daemon itself; another address in ``127.0.0.0/8`` or
    ``::1`` reaches whatever else listens on the loopback, which the block
    keeps sandboxed commands off as well, and is flagged with them."""
    flagged: list[str] = []
    for entry in domains:
        host, port = entry.strip().lower(), None
        if host.startswith("["):
            closing = host.find("]")
            if closing != -1:
                rest = host[closing + 1 :]
                host = host[: closing + 1]
                if rest.startswith(":") and rest[1:].isdigit():
                    port = int(rest[1:])
        elif host.count(":") == 1:
            name, _, tail = host.partition(":")
            if tail.isdigit():
                host, port = name, int(tail)
        if _reaches_loopback(host) and (port is None or port in ports):
            flagged.append(entry)
    return flagged


@dataclass(frozen=True)
class _ClaudeSettings:
    """The settings files the audit read, as (level, path, document): `documents` in merge order, `ranked` highest precedence first."""

    documents: list[tuple[str, Path, dict[str, Any]]]
    ranked: list[tuple[str, Path, dict[str, Any]]]

    def scalar(self, *keys: str) -> tuple[Any, str | None]:
        """Take a key path; returns the value of highest precedence set there and its level, or (None, None)."""
        for level, _, document in self.ranked:
            value = _dig(document, *keys)
            if value is not None:
                return value, level
        return None, None

    def locked(self, *keys: str) -> bool:
        """Take a key path; returns whether a managed file sets it to true."""
        return any(
            _dig(document, *keys) is True
            for level, _, document in self.documents
            if level == "managed"
        )

    def merged(self, *keys: str, managed_only: bool = False) -> list[str]:
        """Take a key path; returns the strings each file lists there, the managed files' alone when managed_only."""
        out: list[str] = []
        for level, _, document in self.documents:
            if level == "managed" or not managed_only:
                out.extend(_strings(_dig(document, *keys)))
        return out


def _read_claude_documents(
    platform: str,
) -> Generator[Finding, None, list[tuple[str, Path, dict[str, Any]]]]:
    """Read the managed and user settings files, yielding a warning for each unreadable one; returns the documents read, as (level, path, document)."""
    documents: list[tuple[str, Path, dict[str, Any]]] = []
    for level, path in _settings_paths(platform):
        document, error = read_settings(path)
        if error is not None:
            yield Finding(
                "claude.settings",
                "warn",
                f"the Claude Code settings file {path} cannot be read: {error}",
                path=str(path),
                fix="fix the file; Claude Code reports it as a settings error too",
            )
        elif document is not None:
            documents.append((level, path, document))
    return documents


def _sandbox_switch_gaps(
    settings: _ClaudeSettings,
) -> Generator[Finding, None, int]:
    """Check the sandbox's failIfUnavailable, allowUnsandboxedCommands and filesystem switches; yields a warning per gap and returns how many."""
    gaps = 0
    fail_if_unavailable, _ = settings.scalar("sandbox", "failIfUnavailable")
    if fail_if_unavailable is not True:
        gaps += 1
        yield Finding(
            "claude.sandbox.fail_if_unavailable",
            "warn",
            "sandbox.failIfUnavailable is not true: where the sandbox cannot "
            "start, commands run without it",
            fix='set "failIfUnavailable": true under "sandbox"',
        )
    unsandboxed, _ = settings.scalar("sandbox", "allowUnsandboxedCommands")
    if unsandboxed is not False:
        gaps += 1
        yield Finding(
            "claude.sandbox.unsandboxed_commands",
            "warn",
            "sandbox.allowUnsandboxedCommands is not false: a command the "
            "sandbox stops can be retried outside it",
            fix='set "allowUnsandboxedCommands": false under "sandbox"',
        )
    filesystem_off, _ = settings.scalar("sandbox", "filesystem", "disabled")
    if filesystem_off is True:
        gaps += 1
        yield Finding(
            "claude.sandbox.filesystem_disabled",
            "warn",
            "sandbox.filesystem.disabled is true: sandboxed commands run without "
            "filesystem isolation, so denyRead holds nothing back",
            fix='remove "disabled" from "sandbox.filesystem"',
        )
    return gaps


def _filesystem_gaps(
    settings: _ClaudeSettings, plan: sandbox_settings.SandboxPlan, home: Path | None
) -> Generator[Finding, None, int]:
    """Check denyRead, allowRead and the deny rules against the plan's denied paths; yields its findings and returns how many are gaps."""
    gaps = 0
    deny_read = settings.merged("sandbox", "filesystem", "denyRead")
    open_dirs = [
        loc
        for loc in plan.denied
        if not any(_covers(_expand(entry, home), loc.path) for entry in deny_read)
    ]
    if open_dirs:
        gaps += 1
        missing = [sandbox_settings.sandbox_path(loc.path, home) for loc in open_dirs]
        yield Finding(
            "claude.sandbox.deny_read",
            "warn",
            "sandbox.filesystem.denyRead leaves these readable to the sandboxed "
            f"shell: {', '.join(missing)}",
            fix="add them to denyRead; `bettermemory security sandbox` prints the list",
            details={"missing": missing},
        )
    allow_read = settings.merged(
        "sandbox",
        "filesystem",
        "allowRead",
        managed_only=settings.locked(
            "sandbox", "filesystem", "allowManagedReadPathsOnly"
        ),
    )
    reopened: dict[str, list[str]] = {}
    for loc in plan.denied:
        entries = _reopened(loc.path, deny_read, allow_read, home)
        if entries:
            reopened[sandbox_settings.sandbox_path(loc.path, home)] = entries
    if reopened:
        gaps += 1
        listed = "; ".join(
            f"{path} (by {', '.join(entries)})" for path, entries in reopened.items()
        )
        yield Finding(
            "claude.sandbox.allow_read",
            "warn",
            "sandbox.filesystem.allowRead opens these to the sandboxed shell "
            f"again, the narrower path winning over denyRead: {listed}",
            fix="drop those allowRead entries, or narrow them to paths outside these",
            details={"reopened": reopened},
        )
    rules = settings.merged("permissions", "deny")
    missing_rules = [
        f"{tool}({sandbox_settings.rule_path(loc.path, home)}/**)"
        for loc in plan.denied
        for tool in ("Read", "Edit")
        if not _rule_covers(rules, tool, loc.path, home)
    ]
    if missing_rules:
        gaps += 1
        yield Finding(
            "claude.sandbox.deny_rules",
            "warn",
            "permissions.deny lacks rules that keep Claude Code's own tools off "
            f"these: {', '.join(missing_rules)}",
            fix="add the rules; `bettermemory security sandbox` prints them",
            details={"missing": missing_rules},
        )
    project_missing = [
        rule
        for rule in sandbox_settings.PROJECT_RULES
        if rule not in rules and rule.replace("(", "(**/", 1) not in rules
    ]
    if project_missing:
        yield Finding(
            "claude.sandbox.project_rules",
            "info",
            "no deny rules for a project-local store: "
            f"{', '.join(project_missing)} would keep Claude Code's tools off one",
            details={"missing": project_missing},
        )
    return gaps


def _network_gaps(
    settings: _ClaudeSettings, plan: sandbox_settings.SandboxPlan, platform: str
) -> Generator[Finding, None, int]:
    """Check the allowed domains, local binding and excluded commands; yields its findings and returns how many are gaps."""
    gaps = 0
    domains = settings.merged(
        "sandbox",
        "network",
        "allowedDomains",
        managed_only=settings.locked("sandbox", "network", "allowManagedDomainsOnly"),
    )
    flagged = _loopback_entries(domains, plan.ports)
    if flagged:
        gaps += 1
        yield Finding(
            "claude.sandbox.network",
            "warn",
            "sandbox.network.allowedDomains lets sandboxed commands reach "
            "this machine's loopback, where the daemon listens on 127.0.0.1: "
            f"{', '.join(flagged)}",
            fix=(
                "name a port other than the daemon's "
                f"({', '.join(str(port) for port in plan.ports)}) on each, or drop them"
            ),
            details={"entries": flagged, "ports": list(plan.ports)},
        )
    local_binding, _ = settings.scalar("sandbox", "network", "allowLocalBinding")
    if local_binding is True and platform == "darwin":
        gaps += 1
        yield Finding(
            "claude.sandbox.local_binding",
            "warn",
            "sandbox.network.allowLocalBinding is true: sandboxed commands can "
            "connect to any port on localhost, the daemon's included",
            fix="leave it off",
        )
    elif local_binding is True:
        # Claude Code's sandboxing documentation: on Linux and WSL2 a
        # sandboxed command's localhost is its own, and the key has no
        # effect there. Native Windows, where the sandbox does not run,
        # returned a skip in `_claude` and never reaches this.
        yield Finding(
            "claude.sandbox.local_binding",
            "info",
            "sandbox.network.allowLocalBinding is true, which has no effect on "
            "Linux: a sandboxed command's localhost is its own",
        )
    excluded = settings.merged("sandbox", "excludedCommands")
    if excluded:
        gaps += 1
        yield Finding(
            "claude.sandbox.excluded_commands",
            "warn",
            "sandbox.excludedCommands runs these as the user, outside the "
            f"sandbox: {', '.join(excluded)}",
            fix="remove them, or accept that each can reach the store, the key and the token",
            details={"commands": excluded},
        )
    return gaps


def _claude(config: Config, *, cwd: Path | None, platform: str) -> Iterator[Finding]:
    """Claude Code's sandbox settings in the managed and user files against
    the block `bettermemory security sandbox` prints for this host. Scalar
    keys take the managed value where one is set (managed settings sit
    above every other level), and among the managed files the last one
    merged: Claude Code merges managed-settings.json first and the drop-ins
    after it in name order, a later file's value replacing an earlier one's.
    List keys merge, as Claude Code merges them, except where a managed
    lock (``allowManagedReadPathsOnly``, ``allowManagedDomainsOnly``, on
    when any managed file sets it) leaves the managed entries alone.
    Beyond the keys the block sets, the audit reads the two that undo it:
    ``sandbox.filesystem.disabled``, and ``allowRead`` entries that open a
    denied directory again. No sandbox at all is information: the
    shell-holding agent is outside the guard by design (SECURITY.md), and
    docs/installation.md section 5 is opt-in. A sandbox that is on with a
    gap in it is a warning."""
    if platform == "win32":
        yield Finding(
            "claude.sandbox",
            "skip",
            "the Claude Code sandbox does not run on native Windows; not checked",
        )
        return
    plan = sandbox_settings.build_plan(
        config, fmt="managed", platform=platform, cwd=cwd
    )
    home = sandbox_settings.home_dir()
    documents = yield from _read_claude_documents(platform)
    read = [str(path) for _, path, _ in documents]
    # Highest precedence first: the managed files, the last merged first,
    # then the user's.
    ranked = [doc for doc in reversed(documents) if doc[0] == "managed"] + [
        doc for doc in documents if doc[0] == "user"
    ]
    settings = _ClaudeSettings(documents, ranked)

    enabled, enabled_level = settings.scalar("sandbox", "enabled")
    if enabled is not True:
        yield Finding(
            "claude.sandbox",
            "info",
            "Claude Code's sandbox is not enabled in the managed or user settings: "
            "an agent with a shell can read the store, the key and the daemon's "
            "token, as SECURITY.md describes",
            fix=(
                "`bettermemory security sandbox --format managed` prints the "
                "settings for this host"
            ),
            details={"read": read},
        )
        return
    gaps = 0
    gaps += yield from _sandbox_switch_gaps(settings)
    gaps += yield from _filesystem_gaps(settings, plan, home)
    gaps += yield from _network_gaps(settings, plan, platform)
    if enabled_level == "user":
        yield Finding(
            "claude.sandbox.scope",
            "info",
            "the sandbox is enabled in the user settings only: a project's "
            ".claude/settings.json can switch it off for its folder",
            fix="deliver it as managed settings (`bettermemory security sandbox --format managed`)",
        )
    if gaps == 0:
        yield Finding(
            "claude.sandbox",
            "ok",
            f"Claude Code's sandbox covers this host's paths ({enabled_level} settings)",
            details={"read": read},
        )
