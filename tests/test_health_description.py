"""The health action's documentation names the fields the report carries.

ENG-171 H1. `health.curation_counts` and its coverage twin fed
`memory_scope_overview`'s `curation_pending`, and lost their last caller
when 9.0 removed that tool. The standalone `memory_health` description
outlived it too: never served (the tool is `memory_admin(action="health")`,
described by `DESC_MEMORY_ADMIN`'s `health` clause), it still sent the
reader to `curation_pending.dead`, a field no response carries. Both are
gone. The fields are listed in docs/api.md, and this file holds that
list to the report the action returns, in both directions, and keeps the
health and eval family from pointing at the removed surfaces again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.events import Recorder
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool as _mcp_call

_ROOT = Path(__file__).resolve().parents[1]
_API = _ROOT / "docs" / "api.md"
_HEADING = '### `memory_admin(action="health")`'
_LEAD_IN = "the tool does not): "

# The surfaces 9.0 removed that the health family used to feed or cite.
_REMOVED = (
    "curation_counts",
    "memory_scope_overview",
    "curation_pending",
    "DESC_MEMORY_HEALTH",
    "DESC_MEMORY_ACKNOWLEDGE_MISS",
    "report_for_directory",
)


@pytest.fixture
def server(memory_dir: Path, store: Store) -> Any:
    cfg = Config(storage=StorageConfig(directory=str(memory_dir)))
    state = SessionState()
    rec = Recorder(store=store, session_id=state.session_id)
    return build_server(config=cfg, store=store, state=state, recorder=rec)


def _documented_fields() -> list[str]:
    """The backticked names at the top level of the field list: the ones
    outside any parenthesis, which carry the nested detail."""
    text = _API.read_text(encoding="utf-8")
    start = text.index(_HEADING) + len(_HEADING)
    paragraph = text[start:].strip().split("\n\n", 1)[0]
    _, found, listing = paragraph.partition(_LEAD_IN)
    assert found, "the field list's lead-in changed; update _LEAD_IN"
    fields: list[str] = []
    depth = 0
    i = 0
    while i < len(listing):
        ch = listing[i]
        if ch == "`":
            end = listing.index("`", i + 1)
            if depth == 0:
                fields.append(listing[i + 1 : end])
            i = end
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return fields


def test_the_parser_reads_the_field_list() -> None:
    fields = _documented_fields()
    assert fields[0] == "generated_at"
    assert "applied=N (auto=X exp=Y)" not in fields
    assert "event_id" not in fields
    assert len(fields) == len(set(fields)), "a field is listed twice"


async def test_the_doc_names_exactly_the_fields_the_health_action_returns(
    server: Any,
) -> None:
    report = await _mcp_call(server, "memory_admin", {"action": "health"})
    documented = set(_documented_fields())
    returned = set(report)
    assert documented - returned == set(), "the doc names fields the report lacks"
    assert returned - documented == set(), "the report carries fields the doc omits"


def test_the_health_family_names_no_removed_surface() -> None:
    src = _ROOT / "src" / "bettermemory"
    files = [
        *sorted(src.glob("health*.py")),
        *sorted(src.glob("eval*.py")),
        src / "handlers" / "health.py",
        src / "cli" / "health_cmd.py",
        src / "cli" / "eval.py",
    ]
    hits = [
        f"{path.relative_to(_ROOT)}:{lineno}: {name}"
        for path in files
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for name in _REMOVED
        if name in line
    ]
    assert hits == []


def test_the_health_handler_keeps_no_description_of_its_own() -> None:
    from bettermemory.handlers import health as handler

    assert not hasattr(handler, "DESC_MEMORY_HEALTH")
    assert handler.__all__ == ["memory_health"]


def test_the_acknowledge_miss_handler_keeps_no_description_of_its_own() -> None:
    """`memory_admin` serves both acknowledgement forms under
    `DESC_MEMORY_ADMIN`'s `acknowledge_miss` clause; the standalone
    description was never served and still named 8.x's `consolidate`."""
    from bettermemory.handlers import acknowledge_miss as handler

    assert not hasattr(handler, "DESC_MEMORY_ACKNOWLEDGE_MISS")
    assert "DESC_MEMORY_ACKNOWLEDGE_MISS" not in handler.__all__
