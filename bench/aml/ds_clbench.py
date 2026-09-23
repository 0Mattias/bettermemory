"""CL-bench (github.com/Tencent-Hunyuan/CL-bench) as AML runs it.

LICENCE. Tencent's CL-bench licence permits evaluation, testing and
benchmarking only: no training, fine-tuning, calibrating, distilling or
any other parameter updating. The data lives under bench/aml/data/
(gitignored) and is never committed.

WHAT AML DOES. AML's data/clbench/pipeline.py (commit 1b8142b) follows a
"project CL-Bench implementation", not Tencent's infer.py/eval.py. Per
task it fills _CLBENCH_ANSWER_PROMPT_TEMPLATE with the task's system
prompt, the memories Search served (one "- [created_at] text" line each,
"(no memories)" when there are none) and the question, strips the result
and sends it as one user message to the answer model, temperature 0, no
max_tokens. The reply, cut after any "Final Answer:" or "</think>", is
graded by one judge call on Tencent's strict rubric prompt: the rubrics
numbered, the judge answering JSON with a per-rubric "yes"/"no" list and
an all-or-nothing "Overall Score". Two numbers come out per task, the
two columns AML's board shows per CLBench range: Strict (1 only when the
judge says 1) and Rubric Coverage (the share of "yes" in the judge's own
list). An empty reply scores 0 without a judge call. A judgment that is
not JSON with an "Overall Score" is re-asked (AML: up to 3 times; here
once, with room to finish, as run.py's _judge_retry) and then scores 0.

THE PROMPT'S FIRST LINE. The template literal escapes a backslash just
before its first newline (the author likely meant a line continuation),
so its value opens with a line holding one backslash, and `.strip()`
keeps it: every prompt AML sends starts with that line and then the
system prompt. It is reproduced, not fixed.

WHAT IS MEMORY AND WHAT IS THE QUESTION. AML's converter is not public;
the pipeline reads an item's `system_prompt`, `question` and `rubrics`
(its `row_id` falls back to CL-bench's own metadata.task_id), and its
template says the question "includes a reference document" and that the
memories come "from previous conversations". So, per CL-bench record:

  system_prompt  messages[0], the system message: into the prompt, never
                 into memory
  question       the final user message, verbatim: the Search query and
                 the prompt's question. In a single-turn task it holds the
                 whole reference document; in a sequential task it is
                 often a short follow-up whose document sits in an earlier
                 turn
  memory         (add="conversation", the default) every non-system
                 message in order, the final user turn included, as one
                 session: AML writes something for every task, and a
                 single-turn task has nothing before its final turn.
                 add="history" writes only the turns before the final one
                 (a single-turn task then Searches an empty store); set
                 CLBENCH_ADD=history to measure that reading

One user_id per task (`clbench:<task_id>`), never per context: the tasks
of one context share its document, but a sequential task's history
carries the reference answers to the earlier tasks, which must not reach
their own Searches.

THE SUBSET. AML's board reports CLBench as two ranges, "0-4k" and
"16-32k" (agentmemoryleaderboard.ai static/app.js, PUBLIC_DATASET_COLUMNS),
without saying what is counted. This loader counts the whole task input
(system prompt and every message, as the CL-bench paper measures input
length) at CHARS_PER_TOKEN characters per token, and by default keeps
the two ranges AML scores (CLBENCH_RANGES=all keeps every task). Whether
AML samples within a range is not public either.

DATA TRAPS. A final user message runs to 309,522 characters: it is the
Search query, and under add="conversation" also a stored round, so that
task's prompt carries it twice (~623k characters, past gpt-4o-mini's
window; it lies outside AML's two ranges). Under add="conversation" the
stored copy of the question ranks first, takes the one slot a character
budget serves regardless of size, and can push a sequential task's
document round (up to ~118k characters in 16-32k) out of a 90,000
budget. AML's Add split (20 messages / 2,000 words) never cuts inside a
message. The JSONL holds raw U+2028 characters inside strings, so it is
split on "\\n" only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Sequence

_HERE = Path(__file__).resolve().parent
DATA = _HERE / "data" / "clbench" / "CL-bench.jsonl"

# AML's post_chat sends no max_tokens, so the provider default applies;
# 16,384 is gpt-4o-mini's own output ceiling, the same as leaving it unset.
MAX_TOKENS = 16384
# The judge call sends no max_tokens either; the first allowance holds a
# rationale plus a yes/no per rubric (up to 114), the retry has room to
# finish after a provider spends the first on thinking.
JUDGE_MAX_TOKENS = 2048
JUDGE_RETRY_MAX_TOKENS = 4000

CHARS_PER_TOKEN = 4
RANGES = (
    (0, 4_000, "0-4k"),
    (4_000, 16_000, "4-16k"),
    (16_000, 32_000, "16-32k"),
    (32_000, None, "32k+"),
)
AML_RANGES = ("0-4k", "16-32k")
ADD_MODES = ("conversation", "history")


# Verbatim from AML data/clbench/pipeline.py (commit 1b8142b), cut out with
# `ast.get_source_segment` and never retyped: the answer template and its
# rendering, the structured-question and selected-memory formatting the
# pipeline cites from memory_search.py, and the rubric judge's prompt and
# parse helpers it cites from rubric_clbench.py.
_CLBENCH_ANSWER_PROMPT_TEMPLATE = """\\
{{system_prompt}}

<context>
The following memories from previous conversations may provide additional context:

{{memories}}
</context>

<task>
Answer the question below thoroughly and completely. The question includes a reference document — read it carefully and base your answer on its content.

Requirements:
- Cover every aspect asked in the question.
- Follow all formatting and style rules defined above (e.g. use or avoid bullet points as instructed, use bold headers if required, etc.).
- Do not truncate your response. A complete, detailed answer is expected.
</task>

{{question}}"""


def render_answer_prompt_clbench(
    *, system_prompt: str, memories: str, question: str
) -> str:
    """Exact rendering behaviour from the supplied ``memory_search.py``."""
    return (
        _CLBENCH_ANSWER_PROMPT_TEMPLATE.replace(
            "{{system_prompt}}", system_prompt or ""
        )
        .replace("{{memories}}", memories or "(no memories)")
        .replace("{{question}}", question)
    ).strip()


def format_structured_question(
    *, question: str, qa_type: str, options: list[str]
) -> str:
    """Exact structured-QA formatting from the supplied ``memory_search.py``."""
    question_text = str(question or "").strip()
    normalized_type = str(qa_type or "").strip().lower()
    if (
        normalized_type not in {"single_choice", "multi_select", "ordering"}
        or not options
    ):
        return question_text
    option_lines: list[str] = []
    for index, raw_option in enumerate(options):
        letter = chr(ord("A") + index)
        option_text = re.sub(
            r"^\s*(?:\([A-Za-z]\)|[A-Za-z][\.:])\s*", "", str(raw_option or "")
        ).strip()
        if option_text:
            option_lines.append(f"{letter}. {option_text}")
    if not option_lines:
        return question_text
    if normalized_type == "single_choice":
        output_contract = (
            "Required answer format: exactly one uppercase option letter, such as A."
        )
    elif normalized_type == "multi_select":
        output_contract = "Required answer format: all supported uppercase option letters, comma-separated."
    else:
        output_contract = "Required answer format: all supported uppercase option letters in order, comma-separated."
    return "\n\n".join(
        (question_text, "Options:\n" + "\n".join(option_lines), output_contract)
    )


def format_selected_memories(selected: list[dict[str, Any]]) -> str:
    """The timestamped prompt block used by the supplied retrieval implementation."""
    lines: list[str] = []
    for item in selected:
        timestamp = str(item.get("created_at") or "").strip()
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- [{timestamp}] {text}" if timestamp else f"- {text}")
    return "\n".join(lines)


def _normalize_model_output(text: str) -> str:
    cleaned = str(text or "").strip()
    if "Final Answer:" in cleaned:
        cleaned = cleaned.split("Final Answer:", 1)[1].strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1].strip()
    return cleaned


def _normalize_rubrics(
    rubrics: Sequence[str] | Sequence[dict[str, Any]] | None,
) -> list[str]:
    normalized: list[str] = []
    for item in rubrics or []:
        text = str(
            item.get("rubric_criteria", "") if isinstance(item, dict) else item or ""
        ).strip()
        if text:
            normalized.append(text)
    return normalized


def _build_rubrics_text(rubrics: Sequence[str]) -> str:
    if not rubrics:
        return "No specific rubrics provided."
    return "\n".join(
        f"{index}. {rubric}" for index, rubric in enumerate(rubrics, start=1)
    )


def _coerce_score(payload: dict[str, Any]) -> int:
    try:
        return 1 if int(payload.get("Overall Score", "")) == 1 else 0
    except (TypeError, ValueError):
        return 0


def _coerce_status_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        return [part.strip() for part in text.split(",") if part.strip()]
    return []


def _compute_requirement_ratio(status_list: Sequence[str]) -> float:
    if not status_list:
        return 0.0
    return sum(
        str(item).strip().lower() in {"yes", "y", "true", "1"} for item in status_list
    ) / len(status_list)


def rubric_judge_prompt(*, rubrics_text: str, model_output: str) -> str:
    """The supplied ``rubric_clbench.py`` judge prompt, including its protocol."""
    return f"""Starting now, you are a rigorous instruction-following grading teacher. Your task is to accurately grade and score student answers based on the 【Rubrics】.

Grading Criteria
This is a strict, all-or-nothing grading system. The final score is binary.
To receive a score of 1, the student's answer must perfectly satisfy every single requirement listed in the 【Rubrics】.
If even one requirement is not fully met, the final score will be 0.
Grading Process
Please strictly follow the steps below for analysis—no steps may be skipped:
Step 1: Analyze the Standard Answer
List all explicit requirements in the 【Rubrics】 item by item (including format, content, quantity, order, etc.).
Identify implicit requirements in the 【Rubrics】 (e.g., language style, logical structure).
Define specific evaluation criteria for each requirement (e.g., "must include X," "must not exceed Y").
Step 2: Check Each Requirement Against the Student's Answer
For every requirement in the 【Rubrics】, verify one by one whether the student's answer fully satisfies it.
Step 3: Self-Reflection
Before giving the final score, you must conduct the following checks:
  Completeness Check: Whether all requirements in the standard answer have been reviewed with no omissions.
  Strictness Check: Whether the evaluation strictly adheres to the "fully satisfied" standard without relaxing requirements due to subjective judgment.
  Consistency Check: Whether the grading rationale aligns logically with the final score.
  Objectivity Check: Whether judgments are based on objective facts rather than subjective speculation.
Output Format Requirements
【Grading Rationale】: xxx
【List of Requirement Satisfaction Status】: [x₁, x₂, …, xᵢ, …, xₙ] (where n is the total number of requirements in the 【Rubrics】, and xᵢ indicates whether the student's answer meets the i-th requirement, with values "yes"/"no")
【Overall Score】: x points (x is an integer, either 0 or 1.)

Content to Be Graded
【Rubrics】:
{rubrics_text}
【Student Response】:
{model_output}

Please strictly output ONLY the following JSON format (do not output any other content):
{{
  "Grading Rationale": "Your detailed grading rationale",
  "List of Requirement Satisfaction Status": ["yes", "no", ...],
  "Overall Score": 0 or 1
}}
"""


# ---------------------------------------------------------------- judging


def parse_judgment(text: str) -> dict[str, Any] | None:
    """One judge reply read the way the pipeline reads it: post_chat's
    strip, call_judge_api's fence strip, then evaluate_rubric_clbench's
    `json.loads` and "Overall Score" check. None where the pipeline would
    re-ask. A JSON value that is not an object is None too (the pipeline
    raises on it outside its except clause)."""
    text = str(text or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "Overall Score" not in payload:
        return None
    statuses = _coerce_status_list(
        payload.get("List of Requirement Satisfaction Status", [])
    )
    return {
        "score": float(_coerce_score(payload)),
        "rationale": str(payload.get("Grading Rationale", "") or ""),
        "statuses": statuses,
        "ratio": _compute_requirement_ratio(statuses),
    }


def status_scores(statuses: Sequence[str]) -> list[float]:
    """Per-rubric 1/0 from the judge's list, by the ratio's own yes-set."""
    return [
        1.0 if str(s).strip().lower() in {"yes", "y", "true", "1"} else 0.0
        for s in statuses
    ]


# ---------------------------------------------------------------- loading


def approx_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages) // CHARS_PER_TOKEN


def length_range(tokens: int) -> str:
    for lo, hi, name in RANGES:
        if tokens >= lo and (hi is None or tokens < hi):
            return name
    raise ValueError(tokens)


def _wanted(ranges: str) -> set[str] | None:
    if ranges == "all":
        return None
    if ranges == "aml":
        return set(AML_RANGES)
    names = {r.strip() for r in ranges.split(",") if r.strip()}
    unknown = names - {name for _, _, name in RANGES}
    if unknown:
        raise ValueError(f"unknown CL-bench ranges {sorted(unknown)}")
    return names


def load(
    *, add: str | None = None, ranges: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Questions and per-task sessions in the shape run.load_locomo
    returns. `add` picks what is written (CLBENCH_ADD, default
    "conversation"); `ranges` picks the length ranges kept
    (CLBENCH_RANGES: "aml" (default), "all", or names like "0-4k,4-16k")."""
    add = add or os.environ.get("CLBENCH_ADD", "conversation")
    if add not in ADD_MODES:
        raise ValueError(f"add {add!r}")
    wanted = _wanted(ranges or os.environ.get("CLBENCH_RANGES", "aml"))
    questions: list[dict[str, Any]] = []
    sessions: dict[str, list[dict[str, Any]]] = {}
    # "\n" only: 343 raw U+2028 characters sit inside JSON strings, and
    # str.splitlines() would cut records apart at each of them.
    for line in DATA.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        record = json.loads(line)
        messages = record["messages"]
        meta = record["metadata"]
        if not messages or messages[-1].get("role") != "user":
            raise ValueError(f"task {meta['task_id']} does not end on a user turn")
        tokens = approx_tokens(messages)
        name = length_range(tokens)
        if wanted is not None and name not in wanted:
            continue
        system = messages[0] if messages[0].get("role") == "system" else None
        turns = messages[1:] if system else messages
        written = turns if add == "conversation" else turns[:-1]
        user_id = f"clbench:{meta['task_id']}"
        sessions[user_id] = (
            [
                {
                    "session_id": "S0",
                    "messages": [
                        {"role": m["role"], "content": m["content"]} for m in written
                    ],
                }
            ]
            if written
            else []
        )
        questions.append(
            {
                "question_id": meta["task_id"],
                "question_type": meta["context_category"],
                "sub_category": meta.get("sub_category"),
                "context_id": meta.get("context_id"),
                "length_range": name,
                "approx_tokens": tokens,
                "question": messages[-1]["content"],
                "system_prompt": system["content"] if system else "",
                "answer": "",
                "rubric": [str(r) for r in record.get("rubrics") or []],
                "user_id": user_id,
                "answer_session_ids": [],
            }
        )
    return questions, sessions


# ---------------------------------------------------------------- answering


def memories_block(hits: list[dict[str, Any]]) -> str:
    """Search's hits as the pipeline's `selected` items: `content` as the
    text, `created_at` as the timestamp (absent on CL-bench, which carries
    no dates)."""
    return format_selected_memories(
        [{"text": h.get("content"), "created_at": h.get("created_at")} for h in hits]
    )


def build_prompt(inst: dict[str, Any], hits: list[dict[str, Any]]) -> str:
    """The pipeline's build_answer_prompt over one task and its hits."""
    return render_answer_prompt_clbench(
        system_prompt=str(inst.get("system_prompt") or ""),
        memories=memories_block(hits),
        question=format_structured_question(
            question=str(inst.get("question") or ""),
            qa_type=str(inst.get("qa_type") or ""),
            options=[str(option) for option in inst.get("options") or []],
        ),
    )


async def answer_and_grade(
    client: Any,
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    reader: str,
    judge: str,
    thinking: str = "low",
) -> dict[str, Any]:
    """AML's answer call, then the strict rubric judge. `score` is the
    Strict credit (0 or 1); `rubric_ratio` is Rubric Coverage. The verdict
    is None only when the judgment never parsed (scored 0, as AML does)."""
    prompt = build_prompt(inst, hits)
    ans = await client.complete(
        reader,
        [{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS,
        temperature=0.0,
    )
    generated = ans.text.strip()
    cost = ans.cost
    answer = _normalize_model_output(generated)
    rubrics = _normalize_rubrics(inst.get("rubric"))
    judgment: dict[str, Any] | None = None
    skipped = (
        "No model output (counted as score 0)"
        if not answer
        else "No rubrics provided"
        if not rubrics
        else None
    )
    raw = skipped or ""
    if skipped is None:
        messages = [
            {
                "role": "user",
                "content": rubric_judge_prompt(
                    rubrics_text=_build_rubrics_text(rubrics), model_output=answer
                ),
            }
        ]
        for allowance in (JUDGE_MAX_TOKENS, JUDGE_RETRY_MAX_TOKENS):
            jud = await client.complete(
                judge,
                messages,
                max_tokens=allowance,
                temperature=0.0,
                reasoning_effort=thinking,
            )
            cost += jud.cost
            raw = jud.text
            judgment = parse_judgment(jud.text)
            if judgment is not None:
                break
    score = judgment["score"] if judgment else 0.0
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "length_range": inst.get("length_range"),
        "abstention": False,
        "n_hits": len(hits),
        "prompt_chars": len(prompt),
        "generated": generated,
        "verdict": None if judgment is None and skipped is None else score == 1.0,
        "score": score,
        "rubric_ratio": round(judgment["ratio"], 4) if judgment else 0.0,
        "rubric_scores": status_scores(judgment["statuses"]) if judgment else None,
        "judge_raw": raw[:400],
        "evidence_sessions": [],
        "evidence_served": 0,
        "cost": cost,
    }
