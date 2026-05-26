"""memory_scope_disable / memory_scope_enable — symmetric pair.

The two handlers are tiny and inverse, so they share a module rather
than each getting their own file. ``memory_scope_disable`` excludes a
scope from search/list for the rest of the session;
``memory_scope_enable`` restores it. Session state is process-local —
both reset when the server restarts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import validate_scope
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_SCOPE_DISABLE = (
    "Disable a scope for the rest of this session. Subsequent "
    "memory_search and memory_list calls will exclude memories "
    "tagged with this scope. Useful when the user says 'this is "
    "unrelated to project X'. Resets when the server restarts."
)


DESC_MEMORY_SCOPE_ENABLE = "Re-enable a previously disabled scope for this session."


async def memory_scope_disable(
    deps: "ToolHandlers", scope: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    clean = validate_scope(scope)
    state.disable(clean)
    deps.recorder.record("scope_disable", scope=clean)
    return {"disabled_scopes": sorted(state.disabled_scopes)}


async def memory_scope_enable(
    deps: "ToolHandlers", scope: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    clean = validate_scope(scope)
    state.enable(clean)
    deps.recorder.record("scope_enable", scope=clean)
    return {"disabled_scopes": sorted(state.disabled_scopes)}


__all__ = [
    "DESC_MEMORY_SCOPE_DISABLE",
    "DESC_MEMORY_SCOPE_ENABLE",
    "memory_scope_disable",
    "memory_scope_enable",
]
