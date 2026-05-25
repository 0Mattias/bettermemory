"""memory_list MCP tool — handler implementation + DESC.

Two response shapes: by default returns lightweight summaries (cheap
triage); `with_bodies=True` inlines full bodies in one call. The body
load is gated behind a flag because it pulls every active memory in
scope into the caller's context — the failure mode this whole project
exists to avoid.

Module is named ``list_active`` because ``list`` shadows the builtin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import utcnow, validate_scope
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_LIST = (
    "List active memories. By default returns one-line summaries "
    "(IDs, scopes, summary, no body) — cheap triage. "
    "Pass `with_bodies=True` to inline full bodies in one call; "
    "useful for small stores where N round trips of "
    "`list -> show -> show` would be wasteful. Don't reach for "
    "`with_bodies` casually — it pulls every memory in scope into "
    "your context, which is the failure mode this project exists "
    "to avoid. Filter by `scopes` if you only care about a subset."
)


async def memory_list(
    deps: "ToolHandlers",
    scopes: list[str] | None = None,
    with_bodies: bool = False,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    if scopes:
        scopes = [validate_scope(s) for s in scopes]
    # Apply session-disabled scopes to listing too — consistency.
    excluded = set(state.disabled_scopes)
    # Single `now` for the whole listing — same reasoning as in
    # memory_search: consistent verification verdict across rows.
    now = utcnow()

    if with_bodies:
        out: list[dict[str, Any]] = []
        for memory in deps.store.load_all():
            memory_scopes = set(memory.scopes)
            if excluded and (memory_scopes & excluded):
                continue
            if scopes and not (memory_scopes & set(scopes)):
                continue
            out.append(deps.responses.memory_to_dict(memory, now=now))
        deps.recorder.record(
            "list",
            scopes_filter=scopes,
            with_bodies=True,
            count=len(out),
            returned=[m["id"] for m in out],
        )
        return out

    out_summary: list[dict[str, Any]] = []
    for summary in deps.store.list_summaries(scopes=scopes):
        if excluded and (set(summary.scopes) & excluded):
            continue
        out_summary.append(deps.responses.summary_to_dict(summary, now=now))
    deps.recorder.record(
        "list",
        scopes_filter=scopes,
        with_bodies=False,
        count=len(out_summary),
        returned=[s["id"] for s in out_summary],
    )
    return out_summary


__all__ = ["DESC_MEMORY_LIST", "memory_list"]
