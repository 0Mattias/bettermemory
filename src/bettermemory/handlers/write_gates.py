"""The write-gate chain `memory_write` runs before the review.

`GateContext`, the `Continue` and `Reject` results, the `WriteGate` base,
its eight gates and `_WRITE_GATES`, the order they run in; the user-claim
detector (`_find_user_claims`, which `memory_update` shares) with its
sentence and list patterns; and the shared chain, `GateDeps`, `GateBundle`
and `apply_write_gates`. `handlers.write` re-exports every name here that
a caller imports from it, so no import path changes.

The gates live apart from `handlers.write` to keep each module under the
package's 2,000-line bound (ENG-96). `handlers.write` imports this module
and this module never imports it: beyond the standard library it imports
only modules `handlers.write` imported before the split, none of which
imports `handlers.write`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..credentials import find_credential_markers
from ..durability import (
    find_capture_rule_hits,
    find_transient_markers,
    in_quoted_span,
    quoted_spans,
)
from ..models import Category, Memory, SimilarHit
from ..scope_match import (
    collect_project_roots,
    collect_project_scopes,
    detect_scope_mismatch,
)
from ..search import find_similar, find_similar_tombstones
from .. import capture as _capture

if TYPE_CHECKING:
    from .._response import ResponseBuilder
    from ..config import Config
    from ..store import Store


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
# WriteGate strategy
# ---------------------------------------------------------------------------


@dataclass
class GateContext:
    """Bundle of inputs every gate evaluates against.

    The orchestrator builds one of these per call (post-payload-
    validation, post-origin-capture) and threads it through the gate
    chain. Mutable state (`related`, `removed_related`,
    `transient_hits`) is set by earlier gates so the commit can
    surface their findings: the committed response carries the related
    lists, and the commit event carries the acknowledged markers from
    `transient_hits` for the override-rate signal.
    """

    payload: dict[str, Any]
    force: bool
    acknowledge_transient: bool
    acknowledge_scope_mismatch: bool
    acknowledge_credential: bool
    acknowledge_user_claim: bool = False
    # The open batch a capture write's token named, or None for every other
    # write. Set, it turns on the capture gates; the orchestrator has
    # already cleared `force` and the acknowledge flags.
    capture: _capture.Batch | None = None
    # Outputs the gates accumulate as they pass — read by later gates
    # or the final commit step.
    credential_hits: list[Any] = None  # type: ignore[assignment]
    transient_hits: list[Any] = None  # type: ignore[assignment]
    user_claim_hits: list[Any] = None  # type: ignore[assignment]
    related: list[SimilarHit] = None  # type: ignore[assignment]
    removed_related: list[SimilarHit] = None  # type: ignore[assignment]
    # The active set `DedupActiveGate` loaded, kept so the review's related
    # set and the persist step's supersession detection read the same
    # snapshot instead of paying a second `load_all`. None when the gate
    # did not run (`force=True`); the review then loads it itself.
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

    Runs FIRST, before every other gate: the store is plain text whose
    signed log keeps every body, so persisting a live secret leaks it to
    disk, the audit log, and every copy — the highest-severity write to refuse,
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
                    "assignment). Describe the secret "
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
            response: dict[str, Any] = {"status": "duplicate"}
            deps.responses.add_similar(response, "matches", high)
            return Reject(
                response={
                    **response,
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
            response: dict[str, Any] = {"status": "previously_removed"}
            deps.responses.add_similar(response, "removed_matches", high_removed)
            return Reject(
                response={
                    **response,
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


class CaptureRulesGate(WriteGate):
    """The enforced half of the capture bar (`capture.CAPTURE_RULES`), on
    capture writes only, with no acknowledge flag: a plain subject, no
    to-dos, no counts that drift, absolute dates. Every rule a body breaks
    is named, with its number in the bar and the phrase, so the model can
    rephrase once for all of them."""

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
        if gc.capture is None:
            return Continue()
        hits = find_capture_rule_hits(gc.payload["content"])
        if not hits:
            return Continue()
        return Reject(
            response={
                "status": "capture_rule",
                "rules": [hit.to_dict() for hit in hits],
                "hint": (
                    "Not written. The body breaks the capture rules named by "
                    "number above. Rephrase it to meet them, or skip it; no "
                    "flag passes a capture rule."
                ),
            },
            event_kwargs={
                "status": "capture_rule",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "capture_batch": gc.capture.batch,
                "capture_rules": [hit.name for hit in hits],
            },
        )


class PreviouslyUndoneGate(WriteGate):
    """A capture write whose body a capture undo already reverted: the
    owner said no to that body once, so no capture writes it again. An
    exact match on the normalized body (`capture.body_hash`); near
    restatements are the dedup gates' to catch."""

    def evaluate(self, deps: GateDeps, gc: GateContext) -> GateResult:
        if gc.capture is None:
            return Continue()
        digest = _capture.body_hash(gc.payload["content"])
        if digest not in _capture.undone_hashes(deps.store):
            return Continue()
        return Reject(
            response={
                "status": "capture_refused",
                "rule": "previously_undone",
                "hint": (
                    "Not written. The owner undid a capture of this body "
                    "before; do not capture it again."
                ),
            },
            event_kwargs={
                "status": "capture_refused",
                "scopes": gc.payload["scopes"],
                "forced": False,
                "capture_batch": gc.capture.batch,
                "capture_rule": "previously_undone",
            },
        )


# Order matters: credential before everything so a secret is refused
# before any other gate records body-derived data in the event log;
# transient before dedup so the writer isn't routed to memory_update on a
# transient parent; user-claim next to transient because both classify the
# BODY, and before dedup for the same reason transient is (a re-categorized
# re-issue must not be routed to memory_update on a mis-filed parent);
# scope-mismatch before dedup so the writer doesn't get a
# duplicate hit on a memory tagged for a different scope. The two capture
# gates pass every write but a capture's; on a capture they run right
# after credential, so the bar is what a capture hears about first.
_WRITE_GATES: tuple[WriteGate, ...] = (
    CredentialGate(),
    CaptureRulesGate(),
    PreviouslyUndoneGate(),
    TransientGate(),
    UserClaimGate(),
    ScopeMismatchGate(),
    DedupActiveGate(),
    DedupTombstoneGate(),
)


# ---------------------------------------------------------------------------
# The shared chain
# ---------------------------------------------------------------------------
#
# `apply_write_gates` is the write policy, and `memory_write` (write.py) is its
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
