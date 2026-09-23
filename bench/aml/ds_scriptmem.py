"""ScriptMem (github.com/memorax-ai/ScriptMem, CC BY-NC 4.0) as AML runs it.

WHAT AML DOES. AML's data/scriptmem/pipeline.py (commit 1b8142b) answers
each question with CHOICE_ANSWER_TEMPLATE over the memories Search
served: one user message to the answer model, temperature 0, no system
message and no max_tokens. It grades the reply with ScriptMem's own exact
option evaluator (upstream src/evaluate.py, reproduced in the pipeline):
option letters are parsed from the reply by fixed rules and compared with
the gold letters, single-choice by identity, multi-select as a set,
ordering as a sequence. No judge model is involved, so credit is 0 or 1.

THE DATA TRAP. ScriptMem's release ships questions, options and gold
answers but not the scripts: each sample's `conversation` holds only a
synthetic two-line `format_example` of the schema (upstream
data/README.md), for copyright reasons, and the manifest's sha256 values
do not match the published raw files. AML must Add transcripts it holds
privately. `load()` reads a sample's transcript from its `conversation`
when that has `session_N` scenes, else from
`data/scriptmem/transcripts/<source>_<sample_id>.json` (the same schema),
and raises TranscriptsWithheld otherwise: Searching empty stores would
grade the answer model's guesses, not a memory.

ISOLATION AND CHUNKING (AML's own are not public). One user_id per
script, the scope AML's gold records key on (`<source>:<sample_id>`);
one Add per `session_N` scene in numeric order, dated by
`session_N_date_time` when it parses ("Unknown" leaves it undated). A
script has many speakers and no assistant, so every line is sent with
role "user" and its speaker named in the content, the way the LoCoMo
loader writes `Speaker: text`; a narration line (speaker null) is sent
as its bare text.

THE QUERY. Each question's text already lists its options and the
answer format; the options also travel as `options`, the Search body's
multiple-choice field.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
DATA = _HERE / "data" / "scriptmem"
RAW = DATA / "raw"
TRANSCRIPTS = DATA / "transcripts"
# The four scripts, in the order of the pipeline's DATASET_FILES.
SOURCES = ("angry", "enemy", "friends", "man_earth")

# AML's post_chat sends no max_tokens, so the provider default applies;
# 16,384 is gpt-4o-mini's own output ceiling, the same as leaving it unset.
MAX_TOKENS = 16384


class TranscriptsWithheld(RuntimeError):
    """The raw files carry no script text and no transcript was supplied."""


# Verbatim from AML data/scriptmem/pipeline.py (commit 1b8142b).
CHOICE_ANSWER_TEMPLATE = """
You are asked to answer a multiple-choice question based on your memories of a conversation.

<instructions>
1. Use only the provided memories. Prefer the memories that answer the question most directly.
2. Your memories are episodic raw observations. Reason about what they imply. Do not refuse just because the answer is not stated verbatim.
3. The question may contain typos. Match it to the most relevant memories even if the wording differs.
4. The question may be single-choice, multi-select, or ordering. The required output format is different for each type. Obey the format stated in the question, not a default format you invent.
5. Do not add unsupported options. Do not omit supported options. Do not hedge.
6. Preserve option letters exactly. Do not rewrite the answer as option text unless the question explicitly asks for that.
7. If memories conflict, prefer the most recent supported memory.
8. Choose "Cannot infer" only when no memory contains any relevant evidence after scanning all memories. Partial or indirect evidence requires a supported answer, not a refusal.
9. When memories conflict in direction, prefer the one semantically closest to the question's core—not the one with the highest keyword overlap.
10. For multi-select: check every option independently. Include if any memory supports it; exclude only if clearly contradicted or out of scope.
11. For "most plausible", "underlying", or "most strongly implies" questions: compare the top candidates directly before choosing; do not default to the option with the most shared vocabulary.
12. Keep reasoning internal. The visible output must be just the answer string required by the question.
</instructions>

<memories>
Memories for user {{speaker_1_name}}:

{{speaker_1_memories}}

Memories for user {{speaker_2_name}}:

{{speaker_2_memories}}
</memories>

Question: {{question}}
Return only the answer, exactly in the format requested by the question:
""".strip()


# The rest of this block is verbatim from the same file: the pipeline's
# prompt rendering and its copy of ScriptMem's exact option evaluator.
def render_answer_prompt(item: dict[str, Any]) -> str:
    return (
        CHOICE_ANSWER_TEMPLATE.replace(
            "{{speaker_1_name}}", str(item.get("speaker_1_name", "speaker 1"))
        )
        .replace("{{speaker_1_memories}}", str(item.get("speaker_1_memories", "")))
        .replace("{{speaker_2_name}}", str(item.get("speaker_2_name", "speaker 2")))
        .replace("{{speaker_2_memories}}", str(item.get("speaker_2_memories", "")))
        .replace("{{question}}", str(item["question"]))
    )


def gold_letters(answer: Any) -> list[str]:
    parts = answer if isinstance(answer, list) else [answer]
    letters: list[str] = []
    for part in parts:
        match = re.match(r"\s*([A-F])\.", str(part))
        if match:
            letters.append(match.group(1))
    return letters


def normalize_prediction_text(text: str) -> str:
    cleaned = str(text or "").strip()
    box_matches = list(re.finditer(r"\\box(?:ed)?\{([^}]*)(?:\}|$)", cleaned))
    if box_matches:
        return box_matches[-1].group(1).strip()
    lower = cleaned.lower()
    if "final answer:" in lower:
        index = lower.index("final answer:")
        cleaned = cleaned[index + len("final answer:") :].strip()
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1].strip()
    return cleaned


def predicted_letters(prediction: str, qa_type: str = "") -> tuple[list[str], bool]:
    normalized = normalize_prediction_text(prediction)
    if not normalized:
        return [], False

    if qa_type in {"multi_select", "ordering"}:
        return predicted_ordered_letters(normalized)

    return predicted_option_letters(normalized)


def predicted_ordered_letters(prediction: str) -> tuple[list[str], bool]:
    paren_matches = list(re.finditer(r"[\(\[]([^\)\]]*)[\)\]]", prediction))
    content = paren_matches[-1].group(1) if paren_matches else prediction
    letters = [letter.upper() for letter in re.findall(r"[A-Fa-f]", content)]
    if letters:
        return letters, len(set(letters)) != len(letters)
    return [], False


def predicted_option_letters(prediction: str) -> tuple[list[str], bool]:
    if re.fullmatch(r"[A-Fa-f]{1,5}", prediction):
        return [letter.upper() for letter in prediction], False
    if re.search(r"\(\s*[A-Fa-f]\s*\)\(\s*[A-Fa-f]\s*\)", prediction) or re.search(
        r"\[\s*[A-Fa-f]\s*\]\[\s*[A-Fa-f]\s*\]",
        prediction,
    ):
        return [], True

    options: set[str] = set()
    token_re = re.compile(r"\([^)]*\)|\[[^\]]*\]")
    for match in token_re.finditer(prediction):
        inner = match.group(0)[1:-1].strip()
        if not inner:
            continue

        single_letter_match = re.fullmatch(r"([A-Fa-f])", inner)
        if single_letter_match:
            options.add(single_letter_match.group(1).upper())
            continue

        labeled_text_match = re.match(r"^([A-Fa-f])\s*[.:]\s*.+$", inner)
        if labeled_text_match:
            options.add(labeled_text_match.group(1).upper())
            continue

        letters_only = re.sub(r"[^A-Za-z]", "", inner)
        if (
            letters_only
            and len(letters_only) <= 5
            and inner[0].upper() in {"A", "B", "C", "D", "E", "F"}
            and re.fullmatch(r"[A-Za-z ]+", inner)
        ):
            options.add(inner[0].upper())
    return sorted(options), False


def score_item(
    qa_type: str, gold: list[str], pred: list[str], malformed: bool
) -> float:
    if malformed:
        return 0.0
    if qa_type == "single_choice":
        return 1.0 if len(gold) == 1 and len(pred) == 1 and gold[0] == pred[0] else 0.0
    if qa_type == "multi_select":
        return (
            1.0
            if bool(gold) and set(gold) == set(pred) and len(pred) == len(set(pred))
            else 0.0
        )
    if qa_type == "ordering":
        return 1.0 if bool(gold) and gold == pred else 0.0
    raise ValueError(f"unsupported qa_type: {qa_type}")


# ---------------------------------------------------------------- loading


_SESSION = re.compile(r"^session_(\d+)$")
_DATE_FORMATS = ("%B %d, %Y", "%I:%M %p on %d %B, %Y")


def _scene_ms(date_time: Any) -> int | None:
    if not isinstance(date_time, str):
        return None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(date_time.strip(), fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return None


def _samples() -> list[tuple[str, str, dict[str, Any]]]:
    """(source, sample_id, sample) in source order, sample ids as the
    pipeline's load_gold_records derives them."""
    out = []
    for source in SOURCES:
        data = json.loads((RAW / f"{source}.json").read_text(encoding="utf-8"))
        for sample_index, sample in enumerate(data):
            sample_id = sample.get("sample_id") or f"{source}-{sample_index}"
            out.append((source, sample_id, sample))
    return out


def _user_id(source: str, sample_id: str) -> str:
    return f"scriptmem:{source}:{sample_id}"


def _chunks(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = sorted(
        (int(m.group(1)), key)
        for key, value in conversation.items()
        if (m := _SESSION.match(key)) and isinstance(value, list)
    )
    chunks = []
    for n, key in scenes:
        ts = _scene_ms(conversation.get(f"{key}_date_time"))
        msgs = []
        for turn in conversation[key]:
            text = str(turn.get("text") or "")
            if not text.strip():
                continue
            speaker = turn.get("speaker")
            msgs.append(
                {
                    "role": "user",
                    "content": f"{speaker}: {text}" if speaker else text,
                    **({"timestamp": ts} if ts else {}),
                }
            )
        if msgs:
            chunks.append({"session_id": f"D{n}", "messages": msgs})
    return chunks


def load_questions() -> list[dict[str, Any]]:
    questions = []
    for source, sample_id, sample in _samples():
        for qa_index, qa in enumerate(sample.get("qa", [])):
            gold = qa["answer"]
            questions.append(
                {
                    "question_id": f"{source}:{sample_id}#q{qa_index:04d}",
                    "question_type": qa["qa_type"],
                    "qa_type": qa["qa_type"],
                    "dataset": source,
                    "question": qa["question"],
                    "options": list(qa["option"]),
                    "answer": "; ".join(str(a) for a in gold)
                    if isinstance(gold, list)
                    else str(gold),
                    "gold": gold,
                    "user_id": _user_id(source, sample_id),
                    "answer_session_ids": [],
                }
            )
    return questions


def load_sessions() -> dict[str, list[dict[str, Any]]]:
    sessions: dict[str, list[dict[str, Any]]] = {}
    withheld = []
    for source, sample_id, sample in _samples():
        conversation = sample.get("conversation") or {}
        side = TRANSCRIPTS / f"{source}_{sample_id}.json"
        if side.exists():
            conversation = json.loads(side.read_text(encoding="utf-8"))
        chunks = _chunks(conversation)
        if not chunks:
            withheld.append(f"{source}:{sample_id}")
            continue
        sessions[_user_id(source, sample_id)] = chunks
    if withheld:
        raise TranscriptsWithheld(
            f"no script text for {', '.join(withheld)}: ScriptMem's release "
            "omits it; put each transcript, in the schema of "
            f"data/README.md, at {TRANSCRIPTS}/<source>_<sample_id>.json"
        )
    return sessions


def load() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Questions and per-script sessions in the shape run.load_locomo
    returns. Raises TranscriptsWithheld when a script's text is missing."""
    sessions = load_sessions()
    return load_questions(), sessions


# ---------------------------------------------------------------- answering


def grade(qa_type: str, gold: Any, generated: str) -> tuple[list[str], bool, float]:
    """(predicted letters, malformed, score) exactly as the pipeline's
    evaluate_official scores one record."""
    pred, malformed = predicted_letters(generated, qa_type)
    return pred, malformed, score_item(qa_type, gold_letters(gold), pred, malformed)


async def answer_and_grade(
    client: Any,
    inst: dict[str, Any],
    hits: list[dict[str, Any]],
    reader: str,
    judge: str,
    thinking: str = "low",
) -> dict[str, Any]:
    """AML's answer call over the served memories, then the exact option
    evaluator; `judge` and `thinking` are unused because no model grades.
    The verdict is None when no option letter could be read from the
    reply (AML scores that 0), False for a wrong or malformed one."""
    prompt = render_answer_prompt(
        {
            "speaker_1_memories": "\n".join(h["content"] for h in hits),
            "question": inst["question"],
        }
    )
    ans = await client.complete(
        reader,
        [{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS,
        temperature=0.0,
    )
    generated = ans.text.strip()
    pred, malformed, score = grade(inst["qa_type"], inst["gold"], generated)
    evidence = set(inst.get("answer_session_ids") or [])
    served = {h.get("_session", "") for h in hits}
    return {
        "question_id": inst["question_id"],
        "question_type": inst["question_type"],
        "abstention": False,
        "n_hits": len(hits),
        "prompt_chars": len(prompt),
        "generated": generated,
        "verdict": None if not pred and not malformed else score == 1.0,
        "judge_raw": ",".join(pred),
        "evidence_sessions": sorted(evidence),
        "evidence_served": len(evidence & served),
        "cost": ans.cost,
    }
