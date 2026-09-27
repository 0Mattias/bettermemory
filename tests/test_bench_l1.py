"""Tests for unit L1's harness, `bench/longmemeval/l1.py`.

L1 reads LongMemEval-S under the benchmark's own protocol with the chat
model as reader. The cases pinned here are the ones that would change
what is measured without failing loudly:

- the reading prompt drifting from upstream's by a character, or its
  slots filled in the wrong order
- the served context differing from what the C0 arm served
- a wrapped prompt losing, adding or moving a byte, or keeping a line the
  Read tool would truncate
- two questions sharing a batch while one's evidence sits in the other's
  haystack
- an answer file missing a question, or carrying one from another batch
- a judge called with other parameters than its validation or the
  benchmark's own script used
- a score computed over the wrong denominator
- a reader transcript with a tool call outside the allowed set, a prompt
  not read to its last line, or recalled memory text in its context

Everything is hermetic: stores live under `tmp_path`, and nothing here
calls a model.
"""

from __future__ import annotations

import asyncio
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


l1 = _load("bench_longmemeval_l1", _BENCH / "longmemeval" / "l1.py")
prompts = _load("judge.prompts", _BENCH / "judge" / "prompts.py")
llm = _load("llm", _BENCH / "llm.py")


# ---------------------------------------------------------------- the prompt


def test_reading_template_is_upstreams_step_by_step_template_byte_for_byte() -> None:
    """Pinned by hash: run_generation.py's "con" template (cot, no key
    expansion) at xiaowu0162/LongMemEval 9e0b455. Re-pin only when the
    declared upstream commit changes."""
    digest = hashlib.sha256(l1.READING_TEMPLATE.encode("utf-8")).hexdigest()
    assert digest == l1.READING_TEMPLATE_SHA256
    assert digest == "9e2b3110622929ab896696dd8937231c7436740ec3b9586f653f97346e19ab2c"
    assert l1.READING_TEMPLATE.count("{}") == 3


@pytest.mark.skipif(
    not l1.UPSTREAM_GENERATION.exists(),
    reason="the fetched upstream file lives in the owner's receipts",
)
def test_reading_template_is_the_literal_in_the_fetched_upstream_file() -> None:
    source = l1.UPSTREAM_GENERATION.read_bytes()
    assert hashlib.sha256(source).hexdigest() == l1.UPSTREAM_GENERATION_SHA256
    assert repr(l1.READING_TEMPLATE) in source.decode("utf-8")


def test_reading_prompt_fills_history_date_and_question_in_upstream_order() -> None:
    out = l1.reading_prompt(
        "user: my {bike} is red\nassistant: nice", "2023/05/30 (Tue) 23:40", "Where?"
    )
    assert out == (
        "I will give you several history chats between you and a user. Please "
        "answer the question based on the relevant chat history. Answer the "
        "question step by step: first extract all the relevant information, and "
        "then reason over the information to get the answer.\n\n\nHistory Chats:"
        "\n\nuser: my {bike} is red\nassistant: nice\n\nCurrent Date: 2023/05/30 "
        "(Tue) 23:40\nQuestion: Where?\nAnswer (step by step):"
    )
    assert l1.reading_context(out) == "user: my {bike} is red\nassistant: nice"


def test_served_context_is_the_text_amls_template_received() -> None:
    aml_run = _load("bench_aml_run_for_l1", _BENCH / "aml" / "run.py")
    hits = [
        {"content": "[2023/05/20 (Sat) 12:00]\nuser: a\nassistant: b"},
        {"content": "user: c"},
    ]
    context = l1.served_context(hits)
    assert context == "[2023/05/20 (Sat) 12:00]\nuser: a\nassistant: b\nuser: c"
    rendered = aml_run.render_answer("Q?", [h["content"] for h in hits])
    assert l1.aml_served_context(rendered) == context
    assert l1.aml_served_context(aml_run.render_answer("Q?", [])) == ""
    assert l1.aml_served_context("no markers here") is None


def test_service_is_the_c0_configuration(tmp_path: Path) -> None:
    s = l1.service_for(tmp_path / "rounds-l1")
    assert (
        s.fill,
        s.order,
        s.annotate,
        s.serve,
        s.trim,
        s.budget,
        s.granularity,
        s.sheet,
        s.expand,
        s.conversational,
    ) == ("none", "rank", "none", 100, "none", 90_000, "rounds", "none", "none", True)


def test_build_serves_the_haystack_under_the_question_date(tmp_path: Path) -> None:
    inst = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What color is my bike?",
        "answer": "red",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_dates": ["2023/05/20 (Sat) 12:00", "2023/05/21 (Sun) 12:00"],
        "haystack_session_ids": ["s_other", "answer_s1"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I like green tea."},
                {"role": "assistant", "content": "Green tea is nice."},
            ],
            [
                {"role": "user", "content": "My bike is red."},
                {"role": "assistant", "content": "A red bike, great."},
            ],
        ],
        "answer_session_ids": ["answer_s1"],
    }
    row, prompt = l1.build(l1.service_for(tmp_path / "store"), inst)
    assert "[2023/05/21 (Sun) 12:00]\nuser: My bike is red." in prompt
    assert prompt.endswith(
        "\n\nCurrent Date: 2023/06/01 (Thu) 10:00\n"
        "Question: What color is my bike?\nAnswer (step by step):"
    )
    assert row["evidence_sessions"] == 1
    assert row["evidence_served"] == 1
    assert row["evidence_all_served"] is True
    assert row["abstention"] is False
    assert row["prompt_sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert row["context_chars"] == len(l1.reading_context(prompt))


# ---------------------------------------------------------------- the wrap


def test_wrap_breaks_long_lines_at_spaces_and_keeps_every_byte() -> None:
    line = ("word " * 900).strip()
    text = "head\n" + line + "\ntail"
    wrapped, inserted = l1.wrap_for_read(text)
    assert inserted == 0
    assert len(wrapped.encode("utf-8")) == len(text.encode("utf-8"))
    assert wrapped.replace("\n", " ") == text.replace("\n", " ")
    lines = wrapped.split("\n")
    assert len(lines) > 3
    assert all(len(x) <= l1.READ_LINE_LIMIT for x in lines)
    assert lines[0] == "head" and lines[-1] == "tail"


def test_wrap_leaves_every_line_the_read_tool_shows_whole() -> None:
    text = "a" * 2000 + "\nb c\n" + "d" * 1999
    assert l1.wrap_for_read(text) == (text, 0)


def test_wrap_breaks_at_the_last_space_before_1800() -> None:
    text = "a" * 1000 + " " + "b" * 700 + " " + "c" * 500
    assert l1.wrap_for_read(text) == (
        "a" * 1000 + " " + "b" * 700 + "\n" + "c" * 500,
        0,
    )


def test_wrap_inserts_and_counts_a_break_where_a_line_has_no_space() -> None:
    wrapped, inserted = l1.wrap_for_read("x" * 4500)
    assert inserted == 2
    assert wrapped.replace("\n", "") == "x" * 4500
    assert [len(x) for x in wrapped.split("\n")] == [1800, 1800, 900]


def test_line_count_numbers_lines_as_the_read_tool_does() -> None:
    assert l1.line_count("a") == 1
    assert l1.line_count("a\nb") == 2
    assert l1.line_count("a\nb\n") == 3


# ---------------------------------------------------------------- batches


def test_cross_evidence_pairs_are_directed() -> None:
    corpus = [
        {
            "question_id": "a",
            "answer_session_ids": ["e1"],
            "haystack_session_ids": ["e1", "e2", "n1"],
        },
        {
            "question_id": "b",
            "answer_session_ids": ["e2"],
            "haystack_session_ids": ["e2", "e1", "n2"],
        },
        {
            "question_id": "c",
            "answer_session_ids": ["e3"],
            "haystack_session_ids": ["e3", "e1"],
        },
        {
            "question_id": "d",
            "answer_session_ids": ["e4"],
            "haystack_session_ids": ["e4", "n1"],
        },
    ]
    assert l1.cross_evidence(corpus) == [("a", "b"), ("a", "c"), ("b", "a")]


def test_batches_are_seeded_full_and_never_join_a_cross_evidence_pair() -> None:
    qids = [f"q{i:03d}" for i in range(500)]
    plain = l1.make_batches(qids, [])
    assert plain == l1.make_batches(list(reversed(qids)), [])
    assert [len(b) for b in plain] == [6] * 83 + [2]
    assert sorted(q for b in plain for q in b) == qids
    x, y = plain[0][0], plain[0][1]
    apart = l1.make_batches(qids, [(x, y)])
    assert not any(x in b and y in b for b in apart)
    assert [len(b) for b in apart] == [6] * 83 + [2]
    assert sorted(q for b in apart for q in b) == qids
    assert apart != l1.make_batches(qids, [(x, y)], seed=l1.BATCH_SEED + 1)


def test_reader_instructions_name_the_two_files_and_the_rules(tmp_path: Path) -> None:
    batch = tmp_path / "batches" / "batch_07.txt"
    answers = tmp_path / "answers" / "batch_07.json"
    text = l1.reader_instructions(batch, answers)
    assert f"Read {batch};" in text
    assert f"to {answers}" in text
    assert "in the form its instructions ask for" in text and "600 words" in text
    for tool in ("memory_search", "memory_write", "episode", "memory_admin"):
        assert tool in text
    assert text == l1.reader_instructions(batch, answers, 2)


def test_both_reader_instruction_versions_stay_byte_for_byte() -> None:
    """The audit matches each transcript to its batch by these texts, so
    neither may change after its readers ran. The second version describes
    the reply as the answer each prompt asks for; the first described it as
    "the step-by-step reasoning", and the API's reasoning_extraction
    safeguard stopped 17 of its 20 readers."""
    v1 = hashlib.sha256(l1.READER_INSTRUCTIONS_V1.encode("utf-8")).hexdigest()
    v2 = hashlib.sha256(l1.READER_INSTRUCTIONS.encode("utf-8")).hexdigest()
    assert v1 == "2e7c89ca1c85f9c7a39145e759977f9a5388f945b110d34a856e0117a68ad9f2"
    assert v2 == "9bf0d699087443e905fafb4e03d9d0177781034d15553e2a1ea3ed2c09dbc8d3"
    assert "reasoning" in l1.READER_INSTRUCTIONS_V1
    assert "reasoning" not in l1.READER_INSTRUCTIONS


# ---------------------------------------------------------------- answers


def test_collect_takes_every_answer_and_names_every_gap(tmp_path: Path) -> None:
    batches = [["a", "b"], ["c"]]
    (tmp_path / "batch_00.json").write_text(
        json.dumps({"a": "  Step 1... The answer is red. ", "b": "Two."})
    )
    (tmp_path / "batch_01.json").write_text(json.dumps({"c": "None."}))
    answers, problems = l1.collect(batches, tmp_path)
    assert problems == []
    assert answers == {"a": "Step 1... The answer is red.", "b": "Two.", "c": "None."}

    (tmp_path / "batch_00.json").write_text(json.dumps({"a": " ", "z": "stray"}))
    (tmp_path / "batch_01.json").write_text("{not json")
    answers, problems = l1.collect(batches, tmp_path)
    assert answers == {}
    assert problems[:3] == [
        "batch_00.json: empty answer for a",
        "batch_00.json: no answer for b",
        "batch_00.json: an answer for z, which is not in the batch",
    ]
    assert problems[3].startswith("batch_01.json: not JSON")
    (tmp_path / "batch_01.json").unlink()
    assert l1.collect(batches, tmp_path)[1][-1] == "batch_01.json: missing"


# ---------------------------------------------------------------- judges


class _FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, model: str, messages: list[dict[str, str]], **kwargs: Any
    ) -> SimpleNamespace:
        self.calls.append({"model": model, "messages": messages, **kwargs})
        return SimpleNamespace(text=self.replies.pop(0), cost=0.001)


def _row(qid: str, qtype: str, gold: str) -> dict[str, Any]:
    return {"question_id": qid, "question_type": qtype, "question": "Q", "gold": gold}


def test_judge_of_record_is_called_as_its_2026_09_22_validation_called_it() -> None:
    item = l1.judge_item(_row("q9_abs", "temporal-reasoning", "3"), "R")
    assert item["abstention"] is True
    client = _FakeClient(["", "Yes"])
    got = asyncio.run(l1.judge_of_record(client, item))
    expected = [
        {
            "role": "user",
            "content": prompts.lme_prompt("temporal-reasoning", "Q", "3", "R", True),
        }
    ]
    assert client.calls == [
        {
            "model": "google/gemini-3.8-flash",
            "messages": expected,
            "max_tokens": tokens,
            "temperature": 0.0,
            "reasoning_effort": "low",
        }
        for tokens in (400, 1600)
    ]
    assert got["verdict"] is True


def test_standard_judge_is_called_as_evaluate_qa_calls_it() -> None:
    item = l1.judge_item(_row("q1", "multi-session", "A"), "R")
    client = _FakeClient(["No."])
    got = asyncio.run(l1.judge_standard(client, item))
    messages = [
        {
            "role": "user",
            "content": prompts.lme_prompt("multi-session", "Q", "A", "R", False),
        }
    ]
    assert client.calls == [
        {
            "model": "openai/gpt-4o-2024-08-06",
            "messages": messages,
            "max_tokens": 10,
            "temperature": 0.0,
        }
    ]
    assert got["verdict"] is False
    # evaluate_qa.py sends model, messages, n=1 (the API's default),
    # temperature 0 and max_tokens 10, and nothing else.
    assert llm.Client.payload(
        "openai/gpt-4o-2024-08-06", messages, max_tokens=10, temperature=0.0
    ) == {
        "model": "openai/gpt-4o-2024-08-06",
        "messages": messages,
        "max_tokens": 10,
        "temperature": 0.0,
    }


# ---------------------------------------------------------------- analysis


def test_summary_counts_every_question_and_an_unparsed_verdict_as_wrong() -> None:
    rows = [
        {
            "question_id": "a",
            "question_type": "multi-session",
            "split": "dev",
            "abstention": False,
            "evidence_all_served": True,
        },
        {
            "question_id": "b",
            "question_type": "multi-session",
            "split": "holdout",
            "abstention": False,
            "evidence_all_served": False,
        },
        {
            "question_id": "c_abs",
            "question_type": "temporal-reasoning",
            "split": "holdout",
            "abstention": True,
            "evidence_all_served": True,
        },
        {
            "question_id": "d",
            "question_type": "temporal-reasoning",
            "split": "dev",
            "abstention": False,
            "evidence_all_served": True,
        },
    ]
    verdicts: dict[str, dict[str, bool | None]] = {
        "g": {"a": True, "b": None, "c_abs": True, "d": False},
        "o": {"a": True, "b": True, "c_abs": True, "d": False},
    }
    s = l1.summarize(rows, verdicts)
    g = s["judges"]["g"]
    assert (g["n"], g["correct"], g["overall"]) == (4, 2, 0.5)
    assert g["by_type"]["multi-session"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert g["category_mean"] == 0.5
    assert g["dev"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert g["holdout"] == {"n": 2, "correct": 1, "accuracy": 0.5}
    assert g["abstention"] == {"n": 1, "correct": 1, "accuracy": 1.0}
    assert g["non_abstention"] == {"n": 3, "correct": 1, "accuracy": 0.3333}
    assert g["evidence_all_served"] == {"n": 3, "correct": 2, "accuracy": 0.6667}
    assert g["evidence_missing"] == {"n": 1, "correct": 0, "accuracy": 0.0}
    assert s["judges"]["o"]["overall"] == 0.75
    assert s["agreement"] == {"n": 4, "agree": 3, "rate": 0.75}
    assert s["disagreements"] == ["b"]


def test_category_mean_is_the_mean_of_the_type_scores_not_of_questions() -> None:
    rows = [
        {
            "question_id": f"m{i}",
            "question_type": "multi-session",
            "split": "dev",
            "abstention": False,
            "evidence_all_served": True,
        }
        for i in range(3)
    ] + [
        {
            "question_id": "t0",
            "question_type": "temporal-reasoning",
            "split": "dev",
            "abstention": False,
            "evidence_all_served": True,
        }
    ]
    s = l1.summarize(rows, {"g": {"m0": True, "m1": True, "m2": True, "t0": False}})
    assert s["judges"]["g"]["overall"] == 0.75
    assert s["judges"]["g"]["category_mean"] == 0.5


# ---------------------------------------------------------------- audit


def _entry(kind: str, content: Any, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "message": {"role": kind, "content": content, **extra}}


def _transcript(
    instructions: str, batch: str, prompt: str, prompt_lines: int, answers: str
) -> list[dict[str, Any]]:
    numbered = "\n".join(f"{n}\tline {n}" for n in range(1, prompt_lines + 1))
    half = prompt_lines // 2
    first = "\n".join(numbered.split("\n")[:half])
    rest = "\n".join(numbered.split("\n")[half:])
    model = {
        "model": "claude-opus-5-5",
        "id": "m1",
        "usage": {"input_tokens": 5, "output_tokens": 7},
    }
    return [
        _entry("user", instructions),
        {
            "type": "attachment",
            "attachment": {
                "type": "instructions",
                "files": [{"content": "see 01M3BNRZG8TE3B39JGK192F9WF"}],
            },
        },
        _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": batch},
                }
            ],
            **model,
        ),
        _entry(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": f"1\t{prompt}\n2\t",
                }
            ],
        ),
        _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Read",
                    "input": {"file_path": prompt},
                }
            ],
            **model,
        ),
        _entry(
            "user", [{"type": "tool_result", "tool_use_id": "t2", "content": first}]
        ),
        _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t3",
                    "name": "Read",
                    "input": {"file_path": prompt, "offset": half + 1},
                }
            ],
            **{**model, "id": "m2"},
        ),
        _entry(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t3",
                    "content": [{"type": "text", "text": rest}],
                }
            ],
        ),
        _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t4",
                    "name": "Write",
                    "input": {"file_path": answers, "content": "{}"},
                }
            ],
            **{**model, "id": "m3"},
        ),
        _entry("user", [{"type": "tool_result", "tool_use_id": "t4", "content": "ok"}]),
        _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t5",
                    "name": "SubagentHandback",
                    "input": {"message": "done."},
                }
            ],
            **{**model, "id": "m4"},
        ),
    ]


def _audit(entries: list[dict[str, Any]], prompt_lines: int = 10) -> dict[str, Any]:
    return l1.audit_transcript(
        entries,
        instructions="INSTR",
        reads={"/w/batches/batch_00.txt", "/w/p/q1.txt"},
        full_reads={"/w/p/q1.txt": prompt_lines},
        write="/w/answers/batch_00.json",
    )


def test_audit_passes_a_reader_that_kept_to_its_files() -> None:
    entries = _transcript(
        "INSTR",
        "/w/batches/batch_00.txt",
        "/w/p/q1.txt",
        10,
        "/w/answers/batch_00.json",
    )
    got = _audit(entries)
    assert got["clean"] is True, got
    assert got["models"] == ["claude-opus-5-5"]
    assert got["tools"] == {"Read": 3, "SubagentHandback": 1, "Write": 1}
    assert got["read_to_end"] == {"/w/p/q1.txt": True}
    assert got["usage"]["output_tokens"] == 28  # four messages, each counted once
    assert got["flagged_attachments"] == []


def test_audit_flags_every_way_a_reader_can_leave_its_files() -> None:
    base = _transcript(
        "INSTR",
        "/w/batches/batch_00.txt",
        "/w/p/q1.txt",
        10,
        "/w/answers/batch_00.json",
    )
    assert _audit(base, prompt_lines=11)["read_to_end"] == {"/w/p/q1.txt": False}
    other = [dict(e) for e in base]
    other[0] = _entry("user", "OTHER")
    assert _audit(other)["instructions_match"] is False
    bash = base + [
        _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t9",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ],
            model="claude-opus-5-5",
        )
    ]
    assert _audit(bash)["violations"] == ["Bash call"]
    stray = base + [
        _entry(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t9",
                    "name": "Read",
                    "input": {"file_path": "/etc/hosts"},
                }
            ],
            model="claude-opus-5-5",
        )
    ]
    assert _audit(stray)["violations"] == ["Read of /etc/hosts"]
    haiku = base + [_entry("assistant", [], model="claude-haiku-4-5")]
    assert _audit(haiku)["clean"] is False
    hook = base + [
        {
            "type": "attachment",
            "attachment": {"type": "hook_additional_context", "content": "recalled"},
        }
    ]
    memory = base + [
        {
            "type": "attachment",
            "attachment": {
                "type": "relevant_memories",
                "text": "01M3GEQVM7D1RX8H4SD3ZQJA9P says",
            },
        }
    ]
    assert _audit(hook)["flagged_attachments"] == ["hook_additional_context"]
    assert _audit(memory)["flagged_attachments"] == ["relevant_memories"]
    assert _audit(hook)["clean"] is False


def test_audit_does_not_count_a_reader_a_safeguard_stopped() -> None:
    """A reader stopped mid-reply is told not to produce the withheld text
    again; whatever it writes afterwards is not a reading under the
    protocol, and one the API refused outright wrote nothing."""
    base = _transcript(
        "INSTR",
        "/w/batches/batch_00.txt",
        "/w/p/q1.txt",
        10,
        "/w/answers/batch_00.json",
    )
    notice = _entry(
        "user",
        "Your response above was stopped by a safety classifier. The rest of it "
        "was withheld.",
    )
    stopped = _audit(base[:8] + [notice] + base[8:])
    assert stopped["safeguard_stops"] == 1
    assert stopped["wrote"] is True
    assert stopped["clean"] is False
    refused = base[:8] + [
        notice,
        {
            "type": "assistant",
            "isApiErrorMessage": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "API Error"}],
            },
        },
    ]
    got = _audit(refused)
    assert (got["api_errors"], got["wrote"], got["clean"]) == (1, False, False)
    assert got["violations"] == ["0 writes"]


# ---------------------------------------------------------------- the grader


def test_blind_items_carry_no_question_id_and_the_key_maps_back() -> None:
    rows = [
        {"question_id": q, "question": f"question {q}", "gold": f"gold {q}"}
        for q in ("a", "b", "c", "d", "e")
    ]
    answers = {q: f"answer {q}" for q in "abcde"}
    parts, key = l1.blind(answers, rows, parts=2)
    assert [len(p) for p in parts] == [3, 2]
    assert (parts, key) == l1.blind(answers, rows, parts=2)
    items = [it for p in parts for it in p]
    assert sorted(key) == sorted(it["id"] for it in items)
    for it in items:
        q = key[it["id"]]
        assert it == {
            "id": it["id"],
            "question": f"question {q}",
            "gold": f"gold {q}",
            "generated": f"answer {q}",
        }
        assert q not in it["id"]
    text = l1.render_items(parts[0])
    assert text.startswith(f"=== ITEM {parts[0][0]['id']} ===\nQuestion: ")
    assert all(len(x) <= l1.READ_LINE_LIMIT for x in text.split("\n"))


def test_grader_verdicts_are_read_back_through_the_key() -> None:
    key = {"i000": "b", "i001": "a"}
    got = l1.unblind({"i000": "CORRECT", "i001": "wrong"}, key)
    assert got == {"b": True, "a": False}
    assert l1.unblind({"i000": "CORRECT"}, key) == {"b": True, "a": None}


def test_jsonl_keeps_a_record_holding_a_unicode_line_separator(tmp_path: Path) -> None:
    """json.dumps leaves U+2028 and U+0085 raw inside strings; a reader that
    split on str.splitlines cut such a transcript line in two and dropped
    the Read result it carried, so a fully read prompt looked unread."""
    records = [
        {"type": "user", "message": {"content": "a b"}},
        {"type": "user", "message": {"content": "c\u0085d"}},
    ]
    path = tmp_path / "agent-x.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    assert l1._jsonl(path) == records
    assert l1._transcripts(tmp_path) == [(path, records)]


def test_price_check_keeps_spending_numbers_and_no_account_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key and credits replies carry the key's label and the account's
    user, organisation and workspace ids; the record keeps none of them."""
    import httpx

    replies = {
        "https://openrouter.ai/api/v1/models": {
            "data": [
                {"id": m, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
                for m in (l1.JUDGE_OF_RECORD, l1.STANDARD_JUDGE)
            ]
        },
        "https://openrouter.ai/api/v1/key": {
            "data": {
                "label": "sk-or-v1-abc...xyz",
                "limit": 150,
                "limit_remaining": 30.5,
                "usage": 119.5,
                "is_free_tier": False,
                "creator_user_id": "user_1",
                "organization_id": "org_1",
                "workspace_id": "ws_1",
            }
        },
        "https://openrouter.ai/api/v1/credits": {
            "data": {"total_credits": 295, "total_usage": 248.5}
        },
    }

    def fake_get(url: str, **_kw: Any) -> SimpleNamespace:
        return SimpleNamespace(status_code=200, json=lambda: replies[url])

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(l1, "_count_tokens", lambda texts: (100, "test"))
    monkeypatch.setattr(
        l1,
        "_judge_items",
        lambda work: [l1.judge_item(_row("q1", "multi-session", "A"), "R")],
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    l1.cmd_price(SimpleNamespace(work=str(tmp_path)))
    saved = (tmp_path / "price.json").read_text(encoding="utf-8")
    for secret in ("sk-or-v1", "user_1", "org_1", "ws_1", "label"):
        assert secret not in saved
    record = json.loads(saved)
    assert record["key"] == {
        "limit": 150,
        "limit_remaining": 30.5,
        "usage": 119.5,
        "is_free_tier": False,
    }
    assert record["credits"] == {"total_credits": 295, "total_usage": 248.5}
    assert record["room_for_cap"] is True
