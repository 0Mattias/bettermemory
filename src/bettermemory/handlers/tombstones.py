"""memory_list_tombstones MCP tool — list removed memories for curation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import validate_scope
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_LIST_TOMBSTONES = (
    "List removed (tombstoned) memories. One-line summaries plus "
    "removal metadata (`removed`, `removed_reason`, "
    "`removed_session`) — body stripped, like memory_list. Use "
    'for curation passes ("what did I clear out last month?") or '
    "to investigate when the user asks 'I think I had a memory "
    "about X — what happened?'. Pass `scopes` to filter, like "
    "memory_list. Tombstones are sorted by `removed` descending — "
    "most-recently-removed first."
)


async def memory_list_tombstones(
    deps: "ToolHandlers",
    scopes: list[str] | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    excluded = set(state.disabled_scopes)
    out: list[dict[str, Any]] = []
    for summary in deps.store.list_tombstones(scopes=scopes):
        if excluded and (set(summary.scopes) & excluded):
            continue
        out.append(deps.responses.tombstone_summary_to_dict(summary))
    deps.recorder.record(
        "list_tombstones",
        scopes_filter=scopes,
        count=len(out),
        returned=[s["id"] for s in out],
    )
    return out


__all__ = ["DESC_MEMORY_LIST_TOMBSTONES", "memory_list_tombstones"]
