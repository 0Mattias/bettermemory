"""memory_verify MCP tool — bump `last_verified_at` after spot-checking.

A verify reads the record's claims where design 3.6 reads them: at its
home checkout's live view, translated across its recorded views, or in a
stand-in checkout that confirms by presence and never convicts
(`verify.ClaimAnchor`, from `_shared.caller_anchoring`). A record with no
home row is homed at the caller's resolution when the caller's checkout is
the tree it was read at, and the caller's HEAD is recorded as a witness of
the caller's project, in the transaction that writes the stamp (design
8.1).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._response import isoformat, isoformat_optional
from ..claims import check_claim, claim_reason_is_indeterminate, load_claims
from ..models import Memory
from ..origin import head_sha
from ..store import ConcurrentUpdateError, MemoryNotFoundError, TombstonedError
from ..symbols import SymbolDriftReport, check_symbol_citations
from ..time_utils import isoformat_utc
from ..verify import (
    Anchoring,
    ClaimAnchor,
    _worktree_root_is_live,
    detect_path_drift,
    unverifiable_attestations,
)
from ._shared import (
    Context,
    _NOTE_MAX_LEN,
    _advance_turn,
    _validate_declared_claims,
    caller_anchoring,
    has_home,
    request_cwd,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from ..location import Caller

# Handler-boundary caps on the verified_* attestation lists. The model
# field validator (`Memory._cap_verified_list`) also caps the count at 64,
# but `Store.mark_verified` writes via `model_copy(update=...)`, which
# Pydantic runs WITHOUT field validators — so the model cap is bypassed on
# the verify path. Enforce here (mirroring how `scopes` is guarded both in
# the model and at the write handler) so a hostile/runaway caller can't push
# an unbounded or pathological attestation list. The per-item length bound is
# generous (a path/commit/version is realistically well under it); the
# `_frontmatter.dumps` aggregate cap is the ultimate backstop against
# frontmatter overflow, but a clear per-field error here is friendlier.
_MAX_VERIFIED_ENTRIES = 64
_MAX_VERIFIED_ITEM_LEN = 1024


def _refuse_stale_stored_claims(
    stored: list[str], origin_root: str | None, *, anchor: ClaimAnchor | None = None
) -> None:
    """Refuse the verify when a STORED claim no longer holds.

    Runs only when the origin worktree is visible; an invisible tree
    skips the check (the commit-drift leg on the origin machine keeps
    watching) rather than blocking verifies from a synced replica.
    Unparseable stored entries are skipped by `load_claims` — doctor's
    job, not this gate's.

    With `anchor` the claims are read at the anchor's root in place of
    `origin_root`, and only where that read convicts (the home checkout's
    live view, design 3.6): a stand-in's contradiction is no evidence
    about the home's tree, and no live view reads nothing.
    """
    if anchor is not None:
        if not anchor.convicting:
            return
        origin_root = anchor.root
    if origin_root is None:
        return
    try:
        root = Path(origin_root).resolve(strict=False)
    except (OSError, ValueError):
        return
    # The guard above covers `resolve()` only; `Path.is_dir()` re-raises
    # EACCES and friends on its own. An unreadable tree is an invisible
    # tree, which this check already skips — `os.path.isdir` is what
    # makes it skip instead of raise.
    if not os.path.isdir(root):
        return
    failures = [
        (claim.render(), reason)
        for claim in load_claims(stored)
        if (reason := check_claim(claim, root)) is not None
    ]
    # The root was readable, so the guard above passed; the claimed FILE
    # one level down may still not be. That is not "no longer hold" —
    # an accusation about a tree nothing read — and the refusal has to
    # say which it is. Both refuse, because a stamp asserts the whole
    # record still matches reality and an unchecked claim is not a
    # checked one; only the diagnosis and the remedy differ.
    contradicted = [
        pair for pair in failures if not claim_reason_is_indeterminate(pair[1])
    ]
    unchecked = [pair for pair in failures if claim_reason_is_indeterminate(pair[1])]
    if contradicted:
        detail = "; ".join(f"{rendered}: {reason}" for rendered, reason in contradicted)
        raise ValueError(
            f"cannot verify: {len(contradicted)} stored claim(s) no longer "
            f"hold — {detail}. A verify stamps the whole record fresh, "
            "and its claims are part of the record. memory_update the "
            "body first (body edits clear claims), or re-declare "
            "corrected claims by passing claims=[...] on this call."
        )
    if unchecked:
        detail = "; ".join(f"{rendered}: {reason}" for rendered, reason in unchecked)
        raise ValueError(
            f"cannot verify: {len(unchecked)} stored claim(s) could not be "
            f"checked here — {detail}. A verify stamps the whole record "
            "fresh, and an unchecked claim is not a checked one. Verify from "
            "a machine that can read the tree, or re-declare corrected "
            "claims by passing claims=[...] on this call."
        )


def _refuse_unverifiable_stored_attestations(
    stored: list[str],
    origin_root: str | None,
    *,
    anchor: ClaimAnchor | None = None,
) -> None:
    """Refuse a preserving re-verify when a STORED attestation is gone.

    The attestation-existence gate in the handler runs on a NEWLY passed
    `verified_paths` list; `verified_paths=None` preserves the stored
    lists (`Store.mark_verified` None-preserves). But stamping
    `last_verified_at` asserts the whole record still matches reality —
    the exact rationale the stored-CLAIMS re-check gives for re-running
    the oracle on every stamp — and a stored attestation whose target
    has since been deleted is the same kind of recorded counterexample.
    The read side cannot recover it on its own: an absolute attestation
    the prose never cites is inert forever (`unverifiable_attestations`'
    own docstring), so without this gate the documented no-arg
    slide-the-timestamp path re-stamps `fresh` on top of it for another
    whole freshness window.

    Scoping mirrors the stored-claims re-check's worktree-visibility
    discipline, split per entry kind because attestations, unlike
    claims, come in both anchored forms:

    - ABSOLUTE entries (`/`, `~`, `$HOME`, drive-letter spellings) are
      checked always — they were attested as on-this-machine
      observations and need no root to resolve;
    - RELATIVE entries are checked only when the origin worktree is a
      live directory HERE (`worktree_root=None` makes
      `unverifiable_attestations` skip them). A synced replica carries
      the origin host's `worktree_root`; joining relatives onto a dead
      root would refuse every replica re-verify wholesale — the
      constant-function failure `verify._worktree_root_is_live`
      documents — so an invisible tree reads as could-not-ask, never as
      a counterexample.

    With `anchor` the stored list is read where design 3.6 reads it
    (`unverifiable_attestations` with ``stored=True``): an absolute entry at
    the live view of the checkout it was recorded under, a relative one at
    the anchor's root, and only a read that convicts refuses.
    """
    if not stored:
        return
    if anchor is not None:
        unseen = unverifiable_attestations(stored, anchor=anchor, stored=True)
    else:
        root: str | None = None
        if origin_root is not None:
            try:
                resolved = Path(origin_root).resolve(strict=False)
            except (OSError, ValueError):
                resolved = None
            # `os.path.isdir`, not `resolved.is_dir()`: the try above
            # guards `resolve()` alone, and an unreadable worktree must
            # read as invisible here (root stays None, attestations go
            # unchecked) rather than raising out of the handler.
            if resolved is not None and os.path.isdir(resolved):
                root = origin_root
        unseen = unverifiable_attestations(stored, worktree_root=root)
    if unseen:
        raise ValueError(
            f"cannot attest {len(unseen)} stored path(s) that do not exist "
            f"on this machine: {', '.join(unseen)}. verified_paths=None "
            "preserves the stored attestation list, and stamping "
            "last_verified_at asserts it still holds. Pass a corrected "
            "verified_paths list (the lists REPLACE, not append), or move "
            "intentionally-absent entries to verified_absent_paths."
        )


# How many resolved citations a refusal lists before eliding the rest.
_SUGGESTED_PATHS_SHOWN = 8


def _relative_to_root(path: str, root: str | None) -> str:
    """The citation as the writer would attest it: repo-relative when it
    sits under the memory's worktree, as written otherwise."""
    if root is None:
        return path
    try:
        return str(
            Path(path)
            .resolve(strict=False)
            .relative_to(Path(root).resolve(strict=False))
        )
    except (OSError, ValueError):
        return path


def _refuse_evidence_free_stamp(
    memory_id: str,
    body: str,
    origin_root: str | None,
    *,
    attests_paths: bool,
    attests_absence: bool,
    declares_claims: bool,
    anchor: ClaimAnchor | None = None,
) -> None:
    """Refuse a stamp that names nothing it checked on a memory whose
    citations resolve.

    `last_verified_at` asserts the record matches reality, and the read
    side's drift legs read exactly two things a verify can leave behind:
    `verified_paths` and `claims`. A bare `memory_verify(id)` stamped
    `fresh` on zero evidence, and on a memory that cites files the caller
    could have attested that stamp rests on nothing a reader can use —
    the first weak point of the integrity recon, and the one a curation
    pass reaches for most (the checkable half of the verification-debt
    buckets is exactly these memories). Scoped to what RESOLVES: an
    absolute citation that exists here, or a relative one anchored in the
    memory's live worktree — `detect_path_drift`'s `checked` less its
    misses. A memory with no resolving citation keeps the documented
    no-arg slide-the-timestamp: a preference or a lesson has nothing to
    attest, and a body whose cited files are gone is drift the read side
    already reports, not evidence a stamp could carry. Attesting
    intentional absence or declaring claims is evidence too, since the
    caller looked; an explicit `[]` on every list is not.

    The refusal lists the resolved citations, repo-relative when they sit
    under the worktree, as the list to attest — the same remedy shape as
    the vanished-attestation refusal, pointed the other way.

    With `anchor` the citations are read where design 3.6 reads them, and
    listed relative to the anchor's root.
    """
    if attests_paths or attests_absence or declares_claims:
        return
    if anchor is not None:
        root = anchor.root
        report = detect_path_drift(body, anchor=anchor)
    else:
        root = (
            origin_root
            if origin_root is not None and _worktree_root_is_live(origin_root)
            else None
        )
        report = detect_path_drift(body, worktree_root=root)
    gone = set(report.missing) | set(report.expected_absent)
    resolved = [p for p in report.checked if p not in gone]
    if not resolved:
        return
    suggested = [_relative_to_root(p, root) for p in resolved]
    shown = ", ".join(suggested[:_SUGGESTED_PATHS_SHOWN])
    if len(suggested) > _SUGGESTED_PATHS_SHOWN:
        shown += f", and {len(suggested) - _SUGGESTED_PATHS_SHOWN} more"
    raise ValueError(
        f"cannot verify: memory {memory_id} cites {len(resolved)} path(s) that "
        f"resolve here and this call attests none of them: {shown}. A stamp "
        "that names nothing it checked asserts nothing the read side can use. "
        "Pass verified_paths with the ones you looked at, verified_absent_paths "
        "for any that should be absent, or claims for what the body asserts "
        "about them."
    )


def _record_root(snapshot: Memory, anchoring: Anchoring | None) -> str | None:
    """The tree a record is read at by its recorded worktree: the worktree
    its origin recorded (design 8.6); for a record that recorded none, its
    anchor's root under the request's `anchoring` (`Anchoring.for_record`,
    design 3.6), else None. No remote is compared (decision B23)."""
    root = snapshot.origin.worktree_root if snapshot.origin is not None else None
    if root is not None or anchoring is None:
        return root
    anchor = anchoring.for_record(snapshot.id)
    return anchor.root if anchor is not None else None


def _within(path: str | None, root: str | None) -> bool:
    """Whether `path` is `root` or below it, read through symlinks."""
    if path is None or root is None:
        return False
    try:
        here, top = os.path.realpath(path), os.path.realpath(root)
    except (OSError, ValueError):
        return False
    return here == top or here.startswith(top.rstrip(os.sep) + os.sep)


def _is_callers_tree(root: str | None, caller_root: str | None) -> bool:
    """Whether `root`, the tree a record with no home row was read at, is
    the live view of the caller's checkout: the one place a verify can say
    the record was written in (design 2.1). Elsewhere the record keeps no
    home row until the migration homes it from its origin (design 8.6)."""
    if root is None or caller_root is None:
        return False
    try:
        return os.path.realpath(root) == os.path.realpath(caller_root)
    except (OSError, ValueError):
        return False


def _stamp(
    deps: ToolHandlers,
    caller: Caller | None,
    snapshot: Memory,
    *,
    home_it: bool,
    verified_paths: list[str] | None,
    verified_commits: list[str] | None,
    verified_versions: list[str] | None,
    verified_absent_paths: list[str] | None,
    claims: list[str] | None,
    verified_head: str | None,
) -> Memory:
    """Write the stamp, the record's home when `home_it` and it still has
    none, and the caller's HEAD as a witness of the caller's project, in
    one store transaction (design 8.1): all of them land or none does. The
    home is the caller's (`location.Caller.home`: its resolution's, or the
    provisional project of a location nothing could be read for, decision
    B6), read again inside the transaction, so a home another writer gave
    the record first stands. With no caller (a record with no home row
    verified from outside the tree it was written in) the stamp is all
    that is written."""
    with deps.store.batch():
        memory = deps.store.mark_verified(
            snapshot.id,
            verified_paths=verified_paths,
            verified_commits=verified_commits,
            verified_versions=verified_versions,
            verified_absent_paths=verified_absent_paths,
            claims=claims,
            verified_head=verified_head,
            expected_last_verified_at=snapshot.last_verified_at,
            # Both snapshot fields ride the CAS: `last_verified_at`
            # alone is None == None on a never-verified memory, so a
            # concurrent edit between `load_one` and the store's
            # under-lock compare would let this stamp land on prose
            # the caller never checked. `updated` moves on the edit.
            expected_updated=snapshot.updated,
            check_expected=True,
        )
        if caller is None:
            return memory
        if home_it and caller.model.home(snapshot.id) is None:
            home = caller.home()
            deps.store.put_membership(
                "home",
                {
                    "record_id": snapshot.id,
                    "kind": "memory",
                    "project": home.project,
                    "checkout": home.checkout,
                    "rule": home.rule,
                    "decided": isoformat_utc(memory.last_verified_at or memory.updated),
                },
            )
        caller.add_head_witness("verify")
    return memory


def _check_verify_args(
    note: str | None,
    verified_paths: list[str] | None,
    verified_commits: list[str] | None,
    verified_versions: list[str] | None,
    verified_absent_paths: list[str] | None,
) -> None:
    """Take the call's note and verified_* lists; return None, raising on a bad one."""
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
        ("verified_absent_paths", verified_absent_paths),
    ):
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ValueError(f"{label} must be a list of strings if provided")
        if len(value) > _MAX_VERIFIED_ENTRIES:
            raise ValueError(
                f"{label} capped at {_MAX_VERIFIED_ENTRIES} entries "
                f"(got {len(value)}); a memory cites a handful of paths, "
                "not a manifest"
            )
        for item in value:
            if len(item) > _MAX_VERIFIED_ITEM_LEN:
                raise ValueError(
                    f"{label} entry is {len(item)} chars — cap is "
                    f"{_MAX_VERIFIED_ITEM_LEN}. Attestations are short "
                    "path/commit/version strings, not prose."
                )


def _load_verify_snapshot(deps: ToolHandlers, memory_id: str) -> Memory:
    """Take the record's id; return its current snapshot, a gone record as ValueError."""
    # W8: load the current snapshot to capture `last_verified_at` for
    # the optimistic-concurrency CAS in `Store.mark_verified`. The
    # snapshot fingerprint is what the under-lock recheck compares
    # against — if another agent's verify lands between this load and
    # the store-level write, the CAS fires and we surface a structured
    # stale response. Mirror of the W2 `memory_update` flow.
    try:
        return deps.store.load_one(memory_id)
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc


def _verify_read_site(
    deps: ToolHandlers, memory_id: str, snapshot: Memory
) -> tuple[str | None, Anchoring | None, Caller | None, ClaimAnchor | None, str | None]:
    """Take the record and its snapshot; return origin root, anchoring, caller, anchor, live root."""
    # Where the claims are read (design 3.6). A homed record's anchor comes
    # from the membership model and the caller's resolution: its home
    # checkout's live view, a stand-in, or nothing. A record with no home
    # row is read where its origin recorded it, as before the migration
    # homes it (design 8.6), and is homed by this verify at the caller's
    # resolution when that is the tree it was read at (design 2.1, 8.1); a
    # caller outside that tree is not resolved, as nothing here reads it.
    homed = has_home(deps.store, memory_id)
    origin_root = None if homed else _record_root(snapshot, None)
    anchoring: Anchoring | None = None
    caller: Caller | None = None
    if homed or _within(request_cwd(), origin_root):
        anchoring, caller = caller_anchoring(deps.store, deps.config)
    anchor = anchoring.for_record(memory_id) if anchoring is not None else None
    # The root relative claims are read at, live: the anchor's, else the
    # recorded worktree when it is a directory here.
    live_root = (
        anchor.root
        if anchor is not None
        else (
            origin_root
            if origin_root is not None and _worktree_root_is_live(origin_root)
            else None
        )
    )
    return origin_root, anchoring, caller, anchor, live_root


def _refuse_unseen_attestations(
    verified_paths: list[str] | None,
    snapshot: Memory,
    origin_root: str | None,
    anchor: ClaimAnchor | None,
    live_root: str | None,
) -> None:
    """Take the passed list, the snapshot and where it is read; return None, raising on unseen paths."""
    # An attestation naming a path this machine cannot see is not evidence.
    # Before this check, `mark_verified` performed no verification of any
    # kind — it stamped `last_verified_at` and copied the caller's lists
    # verbatim — so a caller could attest a path that never existed and the
    # memory would then read `fresh`, with the freshness resting on nothing.
    # The read side cannot recover this on its own: an ABSOLUTE attested
    # path is only ever existence-checked when the body also names it (see
    # `_normalize_attestations` in `verify_anchoring.py`), so an
    # attestation the prose never references stays inert forever.
    #
    # Refuse rather than silently drop the bad entries: this tool's own
    # description instructs the caller to attest specific files, so a
    # caller whose list is wrong needs to learn that, not get a success
    # response covering fewer paths than it claimed.
    #
    # `verified_absent_paths` is deliberately exempt — it attests
    # intentional ABSENCE, so non-existence is the claim being made.
    #
    # The READ side stays lenient on purpose (see
    # `unverifiable_attestations`): a memory attested on one host and
    # synced to another legitimately names paths the reader lacks. Only the
    # moment of attestation, where the caller asserts it looked, can demand
    # existence — which is also why this is the handler's job and not
    # `Store.mark_verified`'s. A preserving re-verify is such a moment too:
    # `verified_paths=None` carries the stored list onto a fresh stamp, so
    # it is gated below as well, with synced-store scoping
    # (`_refuse_unverifiable_stored_attestations`).
    if verified_paths:
        # A RELATIVE attestation is judged against the memory's tree, and a
        # root this machine cannot see cannot say a path under it is
        # missing: every relative entry would read as fabricated at once,
        # the constant-function failure `_worktree_root_is_live` documents.
        # Anchor only a live root; with none, `unverifiable_attestations`
        # skips relative entries as could-not-ask and still judges absolute
        # ones, which were attested as on-this-machine observations. Same
        # gate the stored-list arm below and `handlers/restore` apply.
        unseen = (
            unverifiable_attestations(verified_paths, anchor=anchor)
            if anchor is not None
            else unverifiable_attestations(verified_paths, worktree_root=live_root)
        )
        if unseen:
            raise ValueError(
                f"cannot attest {len(unseen)} path(s) that do not exist on this "
                f"machine: {', '.join(unseen)}. Attest only paths you actually "
                "checked here; if a path is intentionally absent, pass it as "
                "verified_absent_paths instead."
            )
    elif verified_paths is None and snapshot.verified_paths:
        # A PRESERVING re-verify (`None` carries the stored list forward)
        # gets the same existence discipline as a fresh list — stored
        # attestations are part of the record the stamp re-asserts, the
        # same symmetry the stored-claims re-check below enforces. `[]`
        # (the explicit clear) needs no check. Scoped leniently for the
        # synced-store case: see `_refuse_unverifiable_stored_attestations`.
        _refuse_unverifiable_stored_attestations(
            list(snapshot.verified_paths), origin_root, anchor=anchor
        )


def _verify_claims(
    claims: list[str] | None,
    snapshot: Memory,
    origin_root: str | None,
    anchor: ClaimAnchor | None,
) -> list[str] | None:
    """Take the passed claims and the snapshot; return the claims to store, refusing false ones."""
    # Claims are the one attestation the tool can CHECK, so it does — in
    # both directions. A newly-passed list goes through the same
    # declare-time oracle memory_write runs (false ⇒ refused; `[]` is
    # the explicit clear and needs no check). A verify that DOESN'T
    # re-declare, on a memory that stores claims, re-runs the oracle
    # over the stored list first: stamping `last_verified_at` asserts
    # the whole record still matches reality, and a stored claim the
    # tree now contradicts is a recorded counterexample. Refusing here
    # is what makes the read side's trust in claims transitive — every
    # `last_verified_at` on a claim-carrying memory was stamped over
    # claims that held at that instant. The stored re-check is skipped
    # only when the claims' tree isn't visible from this machine (same
    # read-side leniency as attestations: a synced store may
    # legitimately verify prose aspects it cannot stat) or is a stand-in
    # for it, never when the tree is present and disagrees.
    normalized_claims: list[str] | None = None
    if claims is not None:
        normalized_claims = (
            _validate_declared_claims(
                claims,
                worktree_root=(
                    anchor.declared_root if anchor is not None else origin_root
                ),
                surface="memory_verify",
                anchor_rule=anchor.rule if anchor is not None else None,
            )
            if claims
            else []
        )
    elif snapshot.claims:
        _refuse_stale_stored_claims(snapshot.claims, origin_root, anchor=anchor)
    return normalized_claims


def _verified_head(
    snapshot: Memory,
    anchoring: Anchoring | None,
    anchor: ClaimAnchor | None,
    origin_root: str | None,
) -> str | None:
    """Take the snapshot and where it was read; return the HEAD the stamp names, or None."""
    # The stamp's anchor: the commit the tree the checks above read stands
    # at, so the record says which tree state it was verified at and the
    # commit-drift leg can count from there in reachability space. None
    # when no checkout answers (a memory written outside git, a synced
    # replica whose tree is not here, a record with no repository), and
    # the leg keeps the author-date count for it, labelled as such.
    # The basis stays the record's: under an anchor, a record whose origin
    # names a repository and a tree is stamped at the home checkout's live
    # view, and a read that does not convict stamps nothing.
    if anchor is not None:
        return (
            head_sha(Path(anchor.root))
            if anchor.convicting
            and anchor.root is not None
            and snapshot.origin is not None
            and snapshot.origin.repo is not None
            and _record_root(snapshot, anchoring) is not None
            else None
        )
    return (
        head_sha(Path(origin_root))
        if origin_root is not None
        and snapshot.origin is not None
        and snapshot.origin.repo is not None
        and _worktree_root_is_live(origin_root)
        else None
    )


def _stamp_or_stale(
    deps: ToolHandlers,
    memory_id: str,
    caller: Caller | None,
    snapshot: Memory,
    *,
    anchoring: Anchoring | None,
    anchor: ClaimAnchor | None,
    live_root: str | None,
    verified_paths: list[str] | None,
    verified_commits: list[str] | None,
    verified_versions: list[str] | None,
    verified_absent_paths: list[str] | None,
    claims: list[str] | None,
    verified_head: str | None,
) -> tuple[Memory, None] | tuple[None, dict[str, Any]]:
    """Take the stamp's inputs; return `(stamped, None)`, or `(None, stale payload)` on a lost CAS."""
    try:
        stamped = _stamp(
            deps,
            caller,
            snapshot,
            home_it=anchor is None
            and anchoring is not None
            and _is_callers_tree(live_root, anchoring.caller_root),
            verified_paths=verified_paths,
            verified_commits=verified_commits,
            verified_versions=verified_versions,
            verified_absent_paths=verified_absent_paths,
            claims=claims,
            verified_head=verified_head,
        )
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    except ConcurrentUpdateError as exc:
        # W8: another agent landed a verify between this handler's
        # `load_one` snapshot (`_load_verify_snapshot`) and the under-lock
        # CAS in `Store.mark_verified`. The handler doesn't auto-retry — the
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
            current_updated=isoformat(exc.current_updated),
        )
        return None, {
            "status": "stale",
            "memory_id": exc.memory_id,
            "current_updated": isoformat(exc.current_updated),
            "hint": (
                "Memory was verified concurrently. Re-fetch with "
                "memory_show, reassess your attestation against the "
                "current verified_* lists, and retry."
            ),
        }
    except OSError as exc:
        # A disk-level failure under the store's write (ENOSPC, EIO,
        # EACCES). Surface as a structured ValueError so the MCP boundary
        # returns a clean "failed to verify memory <id>: …" rather than a bare
        # OSError — mirrors remove.py/restore.py.
        raise ValueError(f"failed to verify memory {memory_id}: {exc}") from exc
    return stamped, None


def _verify_response(
    deps: ToolHandlers,
    memory: Memory,
    note: str | None,
    symbol_drift: SymbolDriftReport,
) -> dict[str, Any]:
    """Take the stamped record, the note and the symbol report; return the response, after its event."""
    # The event field is conditional so the default verify event keeps its
    # exact shape — and so a non-zero count in the log means the check
    # actually fired on real prose. That count is the only telemetry a
    # future precision measurement could be built from; a field written on
    # every call would drown it.
    missing = symbol_drift.missing
    deps.recorder.record(
        "verify",
        id=memory.id,
        last_verified_at=isoformat_optional(memory.last_verified_at),
        note=note,
        verified_paths=list(memory.verified_paths),
        verified_commits=list(memory.verified_commits),
        verified_versions=list(memory.verified_versions),
        verified_absent_paths=list(memory.verified_absent_paths),
        **({"claims": list(memory.claims)} if memory.claims else {}),
        **({"verified_head": memory.verified_head} if memory.verified_head else {}),
        **({"symbol_drift_missing": len(missing)} if missing else {}),
    )
    response: dict[str, Any] = {
        "verified": memory.id,
        "last_verified_at": isoformat_optional(memory.last_verified_at),
        "updated": isoformat(memory.updated),
        "verified_paths": list(memory.verified_paths),
        "verified_commits": list(memory.verified_commits),
        "verified_versions": list(memory.verified_versions),
        "verified_absent_paths": list(memory.verified_absent_paths),
        "claims": list(memory.claims),
        "verified_head": memory.verified_head,
    }
    # Emitted only when the body actually carried a citation this check
    # could parse. Silence is the normal case and is the honest one: an
    # empty block on every call would read as "checked, nothing wrong"
    # when the truth is usually "there was nothing here to check".
    if symbol_drift:
        advisory: dict[str, Any] = dict(symbol_drift.to_dict())
        if missing:
            advisory["note"] = (
                "Advisory only — no staleness verdict reads this. Each name "
                "was looked up by AST in the file the body cites; a miss "
                "means that file parses and binds the name nowhere."
            )
        response["symbol_drift"] = advisory
    return response


DESC_MEMORY_VERIFY = (
    "Attest a memory after checking a claim against ground truth; resets "
    "last_verified_at. `verified_paths` resolve now and are anchored for "
    "drift; `verified_absent_paths` are absent on purpose; `claims` "
    "replaces the checked bindings. "
    "Refused without evidence when nothing cited resolves."
)


async def memory_verify(
    deps: ToolHandlers,
    id: str,
    note: str | None = None,
    verified_paths: list[str] | None = None,
    verified_commits: list[str] | None = None,
    verified_versions: list[str] | None = None,
    verified_absent_paths: list[str] | None = None,
    claims: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    _check_verify_args(
        note, verified_paths, verified_commits, verified_versions, verified_absent_paths
    )
    snapshot = _load_verify_snapshot(deps, id)
    origin_root, anchoring, caller, anchor, live_root = _verify_read_site(
        deps, id, snapshot
    )
    _refuse_unseen_attestations(
        verified_paths, snapshot, origin_root, anchor, live_root
    )
    normalized_claims = _verify_claims(claims, snapshot, origin_root, anchor)

    # The stamp has to name something it checked when there is something
    # to check. Evidence is what this call attests or what a `None`
    # preserves from the record; an explicit `[]` is a clear, not evidence.
    _refuse_evidence_free_stamp(
        id,
        snapshot.body,
        origin_root,
        attests_paths=bool(verified_paths)
        or (verified_paths is None and bool(snapshot.verified_paths)),
        attests_absence=bool(verified_absent_paths)
        or (verified_absent_paths is None and bool(snapshot.verified_absent_paths)),
        declares_claims=bool(claims) or (claims is None and bool(snapshot.claims)),
        anchor=anchor,
    )

    # Symbol citations in the body, AST-checked against the memory's own
    # tree. ADVISORY, and structurally so: the result is attached to this
    # response and read by nothing else. No staleness verdict, no drift
    # leg, no health rollup consumes it, and none may until a benchmark
    # measures its precision on real prose; the reach measurement in
    # `tests/test_symbol_existence.py` is the reason to be cautious, not
    # the reason to be confident.
    #
    # Computed from the SNAPSHOT rather than from the object `mark_verified`
    # returns, so the evidence and the attestation describe the same
    # revision of the prose: `mark_verified` never edits a body, but a
    # concurrent `memory_update` between the two reads would otherwise let
    # this report describe prose the caller never attested to.
    #
    # In the handler, not the store: `Store.mark_verified` is a policy-free
    # persistence primitive (pinned by
    # `test_mark_verified_does_not_itself_check_path_existence`), and this
    # is policy in the same sense the attestation refusal above is.
    symbol_drift = check_symbol_citations(
        snapshot.body, worktree_root=origin_root, anchor=anchor
    )
    verified_head = _verified_head(snapshot, anchoring, anchor, origin_root)
    outcome = _stamp_or_stale(
        deps,
        id,
        caller,
        snapshot,
        anchoring=anchoring,
        anchor=anchor,
        live_root=live_root,
        verified_paths=verified_paths,
        verified_commits=verified_commits,
        verified_versions=verified_versions,
        verified_absent_paths=verified_absent_paths,
        claims=normalized_claims,
        verified_head=verified_head,
    )
    if outcome[1] is not None:
        return outcome[1]
    return _verify_response(deps, outcome[0], note, symbol_drift)


__all__ = ["DESC_MEMORY_VERIFY", "memory_verify"]
