"""Candidate parity: the bettermemory 9 store's FTS5 query against the v8
index on the public fixture.

WHY THIS EXISTS. The v9 store (`src/bettermemory/sqlite_store.py`) carries
the v8 index's FTS5 table and triggers verbatim and runs the v8 candidate
SQL verbatim, so the claim is exact: the same rows, inserted in the same
order, give the same `(id, bm25)` lists to the ranker. This harness makes
the claim a list of differing queries and records the cost of the store
beside it: the per-record write with its log row and head rewrite, the
`log verify` replay, and the file size against the corpus's own bytes.

WHAT IS FIXED. The corpus, the questions and the three probes are
`bench/parity/run.py`'s; the v8 store is built by its `build_store`, so
the rows and their order are the rank-parity fixture's exactly. The v9
store receives the same records in `Store.iter_active` order, which is
the order the v8 rebuild inserted them in, so rowids agree and bm25 ties
resolve the same way. Every query runs at the prefilter's cap.

Usage:

    .venv/bin/python bench/parity/sqlite_candidates.py \\
        --out bench/parity/results/sqlite-candidates-8.0.0-2026-09-26.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from bettermemory import index as _index  # noqa: E402
from bettermemory.sqlite_store import STORE_FILENAME, SqliteStore  # noqa: E402
from bettermemory.store import Store  # noqa: E402


def _load_parity() -> ModuleType:
    """The rank-parity runner, loaded by path under its own module name:
    several benches ship a `run.py`, and a bare import would return
    whichever of them a process loaded first."""
    name = "bench_parity_run"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, _HERE / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


parity = _load_parity()


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[position]


def _size_at_page_size(root: Path, items: list[Any], page_size: int) -> int:
    """The store's file size for the same rows at another page size."""
    import bettermemory.sqlite_store as module

    root.mkdir(parents=True, exist_ok=True)
    chosen = module.PAGE_SIZE
    module.PAGE_SIZE = page_size
    try:
        store = SqliteStore.create(root / STORE_FILENAME, keys_dir=root / "keys")
    finally:
        module.PAGE_SIZE = chosen
    try:
        for _, memory in items:
            store.put_memory(memory)
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return int(store.path.stat().st_size)
    finally:
        store.close()


def run_candidates(
    scratch: Path,
    corpus_path: Path = parity.CORPUS,
    questions_path: Path = parity.QUESTIONS,
    *,
    limit: int | None = None,
) -> dict[str, Any]:
    v8_root = scratch / "v8"
    parity.build_store(v8_root, corpus_path)
    v8 = Store(v8_root)
    items = list(v8.iter_active())

    body_bytes = sum(len(m.body.encode("utf-8")) for _, m in items)
    store = SqliteStore.create(scratch / STORE_FILENAME, keys_dir=scratch / "keys")
    put_seconds: list[float] = []
    for _, memory in items:
        started = time.perf_counter()
        store.put_memory(memory)
        put_seconds.append(time.perf_counter() - started)

    questions = parity._read_jsonl(questions_path)
    if limit is not None:
        questions = questions[:limit]
    rows: list[dict[str, Any]] = []
    differing: list[dict[str, Any]] = []
    for question in questions:
        for probe in parity.PROBES:
            query = parity.query_for(question, probe)
            expected = _index.query(v8_root, query, max_results=parity.PREFILTER_CAP)
            got = store.query_candidates(query, max_results=parity.PREFILTER_CAP)
            row = {
                "slug": question["slug"],
                "probe": probe,
                "query": query,
                "ids": [i for i, _ in got],
                "scores": [round(s, 6) for _, s in got],
                "same": got == expected,
            }
            rows.append(row)
            if not row["same"]:
                differing.append(
                    {
                        "slug": question["slug"],
                        "probe": probe,
                        "v8": [i for i, _ in expected],
                        "v9": row["ids"],
                    }
                )

    started = time.perf_counter()
    report = store.log_verify()
    verify_seconds = time.perf_counter() - started
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_bytes = store.path.stat().st_size
    page_size = int(store.conn.execute("PRAGMA page_size").fetchone()[0])
    status = store.status()
    store.close()

    # The v8 footprint for the same rows, and the store at 4 KB pages, so
    # the size beside the parity has both comparisons on record.
    v8_index_bytes = _index.index_path(v8_root).stat().st_size
    v8_files_bytes = sum(path.stat().st_size for path, _ in items)
    size_at_4k = _size_at_page_size(scratch / "p4096", items, 4096)

    digest = hashlib.sha256(
        json.dumps([[r["slug"], r["probe"], r["ids"]] for r in rows]).encode("utf-8")
    ).hexdigest()
    return {
        "kind": "candidate-parity/sqlite",
        "provenance": parity._provenance(),
        "corpus": {
            "path": str(corpus_path.relative_to(_ROOT))
            if corpus_path.is_relative_to(_ROOT)
            else str(corpus_path),
            "sha256": parity._sha256(corpus_path),
            "documents": len(items),
            "body_bytes": body_bytes,
        },
        "questions": len(questions),
        "queries": len(rows),
        "cap": parity.PREFILTER_CAP,
        "differing": differing,
        "digest": digest,
        "verify": {
            "status": report["status"],
            "rows": report["rows"],
            "seconds": round(verify_seconds, 3),
        },
        "cost": {
            "put_memory_p50_ms": round(1000 * statistics.median(put_seconds), 3),
            "put_memory_p90_ms": round(1000 * _percentile(put_seconds, 0.9), 3),
            "put_memory_max_ms": round(1000 * max(put_seconds), 3),
            "puts": len(put_seconds),
        },
        "size": {
            "sqlite_bytes": size_bytes,
            "page_size": page_size,
            "sqlite_bytes_at_4096": size_at_4k,
            "log_rows": status["log_rows"],
            "ratio_to_body_bytes": round(size_bytes / body_bytes, 3)
            if body_bytes
            else None,
            "v8_index_bytes": v8_index_bytes,
            "v8_files_bytes": v8_files_bytes,
            "v8_ratio_to_body_bytes": round(
                (v8_index_bytes + v8_files_bytes) / body_bytes, 3
            )
            if body_bytes
            else None,
        },
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as raw:
        artifact = run_candidates(Path(raw), limit=args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(
        f"{artifact['queries']} queries, {len(artifact['differing'])} differing, "
        f"digest {artifact['digest'][:16]}; verify {artifact['verify']['status']} in "
        f"{artifact['verify']['seconds']} s over {artifact['verify']['rows']} rows; "
        f"put p50 {artifact['cost']['put_memory_p50_ms']} ms, "
        f"p90 {artifact['cost']['put_memory_p90_ms']} ms; "
        f"{artifact['size']['sqlite_bytes']} bytes at {artifact['size']['page_size']} "
        f"byte pages ({artifact['size']['sqlite_bytes_at_4096']} at 4096), "
        f"{artifact['size']['ratio_to_body_bytes']}x the body bytes; v8 index "
        f"{artifact['size']['v8_index_bytes']} plus files "
        f"{artifact['size']['v8_files_bytes']}, "
        f"{artifact['size']['v8_ratio_to_body_bytes']}x\n"
    )
    return 0 if not artifact["differing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
