"""Offline consolidation: dedup, demote, scope-typo + cold-scope suggestions.

The `bettermemory consolidate` CLI walks the store and proposes (or
applies, with `--apply`) four kinds of curation:

1. **Near-duplicate dedup.** Pairwise similarity over the active set —
   semantic when the embeddings extra is installed, Jaccard otherwise.
   The pair's newer-`updated` member wins; the older one is proposed
   for tombstoning with reason ``"consolidate: near-duplicate of
   <keeper_id>, similarity=0.NN"``. Ties on `updated` go to the
   memory with more `verified_paths` attestation.

2. **Demote never-applied to ambient.** Mirrors `memory_health`'s
   `dead_weight` rule: memories created before the window with
   retrieval count greater than zero and applied count of zero. The
   `fact` and (default) None categories get retagged to `ambient` so
   they stop appearing in the dead-weight bucket on future health
   passes; their content stays available for retrieval. Ambient
   memories already get this treatment, so they're skipped.

3. **Cold scope suggestions.** Scopes whose most-recently-created
   memory is older than `cold_scope_days` AND which carry no
   `applied` events in their lifetime get a "consider archiving"
   suggestion. Suggest-only — auto-archiving a whole scope is too
   blunt to apply without human review.

4. **Scope-typo pairs.** Levenshtein-≤2 neighbors among the scope
   distribution that look like typos. The rare-scopes detector in
   `health.py` finds candidates; consolidate proposes the canonical
   target (whichever scope has more memories) and shows a rename
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
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import iter_events
from .health import _edit_distance_within
from .models import Category, Memory, snippet_for
from .search import _content_token_set
from .semantic import cached_embed, cosine_similarity_normalized
from .store import Store

log = logging.getLogger("bettermemory.consolidate")


_DEFAULT_SEMANTIC_THRESHOLD = 0.85
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
    method: str  # "semantic" or "jaccard"

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
    """A scope whose newest memory has aged out, with no applied events
    on any memory in the scope. Suggest-only."""

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
    dedup_method: str = "jaccard"  # "semantic" if the model was available

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "dedup_method": self.dedup_method,
            "dedup_candidates": [c.to_dict() for c in self.dedup_candidates],
            "demotion_candidates": [d.to_dict() for d in self.demotion_candidates],
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


def _pick_keeper(a: Memory, b: Memory) -> tuple[Memory, Memory]:
    """Decide which memory wins a dedup pair.

    Tier 1: more-recently-updated wins. Refining a memory implies
    that's the canonical version. Tier 2 (tie on `updated`): more
    `verified_paths` wins — attestation is authority. Tier 3 (tie on
    both): higher ULID wins — newer creation under
    microsecond-tied writes. Returns `(keeper, duplicate)`.
    """
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
    semantic_model: Any | None = None,
    threshold: float | None = None,
) -> tuple[list[DedupCandidate], str]:
    """Pairwise similarity over the active set.

    Returns `(candidates, method)` where `method` is `"semantic"` when
    a model was provided and `"jaccard"` otherwise. The threshold
    defaults to 0.85 for semantic, 0.75 for Jaccard — same calibration
    as the write-time dedup path. Output is sorted descending by
    similarity so the strongest matches surface first.

    Each candidate represents one pair; a memory that's near-duplicate
    to several others appears multiple times. Caller's responsibility
    to deduplicate the duplicate-side ids if a single tombstoning pass
    is wanted (`consolidate()` does this).
    """
    if len(memories) < 2:
        return [], "semantic" if semantic_model is not None else "jaccard"

    if semantic_model is not None:
        method = "semantic"
        eff_threshold = (
            threshold if threshold is not None else _DEFAULT_SEMANTIC_THRESHOLD
        )
        candidates = _find_dedup_semantic(
            memories, semantic_model, threshold=eff_threshold
        )
    else:
        method = "jaccard"
        eff_threshold = (
            threshold if threshold is not None else _DEFAULT_JACCARD_THRESHOLD
        )
        candidates = _find_dedup_jaccard(memories, threshold=eff_threshold)

    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates, method


def _find_dedup_jaccard(
    memories: list[Memory], *, threshold: float
) -> list[DedupCandidate]:
    # Pre-compute token sets once per memory.
    token_sets = [(m, _content_token_set(m.body)) for m in memories]
    out: list[DedupCandidate] = []
    for i in range(len(token_sets)):
        m_i, t_i = token_sets[i]
        if not t_i:
            continue
        for j in range(i + 1, len(token_sets)):
            m_j, t_j = token_sets[j]
            if not t_j:
                continue
            intersection = t_i & t_j
            if not intersection:
                continue
            union = t_i | t_j
            sim = len(intersection) / len(union)
            if sim < threshold:
                continue
            keeper, duplicate = _pick_keeper(m_i, m_j)
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
    return out


def _find_dedup_semantic(
    memories: list[Memory], model: Any, *, threshold: float
) -> list[DedupCandidate]:
    """Pairwise cosine over cached embeddings. Reuses the persistent
    cache from `bettermemory.semantic` — a consolidate run after
    normal use will hit the cache for most memories.
    """
    # Materialize embeddings once per memory; the cache makes repeats
    # cheap but we still pay the dict lookup, so a local list is faster.
    embedded: list[tuple[Memory, Any]] = []
    for memory in memories:
        body = memory.body.strip()
        if not body:
            continue
        vec = cached_embed(model, memory.id, memory.updated.isoformat(), body)
        embedded.append((memory, vec))

    out: list[DedupCandidate] = []
    for i in range(len(embedded)):
        m_i, v_i = embedded[i]
        for j in range(i + 1, len(embedded)):
            m_j, v_j = embedded[j]
            sim = cosine_similarity_normalized(v_i, v_j)
            if sim < threshold:
                continue
            keeper, duplicate = _pick_keeper(m_i, m_j)
            out.append(
                DedupCandidate(
                    keeper_id=keeper.id,
                    keeper_summary=snippet_for(keeper.body, max_chars=100),
                    duplicate_id=duplicate.id,
                    duplicate_summary=snippet_for(duplicate.body, max_chars=100),
                    similarity=sim,
                    method="semantic",
                )
            )
    return out


def find_demotion_candidates(
    memories: list[Memory],
    events: Iterable[dict[str, Any]],
    *,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[DemotionCandidate]:
    """Memories that match the `dead_weight` rule from `memory_health`:
    created before the window with retrieval count greater than zero
    and applied count of zero. Already-ambient memories are skipped
    (they're structurally exempt).

    Returns a list sorted oldest-first so the longest-stale rot is
    surfaced before fresher candidates.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_days * 86400

    retrieved: dict[str, int] = defaultdict(int)
    applied: dict[str, int] = defaultdict(int)
    for event in events:
        if event.get("kind") == "search":
            # The recorder writes the result-id list as `returned`
            # (canonical name in `_handlers.memory_search`). Tolerate
            # the older `hit_ids` field so synthetic test fixtures and
            # any pre-rename event logs still feed the count; without
            # the fallback this whole pass silently produced zero
            # demotion candidates against real event logs.
            for mid in event.get("returned") or event.get("hit_ids") or []:
                retrieved[mid] += 1
        elif event.get("kind") == "use":
            ids = event.get("ids") or []
            if event.get("outcome") == "applied":
                for mid in ids:
                    applied[mid] += 1

    out: list[DemotionCandidate] = []
    for memory in memories:
        if memory.category == Category.AMBIENT:
            continue
        if memory.created.timestamp() >= cutoff:
            continue
        retrieved_count = retrieved.get(memory.id, 0)
        if retrieved_count == 0:
            continue
        if applied.get(memory.id, 0) > 0:
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
    """Scopes whose newest memory is older than `cold_scope_days` AND
    where no memory in the scope ever appears as an applied id.

    A scope passing both filters has fired no value in the audit log,
    in any window. Suggestion only — the user decides whether to
    archive the scope or just leave it on disk.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_ts = now.timestamp() - cold_scope_days * 86400

    # Per-scope: list of memories, max created timestamp
    by_scope: dict[str, list[Memory]] = defaultdict(list)
    for memory in memories:
        for scope in memory.scopes:
            by_scope[scope].append(memory)

    # Per-scope: did any of its memories ever get applied?
    applied_ids: set[str] = set()
    for event in events:
        if event.get("kind") == "use" and event.get("outcome") == "applied":
            for mid in event.get("ids") or []:
                applied_ids.add(mid)

    out: list[ColdScopeSuggestion] = []
    for scope, scope_memories in by_scope.items():
        max_created = max(m.created.timestamp() for m in scope_memories)
        if max_created >= cutoff_ts:
            continue
        if any(m.id in applied_ids for m in scope_memories):
            continue
        days_ago = int((now.timestamp() - max_created) // 86400)
        out.append(
            ColdScopeSuggestion(
                scope=scope,
                memory_count=len(scope_memories),
                most_recent_created_days_ago=days_ago,
                suggestion=(
                    f"Scope {scope!r} has {len(scope_memories)} memories, "
                    f"newest created {days_ago} days ago, no applied events "
                    "in the audit log. Consider archiving the scope or "
                    "reviewing whether the trigger for these memories is "
                    "still real."
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
    """Pairs of scopes within Levenshtein `max_distance` of each other.

    The `keeper` is whichever scope has more memories — more memories
    means more authority / longer history. The `typo` is the lesser
    side. Ties go to the lexically-earlier name for determinism.
    """
    counts = Counter(scope for m in memories for scope in m.scopes)
    scopes = sorted(counts.keys())
    out: list[ScopeTypoPair] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i in range(len(scopes)):
        for j in range(i + 1, len(scopes)):
            a, b = scopes[i], scopes[j]
            if not _edit_distance_within(a, b, max_distance):
                continue
            # Distance is at most max_distance — compute exact value
            # for the suggestion. Cheap rerun on the small candidate
            # pair beats lifting the value out of the early-exit
            # checker.
            distance = _exact_levenshtein(a, b)
            count_a = counts[a]
            count_b = counts[b]
            if count_a >= count_b:
                keeper, typo = a, b
                kc, tc = count_a, count_b
            else:
                keeper, typo = b, a
                kc, tc = count_b, count_a
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
    small candidate pair after `_edit_distance_within` narrowed the
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
    semantic_model: Any | None = None,
    semantic_threshold: float | None = None,
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
    events = list(iter_events(store.root))

    dedup_candidates, dedup_method = find_dedup_candidates(
        memories,
        semantic_model=semantic_model,
        threshold=semantic_threshold,
    )
    demotion_candidates = find_demotion_candidates(
        memories, events, window_days=window_days, now=now
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
    )

    if not apply:
        return report

    # Apply: tombstone duplicates first, then demote.
    #
    # `keepers_so_far` tracks every id that's been crowned as the keeper
    # of some earlier pair. In a 3+ way cluster, the same memory can be
    # the keeper of pair A↔B and then the *duplicate* in pair B↔C — if
    # we tombstoned B in that second pair we'd be deleting the canonical
    # member of the first pair, leaving A's "keeper of B" tombstone
    # reason dangling. Preserve the earlier-crowned keeper.
    tombstoned_ids: set[str] = set()
    keepers_so_far: set[str] = set()
    for candidate in dedup_candidates:
        if candidate.duplicate_id in tombstoned_ids:
            continue
        if candidate.duplicate_id in keepers_so_far:
            continue
        if candidate.duplicate_id == candidate.keeper_id:
            # Defensive: shouldn't happen, but a malformed pair
            # shouldn't tombstone the keeper.
            continue
        keepers_so_far.add(candidate.keeper_id)
        try:
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
            store.update(new_memory)
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
      entries and concatenate their text content blocks.
    - anything else → read verbatim. Plain-text and Markdown
      transcripts pass through unchanged.

    Returns an empty string when the path doesn't exist, can't be
    read, or contains no recoverable content — the caller treats
    that as "no transcript to consolidate from" and skips the
    cluster.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            # `read(n)` on a text stream reads at most n characters —
            # bounded by `_TRANSCRIPT_READ_CAP_BYTES` so a hostile or
            # accidentally-huge transcript can't blow up memory. The
            # downstream prompt builder truncates again at
            # `MAX_TRANSCRIPT_CHARS`, so any truncation here just
            # narrows the candidate window earlier.
            raw = fh.read(_TRANSCRIPT_READ_CAP_BYTES)
    except OSError:
        return ""
    if path.suffix.lower() != ".jsonl":
        return raw

    out: list[str] = []
    for line in raw.splitlines():
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
        out.append(f"[{role}] {text}")
    return "\n\n".join(out)


def consolidate_llm(
    store: Store,
    provider: Any,  # llm.LLMProvider — kept Any to avoid import cycle
    *,
    semantic_model: Any | None = None,
    semantic_threshold: float | None = None,
    today: str | None = None,
    apply: bool = False,
    accept: bool = False,
    interactive_input: Any = None,
    session_id: str | None = None,
    from_transcript: str | None = None,
    max_content_bytes: int | None = None,
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
    the store have to fire here. None disables the size cap (used
    only by tests that exercise the apply path without a Config).
    """
    from . import llm as _llm

    today = today or _llm.today_iso()
    memories = store.load_all()
    events = list(iter_events(store.root))
    by_id = {m.id: m for m in memories}

    # Seed near-duplicate clusters from the existing pass.
    dedup_candidates, _method = find_dedup_candidates(
        memories,
        semantic_model=semantic_model,
        threshold=semantic_threshold,
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
                semantic_model=semantic_model,
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


def _apply_llm_proposal(
    store: Store,
    proposal: Any,
    by_id: dict[str, Memory],
    *,
    session_id: str | None,
    max_content_bytes: int | None = None,
    semantic_model: Any | None = None,
) -> list[LLMProposalAction]:
    """Translate a validated `Proposal` into store-level mutations.

    Dispatches by type; each branch is a small, self-contained
    application. The function is kept narrow so the audit story stays
    legible — every code path that mutates the store on behalf of an
    LLM is right here, and the surface area for "what can --llm
    actually do" is short enough to scan.

    `max_content_bytes` and `semantic_model` gate the `propose_new`
    branch's write — the LLM only saw a small cluster slice, so the
    dedup / size / transient checks `memory_write` runs at the MCP
    surface have to fire here too. Gate failures raise `RuntimeError`;
    `consolidate_llm` catches it as one `LLMClusterFailure` and the
    operator sees the rejection reason in the report.
    """
    from . import llm as _llm

    actions: list[LLMProposalAction] = []
    if isinstance(proposal, _llm.MergeProposal):
        keeper = by_id.get(proposal.keeper_id)
        if keeper is None:
            raise RuntimeError(f"merge keeper {proposal.keeper_id} not found in store")
        merged = keeper.model_copy(
            update={"body": proposal.new_body, "updated": datetime.now(timezone.utc)}
        )
        store.update(merged)
        actions.append(
            LLMProposalAction(
                kind="llm_merge_keeper",
                memory_id=proposal.keeper_id,
                detail=f"merged from {list(proposal.duplicate_ids)}",
            )
        )
        for dup_id in proposal.duplicate_ids:
            reason = (
                f"consolidate --llm: merged into {proposal.keeper_id} "
                f"({proposal.rationale})"
            )
            store.tombstone(dup_id, reason=reason, session_id=session_id)
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
        rewritten = memory.model_copy(
            update={
                "body": proposal.new_body,
                "updated": datetime.now(timezone.utc),
            }
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
        store.update(demoted)
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

        transient = find_transient_markers(body_with_provenance)
        if transient:
            markers = ", ".join(h.marker for h in transient)
            raise RuntimeError(
                f"propose_new body contains transient markers "
                f"({markers}); refuse — the consolidate path can't "
                "ask the LLM to acknowledge_transient"
            )

        from .search import find_similar, find_similar_tombstones

        active = store.load_all()
        high_active = [
            h
            for h in find_similar(
                body_with_provenance, active, semantic_model=semantic_model
            )
            if h.relevance == "high"
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
                body_with_provenance,
                store.load_tombstones(),
                semantic_model=semantic_model,
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
