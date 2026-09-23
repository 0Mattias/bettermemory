"""PersonaMem v1 (github.com/bowen-upenn/PersonaMem, MIT) as AML runs it.

WHAT AML DOES. AML's data/personamem/pipeline_v1.py (commit 1b8142b)
reproduces the dataset's own inference_standalone_openai.py: the history
goes in as chat messages (`context_messages`), then one user message
"<question>\\n\\n<OFFICIAL_INSTRUCTION>\\n\\n<all_options>", where
all_options is the dataset's raw options string (a Python list literal,
passed through untouched). Because the answer model's name contains an
"o" (gpt-4o-mini), every message list goes through
`convert_role_system_to_user`: a system message becomes a "[System]: "
prefix on the next message and consecutive same-role messages merge. The
call carries temperature 0 and no max_tokens. Grading is the dataset's
exact option match (`official_extract_answer`); no judge model is
involved, so credit is 0 or 1.

HOW MEMORIES REACH THAT PROMPT (not public). The pipeline takes a list
of chat messages and Search returns strings. The minimal mapping is one
user message per served memory, in served order; after the pipeline's
own role conversion that is a single user message, the memories joined
by newlines, a newline, then the question prompt. A system-role mapping
would differ only by a leading "[System]: ".

THE TIER (not public). The release ships 32k, 128k and 1M tiers; the
public pipeline names none and the upstream script defaults to 128k.
`load(tier)` reads `questions_<tier>.csv` and
`shared_contexts_<tier>.jsonl` from DATA; the default is the 32k tier,
the only one fetched so far.

ISOLATION. Each question sees its shared context sliced as
`context[:end_index_in_shared_context]` (the dataset card's rule). One
user_id per (shared context, slice point): questions that share both
share a store, and no store holds a message past its questions' slice.
The same prefix is therefore ingested once per slice point.

ADD UNITS. A PersonaMem context is a run of sessions, each opened by a
system message carrying the user persona. AML's Add contract accepts
roles user and assistant only, so each session goes through the
pipeline's own `convert_role_system_to_user` (per session, so a merge
never crosses a session boundary) and is sent as one Add; the
20-message / 2,000-word split is applied downstream. The data carries
no timestamps, so none are sent.
"""

from __future__ import annotations

import ast
import csv
import json
import re
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
DATA = _HERE / "data" / "personamem_v1"
TIER = "32k"

# The answer model AML names for its pipelines; the pipeline's role
# conversion keys on this name (`"o" in model`), not on the stand-in
# reader a local run uses, so the prompt shape is AML's either way.
AML_ANSWER_MODEL = "gpt-4o-mini"
# AML's post_chat sends no max_tokens, so the provider default applies;
# 16,384 is gpt-4o-mini's own output ceiling, the same as leaving it unset.
MAX_TOKENS = 16384

# ---------------------------------------------------------------------------
# Verbatim from AML data/personamem/pipeline_v1.py (commit 1b8142b),
# extracted with `ast.get_source_segment`; only ruff's formatting differs.

OFFICIAL_INSTRUCTION = (
    "Find the most appropriate model response and give your final answer "
    "(a), (b), (c), or (d) after the special token <final_answer>."
)


def official_user_prompt(question: str, all_options: str) -> str:
    if not isinstance(all_options, str):
        raise TypeError(
            "PersonaMem v1 strict mode requires original `all_options` as a string."
        )
    return f"{question}\n\n{OFFICIAL_INSTRUCTION}\n\n{all_options}"


def convert_role_system_to_user(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    converted = []
    system_buffer = ""
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "system":
            system_buffer += f"[System]: {content}\n"
            continue
        if system_buffer:
            content = system_buffer + content
            system_buffer = ""
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"] += "\n" + content
        else:
            converted.append({"role": role, "content": content})
    return converted


def official_messages(item: dict[str, Any], model: str) -> list[dict[str, str]]:
    context = item.get("context_messages", item.get("context"))
    if not isinstance(context, list):
        raise TypeError(
            "PersonaMem v1 input must include `context_messages` as a list of chat messages."
        )

    messages = [dict(message) for message in context]
    messages.append(
        {
            "role": "user",
            "content": official_user_prompt(str(item["question"]), item["all_options"]),
        }
    )

    if "o" in model:
        messages = convert_role_system_to_user(messages)
    return messages


def extract_option_set(answer: Any) -> set[str]:
    text = str(answer).strip().lower()
    in_parens = re.findall(r"\(([a-d])\)", text)
    if in_parens:
        return set(in_parens)
    return set(re.findall(r"\b([a-d])\b", text))


def extract_gold_option(correct_answer: Any) -> str:
    return str(correct_answer).lower().strip("() ")


def official_extract_answer(
    predicted_answer: Any, correct_answer: Any
) -> tuple[bool, str]:
    full_response = str(predicted_answer)
    predicted = full_response.strip()
    correct = extract_gold_option(correct_answer)

    if "<final_answer>" in predicted:
        predicted = predicted.split("<final_answer>")[-1].strip()
    if predicted.endswith("</final_answer>"):
        predicted = predicted[: -len("</final_answer>")].strip()

    pred_options = extract_option_set(predicted)
    if pred_options == {correct}:
        return True, predicted

    response_options = extract_option_set(full_response)
    if response_options == {correct}:
        return True, predicted

    return False, predicted


# End of the verbatim block.
# ---------------------------------------------------------------------------


def grade(generated: str, gold: str) -> tuple[bool | None, str]:
    """The pipeline's verdict, plus the option it parsed. A reply from
    which neither the pipeline's final-answer segment nor the whole text
    yields any option letter is unparseable (None); AML scores it wrong,
    as it does False, and the harness counts it separately."""
    correct, predicted = official_extract_answer(generated, gold)
    options = extract_option_set(predicted) or extract_option_set(generated)
    parsed = "".join(f"({o})" for o in sorted(options))
    if correct:
        return True, parsed
    return (None if not options else False), parsed


def user_id_for(shared_context_id: str, end_index: int, tier: str = TIER) -> str:
    return f"personamem-v1:{tier}:{shared_context_id[:16]}:{end_index}"


def sessions_of(history: list[dict[str, Any]]) -> list[list[dict[str, str]]]:
    """Split a sliced history into sessions (each opened by a system
    message) and map each to user/assistant roles with the pipeline's own
    conversion. A trailing system message with nothing after it is
    dropped, as the pipeline drops it."""
    raw: list[list[dict[str, Any]]] = []
    for m in history:
        if m.get("role") == "system" or not raw:
            raw.append([])
        raw[-1].append(m)
    out = []
    for sess in raw:
        converted = convert_role_system_to_user(
            [{"role": m["role"], "content": m["content"]} for m in sess]
        )
        if converted:
            out.append(converted)
    return out


def _contexts(path: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.update(json.loads(line))
    return out


def load(
    tier: str = TIER,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Questions and per-slice sessions in the shape run.load_locomo
    returns. `options` is the options list as AML's Search body carries
    it; `all_options` keeps the raw string the answer prompt uses."""
    contexts = _contexts(DATA / f"shared_contexts_{tier}.jsonl")
    with (DATA / f"questions_{tier}.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    prefixes: dict[str, str] = {}
    for sid in contexts:
        if prefixes.setdefault(sid[:16], sid) != sid:
            raise ValueError(f"shared_context_id prefix collision: {sid[:16]}")
    questions: list[dict[str, Any]] = []
    sessions: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sid = row["shared_context_id"]
        end = int(row["end_index_in_shared_context"])
        context = contexts[sid]
        if not 0 < end <= len(context):
            raise ValueError(f"{row['question_id']}: slice {end} of {len(context)}")
        uid = user_id_for(sid, end, tier)
        if uid not in sessions:
            sessions[uid] = [
                {"session_id": f"S{i}", "messages": msgs}
                for i, msgs in enumerate(sessions_of(context[:end]))
            ]
        options = ast.literal_eval(row["all_options"])
        if not isinstance(options, list) or not all(
            isinstance(o, str) for o in options
        ):
            raise ValueError(f"{row['question_id']}: all_options is not a list")
        questions.append(
            {
                "question_id": row["question_id"],
                "question_type": row["question_type"],
                "question": row["user_question_or_message"],
                "answer": row["correct_answer"],
                "user_id": uid,
                "answer_session_ids": [],
                "options": options,
                "all_options": row["all_options"],
                "topic": row.get("topic", ""),
                "persona_id": row.get("persona_id", ""),
            }
        )
    return questions, sessions


def answer_messages(inst: dict[str, Any], contents: list[str]) -> list[dict[str, str]]:
    item = {
        "context_messages": [{"role": "user", "content": c} for c in contents],
        "question": inst["question"],
        "all_options": inst["all_options"],
    }
    return official_messages(item, AML_ANSWER_MODEL)


async def answer_and_grade(
    client: Any,
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    reader: str,
    judge: str,
    thinking: str = "low",
) -> dict[str, Any]:
    """AML's answer step over the served memories, graded by the
    pipeline's exact option match. `judge` and `thinking` are unused: the
    grade is deterministic."""
    del judge, thinking
    messages = answer_messages(inst, [h["content"] for h in hits])
    ans = await client.complete(
        reader, messages, max_tokens=MAX_TOKENS, temperature=0.0
    )
    generated = ans.text.strip()
    verdict, parsed = grade(generated, inst["answer"])
    evidence = set(inst.get("answer_session_ids") or [])
    served = {h.get("_session", "") for h in hits}
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "abstention": False,
        "n_hits": len(hits),
        "prompt_chars": sum(len(m["content"]) for m in messages),
        "generated": generated,
        "verdict": verdict,
        "judge_raw": parsed,
        "evidence_sessions": sorted(evidence),
        "evidence_served": len(evidence & served),
        "cost": ans.cost,
    }
