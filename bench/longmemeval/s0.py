"""Answer-turn coverage of served contexts on the LongMemEval-S dev 150.

UNIT S0 (declared 2026-09-27, memory 01M3HWFR3S9XNF6W4QYZ9Z0NY6), step 0
of the retrieval work that followed L1. At the turn level L1's misses are
retrieval misses (memory 01M3HV4YS425EP2AJR5BV9J1RS): the reader got 427
of 432 right when every answer turn reached it and 19 of 47 when one did
not. This harness counts, with no reader, no judge and no model call, how
many dev questions have every answer turn served under each of the bench
service's existing serving options, and where the missing ones sit.

COVERED means every turn LongMemEval-S marks has_answer sits in a served
round and its text is verbatim in that round's served content, so a
trimmed answer counts as missing. A round is found by its position: the
service writes one Add per session in haystack order and a session's
rounds in order, so the n-th round of a question's store is round j of
session k (`round_positions`, checked against the text of every hit).

DEV ONLY: the 150 of aml_run.split, L1's dev. The holdout is never served.

THE ARMS are the service's own options at L1's 90,000-character budget:
order rank, session or chronological; a candidate pool of 100 or 200
rounds; fill none or neighbors; trim none or tail. C0, L1's configuration,
is rank, 100, none, none. The best arm is the one covering the most
questions, ties to fewer mean served characters. The budget curve serves
the engine's own ranked list (rank order, every hit, no fill) and the best
arm at seven budgets, beside the whole haystack, the no-memory control.
Every missing answer round under C0 is classed: (a) in the engine's ranked
list but cut by the budget, (b) not in the list while another round of its
session is, (c) its session absent from the list.

The store is the sweep's own (bench/aml/.stores/coverage-s0-dev); L1's
store and receipts are only read, and their file listing is compared
before and after. BETTERMEMORY_KEYS_DIR must be set, since each question's
store writes a key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
for _path in (_BENCH, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import l1  # noqa: E402
from aml import run as aml_run  # noqa: E402
from aml.service import (  # noqa: E402
    FILLS,
    ORDERS,
    SERVING_BUDGET_CHARS,
    TRIMS,
    MemoryService,
    _text,
    round_spans,
    rounds_of,
)

from bettermemory.store import Store  # noqa: E402

STORE = aml_run.STORES / "coverage-s0-dev"
L1_STORE = l1.STORE
L1_RECEIPTS = l1.RECEIPTS / "l1-2026-09-27"
L1_PROMPTS = L1_RECEIPTS / "work" / "prompts"
L1_ARTIFACT = l1.RESULTS / "l1-official-s-2026-09-27.json"
OUT = l1.RESULTS / "coverage-s0-dev-2026-09-27.json"

BUDGET = SERVING_BUDGET_CHARS
CURVE_BUDGETS = (45_000, 90_000, 135_000, 180_000, 270_000, 360_000, 540_000)


@dataclass(frozen=True)
class Arm:
    """One serving configuration of bench/aml/service.py's MemoryService.
    `top_k` is both the pool asked of the engine and the service's `serve`
    cap, as C0 has them."""

    order: str
    top_k: int
    fill: str
    trim: str

    def __post_init__(self) -> None:
        if self.order not in ORDERS:
            raise ValueError(f"order {self.order!r}")
        if self.fill not in FILLS:
            raise ValueError(f"fill {self.fill!r}")
        if self.trim not in TRIMS:
            raise ValueError(f"trim {self.trim!r}")
        if self.top_k < 1:
            raise ValueError(f"top_k {self.top_k!r}")

    @property
    def name(self) -> str:
        return f"{self.order}-k{self.top_k}-fill_{self.fill}-trim_{self.trim}"


C0 = Arm(order="rank", top_k=100, fill="none", trim="none")
ARMS = tuple(
    Arm(order=o, top_k=k, fill=f, trim=t)
    for o in ("rank", "session", "chronological")
    for k in (100, 200)
    for f in ("none", "neighbors")
    for t in ("none", "tail")
)


def configure(service: MemoryService, arm: Arm, budget: int) -> None:
    """Point a C0 service at another arm. The options are read at search
    time, so one service serves every arm of a question."""
    service.order = arm.order
    service.fill = arm.fill
    service.trim = arm.trim
    service.serve = arm.top_k
    service.budget = budget


def user_id(inst: dict[str, Any]) -> str:
    return str(aml_run.adds_for(inst)[0])


def answer_turns(inst: dict[str, Any]) -> list[tuple[int, int, int]]:
    """(session index, round index, message index) of every has_answer
    turn, the round found with the service's own pairing."""
    out: list[tuple[int, int, int]] = []
    for k, session in enumerate(inst["haystack_sessions"]):
        spans = round_spans(session)
        for i, turn in enumerate(session):
            if turn.get("has_answer"):
                j = next(n for n, (a, b) in enumerate(spans) if a <= i < b)
                out.append((k, j, i))
    return out


def round_text(inst: dict[str, Any], k: int, j: int) -> str:
    """Round j of session k as the service writes its body, without the
    date header."""
    return str(rounds_of(inst["haystack_sessions"][k])[j][0])


def round_positions(
    service: MemoryService, inst: dict[str, Any]
) -> dict[str, tuple[int, int]]:
    """Every round id in the question's store mapped to (session, round).
    Refuses a store whose rounds do not line up with the haystack."""
    us = service._user(user_id(inst))
    counts = [len(round_spans(s)) for s in inst["haystack_sessions"]]
    by_seq = sorted(us.seq, key=us.seq.__getitem__)
    if us.hidden or len(by_seq) != sum(counts):
        raise SystemExit(
            f"{inst['question_id']}: the store holds {len(by_seq)} rounds "
            f"({len(us.hidden)} hidden), the haystack {sum(counts)} rounds"
        )
    out: dict[str, tuple[int, int]] = {}
    n = 0
    for k, count in enumerate(counts):
        sid = inst["haystack_session_ids"][k]
        for j in range(count):
            mid = by_seq[n]
            if us.session_of.get(mid) != sid:
                raise SystemExit(
                    f"{inst['question_id']}: round {n} belongs to "
                    f"{us.session_of.get(mid)!r}, not session {k} {sid!r}"
                )
            out[mid] = (k, j)
            n += 1
    return out


def coverage(
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    positions: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """Whether every answer turn is served whole. A question with no
    answer turns is counted apart, never as covered."""
    turns = answer_turns(inst)
    served: dict[tuple[int, int], str] = {}
    for h in hits:
        pos = positions.get(h["id"])
        if pos is not None:
            served[pos] = h["content"]
    missing: list[tuple[int, int, int]] = []
    for k, j, i in turns:
        content = served.get((k, j))
        # the store keeps a body stripped, so a turn that ends its round
        # loses its trailing whitespace; the text itself is kept verbatim
        text = _text(inst["haystack_sessions"][k][i].get("content")).strip()
        if content is None or text not in content:
            missing.append((k, j, i))
    return {
        "answer_turns": len(turns),
        "missing": missing,
        "covered": bool(turns) and not missing,
    }


def classify(
    missing: list[tuple[int, int, int]], ranked: set[tuple[int, int]]
) -> list[str]:
    """(a) the round is in the engine's ranked list, so a budget or pool
    cut it; (b) it is not, while another round of its session is; (c) no
    round of its session is in the list."""
    sessions = {k for k, _ in ranked}
    return [
        "a" if (k, j) in ranked else "b" if k in sessions else "c"
        for k, j, _ in missing
    ]


def best_arm(stats: dict[str, dict[str, Any]]) -> str:
    """The declared rule: most questions covered, ties to fewer mean served
    characters, then to the name, so the pick never depends on order."""
    return min(stats, key=lambda n: (-stats[n]["covered"], stats[n]["mean_chars"], n))


def dev_instances(corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dev, _ = aml_run.split(corpus)
    keep = set(dev)
    return [inst for inst in corpus if inst["question_id"] in keep]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _served(
    service: MemoryService, inst: dict[str, Any], arm: Arm, budget: int
) -> list[dict[str, Any]]:
    configure(service, arm, budget)
    hits: list[dict[str, Any]] = service.search(
        user_id(inst), inst["question"], arm.top_k
    )
    return hits


def _cell(
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    positions: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    got = coverage(inst, hits, positions)
    return {
        "covered": got["covered"],
        "missing": got["missing"],
        "chars": len(l1.served_context(hits)),
        "rounds": len(hits),
    }


def measure(root: Path, inst: dict[str, Any]) -> dict[str, Any]:
    """Every arm, the engine's ranked list at every budget, the whole
    haystack and C0's decomposition, for one dev question. Its own service
    instance, so questions can run in parallel."""
    service = l1.service_for(root)
    positions = round_positions(service, inst)
    everything = [
        {"id": m.id, "content": m.body}
        for m in Store(service._user(user_id(inst)).root).load_all()
    ]
    n_rounds = len(positions)
    engine = Arm(order="rank", top_k=n_rounds, fill="none", trim="none")
    c0_hits = _served(service, inst, C0, BUDGET)
    mapping_errors = [
        h["id"]
        for h in c0_hits
        if not h["content"]
        .strip()
        .endswith(round_text(inst, *positions[h["id"]]).strip())
    ]
    arms = {
        arm.name: _cell(inst, _served(service, inst, arm, BUDGET), positions)
        for arm in ARMS
    }
    ranked_hits = _served(service, inst, engine, 0)
    ranked = {positions[h["id"]] for h in ranked_hits}
    curve = {
        str(b): _cell(inst, _served(service, inst, engine, b), positions)
        for b in CURVE_BUDGETS
    }
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "answer_turns": len(answer_turns(inst)),
        "rounds": n_rounds,
        "engine_hits": len(ranked_hits),
        "c0_context_sha256": _sha(l1.served_context(c0_hits)),
        "mapping_errors": mapping_errors,
        "whole_haystack": _cell(inst, everything, positions),
        "arms": arms,
        "engine_curve": curve,
        "c0_missing_classes": classify(arms[C0.name]["missing"], ranked),
    }


def best_curve(root: Path, inst: dict[str, Any], arm: Arm) -> dict[str, Any]:
    service = l1.service_for(root)
    positions = round_positions(service, inst)
    return {
        str(b): _cell(inst, _served(service, inst, arm, b), positions)
        for b in CURVE_BUDGETS
    }


def listing(root: Path) -> str:
    """A digest of every file's path, size and modification time under
    `root`: cheap enough for L1's 1.4 GB store, and any write moves it."""
    h = hashlib.sha256()
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(
                    f"{p.relative_to(root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode()
                )
    return h.hexdigest()


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def summarize_cells(
    rows: list[dict[str, Any]], pick: Any, c0_covered: set[str], l1_dev_misses: set[str]
) -> dict[str, Any]:
    scored = [r for r in rows if r["answer_turns"]]
    cells = {r["question_id"]: pick(r) for r in scored}
    covered = {q for q, c in cells.items() if c["covered"]}
    chars = [c["chars"] for c in cells.values()]
    return {
        "n": len(scored),
        "covered": len(covered),
        "rate": _rate(len(covered), len(scored)),
        "mean_chars": round(statistics.fmean(chars), 1) if chars else 0.0,
        "p50_chars": statistics.median(chars) if chars else 0,
        "recovered_vs_c0": sorted(covered - c0_covered),
        "regressed_vs_c0": sorted(c0_covered - covered),
        "l1_dev_misses_covered": sorted(covered & l1_dev_misses),
    }


def cmd_run(args: argparse.Namespace) -> None:
    if not os.environ.get("BETTERMEMORY_KEYS_DIR"):
        raise SystemExit(
            "set BETTERMEMORY_KEYS_DIR: each question's store writes a key, "
            "and they belong under the unit's receipts"
        )
    root = Path(args.store)
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty; S0 builds its own store fresh")
    l1_before = {"store": listing(L1_STORE), "receipts": listing(L1_RECEIPTS)}
    corpus = l1._corpus()
    dev = dev_instances(corpus)
    artifact = json.loads(Path(args.l1_artifact).read_text(encoding="utf-8"))
    l1_rows = {r["question_id"]: r for r in artifact["rows"]}
    l1_dev_misses = {
        q
        for q, r in l1_rows.items()
        if r["split"] == "dev" and not r["verdicts"][l1.STANDARD_JUDGE]
    }
    if {i["question_id"] for i in dev} != {
        q for q, r in l1_rows.items() if r["split"] == "dev"
    }:
        raise SystemExit("the dev split differs from L1's")

    t0 = time.time()
    service = l1.service_for(root)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda inst: aml_run.ingest_and_search(service, inst), dev))
    ingest_seconds = round(time.time() - t0, 1)

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda inst: measure(root, inst), dev))
    measure_seconds = round(time.time() - t1, 1)

    parity: list[str] = []
    for r in rows:
        prompt_file = Path(args.l1_prompts) / f"{r['question_id']}.txt"
        prompt = prompt_file.read_bytes().decode("utf-8")
        if _sha(prompt) != l1_rows[r["question_id"]]["prompt_sha256"]:
            raise SystemExit(f"{prompt_file} is not the prompt L1 read")
        if _sha(l1.reading_context(prompt)) == r["c0_context_sha256"]:
            parity.append(r["question_id"])
    mapping_errors = sum(len(r["mapping_errors"]) for r in rows)
    if len(parity) != len(rows) or mapping_errors:
        _write(
            Path(args.out),
            {
                "unit": "S0",
                "stopped": True,
                "parity": len(parity),
                "mapping_errors": mapping_errors,
                "rows": rows,
            },
        )
        raise SystemExit(
            f"stopped: parity {len(parity)} of {len(rows)}, {mapping_errors} mapping errors"
        )

    scored = [r for r in rows if r["answer_turns"]]
    c0_covered = {r["question_id"] for r in scored if r["arms"][C0.name]["covered"]}
    arms = {
        arm.name: summarize_cells(
            rows, lambda r, n=arm.name: r["arms"][n], c0_covered, l1_dev_misses
        )
        for arm in ARMS
    }
    best = best_arm(arms)
    best_spec = next(a for a in ARMS if a.name == best)
    t2 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        curves = list(pool.map(lambda inst: best_curve(root, inst, best_spec), dev))
    for r, c in zip(rows, curves, strict=True):
        r["best_curve"] = c
    curve_seconds = round(time.time() - t2, 1)

    engine_curve = {
        str(b): summarize_cells(
            rows, lambda r, b=b: r["engine_curve"][str(b)], c0_covered, l1_dev_misses
        )
        for b in CURVE_BUDGETS
    }
    best_arm_curve = {
        str(b): summarize_cells(
            rows, lambda r, b=b: r["best_curve"][str(b)], c0_covered, l1_dev_misses
        )
        for b in CURVE_BUDGETS
    }
    control = summarize_cells(
        rows, lambda r: r["whole_haystack"], c0_covered, l1_dev_misses
    )
    classes = [c for r in scored for c in r["c0_missing_classes"]]
    decomposition = {
        "missing_rounds": len(classes),
        "a_budget_cut": classes.count("a"),
        "b_session_found_round_unscored": classes.count("b"),
        "c_session_absent": classes.count("c"),
        "questions": {
            r["question_id"]: r["c0_missing_classes"]
            for r in scored
            if r["c0_missing_classes"]
        },
    }
    l1_after = {"store": listing(L1_STORE), "receipts": listing(L1_RECEIPTS)}
    src_dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--", "src"],
            capture_output=True,
            text=True,
            cwd=str(_HERE),
            timeout=10,
        ).stdout.strip()
    )

    c0 = arms[C0.name]
    b = arms[best]
    gain = b["covered"] - c0["covered"]
    share_a = classes.count("a") / len(classes) if classes else 0.0
    at_180 = engine_curve["180000"]["rate"] or 0.0
    predictions = [
        {
            "id": "S0-P1",
            "claim": "C0 parity 150 of 150",
            "got": len(parity),
            "verdict": l1._verdict(len(parity) == 150, len(parity) != 150),
        },
        {
            "id": "S0-P2",
            "claim": "no mapping error",
            "got": mapping_errors,
            "verdict": l1._verdict(mapping_errors == 0, mapping_errors != 0),
        },
        {
            "id": "S0-P3",
            "claim": "C0 coverage 0.86 to 0.94",
            "got": c0["rate"],
            "verdict": l1._verdict(
                0.86 <= (c0["rate"] or 0) <= 0.94, not 0.86 <= (c0["rate"] or 0) <= 0.94
            ),
        },
        {
            "id": "S0-P4",
            "claim": "class (a) at least half of C0's missing rounds",
            "got": round(share_a, 4),
            "verdict": l1._verdict(share_a >= 0.5, share_a < 0.5),
        },
        {
            "id": "S0-P5",
            "claim": "best 90k arm at least 4 more covered, at most 1 regression; MISSED under 2 more",
            "got": {
                "arm": best,
                "more": gain,
                "regressions": len(b["regressed_vs_c0"]),
            },
            "verdict": l1._verdict(
                gain >= 4 and len(b["regressed_vs_c0"]) <= 1, gain < 2
            ),
        },
        {
            "id": "S0-P6",
            "claim": "engine list at 180,000 characters covers at least 0.96; MISSED under 0.93",
            "got": at_180,
            "verdict": l1._verdict(at_180 >= 0.96, at_180 < 0.93),
        },
        {
            "id": "S0-P7",
            "claim": "no spend, no reader or judge, src unchanged, L1 store and receipts unchanged",
            "got": {"src_dirty": src_dirty, "l1_unchanged": l1_before == l1_after},
            "verdict": l1._verdict(
                not src_dirty and l1_before == l1_after,
                src_dirty or l1_before != l1_after,
            ),
        },
    ]
    _write(
        Path(args.out),
        {
            "unit": "S0",
            "declaration": "memory 01M3HWFR3S9XNF6W4QYZ9Z0NY6",
            "provenance": l1._provenance(),
            "protocol": {
                "dataset": {
                    "file": str(l1.CORPUS.relative_to(_BENCH.parent)),
                    "sha256": l1.CORPUS_SHA256,
                },
                "split": "dev only, aml_run.split (L1's dev 150); the holdout is not served",
                "covered": "every has_answer turn in a served round, its text verbatim in the served content",
                "store": f"{root.relative_to(_BENCH.parent) if root.is_relative_to(_BENCH.parent) else root}, built fresh",
                "service": "bench/aml/service.py MemoryService, C0 options plus each arm's order, fill, trim and pool",
                "budget_chars": BUDGET,
                "curve_budgets": list(CURVE_BUDGETS),
                "best_arm_rule": "most covered, ties to fewer mean served characters",
                "no_model": "no reader, no judge, no model call, no API spend",
            },
            "seconds": {
                "ingest": ingest_seconds,
                "measure": measure_seconds,
                "best_curve": curve_seconds,
            },
            "parity": {"n": len(rows), "equal": len(parity)},
            "mapping_errors": mapping_errors,
            "questions_without_answer_turns": sorted(
                r["question_id"] for r in rows if not r["answer_turns"]
            ),
            "l1_dev_misses": sorted(l1_dev_misses),
            "arms": arms,
            "best_arm": best,
            "engine_curve": engine_curve,
            "best_arm_curve": best_arm_curve,
            "whole_haystack": control,
            "decomposition": decomposition,
            "l1_untouched": {"before": l1_before, "after": l1_after},
            "predictions": predictions,
            "rows": rows,
        },
    )
    print(
        json.dumps(
            {"best_arm": best, "c0": c0["covered"], "best": b["covered"], "n": c0["n"]}
        )
    )


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run")
    s.add_argument("--store", default=str(STORE))
    s.add_argument("--out", default=str(OUT))
    s.add_argument("--l1-prompts", default=str(L1_PROMPTS))
    s.add_argument("--l1-artifact", default=str(L1_ARTIFACT))
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_run)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
