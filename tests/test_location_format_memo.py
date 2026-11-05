"""A git directory's format is parsed once while its config is unchanged.

`gitfile_rule.tree_format` reads a git directory's config as git reads a
repository's format, parsed whole (`gitconfig.git_entries`), at each
judgment of the directory: once a warm membership capture, twice a warm
origin capture and nine times a cold one. A config of 2 MiB built to be
slow cost 0.6, 1.2 and 5.8 seconds there, against git's 0.12 (review G).
`gitconfig.read_git_config` now keeps the reading of the four names the
format takes with the digest of the bytes it parsed, and a read that
finds the same bytes parses nothing. It was kept first by the file's
stamp (device, inode, size, both times, type) once the file had not
changed for two seconds; a server whose clock runs behind this host's,
with whole-second times (sshfs), let a same-size rewrite leave the stamp
as it was and the stale reading stand (review H). The digest consults no
clock.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bettermemory import gitconfig, gitfile_rule, origin

from .test_location_gitdir_format import _apps, _padding
from .test_location_gitdir_worktree import REMOTE, _membership, _same, _set
from .test_location_gitdir_worktree import pytestmark as pytestmark
from .test_membership_security import checkout, neutral_git
from .test_origin_patch import _git_version


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, git's walk stopped above
    `tmp_path` (`neutral_git`)."""
    neutral_git(tmp_path, monkeypatch)


@pytest.fixture(name="parses")
def _parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The size of each config `gitconfig.git_entries` parses from here for
    the format reader (`gitconfig.read_git_config`). The reader of the
    remotes (`gitconfig.config_tree`) parses with it too, a capture's
    files as it reads them, as the line reader it replaced did (ENG-207's
    round 6), and is not the memo's."""
    seen: list[int] = []
    inside = [0]
    real = gitconfig.git_entries
    real_read = gitconfig.read_git_config

    def counting(data: bytes) -> Any:
        if inside[0]:
            seen.append(len(data))
        return real(data)

    def reading(*args: Any, **kwargs: Any) -> Any:
        inside[0] += 1
        try:
            return real_read(*args, **kwargs)
        finally:
            inside[0] -= 1

    monkeypatch.setattr(gitconfig, "git_entries", counting)
    monkeypatch.setattr(gitconfig, "read_git_config", reading)
    return seen


def test_a_second_read_of_an_unchanged_config_parses_nothing(
    tmp_path: Path, parses: list[int]
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
    # git 2.34 lists the remotes of 30,000 branch sections in about two
    # seconds, past the origin's one, and the origin records none (ENG-227).
    if got.repo is not None or _git_version() >= (2, 35):
        assert got.repo == REMOTE and _same(got.worktree_root, app)
    assert len(parses) == 1, parses

    kept = gitconfig._FORMAT_MEMO[(str(config), gitfile_rule._FORMAT_NAMES)][1]
    assert {name for name, _value in kept.entries} <= gitfile_rule._FORMAT_NAMES
    assert len(kept.entries) <= 4


@pytest.mark.parametrize("how", ["git_config", "in_place"])
def test_a_rewritten_config_is_read_again(
    tmp_path: Path, parses: list[int], how: str
) -> None:
    """A config git rewrites (a new file renamed over it) and one written in
    place at the same size are both parsed again, and the change is read."""
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


def test_a_config_written_just_now_is_kept_at_once(
    tmp_path: Path, parses: list[int]
) -> None:
    """No clock decides what is kept: a config written a moment before its
    first read is parsed once, however recent its times."""
    app, _other = _apps(tmp_path)
    for _ in range(2):
        gitfile_rule.tree_format(str(app / ".git"))
    assert len(parses) == 1


def test_a_rewrite_that_leaves_the_stamp_as_it_was_is_read_again(
    tmp_path: Path, parses: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's case: a server whose clock runs an hour behind this
    host's, with whole-second times (sshfs), so the config reads as long
    settled at its first read, and a rewrite in place at the same size
    within the server's second, which leaves the inode, the size and both
    times as they were. The server's stat answers the same at both reads
    (the change time too, which no test can set: `fstat` answers it), and
    the change is read: the digest is of the bytes."""
    app, _other = _apps(tmp_path)
    git_dir = app / ".git"
    config = git_dir / "config"
    behind = (time.time_ns() // 1_000_000_000 - 3600) * 1_000_000_000
    os.utime(config, ns=(behind, behind))
    first = os.stat(config)
    server = SimpleNamespace(
        st_mode=first.st_mode,
        st_dev=first.st_dev,
        st_ino=first.st_ino,
        st_size=first.st_size,
        st_mtime_ns=behind,
        st_ctime_ns=behind,
    )
    real = os.fstat

    def as_the_server(fd: int) -> Any:
        found = real(fd)
        return server if os.path.samestat(found, first) else found

    monkeypatch.setattr(os, "fstat", as_the_server)
    assert gitfile_rule.tree_format(str(git_dir)).bare is False
    assert len(parses) == 1

    body = config.read_bytes()
    with open(config, "r+b") as fh:
        fh.seek(body.index(b"bare = false"))
        fh.write(b"bare =  true")
    os.utime(config, ns=(behind, behind))
    after = os.stat(config)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        first.st_ino,
        first.st_size,
        first.st_mtime_ns,
    )
    assert gitfile_rule.tree_format(str(git_dir)).bare is True
    assert len(parses) == 2


def test_the_memo_is_bounded(
    tmp_path: Path, parses: list[int], monkeypatch: pytest.MonkeyPatch
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
