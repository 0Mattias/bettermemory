"""Bound health's commit readers while retaining its public patch points."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from . import git_read_budget
from .health_model import (
    CrossRepoDrift,
    CrossRepoDriftGroup,
    MemoryStats,
    unread_fields,
)
from .origin import Origin
from .verify_anchoring import Anchoring
from .verify_basis import CommitDriftUnread


def _defer_caller_homes(
    report: CrossRepoDrift | None,
    *,
    by_id: dict[str, MemoryStats],
    origin_repo_by_id: dict[str, str | None],
    origin_worktree_by_id: dict[str, str | None],
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]],
    verified_head_by_id: dict[str, str | None],
    caller_origin: Origin | None,
    anchoring: Anchoring | None,
) -> CrossRepoDrift | None:
    """Disclose home reads discovered after the estate's scheduled turn.

    A caller whose history does not apply falls back to its records' homes.
    When the estate went first, defer these newly discovered reads to the
    next caller-first call, retaining the one-call root and time bounds.
    """
    from . import health

    groups, homed, unchecked, unanchorable = health._group_cross_repo_candidates(
        by_id,
        origin_repo_by_id=origin_repo_by_id,
        origin_worktree_by_id=origin_worktree_by_id,
        anchor_paths_by_id=anchor_paths_by_id,
        claims_by_id=claims_by_id,
        verified_head_by_id=verified_head_by_id,
        caller_origin=caller_origin,
        anchoring=anchoring,
        counted=frozenset(),
    )
    if not groups and not homed and not unchecked and not unanchorable:
        return report
    report = report if report is not None else CrossRepoDrift()
    report.unanchorable += unanchorable
    for (repo, worktree), reason in unchecked.items():
        report.skipped.append(
            {
                "repo": repo,
                "worktree_root": worktree,
                "reason": reason,
                "last_checked_at": None,
            }
        )
    for repo, worktree in sorted(set(groups) | set(homed)):
        report.skipped.append(
            {
                "repo": repo,
                "worktree_root": worktree,
                "reason": "home read deferred after the caller's history did not apply",
                "last_checked_at": None,
            }
        )
    return report


def _read_estate_root(
    repo: str,
    worktree: str,
    candidates: list[MemoryStats],
    live: bool,
    skipped: list[dict[str, Any]],
    *,
    anchor_paths_by_id: dict[str, tuple[str, ...]],
    claims_by_id: dict[str, tuple[str, ...]] | None,
    verified_head_by_id: dict[str, str | None] | None,
) -> CrossRepoDriftGroup | None:
    """Read one estate group; an exhausted metadata probe is unread, not missing."""
    from . import health

    root = Path(worktree)
    with git_read_budget.reading(root):
        try:
            if live:
                group = health._cross_repo_group(
                    repo,
                    worktree,
                    candidates,
                    anchor_paths_by_id=anchor_paths_by_id,
                    claims_by_id=claims_by_id,
                    verified_head_by_id=verified_head_by_id,
                )
                if group is None:
                    skipped.append(
                        {
                            "repo": repo,
                            "worktree_root": worktree,
                            "reason": "git unreachable in the home checkout",
                        }
                    )
                return group
            return health._recorded_root_group(
                repo,
                worktree,
                root,
                candidates,
                skipped,
                anchor_paths_by_id=anchor_paths_by_id,
                claims_by_id=claims_by_id,
                verified_head_by_id=verified_head_by_id,
            )
        except git_read_budget.ReadDeadline:
            rows, unread = health._drift_rows_for_candidates(
                candidates,
                root=root,
                timestamps=CommitDriftUnread.CALL_BUDGET,
                toplevel=root,
                anchor_paths_by_id=anchor_paths_by_id,
                claims_by_id=claims_by_id,
                verified_head_by_id=verified_head_by_id,
            )
            return CrossRepoDriftGroup(
                repo=repo,
                worktree_root=worktree,
                rows=rows,
                total_drifted=len(rows),
                candidates=len(candidates),
                **unread_fields(unread),
            )


def _health_history(
    root: Path,
) -> tuple[list[datetime] | CommitDriftUnread, Path | None, str | None] | None:
    """Read the caller's dates and location without turning a cutoff into absence."""
    from . import health

    try:
        with git_read_budget.reading(root, home=False):
            timestamps = health.commit_author_timestamps(root)
            if timestamps is None:
                return None
            located = health.repo_toplevel_and_head(root)
            return (
                timestamps,
                located[0] if located else None,
                located[1] if located else None,
            )
    except git_read_budget.ReadDeadline:
        return CommitDriftUnread.CALL_BUDGET, root, None
