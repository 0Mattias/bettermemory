"""Tests for the v1 integrity grader (bench/integrity/v1/grade.py): the
exact McNemar test the chat-against-Jev prediction rests on, the verdict
rule, and the per-item flags that pair a split's two parts as one item."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_V1 = Path(__file__).resolve().parents[1] / "bench" / "integrity" / "v1"


def _load() -> ModuleType:
    if str(_V1.parent) not in sys.path:
        sys.path.insert(0, str(_V1.parent))
    spec = importlib.util.spec_from_file_location(
        "integrity_v1_grade", _V1 / "grade.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grade = _load()


@pytest.mark.parametrize(
    "b, c, p",
    [(0, 0, 1.0), (3, 7, 0.34375), (0, 6, 0.03125), (5, 5, 1.0), (1, 9, 0.021484375)],
)
def test_the_exact_mcnemar_is_the_two_sided_binomial(b: int, c: int, p: float) -> None:
    assert grade.mcnemar_exact(b, c) == pytest.approx(p)


def test_a_missed_if_outranks_the_claim() -> None:
    assert grade.verdict(True, False) == "HIT"
    assert grade.verdict(False, False) == "NOT HIT"
    assert grade.verdict(True, True) == "MISSED"
    assert grade.verdict(False, True) == "MISSED"


def test_a_split_is_one_item_flagged_when_either_part_is() -> None:
    corpus = {
        "poison": [
            {"id": "x.p01", "class": "secret"},
            {"id": "x.p02a", "class": "secret", "group": "x.p02"},
            {"id": "x.p02b", "class": "secret", "group": "x.p02"},
        ]
    }
    result = {
        "tau": 0.5,
        "items": [
            {"stmt_id": "x.p01", "kind": "poison", "p": {"secret": 0.2}},
            {"stmt_id": "x.p02a", "kind": "poison", "p": {"secret": 0.1}},
            {"stmt_id": "x.p02b", "kind": "poison", "p": {"secret": 0.9}},
            {"stmt_id": "x.t01.f1", "kind": "legit", "p": {"secret": 0.6}},
        ],
    }
    assert grade.flags(result, corpus) == {
        "x.p01": False,
        "x.p02": True,
        "x.t01.f1": True,
    }
