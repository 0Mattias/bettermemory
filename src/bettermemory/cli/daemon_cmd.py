"""``bettermemory up``, ``down`` and ``status``: the local daemon's lifecycle.

One daemon per store, found through the state file named after the
store this process would open (`_daemon_client.state_file_for`), so the
three commands act on the same daemon the shim and the hooks use from
this directory.

Each of the three takes ``--json`` for scripts, and ``status --check``
turns the answer into an exit code a monitor can read
(`CHECK_RUNNING` and its siblings). Without those flags the exit codes
are what they always were, and so is the text, except where a state
file is now kept (below) or the port answers for something that is not
this store's daemon (`probe`), which `status` used to call running or
not running.

A state file is removed only when the daemon it names is provably gone:
its process no longer exists, or nobody holds the store's lock
(`lock_file_for`), which a daemon takes before it writes its state file
and gives back only after removing it. A daemon that is starting (the
state file is written once the port is bound, before it listens), busy
or hung holds that lock, and removing its file would leave it serving
the store unseen: every hook would then start a daemon that exits at
once on the held lock, and wait out `START_TIMEOUT_SECONDS` before
reporting that no daemon could be reached. `ensure_daemon` already keeps
such a file for that reason; `status` and `down` now follow the same
rule.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: `status --check`'s exit codes. A daemon that answers /health as this
#: store's daemon (`is_this_daemon`) is running.
CHECK_RUNNING = 0
#: No state file, or one whose daemon is provably gone (removed on the
#: way, as `status` always did).
CHECK_NOT_RUNNING = 1
#: A state file whose daemon does not answer but may be alive: its pid
#: exists and the store's lock is held (or cannot be checked). Starting,
#: busy or hung; the file is kept.
CHECK_NOT_ANSWERING = 2
#: Something answers on the state file's port, but not as this store's
#: daemon: a /health reply naming another store or pid, or a reply that
#: is no daemon's /health at all (an HTTP error, a body that is not the
#: daemon's, a protocol that is not HTTP), which is also how a daemon
#: that answers unhealthily reads. On a shared host that can be another
#: account's listener on a port a crashed daemon left behind.
CHECK_NOT_OURS = 3

#: The `state` of `daemon_report`, one per `--check` outcome, with the
#: provably gone state file told apart from no state file at all.
RUNNING = "running"
STOPPED = "stopped"
STALE = "stale"
NOT_ANSWERING = "not_answering"
NOT_OURS = "not_ours"

_CHECK_OF = {
    RUNNING: CHECK_RUNNING,
    STOPPED: CHECK_NOT_RUNNING,
    STALE: CHECK_NOT_RUNNING,
    NOT_ANSWERING: CHECK_NOT_ANSWERING,
    NOT_OURS: CHECK_NOT_OURS,
}

#: How long `status` and `down` wait for /health, as `status` always did.
_HEALTH_TIMEOUT_SECONDS = 1.0


def add_up(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    help_text = (
        "Start the local daemon for this store, detached, or report the one "
        "already running. --foreground runs it in this process, for supervisors. "
        "Exit 0 when a daemon runs, 1 when none could be started."
    )
    parser = sub.add_parser("up", help=help_text, description=help_text)
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run the daemon in this process instead of detaching it.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="The port to bind on 127.0.0.1 (default: [daemon] port, 7397); "
        "0 or a held port falls back to an ephemeral one.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the running daemon as JSON (the object `status --json` "
        "prints, plus `newly_started`), or the failure. Not with --foreground.",
    )
    return parser


def add_down(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    help_text = (
        "Stop the local daemon for this store. Exit 0 when it stopped or was "
        "not running, 1 when it did not stop; its state file is then kept while "
        "its process holds the store's lock."
    )
    parser = sub.add_parser("down", help=help_text, description=help_text)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the outcome as JSON instead of text.",
    )
    return parser


def add_status(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    help_text = (
        "Report whether the local daemon for this store is running. Exit 0; "
        "with --check the exit code is the answer: 0 running and answering as "
        "this store's daemon, 1 not running, 2 a state file whose daemon does "
        "not answer but may be alive (it holds the store's lock), 3 the port "
        "answers, but not as this store's healthy daemon (another store or "
        "process, or a reply that is not a daemon's /health)."
    )
    parser = sub.add_parser("status", help=help_text, description=help_text)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON: state, port, pid, version, store, the "
        "state, lock and log files, uptime, health. Never the token.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with the code that answers the question (see above), for "
        "monitors and scripts.",
    )
    return parser


# ---------------------------------------------------------------------------
# The report `status`, `up --json` and `service status` share
# ---------------------------------------------------------------------------


def is_this_daemon(answer: dict[str, Any], state: dict[str, Any], nonce: str) -> bool:
    """Whether a /health `answer` to `nonce` comes from the daemon `state`
    names: `_daemon_client.proves`, the check every client makes before
    it posts the token. A daemon from 9.0.0rc12 on answers the nonce with
    the token's MAC over it, which only the process that wrote the state
    file can produce. A listener that repeats the store and the pid an
    earlier, open /health gave it is therefore not taken for this store's
    daemon, and since 9.0.0rc14 neither is a daemon from before the proof,
    which offers none."""
    from .._daemon_client import proves

    return proves(state, answer, nonce)


def probe(
    port: int, *, timeout: float = _HEALTH_TIMEOUT_SECONDS, nonce: str | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """GET /health on `port`, read three ways: (the reply, None) when it
    is a daemon's (a JSON object with status ok, `_daemon_client.health`'s
    reading); (None, what answered) when something answered that is not;
    (None, None) when nothing did: the port refused, closed the
    connection, or stayed silent past `timeout`.

    `health` reads the last two alike, which is what a hook wants (either
    way there is no daemon to use), and lets a reply that is not HTTP at
    all escape as `http.client.HTTPException`. A report must tell them
    apart and must not end in a traceback, which `status --check` would
    turn into exit 1, "not running": a silent port beside a live daemon is
    a daemon starting, busy or hung, while a port that answers as
    something else is not this store's daemon, and `down` posts the token
    to no such listener.

    One plain connection to the port, not `urllib`: a report asks the port
    itself, so no ``http_proxy`` in the environment is consulted (urllib
    sends even 127.0.0.1 through one unless ``no_proxy`` names it) and no
    redirect is followed off the port. With `nonce`, the request asks the
    daemon for its proof (`is_this_daemon` reads it)."""
    import http.client

    from .._daemon_client import HEALTH_NONCE_PARAM, HEALTH_PATH, HOST

    query = f"?{HEALTH_NONCE_PARAM}={nonce}" if nonce else ""
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.request("GET", HEALTH_PATH + query)
        response = connection.getresponse()
        status, raw = response.status, response.read()
    except OSError:
        # Refused, reset, closed without a reply (`RemoteDisconnected` is
        # an OSError too), or silent past the timeout.
        return None, None
    except http.client.HTTPException as exc:
        return None, f"a reply that is not HTTP ({type(exc).__name__})"
    finally:
        connection.close()
    if not 200 <= status < 300:
        return None, f"HTTP {status}"
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, "a reply that is not JSON"
    if not isinstance(body, dict) or body.get("status") != "ok":
        return None, "a reply that is not a daemon's /health"
    return body, None


def _answered(answer: dict[str, Any] | None, other: str | None) -> dict[str, Any]:
    """What answered on a port that is not this store's daemon, for a
    report: the identity a /health reply gave, and `reply`, what the
    answer was when it was no daemon's /health (None when it was one)."""
    reply = answer or {}
    return {
        "store": reply.get("store"),
        "pid": reply.get("pid"),
        "version": reply.get("version"),
        "reply": other,
    }


def _who(
    answer: dict[str, Any] | None, other: str | None, state: dict[str, Any]
) -> str:
    """The listener `_answered` describes, for a message.

    A reply that names the state file's own store and pid is described as
    that, without the token's proof: "another process" followed by this
    daemon's identity would read as a contradiction. What the listener
    sent is its own to make up, and reaches the terminal only through
    `history.printable`, so an escape sequence in it stays text."""
    if answer is not None:
        from ..history import printable

        store, pid = answer.get("store"), answer.get("pid")
        if store == state.get("store") and pid == state.get("pid"):
            return (
                f"a process that repeats this daemon's store and pid {pid} "
                "without the token's proof"
            )
        return printable(f"another process (store {store}, pid {pid})")
    return f"something that is not a bettermemory daemon ({other})"


def lock_held(path: Path) -> bool | None:
    """Whether a process holds the store's lock at `path`: True held, False
    free, None when it cannot be checked (a lock file this process cannot
    open, such as another account's state directory).

    A free lock is taken for an instant and given back, the way
    `_daemon_client` reads it; a starting daemon waits
    `LOCK_WAIT_SECONDS` for it, so the instant costs it nothing. A lock
    file that does not exist is a lock nobody holds (a daemon keeps the
    file open while it holds the lock, and the file is never removed), so
    it reads as free without being created: a report does not write into
    the state directory."""
    from .._daemon_client import release_lock, try_lock

    if not path.exists():
        return False
    try:
        handle = try_lock(path)
    except OSError:
        return None
    if handle is None:
        return True
    release_lock(handle)
    return False


def uptime_seconds(started: object, *, now: datetime | None = None) -> int | None:
    """Whole seconds since `started`, the state file's ISO timestamp (UTC,
    with a ``Z``); None when it does not parse."""
    if not isinstance(started, str):
        return None
    try:
        then = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    current = now if now is not None else datetime.now(timezone.utc)
    return max(0, int((current - then).total_seconds()))


def _gone(state: dict[str, Any], lock_path: Path) -> tuple[bool, bool, bool | None]:
    """(gone, pid alive, lock held) for the daemon `state` names: gone when
    its process does not exist, or when nobody holds the store's lock. A
    lock that cannot be checked under a live pid is not proof, so that
    daemon is not gone."""
    from .._daemon_client import pid_alive

    alive = pid_alive(int(state["pid"]))
    held = lock_held(lock_path) if alive else None
    return (not alive or held is False), alive, held


def _why_kept(pid: int, held: bool | None, *, gone: bool = False) -> str:
    """Why a state file whose daemon does not answer is kept: the daemon
    may be alive, or, when it is `gone`, the file could not be removed or
    names a daemon started since (`_remove_stale`)."""
    if gone:
        return (
            f"the daemon of pid {pid} is gone, but the file could not be "
            "removed or names a daemon started since"
        )
    if held:
        return f"pid {pid} holds the lock"
    return f"pid {pid} is alive; its lock could not be checked"


def _remove_stale(state_dir: Path, store_path: Path, pid: int) -> bool:
    """Remove the state file when it still names `pid`; whether it is gone.
    A daemon started meanwhile has written its own file, which stays."""
    from .._daemon_client import remove_state, state_file_for

    try:
        remove_state(state_dir, store_path, pid=pid)
    except OSError:
        return False
    return not state_file_for(state_dir, store_path).exists()


def daemon_report(
    state_dir: Path, store_path: Path, *, tidy: bool = True
) -> dict[str, Any]:
    """What `status` knows about the daemon for `store_path`, as one dict.

    `state` is one of `RUNNING`, `STOPPED`, `STALE`, `NOT_ANSWERING` and
    `NOT_OURS`; `check` is the exit code `status --check` gives for it;
    `message` is the line `status` prints. The state file's token is never
    copied in. With `tidy` (what `status` does), a state file whose daemon
    is provably gone is removed, as `status` always removed a stale one;
    `service status` passes False and reports without touching anything."""
    from .._daemon_client import (
        lock_file_for,
        log_file_for,
        package_version,
        read_state,
        state_file_for,
    )

    lock_path = lock_file_for(state_dir, store_path)
    report: dict[str, Any] = {
        "state": STOPPED,
        "check": CHECK_NOT_RUNNING,
        "running": False,
        "health_ok": False,
        "store": str(store_path),
        "state_file": str(state_file_for(state_dir, store_path)),
        "lock_file": str(lock_path),
        "log_file": str(log_file_for(state_dir, store_path)),
        "pid": None,
        "port": None,
        "version": None,
        "client_version": package_version(),
        "started": None,
        "uptime_seconds": None,
        "pid_alive": None,
        "lock_held": None,
        "store_orphans": [],
        "answered": None,
        "state_file_removed": False,
        "message": f"bettermemory daemon: not running for {store_path}",
    }
    state = read_state(state_dir, store_path)
    if state is None:
        return report
    pid = int(state["pid"])
    report.update(
        pid=pid,
        port=state["port"],
        version=state.get("version"),
        started=state.get("started"),
    )
    nonce = secrets.token_hex(16)
    answer, other = probe(state["port"], nonce=nonce)
    if answer is not None and is_this_daemon(answer, state, nonce):
        version = answer.get("version", state["version"])
        orphans = answer.get("store_orphans")
        report.update(
            state=RUNNING,
            running=True,
            health_ok=True,
            version=version,
            uptime_seconds=uptime_seconds(state.get("started")),
            pid_alive=True,
            store_orphans=orphans if isinstance(orphans, list) else [],
            message=(
                f"bettermemory daemon {version} running: pid {pid}, port "
                f"{state['port']}, store {state['store']}, since {state['started']}"
            ),
        )
        report["check"] = _CHECK_OF[RUNNING]
        return report
    gone, alive, held = _gone(state, lock_path)
    removed = tidy and gone and _remove_stale(state_dir, store_path, pid)
    report.update(pid_alive=alive, lock_held=held, state_file_removed=removed)
    what = "removed" if removed else "left in place"
    stale = (
        f"bettermemory daemon: not running for {store_path} "
        f"(stale state file for pid {pid} {what})"
    )
    if answer is not None or other is not None:
        report["answered"] = _answered(answer, other)
        report["state"] = NOT_OURS
        who = _who(answer, other, state)
        if gone:
            # The line a stale state file always printed, then what holds
            # its port now.
            report["message"] = f"{stale}; port {state['port']} answers for {who}"
        else:
            report["message"] = (
                f"bettermemory daemon: port {state['port']} answers for {who}, "
                f"not the daemon of {store_path}; its state file is kept "
                f"({_why_kept(pid, held)})"
            )
    elif gone:
        report["state"] = STALE
        report["message"] = stale
    else:
        report["state"] = NOT_ANSWERING
        report["message"] = (
            "bettermemory daemon: state present, daemon not answering "
            f"({_why_kept(pid, held)}); the state file {report['state_file']} is kept"
        )
    report["check"] = _CHECK_OF[report["state"]]
    return report


def _emit(
    payload: dict[str, Any], *, json_out: bool, text: str, err: bool = False
) -> None:
    """One JSON object on stdout with `json_out`, else `text` on stdout
    (stderr with `err`)."""
    if json_out:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        print(text, file=sys.stderr if err else sys.stdout)


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def run_up(args: argparse.Namespace) -> None:
    """`up`: the detached daemon, or `--foreground` in this process.

    When no daemon answers, the failure says why when it can: a daemon that
    exited at start is quoted (`DaemonExited`), and a store whose lock
    another process still holds after the wait (a daemon starting, busy or
    hung, beside which none is started) is named in the shim's words
    (`_daemon_client.held_lock_reason`), in the text and in ``--json``'s
    ``error``. The lock is probed once the wait is over, never held.

    A store inside a backup bundle (`BETTERMEMORY_DIR` naming one) is
    refused before any daemon is spawned, in the line and with the exit 1
    the daemon itself would give it (`_common.exit_if_bundle_file`), so no
    process is started only to refuse it."""
    from ..backup import bundle_refusal
    from .._daemon_client import (
        START_TIMEOUT_SECONDS,
        DaemonExited,
        daemon_env_for,
        ensure_daemon,
        held_lock_reason,
        log_file_for,
        package_version,
        read_state,
        resolve_state_dir,
        resolved_store_path,
    )

    json_out = bool(getattr(args, "json", False))
    if args.foreground:
        if json_out:
            print(
                "bettermemory up: --json reports a detached daemon; it does not "
                "apply to --foreground",
                file=sys.stderr,
            )
            raise SystemExit(2)
        _run_foreground(args.port)
        return
    store_path = resolved_store_path()
    state_dir = resolve_state_dir()
    before = read_state(state_dir, store_path) if json_out else None
    failure: dict[str, Any] = {
        "state": "failed",
        "running": False,
        "store": str(store_path),
        "log_file": str(log_file_for(state_dir, store_path)),
        "exit_status": None,
    }
    refusal = bundle_refusal(store_path)
    if refusal is not None:
        failure["error"] = f"bettermemory: {refusal}"
        _emit(failure, json_out=json_out, text=failure["error"], err=True)
        raise SystemExit(1)
    try:
        state = ensure_daemon(
            state_dir,
            store_path,
            version=package_version(),
            port=args.port,
            timeout=START_TIMEOUT_SECONDS,
            env=daemon_env_for(store_path),
        )
    except DaemonExited as exc:
        failure.update(error=str(exc), exit_status=exc.status, log_file=str(exc.log))
        _emit(failure, json_out=json_out, text=f"bettermemory: {exc}", err=True)
        raise SystemExit(1) from None
    if state is None:
        message = f"bettermemory: no daemon could be started for {store_path}"
        held = held_lock_reason(state_dir, store_path, timeout=START_TIMEOUT_SECONDS)
        if held is not None:
            message = f"{message}: {held}"
        failure["error"] = message
        _emit(failure, json_out=json_out, text=message, err=True)
        raise SystemExit(1)
    if json_out:
        report = daemon_report(state_dir, store_path, tidy=False)
        report["newly_started"] = before is None or before.get("pid") != state["pid"]
        _emit(report, json_out=True, text="")
        return
    print(
        f"bettermemory daemon {state['version']} running: pid {state['pid']}, "
        f"port {state['port']}, store {state['store']}"
    )


def _run_foreground(port: int | None) -> None:
    import logging

    from .._daemon_client import enter_state_dir, resolve_state_dir
    from ..daemon import serve
    from ._common import cli_context

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # The daemon runs the migration under the store's lock
    # (`daemon._migrate_at_start`), not here before the lock is taken.
    ctx = cli_context(migrate=False)
    # The store is resolved where the process was started (./.claude-memory
    # is read from there); then the daemon moves to its state directory, as
    # a spawned one runs there, so no other directory is its working
    # directory and `down` leaves none held.
    state_dir = enter_state_dir(resolve_state_dir(), ctx.store.path)
    code = serve(
        config=ctx.config,
        store=ctx.store,
        state_dir=state_dir,
        port=port,
    )
    raise SystemExit(code)


def run_down(args: argparse.Namespace) -> None:
    from .._daemon_client import (
        lock_file_for,
        pid_alive,
        read_state,
        remove_state,
        resolve_state_dir,
        resolved_store_path,
        shutdown_daemon,
        state_file_for,
    )

    json_out = bool(getattr(args, "json", False))
    store_path = resolved_store_path()
    state_dir = resolve_state_dir()
    lock_path = lock_file_for(state_dir, store_path)
    outcome: dict[str, Any] = {
        "result": "not_running",
        "stopped": False,
        "was_running": False,
        "store": str(store_path),
        "state_file": str(state_file_for(state_dir, store_path)),
        "pid": None,
        "port": None,
        "lock_held": None,
        "state_file_kept": False,
    }
    state = read_state(state_dir, store_path)
    if state is None:
        outcome["message"] = f"bettermemory daemon: not running for {store_path}"
        _emit(outcome, json_out=json_out, text=outcome["message"])
        return
    pid, port = int(state["pid"]), int(state["port"])
    outcome.update(pid=pid, port=port)
    nonce = secrets.token_hex(16)
    answer, other = probe(port, nonce=nonce)
    if other is not None or (
        answer is not None and not is_this_daemon(answer, state, nonce)
    ):
        # The port answers, but not as the daemon the file names: the token
        # is not posted to it and no pid is signalled on its say-so. The
        # daemon the file named is gone when its pid is, or when the
        # store's lock is free, and the file is then removed.
        gone, _, held = _gone(state, lock_path)
        removed = gone and _remove_stale(state_dir, store_path, pid)
        outcome.update(
            result="not_ours",
            lock_held=held,
            state_file_kept=not removed,
            answered=_answered(answer, other),
        )
        who = _who(answer, other, state)
        if removed:
            outcome["message"] = (
                f"bettermemory daemon: not running for {store_path}; port {port} "
                f"answers for {who}, which was left alone, and the stale state "
                f"file for pid {pid} is removed"
            )
            _emit(outcome, json_out=json_out, text=outcome["message"])
            return
        outcome["message"] = (
            f"bettermemory daemon: port {port} answers for {who}, not pid {pid}; "
            "nothing was stopped and the state file is kept "
            f"({_why_kept(pid, held, gone=gone)})"
        )
        _emit(outcome, json_out=json_out, text=outcome["message"], err=True)
        raise SystemExit(1)
    outcome["was_running"] = answer is not None or pid_alive(pid)
    stopped = shutdown_daemon(state, state_dir=state_dir)
    if stopped:
        # Only the file that still names this daemon: a daemon started in
        # the meantime keeps its own.
        remove_state(state_dir, store_path, pid=pid)
        outcome.update(result="stopped", stopped=True)
        outcome["message"] = f"bettermemory daemon: stopped pid {pid} (port {port})"
        _emit(outcome, json_out=json_out, text=outcome["message"])
        return
    gone, _, held = _gone(state, lock_path)
    removed = gone and _remove_stale(state_dir, store_path, pid)
    outcome.update(result="did_not_stop", lock_held=held, state_file_kept=not removed)
    if removed:
        why = "the state file is removed (no process holds the store's lock)"
    else:
        why = f"the state file is kept ({_why_kept(pid, held, gone=gone)})"
    outcome["message"] = f"bettermemory daemon: pid {pid} did not stop; {why}"
    _emit(outcome, json_out=json_out, text=outcome["message"], err=True)
    raise SystemExit(1)


def run_status(args: argparse.Namespace) -> None:
    from .._daemon_client import resolve_state_dir, resolved_store_path

    report = daemon_report(resolve_state_dir(), resolved_store_path())
    _emit(report, json_out=bool(getattr(args, "json", False)), text=report["message"])
    if getattr(args, "check", False):
        raise SystemExit(report["check"])


__all__ = [
    "CHECK_NOT_ANSWERING",
    "CHECK_NOT_OURS",
    "CHECK_NOT_RUNNING",
    "CHECK_RUNNING",
    "add_down",
    "add_status",
    "add_up",
    "daemon_report",
    "is_this_daemon",
    "lock_held",
    "probe",
    "run_down",
    "run_status",
    "run_up",
    "uptime_seconds",
]
