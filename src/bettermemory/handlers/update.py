"""memory_update MCP tool — handler implementation + DESC.

Description-edit history:

- M-U (Round 2): the verification-clearing rule was buried as a
  parenthetical inside the `content` parameter doc. Hoisted to a
  prominent leader paragraph so the verification side-effect lands
  before any parameter detail — it's the most consequential thing a
  caller needs to know about update vs. verify.
- User-claim gate: one clause on the `content` bullet naming the new
  `user_claim_warning` status and the one category it does not fire
  on. Status name only — the remedy stays in the gate's own hint,
  which is the split the description budget's slack is reserved for.
- `acknowledge_user_claim` (this commit): the clause above gained the
  override's name and the one condition that licenses it, because a
  refusal whose only escape is a parameter the description never names
  is one a caller cannot find. Paid for in the same edit by deleting
  " Scope-only edits preserve `last_verified_at`." from the `scopes`
  bullet — the leader paragraph's own first sentence already says
  "scope-only edits preserve it", four lines above it in this same
  string, so the bullet was teaching a rule the description had
  already taught. Measured: 25,869 -> 25,890 of the 26,000 lean
  ceiling, still under the 25,900 pressure line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._response import isoformat
from ..credentials import find_credential_markers
from ..durability import find_transient_markers
from ..models import (
    Category,
    Confidence,
    _PROPOSABLE_CATEGORIES,
    looks_truncated,
    validate_scope,
)
from ..store import ConcurrentUpdateError, MemoryNotFoundError, TombstonedError
from ._shared import (
    Context,
    _advance_turn,
    _validate_content_size,
    _validate_scope_count,
)
from .write import _find_user_claims

if TYPE_CHECKING:
    from .._handlers import ToolHandlers


# Deliberately a type INDEX, not a manual. Picking the right edge type is
# the only part a model cannot infer from the schema, so the four glosses
# stay; the mechanics it used to restate (REPLACE semantics — already on
# the `scopes` / `links` bullet above, verbatim — self-link rejection, and
# how links surface at retrieval) moved to docs/api.md's "Inter-memory
# links" section. That reclaimed 658 characters of the always-resident
# description budget, which is what let the truncation gate below ship at
# all; see docs/ROADMAP.md. Re-measure `_DESC_BASELINE` before trimming
# further — this tail is no longer the cheap reclamation it was.
DESC_MEMORY_LINKS_TAIL = (
    " Each `links` entry is `{type, target_id (a ULID), note?}`. The types: "
    "`supersedes` (prefer this over the target), `contradicts` (both cannot "
    "be true), `extends` (adds nuance to it), `depends_on` (only makes sense "
    "in its context). docs/api.md carries the rest."
)


DESC_MEMORY_UPDATE = (
    "Body edits clear `last_verified_at`; scope-only edits preserve "
    "it. Bundling a scope rename with a body edit clears verification.\n\n"
    "Refine an existing memory in place. Preferred over "
    "memory_remove + memory_write when correcting a stored fact — "
    "preserves `id`, `created`, and `source`; bumps `updated`.\n\n"
    "Parameters (pass at least one):\n"
    "- `id`: required.\n"
    "- `content`: new body. Replacing the body clears "
    "`last_verified_at`, the verified-* attestations, and `claims` "
    "(the prior verification was for prose that no longer exists; "
    "call memory_verify again after, re-declaring claims). A body that reads as a claim "
    "ABOUT THE USER returns `user_claim_warning` unless the record "
    "is already `user-inference`; pass `acknowledge_user_claim=True` "
    "if the subject is someone else. A transient-state body returns "
    "`transient_warning`; `acknowledge_transient=True` overrides. "
    "An edit that SHRINKS the body and "
    "leaves it ending mid-sentence returns `truncation_warning`; pass "
    "`acknowledge_truncation=True` when the cut is deliberate.\n"
    "- `scopes` / `links`: REPLACE semantics — pass the full new "
    "list, or `[]` to clear.\n"
    "- `confidence`: low / medium / high.\n"
    "- `category`: accepts `fact` and `ambient`. "
    "`user-inference` is REJECTED here — that category exists "
    "to gate WRITES through the pending-confirm flow; updates "
    "have no equivalent gate.\n\n"
    'Returns `status="stale"` when another agent updated the '
    "memory first; the `hint` says to re-fetch and retry." + DESC_MEMORY_LINKS_TAIL
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
    acknowledge_transient: bool = False,
    acknowledge_user_claim: bool = False,
    acknowledge_truncation: bool = False,
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
            # Enforced against what this edit INTRODUCES, not the whole list.
            # `scopes` is REPLACE semantics, so keeping a scope the record
            # already carries means resubmitting it — and `[scopes] allowed`
            # is a policy about what a caller may scope a memory to, never a
            # retroactive sweep of what is already stored (nothing rescans the
            # store when the list is tightened; `memory_rename_scope` checks
            # `new_scope` alone, and on `memory_write` the whole list IS the
            # delta — this was the one surface where the two differed).
            # Checking the whole list froze every row whose scopes a tool
            # stamped itself: ingest exempts its own provenance scope and type
            # tag (`_scope_allowlist_reason`, ingest.py) because the user never
            # typed them, and an update that resubmitted them was then refused
            # — so the only way to re-tag an imported row was to drop the
            # provenance stamp the exemption exists to preserve.
            #
            # Keyed on the delta rather than on ingest's stamp names so the
            # exemption can't be borrowed: `imported-from-claude-code` is
            # still refused when ADDED to a memory that was never imported,
            # because a scope absent from the record is a scope this caller
            # is introducing.
            already_present = set(existing.scopes)
            unknown = [
                s for s in new_scopes if s not in allowed and s not in already_present
            ]
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
    # Default for the metadata-only / clean-body paths; a body edit that
    # overrides the credential gate below replaces this. Recorded on the
    # success event so the field is never ABSENT — the too-loose-detector
    # override-rate signal and a forensic `grep credentials_acknowledged`
    # sweep both depend on the update surface logging it like write.py does.
    credentials_acknowledged: list[str] = []
    # Same contract for the user-claim gate's override, for the same reason:
    # the field is the only evidence a too-loose claim detector would ever be
    # revisited on (`UserClaimGate`'s docstring makes override-rate telemetry
    # the entry ticket), so the success event carries it on every path rather
    # than only on the acknowledged one.
    user_claims_acknowledged: list[str] = []
    # Third gate on the same axis, and the field is present on every path for
    # the same reason: the override rate is the only evidence that would ever
    # reopen the shrink-and-mid-sentence predicate. A bool rather than a list
    # because, unlike the other two, there is nothing to enumerate — the body
    # either reads cut off or it does not.
    truncation_acknowledged = False
    # Fourth gate's override evidence, spelled `markers_acknowledged` because
    # that is the field the write path already records and health.py already
    # consumes — one grep covers both surfaces.
    markers_acknowledged: list[str] = []
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
        # Passed the gate on a body edit → the body is clean OR the caller
        # overrode with acknowledge_credential. Capture which detector KINDS
        # the override waved through (empty otherwise) so the success event
        # feeds the same override-rate signal as write.py:638 / proposals.py:164.
        # Kind only, never the value — same redaction contract as the warning.
        credentials_acknowledged = (
            [h.kind for h in credential_hits]
            if credential_hits and acknowledge_credential
            else []
        )
        # Transient-marker gate — the last of memory_write's body gates
        # without an update mirror: a body the write path hard-refuses as
        # transient state ("currently", "as of <date>") could be committed
        # by EDITING an existing record. Sits after the credential gate for
        # the reason the user-claim gate does — a secret is refused before
        # any other gate records body-derived data (here, marker phrases)
        # in the event log. Same markers, same escape, same hint as the
        # write side, so the two surfaces refuse and release identically.
        transient_hits = find_transient_markers(new_body)
        if transient_hits and not acknowledge_transient:
            deps.recorder.record(
                "update",
                id=id,
                status="transient_warning",
                markers=[h.marker for h in transient_hits],
            )
            return {
                "status": "transient_warning",
                "markers": [
                    deps.responses.transient_to_dict(h) for h in transient_hits
                ],
                "hint": (
                    "The updated body contains transient-state markers that "
                    "won't be true in a week. Either rephrase to the durable "
                    "level-up version (extract the architectural decision, "
                    "the why, what-was-built — discard the timestamp/state) "
                    "or pass acknowledge_transient=True if the marker is "
                    "genuinely durable in context."
                ),
            }
        markers_acknowledged = (
            [h.marker for h in transient_hits]
            if transient_hits and acknowledge_transient
            else []
        )
        # User-claim gate — mirror `UserClaimGate` (handlers/write.py) so a
        # claim ABOUT THE USER can't reach a `fact` / `ambient` record by
        # EDITING one. Same laundering shape the credential gate above closes
        # for secrets: `memory_write` hard-refuses this body, so without the
        # mirror a caller writes an innocuous body, updates it to the claim,
        # and the pending/veto handshake whose entire purpose is the user's
        # veto never runs. Runs AFTER the credential gate for the reason the
        # write chain orders them that way — a secret is refused before any
        # other gate records body-derived data (here, `claim_phrases`) in the
        # event log.
        #
        # Judged against the category the record will HAVE after this edit,
        # which is what `UserClaimGate` reads off the write payload. That can
        # only be `user-inference` when the record already was one, since the
        # retag INTO that category is refused above — and that is exactly the
        # structural escape this gate wants: a claim about the user belongs in
        # a `user-inference` memory, and only `memory_write` can create one,
        # staged so the user gets the veto. A legacy `category=None` record is
        # gated, matching the runtime's fact-default reading of that field.
        #
        # `acknowledge_user_claim` mirrors the write path's escape for a body
        # whose subject is someone or something else ("Black prefers double
        # quotes"). It landed one commit late, and the gap was not cosmetic:
        # `_find_user_claims` ORs in `_PREFERENCE_RE`, whose `we (?:use|prefer|
        # avoid|always|never)` branch is case-insensitive, so an ordinary
        # project memory ("We use ruff for linting in this repo.") tripped the
        # refusal. That body is writable — `memory_write(...,
        # acknowledge_user_claim=True)` commits it — so without this parameter
        # there was a body you could CREATE and then could not EDIT into an
        # existing record by any route, while `acknowledge_user_claim=True`
        # passed to this tool was dropped as an unknown argument and the
        # refusal came back anyway, with nothing saying the flag did nothing.
        # The parameter has to reach the `ToolHandlers.memory_update` facade in
        # `_handlers.py` to be on the wire at all — the served schema is built
        # from THAT signature, so a handler-only parameter is silently dropped
        # at call time. That is the same failure mode
        # `tests/test_resident_footprint.py`'s landed-parameter check guards
        # for the three parameters IT names; this one is guarded by
        # `test_the_override_is_served_on_the_wire_defaulting_to_off` in
        # `tests/test_update_user_claim_gate.py`, which asserts against the
        # served schema rather than this signature.
        #
        # Scanned only when the record will NOT be `user-inference` — the
        # empty list on that branch is what makes the rest of this block
        # read the same either way, rather than leaving `claim_hits`
        # conditionally undefined for the override accounting below.
        claim_hits = (
            _find_user_claims(new_body)
            if new_category != Category.USER_INFERENCE
            else []
        )
        if claim_hits and not acknowledge_user_claim:
            deps.recorder.record(
                "update",
                id=id,
                status="user_claim_warning",
                category=(new_category.value if new_category is not None else None),
                claim_phrases=[h.phrase for h in claim_hits],
            )
            return {
                "status": "user_claim_warning",
                "markers": [
                    {"phrase": h.phrase, "sentence": h.sentence} for h in claim_hits
                ],
                "hint": (
                    "The updated body reads as a claim ABOUT THE USER, "
                    "but this memory is filed as "
                    f"`{(new_category or Category.FACT).value}`, so the "
                    "edit would commit without asking them. "
                    "Misattribution sticks, so the user gets the veto: "
                    "file the claim with memory_write and "
                    "category='user-inference', which stages it and "
                    "returns a pending_id so you can ask in plain "
                    "language first. Retagging this record into "
                    "`user-inference` is not available — that is the "
                    "write-time gate. When the subject is someone or "
                    "something else (a teammate, a tool that 'prefers' "
                    "a setting), re-issue this same memory_update with "
                    "acknowledge_user_claim=True."
                ),
            }
        # Passed the gate on a body edit → the body reads clean OR the caller
        # overrode. Which phrases the override waved through, on the same
        # override-rate axis `credentials_acknowledged` rides above and under
        # the field name the write path already uses for it
        # (`user_claims_acknowledged`, handlers/write.py) — one grep has to
        # find both surfaces or the telemetry is per-surface trivia.
        user_claims_acknowledged = (
            [h.phrase for h in claim_hits]
            if claim_hits and acknowledge_user_claim
            else []
        )
        # Truncation gate. `looks_truncated` has shipped as DETECTION since
        # 3.x — `doctor`'s `memory_body_completeness` reports bodies that end
        # mid-sentence — but detection runs after the tail is already gone and
        # the store has no older copy, so the report names a loss it cannot
        # undo. This is the same predicate moved to the one moment both bodies
        # are in hand.
        #
        # The SHRINK conjunct is what makes it a gate rather than a nuisance.
        # `looks_truncated` alone is 0.4% false positive on the maintainer's
        # 234-record store — cheap for a report, but it would fire on every
        # edit to a body that legitimately ends on a bare identifier or list
        # item, forever, including edits that only GREW it. Requiring the edit
        # to also make the record shorter narrows it to the shape the incident
        # actually had: a rewrite that arrived cut off. A deliberate condensing
        # edit that lands on a terminal character never sees this.
        #
        # Rejected alternative, recorded so it is not re-derived: "new body is
        # a strict prefix of the old" is 0% false positive but misses the
        # motivating incident (a rewrite that got cut, not a prefix), and
        # ">30% shorter" false-positives on condensing edits, the single most
        # common update shape on the dogfood store.
        if (
            len(new_body.strip()) < len(existing.body.strip())
            and looks_truncated(new_body)
            and not acknowledge_truncation
        ):
            # Lengths only, never body text — same redaction discipline the
            # credential gate above keeps, since a truncated body is exactly
            # as likely to carry a secret as any other.
            deps.recorder.record(
                "update",
                id=id,
                status="truncation_warning",
                previous_length=len(existing.body.strip()),
                new_length=len(new_body.strip()),
            )
            return {
                "status": "truncation_warning",
                "previous_length": len(existing.body.strip()),
                "new_length": len(new_body.strip()),
                "ends_with": new_body.strip()[-60:],
                "hint": (
                    "This edit makes the body shorter AND leaves it ending "
                    "mid-sentence, which is what a body truncated in transit "
                    "looks like. The store keeps no older copy, so if the tail "
                    "was lost it is unrecoverable once this commits. Re-send "
                    "the complete body, or pass acknowledge_truncation=True if "
                    "the record genuinely ends there (a list item or a bare "
                    "identifier is a legitimate ending)."
                ),
            }
        # Past the gate → the edit is benign OR the caller overrode. True
        # only when the gate WOULD have fired but for the flag — all three
        # conjuncts, the exact complement of the refusal above, the same
        # relationship the two acknowledged lists keep with their gates. A
        # flag re-passed defensively on an edit the gate could not refuse
        # (grew, or ends terminal) is not an override and must not inflate
        # the rate this predicate's reopening decision reads.
        truncation_acknowledged = (
            len(new_body.strip()) < len(existing.body.strip())
            and looks_truncated(new_body)
            and bool(acknowledge_truncation)
        )

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
    # `verified_versions`, `verified_absent_paths`) were also attached
    # to the prior prose and would lie about the new body — clear them
    # in lockstep so the staleness rollup doesn't read e.g.
    # `verified_paths=["/etc/foo"]` against text that no longer mentions
    # `/etc/foo` (or keep suppressing a missing-flag the new body's
    # citation deserves). Scope/confidence/category/links edits don't
    # touch the body's claims, so the verification stays intact for
    # those. This matches the intuition that verification is a property
    # of body content, not of metadata.
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
        update_fields["verified_absent_paths"] = []
        # `claims` clear on the same trigger and for the same reason: a
        # claim declares what the BODY asserts, and this is a different
        # body. The declare-time oracle only ever checked them against
        # the prose that existed then; re-declare via memory_verify
        # (claims=[...]) once the new body's assertions are known.
        update_fields["claims"] = []

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
        credentials_acknowledged=credentials_acknowledged,
        user_claims_acknowledged=user_claims_acknowledged,
        truncation_acknowledged=truncation_acknowledged,
        markers_acknowledged=markers_acknowledged,
    )
    return deps.responses.committed(updated)


__all__ = ["DESC_MEMORY_LINKS_TAIL", "DESC_MEMORY_UPDATE", "memory_update"]
