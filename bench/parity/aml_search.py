"""Rank parity on the AML instrument: the Search step alone, no reader,
no judge, no spend.

bench/aml's published rows carry verdicts, not rankings, so the E3-keys
dev result cannot be compared ranking by ranking. This runs the same
service the E3-keys arm ran (bettermemory over the cached `rounds-c1`
store with Claude-extracted units as extra search keys) for the dev split
and records what Search served per question: the ids, their sessions and
the evidence count. The cached store is opened read-only in effect: no
Add is issued, so a question whose store is missing is reported, never
built. Two things tie the artifact to the published run: `n_hits` and
`evidence_served` per question must match bench/aml/results/dev-E3-keys.json.

Usage:

    .venv/bin/python bench/parity/aml_search.py --out bench/parity/results/aml-search-E3-keys-8.0.0-2026-09-25.json
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
_BENCH = _ROOT / "bench"
for p in (str(_ROOT / "src"), str(_BENCH)):
    if p not in sys.path:
        sys.path.insert(0, p)

from aml import extract_claude  # noqa: E402
from aml import run as aml_run  # noqa: E402
from aml.service import MemoryService  # noqa: E402

ARM = {
    "arm": "E3-keys",
    "ingest": "rounds-c1",
    "fill": "none",
    "order": "rank",
    "annotate": "none",
    "serve": 100,
    "trim": "none",
    "trim_min_chars": 0,
    "budget_chars": 90000,
    "sheet": "none",
    "sheet_chars": 8000,
    "sheet_last": False,
    "units": "claude",
    "expand": "keys",
    "extract_model": "anthropic/claude-haiku-4.5",
}
PUBLISHED = _BENCH / "aml" / "results" / "dev-E3-keys.json"


def build_service(stores: Path = aml_run.STORES) -> MemoryService:
    return MemoryService(
        stores / ARM["ingest"],
        fill=ARM["fill"],
        order=ARM["order"],
        annotate=ARM["annotate"],
        serve=ARM["serve"],
        trim=ARM["trim"],
        trim_min_chars=ARM["trim_min_chars"],
        budget=ARM["budget_chars"],
        granularity="rounds",
        sheet=ARM["sheet"],
        sheet_chars=ARM["sheet_chars"],
        sheet_last=ARM["sheet_last"],
        units=ARM["units"],
        extractor=functools.partial(
            extract_claude.units_from_cache, model=ARM["extract_model"]
        ),
        expand=ARM["expand"],
    )


def dev_questions() -> list[dict[str, Any]]:
    corpus = json.loads(aml_run.CORPUS.read_text(encoding="utf-8"))
    dev, _ = aml_run.split(corpus)
    wanted = set(dev)
    return [q for q in corpus if q["question_id"] in wanted]


def search_rows(
    service: MemoryService, questions: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for q in questions:
        user_id = f"lme-s:{q['question_id']}"
        key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        if not (service.root / key).is_dir():
            missing.append(q["question_id"])
            continue
        hits = service.search(user_id, q["question"], aml_run.TOP_K)
        ids = [h["id"] for h in hits]
        sessions = service.sessions_for(user_id, ids)
        evidence = set(q.get("answer_session_ids") or [])
        rows.append(
            {
                "question_id": q["question_id"],
                "question_type": q["question_type"],
                "n_hits": len(hits),
                "ids": ids,
                "scores": [round(float(h.get("score", 0.0)), 6) for h in hits],
                "sessions": sessions,
                "evidence_served": len(evidence & set(sessions)),
            }
        )
    return rows, missing


def digest(rows: list[dict[str, Any]]) -> str:
    material = [[r["question_id"], r["ids"]] for r in rows]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def check_against_published(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not PUBLISHED.exists():
        return {"published": None}
    published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    by_id = {r["question_id"]: r for r in published.get("rows", [])}
    mismatched: list[dict[str, Any]] = []
    checked = 0
    for r in rows:
        p = by_id.get(r["question_id"])
        if p is None:
            continue
        checked += 1
        if (
            p.get("n_hits") != r["n_hits"]
            or p.get("evidence_served") != r["evidence_served"]
        ):
            mismatched.append(
                {
                    "question_id": r["question_id"],
                    "n_hits": [p.get("n_hits"), r["n_hits"]],
                    "evidence_served": [p.get("evidence_served"), r["evidence_served"]],
                }
            )
    return {
        "published": str(PUBLISHED.relative_to(_ROOT)),
        "published_version": (published.get("provenance") or {}).get(
            "bettermemory_version"
        ),
        "checked": checked,
        "mismatched": mismatched,
    }


def compare(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    index_b = {r["question_id"]: r for r in b["rows"]}
    diffs = []
    for ra in a["rows"]:
        rb = index_b.pop(ra["question_id"], None)
        if rb is None:
            diffs.append({"question_id": ra["question_id"], "field": "missing_in_b"})
        elif ra["ids"] != rb["ids"]:
            diffs.append(
                {
                    "question_id": ra["question_id"],
                    "field": "ids",
                    "a": ra["ids"],
                    "b": rb["ids"],
                }
            )
    diffs.extend({"question_id": k, "field": "missing_in_a"} for k in index_b)
    return diffs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), default=None)
    args = parser.parse_args()
    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        diffs = compare(a, b)
        for d in diffs:
            print(json.dumps(d))
        print(
            f"{len(diffs)} differing questions; digests {'agree' if a['digest'] == b['digest'] else 'differ'}"
        )
        return 0 if not diffs else 1
    questions = dev_questions()
    service = build_service()
    rows, missing = search_rows(service, questions)
    artifact = {
        "kind": "rank-parity/aml-search",
        "provenance": aml_run._provenance(),
        "arm": ARM,
        "dataset": "longmemeval-s",
        "split": "dev",
        "top_k": aml_run.TOP_K,
        "n": len(rows),
        "missing_stores": missing,
        "published_check": check_against_published(rows),
        "digest": digest(rows),
        "rows": rows,
    }
    text = json.dumps(artifact, indent=1) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    check = artifact["published_check"]
    print(
        f"{len(rows)} questions searched, {len(missing)} stores missing; "
        f"published check: {check.get('checked')} checked, "
        f"{len(check.get('mismatched') or [])} mismatched; digest {artifact['digest'][:16]}",
        file=sys.stderr,
    )
    return 0 if not missing and not check.get("mismatched") else 1


if __name__ == "__main__":
    sys.exit(main())
