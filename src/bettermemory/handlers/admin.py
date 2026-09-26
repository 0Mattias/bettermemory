"""The `memory_admin` MCP tool: curation, one action per call.

Each action is a former tool of its own, folded here so the served
surface stays at nine tools without losing the work: restore, tombstones,
health, rename_scope, conflicts, acknowledge_miss, disable_scope and
enable_scope. The bodies keep their modules; this module is the dispatch
the served tool is built from, and every action returns the keys its
former tool returned (`tombstones` wraps its list under one key, since a
dict is the tool's shape).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ._shared import Context
from .acknowledge_miss import acknowledge_misses_before, memory_acknowledge_miss
from .conflicts import memory_conflicts
from .health import memory_health
from .rename_scope import memory_rename_scope
from .restore import memory_restore
from .scope_toggle import memory_scope_disable, memory_scope_enable
from .tombstones import memory_list_tombstones

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


AdminAction = Literal[
    "restore",
    "tombstones",
    "health",
    "rename_scope",
    "conflicts",
    "acknowledge_miss",
    "disable_scope",
    "enable_scope",
]

DESC_MEMORY_ADMIN = (
    "One curation action per call. restore: tombstone `id` back. "
    "tombstones: the removed memories. health: the health report. "
    "rename_scope: `old_scope` to `new_scope`. conflicts: pending "
    "contradiction pairs; scan=True detects, `id` with `verdict` (a, b, "
    "both, neither) and `note` resolves. acknowledge_miss: search_miss `id` "
    "with `reason`, or all misses before ISO `before`. disable_scope, "
    "enable_scope: hide or unhide `scope` this session."
)


def _need(value: str | None, name: str, action: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"action={action!r} needs `{name}`")
    return value


async def memory_admin(
    deps: ToolHandlers,
    action: AdminAction,
    id: str | None = None,
    scope: str | None = None,
    scopes: list[str] | None = None,
    old_scope: str | None = None,
    new_scope: str | None = None,
    scan: bool = False,
    verdict: str | None = None,
    note: str | None = None,
    reason: str | None = None,
    before: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if action == "restore":
        return await memory_restore(deps, _need(id, "id", action), ctx=ctx)
    if action == "tombstones":
        rows = await memory_list_tombstones(deps, scopes=scopes, ctx=ctx)
        return {"tombstones": rows}
    if action == "health":
        return await memory_health(deps, ctx=ctx)
    if action == "rename_scope":
        return await memory_rename_scope(
            deps,
            _need(old_scope, "old_scope", action),
            _need(new_scope, "new_scope", action),
            ctx=ctx,
        )
    if action == "conflicts":
        return await memory_conflicts(
            deps, scan=scan, resolve=id, verdict=verdict, note=note, ctx=ctx
        )
    if action == "acknowledge_miss":
        if before is not None:
            return await acknowledge_misses_before(deps, before, reason=reason, ctx=ctx)
        return await memory_acknowledge_miss(
            deps, _need(id, "id", action), _need(reason, "reason", action), ctx=ctx
        )
    if action == "disable_scope":
        return await memory_scope_disable(deps, _need(scope, "scope", action), ctx=ctx)
    if action == "enable_scope":
        return await memory_scope_enable(deps, _need(scope, "scope", action), ctx=ctx)
    raise ValueError(f"unknown memory_admin action {action!r}")


__all__ = ["AdminAction", "DESC_MEMORY_ADMIN", "memory_admin"]
