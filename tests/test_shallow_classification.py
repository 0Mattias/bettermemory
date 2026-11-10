"""Shallow ranges classify real anchors without accepting truncated counts."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bettermemory import _caches, origin, origin_dates, origin_history, verify
from bettermemory.verify_anchoring import ClaimAnchor

from .test_commit_drift_cache import _T0, _commit, _git, _repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _source(tmp_path: Path) -> tuple[Path, str]:
    source = _repo(tmp_path, "source")
    _commit(source, "seed", when=_T0, files={"seed.txt": "seed\n"})
    for n in (1, 2):
        _commit(
            source,
            f"app {n}",
            when=_T0 + timedelta(days=n),
            files={"src/a.py": f"x = {n}\n"},
        )
    stamp = ""
    for n in (3, 4, 5):
        head = _commit(
            source,
            f"other {n}",
            when=_T0 + timedelta(days=n),
            files={"other.txt": f"other {n}\n"},
        )
        if n == 3:
            stamp = head
    return source, stamp


@pytest.mark.parametrize("depth", [None, 4])
@pytest.mark.parametrize("quiescent", [False, True])
@pytest.mark.parametrize("path", ["src/a.py", "handlers/nope.py"])
def test_a_held_range_classifies_untouched_and_phantom_anchors(
    tmp_path: Path, depth: int | None, quiescent: bool, path: str
) -> None:
    source, stamp = _source(tmp_path)
    clone = tmp_path / "clone"
    depth_args = [] if depth is None else [f"--depth={depth}"]
    _git(tmp_path, "clone", "-q", *depth_args, source.as_uri(), str(clone))
    assert (clone / ".git" / "shallow").exists() is (depth is not None)
    if quiescent:
        stamp = _git(clone, "rev-parse", "HEAD")
    else:
        assert _git(clone, "rev-list", "--count", f"{stamp}..HEAD") == "2"
        assert _git(clone, "log", "--format=%H", f"{stamp}..HEAD", "--", path) == ""
    real = path == "src/a.py"
    assert bool(_git(clone, "log", "--format=%H", "HEAD", "--", path)) is real

    got = verify.compute_commit_drift(
        _T0 + timedelta(days=3, minutes=1),
        None,
        caller_origin=None,
        body=f"the widget rule lives in `{path}`",
        verified_head=stamp,
        anchor=ClaimAnchor(root=str(clone), rule="home_checkout"),
    )
    if real:
        assert isinstance(got, verify.CommitDriftStatus)
        assert got.basis == verify.BASIS_REACHABILITY
        assert (got.status, got.commits_since_verify) == ("clean", 0)
    else:
        assert got is None
        assert (
            verify.verdict_from_signals(
                status="stale", path_drift_missing=0, commit_drift_count=None
            )
            == "spot_check_required"
        )


@pytest.mark.parametrize("entry", ["whole", "extension", "uncached HEAD"])
def test_a_transient_cut_never_returns_or_keeps_truncated_whole_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    repo = _repo(tmp_path)
    for n in range(4):
        _commit(
            repo,
            f"c{n}",
            when=_T0 + timedelta(days=n),
            files={f"notes{n}.md": "a\n"},
        )
    expected = 4
    cut_at = _git(repo, "rev-parse", "HEAD~1")
    _caches.clear_all()
    if entry == "extension":
        before = origin_dates.commit_author_timestamps(repo)
        assert isinstance(before, list) and len(before) == 4
        _git(repo, "checkout", "-q", "-b", "side")
        for n in range(4, 7):
            head = _commit(
                repo,
                f"side {n}",
                when=_T0 + timedelta(days=n),
                files={f"notes{n}.md": "side\n"},
            )
            if n == 5:
                cut_at = head
        _git(repo, "checkout", "-q", "main")
        _commit(repo, "main", when=_T0 + timedelta(days=7), files={"main.md": "a\n"})
        _git(
            repo,
            "merge",
            "-q",
            "--no-ff",
            "side",
            "-m",
            "merge",
            when=_T0 + timedelta(days=8),
        )
        expected = 9
    elif entry == "uncached HEAD":
        monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "1")
        assert origin.toplevel_and_head_from_files(repo) is None
        assert origin_history.located_key(repo) is None
    shallow = repo / ".git" / "shallow"
    real_git = origin._git
    real_result = origin._git_result
    cut: list[bool] = []

    def cut_inside(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if not cut and ("log" in args or "rev-list" in args):
            cut.append(True)
            shallow.write_text(cut_at + "\n", encoding="utf-8")
            try:
                return real_git(cwd, *args, **kwargs)
            finally:
                shallow.unlink()
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(origin, "_git", cut_inside)
    if entry == "extension":

        def cut_result(cwd: Path, *args: str, **kwargs: Any) -> Any:
            if not cut and "log" in args:
                cut.append(True)
                shallow.write_text(cut_at + "\n", encoding="utf-8")
                try:
                    return real_result(cwd, *args, **kwargs)
                finally:
                    shallow.unlink()
            return real_result(cwd, *args, **kwargs)

        monkeypatch.setattr(origin, "_git_result", cut_result)
    first = origin_dates.commit_author_timestamps(repo)
    assert cut, "the log ran under a temporary cut"
    assert not shallow.exists()
    assert first is origin_history.HistoryUnread.SHALLOW or (
        isinstance(first, list) and len(first) == expected
    )
    assert all(
        len(row.dates) == expected or (entry == "extension" and len(row.dates) == 4)
        for row in origin_history._TIMESTAMPS_MEMO.values()
    )
    monkeypatch.setattr(origin, "_git", real_git)
    monkeypatch.setattr(origin, "_git_result", real_result)
    later = origin_dates.commit_author_timestamps(repo)
    assert isinstance(later, list) and len(later) == expected


def test_classification_does_not_relax_the_complete_history_count(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth=4", source.as_uri(), str(clone))
    assert origin_dates.paths_in_history(clone, ["src/a.py"]) is True
    assert origin_dates.paths_in_history(clone, ["src/phantom.py"]) is False
    assert (
        origin_dates.commit_author_timestamps_touching_pathspecs(clone, ["src/a.py"])
        is None
    )


@pytest.mark.parametrize("path", ["src/a.py", "src/phantom.py"])
def test_classification_declines_a_cut_changed_during_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    repo, _ = _source(tmp_path)
    cut_at = _git(repo, "rev-parse", "HEAD~1")
    shallow = repo / ".git" / "shallow"
    real_git = origin._git

    def cut_inside(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if "log" not in args:
            return real_git(cwd, *args, **kwargs)
        shallow.write_text(cut_at + "\n", encoding="utf-8")
        try:
            return real_git(cwd, *args, **kwargs)
        finally:
            shallow.unlink()

    monkeypatch.setattr(origin, "_git", cut_inside)
    assert origin_dates.paths_in_history(repo, [path]) is None
