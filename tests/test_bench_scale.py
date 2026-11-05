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


def test_an_event_loop_runs_where_the_socketpair_is_tcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no AF_UNIX pair: `socket.socketpair` pairs two TCP
    sockets on 127.0.0.1, and every asyncio event loop makes such a pair
    for its self-pipe. The guard lets that connect through, made inside
    `socketpair` to loopback, and still refuses and lists any other: the
    Windows leg's loop, simulated with CPython's own fallback."""
    import asyncio
    import socket

    fallback = getattr(socket, "_fallback_socketpair", None)
    if fallback is None:
        pytest.skip("this Python has no TCP socketpair fallback to simulate with")
    monkeypatch.setattr(socket, "socketpair", fallback)
    world = _load("world")
    with world.no_network() as attempts:
        asyncio.run(asyncio.sleep(0))
        assert attempts == []
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(world._Refused):
                probe.connect(("127.0.0.1", 9))
            assert probe.connect_ex(("127.0.0.1", 9)) == 111
        finally:
            probe.close()
    assert len(attempts) == 2
    assert socket.socketpair is fallback


def test_the_build_reads_its_peak_memory_on_every_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`self_peak_rss_mb`, which the build's result carries, imports no
    POSIX-only module off POSIX: on Windows it reads the peak working set
    (the Windows leg of CI run 41965490499 failed `generate` on
    ``import resource``). Off Windows the read is a positive size."""
    world = _load("world")
    if sys.platform == "win32":
        peak = world.self_peak_rss_mb()
        assert peak is None or peak > 0
        return
    assert world.self_peak_rss_mb() > 0
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(world, "_windows_peak_working_set_mb", lambda: 12.5)
    assert world.self_peak_rss_mb() == 12.5
