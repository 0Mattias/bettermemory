"""memory_restore MCP tool — bring a tombstoned memory back to active."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..claims import check_claim, load_claims
from ..models import Memory, TombstonedMemory
from ..store import MemoryNotFoundError, NotTombstonedError, Store
from ..verify import _worktree_root_is_live, unverifiable_attestations
from ._shared import Context, _advance_turn

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_RESTORE = (
    "Bring a tombstoned memory back to the active set. Strips the "
    "removal frontmatter, moves the file out of .tombstones/, and "
    "preserves the original `created`, `updated`, and "
    "`last_verified_at` timestamps — the body didn't change while "
    "it was tombstoned, so the recency boost stays honest. The trust "
    "fields are re-checked on the way back: a stored claim the origin "
    "tree now contradicts or an attested path that no longer exists is "
    "dropped and `last_verified_at` cleared, reported under "
    "`trust_stripped`. Raises "
    "if the id is active (use memory_update for edits) or unknown. "
    "The original removal reason and session live on in the event "
    "log even after restore."
)


@dataclass(frozen=True)
class TrustStrip:
    """What a restore left behind: the stored claims the origin tree
    contradicts and the attested paths that no longer exist. `any` is
    also whether the verification stamp was cleared — a stamp asserts
    the whole record, and a record that lost a field it was stamped
    over is not the record that was verified."""

    claims: list[str] = field(default_factory=list)
    verified_paths: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.claims or self.verified_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": list(self.claims),
            "verified_paths": list(self.verified_paths),
            "verification_cleared": self.any,
        }

    def event_fields(self) -> dict[str, Any]:
        """Conditional, so a restore that stripped nothing keeps the
        event's shape."""
        out: dict[str, Any] = {}
        if self.claims:
            out["claims_dropped"] = list(self.claims)
        if self.verified_paths:
            out["attestations_dropped"] = list(self.verified_paths)
        if self.any:
            out["verification_cleared"] = True
        return out


def trust_strip_for(tombstone: TombstonedMemory) -> TrustStrip:
    """Judge a tombstone's trust fields the way `memory_verify` judges a
    stored record before it re-stamps one.

    A restore used to re-admit every trust field the tombstone carried —
    `last_verified_at`, the attestations, the claims — with no oracle
    re-check (the 2026-09-01 integrity recon's third weak point, the
    remaining half of it after sync admission). The tree moves while a
    record sits tombstoned, so the same two checks the verify handler
    runs before a stamp run here, with the same scoping: stored claims
    and RELATIVE attestations are judged only against a live origin
    worktree (a synced replica must not lose fields over a root this
    machine never had), ABSOLUTE attestations always, since they were
    attested as on-this-machine observations. Stored entries `load_claims`
    cannot parse are kept — doctor's job, not this gate's.
    """
    root = tombstone.origin.worktree_root if tombstone.origin else None
    live_root = root if root is not None and _worktree_root_is_live(root) else None
    dropped_claims: list[str] = []
    if live_root is not None and tombstone.claims:
        root_path = Path(live_root).resolve(strict=False)
        for raw in tombstone.claims:
            parsed = load_claims([raw])
            if parsed and check_claim(parsed[0], root_path) is not None:
                dropped_claims.append(raw)
    dropped_paths = (
        unverifiable_attestations(tombstone.verified_paths, worktree_root=live_root)
        if tombstone.verified_paths
        else []
    )
    return TrustStrip(claims=dropped_claims, verified_paths=dropped_paths)


def restore_with_trust_check(store: Store, memory_id: str) -> tuple[Memory, TrustStrip]:
    """The restore every surface runs: judge the tombstone's trust
    fields, then restore with the failures dropped and the stamp cleared
    when anything was. Raises what `Store.restore` raises; a tombstone
    that cannot be loaded here is handed to `restore` unjudged so the
    refusal it produces is the canonical one."""
    try:
        tombstone = store.load_tombstone(memory_id)
    except (MemoryNotFoundError, NotTombstonedError):
        strip = TrustStrip()
    else:
        strip = trust_strip_for(tombstone)
    memory = store.restore(
        memory_id,
        drop_claims=strip.claims,
        drop_verified_paths=strip.verified_paths,
        clear_verification=strip.any,
    )
    return memory, strip


_TRUST_STRIPPED_HINT = (
    "The record came back without the trust it could not prove: the "
    "listed claims the origin tree now contradicts and the attested paths "
    "that no longer exist were dropped, and the verification stamp cleared "
    "with them. memory_update the body where it drifted, then memory_verify "
    "with fresh claims and attestations."
)


async def memory_restore(
    deps: "ToolHandlers", id: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    try:
        memory, strip = restore_with_trust_check(deps.store, id)
    except NotTombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    except OSError as exc:
        raise ValueError(f"failed to restore memory {id}: {exc}") from exc
    except ValueError:
        raise
    deps.recorder.record(
        "restore",
        id=memory.id,
        scopes=memory.scopes,
        **strip.event_fields(),
    )
    response = deps.responses.committed(memory)
    if strip.any:
        response["trust_stripped"] = strip.to_dict()
        response["hint"] = _TRUST_STRIPPED_HINT
    return response


__all__ = [
    "DESC_MEMORY_RESTORE",
    "TrustStrip",
    "memory_restore",
    "restore_with_trust_check",
    "trust_strip_for",
]
