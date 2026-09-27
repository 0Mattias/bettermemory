"""LongMemEval-S holdout 350 read by the chat model under the S0 arm.

UNIT S2 (declared 2026-09-27, memory 01M3J07XBSD8S77HJDPDJGMCVB). S1
(memory 01M3HZ4EK75MN9WQRVAQ6BN61M) read the dev 150 under the S0 arm and
got 148 of 150 in one blinded pass against L1's 138. The dev chose the
arm, so the holdout is the number that counts: the 350 questions of
aml_run.split that S0 and S1 never served, read once here and graded on
LongMemEval's own prompts by both official judges, google/gemini-3.8-flash
(the judge of record) and openai/gpt-4o-2024-08-06 (the standard). S1's
dev answers are judged beside them, so all 500 get an official number.

What differs from S1: the split; a fresh store built from the 350
holdout haystacks and checked by C0 parity against L1's holdout prompts
before the arm is served; the grade, by the two official judges through
L1's own price and judge steps, in place of the blinded Claude grader;
the stated file size, set to the holdout's measured maximum by S1's rule.
Everything else is S1's: the arm and its budget, batches of 4, the reader
wording, LongMemEval's reading prompt with the question date, the
cross-evidence batching rule, the wrap and the transcript audit.

STEPS, each a subcommand over one work directory:

  ingest    the 350 holdout haystacks into the fresh store
  prompts   C0 re-served and checked against L1's holdout prompts, the
            arm served, answer-turn coverage under both, fill, wrap, batch
  collect   the readers' answer files into the hypothesis file (L1's step)
  audit     every reader's transcript (S1's step)
  judgeset  S1's dev answers and S2's holdout answers into one directory,
            which L1's `price` and `judge` steps then grade
  report    the artifact

Every process that opens a store runs with BETTERMEMORY_KEYS_DIR set to
the S2 receipts' keys.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
for _path in (_BENCH, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import l1  # noqa: E402
import s0  # noqa: E402
import s1  # noqa: E402
from aml import run as aml_run  # noqa: E402
from aml.service import MemoryService  # noqa: E402

ARM = s1.ARM
BUDGET = s1.BUDGET
BATCH_SIZE = s1.BATCH_SIZE
STORE = aml_run.STORES / "coverage-s2-holdout"
S1_WORK = l1.RECEIPTS / "s1-2026-09-27" / "work"
S1_ARTIFACT = s1.OUT
L1_ARTIFACT = s0.L1_ARTIFACT
L1_PROMPTS = s0.L1_PROMPTS
OUT = l1.RESULTS / "s2-holdout-2026-09-27.json"
N_HOLDOUT = 350
TOKEN_STOP = 26_000_000
JUDGES = {"record": l1.JUDGE_OF_RECORD, "standard": l1.STANDARD_JUDGE}
# The best standard-judge LongMemEval-S result found (memory
# 01M3GEKE13F2BGTN47QW3CER2A): Mastra, gpt-5-mini reading, 468 of 500.
MASTRA_STANDARD = 468


def service_for(root: Path) -> MemoryService:
    """S1's service: C0's options pointed at the arm at 180,000 characters."""
    return s1.service_for(root)


def reader_instructions(
    batch_file: Path, answers_file: Path, chars: int, lines: int
) -> str:
    """S1's wording, unchanged; only the measured file size differs."""
    return s1.reader_instructions(batch_file, answers_file, chars, lines)


def holdout_instances(corpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _, holdout = aml_run.split(corpus)
    keep = set(holdout)
    return [inst for inst in corpus if inst["question_id"] in keep]


def check_split(holdout_ids: list[str], l1_rows: dict[str, Any]) -> None:
    """The holdout must be L1's, question for question, so no dev question
    is served and every holdout question is."""
    l1_holdout = {q for q, r in l1_rows.items() if r["split"] == "holdout"}
    if len(set(holdout_ids)) != len(holdout_ids) or set(holdout_ids) != l1_holdout:
        raise SystemExit("the holdout differs from L1's")


def measure(root: Path, inst: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """One holdout question from the fresh store: C0 re-served as L1 served
    it, for the parity check and C0's coverage, then the arm, its coverage
    and its reading prompt. Its own service, so questions run in parallel;
    the options are read at search time, so one service serves both."""
    service = l1.service_for(root)
    positions = s0.round_positions(service, inst)
    c0_hits = s0._served(service, inst, s0.C0, s0.BUDGET)
    c0 = s0.coverage(inst, c0_hits, positions)
    c0_context = l1.served_context(c0_hits)
    s0.configure(service, ARM, BUDGET)
    row, prompt = s1.build(service, inst)
    row.update(
        split="holdout",
        c0_covered=c0["covered"],
        c0_context_chars=len(c0_context),
        c0_context_sha256=l1._sha(c0_context),
    )
    return row, prompt


def c0_parity(
    rows: list[dict[str, Any]], l1_rows: dict[str, Any], prompts_dir: Path
) -> list[str]:
    """Questions whose C0 context from the fresh store differs from the
    context in the prompt L1 read. Refuses a prompt file that is not the
    one the L1 artifact recorded."""
    differ: list[str] = []
    for r in rows:
        qid = r["question_id"]
        path = prompts_dir / f"{qid}.txt"
        prompt = path.read_bytes().decode("utf-8")
        if l1._sha(prompt) != l1_rows[qid]["prompt_sha256"]:
            raise SystemExit(f"{path} is not the prompt L1 read")
        if l1._sha(l1.reading_context(prompt)) != r["c0_context_sha256"]:
            differ.append(qid)
    return differ


def projected_tokens(counted: int, questions: int) -> int:
    """Counted reader tokens so far, scaled to the 350."""
    return counted * N_HOLDOUT // questions


def over_stop(tokens: int) -> bool:
    return tokens > TOKEN_STOP


def _l1_rows(path: Path) -> dict[str, Any]:
    return {r["question_id"]: r for r in l1._load_json(path)["rows"]}


def _holdout(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not os.environ.get("BETTERMEMORY_KEYS_DIR"):
        raise SystemExit("set BETTERMEMORY_KEYS_DIR to the S2 receipts' keys")
    holdout = holdout_instances(l1._corpus())
    l1_rows = _l1_rows(Path(args.l1_artifact))
    check_split([inst["question_id"] for inst in holdout], l1_rows)
    return holdout, l1_rows


# ---------------------------------------------------------------- the store


def cmd_ingest(args: argparse.Namespace) -> None:
    holdout, _ = _holdout(args)
    root = Path(args.store)
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty; S2 builds its own store fresh")
    t0 = time.time()
    service = l1.service_for(root)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda inst: aml_run.ingest_and_search(service, inst), holdout))
    print(
        json.dumps(
            {
                "ingested": len(holdout),
                "seconds": round(time.time() - t0, 1),
                "store": str(root),
            }
        )
    )


# ---------------------------------------------------------------- the prompts


def cmd_prompts(args: argparse.Namespace) -> None:
    holdout, l1_rows = _holdout(args)
    root = Path(args.store)
    work = Path(args.work).resolve()
    for sub in ("prompts", "prompts_wrapped", "batches", "readers", "answers"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        built = list(pool.map(lambda inst: measure(root, inst), holdout))
    rows = [row for row, _ in built]
    differ = c0_parity(rows, l1_rows, Path(args.l1_prompts))
    if differ:
        l1._write_json(
            work / "stopped.json",
            {"unit": "S2", "stopped_at": "S2-P1", "differ": differ, "rows": rows},
        )
        raise SystemExit(f"S2-P1: {len(differ)} C0 contexts differ from L1's: {differ}")
    for row, prompt in built:
        qid = row["question_id"]
        wrapped, inserted = l1.wrap_for_read(prompt)
        (work / "prompts" / f"{qid}.txt").write_bytes(prompt.encode("utf-8"))
        (work / "prompts_wrapped" / f"{qid}.txt").write_bytes(wrapped.encode("utf-8"))
        row.update(
            wrap_inserted=inserted,
            wrapped_lines=l1.line_count(wrapped),
            longest_line=max(len(x) for x in wrapped.split("\n")),
        )
    chars = s1.round_up(max(r["prompt_chars"] for r in rows), 1_000)
    lines = s1.round_up(max(r["wrapped_lines"] for r in rows), 100)
    pairs = l1.cross_evidence(holdout)
    batches = l1.make_batches([r["question_id"] for r in rows], pairs, size=BATCH_SIZE)
    instructions: list[str] = []
    for i, batch in enumerate(batches):
        batch_file = work / "batches" / f"batch_{i:02d}.txt"
        answers_file = work / "answers" / f"batch_{i:02d}.json"
        listing = "".join(f"{work / 'prompts_wrapped' / q}.txt\n" for q in batch)
        batch_file.write_text(listing, encoding="utf-8")
        text = reader_instructions(batch_file, answers_file, chars, lines)
        (work / "readers" / f"reader_{i:02d}.txt").write_text(text, encoding="utf-8")
        instructions.append(text)
    labeled = [r for r in rows if r["answer_turns"]]
    coverage = {
        "labeled": len(labeled),
        "c0_covered": sum(r["c0_covered"] for r in labeled),
        "arm_covered": sum(r["covered"] for r in labeled),
    }
    # the reader tokens S1 counted per prompt character, carried to the
    # holdout's prompts: the readers' tokens are mostly the files they read
    s1_rows = l1._load_json(Path(args.s1_work) / "meta.json")["rows"]
    s1_tokens = l1._load_json(Path(args.s1_artifact))["tokens"]["readers_counted"]
    per_char = s1_tokens / sum(r["prompt_chars"] for r in s1_rows)
    projection = round(per_char * sum(r["prompt_chars"] for r in rows))
    l1._write_json(
        work / "meta.json",
        {
            "unit": "S2",
            "arm": ARM.name,
            "budget": BUDGET,
            "batch_size": BATCH_SIZE,
            "stated_chars": chars,
            "stated_lines": lines,
            "reader_template_sha256": l1._sha(s1.READER_TEMPLATE),
            "parity": {"n": len(rows), "equal": len(rows) - len(differ)},
            "coverage": coverage,
            "projected_reader_tokens": projection,
            "cross_evidence_pairs": len(pairs),
            "batches": batches,
            "instructions": instructions,
            "rows": rows,
        },
    )
    print(
        json.dumps(
            {
                "questions": len(rows),
                "batches": len(batches),
                "parity": len(rows) - len(differ),
                "coverage": coverage,
                "stated_chars": chars,
                "stated_lines": lines,
                "prompt_chars_max": max(r["prompt_chars"] for r in rows),
                "prompt_chars_mean": round(
                    sum(r["prompt_chars"] for r in rows) / len(rows)
                ),
                "projected_reader_tokens": projection,
                "seconds": round(time.time() - t0, 1),
            }
        )
    )


# ---------------------------------------------------------------- the grade


def judge_set(
    dev_rows: list[dict[str, Any]],
    s1_answers: dict[str, str],
    s1_recorded: dict[str, str],
    holdout_rows: list[dict[str, Any]],
    s2_answers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The 500 judge rows, S1's dev answers then S2's holdout answers, each
    row carrying what l1.judge_item reads and its split. S1's answers must
    be the ones the S1 artifact recorded."""
    dev_ids = [r["question_id"] for r in dev_rows]
    holdout_ids = [r["question_id"] for r in holdout_rows]
    if set(dev_ids) & set(holdout_ids):
        raise SystemExit("a question sits in both splits")
    if set(s1_answers) != set(dev_ids) or any(
        s1_answers[q] != s1_recorded.get(q) for q in dev_ids
    ):
        raise SystemExit("S1's dev answers differ from the S1 artifact's")
    if set(s2_answers) != set(holdout_ids):
        raise SystemExit("S2's answers do not cover the holdout exactly")
    keep = ("question_id", "question_type", "question", "gold", "abstention")
    rows = [{**{k: r[k] for k in keep}, "split": "dev"} for r in dev_rows] + [
        {**{k: r[k] for k in keep}, "split": "holdout"} for r in holdout_rows
    ]
    answers = {q: s1_answers[q] for q in dev_ids} | {
        q: s2_answers[q] for q in holdout_ids
    }
    return rows, answers


def _answers(path: Path) -> dict[str, str]:
    return {h["question_id"]: h["hypothesis"] for h in l1._jsonl(path)}


def cmd_judgeset(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    s1_work = Path(args.s1_work)
    rows, answers = judge_set(
        l1._load_json(s1_work / "meta.json")["rows"],
        _answers(s1_work / "hypotheses.jsonl"),
        {
            r["question_id"]: r["hypothesis"]
            for r in l1._load_json(Path(args.s1_artifact))["rows"]
        },
        l1._load_json(work / "meta.json")["rows"],
        _answers(work / "hypotheses.jsonl"),
    )
    judge = work / "judge"
    judge.mkdir(parents=True, exist_ok=True)
    l1._write_json(judge / "meta.json", {"unit": "S2", "rows": rows})
    with (judge / "hypotheses.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            qid = r["question_id"]
            fh.write(
                json.dumps(
                    {"question_id": qid, "hypothesis": answers[qid]}, ensure_ascii=False
                )
                + "\n"
            )
    print(
        json.dumps(
            {
                "rows": len(rows),
                "dev": sum(r["split"] == "dev" for r in rows),
                "holdout": sum(r["split"] == "holdout" for r in rows),
                "judge_dir": str(judge),
            }
        )
    )


def score(
    rows: list[dict[str, Any]], verdicts: dict[str, bool | None]
) -> dict[str, Any]:
    """The official plain mean over `rows` (a missing or unparsed verdict is
    wrong, as evaluate_qa.py counts a reply without "yes"), the per-type
    scores, their mean (the average some vendors headline) and the
    abstention questions apart."""
    ok = {r["question_id"]: verdicts.get(r["question_id"]) is True for r in rows}
    by_type: dict[str, dict[str, Any]] = {}
    for t in sorted({r["question_type"] for r in rows}):
        keep = [ok[r["question_id"]] for r in rows if r["question_type"] == t]
        by_type[t] = {
            "n": len(keep),
            "correct": sum(keep),
            "accuracy": round(sum(keep) / len(keep), 4),
        }
    abstention = [ok[r["question_id"]] for r in rows if r["abstention"]]
    n, correct = len(rows), sum(ok.values())
    return {
        "n": n,
        "correct": correct,
        "overall": round(correct / n, 4) if n else None,
        "category_mean": round(
            sum(v["accuracy"] for v in by_type.values()) / len(by_type), 4
        )
        if by_type
        else None,
        "by_type": by_type,
        "abstention": {"n": len(abstention), "correct": sum(abstention)},
    }


def paired(
    l1_verdicts: dict[str, Any], s2_verdicts: dict[str, Any], qids: list[str]
) -> dict[str, Any]:
    """L1's answers against S2's on the same questions under one judge; a
    missing verdict counts as wrong."""
    l1_ok = {q: l1_verdicts.get(q) is True for q in qids}
    s2_ok = {q: s2_verdicts.get(q) is True for q in qids}
    recovered = sorted(q for q in qids if s2_ok[q] and not l1_ok[q])
    regressed = sorted(q for q in qids if l1_ok[q] and not s2_ok[q])
    return {
        "n": len(qids),
        "l1_correct": sum(l1_ok.values()),
        "s2_correct": sum(s2_ok.values()),
        "both": sum(l1_ok[q] and s2_ok[q] for q in qids),
        "neither": sum(not l1_ok[q] and not s2_ok[q] for q in qids),
        "l1_misses": sum(not v for v in l1_ok.values()),
        "recovered": recovered,
        "regressed": regressed,
        "gain": len(recovered) - len(regressed),
        "mcnemar_exact_p": round(s1.mcnemar_exact(len(regressed), len(recovered)), 6),
    }


def predictions(n: dict[str, Any]) -> list[dict[str, Any]]:
    """The declaration's nine predictions graded on the run's numbers."""
    v = l1._verdict
    std, rec = n["holdout_standard"], n["holdout_record"]
    return [
        {
            "id": "S2-P1",
            "claim": "C0 from the fresh store equals L1's holdout contexts, 350 of 350",
            "got": n["parity"],
            "verdict": v(n["parity"] == N_HOLDOUT, n["parity"] != N_HOLDOUT),
        },
        {
            "id": "S2-P2",
            "claim": "the arm covers at least 0.97 of the labeled holdout; MISSED under 0.95",
            "got": n["coverage_rate"],
            "verdict": v(n["coverage_rate"] >= 0.97, n["coverage_rate"] < 0.95),
        },
        {
            "id": "S2-P3",
            "claim": "holdout 342 (337 to 347) under the standard, 343 (338 to 348) "
            "under the judge of record; MISSED under 332 or under 333",
            "got": {"standard": std, "record": rec},
            "verdict": v(
                337 <= std <= 347 and 338 <= rec <= 348, std < 332 or rec < 333
            ),
        },
        {
            "id": "S2-P4",
            "claim": "under the standard, at least 11 of L1's holdout misses right, at "
            "most 4 regressions, McNemar p under 0.01; MISSED under 7, over 6 or "
            "p of 0.05 or more",
            "got": {
                "recovered": n["recovered"],
                "regressed": n["regressed"],
                "p": n["p"],
            },
            "verdict": v(
                n["recovered"] >= 11 and n["regressed"] <= 4 and n["p"] < 0.01,
                n["recovered"] < 7 or n["regressed"] > 6 or n["p"] >= 0.05,
            ),
        },
        {
            "id": "S2-P5",
            "claim": "S1's dev answers 146 (144 to 149) under the standard; MISSED at "
            "143 or fewer",
            "got": n["dev_standard"],
            "verdict": v(144 <= n["dev_standard"] <= 149, n["dev_standard"] <= 143),
        },
        {
            "id": "S2-P6",
            "claim": "all 500 at 488 (482 to 494) under the standard; MISSED under 478",
            "got": n["all_standard"],
            "verdict": v(482 <= n["all_standard"] <= 494, n["all_standard"] < 478),
        },
        {
            "id": "S2-P7",
            "claim": "the judges agree on at least 0.98 of the 500 and 21 of 21 "
            "holdout abstentions hold; MISSED under 0.96 or under 19",
            "got": {"agreement": n["agreement"], "abstentions": n["abstentions"]},
            "verdict": v(
                n["agreement"] >= 0.98 and n["abstentions"] == 21,
                n["agreement"] < 0.96 or n["abstentions"] < 19,
            ),
        },
        {
            "id": "S2-P8",
            "claim": "every counted batch clean, at most 3 attempts stopped by a "
            "safeguard",
            "got": {"audit_ok": n["audit_ok"], "stops": n["stops"]},
            "verdict": v(
                n["audit_ok"] and n["stops"] <= 3,
                not n["audit_ok"] or n["stops"] > 3,
            ),
        },
        {
            "id": "S2-P9",
            "claim": "readers 18M to 23M counted tokens (stop 26M), judges under $1; "
            "MISSED over 26M or over $2",
            "got": {"reader_tokens": n["reader_tokens"], "judge_usd": n["judge_usd"]},
            "verdict": v(
                18_000_000 <= n["reader_tokens"] <= 23_000_000 and n["judge_usd"] < 1,
                over_stop(n["reader_tokens"]) or n["judge_usd"] > 2,
            ),
        },
    ]


def cmd_report(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    meta = l1._load_json(work / "meta.json")
    rows = meta["rows"]
    holdout_ids = [r["question_id"] for r in rows]
    judge_dir = work / "judge"
    all_rows = l1._load_json(judge_dir / "meta.json")["rows"]
    dev_rows = [r for r in all_rows if r["split"] == "dev"]
    dev_ids = [r["question_id"] for r in dev_rows]
    answers = _answers(judge_dir / "hypotheses.jsonl")
    judged = l1._load_json(judge_dir / "judgments.json")
    price = l1._load_json(judge_dir / "price.json")
    verdicts = {
        name: {q: r["verdict"] for q, r in judged[name]["rows"].items()}
        for name in JUDGES
    }
    l1_rows = _l1_rows(Path(args.l1_artifact))
    l1_verdicts = {
        name: {q: r["verdicts"][model] for q, r in l1_rows.items()}
        for name, model in JUDGES.items()
    }
    s1_claude = {
        r["question_id"]: r["verdicts"]["s1"]
        for r in l1._load_json(Path(args.s1_artifact))["rows"]
    }
    readers = l1._load_json(work / "audit-readers.json")["summary"]

    holdout = {name: score(rows, verdicts[name]) for name in JUDGES}
    l1_holdout = {name: score(rows, l1_verdicts[name]) for name in JUDGES}
    dev = {name: score(dev_rows, verdicts[name]) for name in JUDGES}
    everything = {name: score(all_rows, verdicts[name]) for name in JUDGES}
    pair_holdout = {
        name: paired(l1_verdicts[name], verdicts[name], holdout_ids) for name in JUDGES
    }
    pair_dev = {
        name: paired(l1_verdicts[name], verdicts[name], dev_ids) for name in JUDGES
    }
    differ = [
        r["question_id"]
        for r in all_rows
        if (verdicts["record"].get(r["question_id"]) is True)
        != (verdicts["standard"].get(r["question_id"]) is True)
    ]
    agreement = round((len(all_rows) - len(differ)) / len(all_rows), 4)
    labeled = [r for r in rows if r["answer_turns"]]
    c0_covered = {r["question_id"] for r in labeled if r["c0_covered"]}
    arm_covered = {r["question_id"] for r in labeled if r["covered"]}
    uncovered = sorted({r["question_id"] for r in labeled} - arm_covered)
    std = verdicts["standard"]
    coverage = {
        "labeled": len(labeled),
        "c0_covered": len(c0_covered),
        "c0_rate": round(len(c0_covered) / len(labeled), 4),
        "arm_covered": len(arm_covered),
        "arm_rate": round(len(arm_covered) / len(labeled), 4),
        "recovered_vs_c0": sorted(arm_covered - c0_covered),
        "regressed_vs_c0": sorted(c0_covered - arm_covered),
        "standard_correct_when_covered": sum(std.get(q) is True for q in arm_covered),
        "uncovered": {
            "ids": uncovered,
            "standard_correct": sum(std.get(q) is True for q in uncovered),
        },
    }
    tokens_readers = s1.counted_tokens(readers["usage"])
    tokens_all = s1.counted_tokens(readers["usage_all_attempts"])
    judge_usd = round(
        judged["record"]["spent_usd"] + judged["standard"]["spent_usd"], 6
    )
    audit_ok = (
        readers["with_answers"] == readers["expected"]
        and readers["clean"] == readers["expected"]
        and readers["violations"] == 0
        and readers["files_not_read_to_end"] == 0
        and readers["flagged_attachments"] == 0
        and readers["models"] == [l1.READER_MODEL]
    )
    graded = predictions(
        {
            "parity": meta["parity"]["equal"],
            "coverage_rate": coverage["arm_rate"],
            "holdout_standard": holdout["standard"]["correct"],
            "holdout_record": holdout["record"]["correct"],
            "recovered": len(pair_holdout["standard"]["recovered"]),
            "regressed": len(pair_holdout["standard"]["regressed"]),
            "p": pair_holdout["standard"]["mcnemar_exact_p"],
            "dev_standard": dev["standard"]["correct"],
            "all_standard": everything["standard"]["correct"],
            "agreement": agreement,
            "abstentions": holdout["standard"]["abstention"]["correct"],
            "audit_ok": audit_ok,
            "stops": readers["attempts_stopped_by_safeguard"],
            "reader_tokens": tokens_readers,
            "judge_usd": judge_usd,
        }
    )
    by_id = {r["question_id"]: r for r in all_rows}
    l1._write_json(
        Path(args.out),
        {
            "unit": "S2",
            "declaration": "memory 01M3J07XBSD8S77HJDPDJGMCVB",
            "provenance": l1._provenance(),
            "protocol": {
                "dataset": {
                    "file": str(l1.CORPUS.relative_to(_BENCH.parent)),
                    "sha256": l1.CORPUS_SHA256,
                },
                "split": "the holdout 350 of aml_run.split (SPLIT_SEED "
                + str(aml_run.SPLIT_SEED)
                + "), never served by S0 or S1; S1's dev 150 answers judged beside it",
                "memory": {
                    "arm": ARM.name,
                    "budget_chars": BUDGET,
                    "store": "bench/aml/.stores/coverage-s2-holdout, built fresh "
                    "from the 350 holdout haystacks",
                    "service": "bench/aml/service.py MemoryService, C0 options with "
                    "the arm's order, fill, trim and a 200-round pool",
                },
                "reading_prompt": "L1's: run_generation.py con at "
                + l1.UPSTREAM
                + ", question dates",
                "reader": {
                    "model": l1.READER_MODEL,
                    "harness": "Claude Code subagents (general-purpose)",
                    "batch_size": BATCH_SIZE,
                    "batches": len(meta["batches"]),
                    "template_sha256": meta["reader_template_sha256"],
                    "stated_chars": meta["stated_chars"],
                    "stated_lines": meta["stated_lines"],
                },
                "judges": {
                    "record": l1.JUDGE_OF_RECORD
                    + ", called as the 2026-09-22 validation called it (max_tokens "
                    "400, temperature 0, reasoning effort low, one retry at 1,600)",
                    "standard": l1.STANDARD_JUDGE
                    + ", called as evaluate_qa.py calls it (temperature 0, "
                    'max_tokens 10, "yes" in the lowercased reply)',
                    "prompts": "bench/judge/prompts.py lme_prompt, LongMemEval's "
                    "evaluate_qa.py prompts at " + l1.UPSTREAM,
                },
            },
            "parity": meta["parity"],
            "coverage": coverage,
            "summary": {
                "holdout": holdout,
                "l1_holdout": l1_holdout,
                "paired_holdout": pair_holdout,
                "dev_s1": dev,
                "paired_dev": pair_dev,
                "dev_s1_claude_grader": sum(bool(s1_claude[q]) for q in dev_ids),
                "all_500": everything,
                "agreement": {
                    "n": len(all_rows),
                    "agree": len(all_rows) - len(differ),
                    "rate": agreement,
                },
                "published": {
                    "mastra_standard": {
                        "correct": MASTRA_STANDARD,
                        "n": 500,
                        "overall": MASTRA_STANDARD / 500,
                        "reader": "gpt-5-mini",
                        "source": "memory 01M3GEKE13F2BGTN47QW3CER2A",
                    }
                },
            },
            "disagreements": [
                {
                    "question_id": q,
                    "split": by_id[q]["split"],
                    "question_type": by_id[q]["question_type"],
                    **{JUDGES[name]: verdicts[name].get(q) for name in JUDGES},
                    "question": by_id[q]["question"],
                    "gold": by_id[q]["gold"],
                    "response": answers[q],
                }
                for q in differ
            ],
            "audit": {"readers": readers},
            "cost": {
                "judges_usd": {
                    JUDGES[name]: judged[name]["spent_usd"] for name in JUDGES
                },
                "judge_calls": {JUDGES[name]: judged[name]["calls"] for name in JUDGES},
                "judge_errors": {
                    JUDGES[name]: judged[name]["errors"] for name in JUDGES
                },
                "price_check": {
                    k: val for k, val in price.items() if k not in l1.ACCOUNT_FIELDS
                },
                "reader_tokens": {
                    "counted": tokens_readers,
                    "all_attempts": tokens_all,
                    "projected_at_prompts": meta["projected_reader_tokens"],
                    "stop": TOKEN_STOP,
                },
            },
            "predictions": graded,
            "rows": [
                {
                    **{
                        k: r[k]
                        for k in (
                            "question_id",
                            "question_type",
                            "abstention",
                            "n_hits",
                            "context_chars",
                            "prompt_sha256",
                            "answer_turns",
                            "covered",
                            "c0_covered",
                            "c0_context_chars",
                        )
                    },
                    "verdicts": {
                        "s2": {
                            JUDGES[name]: verdicts[name].get(r["question_id"])
                            for name in JUDGES
                        },
                        "l1": {
                            JUDGES[name]: l1_verdicts[name][r["question_id"]]
                            for name in JUDGES
                        },
                    },
                    "hypothesis": answers[r["question_id"]],
                }
                for r in rows
            ],
            "dev_rows": [
                {
                    "question_id": q,
                    "question_type": by_id[q]["question_type"],
                    "verdicts": {
                        "s1": {JUDGES[name]: verdicts[name].get(q) for name in JUDGES},
                        "s1_claude_grader": s1_claude[q],
                        "l1": {JUDGES[name]: l1_verdicts[name][q] for name in JUDGES},
                    },
                }
                for q in dev_ids
            ],
        },
    )
    print(
        json.dumps(
            {
                "holdout": {n: holdout[n]["correct"] for n in JUDGES},
                "l1_holdout": {n: l1_holdout[n]["correct"] for n in JUDGES},
                "dev_s1": {n: dev[n]["correct"] for n in JUDGES},
                "all_500": {
                    n: (everything[n]["correct"], everything[n]["category_mean"])
                    for n in JUDGES
                },
                "paired_holdout_standard": {
                    k: pair_holdout["standard"][k]
                    for k in ("gain", "l1_misses", "mcnemar_exact_p")
                },
                "agreement": agreement,
                "coverage": {
                    k: coverage[k] for k in ("labeled", "c0_covered", "arm_covered")
                },
                "predictions": [(p["id"], p["got"], p["verdict"]) for p in graded],
            },
            indent=1,
        )
    )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest")
    s.add_argument("--store", default=str(STORE))
    s.add_argument("--l1-artifact", default=str(L1_ARTIFACT))
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("prompts")
    s.add_argument("--work", required=True)
    s.add_argument("--store", default=str(STORE))
    s.add_argument("--l1-artifact", default=str(L1_ARTIFACT))
    s.add_argument("--l1-prompts", default=str(L1_PROMPTS))
    s.add_argument("--s1-work", default=str(S1_WORK))
    s.add_argument("--s1-artifact", default=str(S1_ARTIFACT))
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_prompts)
    s = sub.add_parser("collect")
    s.add_argument("--work", required=True)
    s.set_defaults(fn=l1.cmd_collect)
    s = sub.add_parser("audit")
    s.add_argument("--work", required=True)
    s.add_argument("--transcripts", required=True)
    s.set_defaults(fn=s1.cmd_audit, role="readers")
    s = sub.add_parser("judgeset")
    s.add_argument("--work", required=True)
    s.add_argument("--s1-work", default=str(S1_WORK))
    s.add_argument("--s1-artifact", default=str(S1_ARTIFACT))
    s.set_defaults(fn=cmd_judgeset)
    s = sub.add_parser("report")
    s.add_argument("--work", required=True)
    s.add_argument("--l1-artifact", default=str(L1_ARTIFACT))
    s.add_argument("--s1-artifact", default=str(S1_ARTIFACT))
    s.add_argument("--out", default=str(OUT))
    s.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
