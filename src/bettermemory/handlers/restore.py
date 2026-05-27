"""memory_restore MCP tool — bring a tombstoned memory back to active."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..store import MemoryNotFoundError, NotTombstonedError
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_RESTORE = (
    "Bring a tombstoned memory back to the active set. Strips the "
    "removal frontmatter, moves the file out of .tombstones/, and "
    "preserves the original `created`, `updated`, and "
    "`last_verified_at` timestamps — the body didn't change while "
    "it was tombstoned, so the recency boost stays honest. Raises "
    "if the id is active (use memory_update for edits) or unknown. "
    "The original removal reason and session live on in the event "
    "log even after restore."
)


async def memory_restore(
    deps: "ToolHandlers", id: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    try:
        memory = deps.store.restore(id)
    except NotTombstonedError as exc:
        # W7: also fires when a concurrent restore won the race and the
        # id is now active under the lock — message carries the
        # "(raced with concurrent restore)" hint so the caller can
        # distinguish from the plain "id is active" case if they want.
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        # W7: also fires when a concurrent restore / prune removed the
        # tombstone between the find walk and the under-lock recheck;
        # the message carries the "(raced with concurrent restore or
        # prune)" hint so the caller can recognise the race-loss shape.
        raise ValueError(str(exc)) from exc
    except OSError as exc:
        # W7 closes the most common race paths via the under-lock
        # recheck in `Store.restore`. Bare OSError can still surface
        # from genuine disk-level failures during the restore write or
        # unlink (EIO mid-write, ENOSPC during the rename, EACCES on
        # the unlink, …). Surface as ValueError so the MCP tool
        # boundary returns a clean structured error rather than letting
        # the bare OSError leak — mirror of the W1 `memory_remove`
        # arm in handlers/remove.py.
        raise ValueError(f"failed to restore memory {id}: {exc}") from exc
    except ValueError:
        # _load_tombstone_path raises ValueError on a malformed file
        # (e.g. missing `created`). Surface verbatim — the message
        # tells the caller which field is missing.
        raise
    deps.recorder.record(
        "restore",
        id=memory.id,
        scopes=memory.scopes,
    )
    return deps.responses.committed(memory)


__all__ = ["DESC_MEMORY_RESTORE", "memory_restore"]
