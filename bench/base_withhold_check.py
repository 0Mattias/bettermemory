"""P64's direct measurement: tie probes are byte-identical under round 9.

Addendum 12 predicts (P64) that on every dev probe where the two base
legs tie on rank-1 evidence, the mechanism-on engine returns output
byte-identical to mechanism-off — the blast radius is exactly the
trailing-leg probes. The unit tests pin that property on toy stores;
this instrument measures it across the whole dev gold set, both corpus
regimes, and reports the partition sizes alongside, so P65's
withheld-share bounds are scored from the same artifact.

For each question x probe it runs the shipped engine twice — constant
False, then constant True — reads the evidence delta off the base
pair via the census tap, and compares full DEPTH-window outputs (ids
AND scores). A tie probe that differs anywhere is a P64 violation and
is reported with its slug.

Read-only over the repo; statistics only; no ranking change ships from
here.

    .venv/bin/python bench/base_withhold_check.py \\
        --out retrieval/results/round9-tie-identity-YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import bettermemory.search as engine  # noqa: E402
from bettermemory.store import Store  # noqa: E402

DEPTH = 50


def _bench(name: str) -> Any:
    sys.path.insert(0, str(_HERE))
    sys.path.insert(0, str(_HERE / "retrieval"))
    import importlib  # noqa: PLC0415

    return importlib.import_module(name)


def _probe_pair(
    memories: list[Any], query: str
) -> tuple[list[tuple[str, float]], list[tuple[str, float]], int]:
    """(off_hits, on_hits, evidence_delta) for one probe.

    The delta is read off the mechanism-off pass via the census tap, so
    the partition is computed by the exact quantity the engine itself
    reads at fusion time.
    """
    blc = _bench("base_leg_census")
    saved = engine._BASE_LEG_TRAILING_WITHHOLD
    try:
        engine._BASE_LEG_TRAILING_WITHHOLD = False
        with blc._fusion_tap(None) as tap:
            off = engine.search(memories, query, max_results=DEPTH)
        rankings = tap["rankings"]
        delta = 0
        if rankings is not None:
            delta = engine._leg_top_evidence(rankings[0]) - engine._leg_top_evidence(
                rankings[1]
            )
        engine._BASE_LEG_TRAILING_WITHHOLD = True
        on = engine.search(memories, query, max_results=DEPTH)
    finally:
        engine._BASE_LEG_TRAILING_WITHHOLD = saved
    return (
        [(h.id, h.score) for h in off],
        [(h.id, h.score) for h in on],
        delta,
    )


def run(pad_to: int | None) -> dict[str, Any]:
    rr = _bench("run")
    questions = rr._read_jsonl(rr.QUESTIONS)
    root = Path(tempfile.mkdtemp(prefix="bm-r9tie-"))
    ties = 0
    tie_identical = 0
    withheld = 0
    violations: list[dict[str, Any]] = []
    try:
        _slug_to_id, size = rr.build_store(root, rr.CORPUS, pad_to=pad_to)
        memories = Store(root).load_all()
        for q in questions:
            probes = (
                ("asked", q["question"]),
                ("requery", q["requery"]),
                ("control", rr.strip_question_words(q["question"])),
            )
            for probe, text in probes:
                off, on, delta = _probe_pair(memories, text)
                if delta == 0:
                    ties += 1
                    if off == on:
                        tie_identical += 1
                    else:
                        violations.append({"slug": q["slug"], "probe": probe})
                else:
                    withheld += 1
    finally:
        shutil.rmtree(root, ignore_errors=True)
    total = ties + withheld
    return {
        "collection_size": size,
        "pad_to": pad_to,
        "probes": total,
        "tie_probes": ties,
        "tie_probes_byte_identical": tie_identical,
        "p64_violations": violations,
        "withheld_probes": withheld,
        "withheld_share": round(withheld / total, 4) if total else 0.0,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Round 9 P64/P65 identity check.")
    p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Relative paths resolve against bench/, like the other runners.",
    )
    args = p.parse_args()

    rr = _bench("run")
    started = time.time()
    payload = {
        "provenance": rr._provenance(),
        "depth": DEPTH,
        "seconds": None,
        "note": (
            "P64: on tie probes the mechanism-on engine must be byte-identical "
            "to mechanism-off across the full depth window (ids and scores). "
            "P65: withheld_share must be > 0 and < 0.5 in both regimes."
        ),
        "arms": {},
    }
    for label, pad in (("unpadded", None), ("padded600", 600)):
        payload["arms"][label] = run(pad)
    payload["seconds"] = round(time.time() - started, 1)
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
