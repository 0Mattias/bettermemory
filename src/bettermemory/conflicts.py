"""Corpus-level contradiction candidates: detection + verdict queue.

The store has always been able to represent a contradiction (the
`contradicts` link type) and to *hear about* one (`record_use
outcome=contradicted` — the model vs. the world at use time). What it
could not do is NOTICE one on its own: two stored memories disagreeing
with EACH OTHER sat unflagged until one of them happened to be
retrieved and judged. The dedup passes even computed the evidence and
threw it away — `polarity_skipped` pairs surfaced on a curation
report and died with it, re-derived and re-shown every run.

This module holds both halves of corpus-level contradiction detection.
The DETECTION is mechanical: the pairwise dedup scan below (high
similarity plus a signal from `_conflict_signal` — a polarity flip or a
numeric divergence; `_find_dedup_with_skips` is the entry point); the
JUDGMENT stays with the model (the `memory_conflicts` MCP tool lists
pending pairs and takes a verdict). The queue is the ONLY exit for a
flagged pair: the signal is consulted inside `_pick_keeper`, which
raises rather than crowning a keeper, so no dedup path — including an
unattended one — can tombstone a side instead of filing it here. The
split mirrors the architecture
everywhere else in this codebase: the server does the corpus-scale
mechanical work no conversation would ever do by hand, the calling
model does the semantics.

Verdict lifecycle, designed for convergence (a dismissed pair must
never haunt every future scan):

- ``pending``: detected, awaiting judgment. Re-detection refreshes
  similarity/summaries but never duplicates the row (stable pair id).
- ``confirmed``: the model judged it a real contradiction; a
  `contradicts` link now ties the pair (written by the handler BEFORE
  the verdict is stamped, so the link-write's `updated` bump cannot
  re-trigger anything). Terminal: the link is the durable artifact,
  retrieval surfaces it on both memories, and re-arbitrating it would
  add nothing.
- ``dismissed``: the model judged the pair compatible. Any standing
  `contradicts` link between the pair is cleared by the handler first
  (same before-the-stamp ordering as the confirm side), so the queue
  and the link layer cannot end up disagreeing about the same pair.
  The verdict also records a fingerprint of each member's BODY as it
  stood when it was judged (`verdict_hash_a` / `verdict_hash_b`).
  Sticky across scans — UNLESS a member's body stops hashing to what
  the verdict judged, in which case that content no longer exists and
  the pair resurrects as pending.

  Keying the resurrect rule on content rather than on `updated` is what
  keeps a dismissal from reappearing for reasons that have nothing to
  do with its own pair. Both arbitration paths REWRITE memories — the
  confirm path adds a `contradicts` link, the dismiss path strips one —
  and each rewrite bumps `updated` on a memory that may well sit in
  other queued pairs too (near-identical bodies cluster, so one memory
  routinely has several partners). Under an `updated`-keyed rule,
  arbitrating one pair silently re-queued every dismissed pair sharing
  a member with it. A body hash cannot mistake a link edit for a claim
  edit. Rows dismissed before the hashes existed carry none and fall
  back to the old `updated > verdict_ts` rule until they are dismissed
  again.

Rows whose members stop being active (tombstoned, merged) are dropped
on the next full-corpus upsert — a conflict with a dead side is moot.
`upsert_scan` is the ONLY garbage collector, which is why every
applying pass calls it unconditionally: a pass that only
upserted when it had fresh candidates would strand those rows in the
file indefinitely. Until a scan collects one, no surface that counts
arbitration WORK advertises it — `split_judgeable` is the shared filter
both the `memory_conflicts` listing and `memory_scope_overview`'s
`curation_pending.conflicts` count run their rows through — but each of
them re-pays a liveness check on it every time they report. The counts
that DO still see such a row say so in their names — `upsert_scan`'s
`pending_rows_on_disk` and `conflicts_pending_count` — because both
count the file, and a name shared with a judgeable count would make one
response answer "how many pending?" two ways.

Collecting needs the caller's snapshot to be COMPLETE, not merely
full-corpus. `Store.load_all` skips any file it cannot parse
(`PARSE_SKIP_EXCEPTIONS` is `(Exception,)` — a truncated write, a bad
`chmod`, a mid-tombstone race), so "absent from the snapshot" is on its
own evidence of a bad read and not of a death, and GC is permanent: a
dropped row takes its status, `verdict_ts`, `note` and body hashes with
it, and re-detection can only ever re-file the pair as `pending`. So
`upsert_scan` compares the snapshot against the number of active `.md`
files under the root and, when it holds fewer, merges and refreshes as
usual but collects NOTHING and reports `gc_deferred`. See
`_snapshot_is_complete` for what else can trip that comparison.

On-disk: ``<root>/.conflicts.jsonl`` — one JSON object per line,
0o600, atomically rewritten under a per-file ``flock`` (the
`memory_conflicts` handler and an unattended scan can race across
processes).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from .models import Memory, snippet_for, utcnow
from .search import _pairwise_content_jaccard, _raw_content_token_set
from .supersession import _numeric_token_set, _strip_provenance
from .time_utils import parse_event_ts

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("bettermemory.conflicts")

CONFLICTS_FILENAME = ".conflicts.jsonl"

_VALID_STATUSES = ("pending", "confirmed", "dismissed")


def _body_hash(body: str) -> str:
    """Fingerprint of one member's body, as a verdict judged it.

    The resurrect rule's key. Deliberately over the RAW body with no
    normalisation: the question is "is this still the text the model
    ruled on", and a rewrite that only moved whitespace is still a
    rewrite the verdict never saw. Erring toward re-arbitration matches
    the direction the old `updated`-keyed rule erred in, so nothing that
    used to re-queue silently stops.

    Truncated to 64 bits — this compares a body against its own earlier
    self, not against an attacker-chosen one, and the queue row it lives
    on is already inside the store's 0o600 trust boundary."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _pair_id(a_id: str, b_id: str) -> str:
    """Stable, order-independent id for a memory pair. Detector is
    deliberately NOT part of the key: a pair flagged by both the
    polarity and the numeric detector is one disagreement to arbitrate,
    not two."""
    lo, hi = sorted((a_id, b_id))
    return "cf-" + hashlib.sha256(f"{lo}:{hi}".encode("utf-8")).hexdigest()[:12]


@dataclass
class ConflictCandidate:
    """One suspected memory-vs-memory contradiction awaiting (or past)
    judgment. `detector` says WHY the pair was flagged: ``"polarity"``
    (negation flip between near-identical bodies), ``"numeric"``
    (near-identical bodies whose number-bearing tokens diverge — ports,
    versions, dates), or ``"value"`` (filed by `memory_write` when the
    new body and a stored claim share a subject and diverge on a value
    with no change cue to say which is current — see `supersession`;
    that path labels a pair of numbers ``"numeric"`` too, so a port or
    version pair reads the same whichever producer queued it).

    `verdict_hash_a` / `verdict_hash_b` are `_body_hash` of each member
    as the verdict judged it — the resurrect rule's key on a dismissed
    row (see the module docstring). `None` on a pending row, and `None`
    on a row whose verdict predates the field or whose member was
    already gone at verdict time, in which case that side falls back to
    the `updated > verdict_ts` rule."""

    id: str
    a_id: str
    b_id: str
    summary_a: str
    summary_b: str
    similarity: float
    method: str
    detector: str
    created: str
    status: str = "pending"
    verdict_ts: str | None = None
    note: str | None = None
    verdict_hash_a: str | None = None
    verdict_hash_b: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "a_id": self.a_id,
            "b_id": self.b_id,
            "summary_a": self.summary_a,
            "summary_b": self.summary_b,
            "similarity": round(self.similarity, 4),
            "method": self.method,
            "detector": self.detector,
            "created": self.created,
            "status": self.status,
        }
        if self.verdict_ts is not None:
            out["verdict_ts"] = self.verdict_ts
        if self.note is not None:
            out["note"] = self.note
        if self.verdict_hash_a is not None:
            out["verdict_hash_a"] = self.verdict_hash_a
        if self.verdict_hash_b is not None:
            out["verdict_hash_b"] = self.verdict_hash_b
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ConflictCandidate:
        status = str(raw.get("status", "pending"))
        if status not in _VALID_STATUSES:
            status = "pending"
        return cls(
            id=str(raw["id"]),
            a_id=str(raw["a_id"]),
            b_id=str(raw["b_id"]),
            summary_a=str(raw.get("summary_a", "")),
            summary_b=str(raw.get("summary_b", "")),
            similarity=float(raw.get("similarity", 0.0)),
            method=str(raw.get("method", "jaccard")),
            detector=str(raw.get("detector", "polarity")),
            created=str(raw.get("created", "")),
            status=status,
            verdict_ts=(
                str(raw["verdict_ts"]) if raw.get("verdict_ts") is not None else None
            ),
            note=(str(raw["note"]) if raw.get("note") is not None else None),
            verdict_hash_a=(
                str(raw["verdict_hash_a"])
                if raw.get("verdict_hash_a") is not None
                else None
            ),
            verdict_hash_b=(
                str(raw["verdict_hash_b"])
                if raw.get("verdict_hash_b") is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Detection: the pairwise dedup scan and its contradiction guards
# ---------------------------------------------------------------------------
#
# Moved here whole from the retired consolidate module. The scan still
# computes both halves of its old report, the dedup candidates and the
# pairs the guards kept out of them, because the guard is a property
# of the keeper decision (`_pick_keeper` raises rather than crowning a
# keeper) and the skipped list is what this module files.

_DEFAULT_JACCARD_THRESHOLD = 0.75


@dataclass
class DedupCandidate:
    """One pair of memories proposed for dedup. `keeper_id` is kept;
    `duplicate_id` is the one proposed for tombstoning."""

    keeper_id: str
    keeper_summary: str
    duplicate_id: str
    duplicate_summary: str
    similarity: float
    method: str  # always "jaccard" since 4.0.0; kept for report shape

    def to_dict(self) -> dict[str, Any]:
        return {
            "keeper_id": self.keeper_id,
            "keeper_summary": self.keeper_summary,
            "duplicate_id": self.duplicate_id,
            "duplicate_summary": self.duplicate_summary,
            "similarity": round(self.similarity, 4),
            "method": self.method,
        }


@dataclass
class PolaritySkippedPair:
    """A pair whose similarity cleared the dedup threshold but whose
    bodies disagree in a way that makes merging wrong. Two detectors
    populate the list (`detector` says which):

    - ``"polarity"``: the bodies differ in negation polarity. Stopword
      stripping makes the negation invisible to the token sets, so a
      high similarity here usually labels a contradiction as a
      duplicate.
    - ``"numeric"``: near-identical bodies whose number-bearing tokens
      DIVERGE on both sides ("port 5432" vs "port 5433", version
      3.27.0 vs 3.27.1). Token overlap on everything else pushes the
      pair over the threshold, and a silent merge would tombstone one
      of two claims that disagree about a value — a mis-curation, not
      a dedup.

    Either way the pair is a disagreement to arbitrate, not a duplicate
    to merge; the guard keeps it out of `dedup_candidates` and the
    conflict flow (`memory_conflicts` / `conflicts.scan_conflicts`)
    takes it from here. The skip is surfaced rather than swallowed
    because both detectors also catch benign cases (an incidental
    negator; an added-detail number) that a human/model reviewer should
    be able to wave through. Suggest-only: the apply path iterates
    `dedup_candidates` exclusively and never tombstones a member of
    this list. No keeper/duplicate roles — no merge decision was made.
    """

    memory_id_a: str
    summary_a: str
    memory_id_b: str
    summary_b: str
    similarity: float
    method: str  # always "jaccard" since 4.0.0; kept for report shape
    # Additive (3.28.0): rows serialized before the field default to
    # "polarity", the only detector that existed.
    detector: str = "polarity"

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id_a": self.memory_id_a,
            "summary_a": self.summary_a,
            "memory_id_b": self.memory_id_b,
            "summary_b": self.summary_b,
            "similarity": round(self.similarity, 4),
            "method": self.method,
            "detector": self.detector,
        }


class ConflictingPair(Exception):
    """`_pick_keeper` refusing a pair that carries a contradiction
    signal: there is no keeper to crown, because the pair is a
    disagreement to arbitrate rather than a duplicate to merge.

    `detector` names the signal — ``"polarity"`` or ``"numeric"``, the
    same vocabulary `PolaritySkippedPair.detector` and the conflict
    queue use — so the catching loop can report WHY without re-running
    detection.

    An exception rather than a sentinel return on purpose. This is the
    fence that keeps the unattended pass off contradictions, and the
    two dedup loops are not its only conceivable callers; a `None` a
    future caller forgets to check would tombstone one side of a
    contradiction silently, whereas an unhandled raise cannot be
    mistaken for a keeper.
    """

    def __init__(self, detector: str) -> None:
        super().__init__(f"contradiction signal ({detector}) — pair has no keeper")
        self.detector = detector


def _pick_keeper(
    a: Memory,
    b: Memory,
    *,
    signals_a: _BodySignals | None = None,
    signals_b: _BodySignals | None = None,
) -> tuple[Memory, Memory]:
    """Decide which memory wins a dedup pair.

    Raises `ConflictingPair` FIRST, before any tier below runs, when
    the two bodies carry a contradiction signal (`_conflict_signal`:
    negation polarity flip or mutual numeric divergence). Every
    `DedupCandidate` in this module is constructed from this function's
    return value, so routing conflict-shaped pairs to the conflict
    queue instead of the tombstone list is a property of the keeper
    decision itself rather than of a check each loop remembers to make;
    the distinction matters because an unattended pass applies its
    candidates with nobody reviewing the diff, and a similarity
    threshold is no defence
    (the inverse-clause pair "Deploy with the blue-green strategy;
    never do in-place." vs its swap measures Jaccard 1.0).

    `signals_a` / `signals_b` are the caller's precomputed
    `_body_signals` for the two bodies — a cache, not a gate: omitting
    them costs one tokenisation pass per body and changes no outcome,
    so a caller cannot disarm the fence by forgetting them.

    Tier 0: when exactly one member carries verification attestation
    (non-empty `verified_paths` or a set `last_verified_at`), it wins
    outright. Safe because content edits deliberately reset
    verification (`Store.update`), so an attested body is by
    construction the spot-checked one. Without this tier the
    "attestation is authority" rule below is unreachable on real
    microsecond-distinct timestamps, and a metadata-only retag (the
    retired demotion pass was one) would bump `updated` and crown
    an unattested ambient husk over the verified fact.
    Tier 1: more-recently-updated wins. Refining a memory implies
    that's the canonical version. Tier 2 (tie on `updated`): more
    `verified_paths` wins — attestation is authority. Tier 3 (tie on
    both): higher ULID wins — newer creation under
    microsecond-tied writes. Returns `(keeper, duplicate)`.
    """
    sig_a = signals_a if signals_a is not None else _body_signals(a.body)
    sig_b = signals_b if signals_b is not None else _body_signals(b.body)
    detector = _conflict_signal(sig_a, sig_b)
    if detector is not None:
        raise ConflictingPair(detector)
    a_attested = bool(a.verified_paths) or a.last_verified_at is not None
    b_attested = bool(b.verified_paths) or b.last_verified_at is not None
    if a_attested != b_attested:
        return (a, b) if a_attested else (b, a)
    if a.updated != b.updated:
        return (a, b) if a.updated > b.updated else (b, a)
    a_verified = len(a.verified_paths or [])
    b_verified = len(b.verified_paths or [])
    if a_verified != b_verified:
        return (a, b) if a_verified > b_verified else (b, a)
    return (a, b) if a.id > b.id else (b, a)


def _find_dedup_with_skips(
    memories: list[Memory],
    *,
    threshold: float | None = None,
) -> tuple[list[DedupCandidate], list[PolaritySkippedPair], str]:
    """The pairwise Jaccard dedup scan: the candidate pairs plus the
    pairs the contradiction guards kept out of them (see
    `PolaritySkippedPair`). Both lists are sorted descending by
    similarity. `threshold` defaults to 0.75, the calibration the
    write-time dedup path uses."""
    if len(memories) < 2:
        return [], [], "jaccard"

    method = "jaccard"
    eff_threshold = threshold if threshold is not None else _DEFAULT_JACCARD_THRESHOLD
    candidates, polarity_skipped = _find_dedup_jaccard(
        memories, threshold=eff_threshold
    )

    candidates.sort(key=lambda c: c.similarity, reverse=True)
    polarity_skipped.sort(key=lambda p: p.similarity, reverse=True)
    return candidates, polarity_skipped, method


# Negation tokens that flip a body's polarity. Both dedup paths are
# blind to negation: the Jaccard tokenizer strips these as stopwords,
# so "Do not use sudo" and "Use sudo" reduce to IDENTICAL token sets
# (Jaccard 1.0) — above even the unattended 0.90 threshold, with zero
# headroom for the threshold to save it — and sentence-embedding
# models routinely score a negated pair above the 0.85 cosine
# threshold too. A negated pair is a semantic contradiction requiring
# judgment, which no unattended pass may take; the pair belongs to the
# contradiction flow (`record_use outcome=contradicted` / the conflict
# queue below), not dedup, regardless of which similarity method
# surfaced it. Word-order reversals ("A
# proxies to B" vs "B proxies to A") would need a positional/bigram
# signal — out of scope for this guard.
_NEGATION_MARKERS = frozenset(
    {
        "no",
        "not",
        "never",
        "none",
        "neither",
        "nor",
        "without",
        "cannot",
        # Apostrophe-stripped contractions ("don't" -> "dont").
        "dont",
        "doesnt",
        "didnt",
        "wont",
        "cant",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "shouldnt",
        "wouldnt",
        "couldnt",
    }
)

_NEGATION_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _has_negation(body: str) -> bool:
    """True when the body carries a grammatical-negation token.

    Tokenizes WITHOUT stopword stripping (the whole point — the shared
    dedup tokenizer `search._raw_content_token_set` drops the negators)
    and normalizes apostrophes away so contracted forms ("don't",
    "won't") match their stripped spellings in `_NEGATION_MARKERS`.
    """
    normalized = body.lower().replace("’", "").replace("'", "")
    return any(
        token in _NEGATION_MARKERS for token in _NEGATION_TOKEN_RE.findall(normalized)
    )


def _numeric_divergence(nums_a: frozenset[str], nums_b: frozenset[str]) -> bool:
    """True when BOTH sides carry number-bearing tokens the other lacks.

    One-sided difference is additional detail, not disagreement:
    "deployed v3 on 2026-07-20" vs "deployed v3" merges fine. Mutual
    difference on bodies similar enough to clear the dedup threshold —
    "port 5432" vs "port 5433" — is two claims disagreeing about a
    value, and `_pick_keeper` would tombstone one of them on recency
    rather than truth. That pair belongs to the conflict flow."""
    return bool(nums_a - nums_b) and bool(nums_b - nums_a)


# Clause boundaries for the ORDER-SENSITIVE half of the polarity guard.
# `_has_negation` is whole-body token presence and therefore order-blind:
# "Deploy with the blue-green strategy; never do in-place." and its exact
# inverse both contain "never", so the whole-body rule sees matching
# polarity while the token sets measure Jaccard 1.0 — a pair the
# unattended 0.90 threshold could not save. Scoping negation to the
# clause it sits in recovers the order the token sets threw away.
#
# Sentence/clause terminators only. Commas are deliberately NOT
# boundaries: a negator scopes across a comma list ("do not use A, B, or
# C"), and splitting there would file B and C as ASSERTED and invent a
# flip against a body that agrees.
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?\n]+")


def _clause_polarity(body: str) -> tuple[frozenset[str], frozenset[str]]:
    """`(asserted, negated)` content tokens, scoped per clause.

    A clause carrying any `_NEGATION_MARKERS` token negates every
    content token in it; the rest are asserted. Tokenisation is the
    dedup tokeniser (`_raw_content_token_set`) so the sets are directly
    comparable to the ones the similarity score is computed over.

    Tokens appearing on BOTH sides within one body are dropped from both
    returned sets: a body that both asserts and negates a term ("use
    sudo for deploys; never use sudo for backups") makes no comparable
    polarity claim about it, and counting it would flip that body
    against any body that merely asserts the term.
    """
    asserted: set[str] = set()
    negated: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(body):
        tokens = _raw_content_token_set(clause)
        if not tokens:
            continue
        if _has_negation(clause):
            negated |= tokens
        else:
            asserted |= tokens
    return frozenset(asserted - negated), frozenset(negated - asserted)


class _BodySignals(NamedTuple):
    """Every contradiction-guard input for ONE body, computed once.

    Held per memory by the dedup loops (the pairwise comparison is
    O(N²) and re-tokenising inside it would be too), and computed
    on demand by `_pick_keeper` for callers that don't have them.
    All four fields judge the provenance-stripped body — see
    `_PROVENANCE_RE` for why the stamp is not claim content.
    """

    has_negation: bool
    asserted: frozenset[str]
    negated: frozenset[str]
    numbers: frozenset[str]


def _body_signals(body: str) -> _BodySignals:
    """Contradiction-guard inputs for a raw (still-stamped) body."""
    stripped = _strip_provenance(body)
    asserted, negated = _clause_polarity(stripped)
    return _BodySignals(
        has_negation=_has_negation(stripped),
        asserted=asserted,
        negated=negated,
        numbers=_numeric_token_set(stripped),
    )


def _polarity_flip(sig_a: _BodySignals, sig_b: _BodySignals) -> bool:
    """True when two bodies disagree in negation polarity. Two rules:

    1. **Whole-body**: exactly one side carries a negator at all. The
       original guard, and still the only one that fires when the
       negated claim shares no tokens with the other body ("It is fast"
       vs "It is not slow").
    2. **Clause-scoped, mutual**: each body asserts a term the other
       negates. Mutuality mirrors `_numeric_divergence`'s rule and for
       the same reason — a one-sided difference is usually scope, not
       disagreement. "Run migrations with the CLI, not by hand." negates
       its whole clause (a comma is not a boundary), so it one-sidedly
       "negates" `cli` against a body that asserts it; requiring the
       mirror keeps that agreeing pair merging as before, while the
       inverse-clause pairs this rule exists for — "Always squash-merge;
       do not rebase." vs "Never squash-merge; always rebase." — swap in
       both directions by construction.

    Documented gap: a pair where BOTH bodies carry a negator and only
    ONE term swaps polarity passes both rules. Reaching the dedup
    threshold at all takes near-identical token sets, which makes the
    unmirrored shape hard to construct, but it is a gap and not a proof.
    """
    if sig_a.has_negation != sig_b.has_negation:
        return True
    return bool(sig_a.negated & sig_b.asserted) and bool(sig_b.negated & sig_a.asserted)


def _conflict_signal(sig_a: _BodySignals, sig_b: _BodySignals) -> str | None:
    """The detector name for a pair that must NOT be merged, or None.

    The single definition of "this is a disagreement, not a duplicate",
    consulted from inside `_pick_keeper` so both dedup paths — and any
    future one — inherit it. Polarity is checked first: when a pair
    trips both, the negation is the more legible frame for the reviewer.
    """
    if _polarity_flip(sig_a, sig_b):
        return "polarity"
    if _numeric_divergence(sig_a.numbers, sig_b.numbers):
        return "numeric"
    return None


def _polarity_skip(
    a: Memory, b: Memory, similarity: float, method: str, detector: str = "polarity"
) -> PolaritySkippedPair:
    """Build the report entry for a pair a conflict guard skipped.
    Shared by both dedup paths (and both detectors) so the surfaced
    shape can't drift."""
    return PolaritySkippedPair(
        memory_id_a=a.id,
        summary_a=snippet_for(a.body, max_chars=100),
        memory_id_b=b.id,
        summary_b=snippet_for(b.body, max_chars=100),
        similarity=similarity,
        method=method,
        detector=detector,
    )


def _find_dedup_jaccard(
    memories: list[Memory], *, threshold: float
) -> tuple[list[DedupCandidate], list[PolaritySkippedPair]]:
    # Pre-compute RAW token sets (and polarity) once per memory, over
    # the provenance-stripped body — the stamp is shared boilerplate,
    # not claim content, and polarity likewise judges the claim, not
    # the quoted transcript turn (see `_PROVENANCE_RE`). Kebab
    # expansion happens per PAIR inside `_pairwise_content_jaccard` —
    # a compound the pair shares must stay one token (symmetric
    # expansion of a shared compound strictly inflates Jaccard; see
    # the helper's docstring), so it can't be precomputed per memory.
    token_sets: list[tuple[Memory, set[str], _BodySignals]] = []
    for m in memories:
        token_sets.append(
            (
                m,
                _raw_content_token_set(_strip_provenance(m.body)),
                _body_signals(m.body),
            )
        )
    out: list[DedupCandidate] = []
    skipped: list[PolaritySkippedPair] = []
    for i in range(len(token_sets)):
        m_i, t_i, sig_i = token_sets[i]
        if not t_i:
            continue
        for j in range(i + 1, len(token_sets)):
            m_j, t_j, sig_j = token_sets[j]
            if not t_j:
                continue
            sim = _pairwise_content_jaccard(t_i, t_j)
            if sim < threshold:
                continue
            try:
                keeper, duplicate = _pick_keeper(
                    m_i, m_j, signals_a=sig_i, signals_b=sig_j
                )
            except ConflictingPair as conflict:
                # A contradiction signal, not a duplicate — `_pick_keeper`
                # refuses to crown a keeper, so no candidate exists to
                # tombstone. Surface the pair rather than dropping it: the
                # guards also catch genuine duplicates (an incidental
                # negator, an added-detail number) that a reviewer should
                # be able to wave through, and a bare `continue` hid those
                # from the report forever. Threshold filtering above keeps
                # the list small.
                skipped.append(
                    _polarity_skip(m_i, m_j, sim, "jaccard", detector=conflict.detector)
                )
                continue
            out.append(
                DedupCandidate(
                    keeper_id=keeper.id,
                    keeper_summary=snippet_for(keeper.body, max_chars=100),
                    duplicate_id=duplicate.id,
                    duplicate_summary=snippet_for(duplicate.body, max_chars=100),
                    similarity=sim,
                    method="jaccard",
                )
            )
    return out, skipped


def skip_to_candidate(pair: Any, *, created: str) -> ConflictCandidate:
    """Lift a `PolaritySkippedPair` (either detector) into a queue row.
    The one mapping every producer of a row goes through, so it can't
    drift."""
    return ConflictCandidate(
        id=_pair_id(pair.memory_id_a, pair.memory_id_b),
        a_id=pair.memory_id_a,
        b_id=pair.memory_id_b,
        summary_a=pair.summary_a,
        summary_b=pair.summary_b,
        similarity=pair.similarity,
        method=pair.method,
        detector=getattr(pair, "detector", "polarity"),
        created=created,
    )


def find_conflict_candidates(
    memories: list[Memory],
    *,
    threshold: float | None = None,
) -> list[ConflictCandidate]:
    """Run the dedup scan and lift its conflict-shaped skips into
    candidates. Pure detection — no queue I/O; `scan_conflicts` is the
    persistence wrapper."""
    _, skipped, _method = _find_dedup_with_skips(memories, threshold=threshold)
    now_iso = utcnow().isoformat()
    out: list[ConflictCandidate] = []
    seen: set[str] = set()
    for pair in skipped:
        cand = skip_to_candidate(pair, created=now_iso)
        if cand.id in seen:
            # Same pair via two detectors: keep the first (higher-
            # similarity ordering upstream makes it the stronger frame).
            continue
        seen.add(cand.id)
        out.append(cand)
    return out


class ConflictQueue:
    """The contradiction pairs awaiting a verdict, as rows of the store's
    `conflicts` table. Every change is one mutation row of the log, so a
    verdict is attested like any other write. Concurrent verdicts
    serialise on the store's write transaction: the loser re-reads the
    row inside its transaction, finds it resolved, and mutates nothing."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def load(self) -> list[ConflictCandidate]:
        out: list[ConflictCandidate] = []
        for raw in self.store.list_conflicts():
            try:
                out.append(ConflictCandidate.from_dict(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def pending(self) -> list[ConflictCandidate]:
        return [c for c in self.load() if c.status == "pending"]

    def file_pair(self, cand: ConflictCandidate, *, session: str | None = None) -> str:
        """Queue one pair the write path detected. Returns the row's
        status: the new row's `pending`, or the existing row's when the
        pair is already queued or judged, in which case nothing changes."""
        with self.store.batch():
            for row in self.load():
                if row.id == cand.id:
                    return row.status
            self.store.put_conflict(cand.to_dict(), session=session)
            return cand.status

    def upsert_scan(
        self,
        fresh: list[ConflictCandidate],
        memories_by_id: dict[str, Memory],
        *,
        session: str | None = None,
    ) -> dict[str, int]:
        """Fold a fresh scan into the queue: new pairs are added, pending
        rows refreshed, dismissed rows resurrected when a judged body
        changed since the verdict, and rows whose member is no longer
        active collected, provided the caller's snapshot covers the
        whole active set (`gc_deferred` says when it did not)."""
        with self.store.batch():
            current = self.load()
            before = {c.id: c.to_dict() for c in current}
            by_id = {c.id: c for c in current}
            added = resurrected = refreshed = 0
            for cand in fresh:
                existing = by_id.get(cand.id)
                if existing is None:
                    by_id[cand.id] = cand
                    added += 1
                    continue
                if existing.status == "pending":
                    existing.summary_a = cand.summary_a
                    existing.summary_b = cand.summary_b
                    existing.similarity = cand.similarity
                    existing.detector = existing.detector or cand.detector
                    refreshed += 1
                elif existing.status == "dismissed" and self._judged_content_changed(
                    existing, memories_by_id
                ):
                    existing.status = "pending"
                    existing.verdict_ts = None
                    existing.note = None
                    existing.verdict_hash_a = None
                    existing.verdict_hash_b = None
                    existing.summary_a = cand.summary_a
                    existing.summary_b = cand.summary_b
                    existing.similarity = cand.similarity
                    existing.created = cand.created
                    resurrected += 1
            collectable = len(memories_by_id) >= self.store.count_memories()
            if not collectable:
                log.warning(
                    "conflict-queue GC deferred: the caller's snapshot has %d "
                    "memories but the store holds %d",
                    len(memories_by_id),
                    self.store.count_memories(),
                )
            kept = [
                c
                for c in by_id.values()
                if not collectable
                or (c.a_id in memories_by_id and c.b_id in memories_by_id)
            ]
            dropped = len(by_id) - len(kept)
            self._replace_all(kept, before, session=session)
            return {
                "added": added,
                "resurrected": resurrected,
                "refreshed": refreshed,
                "dropped": dropped,
                "gc_deferred": 0 if collectable else 1,
                "pending_rows_on_disk": sum(1 for c in kept if c.status == "pending"),
            }

    @staticmethod
    def _judged_content_changed(
        cand: ConflictCandidate, memories_by_id: dict[str, Memory]
    ) -> bool:
        """Did either judged body change since the verdict? Per side the
        stored body hash decides; a side without one falls back to
        `updated > verdict_ts`."""
        verdict = parse_event_ts(cand.verdict_ts)
        for mid, judged in (
            (cand.a_id, cand.verdict_hash_a),
            (cand.b_id, cand.verdict_hash_b),
        ):
            m = memories_by_id.get(mid)
            if m is None:
                continue
            if judged is not None:
                if _body_hash(m.body) != judged:
                    return True
            elif verdict is None or m.updated > verdict:
                return True
        return False

    def resolve(
        self,
        candidate_id: str,
        *,
        status: str,
        note: str | None = None,
        member_bodies: dict[str, str] | None = None,
        before_stamp: Callable[[], None] | None = None,
        session: str | None = None,
    ) -> ConflictCandidate | None:
        """Stamp a verdict on a pending row: `confirmed` or `dismissed`,
        with the bodies as judged fingerprinted onto the row. Returns the
        stamped row, or None when no pending row has that id.
        `before_stamp` runs the caller's side effects inside the same
        transaction, after the pending re-check and before the stamp."""
        if status not in ("confirmed", "dismissed"):
            raise ValueError(
                f"verdict status must be 'confirmed' or 'dismissed', got {status!r}"
            )
        bodies = member_bodies or {}
        with self.store.batch():
            hit = next(
                (
                    c
                    for c in self.load()
                    if c.id == candidate_id and c.status == "pending"
                ),
                None,
            )
            if hit is None:
                return None
            if before_stamp is not None:
                before_stamp()
            hit.status = status
            hit.verdict_ts = utcnow().isoformat()
            hit.note = note
            body_a, body_b = bodies.get(hit.a_id), bodies.get(hit.b_id)
            hit.verdict_hash_a = None if body_a is None else _body_hash(body_a)
            hit.verdict_hash_b = None if body_b is None else _body_hash(body_b)
            self.store.put_conflict(hit.to_dict(), session=session)
            return hit

    def _replace_all(
        self,
        rows: list[ConflictCandidate],
        before: dict[str, dict[str, Any]],
        *,
        session: str | None,
    ) -> None:
        """Make the table hold exactly `rows`, writing only what changed
        against `before` so an unchanged queue appends no log row."""
        keep = {c.id: c.to_dict() for c in rows}
        for conflict_id in before:
            if conflict_id not in keep:
                self.store.delete_conflict(conflict_id, session=session)
        for conflict_id, record in keep.items():
            if before.get(conflict_id) != record:
                self.store.put_conflict(record, session=session)


def scan_conflicts(
    store: Store,
    memories: list[Memory],
    *,
    threshold: float | None = None,
    session: str | None = None,
) -> dict[str, int]:
    """Detect the contradiction pairs in `memories` and fold them into
    the store's queue; returns the upsert counters."""
    fresh = find_conflict_candidates(memories, threshold=threshold)
    queue = ConflictQueue(store)
    return queue.upsert_scan(fresh, {m.id: m for m in memories}, session=session)


def split_judgeable(
    pending: Iterable[ConflictCandidate],
    is_active: Callable[[str], bool],
) -> tuple[list[ConflictCandidate], int]:
    """Split pending rows into the judgeable ones and a count of the rest.

    The single definition of "this queued row is real arbitration
    work": both members still active. A row failing it cannot be ruled
    on at all — `_load_active_member` refuses the verdict and the
    remedy is a scan, not a judgment — so a surface that counts it
    advertises work that resolves to nothing, and a cue that keeps
    resolving to nothing teaches the model to stop following it.

    Every surface that reports a pending count runs its rows through
    here: `memory_conflicts`'s `pending_total` (and the list beside it)
    and `memory_scope_overview`'s `curation_pending.conflicts`. They
    hold different liveness authorities — per-row `store.load_one` vs.
    membership in the full-corpus `load_all` snapshot the overview
    already paid for — which is exactly why the *predicate* is shared
    rather than each surface re-deriving it: the two counts point at
    each other, and the whole point of the fix is that they agree.
    (The two authorities coincide by construction: `load_one` walks the
    same active set `load_all` materialises and refuses anything
    `load_all` skips.)

    `is_active` is called left-to-right and short-circuits, so an
    authority that pays real I/O per member does not price the second
    side of an already-dead pair. Input order is preserved (callers
    sort by similarity before windowing), and the whole iterable is
    consumed — a caller that renders only a window still gets a total
    over everything.

    Report-only: nothing is GC'd here. `upsert_scan` stays the queue's
    only collector, ruling on liveness from one full-corpus snapshot
    under the file lock.
    """
    judgeable: list[ConflictCandidate] = []
    omitted = 0
    for cand in pending:
        if is_active(cand.a_id) and is_active(cand.b_id):
            judgeable.append(cand)
        else:
            omitted += 1
    return judgeable, omitted


def conflicts_pending_count(store: Store) -> int:
    try:
        return len(ConflictQueue(store).pending())
    except Exception:  # noqa: BLE001 - a corrupt queue must not break health
        log.warning("conflict queue unreadable in %s", store.path, exc_info=True)
        return 0


__all__ = [
    "ConflictCandidate",
    "ConflictQueue",
    "conflicts_pending_count",
    "find_conflict_candidates",
    "scan_conflicts",
    "split_judgeable",
]
