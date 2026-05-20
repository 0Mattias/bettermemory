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

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
            for mid in event.get("hit_ids") or []:
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
