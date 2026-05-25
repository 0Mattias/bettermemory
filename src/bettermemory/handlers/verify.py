"""memory_verify MCP tool — bump `last_verified_at` after spot-checking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._response import isoformat, isoformat_optional
from ..store import MemoryNotFoundError, TombstonedError
from ._shared import Context, _NOTE_MAX_LEN, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_VERIFY = (
    "Bump `last_verified_at` to now after spot-checking that a "
    "memory's claims still match reality (file paths exist, "
    "version still matches, configuration still what it says).\n\n"
    "Orthogonal to content edits: this tool does NOT bump "
    "`updated`; memory_update does NOT bump `last_verified_at`. A "
    "typo fix bumps `updated` only; a verify call bumps "
    "`last_verified_at` only. Idempotent — calling twice slides "
    "the timestamp forward.\n\n"
    "Parameters:\n"
    "- `id`: memory id.\n"
    "- `note` (optional, ≤500 chars): what was checked, for the "
    "event log.\n"
    "- `verified_paths` / `verified_commits` / `verified_versions` "
    "(optional lists of strings): structured attestations. The "
    "server uses these to short-circuit later drift signals — "
    "a future retrieval whose path_drift would have flagged a "
    "path still in `verified_paths` downgrades the verdict.\n\n"
    "After memory_update on a memory you later spot-check, verify "
    "again — memory_update clears `last_verified_at` because the "
    "prior verification was for prose that no longer exists.\n\n"
    "Also resolves an unresolved `record_use(contradicted)` flag "
    "in memory_health when the body still matches reality."
)


async def memory_verify(
    deps: "ToolHandlers",
    id: str,
    note: str | None = None,
    verified_paths: list[str] | None = None,
    verified_commits: list[str] | None = None,
    verified_versions: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string if provided")
    if note is not None and len(note) > _NOTE_MAX_LEN:
        raise ValueError(
            f"note is {len(note)} chars — cap is {_NOTE_MAX_LEN}. "
            "The note is a short rationale for the verification, "
            "not a place to paste prose; trim it before recording."
        )
    for label, value in (
        ("verified_paths", verified_paths),
        ("verified_commits", verified_commits),
        ("verified_versions", verified_versions),
    ):
        if value is None:
            continue
        if not isinstance(value, list) or not all(
            isinstance(s, str) for s in value
        ):
            raise ValueError(f"{label} must be a list of strings if provided")
    try:
        memory = deps.store.mark_verified(
            id,
            verified_paths=verified_paths,
            verified_commits=verified_commits,
            verified_versions=verified_versions,
        )
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    deps.recorder.record(
        "verify",
        id=memory.id,
        last_verified_at=isoformat_optional(memory.last_verified_at),
        note=note,
        verified_paths=list(memory.verified_paths),
        verified_commits=list(memory.verified_commits),
        verified_versions=list(memory.verified_versions),
    )
    return {
        "verified": memory.id,
        "last_verified_at": isoformat_optional(memory.last_verified_at),
        "updated": isoformat(memory.updated),
        "verified_paths": list(memory.verified_paths),
        "verified_commits": list(memory.verified_commits),
        "verified_versions": list(memory.verified_versions),
    }


__all__ = ["DESC_MEMORY_VERIFY", "memory_verify"]
