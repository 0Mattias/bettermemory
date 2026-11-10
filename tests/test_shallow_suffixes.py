"""Claim 10: a hand-written shallow line with text after the hash still cuts
git's history (git parses the first 40 hex digits), but `cuts_in` returns the
whole line, so no logged hash equals it and the cut history is counted whole."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bettermemory import gitfile_rule, origin_dates, origin_history
from bettermemory.origin_history import HistoryUnread

from .test_multiline_literal_drift import neutral_git

__all__ = ["neutral_git"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def _repo(tmp_path: Path, object_format: str = "sha1") -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", "--object-format=" + object_format)
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "a")
    for i in range(4):
        (repo / "f.txt").write_text(f"{i}\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", f"c{i}")
    assert len(_git(repo, "log", "--format=%H").split()) == 4
    return repo, _git(repo, "rev-parse", "HEAD~1")


@pytest.mark.parametrize("suffix", ["", " junk", "XYZ", "#note"])
def test_a_shallow_line_with_trailing_text_is_still_a_cut(
    tmp_path: Path, suffix: str
) -> None:
    repo, cut = _repo(tmp_path)
    (repo / ".git" / "shallow").write_text(f"{cut}{suffix}\n")
    # Premise: git cuts the history at the line's hash, whatever follows it.
    assert len(_git(repo, "log", "--format=%H").split()) == 2
    assert origin_history.cuts(repo) == frozenset({cut})
    assert origin_dates.commit_author_timestamps(repo) is HistoryUnread.SHALLOW
    assert (
        origin_dates.commit_author_timestamps_touching_pathspecs(repo, ["f.txt"])
        is None
    )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize(
    "suffix", ["", " junk", "XYZ", "#note", "a" * 24, "\x00ignored", "\vignored"]
)
def test_suffix_width_comes_from_the_actual_storage_format(
    tmp_path: Path, object_format: str, suffix: str
) -> None:
    repo, cut = _repo(tmp_path, object_format)
    shallow = repo / ".git" / "shallow"
    shallow.write_bytes((cut.upper() + suffix + "\n").encode("ascii"))
    assert _git(repo, "rev-parse", "--show-object-format") == object_format
    assert len(_git(repo, "log", "--format=%H").split()) == 2
    assert origin_history.cuts(repo) == frozenset({cut})
    assert origin_dates.commit_author_timestamps(repo) is HistoryUnread.SHALLOW
    assert (
        origin_dates.commit_author_timestamps_touching_pathspecs(repo, ["f.txt"])
        is None
    )


@pytest.mark.parametrize("suffix", ["", " junk", "XYZ", "#note"])
def test_whole_history_reads_shallow(tmp_path: Path, suffix: str) -> None:
    repo, cut = _repo(tmp_path)
    (repo / ".git" / "shallow").write_text(f"{cut}{suffix}\n")
    assert len(_git(repo, "log", "--format=%H").split()) == 2
    assert origin_dates.commit_author_timestamps(repo) is HistoryUnread.SHALLOW


@pytest.mark.parametrize("suffix", ["", " junk", "XYZ", "#note"])
def test_touching_log_declines(tmp_path: Path, suffix: str) -> None:
    repo, cut = _repo(tmp_path)
    (repo / ".git" / "shallow").write_text(f"{cut}{suffix}\n")
    origin_dates.commit_author_timestamps(repo)
    assert (
        origin_dates.commit_author_timestamps_touching_pathspecs(repo, ["f.txt"])
        is None
    )


@pytest.mark.parametrize("bad", [b"\n", b" ", b"a" * 39, b"g" * 40, b" " + b"a" * 40])
def test_a_line_git_refuses_declines_the_cut_read(tmp_path: Path, bad: bytes) -> None:
    repo, _ = _repo(tmp_path)
    (repo / ".git" / "shallow").write_bytes(bad)
    done = subprocess.run(["git", "log", "--format=%H"], cwd=repo, capture_output=True)
    assert done.returncode != 0, "premise: Git refuses this shallow record"
    assert origin_history.cuts(repo) is None


def test_a_linked_worktree_uses_the_common_format_and_shallow_file(
    tmp_path: Path,
) -> None:
    repo, cut = _repo(tmp_path, "sha256")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "linked", str(linked))
    (repo / ".git" / "shallow").write_text(cut + "a" * 24 + "\n")
    assert len(_git(linked, "log", "--format=%H").split()) == 2
    assert origin_history.cuts(linked) == frozenset({cut})
    assert origin_dates.commit_author_timestamps(linked) is HistoryUnread.SHALLOW


@pytest.mark.parametrize(
    "config, expected",
    [
        (b"", b"sha1"),
        (b"[extensions]\nobjectformat=sha256\n", b"sha1"),
        (
            b"[core]\nrepositoryformatversion=1\n[extensions]\nobjectformat=sha256\n",
            b"sha256",
        ),
        (
            b"[core]\nrepositoryformatversion=1\n[extensions]\nobjectformat=unknown\n",
            None,
        ),
        (b"[core]\nrepositoryformatversion=99\n", None),
        (b"[include]\npath=other\n", b"sha1"),
        (b"[broken", None),
    ],
)
def test_cut_format_uses_the_existing_repository_format_rules(
    tmp_path: Path, config: bytes, expected: bytes | None
) -> None:
    (tmp_path / "config").write_bytes(config)
    (tmp_path / "other").write_bytes(b"[extensions]\nobjectformat=sha256\n")
    assert gitfile_rule.object_format_at(str(tmp_path)) == expected
    (tmp_path / "shallow").write_bytes(b"a" * 64 + b"suffix\n")
    got = origin_history.cuts_in(tmp_path / "shallow")
    if expected is None:
        assert got is None
    else:
        width = 64 if expected == b"sha256" else 40
        assert got == frozenset({"a" * width})
