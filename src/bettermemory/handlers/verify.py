"""memory_verify MCP tool — bump `last_verified_at` after spot-checking."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .._response import isoformat, isoformat_optional
from ..claims import check_claim, load_claims
from ..origin import repos_match
from ..store import ConcurrentUpdateError, MemoryNotFoundError, TombstonedError
from ..symbols import check_symbol_citations
from ..verify import (
    _worktree_root_is_live,
    detect_path_drift,
    unverifiable_attestations,
)
from ._shared import (
    Context,
    _NOTE_MAX_LEN,
    _advance_turn,
    _validate_declared_claims,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers

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


def _refuse_stale_stored_claims(stored: list[str], origin_root: str | None) -> None:
    """Refuse the verify when a STORED claim no longer holds.

    Runs only when the origin worktree is visible; an invisible tree
    skips the check (the commit-drift leg on the origin machine keeps
    watching) rather than blocking verifies from a synced replica.
    Unparseable stored entries are skipped by `load_claims` — doctor's
    job, not this gate's.
    """
    if origin_root is None:
        return
    try:
        root = Path(origin_root).resolve(strict=False)
    except (OSError, ValueError):
        return
    if not root.is_dir():
        return
    failures = [
        (claim.render(), reason)
        for claim in load_claims(stored)
        if (reason := check_claim(claim, root)) is not None
    ]
    if failures:
        detail = "; ".join(f"{rendered}: {reason}" for rendered, reason in failures)
        raise ValueError(
            f"cannot verify: {len(failures)} stored claim(s) no longer "
            f"hold — {detail}. A verify stamps the whole record fresh, "
            "and its claims are part of the record. memory_update the "
            "body first (body edits clear claims), or re-declare "
            "corrected claims by passing claims=[...] on this call."
        )


def _refuse_unverifiable_stored_attestations(
    stored: list[str], origin_root: str | None
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
    """
    if not stored:
        return
    root: str | None = None
    if origin_root is not None:
        try:
            resolved = Path(origin_root).resolve(strict=False)
        except (OSError, ValueError):
            resolved = None
        if resolved is not None and resolved.is_dir():
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
    """
    if attests_paths or attests_absence or declares_claims:
        return
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
    "- `note` (optional, ≤800 chars): what was checked, for the "
    "event log.\n"
    "- `verified_paths` (optional list of strings): the ONLY "
    "attestation the drift legs read — checked against the memory's "
    "own worktree, and the anchor narrowing commit drift. Prefer it "
    "when the memory cites paths. Paths absent here are REFUSED. "
    "Stored entries re-check when `None` preserves them — a vanished "
    "one blocks the stamp; pass a corrected list.\n"
    "- `verified_commits` / `verified_versions` (optional lists): "
    "audit trail only; nothing on the read path resolves them.\n"
    "- `verified_absent_paths` (optional): attest paths "
    "INTENTIONALLY absent here (remote host, other platform, "
    "not-the-location) — reported under `expected_absent`, not "
    "`missing`. Never for real drift.\n"
    "- `claims` (optional): memory_write's claim syntax; checked NOW, "
    "false ⇒ refused. Stored claims re-check on every verify — a "
    "false one blocks the stamp; memory_update first.\n"
    "All five lists are REPLACE, not append — `None` "
    "preserves the prior attestation, `[]` clears it, a populated "
    "list supersedes it. Attest the full set each time. A verify "
    "attesting nothing on a memory whose cited paths resolve is "
    "refused; the error lists them as the paths to attest.\n\n"
    "After memory_update on a memory you later spot-check, verify "
    "again — memory_update clears `last_verified_at` because the "
    "prior verification was for prose that no longer exists.\n\n"
    'Returns `status="stale"` when another agent verified first; '
    "the `hint` says to re-fetch and re-attest.\n\n"
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
    verified_absent_paths: list[str] | None = None,
    claims: list[str] | None = None,
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

    # An attestation naming a path this machine cannot see is not evidence.
    # Before this check, `mark_verified` performed no verification of any
    # kind — it stamped `last_verified_at` and copied the caller's lists
    # verbatim — so a caller could attest a path that never existed and the
    # memory would then read `fresh`, with the freshness resting on nothing.
    # The read side cannot recover this on its own: an ABSOLUTE attested
    # path is only ever existence-checked when the body also names it (see
    # `_normalize_attestations` in `verify.py`), so an attestation the prose
    # never references stays inert forever.
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
    origin_root = snapshot.origin.worktree_root if snapshot.origin else None
    if (
        origin_root is None
        and snapshot.origin is not None
        and snapshot.origin.repo is not None
    ):
        # Legacy record: `origin.repo` was captured before
        # `worktree_root` existed as a field, so the record names its
        # repo but not its tree. When the CALLER is sitting in a
        # checkout of that same repo, that checkout speaks for the
        # memory's tree — the exact trust rule the commit-drift leg
        # applies via `repos_match` before counting the caller's
        # commits against the memory. Without this fallback a
        # pre-worktree memory can never carry claims at all (the 3.40.0
        # backfill measured the population: 8 of 128 repo-matched
        # records), and its attestation existence check stays blind for
        # the same reason. Fallback is read-only derivation — nothing
        # rewrites the stored origin.
        from .. import _handlers as _h

        caller = _h.capture_origin()
        if (
            caller is not None
            and caller.repo is not None
            and caller.worktree_root is not None
            and repos_match(snapshot.origin.repo, caller.repo)
        ):
            origin_root = caller.worktree_root
    if verified_paths:
        unseen = unverifiable_attestations(
            verified_paths,
            worktree_root=origin_root,
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
            list(snapshot.verified_paths), origin_root
        )

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
    # only when the origin worktree isn't visible from this machine
    # (same read-side leniency as attestations: a synced store may
    # legitimately verify prose aspects it cannot stat), never when the
    # tree is present and disagrees.
    normalized_claims: list[str] | None = None
    if claims is not None:
        normalized_claims = (
            _validate_declared_claims(
                claims,
                worktree_root=origin_root,
                surface="memory_verify",
            )
            if claims
            else []
        )
    elif snapshot.claims:
        _refuse_stale_stored_claims(snapshot.claims, origin_root)

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
    )

    # Symbol citations in the body, AST-checked against the memory's own
    # recorded worktree. ADVISORY, and structurally so: the result is
    # attached to this response and read by nothing else. No staleness
    # verdict, no drift leg, no health rollup consumes it, and none may
    # until a benchmark measures its precision on real prose — the reach
    # measurement in `tests/test_symbol_existence.py` is the reason to be
    # cautious, not the reason to be confident.
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
    symbol_drift = check_symbol_citations(snapshot.body, worktree_root=origin_root)

    try:
        memory = deps.store.mark_verified(
            id,
            verified_paths=verified_paths,
            verified_commits=verified_commits,
            verified_versions=verified_versions,
            verified_absent_paths=verified_absent_paths,
            claims=normalized_claims,
            expected_last_verified_at=snapshot.last_verified_at,
            # Both snapshot fields ride the CAS: `last_verified_at`
            # alone is None == None on a never-verified memory, so a
            # concurrent edit between `load_one` above and the store's
            # under-lock compare would let this stamp land on prose
            # the caller never checked. `updated` moves on the edit.
            expected_updated=snapshot.updated,
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
            current_updated=isoformat(exc.current_updated),
        )
        return {
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
        # Genuine disk-level failure in the atomic write path (ENOSPC, EIO,
        # EACCES). Surface as a structured ValueError so the MCP boundary
        # returns a clean "failed to verify memory <id>: …" rather than a bare
        # OSError — mirrors remove.py/restore.py and Store.mark_verified's
        # documented handler-boundary contract.
        raise ValueError(f"failed to verify memory {id}: {exc}") from exc
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


__all__ = ["DESC_MEMORY_VERIFY", "memory_verify"]
