"""The breakdown and uvx arms of the daemon latency bench
(bench/daemon/breakdown.py): the parts it derives, the store it places in
the scratch, and the uvx arm's refusal to time anything it cannot resolve
offline. No daemon is started and nothing is timed here."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from typing import Any
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / "bench" / "daemon" / "breakdown.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_daemon_breakdown", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_daemon_breakdown"] = module
    spec.loader.exec_module(module)
    return module


breakdown = _load()


def _arm(p50: float) -> dict[str, float]:
    return {"n": 1, "p50_ms": p50, "p95_ms": p50, "min_ms": p50, "max_ms": p50}


def test_the_parts_are_differences_of_medians() -> None:
    results = {
        "shell": _arm(5),
        "interpreter": _arm(15),
        "client_import": _arm(45),
        "http_round_trip": _arm(2),
        "service": _arm(20),
        "hook_process": _arm(80),
    }
    assert breakdown.derived(results) == {
        "launcher_start": 5,
        "interpreter_start": 10,
        "client_import": 30,
        "http_round_trip": 2,
        "daemon_work": 18,
        "hook_process_rest": 10,
        "hook_process": 80,
    }


def _store(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id TEXT)")
    conn.executemany("INSERT INTO memories VALUES (?)", [("a",), ("b",)])
    conn.commit()
    conn.close()
    return path


def _never(path: Path) -> None:
    return None


def test_a_snapshot_lands_in_the_scratch_with_counts_only(tmp_path: Path) -> None:
    source = _store(tmp_path / "live" / "memory.sqlite")
    scratch = tmp_path / "scratch"
    store = breakdown.place_store(
        scratch, store_copy=None, snapshot_from=source, refuse=_never
    )
    assert (scratch / "store" / "memory.sqlite").is_file()
    assert store["rows"] == {"memories": 2}
    assert str(tmp_path) not in repr(store)
    with pytest.raises(SystemExit):
        breakdown.place_store(
            scratch, store_copy=None, snapshot_from=source, refuse=_never
        )


def test_a_store_copy_goes_through_the_backup_api_and_the_refusal(
    tmp_path: Path,
) -> None:
    source = _store(tmp_path / "scale" / "store" / "memory.sqlite")
    (tmp_path / "scale" / "keys").mkdir()
    (tmp_path / "scale" / "keys" / "k").write_text("x", encoding="utf-8")
    store = breakdown.place_store(
        tmp_path / "s1", store_copy=source, snapshot_from=None, refuse=_never
    )
    assert store["source"] == "store-copy with its scratch keys"
    assert (tmp_path / "s1" / "keys" / "k").is_file()

    def refuse(path: Path) -> None:
        raise SystemExit("live")

    with pytest.raises(SystemExit):
        breakdown.place_store(
            tmp_path / "s2", store_copy=source, snapshot_from=None, refuse=refuse
        )


def test_the_uvx_arm_times_nothing_it_cannot_resolve_offline(tmp_path: Path) -> None:
    env = breakdown.uvx_env(tmp_path, {"PATH": str(tmp_path / "empty")})
    assert env["UV_OFFLINE"] == "1"
    assert set(env) <= {*breakdown.KEPT_ENV, "HOME", "UV_OFFLINE", "UV_CACHE_DIR"} | {
        "BETTERMEMORY_DIR",
        "BETTERMEMORY_KEYS_DIR",
        "BETTERMEMORY_STATE_DIR",
    }
    assert breakdown.uvx_resolves(env, "9.9.9") == {
        "resolved": False,
        "why": "no_uvx",
    }


def _fake_uvx(tmp_path: Path, body: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    uvx = bin_dir / "uvx"
    uvx.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    uvx.chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin"}


def test_a_cached_uvx_of_another_version_is_not_timed(tmp_path: Path) -> None:
    env = _fake_uvx(tmp_path, "echo 0.0.1")
    resolution = breakdown.uvx_resolves(env, "9.9.9")
    assert resolution["why"] == "version_mismatch"
    assert resolution["version"] == "0.0.1"
    assert breakdown.uvx_resolves(_fake_uvx(tmp_path / "b", "echo 9.9.9"), "9.9.9") == {
        "resolved": True,
        "version": "9.9.9",
    }


def test_a_uvx_failure_is_named_by_category_only(tmp_path: Path) -> None:
    env = _fake_uvx(
        tmp_path, "echo 'error: /private/x not found in the cache' >&2; exit 2"
    )
    resolution = breakdown.uvx_resolves(env, "9.9.9")
    assert resolution == {"resolved": False, "why": "not_in_cache", "exit": 2}


def _done(code: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", "")


def test_the_daemon_is_stopped_even_when_up_fails(tmp_path: Path) -> None:
    calls: list[str] = []

    def cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append(args[0])
        return _done(1 if args[0] == "up" else 0)

    outcome: dict[str, Any] = {}
    with pytest.raises(SystemExit):
        with breakdown.scratch_daemon(
            tmp_path, {}, cli=cli, released=lambda p: True, outcome=outcome
        ):
            raise AssertionError("the block must not run")
    assert calls == ["up", "down"]
    assert outcome == {"down_ok": True, "store_released": True}


def test_a_daemon_that_keeps_the_store_is_an_error(tmp_path: Path) -> None:
    def cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return _done(0)

    with pytest.raises(SystemExit):
        with breakdown.scratch_daemon(tmp_path, {}, cli=cli, released=lambda p: False):
            pass


class _Client:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.sessions: list[str] = []

    def read_state(self, state_dir: Path, store: Path) -> dict[str, Any]:
        return {"port": 1, "token": "t"}

    def identify(self, state: Any, timeout: float) -> Any:
        self.calls.append("health")
        return type("A", (), {"ours": True})()

    def post(self, port: int, token: str, path: str, body: Any, timeout: float) -> Any:
        self.calls.append("service")
        self.sessions.append(body["session_id"])
        return {}


def test_the_rounds_alternate_and_drop_the_warm_ups(tmp_path: Path) -> None:
    ran: list[str] = []
    stdin_sessions: list[str] = []

    def run(argv: list[str], env: dict[str, str], stdin: str | None) -> Any:
        ran.append(argv[-1])
        if stdin is not None:
            stdin_sessions.append(json.loads(stdin)["session_id"])
        return 0.01, _done(0)

    client = _Client()
    slept: list[float] = []
    n = 3
    prompts = [f"p{i}" for i in range(n + breakdown.BREAKDOWN_WARM_UPS)]
    times = breakdown.run_rounds(
        tmp_path, {}, prompts, 0.5, run=run, client=client, sleep=slept.append
    )
    assert {name: len(v) for name, v in times.items()} == dict.fromkeys(
        breakdown.PARTS, n
    )
    assert client.calls[:4] == ["health", "service", "service", "health"]
    assert slept == [0.5] * 3 * len(prompts)
    sessions = client.sessions + stdin_sessions
    assert len(set(sessions)) == len(sessions)


def test_n_below_one_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        breakdown.run_breakdown(
            scratch=tmp_path / "s",
            store_copy=None,
            snapshot_from=None,
            n=0,
            gap=0,
            prompts=[],
            refuse=_never,
        )
    with pytest.raises(ValueError):
        breakdown.summary_ms([])
