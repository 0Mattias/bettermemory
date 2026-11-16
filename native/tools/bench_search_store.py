"""Time the reference's pool-and-rank the way ``golden_search_store.rs``'s
``bench_pool_and_rank`` times the native one: the store and callers of
``tests/contract/golden/search_store.jsonl`` (built as `gen_store_path`
builds them), every query of the fixture in its three modes, for the
offline run and each caller's admission on each surface (567 calls a
pass), repeated, in one process.

Each call is `resolve_search_pool` then `search.search` over the pool with
its statistics, as `gen_store_path._run` makes it, with no hydration and
no path drift (the native port builds neither; the reference's hits still
carry their snippet). The store and the callers are made before the clock
starts, and one untimed pass fills the reference's caches (the token
cache and the document frequency memo), as a running server's are.

``python native/tools/bench_search_store.py [reps]`` (default 20), with the
repository's Python, from the repository root.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bettermemory import location  # noqa: E402
from bettermemory import search as bsearch  # noqa: E402
from bettermemory.admission import Admission  # noqa: E402
from bettermemory.config import BehaviorConfig, Config, StorageConfig  # noqa: E402
from bettermemory.handlers.search import resolve_search_pool  # noqa: E402
from bettermemory.store import Store  # noqa: E402
from tests.contract import gen_store_path as g  # noqa: E402
from tests.contract.gen_store import _NOW, _QUERIES, _memories  # noqa: E402


def main(argv: list[str]) -> int:
    reps = int(argv[0]) if argv else 20
    with tempfile.TemporaryDirectory() as raw, g._neutral_git(Path(raw)):
        tmp = Path(raw)
        g._repo(tmp / "alpha", "https://example.invalid/fixture/alpha.git", 1)
        g._repo(tmp / "beta", "https://example.invalid/fixture/beta.git", 2)
        (tmp / "plain").mkdir()
        store = Store.create(tmp / "store", keys_dir=tmp / "keys")
        try:
            cfg = Config(
                storage=StorageConfig(directory=str(store.path.parent)),
                behavior=BehaviorConfig(),
            )
            callers = {
                name: location.resolve_caller(store, cfg, str(tmp / name), session=None)
                for name in g._CALLERS
            }
            for i, memory in enumerate(_memories()):
                home = g._HOMES[i % len(g._HOMES)]
                store.put_memory(
                    memory, home=None if home is None else callers[home].home()
                )
            admissions: list[Admission | None] = [None] + [
                Admission.of(surface, c)
                for c in callers.values()
                for surface in g._SURFACES.values()
            ]
            calls = [
                (query, mode, admission)
                for query in _QUERIES
                for mode in g._MODES
                for admission in admissions
            ]

            def one_pass() -> int:
                hits = 0
                for query, mode, admission in calls:
                    pool = resolve_search_pool(store, query, admission=admission)
                    hits += len(
                        bsearch.search(
                            pool.memories,
                            query,
                            admission=admission,
                            max_results=10,
                            now=_NOW,
                            half_life_days=cfg.behavior.recency_boost_half_life_days,
                            mode=mode,  # type: ignore[arg-type]
                            corpus_stats_provider=pool.corpus_stats_provider,
                            rescue_expansion=False,
                            conversational=cfg.behavior.conversational,
                            path_drift=False,
                        )
                    )
                return hits

            one_pass()
            passes = []
            hits = 0
            for _ in range(reps):
                start = time.perf_counter()
                hits += one_pass()
                passes.append((time.perf_counter() - start) * 1000)
        finally:
            store.close()
    print(
        f"reference pool-and-rank: {len(calls)} calls a pass, {reps} passes, "
        f"median {statistics.median(passes):.2f} ms a pass "
        f"(min {min(passes):.2f}, max {max(passes):.2f}), {hits} hits"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
