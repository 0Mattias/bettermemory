"""Grade the multi-repo results against the pre-registered predictions.

Mechanical on purpose. Each of P1-P7 is a lambda over the results JSON
plus the exact threshold text from PREREGISTRATION.md, so "hit" and
"MISSED" are computed rather than narrated. A prediction graded by prose
after the fact is not a prediction.

    venv/bin/python bench/rot/scorecard.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent


def _in(value: float | None, low: float, high: float) -> bool:
    return value is not None and low <= value <= high


def _relocated(repo: dict[str, Any]) -> bool:
    """Every non-excluded .py under the elected subdir gone at t1.

    Declared in the PREREGISTRATION addendum before any detector ran.
    """
    return repo["file_level"]["base_rate"] == 1.0


def grade(report: dict[str, Any], corpus: dict[str, Any]) -> list[dict[str, Any]]:
    pooled = report["pooled"]
    # Claim DENSITY, derived here rather than stored: the screen already
    # recorded each repo's non-excluded .py count, and the run recorded its
    # claim count, so the ratio needs no extra pass. Every power estimate
    # made while planning this corpus rested on bettermemory's 8.8.
    scored = {r["repo"] for r in report["per_repo"]}
    files = sum(
        e.get("py_files", 0)
        for s in corpus["strata"].values()
        for e in s
        if e["full_name"] in scored
    )
    claims = sum(r["claims"] for r in report["per_repo"])
    report["claims_per_file"] = round(claims / files, 3) if files else None
    all_n = pooled["file_level_incumbent"]["ALL"]["n"]
    report["symbol_share"] = (
        round(pooled["file_level_incumbent"]["symbol"]["n"] / all_n, 4)
        if all_n
        else None
    )
    per_repo = report["per_repo"]
    pruned = [r for r in per_repo if r["stratum"] == "D" and not _relocated(r)]
    relocated = [r for r in per_repo if r["stratum"] == "D" and _relocated(r)]
    path_abs = report["path_drift_absolute_arm"]

    checks: list[tuple[str, str, Callable[[], bool], Any]] = [
        (
            "P1",
            "path_drift (absolute arm) flags >=95% of claims whose file is gone "
            "and <=2% of claims whose file survives; MISSED if TPR < 0.90",
            lambda: (
                (path_abs["ALL"]["unflagged_stale_rate"] is not None)
                and (1 - path_abs["ALL"]["unflagged_stale_rate"]) >= 0.90
            ),
            lambda: {
                "tpr": None
                if path_abs["ALL"]["unflagged_stale_rate"] is None
                else round(1 - path_abs["ALL"]["unflagged_stale_rate"], 4),
                "false_alarm_rate": path_abs["ALL"]["false_alarm_rate"],
            },
        ),
        (
            "P2",
            "relative arm: EXACTLY ZERO path-drift flags; MISSED if any non-zero",
            lambda: pooled["path_drift_only"]["ALL"]["flag_rate"] == 0.0,
            lambda: {
                "relative_arm_flag_rate": pooled["path_drift_only"]["ALL"]["flag_rate"]
            },
        ),
        (
            "P3",
            "incumbent pooled flag rate in [0.35, 0.80] and alerts/catch in [4, 15]; "
            "macro-J < 0.15; MISSED if macro-J >= 0.15",
            lambda: pooled["file_level_incumbent"]["ALL"]["youden_j"] < 0.15,
            lambda: {
                "flag_rate": pooled["file_level_incumbent"]["ALL"]["flag_rate"],
                "alerts_per_catch": pooled["file_level_incumbent"]["ALL"][
                    "alerts_per_catch"
                ],
                "youden_j": pooled["file_level_incumbent"]["ALL"]["youden_j"],
                "flag_rate_in_band": _in(
                    pooled["file_level_incumbent"]["ALL"]["flag_rate"], 0.35, 0.80
                ),
                "alerts_in_band": _in(
                    pooled["file_level_incumbent"]["ALL"]["alerts_per_catch"], 4, 15
                ),
            },
        ),
        (
            "P4",
            "pooled symbol-class AUROC of the commit count in [0.50, 0.65]; "
            "MISSED if >= 0.70",
            lambda: _in(pooled["file_level_incumbent"]["symbol"]["auroc"], 0.50, 0.65),
            lambda: {
                "symbol_auroc": pooled["file_level_incumbent"]["symbol"]["auroc"],
                "symbol_auroc_p": pooled["file_level_incumbent"]["symbol"]["auroc_p"],
                "n_positives": pooled["file_level_incumbent"]["symbol"][
                    "actually_false"
                ],
            },
        ),
        (
            "P5",
            "THE RETRACTION BRANCH — claim_level_strict stops matching "
            "oracle_replica on symbol claims: pooled symbol precision <= 0.97 "
            "with >= 5 false positives. MISSED if precision is exactly 1.000",
            lambda: (
                (pooled["claim_level_strict"]["symbol"]["precision"] is not None)
                and pooled["claim_level_strict"]["symbol"]["precision"] <= 0.97
            ),
            lambda: {
                "symbol_precision": pooled["claim_level_strict"]["symbol"]["precision"],
                "symbol_j": pooled["claim_level_strict"]["symbol"]["youden_j"],
                "oracle_replica_j": pooled["oracle_replica"]["ALL"]["youden_j"],
                "false_positives": None
                if pooled["claim_level_strict"]["symbol"]["precision"] is None
                else round(
                    pooled["claim_level_strict"]["symbol"]["n"]
                    * pooled["claim_level_strict"]["symbol"]["flag_rate"]
                    * (1 - pooled["claim_level_strict"]["symbol"]["precision"])
                ),
            },
        ),
        (
            "P6",
            "pooled claims per non-excluded .py file in [4.0, 9.0] "
            "(bettermemory measured 8.8); MISSED if outside",
            lambda: _in(
                report.get("claims_per_file"),
                4.0,
                9.0,
            ),
            lambda: {
                "claims_per_file": report.get("claims_per_file"),
                "symbol_share": report.get("symbol_share"),
            },
        ),
        (
            "P7",
            ">=8 stratum-D qualifiers; and per the addendum, D-PRUNED is the "
            "headline. MISSED if fewer than 8 pruned repositories",
            lambda: len(pruned) >= 8,
            lambda: {
                "d_total": sum(1 for r in per_repo if r["stratum"] == "D"),
                "d_pruned": len(pruned),
                "d_relocated": len(relocated),
                "walked_to_rank": report["walked_to_rank"],
            },
        ),
    ]

    out = []
    for name, text, predicate, facts in checks:
        try:
            passed = bool(predicate())
        except Exception as error:  # a prediction that cannot be evaluated
            out.append(
                {
                    "id": name,
                    "prediction": text,
                    "verdict": "UNEVALUABLE",
                    "facts": {"error": str(error)},
                }
            )
            continue
        out.append(
            {
                "id": name,
                "prediction": text,
                "verdict": "hit" if passed else "MISSED",
                "facts": facts(),
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grade the pre-registered predictions."
    )
    parser.add_argument("--results", default=str(_HERE / "results" / "multirepo.json"))
    parser.add_argument("--corpus", default=str(_HERE / "corpus.json"))
    parser.add_argument("--out", default=str(_HERE / "results" / "scorecard.json"))
    args = parser.parse_args()

    report = json.loads(Path(args.results).read_text())
    corpus = json.loads(Path(args.corpus).read_text())
    rows = grade(report, corpus)
    Path(args.out).write_text(json.dumps(rows, indent=2))

    hits = sum(1 for r in rows if r["verdict"] == "hit")
    print(f"pre-registered predictions: {hits}/{len(rows)} hit\n")
    for row in rows:
        print(f"  {row['id']}  {row['verdict']:<11} {row['facts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
