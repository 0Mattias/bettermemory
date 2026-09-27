"""Tests for unit S1's harness, `bench/longmemeval/s1.py`.

S1 reads the LongMemEval-S dev 150 with the S0 arm's served context and
grades L1's and S1's answers in one blinded pass. The cases pinned here
are the ones that would change what is measured without failing loudly:

- the service drifting from the arm S0 measured, or from its budget
- the reader instructions differing from L1's second wording in more than
  the batch size and the stated file size
- a served context that differs from S0's cell passing the parity check
- a grader seeing both answers to one question, or an item naming its arm
- the paired counts or the exact McNemar p computed wrong
- a reader transcript under the S1 wording not matched to its batch

Everything is hermetic: stores live under `tmp_path`, and nothing here
calls a model.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
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


s1 = _load("bench_longmemeval_s1", _BENCH / "longmemeval" / "s1.py")


def _inst(qid: str = "q1") -> dict[str, Any]:
    return {
        "question_id": qid,
        "question_type": "multi-session",
        "question": "Which city did I move to?",
        "answer": "Lisbon",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_dates": ["2023/05/20 (Sat) 12:00", "2023/05/21 (Sun) 12:00"],
        "haystack_session_ids": ["s_tea", "answer_move"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I like green tea."},
                {"role": "assistant", "content": "Green tea is nice."},
            ],
            [
                {
                    "role": "user",
                    "content": "I just moved to the city of Lisbon last week.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": "Welcome to Lisbon!"},
            ],
        ],
        "answer_session_ids": ["answer_move"],
    }


# ---------------------------------------------------------------- the memory


def test_the_service_is_the_s0_best_arm_at_180000(tmp_path: Path) -> None:
    s = s1.service_for(tmp_path / "store")
    assert (s.order, s.serve, s.fill, s.trim, s.budget) == (
        "session",
        200,
        "neighbors",
        "tail",
        180_000,
    )
    assert (s.annotate, s.granularity, s.sheet, s.expand, s.conversational) == (
        "none",
        "rounds",
        "none",
        "none",
        True,
    )
    assert s1.ARM.name == "session-k200-fill_neighbors-trim_tail"


def test_build_serves_from_the_store_without_adding(tmp_path: Path) -> None:
    inst = _inst()
    root = tmp_path / "store"
    s1.s0.aml_run.ingest_and_search(s1.l1.service_for(root), inst)
    row, prompt = s1.build(s1.service_for(root), inst)
    assert "user: I just moved to the city of Lisbon last week." in prompt
    assert prompt.endswith(
        "\n\nCurrent Date: 2023/06/01 (Thu) 10:00\n"
        "Question: Which city did I move to?\nAnswer (step by step):"
    )
    assert row["covered"] is True
    assert row["answer_turns"] == 1
    assert row["context_chars"] == len(s1.l1.reading_context(prompt))
    assert row["gold"] == "Lisbon"


def test_parity_names_a_context_whose_characters_or_coverage_differ() -> None:
    rows = [
        {"question_id": "a", "context_chars": 100, "covered": True},
        {"question_id": "b", "context_chars": 200, "covered": True},
        {"question_id": "c", "context_chars": 300, "covered": False},
    ]

    def cell(chars: int, covered: bool) -> dict[str, Any]:
        return {"best_curve": {"180000": {"chars": chars, "covered": covered}}}

    s0_rows = {"a": cell(100, True), "b": cell(201, True), "c": cell(300, True)}
    assert s1.parity(rows, s0_rows) == ["b", "c"]


# ---------------------------------------------------------------- the readers


def test_the_wording_is_l1s_second_with_only_batch_and_size_changed() -> None:
    back = s1.READER_TEMPLATE.replace(s1._S1_BATCH, s1._L1_BATCH).replace(
        s1._S1_SIZE, s1._L1_SIZE
    )
    assert back == s1.l1.READER_INSTRUCTIONS
    assert "reasoning" not in s1.READER_TEMPLATE


def test_reader_instructions_name_the_files_and_the_measured_size(
    tmp_path: Path,
) -> None:
    text = s1.reader_instructions(
        tmp_path / "batch_03.txt",
        tmp_path / "answers" / "batch_03.json",
        181_000,
        3_100,
    )
    assert f"Step 1: Read {tmp_path / 'batch_03.txt'};" in text
    assert "up to 4 prompt file paths" in text
    assert "each is up to about 181,000 characters and 3,100 lines" in text
    assert text.rstrip().endswith("Reply with only: done.")
    assert str(tmp_path / "answers" / "batch_03.json") in text


def test_batches_hold_four_and_keep_cross_evidence_apart() -> None:
    qids = [f"q{n}" for n in range(10)]
    pairs = [("q0", "q1"), ("q2", "q3")]
    batches = s1.l1.make_batches(qids, pairs, size=s1.BATCH_SIZE)
    assert max(len(b) for b in batches) == 4
    assert sorted(q for b in batches for q in b) == sorted(qids)
    for b in batches:
        assert not {"q0", "q1"} <= set(b)
        assert not {"q2", "q3"} <= set(b)


def _reader_transcript(
    text: str, prompt: str, lines: int, answers: str
) -> list[dict[str, Any]]:
    numbered = "\n".join(f"{n}\tline {n}" for n in range(1, lines + 1))
    return [
        {"type": "user", "timestamp": "t0", "message": {"content": text}},
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "model": s1.l1.READER_MODEL,
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 100,
                    "output_tokens": 5,
                },
                "content": [
                    {
                        "type": "tool_use",
                        "id": "r1",
                        "name": "Read",
                        "input": {"file_path": prompt},
                    },
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": answers},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "r1", "content": numbered},
                    {"type": "tool_result", "tool_use_id": "w1", "content": "ok"},
                ]
            },
        },
    ]


def test_the_audit_matches_a_reader_by_its_s1_instructions(tmp_path: Path) -> None:
    prompt = str(tmp_path / "prompts_wrapped" / "q1.txt")
    answers = str(tmp_path / "answers" / "batch_00.json")
    text = s1.reader_instructions(
        tmp_path / "batches" / "batch_00.txt", Path(answers), 1_000, 100
    )
    specs = {
        "batch_00": {
            "instructions": text,
            "reads": {str(tmp_path / "batches" / "batch_00.txt"), prompt},
            "full_reads": {prompt: 3},
            "write": answers,
        }
    }
    got = s1.audit(
        specs, [("agent-a.jsonl", _reader_transcript(text, prompt, 3, answers))]
    )
    assert got["summary"]["clean"] == 1
    assert got["summary"]["files_not_read_to_end"] == 0
    assert s1.counted_tokens(got["summary"]["usage"]) == 115
    short = s1.audit(
        specs, [("agent-b.jsonl", _reader_transcript(text, prompt, 2, answers))]
    )
    assert short["summary"]["clean"] == 0
    other = s1.audit(
        specs,
        [("agent-c.jsonl", _reader_transcript("another task", prompt, 3, answers))],
    )
    assert other["summary"]["with_answers"] == 0


# ---------------------------------------------------------------- the grade


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {"question_id": f"q{i}", "question": f"Q{i}?", "gold": f"G{i}"}
        for i in range(n)
    ]


def test_each_questions_two_answers_go_to_different_parts_and_name_no_arm() -> None:
    rows = _rows(40)
    answers = {
        arm: {
            r["question_id"]: f"answer from the {arm} arm to {r['question_id']}"
            for r in rows
        }
        for arm in s1.ARMS
    }
    parts, key = s1.blind_pair(answers, rows)
    assert [len(p) for p in parts] == [40, 40]
    for part in parts:
        qids = [key[it["id"]][1] for it in part]
        assert sorted(qids) == sorted(r["question_id"] for r in rows)
        for it in part:
            assert "arm" not in it["id"] and "q" not in it["id"]
    assert len(key) == 80
    assert {tuple(v) for v in key.values()} == {
        (arm, r["question_id"]) for arm in s1.ARMS for r in rows
    }
    firsts = {key[it["id"]][0] for it in parts[0]}
    assert firsts == {"l1", "s1"}


def test_the_blind_is_seeded() -> None:
    rows = _rows(10)
    answers = {arm: {r["question_id"]: arm for r in rows} for arm in s1.ARMS}
    assert s1.blind_pair(answers, rows) == s1.blind_pair(answers, rows)


def test_unblind_puts_labels_back_on_arm_and_question() -> None:
    key = {"i000": ["l1", "q1"], "i001": ["s1", "q1"], "i002": ["s1", "q2"]}
    got = s1.unblind_pair({"i000": "WRONG", "i001": "CORRECT"}, key)
    assert got == {"l1": {"q1": False}, "s1": {"q1": True, "q2": None}}


def test_paired_counts_and_the_exact_mcnemar_p() -> None:
    qids = ["a", "b", "c", "d", "e", "f"]
    verdicts = {
        "l1": {"a": True, "b": True, "c": False, "d": False, "e": False, "f": False},
        "s1": {"a": True, "b": False, "c": True, "d": True, "e": True, "f": False},
    }
    got = s1.paired(verdicts, qids)
    assert (got["l1_correct"], got["s1_correct"], got["both"], got["neither"]) == (
        2,
        4,
        1,
        1,
    )
    assert got["l1_only"] == ["b"] and got["s1_only"] == ["c", "d", "e"]
    assert got["gain"] == 2
    assert got["mcnemar_exact_p"] == 0.625
    assert s1.mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert s1.mcnemar_exact(0, 0) == 1.0


def test_l1_dev_answers_must_equal_the_l1_artifacts(tmp_path: Path) -> None:
    rows = [{"question_id": "a"}, {"question_id": "b"}]
    hyp = tmp_path / "hypotheses.jsonl"
    hyp.write_text(
        "\n".join(
            json.dumps({"question_id": q, "hypothesis": f"h{q}"})
            for q in ("a", "b", "z")
        )
        + "\n"
    )
    art = tmp_path / "l1.json"
    art.write_text(
        json.dumps(
            {
                "rows": [
                    {"question_id": q, "hypothesis": f"h{q}"} for q in ("a", "b", "z")
                ]
            }
        )
    )
    assert s1.l1_dev_answers(rows, hyp, art) == {"a": "ha", "b": "hb"}
    art.write_text(
        json.dumps(
            {
                "rows": [
                    {"question_id": "a", "hypothesis": "ha"},
                    {"question_id": "b", "hypothesis": "changed"},
                ]
            }
        )
    )
    with pytest.raises(SystemExit, match="differ"):
        s1.l1_dev_answers(rows, hyp, art)
