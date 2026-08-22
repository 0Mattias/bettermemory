"""Train the F1 tokenizer — own byte-level BPE over a declared corpus sample.

The unit's tokenizer is trained from scratch on a deterministic,
declared sample of the pinned snapshot: for every source directory
(sorted), documents are read in file order from the lexicographically
first chunk file until the per-source byte cap is reached. No
randomness, no normalization — the byte-level model sees each
document's text field verbatim. The trainer library is a declared
bench-side training dependency of the unit; the shipped surface stays
zero-dependency.

Outputs: ``tokenizer.json`` (the single consumable artifact) and a
sample manifest recording per-source document counts, sampled bytes,
and the tokenizer.json sha256 — the pins the unit declaration cites.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SPECIALS = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]", "[EOS]"]


def _source_dirs(corpus_dir: Path) -> list[Path]:
    return sorted(
        p for p in corpus_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def _first_chunk(source: Path) -> Path | None:
    chunks = sorted(source.glob("*.jsonl.gz"))
    return chunks[0] if chunks else None


def _sample(
    corpus_dir: Path, per_source_bytes: int
) -> tuple[list[str], list[dict[str, Any]]]:
    texts: list[str] = []
    stats: list[dict[str, Any]] = []
    for source in _source_dirs(corpus_dir):
        chunk = _first_chunk(source)
        if chunk is None:
            stats.append({"source": source.name, "file": None, "docs": 0, "bytes": 0})
            continue
        taken = 0
        docs = 0
        with gzip.open(chunk, "rt", encoding="utf-8") as fh:
            for line in fh:
                text = json.loads(line)["text"]
                texts.append(text)
                taken += len(text.encode("utf-8"))
                docs += 1
                if taken >= per_source_bytes:
                    break
        stats.append(
            {"source": source.name, "file": chunk.name, "docs": docs, "bytes": taken}
        )
        print(f"sampled {source.name}: {docs} docs, {taken} bytes", flush=True)
    return texts, stats


def train(args: argparse.Namespace) -> int:
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers

    corpus_dir = Path(args.corpus_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    texts, stats = _sample(corpus_dir, args.per_source_mb * 1024 * 1024)

    tokenizer = Tokenizer(models.BPE(unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=args.vocab_size,
        special_tokens=SPECIALS,
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        show_progress=True,
    )

    def _iter() -> Iterator[str]:
        yield from texts

    tokenizer.train_from_iterator(_iter(), trainer=trainer, length=len(texts))

    tok_path = out_dir / "tokenizer.json"
    tokenizer.save(str(tok_path))
    tok_sha = hashlib.sha256(tok_path.read_bytes()).hexdigest()

    import tokenizers as tokenizers_mod

    manifest = {
        "vocab_size": args.vocab_size,
        "actual_vocab": tokenizer.get_vocab_size(),
        "specials": SPECIALS,
        "special_ids": {s: tokenizer.token_to_id(s) for s in SPECIALS},
        "per_source_mb": args.per_source_mb,
        "sample": stats,
        "sample_docs": sum(int(s["docs"]) for s in stats),
        "sample_bytes": sum(int(s["bytes"]) for s in stats),
        "tokenizer_sha256": tok_sha,
        "library": f"tokenizers {tokenizers_mod.__version__}",
    }
    manifest_path = out_dir / "tokenizer_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"tokenizer.json sha256 {tok_sha}", flush=True)
    print(f"manifest at {manifest_path}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--vocab-size", type=int, default=50368)
    parser.add_argument("--per-source-mb", type=int, default=64)
    return train(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
