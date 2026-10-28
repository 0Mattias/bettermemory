"""`bettermemory doctor`: the read-only install and runtime self-check.

Each check is driven to ok, warn and fail through its inputs: the
environment, a faked home with Claude Code and client config files, a
store created under the test's directory, a daemon state file with a
monkeypatched or stub `/health`, a faked PATH and a faked disk. The
command itself is run through the CLI for the JSON shape and the exit
codes, and once over a tree that holds every kind of file doctor reads,
snapshotted before and after: doctor writes nothing, creates no config
and rekeys nothing.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sqlite3
import sys
import threading
from collections import namedtuple
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import bettermemory
from bettermemory import _daemon_client, doctor
from bettermemory import config as _config
from bettermemory.cli import _build_parser
from bettermemory.cli import main as cli_main
from bettermemory.init import KNOWN_CLIENTS
from bettermemory.store import STORE_FILENAME, Store

_Usage = namedtuple("_Usage", "total used free")
_GiB = 1024**3
_MiB = 1024**2
#: Above any pid a POSIX kernel hands out (Linux caps at 2**22, macOS at
#: 99999), so `pid_alive` reads it as gone without a monkeypatch.
_DEAD_PID = 2**22 + 10_007


def _home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point `Path.home()` at `home` on every platform: HOME for the POSIX
    `expanduser`, USERPROFILE for the Windows one, with HOMEDRIVE and
    HOMEPATH cleared so USERPROFILE wins, and APPDATA under it for the
    Windows client paths."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))


def _word(path: Path) -> str:
    """A path as one word of a hook command, on either shell grammar."""
    return f'"{path}"'


@dataclass
class Env:
    tmp: Path
    home: Path
    store_dir: Path
    bin_dir: Path
    monkeypatch: pytest.MonkeyPatch
    on_path: dict[str, Path] = field(default_factory=dict)
    free: int = 100 * _GiB

    def put_on_path(self, name: str, content: bytes = b"#!/bin/sh\n") -> Path:
        """A fake executable `name` that the faked PATH finds."""
        path = self.bin_dir / name
        path.write_bytes(content)
        self.on_path[name] = path
        return path

    def make_store(self, **kwargs: Any) -> Path:
        store = Store.create(self.store_dir, **kwargs)
        path = store.path
        store.close()
        return path

    def write_json(self, path: Path, data: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def settings(self, data: dict[str, Any]) -> Path:
        return self.write_json(self.home / ".claude" / "settings.json", data)

    def plugin(self, *, uvx: bool = True) -> None:
        self.settings({"enabledPlugins": {"bettermemory@bettermemory": True}})
        if uvx:
            self.put_on_path("uvx")

    def checks(self, cwd: Path | None = None) -> dict[str, doctor.Check]:
        return {check.id: check for check in doctor.run_checks(cwd=cwd)}

    def config(self, text: str) -> Path:
        path = self.tmp / "cfg" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.monkeypatch.setattr(_config, "default_config_path", lambda: path)
        return path


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    """A faked home, the store directory named by BETTERMEMORY_DIR (not
    created), an empty PATH, a roomy disk, and no install binary to
    compare against. The state directory and the keys directory are the
    ones conftest names for the test."""
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    store_dir = tmp_path / "store"
    monkeypatch.setenv("BETTERMEMORY_DIR", str(store_dir))
    state = Env(tmp_path, home, store_dir, bin_dir, monkeypatch)

    def which(name: str, *args: Any, **kwargs: Any) -> str | None:
        found = state.on_path.get(name)
        return None if found is None else str(found)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(doctor, "this_install_binary", lambda: None)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda path: _Usage(state.free * 2, state.free, state.free),
    )
    yield state
    # A state file a test wrote names no daemon: remove it before
    # conftest's teardown would ask that daemon to stop.
    for leftover in _daemon_client.resolve_state_dir().glob("daemon-*.json"):
        leftover.unlink()


def _state(env: Env, **overrides: Any) -> Path:
    store_path = env.store_dir / STORE_FILENAME
    state: dict[str, Any] = {
        "pid": _DEAD_PID,
        "port": 9,
        "token": "t" * 32,
        "version": _daemon_client.package_version(),
        "store": str(store_path),
        "started": "2026-10-27T00:00:00Z",
    }
    state.update(overrides)
    return _daemon_client.write_state(_daemon_client.resolve_state_dir(), state)


def _lock_path(env: Env) -> Path:
    return _daemon_client.lock_file_for(
        _daemon_client.resolve_state_dir(), env.store_dir / STORE_FILENAME
    )


@contextmanager
def _held_lock(env: Env) -> Iterator[None]:
    handle = _daemon_client.try_lock(_lock_path(env))
    assert handle is not None
    try:
        yield
    finally:
        _daemon_client.release_lock(handle)


@contextmanager
def _health_stub(body: dict[str, Any]) -> Iterator[int]:
    """A listener on 127.0.0.1 that answers every GET with `body`: the
    real `_daemon_client.health` call, over the wire."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    # One thread serves every request, so `shutdown` leaves none behind.
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _run_cli(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    code = exc.value.code
    assert isinstance(code, int)
    return code, capsys.readouterr().out


def _tree(*roots: Path) -> dict[str, Any]:
    """Every entry under `roots`: directories by name, files by name, size
    and modification time."""
    out: dict[str, Any] = {}
    for root in roots:
        if not root.exists():
            out[str(root)] = "absent"
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                out[os.path.join(dirpath, name)] = "dir"
            for name in filenames:
                path = os.path.join(dirpath, name)
                info = os.lstat(path)
                out[path] = (info.st_size, info.st_mtime_ns)
    return out


# ---------------------------------------------------------------------------
# The command: shape, exit codes, registration, never writes
# ---------------------------------------------------------------------------


def test_a_healthy_install_is_all_ok_and_exits_0(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.make_store()
    env.plugin()
    code, out = _run_cli(["doctor", "--json"], monkeypatch, capsys)
    document = json.loads(out)
    assert set(document) == {"checks", "status"}
    assert [check["id"] for check in document["checks"]] == list(doctor.CHECK_IDS)
    for check in document["checks"]:
        assert set(check) == {"id", "status", "summary", "remedy", "details"}
        assert check["status"] == "ok", check
        assert check["remedy"] is None
        assert isinstance(check["details"], dict)
    assert document["status"] == "ok"
    assert code == 0


def test_warnings_only_exit_2_and_the_text_names_the_fix(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.make_store()
    (env.home / ".claude").mkdir()
    code, out = _run_cli(["doctor"], monkeypatch, capsys)
    assert code == 2
    first = out.splitlines()[0]
    assert first.startswith("bettermemory doctor: warn (")
    assert "warn  claude_code" in out
    assert "fix: install the plugin" in out
    for check_id in doctor.CHECK_IDS:
        assert f"  {check_id}" in out


def test_any_failure_exits_1(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = env.make_store()
    with Store.open(store_path) as store:
        store.keyring.current_path().unlink()
    code, out = _run_cli(["doctor", "--json"], monkeypatch, capsys)
    assert code == 1
    assert json.loads(out)["status"] == "fail"


def test_the_subcommand_is_registered_with_its_exit_codes_in_its_help() -> None:
    _, subparsers = _build_parser()
    help_text = " ".join(subparsers["doctor"].format_help().split())
    assert "Exit 0 when every check is ok, 1 on any failure, 2 on warnings only" in (
        help_text
    )
    assert "--json" in help_text


def test_doctor_writes_nothing(
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every kind of file doctor reads is present (no config, though: the
    case where `load_config` would write one), and nothing under the
    test's directories changes: no file appears or disappears, and no
    file's size or modification time moves."""
    config_path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setattr(_config, "default_config_path", lambda: config_path)
    store_path = env.make_store()
    with Store.open(store_path) as store:
        store.write(content="a memory doctor must leave alone", scopes=["global"])
    # A state file whose daemon is alive and silent: doctor then probes
    # the lock file, the one file it opens for anything but reading.
    monkeypatch.setattr(
        _daemon_client, "health", lambda port, timeout=1.0, nonce=None: None
    )
    monkeypatch.setattr(_daemon_client, "pid_alive", lambda pid: True)
    _state(env)
    with _held_lock(env):
        pass
    env.settings(
        {
            "enabledPlugins": {"bettermemory@bettermemory": True},
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": "bettermemory hook stop"}
                        ]
                    }
                ]
            },
        }
    )
    env.write_json(
        env.home / ".claude.json",
        {"mcpServers": {"bettermemory": {"command": str(tmp_path / "gone")}}},
    )
    env.write_json(env.home / ".cursor" / "mcp.json", {"mcpServers": {}})
    keys = Path(os.environ[_config.KEYS_DIR_ENV])
    state_dir = _daemon_client.resolve_state_dir()
    roots = (tmp_path, keys, state_dir, config_path.parent)
    before = _tree(*roots)
    _run_cli(["doctor", "--json"], monkeypatch, capsys)
    _run_cli(["doctor"], monkeypatch, capsys)
    assert _tree(*roots) == before
    assert not config_path.parent.exists()


def test_a_check_that_raises_fails_alone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(ctx: object) -> doctor.Check:
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "check_daemon", broken)
    checks = env.checks()
    assert list(checks) == list(doctor.CHECK_IDS)
    assert checks["daemon"].status == "fail"
    assert "RuntimeError: boom" in checks["daemon"].summary
    assert checks["daemon"].remedy is not None and "bug" in checks["daemon"].remedy
    assert checks["store"].status == "ok"


def test_a_check_the_filesystem_refuses_names_the_path_not_a_bug(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path.exists` re-raises EACCES on 3.11 to 3.13: a store under a
    directory this user cannot search is the machine's answer, reported
    with the path, not as a fault of doctor's."""
    refused = env.tmp / "unsearchable" / "store"

    def denied(ctx: object) -> doctor.Check:
        raise PermissionError(13, "Permission denied", str(refused))

    monkeypatch.setattr(doctor, "check_disk", denied)
    check = env.checks()["disk"]
    assert check.status == "fail"
    assert f"could not read {refused}" in check.summary
    assert "bug" not in (check.remedy or "")
    assert "readable by this user" in (check.remedy or "")

    def unnamed(ctx: object) -> doctor.Check:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(doctor, "check_disk", unnamed)
    check = env.checks()["disk"]
    assert check.status == "fail"
    assert check.summary.startswith("the check could not read what it needs")
    assert "bug" not in (check.remedy or "")


def test_a_check_an_install_cannot_import_for_says_reinstall(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependency the install lost fails the checks that import it with
    the install check's remedy, not as a fault of doctor's."""

    def lost(ctx: object) -> doctor.Check:
        raise ModuleNotFoundError("No module named 'platformdirs'", name="platformdirs")

    monkeypatch.setattr(doctor, "check_config", lost)
    check = env.checks()["config"]
    assert check.status == "fail"
    assert "could not import platformdirs" in check.summary
    assert "bug" not in (check.remedy or "")
    assert (check.remedy or "").startswith("reinstall bettermemory")


def test_the_text_lists_every_raised_finding_of_a_check() -> None:
    check = doctor.Check(
        "clients",
        "fail",
        "first (and 1 more)",
        "fix one",
        {
            "findings": [
                {"status": "fail", "summary": "first", "remedy": "fix one"},
                {"status": "warn", "summary": "second", "remedy": "fix two"},
                {"status": "ok", "summary": "fine", "remedy": None},
            ]
        },
    )
    text = doctor.render_text(doctor.report([check]))
    assert "bettermemory doctor: fail (0 ok, 0 warn, 1 fail)" in text
    assert "fix: fix one" in text
    assert "warn: second" in text
    assert "fix: fix two" in text
    assert "fine" not in text


def test_the_text_lists_a_warning_read_before_the_failure_it_reports() -> None:
    """The check's line carries the first finding of its worst status,
    not the first raised: a warning that came before stays listed, and
    the failure is not printed twice."""
    check = doctor._from_findings(
        "claude_code",
        [
            doctor.Finding("warn", "the earlier warning", "fix the warning"),
            doctor.Finding("fail", "the failure", "fix the failure"),
        ],
        ok_summary="fine",
        details={},
    )
    assert check.summary == "the failure (and 1 more)"
    text = doctor.render_text(doctor.report([check]))
    assert "warn: the earlier warning" in text
    assert "fix: fix the warning" in text
    assert "fail: the failure" not in text
    assert text.count("fix: fix the failure") == 1


def test_exit_codes_by_status() -> None:
    assert doctor.exit_code("ok") == 0
    assert doctor.exit_code("fail") == 1
    assert doctor.exit_code("warn") == 2


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "base", "direct_url", "installed", "expected"),
    [
        ("/x/.local/share/uv/tools/bettermemory", "/usr", None, True, "uv tool"),
        (
            "/x/.cache/uv/archive-v0/abc123",
            "/usr",
            None,
            True,
            "uvx (cached environment)",
        ),
        ("/x/.local/pipx/venvs/bettermemory", "/usr", None, True, "pipx"),
        (
            "/x/proj/.venv",
            "/usr",
            {"dir_info": {"editable": True}},
            True,
            "editable install",
        ),
        ("/x/proj/.venv", "/usr", {"dir_info": {}}, True, "virtual environment"),
        ("/usr", "/usr", None, True, "system or user site-packages"),
        ("/usr", "/usr", None, False, "source tree, not installed"),
    ],
)
def test_install_method_reads_the_interpreter_layout(
    prefix: str,
    base: str,
    direct_url: dict[str, Any] | None,
    installed: bool,
    expected: str,
) -> None:
    assert (
        doctor.install_method(Path(prefix), Path(base), direct_url, installed=installed)
        == expected
    )


def test_install_names_version_python_and_method(env: Env) -> None:
    check = env.checks()["install"]
    assert check.status == "ok"
    assert check.details["version"] == bettermemory.__version__
    assert check.details["python"].startswith(f"{sys.version_info[0]}.")
    assert check.details["method"]
    assert "no `bettermemory` on PATH" in check.summary


def test_install_warns_when_path_runs_another_install(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = env.tmp / "venv" / "bettermemory"
    mine.parent.mkdir()
    mine.write_bytes(b"this install")
    monkeypatch.setattr(doctor, "this_install_binary", lambda: mine)
    env.put_on_path("bettermemory", b"another install")
    check = env.checks()["install"]
    assert check.status == "warn"
    assert "another install" in check.summary


def test_install_is_ok_when_path_runs_a_copy_of_this_install(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = env.tmp / "venv" / "bettermemory"
    mine.parent.mkdir()
    mine.write_bytes(b"the same launcher")
    monkeypatch.setattr(doctor, "this_install_binary", lambda: mine)
    env.put_on_path("bettermemory", b"the same launcher")
    check = env.checks()["install"]
    assert check.status == "ok"
    assert check.details["path_binary"] == str(env.bin_dir / "bettermemory")


def test_install_fails_when_a_runtime_module_is_missing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor, "_RUNTIME_MODULES", ("yaml", "bettermemory_no_such_dep")
    )
    check = env.checks()["install"]
    assert check.status == "fail"
    assert "bettermemory_no_such_dep" in check.summary
    assert "reinstall" in (check.remedy or "")


def test_install_warns_on_an_unknown_version_and_an_untested_python(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert bettermemory.__version__
    monkeypatch.setattr(bettermemory, "__version__", "0+unknown")
    monkeypatch.setattr(doctor, "_NEWEST_TESTED_PYTHON", (3, 10))
    check = env.checks()["install"]
    assert check.status == "warn"
    summaries = [finding["summary"] for finding in check.details["findings"]]
    assert any("version is unknown" in s for s in summaries)
    assert any("newer than the newest tested (3.10)" in s for s in summaries)
    assert "(and 1 more)" in check.summary


def test_this_install_binary_is_the_script_beside_the_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sysconfig

    name = "bettermemory.exe" if sys.platform == "win32" else "bettermemory"
    monkeypatch.setattr(sysconfig, "get_path", lambda kind: str(tmp_path))
    assert doctor.this_install_binary() is None
    (tmp_path / name).write_bytes(b"")
    assert doctor.this_install_binary() == tmp_path / name


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_a_missing_config_reads_the_defaults_and_is_not_created(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "nowhere" / "config.toml"
    monkeypatch.setattr(_config, "default_config_path", lambda: path)
    checks = env.checks()
    assert checks["config"].status == "ok"
    assert "the defaults apply" in checks["config"].summary
    assert checks["config"].details == {"path": str(path), "exists": False}
    assert checks["store_location"].status == "ok"
    assert not path.parent.exists()


def test_the_shipped_default_config_loads_clean(env: Env) -> None:
    check = env.checks()["config"]
    assert check.status == "ok"
    assert check.summary.endswith("loads")
    assert check.details["removed_keys"] == []
    assert check.details["notices"] == []


def test_a_config_that_does_not_parse_fails_naming_the_file(env: Env) -> None:
    path = env.config("[behavior\nconversational = true\n")
    checks = env.checks()
    assert checks["config"].status == "fail"
    assert str(path) in checks["config"].summary
    assert "does not parse" in checks["config"].summary
    # BETTERMEMORY_DIR still decides the store without the config.
    assert checks["store_location"].status == "ok"


def test_a_config_value_that_does_not_load_fails_and_stops_the_store_rule(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BETTERMEMORY_DIR")
    env.config('[behavior]\ndefault_max_results = "many"\n')
    checks = env.checks()
    assert checks["config"].status == "fail"
    assert "default_max_results" in checks["config"].summary
    for check_id in ("store_location", "store", "key", "daemon", "disk"):
        assert checks[check_id].status == "warn", check_id
        assert checks[check_id].summary.startswith("not checked:")


def test_a_config_that_cannot_be_read_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = env.tmp / "cfg-dir" / "config.toml"
    path.mkdir(parents=True)
    monkeypatch.setattr(_config, "default_config_path", lambda: path)
    check = env.checks()["config"]
    assert check.status == "fail"
    assert "cannot be read" in check.summary


def test_removed_keys_and_loader_fallbacks_warn(env: Env) -> None:
    env.config(
        "[behavior]\n"
        'search_mode = "semantic"\n'
        "endorsement_debt_ratio_threshold = 0.1\n"
        "[telemetry]\n"
        "max_bytes = 5\n"
        "[consolidate]\n"
        "enabled = true\n"
    )
    check = env.checks()["config"]
    assert check.status == "warn"
    removed = check.details["removed_keys"]
    assert "[telemetry] max_bytes (removed in 9.0.0)" in removed
    assert "[consolidate] (removed in 9.0.0)" in removed
    assert any("endorsement_debt_ratio_threshold" in key for key in removed)
    assert any("search_mode" in notice for notice in check.details["notices"])
    assert "removed key" in check.summary


def test_removed_keys_reads_the_loaders_registries() -> None:
    assert doctor.removed_keys({}) == []
    assert doctor.removed_keys({"behavior": "not a table"}) == []
    assert doctor.removed_keys({"capture": {"idle_minutes": 5}}) == [
        "[capture] idle_minutes (removed in 9.0.0)"
    ]


# ---------------------------------------------------------------------------
# store_location
# ---------------------------------------------------------------------------


def test_the_environment_variable_rule(env: Env) -> None:
    check = env.checks()["store_location"]
    assert check.status == "ok"
    assert check.details["rule"] == "env"
    assert check.details["directory"] == str(env.store_dir.resolve())
    assert check.summary.startswith("BETTERMEMORY_DIR chose")


def test_the_storage_directory_rule(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BETTERMEMORY_DIR")
    target = env.tmp / "configured"
    env.config(f"[storage]\ndirectory = {json.dumps(str(target))}\n")
    check = env.checks()["store_location"]
    assert check.status == "ok"
    assert check.details["rule"] == "config"
    assert "[storage] directory" in check.summary


def test_a_store_chosen_by_the_working_directory_warns(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BETTERMEMORY_DIR")
    project = (env.tmp / "checkout").resolve()
    (project / ".claude-memory").mkdir(parents=True)
    checks = env.checks(cwd=project)
    check = checks["store_location"]
    assert check.status == "warn"
    assert check.details["rule"] == "cwd"
    assert "a repository that ships this directory" in check.summary
    assert "out of version control" in (check.remedy or "")
    assert checks["store"].details["path"] == str(
        project / ".claude-memory" / STORE_FILENAME
    )


def test_the_home_default_rule(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BETTERMEMORY_DIR")
    check = env.checks(cwd=(env.tmp / "elsewhere"))["store_location"]
    assert check.status == "ok"
    assert check.details["rule"] == "home"
    assert check.details["directory"] == str((env.home / ".claude-memory").resolve())


def test_the_home_store_seen_from_the_home_directory_is_the_home_rule(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run from the home directory, the cwd rule finds the home store
    itself: that is the default install, not a repository's store."""
    monkeypatch.delenv("BETTERMEMORY_DIR")
    (env.home / ".claude-memory").mkdir()
    check = env.checks(cwd=env.home.resolve())["store_location"]
    assert check.status == "ok", check
    assert check.details["rule"] == "home"


def test_a_store_directory_that_is_a_file_fails(env: Env) -> None:
    env.store_dir.write_bytes(b"a file where the store's directory should be")
    checks = env.checks()
    assert checks["store_location"].status == "fail"
    assert "is a file, not a directory" in checks["store_location"].summary
    for check_id in ("store", "key", "daemon", "disk"):
        assert checks[check_id].summary.startswith("not checked:"), check_id


def test_a_directory_no_rule_names_reads_as_other(tmp_path: Path) -> None:
    """A rule the loader gains, or a resolution patched in, is not
    misnamed as one of the four: the rule is read back from the result."""
    rule = doctor._store_rule(
        _config.Config(), tmp_path / "somewhere", tmp_path / "cwd", None
    )
    assert rule == "other"


def test_a_store_under_a_system_directory_warns(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", "/etc/bettermemory-doctor-probe")
    check = env.checks()["store_location"]
    assert check.status == "warn"
    assert "system directory" in check.summary


# ---------------------------------------------------------------------------
# store and key
# ---------------------------------------------------------------------------


def test_no_store_yet_is_the_first_run_state(env: Env) -> None:
    checks = env.checks()
    assert checks["store"].status == "ok"
    assert "no store yet" in checks["store"].summary
    assert checks["key"].status == "ok"
    assert not env.store_dir.exists()


def test_an_unmigrated_v8_directory_fails(env: Env) -> None:
    env.store_dir.mkdir()
    (env.store_dir / ".events.jsonl").write_text("", encoding="utf-8")
    check = env.checks()["store"]
    assert check.status == "fail"
    assert "bettermemory migrate v8" in (check.remedy or "")


def test_a_store_path_that_is_not_a_file_fails(env: Env) -> None:
    (env.store_dir / STORE_FILENAME).mkdir(parents=True)
    checks = env.checks()
    assert checks["store"].status == "fail"
    assert checks["key"].summary.startswith("not checked:")


def test_a_file_that_is_not_a_database_fails(env: Env) -> None:
    env.store_dir.mkdir()
    (env.store_dir / STORE_FILENAME).write_bytes(b"not a database, " * 64)
    checks = env.checks()
    assert checks["store"].status == "fail"
    assert "does not open" in checks["store"].summary
    assert checks["key"].status == "warn"


def test_a_store_from_a_newer_schema_fails_with_upgrade(env: Env) -> None:
    path = env.make_store()
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.close()
    check = env.checks()["store"]
    assert check.status == "fail"
    assert (check.remedy or "").startswith("upgrade bettermemory")


def test_a_store_of_another_schema_warns(env: Env) -> None:
    path = env.make_store()
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.close()
    check = env.checks()["store"]
    assert check.status == "warn"
    assert "is schema 0" in check.summary
    # Older than any release writes: upgrading would not help.
    assert "upgrade" not in (check.remedy or "")
    assert "bettermemory log verify" in (check.remedy or "")


@pytest.mark.parametrize(
    ("error", "restore"),
    [
        (sqlite3.OperationalError("database is locked"), False),
        (PermissionError(13, "Permission denied"), False),
        (sqlite3.DatabaseError("file is not a database"), True),
        (ValueError("x carries no store id or key fingerprint"), True),
    ],
)
def test_the_open_remedy_restores_only_a_damaged_store(
    env: Env, monkeypatch: pytest.MonkeyPatch, error: Exception, restore: bool
) -> None:
    """Restoring a copy over a sound store loses every write since the
    copy: a busy or unreadable store is not told to."""
    env.make_store()

    def refuse(*args: Any, **kwargs: Any) -> Store:
        raise error

    monkeypatch.setattr(Store, "open", refuse)
    check = env.checks()["store"]
    assert check.status == "fail"
    assert ("restore the store from a copy" in (check.remedy or "")) is restore
    assert "doctor leaves the file as it is" in (check.remedy or "")


def test_a_healthy_store_and_key(env: Env) -> None:
    path = env.make_store()
    with Store.open(path) as store:
        store.write(content="one memory to count", scopes=["global"])
    checks = env.checks()
    assert checks["store"].status == "ok"
    assert checks["store"].details["memories"] == 1
    assert checks["store"].details["schema_version"] == 1
    assert checks["key"].status == "ok"
    assert checks["key"].details["head_seq"] is not None


def test_a_store_created_after_an_orphan_warns(env: Env) -> None:
    env.store_dir.mkdir()
    orphan = env.store_dir / f"{STORE_FILENAME}.orphaned-20261011T010000Z.sqlite"
    orphan.write_bytes(b"the lost store")
    env.make_store()
    check = env.checks()["store"]
    assert check.status == "warn"
    assert orphan.name in check.summary
    assert "bettermemory down" in (check.remedy or "")
    assert check.details["orphans"] == [str(orphan)]


def _log_rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM log").fetchone()
    finally:
        conn.close()
    return int(count)


def test_a_missing_key_fails_and_is_not_rekeyed(env: Env) -> None:
    path = env.make_store()
    with Store.open(path) as store:
        key_path = store.keyring.current_path()
    key_path.unlink()
    rows = _log_rows(path)
    check = env.checks()["key"]
    assert check.status == "fail"
    assert "no key for this store" in check.summary
    assert "BETTERMEMORY_KEYS_DIR" in (check.remedy or "")
    assert _log_rows(path) == rows
    assert not key_path.exists()


def test_a_missing_head_checkpoint_warns(env: Env) -> None:
    path = env.make_store()
    with Store.open(path) as store:
        head_path = store.keyring.head_path()
    head_path.unlink()
    check = env.checks()["key"]
    assert check.status == "warn"
    assert "no head checkpoint" in check.summary
    assert not head_path.exists()


@pytest.mark.parametrize(
    ("seq_offset", "mac", "fragment"),
    [
        (5, None, "but the log ends at seq"),
        (0, "0" * 64, "with another MAC than the log's"),
    ],
)
def test_a_head_the_log_does_not_end_at_warns(
    env: Env, seq_offset: int, mac: str | None, fragment: str
) -> None:
    path = env.make_store()
    with Store.open(path) as store:
        head = store.keyring.read_head()
        head_path = store.keyring.head_path()
    assert head is not None
    head_path.write_text(
        json.dumps(
            {"seq": head.seq + seq_offset, "mac": mac or head.mac, "ts": head.ts}
        ),
        encoding="utf-8",
    )
    check = env.checks()["key"]
    assert check.status == "warn"
    assert fragment in check.summary
    assert check.remedy == "run `bettermemory log verify`"


def test_a_head_that_lags_the_log_is_ok(env: Env) -> None:
    """A writer moves the head after its commit, so a crash between the two
    leaves it one row behind: `log verify` reads that as stale, not as a
    problem, and so does doctor."""
    path = env.make_store()
    with Store.open(path) as store:
        stale = store.keyring.read_head()
        store.write(content="a row past the old head", scopes=["global"])
        head_path = store.keyring.head_path()
    assert stale is not None
    head_path.write_text(
        json.dumps({"seq": stale.seq, "mac": stale.mac, "ts": stale.ts}),
        encoding="utf-8",
    )
    assert env.checks()["key"].status == "ok"


def test_head_finding_with_an_empty_log(tmp_path: Path) -> None:
    finding = doctor._head_finding(
        head_seq=3, head_mac="m", tail=None, head_path=tmp_path / "x.head"
    )
    assert finding is not None
    assert "the log has no rows" in finding.summary


def test_a_foreign_key_fails_naming_both_fingerprints(env: Env) -> None:
    path = env.make_store()
    with Store.open(path) as store:
        key_path = store.keyring.current_path()
        expected = store.key_fingerprint
    key_path.write_bytes(b"\x01" * 32)
    check = env.checks()["key"]
    assert check.status == "fail"
    assert "is not this store's" in check.summary
    assert check.details["expected_fingerprint"] == expected
    assert check.details["found_fingerprint"] != expected


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


def test_no_daemon_is_ok(env: Env) -> None:
    check = env.checks()["daemon"]
    assert check.status == "ok"
    assert "not running" in check.summary


def test_an_unreadable_state_file_warns(env: Env) -> None:
    state_file = _daemon_client.state_file_for(
        _daemon_client.resolve_state_dir(), env.store_dir / STORE_FILENAME
    )
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{not json", encoding="utf-8")
    check = env.checks()["daemon"]
    assert check.status == "warn"
    assert "cannot be read" in check.summary
    assert state_file.exists()


def test_a_held_lock_with_no_state_file_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "DAEMON_START_GRACE_SECONDS", 0.0)
    with _held_lock(env):
        check = env.checks()["daemon"]
    assert check.status == "fail"
    assert "no state file names it" in check.summary
    # A long first start holds the lock with no file too: the remedy
    # says to wait for it before stopping anything.
    assert "still starting" in (check.remedy or "")


def test_a_daemon_that_writes_its_state_file_during_the_wait_is_ok(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon takes the lock before it writes its state file: a start
    caught between the two is waited for, not reported as a fault."""
    store_path = env.store_dir / STORE_FILENAME
    monkeypatch.setattr(doctor, "DAEMON_START_GRACE_SECONDS", 30.0)
    monkeypatch.setattr(
        _daemon_client,
        "health",
        lambda port, timeout=1.0, nonce=None: {
            "status": "ok",
            "version": _daemon_client.package_version(),
            "store": str(store_path),
            "pid": 4242,
        },
    )
    started = threading.Timer(0.2, lambda: _state(env, pid=4242))
    with _held_lock(env):
        started.start()
        try:
            check = env.checks()["daemon"]
        finally:
            started.join()
    assert check.status == "ok", check
    assert check.summary.startswith("running: pid 4242")


def test_a_free_lock_file_with_no_state_file_is_ok(env: Env) -> None:
    with _held_lock(env):
        pass
    assert env.checks()["daemon"].status == "ok"


def test_a_stale_state_file_warns_and_is_kept(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _daemon_client, "health", lambda port, timeout=1.0, nonce=None: None
    )
    state_file = _state(env)
    check = env.checks()["daemon"]
    assert check.status == "warn"
    assert f"stale state file: pid {_DEAD_PID}" in check.summary
    assert state_file.exists()


def test_a_daemon_that_holds_the_lock_but_does_not_answer_warns(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _daemon_client, "health", lambda port, timeout=1.0, nonce=None: None
    )
    monkeypatch.setattr(_daemon_client, "pid_alive", lambda pid: pid == 4242)
    _state(env, pid=4242)
    with _held_lock(env):
        check = env.checks()["daemon"]
    assert check.status == "warn"
    assert "daemon not answering (pid 4242 holds the lock)" in check.summary


def test_a_running_daemon_of_this_version_is_ok(env: Env) -> None:
    store_path = env.store_dir / STORE_FILENAME
    body = {
        "status": "ok",
        "version": _daemon_client.package_version(),
        "store": str(store_path),
        "pid": 4242,
    }
    with _health_stub(body) as port:
        _state(env, port=port, pid=4242)
        check = env.checks()["daemon"]
    assert check.status == "ok"
    assert check.summary.startswith("running: pid 4242")


def test_a_listener_serving_another_store_fails(env: Env) -> None:
    body = {
        "status": "ok",
        "version": _daemon_client.package_version(),
        "store": str(env.tmp / "someone-else" / STORE_FILENAME),
        "pid": 4242,
    }
    with _health_stub(body) as port:
        _state(env, port=port)
        check = env.checks()["daemon"]
    assert check.status == "fail"
    assert "not as the daemon of " + str(env.store_dir / STORE_FILENAME) in (
        check.summary
    )


def test_a_listener_that_repeats_the_state_file_without_the_proof_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A state file written by a daemon that offers the proof
    (`health_proof`) is answered by that daemon with the token's MAC over
    the nonce. A listener that repeats the store, the pid and the version
    an earlier, open /health gave it, without the proof, is not the
    daemon: doctor fails it as `status --check` reads it not ours, instead
    of reporting it running. Nothing it sent reaches the terminal raw."""
    store_path = env.store_dir / STORE_FILENAME
    body = {
        "status": "ok",
        "version": _daemon_client.package_version(),
        "store": str(store_path),
        "pid": 4242,
    }
    with _health_stub(body) as port:
        _state(env, port=port, pid=4242, health_proof=1)
        check = env.checks()["daemon"]
    assert check.status == "fail"
    assert "cannot prove it holds this daemon's token" in check.summary
    assert "running" not in check.summary

    body["store"] = "/x\x1b]52;c;cHduZWQ=\x07\x1b[2J"
    with _health_stub(body) as port:
        _state(env, port=port, pid=4242, health_proof=1)
        code, out = _run_cli(["doctor"], monkeypatch, capsys)
    assert code == 1
    assert "\x1b" not in out and "\x07" not in out


def test_a_daemon_that_proves_its_token_is_running(env: Env) -> None:
    """The reply the daemon gives a nonce: the token's MAC over it."""
    store_path = env.store_dir / STORE_FILENAME
    token = "t" * 32

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            query = self.path.partition("?")[2]
            nonce = query.partition("=")[2]
            body = {
                "status": "ok",
                "version": _daemon_client.package_version(),
                "store": str(store_path),
                "pid": 4242,
                "proof": _daemon_client.health_proof(token, nonce),
            }
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _state(env, port=int(server.server_address[1]), pid=4242, health_proof=1)
        check = env.checks()["daemon"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert check.status == "ok", check
    assert check.summary.startswith("running: pid 4242")


@pytest.mark.parametrize(
    ("theirs", "fragment"),
    [
        ("0.0.1", "older than this CLI's"),
        ("999.0.0", "newer than this CLI's"),
        ("not-a-version", "cannot be compared"),
    ],
)
def test_version_skew_warns(
    env: Env, monkeypatch: pytest.MonkeyPatch, theirs: str, fragment: str
) -> None:
    store_path = env.store_dir / STORE_FILENAME
    monkeypatch.setattr(
        _daemon_client,
        "health",
        lambda port, timeout=1.0, nonce=None: {
            "status": "ok",
            "version": theirs,
            "store": str(store_path),
            "pid": _DEAD_PID,
        },
    )
    _state(env)
    check = env.checks()["daemon"]
    assert check.status == "warn"
    assert fragment in check.summary
    assert check.details["daemon_version"] == theirs


# ---------------------------------------------------------------------------
# claude_code
# ---------------------------------------------------------------------------


def _hooks(commands: dict[str, str]) -> dict[str, Any]:
    return {
        event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
        for event, command in commands.items()
    }


_ALL_HOOKS = {
    "SessionStart": "bettermemory hook session-start || true",
    "UserPromptSubmit": "bettermemory hook prompt || true",
    "Stop": "bettermemory hook stop --quiet || true",
}


def test_no_claude_code_is_ok(env: Env) -> None:
    check = env.checks()["claude_code"]
    assert check.status == "ok"
    assert check.details == {"found": False}


def test_the_plugin_enabled_is_ok(env: Env) -> None:
    env.plugin()
    check = env.checks()["claude_code"]
    assert check.status == "ok"
    assert check.details["plugin_enabled"] is True
    assert "plugin is enabled" in check.summary


def test_the_plugin_without_uvx_fails(env: Env) -> None:
    env.plugin(uvx=False)
    check = env.checks()["claude_code"]
    assert check.status == "fail"
    assert "`uvx` is not on PATH" in check.summary


def test_a_project_setting_that_disables_the_plugin_wins(env: Env) -> None:
    env.plugin()
    env.write_json(
        env.tmp / "proj" / ".claude" / "settings.json",
        {"enabledPlugins": {"bettermemory@bettermemory": False}},
    )
    check = env.checks(cwd=env.tmp / "proj")["claude_code"]
    assert check.details["plugin_enabled"] is False
    assert check.status == "warn"
    assert "no bettermemory hooks" in check.summary


def test_settings_hooks_and_a_registration_are_ok(env: Env) -> None:
    env.put_on_path("bettermemory")
    env.settings({"hooks": _hooks(_ALL_HOOKS)})
    env.write_json(
        env.home / ".claude.json",
        {"mcpServers": {"bettermemory": {"command": "bettermemory", "args": []}}},
    )
    check = env.checks()["claude_code"]
    assert check.status == "ok", check
    assert set(check.details["hooks"]) == set(doctor.CLAUDE_HOOK_EVENTS)
    assert check.details["mcp_registrations"] == [str(env.home / ".claude.json")]


def test_a_local_scope_registration_counts(env: Env) -> None:
    env.put_on_path("bettermemory")
    project = env.tmp / "proj"
    project.mkdir()
    env.settings({"hooks": _hooks(_ALL_HOOKS)})
    env.write_json(
        env.home / ".claude.json",
        {
            "projects": {
                str(project): {
                    "mcpServers": {
                        "bettermemory": {"command": "uvx", "args": ["bettermemory"]}
                    }
                }
            }
        },
    )
    check = env.checks(cwd=project)["claude_code"]
    assert check.status == "ok", check
    assert check.details["mcp_registrations"] == [
        f"{env.home / '.claude.json'} (local scope)"
    ]


def test_a_hook_that_runs_a_missing_binary_fails(env: Env) -> None:
    gone = env.tmp / "old-venv" / "bin" / "bettermemory"
    env.settings(
        {"hooks": _hooks({"Stop": f"{_word(gone)} hook stop --quiet || true"})}
    )
    check = env.checks()["claude_code"]
    assert check.status == "fail"
    assert "the Stop hook runs" in check.summary
    assert "does not exist" in check.summary


def test_a_hook_path_the_shell_expands_is_read_expanded(env: Env) -> None:
    """A hook runs through a shell, so `~` and `$HOME` in its program word
    name the binary the shell will find there."""
    binary = env.home / ".local" / "bin" / "bettermemory"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"#!/bin/sh\n")
    env.settings(
        {
            "hooks": _hooks(
                {
                    "SessionStart": "~/.local/bin/bettermemory hook session-start",
                    "UserPromptSubmit": "$HOME/.local/bin/bettermemory hook prompt",
                    "Stop": "~/.local/bin/bettermemory hook stop --quiet || true",
                }
            )
        }
    )
    env.write_json(
        env.home / ".claude.json",
        {"mcpServers": {"bettermemory": {"command": str(binary)}}},
    )
    check = env.checks()["claude_code"]
    assert check.status == "ok", check
    assert set(check.details["hooks"]) == set(doctor.CLAUDE_HOOK_EVENTS)


def test_a_hook_whose_binary_is_not_on_path_fails(env: Env) -> None:
    env.settings({"hooks": _hooks(_ALL_HOOKS)})
    check = env.checks()["claude_code"]
    assert check.status == "fail"
    assert "which is not on PATH" in check.summary


def test_a_hook_of_another_install_warns(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = env.tmp / "venv" / "bettermemory"
    mine.parent.mkdir()
    mine.write_bytes(b"this install")
    monkeypatch.setattr(doctor, "this_install_binary", lambda: mine)
    other = env.tmp / "other" / "bettermemory"
    other.parent.mkdir()
    other.write_bytes(b"that install")
    commands = {event: f"{_word(other)} hook x" for event in doctor.CLAUDE_HOOK_EVENTS}
    env.settings({"hooks": _hooks(commands)})
    env.write_json(
        env.home / ".claude.json",
        {"mcpServers": {"bettermemory": {"command": str(mine)}}},
    )
    check = env.checks()["claude_code"]
    assert check.status == "warn"
    assert "another install than this one" in check.summary


def test_partial_settings_hooks_warn(env: Env) -> None:
    env.put_on_path("bettermemory")
    env.settings({"hooks": _hooks({"Stop": "bettermemory audit-turn --quiet || true"})})
    env.write_json(
        env.tmp / "proj" / ".mcp.json",
        {"mcpServers": {"x": {"command": "bettermemory"}}},
    )
    check = env.checks(cwd=env.tmp / "proj")["claude_code"]
    assert check.status == "warn"
    assert "cover Stop but not SessionStart, UserPromptSubmit" in check.summary


def test_the_plugin_and_settings_hooks_together_warn(env: Env) -> None:
    env.put_on_path("bettermemory")
    env.put_on_path("uvx")
    env.settings(
        {
            "enabledPlugins": {"bettermemory@bettermemory": True},
            "hooks": _hooks(_ALL_HOOKS),
        }
    )
    check = env.checks()["claude_code"]
    assert check.status == "warn"
    assert "each hook event fires twice" in check.summary


def test_hooks_without_a_registration_warn(env: Env) -> None:
    env.put_on_path("bettermemory")
    env.settings({"hooks": _hooks(_ALL_HOOKS)})
    check = env.checks()["claude_code"]
    assert check.status == "warn"
    assert "no bettermemory MCP server registered" in check.summary


def test_other_hooks_are_not_counted(env: Env) -> None:
    env.put_on_path("bettermemory")
    env.settings(
        {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "bettermemory up"}]}
                ],
                "Notification": [
                    {"hooks": [{"type": "command", "command": "say done"}]}
                ],
                "Odd": "not a list",
                "Odder": [
                    {"hooks": "not a list"},
                    "not a group",
                    {"hooks": [{"command": 3}]},
                ],
            }
        }
    )
    check = env.checks()["claude_code"]
    assert check.details["hooks"] == {}
    assert "no bettermemory hooks" in check.summary


def test_an_unreadable_settings_file_warns(env: Env) -> None:
    path = env.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    check = env.checks()["claude_code"]
    assert check.status == "warn"
    summaries = [finding["summary"] for finding in check.details["findings"]]
    assert any(str(path) in s and "cannot be read" in s for s in summaries)


def test_hook_commands_split_at_the_first_operator() -> None:
    words = doctor._command_words("BM_X=1 uvx bettermemory hook stop --quiet || true")
    assert words == ["uvx", "bettermemory", "hook", "stop", "--quiet"]
    assert doctor._command_words("bettermemory 'unbalanced") == [
        "bettermemory",
        "'unbalanced",
    ]


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------


def _client_path(name: str, index: int = 0) -> Path:
    return KNOWN_CLIENTS[name]().paths[index]


def test_no_client_config_is_ok(env: Env) -> None:
    check = env.checks()["clients"]
    assert check.status == "ok"
    assert check.summary == "no MCP client config file found"


def test_a_runnable_entry_is_ok(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = env.tmp / "venv" / "bettermemory"
    mine.parent.mkdir()
    mine.write_bytes(b"this install")
    monkeypatch.setattr(doctor, "this_install_binary", lambda: mine)
    env.write_json(
        _client_path("claude-desktop"),
        {"mcpServers": {"bettermemory": {"command": str(mine), "args": []}}},
    )
    env.write_json(
        _client_path("cursor"),
        {
            "mcpServers": {
                "memory": {"command": "npx", "args": ["@scope/server-memory"]}
            }
        },
    )
    check = env.checks()["clients"]
    assert check.status == "ok", check
    files = {entry["client"]: entry["entries"] for entry in check.details["files"]}
    assert files == {"claude-desktop": ["bettermemory"], "cursor": []}
    assert check.summary.startswith("1 bettermemory entry in 2 client config file(s)")


def test_a_stale_absolute_command_fails_with_the_init_command(env: Env) -> None:
    gone = env.tmp / "old" / "bettermemory"
    env.write_json(
        _client_path("cursor"), {"mcpServers": {"bettermemory": {"command": str(gone)}}}
    )
    check = env.checks()["clients"]
    assert check.status == "fail"
    assert "does not exist" in check.summary
    assert check.remedy == "`bettermemory init --client cursor` rewrites it"


def test_a_project_scope_entry_names_its_config_path(env: Env) -> None:
    mcp_json = _client_path("claude-code", 1)
    env.write_json(
        mcp_json, {"mcpServers": {"bettermemory": {"command": "bettermemory"}}}
    )
    check = env.checks()["clients"]
    assert check.status == "fail"
    assert "not on PATH" in check.summary
    assert f"--config-path {mcp_json}" in (check.remedy or "")


def test_an_entry_of_another_install_warns(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = env.tmp / "venv" / "bettermemory"
    mine.parent.mkdir()
    mine.write_bytes(b"this install")
    monkeypatch.setattr(doctor, "this_install_binary", lambda: mine)
    other = env.tmp / "other" / "bettermemory"
    other.parent.mkdir()
    other.write_bytes(b"that install")
    env.write_json(
        _client_path("cline"), {"mcpServers": {"bettermemory": {"command": str(other)}}}
    )
    check = env.checks()["clients"]
    assert check.status == "warn"
    assert "another install" in check.summary


def test_a_double_registration_warns(env: Env) -> None:
    env.put_on_path("bettermemory")
    env.write_json(
        _client_path("cursor"),
        {
            "mcpServers": {
                "bettermemory": {"command": "bettermemory"},
                "memory": {"command": "bettermemory"},
            }
        },
    )
    check = env.checks()["clients"]
    assert check.status == "warn"
    assert "registers bettermemory twice" in check.summary


def test_the_legacy_continue_shape_warns(env: Env) -> None:
    env.put_on_path("uvx")
    env.write_json(
        _client_path("continue"),
        {"mcpServers": {"bettermemory": {"command": "uvx", "args": ["bettermemory"]}}},
    )
    check = env.checks()["clients"]
    assert check.status == "warn"
    assert "legacy object shape" in check.summary


def test_an_unparseable_client_config_warns(env: Env) -> None:
    path = _client_path("cursor")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    check = env.checks()["clients"]
    assert check.status == "warn"
    assert "cannot be read" in check.summary
    assert check.details["files"] == [
        {"client": "cursor", "path": str(path), "entries": None}
    ]


def test_a_hermes_yaml_entry_is_read(env: Env) -> None:
    env.put_on_path("bettermemory")
    path = _client_path("hermes")
    path.parent.mkdir(parents=True)
    path.write_text(
        "mcp_servers:\n  bettermemory:\n    command: bettermemory\n    args: []\n",
        encoding="utf-8",
    )
    check = env.checks()["clients"]
    assert check.status == "ok"
    assert check.details["files"] == [
        {"client": "hermes", "path": str(path), "entries": ["bettermemory"]}
    ]


def test_python_dash_m_is_recognised_as_bettermemory() -> None:
    assert doctor.launches_bettermemory(
        "/usr/bin/python3", ["-m", "bettermemory"], "/x/bettermemory"
    )
    assert not doctor.launches_bettermemory(None, [], "/x/bettermemory")
    assert not doctor.launches_bettermemory("npx", ["server-memory"], "/x/bettermemory")


# ---------------------------------------------------------------------------
# disk and the posture pointer
# ---------------------------------------------------------------------------


def test_disk_space(env: Env) -> None:
    check = env.checks()["disk"]
    assert check.status == "ok"
    assert check.details["path"] == str(env.tmp)
    env.free = 512 * _MiB
    assert env.checks()["disk"].status == "warn"
    env.free = 10 * _MiB
    check = env.checks()["disk"]
    assert check.status == "fail"
    assert "a write can fail" in check.summary


def test_disk_warns_below_twice_the_store(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = env.make_store()
    monkeypatch.setattr(doctor, "DISK_WARN_BYTES", 0)
    monkeypatch.setattr(doctor, "DISK_FAIL_BYTES", 0)
    env.free = path.stat().st_size + 1
    check = env.checks()["disk"]
    assert check.status == "warn"
    assert check.details["store_bytes"] == path.stat().st_size


def test_the_posture_pointer_names_security_audit(env: Env) -> None:
    check = env.checks()["security"]
    assert check.status == "ok"
    assert "bettermemory security audit" in check.summary


def test_human_sizes() -> None:
    assert doctor._human(512) == "512 B"
    assert doctor._human(3 * _MiB) == "3.0 MiB"
    assert doctor._human(5 * 1024 * _GiB) == "5120.0 GiB"
