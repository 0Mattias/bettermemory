"""The recommendations `health.compute_health` distills from a finished report.

Each bucket that crosses its floor becomes one `Recommendation`, in the
order `health_model.RECOMMENDATION_KINDS` lists; `memory_ids` is capped at
`_RECOMMENDATION_ROW_CAP`. `health` imports `_compute_recommendations`
from here. Nothing here imports `health`.
"""

from __future__ import annotations

from collections import Counter

from .health_model import (
    CommitDriftDebt,
    CrossRepoDriftGroup,
    HealthReport,
    Recommendation,
    _RECOMMENDATION_ROW_CAP,
    _RECOMMENDATION_SIZE_FLOOR,
)


def _recommend_from_buckets(report: HealthReport) -> list[Recommendation]:
    """Recommend on the dead weight, the contradicted and the cold endorsements; returns them in order."""
    out: list[Recommendation] = []
    if len(report.dead_weight) >= _RECOMMENDATION_SIZE_FLOOR:
        out.append(
            Recommendation(
                kind="remove_dead_weight",
                summary=(
                    f"{len(report.dead_weight)} memories are retrieved but "
                    "never applied — the ranker keeps surfacing them but "
                    "they don't shape replies."
                ),
                action=(
                    "memory_remove(id, reason=...) on the unhelpful ones; "
                    "memory_record_use(applied) on the ones that are still "
                    "valuable clears the signal without touching bodies."
                ),
                count=len(report.dead_weight),
                memory_ids=[s.id for s in report.dead_weight[:_RECOMMENDATION_ROW_CAP]],
            )
        )

    if report.contradicted:
        out.append(
            Recommendation(
                kind="resolve_contradicted",
                summary=(
                    f"{len(report.contradicted)} memories carry an "
                    "unresolved contradiction — recorded as `contradicted` "
                    "and not since updated or re-verified."
                ),
                action=(
                    "memory_update(id, content=...) with the corrected "
                    "fact, then memory_verify(id, verified_paths=...) to "
                    "clear the flag."
                ),
                count=len(report.contradicted),
                memory_ids=[
                    s.id for s in report.contradicted[:_RECOMMENDATION_ROW_CAP]
                ],
            )
        )

    if report.cold_endorsement_memories.total >= _RECOMMENDATION_SIZE_FLOOR:
        out.append(
            Recommendation(
                kind="cleanup_cold_endorsements",
                summary=(
                    f"{report.cold_endorsement_memories.total} memories "
                    f"crossed the retrieval floor "
                    f"({report.cold_endorsement_memories.min_retrievals}+ "
                    "retrievals) but were never explicitly endorsed — the "
                    "auto-applied pass has been closing the loop without "
                    "the model deliberately reaching for them."
                ),
                action=(
                    "memory_record_use(applied) on the ones you are sure are "
                    "useful clears the signal; memory_remove on the ones "
                    "that aren't."
                ),
                count=report.cold_endorsement_memories.total,
                memory_ids=[
                    s.id
                    for s in report.cold_endorsement_memories.rows[
                        :_RECOMMENDATION_ROW_CAP
                    ]
                ],
            )
        )
    return out


def _recommend_from_drift_and_scopes(report: HealthReport) -> list[Recommendation]:
    """Recommend on the commit drift, the unaccounted records and the typo scopes; returns them in order."""
    out: list[Recommendation] = []
    if (
        report.commit_drift_debt is not None
        and report.commit_drift_debt.total_drifted >= _RECOMMENDATION_SIZE_FLOOR
    ):
        out.append(
            Recommendation(
                kind="verify_drifted",
                summary=(
                    f"{report.commit_drift_debt.total_drifted} memories "
                    "anchored in this repo are behind HEAD — verified "
                    "before recent commits landed."
                ),
                action=(
                    "memory_verify(id, verified_paths=[...]) to re-anchor "
                    "if claims still hold — paths are what this count is "
                    "narrowed by, and any verify slides the boundary "
                    "forward; memory_update where the body needs to track "
                    "the new code."
                ),
                count=report.commit_drift_debt.total_drifted,
                memory_ids=[
                    r.id
                    for r in report.commit_drift_debt.rows[:_RECOMMENDATION_ROW_CAP]
                ],
            )
        )

    out.extend(_recommend_unread(report))

    if report.provenance is not None and report.provenance.unaccounted_total >= 1:
        # Floor of one, like `contradicted`: a single record that entered
        # the store outside every recorded path is independently
        # actionable, and the label exists so nobody waits for three.
        out.append(
            Recommendation(
                kind="review_unaccounted",
                summary=(
                    f"{report.provenance.unaccounted_total} memories entered "
                    "the store outside every recorded path: the event log "
                    "covers their creation, nothing wrote them, nothing "
                    "pulled them."
                ),
                action=(
                    "memory_show(id) each one. To keep a record you "
                    "recognise, memory_remove(id, reason=...) then "
                    'memory_admin(action="restore", id=...): the restore '
                    "re-admits it through the store and it reads local from "
                    "then on. Remove the rest."
                ),
                count=report.provenance.unaccounted_total,
                memory_ids=[
                    r.id
                    for r in report.provenance.unaccounted[:_RECOMMENDATION_ROW_CAP]
                ],
            )
        )

    if report.rare_scopes:
        # One recommendation per typo singleton — each carries its own
        # candidate fix surfaced via the scope name. The model reads
        # the list and picks the rename target.
        out.append(
            Recommendation(
                kind="fix_typo_scopes",
                summary=(
                    f"{len(report.rare_scopes)} singleton scopes look like "
                    "typos of more common scopes."
                ),
                action=(
                    'memory_admin(action="rename_scope", old_scope=..., '
                    "new_scope=...) to fold each singleton into the intended "
                    "scope name."
                ),
                count=len(report.rare_scopes),
                memory_ids=[],
                scope=", ".join(report.rare_scopes[:_RECOMMENDATION_ROW_CAP]),
            )
        )
    return out


#: What to do for each cause of a history not read
#: (`origin_history.HistoryUnread`), as the unread recommendation's action
#: names it, beside the spot-check and re-stamp every cause shares.
_UNREAD_REMEDIES: dict[str, str] = {
    "error": "for an error, git log in the checkout shows git's message",
    "over_budget": (
        "for a history longer than bettermemory reads, the re-stamp is the fix"
    ),
    "shallow": "for a shallow clone, git fetch --unshallow fetches the missing history",
    "timeout": "for a timeout, run health again once git answers within its 5 s bound",
}


def _recommend_unread(report: HealthReport) -> list[Recommendation]:
    """Recommend on the memories whose commit history applies and was not
    read (`unread`, at the caller's checkout and in each estate group):
    none reads caught up, so the floor is one, as a contradiction's. The
    summary names each cause and how many, the action the spot-check, the
    re-stamp that lets the next count walk from it, and each cause's
    remedy (`_UNREAD_REMEDIES`)."""
    buckets: list[CommitDriftDebt | CrossRepoDriftGroup] = []
    if report.commit_drift_debt is not None:
        buckets.append(report.commit_drift_debt)
    if report.cross_repo_drift is not None:
        buckets.extend(report.cross_repo_drift.groups)
    total = sum(bucket.unread for bucket in buckets)
    if total < 1:
        return []
    causes: Counter[str] = Counter()
    ids: list[str] = []
    for bucket in buckets:
        causes.update(bucket.unread_causes)
        ids.extend(memory_id for memory_id in bucket.unread_ids if memory_id not in ids)
    named = ", ".join(f"{cause} {n}" for cause, n in sorted(causes.items()))
    remedies = "; ".join(
        _UNREAD_REMEDIES[cause] for cause in sorted(causes) if cause in _UNREAD_REMEDIES
    )
    return [
        Recommendation(
            kind="spot_check_unread",
            summary=(
                f"{total} memories' commit history was not read ({named}), so "
                "the commits since their verify are unknown and none reads "
                "as caught up."
            ),
            action=(
                "Spot-check a claim of each, then memory_verify(id, "
                "verified_paths=[...]): the stamp records HEAD, and the next "
                "count walks from it without the whole history"
                + (f"; {remedies}." if remedies else ".")
            ),
            count=total,
            memory_ids=ids[:_RECOMMENDATION_ROW_CAP],
        )
    ]


def _compute_recommendations(report: HealthReport) -> list[Recommendation]:
    """Distill the bucket rollups into proactive curation suggestions.

    Order is fixed (matches `RECOMMENDATION_KINDS`) so the first
    actionable item is the one that most likely warrants attention.
    Size-driven kinds (dead_weight, cold_endorsement_memories,
    drifted) only fire when the bucket crosses
    `_RECOMMENDATION_SIZE_FLOOR`; per-row kinds (contradicted,
    unread, unaccounted, rare_scopes) fire on first occurrence because
    each instance is independently actionable.
    """
    out: list[Recommendation] = []
    out.extend(_recommend_from_buckets(report))
    out.extend(_recommend_from_drift_and_scopes(report))
    return out
