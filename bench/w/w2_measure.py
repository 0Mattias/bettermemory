"""Run an instrument with the W2 reranker over the engine's candidate window.

The harness `bench/w/W2_DECLARATION.md` §4 constrains: no `src/`
change, the instruments' committed entry points driven as they stand,
the rerank stage applied bench-side. The seam is the same one
`w1_measure.py` established — the runner modules bind
``run_search = bettermemory.search.search`` at import, so loading the
runner and rebinding that name swaps the ranking call and nothing
else. The wrapper asks the real engine for a deeper list (the window,
or the runner's own depth when it is already deeper), reranks the
first ``--window`` candidates by the declared blend, leaves the tail
untouched, and returns the length the runner asked for.

The blend, verbatim from the declaration: within the window, min–max
normalize the engine score and the encoder cosine; final =
(1−λ)·engine + λ·encoder; ties break by the engine's existing order;
the ordering below the window is untouched. λ=0 must reproduce the
engine ordering — the wrapper asserts the blend's λ=0 ordering per
question in every run, and under ``--arm off`` no wrapper is
installed at all (the paired integrity read).

Memory vectors are encoded once per distinct body (sha-keyed cache)
from the memory's stored text at the sequence cap; query vectors once
per distinct query. Every artifact records the weights sha256 and the
vector-cache sha256 it ranked with.
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

import argparse  # noqa: E402  (env pins must precede numpy)
import contextlib  # noqa: E402  (env pins must precede numpy)
import hashlib  # noqa: E402  (env pins must precede numpy)
import importlib.util  # noqa: E402  (env pins must precede numpy)
import io  # noqa: E402  (env pins must precede numpy)
import json  # noqa: E402  (env pins must precede numpy)
import sys  # noqa: E402  (env pins must precede numpy)
from pathlib import Path  # noqa: E402  (env pins must precede numpy)
from types import ModuleType  # noqa: E402  (env pins must precede numpy)
from typing import Any  # noqa: E402  (env pins must precede numpy)

import numpy as np  # noqa: E402  (env pins must precede numpy)

_W_DIR = Path(__file__).parent
_ROOT = _W_DIR.parent.parent

sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_W_DIR))

from bettermemory.search import search as _real_search  # noqa: E402

from w2_train import W2Encoder  # noqa: E402


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _minmax(x: np.ndarray) -> np.ndarray:
    lo, hi = float(x.min()), float(x.max())
    if hi <= lo:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)


def blend_order(
    engine_scores: np.ndarray, cosines: np.ndarray, lam: float
) -> list[int]:
    """The declaration's blend, as an ordering of window indices.

    Min–max normalize both signals over the window; final =
    (1−λ)·engine + λ·encoder; descending; ties break by the engine's
    existing order (the original index). With the engine scores already
    in engine order, λ=0 returns the identity — the property every run
    asserts per question and the CI leg pins on synthetic windows.
    """
    blend = (1.0 - lam) * _minmax(engine_scores) + lam * _minmax(cosines)
    return sorted(range(blend.size), key=lambda i: (-blend[i], i))


class Reranker:
    """The blend over the engine's head window, with per-call accounting."""

    def __init__(self, encoder: W2Encoder, lam: float, window: int) -> None:
        self.encoder = encoder
        self.lam = float(lam)
        self.window = window
        self.query_vecs: dict[str, np.ndarray] = {}
        self.body_vecs: dict[str, np.ndarray] = {}
        self.body_order: list[str] = []
        self.calls = 0
        self.identity_checks = 0

    def _query_vec(self, query: str) -> np.ndarray:
        vec = self.query_vecs.get(query)
        if vec is None:
            vec = self.encoder.encode_texts([query])[0]
            self.query_vecs[query] = vec
        return vec

    def _body_vec(self, body: str) -> np.ndarray:
        key = hashlib.sha256(body.encode("utf-8")).hexdigest()
        vec = self.body_vecs.get(key)
        if vec is None:
            vec = self.encoder.encode_texts([body])[0]
            self.body_vecs[key] = vec
            self.body_order.append(key)
        return vec

    def rerank(self, hits: list[Any], memories: list[Any], query: str) -> list[Any]:
        self.calls += 1
        head = hits[: self.window]
        if len(head) < 2:
            return hits
        by_id = {m.id: m for m in memories}
        engine_scores = np.array([float(h.score) for h in head], dtype=np.float64)
        qv = self._query_vec(query)
        cosines = np.array(
            [float(self._body_vec(by_id[h.id].body) @ qv) for h in head],
            dtype=np.float64,
        )
        identity = blend_order(engine_scores, cosines, 0.0)
        if identity != list(range(len(head))):
            raise AssertionError(
                f"lambda=0 blend broke the engine order on query {query!r}"
            )
        self.identity_checks += 1
        order = blend_order(engine_scores, cosines, self.lam)
        return [head[i] for i in order] + hits[self.window :]

    def cache_sha256(self) -> str:
        digest = hashlib.sha256()
        for key in sorted(self.body_vecs):
            digest.update(key.encode())
            digest.update(self.body_vecs[key].tobytes())
        return digest.hexdigest()


def make_wrapper(reranker: Reranker, *, shallow_check: bool) -> Any:
    """The runner-facing ``run_search`` replacement."""

    def wrapped(memories: list[Any], query: str, **kwargs: Any) -> list[Any]:
        k_req = int(kwargs.pop("max_results", 5))
        deep = max(reranker.window, k_req)
        hits = _real_search(memories, query, max_results=deep, **kwargs)
        if shallow_check and deep > k_req:
            shallow = _real_search(memories, query, max_results=k_req, **kwargs)
            if [h.id for h in shallow] != [h.id for h in hits[:k_req]]:
                raise AssertionError(
                    f"deep ranking perturbed the head on query {query!r}"
                )
        return reranker.rerank(hits, memories, query)[:k_req]

    return wrapped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an instrument with the W2 reranker over the engine window."
    )
    parser.add_argument("--run", required=True, help="the trainer --out directory")
    parser.add_argument("--arm", required=True, choices=("rerank", "pure", "off"))
    parser.add_argument(
        "--lam",
        type=float,
        default=0.5,
        help="blend weight; the pure arm forces 1.0, off installs no wrapper",
    )
    parser.add_argument("--window", type=int, default=50)
    parser.add_argument("--instrument", default="dev", choices=("dev", "longmemeval"))
    parser.add_argument("--out", required=True, help="wrapped-artifact JSON path")
    parser.add_argument(
        "--runner-args",
        default="",
        help="extra args passed through to the instrument runner",
    )
    parser.add_argument(
        "--shallow-check",
        action="store_true",
        help="also assert the deep call left the head unperturbed (lambda=0 reads)",
    )
    args = parser.parse_args()

    reranker: Reranker | None = None
    weights_sha = None
    if args.arm != "off":
        run_dir = Path(args.run)
        meta = json.loads((run_dir / "meta.json").read_text())
        weights_sha = meta["sha256"]["weights.npy"]
        lam = 1.0 if args.arm == "pure" else args.lam
        reranker = Reranker(W2Encoder.load(run_dir), lam, args.window)

    if args.instrument == "dev":
        runner_path = _ROOT / "bench" / "retrieval" / "run.py"
    else:
        runner_path = _ROOT / "bench" / "longmemeval" / "run.py"

    argv = [str(runner_path), "--json"]
    if args.runner_args:
        argv.extend(args.runner_args.split())

    captured = io.StringIO()
    old_argv = sys.argv
    sys.argv = argv
    try:
        runner = _load_module("w2_instrument_runner", runner_path)
        if reranker is not None:
            setattr(  # noqa: B010  (module attribute poke, the harness seam)
                runner,
                "run_search",
                make_wrapper(reranker, shallow_check=args.shallow_check),
            )
        with contextlib.redirect_stdout(captured):
            code = runner.main()
    finally:
        sys.argv = old_argv
    if code != 0:
        print(captured.getvalue(), file=sys.stderr)
        return code

    wrapped: dict[str, Any] = {
        "w2": {
            "arm": args.arm,
            "lambda": (None if reranker is None else reranker.lam),
            "window": args.window,
            "run": args.run if reranker is not None else None,
            "weights_sha256": weights_sha,
            "vector_cache_sha256": (
                None if reranker is None else reranker.cache_sha256()
            ),
            "distinct_bodies_encoded": (
                None if reranker is None else len(reranker.body_vecs)
            ),
            "search_calls": (None if reranker is None else reranker.calls),
            "identity_checks": (None if reranker is None else reranker.identity_checks),
            "instrument": args.instrument,
            "runner_args": args.runner_args,
        },
        "runner": json.loads(captured.getvalue()),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(wrapped, indent=1, sort_keys=True) + "\n")
    summary: dict[str, Any] = {"arm": args.arm, "out": str(out)}
    results = wrapped["runner"].get("results")
    if isinstance(results, list):
        for row in results:
            if isinstance(row, dict) and "recall_at_1" in row:
                key = f"{row.get('arm')}/{row.get('probe')}/pf={row.get('prefilter')}"
                summary[key] = f"r@1={row['recall_at_1']} r@5={row['recall_at_5']}"
            elif isinstance(row, dict) and "macro" in row:
                summary[f"{row.get('arm')}/macro"] = row["macro"]
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
