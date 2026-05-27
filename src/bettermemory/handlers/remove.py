"""memory_remove MCP tool — tombstone a memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..store import MemoryNotFoundError, TombstonedError
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_REMOVE = (
    "Tombstone a memory. The file is moved to .tombstones/ with a "
    "removal reason and the originating session id — never hard-"
    "deleted. Use when a stored fact is wrong or no longer relevant. "
    "Tombstones remain searchable via memory_list_tombstones and "
    "are surfaced as `removed_matches` on memory_write when a new "
    "body looks similar to a previously-removed fact, so the "
    "lesson encoded in the removal reason isn't lost. Use "
    "memory_restore(id) to undo an accidental removal."
)


async def memory_remove(
    deps: "ToolHandlers", id: str, reason: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    if not reason or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    try:
        tombstone_path = deps.store.tombstone(id, reason, session_id=state.session_id)
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    except OSError as exc:
        # W1 closes the most common race path (FileNotFoundError under a
        # concurrent tombstone) by converting it to TombstonedError inside
        # `Store.tombstone`. Bare OSError can still surface from genuine
        # disk-level failures during the tombstone write or unlink (EIO
        # mid-write, ENOSPC during the rename, EACCES on the unlink, …).
        # Surface as ValueError so the MCP tool boundary returns a clean
        # structured error rather than letting the bare OSError leak.
        raise ValueError(f"failed to tombstone memory {id}: {exc}") from exc
    deps.recorder.record("remove", id=id, reason=reason)
    return {
        "removed": id,
        "tombstone_path": str(tombstone_path),
    }


__all__ = ["DESC_MEMORY_REMOVE", "memory_remove"]
