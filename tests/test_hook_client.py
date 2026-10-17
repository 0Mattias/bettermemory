"""``bettermemory hook <event>``: a stdlib-only client of the daemon.

The three commands read the hook's stdin JSON, post it to the daemon and
print the reply's ``stdout`` field; they exit 0 whatever happens, and
they import nothing from the SDK, which is what keeps them under the
process-spawn floor a hook pays.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from bettermemory import _daemon_client, config
from bettermemory._daemon_client import write_state
from bettermemory.store import Store

from .conftest import shielded_child_env
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


def _briefable_repo(tmp_path: Path) -> Path:
    """A git repository holding one memory the session brief shows:
    written there and verified, its body citing no path."""
    from bettermemory.origin import capture

    from .test_verdict import _repo

    repo = _repo(tmp_path)
    with Store(tmp_path / "store") as store:
        written = store.write(
            content="prefer short release notes in this project",
            scopes=["projects:foo"],
            origin=capture(repo),
        )
        store.mark_verified(written.id)
    return repo


_BRIEF_HEAD = (
    "bettermemory brief: the 1 newest checked memory written in this repository."
)


def test_session_start_prints_the_block_through_the_daemon(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    repo = _briefable_repo(tmp_path)
    assert _cli(["up", "--port", "0"], daemon_env).returncode == 0
    _wait_running(tmp_path)
    payload = {"cwd": str(repo), "session_id": "sess_brief", "source": "startup"}
    result = _hook(["hook", "session-start"], daemon_env, payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(_BRIEF_HEAD)
    # The 8.x name is an alias of the same client; from another session,
    # since a session's segment shows no id twice.
    alias = _hook(["session-start"], daemon_env, {**payload, "session_id": "sess_2"})
    assert alias.returncode == 0, alias.stderr
    assert alias.stdout == result.stdout


def test_hook_starts_the_daemon_when_none_answers(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    repo = _briefable_repo(tmp_path)
    result = _hook(
        ["hook", "session-start"],
        daemon_env,
        {"cwd": str(repo), "session_id": "sess_brief"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(_BRIEF_HEAD)
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
    repo = _briefable_repo(tmp_path)
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
        result = _hook(
            ["hook", "session-start"],
            env,
            {"cwd": str(repo), "session_id": "sess_brief"},
        )
        assert result.returncode == 0
        assert result.stdout.startswith(_BRIEF_HEAD), result.stderr
    finally:
        config_path.write_text(good, encoding="utf-8")
        _cli(["down"], env, timeout=30)


class _StaleDaemon(http.server.ThreadingHTTPServer):
    """A daemon's HTTP surface, enough for a hook: /health answers
    `version`, a shutdown request is counted, and a hook post is counted
    and answered with nothing to print."""

    def __init__(self, version: str) -> None:
        super().__init__(("127.0.0.1", 0), _StaleHandler)
        self.version = version
        self.shutdown_calls = 0
        self.hook_calls = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def state(self, store: Path) -> dict[str, Any]:
        return {
            "pid": 2**22 + 4321,
            "port": self.port,
            "token": "t" * 64,
            "version": self.version,
            "store": str(store),
            "started": "2026-01-01T00:00:00Z",
        }

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _StaleHandler(http.server.BaseHTTPRequestHandler):
    @property
    def fake(self) -> _StaleDaemon:
        return cast(_StaleDaemon, self.server)

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {"status": "ok", "version": self.fake.version, "store": "x", "pid": 1}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == _daemon_client.SHUTDOWN_PATH:
            self.fake.shutdown_calls += 1
        elif self.path.startswith(_daemon_client.HOOK_PATH):
            self.fake.hook_calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def _dev_build_of_this_release() -> str:
    """`X.Y.Z.dev0` for pyproject's `X.Y.Z[rcN]`: the version a checkout
    installed before the candidate reports, older than the candidate."""
    match = re.match(r"\d+\.\d+\.\d+", _pyproject_version())
    assert match is not None
    return match.group(0) + ".dev0"


_ASCENDING = [
    "0.0.1",
    "9.0.0.dev0",
    "9.0.0.dev1",
    "9.0.0a1",
    "9.0.0b2",
    "9.0.0rc1.dev1",
    "9.0.0rc1",
    "9.0.0rc2",
    "9.0.0",
    "9.0.1",
    "9.1.0",
    "10.0.0",
]


def test_version_keys_sort_in_pep_440_order() -> None:
    keys = [_daemon_client.version_key(text) for text in _ASCENDING]
    assert all(key is not None for key in keys)
    assert keys == sorted(keys, key=lambda key: key or ())
    assert len(set(keys)) == len(keys)


@pytest.mark.parametrize(
    "text",
    ["", "9", "9.0", "v9.0.0", "9.0.0.post1", "9.0.0+local", "9.0.0rc", None, 9],
)
def test_a_version_of_another_shape_has_no_key(text: object) -> None:
    assert _daemon_client.version_key(text) is None


def test_a_client_keeps_the_same_or_a_newer_daemon_and_replaces_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The rule behind `ensure_daemon`: a daemon of the client's version
    or a newer one is kept, with one log line for the newer; an older
    one, and one whose version cannot be read, is replaced. A client
    whose own version cannot be read keeps only the same string."""
    state = {"port": 1, "pid": 1}

    def keeps(theirs: object, mine: str) -> bool:
        return _daemon_client._keeps(state, theirs, mine)

    caplog.set_level(logging.INFO, logger="bettermemory._daemon_client")
    assert keeps("9.0.0rc1", "9.0.0rc1")
    assert keeps("9.0.1", "9.0.0")
    assert keeps("9.0.0", "9.0.0rc1")
    assert not keeps("9.0.0rc1", "9.0.0")
    assert not keeps("9.0.0.dev0", "9.0.0rc1")
    assert not keeps("0.0.1", "9.0.0rc1")
    assert not keeps("not-a-version", "9.0.0rc1")
    assert not keeps(None, "9.0.0rc1")
    assert keeps("9.0.0+local", "9.0.0+local")
    assert not keeps("9.0.1", "9.0.0+local")
    assert [record.getMessage() for record in caplog.records] == [
        "keeping the daemon of version 9.0.1 at 127.0.0.1:1 (pid 1), newer than "
        "this client's 9.0.0",
        "keeping the daemon of version 9.0.0 at 127.0.0.1:1 (pid 1), newer than "
        "this client's 9.0.0rc1",
    ]


@pytest.mark.parametrize(
    "stale_version",
    ["0.0.1", _dev_build_of_this_release(), "not-a-version"],
    ids=["older", "dev-build-of-this-release", "unreadable"],
)
def test_the_hook_replaces_a_daemon_of_an_older_version_and_keeps_its_own(
    tmp_path: Path, daemon_env: dict[str, str], stale_version: str
) -> None:
    """After an upgrade the state file names a daemon of the old version:
    the hook asks it to stop and starts one of its own. The daemon it
    starts reports the version the hook compares against (the version
    file's, which is pyproject's), so the next hook keeps it rather than
    replacing it again. A dev build of this release is older than its
    candidate, and a version the hook cannot read is replaced too."""
    repo = _briefable_repo(tmp_path)
    payload = {"cwd": str(repo), "session_id": "sess_brief"}
    stale = _StaleDaemon(stale_version)
    try:
        write_state(
            tmp_path / "state", stale.state(tmp_path / "store" / "memory.sqlite")
        )
        first = _hook(["hook", "session-start"], daemon_env, payload)
        assert first.returncode == 0, first.stderr
        assert first.stdout.startswith(_BRIEF_HEAD)
        assert stale.shutdown_calls == 1
        assert stale.hook_calls == 0
        state = _wait_running(tmp_path)
        assert state["version"] == _pyproject_version()
        assert state["version"] == _daemon_client.package_version()
        answer = _daemon_client.health(state["port"])
        assert answer is not None and answer["version"] == state["version"]

        # The next hook, from another session (a session's segment shows
        # no id twice), is answered by the daemon the first one started.
        second = _hook(
            ["hook", "session-start"], daemon_env, {**payload, "session_id": "sess_2"}
        )
        assert second.stdout == first.stdout
        assert _wait_running(tmp_path)["pid"] == state["pid"], "kept, not replaced"
    finally:
        stale.stop()


@pytest.mark.parametrize(
    "running_version", ["999.0.0", _pyproject_version()], ids=["newer", "equal"]
)
def test_the_hook_keeps_a_daemon_of_the_same_or_a_newer_version(
    tmp_path: Path, running_version: str
) -> None:
    """A daemon that answers /health with the hook's version or a newer
    one is used as it is: no shutdown, no daemon started, nothing on
    stderr (a kept daemon is not a failure), and the hook's post goes to
    it. So a hook from an older install beside a newer one does not
    restart the daemon under the newer install's client."""
    env = _env(tmp_path)
    store = tmp_path / "store" / "memory.sqlite"
    running = _StaleDaemon(running_version)
    try:
        write_state(tmp_path / "state", running.state(store))
        result = _hook(
            ["hook", "session-start"],
            env,
            {"cwd": str(tmp_path), "session_id": "sess_keep"},
        )
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
        assert running.shutdown_calls == 0
        assert running.hook_calls == 1
        state = _daemon_client.read_state(tmp_path / "state", store)
        assert state is not None and state["port"] == running.port
    finally:
        running.stop()
        after = _daemon_client.read_state(tmp_path / "state", store)
        if after is not None and after["port"] != running.port:
            _cli(["down"], env, timeout=30)


def _malformed_config(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    """`env` with HOME and XDG_CONFIG_HOME under `tmp_path` and the config
    file there written so that it does not parse."""
    home = tmp_path / "home"
    home.mkdir()
    env = dict(env)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
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
    assert config_path.is_relative_to(home)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("[behavior\n", encoding="utf-8")
    return env


@pytest.mark.skipif(
    os.name != "posix", reason="the child's config directory follows HOME on POSIX"
)
def test_a_daemon_that_exits_at_start_fails_the_hook_at_once_with_its_error(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """No daemon runs, so the hook starts one, and the malformed config
    stops that daemon at start. The hook sees its child exit and reports
    the error the daemon logged, at once, where it used to wait out
    START_TIMEOUT_SECONDS and name no cause."""
    env = _malformed_config(tmp_path, daemon_env)
    started = time.monotonic()
    result = _hook(["hook", "session-start"], env, {"cwd": str(tmp_path)})
    elapsed = time.monotonic() - started
    assert result.returncode == 0
    assert result.stdout == ""
    assert "Expected ']' at the end of a table declaration" in result.stderr, (
        result.stderr
    )
    assert elapsed < _daemon_client.START_TIMEOUT_SECONDS / 2, elapsed


@pytest.mark.skipif(
    os.name != "posix", reason="the child's config directory follows HOME on POSIX"
)
def test_up_reports_the_error_of_a_daemon_that_exits_at_start(
    tmp_path: Path, daemon_env: dict[str, str]
) -> None:
    """`bettermemory up` fails with exit 1 as soon as the daemon it started
    exits, and names the daemon's error and its log file."""
    env = _malformed_config(tmp_path, daemon_env)
    started = time.monotonic()
    result = _cli(["up", "--port", "0"], env)
    elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert "Expected ']' at the end of a table declaration" in result.stderr, (
        result.stderr
    )
    assert "daemon-" in result.stderr and ".log" in result.stderr, result.stderr
    assert elapsed < _daemon_client.START_TIMEOUT_SECONDS / 2, elapsed


def test_the_wait_ends_when_the_started_child_exits(tmp_path: Path) -> None:
    """`wait_for_daemon` given the child it started stops polling when the
    child exits with no daemon answering, and raises `DaemonExited` with the
    child's exit status and the last line the child wrote to its log since
    it was started (earlier runs' lines are not its)."""
    from bettermemory._daemon_client import DaemonExited, Started

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store_path = tmp_path / "store" / "memory.sqlite"
    log_path = _daemon_client.log_file_for(state_dir, store_path)
    log_path.write_text("an earlier run's last line\n", encoding="utf-8")
    offset = log_path.stat().st_size
    with log_path.open("ab") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; print('starting', flush=True); "
                "sys.stderr.write('ValueError: the reason it stopped\\n'); sys.exit(3)",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    started = time.monotonic()
    with pytest.raises(DaemonExited) as raised:
        _daemon_client.wait_for_daemon(
            state_dir,
            store_path,
            timeout=_daemon_client.START_TIMEOUT_SECONDS,
            started=Started(child=child, log=log_path, log_offset=offset),
        )
    assert time.monotonic() - started < _daemon_client.START_TIMEOUT_SECONDS / 2
    assert raised.value.status == 3
    assert raised.value.error == "ValueError: the reason it stopped"
    assert str(log_path) in str(raised.value)


def test_the_wait_returns_a_daemon_that_answers_though_the_child_exited(
    tmp_path: Path,
) -> None:
    """A daemon that answers is the answer, whatever became of the child:
    here the child writes the state file of a server that answers /health
    and exits, and the wait returns that state rather than raising."""
    from bettermemory._daemon_client import Started

    state_dir = tmp_path / "state"
    store_path = tmp_path / "store" / "memory.sqlite"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AnsweringHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = {
        "pid": os.getpid(),
        "port": server.server_address[1],
        "token": "t" * 64,
        "version": _daemon_client.package_version(),
        "store": str(store_path),
        "started": "2026-01-01T00:00:00Z",
    }
    code = (
        "import json, sys; from pathlib import Path; "
        "from bettermemory._daemon_client import write_state; "
        f"write_state(Path({str(state_dir)!r}), json.loads({json.dumps(state)!r}))"
    )
    try:
        state_dir.mkdir()
        log_path = _daemon_client.log_file_for(state_dir, store_path)
        with log_path.open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=shielded_child_env(),
            )
        child.wait(timeout=30)
        assert child.returncode == 0
        answered = _daemon_client.wait_for_daemon(
            state_dir,
            store_path,
            timeout=_daemon_client.START_TIMEOUT_SECONDS,
            started=Started(child=child, log=log_path, log_offset=0),
        )
        assert answered is not None and answered["port"] == state["port"]
    finally:
        server.shutdown()
        server.server_close()


class _AnsweringHandler(http.server.BaseHTTPRequestHandler):
    """A daemon of this package's version that answers /health."""

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "status": "ok",
                "version": _daemon_client.package_version(),
                "store": "x",
                "pid": 1,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Capture at Stop: the attended mark and the hook's own JSON
# ---------------------------------------------------------------------------


def _run_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    *,
    attended: str | None,
    reply: dict[str, Any],
    payload: dict[str, Any] | None = None,
    argv: tuple[str, ...] = ("hook", "stop", "--quiet"),
) -> tuple[dict[str, Any], str]:
    """`hook_main` in process against a stand-in daemon: what it posted, and
    what it printed."""
    import io

    sent: dict[str, Any] = {}
    if attended is None:
        monkeypatch.delenv(_daemon_client.ATTENDED_ENV, raising=False)
    else:
        monkeypatch.setenv(_daemon_client.ATTENDED_ENV, attended)
    monkeypatch.setattr(
        _daemon_client, "resolved_store_path", lambda: tmp_path / "store.db"
    )
    monkeypatch.setattr(
        _daemon_client, "ensure_daemon", lambda *a, **k: {"port": 1, "token": "t"}
    )

    def fake_post(port: int, token: str, path: str, body: dict, **kw: Any) -> dict:
        sent.update(body)
        return reply

    monkeypatch.setattr(_daemon_client, "post", fake_post)
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload or {}).encode()))
    monkeypatch.setattr(sys, "stdin", stdin)
    assert _daemon_client.hook_main(list(argv)) == 0
    return sent, capsys.readouterr().out


@pytest.mark.parametrize(
    ("value", "forwarded"),
    [("1", True), ("0", False), ("", False), ("true", False), (None, False)],
)
def test_the_hook_forwards_attended_only_for_exactly_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    value: str | None,
    forwarded: bool,
) -> None:
    # A payload that claims attended itself is never believed.
    sent, _ = _run_client(
        monkeypatch,
        capsys,
        tmp_path,
        attended=value,
        reply={"stdout": ""},
        payload={"session_id": "s", "attended": "1"},
    )
    assert ("attended" in sent) is forwarded
    if forwarded:
        assert sent["attended"] == "1"


def test_the_hook_prints_hook_output_alone_even_under_quiet(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output = json.dumps({"hookSpecificOutput": {"hookEventName": "Stop"}})
    reply = {"stdout": '{"verdict": "ok"}', "hook_output": output}
    for argv in (("hook", "stop", "--quiet"), ("hook", "stop")):
        _, printed = _run_client(
            monkeypatch, capsys, tmp_path, attended="1", reply=reply, argv=argv
        )
        assert printed == output + "\n"
    # Without it, --quiet still prints nothing.
    _, printed = _run_client(
        monkeypatch, capsys, tmp_path, attended="1", reply={"stdout": "x"}
    )
    assert printed == ""


def test_a_daemon_started_from_an_attended_shell_never_inherits_the_mark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_daemon_client.ATTENDED_ENV, "1")
    env = _daemon_client.daemon_env_for(tmp_path / "store.db")
    assert _daemon_client.ATTENDED_ENV not in env
