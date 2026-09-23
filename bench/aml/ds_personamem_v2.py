"""PersonaMem v2 (huggingface.co/datasets/bowen-upenn/PersonaMem-v2,
CC BY 4.0) as AML runs it, text benchmark, 128k chat histories.

WHAT AML DOES. AML's data/personamem/pipeline_v2.py (commit 1b8142b) has
two paths, chosen by a required `--mode` flag with no default:

  mcq         the chat history, then the user query with RECALL_SUFFIX
              appended as a user message, then MCQ_PROMPT_TEMPLATE over
              the correct answer and the three incorrect ones, shuffled by
              a seeded RNG, as a system message. The reply's letter is
              parsed by fixed rules (`extract_final_letter`) and credit is
              1 when the letter maps to the correct answer, else 0.
  generative  the same messages without the option block; the reply is
              scored 0.0-1.0 by a judge model with the "narrow" prompt of
              upstream inference_utils.py, the negative variant when the
              gold preference starts with "do not", parsed from
              \\boxed{score} (`extract_boxed_score`).

MODE is "mcq". AML's pipeline does not say which it scores, but upstream
inference.py defaults to `--eval_mode mcq` and reports MCQ accuracy as
the benchmark's headline number; AML's API guide sends choice options on
Search in exactly this path's "A. text" form; and AML's leaderboard shows
PersonaMem-v2 as one accuracy column beside the exact-graded choice
datasets. Set MODE = "generative" to run the judge path instead.

Both paths call the answer model with no max_tokens and temperature 0,
and the judge with the prompt as one user message.

WHERE THE MEMORIES GO. The pipeline reads the chat history as a list of
messages (`chat_history`), and AML does not publish how it builds that
list from Search results. Here each served memory becomes one user
message, in served order (`memory_history`): the slot holds messages, a
memory has no role of its own, and a round's own "user:"/"assistant:"
lines stay inside its text. This is a judgement call, not AML's code.

THE QUERY. Search gets the user query's own text; AML's API guide says
the query is the benchmark's original question, and the pipeline appends
RECALL_SUFFIX only when it builds the answer messages. In mcq mode the
lettered options travel as `options`, in the order the answer model sees.

ISOLATION AND ADD UNITS (AML's are not public). Upstream appends every
query to its persona's whole chat history (the file in
`chat_history_128k_link`); nothing is sliced per question. So one user_id
per persona, 25 questions each. The history file is one flat message
list with no session boundaries, so it is one source session (AML's
20-message / 2,000-word split cuts it into Adds). Its first message is
the system prompt that carries the persona profile; it is kept as its
own session, "persona", because a system message leading the history
would shift every user/assistant round after it by one message.

THE DATA TRAPS. One assistant reply is empty (persona 39); AML's Add
requires non-empty content, so it is dropped. Five assistant replies
were cut into stray JSON keys at generation time; only `content` is
sent. The 511 `sensitive_info` questions cite a conversation snippet
that is not in the 128k history; they test whether the answer masks a
secret the query itself states, and carry no evidence label.

EVIDENCE. Each other question's `related_conversation_snippet` is found
in its history by the exact text of its user turns. Those turns are the
evidence: a row counts how many of them appear whole in what Search
served.
"""

from __future__ import annotations

import ast
import csv
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
DATA = _HERE / "data" / "personamem_v2"
BENCHMARK = Path("benchmark") / "text" / "benchmark.csv"
SIZE = "128k"
MODE = "mcq"
KEEP_PERSONA_PROMPT = True
USER_PREFIX = "personamem-v2:"

# AML's post_chat sends no max_tokens, so the provider default applies;
# 16,384 is gpt-4o-mini's own output ceiling, the same as leaving it unset.
MAX_TOKENS = 16384
# The judge's first allowance, as run.py gives AML's judge; an
# unparseable judgment is retried once with 4,000.
JUDGE_MAX_TOKENS = 800
JUDGE_RETRY_MAX_TOKENS = 4000

# Verbatim from AML data/personamem/pipeline_v2.py (commit 1b8142b),
# extracted with `ast`, which quotes PersonaMem-v2 inference.py and
# inference_utils.py. The MCQ block differs from upstream inference.py by
# one character: AML starts "Provide your reasoning" on a new line.
RECALL_SUFFIX = " Please recall my related preferences from our conversation history to give personalized responses."

MCQ_PROMPT_TEMPLATE = "Please choose the best answer from the following options:\n\n{options}\n\nThink step by step about which answer best fits the user's query and conversation context.\nProvide your reasoning first, then give your final answer as 'Final Answer: [Letter]'"

JUDGE_PROMPT_NARROW_POSITIVE = "You are evaluating an AI assistant's personalization capabilities in open-ended conversations.\n\nUser's query:\n{user_query}\n\nGround truth user preference: {preference}\n\nModel's response to evaluate:\n{model_response}\n\nTask: Evaluate how well the model personalizes its response according to the user's ground-truth preference. We expect the model to demonstrate awareness of this preference and provide a response that is specifically aligned with or relevant to it, rather than giving a generic answer that could apply to anyone.\n\nA well-personalized response should:\n- Acknowledge or incorporate the specific preference mentioned\n- Tailor recommendations, advice, or information to align with this preference\n- Show that the model remembers and respects the user's stated preference\n\nA poorly personalized response:\n- Ignores the preference entirely and gives generic advice\n- Explicitly states it doesn't remember or track preferences\n- Provides suggestions that contradict the stated preference\n\nGive a score from 0.0 (completely irrelevant to or contradicts the preference) to 1.0 (explicitly and thoughtfully personalized to the preference).\n\nProvide your reasoning first, then give your final score as \\boxed{{score}} where score is a number between 0.0 and 1.0.\nYour response:"

JUDGE_PROMPT_NARROW_NEGATIVE = "You are evaluating an AI assistant's personalization capabilities in open-ended conversations.\n\nUser's query:\n{user_query}\n\nGround truth user preference: {preference}\n\nModel's response to evaluate:\n{model_response}\n\nIn this case, the user's ground-truth preference is a \"do not personalize\" or \"avoid\" instruction about a certain topic or attribute. The user does not want the assistant to tailor its response based on that specific preference, and instead expects a more general or neutral answer that does not rely on or mention that preference.\n\nTask: Evaluate how well the model respects this preference. The model should avoid using or referencing the sensitive preference in a way that personalizes the response, while still providing a helpful and relevant answer.\n\nGive a score from 0.0 (clearly personalizes using the forbidden preference or ignores the instruction) to 1.0 (fully respects the instruction to not personalize while remaining helpful).\n\nProvide your reasoning first, then give your final score as \\boxed{{score}} where score is a number between 0.0 and 1.0.\nYour response:"

# The parse rules, verbatim from the same file's extract_final_letter and
# extract_boxed_score (also extracted with `ast`).
LETTER_PATTERNS = [
    "\\$\\\\boxed\\{([A-Z])\\}\\$",
    "\\\\boxed\\{([A-Z])\\}",
    "Final Answer:\\s*([A-Z])",
    "final answer:\\s*([A-Z])",
    "Answer:\\s*([A-Z])",
    "answer:\\s*([A-Z])",
    "final answer is\\s*\\$?\\\\boxed\\{([A-Z])\\}\\$?",
    "final answer is\\s*([A-Z])",
    "the answer is\\s*\\$?\\\\boxed\\{([A-Z])\\}\\$?",
    "the answer is\\s*([A-Z])",
    "\\b([A-Z])\\.\\s*$",
]

BOXED_PATTERNS = [
    "\\\\boxed\\{([0-9]*\\.?[0-9]+)\\}",
    "\\$\\\\boxed\\{([0-9]*\\.?[0-9]+)\\}\\$",
    "\\\\boxed\\s*\\{([0-9]*\\.?[0-9]+)\\}",
]

SCORE_PATTERNS = [
    "score[:\\s]+([0-9]*\\.?[0-9]+)",
    "rating[:\\s]+([0-9]*\\.?[0-9]+)",
    "([0-9]*\\.[0-9]+)\\s*/\\s*1\\.?0?",
]


# ---------------------------------------------------------------- the seed
#
# The pipeline seeds its shuffle with Python's built-in `hash()` of a str,
# which CPython salts per process unless PYTHONHASHSEED is set: the
# "deterministic" option order is deterministic within one process only,
# and AML's letters for a question are whatever its worker's salt made
# them. This reproduces `hash()` as CPython 3.11+ computes it with
# PYTHONHASHSEED=0 (SipHash-1-3, zero key, over the string's internal
# 1/2/4-byte representation), so the procedure is AML's and the order is
# the same on every run.

_M64 = (1 << 64) - 1


def _rotl(x: int, b: int) -> int:
    return ((x << b) | (x >> (64 - b))) & _M64


def _siphash13(data: bytes) -> int:
    v0 = 0x736F6D6570736575
    v1 = 0x646F72616E646F6D
    v2 = 0x6C7967656E657261
    v3 = 0x7465646279746573

    def rounds(n: int) -> None:
        nonlocal v0, v1, v2, v3
        for _ in range(n):
            v0 = (v0 + v1) & _M64
            v2 = (v2 + v3) & _M64
            v1 = _rotl(v1, 13) ^ v0
            v3 = _rotl(v3, 16) ^ v2
            v0 = _rotl(v0, 32)
            v2 = (v2 + v1) & _M64
            v0 = (v0 + v3) & _M64
            v1 = _rotl(v1, 17) ^ v2
            v3 = _rotl(v3, 21) ^ v0
            v2 = _rotl(v2, 32)

    whole = len(data) - len(data) % 8
    for i in range(0, whole, 8):
        m = int.from_bytes(data[i : i + 8], "little")
        v3 ^= m
        rounds(1)
        v0 ^= m
    b = ((len(data) & 0xFF) << 56) | int.from_bytes(data[whole:], "little")
    v3 ^= b
    rounds(1)
    v0 ^= b
    v2 ^= 0xFF
    rounds(3)
    return v0 ^ v1 ^ v2 ^ v3


def str_hash(text: str) -> int:
    """`hash(text)` as CPython 3.11+ returns it under PYTHONHASHSEED=0."""
    widest = max(map(ord, text), default=0)
    encoding = (
        "latin-1"
        if widest < 0x100
        else "utf-16-le"
        if widest < 0x10000
        else "utf-32-le"
    )
    data = text.encode(encoding, "surrogatepass")
    if not data:
        return 0
    x = _siphash13(data)
    if x >= 1 << 63:
        x -= 1 << 64
    return -2 if x == -1 else x


# ---------------------------------------------------------------- the pipeline


def user_query_text(item: dict[str, Any]) -> str:
    """The pipeline's reading of the `user_query` cell, a Python dict literal."""
    query = item.get("user_query", item.get("question", item.get("query")))
    if isinstance(query, str) and query.strip().startswith("{"):
        try:
            query = ast.literal_eval(query)
        except (ValueError, SyntaxError):
            pass
    if isinstance(query, dict):
        return str(query.get("content", query.get("text", "")))
    return str(query)


def official_mcq_options(
    item: dict[str, Any],
) -> tuple[list[str], dict[str, str], str]:
    """The pipeline's option construction: the correct answer first, then
    the incorrect ones, shuffled by a RNG seeded from the persona and the
    query with its suffix. `random.Random(seed).shuffle` is the pipeline's
    `random.seed(seed); random.shuffle(...)` without the global state."""
    correct_answer = str(item["correct_answer"])
    incorrect_answers = item.get("incorrect_answers", [])
    if isinstance(incorrect_answers, str):
        try:
            incorrect_answers = (
                json.loads(incorrect_answers) if incorrect_answers else []
            )
        except json.JSONDecodeError:
            incorrect_answers = []
    incorrect_answers = list(incorrect_answers)
    if not incorrect_answers:
        raise TypeError("PersonaMem v2 MCQ mode requires `incorrect_answers`.")

    options = [correct_answer] + [str(answer) for answer in incorrect_answers]
    query_for_seed = user_query_text(item) + RECALL_SUFFIX
    seed = str_hash(f"{item.get('persona_id', '')}_{query_for_seed}") % (2**32)
    random.Random(seed).shuffle(options)

    letters = [chr(65 + i) for i in range(len(options))]
    mapping = dict(zip(letters, options))
    correct_letter = next(
        letter for letter, option in mapping.items() if option == correct_answer
    )
    return options, mapping, correct_letter


def extract_final_letter(text: Any) -> str:
    raw = str(text)
    if not raw:
        return ""
    for pattern in LETTER_PATTERNS:
        match = re.search(pattern, raw, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).upper()
    return ""


def parse_score(text: Any) -> float | None:
    """The pipeline's `extract_boxed_score`, except that where it falls
    through to its 0.0 default this returns None, so an unparseable
    judgment can be retried; the caller scores a final None as 0.0."""
    response = str(text)
    if not response:
        return None
    for pattern in BOXED_PATTERNS:
        match = re.search(pattern, response)
        if match:
            try:
                score = float(match.group(1))
                return max(0.0, min(1.0, score))
            except ValueError:
                continue
    for pattern in SCORE_PATTERNS:
        match = re.search(pattern, response, flags=re.IGNORECASE)
        if match:
            try:
                score = float(match.group(1))
                if 0.0 <= score <= 1.0:
                    return score
            except ValueError:
                continue
    return None


def memory_history(contents: list[str]) -> list[dict[str, str]]:
    """The served memories in the pipeline's `chat_history` slot."""
    return [{"role": "user", "content": c} for c in contents]


def answer_messages(inst: dict[str, Any], contents: list[str]) -> list[dict[str, str]]:
    """`official_generative_messages`, and in mcq mode
    `official_mcq_messages`, over the served memories."""
    messages = memory_history(contents)
    messages.append({"role": "user", "content": inst["question"] + RECALL_SUFFIX})
    if MODE == "mcq":
        option_text = "\n".join(
            f"{letter}. {option}" for letter, option in inst["option_mapping"].items()
        )
        messages.append(
            {
                "role": "system",
                "content": MCQ_PROMPT_TEMPLATE.format(options=option_text),
            }
        )
    return messages


def narrow_judge_prompt(question: str, preference: str, response: str) -> str:
    template = (
        JUDGE_PROMPT_NARROW_NEGATIVE
        if preference.lower().startswith("do not")
        else JUDGE_PROMPT_NARROW_POSITIVE
    )
    return template.format(
        user_query=question, preference=preference, model_response=response
    )


# ---------------------------------------------------------------- loading


def _read_rows() -> list[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with (DATA / BENCHMARK).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _history(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["chat_history"] if isinstance(data, dict) else data)


def _sessions(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    persona: list[dict[str, str]] = []
    turns: list[dict[str, str]] = []
    for i, m in enumerate(history):
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if i == 0 and m.get("role") == "system":
            persona.append({"role": "system", "content": content})
        else:
            turns.append({"role": str(m.get("role", "user")), "content": content})
    out = []
    if persona and KEEP_PERSONA_PROMPT:
        out.append({"session_id": "persona", "messages": persona})
    out.append({"session_id": "history", "messages": turns})
    return out


def _evidence(snippet: str, history: list[dict[str, Any]]) -> dict[str, str]:
    """The snippet's user turns that occur in the history, keyed by their
    position there; {} when the snippet is not in it."""
    try:
        messages = json.loads(snippet) if snippet else []
    except json.JSONDecodeError:
        return {}
    where: dict[str, int] = {}
    for i, m in enumerate(history):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            where.setdefault(m["content"], i)
    out: dict[str, str] = {}
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        text = m.get("content")
        if isinstance(text, str) and text in where:
            out[f"turn-{where[text]}"] = text
    return out


def load() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows = _read_rows()
    link_of: dict[str, str] = {}
    histories: dict[str, list[dict[str, Any]]] = {}
    sessions: dict[str, list[dict[str, Any]]] = {}
    questions: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        persona = str(row["persona_id"])
        link = row[f"chat_history_{SIZE}_link"]
        if link_of.setdefault(persona, link) != link:
            raise ValueError(f"persona {persona} has two {SIZE} histories")
        uid = USER_PREFIX + persona
        if link not in histories:
            histories[link] = _history(DATA / link)
            sessions[uid] = _sessions(histories[link])
        _, mapping, correct_letter = official_mcq_options(row)
        q: dict[str, Any] = {
            "question_id": f"pm2-{index:04d}",
            "question_type": row["pref_type"],
            "question": user_query_text(row),
            "answer": row["correct_answer"],
            "preference": row["preference"],
            "option_mapping": mapping,
            "correct_letter": correct_letter,
            "user_id": uid,
            "answer_session_ids": [],
            "evidence_turns": _evidence(
                row["related_conversation_snippet"], histories[link]
            ),
        }
        if MODE == "mcq":
            q["options"] = [f"{k}. {v}" for k, v in mapping.items()]
        questions.append(q)
    return questions, sessions


# ---------------------------------------------------------------- grading


async def answer_and_grade(
    client: Any,
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    reader: str,
    judge: str,
    thinking: str = "low",
) -> dict[str, Any]:
    contents = [h["content"] for h in hits]
    messages = answer_messages(inst, contents)
    ans = await client.complete(
        reader, messages, max_tokens=MAX_TOKENS, temperature=0.0
    )
    generated = ans.text.strip()
    cost = ans.cost
    extra: dict[str, Any] = {}
    if MODE == "mcq":
        letter = extract_final_letter(generated)
        mapping = inst["option_mapping"]
        verdict: bool | None = mapping.get(letter) == mapping[inst["correct_letter"]]
        judge_raw = ""
        extra = {"predicted_letter": letter, "gold_letter": inst["correct_letter"]}
    else:
        jud_messages = [
            {
                "role": "user",
                "content": narrow_judge_prompt(
                    inst["question"], inst["preference"], generated
                ),
            }
        ]
        jud = await client.complete(
            judge,
            jud_messages,
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=0.0,
            reasoning_effort=thinking,
        )
        score = parse_score(jud.text)
        if score is None:
            cost += jud.cost
            jud = await client.complete(
                judge,
                jud_messages,
                max_tokens=JUDGE_RETRY_MAX_TOKENS,
                temperature=0.0,
                reasoning_effort=thinking,
            )
            score = parse_score(jud.text)
        cost += jud.cost
        verdict = None if score is None else score >= 0.5
        judge_raw = jud.text[:400]
        extra = {"score": round(score or 0.0, 4)}
    evidence = inst.get("evidence_turns") or {}
    served = [key for key, text in evidence.items() if any(text in c for c in contents)]
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "abstention": False,
        "n_hits": len(hits),
        "prompt_chars": sum(len(m["content"]) for m in messages),
        "generated": generated,
        "verdict": verdict,
        "judge_raw": judge_raw,
        "evidence_sessions": sorted(evidence),
        "evidence_served": len(served),
        "cost": cost,
        **extra,
    }
