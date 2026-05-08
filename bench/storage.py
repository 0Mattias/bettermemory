"""Storage benchmark for bettermemory.

Measures latency of the operations a busy store hits often:

- `Store.write` — per-memory insert cost (one file write, one fsync,
  one rename)
- `Store.load_all` — full corpus scan, the read-side hot path that
  every retrieval call goes through
- `search(...)` — keyword scoring over the loaded corpus for a
  representative query

Usage:

    venv/bin/python bench/storage.py                 # default sizes
    venv/bin/python bench/storage.py --sizes 1000,10000,50000
    venv/bin/python bench/storage.py --json          # machine-readable

The script generates synthetic memories into a fresh tmp directory,
times each operation, and prints a small results table. Numbers are
specific to whatever hardware you run on; the value is the *shape* of
the curve (does load_all stay roughly linear, does search latency
stay below your acceptable ceiling) rather than absolute values.

Disposable: the bench dir is created under /tmp by default and
removed on exit unless you pass `--keep`. We deliberately avoid
running this against the user's real `~/.claude-memory/` even with
a flag — corpus sizes above a few thousand would stomp over weeks
of legitimate writes if pointed at the wrong directory.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Add `src/` to sys.path so this script is runnable without an install.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


from bettermemory.config import BehaviorConfig  # noqa: E402
from bettermemory.search import search as run_search  # noqa: E402
from bettermemory.store import Store  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic-corpus generation
# ---------------------------------------------------------------------------


_VOCAB = (
    "homelab tailscale postgres deployment caddy ingress kubernetes "
    "etcd raft consensus storage nfs zfs backup restic snapshot "
    "monitoring grafana prometheus node-exporter scraping loki "
    "container docker compose podman runtime systemd journal "
    "tmux fish zsh prompt vim neovim treesitter lsp coq rust "
    "python typescript javascript pydantic dataclass enum union "
    "mcp claude opus sonnet haiku context window tool-use "
    "memory addendum scope hygiene rare typo project work"
).split()


def _synthetic_body(seed: int) -> str:
    """Generate a single durable-shape body — a couple of sentences,
    enough vocabulary to give search something to chew on, no
    transient markers (so the durability gate doesn't trip)."""
    rng = random.Random(seed)
    n_sentences = rng.randint(2, 4)
    sentences = []
    for _ in range(n_sentences):
        n_words = rng.randint(8, 16)
        words = rng.sample(_VOCAB, min(n_words, len(_VOCAB)))
        sentences.append(" ".join(words).capitalize() + ".")
    return " ".join(sentences) + "\n"


def _populate(store: Store, n: int) -> None:
    """Write n memories into the store. No progress bar — we want clean
    timing numbers from the bench section, not noise from stderr writes."""
    rng = random.Random(0xCAFE)
    scopes_pool = ["tools", "infrastructure", "homelab", "projects:foo", "projects:bar"]
    for i in range(n):
        scope = rng.choice(scopes_pool)
        store.write(content=_synthetic_body(i), scopes=[scope])


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------


def _time(fn: Any, repeats: int) -> dict[str, float]:
    """Run fn `repeats` times, return min/median/p95/max in ms."""
    samples_ms: list[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - t0) * 1000
        samples_ms.append(elapsed)
    samples_ms.sort()
    p95_idx = max(0, int(len(samples_ms) * 0.95) - 1)
    return {
        "min_ms": samples_ms[0],
        "median_ms": statistics.median(samples_ms),
        "p95_ms": samples_ms[p95_idx],
        "max_ms": samples_ms[-1],
        "samples": len(samples_ms),
    }


# ---------------------------------------------------------------------------
# Bench runner
# ---------------------------------------------------------------------------


def _run_size(n: int, *, search_repeats: int, load_repeats: int) -> dict[str, Any]:
    """Bench one corpus size. Returns a dict with the timings + corpus
    metadata. Creates and tears down its own tmp directory."""
    bench_dir = Path(tempfile.mkdtemp(prefix=f"bm-bench-{n}-"))
    try:
        store = Store(bench_dir)

        # Populate — separately timed so we can report write throughput too.
        t0 = time.perf_counter()
        _populate(store, n)
        populate_s = time.perf_counter() - t0
        write_per_sec = n / populate_s if populate_s > 0 else float("inf")

        # load_all: full corpus scan. The hot path for every retrieval.
        load_all_stats = _time(lambda: store.load_all(), load_repeats)

        # search: load once, run search many times against the same
        # in-memory list. This is the realistic per-request profile —
        # memory_search calls load_all once and then run_search.
        memories = store.load_all()
        search_stats = _time(
            lambda: run_search(
                memories,
                query="postgres backup",
                scopes=None,
                excluded_scopes=set(),
                repo_filter=None,
                max_results=5,
                half_life_days=BehaviorConfig().recency_boost_half_life_days,
            ),
            search_repeats,
        )

        # Disk stats: total bytes, file count.
        files = list(bench_dir.glob("*.md"))
        total_bytes = sum(f.stat().st_size for f in files)
        return {
            "n": n,
            "populate_seconds": populate_s,
            "writes_per_second": write_per_sec,
            "files_on_disk": len(files),
            "disk_bytes": total_bytes,
            "load_all_ms": load_all_stats,
            "search_ms": search_stats,
        }
    finally:
        shutil.rmtree(bench_dir, ignore_errors=True)


def _format_text(results: list[dict[str, Any]]) -> str:
    rows = [
        "| n      | files | disk MB | write/s | load_all median | search median | search p95 |",
        "|--------|-------|---------|---------|-----------------|---------------|------------|",
    ]
    for r in results:
        rows.append(
            f"| {r['n']:>6} | {r['files_on_disk']:>5} "
            f"| {r['disk_bytes'] / 1e6:>7.2f} "
            f"| {r['writes_per_second']:>7.1f} "
            f"| {r['load_all_ms']['median_ms']:>13.1f} ms "
            f"| {r['search_ms']['median_ms']:>11.2f} ms "
            f"| {r['search_ms']['p95_ms']:>8.2f} ms |"
        )
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bench storage operations across corpus sizes. Prints a "
            "results table; numbers are hardware-specific."
        ),
    )
    parser.add_argument(
        "--sizes",
        type=str,
        default="1000,10000,50000",
        help=(
            "Comma-separated corpus sizes to bench. Default "
            "`1000,10000,50000`. 100000 takes ~5min on a laptop."
        ),
    )
    parser.add_argument(
        "--search-repeats",
        type=int,
        default=20,
        help="Search-call repetitions per size. Default 20.",
    )
    parser.add_argument(
        "--load-repeats",
        type=int,
        default=5,
        help="load_all repetitions per size. Default 5.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a markdown table."
    )
    args = parser.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    results = []
    for n in sizes:
        print(f"running n={n}…", file=sys.stderr, flush=True)
        results.append(
            _run_size(
                n,
                search_repeats=args.search_repeats,
                load_repeats=args.load_repeats,
            )
        )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(_format_text(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
