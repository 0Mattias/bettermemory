"""``bettermemory hook <event>``: a stdlib-only client of the daemon.

The three commands read the hook's stdin JSON, post it to the daemon and
print the reply's ``stdout`` field; they exit 0 whatever happens, and
they import nothing from the SDK, which is what keeps them under the
process-spawn floor a hook pays.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _daemon_client, config
from bettermemory._daemon_client import write_state
from bettermemory.store import Store

from .test_daemon_lifecycle import _cli, _env, _wait_running

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


@pytest.fixture
def daemon_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    env = _env(tmp_path)
    yield env
    _cli(["down"], env, timeout=30)


def _hook(
    args: list[str], env: dict[str, str], payload: dict
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "bettermemory", *args],
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_session_start_prints_the_block_through_the_daemon(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    with Store(tmp_path / "store") as store:
        store.write(content="a global preference", scopes=["personal-context"])
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    _wait_running(tmp_path)
    result = _hook(["hook", "session-start"], daemon_env, {"cwd": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(
        "bettermemory: 1 memory is in scope for this repository."
    )
    # The 8.x name is an alias of the same client.
    alias = _hook(["session-start"], daemon_env, {"cwd": str(tmp_path)})
    assert alias.returncode == 0, alias.stderr
    assert alias.stdout == result.stdout


def test_hook_starts_the_daemon_when_none_answers(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    with Store(tmp_path / "store") as store:
        store.write(content="a global preference", scopes=["personal-context"])
    result = _hook(["hook", "session-start"], daemon_env, {"cwd": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("bettermemory: 1 memory is in scope")
    _wait_running(tmp_path)


def test_prompt_and_stop_exit_0_and_print_nothing_without_a_miss(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    _wait_running(tmp_path)
    prompt = _hook(
        ["hook", "prompt"],
        daemon_env,
        {"cwd": str(tmp_path), "session_id": "sess_x", "prompt": "hello"},
    )
    assert prompt.returncode == 0, prompt.stderr
    assert prompt.stdout == ""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}})
        + "\n"
    )
    stop = _hook(
        ["hook", "stop", "--quiet"],
        daemon_env,
        {
            "cwd": str(tmp_path),
            "session_id": "sess_x",
            "transcript_path": str(transcript),
        },
    )
    assert stop.returncode == 0, stop.stderr
    assert stop.stdout == ""


def test_hook_exits_0_when_no_daemon_can_be_reached_or_started(tmp_path: Path) -> None:
    env = _env(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    env["BETTERMEMORY_STATE_DIR"] = str(blocker / "state")
    result = _hook(["hook", "session-start"], env, {"cwd": str(tmp_path)})
    assert result.returncode == 0
    assert result.stdout == ""


def test_the_hook_path_imports_neither_the_sdk_nor_pydantic(tmp_path: Path) -> None:
    env = _env(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    env["BETTERMEMORY_STATE_DIR"] = str(blocker / "state")
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "importtime",
            "-m",
            "bettermemory",
            "hook",
            "session-start",
        ],
        env=env,
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    imported = {
        line.rsplit("|", 1)[-1].strip()
        for line in result.stderr.splitlines()
        if line.startswith("import time:")
    }
    for heavy in (
        "mcp",
        "pydantic",
        "starlette",
        "httpx2",
        "bettermemory.builder",
        "bettermemory.store",
    ):
        assert heavy not in imported, f"{heavy} reached the hook path"


def _imported_by_a_hook(env: dict[str, str]) -> set[str]:
    """Every module a `bettermemory hook session-start` process imports,
    by `-X importtime`."""
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "importtime",
            "-m",
            "bettermemory",
            "hook",
            "session-start",
        ],
        env=env,
        input="{}",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    return {
        line.rsplit("|", 1)[-1].strip()
        for line in result.stderr.splitlines()
        if line.startswith("import time:")
    }


def test_the_hook_path_imports_neither_importlib_metadata_nor_the_config(
    tmp_path: Path,
) -> None:
    """With BETTERMEMORY_DIR set, the hook reads its version from the file
    the build wrote and its store from the variable: `importlib.metadata`
    and the config module stay out of the process. The state directory is
    named here too, so platformdirs, which `resolve_state_dir` imports only
    when it is not, stays out as well."""
    env = _env(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    env["BETTERMEMORY_STATE_DIR"] = str(blocker / "state")
    imported = _imported_by_a_hook(env)
    assert "bettermemory._daemon_client" in imported, "premise: the hook ran"
    assert "bettermemory._version" in imported
    for heavy in ("importlib.metadata", "bettermemory.config", "platformdirs"):
        assert heavy not in imported, f"{heavy} reached the hook path"


def test_the_store_path_comes_from_bettermemory_dir_without_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BETTERMEMORY_DIR names the store the same way `Config.resolved_
    directory` reads it (expanded and resolved), and no config is loaded:
    a config that would fail to load does not stop the hook."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "named" / ".." / "store"))

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("a malformed config")

    monkeypatch.setattr(config, "load_config", refuse)
    assert _daemon_client.resolved_store_path() == (
        (tmp_path / "store").resolve() / config.STORE_FILENAME
    )


def test_the_store_path_loads_the_config_where_bettermemory_dir_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the variable the config's own rule decides, as before:
    `load_config().resolved_directory()`, and a config that fails to load
    still stops the caller."""
    monkeypatch.delenv("BETTERMEMORY_DIR", raising=False)
    calls: list[int] = []
    real = config.load_config

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(config, "load_config", counted)
    expected = real().resolved_directory() / config.STORE_FILENAME
    assert _daemon_client.resolved_store_path() == expected
    assert calls == [1]

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("a malformed config")

    monkeypatch.setattr(config, "load_config", refuse)
    with pytest.raises(ValueError, match="malformed"):
        _daemon_client.resolved_store_path()


def test_the_client_names_the_store_file_as_the_config_does() -> None:
    """The client names the store file itself so the hook need not import
    the config module for it; the two names are one."""
    assert _daemon_client.STORE_FILENAME == config.STORE_FILENAME


@pytest.mark.skipif(
    os.name != "posix", reason="the child's config directory follows HOME on POSIX"
)
def test_a_malformed_config_does_not_stop_a_hook_that_names_its_store(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """A daemon serves the store; the config file then stops parsing. A
    hook with BETTERMEMORY_DIR set reaches the daemon and prints its block,
    where before it stopped at the config with one stderr line."""
    home = tmp_path / "home"
    home.mkdir()
    env = dict(daemon_env)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    with Store(tmp_path / "store") as store:
        store.write(content="a global preference", scopes=["personal-context"])
    assert _cli(["up", "--port", "0"], env).returncode == 0
    _wait_running(tmp_path)
    where = subprocess.run(
        [
            sys.executable,
            "-c",
            "from bettermemory.config import default_config_path; "
            "print(default_config_path())",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    config_path = Path(where.stdout.strip())
    assert config_path.is_relative_to(home) and config_path.is_file()
    good = config_path.read_text(encoding="utf-8")
    config_path.write_text("[behavior\n", encoding="utf-8")
    try:
        result = _hook(["hook", "session-start"], env, {"cwd": str(tmp_path)})
        assert result.returncode == 0
        assert result.stdout.startswith("bettermemory: 1 memory is in scope"), (
            result.stderr
        )
    finally:
        config_path.write_text(good, encoding="utf-8")
        _cli(["down"], env, timeout=30)


class _StaleDaemon(http.server.ThreadingHTTPServer):
    shutdown_calls = 0


class _StaleHandler(http.server.BaseHTTPRequestHandler):
    """A daemon of another version: /health answers 0.0.1, and a shutdown
    request is counted."""

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {"status": "ok", "version": "0.0.1", "store": "x", "pid": 1}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == _daemon_client.SHUTDOWN_PATH:
            self.server.shutdown_calls += 1  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def test_the_hook_replaces_a_daemon_of_another_version_and_keeps_its_own(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """After an upgrade the state file names a daemon of the old version:
    the hook asks it to stop and starts one of its own. The daemon it
    starts reports the version the hook compares against (the version
    file's, which is pyproject's), so the next hook keeps it rather than
    replacing it again."""
    with Store(tmp_path / "store") as store:
        store.write(content="a global preference", scopes=["personal-context"])
    stale = _StaleDaemon(("127.0.0.1", 0), _StaleHandler)
    thread = threading.Thread(target=stale.serve_forever, daemon=True)
    thread.start()
    try:
        write_state(
            tmp_path / "state",
            {
                "pid": 2**22 + 4321,
                "port": stale.server_address[1],
                "token": "t" * 64,
                "version": "0.0.1",
                "store": str(tmp_path / "store" / "memory.sqlite"),
                "started": "2026-01-01T00:00:00Z",
            },
        )
        first = _hook(["hook", "session-start"], daemon_env, {"cwd": str(tmp_path)})
        assert first.returncode == 0, first.stderr
        assert first.stdout.startswith("bettermemory: 1 memory is in scope")
        assert stale.shutdown_calls == 1
        state = _wait_running(tmp_path)
        assert state["version"] == _pyproject_version()
        assert state["version"] == _daemon_client.package_version()
        answer = _daemon_client.health(state["port"])
        assert answer is not None and answer["version"] == state["version"]

        second = _hook(["hook", "session-start"], daemon_env, {"cwd": str(tmp_path)})
        assert second.stdout == first.stdout
        assert _wait_running(tmp_path)["pid"] == state["pid"], "kept, not replaced"
    finally:
        stale.shutdown()
        stale.server_close()
