"""Unit WR's two measurement programs, on synthetic inputs."""

from __future__ import annotations

from wr_floor import best_floor, floor_table
from wr_selection import select


def test_floor_table_counts_recall_and_one_call_share() -> None:
    pairs = [
        {"write": 0, "label": "update", "overlap": 5},
        {"write": 0, "label": "unrelated", "overlap": 2},
        {"write": 1, "label": "unrelated", "overlap": 1},
    ]
    rows = floor_table(pairs, 2, "overlap", 6)
    assert rows[1] == {"F": 2, "unrelated": 0.5, "update": 1.0, "one_call": 0.5}
    assert best_floor(rows)["F"] == 5


def test_select_by_scope_and_by_token() -> None:
    records = {
        "a": ("No migration runs against the billing database.", {"x"}),
        "b": ("Release notes are written in the past tense.", {"y"}),
    }
    sel = select("Apply the billing migration now. Then rebuild.", {"y"}, records)
    assert sel["token"] == {"a"}
    assert sel["scope"] == {"b"}
    assert sel["union"] == {"a", "b"}
