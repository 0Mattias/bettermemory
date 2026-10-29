"""`bettermemory doctor`: one read-only pass over the install, the config,
the store, the daemon and the client wiring.

The facts a user needs when memory "does nothing" are scattered across
the code that acts on them: which rule picked the store
(`Config.resolved_directory`), whether the store's key is present and
matches (`Store.open`), whether a daemon answers and at which version
(`_daemon_client`), and whether the hooks and MCP entries a client runs
still name an installed binary (`init`). Each of those paths also acts:
`load_config` writes a default config where none exists, `cli_context`
creates a store and rekeys one whose key is absent, `ensure_daemon`
replaces an older daemon, and `bettermemory status` removes a state file
whose daemon does not answer. A diagnosis that changed what it diagnosed
would hide the fault it was run for, so this module reads only:

- the config is parsed only when the file exists, and `load_config` is
  never reached for a missing one (it would write the default);
- the store opens with `Store.open(..., read_only=True)`, as
  `log verify` and `stats` open it: a store without its key is reported,
  never rekeyed, and no store is created where none exists; a store file
  inside a backup bundle is not opened at all (its open would change the
  snapshot and fail the bundle's hash) and fails the `store` check;
- a restore's record beside the store is read, never removed, and its
  lock is not held past the look (`restore_lock.state_finding`): a
  restore that did not finish refuses every writing open, which the
  read-only open never meets, and fails the `restore` check;
- the daemon is asked `GET /health` and nothing else; its state file is
  read, never removed, and no daemon is started or stopped;
- client and Claude Code settings files are read, never patched;
- the keys directory is listed and each key ring's binding read, and
  nothing in it changes unless `--clear-orphan-keys --yes` is given,
  which moves the rings whose store is shown gone to the keys
  directory's quarantine, deleting nothing (`check_orphan_keys`).

The read-only open writes nothing to the store file or beside it
(`store_schema.connect_read_only`, 9.0.0rc14): no WAL switch, no chmod,
no membership tables added to a store an earlier build created, and no
`-wal` or `-shm` left behind. A closed WAL-mode store is read from a
private copy, removed when the check closes it; one a daemon has open
is read in place through the `-shm` it shares with the daemon, as any
reader reads it.

Each check answers `ok`, `warn` or `fail` with a one-line summary, a
one-line remedy when it is not `ok`, and a details mapping for `--json`.
A check whose input an earlier check could not establish (the store when
the config does not load) answers `warn` with a "not checked" summary:
the earlier check already carries the failure. Posture (file modes,
owners, the sandbox settings) is `bettermemory security audit`'s, which
the security check points at rather than duplicating.

The statuses, `Finding`, `Check`, `_Context` and the verdict helpers are
`doctor_model`'s, and the store, restore and index checks
`doctor_store`'s. Neither imports anything from here; this module
imports both and re-exports every name of theirs a caller reads.
"""

from __future__ import annotations

import contextlib
import filecmp
import importlib.util
import json
import logging
import os
import shlex
import shutil
import sys
import sysconfig
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _install_hints, claude_paths
from .doctor_model import (
    EXIT_CODES as EXIT_CODES,
    FAIL as FAIL,
    OK as OK,
    WARN as WARN,
    Check as Check,
    Finding as Finding,
    _Context as _Context,  # tests/test_doctor*.py, test_key_state.py and others
    _from_findings as _from_findings,  # store_index.doctor_check, tests
    _not_checked as _not_checked,  # store_index.doctor_check
    _worst,
)
from .doctor_store import (
    _ROLLBACK_REMEDY as _ROLLBACK_REMEDY,  # tests/test_doctor.py, test_key_runbook.py
    _head_finding as _head_finding,  # tests/test_doctor.py, test_key_runbook.py
    _open_remedy as _open_remedy,  # tests/test_store_admission.py
    check_index as check_index,  # run_checks; tests/test_index_integrity.py
    check_restore as check_restore,  # run_checks; tests/test_restore_lock.py
    check_store as check_store,  # run_checks; tests/test_key_state.py and others
)

if TYPE_CHECKING:
    from .config import Config

#: The order the checks run and report in.
CHECK_IDS: tuple[str, ...] = (
    "install",
    "config",
    "store_location",
    "restore",
    "store",
    "key",
    "index",
    "daemon",
    "claude_code",
    "clients",
    "disk",
    "security",
    "orphan_keys",
)

#: The modules a running daemon imports that the CLI's own start does not:
#: an install that lost one (a half-finished upgrade, a pruned venv)
#: serves nothing, while `bettermemory doctor` itself still runs.
_RUNTIME_MODULES: tuple[str, ...] = ("mcp", "pydantic", "yaml", "platformdirs")

#: The newest Python the classifiers in pyproject.toml name.
_NEWEST_TESTED_PYTHON = (3, 14)

#: Free space under the store below which a write can fail outright.
#: Derived, not measured: the store keeps SQLite's default WAL
#: autocheckpoint (1,000 pages; the store sets none) at
#: `store_schema.PAGE_SIZE` (8 KiB), so a WAL restarts near 8 MiB, and the
#: checkpoint that restarts it copies those pages into the store file:
#: about 16 MiB of new room between two checkpoints. 64 MiB is four times
#: that, for a WAL a long reader keeps from restarting and for the
#: daemon's log, which has no size cap and shares the filesystem when the
#: state directory does.
DISK_FAIL_BYTES = 64 * 1024 * 1024
#: Free space below which the next growth can reach the floor above: this,
#: or twice the store's size, whichever is larger. Twice the store is
#: derived: `backup create` writes a full copy through SQLite's backup API,
#: by default under ``backups/`` beside the store, and `restore` a full
#: copy of a bundle's snapshot next to the store file. The 1 GiB floor is
#: not measured; it keeps the warning for a small store,
#: whose doubled size would warn only on a disk all but full. What would
#: set it is the store's growth between two `doctor` runs on real hosts
#: (the store file and its WAL, sampled daily): the floor belongs above the
#: tail of that growth plus `DISK_FAIL_BYTES`.
DISK_WARN_BYTES = 1024 * 1024 * 1024

#: How long the daemon check waits for the state file of a process that
#: holds the store's lock without one: an ordinary start writes it within
#: this, and a lock still held with no file after it is the fault (or a
#: long first-start migration, which the remedy names). Measured on the
#: owner's Mac, on an empty scratch store, from the lock's acquisition to
#: the state file's write (`daemon.serve`: the migration check, the bind,
#: the app's build): 0.08 to 0.12 s over ten starts, 0.10 to 0.15 s over
#: three with no bytecode cache. Two seconds is thirteen times the slowest;
#: a slower host is not measured, and a wait that runs long costs only
#: this check's time, where one that runs short reports a fault that is
#: not there.
DAEMON_START_GRACE_SECONDS = 2.0
#: The poll inside that wait: a start the poll just missed is seen at most
#: this much later, under half the fastest measured start.
_GRACE_POLL_SECONDS = 0.05
#: How long the daemon check waits for the daemon's proof (`/health`, with
#: `_daemon_client.identify`): the wait `bettermemory status` gives the
#: same probe (`cli.daemon_cmd._HEALTH_TIMEOUT_SECONDS`; the command
#: layer imports this module, not the reverse, so a test pins the two
#: equal), so the two read one daemon alike. Not measured; what would set
#: it is the reply time of `/health` while the daemon's workers are busy.
DAEMON_PROBE_TIMEOUT_SECONDS = 1.0

#: The Claude Code hook events the plugin's hooks.json wires, and the
#: `bettermemory hook` word each one runs.
CLAUDE_HOOK_EVENTS: dict[str, str] = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
}
#: The plugin's name in Claude Code's `enabledPlugins` keys
#: (`<plugin>@<marketplace>`).
PLUGIN_NAME = "bettermemory"

_PLUGIN_INSTALL = "/plugin install bettermemory@bettermemory"
_BUG_REMEDY = (
    "this is a bug in doctor; report it with this output at "
    "https://github.com/0Mattias/bettermemory/issues"
)
_REINSTALL_REMEDY = (
    f"reinstall bettermemory (`{_install_hints.tool_reinstall_package()}`, "
    "or the installer you used)"
)


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def install_method(
    prefix: Path,
    base_prefix: Path,
    direct_url: Mapping[str, Any] | None,
    *,
    installed: bool,
) -> str:
    """How this copy of the package was installed, from where its
    interpreter lives. A heuristic over the layouts the docs name: uv's
    tool directory (`.../uv/tools/bettermemory`), the cached environment
    `uvx` runs (`.../uv/.../archive-v0/<hash>`), pipx's venvs, an
    editable install (PEP 610's `direct_url.json` says so), any other
    virtual environment, else the interpreter's own site-packages."""
    if not installed:
        return "source tree, not installed"
    if direct_url is not None:
        dir_info = direct_url.get("dir_info")
        if isinstance(dir_info, Mapping) and dir_info.get("editable") is True:
            return "editable install"
    parts = [part.lower() for part in prefix.parts]
    if "pipx" in parts and "venvs" in parts:
        return "pipx"
    if any(part.startswith("archive-v") for part in parts):
        return "uvx (cached environment)"
    if "uv" in parts and "tools" in parts:
        return "uv tool"
    if prefix != base_prefix:
        return "virtual environment"
    return "system or user site-packages"


def _distribution_facts() -> tuple[bool, dict[str, Any] | None]:
    """(whether the package is installed, its PEP 610 `direct_url.json`)."""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution("bettermemory")
    except PackageNotFoundError:
        return False, None
    try:
        text = dist.read_text("direct_url.json")
        loaded = json.loads(text) if text else None
    except (OSError, ValueError):
        return True, None
    return True, loaded if isinstance(loaded, dict) else None


def this_install_binary() -> Path | None:
    """The `bettermemory` console script beside this interpreter, where the
    install has one: what a client or a hook should be running."""
    name = "bettermemory.exe" if sys.platform == "win32" else "bettermemory"
    scripts = sysconfig.get_path("scripts")
    if not scripts:
        return None
    candidate = Path(scripts) / name
    return candidate if candidate.is_file() else None


def _same_binary(a: Path, b: Path) -> bool:
    """Whether two paths run the same program: one file (a symlink to the
    other, as `uv tool` links `~/.local/bin`), or byte-identical copies,
    as a Windows launcher is copied rather than linked."""
    try:
        if a.resolve() == b.resolve():
            return True
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _missing_modules() -> list[str]:
    missing = []
    for name in _RUNTIME_MODULES:
        try:
            found = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            found = None
        if found is None:
            missing.append(name)
    return missing


def check_install() -> Check:
    """The package version, the Python running it, how it was installed,
    and whether the `bettermemory` on PATH is this install."""
    from . import __version__

    installed, direct_url = _distribution_facts()
    method = install_method(
        Path(sys.prefix), Path(sys.base_prefix), direct_url, installed=installed
    )
    python = ".".join(str(part) for part in sys.version_info[:3])
    on_path = shutil.which("bettermemory")
    mine = this_install_binary()
    details: dict[str, Any] = {
        "version": __version__,
        "python": python,
        "executable": sys.executable,
        "method": method,
        "path_binary": on_path,
        "install_binary": None if mine is None else str(mine),
    }
    summary = f"bettermemory {__version__} on Python {python} ({method})"
    findings: list[Finding] = []
    missing = _missing_modules()
    if missing:
        findings.append(
            Finding(
                FAIL,
                f"this install cannot import {', '.join(missing)}: the daemon "
                "and the MCP server cannot start",
                _REINSTALL_REMEDY,
            )
        )
    if __version__ == "0+unknown":
        findings.append(
            Finding(
                WARN,
                "the package version is unknown (a source tree no build has "
                "touched): a client replaces a daemon it cannot place",
                "install the checkout (`uv sync` or `pip install -e .`) so the "
                "build writes its version",
            )
        )
    if tuple(sys.version_info[:2]) > _NEWEST_TESTED_PYTHON:
        newest = ".".join(str(part) for part in _NEWEST_TESTED_PYTHON)
        findings.append(
            Finding(
                WARN,
                f"Python {python} is newer than the newest tested ({newest})",
                f"install bettermemory under Python 3.11 to {newest} if anything "
                "misbehaves",
            )
        )
    if (
        on_path is not None
        and mine is not None
        and not _same_binary(Path(on_path), mine)
    ):
        findings.append(
            Finding(
                WARN,
                f"the `bettermemory` on PATH ({on_path}) is another install than "
                f"this one ({mine}): a client that runs `bettermemory` gets that one",
                "remove the install you do not use, or put the one you do first on PATH",
            )
        )
    if on_path is None:
        summary += "; no `bettermemory` on PATH"
    return _from_findings("install", findings, ok_summary=summary, details=details)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class _NoticeCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _config_notices() -> Iterator[list[str]]:
    """The warnings the config module logs inside the block, held here
    rather than printed: the loader's fallbacks (`search_mode`,
    `prompt_recall`, `checkpoint_tokens`) and the store under a system
    directory are each logged once and never raised, so a report built
    from them is the only place doctor can show them."""
    logger = logging.getLogger("bettermemory.config")
    handler = _NoticeCapture()
    propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate


def removed_keys(data: Mapping[str, Any]) -> list[str]:
    """The keys and sections a parsed config sets that a release removed,
    from the loader's own registries, so the two cannot disagree.
    The loader ignores them with a once-per-process notice, which a second
    load in the same process would not repeat; these are read every time."""
    from . import config as _config

    found: list[str] = []
    registries: tuple[tuple[str, str], ...] = (
        ("behavior", "_REMOVED_BEHAVIOR_KEYS"),
        ("telemetry", "_REMOVED_TELEMETRY_KEYS"),
        ("capture", "_REMOVED_CAPTURE_KEYS"),
    )
    for section, attribute in registries:
        registry: Mapping[str, tuple[str, str]] = getattr(_config, attribute, {})
        raw = data.get(section)
        if not isinstance(raw, Mapping):
            continue
        for key, (removed_in, _why) in registry.items():
            if key in raw:
                found.append(f"[{section}] {key} (removed in {removed_in})")
    sections: Mapping[str, tuple[str, str]] = getattr(_config, "_REMOVED_SECTIONS", {})
    for name, (removed_in, _why) in sections.items():
        if name in data:
            found.append(f"[{name}] (removed in {removed_in})")
    return found


def check_config(ctx: _Context) -> Check:
    """The config file: where it is, whether it exists (never created
    here), whether it parses and loads, and the keys a release removed."""
    from . import config as _config

    path = _config.default_config_path()
    details: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.exists():
        # The defaults, with the organisation-managed file over them, as
        # the daemon loads them: a freshly provisioned account has a
        # managed file and no config.toml yet, and its store is the one
        # the managed file pins. Nothing is written (`create=False`).
        try:
            ctx.config = _config.load_config(path, create=False)
        except Exception as exc:  # noqa: BLE001 - every load error is the report
            ctx.config_failed = True
            return Check(
                "config",
                FAIL,
                f"no config file at {path}, and the defaults do not load: {exc}",
                "fix the file the error names: no daemon starts until it loads",
                details,
            )
        return Check(
            "config",
            OK,
            f"no config file at {path}; the defaults apply (the first daemon "
            "start writes one)",
            None,
            details,
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        ctx.config_failed = True
        return Check(
            "config",
            FAIL,
            f"{path} cannot be read: {exc}",
            f"make {path} a file this user can read: no daemon starts until it loads",
            details,
        )
    try:
        # "utf-8-sig", as the loader decodes it (`config.read_config_file`):
        # a byte-order mark is dropped, so a file the daemon loads is not
        # reported here as one that does not parse.
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        ctx.config_failed = True
        return Check(
            "config",
            FAIL,
            f"{path} does not parse: {exc}",
            f"fix {path}: no daemon starts until it parses",
            details,
        )
    with _config_notices() as notices:
        try:
            loaded = _config.load_config(path)
        except Exception as exc:  # noqa: BLE001 - every load error is the report
            ctx.config_failed = True
            return Check(
                "config",
                FAIL,
                f"{path} does not load: {exc}",
                f"fix the value it names in {path}: no daemon starts until it loads",
                details,
            )
    ctx.config = loaded
    removed = removed_keys(data)
    others = [
        message for message in notices if " was removed in bettermemory " not in message
    ]
    details["removed_keys"] = removed
    details["notices"] = others
    findings: list[Finding] = []
    if removed:
        findings.append(
            Finding(
                WARN,
                f"{path} sets {len(removed)} removed key(s): {'; '.join(removed)}",
                f"delete those lines from {path}; they are ignored",
            )
        )
    for message in others:
        findings.append(Finding(WARN, message, f"correct the value in {path}"))
    return _from_findings(
        "config", findings, ok_summary=f"{path} loads", details=details
    )


# ---------------------------------------------------------------------------
# store_location
# ---------------------------------------------------------------------------


def _store_rule(
    config: Config, directory: Path, cwd: Path | None, env_value: str | None
) -> str:
    """Which rule chose `directory`, read back from the result rather than
    re-derived, so a rule the loader gains (or skips) is not misnamed: the
    environment variable and `[storage] directory` decide whenever they are
    set, the home default when the result is that directory, the cwd's
    `.claude-memory` when it is that one. Home is asked before the cwd:
    run from the home directory, the cwd rule finds the home store itself,
    and naming that the repository-store risk would warn on the default
    install from the directory a terminal opens in."""
    from .config import GLOBAL_DIR_NAME, PROJECT_DIR_NAME

    if env_value:
        return "env"
    if config.storage.directory:
        return "config"
    with contextlib.suppress(OSError, RuntimeError):
        if directory == (Path.home() / GLOBAL_DIR_NAME).resolve():
            return "home"
    if cwd is not None:
        with contextlib.suppress(OSError):
            if directory == (cwd / PROJECT_DIR_NAME).resolve():
                return "cwd"
    return "other"


def check_store_location(ctx: _Context) -> Check:
    """Which directory the store lives in and which rule chose it. The
    cwd's `.claude-memory` is a warning whatever else is true: any
    repository can ship that directory, and every session started in the
    checkout then reads and writes the repository's store."""
    from .config import ENV_DIR_OVERRIDE, PROJECT_DIR_NAME, Config

    env_value = os.environ.get(ENV_DIR_OVERRIDE) or None
    if ctx.config is None and env_value is None:
        return _not_checked(
            "store_location",
            "the config does not load, and the store's directory depends on it",
            "fix the config first",
        )
    config = ctx.config if ctx.config is not None else Config()
    with _config_notices() as notices:
        directory = config.resolved_directory(ctx.cwd)
    ctx.directory = directory
    rule = _store_rule(config, directory, ctx.cwd, env_value)
    details: dict[str, Any] = {
        "directory": str(directory),
        "rule": rule,
        "cwd": None if ctx.cwd is None else str(ctx.cwd),
    }
    if rule == "env":
        summary = f"{ENV_DIR_OVERRIDE} chose {directory}"
    elif rule == "config":
        summary = f"[storage] directory in {config.config_path} chose {directory}"
    elif rule == "home":
        summary = f"the home default chose {directory}"
    else:
        summary = f"the store's directory resolves to {directory}"
    findings: list[Finding] = []
    if directory.exists() and not directory.is_dir():
        # The checks after this one would read a regular file here as "no
        # store yet", the first-run state; it is the state no store can
        # ever be created in.
        ctx.directory = None
        findings.append(
            Finding(
                FAIL,
                f"{directory} is a file, not a directory: no store can be created there",
                "move the file aside, or point the store elsewhere",
            )
        )
    if rule == "cwd":
        findings.append(
            Finding(
                WARN,
                f"./{PROJECT_DIR_NAME} in the working directory chose {directory}: "
                "a repository that ships this directory becomes the store of "
                "every session started in it",
                f"if you did not create it, remove it or pin the store with "
                f"{ENV_DIR_OVERRIDE} or [storage] directory; if you did, keep it "
                "out of version control",
            )
        )
    for message in notices:
        findings.append(
            Finding(WARN, message, "point the store at a directory you own")
        )
    return _from_findings(
        "store_location", findings, ok_summary=summary, details=details
    )


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


def _lock_held(path: Path) -> bool:
    """Whether another process holds the daemon's store lock. Taken for an
    instant and given back, as `_daemon_client` checks it; a lock file that
    does not exist is held by nobody and is not created here."""
    from . import _daemon_client as _dc

    if not path.is_file():
        return False
    try:
        handle = _dc.try_lock(path)
    except OSError:
        return False
    if handle is None:
        return True
    _dc.release_lock(handle)
    return False


def _await_state_file(state_file: Path, lock_path: Path) -> dict[str, Any] | None:
    """The state file of a daemon that holds the store's lock and has not
    written one yet, waited for up to `DAEMON_START_GRACE_SECONDS`. A
    daemon takes the lock first and writes its state file only once its
    port is bound (`daemon.serve`), so a start caught between the two
    holds the lock with no file; the wait ends when the file appears,
    when the lock is given back, or at the deadline."""
    from . import _daemon_client as _dc

    deadline = time.monotonic() + DAEMON_START_GRACE_SECONDS
    while True:
        state = _dc.read_state_file(state_file)
        if state is not None or state_file.exists() or not _lock_held(lock_path):
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(_GRACE_POLL_SECONDS)


def check_daemon(ctx: _Context) -> Check:
    """The daemon for this store: its state file, whether it answers
    `/health` with the proof that it holds the state file's token (as
    `status`, `down` and every hook require), whether the store it serves
    is this one, and its version against this CLI's. The state file is
    read and never removed, and a
    daemon is neither started nor stopped: `bettermemory status` and
    `ensure_daemon` act on what they find, and this reports it."""
    from . import _daemon_client as _dc
    from .store import STORE_FILENAME

    if ctx.directory is None:
        return _not_checked(
            "daemon", "the store's directory is not known", "fix the config first"
        )
    store_path = ctx.directory / STORE_FILENAME
    state_dir = _dc.resolve_state_dir()
    state_file = _dc.state_file_for(state_dir, store_path)
    lock_path = _dc.lock_file_for(state_dir, store_path)
    details: dict[str, Any] = {
        "state_file": str(state_file),
        "log_file": str(_dc.log_file_for(state_dir, store_path)),
        "lock_file": str(lock_path),
    }
    state = _dc.read_state_file(state_file)
    if state is None and not state_file.exists() and _lock_held(lock_path):
        state = _await_state_file(state_file, lock_path)
    if state is None:
        if state_file.exists():
            return Check(
                "daemon",
                WARN,
                f"the state file {state_file} cannot be read",
                "`bettermemory up` starts a daemon and replaces it",
                details,
            )
        if _lock_held(lock_path):
            return Check(
                "daemon",
                FAIL,
                f"a process holds the store's daemon lock ({lock_path}) but no "
                "state file names it: no client can reach it, and no other "
                "daemon can start",
                "if a daemon is still starting (the first start of an upgraded "
                "store migrates it before it binds its port), wait and run doctor "
                f"again; otherwise stop the process holding {lock_path}, then "
                "`bettermemory up`",
                details,
            )
        return Check(
            "daemon",
            OK,
            "not running; the first session or hook starts it",
            None,
            details,
        )
    pid, port = int(state["pid"]), int(state["port"])
    details.update(
        {
            "pid": pid,
            "port": port,
            "version": state.get("version"),
            "started": state.get("started"),
        }
    )
    # The proof every client checks before it posts the token
    # (`_daemon_client.identify`): a reply that repeats the state file's
    # store and pid without the token's MAC over a fresh nonce is not the
    # daemon, however it reads, and `status --check` calls it not ours.
    answer, proven = _dc.identify(state, timeout=DAEMON_PROBE_TIMEOUT_SECONDS)
    if answer is None:
        if _dc.pid_alive(pid) and _lock_held(lock_path):
            return Check(
                "daemon",
                WARN,
                f"state present, daemon not answering (pid {pid} holds the lock)",
                "it may be starting or busy; if this persists, `bettermemory down` "
                "and then `bettermemory up`",
                details,
            )
        return Check(
            "daemon",
            WARN,
            f"stale state file: pid {pid} is not serving on port {port}",
            "`bettermemory up` starts a daemon and replaces the file",
            details,
        )
    served = answer.get("store")
    details["served_store"] = served
    if not proven or served != str(store_path):
        # Nothing the listener sent is quoted: it is not this daemon, and
        # its reply is its own to make up.
        return Check(
            "daemon",
            FAIL,
            f"port {port} answers, but not as the daemon of {store_path}: the "
            "reply cannot prove it holds this daemon's token (the state file's)",
            f"stop the process on port {port}, or move {state_file} aside; then "
            "`bettermemory up`",
            details,
        )
    theirs = answer.get("version")
    mine = _dc.package_version()
    details["daemon_version"] = theirs
    details["cli_version"] = mine
    if theirs == mine:
        return Check(
            "daemon",
            OK,
            f"running: pid {pid}, port {port}, version {theirs}",
            None,
            details,
        )
    their_key, my_key = _dc.version_key(theirs), _dc.version_key(mine)
    if their_key is None or my_key is None:
        return Check(
            "daemon",
            WARN,
            f"the daemon's version {theirs!r} cannot be compared with this CLI's {mine}",
            "`bettermemory down`; the next session starts a daemon of this version",
            details,
        )
    if their_key < my_key:
        return Check(
            "daemon",
            WARN,
            f"the daemon runs {theirs}, older than this CLI's {mine}",
            "`bettermemory down`; the next session or hook starts this version",
            details,
        )
    return Check(
        "daemon",
        WARN,
        f"the daemon runs {theirs}, newer than this CLI's {mine}: another install "
        "on this machine is newer",
        "upgrade this install, or run doctor from the newer one",
        details,
    )


# ---------------------------------------------------------------------------
# claude_code and clients
# ---------------------------------------------------------------------------


def _command_words(command: str) -> list[str]:
    """The words of a shell command up to its first operator (`||`, `;`,
    `&&`, a pipe), leading `NAME=value` assignments dropped: the program a
    hook runs and its arguments."""
    posix = sys.platform != "win32"
    lexer = shlex.shlex(command, posix=posix, punctuation_chars=True)
    lexer.whitespace_split = True
    words: list[str] = []
    try:
        for token in lexer:
            if token and all(ch in "();<>|&" for ch in token):
                break
            if not posix and len(token) >= 2 and token[0] == token[-1] == '"':
                token = token[1:-1]
            words.append(token)
    except ValueError:
        words = command.split()
    while words and "=" in words[0] and words[0].split("=", 1)[0].isidentifier():
        words.pop(0)
    return words


def launches_bettermemory(command: object, args: object, binary: str) -> bool:
    """Whether a client entry or a hook command runs bettermemory: the
    shapes `init.command_launches_bettermemory` recognises, plus the two it
    cannot, because they no longer run anything: an absolute path to a
    `bettermemory` binary that is gone, and `python -m bettermemory`."""
    from .init import command_launches_bettermemory

    if not isinstance(command, str):
        return False
    if command_launches_bettermemory(command, args, binary):
        return True
    if Path(command).stem.lower() == "bettermemory":
        return True
    return isinstance(args, list) and args[:2] == ["-m", "bettermemory"]


def launcher_problem(command: str, *, what: str, remedy: str) -> Finding | None:
    """What is wrong with the program an entry or a hook runs, or None:
    an absolute path that no longer exists, or a bare name (`bettermemory`,
    `uvx`) PATH does not find, each fails silently at the client, since
    every shipped hook ends in `|| true`. An absolute `bettermemory` of
    another install is a warning: it runs, at that install's version."""
    path = Path(command)
    if path.is_absolute():
        if not path.exists():
            return Finding(FAIL, f"{what} runs {command}, which does not exist", remedy)
        mine = this_install_binary()
        if (
            mine is not None
            and path.stem.lower() == "bettermemory"
            and not _same_binary(path, mine)
        ):
            return Finding(
                WARN,
                f"{what} runs {command}, another install than this one ({mine})",
                remedy,
            )
        return None
    if shutil.which(command) is None:
        return Finding(FAIL, f"{what} runs `{command}`, which is not on PATH", remedy)
    return None


def _hooks_copy() -> str:
    """The remedy that copies the plugin's hooks by hand, naming the user
    settings file Claude Code reads here (`claude_paths.user_settings_path`)."""
    return (
        "copy the three hooks of the plugin's hooks/hooks.json into "
        f"{claude_paths.user_settings_path()}"
    )


def _hook_event_word(words: Sequence[str]) -> bool:
    """Whether a word after the binary makes the command a hook: `hook
    <event>` or one of the three 8.x aliases, the words `_entry` routes to
    the hook client (`_daemon_client.HOOK_WORDS`, read from there so the
    two never name different sets)."""
    from ._daemon_client import HOOK_WORDS

    return any(word in HOOK_WORDS for word in words[1:])


def _hook_commands(hooks: object) -> Iterator[tuple[str, str]]:
    """(event, command) for every bettermemory hook a settings file's
    `hooks` block wires: `{event: [{matcher?, hooks: [{type, command}]}]}`."""
    from .init import find_binary

    if not isinstance(hooks, Mapping):
        return
    binary = find_binary()
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, Mapping) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                command = entry.get("command") if isinstance(entry, Mapping) else None
                if not isinstance(command, str):
                    continue
                words = _command_words(command)
                if (
                    words
                    and launches_bettermemory(words[0], words[1:], binary)
                    and _hook_event_word(words)
                ):
                    yield event, command


def _read_json_object(path: Path, *, bounded: bool = True) -> dict[str, Any]:
    """A JSON file's root object; {} for an empty file or a non-object
    root. Raises OSError or ValueError when it cannot be read or parsed.

    Bounded (the default), it is read through Claude Code's one bounded
    reader (`claude_paths.read_settings_text`), which the audit reads the
    same files through: a settings file and a project's ``.mcp.json`` are
    whatever the repository doctor runs in ships, and one over the cap is
    refused unread. Only the global config is read whole: Claude Code
    alone writes it, and it grows with the state kept per project."""
    text = (
        claude_paths.read_settings_text(path)
        if bounded
        else path.read_text(encoding="utf-8")
    )
    data = json.loads(text) if text.strip() else {}
    return data if isinstance(data, dict) else {}


def _claude_registrations(cwd: Path | None, binary: str) -> list[str]:
    """Where Claude Code registers a bettermemory MCP server outside the
    plugin: the user scope (the global config's `mcpServers`, in
    ``$CLAUDE_CONFIG_DIR`` when it is set: `claude_paths.global_config_path`),
    the local scope (its `projects[<cwd>].mcpServers`) and the project
    scope (`./.mcp.json`)."""
    found: list[str] = []
    user_json = claude_paths.global_config_path()
    sources: list[tuple[str, object]] = []
    with contextlib.suppress(OSError, ValueError):
        data = _read_json_object(user_json, bounded=False)
        sources.append((str(user_json), data.get("mcpServers")))
        projects = data.get("projects")
        if cwd is not None and isinstance(projects, Mapping):
            project = projects.get(str(cwd))
            if isinstance(project, Mapping):
                sources.append(
                    (f"{user_json} (local scope)", project.get("mcpServers"))
                )
    if cwd is not None:
        with contextlib.suppress(OSError, ValueError):
            mcp_json = claude_paths.project_mcp_path(cwd)
            sources.append(
                (str(mcp_json), _read_json_object(mcp_json).get("mcpServers"))
            )
    for where, servers in sources:
        if not isinstance(servers, Mapping):
            continue
        if any(
            isinstance(entry, Mapping)
            and launches_bettermemory(entry.get("command"), entry.get("args"), binary)
            for entry in servers.values()
        ):
            found.append(where)
    return found


def check_claude_code(ctx: _Context) -> Check:
    """Claude Code's wiring: the plugin enabled, or the three hooks in a
    settings file, each running a bettermemory this machine has, and an
    MCP server registered. The hooks carry the session brief, the prompt
    recall and the turn-end settlement, and every shipped hook ends in
    `|| true`, so a hook that runs nothing fails silently: this is the
    one place that says so."""
    from .init import find_binary

    # The configuration directory and the global config move together
    # under CLAUDE_CONFIG_DIR (`claude_paths`); the project files do not.
    claude_dir = claude_paths.config_dir()
    global_config = claude_paths.global_config_path()
    if not claude_dir.is_dir() and not global_config.is_file():
        return Check(
            "claude_code",
            OK,
            f"Claude Code not found (no {claude_dir} or {global_config})",
            None,
            {"found": False, "config_dir": str(claude_dir)},
        )
    settings = [claude_paths.user_settings_path()]
    if ctx.cwd is not None:
        settings += list(claude_paths.project_settings_paths(ctx.cwd))
    findings: list[Finding] = []
    plugin: bool | None = None
    plugin_source: str | None = None
    hooks: dict[str, list[str]] = {}
    hook_sources: list[str] = []
    for path in settings:
        # A path that is there but no regular file (a FIFO, a link to a
        # terminal) is read, and refused by the bounded reader with a
        # finding, rather than passed over in silence.
        if not path.exists():
            continue
        try:
            data = _read_json_object(path)
        except (OSError, ValueError) as exc:
            findings.append(
                Finding(
                    WARN,
                    f"{path} cannot be read: {exc}",
                    f"fix {path}; Claude Code ignores a settings file it cannot parse",
                )
            )
            continue
        enabled = data.get("enabledPlugins")
        if isinstance(enabled, Mapping):
            # Later files override earlier ones, user < project < local,
            # as Claude Code layers them.
            for key, value in enabled.items():
                if (
                    isinstance(key, str)
                    and key.split("@", 1)[0] == PLUGIN_NAME
                    and isinstance(value, bool)
                ):
                    plugin, plugin_source = value, str(path)
        for event, command in _hook_commands(data.get("hooks")):
            hooks.setdefault(event, []).append(command)
            if str(path) not in hook_sources:
                hook_sources.append(str(path))
    binary = find_binary()
    registrations = _claude_registrations(ctx.cwd, binary)
    details: dict[str, Any] = {
        "found": True,
        "config_dir": str(claude_dir),
        "plugin_enabled": plugin is True,
        "plugin_source": plugin_source,
        "hooks": hooks,
        "hook_sources": hook_sources,
        "mcp_registrations": registrations,
    }
    if plugin and shutil.which("uvx") is None:
        findings.append(
            Finding(
                FAIL,
                "the plugin's hooks and MCP server run `uvx bettermemory`, and "
                "`uvx` is not on PATH",
                "install uv (https://docs.astral.sh/uv/) so `uvx` is on PATH",
            )
        )
    for event, commands in hooks.items():
        for command in commands:
            words = _command_words(command)
            # A hook runs through a shell, which expands `~` and `$HOME`
            # in the program word; an MCP entry is spawned directly and
            # does not, so only the hooks expand here.
            program = os.path.expandvars(os.path.expanduser(words[0]))
            problem = launcher_problem(
                program,
                what=f"the {event} hook",
                remedy=(
                    f"point the hook at an installed bettermemory, or replace the "
                    f"settings hooks with the plugin ({_PLUGIN_INSTALL})"
                ),
            )
            if problem is not None:
                findings.append(problem)
    if plugin and hooks:
        findings.append(
            Finding(
                WARN,
                "the plugin and settings hooks both run bettermemory: each hook "
                "event fires twice",
                f"remove the bettermemory hooks from {', '.join(hook_sources)}; "
                "the plugin carries them",
            )
        )
    elif not plugin and not hooks:
        findings.append(
            Finding(
                WARN,
                "no bettermemory hooks in Claude Code: the session brief, the "
                "prompt recall and the turn-end settlement are off",
                f"install the plugin ({_PLUGIN_INSTALL}), or {_hooks_copy()}",
            )
        )
    elif not plugin:
        missing = [event for event in CLAUDE_HOOK_EVENTS if event not in hooks]
        if missing:
            findings.append(
                Finding(
                    WARN,
                    f"the settings hooks cover {', '.join(sorted(hooks))} but not "
                    f"{', '.join(missing)}",
                    _hooks_copy(),
                )
            )
    if not plugin and not registrations:
        findings.append(
            Finding(
                WARN,
                "Claude Code has no bettermemory MCP server registered",
                f"install the plugin ({_PLUGIN_INSTALL}), or run "
                "`bettermemory init --client claude-code`",
            )
        )
    if plugin:
        ok_summary = f"the plugin is enabled ({plugin_source})"
    else:
        ok_summary = (
            f"settings hooks for {', '.join(sorted(hooks))} and an MCP server "
            f"in {registrations[0] if registrations else 'no file'}"
        )
    return _from_findings(
        "claude_code", findings, ok_summary=ok_summary, details=details
    )


def check_clients(ctx: _Context) -> Check:
    """Every MCP client config `init` knows that exists here: whether it
    parses, and whether each bettermemory entry in it still runs an
    installed binary. A config with no bettermemory entry is reported in
    the details and is not a fault: the user may not use that client
    with bettermemory."""
    from .init import (
        DEFAULT_SERVER_NAME,
        KNOWN_CLIENTS,
        LEGACY_SERVER_NAME,
        find_binary,
        read_server_entries,
    )

    binary = find_binary()
    findings: list[Finding] = []
    files: list[dict[str, Any]] = []
    registered = 0
    for name, getter in KNOWN_CLIENTS.items():
        try:
            client = getter()
        except OSError:
            continue
        for index, path in enumerate(dict.fromkeys(client.paths)):
            # As in the claude_code check: a path that is there but no
            # regular file is named as unreadable (`read_server_entries`).
            if not path.exists():
                continue
            init_command = f"`bettermemory init --client {name}" + (
                "`" if index == 0 else f" --config-path {path}`"
            )
            # A project-scoped path (`init.ClientPaths.project_paths`) is in
            # the working directory, a repository's file: read through the
            # bounded reader the claude_code check uses.
            cap = (
                claude_paths.SETTINGS_CAP_BYTES
                if path in client.project_paths
                else None
            )
            try:
                servers = read_server_entries(
                    path, config_format=client.format, max_bytes=cap
                )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                files.append({"client": name, "path": str(path), "entries": None})
                findings.append(
                    Finding(
                        WARN,
                        f"{name}: {path} cannot be read: {exc}",
                        f"fix {path} by hand; init does not patch a file it "
                        "cannot parse",
                    )
                )
                continue
            commands = {
                key: entry["command"]
                for key, entry in servers.items()
                if isinstance(entry, Mapping)
                and isinstance(entry.get("command"), str)
                and launches_bettermemory(entry["command"], entry.get("args"), binary)
            }
            ours = list(commands)
            files.append({"client": name, "path": str(path), "entries": ours})
            registered += len(ours)
            for key, command in commands.items():
                problem = launcher_problem(
                    command,
                    what=f"{name}'s `{key}` entry in {path}",
                    remedy=f"{init_command} rewrites it",
                )
                if problem is not None:
                    findings.append(problem)
            if DEFAULT_SERVER_NAME in ours and LEGACY_SERVER_NAME in ours:
                findings.append(
                    Finding(
                        WARN,
                        f"{name}: {path} registers bettermemory twice, as "
                        f"`{DEFAULT_SERVER_NAME}` and the 1.0 name "
                        f"`{LEGACY_SERVER_NAME}`",
                        f"delete the `{LEGACY_SERVER_NAME}` entry, or run {init_command}",
                    )
                )
            if name == "continue" and ours:
                findings.append(
                    Finding(
                        WARN,
                        f"continue: {path} uses the legacy object shape, which "
                        "current Continue ignores",
                        "add bettermemory to ~/.continue/config.yaml as a list "
                        "entry (docs/clients.md, Continue)",
                    )
                )
    details: dict[str, Any] = {"binary": binary, "files": files}
    if not files:
        ok_summary = "no MCP client config file found"
    else:
        ok_summary = (
            f"{registered} bettermemory entr{'y' if registered == 1 else 'ies'} "
            f"in {len(files)} client config file(s), each runnable"
        )
    return _from_findings("clients", findings, ok_summary=ok_summary, details=details)


# ---------------------------------------------------------------------------
# disk and the posture pointer
# ---------------------------------------------------------------------------


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - the loop always returns


def check_disk(ctx: _Context) -> Check:
    """Free space on the filesystem that holds the store (the nearest
    directory that exists, for a store not yet created)."""
    from .store import STORE_FILENAME

    if ctx.directory is None:
        return _not_checked(
            "disk", "the store's directory is not known", "fix the config first"
        )
    probe = ctx.directory
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    store_bytes = 0
    for name in (STORE_FILENAME, STORE_FILENAME + "-wal"):
        with contextlib.suppress(OSError):
            store_bytes += (ctx.directory / name).stat().st_size
    need = max(DISK_WARN_BYTES, 2 * store_bytes)
    details: dict[str, Any] = {
        "path": str(probe),
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "store_bytes": store_bytes,
    }
    if usage.free < DISK_FAIL_BYTES:
        return Check(
            "disk",
            FAIL,
            f"{_human(usage.free)} free at {probe}: a write can fail",
            "free space on that filesystem",
            details,
        )
    if usage.free < need:
        return Check(
            "disk",
            WARN,
            f"{_human(usage.free)} free at {probe}, under {_human(need)}",
            "free space on that filesystem before the store needs it",
            details,
        )
    return Check("disk", OK, f"{_human(usage.free)} free at {probe}", None, details)


def check_security() -> Check:
    """The pointer to the posture audit, which owns what doctor leaves out:
    the modes and owners of the store, key, state and log files, a store
    inside a repository or a synced folder, and the sandbox settings."""
    return Check(
        "security",
        OK,
        "file modes, owners and the sandbox settings: run `bettermemory security audit`",
        None,
        {"command": "bettermemory security audit"},
    )


def check_orphan_keys(
    ctx: _Context, *, clear: bool = False, apply: bool = False
) -> Check:
    """Key rings in the keys directory whose store is shown gone
    (`key_lifecycle.survey_rings`): a warning naming `--clear-orphan-keys`.
    With ``clear`` the clear's plan goes under ``details["clear"]``; with
    ``apply`` too, those rings are moved to the quarantine directory first
    (`key_lifecycle.clear_orphan_rings`; nothing is deleted) and the survey
    reads what is left. A ring whose store is not shown gone, or that names
    no store file, is kept and counted; the ring of the store this run
    checks is in use. Each bound path is read within
    `location_git.PROBE_BUDGET`, so a share that hangs does not hold doctor."""
    from .key_lifecycle import clear_orphan_rings, survey_rings
    from .store import STORE_FILENAME

    current = None if ctx.directory is None else ctx.directory / STORE_FILENAME
    details: dict[str, Any] = {}
    if clear:
        details["clear"] = clear_orphan_rings(current, apply=apply)
    survey = survey_rings(current)
    details.update(survey.to_dict())
    remedy = survey.remedy()
    return Check(
        "orphan_keys", OK if remedy is None else WARN, survey.summary(), remedy, details
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _guarded(check_ids: Sequence[str], run: Callable[[], list[Check]]) -> list[Check]:
    """`run`'s checks, or one failure per id when it raised: a doctor that
    stopped at the first unexpected state would hide every later check.
    Two kinds are the machine's answer, not doctor's fault, and are named
    as such: an OSError (`Path.exists` and `is_file` re-raise EACCES on
    3.11 to 3.13, so a store under a directory this user cannot search
    lands here) and an ImportError (an install that lost a dependency,
    which the install check reports too)."""
    try:
        return run()
    except OSError as exc:
        if exc.filename:
            summary = f"the check could not read {exc.filename}"
            remedy = (
                "make that path readable by this user, or point bettermemory "
                "at one that is"
            )
        else:
            summary = "the check could not read what it needs"
            remedy = (
                "check that this user can read the store, keys and state directories"
            )
        return [
            Check(
                check_id,
                FAIL,
                f"{summary}: {type(exc).__name__}: {exc}",
                remedy,
            )
            for check_id in check_ids
        ]
    except ImportError as exc:
        return [
            Check(
                check_id,
                FAIL,
                f"the check could not import {exc.name or 'a module'}: {exc}",
                _REINSTALL_REMEDY,
            )
            for check_id in check_ids
        ]
    except Exception as exc:  # noqa: BLE001 - the report is the error path
        return [
            Check(
                check_id,
                FAIL,
                f"the check itself failed: {type(exc).__name__}: {exc}",
                _BUG_REMEDY,
            )
            for check_id in check_ids
        ]


def _safe_cwd() -> Path | None:
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


def run_checks(
    *, cwd: Path | None = None, clear_orphan_keys: bool = False, yes: bool = False
) -> list[Check]:
    """Every check, in `CHECK_IDS` order. `cwd` is the directory the store
    rule and Claude Code's project files (`.claude/settings*.json`,
    `.mcp.json`, the local scope in the global config) are read from
    (default: this process's). The `clients` check reads the paths
    `init.KNOWN_CLIENTS` names, whose project-scoped ones `init` places at
    the process's own cwd. ``clear_orphan_keys`` adds the plan of moving
    the key rings whose store is shown gone to the quarantine, at the
    `orphan_keys` check, and ``yes`` moves them."""
    ctx = _Context(cwd=_safe_cwd() if cwd is None else cwd)
    checks: list[Check] = []
    checks += _guarded(("install",), lambda: [check_install()])
    checks += _guarded(("config",), lambda: [check_config(ctx)])
    checks += _guarded(("store_location",), lambda: [check_store_location(ctx)])
    checks += _guarded(("restore",), lambda: [check_restore(ctx)])
    checks += _guarded(("store", "key"), lambda: check_store(ctx))
    checks += _guarded(("index",), lambda: [check_index(ctx)])
    checks += _guarded(("daemon",), lambda: [check_daemon(ctx)])
    checks += _guarded(("claude_code",), lambda: [check_claude_code(ctx)])
    checks += _guarded(("clients",), lambda: [check_clients(ctx)])
    checks += _guarded(("disk",), lambda: [check_disk(ctx)])
    checks += _guarded(("security",), lambda: [check_security()])
    checks += _guarded(
        ("orphan_keys",),
        lambda: [check_orphan_keys(ctx, clear=clear_orphan_keys, apply=yes)],
    )
    return checks


def overall_status(checks: Sequence[Check]) -> str:
    return _worst([check.status for check in checks])


def report(checks: Sequence[Check]) -> dict[str, Any]:
    """The `--json` document: `{checks: [{id, status, summary, remedy,
    details}], status}`."""
    return {
        "checks": [check.to_dict() for check in checks],
        "status": overall_status(checks),
    }


def exit_code(status: str) -> int:
    return EXIT_CODES[status]


def render_text(document: Mapping[str, Any]) -> str:
    """The text form of a `report`: one line per check, its remedy under
    it, and the other findings of a check that has several."""
    checks: list[Mapping[str, Any]] = list(document["checks"])
    counts = {status: 0 for status in (OK, WARN, FAIL)}
    for check in checks:
        counts[check["status"]] += 1
    lines = [
        f"bettermemory doctor: {document['status']} ({counts[OK]} ok, "
        f"{counts[WARN]} warn, {counts[FAIL]} fail)"
    ]
    width = max((len(str(check["id"])) for check in checks), default=0)
    indent = " " * (2 + 4 + 2 + width + 2)
    for check in checks:
        lines.append(
            f"  {check['status']:<4}  {check['id']:<{width}}  {check['summary']}"
        )
        if check["remedy"]:
            lines.append(f"{indent}fix: {check['remedy']}")
        raised = [
            finding
            for finding in check["details"].get("findings", [])
            if finding["status"] != OK
        ]
        # The check's own line carries the first finding of its worst
        # status (`_from_findings`), which need not be the first raised:
        # a warning read before a failure stays above it in the list.
        headline = next(
            (
                index
                for index, finding in enumerate(raised)
                if finding["status"] == check["status"]
            ),
            None,
        )
        for index, finding in enumerate(raised):
            if index == headline:
                continue
            lines.append(f"{indent}{finding['status']}: {finding['summary']}")
            if finding["remedy"]:
                lines.append(f"{indent}  fix: {finding['remedy']}")
    # Summaries quote what doctor read: paths, config values, a daemon's
    # version. None of it reaches the terminal as an escape sequence.
    from .history import printable

    return "\n".join(printable(line) for line in lines) + "\n"


__all__ = [
    "CHECK_IDS",
    "EXIT_CODES",
    "FAIL",
    "OK",
    "WARN",
    "Check",
    "Finding",
    "exit_code",
    "install_method",
    "overall_status",
    "removed_keys",
    "render_text",
    "report",
    "run_checks",
]
