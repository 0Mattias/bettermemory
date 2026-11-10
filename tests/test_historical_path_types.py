"""A pathspec follows both files and descendants across historical type changes."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bettermemory import origin

from .test_origin_patch import _plain, neutral_git

__all__ = ["neutral_git"]


def _write(repo: Path, kind: str, value: int) -> None:
    path = repo / "pkg"
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    if kind == "directory":
        path.mkdir()
        path = path / "module.py"
    path.write_text(f"value = {value}\n")
    _plain(repo, "add", "-A")
    _plain(repo, "commit", "-qm", f"step {value}")


@pytest.mark.parametrize(
    "kinds",
    [
        ("directory", "directory", "file"),
        ("file", "directory", "directory"),
        ("file", "directory", "file"),
        ("file", "file", "file"),
    ],
)
def test_a_path_counts_every_matching_commit_across_type_changes(
    tmp_path: Path, kinds: tuple[str, str, str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _plain(repo, "init", "-q")
    _write(repo, kinds[0], 0)
    anchor = _plain(repo, "rev-parse", "HEAD")
    for value, kind in enumerate(kinds, 1):
        _write(repo, kind, value)
    (repo / "pkg-other").write_text("unrelated prefix\n")
    _plain(repo, "add", ".")
    _plain(repo, "commit", "-qm", "different prefix")
    expected = set(
        _plain(repo, "log", "--format=%H", f"{anchor}..HEAD", "--", "pkg").splitlines()
    )
    assert len(expected) == 3
    walk = origin.commits_since_anchor(repo, anchor)
    assert walk is not None
    assert len(walk.commits) == 4
    assert walk.shas_touching(["pkg"]) == expected
