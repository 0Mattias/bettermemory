"""The batching of rustfmt calls in native/tools/parity.py.

`cargo fmt --check` hands rustfmt every target root on one command line,
which Windows refuses past 32,767 characters. The parity tool splits the
roots into batches instead; this holds the split to its two promises: every
file is in exactly one batch, in order, and no batch's joined length passes
the bound.
"""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path
from types import ModuleType

import pytest

PARITY = Path(__file__).resolve().parents[1] / "native" / "tools" / "parity.py"


def _parity() -> ModuleType:
    spec = importlib.util.spec_from_file_location("native_parity_tool", PARITY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _joined(batch: list[str]) -> int:
    return sum(len(path) + 1 for path in batch)


@pytest.mark.parametrize("bound", [1, 40, 200, 8000])
@pytest.mark.parametrize("count", [0, 1, 7, 900])
def test_every_file_in_exactly_one_batch_under_the_bound(
    bound: int, count: int
) -> None:
    parity = _parity()
    rng = random.Random(count * 31 + bound)
    paths = [
        f"crates/bettermemory-core/tests/golden_{i}_{'x' * rng.randint(0, 30)}.rs"
        for i in range(count)
    ]
    batches = parity.fmt_batches(paths, bound)
    assert [path for batch in batches for path in batch] == paths
    assert all(batch for batch in batches)
    for batch in batches:
        assert _joined(batch) <= bound or len(batch) == 1


def test_default_bound_stays_far_under_the_windows_limit() -> None:
    parity = _parity()
    assert parity.FMT_ARG_BOUND * 2 < 32767
    paths = [f"crates/c/tests/t{i:04}.rs" for i in range(5000)]
    batches = parity.fmt_batches(paths)
    assert len(batches) > 1
    assert all(_joined(batch) <= parity.FMT_ARG_BOUND for batch in batches)
