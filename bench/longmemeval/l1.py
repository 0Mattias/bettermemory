"""LongMemEval-S under the benchmark's own protocol, read by the chat model.

UNIT L1 (declared 2026-09-26, memory 01M3GEQVM7D1RX8H4SD3ZQJA9P). All 500
LongMemEval-S questions: bettermemory's served context, read by Claude as
the chat model under LongMemEval's own reading prompt, graded on
LongMemEval's own grading prompts by two judges.

THE MEMORY is the C0 configuration of bench/aml/service.py (rounds, engine
rank order, a 90,000-character budget, no model on the path), every
haystack ingested fresh into one store root, the question as the query.
It is the configuration the reader-swap proof's 150 prompts were served
with, and `parity` checks that the fresh store serves those 150 byte for
byte.

THE READING PROMPT is run_generation.py's default reading method, "con"
(step by step, no key expansion), at xiaowu0162/LongMemEval 9e0b455. Its
three slots take: History Chats, the served context (upstream renders its
own retriever's sessions there as JSON blocks; the served context replacing
that rendering is the declared deviation, and it is how a memory system
enters the benchmark); Current Date, the question's question_date; and
Question, the question.

THE READER is Claude as Claude Code subagents, six prompts each. The
prompt files are wrapped so that no line reaches the Read tool's
2,000-character truncation, and every reader's transcript is audited.

THE JUDGES, both on bench/judge/prompts.py's lme_prompt (evaluate_qa.py's
prompts, verbatim) and its parse rule ("yes" in the lowercased reply):

  google/gemini-3.8-flash   the judge of record, called as the 2026-09-22
                            validation called it (bench/judge/run.py
                            _judge_one, lme form), so its measured
                            agreement applies
  openai/gpt-4o-2024-08-06  the benchmark's own judge, called as
                            evaluate_qa.py calls it (temperature 0,
                            max_tokens 10); any comparison with another
                            system's number uses this column

STEPS, each a subcommand over one work directory:

  prompts  ingest, serve, fill and wrap; batches and reader instructions
  parity   the dev 150's served contexts against the reader-swap prompts
  collect  the readers' answer files into the official hypothesis file
  audit    every reader's or grader's transcript
  blind    the continuity grader's items, key and instructions
  price    the judges' prompt tokens, list prices, the key and credits
  judge    both judges on every answer (OPENROUTER_API_KEY)
  report   the artifact

Every process that opens a store runs with BETTERMEMORY_KEYS_DIR set; the
harness refuses otherwise, since each of the 500 stores writes a key.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

from aml import run as aml_run  # noqa: E402
from aml.service import SERVING_BUDGET_CHARS, MemoryService  # noqa: E402
from judge import run as judge_run  # noqa: E402
from judge.prompts import lme_prompt, parse_lme  # noqa: E402
from llm import BudgetExceeded, Client  # noqa: E402

CORPUS = aml_run.CORPUS
CORPUS_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
STORE = aml_run.STORES / "rounds-l1"
RESULTS = _HERE / "results"
RECEIPTS = Path("~/.cache/bettermemory-v9/receipts").expanduser()

UPSTREAM = "xiaowu0162/LongMemEval@9e0b455f4ef0e2ab8f2e582289761153549043fc"
UPSTREAM_GENERATION = (
    RECEIPTS / "l1-prep-2026-09-26" / "upstream-9e0b455" / "run_generation.py"
)
UPSTREAM_GENERATION_SHA256 = (
    "4f1eb3c69d7ad40f04065b9c0bc86f6582441018fc6ff751d162d66c95baf672"
)
REFERENCE = RECEIPTS / "reader-swap-2026-09-26"
REFERENCE_RULES = REFERENCE / "judge" / "rules.txt"

# run_generation.py prepare_prompt, a retriever's results, no key expansion,
# cot: the "con" reading method, run_generation.sh's default. Verbatim.
READING_TEMPLATE = "I will give you several history chats between you and a user. Please answer the question based on the relevant chat history. Answer the question step by step: first extract all the relevant information, and then reason over the information to get the answer.\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
READING_TEMPLATE_SHA256 = (
    "9e2b3110622929ab896696dd8937231c7436740ec3b9586f653f97346e19ab2c"
)
_PROMPT_HEAD = READING_TEMPLATE.partition("{}")[0]
_CONTEXT_END = "\n\nCurrent Date: "

# The served context inside a prompt rendered by AML's answer template
# (bench/aml/run.py ANSWER_TEMPLATE), which the reader-swap prompts used.
AML_OPEN = "Memories for user speaker 1:\n\n"
AML_CLOSE = "\n\nMemories for user speaker 2:"

# The Read tool shows a line of up to 2,000 characters whole and cuts the
# rest of a longer one.
READ_LINE_LIMIT = 2_000
WRAP_AT = 1_800

BATCH_SIZE = 6
BATCH_SEED = 20260927
BLIND_SEED = 20260928
READER_MODEL = "claude-opus-5-5"
READER_TOOLS = frozenset({"Read", "Write", "SubagentHandback"})

JUDGE_OF_RECORD = "google/gemini-3.8-flash"
STANDARD_JUDGE = "openai/gpt-4o-2024-08-06"
SPEND_CAP_USD = 5.0
# The reader-swap proof: Claude reading the C0 prompts, blinded Claude
# grader under AML's accuracy rules, dev 150 (130 of 150).
CONTINUITY_BASELINE = 0.867

MEMORY_TOOLS = (
    "memory_search, memory_show, memory_write, memory_update, memory_remove, "
    "memory_verify, memory_record_use, episode, memory_admin"
)

# The first version of the readers' instructions, kept because the first
# wave of readers ran under it. It described the reply as "the step-by-step
# reasoning and the answer", and 13 of its first 20 readers were stopped
# mid-run by the API's reasoning_extraction safeguard; a reader told after
# a stop not to repeat what it was writing is not reading under the
# protocol, so only a reader with no stop counts.
READER_INSTRUCTIONS_V1 = (
    "You are the answering model in a benchmark measurement. Rules: (1) Read "
    "only the files named here; do not list directories, search the filesystem "
    "or the repository, or open any other file. (2) Use no tool but Read, for "
    "those files, and one Write, for your answers file: no memory tools ("
    + MEMORY_TOOLS
    + "), no shell, no web access, no other agents. (3) Each prompt file is a "
    "complete, self-contained task for a different user: instructions, a chat "
    "history, the current date and one question, with the question on the last "
    "lines. Answer each independently, from that file alone, as its "
    "instructions ask: step by step, first extracting all the relevant "
    "information and then reasoning over it to the answer, and end with the "
    "answer. Keep each response under about 600 words.\n\n"
    "Step 1: Read {batch}; it lists up to 6 prompt file paths, one per line.\n"
    "Step 2: Read each prompt file in full with the Read tool (each is up to "
    "about 90,000 characters and 2,000 lines; if a read stops early, continue "
    "with offset until the end, because the question is on the last lines).\n"
    "Step 3: With the Write tool, write one JSON object mapping each prompt "
    "file's base name without .txt to your full response for that prompt (the "
    "step-by-step reasoning and the answer, as one string), to {answers}\n"
    "Reply with only: done."
)

# The instructions every later reader runs under: the reply is described
# as what it is, the answer each prompt asks for (its own template asks for
# it step by step), not as a transcript of the reader's reasoning.
READER_INSTRUCTIONS = (
    "You are the answering model in a benchmark measurement. Rules: (1) Read "
    "only the files named here; do not list directories, search the filesystem "
    "or the repository, or open any other file. (2) Use no tool but Read, for "
    "those files, and one Write, for your answers file: no memory tools ("
    + MEMORY_TOOLS
    + "), no shell, no web access, no other agents. (3) Each prompt file is a "
    "complete, self-contained task for a different user: instructions, a chat "
    "history, the current date and one question, with the question on the last "
    "lines. Answer each independently, from that file alone, in the form its "
    "instructions ask for. Keep each answer under about 600 words.\n\n"
    "Step 1: Read {batch}; it lists up to 6 prompt file paths, one per line.\n"
    "Step 2: Read each prompt file in full with the Read tool (each is up to "
    "about 90,000 characters and 2,000 lines; if a read stops early, continue "
    "with offset until the end, because the question is on the last lines).\n"
    "Step 3: With the Write tool, write one JSON object mapping each prompt "
    "file's base name without .txt to your answer to that prompt, as one "
    "string, to {answers}\n"
    "Reply with only: done."
)
READER_VERSIONS = {1: READER_INSTRUCTIONS_V1, 2: READER_INSTRUCTIONS}

GRADER_INSTRUCTIONS = (
    "You are grading answers in a benchmark. Rules: read only the two files "
    "named here; do not list directories, search the filesystem or the "
    "repository, or open any other file; no memory tools (" + MEMORY_TOOLS + "), "
    "no web access, no other agents.\n\n"
    "1. Read {rules}. It is the benchmark's grading prompt: apply its rules "
    "exactly, including the strict time-granularity rule and the rule that "
    "extra items in a list answer count as WRONG.\n"
    "2. Read {items} in full (if a read stops early, continue with offset until "
    "the end): {n} items in random order, each headed === ITEM <id> === and "
    "giving a question, a gold answer and a generated answer. A generated "
    "answer may reason step by step before it gives its answer; grade the "
    "answer it gives. Grade each item on its own: label it CORRECT or WRONG "
    "against the gold answer under those rules.\n"
    "3. With the Write tool, write one JSON object mapping every item id to "
    '"CORRECT" or "WRONG" to {verdicts} (all {n} ids, nothing else in the '
    "file).\n"
    "Reply with only: done."
)


def _sha(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- the prompt


def service_for(root: Path) -> MemoryService:
    """The C0 arm's configuration (bench/aml/results/dev-C0.json), the one
    the reader-swap proof's prompts were served with."""
    return MemoryService(
        root,
        fill="none",
        order="rank",
        annotate="none",
        serve=100,
        trim="none",
        budget=SERVING_BUDGET_CHARS,
        granularity="rounds",
        sheet="none",
        expand="none",
    )


def served_context(hits: list[dict[str, Any]]) -> str:
    """The served rounds as AML's answer template received them: their
    contents joined by newlines (bench/aml/run.py render_answer)."""
    return "\n".join(h["content"] for h in hits)


def reading_prompt(context: str, question_date: str, question: str) -> str:
    return READING_TEMPLATE.format(context, question_date, question)


def reading_context(prompt: str) -> str:
    """The served context inside a filled reading prompt."""
    return prompt[len(_PROMPT_HEAD) : prompt.rindex(_CONTEXT_END)]


def aml_served_context(prompt: str) -> str | None:
    """The served context inside a prompt rendered by AML's template, or
    None when the prompt is not one."""
    start = prompt.find(AML_OPEN)
    end = prompt.rfind(AML_CLOSE)
    if start < 0 or end < start + len(AML_OPEN):
        return None
    return prompt[start + len(AML_OPEN) : end]


def build(service: MemoryService, inst: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Ingest one question's haystack, serve its context, fill the reading
    prompt. Returns the question's row and the prompt."""
    hits = aml_run.ingest_and_search(service, inst)
    context = served_context(hits)
    prompt = reading_prompt(context, inst["question_date"], inst["question"])
    evidence = set(inst.get("answer_session_ids") or [])
    served = {h.get("_session", "") for h in hits}
    qid = inst["question_id"]
    return {
        "question_id": qid,
        "question_type": inst["question_type"],
        "abstention": "_abs" in qid,
        "n_hits": len(hits),
        "context_chars": len(context),
        "prompt_chars": len(prompt),
        "context_sha256": _sha(context),
        "prompt_sha256": _sha(prompt),
        "evidence_sessions": len(evidence),
        "evidence_served": len(evidence & served),
        "evidence_all_served": evidence <= served,
    }, prompt


def wrap_for_read(text: str) -> tuple[str, int]:
    """Break every line longer than the Read tool shows whole at its last
    space before WRAP_AT characters, the space becoming the newline, so the
    byte count is unchanged. A stretch with no space there gets a newline
    inserted. Returns the text and the number of inserted newlines."""
    out: list[str] = []
    inserted = 0
    for line in text.split("\n"):
        while len(line) > READ_LINE_LIMIT:
            cut = line.rfind(" ", 0, WRAP_AT)
            if cut < 0:
                out.append(line[:WRAP_AT])
                line = line[WRAP_AT:]
                inserted += 1
            else:
                out.append(line[:cut])
                line = line[cut + 1 :]
        out.append(line)
    return "\n".join(out), inserted


def line_count(text: str) -> int:
    """Lines as the Read tool numbers them."""
    return text.count("\n") + 1


# ---------------------------------------------------------------- batches


def cross_evidence(corpus: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(a, b) for every question a whose evidence session sits in question
    b's haystack. A reader holding both prompts could carry a's evidence
    into b's answer, so such questions never share a batch."""
    where: dict[str, set[str]] = defaultdict(set)
    for q in corpus:
        for sid in q["haystack_session_ids"]:
            where[sid].add(q["question_id"])
    return sorted(
        {
            (q["question_id"], other)
            for q in corpus
            for sid in q["answer_session_ids"]
            for other in where.get(sid, ())
            if other != q["question_id"]
        }
    )


def make_batches(
    qids: list[str],
    pairs: list[tuple[str, str]],
    size: int = BATCH_SIZE,
    seed: int = BATCH_SEED,
) -> list[list[str]]:
    """The questions in a seeded order, each into the first batch with room
    that holds none of its cross-evidence partners."""
    apart: dict[str, set[str]] = defaultdict(set)
    for a, b in pairs:
        apart[a].add(b)
        apart[b].add(a)
    order = sorted(qids)
    random.Random(seed).shuffle(order)
    batches: list[list[str]] = []
    for q in order:
        for batch in batches:
            if len(batch) < size and not apart[q] & set(batch):
                batch.append(q)
                break
        else:
            batches.append([q])
    return batches


def reader_instructions(batch_file: Path, answers_file: Path, version: int = 2) -> str:
    return READER_VERSIONS[version].format(batch=batch_file, answers=answers_file)


# ---------------------------------------------------------------- answers


def collect(
    batches: list[list[str]], answers_dir: Path
) -> tuple[dict[str, str], list[str]]:
    """Every batch's answers, stripped as the official generation strips a
    completion, and every problem found: a missing or unreadable file, a
    question with no answer or an empty one, an answer to a question the
    batch did not hold."""
    answers: dict[str, str] = {}
    problems: list[str] = []
    for i, batch in enumerate(batches):
        name = f"batch_{i:02d}.json"
        try:
            got = json.loads((answers_dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            problems.append(f"{name}: missing")
            continue
        except json.JSONDecodeError as exc:
            problems.append(f"{name}: not JSON ({exc})")
            continue
        if not isinstance(got, dict):
            problems.append(f"{name}: not a JSON object")
            continue
        for qid in batch:
            value = got.get(qid)
            if qid not in got:
                problems.append(f"{name}: no answer for {qid}")
            elif not isinstance(value, str) or not value.strip():
                problems.append(f"{name}: empty answer for {qid}")
            else:
                answers[qid] = value.strip()
        for qid in sorted(set(got) - set(batch)):
            problems.append(f"{name}: an answer for {qid}, which is not in the batch")
    return answers, problems


# ---------------------------------------------------------------- judges


def judge_item(row: dict[str, Any], hypothesis: str) -> dict[str, Any]:
    """The item shape bench/judge/run.py's _judge_one grades. Abstention is
    read from the id the way evaluate_qa.py reads it."""
    qid = row["question_id"]
    return {
        "item_id": qid,
        "question_type": row["question_type"],
        "question": row["question"],
        "gold": row["gold"],
        "response": hypothesis,
        "abstention": "_abs" in qid,
    }


async def judge_of_record(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    """Gemini, through the very function its 2026-09-22 validation ran:
    max_tokens 400, temperature 0, reasoning effort low, one retry at 1,600
    tokens on an empty reply."""
    got = await judge_run._judge_one(client, JUDGE_OF_RECORD, "lme", item)
    return {"verdict": got["verdict"], "raw": got["raw"], "cost": got["cost"]}


async def judge_standard(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    """GPT-4o as evaluate_qa.py calls it: temperature 0, max_tokens 10, the
    label "yes" in the stripped, lowercased reply."""
    prompt = lme_prompt(
        item["question_type"],
        item["question"],
        item["gold"],
        item["response"],
        item["abstention"],
    )
    out = await client.complete(
        STANDARD_JUDGE,
        [{"role": "user", "content": prompt}],
        max_tokens=10,
        temperature=0.0,
    )
    return {"verdict": parse_lme(out.text.strip()), "raw": out.text, "cost": out.cost}


# ---------------------------------------------------------------- analysis


def _rate(flags: list[bool]) -> dict[str, Any]:
    n = len(flags)
    return {
        "n": n,
        "correct": sum(flags),
        "accuracy": round(sum(flags) / n, 4) if n else None,
    }


def summarize(
    rows: list[dict[str, Any]], verdicts: dict[str, dict[str, bool | None]]
) -> dict[str, Any]:
    """Each grader's scores over every row. A missing or unparsed verdict
    counts as wrong, as evaluate_qa.py counts any reply without "yes".
    `overall` is the official plain mean over the questions; `category_mean`
    is the mean of the per-type scores, the average some vendors headline."""
    out: dict[str, Any] = {"judges": {}}
    types = sorted({r["question_type"] for r in rows})
    for name, got in verdicts.items():
        ok = {r["question_id"]: got.get(r["question_id"]) is True for r in rows}

        def rate(
            keep: Iterable[dict[str, Any]], ok: dict[str, bool] = ok
        ) -> dict[str, Any]:
            return _rate([ok[r["question_id"]] for r in keep])

        by_type = {t: rate(r for r in rows if r["question_type"] == t) for t in types}
        total = rate(rows)
        out["judges"][name] = {
            "n": total["n"],
            "correct": total["correct"],
            "overall": total["accuracy"],
            "category_mean": round(
                sum(v["accuracy"] for v in by_type.values()) / len(by_type), 4
            ),
            "by_type": by_type,
            "dev": rate(r for r in rows if r["split"] == "dev"),
            "holdout": rate(r for r in rows if r["split"] == "holdout"),
            "abstention": rate(r for r in rows if r["abstention"]),
            "non_abstention": rate(r for r in rows if not r["abstention"]),
            "evidence_all_served": rate(r for r in rows if r["evidence_all_served"]),
            "evidence_missing": rate(r for r in rows if not r["evidence_all_served"]),
        }
    if len(verdicts) == 2:
        a, b = (verdicts[name] for name in verdicts)
        differ = [
            r["question_id"]
            for r in rows
            if (a.get(r["question_id"]) is True) != (b.get(r["question_id"]) is True)
        ]
        out["agreement"] = {
            "n": len(rows),
            "agree": len(rows) - len(differ),
            "rate": round((len(rows) - len(differ)) / len(rows), 4),
        }
        out["disagreements"] = differ
    return out


# ---------------------------------------------------------------- audit

# What Claude Code tells a subagent whose reply an API safeguard stopped.
SAFEGUARD_STOP = "stopped by a safety classifier"
_ULID = re.compile(r"\b01[0-9A-HJKMNP-TV-Z]{24}\b")
_NUMBERED = re.compile(r"^(\d+)\t", re.MULTILINE)
_USAGE = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def audit_transcript(
    entries: list[dict[str, Any]],
    *,
    instructions: str,
    reads: set[str],
    full_reads: dict[str, int],
    write: str,
    model: str = READER_MODEL,
) -> dict[str, Any]:
    """One subagent's transcript against its instructions: the model that
    ran it, every tool call inside the allowed set (Read of the named files,
    one Write to the named file, the handback), every file in `full_reads`
    read to its last line (the line numbers the Read results carried), no
    context attachment but the standing instructions carrying memory text (a
    memory id, or any hook output), the answers file written, and no stop by
    an API safeguard: a reader stopped mid-reply is told not to produce the
    withheld text again, so what it writes afterwards is not a reading under
    the protocol."""
    first = next((e for e in entries if e.get("type") == "user"), {})
    first_text = _text((first.get("message") or {}).get("content"))
    models: set[str] = set()
    tools: dict[str, int] = defaultdict(int)
    violations: list[str] = []
    flagged: list[str] = []
    pending: dict[str, str] = {}
    seen: dict[str, set[int]] = defaultdict(set)
    counted: set[str] = set()
    usage = dict.fromkeys(_USAGE, 0)
    writes = 0
    write_ids: set[str] = set()
    wrote = False
    stops = 0
    api_errors = 0
    for e in entries:
        if e.get("isApiErrorMessage"):
            api_errors += 1
        if e.get("type") == "user" and SAFEGUARD_STOP in _text(
            (e.get("message") or {}).get("content")
        ):
            stops += 1
        if e.get("type") == "attachment":
            att = e.get("attachment") or {}
            kind = str(att.get("type", ""))
            if kind != "instructions" and (
                "hook" in kind or _ULID.search(json.dumps(att, ensure_ascii=False))
            ):
                flagged.append(kind)
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        if e.get("type") == "assistant":
            if msg.get("model"):
                models.add(str(msg["model"]))
            mid = str(msg.get("id") or "")
            if not mid or mid not in counted:
                counted.add(mid)
                for k in _USAGE:
                    usage[k] += int((msg.get("usage") or {}).get(k) or 0)
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                name = str(part.get("name"))
                tools[name] += 1
                args = part.get("input") or {}
                if name == "Read":
                    path = str(args.get("file_path", ""))
                    if path not in reads:
                        violations.append(f"Read of {path}")
                    pending[str(part.get("id"))] = path
                elif name == "Write":
                    writes += 1
                    write_ids.add(str(part.get("id")))
                    if args.get("file_path") != write:
                        violations.append(f"Write to {args.get('file_path')}")
                elif name not in READER_TOOLS:
                    violations.append(f"{name} call")
            elif part.get("type") == "tool_result":
                if str(part.get("tool_use_id")) in write_ids and not part.get(
                    "is_error"
                ):
                    wrote = True
                path = pending.pop(str(part.get("tool_use_id")), None)
                if path is not None and not part.get("is_error"):
                    seen[path].update(
                        int(n) for n in _NUMBERED.findall(_text(part.get("content")))
                    )
    if writes != 1:
        violations.append(f"{writes} writes")
    read_to_end = {
        path: set(range(1, n + 1)) <= seen.get(path, set())
        for path, n in full_reads.items()
    }
    matches = first_text == instructions
    return {
        "started": next((e["timestamp"] for e in entries if e.get("timestamp")), None),
        "instructions_match": matches,
        "models": sorted(models),
        "tools": dict(sorted(tools.items())),
        "violations": violations,
        "read_to_end": read_to_end,
        "flagged_attachments": flagged,
        "safeguard_stops": stops,
        "api_errors": api_errors,
        "wrote": wrote,
        "usage": usage,
        "clean": matches
        and models == {model}
        and not violations
        and all(read_to_end.values())
        and not flagged
        and stops == 0
        and api_errors == 0
        and wrote,
    }


# ---------------------------------------------------------------- the grader


def blind(
    answers: dict[str, str],
    rows: list[dict[str, Any]],
    parts: int,
    seed: int = BLIND_SEED,
) -> tuple[list[list[dict[str, str]]], dict[str, str]]:
    """Every answer under an opaque id in a seeded order, split into
    `parts` grader files; the key maps each id back to its question and is
    never shown to a grader."""
    by_id = {r["question_id"]: r for r in rows}
    order = sorted(answers)
    random.Random(seed).shuffle(order)
    items: list[dict[str, str]] = []
    key: dict[str, str] = {}
    for i, qid in enumerate(order):
        iid = f"i{i:03d}"
        key[iid] = qid
        items.append(
            {
                "id": iid,
                "question": by_id[qid]["question"],
                "gold": by_id[qid]["gold"],
                "generated": answers[qid],
            }
        )
    size = -(-len(items) // parts)
    return [items[k : k + size] for k in range(0, len(items), size)], key


def render_items(items: list[dict[str, str]]) -> str:
    """A grader's items as text wrapped for the Read tool; a JSON string
    holding a long answer is one line, which the Read tool would cut."""
    blocks = [
        f"=== ITEM {it['id']} ===\nQuestion: {it['question']}\n"
        f"Gold answer: {it['gold']}\nGenerated answer:\n{it['generated']}"
        for it in items
    ]
    return wrap_for_read("\n\n".join(blocks) + "\n")[0]


def grader_instructions(rules: Path, items: Path, n: int, verdicts: Path) -> str:
    return GRADER_INSTRUCTIONS.format(rules=rules, items=items, n=n, verdicts=verdicts)


def unblind(verdicts: dict[str, Any], key: dict[str, str]) -> dict[str, bool | None]:
    """Grader labels back onto questions; a missing label is None."""
    return {
        qid: None
        if iid not in verdicts
        else str(verdicts[iid]).strip().upper().startswith("C")
        for iid, qid in key.items()
    }


# ---------------------------------------------------------------- commands


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _corpus() -> list[dict[str, Any]]:
    data = CORPUS.read_bytes()
    if _sha(data) != CORPUS_SHA256:
        raise SystemExit(f"{CORPUS} does not hash to the declared {CORPUS_SHA256}")
    corpus: list[dict[str, Any]] = json.loads(data)
    return corpus


def cmd_prompts(args: argparse.Namespace) -> None:
    keys = os.environ.get("BETTERMEMORY_KEYS_DIR")
    if not keys:
        raise SystemExit(
            "set BETTERMEMORY_KEYS_DIR: each of the 500 stores writes a key, "
            "and they belong under the unit's receipts"
        )
    corpus = _corpus()
    root = Path(args.store)
    if root.exists() and any(root.iterdir()) and not args.resume:
        raise SystemExit(
            f"{root} is not empty; L1 ingests fresh (--resume finishes an interrupted ingest)"
        )
    work = Path(args.work).resolve()
    for sub in ("prompts", "prompts_wrapped", "batches", "readers", "answers"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    dev, _ = aml_run.split(corpus)
    dev_set = set(dev)
    service = service_for(root)
    t0 = time.time()
    done = 0

    def one(inst: dict[str, Any]) -> tuple[dict[str, Any], str]:
        nonlocal done
        got = build(service, inst)
        done += 1
        if done % 50 == 0:
            print(
                f"  {done} served, {time.time() - t0:.0f}s", file=sys.stderr, flush=True
            )
        return got

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        built = list(pool.map(one, corpus))
    seconds = round(time.time() - t0, 1)
    rows: list[dict[str, Any]] = []
    for (row, prompt), inst in zip(built, corpus, strict=True):
        qid = row["question_id"]
        wrapped, inserted = wrap_for_read(prompt)
        (work / "prompts" / f"{qid}.txt").write_bytes(prompt.encode("utf-8"))
        (work / "prompts_wrapped" / f"{qid}.txt").write_bytes(wrapped.encode("utf-8"))
        row.update(
            split="dev" if qid in dev_set else "holdout",
            question=inst["question"],
            gold=str(inst["answer"]),
            question_date=inst["question_date"],
            wrap_inserted=inserted,
            wrapped_lines=line_count(wrapped),
            longest_line=max(len(x) for x in wrapped.split("\n")),
        )
        rows.append(row)
    pairs = cross_evidence(corpus)
    batches = make_batches([r["question_id"] for r in rows], pairs)
    for i, batch in enumerate(batches):
        listing = "".join(f"{work / 'prompts_wrapped' / q}.txt\n" for q in batch)
        (work / "batches" / f"batch_{i:02d}.txt").write_text(listing, encoding="utf-8")
        (work / "readers" / f"reader_{i:02d}.txt").write_text(
            reader_instructions(
                work / "batches" / f"batch_{i:02d}.txt",
                work / "answers" / f"batch_{i:02d}.json",
            ),
            encoding="utf-8",
        )
    upstream_ok = (
        _sha(UPSTREAM_GENERATION.read_bytes()) == UPSTREAM_GENERATION_SHA256
        and repr(READING_TEMPLATE) in UPSTREAM_GENERATION.read_text(encoding="utf-8")
        if UPSTREAM_GENERATION.exists()
        else None
    )
    _write_json(
        work / "meta.json",
        {
            "corpus_sha256": CORPUS_SHA256,
            "reading_template_sha256": _sha(READING_TEMPLATE),
            "upstream": UPSTREAM,
            "upstream_file_checked": upstream_ok,
            "store": str(root),
            "keys_dir": keys,
            "seconds_ingest_serve": seconds,
            "cross_evidence": pairs,
            "batches": batches,
            "rows": rows,
        },
    )
    served = sum(r["evidence_all_served"] for r in rows)
    print(
        json.dumps(
            {
                "questions": len(rows),
                "seconds_ingest_serve": seconds,
                "mean_hits": round(sum(r["n_hits"] for r in rows) / len(rows), 1),
                "mean_prompt_chars": round(
                    sum(r["prompt_chars"] for r in rows) / len(rows)
                ),
                "max_prompt_chars": max(r["prompt_chars"] for r in rows),
                "max_wrapped_lines": max(r["wrapped_lines"] for r in rows),
                "longest_line": max(r["longest_line"] for r in rows),
                "wrap_inserted": sum(r["wrap_inserted"] for r in rows),
                "evidence_all_served": served,
                "evidence_all_served_dev": sum(
                    r["evidence_all_served"] for r in rows if r["split"] == "dev"
                ),
                "cross_evidence_pairs": len(pairs),
                "batches": len(batches),
                "batch_sizes": sorted({len(b) for b in batches}),
                "upstream_file_checked": upstream_ok,
            },
            indent=1,
        )
    )


def cmd_parity(args: argparse.Namespace) -> None:
    """L1-P1: the dev 150's served contexts from the fresh store against the
    reader-swap reference prompts, read between AML's memory markers, after
    the reference files are checked against their recorded hashes."""
    work = Path(args.work)
    reference = Path(args.reference)
    sums = {}
    for line in (reference / "prompts.sha256").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.strip().partition("  ")
        sums[name] = digest
    rows = [r for r in _load_json(work / "meta.json")["rows"] if r["split"] == "dev"]
    equal, mismatches, bad_reference = 0, [], []
    for r in rows:
        qid = r["question_id"]
        raw = (reference / "prompts" / f"{qid}.txt").read_bytes()
        if sums.get(f"{qid}.txt") != _sha(raw):
            bad_reference.append(qid)
        theirs = aml_served_context(raw.decode("utf-8"))
        ours = reading_context(
            (work / "prompts" / f"{qid}.txt").read_text(encoding="utf-8")
        )
        if theirs == ours:
            equal += 1
            continue
        first = next(
            (i for i, (x, y) in enumerate(zip(theirs or "", ours)) if x != y),
            min(len(theirs or ""), len(ours)),
        )
        mismatches.append(
            {
                "question_id": qid,
                "reference_chars": None if theirs is None else len(theirs),
                "fresh_chars": len(ours),
                "first_difference": first,
            }
        )
    result = {
        "n": len(rows),
        "equal": equal,
        "mismatches": mismatches,
        "reference_hash_failures": bad_reference,
    }
    _write_json(work / "parity.json", result)
    print(
        json.dumps({k: v if k != "mismatches" else len(v) for k, v in result.items()})
    )
    if mismatches or bad_reference:
        raise SystemExit("parity failed: the run stops until it is explained")


def cmd_collect(args: argparse.Namespace) -> None:
    work = Path(args.work)
    meta = _load_json(work / "meta.json")
    answers, problems = collect(meta["batches"], work / "answers")
    for p in problems:
        print("  problem:", p, file=sys.stderr)
    if problems:
        raise SystemExit(f"{len(problems)} problems; no hypothesis file written")
    with (work / "hypotheses.jsonl").open("w", encoding="utf-8") as fh:
        for r in meta["rows"]:
            qid = r["question_id"]
            fh.write(
                json.dumps(
                    {"question_id": qid, "hypothesis": answers[qid]}, ensure_ascii=False
                )
                + "\n"
            )
    words = sorted(len(a.split()) for a in answers.values())
    print(
        json.dumps(
            {
                "answers": len(answers),
                "words_median": words[len(words) // 2],
                "words_max": words[-1],
                "over_600_words": sum(w > 600 for w in words),
            }
        )
    )


def _jsonl(path: Path) -> list[Any]:
    """A JSON-lines file, split on newlines only: str.splitlines also
    breaks at U+2028, U+0085 and the other Unicode line boundaries, which
    json.dumps leaves raw inside strings, and a record cut there would be
    dropped as unparseable. A line that is still not JSON (a transcript
    being written) is skipped."""
    out = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _transcripts(directory: Path) -> list[tuple[Path, list[dict[str, Any]]]]:
    return [(path, _jsonl(path)) for path in sorted(directory.glob("agent-*.jsonl"))]


def _first_user_text(entries: list[dict[str, Any]]) -> str:
    first = next((e for e in entries if e.get("type") == "user"), {})
    return _text((first.get("message") or {}).get("content"))


def cmd_audit(args: argparse.Namespace) -> None:
    """Every attempt at every batch (a batch is rerun until an attempt is
    clean), matched to its batch by its instructions under either version.
    The canonical attempt of a batch is the last one whose Write stands; the
    batch counts only when that attempt is clean."""
    work = Path(args.work).resolve()
    meta = _load_json(work / "meta.json")
    rows = {r["question_id"]: r for r in meta["rows"]}
    specs: dict[str, dict[str, Any]] = {}
    by_text: dict[str, tuple[str, int]] = {}
    if args.role == "readers":
        for i, batch in enumerate(meta["batches"]):
            name = f"batch_{i:02d}"
            batch_file = work / "batches" / f"{name}.txt"
            answers_file = work / "answers" / f"{name}.json"
            prompt_files = {
                str(work / "prompts_wrapped" / f"{q}.txt"): rows[q]["wrapped_lines"]
                for q in batch
            }
            specs[name] = {
                "reads": {str(batch_file), *prompt_files},
                "full_reads": prompt_files,
                "write": str(answers_file),
            }
            for version in READER_VERSIONS:
                text = reader_instructions(batch_file, answers_file, version)
                by_text[text] = (name, version)
    else:
        grader = _load_json(work / "grader" / "meta.json")
        for part in grader["parts"]:
            files = {
                part["rules"]: part["rules_lines"],
                part["items"]: part["items_lines"],
            }
            name = Path(part["items"]).stem
            specs[name] = {
                "reads": set(files),
                "full_reads": files,
                "write": part["verdicts"],
            }
            by_text[part["instructions"]] = (name, 1)
    attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path, entries in _transcripts(Path(args.transcripts)):
        text = _first_user_text(entries)
        if text not in by_text:
            continue
        name, version = by_text[text]
        spec = specs[name]
        got = audit_transcript(
            entries,
            instructions=text,
            reads=spec["reads"],
            full_reads=spec["full_reads"],
            write=spec["write"],
        )
        got.update(transcript=path.name, version=version)
        attempts[name].append(got)
    canonical: dict[str, dict[str, Any]] = {}
    for name, runs in attempts.items():
        runs.sort(key=lambda g: g["started"] or "")
        writers = [g for g in runs if g["wrote"]]
        if writers:
            canonical[name] = writers[-1]
    every = [g for runs in attempts.values() for g in runs]

    def total(runs: Iterable[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = dict.fromkeys(_USAGE, 0)
        for g in runs:
            for k in _USAGE:
                out[k] += g["usage"][k]
        return out

    kept = list(canonical.values())
    summary = {
        "role": args.role,
        "expected": len(specs),
        "with_answers": len(canonical),
        "missing": sorted(set(specs) - set(canonical)),
        "clean": sum(g["clean"] for g in kept),
        "not_clean": sorted(n for n, g in canonical.items() if not g["clean"]),
        "versions": {
            v: sum(g["version"] == v for g in kept)
            for v in sorted({g["version"] for g in kept})
        },
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
    _write_json(
        work / f"audit-{args.role}.json",
        {"summary": summary, "canonical": canonical, "attempts": attempts},
    )
    print(json.dumps(summary, indent=1))


def cmd_blind(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    meta = _load_json(work / "meta.json")
    answers = {
        h["question_id"]: h["hypothesis"] for h in _jsonl(work / "hypotheses.jsonl")
    }
    parts, key = blind(answers, meta["rows"], parts=args.parts)
    grader = work / "grader"
    grader.mkdir(parents=True, exist_ok=True)
    rules = grader / "rules.txt"
    shutil.copyfile(REFERENCE_RULES, rules)
    rules_text = rules.read_text(encoding="utf-8")
    specs = []
    for n, part in enumerate(parts):
        items = grader / f"items_{n}.txt"
        text = render_items(part)
        items.write_text(text, encoding="utf-8")
        verdicts = grader / f"verdicts_{n}.json"
        specs.append(
            {
                "rules": str(rules),
                "rules_lines": line_count(rules_text),
                "items": str(items),
                "items_lines": line_count(text),
                "items_chars": len(text),
                "n": len(part),
                "verdicts": str(verdicts),
                "instructions": grader_instructions(rules, items, len(part), verdicts),
            }
        )
        (grader / f"grader_{n}.txt").write_text(
            specs[-1]["instructions"], encoding="utf-8"
        )
    _write_json(grader / "key.json", key)
    _write_json(
        grader / "meta.json",
        {"seed": BLIND_SEED, "rules_sha256": _sha(rules_text), "parts": specs},
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


def _judge_items(work: Path) -> list[dict[str, Any]]:
    meta = _load_json(work / "meta.json")
    answers = {
        h["question_id"]: h["hypothesis"] for h in _jsonl(work / "hypotheses.jsonl")
    }
    return [judge_item(r, answers[r["question_id"]]) for r in meta["rows"]]


# What the price check keeps from /api/v1/key and /api/v1/credits: the
# spending numbers only. The replies also carry the key's label and the
# account's user, organisation and workspace ids, which no file here keeps.
ACCOUNT_FIELDS = {
    "key": ("limit", "limit_remaining", "usage", "is_free_tier"),
    "credits": ("total_credits", "total_usage"),
}


_COUNT = (
    "import json, sys, tiktoken; e = tiktoken.get_encoding('o200k_base'); "
    "print(sum(len(e.encode(t)) for t in json.load(sys.stdin)))"
)


def _count_tokens(texts: list[str]) -> tuple[int, str]:
    """GPT-4o's token count of `texts` (o200k_base), from tiktoken in a
    throwaway `uv run --no-project` environment so the dev venv is not
    changed; characters over four when that cannot run. Gemini's tokenizer
    is not public, so its count is taken as the same; the billed counts
    come back with the judgments."""
    try:
        done = subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--quiet",
                "--with",
                "tiktoken",
                "python",
                "-c",
                _COUNT,
            ],
            input=json.dumps(texts),
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )
        return int(done.stdout.strip()), "tiktoken o200k_base"
    except (OSError, subprocess.SubprocessError, ValueError):
        return sum(len(t) for t in texts) // 4, "characters / 4"


def cmd_price(args: argparse.Namespace) -> None:
    """The judges' input tokens (GPT-4o's own tokenizer, see _count_tokens),
    today's list prices from
    OpenRouter's public model list, a bound on the cost of both judges, and,
    when OPENROUTER_API_KEY is set, the key's limit and the account's
    credits. The key itself is never printed or written."""
    import httpx

    work = Path(args.work)
    items = _judge_items(work)
    texts = [
        lme_prompt(
            it["question_type"],
            it["question"],
            it["gold"],
            it["response"],
            it["abstention"],
        )
        for it in items
    ]
    tokens, method = _count_tokens(texts)
    listing = httpx.get("https://openrouter.ai/api/v1/models", timeout=30).json()[
        "data"
    ]
    prices = {
        m["id"]: {k: float(m["pricing"][k]) for k in ("prompt", "completion")}
        for m in listing
        if m["id"] in (JUDGE_OF_RECORD, STANDARD_JUDGE)
    }
    out_bound = {JUDGE_OF_RECORD: 400, STANDARD_JUDGE: 10}
    bound = {
        model: round(
            tokens * prices[model]["prompt"]
            + len(items) * out_bound[model] * prices[model]["completion"],
            4,
        )
        for model in prices
    }
    result: dict[str, Any] = {
        "judge_calls_per_judge": len(items),
        "input_tokens_per_judge": tokens,
        "token_method": method,
        "prices_per_million": {
            m: {k: round(v * 1e6, 4) for k, v in p.items()} for m, p in prices.items()
        },
        "max_output_tokens_per_call": out_bound,
        "cost_bound_usd": bound,
        "cost_bound_total_usd": round(sum(bound.values()), 4),
        "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        headers = {"Authorization": f"Bearer {key}"}
        for name, fields in ACCOUNT_FIELDS.items():
            resp = httpx.get(
                f"https://openrouter.ai/api/v1/{name}", headers=headers, timeout=30
            )
            data = resp.json().get("data") or {} if resp.status_code == 200 else {}
            result[name] = {
                k: data.get(k) for k in fields
            } or f"http {resp.status_code}"
        remaining = [
            result["key"].get("limit_remaining"),
            (result["credits"].get("total_credits") or 0)
            - (result["credits"].get("total_usage") or 0),
        ]
        result["room_for_cap"] = all(r is None or r >= SPEND_CAP_USD for r in remaining)
    _write_json(work / "price.json", result)
    print(json.dumps(result, indent=1))


async def _judge_all(items: list[dict[str, Any]], budget: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with Client(budget_usd=budget, concurrency=16) as client:
        for name, fn in (("record", judge_of_record), ("standard", judge_standard)):
            before, calls, hits = client.spent_usd, client.calls, client.cache_hits
            got = await asyncio.gather(
                *(fn(client, it) for it in items), return_exceptions=True
            )
            out[name] = {
                "model": JUDGE_OF_RECORD if name == "record" else STANDARD_JUDGE,
                "spent_usd": round(client.spent_usd - before, 6),
                "calls": client.calls - calls,
                "cache_hits": client.cache_hits - hits,
                "rows": {
                    it["item_id"]: r
                    for it, r in zip(items, got, strict=True)
                    if isinstance(r, dict)
                },
                "errors": [
                    f"{it['item_id']}: {r!r}"
                    for it, r in zip(items, got, strict=True)
                    if isinstance(r, BaseException)
                ],
                "budget_exceeded": any(isinstance(r, BudgetExceeded) for r in got),
            }
    return out


def cmd_judge(args: argparse.Namespace) -> None:
    work = Path(args.work)
    result = asyncio.run(_judge_all(_judge_items(work), args.budget))
    _write_json(work / "judgments.json", result)
    for name, got in result.items():
        n = len(got["rows"])
        yes = sum(r["verdict"] is True for r in got["rows"].values())
        print(
            f"{name} {got['model']}: {yes}/{n} correct, {len(got['errors'])} errors, "
            f"calls {got['calls']}, cache hits {got['cache_hits']}, ${got['spent_usd']:.4f}"
        )


def _provenance() -> dict[str, Any]:
    def git(*argv: str) -> str:
        return subprocess.run(
            ["git", *argv], capture_output=True, text=True, cwd=str(_HERE), timeout=10
        ).stdout.strip()

    import bettermemory

    return {
        "bettermemory_version": bettermemory.__version__,
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "tree_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _verdict(hit: bool, missed: bool) -> str:
    return "MISSED" if missed else "HIT" if hit else "HIT (outside the point range)"


def cmd_report(args: argparse.Namespace) -> None:
    work = Path(args.work)
    meta = _load_json(work / "meta.json")
    rows = meta["rows"]
    answers = {
        h["question_id"]: h["hypothesis"] for h in _jsonl(work / "hypotheses.jsonl")
    }
    judged = _load_json(work / "judgments.json")
    record = {q: r["verdict"] for q, r in judged["record"]["rows"].items()}
    standard = {q: r["verdict"] for q, r in judged["standard"]["rows"].items()}
    grader_meta = _load_json(work / "grader" / "meta.json")
    labels: dict[str, Any] = {}
    for part in grader_meta["parts"]:
        labels.update(_load_json(Path(part["verdicts"])))
    continuity = unblind(labels, _load_json(work / "grader" / "key.json"))
    parity = _load_json(work / "parity.json")
    reader_audit = _load_json(work / "audit-readers.json")
    readers = reader_audit["summary"]
    under_v1 = sorted(
        name for name, g in reader_audit["canonical"].items() if g["version"] == 1
    )
    graders = _load_json(work / "audit-graders.json")["summary"]
    price = _load_json(work / "price.json")

    summary = summarize(rows, {JUDGE_OF_RECORD: record, STANDARD_JUDGE: standard})
    cont = summarize(rows, {"continuity": continuity})["judges"]["continuity"]
    rec = summary["judges"][JUDGE_OF_RECORD]
    dev_rows = [r for r in rows if r["split"] == "dev"]
    served_dev = sum(r["evidence_all_served"] for r in dev_rows)
    share = sum(r["evidence_all_served"] for r in rows) / len(rows)
    judge_cost = judged["record"]["spent_usd"] + judged["standard"]["spent_usd"]
    audit_ok = (
        readers["with_answers"] == readers["expected"]
        and readers["clean"] == readers["expected"]
        and readers["violations"] == 0
        and readers["files_not_read_to_end"] == 0
        and readers["flagged_attachments"] == 0
        and readers["models"] == [READER_MODEL]
    )
    predictions = [
        {
            "id": "L1-P1",
            "claim": "all 150 dev served contexts byte-identical to the reader-swap prompts",
            "value": f"{parity['equal']} of {parity['n']}",
            "verdict": _verdict(
                parity["equal"] == parity["n"], parity["equal"] != parity["n"]
            ),
        },
        {
            "id": "L1-P2",
            "claim": "overall under the judge of record 0.91, range 0.88 to 0.94; MISSED if below 0.87 or above 0.95",
            "value": rec["overall"],
            "verdict": _verdict(
                0.88 <= rec["overall"] <= 0.94, not 0.87 <= rec["overall"] <= 0.95
            ),
        },
        {
            "id": "L1-P3",
            "claim": "the two judges agree on at least 95% of the 500; MISSED if below 92%",
            "value": summary["agreement"]["rate"],
            "verdict": _verdict(
                summary["agreement"]["rate"] >= 0.95,
                summary["agreement"]["rate"] < 0.92,
            ),
        },
        {
            "id": "L1-P4",
            "claim": "temporal-reasoning under the judge of record at least 0.84; MISSED if below 0.80",
            "value": rec["by_type"]["temporal-reasoning"]["accuracy"],
            "verdict": _verdict(
                rec["by_type"]["temporal-reasoning"]["accuracy"] >= 0.84,
                rec["by_type"]["temporal-reasoning"]["accuracy"] < 0.80,
            ),
        },
        {
            "id": "L1-P5",
            "claim": "the dev 150 under the continuity grader at least 0.88 (0.867 at the reader-swap); MISSED if below 0.85",
            "value": cont["dev"]["accuracy"],
            "verdict": _verdict(
                cont["dev"]["accuracy"] >= 0.88, cont["dev"]["accuracy"] < 0.85
            ),
        },
        {
            "id": "L1-P6",
            "claim": "every evidence session served for at least 92% of questions (140 of 150 dev), accuracy on those at least 0.92 under the judge of record; MISSED if the share is below 90%",
            "value": {
                "share": round(share, 4),
                "dev_all_served": f"{served_dev} of {len(dev_rows)}",
                "accuracy_when_served": rec["evidence_all_served"]["accuracy"],
            },
            "verdict": _verdict(
                share >= 0.92 and rec["evidence_all_served"]["accuracy"] >= 0.92,
                share < 0.90,
            ),
        },
        {
            "id": "L1-P7",
            "claim": "both judges under $3; MISSED if over $5",
            "value": round(judge_cost, 4),
            "verdict": _verdict(judge_cost < 3, judge_cost > 5),
        },
        {
            "id": "L1-P8",
            "claim": "0 reader tool calls outside the allowed set, every prompt read to its last line, no memory text injected",
            "value": {
                k: readers[k]
                for k in (
                    "expected",
                    "with_answers",
                    "clean",
                    "attempts",
                    "attempts_stopped_by_safeguard",
                    "violations",
                    "files_not_read_to_end",
                    "flagged_attachments",
                    "models",
                )
            },
            "verdict": _verdict(audit_ok, not audit_ok),
        },
    ]
    by_id = {r["question_id"]: r for r in rows}
    disagreements = [
        {
            "question_id": q,
            "question_type": by_id[q]["question_type"],
            JUDGE_OF_RECORD: record.get(q),
            STANDARD_JUDGE: standard.get(q),
            "question": by_id[q]["question"],
            "gold": by_id[q]["gold"],
            "response": answers[q],
        }
        for q in summary["disagreements"]
    ]
    artifact = {
        "unit": "L1",
        "declaration": "memory 01M3GEQVM7D1RX8H4SD3ZQJA9P",
        "provenance": _provenance(),
        "protocol": {
            "dataset": {
                "file": "bench/longmemeval/data/longmemeval_s_cleaned.json",
                "sha256": CORPUS_SHA256,
                "questions": len(rows),
            },
            "upstream": UPSTREAM,
            "reading_prompt": "run_generation.py, reading method con (cot true, no key expansion)",
            "reading_template_sha256": READING_TEMPLATE_SHA256,
            "declared_deviation": "History Chats holds bettermemory's served context where upstream renders its own retriever's sessions as JSON blocks sorted by date",
            "memory": {
                "service": "bench/aml/service.py MemoryService, the C0 configuration",
                "fill": "none",
                "order": "rank",
                "annotate": "none",
                "serve": 100,
                "trim": "none",
                "budget_chars": SERVING_BUDGET_CHARS,
                "granularity": "rounds",
                "top_k": aml_run.TOP_K,
                "store": "bench/aml/.stores/rounds-l1, ingested fresh",
                "seconds_ingest_serve": meta["seconds_ingest_serve"],
            },
            "reader": {
                "model": READER_MODEL,
                "harness": "Claude Code subagents (general-purpose), six prompts each",
                "batches": len(meta["batches"]),
                "batch_seed": BATCH_SEED,
                "cross_evidence_directed_pairs": len(meta["cross_evidence"]),
                "wrap": f"lines over {READ_LINE_LIMIT} characters broken at the last space before {WRAP_AT}",
                "wrap_inserted_newlines": sum(r["wrap_inserted"] for r in rows),
                "instructions": {
                    "v1_sha256": _sha(READER_INSTRUCTIONS_V1),
                    "v2_sha256": _sha(READER_INSTRUCTIONS),
                    "batches_counted_under_v1": under_v1,
                    "why_two": "v1 described the reply as 'the step-by-step reasoning and the answer'; the API's reasoning_extraction safeguard stopped 17 of its 20 readers, and a stopped reader is not counted. v2 describes the reply as the answer each prompt asks for (the prompt itself asks for it step by step); none of its readers was stopped. Every batch counts once, from its last clean attempt.",
                    "attempts": readers["attempts"],
                    "attempts_stopped_by_safeguard": readers[
                        "attempts_stopped_by_safeguard"
                    ],
                },
            },
            "judges": {
                JUDGE_OF_RECORD: "of record; bench/judge/run.py _judge_one, lme form: max_tokens 400, temperature 0, reasoning effort low, one retry at 1,600 tokens on an empty reply",
                STANDARD_JUDGE: "the benchmark's own, as evaluate_qa.py calls it: temperature 0, max_tokens 10, 'yes' in the lowercased reply",
            },
            "continuity_grader": "blinded Claude Opus 5.5 subagents under AML's accuracy rules (reader-swap judge/rules.txt), told to grade the answer a step-by-step response gives",
        },
        "summary": summary,
        "continuity": {
            "scores": cont,
            "reader_swap_dev_150": CONTINUITY_BASELINE,
            "what_changed": [
                "the reading prompt: LongMemEval's step-by-step template in place of AML's shortest-answer template",
                "the question date, which AML's template does not carry",
                "the graded text: a step-by-step response in place of a short answer",
            ],
        },
        "parity": parity,
        "audit": {"readers": readers, "graders": graders},
        "cost": {
            "judges_usd": {
                JUDGE_OF_RECORD: judged["record"]["spent_usd"],
                STANDARD_JUDGE: judged["standard"]["spent_usd"],
            },
            "judge_calls": {
                JUDGE_OF_RECORD: judged["record"]["calls"],
                STANDARD_JUDGE: judged["standard"]["calls"],
            },
            # The account's spending numbers stay in the receipts; the
            # artifact keeps that the room under the cap was checked.
            "price_check": {k: v for k, v in price.items() if k not in ACCOUNT_FIELDS},
            "subagent_tokens": {
                "readers_counted": readers["usage"],
                "readers_all_attempts": readers["usage_all_attempts"],
                "graders_counted": graders["usage"],
                "graders_all_attempts": graders["usage_all_attempts"],
            },
        },
        "predictions": predictions,
        "disagreements": disagreements,
        "rows": [
            {
                **{
                    k: r[k]
                    for k in (
                        "question_id",
                        "question_type",
                        "split",
                        "abstention",
                        "n_hits",
                        "prompt_chars",
                        "prompt_sha256",
                        "evidence_sessions",
                        "evidence_served",
                        "evidence_all_served",
                    )
                },
                "verdicts": {
                    JUDGE_OF_RECORD: record.get(r["question_id"]),
                    STANDARD_JUDGE: standard.get(r["question_id"]),
                    "continuity": continuity.get(r["question_id"]),
                },
                "hypothesis": answers[r["question_id"]],
            }
            for r in rows
        ],
    }
    out = Path(args.out)
    _write_json(out, artifact)
    print(
        json.dumps(
            {"predictions": [(p["id"], p["value"], p["verdict"]) for p in predictions]},
            indent=1,
        )
    )
    print(
        json.dumps(
            {
                k: {x: v[x] for x in ("overall", "category_mean")}
                for k, v in summary["judges"].items()
            }
        )
    )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prompts")
    s.add_argument("--work", required=True)
    s.add_argument("--store", default=str(STORE))
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--resume", action="store_true")
    s.set_defaults(fn=cmd_prompts)
    s = sub.add_parser("parity")
    s.add_argument("--work", required=True)
    s.add_argument("--reference", default=str(REFERENCE))
    s.set_defaults(fn=cmd_parity)
    s = sub.add_parser("collect")
    s.add_argument("--work", required=True)
    s.set_defaults(fn=cmd_collect)
    s = sub.add_parser("audit")
    s.add_argument("--work", required=True)
    s.add_argument("--transcripts", required=True)
    s.add_argument("--role", choices=("readers", "graders"), default="readers")
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("blind")
    s.add_argument("--work", required=True)
    s.add_argument("--parts", type=int, default=2)
    s.set_defaults(fn=cmd_blind)
    s = sub.add_parser("price")
    s.add_argument("--work", required=True)
    s.set_defaults(fn=cmd_price)
    s = sub.add_parser("judge")
    s.add_argument("--work", required=True)
    s.add_argument("--budget", type=float, default=SPEND_CAP_USD)
    s.set_defaults(fn=cmd_judge)
    s = sub.add_parser("report")
    s.add_argument("--work", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
