"""Paired read of bench/aml arms against a reference arm.

Every arm answers the same questions with the same reader and judge, so
the comparison is paired. For yes/no grading (LongMemEval-S, LoCoMo) only
the questions one arm got right and the other got wrong carry
information, and the exact McNemar test on those discordant pairs is the
read. For rubric grading (BEAM, a 0 / 0.5 / 1 mean per question) the read
is the paired mean difference with its 95% interval, since the scores are
fractions rather than successes (bench/interval.py). Accuracy is printed
beside it so the size of a move is visible, never as the verdict.

Categories are taken from the reference file, so the same tool reads every
dataset the driver runs.

Usage:

    .venv/bin/python bench/aml/compare.py bench/aml/results/dev-A0.json \\
        bench/aml/results/dev-A*.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interval import mcnemar_exact, paired_mean_diff_ci  # noqa: E402


def load(path: str) -> tuple[dict[str, object], dict[str, float], dict[str, str], bool]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data["rows"]
    scored = any("score" in r for r in rows)
    credit = {
        r["question_id"]: float(r["score"]) if scored else float(r["verdict"] is True)
        for r in rows
    }
    qtype = {r["question_id"]: r["question_type"] for r in rows}
    return data["summary"], credit, qtype, scored


def _short(t: str) -> str:
    parts = t.replace("_", "-").split("-")
    return "".join(p[0] for p in parts if p)[:5]


def main() -> None:
    ref_path, *paths = sys.argv[1:]
    _, ref, qtype, scored = load(ref_path)
    types = sorted(set(qtype.values()))
    head = "arm        judge          acc     " + "  ".join(
        f"{_short(t):>5}" for t in types
    )
    print(head + ("   diff    95% interval" if scored else "   ref-only  arm-only  p"))
    for path in [ref_path, *[p for p in paths if p != ref_path]]:
        summary, credit, _, _ = load(path)
        if set(credit) != set(ref):
            print(f"{path}: question set differs from the reference; not paired")
            continue
        by_type = []
        for t in types:
            ids = [q for q in credit if qtype[q] == t]
            by_type.append(
                f"{sum(credit[q] for q in ids) / len(ids):5.2f}" if ids else "    -"
            )
        acc = sum(credit.values()) / len(credit)
        judge = str(summary.get("judge", "")).split("/")[-1][:14]
        line = f"{summary.get('arm', '?'):10s} {judge:14s} {acc:.4f}  " + "  ".join(
            by_type
        )
        if scored:
            qs = sorted(ref)
            diff, lo, hi = paired_mean_diff_ci(
                [credit[q] for q in qs], [ref[q] for q in qs]
            )
            line += f"   {diff:+.4f} [{lo:+.4f}, {hi:+.4f}]"
        else:
            ref_only = sum(1 for q in ref if ref[q] and not credit[q])
            arm_only = sum(1 for q in ref if credit[q] and not ref[q])
            p = mcnemar_exact(ref_only, arm_only)
            line += f"   {ref_only:8d}  {arm_only:8d}  {p:.3f}"
        print(line)


if __name__ == "__main__":
    main()
