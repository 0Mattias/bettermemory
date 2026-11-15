"""`bench/daemon/latency.brief_shape` counts the lines under each label of
the brief as `recall_brief.render_brief` prints it, after the lead lines."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bettermemory.recall_brief import render_brief

_RUNNER = Path(__file__).resolve().parents[1] / "bench" / "daemon" / "latency.py"


def _shape(stdout: str) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("bench_daemon_brief", _RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_daemon_brief"] = module
    spec.loader.exec_module(module)
    shape: dict[str, Any] = module.brief_shape(
        [{"stdout": stdout, "ids": ["x"], "chars": len(stdout)}]
    )
    return shape


def _memory(i: int) -> Any:
    return SimpleNamespace(id=f"01ID{i:022d}", body=f"record {i} body")


def test_the_counts_come_from_the_lines_under_each_label() -> None:
    text, _ = render_brief([_memory(0), _memory(1)], [_memory(2)])
    shape = _shape("bettermemory: a membership lead line\n" + text)
    assert (shape["first_in_force"], shape["first_recent"]) == (2, 1)


def test_a_brief_with_one_list_counts_zero_for_the_other() -> None:
    text, _ = render_brief([], [_memory(0), _memory(1), _memory(2)])
    shape = _shape(text)
    assert (shape["first_in_force"], shape["first_recent"]) == (0, 3)
    assert _shape("")["first_in_force"] == 0
