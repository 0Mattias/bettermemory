"""A claimed path reaches git as the path it names, never as a pattern.

Git reads a plain pathspec as a pattern: ``src/foo[1].py`` also matched
``src/foo1.py``, and a file named ``:(glob)**`` matched every commit. The
author-date logs over-counted for such a path while the reachable walk,
which compares paths, did not, so the two bases disagreed about one memory
(ENG-183). Every log, phantom check and patch stream verify runs over a
spec now takes it with the ``:(literal)`` magic
(`verify._literal_pathspecs`); where the environment already makes git
read every pathspec literally the specs pass as they stand.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory.claims import load_claims, parse_claim
from bettermemory.origin import commits_since_anchor
from bettermemory.verify import (
    BASIS_AUTHOR_DATE,
    BASIS_REACHABILITY,
    _quiescent_drift_applicable,
    resolve_commit_drift,
    resolve_commit_drift_count,
)

from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

_T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
_VERIFIED = _T0 + timedelta(hours=1)
_MODULE = 'LIMITS = {\n    "soft": 1,\n    "hard": 2,\n}\n'

#: A path named like a pattern, and the plain path the pattern also names.
_BRACKET, _TWIN = "src/foo[1].py", "src/foo1.py"
#: A path named like a magic pathspec; a name Windows cannot hold.
_MAGIC = ":(glob)**"


@pytest.fixture(autouse=True)
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, no inherited repository or
    pathspec variables."""
    config = tmp_path / "gitconfig"
    config.write_text("[gc]\n\tauto = 0\n[maintenance]\n\tauto = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
    ):
        monkeypatch.delenv(name, raising=False)
    set_git_discovery_ceiling(tmp_path, monkeypatch)


def _git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = os.environ.copy()
    env.update(
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
    )
    # The fixture's own git calls read pathspecs as git does by default.
    env.pop("GIT_LITERAL_PATHSPECS", None)
    env.pop("GIT_GLOB_PATHSPECS", None)
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    done = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(repo: Path, files: dict[str, str], when: datetime) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change", when=when)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository verified at `_VERIFIED` with HEAD at its first commit,
    then one commit to the bracketed file's twin alone, one to the
    bracketed file alone, and one to an unrelated file; the magic-named
    file (POSIX) is never touched after the first commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    first = {_BRACKET: _MODULE, _TWIN: "x = 0\n", "src/other.py": "y = 0\n"}
    if os.name != "nt":
        first[_MAGIC] = "magic\n"
    _commit(repo, first, _T0)
    head = _git(repo, "rev-parse", "HEAD")
    at = _VERIFIED + timedelta(hours=1)
    _commit(repo, {_TWIN: "x = 1\n"}, at)
    _commit(
        repo,
        {_BRACKET: _MODULE.replace('"hard": 2', '"hard": 3')},
        at + timedelta(hours=1),
    )
    _commit(repo, {"src/other.py": "y = 1\n"}, at + timedelta(hours=2))
    return Path(os.path.realpath(repo)), head


def _count(
    repo: Path,
    head: str,
    basis: str,
    *,
    anchors: tuple[str, ...],
    claims: tuple[str, ...],
) -> int | None:
    walk = None
    if basis == BASIS_REACHABILITY:
        walk = commits_since_anchor(repo, head, toplevel=repo)
        assert walk is not None and len(walk.commits) == 3
    resolved = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=3,
        anchors=anchors,
        claims=[parse_claim(c) for c in claims],
        toplevel=repo,
        walk=walk,
    )
    return None if resolved is None else resolved.count


_CASES = [
    # An attested bracketed path counts its own commit, not its twin's.
    pytest.param((_BRACKET,), (), 1, id="bracket-anchor"),
    # The same path under an unaddressable claim, in the any-touch leg.
    pytest.param(
        (), (f"{_BRACKET}::LIMITS={{'soft': 1, 'hard': 2}}",), 1, id="bracket-claim"
    ),
    # A path claim on it is governed: the weak tier reads no deletion.
    pytest.param((), (_BRACKET,), 0, id="bracket-path-claim"),
    # A magic-named file untouched since the verify counts nothing.
    pytest.param(
        (_MAGIC,),
        (),
        0,
        id="magic-anchor",
        marks=pytest.mark.skipif(
            os.name == "nt", reason="Windows names hold no ':' or '*'"
        ),
    ),
]


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
@pytest.mark.parametrize(("anchors", "claims", "expected"), _CASES)
def test_a_pattern_shaped_path_counts_as_the_path_it_names(
    tmp_path: Path,
    basis: str,
    anchors: tuple[str, ...],
    claims: tuple[str, ...],
    expected: int,
) -> None:
    repo, head = _repo(tmp_path)
    assert _count(repo, head, basis, anchors=anchors, claims=claims) == expected


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GIT_LITERAL_PATHSPECS", "1"),
        ("GIT_GLOB_PATHSPECS", "1"),
        ("GIT_NOGLOB_PATHSPECS", "1"),
    ],
)
def test_the_count_holds_under_the_pathspec_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """A user's environment that changes how git reads every pathspec
    leaves the count as it is: under ``GIT_LITERAL_PATHSPECS`` the specs
    pass without the magic (git would read the prefix as a name), and
    the magic outranks ``GIT_GLOB_PATHSPECS``."""
    repo, head = _repo(tmp_path)
    monkeypatch.setenv(variable, value)
    for basis in (BASIS_REACHABILITY, BASIS_AUTHOR_DATE):
        assert _count(repo, head, basis, anchors=(_BRACKET,), claims=()) == 1, basis


def test_the_magic_survives_a_single_pathspec_log_that_follows_renames(
    tmp_path: Path,
) -> None:
    """``log.follow`` turns a single-pathspec log into ``--follow``, which
    takes a literal pathspec."""
    repo, head = _repo(tmp_path)
    _git(repo, "config", "log.follow", "true")
    assert _count(repo, head, BASIS_AUTHOR_DATE, anchors=(_BRACKET,), claims=()) == 1


#: Claimed paths the path gate refuses, as a hand-edited frontmatter list
#: can carry them: a network share, a URL, and a climb out of the
#: repository to a host-named directory.
_HOST = "evil.example"
_REFUSED_CLAIMS = (
    f"//{_HOST}/share/x.py",
    f"file://{_HOST}/share/x.py",
    f"src/../../{_HOST}/x.py",
)


@pytest.fixture
def git_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The argv of every git process spawned from here on."""
    seen: list[list[str]] = []
    real = subprocess.run

    def run(args: Any, *rest: Any, **kwargs: Any) -> Any:
        seen.append([str(a) for a in args])
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return seen


@pytest.mark.parametrize("basis", [BASIS_REACHABILITY, BASIS_AUTHOR_DATE])
def test_a_claim_the_path_gate_refuses_never_reaches_git(
    tmp_path: Path, git_argv: list[list[str]], basis: str
) -> None:
    """A claimed path became a pathspec as it stood: git on Windows
    resolves the components of an absolute pathspec outside the worktree,
    which opens the host, and a ``..`` claim made git exit 128. Read
    beside a path claim on the bracketed file, the count is that claim's
    alone (the governed path claim reads no deletion: 0)."""
    repo, head = _repo(tmp_path)
    claims = load_claims([*_REFUSED_CLAIMS, _BRACKET])
    assert len(claims) == len(_REFUSED_CLAIMS) + 1, "premise: the read keeps them"
    walk = None
    if basis == BASIS_REACHABILITY:
        walk = commits_since_anchor(repo, head, toplevel=repo)
    git_argv.clear()
    resolved = resolve_commit_drift(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=3,
        anchors=(),
        claims=claims,
        toplevel=repo,
        walk=walk,
    )
    count = resolve_commit_drift_count(
        cwd=repo,
        since=_VERIFIED,
        unfiltered=3,
        anchors=(),
        claims=claims,
        toplevel=repo,
        walk=walk,
    )
    assert not [argv for argv in git_argv if any(_HOST in a for a in argv)]
    assert resolved is not None and resolved.count == 0
    assert count == 0


def test_a_quiescent_check_hands_git_no_refused_claim(
    tmp_path: Path, git_argv: list[list[str]]
) -> None:
    """Claims that are all refused leave the quiescent branch nothing to
    ask git about: not applicable, and no process names the host."""
    repo, _head = _repo(tmp_path)
    claims = load_claims(list(_REFUSED_CLAIMS))
    git_argv.clear()
    assert _quiescent_drift_applicable(repo, (), claims, toplevel=repo) is False
    assert not [argv for argv in git_argv if any(_HOST in a for a in argv)]
