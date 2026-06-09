"""memory_update MCP tool — handler implementation + DESC.

Description-edit history:

- M-U (Round 2): the verification-clearing rule was buried as a
  parenthetical inside the `content` parameter doc. Hoisted to a
  prominent leader paragraph so the verification side-effect lands
  before any parameter detail — it's the most consequential thing a
  caller needs to know about update vs. verify.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._response import isoformat
from ..credentials import find_credential_markers
from ..models import Category, Confidence, _PROPOSABLE_CATEGORIES, validate_scope
from ..store import ConcurrentUpdateError, MemoryNotFoundError, TombstonedError
from ._shared import (
    Context,
    _advance_turn,
    _validate_content_size,
    _validate_scope_count,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


DESC_MEMORY_LINKS_TAIL = (
    " Optional `links` parameter sets the typed inter-memory edge "
    "list. Each entry is a dict with `type` (one of `supersedes`, "
    "`contradicts`, `extends`, `depends_on`), `target_id` (a valid "
    "ULID — the other memory this one relates to), and an optional "
    "`note` (free-form, why the link exists). REPLACE semantics: "
    "pass the full new list, not a delta; pass `links=[]` to clear "
    "all links atomically. Self-links are rejected. Links surface "
    "bidirectionally at retrieval: memory_show on the source "
    "carries `links`; memory_show on the target carries "
    "`reverse_links`. Use `supersedes` when this memory replaces "
    "the target (the retrieval consumer should prefer this one "
    "and demote the target); `contradicts` when both can't be "
    "true (consumer should reconcile via memory_verify); "
    "`extends` when this memory adds nuance to the target; "
    "`depends_on` when this memory only makes sense in the "
    "target's context."
)


DESC_MEMORY_UPDATE = (
    "Body edits clear `last_verified_at`; scope-only edits preserve "
    "it. Bundling a scope rename with a body edit clears verification.\n\n"
    "Refine an existing memory in place. Preferred over "
    "memory_remove + memory_write when correcting a stored fact — "
    "preserves `id`, `created`, and `source`; bumps `updated`.\n\n"
    "Concurrency: under multi-agent contention, two disjoint edits "
    "on the same memory used to silently last-write-wins. The handler "
    "now performs an optimistic-concurrency check against the snapshot "
    "the caller fetched via memory_show. If another agent updated the "
    "memory between your read and write, the response is "
    '`status="stale"` with the current on-disk `updated` timestamp; '
    "re-fetch with memory_show and retry your edit on top of the "
    "current snapshot.\n\n"
    "Parameters (pass at least one):\n"
    "- `id`: required.\n"
    "- `content`: new body. Replacing the body clears "
    "`last_verified_at` and the verified-* attestations (the "
    "prior verification was for prose that no longer exists; "
    "call memory_verify again after).\n"
    "- `scopes` / `links`: REPLACE semantics — pass the full new "
    "list, or `[]` to clear. Scope-only edits preserve "
    "`last_verified_at`.\n"
    "- `confidence`: low / medium / high.\n"
    "- `category`: accepts `fact` and `ambient`. "
    "`user-inference` is REJECTED here — that category exists "
    "to gate WRITES through the pending-confirm flow; updates "
    "have no equivalent gate." + DESC_MEMORY_LINKS_TAIL
)


async def memory_update(
    deps: "ToolHandlers",
    id: str,
    content: str | None = None,
    scopes: list[str] | None = None,
    confidence: str | None = None,
    category: str | None = None,
    links: list[dict[str, Any]] | None = None,
    acknowledge_credential: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    _advance_turn(state, deps.recorder)
    if (
        content is None
        and scopes is None
        and confidence is None
        and category is None
        and links is None
    ):
        raise ValueError(
            "memory_update needs at least one of content, scopes, "
            "confidence, category, or links"
        )
    if content is not None and not content.strip():
        raise ValueError("content must be non-empty if provided")
    if content is not None:
        _validate_content_size(content, deps.config.behavior.max_content_bytes)

    try:
        existing = deps.store.load_one(id)
    except TombstonedError as exc:
        raise ValueError(str(exc)) from exc
    except MemoryNotFoundError as exc:
        raise ValueError(str(exc)) from exc

    new_scopes = existing.scopes
    if scopes is not None:
        if not scopes:
            raise ValueError("scopes must contain at least one entry if provided")
        # Mirror the cap memory_write / episode_write enforce so an update
        # can't smuggle past the handler-boundary ceiling memory_write closes.
        # Without this, retag-then-update would let a caller bypass the
        # configurable cap by writing under-cap then updating to ~2200 scopes,
        # corrupting the YAML frontmatter and erasing the record from every
        # read surface — same silent-data-loss path the takeaway cap closed
        # in t16.
        _validate_scope_count(scopes, deps.config.behavior.max_scopes_per_write)
        new_scopes = [validate_scope(s) for s in scopes]
        if deps.config.scopes.allowed:
            allowed = set(deps.config.scopes.allowed)
            unknown = [s for s in new_scopes if s not in allowed]
            if unknown:
                raise ValueError(
                    f"scope(s) not in allowed list: {unknown}. "
                    f"Allowed: {sorted(deps.config.scopes.allowed)}"
                )

    new_confidence = existing.confidence
    if confidence is not None:
        try:
            new_confidence = Confidence(confidence)
        except ValueError as exc:
            raise ValueError(
                f"confidence must be one of {[c.value for c in Confidence]}"
            ) from exc

    new_category = existing.category
    if category is not None:
        # `user-inference` is a write-time gate (pending-confirm flow);
        # there's no analogous gate on update, so allowing a retag
        # *into* `user-inference` would silently bypass that gate.
        # Allow `fact` and `ambient` only. Sourced from
        # `models._PROPOSABLE_CATEGORIES` — the same closed-protocol
        # whitelist gates the LLM-consolidation validators
        # (`_validate_demote`, `_validate_propose_new` in `llm.py`),
        # which can't supply the user confirmation `user-inference`
        # demands either. Sharing the constant means a future
        # ``Category`` member ships the automation-eligibility
        # decision to one place; silent divergence between this site
        # and the LLM validators can't happen.
        if category not in _PROPOSABLE_CATEGORIES:
            raise ValueError(
                "category must be one of "
                f"{sorted(_PROPOSABLE_CATEGORIES)} on update "
                "(`user-inference` is write-only — it gates the "
                "pending-confirm flow which has no equivalent here)"
            )
        new_category = Category(category)

    new_body = existing.body
    if content is not None:
        new_body = content.strip() + "\n"
        # Credential gate — mirror CredentialGate on the write path so a
        # secret can't be smuggled into the store by EDITING a memory rather
        # than creating one. Only fires on a body edit (content provided);
        # scope/confidence/category/links edits leave the body untouched.
        # The value is redacted from both the response and the event log
        # (kind only), exactly as on the write path.
        credential_hits = find_credential_markers(new_body)
        if credential_hits and not acknowledge_credential:
            deps.recorder.record(
                "update",
                id=id,
                status="credential_warning",
                credential_kinds=[h.kind for h in credential_hits],
            )
            return {
                "status": "credential_warning",
                "markers": [
                    deps.responses.credential_to_dict(h) for h in credential_hits
                ],
                "hint": (
                    "The updated body contains a secret-shaped token (API "
                    "key, private-key PEM, JWT, or a `password=…`-style "
                    "assignment). This store is plain-text and `sync` pushes "
                    "it across hosts via git — describe the secret without "
                    "embedding it, or pass acknowledge_credential=True if the "
                    "value is a documented public/example credential. The "
                    "value is redacted from this warning and the event log "
                    "regardless."
                ),
            }

    # `links` is REPLACE semantics — the caller passes the full new
    # list. Same shape as the `scopes` parameter: simpler than
    # diffing add/remove, and lets the caller atomically clear all
    # links with `links=[]`. None means "leave existing links
    # unchanged".
    new_links = existing.links
    if links is not None:
        from ..models import MemoryLink as _MemoryLink

        parsed_links: list[_MemoryLink] = []
        for i, entry in enumerate(links):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"links[{i}] must be a dict with 'type' and 'target_id'"
                )
            try:
                parsed_links.append(_MemoryLink.model_validate(entry))
            except (ValueError, KeyError) as exc:
                raise ValueError(f"links[{i}] invalid: {exc}") from exc
            if parsed_links[-1].target_id == id:
                raise ValueError(
                    f"links[{i}].target_id cannot equal the memory's own id "
                    f"(self-links are incoherent)"
                )
        # Mirror Memory._check_links's 64-entry cap. `model_copy(update=...)`
        # below SKIPS field validators, so without this check an over-cap
        # links list writes to disk as status="committed" but then SILENTLY
        # VANISHES from every read surface: _load_path re-validates through the
        # Memory(...) ctor, hits the cap ValueError, and load_all/load_one
        # catch-and-skip the now-unparseable record. Same model_copy-skips-
        # validators bypass already guarded for scopes (_validate_scope_count
        # above) and verified_* (_MAX_VERIFIED_ENTRIES in handlers/verify.py).
        if len(parsed_links) > 64:
            raise ValueError(
                f"links list capped at 64 entries (got {len(parsed_links)})"
            )
        new_links = parsed_links

    # When `content` changes, the prior verification was for prose
    # that no longer exists — reset `last_verified_at` to None so the
    # caller has to re-confirm against the new body. The structured
    # attestation lists (`verified_paths`, `verified_commits`,
    # `verified_versions`) were also attached to the prior prose and
    # would lie about the new body — clear them in lockstep so the
    # staleness rollup doesn't read e.g. `verified_paths=["/etc/foo"]`
    # against text that no longer mentions `/etc/foo`. Scope/confidence/
    # category/links edits don't touch the body's claims, so the
    # verification stays intact for those. This matches the intuition
    # that verification is a property of body content, not of metadata.
    update_fields: dict[str, Any] = {
        "body": new_body,
        "scopes": new_scopes,
        "confidence": new_confidence,
        "category": new_category,
        "links": new_links,
    }
    if content is not None:
        update_fields["last_verified_at"] = None
        update_fields["verified_paths"] = []
        update_fields["verified_commits"] = []
        update_fields["verified_versions"] = []

    merged = existing.model_copy(update=update_fields)
    try:
        # Metadata-only edits (content is None) must not clobber a verify
        # that landed concurrently: `merged` carries this handler's pre-read
        # snapshot of last_verified_at / verified_*, but a parallel
        # memory_verify bumps last_verified_at WITHOUT bumping `updated`, so
        # Store.update's `updated` CAS would pass and the stale snapshot would
        # win. preserve_verification keeps the freshest on-disk verification.
        # Content edits already reset those fields above, so they don't.
        updated = deps.store.update(merged, preserve_verification=content is None)
    except ConcurrentUpdateError as exc:
        # W2: another agent landed an update between this handler's
        # `load_one` snapshot above and the under-lock CAS in
        # `Store.update`. The handler doesn't auto-retry — the caller's
        # disjoint edit may now conflict with the winner's change in a
        # way only the caller can reconcile (e.g. both edited the same
        # sentence). Surface as a structured `status="stale"` payload
        # mirroring the other soft-refusal shapes in `write.py`
        # (`scope_mismatch`, `transient_warning`, …) so a programmatic
        # caller can branch on the status and retry without parsing
        # a stringified exception. The current on-disk `updated` is
        # carried so the retry skips a redundant memory_show round
        # trip if the caller wants to fast-path the rebase.
        deps.recorder.record(
            "update",
            status="stale",
            id=exc.memory_id,
            current_updated=isoformat(exc.current_updated),
        )
        return {
            "status": "stale",
            "memory_id": exc.memory_id,
            "current_updated": isoformat(exc.current_updated),
            "hint": (
                "Memory was updated concurrently. Re-fetch with "
                "memory_show and retry your edit on top of the "
                "current snapshot."
            ),
        }
    except OSError as exc:
        # A genuine disk-level failure in the atomic write path (ENOSPC on
        # the tmp write/rename, EIO, EACCES). Surface as a structured
        # ValueError so the MCP tool boundary returns a clean "failed to
        # update memory <id>: …" rather than leaking a bare OSError — matches
        # what handlers/remove.py and handlers/restore.py do, and what
        # Store.update's docstring promises the handler boundary does.
        raise ValueError(f"failed to update memory {id}: {exc}") from exc
    fields_changed = [
        name
        for name, value in (
            ("content", content),
            ("scopes", scopes),
            ("confidence", confidence),
            ("category", category),
            ("links", links),
        )
        if value is not None
    ]
    deps.recorder.record(
        "update",
        id=updated.id,
        fields=fields_changed,
        scopes=updated.scopes,
        confidence=updated.confidence.value,
        category=updated.category.value if updated.category is not None else None,
    )
    return deps.responses.committed(updated)


__all__ = ["DESC_MEMORY_LINKS_TAIL", "DESC_MEMORY_UPDATE", "memory_update"]
