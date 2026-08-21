"""G3's CI legs for W2: the trainer reproduces itself, the grammar holds,
and the blend's lambda=0 identity is pinned.

the W2 declaration §8 G3(c): committed fixtures driven through
the tokenizer, stage A, stage B and the blend, byte-equality across two
trains, no corpus bytes, no numpy import in the test venv. The
tokenizer and the pair-row grammar are numpy-free by design
(`bench/w/w2_tokenizer.py`), so those legs run in-process; the trains
and the blend check run as subprocesses via ``uv run --with numpy``,
the idiom `test_w1_determinism.py` established.

Fixtures: the committed SE reduced register
(`bench/w/ci_register/reduced_se.txt`, pinned since W1b) and the
committed synthetic pair fixture
(`bench/w/ci_register/w2_pairs_fixture.tsv` — original hand-written
text, no corpus bytes).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent
_REDUCED = _ROOT / "bench" / "w" / "ci_register" / "reduced_se.txt"
_PAIRS = _ROOT / "bench" / "w" / "ci_register" / "w2_pairs_fixture.tsv"


def _load_tokenizer_module() -> Any:
    """File-location load, the bench-module idiom the w3 tests established."""
    spec = importlib.util.spec_from_file_location(
        "w2_tokenizer", _ROOT / "bench" / "w" / "w2_tokenizer.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w2_tokenizer"] = module
    spec.loader.exec_module(module)
    return module


def test_bpe_is_deterministic_and_reversible() -> None:
    tok = _load_tokenizer_module()
    counts = Counter(
        {
            "toggle": 40,
            "toggles": 12,
            "flag": 35,
            "flags": 9,
            "boolean": 20,
            "rollback": 18,
            "roll": 6,
            "back": 6,
            "error": 50,
            "errors": 14,
            "exception": 30,
        }
    )
    vocab_a, merges_a = tok.train_bpe(counts, 64)
    vocab_b, merges_b = tok.train_bpe(counts, 64)
    assert vocab_a == vocab_b and merges_a == merges_b
    assert vocab_a[:3] == list(tok.SPECIALS)
    bpe = tok.Bpe(vocab_a, merges_a)
    for word in counts:
        ids = bpe.encode_word(word)
        assert ids, word
        rebuilt = "".join(vocab_a[i] for i in ids)
        assert rebuilt == word + "</w>"
    # An unseen word still encodes — down to characters, [unk] only for
    # symbols outside the learned alphabet.
    assert bpe.encode_word("flagged")
    assert bpe.encode_word("日")[0] == bpe.unk_id


def test_pair_row_grammar_and_keep_rule() -> None:
    tok = _load_tokenizer_module()
    good = "site\t1\t2\tleft prose here\tright prose here\tleft markup\tright markup\n"
    assert tok.parse_pair_row(good) == ("left prose here", "right prose here")
    assert tok.parse_pair_row("only\tthree\tcolumns\n") is None
    assert tok.keep_pair([1] * 8, [2] * 8, 8)
    assert not tok.keep_pair([1] * 7, [2] * 8, 8)
    assert not tok.keep_pair([1] * 8, [2] * 7, 8)


_TRAIN_ARGS = [
    "--ci-register",
    "bench/w/ci_register/reduced_se.txt",
    "--pairs-tsv",
    "bench/w/ci_register/w2_pairs_fixture.tsv",
    "--token-cap",
    "30000",
    "--vocab-size",
    "300",
    "--dim",
    "16",
    "--layers",
    "2",
    "--heads",
    "2",
    "--ffn",
    "32",
    "--seq",
    "24",
    "--mlm-epochs",
    "1",
    "--mlm-batch",
    "8",
    "--pair-epochs",
    "2",
    "--pair-batch",
    "4",
    "--pair-min-tokens",
    "4",
    "--seed",
    "20260820",
]


def _run(script: str, *args: str) -> None:
    subprocess.run(
        ["uv", "run", "--with", "numpy", "python", script, *args],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        timeout=600,
    )


def _train(out: Path) -> dict[str, str]:
    _run("bench/w/w2_train.py", "--out", str(out), *_TRAIN_ARGS)
    hashes = {
        name: hashlib.sha256((out / name).read_bytes()).hexdigest()
        for name in ("vocab.txt", "merges.txt", "pretrain.npy", "weights.npy")
    }
    meta = json.loads((out / "meta.json").read_text())
    assert meta["sha256"] == hashes, "meta's own hash block must match the bytes"
    return hashes


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_w2_double_train_is_byte_identical(tmp_path: Path) -> None:
    assert _REDUCED.is_file(), "the committed SE reduced register is missing"
    assert _PAIRS.is_file(), "the committed pair fixture is missing"
    first = _train(tmp_path / "a")
    second = _train(tmp_path / "b")
    assert first == second


_BLEND_CHECK = """
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path("bench/w").resolve()))
from w2_measure import blend_order

rng = np.random.Generator(np.random.PCG64(3))
for trial in range(50):
    n = int(rng.integers(2, 50))
    engine = np.sort(rng.random(n))[::-1].copy()
    if trial % 3 == 0:
        engine[: n // 2] = engine[0]  # ties keep engine order
    cosines = rng.random(n)
    assert blend_order(engine, cosines, 0.0) == list(range(n)), trial
    by_cos = sorted(range(n), key=lambda i: (-cosines[i], i))
    assert blend_order(engine, cosines, 1.0) == by_cos, trial
flat = np.ones(5)
assert blend_order(flat, np.array([0.1, 0.5, 0.2, 0.9, 0.3]), 0.0) == [0, 1, 2, 3, 4]
print("ok")
"""


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_blend_lambda_zero_is_the_identity() -> None:
    result = subprocess.run(
        ["uv", "run", "--with", "numpy", "python", "-c", _BLEND_CHECK],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        timeout=300,
        text=True,
    )
    assert result.stdout.strip() == "ok"
