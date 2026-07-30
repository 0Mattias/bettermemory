"""memory_write MCP tool — orchestrator + WriteGate strategy.

Pre-Round-2 ``memory_write`` was a 356-line method with six sequential
guard blocks inline (durability / scope-mismatch / groundedness /
dedup-active / dedup-tombstone / pending). Each block built a
short-circuit response dict and recorded its own event before
returning. The function read as a single long ladder; understanding any
one gate required scrolling past the others.

Round 2 extracts each gate into a small class (``WriteGate``
subclasses below) whose ``evaluate(payload, ...)`` method returns one
of:

- ``Reject`` — short-circuit with this response dict and recorder
  event. The orchestrator records the event and returns.
- ``Pending`` — stage the write through the SessionState and return a
  pending response.
- ``Continue`` — gate passed; move to the next.

The orchestrator (``memory_write`` below) holds the dependency
references, runs the gates in order, and falls through to the actual
``Store.write`` on the first gate that returns ``Continue`` through
the whole chain. Each gate stays under 40 lines and reads like a
self-contained policy decision; the orchestrator is the readable
sequence.

WriteGate decision: kept as a single file (this one) rather than
``handlers/write/<gate>.py``. The gates are small (10-40 lines each),
share half a dozen helpers, and reading them one after the other in
declaration order matches how they fire at runtime — splitting them
one-per-file would hide that runtime order behind a directory
listing.

Includes ``memory_write_confirm`` / ``memory_write_cancel`` because
those tools complete the pending-write lifecycle this module owns.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..credentials import find_credential_markers
from ..durability import find_transient_markers
from ..models import Category, SimilarHit
from ..proposals import (
    _HARD_WRAP_RE,
    _LIST_PREFIX_RE,
    _PREFERENCE_RE,
    _SENTENCE_SPLIT_RE,
    _SMART_APOSTROPHES,
)
from ..scope_match import (
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)
from ..search import find_similar, find_similar_tombstones
from ..session import GATE_FLAG_KEYS, PendingWrite, SessionState
from ._shared import (
    Context,
    _AMBIENT_LONG_BODY_WORDS,
    _advance_turn,
    _maybe_attach_curation_hint,
    _validate_write_payload,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from .._response import ResponseBuilder
    from ..config import Config
    from ..store import Store

log = logging.getLogger("bettermemory.handlers.write")


# ---------------------------------------------------------------------------
# Description constants
# ---------------------------------------------------------------------------


DESC_MEMORY_WRITE = (
    "Create a new memory. Call PROACTIVELY when something durable "
    "enters the conversation — aggressive writing is safe; the "
    "guardrails below catch bad writes. Trigger→category mapping: "
    "the server `instructions` block.\n\n"
    "Parameters:\n"
    "- `content`: the memory body.\n"
    "- `scopes`: non-empty list. Avoid the catch-all 'general'; "
    "prefer narrow tags like `tools`, `infrastructure`, "
    "`projects:<name>`, `learning-style`.\n"
    "- `category` (default 'fact'): one of `fact`, "
    "`user-inference`, `ambient`.\n"
    "  - `fact`: project / infra / reference / tooling. Commits "
    "immediately (unless `require_write_confirmation`).\n"
    "  - `user-inference`: claims ABOUT THE USER. Always returns "
    "{status:'pending', pending_id} regardless of config — ask "
    "the user in plain language, then memory_write_confirm or "
    "memory_write_cancel. Misattribution sticks; user gets the "
    "veto.\n"
    "  - `ambient`: atmospheric context that shapes replies "
    "without being cited. Commits like fact but excluded from "
    "dead-weight curation; long bodies (>500 words) attach a "
    "non-blocking `ambient_body_long` warning.\n"
    "- `confidence` ('low' / 'medium' / 'high'), `source` "
    "('explicit-statement' / 'inferred').\n"
    "- `groundedness_check=True` + `source_transcript`: optional "
    "gate. Sentences with <30% token overlap to the transcript "
    "return {status:'ungrounded', claims:[…]}. Override via "
    "`acknowledge_ungrounded=True` when you have grounding sources "
    "outside the transcript (file reads, tool results). Off by "
    "default; opt in for a paper trail.\n\n"
    "Return statuses:\n"
    "- `committed` — write succeeded; payload carries the new id "
    "and `related` medium-overlap matches.\n"
    "- `transient_warning` — durability marker detected "
    "('currently', 'today I', 'we just', etc.). Extract the level-up "
    "durable form (the decision, the why) or pass "
    "`acknowledge_transient=True` (rare).\n"
    "- `credential_warning` — the body embeds a secret-shaped "
    "token (API key, private-key PEM, JWT, `password=…`). Describe "
    "the secret instead of storing it, or pass "
    "`acknowledge_credential=True`.\n"
    "- `duplicate` — content dedup fired; the matched memory is "
    "credited a corroboration (`corroboration_recorded: true`, once "
    "per session) — recurrence is evidence. Prefer memory_update "
    "on the matched id; `force=True` only when meaningfully "
    "different.\n"
    "- `previously_removed` — overlap with a tombstone; inspect "
    "`removed_reason`. If the rejection still applies, drop the "
    "write; if the fact is now correct, memory_restore the "
    "tombstone instead of a parallel entry.\n"
    "- `scope_mismatch` — body cites a project the declared "
    "scopes don't cover. Re-scope or pass "
    "`acknowledge_scope_mismatch=True`.\n"
    "- `user_claim_warning` — the body reads as a claim ABOUT THE "
    "USER but `category` isn't `user-inference`. Re-issue as that "
    "(the user gets the veto) or pass `acknowledge_user_claim=True` "
    "if the subject is someone else.\n"
    "- `pending` — `category='user-inference'` or "
    "`require_write_confirmation`. `pending_reason` distinguishes.\n"
    "- `ungrounded` — groundedness gate fired.\n\n"
    "A `committed` or `memory_write_confirm` response may inline a "
    "one-shot per-session `curation_hint` block when "
    "`dead_weight + drifted + cold_endorsement_memories` pressure "
    "crosses the configured threshold. Shape: `{pressure, threshold, "
    "counts: {dead_weight, drifted, cold_endorsement_memories}, "
    "message}`. Passive notification — call `memory_health` for "
    "full buckets, `memory_remove` / `memory_verify` to resolve."
)


DESC_MEMORY_WRITE_CONFIRM = (
    "Commit a memory_write that returned status='pending'. "
    "Pass the pending_id from that response. Pending writes expire "
    "after 1 hour; the confirm call will tell you which case fired "
    "(expired vs. never-existed). Re-gated at commit: `duplicate` / "
    "`previously_removed` / `credential_warning` can return instead of "
    "`committed` when the store changed during the wait. The staged "
    "write survives (`pending_retained: true`, same pending_id) — "
    "memory_write_cancel or resolve the match. The original write's "
    "overrides carry over."
)


DESC_MEMORY_WRITE_CANCEL = (
    "Drop a pending memory_write without committing. "
    "Pass the pending_id from the original write response. "
    "Pending writes expire after 1 hour; if the TTL elapsed (or the "
    "id never existed) the call returns `existed=False`."
)


# ---------------------------------------------------------------------------
# WriteGate strategy
# ---------------------------------------------------------------------------


@dataclass
class GateContext:
    """Bundle of inputs every gate evaluates against.

    The orchestrator builds one of these per call (post-payload-
    validation, post-origin-capture) and threads it through the gate
    chain. Mutable state (`related`, `removed_related`,
    `transient_hits`) is set by earlier gates so later gates can
    surface their findings on the eventual response — `pending`
    needs the related lists, the commit path needs `transient_hits`
    so the override-rate event field can carry the acknowledged
    markers.
    """

    payload: dict[str, Any]
    force: bool
    acknowledge_transient: bool
    acknowledge_scope_mismatch: bool
    acknowledge_ungrounded: bool
    acknowledge_credential: bool
    groundedness_check: bool
    source_transcript: str | None
    # Defaulted so the non-MCP construction sites keep working: `ingest`
    # passes every field by keyword and has no `**kwargs` slack, so a
    # field added without a default breaks that caller at import-time
    # rather than at review-time.
    acknowledge_user_claim: bool = False
    # Outputs the gates accumulate as they pass — read by later gates
    # or the final commit step.
    credential_hits: list[Any] = None  # type: ignore[assignment]
    transient_hits: list[Any] = None  # type: ignore[assignment]
    user_claim_hits: list[Any] = None  # type: ignore[assignment]
    related: list[SimilarHit] = None  # type: ignore[assignment]
    removed_related: list[SimilarHit] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.credential_hits is None:
            self.credential_hits = []
        if self.transient_hits is None:
            self.transient_hits = []
        if self.user_claim_hits is None:
            self.user_claim_hits = []
        if self.related is None:
            self.related = []
        if self.removed_related is None:
            self.removed_related = []


@dataclass
class Continue:
    """Gate passed — move to the next gate, or commit if last."""


@dataclass
class Reject:
    """Gate refused — short-circuit with this response.

    `event_kwargs` go straight into ``recorder.record("write", …)``
    so the audit log captures the rejection cause; `response` is the
    dict the handler returns to the caller.
    """

    response: dict[str, Any]
    event_kwargs: dict[str, Any]


@dataclass
class Pending:
    """Special-case ``Continue`` that stages the write through the
    SessionState rather than committing inline."""

    pending_reason: str


GateResult = Continue | Reject | Pending


class WriteGate:
    """Common base; subclasses override ``evaluate``."""

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        raise NotImplementedError


class CredentialGate(WriteGate):
    """Secret-shaped-token check — reject bodies that embed a credential
    unless `acknowledge_credential`.

    Runs FIRST, before every other gate: the store is plain-text markdown
    that syncs across hosts, so persisting a live secret leaks it to disk,
    the audit log, and every clone — the highest-severity write to refuse,
    and refusing early means no later gate's event ever records body-derived
    data alongside the secret. The warning and the event log carry only the
    detector `kind` and a redacted snippet, never the value.
    """

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        gc.credential_hits = find_credential_markers(gc.payload["content"])
        if not gc.credential_hits or gc.acknowledge_credential:
            return Continue()
        return Reject(
            response={
                "status": "credential_warning",
                "markers": [
                    deps.responses.credential_to_dict(h) for h in gc.credential_hits
                ],
                "hint": (
                    "The body contains a secret-shaped token (API key, "
                    "private-key PEM, JWT, or a `password=…`-style "
                    "assignment). This store is plain-text and `sync` "
                    "pushes it across hosts via git — describe the secret "
                    "without embedding it (e.g. 'the deploy uses an AWS key, "
                    "stored in 1Password'), or pass "
                    "acknowledge_credential=True if the value is a "
                    "documented public/example credential. The value is "
                    "redacted from this warning and the event log regardless."
                ),
            },
            event_kwargs={
                "status": "credential_warning",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "credential_kinds": [h.kind for h in gc.credential_hits],
            },
        )


class TransientGate(WriteGate):
    """Durability check — reject bodies with transient-state markers
    unless `acknowledge_transient`.

    Runs FIRST: a transient body shouldn't become a duplicate of an
    existing transient memory, since the right move is to fix the
    body rather than route to memory_update on an unsalvageable
    parent. Catch transience before dedup so the rejection happens
    on the most actionable axis.
    """

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        gc.transient_hits = find_transient_markers(gc.payload["content"])
        if not gc.transient_hits or gc.acknowledge_transient:
            return Continue()
        return Reject(
            response={
                "status": "transient_warning",
                "markers": [
                    deps.responses.transient_to_dict(h) for h in gc.transient_hits
                ],
                "hint": (
                    "The body contains transient-state markers that won't "
                    "be true in a week. Either rephrase to the durable "
                    "level-up version (extract the architectural decision, "
                    "the why, what-was-built — discard the timestamp/state) "
                    "or pass acknowledge_transient=True if the marker is "
                    "genuinely durable in context."
                ),
            },
            event_kwargs={
                "status": "transient_warning",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "markers": [h.marker for h in gc.transient_hits],
            },
        )


# Third-person user claims — the shape a MODEL writes when it files a
# claim about the user ("Mattias prefers tabs", "the user avoids
# rebase"). `_PREFERENCE_RE` cannot see these: it mines the USER's own
# words in the Stop hook, so every one of its branches is first-person.
# Composed alongside it rather than folded into it — the extractor is
# behaviourally pinned (tests/test_proposals.py) on exactly which shapes
# it captures, and widening it there would change what the hook queues.
#
# Deliberately NOT re.IGNORECASE: the bare-subject branch reads the
# capital as the only available "this is a person" signal, and
# `[A-Z][a-z]+` misses acronyms and CamelCase ("CI", "GitHub") by
# construction — those head infrastructure claims, not user claims. The
# `(?<!ly)` drops sentence-opening adverbs ("Allegedly hates dark
# mode"). Case-insensitive branches carry their own scoped `(?i:)`.
#
# The bare-subject branch takes only verbs that predicate a PERSON:
# "uses" / "runs" / "wants" / "needs" are how ordinary tooling facts
# read ("Postgres runs on 5433", "Docker needs the daemon"), so those
# are admitted only under the explicit `the user` subject. The
# possessive branch mirrors `_PREFERENCE_RE`'s `^(?:my|our)` shape —
# a stative verb within four words — because a bare "the user's X"
# matches ordinary prose about users in general. Residual false
# positives ("Black prefers double quotes") are what
# `acknowledge_user_claim` is for; the override rate in the write event
# is the evidence that would reopen this list.
_USER_CLAIM_RE = re.compile(
    r"(?i:\bthe user\b)\s+(?:(?:always|never|usually|typically|generally)\s+)?"
    r"(?i:(?:prefers|likes|dislikes|loves|hates|avoids|wants|needs|uses|runs"
    r"|works|lives|is|was|has)\b)"
    r"|(?i:\bthe user's\s+(?:\w+\s+){0,4}?"
    r"(?:is|are|was|were|prefers?|uses?|runs?|lives)\b)"
    r"|\b[A-Z][a-z]+(?<!ly)\b\s+(?:(?:always|never|usually|typically|generally)\s+)?"
    r"(?i:(?:prefers|likes|dislikes|loves|hates|avoids)\b)"
)


@dataclass(frozen=True)
class UserClaimHit:
    """One sentence of a body that reads as a claim about the user."""

    phrase: str
    sentence: str


def _find_user_claims(content: str) -> list[UserClaimHit]:
    """Sentences in `content` that read as claims about the user.

    Applied the way production applies `_PREFERENCE_RE` — per sentence,
    after smart-apostrophe normalization and hard-wrap repair — because
    that pattern's `^(?:my|our)` branch anchors to the start of whatever
    string it is handed, and a curly-apostrophe body ("I’m using zsh")
    misses every contraction branch without the translation.

    No length floor, unlike `proposals._iter_candidate_sentences`: that
    30-char / 6-token floor keeps a noisy REVIEW QUEUE clean, while
    "Mattias prefers tabs" (20 chars, 3 tokens) is precisely the write
    this gate exists to catch.
    """
    hits: list[UserClaimHit] = []
    text = _HARD_WRAP_RE.sub(" ", content.translate(_SMART_APOSTROPHES))
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = _LIST_PREFIX_RE.sub("", raw.strip()).strip()
        if not sentence:
            continue
        match = _PREFERENCE_RE.search(sentence) or _USER_CLAIM_RE.search(sentence)
        if match is not None:
            hits.append(UserClaimHit(phrase=match.group(0), sentence=sentence))
    return hits


class UserClaimGate(WriteGate):
    """Reject bodies that read as claims ABOUT THE USER unless they are
    filed as `user-inference` (or `acknowledge_user_claim` is set).

    `PendingGate` triggers on the category LABEL, so a claim about the
    user written as `category='fact'` commits instantly and the staging
    flow whose entire purpose is the user's veto never runs. This gate
    classifies the BODY instead, which is why it sits next to
    `TransientGate` rather than next to `PendingGate`: it must precede
    dedup (a re-categorized re-issue must not be routed to
    `memory_update` against a mis-filed parent) and precede
    `PendingGate` (re-issuing as `user-inference` has to stage
    normally).

    Precision-first, and porous by the same trade the transient and
    credential gates make: it matches predicating shapes, so a nominalised
    claim like "<Name>'s preference is tabs" passes (measured, both
    apostrophe forms). Widening it to possessive-plus-noun would refuse
    ordinary prose — "the parser's preference is the longest match" — and
    chasing shapes one at a time is whack-a-mole. The entry ticket for
    revisiting the pattern is override-rate telemetry per marker, which is
    why an acknowledged write records the phrase it overrode.
    """

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        category_enum: Category = gc.payload["category"]
        if category_enum == Category.USER_INFERENCE:
            return Continue()
        gc.user_claim_hits = _find_user_claims(gc.payload["content"])
        if not gc.user_claim_hits or gc.acknowledge_user_claim:
            return Continue()
        return Reject(
            response={
                "status": "user_claim_warning",
                "markers": [
                    {"phrase": h.phrase, "sentence": h.sentence}
                    for h in gc.user_claim_hits
                ],
                "hint": (
                    "The body reads as a claim ABOUT THE USER but was "
                    f"filed as `{category_enum.value}`, which commits "
                    "without asking them. Re-issue with "
                    "category='user-inference' — that stages the write "
                    "and returns a pending_id so you can ask in plain "
                    "language first; misattribution sticks, so the user "
                    "gets the veto. Pass acknowledge_user_claim=True "
                    "when the subject is someone or something else (a "
                    "teammate, a tool that 'prefers' a setting)."
                ),
            },
            event_kwargs={
                "status": "user_claim_warning",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "category": category_enum.value,
                "claim_phrases": [h.phrase for h in gc.user_claim_hits],
            },
        )


class ScopeMismatchGate(WriteGate):
    """Reject bodies whose path / project-name citations don't match
    the declared scope list (unless `acknowledge_scope_mismatch`)."""

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        if gc.acknowledge_scope_mismatch:
            return Continue()
        existing_memories = deps.store.load_all()
        mismatch = detect_scope_mismatch(
            body=gc.payload["content"],
            declared_scopes=gc.payload["scopes"],
            project_scopes=collect_project_scopes(existing_memories),
            project_roots=collect_project_roots(existing_memories),
        )
        if not mismatch.has_mismatch:
            return Continue()
        return Reject(
            response={
                "status": "scope_mismatch",
                "matches": [m.to_dict() for m in mismatch.matches],
                "suggested_scopes": list(mismatch.suggested_scopes),
                "hint": (
                    "The body cites paths or project names that suggest "
                    "this memory belongs to a different scope. Either "
                    "add one of `suggested_scopes` to the declared "
                    "scope list, or pass acknowledge_scope_mismatch=True "
                    "if the cross-reference is intentional (e.g. an "
                    "infrastructure note that mentions multiple "
                    "projects by design)."
                ),
            },
            event_kwargs={
                "status": "scope_mismatch",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "suggested_scopes": list(mismatch.suggested_scopes),
                "mismatch_kinds": [m.kind for m in mismatch.matches],
            },
        )


class GroundednessGate(WriteGate):
    """Sentence-level overlap against `source_transcript` — fires only
    when `groundedness_check=True` and a transcript is provided.

    The HaluMem-style write-time grounding check: sentences that
    don't anchor to the conversation come back as "ungrounded". Closes
    the hallucinate-at-write-time failure mode common to systems that
    auto-extract memories from conversation.
    """

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        if not gc.groundedness_check:
            return Continue()
        if gc.source_transcript is None or gc.acknowledge_ungrounded:
            return Continue()
        from ..groundedness import check_groundedness

        ungrounded = check_groundedness(gc.payload["content"], gc.source_transcript)
        if not ungrounded:
            return Continue()
        return Reject(
            response={
                "status": "ungrounded",
                "claims": [c.to_dict() for c in ungrounded],
                "hint": (
                    "The body contains sentences that don't share enough "
                    "vocabulary with the source transcript to count as "
                    "grounded — the model may have hallucinated them, "
                    "or paraphrased so heavily that the audit trail is "
                    "lost. Either rephrase to keep the load-bearing "
                    "tokens close to the transcript, or pass "
                    "`acknowledge_ungrounded=True` if you have other "
                    "grounding sources (a file read, a tool result) "
                    "that aren't represented in this transcript."
                ),
            },
            event_kwargs={
                "status": "ungrounded",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "ungrounded_count": len(ungrounded),
            },
        )


def _resolve_dedup_thresholds(
    deps: "GateDeps",
) -> tuple[Any, float | None, float | None]:
    """Shared setup for both dedup gates: the semantic model plus the
    high/medium overlap thresholds (None unless semantic dedup is on).

    `DedupActiveGate` and `DedupTombstoneGate` run back-to-back in the
    same write chain and need the identical triple; keeping it in one
    place stops the two gates from silently drifting apart.
    """
    # Ask for the model ONLY when this consumer wants it. The factory is
    # shared with retrieval, and retrieval now resolves a model whenever an
    # embeddings extra is installed — so taking whatever the factory hands
    # back would silently switch write-dedup from Jaccard to cosine for
    # anyone who installed the extra to improve SEARCH and never opted into
    # semantic dedup. Worse, the thresholds below stay Jaccard-natural in
    # that case, so it would score cosine against 0.75/0.40 — a similarity
    # scale the numbers were never calibrated for. `semantic_dedup` is this
    # gate's own flag; read it here rather than inferring intent from
    # whether some other consumer caused a load.
    semantic_model = (
        deps._semantic_model_factory(deps.config)
        if deps.config.behavior.semantic_dedup
        else None
    )
    # Gate the COSINE-calibrated thresholds on the RESOLVED model, not on
    # the `semantic_dedup` flag alone. When `semantic_dedup=true` but the
    # embeddings extra isn't installed, the factory returns None (one
    # WARNING) and `find_similar` falls back to the Jaccard scorer — feeding
    # it cosine thresholds (0.85/0.65) would silently neuter dedup, since
    # Jaccard rarely reaches 0.85. Passing None lets `find_similar` pick the
    # Jaccard-natural 0.75/0.40.
    if semantic_model is not None:
        high_threshold: float | None = deps.config.behavior.semantic_high_threshold
        medium_threshold: float | None = deps.config.behavior.semantic_medium_threshold
    else:
        high_threshold = None
        medium_threshold = None
    return semantic_model, high_threshold, medium_threshold


class DedupActiveGate(WriteGate):
    """Content dedup against the active set. High overlap → reject as
    duplicate (the right move is memory_update on the matched id);
    medium overlap → record as `related` for the eventual response.

    Skipped when `force=True`.
    """

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        if gc.force:
            return Continue()
        semantic_model, high_threshold, medium_threshold = _resolve_dedup_thresholds(
            deps
        )
        similar = find_similar(
            gc.payload["content"],
            deps.store.load_all(),
            semantic_model=semantic_model,
            high_threshold=high_threshold,
            medium_threshold=medium_threshold,
        )
        high = [h for h in similar if h.relevance == "high"]
        if high:
            return Reject(
                response={
                    "status": "duplicate",
                    "matches": [deps.responses.similar_to_dict(h) for h in high],
                    "hint": (
                        "An existing memory has high content overlap with "
                        "this write. Prefer memory_update on the matched "
                        "id over creating a parallel entry. Pass force=True "
                        "if the new memory is meaningfully different."
                    ),
                },
                event_kwargs={
                    "status": "duplicate",
                    "scopes": gc.payload["scopes"],
                    "forced": False,
                    "matches": [h.id for h in high],
                },
            )
        gc.related = [h for h in similar if h.relevance == "medium"]
        return Continue()


class DedupTombstoneGate(WriteGate):
    """Tombstone-aware dedup. High overlap with a removed memory →
    `previously_removed` (caller can memory_restore the tombstone
    rather than write a parallel entry); medium → `removed_related`
    for the response.

    Skipped when `force=True`.
    """

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        if gc.force:
            return Continue()
        semantic_model, high_threshold, medium_threshold = _resolve_dedup_thresholds(
            deps
        )
        tombstone_similar = find_similar_tombstones(
            gc.payload["content"],
            deps.store.load_tombstones(),
            semantic_model=semantic_model,
            high_threshold=high_threshold,
            medium_threshold=medium_threshold,
        )
        high_removed = [h for h in tombstone_similar if h.relevance == "high-removed"]
        if high_removed:
            return Reject(
                response={
                    "status": "previously_removed",
                    "removed_matches": [
                        deps.responses.similar_to_dict(h) for h in high_removed
                    ],
                    "hint": (
                        "A previously-removed memory has high content overlap "
                        "with this write. Inspect each `removed_reason` — if "
                        "the rejection still applies, drop the write; if the "
                        "fact is now correct, call memory_restore(id) on the "
                        "tombstone instead of writing a parallel entry. Pass "
                        "force=True to bypass when the new memory is "
                        "meaningfully different from the removed one."
                    ),
                },
                event_kwargs={
                    "status": "previously_removed",
                    "scopes": gc.payload["scopes"],
                    "forced": False,
                    "removed_matches": [h.id for h in high_removed],
                },
            )
        gc.removed_related = [
            h for h in tombstone_similar if h.relevance == "medium-removed"
        ]
        return Continue()


class PendingGate(WriteGate):
    """Stage the write through the SessionState when either the global
    config flag (`require_write_confirmation`) OR
    `category=='user-inference'` requires it. User-inference is
    structurally enforced regardless of config: misattribution sticks
    and the user gets the veto."""

    def evaluate(self, deps: "GateDeps", gc: GateContext) -> GateResult:
        category_enum: Category = gc.payload["category"]
        if deps.config.behavior.require_write_confirmation:
            return Pending(pending_reason="config")
        if category_enum == Category.USER_INFERENCE:
            return Pending(pending_reason="user-inference")
        return Continue()


# Order matters: credential before everything so a secret is refused
# before any other gate records body-derived data in the event log;
# transient before dedup so the writer isn't routed to memory_update on a
# transient parent; user-claim next to transient because both classify the
# BODY, and before dedup for the same reason transient is (a re-categorized
# re-issue must not be routed to memory_update on a mis-filed parent) and
# before pending so re-issuing as user-inference stages normally;
# scope-mismatch before dedup so the writer doesn't get a
# duplicate hit on a memory tagged for a different scope; groundedness
# before dedup because a hallucinated write being a "duplicate" of a real
# one is misleading; dedup before pending so the user-inference
# confirmation flow doesn't ask about a write we'd already reject.
# PendingGate is last because everything else either rejects or accepts.
_WRITE_GATES: tuple[WriteGate, ...] = (
    CredentialGate(),
    TransientGate(),
    UserClaimGate(),
    ScopeMismatchGate(),
    GroundednessGate(),
    DedupActiveGate(),
    DedupTombstoneGate(),
    PendingGate(),
)


# ---------------------------------------------------------------------------
# The shared chain: one policy, two of the four write paths so far
# ---------------------------------------------------------------------------
#
# `_WRITE_GATES` used to be reachable only from `memory_write` below, which
# made the write policy a property of ONE entry point rather than of the
# store. Three other paths reached `Store.write` directly and each carried a
# different subset of the policy: `ingest.apply_ingest_plan` ran no gates at
# all, `consolidate._apply_llm_proposal` hand-reimplemented four of them
# (and had already drifted), and `handlers.proposals.accept_proposal`
# mirrored the credential gate alone. `apply_write_gates` is where that
# policy now lives, but only two of the four route through it: `memory_write`
# (the full chain) and `ingest.apply_ingest_plan` (`CONTENT_GATES`).
# `consolidate._apply_llm_proposal` and `accept_proposal` still run their own
# subsets. Consolidate's divergence is deliberate and measured: it refuses
# hard with no override to offer, and it scopes the transient and dedup
# checks to the LLM-authored claim while the credential scan and the size cap
# judge the provenance-stamped text. `accept_proposal`'s is not deliberate —
# it runs payload validation plus a credential scan and is unconverted.
#
# What deliberately stays OUT of this function: recorder events,
# `_corroborate_duplicate`, and SessionState staging. Those need `recorder`
# and `state`, which the non-MCP callers don't have and shouldn't grow — so
# this returns a DECISION and the caller owns the side effects. That split
# is what lets one chain serve callers whose failure modes have nothing in
# common (MCP response dict / skipped ingest row), and what converting the
# two hand-rolled subsets would rest on (consolidate raises per cluster;
# accept_proposal returns a status dict with the proposal left queued).
#
# This layer sits strictly ABOVE `Store.write`. It does not touch the
# `_locked` / `_atomic_write_post` path — the TOCTOU rationale documented at
# `store.py:443-450` and `1798-1830` is load-bearing and eight mutators
# share it.


class GateDeps(Protocol):
    """The four dependencies the gate chain actually reads.

    Narrower than `ToolHandlers` on purpose. `ToolHandlers` satisfies this
    structurally with no changes, and non-MCP callers can satisfy it with
    `GateBundle` instead of constructing a server-shaped object — which is
    what previously pushed `ingest` and `consolidate` into hand-rolling
    policy rather than calling into it.

    `_semantic_model_factory` is declared as a callable ATTRIBUTE, not a
    method, because that is what `ToolHandlers` actually holds
    (`_handlers.py:367` assigns the factory to the instance). Declaring it
    `def` here would type-check against a bound method and quietly exclude
    the very class this protocol exists to describe.
    """

    store: Store
    config: Config
    responses: ResponseBuilder
    _semantic_model_factory: Callable[[Config], Any]


class GateBundle:
    """`GateDeps` for callers that hold a `Store` but no `ToolHandlers`.

    `responses` is a real `ResponseBuilder` rather than a stub: the gates
    build their rejection payloads eagerly, and a caller that discards the
    payload (ingest keeps only `reason`) still benefits from the shaping
    being identical to what the MCP surface would have returned. One
    rejection shape, one place to change it.
    """

    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        responses: ResponseBuilder,
        semantic_model_factory: Callable[[Config], Any],
    ) -> None:
        self.store = store
        self.config = config
        self.responses = responses
        self._semantic_model_factory = semantic_model_factory

    @classmethod
    def for_store(
        cls,
        store: Store,
        config: Config,
        *,
        semantic_model: Any | None = None,
    ) -> GateBundle:
        """Build a bundle from the two things every caller already has.

        `semantic_model` short-circuits the factory for callers that have
        already paid to load a model (`consolidate_llm` loads one per run,
        and re-loading it per proposal would mean a model init per write).
        Passing None keeps the config-driven default, including the Jaccard
        fallback when the `semantic` extra isn't installed.
        """
        from .._response import ResponseBuilder
        from ..semantic_setup import _semantic_model_or_none

        factory: Callable[[Config], Any]
        if semantic_model is not None:
            factory = lambda _config: semantic_model  # noqa: E731
        else:
            factory = _semantic_model_or_none
        return cls(
            store=store,
            config=config,
            responses=ResponseBuilder(
                stale_after_days=config.behavior.verification_stale_days
            ),
            semantic_model_factory=factory,
        )


# The gates that judge CONTENT — everything except the two gates whose
# correctness depends on the caller having a human to ask: `PendingGate`
# (the confirmation handshake itself) and `UserClaimGate` (which refuses
# in order to route the write INTO that handshake). Both are excluded by
# name rather than by "everything but Pending", because the exclusion is
# what keeps the batch callers correct: `apply_ingest_plan` is a bulk
# import of the user's OWN prior auto-memory files — first-person
# preference prose is the norm there, not a model asserting a fresh claim
# — and `accept_proposal` is a human review decision on a queue whose
# extractor deliberately stamps explicit captures ("remember that I prefer
# X") as `fact`, so their bodies match the preference shapes by
# construction. Inheriting `UserClaimGate` would hard-refuse exactly the
# rows those two paths exist to carry, with every acknowledge flag False
# and no human in the loop to flip one.
#
# Splitting the tuple rather than always running the whole chain is a
# deliberate answer to a real finding: the non-MCP writers' deviations from
# `memory_write` are not all oversights. `apply_ingest_plan` bypasses
# `PendingGate` ON PURPOSE — the source file it reads is the user's own
# authored `memory/*.md`, so "the source file is itself the user's act of
# commit" (contract locked by
# `tests/test_ingest.py::test_user_inference_lands_in_active_store_not_pending`).
# Forcing confirmation there would break a reasoned, tested behaviour in the
# name of consistency. What ingest genuinely lacked was the CONTENT
# judgement — a credential, a transient marker, or a duplicate in an
# authored file is still all three of those things.
#
# So each caller names the subset its situation justifies, and the reason
# lives at the call site. A caller that wants the full chain passes nothing.
CONTENT_GATES: tuple[WriteGate, ...] = tuple(
    g for g in _WRITE_GATES if not isinstance(g, (PendingGate, UserClaimGate))
)


# The gates `memory_write_confirm` re-runs against a staged payload before
# it commits. A pending write can sit for an hour, and the store is not
# frozen while it does: the duplicate it is now a duplicate OF may have been
# written five minutes ago, and the memory it now overlaps may have been
# tombstoned since. Confirm used to replay the payload through zero gates,
# so both landed unchecked.
#
# It is a SUBSET, and each omission is a decision rather than an oversight:
#
# - `TransientGate` / `UserClaimGate` / `ScopeMismatchGate` judge the BODY,
#   and the body has not changed since staging. Re-running them can only
#   re-raise a verdict the caller already answered (by rewording, by
#   acknowledging, or by staging deliberately), and the caller has no way to
#   pass an acknowledgement through `memory_write_confirm` even if it wanted
#   to. What they would produce is a refusal with no legal escape.
# - `GroundednessGate` needs the source transcript, which is not staged
#   (`session.GATE_FLAG_KEYS` says why).
# - `PendingGate` would stage the write a second time.
#
# What is left is exactly the set whose verdict depends on the STORE rather
# than on the payload — dedup against the active set and against tombstones
# — plus the credential scan, which is cheap, cannot produce a false refusal
# on an unchanged body (it either fired at staging time or it did not), and
# is the one refusal severe enough to be worth re-asserting at the moment of
# durable commit. Derived from `_WRITE_GATES` by type so confirm inherits
# the chain's ordering instead of pinning its own copy of it.
_CONFIRM_GATES: tuple[WriteGate, ...] = tuple(
    g
    for g in _WRITE_GATES
    if isinstance(g, (CredentialGate, DedupActiveGate, DedupTombstoneGate))
)


def apply_write_gates(
    deps: GateDeps,
    gc: GateContext,
    *,
    gates: tuple[WriteGate, ...] = _WRITE_GATES,
) -> Reject | Pending | None:
    """Run a write-gate chain. Returns the first non-`Continue` decision.

    - `Reject` — refuse the write; `.response` is the caller-facing dict and
      `.event_kwargs` the audit payload the caller should record.
    - `Pending` — the write needs user confirmation before it commits. Only
      reachable when `gates` includes `PendingGate`.
    - `None` — every gate passed; the caller may commit.

    `gates` defaults to the full chain so the MCP path cannot silently lose
    a gate; batch callers pass `CONTENT_GATES` and document why at the call
    site. Note that the escape hatches (`acknowledge_credential` and
    friends) are `GateContext` fields, not gate behaviour — an unattended
    caller that leaves them False gets the hard refusal it wants without
    needing its own copy of the check.
    """
    for gate in gates:
        result = gate.evaluate(deps, gc)
        if isinstance(result, (Reject, Pending)):
            return result
    return None


# ---------------------------------------------------------------------------
# Orchestrator: memory_write
# ---------------------------------------------------------------------------


def _corroborate_duplicate(
    deps: "ToolHandlers", state: SessionState, result: "Reject"
) -> None:
    """Record a recurrence on the memory a duplicate write matched.

    The counter measures INDEPENDENT re-entries of a claim, so the bump
    is once per (memory, session) — `state.corroborated_ids` dedups
    within the session, and only the TOP high-overlap match is credited
    (a write that grazes three near-duplicates is one recurrence of one
    claim, not three).

    Best-effort by contract: the rejection response the model needs is
    already built, and a telemetry bump must never turn a clean
    "duplicate" answer into an error — store races (concurrent
    tombstone) and size-cap refusals log at WARNING and drop. On
    success the response gains `corroboration_recorded: true` +
    `corroborations` (the new total), so the model knows the recurrence
    was captured and doesn't force-write out of capture anxiety; the
    reject event gains `corroborated_id` for the audit trail.
    """
    matches = result.response.get("matches") or []
    if not matches or not isinstance(matches[0], dict):
        return
    top_id = matches[0].get("id")
    if not isinstance(top_id, str) or not top_id:
        return
    if top_id in state.corroborated_ids:
        result.response["corroboration_recorded"] = False
        return
    try:
        bumped = deps.store.record_corroboration(top_id)
    except Exception as exc:  # noqa: BLE001 — telemetry must not break the reject
        log.warning("corroboration bump for %s failed: %s", top_id, exc)
        return
    state.corroborated_ids.add(top_id)
    result.response["corroboration_recorded"] = True
    result.response["corroborations"] = bumped.corroborations
    result.event_kwargs["corroborated_id"] = top_id


async def memory_write(
    deps: "ToolHandlers",
    content: str,
    scopes: list[str],
    confidence: str = "medium",
    source: str = "explicit-statement",
    force: bool = False,
    acknowledge_transient: bool = False,
    acknowledge_scope_mismatch: bool = False,
    acknowledge_ungrounded: bool = False,
    acknowledge_credential: bool = False,
    acknowledge_user_claim: bool = False,
    category: str = "fact",
    groundedness_check: bool = False,
    source_transcript: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Validate the payload, run the gate chain, and either commit or
    short-circuit per the first gate that rejects."""
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
    # See `memory_write_confirm` for why the bind precedes `_advance_turn`.
    state.bind_pending_log(deps.store.root)
    _advance_turn(state, deps.recorder)
    payload = _validate_write_payload(
        content=content,
        scopes=scopes,
        confidence=confidence,
        source=source,
        allowed_scopes=deps.config.scopes.allowed,
        category=category,
        max_content_bytes=deps.config.behavior.max_content_bytes,
        min_content_tokens=deps.config.behavior.min_content_tokens,
        max_scopes_per_write=deps.config.behavior.max_scopes_per_write,
    )

    # Origin is captured before the gate chain so it's part of the
    # payload that flows into either staging or the direct write path.
    # We never persist origin for a rejection — the early return
    # below short-circuits before any disk I/O.
    payload["origin"] = _h.capture_origin()

    gc = GateContext(
        payload=payload,
        force=force,
        acknowledge_transient=acknowledge_transient,
        acknowledge_scope_mismatch=acknowledge_scope_mismatch,
        acknowledge_ungrounded=acknowledge_ungrounded,
        acknowledge_credential=acknowledge_credential,
        groundedness_check=groundedness_check,
        source_transcript=source_transcript,
        acknowledge_user_claim=acknowledge_user_claim,
    )

    pending_decision: Pending | None = None
    decision = apply_write_gates(deps, gc)
    if isinstance(decision, Reject):
        # Recurrence-as-evidence: a duplicate rejection IS the stored
        # claim re-entering a conversation. Record the corroboration
        # on the matched memory (once per session per memory) before
        # the reject event goes out, so the event carries the id.
        if decision.response.get("status") == "duplicate":
            _corroborate_duplicate(deps, state, decision)
        deps.recorder.record("write", **decision.event_kwargs)
        return decision.response
    if isinstance(decision, Pending):
        pending_decision = decision

    # Capture which markers (if any) were overridden by
    # `acknowledge_transient` — feeds the override-rate signal in the
    # event log so we can tell whether a marker is producing too many
    # false positives.
    acknowledged = (
        [h.marker for h in gc.transient_hits]
        if gc.transient_hits and acknowledge_transient
        else []
    )
    # Parallel to `acknowledged`: which credential detectors were overridden
    # by `acknowledge_credential`, recorded (kind only) so a high override
    # rate flags a too-loose detector. Never the value.
    credentials_acknowledged = (
        [h.kind for h in gc.credential_hits]
        if gc.credential_hits and acknowledge_credential
        else []
    )
    # Same axis again for the user-claim gate: the phrases a caller
    # overrode. This gate's phrase list is the kind that only ever gets
    # revisited on override-rate evidence, so the evidence has to exist.
    user_claims_acknowledged = (
        [h.phrase for h in gc.user_claim_hits]
        if gc.user_claim_hits and acknowledge_user_claim
        else []
    )

    if pending_decision is not None:
        return _stage_pending(
            deps,
            state,
            payload=payload,
            pending_reason=pending_decision.pending_reason,
            related=gc.related,
            removed_related=gc.removed_related,
            forced=force,
            acknowledged=acknowledged,
            credentials_acknowledged=credentials_acknowledged,
            user_claims_acknowledged=user_claims_acknowledged,
            # Read off the GateContext rather than off this function's
            # parameters: `GATE_FLAG_KEYS` is a list of GateContext field
            # names, so sourcing the values from anywhere else is how the
            # two would drift.
            gate_flags={key: getattr(gc, key) for key in GATE_FLAG_KEYS},
        )

    response = _commit_write(
        deps,
        payload=payload,
        related=gc.related,
        removed_related=gc.removed_related,
        forced=force,
        acknowledged=acknowledged,
        credentials_acknowledged=credentials_acknowledged,
        user_claims_acknowledged=user_claims_acknowledged,
    )
    _maybe_attach_curation_hint(response, deps, state)
    return response


def _stage_pending(
    deps: "ToolHandlers",
    state: SessionState,
    *,
    payload: dict[str, Any],
    pending_reason: str,
    related: list[SimilarHit],
    removed_related: list[SimilarHit],
    forced: bool,
    acknowledged: list[str],
    credentials_acknowledged: list[str],
    user_claims_acknowledged: list[str],
    gate_flags: dict[str, Any],
) -> dict[str, Any]:
    """Stage the write through the SessionState, record the pending
    event, and return the pending response shape.

    `gate_flags` rides along on the staged write so the confirm-time
    re-gate judges it under the same overrides the caller passed here —
    without it, a `force=True` write is re-refused as a duplicate by the
    very gate the caller already overrode."""
    category_enum: Category = payload["category"]
    pending = state.stage_write(payload, gate_flags=gate_flags)
    hint = (
        "User-inference category — ask the user in plain "
        "language ('want me to remember that you prefer X?') "
        "and only then call memory_write_confirm(pending_id), "
        "or memory_write_cancel(pending_id) if they decline."
        if pending_reason == "user-inference"
        else (
            "Confirm with memory_write_confirm(pending_id) or "
            "drop with memory_write_cancel(pending_id)."
        )
    )
    response: dict[str, Any] = {
        "status": "pending",
        "pending_id": pending.pending_id,
        "pending_reason": pending_reason,
        "preview": {
            "content": payload["content"],
            "scopes": payload["scopes"],
            "confidence": payload["confidence"].value,
            "source": payload["source"].value,
            "category": category_enum.value,
        },
        "hint": hint,
    }
    if related:
        response["related"] = [deps.responses.similar_to_dict(h) for h in related]
    if removed_related:
        response["removed_related"] = [
            deps.responses.similar_to_dict(h) for h in removed_related
        ]
    deps.recorder.record(
        "write",
        status="pending",
        pending_id=pending.pending_id,
        pending_reason=pending_reason,
        category=category_enum.value,
        scopes=payload["scopes"],
        forced=forced,
        related=[h.id for h in related],
        removed_related=[h.id for h in removed_related],
        markers_acknowledged=acknowledged,
        credentials_acknowledged=credentials_acknowledged,
        user_claims_acknowledged=user_claims_acknowledged,
    )
    return response


def _commit_write(
    deps: "ToolHandlers",
    *,
    payload: dict[str, Any],
    related: list[SimilarHit],
    removed_related: list[SimilarHit],
    forced: bool,
    acknowledged: list[str],
    credentials_acknowledged: list[str],
    user_claims_acknowledged: list[str],
) -> dict[str, Any]:
    """Persist the memory, record the commit event, return the
    committed response. Surfaces the ambient long-body warning as a
    non-blocking advisory when applicable."""
    category_enum: Category = payload["category"]
    try:
        memory = deps.store.write(**payload)
    except OSError as exc:
        # Disk-level failure (ENOSPC/EIO/EACCES) in the durable write.
        # Translate to ValueError so the MCP boundary returns a clean
        # structured error rather than leaking the bare OSError's absolute
        # path to the client — matching the sibling lifecycle handlers
        # (remove/restore/verify/rename_scope/proposals).
        raise ValueError(f"failed to write memory: {exc}") from exc
    warnings: list[str] = []
    if (
        category_enum == Category.AMBIENT
        and len(memory.body.split()) > _AMBIENT_LONG_BODY_WORDS
    ):
        warnings.append("ambient_body_long")
    deps.recorder.record(
        "write",
        status="committed",
        id=memory.id,
        category=category_enum.value,
        scopes=memory.scopes,
        confidence=memory.confidence.value,
        source=memory.source.value,
        forced=forced,
        related=[h.id for h in related],
        removed_related=[h.id for h in removed_related],
        markers_acknowledged=acknowledged,
        credentials_acknowledged=credentials_acknowledged,
        user_claims_acknowledged=user_claims_acknowledged,
        warnings=warnings,
    )
    return deps.responses.committed(
        memory,
        related=related,
        removed_related=removed_related,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# memory_write_confirm / memory_write_cancel
# ---------------------------------------------------------------------------


def _confirm_gate_context(pending: PendingWrite) -> GateContext:
    """A `GateContext` for re-judging a staged payload at confirm time.

    The overrides are the ones the ORIGINAL `memory_write` carried
    (`session.GATE_FLAG_KEYS`), spelled out one by one rather than
    splatted so the type checker sees the arity;
    `test_every_staged_gate_flag_reaches_the_confirm_context` is what
    keeps the two lists from drifting apart.

    `groundedness_check=False` is not a policy choice here — the
    transcript that gate needs is not staged, so passing True would only
    make `GroundednessGate` return `Continue` on a missing transcript.
    """
    flags = pending.gate_flags
    return GateContext(
        payload=pending.payload,
        force=flags["force"],
        acknowledge_transient=flags["acknowledge_transient"],
        acknowledge_scope_mismatch=flags["acknowledge_scope_mismatch"],
        acknowledge_ungrounded=flags["acknowledge_ungrounded"],
        acknowledge_credential=flags["acknowledge_credential"],
        acknowledge_user_claim=flags["acknowledge_user_claim"],
        groundedness_check=False,
        source_transcript=None,
    )


def _confirm_refusal(
    deps: "ToolHandlers", state: SessionState, pending_id: str, decision: Reject
) -> dict[str, Any]:
    """Shape a confirm-time gate refusal: the gate's own response, plus
    the still-valid pending id.

    The staged write is deliberately NOT consumed and its promotion
    linkage deliberately NOT popped — this is a refusal, not a resolution,
    and both have to survive for the caller to decide. That mirrors
    `memory_write_cancel`'s contract about the source episode, except
    cancel drops the linkage because the pending is gone and here it isn't.

    The gate's own hint offers `force=True` / `acknowledge_*`, which are
    `memory_write` parameters — `memory_write_confirm` takes a pending id
    and nothing else, so the addendum has to say where those overrides
    actually live or the model retries the same call expecting a
    different answer.
    """
    if decision.response.get("status") == "duplicate":
        _corroborate_duplicate(deps, state, decision)
    response = dict(decision.response)
    response["pending_id"] = pending_id
    response["pending_retained"] = True
    response["hint"] = (
        f"{response.get('hint', '')} This fired at CONFIRM time — the store "
        "changed while the write was staged. Nothing was committed and "
        f"{pending_id!r} is still valid, so the choice is memory_write_cancel"
        "(pending_id) to drop it, or resolve the match (memory_update, "
        "memory_restore) and cancel. The overrides named above are "
        "memory_write parameters; re-issuing the write with one is the only "
        "way to pass them — memory_write_confirm takes the pending id alone."
    ).strip()
    deps.recorder.record(
        "write_confirm",
        pending_id=pending_id,
        pending_retained=True,
        **decision.event_kwargs,
    )
    return response


async def memory_write_confirm(
    deps: "ToolHandlers", pending_id: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    # Before `_advance_turn`: adoption has to happen while the TTL sweep it
    # feeds is still ahead of us, or a write that expired during a restart
    # emits its `pending_expired` event one call later than it should.
    state.bind_pending_log(deps.store.root)
    _advance_turn(state, deps.recorder)
    # Peek, don't pop. A gate refusal below has to leave the staged write
    # re-confirmable, and `take_pending` would already have destroyed it.
    pending = state.peek_pending(pending_id)
    if pending is None:
        # Distinguish "expired" from "never existed" so the model
        # can offer to re-stage the write rather than just retrying
        # with the same id. The 1h TTL is short enough that a long
        # human absence (lunch break, overnight think) is the most
        # common cause; before 2.6.8 the error was indistinguishable
        # from a typo, and the eviction was silent.
        if state.was_recently_expired(pending_id):
            raise ValueError(
                f"pending write {pending_id!r} expired before "
                "confirmation (the 1-hour TTL elapsed). The proposed "
                "memory was not saved. Re-stage with memory_write to "
                "create a fresh pending id."
            )
        raise ValueError(
            f"no pending write with id {pending_id!r} (it may have "
            "been already committed or never existed)"
        )
    decision = apply_write_gates(
        deps, _confirm_gate_context(pending), gates=_CONFIRM_GATES
    )
    if isinstance(decision, Reject):
        return _confirm_refusal(deps, state, pending_id, decision)
    # Every store-dependent gate passed, so the staged write is consumed
    # now — after the judgement, not before it. The peeked object stays the
    # authority for this call: `take_pending` re-runs the TTL sweep, and on
    # the sub-microsecond chance that it evicts between the peek and the
    # pop, the caller's confirm still refers to the write it asked about.
    state.take_pending(pending_id)
    try:
        memory = deps.store.write(**pending.payload)
    except OSError as exc:
        # Disk-level failure after the staged write was consumed a few lines
        # above. Translate to a clean ValueError at the MCP boundary instead
        # of leaking the bare OSError path; note the pending id is already
        # consumed so the caller must re-stage with memory_write rather than
        # re-confirm.
        raise ValueError(
            f"failed to write memory: {exc} (pending {pending_id!r} was "
            "already consumed — re-stage with memory_write)"
        ) from exc
    # If this pending write originated from `episode_promote`, delete
    # the source episode now — the durable memory is the authoritative
    # artifact and leaving the journal entry behind would survive past
    # confirmation as a duplicate. The link was stashed at staging
    # time by the promote handler; consume it (pop) so a redundant
    # later call doesn't try to delete twice.
    promo = state.take_promotion_episode(pending_id)
    promoted_episode_id: str | None = None
    if promo is not None:
        # Local import to break the cycle (episode_promote also imports
        # `memory_write` from this module).
        from .episode_promote import _delete_source_episode

        ep_session_id, ep_id = promo
        _delete_source_episode(deps, ep_session_id, ep_id)
        promoted_episode_id = ep_id
    # Stamp the deleted source-episode id onto the confirm event on the
    # promotion path (None on a normal confirm). This is the durable,
    # confirm-TIME proof the deferred promotion delete actually ran, which
    # `episode_handoff._episode_promoted_out_of_session` requires before it
    # will name a promotion. A BARE `episode_promote` event with
    # write_status="pending" is NOT proof on its own: it is recorded at
    # STAGING time, before the outcome is known, and the staged write may
    # instead be cancelled (memory_write_cancel) or TTL-expired — both KEEP
    # the source episode on disk. A later `prune_old_sessions` then rmtrees
    # the whole session dir, leaving the exact zero-episode shape a real
    # promotion leaves. This episode_id is the only signal that separates
    # "confirmed & deleted" from "cancelled/expired then pruned".
    deps.recorder.record(
        "write_confirm",
        pending_id=pending_id,
        id=memory.id,
        scopes=memory.scopes,
        episode_id=promoted_episode_id,
    )
    response = deps.responses.committed(memory)
    _maybe_attach_curation_hint(response, deps, state)
    return response


async def memory_write_cancel(
    deps: "ToolHandlers", pending_id: str, ctx: Context | None = None
) -> dict[str, Any]:
    state = deps.sessions.for_request(ctx)
    # See `memory_write_confirm` for why the bind precedes `_advance_turn`.
    state.bind_pending_log(deps.store.root)
    _advance_turn(state, deps.recorder)
    existed = state.cancel_pending(pending_id)
    # Drop the promotion linkage if there is one, but DON'T delete the
    # source episode — cancel is the user saying "not yet", so the
    # caller should be able to fix the wording and re-promote from the
    # same journal entry. `take_promotion_episode` pops the same linkage
    # `discard_promotion_episode` would (both are a bare dict pop), but
    # hands back `(session, episode_id)` so we can stamp the KEPT episode's
    # id onto the write_cancel event below.
    promo = state.take_promotion_episode(pending_id)
    cancelled_episode_id = promo[1] if promo is not None else None
    # Stamp the kept source-episode id onto the write_cancel event (None on
    # a non-promotion cancel). This is the confirm-time NEGATIVE-proof
    # counterpart to the episode_id `memory_write_confirm` stamps on
    # write_confirm: where the confirm stamp proves a deferred promotion's
    # delete RAN, this cancel stamp proves the staged promotion did NOT
    # commit (the source episode stays on disk). `episode_handoff`'s
    # promotion detector reads it to tell a provably-cancelled pending
    # (→ honest "no takeaway" note) apart from an unresolvable one
    # (→ hedged note). Old event logs written before this stamp carry
    # neither key on their promote/confirm/cancel events, so a pre-stamp
    # cancelled pending stays unprovable and honestly hedges — no code
    # change can recover a linkage the old log never recorded.
    deps.recorder.record(
        "write_cancel",
        pending_id=pending_id,
        existed=existed,
        episode_id=cancelled_episode_id,
    )
    return {"cancelled": pending_id, "existed": existed}


__all__ = [
    "DESC_MEMORY_WRITE",
    "DESC_MEMORY_WRITE_CANCEL",
    "DESC_MEMORY_WRITE_CONFIRM",
    "memory_write",
    "memory_write_cancel",
    "memory_write_confirm",
]
