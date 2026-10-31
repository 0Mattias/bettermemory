"""`memory_admin(action="restore")`: bring a tombstoned memory back to active."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..claims import check_claim, claim_reason_is_indeterminate, load_claims
from ..models import LinkType, Memory, TombstonedMemory
from ..origin import commit_reachable
from ..store import MemoryNotFoundError, NotFoundError, NotTombstonedError, Store
from ..verify import (
    Anchoring,
    ClaimAnchor,
    _worktree_root_is_live,
    unverifiable_attestations,
)
from . import _guard
from . import standing_review as _standing_review
from ._shared import Context, _advance_turn, caller_anchoring, has_home

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


@dataclass(frozen=True)
class TrustStrip:
    """What a restore left behind: the stored claims the origin tree
    contradicts and the attested paths that no longer exist. `any` is
    also whether the verification stamp was cleared — a stamp asserts
    the whole record, and a record that lost a field it was stamped
    over is not the record that was verified.

    `verified_head` is the one drop that does NOT clear the stamp: the
    origin tree's HEAD no longer descends from the stamp's commit anchor
    (a history rewritten while the record sat tombstoned), so the
    commit-drift leg cannot count from it and falls back to the
    author-date count — but the record is still the one that was
    verified, so `last_verified_at` stays. `reported` is whether the
    response should carry a `trust_stripped` block at all."""

    claims: list[str] = field(default_factory=list)
    verified_paths: list[str] = field(default_factory=list)
    verified_head: bool = False

    @property
    def any(self) -> bool:
        return bool(self.claims or self.verified_paths)

    @property
    def reported(self) -> bool:
        return self.any or self.verified_head

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": list(self.claims),
            "verified_paths": list(self.verified_paths),
            "verification_cleared": self.any,
            "verified_head_dropped": self.verified_head,
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
        if self.verified_head:
            out["verified_head_dropped"] = True
        return out


def trust_strip_for(
    tombstone: TombstonedMemory, anchoring: Anchoring | None = None
) -> TrustStrip:
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

    With `anchoring` a homed record's fields are judged where design 3.6
    reads them (`_anchored_strip`); a record with no home row, and every
    record without `anchoring`, at its recorded worktree as above.
    """
    anchor = anchoring.for_record(tombstone.id) if anchoring is not None else None
    if anchor is not None:
        return _anchored_strip(tombstone, anchor)
    root = tombstone.origin.worktree_root if tombstone.origin else None
    live_root = root if root is not None and _worktree_root_is_live(root) else None
    dropped_claims: list[str] = []
    if live_root is not None and tombstone.claims:
        root_path = Path(live_root).resolve(strict=False)
        for raw in tombstone.claims:
            parsed = load_claims([raw])
            if not parsed:
                continue
            reason = check_claim(parsed[0], root_path)
            # Dropped only on a CONTRADICTION. A claim whose file this
            # process could not read is one the tree said nothing
            # about, and a restore that strips it — and clears the
            # stamp with it — would be manufacturing a counterexample
            # out of a stat that did not answer. It stays, exactly as
            # a claim under a dead root stays.
            if reason is not None and not claim_reason_is_indeterminate(reason):
                dropped_claims.append(raw)
    dropped_paths = (
        unverifiable_attestations(tombstone.verified_paths, worktree_root=live_root)
        if tombstone.verified_paths
        else []
    )
    # The stamp's commit anchor is judged only against a live origin
    # tree, like a relative attestation: a replica that never had the
    # repository cannot say the commit is gone. "Gone" is HEAD no longer
    # descending from it — a history rewritten while the record sat
    # tombstoned leaves the object in the store but nothing to count
    # from. `commit_reachable` answers None when git cannot say either
    # way, which keeps the anchor.
    dropped_head = (
        live_root is not None
        and tombstone.verified_head is not None
        and commit_reachable(Path(live_root), tombstone.verified_head) is False
    )
    return TrustStrip(
        claims=dropped_claims, verified_paths=dropped_paths, verified_head=dropped_head
    )


def _anchored_strip(tombstone: TombstonedMemory, anchor: ClaimAnchor) -> TrustStrip:
    """`trust_strip_for` read through the record's claim anchor (design
    3.6). A stored claim is dropped only on a contradiction at the home
    checkout's live view: a stand-in's contradiction is no evidence about
    the home's tree, and no live view reads nothing. An attested path is
    dropped only where its read convicts (`unverifiable_attestations` with
    ``stored=True``). The stamp's commit is resolved where a cited commit
    is (`ClaimAnchor.commit_root`) and dropped only when the home
    checkout's HEAD no longer descends from it; resolved in the caller's
    store, or nowhere, it stays."""
    dropped_claims: list[str] = []
    if anchor.convicting and anchor.root is not None and tombstone.claims:
        root_path = Path(anchor.root).resolve(strict=False)
        for raw in tombstone.claims:
            parsed = load_claims([raw])
            if not parsed:
                continue
            reason = check_claim(parsed[0], root_path)
            if reason is not None and not claim_reason_is_indeterminate(reason):
                dropped_claims.append(raw)
    dropped_paths = (
        unverifiable_attestations(tombstone.verified_paths, anchor=anchor, stored=True)
        if tombstone.verified_paths
        else []
    )
    dropped_head = False
    if tombstone.verified_head is not None:
        commit_root, rule = anchor.commit_root(tombstone.verified_head)
        dropped_head = (
            commit_root is not None
            and rule == "home_checkout"
            and commit_reachable(Path(commit_root), tombstone.verified_head) is False
        )
    return TrustStrip(
        claims=dropped_claims, verified_paths=dropped_paths, verified_head=dropped_head
    )


def restore_with_trust_check(
    store: Store, memory_id: str, anchoring: Anchoring | None = None
) -> tuple[Memory, TrustStrip]:
    """The restore every surface runs: judge the tombstone's trust
    fields, then restore with the failures dropped and the stamp cleared
    when anything was. Raises what `Store.restore` raises; a tombstone
    that cannot be loaded here is handed to `restore` unjudged so the
    refusal it produces is the canonical one. `anchoring` reads a homed
    record's fields where design 3.6 reads them (`trust_strip_for`)."""
    try:
        tombstone = store.load_tombstone(memory_id)
    except (MemoryNotFoundError, NotTombstonedError):
        strip = TrustStrip()
    else:
        strip = trust_strip_for(tombstone, anchoring)
    memory = store.restore_trimmed(
        memory_id,
        drop_claims=strip.claims,
        drop_verified_paths=strip.verified_paths,
        clear_verification=strip.any,
        drop_verified_head=strip.verified_head,
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
    deps: ToolHandlers, id: str, said: str | None = None, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    # A record whose ``supersedes`` links name a standing record takes it
    # out of force again when it comes back: that rests on the user's
    # words, as the link did (unit RS-F7 part 5).
    try:
        tombstone = deps.store.get_tombstone(id)
        links = deps.store.tombstone_links(id)
    except (NotFoundError, MemoryNotFoundError, NotTombstonedError):
        tombstone, links = None, []
    if tombstone is not None:
        targets = [link.target_id for link in links if link.type == LinkType.SUPERSEDES]
        refusal = _guard.guard(deps, "restore", targets, said)
        if refusal is not None:
            deps.recorder.record("restore", id=id, status="refused_standing")
            return refusal
    # A record with no home row is read at its recorded tree as before
    # (design 8.6) and needs no caller; a homed one is read at its anchor.
    anchoring = (
        caller_anchoring(deps.store, deps.config)[0]
        if has_home(deps.store, id)
        else None
    )
    try:
        memory, strip = restore_with_trust_check(deps.store, id, anchoring)
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
    if strip.reported:
        response["trust_stripped"] = strip.to_dict()
    if strip.any:
        response["hint"] = _TRUST_STRIPPED_HINT
    # A standing memory restored with nothing replacing it is held until
    # it is read against the standing roster (ENG-200).
    return _standing_review.with_held(
        response, _standing_review.held_reply(deps, [memory.id])
    )


__all__ = [
    "TrustStrip",
    "memory_restore",
    "restore_with_trust_check",
    "trust_strip_for",
]
