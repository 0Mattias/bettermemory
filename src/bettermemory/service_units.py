"""Supervised-service units for the local daemon: a launchd LaunchAgent on
macOS and a systemd user unit on Linux (`bettermemory service`).

The daemon was built to run under a supervisor. `up --foreground` serves
in the calling process and moves into its state directory (rc11), a
SIGTERM stops it the way the shutdown endpoint does and it exits 0, and a
second daemon started on a store already served exits with
`DAEMON_HELD_STATUS` (75) without binding anything. What was missing is
the unit: a spawned daemon is a detached child of whichever hook started
it, nothing restarts it after a crash, and nothing starts it at login.

The unit runs the daemon the way `_daemon_client._spawn` does, this
interpreter with ``-m bettermemory up --foreground``, in the state
directory, its output appended to the daemon log `log_file_for` names
(where `DaemonExited` and the docs already look). It carries:

- the restart policy: start at login, and start again after an exit with
  a failure status, `RESTART_SECONDS` apart. An exit 0 is final, because
  a daemon exits 0 when it is asked to stop: a `bettermemory down`, or a
  newer client replacing an older daemon (`_daemon_client._keeps`), which
  must not see the older one come straight back. A daemon that exits 75
  found the store served by a daemon a hook spawned; it is started again
  `RESTART_SECONDS` later and takes over once that one has gone, one
  short-lived process and one log line per attempt meanwhile.
  `service install` stops a running daemon of the store before it loads
  the unit, so that race needs a crash to happen at all;
- the stop bound, `STOP_TIMEOUT_SECONDS`, above the part of a stop the
  daemon bounds itself (`SHUTDOWN_TIMEOUT_SECONDS`: the requests in
  flight plus the workers' join) and the longest step a worker can be in
  that the stop's interrupt does not reach (`UNINTERRUPTED_STEP_SECONDS`),
  so the supervisor's kill does not cut such a stop short, and is the
  hard bound on one that runs longer;
- an owner-only umask, so the files the daemon creates are owner-only
  whatever the login session's umask is (`service install` creates the
  log itself, 0600, before the supervisor first appends to it);
- the installing shell's environment where the daemon would resolve a
  path differently without it (`pinned_environment`).

Everything here is a pure function of the paths, the environment and the
platform it is given: the text of a unit, where it goes, the commands
that load and unload it. `cli/service_cmd.py` resolves those inputs,
writes the file and runs the commands; the tests check the text and the
command lists and never run launchctl or systemctl.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

from ._daemon_client import (
    SHUTDOWN_TIMEOUT_SECONDS,
    STATE_DIR_ENV,
    UNINTERRUPTED_STEP_SECONDS,
)

LAUNCHD = "launchd"
SYSTEMD = "systemd"
SUPERVISORS: tuple[str, ...] = (LAUNCHD, SYSTEMD)

#: The LaunchAgent's label, and the plist's name under ~/Library/LaunchAgents.
LAUNCHD_LABEL = "io.github.0mattias.bettermemory"
#: The systemd user unit's name, under ~/.config/systemd/user.
SYSTEMD_UNIT = "bettermemory.service"

#: How long the supervisor waits, after its SIGTERM, before it kills the
#: daemon: the part of a stop the daemon bounds itself
#: (`SHUTDOWN_TIMEOUT_SECONDS`, `daemon.GRACEFUL_SHUTDOWN_SECONDS` for the
#: requests in flight plus `daemon.WORKERS_CLOSE_SECONDS` for the workers'
#: join, 30 s), then the longest step a worker's job can be in that the
#: stop's interrupt does not reach (`UNINTERRUPTED_STEP_SECONDS`, a git
#: call's 20 s), and ten seconds for the store's close and the
#: interpreter's exit: 60 s. It was 40 s, the step left out, so a stop that
#: found a job inside a git call could be killed before its workers ended.
#: The kill is the hard bound: a job that runs past it is ended with the
#: process, the OS drops the process's connections and its lock, and the
#: next start takes the store over. launchd's default (20 s) is shorter
#: than the daemon's bound; systemd's is 90 s unless a distribution or an
#: administrator lowered DefaultTimeoutStopSec, so the unit names its own
#: either way.
STOP_TIMEOUT_SECONDS = (
    int(SHUTDOWN_TIMEOUT_SECONDS) + int(UNINTERRUPTED_STEP_SECONDS) + 10
)

#: The least time between two starts of the daemon by the supervisor:
#: launchd's ThrottleInterval and systemd's RestartSec. It spaces the
#: retries of a daemon that found its store served by another (exit 75),
#: and is short beside the time a crashed daemon is missed, since a hook
#: that finds no daemon spawns one of its own meanwhile.
RESTART_SECONDS = 30

#: Variables carried from the installing environment into the unit when
#: they are set, made absolute against the directory `install` ran in,
#: the meaning a relative value had there. A supervisor starts the daemon
#: with an environment of its own (launchd's is nearly empty), so a store,
#: a keys directory or a config directory the shell names would otherwise
#: be resolved differently, and the daemon would serve a store, or read a
#: key, that the user's clients do not.
PINNED_PATH_VARIABLES: tuple[str, ...] = (
    "BETTERMEMORY_DIR",
    "BETTERMEMORY_KEYS_DIR",
    # Where platformdirs puts the config file and the default keys
    # directory on Linux.
    "XDG_CONFIG_HOME",
)
#: Variables carried over as they are. PATH because the daemon runs
#: `git` by name (`_git`), and launchd's PATH is /usr/bin:/bin:/usr/sbin:
#: /sbin: a git the user installed elsewhere would be missed.
#: BETTERMEMORY_FLOCK_TIMEOUT tunes the daemon's own lock waits, and
#: BETTERMEMORY_INDEX_THRESHOLD its searches (`_handlers`), which a
#: spawned daemon inherits from the shell that started it. The
#: identity variables (BETTERMEMORY_CLIENT, _CLIENT_VERSION, _MODEL,
#: _WORKSPACE) are left out on purpose: they describe the client a
#: process serves, and one daemon serves every client, the reason
#: `_daemon_client.daemon_env_for` drops the harness session id from a
#: spawned daemon's environment.
PINNED_VARIABLES: tuple[str, ...] = (
    "BETTERMEMORY_FLOCK_TIMEOUT",
    "BETTERMEMORY_INDEX_THRESHOLD",
    "PATH",
)

# Path components of an interpreter that a package manager may delete:
# uv's cache (`uvx`, `uv run --with`: archive-v0, builds-v0,
# environments-v2 under the cache directory) and pipx's (`pipx run`).
_UV_EPHEMERAL = re.compile(r"(archive|builds|environments)-v\d+")


@dataclass(frozen=True)
class UnitSpec:
    """Everything a unit says, resolved: the supervisor that runs it, the
    file it is written to, the command, its environment, its working
    directory and the log its output is appended to."""

    supervisor: str
    path: Path
    command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: Path
    log_path: Path


@dataclass(frozen=True)
class Step:
    """One command `install` or `uninstall` runs. A step that is not
    `required` may fail without stopping the ones after it: unloading a
    unit that is not loaded is the usual case."""

    argv: tuple[str, ...]
    required: bool = True


def supervisor_for(platform: str) -> str | None:
    """The supervisor this module writes a unit for on `platform` (a
    `sys.platform` value): launchd on macOS, systemd on Linux, None
    elsewhere."""
    if platform == "darwin":
        return LAUNCHD
    if platform.startswith("linux"):
        return SYSTEMD
    return None


def unit_path(supervisor: str, *, home: Path, env: Mapping[str, str]) -> Path:
    """Where the unit for `supervisor` goes: the user's LaunchAgents
    directory, or the systemd user unit directory under
    ``$XDG_CONFIG_HOME`` (``~/.config`` when it is unset or not absolute,
    the rule systemd itself applies)."""
    if supervisor == LAUNCHD:
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if supervisor == SYSTEMD:
        config_home = env.get("XDG_CONFIG_HOME") or ""
        base = Path(config_home) if PurePath(config_home).is_absolute() else None
        return (base or home / ".config") / "systemd" / "user" / SYSTEMD_UNIT
    raise ValueError(f"no unit for supervisor {supervisor!r}")


def daemon_command(executable: str) -> tuple[str, ...]:
    """The daemon's command line: `executable` (this interpreter) running
    ``bettermemory up --foreground``, as `_daemon_client._spawn` runs it,
    so a supervised daemon and a spawned one are the same install."""
    return (executable, "-m", "bettermemory", "up", "--foreground")


def _absolute(value: str, *, cwd: Path, home: Path) -> str:
    if value == "~" or value.startswith(("~/", "~\\")):
        value = str(home) + value[1:]
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    # Lexical normalisation, not `resolve()`: the daemon resolves the
    # value itself at start, and a symlink resolved here would pin today's
    # target instead of the link the user named.
    return os.path.normpath(path)


def pinned_environment(
    env: Mapping[str, str], *, state_dir: Path, cwd: Path, home: Path
) -> dict[str, str]:
    """The environment the unit sets: each of `PINNED_PATH_VARIABLES` and
    `PINNED_VARIABLES` that `env` sets (an empty value is unset, the
    resolution rule's own reading), the path ones absolute against `cwd`
    with ``~`` read as `home`, and `BETTERMEMORY_STATE_DIR` always, as
    `state_dir`: the daemon writes its state file there, and the clients
    look there, so the two must not resolve it apart."""
    pinned: dict[str, str] = {}
    for name in PINNED_PATH_VARIABLES:
        value = env.get(name)
        if value:
            pinned[name] = _absolute(value, cwd=cwd, home=home)
    for name in PINNED_VARIABLES:
        value = env.get(name)
        if value:
            pinned[name] = value
    pinned[STATE_DIR_ENV] = str(state_dir)
    return dict(sorted(pinned.items()))


def build_spec(
    supervisor: str,
    *,
    executable: str,
    env: Mapping[str, str],
    home: Path,
    cwd: Path,
    state_dir: Path,
    log_path: Path,
) -> UnitSpec:
    """The unit for `supervisor`, from the paths and environment given."""
    return UnitSpec(
        supervisor=supervisor,
        path=unit_path(supervisor, home=home, env=env),
        command=daemon_command(executable),
        environment=tuple(
            pinned_environment(env, state_dir=state_dir, cwd=cwd, home=home).items()
        ),
        working_directory=state_dir,
        log_path=log_path,
    )


def render(spec: UnitSpec) -> str:
    """The unit file's text."""
    if spec.supervisor == LAUNCHD:
        return render_launchd(spec)
    if spec.supervisor == SYSTEMD:
        return render_systemd(spec)
    raise ValueError(f"no unit for supervisor {spec.supervisor!r}")


_HEADER = (
    "Written by `bettermemory service install`. Run it again rather than "
    "editing this file; `bettermemory service uninstall` removes it."
)


def render_launchd(spec: UnitSpec) -> str:
    """The LaunchAgent plist. KeepAlive restarts the daemon only after an
    unsuccessful exit (see the module docstring); ExitTimeOut is the stop
    bound; Umask 63 is 0o077. plistlib is imported here, not at the top:
    `print` and `status` import this module for a systemd unit too, and
    only a launchd unit needs it."""
    import plistlib

    plist: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": list(spec.command),
        "EnvironmentVariables": dict(spec.environment),
        "WorkingDirectory": str(spec.working_directory),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RESTART_SECONDS,
        "ExitTimeOut": STOP_TIMEOUT_SECONDS,
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
        "Umask": 0o077,
    }
    text = plistlib.dumps(plist, sort_keys=True).decode("utf-8")
    # A comment after the doctype, where plistlib (expat) reads past it.
    head, sep, rest = text.partition("<plist ")
    return f"{head}<!-- {_HEADER} -->\n{sep}{rest}"


def _systemd_value(text: str) -> str:
    """`text` for a systemd setting that takes it whole (a path): `%`
    doubled, since systemd expands specifiers there. A line break cannot
    be written into a unit at all."""
    if "\n" in text or "\r" in text:
        raise ValueError(f"a line break cannot be written into a unit: {text!r}")
    return text.replace("%", "%%")


def _systemd_quoted(text: str, *, command: bool) -> str:
    """`text` as one double-quoted systemd word: backslashes and quotes
    escaped (C-style unescaping applies inside the quotes), `%` doubled
    (specifiers), and in a command line `$` doubled (variables)."""
    escaped = _systemd_value(text).replace("\\", "\\\\").replace('"', '\\"')
    if command:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def render_systemd(spec: UnitSpec) -> str:
    """The systemd user unit. Restart=on-failure restarts the daemon only
    after an unsuccessful exit (see the module docstring); KillMode=mixed
    sends the stop's SIGTERM to the daemon alone, so a git it is running
    finishes inside the graceful stop instead of failing the request;
    TimeoutStopSec is the stop bound. `append:` output needs systemd 240;
    an older one ignores the two lines with a warning and keeps the
    output in the journal."""
    exec_start = " ".join(_systemd_quoted(arg, command=True) for arg in spec.command)
    environment = [
        f"Environment={_systemd_quoted(f'{name}={value}', command=False)}"
        for name, value in spec.environment
    ]
    log = _systemd_value(str(spec.log_path))
    lines = [
        f"# {_HEADER}",
        "[Unit]",
        "Description=bettermemory local daemon",
        "Documentation=https://github.com/0Mattias/bettermemory",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        f"WorkingDirectory={_systemd_value(str(spec.working_directory))}",
        *environment,
        "Restart=on-failure",
        f"RestartSec={RESTART_SECONDS}",
        "KillMode=mixed",
        f"TimeoutStopSec={STOP_TIMEOUT_SECONDS}",
        "UMask=0077",
        "NoNewPrivileges=yes",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _launchd_target(uid: int) -> str:
    return f"gui/{uid}/{LAUNCHD_LABEL}"


def load_steps(spec: UnitSpec, *, uid: int) -> list[Step]:
    """The commands that load a written unit and start the daemon.

    launchd: the job is booted out first, since bootstrap refuses a label
    already loaded (a reinstall); then enabled, which undoes an earlier
    ``launchctl disable``; then bootstrapped, which starts it (RunAtLoad).
    systemd: the manager rereads its units, the unit is enabled for the
    next login, and restarted, which starts it or replaces one running
    from the unit's previous text (``enable --now`` would leave that
    running)."""
    if spec.supervisor == LAUNCHD:
        return [
            Step(("launchctl", "bootout", _launchd_target(uid)), required=False),
            Step(("launchctl", "enable", _launchd_target(uid))),
            Step(("launchctl", "bootstrap", f"gui/{uid}", str(spec.path))),
        ]
    if spec.supervisor == SYSTEMD:
        return [
            Step(("systemctl", "--user", "daemon-reload")),
            Step(("systemctl", "--user", "enable", SYSTEMD_UNIT)),
            Step(("systemctl", "--user", "restart", SYSTEMD_UNIT)),
        ]
    raise ValueError(f"no unit for supervisor {spec.supervisor!r}")


def unload_steps(spec: UnitSpec, *, uid: int) -> list[Step]:
    """The commands that stop the daemon and unload the unit, before its
    file is removed. Each may fail: the unit may not be loaded."""
    if spec.supervisor == LAUNCHD:
        return [Step(("launchctl", "bootout", _launchd_target(uid)), required=False)]
    if spec.supervisor == SYSTEMD:
        return [
            Step(
                ("systemctl", "--user", "disable", "--now", SYSTEMD_UNIT),
                required=False,
            )
        ]
    raise ValueError(f"no unit for supervisor {spec.supervisor!r}")


def after_removal_steps(spec: UnitSpec) -> list[Step]:
    """The commands that follow the unit file's removal: systemd rereads
    its units so the removed one is forgotten; launchd needs nothing."""
    if spec.supervisor == SYSTEMD:
        return [Step(("systemctl", "--user", "daemon-reload"), required=False)]
    return []


def status_steps(spec: UnitSpec, *, uid: int) -> list[Step]:
    """The read-only commands `service status` asks the supervisor with."""
    if spec.supervisor == LAUNCHD:
        return [Step(("launchctl", "print", _launchd_target(uid)), required=False)]
    if spec.supervisor == SYSTEMD:
        return [
            Step(("systemctl", "--user", "is-enabled", SYSTEMD_UNIT), required=False),
            Step(("systemctl", "--user", "is-active", SYSTEMD_UNIT), required=False),
        ]
    raise ValueError(f"no unit for supervisor {spec.supervisor!r}")


_LAUNCHCTL_FIELDS = {
    "state": re.compile(r"^\s*state = (.+?)\s*$", re.MULTILINE),
    "pid": re.compile(r"^\s*pid = (\d+)\s*$", re.MULTILINE),
    "last_exit": re.compile(r"^\s*last exit code = (.+?)\s*$", re.MULTILINE),
}


def parse_launchctl_print(text: str) -> dict[str, str]:
    """The job's state, pid and last exit code from ``launchctl print``'s
    output, where it reports them (the format is not a stable interface,
    so a field it no longer prints is left out, not guessed)."""
    found: dict[str, str] = {}
    for name, pattern in _LAUNCHCTL_FIELDS.items():
        match = pattern.search(text)
        if match:
            found[name] = match.group(1)
    return found


def ephemeral_interpreter(prefix: str, env: Mapping[str, str]) -> bool:
    """Whether an interpreter whose environment lives at `prefix`
    (`sys.prefix`) is one a package manager may delete under the unit:
    a `uvx` or `uv run --with` environment in uv's cache, one in the
    directory ``UV_CACHE_DIR`` names, or a `pipx run` environment in
    pipx's cache. A unit pointing there stops working the day the cache
    is pruned, with nothing to say why."""
    parts = PurePath(prefix).parts
    lowered = [part.lower() for part in parts]
    for index, part in enumerate(lowered):
        above = lowered[:index]
        if _UV_EPHEMERAL.fullmatch(part) and "uv" in above:
            return True
        # pipx keeps `pipx run` environments under $PIPX_HOME/.cache, or
        # under a cache directory's pipx/ on the platforms that split it.
        if part == ".cache" and "pipx" in above:
            return True
        if part == "pipx" and ({".cache", "caches"} & set(above)):
            return True
    cache = env.get("UV_CACHE_DIR")
    if cache:
        cache_parts = PurePath(cache).parts
        if parts[: len(cache_parts)] == cache_parts:
            return True
    return False


def manual_instructions(
    command: Sequence[str], *, state_dir: Path, windows: bool = False
) -> str:
    """What to tell a user on a platform with no unit here: the command a
    supervisor of their own runs, and what it needs to know. The command
    is quoted for the platform's own command line (`windows`: the rules
    `subprocess` applies there), since an interpreter under a directory
    with a space in its name, ``C:\\Program Files`` or a user profile, is
    the usual case on Windows and a bare join would split it."""
    if windows:
        import subprocess

        line = subprocess.list2cmdline(list(command))
    else:
        import shlex

        line = shlex.join(command)
    return (
        "bettermemory service writes launchd (macOS) and systemd (Linux) "
        "units only.\n"
        "To run the daemon under another supervisor (Task Scheduler at log "
        "on, a process manager), have it run:\n"
        f"  {line}\n"
        f"in {state_dir}, start it again when it exits with a status other "
        f"than 0, and allow it {STOP_TIMEOUT_SECONDS} seconds to stop. "
        "`bettermemory down` stops it gracefully on every platform.\n"
    )


__all__ = [
    "LAUNCHD",
    "LAUNCHD_LABEL",
    "PINNED_PATH_VARIABLES",
    "PINNED_VARIABLES",
    "RESTART_SECONDS",
    "STOP_TIMEOUT_SECONDS",
    "SUPERVISORS",
    "SYSTEMD",
    "SYSTEMD_UNIT",
    "Step",
    "UnitSpec",
    "after_removal_steps",
    "build_spec",
    "daemon_command",
    "ephemeral_interpreter",
    "load_steps",
    "manual_instructions",
    "parse_launchctl_print",
    "pinned_environment",
    "render",
    "render_launchd",
    "render_systemd",
    "status_steps",
    "supervisor_for",
    "unit_path",
    "unload_steps",
]
