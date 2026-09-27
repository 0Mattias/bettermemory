"""Hindsight's published LongMemEval-S outputs, regraded and read by our reader.

UNIT H1 (declared 2026-09-27, memory 01M3J301BVTD7MMXA67T732MMM; the
owner's go on all 500, memory 01M3J4ZKE36JZ9S5RX37Q0NMFB). Hindsight's
page puts it first on LongMemEval-S at 94.6, graded by its own AMB harness
with a gemini-2.5-flash-lite judge on rewritten prompts and read by
gemini-3.1-pro-preview (memory 01M3J1HYBCSSSBYHC3BDFBX7AE), so the number
does not sit on the standard column. Its run file carries, for all 500
questions, the served context and the answer, so neither part below runs
Hindsight:

  part A  Hindsight's 500 published answers graded by both official judges
          exactly as S2's were, which puts its own run on the standard
          column
  part B  Hindsight's published served context read by S2's reader under
          S2's protocol, in place of bettermemory's served context and
          nothing else, which compares the two memories under one reader,
          one prompt and two judges

What part B changes from S2: the served context (Hindsight's `context`
field, verbatim), all 500 questions in place of the holdout 350, batches
of 3 in place of 4 (so a reader's load stays near S2's: 3 prompts of about
186,000 characters against 4 of about 135,000), and the stated file size,
set to the measured maximum by S1's rule. The rest is S2's: LongMemEval's
reading prompt with the question date, S1's reader wording, the
cross-evidence batching rule, the wrap for the Read tool and the audit.

STEPS, each a subcommand:

  runfile  the run file copied into the receipts and checked against the
           dataset
  regrade  part A's work directory: the 500 rows and Hindsight's answers,
           which L1's `price` and `judge` steps then grade
  prompts  part B's 500 reading prompts, answer-turn coverage of
           Hindsight's context, wrap, batches, reader instructions
  collect  the readers' answer files into the hypothesis file (L1's step)
  audit    every reader's transcript (S1's step) and the token projection
  report   the artifact

Nothing here opens a store: the served contexts are Hindsight's.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import unicodedata
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
import s2  # noqa: E402
from aml import run as aml_run  # noqa: E402
from aml.service import _text  # noqa: E402

# Hindsight v0.4.19's LongMemEval-S run in Vectorize's AMB harness, as its
# results page links it; the bytes the unit read are pinned by the hash.
RUN_FILE_URL = (
    "https://l4cy6iaq2c4g2ldt.public.blob.vercel-storage.com/outputs/longmemeval/"
    "hindsight/rag/s.json-cdNhcgkKvKuhtZAh4abx03GbOk90T7.gz"
)
RUN_FILE_SHA256 = "d0bcc75fb4060f0a395239a353d2066d3f7d252bbe16d674c951d9855deaf9cf"
RECEIPTS = l1.RECEIPTS / "h1-2026-09-27"
RUN_FILE = RECEIPTS / "hindsight-longmemeval-s.json.gz"
WORK_A = RECEIPTS / "work-a"
WORK_B = RECEIPTS / "work-b"
S2_WORK = l1.RECEIPTS / "s2-2026-09-27" / "work"
S2_ARTIFACT = s2.OUT
S1_ARTIFACT = s1.OUT
OUT = l1.RESULTS / "h1-hindsight-2026-09-27.json"
BATCH_SIZE = 3
N = 500
TOKEN_STOP = 50_000_000
JUDGES = s2.JUDGES
# bettermemory under the standard on all 500 (S1's dev and S2's holdout).
BETTERMEMORY_STANDARD = 492

# The five-window test (memory 01M3HV4YS425EP2AJR5BV9J1RS): an answer turn
# counts as present when any of five evenly spaced 60-character windows of
# its normalised text appears in the normalised context.
WINDOWS = 5
WINDOW = 60


# ---------------------------------------------------------------- the run file


def load_run(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if l1._sha(raw) != RUN_FILE_SHA256:
        raise SystemExit(f"{path} does not hash to the declared {RUN_FILE_SHA256}")
    run: dict[str, Any] = json.loads(gzip.decompress(raw))
    return run


def check_run(
    run: dict[str, Any], corpus: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The run's results by question id, refused unless they are the
    dataset's questions one for one, with the same question text, and each
    carries a context and an answer."""
    results = run["results"]
    by_id = {r["query_id"]: r for r in results}
    ids = {q["question_id"] for q in corpus}
    if len(results) != len(corpus) or len(by_id) != len(results) or set(by_id) != ids:
        raise SystemExit("the run file's questions are not the dataset's")
    differ = [
        q["question_id"]
        for q in corpus
        if by_id[q["question_id"]]["query"] != q["question"]
    ]
    if differ:
        raise SystemExit(f"{len(differ)} questions differ from the dataset's: {differ}")
    bad = sorted(
        qid
        for qid, r in by_id.items()
        if not isinstance(r.get("context"), str)
        or not r["context"].strip()
        or not isinstance(r.get("answer"), str)
    )
    if bad:
        raise SystemExit(f"{len(bad)} results lack a context or an answer: {bad}")
    return by_id


def splits(corpus: list[dict[str, Any]]) -> dict[str, str]:
    dev, holdout = aml_run.split(corpus)
    return {q: "dev" for q in dev} | {q: "holdout" for q in holdout}


def base_row(inst: dict[str, Any], split: str) -> dict[str, Any]:
    """What l1.judge_item reads, and the split."""
    qid = inst["question_id"]
    return {
        "question_id": qid,
        "question_type": inst["question_type"],
        "question": inst["question"],
        "gold": str(inst["answer"]),
        "abstention": "_abs" in qid,
        "split": split,
    }


def run_meta(run: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "dataset",
        "split",
        "memory_provider",
        "mode",
        "total_queries",
        "correct",
        "accuracy",
        "answer_llm",
        "judge_llm",
        "ingestion_time_ms",
        "ingested_docs",
        "avg_context_tokens",
    )
    return {
        "url": RUN_FILE_URL,
        "sha256": RUN_FILE_SHA256,
        **{k: run.get(k) for k in keep},
    }


def cmd_runfile(args: argparse.Namespace) -> None:
    dest = Path(args.run_file)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.source, dest)
    run = load_run(dest)
    check_run(run, l1._corpus())
    print(json.dumps(run_meta(run), indent=1))


# ---------------------------------------------------------------- part A


def cmd_regrade(args: argparse.Namespace) -> None:
    corpus = l1._corpus()
    by_id = check_run(load_run(Path(args.run_file)), corpus)
    split = splits(corpus)
    rows = [base_row(inst, split[inst["question_id"]]) for inst in corpus]
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    l1._write_json(work / "meta.json", {"unit": "H1-A", "rows": rows})
    with (work / "hypotheses.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            qid = r["question_id"]
            fh.write(
                json.dumps(
                    {"question_id": qid, "hypothesis": by_id[qid]["answer"]},
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(
        json.dumps(
            {
                "rows": len(rows),
                "dev": sum(r["split"] == "dev" for r in rows),
                "holdout": sum(r["split"] == "holdout" for r in rows),
                "work": str(work),
            }
        )
    )


# ---------------------------------------------------------------- part B


def normal(text: str) -> str:
    """NFKC, whitespace collapsed, lowercased."""
    return " ".join(unicodedata.normalize("NFKC", text).split()).lower()


def windows(text: str) -> list[str]:
    if len(text) <= WINDOW:
        return [text]
    step = (len(text) - WINDOW) / (WINDOWS - 1)
    return [text[round(i * step) : round(i * step) + WINDOW] for i in range(WINDOWS)]


def unquote(context: str) -> str:
    """Hindsight's context with each quoted line of raw turns (`> ` and a
    JSON list of turns) replaced by the turns' contents, so the test reads
    the text and not its JSON escapes. A line that does not parse (each
    context's last quote, cut by Hindsight's budget) stays as it is."""
    out: list[str] = []
    for line in context.split("\n"):
        if line.startswith("> "):
            try:
                turns = json.loads(line[2:])
            except json.JSONDecodeError:
                turns = None
            if isinstance(turns, list):
                out.extend(
                    str(t.get("content", "")) for t in turns if isinstance(t, dict)
                )
                continue
        out.append(line)
    return "\n".join(out)


def coverage(inst: dict[str, Any], context: str) -> dict[str, Any]:
    """Whether every answer turn is present in the context by the
    five-window test. A question with no answer turns is counted apart,
    never as covered."""
    turns = s0.answer_turns(inst)
    text = normal(unquote(context))
    missing = []
    for k, j, i in turns:
        turn = normal(_text(inst["haystack_sessions"][k][i].get("content")))
        if not any(w in text for w in windows(turn)):
            missing.append((k, j, i))
    return {
        "answer_turns": len(turns),
        "missing": missing,
        "covered": bool(turns) and not missing,
    }


def build(inst: dict[str, Any], context: str, split: str) -> tuple[dict[str, Any], str]:
    """One question's reading prompt on Hindsight's context, and its row."""
    prompt = l1.reading_prompt(context, inst["question_date"], inst["question"])
    got = coverage(inst, context)
    return {
        **base_row(inst, split),
        "question_date": inst["question_date"],
        "context_chars": len(context),
        "prompt_chars": len(prompt),
        "context_sha256": l1._sha(context),
        "prompt_sha256": l1._sha(prompt),
        "answer_turns": got["answer_turns"],
        "turns_missing": len(got["missing"]),
        "covered": got["covered"],
    }, prompt


def reader_instructions(
    batch_file: Path, answers_file: Path, chars: int, lines: int
) -> str:
    """S1's wording, unchanged; only the measured file size differs. Its
    "up to 4 prompt file paths" holds for batches of 3."""
    return s1.reader_instructions(batch_file, answers_file, chars, lines)


def s2_rate(s2_work: Path, s2_artifact: Path) -> float:
    """S2's counted reader tokens per prompt character."""
    rows = l1._load_json(s2_work / "meta.json")["rows"]
    tokens = l1._load_json(s2_artifact)["cost"]["reader_tokens"]["counted"]
    chars: int = sum(r["prompt_chars"] for r in rows)
    return float(tokens) / chars


def cmd_prompts(args: argparse.Namespace) -> None:
    corpus = l1._corpus()
    by_id = check_run(load_run(Path(args.run_file)), corpus)
    split = splits(corpus)
    work = Path(args.work).resolve()
    for sub in ("prompts", "prompts_wrapped", "batches", "readers", "answers"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for inst in corpus:
        qid = inst["question_id"]
        row, prompt = build(inst, by_id[qid]["context"], split[qid])
        wrapped, inserted = l1.wrap_for_read(prompt)
        (work / "prompts" / f"{qid}.txt").write_bytes(prompt.encode("utf-8"))
        (work / "prompts_wrapped" / f"{qid}.txt").write_bytes(wrapped.encode("utf-8"))
        row.update(
            wrap_inserted=inserted,
            wrapped_lines=l1.line_count(wrapped),
            longest_line=max(len(x) for x in wrapped.split("\n")),
        )
        rows.append(row)
    chars = s1.round_up(max(r["prompt_chars"] for r in rows), 1_000)
    lines = s1.round_up(max(r["wrapped_lines"] for r in rows), 100)
    pairs = l1.cross_evidence(corpus)
    batches = l1.make_batches([r["question_id"] for r in rows], pairs, size=BATCH_SIZE)
    instructions: list[str] = []
    for i, batch in enumerate(batches):
        # L1's names: batch_00 to batch_99, then batch_100 and on
        batch_file = work / "batches" / f"batch_{i:02d}.txt"
        answers_file = work / "answers" / f"batch_{i:02d}.json"
        listing = "".join(f"{work / 'prompts_wrapped' / q}.txt\n" for q in batch)
        batch_file.write_text(listing, encoding="utf-8")
        text = reader_instructions(batch_file, answers_file, chars, lines)
        (work / "readers" / f"reader_{i:02d}.txt").write_text(text, encoding="utf-8")
        instructions.append(text)
    labeled = [r for r in rows if r["answer_turns"]]
    held = [r for r in labeled if r["split"] == "holdout"]
    counts = {
        "labeled": len(labeled),
        "covered": sum(r["covered"] for r in labeled),
        "holdout_labeled": len(held),
        "holdout_covered": sum(r["covered"] for r in held),
    }
    projection = round(
        s2_rate(Path(args.s2_work), Path(args.s2_artifact))
        * sum(r["prompt_chars"] for r in rows)
    )
    l1._write_json(
        work / "meta.json",
        {
            "unit": "H1-B",
            "batch_size": BATCH_SIZE,
            "stated_chars": chars,
            "stated_lines": lines,
            "reader_template_sha256": l1._sha(s1.READER_TEMPLATE),
            "coverage": counts,
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
                "coverage": counts,
                "stated_chars": chars,
                "stated_lines": lines,
                "prompt_chars_max": max(r["prompt_chars"] for r in rows),
                "prompt_chars_mean": round(
                    sum(r["prompt_chars"] for r in rows) / len(rows)
                ),
                "projected_reader_tokens": projection,
            }
        )
    )


# ---------------------------------------------------------------- the audit


def projected_tokens(counted: int, questions: int) -> int:
    """Counted reader tokens so far, scaled to the 500."""
    return counted * N // questions if questions else 0


def over_stop(tokens: int) -> bool:
    return tokens > TOKEN_STOP


def progress(result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    """The batches read so far, and the tokens projected to the 500 from
    the clean ones: their counted tokens per question they hold."""
    sizes = {f"batch_{i:02d}": len(b) for i, b in enumerate(meta["batches"])}
    done = {n: g for n, g in result["canonical"].items() if g["clean"]}
    questions = sum(sizes[n] for n in done)
    counted = sum(s1.counted_tokens(g["usage"]) for g in done.values())
    projected = projected_tokens(counted, questions)
    return {
        "batches_clean": len(done),
        "batches": len(sizes),
        "questions_read": questions,
        "tokens_clean": counted,
        "tokens_all_attempts": s1.counted_tokens(
            result["summary"]["usage_all_attempts"]
        ),
        "projected": projected,
        "over_stop": over_stop(projected),
    }


def cmd_audit(args: argparse.Namespace) -> None:
    s1.cmd_audit(args)
    work = Path(args.work).resolve()
    result = l1._load_json(work / "audit-readers.json")
    print(json.dumps(progress(result, l1._load_json(work / "meta.json"))))


# ---------------------------------------------------------------- the grade


def paired(a: dict[str, Any], b: dict[str, Any], qids: list[str]) -> dict[str, Any]:
    """Two sets of answers to the same questions under one judge, a missing
    verdict counted wrong; the lead is a's correct count less b's."""
    a_ok = {q: a.get(q) is True for q in qids}
    b_ok = {q: b.get(q) is True for q in qids}
    a_only = sorted(q for q in qids if a_ok[q] and not b_ok[q])
    b_only = sorted(q for q in qids if b_ok[q] and not a_ok[q])
    return {
        "n": len(qids),
        "a_correct": sum(a_ok.values()),
        "b_correct": sum(b_ok.values()),
        "both": sum(a_ok[q] and b_ok[q] for q in qids),
        "neither": sum(not a_ok[q] and not b_ok[q] for q in qids),
        "a_only": a_only,
        "b_only": b_only,
        "lead": len(a_only) - len(b_only),
        "mcnemar_exact_p": round(s1.mcnemar_exact(len(a_only), len(b_only)), 6),
    }


def bettermemory_verdicts(s2_artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """bettermemory's 500 verdicts by judge as the S2 artifact recorded
    them: S2's holdout answers and S1's dev answers."""
    out: dict[str, dict[str, Any]] = {}
    for name, model in JUDGES.items():
        got = {
            r["question_id"]: r["verdicts"]["s2"][model] for r in s2_artifact["rows"]
        }
        got |= {
            r["question_id"]: r["verdicts"]["s1"][model]
            for r in s2_artifact["dev_rows"]
        }
        out[name] = got
    return out


def predictions(n: dict[str, Any]) -> list[dict[str, Any]]:
    """The declaration's five predictions graded on the run's numbers."""
    v = l1._verdict
    own, held, whole = n["own_standard"], n["b_holdout_standard"], n["b_all_standard"]
    lead, p = n["lead_holdout_standard"], n["p_holdout_standard"]
    tokens, usd = n["reader_tokens"], n["judge_usd"]
    ok = n["audit_ok"] and n["stops"] <= 3 and usd < 1.5
    return [
        {
            "id": "H1-P1",
            "claim": "Hindsight's own answers under the standard 462 of 500 (452 to "
            "472); MISSED above 473 or below 440",
            "got": own,
            "verdict": v(452 <= own <= 472, own > 473 or own < 440),
        },
        {
            "id": "H1-P2",
            "claim": "the same reader on Hindsight's context, the holdout 350 under "
            "the standard, 339 (333 to 344); MISSED above 346 or below 328",
            "got": held,
            "verdict": v(333 <= held <= 344, held > 346 or held < 328),
        },
        {
            "id": "H1-P3",
            "claim": "bettermemory's S2 answers ahead of the same-reader Hindsight "
            "answers on the holdout 350 under the standard by 2 to 8, exact McNemar "
            "p above 0.05; MISSED if Hindsight is ahead by 3 or more",
            "got": {"lead": lead, "p": p},
            "verdict": v(2 <= lead <= 8 and p > 0.05, lead <= -3),
        },
        {
            "id": "H1-P4",
            "claim": "all 500 under the standard, Hindsight read by Claude 484 (476 "
            f"to 490) against bettermemory's {BETTERMEMORY_STANDARD}; no wider band "
            "was declared, so outside the range is MISSED",
            "got": whole,
            "verdict": v(476 <= whole <= 490, not 476 <= whole <= 490),
        },
        {
            "id": "H1-P5",
            "claim": "every counted batch clean, at most 3 attempts stopped by a "
            "safeguard, counted tokens 34M to 46M, judges under $1.50 for both "
            "parts; MISSED if any but the token range fails or tokens pass 50M",
            "got": {
                "audit_ok": n["audit_ok"],
                "stops": n["stops"],
                "reader_tokens": tokens,
                "judge_usd": usd,
            },
            "verdict": v(
                ok and 34_000_000 <= tokens <= 46_000_000,
                not ok or over_stop(tokens),
            ),
        },
    ]


def _verdicts(judged: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: {q: r["verdict"] for q, r in judged[name]["rows"].items()}
        for name in JUDGES
    }


def _disagreements(
    rows: list[dict[str, Any]], verdicts: dict[str, dict[str, Any]]
) -> list[str]:
    return [
        r["question_id"]
        for r in rows
        if (verdicts["record"].get(r["question_id"]) is True)
        != (verdicts["standard"].get(r["question_id"]) is True)
    ]


def cmd_report(args: argparse.Namespace) -> None:
    work_a, work_b = Path(args.work_a).resolve(), Path(args.work_b).resolve()
    rows = l1._load_json(work_a / "meta.json")["rows"]
    meta_b = l1._load_json(work_b / "meta.json")
    if [r["question_id"] for r in rows] != [r["question_id"] for r in meta_b["rows"]]:
        raise SystemExit("parts A and B do not hold the same questions in order")
    rows_b = {r["question_id"]: r for r in meta_b["rows"]}
    qids = [r["question_id"] for r in rows]
    holdout = [r["question_id"] for r in rows if r["split"] == "holdout"]
    dev = [r["question_id"] for r in rows if r["split"] == "dev"]
    of = {"all": qids, "holdout": holdout, "dev": dev}
    run = load_run(Path(args.run_file))
    by_id = check_run(run, l1._corpus())
    own_judge = {q: bool(by_id[q]["correct"]) for q in qids}
    judged = {
        "a": l1._load_json(work_a / "judgments.json"),
        "b": l1._load_json(work_b / "judgments.json"),
    }
    price = {
        "a": l1._load_json(work_a / "price.json"),
        "b": l1._load_json(work_b / "price.json"),
    }
    verdicts = {part: _verdicts(judged[part]) for part in judged}
    answers_b = {
        h["question_id"]: h["hypothesis"]
        for h in l1._jsonl(work_b / "hypotheses.jsonl")
    }
    s2_art = l1._load_json(Path(args.s2_artifact))
    s1_art = l1._load_json(Path(args.s1_artifact))
    bm = bettermemory_verdicts(s2_art)
    if set(bm["standard"]) != set(qids):
        raise SystemExit("the S2 artifact does not carry bettermemory's 500 verdicts")
    arms = {
        "hindsight_own_answers": verdicts["a"],
        "hindsight_read_by_claude": verdicts["b"],
        "bettermemory": bm,
    }
    by_split = {
        k: [r for r in rows if r["question_id"] in set(ids)] for k, ids in of.items()
    }
    scores = {
        arm: {
            name: {k: s2.score(by_split[k], got[name]) for k in of} for name in JUDGES
        }
        for arm, got in arms.items()
    }
    pairs = {
        "bettermemory_vs_hindsight_read_by_claude": {
            name: {
                k: paired(bm[name], verdicts["b"][name], ids) for k, ids in of.items()
            }
            for name in JUDGES
        },
        "hindsight_read_by_claude_vs_own_answers": {
            name: {
                k: paired(verdicts["b"][name], verdicts["a"][name], ids)
                for k, ids in of.items()
            }
            for name in JUDGES
        },
        "bettermemory_vs_hindsight_own_answers": {
            name: {
                k: paired(bm[name], verdicts["a"][name], ids) for k, ids in of.items()
            }
            for name in JUDGES
        },
    }
    disagree = {part: _disagreements(rows, verdicts[part]) for part in verdicts}
    # answer-turn coverage: Hindsight's context by the five-window test,
    # bettermemory's served context by S0's whole-turn test (S2's holdout
    # rows and S1's dev rows)
    bm_rows = {r["question_id"]: r for r in s2_art["rows"]} | {
        r["question_id"]: r for r in s1_art["rows"]
    }
    std_b = verdicts["b"]["standard"]
    cover: dict[str, Any] = {}
    for k, ids in of.items():
        labeled = [q for q in ids if rows_b[q]["answer_turns"]]
        hs_cov = [q for q in labeled if rows_b[q]["covered"]]
        hs_unc = [q for q in labeled if not rows_b[q]["covered"]]
        cover[k] = {
            "labeled": len(labeled),
            "hindsight_covered": len(hs_cov),
            "bettermemory_covered": sum(bool(bm_rows[q]["covered"]) for q in labeled),
            "claude_on_hindsight_standard_correct_when_covered": sum(
                std_b.get(q) is True for q in hs_cov
            ),
            "hindsight_uncovered": {
                "ids": hs_unc,
                "claude_on_hindsight_standard_correct": sum(
                    std_b.get(q) is True for q in hs_unc
                ),
            },
        }
    served = {
        k: {
            "hindsight_mean_chars": round(
                sum(rows_b[q]["context_chars"] for q in ids) / len(ids)
            ),
            "bettermemory_mean_chars": round(
                sum(bm_rows[q]["context_chars"] for q in ids) / len(ids)
            ),
        }
        for k, ids in of.items()
    }
    readers = l1._load_json(work_b / "audit-readers.json")["summary"]
    tokens_readers = s1.counted_tokens(readers["usage"])
    tokens_all = s1.counted_tokens(readers["usage_all_attempts"])
    spent = {
        part: {JUDGES[name]: judged[part][name]["spent_usd"] for name in JUDGES}
        for part in judged
    }
    judge_usd = round(sum(sum(v.values()) for v in spent.values()), 6)
    audit_ok = (
        readers["with_answers"] == readers["expected"]
        and readers["clean"] == readers["expected"]
        and readers["violations"] == 0
        and readers["files_not_read_to_end"] == 0
        and readers["flagged_attachments"] == 0
        and readers["models"] == [l1.READER_MODEL]
    )
    lead = pairs["bettermemory_vs_hindsight_read_by_claude"]["standard"]["holdout"]
    graded = predictions(
        {
            "own_standard": scores["hindsight_own_answers"]["standard"]["all"][
                "correct"
            ],
            "b_holdout_standard": scores["hindsight_read_by_claude"]["standard"][
                "holdout"
            ]["correct"],
            "b_all_standard": scores["hindsight_read_by_claude"]["standard"]["all"][
                "correct"
            ],
            "lead_holdout_standard": lead["lead"],
            "p_holdout_standard": lead["mcnemar_exact_p"],
            "audit_ok": audit_ok,
            "stops": readers["attempts_stopped_by_safeguard"],
            "reader_tokens": tokens_readers,
            "judge_usd": judge_usd,
        }
    )
    by_row = {r["question_id"]: r for r in rows}
    l1._write_json(
        Path(args.out),
        {
            "unit": "H1",
            "declaration": "memory 01M3J301BVTD7MMXA67T732MMM",
            "approval": "memory 01M3J4ZKE36JZ9S5RX37Q0NMFB",
            "provenance": l1._provenance(),
            "protocol": {
                "dataset": {
                    "file": str(l1.CORPUS.relative_to(_BENCH.parent)),
                    "sha256": l1.CORPUS_SHA256,
                },
                "hindsight_run": run_meta(run),
                "split": "all 500, with the holdout 350 and dev 150 of aml_run.split "
                "(SPLIT_SEED " + str(aml_run.SPLIT_SEED) + ") reported apart",
                "part_a": "Hindsight's published answer field graded by both judges, "
                "gold answers and question types from the dataset",
                "part_b": {
                    "served_context": "Hindsight's published context field, verbatim",
                    "reading_prompt": "L1's: run_generation.py con at "
                    + l1.UPSTREAM
                    + ", question dates",
                    "reader": {
                        "model": l1.READER_MODEL,
                        "harness": "Claude Code subagents (general-purpose)",
                        "batch_size": BATCH_SIZE,
                        "batches": len(meta_b["batches"]),
                        "template_sha256": meta_b["reader_template_sha256"],
                        "stated_chars": meta_b["stated_chars"],
                        "stated_lines": meta_b["stated_lines"],
                    },
                },
                "judges": s2_art["protocol"]["judges"],
                "coverage_tests": {
                    "hindsight": "five evenly spaced 60-character windows of each "
                    "answer turn, NFKC, whitespace collapsed, lowercased; any window "
                    "in the context with its quoted raw turns decoded from JSON",
                    "bettermemory": "S0's: every answer turn whole in a served round "
                    "(S2's holdout rows, S1's dev rows)",
                },
            },
            "summary": {
                "scores": scores,
                "paired": pairs,
                "hindsight_own_judge": {
                    "judge": run.get("judge_llm"),
                    "correct": sum(own_judge.values()),
                    "agrees_with": {
                        JUDGES[name]: sum(
                            own_judge[q] == (verdicts["a"][name].get(q) is True)
                            for q in qids
                        )
                        for name in JUDGES
                    },
                },
                "judge_agreement": {
                    part: {"n": len(rows), "agree": len(rows) - len(d)}
                    for part, d in disagree.items()
                },
                "coverage": cover,
                "served": served,
            },
            "disagreements": {
                part: [
                    {
                        "question_id": q,
                        "split": by_row[q]["split"],
                        "question_type": by_row[q]["question_type"],
                        **{
                            JUDGES[name]: verdicts[part][name].get(q) for name in JUDGES
                        },
                    }
                    for q in d
                ]
                for part, d in disagree.items()
            },
            "audit": {"readers": readers},
            "cost": {
                "judges_usd": spent,
                "judge_calls": {
                    part: {JUDGES[name]: judged[part][name]["calls"] for name in JUDGES}
                    for part in judged
                },
                "judge_errors": {
                    part: {
                        JUDGES[name]: judged[part][name]["errors"] for name in JUDGES
                    }
                    for part in judged
                },
                "price_check": {
                    part: {k: val for k, val in p.items() if k not in l1.ACCOUNT_FIELDS}
                    for part, p in price.items()
                },
                "reader_tokens": {
                    "counted": tokens_readers,
                    "all_attempts": tokens_all,
                    "projected_at_prompts": meta_b["projected_reader_tokens"],
                    "stop": TOKEN_STOP,
                },
            },
            "predictions": graded,
            # Hindsight's answers stay in its run file; each is pinned here by
            # its hash. The hypothesis is our reader's answer on its context.
            "rows": [
                {
                    **{
                        k: by_row[q][k]
                        for k in ("question_id", "split", "question_type", "abstention")
                    },
                    "hindsight": {
                        "context_chars": rows_b[q]["context_chars"],
                        "context_sha256": rows_b[q]["context_sha256"],
                        "answer_sha256": l1._sha(by_id[q]["answer"]),
                        "own_judge": own_judge[q],
                        "answer_turns": rows_b[q]["answer_turns"],
                        "covered": rows_b[q]["covered"],
                    },
                    "verdicts": {
                        arm: {JUDGES[name]: got[name].get(q) for name in JUDGES}
                        for arm, got in arms.items()
                    },
                    "hypothesis": answers_b.get(q),
                }
                for q in qids
            ],
        },
    )
    print(
        json.dumps(
            {
                "standard": {
                    arm: {k: scores[arm]["standard"][k]["correct"] for k in of}
                    for arm in arms
                },
                "record": {
                    arm: {k: scores[arm]["record"][k]["correct"] for k in of}
                    for arm in arms
                },
                "holdout_standard_paired": {
                    k: lead[k]
                    for k in ("a_correct", "b_correct", "lead", "mcnemar_exact_p")
                },
                "coverage": {
                    k: {
                        x: cover[k][x]
                        for x in (
                            "labeled",
                            "hindsight_covered",
                            "bettermemory_covered",
                        )
                    }
                    for k in of
                },
                "predictions": [(p["id"], p["got"], p["verdict"]) for p in graded],
            },
            indent=1,
        )
    )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("runfile")
    s.add_argument("--source", required=True)
    s.add_argument("--run-file", default=str(RUN_FILE))
    s.set_defaults(fn=cmd_runfile)
    s = sub.add_parser("regrade")
    s.add_argument("--work", default=str(WORK_A))
    s.add_argument("--run-file", default=str(RUN_FILE))
    s.set_defaults(fn=cmd_regrade)
    s = sub.add_parser("prompts")
    s.add_argument("--work", default=str(WORK_B))
    s.add_argument("--run-file", default=str(RUN_FILE))
    s.add_argument("--s2-work", default=str(S2_WORK))
    s.add_argument("--s2-artifact", default=str(S2_ARTIFACT))
    s.set_defaults(fn=cmd_prompts)
    s = sub.add_parser("collect")
    s.add_argument("--work", default=str(WORK_B))
    s.set_defaults(fn=l1.cmd_collect)
    s = sub.add_parser("audit")
    s.add_argument("--work", default=str(WORK_B))
    s.add_argument("--transcripts", required=True)
    s.set_defaults(fn=cmd_audit, role="readers")
    s = sub.add_parser("report")
    s.add_argument("--work-a", default=str(WORK_A))
    s.add_argument("--work-b", default=str(WORK_B))
    s.add_argument("--run-file", default=str(RUN_FILE))
    s.add_argument("--s2-artifact", default=str(S2_ARTIFACT))
    s.add_argument("--s1-artifact", default=str(S1_ARTIFACT))
    s.add_argument("--out", default=str(OUT))
    s.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
