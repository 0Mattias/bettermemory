"""The default health answer and the section answer (`health_summary`).

The summary names what needs attention within 3,000 characters on the
worst report the closed sets allow: every recommendation kind at once,
the unread one naming every cause's remedy, seven-digit counts, every
section non-empty. A section comes back in full under its own key.
"""

from __future__ import annotations

from typing import Any

import pydantic_core
import pytest

from bettermemory.health import report_for_store
from bettermemory.health_model import RECOMMENDATION_KINDS
from bettermemory.health_recommend import _UNREAD_REMEDIES
from bettermemory.health_summary import (
    HEADLINE_KEYS,
    SUMMARY_IDS,
    health_section,
    health_summary,
    section_names,
)
from bettermemory.store import Store

_ID = "01M7F5ENPZQAP87TC1YC06XAW2"
#: Longer than every fixed recommendation action.
_FIXED_ACTION = "x" * 130
_UNREAD_ACTION = (
    "Spot-check a claim of each, then memory_verify(id, verified_paths=[...]) "
    "stamps HEAD; " + "; ".join(_UNREAD_REMEDIES.values()) + "."
)


def _worst_report(store: Store) -> dict[str, Any]:
    report = report_for_store(store).to_dict()
    report["membership"] = {"open_proposals": []}
    for name in section_names(report):
        report[name] = [{"row": "y" * 100}] * 1_000
    for key in HEADLINE_KEYS:
        if isinstance(report[key], int):
            report[key] = 9_999_999
    report["recommendations"] = [
        {
            "kind": kind,
            "summary": "s" * 300,
            "action": _UNREAD_ACTION if kind == "spot_check_unread" else _FIXED_ACTION,
            "count": 9_999_999,
            "memory_ids": [_ID] * 10,
            "scope": ", ".join(["projects:a-long-project-name"] * 10)
            if kind == "fix_typo_scopes"
            else None,
        }
        for kind in RECOMMENDATION_KINDS
    ]
    return report


def _served(answer: dict[str, Any]) -> str:
    """The answer as the tool serves it: indented JSON
    (`mcp.server.mcpserver.utilities.func_metadata`)."""
    return pydantic_core.to_json(answer, fallback=str, indent=2).decode()


def test_the_worst_summary_fits_3000_characters(store: Store) -> None:
    report = _worst_report(store)
    summary = health_summary(report)
    assert len(_served(summary)) <= 3_000
    kinds = [line.split(" ", 1)[0] for line in summary["attention"]]
    assert kinds == list(RECOMMENDATION_KINDS)
    assert all(line.count(_ID) <= SUMMARY_IDS for line in summary["attention"])
    assert set(summary["section_chars"]) == set(section_names(report))


def test_the_summary_names_attention_and_leaves_rows_to_sections(store: Store) -> None:
    report = report_for_store(store).to_dict()
    report["recommendations"] = [
        {
            "kind": "resolve_contradicted",
            "summary": "one contradiction",
            "action": "memory_update it",
            "count": 4,
            "memory_ids": ["a", "b", "c", "d"],
            "scope": None,
        }
    ]
    summary = health_summary(report)
    assert summary["attention"] == [
        "resolve_contradicted 4: memory_update it [a, b, c]"
    ]
    assert "dead_weight" not in summary["section_chars"]  # empty on a new store
    assert summary["section_chars"]["recommendations"] > 0


def test_a_section_comes_back_whole_under_its_key(store: Store) -> None:
    report = report_for_store(store).to_dict()
    assert health_section(report, "verification_debt") == {
        "verification_debt": report["verification_debt"]
    }
    with pytest.raises(ValueError, match="no section 'generated_at'"):
        health_section(report, "generated_at")


def test_one_call_reads_several_sections_from_one_report(store: Store) -> None:
    report = report_for_store(store).to_dict()
    both = health_section(report, "verification_debt, recommendations")
    assert both == {
        "verification_debt": report["verification_debt"],
        "recommendations": report["recommendations"],
    }
    with pytest.raises(ValueError, match="no section 'nope'"):
        health_section(report, "verification_debt,nope")
    with pytest.raises(ValueError, match="the sections are"):
        health_section(report, " , ")


def test_a_zero_or_false_section_is_listed(store: Store) -> None:
    report = report_for_store(store).to_dict()
    report["orphan_use_events"] = 0
    report["flag"] = False
    sizes = health_summary(report)["section_chars"]
    assert sizes["orphan_use_events"] == 1 and sizes["flag"] == 5
