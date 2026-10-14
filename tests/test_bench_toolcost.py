"""Tests for the tool-schema cost probe.

The published number is a comparison against another project, so the
measurement has to be defensible on method as well as arithmetic. Two
things are pinned: that the byte accounting is what it claims to be, and
that the probe cannot silently measure the operator's configuration
instead of the shipped default — which is exactly what it did on its
first run, reporting 27 tools instead of 18.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from bettermemory._daemon_client import pid_alive

from .conftest import shielded_child_env

_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / "bench" / "toolcost" / "run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_toolcost_run", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_toolcost_run"] = module
    spec.loader.exec_module(module)
    return module


toolcost = _load()

_TOOLS = [
    {
        "name": "alpha",
        "description": "does alpha things",
        "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
    },
    {
        "name": "beta",
        "description": "does beta things",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def test_full_bytes_counts_schemas_not_just_descriptions() -> None:
    """The headline unit is the FULL serialized tools array. The project
    previously quoted the name+description subset, understating its own
    per-turn cost by roughly a quarter — so the distinction is pinned."""
    m = toolcost.measure(_TOOLS)
    assert m["full_bytes"] > m["name_description_bytes"]
    assert m["full_bytes"] > m["input_schema_bytes"]
    assert m["tool_count"] == 2


def test_measurement_is_serialization_stable() -> None:
    """Sorted keys and no whitespace, so a server that pretty-prints its
    JSON cannot score differently from one that does not."""
    reordered = [
        {
            "inputSchema": t["inputSchema"],
            "description": t["description"],
            "name": t["name"],
        }
        for t in _TOOLS
    ]
    assert (
        toolcost.measure(reordered)["full_bytes"]
        == toolcost.measure(_TOOLS)["full_bytes"]
    )


def test_empty_tool_list_does_not_divide_by_zero() -> None:
    m = toolcost.measure([])
    assert m["tool_count"] == 0
    assert m["bytes_per_tool"] is None


def test_probe_isolates_home_so_it_cannot_read_the_operators_config() -> None:
    """The bug this harness shipped with, pinned.

    The first run reported 27 tools for bettermemory instead of the
    shipped 18, because the child process inherited HOME and read the
    author's own `full_tool_surface = true`. A benchmark that reads the
    operator's config is measuring the operator, and the number would have
    been wrong in the direction that made bettermemory look worse against
    a competitor — which is not a safe direction either.
    """
    source = _RUNNER.read_text(encoding="utf-8")
    assert '"HOME": sandbox' in source, "probe no longer redirects HOME"
    assert "XDG_CONFIG_HOME" in source


def test_probe_reports_failures_instead_of_fabricating_numbers() -> None:
    """A server that cannot be spawned must produce an error row, never a
    plausible-looking byte count."""
    spec = [{"label": "nonexistent", "command": ["/nonexistent/binary"], "env": {}}]
    spec_file = Path(_ROOT / "bench" / "toolcost" / ".tmp-spec.json")
    try:
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        try:
            toolcost.probe_tools(["/nonexistent/binary"], timeout=5)
        except Exception as exc:
            assert isinstance(exc, (FileNotFoundError, OSError, RuntimeError))
        else:  # pragma: no cover - would mean a missing binary silently worked
            raise AssertionError("probing a missing binary must raise")
    finally:
        spec_file.unlink(missing_ok=True)


def test_published_result_is_internally_consistent() -> None:
    """The committed artifact must agree with its own arithmetic — a
    ratio that drifted from its operands would be the exact failure this
    directory exists to prevent."""
    results_dir = _ROOT / "bench" / "toolcost" / "results"
    files = sorted(results_dir.glob("*.json"))
    assert files, "no committed toolcost result"
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = {r["label"]: r for r in data["results"] if "error" not in r}
        assert rows, f"{path.name} records no successful probe"
        for row in rows.values():
            assert row["full_bytes"] >= row["name_description_bytes"]
            assert row["bytes_per_tool"] == round(row["full_bytes"] / row["tool_count"])
        if "ratio_full_bytes" in data:
            bm = next(v for k, v in rows.items() if "bettermemory" in k)
            other = next(v for k, v in rows.items() if "bettermemory" not in k)
            expected = round(bm["full_bytes"] / other["full_bytes"], 3)
            assert abs(data["ratio_full_bytes"] - expected) < 0.002


def test_a_failed_spawn_leaves_no_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox is made before the spawn, so a spawn that fails must
    still remove it: the missing-binary probe above used to leave one
    empty `toolcost-home-*` directory in $TMPDIR on every run."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(OSError):
        toolcost.probe_tools(["/nonexistent/binary"], timeout=5)
    assert list(tmp_path.glob("toolcost-home-*")) == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the probe reads the server through select, sockets only on Windows",
)
def test_probing_the_shim_leaves_no_daemon_and_no_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bettermemory's shim starts a detached daemon for the sandbox store.
    The probe must stop it before it removes the sandbox: removing the
    state file first left a daemon `bettermemory down` could not reach,
    serving a deleted store, one per probe."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen: list[dict[str, Any]] = []
    original = toolcost.sandbox_daemons

    def recording(sandbox: str, env: dict[str, str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = original(sandbox, env)
        seen.extend(records)
        return records

    monkeypatch.setattr(toolcost, "sandbox_daemons", recording)
    # Overrides only: `env` is laid over the sandbox's HOME, so a whole
    # environment here would hand the child the real HOME back. The
    # sandbox decides the store, the state directory and the keys,
    # whatever the environment running the suite names (empty is unset),
    # and the working directory has no project store.
    env = {
        "PYTHONPATH": shielded_child_env()["PYTHONPATH"],
        "BETTERMEMORY_DIR": "",
        "BETTERMEMORY_STATE_DIR": "",
        "BETTERMEMORY_KEYS_DIR": "",
    }
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    probe = toolcost.probe_tools(
        [sys.executable, "-m", "bettermemory"],
        env=env,
        cwd=str(workdir),
        timeout=60,
    )
    assert probe.tools, "the shim served no tools"
    pids = {record["pid"] for record in seen}
    assert pids, "no daemon state was found in the sandbox"
    for record in seen:
        assert Path(record["store"]).resolve().is_relative_to(tmp_path.resolve())
    for pid in pids:
        assert not pid_alive(pid), f"daemon pid {pid} outlived the probe"
    assert list(tmp_path.glob("toolcost-home-*")) == []


# ---------------------------------------------------------------------------
# The probe never reaches a daemon outside its sandbox
# ---------------------------------------------------------------------------


def _capture_child_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    def fake_probe(
        command: list[str],
        *,
        child_env: dict[str, str],
        cwd: str | None,
        timeout: float,
    ) -> Any:
        seen.append(dict(child_env))
        return toolcost.ProbeResult(tools=[], instructions="")

    monkeypatch.setattr(toolcost, "_probe", fake_probe)
    return seen


def test_the_child_env_carries_no_runner_bettermemory_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner with another daemon's home and port exported: the probe's
    child sees none of them, so the sandbox shim cannot reuse that daemon,
    nor the clean-up stop it."""
    other = tmp_path / "other-daemon"
    monkeypatch.setenv("BETTERMEMORY_HOME", str(other))
    monkeypatch.setenv("BETTERMEMORY_PORT", "48211")
    monkeypatch.setenv("BETTERMEMORY_DIR", str(other / "store"))
    monkeypatch.setenv("BETTERMEMORY_STATE_DIR", str(other / "state"))
    monkeypatch.setenv("BETTERMEMORY_KEYS_DIR", str(other / "keys"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen = _capture_child_env(monkeypatch)
    toolcost.probe_tools([sys.executable, "-m", "bettermemory"], timeout=5)
    (child_env,) = seen
    assert [key for key in child_env if key.startswith("BETTERMEMORY_")] == []
    assert not any(str(other) in value for value in child_env.values())
    assert "48211" not in child_env.values()
    assert Path(child_env["HOME"]).name.startswith("toolcost-home-")


def test_the_specs_own_bettermemory_variables_still_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(tmp_path / "runner"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen = _capture_child_env(monkeypatch)
    toolcost.probe_tools(["/bin/true"], env={"BETTERMEMORY_DIR": ""}, timeout=5)
    (child_env,) = seen
    assert child_env["BETTERMEMORY_DIR"] == ""


class _Health:
    """A fake daemon's ``/health`` on 127.0.0.1, reporting `pid`."""

    def __init__(self, pid: int) -> None:
        reported = pid

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                body = json.dumps({"status": "ok", "pid": reported}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    """A process this test started and owns, standing in for a daemon."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env=shielded_child_env(),
    )
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait(10)


def _sandbox_with_record(tmp_path: Path, *, pid: int, port: int) -> tuple[Path, Path]:
    sandbox = tmp_path / "toolcost-home-test"
    state = sandbox / "state"
    state.mkdir(parents=True)
    record = state / "daemon-abc.json"
    record.write_text(
        json.dumps(
            {
                "pid": pid,
                "port": port,
                "token": "t",
                "version": "0",
                "store": str(sandbox / "store" / "memory.sqlite"),
                "started": "2026-10-12T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return sandbox, record


def _stop(sandbox: Path) -> list[int]:
    pids: list[int] = toolcost.stop_sandbox_daemons(
        ["/nonexistent/not-bettermemory"],
        sandbox=str(sandbox),
        env={"HOME": str(sandbox), "BETTERMEMORY_STATE_DIR": str(sandbox / "state")},
        cwd=None,
    )
    return pids


def test_the_fallback_signals_a_pid_its_port_confirms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeper: subprocess.Popen[bytes]
) -> None:
    monkeypatch.setattr(toolcost, "_STOP_TIMEOUT", 5.0)
    health = _Health(sleeper.pid)
    try:
        sandbox, _ = _sandbox_with_record(tmp_path, pid=sleeper.pid, port=health.port)
        assert _stop(sandbox) == [sleeper.pid]
    finally:
        health.close()
    assert sleeper.wait(10) is not None


def test_the_fallback_skips_a_pid_another_daemon_answers_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeper: subprocess.Popen[bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The record's pid may since name an unrelated process: the daemon
    on its port reporting another pid means it is not this probe's."""
    monkeypatch.setattr(toolcost, "_STOP_TIMEOUT", 0.2)
    health = _Health(sleeper.pid + 100000)
    try:
        sandbox, _ = _sandbox_with_record(tmp_path, pid=sleeper.pid, port=health.port)
        _stop(sandbox)
    finally:
        health.close()
    assert sleeper.poll() is None
    assert f"not signalling pid {sleeper.pid}" in capsys.readouterr().err


def test_the_fallback_skips_a_pid_nothing_answers_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeper: subprocess.Popen[bytes],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(toolcost, "_STOP_TIMEOUT", 0.2)
    sandbox, _ = _sandbox_with_record(tmp_path, pid=sleeper.pid, port=_free_port())
    _stop(sandbox)
    assert sleeper.poll() is None
    assert f"not signalling pid {sleeper.pid}" in capsys.readouterr().err


def test_the_fallback_skips_a_record_outside_the_sandbox(
    tmp_path: Path, sleeper: subprocess.Popen[bytes]
) -> None:
    """Even with its port confirming the pid, a record outside the
    sandbox is not this probe's to act on."""
    health = _Health(sleeper.pid)
    try:
        elsewhere = tmp_path / "elsewhere"
        _, record = _sandbox_with_record(elsewhere, pid=sleeper.pid, port=health.port)
        found = {"pid": sleeper.pid, "port": health.port, "state_file": str(record)}
        sandbox = tmp_path / "toolcost-home-test"
        sandbox.mkdir()
        assert not toolcost._may_signal(found, str(sandbox))
        assert toolcost._may_signal(found, str(elsewhere))
    finally:
        health.close()
    assert sleeper.poll() is None


def _record_down(monkeypatch: pytest.MonkeyPatch, sandbox: Path) -> list[list[str]]:
    """`subprocess.run` calls from the stop, with one dead daemon record
    found in the sandbox so the stop runs and signals nothing."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> None:
        calls.append(list(argv))

    monkeypatch.setattr(toolcost.subprocess, "run", fake_run)
    monkeypatch.setattr(
        toolcost,
        "sandbox_daemons",
        lambda sandbox_dir, env: [
            {"pid": 999999, "port": 1, "state_file": str(sandbox / "x.json")}
        ],
    )
    monkeypatch.setattr(toolcost, "_pid_alive", lambda pid: False)
    return calls


def test_down_runs_when_its_state_dir_is_in_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = tmp_path / "toolcost-home-test"
    sandbox.mkdir()
    calls = _record_down(monkeypatch, sandbox)
    for env in (
        {"HOME": str(sandbox), "BETTERMEMORY_STATE_DIR": str(sandbox / "state")},
        {"HOME": str(sandbox), "XDG_STATE_HOME": str(sandbox / ".local" / "state")},
    ):
        toolcost.stop_sandbox_daemons(
            [sys.executable, "-m", "bettermemory"],
            sandbox=str(sandbox),
            env=env,
            cwd=None,
        )
    # `_down_state_dirs` names no directory from XDG_STATE_HOME on Windows
    # (the user state directory is not the environment's there), so the
    # second `down` is skipped on that platform.
    runs = 1 if sys.platform == "win32" else 2
    assert calls == [[sys.executable, "-m", "bettermemory", "down"]] * runs


def test_down_is_skipped_when_its_state_dir_is_outside_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox = tmp_path / "toolcost-home-test"
    sandbox.mkdir()
    outside = tmp_path / "live"
    calls = _record_down(monkeypatch, sandbox)
    for env in (
        {"HOME": str(sandbox), "BETTERMEMORY_STATE_DIR": str(outside / "state")},
        {"HOME": str(outside)},
        {"HOME": str(sandbox), "XDG_STATE_HOME": str(outside / "state")},
    ):
        toolcost.stop_sandbox_daemons(
            [sys.executable, "-m", "bettermemory"],
            sandbox=str(sandbox),
            env=env,
            cwd=None,
        )
    assert calls == []
    assert capsys.readouterr().err.count("skipping") == 3
