"""Benches keep their scratch stores' keys out of the user's keys
directory (ENG-106).

A store keeps its key in the keys directory it is opened with, and a
store opened with none keeps it in `BETTERMEMORY_KEYS_DIR`, else the
user's config directory (`log.default_keys_dir`). Benches that created
their stores without naming one left a key there for every run, hundreds
of keys of stores that no longer exist. Pinned here: each bench's store-creating
entry point, run under a fake home with no `BETTERMEMORY_KEYS_DIR`,
leaves the user's keys directory as it found it (absent), and keeps the
key where the bench's scratch directory removes it. The toolcost probe's
child gets the sandbox's keys directory in `test_bench_toolcost`.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from bettermemory import log as _log
from bettermemory.config import KEYS_DIR_ENV
from bettermemory.store import Store

_ROOT = Path(__file__).resolve().parents[1]
_BENCH = _ROOT / "bench"
_V8_FIXTURE = _ROOT / "tests" / "fixtures" / "v8" / "store"


def _load(name: str, path: Path, *, beside: Path | None = None) -> ModuleType:
    if beside is not None and str(beside) not in sys.path:
        sys.path.insert(0, str(beside))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def user_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The user's keys directory under a fake home, with nothing naming
    another: where a store opened without a keys directory keeps its key.
    Absent at the start; the test fails if it exists at the end."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(KEYS_DIR_ENV, raising=False)
    keys = home / "config" / "bettermemory" / "keys"
    monkeypatch.setattr(_log, "default_keys_dir", lambda: keys)
    yield keys
    assert not keys.exists(), sorted(p.name for p in keys.iterdir())


def _key_files(keys: Path) -> list[str]:
    return sorted(p.name for p in keys.glob("*.key"))


def _corpus(tmp_path: Path, n: int = 3) -> Path:
    rows = (_BENCH / "retrieval" / "corpus.jsonl").read_text(encoding="utf-8")
    picked = [line for line in rows.splitlines() if line.strip()][:n]
    path = tmp_path / "corpus.jsonl"
    path.write_text("".join(json.dumps(json.loads(r)) + "\n" for r in picked))
    return path


def test_the_storage_bench_keeps_its_key_in_its_own_directory(
    user_keys: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _load("bench_keys_storage", _BENCH / "storage.py")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    result = storage._run_size(3, search_repeats=1, load_repeats=1)
    assert result["n"] == 3
    # The bench directory went, and the key with it.
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("bench", ["retrieval", "parity"])
def test_the_corpus_benches_keep_their_key_beside_the_store(
    bench: str, user_keys: Path, tmp_path: Path
) -> None:
    runner = _load(f"bench_keys_{bench}_run", _BENCH / bench / "run.py")
    root = tmp_path / "store"
    corpus = _corpus(tmp_path)
    if bench == "retrieval":
        ids, n = runner.build_store(root, corpus, pad_to=None)
        assert n == 3
    else:
        ids = runner.build_store(root, corpus)
    assert len(ids) == 3
    keys = runner.scratch_keys(root)
    assert keys.is_relative_to(root)
    held = _key_files(keys)
    assert len(held) == 1
    # The bench's reopen (its run's second open) finds the key and makes
    # no other.
    with Store(root, keys_dir=runner.scratch_keys(root)) as reopened:
        assert reopened.has_key
    assert _key_files(keys) == held


def test_the_latency_import_keeps_its_key_beside_the_store(
    user_keys: Path, tmp_path: Path
) -> None:
    latency = _load("bench_keys_latency", _BENCH / "daemon" / "latency.py")
    store_dir = tmp_path / "run" / "store"
    report = latency.import_copy(_V8_FIXTURE, store_dir)
    assert report
    assert len(_key_files(tmp_path / "run" / "keys")) == 1


def test_the_latency_import_takes_the_scratch_keys_variable(
    user_keys: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The arms point `BETTERMEMORY_KEYS_DIR` at the scratch directory;
    the import keeps its key there, where the arms' opens look."""
    latency = _load("bench_keys_latency", _BENCH / "daemon" / "latency.py")
    scratch_keys = tmp_path / "scratch" / "keys"
    monkeypatch.setenv(KEYS_DIR_ENV, str(scratch_keys))
    latency.import_copy(_V8_FIXTURE, tmp_path / "scratch" / "store")
    assert len(_key_files(scratch_keys)) == 1


def _hold_env(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> None:
    """Record `names` with the monkeypatch so whatever the code under test
    sets in `os.environ` is undone, set or unset as they were."""
    for name in names:
        before = os.environ.get(name)
        monkeypatch.setenv(name, before if before is not None else "")
        if before is None:
            monkeypatch.delenv(name)


_SCRATCH_ENV = (
    "HOME",
    "BETTERMEMORY_DIR",
    "BETTERMEMORY_STATE_DIR",
    "BETTERMEMORY_KEYS_DIR",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "PYTHONPATH",
)


def test_the_capture_world_refuses_without_a_scratch_keys_directory(
    user_keys: Path, tmp_path: Path
) -> None:
    common = _load("bench_keys_capture_common", _BENCH / "capture" / "_common.py")
    with pytest.raises(SystemExit, match="scratch_env"):
        common.World(tmp_path / "store")


def test_the_capture_world_keeps_its_key_in_the_scratch_directory(
    user_keys: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = _load("bench_keys_capture_common", _BENCH / "capture" / "_common.py")
    _hold_env(monkeypatch, _SCRATCH_ENV)
    monkeypatch.setattr(sys, "path", list(sys.path))
    scratch = tmp_path / "scratch"
    common.scratch_env(scratch)
    world = common.World(scratch / "store")
    try:
        assert world.store.has_key
    finally:
        world.store.close()
    assert len(_key_files(scratch / "keys")) == 1


def test_the_swarm_seed_keeps_its_key_where_it_is_told(
    user_keys: Path, tmp_path: Path
) -> None:
    swarm = _load("bench_keys_swarm", _BENCH / "swarm.py")
    swarm._seed_corpus(tmp_path / "store", tmp_path / "keys", 2, 7)
    assert len(_key_files(tmp_path / "keys")) == 1


def test_the_integrity_arm_keeps_its_key_in_its_scratch_directory(
    user_keys: Path, tmp_path: Path
) -> None:
    adapters = _load(
        "bench_keys_integrity_adapters",
        _BENCH / "integrity" / "adapters.py",
        beside=_BENCH / "integrity",
    )
    arm = adapters.BetterMemoryAdapter(tmp_path / "arm")
    arm.reset()
    try:
        assert len(_key_files(arm.scratch / "keys")) == 1
    finally:
        arm.close()


def test_the_migration_harness_keeps_its_key_in_its_scratch_directory(
    user_keys: Path, tmp_path: Path
) -> None:
    harness = _load(
        "bench_keys_parity_migrate_v8",
        _BENCH / "parity" / "migrate_v8.py",
        beside=_BENCH / "parity",
    )
    harness.run_migration(tmp_path / "run", harness.FIXTURE)
    assert len(_key_files(tmp_path / "run" / "keys")) == 1


#: Bench directories that hold data, not benches, and are not read here.
_PROTECTED = ("integrity/v1/", "decisions/data/", "decisions/results/")


def _bench_sources() -> list[Path]:
    return [
        path
        for path in sorted(_BENCH.rglob("*.py"))
        if not path.relative_to(_BENCH).as_posix().startswith(_PROTECTED)
    ]


def _store_calls_without_scratch_keys(path: Path) -> list[str]:
    """Each call in ``path`` that builds a store other than through
    `scratch_store`: `Store(...)`, `Store.create(...)`,
    `Store.open_or_create(...)`; a `Store.open(...)` that may rekey (one
    with ``allow_rekey=False`` opens an existing store and never writes a
    key); a `migrate_v8(...)` that names no ``keys_dir``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        names = {k.arg for k in node.keywords}
        no_rekey = any(
            k.arg == "allow_rekey"
            and isinstance(k.value, ast.Constant)
            and k.value.value is False
            for k in node.keywords
        )
        where = f"{path.relative_to(_BENCH).as_posix()}:{node.lineno}"
        if isinstance(func, ast.Name) and func.id == "Store":
            found.append(where)
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "Store"
        ):
            if func.attr in ("create", "open_or_create") or (
                func.attr == "open" and not no_rekey
            ):
                found.append(where)
        elif isinstance(func, ast.Name) and func.id == "migrate_v8":
            if "keys_dir" not in names:
                found.append(where)
    return found


def test_every_bench_builds_its_stores_through_scratch_store() -> None:
    """No bench key lands in the user's keys directory, for every bench:
    each store a bench builds or reopens goes through
    `bench/scratch_store.scratch_store`, which names its keys directory,
    rather than wherever the environment happened to point (cold review
    L4)."""
    sources = _bench_sources()
    assert _BENCH / "scratch_store.py" in sources
    offenders = [
        where
        for path in sources
        if path.name != "scratch_store.py"
        for where in _store_calls_without_scratch_keys(path)
    ]
    assert offenders == [], offenders


def test_scratch_store_names_a_scratch_keys_directory(
    user_keys: Path, tmp_path: Path
) -> None:
    helper = _load("bench_keys_scratch_store", _BENCH / "scratch_store.py")
    with pytest.raises(SystemExit, match="the user's keys directory"):
        helper.scratch_store(tmp_path / "refused" / "store", user_keys)
    with helper.scratch_store(tmp_path / "bench" / "store") as store:
        store.write(content="the bench cache holds 3 entries", scopes=["infra"])
        store_id = store.store_id
    assert _key_files(tmp_path / "bench" / "keys") == [f"{store_id}.key"]
