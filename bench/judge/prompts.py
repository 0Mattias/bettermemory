"""The judge prompts every QA harness in bench/ grades with, verbatim from
their sources, and the parse rule each source applies.

`lme_prompt` / `parse_lme`: LongMemEval src/evaluation/evaluate_qa.py.
`aml_prompt` / `parse_aml`: Agent Memory Leaderboard
data/longmemeval-s/pipeline.py, commit 1b8142b.

Kept in one module so a harness that grades and the harness that chose
the judge cannot drift apart on a single character of prompt text.
"""

from __future__ import annotations

import json
import re


def lme_prompt(
    task: str, question: str, answer: str, response: str, abstention: bool
) -> str:
    """Verbatim from LongMemEval src/evaluation/evaluate_qa.py."""
    if not abstention:
        if task in ("single-session-user", "single-session-assistant", "multi-session"):
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == "temporal-reasoning":
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == "knowledge-update":
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == "single-session-preference":
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        else:
            raise ValueError(task)
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
    return template.format(question, answer, response)


# Verbatim from AML data/longmemeval-s/pipeline.py (commit 1b8142b).
AML_ACCURACY_PROMPT = """Your task is to label an answer as ’CORRECT’ or ’WRONG’ given:
(1) a question,
(2) a gold (ground truth) answer,
(3) a generated answer.

Core principle — Inclusion + Non-contradiction
- Be GENEROUS: if the generated answer clearly includes the gold’s key content (or a clear paraphrase of the same content) and does not contradict it, mark CORRECT — even if extra details are added.
- Mark WRONG only when the generated answer does not include the gold’s content, changes it, or contradicts it.

TIME (strict granularity; relative form equivalence; no calendar math)
- Granularity must match exactly: HOUR↔HOUR, DAY↔DAY, MONTH↔MONTH, YEAR↔YEAR.
  Do not answer a gold at a different time unit — even if the numeric value overlaps. Do not answer a month-level gold with a specific day, nor a year with a specific month/day/hour, etc.
  (e.g., gold = "July 26, 2019" [DAY]; generated = "2019-07-26 08:09:17" [includes Second] → WRONG)
- Do NOT convert relative ↔ absolute. If the gold uses a relative time expression, the generated answer must also use a relative form (or a clear paraphrase of that same form), not a computed date/range.
- Treat harmless modifiers in relative forms (e.g., “the/last/previous/just prior”) as equivalent when both the anchor date and the time unit are the same.

- Lists of DISTINCT facts:
- If the gold answer lists multiple distinct facts (joined by "and", commas, or slashes), the generated answer must cover **all** of them.
- Extra non-contradictory items **generally count as WRONG**.
    - Example: gold = A, B, C ; gen = A, B, C → CORRECT
    - Example: gold = A, B, C ; gen = A, B, C, D → WRONG
- Exception: If a gold element is elaborated or split into finer details in the generated answer (e.g., C → C, C′), it is still considered CORRECT.

Preference/Benefit Questions (e.g., "what X likes/values most")
- If gold lists multiple reasons/aspects, the generated answer only needs to include **any one** of them without contradiction to be CORRECT.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG.
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label":

```json
{{
    "label": "CORRECT" or "WRONG"
}}
```"""


def aml_prompt(question: str, gold: str, generated: str) -> str:
    values = {"question": question, "gold_answer": gold, "generated_answer": generated}
    return re.sub(
        r"\{(question|gold_answer|generated_answer)\}",
        lambda m: values[m.group(1)],
        AML_ACCURACY_PROMPT,
    )


def parse_lme(text: str) -> bool:
    """The official rule: 'yes' anywhere in the lowercased response."""
    return "yes" in text.lower()


def parse_aml(text: str) -> bool | None:
    """AML's parse_judge_label; None where AML would raise."""
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    label = str(payload.get("label", "")).upper()
    if label not in {"CORRECT", "WRONG"}:
        return None
    return label == "CORRECT"


# ---------------------------------------------------------------- BEAM
# Verbatim from AML data/beam/pipeline.py (commit 1b8142b), which in turn
# quotes BEAM src/prompts.py at upstream commit
# 3e12035532eb85768f1a7cd779832b650c4b2ef9. Copied by `ast` from the
# pipeline source, not retyped.

BEAM_ANSWER_GENERATION_FOR_RAG = "\nYou are an assistant that MUST answer questions using ONLY the information provided in the context below. \n\nSTRICT INSTRUCTIONS:\n1. Answer ONLY based on the provided context\n2. Do NOT use your internal knowledge\n\nCONTEXT:\n<context>\n\nQUESTION:\n<question>\n\nANSWER REQUIREMENTS:\n- Be direct and concise\n- Only output the answer to the question without any explanation \n\nRESPONSE:\n"

BEAM_UNIFIED_LLM_JUDGE_BASE_PROMPT = '\nYou are an expert evaluator tasked with judging whether the LLM\'s response demonstrates compliance with the specified RUBRIC CRITERION.\n\n## EVALUATION INPUTS\n- QUESTION (what the user asked): <question>\n- RUBRIC CRITERION (what to check): <rubric_item>\n- RESPONSE TO EVALUATE: <llm_response>\n\n## EVALUATION RUBRIC:\nThe rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate. \n\n**IMPORTANT**: Pay careful attention to whether the rubric specifies:\n- **Positive requirements** (things the response SHOULD include/do)\n- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")\n\n## RESPONSIVENESS REQUIREMENT (anchored to the QUESTION)\nA compliant response must be **on-topic with respect to the QUESTION** and attempt to answer it.\n- If the response does not address the QUESTION, score **0.0** and stop.\n- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.\n\n## SEMANTIC TOLERANCE RULES:\nJudge by meaning, not exact wording.\n- Accept **paraphrases** and **synonyms** that preserve intent.\n- **Case/punctuation/whitespace** differences must be ignored.\n- **Numbers/currencies/dates** may appear in equivalent forms (e.g., “$68,000”, “68k”, “68,000 USD”, or “sixty-eight thousand dollars”). Treat them as equal when numerically equivalent.\n- If the rubric expects a number or duration, prefer **normalized comparison** (extract and compare values) over string matching.\n\n## STYLE NEUTRALITY (prevents style contamination):\nIgnore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure (e.g., “itemized list”, “no citations”, “one sentence”).\n- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.\n- Only evaluate format when the rubric **explicitly** mandates it.\n\n## SCORING SCALE:\n- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.\n  - Positive: required element present, accurate, properly executed (allowing semantic equivalents).\n  - Negative: prohibited element **absent** AND response is **responsive**.\n  \n- **0.5 (Partial Compliance)**: Partially complies.\n  - Positive: element present but minor inaccuracies/incomplete execution.\n  - Negative: generally responsive and mostly avoids the prohibited element but with minor/edge violations.\n  \n- **0.0 (No Compliance)**: Fails to comply.\n  - Positive: required element missing or incorrect.\n  - Negative: prohibited element present **or** response is non-responsive/evasive even if the element is absent.\n\n## EVALUATION INSTRUCTIONS:\n1. **Understand the Requirement**: Determine if the rubric is asking for something to be present (positive) or absent (negative/constraint).\n\n2. **Parse Compound Statements**: If the rubric contains multiple elements connected by "and" or commas, evaluate whether:\n   - **All elements** must be present for full compliance (1.0)\n   - **Some elements** present indicates partial compliance (0.5)\n   - **No elements** present indicates no compliance (0.0)\n   \n3. **Check Compliance**: \n   - For positive requirements: Look for the presence and quality of the required element\n   - For negative constraints: Look for the absence of the prohibited element\n\n4. **Assign Score**: Based on compliance with the specific rubric criterion according to the scoring scale above.\n\n5. **Provide Reasoning**: Explain whether the rubric criterion was satisfied and justify the score.\n\n## OUTPUT FORMAT:\nReturn your evaluation in JSON format with two fields:\n\n{\n   "score": [your score: 1.0, 0.5, or 0.0],\n   "reason": "[detailed explanation of whether the rubric criterion was satisfied and why this justified the assigned score]"\n}\n\nNOTE: ONLY output the json object, without any explanation before or after that\n'

BEAM_BATCH_OUTPUT_FORMAT = '## OUTPUT FORMAT:\nReturn one independent evaluation for every indexed rubric criterion in JSON:\n\n{\n  "scores": [\n    {"index": 0, "score": 1.0, "reason": "detailed justification"}\n  ]\n}\n\nInclude every index exactly once. Each score must be 1.0, 0.5, or 0.0.\nNOTE: ONLY output the json object, without any explanation before or after that\n'


def beam_answer_prompt(context: str, question: str) -> str:
    values = {"context": context, "question": question}
    return re.sub(
        r"<(context|question)>",
        lambda m: values[m.group(1)],
        BEAM_ANSWER_GENERATION_FOR_RAG,
    )


def beam_batch_judge_prompt(question: str, response: str, rubrics: list[str]) -> str:
    """AML's render_batch_judge_prompt: every rubric item graded in one call."""
    criteria = "\n".join(f"[{i}] {r}" for i, r in enumerate(rubrics))
    prompt = (
        BEAM_UNIFIED_LLM_JUDGE_BASE_PROMPT.replace("<question>", question)
        .replace("<rubric_item>", criteria)
        .replace("<llm_response>", response)
    )
    prompt = prompt[: prompt.index("## OUTPUT FORMAT:")] + BEAM_BATCH_OUTPUT_FORMAT
    return (
        "Evaluate every indexed RUBRIC CRITERION independently. Apply the complete protocol "
        "below separately to each criterion; do not let one criterion affect another.\n\n"
        + prompt
    )


def parse_beam_scores(response: str, count: int) -> list[float] | None:
    """AML's parse_rubric_scores, returning None where AML would raise."""
    candidate = response.strip()
    if candidate.startswith("```"):
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    raw = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return None
    scores: dict[int, float] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None
        try:
            index, score = int(item["index"]), float(item["score"])
        except (KeyError, TypeError, ValueError):
            return None
        reason = item.get("reason")
        if index in scores or not 0 <= index < count or score not in (0.0, 0.5, 1.0):
            return None
        if not isinstance(reason, str) or not reason.strip():
            return None
        scores[index] = score
    if set(scores) != set(range(count)):
        return None
    return [scores[i] for i in range(count)]
