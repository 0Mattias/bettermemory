"""Time the reference tokenizer the way ``tests/bench_tokenize.rs`` times
the native one: `search_lexicon.tokenize` over every input of
``tests/contract/golden/lexicon.jsonl``, repeated, in one process.

``python native/tools/bench_tokenize.py [reps]`` (default 50), with the
repository's Python (it imports ``bettermemory`` from ``src``).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bettermemory import search_lexicon  # noqa: E402


def inputs() -> list[str]:
    path = ROOT / "tests" / "contract" / "golden" / "lexicon.jsonl"
    records = [json.loads(line) for line in path.read_text("ascii").splitlines()]
    out = []
    for record in records[1:]:
        if "input" in record:
            out.append(record["input"])
        else:
            out.append("".join(map(chr, record["input_cps"])))
    return out


def main(argv: list[str]) -> int:
    reps = int(argv[0]) if argv else 50
    texts = inputs()
    chars = sum(len(t) for t in texts)
    tokens = 0
    start = time.perf_counter()
    for _ in range(reps):
        for text in texts:
            tokens += len(search_lexicon.tokenize(text))
    elapsed = time.perf_counter() - start
    print(
        f"python tokenize: {len(texts)} inputs ({chars} code points) x {reps} reps "
        f"= {elapsed:.3f} s, {elapsed * 1000 / reps:.3f} ms per rep, {tokens} tokens"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
