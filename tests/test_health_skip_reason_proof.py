"""Health keeps distinct reasons and distinguishes a file from unreadable storage."""

import errno
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from bettermemory import health
from bettermemory.health_roots import EstateProgress
from bettermemory.origin import Origin
from bettermemory.verify_anchoring import ClaimAnchor

from .test_health_cross_repo_cap import _foreign
from .test_health_anchoring import _git, neutral_git

__all__ = ["neutral_git"]


@pytest.mark.parametrize(
    "homed", [(False, True), (True, False), (False, False), (True, True)]
)
@pytest.mark.parametrize("caller_first", [True, False])
def test_every_distinct_skip_cause_survives_a_shared_label_and_root(
    tmp_path: Path, homed: tuple[bool, bool], caller_first: bool
) -> None:
    root = tmp_path / "recorded"
    root.mkdir()
    origin = Origin(
        cwd=str(root),
        repo="https://github.com/example/shared.git",
        worktree_root=str(root),
    )
    memories = [
        _foreign(root, i).model_copy(
            update={"origin": origin, "verified_paths": ["src/app.py"]}
        )
        for i in range(2)
    ]
    anchors = {
        m.id: ClaimAnchor(None, "no_live_checkout", origin_root=str(root))
        if live
        else None
        for m, live in zip(memories, homed, strict=True)
    }
    anchoring = cast(
        Any,
        SimpleNamespace(
            prime=lambda ids: None,
            for_record=lambda mid, **kw: anchors[mid],
            home=lambda mid: (
                SimpleNamespace(project="same") if anchors[mid] is not None else None
            ),
        ),
    )
    caller_origin = None
    progress = EstateProgress(caller_first=caller_first)
    if not caller_first:
        caller = tmp_path / "unborn"
        caller.mkdir()
        _git(caller, "init", "-q", "-b", "main")
        _git(caller, "remote", "add", "origin", origin.repo or "")
        assert health.commit_author_timestamps(caller) is None
        caller_origin = Origin(
            cwd=str(caller), repo=origin.repo, worktree_root=str(caller)
        )
    report = health.compute_health(
        memories,
        [],
        caller_origin=caller_origin,
        anchoring=anchoring,
        estate_progress=progress,
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    estate = report.cross_repo_drift
    assert estate is not None and not estate.groups
    found = {
        "no_home"
        if row["reason"].startswith("no home:")
        else "unconfirmed"
        if row["reason"].startswith("unconfirmed:")
        else row["reason"]
        for row in estate.skipped
    }
    expected = {"unconfirmed" if live else "no_home" for live in homed}
    assert found == expected
    assert len(estate.skipped) == len(expected), (
        "Same-cause records should remain one group"
    )


@pytest.mark.parametrize("kind", ["file", "absent", "directory", "invalid"])
def test_stat_readable_non_directory_has_its_own_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "old-checkout"
    if kind == "invalid":
        root = tmp_path / "invalid\0checkout"
    if kind == "file":
        root.write_text("This path now holds a regular file.\n")
        assert root.stat().st_size > 0 and root.is_file()
    elif kind == "directory":
        root.mkdir()
    captured: list[Path] = []

    def capture(path: Path) -> Origin:
        captured.append(Path(path))
        return Origin(cwd=str(path), repo=None, worktree_root=None)

    monkeypatch.setattr(health, "capture", capture)
    report = health.compute_health(
        [_foreign(root, 0)],
        [],
        caller_origin=Origin(
            cwd=str(tmp_path),
            repo="https://github.com/example/caller.git",
            worktree_root=str(tmp_path),
        ),
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    estate = report.cross_repo_drift
    assert estate is not None and len(estate.skipped) == 1
    reason = estate.skipped[0]["reason"]
    if kind == "file":
        assert "not a directory" in reason
        assert not captured
    elif kind in ("absent", "invalid"):
        # Membership classifies an impossible NUL path as gone.
        assert reason == "worktree missing on disk" and not captured
    else:
        assert reason == "no checkout identified in the recorded worktree"
        assert captured == [root]


@pytest.mark.parametrize("failure", ["permission", "io", "disconnected_volume"])
def test_a_stat_refusal_stays_unreadable_without_identifying_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "unanswering"
    root.mkdir()
    if failure == "permission":
        error: OSError = PermissionError(errno.EACCES, "synthetic denied root")
    elif failure == "io":
        error = OSError(errno.EIO, "synthetic unreadable root")
    else:
        error = FileNotFoundError(errno.ENOENT, "synthetic disconnected volume")
        setattr(error, "winerror", 21)
    real_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == str(root):
            raise error
        return real_stat(path, *args, **kwargs)

    captured: list[Path] = []
    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(health, "capture", lambda path: captured.append(path))
    report = health.compute_health(
        [_foreign(root, 0)],
        [],
        caller_origin=Origin(
            cwd=str(tmp_path),
            repo="https://github.com/example/caller.git",
            worktree_root=str(tmp_path),
        ),
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    estate = report.cross_repo_drift
    assert estate is not None and not estate.groups
    assert [row["reason"] for row in estate.skipped] == ["worktree unreadable"]
    assert not captured
