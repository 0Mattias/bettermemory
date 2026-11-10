"""An unread ancestry graph is not evidence for discarding a restore anchor."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bettermemory import origin

from .test_origin_patch import _plain, neutral_git

__all__ = ["neutral_git"]


def _history(repo: Path) -> list[str]:
    repo.mkdir()
    _plain(repo, "init", "-q")
    shas = []
    for n in range(4):
        (repo / "a.py").write_text(f"value = {n}\n")
        _plain(repo, "add", ".")
        _plain(repo, "commit", "-qm", f"step {n}")
        shas.append(_plain(repo, "rev-parse", "HEAD"))
    return shas


def test_missing_intermediate_commit_is_an_unanswered_ancestry_read(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    shas = _history(repo)
    assert origin.commit_reachable(repo, shas[0]) is True
    missing = repo / ".git" / "objects" / shas[2][:2] / shas[2][2:]
    held = tmp_path / "held-object"
    missing.replace(held)
    try:
        assert origin.head_sha(repo) == shas[-1]
        assert origin.commit_reachable(repo, shas[0]) is None
    finally:
        held.replace(missing)
    assert origin.commit_reachable(repo, shas[0]) is True
    assert origin.commit_reachable(repo, "a" * 40) is False


def test_a_signalled_ancestry_process_is_an_unanswered_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    shas = _history(repo)
    real_result = origin._git_result

    def result(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if "merge-base" in args:
            return subprocess.CompletedProcess(args, -9, "", "")
        return real_result(cwd, *args, **kwargs)

    monkeypatch.setattr(origin, "_git_result", result)
    assert origin.commit_reachable(repo, shas[0]) is None


def test_a_shallow_cut_cannot_disprove_an_anchor_beyond_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shas = _history(repo)
    assert origin.commit_reachable(repo, shas[0]) is True
    shallow = repo / ".git" / "shallow"
    shallow.write_text(shas[2] + "\n")
    try:
        assert origin.commit_reachable(repo, shas[-1]) is True
        assert origin.commit_reachable(repo, shas[2]) is True
        assert origin.commit_reachable(repo, shas[0]) is None
        assert origin.commit_reachable(repo, "a" * 40) is None
    finally:
        shallow.unlink()
    assert origin.commit_reachable(repo, shas[0]) is True
    assert origin.commit_reachable(repo, "a" * 40) is False


def test_an_unread_shallow_status_does_not_disprove_an_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    shas = _history(repo)
    _plain(repo, "checkout", "--orphan", "unrelated")
    _plain(repo, "commit", "-qm", "unrelated history", "--allow-empty")
    assert origin.commit_reachable(repo, shas[0]) is False
    real = origin._git_result

    def result(cwd: Path, *args: str, **kwargs: Any) -> Any:
        if args == ("rev-parse", "--is-shallow-repository"):
            return None
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(origin, "_git_result", result)
    assert origin.commit_reachable(repo, shas[0]) is None
