"""Write-time supersession: does a new statement replace a stored one?

The integrity benchmark (`bench/integrity`, results in
`docs/eval-results.md`) measured the gap this module closes. On every
one of its 24 supersession topics the store served the superseded value
in the top five with nothing to tell it apart from the current one, the
same as a store with no memory model at all: `superseded_by` renders
only `supersedes` links a caller sets, and no write path set one. The
model reading the hits had two facts and no way to prefer either.

The fix is at write time, where the new statement and the stored one
are both in hand. `detect_supersession` compares a candidate body
against the active set and returns two lists:

- `supersedes`: stored memories the new body replaces. The caller sets
  a `supersedes` link from the new memory to each, which is exactly
  the edge `memory_search` already renders as `superseded_by` on the
  stale hit and on nothing else — the informative shape the benchmark
  scores (a field every hit carries distinguishes nothing).
- `conflicts`: stored memories the new body disagrees with when
  nothing in it says which side is current. The caller files the pair
  for `memory_conflicts`; the judgment stays with the model, as it
  does for the corpus scan in `consolidate`.

THE RULE. Three questions per stored memory, all lexical, all
deterministic — the project ships no models and this runs inside every
`memory_write`:

1. Same subject? The pairwise Jaccard the dedup gate computes
   (`search._pairwise_content_jaccard`) is at least `MIN_JACCARD`.
   Low on purpose: an update rarely restates its subject the way the
   original did ("Paging moved to Opsgenie" against "On-call pages are
   sent through PagerDuty"), so the bar is a floor and the evidence
   below carries the decision.
2. Different value? Each side carries a value-shaped token the other
   lacks: a number, a joined compound (`deploy-gateway`, `prod-hlx-1`),
   a proper noun, or on the new side the token that follows a change
   cue ("switched to **yarn**"). Mutual, like the numeric-divergence
   guard in `consolidate`: a one-sided extra is detail, not
   disagreement.
3. Same slot? Either the two values are kin — numerics of one shape
   (`8443` / `9443`, `3.11` / `3.13`) or compounds sharing half their
   parts (`runners-medium` / `runners-large`) — or each value's
   neighbourhood in its own body is present in the other body (the
   old value's context in the new body AND the new value's context in
   the old), with change-cue vocabulary excluded from that context
   because "moved" and "the previous" are what every update shares.

A new body that carries a change cue (`CHANGE_CUES`: moved, switched,
renamed, raised, no longer, the previous, again, ...) and passes all
three supersedes the stored memory. One that passes without a cue is a
conflict when the overlap is high (`MIN_CONFLICT_JACCARD`) or the
values are kin; below that it is two statements about the same
subject, which every store holds by the hundred.

CLAIM-SIZED ONLY. A `supersedes` link says "prefer this record over
that one", which is only coherent between single claims. Both bodies
must be at most `MAX_CLAIM_TOKENS` content tokens and
`MAX_CLAIM_SENTENCES` sentences; a session record or a ruling is never
a candidate on either side, however many change cues it carries.

MEASURED, not tuned by eye. On the sealed corpus's ingestion order the
rule links 27 of the 40 update statements to the statement each
replaces (15 of 24 supersession updates, 7 of 8 first reversions, 5 of
8 second reversions), files 1 cue-less reversion as a conflict, and
sets nothing on any distractor, hard negative, reversion that agrees
with its target, or cross-topic pair — `tests/test_supersession.py`
pins those counts. Replayed over the maintainer's live store (339
memories, four of them claim-sized) it fires nothing. The misses are
lexical by construction: an update whose old value is a plain word
the shape rules cannot see (`prettier`, `pnpm`) or whose subject is
restated with no token in common.

THE LEVER. Anything admitted through the write API can carry a change
cue, so a false statement about a stored subject can earn a
`supersedes` link over the true one — the corpus's `p01` does. The
link note names the cue and both values so the reader can judge, the
event log records every link the detector set, and the arrangement is
still the better trade: without it the false fact and the true one
sit side by side unsignaled, which the benchmark shows on every arm.
`SECURITY.md` carries the consequence.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .consolidate import _numeric_token_set, _strip_provenance
from .models import Memory, snippet_for
from .search import (
    _STOPWORDS,
    _kebab_parts,
    _pairwise_content_jaccard,
    _raw_content_token_set,
    tokenize,
)

# Both bodies must be single claims; see the module docstring.
MAX_CLAIM_TOKENS = 80
MAX_CLAIM_SENTENCES = 5
# Question 1: a floor on subject overlap for any candidate at all.
MIN_JACCARD = 0.10
# A cue-less divergent pair is a conflict on overlap alone above this,
# or on value kinship above the lower floor.
MIN_CONFLICT_JACCARD = 0.30
MIN_CONFLICT_KIN_JACCARD = 0.15
# Question 3, context evidence: tokens either side of a value, and how
# many of them must appear in the other body.
CONTEXT_WIDTH = 3
MIN_CONTEXT_SHARED = 2
# Links set per write. A statement replacing more than a handful of
# stored claims is not a single-claim update.
MAX_LINKS_PER_WRITE = 5

# Change-of-state cues. The value maps each cue to whether the new value
# follows it directly ("adopted biome") or after a preposition ("moved
# to port 9443", "now run through release-gateway"). Present tense and
# participles both, because updates are written both ways. Kept to
# phrases whose reading is a transition; "new" and "old" alone are not
# here (the transient gate owns "the new" as a marker, and "old" is
# durable in "old-style").
CHANGE_CUES: dict[str, bool] = {
    "no longer": False,
    "cut over": False,
    "cuts over": False,
    "rolled back": False,
    "instead of": False,
    "used to": False,
    "the old": False,
    "the previous": False,
    "the former": False,
    "back on": False,
    "once more": False,
    "again": False,
    "now": False,
    "moved": False,
    "moves": False,
    "moving": False,
    "switched": False,
    "switches": False,
    "switching": False,
    "migrated": False,
    "migrates": False,
    "migrating": False,
    "replaced": False,
    "replaces": True,
    "replacing": True,
    "renamed": False,
    "renames": False,
    "changed": False,
    "changes": False,
    "upgraded": False,
    "upgrades": False,
    "downgraded": False,
    "raised": False,
    "lowered": False,
    "bumped": False,
    "adopted": True,
    "adopts": True,
    "retired": False,
    "decommissioned": False,
    "deprecated": False,
    "withdrawn": False,
    "reverted": False,
    "previously": False,
    "formerly": False,
}

_CUE_RE = re.compile(
    r"\b("
    + "|".join(re.escape(p) for p in sorted(CHANGE_CUES, key=len, reverse=True))
    + r")\b"
)
# Prepositions that introduce the new value after a cue.
_VALUE_PREPS = frozenset(
    {"to", "through", "on", "in", "at", "with", "by", "via", "into", "onto"}
)
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-_/]*")
# A dot between two digits ("3.13", "2.4.0") is part of a version, not a
# sentence end.
_SENTENCE_RE = re.compile(r"(?:[!?\n]|(?<!\d)\.|\.(?!\d))+")
_CLAUSE_RE = re.compile(r"(?:[!?;:\n]|(?<!\d)\.|\.(?!\d))+")
_PROPER_NOUN_RE = re.compile(
    r"^(?:[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*|[A-Z]{2,}[A-Za-z0-9]*)$"
)
_INTEGER_RE = re.compile(r"\d+")
_DOTTED_RE = re.compile(r"\d+(?:\.\d+)+")

# Excluded from context evidence: the cue vocabulary plus the words
# that name the other side of a transition. Every update shares these,
# so they say nothing about which claim a value belongs to.
_CUE_TOKENS: frozenset[str] = frozenset(
    {tok for phrase in CHANGE_CUES for tok in tokenize(phrase)}
    | {"old", "previous", "new", "former", "prior", "legacy", "replacement", "again"}
)


@dataclass(frozen=True)
class SupersessionMatch:
    """One stored memory the new body replaces or disagrees with.

    `evidence` says which of question 3's tests carried it: `kin` (the
    values share a shape), `context` (each value's neighbourhood is in
    the other body), or `similarity` (a cue-less pair filed on overlap
    alone). `cue` is the first change cue in the new body, None for a
    conflict. `new_value` / `old_value` are surface spellings from the
    two bodies, for the link note and the response.
    """

    memory_id: str
    outcome: str
    evidence: str
    cue: str | None
    new_value: str
    old_value: str
    similarity: float
    summary: str

    def note(self) -> str:
        """The link note: why this edge exists, for the reader of the
        `superseded_by` annotation and for a future curator."""
        return (
            f"set at write time: {self.cue!r} in this memory, "
            f"{self.new_value!r} against {self.old_value!r} ({self.evidence})"
        )

    def detector(self) -> str:
        """The `memory_conflicts` detector label for a filed pair: the
        corpus scan's own ``numeric`` when both values are numbers, so a
        port or version pair reads the same whichever path queued it,
        and ``value`` otherwise."""
        shapes = (
            _numeric_shape(self.new_value.lower()),
            _numeric_shape(self.old_value.lower()),
        )
        return "numeric" if all(shapes) else "value"


@dataclass(frozen=True)
class SupersessionReport:
    """What `detect_supersession` found. `eligible` is False when the new
    body is not claim-sized, in which case both lists are empty and the
    caller knows the detector declined rather than found nothing."""

    eligible: bool
    supersedes: list[SupersessionMatch]
    conflicts: list[SupersessionMatch]


# ---------------------------------------------------------------------------
# Per-body features
# ---------------------------------------------------------------------------


def claim_sized(body: str) -> bool:
    """Single-claim shape: at most `MAX_CLAIM_TOKENS` content tokens and
    `MAX_CLAIM_SENTENCES` sentences (terminal punctuation or a line
    break). Semicolons and colons do not end a sentence here — a claim
    with two clauses is still one claim."""
    stripped = _strip_provenance(body)
    tokens = _raw_content_token_set(stripped)
    sentences = [s for s in _SENTENCE_RE.split(stripped) if s.strip()]
    return 0 < len(tokens) <= MAX_CLAIM_TOKENS and len(sentences) <= MAX_CLAIM_SENTENCES


def change_cues(body: str) -> list[str]:
    """The change cues in `body`, in order of appearance, lower-cased."""
    return [m.group(1) for m in _CUE_RE.finditer(body.lower())]


def _first_content_token(word: str) -> str | None:
    tokens = [t for t in tokenize(word) if t not in _STOPWORDS]
    return tokens[0] if tokens else None


def anchored_values(body: str) -> set[str]:
    """The token that fills the value slot after each change cue.

    For a cue that takes a preposition the slot is the first content
    token after the first of `_VALUE_PREPS` within three words of the
    cue ("moved to port **9443**" yields `port`; the number is caught by
    the shape rule, and `port` is exclusive to neither side). For a cue
    that takes a direct object it is the first content token after the
    cue. A cue with no slot in its clause yields nothing — "withdrawn
    after its queries proved too slow" names no value, and reading one
    off it is how a reversion that agrees with its target got linked
    in an earlier draft of this rule.
    """
    out: set[str] = set()
    low = body.lower()
    for m in _CUE_RE.finditer(low):
        cue = m.group(1)
        clause = _CLAUSE_RE.split(low[m.end() :], 1)[0]
        words = _WORD_RE.findall(clause)
        if CHANGE_CUES[cue]:
            candidates = words[:3]
        else:
            idx = next((i for i, w in enumerate(words[:3]) if w in _VALUE_PREPS), None)
            if idx is None:
                continue
            candidates = words[idx + 1 : idx + 4]
        for w in candidates:
            tok = _first_content_token(w)
            if tok:
                out.add(tok)
                break
    return out


def _proper_nouns(body: str) -> set[str]:
    """Tokens of capitalised words that are not clause-initial: product
    and service names (`Airflow`, `PagerDuty`), weekdays, acronyms. The
    first word of a clause is skipped because sentence case says
    nothing about it."""
    out: set[str] = set()
    for clause in _CLAUSE_RE.split(body):
        for word in clause.split()[1:]:
            bare = word.strip("(),'\"`")
            if _PROPER_NOUN_RE.match(bare):
                out.update(t for t in tokenize(bare) if t not in _STOPWORDS)
    return out


def value_tokens(body: str, raw_tokens: set[str]) -> set[str]:
    """Question 2's value-shaped tokens of a body: digit-bearing tokens
    (`consolidate._numeric_token_set`, length-capped so identifiers do
    not count), joined compounds, and proper nouns — each intersected
    with the body's own content tokens so a stopword never qualifies."""
    values = set(_numeric_token_set(body)) & raw_tokens
    values |= {t for t in raw_tokens if "-" in t and len(t) > 2}
    values |= _proper_nouns(body) & raw_tokens
    return values


def anchor_tokens(body: str, raw_tokens: set[str]) -> set[str]:
    """The subset of `value_tokens` strong enough to anchor a reference
    on their own: numbers, proper nouns, and compounds that carry a digit
    or have three or more parts (`billing-db-green`, `prod-hlx-2`). A
    two-part compound without a digit is dropped because ordinary
    hyphenated English ("on-call", "read-only") has that shape. The
    durability gate's anchored "the new" exemption reads this; the
    supersession detector keeps the wider set because there a value has
    to diverge from and slot-match another one before it counts."""
    out: set[str] = set()
    for tok in value_tokens(body, raw_tokens):
        if any(c.isdigit() for c in tok) or "-" not in tok:
            out.add(tok)
        elif len(_kebab_parts(tok)) >= 3:
            out.add(tok)
    return out


def _numeric_shape(token: str) -> str | None:
    if _INTEGER_RE.fullmatch(token):
        return f"int:{len(token)}"
    if _DOTTED_RE.fullmatch(token):
        return f"dotted:{token.count('.')}"
    return None


def values_are_kin(a: str, b: str) -> bool:
    """Two different values that fill one slot by their own shape: two
    integers of the same width, two dotted numerics with the same
    number of components, or two joined compounds sharing at least half
    the shorter one's parts (`hlx-exports-prod` / `hlx-exports-v2`,
    `eu-west-1` / `eu-central-1`). One shared part out of three is not
    kinship: `prod-hlx-1` and `hlx-exports-v2` share `hlx` and name
    different things."""
    if a == b:
        return False
    shape_a, shape_b = _numeric_shape(a), _numeric_shape(b)
    if shape_a is not None or shape_b is not None:
        return shape_a is not None and shape_a == shape_b
    parts_a, parts_b = _kebab_parts(a), _kebab_parts(b)
    if len(parts_a) < 2 or len(parts_b) < 2:
        return False
    shared = {p for p in parts_a if p not in _STOPWORDS} & set(parts_b)
    return len(shared) >= math.ceil(0.5 * min(len(parts_a), len(parts_b)))


def _content_stream(body: str) -> list[str]:
    return [t for t in tokenize(body) if t not in _STOPWORDS]


def _context(stream: list[str], value: str) -> set[str]:
    """The content tokens within `CONTEXT_WIDTH` of every occurrence of
    `value` in a body's token stream, less the value itself and the
    change-cue vocabulary."""
    out: set[str] = set()
    for i, tok in enumerate(stream):
        if tok == value:
            out.update(stream[max(0, i - CONTEXT_WIDTH) : i])
            out.update(stream[i + 1 : i + 1 + CONTEXT_WIDTH])
    out.discard(value)
    return out - _CUE_TOKENS


def _surface_forms(body: str) -> dict[str, str]:
    """Token -> the first surface spelling that produced it, so a note
    can say `release-gateway` rather than the stemmed token."""
    out: dict[str, str] = {}
    for word in _WORD_RE.findall(body):
        tok = _first_content_token(word)
        if tok is not None:
            out.setdefault(tok, word.strip(".,;:()"))
    return out


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


def detect_supersession(
    new_body: str,
    existing: Iterable[Memory],
    *,
    exclude_ids: Iterable[str] = (),
) -> SupersessionReport:
    """Compare `new_body` against `existing` (the active set) and report
    what it supersedes and what it conflicts with; see the module
    docstring for the rule. Pure: no store I/O, no clock.

    `exclude_ids` drops memories the caller has already ruled on (a
    target the writer declared explicitly). Matches are ordered by
    similarity, then id, and each list is capped at
    `MAX_LINKS_PER_WRITE`.
    """
    body = _strip_provenance(new_body)
    if not claim_sized(body):
        return SupersessionReport(eligible=False, supersedes=[], conflicts=[])
    new_raw = _raw_content_token_set(body)
    cues = change_cues(body)
    new_values_all = value_tokens(body, new_raw)
    if cues:
        new_values_all |= anchored_values(body) & new_raw
    new_stream = _content_stream(body)
    new_surface = _surface_forms(body)
    skip = set(exclude_ids)

    supersedes: list[SupersessionMatch] = []
    conflicts: list[SupersessionMatch] = []
    for memory in existing:
        if memory.id in skip:
            continue
        old_body = _strip_provenance(memory.body)
        if not claim_sized(old_body):
            continue
        old_raw = _raw_content_token_set(old_body)
        similarity = _pairwise_content_jaccard(new_raw, old_raw)
        if similarity < MIN_JACCARD:
            continue
        new_values = new_values_all - old_raw
        old_values = value_tokens(old_body, old_raw) - new_raw
        if not new_values or not old_values:
            continue

        kin = sorted(
            (a, b) for a in new_values for b in old_values if values_are_kin(a, b)
        )
        pair: tuple[str, str] | None = kin[0] if kin else None
        evidence = "kin" if kin else None
        if pair is None:
            old_stream = _content_stream(old_body)
            for b in sorted(old_values):
                if len(_context(old_stream, b) & new_raw) < MIN_CONTEXT_SHARED:
                    continue
                for a in sorted(new_values):
                    if len(_context(new_stream, a) & old_raw) >= MIN_CONTEXT_SHARED:
                        pair, evidence = (a, b), "context"
                        break
                if pair is not None:
                    break

        old_surface = _surface_forms(old_body)

        def _match(outcome: str, ev: str, a: str, b: str) -> SupersessionMatch:
            return SupersessionMatch(
                memory_id=memory.id,
                outcome=outcome,
                evidence=ev,
                cue=cues[0] if outcome == "supersedes" else None,
                new_value=new_surface.get(a, a),
                old_value=old_surface.get(b, b),
                similarity=round(similarity, 4),
                summary=snippet_for(old_body, max_chars=100),
            )

        if cues:
            if pair is not None and evidence is not None:
                supersedes.append(_match("supersedes", evidence, *pair))
            continue
        if similarity >= MIN_CONFLICT_JACCARD or (
            kin and similarity >= MIN_CONFLICT_KIN_JACCARD
        ):
            a, b = (
                pair
                if pair is not None
                else (sorted(new_values)[0], sorted(old_values)[0])
            )
            conflicts.append(_match("conflict", evidence or "similarity", a, b))

    def _order(m: SupersessionMatch) -> tuple[float, str]:
        return (-m.similarity, m.memory_id)

    supersedes.sort(key=_order)
    conflicts.sort(key=_order)
    return SupersessionReport(
        eligible=True,
        supersedes=supersedes[:MAX_LINKS_PER_WRITE],
        conflicts=conflicts[:MAX_LINKS_PER_WRITE],
    )


__all__ = [
    "CHANGE_CUES",
    "MAX_CLAIM_SENTENCES",
    "MAX_CLAIM_TOKENS",
    "MAX_LINKS_PER_WRITE",
    "MIN_CONFLICT_JACCARD",
    "MIN_JACCARD",
    "SupersessionMatch",
    "SupersessionReport",
    "anchor_tokens",
    "anchored_values",
    "change_cues",
    "claim_sized",
    "detect_supersession",
    "value_tokens",
    "values_are_kin",
]
