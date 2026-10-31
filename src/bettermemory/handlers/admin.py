"""The `memory_admin` MCP tool: curation, one action per call.

Each action is a former tool of its own, folded here so the served
surface stays at nine tools without losing the work: restore, tombstones,
health, rename_scope, conflicts, acknowledge_miss, disable_scope and
enable_scope; relations is the capture's closing call; answer_question,
join_project and unjoin_project are the agent's membership declarations
(`questions.py`, design 8.8); answer_standing answers the standing
question for the records that wait for it (`standing.py`, unit RS-F7
part 1). The bodies keep their modules; this module
is the dispatch the served tool is built from, and every action returns
the keys its former tool returned (`tombstones` wraps its list under one
key, since a dict is the tool's shape).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from ._shared import Context
from .acknowledge_miss import acknowledge_misses_before, memory_acknowledge_miss
from .capture_relations import capture_relations
from .conflicts import memory_conflicts
from .health import memory_health
from .questions import answer_question, join_project, unjoin_project
from .rename_scope import memory_rename_scope
from .restore import memory_restore
from .scope_toggle import memory_scope_disable, memory_scope_enable
from .standing import answer_standing
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
    "relations",
    "answer_question",
    "join_project",
    "unjoin_project",
    "answer_standing",
]

DESC_MEMORY_ADMIN = (
    "restore: tombstone `id` back. "
    "tombstones: removed memories. health: the health report. "
    "rename_scope: `old_scope` to `new_scope`. conflicts: pending pairs; "
    "scan=True detects; `id`, `verdict` (contradiction|compatible), `note` "
    "resolve. acknowledge_miss: search_miss `id` with `reason`, or all "
    "before ISO `before`. disable_scope, enable_scope: hide or unhide "
    "`scope` this session. relations: a capture's closing call. "
    "answer_question: a membership question (`id` Qnnn, else the first "
    "here): `answer` separate on your judgement, or join on the user's "
    "words in `said`; `target` picks among several. join_project: merge "
    "project `id` into this checkout's, on the user's words in `said`. "
    "unjoin_project: undo it. answer_standing: `standing` {id: 0 to 1}."
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
    relations: dict[str, Any] | None = None,
    answer: str | None = None,
    target: str | None = None,
    said: str | None = None,
    standing: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if action == "answer_standing":
        return await answer_standing(deps, standing, said=said, ctx=ctx)
    if action == "answer_question":
        return await answer_question(
            deps, id=id, answer=answer, target=target, said=said, ctx=ctx
        )
    if action == "join_project":
        return await join_project(deps, id=id, said=said, ctx=ctx)
    if action == "unjoin_project":
        return await unjoin_project(deps, id=id, ctx=ctx)
    if action == "restore":
        return await memory_restore(deps, _need(id, "id", action), said=said, ctx=ctx)
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
    if action == "relations":
        return await capture_relations(deps, relations, ctx=ctx)
    raise ValueError(f"unknown memory_admin action {action!r}")


__all__ = ["AdminAction", "DESC_MEMORY_ADMIN", "memory_admin"]
