"""Tests for unit H1's harness, `bench/longmemeval/h1.py`.

H1 grades Hindsight's published LongMemEval-S answers under both official
judges (part A) and has S2's reader read Hindsight's published served
context under S2's protocol (part B). The cases pinned here are the ones
that would change what is measured without failing loudly:

- a run file other than the pinned one, or one whose questions drift from
  the dataset's, taken as Hindsight's
- part A grading anything but Hindsight's answer against the dataset's gold
- part B's prompt carrying anything but Hindsight's context verbatim, or
  the wording drifting from S1's
- the coverage test missing a turn Hindsight quotes as JSON, or crediting
  a turn it does not hold
- bettermemory's verdicts taken from anywhere but the S2 artifact
- the paired counts, the token projection or a prediction's bounds read
  wrong

Everything is hermetic: nothing here calls a model or opens a store.
"""

from __future__ import annotations

import argparse
import gzip
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


h1 = _load("bench_longmemeval_h1", _BENCH / "longmemeval" / "h1.py")

_MOVE = "I just moved to the city of Lisbon last week."


def _inst(
    qid: str = "q1", question: str = "Which city did I move to?"
) -> dict[str, Any]:
    return {
        "question_id": qid,
        "question_type": "multi-session",
        "question": question,
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
                {"role": "user", "content": _MOVE, "has_answer": True},
                {"role": "assistant", "content": "Welcome to Lisbon!"},
            ],
        ],
        "answer_session_ids": ["answer_move"],
    }


def _result(qid: str, question: str, **extra: Any) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query": question,
        "answer": f"answer {qid}",
        "context": f"## Memory 1\n**[world]** context for {qid}",
        "correct": True,
        **extra,
    }


# ---------------------------------------------------------------- the run file


def test_the_run_file_must_be_the_pinned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run.json.gz"
    raw = gzip.compress(json.dumps({"results": []}).encode("utf-8"))
    path.write_bytes(raw)
    with pytest.raises(SystemExit):
        h1.load_run(path)
    monkeypatch.setattr(h1, "RUN_FILE_SHA256", h1.l1._sha(raw))
    assert h1.load_run(path) == {"results": []}


def test_the_run_must_hold_the_datasets_questions_one_for_one() -> None:
    corpus = [_inst("a", "Where?"), _inst("b", "When?")]
    run = {"results": [_result("a", "Where?"), _result("b", "When?")]}
    assert set(h1.check_run(run, corpus)) == {"a", "b"}
    for results in (
        [_result("a", "Where?")],
        [_result("a", "Where?"), _result("a", "Where?")],
        [_result("a", "Where?"), _result("c", "When?")],
        [_result("a", "Where?"), _result("b", "What?")],
        [_result("a", "Where?"), _result("b", "When?", context=" ")],
        [_result("a", "Where?"), _result("b", "When?", answer=None)],
    ):
        with pytest.raises(SystemExit):
            h1.check_run({"results": results}, corpus)


# ---------------------------------------------------------------- part A


def test_part_a_grades_hindsights_answer_against_the_datasets_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = [_inst("a", "Where?"), _inst("b_abs", "When?")]
    run = {
        "results": [
            _result("a", "Where?", answer=" Lisbon, per the memories. "),
            _result("b_abs", "When?", gold_answers=["not the dataset's"]),
        ]
    }
    monkeypatch.setattr(h1.l1, "_corpus", lambda: corpus)
    monkeypatch.setattr(h1, "load_run", lambda path: run)
    monkeypatch.setattr(h1, "splits", lambda c: {"a": "dev", "b_abs": "holdout"})
    work = tmp_path / "work-a"
    h1.cmd_regrade(argparse.Namespace(work=str(work), run_file="unused"))
    rows = json.loads((work / "meta.json").read_text(encoding="utf-8"))["rows"]
    assert [(r["question_id"], r["split"], r["abstention"]) for r in rows] == [
        ("a", "dev", False),
        ("b_abs", "holdout", True),
    ]
    assert all(r["gold"] == "Lisbon" for r in rows)
    hyps = [
        json.loads(line)
        for line in (work / "hypotheses.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert hyps == [
        {"question_id": "a", "hypothesis": " Lisbon, per the memories. "},
        {"question_id": "b_abs", "hypothesis": "answer b_abs"},
    ]
    item = h1.l1.judge_item(rows[1], hyps[1]["hypothesis"])
    assert (item["gold"], item["abstention"], item["response"]) == (
        "Lisbon",
        True,
        "answer b_abs",
    )


# ---------------------------------------------------------------- part B


def test_the_prompt_carries_hindsights_context_verbatim() -> None:
    context = (
        '## Memory 1\n**[world]** User moved.\n> [{"role": "user", "content": "x"}]'
    )
    row, prompt = h1.build(_inst(), context, "holdout")
    assert prompt == h1.l1.reading_prompt(
        context, "2023/06/01 (Thu) 10:00", "Which city did I move to?"
    )
    assert h1.l1.reading_context(prompt) == context
    assert (row["split"], row["context_chars"], row["prompt_chars"]) == (
        "holdout",
        len(context),
        len(prompt),
    )
    assert row["context_sha256"] == h1.l1._sha(context)
    assert row["prompt_sha256"] == h1.l1._sha(prompt)


def test_the_wording_is_s1s_and_a_batch_holds_three(tmp_path: Path) -> None:
    args = (tmp_path / "batch_107.txt", tmp_path / "batch_107.json", 204_000, 800)
    text = h1.reader_instructions(*args)
    assert text == h1.s1.reader_instructions(*args)
    assert "up to 4 prompt file paths" in text
    assert "204,000 characters and 800 lines" in text
    assert h1.BATCH_SIZE == 3
    batches = h1.l1.make_batches([f"q{n}" for n in range(10)], [], size=h1.BATCH_SIZE)
    assert max(len(b) for b in batches) == 3


def test_normal_folds_width_space_and_case() -> None:
    assert h1.normal("A  B\n\nC ﬁne") == "a b c fine"


def test_windows_are_five_evenly_spaced_and_a_short_text_is_its_own() -> None:
    assert h1.windows("x" * 60) == ["x" * 60]
    text = "".join(chr(ord("a") + n % 26) for n in range(100))
    got = h1.windows(text)
    assert len(got) == 5 and all(len(w) == 60 for w in got)
    assert got[0] == text[:60] and got[-1] == text[-60:]


def test_a_turn_quoted_as_json_counts_only_after_decoding() -> None:
    quoted = 'She said "go" and "stay" and "wait" to me. ' * 3
    inst = _inst()
    inst["haystack_sessions"][1][0]["content"] = quoted
    line = "> " + json.dumps([{"role": "user", "content": quoted}])
    context = "## Memory 1\n**[world]** User moved.\n" + line
    assert all(w not in h1.normal(context) for w in h1.windows(h1.normal(quoted)))
    got = h1.coverage(inst, context)
    assert (got["answer_turns"], got["missing"], got["covered"]) == (1, [], True)


def test_a_cut_quote_stays_readable_and_a_missing_turn_is_named() -> None:
    cut = "> " + json.dumps([{"role": "user", "content": _MOVE}])[:-3]
    assert h1.unquote(cut) == cut
    assert h1.coverage(_inst(), "## Memory 1\n" + cut)["covered"] is True
    got = h1.coverage(_inst(), "## Memory 1\n**[world]** User likes green tea.")
    assert got["missing"] == [(1, 0, 0)] and got["covered"] is False


def test_a_question_without_answer_turns_is_never_covered() -> None:
    inst = _inst()
    del inst["haystack_sessions"][1][0]["has_answer"]
    got = h1.coverage(inst, _MOVE)
    assert (got["answer_turns"], got["covered"]) == (0, False)


# ---------------------------------------------------------------- the audit


def test_the_projection_scales_the_clean_batches_to_the_500() -> None:
    assert h1.projected_tokens(240_000, 3) == 40_000_000
    assert h1.projected_tokens(0, 0) == 0
    assert h1.over_stop(50_000_001)
    assert not h1.over_stop(50_000_000)
    usage = dict.fromkeys(h1.l1._USAGE, 0)
    meta = {"batches": [["a", "b", "c"], ["d", "e"], ["f"]]}
    result = {
        "canonical": {
            "batch_00": {"clean": True, "usage": {**usage, "input_tokens": 200_000}},
            "batch_01": {"clean": False, "usage": {**usage, "input_tokens": 9_999}},
        },
        "summary": {"usage_all_attempts": {**usage, "input_tokens": 300_000}},
    }
    got = h1.progress(result, meta)
    assert (got["batches_clean"], got["questions_read"], got["tokens_clean"]) == (
        1,
        3,
        200_000,
    )
    assert got["projected"] == 200_000 * 500 // 3 and got["over_stop"] is False
    assert got["tokens_all_attempts"] == 300_000


def test_s2s_rate_is_its_counted_tokens_per_prompt_character(tmp_path: Path) -> None:
    (tmp_path / "meta.json").write_text(
        json.dumps({"rows": [{"prompt_chars": 600}, {"prompt_chars": 400}]}),
        encoding="utf-8",
    )
    artifact = tmp_path / "s2.json"
    artifact.write_text(
        json.dumps({"cost": {"reader_tokens": {"counted": 430}}}), encoding="utf-8"
    )
    assert h1.s2_rate(tmp_path, artifact) == 0.43


# ---------------------------------------------------------------- the grade


def test_paired_counts_the_lead_of_the_first_set() -> None:
    qids = ["a", "b", "c", "d", "e"]
    first = {"a": True, "b": True, "c": True, "d": False, "e": None}
    second = {"a": True, "b": False, "c": False, "d": True, "e": False}
    got = h1.paired(first, second, qids)
    assert (got["a_correct"], got["b_correct"], got["both"], got["neither"]) == (
        3,
        2,
        1,
        1,
    )
    assert got["a_only"] == ["b", "c"] and got["b_only"] == ["d"]
    assert got["lead"] == 1 and got["mcnemar_exact_p"] == 1.0


def test_bettermemorys_verdicts_are_s2s_holdout_and_s1s_dev_as_recorded() -> None:
    std, rec = h1.JUDGES["standard"], h1.JUDGES["record"]
    artifact = {
        "rows": [{"question_id": "h", "verdicts": {"s2": {std: False, rec: True}}}],
        "dev_rows": [{"question_id": "d", "verdicts": {"s1": {std: True, rec: None}}}],
    }
    assert h1.bettermemory_verdicts(artifact) == {
        "record": {"h": True, "d": None},
        "standard": {"h": False, "d": True},
    }


# ---------------------------------------------------------------- predictions


_BASE: dict[str, Any] = {
    "own_standard": 462,
    "b_holdout_standard": 339,
    "b_all_standard": 484,
    "lead_holdout_standard": 5,
    "p_holdout_standard": 0.2,
    "audit_ok": True,
    "stops": 0,
    "reader_tokens": 40_000_000,
    "judge_usd": 1.2,
}


def _graded(**changes: Any) -> dict[str, str]:
    return {p["id"]: p["verdict"] for p in h1.predictions({**_BASE, **changes})}


def test_the_predictions_are_the_five_declared_and_hit_at_the_point() -> None:
    got = _graded()
    assert list(got) == [f"H1-P{n}" for n in range(1, 6)]
    assert set(got.values()) == {"HIT"}


@pytest.mark.parametrize(
    ("changes", "pid", "verdict"),
    [
        ({"own_standard": 474}, "H1-P1", "MISSED"),
        ({"own_standard": 439}, "H1-P1", "MISSED"),
        ({"own_standard": 473}, "H1-P1", "HIT (outside the point range)"),
        ({"own_standard": 440}, "H1-P1", "HIT (outside the point range)"),
        ({"b_holdout_standard": 347}, "H1-P2", "MISSED"),
        ({"b_holdout_standard": 327}, "H1-P2", "MISSED"),
        ({"b_holdout_standard": 345}, "H1-P2", "HIT (outside the point range)"),
        ({"lead_holdout_standard": -3}, "H1-P3", "MISSED"),
        ({"lead_holdout_standard": -2}, "H1-P3", "HIT (outside the point range)"),
        ({"lead_holdout_standard": 9}, "H1-P3", "HIT (outside the point range)"),
        (
            {"lead_holdout_standard": 8, "p_holdout_standard": 0.03},
            "H1-P3",
            "HIT (outside the point range)",
        ),
        ({"b_all_standard": 475}, "H1-P4", "MISSED"),
        ({"b_all_standard": 491}, "H1-P4", "MISSED"),
        ({"b_all_standard": 490}, "H1-P4", "HIT"),
        ({"audit_ok": False}, "H1-P5", "MISSED"),
        ({"stops": 4}, "H1-P5", "MISSED"),
        ({"judge_usd": 1.5}, "H1-P5", "MISSED"),
        ({"reader_tokens": 50_000_001}, "H1-P5", "MISSED"),
        ({"reader_tokens": 47_000_000}, "H1-P5", "HIT (outside the point range)"),
        ({"reader_tokens": 33_000_000}, "H1-P5", "HIT (outside the point range)"),
    ],
)
def test_each_prediction_follows_its_declared_bounds(
    changes: dict[str, Any], pid: str, verdict: str
) -> None:
    assert _graded(**changes)[pid] == verdict
