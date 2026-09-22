"""Local reproduction of the Agent Memory Leaderboard's LongMemEval-S cell.

WHY THIS EXISTS. AML scores a submitted Add/Search service with an
answer model and a judge it does not name. Every change to the engine
has to be measured before it is submitted, and a Smoke run on AML itself
is rationed to one an hour. This driver runs the same three steps on
this machine: Add every haystack session through `service.MemoryService`
(the code the HTTP adapter serves), Search the question at AML's fixed
top_k = 100, then answer with AML's published answer prompt and grade
with AML's published judge prompt, both verbatim from the pipeline in
github.com/AML-memory/agent-memory-leaderboard (commit 1b8142b). The
reader and judge are stand-ins chosen by bench/judge; the numbers are a
local instrument for DELTAS, never a claim about AML's own leaderboard.

THE SPLIT. LongMemEval-S's 500 questions are divided once, by a fixed
seed and stratified by type, into a 150-question dev split every lever
is tuned on and a 350-question holdout that is read once per declared
experiment. `--split holdout` refuses to run without `--i-declared`.

THE STORES are built once per ingest configuration under
bench/aml/.stores/ (gitignored) and reused: ranking and presentation
changes re-run Search only. A store is rebuilt when the ingest
configuration name changes.

Usage:

    OPENROUTER_API_KEY=... .venv/bin/python bench/aml/run.py --split dev \\
        --reader openai/gpt-4o-mini --judge <from bench/judge>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
sys.path.insert(0, str(_BENCH))

from aml.fts import FtsService  # noqa: E402
from aml.service import MemoryService  # noqa: E402
from judge.prompts import aml_prompt, parse_aml  # noqa: E402
from llm import BudgetExceeded, Client  # noqa: E402

CORPUS = _BENCH / "longmemeval" / "data" / "longmemeval_s_cleaned.json"
STORES = _HERE / ".stores"
RESULTS = _HERE / "results"
SPLIT_SEED = 20260922
DEV_N = 150
TOP_K = 100

# Verbatim from AML data/longmemeval-s/pipeline.py (commit 1b8142b).
ANSWER_TEMPLATE = """You are asked to answer a question based on your memories of a conversation.

<instructions>
1. Use only the provided memories. Prefer the memory that answers the question most directly.
2. Your memories are episodic raw observations. Reason about what they imply. Do not refuse just because the answer is not stated verbatim.
3. The question may contain typos. Match it to the most relevant memory even if the wording differs.
4. When multiple answers are possible, list all supported answers, not just the first.
5. For counts or time intervals, enumerate carefully before answering.
6. Preserve specific names, titles, places, and labels from the memories. Use "Rob" not "a colleague", "Sweden" not "home country".
7. Convert relative times like "yesterday", "last month", and "last year" into dates, months, or years when the memory timestamp makes it clear. Keep week-based expressions relative.
8. If memories conflict, prefer the most recent supported memory.
9. For list questions, include all required items and no extras.
10. Keep the final answer minimal. Do not add explanation, background, or extra dates unless needed for correctness.
</instructions>

<memories>
Memories for user {{speaker_1_name}}:

{{speaker_1_memories}}

Memories for user {{speaker_2_name}}:

{{speaker_2_memories}}
</memories>

Question: {{question}}
Answer with the shortest correct phrase or sentence. No preamble, no fluff:"""


_DATE_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})\D*?(\d{2}):(\d{2})")


def _ms(date: str) -> int | None:
    m = _DATE_RE.match(date.strip())
    if not m:
        return None
    y, mo, d, hh, mm = (int(g) for g in m.groups())
    return int(datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def split(corpus: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    rng = random.Random(SPLIT_SEED)
    by_type: dict[str, list[str]] = defaultdict(list)
    for q in corpus:
        by_type[q["question_type"]].append(q["question_id"])
    dev: list[str] = []
    total = len(corpus)
    for qtype in sorted(by_type):
        ids = sorted(by_type[qtype])
        n = round(DEV_N * len(ids) / total)
        dev += rng.sample(ids, n)
    dev_set = set(dev)
    holdout = [q["question_id"] for q in corpus if q["question_id"] not in dev_set]
    return sorted(dev_set), holdout


def render_answer(question: str, contents: list[str]) -> str:
    values = {
        "speaker_1_name": "speaker 1",
        "speaker_1_memories": "\n".join(contents),
        "speaker_2_name": "speaker 2",
        "speaker_2_memories": "",
        "question": question,
    }
    return re.sub(
        r"\{\{(speaker_1_name|speaker_1_memories|speaker_2_name|speaker_2_memories|question)\}\}",
        lambda m: values[m.group(1)],
        ANSWER_TEMPLATE,
    )


def ingest_and_search(service: Any, inst: dict[str, Any]) -> list[dict[str, Any]]:
    user_id = f"lme-s:{inst['question_id']}"
    dates = inst.get("haystack_dates") or []
    for idx, (sid, session) in enumerate(
        zip(inst["haystack_session_ids"], inst["haystack_sessions"])
    ):
        ts = _ms(dates[idx]) if idx < len(dates) else None
        messages = [
            {
                "role": t.get("role", "user"),
                "content": t.get("content", ""),
                **({"timestamp": ts} if ts else {}),
            }
            for t in session
        ]
        service.add(
            request_id=f"{user_id}:{idx}:{sid}",
            user_id=user_id,
            messages=messages,
            session_id=sid,
        )
    hits = service.search(user_id, inst["question"], TOP_K)
    for h, sess in zip(hits, service.sessions_for(user_id, [h["id"] for h in hits])):
        h["_session"] = sess
    return hits


async def answer_and_judge(
    client: Client,
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    reader: str,
    judge: str,
) -> dict[str, Any]:
    prompt = render_answer(inst["question"], [h["content"] for h in hits])
    ans = await client.complete(
        reader, [{"role": "user", "content": prompt}], max_tokens=512, temperature=0.0
    )
    generated = ans.text.strip()
    jud = await client.complete(
        judge,
        [
            {
                "role": "user",
                "content": aml_prompt(inst["question"], str(inst["answer"]), generated),
            }
        ],
        max_tokens=800,
        temperature=0.0,
        reasoning_effort="low",
    )
    verdict = parse_aml(jud.text)
    evidence = set(inst.get("answer_session_ids") or [])
    served = {h.get("_session", "") for h in hits}
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "abstention": inst["question_id"].endswith("_abs"),
        "n_hits": len(hits),
        "prompt_chars": len(prompt),
        "generated": generated,
        "verdict": verdict,
        "judge_raw": jud.text[:400],
        "evidence_sessions": sorted(evidence),
        "evidence_served": len(evidence & served),
        "cost": ans.cost + jud.cost,
    }


def _provenance() -> dict[str, Any]:
    def git(*argv: str) -> str:
        return subprocess.run(
            ["git", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(_HERE),
            timeout=10,
        ).stdout.strip()

    try:
        import bettermemory

        version: str | None = bettermemory.__version__
    except ImportError:
        version = None
    return {
        "bettermemory_version": version,
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "tree_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


async def main_async(args: argparse.Namespace) -> None:
    t0 = time.time()
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    dev, holdout = split(corpus)
    if args.split == "holdout" and not args.i_declared:
        raise SystemExit(
            "the holdout is read once per declared experiment; pass --i-declared"
        )
    wanted = set(
        dev
        if args.split == "dev"
        else holdout
        if args.split == "holdout"
        else dev + holdout
    )
    todo = [q for q in corpus if q["question_id"] in wanted]
    if args.limit:
        todo = todo[: args.limit]
    service = (
        FtsService(STORES / "fts-v1")
        if args.system == "fts"
        else MemoryService(
            STORES / args.ingest,
            fill=args.fill,
            order=args.order,
            annotate=args.annotate,
            serve=args.serve,
            trim=args.trim,
        )
    )
    print(
        f"{len(todo)} questions ({args.split}); corpus loaded in {time.time() - t0:.0f}s",
        file=sys.stderr,
    )

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        hits_all = list(pool.map(lambda q: ingest_and_search(service, q), todo))
    t_search = time.time() - t1
    print(f"ingest+search {t_search:.0f}s", file=sys.stderr)

    async with Client(budget_usd=args.budget, concurrency=args.concurrency) as client:
        results = await asyncio.gather(
            *(
                answer_and_judge(client, q, h, args.reader, args.judge)
                for q, h in zip(todo, hits_all)
            ),
            return_exceptions=True,
        )
        spent, calls, hits_c = client.spent_usd, client.calls, client.cache_hits
    rows = [r for r in results if isinstance(r, dict)]
    errors = [
        f"{q['question_id']}: {r!r}"
        for q, r in zip(todo, results)
        if isinstance(r, BaseException)
    ]
    if any(isinstance(r, BudgetExceeded) for r in results):
        print("BUDGET EXCEEDED; partial result", file=sys.stderr)

    by_type: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_type[r["question_type"]].append(r["verdict"] is True)
    acc = sum(1 for r in rows if r["verdict"] is True) / len(rows) if rows else 0.0
    summary = {
        "arm": args.arm,
        "system": args.system,
        "split": args.split,
        "ingest": args.ingest,
        "fill": args.fill,
        "order": args.order,
        "annotate": args.annotate,
        "serve": args.serve,
        "trim": args.trim,
        "reader": args.reader,
        "judge": args.judge,
        "top_k": TOP_K,
        "n": len(rows),
        "errors": len(errors),
        "accuracy": round(acc, 4),
        "by_type": {
            t: {"n": len(v), "accuracy": round(sum(v) / len(v), 4)}
            for t, v in sorted(by_type.items())
        },
        "unparseable_judgments": sum(1 for r in rows if r["verdict"] is None),
        "mean_hits": round(sum(r["n_hits"] for r in rows) / len(rows), 1)
        if rows
        else 0,
        "mean_prompt_chars": round(sum(r["prompt_chars"] for r in rows) / len(rows))
        if rows
        else 0,
        "new_spend_usd": round(spent, 4),
        "new_calls": calls,
        "cache_hits": hits_c,
        "seconds_ingest_search": round(t_search, 1),
    }
    print(json.dumps(summary, indent=1))
    for e in errors[:5]:
        print("  error:", e, file=sys.stderr)
    if args.out:
        RESULTS.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(
                {
                    "provenance": _provenance(),
                    "summary": summary,
                    "rows": rows,
                    "errors": errors,
                },
                indent=1,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    p.add_argument("--i-declared", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--arm", default="baseline")
    p.add_argument("--system", choices=("bettermemory", "fts"), default="bettermemory")
    p.add_argument("--ingest", default="rounds-v2")
    p.add_argument("--fill", default="none")
    p.add_argument("--order", default="rank")
    p.add_argument("--annotate", default="none")
    p.add_argument("--serve", type=int, default=100)
    p.add_argument("--trim", default="none")
    p.add_argument("--reader", default="openai/gpt-4o-mini")
    p.add_argument("--judge", required=True)
    p.add_argument("--budget", type=float, default=3.0)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default=None)
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
