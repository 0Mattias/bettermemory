"""Tests for the PersonaMem v1 cell of the Agent Memory Leaderboard
reproduction (bench/aml/ds_personamem_v1.py).

The cases are the ones that would corrupt the cell silently:

- the answer instruction drifting from AML's pipeline by a character
- the pipeline's role conversion or option parse being reimplemented
  with a different edge case
- a question's store holding a message past its own slice point
- questions that share a slice point being split across stores, or
  questions with different slice points sharing one

Everything is hermetic: the data is a synthetic fixture under
`tmp_path`, and nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import csv
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


pm = _load("aml.ds_personamem_v1", _BENCH / "aml" / "ds_personamem_v1.py")


# ---------------------------------------------------------------- prompt


def test_the_instruction_matches_the_published_pipeline_exactly() -> None:
    """Pinned by hash against AML data/personamem/pipeline_v1.py (commit
    1b8142b). Re-pin only when that source changes, and say which commit."""
    digest = hashlib.sha256(pm.OFFICIAL_INSTRUCTION.encode("utf-8")).hexdigest()
    assert digest == "7209bb4eacd76d1b69198676ef5929b459f2a31a2dfb626e88ffaa9fd87a361e"
    assert pm.official_user_prompt("Q?", "['(a) x']") == (
        "Q?\n\n" + pm.OFFICIAL_INSTRUCTION + "\n\n['(a) x']"
    )
    with pytest.raises(TypeError):
        pm.official_user_prompt("Q?", ["(a) x"])


def test_role_conversion_prefixes_system_and_merges_runs() -> None:
    out = pm.convert_role_system_to_user(
        [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a1"},
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "u3"},
        ]
    )
    assert out == [
        {"role": "user", "content": "[System]: persona\nu1\nu2"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "[System]: persona\nu3"},
    ]


def test_served_memories_become_one_user_message_before_the_question() -> None:
    inst = {"question": "Q?", "all_options": "['(a) x', '(b) y']"}
    msgs = pm.answer_messages(inst, ["m1", "m2"])
    assert msgs == [
        {
            "role": "user",
            "content": "m1\nm2\nQ?\n\n"
            + pm.OFFICIAL_INSTRUCTION
            + "\n\n['(a) x', '(b) y']",
        }
    ]
    assert pm.answer_messages(inst, []) == [
        {"role": "user", "content": pm.official_user_prompt("Q?", inst["all_options"])}
    ]


# ---------------------------------------------------------------- grading


@pytest.mark.parametrize(
    ("reply", "gold", "verdict", "parsed"),
    [
        ("Reasoning first.\n<final_answer>(c)", "(c)", True, "(c)"),
        ("<final_answer>(C)</final_answer>", "(c)", True, "(c)"),
        ("<final_answer> c", "(c)", True, "(c)"),  # bare letter fallback
        ("<final_answer>(c)", "c", True, "(c)"),  # gold without parens
        # nothing after the token: the whole reply is read instead
        ("I pick (b).\n<final_answer>", "(b)", True, "(b)"),
        ("(a) looks close but <final_answer>(b)", "(a)", False, "(b)"),
        ("<final_answer>(a) or (c)", "(c)", False, "(a)(c)"),
        # the bare-letter rule reads the article "a" as an option
        ("<final_answer>It is a good fit.", "(a)", True, "(a)"),
        ("I cannot tell from the history.", "(c)", None, ""),
        ("<final_answer>(e)", "(a)", None, ""),
    ],
)
def test_the_grade_follows_the_pipeline_parse(
    reply: str, gold: str, verdict: bool | None, parsed: str
) -> None:
    assert pm.grade(reply, gold) == (verdict, parsed)
    assert pm.official_extract_answer(reply, gold)[0] is (verdict is True)


# ---------------------------------------------------------------- loader


def _msg(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


CTX_A = [
    _msg("system", "persona A"),
    _msg("user", "User: I love jazz."),
    _msg("assistant", "Assistant: Nice."),
    _msg("user", "User: I play the sax."),
    _msg("system", "persona A"),
    _msg("user", "User: I moved to Oslo."),
    _msg("user", "User: It is cold."),
    _msg("assistant", "Assistant: Stay warm."),
    _msg("user", "User: FUTURE-ONLY I switched to techno."),
    _msg("assistant", "Assistant: Big change."),
]
CTX_B = [
    _msg("system", "persona B"),
    _msg("user", "User: I bake bread."),
    _msg("assistant", "Assistant: Lovely."),
]
OPTIONS = "[\"(a) It's jazz\", '(b) techno', '(c) bread', '(d) none']"


def _fixture(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "shared_contexts_32k.jsonl").write_text(
        json.dumps({"a" * 64: CTX_A}) + "\n" + json.dumps({"b" * 64: CTX_B}) + "\n",
        encoding="utf-8",
    )
    fields = [
        "persona_id",
        "question_id",
        "question_type",
        "topic",
        "user_question_or_message",
        "correct_answer",
        "all_options",
        "shared_context_id",
        "end_index_in_shared_context",
    ]
    rows = [
        ("0", "q1", "recall_user_shared_facts", "music", "a" * 64, 8),
        ("0", "q2", "suggest_new_ideas", "music", "a" * 64, 8),
        ("0", "q3", "track_full_preference_evolution", "music", "a" * 64, 10),
        ("1", "q4", "recall_user_shared_facts", "food", "b" * 64, 3),
    ]
    with (root / "questions_32k.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for pid, qid, qtype, topic, sid, end in rows:
            w.writerow(
                {
                    "persona_id": pid,
                    "question_id": qid,
                    "question_type": qtype,
                    "topic": topic,
                    "user_question_or_message": f"Question {qid}, with a comma.",
                    "correct_answer": "(a)",
                    "all_options": OPTIONS,
                    "shared_context_id": sid,
                    "end_index_in_shared_context": end,
                }
            )


@pytest.fixture
def loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    _fixture(tmp_path / "pm")
    monkeypatch.setattr(pm, "DATA", tmp_path / "pm")
    return pm.load()


def test_one_store_per_context_and_slice_point(loaded: Any) -> None:
    questions, sessions = loaded
    uid = {q["question_id"]: q["user_id"] for q in questions}
    assert uid["q1"] == uid["q2"]  # same context, same slice: one store
    assert uid["q1"] != uid["q3"]  # same context, later slice: its own store
    assert len({uid["q1"], uid["q3"], uid["q4"]}) == 3
    assert set(sessions) == set(uid.values())
    assert all(u.startswith("personamem-v1:32k:") for u in sessions)


def test_a_question_never_sees_messages_past_its_slice(loaded: Any) -> None:
    questions, sessions = loaded
    by_id = {q["question_id"]: q for q in questions}

    def text(qid: str) -> str:
        return "\n".join(
            m["content"]
            for chunk in sessions[by_id[qid]["user_id"]]
            for m in chunk["messages"]
        )

    assert "FUTURE-ONLY" not in text("q1")
    assert "Stay warm" in text("q1")  # the last message inside the slice is kept
    assert "FUTURE-ONLY" in text("q3")
    assert "persona A" not in text("q4") and "jazz" not in text("q4")


def test_sessions_split_on_system_messages_with_user_assistant_roles(
    loaded: Any,
) -> None:
    questions, sessions = loaded
    chunks = sessions[questions[0]["user_id"]]
    assert [c["session_id"] for c in chunks] == ["S0", "S1"]
    assert chunks[0]["messages"] == [
        _msg("user", "[System]: persona A\nUser: I love jazz."),
        _msg("assistant", "Assistant: Nice."),
        _msg("user", "User: I play the sax."),
    ]
    # the second session is merged on its own, never into the first
    assert chunks[1]["messages"] == [
        _msg("user", "[System]: persona A\nUser: I moved to Oslo.\nUser: It is cold."),
        _msg("assistant", "Assistant: Stay warm."),
    ]
    roles = {m["role"] for cs in sessions.values() for c in cs for m in c["messages"]}
    assert roles <= {"user", "assistant"}
    assert all("timestamp" not in m for c in chunks for m in c["messages"])


def test_question_fields_carry_the_search_options_and_the_raw_string(
    loaded: Any,
) -> None:
    questions, _ = loaded
    q = questions[0]
    assert q["question"] == "Question q1, with a comma."
    assert q["answer"] == "(a)"
    assert q["question_type"] == "recall_user_shared_facts"
    assert q["answer_session_ids"] == []
    assert q["options"] == ["(a) It's jazz", "(b) techno", "(c) bread", "(d) none"]
    assert q["all_options"] == OPTIONS  # the prompt gets the string untouched


def test_a_slice_past_the_context_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path / "pm")
    path = tmp_path / "pm" / "questions_32k.csv"
    path.write_bytes(path.read_bytes().replace(b",3\r\n", b",99\r\n"))
    monkeypatch.setattr(pm, "DATA", tmp_path / "pm")
    with pytest.raises(ValueError, match="slice 99"):
        pm.load()


# ---------------------------------------------------------------- answer step


class _FakeClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self, model: str, messages: list[dict[str, str]], **kw: Any
    ) -> Any:
        self.calls.append({"model": model, "messages": messages, **kw})
        return SimpleNamespace(text=self.reply, cost=0.001)


def test_answer_and_grade_sends_the_pipeline_call_and_returns_a_harness_row() -> None:
    client = _FakeClient("  Because of the jazz.\n<final_answer>(a)  ")
    inst = {
        "question_id": "q1",
        "question_type": "recall_user_shared_facts",
        "question": "Q?",
        "answer": "(a)",
        "all_options": OPTIONS,
        "answer_session_ids": [],
    }
    hits = [{"content": "user: I love jazz.", "_session": "S0"}]
    row = asyncio.run(pm.answer_and_grade(client, inst, hits, "r", "j", "off"))
    [call] = client.calls
    assert call["model"] == "r"
    assert call["temperature"] == 0.0 and call["max_tokens"] == pm.MAX_TOKENS
    assert call["messages"] == pm.answer_messages(inst, ["user: I love jazz."])
    assert set(row) == {
        "question_id",
        "question_type",
        "abstention",
        "n_hits",
        "prompt_chars",
        "generated",
        "verdict",
        "judge_raw",
        "evidence_sessions",
        "evidence_served",
        "cost",
    }
    assert row["verdict"] is True and row["judge_raw"] == "(a)"
    assert row["generated"] == "Because of the jazz.\n<final_answer>(a)"
    assert row["n_hits"] == 1 and row["abstention"] is False
    assert row["prompt_chars"] == len(call["messages"][0]["content"])
    assert row["cost"] == 0.001
