"""``bettermemory up``, ``down`` and ``status``: the local daemon's lifecycle.

One daemon per store, found through the state file named after the
store this process would open (`_daemon_client.state_file_for`), so the
three commands act on the same daemon the shim and the hooks use from
this directory.

Each of the three takes ``--json`` for scripts, and ``status --check``
turns the answer into an exit code a monitor can read
(`CHECK_RUNNING` and its siblings). Without those flags the text and
the exit codes are what they always were.

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
#: Something answers /health on the state file's port, but not as this
#: store's daemon: another store, another pid. On a shared host that can
#: be another account's listener on a port a crashed daemon left behind.
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
        "answers for a process that is not this store's daemon."
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


def is_this_daemon(answer: dict[str, Any], state: dict[str, Any]) -> bool:
    """Whether a /health `answer` comes from the daemon `state` names.

    A daemon reports the store it serves and its own pid, and it wrote
    both into its state file, so the two agree for the daemon that
    wrote the file and for no other process: a daemon of another store
    on a reused port reports that store, and a listener that is not a
    bettermemory daemon at all reports neither. On the Windows launcher
    path the state file holds the daemon's own pid, not the launcher's,
    which is the pid /health reports. Every 9.x daemon reports both
    fields; one that does not is not taken as this store's."""
    same_store = answer.get("store") == state.get("store")
    return same_store and answer.get("pid") == state.get("pid")


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


def _why_kept(pid: int, held: bool | None) -> str:
    """Why a state file whose daemon does not answer is kept."""
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
        health,
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
    answer = health(state["port"], timeout=_HEALTH_TIMEOUT_SECONDS)
    if answer is not None and is_this_daemon(answer, state):
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
    if answer is not None:
        report["answered"] = {
            "store": answer.get("store"),
            "pid": answer.get("pid"),
            "version": answer.get("version"),
        }
        report["state"] = NOT_OURS
        tail = ""
        if removed:
            tail = f"; the stale state file for pid {pid} is removed"
        elif not gone:
            tail = f"; its state file is kept ({_why_kept(pid, held)})"
        report["message"] = (
            f"bettermemory daemon: port {state['port']} answers for another "
            f"process (store {answer.get('store')}, pid {answer.get('pid')}), "
            f"not the daemon of {store_path}{tail}"
        )
    elif gone:
        report["state"] = STALE
        what = "removed" if removed else "left in place"
        report["message"] = (
            f"bettermemory daemon: not running for {store_path} "
            f"(stale state file for pid {pid} {what})"
        )
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
    from .._daemon_client import (
        DaemonExited,
        daemon_env_for,
        ensure_daemon,
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
    try:
        state = ensure_daemon(
            state_dir,
            store_path,
            version=package_version(),
            port=args.port,
            env=daemon_env_for(store_path),
        )
    except DaemonExited as exc:
        failure.update(error=str(exc), exit_status=exc.status, log_file=str(exc.log))
        _emit(failure, json_out=json_out, text=f"bettermemory: {exc}", err=True)
        raise SystemExit(1) from None
    if state is None:
        message = f"bettermemory: no daemon could be started for {store_path}"
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
        health,
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
    answer = health(port, timeout=_HEALTH_TIMEOUT_SECONDS)
    if answer is not None and not is_this_daemon(answer, state):
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
            answered={"store": answer.get("store"), "pid": answer.get("pid")},
        )
        if removed:
            outcome["message"] = (
                f"bettermemory daemon: not running for {store_path}; port {port} "
                f"answers for another process (store {answer.get('store')}), which "
                f"was left alone, and the stale state file for pid {pid} is removed"
            )
            _emit(outcome, json_out=json_out, text=outcome["message"])
            return
        outcome["message"] = (
            f"bettermemory daemon: port {port} answers for another process (store "
            f"{answer.get('store')}), not pid {pid}, which still holds the store's "
            "lock; nothing was stopped and the state file is kept"
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
        why = f"the state file is kept ({_why_kept(pid, held)})"
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
    "run_down",
    "run_status",
    "run_up",
    "uptime_seconds",
]
