"""Tests for unit F1's harness, `bench/longmemeval/f1.py`.

F1 gives the reader each sampled holdout question's whole history in
LongMemEval's own full-history format and pairs the answers with S2's. The
cases pinned here are the ones that would change what is measured without
failing loudly:

- the sample drifting from the declared stratified draw
- the prompt drifting from upstream's full-history rendering: session
  order, the has_answer flag, json.dumps's escaping, the block layout
- the upstream check loading anything but the pinned file, or importing
  its model clients
- the token stop or a prediction's bounds read wrong

Everything is hermetic: nothing here calls a model. The comparison with
the real upstream file runs only where the receipts hold it.
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


f1 = _load("bench_longmemeval_f1", _BENCH / "longmemeval" / "f1.py")


def _inst(qid: str = "q1", qtype: str = "multi-session") -> dict[str, Any]:
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": "Which café did I like?",
        "answer": "Café Lua",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_dates": ["2023/05/21 (Sun) 12:00", "2023/05/20 (Sat) 09:30"],
        "haystack_session_ids": ["answer_cafe", "s_tea"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I loved Café Lua.", "has_answer": True},
                {"role": "assistant", "content": 'A "cozy" spot.'},
            ],
            [
                {"role": "user", "content": "I like green tea."},
                {"role": "assistant", "content": "Green tea is nice."},
            ],
        ],
        "answer_session_ids": ["answer_cafe"],
    }


# ---------------------------------------------------------------- the sample


def test_quotas_split_by_largest_remainder() -> None:
    counts = {
        "knowledge-update": 55,
        "multi-session": 93,
        "single-session-assistant": 39,
        "single-session-preference": 21,
        "single-session-user": 49,
        "temporal-reasoning": 93,
    }
    assert f1.quotas(counts, 150) == {
        "knowledge-update": 23,
        "multi-session": 40,
        "single-session-assistant": 17,
        "single-session-preference": 9,
        "single-session-user": 21,
        "temporal-reasoning": 40,
    }
    assert f1.quotas({"b": 1, "a": 1}, 1) == {"a": 1, "b": 0}


def test_the_sample_is_the_seeded_stratified_draw_in_holdout_order() -> None:
    holdout = [_inst(f"m{n:02d}", "multi-session") for n in range(12)] + [
        _inst(f"t{n:02d}", "temporal-reasoning") for n in range(8)
    ]
    got = f1.sample(holdout, n=5, seed=7)
    assert got == f1.sample(list(holdout), n=5, seed=7)
    assert sum(q.startswith("m") for q in got) == 3
    assert sum(q.startswith("t") for q in got) == 2
    order = [inst["question_id"] for inst in holdout]
    assert got == sorted(got, key=order.index)
    assert f1.sample(holdout, n=5, seed=8) != got


# ---------------------------------------------------------------- the prompt


def test_the_history_is_upstreams_rendering() -> None:
    inst = _inst()
    tea = [
        {"role": "user", "content": "I like green tea."},
        {"role": "assistant", "content": "Green tea is nice."},
    ]
    cafe = [
        {"role": "user", "content": "I loved Café Lua."},
        {"role": "assistant", "content": 'A "cozy" spot.'},
    ]
    history = (
        "\n### Session 1:\nSession Date: 2023/05/20 (Sat) 09:30\nSession Content:\n"
        "\n" + json.dumps(tea) + "\n"
        "\n### Session 2:\nSession Date: 2023/05/21 (Sun) 12:00\nSession Content:\n"
        "\n" + json.dumps(cafe) + "\n"
    )
    prompt = f1.history_prompt(inst)
    assert prompt == f1.l1.reading_prompt(
        history, "2023/06/01 (Thu) 10:00", "Which café did I like?"
    )
    assert "Caf\\u00e9 Lua" in prompt and "has_answer" not in prompt
    assert '\\"cozy\\"' in prompt
    assert inst["haystack_sessions"][0][0]["has_answer"] is True


def test_the_upstream_loader_takes_only_the_pinned_function(tmp_path: Path) -> None:
    source = (
        "import openai_client_that_is_not_installed\n\n"
        "def prepare_prompt(entry, *args, **kwargs):\n"
        "    return json.dumps(entry['question'])\n"
    )
    path = tmp_path / "run_generation.py"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(SystemExit):
        f1.upstream_prepare(path, "0" * 64)
    prepare = f1.upstream_prepare(path, f1.l1._sha(source))
    assert prepare({"question": "Where?"}) == '"Where?"'


@pytest.mark.skipif(
    not f1.l1.UPSTREAM_GENERATION.exists(), reason="the receipts' upstream copy"
)
def test_the_history_equals_the_real_upstream_prompt() -> None:
    prepare = f1.upstream_prepare(
        f1.l1.UPSTREAM_GENERATION, f1.l1.UPSTREAM_GENERATION_SHA256
    )
    inst = _inst()
    assert f1.upstream_prompt(prepare, inst) == f1.history_prompt(inst)
    assert inst["haystack_sessions"][0][0]["has_answer"] is True


def test_a_row_carries_the_history_size_and_the_split() -> None:
    row, prompt = f1.build(_inst())
    assert (row["split"], row["sessions"], row["prompt_chars"]) == (
        "holdout",
        2,
        len(prompt),
    )
    assert row["context_chars"] == len(f1.l1.reading_context(prompt))
    assert row["gold"] == "Café Lua" and row["prompt_sha256"] == f1.l1._sha(prompt)


# ---------------------------------------------------------------- the audit


def test_the_projection_scales_to_the_150() -> None:
    assert f1.projected_tokens(660_000, 3) == 33_000_000
    assert f1.projected_tokens(0, 0) == 0
    assert f1.over_stop(42_000_001)
    assert not f1.over_stop(42_000_000)


# ---------------------------------------------------------------- predictions


_BASE: dict[str, Any] = {
    "parity": 150,
    "f1_standard": 142,
    "lead_standard": 5,
    "p_standard": 0.1,
    "agreement": 0.99,
    "audit_ok": True,
    "stops": 0,
    "reader_tokens": 35_000_000,
    "judge_usd": 0.2,
}


def _graded(**changes: Any) -> dict[str, str]:
    return {p["id"]: p["verdict"] for p in f1.predictions({**_BASE, **changes})}


def test_the_predictions_are_the_five_declared_and_hit_at_the_point() -> None:
    got = _graded()
    assert list(got) == [f"F1-P{n}" for n in range(1, 6)]
    assert set(got.values()) == {"HIT"}


@pytest.mark.parametrize(
    ("changes", "pid", "verdict"),
    [
        ({"parity": 149}, "F1-P1", "MISSED"),
        ({"f1_standard": 149}, "F1-P2", "MISSED"),
        ({"f1_standard": 129}, "F1-P2", "MISSED"),
        ({"f1_standard": 148}, "F1-P2", "HIT (outside the point range)"),
        ({"f1_standard": 130}, "F1-P2", "HIT (outside the point range)"),
        ({"lead_standard": 0}, "F1-P3", "MISSED"),
        ({"lead_standard": -4}, "F1-P3", "MISSED"),
        ({"lead_standard": 1}, "F1-P3", "HIT (outside the point range)"),
        ({"lead_standard": 10}, "F1-P3", "HIT (outside the point range)"),
        ({"agreement": 0.949}, "F1-P4", "MISSED"),
        ({"agreement": 0.96}, "F1-P4", "HIT (outside the point range)"),
        ({"audit_ok": False}, "F1-P5", "MISSED"),
        ({"stops": 4}, "F1-P5", "MISSED"),
        ({"judge_usd": 0.5}, "F1-P5", "MISSED"),
        ({"reader_tokens": 42_000_001}, "F1-P5", "MISSED"),
        ({"reader_tokens": 41_000_000}, "F1-P5", "HIT (outside the point range)"),
        ({"reader_tokens": 30_000_000}, "F1-P5", "HIT (outside the point range)"),
    ],
)
def test_each_prediction_follows_its_declared_bounds(
    changes: dict[str, Any], pid: str, verdict: str
) -> None:
    assert _graded(**changes)[pid] == verdict
