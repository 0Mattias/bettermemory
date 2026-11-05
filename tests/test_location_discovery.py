"""ENG-232: location's discovery walk is git's.

The capture places the caller in the repository git's discovery finds
from its folder, and in none where git finds none or dies. Its walk is
`githead.probed`, the one `githead.entry_above` takes:

* a ``GIT_CEILING_DIRECTORIES`` entry is resolved through its links, as
  git does, unless it follows an empty entry;
* ``GIT_DISCOVERY_ACROSS_FILESYSTEM`` is read as git reads a boolean;
* a value git dies on leaves the capture INDETERMINATE.

Before, a ceiling was compared as written, so a symbolic spelling of it
was walked past, and any value of the variable that was not empty read as
true. Each case is checked against ``git rev-parse --show-toplevel`` from
the same folder, and at the surfaces: the session brief, search's auto
scope and a write's home.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory import githead, location
from bettermemory.location import INDETERMINATE, OUTSIDE, REPO
from bettermemory.membership import SEARCH_AUTO_SCOPE, SESSION_BRIEF

from .test_membership_resolution import Lab
from .test_membership_security import neutral_git

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH"),
    pytest.mark.skipif(os.name != "posix", reason="symbolic links and devices"),
]


@pytest.fixture(autouse=True)
def _neutral(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    neutral_git(tmp_path, monkeypatch)
    monkeypatch.delenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", raising=False)
    location.forget()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """Repository A at ``above``, and the folder ``above/work/deep`` in it,
    which is no repository of its own; ``link`` names ``above/work`` and
    ``home`` names ``above`` through a symbolic link."""
    above = tmp_path / "above"
    start = above / "work" / "deep"
    start.mkdir(parents=True)
    assert _git(above, "init", "-q", "-b", "main").returncode == 0
    (tmp_path / "link").symlink_to(above / "work")
    (tmp_path / "home").symlink_to(above)
    return above, start


def _captured(start: Path) -> location.Capture:
    location.forget()
    return location.capture(str(start), write_token=False, use_cache=False)


def _git_finds(start: Path) -> str | None:
    """The working tree git's discovery finds from `start`, or None."""
    found = _git(start, "rev-parse", "--show-toplevel")
    return os.path.realpath(found.stdout.strip()) if found.returncode == 0 else None


#: Where a ceiling stands, as ``GIT_CEILING_DIRECTORIES`` names it, and
#: whether git's walk from ``above/work/deep`` still reaches A.
_CEILINGS = {
    "the test's floor": True,
    "above/work": False,
    "above": False,
    "the folder over above": True,
    "a path that is not there": True,
    "above/work through a link": False,
    "above through a link (a dotfiles repository at a linked home)": False,
    "above/work through a link, after an empty entry": True,
    "a relative path": True,
    "the start itself": True,
}


@pytest.mark.parametrize("ceiling", list(_CEILINGS))
def test_the_capture_stops_where_a_ceiling_stops_git(
    ceiling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git probes no directory at or above a ``GIT_CEILING_DIRECTORIES``
    entry that is a strict ancestor of its folder, each entry resolved
    through its links unless it follows an empty entry, a relative one
    dropped. The capture is in A exactly where git finds A; where git
    finds nothing it is outside any repository. Before, an entry naming
    A through a link (``$HOME`` where home is linked and a dotfiles
    repository stands there) was walked past, into A."""
    above, start = _layout(tmp_path)
    entries = {
        "the test's floor": str(tmp_path),
        "above/work": str(above / "work"),
        "above": str(above),
        "the folder over above": str(tmp_path),
        "a path that is not there": str(tmp_path / "gone" / "away"),
        "above/work through a link": str(tmp_path / "link"),
        "above through a link (a dotfiles repository at a linked home)": str(
            tmp_path / "home"
        ),
        "above/work through a link, after an empty entry": f":{tmp_path / 'link'}",
        "a relative path": "above/work",
        "the start itself": str(start),
    }
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", entries[ceiling])

    found = _git_finds(start)
    got = _captured(start)

    assert (found is not None) is _CEILINGS[ceiling], "as git's own walk"
    if found is None:
        assert got.state == OUTSIDE and got.root is None
    else:
        assert (got.state, got.root) == (REPO, os.path.realpath(above))


#: ``GIT_DISCOVERY_ACROSS_FILESYSTEM`` as set: each spelling git reads as a
#: boolean, and some it dies on.
_SPELLINGS = [
    None,
    "",
    "1",
    "0",
    "2",
    "-1",
    "1k",
    "true",
    "false",
    "Yes",
    "no",
    "on",
    "OFF",
    "maybe",
    "garbage",
    "1x",
    " true",
]


def _git_reads(value: str, tmp_path: Path) -> bool | None:
    """The boolean git reads `value` as (``git config --type=bool``, the
    reader ``git_env_bool`` shares), or None where git dies on it."""
    done = _git(
        tmp_path, "-c", f"bm.across={value}", "config", "--type=bool", "bm.across"
    )
    if done.returncode != 0:
        return None
    return {"true": True, "false": False}[done.stdout.strip()]


def _across(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", raising=False)
    else:
        monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", value)


@pytest.mark.parametrize("value", _SPELLINGS)
def test_a_value_git_dies_on_leaves_the_capture_indeterminate(
    value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git reads ``GIT_DISCOVERY_ACROSS_FILESYSTEM`` before its walk, and
    dies on a value that is no boolean: it finds no repository anywhere,
    and the capture declines (INDETERMINATE), neither in A nor outside any
    repository. A value it reads leaves the walk on one filesystem as it
    was, in A. Before, ``maybe`` read as true, and the capture was in A."""
    above, start = _layout(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    _across(monkeypatch, value)

    done = _git(start, "rev-parse", "--show-toplevel")
    got = _captured(start)

    reads = None if value is None else _git_reads(value, tmp_path)
    dies = value is not None and reads is None
    assert (done.returncode != 0) is dies, done.stderr
    assert githead.across_filesystems() == (False if value is None else reads)
    if dies:
        assert "bad boolean" in done.stderr
        assert got.state == INDETERMINATE and got.root is None
        assert "GIT_DISCOVERY_ACROSS_FILESYSTEM" in (got.detail or "")
    else:
        assert (got.state, got.root) == (REPO, os.path.realpath(above))


@pytest.mark.parametrize("value", _SPELLINGS)
def test_the_capture_stops_at_a_filesystem_boundary_as_git_reads_the_value(
    value: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``above`` on another filesystem than its folder (its device read so
    by the walk, `githead.probed`): git's walk stops at the boundary
    unless the variable reads true as git reads a boolean, so the capture
    is in A only then, outside any repository where it reads false
    (``0``, ``false``, ``no``, ``off``, empty), and INDETERMINATE where
    git dies on it. Before, every value that was not empty crossed."""
    above, start = _layout(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    real_device = githead._device
    other = os.path.realpath(above)

    def device(path: str) -> int:
        return real_device(path) + (1 if path == other else 0)

    monkeypatch.setattr(githead, "_device", device)
    _across(monkeypatch, value)

    got = _captured(start)

    reads = False if value is None else _git_reads(value, tmp_path)
    if reads is None:
        assert got.state == INDETERMINATE
    elif reads:
        assert (got.state, got.root) == (REPO, other)
    else:
        assert got.state == OUTSIDE and got.root is None


def test_a_folder_git_finds_no_repository_from_is_kept_from_the_one_above(
    tmp_path: Path, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's shape at the surfaces: a dotfiles repository at a home
    named through a link, ``GIT_CEILING_DIRECTORIES=$HOME`` so spelled,
    and a session in a folder below it that is no repository. Git finds
    none there. Before, the capture put the session in the dotfiles
    repository's project: its brief and search's auto scope delivered
    that project's memory as the caller's own, and a write from there was
    homed to it. Now the folder is outside any repository: the memory is
    not delivered as the caller's, and the write is homed elsewhere."""
    above, start = _layout(tmp_path)
    assert (
        _git(above, "commit", "-q", "--allow-empty", "-m", "dotfiles").returncode == 0
    )
    lab = Lab(local_tmp_path / "lab", write_token=False)
    try:
        lab.write("dotfiles", above)
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path / "home"))
        location.forget()
        assert _git_finds(start) is None

        res, brief = lab.view(start)
        written = lab.write("below", start)

        assert res.checkout != written.checkout or res.checkout is None
        assert "dotfiles" not in brief
        assert "dotfiles" not in lab.admitted(res, SEARCH_AUTO_SCOPE)
        assert "dotfiles" not in lab.admitted(res, SESSION_BRIEF)
        assert lab.project_of("below") != lab.project_of("dotfiles")
    finally:
        lab.close()
