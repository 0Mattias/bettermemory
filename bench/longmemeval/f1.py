"""LongMemEval-S's full-context control: the chat model given the whole history.

UNIT F1 (declared 2026-09-27, memory 01M3J68RY2E06P46T87WYSZ1GT; the
owner's go, memory 01M3J4ZKE36JZ9S5RX37Q0NMFB). S2 read the holdout 350
with bettermemory's served context, about 28% of an S haystack, and a
skeptic's first question is how the same reader does with the whole
history and no memory, since an S history fits the reader's window. F1
answers it on a stratified 150 of the holdout: Claude Opus 5.5 given each
question's whole history in LongMemEval's own full-history format, graded
by both official judges and paired with S2's answers on the same 150.

THE PROMPT is run_generation.sh's long-context baseline at
xiaowu0162/LongMemEval 9e0b455: retriever full-history-session (the
orig-session path of prepare_prompt), TOPK 1000 as the README recommends,
history format json, useronly false, reading method con. Every session in
date order, has_answer dropped, each one a "### Session" block holding the
session as json.dumps renders it, inside L1's reading template. Each prompt
is checked byte for byte against the pinned upstream prepare_prompt itself.
The one deviation: upstream truncates the history to its reader's window
(126,200 o200k tokens for gpt-4o); here the reader holds it whole.

What stays S2's: the reader and its wording (S1's, with the stated file
size set to the measured maximum; "up to 4 prompt file paths" holds for
one), the wrap for the Read tool, the audit, the judges. One prompt a
reader, since one history is about three of S2's batches.

STEPS, each a subcommand over one work directory:

  prompts  the stratified 150, their prompts, the upstream check, wrap,
           one batch each, reader instructions
  collect  the readers' answer files into the hypothesis file (L1's step)
  audit    every reader's transcript (S1's step) and the token projection
  report   the artifact, after L1's `price` and `judge` steps

Nothing here opens a store: the context is the history itself.
"""

from __future__ import annotations

import argparse
import ast
import copy
import json
import random
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_BENCH = _HERE.parent
for _path in (_BENCH, _HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import h1  # noqa: E402
import l1  # noqa: E402
import s1  # noqa: E402
import s2  # noqa: E402
from aml import run as aml_run  # noqa: E402

RECEIPTS = l1.RECEIPTS / "f1-2026-09-27"
WORK = RECEIPTS / "work"
S2_WORK = h1.S2_WORK
S2_ARTIFACT = s2.OUT
OUT = l1.RESULTS / "f1-full-context-2026-09-27.json"
N = 150
SAMPLE_SEED = 20260928
BATCH_SIZE = 1
TOKEN_STOP = 42_000_000
JUDGES = s2.JUDGES
# run_generation.sh full-history-session with the README's recommendations.
TOPK = 1000
SESSION_BLOCK = "\n### Session {}:\nSession Date: {}\nSession Content:\n{}\n"


# ---------------------------------------------------------------- the sample


def quotas(counts: dict[str, int], n: int) -> dict[str, int]:
    """n split over the types in proportion to their counts, by largest
    remainder, ties to the type that sorts first."""
    total = sum(counts.values())
    exact = {t: n * c / total for t, c in counts.items()}
    out = {t: int(x) for t, x in exact.items()}
    left = n - sum(out.values())
    for t in sorted(exact, key=lambda t: (-(exact[t] - out[t]), t))[:left]:
        out[t] += 1
    return out


def sample(
    holdout: list[dict[str, Any]], n: int = N, seed: int = SAMPLE_SEED
) -> list[str]:
    """The stratified sample: each type's quota drawn from its sorted ids
    by one seeded generator, the types in sorted order; returned in the
    holdout's order."""
    by_type: dict[str, list[str]] = defaultdict(list)
    for inst in holdout:
        by_type[inst["question_type"]].append(inst["question_id"])
    want = quotas({t: len(ids) for t, ids in by_type.items()}, n)
    rng = random.Random(seed)
    keep: set[str] = set()
    for t in sorted(by_type):
        keep |= set(rng.sample(sorted(by_type[t]), want[t]))
    return [inst["question_id"] for inst in holdout if inst["question_id"] in keep]


# ---------------------------------------------------------------- the prompt


def history_prompt(inst: dict[str, Any]) -> str:
    """The whole history as prepare_prompt renders it for
    full-history-session, json, useronly false, con."""
    chunks = sorted(
        zip(inst["haystack_dates"], inst["haystack_sessions"], strict=True),
        key=lambda c: c[0],
    )
    history = ""
    for i, (date, session) in enumerate(chunks[-TOPK:]):
        turns = [{k: v for k, v in t.items() if k != "has_answer"} for t in session]
        history += SESSION_BLOCK.format(i + 1, date, "\n" + json.dumps(turns))
    return l1.reading_prompt(history, inst["question_date"], inst["question"])


class _Whole:
    """A tokenizer for upstream's truncation step that never truncates."""

    def encode(self, text: str, allowed_special: Any = None) -> list[int]:
        return []


def upstream_prepare(path: Path, sha256: str) -> Callable[..., str]:
    """prepare_prompt from the pinned run_generation.py, compiled alone
    (its module imports the model clients, which the function never uses
    on this path)."""
    raw = path.read_bytes()
    if l1._sha(raw) != sha256:
        raise SystemExit(f"{path} does not hash to the pinned {sha256}")
    tree = ast.parse(raw.decode("utf-8"))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "prepare_prompt"
    )
    namespace: dict[str, Any] = {"json": json}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), namespace)
    prepare: Callable[..., str] = namespace["prepare_prompt"]
    return prepare


def upstream_prompt(prepare: Callable[..., str], inst: dict[str, Any]) -> str:
    """Upstream's own prompt for the long-context baseline; a copy, since
    prepare_prompt drops has_answer from the turns it is given."""
    return prepare(
        copy.deepcopy(inst),
        "orig-session",
        TOPK,
        False,
        "json",
        True,
        tokenizer=_Whole(),
        tokenizer_backend="openai",
        max_retrieval_length=10**12,
        merge_key_expansion_into_value="none",
    )


def build(inst: dict[str, Any]) -> tuple[dict[str, Any], str]:
    prompt = history_prompt(inst)
    return {
        **h1.base_row(inst, "holdout"),
        "question_date": inst["question_date"],
        "sessions": len(inst["haystack_sessions"]),
        "context_chars": len(l1.reading_context(prompt)),
        "prompt_chars": len(prompt),
        "prompt_sha256": l1._sha(prompt),
    }, prompt


def cmd_prompts(args: argparse.Namespace) -> None:
    corpus = l1._corpus()
    split = h1.splits(corpus)
    holdout = [inst for inst in corpus if split[inst["question_id"]] == "holdout"]
    chosen = set(sample(holdout))
    prepare = upstream_prepare(Path(args.upstream), l1.UPSTREAM_GENERATION_SHA256)
    work = Path(args.work).resolve()
    for sub in ("prompts", "prompts_wrapped", "batches", "readers", "answers"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    built: list[tuple[dict[str, Any], str]] = []
    differ: list[str] = []
    for inst in holdout:
        if inst["question_id"] not in chosen:
            continue
        row, prompt = build(inst)
        if prompt != upstream_prompt(prepare, inst):
            differ.append(row["question_id"])
        built.append((row, prompt))
    if differ:
        l1._write_json(
            work / "stopped.json",
            {"unit": "F1", "stopped_at": "F1-P1", "differ": differ},
        )
        raise SystemExit(
            f"F1-P1: {len(differ)} prompts differ from upstream's: {differ}"
        )
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
        rows.append(row)
    chars = s1.round_up(max(r["prompt_chars"] for r in rows), 1_000)
    lines = s1.round_up(max(r["wrapped_lines"] for r in rows), 100)
    batches = l1.make_batches([r["question_id"] for r in rows], [], size=BATCH_SIZE)
    instructions: list[str] = []
    for i, batch in enumerate(batches):
        batch_file = work / "batches" / f"batch_{i:02d}.txt"
        answers_file = work / "answers" / f"batch_{i:02d}.json"
        listing = "".join(f"{work / 'prompts_wrapped' / q}.txt\n" for q in batch)
        batch_file.write_text(listing, encoding="utf-8")
        text = s1.reader_instructions(batch_file, answers_file, chars, lines)
        (work / "readers" / f"reader_{i:02d}.txt").write_text(text, encoding="utf-8")
        instructions.append(text)
    projection = round(
        h1.s2_rate(Path(args.s2_work), Path(args.s2_artifact))
        * sum(r["prompt_chars"] for r in rows)
    )
    by_type: dict[str, int] = defaultdict(int)
    for r in rows:
        by_type[r["question_type"]] += 1
    l1._write_json(
        work / "meta.json",
        {
            "unit": "F1",
            "sample_seed": SAMPLE_SEED,
            "batch_size": BATCH_SIZE,
            "stated_chars": chars,
            "stated_lines": lines,
            "reader_template_sha256": l1._sha(s1.READER_TEMPLATE),
            "upstream_parity": {"n": len(rows), "equal": len(rows) - len(differ)},
            "projected_reader_tokens": projection,
            "batches": batches,
            "instructions": instructions,
            "rows": rows,
        },
    )
    print(
        json.dumps(
            {
                "questions": len(rows),
                "by_type": dict(sorted(by_type.items())),
                "abstention": sum(r["abstention"] for r in rows),
                "upstream_parity": len(rows) - len(differ),
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
    """Counted reader tokens so far, scaled to the 150."""
    return counted * N // questions if questions else 0


def over_stop(tokens: int) -> bool:
    return tokens > TOKEN_STOP


def cmd_audit(args: argparse.Namespace) -> None:
    s1.cmd_audit(args)
    work = Path(args.work).resolve()
    result = l1._load_json(work / "audit-readers.json")
    done = [g for g in result["canonical"].values() if g["clean"]]
    counted = sum(s1.counted_tokens(g["usage"]) for g in done)
    projected = projected_tokens(counted, len(done))
    print(
        json.dumps(
            {
                "questions_read": len(done),
                "tokens_clean": counted,
                "tokens_all_attempts": s1.counted_tokens(
                    result["summary"]["usage_all_attempts"]
                ),
                "projected": projected,
                "over_stop": over_stop(projected),
            }
        )
    )


# ---------------------------------------------------------------- the grade


def predictions(n: dict[str, Any]) -> list[dict[str, Any]]:
    """The declaration's five predictions graded on the run's numbers."""
    v = l1._verdict
    got, lead = n["f1_standard"], n["lead_standard"]
    tokens, usd = n["reader_tokens"], n["judge_usd"]
    ok = n["audit_ok"] and n["stops"] <= 3 and usd < 0.5
    return [
        {
            "id": "F1-P1",
            "claim": "every prompt equals upstream prepare_prompt's, 150 of 150",
            "got": n["parity"],
            "verdict": v(n["parity"] == N, n["parity"] != N),
        },
        {
            "id": "F1-P2",
            "claim": "the whole history under the standard 142 of 150 (137 to 146); "
            "MISSED above 148 or below 130",
            "got": got,
            "verdict": v(137 <= got <= 146, got > 148 or got < 130),
        },
        {
            "id": "F1-P3",
            "claim": "S2's answers ahead of the whole history's on the same 150 under "
            "the standard by 2 to 9; MISSED if the whole history is level or ahead",
            "got": {"lead": lead, "p": n["p_standard"]},
            "verdict": v(2 <= lead <= 9, lead <= 0),
        },
        {
            "id": "F1-P4",
            "claim": "the judges agree on at least 0.97 of the 150; MISSED under 0.95",
            "got": n["agreement"],
            "verdict": v(n["agreement"] >= 0.97, n["agreement"] < 0.95),
        },
        {
            "id": "F1-P5",
            "claim": "every counted batch clean, at most 3 attempts stopped by a "
            "safeguard, counted tokens 31M to 40M, judges under $0.50; MISSED if "
            "any but the token range fails or tokens pass 42M",
            "got": {
                "audit_ok": n["audit_ok"],
                "stops": n["stops"],
                "reader_tokens": tokens,
                "judge_usd": usd,
            },
            "verdict": v(
                ok and 31_000_000 <= tokens <= 40_000_000,
                not ok or over_stop(tokens),
            ),
        },
    ]


def cmd_report(args: argparse.Namespace) -> None:
    work = Path(args.work).resolve()
    meta = l1._load_json(work / "meta.json")
    rows = meta["rows"]
    qids = [r["question_id"] for r in rows]
    judged = l1._load_json(work / "judgments.json")
    price = l1._load_json(work / "price.json")
    f1 = {
        name: {q: r["verdict"] for q, r in judged[name]["rows"].items()}
        for name in JUDGES
    }
    answers = {
        h["question_id"]: h["hypothesis"] for h in l1._jsonl(work / "hypotheses.jsonl")
    }
    s2_art = l1._load_json(Path(args.s2_artifact))
    s2_rows = {r["question_id"]: r for r in s2_art["rows"]}
    if not set(qids) <= set(s2_rows):
        raise SystemExit("a sampled question is not in S2's holdout")
    bm = {
        name: {q: s2_rows[q]["verdicts"]["s2"][model] for q in qids}
        for name, model in JUDGES.items()
    }
    scores = {
        "whole_history": {name: s2.score(rows, f1[name]) for name in JUDGES},
        "bettermemory_s2": {name: s2.score(rows, bm[name]) for name in JUDGES},
    }
    pair = {name: h1.paired(bm[name], f1[name], qids) for name in JUDGES}
    differ = h1._disagreements(rows, f1)
    agreement = round((len(rows) - len(differ)) / len(rows), 4)
    readers = l1._load_json(work / "audit-readers.json")["summary"]
    tokens_readers = s1.counted_tokens(readers["usage"])
    tokens_all = s1.counted_tokens(readers["usage_all_attempts"])
    judge_usd = round(sum(judged[name]["spent_usd"] for name in JUDGES), 6)
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
            "parity": meta["upstream_parity"]["equal"],
            "f1_standard": scores["whole_history"]["standard"]["correct"],
            "lead_standard": pair["standard"]["lead"],
            "p_standard": pair["standard"]["mcnemar_exact_p"],
            "agreement": agreement,
            "audit_ok": audit_ok,
            "stops": readers["attempts_stopped_by_safeguard"],
            "reader_tokens": tokens_readers,
            "judge_usd": judge_usd,
        }
    )
    served = {
        "whole_history_mean_chars": round(
            sum(r["context_chars"] for r in rows) / len(rows)
        ),
        "bettermemory_mean_chars": round(
            sum(s2_rows[q]["context_chars"] for q in qids) / len(qids)
        ),
    }
    l1._write_json(
        Path(args.out),
        {
            "unit": "F1",
            "declaration": "memory 01M3J68RY2E06P46T87WYSZ1GT",
            "approval": "memory 01M3J4ZKE36JZ9S5RX37Q0NMFB",
            "provenance": l1._provenance(),
            "protocol": {
                "dataset": {
                    "file": str(l1.CORPUS.relative_to(_BENCH.parent)),
                    "sha256": l1.CORPUS_SHA256,
                },
                "sample": f"{N} of the holdout 350 of aml_run.split (SPLIT_SEED "
                f"{aml_run.SPLIT_SEED}), stratified by question type by largest "
                f"remainder, drawn with seed {SAMPLE_SEED}",
                "prompt": "run_generation.sh full-history-session at "
                + l1.UPSTREAM
                + f": orig-session, TOPK {TOPK}, json, useronly false, con; checked "
                "against the pinned prepare_prompt; no truncation",
                "reader": {
                    "model": l1.READER_MODEL,
                    "harness": "Claude Code subagents (general-purpose)",
                    "batch_size": BATCH_SIZE,
                    "batches": len(meta["batches"]),
                    "template_sha256": meta["reader_template_sha256"],
                    "stated_chars": meta["stated_chars"],
                    "stated_lines": meta["stated_lines"],
                },
                "judges": s2_art["protocol"]["judges"],
                "comparator": "S2's answers on the same questions: bettermemory's "
                "served context under the S0 arm, the same reader and judges",
            },
            "upstream_parity": meta["upstream_parity"],
            "summary": {
                "scores": scores,
                "paired_bettermemory_vs_whole_history": pair,
                "agreement": {
                    "n": len(rows),
                    "agree": len(rows) - len(differ),
                    "rate": agreement,
                },
                "served": served,
            },
            "disagreements": [
                {
                    "question_id": q,
                    **{JUDGES[name]: f1[name].get(q) for name in JUDGES},
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
                            "sessions",
                            "context_chars",
                            "prompt_sha256",
                        )
                    },
                    "verdicts": {
                        "whole_history": {
                            JUDGES[name]: f1[name].get(r["question_id"])
                            for name in JUDGES
                        },
                        "bettermemory_s2": {
                            JUDGES[name]: bm[name][r["question_id"]] for name in JUDGES
                        },
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
                "whole_history": {
                    n: scores["whole_history"][n]["correct"] for n in JUDGES
                },
                "bettermemory_s2": {
                    n: scores["bettermemory_s2"][n]["correct"] for n in JUDGES
                },
                "paired_standard": {
                    k: pair["standard"][k]
                    for k in ("a_correct", "b_correct", "lead", "mcnemar_exact_p")
                },
                "agreement": agreement,
                "served": served,
                "predictions": [(p["id"], p["got"], p["verdict"]) for p in graded],
            },
            indent=1,
        )
    )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prompts")
    s.add_argument("--work", default=str(WORK))
    s.add_argument("--upstream", default=str(l1.UPSTREAM_GENERATION))
    s.add_argument("--s2-work", default=str(S2_WORK))
    s.add_argument("--s2-artifact", default=str(S2_ARTIFACT))
    s.set_defaults(fn=cmd_prompts)
    s = sub.add_parser("collect")
    s.add_argument("--work", default=str(WORK))
    s.set_defaults(fn=l1.cmd_collect)
    s = sub.add_parser("audit")
    s.add_argument("--work", default=str(WORK))
    s.add_argument("--transcripts", required=True)
    s.set_defaults(fn=cmd_audit, role="readers")
    s = sub.add_parser("report")
    s.add_argument("--work", default=str(WORK))
    s.add_argument("--s2-artifact", default=str(S2_ARTIFACT))
    s.add_argument("--out", default=str(OUT))
    s.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
