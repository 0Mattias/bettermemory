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
import functools
import importlib
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
from aml.fused import FusedService  # noqa: E402
from aml.service import MemoryService  # noqa: E402
from judge.prompts import (  # noqa: E402
    aml_prompt,
    beam_answer_prompt,
    beam_batch_judge_prompt,
    parse_aml,
    parse_beam_scores,
)
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


LOCOMO = _HERE / "data" / "locomo_refined" / "data" / "public"
LOCOMO_CATEGORIES = {
    "1": "multi-hop",
    "2": "temporal",
    "3": "open-domain",
    "4": "single-hop",
}


def _locomo_ms(date_time: str) -> int | None:
    try:
        dt = datetime.strptime(date_time.strip(), "%I:%M %p on %d %B, %Y")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def load_locomo() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """LoCoMo-Refined (github.com/mem-eval-suite/LoCoMo_refined, public
    split): one user_id per conversation, one Add per session, every
    question asked of its conversation's store. A shared image is carried
    as its BLIP caption, which is the text the dataset ships for it."""
    convs = [
        json.loads(line)
        for line in (LOCOMO / "conversations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    sessions: dict[str, list[dict[str, Any]]] = {}
    for c in convs:
        uid = f"locomo:{c['sample_id']}"
        chunks = []
        for sess in c["sessions"]:
            ts = _locomo_ms(sess.get("date_time", ""))
            msgs = []
            for m in sess["messages"]:
                text = f"{m['speaker']}: {m['text']}"
                if m.get("blip_caption"):
                    text += f" [shared an image: {m['blip_caption']}]"
                msgs.append(
                    {
                        "role": m.get("role", "user"),
                        "content": text,
                        **({"timestamp": ts} if ts else {}),
                    }
                )
            chunks.append({"session_id": f"D{sess['session_index']}", "messages": msgs})
        sessions[uid] = chunks
    questions = []
    for line in (LOCOMO / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        q = json.loads(line)
        questions.append(
            {
                "question_id": q["qa_id"],
                "question_type": LOCOMO_CATEGORIES.get(
                    str(q["category"]), str(q["category"])
                ),
                "question": q["question"],
                "answer": "; ".join(str(a) for a in q["answer"]),
                "user_id": f"locomo:{q['sample_id']}",
                "speaker_1_name": q["speaker_a"],
                "speaker_2_name": q["speaker_b"],
                "answer_session_ids": sorted(
                    {f"D{e['session_index']}" for e in q.get("evidence_messages", [])}
                ),
            }
        )
    return questions, sessions


BEAM = _HERE / "data" / "beam"


def _beam_ms(anchor: str | None) -> int | None:
    if not anchor:
        return None
    try:
        dt = datetime.strptime(anchor.strip(), "%B-%d-%Y")
    except ValueError:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def load_beam(
    split: str,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """BEAM (JSON written by beam_convert.py): one user_id per conversation,
    one Add per chat batch dated by the batch's time anchor, every probing
    question graded against its rubric list."""
    convs = json.loads((BEAM / f"{split}.json").read_text(encoding="utf-8"))
    sessions: dict[str, list[dict[str, Any]]] = {}
    questions: list[dict[str, Any]] = []
    for c in convs:
        uid = f"beam-{split}:{c['conversation_id']}"
        chunks = []
        for i, batch in enumerate(c["batches"]):
            ts = _beam_ms(batch[0].get("time_anchor") if batch else None)
            msgs = [
                {
                    "role": m["role"],
                    "content": m["content"],
                    **({"timestamp": ts} if ts else {}),
                }
                for m in batch
            ]
            chunks.append({"session_id": f"B{i}", "messages": msgs})
        sessions[uid] = chunks
        for q in c["questions"]:
            questions.append(
                {
                    "question_id": q["question_id"],
                    "question_type": q["category"],
                    "question": q["question"],
                    "answer": q["gold"],
                    "rubric": q["rubric"],
                    "user_id": uid,
                    "answer_session_ids": [],
                }
            )
    return questions, sessions


# Datasets whose loader and answer/grade protocol live in their own module
# (`bench/aml/ds_<name>.py`). Each module exposes `load() -> (questions,
# sessions)` in the shape load_locomo returns and `answer_and_grade(client,
# inst, hits, reader, judge, thinking) -> row` in the shape answer_and_judge
# returns, with its prompts copied from the AML pipeline that grades it.
EXTERNAL = {
    "scriptmem": "aml.ds_scriptmem",
    "personamem-v1": "aml.ds_personamem_v1",
    "personamem-v2": "aml.ds_personamem_v2",
    "clbench": "aml.ds_clbench",
}


def render_answer(
    question: str,
    contents: list[str],
    speaker_1: str = "speaker 1",
    speaker_2: str = "speaker 2",
) -> str:
    values = {
        "speaker_1_name": speaker_1,
        "speaker_1_memories": "\n".join(contents),
        "speaker_2_name": speaker_2,
        "speaker_2_memories": "",
        "question": question,
    }
    return re.sub(
        r"\{\{(speaker_1_name|speaker_1_memories|speaker_2_name|speaker_2_memories|question)\}\}",
        lambda m: values[m.group(1)],
        ANSWER_TEMPLATE,
    )


def search_query(inst: dict[str, Any]) -> str:
    """The query exactly as server.py composes it from AML's Search body:
    multiple-choice `options` travel separately and are appended."""
    options = inst.get("options")
    if isinstance(options, list) and options:
        return inst["question"] + "\n" + "\n".join(str(o) for o in options)
    return inst["question"]


def adds_for(
    inst: dict[str, Any],
    sessions: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The user_id a question is asked of, and every Add its store gets:
    {request_id, messages, session_id} in order. The one place the Add
    requests are built, so an extraction warmed from them is keyed by
    exactly what Add receives."""
    if sessions is not None:
        user_id = inst["user_id"]
        return user_id, [
            {
                "request_id": f"{user_id}:{chunk['session_id']}",
                "messages": chunk["messages"],
                "session_id": chunk["session_id"],
            }
            for chunk in sessions[user_id]
        ]
    user_id = f"lme-s:{inst['question_id']}"
    dates = inst.get("haystack_dates") or []
    adds = []
    for idx, (sid, session) in enumerate(
        zip(inst["haystack_session_ids"], inst["haystack_sessions"])
    ):
        ts = _ms(dates[idx]) if idx < len(dates) else None
        adds.append(
            {
                "request_id": f"{user_id}:{idx}:{sid}",
                "messages": [
                    {
                        "role": t.get("role", "user"),
                        "content": t.get("content", ""),
                        **({"timestamp": ts} if ts else {}),
                    }
                    for t in session
                ],
                "session_id": sid,
            }
        )
    return user_id, adds


def check_units_store(root: Path, source: str) -> None:
    """Refuse a store whose units came from another source, or that was
    built with none. Units are derived at Add, and a finished Add is never
    repeated (its request_id is recorded), so reusing such a store would
    serve an arm with the wrong units or none, and measure nothing. The
    source is recorded in the store the first time units are written. A
    store with no record is taken as a regex store: E1/E2 built theirs
    (rounds-u1) before the record existed."""
    marker = root / ".units-source"
    if marker.exists():
        recorded = marker.read_text(encoding="utf-8").strip()
        if recorded != source:
            raise SystemExit(
                f"{root.name} holds {recorded} units, not {source}; pass a new --ingest"
            )
        return
    if source != "regex" and root.exists() and any(root.iterdir()):
        raise SystemExit(
            f"{root.name} was built without {source} units; pass a new --ingest"
        )
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(source + "\n", encoding="utf-8")


def ingest_and_search(
    service: Any,
    inst: dict[str, Any],
    sessions: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    user_id, adds = adds_for(inst, sessions)
    for add in adds:
        service.add(user_id=user_id, **add)
    query = search_query(inst) if sessions is not None else inst["question"]
    hits = service.search(user_id, query, TOP_K)
    for h, sess in zip(hits, service.sessions_for(user_id, [h["id"] for h in hits])):
        h["_session"] = sess
    return hits


async def answer_and_judge(
    client: Client,
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    reader: str,
    judge: str,
    thinking: str = "low",
) -> dict[str, Any]:
    if inst.get("rubric") is not None:
        return await _answer_and_judge_beam(client, inst, hits, reader, judge, thinking)
    prompt = render_answer(
        inst["question"],
        [h["content"] for h in hits],
        inst.get("speaker_1_name", "speaker 1"),
        inst.get("speaker_2_name", "speaker 2"),
    )
    ans = await client.complete(
        reader, [{"role": "user", "content": prompt}], max_tokens=512, temperature=0.0
    )
    generated = ans.text.strip()
    jud_messages = [
        {
            "role": "user",
            "content": aml_prompt(inst["question"], str(inst["answer"]), generated),
        }
    ]
    jud = await client.complete(
        judge,
        jud_messages,
        max_tokens=800,
        temperature=0.0,
        reasoning_effort=thinking,
    )
    verdict = parse_aml(jud.text)
    if verdict is None:
        jud = await _judge_retry(client, judge, jud_messages, thinking, json_mode=False)
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


async def _judge_retry(
    client: Client,
    judge: str,
    messages: list[dict[str, str]],
    thinking: str,
    *,
    json_mode: bool,
) -> Any:
    """One retry with room to finish for a judgment that came back empty or
    unparseable. OpenRouter serves Qwen3-14B from a provider that thinks
    even when asked not to, so an 800-token allowance can be spent before
    any answer is written; AML's own call (SiliconFlow, enable_thinking
    False) does not hit this. The retry is harness robustness, never a
    second opinion: it runs only when there is no verdict to keep."""
    return await client.complete(
        judge,
        messages,
        max_tokens=4000,
        temperature=0.0,
        reasoning_effort=thinking,
        response_json=json_mode,
    )


async def _answer_and_judge_beam(
    client: Client,
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    reader: str,
    judge: str,
    thinking: str,
) -> dict[str, Any]:
    """BEAM's protocol as AML runs it: the RAG answer prompt over the served
    context, then every rubric item graded 1 / 0.5 / 0 in one judge call;
    the question scores the mean. An unparseable judgment scores 0."""
    prompt = beam_answer_prompt("\n".join(h["content"] for h in hits), inst["question"])
    ans = await client.complete(
        reader, [{"role": "user", "content": prompt}], max_tokens=512, temperature=0.0
    )
    generated = ans.text.strip()
    rubric = inst["rubric"]
    jud_messages = [
        {
            "role": "user",
            "content": beam_batch_judge_prompt(inst["question"], generated, rubric),
        }
    ]
    jud = await client.complete(
        judge,
        jud_messages,
        max_tokens=1024,
        temperature=0.0,
        reasoning_effort=thinking,
        response_json=True,
    )
    scores = parse_beam_scores(jud.text, len(rubric)) if rubric else None
    if rubric and scores is None:
        jud = await _judge_retry(client, judge, jud_messages, thinking, json_mode=True)
        scores = parse_beam_scores(jud.text, len(rubric))
    score = sum(scores) / len(scores) if scores else 0.0
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "abstention": False,
        "n_hits": len(hits),
        "prompt_chars": len(prompt),
        "generated": generated,
        "verdict": None if scores is None else score >= 0.5,
        "score": round(score, 4),
        "rubric_scores": scores,
        "judge_raw": jud.text[:400],
        "evidence_sessions": [],
        "evidence_served": 0,
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
    sessions: dict[str, list[dict[str, Any]]] | None = None
    grade = answer_and_judge
    if args.dataset in EXTERNAL:
        module = importlib.import_module(EXTERNAL[args.dataset])
        corpus, sessions = module.load()
        grade = module.answer_and_grade
        dev, holdout = [q["question_id"] for q in corpus], []
        if args.split != "all":
            raise SystemExit(
                f"{args.dataset} is a validation instrument; run --split all"
            )
    elif args.dataset.startswith("beam-"):
        corpus, sessions = load_beam(args.dataset.split("-", 1)[1].upper())
        dev, holdout = [q["question_id"] for q in corpus], []
        if args.split != "all":
            raise SystemExit("beam is a validation instrument; run --split all")
    elif args.dataset == "locomo-refined":
        corpus, sessions = load_locomo()
        dev, holdout = [q["question_id"] for q in corpus], []
        if args.split != "all":
            raise SystemExit(
                "locomo-refined is a validation instrument; run --split all"
            )
    else:
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
    prefix = "" if args.dataset == "longmemeval-s" else f"{args.dataset}-"
    extractor: Any = None
    extraction: dict[str, Any] | None = None
    if args.system == "bettermemory" and (
        args.sheet != "none" or args.expand != "none"
    ):
        check_units_store(STORES / f"{prefix}{args.ingest}", args.units)
        if args.units == "claude":
            from aml import extract_claude

            chunks = [add["messages"] for q in todo for add in adds_for(q, sessions)[1]]
            stats = await extract_claude.warm(
                chunks,
                budget_usd=args.extract_budget,
                concurrency=args.concurrency,
                model=args.extract_model,
            )
            print(f"extraction {json.dumps(stats)}", file=sys.stderr)
            if stats["errors"]:
                raise SystemExit(
                    f"extraction failed on {stats['errors']} chunks "
                    f"({stats['first_error']}); an arm is never measured "
                    "on a partial extraction"
                )
            extraction = stats
            extractor = functools.partial(
                extract_claude.units_from_cache, model=args.extract_model
            )
    service = (
        FtsService(STORES / f"{prefix}fts-v1", serve=args.serve)
        if args.system == "fts"
        else FusedService(
            STORES / f"{prefix}{args.ingest}",
            STORES / f"{prefix}fts-v1",
            budget=args.budget_chars,
        )
        if args.system == "fused"
        else MemoryService(
            STORES / f"{prefix}{args.ingest}",
            fill=args.fill,
            order=args.order,
            annotate=args.annotate,
            serve=args.serve,
            trim=args.trim,
            trim_min_chars=args.trim_min_chars,
            budget=args.budget_chars,
            granularity="turns" if args.ingest.startswith("turns") else "rounds",
            sheet=args.sheet,
            sheet_chars=args.sheet_chars,
            sheet_last=args.sheet_last,
            units=args.units,
            extractor=extractor,
            expand=args.expand,
        )
    )
    print(
        f"{len(todo)} questions ({args.split}); corpus loaded in {time.time() - t0:.0f}s",
        file=sys.stderr,
    )

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        hits_all = list(
            pool.map(lambda q: ingest_and_search(service, q, sessions), todo)
        )
    t_search = time.time() - t1
    print(f"ingest+search {t_search:.0f}s", file=sys.stderr)

    async with Client(budget_usd=args.budget, concurrency=args.concurrency) as client:
        results = await asyncio.gather(
            *(
                grade(client, q, h, args.reader, args.judge, args.judge_thinking)
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

    def credit(r: dict[str, Any]) -> float:
        return float(r["score"]) if "score" in r else float(r["verdict"] is True)

    by_type: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_type[r["question_type"]].append(credit(r))
    acc = sum(credit(r) for r in rows) / len(rows) if rows else 0.0
    summary = {
        "arm": args.arm,
        "dataset": args.dataset,
        "system": args.system,
        "split": args.split,
        "ingest": args.ingest,
        "fill": args.fill,
        "order": args.order,
        "annotate": args.annotate,
        "serve": args.serve,
        "trim": args.trim,
        "trim_min_chars": args.trim_min_chars,
        "budget_chars": args.budget_chars,
        "sheet": args.sheet,
        "sheet_chars": args.sheet_chars,
        "sheet_last": args.sheet_last,
        "units": args.units,
        "expand": args.expand,
        "extract_model": args.extract_model if args.units == "claude" else None,
        "extraction": extraction,
        "reader": args.reader,
        "judge": args.judge,
        "judge_thinking": args.judge_thinking,
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
    p.add_argument(
        "--dataset",
        choices=(
            "longmemeval-s",
            "locomo-refined",
            "beam-100k",
            "beam-500k",
            "beam-1m",
            *EXTERNAL,
        ),
        default="longmemeval-s",
    )
    p.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    p.add_argument("--i-declared", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--arm", default="baseline")
    p.add_argument(
        "--system", choices=("bettermemory", "fts", "fused"), default="bettermemory"
    )
    p.add_argument("--ingest", default="rounds-v2")
    p.add_argument("--fill", default="none")
    p.add_argument("--order", default="rank")
    p.add_argument("--annotate", default="none")
    p.add_argument("--serve", type=int, default=100)
    p.add_argument("--trim", default="none")
    p.add_argument("--trim-min-chars", type=int, default=0)
    p.add_argument("--budget-chars", type=int, default=0)
    p.add_argument(
        "--sheet",
        default="none",
        help="units: serve a sheet of distilled user statements first (declaration "
        "E1); needs an ingest name of its own, since units are derived at Add",
    )
    p.add_argument("--sheet-chars", type=int, default=8000)
    p.add_argument("--sheet-last", action="store_true")
    p.add_argument(
        "--units",
        choices=("regex", "claude"),
        default="regex",
        help="claude: units from the product's session-capture prompt (declaration "
        "E3), fetched once before any Add; needs an ingest name of its own",
    )
    p.add_argument(
        "--expand",
        choices=("none", "keys", "keys-inline"),
        default="none",
        help="keys: units as extra search keys for their rounds (RRF), raw rounds "
        "served; keys-inline: the same, each served round carrying its units",
    )
    p.add_argument("--extract-model", default="anthropic/claude-haiku-4.5")
    p.add_argument(
        "--extract-budget",
        type=float,
        default=0.0,
        help="cap on NEW extraction spend in USD; cached extractions are free",
    )
    p.add_argument("--reader", default="openai/gpt-4o-mini")
    p.add_argument("--judge", required=True)
    p.add_argument(
        "--judge-thinking",
        choices=("low", "off"),
        default="low",
        help="off matches AML, which calls Qwen3-14B with thinking disabled",
    )
    p.add_argument("--budget", type=float, default=3.0)
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", default=None)
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
