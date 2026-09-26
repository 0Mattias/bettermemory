"""The daemon's client side: the state file, the health probe, starting and
stopping a daemon, and the three hook commands.

Standard library only, on purpose. A hook is a process the harness spawns
on every turn, so the whole cost of this module is what the hook pays on
top of the interpreter: `import json, urllib.request, socket` measures
about 26 ms on the author's machine against 316 ms for `import mcp`.
`_entry.main` dispatches the hook words to `hook_main` before anything
heavier is imported, and `tests/test_hook_client.py` checks the path with
`-X importtime`.

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
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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

START_TIMEOUT_SECONDS = 8.0
HOOK_TIMEOUT_SECONDS = 15.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
_POLL_SECONDS = 0.02
_STDIN_CAP_BYTES = 1 << 20
_STATE_KEYS = ("pid", "port", "token", "version", "store", "started")


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
# Processes and the wire
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Whether a process with `pid` exists (not whether it is a daemon)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
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


def start_daemon(
    state_dir: Path,
    store_path: Path | str,
    *,
    port: int | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Spawn `bettermemory up --foreground` detached from this process and
    return its pid. The child inherits `env` (default: this environment)
    with the state directory pinned, so it writes the state file where the
    caller will look for it."""
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
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": child_env,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            )
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()
    return child.pid


def wait_for_daemon(
    state_dir: Path, store_path: Path | str, *, timeout: float
) -> dict[str, Any] | None:
    """Poll the state file and /health until a daemon answers or `timeout`."""
    deadline = time.monotonic() + timeout
    while True:
        state = read_state(state_dir, store_path)
        if state is not None and health(state["port"], timeout=0.5) is not None:
            return state
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
    """The state of a running daemon of `version` for `store_path`,
    starting one when `start` and none answers. A daemon of another
    version is stopped and replaced; a stale state file is removed. None
    when no daemon can be reached or started."""
    state = read_state(state_dir, store_path)
    if state is not None:
        answer = health(state["port"], timeout=0.5)
        if answer is not None and answer.get("version") == version:
            return state
        if answer is not None:
            shutdown_daemon(state)
        remove_state(state_dir, store_path)
    if not start:
        return None
    try:
        start_daemon(state_dir, store_path, port=port, env=env)
    except OSError:
        return None
    return wait_for_daemon(state_dir, store_path, timeout=timeout)


def resolved_store_path() -> Path:
    """The store the CLI would open from here, by the config's own rule."""
    from .config import STORE_FILENAME, load_config

    return load_config().resolved_directory() / STORE_FILENAME


def daemon_env_for(store_path: Path | str) -> dict[str, str]:
    """The environment a daemon for `store_path` is started with: the
    store pinned, so the child resolves exactly the store the caller did."""
    env = dict(os.environ)
    env["BETTERMEMORY_DIR"] = str(Path(store_path).parent)
    return env


def package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("bettermemory")
    except PackageNotFoundError:
        return "0+unknown"


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
        payload["quiet"] = quiet
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
    except Exception as exc:  # noqa: BLE001 - the hook contract is exit 0
        print(
            f"bettermemory hook {event}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 0
    out = reply.get("stdout")
    if isinstance(out, str) and out and not quiet:
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
    "DEFAULT_PORT",
    "HOOK_EVENTS",
    "HOOK_WORDS",
    "HOST",
    "STATE_DIR_ENV",
    "daemon_env_for",
    "ensure_daemon",
    "health",
    "hook_main",
    "log_file_for",
    "package_version",
    "pid_alive",
    "post",
    "read_state",
    "read_state_file",
    "remove_state",
    "resolve_state_dir",
    "resolved_store_path",
    "shutdown_daemon",
    "start_daemon",
    "state_file_for",
    "wait_for_daemon",
    "write_state",
]
