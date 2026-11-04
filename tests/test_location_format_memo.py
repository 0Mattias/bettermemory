"""A git directory's format is parsed once while its config is unchanged.

`gitfile_rule.tree_format` reads a git directory's config as git reads a
repository's format, parsed whole (`gitconfig.git_entries`), at each
judgment of the directory: once a warm membership capture, twice a warm
origin capture and nine times a cold one. A config of 2 MiB built to be
slow cost 0.6, 1.2 and 5.8 seconds there, against git's 0.12 (review G).
`gitconfig.read_git_config` now keeps the reading of the four names the
format takes with the file's stamp (device, inode, size, modification and
change times, type), and a read of the file unchanged parses nothing; a
file changed within `_FORMAT_MEMO_SETTLED_NS` of its read, where a write
may leave the stamp as it was, is parsed again.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from bettermemory import gitconfig, gitfile_rule, origin

from .test_location_gitdir_format import _apps, _padding
from .test_location_gitdir_worktree import REMOTE, _membership, _same, _set
from .test_location_gitdir_worktree import pytestmark as pytestmark
from .test_membership_security import checkout, neutral_git


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, git's walk stopped above
    `tmp_path` (`neutral_git`)."""
    neutral_git(tmp_path, monkeypatch)


@pytest.fixture(name="parses")
def _parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The size of each config `gitconfig.git_entries` parses from here."""
    seen: list[int] = []
    real = gitconfig.git_entries

    def counting(data: bytes) -> Any:
        seen.append(len(data))
        return real(data)

    monkeypatch.setattr(gitconfig, "git_entries", counting)
    return seen


@pytest.fixture(name="settled")
def _settled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config written by the test is kept at once, as one older than the
    window is (`gitconfig._FORMAT_MEMO_SETTLED_NS`)."""
    monkeypatch.setattr(gitconfig, "_FORMAT_MEMO_SETTLED_NS", 0)


def test_a_second_read_of_an_unchanged_config_parses_nothing(
    tmp_path: Path, parses: list[int], settled: None
) -> None:
    """The first judgment parses a config of 1.8 MB; the next, membership's
    captures and the origin's capture parse nothing more and read the same
    format. The memo keeps the names the format takes, not the file."""
    app, _other = _apps(tmp_path)
    config = app / ".git" / "config"
    config.write_bytes(config.read_bytes() + _padding(30000))
    git_dir = str(app / ".git")

    first = gitfile_rule.tree_format(git_dir)
    assert len(parses) == 1 and parses[0] == config.stat().st_size
    assert gitfile_rule.tree_format(git_dir) == first
    for _ in range(2):
        assert _same(_membership(app).root, app)
    got = origin.capture(app)
    assert got.repo == REMOTE and _same(got.worktree_root, app)
    assert len(parses) == 1, parses

    kept = gitconfig._FORMAT_MEMO[(str(config), gitfile_rule._FORMAT_NAMES)][1]
    assert {name for name, _value in kept.entries} <= gitfile_rule._FORMAT_NAMES
    assert len(kept.entries) <= 4


@pytest.mark.parametrize("how", ["git_config", "in_place"])
def test_a_rewritten_config_is_read_again(
    tmp_path: Path, parses: list[int], settled: None, how: str
) -> None:
    """A config git rewrites (a new file renamed over it) and one written in
    place at the same size are both parsed again, and the change is read:
    the stamp holds the inode, the size and both times."""
    app, other = _apps(tmp_path)
    git_dir = app / ".git"
    config = git_dir / "config"
    assert gitfile_rule.tree_format(str(git_dir)).worktree is None
    assert len(parses) == 1
    before = os.stat(config)
    if how == "git_config":
        _set(git_dir, "core.worktree", str(other))
        assert gitfile_rule.tree_format(str(git_dir)).worktree == str(other)
    else:
        body = config.read_bytes()
        assert b"bare = false" in body
        with open(config, "r+b") as fh:
            fh.seek(body.index(b"bare = false"))
            fh.write(b"bare =  true")
        after = os.stat(config)
        assert (after.st_ino, after.st_size) == (before.st_ino, before.st_size)
        assert gitfile_rule.tree_format(str(git_dir)).bare is True
    assert len(parses) == 2


def test_a_config_changed_just_now_is_parsed_at_each_read(
    tmp_path: Path, parses: list[int]
) -> None:
    """Within the window a write within one tick of the filesystem's clock
    may leave the stamp as it was, so the reading is not kept."""
    app, _other = _apps(tmp_path)
    for _ in range(2):
        gitfile_rule.tree_format(str(app / ".git"))
    assert len(parses) == 2


def test_the_memo_is_bounded(
    tmp_path: Path,
    parses: list[int],
    settled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oldest reading used is dropped past `_FORMAT_MEMO_SIZE` paths,
    and read again when asked for."""
    monkeypatch.setattr(gitconfig, "_FORMAT_MEMO_SIZE", 2)
    dirs = [str(checkout(tmp_path / "work" / name) / ".git") for name in "abc"]
    for git_dir in dirs:
        gitfile_rule.tree_format(git_dir)
    assert len(gitconfig._FORMAT_MEMO) == 2
    gitfile_rule.tree_format(dirs[2])
    assert len(parses) == 3
    gitfile_rule.tree_format(dirs[0])
    assert len(parses) == 4
