"""The daemon's client side: the state file, the health probe, starting and
stopping a daemon, and the three hook commands.

Standard library only, on purpose. A hook is a process the harness spawns
on every turn, so the whole cost of this module is what the hook pays on
top of the interpreter: `import json, urllib.request, socket` measures
about 26 ms on the author's machine against 316 ms for `import mcp`.
`_entry.main` dispatches the hook words to `hook_main` before anything
heavier is imported, and `tests/test_hook_client.py` checks the path with
`-X importtime`. Each hook posts where its caller stands (the probe of
`_location_probe`, design 8.1), a module on ``errno``, ``os`` and
``socket`` alone; where the daemon cannot read that path, its reply asks,
and the client captures its location in its own namespace, writes the
checkout token there, and answers the daemon's questions about its
repository from its own files (`_membership_rounds`, design 8.7).
`location` and the modules it imports (`checkout_token`, `project_file`,
`gitobjects`, `githead`) are standard library only, and they and
`machine` are imported only in that exchange: a hook whose reply asks
nothing loads none of them.

One daemon per store. The state file is named after the store it serves
(`daemon-<sha256(store path)[:16]>.json` under the state directory), so a
project with its own `.claude-memory` gets its own daemon and the CLI,
the shim and the hooks all find the one that serves the store they
resolved. The state directory is `BETTERMEMORY_STATE_DIR` when set, else
the user's state directory; every test and bench sets the variable so a
daemon it starts stays under its own directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

STATE_DIR_ENV = "BETTERMEMORY_STATE_DIR"
DEFAULT_PORT = 7397
HOST = "127.0.0.1"
HEALTH_PATH = "/health"
SHUTDOWN_PATH = "/api/v1/admin/shutdown"
HOOK_PATH = "/api/v1/hook/"

#: The hook words `_entry.main` routes here. The three 8.x names are
#: aliases of the events the daemon serves, so an installed hooks.json
#: keeps working across the 9.0.0 upgrade.
HOOK_WORDS: frozenset[str] = frozenset(
    {"hook", "session-start", "audit-turn", "prompt-recall"}
)
HOOK_EVENTS: tuple[str, ...] = ("session-start", "stop", "prompt")
_ALIASES = {
    "session-start": "session-start",
    "audit-turn": "stop",
    "prompt-recall": "prompt",
}

#: The store's file name, `config.STORE_FILENAME` spelled here so a hook
#: can name the store without importing the config module;
#: tests/test_hook_client.py pins the two equal.
STORE_FILENAME = "memory.sqlite"

START_TIMEOUT_SECONDS = 8.0
HOOK_TIMEOUT_SECONDS = 15.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.02
_STDIN_CAP_BYTES = 1 << 20
_STATE_KEYS = ("pid", "port", "token", "version", "store", "started")

#: The exit status of a daemon that found its store already served: another
#: process holds the store's lock (`acquire_lock`). EX_TEMPFAIL, so it reads
#: apart from a daemon that failed to start (status 1, or whatever the
#: interpreter exits with).
DAEMON_HELD_STATUS = 75
#: How long a starting daemon waits for the store's lock before it gives
#: up: long enough for a daemon just asked to stop (a version replaced, a
#: `down`) to finish exiting, short enough that a second daemon started
#: beside a serving one leaves promptly.
LOCK_WAIT_SECONDS = 2.0


# ---------------------------------------------------------------------------
# The state file
# ---------------------------------------------------------------------------


def resolve_state_dir() -> Path:
    """`BETTERMEMORY_STATE_DIR` when set, else the user's state directory."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    import platformdirs

    return Path(platformdirs.user_state_dir("bettermemory"))


def state_file_for(state_dir: Path, store_path: Path | str) -> Path:
    """The state file of the daemon that serves `store_path`."""
    digest = hashlib.sha256(str(store_path).encode("utf-8")).hexdigest()[:16]
    return Path(state_dir) / f"daemon-{digest}.json"


def log_file_for(state_dir: Path, store_path: Path | str) -> Path:
    return state_file_for(state_dir, store_path).with_suffix(".log")


def read_state(state_dir: Path, store_path: Path | str) -> dict[str, Any] | None:
    """The state file's contents, or None when it is absent or malformed."""
    return read_state_file(state_file_for(state_dir, store_path))


def read_state_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or any(key not in raw for key in _STATE_KEYS):
        return None
    if not isinstance(raw["pid"], int) or not isinstance(raw["port"], int):
        return None
    if not isinstance(raw["token"], str) or not raw["token"]:
        return None
    return raw


def write_state(state_dir: Path, state: dict[str, Any]) -> Path:
    """Write the state file for `state["store"]`, owner-only, atomically."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    path = state_file_for(state_dir, state["store"])
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    from ._fsutil import replace_atomic

    replace_atomic(tmp, path)
    return path


def remove_state(
    state_dir: Path, store_path: Path | str, *, pid: int | None = None
) -> None:
    """Remove the state file; with `pid`, only when the file names that pid."""
    path = state_file_for(state_dir, store_path)
    if pid is not None:
        current = read_state_file(path)
        if current is not None and current["pid"] != pid:
            return
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The store's locks
# ---------------------------------------------------------------------------


def lock_file_for(state_dir: Path, store_path: Path | str) -> Path:
    """The lock a daemon holds for its whole life while it serves
    `store_path`, beside its state file. The file is never removed: a
    process that unlinked it could let two lockers hold two inodes."""
    return state_file_for(state_dir, store_path).with_suffix(".lock")


def spawn_lock_file_for(state_dir: Path, store_path: Path | str) -> Path:
    """The lock a client holds while it starts a daemon for `store_path`
    and waits for it, so concurrent clients spawn one daemon, not one each."""
    return state_file_for(state_dir, store_path).with_suffix(".spawn.lock")


def try_lock(path: Path) -> Any | None:
    """An exclusive, non-blocking advisory lock on `path`: the open file
    that holds it, or None when another process (or another open of the
    file in this one) holds it. `flock` on POSIX, `msvcrt.locking` on one
    byte on Windows; either way the OS drops the lock with the process, so
    a lock never outlives its holder and needs no stale-lock handling.
    `release_lock` gives it back early."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 - held for the lock's life
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def acquire_lock(path: Path, *, wait: float) -> Any | None:
    """`try_lock`, retried for up to `wait` seconds: the held lock, or None
    when another process still holds it then."""
    deadline = time.monotonic() + wait
    while True:
        handle = try_lock(path)
        if handle is not None or time.monotonic() >= deadline:
            return handle
        time.sleep(_POLL_SECONDS)


def release_lock(handle: Any) -> None:
    """Give back a lock `try_lock` took."""
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Processes and the wire
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Whether a process with `pid` exists (not whether it is a daemon)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def health(port: int, *, timeout: float = 1.0) -> dict[str, Any] | None:
    """GET /health on the daemon at `port`, or None when nothing answers."""
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{port}{HEALTH_PATH}", timeout=timeout
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return body if isinstance(body, dict) and body.get("status") == "ok" else None


def post(
    port: int, token: str, path: str, payload: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    """POST `payload` as JSON with the bearer token; the reply as a dict.
    Raises `urllib.error.URLError` (or `HTTPError`) on any failure."""
    request = urllib.request.Request(
        f"http://{HOST}:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body if isinstance(body, dict) else {}


class Started(NamedTuple):
    """A daemon process this client spawned: the child, the log file its
    output goes to, and the size that log had before the child started, the
    offset at which the child's own lines begin."""

    child: subprocess.Popen[Any]
    log: Path
    log_offset: int


class DaemonExited(RuntimeError):
    """The daemon a client started exited before any daemon answered: a
    config that does not parse, a store that cannot be opened, anything
    that stops `bettermemory up --foreground` at start. `status` is its
    exit status, `error` the last line it wrote to its log (None when it
    wrote none), `log` that file."""

    def __init__(
        self, store_path: Path | str, status: int, error: str | None, log: Path
    ) -> None:
        self.status = status
        self.error = error
        self.log = log
        reason = f": {error}" if error else ""
        super().__init__(
            f"the daemon started for {store_path} exited with status {status} "
            f"before it answered{reason} (log {log})"
        )


# The most of a child's log read back for its last line.
_LOG_TAIL_BYTES = 1 << 16


def _last_log_line(started: Started) -> str | None:
    """The last non-empty line the child wrote to its log, None when it
    wrote none or the log cannot be read."""
    try:
        with open(started.log, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(started.log_offset, size - _LOG_TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return None
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        if line.strip():
            return line.strip()[:500]
    return None


def start_daemon(
    state_dir: Path,
    store_path: Path | str,
    *,
    port: int | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Spawn `bettermemory up --foreground` detached from this process and
    return the pid of the process spawned. That need not be the daemon's: a
    Windows venv's python.exe is a launcher that runs the interpreter as its
    child and waits for it, and the daemon writes its own pid to the state
    file, which is the one to act on. The child inherits `env` (default:
    this environment) with the state directory pinned, so it writes the
    state file where the caller will look for it."""
    return _spawn(state_dir, store_path, port=port, env=env).child.pid


def _spawn(
    state_dir: Path,
    store_path: Path | str,
    *,
    port: int | None,
    env: dict[str, str] | None,
) -> Started:
    """`start_daemon`, returning the child itself and where its log begins."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ if env is None else env)
    child_env[STATE_DIR_ENV] = str(state_dir)
    command = [sys.executable, "-m", "bettermemory", "up", "--foreground"]
    if port is not None:
        command += ["--port", str(port)]
    log_path = log_file_for(state_dir, store_path)
    log_handle = open(log_path, "ab")  # noqa: SIM115 - handed to the child
    try:
        offset = os.fstat(log_handle.fileno()).st_size
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": child_env,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()
    return Started(child=child, log=log_path, log_offset=offset)


def _answering(state_dir: Path, store_path: Path | str) -> dict[str, Any] | None:
    """The state of a daemon for `store_path` that answers /health."""
    state = read_state(state_dir, store_path)
    if state is not None and health(state["port"], timeout=0.5) is not None:
        return state
    return None


def wait_for_daemon(
    state_dir: Path,
    store_path: Path | str,
    *,
    timeout: float,
    started: Started | None = None,
) -> dict[str, Any] | None:
    """Poll the state file and /health until a daemon answers or `timeout`.

    With `started`, the child this client spawned is watched too: once it
    has exited and still no daemon answers (another client's daemon may
    have come up meanwhile, and is then the answer), the wait ends at once
    with `DaemonExited`, which carries the child's exit status and the
    last line of its log, instead of running out the timeout. A child that
    exited with `DAEMON_HELD_STATUS` found the store already served by a
    daemon that holds its lock, one still starting or busy, so the wait
    goes on for that daemon to answer."""
    deadline = time.monotonic() + timeout
    while True:
        state = _answering(state_dir, store_path)
        if state is not None:
            return state
        if started is not None and started.child.poll() is not None:
            state = _answering(state_dir, store_path)
            if state is not None:
                return state
            if started.child.returncode == DAEMON_HELD_STATUS:
                started = None
                continue
            raise DaemonExited(
                store_path,
                started.child.returncode,
                _last_log_line(started),
                started.log,
            )
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)


def shutdown_daemon(
    state: dict[str, Any], *, timeout: float = SHUTDOWN_TIMEOUT_SECONDS
) -> bool:
    """Ask the daemon in `state` to stop, then make sure it did. The
    shutdown endpoint is the mechanism; a signal is the fallback."""
    try:
        post(state["port"], state["token"], SHUTDOWN_PATH, {}, timeout=2.0)
    except (OSError, ValueError, urllib.error.URLError):
        pass
    pid = int(state["pid"])
    port = int(state["port"])
    if _wait_stopped(pid, port, timeout):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return _stopped(pid, port)
    return _wait_stopped(pid, port, timeout)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.2):
            return True
    except OSError:
        return False


def _stopped(pid: int, port: int) -> bool:
    """A daemon is stopped when its process is gone or its port is
    closed. The second clause covers a foreground daemon whose parent
    has not reaped it yet: a zombie still answers `os.kill(pid, 0)`
    but holds no socket."""
    return not pid_alive(pid) or not _port_open(port)


def _wait_stopped(pid: int, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not _stopped(pid, port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)
    return True


_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?")
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}


def version_key(text: object) -> tuple[int, ...] | None:
    """`text` as a key that sorts in PEP 440 order for the shapes this
    project spells (`X.Y.Z`, `X.Y.ZrcN`, `X.Y.Z.devN`; `aN` and `bN` read
    the same way): a dev release before the pre-releases of its version,
    those before the final. None for anything else, a version this cannot
    place. `re` is the one module this costs, and `urllib.request` imports
    it already, so the hook path pays nothing for it."""
    if not isinstance(text, str):
        return None
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    major, minor, patch, pre, pre_n, dev = match.groups()
    if pre is not None:
        pre_key = (_PRE_RANK[pre], int(pre_n))
    elif dev is not None:
        pre_key = (-1, 0)
    else:
        pre_key = (3, 0)
    dev_key = (0, int(dev)) if dev is not None else (1, 0)
    return (int(major), int(minor), int(patch), *pre_key, *dev_key)


def _keeps(state: dict[str, Any], theirs: object, mine: str) -> bool:
    """Whether a client of version `mine` keeps the daemon in `state`,
    which reports version `theirs`: yes for the same version and for a
    newer one, with one log line for the newer; no for an older one and
    for one whose version `version_key` cannot read, which the caller
    then replaces. A newer daemon is kept and used because two installs
    at different versions on one store (the plugin's unpinned `uvx
    bettermemory` beside a `uv tool install` or a venv) would otherwise
    restart the daemon under each other, each flip costing the other
    client a failed call and the shutdown wait. `logging` is imported
    here, not at the top: only a kept newer daemon pays for it, and the
    hook path, which configures no logging, drops the line (INFO is
    under the last-resort handler's level) while the shim's stderr shows
    it once at start."""
    if theirs == mine:
        return True
    ours, other = version_key(mine), version_key(theirs)
    if ours is None or other is None or other < ours:
        return False
    import logging

    logging.getLogger(__name__).info(
        "keeping the daemon of version %s at %s:%s (pid %s), newer than this "
        "client's %s",
        theirs,
        HOST,
        state["port"],
        state["pid"],
        mine,
    )
    return True


def ensure_daemon(
    state_dir: Path,
    store_path: Path | str,
    *,
    version: str,
    start: bool = True,
    port: int | None = None,
    timeout: float = START_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """The state of a running daemon for `store_path` that a client of
    `version` can use, starting one when `start` and none answers. A
    daemon of an older version, or of one `version_key` cannot read, is
    stopped and replaced; one of the same or a newer version is kept
    (`_keeps`). None when no daemon can be reached, or none answers within
    `timeout` of the start. Raises `DaemonExited` when the daemon it
    started exited before any answered, naming that daemon's error.

    A state file whose daemon does not answer is left for the daemon
    started next to overwrite, not removed: it may name a daemon that is
    still starting (the file is written before the port listens) or busy,
    and removing it would leave that daemon serving unseen. The daemon's
    own lock (`lock_file_for`) is what keeps a second one from serving the
    store; the client side only keeps concurrent clients from each
    spawning one: whoever takes the spawn lock spawns and waits, and the
    others wait for the lock or for a daemon to answer, whichever comes
    first."""
    state = read_state(state_dir, store_path)
    if state is not None:
        answer = health(state["port"], timeout=0.5)
        if answer is not None:
            if _keeps(state, answer.get("version"), version):
                return state
            shutdown_daemon(state)
            remove_state(state_dir, store_path, pid=int(state["pid"]))
    if not start:
        return None
    deadline = time.monotonic() + timeout
    spawn_lock = try_lock(spawn_lock_file_for(state_dir, store_path))
    while spawn_lock is None:
        state = _answering(state_dir, store_path)
        if state is not None:
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)
        spawn_lock = try_lock(spawn_lock_file_for(state_dir, store_path))
    try:
        # A client that held the lock before this one may have started a
        # daemon that answers by now.
        state = _answering(state_dir, store_path)
        if state is not None and _keeps(state, state.get("version"), version):
            return state
        try:
            started = _spawn(state_dir, store_path, port=port, env=env)
        except OSError:
            return None
        return wait_for_daemon(
            state_dir,
            store_path,
            timeout=max(deadline - time.monotonic(), _POLL_SECONDS),
            started=started,
        )
    finally:
        release_lock(spawn_lock)


def resolved_store_path() -> Path:
    """The store the CLI would open from here, by the config's own rule.

    BETTERMEMORY_DIR, when set and not empty, decides the directory before
    anything in the config file can (`Config.resolved_directory`), so it
    is read here directly, expanded and resolved as the config reads it,
    and no config is loaded: a hook run with it set imports neither the
    config module nor what it imports. The variable unset, the config is
    loaded and its rule decides, exactly as before.

    With the variable set, what a config load did here no longer happens.
    A malformed config no longer stops a hook, or `bettermemory up`,
    `down` or `status`, before they reach a running daemon. The default
    config file is not created here (a daemon a hook or `up` starts loads
    the config and creates it), and the config's notices, a store under a
    system directory or a key a release removed, are not logged here (that
    daemon logs them to its log file). A malformed config still stops a
    daemon from starting, so where none is running the daemon the hook or
    `up` starts exits at once, and they report its error from its log
    (`DaemonExited`)."""
    named = os.environ.get("BETTERMEMORY_DIR")
    if named:
        return Path(named).expanduser().resolve() / STORE_FILENAME
    from .config import load_config

    return load_config().resolved_directory() / STORE_FILENAME


#: `identity.ENV_HARNESS_SESSION`, spelled here because the hook path
#: imports neither pydantic nor anything that pulls it in
#: (`tests/test_hook_client.py`); a test pins the two equal.
HARNESS_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
#: Set by Claude Code in every hook's environment: "1" when a person is
#: attending the session, "0" for `claude -p` and SDK runs. The hook
#: forwards `attended` only for exactly "1" (`capture_hook`), so an older
#: host, another harness and a scripted run all read as unattended.
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"


def daemon_env_for(store_path: Path | str) -> dict[str, str]:
    """The environment a daemon for `store_path` is started with: the
    store pinned, so the child resolves exactly the store the caller did,
    and the harness session id dropped: one daemon serves every
    conversation, and the id of the one that happened to start it names
    none of the others (each shim forwards its own as a header). The
    attended mark is dropped for the same reason: it describes the session
    whose hook started the daemon, and each hook forwards its own."""
    env = dict(os.environ)
    env["BETTERMEMORY_DIR"] = str(Path(store_path).parent)
    env.pop(HARNESS_SESSION_ENV, None)
    env.pop(ATTENDED_ENV, None)
    return env


def package_version() -> str:
    """This package's version, as `bettermemory.__version__` reads it: from
    the `_version.py` the build wrote, with no `importlib.metadata`, which
    is the fallback only where that file is absent. The daemon reports the
    same attribute (`daemon._version`), so a daemon this install started
    answers /health with the version this compares it against, and one an
    older install started does not and is replaced."""
    from . import __version__

    return __version__


# ---------------------------------------------------------------------------
# The hook commands
# ---------------------------------------------------------------------------


def _read_stdin_payload() -> dict[str, Any]:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.buffer.read(_STDIN_CAP_BYTES + 1)
    except (OSError, ValueError):
        return {}
    if len(raw) > _STDIN_CAP_BYTES:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_hook_argv(argv: list[str]) -> tuple[str | None, dict[str, Any], bool]:
    """(event, flags, quiet) from the hook's argv, or event None on a
    usage error."""
    words = list(argv)
    if not words:
        return None, {}, False
    head = words.pop(0)
    event: str | None
    if head == "hook":
        if not words or words[0] not in HOOK_EVENTS:
            return None, {}, False
        event = words.pop(0)
    else:
        event = _ALIASES.get(head)
        if event is None:
            return None, {}, False
    flags: dict[str, Any] = {}
    quiet = False
    while words:
        word = words.pop(0)
        if word == "--quiet":
            quiet = True
        elif word == "--dry-run":
            flags["dry_run"] = True
        elif word in ("--transcript-path", "--session-id", "--prompt") and words:
            flags[word[2:].replace("-", "_")] = words.pop(0)
        elif word in ("-h", "--help"):
            return None, {}, False
        else:
            return None, {}, False
    return event, flags, quiet


def _location_probe(payload: dict[str, Any]) -> None:
    """Put the caller's namespace probe in the payload (design 8.1,
    `_location_probe.probe`, which `location` re-exports): the cwd
    resolved, its object and the host's name, which the daemon compares
    with its own reading of that path. The probe's module imports only the
    standard library, so a hook pays for no capture module here."""
    from ._location_probe import probe

    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        payload["location"] = probe(cwd)


def _membership_rounds(
    state: dict[str, Any], event: str, payload: dict[str, Any], reply: dict[str, Any]
) -> dict[str, Any]:
    """The client's half of the witness exchange (design 8.7): while the
    daemon's reply asks (``membership.ask``), the client captures its
    location in its own namespace (`location.capture`, writing the
    checkout token under the policy the daemon names, decision D11),
    opens its own machine (`machine.open_machine`) and answers each
    question about its repository from its own files (`location.answer`),
    and posts again; at most `location.EXCHANGE_ROUNDS` rounds. A final
    reply asking for a re-mint (``membership.remint``) re-mints the token
    of the checkout captured here (design 3.3). The daemon never reads
    the client's paths.

    A first reply that asks nothing returns at once, before `location` is
    imported: no round runs and nothing is captured, so no re-mint can
    follow either, and the common hook (the daemon read the cwd itself)
    loads none of the capture modules."""
    said = reply.get("membership")
    if not isinstance(said, dict) or not isinstance(said.get("ask"), dict):
        return reply
    from . import checkout_token, location

    capture: location.Capture | None = None
    policy = False
    answers: dict[str, Any] = {}
    for _ in range(location.EXCHANGE_ROUNDS):
        said = reply.get("membership")
        question = said.get("ask") if isinstance(said, dict) else None
        if not isinstance(question, dict):
            break
        policy = bool(question.get("write_token"))
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            break
        if capture is None or question.get("location") or question.get("machine"):
            from .machine import open_machine

            capture = location.capture(cwd, write_token=policy)
            payload["location"] = capture.to_token()
            payload["machine"] = location.identity_to_json(open_machine())
        else:
            answers = location.answer(question, answers, capture)
            payload["answers"] = answers
        reply = post(
            state["port"],
            state["token"],
            HOOK_PATH + event,
            payload,
            timeout=HOOK_TIMEOUT_SECONDS,
        )
    said = reply.get("membership")
    if (
        isinstance(said, dict)
        and said.get("remint")
        and capture is not None
        and capture.git_dir
    ):
        checkout_token.remint(
            capture.git_dir, policy=policy, bare=capture.kind == location.BARE
        )
    return reply


def _blackhole_stdout() -> None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    except OSError:
        pass


def hook_main(argv: list[str]) -> int:
    """`bettermemory hook <event>` and its three aliases. Always returns 0:
    a hook must never break the harness's turn; a failure is one stderr
    line and no stdout."""
    event, flags, quiet = _parse_hook_argv(argv)
    if event is None:
        print(
            "usage: bettermemory hook {session-start|stop|prompt} [--quiet] [--dry-run] "
            "[--transcript-path PATH] [--session-id ID] [--prompt TEXT]",
            file=sys.stderr,
        )
        return 0
    try:
        payload = _read_stdin_payload()
        payload.update(flags)
        payload.setdefault("cwd", os.getcwd())
        _location_probe(payload)
        payload["quiet"] = quiet
        payload.pop("attended", None)
        if os.environ.get(ATTENDED_ENV) == "1":
            payload["attended"] = "1"
        store_path = resolved_store_path()
        state = ensure_daemon(
            resolve_state_dir(),
            store_path,
            version=package_version(),
            env=daemon_env_for(store_path),
        )
        if state is None:
            print(
                f"bettermemory hook {event}: no daemon could be reached or started",
                file=sys.stderr,
            )
            return 0
        reply = post(
            state["port"],
            state["token"],
            HOOK_PATH + event,
            payload,
            timeout=HOOK_TIMEOUT_SECONDS,
        )
        reply = _membership_rounds(state, event, payload, reply)
    except Exception as exc:  # noqa: BLE001 - the hook contract is exit 0
        print(
            f"bettermemory hook {event}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 0
    # A hook's JSON for the harness (the Stop's capture instruction) is
    # printed even under --quiet, and alone: Claude Code reads stdout as
    # one JSON object.
    out = reply.get("hook_output")
    if not (isinstance(out, str) and out):
        out = None if quiet else reply.get("stdout")
    if isinstance(out, str) and out:
        try:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            print(
                f"bettermemory hook {event}: could not write the context block: "
                f"{exc.__class__.__name__}: {exc}",
                file=sys.stderr,
            )
            _blackhole_stdout()
    return 0


__all__ = [
    "DAEMON_HELD_STATUS",
    "DEFAULT_PORT",
    "DaemonExited",
    "LOCK_WAIT_SECONDS",
    "HOOK_EVENTS",
    "HOOK_WORDS",
    "HOST",
    "STATE_DIR_ENV",
    "STORE_FILENAME",
    "Started",
    "acquire_lock",
    "daemon_env_for",
    "ensure_daemon",
    "health",
    "hook_main",
    "lock_file_for",
    "log_file_for",
    "package_version",
    "pid_alive",
    "post",
    "read_state",
    "read_state_file",
    "release_lock",
    "remove_state",
    "resolve_state_dir",
    "resolved_store_path",
    "shutdown_daemon",
    "spawn_lock_file_for",
    "start_daemon",
    "state_file_for",
    "try_lock",
    "version_key",
    "wait_for_daemon",
    "write_state",
]
