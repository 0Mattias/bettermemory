"""`bettermemory service` and the units it writes (`service_units`).

The unit text, its path and the load commands are pure functions of the
paths, the environment and the platform, so most of this file renders
units for both supervisors on every OS in the CI matrix and reads them
back. The CLI tests replace the platform, the home directory and
`subprocess.run`: launchctl and systemctl are asserted as command lists
and never run. One POSIX test runs the unit's own command line the way
a supervisor would (its environment, working directory, umask and log),
and stops it with the supervisor's signal.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client, daemon, service_units as units
from bettermemory._daemon_client import (
    DAEMON_HELD_STATUS,
    SHUTDOWN_TIMEOUT_SECONDS,
    health,
    lock_file_for,
    log_file_for,
    read_state,
    resolved_store_path,
    state_file_for,
    write_state,
)
from bettermemory.cli import service_cmd
from bettermemory.server import main as cli_main

from .conftest import shielded_child_env

_EXE = "/opt/bm/bin/python"


def _spec(
    supervisor: str,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    executable: str = _EXE,
) -> units.UnitSpec:
    state_dir = tmp_path / "state"
    return units.build_spec(
        supervisor,
        executable=executable,
        env=env if env is not None else {"PATH": "/usr/bin:/bin"},
        home=tmp_path / "home",
        cwd=tmp_path / "cwd",
        state_dir=state_dir,
        log_path=state_dir / "daemon-0123456789abcdef.log",
    )


# ---------------------------------------------------------------------------
# The pure layer
# ---------------------------------------------------------------------------


def test_the_supervisor_follows_the_platform() -> None:
    assert units.supervisor_for("darwin") == units.LAUNCHD
    assert units.supervisor_for("linux") == units.SYSTEMD
    assert units.supervisor_for("win32") is None
    assert units.supervisor_for("freebsd14") is None


def test_unit_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert units.unit_path(units.LAUNCHD, home=home, env={}) == (
        home / "Library" / "LaunchAgents" / "io.github.0mattias.bettermemory.plist"
    )
    assert units.unit_path(units.SYSTEMD, home=home, env={}) == (
        home / ".config" / "systemd" / "user" / "bettermemory.service"
    )
    xdg = tmp_path / "xdg"
    assert units.unit_path(
        units.SYSTEMD, home=home, env={"XDG_CONFIG_HOME": str(xdg)}
    ) == (xdg / "systemd" / "user" / "bettermemory.service")
    # A relative XDG_CONFIG_HOME is invalid by the spec; systemd ignores it.
    assert units.unit_path(
        units.SYSTEMD, home=home, env={"XDG_CONFIG_HOME": "rel"}
    ) == (home / ".config" / "systemd" / "user" / "bettermemory.service")
    with pytest.raises(ValueError):
        units.unit_path("upstart", home=home, env={})


def test_the_pinned_environment(tmp_path: Path) -> None:
    """The path variables the shell sets are carried absolute, against the
    directory `install` ran in and with ``~`` as home; PATH and the
    daemon's tuning variables as they are; the state directory always;
    the identity variables, which describe a client and not the daemon,
    never."""
    home, cwd, state_dir = tmp_path / "home", tmp_path / "cwd", tmp_path / "state"
    env = {
        "BETTERMEMORY_DIR": "store/../store",
        "BETTERMEMORY_KEYS_DIR": "~/keys",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "PATH": "/usr/local/bin:/usr/bin",
        "BETTERMEMORY_FLOCK_TIMEOUT": "5",
        "BETTERMEMORY_INDEX_THRESHOLD": "200",
        "BETTERMEMORY_STATE_DIR": "elsewhere",
        "BETTERMEMORY_CLIENT": "claude-code",
        "BETTERMEMORY_CLIENT_VERSION": "2.0",
        "BETTERMEMORY_MODEL": "a-model",
        "BETTERMEMORY_WORKSPACE": "/w",
        "HOME": str(home),
    }
    pinned = units.pinned_environment(env, state_dir=state_dir, cwd=cwd, home=home)
    assert pinned == {
        "BETTERMEMORY_DIR": str(cwd / "store"),
        "BETTERMEMORY_FLOCK_TIMEOUT": "5",
        "BETTERMEMORY_INDEX_THRESHOLD": "200",
        "BETTERMEMORY_KEYS_DIR": str(home / "keys"),
        "BETTERMEMORY_STATE_DIR": str(state_dir),
        "PATH": "/usr/local/bin:/usr/bin",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }
    assert list(pinned) == sorted(pinned)
    # An empty value is unset, the resolution rule's own reading.
    bare = units.pinned_environment(
        {"BETTERMEMORY_DIR": ""}, state_dir=state_dir, cwd=cwd, home=home
    )
    assert bare == {"BETTERMEMORY_STATE_DIR": str(state_dir)}


def test_the_launchd_plist_round_trips(tmp_path: Path) -> None:
    spec = _spec(units.LAUNCHD, tmp_path, env={"BETTERMEMORY_DIR": "store"})
    text = units.render(spec)
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<!-- Written by `bettermemory service install`." in text
    plist = plistlib.loads(text.encode("utf-8"))
    assert plist["Label"] == "io.github.0mattias.bettermemory"
    assert plist["ProgramArguments"] == [
        _EXE,
        "-m",
        "bettermemory",
        "up",
        "--foreground",
    ]
    environment = plist["EnvironmentVariables"]
    assert environment == {
        "BETTERMEMORY_DIR": str(tmp_path / "cwd" / "store"),
        "BETTERMEMORY_STATE_DIR": str(tmp_path / "state"),
    }
    assert all(Path(value).is_absolute() for value in environment.values())
    assert plist["WorkingDirectory"] == str(tmp_path / "state")
    assert plist["RunAtLoad"] is True
    # An exit 0 (a `down`, a newer client replacing this daemon) is final.
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == units.RESTART_SECONDS
    assert plist["ExitTimeOut"] == units.STOP_TIMEOUT_SECONDS
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(spec.log_path)
    assert plist["Umask"] == 0o077


def test_the_stop_bound_covers_the_daemons_own() -> None:
    """The supervisor's kill comes after the daemon's own bounded stop:
    the requests in flight, then the workers' join, and a margin for the
    store's close and the interpreter's exit."""
    own = daemon.GRACEFUL_SHUTDOWN_SECONDS + daemon.WORKERS_CLOSE_SECONDS
    assert SHUTDOWN_TIMEOUT_SECONDS == own
    assert units.STOP_TIMEOUT_SECONDS >= own + 5


def test_the_systemd_unit(tmp_path: Path) -> None:
    spec = _spec(units.SYSTEMD, tmp_path)
    text = units.render(spec)
    lines = text.splitlines()
    assert lines[0].startswith("# Written by `bettermemory service install`.")
    assert lines[1] == "[Unit]"
    log = str(spec.log_path)
    state_dir = str(tmp_path / "state")
    # Inside the quotes systemd unescapes C-style, so a Windows runner's
    # temporary path has its backslashes doubled there, and only there.
    quoted_state_dir = state_dir.replace("\\", "\\\\")
    expected = [
        "[Service]",
        "Type=simple",
        f'ExecStart="{_EXE}" "-m" "bettermemory" "up" "--foreground"',
        f"WorkingDirectory={state_dir}",
        f'Environment="BETTERMEMORY_STATE_DIR={quoted_state_dir}"',
        'Environment="PATH=/usr/bin:/bin"',
        "Restart=on-failure",
        f"RestartSec={units.RESTART_SECONDS}",
        "KillMode=mixed",
        f"TimeoutStopSec={units.STOP_TIMEOUT_SECONDS}",
        "UMask=0077",
        "NoNewPrivileges=yes",
        f"StandardOutput=append:{log}",
        f"StandardError=append:{log}",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    start = lines.index("[Service]")
    assert lines[start : start + len(expected)] == expected
    assert text.endswith("\n")


def test_systemd_quoting_of_awkward_values(tmp_path: Path) -> None:
    """A path with a space, a quote, a backslash, a `%` (a specifier to
    systemd) or a `$` (a variable in a command line) is written so systemd
    reads it back as it was; a line break cannot be written at all."""
    awkward = '/opt/my bm/50%/$HOME/"q"\\x/python'
    spec = _spec(
        units.SYSTEMD,
        tmp_path,
        env={"PATH": "/a b:/c%d"},
        executable=awkward,
    )
    text = units.render(spec)
    assert 'ExecStart="/opt/my bm/50%%/$$HOME/\\"q\\"\\\\x/python" "-m"' in text
    assert 'Environment="PATH=/a b:/c%%d"' in text
    with pytest.raises(ValueError, match="line break"):
        units.render(_spec(units.SYSTEMD, tmp_path, executable="/opt/bm\n/python"))


def test_render_refuses_an_unknown_supervisor(tmp_path: Path) -> None:
    spec = _spec(units.LAUNCHD, tmp_path)
    other = units.UnitSpec(
        supervisor="upstart",
        path=spec.path,
        command=spec.command,
        environment=spec.environment,
        working_directory=spec.working_directory,
        log_path=spec.log_path,
    )
    for call in (
        lambda: units.render(other),
        lambda: units.load_steps(other, uid=1),
        lambda: units.unload_steps(other, uid=1),
        lambda: units.status_steps(other, uid=1),
    ):
        with pytest.raises(ValueError):
            call()
    assert units.after_removal_steps(other) == []


def test_the_supervisors_commands(tmp_path: Path) -> None:
    launchd = _spec(units.LAUNCHD, tmp_path)
    target = "gui/501/io.github.0mattias.bettermemory"
    assert units.load_steps(launchd, uid=501) == [
        units.Step(("launchctl", "bootout", target), required=False),
        units.Step(("launchctl", "enable", target)),
        units.Step(("launchctl", "bootstrap", "gui/501", str(launchd.path))),
    ]
    assert units.unload_steps(launchd, uid=501) == [
        units.Step(("launchctl", "bootout", target), required=False)
    ]
    assert units.after_removal_steps(launchd) == []
    assert units.status_steps(launchd, uid=501) == [
        units.Step(("launchctl", "print", target), required=False)
    ]
    systemd = _spec(units.SYSTEMD, tmp_path)
    name = "bettermemory.service"
    assert units.load_steps(systemd, uid=1000) == [
        units.Step(("systemctl", "--user", "daemon-reload")),
        units.Step(("systemctl", "--user", "enable", name)),
        units.Step(("systemctl", "--user", "restart", name)),
    ]
    assert units.unload_steps(systemd, uid=1000) == [
        units.Step(("systemctl", "--user", "disable", "--now", name), required=False)
    ]
    assert units.after_removal_steps(systemd) == [
        units.Step(("systemctl", "--user", "daemon-reload"), required=False)
    ]
    assert [step.argv[2] for step in units.status_steps(systemd, uid=1000)] == [
        "is-enabled",
        "is-active",
    ]


_LAUNCHCTL_PRINT = """gui/501/io.github.0mattias.bettermemory = {
	active count = 1
	path = /Users/u/Library/LaunchAgents/io.github.0mattias.bettermemory.plist
	state = running

	program = /opt/bm/bin/python
	pid = 4242
	last exit code = 75: EX_TEMPFAIL
}
"""


def test_launchctl_print_is_read_for_what_it_reports() -> None:
    assert units.parse_launchctl_print(_LAUNCHCTL_PRINT) == {
        "state": "running",
        "pid": "4242",
        "last_exit": "75: EX_TEMPFAIL",
    }
    assert units.parse_launchctl_print("something else entirely") == {}


@pytest.mark.parametrize(
    ("prefix", "env", "expected"),
    [
        ("/home/u/.cache/uv/archive-v0/AbC123", {}, True),
        ("/Users/u/.cache/uv/builds-v0/.tmpX", {}, True),
        ("/home/u/.cache/uv/environments-v2/bm-1a2b", {}, True),
        ("C:/Users/u/AppData/Local/uv/cache/archive-v0/x", {}, True),
        ("/home/u/.local/pipx/.cache/0123abcd", {}, True),
        ("/Users/u/Library/Caches/pipx/0123abcd", {}, True),
        ("/srv/cache/archive-v0/x", {"UV_CACHE_DIR": "/srv/cache"}, True),
        ("/home/u/.local/share/uv/tools/bettermemory", {}, False),
        ("/home/u/.local/pipx/venvs/bettermemory", {}, False),
        ("/home/u/.venvs/bettermemory", {"UV_CACHE_DIR": "/srv/cache"}, False),
        ("/home/u/projects/archive-v1/venv", {}, False),
    ],
)
def test_an_interpreter_in_a_package_managers_cache_is_ephemeral(
    prefix: str, env: dict[str, str], expected: bool
) -> None:
    assert units.ephemeral_interpreter(prefix, env) is expected


def test_the_manual_instructions_name_the_command(tmp_path: Path) -> None:
    text = units.manual_instructions(units.daemon_command(_EXE), state_dir=tmp_path)
    assert f"{_EXE} -m bettermemory up --foreground" in text
    assert str(tmp_path) in text
    assert str(units.STOP_TIMEOUT_SECONDS) in text


def test_the_manual_instructions_quote_an_interpreter_path_with_a_space(
    tmp_path: Path,
) -> None:
    """An interpreter under ``C:\\Program Files`` is one word on the line
    the user copies, quoted the way the platform's command line reads it."""
    windows = units.daemon_command(r"C:\Program Files\Python313\python.exe")
    text = units.manual_instructions(windows, state_dir=tmp_path, windows=True)
    assert (
        r'"C:\Program Files\Python313\python.exe" -m bettermemory up --foreground'
    ) in text
    posix = units.daemon_command("/opt/my bm/bin/python")
    text = units.manual_instructions(posix, state_dir=tmp_path)
    assert "'/opt/my bm/bin/python' -m bettermemory up --foreground" in text


# ---------------------------------------------------------------------------
# The CLI, with the platform, home and subprocess replaced
# ---------------------------------------------------------------------------


class _Supervisor:
    """`subprocess.run` for the supervisor's commands: records each argv
    and answers with the (returncode, stdout) `reply` gives for it."""

    def __init__(self, reply: Callable[[list[str]], tuple[int, str]] | None = None):
        self.calls: list[list[str]] = []
        self._reply = reply or (lambda argv: (0, ""))

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        code, out = self._reply(list(argv))
        return subprocess.CompletedProcess(argv, code, out, "an error" if code else "")


@pytest.fixture
def svc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], tuple[Path, Path]]:
    """Point `service` at a scratch home, store and state directory, as
    the supervisor `platform` would; return (home, state dir)."""

    def configure(platform: str) -> tuple[Path, Path]:
        home = tmp_path / "home"
        state_dir = tmp_path / "state"
        monkeypatch.setattr(service_cmd, "_platform", lambda: platform)
        monkeypatch.setattr(service_cmd, "_home", lambda: home)
        monkeypatch.setattr(service_cmd, "_uid", lambda: 501)
        monkeypatch.setattr(service_cmd, "_interpreter", lambda: (_EXE, "/opt/bm"))
        monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(state_dir))
        monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "store"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        return home, state_dir

    return configure


def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args: object, **kw: object) -> None:
        raise AssertionError("a supervisor command ran")

    monkeypatch.setattr(subprocess, "run", refused)


def _main(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    try:
        cli_main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else b""
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_print_writes_the_unit_to_stdout_and_nothing_to_disk(
    platform: str,
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    svc(platform)
    _no_subprocess(monkeypatch)
    before = _tree(tmp_path)
    assert _main(["service", "print"], monkeypatch) == 0
    text = capsys.readouterr().out
    assert _main(["service", "print", "--json"], monkeypatch) == 0
    payload = json.loads(capsys.readouterr().out)
    assert _tree(tmp_path) == before
    assert payload["unit"] == text
    store = resolved_store_path()
    assert payload["store"] == str(store)
    assert payload["log_file"] == str(log_file_for(tmp_path / "state", store))
    assert payload["command"] == [_EXE, "-m", "bettermemory", "up", "--foreground"]
    if platform == "darwin":
        assert plistlib.loads(text.encode())["Label"] == units.LAUNCHD_LABEL
        assert payload["supervisor"] == "launchd"
    else:
        assert "[Service]" in text
        assert payload["unit_path"] == str(
            tmp_path / "xdg" / "systemd" / "user" / "bettermemory.service"
        )


def test_install_no_load_writes_one_file_and_runs_nothing(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, state_dir = svc("darwin")
    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--no-load"], monkeypatch) == 0
    out = capsys.readouterr().out
    written = [path for path in home.rglob("*") if path.is_file()]
    unit = home / "Library" / "LaunchAgents" / f"{units.LAUNCHD_LABEL}.plist"
    assert written == [unit]
    plist = plistlib.loads(unit.read_bytes())
    assert plist["EnvironmentVariables"]["BETTERMEMORY_STATE_DIR"] == str(
        state_dir.resolve()
    )
    assert f"wrote {unit} (launchd)" in out
    assert "launchctl bootstrap gui/501" in out
    assert "fails harmlessly when not loaded" in out
    # The daemon's working directory, where its log goes, exists for the
    # supervisor, owner-only.
    assert state_dir.is_dir()
    log = log_file_for(state_dir.resolve(), resolved_store_path())
    assert log.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(unit.stat().st_mode) == 0o600
        assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_install_loads_the_launch_agent(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, _ = svc("darwin")
    supervisor = _Supervisor(lambda argv: (3, "") if argv[1] == "bootout" else (0, ""))
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "install", "--json"], monkeypatch) == 0
    payload = json.loads(capsys.readouterr().out)
    unit = home / "Library" / "LaunchAgents" / f"{units.LAUNCHD_LABEL}.plist"
    target = f"gui/501/{units.LAUNCHD_LABEL}"
    assert supervisor.calls == [
        ["launchctl", "bootout", target],
        ["launchctl", "enable", target],
        ["launchctl", "bootstrap", "gui/501", str(unit)],
    ]
    assert payload["written"] is True
    assert payload["loaded"] is True
    assert payload["handed_over"] is None
    assert [command["returncode"] for command in payload["commands"]] == [3, 0, 0]


def test_install_enables_and_restarts_the_systemd_unit(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    svc("linux")
    supervisor = _Supervisor()
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "install"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert supervisor.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "bettermemory.service"],
        ["systemctl", "--user", "restart", "bettermemory.service"],
    ]
    assert (tmp_path / "xdg" / "systemd" / "user" / "bettermemory.service").is_file()
    assert "loaded (systemctl --user restart bettermemory.service)" in out
    assert "loginctl enable-linger" in out


def test_a_failed_load_is_reported_and_the_unit_kept(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, _ = svc("darwin")
    supervisor = _Supervisor(
        lambda argv: (5, "") if argv[1] in {"bootstrap", "bootout"} else (0, "")
    )
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "install"], monkeypatch) == 1
    err = capsys.readouterr().err
    assert "loading failed: `launchctl bootstrap gui/501" in err
    assert "exited 5: an error" in err
    assert (home / "Library" / "LaunchAgents" / f"{units.LAUNCHD_LABEL}.plist").exists()


def test_a_missing_supervisor_command_fails_the_load(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc("linux")

    def missing(argv: list[str], **kw: object) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    assert _main(["service", "install", "--json"], monkeypatch) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["loaded"] is False
    assert payload["commands"][0]["returncode"] is None
    assert "systemctl" in payload["commands"][0]["error"]


def test_install_hands_the_store_over_from_a_running_daemon(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A daemon of the same store running outside the supervisor is
    stopped before the unit is loaded, so the unit's daemon takes the
    store at once instead of exiting 75 and retrying."""
    _, state_dir = svc("darwin")
    store = resolved_store_path()
    pid = os.getpid()
    write_state(
        state_dir,
        {
            "pid": pid,
            "port": 1,
            "token": "t" * 64,
            "version": "9.0.0",
            "store": str(store),
            "started": "2026-01-01T00:00:00Z",
        },
    )
    asked: list[int] = []

    def stopped(state: dict[str, Any], *, state_dir: Path, **kw: Any) -> bool:
        asked.append(int(state["pid"]))
        return True

    monkeypatch.setattr(
        _daemon_client,
        "health",
        lambda port, timeout=1.0, nonce=None: {
            "status": "ok",
            "store": str(store),
            "pid": pid,
        },
    )
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", stopped)
    supervisor = _Supervisor()
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "install"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert asked == [pid]
    assert (
        f"stopped the daemon serving the store outside the supervisor (pid {pid})"
        in out
    )
    assert not state_file_for(state_dir, store).exists()
    assert supervisor.calls[-1][1] == "bootstrap"


def test_install_reports_a_daemon_that_would_not_hand_over(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, state_dir = svc("linux")
    store = resolved_store_path()
    pid = os.getpid()
    write_state(
        state_dir,
        {
            "pid": pid,
            "port": 1,
            "token": "t" * 64,
            "version": "9.0.0",
            "store": str(store),
            "started": "2026-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        _daemon_client,
        "health",
        lambda port, timeout=1.0, nonce=None: {
            "status": "ok",
            "store": str(store),
            "pid": pid,
        },
    )
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", lambda state, **kw: False)
    monkeypatch.setattr(subprocess, "run", _Supervisor())
    assert _main(["service", "install"], monkeypatch) == 0
    assert "did not stop; the unit's daemon starts once it has" in (
        capsys.readouterr().out
    )
    assert read_state(state_dir, store) is not None


def test_install_passes_over_a_port_that_does_not_speak_http(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A listener on the state file's port that answers in another
    protocol makes `health` raise, not return; `install` asks nothing of
    it, stops nothing, and loads the unit."""
    _, state_dir = svc("darwin")
    store = resolved_store_path()
    write_state(
        state_dir,
        {
            "pid": os.getpid(),
            "port": 1,
            "token": "t" * 64,
            "version": "9.0.0",
            "store": str(store),
            "started": "2026-01-01T00:00:00Z",
        },
    )

    def not_http(port: int, timeout: float = 1.0, nonce: str | None = None) -> None:
        # What `health` makes of a reply that is not HTTP: nothing answered.
        return None

    def never(state: dict[str, Any], **kw: Any) -> bool:
        raise AssertionError("shutdown_daemon reached a listener that is not ours")

    monkeypatch.setattr(_daemon_client, "health", not_http)
    monkeypatch.setattr(_daemon_client, "shutdown_daemon", never)
    supervisor = _Supervisor()
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "install", "--json"], monkeypatch) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["handed_over"] is None
    assert payload["loaded"] is True
    assert supervisor.calls[-1][1] == "bootstrap"


def test_install_refuses_an_interpreter_in_a_package_managers_cache(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, _ = svc("darwin")
    cache = "/Users/u/.cache/uv/archive-v0/AbC123"
    monkeypatch.setattr(
        service_cmd, "_interpreter", lambda: (f"{cache}/bin/python", cache)
    )
    _no_subprocess(monkeypatch)
    assert _main(["service", "install"], monkeypatch) == 1
    err = capsys.readouterr().err
    assert "package manager's cache" in err
    assert "uv tool install bettermemory" in err
    assert not home.exists()


def test_install_reports_a_unit_it_cannot_write(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, _ = svc("darwin")
    home.mkdir()
    # A file where the LaunchAgents directory should be.
    (home / "Library").write_text("not a directory")
    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--json"], monkeypatch) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] is False
    assert "writing" in payload["error"]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_uninstall_unloads_and_removes_the_unit(
    platform: str,
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc(platform)
    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--no-load"], monkeypatch) == 0
    capsys.readouterr()
    supervisor = _Supervisor()
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "uninstall", "--json"], monkeypatch) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert payload["removed"] is True
    assert not Path(payload["unit_path"]).exists()
    if platform == "darwin":
        assert supervisor.calls == [
            ["launchctl", "bootout", f"gui/501/{units.LAUNCHD_LABEL}"]
        ]
    else:
        assert supervisor.calls == [
            ["systemctl", "--user", "disable", "--now", "bettermemory.service"],
            ["systemctl", "--user", "daemon-reload"],
        ]


def test_uninstall_without_a_unit_and_without_loading(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc("darwin")
    _no_subprocess(monkeypatch)
    assert _main(["service", "uninstall", "--no-load"], monkeypatch) == 0
    assert "not installed (no unit at" in capsys.readouterr().out
    supervisor = _Supervisor(lambda argv: (113, ""))
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "uninstall"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "unloaded" not in out
    assert "not installed" in out


def test_uninstall_reports_a_unit_it_cannot_remove(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc("darwin")
    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--no-load"], monkeypatch) == 0
    capsys.readouterr()

    def refused(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(self)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "unlink", refused)
        assert _main(["service", "uninstall", "--no-load"], monkeypatch) == 1
    assert "removing" in capsys.readouterr().err


def test_status_reports_the_launch_agent(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc("darwin")
    supervisor = _Supervisor(lambda argv: (113, "Could not find service"))
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "status"], monkeypatch) == service_cmd.EXIT_NOT_INSTALLED
    assert ": not installed" in capsys.readouterr().out

    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--no-load"], monkeypatch) == 0
    capsys.readouterr()
    monkeypatch.setattr(subprocess, "run", supervisor)
    assert _main(["service", "status"], monkeypatch) == service_cmd.EXIT_NOT_LOADED
    assert "loaded: no" in capsys.readouterr().out

    loaded = _Supervisor(lambda argv: (0, _LAUNCHCTL_PRINT))
    monkeypatch.setattr(subprocess, "run", loaded)
    before = _tree(Path(os.environ["BETTERMEMORY_STATE_DIR"]))
    assert _main(["service", "status", "--json"], monkeypatch) == 0
    payload = json.loads(capsys.readouterr().out)
    assert loaded.calls == [["launchctl", "print", f"gui/501/{units.LAUNCHD_LABEL}"]]
    assert payload["installed"] is True
    assert payload["current"] is True
    assert payload["loaded"] is True
    assert payload["active"] is True
    assert payload["supervisor_detail"]["pid"] == "4242"
    assert payload["daemon"]["state"] == "stopped"
    assert payload["check"] == 0
    assert _tree(Path(os.environ["BETTERMEMORY_STATE_DIR"])) == before


def test_status_reports_the_systemd_unit(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc("linux")
    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--no-load"], monkeypatch) == 0
    capsys.readouterr()

    def answers(enabled: str, active: str) -> _Supervisor:
        return _Supervisor(
            lambda argv: (
                (0, enabled + "\n") if argv[2] == "is-enabled" else (0, active + "\n")
            )
        )

    monkeypatch.setattr(subprocess, "run", answers("enabled", "active"))
    assert _main(["service", "status"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "loaded: yes (enabled enabled, active active)" in out
    monkeypatch.setattr(subprocess, "run", answers("disabled", "inactive"))
    assert _main(["service", "status"], monkeypatch) == service_cmd.EXIT_NOT_LOADED


def test_status_when_the_supervisor_cannot_be_asked(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc("linux")
    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--no-load"], monkeypatch) == 0
    capsys.readouterr()

    def missing(argv: list[str], **kw: object) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    assert _main(["service", "status"], monkeypatch) == service_cmd.EXIT_NOT_LOADED
    assert "loaded: unknown" in capsys.readouterr().out


def test_status_flags_a_unit_that_install_would_now_write_differently(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    svc("darwin")
    _no_subprocess(monkeypatch)
    assert _main(["service", "install", "--no-load"], monkeypatch) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        service_cmd, "_interpreter", lambda: ("/new/bin/python", "/new")
    )
    monkeypatch.setattr(subprocess, "run", _Supervisor(lambda argv: (0, "")))
    assert _main(["service", "status", "--json"], monkeypatch) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["current"] is False
    monkeypatch.setattr(subprocess, "run", _Supervisor(lambda argv: (0, "")))
    assert _main(["service", "status"], monkeypatch) == 0
    assert "differs from what `service install` would write now" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("command", ["install", "uninstall", "status", "print"])
def test_other_platforms_get_the_command_to_supervise(
    command: str,
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    svc("win32")
    _no_subprocess(monkeypatch)
    assert _main(["service", command], monkeypatch) == service_cmd.EXIT_UNSUPPORTED
    out = capsys.readouterr().out
    assert f"{_EXE} -m bettermemory up --foreground" in out
    assert not (tmp_path / "home").exists()
    assert _main(["service", command, "--json"], monkeypatch) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["supported"] is False
    assert payload["platform"] == "win32"


def test_service_without_a_subcommand_prints_its_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main(["service"], monkeypatch) == 2
    assert "install" in capsys.readouterr().out


def test_the_store_is_the_one_a_daemon_in_the_state_directory_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With BETTERMEMORY_DIR unset the unit's daemon resolves its store from
    the state directory, its working directory: a project's
    ./.claude-memory where `install` ran is not the unit's store. With no
    config file the defaults decide, and none is created."""
    from bettermemory import config as _config

    monkeypatch.delenv("BETTERMEMORY_DIR", raising=False)
    project = tmp_path / "project"
    (project / ".claude-memory").mkdir(parents=True)
    monkeypatch.chdir(project)
    state_dir = tmp_path / "state"
    store = service_cmd._service_store({}, state_dir)
    assert store != (project / ".claude-memory" / "memory.sqlite").resolve()
    assert store == _config.load_config().resolved_directory(cwd=state_dir) / (
        "memory.sqlite"
    )
    missing = tmp_path / "no-config" / "config.toml"
    monkeypatch.setattr(_config, "default_config_path", lambda: missing)
    assert service_cmd._service_store({}, state_dir).name == "memory.sqlite"
    assert not missing.parent.exists()
    named = service_cmd._service_store(
        {"BETTERMEMORY_DIR": str(tmp_path / "s")}, state_dir
    )
    assert named == (tmp_path / "s").resolve() / "memory.sqlite"


@pytest.mark.parametrize("source", ["config", "managed"])
@pytest.mark.parametrize("command", ["install", "uninstall", "status", "print"])
def test_a_config_that_does_not_load_ends_with_its_reason_and_2(
    command: str,
    source: str,
    svc: Callable[[str], tuple[Path, Path]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_config_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With BETTERMEMORY_DIR unset the unit's store comes from the config.
    One that does not load, a malformed config.toml or managed file, ended
    every subcommand in a ValueError traceback and exit 1, which `status`
    documents as "not installed". It ends with the loader's message and
    2 now, and nothing runs or is written."""
    from bettermemory import config as _config

    svc("darwin")
    monkeypatch.delenv("BETTERMEMORY_DIR", raising=False)
    config_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(_config, "default_config_path", lambda: config_path)
    if source == "config":
        config_path.parent.mkdir(parents=True)
        config_path.write_text("storage = 5\n", encoding="utf-8")
    else:
        managed_config_path.write_text("daemon = 5\n", encoding="utf-8")
    _no_subprocess(monkeypatch)
    argv = ["service", command]
    if command in ("install", "uninstall"):
        argv.append("--no-load")
    assert _main(argv, monkeypatch) == service_cmd.EXIT_NO_CONFIG
    err = capsys.readouterr().err
    assert "the config does not load" in err and "Traceback" not in err
    assert not (tmp_path / "home").exists()
    assert config_path.exists() == (source == "config")


def test_a_command_whose_output_does_not_decode_is_still_reported() -> None:
    """A supervisor command's output is read as text with what does not
    decode replaced: bytes in another encoding (a path in `launchctl
    print`) must not end `service status` with a UnicodeDecodeError."""
    script = "import sys; sys.stdout.buffer.write(b'state = running\\xff\\n')"
    [result] = service_cmd._run_steps([units.Step((sys.executable, "-c", script))])
    assert result["returncode"] == 0
    assert result["stdout"].startswith("state = running")


def test_the_context_falls_back_to_home_when_the_cwd_is_gone(
    svc: Callable[[str], tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, _ = svc("darwin")

    def gone() -> Path:
        raise FileNotFoundError("cwd")

    monkeypatch.setenv("BETTERMEMORY_KEYS_DIR", "keys")
    with monkeypatch.context() as patched:
        patched.setattr(Path, "cwd", gone)
        ctx = service_cmd._context(units.LAUNCHD)
    assert dict(ctx.spec.environment)["BETTERMEMORY_KEYS_DIR"] == str(home / "keys")


# ---------------------------------------------------------------------------
# The unit's own command line, run the way a supervisor runs it
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="no launchd or systemd unit on Windows"
)
def test_the_units_command_serves_under_a_supervisor_and_stops_on_sigterm(
    tmp_path: Path,
) -> None:
    """The unit's command, environment, working directory, umask and log,
    as launchd or systemd apply them: the daemon serves the pinned store,
    writes to the unit's log, creates its files owner-only under the
    unit's umask, and leaves its
    state directory as it found it on SIGTERM, exiting 0, which neither
    unit restarts. A second copy started meanwhile, as a supervisor
    restarts one beside a daemon a hook spawned, exits 75 without
    serving. `--port 0` is the one addition, so the test never binds the
    default port a real daemon on this machine may want."""
    state_dir = (tmp_path / "state").resolve()
    state_dir.mkdir(mode=0o700)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "BETTERMEMORY_DIR": str(tmp_path / "store"),
        "BETTERMEMORY_KEYS_DIR": str(tmp_path / "keys"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    store = (tmp_path / "store").resolve() / "memory.sqlite"
    spec = units.build_spec(
        units.supervisor_for(sys.platform) or units.SYSTEMD,
        executable=sys.executable,
        env=env,
        home=home,
        cwd=tmp_path,
        state_dir=state_dir,
        log_path=log_file_for(state_dir, store),
    )
    # What the supervisor hands the process: the unit's environment and
    # little else (HOME, as launchd and systemd set it), and the import
    # shield the suite gives every child.
    child_env = dict(spec.environment)
    child_env["HOME"] = str(home)
    child_env["PYTHONPATH"] = shielded_child_env()["PYTHONPATH"]
    command = [*spec.command, "--port", "0"]

    def start() -> subprocess.Popen[bytes]:
        with open(spec.log_path, "ab") as log:
            return subprocess.Popen(
                command,
                env=child_env,
                cwd=str(spec.working_directory),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                umask=0o077,
            )

    proc = start()
    second: subprocess.Popen[bytes] | None = None
    try:
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            state = read_state(state_dir, store)
            if state is not None and health(state["port"], timeout=0.5) is not None:
                break
            assert proc.poll() is None, spec.log_path.read_text(errors="replace")
            time.sleep(0.05)
        assert state is not None, spec.log_path.read_text(errors="replace")
        assert state["store"] == str(store)
        # A file the daemon creates itself, under the unit's umask.
        lock = lock_file_for(state_dir, store)
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600

        second = start()
        assert second.wait(timeout=60) == DAEMON_HELD_STATUS
        assert read_state(state_dir, store) == state

        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 0
    finally:
        for child in (proc, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=15)
    log = spec.log_path.read_text(errors="replace")
    assert "bettermemory daemon" in log
    assert "already serves" in log
    assert not state_file_for(state_dir, store).exists()
