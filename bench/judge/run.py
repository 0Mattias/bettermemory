"""Judge validation: which model grades LongMemEval answers correctly.

WHY THIS EXISTS. A QA score is the reader's answer passed through a
judge, and the judge's errors land in the score unannounced. The 2026
judge literature finds no judge uniformly reliable across tasks, and the
LoCoMo audit measured a gpt-4o-mini judge accepting 62.81% of
deliberately wrong answers. So the judge for every QA number this repo
publishes is chosen by its measured error on this grading task, not by
reputation or by habit.

THE INSTRUMENT. LongMemEval questions sampled per type with a fixed
seed, each paired with candidate answers whose correctness is known by
construction:

  normal questions   pos_verbatim    the gold answer, wrapped in a sentence
                     pos_paraphrase  the gold restated, every fact kept
                     neg_swap        another same-type question's gold
                     neg_near_miss   the gold's form with one fact changed
                     neg_hedge       a topical non-answer
  abstention         pos_refusal_fixed  a fixed "not in our history" reply
                     pos_refusal     a refusal that names what IS known
                     neg_fabricated  a confident invented answer
                     neg_swap        another same-type question's gold
  preference         pos_personal    a reply satisfying the rubric
                     neg_generic     helpful advice ignoring the user
                     neg_contradict  a reply personalised the wrong way
                     neg_swap        another preference question's reply

Paraphrases, near misses, hedges, refusals and preference replies are
written by a generator model that is not a judge candidate, then read
and corrected by hand before any judge sees them (`items.json` is the
reviewed file; `build` refuses to overwrite it).

TWO PROMPT FORMS. `lme` is the official LongMemEval per-type yes/no
prompt (evaluate_qa.py, parsed the official way: "yes" in the lowercased
response). `aml` is the Agent Memory Leaderboard's ACCURACY_PROMPT for
LongMemEval-S, parsed with its own JSON rule; an unparseable response is
recorded as such and scored as WRONG.

Usage:

    OPENROUTER_API_KEY=... .venv/bin/python bench/judge/run.py build
    OPENROUTER_API_KEY=... .venv/bin/python bench/judge/run.py judge
    .venv/bin/python bench/judge/run.py score
"""

from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import random
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
sys.path.insert(0, str(_BENCH))

from judge.prompts import aml_prompt, lme_prompt, parse_aml, parse_lme  # noqa: E402
from llm import Client  # noqa: E402

ORACLE = _BENCH / "longmemeval" / "data" / "longmemeval_oracle.json"
ITEMS = _HERE / "items.json"
DRAFT = _HERE / "items.draft.json"
RESULTS = _HERE / "results"

SEED = 20260922
PER_TYPE = 12
ABS_PER_TYPE = 2
GENERATOR = "moonshotai/kimi-k3"

TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)

# Candidates for the `lme` form: the published-comparability reference,
# the lenient negative control, and the current frontier and fast tiers
# from each major provider as listed on OpenRouter on 2026-09-22.
LME_JUDGES = (
    "openai/gpt-4o-2024-08-06",
    "openai/gpt-4o-mini",
    "openai/gpt-6-sol",
    "openai/gpt-6-luna",
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5",
    "google/gemini-3.8-flash",
    "deepseek/deepseek-v4.1-flash",
    "z-ai/glm-5.3",
    "x-ai/grok-4.7",
)

# Candidates for the `aml` form: AML's public pipeline reads its judge key
# from SILICONFLOW_API_KEY, a host for open-weight models, so the local
# stand-in is chosen among open-weight judges, plus gpt-4o-mini as the
# lenient control.
AML_JUDGES = (
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-v4-pro-0813",
    "qwen/qwen3.8-flash",
    "z-ai/glm-5.3",
    "openai/gpt-4o-mini",
)

# Models that reject or ignore a reasoning parameter run without one.
NO_REASONING = {"openai/gpt-4o-2024-08-06", "openai/gpt-4o-mini"}

HEDGE_FALLBACK = "I remember we talked about this, but I can't recall the specifics."
REFUSAL_FIXED = (
    "I don't have that information. It isn't something you mentioned in "
    "our previous conversations."
)


# ---------------------------------------------------------------- build


GEN_NORMAL = """You are building a test set for grading answers to questions about a user's past conversations with an assistant. Given a question and its correct answer, write three candidate answers, phrased as an assistant replying to the user (second person, one or two sentences each):

1. "paraphrase": CORRECT. Restate the correct answer in different words. Keep every fact, number, name and list item. Add nothing false.
2. "near_miss": WRONG. Same form and plausibility as the correct answer, but change exactly one key fact (a number, a date, a name, an item, an order) so that it is definitely incorrect. If the answer is a list, drop or replace one item.
3. "hedge": WRONG. A vague reply that mentions the topic of the question but commits to no answer at all.

Question: {question}
Correct answer: {answer}

Return only a JSON object with the keys "paraphrase", "near_miss", "hedge"."""

GEN_ABSTENTION = """You are building a test set for grading answers to questions about a user's past conversations with an assistant. The question below is UNANSWERABLE from the conversation history; the explanation says why. Write two candidate replies, as an assistant replying to the user (second person, one or two sentences each):

1. "refusal": CORRECT. Say the requested information is not available from past conversations. You may mention what the explanation says IS known.
2. "fabricated": WRONG. A confident, specific answer that invents the missing information as if it were remembered.

Question: {question}
Explanation: {answer}

Return only a JSON object with the keys "refusal", "fabricated"."""

GEN_PREFERENCE = """You are building a test set for grading personalized replies. The rubric describes what the user's past conversations reveal and what a good personalized reply looks like. Write three candidate replies to the user's request, as the assistant (three to five sentences each):

1. "personal": CORRECT. A reply that recalls and uses the user's personal information as the rubric describes.
2. "generic": WRONG. A helpful but generic reply that ignores the user's personal information entirely.
3. "contradict": WRONG. A reply that personalizes, but on the wrong preference: it assumes something the rubric says the user would not want.

User request: {question}
Rubric: {answer}

Return only a JSON object with the keys "personal", "generic", "contradict"."""


def _sample(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    picked: list[dict[str, Any]] = []
    for qtype in TYPES:
        pool = [q for q in questions if q["question_type"] == qtype]
        abs_pool = sorted(
            (q for q in pool if q["question_id"].endswith("_abs")),
            key=lambda q: q["question_id"],
        )
        norm_pool = sorted(
            (q for q in pool if not q["question_id"].endswith("_abs")),
            key=lambda q: q["question_id"],
        )
        n_abs = min(ABS_PER_TYPE, len(abs_pool))
        picked += rng.sample(abs_pool, n_abs)
        picked += rng.sample(norm_pool, PER_TYPE - n_abs)
    return picked


def _json_obj(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


async def _generate(client: Client, q: dict[str, Any], kind: str) -> dict[str, Any]:
    template = {
        "normal": GEN_NORMAL,
        "abstention": GEN_ABSTENTION,
        "preference": GEN_PREFERENCE,
    }[kind]
    prompt = template.format(question=q["question"], answer=str(q["answer"]))
    for attempt in range(3):
        out = await client.complete(
            GENERATOR,
            [{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0.0 if attempt == 0 else 0.3,
            reasoning_effort="low",
        )
        obj = _json_obj(out.text)
        if obj is not None:
            return obj
    raise RuntimeError(f"generator returned no JSON for {q['question_id']}")


def _kind(q: dict[str, Any]) -> str:
    if q["question_id"].endswith("_abs"):
        return "abstention"
    if q["question_type"] == "single-session-preference":
        return "preference"
    return "normal"


def _wrap(gold: str) -> str:
    return f"Based on what you told me before: {gold}"


async def build(args: argparse.Namespace) -> None:
    if ITEMS.exists() and not args.force:
        raise SystemExit(
            f"{ITEMS} exists and is the reviewed set; refusing to overwrite"
        )
    questions = json.loads(ORACLE.read_text(encoding="utf-8"))
    sample = _sample(questions)
    async with Client(budget_usd=args.budget, concurrency=8) as client:
        generated = await asyncio.gather(
            *(_generate(client, q, _kind(q)) for q in sample)
        )
        spent = client.spent_usd
    rng = random.Random(SEED + 1)
    by_type_normal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for q in sample:
        if _kind(q) == "normal":
            by_type_normal[q["question_type"]].append(q)
    pref_replies = {
        q["question_id"]: g.get("personal", "")
        for q, g in zip(sample, generated)
        if _kind(q) == "preference"
    }
    records: list[dict[str, Any]] = []
    for q, g in zip(sample, generated):
        kind = _kind(q)
        base = {
            "question_id": q["question_id"],
            "question_type": q["question_type"],
            "abstention": kind == "abstention",
            "question": q["question"],
            "gold": str(q["answer"]),
        }
        others = [
            o
            for o in by_type_normal[q["question_type"]]
            if o["question_id"] != q["question_id"]
        ]
        swap_gold = str(rng.choice(others)["answer"]) if others else None
        answers: list[tuple[str, str, bool]]
        if kind == "normal":
            answers = [
                ("pos_verbatim", _wrap(str(q["answer"])), True),
                ("pos_paraphrase", str(g.get("paraphrase", "")), True),
                ("neg_near_miss", str(g.get("near_miss", "")), False),
                ("neg_hedge", str(g.get("hedge") or HEDGE_FALLBACK), False),
            ]
            if swap_gold is not None:
                answers.append(("neg_swap", _wrap(swap_gold), False))
        elif kind == "abstention":
            answers = [
                ("pos_refusal_fixed", REFUSAL_FIXED, True),
                ("pos_refusal", str(g.get("refusal", "")), True),
                ("neg_fabricated", str(g.get("fabricated", "")), False),
            ]
            if swap_gold is not None:
                answers.append(("neg_swap", _wrap(swap_gold), False))
        else:
            other_ids = [i for i in pref_replies if i != q["question_id"]]
            answers = [
                ("pos_personal", str(g.get("personal", "")), True),
                ("neg_generic", str(g.get("generic", "")), False),
                ("neg_contradict", str(g.get("contradict", "")), False),
            ]
            if other_ids:
                answers.append(("neg_swap", pref_replies[rng.choice(other_ids)], False))
        for variant, text, correct in answers:
            records.append(
                {
                    "item_id": f"{q['question_id']}:{variant}",
                    **base,
                    "variant": variant,
                    "response": text,
                    "correct": correct,
                }
            )
    DRAFT.write_text(
        json.dumps(
            {
                "seed": SEED,
                "generator": GENERATOR,
                "source": "bench/longmemeval/data/longmemeval_oracle.json",
                "reviewed": False,
                "items": records,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(sample)} questions, {len(records)} items -> {DRAFT} (generator spend ${spent:.4f})"
    )


# ---------------------------------------------------------------- judge


async def _judge_one(
    client: Client, model: str, form: str, item: dict[str, Any]
) -> dict[str, Any]:
    if form == "lme":
        prompt = lme_prompt(
            item["question_type"],
            item["question"],
            item["gold"],
            item["response"],
            item["abstention"],
        )
        max_tokens = 400
    else:
        prompt = aml_prompt(item["question"], item["gold"], item["response"])
        max_tokens = 800
    effort = None if model in NO_REASONING else "low"
    out = await client.complete(
        model,
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.0,
        reasoning_effort=effort,
    )
    if not out.text.strip():
        # A reasoning model can spend the whole allowance thinking and return
        # nothing (finish_reason "length"). That is the harness starving the
        # judge, not the judge failing, so it gets one retry with room to
        # finish before its answer is scored.
        out = await client.complete(
            model,
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens * 4,
            temperature=0.0,
            reasoning_effort=effort,
        )
    verdict: bool | None = parse_lme(out.text) if form == "lme" else parse_aml(out.text)
    return {
        "model": model,
        "form": form,
        "item": item["item_id"],
        "verdict": verdict,
        "raw": out.text[:600],
        "cost": out.cost,
    }


async def judge(args: argparse.Namespace) -> None:
    data = json.loads(ITEMS.read_text(encoding="utf-8"))
    if not data.get("reviewed"):
        raise SystemExit("items.json is not marked reviewed")
    items = data["items"]
    jobs: list[tuple[str, str, dict[str, Any]]] = []
    for model in args.lme_judges:
        jobs += [(model, "lme", it) for it in items]
    for model in args.aml_judges:
        jobs += [(model, "aml", it) for it in items]
    async with Client(budget_usd=args.budget, concurrency=args.concurrency) as client:
        rows = await asyncio.gather(
            *(_judge_one(client, m, f, it) for m, f, it in jobs), return_exceptions=True
        )
        spent, calls, hits = client.spent_usd, client.calls, client.cache_hits
    out_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for (m, f, it), r in zip(jobs, rows):
        if isinstance(r, BaseException):
            errors.append(f"{m} {f} {it['item_id']}: {r}")
        else:
            out_rows.append(r)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tag = f"-{args.tag}" if args.tag else ""
    path = RESULTS / f"judgments{tag}-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "provenance": _provenance(),
                "items_sha": _sha(ITEMS),
                "rows": out_rows,
                "errors": errors,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(out_rows)} judgments, {len(errors)} errors, new calls {calls}, cache hits {hits}, spend ${spent:.4f} -> {path}"
    )
    for e in errors[:10]:
        print("  error:", e)


# ---------------------------------------------------------------- score


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _metrics(pairs: list[tuple[bool, bool | None]]) -> dict[str, Any]:
    """pairs of (truth, verdict); an unparseable verdict counts as WRONG."""
    tp = sum(1 for t, v in pairs if t and v is True)
    fn = sum(1 for t, v in pairs if t and v is not True)
    tn = sum(1 for t, v in pairs if not t and v is not True)
    fp = sum(1 for t, v in pairs if not t and v is True)
    tpr = tp / (tp + fn) if tp + fn else None
    tnr = tn / (tn + fp) if tn + fp else None
    bal = round((tpr + tnr) / 2, 4) if tpr is not None and tnr is not None else None
    return {
        "n": len(pairs),
        "balanced_accuracy": bal,
        "false_accept": _rate(fp, fp + tn),
        "false_reject": _rate(fn, fn + tp),
        "unparseable": sum(1 for _, v in pairs if v is None),
    }


def score(args: argparse.Namespace) -> None:
    items = {
        it["item_id"]: it
        for it in json.loads(ITEMS.read_text(encoding="utf-8"))["items"]
    }
    path = (
        Path(args.judgments).resolve()
        if args.judgments
        else sorted(RESULTS.glob("judgments-*.json"))[-1]
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in data["rows"]:
        grouped[(r["form"], r["model"])].append(r)
    table: dict[str, Any] = {}
    for (form, model), rows in sorted(grouped.items()):
        pairs = [(items[r["item"]]["correct"], r["verdict"]) for r in rows]
        by_type: dict[str, Any] = {}
        for qtype in TYPES:
            sub = [
                (items[r["item"]]["correct"], r["verdict"])
                for r in rows
                if items[r["item"]]["question_type"] == qtype
            ]
            by_type[qtype] = _metrics(sub)
        by_variant: dict[str, Any] = {}
        for variant in sorted({items[r["item"]]["variant"] for r in rows}):
            sub = [r["verdict"] for r in rows if items[r["item"]]["variant"] == variant]
            truth = items[
                next(r["item"] for r in rows if items[r["item"]]["variant"] == variant)
            ]["correct"]
            accepted = sum(1 for v in sub if v is True)
            by_variant[variant] = {"n": len(sub), "accepted": accepted, "truth": truth}
        non_pref = [
            (items[r["item"]]["correct"], r["verdict"])
            for r in rows
            if items[r["item"]]["question_type"] != "single-session-preference"
        ]
        table.setdefault(form, {})[model] = {
            "all": _metrics(pairs),
            "non_preference": _metrics(non_pref),
            "by_type": by_type,
            "by_variant": by_variant,
            "cost_usd": round(sum(r["cost"] for r in rows), 4),
        }
    out = {
        "judgments": str(path.relative_to(_BENCH.parent)),
        "items_sha": data.get("items_sha"),
        "table": table,
    }
    for form, models in table.items():
        print(f"\n== form {form}")
        ranked = sorted(
            models.items(),
            key=lambda kv: (
                -(kv[1]["all"]["balanced_accuracy"] or 0),
                kv[1]["all"]["false_accept"] or 0,
            ),
        )
        for model, m in ranked:
            a = m["all"]
            worst = min(
                TYPES,
                key=lambda t: (
                    m["by_type"][t]["balanced_accuracy"]
                    if m["by_type"][t]["balanced_accuracy"] is not None
                    else 2
                ),
            )
            print(
                f"{model:32s} bal {a['balanced_accuracy']:.4f}  FA {a['false_accept']:.3f}  FR {a['false_reject']:.3f}"
                f"  unparse {a['unparseable']:3d}  nonpref {m['non_preference']['balanced_accuracy']:.4f}"
                f"  worst {worst} {m['by_type'][worst]['balanced_accuracy']}  ${m['cost_usd']:.3f}"
            )
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
        print(f"\n-> {args.out}")


# ---------------------------------------------------------------- real answers


async def real(args: argparse.Namespace) -> None:
    """Stage 2: grade REAL reader answers with every candidate, then list the
    items the judges disagree on for adjudication by hand.

    The synthetic set separates a broken judge from a working one; it
    cannot separate two working ones, because its wrong answers are
    unambiguous by construction. Real answers carry the hard cases the
    literature warns about (partial lists, rounding, a rubric read two
    ways), so the final choice is made on agreement with adjudicated
    labels over those.
    """
    oracle = {
        q["question_id"]: q for q in json.loads(ORACLE.read_text(encoding="utf-8"))
    }
    items: dict[str, dict[str, Any]] = {}
    for path in args.answers:
        for row in json.loads(Path(path).read_text(encoding="utf-8"))["rows"]:
            q = oracle[row["question_id"]]
            key = f"{row['question_id']}:{hashlib.sha256(row['generated'].encode('utf-8')).hexdigest()[:12]}"
            items[key] = {
                "item_id": key,
                "question_id": row["question_id"],
                "question_type": q["question_type"],
                "abstention": row["question_id"].endswith("_abs"),
                "question": q["question"],
                "gold": str(q["answer"]),
                "response": row["generated"],
            }
    rows_in = list(items.values())
    jobs = [(m, "lme", it) for m in args.lme_judges for it in rows_in]
    jobs += [(m, "aml", it) for m in args.aml_judges for it in rows_in]
    async with Client(budget_usd=args.budget, concurrency=args.concurrency) as client:
        got = await asyncio.gather(
            *(_judge_one(client, m, f, it) for m, f, it in jobs), return_exceptions=True
        )
        spent = client.spent_usd
    verdicts: dict[str, dict[str, dict[str, bool | None]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    errors = []
    for (m, f, it), r in zip(jobs, got):
        if isinstance(r, BaseException):
            errors.append(f"{m} {f} {it['item_id']}: {r}")
        else:
            verdicts[it["item_id"]][f][m] = r["verdict"]
    split = [
        it
        for it in rows_in
        if len({v for f in verdicts[it["item_id"]].values() for v in f.values()}) > 1
    ]
    accepted = {
        f: {
            m: sum(1 for it in rows_in if verdicts[it["item_id"]][f].get(m) is True)
            for m in (args.lme_judges if f == "lme" else args.aml_judges)
        }
        for f in ("lme", "aml")
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tag = f"-{args.tag}" if args.tag else ""
    out = RESULTS / f"real-judgments{tag}-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "provenance": _provenance(),
                "answers": [str(Path(a)) for a in args.answers],
                "summary": {
                    "n_items": len(rows_in),
                    "n_disputed": len(split),
                    "accepted": accepted,
                },
                "items": rows_in,
                "verdicts": verdicts,
                "errors": errors,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(rows_in)} real answers, {len(split)} with any judge disagreement, {len(errors)} errors, spend ${spent:.4f} -> {out}"
    )


def score_real(args: argparse.Namespace) -> None:
    """Agreement of every judge with the adjudicated labels on real answers.

    The labels file carries one boolean per form for every item: the
    unanimous verdict where the judges agreed, and a hand ruling with its
    reason where they split.
    """
    data = json.loads(Path(args.judgments).read_text(encoding="utf-8"))
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))["labels"]
    verdicts = data["verdicts"]
    table: dict[str, dict[str, dict[str, int]]] = {}
    for form in ("lme", "aml"):
        models = sorted(
            {m for it in data["items"] for m in verdicts[it["item_id"]].get(form, {})}
        )
        for m in models:
            agree = fa = fr = 0
            for it in data["items"]:
                truth = labels[it["item_id"]][form]
                said = verdicts[it["item_id"]].get(form, {}).get(m) is True
                agree += said == truth
                fa += said and not truth
                fr += truth and not said
            table.setdefault(form, {})[m] = {
                "agree": agree,
                "n": len(data["items"]),
                "false_accept": fa,
                "false_reject": fr,
            }
        if form not in table:
            continue
        print(f"-- form {form}")
        for m, row in sorted(table[form].items(), key=lambda kv: -kv[1]["agree"]):
            print(
                f"   {m:32s} {row['agree']}/{row['n']}  FA {row['false_accept']}  FR {row['false_reject']}"
            )
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"judgments": args.judgments, "labels": args.labels, "table": table},
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------- helpers


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


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

    return {
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "tree_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def main() -> None:
    root = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = root.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--budget", type=float, default=1.5)
    b.add_argument("--force", action="store_true")
    j = sub.add_parser("judge")
    j.add_argument("--budget", type=float, default=6.0)
    j.add_argument("--concurrency", type=int, default=24)
    j.add_argument("--lme-judges", nargs="*", default=list(LME_JUDGES))
    j.add_argument("--aml-judges", nargs="*", default=list(AML_JUDGES))
    j.add_argument("--tag", default="", help="suffix for the output file name")
    r = sub.add_parser("real")
    r.add_argument("answers", nargs="+")
    r.add_argument("--budget", type=float, default=2.0)
    r.add_argument("--concurrency", type=int, default=24)
    r.add_argument("--lme-judges", nargs="*", default=list(LME_JUDGES))
    r.add_argument("--aml-judges", nargs="*", default=list(AML_JUDGES))
    r.add_argument("--tag", default="", help="suffix for the output file name")
    sr = sub.add_parser("score-real")
    sr.add_argument("judgments")
    sr.add_argument("labels")
    sr.add_argument("--out", default=None)
    s = sub.add_parser("score")
    s.add_argument("--judgments", default=None)
    s.add_argument("--out", default=None)
    args = root.parse_args()
    if args.cmd == "build":
        asyncio.run(build(args))
    elif args.cmd == "judge":
        asyncio.run(judge(args))
    elif args.cmd == "real":
        asyncio.run(real(args))
    elif args.cmd == "score-real":
        score_real(args)
    else:
        score(args)


if __name__ == "__main__":
    main()
