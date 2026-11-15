"""Time the reference ranker the way ``tests/bench_search.rs`` times the
native one: `search.search` over the corpus of
``tests/contract/golden/search_ranker.jsonl``, every query of the file in
each of its core modes (keyword, bm25, hybrid, hybrid with rescue
expansion) at its ``now`` and ``max_results``, repeated, in one process.

The records are parsed into `Memory` objects before the clock starts, and
one untimed pass fills the reference's token cache, as a running server's
is (the native corpus is likewise prepared once). Each call takes the
query's text and returns the ranked hits (``path_drift=False``; the
reference's hits carry their snippet, the native ones do not).

``python native/tools/bench_search.py [reps]`` (default 5), with the
repository's Python (it imports ``bettermemory`` from ``src``).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bettermemory import search  # noqa: E402
from bettermemory.models import Memory  # noqa: E402


def text(record: dict[str, Any], name: str) -> str:
    if name in record:
        return str(record[name])
    return "".join(map(chr, record[f"{name}_cps"]))


def main(argv: list[str]) -> int:
    reps = int(argv[0]) if argv else 5
    path = ROOT / "tests" / "contract" / "golden" / "search_ranker.jsonl"
    lines = [json.loads(line) for line in path.read_text("ascii").splitlines()]
    header = lines[0]
    memories = [
        Memory(
            id=m["id"],
            created=datetime.fromisoformat(m["created"]),
            updated=datetime.fromisoformat(m["updated"]),
            scopes=m["scopes"],
            confidence=m["confidence"],
            source=m["source"],
            body=text(m, "body"),
        )
        for m in header["memories"]
    ]
    now = datetime.fromisoformat(header["now"])
    queries = [text(r, "query") for r in lines[1:]]
    modes = [header["modes"][name] for name in header["core_modes"]]

    def one_pass() -> int:
        hits = 0
        for query in queries:
            for options in modes:
                hits += len(
                    search.search(
                        memories,
                        query,
                        now=now,
                        max_results=header["max_results"],
                        path_drift=False,
                        **options,
                    )
                )
        return hits

    one_pass()
    hits = 0
    start = time.perf_counter()
    for _ in range(reps):
        hits += one_pass()
    elapsed = time.perf_counter() - start
    print(
        f"python search: {len(memories)} records, {len(queries)} queries x "
        f"{len(modes)} modes x {reps} reps = {elapsed:.3f} s, "
        f"{elapsed * 1000 / reps:.1f} ms per pass, {hits} hits"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
