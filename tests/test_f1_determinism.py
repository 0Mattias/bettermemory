"""F1's CI determinism leg: the stage-P trainer reproduces itself on CPU.

The F1 unit's clause-3 CI guard for the torch trainer class: a reduced
synthetic shard driven through ``bench/w/f1_train_torch.py`` twice at a
tiny CPU config, weights byte-equality across the two trains. No corpus
bytes — the fixture stream is arithmetic. The CI venv carries no torch;
the trains run as subprocesses via ``uv run --with torch``
(``UV_TORCH_BACKEND=cpu`` keeps the wheel CPU-only), the idiom the
w-lane determinism legs established for numpy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_TRAINER = _ROOT / "bench" / "w" / "f1_train_torch.py"


def _write_fixture(base: Path) -> tuple[Path, Path]:
    shard_dir = base / "shards" / "news"
    shard_dir.mkdir(parents=True)
    stream = bytearray()
    for i in range(20000):
        tok = 5 if i % 97 == 96 else 6 + ((i * 7919) % 500)
        stream += int(tok).to_bytes(2, "little")
    bin_path = shard_dir / "news.chunk.00.bin"
    bin_path.write_bytes(bytes(stream))
    manifest = {
        "tokenizer_sha256": "fixture",
        "dtype": "uint16",
        "spec": {"news": 1},
        "shards": [
            {
                "source": "news",
                "input": "synthetic",
                "output": str(bin_path),
                "docs": 206,
                "tokens": 20000,
                "bytes": 40000,
                "sha256": hashlib.sha256(bytes(stream)).hexdigest(),
            }
        ],
        "total_shards": 1,
        "total_docs": 206,
        "total_tokens": 20000,
    }
    (base / "shards" / "MANIFEST.json").write_text(json.dumps(manifest))
    tok_manifest = base / "tokenizer_manifest.json"
    tok_manifest.write_text(
        json.dumps({"vocab_size": 512, "tokenizer_sha256": "fixture"})
    )
    return base / "shards", tok_manifest


def _train(out: Path, shards: Path, tok_manifest: Path) -> str:
    env = dict(os.environ, UV_TORCH_BACKEND="cpu")
    result = subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "torch",
            "--with",
            "numpy",
            "python",
            str(_TRAINER),
            "--shards",
            str(shards),
            "--tokenizer-manifest",
            str(tok_manifest),
            "--out",
            str(out),
            "--device",
            "cpu",
            "--no-compile",
            "--steps",
            "3",
            "--micro-batch",
            "2",
            "--accum",
            "2",
            "--seq",
            "64",
            "--hidden",
            "32",
            "--layers",
            "2",
            "--heads",
            "2",
            "--ffn",
            "48",
            "--window",
            "16",
            "--warmup",
            "1",
            "--log-every",
            "3",
            "--ckpt-every",
            "99",
        ],
        check=True,
        env=env,
        cwd=_ROOT,
        timeout=900,
        capture_output=True,
        text=True,
    )
    assert "stage P complete" in result.stdout, result.stdout[-500:]
    return hashlib.sha256((out / "weights_p.npz").read_bytes()).hexdigest()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_f1_trainer_reproduces_itself_cpu(tmp_path: Path) -> None:
    shards, tok_manifest = _write_fixture(tmp_path)
    first = _train(tmp_path / "run1", shards, tok_manifest)
    second = _train(tmp_path / "run2", shards, tok_manifest)
    assert first == second
