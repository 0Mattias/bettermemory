"""memory_write MCP tool — orchestrator + WriteGate strategy.

Pre-Round-2 ``memory_write`` was a 356-line method with six sequential
guard blocks inline (durability / scope-mismatch / groundedness /
dedup-active / dedup-tombstone). Each block built a
short-circuit response dict and recorded its own event before
returning. The function read as a single long ladder; understanding any
one gate required scrolling past the others.

Round 2 extracts each gate into a small class (``WriteGate``
subclasses below) whose ``evaluate(payload, ...)`` method returns one
of:

- ``Reject`` — short-circuit with this response dict and recorder
  event. The orchestrator records the event and returns.
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

"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..conflicts import ConflictCandidate, ConflictQueue, _pair_id
from ..credentials import find_credential_markers
from ..durability import find_transient_markers, in_quoted_span, quoted_spans
from ..models import Category, Memory, SimilarHit, is_valid_ulid, snippet_for, utcnow
from ..scope_match import (
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)
from ..search import find_similar, find_similar_tombstones
from .. import identity
from ..session import SessionState
from ..store import MemoryNotFoundError, TombstonedError
from ..supersession import SupersessionMatch, detect_supersession
from ._shared import (
    Context,
    _AMBIENT_LONG_BODY_WORDS,
    _advance_turn,
    _validate_declared_claims,
    _validate_write_payload,
)

if TYPE_CHECKING:
    from .._handlers import ToolHandlers
    from .._response import ResponseBuilder
    from ..config import Config
    from ..store import Store

log = logging.getLogger("bettermemory.handlers.write")


# Sentence and list shaping for the user-claim gate below. These were the
# proposals extractor's patterns; the extractor left in 9.0 and the gate
# keeps the shapes it was pinned against.
_SENTENCE_SPLIT_RE = re.compile(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.!?])\s+|\n+")
# Join a single newline into a space only when the prior char is not
# terminal punctuation, a colon or another newline, and the next line
# does not open with a list or quote marker.
_HARD_WRAP_RE = re.compile(r"(?<![.!?:\n])\n(?=\S)(?![-*+•>])(?!\d{1,3}[.)]\s)")
# Leading markdown list or quote markers, possibly stacked.
_LIST_PREFIX_RE = re.compile(r"^(?:[-*+•]\s+|\d{1,3}[.)]\s+|>\s+)+")
# Typographic single quotes to the ASCII apostrophe the patterns expect.
_SMART_APOSTROPHES = str.maketrans({"‘": "'", "’": "'"})
_PREFERENCE_RE = re.compile(
    r"\b(i (?:prefer|like|love|hate|avoid|always|never|usually|use)\b"
    r"|i (?:want|need)\b(?!\s+you\b)"
    r"|i(?:'?m| am) using|i(?:'?ve| have) been using"
    r"|we (?:use|prefer|avoid|always|never)\b"
    r"|we(?:'?re| are) using|we(?:'?ve| have) been using"
    r"|^(?:my|our)\s+(?:\w+\s+){0,4}?(?:is|are|was|were|prefers?|uses?|runs?|lives)\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Description constants
# ---------------------------------------------------------------------------


DESC_MEMORY_WRITE = (
    "Store a durable fact; call it proactively. `scopes`: narrow tags "
    "(projects:<name>, tools), never general. `category`: fact, "
    "user-inference (about the user) or ambient (context, never cited). "
    "Gates refuse transient state, secrets, user claims filed as fact, "
    "scope mismatch, duplicates and, under groundedness_check, sentences "
    "absent from source_transcript; each names its acknowledge flag. "
    "`claims`: path or path::symbol bindings watched for drift. "
    "`supersedes`: ids this replaces."
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
    acknowledge_user_claim: bool = False
    # Outputs the gates accumulate as they pass — read by later gates
    # or the final commit step.
    credential_hits: list[Any] = None  # type: ignore[assignment]
    transient_hits: list[Any] = None  # type: ignore[assignment]
    user_claim_hits: list[Any] = None  # type: ignore[assignment]
    related: list[SimilarHit] = None  # type: ignore[assignment]
    removed_related: list[SimilarHit] = None  # type: ignore[assignment]
    # The active set `DedupActiveGate` loaded, kept so the persist step's
    # supersession detection reads the same snapshot instead of paying a
    # second `load_all`. None when the gate did not run (`force=True`).
    active_snapshot: list[Memory] | None = None

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


GateResult = Continue | Reject


class WriteGate:
    """Common base; subclasses override ``evaluate``."""

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
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

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
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

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
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
                    "be true in a week. Three remedies: rephrase to the "
                    "durable level-up version (extract the architectural "
                    "decision, the why, what-was-built — discard the "
                    "timestamp/state); route genuine run-state to "
                    "episode_write, the journal tier built for exactly "
                    "this content (no durability gate there); or pass "
                    "acknowledge_transient=True if the marker is "
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
# words, so every one of its branches is first-person. Composed alongside
# it rather than folded into it.
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


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """`_SENTENCE_SPLIT_RE.split(text)` segments, as offsets into `text`.

    The split pattern captures nothing, so the segments are exactly the
    gaps between separator matches — same sequence `split` returns, with
    the positions kept.
    """
    out: list[tuple[int, int]] = []
    prev = 0
    for sep in _SENTENCE_SPLIT_RE.finditer(text):
        out.append((prev, sep.start()))
        prev = sep.end()
    out.append((prev, len(text)))
    return out


def _first_unquoted(
    pattern: re.Pattern[str],
    sentence: str,
    base: int,
    spans: tuple[tuple[int, int], ...],
) -> re.Match[str] | None:
    """First `pattern` match in `sentence` that is not inside a quotation.

    `base` is `sentence`'s offset in the text `spans` was measured on.
    Applied to `_PREFERENCE_RE` only — see `_find_user_claims`.
    """
    for match in pattern.finditer(sentence):
        if in_quoted_span(spans, base + match.start(), base + match.end()):
            continue
        return match
    return None


def _find_user_claims(content: str) -> list[UserClaimHit]:
    """Sentences in `content` that read as claims about the user.

    Applied the way production applies `_PREFERENCE_RE` — per sentence,
    after smart-apostrophe normalization and hard-wrap repair — because
    that pattern's `^(?:my|our)` branch anchors to the start of whatever
    string it is handed, and a curly-apostrophe body ("I’m using zsh")
    misses every contraction branch without the translation.

    No length floor: "Mattias prefers tabs" (20 chars, 3 tokens) is
    precisely the write this gate exists to catch.

    QUOTATION EXEMPTS THE `_PREFERENCE_RE` LEG ONLY. That leg is a
    transcript miner: every branch is first person, because in the Stop
    hook the text it reads was typed BY the user. A memory body is
    written by the assistant, so first person there is either the
    assistant's own voice or a transcription of somebody else's — never
    the shape this gate exists to relabel. Measured on the 360-body
    dogfood store: 9 of the leg's 11 fires sat inside a quotation, all
    nine on owner rulings and directives recorded verbatim, and filing
    those as `user-inference` would label what the user is quoted
    saying as something inferred about them. `_USER_CLAIM_RE` keeps firing
    inside quotations — it reads the THIRD-person shape a model writes
    when it files a claim of its own, its evidence is nine fires none of
    which is quoted, and narrowing an unfired leg on no evidence is how
    a gate quietly stops working.

    The residue this does not clear, named so it is not mistaken for
    solved: two fires are unquoted first person in the ASSISTANT's voice
    ("the one I never think to query for", "a corpus we never ran"),
    where the pronoun heads a relative clause rather than a self-report.
    Both are false positives and both still block. The rule that would
    separate them is clause position, and four fires is not enough
    evidence to tune one.
    """
    hits: list[UserClaimHit] = []
    text = _HARD_WRAP_RE.sub(" ", content.translate(_SMART_APOSTROPHES))
    # Both normalizations above are length-preserving — `str.translate`
    # maps one character to one, and `_HARD_WRAP_RE` replaces a single
    # `\n` with a single space — so a span measured here indexes
    # `content` too, and the hard-wrap repair has already rejoined a
    # quotation broken across a soft wrap before it is measured.
    spans = quoted_spans(text)
    for start, end in _sentence_spans(text):
        raw = text[start:end]
        lead = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        delisted = _LIST_PREFIX_RE.sub("", stripped)
        lead += len(stripped) - len(delisted)
        sentence = delisted.strip()
        lead += len(delisted) - len(delisted.lstrip())
        if not sentence:
            continue
        base = start + lead
        match = _first_unquoted(_PREFERENCE_RE, sentence, base, spans)
        if match is None:
            match = _USER_CLAIM_RE.search(sentence)
        if match is not None:
            hits.append(UserClaimHit(phrase=match.group(0), sentence=sentence))
    return hits


class UserClaimGate(WriteGate):
    """Reject bodies that read as claims ABOUT THE USER unless they are
    filed as `user-inference` (or `acknowledge_user_claim` is set).

    The category LABEL is the only thing that marks a stored claim about
    the user as an inference, and `user-inference` commits exactly as
    `fact` does, so the label is all this gate protects: a claim about
    the user written as `category='fact'` would read back as an
    established fact, indistinguishable from the project facts beside it
    and invisible to anyone looking for inferences to correct. The fix
    costs the writer one re-issue and the user nothing — they never see
    this refusal. The gate classifies the BODY, which is why it sits
    next to `TransientGate`: it must precede dedup (a re-categorized
    re-issue must not be routed to `memory_update` against a mis-filed
    parent).

    Precision-first, and porous by the same trade the transient and
    credential gates make: it matches predicating shapes, so a nominalised
    claim like "<Name>'s preference is tabs" passes (measured, both
    apostrophe forms). Widening it to possessive-plus-noun would refuse
    ordinary prose — "the parser's preference is the longest match" — and
    chasing shapes one at a time is whack-a-mole. The entry ticket for
    revisiting the pattern is override-rate telemetry per marker, which is
    why an acknowledged write records the phrase it overrode.
    """

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
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
                    f"filed as `{category_enum.value}`, which would store "
                    "it as an established fact. Re-issue with "
                    "category='user-inference' — it commits the same way, "
                    "labelled as an inference about the user so it stays "
                    "distinguishable and correctable. Pass "
                    "acknowledge_user_claim=True when the subject is "
                    "someone or something else (a teammate, a tool that "
                    "'prefers' a setting)."
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

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
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

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
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


class DedupActiveGate(WriteGate):
    """Content dedup against the active set. High overlap → reject as
    duplicate (the right move is memory_update on the matched id);
    medium overlap → record as `related` for the eventual response.

    Skipped when `force=True`.
    """

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
        if gc.force:
            return Continue()
        existing = deps.store.load_all()
        gc.active_snapshot = existing
        similar = find_similar(gc.payload["content"], existing)
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

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
        if gc.force:
            return Continue()
        tombstone_similar = find_similar_tombstones(
            gc.payload["content"],
            deps.store.load_tombstones(),
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
                        'fact is now correct, call memory_admin(action="restore", '
                        "id=...) on the tombstone instead of writing a parallel "
                        "entry. Pass "
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


# Order matters: credential before everything so a secret is refused
# before any other gate records body-derived data in the event log;
# transient before dedup so the writer isn't routed to memory_update on a
# transient parent; user-claim next to transient because both classify the
# BODY, and before dedup for the same reason transient is (a re-categorized
# re-issue must not be routed to memory_update on a mis-filed parent);
# scope-mismatch before dedup so the writer doesn't get a
# duplicate hit on a memory tagged for a different scope; groundedness
# before dedup because a hallucinated write being a "duplicate" of a real
# one is misleading.
_WRITE_GATES: tuple[WriteGate, ...] = (
    CredentialGate(),
    TransientGate(),
    UserClaimGate(),
    ScopeMismatchGate(),
    GroundednessGate(),
    DedupActiveGate(),
    DedupTombstoneGate(),
)


# ---------------------------------------------------------------------------
# The shared chain
# ---------------------------------------------------------------------------
#
# `apply_write_gates` is the write policy, and `memory_write` below is its
# caller. The function returns a DECISION and the caller owns the side
# effects (the recorder event, `_corroborate_duplicate`), so a caller that
# holds a store but no `ToolHandlers` can run the same chain through
# `GateBundle`. The chain sits strictly above `Store.write`.


class GateDeps(Protocol):
    """The four dependencies the gate chain actually reads.

    Narrower than `ToolHandlers` on purpose. `ToolHandlers` satisfies this
    structurally with no changes, and a caller without a server satisfies
    it with `GateBundle`.
    """

    store: Store
    config: Config
    responses: ResponseBuilder


class GateBundle:
    """`GateDeps` for callers that hold a `MemoryStore` but no
    `ToolHandlers`.

    `responses` is a real `ResponseBuilder` rather than a stub: the gates
    build their rejection payloads eagerly, so a caller that keeps only the
    reason still gets the shaping the MCP surface would have returned. One
    rejection shape, one place to change it.

    Takes the PROTOCOL, matching `GateDeps.store`, because `GateDeps` is
    satisfied structurally: a protocol attribute is invariant, so a
    bundle declaring the concrete `Store` would not satisfy a `GateDeps`
    whose `store` is a `MemoryStore` — and the two arrive together, since
    `ToolHandlers.store` is the third member of that set.
    """

    def __init__(
        self,
        *,
        store: Store,
        config: Config,
        responses: ResponseBuilder,
    ) -> None:
        self.store = store
        self.config = config
        self.responses = responses

    @classmethod
    def for_store(cls, store: Store, config: Config) -> GateBundle:
        """Build a bundle from the two things every caller already has."""
        from .._response import ResponseBuilder

        return cls(
            store=store,
            config=config,
            responses=ResponseBuilder(
                stale_after_days=config.behavior.verification_stale_days
            ),
        )


def apply_write_gates(
    deps: GateDeps,
    gc: GateContext,
    *,
    gates: tuple[WriteGate, ...] = _WRITE_GATES,
) -> Reject | None:
    """Run a write-gate chain. Returns the first `Reject`, or `None` when
    every gate passed and the caller may commit. `.response` is the
    caller-facing dict and `.event_kwargs` the audit payload the caller
    should record.

    `gates` defaults to the full chain so the MCP path cannot silently lose
    a gate. The escape hatches (`acknowledge_credential` and friends) are
    `GateContext` fields, not gate behaviour: an unattended caller that
    leaves them False gets the hard refusal it wants without needing its
    own copy of the check.
    """
    for gate in gates:
        result = gate.evaluate(deps, gc)
        if isinstance(result, Reject):
            return result
    return None


# ---------------------------------------------------------------------------
# Orchestrator: memory_write
# ---------------------------------------------------------------------------


def _corroborate_duplicate(
    deps: ToolHandlers, state: SessionState, result: Reject
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
    deps: ToolHandlers,
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
    claims: list[str] | None = None,
    supersedes: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Validate the payload, run the gate chain, and either commit or
    short-circuit per the first gate that rejects."""
    from .. import _handlers as _h

    state = deps.sessions.for_request(ctx)
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
    # The actor rides beside the origin: who is writing, resolved at the
    # handler entry (`identity.bind` inside `sessions.for_request`) from
    # the request's headers, the server environment, the `initialize`
    # handshake's clientInfo and the attested principal. None when the
    # caller declared nothing, and then the record carries no block.
    payload["actor"] = identity.current_actor()

    # Declared claims are oracle-checked HERE, against the origin just
    # captured — before the gate chain, because a false claim has no
    # acknowledge_* escape by design (the entire value of the field is
    # that a stored claim was true at declaration; an override would
    # mint the one thing the read side is promised cannot exist). The
    # canonical rendered forms land in the payload so the pending
    # confirm path (`store.write(**pending.payload)`) carries them
    # without re-checking.
    if claims:
        origin = payload["origin"]
        payload["claims"] = _validate_declared_claims(
            claims,
            worktree_root=origin.worktree_root if origin else None,
            surface="memory_write",
        )

    # A declared `supersedes` list is checked here, ahead of the gates,
    # for the same reason claims are: a target that is not an active
    # memory is a defect the writer can only fix now, and the link dicts
    # ride the payload through staging so a confirm sets them unchanged.
    if supersedes:
        payload["links"] = _validate_declared_supersedes(deps, supersedes)

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

    decision = apply_write_gates(deps, gc)
    if decision is not None:
        # Recurrence-as-evidence: a duplicate rejection IS the stored
        # claim re-entering a conversation. Record the corroboration
        # on the matched memory (once per session per memory) before
        # the reject event goes out, so the event carries the id.
        if decision.response.get("status") == "duplicate":
            _corroborate_duplicate(deps, state, decision)
        deps.recorder.record("write", **decision.event_kwargs)
        return decision.response

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

    response = _commit_write(
        deps,
        payload=payload,
        related=gc.related,
        removed_related=gc.removed_related,
        forced=force,
        acknowledged=acknowledged,
        credentials_acknowledged=credentials_acknowledged,
        user_claims_acknowledged=user_claims_acknowledged,
        active_snapshot=gc.active_snapshot,
    )
    return response


# ---------------------------------------------------------------------------
# Write-time supersession
# ---------------------------------------------------------------------------


# A writer consolidating several stale notes into one may name them all;
# the detector's own cap (`supersession.MAX_LINKS_PER_WRITE`) is lower
# because it has no such intent to read.
MAX_DECLARED_SUPERSEDES = 16

_CONFLICTS_FILED_HINT = (
    "Each pair under `conflicts_filed` disagrees with this write on a value "
    "and nothing in the body says which side is current. "
    'memory_admin(action="conflicts") lists them; confirm or dismiss there.'
)


@dataclass
class SupersessionOutcome:
    """What the persist step did about supersession, for the event and
    the response. `declared` holds the targets the writer named in
    `supersedes=`; `detected` the matches `supersession.detect_supersession`
    linked on its own; `conflicts` the `(pair id, match)` rows filed for
    `memory_conflicts`."""

    declared: list[str] = field(default_factory=list)
    detected: list[SupersessionMatch] = field(default_factory=list)
    conflicts: list[tuple[str, SupersessionMatch]] = field(default_factory=list)

    def event_fields(self) -> dict[str, Any]:
        """Conditional, so a write that set nothing keeps the event's
        shape. `supersedes` is every target linked; `supersedes_detected`
        the subset the detector chose on its own, which is the telemetry
        a detector-set link removed by a later `memory_update(links=[])`
        is judged against."""
        out: dict[str, Any] = {}
        if self.declared or self.detected:
            out["supersedes"] = self.declared + [m.memory_id for m in self.detected]
        if self.detected:
            out["supersedes_detected"] = [m.memory_id for m in self.detected]
        if self.conflicts:
            out["conflicts_filed"] = [pair_id for pair_id, _ in self.conflicts]
        return out

    def response_fields(self) -> dict[str, Any]:
        rows = [{"id": target, "evidence": "declared"} for target in self.declared]
        rows += [_match_row(m) for m in self.detected]
        out: dict[str, Any] = {}
        if rows:
            out["supersedes"] = rows
        if self.conflicts:
            out["conflicts_filed"] = [
                {"pair_id": pair_id, **_match_row(m)} for pair_id, m in self.conflicts
            ]
            out["hint"] = _CONFLICTS_FILED_HINT
        return out


def _match_row(match: SupersessionMatch) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": match.memory_id,
        "summary": match.summary,
        "evidence": match.evidence,
        "new_value": match.new_value,
        "old_value": match.old_value,
    }
    if match.cue is not None:
        row["cue"] = match.cue
    return row


def _validate_declared_supersedes(deps: ToolHandlers, ids: Any) -> list[dict[str, Any]]:
    """The writer's `supersedes=` list as link dicts for the payload.

    Each id must be a ULID naming an ACTIVE memory: a declared edge to a
    tombstone or a typo would render nothing and sit in the frontmatter
    unread, so it is refused at the one moment the writer can fix it.
    Dicts rather than `MemoryLink`s because the pending-write sidecar is
    JSON and stages the payload as-is; `Store.write` validates them.
    """
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("supersedes must be a list of memory ids")
    unique: list[str] = []
    for memory_id in ids:
        if memory_id not in unique:
            unique.append(memory_id)
    if len(unique) > MAX_DECLARED_SUPERSEDES:
        raise ValueError(
            f"supersedes lists at most {MAX_DECLARED_SUPERSEDES} ids (got {len(unique)})"
        )
    links: list[dict[str, Any]] = []
    for memory_id in unique:
        if not is_valid_ulid(memory_id):
            raise ValueError(f"supersedes entry {memory_id!r} is not a memory id")
        try:
            deps.store.load_one(memory_id)
        except (MemoryNotFoundError, TombstonedError) as exc:
            raise ValueError(
                f"supersedes entry {memory_id!r} is not an active memory: {exc}"
            ) from exc
        links.append(
            {
                "type": "supersedes",
                "target_id": memory_id,
                "note": "declared at write time",
            }
        )
    return links


def _persist(
    deps: GateDeps,
    payload: dict[str, Any],
    *,
    active_snapshot: list[Memory] | None = None,
) -> tuple[Memory, SupersessionOutcome]:
    """Detect supersession, write the record with its links in one locked
    write, file the cue-less disagreements. Shared by the direct commit,
    the confirm path and session capture (`capture._Writer`, through a
    `GateBundle`) so none of them can drift on what a write sets.

    `active_snapshot` is the `load_all` the dedup gate already paid for
    on the direct path; the confirm path has none and loads. A target
    the writer declared is excluded from detection. `OSError` from the
    write propagates so each caller can name its own remedy.
    """
    payload = dict(payload)
    declared_links = list(payload.pop("links", None) or [])
    declared_ids = [str(link["target_id"]) for link in declared_links]
    detected: list[SupersessionMatch] = []
    disagreements: list[SupersessionMatch] = []
    if deps.config.behavior.write_supersession:
        existing = (
            active_snapshot if active_snapshot is not None else deps.store.load_all()
        )
        report = detect_supersession(
            payload["content"], existing, exclude_ids=declared_ids
        )
        detected, disagreements = report.supersedes, report.conflicts
    links = declared_links + [
        {"type": "supersedes", "target_id": m.memory_id, "note": m.note()}
        for m in detected
    ]
    memory = deps.store.write(**payload, links=links)
    return memory, SupersessionOutcome(
        declared=declared_ids,
        detected=detected,
        conflicts=_file_conflicts(deps, memory, disagreements),
    )


def _file_conflicts(
    deps: GateDeps, memory: Memory, matches: list[SupersessionMatch]
) -> list[tuple[str, SupersessionMatch]]:
    """Queue each cue-less disagreement for `memory_conflicts`.
    Best-effort by contract: the memory is already on disk, and a
    queue-file failure must not turn a committed write into an error —
    it logs, and the response omits the pair."""
    if not matches:
        return []
    queue = ConflictQueue(deps.store)
    created = utcnow().isoformat()
    filed: list[tuple[str, SupersessionMatch]] = []
    for match in matches:
        candidate = ConflictCandidate(
            id=_pair_id(memory.id, match.memory_id),
            a_id=memory.id,
            b_id=match.memory_id,
            summary_a=snippet_for(memory.body, max_chars=100),
            summary_b=match.summary,
            similarity=match.similarity,
            method="jaccard",
            detector=match.detector(),
            created=created,
        )
        try:
            queue.file_pair(candidate)
        except OSError as exc:
            log.warning(
                "conflict filing for %s / %s failed: %s",
                memory.id,
                match.memory_id,
                exc,
            )
            continue
        filed.append((candidate.id, match))
    return filed


def _commit_write(
    deps: ToolHandlers,
    *,
    payload: dict[str, Any],
    related: list[SimilarHit],
    removed_related: list[SimilarHit],
    forced: bool,
    acknowledged: list[str],
    credentials_acknowledged: list[str],
    user_claims_acknowledged: list[str],
    active_snapshot: list[Memory] | None = None,
) -> dict[str, Any]:
    """Persist the memory, record the commit event, return the
    committed response. Surfaces the ambient long-body warning as a
    non-blocking advisory when applicable."""
    category_enum: Category = payload["category"]
    try:
        memory, supersession = _persist(deps, payload, active_snapshot=active_snapshot)
    except OSError as exc:
        # Disk-level failure (ENOSPC/EIO/EACCES) in the durable write.
        # Translate to ValueError so the MCP boundary returns a clean
        # structured error rather than leaking the bare OSError's absolute
        # path to the client — matching the sibling lifecycle handlers
        # (remove/restore/verify/rename_scope).
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
        # Conditional so the default write event keeps its exact shape —
        # a populated field in the log means claims were actually
        # declared, which is the only telemetry the backfill pass and a
        # future coverage measurement can be built from.
        **({"claims": list(memory.claims)} if memory.claims else {}),
        **supersession.event_fields(),
    )
    return deps.responses.committed(
        memory,
        related=related,
        removed_related=removed_related,
        warnings=warnings,
        **supersession.response_fields(),
    )


__all__ = ["DESC_MEMORY_WRITE", "memory_write"]
