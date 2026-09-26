"""``bettermemory up``, ``down`` and ``status``: the local daemon's lifecycle.

One daemon per store, found through the state file named after the
store this process would open (`_daemon_client.state_file_for`), so the
three commands act on the same daemon the shim and the hooks use from
this directory.
"""

from __future__ import annotations

import argparse
import sys


def add_up(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = sub.add_parser(
        "up",
        help=(
            "Start the local daemon for this store, detached, or report the one "
            "already running. --foreground runs it in this process, for supervisors."
        ),
    )
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
    return parser


def add_down(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    return sub.add_parser("down", help="Stop the local daemon for this store.")


def add_status(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    return sub.add_parser(
        "status", help="Report whether the local daemon for this store is running."
    )


def run_up(args: argparse.Namespace) -> None:
    from .._daemon_client import (
        daemon_env_for,
        ensure_daemon,
        package_version,
        resolve_state_dir,
        resolved_store_path,
    )

    if args.foreground:
        _run_foreground(args.port)
        return
    store_path = resolved_store_path()
    state = ensure_daemon(
        resolve_state_dir(),
        store_path,
        version=package_version(),
        port=args.port,
        env=daemon_env_for(store_path),
    )
    if state is None:
        print(
            f"bettermemory: no daemon could be started for {store_path}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(
        f"bettermemory daemon {state['version']} running: pid {state['pid']}, "
        f"port {state['port']}, store {state['store']}"
    )


def _run_foreground(port: int | None) -> None:
    import logging

    from .._daemon_client import resolve_state_dir
    from ..daemon import serve
    from ._common import cli_context

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ctx = cli_context()
    code = serve(
        config=ctx.config,
        store=ctx.store,
        state_dir=resolve_state_dir(),
        port=port,
    )
    raise SystemExit(code)


def run_down(args: argparse.Namespace) -> None:
    from .._daemon_client import (
        read_state,
        remove_state,
        resolve_state_dir,
        resolved_store_path,
        shutdown_daemon,
    )

    store_path = resolved_store_path()
    state_dir = resolve_state_dir()
    state = read_state(state_dir, store_path)
    if state is None:
        print(f"bettermemory daemon: not running for {store_path}")
        return
    stopped = shutdown_daemon(state)
    remove_state(state_dir, store_path)
    if not stopped:
        print(
            f"bettermemory daemon: pid {state['pid']} did not stop; the state file is removed",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"bettermemory daemon: stopped pid {state['pid']} (port {state['port']})")


def run_status(args: argparse.Namespace) -> None:
    from .._daemon_client import (
        health,
        read_state,
        remove_state,
        resolve_state_dir,
        resolved_store_path,
    )

    store_path = resolved_store_path()
    state_dir = resolve_state_dir()
    state = read_state(state_dir, store_path)
    if state is None:
        print(f"bettermemory daemon: not running for {store_path}")
        return
    answer = health(state["port"], timeout=1.0)
    if answer is None:
        remove_state(state_dir, store_path)
        print(
            f"bettermemory daemon: not running for {store_path} "
            f"(stale state file for pid {state['pid']} removed)"
        )
        return
    print(
        f"bettermemory daemon {answer.get('version', state['version'])} running: "
        f"pid {state['pid']}, port {state['port']}, store {state['store']}, "
        f"since {state['started']}"
    )


__all__ = ["add_down", "add_status", "add_up", "run_down", "run_status", "run_up"]
