"""Do cheap store statistics distinguish the campaign's two corpora?

Round 7 located the campaign's obstacle in a single scalar: the weight
the rescue leg's floor stratum should carry. The technical corpus wants
it at full strength and the conversational one wants it damped, both
monotonically and in opposite directions, so no constant satisfies
both. The obvious answer is to make the weight a function of the store
— C1's demand since round 2.

That only works if the two stores are *distinguishable* by something
the engine can compute cheaply from their own text. This module asks
exactly that, before any adaptation rule is written: it measures a
handful of register-adjacent statistics on both corpora and reports how
far apart they land.

**Statistics only.** No ranking change, no rule, no threshold applied.
The held-out instrument under `bench/heldout/` is not read.

    .venv/bin/python bench/store_census.py --out retrieval/results/store-census-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory.search as engine  # noqa: E402
from bettermemory.models import Memory  # noqa: E402
from bettermemory.store import Store  # noqa: E402

# How many LongMemEval question-stores to average over. Each is an
# independent store of ~245 items; the statistics below are stable well
# before this many.
LME_SAMPLE = 20

# A rule keyed on a statistic has to AMPLIFY that statistic's spread
# into the weight's spread. Round 7 measured the weight needing to move
# roughly 2x between the corpora (full strength to about half), so a
# usable signal has to separate them by at least that much — otherwise
# the rule is a high-gain amplifier on a near-constant input, which is
# a fit rather than a derivation.
SEPARATION_BAR = 2.0


def store_statistics(memories: list[Memory]) -> dict[str, float]:
    """Register-adjacent statistics, all computable from the store alone."""
    streams = [engine._memory_tokens(m).content for m in memories]
    tokens = [t for s in streams for t in s]
    counts = Counter(tokens)
    filler = engine._EXPANSION_TABLES.filler_stems
    raw = [engine.tokenize(m.body) for m in memories]
    raw_total = sum(len(r) for r in raw)
    return {
        "docs": float(len(memories)),
        "mean_doc_len": statistics.fmean(len(s) for s in streams) if streams else 0.0,
        "type_token_ratio": len(counts) / max(1, len(tokens)),
        "hapax_share": sum(1 for c in counts.values() if c == 1) / max(1, len(counts)),
        "filler_tok_share": sum(1 for t in tokens if t in filler) / max(1, len(tokens)),
        "stopword_share": sum(1 for r in raw for t in r if t in engine._STOPWORDS)
        / max(1, raw_total),
    }


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def dev_statistics() -> dict[str, float]:
    rr = _load("store_census_ret", _HERE / "retrieval" / "run.py")
    root = Path(tempfile.mkdtemp(prefix="bm-storecensus-"))
    try:
        rr.build_store(root, rr.CORPUS, pad_to=None)
        return store_statistics(Store(root).load_all())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def longmemeval_statistics(sample: int = LME_SAMPLE) -> dict[str, float]:
    lr = _load("store_census_lme", _HERE / "longmemeval" / "run.py")
    corpus = json.loads(lr.DEFAULT_CORPUS.read_text(encoding="utf-8"))
    rows: list[dict[str, float]] = []
    for inst in corpus:
        if len(rows) >= sample:
            break
        if not inst["answer_session_ids"]:
            continue
        root = Path(tempfile.mkdtemp(prefix="bm-storecensus-"))
        try:
            lr.build_question_store(root, inst)
            rows.append(store_statistics(Store(root).load_all()))
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return {k: statistics.fmean(r[k] for r in rows) for k in rows[0]}


def compare(dev: dict[str, float], other: dict[str, float]) -> dict[str, Any]:
    """Ratio per statistic, and whether any clears the separation bar."""
    out: dict[str, Any] = {}
    for key, dev_value in dev.items():
        ratio = other[key] / dev_value if dev_value else float("inf")
        out[key] = {
            "dev": round(dev_value, 6),
            "longmemeval": round(other[key], 6),
            "ratio": round(ratio, 4),
            "separates": ratio >= SEPARATION_BAR or ratio <= 1.0 / SEPARATION_BAR,
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Store-statistic census. No ranking change."
    )
    p.add_argument("--sample", type=int, default=LME_SAMPLE)
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Relative paths resolve against bench/, like the other runners.",
    )
    args = p.parse_args()

    started = time.time()
    dev = dev_statistics()
    lme = longmemeval_statistics(args.sample)
    comparison = compare(dev, lme)
    payload = {
        "separation_bar": SEPARATION_BAR,
        "lme_sample": args.sample,
        "seconds": round(time.time() - started, 1),
        "note": (
            "STATISTICS ONLY — register-adjacent store statistics on both dev "
            "corpora. No ranking change, no adaptation rule, no threshold "
            "applied. The held-out instrument is not read."
        ),
        "any_statistic_separates": any(v["separates"] for v in comparison.values()),
        "comparison": comparison,
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        out = Path(args.out).expanduser()
        if not out.is_absolute():
            out = (_HERE / out).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
