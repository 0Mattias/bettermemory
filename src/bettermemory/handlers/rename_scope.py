"""memory_rename_scope MCP tool — bulk-rename a scope tag."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import validate_scope
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_RENAME_SCOPE = (
    "Replace `old_scope` with `new_scope` across active memories "
    "(and tombstones, by default). The cheap fix for typo'd or "
    "deprecated scopes — e.g. `projct:foo` -> `projects:foo` "
    "after a misspell, or `infra` -> `infrastructure` after "
    "settling on the long form. Bumps `updated` on each touched "
    "memory; preserves `last_verified_at` (the body's claims "
    "didn't change, only the tag did). Memories that already "
    "carry `new_scope` get `old_scope` removed without "
    "duplicating `new_scope`. Returns "
    "`{active: [ids], tombstoned: [ids], failed: [{id, reason}]}` — "
    "the first two list the records actually modified; `failed` lists "
    "any records whose re-dump was skipped (e.g. the rename would push "
    "the file past the size cap) so a partial run is never reported as "
    "a clean one. Pass `include_tombstones=False` to "
    "leave the removal audit log untouched. Use after "
    "memory_health surfaces a typo in `rare_scopes`."
)


async def memory_rename_scope(
    deps: "ToolHandlers",
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
        # Store.rename_scope swallows per-file (ValueError, KeyError,
        # FileNotFoundError) — concurrent tombstone/restore races and
        # malformed files are skipped. A genuine disk-level failure
        # (EIO mid-write, ENOSPC during the atomic rename, EACCES on
        # the unlink, …) from `_write_path`/`_atomic_write_post` still
        # propagates out. Surface as ValueError so the MCP tool
        # boundary returns a clean structured error rather than leaking
        # the bare OSError — mirror of the OSError arms in
        # handlers/remove.py and handlers/restore.py. The bulk rename
        # is applied file-by-file, so a mid-loop failure may leave the
        # split-scope state partially applied; the operation is
        # idempotent, so a re-run safely finishes the remaining files.
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


__all__ = ["DESC_MEMORY_RENAME_SCOPE", "memory_rename_scope"]
