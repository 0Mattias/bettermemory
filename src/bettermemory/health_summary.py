"""The two answers `memory_admin(action="health")` gives from one report.

`health_summary` is the default: the store's headline counts, each
recommendation as one line of what needs attention (its kind, count,
action and the first `SUMMARY_IDS` ids or scopes), and every other
non-empty section by name with its size in characters. `health_section` is the answer to a
call naming one section: that section in full, under its own key. Both
read the dict `HealthReport.to_dict` returns, with the handler's
membership section added; neither renders the other.

The summary is bounded by construction: the recommendation kinds are a
closed set (`health_model.RECOMMENDATION_KINDS`), each action is a fixed
sentence, and the section names are the report's keys.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: The ids (or scopes) each attention row names; the section has the rest.
SUMMARY_IDS = 3

#: The report's scalar headline, carried in the summary as it is.
HEADLINE_KEYS: tuple[str, ...] = (
    "generated_at",
    "window_days",
    "total_active_memories",
    "total_events",
    "distinct_sessions",
)


def _empty(value: Any) -> bool:
    """None or an empty list or mapping; a zero or a False is a value."""
    return value is None or (isinstance(value, (list, dict)) and not value)


def _attention(recommendation: Mapping[str, Any]) -> str:
    """``<kind> <count>: <action> [<ids or scopes>]``, one line: the tool
    answer is indented JSON, where a string costs one line and an object
    one line per key."""
    named = list(recommendation.get("memory_ids") or [])[:SUMMARY_IDS]
    scope = recommendation.get("scope")
    if scope:
        named = str(scope).split(", ")[:SUMMARY_IDS]
    line = f"{recommendation['kind']} {recommendation['count']}: {recommendation['action']}"
    return f"{line} [{', '.join(named)}]" if named else line


def section_names(report: Mapping[str, Any]) -> list[str]:
    """Every name `health_section` answers for `report`."""
    return [name for name in report if name not in HEADLINE_KEYS]


def health_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """The default health answer for `report` (see the module docstring)."""
    sections = {
        name: len(json.dumps(report[name], separators=(",", ":"), default=str))
        for name in section_names(report)
        if not _empty(report[name])
    }
    return {
        **{key: report[key] for key in HEADLINE_KEYS if key in report},
        "attention": [_attention(r) for r in report.get("recommendations") or []],
        "section_chars": sections,
    }


def health_section(report: Mapping[str, Any], name: str) -> dict[str, Any]:
    """The sections `name` lists (one name, or several separated by
    commas) of `report` in full, each under its own key, from the one
    report: a caller that needs two sections reads both as one computation
    saw them. A ValueError naming the sections for a name the report has
    not."""
    names = section_names(report)
    asked = [part.strip() for part in name.split(",") if part.strip()]
    unknown = [part for part in asked if part not in names]
    if unknown or not asked:
        raise ValueError(
            f"health has no section {(unknown[0] if unknown else name)!r}; "
            f"the sections are {', '.join(names)}"
        )
    return {part: report[part] for part in asked}


__all__ = [
    "HEADLINE_KEYS",
    "SUMMARY_IDS",
    "health_section",
    "health_summary",
    "section_names",
]
