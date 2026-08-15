"""The T2 acceptance read — A-P2 (wire compatibility) and A-P4 (the live
reappeared cases), graded mechanically against
`bench/rot/T2_ABSENCE_CLAIM_DECLARATION.md`.

Read-only over the live store, aggregates-only in the artifact — the
same publication rule as the T1 census: counts, booleans and grades, no
memory ids, no bodies, no claim strings, no paths beyond this
repository's own HEAD. The shipped functions do the work
(`parse_claim`, `check_claim`, `build_binding_index`,
`claim_level_drift`, `commit_patch_stream`) — the bench ethos, "the
shipped function, not a reimplementation," applies to acceptance reads
too.

Usage:

    .venv/bin/python bench/rot/t2_validation.py \
        --out bench/rot/results/t2-validation-2026-08-14.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_SRC = _REPO / "src"
for _p in (str(_SRC), str(_REPO), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bettermemory.claims import (  # noqa: E402
    build_binding_index,
    check_claim,
    claim_level_drift,
    parse_claim,
)
from bettermemory.origin import commit_patch_stream  # noqa: E402

from live_census import _load_memories  # noqa: E402


def _head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def read_a_p2(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every stored claim parses under the new grammar; the `!`-prefix
    reinterpretation count matches the declaration's measured 0."""
    total = 0
    failures = 0
    bang = 0
    for row in rows:
        for raw in row["claims_raw"]:
            if not isinstance(raw, str):
                failures += 1
                continue
            total += 1
            if raw.strip().startswith("!"):
                bang += 1
            try:
                parse_claim(raw)
            except ValueError:
                failures += 1
    return {
        "stored_claims_total": total,
        "parse_failures": failures,
        "bang_prefixed": bang,
    }


def read_a_p4(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """T1's REAPPEARED attested-absent paths, located by the same
    cohort-D rule (`live_census.census_absent`): the attested path
    exists again. For each one that is expressible as a `!path` claim
    (origin worktree live, path inside it): the declare-time oracle must
    refuse it, and the window of its most recent re-adding commit must
    fire the absent kind's strict tier."""
    reappeared = 0
    expressible = 0
    declaration_refused = 0
    readd_window_found = 0
    strict_fired = 0
    for row in rows:
        paths = row["absent_paths"]
        if not paths:
            continue
        worktree_raw = (row["origin"] or {}).get("worktree_root")
        worktree: Path | None = Path(worktree_raw) if worktree_raw else None
        if worktree is not None and not worktree.is_dir():
            worktree = None
        for raw in paths:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                if worktree is None:
                    continue
                candidate = worktree / raw
            if not candidate.exists():
                continue
            reappeared += 1
            if worktree is None or not candidate.is_relative_to(worktree):
                continue
            rel = candidate.relative_to(worktree).as_posix()
            claim = parse_claim(f"!{rel}")
            expressible += 1
            reason = check_claim(claim, worktree)
            if reason is not None and "exists" in reason:
                declaration_refused += 1
            try:
                out = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(worktree),
                        "log",
                        "--diff-filter=A",
                        "-1",
                        "--format=%H",
                        "--",
                        rel,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except OSError:
                continue
            sha = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
            if out.returncode != 0 or not sha:
                continue
            readd_window_found += 1
            stream = commit_patch_stream(worktree, [sha], [rel], toplevel=worktree)
            if stream is None:
                continue
            result = claim_level_drift(claim, build_binding_index(stream))
            if result["strict"]:
                strict_fired += 1
    return {
        "reappeared": reappeared,
        "expressible": expressible,
        "declaration_refused": declaration_refused,
        "readd_window_found": readd_window_found,
        "strict_fired": strict_fired,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=str(Path.home() / ".claude-memory"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    rows = _load_memories(Path(args.store))
    a_p2 = read_a_p2(rows)
    a_p4 = read_a_p4(rows)

    grades = {
        "A-P2": (
            "hit"
            if a_p2["parse_failures"] == 0 and a_p2["bang_prefixed"] == 0
            else "MISSED"
        ),
        "A-P4": (
            "hit"
            if a_p4["declaration_refused"] >= 2 and a_p4["strict_fired"] >= 2
            else "MISSED"
        ),
    }
    artifact = {
        "instrument": "bench/rot/t2_validation.py",
        "declaration": "bench/rot/T2_ABSENCE_CLAIM_DECLARATION.md",
        "generated": datetime.now(timezone.utc).isoformat(),
        "repo_head": _head(_REPO),
        "a_p2": a_p2,
        "a_p4": a_p4,
        "grades": grades,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(artifact, indent=2))
    return 0 if all(g == "hit" for g in grades.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
