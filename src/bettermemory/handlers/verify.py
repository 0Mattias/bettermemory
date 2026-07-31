"""memory_verify MCP tool — bump `last_verified_at` after spot-checking."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._response import isoformat, isoformat_optional
from ..store import ConcurrentUpdateError, MemoryNotFoundError, TombstonedError
from ..symbols import check_symbol_citations
from ..verify import unverifiable_attestations
from ._shared import Context, _NOTE_MAX_LEN, _advance_turn

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
    "- `verified_paths` (optional list of strings): the ONLY "
    "attestation the drift legs read — checked against the memory's "
    "own worktree, and the anchor narrowing commit drift. Prefer it "
    "when the memory cites paths. Paths absent here are REFUSED.\n"
    "- `verified_commits` / `verified_versions` (optional lists): "
    "audit trail only; nothing on the read path resolves them.\n"
    "- `verified_absent_paths` (optional): attest paths "
    "INTENTIONALLY absent here (remote host, other platform, "
    "not-the-location) — reported under `expected_absent`, not "
    "`missing`. Never for real drift.\n"
    "All four `verified_*` lists are REPLACE, not append — `None` "
    "preserves the prior attestation, `[]` clears it, a populated "
    "list supersedes it. Attest the full set each time.\n\n"
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
    # `Store.mark_verified`'s.
    origin_root = snapshot.origin.worktree_root if snapshot.origin else None
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
