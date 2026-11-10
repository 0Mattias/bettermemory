"""Caller-counted records cannot also be unanchorable estate coverage."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from bettermemory.health import compute_health
from bettermemory.origin import Origin

from .test_origin_patch import _plain, neutral_git
from .test_store import _memory

__all__ = ["neutral_git"]

_REMOTE = "https://example.invalid/caller.git"
_CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)
_VERIFIED = datetime(2026, 1, 2, tzinfo=timezone.utc)
_NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def test_a_caller_checked_legacy_record_is_not_counted_unanchorable(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _plain(repo, "init", "-q")
    _plain(repo, "remote", "add", "origin", _REMOTE)
    (repo / "a.py").write_text("value = 1\n")
    _plain(repo, "add", ".")
    _plain(
        repo,
        "commit",
        "-qm",
        "before verification",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    anchor = _plain(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 2\n")
    _plain(repo, "add", ".")
    _plain(
        repo,
        "commit",
        "-qm",
        "after verification",
        when=datetime(2026, 1, 3, tzinfo=timezone.utc),
    )
    memory = _memory(
        "The implementation is `a.py`.",
        created=_CREATED,
        updated=_CREATED,
        origin=Origin(cwd=str(repo), repo=_REMOTE),
        last_verified_at=_VERIFIED,
        verified_head=anchor,
        verified_paths=["a.py"],
    )
    caller = Origin(cwd=str(repo), repo=_REMOTE, worktree_root=str(repo))
    report = compute_health([memory], [], caller_origin=caller, now=_NOW)
    assert report.commit_drift_debt is not None
    assert report.commit_drift_debt.total_drifted == 1
    assert [row.id for row in report.commit_drift_debt.rows] == [memory.id]
    assert report.cross_repo_drift is None


def test_a_foreign_legacy_record_with_no_root_keeps_its_coverage_count() -> None:
    memory = _memory(
        "The implementation is `a.py`.",
        created=_CREATED,
        updated=_CREATED,
        origin=Origin(repo="https://example.invalid/foreign.git"),
        last_verified_at=_VERIFIED,
        verified_paths=["a.py"],
    )
    report = compute_health([memory], [], now=_NOW)
    assert report.cross_repo_drift is not None
    assert report.cross_repo_drift.unanchorable == 1


def test_originless_verified_records_remain_a_documented_coverage_figure() -> None:
    memory = _memory(
        "A verified prose fact.",
        created=_CREATED,
        updated=_CREATED,
        last_verified_at=_VERIFIED,
    )
    report = compute_health([memory], [], now=_NOW)
    assert report.cross_repo_drift is not None
    assert report.cross_repo_drift.unanchorable == 1
