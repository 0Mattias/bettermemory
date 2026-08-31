"""Offline consolidation: dedup, demote, scope-typo + cold-scope suggestions.

The `bettermemory consolidate` CLI walks the store and proposes (or
applies, with `--apply`) four kinds of curation:

1. **Near-duplicate dedup.** Pairwise Jaccard similarity over the
   active set.
   Bodies are compared with any `--llm --from-transcript` provenance
   stamp stripped (`_PROVENANCE_RE`) — the stamp is system boilerplate
   shared by every fact distilled from the same transcript turn, not
   claim content. Pairs carrying a contradiction signal — a negation
   polarity flip ("Use X" vs "Do not use X" — token sets collapse once
   stopwords drop, and embedding models score negated pairs above
   threshold too) or a mutual numeric divergence ("port 5432" vs "port
   5433") — are skipped on BOTH paths: that's a contradiction to
   arbitrate, not a duplicate to merge. The refusal lives INSIDE
   `_pick_keeper` (it raises `ConflictingPair`), not at the call sites,
   so no dedup path can produce a `DedupCandidate` for such a pair
   however it was scored — the unattended pass has no threshold at
   which it could tombstone one side. Skipped pairs above the
   threshold surface on the report as `polarity_skipped` (suggest-only,
   never applied) and are lifted into the conflict queue by an applying
   pass, so a genuine duplicate caught by an incidental negator doesn't
   vanish silently. An attested member (non-empty `verified_paths` or a set
   `last_verified_at`) beats an unattested one; otherwise the pair's
   newer-`updated` member wins. The loser is proposed for tombstoning
   with reason ``"consolidate: near-duplicate of <keeper_id>,
   similarity=0.NN"``. Ties on `updated` go to the memory with more
   `verified_paths` attestation. When applying, the keeper first
   inherits the union of both scope lists, so a scope-disjoint
   duplicate can't silently vanish from one project's auto-scoped
   retrieval.

2. **Demote never-applied to ambient.** Shares `memory_health`'s
   `dead_weight` rule (one predicate — `health._is_dead_weight` —
   keeps this pass, the `dead_weight` bucket, and
   `memory_scope_overview`'s `dead` count in lockstep): memories whose
   latest maintenance touch (created / updated / last-verified)
   predates the window, with retrieval count greater than zero and
   applied count of zero. ONLY
   the `fact` and (default) None categories get retagged to `ambient`
   so they stop appearing in the dead-weight bucket on future health
   passes; their content stays available for retrieval. Ambient
   memories already get this treatment, so they're skipped, and
   `user-inference` (plus any future category) keeps its
   confirmation-protected tier — `memory_update` cannot restore that
   tag, so an automated retag would be one-way. Memories carrying an
   unresolved contradiction flag are skipped (they're parked for
   explicit resolution, not lacking value), as are memories whose
   earliest retrieval is too recent for the auto-applied endorsement
   window to have elapsed.

3. **Cold scope suggestions.** Scopes whose newest activity (created /
   updated / last-verified) is older than `cold_scope_days` AND which
   carry no `applied` events within that window get a "consider
   archiving" suggestion. All-ambient scopes are exempt (ambient
   value is implicit by design — zero applied events there is not an
   indictment), and the pass goes silent when the event log carries
   no applied events at all (absence of telemetry cannot distinguish
   dead scopes from healthy ones). Suggest-only — auto-archiving a
   whole scope is too blunt to apply without human review.

4. **Scope-typo pairs.** Scope pairs that plausibly look like typos,
   per the SAME neighbor rule as the rare-scopes detector in
   `health.py` (`_scope_typo_neighbor`: namespace-aware tails,
   sibling-suffix exemption, length-scaled distance). Only pairs whose
   lesser side holds a single memory are flagged — the same singleton
   gate as that detector (an established multi-memory scope is not
   plausibly a typo); consolidate proposes the canonical target
   (whichever scope has more memories) and shows a rename
   command. Suggest-only — scope renames are reversible but
   touch every memory in a scope, so a human should review.

Dry-run by default. With `apply=True`, dedup and demotion run for
real (cold scopes + typos remain suggest-only). A `ConsolidateReport`
captures every candidate and every action actually taken so the
caller can render text, JSON, or whatever else.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from ._fsutil import atomic_write_bytes, bounded_tail_read
from .events import Recorder, iter_all_events
from .health import (
    _ENDORSEMENT_GRACE_DAYS,
    _HOOKLESS_REASON,
    _freshest_touch_ts,
    _has_unresolved_contradiction,
    _is_dead_weight,
    _scope_typo_neighbor,
    is_hook_telemetry_event,
)
from .models import Category, Memory, Source, snippet_for
from .origin import Origin
from .search import _pairwise_content_jaccard, _raw_content_token_set
from .store import Store
from .time_utils import isoformat_utc, parse_event_ts

log = logging.getLogger("bettermemory.consolidate")


_DEFAULT_JACCARD_THRESHOLD = 0.75
_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_COLD_SCOPE_DAYS = 180
_DEFAULT_TYPO_DISTANCE = 2

# Hard cap on bytes read from a transcript file. The downstream prompt
# builder already caps the text it ships to the LLM at
# `llm.MAX_TRANSCRIPT_CHARS` (12k chars + truncation marker); this
# cap exists one layer earlier so a multi-GB transcript path can't OOM
# the process before the truncation kicks in. 1 MiB is comfortably
# larger than the longest sensible Claude Code session JSONL while
# still bounded.
_TRANSCRIPT_READ_CAP_BYTES = 1_048_576

# `type="user"` transcript rows the human never typed. Claude Code
# records background task notifications, slash-command bookkeeping,
# harness stdout wrappers, and system reminders as user rows whose
# content opens with one of these envelope tags; skill/command
# expansions additionally carry `isMeta: true` at the row level.
# Without the filter, `_load_transcript` flattens that harness text
# into "[user]" lines and the transcript_facts cluster hands it to the
# LLM as conversation — `consolidate --llm --from-transcript` then
# proposes "facts" distilled from documentation prose and command
# bookkeeping. Duplicated from `hook._SYNTHETIC_USER_PREFIXES` (the
# canonical list — keep the two in sync) with a cross-reference rather
# than a shared helper: hook.py is the Stop-hook entry point and this
# offline pass shouldn't couple its import graph to it.
_SYNTHETIC_USER_PREFIXES = (
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "<system-reminder>",
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


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


@dataclass
class DemotionCandidate:
    """A memory proposed for demotion to category=ambient."""

    memory_id: str
    summary: str
    age_days: int
    retrieved_count: int
    current_category: str
    proposed_category: str = "ambient"

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "summary": self.summary,
            "age_days": self.age_days,
            "retrieved_count": self.retrieved_count,
            "current_category": self.current_category,
            "proposed_category": self.proposed_category,
        }


@dataclass
class ColdScopeSuggestion:
    """A scope whose newest activity has aged out, with no in-window
    applied events on any memory in the scope. Suggest-only.

    `most_recent_created_days_ago` keeps its historical name for
    JSON-schema stability but now carries days since the newest
    activity (max of created / updated / last-verified)."""

    scope: str
    memory_count: int
    most_recent_created_days_ago: int
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "memory_count": self.memory_count,
            "most_recent_created_days_ago": self.most_recent_created_days_ago,
            "suggestion": self.suggestion,
        }


@dataclass
class ScopeTypoPair:
    """Two scopes within Levenshtein distance whose pair looks like a
    typo. The `keeper` scope is the one with more memories (more
    authority); `typo` is the candidate to rename. Suggest-only."""

    keeper: str
    typo: str
    distance: int
    keeper_count: int
    typo_count: int
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "keeper": self.keeper,
            "typo": self.typo,
            "distance": self.distance,
            "keeper_count": self.keeper_count,
            "typo_count": self.typo_count,
            "suggestion": self.suggestion,
        }


@dataclass
class ConsolidateAction:
    """A single action actually taken — only populated when apply=True."""

    kind: str  # "tombstoned" | "demoted_to_ambient"
    memory_id: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "memory_id": self.memory_id,
            "detail": self.detail,
        }


@dataclass
class ConsolidateFailure:
    """A single action that the apply pass attempted but couldn't
    complete. Aggregated so a run that hits 10 disk-full errors
    surfaces as one rollup, not 10 stray warning lines that scroll
    off the user's terminal."""

    kind: str  # "tombstone" | "demote"
    memory_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "memory_id": self.memory_id,
            "reason": self.reason,
        }


@dataclass
class ConsolidateReport:
    dedup_candidates: list[DedupCandidate] = field(default_factory=list)
    demotion_candidates: list[DemotionCandidate] = field(default_factory=list)
    cold_scope_suggestions: list[ColdScopeSuggestion] = field(default_factory=list)
    scope_typo_pairs: list[ScopeTypoPair] = field(default_factory=list)
    applied: bool = False
    actions_taken: list[ConsolidateAction] = field(default_factory=list)
    failures: list[ConsolidateFailure] = field(default_factory=list)
    dedup_method: str = "jaccard"  # always "jaccard" since 4.0.0
    # Pairs the polarity guard kept out of `dedup_candidates` (above
    # threshold, opposite negation polarity). Suggest-only — the apply
    # path never reads this list. Declared last so positional
    # construction of the older fields stays valid.
    polarity_skipped: list[PolaritySkippedPair] = field(default_factory=list)
    # One line explaining an EMPTY `demotion_candidates` list that is
    # empty by refusal rather than because the store is clean — today
    # only the telemetry-coverage gate sets it (see
    # `find_demotion_candidates`). None on every pass that actually
    # ran. Declared last so positional construction stays valid.
    #
    # Why surface it at all: the unattended Stop-hook path
    # (`run_auto_consolidate`) is exactly where a silent `[]` would be
    # invisible, and "the demotion pass has found nothing for three
    # weeks" and "the demotion pass has been refusing to run for three
    # weeks" are the same output without it.
    demotion_skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "dedup_method": self.dedup_method,
            "dedup_candidates": [c.to_dict() for c in self.dedup_candidates],
            "polarity_skipped": [p.to_dict() for p in self.polarity_skipped],
            "demotion_candidates": [d.to_dict() for d in self.demotion_candidates],
            "demotion_skipped_reason": self.demotion_skipped_reason,
            "cold_scope_suggestions": [
                s.to_dict() for s in self.cold_scope_suggestions
            ],
            "scope_typo_pairs": [p.to_dict() for p in self.scope_typo_pairs],
            "actions_taken": [a.to_dict() for a in self.actions_taken],
            "failures": [f.to_dict() for f in self.failures],
        }


# ---------------------------------------------------------------------------
# Per-pass helpers
# ---------------------------------------------------------------------------


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
    decision itself rather than of a check each loop remembers to make
    — the distinction matters because the unattended
    `run_auto_consolidate` path applies its candidates with nobody
    reviewing the diff, and a similarity threshold is no defence
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
    microsecond-distinct timestamps, and a metadata-only retag (e.g.
    this module's own demotion pass) would bump `updated` and crown
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


def find_dedup_candidates(
    memories: list[Memory],
    *,
    threshold: float | None = None,
) -> tuple[list[DedupCandidate], str]:
    """Pairwise similarity over the active set.

    Returns `(candidates, method)`; `method` is always `"jaccard"` (the
    field survives for report compatibility). The threshold defaults to
    0.75 — same calibration as the write-time dedup path. Output is
    sorted descending by similarity so the strongest matches surface
    first.

    Each candidate represents one pair; a memory that's near-duplicate
    to several others appears multiple times. Caller's responsibility
    to deduplicate the duplicate-side ids if a single tombstoning pass
    is wanted (`consolidate()` does this).

    Polarity-skipped pairs are dropped by this wrapper (its return
    shape predates them); callers that want the skip list — the
    `consolidate()` report — go through `_find_dedup_with_skips`.
    """
    candidates, _polarity_skipped, method = _find_dedup_with_skips(
        memories, threshold=threshold
    )
    return candidates, method


def _find_dedup_with_skips(
    memories: list[Memory],
    *,
    threshold: float | None = None,
) -> tuple[list[DedupCandidate], list[PolaritySkippedPair], str]:
    """`find_dedup_candidates` plus the pairs the polarity guard kept
    out of the candidate list (see `PolaritySkippedPair`). Both lists
    are sorted descending by similarity."""
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
# judgment, which `run_auto_consolidate`'s safety contract explicitly
# forswears; the pair belongs to the contradiction flow (`record_use
# outcome=contradicted` / `consolidate --llm`), not dedup — regardless
# of which similarity method surfaced it. Word-order reversals ("A
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


# Provenance stamp appended by `--llm --from-transcript` propose_new
# writes (built in `_apply_llm_proposal`). The stamp is
# system-manufactured boilerplate shared BY CONSTRUCTION between every
# fact distilled from the same transcript turn: two semantically
# DISTINCT facts citing one turn measure ~0.93 Jaccard stamped vs ~0.11
# unstamped — above both the 0.75 manual `--apply` default and the 0.90
# unattended `run_auto_consolidate` threshold — so similarity over the
# stamped body tombstones a genuine fact. The write-time gates already
# judge the unstamped claim (see the scoping rationale where the stamp
# is built); both dedup paths strip it the same way before comparing.
# Similarity/polarity input only: `_pick_keeper`, the report summaries,
# and the persisted body keep the stamp. Greedy `.*` + DOTALL reach the
# final `)_` even when the excerpt contains parentheses or newlines.
_PROVENANCE_RE = re.compile(
    r"\n\n_\(consolidate --llm --from-transcript: .*\)_\s*$", re.S
)


def _strip_provenance(body: str) -> str:
    """Body with any trailing `--from-transcript` provenance stamp
    removed — the comparison view of a memory for the dedup passes.
    Unstamped bodies come back unchanged."""
    return _PROVENANCE_RE.sub("", body)


# Number-bearing tokens for the numeric-divergence guard. Length-capped
# at 16 so long opaque identifiers (ULIDs, full SHAs, JWT fragments)
# don't count — two bodies citing different record ids are referencing,
# not disagreeing. Short version/port/date/count shapes all pass:
# "5432", "3.27.0", "2026-07-20", "v2", "74f625d".
_NUMERIC_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.\-_]*")


def _numeric_token_set(body: str) -> frozenset[str]:
    """The digit-bearing tokens of a provenance-stripped body,
    lowercased, trailing punctuation trimmed."""
    out: set[str] = set()
    for tok in _NUMERIC_TOKEN_RE.findall(body.lower()):
        tok = tok.strip(".-_")
        if tok and len(tok) <= 16 and any(c.isdigit() for c in tok):
            out.add(tok)
    return frozenset(out)


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


def find_demotion_candidates(
    memories: list[Memory],
    events: Iterable[dict[str, Any]],
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    hook_telemetry_events: int | None = None,
) -> list[DemotionCandidate]:
    """Memories that match the `dead_weight` rule from `memory_health`:
    last touched (created / updated / verified) before the window with
    retrieval count greater than zero and applied count of zero.

    The rule itself is `health._is_dead_weight` — one shared predicate,
    so this action pass, `compute_health`'s dead_weight bucket, and
    `curation_counts`' `dead` count cannot diverge (the reported signal
    used to count memories the demotion pass refused to drain). The
    predicate carries the conservative gates:

    - Already-ambient memories (structurally exempt — the use signal
      is implicit).
    - Freshest-touch window: a rewrite (`updated`) or attestation
      (`last_verified_at`) inside the window is active maintenance,
      not rot.
    - Unresolved contradictions: a memory whose newest
      `use(contradicted)` event postdates both `updated` and
      `last_verified_at` is parked in health's contradicted bucket
      awaiting explicit resolution (mirrors
      `health.MemoryStats.has_unresolved_contradiction`). Demoting it
      would launder the flag — the retag bumps `updated` — while the
      known-wrong content stays active as ambient.
    - Endorsement grace: the auto-applied endorsement lags every
      retrieval by >= 2 memory-tool turns, so applied == 0 is
      structurally guaranteed right after a retrieval. The earliest
      timestamped retrieval must be older than
      `_ENDORSEMENT_GRACE_DAYS` before applied == 0 may count against
      the memory (missing/unparseable ts counts as old — legacy logs
      stay eligible).

    On top of the predicate, this ACTION pass keeps its own category
    whitelist: only ``fact`` and (legacy) None are demotion-eligible,
    per the module docstring's enumeration. ``user-inference`` carries
    a user-confirmation ceremony an automated pass cannot re-supply,
    and `memory_update` cannot restore the tag — the retag would be
    one-way. Future categories are protected by default. (The health
    REPORT still surfaces such rows as dead weight; only the
    unattended retag is category-restricted.)

    `hook_telemetry_events` arms the telemetry-coverage gate — same
    contract as `health.compute_health`'s parameter (`None` = caller
    did not measure, assume covered; an int = gate on, OR-ed with this
    walk's own observation). When coverage is zero this function
    REFUSES the pass entirely and returns `[]`.

    The refusal matters more here than on either reporting surface:
    this is the only one of the three `_is_dead_weight` consumers that
    MUTATES. `run_auto_consolidate` calls it from the Stop hook with
    nobody reviewing the diff, and every candidate it returns is
    retagged fact->ambient — a one-way trip `memory_update` cannot
    reverse. On a store with no settlement telemetry every retrieved
    memory satisfies `applied == 0`, so the unattended pass would demote
    the entire working set on the strength of a hook that was never
    wired. `consolidate()` surfaces the refusal on
    `ConsolidateReport.demotion_skipped_reason` — a silent `[]` on the
    unattended path is correct but invisible.

    Returns a list sorted oldest-first so the longest-stale rot is
    surfaced before fresher candidates.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_days * 86400

    retrieved: dict[str, int] = defaultdict(int)
    applied: dict[str, int] = defaultdict(int)
    earliest_retrieval: dict[str, datetime] = {}
    last_contradicted: dict[str, datetime] = {}
    observed_hook_telemetry = 0
    for event in events:
        # Coverage bookkeeping on this pass's own walk — the same
        # predicate the two reporting surfaces count, so the three
        # cannot disagree about whether the store has telemetry any
        # more than they can disagree about the dead-weight rule.
        if is_hook_telemetry_event(event):
            observed_hook_telemetry += 1
        if event.get("kind") == "search":
            # The recorder writes the result-id list as `returned`
            # (canonical name in `_handlers.memory_search`). Tolerate
            # the older `memory_ids` and `hit_ids` fields so synthetic
            # test fixtures and any pre-rename event logs still feed
            # the count; without the fallback this whole pass silently
            # produced zero demotion candidates against real event
            # logs. Order mirrors the canonical-first / legacy-second
            # discipline applied at health.py:699, health.py:1423,
            # eval.py:361, hook.py:365-367 — all sibling read sites.
            event_ts = parse_event_ts(event.get("ts"))
            for mid in (
                event.get("returned")
                or event.get("memory_ids")
                or event.get("hit_ids")
                or []
            ):
                retrieved[mid] += 1
                if event_ts is not None:
                    prev = earliest_retrieval.get(mid)
                    if prev is None or event_ts < prev:
                        earliest_retrieval[mid] = event_ts
        elif event.get("kind") == "use":
            # Same legacy fallback as the `returned` branch above —
            # pre-2.6.3 `use` events wrote `memory_ids` as the
            # canonical id list field, before the `ids` rename.
            ids = event.get("ids") or event.get("memory_ids") or []
            if event.get("outcome") == "applied":
                for mid in ids:
                    applied[mid] += 1
            elif event.get("outcome") == "contradicted":
                event_ts = parse_event_ts(event.get("ts"))
                if event_ts is not None:
                    for mid in ids:
                        prev = last_contradicted.get(mid)
                        if prev is None or event_ts > prev:
                            last_contradicted[mid] = event_ts

    if (
        hook_telemetry_events is not None
        and (hook_telemetry_events + observed_hook_telemetry) == 0
    ):
        # Refuse the whole pass, before the memory loop — there is no
        # per-memory judgement to make when the signal every judgement
        # reads is absent for all of them at once.
        return []

    grace_cutoff = now.timestamp() - _ENDORSEMENT_GRACE_DAYS * 86400
    out: list[DemotionCandidate] = []
    for memory in memories:
        # Whitelist, not skip-list: only `fact` and (legacy) None are
        # demotion-eligible. Ambient is already demoted; user-inference
        # (and any future category) keeps its protected tier. This is
        # the action-side gate layered ON TOP of the shared predicate.
        if memory.category is not None and memory.category != Category.FACT:
            continue
        retrieved_count = retrieved.get(memory.id, 0)
        first_seen = earliest_retrieval.get(memory.id)
        if not _is_dead_weight(
            category=memory.category,
            freshest_ts=_freshest_touch_ts(
                memory.created,
                memory.updated,
                memory.last_verified_at,
                memory.last_corroborated,
            ),
            retrieval_count=retrieved_count,
            applied_count=applied.get(memory.id, 0),
            has_unresolved_contradiction=_has_unresolved_contradiction(
                last_contradicted.get(memory.id),
                memory.updated,
                memory.last_verified_at,
            ),
            earliest_retrieval_ts=(
                first_seen.timestamp() if first_seen is not None else None
            ),
            cutoff_ts=cutoff,
            grace_cutoff_ts=grace_cutoff,
        ):
            continue
        age_seconds = now.timestamp() - memory.created.timestamp()
        age_days = int(age_seconds // 86400)
        current = memory.category.value if memory.category else "fact"
        out.append(
            DemotionCandidate(
                memory_id=memory.id,
                summary=snippet_for(memory.body, max_chars=100),
                age_days=age_days,
                retrieved_count=retrieved_count,
                current_category=current,
            )
        )
    out.sort(key=lambda d: d.age_days, reverse=True)
    return out


def find_cold_scopes(
    memories: list[Memory],
    events: Iterable[dict[str, Any]],
    *,
    cold_scope_days: int = _DEFAULT_COLD_SCOPE_DAYS,
    now: datetime | None = None,
) -> list[ColdScopeSuggestion]:
    """Scopes whose newest activity (created / updated / last-verified)
    is older than `cold_scope_days` AND where no memory in the scope
    appears as an applied id within that same window.

    A scope passing both filters has fired no value in the audit log
    recently AND received no maintenance. Applied events older than the
    window no longer prove current value — a finished-but-once-useful
    project scope (the canonical archivable shape) would otherwise be
    permanently exempt; events with a missing/unparseable ts keep the
    exemption (conservative default). Two structural guards:

    - When the event log contains no `use(applied)` event at all
      (telemetry disabled, or a scope lifetime predating telemetry),
      the pass returns [] — pure absence of telemetry cannot
      distinguish dead scopes from healthy ones, and the conservative
      default is silence, not a flag.
    - All-ambient scopes are skipped: ambient value is implicit
      (uncited by design), so a lifetime of zero applied events is not
      evidence of deadness. Mirrors the demotion pass's exemption and
      health.py's dead_weight/cold exclusion.

    Suggestion only — the user decides whether to archive the scope or
    just leave it on disk.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_ts = now.timestamp() - cold_scope_days * 86400

    # Per-scope: list of memories, max activity timestamp
    by_scope: dict[str, list[Memory]] = defaultdict(list)
    for memory in memories:
        for scope in memory.scopes:
            by_scope[scope].append(memory)

    # Per-scope: did any of its memories get applied within the window?
    # `any_applied` tracks whether the log holds ANY applied event (in
    # or out of window) — zero means the applied signal is unavailable,
    # not that every scope is dead.
    any_applied = False
    applied_ids: set[str] = set()
    for event in events:
        if event.get("kind") == "use" and event.get("outcome") == "applied":
            any_applied = True
            event_ts = parse_event_ts(event.get("ts"))
            if event_ts is not None and event_ts.timestamp() < cutoff_ts:
                # Aged out of the cold window: a years-old endorsement
                # doesn't prove the scope is firing value NOW. Missing
                # ts keeps the exemption (conservative).
                continue
            # Legacy fallback for `memory_ids` — see 70e41a4.
            for mid in event.get("ids") or event.get("memory_ids") or []:
                applied_ids.add(mid)

    if not any_applied:
        return []

    out: list[ColdScopeSuggestion] = []
    for scope, scope_memories in by_scope.items():
        if all(m.category == Category.AMBIENT for m in scope_memories):
            # All-ambient scope: the applied signal is structurally
            # absent for ambient by design — not an indictment.
            continue
        max_activity = max(
            max(
                m.created.timestamp(),
                m.updated.timestamp(),
                (m.last_verified_at.timestamp() if m.last_verified_at else 0.0),
            )
            for m in scope_memories
        )
        if max_activity >= cutoff_ts:
            continue
        if any(m.id in applied_ids for m in scope_memories):
            continue
        days_ago = int((now.timestamp() - max_activity) // 86400)
        out.append(
            ColdScopeSuggestion(
                scope=scope,
                memory_count=len(scope_memories),
                most_recent_created_days_ago=days_ago,
                suggestion=(
                    f"Scope {scope!r} has {len(scope_memories)} memories, "
                    f"newest activity {days_ago} days ago, no applied events "
                    f"in the last {cold_scope_days} days. Consider archiving "
                    "the scope or reviewing whether the trigger for these "
                    "memories is still real."
                ),
            )
        )
    out.sort(key=lambda s: s.most_recent_created_days_ago, reverse=True)
    return out


def find_scope_typo_pairs(
    memories: list[Memory],
    *,
    max_distance: int = _DEFAULT_TYPO_DISTANCE,
) -> list[ScopeTypoPair]:
    """Pairs of scopes that plausibly look like typos of each other.

    Neighbor detection is health.py's `_scope_typo_neighbor` — the SAME
    rule backing the rare-scopes detector, so the two surfaces cannot
    diverge: namespace-aware tail comparison, the sibling-suffix
    exemption (aoc2023/aoc2024, blog-v2/blog-v3 are deliberate
    successors), and a length-scaled distance threshold. health.py's
    recorded rationale is that a raw whole-string Levenshtein threshold
    (this function's pre-parity rule) was "both too loose and too
    tight": a shared `projects:` prefix lent distance slack to short
    distinct tails (projects:app / projects:api) while namespace
    omission was never seen. `max_distance` is vestigial — accepted for
    signature stability, but the shared neighbor rule owns its own
    thresholds.

    The `keeper` is whichever scope has more memories — more memories
    means more authority / longer history. The `typo` is the lesser
    side. Ties go to the lexically-earlier name for determinism.

    Only pairs whose typo side holds exactly one memory are emitted —
    the same singleton gate as health.py's rare-scopes detector, whose
    recorded rationale is that flagging non-singletons produced enough
    false positives that the bucket stopped being actionable. A typo
    scope by definition accumulates ~one memory before being noticed;
    a 15-memory neighbor is an established scope, and the printed
    rename command would merge two projects' memories. (The singleton
    gate alone can't protect singleton SIBLINGS — a fresh aoc2024 next
    to aoc2023 — which is what the shared distance rule handles.)
    """
    counts = Counter(scope for m in memories for scope in m.scopes)
    scopes = sorted(counts.keys())
    out: list[ScopeTypoPair] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i in range(len(scopes)):
        for j in range(i + 1, len(scopes)):
            a, b = scopes[i], scopes[j]
            if not _scope_typo_neighbor(a, b):
                # Symmetric in its arguments (equality / common-prefix
                # stripping / min-length threshold), so one call per
                # unordered pair is enough.
                continue
            # Exact whole-string distance for the suggestion text. May
            # exceed the old ≤2 gate for namespace-equality hits
            # ("bettermemory" vs "projects:bettermemory") —
            # informative, not a filter. Cheap rerun on the small
            # candidate pair beats lifting the value out of the
            # neighbor checker.
            distance = _exact_levenshtein(a, b)
            count_a = counts[a]
            count_b = counts[b]
            if count_a >= count_b:
                keeper, typo = a, b
                kc, tc = count_a, count_b
            else:
                keeper, typo = b, a
                kc, tc = count_b, count_a
            if tc != 1:
                # Rarity gate: both sides are established scopes —
                # not plausibly a typo. See docstring.
                continue
            pair_key = (keeper, typo)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            out.append(
                ScopeTypoPair(
                    keeper=keeper,
                    typo=typo,
                    distance=distance,
                    keeper_count=kc,
                    typo_count=tc,
                    suggestion=(
                        f"Scopes {keeper!r} ({kc}) and {typo!r} ({tc}) "
                        f"differ by edit distance {distance}. "
                        f"If {typo!r} is a typo, run: "
                        f"memory_rename_scope(old_scope={typo!r}, "
                        f"new_scope={keeper!r})."
                    ),
                )
            )
    return out


def _exact_levenshtein(a: str, b: str) -> int:
    """Two-row Wagner-Fischer for the exact distance. Used only on the
    small candidate pair after `_scope_typo_neighbor` narrowed the
    field, so the cost is bounded."""
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for i, cb in enumerate(b, 1):
        curr = [i] + [0] * len(a)
        for j, ca in enumerate(a, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def consolidate(
    store: Store,
    *,
    dedup_threshold: float | None = None,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    cold_scope_days: int = _DEFAULT_COLD_SCOPE_DAYS,
    typo_distance: int = _DEFAULT_TYPO_DISTANCE,
    apply: bool = False,
    session_id: str | None = None,
    now: datetime | None = None,
) -> ConsolidateReport:
    """Run all four passes against the store. With `apply=True`, dedup
    and demotion candidates are committed; cold scopes and typo pairs
    remain suggest-only regardless.

    `session_id` is forwarded to `Store.tombstone` so the tombstones
    record which session ran the consolidation — visible in
    `memory_list_tombstones` and the event log.

    Dedup logic when applying: each duplicate id is tombstoned at most
    once even if it appears in multiple pairs (e.g. memory C is
    similar to both A and B). The first encountered pair determines
    the keeper; later pairs naming the same duplicate are skipped from
    the action list but kept in the report so the caller sees the
    full set of similarities.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    memories = store.load_all()
    # Read the FULL history (active log + rotated .gz archives), not just
    # the active log. The demotion pass mirrors `memory_health`'s
    # `dead_weight` rule, which counts `use(applied)` endorsements from
    # `iter_all_events` (health.py:42,2488). After routine log rotation
    # (telemetry.max_bytes, default 10MB) every applied event in an archive
    # is invisible to `iter_events`; reading only the active log would see
    # applied_count==0 for a genuinely-endorsed memory and demote a
    # load-bearing fact->ambient on the unattended Stop-hook path. The
    # cold-scope pass reads the same `applied` signal, so it shares the
    # source. (The similarity-based dedup pass doesn't touch events.)
    events = list(iter_all_events(store.root))
    # Telemetry coverage for the demotion gate, derived HERE because
    # this is the production entry point — `consolidate()` is what both
    # `bettermemory consolidate --apply` and the unattended
    # `run_auto_consolidate` go through, and the gate has to be armed
    # on the path that mutates, not merely available on the pure
    # function. The list is already materialised above (the demotion and
    # cold-scope passes each walk it), so the count is one extra pass
    # over an in-memory list, no additional I/O.
    hook_telemetry_events = sum(1 for ev in events if is_hook_telemetry_event(ev))

    dedup_candidates, polarity_skipped, dedup_method = _find_dedup_with_skips(
        memories,
        threshold=dedup_threshold,
    )
    demotion_candidates = find_demotion_candidates(
        memories,
        events,
        window_days=window_days,
        now=now,
        hook_telemetry_events=hook_telemetry_events,
    )
    cold_scopes = find_cold_scopes(
        memories, events, cold_scope_days=cold_scope_days, now=now
    )
    typo_pairs = find_scope_typo_pairs(memories, max_distance=typo_distance)

    report = ConsolidateReport(
        dedup_candidates=dedup_candidates,
        demotion_candidates=demotion_candidates,
        cold_scope_suggestions=cold_scopes,
        scope_typo_pairs=typo_pairs,
        applied=apply,
        dedup_method=dedup_method,
        polarity_skipped=polarity_skipped,
        demotion_skipped_reason=(
            _HOOKLESS_REASON if hook_telemetry_events == 0 else None
        ),
    )

    if not apply:
        return report

    # Persist the conflict-shaped skips into the verdict queue
    # (`memory_conflicts` arbitrates them later). Apply-side only: the
    # dry-run contract is "zero side effects", and the report above
    # already SHOWS the pairs either way. Best-effort — queue I/O must
    # never fail a curation pass that is about to mutate the store.
    #
    # UNCONDITIONAL, including when this scan produced no skips at all.
    # `upsert_scan` is not only the merge — it is also the queue's ONLY
    # garbage collector, dropping rows whose members stopped being
    # active against the full-corpus liveness map it is handed (which it
    # first checks for completeness: `memories` came from `load_all`,
    # which skips files it cannot parse, and a settled verdict must not
    # die of a bad read). Gating
    # the call on `polarity_skipped` stranded dead rows forever the
    # moment fresh skips stopped (arbitrate the last pair, tombstone a
    # member, and the row stays `pending` on disk with nothing able to
    # collect it). The model-facing counters no longer inflate on such a
    # row — `memory_conflicts` and the `curation_pending.conflicts`
    # session-start cue both exclude it via `conflicts.split_judgeable`,
    # so neither prompts a pass that would find nothing — but that is a
    # per-report liveness filter, not a fix for the row: this pass is
    # the only thing that ever removes it. `upsert_scan([])` merges
    # nothing and still collects.
    try:
        from .conflicts import ConflictQueue, skip_to_candidate
        from .models import utcnow as _utcnow

        now_iso = _utcnow().isoformat()
        # Lift the report rows into queue rows here rather than
        # re-running detection: same scan, same pairs. Dedup by pair
        # id — the same pair can be flagged by both detectors.
        deduped: dict[str, Any] = {}
        for p in polarity_skipped:
            cand = skip_to_candidate(p, created=now_iso)
            deduped.setdefault(cand.id, cand)
        ConflictQueue(store.root).upsert_scan(
            list(deduped.values()), {m.id: m for m in memories}
        )
    except Exception:  # noqa: BLE001 — telemetry, not curation-critical
        log.warning("conflict-queue upsert failed", exc_info=True)

    # Apply: tombstone duplicates first, then demote.
    #
    # `keepers_so_far` tracks every id that's been crowned as the keeper
    # of some earlier pair. In a 3+ way cluster, the same memory can be
    # the keeper of pair A↔B and then the *duplicate* in pair B↔C — if
    # we tombstoned B in that second pair we'd be deleting the canonical
    # member of the first pair, leaving A's "keeper of B" tombstone
    # reason dangling. Preserve the earlier-crowned keeper.
    #
    # The mirror hazard also exists: in a bridge cluster Z–X–Y where
    # sim(Z,X) and sim(X,Y) clear the threshold but sim(Z,Y) doesn't, the
    # candidates sort [keeper=Z dup=X, keeper=X dup=Y]. The first pair
    # tombstones X; the second would then crown the *already-tombstoned* X
    # as Y's keeper and tombstone Y "near-duplicate of X" — collapsing Y's
    # content into a memory that no longer exists in the active set and
    # leaving Y's tombstone reason citing a dead memory. So we also skip
    # any pair whose keeper was itself tombstoned earlier: Y was only
    # known-similar to a removed memory (its similarity to the surviving
    # root Z was below threshold), so leaving it active is the safe call —
    # it stays a candidate in the report for a later pass / human review.
    tombstoned_ids: set[str] = set()
    keepers_so_far: set[str] = set()
    # Snapshot id-map for the scope merge below. Refreshed in place
    # after every keeper update so a keeper appearing in multiple pairs
    # carries the current `updated` for the W2 CAS check.
    dedup_by_id = {m.id: m for m in memories}
    for candidate in dedup_candidates:
        if candidate.duplicate_id in tombstoned_ids:
            continue
        if candidate.duplicate_id in keepers_so_far:
            continue
        if candidate.keeper_id in tombstoned_ids:
            continue
        if candidate.duplicate_id == candidate.keeper_id:
            # Defensive: shouldn't happen, but a malformed pair
            # shouldn't tombstone the keeper.
            continue
        keepers_so_far.add(candidate.keeper_id)
        try:
            # Merge the duplicate's scopes into the keeper BEFORE
            # tombstoning. Similarity is scope-blind, so two identical
            # boilerplate bodies in disjoint project scopes dedup at
            # 1.0 — without the merge, the surviving keeper is
            # invisible to the other project's auto-scoped retrieval
            # and the fact silently vanishes there. Merge-first means
            # a failure leaves both memories active (conservative);
            # the inverse order would lose the scope on a merge
            # failure after the tombstone.
            keeper_mem = dedup_by_id.get(candidate.keeper_id)
            dup_mem = dedup_by_id.get(candidate.duplicate_id)
            if keeper_mem is not None and dup_mem is not None:
                missing_scopes = set(dup_mem.scopes) - set(keeper_mem.scopes)
                if missing_scopes:
                    merged_scopes = sorted(set(keeper_mem.scopes) | missing_scopes)
                    # Scopes-only edit → metadata-only convention
                    # (store.py): `mark_verified` bumps
                    # `last_verified_at` WITHOUT bumping `updated`, so
                    # the W2 CAS cannot catch a verify racing this
                    # pass; without preserve_verification the stale
                    # snapshot's verification fields would silently
                    # clobber the fresh attestation — which then feeds
                    # `_pick_keeper`'s Tier-0 attested-beats-unattested
                    # rule and dead-weight classification on later
                    # passes.
                    updated_keeper = store.update(
                        keeper_mem.model_copy(update={"scopes": merged_scopes}),
                        preserve_verification=True,
                    )
                    dedup_by_id[candidate.keeper_id] = updated_keeper
            reason = (
                f"consolidate: near-duplicate of {candidate.keeper_id}, "
                f"similarity={candidate.similarity:.2f} ({candidate.method})"
            )
            store.tombstone(
                candidate.duplicate_id,
                reason=reason,
                session_id=session_id,
            )
            tombstoned_ids.add(candidate.duplicate_id)
            report.actions_taken.append(
                ConsolidateAction(
                    kind="tombstoned",
                    memory_id=candidate.duplicate_id,
                    detail=reason,
                )
            )
        except Exception as exc:  # noqa: BLE001 — never break the pass
            log.warning(
                "consolidate: failed to tombstone %s: %s",
                candidate.duplicate_id,
                exc,
            )
            report.failures.append(
                ConsolidateFailure(
                    kind="tombstone",
                    memory_id=candidate.duplicate_id,
                    reason=str(exc),
                )
            )

    # Demotion: retag to category=ambient. Skip any memory that was
    # just tombstoned in the dedup pass — same id can't be both.
    fresh_memories = {m.id: m for m in store.load_all()}
    for demotion in demotion_candidates:
        if demotion.memory_id in tombstoned_ids:
            continue
        memory = fresh_memories.get(demotion.memory_id)
        if memory is None:
            # Was tombstoned between load_all and now — skip.
            continue
        try:
            new_memory = memory.model_copy(update={"category": Category.AMBIENT})
            # Category-only edit → metadata-only convention (store.py):
            # preserve a verify that landed between the `load_all`
            # snapshot above and this write — the `updated` CAS can't
            # see it (verify doesn't bump `updated`), and clobbering
            # the attestation would also strip the demoted memory of
            # its `_pick_keeper` Tier-0 standing.
            store.update(new_memory, preserve_verification=True)
            report.actions_taken.append(
                ConsolidateAction(
                    kind="demoted_to_ambient",
                    memory_id=demotion.memory_id,
                    detail=(
                        f"category {demotion.current_category!r} -> "
                        f"'ambient' (retrieved={demotion.retrieved_count}, "
                        f"age={demotion.age_days}d)"
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 — never break the pass
            log.warning(
                "consolidate: failed to demote %s: %s",
                demotion.memory_id,
                exc,
            )
            report.failures.append(
                ConsolidateFailure(
                    kind="demote",
                    memory_id=demotion.memory_id,
                    reason=str(exc),
                )
            )

    return report


# ---------------------------------------------------------------------------
# Unattended (auto-apply) consolidation — the opt-in self-improving loop
# ---------------------------------------------------------------------------
#
# This is the only place in the project that mutates the store WITHOUT a
# human (or the model) in the loop. It is allowed to because every action
# it takes is (1) opt-in, (2) debounced, (3) bounded, (4) conservative,
# (5) reversible, and (6) recorded as a reviewable event/tombstone — the
# deliberate opposite of invisible "Dreaming" consolidation. See
# `run_auto_consolidate`'s docstring for the full safety contract.

# Dedup threshold for the *unattended* path. The manual `consolidate
# --apply` default is 0.75 Jaccard; auto-apply runs with no human
# reviewing the diff, so it merges only near-identical bodies (>=0.90
# token overlap). Higher = safer (fewer false merges), at the cost of
# leaving looser near-dups for a manual pass.
_AUTO_DEDUP_JACCARD_THRESHOLD = 0.90

# Event kind recorded for every auto-consolidate decision (ran OR skipped) —
# the audit-transparency record. It is NO LONGER the debounce clock: the
# event log gzip-rotates at `telemetry.max_bytes`, so reading the clock back
# from the active log alone would see "never ran" right after a rotation and
# fire an unscheduled pass every turn until the active log re-accumulated one.
AUTO_CONSOLIDATE_EVENT = "auto_consolidate"

# Sidecar holding the debounce clock, decoupled from the rotating event log.
# Contains the ISO-8601 timestamp of the last auto-consolidate DECISION (ran
# or skipped-for-size); absent means "never run". Rewritten atomically at
# 0o600 (it sits beside the memories and event log, same privacy bar). This
# decoupling is what lets the debounce survive a log rotation.
AUTO_CONSOLIDATE_CLOCK_FILENAME = ".auto_consolidate_last"


def _read_last_run(root: Path) -> datetime | None:
    """Last auto-consolidate decision time from the sidecar clock. Returns
    None when the file is absent or unparseable — fail-safe to "due", which
    costs at most one extra debounced pass (the pass is idempotent and every
    action it takes is reversible)."""
    try:
        raw = (root / AUTO_CONSOLIDATE_CLOCK_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_event_ts(raw.strip())


def _write_last_run(root: Path, when: datetime) -> None:
    """Persist the debounce clock atomically. Called on every decision (ran
    or skipped-for-size) so the next turn debounces against it regardless of
    whether the event log has since rotated."""
    atomic_write_bytes(
        root / AUTO_CONSOLIDATE_CLOCK_FILENAME,
        isoformat_utc(when).encode("utf-8"),
        mode_before_rename=0o600,
    )


def run_auto_consolidate(
    store: Store,
    *,
    recorder: Recorder,
    session_id: str | None,
    interval_hours: float,
    max_memories: int,
    memories: list[Memory] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Debounced, unattended run of the structurally-safe consolidation
    subset — the opt-in self-improving loop the Stop hook fires.

    Returns None when the pass was NOT due (interval not elapsed) so the
    caller stays silent; otherwise a small summary dict (status 'ran' or
    'skipped_store_too_large'), recorded as an event either way.

    Safety contract — why this may mutate the store unattended:
    - **Debounced**: runs at most once per `interval_hours`, gated on a
      sidecar clock file (rotation-immune, unlike the event log it used to
      read — see AUTO_CONSOLIDATE_CLOCK_FILENAME).
    - **Bounded**: skips when the active set exceeds `max_memories` — the
      pairwise dedup is O(N²) and this runs in the turn-end hook, which
      must stay responsive; oversized stores defer to manual `consolidate`.
    - **Conservative**: dedup is Jaccard at `_AUTO_DEDUP_JACCARD_THRESHOLD`
      (0.90, stricter than the 0.75 manual default) with NO embedding
      model loaded in the hook; demotion is the non-destructive fact→
      ambient retag. No LLM passes, no contradiction resolution — nothing
      requiring judgment.
    - **Reversible + reviewable**: every action is a tombstone (restorable
      via `memory_restore`) or a retag (reversible via `memory_update`),
      visible in `memory_list_tombstones` and the event log.

    `memories`, when supplied, is reused for the size guard only; the
    actual `consolidate` re-loads a fresh view so it acts on current
    truth. It MUST be the store's whole active set, because the guard
    reads `len()` as the store's SIZE: a caller that hands over a
    filtered or capped subset — a `handlers.search.SearchPool`, say —
    reports a store small enough to pass and disarms the Bounded
    contract above on precisely the stores it protects. Callers holding
    anything narrower should pass None and let this function load; the
    debounce above means that load is paid at most once per
    `interval_hours`, not per turn. (The Stop hook passes None for
    exactly this reason — the only list it holds is its probe's search
    pool.)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Debounce: skip entirely if a prior decision landed within the window.
    # The clock is the sidecar file, not the event log, so a log rotation
    # can't reset it and trigger an unscheduled pass.
    last = _read_last_run(store.root)
    if last is not None:
        elapsed_hours = (now - last).total_seconds() / 3600.0
        if elapsed_hours < interval_hours:
            return None

    # Size guard: keep the turn-end hook responsive. Recording the skip
    # decision (event + sidecar clock) debounces it too, so a large store
    # isn't re-scanned (re-`load_all`'d) on every subsequent turn.
    active = memories if memories is not None else store.load_all()
    if len(active) > max_memories:
        recorder.record(
            AUTO_CONSOLIDATE_EVENT,
            status="skipped_store_too_large",
            active_count=len(active),
            max_memories=max_memories,
            session_id=session_id,
            triggered_from="stop_hook",
        )
        _write_last_run(store.root, now)
        return {
            "status": "skipped_store_too_large",
            "active_count": len(active),
            "max_memories": max_memories,
        }

    report = consolidate(
        store,
        apply=True,
        dedup_threshold=_AUTO_DEDUP_JACCARD_THRESHOLD,
        session_id=session_id,
        now=now,
    )
    tombstoned = sum(1 for a in report.actions_taken if a.kind == "tombstoned")
    demoted = sum(1 for a in report.actions_taken if a.kind == "demoted_to_ambient")
    recorder.record(
        AUTO_CONSOLIDATE_EVENT,
        status="ran",
        tombstoned=tombstoned,
        demoted=demoted,
        failures=len(report.failures),
        dedup_method=report.dedup_method,
        session_id=session_id,
        triggered_from="stop_hook",
    )
    _write_last_run(store.root, now)
    return {
        "status": "ran",
        "tombstoned": tombstoned,
        "demoted": demoted,
        "failures": len(report.failures),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(report: ConsolidateReport) -> str:
    """Human-readable summary of a consolidate report. Pairs with the
    `--json` flag's `render_json` for machine output."""
    lines: list[str] = []
    title = "Consolidate report"
    if report.applied:
        title += " (applied)"
    else:
        title += " (dry-run — pass --apply to commit)"
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")

    lines.append(
        f"Dedup candidates ({len(report.dedup_candidates)}, method={report.dedup_method})"
    )
    if report.dedup_candidates:
        for c in report.dedup_candidates:
            lines.append(
                f"  {c.similarity:.3f}  keep {c.keeper_id}, tombstone {c.duplicate_id}"
            )
            lines.append(f"    keep: {c.keeper_summary}")
            lines.append(f"    drop: {c.duplicate_summary}")
    else:
        lines.append("  (none)")
    lines.append("")

    if report.polarity_skipped:
        # Exception bucket, not a routine pass output — rendered only
        # when it fired (same convention as Failures below). Suggest-
        # only: the apply path never touches these pairs.
        lines.append(
            f"Polarity-skipped pairs ({len(report.polarity_skipped)}) — "
            "similar but opposite polarity; review manually, not auto-merged"
        )
        for ps in report.polarity_skipped:
            lines.append(f"  {ps.similarity:.3f}  {ps.memory_id_a} <> {ps.memory_id_b}")
            lines.append(f"    a: {ps.summary_a}")
            lines.append(f"    b: {ps.summary_b}")
        lines.append("")

    lines.append(f"Demotion candidates ({len(report.demotion_candidates)})")
    if report.demotion_candidates:
        for d in report.demotion_candidates:
            lines.append(
                f"  {d.memory_id}  ({d.current_category} -> ambient, "
                f"retrieved={d.retrieved_count}, age={d.age_days}d)"
            )
            lines.append(f"    {d.summary}")
    else:
        lines.append("  (none)")
        # Distinguish "nothing to demote" from "the pass refused to
        # run" — see `ConsolidateReport.demotion_skipped_reason`.
        if report.demotion_skipped_reason:
            lines.append(f"  NOT MEASURED: {report.demotion_skipped_reason}")
    lines.append("")

    lines.append(f"Cold-scope suggestions ({len(report.cold_scope_suggestions)})")
    if report.cold_scope_suggestions:
        for s in report.cold_scope_suggestions:
            lines.append(
                f"  {s.scope}  ({s.memory_count} memories, "
                f"newest {s.most_recent_created_days_ago}d ago)"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"Scope-typo pairs ({len(report.scope_typo_pairs)})")
    if report.scope_typo_pairs:
        for p in report.scope_typo_pairs:
            lines.append(
                f"  {p.keeper} ({p.keeper_count}) <- {p.typo} ({p.typo_count})  "
                f"distance={p.distance}"
            )
            lines.append(f"    {p.suggestion}")
    else:
        lines.append("  (none)")
    lines.append("")

    if report.applied:
        lines.append(f"Actions taken ({len(report.actions_taken)})")
        if report.actions_taken:
            for a in report.actions_taken:
                lines.append(f"  {a.kind}  {a.memory_id}  ({a.detail})")
        else:
            lines.append(
                "  (none — every candidate was a duplicate-of-action "
                "or otherwise skipped)"
            )
        lines.append("")

        if report.failures:
            lines.append(f"Failures ({len(report.failures)})")
            for f in report.failures:
                lines.append(f"  {f.kind}  {f.memory_id}  ({f.reason})")
            lines.append(
                "  Investigate the underlying issue (disk space, "
                "permissions, lock contention) before re-running."
            )
            lines.append("")

    return "\n".join(lines) + "\n"


def render_json(report: ConsolidateReport) -> str:
    """JSON rendering for machine consumers (CI, scripts, the
    `--json` CLI flag). Indent=2 to match the rest of bettermemory's
    CLI surface."""
    import json as _json

    return _json.dumps(report.to_dict(), indent=2) + "\n"


# ---------------------------------------------------------------------------
# LLM-driven consolidation (--llm)
# ---------------------------------------------------------------------------
#
# `bettermemory consolidate --llm` extends the four non-LLM passes with a
# fifth: cluster the active store, ask a local (or remote) LLM to propose
# merges, contradiction resolutions, date rewrites, and tier demotions,
# render each proposal as a diff for human review, and only commit on
# explicit accept. The audit-transparency framing is the lane-claim:
# Anthropic's Dreaming consolidates invisibly; bettermemory's --llm
# refuses to commit without your accept.


@dataclass
class LLMProposalAction:
    """Outcome of applying one Proposal. Mirrors `ConsolidateAction`'s
    shape so renderers (and machine consumers) see a uniform
    actions-taken stream regardless of which pass produced the
    mutation."""

    kind: str  # "llm_merge_tombstone" / "llm_resolve_tombstone" / ...
    memory_id: str
    detail: str


@dataclass
class LLMClusterFailure:
    """One cluster's worth of LLM failure. Carries the cluster id and
    the underlying error so the operator can re-run a specific cluster
    after fixing whatever broke (network, key, model load, etc.)."""

    cluster_id: str
    reason: str


@dataclass
class LLMConsolidateReport:
    """End-to-end report of an --llm pass."""

    provider_name: str
    cluster_count: int
    proposals: list[Any] = field(default_factory=list)  # list[Proposal]
    accepted: list[Any] = field(default_factory=list)  # list[Proposal]
    rejected: list[Any] = field(default_factory=list)  # list[Proposal]
    actions_taken: list[LLMProposalAction] = field(default_factory=list)
    failures: list[LLMClusterFailure] = field(default_factory=list)
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "cluster_count": self.cluster_count,
            "proposal_count": len(self.proposals),
            "accepted_count": len(self.accepted),
            "rejected_count": len(self.rejected),
            "actions_taken": [
                {"kind": a.kind, "memory_id": a.memory_id, "detail": a.detail}
                for a in self.actions_taken
            ],
            "failures": [
                {"cluster_id": f.cluster_id, "reason": f.reason} for f in self.failures
            ],
            "applied": self.applied,
        }


# Cap on the number of existing memories the transcript cluster
# carries as "don't propose duplicates of these" context. Keeps the
# prompt cost bounded; the LLM gets the most-recently-updated slice,
# which is the most likely overlap with whatever the conversation just
# produced. Tuning knob — bump if false-positive duplicate proposals
# show up in practice.
_TRANSCRIPT_CLUSTER_MEMORY_CAP = 8


def build_transcript_cluster(
    *,
    transcript_path: Path,
    memories: list[Memory],
    events: list[dict[str, Any]],
) -> Any | None:
    """Build a `transcript_facts` cluster from a transcript file.

    The transcript text is flattened to a readable form: a
    Claude Code session JSONL gets its `user` / `assistant` text
    blocks extracted in order; any other content is read verbatim and
    handed to the LLM as-is. Returns `None` for an unreadable or
    empty transcript so the caller can surface the failure as one bad
    input rather than tanking the whole pass.

    The cluster's "members" are the most-recently-updated memories,
    capped at `_TRANSCRIPT_CLUSTER_MEMORY_CAP`. The LLM sees them as
    the "already covered" context — propose_new proposals that
    duplicate any of these get caught at validation time
    (`parse_and_validate` reuses the same hallucination-defence
    rejection path the other proposal types use, and the prompt
    itself instructs the LLM to skip duplicates).
    """
    from . import llm as _llm

    transcript = _load_transcript(transcript_path)
    if not transcript or not transcript.strip():
        return None

    # Most-recently-updated first; cap the slice.
    recent = sorted(memories, key=lambda m: m.updated, reverse=True)[
        :_TRANSCRIPT_CLUSTER_MEMORY_CAP
    ]
    members = tuple(
        _llm._build_cluster_member(m, events)
        for m in recent  # noqa: SLF001
    )
    return _llm.Cluster(
        cluster_id="transcript-facts",
        cluster_kind="transcript_facts",
        members=members,
        transcript=transcript,
    )


def _load_transcript(path: Path) -> str:
    """Read a transcript file and flatten it to a plain-text form.

    Detection is by file extension only:

    - ``.jsonl`` → Claude Code per-session log. Parse line-by-line;
      keep `{"type": "user", ...}` and `{"type": "assistant", ...}`
      entries and concatenate their text content blocks. Synthetic
      user rows are dropped: rows stamped `isMeta: true` (skill /
      command expansions) and rows whose text opens with one of the
      `_SYNTHETIC_USER_PREFIXES` envelope tags (task notifications,
      command bookkeeping, system reminders) are harness text the
      human never typed — flattening them as "[user]" lines fed the
      transcript_facts cluster documentation prose to distill "facts"
      from. Mirrors `hook._extract_last_exchange`'s row filtering.
    - anything else → read verbatim. Plain-text and Markdown
      transcripts pass through unchanged.

    Returns an empty string when the path doesn't exist, can't be
    read, or contains no recoverable content — the caller treats
    that as "no transcript to consolidate from" and skips the
    cluster.
    """
    # Reject anything that isn't a regular file before opening it.
    # `bounded_tail_read` opens the path in binary mode; a FIFO with
    # no writer would block `open()` indefinitely, hanging
    # `consolidate --llm --from-transcript`. `is_file()` stats without
    # opening (no block) and is False for FIFOs, devices, directories,
    # and missing paths — all "no transcript", same as the OSError
    # branch. `hook.py` guards its transcript path the same way.
    if not path.is_file():
        return ""
    try:
        # `bounded_tail_read` enforces the byte cap (not chars) so a
        # multibyte UTF-8 transcript can't bypass it — see 2.6.3 fix.
        # The downstream prompt builder truncates again at
        # `MAX_TRANSCRIPT_CHARS`; any truncation here just narrows the
        # candidate window earlier. Tail-read so a long session keeps
        # the most-recent content rather than ancient lead-in.
        raw = bounded_tail_read(path, _TRANSCRIPT_READ_CAP_BYTES).decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""
    if path.suffix.lower() != ".jsonl":
        return raw

    out: list[str] = []
    # Split on "\n" only — NOT splitlines(). The transcript is written
    # by external serializers (Node's JSON.stringify emits U+2028/U+2029
    # raw inside strings, legal JSON), and splitlines() breaks on those
    # code points, shattering a valid row into fragments that fail
    # json.loads and silently vanish from the candidate window. Same
    # fix as hook._extract_last_exchange; strip() below still absorbs
    # any \r remnants and blank pieces.
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        role = row.get("type")
        message = row.get("message")
        if role not in ("user", "assistant") or not isinstance(message, dict):
            continue
        if role == "user" and row.get("isMeta"):
            # Skill/command expansions are stamped `isMeta: true` at
            # the row level — harness-injected, not the human's words.
            # Same row-level check as hook._extract_last_exchange.
            continue
        content = message.get("content")
        text: str | None = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    chunk = block.get("text")
                    if isinstance(chunk, str) and chunk:
                        parts.append(chunk)
            if parts:
                text = "\n".join(parts)
        if not text:
            continue
        if role == "user" and text.lstrip().startswith(_SYNTHETIC_USER_PREFIXES):
            # Envelope-tagged synthetic payload (see the constant's
            # comment) — harness bookkeeping, not conversation.
            # Assistant rows are kept unfiltered, matching hook.py
            # (only user rows carry synthetic envelopes).
            continue
        out.append(f"[{role}] {text}")
    return "\n\n".join(out)


def consolidate_llm(
    store: Store,
    provider: Any,  # llm.LLMProvider — kept Any to avoid import cycle
    *,
    dedup_threshold: float | None = None,
    today: str | None = None,
    apply: bool = False,
    accept: bool = False,
    interactive_input: Any = None,
    session_id: str | None = None,
    from_transcript: str | None = None,
    max_content_bytes: int | None = None,
    allowed_scopes: list[str] | None = None,
    origin: Origin | None = None,
) -> LLMConsolidateReport:
    """Run an LLM-driven consolidation pass.

    Steps:

    1. Use the existing `find_dedup_candidates` pass to seed
       `near_duplicates` clusters — same threshold + same semantic /
       Jaccard fallback.
    2. Walk the event log to seed `contradiction_candidates` clusters
       from any `record_use(outcome="contradicted")` events.
    3. When ``from_transcript`` is provided, append a
       ``transcript_facts`` cluster that carries the conversation text
       plus a sample of existing memories as the "don't propose
       duplicates" context. The LLM proposes `propose_new` actions
       (new memories worth saving from the conversation) on this
       cluster, closing the writing-reflex gap.
    4. Ask `provider` for proposals on each cluster. Hallucinated
       memory IDs are rejected at validation time by `llm.parse_and_validate`.
    5. With `apply=False` (dry-run, the default): return the report
       with every proposal but no mutations.
    6. With `apply=True` AND `accept=True`: commit every validated
       proposal silently (CI / scripted use).
    7. With `apply=True` AND `interactive_input` provided (the
       interactive path the CLI uses): prompt per proposal and only
       commit the accepted ones.
    8. With `apply=True` AND no accept AND no interactive_input:
       refuse to commit — the audit-transparency contract requires an
       explicit human accept.

    `today` defaults to today's UTC date in ISO format; pass it
    explicitly in tests for determinism.

    `interactive_input` is a callable taking one positional argument
    (the prompt text) returning the user's response — i.e. `input`
    in interactive mode, a stub in tests. Pass `None` to mean
    "non-interactive."

    `from_transcript` is a path to a transcript file (plain text or
    JSONL — Claude Code's per-session log shape is auto-detected).
    When provided, the consolidate pass adds a transcript-facts
    cluster to the run; without it, only the existing dedup /
    contradiction passes fire.

    `max_content_bytes` mirrors the `[behavior] max_content_bytes`
    config knob and gates propose_new writes the same way
    `memory_write` does — the LLM only sees ~8 cluster members as
    context, so guardrails that don't depend on a single-call view of
    the store have to fire here. It also caps the merge / rewrite_date
    REPLACEMENT bodies (`_gate_llm_replacement_body`), which
    `memory_update` would size-check on the interactive edit surface.
    None disables the size cap (used only by tests that exercise the
    apply path without a Config).

    `allowed_scopes` mirrors the `[scopes] allowed` config knob the same
    way — `memory_write` rejects an out-of-allowlist scope in
    `_validate_write_payload`, but the propose_new write path never
    routes through it, so the allowlist gate has to fire here too. An
    empty/None list disables the check (matching `_validate_write_payload`,
    which enforces only when the allowlist is non-empty).

    `origin` is stamped onto every propose_new write so an LLM-distilled
    memory carries the caller's repo/worktree context — without it the
    write persists `origin=None`, which `origin.should_include_for_caller`
    treats as global and surfaces in every scope and worktree. Threaded
    from the CLI (which has the CWD context to `capture()` it); mirrors
    the accept-proposal sibling (`handlers/proposals.py`, `ingest.py`).
    """
    from . import llm as _llm

    today = today or _llm.today_iso()
    memories = store.load_all()
    # Full history (active + rotated archives) so the contradiction-cluster
    # seeding sees `record_use(outcome="contradicted")` events that have
    # aged into a .gz archive, matching the non-LLM `consolidate` pass and
    # health.py's canonical `iter_all_events` source.
    events = list(iter_all_events(store.root))
    by_id = {m.id: m for m in memories}

    # Seed near-duplicate clusters from the existing pass.
    dedup_candidates, _method = find_dedup_candidates(
        memories,
        threshold=dedup_threshold,
    )
    pairs = [(c.keeper_id, c.duplicate_id) for c in dedup_candidates]
    clusters = _llm.build_clusters(memories, events=events, near_duplicate_pairs=pairs)

    # Append a transcript_facts cluster when the caller asked for it.
    # `build_transcript_cluster` is a no-op (returns None) when the
    # transcript is empty or all-whitespace; failures during read are
    # surfaced as a LLMClusterFailure rather than bubbling up so one
    # bad input doesn't tank the whole pass.
    if from_transcript is not None:
        try:
            transcript_cluster = build_transcript_cluster(
                transcript_path=Path(from_transcript),
                memories=memories,
                events=events,
            )
        except Exception as exc:  # noqa: BLE001 — surface as failure
            transcript_cluster = None
            log.warning(
                "consolidate --llm --from-transcript: failed to load %s: %s",
                from_transcript,
                exc,
            )
        if transcript_cluster is not None:
            clusters.append(transcript_cluster)

    report = LLMConsolidateReport(
        provider_name=getattr(provider, "name", "?"),
        cluster_count=len(clusters),
        applied=apply,
    )

    for cluster in clusters:
        try:
            cluster_proposals = provider.propose(cluster, today=today)
        except _llm.LLMParseError as exc:
            # A total parse failure (garbage / non-JSON / fence-mangled
            # response) is distinct from a well-formed object carrying
            # zero valid proposals — `parse_and_validate` raises rather
            # than returning [] so a broken provider surfaces as a
            # recorded cluster failure instead of hiding as a phantom
            # empty cluster. Record it the same way any other
            # cluster-level failure is recorded.
            log.warning(
                "consolidate --llm: cluster %s returned an unparseable response: %s",
                cluster.cluster_id,
                exc,
            )
            report.failures.append(
                LLMClusterFailure(cluster_id=cluster.cluster_id, reason=str(exc))
            )
            continue
        except Exception as exc:  # noqa: BLE001 — one bad cluster shouldn't tank the pass
            log.warning(
                "consolidate --llm: cluster %s failed: %s",
                cluster.cluster_id,
                exc,
            )
            report.failures.append(
                LLMClusterFailure(cluster_id=cluster.cluster_id, reason=str(exc))
            )
            continue
        report.proposals.extend(cluster_proposals)

    if not apply:
        return report

    # Apply gate: require either non-interactive accept-all or an
    # interactive prompt. Silent batch commits violate the
    # audit-transparency contract.
    if not accept and interactive_input is None:
        log.warning(
            "consolidate --llm --apply: refusing to commit without "
            "either --yes or an interactive accept loop. Re-run with "
            "--apply --yes for batch commit, or run interactively."
        )
        return report

    for proposal in report.proposals:
        if not accept and interactive_input is not None:
            diff = _llm.render_proposal_diff(proposal, by_id)
            print(diff)
            response = interactive_input("Apply? [y/N]: ").strip().lower()
            if response not in {"y", "yes"}:
                report.rejected.append(proposal)
                continue
        report.accepted.append(proposal)
        try:
            actions = _apply_llm_proposal(
                store,
                proposal,
                by_id,
                session_id=session_id,
                max_content_bytes=max_content_bytes,
                allowed_scopes=allowed_scopes,
                origin=origin,
            )
            report.actions_taken.extend(actions)
        except Exception as exc:  # noqa: BLE001
            log.warning("consolidate --llm: apply failed: %s", exc)
            report.failures.append(
                LLMClusterFailure(
                    cluster_id=str(
                        getattr(proposal, "memory_id", None)
                        or getattr(proposal, "keeper_id", "?")
                    ),
                    reason=str(exc),
                )
            )

    return report


def _body_replacement_reset_fields() -> dict[str, Any]:
    """The verification/claims reset every body-replacing branch applies.

    Mirrors `handlers/update.py`'s content-edit reset field for field:
    when a body is replaced, the prior `last_verified_at` attested prose
    that no longer exists, the structured `verified_*` lists would lie
    about the new text, and `claims` declare what the OLD body asserted
    — carrying any of them onto an LLM-authored body manufactures a
    false `staleness_verdict="fresh"` (the quick-card tells the model to
    rely on fresh without spot-checking) plus unearned `_pick_keeper`
    Tier-0 standing and a dead-weight freshest-touch exemption on later
    curation passes. Returned fresh per call so no two `model_copy`
    updates share list objects.
    """
    return {
        "last_verified_at": None,
        "verified_paths": [],
        "verified_commits": [],
        "verified_versions": [],
        "verified_absent_paths": [],
        "claims": [],
    }


def _gate_llm_replacement_body(
    new_body: str,
    *,
    proposal_kind: str,
    max_content_bytes: int | None,
) -> None:
    """Body-content gates for an LLM-authored replacement body.

    The merge and rewrite_date branches persist `proposal.new_body` —
    text the LLM authored freely, not a body any write gate has ever
    seen. `memory_update`, the equivalent interactive body-edit
    surface, refuses a credential-shaped token, transient phrasing, and
    an over-cap body on every content edit; without the same checks
    here, a body `memory_update` would refuse commits through
    `consolidate --llm --apply` (and with `--yes`, nobody reviews it).
    Hard-refuse via `RuntimeError` like the propose_new branch — no
    `acknowledge_*` escape hatch exists on this path, and refusing is
    conservative: the originals stay active and `consolidate_llm`
    reports the cluster as failed. Credential first, mirroring
    `handlers/write.py`'s gate ordering ("credential before everything").
    """
    from .credentials import find_credential_markers
    from .durability import find_transient_markers

    credential_hits = find_credential_markers(new_body)
    if credential_hits:
        kinds = ", ".join(h.kind for h in credential_hits)
        raise RuntimeError(
            f"{proposal_kind} new_body contains a secret-shaped token "
            f"({kinds}); refuse — the consolidate path can't ask the "
            "LLM to acknowledge_credential and this store syncs "
            "plain-text across hosts"
        )
    transient = find_transient_markers(new_body)
    if transient:
        markers = ", ".join(h.marker for h in transient)
        raise RuntimeError(
            f"{proposal_kind} new_body contains transient markers "
            f"({markers}); refuse — the consolidate path can't ask "
            "the LLM to acknowledge_transient"
        )
    if max_content_bytes is not None:
        body_bytes = len(new_body.encode("utf-8"))
        if body_bytes > max_content_bytes:
            raise RuntimeError(
                f"{proposal_kind} new_body exceeds max_content_bytes "
                f"({body_bytes} > {max_content_bytes})"
            )


def _apply_llm_proposal(
    store: Store,
    proposal: Any,
    by_id: dict[str, Memory],
    *,
    session_id: str | None,
    max_content_bytes: int | None = None,
    allowed_scopes: list[str] | None = None,
    origin: Origin | None = None,
) -> list[LLMProposalAction]:
    """Translate a validated `Proposal` into store-level mutations.

    Dispatches by type; each branch is a small, self-contained
    application. The function is kept narrow so the audit story stays
    legible — every code path that mutates the store on behalf of an
    LLM is right here, and the surface area for "what can --llm
    actually do" is short enough to scan.

    `max_content_bytes` and `allowed_scopes` gate the
    `propose_new` branch's write — the LLM only saw a small cluster
    slice, so the credential / size / scope-allowlist / transient /
    dedup / user-claim checks `memory_write` runs at the MCP surface
    have to fire here too. The merge and rewrite_date branches persist
    an LLM-authored REPLACEMENT body, so the body-content subset
    (credential / transient / size) fires on `proposal.new_body` as
    well (`_gate_llm_replacement_body`), and both branches reset the
    target's verification fields and claims alongside the body
    (`_body_replacement_reset_fields`) — the old attestations described
    prose that no longer exists. Gate failures raise `RuntimeError`;
    `consolidate_llm` catches it as one `LLMClusterFailure` and the
    operator sees the rejection reason in the report.

    `origin` is passed straight into the propose_new `store.write` so
    the persisted memory carries the caller's repo/worktree context;
    without it the write lands `origin=None` and leaks across scopes and
    worktrees (see `origin.should_include_for_caller`).
    """
    from . import llm as _llm

    actions: list[LLMProposalAction] = []
    if isinstance(proposal, _llm.MergeProposal):
        keeper = by_id.get(proposal.keeper_id)
        if keeper is None:
            raise RuntimeError(f"merge keeper {proposal.keeper_id} not found in store")
        # Gate BEFORE any mutation — a gate that refuses after the
        # keeper update or a tombstone would be the worse bug.
        _gate_llm_replacement_body(
            proposal.new_body,
            proposal_kind="merge",
            max_content_bytes=max_content_bytes,
        )
        # Carry the duplicates' scopes onto the keeper, for exactly the
        # reason the non-LLM dedup path states at the top of its own
        # merge block: similarity is scope-blind, so two near-identical
        # bodies in disjoint project scopes cluster at well over the
        # threshold, and a keeper that does not inherit the duplicate's
        # scope becomes invisible to that project's auto-scoped
        # retrieval — the fact vanishes there with no error and no
        # report entry. This path's clusters are seeded from the SAME
        # scope-blind `find_dedup_candidates` pass, so it is exposed to
        # the identical case, and `MergeProposal` carries no scopes
        # field, so nothing downstream can recover them.
        #
        # Over-cap unions are left to `Store._write_path`'s
        # re-validation: it refuses loudly and `consolidate_llm` reports
        # the cluster as failed, which is correct — merging is optional,
        # losing a record is not.
        #
        # `updated` is stamped by `Store.update` itself; passing a
        # pre-bumped value would break the W2 CAS check (caller's
        # `memory.updated` is the snapshot timestamp the CAS compares
        # against the on-disk record). The `model_copy` preserves the
        # keeper's snapshot `updated`, which IS what the CAS needs.
        merged_scopes = set(keeper.scopes)
        for dup_id in proposal.duplicate_ids:
            dup = by_id.get(dup_id)
            if dup is not None:
                merged_scopes.update(dup.scopes)
        # The body is REPLACED, so the keeper's verification fields and
        # claims reset in the same write (`_body_replacement_reset_fields`)
        # — carrying them forward would present the LLM-authored fusion
        # as `staleness_verdict="fresh"` prose no one ever verified. The
        # rollback below restores the full pre-merge snapshot, original
        # attestation included, via `store.update(keeper, force=True)`.
        update_fields: dict[str, Any] = {
            "body": proposal.new_body,
            **_body_replacement_reset_fields(),
        }
        if merged_scopes != set(keeper.scopes):
            update_fields["scopes"] = sorted(merged_scopes)
        merged = keeper.model_copy(update=update_fields)
        store.update(merged)
        actions.append(
            LLMProposalAction(
                kind="llm_merge_keeper",
                memory_id=proposal.keeper_id,
                detail=f"merged from {list(proposal.duplicate_ids)}",
            )
        )
        # Tombstone duplicates one-by-one. On any failure, fully roll
        # back: restore the keeper's pre-merge body AND un-tombstone
        # every duplicate already removed in this proposal. `keeper`
        # is the pre-merge object — `merged` above is a separate
        # `model_copy` — so `store.update(keeper)` is a true rollback.
        #
        # Restoring the earlier duplicates is load-bearing for multi-
        # way clusters: a 3+-member cluster gives `duplicate_ids` two
        # or more entries, so "dup A tombstoned, dup B fails" is a
        # reachable state. Without the restore loop, dup A's content
        # survives in neither the keeper (rolled back, never got the
        # merge) nor the active set (tombstoned) — silent data loss
        # until a manual `memory_restore`. Both rollback arms are
        # best-effort: if one also fails, log loudly with the id the
        # operator needs so the partial state is at least visible.
        tombstoned: list[str] = []
        for dup_id in proposal.duplicate_ids:
            reason = (
                f"consolidate --llm: merged into {proposal.keeper_id} "
                f"({proposal.rationale})"
            )
            try:
                store.tombstone(dup_id, reason=reason, session_id=session_id)
            except Exception:
                try:
                    # `force=True` bypasses the W2 CAS. The keeper's
                    # snapshot `updated` no longer matches the on-disk
                    # record (the just-completed `store.update(merged)`
                    # call above bumped it); without `force`, the
                    # rollback would itself raise `ConcurrentUpdateError`
                    # and the operator would lose both the merge AND
                    # the rollback. This is the canonical use case for
                    # the escape hatch: we've already reconciled the
                    # concurrent edit out-of-band (it was OUR write).
                    store.update(keeper, force=True)
                except Exception:  # noqa: BLE001 — log path
                    log.warning(
                        "merge rollback: keeper %s body could not be "
                        "restored after duplicate %s failed to tombstone; "
                        "manual cleanup required",
                        proposal.keeper_id,
                        dup_id,
                    )
                for done_id in tombstoned:
                    try:
                        store.restore(done_id)
                    except Exception:  # noqa: BLE001 — log path
                        log.warning(
                            "merge rollback: duplicate %s was tombstoned "
                            "then could not be restored after the merge "
                            "aborted; run `memory_restore %s` to recover",
                            done_id,
                            done_id,
                        )
                raise
            tombstoned.append(dup_id)
            actions.append(
                LLMProposalAction(
                    kind="llm_merge_tombstone",
                    memory_id=dup_id,
                    detail=reason,
                )
            )
    elif isinstance(proposal, _llm.ResolveContradictionProposal):
        reason = (
            f"consolidate --llm: contradicted by {proposal.winner_id} "
            f"({proposal.rationale})"
        )
        store.tombstone(proposal.loser_id, reason=reason, session_id=session_id)
        actions.append(
            LLMProposalAction(
                kind="llm_resolve_tombstone",
                memory_id=proposal.loser_id,
                detail=reason,
            )
        )
    elif isinstance(proposal, _llm.RewriteRelativeDateProposal):
        memory = by_id.get(proposal.memory_id)
        if memory is None:
            raise RuntimeError(f"rewrite target {proposal.memory_id} not found")
        _gate_llm_replacement_body(
            proposal.new_body,
            proposal_kind="rewrite_date",
            max_content_bytes=max_content_bytes,
        )
        # `updated` is stamped by `Store.update` itself; preserve the
        # snapshot's `updated` for the W2 CAS check (see the
        # MergeProposal branch above for the same fix). The body is
        # replaced, so verification and claims reset alongside it —
        # rewrite_date specifically targets OLDER (hence plausibly
        # verified) memories, and carrying the attestation forward would
        # stamp the rewritten prose "fresh" unexamined.
        rewritten = memory.model_copy(
            update={"body": proposal.new_body, **_body_replacement_reset_fields()}
        )
        store.update(rewritten)
        actions.append(
            LLMProposalAction(
                kind="llm_rewrite_date",
                memory_id=proposal.memory_id,
                detail=proposal.rationale,
            )
        )
    elif isinstance(proposal, _llm.DemoteTierProposal):
        memory = by_id.get(proposal.memory_id)
        if memory is None:
            raise RuntimeError(f"demote target {proposal.memory_id} not found")
        new_category = (
            Category.AMBIENT if proposal.new_category == "ambient" else Category.FACT
        )
        demoted = memory.model_copy(update={"category": new_category})
        # Category-only edit → metadata-only convention (store.py):
        # `by_id` is the snapshot taken at consolidate_llm start, and
        # the window to this apply spans LLM provider calls and
        # interactive accept prompts — minutes, not microseconds. A
        # `memory_verify` landing in that window bumps
        # `last_verified_at` WITHOUT bumping `updated`, so the W2 CAS
        # cannot catch it; without preserve_verification the stale
        # snapshot's verification fields silently clobber the fresh
        # attestation — which then feeds `_pick_keeper`'s Tier-0
        # attested-beats-unattested rule and dead-weight classification
        # on later passes. Same rationale as the non-LLM demotion retag
        # and the dedup scope-merge above.
        store.update(demoted, preserve_verification=True)
        actions.append(
            LLMProposalAction(
                kind="llm_demote_tier",
                memory_id=proposal.memory_id,
                detail=(
                    f"{(memory.category or Category.FACT).value} -> "
                    f"{proposal.new_category}: {proposal.rationale}"
                ),
            )
        )
    elif isinstance(proposal, _llm.ProposeNewProposal):
        new_category = (
            Category.AMBIENT if proposal.category == "ambient" else Category.FACT
        )
        # Stamp the source_excerpt into the body as a provenance line.
        # Future audits can trace the claim back to the transcript turn
        # without having to keep the transcript itself around.
        provenance = (
            f"\n\n_(consolidate --llm --from-transcript: {proposal.source_excerpt})_"
        )
        body_with_provenance = proposal.body.rstrip() + provenance

        # Credential gate — FIRST, mirroring the ordering
        # `handlers/write.py` uses (`CredentialGate` runs before every
        # other write gate: "credential before everything so a secret is
        # refused before any other gate records body-derived data"). The
        # store is plain-text markdown that `sync` pushes across hosts,
        # so persisting a live secret leaks it to disk, the audit log,
        # and every clone. Scan the STAMPED body: the source_excerpt is a
        # verbatim transcript quote, so a secret the user pasted mid-turn
        # rides into `body_with_provenance` even when `proposal.body`
        # itself is clean. Unlike `memory_write` there is no
        # `acknowledge_credential` escape hatch on this unattended path —
        # a hit is a hard refuse.
        from .credentials import find_credential_markers

        credential_hits = find_credential_markers(body_with_provenance)
        if credential_hits:
            kinds = ", ".join(h.kind for h in credential_hits)
            raise RuntimeError(
                f"propose_new body contains a secret-shaped token "
                f"({kinds}); refuse — the consolidate path can't ask the "
                "LLM to acknowledge_credential and this store syncs "
                "plain-text across hosts"
            )

        # Scope allowlist gate — the `[scopes] allowed` config knob.
        # `memory_write` enforces this in `_validate_write_payload`
        # (handlers/_shared.py), but the propose_new write below never
        # routes through it, so the allowlist would be a no-op on this
        # path without an explicit check. Enforce only when the list is
        # non-empty, matching `_validate_write_payload`'s semantics (an
        # empty allowlist means "any scope").
        if allowed_scopes and proposal.scope not in set(allowed_scopes):
            raise RuntimeError(
                f"propose_new scope {proposal.scope!r} is not in the "
                f"configured [scopes] allowed list ({sorted(allowed_scopes)}); "
                "refuse rather than write to an unsanctioned scope"
            )

        # Mirror the write-time guardrails `_handlers.memory_write` runs.
        # The LLM only sees ~8 cluster members as "don't duplicate
        # these" context, so dedup against the full active set + the
        # tombstone set is load-bearing here — without it, --llm
        # --from-transcript would happily re-create memories the user
        # already wrote (or already removed).
        if max_content_bytes is not None:
            body_bytes = len(body_with_provenance.encode("utf-8"))
            if body_bytes > max_content_bytes:
                raise RuntimeError(
                    f"propose_new body exceeds max_content_bytes "
                    f"({body_bytes} > {max_content_bytes})"
                )

        from .durability import find_transient_markers

        # Gate the durability check on the LLM-authored claim, NOT on
        # `body_with_provenance`. The provenance line is a verbatim
        # transcript quote (the prompt asks for "the literal turn the body
        # distils"), and real conversational turns overwhelmingly carry
        # transient phrasing ("today i", "we just", "currently", …). Scanning
        # the combined text would bounce almost every genuine --from-transcript
        # proposal on a marker in the audit citation rather than in the durable
        # claim. The byte-size cap above still counts the provenance (it is
        # persisted); only the transient gate is scoped to proposal.body.
        transient = find_transient_markers(proposal.body)
        if transient:
            markers = ", ".join(h.marker for h in transient)
            raise RuntimeError(
                f"propose_new body contains transient markers "
                f"({markers}); refuse — the consolidate path can't "
                "ask the LLM to acknowledge_transient"
            )

        # User-claim body classification — the same body-shape rule
        # `UserClaimGate` enforces at the memory_write surface
        # (handlers/write.py): a body that reads as a claim ABOUT THE
        # USER ("Mattias prefers tabs") must go through the
        # `user-inference` pending-confirm flow so the user keeps the
        # veto — misattribution sticks. This branch is literally a model
        # inferring claims about the user from a transcript, the exact
        # high-risk surface that flow exists for, yet
        # `_validate_propose_new` whitelists only fact/ambient (the
        # user-inference tier needs a confirmation the consolidate pass
        # can't supply), so a user-claim-shaped body here cannot be
        # rerouted into staging — only refused. Scoped to
        # `proposal.body` like the transient and dedup gates above: the
        # provenance excerpt is a verbatim user turn, and first-person
        # phrasing there ("My Postgres is on 5433") would bounce genuine
        # proposals on the citation rather than the claim.
        from .handlers.write import _find_user_claims

        user_claims = _find_user_claims(proposal.body)
        if user_claims:
            phrases = ", ".join(h.phrase for h in user_claims)
            raise RuntimeError(
                f"propose_new body reads as a claim about the user "
                f"({phrases}); refuse — that tier requires the "
                "user-inference pending-confirm flow, which the "
                "consolidate path can't stage"
            )

        from .search import find_similar, find_similar_tombstones

        # Like the transient gate above, the similarity gates judge the
        # LLM-authored claim (proposal.body), NOT body_with_provenance.
        # The provenance stamp is system-manufactured boilerplate shared
        # by construction between every proposal citing the same turn:
        # two distinct facts distilled from one user turn carry an
        # identical excerpt whose tokens dominate the Jaccard sets
        # (measured 0.882 stamped vs 0.10 unstamped), so scanning the
        # stamped text bounces the second genuine fact as a
        # "near-duplicate". The max_content_bytes check above stays on
        # body_with_provenance because that is what persists.
        active = store.load_all()
        high_active = [
            h for h in find_similar(proposal.body, active) if h.relevance == "high"
        ]
        if high_active:
            raise RuntimeError(
                f"propose_new body high-overlaps existing memory "
                f"{high_active[0].id}; the LLM only saw the cluster "
                "slice, not the full active set — skip rather than "
                "create a parallel entry"
            )

        high_removed = [
            h
            for h in find_similar_tombstones(
                proposal.body,
                store.load_tombstones(),
            )
            if h.relevance == "high-removed"
        ]
        if high_removed:
            raise RuntimeError(
                f"propose_new body high-overlaps previously-removed "
                f"memory {high_removed[0].id}; the prior tombstone "
                "stands until the user explicitly memory_restore's it"
            )

        written = store.write(
            content=body_with_provenance,
            scopes=[proposal.scope],
            category=new_category,
            # Machine-distilled from a transcript turn, not something the
            # user explicitly stated — mirror the accept-proposal path
            # (handlers/proposals.py, ingest.py) and stamp INFERRED so the
            # provenance distinction between said and inferred survives.
            source=Source.INFERRED,
            # Stamp the caller's repo/worktree context. Omitting origin
            # persists origin=None, which `should_include_for_caller`
            # treats as global — the memory would then surface in every
            # scope and worktree, not just where it was distilled. The
            # accept-proposal sibling passes origin=capture(...) for the
            # same reason.
            origin=origin,
        )
        actions.append(
            LLMProposalAction(
                kind="llm_propose_new",
                memory_id=written.id,
                detail=(
                    f"scope={proposal.scope} category={proposal.category}: "
                    f"{proposal.rationale}"
                ),
            )
        )
    else:
        raise RuntimeError(f"unknown proposal type: {type(proposal).__name__}")
    return actions


def render_llm_text(report: LLMConsolidateReport) -> str:
    """Human-readable rendering of an --llm report. Used by the CLI
    when --json isn't passed."""
    from . import llm as _llm

    lines: list[str] = []
    title = f"Consolidate --llm report (provider={report.provider_name})"
    if report.applied:
        title += " (applied)"
    else:
        title += " (dry-run — pass --apply to commit, --yes to skip prompts)"
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")
    lines.append(
        f"{report.cluster_count} clusters, {len(report.proposals)} proposals "
        f"({len(report.accepted)} accepted, {len(report.rejected)} rejected)"
    )
    lines.append("")

    if report.proposals:
        # Rebuild by_id from proposals' referenced ids is impractical
        # here without the store; the renderer is called from the CLI
        # with by_id available, so consumers needing diffs use
        # `llm.render_proposal_diff` directly. This text path lists
        # rationales without diffs — keeps the report compact for
        # batch use.
        lines.append("Proposals:")
        for proposal in report.proposals:
            kind = getattr(proposal, "type", type(proposal).__name__)
            target = (
                getattr(proposal, "memory_id", None)
                or getattr(proposal, "keeper_id", None)
                or getattr(proposal, "winner_id", "?")
            )
            rationale = getattr(proposal, "rationale", "")
            lines.append(f"  [{kind}] target={target}  rationale: {rationale}")
        lines.append("")
        del (
            _llm
        )  # silence "imported but unused" — the import is intentional for callers

    if report.applied and report.actions_taken:
        lines.append(f"Actions taken ({len(report.actions_taken)}):")
        for action in report.actions_taken:
            lines.append(f"  {action.kind}  {action.memory_id}  ({action.detail})")
        lines.append("")

    if report.failures:
        lines.append(f"Failures ({len(report.failures)}):")
        for failure in report.failures:
            lines.append(f"  {failure.cluster_id}: {failure.reason}")
        lines.append("")

    return "\n".join(lines) + "\n"


def render_llm_json(report: LLMConsolidateReport) -> str:
    """JSON rendering for --llm reports."""
    import json as _json

    return _json.dumps(report.to_dict(), indent=2) + "\n"
