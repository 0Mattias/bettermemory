"""Paired read of bench/aml arms against a reference arm.

Every arm answers the same questions with the same reader and judge, so
the comparison is paired: only the questions one arm got right and the
other got wrong carry information, and the exact McNemar test on those
discordant pairs is the read (bench/interval.py). Accuracy is printed
beside it so the size of a move is visible, never as the verdict.

Usage:

    .venv/bin/python bench/aml/compare.py bench/aml/results/dev-A0.json \\
        bench/aml/results/dev-A*.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interval import mcnemar_exact  # noqa: E402

TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)
SHORT = {t: "".join(w[0] for w in t.split("-")) for t in TYPES}


def load(path: str) -> tuple[dict[str, object], dict[str, bool], dict[str, str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data["rows"]
    right = {r["question_id"]: r["verdict"] is True for r in rows}
    qtype = {r["question_id"]: r["question_type"] for r in rows}
    return data["summary"], right, qtype


def main() -> None:
    ref_path, *paths = sys.argv[1:]
    _, ref, qtype = load(ref_path)
    head = "arm        judge          acc     " + "  ".join(
        f"{SHORT[t]:>5}" for t in TYPES
    )
    print(head + "   ref-only  arm-only  p")
    for path in [ref_path, *[p for p in paths if p != ref_path]]:
        summary, right, _ = load(path)
        if set(right) != set(ref):
            print(f"{path}: question set differs from the reference; not paired")
            continue
        by_type = []
        for t in TYPES:
            ids = [q for q in right if qtype[q] == t]
            by_type.append(
                f"{sum(right[q] for q in ids) / len(ids):5.2f}" if ids else "    -"
            )
        ref_only = sum(1 for q in ref if ref[q] and not right[q])
        arm_only = sum(1 for q in ref if right[q] and not ref[q])
        p = mcnemar_exact(ref_only, arm_only)
        acc = sum(right.values()) / len(right)
        judge = str(summary.get("judge", "")).split("/")[-1][:14]
        print(
            f"{summary.get('arm', '?'):10s} {judge:14s} {acc:.4f}  "
            + "  ".join(by_type)
            + f"   {ref_only:8d}  {arm_only:8d}  {p:.3f}"
        )


if __name__ == "__main__":
    main()
