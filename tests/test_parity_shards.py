"""The shards of native/tools/parity.py and the CI matrix that runs them.

The CI job `native parity (<os>, K/N)` runs ``parity.py --shard K/N``. The
split promises that every test target is in exactly one shard for every N,
that the unit, example and doc tests are in exactly one, that step 1b's
release binaries are each in exactly one, and that no batch of ``--test``
arguments passes the command-line bound. The workflow's matrix must name
exactly the shards 1 to N of the N its run line passes, or a shard goes
unrun with every job green.
"""

from __future__ import annotations

import importlib.util
import random
import re
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PARITY = ROOT / "native" / "tools" / "parity.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _parity() -> ModuleType:
    spec = importlib.util.spec_from_file_location("native_parity_tool", PARITY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _real_targets() -> list[str]:
    """The tree's integration test targets as cargo discovers them: every
    root file under crates/*/tests, sorted by package and name. No manifest
    declares a [[test]] of its own or turns discovery off, so the files are
    the targets (cargo metadata is not run here: the test jobs have no
    pinned toolchain)."""
    found: list[tuple[str, str]] = []
    for manifest in sorted((ROOT / "native" / "crates").glob("*/Cargo.toml")):
        text = manifest.read_text(encoding="utf-8")
        assert "[[test]]" not in text and "autotests" not in text, manifest
        package = re.search(r'^name = "([^"]+)"', text, re.MULTILINE)
        assert package is not None
        for root in (manifest.parent / "tests").glob("*.rs"):
            found.append((package.group(1), root.stem))
    return [name for _, name in sorted(found)]


def _synthetic(count: int) -> list[str]:
    rng = random.Random(count)
    return sorted(f"golden_{i:04}_{'x' * rng.randint(0, 40)}" for i in range(count))


def _joined(args: list[str]) -> int:
    return sum(len(arg) + 1 for arg in args)


def _check_partition(parity: ModuleType, targets: list[str], n: int) -> None:
    items = parity.shard_items(targets)
    shards = [parity.shard_select(items, k, n) for k in range(1, n + 1)]
    flat = [item for shard in shards for item in shard]
    assert sorted(flat) == sorted(items)
    assert len(flat) == len(set(flat)) == len(items)
    assert sum(parity.UNIT_ITEM in shard for shard in shards) == 1
    sizes = [len(shard) for shard in shards]
    assert max(sizes) - min(sizes) <= 1
    for name in parity.RELEASE_TESTS:
        if name in targets:
            assert sum(name in shard for shard in shards) == 1
            assert (
                sum(bool(parity.test_calls(shard, release=True)) for shard in shards)
                == 1
            )
    named: list[str] = []
    unit_calls = 0
    for shard in shards:
        for cmd in parity.test_calls(shard):
            if "--test" in cmd:
                tail = cmd[cmd.index("--test") :]
                assert _joined(tail) <= parity.FMT_ARG_BOUND
                assert tail[0::2] == ["--test"] * (len(tail) // 2)
                named += tail[1::2]
            else:
                unit_calls += 1
    assert unit_calls == 2
    assert sorted(named) == sorted(targets)


@pytest.mark.parametrize("n", range(1, 9))
@pytest.mark.parametrize("count", [0, 1, 7, 650, 2000])
def test_every_target_in_exactly_one_shard(n: int, count: int) -> None:
    parity = _parity()
    targets = _synthetic(count)
    if count:
        targets[count // 2] = "golden_pyre_fold"
        targets.sort()
    _check_partition(parity, targets, n)


@pytest.mark.parametrize("n", range(1, 9))
def test_the_trees_targets_split_into_exactly_one_shard_each(n: int) -> None:
    parity = _parity()
    targets = _real_targets()
    assert len(targets) > 100
    assert set(parity.RELEASE_TESTS) <= set(targets)
    _check_partition(parity, targets, n)


def test_no_shard_plans_the_unsharded_calls() -> None:
    parity = _parity()
    assert parity.test_calls(None) == [["cargo", "test", "--locked", "--no-fail-fast"]]
    assert parity.test_calls(None, release=True) == [
        [
            "cargo",
            "test",
            "--locked",
            "--release",
            "--no-fail-fast",
            *(arg for name in parity.RELEASE_TESTS for arg in ("--test", name)),
        ]
    ]


def test_a_shard_past_the_items_selects_nothing() -> None:
    parity = _parity()
    items = parity.shard_items(["a", "b"])
    assert parity.shard_select(items, 4, 4) == []
    assert parity.test_calls([]) == []


@pytest.mark.parametrize("text", ["0/4", "5/4", "1", "a/b", "1/0"])
def test_a_malformed_shard_is_refused(text: str) -> None:
    parity = _parity()
    with pytest.raises(Exception):
        parity.parse_shard(text)


def test_the_ci_matrix_runs_every_shard_of_the_tools_split() -> None:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    job = jobs["native-parity"]
    shards = job["strategy"]["matrix"]["shard"]
    runs = [step["run"] for step in job["steps"] if "parity.py" in step.get("run", "")]
    assert len(runs) == 1
    found = re.search(r"--shard \$\{\{ matrix\.shard \}\}/(\d+)", runs[0])
    assert found is not None, runs[0]
    n = int(found.group(1))
    assert shards == list(range(1, n + 1))
    assert "--lint" not in runs[0]
    assert f"/{n})" in job["name"]
    assert job["strategy"]["fail-fast"] is False
    lint = jobs["native-lint"]
    lint_runs = [s["run"] for s in lint["steps"] if "parity.py" in s.get("run", "")]
    assert len(lint_runs) == 1 and "--lint-only" in lint_runs[0]
    assert lint["strategy"]["matrix"]["os"] == job["strategy"]["matrix"]["os"]
