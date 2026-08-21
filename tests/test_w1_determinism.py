"""G3's CI leg: the W1 trainer reproduces itself bit-for-bit.

The determinism bar of the W1 declaration §7 has a CI half:
retrain the committed reduced register twice and require byte-identical
artifacts on every push. This test is that check. It runs the real
trainer and the real emitter — `bench/w/w1_train.py` and
`bench/w/w1_emit.py`, the same entry points the full run uses — as
``uv run --with numpy`` subprocesses, so numpy stays a bench-side
build dependency exactly as the declaration admits it: nothing in the
shipped package or its dependency metadata changes, and the layered
install resolves from uv's cache in CI.

The corpus is `bench/w/ci_register/reduced.txt` — committed,
Gutenberg-derived public-domain bytes pinned in `bench/w/corpora.json`
— and the hyperparameters are scaled far down; what is under test is
the mechanism (seeded RNG order, stable-sort reductions, single-thread
BLAS pins), which is parameter-independent. A mismatch here means a
nondeterminism crept into the training chain, and the full-register
retrain proof the unit's gate read depends on would fail with it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_REDUCED = _ROOT / "bench" / "w" / "ci_register" / "reduced.txt"

_TRAIN_ARGS = [
    "--ci-register",
    "bench/w/ci_register/reduced.txt",
    "--token-cap",
    "120000",
    "--dim",
    "16",
    "--buckets",
    "4096",
    "--epochs",
    "1",
    "--vocab-cap",
    "4000",
    "--min-count",
    "5",
    "--batch",
    "1024",
    "--lr",
    "0.025",
    "--seed",
    "20260816",
]


def _run(script: str, *args: str) -> None:
    subprocess.run(
        ["uv", "run", "--with", "numpy", "python", script, *args],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        timeout=600,
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_w1_retrain_is_bit_identical(tmp_path: Path) -> None:
    assert _REDUCED.is_file(), "the committed reduced register is missing"
    digests: list[dict[str, str]] = []
    for attempt in ("a", "b"):
        out = tmp_path / attempt
        _run("bench/w/w1_train.py", "--out", str(out), *_TRAIN_ARGS)
        _run(
            "bench/w/w1_emit.py",
            "--run",
            str(out),
            "--out",
            str(out / "table.py"),
            "--floor",
            "0.35",
        )
        digests.append(
            {
                name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                for name in ("inp.npy", "ctx.npy", "vocab.txt", "table.py")
            }
        )
    assert digests[0] == digests[1], (
        "the W1 retrain diverged; a nondeterminism entered the training chain"
    )
