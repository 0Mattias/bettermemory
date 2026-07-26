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
from pathlib import Path
from types import ModuleType

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
