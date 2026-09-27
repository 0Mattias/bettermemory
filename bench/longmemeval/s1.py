"""LongMemEval-S dev 150 read by the chat model under the S0 arm.

UNIT S1 (declared 2026-09-27, memory 01M3HXRQXE9EX4QXGA3BH70J2S). L1 read
all 500 questions with C0's served context. S0 (memory
01M3HXFX9YMS7WB6Y9NA4QVK32) found that session order, a 200-round pool,
neighbor fill and tail trim serve every answer turn on 142 of the 143 dev
questions that carry answer-turn labels at a 180,000-character budget,
against 129 under C0. S1 reads the dev 150 under that arm, with the rest
as L1 has it: LongMemEval's own reading prompt with the question date,
Claude Opus 5.5 as Claude Code subagents, the cross-evidence batching
rule, the wrap for the Read tool and the transcript audit. What differs:
the served context, 4 prompts a batch in place of 6 (so each reader's
context stays near L1's), and, in the reader instructions, the batch size
and the stated file size.

THE GRADE is one blinded pass over L1's dev answers and S1's: each
question's two answers sit in different grader files, no arm is named,
and Claude Opus 5.5 subagents grade under AML's accuracy rules (the
reader-swap rules.txt, as L1's continuity grader). The comparison is
paired and has one grader pass. No API call; the official judges come
with the holdout read.

STEPS, each a subcommand over one work directory:

  prompts  serve the dev 150 from the S0 store under the arm, check every
           served context against S0's cell, fill, wrap and batch
  collect  the readers' answer files into the hypothesis file (L1's step)
  audit    every reader's or grader's transcript
  blind    L1's and S1's dev answers into the graders' files
  report   the artifact

Every process that opens a store runs with BETTERMEMORY_KEYS_DIR set to
the S0 receipts' keys.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
for _path in (_BENCH, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import l1  # noqa: E402
import s0  # noqa: E402
from aml.service import MemoryService  # noqa: E402

ARM = s0.Arm(order="session", top_k=200, fill="neighbors", trim="tail")
BUDGET = 180_000
BATCH_SIZE = 4
STORE = s0.STORE
S0_ARTIFACT = s0.OUT
L1_HYPOTHESES = s0.L1_RECEIPTS / "work" / "hypotheses.jsonl"
OUT = l1.RESULTS / "s1-dev-2026-09-27.json"
BLIND_SEED = 20260929
TOKEN_STOP = 12_000_000
ARMS = ("l1", "s1")

# L1's second wording (l1.READER_INSTRUCTIONS) with two spans changed:
# the batch size, and the stated file size, which the S1 prompts exceed
# enough that one Read returns only part of a file.
_L1_BATCH = "it lists up to 6 prompt file paths, one per line."
_L1_SIZE = (
    "(each is up to about 90,000 characters and 2,000 lines; if a read stops "
    "early, continue with offset until the end, because the question is on "
    "the last lines)"
)
_S1_BATCH = "it lists up to 4 prompt file paths, one per line."
_S1_SIZE = (
    "(each is up to about {chars:,} characters and {lines:,} lines, more than "
    "one Read returns; continue with offset until the end, because the "
    "question is on the last lines)"
)
assert l1.READER_INSTRUCTIONS.count(_L1_BATCH) == 1
assert l1.READER_INSTRUCTIONS.count(_L1_SIZE) == 1
READER_TEMPLATE = l1.READER_INSTRUCTIONS.replace(_L1_BATCH, _S1_BATCH).replace(
    _L1_SIZE, _S1_SIZE
)


def service_for(root: Path) -> MemoryService:
    """C0's service pointed at the S0 arm at S1's budget, exactly as S0
    measured the arm's curve."""
    service = l1.service_for(root)
    s0.configure(service, ARM, BUDGET)
    return service


def reader_instructions(
    batch_file: Path, answers_file: Path, chars: int, lines: int
) -> str:
    return READER_TEMPLATE.format(
        batch=batch_file, answers=answers_file, chars=chars, lines=lines
    )


def round_up(n: int, step: int) -> int:
    return -(-n // step) * step


def build(service: MemoryService, inst: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """One dev question served from the S0 store under the arm (the store
    already holds its haystack, so nothing is added), its coverage and its
    reading prompt."""
    hits: list[dict[str, Any]] = service.search(
        s0.user_id(inst), inst["question"], ARM.top_k
    )
    positions = s0.round_positions(service, inst)
    context = l1.served_context(hits)
    got = s0.coverage(inst, hits, positions)
    prompt = l1.reading_prompt(context, inst["question_date"], inst["question"])
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "split": "dev",
        "abstention": "_abs" in inst["question_id"],
        "question": inst["question"],
        "gold": str(inst["answer"]),
        "question_date": inst["question_date"],
        "n_hits": len(hits),
        "context_chars": len(context),
        "prompt_chars": len(prompt),
        "context_sha256": l1._sha(context),
        "prompt_sha256": l1._sha(prompt),
        "answer_turns": got["answer_turns"],
        "covered": got["covered"],
    }, prompt


def parity(rows: list[dict[str, Any]], s0_rows: dict[str, Any]) -> list[str]:
    """Questions whose served context differs from S0's cell for the arm at
    this budget, in characters or coverage."""
    key = str(BUDGET)
    return [
        r["question_id"]
        for r in rows
        if s0_rows[r["question_id"]]["best_curve"][key]["chars"] != r["context_chars"]
        or s0_rows[r["question_id"]]["best_curve"][key]["covered"] != r["covered"]
    ]


def cmd_prompts(args: argparse.Namespace) -> None:
    if not os.environ.get("BETTERMEMORY_KEYS_DIR"):
        raise SystemExit("set BETTERMEMORY_KEYS_DIR to the S0 receipts' keys")
    s0_art = json.loads(Path(args.s0_artifact).read_text(encoding="utf-8"))
    if s0_art["best_arm"] != ARM.name:
        raise SystemExit(f"S0's best arm is {s0_art['best_arm']}, not {ARM.name}")
    s0_rows = {r["question_id"]: r for r in s0_art["rows"]}
    corpus = l1._corpus()
    dev = s0.dev_instances(corpus)
    work = Path(args.work).resolve()
    for sub in ("prompts", "prompts_wrapped", "batches", "readers", "answers"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    service = service_for(Path(args.store))
    rows: list[dict[str, Any]] = []
    for inst in dev:
        row, prompt = build(service, inst)
        qid = row["question_id"]
        wrapped, inserted = l1.wrap_for_read(prompt)
        (work / "prompts" / f"{qid}.txt").write_bytes(prompt.encode("utf-8"))
        (work / "prompts_wrapped" / f"{qid}.txt").write_bytes(wrapped.encode("utf-8"))
        row.update(
            wrap_inserted=inserted,
            wrapped_lines=l1.line_count(wrapped),
            longest_line=max(len(x) for x in wrapped.split("\n")),
        )
        rows.append(row)
    differ = parity(rows, s0_rows)
    if differ:
        raise SystemExit(
            f"S1-P1: {len(differ)} served contexts differ from S0: {differ}"
        )
    chars = round_up(max(r["prompt_chars"] for r in rows), 1_000)
    lines = round_up(max(r["wrapped_lines"] for r in rows), 100)
    pairs = l1.cross_evidence(dev)
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
    l1._write_json(
        work / "meta.json",
        {
            "unit": "S1",
            "arm": ARM.name,
            "budget": BUDGET,
            "batch_size": BATCH_SIZE,
            "stated_chars": chars,
            "stated_lines": lines,
            "reader_template_sha256": l1._sha(READER_TEMPLATE),
            "parity": {"n": len(rows), "equal": len(rows) - len(differ)},
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
                "stated_chars": chars,
                "stated_lines": lines,
                "prompt_chars_max": max(r["prompt_chars"] for r in rows),
                "prompt_chars_mean": round(
                    sum(r["prompt_chars"] for r in rows) / len(rows)
                ),
                "covered": sum(r["covered"] for r in rows),
            }
        )
    )


# ---------------------------------------------------------------- the audit


def reader_specs(work: Path, meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = {r["question_id"]: r for r in meta["rows"]}
    specs: dict[str, dict[str, Any]] = {}
    for i, (batch, text) in enumerate(
        zip(meta["batches"], meta["instructions"], strict=True)
    ):
        name = f"batch_{i:02d}"
        prompt_files = {
            str(work / "prompts_wrapped" / f"{q}.txt"): rows[q]["wrapped_lines"]
            for q in batch
        }
        specs[name] = {
            "instructions": text,
            "reads": {str(work / "batches" / f"{name}.txt"), *prompt_files},
            "full_reads": prompt_files,
            "write": str(work / "answers" / f"{name}.json"),
        }
    return specs


def grader_specs(work: Path) -> dict[str, dict[str, Any]]:
    grader = l1._load_json(work / "grader" / "meta.json")
    specs: dict[str, dict[str, Any]] = {}
    for part in grader["parts"]:
        files = {part["rules"]: part["rules_lines"], part["items"]: part["items_lines"]}
        specs[Path(part["items"]).stem] = {
            "instructions": part["instructions"],
            "reads": set(files),
            "full_reads": files,
            "write": part["verdicts"],
        }
    return specs


def audit(
    specs: dict[str, dict[str, Any]],
    transcripts: list[tuple[str, list[dict[str, Any]]]],
) -> dict[str, Any]:
    """Every attempt matched to its spec by its instructions; the last
    attempt whose Write stands is the canonical one, and a spec counts only
    when that attempt is clean (l1.audit_transcript's checks)."""
    by_text = {spec["instructions"]: name for name, spec in specs.items()}
    attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name_of_file, entries in transcripts:
        text = l1._first_user_text(entries)
        if text not in by_text:
            continue
        name = by_text[text]
        spec = specs[name]
        got = l1.audit_transcript(
            entries,
            instructions=text,
            reads=spec["reads"],
            full_reads=spec["full_reads"],
            write=spec["write"],
        )
        got["transcript"] = name_of_file
        attempts[name].append(got)
    canonical: dict[str, dict[str, Any]] = {}
    for name, runs in attempts.items():
        runs.sort(key=lambda g: g["started"] or "")
        writers = [g for g in runs if g["wrote"]]
        if writers:
            canonical[name] = writers[-1]
    every = [g for runs in attempts.values() for g in runs]

    def total(runs: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = dict.fromkeys(l1._USAGE, 0)
        for g in runs:
            for k in l1._USAGE:
                out[k] += g["usage"][k]
        return out

    kept = list(canonical.values())
    summary = {
        "expected": len(specs),
        "with_answers": len(canonical),
        "missing": sorted(set(specs) - set(canonical)),
        "clean": sum(g["clean"] for g in kept),
        "not_clean": sorted(n for n, g in canonical.items() if not g["clean"]),
        "attempts": len(every),
        "attempts_stopped_by_safeguard": sum(
            g["safeguard_stops"] > 0 or g["api_errors"] > 0 for g in every
        ),
        "models": sorted({m for g in kept for m in g["models"]}),
        "violations": sum(len(g["violations"]) for g in kept),
        "files_not_read_to_end": sum(
            not ok for g in kept for ok in g["read_to_end"].values()
        ),
        "flagged_attachments": sum(len(g["flagged_attachments"]) for g in kept),
        "tools": {
            t: sum(g["tools"].get(t, 0) for g in kept)
            for t in sorted({t for g in kept for t in g["tools"]})
        },
        "usage": total(kept),
        "usage_all_attempts": total(every),
    }
    return {"summary": summary, "canonical": canonical, "attempts": attempts}


def counted_tokens(usage: dict[str, int]) -> int:
    """L1's measure: input, cache creation and output."""
    return (
        usage["input_tokens"]
        + usage["cache_creation_input_tokens"]
        + usage["output_tokens"]
    )


def cmd_audit(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    meta = l1._load_json(work / "meta.json")
    specs = reader_specs(work, meta) if args.role == "readers" else grader_specs(work)
    transcripts = [
        (path.name, entries)
        for path, entries in l1._transcripts(Path(args.transcripts))
    ]
    result = audit(specs, transcripts)
    result["summary"]["role"] = args.role
    l1._write_json(work / f"audit-{args.role}.json", result)
    s = result["summary"]
    print(
        json.dumps(
            {
                **{
                    k: v
                    for k, v in s.items()
                    if k not in ("usage", "usage_all_attempts")
                },
                "tokens_counted": counted_tokens(s["usage"]),
                "tokens_all_attempts": counted_tokens(s["usage_all_attempts"]),
            },
            indent=1,
        )
    )


# ---------------------------------------------------------------- the grade


def blind_pair(
    answers: dict[str, dict[str, str]],
    rows: list[dict[str, Any]],
    seed: int = BLIND_SEED,
) -> tuple[list[list[dict[str, str]]], dict[str, list[str]]]:
    """Both arms' answers under opaque ids in two parts: each question's two
    answers go to different parts (a seeded coin picks which), each part is
    shuffled, and the key maps an id to its arm and question. No item names
    its arm."""
    rng = random.Random(seed)
    by_id = {r["question_id"]: r for r in rows}
    parts: list[list[tuple[str, str]]] = [[], []]
    for qid in sorted(by_id):
        first = rng.randrange(2)
        parts[first].append(("l1", qid))
        parts[1 - first].append(("s1", qid))
    out: list[list[dict[str, str]]] = []
    key: dict[str, list[str]] = {}
    n = 0
    for part in parts:
        rng.shuffle(part)
        items = []
        for arm, qid in part:
            iid = f"i{n:03d}"
            n += 1
            key[iid] = [arm, qid]
            items.append(
                {
                    "id": iid,
                    "question": by_id[qid]["question"],
                    "gold": by_id[qid]["gold"],
                    "generated": answers[arm][qid],
                }
            )
        out.append(items)
    return out, key


def unblind_pair(
    verdicts: dict[str, Any], key: dict[str, list[str]]
) -> dict[str, dict[str, bool | None]]:
    out: dict[str, dict[str, bool | None]] = {arm: {} for arm in ARMS}
    for iid, (arm, qid) in key.items():
        out[arm][qid] = (
            None
            if iid not in verdicts
            else str(verdicts[iid]).strip().upper().startswith("C")
        )
    return out


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p: the binomial test of the discordant pairs
    against one half."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def l1_dev_answers(
    rows: list[dict[str, Any]], path: Path, artifact: Path
) -> dict[str, str]:
    """L1's dev answers from its receipts, each checked against the
    hypothesis the L1 artifact recorded."""
    recorded = {
        r["question_id"]: r["hypothesis"]
        for r in json.loads(artifact.read_text(encoding="utf-8"))["rows"]
    }
    dev = {r["question_id"] for r in rows}
    got = {
        h["question_id"]: h["hypothesis"]
        for h in l1._jsonl(path)
        if h["question_id"] in dev
    }
    if set(got) != dev or any(got[q] != recorded[q] for q in dev):
        raise SystemExit("L1's dev answers differ from the L1 artifact's")
    return got


def cmd_blind(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    meta = l1._load_json(work / "meta.json")
    rows = meta["rows"]
    answers = {
        "l1": l1_dev_answers(rows, Path(args.l1_hypotheses), Path(args.l1_artifact)),
        "s1": {
            h["question_id"]: h["hypothesis"]
            for h in l1._jsonl(work / "hypotheses.jsonl")
        },
    }
    parts, key = blind_pair(answers, rows)
    grader = work / "grader"
    grader.mkdir(parents=True, exist_ok=True)
    rules = grader / "rules.txt"
    shutil.copyfile(l1.REFERENCE_RULES, rules)
    rules_text = rules.read_text(encoding="utf-8")
    specs = []
    for n, part in enumerate(parts):
        items = grader / f"items_{n}.txt"
        text = l1.render_items(part)
        items.write_text(text, encoding="utf-8")
        verdicts = grader / f"verdicts_{n}.json"
        specs.append(
            {
                "rules": str(rules),
                "rules_lines": l1.line_count(rules_text),
                "items": str(items),
                "items_lines": l1.line_count(text),
                "items_chars": len(text),
                "n": len(part),
                "verdicts": str(verdicts),
                "instructions": l1.grader_instructions(
                    rules, items, len(part), verdicts
                ),
            }
        )
        (grader / f"grader_{n}.txt").write_text(
            str(specs[-1]["instructions"]), encoding="utf-8"
        )
    l1._write_json(grader / "key.json", key)
    l1._write_json(
        grader / "meta.json",
        {"seed": BLIND_SEED, "rules_sha256": l1._sha(rules_text), "parts": specs},
    )
    print(
        json.dumps(
            [
                {k: s[k] for k in ("items", "n", "items_lines", "items_chars")}
                for s in specs
            ],
            indent=1,
        )
    )


def paired(
    verdicts: dict[str, dict[str, bool | None]], qids: list[str]
) -> dict[str, Any]:
    both = sum(bool(verdicts["l1"][q]) and bool(verdicts["s1"][q]) for q in qids)
    l1_only = sorted(q for q in qids if verdicts["l1"][q] and not verdicts["s1"][q])
    s1_only = sorted(q for q in qids if verdicts["s1"][q] and not verdicts["l1"][q])
    neither = sum(not verdicts["l1"][q] and not verdicts["s1"][q] for q in qids)
    return {
        "l1_correct": both + len(l1_only),
        "s1_correct": both + len(s1_only),
        "both": both,
        "l1_only": l1_only,
        "s1_only": s1_only,
        "neither": neither,
        "gain": len(s1_only) - len(l1_only),
        "mcnemar_exact_p": round(mcnemar_exact(len(l1_only), len(s1_only)), 6),
    }


def cmd_report(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    meta = l1._load_json(work / "meta.json")
    rows = meta["rows"]
    qids = [r["question_id"] for r in rows]
    answers = {
        h["question_id"]: h["hypothesis"] for h in l1._jsonl(work / "hypotheses.jsonl")
    }
    grader_meta = l1._load_json(work / "grader" / "meta.json")
    labels: dict[str, Any] = {}
    for part in grader_meta["parts"]:
        labels.update(l1._load_json(Path(part["verdicts"])))
    verdicts = unblind_pair(labels, l1._load_json(work / "grader" / "key.json"))
    unlabeled = sorted(q for arm in ARMS for q in qids if verdicts[arm].get(q) is None)
    readers = l1._load_json(work / "audit-readers.json")["summary"]
    graders = l1._load_json(work / "audit-graders.json")["summary"]
    l1_art = json.loads(Path(args.l1_artifact).read_text(encoding="utf-8"))
    l1_rows = {r["question_id"]: r for r in l1_art["rows"]}
    gpt4o_misses = sorted(
        q for q in qids if not l1_rows[q]["verdicts"][l1.STANDARD_JUDGE]
    )
    recorded = {q: l1_rows[q]["verdicts"]["continuity"] for q in qids}

    pair = paired(verdicts, qids)
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "l1": 0, "s1": 0})
    for r in rows:
        t = by_type[r["question_type"]]
        t["n"] += 1
        t["l1"] += bool(verdicts["l1"][r["question_id"]])
        t["s1"] += bool(verdicts["s1"][r["question_id"]])
    abstention = [r["question_id"] for r in rows if r["abstention"]]
    covered = [r["question_id"] for r in rows if r["covered"]]
    uncovered = [
        r["question_id"] for r in rows if r["answer_turns"] and not r["covered"]
    ]
    tokens_readers = counted_tokens(readers["usage"])
    tokens_graders = counted_tokens(graders["usage"])
    tokens_all = counted_tokens(readers["usage_all_attempts"]) + counted_tokens(
        graders["usage_all_attempts"]
    )
    tokens = tokens_readers + tokens_graders
    misses_right = sum(bool(verdicts["s1"][q]) for q in gpt4o_misses)
    regressions = len(pair["l1_only"])
    abst_right = sum(bool(verdicts["s1"][q]) for q in abstention)
    audit_ok = (
        readers["with_answers"] == readers["expected"]
        and readers["clean"] == readers["expected"]
        and graders["clean"] == graders["expected"]
        and readers["models"] == [l1.READER_MODEL]
    )
    stops = (
        readers["attempts_stopped_by_safeguard"]
        + graders["attempts_stopped_by_safeguard"]
    )
    v = l1._verdict
    s1n, gain = pair["s1_correct"], pair["gain"]
    predictions = [
        {
            "id": "S1-P1",
            "claim": "served contexts equal S0's cells, 150 of 150",
            "got": meta["parity"],
            "verdict": v(
                meta["parity"]["equal"] == 150, meta["parity"]["equal"] != 150
            ),
        },
        {
            "id": "S1-P2",
            "claim": "S1 correct on 143 to 148; MISSED at 140 or fewer",
            "got": s1n,
            "verdict": v(143 <= s1n <= 148, s1n <= 140),
        },
        {
            "id": "S1-P3",
            "claim": "paired gain +5 to +10; MISSED under +3",
            "got": gain,
            "verdict": v(5 <= gain <= 10, gain < 3),
        },
        {
            "id": "S1-P4",
            "claim": "at least 7 of L1's 12 dev GPT-4o misses correct; MISSED under 4",
            "got": misses_right,
            "verdict": v(misses_right >= 7, misses_right < 4),
        },
        {
            "id": "S1-P5",
            "claim": "at most 3 regressions; MISSED over 5",
            "got": regressions,
            "verdict": v(regressions <= 3, regressions > 5),
        },
        {
            "id": "S1-P6",
            "claim": "7 of 7 abstentions; MISSED under 6",
            "got": abst_right,
            "verdict": v(abst_right == len(abstention), abst_right < 6),
        },
        {
            "id": "S1-P7",
            "claim": "every counted batch clean, at most 2 safeguard stops",
            "got": {"audit_ok": audit_ok, "stops": stops},
            "verdict": v(audit_ok and stops <= 2, not audit_ok or stops > 2),
        },
        {
            "id": "S1-P8",
            "claim": "counted tokens 8M to 11M; MISSED over 12M",
            "got": tokens,
            "verdict": v(8_000_000 <= tokens <= 11_000_000, tokens > TOKEN_STOP),
        },
    ]
    l1._write_json(
        Path(args.out),
        {
            "unit": "S1",
            "declaration": "memory 01M3HXRQXE9EX4QXGA3BH70J2S",
            "provenance": l1._provenance(),
            "protocol": {
                "dataset": {
                    "file": str(l1.CORPUS.relative_to(_BENCH.parent)),
                    "sha256": l1.CORPUS_SHA256,
                },
                "split": "the dev 150 of aml_run.split",
                "memory": {
                    "arm": ARM.name,
                    "budget_chars": BUDGET,
                    "store": "bench/aml/.stores/coverage-s0-dev (S0's)",
                    "service": "bench/aml/service.py MemoryService, C0 options with the arm's order, fill, trim and a 200-round pool",
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
                "grader": "one blinded pass over L1's and S1's dev answers, each question's two in different parts, Claude Opus 5.5 subagents under the reader-swap rules.txt (AML's accuracy rules), seed "
                + str(BLIND_SEED),
                "no_api": "no API call; the official judges are not run on the dev",
            },
            "summary": {
                "n": len(qids),
                "s1_correct": pair["s1_correct"],
                "l1_correct_same_pass": pair["l1_correct"],
                "l1_continuity_recorded": sum(bool(x) for x in recorded.values()),
                "l1_recorded_vs_same_pass_agree": sum(
                    recorded[q] == verdicts["l1"][q] for q in qids
                ),
                "paired": pair,
                "by_type": dict(sorted(by_type.items())),
                "l1_gpt4o_dev_misses": {
                    "ids": gpt4o_misses,
                    "s1_correct": misses_right,
                },
                "abstention": {"n": len(abstention), "s1_correct": abst_right},
                "covered": {
                    "n": len(covered),
                    "s1_correct": sum(bool(verdicts["s1"][q]) for q in covered),
                },
                "uncovered": {
                    "ids": uncovered,
                    "s1_correct": sum(bool(verdicts["s1"][q]) for q in uncovered),
                },
                "unlabeled": unlabeled,
                "s2_gate_open": gain >= 3,
            },
            "audit": {"readers": readers, "graders": graders},
            "tokens": {
                "readers_counted": tokens_readers,
                "graders_counted": tokens_graders,
                "counted": tokens,
                "all_attempts": tokens_all,
                "stop": TOKEN_STOP,
            },
            "predictions": predictions,
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
                        )
                    },
                    "verdicts": {
                        "s1": verdicts["s1"][r["question_id"]],
                        "l1_same_pass": verdicts["l1"][r["question_id"]],
                        "l1_recorded": recorded[r["question_id"]],
                    },
                    "hypothesis": answers.get(r["question_id"]),
                }
                for r in rows
            ],
        },
    )
    print(
        json.dumps(
            {
                "summary": {
                    k: pair[k]
                    for k in ("s1_correct", "l1_correct", "gain", "mcnemar_exact_p")
                },
                "predictions": [(p["id"], p["got"], p["verdict"]) for p in predictions],
            },
            indent=1,
        )
    )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prompts")
    s.add_argument("--work", required=True)
    s.add_argument("--store", default=str(STORE))
    s.add_argument("--s0-artifact", default=str(S0_ARTIFACT))
    s.set_defaults(fn=cmd_prompts)
    s = sub.add_parser("collect")
    s.add_argument("--work", required=True)
    s.set_defaults(fn=l1.cmd_collect)
    s = sub.add_parser("audit")
    s.add_argument("--work", required=True)
    s.add_argument("--transcripts", required=True)
    s.add_argument("--role", choices=("readers", "graders"), default="readers")
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("blind")
    s.add_argument("--work", required=True)
    s.add_argument("--l1-hypotheses", default=str(L1_HYPOTHESES))
    s.add_argument("--l1-artifact", default=str(s0.L1_ARTIFACT))
    s.set_defaults(fn=cmd_blind)
    s = sub.add_parser("report")
    s.add_argument("--work", required=True)
    s.add_argument("--l1-artifact", default=str(s0.L1_ARTIFACT))
    s.add_argument("--out", default=str(OUT))
    s.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
