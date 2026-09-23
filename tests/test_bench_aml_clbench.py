"""Tests for the CL-bench cell of the local AML reproduction
(bench/aml/ds_clbench.py).

CL-bench is graded by a strict rubric judge, so the cases are the ones
that would move a score without a crash:

- the answer or judge prompt drifting from AML's pipeline by a character,
  including the backslash line the answer template opens with
- the memory block or the "(no memories)" fallback rendering differently
- a judgment read differently from AML's parse: fences, a missing
  "Overall Score", a status list sent as a string
- an unparseable judgment scoring anything but 0, or an empty reply
  reaching the judge
- a task's final turn, system prompt or length range landing in the wrong
  field, or a record cut apart at a raw U+2028

The expected renderings were produced by AML's own pipeline functions
(data/clbench/pipeline.py, commit 1b8142b) and frozen here; nothing is
imported from the AML clone. Everything is hermetic: the data is a
synthetic fixture under `tmp_path`, and nothing here calls a model.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BENCH = _ROOT / "bench"


def _load(name: str, path: Path) -> ModuleType:
    if str(_BENCH) not in sys.path:
        sys.path.insert(0, str(_BENCH))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ds = _load("aml.ds_clbench", _BENCH / "aml" / "ds_clbench.py")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- prompts


def test_prompts_match_the_aml_pipeline_exactly() -> None:
    """Pinned by hash of the values AML uses: the answer template literal
    and the judge f-string rendered with its own placeholder names. Re-pin
    only when the upstream source changes, and say which commit."""
    assert _sha(ds._CLBENCH_ANSWER_PROMPT_TEMPLATE) == (
        "72d27a678f272170dc076368aa6429d87d1260baeba5f09e319045746aa37bd3"
    )
    judge = ds.rubric_judge_prompt(
        rubrics_text="{rubrics_text}", model_output="{model_output}"
    )
    assert _sha(judge) == (
        "303fc17cba6b97105bc4456daa2a49d6c35939e75830625b417a3a3239b42db5"
    )


_TASK = {
    "system_prompt": "You are Pluto.",
    "question": "  Doc body.\n\nWrite a poem.  ",
}
_HITS = [
    {
        "content": "user: earlier doc\nassistant: earlier answer",
        "created_at": "2024-05-01T12:00:00+00:00",
    },
    {"content": "user: plain"},
]


def test_answer_prompt_renders_as_the_pipeline_renders_it() -> None:
    with_hits = ds.build_prompt(_TASK, _HITS)
    assert _sha(with_hits) == (
        "09b9063fd9698e4e092aec520cfdd150920a3d0fe55f7c1ef43191c69f6ca94b"
    )
    # the template's escaped backslash survives the strip as a first line
    assert with_hits.startswith("\\\nYou are Pluto.\n\n<context>\n")
    assert (
        "- [2024-05-01T12:00:00+00:00] user: earlier doc\nassistant: earlier answer\n"
        "- user: plain\n</context>"
    ) in with_hits
    assert with_hits.endswith("</task>\n\nDoc body.\n\nWrite a poem.")


def test_no_hits_render_the_pipelines_placeholder() -> None:
    empty = ds.build_prompt(_TASK, [])
    assert _sha(empty) == (
        "8e1a4e4a4425f372c5b73af906646ad38b5b21a9e707cc941a3eb8775acfb946"
    )
    assert "context:\n\n(no memories)\n</context>" in empty
    blank = ds.build_prompt(_TASK, [{"content": "   "}])
    assert blank == empty  # a blank memory is dropped, not printed as "- "


def test_judge_prompt_numbers_the_rubrics_and_drops_blank_ones() -> None:
    rubrics = ds._normalize_rubrics(["Has a title.", " ", "Three stanzas."])
    prompt = ds.rubric_judge_prompt(
        rubrics_text=ds._build_rubrics_text(rubrics), model_output="A poem."
    )
    assert _sha(prompt) == (
        "5c04bd83cb3dc3e8f20e2903279741604238539d174627dde2f415bd407e93ee"
    )
    assert "【Rubrics】:\n1. Has a title.\n2. Three stanzas.\n【Student" in prompt
    assert prompt.endswith('"Overall Score": 0 or 1\n}\n')


# ---------------------------------------------------------------- judging


@pytest.mark.parametrize(
    ("reply", "score", "ratio"),
    [
        (
            '```json\n{"Grading Rationale": "ok", "List of Requirement '
            'Satisfaction Status": ["yes", "no"], "Overall Score": 1}\n```',
            1.0,
            0.5,
        ),
        (
            '{"Overall Score": "1", "List of Requirement Satisfaction Status": '
            '"yes, yes"}',
            1.0,
            1.0,
        ),
        (
            '{"Overall Score": 0, "List of Requirement Satisfaction Status": '
            '"[\\"yes\\", \\"no\\", \\"no\\"]"}',
            0.0,
            1 / 3,
        ),
        ('{"Overall Score": "1 point"}', 0.0, 0.0),
        ('{"Overall Score": 1.0}', 1.0, 0.0),
        ('```\n{"Overall Score": true}\n```', 1.0, 0.0),
    ],
)
def test_judgments_parse_as_the_pipeline_parses_them(
    reply: str, score: float, ratio: float
) -> None:
    parsed = ds.parse_judgment(reply)
    assert parsed is not None
    assert parsed["score"] == score
    assert parsed["ratio"] == pytest.approx(ratio)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "not json",
        '{"score": 1}',  # no "Overall Score": the pipeline re-asks
        'Here you go: {"Overall Score": 1}',  # prose before the JSON
        '["Overall Score"]',  # JSON, but not an object
    ],
)
def test_unparseable_judgments_are_none(reply: str) -> None:
    assert ds.parse_judgment(reply) is None


def test_the_reply_is_cut_after_final_answer_and_think() -> None:
    assert ds._normalize_model_output("plan</think> Final Answer: done") == "done"
    assert ds._normalize_model_output("  </think>  ") == ""


class _FakeClient:
    """Replays canned texts: the first call is the reader, the rest the
    judge. Records every call's model and max_tokens."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, model: str, messages: list[dict[str, str]], **kw: Any
    ) -> SimpleNamespace:
        self.calls.append({"model": model, "messages": messages, **kw})
        return SimpleNamespace(text=self.texts.pop(0), cost=0.01)


_INST = {
    "question_id": "t1",
    "question_type": "Rule System Application",
    "length_range": "0-4k",
    "system_prompt": "You are Pluto.",
    "question": "Doc body.\n\nWrite a poem.",
    "rubric": ["Has a title.", "Three stanzas."],
}
_PASS = (
    '{"Grading Rationale": "r", "List of Requirement Satisfaction Status": '
    '["yes", "yes"], "Overall Score": 1}'
)
_PARTIAL = (
    '{"Grading Rationale": "r", "List of Requirement Satisfaction Status": '
    '["yes", "no"], "Overall Score": 0}'
)


async def test_a_graded_task_carries_strict_score_and_rubric_coverage() -> None:
    client = _FakeClient("Final Answer: A poem.", _PARTIAL)
    row = await ds.answer_and_grade(client, _INST, _HITS, "reader", "judge", "off")
    reader, judge = client.calls
    assert reader["messages"] == [
        {"role": "user", "content": ds.build_prompt(_INST, _HITS)}
    ]
    assert reader["max_tokens"] == ds.MAX_TOKENS and reader["temperature"] == 0.0
    assert judge["model"] == "judge" and judge["reasoning_effort"] == "off"
    # the judge grades the reply after its "Final Answer:" cut
    assert "【Student Response】:\nA poem.\n" in judge["messages"][0]["content"]
    assert (row["score"], row["verdict"], row["rubric_ratio"]) == (0.0, False, 0.5)
    assert row["rubric_scores"] == [1.0, 0.0]
    assert row["cost"] == pytest.approx(0.02)
    assert set(row) >= {
        "question_id",
        "question_type",
        "abstention",
        "n_hits",
        "prompt_chars",
        "generated",
        "verdict",
        "score",
        "rubric_scores",
        "judge_raw",
        "evidence_sessions",
        "evidence_served",
        "cost",
    }


async def test_an_unparseable_judgment_is_retried_once_with_room() -> None:
    client = _FakeClient("A poem.", "", _PASS)
    row = await ds.answer_and_grade(client, _INST, [], "reader", "judge")
    allowances = [c["max_tokens"] for c in client.calls[1:]]
    assert allowances == [ds.JUDGE_MAX_TOKENS, ds.JUDGE_RETRY_MAX_TOKENS]
    assert (row["score"], row["verdict"], row["rubric_ratio"]) == (1.0, True, 1.0)
    assert row["cost"] == pytest.approx(0.03)  # every call's spend is kept


async def test_a_judgment_unparseable_after_the_retry_scores_zero() -> None:
    client = _FakeClient("A poem.", "no json", "still none")
    row = await ds.answer_and_grade(client, _INST, [], "reader", "judge")
    assert len(client.calls) == 3
    assert (row["score"], row["verdict"], row["rubric_scores"]) == (0.0, None, None)
    assert row["judge_raw"] == "still none"


async def test_an_empty_reply_scores_zero_without_a_judge_call() -> None:
    client = _FakeClient("Final Answer:   ")
    row = await ds.answer_and_grade(client, _INST, [], "reader", "judge")
    assert len(client.calls) == 1
    assert (row["score"], row["verdict"]) == (0.0, False)
    assert row["judge_raw"].startswith("No model output")


# ---------------------------------------------------------------- loading


def _record(task: str, category: str, *turns: tuple[str, str]) -> dict[str, Any]:
    return {
        "messages": [{"role": "system", "content": f"system for {task}"}]
        + [{"role": r, "content": c} for r, c in turns],
        "rubrics": ["r1", "r2"],
        "metadata": {
            "task_id": task,
            "context_id": "ctx",
            "context_category": category,
            "sub_category": "sub",
        },
    }


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    doc = "Rule 7: blue beats red. " + "x" * 80_000  # ~20k tokens
    records = [
        _record("short", "Rule System Application", ("user", "Doc. Task?")),
        _record(
            "sequential",
            "Procedural Task Execution",
            ("user", doc + " First task?"),
            ("assistant", "Reference answer one."),
            ("user", "Second task?"),
        ),
        _record("middle", "Domain Knowledge Reasoning", ("user", "y" * 30_000)),
    ]
    path = tmp_path / "CL-bench.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "DATA", path)
    monkeypatch.delenv("CLBENCH_ADD", raising=False)
    monkeypatch.delenv("CLBENCH_RANGES", raising=False)
    return path


def test_load_keeps_amls_two_ranges_one_scope_per_task(data: Path) -> None:
    questions, sessions = ds.load()
    by_id = {q["question_id"]: q for q in questions}
    assert sorted(by_id) == ["sequential", "short"]  # 4-16k is not scored
    assert by_id["short"]["length_range"] == "0-4k"
    assert by_id["sequential"]["length_range"] == "16-32k"
    seq = by_id["sequential"]
    assert seq["question"] == "Second task?"
    assert seq["system_prompt"] == "system for sequential"
    assert seq["rubric"] == ["r1", "r2"] and seq["answer"] == ""
    assert seq["question_type"] == "Procedural Task Execution"
    assert seq["user_id"] == "clbench:sequential" and seq["answer_session_ids"] == []
    assert set(sessions) == {"clbench:short", "clbench:sequential"}
    [chunk] = sessions["clbench:sequential"]
    roles = [m["role"] for m in chunk["messages"]]
    assert roles == ["user", "assistant", "user"]  # system prompt never stored
    assert " " in chunk["messages"][0]["content"]  # the record was not cut
    assert chunk["messages"][-1]["content"] == "Second task?"


def test_history_mode_stores_only_turns_before_the_question(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLBENCH_ADD", "history")
    _, sessions = ds.load()
    assert sessions["clbench:short"] == []  # a single-turn task has no history
    [chunk] = sessions["clbench:sequential"]
    assert [m["role"] for m in chunk["messages"]] == ["user", "assistant"]


def test_all_ranges_and_bad_values(data: Path) -> None:
    questions, _ = ds.load(ranges="all")
    assert {q["length_range"] for q in questions} == {"0-4k", "4-16k", "16-32k"}
    only, _ = ds.load(ranges="4-16k")
    assert [q["question_id"] for q in only] == ["middle"]
    with pytest.raises(ValueError):
        ds.load(ranges="0-8k")
    with pytest.raises(ValueError):
        ds.load(add="everything")
