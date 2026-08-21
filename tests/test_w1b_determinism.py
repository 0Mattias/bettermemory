"""G3's CI legs for W1b: the revised trainer reproduces itself, and the
segment knob cannot move the emitted bytes.

the W1b declaration §7 extends W1's CI half in three ways,
all covered here:

* the reduced register is the SE-derived slice
  (`bench/w/ci_register/reduced_se.txt`, pinned in
  `bench/w/corpora.json`), so the determinism assert runs over the
  register family this unit actually turns on;
* the same reduced train runs at two segment sizes — one large enough
  to hold the whole stream, one small and deliberately not a multiple
  of the batch size — and must emit identical bytes, the declaration's
  segment-size-invariance clause;
* the Stack Exchange row parser is checked against hand-written rows,
  the `w3p2` CI idiom: the row grammar is exercised without corpus
  bytes or ``bsdtar`` on the CI machine.

The subprocess trains run the real entry points via ``uv run --with
numpy`` exactly as `test_w1_determinism.py` established; the
hyperparameters are scaled far down because what is under test is the
mechanism, which is parameter-independent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parent.parent
_REDUCED = _ROOT / "bench" / "w" / "ci_register" / "reduced_se.txt"


def _load_corpus() -> Any:
    """The W1b reader, loaded by path like the other bench-side tests.

    A file-location load rather than a `sys.path` import: `bench/` is not
    a package on the type checker's search path, and the w3 determinism
    tests already establish this idiom for reaching bench modules.
    """
    spec = importlib.util.spec_from_file_location(
        "w1b_corpus", _ROOT / "bench" / "w" / "w1b_corpus.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["w1b_corpus"] = module
    spec.loader.exec_module(module)
    return module


_TRAIN_ARGS = [
    "--ci-register",
    "bench/w/ci_register/reduced_se.txt",
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
    "20260818",
]


def _run(script: str, *args: str) -> None:
    subprocess.run(
        ["uv", "run", "--with", "numpy", "python", script, *args],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        timeout=600,
    )


def _train_and_emit(out: Path, segment: str) -> dict[str, str]:
    _run(
        "bench/w/w1_train.py",
        "--out",
        str(out),
        "--segment",
        segment,
        *_TRAIN_ARGS,
    )
    _run(
        "bench/w/w1_emit.py",
        "--run",
        str(out),
        "--out",
        str(out / "table.py"),
        "--floor",
        "0.35",
    )
    return {
        name: hashlib.sha256((out / name).read_bytes()).hexdigest()
        for name in ("inp.npy", "ctx.npy", "vocab.txt", "table.py")
    }


def test_se_row_parsing() -> None:
    se_row_doc = _load_corpus().se_row_doc
    question = (
        '  <row Id="1" PostTypeId="1" '
        'Body="&lt;p&gt;Fizz &amp;amp; foam everywhere.&lt;/p&gt;&#xA;" '
        'Title="Why so much foam?" />'
    )
    assert se_row_doc(question) == "Why so much foam? Fizz & foam everywhere."
    answer = (
        '  <row Id="2" PostTypeId="2" '
        'Body="&lt;p&gt;Chill the &lt;b&gt;glass&lt;/b&gt; first.&lt;/p&gt;" />'
    )
    assert se_row_doc(answer) == "Chill the glass first."
    tag_wiki = '  <row Id="3" PostTypeId="4" Body="excerpt text" />'
    assert se_row_doc(tag_wiki) is None
    assert se_row_doc("<posts>") is None
    assert se_row_doc('  <row Id="4" PostTypeId="1" />') is None


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_w1b_retrain_and_segment_invariance(tmp_path: Path) -> None:
    assert _REDUCED.is_file(), "the committed SE reduced register is missing"
    # 50000 holds the whole reduced stream in one segment; 7001 forces
    # many segments and is deliberately not a multiple of the batch
    # size, so the carry path is exercised.
    digests_a = _train_and_emit(tmp_path / "a", "50000")
    digests_b = _train_and_emit(tmp_path / "b", "50000")
    digests_c = _train_and_emit(tmp_path / "c", "7001")
    assert digests_a == digests_b, (
        "the W1b retrain diverged; a nondeterminism entered the training chain"
    )
    assert digests_a == digests_c, (
        "the segment knob moved the emitted bytes; the enumeration is not "
        "segment-invariant"
    )
