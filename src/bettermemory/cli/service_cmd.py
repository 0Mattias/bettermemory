"""``bettermemory service install|uninstall|status|print``: the local daemon
as a supervised service, under launchd on macOS and systemd on Linux.

The unit's text, its path and the commands that load it come from
`service_units`, pure functions of the paths, the environment and the
platform. This module resolves those inputs the way the daemon will
(the state directory, the store the daemon will serve from its working
directory, this interpreter), writes the file, and runs the supervisor's
commands. `print` writes nothing; `status` changes nothing.

One unit per user, under a fixed name, serving the store a daemon started
in the state directory resolves: ``BETTERMEMORY_DIR`` when the installing
shell sets it, else ``[storage] directory``, else the home default. A
project's own ``.claude-memory`` is not the unit's store; the hooks keep
spawning a daemon for it on first use, as they do today.

A supervised daemon and the ones the hooks spawn share a store safely:
the store's lock admits one daemon, and a second one exits 75 without
serving. So `install`, before it loads the unit, stops a daemon of the
same store that is running outside the supervisor; otherwise the unit's
daemon would find the store held and wait for its turn in restarts
(`service_units.RESTART_SECONDS` apart). `--no-load` skips both.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from .. import service_units as units

#: `service status`'s exit codes besides 0 (installed and loaded).
EXIT_NOT_INSTALLED = 1
#: Every subcommand's exit code on a platform with no unit here.
EXIT_UNSUPPORTED = 2
EXIT_NOT_LOADED = 3

#: How long one launchctl or systemctl command may take beyond the unit's
#: stop bound (`service_units.STOP_TIMEOUT_SECONDS`): a bootout or a
#: restart waits for the daemon to stop.
COMMAND_MARGIN_SECONDS = 30

#: The most of a failed command's error output carried into a report.
_ERROR_CHARS = 500


def add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    """Register ``service`` and its four subcommands."""
    help_text = (
        "Run the local daemon as a supervised service: a launchd LaunchAgent "
        "on macOS, a systemd user unit on Linux, started at login and again "
        "after a crash. `install` writes and loads the unit, `uninstall` "
        "unloads and removes it, `status` reports it, `print` writes it to "
        "stdout only. Exit 2 on other platforms, with the command to "
        "supervise yourself."
    )
    parser = sub.add_parser("service", help=help_text, description=help_text)
    service_sub = parser.add_subparsers(dest="service_cmd")

    install_text = (
        "Write the unit for this install and this store, stop a daemon of the "
        "store running outside the supervisor, and load the unit, which starts "
        "the daemon. Exit 0 when written and loaded, 1 when loading failed or "
        "the interpreter lives in a package manager's cache."
    )
    install = service_sub.add_parser(
        "install", help=install_text, description=install_text
    )
    install.add_argument(
        "--no-load",
        action="store_true",
        help="Write the unit only; print the commands that load it.",
    )
    install.add_argument("--json", action="store_true", help="Print JSON.")

    uninstall_text = (
        "Unload the unit, which stops its daemon, and remove the file. Exit 0 "
        "when removed or not installed, 1 when the file could not be removed."
    )
    uninstall = service_sub.add_parser(
        "uninstall", help=uninstall_text, description=uninstall_text
    )
    uninstall.add_argument(
        "--no-load",
        action="store_true",
        help="Remove the file only; leave the supervisor's state alone.",
    )
    uninstall.add_argument("--json", action="store_true", help="Print JSON.")

    status_text = (
        "Report the unit: installed or not, whether it matches what `install` "
        "would write now, whether the supervisor has it loaded, and the "
        "daemon's state. Changes nothing. Exit 0 installed and loaded, 1 not "
        "installed, 3 installed but not loaded."
    )
    status = service_sub.add_parser("status", help=status_text, description=status_text)
    status.add_argument("--json", action="store_true", help="Print JSON.")

    print_text = (
        "Write the unit `install` would write to stdout, for review or for "
        "deployment by other means. Writes no file. Exit 0."
    )
    printer = service_sub.add_parser("print", help=print_text, description=print_text)
    printer.add_argument(
        "--json",
        action="store_true",
        help="Print the unit and its resolved inputs as JSON.",
    )
    return parser


# ---------------------------------------------------------------------------
# The inputs, each behind a function the tests replace
# ---------------------------------------------------------------------------


def _platform() -> str:
    return sys.platform


def _home() -> Path:
    return Path.home()


def _uid() -> int:
    if sys.platform == "win32":
        return 0
    return os.getuid()


def _interpreter() -> tuple[str, str]:
    """This interpreter, as the unit runs it, and its environment's root.
    Not resolved: a venv's python is a link to the base interpreter, and
    resolving it would lose the venv."""
    return sys.executable, sys.prefix


class _Context(NamedTuple):
    spec: units.UnitSpec
    state_dir: Path
    store: Path


def _service_store(pinned: dict[str, str], state_dir: Path) -> Path:
    """The store a daemon started in `state_dir` with `pinned` resolves:
    the config's own rule, read from the state directory. The config is
    loaded only when its file exists, since `load_config` creates a
    default one otherwise and `print` and `status` write nothing; an
    absent file is the defaults."""
    from ..config import STORE_FILENAME, Config, default_config_path, load_config

    named = pinned.get("BETTERMEMORY_DIR")
    if named:
        return Path(named).resolve() / STORE_FILENAME
    config = load_config() if default_config_path().exists() else Config()
    return config.resolved_directory(cwd=state_dir) / STORE_FILENAME


def _context(supervisor: str) -> _Context:
    from .. import service_units as units
    from .._daemon_client import log_file_for, resolve_state_dir

    home = _home()
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = home
    state_dir = resolve_state_dir().expanduser().resolve()
    pinned = units.pinned_environment(
        os.environ, state_dir=state_dir, cwd=cwd, home=home
    )
    store = _service_store(pinned, state_dir)
    spec = units.build_spec(
        supervisor,
        executable=_interpreter()[0],
        env=os.environ,
        home=home,
        cwd=cwd,
        state_dir=state_dir,
        log_path=log_file_for(state_dir, store),
    )
    return _Context(spec=spec, state_dir=state_dir, store=store)


# ---------------------------------------------------------------------------
# Running the supervisor's commands
# ---------------------------------------------------------------------------


def _run_steps(steps: list[units.Step]) -> list[dict[str, Any]]:
    """Run `steps` in order and report each; stop after a required one
    fails. A command that cannot be run at all (no launchctl or systemctl
    on PATH) reports returncode None. Output that does not decode (a path
    in another encoding in `launchctl print`) is replaced, not raised."""
    from .. import service_units as units

    results: list[dict[str, Any]] = []
    for step in steps:
        try:
            done = subprocess.run(
                list(step.argv),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=units.STOP_TIMEOUT_SECONDS + COMMAND_MARGIN_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            code: int | None = None
            out, err = "", str(exc)
        else:
            code, out, err = done.returncode, done.stdout or "", done.stderr or ""
        results.append(
            {
                "command": shlex.join(step.argv),
                "returncode": code,
                "required": step.required,
                "stdout": out,
                "error": (err.strip() or out.strip())[:_ERROR_CHARS]
                if code != 0
                else "",
            }
        )
        if code != 0 and step.required:
            break
    return results


def _failed(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    for result in results:
        if result["required"] and result["returncode"] != 0:
            return result
    return None


def _public(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`results` for a report: the command, its status and its error,
    without a status command's full output."""
    return [
        {key: result[key] for key in ("command", "returncode", "error")}
        for result in results
    ]


def _hand_over(state_dir: Path, store: Path) -> dict[str, Any] | None:
    """Stop a running daemon of `store` before the unit is loaded, so the
    unit's daemon takes the store's lock at once instead of exiting 75
    and retrying. Only a daemon that answers as the one its state file
    names (`daemon_cmd.is_this_daemon`) is asked; None when none runs.
    A port held by a listener that does not speak HTTP, which `health`
    lets escape as `http.client.HTTPException`, is passed over the same
    way instead of ending the install with a traceback."""
    import http.client

    from .._daemon_client import health, read_state, remove_state, shutdown_daemon
    from .daemon_cmd import is_this_daemon

    state = read_state(state_dir, store)
    if state is None:
        return None
    try:
        answer = health(state["port"], timeout=1.0)
    except http.client.HTTPException:
        return None
    if answer is None or not is_this_daemon(answer, state):
        return None
    stopped = shutdown_daemon(state, state_dir=state_dir)
    if stopped:
        remove_state(state_dir, store, pid=int(state["pid"]))
    return {"pid": int(state["pid"]), "stopped": stopped}


# ---------------------------------------------------------------------------
# The subcommands
# ---------------------------------------------------------------------------


def _emit(
    payload: dict[str, Any], *, json_out: bool, text: str, err: bool = False
) -> None:
    if json_out:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    elif text:
        stream = sys.stderr if err else sys.stdout
        stream.write(text if text.endswith("\n") else text + "\n")


def _describe(ctx: _Context) -> dict[str, Any]:
    spec = ctx.spec
    return {
        "supervisor": spec.supervisor,
        "unit_path": str(spec.path),
        "command": list(spec.command),
        "environment": dict(spec.environment),
        "working_directory": str(spec.working_directory),
        "store": str(ctx.store),
        "log_file": str(spec.log_path),
    }


def _unsupported(json_out: bool) -> int:
    from .. import service_units as units
    from .._daemon_client import resolve_state_dir

    command = units.daemon_command(_interpreter()[0])
    text = units.manual_instructions(
        command, state_dir=resolve_state_dir(), windows=_platform() == "win32"
    )
    payload = {
        "supported": False,
        "platform": _platform(),
        "command": list(command),
        "instructions": text,
    }
    _emit(payload, json_out=json_out, text=text)
    return EXIT_UNSUPPORTED


def _owner_only_log(path: Path) -> None:
    """Create the daemon's log owner-only before the supervisor first opens
    it, and tighten one that exists. The supervisor opens the log for
    appending, which keeps the mode a file already has; what mode it would
    create one with is the supervisor's own (launchd's and systemd's
    documentation do not say that the unit's umask applies to it)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def _install(ctx: _Context, *, load: bool, json_out: bool) -> int:
    from .. import service_units as units
    from .._fsutil import atomic_write_bytes, ensure_owner_only_dir

    spec = ctx.spec
    payload: dict[str, Any] = {**_describe(ctx), "written": False, "loaded": False}
    _, prefix = _interpreter()
    if units.ephemeral_interpreter(prefix, os.environ):
        payload["error"] = (
            f"this bettermemory runs from a package manager's cache ({prefix}), "
            "which can be deleted under the unit. Install it to stay (`uv tool "
            "install bettermemory` or `pipx install bettermemory`) and run "
            "`bettermemory service install` from that install."
        )
        _emit(
            payload,
            json_out=json_out,
            text=f"bettermemory service: {payload['error']}",
            err=True,
        )
        return 1
    try:
        # The daemon's working directory and its log's directory: launchd
        # and systemd both fail the start when either is missing.
        ensure_owner_only_dir(ctx.state_dir, parents=True)
        _owner_only_log(spec.log_path)
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(spec.path, units.render(spec).encode("utf-8"))
    except OSError as exc:
        payload["error"] = f"writing the unit failed: {exc}"
        _emit(
            payload,
            json_out=json_out,
            text=f"bettermemory service: {payload['error']}",
            err=True,
        )
        return 1
    payload["written"] = True
    lines = [
        f"bettermemory service: wrote {spec.path} ({spec.supervisor})",
        f"  runs   {shlex.join(spec.command)}",
        f"  store  {ctx.store}",
        f"  log    {spec.log_path}",
    ]
    steps = units.load_steps(spec, uid=_uid())
    if not load:
        payload["load_commands"] = [shlex.join(step.argv) for step in steps]
        lines.append("  not loaded (--no-load); load it with:")
        lines += [
            f"    {shlex.join(step.argv)}"
            + ("" if step.required else "   (fails harmlessly when not loaded)")
            for step in steps
        ]
    else:
        handed = _hand_over(ctx.state_dir, ctx.store)
        payload["handed_over"] = handed
        if handed is not None:
            lines.append(
                f"  stopped the daemon serving the store outside the supervisor "
                f"(pid {handed['pid']})"
                if handed["stopped"]
                else f"  the daemon serving the store (pid {handed['pid']}) did not "
                "stop; the unit's daemon starts once it has"
            )
        results = _run_steps(steps)
        payload["commands"] = _public(results)
        failed = _failed(results)
        if failed is not None:
            payload["error"] = (
                f"`{failed['command']}` exited {failed['returncode']}: {failed['error']}"
            )
            lines.append(f"  loading failed: {payload['error']}")
            _emit(payload, json_out=json_out, text="\n".join(lines), err=True)
            return 1
        payload["loaded"] = True
        lines.append(f"  loaded ({results[-1]['command']})")
    if spec.supervisor == units.SYSTEMD:
        lines.append(
            "  a systemd user unit runs while you are logged in; "
            "`loginctl enable-linger` keeps it running without a session"
        )
    _emit(payload, json_out=json_out, text="\n".join(lines))
    return 0


def _uninstall(ctx: _Context, *, load: bool, json_out: bool) -> int:
    from .. import service_units as units

    spec = ctx.spec
    installed = spec.path.exists()
    payload: dict[str, Any] = {
        **_describe(ctx),
        "installed": installed,
        "removed": False,
        "commands": [],
    }
    lines: list[str] = []
    if load:
        results = _run_steps(units.unload_steps(spec, uid=_uid()))
        payload["commands"] = _public(results)
        unloaded = [r["command"] for r in results if r["returncode"] == 0]
        if unloaded:
            lines.append(f"bettermemory service: unloaded ({unloaded[0]})")
    if installed:
        try:
            spec.path.unlink()
        except OSError as exc:
            payload["error"] = f"removing {spec.path} failed: {exc}"
            lines.append(f"bettermemory service: {payload['error']}")
            _emit(payload, json_out=json_out, text="\n".join(lines), err=True)
            return 1
        payload["removed"] = True
        lines.append(f"bettermemory service: removed {spec.path}")
    else:
        lines.append(f"bettermemory service: not installed (no unit at {spec.path})")
    if load:
        payload["commands"] += _public(_run_steps(units.after_removal_steps(spec)))
    _emit(payload, json_out=json_out, text="\n".join(lines))
    return 0


def _supervisor_view(spec: units.UnitSpec) -> dict[str, Any]:
    """What the supervisor says about the unit: loaded (None when it
    cannot be asked), active, and the details it gave."""
    from .. import service_units as units

    results = _run_steps(units.status_steps(spec, uid=_uid()))
    if any(result["returncode"] is None for result in results):
        return {"loaded": None, "active": None, "detail": {}}
    if spec.supervisor == units.LAUNCHD:
        printed = results[0]
        detail = (
            units.parse_launchctl_print(printed["stdout"])
            if printed["returncode"] == 0
            else {}
        )
        return {
            "loaded": printed["returncode"] == 0,
            "active": detail.get("state") == "running",
            "detail": detail,
        }
    enabled = results[0]["stdout"].strip()
    active = results[1]["stdout"].strip() if len(results) > 1 else ""
    return {
        "loaded": enabled == "enabled",
        "active": active == "active",
        "detail": {"enabled": enabled, "active": active},
    }


def _status(ctx: _Context, *, json_out: bool) -> int:
    from .. import service_units as units
    from .daemon_cmd import daemon_report

    spec = ctx.spec
    installed = spec.path.is_file()
    current: bool | None = None
    if installed:
        try:
            current = spec.path.read_text(encoding="utf-8") == units.render(spec)
        except OSError:
            current = None
    view = _supervisor_view(spec)
    daemon = daemon_report(ctx.state_dir, ctx.store, tidy=False)
    if not installed:
        code = EXIT_NOT_INSTALLED
    elif view["loaded"]:
        code = 0
    else:
        code = EXIT_NOT_LOADED
    payload = {
        **_describe(ctx),
        "installed": installed,
        "current": current,
        "loaded": view["loaded"],
        "active": view["active"],
        "supervisor_detail": view["detail"],
        "daemon": daemon,
        "check": code,
    }
    state = "installed" if installed else "not installed"
    if current is False:
        state += ", differs from what `service install` would write now"
    loaded = {True: "yes", False: "no", None: "unknown"}[view["loaded"]]
    detail = ", ".join(f"{key} {value}" for key, value in view["detail"].items())
    lines = [
        f"bettermemory service: {spec.supervisor} unit {spec.path}: {state}",
        f"  loaded: {loaded}" + (f" ({detail})" if detail else ""),
        f"  {daemon['message']}",
    ]
    _emit(payload, json_out=json_out, text="\n".join(lines))
    return code


def _print(ctx: _Context, *, json_out: bool) -> int:
    from .. import service_units as units

    text = units.render(ctx.spec)
    if json_out:
        _emit({**_describe(ctx), "unit": text}, json_out=True, text="")
    else:
        sys.stdout.write(text)
    return 0


def run(args: argparse.Namespace, *, sub_parser: argparse.ArgumentParser) -> None:
    """Dispatch handler for ``bettermemory service ...``."""
    from .. import service_units as units

    command = getattr(args, "service_cmd", None)
    if command is None:
        sub_parser.print_help()
        raise SystemExit(2)
    json_out = bool(getattr(args, "json", False))
    supervisor = units.supervisor_for(_platform())
    if supervisor is None:
        raise SystemExit(_unsupported(json_out))
    ctx = _context(supervisor)
    load = not getattr(args, "no_load", False)
    if command == "install":
        raise SystemExit(_install(ctx, load=load, json_out=json_out))
    if command == "uninstall":
        raise SystemExit(_uninstall(ctx, load=load, json_out=json_out))
    if command == "status":
        raise SystemExit(_status(ctx, json_out=json_out))
    if command == "print":
        raise SystemExit(_print(ctx, json_out=json_out))
    sub_parser.error(f"unknown service subcommand: {command!r}")


__all__ = [
    "COMMAND_MARGIN_SECONDS",
    "EXIT_NOT_INSTALLED",
    "EXIT_NOT_LOADED",
    "EXIT_UNSUPPORTED",
    "add_subparser",
    "run",
]
