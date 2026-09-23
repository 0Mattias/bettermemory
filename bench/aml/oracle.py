"""The reader's ceiling: serve only the evidence (LongMemEval-S, LoCoMo-Refined).

Every served configuration is judged by a fixed reader (gpt-4o-mini under
AML's answer prompt). This asks how well that reader does when retrieval is
perfect at session granularity: for each question, the served memories are
exactly the rounds of its labelled evidence sessions, oldest first, with the
same date headers the adapter writes, and nothing else. The gap between this
and a served arm is the most any ranking change can buy under this reader;
what remains below the ceiling is the reader's own.

    OPENROUTER_API_KEY=... .venv/bin/python bench/aml/oracle.py --split dev \\
        --judge qwen/qwen3-14b --out bench/aml/results/dev-ORACLE.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aml import run  # noqa: E402
from aml.service import _fmt_ts, rounds_of  # noqa: E402
from llm import Client  # noqa: E402


def evidence_hits(inst: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = set(inst["answer_session_ids"])
    dates = inst.get("haystack_dates") or []
    rounds: list[tuple[int, str, str]] = []
    for idx, (sid, session) in enumerate(
        zip(inst["haystack_session_ids"], inst["haystack_sessions"])
    ):
        if sid not in wanted:
            continue
        ts = run._ms(dates[idx]) if idx < len(dates) else None
        messages = [
            {
                "role": t.get("role", "user"),
                "content": t.get("content", ""),
                **({"timestamp": ts} if ts else {}),
            }
            for t in session
        ]
        for body, rts in rounds_of(messages):
            stamp = _fmt_ts(rts)
            rounds.append((rts or 0, sid, f"[{stamp}]\n{body}" if stamp else body))
    rounds.sort(key=lambda r: r[0])
    return [{"content": c, "_session": sid} for _, sid, c in rounds]


def locomo_evidence_hits(
    inst: dict[str, Any], sessions: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    wanted = set(inst["answer_session_ids"])
    out: list[dict[str, Any]] = []
    for chunk in sessions[inst["user_id"]]:
        if chunk["session_id"] not in wanted:
            continue
        for body, rts in rounds_of(chunk["messages"]):
            stamp = _fmt_ts(rts)
            out.append(
                {
                    "content": f"[{stamp}]\n{body}" if stamp else body,
                    "_session": chunk["session_id"],
                }
            )
    return out


async def main_async(args: argparse.Namespace) -> None:
    if args.dataset == "locomo-refined":
        corpus, sessions = run.load_locomo()
        todo = [q for q in corpus if q["answer_session_ids"]]
        hits_for = lambda q: locomo_evidence_hits(q, sessions)  # noqa: E731
    else:
        corpus = json.loads(run.CORPUS.read_text(encoding="utf-8"))
        dev, holdout = run.split(corpus)
        wanted = set(dev if args.split == "dev" else holdout)
        todo = [q for q in corpus if q["question_id"] in wanted]
        hits_for = evidence_hits
    async with Client(budget_usd=args.budget, concurrency=16) as client:
        rows = await asyncio.gather(
            *(
                run.answer_and_judge(
                    client,
                    q,
                    hits_for(q),
                    args.reader,
                    args.judge,
                    args.judge_thinking,
                )
                for q in todo
            )
        )
        spent = client.spent_usd
    by_type: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_type[r["question_type"]].append(r["verdict"] is True)
    acc = sum(r["verdict"] is True for r in rows) / len(rows)
    summary = {
        "arm": "ORACLE",
        "dataset": args.dataset,
        "system": "evidence-only",
        "split": args.split,
        "reader": args.reader,
        "judge": args.judge,
        "judge_thinking": args.judge_thinking,
        "n": len(rows),
        "accuracy": round(acc, 4),
        "by_type": {
            t: {"n": len(v), "accuracy": round(sum(v) / len(v), 4)}
            for t, v in sorted(by_type.items())
        },
        "unparseable_judgments": sum(1 for r in rows if r["verdict"] is None),
        "mean_prompt_chars": round(sum(r["prompt_chars"] for r in rows) / len(rows)),
        "new_spend_usd": round(spent, 4),
    }
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"provenance": run._provenance(), "summary": summary, "rows": rows},
                indent=1,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument(
        "--dataset",
        choices=("longmemeval-s", "locomo-refined"),
        default="longmemeval-s",
    )
    p.add_argument("--split", choices=("dev", "holdout"), default="dev")
    p.add_argument("--reader", default="openai/gpt-4o-mini")
    p.add_argument("--judge", required=True)
    p.add_argument("--judge-thinking", choices=("low", "off"), default="off")
    p.add_argument("--budget", type=float, default=2.0)
    p.add_argument("--out", default=None)
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
