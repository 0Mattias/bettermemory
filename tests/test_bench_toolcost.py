"""Tests for the tool-schema cost probe.

The published number is a comparison against another project, so the
measurement has to be defensible on method as well as arithmetic. Two
things are pinned: that the byte accounting is what it claims to be, and
that the probe cannot silently measure the operator's configuration
instead of the shipped default — which is exactly what it did on its
first run, reporting 27 tools instead of 18.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
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
