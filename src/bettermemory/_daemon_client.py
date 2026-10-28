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

A client trusts no listener for answering on the state file's port. The
file outlives a daemon killed or crashed (only a clean stop removes it),
127.0.0.1 ports are shared by every account on the host, and a hook
posts the user's prompt, cwd and transcript path to whatever answers
there and prints its reply into the model's context. So wherever a
client decides that a daemon answers, before it posts a payload or
signals a pid, it sends ``/health`` a fresh nonce and checks the reply's
proof against the token in the state file (`identify`): only the daemon
that wrote the file holds that token. A daemon from before 9.0.0rc12
offers no proof and is taken as ours only when its state file says it
predates the proof and it reports that file's store and pid. Anything
else answering there is someone else's:
nothing is posted to it, its pid is never signalled, and a daemon is
started beside it, which binds another port when that one is held.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

STATE_DIR_ENV = "BETTERMEMORY_STATE_DIR"
#: The path variables a starting daemon resolves (`config.ENV_DIR_OVERRIDE`
#: and `config.KEYS_DIR_ENV`, spelled here so a hook imports no config;
#: tests/test_daemon_lifecycle.py pins them equal). `_spawn` hands them to
#: the child absolute, since the child does not run where its parent stands.
_STORE_DIR_ENV = "BETTERMEMORY_DIR"
_KEYS_DIR_ENV = "BETTERMEMORY_KEYS_DIR"
_PATH_ENVS: tuple[str, ...] = (_STORE_DIR_ENV, _KEYS_DIR_ENV)
DEFAULT_PORT = 7397
HOST = "127.0.0.1"
HEALTH_PATH = "/health"
SHUTDOWN_PATH = "/api/v1/admin/shutdown"
HOOK_PATH = "/api/v1/hook/"


# The daemon is on 127.0.0.1, and nothing between this client and it may
# see a request: `post` carries the bearer token and the hook payloads.
# `urllib.request.urlopen` consults http_proxy (and its kin) even for a
# loopback address unless no_proxy names it, so every request here goes
# through an opener with no proxy handler at all.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: The query parameter ``GET /health`` reads a client's nonce from, and the
#: nonces it answers a proof for: 32 to 128 hex digits, so a proof is never
#: computed over a value short enough to have been seen before.
HEALTH_NONCE_PARAM = "nonce"
_NONCE = re.compile(r"[0-9a-fA-F]{32,128}")
#: What the proof's MAC covers ahead of the nonce, so the token's MAC over
#: a health nonce can never stand for its MAC over anything else.
_PROOF_CONTEXT = b"bettermemory-health-v1:"
#: The key a daemon that answers the proof writes into its state file
#: (`daemon.serve`, 9.0.0rc12 on), with the proof's version, 1. The pid and
#: the store a daemon reports at ``/health`` are no secret: the route
#: answers them to any process on the host, without the token. So a reply
#: without a proof is taken for a daemon from before the proof only at the
#: port of a state file without this key, one such a daemon wrote. At the
#: port of a file that carries it, that reply is another listener's,
#: whatever pid and store it repeats (`proves`).
PROOF_STATE_KEY = "health_proof"

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
#: How long `shutdown_daemon` waits for a daemon to release the store
#: after the shutdown endpoint answered, before it falls back to a
#: signal: the daemon's own bounds, `daemon.GRACEFUL_SHUTDOWN_SECONDS`
#: (20 s for the requests in flight) plus `daemon.WORKERS_CLOSE_SECONDS`
#: (10 s for the workers' join, both pools under one deadline);
#: tests/test_daemon_lifecycle.py pins
#: the sum. A daemon with nothing in flight stops in milliseconds.
SHUTDOWN_TIMEOUT_SECONDS = 30.0
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
    """Write the state file for `state["store"]`, owner-only, atomically,
    in a state directory made or healed owner-only first
    (`_fsutil.ensure_owner_only_dir`)."""
    from ._fsutil import ensure_owner_only_dir, replace_atomic

    state_dir = Path(state_dir)
    ensure_owner_only_dir(state_dir, parents=True)
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


def health(
    port: int, *, timeout: float = 1.0, nonce: str | None = None
) -> dict[str, Any] | None:
    """GET /health on the daemon at `port`, or None when nothing answers.
    With `nonce`, the request asks the daemon to prove it holds its token
    (`health_proof`); whatever answers decides whether a ``proof`` comes
    back, so the reply says nothing about who answered until `proves`
    has read it.

    A listener that does not speak HTTP answers nothing either: its reply
    fails as `http.client.HTTPException` (a status line that is not one, a
    header line past the limit), which is neither an OSError nor a
    ValueError, and escaped this before 9.0.0rc12. A client that met one on
    a crashed daemon's port then raised where it should start a daemon
    beside it (`ensure_daemon`), and every hook failed while the listener
    stayed."""
    query = f"?{HEALTH_NONCE_PARAM}={nonce}" if nonce else ""
    try:
        with _LOOPBACK.open(
            f"http://{HOST}:{port}{HEALTH_PATH}{query}", timeout=timeout
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return None
    return body if isinstance(body, dict) and body.get("status") == "ok" else None


def valid_nonce(nonce: object) -> TypeGuard[str]:
    """Whether `nonce` is one ``/health`` answers a proof for: 32 to 128
    hex digits."""
    return isinstance(nonce, str) and _NONCE.fullmatch(nonce) is not None


def health_proof(token: str, nonce: str) -> str:
    """The proof a daemon holding `token` answers ``/health?nonce=`` with:
    hex(HMAC-SHA256(token, ``bettermemory-health-v1:`` + nonce)), the nonce
    as the ASCII text it was sent as. The daemon computes it
    (`daemon.build_app`), and a client that recomputes it from the state
    file's token knows it reached the daemon that wrote the file. One
    definition, so the two sides cannot drift.

    The token is encoded with ``surrogatepass``: a daemon's own token is
    ASCII, but the client reads it off a state file, where JSON can spell
    a lone surrogate that plain UTF-8 refuses to encode. Such a token then
    yields a proof no daemon answers with, and the reply reads as not
    ours (`proves`), rather than ending the hook in UnicodeEncodeError."""
    return hmac.new(
        token.encode("utf-8", errors="surrogatepass"),
        _PROOF_CONTEXT + nonce.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def proves(state: Mapping[str, Any], answer: Mapping[str, Any], nonce: str) -> bool:
    """Whether `answer`, the ``/health`` reply to `nonce`, comes from the
    daemon `state` names.

    A reply that carries a ``proof`` is decided by the proof alone: the
    token's MAC over the nonce, compared in constant time, which only the
    process that wrote the state file can produce. A reply without one can
    be a daemon from before 9.0.0rc12, which offers none, only when the
    state file was written by such a daemon (it carries no
    `PROOF_STATE_KEY`); it is then taken as ours only when it reports the
    state file's store and the state file's pid. The state file is the one
    named after the caller's store (`state_file_for`), so its ``store`` is
    the caller's store path. A listener another account left on a crashed
    daemon's port, or another store's daemon that came up on it, fails
    either way, even when it repeats the pid and the store an earlier
    ``/health`` told it."""
    if "proof" in answer:
        proof = answer["proof"]
        if not isinstance(proof, str):
            return False
        expected = health_proof(str(state["token"]), nonce)
        # Bytes, not str: compare_digest refuses a str that is not ASCII,
        # and the proof is whatever the listener chose to send.
        return hmac.compare_digest(
            proof.encode("utf-8", errors="replace"), expected.encode("ascii")
        )
    if PROOF_STATE_KEY in state:
        # The daemon that wrote this file answers every nonce with a
        # proof: a reply without one is a listener that read the pid and
        # the store off an earlier, open ``/health``.
        return False
    pid = answer.get("pid")
    return (
        type(pid) is int
        and pid == state["pid"]
        and answer.get("store") == state["store"]
    )


class Identified(NamedTuple):
    """What answered ``/health`` at a state file's port: `answer`, its reply
    (None when nothing answered), and `ours`, whether that reply proved it
    the daemon the state file names (`proves`)."""

    answer: dict[str, Any] | None
    ours: bool

    @property
    def foreign(self) -> bool:
        """Something answers at the port, and it is not our daemon."""
        return self.answer is not None and not self.ours


def identify(state: Mapping[str, Any], *, timeout: float = 0.5) -> Identified:
    """GET ``/health`` at the port `state` names with a fresh nonce
    (`secrets.token_hex(16)`), and whether what answers is the daemon that
    wrote `state`. One request, the one a client's discovery made before
    the proof existed, so the hook's fast path grows no round trip."""
    nonce = secrets.token_hex(16)
    answer = health(int(state["port"]), timeout=timeout, nonce=nonce)
    if answer is None:
        return Identified(None, False)
    return Identified(answer, proves(state, answer, nonce))


def _warn_foreign(state: Mapping[str, Any]) -> None:
    """One warning that the listener at `state`'s port is not the daemon
    the state file names. `logging` is imported here, as in `_keeps`: only
    this case pays for it. On the hook path, which configures no logging,
    the last-resort handler prints the line on stderr, beside the hook's
    own failure lines. Nothing the listener sent is quoted: the line names
    only what the state file holds."""
    import logging

    logging.getLogger(__name__).warning(
        "the process answering at %s:%s is not the bettermemory daemon its "
        "state file names (pid %s, store %s): nothing is sent to it",
        HOST,
        state["port"],
        state["pid"],
        state["store"],
    )


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
    with _LOOPBACK.open(request, timeout=timeout) as response:
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
    state file where the caller will look for it, and the store's
    directory pinned when `env` names none, so it serves `store_path`."""
    return _spawn(state_dir, store_path, port=port, env=env).child.pid


def daemon_process_env(
    env: Mapping[str, str], *, state_dir: Path, store_path: Path | str
) -> dict[str, str]:
    """A copy of `env` for a daemon process that runs in `state_dir`.

    The store's directory is set to the store's parent when `env` names
    none, or names it empty, which the resolution rule reads as none, so
    a daemon never resolves a store of its own where it runs.
    Each of `_PATH_ENVS` that is set is made absolute against this
    process's directory, which is what a relative value meant:
    `Config.resolved_directory` reads the store directory on every call,
    so a relative value left in place would resolve against the state
    directory once the process has moved there. The state directory is
    set to `state_dir`, resolved.

    The keys directory is resolved but for its last component
    (`_pin_keys_dir`): a keys directory that is a symbolic link reaches
    the daemon as the link. Resolved through, a link to a volume that is
    not mounted named the directory it points at, where the daemon found
    no key, wrote a new one and rekeyed the store, the very failure the
    owner's opener refuses for a link it can see (`log.KeyRing.load_current`
    reads it as a key it cannot read, and the daemon exits saying so)."""
    pinned = dict(env)
    if not pinned.get(_STORE_DIR_ENV):
        pinned[_STORE_DIR_ENV] = str(Path(store_path).parent)
    for name in _PATH_ENVS:
        value = pinned.get(name)
        if value and name == _KEYS_DIR_ENV:
            pinned[name] = _pin_keys_dir(value)
        elif value:
            pinned[name] = str(Path(value).expanduser().resolve())
    pinned[STATE_DIR_ENV] = str(Path(state_dir).expanduser().resolve())
    return pinned


def _pin_keys_dir(value: str) -> str:
    """`value`, the keys directory, absolute with every component but the
    last resolved, so the directory itself, when it is a symbolic link,
    stays one (`daemon_process_env`). A value whose last component names
    no directory entry (``.``, ``..``, a root) is resolved whole."""
    path = Path(value).expanduser().absolute()
    if path.name in ("", ".", ".."):
        return str(path.resolve())
    return str(path.parent.resolve() / path.name)


def enter_state_dir(state_dir: Path, store_path: Path | str) -> Path:
    """Move this process into `state_dir` as a daemon, and return it resolved.

    A daemon started by hand (`up --foreground`, or a supervisor that sets
    no working directory) would otherwise keep the directory it was
    started from, and Windows holds a process's working directory open
    against a rename or a delete for the process's life. The path
    variables are pinned in this environment first (`daemon_process_env`),
    since they must be made absolute before the process moves. A spawned
    daemon arrives here already pinned and already in its state directory,
    so for it nothing changes. The directory is made or healed owner-only
    (`_fsutil.ensure_owner_only_dir`) before the daemon opens its lock in
    it, as `_spawn` does before it opens the log."""
    from ._fsutil import ensure_owner_only_dir

    state_dir = Path(state_dir).expanduser().resolve()
    ensure_owner_only_dir(state_dir, parents=True)
    os.environ.update(
        daemon_process_env(os.environ, state_dir=state_dir, store_path=store_path)
    )
    os.chdir(state_dir)
    return state_dir


def _spawn(
    state_dir: Path,
    store_path: Path | str,
    *,
    port: int | None,
    env: dict[str, str] | None,
) -> Started:
    """`start_daemon`, returning the child itself and where its log begins.

    The child runs in the state directory, not where this process stands.
    Windows holds a process's working directory open, so a daemon spawned
    from a project checkout pinned that checkout against a rename or a
    delete until `down`; the state directory is held by the child's log
    and lock handles anyway. Nothing the daemon reads once it serves
    depends on its working directory (each hook posts its caller's cwd,
    the shim sends its own), but the path variables it resolves at
    startup (`_PATH_ENVS`, the state directory) would resolve a relative
    value against it, so they are made absolute first
    (`daemon_process_env`), against this process's directory, which is
    what a relative value meant. The
    store's directory is pinned the same way when `env` names none
    (`daemon_env_for` does), since a daemon left to resolve its store
    where it runs would open one under the state directory.

    The state directory is made or healed owner-only first
    (`_fsutil.ensure_owner_only_dir`, every ancestor it creates too), and
    the log is opened owner-only (`_open_log`): the daemon tightened the
    directory only when it wrote its state file, so a daemon that exited
    before that left a directory and a log, which holds the store's path
    and every start's errors, at the umask, typically readable by every
    account on the host."""
    from ._fsutil import ensure_owner_only_dir

    state_dir = Path(state_dir).expanduser().resolve()
    ensure_owner_only_dir(state_dir, parents=True)
    child_env = daemon_process_env(
        os.environ if env is None else env, state_dir=state_dir, store_path=store_path
    )
    command = [sys.executable, "-m", "bettermemory", "up", "--foreground"]
    if port is not None:
        command += ["--port", str(port)]
    log_path = log_file_for(state_dir, store_path)
    log_handle = _open_log(log_path)
    try:
        offset = os.fstat(log_handle.fileno()).st_size
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": child_env,
            "cwd": str(state_dir),
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


def _open_log(path: Path) -> Any:
    """The daemon's log at `path`, opened for append and owner-only: made
    with mode 0o600, and an existing log this account owns that still
    carries group or other bits (one an earlier release made at the umask)
    is narrowed to 0o600 through the open descriptor. A log another
    account owns is left as it is; its owner chose its mode. POSIX modes
    mean nothing on Windows, where only the open is done."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    if sys.platform != "win32":
        try:
            found = os.fstat(fd)
            if found.st_uid == os.getuid() and found.st_mode & 0o077:
                os.fchmod(fd, 0o600)
        except OSError:
            pass
    return os.fdopen(fd, "ab")


def _answering(state_dir: Path, store_path: Path | str) -> dict[str, Any] | None:
    """The state of a daemon for `store_path` that answers /health and
    proves itself the daemon its state file names (`identify`); None when
    nothing answers there, or something else does."""
    state = read_state(state_dir, store_path)
    if state is not None and identify(state, timeout=0.5).ours:
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


def start_and_wait(
    state_dir: Path,
    store_path: Path | str,
    *,
    port: int | None,
    env: dict[str, str] | None,
    timeout: float,
) -> dict[str, Any] | None:
    """Start a daemon for `store_path` and wait up to `timeout` for one to
    answer (`wait_for_daemon`), unless another process holds the store's
    lock (`lock_file_for`): then start none, and wait for that process.

    A held lock means a daemon of this store is alive: one still starting
    (it takes the lock before it writes its state file or listens), one
    busy past the health probe's half second, or one whose state file is
    gone. A daemon started beside it could not serve: it waits
    `LOCK_WAIT_SECONDS` for the lock and exits with `DAEMON_HELD_STATUS`,
    a process and a log line spent on every hook while the holder is slow.
    So while the lock is held this polls for a daemon that answers and
    proves itself (`_answering`, the check every discovery makes) and
    returns its state; when the lock comes free first (the holder exited
    without answering), it starts a daemon, within what is left of
    `timeout`; and when `timeout` passes first, it answers None, as a
    start nobody answered does. The lock is probed, never held
    (`_held_elsewhere`), so a daemon starting meanwhile is not kept from
    it. With the lock free this is the start it always was.

    Raises `OSError` when the spawn fails and `DaemonExited` as
    `wait_for_daemon` does."""
    deadline = time.monotonic() + timeout
    lock_path = lock_file_for(state_dir, store_path)
    while _held_elsewhere(lock_path):
        state = _answering(state_dir, store_path)
        if state is not None:
            return state
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_SECONDS)
    started = _spawn(state_dir, store_path, port=port, env=env)
    return wait_for_daemon(
        state_dir,
        store_path,
        timeout=max(deadline - time.monotonic(), _POLL_SECONDS),
        started=started,
    )


def _held_elsewhere(lock_path: Path) -> bool:
    """Whether the store's lock at `lock_path` is held at this instant by
    another process (or another open of it in this one). Probed as
    `_lock_free` probes it, taken and given back at once; a lock file that
    does not exist is free without being made, since a daemon creates it
    before it can hold it and nothing removes it. A lock this process
    cannot open reads as free, so the start goes ahead as it did before
    the probe."""
    if not os.path.exists(lock_path):
        return False
    return not _lock_free(lock_path)


def shutdown_daemon(
    state: dict[str, Any],
    *,
    state_dir: Path,
    timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
) -> bool:
    """Ask the daemon in `state` to stop, then make sure it did. The
    shutdown endpoint is the mechanism; a signal is the fallback. Stopped
    means the store is released, not only the port closed (`_stopped`);
    `state_dir` is where the daemon's lock lives, the last thing it gives
    back.

    What answers at the state file's port must prove itself first
    (`identify`). Only a daemon that did is posted the shutdown, which
    carries the token, and only its port counts as the daemon's. A
    listener that is not ours gets nothing, and its port says nothing
    about our daemon, which is then stopped when its pid is gone or the
    store's lock is free.

    The signal goes to the state file's pid only when the daemon at the
    port proved itself, or when that pid is alive while the store's lock
    is held (`_holds_lock`): never on a state file alone. A daemon killed
    or crashed leaves its state file behind, and once the OS hands its pid
    to another process, a signal on the file's word alone would stop that
    process instead."""
    pid = int(state["pid"])
    port = int(state["port"])
    lock_path = lock_file_for(state_dir, state["store"])
    ours = identify(state, timeout=1.0).ours
    watched = port if ours else None
    if ours:
        try:
            post(port, state["token"], SHUTDOWN_PATH, {}, timeout=2.0)
        except (OSError, ValueError, urllib.error.URLError):
            pass
    if _wait_stopped(pid, watched, lock_path, timeout):
        return True
    if not ours and not _holds_lock(pid, lock_path):
        return _stopped(pid, watched, lock_path)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return _stopped(pid, watched, lock_path)
    return _wait_stopped(pid, watched, lock_path, timeout)


def _holds_lock(pid: int, lock_path: Path) -> bool:
    """Whether `pid` may be taken for the process holding the store's lock:
    it is alive, and the lock is held. An advisory lock does not name its
    holder (`flock` and `msvcrt.locking` both keep that to the kernel), so
    this is the closest a client can read "the daemon still holds the
    lock": a daemon holds it from its start to its exit, and the OS drops
    it with the process, so a held lock means a daemon of this store is
    alive. The one case it misreads needs three things at once: the state
    file's daemon gone, its pid reused by another process of this account,
    and a new daemon of the same store holding the lock in the moment
    before it writes its own state file."""
    return pid_alive(pid) and not _lock_free(lock_path)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.2):
            return True
    except OSError:
        return False


def _lock_free(path: Path) -> bool:
    """Whether nobody holds the lock at `path`: taken for an instant and
    given back when it is free. A lock this process cannot open (another
    user's state directory, a read-only one) reads as free, and the port
    is then the signal, as it was before the lock."""
    try:
        handle = try_lock(path)
    except OSError:
        return True
    if handle is None:
        return False
    release_lock(handle)
    return True


def _stopped(pid: int, port: int | None, lock_path: Path) -> bool:
    """A daemon is stopped when its process is gone, or when it holds
    neither its port nor the store's lock; with `port` None (what answers
    at the state file's port did not prove itself ours, `shutdown_daemon`),
    when its process is gone or the lock is free. The lock
    (`lock_file_for`) is the last thing a daemon gives back: `daemon.serve`
    closes the store and removes the state file before it, so a free lock
    means no handle of the daemon is open on the store's files, and the
    store can be moved, copied or removed the moment `down` returns, on
    Windows too, where an open handle refuses the rename. The port closes
    earlier, as the server stops accepting and while the store is still
    open, so it is the signal only for a daemon that holds no lock: one
    from before the lock, or a zombie a parent has not reaped, which still
    answers `os.kill(pid, 0)` but whose socket and lock the OS dropped
    with its handles."""
    if not pid_alive(pid):
        return True
    if port is not None and _port_open(port):
        return False
    return _lock_free(lock_path)


def _wait_stopped(pid: int, port: int | None, lock_path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not _stopped(pid, port, lock_path):
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
    first. Whoever takes it spawns nothing while another process holds the
    store's lock: it waits, within `timeout`, for that holder to answer
    and prove itself, and starts a daemon only if the lock comes free
    first (`start_and_wait`).

    A daemon answers only when it proves itself the one the state file
    names (`identify`). Something else answering at that port, a listener
    on a crashed daemon's port or another store's daemon, is logged once
    and left alone: it is neither kept, nor asked to stop, nor signalled,
    and the daemon started next binds another port (`daemon.bind`) and
    rewrites the state file."""
    state = read_state(state_dir, store_path)
    if state is not None:
        found = identify(state, timeout=0.5)
        if found.ours and found.answer is not None:
            if _keeps(state, found.answer.get("version"), version):
                return state
            shutdown_daemon(state, state_dir=state_dir)
            remove_state(state_dir, store_path, pid=int(state["pid"]))
        elif found.foreign:
            _warn_foreign(state)
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
        # No daemon is started while the store's lock is held: the holder
        # is waited for instead (`start_and_wait`).
        try:
            return start_and_wait(
                state_dir,
                store_path,
                port=port,
                env=env,
                timeout=max(deadline - time.monotonic(), _POLL_SECONDS),
            )
        except OSError:
            return None
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
    env[_STORE_DIR_ENV] = str(Path(store_path).parent)
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
    "HEALTH_NONCE_PARAM",
    "LOCK_WAIT_SECONDS",
    "HOOK_EVENTS",
    "HOOK_WORDS",
    "HOST",
    "Identified",
    "PROOF_STATE_KEY",
    "STATE_DIR_ENV",
    "STORE_FILENAME",
    "Started",
    "acquire_lock",
    "daemon_env_for",
    "daemon_process_env",
    "enter_state_dir",
    "ensure_daemon",
    "health",
    "health_proof",
    "hook_main",
    "identify",
    "lock_file_for",
    "log_file_for",
    "package_version",
    "pid_alive",
    "post",
    "proves",
    "read_state",
    "read_state_file",
    "release_lock",
    "remove_state",
    "resolve_state_dir",
    "resolved_store_path",
    "shutdown_daemon",
    "spawn_lock_file_for",
    "start_and_wait",
    "start_daemon",
    "state_file_for",
    "try_lock",
    "valid_nonce",
    "version_key",
    "wait_for_daemon",
    "write_state",
]
