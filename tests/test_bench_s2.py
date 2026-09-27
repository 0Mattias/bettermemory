"""Tests for unit S2's harness, `bench/longmemeval/s2.py`.

S2 reads the LongMemEval-S holdout 350 under the S0 arm and grades the
answers, with S1's dev answers beside them, under both official judges.
The cases pinned here are the ones that would change what is measured
without failing loudly:

- the holdout drifting from L1's, or a dev question served as holdout
- the service drifting from S1's arm or budget, or the wording from S1's
- a C0 context that differs from L1's passing the parity check, or a
  prompt file L1 did not read taken as the reference
- the judge set dropping, duplicating or mislabelling an answer, or taking
  S1's answers from anywhere but what the S1 artifact recorded
- the paired counts against L1 computed wrong
- a prediction graded on the wrong side of its declared bounds
- the token stop reading the projection wrong

Everything is hermetic: stores live under `tmp_path`, and nothing here
calls a model.
"""

from __future__ import annotations

import importlib.util
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


s2 = _load("bench_longmemeval_s2", _BENCH / "longmemeval" / "s2.py")


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


def test_the_service_and_the_wording_are_s1s(tmp_path: Path) -> None:
    s = s2.service_for(tmp_path / "store")
    assert (s.order, s.serve, s.fill, s.trim, s.budget) == (
        "session",
        200,
        "neighbors",
        "tail",
        180_000,
    )
    assert s2.ARM == s2.s1.ARM
    assert s2.BATCH_SIZE == 4
    args = (tmp_path / "batch_07.txt", tmp_path / "batch_07.json", 190_000, 3_200)
    assert s2.reader_instructions(*args) == s2.s1.reader_instructions(*args)
    assert "reasoning" not in s2.reader_instructions(*args)


def test_the_holdout_must_be_l1s_and_hold_no_dev_question() -> None:
    l1_rows = {
        "a": {"split": "dev"},
        "b": {"split": "holdout"},
        "c": {"split": "holdout"},
    }
    s2.check_split(["b", "c"], l1_rows)
    with pytest.raises(SystemExit):
        s2.check_split(["b"], l1_rows)
    with pytest.raises(SystemExit):
        s2.check_split(["a", "b", "c"], l1_rows)
    with pytest.raises(SystemExit):
        s2.check_split(["b", "c", "c"], l1_rows)


def test_measure_serves_c0_and_the_arm_from_one_store(tmp_path: Path) -> None:
    inst = _inst()
    root = tmp_path / "store"
    s2.s0.aml_run.ingest_and_search(s2.l1.service_for(root), inst)
    row, prompt = s2.measure(root, inst)
    assert row["split"] == "holdout"
    assert row["covered"] is True and row["c0_covered"] is True
    assert row["answer_turns"] == 1
    assert row["context_chars"] == len(s2.l1.reading_context(prompt))
    c0_hits = s2.s0._served(s2.l1.service_for(root), inst, s2.s0.C0, s2.s0.BUDGET)
    c0_context = s2.l1.served_context(c0_hits)
    assert row["c0_context_sha256"] == s2.l1._sha(c0_context)
    assert row["c0_context_chars"] == len(c0_context)
    assert prompt.endswith(
        "\n\nCurrent Date: 2023/06/01 (Thu) 10:00\n"
        "Question: Which city did I move to?\nAnswer (step by step):"
    )


def test_c0_parity_names_a_differing_context_and_refuses_a_foreign_prompt(
    tmp_path: Path,
) -> None:
    prompts = tmp_path / "prompts"
    prompts.mkdir()

    def prompt_for(context: str) -> str:
        return s2.l1.reading_prompt(context, "2023/06/01 (Thu) 10:00", "Where?")

    for qid, context in (("a", "alpha"), ("b", "beta")):
        (prompts / f"{qid}.txt").write_bytes(prompt_for(context).encode("utf-8"))
    l1_rows = {
        "a": {"prompt_sha256": s2.l1._sha(prompt_for("alpha"))},
        "b": {"prompt_sha256": s2.l1._sha(prompt_for("beta"))},
    }
    rows = [
        {"question_id": "a", "c0_context_sha256": s2.l1._sha("alpha")},
        {"question_id": "b", "c0_context_sha256": s2.l1._sha("beta!")},
    ]
    assert s2.c0_parity(rows, l1_rows, prompts) == ["b"]
    l1_rows["a"]["prompt_sha256"] = "0" * 64
    with pytest.raises(SystemExit):
        s2.c0_parity(rows, l1_rows, prompts)


# ---------------------------------------------------------------- the grade


def _row(qid: str, split: str) -> dict[str, Any]:
    return {
        "question_id": qid,
        "question_type": "multi-session",
        "question": f"Question {qid}?",
        "gold": f"gold {qid}",
        "abstention": False,
        "split": split,
        "prompt_chars": 1,
    }


def test_the_judge_set_joins_s1s_recorded_dev_answers_and_s2s_holdout() -> None:
    dev = [_row("a", "dev"), _row("b", "dev")]
    hold = [_row("c", "holdout"), _row("d", "holdout")]
    s1_answers = {"a": "ha", "b": "hb"}
    recorded = {"a": "ha", "b": "hb", "z": "hz"}
    s2_answers = {"c": "hc", "d": "hd"}
    rows, hyps = s2.judge_set(dev, s1_answers, recorded, hold, s2_answers)
    assert [r["question_id"] for r in rows] == ["a", "b", "c", "d"]
    assert [r["split"] for r in rows] == ["dev", "dev", "holdout", "holdout"]
    assert hyps == {"a": "ha", "b": "hb", "c": "hc", "d": "hd"}
    assert all("prompt_chars" not in r for r in rows)
    for r in rows:
        item = s2.l1.judge_item(r, hyps[r["question_id"]])
        assert item["gold"] == f"gold {r['question_id']}"
    with pytest.raises(SystemExit):
        s2.judge_set(dev, {"a": "ha", "b": "edited"}, recorded, hold, s2_answers)
    with pytest.raises(SystemExit):
        s2.judge_set(dev, s1_answers, recorded, hold, {"c": "hc"})
    with pytest.raises(SystemExit):
        s2.judge_set(dev, s1_answers, recorded, [_row("a", "holdout")], {"a": "x"})


def test_paired_against_l1_counts_recoveries_and_regressions() -> None:
    qids = ["a", "b", "c", "d", "e"]
    l1_verdicts = {"a": True, "b": True, "c": False, "d": False, "e": False}
    s2_verdicts: dict[str, bool | None] = {
        "a": True,
        "b": False,
        "c": True,
        "d": True,
        "e": None,
    }
    got = s2.paired(l1_verdicts, s2_verdicts, qids)
    assert (got["l1_correct"], got["s2_correct"], got["both"], got["neither"]) == (
        2,
        3,
        1,
        1,
    )
    assert got["l1_misses"] == 3
    assert got["recovered"] == ["c", "d"] and got["regressed"] == ["b"]
    assert got["gain"] == 1
    assert got["mcnemar_exact_p"] == 1.0


def test_scores_count_a_missing_verdict_as_wrong_and_keep_the_category_mean() -> None:
    rows = [
        {"question_id": "a", "question_type": "t1", "abstention": False},
        {"question_id": "b", "question_type": "t1", "abstention": False},
        {"question_id": "c", "question_type": "t2", "abstention": True},
    ]
    got = s2.score(rows, {"a": True, "b": None, "c": True})
    assert (got["n"], got["correct"], got["overall"]) == (3, 2, round(2 / 3, 4))
    assert got["category_mean"] == 0.75
    assert got["abstention"] == {"n": 1, "correct": 1}
    assert got["by_type"]["t1"] == {"n": 2, "correct": 1, "accuracy": 0.5}


# ---------------------------------------------------------------- predictions


_BASE: dict[str, Any] = {
    "parity": 350,
    "coverage_rate": 0.98,
    "holdout_standard": 342,
    "holdout_record": 343,
    "recovered": 12,
    "regressed": 2,
    "p": 0.001,
    "dev_standard": 146,
    "all_standard": 488,
    "agreement": 0.99,
    "abstentions": 21,
    "audit_ok": True,
    "stops": 0,
    "reader_tokens": 20_800_000,
    "judge_usd": 0.6,
}


def _verdicts(**changes: Any) -> dict[str, str]:
    return {p["id"]: p["verdict"] for p in s2.predictions({**_BASE, **changes})}


def test_the_predictions_are_the_nine_declared_and_hit_at_the_point() -> None:
    got = _verdicts()
    assert list(got) == [f"S2-P{n}" for n in range(1, 10)]
    assert set(got.values()) == {"HIT"}


@pytest.mark.parametrize(
    ("changes", "pid", "verdict"),
    [
        ({"parity": 349}, "S2-P1", "MISSED"),
        ({"coverage_rate": 0.949}, "S2-P2", "MISSED"),
        ({"coverage_rate": 0.96}, "S2-P2", "HIT (outside the point range)"),
        ({"holdout_standard": 331}, "S2-P3", "MISSED"),
        ({"holdout_record": 332}, "S2-P3", "MISSED"),
        (
            {"holdout_standard": 332, "holdout_record": 333},
            "S2-P3",
            "HIT (outside the point range)",
        ),
        ({"holdout_standard": 348}, "S2-P3", "HIT (outside the point range)"),
        ({"recovered": 6}, "S2-P4", "MISSED"),
        ({"regressed": 7}, "S2-P4", "MISSED"),
        ({"p": 0.05}, "S2-P4", "MISSED"),
        ({"p": 0.02}, "S2-P4", "HIT (outside the point range)"),
        ({"dev_standard": 143}, "S2-P5", "MISSED"),
        ({"dev_standard": 150}, "S2-P5", "HIT (outside the point range)"),
        ({"all_standard": 477}, "S2-P6", "MISSED"),
        ({"all_standard": 478}, "S2-P6", "HIT (outside the point range)"),
        ({"agreement": 0.959}, "S2-P7", "MISSED"),
        ({"abstentions": 18}, "S2-P7", "MISSED"),
        ({"abstentions": 20}, "S2-P7", "HIT (outside the point range)"),
        ({"audit_ok": False}, "S2-P8", "MISSED"),
        ({"stops": 4}, "S2-P8", "MISSED"),
        ({"reader_tokens": 26_000_001}, "S2-P9", "MISSED"),
        ({"judge_usd": 2.01}, "S2-P9", "MISSED"),
        ({"reader_tokens": 24_000_000}, "S2-P9", "HIT (outside the point range)"),
    ],
)
def test_each_prediction_follows_its_declared_bounds(
    changes: dict[str, Any], pid: str, verdict: str
) -> None:
    assert _verdicts(**changes)[pid] == verdict


def test_the_token_stop_reads_the_projection_to_the_350() -> None:
    assert s2.projected_tokens(714_000, 12) == 20_825_000
    assert s2.over_stop(26_000_001)
    assert not s2.over_stop(26_000_000)
