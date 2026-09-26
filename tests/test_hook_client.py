"""``bettermemory hook <event>``: a stdlib-only client of the daemon.

The three commands read the hook's stdin JSON, post it to the daemon and
print the reply's ``stdout`` field; they exit 0 whatever happens, and
they import nothing from the SDK, which is what keeps them under the
process-spawn floor a hook pays.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from bettermemory.store import Store

from .test_daemon_lifecycle import _cli, _env, _wait_running


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
