"""Tests for unit S0's harness, `bench/longmemeval/s0.py`.

S0 counts, with no reader and no model, how many LongMemEval-S dev
questions have every answer turn served under each of the bench service's
serving options. The cases pinned here are the ones that would change
what is counted without failing loudly:

- an answer turn mapped to the wrong round (a lone tail, a system message)
- a store round mapped to the wrong session or round
- a trimmed answer counted as served
- a missing round put in the wrong class
- the arm grid drifting from the declared 24, or C0 missing from it
- the best arm picked by anything but the declared rule
- a run that opens stores without a keys directory, or serves the holdout

Everything is hermetic: stores live under `tmp_path`, and nothing here
calls a model.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from bettermemory.store import Store

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


cov = _load("bench_longmemeval_s0", _BENCH / "longmemeval" / "s0.py")


def _inst() -> dict[str, Any]:
    return {
        "question_id": "q1",
        "question_type": "multi-session",
        "question": "Which city did I move to and what bike did I buy?",
        "answer": "Lisbon, a red bike",
        "question_date": "2023/06/01 (Thu) 10:00",
        "haystack_dates": [
            "2023/05/20 (Sat) 12:00",
            "2023/05/21 (Sun) 12:00",
            "2023/05/22 (Mon) 12:00",
        ],
        "haystack_session_ids": ["s_tea", "answer_move", "answer_bike"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I like green tea."},
                {"role": "assistant", "content": "Green tea is nice."},
            ],
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Any tips for packing boxes?"},
                {"role": "assistant", "content": "Label every box."},
                {
                    "role": "user",
                    "content": "I just moved to Lisbon last week.",
                    "has_answer": True,
                },
                {"role": "assistant", "content": "Welcome to Lisbon!"},
            ],
            [
                {"role": "user", "content": "Is a steel frame heavy?"},
                {"role": "assistant", "content": "Heavier than aluminium."},
                {
                    "role": "user",
                    "content": "I bought a red bike yesterday.",
                    "has_answer": True,
                },
            ],
        ],
        "answer_session_ids": ["answer_move", "answer_bike"],
    }


# ---------------------------------------------------------------- the mapping


def test_answer_turns_name_the_round_each_marked_turn_sits_in() -> None:
    # session 1: the system message stands alone (round 0), then (1, 2)
    # is round 1 and (3, 4) round 2; session 2 ends on a lone user turn,
    # its own round 1.
    assert cov.answer_turns(_inst()) == [(1, 2, 3), (2, 1, 2)]


def test_answer_turns_are_empty_for_a_question_with_none_marked() -> None:
    inst = _inst()
    for session in inst["haystack_sessions"]:
        for turn in session:
            turn.pop("has_answer", None)
    assert cov.answer_turns(inst) == []


def test_round_positions_follow_the_store_order(tmp_path: Path) -> None:
    inst = _inst()
    service = cov.l1.service_for(tmp_path / "store")
    cov.aml_run.ingest_and_search(service, inst)
    positions = cov.round_positions(service, inst)
    assert sorted(positions.values()) == [
        (0, 0),
        (1, 0),
        (1, 1),
        (1, 2),
        (2, 0),
        (2, 1),
    ]
    root = service._user(cov.user_id(inst)).root
    bodies = {m.id: m.body for m in Store(root).load_all()}
    assert set(bodies) == set(positions)
    for mid, (k, j) in positions.items():
        assert bodies[mid].strip().endswith(cov.round_text(inst, k, j))


def test_round_text_is_the_rounds_messages_as_the_service_writes_them() -> None:
    assert cov.round_text(_inst(), 1, 2) == (
        "user: I just moved to Lisbon last week.\nassistant: Welcome to Lisbon!"
    )
    assert cov.round_text(_inst(), 1, 0) == "system: You are a helpful assistant."


def test_round_positions_refuse_a_store_that_does_not_match_the_haystack(
    tmp_path: Path,
) -> None:
    inst = _inst()
    service = cov.l1.service_for(tmp_path / "store")
    cov.aml_run.ingest_and_search(service, inst)
    other = _inst()
    other["haystack_sessions"][2].append({"role": "assistant", "content": "Nice."})
    other["haystack_sessions"][2].append({"role": "user", "content": "Thanks."})
    with pytest.raises(SystemExit, match="rounds"):
        cov.round_positions(service, other)


# ---------------------------------------------------------------- coverage


def _hits(inst: dict[str, Any], rounds: list[tuple[int, int]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"m{k}{j}",
            "content": "[2023/05/21 (Sun) 12:00]\n" + cov.round_text(inst, k, j),
        }
        for k, j in rounds
    ]


def _positions(rounds: list[tuple[int, int]]) -> dict[str, tuple[int, int]]:
    return {f"m{k}{j}": (k, j) for k, j in rounds}


def test_a_question_is_covered_when_every_answer_turn_is_served_whole() -> None:
    inst = _inst()
    everything = [(0, 0), (1, 0), (1, 1), (1, 2), (2, 0), (2, 1)]
    got = cov.coverage(inst, _hits(inst, [(1, 2), (2, 1)]), _positions(everything))
    assert got == {"answer_turns": 2, "missing": [], "covered": True}


def test_one_missing_answer_round_leaves_the_question_uncovered() -> None:
    inst = _inst()
    everything = [(0, 0), (1, 0), (1, 1), (1, 2), (2, 0), (2, 1)]
    got = cov.coverage(inst, _hits(inst, [(1, 2), (2, 0)]), _positions(everything))
    assert got["covered"] is False
    assert got["missing"] == [(2, 1, 2)]


def test_a_trimmed_answer_counts_as_missing() -> None:
    inst = _inst()
    everything = [(1, 2), (2, 1)]
    hits = _hits(inst, everything)
    hits[0]["content"] = hits[0]["content"].replace(
        "I just moved to Lisbon last week.", "[...]"
    )
    got = cov.coverage(inst, hits, _positions(everything))
    assert got["missing"] == [(1, 2, 3)]
    assert got["covered"] is False


def test_a_question_without_answer_turns_is_never_covered() -> None:
    inst = _inst()
    for session in inst["haystack_sessions"]:
        for turn in session:
            turn.pop("has_answer", None)
    assert cov.coverage(inst, [], {}) == {
        "answer_turns": 0,
        "missing": [],
        "covered": False,
    }


def test_classify_names_a_budget_cut_an_unscored_round_and_an_absent_session() -> None:
    ranked = {(1, 0), (1, 2)}
    missing = [(1, 2, 3), (1, 1, 2), (2, 1, 2)]
    assert cov.classify(missing, ranked) == ["a", "b", "c"]


# ---------------------------------------------------------------- the arms


def test_the_grid_is_the_declared_24_arms_with_c0_among_them() -> None:
    names = [a.name for a in cov.ARMS]
    assert len(cov.ARMS) == 24
    assert len(set(names)) == 24
    assert {a.order for a in cov.ARMS} == {"rank", "session", "chronological"}
    assert {a.top_k for a in cov.ARMS} == {100, 200}
    assert {a.fill for a in cov.ARMS} == {"none", "neighbors"}
    assert {a.trim for a in cov.ARMS} == {"none", "tail"}
    assert cov.C0 in cov.ARMS
    assert (cov.C0.order, cov.C0.top_k, cov.C0.fill, cov.C0.trim) == (
        "rank",
        100,
        "none",
        "none",
    )


def test_the_curve_budgets_are_the_declared_seven() -> None:
    assert cov.CURVE_BUDGETS == (
        45_000,
        90_000,
        135_000,
        180_000,
        270_000,
        360_000,
        540_000,
    )
    assert cov.BUDGET == 90_000


def test_configure_sets_the_serving_options_on_a_c0_service(tmp_path: Path) -> None:
    service = cov.l1.service_for(tmp_path / "store")
    arm = cov.Arm(order="session", top_k=200, fill="neighbors", trim="tail")
    cov.configure(service, arm, 45_000)
    assert (
        service.order,
        service.serve,
        service.fill,
        service.trim,
        service.budget,
    ) == (
        "session",
        200,
        "neighbors",
        "tail",
        45_000,
    )
    cov.configure(service, cov.C0, cov.BUDGET)
    assert (
        service.order,
        service.serve,
        service.fill,
        service.trim,
        service.budget,
    ) == (
        "rank",
        100,
        "none",
        "none",
        90_000,
    )


def test_an_arm_with_an_unknown_option_is_refused() -> None:
    with pytest.raises(ValueError):
        cov.Arm(order="random", top_k=100, fill="none", trim="none")
    with pytest.raises(ValueError):
        cov.Arm(order="rank", top_k=100, fill="everything", trim="none")
    with pytest.raises(ValueError):
        cov.Arm(order="rank", top_k=100, fill="none", trim="all")


def test_the_best_arm_has_the_most_covered_and_ties_go_to_fewer_characters() -> None:
    stats = {
        "a": {"covered": 130, "mean_chars": 89_000.0},
        "b": {"covered": 134, "mean_chars": 89_900.0},
        "c": {"covered": 134, "mean_chars": 88_000.0},
    }
    assert cov.best_arm(stats) == "c"


# ---------------------------------------------------------------- the run


def test_dev_instances_are_the_dev_split_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cov.aml_run, "DEV_N", 10)
    corpus = []
    for n in range(40):
        inst = _inst()
        inst["question_id"] = f"q{n}"
        inst["question_type"] = ("multi-session", "temporal-reasoning")[n % 2]
        corpus.append(inst)
    dev_ids, _ = cov.aml_run.split(corpus)
    got = cov.dev_instances(corpus)
    assert [i["question_id"] for i in got] == [
        i["question_id"] for i in corpus if i["question_id"] in set(dev_ids)
    ]


def test_run_refuses_without_a_keys_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BETTERMEMORY_KEYS_DIR", raising=False)
    args = SimpleNamespace(
        store=str(tmp_path / "store"),
        out=str(tmp_path / "out.json"),
        l1_prompts=str(tmp_path / "prompts"),
        l1_artifact=str(tmp_path / "l1.json"),
        workers=1,
    )
    with pytest.raises(SystemExit, match="BETTERMEMORY_KEYS_DIR"):
        cov.cmd_run(args)


def test_measure_maps_every_hit_and_the_whole_haystack_covers_the_question(
    tmp_path: Path,
) -> None:
    inst = _inst()
    root = tmp_path / "store"
    cov.aml_run.ingest_and_search(cov.l1.service_for(root), inst)
    row = cov.measure(root, inst)
    assert row["mapping_errors"] == []
    assert row["rounds"] == 6
    assert row["answer_turns"] == 2
    assert row["whole_haystack"]["covered"] is True
    assert set(row["arms"]) == {a.name for a in cov.ARMS}
    assert set(row["engine_curve"]) == {str(b) for b in cov.CURVE_BUDGETS}
    c0 = row["arms"][cov.C0.name]
    assert len(row["c0_missing_classes"]) == len(c0["missing"])


def test_summaries_count_recoveries_and_regressions_against_c0() -> None:
    def row(q: str, covered: bool, chars: int, turns: int = 1) -> dict[str, Any]:
        return {
            "question_id": q,
            "answer_turns": turns,
            "x": {"covered": covered, "chars": chars},
        }

    rows = [
        row("a", True, 10),
        row("b", False, 20),
        row("c", True, 30),
        row("d", False, 5, 0),
    ]
    got = cov.summarize_cells(
        rows, lambda r: r["x"], c0_covered={"a", "b"}, l1_dev_misses={"c"}
    )
    assert (got["n"], got["covered"], got["rate"]) == (3, 2, 0.6667)
    assert got["recovered_vs_c0"] == ["c"]
    assert got["regressed_vs_c0"] == ["b"]
    assert got["l1_dev_misses_covered"] == ["c"]
    assert got["mean_chars"] == 20.0
