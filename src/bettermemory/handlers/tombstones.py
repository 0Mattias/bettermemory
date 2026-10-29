"""memory_list_tombstones MCP tool — list removed memories for curation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import validate_scope
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


async def memory_list_tombstones(
    deps: ToolHandlers,
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


__all__ = ["memory_list_tombstones"]
