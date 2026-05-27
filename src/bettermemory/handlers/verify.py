"""memory_verify MCP tool — bump `last_verified_at` after spot-checking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._response import isoformat, isoformat_optional
from ..store import ConcurrentUpdateError, MemoryNotFoundError, TombstonedError
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
    "Concurrency: under multi-agent contention, two parallel "
    "verify calls on the same id used to silently last-write-wins "
    "— agent A attesting path #1 and agent B attesting path #2 "
    "simultaneously would lose one of the attestations because "
    "`verified_*` lists have REPLACE (not append) semantics. The "
    "handler now performs an optimistic-concurrency check against "
    "the snapshot it fetched. If another agent verified the memory "
    "between the snapshot and the write, the response is "
    '`status="stale"` with the current on-disk `updated` timestamp; '
    "re-fetch with memory_show, reassess your attestation against "
    "the now-current verified_* lists, and retry. Contract is "
    "reread + reattest, not silent merge.\n\n"
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
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ValueError(f"{label} must be a list of strings if provided")
    # W8: load the current snapshot to capture `last_verified_at` for
    # the optimistic-concurrency CAS in `Store.mark_verified`. The
    # snapshot fingerprint is what the under-lock recheck compares
    # against — if another agent's verify lands between this load and
    # the store-level write, the CAS fires and we surface a structured
    # stale response. Mirror of the W2 `memory_update` flow.
    try:
        snapshot = deps.store.load_one(id)
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc

    try:
        memory = deps.store.mark_verified(
            id,
            verified_paths=verified_paths,
            verified_commits=verified_commits,
            verified_versions=verified_versions,
            expected_last_verified_at=snapshot.last_verified_at,
            check_expected=True,
        )
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    except ConcurrentUpdateError as exc:
        # W8: another agent landed a verify between this handler's
        # `load_one` snapshot above and the under-lock CAS in
        # `Store.mark_verified`. The handler doesn't auto-retry — the
        # caller's attestation may now conflict with the winner's (e.g.
        # both attested different `verified_paths` entries) in a way
        # only the caller can reconcile. Surface as a structured
        # `status="stale"` payload mirroring the W2 `memory_update`
        # response shape exactly so a programmatic caller can branch on
        # the status with the same code path and rebase via the carried
        # `current_updated`.
        deps.recorder.record(
            "verify",
            status="stale",
            id=exc.memory_id,
            current_updated=exc.current_updated.isoformat(),
        )
        return {
            "status": "stale",
            "memory_id": exc.memory_id,
            "current_updated": exc.current_updated.isoformat(),
            "hint": (
                "Memory was verified concurrently. Re-fetch with "
                "memory_show, reassess your attestation against the "
                "current verified_* lists, and retry."
            ),
        }
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
