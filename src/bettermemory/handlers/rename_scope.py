"""memory_rename_scope MCP tool — bulk-rename a scope tag."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import validate_scope
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


async def memory_rename_scope(
    deps: ToolHandlers,
    old_scope: str,
    new_scope: str,
    include_tombstones: bool = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    clean_old = validate_scope(old_scope)
    clean_new = validate_scope(new_scope)
    if clean_old == clean_new:
        raise ValueError("old_scope and new_scope must differ")
    if deps.config.scopes.allowed and clean_new not in set(deps.config.scopes.allowed):
        raise ValueError(
            f"new_scope {clean_new!r} is not in the allowed list: "
            f"{sorted(deps.config.scopes.allowed)}"
        )
    try:
        result = deps.store.rename_scope(
            clean_old, clean_new, include_tombstones=include_tombstones
        )
    except OSError as exc:
        # Store.rename_scope reports a record it could not rewrite under
        # `failed` and carries on; a disk-level failure under the store's
        # write (ENOSPC, EIO, EACCES) still propagates out. Surface as
        # ValueError so the MCP tool boundary returns a clean structured
        # error rather than leaking the bare OSError — mirror of the
        # OSError arms in handlers/remove.py and handlers/restore.py. The
        # rename commits chunk by chunk (`Store._in_chunks`), so a failure
        # may leave it partially applied; a re-run renames the records
        # still carrying the old scope.
        raise ValueError(
            f"failed to rename scope {clean_old!r} -> {clean_new!r}: {exc} "
            "(rename may be partially applied; safe to re-run)"
        ) from exc
    # Item 6/6b: `failed` lists the {id, reason} records whose per-record
    # re-dump raised inside the rename loop and were skipped rather than
    # aborting the whole run. `Store.rename_scope` omits the key on a clean run,
    # so normalise with `.get`. Surface it (always, even when empty) so a
    # partial run reports which records did not rename instead of silently
    # claiming full success.
    failed = result.get("failed", [])
    deps.recorder.record(
        "rename_scope",
        old=clean_old,
        new=clean_new,
        include_tombstones=include_tombstones,
        active_count=len(result["active"]),
        tombstoned_count=len(result["tombstoned"]),
        failed_count=len(failed),
    )
    return {
        "old_scope": clean_old,
        "new_scope": clean_new,
        "active": result["active"],
        "tombstoned": result["tombstoned"],
        "failed": failed,
    }


__all__ = ["memory_rename_scope"]
