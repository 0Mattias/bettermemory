"""Tests for the ScriptMem cell of the local AML reproduction
(bench/aml/ds_scriptmem.py).

ScriptMem is graded by an exact option evaluator, not a judge, so the
cases are the ones that would move a score without a crash:

- the answer prompt drifting from AML's pipeline by a character
- the option parser reading a reply differently from AML's, above all a
  refusal, a doubled option or a reply with no parenthesised letter
- question ids, user_ids or scene order diverging from AML's records
- empty stores being Searched because the release omits the scripts

Everything is hermetic: the data is a synthetic fixture under
`tmp_path`, and nothing here calls a model.
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


ds = _load("aml.ds_scriptmem", _BENCH / "aml" / "ds_scriptmem.py")


# ---------------------------------------------------------------- prompt


def test_answer_prompt_matches_the_aml_pipeline_exactly() -> None:
    """Pinned by hash of the value AML uses: the literal in
    data/scriptmem/pipeline.py (commit 1b8142b) after the pipeline's own
    `.strip()`. Re-pin only when the upstream source changes, and say
    which commit in the message."""
    digest = hashlib.sha256(ds.CHOICE_ANSWER_TEMPLATE.encode("utf-8")).hexdigest()
    assert digest == "66c421323c0e6c74fc7afe414dc36aac3796d152b2ecf539a9749cd33d93441f"


def test_rendered_prompt_places_memories_under_speaker_one() -> None:
    prompt = ds.render_answer_prompt(
        {"speaker_1_memories": "m1\nm2", "question": "Q?\n\nA. x\nB. y"}
    )
    assert (
        "<memories>\nMemories for user speaker 1:\n\nm1\nm2\n\n"
        "Memories for user speaker 2:\n\n\n</memories>" in prompt
    )
    assert prompt.endswith(
        "Question: Q?\n\nA. x\nB. y\n"
        "Return only the answer, exactly in the format requested by the question:"
    )
    assert "{{" not in prompt


# ---------------------------------------------------------------- parser


@pytest.mark.parametrize(
    ("reply", "letters", "malformed"),
    [
        ("(B)", ["B"], False),
        ("B", ["B"], False),
        ("b", ["B"], False),
        ("[c]", ["C"], False),
        ("(D. He is teasing Bennett.)", ["D"], False),
        ("The answer is (E).", ["E"], False),
        ("\\boxed{A}", ["A"], False),
        ("Reasoning... Final answer: (F)", ["F"], False),
        ("<think>maybe (A)</think>(C)", ["C"], False),
        ("(A)(B)", [], True),
        ("[A][B]", [], True),
        ("(A) or (C)", ["A", "C"], False),  # parsed, and wrong for single choice
        # refusals: the "Cannot infer" option is answered by its letter;
        # refusing in words yields no letter at all
        ("Cannot infer the answer based on the memories.", [], False),
        ("(Cannot infer)", [], False),
        ("", [], False),
    ],
)
def test_single_choice_parse_follows_the_pipeline(
    reply: str, letters: list[str], malformed: bool
) -> None:
    assert ds.predicted_letters(reply, "single_choice") == (letters, malformed)


@pytest.mark.parametrize(
    ("reply", "letters", "malformed"),
    [
        ("(A, C, D)", ["A", "C", "D"], False),
        ("(D, A, C, B)", ["D", "A", "C", "B"], False),
        ("A, C", ["A", "C"], False),
        ("First (B), then: (A, C)", ["A", "C"], False),  # the last group wins
        ("(A, A, C)", ["A", "A", "C"], True),
        # a refusal in words is scraped for letters A-F, not refused
        ("Cannot infer", ["C", "A", "F", "E"], False),
        ("none", ["E"], False),
        ("", [], False),
    ],
)
def test_multi_select_and_ordering_parse_follows_the_pipeline(
    reply: str, letters: list[str], malformed: bool
) -> None:
    for qa_type in ("multi_select", "ordering"):
        assert ds.predicted_letters(reply, qa_type) == (letters, malformed)


def test_scoring_compares_sets_for_multi_select_and_sequences_for_ordering() -> None:
    assert ds.gold_letters("B. text") == ["B"]
    assert ds.gold_letters(["C. x", "A. y", "no letter"]) == ["C", "A"]
    multi = ["A. x", "C. y"]
    assert ds.grade("multi_select", multi, "(C, A)")[2] == 1.0
    assert ds.grade("multi_select", multi, "(A, C, D)")[2] == 0.0
    assert ds.grade("multi_select", multi, "(A, C, A)")[2] == 0.0  # malformed
    order = ["D. x", "A. y", "B. z"]
    assert ds.grade("ordering", order, "(D, A, B)")[2] == 1.0
    assert ds.grade("ordering", order, "(A, D, B)")[2] == 0.0
    assert ds.grade("single_choice", "B. x", "(B)")[2] == 1.0
    assert ds.grade("single_choice", "B. x", "(A) or (B)")[2] == 0.0
    with pytest.raises(ValueError):
        ds.score_item("open", ["A"], ["A"], False)


# ---------------------------------------------------------------- answering


class _Client:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, list[dict[str, str]], dict[str, Any]]] = []

    async def complete(
        self, model: str, messages: list[dict[str, str]], **kw: Any
    ) -> Any:
        self.calls.append((model, messages, kw))
        return SimpleNamespace(text=self.text, cost=0.001)


_INST = {
    "question_id": "angry:conv-0#q0000",
    "question_type": "single_choice",
    "qa_type": "single_choice",
    "question": "Who? \n\nA. Ann\nB. Bo\n\nPlease provide ... e.g., (X).",
    "gold": "B. Bo",
    "answer_session_ids": [],
}


@pytest.mark.parametrize(
    ("reply", "verdict", "judge_raw"),
    [
        (" (B) ", True, "B"),
        ("(A)", False, "A"),
        ("(A)(B)", False, ""),
        ("Cannot infer.", None, ""),
    ],
)
def test_answer_and_grade_makes_the_pipeline_call_and_grades_exactly(
    reply: str, verdict: bool | None, judge_raw: str
) -> None:
    client = _Client(reply)
    hits = [
        {"content": "[d]\nuser: Bo: I did it.", "_session": "D2"},
        {"content": "[d]\nuser: Ann: Not me.", "_session": "D1"},
    ]
    row = asyncio.run(ds.answer_and_grade(client, _INST, hits, "reader", "judge"))
    [(model, messages, kw)] = client.calls
    assert model == "reader"
    assert kw == {"max_tokens": ds.MAX_TOKENS, "temperature": 0.0}
    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"] == ds.render_answer_prompt(
        {
            "speaker_1_memories": "[d]\nuser: Bo: I did it.\n[d]\nuser: Ann: Not me.",
            "question": _INST["question"],
        }
    )
    assert row["verdict"] is verdict
    assert row["judge_raw"] == judge_raw
    assert row["generated"] == reply.strip()
    assert row["n_hits"] == 2 and row["prompt_chars"] == len(messages[0]["content"])
    assert row["abstention"] is False and row["cost"] == 0.001
    assert row["evidence_sessions"] == [] and row["evidence_served"] == 0
    assert "score" not in row  # credit is binary


# ---------------------------------------------------------------- loading


def _raw(conversation: dict[str, Any], qa: list[dict[str, Any]]) -> str:
    return json.dumps([{"qa": qa, "conversation": conversation, "sample_id": "conv-0"}])


_QA = [
    {
        "qa_type": "single_choice",
        "question": "Q0?\n\nA. a\nB. b",
        "option": ["A. a", "B. b"],
        "answer": "B. b",
    },
    {
        "qa_type": "ordering",
        "question": "Q1?\n\nA. a\nB. b\nC. c",
        "option": ["A. a", "B. b", "C. c"],
        "answer": ["C. c", "A. a", "B. b"],
    },
]

_SCRIPT = {
    "speakers": ["Ann", "Bo"],
    "session_1_date_time": "September 22, 1994",
    "session_1": [
        {"type": "narration", "speaker": None, "dia_id": "D1:1", "text": "A door."},
        {"type": "dialogue", "speaker": "Ann", "dia_id": "D1:2", "text": "Hi."},
    ],
    "session_10_date_time": "Unknown",
    "session_10": [
        {"type": "dialogue", "speaker": "Bo", "dia_id": "D10:1", "text": "Late."}
    ],
    "session_2_date_time": "September 29, 1994",
    "session_2": [
        {"type": "dialogue", "speaker": "Bo", "dia_id": "D2:1", "text": "Bye."},
        {"type": "dialogue", "speaker": "Ann", "dia_id": "D2:2", "text": "  "},
    ],
}

_PLACEHOLDER = {"format_example": {"speakers": ["Speaker A"], "session_1": []}}


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "angry.json").write_text(_raw(_SCRIPT, _QA), encoding="utf-8")
    (raw / "friends.json").write_text(_raw(_PLACEHOLDER, _QA[:1]), encoding="utf-8")
    monkeypatch.setattr(ds, "RAW", raw)
    monkeypatch.setattr(ds, "TRANSCRIPTS", tmp_path / "transcripts")
    monkeypatch.setattr(ds, "SOURCES", ("angry", "friends"))
    return tmp_path


def test_questions_carry_aml_ids_one_store_per_script_and_options(
    data: Path,
) -> None:
    qs = ds.load_questions()
    assert [q["question_id"] for q in qs] == [
        "angry:conv-0#q0000",
        "angry:conv-0#q0001",
        "friends:conv-0#q0000",
    ]
    assert [q["user_id"] for q in qs] == [
        "scriptmem:angry:conv-0",
        "scriptmem:angry:conv-0",
        "scriptmem:friends:conv-0",
    ]
    assert [q["question_type"] for q in qs] == [
        "single_choice",
        "ordering",
        "single_choice",
    ]
    assert qs[1]["options"] == ["A. a", "B. b", "C. c"]
    assert qs[1]["question"] == "Q1?\n\nA. a\nB. b\nC. c"
    assert qs[1]["answer"] == "C. c; A. a; B. b"
    assert ds.gold_letters(qs[1]["gold"]) == ["C", "A", "B"]
    assert all(q["answer_session_ids"] == [] for q in qs)


def test_a_withheld_script_is_refused_not_searched_empty(data: Path) -> None:
    with pytest.raises(ds.TranscriptsWithheld, match="friends:conv-0"):
        ds.load()


def test_scenes_load_in_numeric_order_dated_and_speaker_named(data: Path) -> None:
    transcripts = data / "transcripts"
    transcripts.mkdir()
    (transcripts / "friends_conv-0.json").write_text(
        json.dumps({"session_1": [{"speaker": "Cy", "text": "Hey."}]}),
        encoding="utf-8",
    )
    qs, sessions = ds.load()
    assert len(qs) == 3
    angry = sessions["scriptmem:angry:conv-0"]
    assert [c["session_id"] for c in angry] == ["D1", "D2", "D10"]
    sep22 = 780_192_000_000  # 1994-09-22 00:00 UTC
    assert angry[0]["messages"] == [
        {"role": "user", "content": "A door.", "timestamp": sep22},
        {"role": "user", "content": "Ann: Hi.", "timestamp": sep22},
    ]
    # a blank line is dropped; an "Unknown" date leaves the scene undated
    assert angry[1]["messages"] == [
        {"role": "user", "content": "Bo: Bye.", "timestamp": sep22 + 7 * 86_400_000}
    ]
    assert angry[2]["messages"] == [{"role": "user", "content": "Bo: Late."}]
    assert sessions["scriptmem:friends:conv-0"] == [
        {"session_id": "D1", "messages": [{"role": "user", "content": "Cy: Hey."}]}
    ]
