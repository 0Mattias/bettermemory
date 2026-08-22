"""Pack the pinned corpus into pre-tokenized uint16 shards — the F1 dataloader's substrate.

One shard per input chunk file, encoded with the unit's own tokenizer,
documents delimited by ``[EOS]``. Each shard's bytes are a pure
function of its input file and tokenizer.json, so worker count and
scheduling never touch the artifact. A finished shard leaves a sidecar
stat file; an interrupted packing run resumes by skipping inputs whose
sidecar already exists, and the final manifest is assembled from the
sidecars in sorted order.

The subset a run packs is declared per source as a chunk-file count:
``--spec source=K`` takes the K lexicographically first chunk files
(``K=all`` takes the whole source). The manifest records the spec so
the shard set is reproducible from the declaration and the pinned
snapshot alone.
"""

from __future__ import annotations

import os

for _var in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_var, "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import argparse  # noqa: E402  (env pins must precede numpy)
import gzip  # noqa: E402  (env pins must precede numpy)
import hashlib  # noqa: E402  (env pins must precede numpy)
import json  # noqa: E402  (env pins must precede numpy)
import sys  # noqa: E402  (env pins must precede numpy)
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402  (env pins must precede numpy)

import numpy as np  # noqa: E402  (env pins must precede numpy)

_TOKENIZER = None
_FLUSH_TOKENS = 8 << 20  # tokens buffered before an append to the tmp file


def _init_worker(tokenizer_path: str) -> None:
    # Hard-set in the worker itself, before the Rust side can size a thread
    # pool: one worker = one thread, parallelism comes from the process pool.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    global _TOKENIZER
    from tokenizers import Tokenizer

    _TOKENIZER = Tokenizer.from_file(tokenizer_path)


def _pack_one(job: tuple[str, str, str, str]) -> dict[str, object]:
    source, in_path, out_path, sidecar_path = job
    assert _TOKENIZER is not None
    encode = _TOKENIZER.encode
    eos_id = _TOKENIZER.token_to_id("[EOS]")
    docs = 0
    tokens = 0
    digest = hashlib.sha256()
    parts: list[np.ndarray] = []
    buffered = 0

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "wb") as out:

        def _flush() -> None:
            nonlocal parts, buffered
            if not parts:
                return
            arr = np.concatenate(parts)
            if int(arr.max(initial=0)) > np.iinfo(np.uint16).max:
                raise ValueError(f"token id overflows uint16 in {in_path}")
            blob = arr.tobytes()
            digest.update(blob)
            out.write(blob)
            parts = []
            buffered = 0

        with gzip.open(in_path, "rt", encoding="utf-8") as fh:
            for line in fh:
                ids = encode(json.loads(line)["text"]).ids
                ids.append(eos_id)
                arr = np.asarray(ids, dtype=np.uint16)
                parts.append(arr)
                docs += 1
                tokens += arr.size
                buffered += arr.size
                if buffered >= _FLUSH_TOKENS:
                    _flush()
        _flush()
    os.replace(tmp_path, out_path)
    stat = {
        "source": source,
        "input": in_path,
        "output": out_path,
        "docs": docs,
        "tokens": tokens,
        "bytes": tokens * 2,
        "sha256": digest.hexdigest(),
    }
    Path(sidecar_path).write_text(json.dumps(stat, sort_keys=True) + "\n")
    return stat


def _parse_spec(spec_args: list[str]) -> dict[str, int | None]:
    spec: dict[str, int | None] = {}
    for item in spec_args:
        name, _, count = item.partition("=")
        if not count:
            raise SystemExit(f"bad --spec entry (want source=K or source=all): {item}")
        spec[name] = None if count == "all" else int(count)
    return spec


def _jobs(
    corpus_dir: Path, out_dir: Path, spec: dict[str, int | None]
) -> list[tuple[str, str, str, str]]:
    jobs: list[tuple[str, str, str, str]] = []
    for source, count in sorted(spec.items()):
        source_dir = corpus_dir / source
        chunks = sorted(source_dir.glob("*.jsonl.gz"))
        if not chunks:
            raise SystemExit(f"no chunk files under {source_dir}")
        if count is not None:
            chunks = chunks[:count]
        for chunk in chunks:
            out_path = (
                out_dir / source / (chunk.name.removesuffix(".jsonl.gz") + ".bin")
            )
            sidecar = out_path.with_suffix(".bin.stat.json")
            jobs.append((source, str(chunk), str(out_path), str(sidecar)))
    return jobs


def pack(args: argparse.Namespace) -> int:
    corpus_dir = Path(args.corpus_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = _parse_spec(args.spec)

    jobs = _jobs(corpus_dir, out_dir, spec)
    pending = [j for j in jobs if not Path(j[3]).exists()]
    print(f"{len(jobs)} shards in spec, {len(pending)} to pack", flush=True)

    if pending:
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_init_worker,
            initargs=(args.tokenizer,),
        ) as pool:
            for i, stat in enumerate(pool.map(_pack_one, pending, chunksize=1), 1):
                print(
                    f"[{i}/{len(pending)}] {stat['output']} "
                    f"docs={stat['docs']} tokens={stat['tokens']}",
                    flush=True,
                )

    stats = [
        json.loads(Path(sidecar).read_text())
        for _, _, _, sidecar in sorted(jobs, key=lambda j: j[2])
    ]
    tokenizer_sha = hashlib.sha256(Path(args.tokenizer).read_bytes()).hexdigest()
    manifest = {
        "tokenizer_sha256": tokenizer_sha,
        "dtype": "uint16",
        "delimiter": "[EOS] token appended after every document",
        "spec": {k: ("all" if v is None else v) for k, v in sorted(spec.items())},
        "shards": stats,
        "total_shards": len(stats),
        "total_docs": sum(int(s["docs"]) for s in stats),
        "total_tokens": sum(int(s["tokens"]) for s in stats),
    }
    manifest_path = (
        Path(args.manifest_out) if args.manifest_out else out_dir / "MANIFEST.json"
    )
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(
        f"manifest at {manifest_path}: {manifest['total_shards']} shards, "
        f"{manifest['total_tokens']} tokens",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--tokenizer", required=True, help="path to tokenizer.json")
    parser.add_argument("--out", required=True, help="shard output directory")
    parser.add_argument("--manifest-out", default=None)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument(
        "--spec",
        nargs="+",
        required=True,
        help="per-source chunk counts, e.g. stackexchange=all peS2o=29",
    )
    return pack(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
