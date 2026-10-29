"""The bounds of the location and membership families state their evidence
(ENG-160).

Each bound the review listed (H4) names, in the comment above it, the
measurement that sets it or the source it follows; the relations those
comments assert are pinned here, so a bound changed alone fails. A
measurement is a number with its unit; the issue id alone, or a design
section, is not evidence (review M H4).
"""

from __future__ import annotations

import inspect
import re
from types import ModuleType

import pytest

from bettermemory import githead, location, location_git, membership_records

#: (module, bound) for every bound the review named.
BOUNDS = [
    (location_git, "REF_TIPS_MAX"),
    (location_git, "MANIFEST_LIMIT"),
    (location_git, "CACHE_CAP"),
    (location_git, "WALK_LIMIT"),
    (location_git, "BLOB_LOOK"),
    (location_git, "EXCHANGE_ROUNDS"),
    (location_git, "_GIT_TIMEOUT_SECONDS"),
    (location_git, "_CONFIG_LIMIT"),
    (location_git, "_GITFILE_LIMIT"),
    (location_git, "_INCLUDE_DEPTH"),
    (location_git, "_INCLUDE_FILES"),
    (location, "STANDS_CAP"),
    (membership_records, "WALK_LIMIT"),
    (membership_records, "CONTENT_MATCH"),
    (membership_records, "CONTENT_MAX"),
    (membership_records, "RETIRE_AFTER"),
    (membership_records, "VIEW_REFRESH"),
    (membership_records, "EXPORT_WITNESSES"),
]

#: A measurement: a number with its unit (a time, a size, or a count of
#: what was read).
_MEASUREMENT = (
    r"\d[\d,.]*\s?(?:µs|ms|s|B|KB|KiB|MB|MiB|bytes|files|commits|entries"
    r"|exchanges|reads|ids)\b"
)
#: A named source that fixes the value: git's own bound (its function or
#: file), another bound it is measured at, a decision, a design scenario.
_SOURCE = (
    r"read_gitfile_gently|config\.c|location\.WALK_LIMIT"
    r"|decision [A-Z]\d+|design scenario I\d+[a-z]?"
)
#: What a stated evidence cites (review M H4): a measurement or a named
#: source. The issue id alone (ENG-160) or a design section names what the
#: bound is for, not why this value.
_CITES = re.compile(f"{_MEASUREMENT}|{_SOURCE}")


def _comment_above(module: ModuleType, name: str) -> str:
    lines = inspect.getsource(module).splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith(f"{name} = "))
    block: list[str] = []
    for line in reversed(lines[:at]):
        if not line.startswith("#"):
            break
        block.append(line)
    return "\n".join(reversed(block))


@pytest.mark.parametrize(
    ("module", "name"), BOUNDS, ids=[f"{m.__name__}.{n}" for m, n in BOUNDS]
)
def test_each_bound_states_its_evidence(module: ModuleType, name: str) -> None:
    # The comment as one text: a number and its unit may sit on two lines.
    comment = re.sub(r"\n#:?\s*", " ", _comment_above(module, name))
    assert _CITES.search(comment), f"{module.__name__}.{name} states no evidence"


@pytest.mark.parametrize(
    ("text", "cites"),
    [
        ("the walk's bound (ENG-160).", False),
        ("C only asks (design 2.2).", False),
        ("a sighting undoes it (D15): not load-bearing (ENG-160).", False),
        ("512 commits in 55 ms", True),
        ("about 3 KB as JSON", True),
        ("git refuses a .git file over 1 MiB (read_gitfile_gently)", True),
        ("A checkout unseen this long leaves health (decision D15)", True),
    ],
)
def test_only_a_measurement_or_a_named_source_is_evidence(
    text: str, cites: bool
) -> None:
    assert bool(_CITES.search(text)) is cites


def test_the_relations_the_evidence_states_hold() -> None:
    """The client walks what the model walks; the per-location bounds
    scale from the capture cache's; a .git file is bounded where git
    bounds it, in both readers; an export's oldest half is among the
    witnesses every store keeps; a view is refreshed well inside the
    retirement it serves."""
    assert location_git.WALK_LIMIT == membership_records.WALK_LIMIT
    assert location.STANDS_CAP == location_git.CACHE_CAP * 4
    assert location_git._GITFILE_LIMIT == githead._GITFILE_LIMIT == 1 << 20
    assert location_git._INCLUDE_DEPTH == 10
    assert membership_records.EXPORT_WITNESSES // 2 <= membership_records.KEEP_OLDEST
    assert membership_records.RETIRE_AFTER == 180 * membership_records.VIEW_REFRESH
    assert 0 < membership_records.CONTENT_MATCH < 1
