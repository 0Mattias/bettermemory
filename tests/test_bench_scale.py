"""The scale bench (bench/scale/, ENG-109) builds its stores through the
store's own write and sign code: a store it replays verifies, its log's
stamps rise with its sequence, and nothing in it reaches the network.

A small run: template sessions through the public API on a store of four
projects, then a replay of them at a hundredth of the live store's shape.
The repositories are real ones made with git under ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from bettermemory.store import Store

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_SCALE = Path(__file__).resolve().parents[1] / "bench" / "scale"


def _load(name: str) -> ModuleType:
    if str(_SCALE) not in sys.path:
        sys.path.insert(0, str(_SCALE))
    spec = importlib.util.spec_from_file_location(name, _SCALE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The bench's scratch environment under `tmp_path`, the process's
    environment and directory put back afterwards."""
    saved = dict(os.environ)
    monkeypatch.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_a_replayed_store_verifies_and_its_stamps_rise(scratch: Path) -> None:
    world = _load("world")
    world.scratch_env(scratch / "templates")
    build = _load("build")
    with world.no_network() as attempts:
        templates = build.calibrate(
            scratch / "templates",
            sessions=3,
            seed=5,
            plan=build.SessionPlan(),
            scale=0.05,
            backdated=30,
        )
    assert attempts == []
    assert len(templates["templates"]) == 3
    assert all(t["rows"] for t in templates["templates"])
    kinds = {r["kind"] for t in templates["templates"] for r in t["rows"]}
    assert {"turn_audited", "memory_put"} <= kinds, kinds

    file = scratch / "templates.json"
    import json

    file.write_text(json.dumps(templates), encoding="utf-8")
    world.scratch_env(scratch / "store")
    with world.no_network() as attempts:
        out = build.generate(scratch / "store", scale=0.01, templates_file=file, seed=3)
    assert attempts == []
    census = out["census"]
    assert census["memories"] > 0 and census["log_rows"] > 100
    assert out["replay_counts"]["sights"] >= 3

    with Store(
        scratch / "store" / "store" / "memory.sqlite",
        keys_dir=scratch / "store" / "keys",
    ) as store:
        report = store.log_verify()
        stamps = [
            str(r[0]) for r in store.conn.execute("SELECT ts FROM log ORDER BY seq")
        ]
    assert report["status"] == "ok", report["problems"][:3]
    assert stamps == sorted(stamps)
